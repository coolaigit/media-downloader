# -*- coding: utf-8 -*-
"""Desktop 服务主逻辑（带崩溃自证 + 统一端口决策 + 自动开页面）。

为什么存在（2026-09-30）：uvicorn 实例曾无痕消失（事件日志无崩溃记录、
stderr 无从查起）。本模块做三件事：
  1) faulthandler —— 原生崩溃（段错误等）时把线程栈写进 service_crash.log
  2) stdout/stderr 全量落盘到 service.out.log / service.err.log
  3) 未处理异常钩子也落盘

1A.1（2026-10-01）：所有运行日志统一写入 DATA_ROOT\\logs\\，
不再向程序资源目录（源码目录 / exe 目录 / _internal）写任何文件。
chdir 已移除：app 包导入不依赖当前工作目录；这里仍显式 insert
sys.path 一次，保证任意路径调起都成立。

1C（2026-10-01）：主体抽成 main()，供 launcher.py 复用。

1D（2026-10-01）：端口不再硬编码 —— 统一走 app.ports.resolve_port()
（CLI --port > 环境变量 MEDIA_DOWNLOADER_PORT > 自动扫描 8848-8870），
并补齐「已有实例识别」与「服务就绪后自动打开浏览器」：
EXE/入口自己完成 找端口 → 启服务 → 打开网页，bat/ps1 不再各自决定端口。
面向用户的提示在 stdout 重定向**之前**发往真实控制台（否则 bat 窗口什么都看不到）。

注意：windowed frozen 下 sys.stdout 可能为 None / 坏句柄——本模块
在导入任何会碰 stdout 的模块之前就把 stdout/stderr 重定向到日志文件。

用法：python serve.py [--port N] [--no-browser]   （launcher.py / bat / ps1 调起）
"""
import faulthandler
import os
import sys
import time
import threading

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _usable(stream) -> bool:
    """判据沿用 run.py 的教训（2026-09-30）：不能只判 None ——
    还可能是「句柄已失效」的流，isatty()/fileno() 会抛异常。"""
    if stream is None:
        return False
    try:
        stream.fileno()
        stream.isatty()
        return True
    except Exception:  # noqa: BLE001
        return False


def _open_when_ready(port: int, immediate: bool = False) -> None:
    """服务就绪后用系统默认浏览器打开页面（守护线程轮询，失败静默）。

    immediate=True 用于「已在运行」场景：服务本来就活着，直接开。
    """
    import webbrowser

    def _worker():
        url = f"http://127.0.0.1:{port}"
        if not immediate:
            from app.ports import probe_running
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                if probe_running(port, timeout=0.8):
                    break
                time.sleep(0.5)
            else:
                return  # 60 秒还没就绪，不硬开
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001 —— 无浏览器环境不该拖垮服务
            pass

    threading.Thread(target=_worker, daemon=True).start()


def main(port: int | None = None, open_browser: bool | None = None) -> int:
    from app.config import DATA_ROOT
    from app.ports import ENV_NO_BROWSER, resolve_port

    console = sys.stdout if _usable(sys.stdout) else None

    def say(msg: str) -> None:
        # 用户可见的提示发真实控制台；重定向后同时落日志（windowed 下 console 为
        # None，print 走已重定向的 sys.stdout —— 同样落日志，不丢）
        if console is not None:
            print(msg, file=console, flush=True)
        print(msg, flush=True)

    LOG_DIR = DATA_ROOT / "logs"
    LOG_DIR.mkdir(parents=True, exist_ok=True)  # DATA_ROOT 可写，安全

    crash_log = open(LOG_DIR / "service_crash.log", "w", buffering=1)
    faulthandler.enable(crash_log)
    sys.stdout = open(LOG_DIR / "service.out.log", "a", buffering=1)
    sys.stderr = open(LOG_DIR / "service.err.log", "a", buffering=1)

    def _hook(exc_type, exc, tb):
        import traceback
        print(f"\n[{time.strftime('%m-%d %H:%M:%S')}] 未处理异常:", file=sys.stderr)
        traceback.print_exception(exc_type, exc, tb, file=sys.stderr)

    sys.excepthook = _hook
    print(f"\n===== 服务启动 {time.strftime('%m-%d %H:%M:%S')} =====", flush=True)

    if open_browser is None:
        open_browser = os.environ.get(ENV_NO_BROWSER, "").strip() not in ("1", "true", "yes")

    mode, p = resolve_port(port)
    if mode == "running":
        say(f"[提示] 检测到工具已在运行（http://127.0.0.1:{p}），直接打开页面，不重复启动。")
        if open_browser:
            _open_when_ready(p, immediate=True)
        time.sleep(1.0)  # 给浏览器线程一点启动时间
        return 0
    if mode == "busy-foreign":
        say(f"[错误] 端口 {p} 已被其它程序占用（不是本工具的实例）。")
        say("       可改用 --port <端口> 指定其它端口，或设置环境变量 MEDIA_DOWNLOADER_PORT。")
        return 1
    if mode == "exhausted":
        say("[错误] 8848 到 8870 端口全部被占用了，请关闭其它程序后重试。")
        return 1

    say("")
    say("============================================")
    say("           媒体下载工具  已启动")
    say("============================================")
    say(f"  访问地址:  http://127.0.0.1:{p}")
    say(f"  运行日志:  {LOG_DIR}")
    say("  关闭本窗口（或 Ctrl+C）即可停止服务。")
    say("============================================")
    say("")
    if open_browser:
        _open_when_ready(p)

    import uvicorn  # noqa: E402

    # Frozen 启动前先显式导入一次：若某个打包依赖缺失，uvicorn 的字符串导入
    # 只会留下 “Could not import module app.main”，看不到真正缺哪个模块。
    # 显式 import 不改变运行语义（uvicorn 后续命中 sys.modules 缓存），但能把
    # 完整 traceback 落进 service.err.log，便于发布构建自证。
    try:
        __import__("app.main")
    except Exception:  # noqa: BLE001
        import traceback
        print("[startup] import app.main failed:", file=sys.stderr, flush=True)
        traceback.print_exc(file=sys.stderr)
        return 3

    uvicorn.run("app.main:app", host="127.0.0.1", port=p, log_level="info")
    return 0


if __name__ == "__main__":
    from app.ports import InvalidPortError, parse_launch_flags

    try:
        _port, _ob = parse_launch_flags(sys.argv[1:])
    except InvalidPortError as e:
        # 1D.1：与 launcher.py 行为完全一致 —— 非法显式端口 exit 2，绝不回退扫描
        print(f"[错误] 参数错误：{e}", file=sys.stderr)
        print("用法: serve.py [--port N] [--no-browser]", file=sys.stderr)
        sys.exit(2)
    sys.exit(main(port=_port, open_browser=_ob))
