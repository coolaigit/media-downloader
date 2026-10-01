# -*- coding: utf-8 -*-
"""成人站点来源（作为一类独立的视频来源）。

## 为什么必须单独接这些站

实测（2026-09-22，关键词「angela white anal」）——**两个通用引擎都拿不到成人站内容**：

| 途径 | 结果 |
|---|---|
| DuckDuckGo 视频接口 | **0 条**（对成人内容彻底过滤；连 `site:pornhub.com angela white` 也是 0 条） |
| Bing 视频搜索 | 30 条，其中 **29 条是 YouTube**，成人站 0 条 |
| Bing 关安全搜索 `adlt=off` | 无效，结果不变 |
| DDG 关安全搜索 `kp=-2` | 无效，仍是 0 条 |

用户搜「angela white anal」看到的全是采访、花絮（都在 YouTube 上），
这就是「搜成人内容却出不来成人站内容」的根因。

## 怎么解决（不写任何页面解析规则）

把**站点自己的公开检索入口**交给现成能力去解析：
  · 有公开搜索页且 yt-dlp 自带支持的（Pornhub）→ 交给 yt-dlp 的 extractor；
  · 有公开 JSON 接口的（Eporner）→ 直接调接口。

我们只提供「地址」，不解析任何页面 —— 与本项目「不写站点专用规则」一致，
与 B 站走 `wbi/search`、Dailymotion 走官方 REST 同一性质。

实测：`yt-dlp` 抓 pornhub 搜索页，不限条数要 30.9s（它在遍历整个列表），
加 `playlistend=20` 后 **2.4s / 20 条**，内容正确。

## 关于内容边界

本模块只做「按关键词检索公开成人视频站」这一件事：
不带任何年龄相关词，也不对未成年人相关内容做任何针对性处理 ——
这些站点本身禁止此类内容，工具不做超出「检索」的事。
"""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor

import requests

from .config import HEADERS
from .search_web_video import (
    ADULT_HOSTS,          # 域名总表在 search_web_video（与 FOREIGN_HOSTS 同处）
    SEARCH_TIMEOUT,
    TRANSIENT_ERRORS,
    _clean_title,
    _format_duration,
    _get_with_retry,
)

# ---------------------------------------------------------------- 站点登记

# 每项：显示名 + 域名 + 公开检索入口（含 {q} 占位）
# kind = "ytdlp"   → 把搜索页交给 yt-dlp 的 extractor 解析
# kind = "api"     → 直接调该站公开 JSON 接口
ADULT_SITES: dict[str, dict] = {
    "pornhub": {
        "label": "Pornhub",
        "hosts": ("pornhub.com",),
        "kind": "ytdlp",
        "search": "https://www.pornhub.com/video/search?search={q}",
    },
    "eporner": {
        "label": "Eporner",
        "hosts": ("eporner.com",),
        "kind": "api",
        "api": "https://www.eporner.com/api/v2/video/search/",
    },
    # 下面这两个的域名也登记进来，仅用于「识别结果是不是成人站」，
    # 但**不作为检索来源** —— 实测它们的搜索页 yt-dlp 不支持
    # （`Unsupported URL`），硬做就得自己写解析规则，违背设计。
    "xvideos": {"label": "XVideos", "hosts": ("xvideos.com",), "kind": None},
    "xhamster": {"label": "xHamster", "hosts": ("xhamster.com",), "kind": None},
    "spankbang": {"label": "SpankBang", "hosts": ("spankbang.com",), "kind": None},
    "xnxx": {"label": "XNXX", "hosts": ("xnxx.com",), "kind": None},
    "redtube": {"label": "RedTube", "hosts": ("redtube.com",), "kind": None},
    "youporn": {"label": "YouPorn", "hosts": ("youporn.com",), "kind": None},
    "hqporner": {"label": "HQPorner", "hosts": ("hqporner.com",), "kind": None},
    "txxx": {"label": "TXXX", "hosts": ("txxx.com",), "kind": None},
    "beeg": {"label": "Beeg", "hosts": ("beeg.com",), "kind": None},
    "porntrex": {"label": "PornTrex", "hosts": ("porntrex.com",), "kind": None},
    "tnaflix": {"label": "TNAFlix", "hosts": ("tnaflix.com",), "kind": None},
    "drtuber": {"label": "DrTuber", "hosts": ("drtuber.com",), "kind": None},
    "porn.com": {"label": "Porn.com", "hosts": ("porn.com",), "kind": None},
    "tube8": {"label": "Tube8", "hosts": ("tube8.com",), "kind": None},
    "motherless": {"label": "Motherless", "hosts": ("motherless.com",), "kind": None},
    "erome": {"label": "Erome", "hosts": ("erome.com",), "kind": None},
    "onlyfans": {"label": "OnlyFans", "hosts": ("onlyfans.com",), "kind": None},
    "brazzers": {"label": "Brazzers", "hosts": ("brazzers.com",), "kind": None},
}

