# -*- coding: utf-8 -*-
"""Google 图片搜索引擎 —— 两条通道，按可靠性排序自动降级。

为什么不能只有一条路（2026-11 实测，本机出口为美国机房 IP 45.66.157.223）：

  | 通道        | 结果 |
  |------------|------|
  | 纯 HTTP 抓   | Google 只回 enablejs 空壳（93KB、0 张图）；`gbv=1` 老接口返回
  |            | "Update your browser"，`/images/async` 已 404 —— 纯 HTTP 路线已死 |
  | 浏览器渲染    | 导航即 302 到 `/sorry/index`（"系统检测到您的计算机网络存在异常流量"），
  |            | 页面标题直接把出口 IP 写出来 —— 机房 IP 被整体判定为机器人 |

2026-11-05 复核：Google 官方 Custom Search JSON API 已**对新客户关闭**
（官方原文 "The Custom Search JSON API is closed to new customers"，
存量客户 2027-01-01 停服），新项目拿不到 key。所以 API 通道连同设置项一并移除，
不再给用户一个「看起来能配、实际配不成」的入口。

所以本模块按「浏览器渲染 → SearXNG 代理」两档降级：
  1. render ：真实内核渲染 + 从 DOM 的 `/imgres?imgurl=` 链接取原图。
              住家 IP / 干净节点下可用；设置 google_proxy 可单独给这条通道挂代理。
  2. searx  ：用户自备 SearXNG 实例（`google_searx`），由实例服务器去抓 Google。
              适合「本机怎么都过不了 /sorry」但手头有可用实例的情况。

两条都失败时，错误信息会把每条通道的真实原因摊开，不再只给一句「Google 不可用」。

数据结构（两条通道统一成同一份字段）：
  url / thumbnail / source_page / title / source_site / referer / engine
  width / height / size_bytes / size_text / format   ← 有就带上，能省掉最慢的原图探测
"""
from __future__ import annotations

import html as _html
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, quote, urlencode, urljoin, urlparse

import requests

from .config import DEFAULT_UA, HEADERS, TIMEOUT
from .renderer import render_page, renderer_status
from . import store

# 三元组：["URL",宽,高]
_TRIPLE_RE = re.compile(r'\["(https?://[^"\\]+?)",\s*(\d{1,5}),\s*(\d{1,5})\]')
# 三元组之后的元数据块
_DOMAIN_SIZE_RE = re.compile(r'"2000":\[null,"([^"]{1,120})","([^"]{1,30})"\]')
# "2003":[null,"docid","来源页URL","标题…",null,0,null,"缩略图URL"]
# 注意：来源页 URL 之后跟的是逗号+标题，**不是**数组结束符 ]。
# 此前写成 ..."(https?://[^"]{1,600})"\] 要求紧跟右方括号，导致永远匹配不到，
# 68 条结果的 source_page 全为空（曾误判为「截取窗口太短」）。
_SOURCE_PAGE_RE = re.compile(
    r'"2003":\[null,"([^"]*)","(https?://[^"]{1,600})"(?:,"([^"]{0,300})")?')
# 图片结果卡片：<a href="/imgres?imgurl=...&imgrefurl=...">…<img src="...">
_IMGRES_HREF_RE = re.compile(
    r'href=["\']((?:https?://[^"\'/]+)?/imgres\?[^"\']{1,4000})["\']', re.I)
_IMG_TAG_RE = re.compile(r"<img\b[^>]{0,1200}?>", re.I)
_ATTR_SRC_RE = re.compile(r'\bsrc="([^"]{1,2000})"', re.I)
_ATTR_ALT_RE = re.compile(r'\balt="([^"]{0,300})"', re.I)

# Google 自身资源，不是搜索结果
_GOOGLE_HOSTS = ("gstatic.com", "google.com", "googleapis.com", "ggpht.com",
                 "googleusercontent.com", "youtube.com", "ytimg.com")

