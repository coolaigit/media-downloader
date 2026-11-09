# -*- coding: utf-8 -*-
"""视频下载后端：基于本机 yt-dlp，支持链接解析、清晰度选择、进度回调与取消。

要点：
- 用 bin/ffmpeg.exe（已从 wheel 提取到项目内）作为 ffmpeg_location，支持音视频分离流合并。
- ffmpeg 不可用时，自动优先选择「自带音轨的完整格式」（progressive），避免下出无声/无画面文件。
- 支持播放列表（playlist）与关键词搜视频（ytsearch / bilisearch）。
- 取消通过 progress_hook 抛出 CancelledError 实现，可及时中断长下载。
"""
from __future__ import annotations

import re
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlunparse

import yt_dlp

from .config import HEADERS, find_ffmpeg, find_js_runtime

# 搜索前缀：关键词搜视频（yt-dlp 内置搜索提取器，实测存在 YoutubeSearch / BiliBiliSearch）
SEARCH_PREFIXES = {
    "youtube": "ytsearch",
    "bilibili": "bilisearch",
    "bili": "bilisearch",
}


class DownloadCancelled(Exception):
    """用户取消下载。"""


# 全局取消标记：task_id -> threading.Event
_CANCEL_EVENTS: dict[str, threading.Event] = {}
_CANCEL_LOCK = threading.Lock()


def register_cancel_event(task_id: str) -> threading.Event:
    with _CANCEL_LOCK:
        ev = _CANCEL_EVENTS.setdefault(task_id, threading.Event())
    return ev


def request_cancel(task_id: str) -> bool:
    with _CANCEL_LOCK:
        ev = _CANCEL_EVENTS.get(task_id)
    if ev:
        ev.set()
        return True
    return False


def clear_cancel_event(task_id: str) -> None:
    with _CANCEL_LOCK:
        _CANCEL_EVENTS.pop(task_id, None)


def get_cancel_event(task_id: str):
    """给换源下载用：拿任务已注册的取消标记（没有就返回 None）。"""
    with _CANCEL_LOCK:
        return _CANCEL_EVENTS.get(task_id)


def _base_ydl_opts(
    task_id: str | None = None,
    quiet: bool = True,
    cookies_file: str | None = None,
    cookies_from_browser: str | None = None,
    custom_headers: bool = False,
    include_playlist: bool = False,
    proxy: str = "",
) -> dict:
    """构造 yt-dlp 公共选项。

    注意：默认 **不** 设置 http_headers。yt-dlp 各站点提取器（B 站、YouTube 等）
    自带经过调优的请求头组合，全局覆盖会破坏其行为（实测 B 站会触发 412 风控）。
    仅在 custom_headers=True 时才附加通用浏览器头，用于普通直链下载。

    include_playlist 默认 False：用户粘贴单个视频链接时只下该视频，
    避免视频恰属某合集而把整个合集全部下载（远超预期、可能极大）。
    """
    ffmpeg = find_ffmpeg()
    opts: dict = {
        "quiet": quiet,
        "no_warnings": True,
        "noprogress": quiet,
        "socket_timeout": 25,
        "retries": 3,
        "fragment_retries": 3,
        "noplaylist": not include_playlist,
        # 文件名清理：避免 Windows 非法字符
        "restrictfilenames": False,
        "windowsfilenames": True,
        "trim_file_name": 150,
        # 不写入 .info.json 等附属文件（保持下载目录整洁）
        "writeinfojson": False,
    }
    if custom_headers:
        opts["http_headers"] = dict(HEADERS)
    # 海外源（YouTube 等）单独走代理：国内资源站直连，互不影响。
    if proxy:
        opts["proxy"] = proxy
    if ffmpeg:
        opts["ffmpeg_location"] = str(Path(ffmpeg).parent)

    # JS 运行时：YouTube 的 JS 挑战必须有它，否则 media 地址一律 403
    # （见 config.find_js_runtime 的说明）。找不到时保持 yt-dlp 默认行为，
    # 不强行设置 —— 设一个不存在的运行时会报错。
    js = find_js_runtime()
    if js:
        name, path = js
        opts["js_runtimes"] = {name: {"path": path}}

    # Cookie 支持：应对需要登录态的风控站点（如 B 站 412 / YouTube 年龄限制）
    if cookies_file:
        p = Path(cookies_file).expanduser()
        if p.exists():
            opts["cookiefile"] = str(p)
    elif cookies_from_browser:
        # 形如 "chrome" / "edge:Default" / "firefox"
        # ⚠️ 必须预检：Chrome 运行时 cookie 库被锁，yt-dlp 读取会抛
        # "Could not copy Chrome cookie database"，旧实现把该选项直接塞给
        # yt-dlp → 整个解析/下载跟着一起失败（2026-09-30 用户实测
        # 「单个视频无法抓取和下载」的真凶）。读不了就优雅降级为无 cookie。
        jar, note = _browser_cookiejar_safe(_parse_browser_spec(cookies_from_browser))
        if jar is not None:
            opts["cookiejar"] = jar
        else:
            global _LAST_COOKIE_NOTE
            _LAST_COOKIE_NOTE = note

    if task_id:
        ev = register_cancel_event(task_id)

        def hook(d):
            if ev.is_set():
                raise DownloadCancelled("用户已取消下载")

        opts["progress_hooks"] = [hook]
    return opts


def _parse_browser_spec(spec: str) -> tuple:
    """把 "chrome" / "edge:Default" / "firefox:Profile 1" 解析为 yt-dlp 需要的元组。"""
    s = (spec or "").strip()
    if not s:
        return ()
    parts = s.split(":", 1)
    browser = parts[0].strip().lower()
    profile = parts[1].strip() if len(parts) > 1 else None
    # yt-dlp 期望 (browser, profile, keyring, container)
    return (browser, profile, None, None)


_LAST_COOKIE_NOTE: str | None = None


def last_cookie_note() -> str | None:
    """最近一次「浏览器 Cookies 读取失败已降级」的说明（给界面提示用）。"""
    return _LAST_COOKIE_NOTE


def _browser_cookiejar_safe(spec: tuple):
    """预检浏览器 Cookies：读不了（Chrome 锁库等）就返回 None + 说明，
    绝不让一个 cookie 选项拖垮整个解析/下载。"""
    browser, profile, keyring, container = spec
    if not browser:
        return None, None
    try:
        from yt_dlp.cookies import extract_cookies_from_browser
        # ⚠️ 该函数第 3 个位置参数是 logger（不是 keyring）——按关键字传参
        jar = extract_cookies_from_browser(browser, profile, keyring=keyring,
                                           container=container)
        return jar, None
    except Exception as e:  # noqa: BLE001
        return None, (f"浏览器 {browser} 的 Cookies 读不出来"
                      f"（{str(e)[:80]}）——本次不使用浏览器登录态继续")


def ffmpeg_available() -> bool:
    return find_ffmpeg() is not None


def build_format_selector(
    quality: str = "best",
    container: str = "",
    prefer_audio_only: bool = False,
    max_height: int | None = None,
    format_id: str = "",
) -> str:
    """按用户选择构造 yt-dlp format 字符串。

    quality 取值：
      best      - 最佳画质（可能需要 ffmpeg 合并）
      2160/1440/1080/720/480/360 - 不超过该高度的最佳画质
      worst     - 最小文件
      audio     - 仅音频
    format_id：精确格式选择（"用此格式"按钮），优先于 quality 档位。
    """
    if prefer_audio_only or quality == "audio":
        return "bestaudio/best"

    # 精确格式：直接用 format_id 选择；该格式若不含音轨则尝试与最佳音频合并，
    # 合并不了就退回该格式本身，最后才退回整体最佳。
    fid = (format_id or "").strip()
    if fid:
        # 防注入：format_id 应是普通标识符（如 "240p" / "137"），
        # 含选择器元字符的一律拒绝，退回常规档位逻辑
        if re.fullmatch(r"[\w.\-]+", fid):
            if ffmpeg_available():
                return f"{fid}+bestaudio/{fid}/best[vcodec!=none]"
            return f"{fid}/best[vcodec!=none]"

    height_cap = max_height
    if not height_cap:
        height_cap = {
            "2160": 2160, "4k": 2160,
            "1440": 1440, "2k": 1440,
            "1080": 1080, "fullhd": 1080,
            "720": 720, "hd": 720,
            "480": 480, "sd": 480,
            "360": 360, "240": 240, "144": 144,
        }.get(str(quality).lower())

    ext_part = ""
    if container:
        ext_part = f"[ext={container}]"

    if height_cap:
        # 有 ffmpeg 时用「最佳视频+最佳音频」合并；同时给出单文件兜底
        if ffmpeg_available():
            base = f"bestvideo{ext_part}[height<={height_cap}]+bestaudio/best{ext_part}[height<={height_cap}][vcodec!=none]/best[height<={height_cap}][vcodec!=none]/best{ext_part}[height<={height_cap}]/best[height<={height_cap}]"
        else:
            # 无 ffmpeg：优先自带音轨的完整视频；至少也必须包含视频轨。
            base = f"best{ext_part}[height<={height_cap}][vcodec!=none][acodec!=none]/best{ext_part}[height<={height_cap}][vcodec!=none]/best[height<={height_cap}][vcodec!=none]"
        return base

    if quality == "worst":
        # 不能只写 "worst"：实测（2026-09-21）B 站下 `worst` 会报
        # "Requested format is not available" —— 该站只暴露 bestvideo/bestaudio 这类
        # 分离流，没有 `worst` 这个别名。改成带兜底链的选择器：
        # 最小分离流合并 -> 最小完整视频 -> 最小视频。任何视频模式都禁止
        # 最终退到纯音频（TikTok 曾因此“任务成功”却落盘 .mp3）。
        if ffmpeg_available():
            return f"worstvideo{ext_part}+worstaudio/worst{ext_part}[vcodec!=none][acodec!=none]/worst{ext_part}[vcodec!=none]/worst[vcodec!=none]/worst{ext_part}[height>0]/worst[height>0]"
        return f"worst{ext_part}[vcodec!=none][acodec!=none]/worst{ext_part}[vcodec!=none]/worst[vcodec!=none]/worst{ext_part}[height>0]/worst[height>0]"
    if ffmpeg_available():
        return f"bestvideo{ext_part}+bestaudio/best{ext_part}[vcodec!=none]/best[vcodec!=none]/best{ext_part}[height>0]/best[height>0]"
    return f"best{ext_part}[vcodec!=none]/best[vcodec!=none]/best{ext_part}[height>0]/best[height>0]"


def _fallback_format_selector(container: str = "") -> str:
    """用户选的清晰度档位不可用时使用的兜底选择器。

    只求「能下下来」：分离流合并不成就退回整体最佳。实测 B 站选 `worst` 会报
    "Requested format is not available"，而 `bestvideo+bestaudio/best` 正常 ——
    所以这里的兜底以「最佳整体」为准，而不是继续在小档位里挑（那只是换一个报错）。
    """
    ext_part = f"[ext={container}]" if container else ""
    if ffmpeg_available():
        return f"bestvideo{ext_part}+bestaudio/best{ext_part}[vcodec!=none]/best[vcodec!=none]/best{ext_part}[height>0]/best[height>0]"
    return f"best{ext_part}[vcodec!=none]/best[vcodec!=none]/best{ext_part}[height>0]/best[height>0]"


def _is_media_forbidden(low_msg: str) -> bool:
    """是不是「媒体流被站点拒绝（403）」这一类错误。

    与「链接失效 / 权限不足」区分开：那类换客户端也没用，不该无谓重试。
    这里要求同时出现「403/forbidden」与「下载媒体数据」的迹象。
    """
    m = low_msg or ""
    if "403" not in m and "forbidden" not in m:
        return False
    markers = ("unable to download video data", "unable to download webpage",
               "videoplayback", "http error 403", "forbidden")
    return any(k in m for k in markers)


# 会把语言放进路径第一段的站点；只列确认过的，别一上来就通配。
_LANG_PATH_HOSTS = ("bilibili.com",)
_LANG_PATH_CODES = frozenset({
    "zh-hans", "zh-hant", "zh-cn", "zh-tw", "zh-hk", "zh-sg",
    "en", "en-us", "en-gb", "ja", "jp", "ko", "kr",
})


def _strip_lang_path(url: str) -> str:
    """剥掉站点塞在路径第一段的语言前缀（B 站 /zh-Hans/bangumi/play/ep810642）。

    为什么必须剥：yt-dlp 的 bilibili extractor 正则只认紧贴域名的
    `/bangumi/play/ep<id>`，带语言段时直接判 `unsupported url`，用户看到的是
    「该链接不是受支持的视频页面」（2026-11-09 实测：同一条 ep810642，
    带 `/zh-Hans/` 失败，去掉立刻下到 6 分钟片并给出试看提示）。

    只对白名单站点、且第一段确实在白名单语言码里才剥 —— 不做通用「去掉首段」，
    那样会把 `/tv/123` 这类真路径改坏。
    """
    try:
        p = urlparse(url)
    except ValueError:
        return url
    host = (p.netloc or "").lower()
    if not any(host == h or host.endswith("." + h) for h in _LANG_PATH_HOSTS):
        return url
    segs = [s for s in (p.path or "").split("/") if s]
    if len(segs) < 2 or segs[0].lower() not in _LANG_PATH_CODES:
        return url
    path = "/" + "/".join(segs[1:])
    return urlunparse((p.scheme, p.netloc, path, p.params, p.query, p.fragment))


def _normalize_media_url(url: str) -> str:
    """把「同一内容的不同地址形态」换成 yt-dlp 认的那一种。

    2026-11-08 实测（用户报「知乎抓取视频异常」）：
      https://www.zhihu.com/zvideo/<id>    yt-dlp 有专门 extractor，直接能解析
      https://www.zhihu.com/video/<id>     302 到 video.zhihu.com/video/<id>?$args，
                                          只回 1.3KB 的 JS 壳（<title>知乎视频</title>），
                                          纯 HTTP 拿不到任何内容 —— 走渲染也是空壳
    两者 id 相同、指向同一条视频（实测 /zvideo/1342930761977176064 标题就是那条）。
    所以先把 /video/ 形态换成 /zvideo/ 再解析。

    只在识别出这个明确形态时才改写，其它地址原样返回；
    任何站点都不会因为这条规则被改掉地址。
    """
    if not url:
        return url
    m = re.match(r"^https?://(?:www\.|video\.)?zhihu\.com/video/(\d+)", url.strip())
    if m:
        return f"https://www.zhihu.com/zvideo/{m.group(1)}"
    return _strip_lang_path(url.strip())


def probe_video(
    url: str,
    task_id: str | None = None,
    include_playlist: bool = False,
    cookies_file: str | None = None,
    cookies_from_browser: str | None = None,
    max_attempts: int = 3,
) -> dict:
    """解析视频链接，返回可用清晰度列表与元信息（不下载）。

    对瞬时网络错误（SSL 中断、超时、连接重置、5xx）自动重试；
    权限类错误（登录、私密、地域限制）不重试，直接返回友好提示。
    """
    _shown_url = url
    url = _normalize_media_url(url)
    last_err = ""
    for attempt in range(1, max_attempts + 1):
        opts = _base_ydl_opts(
            task_id,
            include_playlist=include_playlist,
            cookies_file=cookies_file,
            cookies_from_browser=cookies_from_browser,
        )
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
            break
        except DownloadCancelled:
            raise
        except yt_dlp.utils.DownloadError as e:
            last_err = str(e)
            # 非瞬时错误重试无意义，立即返回
            if not _is_transient_error(last_err):
                return {"ok": False, "error": _friendly_error(last_err), "url": url}
            if attempt < max_attempts:
                time.sleep(1.2 * attempt)
                continue
            return {"ok": False, "error": _friendly_error(last_err), "url": url}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"解析失败: {type(e).__name__}: {e}", "url": url}
    else:
        return {"ok": False, "error": _friendly_error(last_err), "url": url}

    if not info:
        return {"ok": False, "error": "未取到任何视频信息", "url": url}

    # 播放列表
    if info.get("_type") == "playlist" or "entries" in info:
        entries = [e for e in (info.get("entries") or []) if e]
        return {
            "ok": True,
            "is_playlist": True,
            "playlist_title": info.get("title") or "",
            "playlist_count": len(entries),
            "entries": [_summarize_entry(e, i) for i, e in enumerate(entries[:200])],
            "ffmpeg": ffmpeg_available(),
            "notice": last_cookie_note(),
        }

    summary = _summarize_info(info)
    # 这是“视频解析”入口：若上游明确只返回纯音频，不能把它当成视频解析成功。
    # 但 Generic 直链（例如小红书/抖音签名后的 .mp4）常不填 vcodec/acodec，
    # 此时 ext=mp4/webm/... 已足以证明是视频；不能因为 codec 元数据未知就误判失败。
    video_exts = {"mp4", "webm", "mkv", "mov", "avi", "flv", "m4v", "mpg", "mpeg", "3gp", "ts"}
    formats = summary.get("formats") or []
    has_video_format = any(
        bool(f.get("has_video")) or (
            str(f.get("ext") or "").lower() in video_exts
            and str(f.get("vcodec") or "").lower() != "none"
        )
        for f in formats
    )
    if not has_video_format and not summary.get("is_live"):
        return {
            "ok": False,
            "is_playlist": False,
            "error": "当前页面只解析到音频流，没有拿到可下载的视频流。可能是站点接口变化、地区/IP 或登录限制；请稍后重试或更新登录态。",
            "audio_only_available": any(bool(f.get("has_audio")) for f in (summary.get("formats") or [])),
            "notice": last_cookie_note(),
            **summary,
        }
    return {
        "ok": True,
        "is_playlist": False,
        "notice": last_cookie_note(),
        **summary,
    }


_SPECIFIC_IES_CACHE: list | None = None


def has_specific_extractor(url: str) -> bool:
    """yt-dlp 是否**专门**支持这个地址（而不是只能走通用 generic 提取器）。

    为什么需要这个闸门：我们要在「抓取视频」时先问一次「这是不是一个视频列表」，
    但这一步对 yt-dlp 没有专门支持的站点纯属浪费（generic 提取器永远不会给出
    列表，实测 oreelighting.com 白花约 5 秒）。判据是「yt-dlp 自己知不知道这个站」，
    不是我们写站点名单 —— yt-dlp 自带上千个站点提取器，这里只是查询它。
    """
    global _SPECIFIC_IES_CACHE
    if not url:
        return False
    if _SPECIFIC_IES_CACHE is None:
        try:
            from yt_dlp.extractor import gen_extractor_classes
            _SPECIFIC_IES_CACHE = [ie for ie in gen_extractor_classes()
                                   if ie.ie_key() not in ("Generic", "URL")]
        except Exception:  # noqa: BLE001
            _SPECIFIC_IES_CACHE = []
    for ie in _SPECIFIC_IES_CACHE:
        try:
            if ie.suitable(url):
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def strip_ansi(text: str) -> str:
    """去掉 ANSI 颜色码。

    为什么必须做：yt-dlp 的报错在「有终端」的进程里会带上颜色码，而且是插在词中间 ——
    实测服务进程里的原文是
        "ERROR: [instagram:user] x: Unable to extract <ESC>[0;94mdata<ESC>[0m"
    于是按 "unable to extract data" 做的判据**匹配不上**，
    结果是「需要登录」这种能给出下一步的提示退化成了一串谁也看不懂的转义字符
    （用户界面里真的显示出 \x1b[0;31m 这种乱码）。
    """
    return _ANSI_RE.sub("", text or "")


def _expand_error_hint(msg: str) -> str:
    """把「展开视频列表」失败的原因说成人话（含下一步该做什么）。

    这些判据都是 yt-dlp 的错误文本，不是站点规则。实测遇到的三种：
      1. Pinterest 画板全是图片 → "No video formats found"（此前被说成「配置 Cookies」，
         把人引向错误方向）
      2. Instagram 未登录 → "Unable to extract data"
      3. 浏览器正在运行 → "Could not copy Chrome cookie database"
    """
    clean = strip_ansi(msg)
    low = clean.lower()
    if "no video formats" in low or "no formats found" in low:
        return ("这个列表里的条目没有一个是视频（大多是图片），所以没有可下载的视频。"
                "要抓图片请切到「抓取图片」页，粘同一个地址。")
    if "could not copy" in low and "cookie" in low:
        return ("读取浏览器 Cookies 失败：浏览器正在运行会锁住 cookie 数据库。"
                "请先完全退出浏览器再试，或改用导出的 cookies.txt（推荐，不用关浏览器）。")
    if "unable to extract data" in low or "login" in low or "sign in" in low \
            or "requires authentication" in low or "rate-limit" in low:
        return ("该主页读不到内容：平台要求登录（Instagram 等对未登录访问不返回数据），"
                "也可能是账号名写错 / 已改名 / 已注销。"
                "需要登录的站点请在「设置」里填 cookies.txt 路径，"
                "或选「从浏览器读取 Cookies」（需先完全关闭浏览器）。")
    return _friendly_error(clean)


