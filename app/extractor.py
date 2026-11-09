# -*- coding: utf-8 -*-
"""通用媒体提取器：从 HTML 与 JSON 双通道递归提取媒体地址，不依赖站点专用规则。

设计目标（用户要求「给什么网站就能搜什么网站」）：
  绝不为单个站点写死规则。所有提取都基于「地址形态 + 字段语义」的通用启发式。

实测得到的、必须处理的陷阱：
  1. **协议相对地址**：阿里接口返回 "//cloud.video.alibaba.com/x.mp4"，
     不带 https: 前缀。按 `https?://` 匹配的正则会全部漏掉
     （实测：全文正则匹配到 0 个 mp4，而递归遍历找到 2 个）。
  2. **JSON 字符串内嵌套转义 URL**：字段值是被转义的 JSON 字符串，
     需要先反转义再提取。
  3. **缩略图与原图分离**：列表页只有 300x300 缩略图，原图在详情页。
  4. **Base64 data URI**：不是可下载地址，必须排除。
"""
from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse, unquote

IMAGE_EXTS = {
    "jpg", "jpeg", "png", "gif", "webp", "bmp", "svg", "ico", "tif", "tiff",
    "avif", "heic", "heif", "jxl",
}
VIDEO_EXTS = {"mp4", "webm", "mkv", "mov", "avi", "flv", "wmv", "m4v",
              "mpg", "mpeg", "3gp", "ts", "m2ts"}
STREAM_EXTS = {"m3u8", "mpd"}
# 纯音频：本工具处理的是图片/视频/流媒体，音频（抖音的 music.play_url 等）不算媒体，
# 否则「字段名带 play」会把 BGM 的 mp3 也当成视频塞进结果里。
AUDIO_EXTS = {"mp3", "m4a", "aac", "wav", "ogg", "flac", "opus", "wma", "amr"}
# 明确不是媒体的资源后缀（用于媒体提取时排除）
SKIP_EXTS = {
    "js", "css", "json", "xml", "txt", "pdf", "zip", "rar",
    "exe", "dll", "woff", "woff2", "ttf", "otf", "eot", "map", "sh", "py",
    "yml", "yaml", "csv",
}
# 页面后缀：不是媒体，但正是「详情页链接」的典型形态，提取链接时必须保留。
# 曾把 html 混进 SKIP_EXTS，导致 60 个 productUrl 全被当成资源过滤掉。
PAGE_EXTS = {"html", "htm", "shtml", "xhtml", "php", "asp", "aspx", "jsp", "do", "action"}

# 字段名语义提示（通用，不含任何具体站点名）
VIDEO_KEY_HINTS = ("video", "mp4", "stream", "media", "play", "clip", "movie", "hls")
IMAGE_KEY_HINTS = ("image", "img", "pic", "photo", "thumb", "poster", "cover",
                   "avatar", "banner", "icon", "logo", "snapshot", "frame")
LINK_KEY_HINTS = ("url", "href", "link", "src", "path", "detail", "page", "target")
# 这些字段虽然名字像，但装的不是媒体
NON_MEDIA_KEY_HINTS = (
    "eurl", "clickurl", "impression", "imps", "trace", "track", "spm",
    "utm", "chat", "token", "css", "script", "stylesheet", "font",
)

# 「逗号 + 数字 + 非词字符」= CDN 签名令牌的时间戳后缀，属于地址的一部分。
# 实测教训（xvideos 2026-09）：直链形如
#   .../video_360p.mp4?secure=<hash>==,1790663430
# 逗号 + 过期时间戳是完整令牌的一半；旧正则把逗号一律当分隔符截断，
# 令牌缺了尾巴 → CDN77 直接回 403（症状：抓取正常、批量下载 27/27 全 403）。
# 保留防粘连：逗号后跟字母（如「url1,url2」列表）仍然切断。
_TOKEN_COMMA = r",(?=\d{1,12}(?:\W|$))"

# 通用地址匹配：同时覆盖 https:// http:// 与协议相对 //host/path
# 关键点：必须允许 // 开头，否则会漏掉大量真实媒体地址
_URL_RE = re.compile(
    r"""(?xi)
    (?P<url>
        (?:https?:)?//                       # 绝对或协议相对
        (?:[^\s"'<>\\)\]},]|""" + _TOKEN_COMMA + r""")+?   # 主机与路径（保守字符集）
        \.(?:jpg|jpeg|png|gif|webp|bmp|svg|avif|heic|jxl|tif|tiff
            |mp4|webm|mkv|mov|avi|flv|m4v|mpg|mpeg|3gp|ts|m2ts|m3u8|mpd)
        (?:\?(?:[^\s"'<>\\)\]},]|""" + _TOKEN_COMMA + r""")*)?   # 查询串
    )
    """
)

# 无扩展名的媒体地址（很多 CDN 用 /video/123 这类路径），靠字段名语义判定
_BARE_URL_RE = re.compile(
    r"""(?xi)(?:https?:)?//(?:[^\s"'<>\\)\]},]|""" + _TOKEN_COMMA + r"""){8,400}""")

# HTML 属性
_HTML_MEDIA_ATTRS = ("src", "data-src", "data-original", "data-lazy-src", "data-echo",
                     "data-url", "data-image", "data-img", "data-actualsrc",
                     "data-hires", "data-highres", "data-video", "data-mp4",
                     "poster", "href", "content")
_HTML_MEDIA_ATTR_SET = frozenset(_HTML_MEDIA_ATTRS)

# 一次扫描取出「媒体属性 = 值」，名字用 alternation 限定。
#
# 为什么改成单次扫描（性能）：原实现是「每个属性名各跑一遍全文正则」，
# 16 个属性 = 把整份 HTML 扫 16 遍。实测一个 1.6MB 的详情页光这一步就要
# 1.1 秒；批量抓取 30 个详情页 = 30 多秒纯 CPU，页面越大越糟（渲染后的 DOM
# 常达 3~5MB，成本再翻两三倍），是「批量抓取很慢」的主要来源。
# 单次扫描后同样的页面降到约 0.13 秒（实测 9.8x）。
#
# 为什么名字必须是 alternation、而不是「抓任意 名字=值 再筛」：
# 后者会把 `var html = '<img src="https://a/1.jpg">'` 这种**整段值**当成
# 一次匹配吃掉，字符串里的 src 再也扫不到（实测漏抓，且漏的是真实媒体）。
# 按属性名限定起点，行为与原逐属性扫描逐条一致（见 test_attr_scan.py）。
# 长的名字排前面，避免 data-image 被 data-img 抢先。
_MEDIA_ATTR_ALT = "|".join(
    re.escape(a) for a in sorted(_HTML_MEDIA_ATTRS, key=len, reverse=True))
_ATTR_VALUE_RE = re.compile(
    rf"""(?<![-\w])(?P<name>{_MEDIA_ATTR_ALT})\s*=\s*"""
    r"""(?:"([^"]*)"|'([^']*)'|([^\s"'=<>`]+))""",
    re.I)


def _canon_media_attr(name: str) -> str | None:
    r"""属性名 → 规范媒体属性名；不是媒体载体则返回 None。

    规范名必须与原实现「按属性名逐条扫描」的记录完全一致，否则下游会退化
    （batch_crawler 用 `source.endswith("attr:poster")` 认封面，
    is_preview_video 也看 source 串）：大写写法 SRC= 要记成 'src'。
    命名空间写法（xlink:href）由正则直接从 href 处命中，天然就是 'href'。
    """
    n = (name or "").lower()
    return n if n in _HTML_MEDIA_ATTR_SET else None


@dataclass
class MediaItem:
    url: str
    kind: str                       # image | video | stream | page | unknown
    source: str = ""                # html | json | attr:...
    key_path: str = ""              # JSON 键路径，便于调试
    title: str = ""
    referer: str = ""
    origin_page: str = ""           # 来自哪个页面（列表页 or 详情页）
    width: int | None = None
    height: int | None = None
    is_thumb: bool = False          # 是否疑似缩略图（尺寸后缀）
    thumbnail: str = ""             # 封面/海报（视频用；图片一般留空）
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "url": self.url,
            "type": self.kind,
            "source": self.source,
            "key_path": self.key_path,
            "title": self.title,
            "referer": self.referer,
            "origin_page": self.origin_page,
            "width": self.width,
            "height": self.height,
            "is_thumb": self.is_thumb,
            "thumbnail": self.thumbnail,
            "ext": self.extra.get("ext", ""),
            # 同一媒体的其它档位地址（归并时被合并掉的那些）。
            # 前端会把它带给下载接口：首选地址下不了时自动换用。
            "variants": self.extra.get("variants", []),
            # 「这条 url 是一个**视频页地址**，不是媒体直链」——前端据此改用
            # yt-dlp 下载（见 app.js 的 downloadCrawlSelected）。来自
            # batch_crawler 的 yt-dlp 列表展开。
            "is_page": bool(self.extra.get("is_page")),
            # 单条解析（挑清晰度）应该打开的页面地址：
            #   1) 视频页条目直接用自己；
            #   2) 直链条目用抓取时记录的详情页（detail_page），这是**所有站点**
            #      通用的回退——批量结果里的直链几乎都来自某个详情页下钻。
            # 没有详情页（媒体直接挂在列表页）时留空，前端不会再猜一个列表页去解析。
            "probe_url": (
                self.extra.get("probe_url", "")
                or (self.url if self.extra.get("is_page") else "")
                or (self.extra.get("detail_page", "")
                    if self.kind in ("video", "stream") else "")
            ),
            "duration_string": self.extra.get("duration_string", ""),
            "uploader": self.extra.get("uploader", ""),
            "site": self.extra.get("site", ""),
            # 流条目（HLS/DASH master）探测出的全部档位，如 [1080, 720, 480, 360, 250]。
            # 前端据此在卡片和清晰度下拉里如实标注；下载时 ffmpeg 自动选最高档。
            "stream_heights": self.extra.get("stream_heights", []),
        }


# ---------- 地址规范化与分类 ----------

def normalize(url: str, base: str = "") -> str | None:
    """规范化地址：补协议、解析协议相对地址、去锚点、解转义。"""
    if not url:
        return None
    u = url.strip().strip('"').strip("'").strip()
    # 去掉 HTML 实体与 JSON 转义残留
    u = u.replace("\\/", "/").replace("&amp;", "&")
    u = u.replace("\\u002F", "/").replace("\\u002f", "/")
    if not u or u.startswith(("data:", "blob:", "javascript:", "about:", "mailto:")):
        return None
    if u.startswith("//"):
        u = "https:" + u
    elif not u.startswith(("http://", "https://")):
        if not base:
            return None
        u = urljoin(base, u)
    try:
        p = urlparse(u)
    except ValueError:
        return None
    if p.scheme not in ("http", "https") or not p.netloc:
        return None
    return u


