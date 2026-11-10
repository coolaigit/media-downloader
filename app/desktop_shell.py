# -*- coding: utf-8 -*-
"""Windows desktop shell for MediaDownloader.

The application keeps the proven FastAPI/web frontend, but hosts it inside a
native window with pywebview/WebView2. Browser mode remains as a fallback.

⚠️ 本模块的核心职责不是「开个窗口」，而是**保证窗口里跑的是 Chromium**。
2026-10-29 另一台 Win10 实机故障：WebView2 注册表键被清理工具破坏 →
pywebview 判定「没有 WebView2」→ **不抛异常**、静默返回一个 MSHTML(IE11)
窗口 → web/app.js 的箭头函数/async 在 IE11 里解析阶段整体失败 →
白窗口 + 所有按钮失效，且一个字报错都不留。

2026-11-03 补充实测（本机强制 `gui='mshtml'` 复现）：
    原来那版自检 `evaluate_js('1+1') == 2` **在 IE11 下同样返回 2**，
    因为 1+1 只用到 eval 和 JSON.stringify，IE11 两者都有 ——
    也就是说旧自检恒为「通过」，降级到 IE 也照旧白屏，等于没修。
    实测 IE11 下可靠成立的指纹是：
        `(()=>1)()`              → SyntaxError（无箭头函数）
        `navigator.userAgent`    → 含 Trident/7.0; rv:11.0
    所以现在的判据 = UA 指纹 + ES6 语法 + 前端就绪标记（三重）。
"""
from __future__ import annotations

import os
import re
import sys
import threading
import time
import traceback
import webbrowser
from pathlib import Path

from .config import BUNDLE_DIR, DATA_ROOT, RESOURCE_ROOT
from .ports import probe_running, resolve_port

TITLE = "MediaDownloader 3.0"   # 与 V2.5 的 "MediaDownloader" 区分，避免用户分不清打开的是哪个
WIDTH = 1280
HEIGHT = 860
MIN_SIZE = (980, 680)

# 单次 evaluate_js 的上限。pywebview 的 edgechromium.evaluate_js 内部是
# `Semaphore.acquire()`，**没有超时** —— 内核半死（窗口起来了但 WebView2
# 没初始化完）时会永久阻塞，自检线程被卡死，浏览器兜底永远不会触发。
# 所以每一次探测都必须由我们自己加超时。
PROBE_JS_TIMEOUT = 5.0
# 等前端把 window.__MD_APP_READY__ 置位的总时长（冷启动 /api/status 可能较慢）。
PROBE_READY_DEADLINE = 20.0
# 等「内核指纹」出来（拿 UA / 试 ES6）的总时长。
PROBE_SIGNATURE_DEADLINE = 10.0

WEBVIEW2_DOWNLOAD_URL = "https://go.microsoft.com/fwlink/p/?LinkId=2124703"
WEBVIEW2_MIN_BUILD = "86.0.622.0"

# pywebview 只查这 4 个注册表键（winforms._is_chromium）。
_WEBVIEW2_CLIENT_KEYS = (
    "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}",  # WebView2 Runtime（正式版）
    "{2CD8A007-E189-409D-A2C8-9AF4EF3C72AA}",  # Beta
    "{0D50BFEC-CD6A-4F9A-964C-C7416E3ACB10}",  # Dev
    "{65C35B14-6C1D-4122-AC46-7148CC9D6497}",  # Canary
)

# IE / 老 Edge(EdgeHTML) 的 UA 指纹。命中即判定「不是 Chromium」。
_LEGACY_UA_RE = re.compile(r"Trident/|MSHTML|EdgeHTML|Edge/1[0-8]\.", re.I)


def _log_path() -> Path:
    p = DATA_ROOT / "logs"
    p.mkdir(parents=True, exist_ok=True)
    return p / "desktop_shell.log"


def _log(message: str) -> None:
    try:
        with _log_path().open("a", encoding="utf-8", errors="replace") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}\n")
    except OSError:
        pass


