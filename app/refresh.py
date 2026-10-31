# -*- coding: utf-8 -*-
"""直链过期后的「回源刷新」。

## 为什么需要

pornhao 这类站把视频挂在自己的 CDN 上，直链带签名与过期时间：

    https://video28.pornocdn.info/zak/own_hq1/31527.mp4?st=xxxx&e=1793291464

`e` 就是过期时间戳。实测有效期约 3 小时（21:22 抓到的 `e=1793291464` 对应
次日 00:31）。抓取当下点下载没问题，**过几小时再点就整批 403**：

    e 改成过去时间 → 403 Forbidden (nginx)
    st 改坏        → 403 Forbidden
    去掉 st        → 403 Forbidden

用户看到的现象就是「昨天还能抓，今天点了下载全部失败」。这时重试同一个 URL
毫无意义（签名不会自己变好），正确做法是**回源**：重新打开抓到这条直链的
详情页，取出同一部片的新签名地址再下。

也不是站点专用逻辑 —— 任何「签名直链 + 短有效期」的站都是同一个解法
（同理适用于各家图床、私有 CDN、防盗链资源）。

## 身份判定

同一条媒体在不同档位目录下文件名相同：

    /zak/own_hq1/31527.mp4
    /zak/own_m1/31527.mp4
    /zak/own1/31527.mp4

所以用**路径最后一段（basename）**做身份键。匹配不上就返回空 —— 宁可失败，
也绝不「猜一个近似的地址」把别的片子下下来。
"""
from __future__ import annotations

import re
import threading
from urllib.parse import urlparse

from .config import TIMEOUT

# 这些错误码/描述意味着「地址侧失效」，值得回源换新地址
_EXPIRED_HINTS = ("HTTP 403", "HTTP 401", "HTTP 410", "HTTP 404")

# 同一页面在一批下载里可能被多条失败项引用，缓存一次解析结果
_cache: dict[str, dict[str, str]] = {}
_cache_lock = threading.Lock()


def media_identity(url: str) -> str:
    """媒体的身份键：路径最后一段（小写，不含查询参数）。"""
    try:
        path = urlparse(url).path
    except Exception:
        return ""
    return (path.rsplit("/", 1)[-1] or "").strip().lower()


def media_path(url: str) -> str:
    """媒体的完整路径（不含主机与查询参数）。用于优先匹配同一档位。"""
    try:
        return (urlparse(url).path or "").lower()
    except Exception:
        return ""


def looks_address_expired(error: str) -> bool:
    """错误信息是否属于「地址失效」类，值得回源。"""
    e = str(error or "").upper()
    return any(h in e for h in _EXPIRED_HINTS)


def _page_map(origin_page: str, timeout: int = TIMEOUT,
              allow_render: bool = True) -> dict[str, str]:
    """抓一次页面，返回 {键: 直链}。

    键有两套：`p:<完整路径>` 与 `n:<文件名>`。刷新时优先按完整路径匹配
    （保持原来的档位/体积），匹配不到才退化为按文件名匹配 —— 否则一部
    30MB 的低码率片可能被刷成 300MB 的原片。
    """
    with _cache_lock:
        if origin_page in _cache:
            return _cache[origin_page]

    out: dict[str, str] = {}
    try:
        from .batch_crawler import fetch_page
        from .extractor import extract_from_html, extract_from_json_text

        fr = fetch_page(origin_page, allow_render=allow_render, timeout=timeout)
        if fr.ok:
            items = extract_from_html(fr.html or "", origin_page, origin_page)
            for txt in (fr.json_texts or []):
                try:
                    items += extract_from_json_text(txt, origin_page, origin_page)
                except Exception:
                    continue
            for it in items:
                u = getattr(it, "url", "") or ""
                path_key = "p:" + media_path(u)
                name_key = "n:" + media_identity(u)
                if media_identity(u):
                    out.setdefault(name_key, u)
                    out.setdefault(path_key, u)
    except Exception:
        out = {}

    with _cache_lock:
        _cache[origin_page] = out
    return out


def refresh_url(origin_page: str, old_url: str, *, timeout: int = TIMEOUT,
                allow_render: bool = True) -> str:
    """回源找同一条媒体的新直链；找不到返回空串。

    先按完整路径找同一档位，再按文件名找任意档位。
    """
    page = (origin_page or "").strip()
    if not page or not page.startswith("http"):
        return ""
    if not media_identity(old_url):
        return ""
    m = _page_map(page, timeout=timeout, allow_render=allow_render)
    for key in ("p:" + media_path(old_url), "n:" + media_identity(old_url)):
        new_url = m.get(key) or ""
        if new_url and new_url != old_url:
            return new_url
    return ""


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()
