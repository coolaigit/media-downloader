# -*- coding: utf-8 -*-
"""登录助手：用一个**有头**浏览器打开站点，让用户登录一次，登录态留在 profile 目录里。

为什么需要（2026-09-23）：
  Instagram / Pinterest / 抖音 / 小红书 的主页内容对**未登录访客**要么不给，要么只给空壳。
  我们不去伪造签名、也不去偷用户的浏览器登录态 —— 而是给用户一个「登录一次、之后复用」的
  浏览器配置目录：之后所有渲染（含拦截页面自己发的签名接口响应）都用它。

用法（作为独立进程跑，避免阻塞服务）：
    python -m app.login_browser <url> [--dir <profile目录>] [--seconds 900] [--headless no]

约定：进程退出（用户关掉窗口 / 到期）时，把本次登录结果写到
    <dir>/../login_state.json
内容含：时间、profile 目录、以及 profile 里各域的 cookie 数量（用于判断「登录成功没有」）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path


def _marker_path(profile_dir: str) -> Path:
    # 状态/Cookie 放在 profile 的父目录：Chrome profile 本身只保存浏览器数据；
    # 主服务与 gallery-dl/yt-dlp 也统一从父目录读取，避免“登录成功但下载仍未登录”。
    return Path(profile_dir).resolve().parent / "login_state.json"


def _cookie_path(profile_dir: str) -> Path:
    return Path(profile_dir).resolve().parent / "cookies_from_profile.txt"


def _write_state(profile_dir: str, url: str, domains: dict, note: str = "",
                 cookie_file: str = "", cookie_count: int = 0) -> None:
    try:
        p = _marker_path(profile_dir)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({
            "ts": int(time.time()), "profile_dir": str(profile_dir), "url": url,
            "domains": domains, "note": note,
            "cookie_file": cookie_file, "cookie_count": cookie_count,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def _export_netscape(cookies: list, dst: "Path") -> int:
    """把浏览器里的 Cookies 导出成标准 Netscape cookies.txt（yt-dlp 直接能用）。

    为什么要顺手导出（2026-09-26）：
      登录态 profile 只对**渲染**这条路有效；yt-dlp 需要的是 cookies.txt。
      两边都想要就得登两次、导出两次 —— 所以在关闭登录窗口时顺手导出一份，
      设置里的「Cookies 文件」就能自动指向它，yt-dlp 那条路同时打通。
    """
    import time as _t
    lines = ["# Netscape HTTP Cookie File", "# 由「打开浏览器去登录」自动导出", ""]
    n = 0
    for c in cookies or []:
        dom = (c.get("domain") or "").strip()
        if not dom:
            continue
        name = c.get("name") or ""
        val = c.get("value") or ""
        if not name:
            continue
        try:
            exp = int(c.get("expires") or 0)
        except (TypeError, ValueError):
            exp = 0
        if exp <= 0:
            exp = int(_t.time()) + 86400 * 30          # 会话 cookie 给 30 天
        prefix = "#HttpOnly_" if c.get("httpOnly") else ""
        secure = "TRUE" if c.get("secure") else "FALSE"
        # 第 2 列是「是否包含子域」标记，**必须**与域名是否以点开头一致，
        # 否则 Python/yt-dlp 的 cookiejar 直接判为非法格式（实测 LoadError：
        # www.pinterest.com 配 TRUE 会被拒）。
        subdom = "TRUE" if dom.startswith(".") else "FALSE"
        lines.append("\t".join([prefix + dom, subdom,
                                c.get("path") or "/", secure, str(exp), name, val]))
        n += 1
    try:
        dst.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError:
        return 0
    return n


def export_from_profile(profile_dir: str, headless: bool = True) -> dict:
    """不开新窗口，直接从一个已存在的 profile 里导出 cookies.txt。

    用途：登录窗口是用旧逻辑打开的、或者用户手动关了窗口但没走导出分支 ——
    这时用它兜底，把 profile 里的登录态落成 yt-dlp 能读的 cookies.txt。
    返回 {"ok", "path", "count", "domains", "error"}。
    """
    from playwright.sync_api import sync_playwright

    from .renderer import _LAUNCH_ARGS, _find_browser_channel
    from .config import DEFAULT_UA

    if not Path(profile_dir).is_dir():
        return {"ok": False, "path": "", "count": 0, "domains": {},
                "error": f"profile 目录不存在：{profile_dir}"}
    out = _cookie_path(profile_dir)
    with sync_playwright() as p:
        ch = _find_browser_channel()
        kw = dict(user_data_dir=str(profile_dir), headless=headless,
                  locale="zh-CN", user_agent=DEFAULT_UA,
                  args=list(_LAUNCH_ARGS))
        try:
            ctx = (p.chromium.launch_persistent_context(channel=ch, **kw) if ch
                   else p.chromium.launch_persistent_context(**kw))
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "path": "", "count": 0, "domains": {},
                    "error": f"打开 profile 失败（可能窗口还开着）：{e}"}
        try:
            cks = ctx.cookies()
        except Exception as e:  # noqa: BLE001
            cks = []
            err = str(e)
            ctx.close()
            return {"ok": False, "path": "", "count": 0, "domains": {}, "error": err}
        domains: dict = {}
        for c in cks:
            d = (c.get("domain") or "").lstrip(".")
            if d:
                domains[d] = domains.get(d, 0) + 1
        n = _export_netscape(cks, out)
        ctx.close()
    return {"ok": n > 0, "path": str(out) if n else "", "count": n,
            "domains": domains, "error": "" if n else "这个 profile 里没有任何 cookie"}


def run(url: str, profile_dir: str, seconds: int = 900, headless: bool = False) -> int:
    from playwright.sync_api import sync_playwright

    from .renderer import _LAUNCH_ARGS, _find_browser_channel
    from .config import DEFAULT_UA

    Path(profile_dir).mkdir(parents=True, exist_ok=True)
    channel = _find_browser_channel()
    with sync_playwright() as p:
        kwargs = dict(
            user_data_dir=profile_dir,
            headless=headless,
            viewport={"width": 1400, "height": 900},
            locale="zh-CN",
            user_agent=DEFAULT_UA,
            args=list(_LAUNCH_ARGS) + ["--disable-blink-features=AutomationControlled"],
        )
        ctx = None
        if channel:
            try:
                ctx = p.chromium.launch_persistent_context(channel=channel, **kwargs)
            except Exception:
                ctx = None
        if ctx is None:
            ctx = p.chromium.launch_persistent_context(**kwargs)
        pages = ctx.pages
        page = pages[0] if pages else ctx.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
        except Exception:
            pass
        print(f"[login-browser] 已打开 {url}，请在该窗口里完成登录，完成后关闭窗口即可。")
        sys.stdout.flush()

        deadline = time.time() + max(30, int(seconds))
        closed = False

        def _on_close(*_a):
            nonlocal closed
            closed = True

        ctx.on("close", _on_close)

        # 看门狗：到点**强制**收尾并退出。
        #
        # 为什么必须有（2026-09-26 实测）：playwright 的同步 API 在用户正与页面交互
        # （登录弹窗/验证码）时，ctx.cookies() 这类调用可能一直阻塞，主循环根本走不到
        # deadline 判断 —— 表现为「窗口一直不关、登录态也没落盘、抓取还用不上」。
        # 这里用独立线程到点就导出 cookies + 写状态 + 硬退出，不依赖主循环。
        def _hard_finish(*_a):
            try:
                cks = ctx.cookies()
            except Exception:
                cks = []
            try:
                _finish(profile_dir, url, cks)
            except Exception:
                pass
            os._exit(0)

        watchdog = threading.Timer(max(30, int(seconds)), _hard_finish)
        watchdog.daemon = True
        watchdog.start()

        while time.time() < deadline:
            if closed:
                break
            # 用户把窗口关掉后 ctx.pages 会变空；用 cookies() 探测存活
            try:
                ctx.cookies()
            except Exception:
                break
            time.sleep(2)

        watchdog.cancel()
        try:
            cks = ctx.cookies()
        except Exception:
            cks = []
        try:
            ctx.close()
        except Exception:
            pass
        _finish(profile_dir, url, cks)
        return 0


def _finish(profile_dir: str, url: str, raw_cookies: list) -> dict:
    """收尾：统计域、导出 cookies.txt、写状态文件。"""
    domains: dict = {}
    for c in raw_cookies or []:
        d = (c.get("domain") or "").lstrip(".")
        if d:
            domains[d] = domains.get(d, 0) + 1
    ck_path = _cookie_path(profile_dir)
    # ⚠️ 读不到 cookie（窗口已被关掉/会话异常）时**绝不能**覆盖旧导出 ——
    # 实测看门狗在窗口关闭后才触发，ctx.cookies() 抛错拿到空列表，
    # 一写就把之前导出的有效登录态清空了。
    if raw_cookies:
        n_ck = _export_netscape(raw_cookies, ck_path)
    else:
        n_ck = 0
    _write_state(profile_dir, url, domains,
                 cookie_file=str(ck_path) if n_ck else "", cookie_count=n_ck)
    print(f"[login-browser] 结束，登录态已保存到 {profile_dir}"
          f"（覆盖 {len(domains)} 个域）"
          + (f"；另导出 cookies.txt：{ck_path}（{n_ck} 条）" if n_ck else
             "；未读到 cookie，保留原有导出文件" if ck_path.exists() else ""))
    return {"domains": domains, "cookie_file": str(ck_path) if n_ck else "",
            "cookie_count": n_ck}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--dir", default="", help="profile 目录（默认项目 data/browser_profile）")
    ap.add_argument("--seconds", type=int, default=900, help="最长等待秒数")
    ap.add_argument("--headless", default="no")
    a = ap.parse_args()
    d = a.dir
    if not d:
        from .browser_profile import default_login_dir
        d = default_login_dir()
    return run(a.url, d, a.seconds, str(a.headless).lower() in ("yes", "true", "1", "y"))


if __name__ == "__main__":
    raise SystemExit(main())
