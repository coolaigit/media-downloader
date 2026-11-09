# -*- coding: utf-8 -*-
"""海外免费替代源：YouTube（yt-dlp 搜索 + 下载）。

为什么需要它：
  国内影视资源站覆盖不到记录片、冷门剧、部分综艺（实测《人间世 第二季》
  资源站只有第一季）。有代理的用户可以从 YouTube 拿到同片完整版，
  所以把它做成「换源」的第四层兜底。

匹配策略（不靠平台专用接口，只靠标题/时长/集号）：
  - 片名相似度（复用 resource_sites 的归一与打分）
  - 季号必须一致（第二季不能拿第一季）
  - 集号一致（第 N 集 / EP N / 第 N 期）
  - 时长与标称时长比对：YouTube 常带片头片尾，容忍 -20% ~ +30%
  - 排除解说/预告/盘点/切片类（标题关键词）
  - 官方频道轻微加分

代理：设置里的 download_proxy（http://127.0.0.1:7890）只对海外源生效，
不影响国内资源站下载。
"""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import resource_sites

_CLIP_HINTS = ("解说", "预告", "花絮", "盘点", "合集", "片段", "混剪", "速看",
               "影评", "reaction", "trailer", "teaser", "review", "shorts",
               "cut", "clip", "解析")
_OFFICIAL_HINTS = ("官方", "official", "卫视", "tv", "channel", "纪录片",
                   "电视剧", "drama")


def _ydl_opts(proxy: str = "") -> dict:
    from .config import find_js_runtime

    opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": "in_playlist",
        "skip_download": True,
        "noplaylist": True,
        "socket_timeout": 20,
        "retries": 1,
        "extractor_retries": 1,
        "ignoreerrors": True,
    }
    if proxy:
        opts["proxy"] = proxy
    js = find_js_runtime()
    if js:
        opts["js_runtimes"] = {js[0]: {"path": js[1]}}
    return opts


_EP_MARK_RE = re.compile(r"第\s*[0-9零〇一二两三四五六七八九十百]{1,6}\s*[集话話期回]")


def _series_title(title: str) -> str:
    """从单集标题里取出「剧名」。

    实测优酷单集标题形如「谷雨 第2集 李阿红接手假发票印刷厂」——
    整串拿去 YouTube 搜是 0 条；必须截到「第X集」之前，只搜「谷雨」。
    """
    from .vip_unlock import title_usable

    t = title or ""
    m = _EP_MARK_RE.search(t)
    if m and m.start() >= 2:
        t = t[:m.start()]
    return title_usable(t)


def _queries(title: str, episode: int | None) -> list[str]:
    base = _series_title(title)
    if not base:
        return []
    qs: list[str] = []
    if episode:
        qs += [f"{base} 第{episode}集", f"{base} EP{episode:02d}",
               f"{base} 第{episode}期"]
    qs.append(base)
    out: list[str] = []
    for q in qs:
        if q not in out:
            out.append(q)
    return out


def _entry_url(e: dict) -> str:
    u = str(e.get("webpage_url") or e.get("url") or "")
    if u.startswith("http"):
        return u
    vid = str(e.get("id") or "")
    return f"https://www.youtube.com/watch?v={vid}" if vid else ""


def _score(e: dict, raw_title: str, episode: int | None,
           expected: float) -> float:
    t = str(e.get("title") or "")
    # 相似度只比「剧名」，不把单集的剧情小标题算进去
    # （实测优酷标题带「李阿红接手假发票印刷厂」，整串比会把 EP02 压到 60 分以下）。
    s = resource_sites.similarity(_series_title(raw_title), t)
    want_season = resource_sites.parse_season(raw_title)
    if want_season:
        cs = resource_sites.parse_season(t)
        if cs and cs != want_season:
            s -= 50.0
        elif not cs:
            s -= 5.0
    if episode is not None:
        ce = resource_sites.parse_episode(t)
        # 找某一集时，标题里没有集号 = 大概率是合集/切片，不能拿它当第 N 集；
        # 集号对不上也直接判死（实测否则会拿 S02EP06 当第 1 集下回来）。
        if ce is None or ce != episode:
            return -100.0
        s += 30.0
    dur = float(e.get("duration") or 0)
    if expected > 0 and dur > 0:
        ratio = dur / expected
        if 0.8 <= ratio <= 1.3:
            s += 25.0
        elif 0.55 <= ratio <= 1.6:
            s += 5.0
        else:
            s -= 40.0
    low = t.lower()
    if any(h in low for h in _CLIP_HINTS):
        s -= 60.0
    if any(h in low for h in _OFFICIAL_HINTS):
        s += 8.0
    return round(s, 1)


def search(title: str, episode: int | None = None, expected: float = 0.0,
           proxy: str = "", limit: int = 6, timeout: float = 25.0) -> list[dict]:
    """在 YouTube 搜同片，返回按匹配度排序的候选。"""
    import yt_dlp

    queries = _queries(title, episode)
    if not queries:
        return []
    found: dict[str, dict] = {}
    opts = _ydl_opts(proxy)

    def _one(q: str):
        with yt_dlp.YoutubeDL(opts) as y:
            d = y.extract_info(f"ytsearch10:{q}", download=False)
        return (d or {}).get("entries") or []

    deadline = timeout
    ex = ThreadPoolExecutor(max_workers=2)
    try:
        futs = [ex.submit(_one, q) for q in queries[:2]]
        for fut in as_completed(futs, timeout=deadline):
            try:
                for e in fut.result() or []:
                    if not isinstance(e, dict):
                        continue
                    key = str(e.get("id") or _entry_url(e))
                    if not key or key in found:
                        continue
                    url = _entry_url(e)
                    if not url:
                        continue
                    found[key] = {
                        "url": url,
                        "title": str(e.get("title") or ""),
                        "duration": float(e.get("duration") or 0),
                        "channel": str(e.get("channel") or e.get("uploader") or ""),
                        "view_count": e.get("view_count"),
                        "id": e.get("id"),
                    }
            except Exception:  # noqa: BLE001 —— 单个查询失败不影响另一个
                continue
    except TimeoutError:
        pass
    finally:
        for f in futs:
            if not f.done():
                f.cancel()
        ex.shutdown(wait=False, cancel_futures=True)

    for it in found.values():
        it["score"] = _score(it, title, episode, expected)
    ranked = sorted(found.values(), key=lambda x: -x["score"])
    return [x for x in ranked if x["score"] >= 60][:limit]


def resolve(meta: dict, proxy: str = "") -> dict:
    """给 VIP 页面的元数据找海外同片。"""
    title = str(meta.get("title") or "")
    if not title:
        return {"ok": False, "error": "没有标题，无法搜索海外源"}
    episode = meta.get("episode")
    expected = float(meta.get("duration") or 0)
    items = search(title, episode=episode, expected=expected, proxy=proxy)
    if not items:
        return {"ok": False,
                "error": "YouTube 没有匹配到同一集（片名/集号/时长都对不上）"}
    best = items[0]
    return {
        "ok": True,
        "url": best["url"],
        "urls": [{"url": x["url"], "referer": "", "site": "YouTube"} for x in items],
        "backend": "youtube",
        "site": "YouTube",
        "vod_name": best["title"],
        "episode": episode,
        "duration": best["duration"],
        "score": best["score"],
        "candidates": [{"site": "YouTube", "name": x["title"][:60],
                        "year": "", "remarks": x.get("channel") or "",
                        "score": x["score"]} for x in items[:5]],
    }
