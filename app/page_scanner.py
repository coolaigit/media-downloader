# -*- coding: utf-8 -*-
"""网页媒体扫描：输入任意网页链接，提取页面上所有图片与视频地址。

覆盖来源：
  - <img src> / srcset / <picture><source>
  - 懒加载属性（data-src / data-original / data-lazy-src / data-echo ...）
  - CSS 内联与 <style> 中的 background-image / url(...)
  - <a href> 指向的图片或视频直链
  - <video><source> / <video src> / <audio>
  - og:image / og:video / twitter:image 等 meta
  - JSON-LD（application/ld+json）中的 image/video/contentUrl
  - 内联 <script> 文本里的 mp4/m3u8/webm/jpg/png 等 URL

安全：默认拒绝解析内网/本机地址（SSRF 防护），需用户显式开启才允许。
"""
from __future__ import annotations

import ipaddress
import json
import re
import socket
import threading
from urllib.parse import urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

from .config import HEADERS, TIMEOUT
from . import cookie_store

IMAGE_EXTS = {
    "jpg", "jpeg", "png", "gif", "webp", "bmp", "svg", "ico", "tif", "tiff",
    "avif", "heic", "heif", "jxl",
}
VIDEO_EXTS = {"mp4", "webm", "mkv", "mov", "avi", "flv", "wmv", "m4v", "ts", "mpg", "mpeg", "3gp"}
STREAM_EXTS = {"m3u8", "mpd"}

# ---------- 统一的页面抓取：代理与直连互为兜底 ----------
#
# 为什么需要（2026-09-22 实测）：本机环境变量里有 HTTP_PROXY=127.0.0.1:11794，
# requests 默认读它。该本地代理会**偶发** ProxyError
# （"Tunnel connection failed: 502 Bad Gateway"），实测 olelighting.com /
# kvilighting.com 直接整页抓取失败 —— 页面失败会连带丢掉它下面整棵详情页树，
# 用户看到的就是「粘贴链接抓不到」。而同一地址直连是通的。
# 这与搜索模块早已确立的做法一致：代理与直连互为兜底。
_SESSION_LOCK = threading.Lock()
_SESS_ENV: requests.Session | None = None
_SESS_DIRECT: requests.Session | None = None


def _http_sessions() -> tuple[requests.Session, requests.Session]:
    """懒加载两个会话：走环境代理的、以及绕过代理直连的。"""
    global _SESS_ENV, _SESS_DIRECT
    with _SESSION_LOCK:
        if _SESS_ENV is None:
            s = requests.Session()
            s.headers.update(HEADERS)
            _SESS_ENV = s
            d = requests.Session()
            d.trust_env = False          # 忽略 HTTP(S)_PROXY，直接连
            d.headers.update(HEADERS)
            _SESS_DIRECT = d
        return _SESS_ENV, _SESS_DIRECT


def http_get_with_fallback(url: str, timeout: int = TIMEOUT):
    """抓一个页面：先按环境代理，异常再直连兜底。

    返回 (response | None, 错误说明列表)。只有**网络异常**才兜底；
    HTTP 4xx/5xx 是站点给的正常回复，交给调用方判断，不重复请求。

    会带上「设置」里配置的 cookies（若有）—— 需要登录的站点（Instagram 等）
    不带 cookies 时只会返回登录墙，页面正文里根本没有内容。
    """
    env, direct = _http_sessions()
    jar = cookie_store.requests_cookies()
    errs: list[str] = []
    for label, sess in (("代理", env), ("直连", direct)):
        try:
            return sess.get(url, timeout=timeout, allow_redirects=True,
                            cookies=jar), errs
        except requests.exceptions.RequestException as e:
            errs.append(f"{label}失败（{type(e).__name__}）")
    return None, errs

# 懒加载常见的 data-* 属性
LAZY_ATTRS = (
    "data-src", "data-original", "data-lazy-src", "data-lazy", "data-echo",
    "data-url", "data-image", "data-img", "data-actualsrc", "data-thumb",
    "data-big-img", "data-srcset", "data-original-src", "data-lazy-original",
    "data-hires", "data-highres",
)