def _run_with_deadline(fn, deadline: float):
    """在守护线程里跑 fn，最多等 deadline 秒。

    为什么要它（实测 2026-09-22）：Pinterest 的某个画板让 yt-dlp 跑了 82 秒才报错，
    用户干等一分半，最后得到的还是一句误导性的提示。展开列表只是「锦上添花」的一步，
    超时就应该放弃并如实说明，让网页抓取继续跑。
    超时的线程是守护线程，不会阻止进程退出（它自己会随请求结束而收尾）。
    """
    box: dict = {}

    def _runner():
        try:
            box["ok"] = fn()
        except BaseException as e:  # noqa: BLE001
            box["err"] = e

    t = threading.Thread(target=_runner, daemon=True, name="expand-list")
    t.start()
    t.join(deadline)
    if t.is_alive():
        return None, "timeout"
    if "err" in box:
        raise box["err"]
    return box.get("ok"), ""


_AD_MEDIA_MARKERS = ("728x90", "300x250", "320x50", "970x250", "160x600", "300x600",
                     "/ads/", "/ad/", "banner", "sponsor", "promo")
# 站内「推广位」路径：hornyleak 的 HL.VIDEOS 里 8 条全是 contents/data/video-XX，
# 那是广告轮播，不是当前视频（实测 2026-10-24）。
_AD_PATH_HINTS = ("/contents/data/video-", "/creative/", "/promo/")


def _is_ad_media(url: str) -> bool:
    low = (url or "").lower()
    return any(mk in low for mk in _AD_MEDIA_MARKERS) or any(h in low for h in _AD_PATH_HINTS)


_dy_bsk_video_failed = False


def _douyin_real_url_via_bsk(url: str, timeout: int = 45) -> str:
    """用 BrowserSkill 打开抖音视频页，读 <video>.currentSrc（无窗口）。

    作为抖音直链解析的**第二意见**（主力是 _douyin_real_url_profiled）：
    - bsk 的浏览器指纹和内置 Chromium 不同，主力路被风控时可能从这里过；
    - 但实测视频页在 bsk 下拿不到内容（约 87s 才空手而归，2026-10-25），
      所以失败一次就在本进程内记为不可用 —— 批量下载时不能每条都白等 80 秒。

    注意过滤 douyinstatic.com：那不是正片。
    """
    global _dy_bsk_video_failed
    if _dy_bsk_video_failed:
        return ""
    try:
        from . import browser_skill as bs
        from . import browser_profile
    except Exception:  # noqa: BLE001
        return ""
    ready = bs.ensure_ready(headless=not browser_profile.headed(), timeout=40)
    if not ready.get("ok"):
        _dy_bsk_video_failed = True
        return ""
    sid = ""
    try:
        op = bs.start_page(url)
        sid = str(op.get("session_id") or "")
        time.sleep(6)
        deadline = time.time() + max(5, timeout - 12)
        while time.time() < deadline:
            srcs = bs.evaluate(
                sid,
                "() => Array.from(document.querySelectorAll('video'))"
                ".map(v => v.currentSrc || v.src || '').filter(s => s && s.indexOf('douyinstatic.com') < 0)",
                timeout=20) or []
            if isinstance(srcs, list):
                good = [s for s in srcs if isinstance(s, str) and s.startswith("http")]
                if good:
                    return good[-1]
            # 还没挂上地址：补一次播放
            try:
                bs.evaluate(sid, "() => { const v = document.querySelector('video');"
                                 " if (v) { v.muted = true; v.play().catch(()=>{}); } }",
                            timeout=10)
            except Exception:  # noqa: BLE001
                pass
            time.sleep(2.2)
        _dy_bsk_video_failed = True   # 视频页确认拿不到：本进程后续不再试
        return ""
    except Exception:  # noqa: BLE001
        _dy_bsk_video_failed = True
        return ""
    finally:
        if sid:
            try:
                bs.stop(sid)
            except Exception:  # noqa: BLE001
                pass


_dy_profile_lock = threading.Lock()


def _douyin_snapshot(url: str, timeout: int = 50) -> dict:
    """内置 Chromium（headless）+ 抖音登录态打开视频页，拿正片直链与元信息。

    返回 {"ok", "direct_url", "title", "cover", "duration", "error"}。

    2026-10-25 / 2026-11-07 实测矩阵：
    - headless + dy登录 → **能拿到正片直链**（zjcdn.com/video/tos/…，8~25s）。
    - headless + 无登录 → <video> 是占位资源 douyinstatic.com/uuu_265.mp4
    - yt-dlp + dy cookies → `Fresh cookies (not necessarily logged in) are needed`
      （detail 接口要 a_bogus 签名，extractor 里那段是 TODO）。所以**解析也必须走这里**，
      不能只在下载时走渲染。
    曾经在这里弹「有头窗口兜底」—— 用户明确要求下载时不许弹窗（2026-10-25），已删除。
    """
    try:
        from playwright.sync_api import sync_playwright
        from . import browser_runtime
        from .config import DEFAULT_UA
        from .renderer import _LAUNCH_ARGS
        from .platform_login import profile_dir_for_url
        prof = profile_dir_for_url(url, ready_only=True)
        if not prof:
            return {"ok": False, "direct_url": "", "title": "", "cover": "",
                    "duration": 0,
                    "error": "抖音登录态不可用：请到「平台登录」页重新登录抖音后再解析。"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "direct_url": "", "title": "", "cover": "",
                "duration": 0, "error": f"{type(e).__name__}: {e}"}

    # 同一个 dy profile 同时只能被一个 Chromium 占用：并发时在这里串行化，
    # 抢不到锁的请求等前一个关完再开（每次只占几秒到几十秒）。
    with _dy_profile_lock:
        try:
            with sync_playwright() as p:
                try:
                    ctx = p.chromium.launch_persistent_context(
                        user_data_dir=str(prof), headless=True, locale="zh-CN",
                        user_agent=DEFAULT_UA, viewport={"width": 1150, "height": 880},
                        args=list(_LAUNCH_ARGS), **browser_runtime.launch_kwargs())
                except Exception as e:  # noqa: BLE001 —— profile 打不开（被占用等）
                    return {"ok": False, "direct_url": "", "title": "", "cover": "",
                            "duration": 0, "error": f"打开抖音登录态失败：{e}"}
                try:
                    page = ctx.pages[0] if ctx.pages else ctx.new_page()
                    # 抖音现在用 MSE 播放：<video>.currentSrc 是
                    # `blob:https://www.douyin.com/<uuid>`，拿它下载只会得到
                    # 一个打不开的 blob。真实地址要从网络请求里捡。
                    # （2026-11-07 实测：页面渲染正常、标题拿得到，currentSrc 是 blob。）
                    media_urls: list[tuple[str, int]] = []   # (url, content-length)
                    cover_urls: list[str] = []               # 抖音封面（douyinpic CDN）

                    def _on_resp(resp):
                        try:
                            u = str(resp.url or "")
                            low = u.lower()
                            # 静态域的 uuu_265.mp4 是播放器 UI 占位，不是正片
                            if "douyinstatic.com" in low or "/obj/douyin-pc-web/" in low:
                                return
                            ct = str((resp.headers or {}).get("content-type") or "").lower()
                            # 封面：douyinpic CDN 的 tplv 裁切图，排掉 aweme-avatar（头像）
                            if ("douyinpic.com" in low and "aweme-avatar" not in low
                                    and "tplv" in low):
                                if u not in cover_urls:
                                    cover_urls.append(u)
                                return
                            if "douyinvod.com" not in low and not ct.startswith("video/"):
                                return
                            try:
                                size = int((resp.headers or {}).get("content-length") or 0)
                            except (TypeError, ValueError):
                                size = 0
                            if all(u != x for x, _ in media_urls):
                                media_urls.append((u, size))
                        except Exception:  # noqa: BLE001
                            pass

                    page.on("response", _on_resp)
                    try:
                        page.goto(url, wait_until="domcontentloaded", timeout=25000)
                    except Exception:  # noqa: BLE001
                        pass
                    deadline = time.time() + max(12, timeout - 12)
                    while time.time() < deadline:
                        try:
                            srcs = page.evaluate(
                                "() => Array.from(document.querySelectorAll('video'))"
                                ".map(v => v.currentSrc || v.src || '')"
                                ".filter(s => s && s.indexOf('douyinstatic.com') < 0)")
                        except Exception:  # noqa: BLE001
                            srcs = []
                        real = [(u, sz) for u, sz in media_urls if u.startswith("http")]
                        direct = ""
                        if real:
                            # 优先 .mp4 直链；m3u8 交给下载侧的分片合并。
                            # 同一页抖音会先吐低码率预览再吐正片，**取体积最大的那条**，
                            # 否则 235 秒的视频会下到 1.5 MB 的低清小样。
                            mp4 = [t for t in real if ".mp4" in t[0].lower().split("?")[0]]
                            direct = max(mp4 or real, key=lambda t: t[1])[0]
                        elif srcs and not str(srcs[-1]).startswith("blob:"):
                            direct = str(srcs[-1])
                        if direct:
                            meta = {}
                            try:
                                meta = page.evaluate(
                                    # async：直链往往比元数据先就绪 —— <video> 挂上 src
                                    # 时 duration 还是 NaN、poster 还是空（实测 API 返回
                                    # duration=0 / thumbnail 空，而单独直调有值）。
                                    # 最多等 2.5s 的 loadedmetadata，再读一次。
                                    "async () => {"
                                    "  const v = document.querySelector('video');"
                                    "  if (v && v.readyState < 1) {"
                                    "    await new Promise(r => {"
                                    "      v.addEventListener('loadedmetadata', r, {once: true});"
                                    "      setTimeout(r, 2500);"
                                    "    });"
                                    "  }"
                                    "  const c = s => { const n = document.querySelector(s);"
                                    "    return n ? (n.getAttribute('content') || '') : ''; };"
                                    "  let cover = c('meta[property=\"og:image\"]') || '';"
                                    "  if (!cover && v) cover = v.getAttribute('poster') || '';"
                                    "  if (!cover) {"
                                    # 抖音详情页的 og:image 经常是空的；封面实际来自
                                    # douyinpic.com 的 <img>。必须排掉 aweme-avatar
                                    # （那是作者头像，100x100），否则卡片上显示的是头像。
                                    "    const imgs = Array.from(document.querySelectorAll('img'))"
                                    "      .map(n => n.currentSrc || n.src || '')"
                                    "      .filter(s => s && s.indexOf('douyinpic.com') >= 0"
                                    "                     && s.indexOf('aweme-avatar') < 0);"
                                    "    cover = imgs[0] || '';"
                                    "  }"
                                    "  return {"
                                    "    title: c('meta[property=\"og:title\"]') || document.title || '',"
                                    "    cover: cover,"
                                    "    duration: (v && isFinite(v.duration)) ? Math.round(v.duration) : 0,"
                                    "  }; }") or {}
                            except Exception:  # noqa: BLE001
                                meta = {}
                            title = str(meta.get("title") or "").strip()
                            # 封面兜底：DOM 里没渲染出来时，用网络层捡到的 CDN 封面图。
                            cover = str(meta.get("cover") or "") or (cover_urls[0] if cover_urls else "")
                            # 抖音的 og:title 形如「作品标题 - 抖音」，后缀没用还占宽度
                            for suffix in (" - 抖音", "-抖音", "_抖音"):
                                if title.endswith(suffix):
                                    title = title[: -len(suffix)].strip()
                            return {"ok": True, "direct_url": direct,
                                    "title": title,
                                    "cover": cover,
                                    "duration": int(meta.get("duration") or 0),
                                    "error": ""}
                        # 还没挂上地址：补一次播放（有的播放器要用户手势才加载）
                        try:
                            page.evaluate(
                                "() => { const v = document.querySelector('video');"
                                " if (v) { v.muted = true; v.play().catch(()=>{}); } }")
                        except Exception:  # noqa: BLE001
                            pass
                        time.sleep(2.5)
                    return {"ok": False, "direct_url": "", "title": "", "cover": "",
                            "duration": 0,
                            "error": "这个视频页没有吐出可下载的媒体地址；"
                                     "可能是登录态过期或站点要求验证，请在「平台登录」"
                                     "重新登录抖音后重试。"}
                finally:
                    try:
                        ctx.close()
                    except Exception:  # noqa: BLE001
                        pass
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "direct_url": "", "title": "", "cover": "",
                    "duration": 0, "error": f"{type(e).__name__}: {e}"}


def _douyin_real_url_profiled(url: str, timeout: int = 50) -> str:
    """只要正片直链（下载侧沿用这个签名）。"""
    return str(_douyin_snapshot(url, timeout).get("direct_url") or "")


def douyin_item_probe(url: str, timeout: int = 50) -> dict:
    """抖音单条视频的「解析清晰度」入口：渲染拿直链，包成 probe 结果。

    为什么不再让 yt-dlp 解析抖音（2026-11-07 用户报错）：
      yt-dlp 的 DouyinIE 直接请求 /aweme/v1/web/aweme/detail/，该接口现在要
      a_bogus 签名（页面 JS 生成）。拿到空响应时它抛
      `Fresh cookies (not necessarily logged in) are needed` —— 与登录态无关，
      换成新导出的 cookies 也一样。同一份登录态走渲染能拿到 zjcdn 直链。
    """
    snap = _douyin_snapshot(url, timeout)
    if not snap.get("ok"):
        return {"ok": False, "error": snap.get("error") or "抖音解析失败", "url": url}
    item = {
        "direct_url": snap.get("direct_url") or "",
        "title": snap.get("title") or "",
        "cover": snap.get("cover") or "",
        "duration": snap.get("duration") or 0,
        "uploader": "",
    }
    return {"ok": True, "item": item, "source": "profiled", "url": url}
    return ""


def resolve_media_via_render(page_url: str, *, timeout: int = 40,
                             profile_dir: str | None = None) -> str:
    """给 yt-dlp 不认识的站解析真实媒体地址（渲染页面 + 监听网络请求）。

    薄包装：完整结果见 `resolve_media_render_full`（含「txt 分片流」）。
    """
    res = resolve_media_render_full(page_url, timeout=timeout, profile_dir=profile_dir)
    u = str(res.get("url") or "")
    return u if res.get("kind") in ("direct", "both") else ""


def resolve_media_render_full(page_url: str, *, timeout: int = 40,
                              profile_dir: str | None = None) -> dict:
    """渲染解析的完整结果：

    - `{"kind": "direct", "url": u}`   单直链（多数站）
    - `{"kind": "both", "url": u, "ts": {...}}`  同时有直链与分片流（分片优先）
    - `{"kind": "ts", "ts": {...}}`    只有分片流
    - `{"kind": "none"}`               解析失败

    「txt 分片流」是什么（2026-10-30 hornyleak.tv 实测）：这类播放器把
    MPEG-TS 正片切成 `480p_0.txt`、`480p_1.txt`… 递增编号的**伪 txt**，
    内容就是 TS 段（0x47 同步字节开头，FFmpeg 封装），按序下载拼接即可播放。
    判据全部通用：同目录 + 递增编号 + ≥2 片 + 首片 0x47 魔数，无任何站点名单。
    正片只能这样拿 —— 详情页 HTML 里能看到的 mp4 只有 728x90 广告位和
    400~700KB 的预览片段（preview.mp4，实测 6 部片全是），而正片在
    「embed iframe → 第三方播放器 → JS 解密分片列表」三层嵌套的最里层。
    """
    try:
        from playwright.sync_api import sync_playwright
        from . import browser_runtime
        from .config import DEFAULT_UA
    except Exception:  # noqa: BLE001
        return {"kind": "none"}
    fetched: list = []
    # 伪 txt 分片：键 = (目录指纹, 文件名前缀)，值 = {编号: [候选完整URL]}
    seg_map: dict = {}

    def _on_resp(resp):
        try:
            u = str(resp.url or "")
            low = u.lower()
            ct = str((resp.headers or {}).get("content-type") or "").lower()
            # 只按 .mp4/.m3u8 后缀判会漏掉一大类：实测 hornyleak 的正片在
            # https://hdplayer.gives/upload/videos/2026/09/xxx_vid —— 没有扩展名、
            # content-type 是 application/octet-stream。所以这里"宽进"：
            # 后缀、video/*、octet-stream、/videos/ 路径、常见播放域名都收。
            is_media = (
                any(x in low.split("?")[0] for x in (".mp4", ".m3u8", ".webm", ".mov", ".flv"))
                or ct.startswith("video/")
                or ct.startswith("application/octet-stream")
                or "/upload/videos/" in low
            )
            # 注意不要把播放器**页面**当成媒体：hdplayer.gives/embed/xxx 是 HTML。
            # 也别把同目录的 thumbs.vtt / poster.jpg 当视频 ——
            # hdplayer 的正片没有扩展名，所以这里按"排除法"过滤非视频后缀。
            _non_video = (".vtt", ".srt", ".ass", ".jpg", ".jpeg", ".png", ".webp",
                          ".gif", ".json", ".css", ".js", ".html", ".xml", ".txt")
            # 抖音的**静态资源域名**里有 uuu_265.mp4 这类 UI 占位视频
            # （lf-douyin-pc-web.douyinstatic.com/obj/douyin-pc-web/uuu_265.mp4），
            # content-type 同样是 video/mp4，会被当成正片收进来 ——
            # 结果所有抖音视频都解析到同一个文件，用户下到的全是一模一样的
            # 「uuu_265」（2026-10-25 实测）。正片在 douyinvod.com，这里排掉静态域。
            if "douyinstatic.com" in low or "/obj/douyin-pc-web/" in low:
                return
            if is_media and "/embed/" not in low \
                    and not low.split("?")[0].endswith(_non_video):
                fetched.append(u)
            # ---- 伪 txt 分片（宽进）：判定放到收尾（同目录 ≥2 片才算数）----
            # 单凭 URL 判不了「分片」还是「说明文件」，但同目录下出现
            # `前缀_0.txt / 前缀_1.txt / …` 递增编号的，几乎只会是分片流。
            # 分组 key 用**纯路径**（不含域）—— 播放器会把相邻分片分散到不同
            # CDN 域（hornyleak 实测 0~5 片分属 swa3~swa9 六个域），按完整
            # URL 分组会把一部片拆成 6 个"每组 1 片"的假组。
            if resp.status == 200 and low.split("?")[0].endswith(".txt"):
                m = re.match(r"^(https?://[^/]+(/.*/))([^/]*?)_(\d{1,4})\.txt$", u)
                if m and int(m.group(4)) < 9000:
                    key = (m.group(2), m.group(3))
                    seg_map.setdefault(key, {}).setdefault(
                        int(m.group(4)), []).append(u)
        except Exception:  # noqa: BLE001
            pass

    def _score(u: str) -> int:
        """给候选打分：越像"正片"分越高。

        只用通用特征，不为任何具体站点写规则：
        - 视频目录（/upload/videos/、/videos/、/original）加分
        - 站点自己的推广位目录（contents/data、creative、promo）减分
        """
        low = (u or "").lower()
        s = 0
        if any(k in low for k in ("/upload/videos/", "/videos/", "/video/")):
            s += 60
        if any(k in low for k in ("/original", "/hd/", "/source", "/media/")):
            s += 40
        if any(k in low.split("?")[0] for k in (".mp4", ".m3u8", ".webm")):
            s += 20
        if any(k in low for k in ("/contents/data/", "/creative/", "/promo/", "/banner/")):
            s -= 60
        return s

    dom_cands: list = []
    try:
        with sync_playwright() as p:
            # 需要登录态的站（抖音）必须带 platform profile 打开：
            # 不带登录态时，页面里的 <video> 只是一段占位视频
            # （douyinstatic.com/obj/douyin-pc-web/uuu_265.mp4），
            # 所有视频都会解析成同一个文件（2026-10-25 实测）。
            b = None
            if profile_dir:
                try:
                    from .renderer import _LAUNCH_ARGS
                    b = p.chromium.launch_persistent_context(
                        user_data_dir=str(profile_dir), headless=True,
                        locale="zh-CN", user_agent=DEFAULT_UA,
                        viewport={"width": 1400, "height": 1000},
                        args=list(_LAUNCH_ARGS), **browser_runtime.launch_kwargs())
                    page = b.pages[0] if b.pages else b.new_page()
                except Exception:  # noqa: BLE001 —— profile 打不开就退回无登录态
                    b = None
            if b is None:
                b = p.chromium.launch(headless=True, **browser_runtime.launch_kwargs())
                page = b.new_page(user_agent=DEFAULT_UA,
                                  viewport={"width": 1400, "height": 1000}, locale="zh-CN")
            page.on("response", _on_resp)
            try:
                page.goto(page_url, wait_until="domcontentloaded", timeout=timeout * 1000)
            except Exception:  # noqa: BLE001
                pass
            page.wait_for_timeout(3500)
            # 真正的播放器常独立在一个 /embed/ iframe 里：
            # hornyleak 主框架只有 728x90 广告，正片由 iframe 里的播放器提供，
            # 而那个播放器只有「直接打开 embed 页」时才会请求视频文件
            # （在父页面里点 iframe 拿不到，实测 2026-10-24）。
            embed_url = ""
            for fr_ in page.frames:
                fu = str(fr_.url or "")
                if "/embed/" in fu.lower():
                    embed_url = fu
                    break
            if embed_url:
                try:
                    page.goto(embed_url, wait_until="domcontentloaded", timeout=30000)
                    page.wait_for_timeout(3000)
                except Exception:  # noqa: BLE001
                    pass
            # 必须点一下播放：播放器不点不发媒体请求
            for sel in ("video", ".play, [class*=play], [id*=play], .vjs-big-play-button"):
                try:
                    page.click(sel, timeout=1800)
                    break
                except Exception:  # noqa: BLE001
                    continue
            page.wait_for_timeout(7000)

            # 耐心等 <video> 真正挂上地址。
            # 抖音这类页面首帧的 <video> 是空的，点完播放还要再等一会儿
            # currentSrc 才有值；固定等 7 秒常常不够，结果被当成「解析不出」
            # 直接报失败（2026-10-25 用户实测）。这里改成轮询，最多再等 18 秒。
            _dom_expr = """() => {
                        const out = [];
                        document.querySelectorAll('video').forEach(v => {
                          // 必须同时看 currentSrc：抖音这类用 MSE 播放的页面，
                          // v.src 是空、真正的地址在 v.currentSrc（2026-10-25 实测）。
                          const s = v.currentSrc || v.src;
                          if (s) out.push(s);
                          v.querySelectorAll('source').forEach(x => { if (x.src) out.push(x.src); });
                        });
                        document.querySelectorAll('[data-src],[data-video],[data-file],[data-href],[data-url]').forEach(el => {
                          ['data-src','data-video','data-file','data-href','data-url'].forEach(a => {
                            const v = el.getAttribute(a);
                            if (v && /\\.(mp4|m3u8|webm)(\\?|$)/i.test(v)) out.push(v);
                          });
                        });
                        return out;
                    }"""
            dom_cands: list = []
            _deadline = time.time() + 18
            while True:
                try:
                    dom_cands = page.evaluate(_dom_expr) or []
                except Exception:  # noqa: BLE001
                    dom_cands = []
                if dom_cands or time.time() >= _deadline:
                    break
                # 还没挂上地址：再点一次播放（有的播放器要用户手势才加载）
                try:
                    page.click("video", timeout=1200)
                except Exception:  # noqa: BLE001
                    pass
                page.wait_for_timeout(1500)
            b.close()
    except Exception:  # noqa: BLE001
        return {"kind": "none"}

    def _ok(u: str) -> str:
        u = str(u or "").strip()
        if not u.startswith("http") or _is_ad_media(u):
            return ""
        # 抖音页面的静态占位视频（douyinstatic.com/.../uuu_265.mp4，2 秒）：
        # 上面的网络监听路径已过滤该域名，DOM 候选路径必须同样过滤 ——
        # 否则无登录态时每条视频都会"成功"下成同一个占位文件（2026-10-25 实测）。
        low = u.lower()
        if "douyinstatic.com" in low or "/obj/douyin-pc-web/" in low:
            return ""
        return u

    # 网络请求优先：按「像正片的程度」排序，而不是按出现顺序 ——
    # 页面上先发的往往是推广位/预览，正片由 iframe 播放器后发。
    direct = ""
    pool = [u for u in fetched if _ok(u)]
    if pool:
        pool.sort(key=_score, reverse=True)
        direct = pool[0]
    else:
        for u in dom_cands if isinstance(dom_cands, list) else []:
            good = _ok(u)
            if good:
                direct = good
                break

    # 分片流优先：有「同目录递增编号 ≥2 片」的分片组，说明正片走的是分片协议；
    # 此时 direct 多半只是预览片段（hornyleak 的 preview.mp4 实测只有 400~700KB），
    # 拿它当结果就会下成十几秒的预览 —— 正是「下载的视频不对」的又一个来源。
    if seg_map:
        ts = {"groups": [
            {"dir": k[0], "prefix": k[1], "segments": v}
            for k, v in seg_map.items()
        ]}
        if direct:
            return {"kind": "both", "url": direct, "ts": ts}
        return {"kind": "ts", "ts": ts}
    if direct:
        return {"kind": "direct", "url": direct}
    return {"kind": "none"}


