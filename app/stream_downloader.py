# -*- coding: utf-8 -*-
"""流媒体下载：把 m3u8 / mpd 播放列表抓成本地视频文件。

为什么必须单独做：
  大量视频站点（含各类成人站、部分商品站的商品视频）用 HLS 分发，
  页面里给出的是 .m3u8 地址。这类地址不是一个文件，而是一份「分片清单」——
  用普通 HTTP 下载只会拿到几 KB 的文本清单，看起来「下载成功」但实际毫无内容。
  必须把分片逐个拉齐再合并封装，才能得到可播放的视频。

实现选择（实测驱动）：
  主路径 = 内置 ffmpeg（`-c copy`，不转码 → 速度快、无损）。
  ffmpeg 原生支持 HLS/DASH：master playlist 选流、分片并发拉取、
  AES-128 解密、时间戳修复，全都由它处理，比自己实现分片下载稳得多。
  仅在没有 ffmpeg 时退化为自研分片下载（只适用于未加密的 TS 流）。

实测踩坑：
  - TS 分片里的 AAC 是 ADTS 格式，封进 MP4 必须加 `-bsf:a aac_adtstoasc`；
    但部分现代流已经是 fMP4/带 ASC，加了反而报错 → 因此按「带 bsf → 不带 bsf
    → 回落 TS 容器」三级降级，而不是写死一种。
  - ffmpeg 的进度必须用 `-progress pipe:1` 读，不能靠解析 stderr：
    stderr 是给人看的、行尾有 \\r 覆盖，解析不可靠。
"""
from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import requests

from .config import HEADERS, TIMEOUT, find_ffmpeg
from .downloader import DownloadResult, sanitize_filename, unique_path, url_fingerprint

# 单个分片下载的最大并发（自研兜底路径用）
SEG_CONCURRENCY = 8


@dataclass
class StreamInfo:
    url: str
    kind: str = ""                    # hls-master | hls-media | dash | unknown
    duration: float = 0.0
    variants: list[dict] = field(default_factory=list)
    segments: list[str] = field(default_factory=list)
    encrypted: bool = False
    error: str = ""


def _headers_for_ffmpeg(referer: str = "", user_agent: str = "") -> str:
    """构造 ffmpeg -headers 参数值。必须是真实 CRLF 分隔的 "Key: Value" 串。"""
    ua = user_agent or HEADERS.get("User-Agent") or ""
    lines = []
    if ua:
        lines.append(f"User-Agent: {ua}")
    if referer:
        lines.append(f"Referer: {referer}")
    lines.append("Accept: */*")
    return "\r\n".join(lines) + "\r\n"


def _abs(base: str, line: str) -> str:
    """把播放列表里的相对地址转成绝对地址。"""
    from urllib.parse import urljoin

    line = (line or "").strip()
    if not line:
        return ""
    if line.startswith("//"):
        return "https:" + line
    if line.startswith(("http://", "https://")):
        return line
    return urljoin(base, line)


def _get(url: str, headers: dict, timeout: int, attempts: int = 3):
    """带重试的 GET。

    必要性：实测本机走代理时，同一地址会间歇性报 ProxyError / SSLError
    （内联探测里紧挨着的两次请求，一次成功一次失败）。
    解析播放列表失败会让整个流下载直接放弃，因此对瞬时错误重试，
    与 batch_crawler.fetch_page 的处理保持一致。
    """
    transient = (requests.exceptions.SSLError, requests.exceptions.Timeout,
                 requests.exceptions.ConnectionError,
                 requests.exceptions.ChunkedEncodingError)
    last: Exception | None = None
    for i in range(1, max(1, attempts) + 1):
        try:
            return requests.get(url, headers=headers, timeout=timeout)
        except transient as e:
            last = e
            if i < attempts:
                time.sleep(0.8 * i)
    raise last if last else RuntimeError("请求失败")