# CDN 常见的「变换后缀」：真实扩展名在这些后缀之前。
#   字节系：.../xxx.jpeg~tplv-tokbwntzau-image.image
#   小红书：.../1040g3k831s46qf8j68a05os1qo87rvtp845dho0!nd_dft_wgth_webp_3
#   阿里 OSS：?x-oss-process=image/resize,w_300（在查询串里，本就不影响 path）
# 不剥掉它们就取不到扩展名，图片会被按「字段名语义」误判成视频
# （实测抖音主页：64 条结果里绝大部分是这种被误判的图片）。
_TRANSFORM_SEP = "~!"  # noqa: S105  （只是分隔符集合，不是密码）


def ext_of(url: str) -> str:
    """取地址扩展名（小写、不含点）。会先剥掉 CDN 的变换后缀。"""
    path = urlparse(url).path
    last = path.rsplit("/", 1)[-1]
    # 只保留第一个变换分隔符之前的部分
    for sep in _TRANSFORM_SEP:
        if sep in last:
            last = last.split(sep, 1)[0]
    if "." not in last:
        return ""
    e = last.rsplit(".", 1)[-1].lower()
    # 扩展名里残留的其它分隔符/参数（如 .jpg%3F、.png;）一并切掉
    e = re.split(r"[^a-z0-9]", e)[0]
    return e if len(e) <= 5 else ""


def classify(url: str, key_path: str = "") -> str:
    """判定媒体类型：优先看扩展名，其次看字段名语义。"""
    e = ext_of(url)
    if e in IMAGE_EXTS:
        return "image"
    if e in VIDEO_EXTS:
        return "video"
    if e in STREAM_EXTS:
        return "stream"
    if e in SKIP_EXTS or e in AUDIO_EXTS:
        return "skip"
    kl = (key_path or "").lower()
    if any(h in kl for h in VIDEO_KEY_HINTS):
        return "video"
    if any(h in kl for h in IMAGE_KEY_HINTS):
        return "image"
    return "unknown"


def is_thumb(url: str) -> bool:
    """判断是否疑似缩略图：CDN 常在地址里带尺寸后缀或 /thumb/ 目录。

    通用形态（不绑定具体站点）：
      xxx.jpg_300x300.jpg / xxx.jpg?x-oss-process=image/resize,w_300
      xxx-300x300.jpg / xxx_w300.jpg / thumb_xxx / small_xxx
      /tmb/123/1.jpg  /thumbs/x.jpg  （目录段级标记）

    实测教训（avjoy.me）：站点缩略图是
      media-cdn2.avjoy.me/media/avjoytmb/tmb/12055/20.jpg
    文件名是纯数字 `20.jpg`，路径里的标记是 `tmb`（thumb 的常见缩写）。
    旧实现只认「关键词 + 紧邻分隔符」的文件名形态，导致整页 62 张缩略图
    全部漏判 —— 缩略图占比算出 3%，下钻门控因此认为「列表页已含完整媒体」
    而跳过详情页，最终只交付了 1 个站点装饰资源。
    """
    u = url.lower()
    if re.search(r"[_\-.](\d{2,4})x(\d{2,4})\.", u):
        return True
    if re.search(r"[/_-](thumb|thumbnail|small|mini|preview|square)[_.\-]", u):
        return True
    if re.search(r"resize,w_\d{2,3}|/w/\d{2,3}|_\d{2,3}x\d{2,3}q\d", u):
        return True

    # 目录段级标记：把整条路径切成段看，而不是只看最后一段文件名。
    # 覆盖 tmb / thumbs / preview / poster / snapshot 这类「存放缩略图的目录」。
    # 实测意义：/media/avjoytmb/tmb/12055/20.jpg 里，`tmb` 是目录段而非文件名。
    try:
        segs = [s for s in urlparse(u).path.split("/") if s]
    except ValueError:
        segs = []
    DIR_THUMB = {"tmb", "thumbs", "thumb", "thumbnail", "thumbnails",
                 "preview", "previews", "poster", "posters",
                 "snapshot", "snapshots", "small", "mini"}
    for s in segs[:-1]:                       # 不含最后一段（那是文件名）
        # avjoytmb 这种「前缀+标记」合并写法也要认
        if s in DIR_THUMB or s.endswith(("tmb", "thumb", "thumbnail")):
            return True

    # 文件名以 small/thumb 开头
    name = urlparse(u).path.rsplit("/", 1)[-1]
    if name.startswith(("small", "thumb", "mini", "tmb")):
        return True
    return False


def is_preview_video(url: str, source: str = "") -> bool:
    """判断视频地址是否是「预览片段」而非完整视频。

    为什么要单独一条：很多站点（tube 站尤其普遍）在列表页放两类"假视频"——
      a) 悬停预览：鼠标移上去才播放的小动画，放在 data-mp4 / data-mediabook
         这类「延迟加载/预览」语义的 data-* 属性里；
      b) 低码率试看流：文件名以 {高度}P_{码率}K_ 开头（如 180P_225K_xxx.webm），
         高度一般 ≤ 200，是"试看"级别的预览流。
    它们会被 classify 判成 video，从而让「列表页已有视频，无需下钻」的门控
    被骗过（pornhub 实测：84 个媒体里有 84 个这类预览、0 个完整视频），
    也让最终结果里混满 "180P" 垃圾条目。

    判定全部基于「地址形态 + 属性语义」，不绑定任何站点：
      - data-mp4 / data-mediabook 属性里的视频 = 悬停预览（属性语义）
      - 文件名 {高度}P_{码率}K_ 且高度 < 240 = 低码率试看流（命名形态）
      - 路径含 gifs 目录段 = 动图预览（gif 本义就是循环动画预览）
    """
    if source and source.lower().startswith("attr:data-mp4"):
        return True
    if source and source.lower().startswith("attr:data-mediabook"):
        return True
    u = (url or "").lower()
    try:
        path = urlparse(u).path
    except ValueError:
        return False
    m = re.search(r"(\d{2,3})P_\d{1,4}K_", path.rsplit("/", 1)[-1], re.I)
    if m and int(m.group(1)) < 240:
        return True
    segs = [s for s in path.split("/") if s]
    if "gifs" in segs[:-1]:
        return True
    # 媒体扩展名后还挂着路径段的是 CDN 派生资源（如
    # xxx.mp4/plain/rs:fit:640:360/... 是从视频帧现裁的缩略图），不是视频本体
    if re.search(r"\.(mp4|mov|mkv|webm|m4v)/[^/]+/", path):
        return True

    # 主机名带「缩略图 / 预览 / 试看」语义，却挂着视频扩展名 —— 一般是列表页的
    # 悬停预览片段，不是完整视频。
    # 实测 xvideos：thumb-cdn77.xvideos-cdn.com/<uuid>/0/preview.mp4 约 130KB，
    # 而完整视频在 video-cdn 域上、体量大两个数量级；这类片段混进结果会让
    # 「批量下载」下到一堆几秒钟的预览。
    try:
        host = urlparse(u).hostname or ""
    except ValueError:
        host = ""
    if re.search(r"(^|[.\-])(thumb|thumbs|preview|sample|trailer|teaser)", host):
        return True

    # 直播拉流地址（pull 型边缘节点 + /stage/stream- 路径）不是点播文件：
    # 实测抖音主页会把 `pull-flv-l11.douyincdn.com/stage/stream-<id>.flv` 之类的
    # 直播边缘地址写进页面，被当成"视频"混进结果（实测 14 条结果全是同一个流地址）。
    # 这类地址拿来下载只会拿到一段无意义的实时流，或者根本无法下载。
    # 判据用「主机名 + 路径形态」两段同时命中，避免误伤正常的 CDN 点播地址。
    if re.search(r"(^|[.\-])(pull-(flv|hls|rtmp))", host) and \
            re.search(r"/(stage|live)/stream", path):
        return True

    return False


def strip_thumb_suffix(url: str) -> str | None:
    """尝试从缩略图地址还原原图地址（通用规则，失败返回 None）。

    常见形态：
      a.jpg_300x300.jpg      -> a.jpg
      a.jpg?x-oss-process=…  -> a.jpg
      a-300x300.jpg          -> a.jpg
      a_w300.jpg             -> a.jpg
    """
    u = url
    # 去掉图片处理查询参数（常见于对象存储 CDN）
    u = re.sub(r"[?&](x-oss-process|imageMogr2|imageView2|w|width|h|height)="
               r"[^&]*", "", u, flags=re.I)
    u = u.rstrip("?&")
    # 去掉 xxx.jpg_300x300.jpg 这种「原图名 + 尺寸 + 扩展名」尾巴
    m = re.match(r"^(.*?\.(?:jpg|jpeg|png|webp|gif|bmp|avif))[_\-]\d+x\d+.*$", u, flags=re.I)
    if m:
        return m.group(1)
    # 去掉 -300x300 / _w300 这类尺寸后缀
    m = re.match(r"^(.*?)[_\-]\d{2,4}x\d{2,4}(?=\.[a-z]{2,5}$)", u, flags=re.I)
    if m:
        return m.group(1) + u[m.end():]
    m = re.match(r"^(.*?)[_\-]w\d{2,4}(?=\.[a-z]{2,5}$)", u, flags=re.I)
    if m:
        return m.group(1) + u[m.end():]
    # 数字在 w 前面这一类：v2-xxx_1440w.jpg / _720w.jpg（知乎正文图就是这种）。
    # 同一张图会同时出现多个档位，不归一化就会在结果里出现两三份。
    m = re.match(r"^(.*?)[_\-]\d{2,4}w(?=\.[a-z]{2,5}$)", u, flags=re.I)
    if m:
        return m.group(1) + u[m.end():]
    # 质量档后缀：_r(raw) / _b(big) / _hd / _qhd / _xl / _xxl。
    # 知乎同一张图常以 v2-xxx.jpg 与 v2-xxx_r.jpg 两种写法同时出现，
    # 只差这一个字母，内容完全一样。
    m = re.match(r"^(.*?)[_\-](?:r|b|hd|qhd|xl|xxl)(?=\.[a-z]{2,5}$)", u, flags=re.I)
    if m:
        return m.group(1) + u[m.end():]
    return u if u != url else None


