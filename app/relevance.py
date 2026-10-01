# -*- coding: utf-8 -*-
"""搜索结果相关性校验。

背景（2026-09-18 实测）：搜索引擎的上游链路并不总是可信 —— 同一关键词连续请求，
拿回的卡片主题可能在「日文医学图 / 猫的画 / 巴基斯坦前总理 / 墨西哥帽子」之间乱跳，
完全与关键词无关。此时若把结果原样展示，用户看到的就是「关键词与结果无法匹配」。

本模块提供一套**与站点无关**的通用判据：
1. 从关键词里提取有区分度的 token（英文按词、中文按 2-gram，剔除无区分度的通用词）；
2. 在结果的多个字段（标题 / 原图 URL / 缩略图 URL / 来源页 / 来源域名）里检索命中；
3. 引擎级审计：样本足够却一条都不命中，判该引擎本批结果不可信 → 整批拦截，
   交由上层换引擎兜底或如实告知用户，而不是把脏结果塞给用户。

设计原则：宁可多给，不可误删。只有「一条都不相关且样本足够」才拦截；
命中率尚可时**不做剔除**，仅把相关条目排在前面，避免无关内容挤占 limit 名额。
"""
from __future__ import annotations

import re
from urllib.parse import unquote

# 无区分度的通用词：几乎所有图片结果的标题/域名都会命中，留着只会虚高命中率
_STOPWORDS = {
    # 英文虚词
    "the", "and", "for", "with", "from", "that", "this", "these", "those", "are", "was",
    "were", "you", "your", "his", "her", "its", "our", "their", "but", "not", "all", "any",
    "can", "has", "have", "had", "how", "what", "when", "where", "which", "who", "why",
    "will", "would", "into", "than", "then", "them", "they", "she", "him", "out", "off",
    "one", "two", "new", "old", "best", "top", "get", "got", "use", "using", "about",
    # 图片站/搜索引擎通用词
    "free", "hd", "uhd", "4k", "8k", "5k", "high", "quality", "resolution", "desktop",
    "image", "images", "img", "imgs", "picture", "pictures", "pic", "pics", "photo",
    "photos", "photography", "photograph", "wallpaper", "wallpapers", "background",
    "backgrounds", "stock", "clipart", "vector", "gallery", "collection", "album",
    "download", "downloads", "royalty", "shutterstock", "istock", "istockphoto",
    "gettyimages", "getty", "dreamstime", "alamy", "adobe", "stockphoto", "unsplash",
    "pexels", "pixabay", "pinterest", "flickr", "deviantart", "screen", "mobile",
    "www", "com", "net", "org", "http", "https", "html", "htm", "jpg", "jpeg", "png",
    "webp", "gif", "small", "medium", "large", "thumb", "thumbnail", "preview",
    "original", "resize", "scaled", "cropped", "width", "height", "static", "cdn",
}

_MIN_TOKEN_LEN = 3
# 拦截门槛：样本数达标 + 命中率低于该值 → 判该引擎本批不可信
_BLOCK_MIN_SAMPLE = 8
_BLOCK_MAX_RATIO = 0.12


def query_tokens(query: str) -> list[str]:
    """提取用于相关性判定的 token（保持出现顺序、去重）。"""
    q = (query or "").strip().lower()
    if not q:
        return []
    toks: list[str] = []

    # 英文/数字：按词切；中英混排时中文段由下面的 CJK 分支处理
    for w in re.findall(r"[a-z0-9]+", q):
        if len(w) >= _MIN_TOKEN_LEN and w not in _STOPWORDS:
            toks.append(w)

    # 中文：单字词直接用；多字词取相邻 2-gram，避免分词依赖
    for seg in re.findall(r"[\u4e00-\u9fff]+", q):
        if len(seg) == 1:
            toks.append(seg)
        elif len(seg) == 2:
            toks.append(seg)
        else:
            toks.extend(seg[i : i + 2] for i in range(len(seg) - 1))

    seen: set[str] = set()
    uniq: list[str] = []
    for t in toks:
        if t not in seen:
            seen.add(t)
            uniq.append(t)
    return uniq


def item_blob(item: dict) -> str:
    """把一条结果的可检索字段拼成一个字符串（URL 先解码，否则中文关键词命中不了）。

    同时认 `url`（图片结果）与 `webpage_url`（视频结果）—— 两条链路共用本模块的
    相关性判据，字段名不一致时要一起认，否则视频结果会因为「URL 没被检索到」
    而被误判成无关。
    """
    parts = [
        str(item.get("title") or ""),
        str(item.get("url") or ""),
        str(item.get("webpage_url") or ""),
        str(item.get("thumbnail") or ""),
        str(item.get("source_page") or ""),
        str(item.get("source_site") or ""),
    ]
    raw = " ".join(parts)
    try:
        raw = unquote(raw)
    except Exception:  # noqa: BLE001
        pass
    return raw.lower()


def is_relevant(item: dict, tokens: list[str]) -> bool:
    """任一个 token 命中即算相关。tokens 为空时无法判定，一律视为相关（不误杀）。"""
    if not tokens:
        return True
    blob = item_blob(item)
    return any(t in blob for t in tokens)


def audit(
    items: list[dict],
    query: str,
    min_sample: int = _BLOCK_MIN_SAMPLE,
    max_ratio: float = _BLOCK_MAX_RATIO,
) -> tuple[list[dict], dict]:
    """审计一批结果与关键词的相关性。

    返回 (保留的条目, 报告)：
    - 报告 checked=False 表示关键词里没有可用的判别 token，跳过校验；
    - 报告 blocked>0 表示整批被判为与关键词无关（调用方应换引擎兜底或如实告知）。
    """
    total = len(items)
    report: dict = {"checked": False, "total": total, "hits": total, "blocked": 0}
    tokens = query_tokens(query)
    if not tokens or not items:
        return items, report

    hits = [it for it in items if is_relevant(it, tokens)]
    ratio = len(hits) / total if total else 1.0
    report.update({"checked": True, "hits": len(hits), "ratio": round(ratio, 3)})

    if total >= min_sample and ratio <= max_ratio:
        report["blocked"] = total
        report["reason"] = "unrelated"
        return [], report
    return items, report


def relevance_first(items: list[dict], query: str) -> list[dict]:
    """稳定排序：相关条目在前，无关条目在后（不删除任何条目）。

    limit 截断发生在排序之后，因此相关的条目会优先占满名额。
    关键词没有可用 token 时原样返回。
    """
    tokens = query_tokens(query)
    if not tokens or not items:
        return items
    return sorted(items, key=lambda it: 0 if is_relevant(it, tokens) else 1)