def probe_stream(url: str, referer: str = "", timeout: int = TIMEOUT) -> StreamInfo:
    """解析 m3u8/mpd：识别类型、总时长、可选清晰度、分片数量。

    只做一次 HTTP 拉取（master 时再拉一次子列表），用于下载前预估与进度分母。
    """
    info = StreamInfo(url=url)
    headers = dict(HEADERS)
    if referer:
        headers["Referer"] = referer

    try:
        r = _get(url, headers, timeout)
    except requests.exceptions.RequestException as e:
        info.error = f"请求失败: {type(e).__name__}"
        return info

    if r.status_code >= 400:
        info.error = f"HTTP {r.status_code}"
        return info

    text = r.text or ""
    low_url = url.lower()

    # ---------- DASH ----------
    if ".mpd" in low_url or "<MPD" in text[:2000]:
        info.kind = "dash"
        # DASH 时长写在 mediaPresentationDuration="PT1H2M3.4S"
        m = re.search(r'mediaPresentationDuration="PT(?:(\d+)H)?(?:(\d+)M)?([\d.]+)S"', text)
        if m:
            h, mi, s = int(m.group(1) or 0), int(m.group(2) or 0), float(m.group(3) or 0)
            info.duration = h * 3600 + mi * 60 + s
        return info

    # ---------- HLS ----------
    if not text.lstrip().startswith("#EXTM3U"):
        info.kind = "unknown"
        info.error = "返回内容不是 HLS/DASH 播放列表"
        return info

    info.encrypted = "#EXT-X-KEY" in text

    if "#EXT-X-STREAM-INF" in text:
        info.kind = "hls-master"
        pending = None
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("#EXT-X-STREAM-INF"):
                bw = re.search(r"BANDWIDTH=(\d+)", line)
                res = re.search(r"RESOLUTION=(\d+x\d+)", line)
                pending = {
                    "bandwidth": int(bw.group(1)) if bw else 0,
                    "resolution": res.group(1) if res else "",
                }
            elif line and not line.startswith("#") and pending is not None:
                nu = _abs(url, line)
                if nu:
                    variant = dict(pending)
                    variant["url"] = nu
                    info.variants.append(variant)
                pending = None
        info.variants.sort(key=lambda v: -v.get("bandwidth", 0))
        # 顺手把最高码率那条的时长读出来，作为进度分母
        if info.variants:
            try:
                sub = _get(info.variants[0]["url"], headers, timeout)
                info.duration = _duration_from_media_playlist(sub.text or "")
                info.segments = _segments_from_media_playlist(sub.text or "",
                                                              info.variants[0]["url"])
            except requests.exceptions.RequestException:
                pass
        return info

    info.kind = "hls-media"
    info.duration = _duration_from_media_playlist(text)
    info.segments = _segments_from_media_playlist(text, url)
    return info


def _duration_from_media_playlist(text: str) -> float:
    """累加 #EXTINF 得到总时长（秒）。"""
    total = 0.0
    for m in re.finditer(r"#EXTINF:\s*([\d.]+)", text or ""):
        try:
            total += float(m.group(1))
        except ValueError:
            continue
    return round(total, 2)


def _segments_from_media_playlist(text: str, base: str) -> list[str]:
    """取出分片地址。"""
    out: list[str] = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith(("http://", "https://", "//", "/")) or "." in line.split("?")[0]:
            u = _abs(base, line)
            if u:
                out.append(u)
    return out


# ---------- ffmpeg 路径 ----------

def _ffmpeg_available() -> str:
    return find_ffmpeg() or ""


