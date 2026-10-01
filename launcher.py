# -*- coding: utf-8 -*-
"""统一程序入口（1C）。

三种形态都是它：
    源码：    python launcher.py
    frozen：  MediaDownloader.exe
    内部子命令：MediaDownloader.exe --internal <cmd> [args...]
              python launcher.py --internal <cmd> [args...]

**分流必须在最前面**：带 `--internal` 时直接进入子命令、跑完即退，
绝不启动 FastAPI —— 否则会出现
    主程序 → exe --internal login-browser → 又起服务器 → 又开浏览器 → 递归
另：windowed frozen 下 sys.stdout 可能为 None / 坏句柄，internal 分支
在 import 任何会碰 stdout 的模块前先兜底重定向（1C probe 验收项）。

正常服务启动复用 serve.py 的崩溃自证逻辑（日志走 DATA_ROOT\\logs\\）。
"""
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _ensure_stdio() -> None:
    """windowed frozen 兜底：stdout/stderr 为 None 或坏句柄时重定向到日志。

    判据沿用 run.py 的教训（2026-09-30）：不能只判 None —— stdout 还可能是
    「句柄已失效」的流，isatty()/fileno() 会抛异常，用 try 包住。
    """
    for name, mode in (("stdout", "a"), ("stderr", "a")):
        stream = getattr(sys, name, None)
        broken = stream is None
        if not broken:
            try:
                stream.fileno()
                stream.isatty()
            except Exception:  # noqa: BLE001
                broken = True
        if broken:
            try:
                from app.config import DATA_ROOT
                log_dir = DATA_ROOT / "logs"
                log_dir.mkdir(parents=True, exist_ok=True)
                setattr(sys, name, open(log_dir / f"internal.{name}.log",
                                        mode, buffering=1, encoding="utf-8",
                                        errors="replace"))
            except Exception:  # noqa: BLE001
                setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))


def main() -> int:
    argv = sys.argv[1:]

    # ---------- internal 子命令：跑完即退，永不启动服务器 ----------
    if argv and argv[0] == "--internal":
        _ensure_stdio()
        from app.internal_cmds import dispatch
        cmd = argv[1] if len(argv) > 1 else ""
        return dispatch(cmd, argv[2:])

    # ---------- 正常 Desktop 服务（1D：端口/实例/浏览器统一在这里之后决策） ----------
    from app.ports import InvalidPortError, parse_launch_flags

    try:
        port, open_browser = parse_launch_flags(argv)
    except InvalidPortError as e:
        # 1D.1：非法显式端口一律 exit 2，绝不回退自动扫描
        print(f"[错误] 参数错误：{e}", file=sys.stderr)
        print("用法: launcher.py [--port N] [--no-browser] | --internal <cmd> [args...]",
              file=sys.stderr)
        return 2

    import serve

    return serve.main(port=port, open_browser=open_browser)


if __name__ == "__main__":
    sys.exit(main())