_FETCH_SEG_JS = """async (u) => {
    try {
        const r = await fetch(u, {credentials: 'omit'});
        if (!r.ok) return null;
        const buf = await r.arrayBuffer();
        const bytes = new Uint8Array(buf);
        let bin = '';
        const CH = 0x8000;
        for (let i = 0; i < bytes.length; i += CH)
            bin += String.fromCharCode.apply(null, bytes.subarray(i, i + CH));
        return btoa(bin);
    } catch (e) { return null; }
}"""

_PROBE_SEG_JS = """async (u) => {
    try { const r = await fetch(u, {credentials: 'omit'}); return r.status; }
    catch (e) { return 0; }
}"""


def _hls_seg_entry(u: str) -> tuple[tuple[str, str], int] | None:
    """把分片 URL 归成 ((目录, 前缀), 序号)。只认 <prefix>_<数字>.txt/.ts。"""
    m = re.match(r"^(https?://[^/]+(/.*/))([^/]*?)_(\d{1,4})\.(?:txt|ts)(?:\?|$)",
                 u or "", re.I)
    if not m:
        return None
    return (m.group(2), m.group(3)), int(m.group(4))


def _parse_hls_master(text: str, base: str) -> list[dict]:
    """解析 HLS master playlist 的变体列表（分辨率 / 带宽 / 子清单地址）。"""
    out: list[dict] = []
    height = bandwidth = 0
    name = ""
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-STREAM-INF"):
            height = bandwidth = 0
            name = ""
            m = re.search(r"RESOLUTION=\d+x(\d+)", line, re.I)
            if m:
                height = int(m.group(1))
            m = re.search(r"BANDWIDTH=(\d+)", line, re.I)
            if m:
                bandwidth = int(m.group(1))
            m = re.search(r'NAME="([^"]+)"', line, re.I)
            if m:
                name = m.group(1)
            continue
        if line.startswith("#"):
            continue
        if height or bandwidth:
            out.append({"height": height, "bandwidth": bandwidth,
                        "name": name, "url": urljoin(base, line)})
            height = bandwidth = 0
    return out


def _parse_hls_segments(text: str, base: str) -> list[str]:
    """解析 HLS 子清单里的分片地址（非 # 行，按顺序）。"""
    return [urljoin(base, line.strip()) for line in (text or "").splitlines()
            if line.strip() and not line.strip().startswith("#")]


def _group_height(key: tuple[str, str]) -> int:
    """从分片组 key（目录 + 前缀）里读分辨率高度，如 1080p / 720p。"""
    text = "/".join(str(x or "") for x in key)
    m = re.search(r"(?<!\d)(\d{3,4})p(?!\d)", text, re.I)
    return int(m.group(1)) if m else 0


def _choose_ts_group(seg_map: dict, prefer_quality: str = "",
                     meta: dict | None = None) -> dict | None:
    """按清晰度偏好选分片组：默认最高，支持 lowest / 具体高度。

    为什么不能按「分片数量最多」选：同一部片的 480P 分片更短、数量更多，
    按数量选恰好选到最低档（实测 hornyleak：480P 5 段、1080P 4 段）。
    meta 来自 HLS master 的变体声明，站点分片路径不带 1080p 字样时也能选对档。
    """
    items = list((seg_map or {}).items())
    if not items:
        return None
    pq = (prefer_quality or "").strip().lower()

    def _height(key):
        from_meta = int(((meta or {}).get(key) or {}).get("height") or 0)
        return from_meta or _group_height(key)

    def _rank(kv):
        h = _height(kv[0])
        n = len(kv[1])
        if pq == "lowest":
            return (h if h else 10 ** 6, n)
        if pq.isdigit():
            return (abs((h or 0) - int(pq)), -n)
        return (-h, -n)          # highest：有高度的优先，再按分片数兜底

    (dir_path, prefix), segs = min(items, key=_rank)
    return {"dir": dir_path, "prefix": prefix, "segs": segs,
            "meta": (meta or {}).get((dir_path, prefix)) or {}}


def _index_ts_playlist(txt: str, base: str, seg_map: dict,
                       group_meta: dict, variant: dict | None = None) -> int:
    """把一份 HLS 子清单登记为分片组：按清单顺序存地址、总段数与总时长。

    纯函数，可离线验收。清单是唯一权威：段数 / 时长 / 顺序都不靠播放器
    实际请求到哪一片来推断（分片按需加载时会只播前几片）。
    """
    if "#EXTM3U" not in (txt or ""):
        return 0
    seg_urls = _parse_hls_segments(txt, base)
    if not seg_urls:
        return 0
    vm = variant or {}
    durs = [float(x) for x in re.findall(r"#EXTINF:([\d.]+)", txt)]
    total_dur = sum(durs)
    for su in seg_urls:
        ent = _hls_seg_entry(su)
        if not ent:
            continue
        key, n = ent
        seg_map.setdefault(key, {}).setdefault(n, []).append(su)
        g = group_meta.setdefault(key, {
            "height": 0, "bandwidth": 0, "duration": 0.0,
            "expected": 0, "order": {}})
        g["order"].setdefault(n, su)
        # 分片路径里的 1080p 才是实际档位；master 声明只作兜底。
        g["height"] = _group_height(key) or int(vm.get("height") or 0)
        g["bandwidth"] = int(vm.get("bandwidth") or 0)
        g["duration"] = max(g["duration"], total_dur)
        g["expected"] = max(g["expected"], len(seg_urls))
    return len(seg_urls)


def _ts_plan(meta: dict, segs: dict) -> list[str]:
    """按清单顺序给出所选档位的分片地址（无清单时退回编号顺序）。"""
    order_map = dict((meta or {}).get("order") or {})
    if order_map:
        return [order_map[k] for k in sorted(order_map)]
    return [u for n in sorted(segs or {}) for u in (segs[n] or [])]


def _ts_integrity_error(ok_seg: int, expected: int, *,
                        actual_dur: float = 0.0,
                        expected_dur: float = 0.0,
                        partial: bool = False) -> str:
    """完整性闸门：段数不足或时长明显短于清单，一律判失败，不交付半部片。"""
    if ok_seg < 2:
        return "分片下载中断（只拿到 0~1 片）"
    if not partial and ok_seg < expected:
        return f"分片不完整（{ok_seg}/{expected} 段），已保留分片，重试可续传。"
    if not partial and expected_dur > 0 and actual_dur \
            and actual_dur < expected_dur * 0.9:
        return (f"下载不完整：实际 {actual_dur:.0f} 秒，"
                f"清单标注 {expected_dur:.0f} 秒；已保留分片供续传。")
    return ""


def _cleanup_parts(parts_dir: Path) -> None:
    """尽力清理临时分片：清理失败只留残留，不能把一次成功的下载判成失败。"""
    try:
        shutil.rmtree(parts_dir, ignore_errors=True)
    except BaseException:  # noqa: BLE001  某些运行环境会拦截批量删除
        pass


