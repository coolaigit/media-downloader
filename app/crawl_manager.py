# -*- coding: utf-8 -*-
"""批量抓取任务管理：把耗时的同步抓取放到后台线程，前端轮询进度。

为什么不直接在请求里跑：一次批量抓取（列表页 + 渲染 + 下钻详情页）
实测动辄 30~300 秒，同步端点会把浏览器请求挂死，也无法展示进度、
无法取消。因此统一走「提交任务 → 轮询进度 → 取结果」三段式。

线程模型：每个任务一个工作线程；渲染层内部自带并发，
抓取并发度由 crawl_list 的 detail_concurrency 控制。
"""
from __future__ import annotations

import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field

from .batch_crawler import crawl_list, crawl_site

ST_QUEUED = "queued"
ST_RUNNING = "running"
ST_COMPLETED = "completed"
ST_FAILED = "failed"
ST_CANCELLED = "cancelled"

TERMINAL = {ST_COMPLETED, ST_FAILED, ST_CANCELLED}

# 抓取阶段的中文名，用于前端进度文案
STAGE_LABEL = {
    "list": "抓取列表页",
    "list-error": "列表页失败",
    "detail": "下钻详情页",
    "merge": "汇总去重",
    "done": "抓取完成",
    "cancelled": "已取消",
    "failed": "抓取失败",
}


@dataclass
class CrawlJob:
    id: str
    url: str
    params: dict = field(default_factory=dict)
    status: str = ST_QUEUED
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None

    # 进度
    stage: str = ""
    stage_done: int = 0
    stage_total: int = 0
    note: str = ""
    media_found: int = 0

    # 结果
    result: dict | None = None
    error: str = ""

    def public(self, with_result: bool = False) -> dict:
        out = {
            "id": self.id,
            "url": self.url,
            "status": self.status,
            "mode": self.params.get("mode") or "list",
            "params": self.params,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "stage": self.stage,
            "stage_label": STAGE_LABEL.get(self.stage, self.stage),
            "stage_done": self.stage_done,
            "stage_total": self.stage_total,
            "note": self.note,
            "media_found": self.media_found,
            "error": self.error,
            "elapsed": round((self.finished_at or time.time())
                             - self.started_at, 1) if self.started_at else 0.0,
            "has_result": self.result is not None,
            "summary": (self.result or {}).get("summary") if self.result else None,
            "warnings": (self.result or {}).get("warnings") if self.result else None,
            "stats": (self.result or {}).get("stats") if self.result else None,
            "pages": (self.result or {}).get("pages") if self.result else None,
            "detail_pages": (self.result or {}).get("detail_pages") if self.result else None,
        }
        if with_result:
            out["items"] = (self.result or {}).get("items") or []
        return out


