# -*- coding: utf-8 -*-
"""Windows desktop shell for MediaDownloader.

The application keeps the proven FastAPI/web frontend, but hosts it inside a
native window with pywebview/WebView2. Browser mode remains as a fallback.
"""
from __future__ import annotations

import os
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
    candidates: list[Path] = []
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


def _attach_webview_logging() -> None:
    """把 pywebview 自己的日志接到文件里。

    原来这些信息只走 stdout：双击 exe 启动时 stdout 无处可看，
    `service.out.log` 里一个字都没有 —— 用户侧就是「纯静默白屏，无从排查」。
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


def _show_error(text: str) -> None:
    _log(text)
    if os.name != "nt":
        return
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, text, TITLE, 0x10)
    except Exception:
        pass


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


def _js_engine_ok(window) -> bool:
    """窗口里的 JS 引擎能不能跑。`1+1` 只依赖引擎本身，与页面加载进度无关。

    这是判断「内核是不是被降级成 IE/MSHTML」最干净的判据：
    IE11 下 evaluate_js 要么抛异常，要么返回 None。
    """
    try:
        return window.evaluate_js("1+1") == 2
    except Exception:
        return False


def _open_native(url: str, own_server: bool, holder: dict) -> int:
    try:
        import webview
    except Exception:
        _log("pywebview import failed:\n" + traceback.format_exc())
        return -1

    _attach_webview_logging()

    # 先把「文件系统里能找到的 WebView2 运行时」交给 pywebview：
    # 注册表被清理过、但文件还在的机器，靠这一步才走得通 EdgeChromium。
    rt = _webview2_runtime_dir()
    if rt:
        try:
            webview.settings["WEBVIEW2_RUNTIME_PATH"] = rt
            _log(f"WebView2 运行时（文件系统探测）: {rt}")
        except Exception:
            _log("set WEBVIEW2_RUNTIME_PATH failed:\n" + traceback.format_exc())
    else:
        _log("文件系统里没找到 WebView2 运行时目录（将依赖注册表判定）")

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
                 "renderer_failed": False}

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

        def _probe_renderer() -> None:
            """窗口起来后验一次 JS 引擎；不可用就关窗转浏览器。

            为什么必须自检：pywebview 在内核判定失败时**不抛异常**，
            而是成功返回一个 MSHTML(IE11) 窗口 —— 应用以为桌面壳正常，
            《使用说明》承诺的「初始化失败回退浏览器」永远不会触发。
            而 web/app.js 有 153 处箭头函数、86 处 fetch，IE11 在语法解析
            阶段就整体失败，表现就是白窗口 + 所有按钮没反应（2026-10-29
            另一台 Win10 实机故障）。

            `1+1` 只依赖 JS 引擎本身，跟页面加载进度无关，是干净的判据。
            """
            ok = False
            for i in range(5):
                time.sleep(0.9 if i == 0 else 0.8)
                if _js_engine_ok(window):
                    ok = True
                    break
            if ok:
                _log("渲染内核自检通过（JS 可执行）")
                return
            _log("渲染内核自检失败：疑似降级到 MSHTML/IE 内核，关闭窗口改用默认浏览器")
            state["renderer_failed"] = True
            try:
                window.destroy()
            except Exception:
                _log("destroy window failed:\n" + traceback.format_exc())

        threading.Thread(target=_probe_renderer, name="RendererProbe",
                         daemon=True).start()

        storage = DATA_ROOT / "webview"
        storage.mkdir(parents=True, exist_ok=True)
        webview.start(
            gui="edgechromium",
            debug=False,
            private_mode=False,
            storage_path=str(storage),
        )
        if state.get("renderer_failed"):
            # 让 main 走浏览器兜底（那边会打开默认浏览器并保持服务运行）
            return -1
        return 0
    except Exception:
        _log("native window failed:\n" + traceback.format_exc())
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

    # WebView2/桌面壳异常时保底：继续使用原来的浏览器界面。
    try:
        webbrowser.open(url)
        _log("native shell unavailable; opened default browser fallback")
    except Exception:
        _show_error("桌面窗口与默认浏览器都无法打开。")
        return 4

    if own_server and server_thread is not None:
        server_thread.join()
    return 0