def _run_ffmpeg(cmd: list[str], duration: float, on_progress, cancel_event,
                work_dir: Path):
    """跑 ffmpeg 并解析 -progress 输出，返回 (ok, err, produced_size)。"""
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    # 诊断日志：设 MD_STREAM_DEBUG=1 时把进度行与输出文件大小写到临时目录。
    # 为什么需要（2026-11-04）：出现过「服务里 ffmpeg 报进度但 .part 一直 0 字节」，
    # 不落盘就看不出它到底在等什么。
    import tempfile
    _dbg = bool(os.environ.get("MD_STREAM_DEBUG"))
    _dbg_path = Path(tempfile.gettempdir()) / "md_stream_debug.log"
    _out_path = Path(cmd[-1]) if cmd else Path()

    def _dbg_log(msg: str) -> None:
        if not _dbg:
            return
        try:
            size = _out_path.stat().st_size if _out_path.exists() else -1
            with open(_dbg_path, "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%H:%M:%S')} size={size} {msg}\n")
        except Exception:  # noqa: BLE001
            pass
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
            creationflags=creationflags, cwd=str(work_dir),
        )
    except OSError as e:
        return False, f"无法启动 ffmpeg: {e}", 0
    _dbg_log(f"start pid={proc.pid} out={_out_path}")

    err_tail: list[str] = []

    # 父进程（本服务）一旦被杀/重启，ffmpeg 必须跟着死 —— 否则变孤儿：
    # 占着 .part 文件句柄和站点连接（实测累积 14 个孤儿，
    # 「有时成功有时失败有时没反应」的元凶）。Job Object：父死子灭。
    try:
        from .win_job import bind_to_current_process_job
        bind_to_current_process_job(proc.pid)
    except Exception:  # noqa: BLE001
        pass

    def _drain_err():
        try:
            for line in proc.stderr:  # type: ignore[union-attr]
                line = line.strip()
                if not line:
                    continue
                err_tail.append(line)
                if len(err_tail) > 40:
                    err_tail.pop(0)
        except Exception:
            pass

    t = threading.Thread(target=_drain_err, daemon=True)
    t.start()

    cancelled = False
    timed_out_reason = ""
    t0 = time.time()
    # ⚠️ 卡死保护（实测踩到）：网络停摆时 ffmpeg 不再输出任何 progress 行，
    # `for raw in proc.stdout` 会**永久阻塞**，worker 和整个下载任务一起挂死
    # （实测 2 条流卡了 3 个多小时、任务停在 running）。看门狗负责掐死：
    #   - 停摆：90 秒没有任何输出
    #   - 总时长：已知时长按 4 倍余量，未知最长 30 分钟
    STALL_SEC = 90
    total_cap = (duration * 4 + 180) if duration and duration > 0 else 1800.0
    last_output = time.time()
    # 进度看门狗：ffmpeg 在「连上了但分片拉不动」时仍会每 0.5 秒吐一行
    # `progress=continue`，只看「有没有输出」永远判不出卡死（2026-11-04 实测：
    # 会员换源下载停在 0 字节的 .part 上 3 分钟，进程活着、日志有输出）。
    # 因此额外看 out_time 有没有前进，120 秒不前进就掐掉换下一条封装。
    PROGRESS_STALL_SEC = 120
    last_progress = time.time()
    progress = {"sec": -1.0}
    # 输出文件大小看门狗：ffmpeg 在「连上了但一个字节都写不出来」时，
    # -progress 仍会每 0.5 秒吐一行（out_time 甚至可能前进），
    # 只看 stdout / out_time 都判不出来（2026-11-04 实测：0 字节 .part 挂了 18 分钟）。
    # 文件连续 120 秒没有增长就一律判卡死。
    def _out_size() -> int:
        try:
            return _out_path.stat().st_size if _out_path.exists() else 0
        except OSError:
            return 0
    growth = {"size": _out_size(), "t": time.time()}

    def _watchdog():
        nonlocal timed_out_reason
        while proc.poll() is None:
            if cancel_event is not None and cancel_event.is_set():
                return
            now = time.time()
            try:
                sz = _out_size()
            except Exception:  # noqa: BLE001
                sz = 0
            if sz > growth["size"]:
                growth["size"] = sz
                growth["t"] = now
            elif now - growth["t"] > PROGRESS_STALL_SEC:
                timed_out_reason = (f"输出文件 {PROGRESS_STALL_SEC} 秒没有增长"
                                    f"（{sz} 字节），判定卡死并中止")
                _dbg_log("watchdog: file not growing")
                try:
                    proc.kill()
                except Exception:
                    pass
                return
            if now - last_output > STALL_SEC:
                timed_out_reason = f"拉流停摆超 {STALL_SEC} 秒无数据（网络中断？），已中止本次下载"
                _dbg_log("watchdog: no output stall")
                try:
                    proc.kill()
                except Exception:
                    pass
                return
            if now - last_progress > PROGRESS_STALL_SEC:
                timed_out_reason = (f"拉流 {PROGRESS_STALL_SEC} 秒没有任何进度"
                                    f"（已下到 {progress['sec']:.0f}s / 共 {duration:.0f}s），已中止")
                _dbg_log("watchdog: no progress stall")
                try:
                    proc.kill()
                except Exception:
                    pass
                return
            if now - t0 > total_cap:
                timed_out_reason = f"单条流下载超过总时长上限（{int(total_cap)} 秒），已中止"
                _dbg_log("watchdog: total cap")
                try:
                    proc.kill()
                except Exception:
                    pass
                return
            time.sleep(2)

    wd = threading.Thread(target=_watchdog, daemon=True)
    wd.start()
    try:
        for raw in proc.stdout:  # type: ignore[union-attr]
            last_output = time.time()
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                try:
                    proc.terminate()
                except Exception:
                    pass
                break
            line = raw.strip()
            if not line or "=" not in line:
                continue
            k, _, v = line.partition("=")
            if k == "out_time_ms":          # 微秒
                try:
                    sec = int(v) / 1_000_000
                except ValueError:
                    continue
                if sec > progress["sec"] + 0.4:
                    progress["sec"] = sec
                    last_progress = time.time()
                    _dbg_log(f"progress {sec:.1f}s / {duration:.0f}s")
                if on_progress:
                    pct = round(sec * 100 / duration, 1) if duration > 0 else 0.0
                    on_progress({"status": "downloading", "percent": min(pct, 99.0),
                                 "progressed_bytes": 0, "total_bytes": 0,
                                 "speed": 0.0, "elapsed": round(time.time() - t0, 1)})
            elif k == "total_size":
                pass
            elif k == "progress" and v == "end":
                if on_progress:
                    on_progress({"status": "downloading", "percent": 100.0,
                                 "progressed_bytes": 0, "total_bytes": 0, "speed": 0.0})
        proc.wait(timeout=60)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    finally:
        try:
            t.join(timeout=2)
        except Exception:
            pass

    if cancelled:
        return False, "已取消", 0
    if timed_out_reason:
        try:
            proc.kill()
        except Exception:
            pass
        msg = " | ".join(err_tail[-2:])
        return False, (timed_out_reason + (f"（{msg}）" if msg else ""))[:300], 0
    if proc.returncode == 0:
        _dbg_log("exit rc=0")
        return True, "", 0
    msg = " | ".join(err_tail[-4:]) or f"ffmpeg 退出码 {proc.returncode}"
    _dbg_log(f"exit rc={proc.returncode} err={msg[:200]}")
    return False, msg[:400], 0


