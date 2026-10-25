# -*- coding: utf-8 -*-
"""文件下载与命名工具：安全文件名、防盗链下载、断点跳过、重试。"""
from __future__ import annotations

import hashlib
import re
import time
from pathlib import Path
from urllib.parse import unquote, urlparse

import requests

from .config import DOWNLOADS_DIR, HEADERS, TIMEOUT

# Windows 文件名非法字符 + 控制字符
_ILLEGAL_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

# Content-Type -> 扩展名
_CT_EXT = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
    "image/svg+xml": ".svg",
    "image/avif": ".avif",
    "image/tiff": ".tiff",
    "image/x-icon": ".ico",
    "image/vnd.microsoft.icon": ".ico",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "video/quicktime": ".mov",
    "video/x-matroska": ".mkv",
    "audio/mpeg": ".mp3",
    "audio/mp4": ".m4a",
}


# 一个媒体最多再试几个其它档位地址（防止一个条目挂着一长串地址时反复重试）
MAX_ALTERNATES = 6


def sanitize_filename(name: str, max_len: int = 120) -> str:
    """把任意字符串清理为 Windows 安全文件名。"""
    if not name:
        return "download"
    s = unquote(name).strip()
    s = _ILLEGAL_RE.sub("_", s)          # 非法字符替换
    s = re.sub(r"\s+", " ", s).strip()    # 折叠空白
    s = s.strip(". ")                      # 去掉首尾点和空格（Windows 不允许）
    if not s:
        return "download"
    # 保留扩展名地截断
    stem, dot, ext = s.rpartition(".")
    if dot and len(ext) <= 5:
        keep = max(1, max_len - len(ext) - 1)
        s = stem[:keep].rstrip() + "." + ext
    else:
        s = s[:max_len]
    # 处理 Windows 保留设备名
    base = s.split(".")[0].upper()
    if base in _RESERVED_NAMES:
        s = "_" + s
    return s


# 「占位型」URL 文件名：video_360p / media / 123 这类名字没有内容语义，
# 真正的名字在页面标题里。命中就用标题命名，并保留清晰度标记。
# 为什么不能用 URL 名：xvideos 的 27 部片 URL 文件名全是 video_360p.mp4，
# 落盘变成 video_360p.mp4 / video_360p (1).mp4 …用户完全分不清哪部是哪部。
_GENERIC_STEM_RE = re.compile(
    r"^(?:video|media|file|download|image|photo|pic|img|index|main|movie|"
    r"stream|play|clip|full|hd|watch|attachment|banner|temp|tmp)\d*$|^\d{1,6}$"
    # 长哈希名也算「没语义」：抖音等站的直链文件名是 40+ 位无分隔的字母数字
    # （如 oEuftOeB1CwymGriiQxF5AILAEOYvbxw4eEsjz）。不认它，标题就没机会生效，
    # 用户下到的就是一串乱码文件名（2026-10-25 实测）。
    # 要求「无分隔符 + 18 位以上」，正常文件名基本都带 - _ 或空格，不会误伤。
    r"|^[A-Za-z0-9]{18,}$",
    re.I)
_QUALITY_SUFFIX_RE = re.compile(r"[_\-.](\d{3,4})p$", re.I)


def filename_from_url(url: str, title: str = "", content_type: str = "") -> str:
    """从 URL / 标题 / Content-Type 推断文件名（含扩展名）。

    命名优先级：
      1. URL 文件名有内容语义（mountains-100367_1280.jpg）→ 直接用；
      2. URL 文件名是占位名（video_360p.mp4）且抓到了标题 → 用标题 + [档位]；
      3. 都没有 → image/download 兜底。
    """
    path = urlparse(url).path
    base = Path(path).name
    name, ext = "", ""
    if base and "." in base:
        stem, dot, e = base.rpartition(".")
        if dot and 1 <= len(e) <= 5:
            name, ext = stem, "." + e.lower()
    elif base:
        name = base

    # 占位名判定要把清晰度后缀先剥掉（video_360p → video），否则判不出「没语义」
    stem_base = _QUALITY_SUFFIX_RE.sub("", name)
    if title and _GENERIC_STEM_RE.match(stem_base or ""):
        t = sanitize_filename(title)
        if t:
            q = _QUALITY_SUFFIX_RE.search(name)
            # 档位标记挂在标题后：Erika Bella (1995) [360p].mp4
            name = f"{t} [{q.group(1)}p]" if q else t

    if not name:
        name = sanitize_filename(title) if title else "image"
    if not ext:
        ext = _CT_EXT.get((content_type or "").split(";")[0].strip().lower(), "")

    name = sanitize_filename(name)
    if ext and not name.lower().endswith(ext.lower()):
        name = name + ext
    return name or "download"


