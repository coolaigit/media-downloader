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
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

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


def _base_ydl_opts(
    task_id: str | None = None,
    quiet: bool = True,
    cookies_file: str | None = None,
    cookies_from_browser: str | None = None,
    custom_headers: bool = False,
    include_playlist: bool = False,
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
                return f"{fid}+bestaudio/{fid}/best"
            return f"{fid}/best"

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
            base = f"bestvideo{ext_part}[height<={height_cap}]+bestaudio/best[height<={height_cap}]"
        else:
            # 无 ffmpeg：只能选自带音轨的完整格式，否则得到无声或无画面文件
            base = f"best{ext_part}[height<={height_cap}]/best[height<={height_cap}]"
        return base

    if quality == "worst":
        # 不能只写 "worst"：实测（2026-09-21）B 站下 `worst` 会报
        # "Requested format is not available" —— 该站只暴露 bestvideo/bestaudio 这类
        # 分离流，没有 `worst` 这个别名。改成带兜底链的选择器：
        # 最小分离流合并 -> 最小的单文件 -> 整体最小。
        if ffmpeg_available():
            return f"worstvideo{ext_part}+worstaudio/worst{ext_part}/worst"
        return f"worst{ext_part}/worst"
    if ffmpeg_available():
        return f"bestvideo{ext_part}+bestaudio/best{ext_part}"
    return f"best{ext_part}/best"


def _fallback_format_selector(container: str = "") -> str:
    """用户选的清晰度档位不可用时使用的兜底选择器。

    只求「能下下来」：分离流合并不成就退回整体最佳。实测 B 站选 `worst` 会报
    "Requested format is not available"，而 `bestvideo+bestaudio/best` 正常 ——
    所以这里的兜底以「最佳整体」为准，而不是继续在小档位里挑（那只是换一个报错）。
    """
    ext_part = f"[ext={container}]" if container else ""
    if ffmpeg_available():
        return f"bestvideo{ext_part}+bestaudio/best{ext_part}"
    return f"best{ext_part}/best"


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

    return {
        "ok": True,
        "is_playlist": False,
        "notice": last_cookie_note(),
        **_summarize_info(info),
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
) -> dict:
    """下载单个视频（或播放列表），返回结果摘要。

    on_progress(info: dict) 回调字段：
      status(downloading/finished/合并中...)、progressed_bytes、total_bytes、
      percent、speed、eta、filename、info
    抛出 DownloadCancelled 表示用户取消。
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    fmt = build_format_selector(quality, container, prefer_audio_only, max_height,
                                format_id=format_id)

    opts = _base_ydl_opts(
        task_id,
        quiet=True,
        cookies_file=cookies_file,
        cookies_from_browser=cookies_from_browser,
        include_playlist=include_playlist,
    )
    opts.update(
        {
            "format": fmt,
            "outtmpl": str(output_dir / filename_template),
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
        return {"ok": False, "error": _friendly_error(last_err), "url": url, "files": [],
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
        "note": fallback_note,
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

    # 兜底：仍不够数时把范围放开到**不限域的全网**。
    #
    # 原来兜底也带着 host_patterns，等于在同一个白名单里再找一遍 ——
    # 找不到还是找不到。用户明确要求「兜底是全网搜索，而不是仅仅限于我们
    # 罗列的这些主流视频网站」。这里用的是同一份全网数据里被域名筛掉的那部分，
    # **不额外发请求**；这些条目会在提示里如实说明「不属于该站」。
    if len(items) < need and broad_pool:
        # 给兜底条目打标记：最终列表要按标记把它们排在后面，提示里也按它计数
        # （不能用「合并前后长度差」——列表随后会被截断到 need，数字会对不上）
        for it in broad_pool:
            ex = dict(it.get("extra") or {})
            ex["broad_fallback"] = True
            it["extra"] = ex
        merged_all = merge_items(items, broad_pool, limit=10 ** 6)
        if len(merged_all) > len(items):
            items = merged_all

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

