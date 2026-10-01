# -*- coding: utf-8 -*-
"""全网视频搜索：多来源聚合。

为什么是「多来源」而不是单打一：
  yt-dlp 的搜索前缀只有 `ytsearch` / `bilisearch`，原生只能搜 YouTube 与 Bilibili。
  要做到「全网」，只能借助搜索引擎的视频频道 —— 它们索引了各站视频页且**不限站点**，
  因此不需要为任何站点写专用规则（守住项目红线）。

但**只靠一个来源是不可靠的**。2026-09-21 实测：Bing 视频频道对多数关键词会走
「精简/推荐」路径，只返回 1 条与关键词毫无关系的内容（搜「科比」返回
"Create Interactive Materials with Gemini Notebook"），表现为「整个功能没法用」。
因此这里改成「多来源 + 逐源相关性门控 + 合并去重」：

  来源 1  DuckDuckGo 视频接口（主源）—— 实测同一关键词重复 3 次，
          60/60、58/58、58/58 条标题含关键词，稳定；每条都有标题/时长/缩略图/来源站。
  来源 2  Bing 视频频道（补充）—— 必须同时带 `qft=`（空值）与 `form=QBLH`，
          否则大量关键词只返回 1 条无关内容（见下方 search_bing_videos 的注释）。

两个来源**各自独立做相关性门控**：某一源整批与关键词无关时只丢弃该源，
不影响另一源，这样单一来源退化不会让整个功能失效。
"""
from __future__ import annotations

import html as _html
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlencode, urlparse

import requests

from . import relevance
from .config import HEADERS, TIMEOUT

# ---------------------------------------------------------------- 通用

TRANSIENT_ERRORS = (
    requests.exceptions.SSLError,
    requests.exceptions.Timeout,
    requests.exceptions.ConnectionError,
    requests.exceptions.ChunkedEncodingError,
)

# 搜索专用超时（比全局 TIMEOUT=25 紧）。
# 搜索要的是「快而可用」：某一个来源慢死时，宁可放弃它让另一个来源顶上，
# 也不该让用户盯着转圈 30 秒。实测本机代理会偶发卡住（同一地址前一次 2s、
# 后一次 20s+），收紧超时能显著压低最坏耗时的长尾。
SEARCH_TIMEOUT = 15

# ------------------------------------------------- 站点分组（供「国内/国外/单站」源使用）
#
# 为什么不是「限定查询」：实测（2026-09-21）`site:douyin.com` / `site:vimeo.com`
# 在两个引擎的视频频道里都返回 **0 条** —— 视频垂直并不认 `site:` 操作符。
#
# 改成「先照常搜多个来源，再按站点域名挑选」。这样做的前提是成立的：
# 实测跨 6 个关键词累计，全网结果里 B站 87 条、抖音 60、腾讯 33、搜狐 19、
# 爱奇艺 11 —— 各站内容本来就在多来源结果里，只是混在一起。
#
# 这是「按站点挑选结果」，**不是**直接调用各站自己的搜索接口（那要逐站写规则、
# 还要对抗各家风控，且违反本项目「不写站点专用规则」的红线）。界面上会如实说明。
DOMESTIC_HOSTS = (
    "bilibili.com", "douyin.com", "iesdouyin.com", "kuaishou.com", "qq.com",
    "youku.com", "iqiyi.com", "sohu.com", "weibo.com", "xiaohongshu.com",
    "ixigua.com", "acfun.cn", "mgtv.com", "163.com", "le.com", "pptv.com",
    "huya.com", "douyu.com", "zhihu.com", "baidu.com", "bilibili.tv",
)

# 成人站域名（作为一类国外视频站登记）。
#
# 为什么单独列：实测（2026-09-22）两个通用引擎**都拿不到**成人站内容 ——
# DDG 对成人内容彻底过滤（0 条），Bing 只返回 YouTube（30 条里 29 条），
# 关安全搜索也无效。所以这些站的内容只能靠主动检索（见 app/search_adult.py），
# 而它们理应是「国外主流」（以及「全网」）覆盖范围的一部分。
ADULT_HOSTS = (
    "pornhub.com", "eporner.com", "xvideos.com", "xhamster.com",
    "spankbang.com", "xnxx.com", "redtube.com", "youporn.com",
    "hqporner.com", "txxx.com", "beeg.com", "porntrex.com", "tnaflix.com",
    "drtuber.com", "porn.com", "tube8.com", "motherless.com", "erome.com",
    "onlyfans.com", "brazzers.com",
)