# 同意页 cookie：不带时部分地区的首次访问会被 302 到 consent.google.com
# （渲染通道会误判成「被拦截」）。值取自 2024 版 Google 同意页的稳定写法。
_CONSENT_COOKIES = [
    {"name": "SOCS", "value": "CAESHAgBEhJnd3NfMjAyMzA4MTAtMF9SQzIaAmVuIAEaBgiA_LyaBg",
     "domain": ".google.com", "path": "/"},
    {"name": "CONSENT", "value": "YES+cb.20240101-00-p0.en+FX+000",
     "domain": ".google.com", "path": "/"},
]

# ---------- 筛选项 -> Google tbs 参数 ----------
# （tbs 是 Google 的稳定公开参数语法）
_GOOGLE_SIZE = {
    "small": "isz:s",
    "medium": "isz:m",
    "large": "isz:l",
    "wallpaper": "isz:xl",
}
_GOOGLE_TYPE = {
    "photo": "itp:photo",
    "clipart": "itp:clipart",
    "linedrawing": "itp:lineart",
    "animatedgif": "itp:animated",
    "transparent": "ic:trans",
}
_GOOGLE_TIME = {
    "day": "qdr:d",
    "week": "qdr:w",
    "month": "qdr:m",
    "year": "qdr:y",
}
_GOOGLE_ASPECT = {
    "wide": "iar:w",
    "square": "iar:s",
    "tall": "iar:t",
}
_GOOGLE_FACE = {
    "face": "itp:face",
    "portrait": "itp:portrait",
}
_GOOGLE_COLOR = {
    "color": "ic:color",
    "bw": "ic:gray",
    # 具体颜色：ic:specific,isc:<色号>
    "red": "ic:specific,isc:#c00",
    "orange": "ic:specific,isc:#f80",
    "yellow": "ic:specific,isc:#ff0",
    "green": "ic:specific,isc:#0a0",
    "teal": "ic:specific,isc:#088",
    "blue": "ic:specific,isc:#00f",
    "purple": "ic:specific,isc:#80f",
    "pink": "ic:specific,isc:#f8f",
    "brown": "ic:specific,isc:#840",
    "black": "ic:specific,isc:#000",
    "gray": "ic:specific,isc:#888",
    "white": "ic:specific,isc:#fff",
}
_GOOGLE_LICENSE = {
    "public": "sur:fc",
    "share": "sur:fmc",
    "share_modify": "sur:fms",
    "all_cc": "sur:fmcs",
}
# 分辨率下限 -> Google 的最小像素档位（tbs 的 islt 参数）
_GOOGLE_MIN_PIXELS = [
    (2160, "islt:4mp"),
    (1440, "islt:2mp"),
    (1080, "islt:qhd"),
    (720, "islt:hd"),
]

# ---------------------------------------------------------------- 工具函数

def _settings() -> dict:
    try:
        return store.load()
    except Exception:  # noqa: BLE001 —— 设置读失败不该让搜索崩掉
        return {}


def _has_cjk(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in (text or ""))


def build_tbs(filters: dict) -> str:
    """把筛选项拼成 Google 的 tbs 参数。"""
    parts: list[str] = []

    def _push(key: str, mapping: dict):
        v = (filters.get(key) or "").strip().lower()
        if v and v in mapping:
            parts.append(mapping[v])

    _push("size", _GOOGLE_SIZE)
    _push("type", _GOOGLE_TYPE)
    _push("time", _GOOGLE_TIME)
    _push("aspect", _GOOGLE_ASPECT)
    _push("face", _GOOGLE_FACE)
    _push("color", _GOOGLE_COLOR)
    _push("license", _GOOGLE_LICENSE)

    # 分辨率下限：转成 Google 的像素档位
    try:
        min_res = int(filters.get("min_resolution") or 0)
    except (TypeError, ValueError):
        min_res = 0
    if min_res:
        for threshold, token in _GOOGLE_MIN_PIXELS:
            if min_res >= threshold:
                parts.append(token)
                break
        # 未手动指定尺寸时，高分辨率再叠加大图约束以提高命中率
        if min_res >= 1080 and "size" not in filters:
            parts.append(_GOOGLE_SIZE["wallpaper"])
        elif min_res >= 720 and "size" not in filters:
            parts.append(_GOOGLE_SIZE["large"])

    # 去重保序
    seen, uniq = set(), []
    for p in parts:
        if p not in seen:
            seen.add(p)
            uniq.append(p)
    return ",".join(uniq)


