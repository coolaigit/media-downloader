# -*- coding: utf-8 -*-
"""下载任务队列：统一管理图片与视频下载任务的状态、进度、取消。

状态机：queued -> running -> (completed | failed | cancelled)
线程模型：每个任务一个工作线程，任务内部的图片并发由 downloader 控制。
"""
from __future__ import annotations

import queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .config import DATA_ROOT, DOWNLOADS_DIR
from .concurrency import (GOVERNOR, KIND_STREAM_DOWNLOAD, KIND_VIDEO_DOWNLOAD,
                          OK, classify_error)
from .downloader import DownloadResult, batch_download_images, download_file
from .stream_downloader import download_stream
from .video_downloader import DownloadCancelled, download_video, request_cancel

# 任务状态
ST_QUEUED = "queued"
ST_RUNNING = "running"
ST_COMPLETED = "completed"
ST_FAILED = "failed"
ST_CANCELLED = "cancelled"

TERMINAL = {ST_COMPLETED, ST_FAILED, ST_CANCELLED}


class _AdjustableGate:
    """容量可调的闸门，用来限制「同时跑几个 yt-dlp」。

    为什么需要它：关键词搜视频的结果是一条条**视频页面地址**，批量下载要走 yt-dlp
    逐个解析+下载。原先 `submit_video` 是「一个任务一个线程、提交即开跑」，
    勾选 20 条就是 20 个 yt-dlp 同时抢带宽 —— 本机走代理，实测并发一高就大面积
    ReadTimeout / 502。这里把并发收进可控范围（默认 3），并且在排队期间
    任务状态保持 `queued`、`current` 写明「排队中…」，而不是假装已经在下载。

    用「可调」而不是固定 Semaphore，是因为界面上能就地改并发数，
    而且最后一次设置应当对整批生效。
    """

    def __init__(self, capacity: int = 3):
        self._cv = threading.Condition()
        self._capacity = max(1, int(capacity))
        self._running = 0

    def set_capacity(self, n: int) -> int:
        with self._cv:
            self._capacity = max(1, min(int(n or 1), 16))
            self._cv.notify_all()
            return self._capacity

    @property
    def capacity(self) -> int:
        with self._cv:
            return self._capacity

    def acquire(self, cancelled=None) -> bool:
        """等到有名额再进入；返回 False 表示等待期间被取消。"""
        with self._cv:
            while self._running >= self._capacity:
                if cancelled is not None and cancelled():
                    return False
                # 上限 0.2s 是为了能及时看到「容量被调大」和「被取消」
                self._cv.wait(timeout=0.2)
            self._running += 1
            return True

    def release(self) -> None:
        with self._cv:
            self._running = max(0, self._running - 1)
            self._cv.notify_all()


# 注意：V3.0 起视频下载不再用这里的全局闸门，改为
# GOVERNOR.gate_for_url(url, KIND_VIDEO_DOWNLOAD) —— 按平台独立升降速。
# _AdjustableGate 保留是因为 test_video_search_offline.py 直接用它做离线验证，
# 而且它的「容量可调」语义仍是 concurrency.AdaptiveGate 的基础。


# 平台登录态刷新必须串行；同一时刻只允许一个线程操作浏览器 profile /
# cookies.txt，避免大批量任务形成“惊群”并再次把 Cookie 文件写坏。
_PLATFORM_REFRESH_GUARD = threading.Lock()
_PLATFORM_REFRESH_LOCKS: dict[str, threading.Lock] = {}
_PLATFORM_REFRESH_TS: dict[str, float] = {}
_PLATFORM_PACE_GUARD = threading.Lock()
_PLATFORM_LAST_START: dict[str, float] = {}


def _platform_lock(platform: str) -> threading.Lock:
    with _PLATFORM_REFRESH_GUARD:
        lock = _PLATFORM_REFRESH_LOCKS.get(platform)
        if lock is None:
            lock = threading.Lock()
            _PLATFORM_REFRESH_LOCKS[platform] = lock
        return lock


def _cookie_auth_error(message: str) -> bool:
    low = str(message or "").lower()
    return (
        "fresh cookies" in low
        or "http error 412" in low
        or "http 412" in low
        or "netscape format cookies file" in low
        or "cookies are needed" in low
    )


def _snapshot_cookie_file(source: str, task_id: str) -> str:
    """给单个 yt-dlp 任务复制一份只属于它的 CookieJar。

    yt-dlp 会把 CookieJar 回写到 cookiefile。多个下载任务若共用同一个文件，
    会发生并发覆盖/半写读取，表现为 Netscape 格式损坏或短期 Cookie 丢失。
    """
    src = Path(str(source or "")).expanduser()
    if not src.is_file():
        return ""
    dst_dir = DATA_ROOT / "tmp" / "cookie_snapshots"
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / f"{task_id}-{uuid.uuid4().hex[:8]}.txt"
    for attempt in range(5):
        try:
            data = src.read_bytes()
            head = data[:256].decode("utf-8", errors="ignore")
            if "Netscape HTTP Cookie File" not in head:
                if attempt < 4:
                    time.sleep(0.05 * (attempt + 1))
                    continue
                return ""
            dst.write_bytes(data)
            return str(dst)
        except OSError:
            if attempt < 4:
                time.sleep(0.05 * (attempt + 1))
    return ""