FOREIGN_HOSTS = (
    "youtube.com", "youtu.be", "vimeo.com", "dailymotion.com", "tiktok.com",
    "twitter.com", "x.com", "facebook.com", "instagram.com", "twitch.tv",
    "rumble.com", "nicovideo.jp", "soundcloud.com", "reddit.com", "bitchute.com",
    "odysee.com", "bilibili.tv", "metacafe.com", "veoh.com", "break.com",
) + ADULT_HOSTS
# 单站源：平台名 -> 该平台的域名特征
SITE_HOSTS: dict[str, tuple[str, ...]] = {
    "douyin": ("douyin.com", "iesdouyin.com"),
    "qq": ("qq.com",),
    "youku": ("youku.com",),
    "iqiyi": ("iqiyi.com",),
    "sohu": ("sohu.com",),
    "kuaishou": ("kuaishou.com",),
    "weibo": ("weibo.com",),
    "xiaohongshu": ("xiaohongshu.com",),
    "vimeo": ("vimeo.com",),
    "dailymotion": ("dailymotion.com",),
    "tiktok": ("tiktok.com",),
    "twitter": ("twitter.com", "x.com"),
    "instagram": ("instagram.com",),
    "facebook": ("facebook.com",),
}
# 站点常用中文名 / 通用叫法。
#
# 用途：单站检索时把站点名拼进关键词（「科比 抖音」），这是**纯关键词技巧** ——
# 不依赖任何站点接口，也不解析任何站点页面，与 SITE_HOSTS 一样只是「站点怎么称呼」
# 的元信息表。
#
# 为什么需要它（2026-09-21 实测，关键词「科比」，只取 DDG 单引擎）：
#   | 站点      | 直接搜该关键词的通用结果里属于该站的条数 | 拼上站点名后 |
#   |-----------|------------------------------------------|--------------|
#   | 抖音      | 0                                        | **56**       |
#   | 腾讯视频  | 0                                        | 56           |
#   | TikTok    | 0                                        | 49           |
#   | Instagram | 0                                        | 40           |
#   | 爱奇艺    | 0                                        | 8            |
# 即：通用检索里某站的条目几乎为 0，拼上站点名才检索得到。
SITE_WORDS: dict[str, str] = {
    "douyin": "抖音",
    "qq": "腾讯视频",
    "youku": "优酷",
    "iqiyi": "爱奇艺",
    "sohu": "搜狐视频",
    "kuaishou": "快手",
    "weibo": "微博",
    "xiaohongshu": "小红书",
    "vimeo": "Vimeo",
    "dailymotion": "Dailymotion",
    "tiktok": "TikTok",
    "twitter": "Twitter",
    "instagram": "Instagram",
    "facebook": "Facebook",
    "youtube": "YouTube",
    "bilibili": "哔哩哔哩",
}

# 已知「公开网页索引里几乎检索不到内容」的站点。
#
# 实测（2026-09-21，两个引擎 × 两种通用手段 all = 0 条）：优酷、快手、小红书、
# Vimeo、X、Facebook。这类站的内容主要在自己的 App / 登录墙内，搜索引擎索引不到。
# 用途：用户选中这些源时**提前如实告知**，而不是给一个空列表让人以为工具坏了。
# 这不是永久判定 —— 一旦某天检索得到，正常返回结果，此处只用于「解释为什么空」。
THIN_INDEX_SITES: dict[str, str] = {
    "youku": "优酷",
    "kuaishou": "快手",
    "xiaohongshu": "小红书",
    "vimeo": "Vimeo",
    "twitter": "X（Twitter）",
    "facebook": "Facebook",
}

SITE_LABELS = {
    "web": "全网（多来源）",
    "domestic": "国内主流（B站/抖音/腾讯/优酷/爱奇艺/搜狐…）",
    "foreign": "国外主流（YouTube/Vimeo/Dailymotion/TikTok/X…）",
    "youtube": "YouTube",
    "bilibili": "Bilibili",
    "douyin": "抖音",
    "qq": "腾讯视频",
    "youku": "优酷",
    "iqiyi": "爱奇艺",
    "sohu": "搜狐视频",
    "kuaishou": "快手",
    "weibo": "微博",
    "xiaohongshu": "小红书",
    "vimeo": "Vimeo",
    "dailymotion": "Dailymotion",
    "tiktok": "TikTok",
    "twitter": "X（Twitter）",
    "instagram": "Instagram",
    "facebook": "Facebook",
    "adult": "成人站点（Pornhub / Eporner）",
}


def host_matches(host: str, patterns: tuple[str, ...]) -> bool:
    """域名是否命中某一组特征（按域名后缀匹配，避免 `notyoutube.com` 误命中）。"""
    h = (host or "").lower()
    if h.startswith("www."):
        h = h[4:]
    return any(h == p or h.endswith("." + p) for p in patterns)


