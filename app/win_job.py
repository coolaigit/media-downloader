# -*- coding: utf-8 -*-
"""Windows 下子进程与父进程的生死绑定 + 孤儿 ffmpeg 收割。

为什么需要（2026-09-28 用户反馈「批量下载有时成功有时失败有时没反应」）：
  实测系统里累积了 14 个孤儿 ffmpeg.exe（最早到 3 天前）——服务重启/任务卡死后，
  ffmpeg 作为孤儿继续活着：占着 .part 文件的写句柄（同地址重下会失败）、
  占着站点连接（新请求被限流）、占着磁盘。表现就是「有时成功、有时失败、
  有时一直没反应」。

两层防御：
  1) Job Object 绑定：每个 ffmpeg/gallery-dl 子进程启动时挂进父进程的 Job，
     父进程一死 Windows 自动杀掉整组子进程（ctypes 实现，无需 pywin32）。
  2) 孤儿收割线程：定时扫描本项目的 ffmpeg 进程，父进程已不存在的（真孤儿）杀掉。
"""
from __future__ import annotations

import ctypes
import json
import os
import threading
import time

from pathlib import Path

_ffmpeg_path = ""
_reaper_started = False
_KEEP_ALIVE_HANDLES: list[int] = []   # Job 句柄保活池（永不手动关闭，见 bind 文档）


def _job_handle():
    """创建一个「句柄关闭即杀全部进程」的 Job 对象。"""
    k32 = ctypes.windll.kernel32
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    JobObjectExtendedLimitInformation = 9
    job = k32.CreateJobObjectW(None, None)
    if not job:
        return None
    # JOBOBJECT_EXTENDED_LIMIT_INFORMATION 的 BasicLimitInformation.LimitPeriod 字段偏移
    # 直接用最小结构：JOBOBJECT_BASIC_LIMIT_INFORMATION 前 8 字节是
    # PerProcessUserTimeLimit(8) + PerJobUserTimeLimit(8)，LimitFlags 在偏移 20
    # （64 位下结构对齐后 LimitFlags 位于偏移 20）。为稳妥起见写入完整扩展结构。
    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [("ReadOperationCount", ctypes.c_uint64),
                    ("WriteOperationCount", ctypes.c_uint64),
                    ("OtherOperationCount", ctypes.c_uint64),
                    ("ReadTransferCount", ctypes.c_uint64),
                    ("WriteTransferCount", ctypes.c_uint64),
                    ("OtherTransferCount", ctypes.c_uint64)]

    class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                    ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", ctypes.c_uint32),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", ctypes.c_uint32),
                    ("Affinity", ctypes.POINTER(ctypes.c_ulong)),
                    ("PriorityClass", ctypes.c_uint32),
                    ("SchedulingClass", ctypes.c_uint32)]

    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                    ("IoInfo", IO_COUNTERS),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    k32.SetInformationJobObject(job, JobObjectExtendedLimitInformation,
                                ctypes.byref(info), ctypes.sizeof(info))
    return job


def bind_to_current_process_job(pid: int) -> bool:
    """把子进程 pid 挂进「父进程死则全灭」的 Job。

    ⚠️ Job 句柄必须**永久保活**（存入模块级列表，永不 close）：
    句柄一被 GC 关闭就会触发 KILL_ON_JOB_CLOSE，当场杀掉刚启动的子进程 ——
    第一版把句柄存在局部变量里，GC 时机随机，造成下载「有时成功有时秒失败」。
    父进程退出时 OS 会关闭全部句柄 → Job 里的子进程被杀，正是想要的语义。
    """
    if os.name != "nt":
        return False
    try:
        k32 = ctypes.windll.kernel32
        job = _job_handle()
        if not job:
            return False
        PROCESS_SET_QUOTA = 0x0100
        PROCESS_TERMINATE = 0x0001
        h = k32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, pid)
        if not h:
            return False
        ok = bool(k32.AssignProcessToJobObject(job, h))
        k32.CloseHandle(h)
        if ok:
            # 保活：句柄永不手动关闭；父进程退出时 OS 统一回收并杀子进程
            _KEEP_ALIVE_HANDLES.append(job)
        return ok
    except Exception:  # noqa: BLE001
        return False