# ---------- 尺寸推断（用于识别装饰图、选原图版本、排序）----------

# CDN 把尺寸写进文件名的常见写法：
#   ...-2-tps-600-600.png      （阿里：短横分隔）
#   ...-300x300.jpg            （WordPress 系）
#   ..._640.jpg                （pixabay 系，单边尺寸）
# 必须要求两边都 ≥20：否则文件名里的日期（photo-2023-04-15.jpg）
# 会被读成 2023×4 —— 宽高比 505，直接把正常图片误判成横幅过滤掉。
_DIM_RE = re.compile(
    r"[_\-](\d{2,5})[_\-x](\d{2,5})"
    r"(?=\.(?:jpe?g|png|gif|webp|bmp|avif|heic|heif|jxl|tiff?)$)",
    re.I)


def guess_size_from_url(url: str) -> tuple[int, int] | None:
    """从地址里推断图片尺寸；推断不出返回 None。

    实测样本（阿里商品列表页）：
      ...O1CN01ByGrWI1G1RjnWEc7W_!!6000000000562-0-tps-1518-360.jpg → 1518×360（横幅）
      ...O1CN01qOsGmF2ALhXk0QWRf_!!6000000008187-2-tps-297-40.png  → 297×40（图标条）
    这类地址里同时含超长数字 ID，靠范围约束才不会把 ID 当尺寸。
    """
    path = urlparse((url or "").split("?")[0]).path
    name = path.rsplit("/", 1)[-1]
    for m in _DIM_RE.finditer(name):
        w, h = int(m.group(1)), int(m.group(2))
        if not (2 <= w <= 20000 and 2 <= h <= 20000):
            continue
        # 排除日期型文件名：photo-2023-04-15.jpg 里的 "04-15" 会被读成 4×15，
        # 从而被当成小图标过滤掉 —— 实测这类误判会直接吞掉正常图片。
        # 特征：尺寸对前面紧挨着 "-四位年份"（如 photo-2023-04-15）。
        if re.search(r"[_\-]\d{4}$", name[:m.start()]):
            continue
        return w, h
    return None


# 常见图片尺寸：只有命中它才认定 `_数字` 是「尺寸版本」而不是序号。
# 必要性：有些站点用 _001/_002 区分不同图片，无差别归一会把它们错误合并。
_COMMON_IMAGE_SIZES = {
    100, 120, 150, 160, 180, 200, 220, 240, 250, 300, 320, 340, 350, 360, 375,
    400, 420, 450, 480, 500, 512, 540, 600, 620, 640, 660, 680, 700, 720, 750,
    760, 768, 800, 810, 840, 900, 960, 1000, 1024, 1080, 1100, 1200, 1280,
    1350, 1440, 1500, 1536, 1600, 1620, 1800, 1920, 2000, 2048, 2160, 2400,
    2500, 2560, 2880, 3000, 3200, 3840, 4000, 4096, 5120,
}


def variant_key(url: str) -> str:
    """把「同一张图的不同尺寸版本」归一到同一个 key。

    实测必要性（pixabay）：同一张图会在列表里出现两次：
      cdn.pixabay.com/photo/2013/04/04/12/34/mountains-100367_1280.jpg
      cdn.pixabay.com/photo/2013/04/04/12/34/mountains-100367_640.jpg
    不去重的话结果里一半是重复版本。
    """
    p = urlparse(url or "")
    directory, _, name = p.path.rpartition("/")

    # 「原图名 + 尺寸尾巴 + 扩展名」：x.jpg_300x300.jpg → x.jpg
    m = re.match(r"^(.*?\.(?:jpe?g|png|gif|webp|bmp|avif))[_\-]\d+x\d+.*$", name, re.I)
    if m:
        name = m.group(1)

    stem, dot, ext = name.rpartition(".")
    s = stem
    s = re.sub(r"[_\-]\d{2,5}x\d{2,5}$", "", s)                        # -300x300
    s = re.sub(r"[_\-](?:w|h|s|q)\d{2,5}$", "", s, flags=re.I)         # _w300
    s = re.sub(r"[_\-](?:thumb|thumbnail|small|medium|large|preview|mini)$",
               "", s, flags=re.I)
    m2 = re.match(r"^(.*)_(\d{2,5})$", s)                              # _1280 / _640
    if m2 and int(m2.group(2)) in _COMMON_IMAGE_SIZES:
        s = m2.group(1)

    key = f"{directory}/{s}.{ext}" if dot else f"{directory}/{s}"
    return key.lower()


def _version_size_hint(url: str) -> int:
    """估算一个「版本大小」分值，用于在多个尺寸版本里挑最大的那个。

    两种尺寸写法都要认：
      WxH（-300x300.jpg）→ 直接相乘
      单边（pixabay 的 _1280.jpg）→ 按正方形近似
    返回 0 表示「地址里没有尺寸信息」（这种最可能是原图，排序时优先）。
    """
    size = guess_size_from_url(url)
    if size:
        return size[0] * size[1]
    name = urlparse((url or "").split("?")[0]).path.rsplit("/", 1)[-1]
    stem = name.rsplit(".", 1)[0]
    m = re.search(r"[_\-](\d{2,5})$", stem)
    if m and int(m.group(1)) in _COMMON_IMAGE_SIZES:
        v = int(m.group(1))
        return v * v
    return 0


def collapse_variants(items: list[MediaItem]) -> list[MediaItem]:
    """合并同一张图的多个尺寸版本，只保留最像原图的那一条。

    保留策略：无尺寸标记 > 尺寸大 > 无查询串 > 非缩略图。
    被合并掉的地址记进 extra['variants']，需要时仍可追溯。
    """
    groups: dict[str, list[MediaItem]] = {}
    out: list[MediaItem] = []
    for it in items:
        if it.kind != "image":
            out.append(it)
            continue
        groups.setdefault(variant_key(it.url), []).append(it)

    def _rank(m: MediaItem):
        hint = _version_size_hint(m.url)
        primary = 0 if hint == 0 else 1        # 无尺寸标记的最可能是原图
        has_query = 1 if "?" in m.url else 0
        return (primary, -hint, has_query, m.is_thumb, m.url)

    for key, group in groups.items():
        if len(group) == 1:
            out.append(group[0])
            continue
        group.sort(key=_rank)
        best = group[0]
        best.extra["variant_count"] = len(group)
        best.extra["variants"] = [g.url for g in group[1:]][:10]
        out.append(best)
    return out


def _path_quality_hint(path: str) -> int:
    """从路径段里猜清晰度（只在同一部片的多份副本之间排序用）。

    同名副本常被分到 /HD/ /SD/ /hevc/HD/ 这类目录，文件名里没有任何分辨率信息。
    猜错不影响正确性：它只决定「保留哪一份」，被替换的地址仍记在 variants 里。
    """
    p = (path or "").lower()
    m = re.search(r"/(\d{3,4})p/", p)
    if m:
        return int(m.group(1))
    for word, h in (("uhd", 2160), ("4k", 2160), ("fhd", 1080), ("hd", 1080),
                    ("sd", 480), ("ld", 360)):
        if re.search(rf"/{word}/", p):
            return h
    return 0


def _video_variant_info(url: str) -> tuple[str | None, bool, int]:
    """解析视频地址的「档位信息」。

    两种通用命名形态都要认（不绑定站点）：
      - 前缀式：240P_1000K_61466355.mp4 / original_61466355.mp4，
        去掉档位前缀后是同一个长数字媒体 ID。
      - 后缀式：video_240p.mp4 / clip-1080p.mp4，清晰度挂在文件名末尾。
    返回 (归并键=媒体 ID, 是否原始档, 档位高度)；非档位命名返回 (None, False, 0)。

    为什么键不含目录段：同一部片的不同档位常被分到不同 CDN 目录
    （实测 pornhub：240P 在 /c6251/videos/...，original 在 /c6371/videos/...），
    按目录归并会把同一部片拆成两组。纯长数字 ID 在页面尺度内不会碰撞。
    """
    try:
        path = urlparse(url).path
    except ValueError:
        return None, False, 0
    name = path.rsplit("/", 1)[-1]
    stem = name.rsplit(".", 1)[0]
    is_orig = bool(re.match(r"^original_", stem, re.I))
    height = 0
    # 档位前缀不一定在文件名开头：实测 pornhub 有
    #   .../447706221/250507_2231_240P_1000K_447706221.mp4
    # 记录时间串还挂在前面。只在开头匹配会导致解析失败 → 锚点失效 →
    # 该页几十个相关视频全部泄进结果（实测泄漏 10 条）。
    hm = re.search(r"(\d{2,4})P_(\d{1,4})K_", stem, re.I)
    if hm:
        height = int(hm.group(1))
        stem = stem[hm.end():]
    elif is_orig:
        stem = stem[len("original_"):]
    m = re.match(r"^(\d{6,})(?:_[A-Za-z0-9]{1,12})?$", stem)
    if m:
        return m.group(1), is_orig, height

    # 后缀式档位命名：video_240p.mp4 / clip-1080p.mp4。
    # 与上面的「前缀式」相反，清晰度挂在文件名末尾，且两个档位同目录。
    # 实测 xvideos：同一部片的 /<uuid>/0/video_240p.mp4 与 /<uuid>/0/video_360p.mp4
    # 只有清晰度后缀不同、UUID 目录相同，旧判据（只认前缀式）完全认不出来，
    # 结果 27 部片在结果里变成 55 条、每部都重复两遍。
    # 归并键：路径里的 UUID 段优先（天然就是媒体 ID）；没有 UUID 时退回
    # 「目录段 + 去掉档位后缀的文件名」。
    sm = re.search(r"[_\-](\d{3,4})p$", stem, re.I)
    if sm:
        h2 = int(sm.group(1))
        if 100 <= h2 <= 4320:      # 只认合理分辨率，避免误伤 1000p 这类奇怪命名
            um = re.search(r"/([0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12})(?:/|$)",
                           path, re.I)
            if um:
                return "uuid:" + um.group(1).lower(), False, h2
            base = path.rsplit("/", 1)[0]
            return "sfx:" + (base + "/" + stem[:sm.start()]).lower(), False, h2

    # 第三种通用形态：<媒体ID>-<轨道号>-<档位号>.mp4（无扩展名外的任何站点标记）
    # 实测 B站番剧页：同一集在十几个 CDN 镜像上各挂一份，
    #   upgcxcode/11/83/1401558311/1401558311-1-448.mp4   ← estgcos
    #   upgcxcode/11/83/1401558311/1401558311-1-416.mp4   ← estgoss
    # 第一段是媒体 ID、第二段是音轨/分P、第三段是档位号。不认这个形态时，
    # 同一集会被列成 16 条「不同视频」，用户以为抓到了一整季。
    # 归并键不含 host：镜像域名正是要合并掉的那一维。
    tm = re.match(r"^(\d{6,})-(\d{1,3})-(\d{2,4})$", stem)
    if tm:
        q = int(tm.group(3))
        if 100 <= q <= 4320:       # 只认像分辨率的档位号，避免吃掉分片序号
            return "bid:" + tm.group(1) + "-" + tm.group(2), False, q

    # 第四种：UUID 挂在文件名开头，档位写在目录里
    #   /HD/f7c761b1-...-zufu-v4_f2_t1_2l0M8F2j.mp4
    #   /hevc/HD/同一串 UUID-..._f3_t3_pfWAlDoY.mp4
    # 同一部片在 HD / SD / hevc 三个目录各一份，码率与编码后缀也不同，
    # 只有开头那段 UUID 稳定。不认它时，一个问题页会把同一部片列成 14 条。
    um2 = re.match(r"^([0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12})", stem, re.I)
    if um2:
        return "uuid:" + um2.group(1).lower(), False, _path_quality_hint(path)
    return None, False, 0


