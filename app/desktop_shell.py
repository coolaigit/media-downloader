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

from .config import DATA_ROOT
from .ports import probe_running, resolve_port

TITLE = "MediaDownloader"
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


def _open_native(url: str, own_server: bool, holder: dict) -> int:
    try:
        import webview
    except Exception:
        _log("pywebview import failed:\n" + traceback.format_exc())
        return -1

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

        def _closed() -> None:
            if own_server:
                server = holder.get("server")
                if server is not None:
                    server.should_exit = True

        if window is not None:
            window.events.closed += _closed

        storage = DATA_ROOT / "webview"
        storage.mkdir(parents=True, exist_ok=True)
        webview.start(
            gui="edgechromium",
            debug=False,
            private_mode=False,
            storage_path=str(storage),
        )
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