def host_group(host: str) -> str:
    """域名属于哪一组：domestic / foreign / other。"""
    if host_matches(host, DOMESTIC_HOSTS):
        return "domestic"
    if host_matches(host, FOREIGN_HOSTS):
        return "foreign"
    return "other"


def filter_items_by_hosts(items: list[dict], patterns: tuple[str, ...] | None) -> list[dict]:
    """按站点特征挑选结果（patterns 为 None 时原样返回）。"""
    if not patterns:
        return list(items or [])
    out = []
    for it in items or []:
        host = _site_from_url(str((it or {}).get("webpage_url") or ""))
        if host_matches(host, patterns):
            out.append(it)
    return out


# 拦截特征
_CHALLENGE = ("captcha", "unusual traffic", "verify you are human",
              "人机验证", "安全验证", "request was banned")


def _get_with_retry(url: str, *, headers: dict | None = None, params: dict | None = None,
                    timeout: int = SEARCH_TIMEOUT, tries: int = 2,
                    session: requests.Session | None = None):
    """带重试的 GET，且**代理与直连互为兜底**。

    两个真实问题逼出来的设计（2026-09-21 实测）：
      1. 本机 env 里设了 `HTTP_PROXY/HTTPS_PROXY=http://127.0.0.1:11794`，
         `requests` 默认读它，且它会偶发 `ProxyError / ConnectionReset`
         —— 浏览器验收时就出现过「B 站搜索整条失败：请求失败: ProxyError」。
      2. 但**不是所有站点都该走代理**：国内站点直连又快又稳，
         而海外站点（Bing / DuckDuckGo）在直连下可能不通。

    所以这里不再「一条路走到黑」：先按调用方给的那条路重试，仍失败就换另一条路
    （走代理 ⇄ 不走代理）再试一次。两条都试过才算真失败 ——
    一次代理抖动不该让用户看到「搜索失败」。
    """
    if session is None:
        session = requests.Session()
        session.headers.update(dict(HEADERS))

    preferred_direct = not getattr(session, "trust_env", True)
    other = requests.Session()
    other.headers.update(session.headers)
    other.trust_env = not preferred_direct       # 与首选相反的那条路

    last: Exception | None = None
    for idx, sess in enumerate((session, other)):
        attempts = tries if idx == 0 else 1
        for attempt in range(1, attempts + 1):
            try:
                return sess.get(url, headers=headers, params=params, timeout=timeout)
            except TRANSIENT_ERRORS as e:
                last = e
                if attempt < attempts:
                    time.sleep(0.6 * attempt)
        if idx == 0:
            time.sleep(0.3)     # 换路径前略等，避免立刻撞上同一个抖动
    raise last if last else RuntimeError("请求失败")


def _clean_title(s: str) -> str:
    """去标签、反转义、压缩空白。

    搜索结果里的标题常带高亮标记（B 站返回 `<em class="keyword">科比</em>`），
    直接展示会把标签文本露出来。
    """
    s = re.sub(r"<[^>]{1,80}>", "", s or "")
    s = _html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()[:200]


def _parse_duration(text: str) -> int | None:
    """把 "0:11" / "1:02:03" / "49:4" 转成秒数。"""
    if not text:
        return None
    try:
        nums = [int(x) for x in str(text).strip().split(":")]
    except ValueError:
        return None
    if len(nums) == 2:
        return nums[0] * 60 + nums[1]
    if len(nums) == 3:
        return nums[0] * 3600 + nums[1] * 60 + nums[2]
    return None


def _format_duration(seconds: int | None) -> str:
    if not seconds or seconds < 0:
        return ""
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _site_from_url(url: str) -> str:
    """从视频页 URL 取来源站点（域名是每条结果都必然有的，比页面里的标记稳定）。"""
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def dedupe_key(url: str) -> str:
    """跨来源去重用的 URL 规范化键。

    同一部片在两个来源里可能分别是 `http`/`https`、带不带 `www.`、带不带末尾斜杠，
    直接按字符串比会漏掉重复。这里只做**语义无关**的规范化，不动查询串
    （查询串常常是内容 ID 本身，去掉反而会把不同视频并成一条）。
    """
    u = (url or "").strip()
    if not u:
        return ""
    u = re.sub(r"^https?://", "", u, flags=re.I)
    u = re.sub(r"^www\.", "", u, flags=re.I)
    u = u.rstrip("/")
    return u.lower()