def unique_path(dir_path: Path, filename: str) -> Path:
    """若同名文件已存在，追加 (1)/(2)... 避免覆盖。"""
    p = dir_path / filename
    if not p.exists():
        return p
    stem, ext = p.stem, p.suffix
    i = 1
    while True:
        cand = dir_path / f"{stem} ({i}){ext}"
        if not cand.exists():
            return cand
        i += 1


def url_fingerprint(url: str) -> str:
    """URL 指纹，用于去重与跳过已下载。"""
    return hashlib.sha1(url.encode("utf-8", "ignore")).hexdigest()[:16]


class DownloadResult:
    def __init__(self, ok: bool, path: str = "", size: int = 0,
                 skipped: bool = False, error: str = "", url: str = "",
                 note: str = ""):
        self.ok = ok
        self.path = path
        self.size = size
        self.skipped = skipped
        self.error = error
        self.url = url
        self.note = note          # 供界面展示的补充说明（如「已自动换档位」）

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "path": self.path,
            "name": Path(self.path).name if self.path else "",
            "size": self.size,
            "skipped": self.skipped,
            "error": self.error,
            "url": self.url,
            "note": self.note,
        }


def _quality_of(url: str) -> int:
    """从地址里推断清晰度（竖线高度）。读不出返回 0。

    覆盖两种档位命名形态（项目里都实测过）：
      前缀式  240P_1000K_61466355.mp4 / 720P_2500K_xxx.mp4
      后缀式  video_240p.mp4 / clip-1080p.mp4
      尺寸式  1280x720.mp4
    """
    u = (url or "").lower()
    m = re.search(r"(\d{3,4})p(?![a-z0-9])", u)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d{3,4})x(\d{3,4})", u)
    if m:
        return int(m.group(2))
    return 0


def _order_by_quality(url: str, alternates: list[str] | None,
                      prefer: str) -> tuple[str, list[str]]:
    """按清晰度偏好重排「首选 + 候选」，返回 (新的首选, 新的候选)。

    为什么要这个：批量抓取的结果里，一条媒体可能带着若干档位地址
    （归并时被合并的那些，存在 variants 里）。原来只有「首选失败才试候选」，
    用户没法主动挑 —— 抓到的第一条是 240P 就只能下 240P。
    """
    cands = [url] + [c for c in (alternates or []) if c and c != url]
    scored = [(c, _quality_of(c)) for c in cands]
    known = [(c, h) for c, h in scored if h > 0]
    unknown = [c for c, h in scored if h == 0]

    if not known:                      # 一个都读不出高度，保持原顺序
        return url, [c for c in cands[1:]]

    if prefer == "lowest":
        known.sort(key=lambda x: x[1])
    elif prefer == "highest":
        known.sort(key=lambda x: -x[1])
    else:
        try:
            want = int(prefer)
        except (TypeError, ValueError):
            want = 0
        if want:
            # 优先精确匹配，其次最接近；同距离取高的
            known.sort(key=lambda x: (abs(x[1] - want), -x[1]))
        else:
            known.sort(key=lambda x: -x[1])

    ordered = [c for c, _ in known] + unknown
    return ordered[0], ordered[1:]