_URL_IN_TEXT_RE = re.compile(
    r"""(?xi)
    (?:https?:)?//
    [^\s'"<>()\\]+?
    \.(?:jpg|jpeg|png|gif|webp|bmp|svg|avif|heic|jxl|tif|tiff
        |mp4|webm|mkv|mov|avi|flv|m4v|mpg|mpeg|3gp|m3u8|mpd)
    (?:\?[^\s'"<>()\\]*)?
    """,
)
_CSS_URL_RE = re.compile(r"""url\(\s*['"]?([^'")]+)['"]?\s*\)""", re.I)
_JSON_BLOCK_RE = re.compile(r"\{[^{}]*\}", re.S)

# 反爬/人机验证墙特征（命中时明确告知用户，避免误以为工具失效）
BOT_WALL_MARKERS = (
    ("正在确认你是不是机器人", "站点启用了机器人验证（Anubis），需人工通过验证后才能抓取。"),
    ("百度安全验证", "站点出现百度安全验证，静态抓取被拦截。"),
    ("安全验证", "站点出现安全验证页，静态抓取被拦截。"),
    ("just a moment", "站点启用了 Cloudflare 人机验证，静态抓取被拦截。"),
    ("checking your browser", "站点正在校验浏览器，静态抓取被拦截。"),
    # Cloudflare 中文中间页的标题就是「请稍候…」（见工程手册的挑战页判据）；
    # 英文站的排队中间页常写 "Please wait..."（实测 TikTok 的中间页）。
    ("请稍候", "站点正在校验浏览器（Cloudflare 中间页），静态抓取被拦截。"),
    ("please wait", "站点返回了等待页（常见的排队 / 反爬中间页），需要稍后重试。"),
    ("verify you are human", "站点要求人机验证，静态抓取被拦截。"),
    ("captcha", "站点出现验证码拦截，静态抓取被拦截。"),
    ("access denied", "站点拒绝访问（可能有地域或 UA 限制）。"),
    ("forbidden", "站点返回禁止访问（可能需要登录或有防盗链）。"),
    ("anubis", "站点启用了 Anubis 反爬，静态抓取被拦截。"),
    ("请输入验证码", "站点要求输入验证码，静态抓取被拦截。"),
    ("滑动验证", "站点要求滑动验证，静态抓取被拦截。"),
    # 社媒平台（instagram / tiktok 等）常用「确认你不是机器人」这类措辞，
    # 且验证页带完整导航文字、只是内容取不到。配合「没抓到媒体」的条件使用，
    # 避免在正常页面上误报。
    ("not a robot", "站点要求人机验证（需登录或人工确认），静态抓取被拦截。"),
    ("are you a robot", "站点要求人机验证（需登录或人工确认），静态抓取被拦截。"),
    ("too many requests", "站点限流（请求过多），请稍后重试。"),
)


def _detect_bot_wall(soup: BeautifulSoup, text_sample: str) -> str | None:
    """检测是否撞上人机验证墙。返回提示文案，未命中返回 None。"""
    title = ""
    if soup.title and soup.title.string:
        title = soup.title.string.strip().lower()
    body_text = (soup.get_text(" ", strip=True)[:1200] or "").lower()
    blob = f"{title} {body_text} {text_sample[:1200].lower()}"
    for marker, msg in BOT_WALL_MARKERS:
        if marker in blob:
            return msg
    return None