def _ffmpeg_download(url: str, target: Path, referer: str, duration: float,
                     on_progress, cancel_event, work_dir: Path,
                     limit_sec: int = 0) -> tuple[bool, str, Path]:
    """用 ffmpeg 下载并封装。三级降级：带 bsf → 不带 bsf → TS 容器。

    limit_sec > 0 时只下前 N 秒（诊断/验证链路用，不产出完整文件）。
    """
    ff = _ffmpeg_available()
    if not ff:
        return False, "未找到 ffmpeg"
    # 绝对路径：ffmpeg 以 cwd=work_dir 运行，相对输出路径会被二次拼接
    target = Path(target).resolve()
    work_dir = Path(work_dir).resolve()

    hdr = _headers_for_ffmpeg(referer)
    head = [ff, "-hide_banner", "-loglevel", "error", "-y",
            "-headers", hdr,
            # 断流重连：HLS 分片偶发 5xx 时不要让整个任务失败
            "-reconnect", "1", "-reconnect_streamed", "1",
            "-reconnect_delay_max", "10",
            # 读写超时（微秒）：死连接最多卡 30 秒就报错，交给下一条源，
            # 而不是让任务停在 0 字节的 .part 上（2026-11-04 实测踩到）。
            "-rw_timeout", "30000000"]
    tail = ["-i", url]
    limited = ["-t", str(int(limit_sec))] if limit_sec and limit_sec > 0 else []

    attempts = [
        (head + tail + ["-c", "copy", "-bsf:a", "aac_adtstoasc",
                        "-movflags", "+faststart", *limited,
                        "-progress", "pipe:1", "-nostats",
                        str(target)], target),
        (head + tail + ["-c", "copy", "-movflags", "+faststart", *limited,
                        "-progress", "pipe:1", "-nostats", str(target)], target),
        # 四级：资源站/盗链 CDN 常见证书链不完整，ffmpeg 的 CA 库不认但内容可下。
        # 这条只在上面都失败时使用，并且有 rw_timeout 兜底，不会无限挂。
        (head + ["-tls_verify", "0"] + tail + ["-c", "copy", "-movflags", "+faststart",
                                               *limited, "-progress", "pipe:1", "-nostats",
                                               str(target)], target),
    ]
    # 第三级：TS 容器（HLS 的原生封装，几乎不会失败；拿不到 mp4 时至少给个能播的）
    ts_target = target.with_suffix(".ts")
    attempts.append(
        (head + tail + ["-c", "copy", *limited, "-progress", "pipe:1", "-nostats",
                        str(ts_target)], ts_target)
    )

    last_err = ""
    for cmd, out in attempts:
        out.unlink(missing_ok=True)
        ok, err, _ = _run_ffmpeg(cmd, duration, on_progress, cancel_event, work_dir)
        if ok and out.exists() and out.stat().st_size > 1024:
            # 返回实际产出的文件：TS 回落时扩展名与最初的 .mp4 不同，
            # 调用方要按真实扩展名交付（把 TS 内容命名成 .mp4 会让播放器误判格式）
            return True, "", out
        if err == "已取消":
            out.unlink(missing_ok=True)
            return False, err, target
        last_err = err
        # bsf 失败以外的原因（如 403）重试其它封装没有意义，但成本很低，继续试
    return False, last_err or "ffmpeg 下载失败", target