def _cleanup_cookie_snapshot(path: str) -> None:
    if not path:
        return
    try:
        p = Path(path)
        # 只删我们自己的临时快照，绝不能误删用户/平台主 Cookie 文件。
        if p.parent == (DATA_ROOT / "tmp" / "cookie_snapshots"):
            p.unlink(missing_ok=True)
    except OSError:
        pass


def _pace_platform(url: str) -> None:
    """抖音长批次轻量节流，减少连续几百次解析触发 412/风控。"""
    try:
        from . import platform_login
        platform = platform_login.platform_for_url(url)
    except Exception:
        platform = ""
    if platform != "dy":
        return
    minimum_gap = 0.7
    with _PLATFORM_PACE_GUARD:
        now = time.monotonic()
        last = _PLATFORM_LAST_START.get(platform, 0.0)
        wait = minimum_gap - (now - last)
        if wait > 0:
            time.sleep(wait)
        _PLATFORM_LAST_START[platform] = time.monotonic()


def _refresh_platform_cookie(url: str) -> tuple[bool, str]:
    """遇到 fresh-cookie/412 时只集中刷新一次平台登录态。"""
    try:
        from . import platform_login
        platform = platform_login.platform_for_url(url)
    except Exception as e:
        return False, str(e)
    if not platform:
        return False, "未识别平台"

    lock = _platform_lock(platform)
    with lock:
        # 同一波失败的其它 worker 不重复开浏览器；15 秒内复用前一个刷新结果。
        last = _PLATFORM_REFRESH_TS.get(platform, 0.0)
        if time.monotonic() - last < 15.0:
            return True, "复用刚刚刷新的登录态"
        st = platform_login.refresh_from_profile(platform)
        ok = (
            bool(st.get("refresh_ok"))
            and bool(st.get("cookie_file"))
            and bool(st.get("logged_in"))
        )
        if ok:
            _PLATFORM_REFRESH_TS[platform] = time.monotonic()
            return True, "已自动刷新平台登录态"
        if st.get("refresh_ok") and not st.get("logged_in"):
            return False, "刷新到了设备 Cookie，但账号认证 Cookie 不可用，需要在平台登录中心重新登录"
        return False, str(st.get("refresh_error") or "平台登录态刷新失败")


@dataclass
class Task:
    id: str
    kind: str                      # image | video
    title: str = ""
    status: str = ST_QUEUED
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None

    # 目标
    urls: list[dict] = field(default_factory=list)   # 图片：[{url, referer, title}]
    video_url: str = ""                               # 视频
    output_dir: str = ""

    # 进度
    total: int = 0
    done: int = 0
    failed: int = 0
    skipped: int = 0
    percent: float = 0.0
    speed: float = 0.0
    current: str = ""
    error: str = ""

    # 结果
    files: list[dict] = field(default_factory=list)
    items: list[dict] = field(default_factory=list)   # 每张图片的明细
    alt_used: int = 0          # 有多少个文件是「首选地址失败、自动换了同媒体其它档位」
    note: str = ""             # 兜底说明（如「已自动改用 mweb 客户端，清晰度受限」）

    # 视频选项
    options: dict = field(default_factory=dict)

    def failed_items(self) -> list[dict]:
        """本任务里失败的条目（排除用户取消）。

        「用户取消」在 items 里也是 ok=False，但它不是失败 —— 把它算进失败列表，
        点「重试失败」时会把用户主动放弃的条目又下回来，属于帮倒忙。
        """
        return [it for it in self.items
                if (not it.get("ok")) and str(it.get("error") or "") != "已取消"]

    def public(self) -> dict:
        # 失败项必须**全部**保留。
        # 原来统一 items[-200:]：300 条任务里若前 100 条失败、后 200 条成功，
        # 恰好把用户唯一要逐条处理的失败项截掉，等于没给。
        fails: list[dict] = []
        rest: list[dict] = []
        for it in self.items:
            if (not it.get("ok")) and str(it.get("error") or "") != "已取消":
                fails.append(it)
            else:
                rest.append(it)
        items = fails + rest[-200:]
        items.sort(key=lambda x: x.get("index") if isinstance(x.get("index"), int) else 0)
        return {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "total": self.total,
            "done": self.done,
            "failed": self.failed,
            "skipped": self.skipped,
            "percent": round(self.percent, 1),
            "speed": self.speed,
            "current": self.current,
            "error": self.error,
            "alt_used": self.alt_used,
            "note": self.note,
            "files": self.files,
            "items": items,               # 失败全留 + 其余最近 200 条
            "items_total": len(self.items),
            "failed_items": len(fails),
            "video_url": self.video_url,
            "output_dir": self.output_dir,
        }


def apply_video_progress(t: Task, info: dict) -> None:
    """把下载器的进度回调映射到任务对象（调用方需持有 manager 锁）。

    以前这里是 `t.percent = info.get("percent") or t.percent`：分片下载回调
    没带 percent，任务条就一直 0%，直到结束瞬间跳 100%。现在统一走这里，
    并把 filename / note 都映射到 current，让「分片 x/N」在界面上可见。
    """
    p = info.get("percent")
    if p is not None:
        try:
            # 完成前最高 99.9%，100% 只由任务真正结束时设置
            t.percent = min(99.9, max(0.0, float(p or 0)))
        except (TypeError, ValueError):
            pass
    try:
        t.speed = float(info.get("speed") or 0)
    except (TypeError, ValueError):
        t.speed = 0.0
    label = str(info.get("filename") or info.get("note") or "")
    if label:
        t.current = label[:80]
    if info.get("status") == "downloading":
        t.done = 0
        if info.get("total_bytes"):
            t.total = 1


