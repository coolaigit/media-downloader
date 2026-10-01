# -*- coding: utf-8 -*-
"""关键词搜图引擎：Bing 主源 + DuckDuckGo 备用，支持清晰度/颜色/长宽比/类型/时间等筛选。

设计说明：
- Bing 用公开的 images/async 接口，解析每张卡片 m 属性里的 murl(原图)/turl(缩略图)/purl(来源页)。
- 搜索接口的卡片 HTML 不含可靠的原图像素尺寸，因此「清晰度」筛选走两级：
  先用 qft 粗筛（大图/壁纸等），再用 probe_media 并发探测真实宽高做精确过滤。
- DuckDuckGo 作为备用源（ddgs 库），在网络可达时提供额外结果。
"""
from __future__ import annotations

import html
import json
import os
import re
import time
from urllib.parse import urljoin

import requests

from .config import HEADERS, TIMEOUT
from .probe_media import probe_many
from .relevance import audit, query_tokens, relevance_first

# ---------- 筛选项到 Bing qft token 的映射（token 已从 Bing 页面 UI 实测提取） ----------

SIZE_MAP = {
    "small": "+filterui:imagesize-small",
    "medium": "+filterui:imagesize-medium",
    "large": "+filterui:imagesize-large",
    "wallpaper": "+filterui:imagesize-wallpaper",
}

ASPECT_MAP = {
    "wide": "+filterui:aspect-wide",
    "square": "+filterui:aspect-square",
    "tall": "+filterui:aspect-tall",
}

TYPE_MAP = {
    "photo": "+filterui:photo-photo",
    "clipart": "+filterui:photo-clipart",
    "linedrawing": "+filterui:photo-linedrawing",
    "animatedgif": "+filterui:photo-animatedgif",
    "transparent": "+filterui:photo-transparent",
}

# 发布时间：分钟阈值 -> Bing age token
TIME_MAP = {
    "day": "+filterui:age-lt1440",       # 24 小时内
    "week": "+filterui:age-lt10080",     # 7 天内
    "month": "+filterui:age-lt43200",    # 30 天内
    "year": "+filterui:age-lt525600",    # 365 天内
}

COLOR_MAP = {
    "color": "+filterui:color2-color",
    "bw": "+filterui:color2-bw",
    "red": "+filterui:color2-FGcls_RED",
    "orange": "+filterui:color2-FGcls_ORANGE",
    "yellow": "+filterui:color2-FGcls_YELLOW",
    "green": "+filterui:color2-FGcls_GREEN",
    "teal": "+filterui:color2-FGcls_TEAL",
    "blue": "+filterui:color2-FGcls_BLUE",
    "purple": "+filterui:color2-FGcls_PURPLE",
    "pink": "+filterui:color2-FGcls_PINK",
    "brown": "+filterui:color2-FGcls_BROWN",
    "black": "+filterui:color2-FGcls_BLACK",
    "gray": "+filterui:color2-FGcls_GRAY",
    "white": "+filterui:color2-FGcls_WHITE",
}

FACE_MAP = {
    "face": "+filterui:face-face",
    "portrait": "+filterui:face-portrait",
}

LICENSE_MAP = {
    "any": "+filterui:licenseType-Any",
    "public": "+filterui:license-L1",
    "share": "+filterui:license-L2_L3",
    "share_modify": "+filterui:license-L2_L3_L5_L6",
    "all_cc": "+filterui:license-L2_L3_L4_L5_L6_L7",
}

# 分辨率下限（短边像素），用于探测后的精确过滤
RES_PRESETS = {
    "any": 0,
    "sd": 480,      # >=480p
    "hd": 720,      # >=720p
    "fullhd": 1080,  # >=1080p
    "2k": 1440,
    "4k": 2160,
}

_IUSC_RE = re.compile(r'class="iusc"[^>]*?\sm="(.*?)"', re.S)
_MURL_RE = re.compile(r'"murl"\s*:\s*"(.*?)"')
_TURL_RE = re.compile(r'"turl"\s*:\s*"(.*?)"')
_PURL_RE = re.compile(r'"purl"\s*:\s*"(.*?)"')
_TITLE_RE = re.compile(r'"t"\s*:\s*"(.*?)"')
_DESC_RE = re.compile(r'"desc"\s*:\s*"(.*?)"')