def merge_items(*groups: list[dict], limit: int = 20) -> list[dict]:
    """合并多个来源的结果：按规范化 URL 去重，再按相关性排序。

    排序用「命中关键词 token 的个数」，同分时**保持来源轮流出现的顺序**
    （见 _interleave），避免某一个来源把名额全部占满。
    """
    seen: set[str] = set()
    merged: list[dict] = []
    for group in groups:
        for it in group or []:
            k = dedupe_key(it.get("webpage_url") or "")
            if not k or k in seen:
                continue
            seen.add(k)
            merged.append(it)
    return merged[:max(1, limit)]


def _interleave(*groups: list[list[dict]]) -> list[dict]:
    """把多个来源的结果轮流拼接（a1,b1,a2,b2…）。"""
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
    return out


def rank_by_relevance(items: list[dict], keyword: str) -> list[dict]:
    """按「命中关键词 token 的个数」稳定排序（不删任何条目）。

    复用 `app/relevance.py` 的 token 提取（英文按词、中文按 2-gram），
    保证与图片搜索的判据一致。关键词没有可用 token 时原样返回。
    """
    tokens = relevance.query_tokens(keyword)
    if not tokens or not items:
        return items

    def score(it: dict) -> int:
        blob = relevance.item_blob(it)
        return -sum(1 for t in tokens if t in blob)

    return sorted(items, key=score)


def batch_is_irrelevant(items: list[dict], keyword: str) -> bool:
    """整批是否与关键词无关（用于**逐来源**门控）。

    判据与 `app/relevance.py` 完全一致：样本 ≥8 且命中率 ≤0.12 才算无关。
    样本太少时不下结论 —— 否则「1 条结果、恰好不含关键词」会被误判成整批无关
    （用户实测到过这种误报：返回 1 条却提示「0/1 条标题含关键词」）。
    """
    kinds = [dict(it, url=it.get("webpage_url") or "") for it in (items or [])]
    _, report = relevance.audit(kinds, keyword)
    return bool(report.get("blocked"))


def _phrase_of(keyword: str) -> str:
    return re.sub(r"\s+", " ", (keyword or "").strip().lower())


def is_strong_hit(item: dict, tokens: list[str], phrase: str) -> bool:
    """条目与关键词「强相关」吗？

    弱相关（只命中一个词）在多词查询下几乎必然是噪声 —— 实测（2026-09-21）
    B 站自己的搜索对 `led string lights` 会按「string」匹配，返回
    「c++ string」「python中string模块」；搜 `product demo` 会返回
    「git_jenkins构建企业生产环境CI/CD」。这类结果 title 里确实含关键词的某个词，
    用「命中任一词即相关」判会全部放过，用户看到的就是「搜出来的东西不对」。

    但**这条严格规则只适用于英文多词查询**：
      - 英文单词之间是独立语义，只中一个词（`string`）几乎必然是跑偏；
      - 中文是按 2-gram 切的（「风景 航拍」→ 风景 / 航拍），一个词就足以定主题，
        要求「两个都中」会把「大美新疆航拍」这种完全对的结果误判成不相关
        （这个误报我在 2026-09-21 的复验里踩到过）。
    因此：有 ≥2 个**英文** token 时才要求命中 2 个，其余情况命中 1 个即算强相关。
    """
    if not tokens:
        return True
    blob = relevance.item_blob(item)
    if phrase and len(phrase) >= 2 and phrase in blob:
        return True
    latin = [t for t in tokens if t.isascii()]
    need = 2 if len(latin) >= 2 else 1
    return sum(1 for t in tokens if t in blob) >= need


def count_strong_hits(items: list[dict], keyword: str) -> int:
    """强相关条目数（用于「要不要换一个更有把握的来源」的决策，不做删除）。"""
    tokens = relevance.query_tokens(keyword)
    phrase = _phrase_of(keyword)
    return sum(1 for it in (items or []) if is_strong_hit(it, tokens, phrase))


def _finalize(items: list[dict], count: int) -> list[dict]:
    for i, it in enumerate(items[:count]):
        it["index"] = i
    return items[:count]


# ------------------------------------------------------- 来源 1：DuckDuckGo

_DDG_HOST = "https://duckduckgo.com/"
_VQD_RE = re.compile(r'vqd=["\']?([\w-]{5,})')