def _ffprobe_duration(path: Path) -> float:
    """读合成文件的实际时长，用于「下到一半却报成功」的通用闸门。"""
    from .subproc import run_external
    ff = find_ffmpeg()
    if not ff:
        return 0.0
    probe = Path(ff).with_name("ffprobe" + Path(ff).suffix)
    if probe.exists():
        probe = str(probe)
    else:
        import shutil
        probe = shutil.which("ffprobe") or ""
    if not probe:
        return 0.0
    try:
        r = run_external(
            [probe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nk=1:nw=1", str(path)],
            capture_output=True, text=True, timeout=90)
        return float((r.stdout or "").strip().splitlines()[0])
    except Exception:  # noqa: BLE001
        return 0.0


def _http_fetch_ts(url: str, referer: str, timeout: int = 35) -> bytes | None:
    """普通 HTTP 取一个 TS 分片。

    2026-11-07 实测：CDN 分片在浏览器会话之外同样返回 200（curl / requests
    均可），旧实现全部经 Playwright evaluate 回传 base64，1080P 单片 5MB 时
    CDP 传输会中断 —— 表现就是「只下了前 9 段却报成功」。保留双通道兜底：
    环境代理失败时改直连。
    """
    import requests
    from .config import DEFAULT_UA
    headers = {"User-Agent": DEFAULT_UA, "Referer": referer or ""}
    for direct in (False, True):
        try:
            if direct:
                with requests.Session() as s:
                    s.trust_env = False      # 绕开发生故障的环境代理
                    r = s.get(url, headers=headers, timeout=(8, timeout))
            else:
                r = requests.get(url, headers=headers, timeout=(8, timeout))
            if r.status_code == 200 and r.content[:1] == b"\x47":
                return r.content
        except Exception:  # noqa: BLE001
            continue
    return None


def _download_ts_via_browser(page_url: str, output_dir: Path, *,
                             title: str = "", url: str = "",
                             cancel_event=None, on_progress=None,
                             timeout: int = 40,
                             prefer_quality: str = "",
                             probe_only: bool = False,
                             max_segments: int = 0) -> dict:
    """浏览器会话一体式下载「txt 分片流」正片。

    为什么解析和下载必须在**同一个浏览器会话**里（2026-10-30 hornyleak.tv
    实测两次踩坑）：分片 CDN 校验客户端指纹 —— 播放器（Chrome）请求返回
    200，渲染窗口一关、换 requests 请求**同一个 URL** 直接 404，带不带
    Referer 都一样，curl-cffi 又不在本机环境里。所以捕获到分片地址后，
    不关浏览器，直接用 page.evaluate(fetch) 逐片把内容 base64 带回 ——
    与播放器同源同指纹，CDN 视为正常播放。

    判据全部通用（同目录 + 递增编号 + ≥2 片 + 首片 0x47 魔数），无站点名单。
    """
    import base64
    import hashlib
    import shutil

    from playwright.sync_api import sync_playwright
    from urllib.parse import urlparse as _up

    from . import browser_runtime
    from .config import DEFAULT_UA
    from .downloader import url_fingerprint as _url_fp

    fetched: list = []
    seg_map: dict = {}
    # 每个分片组的清单元数据：总段数 / 总时长 / 分辨率 / 按清单顺序的分片地址。
    # 权威来源是 HLS 子清单本身，不能靠「播放器实际播到哪一片」推断片尾。
    group_meta: dict = {}
    # HLS master / 子清单：content-type 常是 text/plain（不是 mpegurl），
    # 只能靠正文的 #EXTM3U 认。捕获后从里面解析出全部清晰度，
    # 不依赖播放器当前实际在播哪一档。
    playlists: list[tuple[str, str]] = []

    def _on_resp(resp):
        try:
            u = str(resp.url or "")
            low = u.lower()
            ct = str((resp.headers or {}).get("content-type") or "").lower()
            is_media = (
                any(x in low.split("?")[0] for x in (".mp4", ".m3u8", ".webm", ".mov", ".flv"))
                or ct.startswith("video/")
                or ct.startswith("application/octet-stream")
                or "/upload/videos/" in low
            )
            if "douyinstatic.com" in low or "/obj/douyin-pc-web/" in low:
                return
            _non_video = (".vtt", ".srt", ".ass", ".jpg", ".jpeg", ".png", ".webp",
                          ".gif", ".json", ".css", ".js", ".html", ".xml", ".txt")
            if is_media and "/embed/" not in low \
                    and not low.split("?")[0].endswith(_non_video):
                fetched.append(u)
            if resp.status == 200 and low.split("?")[0].endswith(".txt"):
                m = re.match(r"^(https?://[^/]+(/.*/))([^/]*?)_(\d{1,4})\.txt$", u)
                if m and int(m.group(4)) < 9000:
                    key = (m.group(2), m.group(3))
                    seg_map.setdefault(key, {}).setdefault(
                        int(m.group(4)), []).append(u)
            # 播放器清单（text/plain + #EXTM3U）也收下：里面才有 1080P 等全部档位。
            # 只读 2MB 以内的文本，避免把大文件正文拖进内存。
            if ct.startswith("text/plain"):
                # 伪 txt 分片是二进制 MPEG-TS，只是 CDN 标了 text/plain。
                # 不读它的正文：多浏览器并发时 resp.text() 会把整段搬进内存。
                if re.search(r"_\d{1,4}\.txt$", low.split("?")[0]):
                    return
                cl = str((resp.headers or {}).get("content-length") or "")
                if cl.isdigit() and int(cl) > 2_000_000:
                    return
                try:
                    body = resp.text()
                except Exception:  # noqa: BLE001
                    body = ""
                if body.lstrip().startswith("#EXTM3U"):
                    playlists.append((u, body))
        except Exception:  # noqa: BLE001
            pass

    def _pick_group():
        # 默认按分辨率挑最高档，而不是「分片最多」——低码率分片更短、数量更多。
        return _choose_ts_group(seg_map, prefer_quality, group_meta)

    seeded_playlists: set[str] = set()
    seeded_variants: set[str] = set()

    def _seed_from_playlists(page):
        """从捕获到的 HLS master / 子清单里补齐全部清晰度的分片地址。

        播放器默认自适应，往往只请求 480P；只靠它实际请求的分片，最高档
        永远不会出现在 seg_map 里。master 清单里写了全部变体，直接读出来
        就能拿到 1080P 的子清单与分片（同一浏览器会话里 fetch，带同一指纹）。
        """
        for pu, body in list(playlists):
            if pu in seeded_playlists:
                continue
            seeded_playlists.add(pu)
            variants = _parse_hls_master(body, pu)
            # 带上变体声明的分辨率/带宽；子清单本身没有变体时声明为空。
            targets = [(v["url"], v) for v in variants] if variants else [(pu, {})]
            for vu, vm in targets:
                if vu in seeded_variants:
                    continue
                seeded_variants.add(vu)
                txt = ""
                try:
                    txt = page.evaluate(
                        "async (u) => { const r = await fetch(u); return await r.text(); }",
                        vu)
                except Exception:  # noqa: BLE001
                    txt = ""
                if not txt:
                    # 跨域/CSP 挡住浏览器 fetch 时，用普通请求补一次。
                    # 清单本身不校验指纹（分片才校验），Referer 带上即可。
                    try:
                        import requests as _rq
                        txt = _rq.get(vu, headers={"User-Agent": DEFAULT_UA,
                                                   "Referer": str(page.url or "")},
                                      timeout=20).text
                    except Exception:  # noqa: BLE001
                        txt = ""
                _index_ts_playlist(txt, vu, seg_map, group_meta, vm)

    stem = ""
    if title:
        try:
            from .downloader import sanitize_filename as _sn
            stem = _sn(title)[:110].strip()
        except Exception:  # noqa: BLE001
            stem = ""
    if not stem:
        stem = "video"
    fp = _url_fp(url)[:8] if url else ""
    base_name = f"{stem} [{fp}]" if fp else stem
    parts_dir = output_dir / f".{hashlib.md5(url.encode('utf-8')).hexdigest()[:12]}.tsseg"
    parts_dir.mkdir(parents=True, exist_ok=True)

    def _emit(percent: float, done: int, total: int, speed: float,
              label: str):
        """统一进度上报：percent 必须给，否则界面任务条在完成前永远是 0%。

        2026-11-07 实测：旧回调只给 progressed_bytes + note，
        task_manager 的 `percent = info.get("percent") or t.percent` 拿不到值，
        于是进度条只在任务结束瞬间从 0 跳到 100。
        """
        if not on_progress:
            return
        try:
            on_progress({
                "status": "downloading",
                "percent": round(min(99.0, max(0.0, percent)), 1),
                "progressed_bytes": int(done), "total_bytes": int(total),
                "speed": float(speed), "eta": None,
                "filename": label, "note": label,
            })
        except Exception:  # noqa: BLE001  进度回调不能反过来打断下载
            pass

    _emit(0.0, 0, 0, 0.0, "正在解析播放清单…")

    try:
        with sync_playwright() as p:
            b = p.chromium.launch(headless=True, **browser_runtime.launch_kwargs())
            page = b.new_page(user_agent=DEFAULT_UA,
                              viewport={"width": 1280, "height": 800})
            page.on("response", _on_resp)

            # 播放器藏在 /embed/ iframe 里（hornyleak 是两层：站内 embed →
            # 第三方播放器 embed）。直接导航到播放器 embed 地址，播放器才会
            # 真正开始请求分片 —— 在父页面里点 iframe 是拿不到的（2026-10-24）。
            # 先用纯 HTTP 把 embed 链条剥出来（通用 iframe src 提取，最多两层），
            # 实测这比 frames 轮询稳：播放器加载时序不定，frames 等待时好时坏。
            target = page_url
            try:
                from .page_scanner import http_get_with_fallback as _hgf
                cur = page_url
                for _depth in range(2):
                    fr_, _errs = _hgf(cur, timeout=25)
                    if fr_ is None:
                        break
                    m = re.search(r'<iframe[^>]+src="(https?://[^"]*/embed/[^"]+)"',
                                  fr_.text, re.I)
                    if not m:
                        break
                    cur = m.group(1).replace("&amp;", "&")
                    target = cur
            except Exception:  # noqa: BLE001
                pass
            try:
                page.goto(target, wait_until="domcontentloaded",
                          timeout=timeout * 1000)
            except Exception:  # noqa: BLE001
                pass
            page.wait_for_timeout(4000)
            # 播放器已请求 master 清单：先解析出全部清晰度，避免只拿到 480P
            _seed_from_playlists(page)

            # HTTP 剥链失败时兜底：从 frames 里找 /embed/ 逐层导航
            for _round in range(2):
                grp0 = _pick_group()
                if grp0 and len(grp0.get("segs") or {}) >= 1:
                    break
                if cancel_event is not None and cancel_event.is_set():
                    b.close()
                    return {"ok": False, "error": "已取消"}
                embed = ""
                for fr in page.frames:
                    fu = str(fr.url or "")
                    if "/embed/" in fu.lower() and fu != str(page.url):
                        embed = fu
                        break
                if not embed:
                    break
                try:
                    page.goto(embed, wait_until="domcontentloaded", timeout=30000)
                    page.wait_for_timeout(3500)
                except Exception:  # noqa: BLE001
                    pass

            for sel in ("video", ".play, [class*=play], [id*=play], .vjs-big-play-button",
                        "div[class*=player]"):
                try:
                    page.click(sel, timeout=1500)
                    break
                except Exception:  # noqa: BLE001
                    continue

            deadline = time.time() + 20
            while time.time() < deadline:
                if cancel_event is not None and cancel_event.is_set():
                    b.close()
                    return {"ok": False, "error": "已取消"}
                _seed_from_playlists(page)   # 后续才到的清单也补进来
                grp = _pick_group()
                if grp and len(grp.get("segs") or {}) >= 2:
                    break
                try:
                    page.wait_for_timeout(1000)
                except Exception:  # noqa: BLE001
                    break
            grp = _pick_group()
            if not grp or len(grp.get("segs") or {}) < 2:
                b.close()
                return {"ok": False, "error": "未在播放器页捕获到分片流"}

            dir_path, prefix, segs = grp["dir"], grp["prefix"], grp["segs"]
            meta = grp.get("meta") or {}
            if probe_only:
                # 验收/诊断用：只回报选中的档位，不下载（避免为测 1080P 拉整部片）
                b.close()
                return {"ok": True, "probe_only": True, "prefix": prefix,
                        "height": int(meta.get("height") or 0)
                        or _group_height((dir_path, prefix)),
                        "segments": len(segs),
                        "expected": int(meta.get("expected") or len(segs)),
                        "duration": float(meta.get("duration") or 0),
                        "groups": sorted({k[1] for k in seg_map})}

            # 清单顺序即拼接顺序；不再用「取到 404 就是片尾」的试探式循环。
            # 实测 2026-11-07：Playwright evaluate 回传 5MB 的 1080P 分片会中断，
            # 旧循环在第 9 段 break，65.8 秒的片被当成全片交付（片源 911.8 秒）。
            ordered = _ts_plan(meta, segs)
            if max_segments:
                ordered = ordered[:max_segments]
            expected = len(ordered)
            if expected < 2:
                b.close()
                return {"ok": False, "error": "清单里没有可下载的分片"}

            q_label = (f"{int(meta.get('height') or 0)}P"
                       if int(meta.get('height') or 0) else str(prefix))
            _emit(0.5, 0, 0, 0.0, f"已选 {q_label} · 共 {expected} 段")
            hosts: list[str] = []
            for u in ordered:
                p_ = _up(u)
                h = f"{p_.scheme}://{p_.netloc}"
                if h not in hosts:
                    hosts.append(h)

            def _candidates(i: int) -> list[str]:
                u = ordered[i]
                out = [u]
                ent = _hls_seg_entry(u)
                if ent:
                    out.extend(segs.get(ent[1]) or [])
                p_ = _up(u)
                for h in hosts:                       # 同一片在相邻 CDN 域都有副本
                    out.append(f"{h}{p_.path}" + (f"?{p_.query}" if p_.query else ""))
                return list(dict.fromkeys(out))

            referer = str(page.url or "") or page_url

            def _grab(i: int) -> tuple[int, int]:
                pf = parts_dir / f"seg_{i:05d}.ts"
                if pf.exists() and pf.stat().st_size > 0:
                    return i, pf.stat().st_size      # 断点续传：已落盘的不重下
                for cu in _candidates(i):
                    body = _http_fetch_ts(cu, referer)
                    if not body:
                        continue
                    tmp = pf.with_suffix(".dl")
                    tmp.write_bytes(body)
                    tmp.replace(pf)
                    return i, len(body)
                return i, 0

            from concurrent.futures import ThreadPoolExecutor

            missing: list[int] = []
            done_bytes = 0
            t_start = time.time()
            t_last = t_start
            with ThreadPoolExecutor(max_workers=6) as ex:
                # ex.map 按输入序返回结果，所以 i 单调递增，可直接算完成百分比。
                for i, sz in ex.map(_grab, range(expected)):
                    if cancel_event is not None and cancel_event.is_set():
                        b.close()
                        return {"ok": False, "error": "已取消"}
                    done_bytes += sz
                    if not sz:
                        missing.append(i)
                    now = time.time()
                    # 最后一片必须上报，否则短片源的进度会永远停在 0.4 秒节流前
                    if now - t_last > 0.4 or i + 1 == expected:
                        # 用累计平均速度，不用「与上一条回调的差值」：
                        # 最后一片强制上报时两次回调可能只隔几毫秒，瞬时差速
                        # 会算出几 GB/s 的假数字（2026-11-07 实测 4.8GB/s）。
                        speed = done_bytes / max(now - t_start, 0.2)
                        t_last = now
                        _emit((i + 1) * 100.0 / expected, done_bytes,
                              int(done_bytes * expected / max(i + 1, 1)),
                              speed, f"{q_label} · 分片 {i + 1}/{expected}")

            # 部分 CDN 只认播放器会话指纹：HTTP 失败的片用同一浏览器会话补。
            if missing:
                for i in list(missing):
                    for cu in _candidates(i):
                        try:
                            b64 = page.evaluate(_FETCH_SEG_JS, cu)
                        except Exception:  # noqa: BLE001
                            continue
                        if not b64 or b64.startswith("ERR:"):
                            continue
                        body = base64.b64decode(b64)
                        if body[:1] != b"\x47":
                            continue
                        pf = parts_dir / f"seg_{i:05d}.ts"
                        tmp = pf.with_suffix(".dl")
                        tmp.write_bytes(body)
                        tmp.replace(pf)
                        done_bytes += len(body)
                        missing.remove(i)
                        break
            if missing:                               # 再给一轮 HTTP（换时刻/换域）
                with ThreadPoolExecutor(max_workers=4) as ex:
                    for i, sz in ex.map(_grab, missing):
                        if sz:
                            done_bytes += sz
                missing = [i for i in missing
                           if not (parts_dir / f"seg_{i:05d}.ts").exists()]
            b.close()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:100]}"}

    # 拼接 + ffmpeg 转封装可能耗时数秒，明确给一个「合并中」的终态前进度，
    # 不让任务条在最后一段下载完后突然失去反馈。
    _emit(99.0, done_bytes, max(done_bytes, 1), 0.0, "正在合并分片并转封装…")
    ok_seg = sum(1 for i in range(expected)
                 if (parts_dir / f"seg_{i:05d}.ts").exists()
                 and (parts_dir / f"seg_{i:05d}.ts").stat().st_size > 0)
    # 完整性闸门：清单说有 N 段就必须拿到 N 段。缺一片就报失败、保留分片供续传，
    # 绝不把半部片写成 mp4 并标记成功（hornyleak 实测 9/115 段的旧行为）。
    gate = _ts_integrity_error(ok_seg, expected, partial=bool(max_segments))
    if gate:
        return {"ok": False, "truncated": True, "error": gate}

    joined = parts_dir / "full.ts"
    with open(joined, "wb") as out:
        for i in range(expected):
            pf = parts_dir / f"seg_{i:05d}.ts"
            if pf.exists():
                out.write(pf.read_bytes())

    final = output_dir / f"{base_name}.mp4"
    ff = find_ffmpeg()
    converted = False
    if ff:
        from .subproc import run_external
        try:
            r = run_external(
                [ff, "-hide_banner", "-loglevel", "error", "-y",
                 "-i", str(joined), "-c", "copy", "-bsf:a", "aac_adtstoasc",
                 str(final)],
                capture_output=True, text=True, timeout=900)
            converted = (r.returncode == 0 and final.exists()
                         and final.stat().st_size > 0)
        except Exception:  # noqa: BLE001
            converted = False

    out_path = final if converted else output_dir / f"{base_name}.ts"
    if not converted:
        joined.replace(out_path)

    # 第二道闸门：用实际时长对账清单 EXTINF 总和。任何站点只要出现「小样 /
    # 截断」，这里就拦下，避免旧文件 + skip_existing 把错误固化下来。
    expected_dur = float(meta.get("duration") or 0)
    actual_dur = _ffprobe_duration(out_path) if expected_dur > 0 else 0.0
    gate = _ts_integrity_error(ok_seg, expected, actual_dur=actual_dur,
                               expected_dur=expected_dur,
                               partial=bool(max_segments))
    if gate:
        try:
            out_path.unlink(missing_ok=True)
        except OSError:
            pass
        return {"ok": False, "truncated": True, "error": gate}

    size = out_path.stat().st_size
    _cleanup_parts(parts_dir)
    height = int(meta.get("height") or 0)
    label = f"{height}P" if height else prefix
    return {"ok": True, "path": str(out_path), "size": size,
            "height": height, "segments": ok_seg,
            "note": (f"该站正片为分片流（{label}，{ok_seg} 段"
                     + (f" / {expected_dur:.0f} 秒" if expected_dur else "")
                     + ("），已拼接并转封装为 MP4。" if converted
                        else "），已按序拼接交付 .ts（未转封装 MP4）。"))}


def _download_ts_stream(ts_info: dict, output_dir: Path, *,
                        title: str = "", url: str = "",
                        cancel_event=None, on_progress=None) -> dict:
    """下载「伪 txt 分片流」（MPEG-TS 段按编号递增的播放器协议）并转封装 mp4。

    判据与流程全部通用（2026-10-30 hornyleak.tv 实测打通）：
    1. 分片 = 播放器页捕获到的 `…/{前缀}_{n}.txt`，内容是 TS 段（首字节 0x47）；
    2. 总片数播放器不会一次给全（按需加载），用「指数步进 + 二分」探到 404 为止；
    3. 分片可能分布在多个 CDN 域（实测 swa3~swa9 轮换），但同一片在任一域都在 ——
       下载时逐域轮询兜底；
    4. 每片落盘到 .parts 目录（天然断点续传），全部完成后按序拼接，
       ffmpeg `-c copy -bsf:a aac_adtstoasc` 转封装 mp4（无损、秒级）。
    """
    import hashlib
    import shutil

    import requests

    from .config import DEFAULT_UA
    from .downloader import url_fingerprint as _url_fp
    from .page_scanner import _http_sessions as _fallback_sessions

    from urllib.parse import urlparse as _up

    groups = (ts_info or {}).get("groups") or []
    if not groups:
        return {"ok": False, "error": "没有可用的分片组"}
    grp = max(groups, key=lambda g: len(g.get("segments") or {}))
    segs: dict = grp.get("segments") or {}
    dir_url = str(grp.get("dir") or "")
    prefix = str(grp.get("prefix") or "")
    if not dir_url or not segs:
        return {"ok": False, "error": "分片组信息不完整"}

    # 域名候选：捕获到的分片域 + 播放器页自身的域
    hosts: list[str] = []
    for urls in segs.values():
        for u in urls:
            p = _up(u)
            if p.scheme and p.netloc:
                host = f"{p.scheme}://{p.netloc}"
                if host not in hosts:
                    hosts.append(host)
    p0 = _up(url or "")
    if p0.scheme and p0.netloc:
        host = f"{p0.scheme}://{p0.netloc}"
        if host not in hosts:
            hosts.append(host)
    if not hosts:
        return {"ok": False, "error": "分片没有可用域名"}

    def _seg_url(n: int, host: str) -> str:
        return f"{host}{dir_url}{prefix}_{n}.txt"

    def _ts_sessions():
        """分片下载用：代理与直连两条会话（page_scanner 同款兜底策略）。"""
        env, direct = _fallback_sessions()
        return (direct, env)      # 分片 CDN 直连实测稳定，直连优先

    def _fetch(n: int) -> bytes | None:
        for host in hosts:
            if cancel_event is not None and cancel_event.is_set():
                return None
            u = _seg_url(n, host)
            # 代理与直连互为兜底（AGENTS 工程纪律）：首次实现走的是全局
            # requests，被系统代理偶发故障整批打挂 —— 表现就是「分片 0/6
            # 下载失败（所有候选域均不可用）」，而手工直连测试全是 200。
            for sess in _ts_sessions():
                if cancel_event is not None and cancel_event.is_set():
                    return None
                try:
                    r = sess.get(u, timeout=(6, 40),
                                 headers={"User-Agent": DEFAULT_UA,
                                          "Referer": referer or ""})
                    if r.status_code == 200 and r.content:
                        return r.content
                except Exception:  # noqa: BLE001
                    continue
        return None

    # 探测总数：捕获到的最大编号之后继续指数步进 + 二分（播放器按需加载，
    # 渲染那 40 秒只够它预取前几片，真实片数要自己探）
    known_max = max(segs)
    if exists := _fetch(known_max + 1):
        lo, hi = known_max + 1, known_max + 2
        while _fetch(hi) is not None:
            lo = hi
            hi *= 2
            if hi > 9000:
                break
        while lo + 1 < hi:
            mid = (lo + hi) // 2
            if _fetch(mid) is not None:
                lo = mid
            else:
                hi = mid
        total = hi          # 编号 0..hi-1
    else:
        total = known_max + 1

    stem = ""
    if title:
        try:
            from .downloader import sanitize_filename as _sn
            stem = _sn(title)[:110].strip()
        except Exception:  # noqa: BLE001
            stem = ""
    if not stem:
        stem = "video"
    fp = _url_fp(url)[:8] if url else ""
    base_name = f"{stem} [{fp}]" if fp else stem

    parts_dir = output_dir / f".{hashlib.md5(url.encode('utf-8')).hexdigest()[:12]}.tsseg"
    parts_dir.mkdir(parents=True, exist_ok=True)

    done_bytes = 0
    t_last = time.time()
    b_last = 0
    ok_seg = 0
    try:
        for n in range(total):
            if cancel_event is not None and cancel_event.is_set():
                return {"ok": False, "error": "已取消"}
            pf = parts_dir / f"seg_{n:05d}.ts"
            if pf.exists() and pf.stat().st_size > 0:
                body = pf.read_bytes()
            else:
                body = _fetch(n)
                if body is None:
                    return {"ok": False,
                            "error": f"分片 {n}/{total} 下载失败（所有候选域均不可用）"}
                if n == 0 and body[:1] != b"\x47":
                    # 首片不是 TS：判据不成立（可能是字幕/说明文件），不硬拼
                    return {"ok": False, "error": "分片内容不是 MPEG-TS（首字节不是 0x47）"}
                tmp = pf.with_suffix(".dl")
                tmp.write_bytes(body)
                tmp.replace(pf)
            done_bytes += len(body)
            ok_seg += 1
            if on_progress:
                now = time.time()
                if now - t_last > 0.4:
                    speed = max(0.0, (done_bytes - b_last) / (now - t_last))
                    t_last, b_last = now, done_bytes
                    on_progress({"status": "downloading",
                                 "progressed_bytes": done_bytes,
                                 "total_bytes": int(done_bytes * total / max(ok_seg, 1)),
                                 "percent": round(ok_seg * 100.0 / total, 1),
                                 "speed": speed})
    finally:
        pass

    joined = parts_dir / "full.ts"
    with open(joined, "wb") as out:
        for n in range(ok_seg):
            pf = parts_dir / f"seg_{n:05d}.ts"
            if pf.exists():
                out.write(pf.read_bytes())

    # 转封装：TS → MP4（-c copy 无损；aac_adtstoasc 是 TS 里 AAC 裸流进 MP4 的标准过滤）
    final = output_dir / f"{base_name}.mp4"
    ff = find_ffmpeg()
    if ff:
        from .subproc import run_external
        try:
            r = run_external(
                [ff, "-hide_banner", "-loglevel", "error", "-y",
                 "-i", str(joined), "-c", "copy", "-bsf:a", "aac_adtstoasc",
                 str(final)],
                capture_output=True, text=True, timeout=600)
            if r.returncode != 0 or not final.exists() or final.stat().st_size == 0:
                final = Path("")
        except Exception:  # noqa: BLE001
            final = Path("")
    else:
        final = Path("")

    if not final:
        # 转封装失败/无 ffmpeg：交付 .ts（主流播放器都能播），说明里写清楚
        target = output_dir / f"{base_name}.ts"
        joined.replace(target)
        _cleanup_parts(parts_dir)
        return {"ok": True, "path": str(target), "size": target.stat().st_size,
                "note": "该站正片是 MPEG-TS 分片流，本机未转封装成 MP4（无 ffmpeg 或转封装失败），已交付 .ts 原始流。"}

    size = final.stat().st_size
    _cleanup_parts(parts_dir)
    return {"ok": True, "path": str(final), "size": size, "note": "分段流已转封装为 MP4"}