class TaskManager:
    def __init__(self, max_tasks: int = 2000):
        self._tasks: dict[str, Task] = {}
        self._order: list[str] = []          # 新任务在前
        self._lock = threading.RLock()
        self._max_tasks = max_tasks
        self._cancel_flags: dict[str, threading.Event] = {}

        # 视频批量下载以前是“每个条目立即起一个线程”。勾 300 条时会瞬间
        # 创建 300 个线程，即使并发闸门最终只允许少数几个 yt-dlp 真正开跑，
        # 仍会先把本机线程/内存资源堆高。改成固定 16 个 daemon worker +
        # 队列：提交多少都只是排队，不再按条目数量创建线程。
        self._video_queue: queue.Queue[str] = queue.Queue()
        self._video_workers_started = False
        self._video_worker_lock = threading.Lock()

    # ---------- 基础操作 ----------

    def get(self, task_id: str) -> Task | None:
        with self._lock:
            return self._tasks.get(task_id)

    def list(self, limit: int = 50) -> list[dict]:
        with self._lock:
            ids = self._order[:limit]
            return [self._tasks[i].public() for i in ids if i in self._tasks]

    def counts(self) -> dict:
        """返回全队列统计；任务面板即使只渲染最近 30 条也不会误报总数/误停轮询。"""
        with self._lock:
            vals = list(self._tasks.values())
            queued = sum(1 for t in vals if t.status == ST_QUEUED)
            running = sum(1 for t in vals if t.status == ST_RUNNING)
            return {
                "total": len(vals),
                "queued": queued,
                "running": running,
                "active": queued + running,
            }

    def _prune(self):
        """超出上限时清理最旧的已结束任务。"""
        if len(self._order) <= self._max_tasks:
            return
        for tid in reversed(self._order):
            t = self._tasks.get(tid)
            if t and t.status in TERMINAL:
                self._order.remove(tid)
                self._tasks.pop(tid, None)
                self._cancel_flags.pop(tid, None)
            if len(self._order) <= self._max_tasks:
                break

    def _register(self, task: Task):
        with self._lock:
            self._tasks[task.id] = task
            self._order.insert(0, task.id)
            self._prune()

    def _ensure_video_workers(self) -> None:
        if self._video_workers_started:
            return
        with self._video_worker_lock:
            if self._video_workers_started:
                return
            for idx in range(16):
                threading.Thread(
                    target=self._video_worker,
                    name=f"media-video-worker-{idx + 1}",
                    daemon=True,
                ).start()
            self._video_workers_started = True

    def _video_worker(self) -> None:
        while True:
            task_id = self._video_queue.get()
            try:
                self._run_video(task_id)
            except Exception:
                # _run_video/_run_video_inner 本身会把任务错误落到状态里；
                # 这里兜底只为保证 worker 不会因为单条异常退出。
                pass
            finally:
                self._video_queue.task_done()

    # ---------- 创建任务 ----------

    def submit_images(
        self,
        urls: list[dict],
        output_subdir: str = "images",
        title: str = "",
        concurrency: int = 6,
        skip_existing: bool = True,
        prefer_quality: str = "",
        cookies_file: str = "",
    ) -> str:
        out = _resolve_output(output_subdir)
        task = Task(
            id=_new_id(),
            kind="image",
            title=title or f"图片下载 {len(urls)} 张",
            urls=urls,
            total=len(urls),
            output_dir=str(out),
            options={"concurrency": concurrency, "skip_existing": skip_existing,
                     "prefer_quality": prefer_quality, "cookies_file": cookies_file},
        )
        self._register(task)
        self._cancel_flags[task.id] = threading.Event()
        threading.Thread(target=self._run_images, args=(task.id,), daemon=True).start()
        return task.id

    def submit_video(
        self,
        video_url: str,
        output_subdir: str = "videos",
        title: str = "",
        quality: str = "best",
        container: str = "",
        prefer_audio_only: bool = False,
        write_subtitles: bool = False,
        subtitle_langs: str = "zh.*,en",
        include_playlist: bool = False,
        cookies_file: str = "",
        cookies_from_browser: str = "",
        rate_limit: str = "",
        format_id: str = "",
    ) -> str:
        out = _resolve_output(output_subdir)
        task = Task(
            id=_new_id(),
            kind="video",
            title=title or "视频下载",
            video_url=video_url,
            total=1,
            output_dir=str(out),
            options={
                "quality": quality,
                "container": container,
                "prefer_audio_only": prefer_audio_only,
                "write_subtitles": write_subtitles,
                "subtitle_langs": subtitle_langs,
                "include_playlist": include_playlist,
                "cookies_file": cookies_file,
                "cookies_from_browser": cookies_from_browser,
                "rate_limit": rate_limit,
                "format_id": format_id,
            },
        )
        self._register(task)
        self._cancel_flags[task.id] = threading.Event()
        self._ensure_video_workers()
        self._video_queue.put(task.id)
        return task.id

    def set_video_concurrency(self, n: int) -> int:
        """设置「同时跑几个 yt-dlp」，返回生效后的值。

        V3.0 起这是**上限**：各平台在这个上限内自行升降速
        （抖音被 412 时会自己降到 1，稳定后再爬回来），不是一刀切定死。
        """
        return GOVERNOR.set_ceiling_for_kind(KIND_VIDEO_DOWNLOAD, n)

    def video_concurrency(self) -> int:
        return GOVERNOR.ceiling_for_kind(KIND_VIDEO_DOWNLOAD)

    def submit_videos(
        self,
        items: list[dict],
        output_subdir: str = "videos",
        quality: str = "best",
        container: str = "",
        include_playlist: bool = False,
        concurrency: int = 3,
        cookies_file: str = "",
        cookies_from_browser: str = "",
    ) -> list[str]:
        """批量提交视频页面地址（关键词搜视频 / 列表里勾选的那些）。

        items = [{url, title}]。每条各自成为一个任务，但**同时真正开跑的个数**
        由并发闸门限制（见 concurrency.AdaptiveGate）——
        用户勾 20 条时不会变成 20 个 yt-dlp 抢带宽。
        """
        self.set_video_concurrency(concurrency)
        ids: list[str] = []
        for it in items or []:
            url = str((it or {}).get("url") or "").strip()
            if not url:
                continue
            ids.append(self.submit_video(
                url,
                output_subdir=output_subdir,
                title=str((it or {}).get("title") or "")[:60],
                quality=quality,
                container=container,
                include_playlist=include_playlist,
                cookies_file=cookies_file,
                cookies_from_browser=cookies_from_browser,
            ))
        return ids

    def submit_streams(
        self,
        items: list[dict],
        output_subdir: str = "streams",
        title: str = "",
        skip_existing: bool = True,
        prefer_quality: str = "",
    ) -> str:
        """提交流媒体（m3u8/mpd）下载任务：items = [{url, referer, title}]。

        并发执行：具体路数由 _run_streams 每次启动时从设置
        （stream_concurrency，默认 4，1-8）实时读取 —— 设置页改完立即生效。
        """
        out = _resolve_output(output_subdir)
        task = Task(
            id=_new_id(),
            kind="stream",
            title=title or f"流媒体下载 {len(items)} 个",
            urls=items,
            total=len(items),
            output_dir=str(out),
            options={"skip_existing": skip_existing, "prefer_quality": prefer_quality},
        )
        self._register(task)
        self._cancel_flags[task.id] = threading.Event()
        threading.Thread(target=self._run_streams, args=(task.id,), daemon=True).start()
        return task.id

    # ---------- 取消 / 清理 ----------

    def cancel(self, task_id: str) -> bool:
        with self._lock:
            t = self._tasks.get(task_id)
            ev = self._cancel_flags.get(task_id)
        if not t or t.status in TERMINAL:
            return False
        if ev:
            ev.set()
        if t.kind == "video":
            request_cancel(task_id)   # 通知 yt-dlp 的 progress hook
        return True

    def remove(self, task_id: str) -> bool:
        self.cancel(task_id)
        with self._lock:
            if task_id in self._tasks:
                self._tasks.pop(task_id, None)
                if task_id in self._order:
                    self._order.remove(task_id)
                self._cancel_flags.pop(task_id, None)
                return True
        return False

    def clear_finished(self) -> int:
        n = 0
        with self._lock:
            for tid in list(self._order):
                t = self._tasks.get(tid)
                if t and t.status in TERMINAL:
                    self._tasks.pop(tid, None)
                    self._order.remove(tid)
                    self._cancel_flags.pop(tid, None)
                    n += 1
        return n

    def retry_failed(self, task_id: str) -> dict:
        """把某个任务里失败的条目重新提交成一个**新任务**（只重试失败项）。

        为什么新建任务、而不是把原任务的计数清零重跑：
        - 原任务的 items / files 是那次执行的证据，抹掉就再也说不清当时挂了哪几条；
        - 用户连点两次「重试」，新建任务天然幂等，不会互相覆盖计数；
        - 原任务失败明细还在，重试再失败也能对照。
        输出目录沿用原任务 —— 重试结果不能落到别的文件夹里去。
        """
        t = self.get(task_id)
        if not t:
            raise KeyError("任务不存在")
        if t.status not in TERMINAL:
            return {"ok": False, "count": 0,
                    "message": "任务还在进行中，等它结束再重试"}

        with self._lock:
            kind = t.kind
            base_options = dict(t.options or {})
            out_dir = t.output_dir
            orig_title = t.title or "下载任务"
            url_pool = [dict(x) for x in t.urls]
            video_url = t.video_url
            fails = t.failed_items()

        if kind == "video":
            # 视频批量里每条视频各自是一个任务（total=1），失败信息在 t.error 上，
            # items 是空的 —— 所以不能靠 fails 判断。
            if not video_url:
                return {"ok": False, "count": 0, "message": "这条视频没有可用地址"}
            urls: list[dict] = []
            count = 1
        else:
            urls = []
            for it in fails:
                idx = it.get("index")
                src = None
                if isinstance(idx, int) and 0 <= idx < len(url_pool):
                    src = dict(url_pool[idx])
                if not src or not str(src.get("url") or ""):
                    u = str(it.get("url") or "")
                    if u:
                        src = {"url": u}
                if src and str(src.get("url") or ""):
                    urls.append(src)
            count = len(urls)
            if not count:
                return {"ok": False, "count": 0, "message": "没有可重试的失败地址"}

        new_task = Task(
            id=_new_id(),
            kind=kind,
            title=f"{orig_title[:40]}（重试失败 {count} 条）",
            urls=urls,
            video_url=video_url,
            total=count,
            output_dir=out_dir,
            options=base_options,
        )
        self._register(new_task)
        self._cancel_flags[new_task.id] = threading.Event()
        if kind == "image":
            threading.Thread(target=self._run_images, args=(new_task.id,),
                             daemon=True).start()
        elif kind == "stream":
            threading.Thread(target=self._run_streams, args=(new_task.id,),
                             daemon=True).start()
        elif kind == "video":
            self._ensure_video_workers()
            self._video_queue.put(new_task.id)
        else:  # noqa: 兜底，未知类型不静默成功
            self.remove(new_task.id)
            return {"ok": False, "count": 0,
                    "message": f"不支持重试的任务类型：{kind}"}
        return {"ok": True, "count": count, "task_id": new_task.id,
                "title": new_task.title}

    def _cancelled(self, task_id: str) -> bool:
        ev = self._cancel_flags.get(task_id)
        return bool(ev and ev.is_set())

    # ---------- 工作线程 ----------

    def _run_images(self, task_id: str):
        t = self.get(task_id)
        if not t:
            return
        with self._lock:
            t.status = ST_RUNNING
            t.started_at = time.time()

        def on_item_done(index: int, result):
            with self._lock:
                if result.ok and result.skipped:
                    t.skipped += 1
                elif result.ok:
                    t.done += 1
                    t.files.append({
                        "name": Path(result.path).name,
                        "path": result.path,
                        "size": result.size,
                    })
                elif result.error == "已取消":
                    # 取消导致的未完成不计入失败，否则进度面板会虚报失败数
                    pass
                else:
                    t.failed += 1
                if result.note:
                    t.alt_used += 1
                if result.ok and result.path:
                    try:
                        from . import history
                        src = t.urls[index] if 0 <= index < len(t.urls) else {}
                        ext = Path(result.path).suffix.lower()
                        media_kind = "video" if ext in {
                            ".mp4", ".webm", ".mkv", ".mov", ".avi", ".flv",
                            ".m4v", ".mpg", ".mpeg", ".3gp", ".ts",
                        } else "image"
                        source_page = str(
                            src.get("origin_page")
                            or src.get("referer")
                            or src.get("url")
                            or result.url
                            or ""
                        )
                        thumb = str(src.get("thumbnail") or "")
                        if media_kind == "image" and not thumb:
                            thumb = str(src.get("url") or result.url or "")
                        history.record(
                            str(src.get("url") or result.url or ""),
                            None,
                            title=str(src.get("title") or Path(result.path).name),
                            kind=media_kind,
                            path=result.path,
                            size=int(result.size or 0),
                            source_url=source_page,
                            thumbnail=thumb,
                        )
                    except Exception:
                        pass

                t.items.append({
                    "index": index,
                    "url": result.url,
                    "ok": result.ok,
                    "skipped": result.skipped,
                    "name": Path(result.path).name if result.path else "",
                    "size": result.size,
                    "error": result.error,
                    "note": result.note,
                })
                finished = t.done + t.failed + t.skipped
                t.percent = finished * 100 / t.total if t.total else 0
                if result.url:
                    t.current = Path(result.url).name[:60]

        # 每个条目当前的字节进度（index -> {done,total}）。
        # 只用于算任务级百分比，条目完成后由 on_item_done 覆盖。
        item_prog: dict[int, dict] = {}

        def on_progress(index: int, info: dict):
            with self._lock:
                if info.get("speed"):
                    t.speed = info.get("speed") or 0.0
                slot = item_prog.setdefault(index, {})
                if info.get("progressed_bytes") is not None:
                    slot["done"] = int(info.get("progressed_bytes") or 0)
                if info.get("total_bytes"):
                    slot["total"] = int(info.get("total_bytes") or 0)

                # 百分比必须跟着**字节**走，不能只在条目完成时跳一格：
                # 单下 192MB 的片子时，旧逻辑在几十分钟里一直是 0%，
                # 用户看到的就是「点了下载没反应」（2026-10-29 实测反馈）。
                finished = t.done + t.failed + t.skipped
                cnt_pct = finished * 100.0 / t.total if t.total else 0.0
                total_bytes = sum(int(v.get("total") or 0) for v in item_prog.values())
                done_bytes = sum(int(v.get("done") or 0) for v in item_prog.values())
                byte_pct = (done_bytes * 100.0 / total_bytes) if total_bytes else 0.0
                # 收敛到 99.9：100% 由收尾逻辑给，避免「显示 100% 但还没写完文件」
                t.percent = min(99.9, max(cnt_pct, byte_pct))

                # 当前正在下的条目名（前端显示「正在下哪一条」）
                if 0 <= index < len(t.urls):
                    cur = str((t.urls[index] or {}).get("title") or "")
                    if not cur:
                        cur = Path(str((t.urls[index] or {}).get("url") or "")).name
                    if cur:
                        t.current = cur[:60]

        try:
            res = batch_download_images(
                t.urls,
                Path(t.output_dir),
                max_workers=int(t.options.get("concurrency", 6)),
                skip_existing=bool(t.options.get("skip_existing", True)),
                prefer_quality=str(t.options.get("prefer_quality", "") or ""),
                cookies_file=str(t.options.get("cookies_file", "") or ""),
                on_item_done=on_item_done,
                on_progress=on_progress,
                should_cancel=lambda: self._cancelled(task_id),
            )
            with self._lock:
                # 以批量结果为准修正计数（回调与批量统计可能因异常有偏差）
                t.done = res["ok_count"]
                t.failed = res["fail_count"]
                t.skipped = res["skipped_count"]
                t.speed = 0.0
                was_cancelled = bool(res.get("cancelled") or self._cancelled(task_id))
                # 取消不是「下完了」：保留取消那一刻的真实进度，
                # 否则界面显示「100% · 已取消」，用户会以为文件其实下全了。
                t.percent = min(t.percent, 99.9) if was_cancelled else 100.0
                if was_cancelled:
                    t.status = ST_CANCELLED
                    t.error = "已取消"
                elif t.done == 0 and t.skipped == 0 and t.failed > 0:
                    t.status = ST_FAILED
                    t.error = _classify_failures(
                        [str(it.get("error") or "") for it in t.items
                         if not it.get("ok")])
                else:
                    t.status = ST_COMPLETED
                t.finished_at = time.time()
        except Exception as e:  # noqa: BLE001
            with self._lock:
                t.status = ST_CANCELLED if self._cancelled(task_id) else ST_FAILED
                t.error = "已取消" if t.status == ST_CANCELLED else f"{type(e).__name__}: {e}"
                t.finished_at = time.time()

    def _run_video(self, task_id: str):
        t = self.get(task_id)
        if not t:
            return

        # 先过闸门：名额满了就停在 queued 状态等，别让几十个 yt-dlp 同时抢带宽。
        # V3.0 起闸门按平台独立 —— 抖音触发风控只降抖音，B站/YouTube 该跑多快还跑多快。
        gate = GOVERNOR.gate_for_url(t.video_url, KIND_VIDEO_DOWNLOAD)
        with self._lock:
            t.current = f"排队中…（同时最多 {gate.capacity} 个视频）"
        got = gate.acquire(cancelled=lambda: self._cancelled(task_id))
        if not got:
            with self._lock:
                t.status = ST_CANCELLED
                t.error = "已取消"
                t.finished_at = time.time()
            return
        try:
            self._run_video_inner(task_id)
        finally:
            # 把这次的结果反馈给闸门：成功攒恢复计数，412/验证码立刻降速
            gate.release(self._signal_of(t))

    @staticmethod
    def _signal_of(t: Task) -> str | None:
        """任务终态 → 并发信号。取消/被移除返回 None（纯释放，不参与升降速判断）。"""
        if t.status == ST_COMPLETED:
            return OK
        if t.status == ST_FAILED:
            return classify_error(t.error)
        return None

    def _run_video_inner(self, task_id: str):
        t = self.get(task_id)
        if not t:
            return
        with self._lock:
            t.status = ST_RUNNING
            t.started_at = time.time()
            t.title = t.title or "视频下载"
            t.current = ""

        t0 = time.time()

        def on_progress(info: dict):
            if self._cancelled(task_id):
                raise DownloadCancelled("用户已取消下载")
            with self._lock:
                apply_video_progress(t, info)

        try:
            o = t.options
            source_cookie = str(o.get("cookies_file") or "")
            retry_note = ""

            def _download_once():
                snap = _snapshot_cookie_file(source_cookie, task_id) if source_cookie else ""
                try:
                    return download_video(
                        t.video_url,
                        Path(t.output_dir),
                        task_id=task_id,
                        # 传标题：直链下载（抖音/P站等）的文件名是哈希，
                        # 只有 title 能让它落成可读的文件名（2026-10-25 实测）。
                        title=t.title or "",
                        quality=o.get("quality", "best"),
                        container=o.get("container", ""),
                        prefer_audio_only=bool(o.get("prefer_audio_only")),
                        write_subtitles=bool(o.get("write_subtitles")),
                        subtitle_langs=o.get("subtitle_langs", "zh.*,en"),
                        include_playlist=bool(o.get("include_playlist")),
                        # V3.0：把「跳过已存在」也传给 yt-dlp 路径 ——
                        # 它现在会读写 .done 标记，不传的话开关对它无效。
                        skip_existing=bool(o.get("skip_existing", True)),
                        # 有平台 Cookie 时只把“任务私有快照”交给 yt-dlp。
                        # 主 cookies_from_profile.txt 永远不让并发 yt-dlp 直接回写。
                        cookies_file=snap or None,
                        cookies_from_browser=(None if source_cookie else
                                              (o.get("cookies_from_browser") or None)),
                        rate_limit=o.get("rate_limit", ""),
                        format_id=o.get("format_id", ""),
                        on_progress=on_progress,
                    )
                finally:
                    _cleanup_cookie_snapshot(snap)

            _pace_platform(t.video_url)

            # 主 Cookie 文件若刚好处于旧版本留下的损坏状态，先刷新一次再跑。
            pre = _snapshot_cookie_file(source_cookie, task_id + "-precheck") if source_cookie else ""
            if source_cookie and not pre:
                ok_refresh, why = _refresh_platform_cookie(t.video_url)
                if not ok_refresh:
                    retry_note = f"Cookie 文件不可用，自动刷新失败：{why}"
            _cleanup_cookie_snapshot(pre)

            res = _download_once()

            # 抖音长批次常在数百条后要求 fresh cookies / 返回 412。
            # 此时集中刷新一次 profile，再用新快照重试当前任务；不要让后面几百条
            # 继续拿同一份过期 Cookie 逐条失败。
            if not res.get("ok") and _cookie_auth_error(str(res.get("error") or "")):
                ok_refresh, why = _refresh_platform_cookie(t.video_url)
                if ok_refresh:
                    time.sleep(1.2)
                    _pace_platform(t.video_url)
                    retry = _download_once()
                    retry_note = why + "，当前任务已自动重试一次"
                    res = retry
                else:
                    retry_note = f"检测到平台登录态/风控异常；自动刷新失败：{why}"

            with self._lock:
                t.files = [{"name": f["name"], "path": f["path"], "size": f["size"]} for f in res.get("files", [])]
                # 兜底说明（如「已自动改用 mweb 客户端，清晰度受限」）必须往上传，不许静默降级
                base_note = str(res.get("note") or "")
                t.note = "；".join(x for x in (base_note, retry_note) if x)
                t.done = len(t.files)
                # 跳过也要计数，否则界面永远是「跳过 0」：
                # 用户下 2 个只出 1 个文件，统计显示 0 跳过，就会以为程序出错了
                # （2026-10-25 实测：台账/占位名残留导致跳过，但统计看不出来）。
                t.skipped = 1 if res.get("skipped") else 0
                t.total = max(t.total, t.done)
                t.percent = 100.0 if res.get("ok") else t.percent
                t.speed = 0.0
                if res.get("ok"):
                    t.status = ST_COMPLETED
                elif self._cancelled(task_id):
                    # 用户取消要把「取消」和「失败」分开：下载被取消时结果是
                    # ok=False，旧逻辑直接记 failed，用户以为下载出错了。
                    t.status = ST_CANCELLED
                    t.error = "已取消"
                else:
                    t.status = ST_FAILED
                    t.error = res.get("error") or "下载失败"
                t.finished_at = time.time()
        except DownloadCancelled:
            with self._lock:
                t.status = ST_CANCELLED
                t.error = "已取消"
                t.finished_at = time.time()
        except Exception as e:  # noqa: BLE001
            with self._lock:
                t.status = ST_FAILED
                t.error = f"{type(e).__name__}: {e}"
                t.finished_at = time.time()


    def _run_streams(self, task_id: str, workers: int | None = None):
        """流媒体下载：N 路并发拉取（设置页可调，默认 4），实时回报整体进度。

        为什么并发可调（2026-09-28 用户反馈「下载仍然不够快，带宽有 1~2M/s」）：
        Pinterest 短视频 HLS 单路实测只有 ~220KB/s（国际链路），远吃不满带宽；
        多路并发能线性提速。默认 4 路；设置页「流媒体下载并发数」可改。
        （大体积长视频仍建议并发调小 —— 那种场景单路就能占满带宽。）
        """
        if not workers:
            try:
                from . import store
                workers = int(store.load().get("stream_concurrency") or 4)
            except Exception:  # noqa: BLE001
                workers = 4
        workers = max(1, min(int(workers), 8))
        t = self.get(task_id)
        if not t:
            return
        with self._lock:
            t.status = ST_RUNNING
            t.started_at = time.time()

        ev = self._cancel_flags.get(task_id)
        n = max(1, len(t.urls))
        counters = {"ok": 0, "fail": 0, "skip": 0, "done": 0}
        # 流下载并发同样交给闸门：workers 只是上限，实际同时跑几个由平台反馈决定。
        _first = str((t.urls[0] or {}).get("url") or "") if t.urls else ""
        gate = GOVERNOR.gate_for_url(_first, KIND_STREAM_DOWNLOAD, ceiling=workers)
        import queue as _queue

        q: _queue.Queue = _queue.Queue()
        for i, it in enumerate(t.urls):
            q.put((i, it))

        def _bump():
            with self._lock:
                t.done, t.failed, t.skipped = (counters["ok"], counters["fail"],
                                               counters["skip"])
                t.percent = round(counters["done"] * 100.0 / n, 1)

        def _worker():
            while True:
                try:
                    i, it = q.get_nowait()
                except _queue.Empty:
                    return
                if ev and ev.is_set():
                    continue          # 取消：跳过剩余（不计失败）
                url = str(it.get("url") or "")
                if not url:
                    continue
                # 名额满了就在这里等 —— 等的时候也要能响应取消
                if not gate.acquire(cancelled=lambda: bool(ev and ev.is_set())):
                    continue
                signal = None
                try:
                    name = url.split("?")[0].rsplit("/", 1)[-1] or url
                    with self._lock:
                        t.current = name[:60]

                    def on_progress(info: dict):
                        with self._lock:
                            t.speed = max(t.speed or 0.0, float(info.get("speed") or 0))

                    try:
                        res = download_stream(
                            url,
                            Path(t.output_dir),
                            referer=str(it.get("referer") or ""),
                            title=str(it.get("title") or ""),
                            skip_existing=bool(t.options.get("skip_existing", True)),
                            on_progress=on_progress,
                            cancel_event=ev,
                            prefer_quality=str(it.get("prefer_quality")
                                               or t.options.get("prefer_quality", "") or ""),
                        )
                    except Exception as e:  # noqa: BLE001
                        res = DownloadResult(False, error=f"{type(e).__name__}: {e}", url=url)

                    if res.ok:
                        signal = OK
                    elif str(res.error or "") != "已取消":
                        signal = classify_error(res.error)

                    with self._lock:
                        if res.ok and res.skipped:
                            counters["skip"] += 1
                        elif res.ok:
                            counters["ok"] += 1
                            t.files.append({
                                "name": Path(res.path).name,
                                "path": res.path,
                                "size": res.size,
                            })
                        elif res.error == "已取消":
                            pass          # 取消导致的未完成不计入失败
                        else:
                            counters["fail"] += 1
                        counters["done"] += 1
                        t.items.append({
                            "index": i,
                            "url": url,
                            "ok": res.ok,
                            "skipped": res.skipped,
                            "name": Path(res.path).name if res.path else "",
                            "size": res.size,
                            "error": res.error,
                        })
                        _bump()
                finally:
                    gate.release(signal)

        threads = [threading.Thread(target=_worker, daemon=True)
                   for _ in range(max(1, min(gate.ceiling, len(t.urls) or 1)))]
        for th in threads:
            th.start()
        for th in threads:
            th.join()

        with self._lock:
            t.speed = 0.0
            if self._cancelled(task_id):
                t.status = ST_CANCELLED
                t.error = "已取消"
            elif counters["ok"] + counters["skip"] == 0 and counters["fail"] > 0:
                t.status = ST_FAILED
                t.error = ("全部流下载失败：可能该地址需要登录 / 有防盗链，"
                           "或 ffmpeg 不可用（加密流必须依赖 ffmpeg）。")
            else:
                t.status = ST_COMPLETED
                t.percent = 100.0
            t.finished_at = time.time()


