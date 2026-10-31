# -*- coding: utf-8 -*-
"""多连接分段下载：专治「单连接被 CDN 限速」的大文件。

## 为什么需要

2026-10-29 实测 pornhao 的视频（挂在 pornocdn 上）：

| 方式 | 速度 |
| --- | --- |
| 单连接第一次 | 107 KB/s |
| 单连接第二次（同一地址） | **28 KB/s** |
| 8 路 Range 并发 | **461 KB/s** |

本机带宽上限约 380 KB/s（Cloudflare 测速），也就是说 8 路并发已经打满带宽，
而单连接只用了不到 1/10。一部 192MB 的片子：单连接一小时以上（用户看到的现象
就是「点了下载没反应」），分段 8 分钟内下完。

这不是某一个站的特例 —— 大量中小站 / 图床 / 廉价 CDN 都按**连接**限速，
单连接的下载器在这类站上天然吃亏。所以这里做的是通用能力，不写任何站点规则。

## 设计取舍

1. **先探测再分段**：`Range: bytes=0-0` 探一次，只有返回 206 且能读出总长度
   才走分段；不支持 Range 的站原样回退单连接，行为不变。
2. **每片一个分片文件**（`.parts/pNNN`），分片文件大小本身就是进度，
   中断后按大小续传，不需要额外的状态文件（不会出现「状态文件与分片不一致」）。
3. **合并边读边删**：磁盘峰值 ≈ 文件大小 + 一个分片，不会翻倍。
4. 任何一片重试耗尽 → 返回失败并**保留已完成分片**，上层可回退单连接；
   下次同一个 URL 再下时，已完成的分片直接跳过。
5. 只接受 206。服务器若忽略 Range 返回 200，说明分段在这条链路上不可靠，
   直接判定失败让上层走单连接，绝不把整份内容错写进某个分片。
"""
from __future__ import annotations

import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import requests

from .config import HEADERS, TIMEOUT

# 小于这个体积不值得开多连接：探测 + 合并的开销可能比省下的时间还多
MIN_SIZE = 3 * 1024 * 1024
# 每片至少 1MB，避免把 4MB 文件切成 20 片
MIN_PART = 1024 * 1024
# 单片大小：片越小，慢片拖累越小、动态调度越灵活；太小则请求开销变大。
PART_SIZE = 4 * 1024 * 1024
# 默认并发连接数。6 路在实测里已能打满普通家庭宽带；再多容易触发站侧风控。
DEFAULT_CONNECTIONS = 6
# 单连接停摆判定（与 downloader 单连接路径保持一致）
STALL_SEC = 60


@dataclass
class MultipartResult:
    ok: bool
    size: int = 0
    total: int = 0
    error: str = ""
    connections: int = 0

    @property
    def used(self) -> bool:
        return self.connections > 1


def probe_range(session: requests.Session, url: str, headers: dict,
                timeout=TIMEOUT) -> tuple[bool, int]:
    """探测服务器是否支持分段，以及资源总长度。返回 (支持, 总长)。"""
    h = dict(headers)
    h["Range"] = "bytes=0-0"
    try:
        r = session.get(url, headers=h, timeout=timeout, stream=True, allow_redirects=True)
    except requests.RequestException:
        return False, 0
    try:
        if r.status_code != 206:
            return False, 0
        total = 0
        cr = r.headers.get("Content-Range") or ""
        if "/" in cr:
            tail = cr.rsplit("/", 1)[-1].strip()
            if tail.isdigit():
                total = int(tail)
        return (total > 0), total
    finally:
        try:
            r.close()
        except Exception:
            pass