def collapse_video_variants(items: list[MediaItem]) -> list[MediaItem]:
    """把同一部视频的多个清晰度档位收敛成一条，保留最可能是原始/最高清的档。

    为什么要做：详情页播放器数据里同一部片会有 240P/360P/720P/1080P/original
    多个档位地址，不合并的话批量结果里同一部片出现 4~6 条，用户还得自己挑。
    只对「档位命名」成立的地址归并（键 = 目录段 + 媒体 ID），其余保持原样，
    因此不会误伤普通站点里恰好同目录的独立视频。
    保留优先级：站点声明的播放地址 > 原始档(original_) > 档位高度高 > 无时效令牌 > mp4。
    被合并的地址记进 extra['variants']，需要时仍可追溯。

    为什么「站点声明的播放地址」要排第一（实测踩坑）：
    不少站点把同一部片同时挂在两类 CDN 上，名字看起来越漂亮的那条反而下不了。
    实测 pornhub：同一部片既有
      pix-fl.phncdn.com/c6371/videos/.../original_<id>.mp4     ← 图片 CDN，无令牌
      ev.phncdn.com/videos/.../240P_1000K_<id>.mp4?...&hash=... ← og:video 声明的地址
    前者请求返回 470 No Token（Varnish），下载必然失败；后者是 200 / 88MB 真视频。
    旧排序「original_ 优先」恰好选中前者 —— 用户看到的就是「抓取完的视频无法下载」。
    站点用 OpenGraph / schema.org 声明的那条才是它真正用来播放的地址，属于通用信号
    （对应 batch_crawler 里 per 页打上的 extra['declared']）。
    """
    groups: dict[tuple, list] = {}
    out: list[MediaItem] = []
    for it in items:
        if it.kind not in ("video", "stream"):
            out.append(it)
            continue
        key, is_orig, height = _video_variant_info(it.url)
        if key is None:
            out.append(it)
            continue
        groups.setdefault(key, []).append((it, is_orig, height))

    def _rank(t):
        it, is_orig, height = t
        u = it.url.lower()
        has_token = 1 if re.search(r"[?&](validfrom|validto|hdnea)=", u) else 0
        is_mp4 = 1 if u.rsplit(".", 1)[-1].startswith("mp4") else 0
        declared = 1 if it.extra.get("declared") else 0
        return (-declared, -is_orig, -height, has_token, -is_mp4, it.url)

    for key, group in groups.items():
        if len(group) == 1:
            out.append(group[0][0])
            continue
        group.sort(key=_rank)
        best = group[0][0]
        best.extra["variant_count"] = len(group)
        best.extra["variants"] = [g[0].url for g in group[1:]][:10]
        for it, _, _ in group[1:]:
            if _title_quality(it.title) > _title_quality(best.title):
                best.title = it.title
            if not best.thumbnail and it.thumbnail:
                best.thumbnail = it.thumbnail
            if it.extra.get("detail_page") and not best.extra.get("detail_page"):
                best.extra["detail_page"] = it.extra["detail_page"]
        out.append(best)
    return out


def _looks_media_url(url: str) -> bool:
    """无扩展名地址是否像媒体（用于配合字段名语义判断）。"""
    u = url.lower()
    if any(f".{e}" in u for e in (IMAGE_EXTS | VIDEO_EXTS | STREAM_EXTS)):
        return True
    # CDN 常见路径特征
    return bool(re.search(r"/(image|img|photo|pic|video|media|kf|imgextra)/", u))


def _is_tracking(url: str) -> bool:
    """排除埋点/统计/广告/推广跳转地址（通用特征，非站点专用）。

    实测教训：放宽「字段名有媒体语义即接受无扩展名地址」后，
    Pixabay 结果里混进了大量 canva.com/content-partner/?utm_medium=...
    这类推广跳转链接。真实媒体地址几乎不会带 utm_ 参数，
    因此把营销参数与跳转路径一并排除是安全且通用的。
    """
    u = url.lower()

    # 0) 埋点/统计/广告域名（按 host 判断，比子串匹配更准）
    #    此前该判断只写在 batch_crawler._is_decoration 里，
    #    而媒体提取走的是本函数，导致 hm.baidu.com/hm.gif 这类埋点漏网。
    try:
        host = (urlparse(u).hostname or "").lower()
    except ValueError:
        host = ""
    if host:
        TRACK_HOSTS = ("mmstat.com", "google-analytics.com", "googletagmanager.com",
                       "googlesyndication.com", "doubleclick.net", "hotjar.com",
                       "clarity.ms", "scorecardresearch.com", "quantserve.com",
                       "cnzz.com", "umeng.com", "hm.baidu.com", "facebook.com/tr")
        if any(host == h or host.endswith("." + h) for h in TRACK_HOSTS):
            return True
        # log./beacon./track./pixel./analytics./stat. 这类子域前缀
        first = host.split(".")[0]
        if first in ("log", "logs", "beacon", "track", "tracking", "pixel",
                     "analytics", "stat", "stats", "counter", "metrics"):
            return True

    # 1) 常见埋点/统计/广告路径特征
    marks = ("/cl_bw?", "adexpo", "impression", "/pixel", "google-analytics",
             "doubleclick", "beacon", "/track?", "collect?", "noscript",
             "1x1", "spacer", "blank.gif", "googlesyndication",
             "googletagmanager", "facebook.com/tr", "hotjar", "scorecardresearch")
    if any(m in u for m in marks):
        return True

    # 2) 营销追踪参数：真实媒体地址几乎不会带这些
    if re.search(r"[?&](utm_(source|medium|campaign|content|term)|gclid|fbclid|"
                 r"mc_cid|mc_eid|ref|referrer|affiliate|aff_id|partner_id|tag)=", u):
        return True

    # 3) 跳转/推广路径段
    if re.search(r"/(content-partner|partner|affiliate|redirect|goto|out|clk|"
                 r"click|jump|link-out|sponsored|ad|ads|banner)(/|\?|$)", u):
        return True

    return False


def _looks_like_marketing(url: str) -> bool:
    """无扩展名地址是否像营销/跳转页（而非媒体）。

    用于「字段名含媒体语义但地址无扩展名」这一宽松分支的二次把关：
    既保留 //cdn/play/99887766 这类合法 CDN 地址，
    又挡掉 canva.com/content-partner/?utm_... 这类推广链接。
    """
    if _is_tracking(url):
        return True
    try:
        p = urlparse(url)
    except ValueError:
        return True
    # 查询串很长且无媒体扩展名：**疑似**跳转链接
    if len(p.query) > 60 and ext_of(url) not in (IMAGE_EXTS | VIDEO_EXTS | STREAM_EXTS):
        # 但「路径本身就是媒体路径」时不算营销 —— 这类 CDN 地址的正常形态就是
        # 「超长签名查询串 + 无扩展名 + 路径含 /video/」：
        #   实测抖音 play_addr.url_list：
        #     https://v5-xxx.zjcdn.com/<hash>/<ts>/video/tos/cn/tos-cn-ve-15c000-ce/xxx/?a=6383&ch=4...
        # 旧判据只看「查询串 > 60 且无扩展名」，把这类地址全部当成营销链接挡掉，
        # 结果一个博主页 306 个 play_addr 全被丢弃（0 个视频）。
        if re.search(r"/(video|videos|img|image|images|photo|photos|media|audio|obj|"
                     r"stream|hls|dash|play|playback|tos)(/|$|\.)", p.path or "", re.I):
            return False
        return True
    return False


