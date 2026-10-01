# -*- coding: utf-8 -*-
"""服务常驻启动器（带崩溃自证）。

为什么存在（2026-09-30）：uvicorn 实例曾无痕消失（事件日志无崩溃记录、
stderr 无从查起）。本启动器做三件事：
  1) faulthandler —— 原生崩溃（段错误等）时把线程栈写进 _service_crash.log
  2) stdout/stderr 全量落盘到 _service.out.log / _service.err.log
  3) 未处理异常钩子也落盘
用法：C:\\Python314\\python.exe _serve.py   （由 打开工具.bat / WMI 调起）
"""
import faulthandler
import os
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
os.chdir(ROOT)

crash_log = open(os.path.join(ROOT, "_service_crash.log"), "w", buffering=1)
faulthandler.enable(crash_log)
sys.stdout = open(os.path.join(ROOT, "_service.out.log"), "a", buffering=1)
sys.stderr = open(os.path.join(ROOT, "_service.err.log"), "a", buffering=1)


def _hook(exc_type, exc, tb):
    import traceback
    print(f"\n[{time.strftime('%m-%d %H:%M:%S')}] 未处理异常:", file=sys.stderr)
    traceback.print_exception(exc_type, exc, tb, file=sys.stderr)


sys.excepthook = _hook
print(f"\n===== 服务启动 {time.strftime('%m-%d %H:%M:%S')} =====", flush=True)

import uvicorn  # noqa: E402

uvicorn.run("app.main:app", host="127.0.0.1", port=8848, log_level="info")
