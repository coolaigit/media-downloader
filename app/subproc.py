# -*- coding: utf-8 -*-
"""统一子进程助手：internal 命令构造 + 子进程环境控制（1C）。

## 为什么需要（frozen 语义）

源码模式下 `sys.executable` 是 python.exe，所以 `sys.executable -m app.login_browser`
能跑。PyInstaller 打包后 `sys.executable` 是 MediaDownloader.exe —— 再按老写法
等于「用自己启动自己」，要么递归起服务器、要么根本不是解释器。

所以规定：

- **internal 命令**（本程序的内部子命令，如 login-browser / gallery-dl）
  一律经 `build_internal_command()` 构造：
    源码： [python.exe, launcher.py, --internal, <cmd>, ...]
    frozen: [MediaDownloader.exe, --internal, <cmd>, ...]
  launcher.py / 打包入口在最顶部先分流 `--internal`，**绝不递归启动服务器**。
  业务模块里不要自行判断 sys.frozen —— 都走这里。

- **外部程序**（ffmpeg / explorer 等）不经 internal dispatcher；
  argv 由调用方明确提供，不能把 frozen 下的本程序 EXE 当成 Python 解释器。

- **子进程环境**：cwd 必须绝对化（frozen/安装版启动目录可能是 Desktop、
  Program Files、浏览器启动目录，不能依赖"当前目录刚好是项目目录"）。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from .config import FROZEN, RESOURCE_ROOT

LAUNCHER_SCRIPT = RESOURCE_ROOT / "launcher.py"

# 所有子进程统一的 Windows 行为
_BASE_CREATIONFLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def build_internal_command(cmd: str, *args: str) -> list[str]:
    """构造「调用本程序某个 internal 子命令」的 argv。

    源码：  [sys.executable, <项目根>/launcher.py, --internal, cmd, ...]
    frozen: [sys.executable, --internal, cmd, ...]
             （PyInstaller onedir/onefile 都把 argv[1:] 原样交给入口脚本）
    """
    if FROZEN:
        return [sys.executable, "--internal", cmd, *[str(a) for a in args]]
    return [sys.executable, str(LAUNCHER_SCRIPT), "--internal", cmd,
            *[str(a) for a in args]]


def child_env(extra: dict | None = None) -> dict:
    """子进程环境变量基线：显式编码，杜绝「靠继承碰运气」。"""
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    if extra:
        env.update({k: v for k, v in extra.items() if v is not None})
    return env


def popen_internal(cmd: str, *args: str, cwd: Path | str | None = None,
                   env_extra: dict | None = None, **popen_kw) -> subprocess.Popen:
    """以 internal 命令启动一个**长期存活**的子进程（如登录浏览器）。

    与 run_external（等待并收集结果）不同：不等待、不捕获输出，
    调用方拿 Popen 自己管理生命周期。
    默认 CREATE_NO_WINDOW + 显式 cwd/env，其余参数原样透传。
    """
    argv = build_internal_command(cmd, *args)
    kw = {
        "cwd": str(Path(cwd).resolve()) if cwd else str(RESOURCE_ROOT),
        "env": child_env(env_extra),
        "creationflags": popen_kw.pop("creationflags", _BASE_CREATIONFLAGS),
        **popen_kw,
    }
    return subprocess.Popen(argv, **kw)


def run_external(argv: list, *, cwd: Path | str | None = None,
                 env_extra: dict | None = None, timeout: float | None = None,
                 **run_kw) -> subprocess.CompletedProcess:
    """运行**真正的外部程序**（如 ffmpeg / explorer）。

    与 internal 的区别：argv 由调用方给全（可执行文件必须是绝对路径或 PATH 可见），
    这里只统一 cwd/env/flags，不解释 argv[0]。
    """
    kw = {
        "cwd": str(Path(cwd).resolve()) if cwd else str(RESOURCE_ROOT),
        "env": child_env(env_extra),
        "creationflags": run_kw.pop("creationflags", _BASE_CREATIONFLAGS),
        "timeout": timeout,
        **run_kw,
    }
    return subprocess.run([str(a) for a in argv], **kw)