def _is_private_host(host: str) -> bool:
    """判断主机名是否指向内网/本机（SSRF 防护）。"""
    if not host:
        return True
    h = host.lower().strip("[]")
    if h in ("localhost", "localhost.localdomain"):
        return True
    try:
        infos = socket.getaddrinfo(h, None)
    except Exception:
        return False  # 解析失败不判定为内网，交由请求阶段报错
    for info in infos:
        ip_str = info[4][0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return True
    return False


def normalize_url(url: str, base: str) -> str | None:
    """相对地址转绝对、去掉锚点与多余空白；协议相对 //host 补 https。"""
    if not url:
        return None
    u = url.strip()
    if not u or u.startswith("data:") or u.startswith("blob:") or u.startswith("javascript:"):
        return None
    if u.startswith("//"):
        u = "https:" + u
    absu = urljoin(base, u)
    p = urlparse(absu)
    if p.scheme not in ("http", "https"):
        return None
    return urlunparse((p.scheme, p.netloc, p.path, p.params, p.query, ""))


def classify(url: str) -> str:
    """按扩展名判断类型：image / video / stream / unknown。"""
    path = urlparse(url).path.lower()
    ext = path.rsplit(".", 1)[-1] if "." in path.rsplit("/", 1)[-1] else ""
    if ext in IMAGE_EXTS:
        return "image"
    if ext in VIDEO_EXTS:
        return "video"
    if ext in STREAM_EXTS:
        return "stream"
    return "unknown"


def _dedupe(items: list[dict]) -> list[dict]:
    """按 URL 去重，保留首次出现（信息更全的那条会被后续 merge 补充）。"""
    out: dict[str, dict] = {}
    for it in items:
        u = it.get("url")
        if not u:
            continue
        if u in out:
            # 合并字段：补充缺失信息，记录多个来源方式
            cur = out[u]
            for k, v in it.items():
                if k == "found_via":
                    if v and v not in cur.get("found_via", []):
                        cur.setdefault("found_via", []).append(v)
                elif v and not cur.get(k):
                    cur[k] = v
        else:
            it = dict(it)
            it["found_via"] = [it.get("found_via") or "html"]
            out[u] = it
    return list(out.values())


def _collect_srcset(value: str, base: str) -> list[str]:
    """解析 srcset="url 1x, url2 2x" 或 "url 300w, url2 800w"。"""
    urls = []
    if not value:
        return urls
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        cand = part.split()[0].strip()
        nu = normalize_url(cand, base)
        if nu:
            urls.append(nu)
    return urls


_META_CHARSET_RE = re.compile(r"""<meta[^>]+charset\s*=\s*['"]?\s*([a-zA-Z0-9_\-]+)""", re.I)
_META_CT_CHARSET_RE = re.compile(
    r"""<meta[^>]+http-equiv\s*=\s*['"]?Content-Type['"]?[^>]+charset=([a-zA-Z0-9_\-]+)""", re.I
)
_HEADER_CHARSET_RE = re.compile(r"charset\s*=\s*([a-zA-Z0-9_\-]+)", re.I)

# 编码别名归一（中文站点尤其多，gb2312/gbk 实际多为 gb18030 超集）
_ENCODING_ALIASES = {
    "gb2312": "gb18030",
    "gbk": "gb18030",
    "gb-2312": "gb18030",
    "utf8": "utf-8",
    "iso-8859-1": "utf-8",
    "latin1": "utf-8",
    "latin-1": "utf-8",
    "ascii": "utf-8",
    "us-ascii": "utf-8",
}


def _normalize_encoding(name: str | None) -> str | None:
    if not name:
        return None
    n = name.strip().lower()
    return _ENCODING_ALIASES.get(n, n)


def decode_html(resp: requests.Response, max_bytes: int = 4 * 1024 * 1024) -> str:
    """从响应字节解码 HTML，正确处理中文站点的 GBK/GB18030/Big5 编码。

    判定顺序：HTTP 头 charset -> HTML 内 meta charset -> chardet 推断 -> utf-8 兜底。
    不用 resp.text 的原因：requests 在响应缺少 charset 时会对 text/* 退回
    ISO-8859-1，导致新浪、彼岸网等 GBK 站点标题与文字全部乱码。
    """
    raw = (resp.content or b"")[:max_bytes]
    if not raw:
        return ""

    candidates: list[str] = []
    m = _HEADER_CHARSET_RE.search(resp.headers.get("Content-Type") or "")
    if m:
        candidates.append(m.group(1))

    # 只看前 4KB 即可找到 meta charset，避免为大页面重复解码
    probe = raw[:4096].decode("ascii", errors="ignore")
    for pattern in (_META_CHARSET_RE, _META_CT_CHARSET_RE):
        mm = pattern.search(probe)
        if mm:
            candidates.append(mm.group(1))

    try:
        if resp.apparent_encoding:
            candidates.append(resp.apparent_encoding)
    except Exception:
        pass

    for cand in candidates:
        enc = _normalize_encoding(cand)
        if not enc:
            continue
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue

    return raw.decode("utf-8", errors="replace")


def _looks_like_url(value: str) -> bool:
    """判断 meta content 是否真的是地址，排除宽高/时长/日期等纯元数据。"""
    v = (value or "").strip()
    if not v:
        return False
    if v.startswith(("http://", "https://", "//")):
        return True
    # 站内绝对路径：要求形如 /path/... 且不是单个数字或日期片段
    if v.startswith("/") and len(v) > 1:
        return not re.fullmatch(r"/[\d\-:TZ.+ ]+", v)
    return False


def scan_page(
    url: str,
    timeout: int = TIMEOUT,
    allow_private: bool = False,
    max_bytes: int = 4 * 1024 * 1024,
) -> dict:
    """抓取并扫描单个网页，返回 {items, meta}。"""
    url = (url or "").strip()
    if not url:
        return {"items": [], "meta": {"error": "未提供链接"}}
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    host = urlparse(url).hostname or ""
    if not allow_private and _is_private_host(host):
        return {
            "items": [],
            "meta": {
                "error": f"出于安全考虑，已拒绝解析内网/本机地址（{host}）。"
                "如确需扫描内网页面，请在设置中显式开启。",
                "url": url,
            },
        }

    try:
        r, errs = http_get_with_fallback(url, timeout=timeout)
    except requests.exceptions.RequestException as e:
        return {"items": [], "meta": {"url": url, "error": f"页面抓取失败: {type(e).__name__}: {e}"}}
    if r is None:
        return {"items": [], "meta": {"url": url,
                                      "error": "页面抓取失败：" + "；".join(errs)}}

    ctype = (r.headers.get("Content-Type") or "").lower()
    final_url = r.url

    # 目标本身就是媒体文件直链
    if any(t in ctype for t in ("image/", "video/", "audio/", "application/octet-stream")):
        return {
            "items": [
                {
                    "url": final_url,
                    "type": "image" if "image/" in ctype else "video",
                    "title": final_url.rsplit("/", 1)[-1][:120],
                    "source_site": host,
                    "referer": "",
                    "found_via": ["direct-content-type"],
                    "content_type": ctype.split(";")[0].strip(),
                }
            ],
            "meta": {"url": final_url, "note": "链接本身就是一个媒体文件，已直接返回。"},
        }

    if "text/html" not in ctype and "xml" not in ctype and ctype:
        return {
            "items": [],
            "meta": {"url": final_url, "error": f"返回内容不是网页（Content-Type: {ctype.split(';')[0]}）"},
        }

    html_text = decode_html(r, max_bytes)
    soup = BeautifulSoup(html_text, "lxml")
    base_tag = soup.find("base", href=True)
    base = normalize_url(base_tag["href"], final_url) if base_tag else final_url

    page_title = ""
    if soup.title and soup.title.string:
        page_title = soup.title.string.strip()

    bot_wall = _detect_bot_wall(soup, html_text)

    items: list[dict] = []

    def add(u: str | None, typ: str, via: str, title: str = "", referer: str = ""):
        nu = normalize_url(u or "", base)
        if not nu:
            return
        items.append(
            {
                "url": nu,
                "type": typ,
                "title": title.strip()[:200],
                "source_site": urlparse(nu).hostname or "",
                "page_site": urlparse(final_url).hostname or "",
                "referer": referer or final_url,
                "found_via": via,
            }
        )

    # ---------- 1. <img> ----------
    for img in soup.find_all("img"):
        title = img.get("alt") or img.get("title") or ""
        for attr in ("src",) + LAZY_ATTRS:
            v = img.get(attr)
            if isinstance(v, str) and v.strip():
                add(v, "image", f"img:{attr}", title)
        for attr in ("srcset", "data-srcset"):
            v = img.get(attr)
            if isinstance(v, str) and v.strip():
                for su in _collect_srcset(v, base):
                    add(su, "image", f"img:{attr}", title)

    # ---------- 2. <picture><source> ----------
    for src in soup.find_all("source"):
        title = src.get("alt") or ""
        for attr in ("src",) + LAZY_ATTRS:
            v = src.get(attr)
            if isinstance(v, str) and v.strip():
                add(v, "image", f"source:{attr}", title)
        for attr in ("srcset", "data-srcset"):
            v = src.get(attr)
            if isinstance(v, str) and v.strip():
                for su in _collect_srcset(v, base):
                    add(su, "image", f"source:{attr}", title)

    # ---------- 3. <video> / <audio> ----------
    for tag in soup.find_all(["video", "audio"]):
        title = tag.get("title") or tag.get("aria-label") or ""
        v = tag.get("src")
        if isinstance(v, str) and v.strip():
            add(v, "video" if tag.name == "video" else "audio", f"{tag.name}:src", title)
        v = tag.get("poster")
        if isinstance(v, str) and v.strip():
            add(v, "image", f"{tag.name}:poster", title)

    # ---------- 4. <a href> 直链 ----------
    for a in soup.find_all("a", href=True):
        href = a["href"]
        nu = normalize_url(href, base)
        if not nu:
            continue
        typ = classify(nu)
        if typ in ("image", "video", "stream"):
            title = a.get_text(strip=True)[:200] or a.get("title") or ""
            add(href, typ, "a:href", title)

    # ---------- 5. meta（og / twitter / itemprop） ----------
    # 跳过纯元数据字段：og:video:width=1920 这类若不过滤会被拼成假 URL
    META_SKIP_KEYS = ("width", "height", "duration", "release_date", "tag", "site_name", "type", "ttl")
    for m in soup.find_all("meta"):
        key = (m.get("property") or m.get("name") or m.get("itemprop") or "").lower()
        content = m.get("content")
        if not content or not key:
            continue
        if any(key.endswith(":" + sk) or key == sk for sk in META_SKIP_KEYS):
            continue
        if not _looks_like_url(content):
            continue
        if "image" in key or "thumbnail" in key:
            for su in _collect_srcset(content, base) or [content]:
                add(su, "image", f"meta:{key}", page_title)
        elif "video" in key or "audio" in key:
            for su in _collect_srcset(content, base) or [content]:
                add(su, "video", f"meta:{key}", page_title)

    # ---------- 6. 内联 style 与 <style> 中的 CSS 背景图 ----------
    css_texts: list[str] = []
    for st in soup.find_all("style"):
        if st.string:
            css_texts.append(st.string)
    for el in soup.find_all(style=True):
        css_texts.append(el["style"])
    for el in soup.find_all(attrs={"data-style": True}):
        css_texts.append(el["data-style"])
    for css in css_texts:
        for m in _CSS_URL_RE.finditer(css):
            u = m.group(1).strip()
            typ = classify(normalize_url(u, base) or u)
            add(u, typ if typ != "unknown" else "image", "css:url", page_title)

    # ---------- 7. JSON-LD ----------
    for script in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)}):
        raw = script.string or script.get_text() or ""
        if not raw.strip():
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                for k, v in node.items():
                    kl = k.lower()
                    if isinstance(v, str) and _looks_like_url(v):
                        if "image" in kl or "thumbnail" in kl:
                            add(v, "image", f"jsonld:{k}", page_title)
                        elif "video" in kl or "contenturl" in kl or "embedurl" in kl:
                            add(v, "video", f"jsonld:{k}", page_title)
                    elif isinstance(v, (dict, list)):
                        stack.append(v)
                # image 可能是对象列表
                for key in ("image", "thumbnailUrl", "contentUrl"):
                    val = node.get(key)
                    if isinstance(val, list):
                        stack.extend(val)
            elif isinstance(node, list):
                stack.extend(node)

    # ---------- 8. 内联 <script> 文本中的媒体 URL ----------
    for script in soup.find_all("script"):
        if script.get("src"):
            continue
        txt = script.string or script.get_text() or ""
        if not txt or len(txt) > 3 * 1024 * 1024:
            continue
        for m in _URL_IN_TEXT_RE.finditer(txt):
            u = m.group(0)
            typ = classify(u)
            if typ == "unknown":
                continue
            add(u, typ, "script:text", page_title)

    # ---------- 9. 常见 data-* 全局兜底（懒加载容器） ----------
    for el in soup.find_all(attrs={"data-src": True}):
        add(el["data-src"], "image", "attr:data-src", el.get("alt") or page_title)

    uniq = _dedupe(items)

    # 标注是否为媒体直链：页面/播放器地址（如 bilibili player.html）需交给视频解析
    PAGE_EXTS = {"html", "htm", "php", "asp", "aspx", "jsp", "shtml", "xhtml", "do", "action"}
    for it in uniq:
        u = it.get("url", "")
        path = urlparse(u).path.lower()
        last = path.rsplit("/", 1)[-1]
        ext = last.rsplit(".", 1)[-1] if "." in last else ""
        if ext in PAGE_EXTS:
            # 明确是网页，绝不是媒体直链
            it["is_direct"] = False
        elif ext in IMAGE_EXTS or ext in VIDEO_EXTS or ext in STREAM_EXTS:
            it["is_direct"] = True
        else:
            # 无扩展名：来自 og:image / 缩略图接口（如 bing th?id=）视为图片直链
            via = str(it.get("found_via", ""))
            it["is_direct"] = ("og:image" in via or "th?id=" in u or it.get("type") == "image")

    # 排序：可直下 > 视频 > 图片 > 其它
    uniq.sort(
        key=lambda x: (
            0 if x.get("is_direct") else 1,
            0 if x.get("type") == "video" else 1,
            x.get("url", ""),
        )
    )

    stats = {"image": 0, "video": 0, "stream": 0, "audio": 0, "unknown": 0}
    for it in uniq:
        stats[it.get("type", "unknown")] = stats.get(it.get("type", "unknown"), 0) + 1
    direct_count = sum(1 for it in uniq if it.get("is_direct"))

    return {
        "items": uniq,
        "meta": {
            "url": final_url,
            "requested_url": url,
            "page_title": page_title,
            "page_site": urlparse(final_url).hostname or "",
            "counts": stats,
            "total": len(uniq),
            "direct_count": direct_count,
            "bot_wall": bot_wall,
            "note": bot_wall
            or "静态解析结果。若页面依赖 JS 动态加载且结果偏少，可尝试对该页的具体媒体直链使用视频下载功能。",
        },
    }


def resolve_stream(url: str, referer: str = "", timeout: int = 15) -> dict:
    """把 m3u8/mpd 播放列表解析为分片清单（仅信息展示；实际下载交给 yt-dlp/ffmpeg）。"""
    out = {"url": url, "kind": None, "segments": [], "variants": [], "error": None}
    try:
        h = dict(HEADERS)
        if referer:
            h["Referer"] = referer
        r = requests.get(url, headers=h, timeout=timeout)
        text = r.text
        if not text.lstrip().startswith("#EXTM3U") and ".mpd" not in url.lower():
            out["error"] = "返回内容不是 HLS/DASH 播放列表"
            return out
        if "#EXT-X-STREAM-INF" in text:
            out["kind"] = "hls-master"
            for line in text.splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    nu = normalize_url(line, url)
                    if nu:
                        out["variants"].append(nu)
        else:
            out["kind"] = "hls-media"
            for line in text.splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    nu = normalize_url(line, url)
                    if nu:
                        out["segments"].append(nu)
        return out
    except Exception as e:  # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {e}"
        return out