# 可实际用作检索来源的站（有公开入口且现成能力吃得下）
ADULT_SEARCHABLE: tuple[str, ...] = tuple(
    k for k, v in ADULT_SITES.items() if v.get("kind")
)


# ------------------------------------------------------- 成人向关键词判定

# 常见成人词（英文按词边界匹配、中文按子串）。
#
# 用途：**只在用户搜成人内容时**才去查成人站 —— 否则每个普通关键词
# （「科比」「风景 航拍」）都多发两个请求，既慢又可能返回不相关内容。
#
# 注意：这里**刻意不含任何年龄相关词**。工具只做「按关键词检索公开成人站」，
# 不对未成年人相关内容做任何针对性处理。
ADULT_WORDS: tuple[str, ...] = (
    "porn", "porno", "pornography", "nsfw", "xxx", "x-rated",
    "sex", "sexual", "sexy", "hardcore", "softcore", "erotic", "erotica",
    "anal", "oral", "blowjob", "handjob", "cumshot", "creampie", "facial",
    "gangbang", "threesome", "foursome", "orgy", "double penetration",
    "milf", "gilf", "lesbian", "squirt", "deepthroat", "doggy", "missionary",
    "bdsm", "bondage", "fetish", "hentai", "cosplay sex", "roleplay sex",
    "nude", "naked", "boobs", "tits", "pussy", "dick", "cock", "penis",
    "vagina", "orgasm", "masturbat", "striptease", "stripping",
    "camgirl", "onlyfans", "escort", "amateur porn", "jav", "uncensored",
    "做爱", "性爱", "色情", "成人视频", "黄片", "裸体", "自慰", "援交",
    "无码", "情色",
)

_ADULT_WORD_RE = re.compile(
    r"(?<![a-z0-9])(" + "|".join(re.escape(w) for w in ADULT_WORDS
                                 if w.isascii()) + r")(?![a-z0-9])",
    re.I,
)
_ADULT_CJK = tuple(w for w in ADULT_WORDS if not w.isascii())

# 成人站品牌名：用户直接搜站名时也算成人向。
#
# 为什么单独列：`porn` 在词表里有词边界限制（避免匹配到别的单词内部），
# 于是 **「pornhub」匹配不到里面的「porn」** —— 实测踩到（自测里 FAIL 过）。
# 这些名字几乎不可能出现在普通查询里，按子串匹配是安全的。
ADULT_BRANDS: tuple[str, ...] = (
    "pornhub", "eporner", "xvideos", "xvideo", "xhamster", "spankbang",
    "xnxx", "redtube", "youporn", "hqporner", "porntrex", "tnaflix",
    "drtuber", "tube8", "motherless", "onlyfans", "brazzers", "erome",
    "porn300", "porndig", "javhd", "javbus",
)


def is_adult_query(keyword: str) -> bool:
    """关键词是否属于成人向（用于决定要不要去查成人站）。

    三个来源任一命中即算：
      ① 成人词（英文按**词边界**匹配，避免 "sexton" 被 "sex" 误命中）；
      ② 中文成人词（按子串）；
      ③ 成人站品牌名（用户直接搜站名，如「pornhub」）。
    """
    q = (keyword or "").strip().lower()
    if not q:
        return False
    if _ADULT_WORD_RE.search(q):
        return True
    if any(w in q for w in _ADULT_CJK):
        return True
    return any(b in q for b in ADULT_BRANDS)


def is_adult_url(url: str) -> bool:
    """地址是否来自成人站（用于识别与归类，按域名后缀匹配）。"""
    from urllib.parse import urlparse

    try:
        h = (urlparse(url or "").hostname or "").lower()
    except ValueError:
        return False
    if h.startswith("www."):
        h = h[4:]
    return any(h == p or h.endswith("." + p) for p in ADULT_HOSTS)


# ---------------------------------------------------------------- 检索实现

def _norm_entry(id_: str, title: str, url: str, thumb: str, dur, label: str,
                uploader: str = "") -> dict | None:
    if not (url or "").startswith("http"):
        return None
    try:
        dur_i = int(dur) if dur is not None else None
    except (TypeError, ValueError):
        dur_i = None
    return {
        "id": id_ or "",
        "title": _clean_title(title or ""),
        "duration": dur_i,
        "duration_string": _format_duration(dur_i),
        "webpage_url": url,
        "thumbnail": thumb or "",
        "uploader": _clean_title(uploader or ""),
        "source_site": label,
        "view_count": None,
        "source": "adult",
    }


