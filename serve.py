# -*- coding: utf-8 -*-
"""服务常驻启动器（带崩溃自证）。

为什么存在（2026-09-30）：uvicorn 实例曾无痕消失（事件日志无崩溃记录、
stderr 无从查起）。本启动器做三件事：
  1) faulthandler —— 原生崩溃（段错误等）时把线程栈写进 service_crash.log
  2) stdout/stderr 全量落盘到 service.out.log / service.err.log
  3) 未处理异常钩子也落盘

1A.1（2026-10-01）：所有运行日志统一写入 DATA_ROOT\\logs\\，
不再向程序资源目录（源码目录 / exe 目录 / _internal）写任何文件——
RESOURCE_ROOT 只读。chdir 已移除：`python serve.py` 时脚本目录
自动位于 sys.path[0]，app 包导入不依赖当前工作目录；
这里仍显式 insert 一次，保证任意路径调起都成立。

注意：windowed frozen 下 sys.stdout 可能为 None / 坏句柄——本启动器
在导入任何会碰 stdout 的模块之前就把 stdout/stderr 重定向到日志文件，
因此天然规避（frozen 专项在阶段 1C 再验证 run.py 直启路径）。

用法：python serve.py   （由 打开工具.bat / WMI 调起）
"""
import faulthandler
import os
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.config import DATA_ROOT  # noqa: E402

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

import uvicorn  # noqa: E402

uvicorn.run("app.main:app", host="127.0.0.1", port=8848, log_level="info")