def expand_video_list(
    url: str,
    *,
    limit: int = 60,
    cookies_file: str | None = None,
    cookies_from_browser: str | None = None,
    max_attempts: int = 2,
    deadline: float = 40.0,
    is_sub: bool = False,
) -> dict:
    """把一个「视频列表页 / 博主页」展开成视频条目列表（yt-dlp 扁平模式）。

    为什么必须有它（2026-09-22 用户反馈的重大 bug）：
      TikTok / YouTube / B 站这类平台的**博主页**里，视频地址不在页面 HTML 或
      渲染后的 DOM 里 —— 前端是调接口拿 JSON 再渲染的，通用网页提取器在主页上
      只能抓到站点自己的 JS 资源，于是「粘贴主页抓视频」永远返回 0 条。
      实测：https://www.tiktok.com/@kvi.lighting → 通用抓取 0 条 / 本函数 20 条。

    这是「交给通用工具」而不是「为站点写规则」：yt-dlp 自带这些平台的**列表**
    提取器（tiktok:user、youtube:tab、bilibili:space…），我们只是调用它。
    扁平模式（extract_flat）只取列表、不解析每个视频的格式，几秒就能返回。

    只接受**真列表**（≥2 条）。单条视频页不在这里处理 —— 那属于
    「粘贴单个视频链接 → 解析清晰度」，那里能选清晰度与字幕，比在这里返回
    一条地址更有用。返回 ok=False 时调用方应回退到通用网页抓取。

    返回：{ok, is_list, entries, error, extractor, title}
    """
    n = max(1, min(int(limit or 60), 300))
    # cookies 的单一来源：显式传入优先，否则用「设置」里配好的那份
    # （与网页抓取/渲染共用同一份，避免「配了 cookies 但这条路径没用上」）
    if not cookies_file and not cookies_from_browser:
        from . import cookie_store
        cookies_file = cookie_store.cookie_file() or None
    opts = _base_ydl_opts(
        None,
        include_playlist=True,          # 允许把「主页/合集」当列表展开
        cookies_file=cookies_file,
        cookies_from_browser=cookies_from_browser,
    )
    opts.update({
        # 只看列表，不解析单条视频的格式：主页可能上百条，逐条解析既慢又容易触发风控
        "extract_flat": "in_playlist",
        "playlistend": n,
        "skip_download": True,
        # 列表页是「取一批条目」，不宜按「一个视频」的耐心去等
        "socket_timeout": 20,
        "retries": 2,
    })

    def _fe(code: str, err: str = "") -> dict:
        return {"ok": False, "is_list": False, "entries": [], "error": err,
                "extractor": "", "title": ""}

    last_err = ""
    info = None
    for attempt in range(1, max(1, max_attempts) + 1):
        try:
            def _do_extract():
                with yt_dlp.YoutubeDL(opts) as ydl:
                    return ydl.extract_info(url, download=False)

            info, why = _run_with_deadline(_do_extract, deadline)
            if why == "timeout":
                return _fe("timeout", f"展开超时（超过 {int(deadline)} 秒）："
                                      "该列表条目太多或站点响应慢，已放弃这一步、"
                                      "改用网页抓取继续。")
            break
        except yt_dlp.utils.DownloadError as e:
            last_err = str(e)
            # 不支持的地址 / 需要登录这类错误重试没有意义
            if not _is_transient_error(last_err):
                return _fe("fail", _expand_error_hint(last_err))
            if attempt < max_attempts:
                time.sleep(1.0 * attempt)
        except Exception as e:  # noqa: BLE001
            return _fe("fail", f"{type(e).__name__}: {str(e)[:200]}")

    if info is None:
        return _fe("fail", _expand_error_hint(last_err))

    entries = [e for e in (info.get("entries") or []) if e]
    # 只认「真的是视频」的条目：扁平模式下条目分两种形态，yt-dlp 自己标了
    #   _type = "url"      → 一条视频（TikTok 用户页实测如此）
    #   _type = "playlist" → 一个**分栏/子列表**，不是视频本身
    #                        （YouTube 频道页实测得到 Videos / Shorts 两个分栏，
    #                         它们带 id 但不是视频，旧判据「有 id 就算视频」会误收）
    vids = [e for e in entries if str(e.get("_type") or "") != "playlist"]
    out = [_summarize_entry(e, i) for i, e in enumerate(vids[:n])]
    real = [e for e in out if e.get("webpage_url")]

    if len(real) < 2 and not is_sub:
        # 分栏形态：往下一层展开（只下一层，避免无限递归）。
        # 通用做法 —— 分栏地址是 yt-dlp 自己给出的，不是我们猜的规则。
        sub_url = ""
        for e in entries:
            if str((e or {}).get("_type") or "") != "playlist":
                continue
            u = str((e or {}).get("url") or (e or {}).get("webpage_url") or "")
            if u.startswith("http"):
                sub_url = u
                break
        if sub_url and sub_url != url:
            sub = expand_video_list(
                sub_url, limit=n, cookies_file=cookies_file,
                cookies_from_browser=cookies_from_browser,
                max_attempts=2, is_sub=True, deadline=deadline,
            )
            if sub.get("is_list"):
                sub["title"] = sub.get("title") or _clean_title(info.get("title") or "")
                return sub

    return {
        "ok": True,
        "is_list": len(real) >= 2,
        "entries": real,
        "error": "",
        "extractor": str(info.get("extractor") or ""),
        "title": _clean_title(info.get("title") or ""),
    }


