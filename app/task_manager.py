# -*- coding: utf-8 -*-
"""下载任务队列：统一管理图片与视频下载任务的状态、进度、取消。

状态机：queued -> running -> (completed | failed | cancelled)
线程模型：每个任务一个工作线程，任务内部的图片并发由 downloader 控制。
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .config import DOWNLOADS_DIR
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


_VIDEO_GATE = _AdjustableGate(3)


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

    def public(self) -> dict:
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
            "items": self.items[-200:],   # 只回传最近明细，避免响应过大
            "output_dir": self.output_dir,
        }


class TaskManager:
    def __init__(self, max_tasks: int = 200):
        self._tasks: dict[str, Task] = {}
        self._order: list[str] = []          # 新任务在前
        self._lock = threading.RLock()
        self._max_tasks = max_tasks
        self._cancel_flags: dict[str, threading.Event] = {}

    # ---------- 基础操作 ----------

    def get(self, task_id: str) -> Task | None:
        with self._lock:
            return self._tasks.get(task_id)

    def list(self, limit: int = 50) -> list[dict]:
        with self._lock:
            ids = self._order[:limit]
            return [self._tasks[i].public() for i in ids if i in self._tasks]

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

    # ---------- 创建任务 ----------

    def submit_images(
        self,
        urls: list[dict],
        output_subdir: str = "images",
        title: str = "",
        concurrency: int = 6,
        skip_existing: bool = True,
        prefer_quality: str = "",
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
                     "prefer_quality": prefer_quality},
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
        threading.Thread(target=self._run_video, args=(task.id,), daemon=True).start()
        return task.id

    def set_video_concurrency(self, n: int) -> int:
        """设置「同时跑几个 yt-dlp」，返回生效后的值。"""
        return _VIDEO_GATE.set_capacity(n)

    def video_concurrency(self) -> int:
        return _VIDEO_GATE.capacity

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
        由 concurrency 限制（见 _AdjustableGate）——
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

        串行执行：ffmpeg 拉流本身就能占满带宽，并发只会互相拖慢，
        而且多数站点对同 IP 的多路拉流有限流。
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

        def on_progress(index: int, info: dict):
            with self._lock:
                t.speed = info.get("speed") or t.speed

        try:
            res = batch_download_images(
                t.urls,
                Path(t.output_dir),
                max_workers=int(t.options.get("concurrency", 6)),
                skip_existing=bool(t.options.get("skip_existing", True)),
                prefer_quality=str(t.options.get("prefer_quality", "") or ""),
                on_item_done=on_item_done,
                on_progress=on_progress,
                should_cancel=lambda: self._cancelled(task_id),
            )
            with self._lock:
                # 以批量结果为准修正计数（回调与批量统计可能因异常有偏差）
                t.done = res["ok_count"]
                t.failed = res["fail_count"]
                t.skipped = res["skipped_count"]
                t.percent = 100.0
                t.speed = 0.0
                if res.get("cancelled") or self._cancelled(task_id):
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

        # 先过闸门：名额满了就停在 queued 状态等，别让几十个 yt-dlp 同时抢带宽
        with self._lock:
            t.current = f"排队中…（同时最多 {_VIDEO_GATE.capacity} 个视频）"
        got = _VIDEO_GATE.acquire(cancelled=lambda: self._cancelled(task_id))
        if not got:
            with self._lock:
                t.status = ST_CANCELLED
                t.error = "已取消"
                t.finished_at = time.time()
            return
        try:
            self._run_video_inner(task_id)
        finally:
            _VIDEO_GATE.release()

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
                t.percent = info.get("percent") or t.percent
                t.speed = info.get("speed") or 0
                if info.get("filename"):
                    t.current = info["filename"][:80]
                st = info.get("status")
                if st == "downloading":
                    done = info.get("progressed_bytes") or 0
                    total = info.get("total_bytes") or 0
                    t.done = 1 if info.get("percent", 0) >= 99.9 else 0
                    if total:
                        t.total = 1

        try:
            o = t.options
            res = download_video(
                t.video_url,
                Path(t.output_dir),
                task_id=task_id,
                quality=o.get("quality", "best"),
                container=o.get("container", ""),
                prefer_audio_only=bool(o.get("prefer_audio_only")),
                write_subtitles=bool(o.get("write_subtitles")),
                subtitle_langs=o.get("subtitle_langs", "zh.*,en"),
                include_playlist=bool(o.get("include_playlist")),
                cookies_file=o.get("cookies_file") or None,
                cookies_from_browser=o.get("cookies_from_browser") or None,
                rate_limit=o.get("rate_limit", ""),
                format_id=o.get("format_id", ""),
                on_progress=on_progress,
            )
            with self._lock:
                t.files = [{"name": f["name"], "path": f["path"], "size": f["size"]} for f in res.get("files", [])]
                # 兜底说明（如「已自动改用 mweb 客户端，清晰度受限」）必须往上传，不许静默降级
                t.note = str(res.get("note") or "")
                t.done = len(t.files)
                t.total = max(t.total, t.done)
                t.percent = 100.0 if res.get("ok") else t.percent
                t.speed = 0.0
                if res.get("ok"):
                    t.status = ST_COMPLETED
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

        threads = [threading.Thread(target=_worker, daemon=True)
                   for _ in range(max(1, min(workers, len(t.urls) or 1)))]
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
