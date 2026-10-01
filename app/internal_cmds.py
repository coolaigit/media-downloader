# -*- coding: utf-8 -*-
"""internal 子命令注册表（1C）。

`MediaDownloader.exe --internal <cmd> [args...]`（源码模式为
`python launcher.py --internal ...`）由 launcher.py 分流到这里。

设计约束：
- **保持子进程边界**：登录浏览器 / gallery-dl 生命周期长、可能被独立终止、
  带自己的进程树 —— 绝不为了"省一个进程"改成主进程内函数调用。
- 每个命令返回**退出码**（int），launcher 据此 sys.exit —— 退出码能回传
  是 1C probe 的验收项之一。
- stdout 可能为 None（windowed frozen）：launcher 在分流前已做兜底重定向，
  这里不用再操心；但 handler 自身不要新增对 stdout 的硬假设。
"""
from __future__ import annotations

import sys


def _cmd_login_browser(args: list[str]) -> int:
    """登录浏览器子命令：参数语义与原 `python -m app.login_browser` 完全一致。"""
    from .login_browser import main as lb_main
    sys.argv = ["login-browser", *args]
    return int(lb_main() or 0)


def _cmd_gallery_dl(args: list[str]) -> int:
    """gallery-dl 子命令：把参数原样交给 gallery_dl 的 CLI main。

    为什么走 CLI main 而不是库 API（1C 定案）：现有集成高度依赖 CLI 语义
    （--dump-json / --range / --cookies / 429 重试参数），`gallery_dl.main()`
    就是官方 CLI 入口本身 —— 参数、退出码、stdout JSON 输出全部原样保持，
    等价于"换了个解释器跑同一条命令"，零行为漂移风险。
    """
    import gallery_dl
    sys.argv = ["gallery-dl", *args]
    return int(gallery_dl.main() or 0)


# 命令名 → handler。新增 internal 命令只改这里 + launcher 无需动。
COMMANDS = {
    "login-browser": _cmd_login_browser,
    "gallery-dl": _cmd_gallery_dl,
}


def dispatch(cmd: str, args: list[str]) -> int:
    """执行 internal 子命令，返回退出码。未知命令返回 2（argparse 惯例）。"""
    handler = COMMANDS.get(cmd)
    if handler is None:
        known = ", ".join(sorted(COMMANDS)) or "（无）"
        print(f"[internal] 未知子命令：{cmd}（可用：{known}）", file=sys.stderr)
        return 2
    return int(handler(args) or 0)