def is_decoration(url: str) -> bool:
    """判断是否站点装饰资源：埋点像素 / favicon / logo / 图标 / 占位图 / 横幅细条。

    为什么必须过滤：站点返回反爬壳页或空页时，页面里只剩 UI 资源。
    不过滤就会把 font_*.svg、14-14.png、-tps-1518-360.jpg 这类横幅和图标
    当成交付结果 —— 实测阿里商品搜索抓到的 178 条里大半都是这些，
    用户看到的会是一堆 logo 和分隔条，而不是商品图。

    判定顺序：埋点域名 → 文件名语义 → 装饰目录段 → URL 内嵌尺寸（最可靠的一路）。

    实测教训（avjoy.me）：站点 PWA 图标视频
      avjoy.me/images/favicons/site.webm
    被当成唯一「视频」交付，而该地址实际返回 404 / 0 字节 —— 用户看到的就是
    「只有一个打不开的视频」。旧实现只看 URL 最后一段文件名：`site.webm` 里
    既没有 logo/icon 等关键词，`.webm` 也不在 ico/svg 白名单，于是漏网。
    因此必须把**目录段**（/favicons/）纳入判定，它比文件名可靠得多。
    """
    u = (url or "").lower()
    if not u:
        return True
    if _is_tracking(u):
        return True

    try:
        path = urlparse(u).path
    except ValueError:
        path = ""
    name = path.rsplit("/", 1)[-1]

    if name.startswith(("favicon", "apple-touch-icon")):
        return True
    if re.search(r"(^|[_\-/.])(logo|icon|sprite|placeholder|blank|spacer|loading|"
                 r"avatar-default|no-image|noimage|dot|pixel|trans)([_\-/.]|\d|$)", name):
        return True

    # 装饰目录段：这些目录本身就是站点 UI 资源区，里面的东西一律不是内容。
    # 放在文件名判定之后、尺寸判定之前 —— 它命中率高且几乎不会误伤内容图。
    if re.search(r"/(favicons?|icons?|logos?|sprites?|placeholders?|branding|theme-assets)/",
                 path):
        return True
    # 站点级装饰媒体：加载动画 / 背景视频 / 占位视频，名字通常就叫 site/bg/loader
    if re.search(r"(^|[/_\-.])site\.(webm|mp4|gif)$", path):
        return True
    if re.search(r"(^|[_\-/.])(bg|background|loader|loading|preload|placeholder|"
                 r"watermark|spacer)[_\-.]\w+$", name):
        return True

    # 站点静态资源主机（如 lf3-static.bytednsdoc.com / static.cdninstagram.com）
    # 上的资源目录内容：加载动画、客户端推广片、播放器素材都属于站点 UI。
    #
    # 为什么加这条（2026-09-23 实测抖音博主主页）：页面自己的推广片
    #   lf3-static.bytednsdoc.com/obj/eden-cn/<hash>/鸣潮1.22.mp4
    # 与下载入口 douyin_pc_client.mp4 混进了结果顶部（按体积排序时排最前），
    # 用户第一眼看到的就是「不是自己主页内容」的视频。
    # 判据刻意收窄，避免误伤真媒体：
    #   - 主机首段**等于** static 或以 -static 结尾（douyinstatic.com 这类真媒体 CDN 不匹配）
    #   - 且路径落在资源目录（/obj/ /webapp/ /static/ /assets/ /rsrc /public/）
    try:
        _host = (urlparse(u).hostname or "").lower()
    except ValueError:
        _host = ""
    _first = _host.split(".")[0] if _host else ""
    if (_first == "static" or _first.endswith("-static")) and re.search(
            r"^/(obj|webapp|static|assets|rsrc|public|download|player)([./]|$)",
            path, re.I):
        return True

    # 站点自身的前端构建产物目录：里面的媒体是 UI 构件，不是页面内容。
    # 实测 tiktok 主页：抓到
    #   lf16-tiktok-web.tiktokcdn-us.com/obj/tiktok-web-tx/tiktok/webapp/main/
    #   webapp-desktop/playback1.mp4
    # 它是 webapp 的播放器资源，却被当成该页唯一「视频」交付。
    # 只收明确属于构建产物的目录名，不含 static/assets —— 那两类有站点会拿来放内容。
    if re.search(r"/(webapp|web-app|node_modules|bundle|bundles|chunks?|dist)/",
                 path):
        return True

    # URL 内嵌尺寸：比文件名关键词可靠得多，能认出「横幅」这种名字里没线索的图
    size = guess_size_from_url(u)
    if size:
        w, h = size
        # 阈值取 100 而不是 200：商品列表页的缩略图常见 150×150 / 200×200，
        # 那些是有用的内容，不能当图标过滤掉；真正的 UI 图标在 100 以下。
        if w <= 100 and h <= 100:
            return True                        # 小图标
        if max(w, h) / max(1, min(w, h)) >= 3.0:
            return True                        # 横幅 / 细长条（1518×360、297×40 均属此类）

    if ext_of(u) in ("ico", "svg"):
        return True
    return False


# ---------- 标题提取与择优 ----------

def html_unescape(text: str) -> str:
    """解码 HTML 实体（如 &gt; &amp; &raquo;）。"""
    try:
        import html as _h
        return _h.unescape(text or "")
    except Exception:
        return text or ""


def _clean_title(text: str) -> str:
    """把一段可能含标签/实体的文本清理成单行标题；无信息量时返回空串。

    为什么要判「无信息量」：实测 avjoy.me 的列表页里，同一个详情页有两个 <a> ——
    一个包缩略图（inner text 只有覆盖层上的时长，如 "HD\\t\\t…\\t10:00"），
    另一个包标题（`<span class="content-title">`）。若不加判断，
    取到的「标题」就是一串时长，前端渲染出来是空白 —— 用户看到的就是
    「视频没有名称」。因此纯时长/纯标点要判为无效，让调用方走属性回退。
    """
    t = re.sub(r"<[^>]+>", " ", text or "")
    t = html_unescape(t)
    t = re.sub(r"\s+", " ", t).strip()
    if not t:
        return ""
    # 站点未读角标会被拼进 document.title：「(9 封私信) 身材比例 - 知乎」。
    # 不剥掉的话，这个角标会跟着页面标题一起被套到每一条媒体上，
    # 用户看到的每条视频都叫「(9 封私信) …」（2026-11-08 知乎问题页实测）。
    t = re.sub(r"^[（(\[]\s*\d{1,3}\s*"
               r"(?:封私信|条私信|条消息|条新闻|条通知|条评论|个通知|条未读|"
               r"条新消息|未读消息|new messages?)\s*[)）\]]\s*", "", t, flags=re.I)
    if not t:
        return ""
    # 纯「HD/4K + 时长」或纯时长：没有信息量
    if re.fullmatch(r"(?:HD|SD|4K|FHD|UHD)?\s*[\d:：]{3,14}", t, re.I):
        return ""
    if re.fullmatch(r"[\s\-–—·•|/,、。.]+", t):
        return ""
    return t


def _title_quality(t: str) -> int:
    """给标题打分，用于「同一地址有多条记录」时择优保留。

    只要求「谁更像真标题」，不需要绝对准确：命中中日韩文字/多个拉丁词加高分，
    纯数字时长对象扣分。
    """
    t = (t or "").strip()
    if not t:
        return 0
    score = min(len(t), 120)
    if re.search(r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]", t):
        score += 40
    if len(re.findall(r"[A-Za-z]{3,}", t)) >= 2:
        score += 20
    if re.fullmatch(r"(?:HD|SD|4K|FHD)?\s*[\d:：\s]{3,14}", t, re.I):
        score -= 60
    return score


# 页面级标题：og:title > <title> > <h1>（通用，无站点专用规则）
_PAGE_TITLE_PATTERNS = (
    r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)',
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:title["\']',
    r'<meta[^>]+name=["\']twitter:title["\'][^>]+content=["\']([^"\']+)',
    r"<title[^>]*>(.*?)</title>",
    r"<h1[^>]*>(.*?)</h1>",
)


def page_title(html: str) -> str:
    """从详情页取一个可读标题。

    用于给「标题为空或标题是垃圾（如纯时长）」的媒体兜底。
    实测 avjoy.me 的 og:title / <title> / <h1> 都指向同一个真实标题。
    """
    if not html:
        return ""
    for pat in _PAGE_TITLE_PATTERNS:
        m = re.search(pat, html, re.S | re.I)
        if not m:
            continue
        t = _clean_title(m.group(1))
        if t:
            return t[:200]
    return ""


# ---------- HTML 通道提取 ----------

def extract_from_html(html: str, base_url: str = "", origin_page: str = "") -> list[MediaItem]:
    """从 HTML 文本提取媒体（含 srcset / 懒加载 / 内嵌 JSON / CSS 背景）。"""
    if not html:
        return []
    items: list[MediaItem] = []
    seen_local: set[str] = set()

    def add(raw: str, source: str, key_path: str = "", title: str = ""):
        u = normalize(raw, base_url)
        if not u or u in seen_local:
            return
        if _is_tracking(u):
            return
        kind = classify(u, key_path)
        if kind == "skip":
            return
        if kind == "unknown" and not _looks_media_url(u):
            return
        seen_local.add(u)
        th = is_thumb(u)
        if kind in ("video", "stream") and not th:
            # 悬停预览 / 低码率试看流也算「预览资源」，与缩略图同待遇
            th = is_preview_video(u, source)
        items.append(MediaItem(url=u, kind=kind, source=source, key_path=key_path,
                               title=title, origin_page=origin_page or base_url,
                               is_thumb=th, extra={"ext": ext_of(u)}))

    # 1) 所有带媒体地址的属性值（通用扫描，不依赖具体标签）
    #
    # 引号必须三种都支持：实测 avjoy.me 详情页把 <source> 的辅助属性写成单引号
    #   <source src="...mp4" type='video/mp4' label='720p' res='720'/>
    # 只匹配双引号的实现，只要站点整体改用单引号就会一个视频都拿不到。
    # 同时用 (?<![-\w]) 保证 attr="src" 不会错误命中 data-src="..." 的内部。
    for m in _ATTR_VALUE_RE.finditer(html):
        attr = _canon_media_attr(m.group("name"))
        if not attr:
            continue
        v = m.group(2) or m.group(3) or m.group(4) or ""
        if not v or len(v) > 1000:
            continue
        # srcset 形式：多个候选
        if "," in v and re.search(r"\d+[wx]", v):
            for part in v.split(","):
                add(part.strip().split()[0] if part.strip() else "", f"attr:{attr}")
        else:
            add(v, f"attr:{attr}")

    # 2) 正文中所有带扩展名的地址（覆盖内联 JSON、CSS、脚本里的地址）
    for m in _URL_RE.finditer(html):
        add(m.group("url"), "html:text")

    # 3) <script type="application/ld+json"> 与内嵌 JSON：交给 JSON 通道
    for m in re.finditer(r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
                         html, re.S | re.I):
        items.extend(extract_from_json_text(m.group(1), base_url, origin_page, source="jsonld"))

    # 4) 内嵌 JSON 数据块（通用特征：赋值给全局变量的大 JSON）
    for m in re.finditer(r'window\.[A-Za-z0-9_$_.]+\s*=\s*(\{.{500,}?\});', html, re.S):
        items.extend(extract_from_json_text(m.group(1), base_url, origin_page, source="window-json"))

    # 5) <video poster="..."> → 视频封面
    #
    # 通用语义：poster 是 video 元素的封面属性，与同一播放器里的 src/<source> 配对。
    # 为什么必须显式关联：用户选「仅视频」时，类型筛选会把页面上所有图片丢掉 ——
    # 封面图也一起被丢掉，结果卡片既没缩略图也没名称（用户反馈的 bug）。
    # 封面只有挂在视频条目自身（thumbnail 字段）上，才能跟着视频一起留下来。
    if items:
        poster_map: dict[str, str] = {}
        first_poster = ""
        for m in re.finditer(r"<video\b([^>]*)>(.*?)</video>", html, re.S | re.I):
            attrs, inner_html = m.group(1), m.group(2)
            pm = re.search(r"""poster\s*=\s*(?:"([^"]+)"|'([^']+)')""", attrs, re.I)
            poster = normalize((pm.group(1) or pm.group(2)) if pm else "", base_url)
            if not poster:
                continue
            first_poster = first_poster or poster
            srcs: list[str] = []
            sm = re.search(r"""src\s*=\s*(?:"([^"]+)"|'([^']+)')""", attrs, re.I)
            if sm:
                srcs.append(sm.group(1) or sm.group(2) or "")
            for s in re.finditer(r"""<source\b[^>]*?\bsrc\s*=\s*(?:"([^"]+)"|'([^']+)')""",
                                 inner_html, re.I):
                srcs.append(s.group(1) or s.group(2) or "")
            for raw in srcs:
                su = normalize(raw, base_url)
                if su:
                    poster_map[su] = poster
        for it in items:
            if it.kind in ("video", "stream"):
                if it.url in poster_map:
                    it.thumbnail = poster_map[it.url]
                elif first_poster and not it.thumbnail:
                    it.thumbnail = first_poster

    return items