# ---------- 自研兜底（无 ffmpeg） ----------

def _raw_download(url: str, info: StreamInfo, target: Path, referer: str,
                  on_progress, cancel_event) -> tuple[bool, str]:
    """没有 ffmpeg 时的兜底：并发拉分片 → 顺序拼接。

    只能处理未加密的 TS 流；加密流需要 key/IV 解密，这里明确拒绝而不是产出坏文件。
    """
    if info.encrypted:
        return False, "该流已加密（AES-128），需要 ffmpeg 才能解密合并"
    segs = info.segments
    if not segs:
        return False, "没有解析到分片地址"

    headers = dict(HEADERS)
    if referer:
        headers["Referer"] = referer

    from concurrent.futures import ThreadPoolExecutor, as_completed

    parts: list[bytes | None] = [None] * len(segs)
    done = {"n": 0}

    def _one(i: int, u: str):
        if cancel_event is not None and cancel_event.is_set():
            return i, None
        for attempt in range(1, 3):
            try:
                r = requests.get(u, headers=headers, timeout=TIMEOUT)
                if r.status_code < 400:
                    return i, r.content
            except requests.exceptions.RequestException:
                if attempt == 1:
                    time.sleep(0.5)
        return i, None

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=SEG_CONCURRENCY) as pool:
        futs = [pool.submit(_one, i, u) for i, u in enumerate(segs)]
        for fut in as_completed(futs):
            if cancel_event is not None and cancel_event.is_set():
                break
            try:
                i, data = fut.result()
            except Exception:
                continue
            parts[i] = data
            done["n"] += 1
            if on_progress:
                on_progress({
                    "status": "downloading",
                    "percent": round(done["n"] * 100 / len(segs), 1),
                    "progressed_bytes": 0, "total_bytes": 0,
                    "speed": 0.0, "elapsed": round(time.time() - t0, 1),
                })

    if cancel_event is not None and cancel_event.is_set():
        return False, "已取消"

    missing = [i for i, p in enumerate(parts) if p is None]
    if missing:
        return False, f"{len(missing)}/{len(segs)} 个分片下载失败"

    # 调用方传入的已经是 .part 路径，这里直接落盘，由调用方在成功后才改名
    with open(target, "wb") as f:
        for p in parts:
            f.write(p or b"")
    return True, ""


# ---------- 对外接口 ----------