# --------------------------------------------------------------------------
# WebView2 运行时探测（文件系统 + 注册表）
# --------------------------------------------------------------------------
def _bundled_webview2_root() -> Path:
    """安装包随带的 Fixed Version Runtime 目录（{app}\\webview2）。

    Inno 打包时把一份完整的 WebView2 运行时（自包含，含 msedgewebview2.exe）
    解压到安装目录的 webview2\\ 子目录，版本号目录形如 `157.0.4341.46`。
    Fixed Version Runtime 本质就是一份可直接搬迁的目录拷贝
    （微软官方文档的定义），不需要注册表、不需要 EdgeUpdate 服务。
    源码模式 / 精简包里没有这个目录 → 返回的 Path 不存在，
    `_webview2_runtime_dir()` 自然跳过它走系统探测，行为不变。
    """
    return BUNDLE_DIR / "webview2"


def _webview2_runtime_dir() -> str:
    """按**文件系统**找 WebView2 运行时目录，找不到返回空串。

    为什么必须自己找（2026-10-29 另一台 Win10 实机）：

    pywebview 判断「这台机器有没有 WebView2」只查 4 个注册表键
    （`EdgeUpdate\\Clients\\{F3017226-FE2A-...}` 等）。那台机器上运行时文件
    齐全（`C:\\Program Files (x86)\\Microsoft\\EdgeWebView\\Application\\153.x\\`，
    383MB，msedgewebview2.exe / msedge.dll / 168 个 Locales 都在），「应用和功能」
    里的卸载项也在，**唯独那几个注册表键一个都没有**（疑似被清理/搬迁类工具破坏）。

    结果：pywebview 判定「没有 WebView2」→ 静默降级到 MSHTML(IE11) → 前端
    用了箭头函数/async，IE11 在语法解析阶段就崩 → 白窗口 + 所有按钮失效，
    而且**一个字的报错都不留**。

    这里兜一层：文件系统里能找到 msedgewebview2.exe 就用它，通过
    `webview.settings['WEBVIEW2_RUNTIME_PATH']` 交给 WebView2 当
    BrowserExecutableFolder —— 绕开坏掉的注册表。
    """
    # V3.0.5：安装包随带 Fixed Version Runtime（{app}\webview2\<ver>\）。
    # 放在系统路径**之前**：随包版本与发布时测试过的版本一致，
    # 不受目标机器 Evergreen 自动升级/清理工具破坏注册表的影响。
    candidates: list[Path] = []
    bundled_root = _bundled_webview2_root()
    if bundled_root.is_dir():
        candidates.append(bundled_root)

    roots: list[str] = []
    for env in ("ProgramFiles(x86)", "ProgramFiles"):
        root = os.environ.get(env)
        if root:
            roots.append(root)
    # 环境变量在部分受限进程里可能取不到（实测：从脚本/服务方式启动时
    # ProgramFiles(x86) 会是 None），补上 Windows 的标准安装路径兜底。
    roots += [r"C:\Program Files (x86)", r"C:\Program Files"]
    for root in dict.fromkeys(roots):     # 去重且保持顺序
        candidates.append(Path(root) / "Microsoft" / "EdgeWebView" / "Application")

    local = os.environ.get("LOCALAPPDATA")
    if not local:
        local = str(Path.home() / "AppData" / "Local")
    candidates.append(Path(local) / "Microsoft" / "EdgeWebView" / "Application")

    for base in candidates:
        try:
            if not base.is_dir():
                continue
            versions = sorted(base.iterdir(), reverse=True)
        except OSError:
            continue
        for ver in versions:
            try:
                if (ver / "msedgewebview2.exe").is_file():
                    return str(ver)
            except OSError:
                continue
    return ""


def _version_tuple(text: str) -> tuple[int, ...]:
    out: list[int] = []
    for part in re.split(r"[._]", str(text or "")):
        if not part.isdigit():
            break
        out.append(int(part))
    return tuple(out)