# ---------- JSON 通道提取 ----------

def _try_parse_json(text: str):
    """尽量把文本解析成 JSON 对象；失败返回 None。"""
    if not text or not text.strip():
        return None
    s = text.strip()
    if s[0] not in "[{":
        return None
    try:
        return json.loads(s)
    except (json.JSONDecodeError, ValueError):
        # 有些字段是被转义的 JSON 字符串，先反转义再试
        try:
            unescaped = json.loads(f'"{s}"')
            return json.loads(unescaped)
        except Exception:
            return None


def extract_from_json_text(text: str, base_url: str = "", origin_page: str = "",
                           source: str = "json") -> list[MediaItem]:
    """从一段 JSON 文本提取媒体。"""
    data = _try_parse_json(text)
    if data is None:
        # 不是合法 JSON，退化为纯文本正则提取
        out = []
        for m in _URL_RE.finditer(text or ""):
            u = normalize(m.group("url"), base_url)
            if u and not _is_tracking(u):
                kind = classify(u)
                th = is_thumb(u) or (kind in ("video", "stream")
                                     and is_preview_video(u, source))
                out.append(MediaItem(url=u, kind=kind, source=source,
                                     origin_page=origin_page, is_thumb=th,
                                     extra={"ext": ext_of(u)}))
        return out
    return extract_from_json(data, base_url, origin_page, source)


def extract_from_json(data, base_url: str = "", origin_page: str = "",
                      source: str = "json") -> list[MediaItem]:
    """递归遍历任意 JSON 结构，提取媒体地址。

    通用规则（不绑定任何站点）：
      - 字符串值：先按带扩展名的正则提取；再对「字段名含媒体语义」的值放宽为裸地址
      - 字段值本身是被转义的 JSON 字符串：递归解析（阿里 adInfo.ext 就是这种）
      - 记录键路径，便于定位与调试
    """
    items: list[MediaItem] = []
    seen_local: set[str] = set()
    # 防止极端嵌套导致爆栈
    MAX_DEPTH = 12
    # 防止超大 JSON 遍历过久
    MAX_NODES = 200000
    state = {"n": 0}

    # 同一个媒体在 JSON 里通常会出现多次：多个清晰度档位 + 多个 CDN 镜像。
    # 它们共享同一个「媒体标识」（实测抖音：16 个 bit_rate 档位 + 各自的 3 个镜像
    # url_list 全部共用同一个 play_addr.uri）。不归并的话，一个只有 18 条视频的
    # 博主页会变成 800 多条结果，用户根本没法用。
    # 这里按标识收敛成一条，其余地址进 extra.variants —— 下载层本来就支持
    # 「首选地址失败时自动换用候选地址」，正好复用。
    by_ident: dict[str, MediaItem] = {}

    def add(raw: str, key_path: str, title: str = "", ident: str = ""):
        u = normalize(raw, base_url)
        if not u or u in seen_local:
            return
        if _is_tracking(u):
            return
        kind = classify(u, key_path)
        if kind == "skip":
            return
        seen_local.add(u)
        # JSON 里没有 uri/video_id 字段时，退一步用地址本身推出的归并键。
        # 实测知乎问题页：回答里的视频只给了 14 条裸地址（HD/SD/hevc 各一份），
        # 没有任何 id 字段，靠 IDENT_KEYS 一条都归并不上，页面上就是 14 条同一部片。
        if not ident and kind in ("video", "stream"):
            ident = _video_variant_info(u)[0] or ""
        if ident:
            prev = by_ident.get(ident)
            if prev is not None:
                vs = prev.extra.setdefault("variants", [])
                if u != prev.url and u not in vs and len(vs) < 8:
                    vs.append(u)
                return
        th = is_thumb(u)
        if kind in ("video", "stream") and not th:
            th = is_preview_video(u, source)
        item = MediaItem(url=u, kind=kind, source=source, key_path=key_path,
                         title=title, origin_page=origin_page or base_url,
                         is_thumb=th, extra={"ext": ext_of(u)})
        items.append(item)
        if ident:
            by_ident[ident] = item

    # 带媒体标识语义的键名（不含裸 id：裸 id 在站点结构里到处都有，会误归并）
    IDENT_KEYS = ("uri", "vid", "video_id", "videoid", "media_id", "mediaid",
                  "file_id", "fileid", "asset_id", "play_id")

    def walk(node, path: str, depth: int, parent_title: str = "",
             ident: str = ""):
        state["n"] += 1
        if depth > MAX_DEPTH or state["n"] > MAX_NODES:
            return
        # 键名语义要看**路径末两段**，不能只看最后一段。
        #
        # 为什么（2026-09-23 实测抖音博主主页）：作品列表接口的结构是
        #     aweme_list[].video.play_addr.url_list[]
        # 最后一段是 `url_list[]`，本身不含任何媒体语义，于是这些**无扩展名**的
        # CDN 视频地址（抖音就是这么给的：.../video/tos/cn/xxx/?a=6383）全部被漏掉 ——
        # 一次能抓到 306 个 play_addr，实际只提取出 0 个视频。
        # 取末两段后 `play_addr.url_list` 命中 "play" 语义，即可正确收录。
        kl = ".".join(path.lower().split(".")[-2:]) if path else ""

        if isinstance(node, dict):
            # 同层的标题字段，作为该层媒体的 title（通用键名）
            local_title = parent_title
            for tk in ("title", "name", "subject", "caption", "alt", "text",
                       "productTitle", "desc", "description"):
                v = node.get(tk)
                if isinstance(v, str) and 2 < len(v) < 300:
                    local_title = v
                    break
            # 媒体标识：本层若带 uri/video_id 之类字段，往下传递作为归并键
            local_ident = ident
            for ik in IDENT_KEYS:
                iv = node.get(ik)
                if isinstance(iv, str) and 3 < len(iv) < 200:
                    local_ident = iv
                    break
            for k, v in node.items():
                walk(v, f"{path}.{k}" if path else k, depth + 1, local_title,
                     local_ident)

        elif isinstance(node, list):
            for it in node:
                walk(it, path + "[]", depth + 1, parent_title, ident)

        elif isinstance(node, str):
            s = node.strip()
            if not s:
                return
            # (a) 带扩展名的地址：直接提取
            for m in _URL_RE.finditer(s):
                add(m.group("url"), path, parent_title, ident)

            # (b) 字段名含媒体语义 -> 接受无扩展名地址
            #
            # 关键：字段名有媒体语义时，**不再**要求 URL 路径也含媒体关键词。
            # 此前两个条件取交集，导致 //cdn/play/99887766 这类地址被漏掉
            # （playAddr 已命中 play 语义，但路径不含 /video/ 就被拒）。
            # 很多 CDN 用无扩展名路径，字段语义本身已是足够强的信号。
            #
            # 但放宽后广告链接也钻了进来（实测 Pixabay 结果混入
            # canva.com/content-partner/?utm_medium=...），因此对**无扩展名**地址
            # 追加营销特征二次把关；带媒体扩展名的地址是真实媒体，直接接受。
            hint = (any(h in kl for h in VIDEO_KEY_HINTS + IMAGE_KEY_HINTS)
                    and not any(h in kl for h in NON_MEDIA_KEY_HINTS))
            if hint and len(s) < 1500:
                cand = s
                if cand.startswith("//") or cand.startswith("http"):
                    e = ext_of(cand)
                    if e in SKIP_EXTS or e in PAGE_EXTS:
                        pass                     # 资源文件/页面链接，不是媒体
                    elif e in (IMAGE_EXTS | VIDEO_EXTS | STREAM_EXTS):
                        add(cand, path, parent_title)   # 有媒体扩展名，直接接受
                    elif ext_of(cand) in AUDIO_EXTS:
                        pass                     # 音频（BGM 等）不是本工具的媒体类型
                    elif not _looks_like_marketing(cand):
                        add(cand, path, parent_title, ident)  # 无扩展名且不像营销链接
                elif cand.startswith("/") and len(cand) < 400:
                    if not _looks_like_marketing(cand):
                        add(cand, path, parent_title, ident)

            # (c) 值是被转义的 JSON 字符串：递归解析
            if len(s) > 60 and s.lstrip()[0:1] in ("{", "["):
                inner = _try_parse_json(s)
                if inner is not None:
                    walk(inner, path + "(json)", depth + 1, parent_title)

    walk(data, "", 0)
    return items


# ---------- 详情链接提取（用于下钻）----------