def download_multipart(
    url: str,
    tmp: Path,
    *,
    headers: dict | None = None,
    session: requests.Session | None = None,
    connections: int = DEFAULT_CONNECTIONS,
    timeout=TIMEOUT,
    max_retries: int = 3,
    chunk_size: int = 256 * 1024,
    on_progress=None,
    min_size: int = MIN_SIZE,
    max_total_sec: int = 3600,
    should_cancel=None,
) -> MultipartResult | None:
    """尝试分段并发下载到 tmp。

    返回 None 表示「这个地址不适合分段」（不支持 Range / 文件太小），
    调用方应原样走单连接逻辑。
    """
    base_headers = dict(headers or HEADERS)
    own_session = session is None
    sess = session or requests.Session()
    try:
        supports, total = probe_range(sess, url, base_headers, timeout)
        if not supports or total < min_size:
            return None

        n = max(1, min(int(connections or DEFAULT_CONNECTIONS), total // MIN_PART or 1))
        if n < 2:
            return None

        # 总时长预算：慢得出奇的链路（每 20 秒才挪 1KB）如果允许无限重试，
        # 任务会永远停在「下载中」。到点就返回失败并保留分片 —— 用户再点一次
        # 是断点续传，不是从 0 开始。
        deadline = time.time() + max(60, int(max_total_sec or 3600))

        parts_dir = tmp.parent / (tmp.name + ".parts")
        parts_dir.mkdir(parents=True, exist_ok=True)

        # ---- 切片：固定 4MB 一片，片数远多于连接数，worker 动态领取 ----
        # 为什么不用「按连接数等分」：实测 4 等分时，先下完的 3 片就空转，
        # 剩 1 片以 20 KB/s 慢慢爬，整条下载速度掉到 20 KB/s（前 30% 有 800 KB/s）。
        # 小片 + 动态领取能把慢片的影响摊薄到 4MB 以内，谁快谁多干。
        nparts = (total + PART_SIZE - 1) // PART_SIZE
        if nparts < 2:
            return None
        ranges: list[tuple[int, int, int]] = []
        for i in range(nparts):
            start = i * PART_SIZE
            end = min(start + PART_SIZE, total) - 1
            ranges.append((i, start, end))
        n = max(2, min(int(connections or DEFAULT_CONNECTIONS), nparts))

        done_map: dict[int, int] = {}
        for i, start, end in ranges:
            pf = parts_dir / f"p{i:03d}"
            want = end - start + 1
            have = pf.stat().st_size if pf.exists() else 0
            done_map[i] = min(have, want)

        lock = threading.Lock()
        errors: dict[int, str] = {}
        # 只用于「服务器忽略 Range」这种致命且全局的失败；普通超时不置位，
        # 否则一片抖动就会把其它已经在下传的分片一起拖死。
        fatal = threading.Event()
        # 用户点「取消」：分片循环里每轮都要看一眼，不能等下完才响应
        cancelled = threading.Event()

        def _user_cancelled() -> bool:
            if cancelled.is_set():
                return True
            if should_cancel:
                try:
                    if should_cancel():
                        cancelled.set()
                        return True
                except Exception:
                    pass
            return False

        state = {"t": time.time(), "b": sum(done_map.values())}

        # 超时拆成 (连接, 读) 两段：卡住的连接越早发现越好 ——
        # 重试是从分片断点续传的，掐掉重连的代价只是重发一个 Range 请求。
        if isinstance(timeout, tuple):
            tmo = timeout
        else:
            base = int(timeout or TIMEOUT)
            tmo = (min(base, 10), min(base, 20))

        def report(force: bool = False):
            if not on_progress:
                return
            now = time.time()
            if not force and (now - state["t"]) < 0.3:
                return
            with lock:
                done = sum(done_map.values())
            speed = 0.0
            if now - state["t"] > 0.001:
                speed = max(0.0, (done - state["b"]) / (now - state["t"]))
            state["t"], state["b"] = now, done
            on_progress({
                "status": "downloading",
                "progressed_bytes": done,
                "total_bytes": total,
                "percent": round(done * 100 / total, 1) if total else 0.0,
                "speed": speed,
                "connections": n,
            })

        def worker(i: int, start: int, end: int):
            """下一个分片。

            重试策略（踩过的坑）：**一片超时不能拖停其它片**。
            第一版在任意一片失败时 set 全局 stop，结果「6 路里 1 路 ReadTimeout
            → 全部退出 → 整片下载失败」，而这恰恰是最常见的失败形态（CDN 掐单条
            连接）。现在只有「服务器忽略 Range（200）」这种**致命且全局**的情况
            才停全局；普通超时/断流只重试自己，并且**只要这一轮写进去了字节就
            重置重试计数** —— 慢速链路上每轮推进一点，最终也能下完。
            """
            pf = parts_dir / f"p{i:03d}"
            want = end - start + 1
            attempts = 0
            hard_limit = max(4, max_retries * 3)
            last_rep = 0.0
            while attempts < hard_limit and not fatal.is_set():
                if time.time() > deadline:
                    errors[i] = "超出本次下载时长预算"
                    return
                have = pf.stat().st_size if pf.exists() else 0
                if have >= want:
                    with lock:
                        done_map[i] = want
                    return
                attempts += 1
                h = dict(base_headers)
                h["Range"] = f"bytes={start + have}-{end}"
                try:
                    r = sess.get(url, headers=h, timeout=tmo, stream=True,
                                 allow_redirects=True)
                    if r.status_code != 206:
                        # 200 = 服务器忽略了 Range。宁可不分段，也不能把整份内容
                        # 错写进一个分片（那会得到一部「能播但内容错位」的片子）。
                        errors[i] = f"HTTP {r.status_code}"
                        r.close()
                        fatal.set()
                        return
                    written = have
                    last_byte = time.time()
                    with open(pf, "ab") as fh:
                        for chunk in r.iter_content(chunk_size=chunk_size):
                            if not chunk:
                                continue
                            fh.write(chunk)
                            written += len(chunk)
                            last_byte = time.time()
                            now = time.time()
                            if now - last_rep > 0.25:
                                with lock:
                                    done_map[i] = min(written, want)
                                report()
                                last_rep = now
                            if now - last_byte > STALL_SEC:
                                raise requests.exceptions.ConnectionError(
                                    f"连接停摆（{STALL_SEC} 秒未收到数据）")
                            if now > deadline:
                                raise requests.exceptions.ConnectionError("超出本次下载时长预算")
                            if _user_cancelled():
                                raise requests.exceptions.ConnectionError("已取消")
                    r.close()
                    written = pf.stat().st_size
                    with lock:
                        done_map[i] = min(written, want)
                    if written >= want:
                        report(force=True)
                        return
                    errors[i] = "分片不完整"
                except Exception as e:  # noqa: BLE001
                    if _user_cancelled() or "已取消" in str(e):
                        return
                    errors[i] = f"{type(e).__name__}"
                    # 这一轮有推进就不算「白试」：慢速链路下允许更多轮次
                    try:
                        if pf.exists() and pf.stat().st_size > have:
                            attempts = max(0, attempts - 1)
                    except OSError:
                        pass
                    time.sleep(min(0.5 * attempts, 3.0))
            if not fatal.is_set():
                errors[i] = errors.get(i) or "重试耗尽"

        cursor = [0]

        def runner():
            """领取下一个未完成分片并下载，直到分片发完。"""
            while not fatal.is_set():
                if _user_cancelled():
                    return
                with lock:
                    if cursor[0] >= len(ranges):
                        return
                    idx = cursor[0]
                    cursor[0] += 1
                if time.time() > deadline:
                    return
                worker(*ranges[idx])

        threads = [threading.Thread(target=runner, daemon=True) for _ in range(n)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()

        with lock:
            ok_all = all(done_map[i] >= (e - s + 1) for i, s, e in ranges)
        if _user_cancelled():
            # 保留已完成分片：用户之后再下同一条会从断点续传
            return MultipartResult(False, error="已取消", total=total, connections=n)
        if not ok_all:
            err = next((v for v in errors.values() if v), "未知")
            return MultipartResult(False, error=f"分段下载失败（{err}）",
                                   total=total, connections=n)

        # ---- 合并：边读边删，磁盘峰值 ≈ 文件大小 + 一个分片 ----
        tmp.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, "wb") as out:
            for i, start, end in ranges:
                pf = parts_dir / f"p{i:03d}"
                with open(pf, "rb") as pfh:
                    shutil.copyfileobj(pfh, out, 1024 * 1024)
                try:
                    pf.unlink()
                except OSError:
                    pass
        try:
            parts_dir.rmdir()
        except OSError:
            pass

        size = tmp.stat().st_size
        if size != total:
            return MultipartResult(False, error=f"合并后大小不符（{size}/{total}）",
                                   total=total, connections=n)
        report(force=True)
        return MultipartResult(True, size=size, total=total, connections=n)
    finally:
        if own_session:
            try:
                sess.close()
            except Exception:
                pass