def search_ddg_videos(keyword: str, count: int = 20,
                      timeout: int = SEARCH_TIMEOUT) -> tuple[list[dict], str | None]:
    """DuckDuckGo 视频接口。

    流程是两段：先请求结果页拿 `vqd` 令牌，再用它请求 `v.js` 拿 JSON。
    实测（2026-09-21）同一关键词重复 3 次结果完全一致（60/60、58/58 条标题含关键词），
    是四个来源里最稳的一个，因此作为全网搜索的主源。

    返回的每条都有：标题、时长、缩略图、发布者、原始页面地址（content）。
    """
    keyword = (keyword or "").strip()
    if not keyword:
        return [], "请输入关键词"

    session = requests.Session()
    session.headers.update(dict(HEADERS))
    session.headers["Accept-Language"] = "zh-CN,zh;q=0.9,en;q=0.8"
    try:
        r = _get_with_retry(_DDG_HOST, session=session,
                            params={"q": keyword, "iax": "videos", "ia": "videos"},
                            timeout=timeout)
        m = _VQD_RE.search(r.text or "")
        if not m:
            return [], "DuckDuckGo 未返回检索令牌（可能触发了反爬）"
        r2 = _get_with_retry("https://duckduckgo.com/v.js", session=session,
                             params={"l": "us-en", "o": "json", "q": keyword,
                                     "vqd": m.group(1), "f": ",,,"},
                             headers={"Referer": "https://duckduckgo.com/"},
                             timeout=timeout)
        data = r2.json()
    except TRANSIENT_ERRORS as e:
        return [], f"请求失败: {type(e).__name__}（已重试 3 次）"
    except (ValueError, requests.exceptions.RequestException) as e:
        return [], f"请求失败: {type(e).__name__}"

    results = (data or {}).get("results") or []
    items: list[dict] = []
    for x in results[: max(1, count) * 2]:
        if not isinstance(x, dict):
            continue
        url = (x.get("content") or "").strip()
        if not url.startswith("http"):
            continue
        imgs = x.get("images") if isinstance(x.get("images"), dict) else {}
        thumb = ""
        for k in ("large", "medium", "small"):
            if imgs.get(k):
                thumb = imgs[k]
                break
        dur_text = str(x.get("duration") or "")
        dur = _parse_duration(dur_text)
        items.append({
            "id": x.get("image_token") or "",
            "title": _clean_title(x.get("title") or x.get("description") or ""),
            "duration": dur,
            "duration_string": dur_text or _format_duration(dur),
            "webpage_url": url,
            "thumbnail": thumb,
            "uploader": _clean_title(x.get("uploader") or ""),
            # publisher 是「来源站点」域名字符串（qq.com / youtube.com …）
            "source_site": (x.get("publisher") or "").strip() or _site_from_url(url),
            "view_count": None,
            "source": "ddg",
        })
    if not items:
        return [], "DuckDuckGo 未返回视频结果"
    return items, None


# ------------------------------------------------------------- 来源 2：Bing

# 结果块分界
_BLOCK_SPLIT = re.compile(r'<div id="mc_vtvc_video_\d+"')
# 主数据：mmeta 属性里的裸 JSON
_MMETA_RE = re.compile(r'mmeta="(\{.+?\})"')
# 标题：优先用 title 属性，退化到 <strong> 内容
_TITLE_RE = re.compile(r'class="mc_vtvc_title[^"]*"[^>]*\btitle="([^"]{1,300})"')
_TITLE_ALT_RE = re.compile(r'<strong>([^<]{1,300})</strong>')
# 时长，如 0:11 / 12:34 / 1:02:03
_DUR_RE = re.compile(r'class="mc_bc_rc items">(\d{1,2}:\d{2}(?::\d{2})?)<')
# 来源站点 + 上传者
_META_ROW_RE = re.compile(
    r'<span>([^<]{1,40})</span>\s*<span class="mc_vtvc_meta_row_channel">([^<]{1,120})</span>')


def parse_bing_video_html(html_text: str, limit: int = 20,
                          keyword: str = "") -> list[dict]:
    """从 Bing 视频搜索结果页解析条目（结构与 yt-dlp 那条路径保持一致）。"""
    if not html_text:
        return []
    doc = _html.unescape(html_text)          # 必须先整体反转义，否则正则拿不到裸 JSON
    blocks = _BLOCK_SPLIT.split(doc)
    raw: list[dict] = []
    seen: set[str] = set()

    for block in blocks[1:]:                 # 第 0 段是首个结果之前的内容
        mm = _MMETA_RE.search(block)
        if not mm:
            continue
        try:
            meta = json.loads(mm.group(1))
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(meta, dict):
            continue

        url = (meta.get("murl") or meta.get("pgurl") or "").strip()
        if not url.startswith("http") or url in seen:
            continue
        seen.add(url)

        tm = _TITLE_RE.search(block) or _TITLE_ALT_RE.search(block)
        dm = _DUR_RE.search(block)
        mr = _META_ROW_RE.search(block)
        dur_text = dm.group(1) if dm else ""
        title = _clean_title(tm.group(1) if tm else "")
        site = _clean_title(mr.group(1)) if mr else ""

        raw.append({
            "id": str(meta.get("mid") or ""),
            "title": title,
            "duration": _parse_duration(dur_text),
            "duration_string": dur_text,
            "webpage_url": url,
            "thumbnail": meta.get("turl") or "",
            "uploader": _clean_title(mr.group(2)) if mr else "",
            # 页面里的来源标记只有部分模板有，域名则每条都有 —— 以域名兜底
            "source_site": site or _site_from_url(url),
            "view_count": None,
            "source": "bing",
        })

    return raw[:max(1, limit)]