def download_stream(
    url: str,
    output_dir: Path | str,
    referer: str = "",
    title: str = "",
    filename: str = "",
    skip_existing: bool = True,
    on_progress=None,
    cancel_event: threading.Event | None = None,
    prefer_quality: str = "",
) -> DownloadResult:
    """下载一个 m3u8/mpd 流到本地。

    产出优先 .mp4；ffmpeg 封装失败时回落为 .ts（仍可播放）。
    prefer_quality：master playlist 时的档位选择（highest / lowest / "720"）。
    空值 = 交给 ffmpeg 自动选（它本来就选最高档）。
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    # 统一成绝对路径：ffmpeg 以 cwd=output_dir 运行，相对路径会被二次拼接成
    # 「<output_dir>/<output_dir>/x.part.mp4」→ "No such file or directory"
    # （2026-11-04 实测踩到，调用方传相对目录时必错）。
    output_dir = output_dir.resolve()

    fp = url_fingerprint(url)
    marker = output_dir / f".{fp}.done"

    # 已下载过则跳过（与普通文件下载同一套指纹机制）
    if skip_existing and marker.exists():
        try:
            saved = marker.read_text(encoding="utf-8").strip()
        except Exception:
            saved = ""
        existing = (output_dir / saved) if saved else None
        if existing and existing.exists() and existing.stat().st_size > 4096:
            return DownloadResult(True, str(existing), existing.stat().st_size,
                                  skipped=True, url=url)

    info = probe_stream(url, referer=referer)
    if info.kind in ("", "unknown"):
        return DownloadResult(False, error=info.error or "无法识别该地址的流格式", url=url)

    if not filename:
        stem = sanitize_filename(title) if title else ""
        if not stem:
            stem = "stream_" + fp
        filename = stem if stem.lower().endswith(".mp4") else stem + ".mp4"
    filename = sanitize_filename(filename)
    if not filename.lower().endswith((".mp4", ".ts", ".mkv")):
        filename += ".mp4"
    target = unique_path(output_dir, filename)
    # 全程先写 .part，成功后才改名为最终文件。
    # 必要性（实测踩到）：ffmpeg 被中断时 moov atom（写在文件末尾）不会生成，
    # 若直接写最终文件名，用户会得到一个「大小正常但打不开」的损坏视频 ——
    # 而且它还会被当成已下载，后续 skip_existing 直接跳过。
    # 保留媒体扩展名（xxx.part.mp4）是因为 ffmpeg 靠扩展名推断容器格式。
    tmp = target.with_name(f"{target.stem}.part{target.suffix}")

    if on_progress:
        on_progress({"status": "downloading", "percent": 0.0,
                     "progressed_bytes": 0, "total_bytes": 0, "speed": 0.0,
                     "note": f"分片 {len(info.segments)} 个 / 时长 {info.duration:.0f}s"})

    # master playlist 的档位选择：按偏好从变体列表里挑一条子列表下。
    # 不挑就喂 master 给 ffmpeg（它自动选最高档）。
    target_url = url
    pick_note = ""
    pq = (prefer_quality or "").strip().lower()
    if info.kind == "hls-master" and info.variants and pq:
        def _vh(v: dict) -> int:
            res = v.get("resolution") or ""
            try:
                return int(res.split("x")[1])
            except Exception:  # noqa: BLE001
                return 0
        known = [(v, _vh(v)) for v in info.variants]
        known = [(v, h) for v, h in known if h > 0]
        if known:
            if pq == "lowest":
                known.sort(key=lambda x: x[1])
            elif pq in ("highest", "best"):
                known.sort(key=lambda x: -x[1])
            else:
                try:
                    want = int(pq)
                except ValueError:
                    want = 0
                # 精确 → 就近，同距离取高的
                known.sort(key=lambda x: (abs(x[1] - want), -x[1]) if want
                           else -x[1])
            v, h = known[0]
            if v.get("url") and h:
                target_url = v["url"]
                pick_note = f"已按清晰度偏好选 {h}p 档"

    ok, err, produced = _ffmpeg_download(target_url, tmp, referer, info.duration,
                                         on_progress, cancel_event, output_dir)
    if not ok and err != "已取消":
        # ffmpeg 不可用或失败 → 自研兜底（只对未换档的原地址有效；
        # 换了档的子列表分片路径不同，兜底下载器对 master 的分片解析不适用）
        if target_url == url:
            ok2, err2 = _raw_download(url, info, tmp, referer, on_progress, cancel_event)
            if ok2:
                ok, err, produced = True, "", tmp
            else:
                err = f"{err}；兜底也失败: {err2}"
    if pick_note and ok and on_progress:
        on_progress({"status": "finished", "percent": 100.0, "note": pick_note})

    if not ok:
        for p in (produced, tmp):
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass
        return DownloadResult(False, error=err, url=url)

    # ffmpeg 回落成 TS 时扩展名会变，按真实扩展名交付
    if produced.suffix.lower() != target.suffix.lower():
        target = unique_path(output_dir, target.stem + produced.suffix)
    try:
        produced.replace(target)
    except OSError:
        target = produced          # 改名失败也要把文件交出去，不能白下

    size = target.stat().st_size
    try:
        marker.write_text(target.name, encoding="utf-8")
    except OSError:
        pass
    if on_progress:
        on_progress({"status": "finished", "percent": 100.0,
                     "progressed_bytes": size, "total_bytes": size, "speed": 0.0})
    return DownloadResult(True, str(target), size, url=url)


def stream_supported() -> dict:
    """流下载能力状态，供前端提示。"""
    ff = _ffmpeg_available()
    return {
        "ffmpeg": bool(ff),
        "ffmpeg_path": ff,
        "raw_fallback": True,
        "hint": "" if ff else "未找到 ffmpeg：未加密的 TS 流仍可下载，加密流（AES-128）无法处理。",
    }
