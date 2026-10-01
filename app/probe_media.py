# -*- coding: utf-8 -*-
"""媒体探测：用 Range 分段请求 + PIL 增量解析，只读文件头即可拿到图片真实宽高。

为什么要探测：Bing 等搜索接口返回的卡片 HTML 里没有可靠的原图像素尺寸，
而「清晰度筛选」必须基于原图真实分辨率，因此需要并发探测每张原图的文件头。
"""
from __future__ import annotations

import io
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

import requests
from PIL import Image, ImageFile

from .config import HEADERS, MAX_PROBE_BYTES, PROBE_TIMEOUT

# 允许 PIL 处理截断的图片数据（我们只读文件头，数据必然不完整）
ImageFile.LOAD_TRUNCATED_IMAGES = True


@dataclass
class ProbeResult:
    url: str
    ok: bool = False
    width: int | None = None
    height: int | None = None
    fmt: str | None = None
    size_bytes: int | None = None
    content_type: str | None = None
    error: str | None = None


@dataclass
class ProbeOutcome:
    results: list[ProbeResult] = field(default_factory=list)


def _headers_for(url: str, referer: str | None, extra: dict | None = None) -> dict:
    """构造请求头：Referer 对很多图床是必需的（否则 403 / 返回占位图）。"""
    h = dict(HEADERS)
    if referer:
        h["Referer"] = referer
    if extra:
        h.update(extra)
    return h


def _parse_size_from_bytes(data: bytes) -> tuple[int | None, int | None, str | None]:
    """从（可能不完整的）图片字节流中解析宽高与格式。

    用增量 Parser：喂入不完整数据也能在拿到图片头/SOF 段后得到尺寸。
    """
    if not data:
        return None, None, None
    parser = ImageFile.Parser()
    try:
        parser.feed(data)
        img = parser.image
        if img is not None and getattr(img, "width", 0) and getattr(img, "height", 0):
            return img.width, img.height, (img.format or None)
    except Exception:
        pass
    try:
        parser.close()
    except Exception:
        pass
    # 兜底：直接 Image.open（部分格式只需极少字节即可读到 size）
    try:
        with Image.open(io.BytesIO(data)) as img:
            return img.width, img.height, (img.format or None)
    except Exception:
        return None, None, None


def probe_one(
    url: str,
    referer: str | None = None,
    timeout: int = PROBE_TIMEOUT,
    max_bytes: int = MAX_PROBE_BYTES,
    session: requests.Session | None = None,
) -> ProbeResult:
    """探测单个 URL 的图片宽高与文件大小。失败不抛异常，返回 ok=False。

    策略：流式读取并增量解析，一旦拿到宽高就立即停止下载；
    最多读取 max_bytes（默认 3MB），避免为大图下载整个文件。
    """
    res = ProbeResult(url=url)
    sess = session or requests
    headers = _headers_for(url, referer)

    try:
        r = sess.get(url, headers=headers, timeout=timeout, stream=True, allow_redirects=True)
        try:
            ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip()
            res.content_type = ctype or None
            cl = r.headers.get("Content-Length")
            if cl and cl.isdigit():
                res.size_bytes = int(cl)
            if r.status_code >= 400:
                res.error = f"HTTP {r.status_code}"
                return res

            parser = ImageFile.Parser()
            buf = bytearray()  # 自己累积字节，兜底时可整体重新解析
            total_read = 0
            for chunk in r.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                total_read += len(chunk)
                buf.extend(chunk)
                try:
                    parser.feed(chunk)
                except Exception:  # noqa: BLE001
                    pass
                img = getattr(parser, "image", None)
                if img is not None and getattr(img, "width", 0) and getattr(img, "height", 0):
                    res.width = int(img.width)
                    res.height = int(img.height)
                    res.fmt = img.format or None
                    break
                if total_read >= max_bytes:
                    break

            if not res.width:
                # 兜底：用累积的字节整体重新解析一次
                w, h, fmt = _parse_size_from_bytes(bytes(buf))
                if w and h:
                    res.width, res.height, res.fmt = w, h, fmt

            res.ok = bool(res.width and res.height)
            if not res.ok:
                res.error = "无法解析出宽高（可能是防盗链/动态渲染/非图片/网络受限）"
            return res
        finally:
            r.close()
    except requests.exceptions.SSLError as e:
        res.error = f"SSL 错误: {type(e).__name__}"
    except requests.exceptions.Timeout:
        res.error = "探测超时"
    except requests.exceptions.RequestException as e:
        res.error = f"请求失败: {type(e).__name__}"
    except Exception as e:  # noqa: BLE001
        res.error = f"探测异常: {type(e).__name__}: {e}"
    return res


def probe_many(
    items: list[dict],
    max_workers: int = 16,
    timeout: int = PROBE_TIMEOUT,
) -> ProbeOutcome:
    """并发探测多个 {url, referer} 项，返回与输入等长、按输入顺序排列的结果。"""
    outcome = ProbeOutcome(results=[ProbeResult(url="") for _ in items])
    if not items:
        return outcome

    sess = requests.Session()
    try:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            fut_map = {
                pool.submit(
                    probe_one,
                    it.get("url"),
                    it.get("referer"),
                    timeout,
                    it.get("max_bytes", MAX_PROBE_BYTES),
                    sess,
                ): i
                for i, it in enumerate(items)
            }
            for fut in as_completed(fut_map):
                i = fut_map[fut]
                try:
                    outcome.results[i] = fut.result()
                except Exception as e:  # noqa: BLE001
                    outcome.results[i] = ProbeResult(
                        url=items[i].get("url", ""), ok=False, error=f"{type(e).__name__}: {e}"
                    )
    finally:
        sess.close()
    return outcome


def probe_size_only(url: str, referer: str | None = None, timeout: int = 10) -> int | None:
    """只取文件大小（HEAD 优先，失败退回 Range）。"""
    sess = requests.Session()
    try:
        try:
            r = sess.head(url, headers=_headers_for(url, referer), timeout=timeout, allow_redirects=True)
            cl = r.headers.get("Content-Length")
            if cl and cl.isdigit() and int(cl) > 0:
                return int(cl)
        except Exception:
            pass
        h = _headers_for(url, referer)
        h["Range"] = "bytes=0-0"
        r = sess.get(url, headers=h, timeout=timeout, allow_redirects=True)
        cr = r.headers.get("Content-Range")
        if cr and "/" in cr:
            total = cr.rsplit("/", 1)[-1].strip()
            if total.isdigit():
                return int(total)
        return None
    except Exception:
        return None
    finally:
        sess.close()