def _search_via_ytdlp(name: str, url: str, count: int,
                      timeout: int) -> tuple[list[dict], str | None]:
    """把站点搜索页交给 yt-dlp 的 extractor（我们只提供地址、不解析页面）。"""
    import yt_dlp

    cfg = ADULT_SITES[name]
    opts = {
        "quiet": True, "no_warnings": True, "skip_download": True,
        "extract_flat": "in_playlist",
        # ★ 关键：不限制条数时 yt-dlp 会遍历整个列表（实测 30.9s），
        #   加上它之后 2.4s 就返回同样正确的前 N 条。
        "playlistend": max(1, count),
        "socket_timeout": timeout,
        "retries": 1, "extractor_retries": 1,
        "ignoreerrors": True, "noprogress": True,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as y:
            info = y.extract_info(url, download=False)
    except Exception as e:  # noqa: BLE001
        return [], f"{type(e).__name__}: {str(e)[:90]}"

    entries = [e for e in ((info or {}).get("entries") or []) if e]
    items: list[dict] = []
    seen: set[str] = set()
    for e in entries:
        page = e.get("webpage_url") or e.get("url") or ""
        if not page.startswith("http"):
            continue
        if page in seen:
            continue
        seen.add(page)
        thumbs = e.get("thumbnails") or []
        thumb = e.get("thumbnail") or (thumbs[0].get("url") if thumbs else "") or ""
        it = _norm_entry(str(e.get("id") or ""), e.get("title") or "", page,
                         thumb, e.get("duration"), cfg["label"],
                         e.get("uploader") or e.get("channel") or "")
        if it:
            items.append(it)
    if not items:
        return [], f"{cfg['label']} 没有该关键词的结果"
    return items, None


def _search_via_api(name: str, keyword: str, count: int,
                    timeout: int) -> tuple[list[dict], str | None]:
    """走该站公开 JSON 接口（Eporner）。"""
    cfg = ADULT_SITES[name]
    try:
        r = _get_with_retry(
            cfg["api"],
            headers={**HEADERS, "Accept": "application/json"},
            params={"query": keyword, "per_page": min(100, max(1, count)),
                    "page": 1, "thumbsize": "medium", "format": "json"},
            timeout=timeout,
        )
    except TRANSIENT_ERRORS as e:
        return [], f"请求失败: {type(e).__name__}（已重试 3 次）"
    except requests.exceptions.RequestException as e:
        return [], f"请求失败: {type(e).__name__}"

    if r.status_code >= 400:
        return [], f"{cfg['label']} 返回 HTTP {r.status_code}"
    try:
        data = r.json()
    except ValueError:
        return [], f"{cfg['label']} 返回的不是 JSON"

    items: list[dict] = []
    for v in (data.get("videos") or []):
        if not isinstance(v, dict):
            continue
        thumbs = v.get("thumbs") or []
        thumb = ""
        if isinstance(thumbs, list) and thumbs:
            first = thumbs[0]
            thumb = first.get("src") if isinstance(first, dict) else str(first)
        it = _norm_entry(str(v.get("id") or ""), v.get("title") or "",
                         v.get("url") or "", thumb, v.get("length_sec"),
                         cfg["label"])
        if it:
            items.append(it)
    if not items:
        return [], f"{cfg['label']} 没有该关键词的结果"
    return items, None


def search_adult_videos(keyword: str, count: int = 20,
                        timeout: int = SEARCH_TIMEOUT,
                        sites: tuple[str, ...] | None = None
                        ) -> tuple[list[dict], str | None, dict]:
    """按关键词检索成人站，返回 (条目, 错误, 各站明细)。

    多个站**并行**（单站失败不影响其它站）。
    """
    keyword = (keyword or "").strip()
    if not keyword:
        return [], "请输入关键词", {}

    names = tuple(sites) if sites else ADULT_SEARCHABLE
    names = tuple(n for n in names if ADULT_SITES.get(n, {}).get("kind"))
    if not names:
        return [], "没有可用的成人站来源", {}

    from urllib.parse import quote
    q = quote(keyword)

    detail: dict[str, dict] = {}
    groups: list[list[dict]] = []
    errs: list[str] = []

    def _one(name: str):
        cfg = ADULT_SITES[name]
        if cfg["kind"] == "ytdlp":
            return _search_via_ytdlp(name, cfg["search"].format(q=q), count, timeout)
        return _search_via_api(name, keyword, count, timeout)

    with ThreadPoolExecutor(max_workers=max(2, len(names))) as pool:
        futs = {n: pool.submit(_one, n) for n in names}
        for n, fut in futs.items():
            label = ADULT_SITES[n]["label"]
            try:
                got, err = fut.result()
            except Exception as e:  # noqa: BLE001
                detail[n] = {"count": 0, "error": f"{type(e).__name__}: {e}"}
                errs.append(f"{label}: {type(e).__name__}")
                continue
            detail[n] = {"count": len(got), "error": err}
            if got:
                groups.append(got)
            elif err:
                errs.append(f"{label}: {err}")

    if not groups:
        return [], ("；".join(errs[:3]) or "成人站没有该关键词的结果"), detail

    # 轮流拼接，避免某一个站占满
    out: list[dict] = []
    idx = 0
    while True:
        added = False
        for g in groups:
            if idx < len(g):
                out.append(g[idx])
                added = True
        if not added:
            break
        idx += 1
    return out, None, detail
