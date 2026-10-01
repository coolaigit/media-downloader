# -*- coding: utf-8 -*-
"""Bilibili 关键词搜索。

为什么单独一个模块（不算破坏「不写站点专用规则」的红线）：
  界面里 **Bilibili 本来就是用户显式选择的平台**（与 YouTube 并列），
  `video_downloader.SEARCH_PREFIXES` 也早就硬编码了 `bilisearch`。
  这里只是把「B 站搜索」这条平台级链路换一个能用的实现，不涉及通用站的抓取规则。

为什么换掉 yt-dlp 的 `bilisearch`（2026-09-21 实测）：
  - `bilisearch5:科比`    → 10 条，但**只有 id 和 url，标题字段是 None**，
    扁平模式下拿不到标题/封面/时长，界面只能显示「(无标题)」；
  - `bilisearch5:张梓琳`  → `HTTP Error 412: Precondition Failed`
    （yt-dlp 走的是已废弃的 `x/web-interface/search/type`，被 B 站 WAF 拦）；
  - 即使去掉扁平模式按单个视频解析，也会 502 / ReadTimeout。

改用 B 站当前在用的 `x/web-interface/wbi/search/type`，**必须带
`Referer: https://search.bilibili.com/` 与 `Origin`**：
  实测（同一关键词连续请求）—— 不带 Referer 时旧接口返回 412 `request was banned`；
  带上后 wbi 接口 `code=0` 且稳定返回 20 条，直连与走代理都正常。
  另：全新会话（无 buvid3 cookie）也能直接用，不需要登录态。
"""
from __future__ import annotations

import re

import requests

from .config import HEADERS
from .search_web_video import (
    SEARCH_TIMEOUT,
    TRANSIENT_ERRORS,
    _clean_title,
    _format_duration,
    _get_with_retry,
    _parse_duration,
)

API = "https://api.bilibili.com/x/web-interface/wbi/search/type"
PAGE_SIZE = 20          # 接口单页上限


def _pic_url(pic: str) -> str:
    """封面地址是协议相对的（`//i0.hdslb.com/...`），前端要绝对地址。"""
    p = (pic or "").strip()
    if p.startswith("//"):
        return "https:" + p
    return p


def search_bilibili_videos(keyword: str, count: int = 20,
                           timeout: int = SEARCH_TIMEOUT) -> tuple[list[dict], str | None]:
    """按关键词搜 B 站视频，返回统一结构的条目列表。

    条目结构与「全网」那条链路一致（index/title/duration/webpage_url/thumbnail/
    uploader/source_site/view_count），前端复用同一个列表渲染。
    """
    keyword = (keyword or "").strip()
    if not keyword:
        return [], "请输入关键词"

    want = max(1, count)
    session = requests.Session()
    session.headers.update({
        "User-Agent": HEADERS["User-Agent"],
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        # 这两行是硬性要求：缺失会被 WAF 判为盗链（412 request was banned）
        "Referer": "https://search.bilibili.com/",
        "Origin": "https://search.bilibili.com",
    })
    # B 站是国内站点，**直连首选**：实测直连与走代理都通，但本机那个本地代理
    # 会偶发 ProxyError / ConnectionReset，走代理反而更慢更容易失败。
    # 直连失败时 _get_with_retry 会自动换到走代理那条路重试一次。
    session.trust_env = False

    items: list[dict] = []
    seen: set[str] = set()
    page = 1
    # 单页 20 条；要更多就翻页（翻页上限 3 次，避免慢）
    while len(items) < want and page <= 3:
        try:
            r = _get_with_retry(API, session=session, params={
                "search_type": "video", "keyword": keyword,
                "page": page, "page_size": PAGE_SIZE,
            }, timeout=timeout)
            data = r.json()
        except TRANSIENT_ERRORS as e:
            return items, (None if items else
                           f"请求失败: {type(e).__name__}（已重试 3 次）")
        except (ValueError, requests.exceptions.RequestException) as e:
            return items, (None if items else f"请求失败: {type(e).__name__}")

        if data.get("code") != 0:
            msg = str(data.get("message") or data.get("code"))
            if items:
                break
            hint = "（站点侧风控拦截，稍后重试）" if "banned" in msg or data.get("code") == -412 else ""
            return [], f"Bilibili 搜索失败：{msg}{hint}"

        rows = ((data.get("data") or {}).get("result")) or []
        if not isinstance(rows, list) or not rows:
            break

        for x in rows:
            if not isinstance(x, dict):
                continue
            bvid = (x.get("bvid") or "").strip()
            url = (x.get("arcurl") or "").strip()
            if bvid:
                url = f"https://www.bilibili.com/video/{bvid}"
            if not url.startswith("http") or url in seen:
                continue
            seen.add(url)
            dur_text = str(x.get("duration") or "")
            dur = _parse_duration(dur_text)
            items.append({
                "id": bvid or str(x.get("aid") or ""),
                "title": _clean_title(x.get("title") or ""),
                "duration": dur,
                "duration_string": dur_text or _format_duration(dur),
                "webpage_url": url,
                "thumbnail": _pic_url(x.get("pic") or ""),
                "uploader": _clean_title(x.get("author") or ""),
                "source_site": "bilibili.com",
                "view_count": x.get("play"),
                "source": "bilibili",
            })
            if len(items) >= want:
                break
        page += 1

    if not items:
        return [], "Bilibili 没有搜到与该关键词相关的视频"
    return items, None


# 关键词里的高亮标签（`<em class="keyword">科比</em>`）由 _clean_title 统一剥离，
# 这里保留一个断言用的正则，便于测试确认接口返回确实带这类标记。
HIGHLIGHT_RE = re.compile(r'<em class="keyword">')
