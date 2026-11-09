# -*- coding: utf-8 -*-
"""Windows 下统一隐藏子进程的控制台窗口。

打包版是 windowed（spec: console=False），本身没有控制台。此时任何
`subprocess.run(["ffmpeg.exe", ...])` / `subprocess.Popen(["node.exe", ...])`
都会被 Windows 新建一个 conhost 控制台窗口，用户下载过程中就会看到一个
黑框一闪（2026-11-07 实测：ffmpeg.exe、node.exe 两个 ConsoleWindowClass）。

做法：在 config 被任何业务模块导入之前，给 `subprocess.Popen` 加一层默认
`CREATE_NO_WINDOW`。只做 setdefault —— 显式传了 creationflags（例如需要可见
验证窗口的 browser_skill、需要新进程组的 serve.py）的地方完全不受影响；
GUI 程序（Chrome/Chromium）也不受影响，这个标志只管控制台窗口。

复杂度：一次性 O(1) 补丁，之后每次 Popen 只多一次 dict.setdefault。
"""
from __future__ import annotations

import os
import subprocess

_FLAG = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_PATCH_MARK = "_md_quiet_console"


def ensure_quiet_subprocess() -> None:
    """全局生效一次；非 Windows 或已打过补丁时直接返回。"""
    if os.name != "nt" or not _FLAG:
        return
    if getattr(subprocess.Popen, _PATCH_MARK, False):
        return
    original = subprocess.Popen

    class _QuietPopen(original):  # type: ignore[misc,valid-type]
        def __init__(self, *args, **kwargs):
            kwargs.setdefault("creationflags", _FLAG)
            super().__init__(*args, **kwargs)

    setattr(_QuietPopen, _PATCH_MARK, True)
    subprocess.Popen = _QuietPopen  # type: ignore[assignment]
