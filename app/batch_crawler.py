# -*- coding: utf-8 -*-
"""批量抓取：列表页 → 详情页两段式通用抓取。

核心设计（实测驱动，不是拍脑袋）：

1. **HTTP 优先、渲染兜底**：先用普通 HTTP 抓取（约 1 秒），检测到 JS 空壳才升级
   到浏览器渲染（约 10-28 秒）。实测依据：
     - 阿里搜索页：静态 10KB 空壳 → 渲染后 1.1MB（必须渲染）
     - netbian：静态抓取正常，而 headless Chrome 反而报 ERR_SSL_PROTOCOL_ERROR
   所以「一律上浏览器」既慢又会在部分站点上失败。

2. **HTML + JSON 双通道提取**：拦截网络响应中的 JSON，实测比只解析 DOM 多提取
   3 倍媒体（304 vs 94）。

3. **两段式下钻**：列表页常只有缩略图，原图/视频在详情页。
   实测阿里列表页 60 个商品、仅 1 个直接带视频，其余需下钻详情页。

4. **不写站点专用规则**：全部基于「地址形态 + 字段语义」的通用启发式。
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import requests

from .config import HEADERS, TIMEOUT
from .concurrency import (GOVERNOR, KIND_LIST_CRAWL, OK, classify_error)
from .page_scanner import BOT_WALL_MARKERS, http_get_with_fallback
from .extractor import (
    IMAGE_EXTS,
    PAGE_EXTS,
    SKIP_EXTS,
    STREAM_EXTS,
    VIDEO_EXTS,
    MediaItem,
    _clean_title,
    _is_tracking,
    _title_quality,
    collapse_variants,
    collapse_video_variants,
    dedupe,
    enrich_with_originals,
    ext_of,
    extract_from_html,
    extract_from_json_text,
    extract_page_links,
    guess_size_from_url,
    is_decoration,
    is_thumb,
    normalize,
    page_title,
    summarize,
    _video_variant_info,
)
from .renderer import RenderResult, render_many, render_page, renderer_status

# 判定 JS 空壳的阈值：可见文本过少 = 内容由前端渲染
MIN_VISIBLE_TEXT = 400
# 静态抓取判定为「够用」的最少媒体数
MIN_STATIC_MEDIA = 3


@dataclass
class FetchResult:
    """单页抓取结果（无论走 HTTP 还是渲染）。"""
    url: str
    final_url: str = ""
    html: str = ""
    json_texts: list[str] = field(default_factory=list)
    method: str = "http"        # http | render
    ok: bool = False
    error: str = ""
    elapsed: float = 0.0
    visible_text_len: int = 0


@dataclass
class CrawlStats:
    pages_list: int = 0
    pages_detail: int = 0
    http_ok: int = 0
    render_used: int = 0
    render_failed: int = 0
    elapsed: float = 0.0


# ---------- 智能抓取 ----------

def _visible_text_len(html: str) -> int:
    """统计去掉脚本/样式/标签后的可见文本长度，用于判断是否 JS 空壳。"""
    if not html:
        return 0
    t = re.sub(r"<script[^>]*>.*?</script>", " ", html, flags=re.S | re.I)
    t = re.sub(r"<style[^>]*>.*?</style>", " ", t, flags=re.S | re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    t = re.sub(r"\s+", " ", t)
    return len(t.strip())


def _count_meaningful_media(html: str, base: str) -> int:
    """统计页面里「有意义」的媒体数量，排除站点装饰资源。

    为什么要排除：阿里搜索页的空壳 HTML 里也有 3 个媒体（favicon + 2 个 logo），
    如果把它们算进「已有内容」，就会擦过阈值不触发渲染，
    结果整页只抓到 3 个垃圾、0 个详情链接。
    """
    if not html:
        return 0
    try:
        items = extract_from_html(html, base)
    except Exception:
        return 0
    return sum(1 for i in items if not _is_decoration(i.url))


def _is_decoration(url: str) -> bool:
    """判断是否站点装饰资源或埋点像素（favicon、logo、图标、占位图、统计点）。

    为什么要过滤：站点被反爬拦截时，返回的空页里只剩 UI 图标，
    不过滤就会把 font_*.svg、14-14.png、mmstat/o.gif 这类垃圾当成交付结果。
    """
    u = (url or "").lower()
    name = urlparse(u).path.rsplit("/", 1)[-1]

    # 埋点 / 统计 / 广告域名：复用 extractor 的统一判定（单一事实来源）。
    # 此前两处各写一份规则，导致 hm.baidu.com/hm.gif 在媒体过滤路径上漏网。
    if _is_tracking(u):
        return True

    if name.startswith(("favicon", "apple-touch-icon")):
        return True
    if re.search(r"(^|[_\-/.])(logo|icon|sprite|placeholder|blank|spacer|loading|"
                 r"avatar-default|no-image|noimage|dot|pixel|trans)([_\-/.]|\d|$)", name):
        return True
    # 小尺寸图标：同时兼容 33x30 / 33-30 / 33_30 三种分隔符写法
    # （此前只认 "x" 分隔，导致 tps-33-30.png、14-14.png 漏网）
    m = re.search(r"[_\-](\d{1,4})[_\-x](\d{1,4})\.(?:png|jpe?g|gif|webp|svg)", name)
    if m:
        w, h = int(m.group(1)), int(m.group(2))
        if w <= 200 and h <= 200:
            return True
    # 本地快速路径未命中 → 转交 extractor 的统一判定。
    # 那边多了一条「按 URL 内嵌尺寸识别横幅/细长条」的规则：
    # 实测阿里列表页的 -tps-1518-360.jpg（横幅）、-tps-297-40.png（图标条）
    # 名字里毫无线索，只有读出尺寸才能识别。
    return is_decoration(url)


def _direct_media_item(url: str) -> MediaItem | None:
    """URL 本身就是媒体文件时，直接构造一条媒体记录。

    必要性：批量抓取原本一律按「网页」处理，粘一个 .jpg / .mp4 直链会得到
    「非网页内容」的报错。这个能力原属「网页扫描」，该模块合并后搬到这里。
    只按扩展名判断、**不发额外请求**；扩展名不明确的仍走正常抓取流程。
    """
    e = ext_of(url)
    if e in IMAGE_EXTS:
        return MediaItem(url=url, kind="image", source="direct-url",
                         is_thumb=is_thumb(url), origin_page=url,
                         extra={"ext": e})
    if e in VIDEO_EXTS:
        return MediaItem(url=url, kind="video", source="direct-url",
                         origin_page=url, extra={"ext": e})
    if e in STREAM_EXTS:
        return MediaItem(url=url, kind="stream", source="direct-url",
                         origin_page=url, extra={"ext": e})
    return None


def _visible_text(html: str, limit: int = 4000) -> str:
    """取出页面的「可见文本」：去掉 script/style/noscript 与标签后的正文。"""
    if not html:
        return ""
    t = re.sub(r"<(script|style|noscript|template)[^>]*>.*?</\1>", " ",
               html, flags=re.S | re.I)
    t = re.sub(r"<!--.*?-->", " ", t, flags=re.S)
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"\s+", " ", t).strip()[:limit]


def _detect_wall(html: str) -> str | None:
    """从页面「可见文本 + 标题」里认出人机验证墙，返回可读原因；没命中返回 None。

    这个判定原先只在「网页扫描」里，该模块合并后搬到这里 ——
    它比「渲染后正文为空」更明确：能说出撞的是哪家的验证
    （Cloudflare / Anubis / 百度安全验证等）。

    ⚠️ 只在**可见文本**上判定，绝不能扫整份 HTML（2026-09-22 踩的坑）：
    现代站点（实测 TikTok 主页 135 个 <script>）的脚本、内联配置里普遍出现
    "captcha" / "challenge" / "verify" 这些**前端功能名**，扫全文就会把
    正常页面（实测是「找不到此账号」页）误报成「站点出现验证码拦截」，
    把用户引向完全错误的方向（以为被反爬，其实账号名打错了）。
    """
    text = _visible_text(html, 4000)
    title = (page_title(html) or "")
    blob = f"{title} {text}".lower()
    for marker, msg in BOT_WALL_MARKERS:
        if marker in blob:
            return msg
    return None


def _recover_with_browser_skill(url: str, wall: str, on_note=None) -> FetchResult | None:
    """普通渲染撞上验证墙之后，才升级到 BrowserSkill 的可见会话。

    V3.0：bsk、浏览器扩展、内核全部随包（见 app/browser_skill.py），
    所以用户机器上一个浏览器都没装、也从没装过扩展，这条路照样能走。
    仍然只在「已经检测到验证墙」时才进来 —— 普通抓取不经过这里。
    """
    try:
        from . import browser_skill
    except Exception:
        return None
    if not browser_skill.available():
        return None

    session_id = ""
    t0 = time.time()
    try:
        if on_note:
            on_note("普通浏览器渲染遇到验证，正在准备可见验证窗口…")
        # 可见性交给「设置 → 显示浏览器窗口」，不再硬写 headless=False。
        # 原因（2026-10-24 用户实测）：抓 Instagram 视频时 gallery-dl 被风控挡回，
        # 回退渲染又撞验证墙，于是凭空弹出一个窗口 —— 用户明明登录着，弹窗帮不上忙，
        # 还打断他正在做的事。默认后台跑；真需要人工滑块的那种，把开关打开即可。
        from . import browser_profile
        want_headed = browser_profile.headed()
        ready = browser_skill.ensure_ready(headless=not want_headed, timeout=40)
        if not ready.get("ok"):
            if on_note:
                on_note(f"验证窗口准备失败：{ready.get('error') or '未知原因'}")
            return None
        if on_note:
            on_note("验证窗口已就绪，正在打开页面…")
        opened = browser_skill.start_page(url, browser_id=str(ready.get("instance_id") or ""))
        session_id = str(opened.get("session_id") or "")
        if not session_id:
            return None

        html = browser_skill.get_html(session_id)
        current_wall = _detect_wall(html) if html else wall

        if current_wall:
            clicked = browser_skill.click_simple_verification(session_id)
            if clicked:
                if on_note:
                    on_note(f"BrowserSkill 已点击「{clicked}」，正在确认页面…")
                html = browser_skill.get_html(session_id)
                current_wall = _detect_wall(html) if html else current_wall

        if current_wall:
            if on_note:
                on_note("BrowserSkill 已打开验证窗口；如出现滑块/验证码，请在窗口中完成后继续。")
            browser_skill.request_human_help(
                session_id,
                "当前抓取被网站验证拦截。请在这个浏览器窗口完成验证码、滑块或登录确认；"
                "页面恢复正常内容后，在 BrowserSkill 提示中确认完成。",
                timeout_seconds=180,
            )
            html = browser_skill.get_html(session_id)
            current_wall = _detect_wall(html) if html else current_wall

        if not html or current_wall:
            return None

        return FetchResult(
            url=url,
            final_url=str(opened.get("url") or url),
            html=html,
            json_texts=[],
            method="browser-skill",
            ok=True,
            error="",
            elapsed=time.time() - t0,
            visible_text_len=_visible_text_len(html),
        )
    except Exception:
        return None
    finally:
        if session_id:
            browser_skill.stop(session_id)


def _page_self_report(html: str) -> tuple[str, str]:
    """返回 (页面标题, 正文片段)，用于「抓到 0 条」时如实说明页面自己怎么说。

    为什么要它：站点侧原因（账号名不存在、页面已删除、要求登录）与工具侧原因
    必须能被用户一眼区分。与其猜，不如把页面自己的话摆出来 ——
    实测 TikTok 的「找不到此账号」、Instagram 的「链接可能已损坏或主页被移除」
    都是这样被认出来的。不做任何关键词表：换个语言/措辞就失效，也不通用。
    """
    title = (page_title(html) or "").strip()
    text = _visible_text(html, 240)
    return title[:120], text[:200]


def _has_js_hint(html: str) -> bool:
    """页面是否像「依赖 JS 渲染」。

    为什么需要：`_needs_render` 原来只看「文本少 + 媒体少」，
    结果连 example.com 这种纯静态页（559 字节、0 个脚本、正文 142 字符）
    也被判为需要渲染，白等约 50 秒。真正需要渲染的页面，一定有脚本或前端框架的挂载点。

    取向保守：宁可多渲染（慢一点），也不漏渲染（抓不到）。
    因此只有「几乎没有脚本、也没有任何框架特征」时才返回 False。
    """
    if not html:
        return False
    scripts = len(re.findall(r"<script", html, re.I))
    if scripts >= 3:
        return True
    low = html[:30000].lower()
    for marker in ("__next_data__", "__nuxt__", "window.__initial", "__initial_state__",
                   "data-reactroot", "ng-app", "ng-version", 'id="app"', "id='app'",
                   'id="root"', "id='root'", "vue.", "react."):
        if marker in low:
            return True
    # 有脚本且 HTML 有一定体量：多半是 SPA 壳
    return scripts >= 1 and len(html) > 20000


def _needs_render(html: str, media_count, html_len: int = 0,
                  visible_text: int | None = None) -> bool:
    """判断是否需要升级到浏览器渲染。

    判据（实测校准，按强度排列）：
      1. **可见文本近乎为零（<100 字符）→ 一定渲染**。这是最强的未渲染信号：
         正文完全没出来。此时即使 HTML 里能正则刮到几个媒体，那些也来自未渲染的
         bootstrap/内嵌 JSON，不是页面真实内容。
         实测教训：阿里反爬壳页返回 87KB HTML 但可见文本=0，旧判据因
         「HTML>60KB 且媒体≥3」放过了它，结果整页只抓到 4 个垃圾媒体、0 详情链接。
      2. 可见文本少（<400）且有意义的媒体也少（<3）→ 渲染（图片为主的正常页）。
      3. HTML 很小（<60KB）且有意义的媒体很少（<10）→ 渲染。
      4. 页面明确提示需要 JavaScript。

    `media_count` 可以是 int，也可以是「返回 int 的可调用对象」。
    为什么要允许惰性：算出它需要把整页媒体完整提取一遍（实测 1.6MB 详情页
    ≈0.55 秒），而绝大多数页面在判据 1/2/3 的廉价条件上就已定论、根本用不到。
    原先调用方一律提前算好，等于每个详情页都白解析一遍 —— 批量抓取 30 页
    就是十几秒起步，页面更大时更明显。判据的顺序也据此调整：要用到这个数的
    条件一律放在最后。
    """
    vt = _visible_text_len(html) if visible_text is None else visible_text

    def _mc() -> int:
        try:
            return int(media_count() if callable(media_count) else (media_count or 0))
        except Exception:  # noqa: BLE001
            return 0

    # 判据 1：可见文本近乎为零 = 未渲染壳，无条件渲染
    if vt < 100:
        return True
    # 判据 2：文本少，且页面确实像依赖 JS，且有意义的媒体也少
    # （把"媒体数"放最后：它是这里唯一昂贵的一步）
    if (vt < MIN_VISIBLE_TEXT and _has_js_hint(html) and _mc() < MIN_STATIC_MEDIA):
        return True
    # 判据 3：HTML 很小、可见文本不足、页面像依赖 JS、有意义媒体很少
    #
    # 必须同时要求「可见文本不足」：否则纯文字站点（如 quotes.toscrape，
    # 可见文本 1681 字符但 0 张图片）会被判为需渲染，每页白等约 19 秒。
    # 页面正文已经渲染出来了，只是本来就没有图片 —— 这不是空壳。
    #
    # 还必须要求「有 JS 迹象」：否则 example.com 这类纯静态小页
    # （559 字节、0 个脚本、正文 142 字符）也会被判需渲染，白等约 50 秒。
    # html_len < 60KB 保证这一步 _mc() 本身也是廉价的。
    if (html_len and html_len < 60 * 1024 and vt < MIN_VISIBLE_TEXT
            and _has_js_hint(html) and _mc() < 10):
        return True
    # 判据 4：明确要求 JS
    low = (html or "")[:8000].lower()
    if ("enablejs" in low or "please enable javascript" in low
            or "you need to enable javascript" in low):
        return True
    return False


def fetch_page(
    url: str,
    *,
    allow_render: bool = True,
    force_render: bool = False,
    timeout: int = TIMEOUT,
    render_settle_ms: int = 3500,
    render_scroll: int = 5,
) -> FetchResult:
    """抓取单个页面：HTTP 优先，检测到 JS 空壳才用浏览器渲染。

    force_render：无条件走渲染。给「要视频但静态页一个视频都没有、却有详情链接」
    这类场景升级用（实测小红书主页：HTTP 静态页只有 9 条链接且都不带访问令牌，
    渲染后才有 31 条带 xsec_token 的笔记链接 —— 不渲染就永远下钻不到视频）。
    """
    t0 = time.time()
    res = FetchResult(url=url)

    # ---- 第一层：普通 HTTP（瞬时错误自动重试）----
    # 本次开发中多次遇到间歇性失败：同一 URL 先 SSLError/DNS 失败、随后即成功。
    # 因此对瞬时错误重试，而不是立刻判定该页失败（否则会连带丢失整棵详情页树）。
    # 网络层本身还会「代理 → 直连」互为兜底，见 page_scanner.http_get_with_fallback。
    html = ""
    for attempt in range(1, 4):
        res.error = ""
        try:
            r, ferrs = http_get_with_fallback(url, timeout=timeout)
            if r is None:
                # 代理与直连都失败了：这类多为网络层瞬时问题，值得重试
                res.error = "；".join(ferrs) or "请求失败"
                if attempt < 3:
                    time.sleep(0.8 * attempt)
                    continue
                break
            res.final_url = r.url or url
            ctype = (r.headers.get("Content-Type") or "").lower()
            if r.status_code < 400 and ("html" in ctype or "xml" in ctype or not ctype):
                from .page_scanner import decode_html
                html = decode_html(r, 8 * 1024 * 1024)
                res.html = html
                res.ok = True
                res.method = "http"
                break
            elif r.status_code >= 400:
                res.error = f"HTTP {r.status_code}"
                # 5xx 与限流可重试；4xx 重试无意义
                if r.status_code >= 500 or r.status_code == 429:
                    time.sleep(0.8 * attempt)
                    continue
                break
            else:
                res.error = f"非网页内容（{ctype.split(';')[0]}）"
                break
        except requests.exceptions.RequestException as e:
            res.error = f"请求失败: {type(e).__name__}"
            if attempt < 3:
                time.sleep(0.8 * attempt)
                continue
            break

    # HTTP 层已拿到内容。这里只算「可见文本」（一行扫描，很便宜）；
    # 「有意义的媒体数」改成惰性——判据用不到就不解析整页（见 _needs_render）。
    if html:
        res.visible_text_len = _visible_text_len(html)

    # A challenge page may contain plenty of visible text and therefore look rendered.
    # Force the browser layer when the page itself says it is a verification wall.
    http_wall = _detect_wall(html) if html else None
    need_render = allow_render and (
        force_render
        or bool(http_wall)
        or not res.ok
        or _needs_render(html,
                         lambda: _count_meaningful_media(html, res.final_url or url),
                         len(html), res.visible_text_len)
    )
    if not need_render:
        res.elapsed = time.time() - t0
        return res

    # ---- 第二层：浏览器渲染 ----
    if not renderer_status().get("available"):
        res.elapsed = time.time() - t0
        if not res.ok:
            res.error = (res.error + " | " if res.error else "") + \
                "页面需要 JS 渲染，但渲染能力不可用（未安装 playwright）"
        return res

    rr: RenderResult = render_page(
        url, settle_ms=render_settle_ms, scroll_rounds=render_scroll,
        timeout_ms=timeout * 1000,
    )
    if rr.ok and rr.html:
        # 渲染结果通常更全，采用之；但保留 HTTP 已拿到的 JSON
        res.html = rr.html
        res.final_url = rr.final_url or res.final_url or url
        res.ok = True
        res.method = "render"
        res.elapsed = time.time() - t0
        res.visible_text_len = _visible_text_len(rr.html)
        res.json_texts = [c.body for c in rr.captured if c.body]

        # BrowserSkill is a last resort. It borrows the user's connected real
        # browser only after Playwright has positively hit a verification wall.
        wall = _detect_wall(rr.html)
        if wall:
            recovered = _recover_with_browser_skill(url, wall)
            if recovered is not None:
                recovered.elapsed += time.time() - t0
                return recovered
        return res

    # 渲染失败：若 HTTP 至少拿到了内容，仍返回 HTTP 结果
    res.elapsed = time.time() - t0
    if res.ok:
        res.method = "http(render-failed)"
    else:
        res.error = (res.error + " | " if res.error else "") + f"渲染失败: {rr.error}"
    return res


def fetch_many(
    urls: list[str],
    *,
    allow_render: bool = True,
    concurrency: int = 4,
    timeout: int = TIMEOUT,
    render_settle_ms: int = 2500,
    render_scroll: int = 2,
    on_progress=None,
    should_cancel=None,
) -> list[FetchResult]:
    """批量抓取。

    策略：先全部用 HTTP 并发抓（快），再只对判定需要渲染的少数页面用浏览器
    并发渲染。避免对每个页面都付渲染代价。
    """
    results: list[FetchResult] = [FetchResult(url=u) for u in urls]
    if not urls:
        return results

    # ---- 第一阶段：HTTP 并发 ----
    # 会话由 page_scanner 统一提供（走环境代理的那一个 + 绕过代理直连的那一个），
    # 每个请求都会在代理异常时自动改走直连 —— 见 http_get_with_fallback。
    try:
        from concurrent.futures import ThreadPoolExecutor, as_completed

        # concurrency*2 只是**上限**（原来硬封顶 16），实际同时打几个由
        # kind=list_crawl 的闸门按平台实时调节：撞 412/429 立刻降，稳定后自己升回来。
        gate = GOVERNOR.gate_for_url(str(urls[0] or ""), KIND_LIST_CRAWL,
                                     ceiling=max(1, min(concurrency * 2, 16)))

        def _http(i: int, u: str) -> tuple[int, FetchResult]:
            if should_cancel and should_cancel():
                return i, FetchResult(url=u, error="已取消")
            # 名额满了就在这里等（列表抓取是最容易撞频控的一环：几十个详情页
            # 同时打过去，平台一眼就看出是脚本）。等的时候也要能响应取消。
            if not gate.acquire(cancelled=lambda: bool(should_cancel and should_cancel())):
                return i, FetchResult(url=u, error="已取消")
            signal = None
            try:
                t0 = time.time()
                r = FetchResult(url=u)
                # 与 fetch_page 一致：瞬时错误重试，避免间歇性 SSL/DNS/超时
                # 导致整个详情页被丢弃（详情页失败会连带丢失其下全部媒体）
                for attempt in range(1, 4):
                    if should_cancel and should_cancel():
                        break
                    try:
                        resp, ferrs = http_get_with_fallback(u, timeout=timeout)
                        if resp is None:
                            r.error = "；".join(ferrs) or "请求失败"
                            if attempt < 3:
                                time.sleep(0.6 * attempt)
                                continue
                            break
                        r.final_url = resp.url or u
                        ct = (resp.headers.get("Content-Type") or "").lower()
                        if resp.status_code >= 400:
                            r.error = f"HTTP {resp.status_code}"
                            if resp.status_code >= 500 or resp.status_code == 429:
                                time.sleep(0.6 * attempt)
                                continue
                            break
                        elif "html" in ct or "xml" in ct or not ct:
                            from .page_scanner import decode_html
                            r.html = decode_html(resp, 8 * 1024 * 1024)
                            r.ok = True
                            r.method = "http"
                            r.visible_text_len = _visible_text_len(r.html)
                            r.error = ""
                            break
                        else:
                            r.error = f"非网页内容（{ct.split(';')[0]}）"
                            break
                    except requests.exceptions.RequestException as e:
                        r.error = type(e).__name__
                        if attempt < 3:
                            time.sleep(0.6 * attempt)
                            continue
                        break
                r.elapsed = time.time() - t0
                if r.ok:
                    signal = OK
                elif r.error:
                    # 只有平台明确的频控（412/429/验证页）才降速；
                    # 普通 404/超时/代理抖动不该把整站抓取速度压下去
                    signal = classify_error(r.error)
                return i, r
            finally:
                gate.release(signal)

        with ThreadPoolExecutor(max_workers=max(1, min(gate.ceiling, len(urls)))) as pool:
            futs = [pool.submit(_http, i, u) for i, u in enumerate(urls)]
            done = 0
            for fut in as_completed(futs):
                try:
                    i, r = fut.result()
                    results[i] = r
                except Exception:
                    pass
                done += 1
                if on_progress:
                    try:
                        on_progress("http", done, len(urls))
                    except Exception:
                        pass
                if should_cancel and should_cancel():
                    break
    finally:
        # 这里的会话由 page_scanner 统一持有并复用（含「绕过代理直连」的那个），
        # 不能在此关闭 —— 关掉之后同一进程里后续页面的请求会拿到已关闭的会话。
        pass

    if should_cancel and should_cancel():
        return results

    # ---- 第二阶段：仅对需要渲染的页面用浏览器 ----
    need_idx = []
    for i, r in enumerate(results):
        if not allow_render:
            break
        # 媒体数惰性传入：只有判据真的需要时才解析整页（详见 _needs_render）
        if not r.ok or _needs_render(
                r.html, lambda rr=r: _count_meaningful_media(
                    rr.html, rr.final_url or rr.url),
                len(r.html), r.visible_text_len):
            need_idx.append(i)

    if not need_idx or not allow_render:
        return results
    if not renderer_status().get("available"):
        for i in need_idx:
            r = results[i]
            r.error = (r.error + " | " if r.error else "") + \
                "页面需要 JS 渲染，但渲染能力不可用（未安装 playwright）"
        return results

    sub_urls = [urls[i] for i in need_idx]
    rr_list = render_many(
        sub_urls,
        concurrency=concurrency,
        settle_ms=render_settle_ms,
        scroll_rounds=render_scroll,
        timeout_ms=timeout * 1000,
        on_progress=lambda d, t: on_progress("render", d, t) if on_progress else None,
        should_cancel=should_cancel,
    )
    for i, rr in zip(need_idx, rr_list):
        r = results[i]
        if rr.ok and rr.html:
            r.html = rr.html
            r.final_url = rr.final_url or r.final_url or r.url
            r.ok = True
            r.method = "render"
            r.error = ""
            r.visible_text_len = _visible_text_len(rr.html)
            r.json_texts = [c.body for c in rr.captured if c.body]
            r.elapsed += rr.elapsed
        else:
            r.method = (r.method + "+render-failed") if r.ok else "render-failed"
            r.error = (r.error + " | " if r.error else "") + f"渲染失败: {rr.error}"
    return results


# ---------- 分页 ----------

_PAGINATION_PARAM_RE = re.compile(
    r"^(page|p|pn|pg|pageno|pagenum|pagenumber|pageindex|page_start"
    r"|offset|start|from|first|skip)$", re.I)
# offset 语义的参数：值是「跳过条数」而非页码，需要按步长递增
_OFFSET_PARAMS = {"offset", "start", "from", "first", "skip"}


def _host_path_of(url: str) -> tuple[str, str]:
    """取出 URL 的主机名与路径（去掉查询串），用于同列表校验。"""
    try:
        p = urlparse(url or "")
    except ValueError:
        return "", ""
    host = (p.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = (p.path or "").rstrip("/")
    return host, path


def _same_location(url: str, host: str, path: str) -> bool:
    """判断 url 与 (host, path) 是否指向同一列表位置。

    分页链接应当只在查询串上变化（page=N / first=N），
    主机名或路径不同的链接不可能是同一列表的下一页。
    """
    h, p = _host_path_of(url)
    if not h:
        return False
    if host and h != host:
        return False
    # 路径为空时不校验路径（有些站点首页即列表）
    if path and p and p != path:
        return False
    return True


def html_unescape(text: str) -> str:
    """解码 HTML 实体（如 &gt; &raquo; &amp;）。"""
    try:
        import html as _h
        return _h.unescape(text or "")
    except Exception:
        return text or ""


def build_page_url(base_url: str, page_no: int, current_page: int = 1) -> str | None:
    """按页码生成 URL。通用策略：改写已存在的分页参数。

    current_page: base_url 当前所处的页码（默认 1）。
      offset 型参数（offset/start/first/skip）的值是「跳过条数」而非页码，
      必须结合当前页码才能推出每页步长：step = 当前值 / (当前页 - 1)。
      此前一律按 step*(page_no-1) 计算，等于假设 base_url 总是第 1 页；
      当 base_url 已是第 2 页（如 ?start=20）时会算出 step=20 → 目标值 20，
      与输入完全相同，触发「URL 重复，停止翻页」。
    """
    try:
        p = urlparse(base_url)
    except ValueError:
        return None
    if not p.scheme or not p.netloc:
        return None
    qs = parse_qsl(p.query, keep_blank_values=True)

    # 已有分页参数 → 直接改值
    hit = None
    for i, (k, v) in enumerate(qs):
        if _PAGINATION_PARAM_RE.match(k):
            hit = i
            break
    if hit is not None:
        k, v = qs[hit]
        kl = k.lower()
        if kl in _OFFSET_PARAMS:
            try:
                cur_val = int(v)
            except (TypeError, ValueError):
                cur_val = 0
            # 从当前页码反推每页步长；推不出则用常见默认值
            if current_page > 1 and cur_val > 0:
                step = max(cur_val // (current_page - 1), 1)
            elif cur_val > 0:
                step = cur_val          # 当前是第 1 页且值>0，视该值为步长
            else:
                step = 20               # 无信息时的保守默认（Bing 用 35，Google 用 20）
            qs[hit] = (k, str(step * (page_no - 1)))
        else:
            qs[hit] = (k, str(page_no))
        return urlunparse((p.scheme, p.netloc, p.path, p.params, urlencode(qs), ""))

    # 没有分页参数 → 追加 page=N
    qs.append(("page", str(page_no)))
    return urlunparse((p.scheme, p.netloc, p.path, p.params, urlencode(qs), ""))


def detect_pagination(html: str, base_url: str, current_page: int = 1) -> dict:
    """从页面检测分页信息：是否存在下一页、最大页码、分页 URL 模式。

    通用启发式，不绑定站点。

    返回 page_urls: {页码: URL} —— 站点自己给出的分页链接。
    这是最可靠的翻页依据：实测 Bing 用 first=36 分页，猜 page=2 会 404；
    而页面里本来就带着正确的分页链接，直接用即可，无需猜测参数名。
    """
    out = {"has_next": False, "max_page": current_page, "next_url": None,
           "param": None, "mode": None, "page_urls": {}, "step": None}
    if not html:
        return out

    # 1) rel=next
    m = re.search(r'<link[^>]+rel=["\']next["\'][^>]+href=["\']([^"\']+)["\']', html, re.I)
    if not m:
        m = re.search(r'<link[^>]+href=["\']([^"\']+)["\'][^>]+rel=["\']next["\']', html, re.I)
    if m:
        nu = normalize(m.group(1), base_url)
        if nu:
            out.update(has_next=True, next_url=nu, mode="rel-next")

    # 2) <a> 里的下一页文案
    #
    # 严格匹配，不能用子串：实测宽松匹配把 Bing 的 /homes/map?q= 误判成
    # 「下一页」（因为 &gt; 和子串 "next" 会命中大量无关导航链接），
    # 导致爬虫跳到完全无关的页面并 404。
    NEXT_EXACT = {"下一页", "下页", "next", "next page", "next »", "next ›",
                  "›", "»", ">", "→", "다음", "次へ", "siguiente"}
    base_host, base_path = _host_path_of(base_url)

    for m in re.finditer(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', html, re.S | re.I):
        href, inner = m.group(1), re.sub(r"<[^>]+>", " ", m.group(2))
        # 文案必须短且完全等于翻页词；实体先解码再比对
        text = html_unescape(inner).strip().lower()
        text = re.sub(r"\s+", " ", text)
        if not text or len(text) > 24 or text not in NEXT_EXACT:
            continue
        nu = normalize(href, base_url)
        if not nu or not _same_location(nu, base_host, base_path):
            continue          # 跨路径的链接不可能是同列表的下一页
        out["has_next"] = True
        out["next_url"] = out["next_url"] or nu
        out["mode"] = out["mode"] or "a-next"
        # 文案本身就是页码数字的（如 "2" "3"），一并记入映射
        digits = re.fullmatch(r"\d{1,4}", text)
        if digits:
            out["page_urls"][int(text)] = nu

    # 3) 统计分页参数、最大页码，并收集「页码 → 真实 URL」映射
    pages = set()
    param_names = set()
    offset_values = set()
    for m in re.finditer(r'href=["\']([^"\']+)["\']', html, re.I):
        u = normalize(m.group(1), base_url)
        if not u:
            continue
        for k, v in parse_qsl(urlparse(u).query):
            if not _PAGINATION_PARAM_RE.match(k):
                continue
            param_names.add(k)
            try:
                iv = int(v)
            except (TypeError, ValueError):
                continue
            if k.lower() in _OFFSET_PARAMS:
                offset_values.add(iv)
                # offset 型无法直接得知页码，跳过映射（由步长推导）
                continue
            if 1 <= iv <= 2000:
                pages.add(iv)
                # 同页码可能被多处链接指向，保留最短的那个（通常最干净）
                prev = out["page_urls"].get(iv)
                if prev is None or len(u) < len(prev):
                    out["page_urls"][iv] = u

    # 路径式分页：/page/3、/p3、_3.html
    for m in re.finditer(r'href=["\']([^"\']+)["\']', html, re.I):
        u = normalize(m.group(1), base_url)
        if not u:
            continue
        pm = re.search(r"/(?:page|p)[/_-]?(\d{1,4})(?:/|\.html?|$)", u, re.I)
        if pm:
            try:
                iv = int(pm.group(1))
            except ValueError:
                continue
            if 1 <= iv <= 2000:
                pages.add(iv)
                out["page_urls"].setdefault(iv, u)
            out["mode"] = out["mode"] or "path-page"

    if param_names:
        out["param"] = sorted(param_names, key=len)[0]
        out["mode"] = out["mode"] or "query-param"

    # offset 型：从相邻值推出每页步长（如 Bing first=1,36,71 → step=35）
    if offset_values and len(offset_values) >= 2:
        sv = sorted(offset_values)
        diffs = {b - a for a, b in zip(sv, sv[1:]) if b - a > 0}
        if diffs:
            out["step"] = min(diffs)
            # 基准值：offset 不一定从 0 起算（Bing 是 1,36,71,106 → base=1）。
            # 正确的页码→值公式是 base + step*(页码-1)，忽略 base 会算出 70 而非 71。
            out["offset_base"] = sv[0]
            out["mode"] = out["mode"] or "offset-param"
            out["max_page"] = max(out["max_page"], current_page + 1)
            out["has_next"] = True

    if pages:
        out["max_page"] = max(max(pages), current_page)
        if out["max_page"] > current_page:
            out["has_next"] = True
    return out


def _content_fingerprint(items: list) -> str:
    """由页面提取到的媒体地址生成内容指纹，用于检测「翻页未生效」。

    只取地址集合的稳定摘要：同一页重复抓取会得到相同指纹，
    而真正翻到新页会得到不同指纹。
    """
    if not items:
        return ""
    urls = sorted({(getattr(i, "url", "") or "") for i in items if getattr(i, "url", "")})
    if not urls:
        return ""
    # 取前后各若干条 + 总数，兼顾稳定性与区分度（避免对超大集合做全量哈希过慢）
    sample = urls[:40] + (["..."] if len(urls) > 80 else []) + urls[-40:]
    import hashlib
    digest = hashlib.sha1("\n".join(sample).encode("utf-8", "ignore")).hexdigest()
    return f"{len(urls)}:{digest}"


def _next_page_url(base_url: str, page_no: int, current_page: int, pg: dict) -> str | None:
    """决定下一页 URL，按可靠性从高到低尝试。

    为什么不能只靠猜参数：实测 Bing 图片搜索用 first=36 分页，
    猜成 page=2 直接 404；而页面里本来就带着正确的分页链接。
    因此优先用站点自己给出的地址，猜测仅作兜底。
    """
    target = page_no

    # 1) 站点明确给出的该页链接（最可靠）
    page_urls = pg.get("page_urls") or {}
    if target in page_urls:
        return page_urls[target]

    # 2) offset 型参数：能确定性计算时优先用它。
    #    公式 base + step*(目标页-1)，如 Bing first=1,36,71,106 → base=1, step=35。
    #    放在 next_url 之前的原因：next_url 是从「当前页」解析出来的，
    #    只指向下一页；若被用于更远的页码（或 pg 与 base_url 不同步）会返回过期地址。
    #    实测正是这个顺序错误让第 3、4 页都拿到了 first=36。
    step = pg.get("step")
    param = pg.get("param")
    if step and param and param.lower() in _OFFSET_PARAMS:
        base_val = int(pg.get("offset_base") or 0)
        want = base_val + step * (target - 1)
        built = _replace_param(base_url, param, want)
        if built:
            return built

    # 3) rel=next / "下一页" 链接
    #    仅当目标页正好是「当前页的下一页」、且该链接不等于当前页地址时才可用。
    nu = pg.get("next_url")
    if nu and target == current_page + 1 and nu != base_url:
        return nu

    # 4) 非 offset 型参数但已知步长（少见），同样按公式计算
    if step and param:
        want = step * (target - 1)
        built = _replace_param(base_url, param, want)
        if built:
            return built

    # 5) 兜底：改写已有分页参数或追加 page=N
    return build_page_url(base_url, target, current_page)


def _replace_param(base_url: str, param: str, value: int) -> str | None:
    """把 URL 中的指定查询参数替换为给定值（不存在则追加）。"""
    try:
        p = urlparse(base_url)
        qs = parse_qsl(p.query, keep_blank_values=True)
        replaced = False
        for i, (k, _v) in enumerate(qs):
            if k.lower() == param.lower():
                qs[i] = (k, str(value))
                replaced = True
                break
        if not replaced:
            qs.append((param, str(value)))
        return urlunparse((p.scheme, p.netloc, p.path, p.params, urlencode(qs), ""))
    except ValueError:
        return None


# ---------- 两段式批量抓取 ----------

# ---------- 详情页主视频锚点 ----------

def _hls_master_heights(url: str, timeout: int = 10) -> list[int]:
    """拉一个 HLS master playlist，返回里面的档位高度（降序）。

    为什么要探：详情页里写死的 MP4 直链往往只有低档（实测 xvideos：静态页只给
    240p/360p），而播放器真正用的 HLS master 里有 480/720/1080 —— 页面标题的
    「HD 1080p」标记就来自它。下载 master 时 ffmpeg 会自动选最高档，
    这里把档位探出来是为了在结果里如实标注。
    失败返回 []，不影响主流程（master 照样保留，下载时 ffmpeg 自己选档）。
    """
    try:
        r, _errs = http_get_with_fallback(url, timeout=timeout)
    except Exception:
        return []
    if r is None or getattr(r, "status_code", 0) >= 400:
        return []
    text = r.text or ""
    if "#EXT-X-STREAM-INF" not in text:
        return []
    heights = set()
    for line in text.splitlines():
        if not line.startswith("#EXT-X-STREAM-INF"):
            continue
        res = re.search(r"RESOLUTION=(\d+)x(\d+)", line)
        if res:
            heights.add(int(res.group(2)))
        nm = re.search(r'NAME="(\d{3,4})p"', line)
        if nm:
            heights.add(int(nm.group(1)))
    return sorted(heights, reverse=True)


def _page_main_video_anchor(html: str, base: str):
    """从详情页找「页面主视频」的通用锚点。

    为什么需要：详情页 HTML 里除主视频外，通常还内嵌几十个「相关推荐」
    视频的播放地址。不加甄别全部提取的话，一个 46 条的搜索列表能带回
    500+ 条视频，绝大多数不是用户要的（pornhub 实测 60 页下钻带出 512 个
    无关视频）。

    锚点优先级（全部是跨站点的通用标准，非站点专用规则）：
      1. ld+json 的 schema.org VideoObject.contentUrl（结构化数据里的正式地址）
      2. OpenGraph 的 og:video meta（og:video:type 为视频文件时）
    返回 (主视频归并键 or None, VideoObject dict or None, 声明的播放地址)。

    第三个返回值（声明的播放地址原文）为什么需要：同一部片常同时存在多个
    档位地址，而「名字看起来最高清」的那条未必能下 —— 实测 pornhub 的
    original_<id>.mp4 挂在图片 CDN 上、请求返回 470 No Token，而 og:video
    声明的 240P 地址是 200 / 88MB 真视频。归并成一条时要优先保住声明地址，
    见 extractor.collapse_video_variants 的排序说明。
    """
    if not html:
        return None, None, ""
    from .extractor import _try_parse_json

    vobj = None
    for m in re.finditer(
            r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
            html, re.S | re.I):
        data = _try_parse_json(m.group(1))
        if data is None:
            continue
        for o in (data if isinstance(data, list) else [data]):
            if not isinstance(o, dict):
                continue
            if "videoobject" not in str(o.get("@type", "")).lower():
                continue
            vobj = o
            cu = normalize(o.get("contentUrl") or "", base)
            key = _video_variant_info(cu)[0] if cu else None
            if key:
                return key, o, cu
            break

    # og:video meta 兜底
    head = html[:800000]
    for m in re.finditer(r"<meta\s+[^>]*>", head, re.I):
        tag = m.group(0)
        if not re.search(r'''property\s*=\s*["\']og:video["\']''', tag, re.I):
            continue
        cm = re.search(r'''content\s*=\s*["\']([^"\']+)["\']''', tag, re.I)
        if not cm:
            continue
        cu = normalize(html_unescape(cm.group(1)), base)
        key = _video_variant_info(cu)[0] if cu else None
        if key:
            return key, vobj, cu
    return None, vobj, ""


_LISTING_LINK_WORDS = ("video", "watch", "view", "clip", "post", "scene", "movie", "embed")
_LISTING_LINK_DENY = ("tag", "category", "categor", "page=", "sort", "filter",
                      "login", "signup", "register", "premium", "dmca", "terms",
                      "privacy", "contact", "about", "help")


def _render_listing_links(list_url: str, *, limit: int = 60, on_note=None) -> list[dict]:
    """用内置浏览器渲染列表页，抽出「视频页」链接（yt-dlp 不认识这个站时的兜底）。

    为什么需要（2026-10-24 用户反馈 hornyleak.tv）：
    页面形态是 `/models/<名字>/` 这种博主/集合页，识别没问题；但 yt-dlp 没有这个站的
    提取器，于是回退到通用网页抓取 —— 这类站内容全靠 JS 渲染，静态抓只拿到 1 条。
    这是一般性问题：**所有 yt-dlp 不支持的中小站点的列表页都会这样**。

    筛选刻意保守（宁可少抓，也不要抓一堆标签页/登录页回来）：
    - 必须同域
    - 路径深度 ≥ 2
    - 命中视频类路径词，或末段是长 ID
    - 排除 tag/category/page=/login 这类非内容链接
    """
    try:
        from playwright.sync_api import sync_playwright
        from . import browser_runtime
        from .config import DEFAULT_UA
    except Exception:  # noqa: BLE001
        return []
    from urllib.parse import urlparse as _up
    try:
        base = _up(list_url)
    except ValueError:
        return []
    host = (base.hostname or "").lower().removeprefix("www.")
    if not host:
        return []
    if on_note:
        on_note("这个站 yt-dlp 不认识，改用内置浏览器渲染列表页并逐条找视频页…")

    rows: list[dict] = []
    try:
        with sync_playwright() as p:
            b = p.chromium.launch(headless=True, **browser_runtime.launch_kwargs())
            page = b.new_page(user_agent=DEFAULT_UA,
                              viewport={"width": 1400, "height": 1000}, locale="zh-CN")
            try:
                page.goto(list_url, wait_until="domcontentloaded", timeout=45000)
            except Exception:  # noqa: BLE001
                pass
            page.wait_for_timeout(3500)
            for _ in range(4):        # 往下拉几次，让懒加载的卡片出来
                try:
                    page.mouse.wheel(0, 2400)
                except Exception:  # noqa: BLE001
                    break
                page.wait_for_timeout(1200)
            try:
                rows = page.evaluate(
                    """() => Array.from(document.links).map(a => {
                         const img = a.querySelector('img');
                         const head = a.querySelector('h1,h2,h3,h4,h5,[class*=title],[class*=Title]');
                         return {
                           href: a.href || '',
                           text: (a.innerText || '').trim().slice(0, 140),
                           title_attr: (a.getAttribute('title') || '').trim().slice(0, 140),
                           img_alt: img ? (img.getAttribute('alt') || '').trim().slice(0, 140) : '',
                           heading: head ? (head.innerText || '').trim().slice(0, 140) : '',
                           img: img ? (img.src || '') : ''
                         };
                       })""")
            except Exception:  # noqa: BLE001
                rows = []
            b.close()
    except Exception:  # noqa: BLE001
        return []

    out: list[dict] = []
    seen: set[str] = set()
    list_path = (base.path or "").rstrip("/")
    for r in rows if isinstance(rows, list) else []:
        href = str((r or {}).get("href") or "").split("#", 1)[0]
        if not href.startswith("http"):
            continue
        try:
            pu = _up(href)
        except ValueError:
            continue
        if (pu.hostname or "").lower().removeprefix("www.") != host:
            continue
        path = "/".join([s for s in (pu.path or "").split("/") if s])
        segs = [s for s in (pu.path or "").split("/") if s]
        if len(segs) < 2:
            continue
        low_q = (pu.query or "").lower()
        if any(w in low_q for w in ("tag", "category", "sort", "page")):
            continue
        low_path = path.lower()
        if any(w in low_path for w in _LISTING_LINK_DENY):
            continue
        last = segs[-1].lower()
        # 末段是长 ID（数字/十六进制/短横线混合，≥6 位）也算内容页
        looks_like_id = bool(re.fullmatch(r"(?=.*\d)[\w\-]{6,}", last))
        if not (any(w in low_path for w in _LISTING_LINK_WORDS) or looks_like_id):
            continue
        if path == list_path.strip("/") or href in seen:
            continue
        seen.add(href)
        # 标题优先级：title 属性 > 图片 alt > 卡片内标题元素 > URL slug > 链接文字。
        # 链接文字常常是「16:01 / 86%」这种时长+进度，绝不能拿来当名字
        # （实测 hornyleak.tv 抓出来的名字全是 "16:01 86%"）。
        cands = [
            str((r or {}).get("title_attr") or "").strip(),
            str((r or {}).get("img_alt") or "").strip(),
            str((r or {}).get("heading") or "").strip(),
        ]
        title = ""
        for c in cands:
            if len(c) >= 4 and not re.fullmatch(r"[\d:.\s%\-/]+", c):
                title = c
                break
        if not title:
            slug = segs[-1]
            if re.fullmatch(r"\d+", slug) and len(segs) >= 3:
                slug = segs[-2]          # /video/33006/comatozze-xxx/ → 用 xxx
            if slug and not re.fullmatch(r"[\d\-]+", slug):
                title = slug.replace("-", " ").replace("_", " ").strip()
        if not title:
            title = str((r or {}).get("text") or "")[:150]
        out.append({
            "page_url": href,
            "direct_url": "",
            "title": title[:150],
            "cover": str((r or {}).get("img") or ""),
            "uploader": "",
            "site": host,
            "kind_hint": "video",
        })
        if len(out) >= max(1, int(limit or 60)):
            break
    return out


def _looks_like_collection_page(url: str) -> bool:
    """地址**形态**上像「人 / 集合」页（主页、频道、合集），而不是单条内容页。

    只看地址形态、不看站点（项目一贯的通用启发式：地址形态 + 字段语义 + URL 内嵌尺寸）。

      像集合页：/@handle、/user/xxx、/channel/xxx、/space/xxx、/@xxx/videos
               以及「浅路径（≤1 段）且不带长数字 ID」的地址（如 /USKYLEDS/）
      像内容页：/video/7539022…、/watch?v=…、/reel/xxx、/p/abc123 —— 这类是**单条视频**，
               交给「粘贴单个视频链接 → 解析清晰度」更合适（能选清晰度与字幕），
               不必在这里试列表接口。

    顺序很关键（都会互相误伤，实测踩过）：
      a) 查询串里带内容 ID 参数 → 内容页，先判（/view_video.php?viewkey=…）
      b) 「内容名词 + ID 形态」的末两段 → 内容页（/@x/video/7539… 也命中 @handle，
         所以必须排在集合名词判断**之前**）
      c) 末段/倒数第二段是集合名词、或末段是 @handle → 集合页（/space/12345、
         /@x/videos 都是集合，虽然末段看着像 ID）
      d) 其余：带长数字 ID 的当内容页，否则当集合页
    """
    if not url:
        return False
    try:
        p = urlparse(url)
    except ValueError:
        return False
    segs = [s for s in (p.path or "").split("/") if s]
    if not segs:
        return True                          # 首页本身可能就是内容聚合页
    if len(segs) > 3:
        return False
    low = [s.lower() for s in segs]
    last = low[-1]
    prev = low[-2] if len(low) >= 2 else ""

    # a) 查询串里的「内容 ID」语义参数（与 extractor._score_link 同一套语义）
    if re.search(r"[?&](v|id|vid|video_?id|content_?id|media_?id|watch|viewkey)=",
                 url, re.I):
        return False
    # b) 内容名词 + ID 形态 → 单条内容
    if prev in _CONTENT_SEG_NAMES and re.fullmatch(r"[\w.\-]{6,}", segs[-1]):
        return False
    # c) 集合名词 / @handle → 人 或 集合
    if prev in _COLLECTION_SEG_NAMES or last in _COLLECTION_SEG_NAMES \
            or last.startswith("@"):
        return True
    if len(segs) <= 1:
        return True
    # d) 带长数字 ID 的多半是内容
    if re.search(r"\d{5,}", p.path or ""):
        return False
    return True


# 内容页路径里的「内容名词」（末段是 ID 时用于判定单条内容）
_CONTENT_SEG_NAMES = frozenset((
    "video", "watch", "reel", "reels", "short", "status", "p", "clip",
    "photo", "detail", "post", "item", "play", "player",
))
# 集合页路径里的「人 / 集合名词」
_COLLECTION_SEG_NAMES = frozenset((
    "videos", "shorts", "streams", "space", "profile", "profiles", "posts",
    "media", "channel", "channels", "user", "users", "c", "u", "playlist",
    "playlists", "board", "boards", "album", "albums", "gallery",
))


def _expand_video_listing(
    list_url: str,
    *,
    limit: int = 60,
    cookies_file: str | None = None,
    cookies_from_browser: str | None = None,
    on_note=None,
) -> tuple[dict | None, str]:
    """把「视频列表页 / 博主页」交给 yt-dlp 展开成一批视频条目。

    命中时返回 (与 crawl_list 同构的结果字典, "")：items 里每条都是一个**视频页
    地址**，带 extra.is_page 标记；不命中返回 (None, 原因)，调用方回退到通用网页抓取。

    为什么这是通用做法而不是「站点专用规则」：判据只有 ——
    「用户要视频 + 地址形态像人/集合页 + yt-dlp 自己认识这个站 + 它说这是个列表」。
    我们既没为任何站点写规则，也没猜接口，只是把页面交给 yt-dlp 自带的列表提取器
    （tiktok:user / youtube:tab / bilibili:space …）。

    实测（2026-09-22）：TikTok 博主页 https://www.tiktok.com/@kvi.lighting
      通用网页抓取 = 0 条（视频地址根本不在 DOM 里）
      本路径 = 30 条（含标题与封面），约 10 秒
    """
    from .video_downloader import expand_video_list, has_specific_extractor

    if not _looks_like_collection_page(list_url):
        return None, ""
    if not has_specific_extractor(list_url):
        # yt-dlp 没有这个站的提取器。以前这里直接放弃 → 回退通用网页抓取，
        # 而这类站的内容基本都是 JS 渲染，静态抓只拿到 1 条（实测 hornyleak.tv）。
        # 现在改用内置浏览器渲染列表页，把视频页链接抽出来当列表结果。
        rendered = _render_listing_links(list_url, limit=limit, on_note=on_note)
        if len(rendered) < 2:
            return None, ""
        site_host = (urlparse(list_url).hostname or "").removeprefix("www.")

        # ★ 必须就地换成直链，不能把「视频页地址」丢给 yt-dlp。
        # 判据是「yt-dlp 自己说不认识这个站」（has_specific_extractor=False），
        # 那 /api/download/videos 拿这些地址去问 yt-dlp 只会得到 Unsupported URL。
        # 2026-10-29 实测 pornhao.com：列表页渲染出 8 个视频页，勾选后下载全部
        # Unsupported URL —— 用户看到的就是「能抓、点下载全失败」；而这些页面的
        # mp4 直链就写在 HTML 里，纯 HTTP 下钻一次就能拿到。
        page_urls = [r["page_url"] for r in rendered]
        if on_note:
            on_note(f"正在逐个打开 {len(page_urls)} 个视频页取直链…")
        try:
            dres = fetch_many(
                page_urls, allow_render=False, concurrency=6, render_scroll=0,
                on_progress=(lambda stage, d, t: on_note(
                    f"正在打开视频页（{stage}）{d}/{t}")) if on_note else None,
            )
        except Exception:  # noqa: BLE001
            dres = []

        # 先解析每一页，再统一校验，因为「可信」要跨页面比对才知道。
        per_page: list[tuple[dict, list[MediaItem]]] = []
        for idx, r in enumerate(rendered):
            vids: list[MediaItem] = []
            fr = dres[idx] if idx < len(dres) else None
            if fr is not None and getattr(fr, "ok", False) and getattr(fr, "html", ""):
                try:
                    found = extract_from_html(fr.html, fr.final_url or r["page_url"],
                                              r["page_url"])
                    vids = [g for g in found if g.kind == "video"]
                except Exception:  # noqa: BLE001
                    vids = []
            if vids:
                try:
                    vids = collapse_video_variants(vids)
                except Exception:  # noqa: BLE001
                    pass
            per_page.append((r, vids))

        # ---- 下钻结果的通用校验（2026-10-30 hornyleak.tv 实测教训）----
        # 每个详情页主框架都挂同一个 728x90 广告视频，正片在 iframe 播放器里，
        # 纯 HTTP 下钻能看到的只有广告。「取第一个 video」的结果就是 6 个不同
        # 视频页全部解析出同一个 contents/data/728x90-1.mp4 —— 用户看到的还是
        # 「下载的视频不是列表里的」（10-24 修过下载侧，这次抓取侧也堵住）。
        # 两条判据，都不含站点名单：
        #   a) 广告位命名：路径里出现「宽x高」标准 banner 尺寸（728x90、300x250…）
        #      —— 广告位按标准尺寸命名是行业惯例；正片尺寸命名（1280x720 等）
        #      位数对不上，不会被误伤。
        #   b) 跨页同 URL：不同详情页解析出**同一条**直链 = 拿到的是页面模板的
        #      通用资源（广告/背景视频），不是各页自己的内容。
        def _is_ad_slot(u: str) -> bool:
            try:
                path = urlparse(u).path
            except ValueError:
                return False
            return bool(re.search(r"(?:^|/)\d{3,4}x\d{2,3}[-_.]", path))

        # 预览片段命名（行业通用词）：hornyleak 每个详情页都有一部当前片的
        # preview.mp4（实测 400~700KB，只有十几秒），各页不同所以「跨页唯一」
        # 拦不住 —— 必须按命名识别。正片文件名几乎从不含这些词。
        _PREVIEW_RE = re.compile(
            r"(?:^|[/_.])(preview|trailer|sample|teaser)(?:[/_.]|$)", re.I)

        def _looks_preview(u: str) -> bool:
            try:
                path = urlparse(u).path
            except ValueError:
                return False
            return bool(_PREVIEW_RE.search(path))

        dup_count: dict[str, int] = {}
        for _r, vids in per_page:
            for v in vids:
                dup_count[v.url] = dup_count.get(v.url, 0) + 1

        def _trustworthy(v: MediaItem) -> bool:
            return (not _is_ad_slot(v.url)
                    and not _looks_preview(v.url)
                    and dup_count.get(v.url, 0) <= 1)

        r_items: list[MediaItem] = []
        got_direct = 0
        for r, vids in per_page:
            best = next((v for v in vids if _trustworthy(v)), None)
            if best is not None:
                got_direct += 1
                r_items.append(MediaItem(
                    url=best.url, kind="video", source=f"下钻:{site_host}",
                    title=r.get("title") or best.title or "",
                    thumbnail=r.get("cover") or best.thumbnail or "",
                    referer=r["page_url"], origin_page=r["page_url"],
                    width=best.width, height=best.height,
                    extra={
                        "is_page": False,
                        "ext": best.extra.get("ext", ""),
                        "variants": [v.url for v in vids[1:]
                                     if v.url != best.url and _trustworthy(v)],
                        "duration_string": best.extra.get("duration_string", ""),
                        "uploader": "", "site": site_host,
                        "stream_heights": best.extra.get("stream_heights", []),
                    },
                ))
            else:
                # 没有可信直链（页面打不开 / 内容在 iframe 播放器里 / 解析出的
                # 全是广告位与模板资源）：保留视频页地址，交给下载层用渲染解析
                # （那套逻辑会跟随 iframe 播放器），而不是把错文件当成结果。
                r_items.append(MediaItem(
                    url=r["page_url"], kind="video", source=f"渲染列表:{site_host}",
                    title=r.get("title") or "", thumbnail=r.get("cover") or "",
                    referer=list_url, origin_page=list_url,
                    extra={"is_page": True, "ext": "", "duration": None,
                           "duration_string": "", "uploader": "", "site": site_host},
                ))

        warnings = [
            "这个站 yt-dlp 没有专用提取器，已用内置浏览器渲染列表页并抽出 "
            f"{len(r_items)} 个视频条目。",
        ]
        if got_direct:
            warnings.append(
                f"其中 {got_direct} 条已逐个打开视频页取到**直链**，可以直接下载。")
        if got_direct < len(r_items):
            warnings.append(
                f"另有 {len(r_items) - got_direct} 条只保留视频页地址（正片在 "
                "iframe/加密播放器里，网页里能直接看到的视频被识别为广告位已剔除）"
                "—— 下载时会逐条渲染解析真实播放器。")
        if got_direct:
            warnings.append(
                "直链带签名会过期（实测 pornhao 约 3 小时）；过期后重新抓一次即可，"
                "下载时若已过期，工具会自动回源换新地址再下一次。")

        return {
            "items": [m.to_dict() for m in r_items],
            "summary": summarize(r_items),
            "pages": [{"page": 1, "url": list_url, "method": "内置浏览器渲染列表+逐页下钻",
                       "html_bytes": 0, "media": len(r_items), "links": len(r_items),
                       "elapsed": 0.0, "visible_text": 0}],
            "detail_pages": {"total": len(page_urls), "ok": got_direct,
                             "failed": len(page_urls) - got_direct,
                             "urls": page_urls[:20]},
            "stats": {"list_pages": 1, "detail_pages": len(page_urls),
                      "http_ok": got_direct, "render_used": 1,
                      "variants_collapsed": 0, "elapsed": 0.0},
            "warnings": warnings,
            "list_url": list_url,
            "renderer": renderer_status(),
            "via": "render-listing+drill",
        }, ""

    try:
        if on_note:
            on_note("正在判断这是不是一个视频列表页 / 博主页（通用视频列表接口）…")
        exp = expand_video_list(
            list_url, limit=limit,
            cookies_file=cookies_file, cookies_from_browser=cookies_from_browser,
        )
    except Exception as e:  # noqa: BLE001
        return None, f"{type(e).__name__}: {str(e)[:160]}"

    if not exp.get("ok"):
        return None, exp.get("error") or "未能展开"
    if not exp.get("is_list"):
        return None, ""

    # 列表模式（extract_flat）不解析详情，很多站的条目里根本没有封面 ——
    # 实测 2026-10-24：pornhub 的 PornHubPagedVideoList 每条只有 title/url。
    # 抓一次列表页 HTML 把缩略图补回来（匹配不上就留空，绝不乱配别人的图）。
    try:
        from .profile_sources import _backfill_covers
        if any(not str(e.get("thumbnail") or "").strip()
               for e in (exp.get("entries") or [])):
            _backfill_covers(list_url, exp.get("entries") or [])
    except Exception:  # noqa: BLE001 —— 补封面失败不该影响抓取本身
        pass

    items: list[MediaItem] = []
    seen: set[str] = set()
    for e in exp.get("entries") or []:
        u = (e.get("webpage_url") or "").strip()
        if not u or u in seen:
            continue
        seen.add(u)
        items.append(MediaItem(
            url=u, kind="video", source=f"列表:{exp.get('extractor') or 'video'}",
            title=e.get("title") or "", thumbnail=e.get("thumbnail") or "",
            referer=list_url, origin_page=list_url,
            extra={
                "is_page": True,            # 前端据此走 yt-dlp 下载（见 app.js）
                "ext": "",
                "duration": e.get("duration"),
                "duration_string": e.get("duration_string") or "",
                "uploader": e.get("uploader") or "",
                "site": e.get("source_site") or "",
            },
        ))

    if len(items) < 2:
        return None, ""

    where = exp.get("title") or list_url
    warnings = [
        f"这是一个视频列表（{exp.get('extractor') or '通用列表'}：{where}），"
        f"已用平台自带的视频列表接口展开 {len(items)} 条视频 —— "
        "视频地址不在页面 HTML 里，走网页抓取只会得到 0 条，所以这里换用了通用视频工具。",
        "每条都是一个视频页地址：勾选后点「下载选中」，会由 yt-dlp 逐条解析下载"
        "（可先在工具栏选清晰度）。想按清晰度单独挑某一条，可点该条的「打开」再单独解析。",
    ]
    return {
        "items": [m.to_dict() for m in items],
        "summary": summarize(items),
        "pages": [{"page": 1, "url": list_url, "method": "yt-dlp 列表",
                   "html_bytes": 0, "media": len(items), "links": 0,
                   "elapsed": 0.0, "visible_text": 0}],
        "detail_pages": {"total": 0, "ok": 0, "failed": 0, "urls": []},
        "stats": {"list_pages": 1, "detail_pages": 0, "http_ok": 0,
                  "render_used": 0, "variants_collapsed": 0,
                  "elapsed": 0.0},
        "warnings": warnings,
        "list_url": list_url,
        "renderer": renderer_status(),
        "via": "yt-dlp",
    }, ""



def _expand_xhs_image_source(list_url: str, *, limit: int = 30, on_note=None) -> tuple[dict, str]:
    """XHS author image source: accept only note-card body images."""
    from . import profile_sources

    rows, note = profile_sources.list_author_items(
        list_url, limit=limit, want_video=False, on_note=on_note)
    items: list[MediaItem] = []
    for r in rows:
        direct = str(r.get("direct_url") or "").strip()
        if not direct:
            continue
        page = str(r.get("page_url") or list_url).strip()
        items.append(MediaItem(
            url=direct, kind="image", source="xhs:note-image",
            title=str(r.get("title") or ""), thumbnail=direct,
            referer=page, origin_page=page,
            width=r.get("width"), height=r.get("height"), is_thumb=False,
            extra={"site": "xiaohongshu.com", "uploader": r.get("uploader") or "",
                   "note_image_index": r.get("note_image_index") or 0},
        ))
    items = dedupe(items)[:max(1, min(int(limit or 30), 2000))]
    warnings = []
    if items:
        warnings.append(f"\u5c0f\u7ea2\u4e66\u56fe\u7247\u5df2\u4f7f\u7528\u4f5c\u54c1\u8be6\u60c5\u63a5\u53e3\uff0c\u5f97\u5230 {len(items)} \u5f20\u7b14\u8bb0\u6b63\u6587\u56fe\u7247\u3002")
        warnings.append("\u5df2\u7981\u7528\u666e\u901a\u7f51\u9875\u626b\u56fe\u56de\u9000\uff0c\u4e0d\u518d\u6df7\u5165\u5934\u50cf\u3001\u7ad9\u70b9 UI \u548c\u63a8\u8350\u6d41\u56fe\u7247\u3002")
    else:
        warnings.append(note or "\u6ca1\u6709\u627e\u5230\u5c0f\u7ea2\u4e66\u56fe\u6587\u7b14\u8bb0\u6b63\u6587\u56fe\u7247\u3002")
        warnings.append("\u5df2\u505c\u6b62\u666e\u901a\u7f51\u9875\u626b\u56fe\u56de\u9000\uff0c\u907f\u514d\u8fd4\u56de\u4e0e\u4f5c\u8005\u7b14\u8bb0\u65e0\u5173\u7684\u9875\u9762\u56fe\u7247\u3002")
    return {
        "items": [m.to_dict() for m in items],
        "summary": summarize(items),
        "pages": [{"page": 1, "url": list_url, "method": "builtin-xhs-note-images",
                   "html_bytes": 0, "media": len(items), "links": 0,
                   "elapsed": 0.0, "visible_text": 0}],
        "detail_pages": {"total": 0, "ok": 0, "failed": 0, "urls": []},
        "stats": {"list_pages": 1, "detail_pages": 0, "http_ok": 0,
                  "render_used": 0, "variants_collapsed": 0, "elapsed": 0.0},
        "warnings": warnings,
        "list_url": list_url,
        "renderer": renderer_status(),
        "via": "builtin-platform:xhs-images",
    }, note


def _expand_profile_image_source(
    list_url: str,
    *,
    limit: int = 30,
    on_note=None,
) -> tuple[dict, str]:
    """Return only author-owned image entries from a mature platform source."""
    from . import profile_sources

    platform = profile_sources.platform_for_url(list_url)
    rows, note = profile_sources.list_author_items(
        list_url, limit=limit, want_video=False, on_note=on_note)
    items: list[MediaItem] = []
    for r in rows:
        if (r.get("kind_hint") or "").strip() != "image":
            continue
        direct = str(r.get("direct_url") or "").strip()
        if not direct:
            continue
        page = str(r.get("page_url") or list_url).strip()
        preview = str(r.get("cover") or direct).strip()
        if platform == "wb" and "sinaimg.cn/" in preview:
            preview = preview.replace("/large/", "/mw1024/").replace("/original/", "/mw1024/")
        items.append(MediaItem(
            url=direct,
            kind="image",
            source=f"builtin-profile-image:{platform}",
            title=str(r.get("title") or ""),
            thumbnail=preview,
            referer=page,
            origin_page=page,
            width=r.get("width"),
            height=r.get("height"),
            is_thumb=False,
            extra={
                "site": r.get("site") or "",
                "uploader": r.get("uploader") or "",
            },
        ))
    items = dedupe(items)[:max(1, min(int(limit or 30), 2000))]
    warnings: list[str] = []
    if items:
        warnings.append(
            f"博主图片已通过平台作品源读取，共得到 {len(items)} 张；"
            "没有使用整页扫图，因此不会混入推荐流、头像和站点 UI 图片。")
    else:
        warnings.append(note or "平台作品源没有返回该博主的图片。")
        warnings.append(
            "已停止普通网页扫图回退，避免把推荐流、头像或全站图片误当成该博主作品。")
    return {
        "items": [m.to_dict() for m in items],
        "summary": summarize(items),
        "pages": [{
            "page": 1, "url": list_url, "method": f"builtin-profile-image:{platform}",
            "html_bytes": 0, "media": len(items), "links": 0,
            "elapsed": 0.0, "visible_text": 0,
        }],
        "detail_pages": {"total": 0, "ok": 0, "failed": 0, "urls": []},
        "stats": {"list_pages": 1, "detail_pages": 0, "http_ok": 0,
                  "render_used": 0, "variants_collapsed": 0, "elapsed": 0.0},
        "warnings": warnings,
        "list_url": list_url,
        "renderer": renderer_status(),
        "via": f"builtin-platform:{platform}-images",
    }, note


def _expand_via_platform_source(
    list_url: str,
    *,
    limit: int = 30,
    want_kinds: tuple[str, ...] | list[str] | None = None,
    on_note=None,
) -> tuple[dict | None, str]:
    """平台主页的自包含来源：内置提取器优先，必要时用独立登录浏览器。

    抖音/小红书等主页作品数据常依赖签名参数、登录态或页面自身的网络请求。
    2.0 不再调用外部 Python 项目，而由 profile_sources 统一调度 yt-dlp、
    gallery-dl、xhshow 与本程序 Playwright 渲染层。

    want_kinds：用户这次要的类型（image / video / stream）。
    之前这里**写死 want_video=True**，于是「抓图片」也会按视频走，
    导致 Instagram 这类图vid混排的主页拿到一堆视频、图片只有零星几条
    （2026-10-27 用户实测）。现在按真实诉求走，并在返回前按类型过滤。

    返回 (结果 | None, 说明)。
    """
    from . import profile_sources

    platform = profile_sources.platform_for_url(list_url)
    if not platform:
        return None, ""
    _kinds = tuple(want_kinds or ("image", "video", "stream"))
    _want_video = ("video" in _kinds) and ("image" not in _kinds)
    _want_image = ("image" in _kinds) and ("video" not in _kinds)
    # 统一入口：按平台选择内置来源；见 profile_sources.list_author_items。
    items_raw, note = profile_sources.list_author_items(
        list_url, limit=limit, want_video=_want_video, on_note=on_note)
    if not items_raw:
        return None, note

    items: list[MediaItem] = []
    _dropped = 0
    for r in items_raw:
        direct = (r.get("direct_url") or "").strip()
        page = (r.get("page_url") or "").strip()
        # 各平台来源的字段名不统一：ig 把直链放在 url、page_url 放页面地址；
        # 其它来源用 direct_url。这里做一次归一，否则 ig 的图片/视频永远只有
        # 页面地址（下载层拿不到真文件，2026-10-27 实测）。
        _u = str(r.get("url") or "").strip()
        if not direct and page and _u.startswith("http") and _u != page:
            direct = _u
        target = direct or page          # 有签名直链就直链，否则退回页面地址（交给 yt-dlp）
        if not target:
            continue
        # 按用户这次要的类型过滤。平台来源会图vid混排，
        # 只看 kind_hint 不够：还要看条目自己标的 type（ig 有 image/video）。
        kind_hint = (r.get("kind_hint") or "").strip()
        is_video = kind_hint == "video" or str(r.get("type") or "") == "video"
        if is_video and _want_image:
            _dropped += 1
            continue
        if (not is_video) and _want_video:
            _dropped += 1
            continue
        # m3u8/mpd 是流媒体清单，不能当普通文件下（会得到坏文件）——
        # 标成 stream，前端会走 ffmpeg 的流下载路径（实测能合成音画齐全的单文件）。
        _ext = target.rsplit("?", 1)[0].rsplit(".", 1)[-1].lower()
        _kind = "stream" if _ext in ("m3u8", "mpd") else ("video" if is_video else "image")
        items.append(MediaItem(
            url=target, kind=_kind, source=f"内置平台来源:{platform}",
            title=r.get("title") or "", thumbnail=r.get("cover") or "",
            referer=page or list_url, origin_page=page or list_url,
            extra={
                # 没有直链时，url 是页面地址 → 前端要走 yt-dlp
                "is_page": not direct,
                "ext": "",
                # 同一条媒体的其它档位直链（如小红书会给多个清晰度）：
                # 首选地址下不了时，下载层会自动换用（与网页抓取那条路径同机制）
                "variants": r.get("variants") or [],
                "uploader": r.get("uploader") or "",
                "site": r.get("site") or "",
                "duration_string": "",
            },
        ))
    if not items:
        # 来源确实返回了条目、但全被类型过滤（图vid混排）时，必须说明，
        # 否则表现成「没有原因的 0 条」（实测 Pinterest 画板踩过）。
        if _dropped:
            _what = "图片" if _want_image else "视频"
            return None, (f"来源拿到了 {len(items_raw)} 个作品，但其中没有{_what}"
                          f"（这个主页是图和视频混排的，{_what}可能排在更靠后的位置，"
                          "可以把「最多作品」调大再试）。")
        return None, note

    # ---------- xhs 笔记页自行下钻：把「笔记页地址」换成「视频直链」 ----------
    # 为什么必须自己下（2026-09-28 实测）：yt-dlp 没有小红书提取器，
    # is_page 条目交给 /api/download/videos 会直接 Unsupported URL。
    # 而带令牌的笔记页**纯 HTTP** 就能拿到 mp4 主地址（实测 25 秒拿 2 条），
    # 所以在这里用 HTTP 并发下钻，把直链填回条目。
    page_items = [m for m in items if m.extra.get("is_page")]
    if page_items and platform == "xhs":
        on_note(f"正在逐篇打开 {len(page_items)} 篇笔记获取视频直链…")
        dres = fetch_many(
            [m.url for m in page_items],
            allow_render=False,
            concurrency=6,
            render_scroll=0,
            on_progress=lambda stage, d, t: on_note(
                f"正在下钻笔记页（{stage}）{d}/{t}"),
        )
        drilled: list[MediaItem] = []
        dropped = 0
        for m, fr in zip(page_items, dres):
            if not fr.ok or not fr.html:
                drilled.append(m)      # 打不开的保留原条目，让下载层给出明确报错
                continue
            got = extract_from_html(fr.html, fr.final_url or m.url, m.url)
            vids = [g for g in got if g.kind == "video"]
            if not vids:
                dropped += 1           # 图文笔记：没有视频，丢弃
                continue
            best = vids[0]
            drilled.append(MediaItem(
                url=best.url, kind="video", source=m.source,
                title=m.title or "", thumbnail=m.thumbnail or best.thumbnail or "",
                referer=m.url, origin_page=m.url,
                extra={
                    "is_page": False,
                    "ext": "",
                    "variants": [v.url for v in vids[1:] if v.url != best.url],
                    "uploader": m.extra.get("uploader") or "",
                    "site": m.extra.get("site") or "",
                    "duration_string": "",
                },
            ))
        items = [m for m in items if not m.extra.get("is_page")] + drilled
        note = (note + "；" if note else "") + \
            f"已逐篇下钻 {len(page_items)} 篇笔记（图文笔记 {dropped} 篇已跳过）。"

    name = profile_sources.PLATFORM_NAMES.get(platform, platform)
    warnings = [
        f"这是一个{name}主页，已用内置来源（{profile_sources.source_name(platform)}）"
        f"展开 {len(items)} 条作品 —— 该平台的作品列表要签名参数 + 登录态，"
        "普通网页抓取不足时由该内置来源补齐。",
    ]
    if any(not m.extra.get("is_page") for m in items):
        warnings.append("工具返回的是**带签名的直链**（会过期），点「下载选中」会立刻下载；"
                        "过一段时间再下需要重新抓一次。")
    if note:
        warnings.append(note)
    return {
        "items": [m.to_dict() for m in items],
        "summary": summarize(items),
        "pages": [{"page": 1, "url": list_url, "method": f"内置来源({name})",
                   "html_bytes": 0, "media": len(items), "links": 0,
                   "elapsed": 0.0, "visible_text": 0}],
        "detail_pages": {"total": 0, "ok": 0, "failed": 0, "urls": []},
        "stats": {"list_pages": 1, "detail_pages": 0, "http_ok": 0,
                  "render_used": 0, "variants_collapsed": 0, "elapsed": 0.0},
        "warnings": warnings,
        "list_url": list_url,
        "renderer": renderer_status(),
        "via": f"builtin-platform:{platform}",
    }, note


def crawl_list(
    list_url: str,
    *,
    max_pages: int = 1,
    max_items: int = 60,
    drill_down: bool = True,
    drill_mode: str = "auto",
    detail_concurrency: int = 4,
    allow_render: bool = True,
    want_kinds: tuple[str, ...] = ("image", "video", "stream"),
    originals_only: bool = False,
    prefetched: dict | None = None,
    on_progress=None,
    should_cancel=None,
    expand_listing: bool = False,
    cookies_file: str | None = None,
    cookies_from_browser: str | None = None,
) -> dict:
    """从列表页/搜索页批量抓取媒体。

    流程：
      1. 抓列表页（HTTP 优先，JS 空壳自动升级渲染）
      2. 双通道提取媒体（HTML + 拦截 JSON）
      3. 自动翻页（按 max_pages，识别失败则停）
      4. 提取详情页链接并按相关性排序
      5. 按需并发下钻详情页，提取原图与视频
      6. 汇总去重、过滤站点装饰资源；originals_only 时丢弃缩略图只留原图

    drill_mode:
      "auto"（默认）自适应判断是否需要下钻。列表页已含足够完整媒体时跳过，
        避免无谓耗时（实测 Pexels 列表页已含 132 个 mp4 直链，下钻 4 页
        却 0 成功、媒体数毫无增长，白花约 115 秒）。
        缩略图占比高或列表页无视频时仍会下钻（阿里即属此类）。
      "always" 总是下钻，用于明确要拿详情页高清原图的场景。
    """
    t0 = time.time()

    # 直链兜底：地址本身就是媒体文件时直接返回，不按网页去抓。
    # （原「网页扫描」的能力，该模块合并后搬到这里）
    direct = _direct_media_item(list_url)
    if direct:
        kept = [direct] if (not want_kinds or direct.kind in want_kinds) else []
        return {
            "items": [m.to_dict() for m in kept],
            "summary": summarize(kept),
            "pages": [{"page": 1, "url": list_url, "method": "direct",
                       "html_bytes": 0, "media": len(kept), "links": 0,
                       "elapsed": 0.0, "visible_text": 0}],
            "detail_pages": {"total": 0, "ok": 0, "failed": 0, "urls": []},
            "stats": {"list_pages": 1, "detail_pages": 0, "http_ok": 1,
                      "render_used": 0, "variants_collapsed": 0,
                      "elapsed": round(time.time() - t0, 2)},
            "warnings": ["该地址本身就是媒体文件，已直接返回，未做页面抓取。"]
                        if kept else
                        ["该地址是媒体文件，但被「媒体类型」筛选排除了。"],
            "list_url": list_url,
            "renderer": renderer_status(),
        }

    stats = CrawlStats()
    all_media: list[MediaItem] = []
    all_links: list[dict] = []
    pages_info: list[dict] = []
    warnings: list[str] = []

    def _emit(stage: str, done: int = 0, total: int = 0, note: str = ""):
        if on_progress:
            try:
                on_progress({"stage": stage, "done": done, "total": total,
                             "note": note, "media": len(all_media)})
            except Exception:
                pass

    # ---------- 0. 视频列表前置：这个地址是不是「一个页面装着多条视频」？ ----------
    #
    # 为什么放在最前面（2026-09-22 用户反馈的「粘博主页永远 0 条」）：
    # TikTok / YouTube / B 站这类平台的博主页，视频地址**不在页面 HTML/DOM 里**，
    # 是前端调接口拿 JSON 再渲染的 —— 走网页抓取只能得到站点自身的 JS 资源，
    # 于是无论怎么下钻都是 0 条。判据只有「要视频 + 页面地址 + 通用工具说它是列表」，
    # 完全不含站点专用规则。命中就直接返回，不再走后面的网页抓取（省时且结果更全）。
    # ---------- 0A. Author image pipelines ----------
    # Generic page scanning mixes avatars/UI/recommendations into social-profile images.
    _kinds = set(want_kinds or ())
    _path_low = (urlparse(list_url).path or "").lower()
    if expand_listing and _kinds and _kinds <= {"image"}:
        from . import profile_sources as _ps_img
        _img_platform = _ps_img.platform_for_url(list_url)
        # V3.0：发现页/搜索页也走小红书专用图文来源。
        # 以前这里只认 /user/profile/，发现页会掉到普通网页抓取 —— 而发现页是纯 JS 渲染，
        # 实测只能捞到 6 张站点装饰图（横幅/图标/埋点），一张正文图都没有。
        if _img_platform == "xhs" and _ps_img.xhs_page_kind(list_url) in {"profile", "feed", "search"}:
            _emit("list", 0, 1, "\u6b63\u5728\u8bfb\u53d6\u5c0f\u7ea2\u4e66\u4f5c\u8005\u4f5c\u54c1\u5217\u8868\u548c\u56fe\u6587\u7b14\u8bb0\u6b63\u6587\u56fe\u7247\u2026")
            xhs_exp, _xhs_note = _expand_xhs_image_source(
                list_url, limit=max(1, min(int(max_items or 30) or 30, 2000)),
                on_note=lambda msg: _emit("detail", 0, 0, msg),
            )
            xhs_exp["stats"]["elapsed"] = round(time.time() - t0, 2)
            _emit("merge", 0, 0, f"\u5df2\u63d0\u53d6 {len(xhs_exp.get('items') or [])} \u5f20\u6b63\u6587\u56fe\u7247")
            return xhs_exp
        if _img_platform == "wb":
            _emit("list", 0, 1, "正在读取微博博主作品流中的图片…")
            img_exp, _img_note = _expand_profile_image_source(
                list_url, limit=max(1, min(int(max_items or 30) or 30, 2000)),
                on_note=lambda msg: _emit("detail", 0, 0, msg),
            )
            img_exp["stats"]["elapsed"] = round(time.time() - t0, 2)
            _emit("merge", 0, 0, f"已提取 {len(img_exp.get('items') or [])} 张博主图片")
            return img_exp
        # Instagram 的图片也必须在平台来源里拿：它的作品数据同样在 JS/登录态后面，
        # 走通用网页抓取只会捞到 56 张头像/UI/推荐位装饰图，正文图片一条都没有
        # （2026-10-27 用户实测：抓图片只抓到一部分，且都是错的）。
        if _img_platform == "ig":
            _emit("list", 0, 1, "正在读取 Instagram 博主的图文作品…")
            ig_exp, _ig_note = _expand_via_platform_source(
                list_url, limit=max(1, min(int(max_items or 30) or 30, 2000)),
                want_kinds=("image",),
                on_note=lambda msg: _emit("detail", 0, 0, msg),
            )
            if ig_exp is not None:
                ig_exp["stats"]["elapsed"] = round(time.time() - t0, 2)
                _emit("merge", 0, 0, f"已提取 {len(ig_exp.get('items') or [])} 张博主图片")
                return ig_exp

    if expand_listing and want_kinds and "video" in want_kinds:
        _emit("list", 0, 1, "正在判断这是不是一个视频列表 / 博主页…")
        # 已知必须靠签名接口 / 登录态拿作品列表的平台，专用来源必须放在前面。
        # 已知主页需要签名/登录态时先走内置平台来源，避免先让通用 yt-dlp 白等超时。
        # YouTube/B站等仍维持通用列表优先。
        from . import profile_sources as _profile_sources
        platform0 = _profile_sources.platform_for_url(list_url)
        profile_first = platform0 in {"dy", "xhs", "ks", "wb", "zhihu", "pin", "ig"}
        exp = None
        exp_err = ""
        pt_exp = None
        pt_note = ""

        if profile_first:
            pt_exp, pt_note = _expand_via_platform_source(
                list_url,
                limit=max(2, min(int(max_items or 30) or 30, 2000)),
                want_kinds=want_kinds,
                on_note=lambda s: _emit("list", 0, 1, s),
            )
            if pt_exp is not None:
                pt_exp["stats"]["elapsed"] = round(time.time() - t0, 2)
                _emit("merge", 0, 0, f"已展开 {len(pt_exp.get('items') or [])} 条作品")
                return pt_exp

        # 权威平台（dy/ks/xhs/pin/ig）专用来源失败后，不再白跑第二个通用 90 秒。
        # 这些主页的作品列表本来就不在普通 HTML / yt-dlp 通用列表里。
        if not (profile_first and _profile_sources.authoritative(list_url)):
            exp, exp_err = _expand_video_listing(
                list_url,
                limit=max(2, int(max_items or 0) or 60),
                cookies_file=cookies_file,
                cookies_from_browser=cookies_from_browser,
                on_note=lambda s: _emit("list", 0, 1, s),
            )
            if exp is not None:
                exp["stats"]["elapsed"] = round(time.time() - t0, 2)
                _emit("merge", 0, 0, f"已展开 {len(exp.get('items') or [])} 条视频")
                return exp

        if not profile_first:
            pt_exp, pt_note = _expand_via_platform_source(
                list_url,
                limit=max(2, min(int(max_items or 30) or 30, 2000)),
                want_kinds=want_kinds,
                on_note=lambda s: _emit("list", 0, 1, s),
            )
            if pt_exp is not None:
                pt_exp["stats"]["elapsed"] = round(time.time() - t0, 2)
                _emit("merge", 0, 0, f"已展开 {len(pt_exp.get('items') or [])} 条作品")
                return pt_exp

        # 两条路都没成：把原因都写清楚，再退回网页抓取
        hints = [h for h in (exp_err, pt_note) if h]
        # 「需要登录」的提示单独说，别包进「核对账号名」的通用文案里
        login_hint = next((h for h in hints if "打开浏览器去登录" in h), "")
        if login_hint:
            warnings.append(login_hint)
        elif hints:
            # 展开失败必须说明原因，不能静默退回网页抓取 ——
            # 否则「限流」「需要登录」会表现成又一次「抓到 0 条」，用户无从判断。
            # 「账号名写错」是最常见的一种（实测 @kvl 应为 @kvi），所以点名提示。
            warnings.append(
                "已尝试用通用视频工具展开该地址，但没成功："
                + "；".join(hints)
                + "（接下来按普通网页继续抓取）。若这是一个博主页 / 频道页，"
                  "请先核对账号名是否写对 —— 拼错、改名、已注销都会读不到。")

        # 抖音 / 小红书 / 快手这类平台：作品列表只在**签名接口**里，
        # 网页抓取拿不到（实测抖音主页 793KB HTML 里 aweme_id 0 个），
        # 能抓到的基本都是站点自己的宣传片 / 直播拉流地址 —— 是垃圾结果。
        # 所以这两条路都没成时**直接如实收尾**，不退回网页抓取去制造垃圾。
        from . import profile_sources as _ps   # 与上面的用法一致，局部导入避免循环依赖
        if _ps.authoritative(list_url):
            _emit("merge", 0, 0, "该平台主页需要外部工具，已停止网页抓取回退")
            return {
                "items": [], "summary": summarize([]), "pages": [],
                "detail_pages": {"total": 0, "ok": 0, "failed": 0, "urls": []},
                "stats": {"list_pages": 0, "detail_pages": 0, "http_ok": 0,
                          "render_used": 0, "variants_collapsed": 0,
                          "elapsed": round(time.time() - t0, 2)},
                "warnings": warnings + [
                    f"{_ps.PLATFORM_NAMES.get(_ps.platform_for_url(list_url), '该平台')}"
                    "主页的作品列表只能通过平台接口拿到，网页抓取这里没有可用的东西 ——"
                    "已停止回退，避免给你一堆站点自己的宣传片 / 直播地址当成结果。"],
                "list_url": list_url,
                "renderer": renderer_status(),
                "via": "external-tool-failed",
            }

    # ---------- 1~3. 列表页（含翻页）----------
    page_url = list_url
    page_no = 1
    visited: set[str] = set()
    prev_fingerprint = ""
    # 输入页的「页面自述」（标题 + 正文片段）+ 该页是不是内容稀薄：
    # 收尾时若一条结果都没有，把这段原文摆给用户 —— 站点侧原因
    # （账号不存在 / 页面已删 / 要登录）与工具侧原因必须能一眼区分。
    first_report: tuple[str, str] = ("", "")
    first_page_thin = False
    while page_no <= max(1, max_pages):
        if should_cancel and should_cancel():
            warnings.append("用户取消")
            break
        if page_url in visited:
            warnings.append(f"第 {page_no} 页 URL 重复，停止翻页")
            break
        visited.add(page_url)

        _emit("list", page_no, max_pages, f"正在抓取列表页第 {page_no} 页")
        # 若调用方已经抓过这个页面，直接复用，不要重复请求。
        # 必要性（实测）：同一 URL 连续请求两次很容易触发站点风控 ——
        # pixabay 第一次渲染拿到 1MB 正常页面，紧接着第二次就被返回 28KB 拦截页。
        fr = (prefetched or {}).get(page_url)
        if fr is None:
            fr = fetch_page(page_url, allow_render=allow_render)
        stats.pages_list += 1
        if fr.method.startswith("render"):
            stats.render_used += 1
        elif fr.ok:
            stats.http_ok += 1

        if not fr.ok:
            warnings.append(f"第 {page_no} 页抓取失败: {fr.error}")
            _emit("list-error", page_no, max_pages, fr.error)
            break

        base = fr.final_url or page_url
        # 提取媒体
        got = extract_from_html(fr.html, base, base)
        for jt in fr.json_texts:
            got.extend(extract_from_json_text(jt, base, base, source="json"))

        # ---- 视频升级：要视频、静态页 0 视频、却有详情链接 → 渲染一次再来 ----
        # 必要性（2026-09-26 小红书主页实测）：
        #   HTTP 静态页只有 9 条 /explore/<id> 链接（都不带 xsec_token，打开会被拒），
        #   渲染后才有 31 条 /user/profile/<uid>/<id>?xsec_token=… 的笔记链接 ——
        #   不渲染就永远下钻不到视频，表现成「抓了 449 张图、0 个视频」。
        # 判据完全通用：只看「用户要视频 + 这一页一个视频都没抓到 + 有详情链接 + 当前是静态抓取」。
        if (page_no == 1 and allow_render and fr.method == "http" and "video" in (want_kinds or ())
                and not any(getattr(m, "kind", "") == "video" for m in got)):
            _probe_links = extract_page_links(fr.html, "\n".join(fr.json_texts), base)
            if _probe_links:
                _emit("list", page_no, max_pages,
                      "这一页没有视频但有详情链接，正在用浏览器再取一次（列表数据可能是 JS 加载）")
                # 渲染预算单独放宽：默认 TIMEOUT=25 秒对重前端站点太紧
                # （实测小红书主页渲染要 12~30 秒，25 秒会在 JS 跑完前就截断，
                #  拿到的仍是那 9 条不含访问令牌的链接 —— 白渲染一次）。
                fr2 = fetch_page(page_url, allow_render=True, force_render=True,
                                 timeout=max(TIMEOUT, 45),
                                 render_settle_ms=4000, render_scroll=3)
                if fr2.ok and fr2.html:
                    got2 = extract_from_html(fr2.html, fr2.final_url or base, base)
                    for jt in fr2.json_texts:
                        got2.extend(extract_from_json_text(jt, base, base, source="json"))
                    _links2 = extract_page_links(fr2.html, "\n".join(fr2.json_texts),
                                                 fr2.final_url or base)
                    if len(_links2) > len(_probe_links) or \
                            any(("token=" in l.get("url", "") or "sign=" in l.get("url", ""))
                                for l in _links2):
                        fr, got, base = fr2, got2, (fr2.final_url or base)
                        stats.render_used += 1
                        stats.pages_list += 1
                        warnings.append(
                            f"这一页静态内容里没有视频，改用浏览器渲染后重新取了一遍"
                            f"（详情链接 {len(_probe_links)} → {len(_links2)} 条）："
                            "该站的列表数据由 JS 加载，静态抓取拿不到。")
                        _emit("list", page_no, max_pages,
                              f"渲染后链接 {len(_probe_links)} → {len(_links2)} 条")
                    else:
                        warnings.append(
                            f"这一页没有视频，尝试用浏览器渲染再取一次也没拿到更多链接"
                            f"（{len(_probe_links)} → {len(_links2)} 条）。"
                            "该站列表数据可能需要登录态才能加载 —— "
                            "可在「设置」里用「打开浏览器去登录」登一次该站再抓。")
                else:
                    warnings.append(
                        f"这一页没有视频，尝试用浏览器渲染再取一次但渲染失败"
                        f"（{fr2.error or '未知原因'}）。可能是登录态配置被其它窗口占用，"
                        "关掉其它浏览器窗口后重试。")

        # 内容指纹：检测「URL 变了但内容没变」的情况。
        # 必要性：无限滚动站点（如 Bing 图片搜索）的 HTML 里没有分页链接，
        # 分页参数只能靠猜；猜错时 URL 不重复、上面的 visited 检查拦不住，
        # 会反复抓到同一页内容直到耗尽 max_pages。
        fingerprint = _content_fingerprint(got)
        if page_no > 1 and fingerprint and fingerprint == prev_fingerprint:
            warnings.append(
                f"第 {page_no} 页内容与上一页完全相同（{len(got)} 个媒体指纹一致），"
                "判定翻页未生效并停止。该站点可能使用无限滚动加载，"
                "不支持传统分页参数；可尝试增大单页抓取量或直接在页面上滚动加载。"
            )
            _emit("list", page_no, max_pages, f"第 {page_no} 页内容重复，停止翻页")
            break
        prev_fingerprint = fingerprint

        all_media.extend(got)
        if page_no == 1:
            first_report = _page_self_report(fr.html)
            first_page_thin = (fr.visible_text_len < 400) or (not got)

        # 提取详情页链接
        json_blob = "\n".join(fr.json_texts)
        links = extract_page_links(fr.html, json_blob, base)
        all_links.extend(links)

        pages_info.append({
            "page": page_no, "url": base, "method": fr.method,
            "html_bytes": len(fr.html), "media": len(got),
            "links": len(links), "elapsed": round(fr.elapsed, 1),
            "visible_text": fr.visible_text_len,
        })
        # 渲染后仍近乎无正文：站点反爬拦截页 / 需登录 / 地域限制。
        #
        # 判据在 2026-09-20 放宽过一次：原来只看「可见文本 < 100」，
        # 但 tiktok / instagram 这类主页返回的验证页**带完整导航与登录提示文字**，
        # 可见文本并不少，内容却一条都取不到 —— 于是既没提示、也没结果，
        # 用户只看到「抓到 2 个媒体」（其实那 2 条是站点自身的 JS 资源）。
        # 现在补一条：**一个媒体都没拿到**时也去查一次验证墙。
        if fr.method.startswith("render") and (fr.visible_text_len < 100 or not got):
            wall = _detect_wall(fr.html)
            if wall:
                # 认出具体是哪家的验证，比笼统的「正文为空」有用得多
                warnings.append(
                    f"第 {page_no} 页抓取被拦截：{wall}"
                    "（可尝试稍后重试、换网络出口，或配置该站点 Cookies）")
            else:
                warnings.append(
                    f"第 {page_no} 页已用浏览器渲染，但页面正文仍为空（可见文本 "
                    f"{fr.visible_text_len} 字符）。站点可能返回了反爬拦截页、要求登录，"
                    "或有地域限制。可尝试：稍后重试、在设置中配置该站点 Cookies、"
                    "或确认链接在浏览器中人工打开是否正常。"
                )
        elif not got:
            # 一条媒体都没拿到（但也没撞验证墙）→ 把页面自己的话摆出来。
            # 站点侧原因（账号不存在 / 页面已删除 / 要登录）必须与工具侧原因
            # 能被一眼区分，否则用户会以为是工具坏了（2026-09-22 反馈的问题）。
            p_title, p_text = _page_self_report(fr.html)
            detail = []
            if p_title:
                detail.append(f"页面标题《{p_title}》")
            if p_text:
                detail.append(f"页面自述「{p_text}」")
            warnings.append(
                f"第 {page_no} 页没有识别到任何媒体（方式={fr.method}"
                f"{'，可见文本 ' + str(fr.visible_text_len) + ' 字符' if fr.visible_text_len < 100 else ''}）。"
                + ("；".join(detail) + "。" if detail else "")
                + "请对照上面这段页面原文判断：若它说明「账号/页面不存在」「已删除」"
                  "或「需要登录」，那是站点侧原因；需要登录的站点可在「设置」里配置 Cookies。"
            )
        _emit("list", page_no, max_pages,
              f"第 {page_no} 页：{len(got)} 个媒体 / {len(links)} 个详情链接")

        if page_no >= max_pages:
            break

        # 翻页：优先用站点自己给出的分页链接，猜参数只作兜底
        pg = detect_pagination(fr.html, base, page_no)
        nxt = _next_page_url(base, page_no + 1, page_no, pg)
        if not nxt or nxt in visited:
            warnings.append(
                f"第 {page_no} 页未能识别下一页，停止翻页。"
                f"（分页方式={pg.get('mode')}、参数={pg.get('param')}、"
                f"步长={pg.get('step')}、已识别页码={sorted(pg.get('page_urls') or {})[:8]}）"
            )
            break
        page_url = nxt
        page_no += 1

    # ---------- 4. 选定要下钻的详情页 ----------
    links_dedup = {}
    for l in all_links:
        u = l["url"]
        if u not in links_dedup or l.get("score", 0) > links_dedup[u].get("score", 0):
            links_dedup[u] = l
    ranked = sorted(links_dedup.values(), key=lambda x: -x.get("score", 0))

    # 自适应门控：列表页已经拿到足够的「完整媒体」时，下钻是纯浪费。
    # 实测教训：Pexels 列表页已含 132 个 mp4 直链，下钻 4 个详情页却
    # 0 个成功、媒体数全程 555→555 毫无增长，白花了约 115 秒。
    # 但阿里列表页只有 300x300 缩略图（thumbs 占比高），下钻才能拿到原图，必须保留。
    #
    # 两个统计口径上的坑（avjoy.me 实测踩中）：
    #   a) **先剔除装饰资源再统计**。站点装饰视频（如 site.webm）会被算成一个
    #      「完整媒体」，让 n_video 从 0 变成 1，从而骗过「列表页没有视频」这条判据。
    #   b) **「要视频但列表页没视频」必须在缩略图占比之前判**。否则一旦缩略图识别
    #      失效（thumb_ratio 算成 3%），就会落到最后的「无需下钻」分支上。
    list_media = [m for m in dedupe(all_media) if not _is_decoration(m.url)]
    n_total = len(list_media)
    n_thumb = sum(1 for m in list_media if m.is_thumb)
    n_video = sum(1 for m in list_media if m.kind in ("video", "stream"))
    # 「完整视频」要排除悬停预览 / 低码率试看流这类预览片段。
    # 实测教训（pornhub）：列表页 84 个"视频"全部是 data-mp4 悬停动画和
    # 180P 试看流，旧统计把它们当成完整视频，得出「无需下钻」的错误结论，
    # 详情页里 48 个真视频一个都没拿到。
    n_video_real = sum(1 for m in list_media
                       if m.kind in ("video", "stream") and not m.is_thumb)
    thumb_ratio = (n_thumb / n_total) if n_total else 0.0

    drill_reasons: list[str] = []
    if not drill_down:
        drill_reasons.append("未开启下钻")
    elif drill_mode == "always":
        drill_reasons.append("用户指定总是下钻")
    elif n_total == 0:
        drill_reasons.append("列表页未提取到媒体，尝试从详情页获取")
    elif ("video" in want_kinds and n_video_real == 0
          and n_video < len(ranked)):
        drill_reasons.append(
            f"列表页有 {n_total} 个媒体但只有 {n_video} 个预览片段、"
            "没有任何完整视频，视频应在详情页播放器里"
        )
    elif ("video" in want_kinds and n_video_real * 2 < len(ranked)):
        # 视频远少于详情页链接：列表页的"完整视频"只覆盖少数条目，
        # 其余条目仍要下钻。实测 pexels 列表页 132 个直链视频远多于
        # 详情链接，不会触发这条，跳过下钻的优化得以保留。
        drill_reasons.append(
            f"完整视频仅 {n_video_real} 个，远少于 {len(ranked)} 个详情页，"
            "其余视频需下钻获取"
        )
    elif n_total < 10:
        drill_reasons.append(f"列表页媒体过少（{n_total} 个）")
    elif thumb_ratio >= 0.5:
        drill_reasons.append(f"列表页多为缩略图/预览（{n_thumb}/{n_total}），需下钻取原图")
    else:
        drill_reasons.append(
            f"列表页已含 {n_total} 个完整媒体（完整视频 {n_video_real}、"
            f"缩略图/预览占比 {thumb_ratio:.0%}），无需下钻"
        )

    should_drill = bool(drill_down and ranked and (
        drill_mode == "always" or "无需下钻" not in drill_reasons[0]
    ))
    targets = ranked[:max(0, max_items)] if should_drill else []

    if drill_down and not ranked:
        warnings.append("未能从列表页识别出详情页链接，将只返回列表页内的媒体。")
    elif not should_drill and drill_mode == "auto":
        warnings.append(f"已跳过详情页下钻：{drill_reasons[0]}。"
                        "如需强制下钻（例如想拿更高清原图），请把下钻模式改为「总是下钻」。")

    # ---------- 5. 并发下钻详情页 ----------
    detail_media: list[MediaItem] = []
    detail_fail = 0
    anchored_pages = 0      # 有多少个详情页给出了主视频声明（og:video / ld+json）
    if targets:
        _emit("detail", 0, len(targets), f"准备下钻 {len(targets)} 个详情页")
        dres = fetch_many(
            [t["url"] for t in targets],
            allow_render=allow_render,
            concurrency=detail_concurrency,
            render_scroll=2,
            on_progress=lambda stage, d, t: _emit(
                "detail", d, t, f"正在下钻详情页（{stage}）{d}/{t}"),
            should_cancel=should_cancel,
        )
        for t, fr in zip(targets, dres):
            stats.pages_detail += 1
            if fr.method.startswith("render") and "+render-failed" not in fr.method:
                stats.render_used += 1
            if not fr.ok:
                detail_fail += 1
                continue
            base = fr.final_url or t["url"]
            got = extract_from_html(fr.html, base, base)
            for jt in fr.json_texts:
                got.extend(extract_from_json_text(jt, base, base, source="json"))

            # ---- 只留页面主视频（ld+json VideoObject / og:video 锚点）----
            #
            # 详情页内嵌的相关推荐视频地址不丢掉的话，46 条的搜索列表会带回
            # 500+ 条无关视频（实测）。锚点命中的站点只保留主视频；
            # 锚点一条都没命中（可能站点没有这些标准标记）时保持原样，
            # 宁可多给不误删。
            anchor_key, vobj, declared_url = _page_main_video_anchor(fr.html, base)
            anchored = False
            # 过滤**之前**先把流条目（m3u8/mpd）存住：锚点过滤按 MP4 归并键匹配，
            # 流地址没有这个键会被一并丢掉 —— 而它往往带着 MP4 直链没有的高清档
            # （实测 xvideos：MP4 只有 240p/360p，HLS master 里有 480/720/1080）。
            page_streams = [g for g in got if g.kind == "stream"]
            if anchor_key:
                page_vids = [g for g in got if g.kind in ("video", "stream")
                             and _video_variant_info(g.url)[0] == anchor_key]
                if page_vids:
                    got = [g for g in got if g.kind not in ("video", "stream")
                           or _video_variant_info(g.url)[0] == anchor_key]
                    anchored = True
                    anchored_pages += 1
                    # 主视频的 HLS/DASH master：整页只有一条流地址时才保留 ——
                    # 此时它几乎必然是播放器的主视频流；有多条时无法确认哪条
                    # 属于主视频（可能是广告流），宁可不给不给错。
                    hls_urls = {g.url for g in page_streams
                                if g.url.lower().split("?")[0].endswith((".m3u8", ".mpd"))}
                    if len(hls_urls) == 1:
                        hitem = next((g for g in page_streams if g.url == next(iter(hls_urls))),
                                     None)
                        if hitem is not None:
                            heights = _hls_master_heights(hitem.url)
                            mp4_max = max((_video_variant_info(g.url)[2] or 0)
                                          for g in got if g.kind == "video") \
                                if any(g.kind == "video" for g in got) else 0
                            if heights and heights[0] > mp4_max:
                                # 流的最高档高于全部 MP4 直链 → 这些 MP4 只是同一部片
                                # 的低清副本。全部舍弃，一部片只留一条（高清流）——
                                # 否则同一部片占两张卡，全选下载时高清低清各下一份
                                # （2026-09-29 用户实测：54 条实际应为 27 条）。
                                got = [g for g in got if g.kind != "video"]
                                hitem.height = heights[0]    # 最高档，给前端展示/排序用
                                hitem.extra["stream_heights"] = heights
                            elif heights and heights[0] <= mp4_max:
                                # 流里没有比 MP4 直链更高的档 → 这条流没有保留价值
                                # （MP4 直链下载更快），不引入重复卡片。
                                hitem = None
                            if hitem is not None:
                                got.append(hitem)
                    # 标记「站点声明的那条播放地址」。
                    # 必要性：同一部片可能有多个档位地址，其中名字看着最高清的
                    # （图片 CDN 上的 original_<id>.mp4）实测下不了（470 No Token），
                    # 而 og:video / ld+json 声明的这条才是真能播的。
                    # 归并成一个条目时靠这个标记保住声明地址（详见
                    # extractor.collapse_video_variants 的排序说明）。
                    # 只按**地址完全相同**打标，避免把同 key 的其它档位一并标成声明。
                    if declared_url:
                        for g in got:
                            if g.url == declared_url:
                                g.extra["declared"] = True
            # 给视频打「是否由本页主视频声明锚定」标记，供收尾时甄别聚合页泄漏
            for g in got:
                if g.kind in ("video", "stream"):
                    g.extra["main_video"] = anchored

            # ---- 标题与封面 ----
            #
            # 为什么必须在这里补：用户选「仅视频」时，最终结果只留下视频条目，
            # 而列表页的标题/缩略图都属于图片，全被类型筛选丢掉 ——
            # 卡片于是既没名字也没预览图。正确做法是把它们挂到视频条目上。
            #
            # 封面优先级：视频自身的 <video poster>（extract_from_html 已挂好）
            #            > 详情页里的 poster 图
            #            > 列表页该条目的缩略图（随详情链接一起带过来）
            page_poster = next(
                (g.url for g in got
                 if g.kind == "image" and g.source.endswith("attr:poster")), "")
            # ld+json VideoObject.thumbnailUrl 也是标准封面来源（可能是个列表）
            vthumb = ""
            if vobj:
                vt = vobj.get("thumbnailUrl")
                if isinstance(vt, list):
                    vt = next((x for x in vt if isinstance(x, str)), "")
                if isinstance(vt, str) and vt:
                    vthumb = normalize(vt, base)
            fallback_poster = page_poster or t.get("thumb") or vthumb or ""

            # 详情页标题（og:title / <title> / <h1>）；
            # 列表页传下来的标题可能为空、也可能是一串时长垃圾，故取更优的那个。
            pt = page_title(fr.html)
            link_title = _clean_title(t.get("title") or "")
            best_title = (pt if _title_quality(pt) > _title_quality(link_title)
                          else (link_title or pt))

            for g in got:
                if g.kind in ("video", "stream"):
                    if not g.thumbnail:
                        g.thumbnail = fallback_poster
                    if _title_quality(g.title) < _title_quality(best_title):
                        g.title = best_title or g.title
                elif not g.title:
                    g.title = best_title
                g.extra["detail_page"] = base
            detail_media.extend(got)

    # ---- 主视频声明优先（只针对视频）----
    #
    # 站点普遍用 og:video / ld+json 声明「本页的视频」时（锚点命中 ≥3 页），
    # 只有被声明的视频才算内容：
    #   a) 没有声明的页面（帮助页 / 政策页 / 相册页 / GIF 页 / 聚合页）带回的视频
    #   b) 列表页里的视频 —— 那是列表卡片数据，内容以详情页声明为准
    # 实测 pornhub：84 条结果里 47 条来自这两类噪音（政策页 4 + 相册 2 +
    # 9 个未锚定页 34 + 列表页 12），而用户要的只有 46 条。
    # 放在合并/去重之前做：去重是按 URL 保留先出现的那条，
    # 若先合并，「列表页那条」可能把「详情页那条」顶掉、标记随之丢失。
    # 锚点命中不足 3 页时（站点没有这类标准标记）不启用，保持原行为。
    dropped_unverified = 0
    if "video" in want_kinds and anchored_pages >= 3:
        before_verified = len(detail_media) + len(all_media)
        detail_media = [m for m in detail_media
                        if m.kind not in ("video", "stream")
                        or m.extra.get("main_video")]
        all_media = [m for m in all_media if m.kind not in ("video", "stream")]
        dropped_unverified = before_verified - len(detail_media) - len(all_media)
        if dropped_unverified:
            warnings.append(
                f"已丢弃 {dropped_unverified} 个视频条目：它们来自没有主视频声明的页面"
                "（帮助 / 政策 / 相册 / GIF / 聚合页）或列表页的卡片数据，"
                "不属于内容页声明的视频。"
            )

    # ---------- 6. 汇总 ----------
    _emit("merge", 0, 0, "正在汇总去重")
    merged = dedupe(all_media + detail_media)
    merged = enrich_with_originals(merged)
    merged = dedupe(merged)

    # 合并同一张图的多个尺寸版本。
    # 实测必要性：pixabay 搜索页会把同一张图的 640 / 1280 两个版本都放进页面，
    # 不去重的话结果里一半是重复的同一张图。
    before_variants = len(merged)
    merged = collapse_variants(merged)
    # 同一部视频的多个清晰度档位（240P / 720P / original_…）也要收敛成一条，
    # 否则同一部片会重复出现 3~6 次。
    # ⚠️ 这一行在 2026-09-18 曾被另一处改动覆盖掉（只剩导入语句），
    #    结果 pornhub 一页泄出 3 条、排查很久 —— 改动这块务必确认它还在。
    merged = collapse_video_variants(merged)
    variants_collapsed = before_variants - len(merged)

    # 过滤站点装饰资源（favicon/logo/图标/占位图）。
    # 此前只在「是否需要渲染」的判定里排除装饰图，没用于最终输出，
    # 导致阿里被反爬拦截时，返回的 8 个结果全是 font_*.svg、14-14.png 这类 UI 图标。
    before_deco = len(merged)
    merged = [m for m in merged if not _is_decoration(m.url)]
    deco_removed = before_deco - len(merged)
    if before_deco and not merged:
        warnings.append("提取到的全部是站点图标/装饰资源，没有真实内容媒体。"
                        "该站点可能返回了反爬拦截页或需要登录。")
    elif deco_removed:
        warnings.append(
            f"已过滤 {deco_removed} 个站点装饰资源（横幅 / 图标 / 埋点像素），"
            "它们不属于页面内容。"
        )
    if variants_collapsed:
        warnings.append(
            f"已合并 {variants_collapsed} 个重复版本（同一张图的不同分辨率 / "
            "同一部视频的不同清晰度档位），每个只保留最清晰的一份。"
        )

    # 类型过滤
    before_kinds = len(merged)
    empty_explained = False     # 是否已解释过「为什么是 0 条」（防止叠出矛盾提示）
    if want_kinds:
        merged = [m for m in merged if m.kind in want_kinds]
    # 「抓到东西了，但没有你要的类型」必须说清楚 ——
    # 否则用户看到的就是一个孤零零的「共 0 个媒体」，无法区分
    # 「页面里本来就没视频」和「工具抓不到」（用户反馈的困惑点）。
    if before_kinds and not merged:
        want_label = "、".join({"image": "图片", "video": "视频", "stream": "流媒体"}
                               .get(k, k) for k in want_kinds)
        extra_hint = ""
        # 视频页抓到一堆图片 = 这个地址更适合用「抓取图片」——直接指路，别让人猜
        if "video" in (want_kinds or ()) and "image" not in (want_kinds or ()):
            extra_hint = "要抓这些图片请切到「抓取图片」页，粘同一个地址。"
        warnings.append(
            f"这个页面抓到了 {before_kinds} 个媒体，但没有一个属于当前要的类型"
            f"（{want_label}）—— 即页面里本来就没有这类内容，不是抓取失败。"
            + extra_hint)
        # 已经解释清楚「为什么是 0」了，就不要再叠一条「最终没有拿到任何结果 +
        # 输入页原文」—— 两条一起出现会自相矛盾（实测 Pinterest 画板：
        # 先说抓到 934 个媒体，紧接着说最终没拿到任何结果）。
        empty_explained = True
    # 要视频时，若已有完整视频，则丢弃预览片段（悬停预览/低码率试看流）。
    # 实测必要性：pornhub 列表页的 84 个"视频"里绝大多数是 180P 试看流和
    # 悬停预览动画，不下掉的话结果里一半是垃圾条目（用户反馈的 bug）。
    # 兜底：若整站只有预览片段（没有完整视频），仍然保留，宁可多给不误删。
    if "video" in want_kinds:
        real_videos = [m for m in merged
                       if m.kind in ("video", "stream") and not m.is_thumb]
        if real_videos:
            before_pv = len(merged)
            merged = [m for m in merged
                      if not (m.kind in ("video", "stream") and m.is_thumb)]
            dropped_pv = before_pv - len(merged)
            if dropped_pv:
                warnings.append(
                    f"已过滤 {dropped_pv} 个低清预览片段（悬停预览 / 试看流），"
                    "它们不是完整视频。"
                )
    # 只留原图（丢弃缩略图）
    if originals_only:
        kept = [m for m in merged if not m.is_thumb]
        if kept:
            merged = kept
    # 视频优先，其次原图，最后缩略图；同类里大图排前面
    # （能读出尺寸的按面积降序，读不出的保持原位 —— 实测阿里结果里
    #   横幅和图标排在商品图前面，用户第一眼看到的全是站点 UI）
    kind_rank = {"video": 0, "stream": 1, "image": 2, "unknown": 3}

    def _sort_key(m: MediaItem):
        size = guess_size_from_url(m.url)
        area = (size[0] * size[1]) if size else 0
        return (kind_rank.get(m.kind, 9), m.is_thumb, -area, m.url)

    merged.sort(key=_sort_key)

    # 补 referer：下钻得到的媒体用其详情页做 Referer（防盗链）
    for m in merged:
        if not m.referer:
            m.referer = m.extra.get("detail_page") or m.origin_page or list_url

    # 最后一条、也是最重要的一条：一条结果都没有时，把**输入页的原文**摆出来。
    # 为什么必须这么做：用户看到「共 0 个媒体」时，分不清是「页面本来就没有」、
    # 「账号/页面不存在」还是「工具坏了」。与其猜，不如把页面自己的话给他看。
    if (not merged and not empty_explained and first_page_thin
            and (first_report[0] or first_report[1])):
        t_, txt_ = first_report
        from . import cookie_store
        ck = cookie_store.status()
        cookie_hint = ("" if ck.get("count") else
                       "（若该地址需要登录：现在抓取与浏览器渲染都是匿名访问，"
                       "请在「设置」里填 cookies.txt 路径或选「从浏览器读取 Cookies」后重试）")
        warnings.append(
            "最终没有拿到任何结果。输入页的原文如下，请对照判断是站点侧还是抓取侧："
            + (f"标题《{t_}》；" if t_ else "")
            + (f"正文「{txt_}」。" if txt_ else "")
            + "若这段原文说明「账号/页面不存在」「已删除/无法访问」或「需要登录」，"
              "那就是站点侧原因（拼写、改名、注销、登录墙），不是抓取逻辑的问题。"
            + cookie_hint
        )

    stats.elapsed = time.time() - t0
    return {
        "items": [m.to_dict() for m in merged],
        "summary": summarize(merged),
        "pages": pages_info,
        "detail_pages": {
            "total": len(targets),
            "ok": len(targets) - detail_fail,
            "failed": detail_fail,
            "urls": [{"url": t["url"], "title": t.get("title", ""),
                      "score": t.get("score", 0)} for t in targets[:200]],
        },
        "stats": {
            "list_pages": stats.pages_list,
            "detail_pages": stats.pages_detail,
            "http_ok": stats.http_ok,
            "render_used": stats.render_used,
            "variants_collapsed": variants_collapsed,
            "elapsed": round(stats.elapsed, 1),
        },
        "warnings": warnings,
        "list_url": list_url,
        "renderer": renderer_status(),
    }


# ---------- 整站抓取 ----------

# 整站遍历时明确跳过、不会放媒体素材的路径
_SITE_SKIP_RE = re.compile(
    r"/(login|logout|signin|signup|register|cart|checkout|my-account|"
    r"wp-admin|wp-json|cdn-cgi|feed|rss|sitemap|search)(/|$|\?)", re.I)


def _is_site_skippable(url: str) -> bool:
    """整站遍历时要跳过的地址：登录/购物车/后台/订阅源，以及资源文件本身。

    注意不能一刀切「只跟 .html」：很多 CMS 用无扩展名路径（/products/、/case/），
    那些正是内容页，必须抓。
    """
    path = urlparse(url).path or "/"
    if _SITE_SKIP_RE.search(path):
        return True
    e = ext_of(url)
    if e in PAGE_EXTS:
        return False                            # .html/.php/.aspx 正是网页
    if e in (IMAGE_EXTS | VIDEO_EXTS | STREAM_EXTS | SKIP_EXTS):
        return True                             # 资源文件，不是页面
    return False                                # 无扩展名 → 当页面处理


def _site_key(url: str) -> str:
    """整站遍历的去重键：把「同一页面的不同写法」归一到一起。

    实测必要性：oreelighting.com 的首页同时被链成 `/` 和 `/index.php`，
    不归一就会重复抓一遍，白白占掉一个页数名额、多发一次请求。
    """
    try:
        p = urlparse(url)
    except ValueError:
        return url
    path = p.path or "/"
    path = re.sub(r"/(?:index|default)\.(?:php|html?|aspx?|jsp)$", "/", path, flags=re.I)
    return f"{p.scheme}://{p.netloc}{path}" + (f"?{p.query}" if p.query else "")


# 约定俗成的站点地图位置（sitemap 是 Web 标准，不是站点专用规则）
_SITEMAP_FALLBACKS = (
    "/sitemap.xml", "/sitemap_index.xml", "/sitemap-index.xml",
    "/sitemap/sitemap.xml", "/wp-sitemap.xml",
)
_LOC_RE = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.I)
_ROBOTS_SITEMAP_RE = re.compile(r"^\s*sitemap\s*:\s*(\S+)\s*$", re.I | re.M)


def _is_sitemap_xml(url: str) -> bool:
    return (url or "").lower().endswith((".xml", ".xml.gz"))


def _sitemap_body(resp) -> str:
    """取 sitemap 正文；.xml.gz 自动解压（Pexels 等站点的地图就是 gzip）。"""
    raw = resp.content or b""
    if raw[:2] == b"\x1f\x8b" or (resp.url or "").lower().endswith(".gz"):
        try:
            import gzip
            raw = gzip.decompress(raw)
        except Exception:  # noqa: BLE001
            pass
    try:
        return raw.decode("utf-8", "ignore")[:4 * 1024 * 1024]
    except Exception:  # noqa: BLE001
        return ""


def _collect_sitemap_urls(
    start_url: str, *, max_urls: int = 3000, timeout: int = 15,
    same_host: bool = True,
) -> tuple[list[str], str]:
    """从 robots.txt / sitemap.xml 收集整站页面地址，返回 (地址列表, 说明)。

    为什么整站抓取必须有它（2026-09-22）：
      纯靠「首页 → 顺着链接走」是**漏斗式**发现，越往后越难覆盖全 ——
      实测 oreelighting.com 抓满 20 页后队列里还剩 33 页没走。
      sitemap 是站点自己声明的**完整页面清单**（WordPress / Shopify / 主流 CMS 都自带），
      把它当种子能一次拿到全站地址。这是 Web 标准（robots.txt 的 Sitemap 指令 +
      sitemap.xml 协议），不含任何站点专用规则。
    """
    p = urlparse(start_url)
    root = f"{p.scheme}://{p.netloc}"
    host = (p.hostname or "").lower()
    host = host[4:] if host.startswith("www.") else host

    tried: list[str] = []
    maps: list[str] = []

    # 1) robots.txt 里声明的 sitemap（最权威：站点自己写的）
    try:
        r, _errs = http_get_with_fallback(f"{root}/robots.txt", timeout=timeout)
        if r is not None and r.status_code < 400:
            for m in _ROBOTS_SITEMAP_RE.finditer(r.text or ""):
                u = m.group(1).strip()
                if u.startswith("//"):
                    u = f"{p.scheme}:{u}"
                elif u.startswith("/"):
                    u = root + u
                if u.startswith("http"):
                    maps.append(u)
    except Exception:  # noqa: BLE001
        pass

    # 2) 没有声明就用约定俗成的位置
    for path in _SITEMAP_FALLBACKS:
        if len(maps) >= 5:
            break
        maps.append(root + path)

    urls: list[str] = []
    seen: set[str] = set()
    fetched = 0
    for sm in maps:
        if fetched >= 6 or len(urls) >= max_urls:
            break
        # 跨域的 sitemap（有些站把地图放在静态域）也允许拉，但只收同域页面地址
        try:
            r, _errs = http_get_with_fallback(sm, timeout=timeout)
        except Exception:  # noqa: BLE001
            continue
        if r is None or r.status_code >= 400:
            continue
        body = _sitemap_body(r)
        if "<" not in body:
            continue
        locs = _LOC_RE.findall(body)
        if not locs:
            # 有响应但没列出任何页面地址（例如站点把 /sitemap.xml 兜底成首页 HTML）
            # —— 不能算「读到了站点地图」，否则界面会出现
            # 「从站点地图读到 0 个页面地址」这种自相矛盾的提示（实测 uskyled.com）。
            continue
        fetched += 1
        tried.append(sm)
        nested = [u for u in locs if _is_sitemap_xml(u)]
        for u in locs:
            if _is_sitemap_xml(u):
                continue
            h = (urlparse(u).hostname or "").lower()
            h = h[4:] if h.startswith("www.") else h
            if same_host and host and h != host:
                continue
            if u in seen:
                continue
            seen.add(u)
            urls.append(u)
            if len(urls) >= max_urls:
                break
        # sitemap 索引：下一层再取几个（只一层，避免无限递归）
        for n in nested[:4]:
            if fetched >= 6 or len(urls) >= max_urls:
                break
            try:
                nr, _e = http_get_with_fallback(n, timeout=timeout)
            except Exception:  # noqa: BLE001
                continue
            if nr is None or nr.status_code >= 400:
                continue
            fetched += 1
            for u in _LOC_RE.findall(_sitemap_body(nr)):
                if _is_sitemap_xml(u):
                    continue
                h = (urlparse(u).hostname or "").lower()
                h = h[4:] if h.startswith("www.") else h
                if same_host and host and h != host:
                    continue
                if u in seen:
                    continue
                seen.add(u)
                urls.append(u)
                if len(urls) >= max_urls:
                    break

    note = (f"从站点地图（{tried[0]}）读到 {len(urls)} 个页面地址" if (tried and urls)
            else "站点没有可用的 sitemap（或它没列出页面地址），改为顺着页面链接遍历")
    return urls, note


def crawl_site(
    start_url: str,
    *,
    max_pages: int = 30,
    max_depth: int = 2,
    same_host: bool = True,
    allow_render: bool = False,
    concurrency: int = 4,
    originals_only: bool = False,
    want_kinds: tuple[str, ...] = ("image", "video", "stream"),
    use_sitemap: bool = True,
    on_progress=None,
    should_cancel=None,
) -> dict:
    """全站抓取（独立站）：从起始页出发把同域页面走一遍，汇总全站媒体。

    和 crawl_list 的分工（用户 2026-09-22 明确要求两者不要混在一起）：
      crawl_list**假设**「一个列表页 + 若干详情页」，适合商品列表、搜索结果页、博主页；
      crawl_site**不假设**任何页面结构，靠遍历把整站的素材捞出来 ——
      适合独立站（公司官网、博客、作品站）这类没有统一列表页、素材散落各页的站点。

    链接策略：
      - 种子优先用站点地图（sitemap.xml / robots.txt 声明）—— 站点自己给的完整清单
      - 其余靠同域链接广度优先遍历（跳过登录/购物车/后台/订阅源等无素材页面）
      - 同页新链接按相关性分排序后入队，让内容页先于导航页被访问
      - allow_render=True 时仍是「逐页自适应」：静态 HTTP 优先，只有判定是 JS 空壳
        的页面才上浏览器 —— 所以开着它也不会把每一页都渲染一遍
    """
    t0 = time.time()
    stats = CrawlStats()
    all_media: list[MediaItem] = []
    pages_info: list[dict] = []
    warnings: list[str] = []

    def _emit(stage: str, done: int = 0, total: int = 0, note: str = ""):
        if on_progress:
            try:
                on_progress({"stage": stage, "done": done, "total": total,
                             "note": note, "media": len(all_media)})
            except Exception:
                pass

    base_host, _ = _host_path_of(start_url)
    if not base_host:
        return {"items": [], "summary": summarize([]), "pages": [],
                "detail_pages": {"total": 0, "ok": 0, "failed": 0, "urls": []},
                "stats": {}, "warnings": ["起始地址无效"], "list_url": start_url,
                "mode": "site"}

    def _same_host(u: str) -> bool:
        h, _p = _host_path_of(u)
        return h == base_host

    visited: set[str] = {_site_key(start_url)}
    queue: list[tuple[str, int, str]] = [(start_url, 0, "")]
    first_report: tuple[str, str] = ("", "")
    first_page_thin = False
    _emit("list", 0, max_pages, "准备抓取整站")

    # 站点地图种子：站点自己声明的完整页面清单，先入队（最全的发现方式）。
    # 放在起始页之后、BFS 发现之前 —— 队列是 FIFO，所以 sitemap 里的页面会先被访问。
    sitemap_added = 0
    if use_sitemap:
        _emit("list", 0, max_pages, "正在读取站点地图（sitemap）")
        sm_urls, sm_note = _collect_sitemap_urls(start_url)
        for u in sm_urls:
            key = _site_key(u)
            if key in visited or _is_site_skippable(u):
                continue
            if same_host and not _same_host(u):
                continue
            visited.add(key)
            queue.append((u, 0, "sitemap"))
            sitemap_added += 1
        warnings.append(f"{sm_note}（其中 {sitemap_added} 个已加入本次抓取队列）。")
        _emit("list", 0, max_pages,
              f"站点地图提供 {sitemap_added} 个页面，开始遍历")

    while queue and stats.pages_list < max_pages:
        if should_cancel and should_cancel():
            warnings.append("用户取消")
            break

        batch: list[tuple[str, int, str]] = []
        workers = max(1, concurrency)
        while (queue and len(batch) < workers
               and stats.pages_list + len(batch) < max_pages):
            batch.append(queue.pop(0))
        if not batch:
            break

        _emit("list", stats.pages_list, max_pages,
              f"正在抓取第 {stats.pages_list + 1}～{stats.pages_list + len(batch)} 页")
        fresults = fetch_many(
            [b[0] for b in batch],
            allow_render=allow_render,
            concurrency=concurrency,
            render_scroll=2,
            on_progress=lambda stage, d, t: _emit(
                "list", stats.pages_list + d, max_pages, f"抓取中（{stage}）{d}/{t}"),
            should_cancel=should_cancel,
        )

        for (u, depth, from_page), fr in zip(batch, fresults):
            stats.pages_list += 1
            if fr.method.startswith("render") and "+render-failed" not in fr.method:
                stats.render_used += 1
            elif fr.ok:
                stats.http_ok += 1

            if not fr.ok:
                pages_info.append({
                    "page": stats.pages_list, "url": u,
                    "method": fr.method or "failed", "html_bytes": 0,
                    "media": 0, "links": 0, "elapsed": round(fr.elapsed, 1),
                    "visible_text": 0, "depth": depth, "from": from_page,
                    "error": fr.error,
                })
                continue

            base = fr.final_url or u
            visited.add(_site_key(base))     # 重定向后的真实地址也标记，避免换个写法又抓一次
            got = extract_from_html(fr.html, base, base)
            for jt in fr.json_texts:
                got.extend(extract_from_json_text(jt, base, base, source="json"))
            for g in got:
                g.extra["site_page"] = base
            all_media.extend(got)
            if stats.pages_list == 1:
                first_report = _page_self_report(fr.html)
                first_page_thin = (fr.visible_text_len < 400) or (not got)

            # 同域链接入队（按相关性分排序，让内容页先被访问）
            fresh: list[tuple[str, int, str]] = []
            if depth < max_depth:
                links = extract_page_links(fr.html, "", base,
                                           same_host_only=False)
                for l in links:
                    lu = l["url"]
                    # 嵌入播放器（YouTube/B站等 iframe）就是「确定的视频页」：
                    # 不入遍历队列，直接变成结果条目 —— 下载走 yt-dlp
                    # （与「TikTok 博主页」条目同一机制）。要视频时全部收下。
                    if "video" in (want_kinds or ()) and l.get("source") == "html:iframe" \
                            and l.get("score", 0) >= 99.0:
                        all_media.append(MediaItem(
                            url=lu, kind="video", source="嵌入视频",
                            title=l.get("title") or "", thumbnail=l.get("thumb") or "",
                            referer=base, origin_page=base,
                            extra={"is_page": True, "ext": "", "variants": [],
                                   "uploader": "", "site": "embed", "duration_string": ""},
                        ))
                        continue
                    key = _site_key(lu)
                    if key in visited or _is_site_skippable(lu):
                        continue
                    if same_host and not _same_host(lu):
                        continue
                    visited.add(key)
                    fresh.append((lu, depth + 1, base, l.get("score", 0)))
                fresh.sort(key=lambda x: -x[3])
                queue.extend((a, b, c) for a, b, c, _s in fresh)

            pages_info.append({
                "page": stats.pages_list, "url": base, "method": fr.method,
                "html_bytes": len(fr.html), "media": len(got),
                "links": len(fresh), "elapsed": round(fr.elapsed, 1),
                "visible_text": fr.visible_text_len, "depth": depth,
                "from": from_page,
            })
            # 与 crawl_list 同一判据：可见文本少、或一条媒体都没抓到，都去查验证墙
            if fr.method.startswith("render") and (fr.visible_text_len < 100
                                                  or not got):
                wall = _detect_wall(fr.html)
                warnings.append(
                    f"{base} 抓取被拦截：{wall}" if wall
                    else f"{base} 渲染后正文仍为空，可能是反爬拦截页或需要登录。")
            elif not got:
                # 没撞验证墙但一条都没有 → 把页面自己的话摆出来，便于区分
                # 「页面/账号不存在 / 需要登录」与「工具没抓到」
                _t, _txt = _page_self_report(fr.html)
                warnings.append(
                    f"{base} 没有识别到媒体"
                    + (f"；页面标题《{_t}》" if _t else "")
                    + (f"；页面自述「{_txt}」" if _txt else "")
                    + "。")
        _emit("list", stats.pages_list, max_pages,
              f"已抓 {stats.pages_list} 页，累计 {len(all_media)} 个媒体")

    if queue and stats.pages_list >= max_pages:
        warnings.append(
            f"已达到页数上限（{max_pages} 页），本次共发现 {len(visited)} 个页面地址，"
            f"队列中还有 {len(queue)} 个未抓取。调高「抓取页数」可继续把剩下的抓完。")
    if not all_media:
        warnings.append(
            "整站未提取到任何媒体。若站点是 JS 渲染的，请把「浏览器渲染」打开"
            "（默认已开启，逐页自适应）。")

    # ---------- 汇总（与 crawl_list 同一套净化流程）----------
    _emit("merge", 0, 0, "正在汇总去重")
    merged = dedupe(all_media)
    merged = enrich_with_originals(merged)
    merged = dedupe(merged)

    before_variants = len(merged)
    merged = collapse_variants(merged)
    # 同视频多档位同样要收敛（与 crawl_list 保持一致）
    merged = collapse_video_variants(merged)
    variants_collapsed = before_variants - len(merged)

    before_deco = len(merged)
    merged = [m for m in merged if not _is_decoration(m.url)]
    deco_removed = before_deco - len(merged)
    if deco_removed:
        warnings.append(
            f"已过滤 {deco_removed} 个站点装饰资源（横幅 / 图标 / 埋点像素）。")

    before_kinds = len(merged)
    empty_explained = False
    if want_kinds:
        merged = [m for m in merged if m.kind in want_kinds]
    if before_kinds and not merged:
        want_label = "、".join({"image": "图片", "video": "视频", "stream": "流媒体"}
                               .get(k, k) for k in want_kinds)
        warnings.append(
            f"整站抓到了 {before_kinds} 个媒体，但没有一个属于当前要的类型"
            f"（{want_label}）—— 即这个站点里本来就没有这类内容。")
        empty_explained = True
    if originals_only:
        kept = [m for m in merged if not m.is_thumb]
        if kept:
            merged = kept

    kind_rank = {"video": 0, "stream": 1, "image": 2, "unknown": 3}

    def _sort_key(m: MediaItem):
        size = guess_size_from_url(m.url)
        area = (size[0] * size[1]) if size else 0
        return (kind_rank.get(m.kind, 9), m.is_thumb, -area, m.url)

    merged.sort(key=_sort_key)

    for m in merged:
        if not m.referer:
            m.referer = m.extra.get("site_page") or m.origin_page or start_url

    # 一条结果都没有时，把输入页的原文摆出来（与 crawl_list 同一处理）——
    # 让用户能区分「站点侧原因」与「抓取侧问题」。
    if (not merged and not empty_explained and first_page_thin
            and (first_report[0] or first_report[1])):
        t_, txt_ = first_report
        warnings.append(
            "最终没有拿到任何结果。输入页的原文如下，请对照判断："
            + (f"标题《{t_}》；" if t_ else "")
            + (f"正文「{txt_}」。" if txt_ else "")
            + "若原文说明「页面不存在/无法访问」或「需要登录」，那是站点侧原因。"
        )

    stats.elapsed = time.time() - t0
    return {
        "items": [m.to_dict() for m in merged],
        "summary": summarize(merged),
        "pages": pages_info,
        "detail_pages": {"total": 0, "ok": 0, "failed": 0, "urls": []},
        "stats": {
            "list_pages": stats.pages_list,
            "detail_pages": 0,
            "http_ok": stats.http_ok,
            "render_used": stats.render_used,
            "variants_collapsed": variants_collapsed,
            "elapsed": round(stats.elapsed, 1),
            "queued": len(queue),
            "discovered": len(visited),
            "sitemap_seeds": sitemap_added,
        },
        "warnings": warnings,
        "list_url": start_url,
        "renderer": renderer_status(),
        "mode": "site",
    }