def download_file(
    url: str,
    output_dir: Path | str,
    referer: str = "",
    title: str = "",
    filename: str = "",
    timeout: int = TIMEOUT,
    max_retries: int = 3,
    skip_existing: bool = True,
    session: requests.Session | None = None,
    on_progress=None,
    chunk_size: int = 64 * 1024,
    alternates: list[str] | None = None,
    prefer_quality: str = "",
) -> DownloadResult:
    """下载单个文件（图片/视频直链）。

    - 自动带 Referer 应对防盗链
    - 失败重试（指数退避）
    - skip_existing：同 URL 指纹文件已存在则跳过
    - on_progress(dict) 回报进度
    - alternates：同一媒体的**其它档位地址**。首选地址失败时依次尝试。
    - prefer_quality：清晰度偏好（highest / lowest / 具体高度如 "720"）。
      设了就从「首选 + 其它档位」里**主动挑**该档位，而不是等失败了才回退。

    alternates 为什么必要（实测踩坑）：同一部片常有多个档位地址，而
    「名字看起来最高清」的那条未必下得了 —— pornhub 的 original_<id>.mp4
    挂在图片 CDN 上、请求返回 470 No Token，同片的 240P 地址却是
    200 / 88MB 真视频。抓取侧已尽量只留能下的那条，这里再兜一层，
    避免用户点了下载只看到「全部失败」。
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if prefer_quality:
        url, alternates = _order_by_quality(url, alternates, prefer_quality)

    sess = session or requests.Session()
    own_session = session is None

    def _try(target_url: str) -> DownloadResult:
        """对单个地址尝试下载（含跳过判断与重试）。"""
        headers = dict(HEADERS)
        if referer:
            headers["Referer"] = referer

        fp = url_fingerprint(target_url)
        marker = output_dir / f".{fp}.done"

        # ---- 先判断跳过，再发请求 ----
        # 跳过判断必须早于网络请求：否则已下载的文件仍会重新请求，
        # 站点波动时会误报「下载失败」，且白白消耗流量。
        if skip_existing and marker.exists():
            try:
                saved_name = marker.read_text(encoding="utf-8").strip()
            except Exception:
                saved_name = ""
            existing = (output_dir / saved_name) if saved_name else None
            if existing and existing.exists() and existing.stat().st_size > 0:
                res = DownloadResult(True, str(existing), existing.stat().st_size,
                                     skipped=True, url=target_url)
                if on_progress:
                    on_progress({"status": "finished", "percent": 100.0,
                                 "progressed_bytes": res.size, "total_bytes": res.size,
                                 "skipped": True})
                return res
            # 标记存在但文件已被移动/删除：**仍然算下载过**，按记录跳过。
            # V3.0 改这里的原因（2026-10-24 用户需求）：以前会删掉标记并重新下载，
            # 用户把视频转移到别的盘之后，再抓同一个页面就会白白重下一整批。
            # 想真正重下：关掉「跳过已存在」。
            return DownloadResult(
                True, "", 0, skipped=True, url=target_url,
                error="", note=(f"此前已下载过（{saved_name or '文件名未记录'}），"
                                "但文件已不在当前目录，已按记录跳过。"
                                "需要重新下载请关闭「跳过已存在」。"))

        last_err = ""
        # ---- 断点续传：.part 按 URL 指纹固定命名，重试接着已下的字节继续 ----
        # 为什么必须（2026-09-29 用户实测）：avjoy 的大文件 450~737MB，CDN 会在
        # 传输中途掐断连接；旧实现每次重试都从 0 开始，3 次重试也下不完 ——
        # 表现就是「大文件有时成功有时失败」。
        tmp = output_dir / f".{url_fingerprint(target_url)}.part"
        stall_sec = 60          # 这么久一个字节都没到 → 判定连接停摆（重试+续传）
        gave_up_range = False   # 服务器不支持 Range 时置位，后续不再带 Range
        for attempt in range(1, max_retries + 1):
            try:
                resume_from = 0
                if not gave_up_range:
                    resume_from = tmp.stat().st_size if tmp.exists() else 0
                headers2 = dict(headers)
                if resume_from > 0:
                    headers2["Range"] = f"bytes={resume_from}-"
                with sess.get(target_url, headers=headers2, timeout=timeout, stream=True,
                              allow_redirects=True) as r:
                    if r.status_code == 403 and referer:
                        try:
                            src = urlparse(referer)
                            root_ref = f"{src.scheme or 'https'}://{src.netloc}/" if src.netloc else ""
                        except Exception:
                            root_ref = ""
                        if root_ref and headers2.get("Referer") != root_ref:
                            r.close()
                            h3 = dict(headers2)
                            h3["Referer"] = root_ref
                            r = sess.get(target_url, headers=h3, timeout=timeout, stream=True,
                                         allow_redirects=True)
                    if r.status_code >= 400:
                        last_err = f"HTTP {r.status_code}"
                        if r.status_code == 416:
                            # Range 越界：远端资源已变化，弃掉旧进度从头来
                            tmp.unlink(missing_ok=True)
                            gave_up_range = True
                            continue
                        # 4xx 通常重试无意义，403/429/408 可试
                        if r.status_code not in (403, 429, 408):
                            break
                        time.sleep(0.8 * attempt)
                        continue

                    resuming = (resume_from > 0 and r.status_code == 206)
                    ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                    total = r.headers.get("Content-Length")
                    total = int(total) if total and total.isdigit() else 0
                    if resuming and total:
                        total = total + resume_from

                    # 决定文件名（用局部变量，避免换档位时沿用上一个地址推出来的名字）
                    if filename:
                        fname = sanitize_filename(filename)
                    else:
                        fname = sanitize_filename(filename_from_url(target_url, title, ctype))

                    target = unique_path(output_dir, fname)

                    done = resume_from if resuming else 0
                    t_last = time.time()
                    bytes_last = done
                    last_byte_ts = time.time()
                    mode = "ab" if resuming else "wb"
                    with open(tmp, mode) as f:
                        for chunk in r.iter_content(chunk_size=chunk_size):
                            if not chunk:
                                continue
                            f.write(chunk)
                            done += len(chunk)
                            last_byte_ts = time.time()
                            if on_progress:
                                now = time.time()
                                speed = 0.0
                                if now - t_last > 0.2:
                                    speed = (done - bytes_last) / (now - t_last)
                                    t_last, bytes_last = now, done
                                on_progress({
                                    "status": "downloading",
                                    "progressed_bytes": done,
                                    "total_bytes": total,
                                    "percent": round(done * 100 / total, 1) if total else 0.0,
                                    "speed": speed,
                                })
                            # 停摆检测：读超时只管「一次 read 卡死」，管不了
                            # 「每 20 秒来 1 字节」的涓流 —— 那种连接的表现就是
                            # 用户说的「一直没反应」。主动掐断，交给重试续传。
                            if time.time() - last_byte_ts > stall_sec:
                                raise requests.exceptions.ConnectionError(
                                    f"连接停摆（{stall_sec} 秒未收到任何数据）")
                    # 校验大小
                    real = tmp.stat().st_size
                    if total and real < total * 0.99:
                        last_err = f"下载不完整（{real}/{total} 字节）"
                        # ⚠️ 不删 .part —— 保留进度，下一次重试从这里续传
                        time.sleep(0.6 * attempt)
                        continue
                    tmp.replace(target)
                    # 写完成标记（含真实文件名），便于精确跳过
                    try:
                        marker.write_text(target.name, encoding="utf-8")
                    except Exception:
                        pass
                    res = DownloadResult(True, str(target), real, url=target_url)
                    if on_progress:
                        on_progress({"status": "finished", "percent": 100.0,
                                     "progressed_bytes": real, "total_bytes": real})
                    return res
            except requests.exceptions.SSLError as e:
                # SSL 错误也要重试。原实现直接 break（注释写「重试无意义」），
                # 但实测在本机（走代理）下，SSLEOFError 是**常见且瞬时**的：
                # 同一批 3 个 pornhub 下载里就零星失败 1 个，重试即可恢复。
                # 只有证书校验类问题才是真的重试无意义。
                last_err = f"SSL 错误: {type(e).__name__}"
                _msg = str(e).upper()
                if "CERTIFICATE" in _msg or "CERT_VERIFY" in _msg:
                    break
                time.sleep(0.8 * attempt)
            except requests.exceptions.Timeout:
                last_err = "下载超时"
            except requests.exceptions.RequestException as e:
                last_err = f"网络错误: {type(e).__name__}"
            except OSError as e:
                last_err = f"写入失败: {e}"
                break
            time.sleep(0.8 * attempt)
        # 重试耗尽：**保留 .part**（断点续传进度），下次同一地址接着下
        return DownloadResult(False, error=last_err or "下载失败", url=target_url)

    try:
        tried = [url]
        first = _try(url)
        res = None
        if first.ok or not alternates:
            res = first
        else:
            for alt in [a for a in alternates if a and a not in tried][:MAX_ALTERNATES]:
                tried.append(alt)
                r = _try(alt)
                if r.ok:
                    # 明确告诉用户「下到的是换档位后的结果」，别让人以为首选可用
                    r.note = (f"首选地址不可用（{first.error}），"
                              f"已自动改用同媒体的其它档位地址（共试 {len(tried)} 个）")
                    res = r
                    break
            if res is None:
                # 错误信息保持以首个错误开头：task_manager 的失败归因是看前缀的
                first.error = (f"{first.error}（并已尝试同媒体其它 {len(tried) - 1} 个档位地址，"
                               f"同样失败）")
                res = first
        if res.ok:
            # ⚠️ 这里只能用 `res.url`（DownloadResult 里记的就是实际请求到的地址）
            # 或外层参数 `url`；`target_url` 是 _try 闭包里的局部变量，外层拿不到
            # —— 写错过一次，回归套件立刻报 NameError 才没漏出去。
            _record_history(res, res.url or url, title)
        return res
    finally:
        if own_session:
            sess.close()


# 视频类扩展名（用于台账里区分 kind，只影响展示分组，不参与身份判定）
_VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".mov", ".flv", ".ts", ".m3u8", ".mpd",
               ".avi", ".wmv", ".m4v", ".f4v", ".mpg", ".mpeg", ".3gp"}


def _record_history(res: "DownloadResult", url: str, title: str = "") -> None:
    """把一次成功的直链下载记入台账（去重用）。

    判据是**下载地址的归一身份**，不是文件名/路径 —— 文件被移走后依然算「下过」。
    记账失败绝不影响下载本身，所以整体吞异常。

    ⚠️ **只记视频，不记图片**：
    台账是给「搜索结果隐藏已下载的视频」用的；图片没有这个入口，
    而批量下 500 张图会触发 500 次「读整个 JSON + 写整个 JSON」，
    文件越大越慢、还和并发线程抢锁。没必要为了用不到的用途付这个代价。
    """
    try:
        ext = Path(res.path or "").suffix.lower()
        if ext not in _VIDEO_EXTS:
            return
        from . import history

        history.record(url, None, title=title or Path(res.path or "").name,
                       kind="video", path=res.path or "", size=int(res.size or 0),
                       source_url=referer or url)
    except Exception:
        pass


def batch_download_images(
    items: list[dict],
    output_dir: Path | str,
    max_workers: int = 6,
    skip_existing: bool = True,
    on_item_done=None,
    on_progress=None,
    should_cancel=None,
    prefer_quality: str = "",
    cookies_file: str = "",
) -> dict:
    """并发批量下载图片。

    items: [{url, referer, title, filename?, alternates?, prefer_quality?}, ...]
    on_item_done(index, DownloadResult) 每张完成后回调
    should_cancel() -> bool 取消检查；返回 True 时不再提交新任务并尽快结束
    返回 {results, ok_count, fail_count, skipped_count, cancelled}

    prefer_quality：整批的清晰度偏好（highest / lowest / "720"）。
    单条 item 里也可以带同名字段覆盖整批设置。

    取消用显式检查（should_cancel）而非抛异常穿透：
    as_completed 的 except 会吞掉异常，靠异常传递取消会导致取消失效。
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    results: list[DownloadResult | None] = [None] * len(items)
    sess = requests.Session()
    if cookies_file:
        try:
            from http.cookiejar import MozillaCookieJar
            jar = MozillaCookieJar(str(cookies_file))
            jar.load(ignore_discard=True, ignore_expires=True)
            sess.cookies.update(jar)
        except Exception:
            pass
    cancel_flag = {"v": False}

    def _is_cancelled() -> bool:
        if cancel_flag["v"]:
            return True
        if should_cancel:
            try:
                if should_cancel():
                    cancel_flag["v"] = True
                    return True
            except Exception:
                pass
        return False

    # 并发不再是「开 max_workers 个线程就同时跑 max_workers 个请求」：
    # max_workers 是用户设的**上限**，真正同时跑几个由闸门按平台实时调节
    # （平台返回 412/429 时自动减半，稳定后再爬回来）。
    # 线程池仍按上限开，多余的 worker 会停在 acquire 上等名额 ——
    # 不重建池、不打断在途请求，也不会有半截文件。
    from .concurrency import GOVERNOR, KIND_IMAGE_DOWNLOAD, OK, classify_error
    _first_url = str((items[0] or {}).get("url") or "") if items else ""
    gate = GOVERNOR.gate_for_url(_first_url, KIND_IMAGE_DOWNLOAD, ceiling=max_workers)

    def _work(i: int, it: dict) -> tuple[int, DownloadResult]:
        # 提交后可能仍在队列中等待，开跑前再查一次取消，避免做无用功
        if _is_cancelled():
            return i, DownloadResult(False, error="已取消", url=it.get("url", ""))

        # 等名额期间被取消要能立刻退出，否则用户点了取消还得等几百张排完
        if not gate.acquire(cancelled=_is_cancelled):
            return i, DownloadResult(False, error="已取消", url=it.get("url", ""))

        signal = None
        try:
            def _cb(info):
                if on_progress:
                    on_progress(i, info)

            r = download_file(
                it.get("url", ""),
                output_dir,
                referer=it.get("referer", ""),
                title=it.get("title", ""),
                filename=it.get("filename", ""),
                skip_existing=skip_existing,
                session=sess,
                on_progress=_cb,
                alternates=it.get("alternates") or None,
                prefer_quality=it.get("prefer_quality") or prefer_quality,
            )
            if r.ok:
                signal = OK
            elif str(r.error or "") != "已取消":
                signal = classify_error(r.error)
            return i, r
        except BaseException:  # noqa: BLE001  异常照常往上抛，但名额必须还
            signal = "network"
            raise
        finally:
            gate.release(signal)

    ok = fail = skipped = 0
    valid = [(i, it) for i, it in enumerate(items) if it.get("url")]
    workers = max(1, min(gate.ceiling, len(valid) or 1))
    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = []
            for i, it in valid:
                if _is_cancelled():
                    break
                futs.append(pool.submit(_work, i, it))
            for fut in as_completed(futs):
                try:
                    i, r = fut.result()
                except Exception:  # noqa: BLE001
                    # 单个任务异常不中断整体，但也不吞掉取消状态
                    _is_cancelled()
                    continue
                results[i] = r
                if r.ok and r.skipped:
                    skipped += 1
                elif r.ok:
                    ok += 1
                elif r.error != "已取消":
                    fail += 1
                if on_item_done:
                    on_item_done(i, r)
                _is_cancelled()
    finally:
        sess.close()

    return {
        "results": [r.to_dict() if r else None for r in results],
        "ok_count": ok,
        "fail_count": fail,
        "skipped_count": skipped,
        "total": len(items),
        "output_dir": str(output_dir),
        "cancelled": cancel_flag["v"],
    }
