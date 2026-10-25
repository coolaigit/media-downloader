# -*- coding: utf-8 -*-
"""BrowserSkill 桥：只在「遇到验证墙」时升级，绝不进普通抓取链路。

V2.5 的状态：只认 PATH 里的 `bsk`，浏览器靠用户自己在 Chrome 商店装扩展。
换台电脑就等于没有 —— 这正是 V3.0 要解决的。

V3.0 起完全自包含，三件资产都随包：
    bin/bsk.exe        命令行 + 守护进程
    bsk-extension/     浏览器扩展（MV3，manifest 里带 key，加载后 ID 保持不变）
    browsers/chromium  内置内核（Phase 3），用它承载扩展

自包含链路（实测 2026-10-21，Chromium 151 + 扩展 0.3.3）：
    内置 Chromium --load-extension → 扩展连上 daemon → bsk browsers 里多一个实例
    → bsk session start --browser <该实例> → Agent Window
所以用户机器上没有 Chrome、没装过扩展，也能走完验证流程。

**定位不变（别改）**：
- 正常抓取（HTTP / 内置 Chromium 渲染）完全不经过这里。
- 只有检测到验证码 / 滑块 / 安全验证，才升级到「可见窗口 + 人来处理」。
- bsk 不可用时，普通抓取必须照常工作，只是验证墙解不开而已。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from .config import BUNDLE_DIR, DATA_ROOT

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

_SIMPLE_VERIFY_LABELS = {
    "verify", "verify you are human", "continue", "continue verification",
    "confirm", "i am human", "i'm not a robot",
    "验证", "继续", "继续验证", "开始验证", "确认", "我是人类", "我不是机器人",
}
_REF_LINE = re.compile(r'(@e\d+)\s+(?:button|link)\s+"([^"]+)"', re.I)

# 我们启动的那个浏览器进程（只用来善后，绝不杀用户自己的浏览器）
_browser_lock = threading.Lock()
_browser_proc: subprocess.Popen | None = None
# 记录当前实例是以什么模式起的。复用实例时必须比对它，
# 否则一次「可见」启动会让后续所有抓取都弹窗（2026-10-25 用户实测）。
_browser_headless: bool | None = None
_browser_instance: str = ""


# ---------- 资产 ----------

def bsk_binary() -> str:
    """bsk 可执行文件：包内 bin/ 优先，其次 PATH。

    包内优先的理由和 Chromium 一样 —— 用户机器上不一定装过 bsk，
    而且就算装过，版本也可能跟包里的扩展对不上（协议 1.3 才兼容）。
    """
    for cand in (BUNDLE_DIR / "bin" / "bsk.exe", BUNDLE_DIR / "bin" / "bsk"):
        if cand.is_file():
            return str(cand)
    return shutil.which("bsk") or shutil.which("bsk.exe") or ""


def extension_dir() -> Path:
    """内置扩展目录。frozen 下在 _internal/bsk-extension。"""
    return BUNDLE_DIR / "bsk-extension"


def chromium_exe() -> str:
    """承载扩展用的内核：内置 Chromium 优先，系统浏览器兜底。"""
    from . import browser_runtime
    t = browser_runtime.pick()
    return str(t.get("exe") or "")


def available() -> bool:
    return bool(bsk_binary())


def status() -> dict:
    """给设置页/诊断用的整体状态。"""
    bsk = bsk_binary()
    ext = extension_dir()
    daemon = daemon_status() if bsk else None
    return {
        "available": bool(bsk),
        "bsk_path": bsk,
        "bsk_bundled": bool(bsk.startswith(str(BUNDLE_DIR))),
        "extension_dir": str(ext),
        "extension_ok": (ext / "manifest.json").is_file(),
        "chromium": chromium_exe(),
        "daemon_running": bool(daemon and daemon.get("pid")),
        "daemon_version": (daemon or {}).get("daemon_version") or "",
        "browser_count": len((daemon or {}).get("browsers") or []),
        "our_browser": _browser_instance,
    }


# ---------- 命令执行 ----------

def _run(args: list[str], *, timeout: int = 45, check: bool = True) -> subprocess.CompletedProcess:
    bsk = bsk_binary()
    if not bsk:
        raise RuntimeError("BrowserSkill (bsk) 不可用")
    cp = subprocess.run(
        [bsk, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        creationflags=_CREATE_NO_WINDOW,
    )
    if check and cp.returncode != 0:
        msg = (cp.stderr or cp.stdout or f"bsk exit {cp.returncode}").strip()
        raise RuntimeError(msg[:600])
    return cp


def _json(args: list[str], *, timeout: int = 45, check: bool = True) -> dict:
    cp = _run([*args, "--json"], timeout=timeout, check=check)
    raw = (cp.stdout or "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"BrowserSkill 返回了无效 JSON: {e}") from e
    # status 有时直接返回浏览器列表，统一成 {"browsers": [...]}
    if isinstance(data, list):
        return {"browsers": data}
    return data


_daemon_cache: dict = {"ts": 0.0, "data": None}
_DAEMON_TTL = 5.0


def daemon_status(force: bool = False) -> dict | None:
    """读 daemon 状态；没跑或没装返回 None（不抛异常）。

    带 5 秒缓存：/api/status 会被前端频繁调用，每次去 spawn 一个 bsk 子进程太贵。
    轮询等待浏览器上线的那几秒必须 force=True，否则永远读不到新实例。
    """
    if not bsk_binary():
        return None
    now = time.time()
    if not force and now - float(_daemon_cache["ts"] or 0) < _DAEMON_TTL:
        return _daemon_cache["data"]
    try:
        st = _json(["status"], timeout=12, check=False)
    except Exception:  # noqa: BLE001
        st = None
    if not st or not st.get("pid"):
        st = None
    _daemon_cache.update(ts=now, data=st)
    return st


def ensure_daemon(timeout: float = 15.0) -> tuple[bool, str]:
    """确保 daemon 在跑。没跑就用包内 bsk 拉起来（后台，不弹窗）。"""
    if daemon_status():
        return True, "daemon 已在运行"
    bsk = bsk_binary()
    if not bsk:
        return False, "找不到 bsk（安装包应自带 bin/bsk.exe）"
    try:
        subprocess.Popen(
            [bsk, "daemon", "start"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
            creationflags=_CREATE_NO_WINDOW | _CREATE_NEW_PROCESS_GROUP,
            close_fds=True,
        )
    except Exception as e:  # noqa: BLE001
        return False, f"启动 daemon 失败：{type(e).__name__}: {e}"
    t0 = time.time()
    while time.time() - t0 < timeout:
        time.sleep(0.6)
        if daemon_status(force=True):
            return True, f"daemon 已启动（{time.time() - t0:.1f}s）"
    return False, f"daemon 启动超时（{timeout:.0f}s）"


# ---------- 浏览器 ----------

def _browser_ids(force: bool = False) -> list[str]:
    st = daemon_status(force=force) or {}
    return [str(b.get("instance_id") or "") for b in (st.get("browsers") or [])]


def ensure_browser(*, headless: bool = False, timeout: float = 25.0) -> dict:
    """确保 daemon 至少有一个可用浏览器；没有就用内置 Chromium + 内置扩展起一个。

    headless=False 时是有窗口的 —— 这是**验证场景专用**，因为要人来看。
    正常抓取不会走到这里。

    返回 {"ok", "instance_id", "started", "error"}。
    """
    global _browser_proc, _browser_instance, _browser_headless

    ok, why = ensure_daemon()
    if not ok:
        return {"ok": False, "instance_id": "", "started": False, "error": why}

    existing = _browser_ids(force=True)
    if existing:
        # 复用前先比对模式：如果现有实例是**我们起的**、但模式和这次请求不一致，
        # 就把它关掉重起。否则只要有一次以可见模式启动过，后面每次
        # headless=True 的抓取都会沿用那个窗口 —— 用户看到的就是「一直弹窗」
        # （2026-10-25 实测：用户要求全部内置后台运行）。
        if (_browser_proc is not None and _browser_proc.poll() is None
                and _browser_headless != bool(headless)):
            try:
                _browser_proc.terminate()
                _browser_proc.wait(timeout=6)
            except Exception:  # noqa: BLE001
                pass
            _browser_proc = None
            time.sleep(1.2)
            existing = _browser_ids(force=True)
        if existing:
            # 用户自己开着 Chrome 且装了扩展时直接用他的实例，省一次启动
            return {"ok": True, "instance_id": existing[0], "started": False, "error": ""}

    exe = chromium_exe()
    if not exe:
        return {"ok": False, "instance_id": "", "started": False,
                "error": "没有可用的浏览器内核（连内置 Chromium 都没找到）"}
    ext = extension_dir()
    if not (ext / "manifest.json").is_file():
        return {"ok": False, "instance_id": "", "started": False,
                "error": f"内置扩展缺失：{ext}"}

    profile = DATA_ROOT / "bsk_profile"
    profile.mkdir(parents=True, exist_ok=True)
    args = [
        exe,
        f"--user-data-dir={profile}",
        f"--disable-extensions-except={ext}",
        f"--load-extension={ext}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-blink-features=AutomationControlled",
    ]
    if headless:
        # 只有 Chrome 112+ 的 new headless 才加载扩展；老 headless 会静默不加载
        args.append("--headless=new")
    with _browser_lock:
        if _browser_proc is None or _browser_proc.poll() is not None:
            try:
                _browser_proc = subprocess.Popen(
                    args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    stdin=subprocess.DEVNULL, creationflags=_CREATE_NO_WINDOW,
                    close_fds=True)
                # 记下这次是以什么模式起的，供下次复用前比对
                _browser_headless = bool(headless)
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "instance_id": "", "started": False,
                        "error": f"启动内置浏览器失败：{type(e).__name__}: {e}"}

    # 等扩展连上 daemon（实测 2~6 秒）
    t0 = time.time()
    while time.time() - t0 < timeout:
        time.sleep(0.6)
        now = _browser_ids(force=True)
        fresh = [i for i in now if i not in existing]
        if fresh or now:
            _browser_instance = (fresh or now)[0]
            return {"ok": True, "instance_id": _browser_instance, "started": True,
                    "error": "", "waited": round(time.time() - t0, 1)}
    return {"ok": False, "instance_id": "", "started": True,
            "error": f"浏览器已启动但扩展 {timeout:.0f}s 内没连上 daemon"}


def stop_browser() -> None:
    """关掉**我们自己启动的**那个浏览器进程。

    只杀 Popen 的 pid —— 用户自己的 Chrome 一根汗毛都不能碰。
    """
    global _browser_proc, _browser_instance, _browser_headless
    with _browser_lock:
        proc = _browser_proc
        _browser_proc = None
        _browser_instance = ""
        _browser_headless = None
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=6)
    except Exception:  # noqa: BLE001
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


def ensure_ready(*, headless: bool = False, timeout: float = 40.0) -> dict:
    """一步到位：daemon + 浏览器都就绪。调用方拿到 instance_id 后直接 start_page。"""
    t0 = time.time()
    res = ensure_browser(headless=headless, timeout=timeout)
    res["elapsed"] = round(time.time() - t0, 1)
    return res


# ---------- 会话 ----------

def start_page(url: str, *, name: str = "MediaDownloader 验证辅助",
               browser_id: str = "") -> dict:
    args = ["session", "start", "--name", name]
    target = browser_id or _browser_instance
    if target:
        args += ["--browser", target]
    sess = _json(args, timeout=30)
    sid = str(sess.get("session_id") or "")
    if not sid:
        raise RuntimeError("BrowserSkill 未返回 session_id")
    try:
        nav = _json(
            ["navigate", url, "--session", sid, "--wait-until", "domcontentloaded",
             "--timeout", "45s"],
            timeout=55,
        )
    except Exception:
        nav = {}
    return {
        "session_id": sid,
        "tab_id": nav.get("tab_id"),
        "url": str(nav.get("final_url") or nav.get("url") or url),
    }


def observe(session_id: str) -> str:
    data = _json(["observe", "--session", session_id, "--max-tokens", "5000"], timeout=30)
    return str(data.get("text") or "")


def evaluate(session_id: str, expression: str, *, timeout: int = 30):
    data = _json(
        ["evaluate", "--session", session_id, expression, "--timeout", f"{timeout}s"],
        timeout=timeout + 5,
    )
    if data.get("ok") is False:
        err = data.get("error") or {}
        raise RuntimeError(str(err.get("text") or err or "BrowserSkill evaluate failed"))
    return data.get("value")


def click_simple_verification(session_id: str) -> str:
    """点一个明显的「验证/继续」按钮并返回它的文案。

    只点「唯一一个」候选 —— 有几个长得像的按钮时宁可不点。
    乱点会把用户带到登录页或广告页，比不点更糟。
    """
    text = observe(session_id)
    candidates: list[tuple[str, str]] = []
    for ref, label in _REF_LINE.findall(text):
        norm = re.sub(r"\s+", " ", label).strip().lower()
        if norm in _SIMPLE_VERIFY_LABELS:
            candidates.append((ref, label.strip()))
    if len(candidates) != 1:
        return ""
    ref, label = candidates[0]
    _json(["click", ref, "--session", session_id, "--timeout", "20s"], timeout=25)
    time.sleep(1.5)
    return label


def get_html(session_id: str, *, max_bytes: int = 8 * 1024 * 1024) -> str:
    fd, raw_path = tempfile.mkstemp(prefix="media_bsk_", suffix=".html")
    os.close(fd)
    path = Path(raw_path)
    try:
        _run(
            ["get-html", "--session", session_id, "--max-bytes", str(max_bytes),
             "--out", str(path)],
            timeout=45,
        )
        return path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
    finally:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def request_human_help(session_id: str, prompt: str, *, timeout_seconds: int = 180) -> bool:
    """请用户在 Agent Window 里完成真正的验证码 / 登录。"""
    timeout_seconds = max(30, min(int(timeout_seconds or 180), 300))
    cp = _run(
        [
            "request-help",
            "--session", session_id,
            "--title", "Media Downloader 需要完成网页验证",
            "--prompt", prompt,
            "--timeout", f"{timeout_seconds}s",
        ],
        timeout=timeout_seconds + 15,
        check=False,
    )
    return cp.returncode == 0


def stop(session_id: str) -> None:
    if not session_id or not bsk_binary():
        return
    try:
        _run(["session", "stop", session_id], timeout=20, check=False)
    except Exception:  # noqa: BLE001
        pass