def search_bing_videos(keyword: str, count: int = 20,
                       timeout: int = SEARCH_TIMEOUT) -> tuple[list[dict], str | None]:
    """Bing 视频频道。返回 (条目列表, 错误信息)。

    ⚠️ 两个参数缺一不可，否则**大量关键词会只返回 1 条无关内容**：

    - `qft=`（空值）：不带时 Bing 走「精简」路径。用**空值**而不是某个 filterui
      取值，是为了不引入筛选语义（`duration-long` 之类会悄悄过滤掉短片）。
    - `form=QBLH`：这是「从视频搜索框发起」的标识。2026-09-21 实测（这是本轮
      「关键词搜视频完全没法用」的直接根因）：

        | 参数              | 科比            | 冲田杏梨         | 张梓琳          |
        |-------------------|-----------------|------------------|-----------------|
        | qft 空 无 form    | 220KB 1 块 0命中 | 143KB 1 块 0命中 | 141KB 1 块 0命中 |
        | qft 空 + form=QBLH| 781KB 51块 79命中| 473KB 30块 24命中| 575KB 31块 30命中|

      且 form=QBLH 重复 4 次结果一致（781/783/782/759KB），是**稳定**而非偶然。
      之前的排查只加了 qft，对「詹姆斯 / 周杰伦 / 风景 航拍」这类词恰好有效，
      所以误以为修好了 —— 实际上多数词仍在退化路径上。
    """
    keyword = (keyword or "").strip()
    if not keyword:
        return [], "请输入关键词"

    url = "https://www.bing.com/videos/search?" + urlencode(
        {"q": keyword, "qft": "", "form": "QBLH"})
    headers = dict(HEADERS)
    headers["Accept-Language"] = "zh-CN,zh;q=0.9,en;q=0.8"
    headers["Referer"] = "https://www.bing.com/"

    try:
        r = _get_with_retry(url, headers=headers, timeout=timeout)
    except TRANSIENT_ERRORS as e:
        return [], f"请求失败: {type(e).__name__}（已重试 3 次）"
    except requests.exceptions.RequestException as e:
        return [], f"请求失败: {type(e).__name__}"

    if r.status_code >= 400:
        return [], f"Bing 返回 HTTP {r.status_code}"

    html_text = r.text or ""
    low = html_text[:20000].lower()
    if any(m in low for m in _CHALLENGE):
        return [], "Bing 触发了人机验证，请稍后重试"

    # 多解析一些，交给上层和另一个来源一起排序后再截断
    items = parse_bing_video_html(html_text, limit=max(1, count) * 2, keyword=keyword)
    if not items:
        return [], (f"Bing 未返回视频结果（页面 {len(html_text) // 1024}KB）。"
                    "可能是该关键词没有视频，或页面结构变化。")
    return items, None


# ------------------------------------------- 来源 3：Dailymotion 官方公开 API

_DAILYMOTION_API = "https://api.dailymotion.com/videos"
_DAILYMOTION_FIELDS = ("id,title,url,duration,thumbnail_360_url,"
                       "owner.screenname,description")


