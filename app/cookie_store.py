# -*- coding: utf-8 -*-
"""Cookies 复用：把「设置」里配好的 cookies.txt 同时喂给 HTTP 抓取与浏览器渲染。

为什么要它（2026-09-22 用户反馈）：Instagram 这类平台对未登录访问**根本不返回内容**
（页面原文是「链接可能已损坏或主页被移除」），Pinterest 的登录后视图也与匿名不同。
此前 cookies 只传给了 yt-dlp，网页抓取与渲染都是「干净访客」——
于是「粘主页抓取」在这些平台上永远 0 条，用户却无从判断。
设置页早就提供了 cookies.txt 与「从浏览器读取」两项，这里把它们接到抓取链路上。

格式：Netscape cookies.txt（浏览器扩展导出的标准格式），与 yt-dlp 同一份文件。
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

_provider = None            # () -> str：返回 cookies 文件路径（空 = 不用）
_cache: dict = {"path": None, "mtime": None, "rows": [], "ts": 0.0}
_lock = threading.Lock()
TTL = 30.0                  # 缓存有效期：文件被重新导出后最多 30 秒生效


def set_provider(fn) -> None:
    """注册「cookie 文件从哪来」（由 app.main 启动时注册读 settings 的函数）。"""
    global _provider
    _provider = fn


def _from_store() -> str:
    """没注册 provider 时的兜底：直接读 config.json（独立脚本/诊断工具也生效）。"""
    try:
        from .store import load
        return str(load().get("cookies_file") or "")
    except Exception:  # noqa: BLE001
        return ""


def cookie_file() -> str:
    """当前生效的 cookies 文件路径（没配则为空字符串）。"""
    if not _provider:
        return _from_store()
    try:
        return str(_provider() or "")
    except Exception:  # noqa: BLE001
        return ""


def _load_rows() -> list[dict]:
    """读取并缓存 Netscape 格式 cookies，返回统一结构的行。"""
    path = cookie_file()
    if not path:
        return []
    p = Path(path).expanduser()
    if not p.exists():
        return []
    try:
        mtime = p.stat().st_mtime
    except OSError:
        return []
    with _lock:
        fresh = (time.time() - _cache["ts"] < TTL and _cache["path"] == str(p)
                 and _cache["mtime"] == mtime)
        if fresh:
            return _cache["rows"]
    rows: list[dict] = []
    try:
        # 手写解析 Netscape 格式（7 列制表符分隔），**不落临时文件**。
        #
        # 为什么不沿用 MozillaCookieJar：它要一个文件路径，我原先的实现得先把内容
        # 写到临时文件再 load、再 unlink —— 既慢又脆（实测在本机沙箱里删临时文件会被
        # 安全策略拦下，直接让 test_cookies_offline 整支失败）。手写解析同时也自然处理了
        # `#HttpOnly_` 前缀与注释行（jar.load 会跳过以 # 开头的行，反而要额外剥前缀）。
        #
        # 列：domain, include_subdomains, path, secure, expires, name, value
        for raw in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw.rstrip("\r\n")
            if not line.strip():
                continue
            if line.startswith("#HttpOnly_"):
                line = line[len("#HttpOnly_"):]
            elif line.lstrip().startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) < 7:
                continue
            dom, _inc, cpath, secure, expires, name, value = parts[:7]
            if not name:
                continue
            try:
                exp = int(float(expires))
            except ValueError:
                exp = -1
            rows.append({
                "name": name,
                "value": value,
                "domain": dom or "",
                "path": cpath or "/",
                "secure": str(secure).strip().upper() == "TRUE",
                "expires": exp,
            })
    except Exception:  # noqa: BLE001
        rows = []
    with _lock:
        _cache.update({"path": str(p), "mtime": mtime, "rows": rows, "ts": time.time()})
    return rows


def requests_cookies():
    """给 requests 用的 cookie jar（没配/读不到返回 None）。"""
    rows = _load_rows()
    if not rows:
        return None
    try:
        from requests.cookies import RequestsCookieJar
    except ImportError:  # pragma: no cover
        return None
    jar = RequestsCookieJar()
    for r in rows:
        try:
            jar.set(r["name"], r["value"], domain=r["domain"].lstrip("."),
                    path=r["path"] or "/")
        except Exception:  # noqa: BLE001
            continue
    return jar or None


def playwright_cookies() -> list[dict]:
    """给 playwright `context.add_cookies` 用的列表（没配/读不到返回空）。"""
    out = []
    for r in _load_rows():
        dom = r["domain"] or ""
        if not dom:
            continue
        item = {"name": r["name"], "value": r["value"],
                "domain": dom, "path": r["path"] or "/"}
        if r["secure"]:
            item["secure"] = True
        # 不写 expires：会话 cookie 与会话生命周期绑定，避免因过期时间格式
        # 引起 add_cookies 报错（playwright 要求 expires 是秒级浮点或 -1）。
        out.append(item)
    return out


def status() -> dict:
    """给界面/诊断用：当前是否启用了 cookies、条数、来源文件。"""
    f = cookie_file()
    rows = _load_rows()
    return {"file": f, "exists": bool(f) and Path(f).expanduser().exists(),
            "count": len(rows), "domains": sorted({r["domain"] for r in rows})[:12]}


def import_from_browser(browser: str, out_dir: str = "") -> dict:
    """把浏览器里的 Cookies 导出成 Netscape 文件（一键复用登录态）。

    为什么要它：Instagram 这类平台未登录访问**连主页数据都不返回**，
    而让用户自己装扩展、导出、找路径，实际很少有人做完。
    这里直接用 yt-dlp 的读浏览器能力（它自己处理 Windows DPAPI / 各种加密），
    导出成标准 cookies.txt 后，网页抓取 / 浏览器渲染 / yt-dlp 三条链路就都能用了。

    返回 {"ok": bool, "path": str, "count": int, "error": str, "hint": str}
    常见失败：浏览器正在运行 → 数据库被锁（Chrome/Edge）；此时提示先完全退出浏览器。
    """
    browser = (browser or "").strip().lower()
    if not browser or browser == "none":
        return {"ok": False, "path": "", "count": 0,
                "error": "没有选择浏览器（可选 chrome / edge / firefox / brave）", "hint": ""}
    try:
        from yt_dlp.cookies import extract_cookies_from_browser
    except ImportError:
        return {"ok": False, "path": "", "count": 0,
                "error": "当前环境没有 yt-dlp，无法读取浏览器 Cookies", "hint": ""}
    try:
        jar = extract_cookies_from_browser(browser)
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        low = msg.lower()
        if "could not copy" in low or "database" in low or "locked" in low or "permission" in low:
            hint = ("浏览器正在运行会锁住 cookie 数据库。请**完全退出**该浏览器"
                    "（任务栏图标也要退出）后再点一次；或改用「从扩展导出 cookies.txt」（无需关浏览器）。")
        elif "unsupported browser" in low or "no such" in low:
            hint = "不支持的浏览器名，可试 chrome / edge / firefox / brave。"
        else:
            hint = "可改用「从扩展导出 cookies.txt」的方式（更稳）。"
        return {"ok": False, "path": "", "count": 0,
                "error": f"读取 {browser} 的 Cookies 失败：{msg[:160]}", "hint": hint}
    n = len(list(jar)) if jar else 0
    if not n:
        return {"ok": False, "path": "", "count": 0,
                "error": f"{browser} 里没读到任何 Cookies（可能没登录过任何站点）", "hint": ""}
    if not out_dir:
        from .config import DATA_ROOT  # 局部导入避免环

        out_dir = str(DATA_ROOT / "cookies")
    folder = Path(out_dir)
    folder.mkdir(parents=True, exist_ok=True)
    dst = folder / f"cookies_{browser}.txt"
    try:
        jar.save(str(dst), ignore_discard=True, ignore_expires=True)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "path": "", "count": n,
                "error": f"Cookies 读到了（{n} 条）但写文件失败：{e}", "hint": ""}
    with _lock:
        _cache.update({"path": None, "mtime": None, "rows": [], "ts": 0.0})
    return {"ok": True, "path": str(dst), "count": n, "error": "", "hint": ""}