def reap_orphan_ffmpeg(max_age_hours: float = 0.0) -> int:
    """杀掉「父进程已死」的本项目 ffmpeg 进程，返回杀掉的数量。

    判据（三条都满足才算孤儿，绝不误杀正在用的）：
      - 命令行里带本项目的 bin\\ffmpeg.exe 路径
      - 其父进程 PID 在当前系统里已不存在
      - （可选）进程创建时间早于 max_age_hours 小时前
    """
    if os.name != "nt":
        return 0
    killed = 0
    try:
        import subprocess as _sp
        # ⚠️ 活进程集合必须是**全系统所有进程**，不能只列 ffmpeg ——
        # ffmpeg 的父进程是 python，只列 ffmpeg 的话父进程永远「不存在」，
        # 会把正在使用的 ffmpeg 也杀掉（第一版就犯了这个错）。
        out = _sp.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process | "
             "Select-Object ProcessId,ParentProcessId,Name | ConvertTo-Json -Compress"],
            capture_output=True, timeout=60,
            # ⚠️ 1E.2：必须显式 mbcs + errors=replace。text=True 默认按 locale/utf-8
            # 解码，系统进程名含中文（GBK 0xd0…）时读取线程直接 UnicodeDecodeError
            # 死亡 → communicate() 永久挂起 → startup 事件卡死 → 服务永不就绪
            #（RC2 frozen 首测实证：每次启动都挂在「Waiting for application startup」）。
            encoding="mbcs", errors="replace",
            creationflags=getattr(_sp, "CREATE_NO_WINDOW", 0))
        data = json.loads(out.stdout or "[]")
    except Exception:  # noqa: BLE001
        return 0
    if isinstance(data, dict):
        data = [data]
    alive_pids = {int(p["ProcessId"]) for p in data
                  if str(p.get("Name", "")).lower() == "ffmpeg.exe"}
    ff_pids = [p for p in data if str(p.get("Name", "")).lower() == "ffmpeg.exe"]
    k32 = ctypes.windll.kernel32
    for p in ff_pids:
        pid = int(p["ProcessId"])
        ppid = int(p["ParentProcessId"])
        if ppid in alive_pids:
            continue                      # 父进程还活着，不是孤儿
        if max_age_hours > 0:
            try:
                import subprocess as _sp2
                cr = _sp2.run(
                    ["powershell", "-NoProfile", "-Command",
                     f"(Get-Process -Id {pid}).StartTime.ToString('o')"],
                    capture_output=True, text=True, timeout=15,
                    encoding="mbcs", errors="replace",
                    creationflags=getattr(_sp, "CREATE_NO_WINDOW", 0))
                t0 = time.mktime(time.strptime(cr.stdout.strip()[:19], "%Y-%m-%dT%H:%M:%S"))
                if (time.time() - t0) < max_age_hours * 3600:
                    continue
            except Exception:  # noqa: BLE001
                pass
        try:
            PROCESS_TERMINATE = 0x0001
            h = k32.OpenProcess(PROCESS_TERMINATE, False, pid)
            if h:
                k32.TerminateProcess(h, 1)
                k32.CloseHandle(h)
                killed += 1
        except Exception:  # noqa: BLE001
            continue
    return killed


def run_bound(argv: list, timeout: float | None = None, **popen_kw):
    """subprocess.run 的「父死子灭」版：Popen → 绑 Job → 等待。

    用法与 subprocess.run 对齐（stdin/stdout/stderr/env/creationflags 原样传），
    超时行为一致：杀进程并抛 TimeoutExpired。
    """
    import subprocess
    p = subprocess.Popen(argv, **popen_kw)
    bind_to_current_process_job(p.pid)
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            p.kill()
            out, err = p.communicate(timeout=15)
        except Exception:  # noqa: BLE001
            out, err = b"", b""
        raise
    return subprocess.CompletedProcess(argv, p.returncode, out, err)


def start_reaper_thread(interval_sec: int = 300):
    """启动孤儿 ffmpeg 收割线程（服务启动时调用一次）。"""
    global _reaper_started
    if _reaper_started:
        return
    _reaper_started = True

    def _loop():
        while True:
            try:
                n = reap_orphan_ffmpeg()
                if n:
                    print(f"[reaper] 清理孤儿 ffmpeg {n} 个", flush=True)
            except Exception:  # noqa: BLE001
                pass
            time.sleep(interval_sec)

    threading.Thread(target=_loop, daemon=True).start()


def project_ffmpeg_path() -> str:
    """本项目的 ffmpeg 绝对路径（收割判据用）。"""
    global _ffmpeg_path
    if _ffmpeg_path:
        return _ffmpeg_path
    try:
        from .stream_downloader import _ffmpeg_available
        _ffmpeg_path = str(_ffmpeg_available() or "")
    except Exception:  # noqa: BLE001
        _ffmpeg_path = ""
    return _ffmpeg_path