def search_dailymotion_videos(keyword: str, count: int = 20,
                              timeout: int = SEARCH_TIMEOUT
                              ) -> tuple[list[dict], str | None]:
    """Dailymotion 官方公开 REST API（**无需密钥**）。

    为什么单独接它：两个搜索引擎对 Dailymotion 的索引实测都是 **0 条**
    （`site:dailymotion.com 科比` 与「科比 Dailymotion」全空），于是「国外主流」
    与「Dailymotion」单站永远搜不出东西。而该站有公开检索接口，实测
    HTTP 200 / 20 条 / 7KB，`total` 上千。

    这是该站自己的公开检索入口，与 B 站 `wbi/search` 性质相同 ——
    是「来源」，不是「对页面的解析规则」。
    """
    keyword = (keyword or "").strip()
    if not keyword:
        return [], "请输入关键词"
    try:
        r = _get_with_retry(
            _DAILYMOTION_API,
            headers={**HEADERS, "Accept": "application/json"},
            params={"search": keyword, "limit": min(100, max(1, count)),
                    "fields": _DAILYMOTION_FIELDS, "sort": "relevance"},
            timeout=timeout,
        )
    except TRANSIENT_ERRORS as e:
        return [], f"请求失败: {type(e).__name__}（已重试 3 次）"
    except requests.exceptions.RequestException as e:
        return [], f"请求失败: {type(e).__name__}"

    if r.status_code >= 400:
        return [], f"Dailymotion 返回 HTTP {r.status_code}"
    try:
        data = r.json()
    except ValueError:
        return [], "Dailymotion 返回的不是 JSON"

    items: list[dict] = []
    for x in (data.get("list") or []):
        if not isinstance(x, dict):
            continue
        url = (x.get("url") or "").strip()
        if not url.startswith("http"):
            continue
        dur = x.get("duration")
        try:
            dur = int(dur) if dur is not None else None
        except (TypeError, ValueError):
            dur = None
        owner = x.get("owner") if isinstance(x.get("owner"), dict) else {}
        items.append({
            "id": x.get("id") or "",
            "title": _clean_title(x.get("title") or ""),
            "duration": dur,
            "duration_string": _format_duration(dur),
            "webpage_url": url,
            "thumbnail": x.get("thumbnail_360_url") or "",
            "uploader": _clean_title(owner.get("screenname") or ""),
            "source_site": "Dailymotion",
            "view_count": None,
            "source": "dailymotion",
        })
    if not items:
        return [], "Dailymotion 没有该关键词的结果"
    return items, None


# --------------------------------------------------- 站点专向检索（单站源用）

def search_site_scoped(keyword: str, patterns: tuple[str, ...],
                       site_word: str = "", count: int = 20,
                       timeout: int = SEARCH_TIMEOUT) -> tuple[list[dict], str | None, dict]:
    """针对**某个站点**做检索，两条通用手段并用。

    用户报「按站点挑选结果这个选项没意义了 —— 搜出来都很少」。根因是
    「单站」原本只是**从通用检索结果里筛域名**，而通用结果里某站的条目本来就
    极少（实测「科比」：抖音 0 条、腾讯 0 条、TikTok 0 条）。于是选项形同虚设。

    这里改成主动**按站点**去检索，用两条不依赖任何站点接口的通用手段：

    1. `site:<域名> <关键词>` —— 检索语法，站点限定最精准。
       实测（2026-09-21，关键词「科比」）：腾讯视频 **60/60**、搜狐视频 **60/60**、
       抖音 8/8、TikTok 3/3、爱奇艺 2/2。
       ⚠️ 只有 DuckDuckGo 认这个语法，Bing 会**静默忽略**它（实测
       `site:douyin.com 科比` 在 Bing 上返回的是 bilibili/qq 的普通结果），
       所以 site: 只发给 DDG。
    2. `<关键词> <站点常用名>` —— 纯关键词技巧。实测抖音 **56**、腾讯 56、
       TikTok 49、Instagram 40、爱奇艺 8。

    两条互补：site: 精准但覆盖窄，拼站点名量大。**并用**才够看。

    返回 (条目, 错误, 各手段的原始条数)。条目已按域名过滤（保险）并去重。
    """
    keyword = (keyword or "").strip()
    if not keyword or not patterns:
        return [], "缺少关键词或站点特征", {}

    fetch = max(1, count) * 2
    dom = patterns[0]
    # site: 单独设一个更紧的超时。实测它的耗时与「该站在索引里有多少内容」强相关：
    # 内容多的站 3~5s 就返回（腾讯 3.7s / 搜狐 4.7s），内容少的站会一直耗到超时
    # 甚至重试（抖音 20.2s 才拿到 8 条、优酷 18.9s 拿到 0 条）。
    # 而「关键词+站点名」通常 2s 就给出几十条，所以没必要为 site: 一直等。
    site_timeout = min(timeout, 9)

    jobs: list[tuple[str, object, tuple]] = [
        ("ddg_site", search_ddg_videos, (f"site:{dom} {keyword}", fetch, site_timeout)),
    ]
    if site_word:
        jobs.append(("ddg_word", search_ddg_videos, (f"{keyword} {site_word}", fetch, timeout)))
        jobs.append(("bing_word", search_bing_videos, (f"{keyword} {site_word}", fetch, timeout)))

    raw: dict[str, dict] = {}
    groups: list[list[dict]] = []
    errs: list[str] = []
    with ThreadPoolExecutor(max_workers=3) as pool:
        futs = {name: pool.submit(fn, *args) for name, fn, args in jobs}
        for name, fut in futs.items():
            try:
                items, err = fut.result()
            except Exception as e:  # noqa: BLE001
                raw[name] = {"count": 0, "error": f"{type(e).__name__}: {e}"}
                errs.append(f"{name}: {type(e).__name__}")
                continue
            items = items or []
            # 保险：确认结果确实属于该站点（site: 偶尔会带出同域的其它子站）
            got = filter_items_by_hosts(items, patterns)
            raw[name] = {"count": len(got), "raw": len(items), "error": err}
            if got:
                groups.append(got)

    if not groups:
        msg = "；".join(errs) if errs else None
        return [], msg, raw
    merged = merge_items(*groups, limit=10 ** 6)
    return merged, None, raw