def _decode_m(raw: str) -> str:
    """卡片 m 属性是 HTML 转义过的 JSON 字符串，先反转义。"""
    s = html.unescape(raw)
    # Bing 内部还会把引号写成 &quot;，unescape 已处理；这里兼容残留
    return s.replace("&quot;", '"')


def build_qft(filters: dict) -> str:
    """把筛选项字典拼成 Bing 的 qft 参数（自动去重，保持顺序）。"""
    parts: list[str] = []
    for key, mapping in (
        ("size", SIZE_MAP),
        ("aspect", ASPECT_MAP),
        ("type", TYPE_MAP),
        ("time", TIME_MAP),
        ("color", COLOR_MAP),
        ("face", FACE_MAP),
        ("license", LICENSE_MAP),
    ):
        val = (filters.get(key) or "").strip().lower()
        if val and val in mapping:
            parts.append(mapping[val])
    # 自定义分辨率：若用户未手动选尺寸，则按分辨率追加 Bing 尺寸粗筛，减少无效探测
    if not any(p.startswith("+filterui:imagesize") for p in parts):
        min_res = int(filters.get("min_resolution") or 0)
        if min_res >= 1080:
            parts.append(SIZE_MAP["wallpaper"])
        elif min_res >= 720:
            parts.append(SIZE_MAP["large"])
    # 去重并保持顺序
    seen = set()
    uniq = []
    for p in parts:
        if p not in seen:
            seen.add(p)
            uniq.append(p)
    return "".join(uniq)


def search_bing(
    query: str,
    filters: dict | None = None,
    limit: int = 40,
    region: str = "zh-CN",
    timeout: int = TIMEOUT,
    session: requests.Session | None = None,
) -> tuple[list[dict], str | None]:
    """Bing 图片搜索。返回 (结果列表, 错误信息)。

    session 可注入：便于复用已建立偏好的会话，也便于测试时替换。
    """
    filters = filters or {}
    qft = build_qft(filters)
    safe = (filters.get("safesearch") or "moderate").lower()
    results: list[dict] = []
    err: str | None = None

    sess = session or requests.Session()
    sess.headers.update(HEADERS)
    if safe == "off":
        # 只传 adlt=off 效果有限：实测 off 与 moderate 的结果仍有约 3/4 重合，
        # 而用户对照过「在 bing.com 网页上关掉安全搜索就正常」。
        # 原因是 Bing 的安全等级主要记在 cookie（SRCHHPGUSR 的 ADLT 字段）上，
        # URL 参数只在部分场景生效。这里显式写入 cookie 强制放开。
        sess.cookies.set("SRCHHPGUSR", "ADLT=OFF", domain=".bing.com")
    try:
        first = 1
        per_page = min(35, max(limit, 10))
        guard = 0
        while len(results) < limit and guard < 8:
            guard += 1
            params = {
                "q": query,
                "first": first,
                "count": per_page,
                "mmasync": 1,
                # 三档都要正确映射。原写法是 "strict" if safe == "strict" else "moderate"，
                # 把「关闭」也映射成了 moderate —— 所以用户在面板里选「关闭」时，
                # 发出去的仍是中度过滤，这正是他反馈「关了也没用」的原因。
                "adlt": {"off": "off", "strict": "strict"}.get(safe, "moderate"),
            }
            if qft:
                params["qft"] = qft
            # 单次请求带退避重试：Bing 偶发返回空卡片或瞬时网络错误
            r = None
            last_exc: Exception | None = None
            for attempt in range(3):
                try:
                    r = sess.get(
                        "https://www.bing.com/images/async",
                        params=params,
                        timeout=timeout,
                    )
                    if r.status_code == 200 and _IUSC_RE.search(r.text):
                        break
                except requests.exceptions.RequestException as e:
                    last_exc = e
                    r = None
                time.sleep(0.6 * (attempt + 1))
            if r is None:
                err = f"Bing 请求失败（已重试3次）: {type(last_exc).__name__ if last_exc else '空响应'}"
                break
            if r.status_code != 200:
                err = f"Bing 返回状态 {r.status_code}"
                break
            cards = _IUSC_RE.findall(r.text)
            if not cards:
                break
            for raw in cards:
                if len(results) >= limit:
                    break
                s = _decode_m(raw)
                m_murl = _MURL_RE.search(s)
                if not m_murl:
                    continue
                murl = html.unescape(m_murl.group(1))
                if not murl.startswith("http"):
                    continue
                turl_m = _TURL_RE.search(s)
                purl_m = _PURL_RE.search(s)
                title_m = _TITLE_RE.search(s)
                desc_m = _DESC_RE.search(s)
                title = ""
                if title_m:
                    title = html.unescape(title_m.group(1))
                elif desc_m:
                    title = html.unescape(desc_m.group(1))
                thumb = html.unescape(turl_m.group(1)) if turl_m else ""
                source_page = html.unescape(purl_m.group(1)) if purl_m else ""
                results.append(
                    {
                        "url": murl,
                        "thumbnail": thumb,
                        "source_page": source_page,
                        "title": title.strip(),
                        "source_site": _domain_of(source_page or murl),
                        "referer": source_page or "",
                        "engine": "bing",
                    }
                )
            if len(cards) < 3:
                break
            first += per_page
            time.sleep(0.25)
    finally:
        if session is None:          # 注入进来的会话由调用方负责关闭
            sess.close()
    return results, err