def extract_page_links(html: str, json_text: str, base_url: str,
                       same_host_only: bool = True) -> list[dict]:
    """提取列表页里的「详情页链接」，供下钻抓取。

    通用启发式（不绑定任何站点）：
      - HTML 的 <a href>
      - HTML 内嵌 JSON（window.xxx = {...} / ld+json）—— 很多站点把数据放在这里，
        页面 <a> 反而没有（实测阿里 60 个商品链接全在 JSON 里）
      - 显式传入的拦截 JSON
      - 按相关性评分排序，让真正的详情页排在导航/分类链接之前

    每个链接还会带上 `thumb`（该 <a> 内部图片的地址）：
    列表页的条目缩略图，正是详情页视频最合适的封面 —— 用户只看「仅视频」时，
    这张图本身会被类型筛选丢掉，但挂在视频条目上就能留下来当封面。
    """
    links: list[dict] = []
    by_url: dict[str, dict] = {}
    try:
        base_host = (urlparse(base_url).hostname or "").lower()
        base_root = ".".join(base_host.split(".")[-2:]) if base_host else ""
    except Exception:
        base_root = ""
    # 当前列表页的路径前缀：用来识别「同一列表的筛选/翻页/导航变体」。
    # 实测（xvideos 分类页 2026-09-29）：/c/m:today/…、/c/q:hd/… 这类筛选链接
    # 与真视频详情页**同分**，max_items 小的时候把真视频页挤出下钻名额
    # （40 个名额只进了 9 条视频，27 条视频被分散到第 2~76 名）。
    # 判据：链接与当前页**同首段路径**且不比当前页深 → 是导航变体，扣分沉底。
    base_segs = [s for s in (urlparse(base_url).path or "").split("/") if s] if base_url else []

    def _adj_score(u: str, source: str, title: str) -> float:
        sc = _score_link(u, source, title)
        if base_segs:
            lsegs = [s for s in (urlparse(u).path or "").split("/") if s]
            if lsegs and lsegs[0] == base_segs[0] and len(lsegs) <= len(base_segs):
                sc -= 5
        return sc

    def add(raw: str, source: str, title: str = "", thumb: str = ""):
        u = normalize(raw, base_url)
        if not u:
            return
        p = urlparse(u)
        if p.path in ("", "/") and not p.query:
            return                      # 首页/空路径不是详情页
        if _is_search_query_url(u):
            return                      # 搜索/列表页不是详情页（下钻它会带回整页无关媒体）
        if _is_tracking(u):
            return
        e = ext_of(u)
        if e in SKIP_EXTS or e in IMAGE_EXTS | VIDEO_EXTS | STREAM_EXTS:
            return                      # 资源文件不是详情页
        host = (p.hostname or "").lower()
        if same_host_only and base_root and not host.endswith(base_root):
            return
        tu = normalize(thumb, base_url) if thumb else ""

        prev = by_url.get(u)
        if prev is not None:
            # 同一地址出现多次时**不能直接丢弃后来者**。
            # 实测 avjoy.me：同一个详情页有两个 <a>（一个包缩略图、一个包标题），
            # 原实现保留最先出现的那条，标题恰好取自缩略图 <a> 的
            # inner text（一串时长），结果卡片标题是空的。
            # 改成「标题更有信息量者胜」，并保留更高的相关性分数。
            if _title_quality(title) > _title_quality(prev.get("title", "")):
                prev["title"] = title
            if tu and not prev.get("thumb"):
                prev["thumb"] = tu
            if source not in prev.get("source", ""):
                prev["source"] = f"{prev.get('source', '')},{source}"
            prev["score"] = max(prev.get("score", 0.0),
                                _adj_score(u, source, title))
            return

        item = {
            "url": u, "source": source, "title": title, "host": host,
            "thumb": tu, "score": _adj_score(u, source, title),
        }
        by_url[u] = item
        links.append(item)

    # 1) HTML <a>
    #
    # 标题三级回退（都必须有，否则「视频没有名称」）：
    #   ① <a> 的 inner text        —— 常规情况
    #   ② <a title="...">          —— 文本由 JS 填充时才看得到
    #   ③ 内部 <img> 的 alt/title  —— avjoy 这种「<a> 里只有一张图」的结构，
    #                                真正的标题只存在于 img 属性里
    for m in re.finditer(r"<a\s([^>]*)>(.*?)</a>", html or "", re.S | re.I):
        attrs, inner = m.group(1), m.group(2)
        hm = re.search(r"""href\s*=\s*(?:"([^"]+)"|'([^']+)'|([^\s>]+))""", attrs, re.I)
        if not hm:
            continue
        href = hm.group(1) or hm.group(2) or hm.group(3) or ""

        title = _clean_title(inner)
        if not title:
            am = re.search(r"""title\s*=\s*(?:"([^"]+)"|'([^']+)')""", attrs, re.I)
            if am:
                title = _clean_title(am.group(1) or am.group(2) or "")
        if not title:
            for key in ("alt", "title"):
                im = re.search(rf"""<img[^>]*\b{key}\s*=\s*(?:"([^"]+)"|'([^']+)')""",
                               inner, re.I)
                if im:
                    title = _clean_title(im.group(1) or im.group(2) or "")
                    if title:
                        break

        # 内部第一张「图片」= 该条目的缩略图（用于给详情页视频当封面）。
        # 为什么不能只取第一张：实测 pornhub 列表页 <a> 里的 <img> src 本身
        # 可能就是悬停预览 mp4 —— 拿视频当封面，前端只会显示一张裂图。
        # 因此收集全部候选，优先选非视频扩展名的；全是视频时才退回第一张。
        thumb = ""
        cand_thumbs = [(a or b or c) for a, b, c in re.findall(
            r"""<img[^>]*\bsrc\s*=\s*(?:"([^"]+)"|'([^']+)'|([^\s"'=<>`]+))""",
            inner, re.I)]
        for cand in cand_thumbs:
            if cand and ext_of(cand) not in VIDEO_EXTS | STREAM_EXTS:
                thumb = cand
                break
        if not thumb and cand_thumbs:
            thumb = cand_thumbs[0]

        add(href, "html:a", title, thumb)

    # 1b) <iframe src>：YouTube / Bilibili / Vimeo 的「嵌入播放器」。
    # 企业官网的视频几乎全是这种形态（实测 ledyilighting.com 全站 8 个 iframe
    # 全是 YouTube embed，页面上一个 <a> 都没有）——不认 iframe，整站抓取
    # 的「要视频」就永远是 0。嵌入地址 yt-dlp 都能直接解析下载，
    # 所以给高分让它们排进下钻/视频名单。
    for m in re.finditer(
            r'<iframe[^>]*\bsrc\s*=\s*(?:"([^"]+)"|\'([^\']+)\')', html or "", re.I):
        src = (m.group(1) or m.group(2) or "").strip()
        if not src:
            continue
        add(src, "html:iframe", "", "")

    # 2) HTML 内嵌 JSON（很多站点的列表数据只在这里）
    if html:
        for m in re.finditer(r'window\.[A-Za-z0-9_$_.]+\s*=\s*(\{.{500,}?\});', html, re.S):
            data = _try_parse_json(m.group(1))
            if data is not None:
                _walk_links(data, "", add, base_url)
        for m in re.finditer(
                r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
                html, re.S | re.I):
            data = _try_parse_json(m.group(1))
            if data is not None:
                _walk_links(data, "", add, base_url)

    # 3) 拦截到的 JSON
    data = _try_parse_json(json_text or "")
    if data is not None:
        _walk_links(data, "", add, base_url)

    # 4) 同一路径的多个变体：优先「带访问参数」的那条
    #
    # 实测（2026-09-26 小红书主页）：同一篇笔记在页面里出现两种形式 ——
    #   /explore/<id>                                   ← 卡片 <a>，**打开会被拒**（没有令牌）
    #   /user/profile/<uid>/<id>?xsec_token=ABZ…        ← 内嵌数据里，**能打开并拿到视频**
    # 前者排在前就把下钻名额全占了，结果是「抓到一堆图片、0 个视频」。
    # 判据只看「查询串里有没有像访问令牌/签名的东西」（名字含 token/sign/auth…，
    # 或值长得像不透明串），不看站点 —— 避免把 ?utm_source= 这类跟踪参数当成访问凭据。
    _ACCESS_KW = re.compile(
        r"(token|sign|signature|auth|ticket|credential|secret|apikey|api_key|"
        r"\bkey\b|\bsig\b|\bsec\b|expire|expires|signature)", re.I)

    def _has_access_param(u: str) -> bool:
        try:
            q = urlparse(u).query
        except ValueError:
            return False
        for kv in (q or "").split("&"):
            if not kv:
                continue
            k, _, v = kv.partition("=")
            if _ACCESS_KW.search(k) or len(v) >= 24:
                return True
        return False

    grouped: dict[str, list[dict]] = {}

    def _content_key(u: str) -> str:
        """按「内容 ID」归并同一目标的多种地址形态。

        为什么不能只按 path 归并（2026-09-26 小红书实测）：
          同一篇笔记在页面里有两种地址 ——
            /explore/<note_id>                            ← 卡片链接，**打开被拒**
            /user/profile/<uid>/<note_id>?xsec_token=ABZ… ← 内嵌数据，**能打开拿到视频**
          路径完全不同，但最后一段的 <note_id> 是同一个。所以按「最后一个**像 ID** 的
          路径段」归并，才能把这两种形态认成同一篇笔记并挑出能用的那条。
          只认「像 ID 的段」（长十六进制 / 长数字 / 长的十六进制混合），
          避免把 index.html、page2 这类通用尾段误当成同一个内容。
        """
        try:
            p = urlparse(u)
        except ValueError:
            return u
        host = (p.hostname or "").lower()
        for seg in reversed([s for s in (p.path or "").split("/") if s]):
            if re.fullmatch(r"[0-9a-fA-F]{8,}", seg) or re.fullmatch(r"\d{6,}", seg) \
                    or re.fullmatch(r"[0-9a-zA-Z_-]{16,}", seg):
                return f"{host}#{seg}"
        return u          # 拿不到 ID 就按整条地址处理（不归并）

    for l in links:
        grouped.setdefault(_content_key(l["url"]), []).append(l)
    pruned: list[dict] = []
    for _key, group in grouped.items():
        with_acc = [g for g in group if _has_access_param(g["url"])]
        if with_acc and len(group) > 1:
            # 有可访问变体时丢掉同内容的裸变体（裸地址大概率打不开，别浪费下钻名额），
            # 并把它顶到同组最高分之前，保证它排在下钻名单前面。
            top = max(g.get("score", 0) for g in group)
            for g in with_acc:
                g["score"] = top + 2
            pruned.extend(with_acc)
        else:
            pruned.extend(group)
    links = pruned

    links.sort(key=lambda x: -x["score"])
    return links


def _is_search_query_url(url: str) -> bool:
    """判断地址是不是「搜索/关键词列表页」而不是内容详情页。

    通用形态（不绑定站点）：路径很浅（0~1 段）+ 查询串只是一个搜索词
    （/?k=xxx、/?q=xxx、/?s=xxx、/?search=xxx）。
    实测教训（xvideos）：搜索结果页的「相关搜索」链接带出
    /?k=curious+wife+no+panties&top 这类地址，它被当成详情页下钻之后，
    整页列表卡片的视频被当成该页内容带回结果，27 部片之外多出 1 条完全无关的片。
    只扣分挡不住（下钻名额够多时它仍然会入选），所以直接从候选里排除。
    """
    try:
        p = urlparse(url)
    except ValueError:
        return False
    if len([s for s in (p.path or "").split("/") if s]) > 1:
        return False
    # 查询串里带内容 ID 的（?viewkey=xxx / ?id=xxx）是详情页，不是搜索页
    if re.search(r"[?&](viewkey|video_?id|content_?id|media_?id|vid|v|id)=[\w-]{6,}",
                 url, re.I):
        return False
    return bool(re.search(r"(?:^|[?&])(k|q|query|search|keyword|kw|wd|term|s)=[^&]{3,}",
                          p.query, re.I))