# --------------------------------------------------------------- 聚合入口

def search_web_videos(keyword: str, count: int = 20,
                      timeout: int = SEARCH_TIMEOUT,
                      host_patterns: tuple[str, ...] | None = None,
                      pool_factor: int = 3) -> dict:
    """全网视频搜索：多来源聚合并做相关性门控。

    返回 {"items": [...], "sources": {...}, "error": str|None}。
    `sources` 记录每个来源的原始条数与是否被判为无关，便于前端/排查时说明
    「结果是怎么来的」，也让「某一源退化」这件事变得可观测而不是静默降级。

    host_patterns 非空时，最后只保留命中这些域名特征的结果（「国内/国外/单站」源
    就是这么实现的 —— 见文件上方 SITE_HOSTS 的说明）。此时**必须多抓一些原始结果**，
    否则筛完可能不够数；所以 pool_factor 允许上层调大。
    """
    keyword = (keyword or "").strip()
    if not keyword:
        return {"items": [], "sources": {}, "error": "请输入关键词"}

    fetch = max(1, count) * max(1, pool_factor)
    with ThreadPoolExecutor(max_workers=2) as pool:
        f_ddg = pool.submit(search_ddg_videos, keyword, fetch, timeout)
        f_bing = pool.submit(search_bing_videos, keyword, fetch, timeout)
        ddg_items, ddg_err = f_ddg.result()
        bing_items, bing_err = f_bing.result()

    sources: dict[str, dict] = {}
    kept: list[list[dict]] = []
    for name, items, err in (("ddg", ddg_items, ddg_err), ("bing", bing_items, bing_err)):
        info = {"count": len(items), "error": err, "dropped": False}
        # 逐源门控：只有「样本足够且整批无关」才丢弃该源，另一源不受影响。
        if items and batch_is_irrelevant(items, keyword):
            info["dropped"] = True
            info["reason"] = "unrelated"
            items = []
        sources[name] = info
        if items:
            kept.append(rank_by_relevance(items, keyword))

    merged = merge_items(*kept, limit=10 ** 6) if kept else []

    # 站点筛选（国内/国外/单站）：在**合并去重之后**做，避免两个来源的同一站结果
    # 各自被截断后再筛导致漏。
    before_host = len(merged)
    if host_patterns:
        merged = filter_items_by_hosts(merged, host_patterns)

    # 主源全空时才靠「加通用词重搜」兜底（改写词偏向所有视频站，不偏向某一家）
    rewritten = ""
    if not merged and not host_patterns:
        alt = f"{keyword} 视频"
        alt_items, alt_err = search_ddg_videos(alt, fetch, timeout)
        if alt_items and not batch_is_irrelevant(alt_items, keyword):
            merged = merge_items(rank_by_relevance(alt_items, keyword), limit=10 ** 6)
            rewritten = alt
            sources["ddg_rewrite"] = {"count": len(alt_items), "query": alt}
        else:
            sources["ddg_rewrite"] = {"count": len(alt_items), "error": alt_err,
                                      "dropped": True}

    if not merged:
        if host_patterns:
            # 选了具体站点却没有结果：如实说明，**不要**拿别的站结果充数
            # （用户明确说了「我只想在 X 上搜」）。
            return {
                "items": [], "sources": sources, "host_filtered": before_host,
                "error": (f"这次搜索的来源里没有该站点的视频"
                          f"（同关键词在其它站点共找到 {before_host} 条）。"
                          f"可以换关键词，或把视频源改成「全网」看看别站有没有。"),
            }
        errs = [v.get("error") for v in sources.values() if v.get("error")]
        msg = "；".join(str(e) for e in errs) or (
            "各来源都没有取到与关键词相关的视频，建议换个说法或加限定词再搜。")
        return {"items": [], "sources": sources, "error": msg}

    return {"items": _finalize(merged, max(1, count)), "sources": sources,
            "rewritten_query": rewritten, "error": None,
            "host_filtered": before_host if host_patterns else None}