def _classify_failures(errors: list[str]) -> str:
    """把一批下载错误归类成人能看懂的原因。

    原来固定提示「全部图片下载失败，请检查链接是否失效或需要登录」，
    但真实原因差别很大（网络波动 / 防盗链 / 资源已删），一律推到「链接失效」
    会让用户白折腾 —— 实测本机最常见的是代理导致的 SSL 中断，**重试就能好**。
    """
    errors = [e for e in (errors or []) if e]
    if not errors:
        return "全部下载失败。"
    up = [e.upper() for e in errors]
    n = len(errors)
    ssl_n = sum(1 for e in up if "SSL" in e)
    net_n = sum(1 for e in up if ("网络错误" in e or "超时" in e or "请求失败" in e))
    denied = sum(1 for e in errors if e.startswith("HTTP 403"))
    gone = sum(1 for e in errors if e.startswith("HTTP 404"))

    if ssl_n + net_n >= n:
        return ("全部下载失败：网络层出错（SSL 中断 / 超时）。"
                "这类多是代理或出口波动导致的瞬时问题，稍后重试通常即可恢复。")
    if denied >= n:
        return ("全部下载失败：站点返回 403 拒绝访问。常见于两种情况 —— "
                "①防盗链（需要来源页）：可在设置里配置该站 Cookies 后重试；"
                "②这些地址其实是「封面 / 预览」类资源。很多站点把视频封面也做成 .mp4 地址"
                "并放在图片 CDN 上（如 .../videos/.../original_xxx.mp4），"
                "这类资源本身就不可下载，换成同一页面里其它视频条目即可。")
    if gone >= n:
        return "全部下载失败：链接已失效（站点返回 404），原资源可能已被删除。"

    parts = []
    if ssl_n + net_n:
        parts.append(f"网络问题 {ssl_n + net_n} 个")
    if denied:
        parts.append(f"被拒绝 403 {denied} 个")
    if gone:
        parts.append(f"链接失效 404 {gone} 个")
    other = n - (ssl_n + net_n + denied + gone)
    if other > 0:
        parts.append(f"其它 {other} 个")
    return f"全部下载失败（{'，'.join(parts)}）。示例：{errors[0][:70]}"


def _new_id() -> str:    return uuid.uuid4().hex[:12]


def _resolve_output(subdir: str) -> Path:
    """把用户给的子目录名安全地拼到下载根目录下（拒绝路径穿越）。"""
    name = sanitize_subdir(subdir)
    p = (DOWNLOADS_DIR / name) if name else DOWNLOADS_DIR
    p.mkdir(parents=True, exist_ok=True)
    return p


def sanitize_subdir(name: str) -> str:
    """清理子目录名：去掉非法字符、拒绝 .. 与绝对路径。"""
    if not name:
        return ""
    s = str(name).strip().replace("\\", "/")
    parts = [p for p in s.split("/") if p and p not in (".", "..")]
    cleaned = []
    for p in parts[:3]:                       # 最多三层
        p = "".join(c for c in p if c not in '<>:"|?*').strip()
        p = p[:60]
        if p and not p.startswith(("/", "\\")):
            cleaned.append(p)
    return "/".join(cleaned)


# 全局单例
manager = TaskManager()
