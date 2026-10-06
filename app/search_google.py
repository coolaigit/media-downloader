# -*- coding: utf-8 -*-
"""Google 图片搜索引擎（需浏览器渲染）。

为什么必须渲染：实测 Google 图片搜索对纯 HTTP 请求只返回 enablejs 空壳
（约 92KB、0 张图片），必须执行 JavaScript 才有结果。

实测得到的数据结构（本解析器的依据，不是猜测）：
  渲染后 HTML 中内嵌着形如
      ["https://ex.com/a.jpg",1500,1138],null,0,"rgb(63,92,63)",null,0,
      {"2000":[null,"www.esperoart.com","1.7MB"],
       "2003":[null,"sOUsUdMTuc_3aM","https://www.esperoart.com/xxx"]}
  的数组三元组 + 尾随元数据。其中：
    - 三元组 = [原图URL, 宽, 高]
      宽高顺序已用文件名交叉验证：esperoart 的 1500x1138=1.318，
      而其文件名为 Mountain-Landscape-16x12in（16/12=1.333），吻合，
      故第一个数字是宽、第二个是高。
    - "2000" = [null, 来源域名, 文件体积文本]
    - "2003" = [null, docid, 来源页 URL]

  这意味着 Google 结果**自带真实宽高与文件体积**，
  可直接用于清晰度筛选，无需再逐个探测原图（探测是整个搜索里最慢的环节）。
"""
from __future__ import annotations

import html as _html
import re
from urllib.parse import urlencode, urljoin, urlparse

from .renderer import render_page, renderer_status

# 三元组：["URL",宽,高]
_TRIPLE_RE = re.compile(r'\["(https?://[^"\\]+?)",(\d{1,5}),(\d{1,5})\]')
# 三元组之后的元数据块
_DOMAIN_SIZE_RE = re.compile(r'"2000":\[null,"([^"]{1,120})","([^"]{1,30})"\]')
# "2003":[null,"docid","来源页URL","标题…",null,0,null,"缩略图URL"]
# 注意：来源页 URL 之后跟的是逗号+标题，**不是**数组结束符 ]。
# 此前写成 ..."(https?://[^"]{1,600})"\] 要求紧跟右方括号，导致永远匹配不到，
# 68 条结果的 source_page 全为空（曾误判为「截取窗口太短」）。
_SOURCE_PAGE_RE = re.compile(
    r'"2003":\[null,"([^"]*)","(https?://[^"]{1,600})"(?:,"([^"]{0,300})")?')

# Google 自身资源，不是搜索结果
_GOOGLE_HOSTS = ("gstatic.com", "google.com", "googleapis.com", "ggpht.com",
                 "googleusercontent.com", "youtube.com", "ytimg.com")

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
    min_res = int(filters.get("min_resolution") or 0)
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
    """构造 Google 图片搜索 URL。"""
    params = {
        "q": query,
        "tbm": "isch",
        "hl": filters.get("hl") or "zh-CN",
        "gl": "us",
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


def parse_google_html(html_text: str, page_url: str = "") -> list[dict]:
    """从渲染后的 Google HTML 解析搜索结果。

    返回的条目自带 width/height/size_bytes，因此调用方可跳过原图探测。
    """
    if not html_text:
        return []
    out: list[dict] = []
    seen: set[str] = set()

    for m in _TRIPLE_RE.finditer(html_text):
        url = _html.unescape(m.group(1))
        # Google 会把 URL 里的字符做 JS 转义（\u003d 等），统一还原
        url = url.replace("\\u003d", "=").replace("\\u0026", "&")
        url = url.replace("\\/", "/")
        if not url.startswith("http"):
            continue
        if _is_google_host(url):
            continue
        if url in seen:
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
            source_page = _html.unescape(sm.group(2)).replace("\\/", "/")
            # 第 3 组是结果标题（可能含 \u0026 这类 JS 转义）
            raw_title = sm.group(3) or ""
            if raw_title:
                title = _html.unescape(
                    raw_title.replace("\\u0026", "&").replace("\\/", "/")).strip()

        seen.add(url)
        out.append({
            "url": url,
            "thumbnail": "",                      # Google 缩略图是 base64 内联，无独立地址
            "source_page": source_page,
            "title": title,
            "source_site": domain or (urlparse(url).hostname or ""),
            "referer": source_page or "",
            "engine": "google",
            # 以下为 Google 直接给出的真实元数据，可免去探测
            "width": width,
            "height": height,
            "size_bytes": _parse_size_text(size_text),
            "size_text": size_text,
            "format": _ext_of(url),
            "docid": docid,
        })
    return out


def _ext_of(url: str) -> str:
    m = re.search(r"\.([a-zA-Z0-9]{2,5})(?:\?|#|$)", url or "")
    return m.group(1).lower() if m else ""


def search_google(
    query: str,
    filters: dict | None = None,
    limit: int = 40,
    scroll_rounds: int = 6,
    settle_ms: int = 4000,
    timeout_ms: int = 60000,
) -> tuple[list[dict], str | None]:
    """Google 图片搜索。返回 (结果列表, 错误信息)。

    结果不足时自动向下滚动加载更多（Google 是无限滚动）。
    """
    filters = filters or {}
    if not renderer_status().get("available"):
        return [], ("Google 需要浏览器渲染，但渲染能力不可用。"
                    "请安装 playwright（见「设置」页提示）。")

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
        )
    except Exception as e:  # noqa: BLE001
        return [], f"Google 渲染异常: {type(e).__name__}"

    if not r.ok or not r.html:
        return [], f"Google 渲染失败: {r.error or '无内容'}"

    items = parse_google_html(r.html, r.final_url or url)

    # 被反爬拦截时，页面可能只有骨架
    if not items:
        low = r.html[:20000].lower()
        if "unusual traffic" in low or "captcha" in low or "not a robot" in low:
            return [], "Google 触发了人机验证（当前 IP 请求过多），请稍后重试或换用 Bing。"
        return [], f"Google 未解析到结果（HTML {len(r.html) // 1024}KB，可见文本 " \
                   f"{len(re.sub(r'<[^>]+>', ' ', r.html))} 字符）"

    return items[:limit], None