def search_ddg(
    query: str,
    filters: dict | None = None,
    limit: int = 30,
    timeout: int = TIMEOUT,
) -> tuple[list[dict], str | None]:
    """DuckDuckGo 备用搜图（ddgs 库）。网络不可达时返回错误而不抛异常。"""
    filters = filters or {}
    try:
        from ddgs import DDGS
    except ImportError:
        return [], "ddgs 库未安装"

    kwargs: dict = {"max_results": max(limit, 5)}
    safe = (filters.get("safesearch") or "moderate").lower()
    kwargs["safesearch"] = {"off": "off", "moderate": "moderate", "strict": "on"}.get(safe, "moderate")
    # ddgs 9.x 参数名（实测签名）
    size_val = (filters.get("size") or "").lower()
    if size_val in ("small", "medium", "large", "wallpaper"):
        kwargs["size_image"] = size_val.capitalize() if size_val != "wallpaper" else "Wallpaper"
    color_val = (filters.get("color") or "").lower()
    if color_val and color_val not in ("color", "bw"):
        kwargs["color"] = color_val.capitalize()
    type_val = (filters.get("type") or "").lower()
    if type_val in ("photo", "clipart", "linedrawing", "transparent", "animatedgif"):
        kwargs["type_image"] = "photo" if type_val == "photo" else type_val
    time_val = (filters.get("time") or "").lower()
    if time_val in ("day", "week", "month", "year"):
        kwargs["time"] = time_val.capitalize()
    layout = (filters.get("aspect") or "").lower()
    if layout in ("wide", "square", "tall"):
        kwargs["layout"] = layout.capitalize()

    results: list[dict] = []
    try:
        # ddgs 底层用 primp(Rust) 发请求，默认**不读**系统代理环境变量；
        # 直连时上游可能把请求降级，返回与关键词无关的结果。这里显式把项目在用
        # 的代理传进去，保证它与其它请求走同一条链路。
        ddgs_kwargs: dict = {"timeout": timeout}
        proxy = (
            os.environ.get("HTTPS_PROXY")
            or os.environ.get("https_proxy")
            or os.environ.get("HTTP_PROXY")
            or os.environ.get("http_proxy")
        )
        if proxy:
            ddgs_kwargs["proxy"] = proxy
        with DDGS(**ddgs_kwargs) as d:
            items = d.images(query, **kwargs)
        for it in items[:limit]:
            u = it.get("image") or it.get("url")
            if not u:
                continue
            results.append(
                {
                    "url": u,
                    "thumbnail": it.get("thumbnail") or "",
                    "source_page": it.get("source") or "",
                    "title": (it.get("title") or "").strip(),
                    "source_site": _domain_of(it.get("source") or u),
                    "referer": it.get("source") or "",
                    "engine": "ddg",
                }
            )
        return results, None
    except Exception as e:  # noqa: BLE001
        return [], f"DuckDuckGo 不可用: {type(e).__name__}"


def _domain_of(url: str) -> str:
    try:
        m = re.match(r"https?://([^/]+)", url or "")
        return m.group(1) if m else ""
    except Exception:
        return ""