def _webview2_registry_ok() -> bool:
    """镜像 pywebview 的 `_is_chromium()`（注册表那一半），用于快速判死。"""
    try:
        import winreg
    except Exception:  # noqa: BLE001 —— 非 Windows
        return False

    paths: list[str] = []
    for guid in _WEBVIEW2_CLIENT_KEYS:
        paths.append(rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{guid}")
        paths.append(rf"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{guid}")

    min_ver = _version_tuple(WEBVIEW2_MIN_BUILD)
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for path in paths:
            try:
                with winreg.OpenKey(hive, path) as key:
                    pv, _ = winreg.QueryValueEx(key, "pv")
            except OSError:
                continue
            except Exception:  # noqa: BLE001
                continue
            if _version_tuple(str(pv)) >= min_ver:
                return True
    return False


def _webview2_available() -> tuple[bool, str]:
    """快速判死：这台机器到底能不能跑 EdgeChromium。

    判据 = 「文件系统找到运行时」**或**「注册表认出运行时」。
    前者比 pywebview 更宽松（pywebview 只看注册表），所以这里判 False 时
    pywebview 必然也判 False、必然掉进 MSHTML —— 结论可靠，不会误杀。
    """
    rt = _webview2_runtime_dir()
    if rt:
        return True, rt
    if _webview2_registry_ok():
        return True, ""
    return False, ""


def _pin_webview2_loader() -> None:
    """把**当前架构**的 WebView2Loader.dll 目录前置到 PATH。

    pywebview（`edgechromium.py` 第 50~51 行）按 arm64 → x64 → x86 的顺序
    把三个目录 append 到 PATH，arm64 在最前。x64 进程理论上会先命中
    arm64 的 loader（正常应报 BadImageFormatException）。这里把正确的
    那个目录**前置**，规避这个隐患。
    """
    try:
        import platform
        from webview.util import interop_dll_path

        machine = (platform.machine() or "").upper()
        arch = {"AMD64": "win-x64", "X86_64": "win-x64",
                "ARM64": "win-arm64", "X86": "win-x86"}.get(machine)
        if not arch:
            return
        d = str(interop_dll_path(arch))
        if not os.path.isdir(d):
            return
        old = os.environ.get("PATH", "")
        parts = old.split(os.pathsep) if old else []
        parts = [p for p in parts if p and os.path.normcase(p) != os.path.normcase(d)]
        os.environ["PATH"] = os.pathsep.join([d] + parts)
        _log(f"WebView2Loader 目录已前置到 PATH（{arch}）: {d}")
    except Exception:  # noqa: BLE001 —— 纯优化项，失败不影响主流程
        _log("pin WebView2Loader path failed:\n" + traceback.format_exc())


def _attach_webview_logging() -> None:
    """把 pywebview 自己的日志接到文件里。

    原来这些信息只走 stdout：双击 exe 启动时 stdout 无处可看，
    `service.out.log` 里一个字都没有 —— 用户侧就是「纯静默白屏，无从排查」。
    WebView2 初始化失败的原始异常（`Couldn't find a compatible Webview2
    Runtime installation` 等）现在会落在 desktop_webview.log 里。
    """
    try:
        import logging

        logger = logging.getLogger("pywebview")
        logger.setLevel(logging.DEBUG)
        logs = DATA_ROOT / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(logs / "desktop_webview.log", encoding="utf-8")
        handler.setFormatter(logging.Formatter(
            "[%(asctime)s] [%(name)s] %(levelname)s %(message)s"))
        logger.addHandler(handler)
    except Exception:
        _log("attach pywebview logging failed:\n" + traceback.format_exc())


# --------------------------------------------------------------------------
# 渲染内核自检
# --------------------------------------------------------------------------
def _eval_js(window, script: str, timeout: float = PROBE_JS_TIMEOUT):
    """带超时的 evaluate_js。返回 (ok, value, error_text)。

    必须自己加超时：pywebview 的 `Semaphore.acquire()` 没有 timeout，
    内核半死时会永久阻塞，自检线程跟着卡死，兜底就永远不会触发。
    """
    box: dict = {}

    def _run() -> None:
        try:
            box["value"] = window.evaluate_js(script)
        except Exception as exc:  # noqa: BLE001
            box["error"] = exc

    t = threading.Thread(target=_run, name="EvalJS", daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        return False, None, f"evaluate_js 超时 {timeout:.0f}s（渲染进程无响应）"
    if "error" in box:
        exc = box["error"]
        return False, None, f"{type(exc).__name__}: {exc}"
    return True, box.get("value"), ""


def _renderer_signature(window) -> tuple[str, str]:
    """判断窗口里的渲染内核到底是不是 Chromium。

    返回 (verdict, detail)，verdict ∈ {"ok", "legacy", "dead", "unknown"}：
        ok      —— Chromium（可以继续用）
        legacy  —— MSHTML/IE11 或 EdgeHTML（必须降级到浏览器）
        dead    —— 引擎连话都不回（渲染进程挂了）
        unknown —— 页面还没加载出来，稍后再试

    为什么不能用 `evaluate_js('1+1') == 2`（2026-11-03 本机强制 mshtml 实测）：
        IE11 下 `1+1` 正常返回 2。`1+1` 只用 eval + JSON.stringify，
        两者 IE11 都有 —— 旧判据恒真，等于没检查。
    实测唯一稳定成立的差异：
        `navigator.userAgent` → 'Trident/7.0; rv:11.0'
        `(()=>1)()`           → SyntaxError（IE11 没有箭头函数）
    """
    ok, ua, err = _eval_js(window, "navigator.userAgent")
    if not ok:
        # 页面还没加载完时 evaluate_js 也可能失败，先不判死，交给外层重试。
        return "unknown", err
    ua = str(ua or "")
    if not ua:
        return "unknown", "UA 为空"
    if _LEGACY_UA_RE.search(ua):
        return "legacy", ua

    ok_es6, _, err_es6 = _eval_js(window, "(()=>1)()")
    if not ok_es6:
        return "legacy", f"不支持 ES6 语法（{err_es6}）；UA={ua}"
    return "ok", ua


def _wait_frontend_ready(window, deadline: float) -> tuple[bool, str]:
    """等前端脚本把 `window.__MD_APP_READY__` 置位。

    这是**最终的**验收判据：内核是 Chromium 但前端脚本自身报错（语法/运行时）
    时，用户看到的同样是「界面全哑」，必须一视同仁地降级到浏览器。
    标记在 web/app.js 的 init 里、所有 await 之前置位（同步绑定完成即可交互）。
    """
    end = time.monotonic() + deadline
    last = ""
    while time.monotonic() < end:
        ok, value, err = _eval_js(window, "window.__MD_APP_READY__ === true",
                                  timeout=min(PROBE_JS_TIMEOUT, 3.0))
        if ok and value is True:
            return True, ""
        last = err or f"标记未置位（value={value!r}）"
        time.sleep(0.6)
    return False, last


def _probe_renderer(window, state: dict) -> None:
    """窗口起来后验内核 + 前端；不可用就关窗，让 main() 转浏览器。

    为什么必须自检：pywebview 在内核判定失败时**不抛异常**，
    而是成功返回一个 MSHTML(IE11) 窗口 —— 应用以为桌面壳正常，
    《使用说明》承诺的「初始化失败回退浏览器」永远不会触发。
    """
    # 1) 内核指纹
    sig_end = time.monotonic() + PROBE_SIGNATURE_DEADLINE
    verdict, detail = "unknown", ""
    while time.monotonic() < sig_end:
        verdict, detail = _renderer_signature(window)
        if verdict != "unknown":
            break
        time.sleep(0.7)

    if verdict == "legacy":
        _fail_renderer(window, state, f"内核不是 Chromium，已降级为 IE/MSHTML（UA={detail}）")
        return
    if verdict == "dead":
        _fail_renderer(window, state, f"渲染进程无响应（{detail}）")
        return
    if verdict == "unknown":
        _fail_renderer(window, state, f"渲染进程起不来（{detail}）")
        return

    _log(f"内核自检通过（Chromium）: {detail}")

    # 2) 前端到底跑起来没有
    ok, why = _wait_frontend_ready(window, PROBE_READY_DEADLINE)
    if not ok:
        _fail_renderer(window, state, f"前端脚本未完成初始化（{why}）")
        return

    _log("前端自检通过（__MD_APP_READY__ 已置位）")


def _fail_renderer(window, state: dict, reason: str) -> None:
    _log(f"渲染自检失败：{reason} → 关闭窗口，改用默认浏览器")
    state["renderer_failed"] = True
    state["renderer_reason"] = reason
    try:
        window.destroy()
    except Exception:
        _log("destroy window failed:\n" + traceback.format_exc())


# --------------------------------------------------------------------------
# 浏览器兜底
# --------------------------------------------------------------------------
def _browser_launcher_doc() -> str:
    return (
        "@echo off\r\n"
        "rem MediaDownloader - open the UI in your default browser.\r\n"
        "rem Use this when the desktop window cannot start (e.g. WebView2 runtime\r\n"
        "rem is missing or broken). No WebView2 is required in this mode.\r\n"
        "cd /d \"%~dp0\"\r\n"
        "start \"\" \"%~dp0MediaDownloader.exe\" --browser\r\n"
    )


def _ensure_browser_launcher() -> None:
    """在程序目录放一个「以浏览器模式启动.cmd」。

    为什么要有：WebView2 坏掉的机器上，桌面窗口救不回来，用户必须有个
    **不需要记命令、双击就行**的入口（报告 §8 里那条路当时是人工手搓的）。
    只在 frozen（安装版）下生成；源码运行没必要。写不进去就静默放弃。
    """
    if not getattr(sys, "frozen", False):
        return
    try:
        base = Path(sys.executable).resolve().parent
        target = base / "以浏览器模式启动.cmd"
        if target.is_file():
            return
        target.write_text(_browser_launcher_doc(), encoding="utf-8", newline="")
        _log(f"已生成浏览器模式启动器: {target}")
    except Exception:  # noqa: BLE001 —— 纯便利项
        _log("create browser launcher failed:\n" + traceback.format_exc())


def _fallback_message(url: str, reason: str) -> str:
    return (
        "桌面窗口没能启用 Chromium 内核，已改用系统默认浏览器打开界面。\n\n"
        f"原因：{reason}\n\n"
        f"如果浏览器没有自动打开，请手动访问：\n{url}\n\n"
        "想让桌面窗口恢复正常：安装 Microsoft Edge WebView2 运行时后重新打开本程序。\n"
        f"{WEBVIEW2_DOWNLOAD_URL}\n\n"
        "也可以随时双击程序目录里的「以浏览器模式启动.cmd」直接走浏览器。"
    )


def _fallback_to_browser(url: str, reason: str) -> bool:
    """转浏览器模式。先开浏览器再弹提示，保证提示框卡住也不影响使用。"""
    _log(f"转浏览器模式：{reason}")
    opened = False
    try:
        webbrowser.open(url)
        opened = True
        _log("默认浏览器已打开")
    except Exception:
        _log("open default browser failed:\n" + traceback.format_exc())
    if os.name == "nt":
        _show_message(_fallback_message(url, reason), icon=0x40)
    if not opened:
        _show_error("桌面窗口与默认浏览器都无法打开。\n\n请手动访问：\n" + url)
    return opened


def _start_server(port: int, holder: dict) -> threading.Thread:
    def _worker() -> None:
        try:
            import uvicorn
            from .main import app

            config = uvicorn.Config(
                app,
                host="127.0.0.1",
                port=port,
                log_level="warning",
                access_log=False,
                # PyInstaller windowed EXE 中 sys.stdout/sys.stderr 为 None；
                # uvicorn 默认 formatter 会调用 isatty()，导致桌面版 Frozen-only 崩溃。
                # 禁用默认 logging dictConfig，由我们自己的文件日志承担故障记录。
                log_config=None,
                use_colors=False,
            )
            server = uvicorn.Server(config)
            holder["server"] = server
            server.run()
        except Exception:
            _log("server thread failed:\n" + traceback.format_exc())

    t = threading.Thread(target=_worker, name="MediaDownloaderServer", daemon=True)
    t.start()
    return t


def _wait_ready(port: int, timeout: float = 60.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if probe_running(port, timeout=0.8):
            return True
        time.sleep(0.35)
    return False


def _show_message(text: str, icon: int = 0x10) -> None:
    _log(text)
    if os.name != "nt":
        return
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, text, TITLE, icon)
    except Exception:
        pass


def _show_error(text: str) -> None:
    _show_message(text, icon=0x10)


def _close_behavior() -> str:
    try:
        from .store import load
        value = str(load().get("close_behavior") or "exit").strip().lower()
        return value if value in {"exit", "tray"} else "exit"
    except Exception:
        return "exit"


def _tray_image():
    from PIL import Image, ImageDraw

    for candidate in (BUNDLE_DIR / "app_icon.ico", RESOURCE_ROOT / "app_icon.ico"):
        if candidate.is_file():
            try:
                return Image.open(candidate).convert("RGBA")
            except Exception:
                pass

    # 资源异常时仍给托盘一个可见图标，不让“缩小到托盘”变成无入口。
    image = Image.new("RGBA", (64, 64), (79, 70, 229, 255))
    draw = ImageDraw.Draw(image)
    draw.rectangle((28, 10, 36, 39), fill=(255, 255, 255, 255))
    draw.polygon(((18, 32), (46, 32), (32, 49)), fill=(255, 255, 255, 255))
    return image


def _ensure_tray(window, state: dict):
    icon = state.get("tray_icon")
    if icon is not None:
        return icon

    import pystray

    def _show(_icon=None, _item=None):
        try:
            window.show()
            state["hidden_to_tray"] = False
        except Exception:
            _log("tray show failed:\n" + traceback.format_exc())

    def _exit(icon_obj=None, _item=None):
        state["force_exit"] = True
        try:
            if icon_obj is not None:
                icon_obj.visible = False
                icon_obj.stop()
        except Exception:
            pass
        try:
            window.destroy()
        except Exception:
            _log("tray exit failed:\n" + traceback.format_exc())

    menu = pystray.Menu(
        pystray.MenuItem("打开 MediaDownloader", _show, default=True),
        pystray.MenuItem("退出", _exit),
    )
    icon = pystray.Icon("MediaDownloader", _tray_image(), TITLE, menu)
    icon.run_detached()
    state["tray_icon"] = icon
    return icon


def _handle_window_close(window, state: dict) -> bool | None:
    """Return False to cancel close and hide to tray; None to allow real exit."""
    if state.get("force_exit"):
        return None
    if _close_behavior() != "tray":
        return None
    try:
        _ensure_tray(window, state)
        window.hide()
        state["hidden_to_tray"] = True
        return False
    except Exception:
        # 用户明确选择“关闭到托盘”时，托盘组件异常也不能擅自改成“直接退出”。
        # 记录错误并取消本次关闭；窗口继续保留，避免任务/服务被意外终止。
        _log("minimize-to-tray failed; close cancelled:\n" + traceback.format_exc())
        try:
            window.show()
        except Exception:
            pass
        return False


def _open_native(url: str, own_server: bool, holder: dict) -> int:
    try:
        import webview
    except Exception:
        _log("pywebview import failed:\n" + traceback.format_exc())
        holder["fallback_reason"] = "pywebview 加载失败（缺少组件）"
        return -1

    _attach_webview_logging()

    # 0) 快速判死：WebView2 根本没装 / 注册表与文件系统双双认不出来
    #    → 不创建窗口，直接走浏览器。省掉几秒白窗，并把原因说清楚。
    available, rt = _webview2_available()
    if not available:
        holder["fallback_reason"] = ("本机未检测到可用的 Microsoft Edge WebView2 运行时"
                                     "（注册表与文件系统均未找到）")
        _log("WebView2 不可用（快速判死），跳过桌面窗口")
        return -1

    # 文件系统能找到运行时，就交给 pywebview 当 BrowserExecutableFolder ——
    # 这一步同时让 pywebview 的 `_is_chromium()` 直接返回 True，
    # 绕开被清理坏的注册表。
    if rt:
        try:
            webview.settings["WEBVIEW2_RUNTIME_PATH"] = rt
            _log(f"WebView2 运行时（文件系统探测）: {rt}")
        except Exception:
            _log("set WEBVIEW2_RUNTIME_PATH failed:\n" + traceback.format_exc())
    else:
        _log("文件系统里没找到运行时目录，走注册表判定")

    _pin_webview2_loader()

    try:
        window = webview.create_window(
            TITLE,
            url=url,
            width=WIDTH,
            height=HEIGHT,
            min_size=MIN_SIZE,
            resizable=True,
            confirm_close=False,
            background_color="#111827",
            text_select=True,
            zoomable=True,
        )

        state = {"force_exit": False, "hidden_to_tray": False, "tray_icon": None,
                 "renderer_failed": False, "renderer_reason": ""}

        def _closing() -> bool | None:
            # pywebview 的 cancellable closing 事件：返回 False = 取消真正关闭。
            return _handle_window_close(window, state)

        def _closed() -> None:
            # 内核自检失败时是我们自己关的窗，后面还要用浏览器接管，
            # 服务不能跟着停，否则浏览器打开的是个死页面。
            if state.get("renderer_failed"):
                return
            icon = state.get("tray_icon")
            if icon is not None:
                try:
                    icon.visible = False
                    icon.stop()
                except Exception:
                    pass
            if own_server:
                server = holder.get("server")
                if server is not None:
                    server.should_exit = True

        if window is not None:
            window.events.closing += _closing
            window.events.closed += _closed

        threading.Thread(target=_probe_renderer, args=(window, state),
                         name="RendererProbe", daemon=True).start()

        storage = DATA_ROOT / "webview"
        storage.mkdir(parents=True, exist_ok=True)
        webview.start(
            # 绝不能传 'mshtml'：那是这台机器静默降级的元凶。
            # （winforms.py 第 131 行 `forced_gui_ != 'mshtml'` 就是给强制降级留的口子）
            gui="edgechromium",
            debug=False,
            private_mode=False,
            storage_path=str(storage),
        )
        if state.get("renderer_failed"):
            holder["fallback_reason"] = state.get("renderer_reason") or "渲染内核自检未通过"
            # 让 main 走浏览器兜底（那边会打开默认浏览器并保持服务运行）
            return -1
        return 0
    except Exception:
        _log("native window failed:\n" + traceback.format_exc())
        holder["fallback_reason"] = "桌面窗口初始化异常（详见 desktop_shell.log）"
        return -1


def main(port: int | None = None) -> int:
    mode, resolved = resolve_port(port)
    if mode == "busy-foreign":
        _show_error(f"端口 {resolved} 已被其他程序占用。")
        return 1
    if mode == "exhausted" or resolved is None:
        _show_error("8848-8870 端口全部被占用，请关闭占用端口的程序后重试。")
        return 1

    url = f"http://127.0.0.1:{resolved}"
    holder: dict = {}
    own_server = mode == "serve"
    server_thread: threading.Thread | None = None

    _ensure_browser_launcher()

    if own_server:
        server_thread = _start_server(resolved, holder)
        if not _wait_ready(resolved):
            _show_error("本地服务启动失败。请查看日志："
                        + str(_log_path()))
            server = holder.get("server")
            if server is not None:
                server.should_exit = True
            return 3

    rc = _open_native(url, own_server, holder)
    if rc == 0:
        if own_server and server_thread is not None:
            server_thread.join(timeout=5)
        return 0

    # 桌面壳不可用时的保底：继续使用原来的浏览器界面，并**明确告诉用户为什么**。
    # （旧版这里只写一行日志、静默开浏览器，用户以为自己点错了程序）
    reason = str(holder.get("fallback_reason") or "桌面窗口不可用")
    opened = _fallback_to_browser(url, reason)

    if own_server and server_thread is not None:
        server_thread.join()
    return 0 if opened else 4