class CrawlManager:
    def __init__(self, max_jobs: int = 60):
        self._jobs: dict[str, CrawlJob] = {}
        self._order: list[str] = []          # 新任务在前
        self._lock = threading.RLock()
        self._cancel_flags: dict[str, threading.Event] = {}
        self._max_jobs = max_jobs

    # ---------- 基础操作 ----------

    def get(self, job_id: str) -> CrawlJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self, limit: int = 20) -> list[dict]:
        with self._lock:
            return [self._jobs[i].public() for i in self._order[:limit]
                    if i in self._jobs]

    def _prune(self):
        """超出上限时清理最旧的已结束任务（进行中的不动）。"""
        if len(self._order) <= self._max_jobs:
            return
        for jid in reversed(self._order):
            j = self._jobs.get(jid)
            if j and j.status in TERMINAL:
                self._order.remove(jid)
                self._jobs.pop(jid, None)
                self._cancel_flags.pop(jid, None)
            if len(self._order) <= self._max_jobs:
                break

    def cancel(self, job_id: str) -> bool:
        """请求取消：置标志位，工作线程在检查点自行退出。"""
        with self._lock:
            job = self._jobs.get(job_id)
            if not job or job.status in TERMINAL:
                return False
            ev = self._cancel_flags.get(job_id)
            if ev:
                ev.set()
            job.note = "正在取消…"
            return True

    # ---------- 启动任务 ----------

    def start(self, url: str, params: dict | None = None) -> str:
        """提交一个批量抓取任务，立即返回 job_id。"""
        params = dict(params or {})
        jid = uuid.uuid4().hex[:12]
        job = CrawlJob(id=jid, url=url, params=params)
        ev = threading.Event()
        with self._lock:
            self._jobs[jid] = job
            self._order.insert(0, jid)
            self._cancel_flags[jid] = ev
            self._prune()

        t = threading.Thread(target=self._run, args=(jid, ev), daemon=True,
                             name=f"crawl-{jid}")
        t.start()
        return jid

    def _run(self, jid: str, ev: threading.Event):
        job = self._jobs.get(jid)
        if not job:
            return
        job.status = ST_RUNNING
        job.started_at = time.time()
        p = job.params

        def on_progress(info: dict):
            """crawl_list 的阶段回调（列表页/详情页/汇总）。"""
            job.stage = info.get("stage") or job.stage
            job.stage_done = int(info.get("done") or 0)
            job.stage_total = int(info.get("total") or 0)
            job.note = info.get("note") or ""
            job.media_found = int(info.get("media") or 0)

        try:
            mode = str(p.get("mode") or "list")
            common = dict(
                allow_render=bool(p.get("allow_render", True)),
                want_kinds=tuple(p.get("want_kinds") or ("image", "video", "stream")),
                originals_only=bool(p.get("originals_only", False)),
                on_progress=on_progress,
                should_cancel=ev.is_set,
            )
            if mode == "site":
                # 全站抓取：把「一页」换成「整个站点」
                res = crawl_site(
                    job.url,
                    max_pages=int(p.get("max_pages") or 30),
                    max_depth=int(p.get("max_depth") or 2),
                    same_host=bool(p.get("same_host", True)),
                    use_sitemap=bool(p.get("use_sitemap", True)),
                    concurrency=int(p.get("detail_concurrency") or 4),
                    **common,
                )
            else:
                res = crawl_list(
                    job.url,
                    max_pages=int(p.get("max_pages") or 1),
                    max_items=int(p.get("max_items") or 0),
                    drill_down=bool(p.get("drill_down", True)),
                    drill_mode=str(p.get("drill_mode") or "auto"),
                    detail_concurrency=int(p.get("detail_concurrency") or 4),
                    # 要视频时先把页面交给 yt-dlp 判断「是不是视频列表（博主页/频道）」
                    expand_listing=bool(p.get("expand_listing", True)),
                    cookies_file=p.get("cookies_file") or None,
                    cookies_from_browser=p.get("cookies_from_browser") or None,
                    **common,
                )
            job.result = res
            job.media_found = len(res.get("items") or [])
            job.status = ST_CANCELLED if ev.is_set() else ST_COMPLETED
            if ev.is_set():
                job.note = "已取消（已抓到的结果仍可查看）"
        except Exception as e:  # noqa: BLE001
            job.status = ST_FAILED
            job.error = f"{type(e).__name__}: {e}"
            job.result = {"items": [], "summary": {"total": 0},
                          "warnings": [f"抓取异常：{job.error}"],
                          "stats": {}, "pages": [], "detail_pages": {},
                          "trace": traceback.format_exc()[-1500:]}
        finally:
            job.finished_at = time.time()
            # 终态必须换成明确的收尾阶段名。
            # 这里原来写的是 `job.stage = "merge" if 完成 else job.stage`，
            # 于是任务早已结束后，进度条仍停在「汇总去重」不动 ——
            # 用户看到的就是「去重完成后还显示去重」（反馈的 bug）。
            if job.status == ST_COMPLETED:
                job.stage = "done"
                job.note = ""
            elif job.status == ST_CANCELLED:
                job.stage = "cancelled"
            elif job.status == ST_FAILED:
                job.stage = "failed"


manager = CrawlManager()