def _status_text(err: str | None, items: list[dict], rep: dict) -> str:
    """把「错误 / 条数 / 相关性审计结果」拼成一句可展示的引擎状态。"""
    if err:
        return err
    n = len(items)
    if rep.get("blocked"):
        return f"结果与关键词无关，已拦截 {rep['blocked']} 条"
    hits = rep.get("hits")
    if rep.get("checked") and hits is not None and hits < n:
        return f"ok:{n}（其中 {hits} 条与关键词相关）"
    return f"ok:{n}"


def search_images(
    query: str,
    filters: dict | None = None,
    limit: int = 40,
    probe: bool = True,
    probe_workers: int = 16,
) -> dict:
    """搜索主入口。

    返回 {items, meta}。meta 含各引擎状态与过滤统计。
    """
    filters = filters or {}
    engines = [e.strip() for e in (filters.get("engines") or "bing,ddg").split(",") if e.strip()]
    min_res = int(filters.get("min_resolution") or 0)
    max_res = int(filters.get("max_resolution") or 0)
    min_size_kb = int(filters.get("min_size_kb") or 0)
    exact_formats = {f.strip().lower() for f in (filters.get("formats") or "").split(",") if f.strip()}

    # 分辨率门槛越高，Bing 首页命中率越低，需抓取更多候选来保证过滤后仍有结果
    fetch_multiplier = 2
    if min_res >= 2160:
        fetch_multiplier = 6
    elif min_res >= 1440:
        fetch_multiplier = 4
    elif min_res >= 1080:
        fetch_multiplier = 3

    raw: list[dict] = []
    engine_status: dict[str, str] = {}
    relevance_reports: dict[str, dict] = {}
    blocked_total = 0
    fallback_used = False

    def _collect(name: str, items: list[dict], err: str | None) -> None:
        """收录某引擎的结果：先做相关性审计，明显跑偏的整批拦下不展示。"""
        nonlocal blocked_total
        kept, rep = audit(items, query)
        relevance_reports[name] = rep
        blocked_total += int(rep.get("blocked") or 0)
        engine_status[name] = _status_text(err, items, rep)
        raw.extend(kept)

    if "bing" in engines:
        items, err = search_bing(query, filters, limit=max(limit * fetch_multiplier, 60))
        _collect("bing", items, err)
    if "google" in engines:
        # Google 需浏览器渲染（纯 HTTP 只返回 enablejs 空壳），耗时较长，
        # 但结果自带真实宽高与文件体积，可跳过最慢的逐个探测环节
        try:
            from .search_google import search_google
            items, err = search_google(query, filters, limit=max(limit * 2, 40))
        except Exception as e:  # noqa: BLE001
            items, err = [], f"{type(e).__name__}: {e}"
        _collect("google", items, err)
    if "ddg" in engines:
        items, err = search_ddg(query, filters, limit=limit * 2)
        _collect("ddg", items, err)

    # 所有引擎都被判定为「与关键词无关」时，用 Bing 再兜底一次。
    # 实测 DDG 在链路异常时会拿回另一个查询的结果（vqd 与实际查询错配），
    # 此时换一条相对可靠的链路重试，比直接把脏结果给用户更有意义。
    if not raw and blocked_total and "bing" not in engines:
        fallback_used = True
        items, err = search_bing(query, filters, limit=max(limit * fetch_multiplier, 60))
        _collect("bing（自动兜底）", items, err)

    # 去重（按原图 URL）
    seen: set[str] = set()
    uniq: list[dict] = []
    for it in raw:
        u = it.get("url")
        if not u or u in seen:
            continue
        seen.add(u)
        uniq.append(it)

    # 扩展名初筛（仅在有明确要求且能从 URL 判断时）
    if exact_formats:
        kept = []
        for it in uniq:
            ext = _ext_of(it["url"]).lstrip(".").lower()
            if ext in exact_formats or not ext:
                kept.append(it)
        uniq = kept

    # 自带真实元数据的条目（Google）可免去探测：
    # 探测是整个搜索里最慢的环节（要对每张原图发请求读文件头解析宽高）
    prefetched = [it for it in uniq if it.get("width") and it.get("height")]
    need_probe = [it for it in uniq if not (it.get("width") and it.get("height"))]

    def _passes(w: int, h: int, size_bytes, fmt) -> bool:
        """按用户筛选条件判定单条结果是否通过。"""
        short_side = min(w, h) if w and h else 0
        if min_res and short_side < min_res:
            return False
        if max_res and short_side > max_res:
            return False
        if min_size_kb and size_bytes and size_bytes < min_size_kb * 1024:
            return False
        if exact_formats and fmt and str(fmt).lower() not in exact_formats:
            return False
        return True

    need_filter = probe and (min_res > 0 or max_res > 0 or min_size_kb > 0 or bool(exact_formats))
    probed_count = 0

    if need_filter:
        # 1) 自带元数据的直接过滤，不发探测请求
        filtered: list[dict] = [
            it for it in prefetched
            if _passes(it.get("width", 0), it.get("height", 0),
                       it.get("size_bytes"), it.get("format"))
        ]
        # 2) 其余按需探测；已有足够结果时减少探测量以提速
        if need_probe and len(filtered) < limit:
            # 门槛越高、命中越稀疏，需探测越多候选
            if min_res >= 2160 or max_res > 0:
                probe_target = len(need_probe)        # 4K 或设了上限：全探
            elif min_res >= 1080:
                probe_target = min(len(need_probe), max(limit * 4, 48))
            else:
                probe_target = min(len(need_probe), max(limit * 2, limit + 24))
            batch = need_probe[:probe_target]
            outcome = probe_many(
                [{"url": b["url"], "referer": b.get("referer")} for b in batch],
                max_workers=probe_workers,
            )
            probed_count = len(batch)
            for item, pr in zip(batch, outcome.results):
                if not pr.ok:
                    continue
                w, h = pr.width or 0, pr.height or 0
                if not _passes(w, h, pr.size_bytes, pr.fmt):
                    continue
                item = dict(item)
                item["width"] = w
                item["height"] = h
                item["size_bytes"] = pr.size_bytes
                item["format"] = (pr.fmt or "").lower() or None
                filtered.append(item)
        # 自带元数据的排前（最可信），再按分辨率从高到低
        uniq = sorted(
            filtered,
            key=lambda x: (-min(x.get("width", 0), x.get("height", 0)), x["url"]),
        )
    elif probe:
        # 无过滤条件时，只对缺元数据的条目探测一批，用于前端展示分辨率
        batch = need_probe[: min(len(need_probe), limit)]
        if batch:
            outcome = probe_many(
                [{"url": b["url"], "referer": b.get("referer")} for b in batch],
                max_workers=probe_workers,
            )
            probed_count = len(batch)
            for item, pr in zip(batch, outcome.results):
                if pr.ok:
                    item["width"] = pr.width
                    item["height"] = pr.height
                    item["size_bytes"] = pr.size_bytes
                    item["format"] = (pr.fmt or "").lower() or None
        uniq = prefetched + need_probe

    # 相关条目优先占名额（稳定排序，不删除任何条目；limit 截断发生在其后）
    uniq = relevance_first(uniq, query)

    return {
        "items": uniq[:limit],
        "meta": {
            "query": query,
            "engine_status": engine_status,
            "candidates": len(raw),
            "unique": len(uniq),
            "probed": probed_count,
            "filters_applied": {
                "min_resolution": min_res,
                "max_resolution": max_res,
                "min_size_kb": min_size_kb,
                "formats": sorted(exact_formats),
            },
            "qft": build_qft(filters),
            "relevance": {
                "tokens": query_tokens(query),
                "engines": relevance_reports,
                "blocked": blocked_total,
                "fallback_used": fallback_used,
                "all_blocked": bool(blocked_total and not uniq),
            },
        },
    }


def _ext_of(url: str) -> str:
    m = re.search(r"\.([a-zA-Z0-9]{2,5})(?:\?|#|$)", url or "")
    return m.group(1) if m else ""


def resolution_preset(name: str) -> int:
    return RES_PRESETS.get((name or "any").lower(), 0)


# 供前端下拉框使用的选项定义（单一事实来源）
FILTER_OPTIONS = {
    "size": list(SIZE_MAP.keys()),
    "aspect": list(ASPECT_MAP.keys()),
    "type": list(TYPE_MAP.keys()),
    "time": list(TIME_MAP.keys()),
    "color": list(COLOR_MAP.keys()),
    "face": list(FACE_MAP.keys()),
    "license": list(LICENSE_MAP.keys()),
    "resolution_preset": list(RES_PRESETS.keys()),
}