def _score_link(url: str, source: str, title: str = "") -> float:
    """给详情页链接打相关性分，用于排序（通用启发式，不含站点专用规则）。

    思路：真正的详情页通常「路径更深、带数字 ID、来自数据接口而非页面导航」。
    导航/分类链接通常路径浅、指向聚合页。
    """
    score = 0.0
    p = urlparse(url)
    path = p.path or ""
    segs = [s for s in path.split("/") if s]

    # 畸形链接：目标地址里又嵌套了一个完整 URL / 带引号转义（页面源码里
    # href 被错误拼接，实测形如 /video/\"https:/help.xxx/hc/...）。
    # 这类链接不仅没用，还会以高分挤占下钻名额（实测拿到 17 分，排在第 1 位）。
    if re.search(r"https?:/", path, re.I) or re.search(r"https?:/", p.query, re.I) \
            or '"' in url or "\\" in url:
        return -99.0

    # 帮助 / 法务 / 信息页：跨站点的通用语义，绝不是内容详情页。
    # 权重必须压过「路径深 + 带数字」的加分，否则帮助中心的文章
    # （/hc/en-us/categories/4419836205203，深路径+长数字）会排到真视频详情页前面，
    # 实测正是它和畸形链接一起吃掉了 1/3 的下钻名额。
    if re.search(r"/(information|help|support|terms|privacy|cookie|policy|dmca|"
                 r"2257|faq|legal|about|contact|contest|contest_hub|campaign|promo)"
                 r"(/|$|\?)", url, re.I):
        score -= 30
    # help./support./legal./docs. 这类子域同样是信息页
    if (p.hostname or "").split(".")[0].lower() in (
            "help", "support", "legal", "info", "docs", "faq", "static", "cdn", "assets"):
        score -= 25

    # 路径深度：详情页一般比导航页深
    score += min(len(segs), 4) * 1.5

    # 含数字 ID（如 _1601560639032.html）是详情页的强特征。
    # 注意查询串也算：很多站点的详情页是 /view_video.php?viewkey=xxx 这种
    # 「浅路径 + 查询串内容 ID」形态，只看路径会把它误判成导航页
    # （实测 pornhub：48 个视频详情链接全部因此沉底，下钻名额被用户主页吃掉）。
    if re.search(r"\d{6,}", path) or re.search(r"\d{6,}", p.query):
        score += 6

    # 嵌入播放器（iframe 带来的）是「确定的视频页」，不是普通详情页 ——
    # 给全场最高优先级，保证它们排在下钻名单最前（实测企业站视频全是 YouTube embed）。
    if source == "html:iframe" and (
            re.search(r"(youtube|youtu\.be|bilibili|vimeo|dailymotion|tiktok|douyin)", url, re.I)):
        return 99.0
    elif re.search(r"\d{3,}", path):
        score += 3

    # 查询串里的内容 ID 参数（viewkey / video_id / vid / v / id）是详情页的强特征，
    # 能覆盖「ID 是字母数字混合、没有连续数字」的情况（如 ?viewkey=6a9df5ea4da71）。
    # 这类地址按 ID 就是一条内容，不该再按「浅路径」扣分。
    has_content_id = bool(re.search(
        r"[?&](viewkey|video_?id|content_?id|media_?id|vid|v|id)=[\w-]{6,}", url, re.I))
    if has_content_id:
        score += 10

    # 路径含详情语义关键词
    if re.search(r"/(product|item|detail|video|watch|photo|gallery|note|post|article)"
                 r"[-/]", path, re.I):
        score += 5

    # 来自 JSON 数据接口比来自页面 <a> 更可能是数据条目
    if source.startswith("json"):
        score += 3

    # 有标题文本（说明是内容链接而非图标/按钮）
    if title and len(title) > 8:
        score += 2

    # 扣分项：明显的聚合/导航页。
    # profile/user/channel/playlist/tag 都是「人/集合」页而非「内容」页，
    # 下钻它们只会带回一堆无关媒体（实测 pornhub 用户主页每页带出上百个
    # 无关视频档位，把结果污染得没法看）。
    if re.search(r"/(search|category|cate|list|filter|purchase|cart|login|"
                 r"register|account|profile|users?|member|model|"
                 r"channel|playlist|tags?|labels?)(/|$|\?)", url, re.I):
        score -= 8
    # 「根路径 + 查询串是搜索词」（/?k=xxx、/?q=xxx、/?search=xxx）是搜索/列表页，
    # 不是内容详情页（判据见 _is_search_query_url），直接压到最低档。
    if _is_search_query_url(url):
        score -= 30
    if len(segs) <= 1 and not has_content_id:
        score -= 3
    # 浅路径且没有任何 ID（路径和查询串都算不上）：
    # 基本是分类/标签/静态页，不可能是具体内容详情页。
    # 实测 avjoy.me 的 120 个链接里，24 个真详情页全是 /video/{id}/{slug}
    # （带 5 位数字 ID，score 14.5+），其余是 /videos/jav、/static/terms 这类
    # 导航页（score 5.0/3.0）。max_items 有限时必须把它们排到最后，
    # 否则下钻名额会被导航页吃掉。
    if not re.search(r"\d{3,}", url) and len(segs) <= 2 and not has_content_id:
        # 例外：首段是「字母数字混合的内容 ID」（实测 xvideos 的
        # /video.ubiaiukdc81/slug —— ID 无连续长数字，上面的 \d{3,} 读不出来），
        # 不豁免的话真详情页会沉到筛选杂链后面（27 条视频分散到第 2~76 名）。
        seg0 = segs[0] if segs else ""
        id_like = len(seg0) >= 8 and re.search(r"[A-Za-z]", seg0) and re.search(r"\d", seg0)
        if not id_like:
            score -= 4
    # 查询串过长的跳转链接（常见于广告/埋点跳转）
    if len(p.query) > 120:
        score -= 2

    return score


def _walk_links(node, path: str, add, base_url: str, depth: int = 0):
    """递归在 JSON 中找详情页链接。"""
    if depth > 12:
        return
    if isinstance(node, dict):
        for k, v in node.items():
            _walk_links(v, f"{path}.{k}" if path else k, add, base_url, depth + 1)
    elif isinstance(node, list):
        for it in node:
            _walk_links(it, path + "[]", add, base_url, depth + 1)
    elif isinstance(node, str):
        s = node.strip()
        if not s or len(s) > 600:
            return
        kl = path.rsplit(".", 1)[-1].lower()
        if any(h in kl for h in NON_MEDIA_KEY_HINTS):
            return
        if not (s.startswith("http") or s.startswith("//") or s.startswith("/")):
            return
        # 字段名含链接语义，或值看起来是详情页路径
        if any(h in kl for h in LINK_KEY_HINTS) or re.search(
                r"/(product|item|detail|video|watch|photo|gallery|p|v|note)/", s, re.I):
            add(s, f"json:{path}")


# ---------- 汇总 ----------

def _dedupe_key(it: "MediaItem") -> str:
    """同一张图的不同尺寸档位算成一张。

    2026-11-08 用户报「知乎抓取图片会有重复图」：一篇回答里同一张图会同时以
    `v2-xxx_720w.jpg`、`v2-xxx_1440w.jpg`、`v2-xxx_r.jpg` 出现，URL 各不相同
    但内容是同一张。只按完整 URL 去重，列表里就会有两三份一模一样的图。
    视频不做归一化 —— 不同清晰度是用户要选的东西，合并会丢档位。
    """
    if it.kind != "image":
        return it.url
    return strip_thumb_suffix(it.url) or it.url


def dedupe(items: list[MediaItem]) -> list[MediaItem]:
    """按「内容」去重，合并来源信息，优先保留信息更全的那条。

    图片按去掉尺寸后缀后的地址去重（见 _dedupe_key），并且**原图优先于缩略图**：
    enrich_with_originals 会为缩略图补一条原图记录，两者必须归成一条，
    否则用户看到的就是「一张图 + 它的原图」两份。
    """
    best: dict[str, MediaItem] = {}
    for it in items:
        if not it.url:
            continue
        key = _dedupe_key(it)
        cur = best.get(key)
        if cur is None:
            best[key] = it
            continue
        # 同一张图的两个版本：原图（is_thumb=False）压过缩略图
        if cur.is_thumb and not it.is_thumb:
            it.source = f"{cur.source},{it.source}" if cur.source and cur.source not in it.source else it.source
            best[key] = it
            cur = it
        # 合并：补全缺失字段
        if not cur.title and it.title:
            cur.title = it.title
        if not cur.width and it.width:
            cur.width, cur.height = it.width, it.height
        if it.source and it.source not in cur.source:
            cur.source = f"{cur.source},{it.source}"
        # 视频优先于图片（同地址被判成两种类型时）
        if it.kind in ("video", "stream") and cur.kind not in ("video", "stream"):
            cur.kind = it.kind
    return list(best.values())


def enrich_with_originals(items: list[MediaItem]) -> list[MediaItem]:
    """为缩略图补充还原出的原图地址（保留缩略图，另加一条原图记录）。

    通用规则：识别 CDN 尺寸后缀并尝试还原。还原失败则不处理。
    """
    out: list[MediaItem] = []
    seen = {i.url for i in items}
    for it in items:
        out.append(it)
        if it.kind != "image" or not it.is_thumb:
            continue
        orig = strip_thumb_suffix(it.url)
        if orig and orig != it.url and orig not in seen:
            seen.add(orig)
            out.append(MediaItem(
                url=orig, kind="image", source="derived:thumb->original",
                key_path=it.key_path, title=it.title, referer=it.referer,
                origin_page=it.origin_page, is_thumb=False,
                extra={"ext": ext_of(orig), "derived_from": it.url},
            ))
    return out


def summarize(items: list[MediaItem]) -> dict:
    """统计各类媒体数量，供前端展示。"""
    from collections import Counter
    c = Counter(i.kind for i in items)
    return {
        "total": len(items),
        "image": c.get("image", 0),
        "video": c.get("video", 0),
        "stream": c.get("stream", 0),
        "unknown": c.get("unknown", 0),
        "thumbs": sum(1 for i in items if i.is_thumb),
        "sources": dict(Counter(i.source.split(",")[0] for i in items).most_common(8)),
    }