def build_google_url(query: str, filters: dict, start: int = 0) -> str:
    """构造 Google 图片搜索 URL。

    `udm=2` 是 2024 之后 Google 图片搜索的稳定表达（`tbm=isch` 仍兼容但会被
    重写成 udm=2）。两个都带上，让服务端无论新旧路由都能落到图片结果。
    """
    params = {
        "q": query,
        "tbm": "isch",
        "udm": "2",
        "hl": filters.get("hl") or "zh-CN",
        "gl": filters.get("gl") or "us",
    }
    tbs = build_tbs(filters)
    if tbs:
        params["tbs"] = tbs
    if start:
        params["start"] = start
    # safe 参数：strict 开启安全搜索
    safe = (filters.get("safesearch") or "moderate").lower()
    if safe == "strict":
        params["safe"] = "strict"
    elif safe == "off":
        params["safe"] = "off"
    return "https://www.google.com/search?" + urlencode(params)


def _is_google_host(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return True
    return any(host == h or host.endswith("." + h) for h in _GOOGLE_HOSTS)


def _parse_size_text(text: str) -> int | None:
    """把 "1.7MB" / "71KB" / "1013KB" 解析为字节数。"""
    if not text:
        return None
    m = re.match(r"\s*([\d.]+)\s*(KB|MB|GB|B)", text, re.I)
    if not m:
        return None
    val = float(m.group(1))
    unit = m.group(2).upper()
    mult = {"B": 1, "KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3}[unit]
    return int(val * mult)


def _ext_of(url: str) -> str:
    m = re.search(r"\.([a-zA-Z0-9]{2,5})(?:\?|#|$)", url or "")
    return m.group(1).lower() if m else ""


def _norm_url(u: str) -> str:
    """统一反斜杠转义 / HTML 实体，方便去重。"""
    if not u:
        return ""
    u = _html.unescape(u)
    u = u.replace("\\u003d", "=").replace("\\u0026", "&").replace("\\/", "/")
    return u.strip()


def _make_item(url, *, thumbnail="", source_page="", title="", site="",
               width=None, height=None, size_bytes=None, size_text="",
               docid="", detail="", referer=None) -> dict:
    return {
        "url": url,
        "thumbnail": thumbnail,
        "source_page": source_page,
        "title": title,
        "source_site": site or (urlparse(url).hostname or ""),
        "referer": source_page if referer is None else referer,
        "engine": "google",
        "engine_detail": detail,
        "width": width,
        "height": height,
        "size_bytes": size_bytes,
        "size_text": size_text,
        "format": _ext_of(url),
        "docid": docid,
    }


# ---------------------------------------------------------------- 通道 1：浏览器渲染

def parse_google_imgres(html_text: str, page_url: str = "") -> list[dict]:
    """从渲染后的 HTML 里解析 `/imgres?imgurl=` 结果卡。

    这是 Google 图片结果最稳定的结构：每张图的「原图地址 + 来源页 + tbnid」
    全在卡片的 href 里，点击行为一直依赖它，所以比 AF_initDataCallback 的内部
    数组结构更不容易随版本变。
    """
    if not html_text:
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for m in _IMGRES_HREF_RE.finditer(html_text):
        href = _html.unescape(m.group(1))
        try:
            qs = parse_qs(urlparse(href).query, keep_blank_values=False)
        except ValueError:
            continue
        raw = (qs.get("imgurl") or [""])[0]
        url = _norm_url(raw)
        if not url.startswith("http") or _is_google_host(url) or url in seen:
            continue
        seen.add(url)
        ref = _norm_url((qs.get("imgrefurl") or [""])[0])
        tbnid = (qs.get("tbnid") or [""])[0]
        # 卡片后面紧跟的 <img> 是缩略图；连续匹配到下一个卡片前为止
        tail = html_text[m.end(): m.end() + 1500]
        img_tag = _IMG_TAG_RE.search(tail)
        thumb, title = "", ""
        if img_tag:
            sm = _ATTR_SRC_RE.search(img_tag.group(0))
            am = _ATTR_ALT_RE.search(img_tag.group(0))
            thumb = _norm_url(sm.group(1)) if sm else ""
            title = _html.unescape(am.group(1)) if am else ""
        out.append(_make_item(url, thumbnail=thumb, source_page=ref, title=title,
                              docid=tbnid, detail="render"))
    return out


def parse_google_html(html_text: str, page_url: str = "") -> list[dict]:
    """解析渲染后的 Google HTML（三条结构全部尝试，按 URL 去重）。

    1. `/imgres?imgurl=` 卡片（最稳，2024 后的主力结构）
    2. `["https://…",宽,高]` 三元组 + 尾随 "2000"/"2003" 元数据（AF_initDataCallback）
    """
    if not html_text:
        return []
    out: list[dict] = parse_google_imgres(html_text, page_url)
    seen: set[str] = {it["url"] for it in out}

    for m in _TRIPLE_RE.finditer(html_text):
        url = _norm_url(m.group(1))
        if not url.startswith("http"):
            continue
        if _is_google_host(url) or url in seen:
            continue
        try:
            width = int(m.group(2))
            height = int(m.group(3))
        except ValueError:
            continue
        # 过滤明显不是图片尺寸的异常值
        if width < 8 or height < 8 or width > 20000 or height > 20000:
            continue

        # 取三元组之后的元数据块
        tail = html_text[m.end(): m.end() + 1200]
        domain, size_text, source_page, title, docid = "", "", "", "", ""

        dm = _DOMAIN_SIZE_RE.search(tail)
        if dm:
            domain = dm.group(1)
            size_text = dm.group(2)
        sm = _SOURCE_PAGE_RE.search(tail)
        if sm:
            docid = sm.group(1) or ""
            source_page = _norm_url(sm.group(2))
            raw_title = sm.group(3) or ""
            if raw_title:
                title = _html.unescape(
                    raw_title.replace("\\u0026", "&").replace("\\/", "/")).strip()

        seen.add(url)
        out.append(_make_item(url, source_page=source_page, title=title,
                              site=domain, width=width, height=height,
                              size_bytes=_parse_size_text(size_text),
                              size_text=size_text, docid=docid, detail="render"))
    return out


def _diagnose_render(r) -> str:
    """把渲染失败/被拦截的页面翻译成一句有下一步动作的话。"""
    final = (r.final_url or "")
    html = (r.html or "")
    low = html[:40000].lower()
    if "/sorry/" in final or "unusual traffic" in low or "异常流量" in html:
        return ("Google 判定当前出口 IP 存在异常流量（/sorry 验证页），"
                "浏览器渲染通道走不通。请任选一条：① 在「设置」里配置 google_proxy "
                "走干净节点；② 配 google_searx 用 SearXNG 实例代理。")
    if "consent.google.com" in final or "before you continue" in low:
        return "Google 停在同意页（consent），渲染通道未能越过；请配置 google_proxy 或改用 SearXNG。"
    if "captcha" in low or "not a robot" in low or "recaptcha" in low:
        return "Google 要求人机验证，渲染通道当前不可用（换出口 IP 或改用 SearXNG）。"
    if not r.ok:
        return f"渲染失败: {r.error or '无内容'}"
    try:
        visible = len(re.sub(r"<[^>]+>", " ", html))
    except Exception:  # noqa: BLE001
        visible = 0
    return (f"Google 未解析到结果（HTML {len(html) // 1024}KB，可见字符 {visible}）。"
            "多半是结果结构又变了：请把这条关键词和 google 引擎报错一起反馈。")


def search_google_render(query: str, filters: dict, limit: int = 40,
                         scroll_rounds: int = 8, settle_ms: int = 4000,
                         timeout_ms: int = 45000) -> tuple[list[dict], str | None]:
    """真实内核渲染通道（支持 google_proxy 单独走代理）。"""
    if not renderer_status().get("available"):
        return [], "渲染能力不可用（未装 playwright / 无浏览器内核）"

    st = _settings()
    proxy = str(st.get("google_proxy") or "").strip()
    url = build_google_url(query, filters)
    try:
        r = render_page(
            url,
            timeout_ms=timeout_ms,
            settle_ms=settle_ms,
            scroll_rounds=scroll_rounds,
            capture_types=("json",),
            # 关键词搜图属于后台搜索任务，绝不能因为高级设置里开了
            # browser_headed 就弹出可见浏览器窗口。
            force_headless=True,
            proxy=proxy,
            extra_cookies=_CONSENT_COOKIES,
            extract_js=_JS_COLLECT,
            scroll_wait_ms=900,
        )
    except Exception as e:  # noqa: BLE001
        return [], f"Google 渲染异常: {type(e).__name__}: {e}"

    items: list[dict] = []
    seen: set[str] = set()

    # ① 浏览器内取回的 DOM 结构化结果
    js = getattr(r, "js", None)
    if isinstance(js, list):
        for it in js:
            if not isinstance(it, dict):
                continue
            u = _norm_url(str(it.get("u") or ""))
            if not u.startswith("http") or _is_google_host(u) or u in seen:
                continue
            seen.add(u)
            items.append(_make_item(
                u,
                thumbnail=_norm_url(str(it.get("th") or "")),
                source_page=_norm_url(str(it.get("r") or "")),
                title=_html.unescape(str(it.get("t") or "")),
                detail="render",
            ))
            if len(items) >= limit:
                break

    # ② 解析渲染后的 HTML（补 API 没覆盖到的三元组结构）
    if len(items) < limit:
        for it in parse_google_html(r.html or "", r.final_url or url):
            if it["url"] in seen:
                continue
            seen.add(it["url"])
            items.append(it)
            if len(items) >= limit:
                break

    if items:
        return items[:limit], None
    return [], _diagnose_render(r)


# ---------------------------------------------------------------- 通道 2：SearXNG 代理

def _searx_instances() -> list[str]:
    """只有用户显式配置了实例才启用这条通道。

    内置公共实例池不再默认使用：2026-11-04 实测它们对本机/多数国内出口
    一律 429 或反爬页，每次都白等十几秒。需要时把可用实例填进设置即可。
    """
    st = _settings()
    raw = str(st.get("google_searx") or "").strip()
    if not raw:
        return []
    out = [x.strip().rstrip("/") for x in re.split(r"[,;\s]+", raw) if x.strip()]
    return [x for x in out if x.startswith("http")]


def _searx_one(base: str, query: str, filters: dict, limit: int) -> tuple[list[dict], str | None]:
    """问一个 SearXNG 实例要 Google 图片结果（实例服务器代抓）。"""
    params = {
        "q": query,
        "format": "json",
        "categories": "images",
        "engines": "google images",
        "safesearch": {"strict": "2", "off": "0"}.get(
            (filters.get("safesearch") or "moderate").lower(), "1"),
        "language": "zh-CN" if _has_cjk(query) else "en-US",
    }
    try:
        r = requests.get(base + "/search", params=params,
                         headers={**HEADERS, "Accept": "application/json"},
                         timeout=12)
    except requests.RequestException as e:
        return [], f"{type(e).__name__}"
    if r.status_code != 200:
        return [], f"HTTP {r.status_code}"
    try:
        data = r.json()
    except ValueError:
        return [], "实例未开放 JSON 输出"
    out: list[dict] = []
    for it in (data.get("results") or []):
        u = _norm_url(it.get("img_src") or it.get("url") or "")
        if not u.startswith("http"):
            continue
        thumb = _norm_url(it.get("thumbnail_src") or "")
        page = _norm_url(it.get("url") or "")
        out.append(_make_item(
            u,
            thumbnail=thumb,
            source_page=page,
            title=_html.unescape(it.get("title") or ""),
            site=(urlparse(page).hostname or urlparse(u).hostname or ""),
            width=it.get("img_width") or None,
            height=it.get("img_height") or None,
            detail=f"searx:{urlparse(base).hostname or base}",
        ))
        if len(out) >= limit:
            break
    if out:
        return out, None
    unresp = data.get("unresponsive_engines")
    return [], f"0 条（{unresp or '实例未启用 google images'}）"


def search_google_searx(query: str, filters: dict, limit: int = 40,
                        timeout: float = 14.0) -> tuple[list[dict], str | None]:
    """SearXNG 通道：并发问前几个实例，谁先给结果用谁。"""
    insts = _searx_instances()
    if not insts:
        return [], "未配置 SearXNG 实例（设置 → google_searx）"
    errors: list[str] = []
    deadline = time.time() + timeout
    with ThreadPoolExecutor(max_workers=min(4, len(insts))) as ex:
        futs = [ex.submit(_searx_one, b, query, filters, limit) for b in insts[:4]]
        for fut in futs:
            left = deadline - time.time()
            if left <= 0:
                break
            try:
                items, err = fut.result(timeout=max(0.5, left))
            except Exception as e:  # noqa: BLE001
                errors.append(f"{type(e).__name__}")
                continue
            if items:
                return items[:limit], None
            errors.append(err or "空")
    return [], "SearXNG 全部失败：" + "；".join(errors[:4])


# ---------------------------------------------------------------- 统一入口

def _transport_order(filters: dict) -> list[str]:
    st = _settings()
    mode = str(filters.get("google_transport") or st.get("google_transport") or "auto").lower()
    if mode in ("render", "searx"):
        return [mode]
    return ["render", "searx"]


def google_status() -> dict:
    """给 /api/status 与设置页：各通道是否具备条件。"""
    st = _settings()
    transport = str(st.get("google_transport") or "auto")
    if transport not in ("auto", "render", "searx"):
        # 旧配置里可能还留着 api（通道已移除），统一回落 auto
        transport = "auto"
    return {
        "render_available": bool(renderer_status().get("available")),
        "proxy": str(st.get("google_proxy") or ""),
        "searx": _searx_instances() if str(st.get("google_searx") or "").strip() else [],
        "transport": transport,
    }


def search_google(
    query: str,
    filters: dict | None = None,
    limit: int = 40,
    scroll_rounds: int = 8,
    settle_ms: int = 4000,
    timeout_ms: int = 45000,
) -> tuple[list[dict], str | None]:
    """Google 图片搜索。返回 (结果列表, 错误信息)。

    按「配置自动选路 → 失败逐条降级」，每条通道的真实失败原因会拼进错误信息。
    """
    filters = filters or {}
    attempts: list[str] = []
    for name in _transport_order(filters):
        if name == "render":
            items, err = search_google_render(
                query, filters, limit=limit,
                scroll_rounds=scroll_rounds, settle_ms=settle_ms, timeout_ms=timeout_ms)
        else:
            items, err = search_google_searx(query, filters, limit=limit)
        if items:
            return items[:limit], None
        attempts.append(f"{name}: {err}")
    return [], "Google 全部通道失败 → " + " ｜ ".join(attempts)