def _clean_title(value: str) -> str:
    """去掉搜索结果标题里的高亮标记（B 站会返回 <em class="keyword"> 包裹）。"""
    s = value or ""
    s = re.sub(r"</?em[^>]*>", "", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    return s.strip()


def _pick_thumbnail(entry: dict) -> str:
    """取封面地址：兼容 thumbnail 单字段与 thumbnails 列表两种形式。"""
    th = entry.get("thumbnail")
    if isinstance(th, str) and th:
        return th
    ths = entry.get("thumbnails")
    if isinstance(ths, list):
        for item in reversed(ths):  # 列表通常按清晰度升序，取最后一个较大的
            if isinstance(item, dict):
                u = item.get("url")
                if isinstance(u, str) and u:
                    return u
            elif isinstance(item, str) and item:
                return item
    return ""


def _site_of(url: str) -> str:
    """从页面地址取来源域名（去掉 www.），用于在结果里标注「这条来自哪儿」。"""
    try:
        from urllib.parse import urlparse

        host = (urlparse(url or "").hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def _summarize_entry(entry: dict, index: int) -> dict:
    """汇总一个视频条目。不同站点/扁平模式下字段名不统一，逐项回退。"""
    title = _clean_title(
        entry.get("title")
        or entry.get("fulltitle")
        or entry.get("description")
        or ""
    )
    url = (
        entry.get("webpage_url")
        or entry.get("original_url")
        or entry.get("url")
        or ""
    )
    # 扁平模式下 B 站等只给 id，需要拼出播放页地址
    if not url or not str(url).startswith("http"):
        vid = entry.get("id") or ""
        if vid:
            url = f"https://www.bilibili.com/video/{vid}" if str(vid).startswith(("BV", "av")) else str(vid)

    duration = entry.get("duration")
    if not duration:
        # 部分站点用 duration_string / 秒数放在其它字段
        ds = entry.get("duration_string") or ""
        m = re.match(r"(?:(\d+):)?(\d{1,2}):(\d{2})", ds)
        if m:
            h, mi, sec = (int(m.group(1) or 0), int(m.group(2)), int(m.group(3)))
            duration = h * 3600 + mi * 60 + sec

    return {
        "index": index,
        "id": entry.get("id") or "",
        "title": title,
        "duration": duration,
        "duration_string": entry.get("duration_string") or _format_duration(duration),
        "webpage_url": url,
        "thumbnail": _pick_thumbnail(entry),
        "uploader": entry.get("uploader") or entry.get("channel") or entry.get("author") or "",
        # 前端的结果列表会把「来源站点」显示出来（YouTube / Bilibili / 某站 …），
        # 少了它用户不知道这条是哪儿来的，也就没法判断值不值得点。
        "source_site": _site_of(url),
        "view_count": entry.get("view_count"),
    }


def _format_duration(sec) -> str:
    """秒数转 HH:MM:SS / MM:SS。"""
    try:
        s = int(sec)
    except (TypeError, ValueError):
        return ""
    if s <= 0:
        return ""
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _summarize_info(info: dict) -> dict:
    formats_raw = info.get("formats") or []
    seen: set[str] = set()
    formats: list[dict] = []
    heights: set[int] = set()

    for f in formats_raw:
        fid = str(f.get("format_id") or "")
        if not fid or fid in seen:
            continue
        h = f.get("height")
        w = f.get("width")
        vcodec = f.get("vcodec") or "none"
        acodec = f.get("acodec") or "none"
        has_video = vcodec not in ("none", "", None)
        has_audio = acodec not in ("none", "", None)
        # 有些站点的提取器（实测 pornhub）返回的格式只有 height，没有
        # vcodec/acodec/width 元数据。若严格按 vcodec 判定，这些格式会被当成
        # "非视频"，导致清晰度下拉框缺少对应档位、前端"用此格式"选中一个
        # 不存在的值而静默失效（用户点了 240p 实际仍下载最佳画质）。
        # 高度存在但没有编解码信息时，按视频处理（音频格式不会有 height）。
        if not has_video and h:
            has_video = True
        if has_video and h:
            heights.add(int(h))
        seen.add(fid)
        formats.append(
            {
                "format_id": fid,
                "ext": f.get("ext") or "",
                "width": w,
                "height": h,
                "fps": f.get("fps"),
                "vcodec": vcodec if has_video else None,
                "acodec": acodec if has_audio else None,
                "has_video": has_video,
                "has_audio": has_audio,
                "filesize": f.get("filesize") or f.get("filesize_approx"),
                "tbr": f.get("tbr"),
                "format_note": f.get("format_note") or "",
                "quality_label": _quality_label(f),
            }
        )

    # 清晰度档位（供前端下拉）：从高到低
    preset_options = [
        {"value": str(h), "label": f"{h}p", "height": h}
        for h in sorted((x for x in heights if x), reverse=True)
    ]

    return {
        "id": info.get("id") or "",
        "title": info.get("title") or "",
        "duration": info.get("duration"),
        "duration_string": info.get("duration_string") or "",
        "uploader": info.get("uploader") or info.get("channel") or "",
        "upload_date": info.get("upload_date") or "",
        "view_count": info.get("view_count"),
        "webpage_url": info.get("webpage_url") or info.get("original_url") or "",
        "thumbnail": info.get("thumbnail") or "",
        "extractor": info.get("extractor_key") or info.get("extractor") or "",
        "description": (info.get("description") or "")[:400],
        "formats": formats,
        "quality_options": preset_options,
        "is_live": bool(info.get("is_live")),
        "ffmpeg": ffmpeg_available(),
    }


def _quality_label(f: dict) -> str:
    """给单个格式生成人读的标签，如 1080p / mp4 / 视频流 / 音频流。"""
    parts = []
    h = f.get("height")
    if h:
        fps = f.get("fps")
        parts.append(f"{int(h)}p" + (f"{int(fps)}" if fps and fps > 30 else ""))
    elif f.get("vcodec") not in ("none", None, ""):
        parts.append("视频流")
    if f.get("acodec") not in ("none", None, ""):
        abr = f.get("abr")
        parts.append(f"音频 {int(abr)}k" if abr else "含音轨")
    if f.get("ext"):
        parts.append(f["ext"])
    size = f.get("filesize") or f.get("filesize_approx")
    if size:
        parts.append(_human_size(size))
    return " · ".join(parts)


def _human_size(n: float | int | None) -> str:
    if not n:
        return ""
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


# 瞬时网络错误特征：这类错误重试通常能成功，不应直接判为失败
_TRANSIENT_PATTERNS = (
    "unexpected_eof_while_reading",
    "ssl: ",
    "ssleoferror",
    "connection reset",
    "connection aborted",
    "remote end closed",
    "timed out",
    "timeout",
    "temporarily unavailable",
    "bad gateway",
    "http error 500",
    "http error 502",
    "http error 503",
    "http error 504",
    "incomplete read",
    "chunkedencodingerror",
    "protocolerror",
    # DNS 解析失败常为临时抖动（实测同一域名先失败后成功），值得重试
    "failed to resolve",
    "getaddrinfo failed",
    "name or service not known",
    "nodename nor servname",
)


def _is_transient_error(msg: str) -> bool:
    """判断是否为值得重试的瞬时网络错误。"""
    m = (msg or "").lower()
    return any(p in m for p in _TRANSIENT_PATTERNS)


def _friendly_error(msg: str) -> str:
    """把 yt-dlp 的英文长错误转成对用户有用的中文提示。"""
    m = msg.lower()
    if "sign in" in m or "login" in m or "account" in m:
        return "该视频需要登录或会员权限才能下载。可在「设置」中配置浏览器 Cookies 后重试，或换一个视频链接。"
    if "412" in m or "precondition failed" in m:
        return "站点风控拦截（HTTP 412）：该站点识别出当前不是普通浏览器访问。" \
               "请在「设置」里配置已登录该站点的 Cookies 文件后重试。"
    if "age" in m and "restrict" in m:
        return "该视频有年龄限制，需要登录后才能下载。可在「设置」中配置 Cookies。"
    if "private" in m:
        return "该视频是私密的，无法下载。"
    # ⚠️ 必须在下面那条泛化的 "not available" 之前判：
    # 实测（2026-09-21）B 站选「最小文件」时 yt-dlp 报
    # "Requested format is not available"，被泛化规则吃掉后显示成
    # 「该视频已被删除或不可用」—— 视频其实好着，只是这个清晰度档位不存在，
    # 归因错了会把用户引到完全错误的方向。
    if "requested format is not available" in m or "format is not available" in m:
        return "所选清晰度档位在该站点不存在（视频本身没问题）。已自动改用带兜底的档位；若仍失败请换一个清晰度档位重试。"
    if "no video formats found" in m or "no formats found" in m:
        return "该站点没有返回任何可下载的清晰度，可能需要在「设置」里配置 Cookies 后重试。"
    if "removed" in m or "unavailable" in m or "not available" in m:
        return "该视频已被删除或不可用。"
    if "geo" in m and ("restrict" in m or "block" in m):
        return "该视频有地域限制，当前网络所在地区无法访问。"
    if "unsupported url" in m or "no video" in m:
        return "该链接不是受支持的视频页面。请提供视频播放页地址，或改用「网页扫描」提取页面里的媒体直链。"
    if "unexpected_eof" in m or "ssleoferror" in m or "ssl:" in m:
        return "与站点的加密连接被中断（SSL），通常是站点侧不稳定或网络波动。已自动重试仍未成功，请稍后再试或换一个链接。"
    if "timed out" in m or "timeout" in m:
        return "连接超时，可能是网络波动或站点响应慢，请稍后重试。"
    if "failed to resolve" in m or "getaddrinfo" in m or "name or service not known" in m:
        return "域名解析失败：无法找到该站点的服务器地址。请检查链接是否正确，或本机网络/DNS 是否正常。"
    if "http error 403" in m or " 403" in m:
        return "站点拒绝了请求（403），可能存在防盗链或需要特定登录态，可尝试配置 Cookies 或换链接。"
    if "http error 404" in m or " 404" in m:
        return "资源不存在（404），链接可能已失效。"
    if "ffmpeg" in m and ("not found" in m or "missing" in m):
        return "该视频需要 ffmpeg 合并音视频，但当前未找到 ffmpeg。工具已内置 ffmpeg 到 bin 目录，请确认该文件存在（可能被杀毒软件删除）。"
    if _is_transient_error(msg):
        return "网络或站点临时异常，已自动重试仍未成功，请稍后再试。原始信息：" + msg.strip()[:180]
    # 去掉 yt-dlp 附带的「请上报 issue / 升级版本」等面向开发者的噪音，避免误导用户
    cleaned = re.split(r";?\s*please report this issue", msg, flags=re.I)[0]
    cleaned = re.split(r";?\s*confirm you are on the latest version", cleaned, flags=re.I)[0]
    return cleaned.strip()[:400] or "解析失败"


def build_search_query(keyword: str, platform: str = "youtube", count: int = 10) -> str:
    """把关键词转成 yt-dlp 搜索查询，如 ytsearch10:风景。"""
    prefix = SEARCH_PREFIXES.get((platform or "youtube").lower(), "ytsearch")
    n = max(1, min(int(count or 10), 50))
    return f"{prefix}{n}:{keyword}"


# ---- 会员试看兜底（2026-11）----
# 症状：B 站大会员番剧 / 优酷爱奇艺会员片，下载「成功」但文件只有 6 分钟。
# 原因：播放地址本身就是按游客权限裁剪过的，改参数拿不到完整流。
# 这里的做法是「下完核对时长 → 判定试看 → 换源重下完整版」，
# 具体链路见 app/vip_unlock.py（会员 Cookie → 资源站换源 → 解析网关）。
_VIP_NO_FALLBACK_HINTS = (
    "404", "not found", "unsupported url", "is not a valid url",
    "private video", "video unavailable", "incomplete youtube id",
    "已删除", "不存在", "no video formats found",
)


def _vip_auto_enabled() -> bool:
    try:
        from . import store
        return bool(store.load().get("vip_auto_unlock", True))
    except Exception:  # noqa: BLE001
        return False


def _vip_should_try(url: str, error_text: str = "") -> bool:
    """只有「会员平台」才折腾兜底，普通站点多一次探测纯属浪费。"""
    if not _vip_auto_enabled():
        return False
    try:
        from . import vip_unlock
        if not vip_unlock.platform_of(url):
            return False
    except Exception:  # noqa: BLE001
        return False
    if error_text:
        low = error_text.lower()
        if any(h in low for h in _VIP_NO_FALLBACK_HINTS):
            return False
    return True


def _unlock_with_budget(url: str, output_dir: Path, *, title: str, meta,
                        on_progress, task_id: str) -> dict:
    """带时间预算的换源：超预算就收手，把控制权交回调用方。

    为什么需要（2026-11-08 实测）：番剧换源连着试 3 个资源站，其中一条 CDN
    拉到 29MB 后彻底停住，ffmpeg 进程还活着、流下载的看门狗也没掐掉它，
    整个任务在界面上一直显示「running」，33 分钟没有任何进展说明。
    用户看到的是「点下载没反应」。这里给整段换源一个总预算，到点就置
    cancel_event（unlock 内部会顺着它中止 ffmpeg），然后按失败处理 ——
    调用方会把已下到的试看片段连同「这是试看段」的说明一起交回去。
    """
    import os
    import threading

    from . import vip_unlock

    try:
        budget = float(os.environ.get("MEDIA_DOWNLOADER_VIP_BUDGET") or 900)
    except ValueError:
        budget = 900.0

    ev = threading.Event()
    box: dict = {}

    def _work():
        try:
            box["r"] = vip_unlock.unlock(
                url, output_dir, title=title, meta=meta,
                on_progress=on_progress, task_id=task_id, cancel_event=ev)
        except Exception as e:  # noqa: BLE001
            box["e"] = e

    th = threading.Thread(target=_work, daemon=True)
    th.start()
    th.join(budget)
    if th.is_alive():
        ev.set()                      # 让 unlock 里的下载层中止 ffmpeg
        th.join(30)                   # 给它收尾的时间（删半截文件）
        return {"ok": False, "error": f"换源超过 {int(budget)} 秒仍未拿到完整版，已中止"}
    if "e" in box:
        raise box["e"]
    return box.get("r") or {"ok": False, "error": "换源没有返回结果"}


def _vip_replace_with_full(url: str, output_dir: Path, *, task_id: str | None,
                           title: str, info=None, old_files=None,
                           on_progress=None) -> dict | None:
    """尝试换源拿完整版；成功返回最终结果摘要，不满足条件返回 None。"""
    from . import vip_unlock

    meta = {}
    expected, actual = 0.0, 0.0
    if old_files:
        # B 站普通投稿（UGC）不存在「试看」：非会员只是最高清晰度受限，
        # 文件本身是完整的。真正会裁剪的只有番剧/影视/课堂（bangumi/cheese）。
        # 这里先做廉价预判，避免给每条普通 B 站视频都多花一次元数据请求。
        if "bilibili.com" in url and not any(k in url for k in
                                             ("/bangumi/", "/cheese/", "ep_id=")):
            return None
        # 已下完但可能只是试看：拿「页面标称时长」和「实际文件时长」比
        try:
            meta = vip_unlock.extract_meta(url)
        except Exception:  # noqa: BLE001
            meta = {}
        expected = max(float((info or {}).get("duration") or 0),
                       float(meta.get("duration") or 0))
        actual = max((vip_unlock.probe_duration(f.get("path") or "") for f in old_files),
                     default=0.0)
        if not vip_unlock.detect_preview(expected, actual):
            return None

    if on_progress:
        on_progress({
            "status": "retrying", "percent": 0.0, "progressed_bytes": 0,
            "total_bytes": 0, "speed": 0, "eta": None,
            "filename": (f"检测到试看片段（{int(actual)}s / 正片 {int(expected)}s），"
                         "正在换源下载完整版…" if old_files else "会员内容解析失败，正在换源下载完整版…"),
        })

    try:
        res = _unlock_with_budget(
            url, output_dir, title=title or str(meta.get("title") or ""),
            meta=meta or None, on_progress=on_progress, task_id=task_id or "")
    except Exception as e:  # noqa: BLE001 —— 兜底失败不能把原结果搞坏
        return None
    if not res.get("ok"):
        return None

    # 换源成功：删掉试看片段，避免目录里同时留一个 6 分钟的假正片
    for f in old_files or []:
        try:
            p = Path(f.get("path") or "")
            if p.is_file() and p.suffix.lower() not in (".part", ".tmp"):
                p.unlink()
        except OSError:
            pass

    fp = Path(res["path"])
    name = fp.name
    size = int(res.get("size") or fp.stat().st_size)
    from .downloader import url_fingerprint as _url_fp
    try:
        (output_dir / f".{_url_fp(url)}.done").write_text(name, encoding="utf-8")
    except OSError:
        pass
    try:
        from . import history as _hist
        _hist.record(url, {"title": title or name, "kind": "video",
                           "path": str(fp), "size": size})
    except Exception:  # noqa: BLE001
        pass
    return {
        "ok": True, "url": url,
        "files": [{"path": str(fp), "name": name, "size": size}],
        "count": 1,
        "note": res.get("note") or "会员内容已换源下载完整版。",
        "error": None,
        "output_dir": str(output_dir),
    }


# 界面/任务层在没有标题时会塞进来的占位名。它们不是真标题：
# 拿它命名只会得到「视频下载 [指纹].mp4」，用户分不清下的是哪一集。
_PLACEHOLDER_TITLES = frozenset({
    "视频下载", "视频", "download", "video", "Video", "未命名",
    "untitled", "Untitled",
})


def _dur_text(seconds: float) -> str:
    """秒数 → 人能读的时长（6:00 / 51:15 / 1:02:03）。"""
    s = max(0, int(round(float(seconds or 0))))
    if s >= 3600:
        return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"
    return f"{s // 60}:{s % 60:02d}"


def _preview_scope(url: str) -> bool:
    """这个地址下完之后要不要核对「是不是只有试看段」。

    跟「要不要自动换源」分开：换源是动作，可以由用户开关；核对是判断，
    必须一直做。返回 False 的只有两种：
    - 平台拿不到正片信息（vip_unlock.platform_of 为空），没法比对时长；
    - B 站普通投稿（UGC）不裁剪，非会员只是最高清晰度受限，文件本身完整。
    """
    try:
        from . import vip_unlock
        if not vip_unlock.platform_of(url):
            return False
    except Exception:  # noqa: BLE001
        return False
    # 真正会裁剪的只有番剧/影视/课堂，先用这个廉价判据挡掉，避免每条普通
    # B 站视频都多花一次元数据请求。
    if "bilibili.com" in url and not any(k in url for k in
                                         ("/bangumi/", "/cheese/", "ep_id=")):
        return False
    return True


def _preview_guard(url: str, output_dir: Path, files: list[dict], *, task_id,
                   title: str, info, on_progress) -> tuple[dict | None, str]:
    """下完核对时长：这一份是不是只有试看段。

    为什么必须在这里做：会员平台的页面播放地址本身就被账号权限裁剪过，
    浏览器解析出来的「真实播放地址」同样是 6 分钟试看段 —— 下载会「成功」。
    这条早返回的分支此前完全跳过了 2871 行的试看核对，用户拿到的
    15MB 文件被当成整集交付（2026-11-08 实测：人间世 第二季第 8 集
    标称 51:15，实际 6:00）。
    """
    if not files:
        return None, ""
    if not _preview_scope(url):
        return None, ""
    from . import vip_unlock
    try:
        meta = vip_unlock.extract_meta(url)
    except Exception:  # noqa: BLE001
        meta = {}
    expected = max(float((info or {}).get("duration") or 0)
                   if isinstance(info, dict) else 0.0,
                   float(meta.get("duration") or 0))
    actual = max((vip_unlock.probe_duration(f.get("path") or "") for f in files),
                 default=0.0)
    if not vip_unlock.detect_preview(expected, actual):
        return None, ""
    # 关掉「自动换源」的用户也仍然要知道这是试看段，所以判定在前、换源在后。
    if _vip_should_try(url):
        full = _vip_replace_with_full(url, output_dir, task_id=task_id, title=title,
                                      info=info, old_files=files, on_progress=on_progress)
        if full:
            return full, ""
    return None, (f"⚠️ 只拿到试看片段（{_dur_text(actual)} / 正片 {_dur_text(expected)}），"
                  "换源也没拿到完整版；这个文件不是完整正片。")


def download_video(
    url: str,
    output_dir: str | Path,
    task_id: str | None = None,
    quality: str = "best",
    container: str = "",
    prefer_audio_only: bool = False,
    filename_template: str = "%(title).120s [%(id)s].%(ext)s",
    write_subtitles: bool = False,
    subtitle_langs: str = "zh.*,en",
    include_playlist: bool = False,
    cookies_file: str | None = None,
    cookies_from_browser: str | None = None,
    on_progress=None,
    rate_limit: str = "",
    max_height: int | None = None,
    format_id: str = "",
    skip_existing: bool = True,
    title: str = "",
    proxy: str = "",
) -> dict:
    """下载单个视频（或播放列表），返回结果摘要。

    on_progress(info: dict) 回调字段：
      status(downloading/finished/合并中...)、progressed_bytes、total_bytes、
      percent、speed、eta、filename、info
    抛出 DownloadCancelled 表示用户取消。
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    # 同一内容的不同地址形态先归一（见 _normalize_media_url）。
    # 不归一的话 /video/<id> 会先去 302 到 video.zhihu.com 的 JS 壳，
    # 渲染拿不到东西，最后报「解析不出真实播放地址」。
    url = _normalize_media_url(url)
    # 标题为空时先向会员平台的官方接口要一次标题。不补的话，落盘名只能是
    # 「视频下载 [指纹]」—— 用户下完一整季，文件夹里全是同一个名字，
    # 分不清哪一集（2026-11-08 实测：人间世 第二季第 8 集落成「视频下载 [2eb6bd8e].mp4」）。
    if not title or title.strip() in _PLACEHOLDER_TITLES:
        try:
            from . import vip_unlock as _vu_t
            if _vu_t.platform_of(url):
                _m = _vu_t.extract_meta(url) or {}
                _t = str(_m.get("title") or "").strip()
                _ep = _m.get("episode")
                _epn = str(_m.get("episode_title") or "").strip()
                if _t and _ep:
                    _t = f"{_t} 第{_ep}集" + (f" {_epn}" if _epn else "")
                title = _t or title
        except Exception:  # noqa: BLE001 —— 补标题失败不影响下载
            pass
    if on_progress:
        # 渲染解析（resolve_media_render_full）可能跑十几秒且没有任何回调，
        # 先给界面一条明确状态，避免任务条在整个前置阶段都是空白的。
        try:
            on_progress({"status": "downloading", "percent": 0.0,
                         "progressed_bytes": 0, "total_bytes": 0, "speed": 0,
                         "eta": None, "filename": "正在解析视频页面…",
                         "note": "正在解析视频页面…"})
        except Exception:  # noqa: BLE001
            pass

    # ---- 去重标记（V3.0 统一）----
    # 实测（2026-10-24，用户报告）：TikTok / pornhub 走 yt-dlp 下载时**完全没写 .done**，
    # 而 cn.avjoy.me 那种直链下载会写 —— 同一个软件两种行为。
    # 后果：用户把视频转移走之后无从判断"下过没有"，重新抓同一个页面会再下一遍。
    # 所以这里和 downloader.py 用同一套标记：.{url指纹}.done，内容存实际文件名。
    from .downloader import url_fingerprint as _url_fp
    _marker = output_dir / f".{_url_fp(url)}.done"
    if skip_existing and _marker.exists():
        try:
            _saved = _marker.read_text(encoding="utf-8").strip()
        except OSError:
            _saved = ""
        if _saved:
            _p = output_dir / _saved
            if _p.exists() and _p.stat().st_size > 0:
                return {
                    "ok": True, "url": url, "skipped": True, "note": "此前已下载过，本次跳过。",
                    "files": [{"path": str(_p), "name": _p.name,
                               "size": _p.stat().st_size}],
                }
            # 文件不在了（被移走或删掉）：仍然算「下过」并按记录跳过。
            # 这是用户明确要的语义 —— 转移视频后不要重复下一遍。
            # 真要重下：关掉界面上的「跳过已存在」。
            return {
                "ok": True, "url": url, "skipped": True, "files": [],
                "note": (f"此前已下载过，但文件已不在当前目录（{_saved}），已按记录跳过。"
                         "需要重新下载请关闭「跳过已存在」。"),
            }

    # ---- 台账去重（V3.0 接上原本没被调用的 history.is_downloaded）----
    # history.is_downloaded 的语义是「只看视频身份，不看文件还在不在」，
    # 正好就是用户要的：视频转移走了也仍然算下过。但这个函数此前**没有任何调用处**，
    # 批量下载只靠 .done 标记，所以标记一丢就会重下。
    if skip_existing:
        try:
            from . import history as _hist
            if _hist.is_downloaded(url):
                return {
                    "ok": True, "url": url, "skipped": True, "files": [],
                    "note": ("下载台账里已有这条记录（文件可能已转移），按记录跳过。"
                             "需要重新下载请关闭「跳过已存在」。"),
                }
        except Exception:  # noqa: BLE001 —— 台账读失败不该阻断下载
            pass

    # ---- 未知站先渲染找真实播放器 ----
    # yt-dlp 的 HTML5MediaEmbed 会抓页面第一个 <video>，而那里常是广告位
    # （实测 hornyleak 拿到 728x90-1.mp4，用户看到的就是"下载的文件不对"）。
    # 这里先渲染解析一次；拿到非广告地址就走直链下载，绕开 yt-dlp 的误判。
    #
    # 抖音单独列出来：yt-dlp **有** Douyin extractor，但它要求 fresh cookies，
    # 批量抓来的页面地址直接下会报 "Fresh cookies (not necessarily logged in)
    # are needed" —— 而页面里 <video>.currentSrc 就是可用直链（实测能下）
    # （2026-10-25：抖音批量抓取成功、下载全部失败）。
    _dy_page = "douyin.com/video/" in url or "iesdouyin.com/share/video/" in url
    if (not has_specific_extractor(url)) or _dy_page:
        # 抖音直链解析顺序（全部无窗口 —— 2026-10-25 用户要求：下载用内置浏览器、绝不弹窗）：
        # ① 内置 Chromium headless + dy登录态（主力：实测拿到 zjcdn.com 正片直链）
        # → ② BrowserSkill（不同指纹的第二意见；失败一次本进程内不再试）
        # → ③ resolve_media_via_render + dy登录态（通用渲染兜底，同样 headless）
        # 无登录态时 ③ 直接跳过：抖音页面在无登录态下只有 uuu_265 占位视频，
        # 渲染纯属白跑（2026-10-25 实测），直接给出「去平台登录」的明确失败。
        _real = ""
        _parsed = None
        if _dy_page:
            _real = _douyin_real_url_profiled(url)
            if not _real:
                _real = _douyin_real_url_via_bsk(url)
        if not _real:
            _prof = None
            if _dy_page:
                try:
                    from .platform_login import profile_dir_for_url as _pdu
                    _p = _pdu(url, ready_only=True)
                    _prof = _p or None
                except Exception:  # noqa: BLE001
                    _prof = None
            if not _dy_page or _prof:
                _parsed = resolve_media_render_full(url, profile_dir=_prof)
                _real = str(_parsed.get("url") or "") \
                    if _parsed.get("kind") in ("direct", "both") else ""

        # ---- 「txt 分片流」优先 ----
        # hornyleak 实测（2026-10-30）：direct 是 400~700KB 的 preview.mp4 预览
        # 片段，15 分钟正片在「递增编号的伪 txt 分片」里；且分片 CDN 校验浏览器
        # 指纹，解析与下载必须在同一个浏览器会话里完成（见 _download_ts_via_browser）。
        # 分片下载失败才回退 direct（kind=both 时），kind=ts 时没有直链可回退。
        if _parsed and _parsed.get("kind") in ("ts", "both"):
            ev = register_cancel_event(task_id) if task_id else None
            _pq_ts = (str(max_height) if max_height
                      else "highest" if quality in ("best", "")
                      else "lowest" if quality == "worst" else str(quality))
            _ts = _download_ts_via_browser(
                page_url=url, output_dir=Path(output_dir),
                title=title or "", url=url,
                cancel_event=ev, on_progress=on_progress,
                prefer_quality=_pq_ts)
            if _ts.get("ok") and _ts.get("path"):
                _name = Path(_ts["path"]).name
                _sz = int(_ts.get("size") or 0)
                try:
                    (output_dir / f".{_url_fp(url)}.done").write_text(_name, encoding="utf-8")
                except OSError:
                    pass
                try:
                    from . import history as _hist3
                    _hist3.record(url, {"title": _name, "kind": "video",
                                        "path": _ts["path"], "size": _sz})
                except Exception:  # noqa: BLE001
                    pass
                return {
                    "ok": True, "url": url,
                    "files": [{"path": _ts["path"], "name": _name, "size": _sz}],
                    "note": _ts.get("note") or "已解析播放器分片流并下载正片。",
                }
            if _parsed.get("kind") == "ts" or _ts.get("truncated"):
                # 分片已解析成功但下不完整时，绝不回退到预览直链：那会交付
                # 几十 MB 的小样并报成功（2026-11-07 用户实测的三条视频）。
                return {"ok": False, "url": url, "truncated": True,
                        "error": _ts.get("error") or "分片流下载失败"}
            # 分片完全没解析出来才落回下面的 direct 路径继续

        if _real and _real.lower().split("?")[0].endswith((".m3u8", ".mpd")):
            # 渲染解析拿到的可能是 HLS/DASH 清单：当普通文件下只会存下几百字节
            # 的文本（实测 xvideos 310B 的 master playlist），任务却显示成功。
            # 改走流下载，由 ffmpeg 合并分片；prefer_quality=highest 取最高档。
            try:
                from .stream_downloader import download_stream as _dl_stream
                # 用户选的档位要带进流下载：best→最高、worst→最低、数字→该高度
                _pq = ("highest" if quality in ("best", "")
                       else "lowest" if quality == "worst" else str(quality))
                _pq_label = {"highest": "最高档", "lowest": "最低档"}.get(
                    _pq, f"{_pq}p")
                _sr = _dl_stream(
                    _real, output_dir, referer=url, title=title or "",
                    skip_existing=False, on_progress=on_progress,
                    prefer_quality=_pq,
                )
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "url": url,
                        "error": f"流媒体下载失败: {type(e).__name__}: {str(e)[:160]}"}
            if _sr.ok and _sr.path:
                _name = Path(_sr.path).name
                try:
                    (output_dir / f".{_url_fp(url)}.done").write_text(_name, encoding="utf-8")
                except OSError:
                    pass
                try:
                    from . import history as _hist_s
                    _hist_s.record(url, {"title": title or _name, "kind": "video",
                                         "path": _sr.path, "size": _sr.size})
                except Exception:  # noqa: BLE001
                    pass
                _files = [{"path": _sr.path, "name": _name, "size": _sr.size}]
                _full, _pnote = _preview_guard(
                    url, output_dir, _files, task_id=task_id, title=title,
                    info=info, on_progress=on_progress)
                if _full:
                    _full["note"] = "；".join(
                        x for x in (_full.get("note") or "",
                                    "换源拿到了完整版") if x)
                    return _full
                return {
                    "ok": True, "url": url,
                    "files": _files,
                    "note": f"解析到 HLS 清单，已按{_pq_label}合并下载。"
                            + (f" {_pnote}" if _pnote else ""),
                }
            return {"ok": False, "url": url,
                    "error": _sr.error or "HLS 流下载失败"}

        if _real:
            try:
                from .downloader import download_file as _dl_file
                # 这里**直接指定文件名**，不再依赖「URL 名像不像哈希」去判断。
                # 原因：抖音 CDN 的直链文件名是 uuu_265.mp4 这种短名（不是长哈希），
                # 原来的占位名规则认不出来 → 一批视频全落成同一个 uuu_265.mp4，
                # 互相覆盖（2026-10-25 用户实测：下载下来很多都是同一个视频）。
                # 既然抓取时标题是准的，就直接用它命名，最稳。
                _fname = ""
                if title:
                    try:
                        from .downloader import sanitize_filename as _sn
                        _stem = _sn(title)[:110].strip()
                        if _stem:
                            # 带上 URL 指纹：抖音一批视频里标题重复的情况不少，
                            # 只用标题会互相覆盖，最后只剩一个文件。
                            _fname = f"{_stem} [{_url_fp(url)[:8]}].mp4"
                    except Exception:  # noqa: BLE001
                        _fname = ""
                _r = _dl_file(_real, output_dir, skip_existing=False,
                              referer=url, title=title or "", filename=_fname)
                if _r.ok and _r.path:
                    _name = Path(_r.path).name
                    try:
                        (output_dir / f".{_url_fp(url)}.done").write_text(_name, encoding="utf-8")
                    except OSError:
                        pass
                    try:
                        from . import history as _hist2
                        _hist2.record(url, {"title": _name, "kind": "video",
                                           "path": _r.path, "size": _r.size})
                    except Exception:  # noqa: BLE001
                        pass
                    _files = [{"path": _r.path, "name": _name, "size": _r.size}]
                    _full, _pnote = _preview_guard(
                        url, output_dir, _files, task_id=task_id, title=title,
                        info=info, on_progress=on_progress)
                    if _full:
                        _full["note"] = "；".join(
                            x for x in (_full.get("note") or "",
                                        "换源拿到了完整版") if x)
                        return _full
                    return {
                        "ok": True, "url": url,
                        "files": _files,
                        "note": ("已用内置浏览器解析出真实播放地址"
                                 "（绕开了 yt-dlp 对这类页面的限制）。"
                                 + (f" {_pnote}" if _pnote else "")),
                    }
            except Exception:  # noqa: BLE001 —— 失败就回到原流程
                pass
        else:
            # 解析不到真实播放器时**明确失败**，绝不退回 yt-dlp ——
            # 那条路会把这个站的 728x90 广告位当成视频下下来，
            # 用户拿到一个"能播放的假文件"比报错更糟（2026-10-24 实测）。
            # 前置渲染解析拿不到播放器时，再用「一体式分片流」兜底：它自己
            # 剥 iframe 链、抓 HLS master / 子清单，不依赖上面的渲染结果。
            # 2026-11-07 实测：并发下 resolve_media_render_full 偶发返回 none，
            # 但同一个视频页的清单解析是稳定的，不该因此整条判失败。
            _fb = None
            if not _dy_page:
                _pq_fb = (str(max_height) if max_height
                          else "highest" if quality in ("best", "")
                          else "lowest" if quality == "worst" else str(quality))
                _fb = _download_ts_via_browser(
                    page_url=url, output_dir=Path(output_dir),
                    title=title or "", url=url,
                    cancel_event=register_cancel_event(task_id) if task_id else None,
                    on_progress=on_progress, prefer_quality=_pq_fb)
                if _fb.get("ok") and _fb.get("path"):
                    _name = Path(_fb["path"]).name
                    _sz = int(_fb.get("size") or 0)
                    try:
                        (output_dir / f".{_url_fp(url)}.done").write_text(
                            _name, encoding="utf-8")
                    except OSError:
                        pass
                    try:
                        from . import history as _hist_fb
                        _hist_fb.record(url, {"title": _name, "kind": "video",
                                              "path": _fb["path"], "size": _sz})
                    except Exception:  # noqa: BLE001
                        pass
                    return {"ok": True, "url": url,
                            "files": [{"path": _fb["path"], "name": _name,
                                       "size": _sz}],
                            "note": _fb.get("note") or "已解析播放器分片流并下载正片。"}
            if _dy_page:
                _err = ("没能解析出这条抖音视频的真实播放地址（全程后台、没有弹窗）。"
                        "最常见原因是抖音登录态过期或触发风控："
                        "请到「平台登录」页重新登录/刷新抖音，然后重试这批任务。")
            else:
                _err = ("这个站点的视频由第三方播放器承载（iframe / 无扩展名地址），"
                        "暂时无法稳定解析出真实视频地址"
                        + (f"（{_fb.get('error')}）" if _fb and _fb.get("error") else "")
                        + "。为避免把页面上的广告位当成视频下载，这里直接报失败而不是给你一个假文件。")
            return {
                "ok": False, "url": url, "files": [],
                "error": _err,
            }

    # 已经解析出的签名媒体直链（XHS/Douyin 等）不要再套网页格式选择器。
    # Generic extractor 对这类 .mp4 经常不填 vcodec/acodec/height；继续使用
    # [vcodec!=none] 会把真实视频错误过滤成“格式不存在”。音频直链扩展名不会进这里。
    direct_video_exts = {".mp4", ".webm", ".mkv", ".mov", ".avi", ".flv", ".m4v", ".mpg", ".mpeg", ".3gp", ".ts"}
    direct_ext = Path(urlparse(url).path).suffix.lower()
    # 内置平台来源给出的签名播放地址有些并不带扩展名，但 yt-dlp 对它们只会走 Generic。
    # 这类地址已经是“解析后的媒体”，不应再二次挑格式；完全省略 format 最稳。
    is_generic_media = (direct_ext in direct_video_exts) or (not has_specific_extractor(url))
    fmt = None if (is_generic_media and not prefer_audio_only and quality != "audio" and not format_id) else build_format_selector(
        quality, container, prefer_audio_only, max_height, format_id=format_id)

    opts = _base_ydl_opts(
        task_id,
        quiet=True,
        cookies_file=cookies_file,
        cookies_from_browser=cookies_from_browser,
        include_playlist=include_playlist,
        proxy=proxy,
    )
    # 抓取阶段已经知道作品名时，直接拿它当文件名主干。
    #
    # 为什么必须覆盖（2026-11-08 用户报「下载下来的名字是随机字符」）：
    #   `outtmpl` 里的 `%(title)s` 是 **yt-dlp 自己解析出来的** 标题，不是调用方
    #   传进来的 `title`。小红书、微博这类走直链或 Generic 提取器的地址，
    #   yt-dlp 只能把 URL 最后一段当标题 —— 那是 CDN 哈希，落盘就是随机字符。
    #   而调用方（抓取结果）明明已经有正确作品名了。
    #   截断 id 保留前 16 位：既能在同名时区分，又不至于让文件名变回一串乱码。
    _tmpl = filename_template
    _want_title = (title or "").strip()
    if _want_title and "%(title)" in _tmpl:
        try:
            from .downloader import sanitize_filename as _sn_t
            _stem = _sn_t(_want_title)[:110].strip()
        except Exception:  # noqa: BLE001
            _stem = ""
        # 别把占位标题（提交时没带真实标题）当成作品名写进文件名
        if _stem and _stem not in ("视频下载", "视频", "download", "video", "Video", "未命名"):
            _tmpl = _stem + " [%(id).16s].%(ext)s"
    opts.update(
        {
            "format": fmt,
            "outtmpl": str(output_dir / _tmpl),
            "merge_output_format": container if container in ("mp4", "mkv", "webm", "mov") else None,
            "noprogress": True,
        }
    )
    opts = {k: v for k, v in opts.items() if v is not None}

    if rate_limit:
        opts["ratelimit"] = _parse_rate(rate_limit)
    if write_subtitles:
        opts["writesubtitles"] = True
        opts["writeautomaticsub"] = True
        opts["subtitleslangs"] = [s.strip() for s in subtitle_langs.split(",") if s.strip()]
        opts["subtitlesformat"] = "srt/best"
        ff = find_ffmpeg()
        if ff:
            opts.setdefault("postprocessor_args", {})

    def _hook(d):
        if not on_progress:
            return
        status = d.get("status")
        total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
        done = d.get("downloaded_bytes") or 0
        percent = round(done * 100 / total, 1) if total else 0.0
        info = {
            "status": status,
            "progressed_bytes": done,
            "total_bytes": total,
            "percent": 100.0 if status == "finished" else percent,
            "speed": d.get("speed") or 0,
            "eta": d.get("eta"),
            "filename": Path(d.get("filename") or "").name,
        }
        on_progress(info)

    if task_id:
        # 保留取消 hook，同时追加进度 hook
        ev = register_cancel_event(task_id)

        def _cancel_hook(d):
            if ev.is_set():
                raise DownloadCancelled("用户已取消下载")

        opts["progress_hooks"] = [_cancel_hook, _hook]
    else:
        opts["progress_hooks"] = [_hook]

    downloaded: list[str] = []
    max_attempts = 3
    last_err = ""
    info = None
    # 档位兜底：站点没有该清晰度的独立流时（实测 B 站选「最小文件」→
    # "Requested format is not available"），换用带兜底链的选择器再试一次。
    # 这与直链下载那套「首选地址失败换同媒体其它档位」是同一个原则：
    # 不能因为「用户选的档位恰好不存在」就把「视频本身没问题」判成下载失败。
    fmt_fallback_used = False
    # 客户端兜底：YouTube 会对默认客户端（android_vr）返回的媒体地址直接 403
    # （实测 2026-09-21：装上 JS 运行时后仍 403，yt-dlp 日志显示
    #  "YouTube is forcing SABR streaming" + 媒体地址 403）。
    # 换 `mweb` 客户端可以下成功，但**只到 360p**（默认客户端能到 4K），
    # 所以它是兜底而不是首选，并且成功时要明确告知用户清晰度可能受限。
    client_fallback_used = False
    fallback_note = ""
    attempt = 1
    while attempt <= max_attempts:
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                if info:
                    if info.get("_type") == "playlist" or "entries" in info:
                        for e in (info.get("entries") or []):
                            if e:
                                try:
                                    downloaded.append(ydl.prepare_filename(e))
                                except Exception:
                                    pass
                    else:
                        downloaded.append(ydl.prepare_filename(info))
            last_err = ""
            break
        except DownloadCancelled:
            raise
        except yt_dlp.utils.DownloadError as e:
            last_err = str(e)
            low = last_err.lower()
            if not fmt_fallback_used and (
                    "requested format is not available" in low
                    or "format is not available" in low
                    or "no video formats found" in low):
                fmt_fallback_used = True
                opts["format"] = _fallback_format_selector(container)
                if on_progress:
                    on_progress({
                        "status": "retrying", "percent": 0.0, "progressed_bytes": 0,
                        "total_bytes": 0, "speed": 0, "eta": None,
                        "filename": "该清晰度档位不可用，自动改用兜底档位重试…",
                    })
                continue          # 档位兜底不占用网络重试次数
            # 媒体地址被站点拒绝（YouTube 的 403 Forbidden）-> 换客户端兜底。
            # 这不占网络重试次数，也不与「瞬时网络错误」混为一谈：
            # 它是站点侧的策略性拒绝，重试同样的客户端只会再 403 一次。
            if not client_fallback_used and _is_media_forbidden(low):
                client_fallback_used = True
                args = dict(opts.get("extractor_args") or {})
                yt_args = dict(args.get("youtube") or {})
                yt_args["player_client"] = ["mweb"]
                args["youtube"] = yt_args
                opts["extractor_args"] = args
                fallback_note = ("默认客户端拿不到可用的媒体地址（403），"
                                 "已自动改用 mweb 客户端下载成功；"
                                 "该客户端清晰度会受限（通常最高 360p）。")
                if on_progress:
                    on_progress({
                        "status": "retrying", "percent": 0.0, "progressed_bytes": 0,
                        "total_bytes": 0, "speed": 0, "eta": None,
                        "filename": "媒体地址被拒（403），改用 mweb 客户端重试…",
                    })
                continue
            # 权限/内容类错误重试无意义；仅对瞬时网络错误重试
            if not _is_transient_error(last_err) or attempt >= max_attempts:
                break
            if on_progress:
                on_progress({
                    "status": "retrying",
                    "percent": 0.0,
                    "progressed_bytes": 0,
                    "total_bytes": 0,
                    "speed": 0,
                    "eta": None,
                    "filename": f"网络波动，第 {attempt + 1} 次尝试…",
                })
            time.sleep(1.5 * attempt)
            attempt += 1
            continue
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"下载失败: {type(e).__name__}: {e}", "url": url, "files": [],
                "note": fallback_note}
        attempt += 1

    if last_err:
        full = None
        tried_vip = _vip_should_try(url, last_err)
        if tried_vip:
            full = _vip_replace_with_full(
                url, output_dir, task_id=task_id, title=title, info=info,
                old_files=None, on_progress=on_progress)
        if full:
            full["note"] = "；".join(x for x in (full.get("note") or "",
                                                 fallback_note) if x)
            return full
        msg = _friendly_error(last_err)
        if tried_vip:
            msg += "（已自动尝试换源兜底，但没拿到完整版；可用 probe_vip.py 单独诊断）"
        return {"ok": False, "error": msg, "url": url, "files": [],
                "note": fallback_note}

    # 核对真实落盘文件（合并后扩展名可能变化）
    real_files = []
    for f in downloaded:
        p = Path(f)
        if p.exists():
            real_files.append({"path": str(p), "name": p.name, "size": p.stat().st_size})
        else:
            # 尝试同目录同名不同扩展
            for cand in output_dir.glob(p.stem + ".*"):
                if cand.exists() and cand.suffix.lower() not in (".part", ".ytdl", ".tmp"):
                    real_files.append({"path": str(cand), "name": cand.name, "size": cand.stat().st_size})
                    break

    # ---- 会员试看兜底：下完先核对时长，明显短于正片就换源重下 ----
    preview_note = ""
    if real_files and not (isinstance(info, dict) and info.get("entries")):
        full, preview_note = _preview_guard(
            url, output_dir, real_files, task_id=task_id, title=title,
            info=info, on_progress=on_progress)
        if full:
            full["note"] = "；".join(x for x in (full.get("note") or "",
                                                 fallback_note) if x)
            return full

    # ---- 写去重标记（和直链下载保持一致）----
    # 成功落盘后写 .done，内容为实际文件名。这样用户把文件转移走之后，
    # 下次再抓同一页面能凭标记判断「下过」，不会重复下载。
    if real_files:
        try:
            (output_dir / f".{_url_fp(url)}.done").write_text(
                str(real_files[0]["name"]), encoding="utf-8")
        except OSError:
            pass

    # ---- 记入下载台账（去重用）----
    # 记的是**视频身份**（yt-dlp 的 extractor+id，以及归一的页面地址），
    # 不是文件路径 —— 用户把文件挪走/删掉后，这里依然算「下过」，
    # 下次搜索不会再冒出来。path 只作为备注展示。
    if real_files:
        try:
            from . import history

            first = real_files[0]
            if isinstance(info, dict) and info.get("entries"):
                for e in (info.get("entries") or []):
                    if isinstance(e, dict):
                        history.record(str(e.get("webpage_url") or e.get("url") or url),
                                       e, kind="video")
            else:
                history.record(url, info if isinstance(info, dict) else None,
                               title=str((info or {}).get("title") or "") if isinstance(info, dict) else "",
                               kind="video", path=first.get("path", ""),
                               size=int(first.get("size") or 0))
        except Exception:
            # 记账失败绝不能影响下载结果本身（台账只是附加信息）
            pass

    return {
        "ok": bool(real_files),
        "url": url,
        "files": real_files,
        "count": len(real_files),
        # 说明性备注（如「已自动改用 mweb 客户端，清晰度受限」）。
        # 必须往上传到界面，不允许静默降级。
        # preview_note：这一份只有试看段且换源没成 —— 不给说明的话，
        # 用户会以为拿到了整集（2026-11-08 实测：51:15 的剧交付了 6:00 的片段）。
        "note": "；".join(x for x in (fallback_note, preview_note) if x),
        "error": None if real_files else "下载完成但未找到落盘文件",
        "output_dir": str(output_dir),
    }


def _parse_rate(rate: str) -> int | None:
    """把 '1M' / '500K' / '2.5M' 解析为字节/秒。"""
    if not rate:
        return None
    m = re.match(r"([\d.]+)\s*([KMG]?)", str(rate).strip().upper())
    if not m:
        return None
    val = float(m.group(1))
    unit = m.group(2)
    mult = {"": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3}[unit]
    return int(val * mult)


def _relevance_summary(items: list[dict], keyword: str) -> tuple[int, bool]:
    """返回 (与关键词相关的条数, 是否整批无关)。

    判据与 `app/relevance.py` 完全一致（样本 ≥8 且命中率 ≤0.12 才算整批无关），
    只用于「要不要如实提示用户」，**不参与过滤** —— 条目一条都不删。
    用同一套阈值是为了避免误报：曾经用「命中率 ≤10%」这种自造阈值，
    结果返回 1 条时会提示「0/1 条标题含关键词」，其实 1 条样本根本不足以定论。
    """
    from . import relevance

    kinds = [dict(it, url=it.get("webpage_url") or "") for it in (items or [])]
    _, report = relevance.audit(kinds, keyword)
    return int(report.get("hits") or 0), bool(report.get("blocked"))


# 平台专属搜索失败时的兜底：用全网来源搜完再按域名筛
_YT_HOST_RE = re.compile(r"(^|\.)(youtube\.com|youtu\.be)$", re.I)
_BILI_HOST_RE = re.compile(r"(^|\.)bilibili\.com$", re.I)


def _search_ddg_scoped(keyword: str, count: int, host_re: re.Pattern) -> list[dict]:
    """用全网来源搜，再按站点域名过滤。

    作为「平台专属搜索不可用」时的兜底：YouTube / B 站各自的搜索链路都可能因为
    站点风控或网络环境失效，但搜索引擎的视频索引里本来就含这两家的内容，
    按域名筛一下就能救回大部分结果 —— 关键是**别让用户面对一个空列表**。
    """
    from .search_web_video import search_ddg_videos

    items, _ = search_ddg_videos(keyword, max(count * 4, 40))
    out = [it for it in items
           if host_re.search(_site_of(it.get("webpage_url") or ""))]
    return out[:count]


def _ytdlp_search(keyword: str, platform: str, count: int,
                  tries: int = 2) -> tuple[list[dict], str | None]:
    """走 yt-dlp 原生搜索（ytsearch / bilisearch）。

    加重试是因为本机走代理时 YouTube 侧会偶发 502 / ReadTimeout；
    一次失败就报错会让用户以为「功能坏了」。
    """
    q = build_search_query(keyword, platform, count)
    opts = _base_ydl_opts(include_playlist=True)
    opts["extract_flat"] = "in_playlist"  # 只取列表信息，不逐个解析，速度快
    opts["skip_download"] = True
    # 收紧网络层超时：实测本机代理会偶发挂住，yt-dlp 默认值能让一次搜索拖到 50s+
    # （用户侧表现就是「点了搜索一直转圈」）。同一关键词失败后还能靠上层兜底。
    opts["socket_timeout"] = 10
    opts["retries"] = 1
    opts["extractor_retries"] = 1

    last = ""
    for attempt in range(1, tries + 1):
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(q, download=False)
        except Exception as e:  # noqa: BLE001
            last = _friendly_error(f"{type(e).__name__}: {e}")
            if attempt < tries:
                time.sleep(1.5 * attempt)
            continue

        entries = [e for e in ((info or {}).get("entries") or []) if e]
        items = [_summarize_entry(e, i) for i, e in enumerate(entries)]
        # 扁平模式下有的站点只给 id 不给标题，这类条目在界面上只是「(无标题)」噪声
        items = [it for it in items if it.get("title") and it.get("webpage_url")]
        if items:
            return items[:count], None
        last = "搜索没有返回结果"
    return [], last or "搜索失败"


def _search_platform(keyword: str, platform: str, count: int) -> dict:
    """平台专属搜索：主链路 + 「更有把握的兜底」。

    兜底不是无脑换来源，而是**只在主链路整批都拿不出强相关结果时**才动用，
    并且要求兜底结果确实有强相关命中才采用 —— 宁可保留原结果并如实提示，
    也不因为换来源反而给出更差的内容。
    触发场景（实测）：B 站自身搜索对英文多词查询会按单个词乱匹配
    （`led string lights` → 「c++ string」「python中string模块」）。
    """
    if platform == "youtube":
        host_re, site = _YT_HOST_RE, "YouTube"
        items, err = _ytdlp_search(keyword, "youtube", count)
        err_hint = f"YouTube 原生搜索不可用（{err}）" if err else "YouTube 原生搜索没有结果"
    else:
        host_re, site = _BILI_HOST_RE, "Bilibili"
        from .search_bilibili import search_bilibili_videos

        items, err = search_bilibili_videos(keyword, count)
        err_hint = f"Bilibili 搜索接口不可用（{err}）" if err else "Bilibili 没有搜到结果"

    from .search_web_video import count_strong_hits, rank_by_relevance

    strong = count_strong_hits(items, keyword) if items else 0
    # 「强相关占比过低」也算拿不出好东西 —— 实测 B 站搜 product demo：
    # 10 条里只有 1 条同时含 product 与 demo（还是句凑巧的话），其余全是
    # 「【Product/熟肉】」或「音乐剧 demo」，单看「0 条」是抓不住的。
    #
    # 用严格小于（<20%）而不是 ≤：实测「B 站搜 风景 航拍」是 2/10 恰好等于 20%，
    # 结果其实都是航拍内容、完全可用，却会被判成「相关性很低」并触发换源，
    # 提示语看着像出问题了。边界情况宁可不折腾。
    weak_batch = bool(items) and len(items) >= 5 and strong * 5 < len(items)

    # 主链路没结果、或整批几乎没有强相关命中 -> 试试按域名从全网来源兜底
    if not items or weak_batch:
        alt = _search_ddg_scoped(keyword, count, host_re)
        if alt and count_strong_hits(alt, keyword) > strong:
            hint = (f"{err_hint}，" if not items else
                    f"{site} 对「{keyword}」的搜索结果与关键词相关性很低，")
            return {
                "ok": True, "query": keyword, "platform": platform,
                "count": len(alt), "items": alt, "fallback": True,
                "hint": f"{hint}已自动改用全网来源里属于 {site} 的结果。",
            }
        if not items:
            return {"ok": False, "platform": platform, "query": keyword,
                    "error": err or f"{site} 没有搜到与该关键词相关的视频"}

    items = rank_by_relevance(items, keyword)
    resp = {"ok": True, "query": keyword, "platform": platform,
            "count": len(items), "items": items[:count]}
    if strong == 0:
        # 一条都没有文本命中 -> 基本可以断定这批跟关键词无关（实测 B 站搜
        # `led string lights` 返回「c++ string / python中string模块」、
        # 搜 `drone footage` 返回《后室 Found Footage》）。
        # 为什么只在「一条都没有」时才提示：**中文关键词 + 英文标题**会让匹配率
        # 天然偏低，按比例判会误报 —— 实测「YouTube 搜 风景 航拍」10 条结果都正常，
        # 却因为只有 1 条标题含中文关键词而被提示「相关性低」。宁可少提示，不可误报。
        resp["low_relevance"] = True
        resp["title_hits"] = strong
        resp["hint"] = (f"{site} 返回的 {len(items)} 条结果里没有一条标题含「{keyword}」"
                        f"（站点搜索是按单个词匹配的），建议换个说法或换平台再搜。")
    return resp


def _search_by_host(keyword: str, platform: str, count: int,
                    patterns: tuple[str, ...], label: str,
                    extra_sources: tuple = ()) -> dict:
    """按站点挑选结果（国内主流 / 国外主流 / 单站）。

    两条腿走路，缺一不可：

    1. **多来源结果里按域名挑选** —— 各站内容本来就混在 DDG/Bing 的结果里
       （实测跨 6 个关键词累计：B站 87、抖音 60、腾讯 33、搜狐 19、爱奇艺 11）。
    2. **补上该组里"有官方搜索接口"的站点** —— 只靠第 1 条会漏：
       实测中文关键词「张梓琳」下，搜索引擎索引里几乎没有 YouTube 条目
       （因为该关键词在搜索引擎的视频池里就是中文站占绝大多数），
       于是「国外主流」返回 0，而单独选 YouTube 却能拿到 10 条相关的。
       所以国内主流要额外并入 B 站官方搜索、国外主流要额外并入 YouTube 原生搜索。

    注意并入的是**有公开检索入口**的站点（国内并 B 站、国外并 YouTube 与
    Dailymotion），其余站点仍靠检索结果按域名归属。

    ⚠️ **单站（douyin / qq / youku …）不走本函数**，走 `_search_site` ——
    因为「国内/国外主流」是宽口径（池子里总量够），而单站要求「全是这一家的」，
    靠筛域名会几乎筛不到（实测「科比」下抖音 0 条、腾讯 0 条、TikTok 0 条），
    选项会形同虚设。界面文案会如实说明各自的做法。
    """
    from .search_web_video import (
        dedupe_key,
        filter_items_by_hosts,
        merge_items,
        rank_by_relevance,
        search_web_videos,
    )

    # 通用池与各个额外来源**并行**发出。它们互不依赖 —— 原来池先跑一轮
    # （10~30s），额外来源再各等一次超时，耗时是相加的。
    #
    # 池取的是**不限域**的全网结果（多取一些）：本组站点的条目从里面筛，
    # 「兜底」也从同一份数据里取，所以放开兜底不会带来额外请求。
    extras: list[list[dict]] = []
    sources: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=1 + max(1, len(extra_sources))) as pool_ex:
        f_pool = pool_ex.submit(
            lambda: search_web_videos(keyword, max(1, count) * 2, pool_factor=3))
        futs = {name: pool_ex.submit(fn) for name, fn in extra_sources}

        try:
            r = f_pool.result()
        except Exception as e:  # noqa: BLE001
            r = {"items": [], "sources": {}, "error": f"{type(e).__name__}: {e}"}
        pool_all = r.get("items") or []
        sources.update(r.get("sources") or {})
        items = filter_items_by_hosts(pool_all, patterns)
        _pk = {dedupe_key(it.get("webpage_url") or "") for it in items}
        broad_pool = [it for it in pool_all
                      if dedupe_key(it.get("webpage_url") or "") not in _pk]

        for name, fut in futs.items():
            try:
                got, err = fut.result()
            except Exception as e:  # noqa: BLE001
                sources[name] = {"count": 0, "error": f"{type(e).__name__}: {e}"}
                continue
            got = filter_items_by_hosts(got or [], patterns)   # 再保险一次归属
            sources[name] = {"count": len(got), "error": err}
            if got:
                extras.append(got)

    merged = merge_items(*extras, items, limit=10 ** 6)

    # 兜底：不够数时把范围放开到**不限域的全网**（用的是同一份池里被域名筛掉的
    # 那部分，不额外发请求）。用户要求「兜底是全网搜索，而不是仅仅限于我们
    # 罗列的这些主流视频网站」——原来兜底也带 host_patterns，等于在原地再找一遍。
    if len(merged) < max(1, count) and broad_pool:
        for it in broad_pool:
            ex = dict(it.get("extra") or {})
            ex["broad_fallback"] = True
            it["extra"] = ex
        merged_all = merge_items(merged, broad_pool, limit=10 ** 6)
        if len(merged_all) > len(merged):
            merged = merged_all

    # 本组站点的结果优先，兜底排在后面 —— 排序不能把「不是这个范围的」顶到前面
    _own = [x for x in merged if not (x.get("extra") or {}).get("broad_fallback")]
    _broad = [x for x in merged if (x.get("extra") or {}).get("broad_fallback")]
    merged = (rank_by_relevance(_own, keyword)
              + rank_by_relevance(_broad, keyword))

    for i, it in enumerate(merged[: max(1, count)]):
        it["index"] = i
    merged = merged[: max(1, count)]

    if not merged:
        return {"ok": False, "platform": platform, "query": keyword,
                "error": r.get("error"), "host_filtered": r.get("host_filtered"),
                "sources": sources}

    resp = {"ok": True, "query": keyword, "platform": platform,
            "count": len(merged), "items": merged, "sources": sources,
            "host_source": label}
    own_n = sum(1 for x in merged
                if not (x.get("extra") or {}).get("broad_fallback"))
    broad_n = len(merged) - own_n
    parts: list[str] = []
    if len(merged) < max(1, count) and own_n:
        # 数量没凑够就说清楚，别让用户以为「只有这么几条」
        parts.append(f"属于「{label}」的视频共找到 {own_n} 条"
                     f"（少于你要的 {count} 条）。")
    if broad_n:
        where = "排在后面" if own_n else "就是上面这些"
        parts.append(f"其中 {broad_n} 条来自**全网**（不属于「{label}」，{where}）"
                     "—— 兜底不限于我们罗列的站点。")
    if parts:
        resp["hint"] = "".join(parts)
        resp["broad_fallback"] = broad_n
    return resp


def _site_word_bundle(keyword: str, keys: tuple[str, ...], count: int):
    """构造一个 extra_source：对若干站点并行做「关键词+站点名」检索并合并。

    用于「国内主流 / 国外主流」的**来源多样化**。这两组原来只并入各自那一两个
    有官方接口的站，结果会被它们占满 —— 实测「国内主流」20 条里 19 条是 B 站，
    用户看到的就像「整个国内只剩 B 站」。用站点常用名去检索该组的其它主力站，
    结果才有代表性。

    与单站那套一样是**纯关键词技巧**（不碰任何站点接口、不解析任何站点页面）。
    """
    def run():
        from .search_web_video import (
            SITE_HOSTS,
            SITE_WORDS,
            filter_items_by_hosts,
            merge_items,
            search_ddg_videos,
        )

        targets = [(k, SITE_WORDS[k]) for k in keys
                   if SITE_WORDS.get(k) and SITE_HOSTS.get(k)]
        if not targets:
            return [], None
        groups: list[list[dict]] = []
        with ThreadPoolExecutor(max_workers=min(4, len(targets))) as pool:
            futs = [(k, pool.submit(search_ddg_videos, f"{keyword} {w}",
                                    max(10, count)))
                    for k, w in targets]
            for k, fut in futs:
                try:
                    got, _err = fut.result()
                except Exception:  # noqa: BLE001
                    continue
                hit = filter_items_by_hosts(got or [], SITE_HOSTS[k])
                if hit:
                    groups.append(hit)
        return (merge_items(*groups, limit=10 ** 6) if groups else []), None

    return run


def _search_site(keyword: str, platform: str, count: int,
                 patterns: tuple[str, ...], label: str,
                 extra_sources: tuple = ()) -> dict:
    """**单站**检索：主动按站点去检索，再用通用池补足。

    用户反馈「按站点挑选结果这个选项没有意义了 —— 搜出来都很少」。根因是
    单站原本只做「从通用检索结果里筛域名」，而通用结果里某站的条目本就极少
    （实测关键词「科比」：抖音 0 条、腾讯 0 条、TikTok 0 条、优酷 0 条）。

    现在改为先用 `search_site_scoped` **主动按站点检索**（`site:<域名>` 限定 +
    「关键词+站点常用名」两条通用手段并施），不够数时再拿通用池补。

    实测（2026-09-21，关键词「科比」，改前 -> 改后）：
      抖音 0 -> 56 · 腾讯视频 0 -> 六十余 · TikTok 0 -> 49 ·
      Instagram 0 -> 40 · 搜狐视频 3 -> 60 · 爱奇艺 0 -> 8
    """
    from .search_web_video import (
        SITE_WORDS,
        THIN_INDEX_SITES,
        dedupe_key,
        filter_items_by_hosts,
        merge_items,
        rank_by_relevance,
        search_site_scoped,
        search_web_videos,
    )

    need = max(1, count)
    site_word = SITE_WORDS.get(platform, "")

    def _pool_job():
        # 取**不限域**的全网结果并多取一些：该站的条目从里面筛，
        # 「兜底」也从同一份数据里取 —— 用户要求「兜底应是全网搜索，而不是
        # 仅仅限于我们罗列的这些站点」。这样兜底**不额外发请求**。
        return search_web_videos(keyword, need * 2, pool_factor=2)

    # 三路**并行**：站点检索 / 该站自带的公开接口 / 通用池。
    #
    # 原来这三段是串行的，实测最慢到 50.7s（快手）与 45.8s（Vimeo）——
    # 站点检索等到超时之后，通用池才从零开始跑一遍，耗时直接相加。
    # 它们之间没有任何依赖，没理由排队。
    sources: dict[str, dict] = {}
    groups: list[list[dict]] = []
    scoped_err: str | None = None
    pool_err: str | None = None

    workers = 2 + max(1, len(extra_sources))
    with ThreadPoolExecutor(max_workers=workers) as pool_ex:
        f_scoped = pool_ex.submit(search_site_scoped, keyword, patterns,
                                  site_word, need)
        f_pool = pool_ex.submit(_pool_job)
        f_extras = [(name, pool_ex.submit(fn)) for name, fn in extra_sources]

        # 1) 站点检索（site: 限定 + 关键词拼站点名）
        try:
            scoped, scoped_err, detail = f_scoped.result()
        except Exception as e:  # noqa: BLE001
            scoped, detail = [], {}
            scoped_err = f"{type(e).__name__}: {e}"
        for k, v in (detail or {}).items():
            sources[f"站点检索·{k}"] = v
        if scoped:
            groups.append(list(scoped))

        # 2) 该站自带的公开检索入口（若有）
        for name, fut in f_extras:
            try:
                got, err = fut.result()
            except Exception as e:  # noqa: BLE001
                sources[name] = {"count": 0, "error": f"{type(e).__name__}: {e}"}
                continue
            got = filter_items_by_hosts(got or [], patterns)   # 再保险一次归属
            sources[name] = {"count": len(got), "error": err}
            if got:
                groups.append(got)

        # 3) 通用池（**不限域**的全网）：先筛出该站的条目，
        #    剩下的留作「兜底」用 —— 不再是「在同一个白名单里再找一遍」。
        try:
            r = f_pool.result()
        except Exception as e:  # noqa: BLE001
            r = {"items": [], "error": f"{type(e).__name__}: {e}"}
        pool_all = r.get("items") or []
        pool_err = r.get("error")
        pool = filter_items_by_hosts(pool_all, patterns)
        _pk = {dedupe_key(it.get("webpage_url") or "") for it in pool}
        broad_pool = [it for it in pool_all
                      if dedupe_key(it.get("webpage_url") or "") not in _pk]
        sources["通用池"] = {"count": len(pool), "raw": len(pool_all)}
        if pool:
            groups.append(pool)

    items = merge_items(*groups, limit=10 ** 6) if groups else []

    # 单站模式必须严格保持平台归属。
    # 过去为了“凑够数量”会把不限域全网结果塞进来，导致用户选「优酷」却看到
    # 腾讯视频、选「小红书」却下载 v.qq.com —— 平台筛选因此失去意义。
    # 全网补充只属于 web/domestic/foreign 聚合模式；明确单站时宁可结果少/为空，
    # 也不冒充该平台结果。

    if not items:
        thin = THIN_INDEX_SITES.get(platform)
        if thin:
            # 这类站实测两个引擎、两种手段全为 0 —— 如实解释，不让人以为工具坏了
            return {"ok": False, "platform": platform, "query": keyword,
                    "error": (f"「{thin}」的视频基本只在它自己的 App / 登录后才能看，"
                              f"公开网页里检索不到。换其它站点或选「全网」试试。"),
                    "sources": sources, "host_filtered": 0}
        return {"ok": False, "platform": platform, "query": keyword,
                "error": scoped_err or pool_err or
                         f"没有检索到属于「{label}」的视频。",
                "sources": sources, "host_filtered": 0}

    # 该站的结果优先，兜底（不限域全网补充）排在后面 —— 不能因为排序把
    # 「不是这个站的」内容顶到前面去。
    _own = [x for x in items if not (x.get("extra") or {}).get("broad_fallback")]
    _broad = [x for x in items if (x.get("extra") or {}).get("broad_fallback")]
    merged = (rank_by_relevance(_own, keyword)
              + rank_by_relevance(_broad, keyword))[:need]
    for i, it in enumerate(merged):
        it["index"] = i

    resp = {"ok": True, "query": keyword, "platform": platform,
            "count": len(merged), "items": merged, "sources": sources,
            "host_source": label}
    own_n = sum(1 for x in merged
                if not (x.get("extra") or {}).get("broad_fallback"))
    broad_n = len(merged) - own_n
    parts: list[str] = []
    if len(merged) < need and own_n:
        tried = "、".join(k for k, v in sources.items() if v.get("count"))
        parts.append(f"属于「{label}」的视频共找到 {own_n} 条"
                     f"（少于你要的 {need} 条）。已尝试：{tried or '多种检索方式'}。")
    if own_n == 0:
        thin = THIN_INDEX_SITES.get(platform)
        if thin:
            # 这类站实测两个引擎、两种手段全为 0 —— 先如实解释，再给全网兜底
            parts.append(f"「{thin}」的视频基本只在它自己的 App / 登录后才能看，"
                         "公开网页里检索不到。")
    if broad_n:
        where = "排在后面" if own_n else "就是上面这些"
        parts.append(f"其中 {broad_n} 条来自**全网**（不属于「{label}」，{where}）"
                     "—— 兜底不限于我们罗列的站点。")
    if parts:
        resp["hint"] = "".join(parts)
        resp["broad_fallback"] = broad_n
    return resp


def _search_by_host_dispatch(keyword: str, platform: str, count: int) -> dict:
    from .search_web_video import DOMESTIC_HOSTS, FOREIGN_HOSTS, SITE_HOSTS, SITE_LABELS

    if platform == "domestic":
        # 国内主流 = 多来源结果里的国内站点 + B 站官方搜索
        def _bili():
            from .search_bilibili import search_bilibili_videos

            return search_bilibili_videos(keyword, count)

        return _search_by_host(keyword, platform, count, DOMESTIC_HOSTS,
                               "国内主流",
                               (("bilibili", _bili),
                                ("站点名检索", _site_word_bundle(
                                    keyword, ("douyin", "qq", "sohu", "iqiyi"),
                                    count))))
    if platform == "foreign":
        # 国外主流 = 通用池里的国外站点 + YouTube 原生搜索 + Dailymotion 公开接口
        #            （成人向查询再并入成人站 —— 它们也是国外视频站，只是两个
        #             通用引擎都检索不到，只能主动去查）
        def _yt():
            return _ytdlp_search(keyword, "youtube", count)

        def _dm():
            from .search_web_video import search_dailymotion_videos

            return search_dailymotion_videos(keyword, count)

        extras: list[tuple] = [("youtube", _yt), ("dailymotion", _dm),
                               ("站点名检索", _site_word_bundle(
                                   keyword, ("tiktok", "instagram"), count))]
        if _is_adult_query(keyword):
            extras.append(("成人站点", lambda: _search_adult_plain(keyword, count)[:2]))
        return _search_by_host(keyword, platform, count, FOREIGN_HOSTS,
                               "国外主流", tuple(extras))
    pats = SITE_HOSTS.get(platform)
    if pats:
        # 单站：主动按站点检索（site: 限定 + 关键词拼站点名），而不是被动筛域名
        extra = ()
        if platform == "dailymotion":
            def _dm_one():
                from .search_web_video import search_dailymotion_videos

                return search_dailymotion_videos(keyword, count)

            extra = (("Dailymotion 公开接口", _dm_one),)
        return _search_site(keyword, platform, count, pats,
                            SITE_LABELS.get(platform, platform), extra)
    # 未知平台名：按全网处理，保持宽松
    return _search_web_plain(keyword, platform, count)


def _apply_download_history(resp: dict, keyword: str, hide_downloaded: bool) -> dict:
    """按「下载台账」隐藏下过的视频（与文件放在哪儿无关）。

    用户诉求：反复搜同一个关键词、每次挑不同的下，下次不该再看到已下过的。
    判据是**视频身份**（页面地址归一 / yt-dlp 的 extractor+id），不是文件路径 ——
    所以把已下载的视频挪到别处，依然算「下过」，不会重新冒出来。
    想再看到它们，关掉搜索筛选里的「隐藏已下载」即可。
    """
    if not hide_downloaded:
        return resp
    from . import history

    items = resp.get("items") or []
    if not items:
        return resp
    kept, dropped = history.filter_new(items)
    if not dropped:
        return resp

    resp["hidden_downloaded"] = dropped
    resp["count"] = len(kept)
    resp["items"] = kept
    for i, it in enumerate(kept):
        it["index"] = i
    if not kept:
        # 全被隐藏时不能说「没有搜到视频」—— 那是另一回事，会误导用户
        resp["hint"] = (f"搜到的 {dropped} 条都是你已经下载过的，已按「隐藏已下载」"
                        f"全部隐藏。想再看它们，把筛选里的「隐藏已下载」取消勾选。")
    else:
        resp["hint"] = (resp.get("hint") or "") + \
            (("　" if resp.get("hint") else "") +
             f"已隐藏 {dropped} 条你下载过的视频。")
    return resp


def _search_web_plain(keyword: str, platform: str, count: int) -> dict:
    """不限定站点的全网搜索（多来源聚合）。

    **成人向查询会额外并入成人站结果并排在前面**。为什么必须这样：
    实测（2026-09-22，关键词「angela white anal」）两个通用引擎都拿不到成人站内容 ——
      · DuckDuckGo 视频接口对成人内容彻底过滤（0 条，连 `site:pornhub.com …` 也是 0）；
      · Bing 视频搜索 30 条里 29 条是 YouTube，成人站 0 条；
      · 关安全搜索（Bing `adlt=off` / DDG `kp=-2`）实测均无效。
    所以用户搜成人内容时看到的全是采访、花絮。解决办法是**主动去成人站检索**
    （见 `app/search_adult.py`），并把这些结果排在前面 —— 这才是「优先从成人站返回」。
    """
    from .search_web_video import merge_items, search_web_videos

    need = max(1, count)
    adult_on = _is_adult_query(keyword)

    # 全网聚合与成人站检索并行（两者互不依赖）
    with ThreadPoolExecutor(max_workers=2) as pool_ex:
        f_web = pool_ex.submit(search_web_videos, keyword, need)
        f_adult = pool_ex.submit(_search_adult_plain, keyword, need) if adult_on else None

        try:
            r = f_web.result()
        except Exception as e:  # noqa: BLE001
            r = {"items": [], "sources": {}, "error": f"{type(e).__name__}: {e}"}
        if f_adult is not None:
            adult_items, adult_err, adult_detail = f_adult.result()
        else:
            adult_items, adult_err, adult_detail = [], None, {}

    items = r.get("items") or []
    sources = dict(r.get("sources") or {})
    if adult_on:
        detail = dict(adult_detail or {})
        if adult_err:
            detail["error"] = adult_err
        sources["成人站点"] = detail

    if adult_items:
        # 成人站结果排最前，其余来源去重后补在后面
        merged = merge_items(adult_items, items, limit=need)
    else:
        merged = items[:need]

    if not merged:
        return {"ok": False, "error": r.get("error") or adult_err,
                "platform": platform, "query": keyword, "sources": sources}

    for i, it in enumerate(merged):
        it["index"] = i

    resp = {"ok": True, "query": keyword, "platform": platform,
            "count": len(merged), "items": merged, "sources": sources}

    if adult_items:
        sites = "、".join(sorted({i.get("source_site") or "" for i in adult_items}
                                 - {""}))
        resp["hint"] = (f"识别为成人向搜索，已**优先展示成人站点**（{sites}）的 "
                        f"{len(adult_items)} 条结果。"
                        "主流视频站（YouTube 等）的结果排在后面。")

    if r.get("rewritten_query"):
        # 明确告诉用户用过改写查询，别让人以为搜的就是原词
        resp["rewritten_query"] = r["rewritten_query"]
        resp["hint"] = resp.get("hint") or (
            f"原词「{keyword}」取不到结果，已自动改用「{r['rewritten_query']}」重搜。")

    hits, blocked = _relevance_summary(merged, keyword)
    if blocked and not adult_items:
        resp["low_relevance"] = True
        resp["title_hits"] = hits
        resp["hint"] = (f"这批结果与关键词的匹配度很低（{hits}/{len(merged)} 条相关）。"
                        "各来源对该词的视频索引都不多，建议换个说法或加限定词再搜。")
    elif not resp.get("hint"):
        # 如实说明「哪一个来源被丢弃了」，不静默降级
        dropped = [k for k, v in sources.items() if v.get("dropped")]
        if dropped:
            resp["hint"] = ("来源 " + "、".join(dropped) +
                            " 返回的内容与关键词无关，已自动改用其它来源的结果。")
    return resp


def _is_adult_query(keyword: str) -> bool:
    try:
        from .search_adult import is_adult_query

        return is_adult_query(keyword)
    except Exception:  # noqa: BLE001
        return False


def _search_adult_plain(keyword: str, count: int) -> tuple[list[dict], str | None, dict]:
    """成人站检索（永不抛异常，失败返回空）。"""
    try:
        from .search_adult import search_adult_videos

        return search_adult_videos(keyword, count)
    except Exception as e:  # noqa: BLE001
        return [], f"{type(e).__name__}: {e}", {}


def _search_adult_only(keyword: str, platform: str, count: int) -> dict:
    """「成人站点」源：只返回成人站结果。"""
    need = max(1, count)
    items, err, detail = _search_adult_plain(keyword, need)
    sources: dict[str, dict] = {"成人站点": dict(detail or {})}
    if err:
        sources["成人站点"]["error"] = err
    if not items:
        return {"ok": False, "platform": platform, "query": keyword,
                "error": err or "成人站没有该关键词的结果", "sources": sources}
    for i, it in enumerate(items[:need]):
        it["index"] = i
    return {"ok": True, "query": keyword, "platform": platform,
            "count": len(items[:need]), "items": items[:need], "sources": sources}


def search_videos(keyword: str, platform: str = "web", count: int = 10,
                  hide_downloaded: bool = True) -> dict:
    """关键词搜视频：返回候选列表（标题/时长/封面/链接）。

    platform 取值（都能在**搜索开始前**选好，界面下拉里逐个列出）：
      web        全网：多来源聚合（DuckDuckGo 视频 + Bing 视频），逐源门控后合并去重
      domestic   国内主流：在全网结果里只保留国内视频站（B站/抖音/腾讯/优酷/爱奇艺…）
      foreign    国外主流：只保留国外视频站（YouTube/Vimeo/Dailymotion/TikTok/X…）
      youtube    YouTube：yt-dlp 原生搜索（失败时按域名从全网来源兜底）
      bilibili   Bilibili：官方搜索接口（失败时同上兜底）
      douyin / qq / youku / iqiyi / sohu / kuaishou / weibo / xiaohongshu /
      vimeo / dailymotion / tiktok / twitter / instagram / facebook：单站
      adult      成人站点：Pornhub / Eporner

    **成人向关键词**（如「angela white anal」）会自动优先返回成人站结果 ——
    实测两个通用引擎都拿不到成人站内容（DDG 彻底过滤、Bing 只给 YouTube 的花絮），
    只能主动去查（见 app/search_adult.py）。「全网」与「国外主流」都会并入。

    **兜底不限于上面罗列的站点**：某个源结果不足时会自动从**全网**补充，
    并如实标注哪些是兜底来的（这些条目带 `extra.broad_fallback`）。

    之所以默认走搜索引擎而不是 yt-dlp：yt-dlp 的搜索前缀只有
    ytsearch / bilisearch，原生只能搜 YouTube 与 Bilibili，对「搜全网」太窄。

    hide_downloaded：按下载台账隐藏已经下过的视频（默认开）。
    """
    keyword = (keyword or "").strip()
    p = (platform or "web").lower()
    if not keyword:
        return {"ok": False, "error": "请输入关键词", "platform": p, "query": keyword}

    if p in ("youtube", "yt"):
        resp = _search_platform(keyword, "youtube", count)
    elif p in ("bilibili", "bili"):
        resp = _search_platform(keyword, "bilibili", count)
    elif p in ("adult", "成人"):
        resp = _search_adult_only(keyword, "adult", count)
    elif p in ("web", "all", "bing", ""):
        resp = _search_web_plain(keyword, "web", count)
    else:
        resp = _search_by_host_dispatch(keyword, p, count)

    return _apply_download_history(resp, keyword, hide_downloaded)

