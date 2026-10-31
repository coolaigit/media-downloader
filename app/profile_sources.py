# -*- coding: utf-8 -*-
"""Self-contained platform homepage/detail sources for MediaDownloader 2.0.

Runtime sources are bundled libraries (yt-dlp, gallery-dl, xhshow) plus the
application's own Playwright renderer. There is no external crawler process,
external Python environment, or project-path dependency.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests

from .config import RESOURCE_ROOT, HEADERS
from .concurrency import (GOVERNOR, KIND_METADATA, NETWORK, OK, RATE_LIMIT,
                          classify_error)
from . import browser_profile, platform_login, subproc

PLATFORM_NAMES = {k: v["name"] for k, v in platform_login.PLATFORMS.items()}


def platform_for_url(url: str) -> str:
    return platform_login.platform_for_url(url)


_COLLECTION_SEG_NAMES = frozenset((
    "videos", "shorts", "streams", "space", "profile", "profiles", "posts",
    "media", "channel", "channels", "user", "users", "c", "u", "home", "index",
    "people", "note", "notes", "video", "detail", "index.html", "main",
))


def creator_id_from_url(url: str) -> str:
    try:
        p = urlparse(url or "")
    except ValueError:
        return ""
    segs = [s for s in (p.path or "").split("/") if s]
    q = dict(kv.split("=", 1) for kv in (p.query or "").split("&") if "=" in kv)
    for k in ("sec_uid", "uid", "user_id", "id", "mid", "creator_id"):
        v = q.get(k)
        if v and len(v) >= 4:
            return v
    for seg in reversed(segs):
        s = seg.strip()
        if not s or s.lower() in _COLLECTION_SEG_NAMES or s.startswith(("#", "?")):
            continue
        if len(s) >= 6 or s.isdigit():
            return s
    return ""


_AUTHORITATIVE = frozenset(("dy", "ks", "xhs", "pin", "ig"))


def authoritative(url: str) -> bool:
    return platform_for_url(url) in _AUTHORITATIVE


def source_name(platform: str) -> str:
    if platform == "xhs":
        return "内置 xhshow"
    if platform in {"ig", "pin"}:
        return "内置 gallery-dl / yt-dlp"
    return "内置 yt-dlp / gallery-dl / 浏览器"


def status() -> dict:
    return {"self_contained": True, "external_runtime": False,
            "platforms": platform_login.all_status()}


def invalidate_cache() -> None:
    return None


def login_cached(platform: str) -> bool:
    return platform_login.ready(platform)


def login_hint(platform: str) -> str:
    st = platform_login.status(platform)
    name = st.get("name") or platform
    if st.get("relogin_required"):
        return f"{name} 登录态已失效，请到「平台登录中心」重新登录。"
    return f"{name} 当前未登录；如公开访问失败，请到「平台登录中心」登录一次。"


def _load_cookie_dict(keyword: str) -> tuple[dict, str]:
    key = "xhs" if "xiaohongshu" in keyword else ""
    if not key:
        return {}, ""
    ck = platform_login.requests_cookie_dict(key)
    f = platform_login.cookie_file_for_platform(key)
    return ck, f


_VIDEO_EXTS = {"mp4", "m3u8", "mov", "webm", "mkv", "flv"}


def _candidate_cookie_files(url: str = "") -> list[str]:
    """Cookies for a known platform come only from that platform's isolated login."""
    platform = platform_for_url(url)
    if platform:
        own = platform_login.cookie_file_for_platform(platform)
        return [own] if own else []
    return []


def _cookie_file_has_domain(cookie_file: str, keyword: str) -> bool:
    try:
        txt = Path(cookie_file).read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    return any(keyword in line and not (line.startswith("#") and not line.startswith("#HttpOnly_"))
               for line in txt.splitlines())


def gallery_dl_items(url: str, *, limit: int = 60, timeout: int = 300,
                     video_only: bool = False, retries: int = 0,
                     retry_wait: int = 60, on_note=None) -> dict:
    """gallery-dl 入口（带整体重试）。

    为什么要整体重试（2026-09-28）：Instagram 对自动化请求限流很凶
    （web_profile_info 持续 429，时好时坏），单次跑经常空手而归；
    限流窗口通常几分钟内解除，所以失败后等一段时间再整体重跑。
    """
    cookie_file = ""
    for f in _candidate_cookie_files(url):
        cookie_file = f
        break                      # 有就带上（Pinterest 匿名也能用，带上无害）
    argv_extra = ["-o", "sleep-429=30"]   # 429 时每 30 秒重试，别误报「用户不存在」
    res: dict = {}
    for attempt in range(1, max(1, int(retries)) + 1):
        res = _gallery_dl_once(url, limit=limit, timeout=timeout, video_only=video_only,
                               cookie_file=cookie_file, argv_extra=argv_extra,
                               on_note=on_note)
        if res.get("ok") or res.get("login_required") or attempt >= max(1, int(retries)):
            return res
        if on_note:
            on_note(f"第 {attempt} 次没有拿到条目（多半是限流），{retry_wait} 秒后重试…")
        time.sleep(retry_wait)
    return res


def _gallery_dl_once(url: str, *, limit: int, timeout: int, video_only: bool,
                     cookie_file: str, argv_extra: list, on_note=None) -> dict:
    """用 gallery-dl 的元数据模式（--dump-json，不落盘）拿一批直链条目。

    输出结构：JSON 数组，元素是 [kind, data]：
      kind=2 → 作品元数据 dict（标题等上下文）
      kind=3 → 下载地址字符串
    返回 {"ok", "items", "error", "login_required"}，条目结构与 list_creator_items 一致。
    """
    try:
        import gallery_dl  # noqa: F401
    except ImportError:
        return {"ok": False, "items": [], "login_required": False,
                "error": "本机未安装 gallery-dl（pip install gallery-dl）。"
                         "它负责 Pinterest / Instagram 的批量提取。"}
    # 要视频时必须扫更大的范围：画板/主页的文件是图和视频混排的，
    # 前 N 个文件可能全是图（实测 USKYLEDS/architecture 前 22 个全是图，
    # 视频 pin 混在后面）。只扫 limit 个会得出「没有视频」的错误结论。
    # 要视频时必须扫更大的范围：画板/主页的文件是图和视频混排的，
    # 前 N 个文件可能全是图（实测 USKYLEDS/architecture 前 22 个全是图，
    # 视频 pin 混在后面）。只扫 limit 个会得出「没有视频」的错误结论。
    # 大批量主页抓取不再把 200 当硬上限。视频混排场景仍会适当多扫，
    # 但给扫描范围设 2400 的保护上限，避免一次元数据读取无限膨胀。
    host0 = (urlparse(url).hostname or "").lower()
    weibo_video_mode = bool(
        video_only and (host0 == "weibo.com" or host0.endswith(".weibo.com"))
    )
    if weibo_video_mode:
        # Weibo galleries can contain many photos. --range counts individual media
        # files, so a 9-image post can consume nine slots before the next video.
        # Use post-range instead: it advances by Weibo posts, while --filter keeps
        # only video media in the dump. This makes "30 videos" mean roughly 30
        # video posts rather than "the first 30 files".
        post_scan = min(1200, max(int(limit) * 2, 60))
        argv = subproc.build_internal_command(
            "gallery-dl", "--dump-json",
            "--post-range", f"1-{post_scan}",
            "--filter", "extension in ('mp4','m3u8','webm','mov','flv')",
        )
        scan = post_scan
    else:
        scan = max(int(limit) * 4, 120) if video_only else max(1, min(int(limit), 2000))
        # 1C：经统一 dispatcher 调 gallery-dl（internal 子命令内部就是
        # gallery_dl.main()，CLI 参数/退出码/输出语义 100% 保持）——
        # frozen 下 sys.executable 是本程序 EXE，老的 `-m gallery_dl` 写法不成立
        argv = subproc.build_internal_command(
            "gallery-dl", "--dump-json", "--range", f"1-{min(scan, 2400)}"
        )
    if cookie_file:
        argv += ["--cookies", cookie_file]
    argv += list(argv_extra or [])
    # gallery-dl --dump-json does not recursively execute Queue/Dispatch messages.
    # Instagram user roots and Weibo user roots both dispatch to a concrete child
    # extractor first, so normalize only the collection root URL to a concrete
    # gallery-dl listing endpoint. This is gallery-dl routing, not DOM scraping.
    run_url = url
    try:
        pu = urlparse(url)
        host = (pu.hostname or "").lower()
        segs = [x for x in (pu.path or "").split("/") if x]
        if (host == "instagram.com" or host.endswith(".instagram.com")) and len(segs) == 1:
            run_url = f"{pu.scheme or 'https'}://{pu.netloc}/{segs[0]}/posts/"
        elif (host == "weibo.com" or host.endswith(".weibo.com")):
            q = (pu.query or "").lower()
            is_user_root = (
                (len(segs) == 1 and segs[0] not in {"detail", "status"})
                or (len(segs) == 2 and segs[0].lower() in {"u", "n", "p", "profile"})
            )
            if is_user_root and "tabtype=" not in q:
                sep = "&" if pu.query else "?"
                run_url = url + sep + "tabtype=feed"
    except Exception:
        run_url = url
    argv.append(run_url)
    if on_note:
        on_note("正在用 gallery-dl 读取该页的作品列表（成熟开源下载器，只取元数据不下载）…")
    try:
        from .win_job import run_bound
        # 父进程退出时一并收掉元数据子进程。
        proc = run_bound(argv, timeout=timeout, stdin=subprocess.DEVNULL,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         cwd=str(RESOURCE_ROOT),
                         env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    except subprocess.TimeoutExpired:
        return {"ok": False, "items": [], "login_required": False,
                "error": f"gallery-dl 超时（{timeout} 秒）"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "items": [], "login_required": False,
                "error": f"{type(e).__name__}: {e}"}
    err = (proc.stderr or b"").decode("utf-8", "replace")
    if proc.returncode != 0 and not proc.stdout:
        low = err.lower()
        login_required = ("login" in low or "auth" in low or "429" in low
                          or "rate" in low or "require" in low)
        first = next((l for l in err.splitlines() if l.strip()), "")[:220]
        return {"ok": False, "items": [], "login_required": login_required,
                "error": first or f"gallery-dl 退出码 {proc.returncode}"}
    try:
        arr = json.loads((proc.stdout or b"").decode("utf-8", "replace"))
    except json.JSONDecodeError:
        return {"ok": False, "items": [], "login_required": False,
                "error": "gallery-dl 输出无法解析" + (f"：{err[:160]}" if err else "")}
    items: list[dict] = []
    ctx: dict = {}
    seen: set[str] = set()
    for pair in arr if isinstance(arr, list) else []:
        if not isinstance(pair, list) or not pair:
            continue
        kind, data = pair[0], pair[1] if len(pair) > 1 else None
        if kind == 2 and isinstance(data, dict):
            # 取作品封面：卡片没有预览图用户会以为「下载的东西是坏的」（实测反馈）。
            # Pinterest 的封面在 images.<尺寸>.url，优先高清档；没有就兜底找
            # 元数据里第一张 i.pinimg.com 的图（story pin 的封面埋得更深）。
            cover = ""
            # Instagram/gallery-dl 常见字段；Pinterest 仍继续走下面原有 images 逻辑。
            for k in ("display_url", "thumbnail", "preview_url", "image_url", "thumb"):
                v = data.get(k)
                if isinstance(v, str) and v.startswith("http"):
                    cover = v
                    break
            imgs = data.get("images")
            if isinstance(imgs, dict):
                for pref in ("orig", "1200x", "736x", "600x", "474x"):
                    v = imgs.get(pref)
                    if isinstance(v, dict) and (v.get("url") or "").startswith("http"):
                        cover = v["url"]
                        break
                if not cover:
                    for v in imgs.values():
                        if isinstance(v, dict) and (v.get("url") or "").startswith("http"):
                            cover = v["url"]
                            break
            if not cover:
                m = re.search(r'https:\\?/\\?/i\.pinimg\.com[^"\\\\ ]+?\.(?:jpg|png|webp)',
                              json.dumps(data, ensure_ascii=False))
                if m:
                    cover = m.group(0).replace("\\/", "/")
            page_info = data.get("page_info") if isinstance(data.get("page_info"), dict) else {}
            media_info = page_info.get("media_info") if isinstance(page_info.get("media_info"), dict) else {}
            if not cover:
                big = media_info.get("big_pic_info") if isinstance(media_info.get("big_pic_info"), dict) else {}
                for key in ("pic_big", "pic_middle", "pic_small"):
                    node = big.get(key)
                    if isinstance(node, dict) and str(node.get("url") or "").startswith("http"):
                        cover = str(node.get("url"))
                        break
            if not cover:
                pp = page_info.get("page_pic")
                if isinstance(pp, dict):
                    cover = str(pp.get("url") or pp.get("pic_big") or "")
                elif isinstance(pp, str):
                    cover = pp
            user = data.get("user") if isinstance(data.get("user"), dict) else {}
            uid = str(user.get("idstr") or user.get("id") or page_info.get("author_id") or "").strip()
            mblogid = str(data.get("mblogid") or "").strip()
            post_url = f"https://weibo.com/{uid}/{mblogid}" if uid and mblogid else ""
            ctx = {
                "title": (data.get("title") or data.get("grid_title")
                          or data.get("text_raw") or page_info.get("content2")
                          or data.get("full_name") or data.get("username") or ""),
                "cover": cover,
                "post_url": post_url,
                "uploader": str(user.get("screen_name") or user.get("name") or ""),
            }
        elif kind == 3 and isinstance(data, str):
            meta = pair[2] if len(pair) > 2 and isinstance(pair[2], dict) else {}
            # Instagram 的 video 项是 ytdl:<作品页>/1.mp4：这是 gallery-dl 给 yt-dlp 的
            # 交接标记，不是真实 MP4。必须还原为作品页，让我们自己的 yt-dlp + 登录态处理。
            ytdl = re.match(r"^ytdl:(https?://.+)", data, re.I)
            if ytdl and "instagram.com" in ytdl.group(1).lower():
                page = (meta.get("post_url") or ytdl.group(1)).strip()
                page = re.sub(r"/\d+\.mp4(?:\?.*)?$", "/", page)
                key = "page:" + page
                if page and key not in seen:
                    seen.add(key)
                    items.append({
                        "page_url": page,
                        "direct_url": "",
                        "variants": [],
                        "title": (meta.get("description") or meta.get("fullname") or
                                  ctx.get("title") or "").strip()[:150],
                        "cover": (meta.get("display_url") or ctx.get("cover") or "").strip(),
                        "uploader": (meta.get("username") or "").strip(),
                        "site": "instagram.com",
                        "kind_hint": "video",
                    })
                continue
            # Pinterest 等的 ytdl: 前缀后面是真实 HLS/媒体地址，保留原行为。
            m = re.match(r"^[a-z][a-z0-9_]*:(https?://.+)", data, re.I)
            data = m.group(1) if m else data
            if not data.startswith("http") or data in seen:
                continue
            seen.add(data)
            ext = data.rsplit("?", 1)[0].rsplit(".", 1)[-1].lower()
            kind_i = "video" if ext in _VIDEO_EXTS else "image"
            source_host = (urlparse(url).hostname or "").replace("www.", "")
            page_url = str(ctx.get("post_url") or url)
            # Weibo video CDN URLs are short-lived signed links (Expires/ssig).
            # Keep the canonical post page as the downloadable item so yt-dlp resolves
            # a fresh URL at click time instead of failing later with HTTP 403.
            weibo_page_video = source_host.endswith("weibo.com") and kind_i == "video" and bool(ctx.get("post_url"))
            items.append({
                "page_url": page_url,
                "direct_url": "" if weibo_page_video else data,
                "variants": [data] if weibo_page_video else [],
                "title": ctx.get("title") or "",
                "cover": ctx.get("cover") or "",
                "uploader": ctx.get("uploader") or "",
                "site": source_host,
                "kind_hint": kind_i,
            })
    if video_only:
        vids = [it for it in items if it.get("kind_hint") == "video"]
        if not vids and not items:
            # 一个文件都没拿到：这不是「没有视频」，而是登录墙 / 风控 / 地址问题
            low = err.lower()
            login_required = ("login" in low or "auth" in low or "429" in low
                              or "rate" in low or "require" in low
                              or "could not be found" in low)
            first = next((l for l in err.splitlines() if l.strip()), "")[:200]
            return {"ok": False, "items": [], "login_required": login_required,
                    "error": first or "该页一个文件都没拿到（可能需要登录或被风控）"}
        if not vids:
            return {"ok": False, "items": [], "login_required": False,
                    "error": (f"该页前 {min(scan, 400)} 个文件里没有视频（图片 {len(items)} 个）。"
                              "视频 pin 可能排得更靠后，或这个页确实没有视频。")}
        return {"ok": True, "items": vids[:limit], "error": "", "login_required": False}
    if not items:
        # 元数据模式 0 条：多半是登录墙 / 风控 / 地址问题，给可操作的错误
        low = err.lower()
        login_required = ("login" in low or "auth" in low or "429" in low
                          or "rate" in low or "require" in low
                          or "could not be found" in low)
        # 真登录错误（不含 429/限流——那是 IP 风控，不是登录态问题）要
        # 标记失效，否则平台登录中心一直显示「已登录」，用户不知道要重登。
        _plat = platform_for_url(url)
        if (_plat and login_required
                and platform_login.looks_like_auth_error(err)
                and platform_login.ready(_plat)):
            platform_login.mark_invalid(_plat, err[:300])
        first = next((l for l in err.splitlines() if l.strip()), "")[:200]
        return {"ok": False, "items": [], "login_required": login_required,
                "error": first or "gallery-dl 没有返回任何条目（该页可能为空、需要登录，或地址写错）"}
    return {"ok": bool(items), "items": items[:limit], "error": "",
            "login_required": False}




_XHS_USER_API = "https://edith.xiaohongshu.com/api/sns/web/v1/user_posted"


def xhs_author_items(url: str, *, limit: int = 30, timeout: int = 90,
                     on_note=None) -> dict:
    """用签名的官方接口拿小红书账号的作品列表（含每篇笔记的 xsec_token）。

    返回的条目是**笔记页地址**（带令牌），由既有下钻逻辑逐篇打开取视频 ——
    实测带令牌的笔记页纯 HTTP 就能拿到 mp4 主地址（无需渲染）。
    登录要求：接口要 web_session（扫码登录一次）；没有登录态时如实返回 login_required。
    """
    try:
        from xhshow import Xhshow
    except ImportError:
        return {"ok": False, "items": [], "login_required": False,
                "error": "本机未安装 xhshow（pip install xhshow）——"
                         "它负责小红书接口签名。"}
    uid = creator_id_from_url(url)
    if not uid:
        return {"ok": False, "items": [], "login_required": False,
                "error": "没能从地址里识别出作者 ID（应为 /user/profile/<24位ID>）"}
    ck, _ck_file = _load_cookie_dict("xiaohongshu")
    if not ck.get("web_session"):
        return {"ok": False, "items": [], "login_required": True,
                "error": "小红书的作品列表接口要求登录态（web_session）。"
                         "请在「设置」→「打开浏览器去登录」选小红书扫码登录一次，"
                         "登录态会自动保存并长期复用。"}
    if on_note:
        on_note("正在用签名接口读取该账号的作品列表（官方接口 + 开源签名库 xhshow）…")
    params = {"num": 30, "cursor": "", "user_id": uid, "image_formats": "jpg"}
    signer = Xhshow()
    items: list[dict] = []
    cursor = ""
    for _page in range(max(1, (int(limit) + 29) // 30)):
        if cursor:
            params["cursor"] = cursor
        try:
            headers = signer.sign_headers_get(uri=_XHS_USER_API, cookies=ck, params=params)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "items": [], "login_required": False,
                    "error": f"签名失败：{e}"}
        headers.update({
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126.0.0.0 Safari/537.36",
            "accept": "application/json, text/plain, */*",
            "origin": "https://www.xiaohongshu.com",
            "referer": f"https://www.xiaohongshu.com/user/profile/{uid}",
        })
        try:
            # 代理 → 直连互为兜底（本机代理会偶发卡死 / ProxyError，实测
            # edith.xiaohongshu.com 走代理直接 ReadTimeout，直连秒回）。
            resp = None
            last_exc: Exception | None = None
            for _attempt, trust in enumerate((True, False), 1):
                try:
                    with requests.Session() as sess:
                        sess.trust_env = trust
                        sess.headers.update(headers)
                        resp = sess.get(_XHS_USER_API, params=params, cookies=ck,
                                        timeout=min(timeout, 30))
                    break
                except requests.exceptions.RequestException as e:
                    last_exc = e
                    continue
            if resp is None:
                raise last_exc or RuntimeError("请求失败")
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "items": [], "login_required": False,
                    "error": f"{type(e).__name__}: {e}（代理与直连都试过了）"}
        try:
            data = resp.json()
        except ValueError:
            return {"ok": False, "items": [], "login_required": False,
                    "error": f"接口返回非 JSON（HTTP {resp.status_code}）：{resp.text[:120]}"}
        if not data.get("success"):
            code = data.get("code")
            msg = (data.get("msg") or "")[:80]
            if code in (-100, -101):
                # -100「登录已过期」/ -101 都代表会话失效。此前只认 -101，
                # -100 落进通用报错（login_required=False），应用既不提示
                # 重新登录也不标记失效，还继续拿登录墙页面的演示视频充数。
                err = f"登录态已失效（{msg}）。请重新扫码登录一次小红书。"
                platform_login.mark_invalid("xhs", err)
                return {"ok": False, "items": [], "login_required": True,
                        "error": err}
            return {"ok": False, "items": [], "login_required": False,
                    "error": f"接口报错（code={code}）：{msg}"}
        notes = (data.get("data") or {}).get("notes") or []
        for n in notes:
            nid = n.get("note_id") or ""
            tok = n.get("xsec_token") or ""
            if not nid or not tok:
                continue
            page = (f"https://www.xiaohongshu.com/explore/{nid}"
                    f"?xsec_token={tok}&xsec_source=pc_user")
            cover = n.get("cover") if isinstance(n.get("cover"), dict) else {}
            user = n.get("user") if isinstance(n.get("user"), dict) else {}
            items.append({
                "page_url": page,
                "direct_url": "",
                "variants": [],
                "title": (n.get("display_title") or "").strip()[:120],
                "cover": (cover.get("url_default") or ""),
                "uploader": (user.get("nickname") or ""),
                "site": "xiaohongshu.com",
                "is_page": True,
            })
            if len(items) >= int(limit):
                break
        cursor = (data.get("data") or {}).get("cursor") or ""
        if not cursor or not notes or len(items) >= int(limit):
            break
    if not items:
        return {"ok": False, "items": [], "login_required": False,
                "error": ("接口正常但没有可解析的作品（可能该账号只有图文笔记，"
                          "或作品列表为空）。")}
    return {"ok": True, "items": items[:limit], "error": "", "login_required": False}


def xhs_detail_images(url: str, *, timeout: int = 60) -> dict:
    """用签名详情接口读取一篇小红书图文笔记里的真实图片。

    只返回 note_card.image_list，明确排除头像、站点 UI、推荐流图片等页面装饰资源。
    视频笔记返回 ok=True + items=[] + is_video=True，调用方可直接跳过。
    """
    try:
        from xhshow import Xhshow
        from urllib.parse import parse_qs
    except ImportError:
        return {"ok": False, "items": [], "error": "内置 xhshow 不可用"}

    try:
        pu = urlparse(url)
        segs = [x for x in (pu.path or "").split("/") if x]
        note_id = segs[-1] if segs else ""
        qs = parse_qs(pu.query or "")
        xsec_token = (qs.get("xsec_token") or [""])[0]
        xsec_source = (qs.get("xsec_source") or ["pc_user"])[0] or "pc_user"
    except Exception as e:
        return {"ok": False, "items": [], "error": f"小红书地址解析失败：{e}"}
    if not note_id or not xsec_token:
        return {"ok": False, "items": [],
                "error": "小红书笔记链接缺少 xsec_token；请重新抓取作者主页以取得完整链接。"}

    ck, _ = _load_cookie_dict("xiaohongshu")
    if not ck.get("web_session"):
        return {"ok": False, "items": [],
                "error": "小红书需要登录态。请到「平台登录中心」登录一次小红书。"}

    endpoint = "https://edith.xiaohongshu.com/api/sns/web/v1/feed"
    payload = {
        "source_note_id": note_id,
        "image_formats": ["jpg", "webp", "avif"],
        "extra": {"need_body_topic": 1},
        "xsec_source": xsec_source,
        "xsec_token": xsec_token,
    }
    try:
        headers = Xhshow().sign_headers_post(
            uri=endpoint, cookies=ck, payload=payload, x_rap=True)
        headers.update({
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126.0.0.0 Safari/537.36",
            "accept": "application/json, text/plain, */*",
            "origin": "https://www.xiaohongshu.com",
            "referer": url,
        })
        resp = None
        last_exc = None
        for trust in (True, False):
            try:
                with requests.Session() as sess:
                    sess.trust_env = trust
                    resp = sess.post(endpoint, json=payload, cookies=ck, headers=headers,
                                     timeout=min(int(timeout), 30))
                break
            except requests.exceptions.RequestException as e:
                last_exc = e
        if resp is None:
            raise last_exc or RuntimeError("请求失败")
        data = resp.json()
    except Exception as e:
        return {"ok": False, "items": [],
                "error": f"小红书详情接口失败：{type(e).__name__}: {e}"}

    if not data.get("success"):
        code = data.get("code")
        msg = (data.get("msg") or "")[:100]
        err = f"小红书详情接口报错（code={code}）：{msg}"
        lr = bool(code in (-100, -101) or platform_login.looks_like_auth_error(err))
        if lr:
            platform_login.mark_invalid("xhs", err)
        return {"ok": False, "items": [], "error": err, "login_required": lr}

    entries = (data.get("data") or {}).get("items") or []
    card = (entries[0].get("note_card")
            if entries and isinstance(entries[0], dict) else None) or {}
    if card.get("type") == "video":
        return {"ok": True, "items": [], "is_video": True, "error": ""}

    title = (card.get("title") or card.get("desc") or "").strip()[:150]
    user = card.get("user") if isinstance(card.get("user"), dict) else {}
    uploader = (user.get("nickname") or user.get("nick_name") or "").strip()
    out, seen = [], set()
    images = card.get("image_list") or []
    for idx, im in enumerate(images if isinstance(images, list) else []):
        if not isinstance(im, dict):
            continue
        # WB_DFT/url_default 是正文图片的默认高质量档；没有时才退 WB_PRV。
        direct = str(im.get("url_default") or "").strip()
        if not direct:
            infos = im.get("info_list") or []
            for scene in ("WB_DFT", "WB_PRV"):
                hit = next((x for x in infos
                            if isinstance(x, dict)
                            and x.get("image_scene") == scene
                            and str(x.get("url") or "").startswith("http")), None)
                if hit:
                    direct = str(hit.get("url") or "").strip()
                    break
        direct = direct or str(im.get("url_pre") or im.get("url") or "").strip()
        if not direct.startswith("http") or direct in seen:
            continue
        seen.add(direct)
        out.append({
            "page_url": url,
            "direct_url": direct,
            "variants": [],
            "title": title,
            "cover": direct,
            "uploader": uploader,
            "site": "xiaohongshu.com",
            "kind_hint": "image",
            "width": im.get("width"),
            "height": im.get("height"),
            "index": idx + 1,
        })
    if not out:
        return {"ok": False, "items": [], "error": "图文笔记详情接口成功，但没有找到正文图片。"}
    return {"ok": True, "items": out, "is_video": False, "error": ""}



def xhs_author_image_items(url: str, *, limit: int = 30, on_note=None) -> dict:
    """Read only body images from XHS author notes via signed detail API."""
    from concurrent.futures import ThreadPoolExecutor

    max_media = max(1, min(int(limit or 30), 2000))
    # Scan beyond the requested media count so video notes do not starve image results.
    # Keep a hard ceiling to avoid excessive detail requests on very large profiles.
    note_scan = min(max(max_media * 2, 30), 120)
    author = xhs_author_items(url, limit=note_scan, on_note=on_note)
    if not author.get("ok"):
        return {"ok": False, "items": [],
                "login_required": bool(author.get("login_required")),
                "error": str(author.get("error") or "XHS author list failed")}

    notes = list(author.get("items") or [])
    if on_note:
        on_note(f"\u5df2\u53d6\u5f97 {len(notes)} \u7bc7\u4f5c\u54c1\uff0c\u6b63\u5728\u9010\u7bc7\u8bfb\u53d6\u56fe\u6587\u7b14\u8bb0\u6b63\u6587\u56fe\u7247\u2026")
    out: list[dict] = []
    failed = 0
    videos = 0
    checked = 0

    with ThreadPoolExecutor(max_workers=6) as pool:
        for off in range(0, len(notes), 6):
            batch = notes[off:off + 6]
            work = []
            for row in batch:
                page = str(row.get("page_url") or "").strip()
                if page:
                    work.append((row, pool.submit(xhs_detail_images, page)))
            for row, fut in work:
                checked += 1
                try:
                    detail = fut.result()
                except Exception:
                    failed += 1
                    continue
                if not detail.get("ok"):
                    failed += 1
                    continue
                if detail.get("is_video"):
                    videos += 1
                    continue
                for img in detail.get("items") or []:
                    direct = str(img.get("direct_url") or "").strip()
                    if not direct:
                        continue
                    title = str(img.get("title") or row.get("title") or "").strip()
                    idx = int(img.get("index") or 0)
                    out.append({
                        "page_url": str(img.get("page_url") or row.get("page_url") or ""),
                        "direct_url": direct,
                        "variants": img.get("variants") or [],
                        "title": (f"{title} #{idx}" if title and idx else title),
                        "cover": direct,
                        "uploader": img.get("uploader") or row.get("uploader") or "",
                        "site": "xiaohongshu.com",
                        "kind_hint": "image",
                        "width": img.get("width"),
                        "height": img.get("height"),
                        "note_image_index": idx,
                    })
                    if len(out) >= max_media:
                        break
                if len(out) >= max_media:
                    break
            if on_note:
                on_note(f"\u5df2\u68c0\u67e5 {checked}/{len(notes)} \u7bc7\uff0c\u6b63\u6587\u56fe\u7247 {len(out)} \u5f20")
            if len(out) >= max_media:
                break

    note = (f"\u5df2\u4ece\u5c0f\u7ea2\u4e66\u7b14\u8bb0\u8be6\u60c5\u63a5\u53e3\u63d0\u53d6 {len(out)} \u5f20\u6b63\u6587\u56fe\u7247\uff1b"
            f"\u68c0\u67e5 {checked} \u7bc7\uff0c\u8df3\u8fc7\u89c6\u9891\u7b14\u8bb0 {videos} \u7bc7\uff0c\u8be6\u60c5\u5931\u8d25 {failed} \u7bc7\u3002")
    if note_scan >= 120 and len(out) < max_media:
        note += " \u4e3a\u63a7\u5236\u5e73\u53f0\u8bf7\u6c42\u91cf\uff0c\u672c\u8f6e\u6700\u591a\u626b\u63cf 120 \u7bc7\u4f5c\u54c1\u3002"
    return {"ok": bool(out), "items": out[:max_media], "login_required": False,
            "error": "" if out else "\u6ca1\u6709\u627e\u5230\u56fe\u6587\u7b14\u8bb0\u6b63\u6587\u56fe\u7247\u3002", "note": note}


def _xhs_signed_detail_item(url: str, *, timeout: int = 60) -> dict:
    """用 xhshow + 该平台独立登录 cookie 直接解析一条小红书笔记。"""
    try:
        from xhshow import Xhshow
        from urllib.parse import parse_qs
    except ImportError:
        return {"ok": False, "item": None, "error": "内置 xhshow 不可用"}

    try:
        pu = urlparse(url)
        segs = [x for x in (pu.path or "").split("/") if x]
        note_id = segs[-1] if segs else ""
        qs = parse_qs(pu.query or "")
        xsec_token = (qs.get("xsec_token") or [""])[0]
        xsec_source = (qs.get("xsec_source") or ["pc_search"])[0] or "pc_search"
    except Exception as e:
        return {"ok": False, "item": None, "error": f"小红书地址解析失败：{e}"}
    if not note_id or not xsec_token:
        return {"ok": False, "item": None,
                "error": "小红书单条链接缺少 xsec_token；请使用浏览器分享/地址栏中的完整链接。"}

    ck, _ = _load_cookie_dict("xiaohongshu")
    if not ck.get("web_session"):
        return {"ok": False, "item": None,
                "error": "小红书需要登录态。请在「设置」→「打开浏览器去登录」登录一次小红书。"}

    endpoint = "https://edith.xiaohongshu.com/api/sns/web/v1/feed"
    payload = {
        "source_note_id": note_id,
        "image_formats": ["jpg", "webp", "avif"],
        "extra": {"need_body_topic": 1},
        "xsec_source": xsec_source,
        "xsec_token": xsec_token,
    }
    try:
        headers = Xhshow().sign_headers_post(uri=endpoint, cookies=ck, payload=payload, x_rap=True)
        headers.update({
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126.0.0.0 Safari/537.36",
            "accept": "application/json, text/plain, */*",
            "origin": "https://www.xiaohongshu.com",
            "referer": url,
        })
        resp = None
        last_exc = None
        for trust in (True, False):
            try:
                with requests.Session() as sess:
                    sess.trust_env = trust
                    resp = sess.post(endpoint, json=payload, cookies=ck, headers=headers,
                                     timeout=min(int(timeout), 30))
                break
            except requests.exceptions.RequestException as e:
                last_exc = e
        if resp is None:
            raise last_exc or RuntimeError("请求失败")
        data = resp.json()
    except Exception as e:
        return {"ok": False, "item": None, "error": f"小红书详情接口失败：{type(e).__name__}: {e}"}

    if not data.get("success"):
        code = data.get("code")
        msg = (data.get("msg") or "")[:100]
        lr = bool(code in (-100, -101) or platform_login.looks_like_auth_error(
            f"code={code}：{msg}"))
        if lr:
            platform_login.mark_invalid("xhs", f"小红书详情接口报错（code={code}）：{msg}")
        return {"ok": False, "item": None,
                "error": f"小红书详情接口报错（code={code}）：{msg}",
                "login_required": lr}

    entries = (data.get("data") or {}).get("items") or []
    card = (entries[0].get("note_card") if entries and isinstance(entries[0], dict) else None) or {}
    if card.get("type") != "video":
        return {"ok": False, "item": None, "error": "这条小红书笔记不是视频笔记。"}

    urls = []
    video = card.get("video") if isinstance(card.get("video"), dict) else {}
    media_v2 = video.get("media_v2")
    try:
        mv = json.loads(media_v2) if isinstance(media_v2, str) else (media_v2 or {})
    except (TypeError, ValueError):
        mv = {}
    stream = ((mv.get("video") or {}).get("stream") or {}) if isinstance(mv, dict) else {}
    for codec_group in stream.values() if isinstance(stream, dict) else []:
        if not isinstance(codec_group, list):
            continue
        for fmt in codec_group:
            if not isinstance(fmt, dict):
                continue
            u = (fmt.get("master_url") or "").strip()
            if u and u not in urls:
                urls.append(u)
            for bu in fmt.get("backup_urls") or []:
                if isinstance(bu, str) and bu.startswith("http") and bu not in urls:
                    urls.append(bu)
    # 老结构兜底：递归找 master_url。
    if not urls:
        def walk(obj):
            if isinstance(obj, dict):
                u = obj.get("master_url")
                if isinstance(u, str) and u.startswith("http") and u not in urls:
                    urls.append(u)
                for v in obj.values():
                    walk(v)
            elif isinstance(obj, list):
                for v in obj:
                    walk(v)
        walk(video)

    if not urls:
        return {"ok": False, "item": None, "error": "详情接口成功，但没有找到视频流地址。"}

    cover = ""
    images = card.get("image_list") or []
    for im in images if isinstance(images, list) else []:
        if not isinstance(im, dict):
            continue
        for k in ("url_default", "url_pre", "url"):
            v = im.get(k)
            if isinstance(v, str) and v.startswith("http"):
                cover = v
                break
        if cover:
            break
    user = card.get("user") if isinstance(card.get("user"), dict) else {}
    item = {
        "page_url": url,
        "direct_url": urls[0],
        "variants": urls[1:],
        "title": (card.get("title") or card.get("desc") or "").strip()[:150],
        "cover": cover,
        "uploader": (user.get("nickname") or user.get("nick_name") or "").strip(),
        "site": "xiaohongshu.com",
        "duration": ((video.get("capa") or {}).get("duration")
                     if isinstance(video.get("capa"), dict) else None),
    }
    return {"ok": True, "item": item, "error": "", "source": "xhshow"}



def xhs_detail_item(url: str, *, timeout: int = 300, on_note=None) -> dict:
    direct = _xhs_signed_detail_item(url, timeout=min(timeout, 60))
    if direct.get("ok"):
        return direct
    msg = str(direct.get("error") or "")
    if platform_login.looks_like_auth_error(msg):
        platform_login.mark_invalid("xhs", msg)
        direct["error"] = "小红书登录态已失效，请到「平台登录中心」重新登录。" + (f" {msg}" if msg else "")
    return direct


# ---------- 小红书发现页 / 搜索页（V3.0 Phase 5）----------
# 主页有 /user/profile/<id> 可以按用户拉列表；发现页和搜索页没有「用户」这个概念，
# 只能先拿到一批笔记引用（note_id + xsec_token），再逐篇下钻。
# 两条接口都走 xhshow 签名，和 xhs_author_items 同一套机制。
_XHS_HOMEFEED_API = "https://edith.xiaohongshu.com/api/sns/web/v1/homefeed"
_XHS_SEARCH_API = "https://edith.xiaohongshu.com/api/sns/web/v1/search/notes"


def xhs_page_kind(url: str) -> str:
    """判断一个小红书地址属于哪类页面：profile / feed / search / note / 空。

    为什么要分：主页拉列表、发现页/搜索页拉列表、单篇笔记取图片，
    三者用的接口和参数完全不同，混用只会报「缺少 xsec_token」这种看不懂的错。
    """
    try:
        pu = urlparse(url)
    except Exception:
        return ""
    host = (pu.hostname or "").lower()
    if "xiaohongshu.com" not in host:
        return ""
    path = (pu.path or "").lower()
    if "/user/profile/" in path:
        return "profile"
    if "/search_result" in path:
        return "search"
    if path.startswith("/explore") and path.strip("/") == "explore":
        return "feed"
    if "/explore/" in path or "/discovery/item/" in path:
        return "note"
    if path.strip("/") in ("", "explore"):
        return "feed"
    return ""


def _xhs_https(u: str) -> str:
    """小红书 CDN 返回的是 http 链接，统一升级到 https。

    只在发现页这条新链路上用，不动作者主页的既有行为（避免回归）。
    xhscdn.com 两种协议都能访问，但 https 在代理/重定向环境下更稳。
    """
    u = str(u or "")
    if u.startswith("http://") and "xhscdn.com" in u:
        return "https://" + u[len("http://"):]
    return u


def _xhs_note_url(note_id: str, token: str, source: str = "pc_feed") -> str:
    from urllib.parse import quote
    return (f"https://www.xiaohongshu.com/explore/{note_id}"
            f"?xsec_token={quote(str(token), safe='')}&xsec_source={source}")


def _xhs_api_post(endpoint: str, payload: dict, *, referer: str, cookies: dict,
                  timeout: int = 30) -> tuple[dict | None, str]:
    """小红书签名 POST 的统一封装（代理 ⇄ 直连互为兜底）。返回 (data, error)。"""
    try:
        from xhshow import Xhshow
    except ImportError:
        return None, "内置 xhshow 不可用"
    try:
        headers = Xhshow().sign_headers_post(
            uri=endpoint, cookies=cookies, payload=payload, x_rap=True)
    except Exception as e:  # noqa: BLE001
        return None, f"小红书签名失败：{e}"
    headers.update({
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126.0.0.0 Safari/537.36",
        "accept": "application/json, text/plain, */*",
        "content-type": "application/json;charset=UTF-8",
        "origin": "https://www.xiaohongshu.com",
        "referer": referer,
    })
    resp = None
    last_exc: Exception | None = None
    for trust in (True, False):
        try:
            with requests.Session() as sess:
                sess.trust_env = trust
                resp = sess.post(endpoint, json=payload, cookies=cookies,
                                 headers=headers, timeout=min(int(timeout), 30))
            break
        except requests.exceptions.RequestException as e:
            last_exc = e
    if resp is None:
        return None, f"小红书接口请求失败：{type(last_exc).__name__}: {last_exc}"
    try:
        data = resp.json()
    except Exception:
        return None, f"小红书接口返回非 JSON（HTTP {resp.status_code}）"
    if not data.get("success"):
        err = (f"小红书接口报错（code={data.get('code')}）："
               f"{str(data.get('msg') or '')[:100]}")
        if (data.get("code") in (-100, -101)
                or platform_login.looks_like_auth_error(err)):
            platform_login.mark_invalid("xhs", err)
        return None, err
    return data, ""


def xhs_feed_note_refs(url: str, *, limit: int = 30, timeout: int = 30,
                       on_note=None) -> dict:
    """发现页 / 搜索页 → 笔记引用列表（note_id + xsec_token + xsec_source）。

    返回 {"ok","refs":[{note_id,xsec_token,xsec_source,title}],"login_required","error"}。
    """
    from urllib.parse import parse_qs

    kind = xhs_page_kind(url)
    if kind not in {"feed", "search"}:
        return {"ok": False, "refs": [], "login_required": False,
                "error": f"不是发现页/搜索页地址（识别为 {kind or '未知'}）"}
    ck, _ = _load_cookie_dict("xiaohongshu")
    if not ck.get("web_session"):
        return {"ok": False, "refs": [], "login_required": True,
                "error": "小红书发现页需要登录态（web_session），"
                         "请到「平台登录」登录小红书一次。"}

    want = max(1, min(int(limit or 30), 300))
    refs: list[dict] = []
    seen: set[str] = set()

    def _take(items, source: str) -> int:
        added = 0
        for it in items if isinstance(items, list) else []:
            if not isinstance(it, dict):
                continue
            nid = str(it.get("id") or (it.get("note_card") or {}).get("note_id") or "")
            tok = str(it.get("xsec_token") or "")
            if not nid or not tok or nid in seen:
                continue
            seen.add(nid)
            card = it.get("note_card") if isinstance(it.get("note_card"), dict) else {}
            refs.append({
                "note_id": nid,
                "xsec_token": tok,
                "xsec_source": source,
                "title": str(card.get("display_title") or card.get("title") or "")[:120],
            })
            added += 1
        return added

    if kind == "search":
        qs = parse_qs(urlparse(url).query or "")
        keyword = str((qs.get("keyword") or qs.get("q") or [""])[0]).strip()
        if not keyword:
            return {"ok": False, "refs": [], "login_required": False,
                    "error": "搜索页地址里没有 keyword 参数"}
        if on_note:
            on_note(f"正在用签名接口搜索「{keyword}」并读取笔记列表…")
        # search_id 不能为空：实测传空字符串时接口 success=True 但 data 是空对象（0 条），
        # 传一个 24 位十六进制（与前端生成的一致）才能拿到 items。这是个很坑的静默失败。
        import random as _random
        search_id = "".join(_random.choices("0123456789abcdef", k=24))
        page = 1
        while len(refs) < want and page <= 8:
            payload = {
                "keyword": keyword, "page": page, "page_size": 20,
                "search_id": search_id, "sort": "general", "note_type": 0,
            }
            data, err = _xhs_api_post(
                _XHS_SEARCH_API, payload, referer=url, cookies=ck, timeout=timeout)
            if not data:
                if refs:
                    break
                return {"ok": False, "refs": [],
                        "login_required": platform_login.looks_like_auth_error(err),
                        "error": err}
            got = _take(((data.get("data") or {}).get("items") or []), "pc_search")
            if not got:
                break
            page += 1
    else:
        if on_note:
            on_note("正在用签名接口读取小红书发现页笔记列表…")
        cursor = ""
        rounds = 0
        while len(refs) < want and rounds < 8:
            rounds += 1
            payload = {
                "cursor_score": cursor, "num": 20, "refresh_type": 1,
                "note_index": len(refs), "unread_begin_note_id": "",
                "unread_end_note_id": "", "unread_note_count": 0,
                "category": "homefeed_recommend", "search_key": "",
                "need_num": 20, "image_formats": ["jpg", "webp", "avif"],
                "need_filter_image": False,
            }
            data, err = _xhs_api_post(
                _XHS_HOMEFEED_API, payload, referer="https://www.xiaohongshu.com/explore",
                cookies=ck, timeout=timeout)
            if not data:
                if refs:
                    break
                return {"ok": False, "refs": [],
                        "login_required": platform_login.looks_like_auth_error(err),
                        "error": err}
            body = data.get("data") or {}
            got = _take(body.get("items") or [], "pc_feed")
            cursor = str(body.get("cursor_score") or "")
            if not got or not cursor:
                break

    if not refs:
        return {"ok": False, "refs": [], "login_required": False,
                "error": "接口正常但没有解析出任何笔记（可能是账号无推荐内容或需要重新登录）。"}
    return {"ok": True, "refs": refs[:want], "login_required": False, "error": ""}


def xhs_feed_items(url: str, *, limit: int = 30, want_video: bool = False,
                   on_note=None) -> dict:
    """发现页 / 搜索页 → 逐篇下钻取图或视频。

    图片笔记走 xhs_detail_images（正文图，排除头像/UI），
    视频笔记走 xhs_detail_item（签名详情拿 mp4 直链）。
    视频模式下遇到纯图文笔记会自然跳过；图片模式下遇到视频笔记同理。
    """
    got = xhs_feed_note_refs(url, limit=max(int(limit or 30) * 2, 20), on_note=on_note)
    if not got.get("ok"):
        return {"ok": False, "items": [], "login_required": got.get("login_required", False),
                "error": got.get("error") or "没有拿到发现页笔记列表"}

    refs = got.get("refs") or []
    if on_note:
        on_note(f"发现页拿到 {len(refs)} 篇笔记，正在逐篇读取"
                f"{'视频' if want_video else '正文图片'}…")

    out: list[dict] = []
    videos = images = failed = skipped = 0
    for r in refs:
        if len(out) >= max(1, int(limit or 30)) and (videos if want_video else images):
            break
        note_url = _xhs_note_url(r["note_id"], r["xsec_token"],
                                 r.get("xsec_source") or "pc_feed")
        if want_video:
            res = xhs_detail_item(note_url)
            item = (res or {}).get("item") or {}
            direct = str(item.get("direct_url") or "")
            if not direct:
                # 接口成功但没有视频直链 = 这是一篇图文笔记，视频模式下正常跳过；
                # 只有接口本身失败才算 failed（否则统计会把「跳过」说成「失败」）。
                if (res or {}).get("ok"):
                    skipped += 1
                else:
                    failed += 1
                continue
            videos += 1
            out.append({
                "page_url": note_url,
                "direct_url": _xhs_https(direct),
                "variants": item.get("variants") or [],
                "title": str(item.get("title") or r.get("title") or "")[:150],
                "cover": _xhs_https(str(item.get("cover") or "")),
                "uploader": str(item.get("uploader") or ""),
                "site": "xiaohongshu.com",
                "duration": item.get("duration"),
                "kind_hint": "video",
            })
        else:
            res = xhs_detail_images(note_url)
            if res.get("is_video"):
                continue          # 视频笔记在图片模式里本来就该跳过，不算失败
            if not res.get("ok"):
                failed += 1
                continue
            for it in res.get("items") or []:
                it = dict(it)
                it["page_url"] = note_url
                it.setdefault("uploader", "")
                it["direct_url"] = _xhs_https(it.get("direct_url") or "")
                it["cover"] = _xhs_https(it.get("cover") or it.get("direct_url") or "")
                out.append(it)
                images += 1
                if len(out) >= max(1, int(limit or 30)):
                    break

    if not out:
        return {"ok": False, "items": [], "login_required": False,
                "error": f"发现页共 {len(refs)} 篇笔记，但没读到"
                         f"{'可下载的视频' if want_video else '正文图片'}"
                         f"（跳过 {skipped} 篇类型不符，失败 {failed} 篇）。"}
    return {"ok": True, "items": out[:max(1, int(limit or 30))], "login_required": False,
            "error": "", "scanned": len(refs), "failed": failed, "skipped": skipped}


def _best_thumb(entry: dict) -> str:
    """从 yt-dlp / 网页条目里挑一张最合适的封面。

    关键：yt-dlp 的 info 用的是 **thumbnails（列表）**，带 width/preference；
    而网页来源常用单值 thumbnail。原来的 `entry.get("thumbnail")` 对 yt-dlp 条目
    永远是空 —— 所以走 yt-dlp 列表提取器的站点（pornhub 的
    PornHubPagedVideoList、微博/抖音/TikTok 的部分路径）抓下来全都没有封面，
    但下载之后又能在「已下载」里看到封面（下载时解析的是完整 info）。
    实测 2026-10-24 用户反馈。
    """
    direct = str(entry.get("thumbnail") or "").strip()
    if direct.startswith("http"):
        return direct
    arr = entry.get("thumbnails")
    if not isinstance(arr, list):
        return ""
    best, best_score = "", -1.0
    for c in arr:
        if not isinstance(c, dict):
            continue
        u = str(c.get("url") or "").strip()
        if not u.startswith("http"):
            continue
        # preference 是最权威的排序依据；同分时按宽度取更大的那张
        try:
            pref = float(c.get("preference") or 0)
        except (TypeError, ValueError):
            pref = 0.0
        try:
            width = float(c.get("width") or 0)
        except (TypeError, ValueError):
            width = 0.0
        score = pref * 1e6 + width
        if score >= best_score:
            best, best_score = u, score
    return best


def _backfill_covers(page_url: str, items: list[dict]) -> int:
    """列表页补封面：抓一次 HTML，按视频 key 把缩略图对回去。

    为什么需要（2026-10-24 用户反馈 pornhub 抓取没缩略图）：
    yt-dlp 的列表模式 `extract_flat` 不解析详情，条目里常常只有
    `title / url / ie_key / _type` 四个字段 —— 缩略图完全没有。
    所以走 yt-dlp 列表的站点抓下来全是空封面，而下载之后又能看到封面
    （下载时解析的是完整 info）。

    做法：抓一次列表页 HTML，按 `data-video-vkey="..."` 切卡片，
    取卡片里第一张图，再用这个 key 去匹配条目的 URL。匹配不上就跳过，
    不会乱配 —— 宁可没封面，也不能挂一张别人的图。
    """
    if not page_url or not items:
        return 0
    resp = None
    for trust in (False, True):        # 先直连再代理：这类站点多数在国内可直连
        try:
            with requests.Session() as s:
                s.trust_env = trust
                resp = s.get(page_url,
                             headers={**HEADERS, "Accept-Language": "zh-CN,zh;q=0.9"},
                             timeout=20)
            break
        except requests.RequestException:
            continue
    if resp is None or resp.status_code != 200:
        return 0
    html = resp.text or ""

    thumb_map: dict[str, str] = {}
    for m in re.finditer(r'data-video-vkey="([^"]+)"(.*?)(?=data-video-vkey=|$)', html, re.S):
        vkey, chunk = m.group(1), (m.group(2) or "")[:6000]
        # 这类站点常把缩略图挂在 data-mediumthumb / data-thumbs 上，不是 <img src>；
        # 只有都没有时才退回 <img src>。实测 pornhub 2026-10：卡片里没有 <img>，
        # 但每个卡片块内有 data-mediumthumb。
        hit = (re.search(r'data-mediumthumb="(https?://[^"]+)"', chunk)
               or re.search(r'<img[^>]+src="(https?://[^"]+?\.(?:jpg|jpeg|png|webp))"', chunk))
        if hit:
            thumb_map[vkey] = hit.group(1)
    if not thumb_map:
        return 0

    filled = 0
    for it in items:
        if str(it.get("cover") or it.get("thumbnail") or "").strip():
            continue
        target = f'{it.get("page_url") or it.get("webpage_url") or ""} {it.get("direct_url") or ""} {it.get("url") or ""}'
        for vkey, thumb in thumb_map.items():
            if vkey and vkey in target:
                it["cover"] = thumb
                # 兼容 yt-dlp 原始条目（它用 thumbnail 这个键）
                if not str(it.get("thumbnail") or "").strip():
                    it["thumbnail"] = thumb
                filled += 1
                break
    return filled


def _entry_item(entry: dict, source_url: str) -> dict | None:
    page = str(entry.get("webpage_url") or entry.get("original_url") or entry.get("url") or "").strip()
    if page and not page.startswith("http"):
        page = ""
    direct = ""
    raw_url = str(entry.get("url") or "").strip()
    if raw_url.startswith("http") and any(raw_url.lower().split("?", 1)[0].endswith("." + x)
                                           for x in ("mp4", "webm", "m3u8", "mov", "flv")):
        direct = raw_url
    page = page or (source_url if direct else "")
    if not page and not direct:
        return None
    return {
        "page_url": page,
        "direct_url": direct,
        "variants": [],
        "title": str(entry.get("title") or entry.get("description") or "")[:150],
        "cover": _best_thumb(entry),
        "uploader": str(entry.get("uploader") or entry.get("channel") or ""),
        "site": (urlparse(page or source_url).hostname or "").replace("www.", ""),
        "duration": entry.get("duration"),
        "kind_hint": "video",
    }


def _netscape_cookie_dict(path: str) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path:
        return out
    try:
        for raw in Path(path).read_text(encoding="utf-8", errors="ignore").splitlines():
            if not raw or raw.startswith("#"):
                continue
            cols = raw.split("\t")
            if len(cols) >= 7 and cols[5]:
                out[cols[5]] = cols[6]
    except OSError:
        pass
    return out


def weibo_author_items(url: str, *, limit: int = 30, want_video: bool = False,
                       on_note=None) -> dict:
    """Read Weibo's own paged profile feed; count requested videos/images, not raw media slots."""
    uid = creator_id_from_url(url)
    if not uid:
        return {"ok": False, "items": [], "login_required": False,
                "error": "无法从微博主页地址识别 UID"}
    ck = platform_login.cookie_file_for_platform("wb") or ""
    cookies = _netscape_cookie_dict(ck)
    if not cookies:
        return {"ok": False, "items": [], "login_required": True,
                "error": "微博登录态不可用，请先到平台登录中心登录。"}

    sess = requests.Session()
    sess.cookies.update(cookies)
    headers = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 Chrome/154.0.0.0 Safari/537.36"),
        "Referer": f"https://weibo.com/u/{uid}",
        "X-Requested-With": "XMLHttpRequest",
    }
    # V3.0：微博的 ajax 接口除了 SUB cookie，还校验 X-XSRF-TOKEN 头。
    # 只带 cookie 不带这个头会直接 403（实测 2026-10-22：cookie 里 XSRF-TOKEN 明明存在，
    # 但 platform_login.status 只看 cookie 是否存在，所以界面显示「已登录」而接口拒绝）。
    _xsrf = str(cookies.get("XSRF-TOKEN") or "").strip()
    if _xsrf:
        headers["X-XSRF-TOKEN"] = _xsrf
    target = max(1, min(int(limit or 30), 2000))
    items: list[dict] = []
    seen: set[str] = set()
    empty_pages = 0
    max_pages = min(120, max(4, target // 8 + 8))

    if on_note:
        kind = "视频" if want_video else "图片"
        on_note(f"正在按微博作品页翻页读取{kind}，目标 {target} 个…")

    for page_no in range(1, max_pages + 1):
        try:
            r = sess.get(
                "https://weibo.com/ajax/statuses/mymblog",
                params={"uid": uid, "page": page_no, "feature": 0},
                headers=headers, timeout=18,
            )
        except Exception as e:
            if items:
                break
            return {"ok": False, "items": [], "login_required": False,
                    "error": f"微博作品流请求失败：{type(e).__name__}: {e}"}
        if r.status_code in (401, 403, 418):
            # 标记失效，否则平台登录页会一直显示「已登录」（它只看 cookie 在不在），
            # 用户完全不知道要去重新登录一次。实测 2026-10-22：cookie 全在但服务端 403。
            platform_login.mark_invalid(
                "wb", f"微博作品流返回 HTTP {r.status_code}，登录凭据已被服务端拒绝")
            return {"ok": False, "items": items, "login_required": True,
                    "error": f"微博作品流返回 HTTP {r.status_code}，登录态已失效，"
                             f"请到「平台登录」重新登录微博后再试。"}
        try:
            payload = r.json()
        except Exception:
            if items:
                break
            return {"ok": False, "items": [], "login_required": False,
                    "error": f"微博作品流返回非 JSON（HTTP {r.status_code}）"}
        data = payload.get("data") if isinstance(payload, dict) else {}
        rows = data.get("list") if isinstance(data, dict) else []
        if not isinstance(rows, list) or not rows:
            empty_pages += 1
            if empty_pages >= 2:
                break
            continue
        empty_pages = 0

        for post in rows:
            if not isinstance(post, dict):
                continue
            mblogid = str(post.get("mblogid") or "")
            if not mblogid:
                continue
            post_url = f"https://weibo.com/{uid}/{mblogid}"
            user = post.get("user") if isinstance(post.get("user"), dict) else {}
            uploader = str(user.get("screen_name") or "")
            pi = post.get("page_info") if isinstance(post.get("page_info"), dict) else {}
            mi = pi.get("media_info") if isinstance(pi.get("media_info"), dict) else {}
            title = str(post.get("text_raw") or pi.get("content2") or pi.get("content1") or "")
            title = re.sub(r"<[^>]+>", "", title).strip()[:150]

            if want_video:
                is_video = (
                    str(pi.get("object_type") or "").lower() == "video"
                    or str(mi.get("media_type") or "").lower() == "video"
                    or bool(mi.get("stream_url") or mi.get("stream_url_hd") or mi.get("h5_url"))
                )
                if not is_video or post_url in seen:
                    continue
                big = mi.get("big_pic_info") if isinstance(mi.get("big_pic_info"), dict) else {}
                cover = ""
                for key in ("pic_middle", "pic_big", "pic_small"):
                    node = big.get(key)
                    if isinstance(node, dict) and str(node.get("url") or "").startswith("http"):
                        cover = str(node.get("url"))
                        break
                if not cover:
                    pp = pi.get("page_pic")
                    if isinstance(pp, dict):
                        cover = str(pp.get("url") or "")
                    elif isinstance(pp, str):
                        cover = pp
                variants = []
                for key in ("stream_url_hd", "stream_url", "mp4_hd_url", "mp4_sd_url"):
                    v = str(mi.get(key) or "")
                    if v.startswith("http") and v not in variants:
                        variants.append(v)
                seen.add(post_url)
                items.append({
                    "page_url": post_url, "direct_url": "", "variants": variants,
                    "title": title or str(mi.get("name") or ""),
                    "cover": cover, "uploader": uploader, "site": "weibo.com",
                    "duration": mi.get("duration"), "kind_hint": "video",
                })
            else:
                pics = post.get("pic_infos") if isinstance(post.get("pic_infos"), dict) else {}
                for pic in pics.values():
                    if not isinstance(pic, dict):
                        continue
                    def purl(name):
                        node = pic.get(name)
                        return str(node.get("url") or "") if isinstance(node, dict) else ""
                    direct = purl("largest") or purl("original") or purl("large") or purl("mw2000")
                    cover = purl("bmiddle") or purl("thumbnail") or direct
                    if not direct or direct in seen:
                        continue
                    seen.add(direct)
                    items.append({
                        "page_url": post_url, "direct_url": direct, "variants": [],
                        "title": title, "cover": cover, "uploader": uploader,
                        "site": "weibo.com", "kind_hint": "image",
                    })
                    if len(items) >= target:
                        break
            if len(items) >= target:
                break

        if on_note and page_no % 2 == 0 and len(items) < target:
            on_note(f"微博已读取 {len(items)} 个，继续翻页…")
        if len(items) >= target:
            break

    return {
        "ok": bool(items), "items": items[:target], "login_required": False,
        "error": "" if items else ("微博作品流中没有找到视频" if want_video else "微博作品流中没有找到图片"),
    }


def _bili_enrich(items: list[dict], cookie_file: str, on_note=None) -> list[dict]:
    """Fill metadata for flat Bilibili space rows without N serial extracts."""
    if not items:
        return items
    cookies = _netscape_cookie_dict(cookie_file)
    headers = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 Chrome/154.0.0.0 Safari/537.36"),
        "Referer": "https://www.bilibili.com/",
        "Origin": "https://www.bilibili.com",
    }

    def work(idx_item):
        idx, it = idx_item
        page = str(it.get("page_url") or "")
        m = re.search(r"/video/(BV[0-9A-Za-z]+)", page, re.I)
        if not m:
            return idx, None
        # 真正同时跑几个由闸门决定：B站 view API 对瞬时并发敏感，
        # bili/metadata 闸门保守起步 4 路，稳定后自己升到用户设的上限。
        # 线程池按上限开，多余的 worker 会停在这里等名额，不会去撞 API。
        if not gate.acquire():
            return idx, None
        signal = NETWORK
        result = None
        try:
            for attempt in range(3):
                try:
                    r = requests.get(
                        "https://api.bilibili.com/x/web-interface/view",
                        params={"bvid": m.group(1)}, headers=headers, cookies=cookies,
                        timeout=12,
                    )
                    payload = r.json() if r.ok else {}
                    data = (payload.get("data") or {}) if isinstance(payload, dict) else {}
                    code = payload.get("code") if isinstance(payload, dict) else None
                    if isinstance(data, dict) and data:
                        owner = data.get("owner") if isinstance(data.get("owner"), dict) else {}
                        cover = str(data.get("pic") or "")
                        if cover.startswith("http://"):
                            cover = "https://" + cover[len("http://"):]
                        result = {
                            "title": str(data.get("title") or "")[:150],
                            "cover": cover,
                            "duration": data.get("duration"),
                            "uploader": str(owner.get("name") or ""),
                            "page_url": f"https://www.bilibili.com/video/{data.get('bvid') or m.group(1)}",
                        }
                        signal = OK
                        break
                    # 412 / 429 / -412 是明确的频控；其它空响应只当普通失败，
                    # 不能让一次偶发空包把并发永久压下去。
                    if r.status_code in {412, 429} or code == -412:
                        signal = RATE_LIMIT
                    if attempt < 2 and (r.status_code in {412, 429} or code not in (0, None)):
                        time.sleep(0.45 * (attempt + 1))
                        continue
                except Exception as e:  # noqa: BLE001
                    signal = classify_error(str(e))
                    if attempt < 2:
                        time.sleep(0.35 * (attempt + 1))
                        continue
                break
            return idx, result
        finally:
            gate.release(signal)

    if on_note:
        on_note(f"已列出 {len(items)} 个 B站作品，正在并发补标题和缩略图…")
    # V2.5 在这里硬限 4 路（16 路会整批返回空 → 「30 条都有标题但没缩略图」）。
    # V3.0 保留这个保护，但改成「保守起步 + 稳定后自动升」，不再永久卡在 4。
    #
    # 为什么要看 cookies：实测（2026-10-21）**匿名**打 view API，并发 3 就返回 412，
    # 连单条串行都能被拦 —— 匿名根本没有「安全并发」这回事。
    # V2.5 的「30/30 缩略图」是**带 Cookie** 时验证出来的，不能拿同样的数字去要求匿名访问。
    # 所以：有 Cookie 起步 4 路，没 Cookie 从 1 路起，稳定后照样升到上限。
    gate = GOVERNOR.gate("bili", KIND_METADATA, start_at=(4 if cookies else 1))
    workers = max(1, min(gate.ceiling, len(items)))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(work, pair) for pair in enumerate(items)]
        for f in as_completed(futures):
            idx, meta = f.result()
            if meta:
                items[idx].update({k: v for k, v in meta.items() if v not in ("", None)})
    return items


def ytdlp_items(url: str, *, limit: int = 30, on_note=None) -> dict:
    try:
        import yt_dlp
    except ImportError:
        return {"ok": False, "items": [], "login_required": False, "error": "内置 yt-dlp 不可用"}
    platform = platform_for_url(url)
    cookie = platform_login.cookie_file_for_platform(platform) if platform else ""
    opts = {
        "quiet": True, "no_warnings": True, "skip_download": True,
        # Keep playlist enumeration flat. Deep-resolving many Bilibili videos
        # serially makes the UI appear frozen; metadata is enriched concurrently.
        "extract_flat": "in_playlist",
        "playlistend": max(1, int(limit)),
        "ignoreerrors": True, "noplaylist": False,
    }
    if cookie:
        opts["cookiefile"] = cookie
    if on_note:
        on_note("正在用内置 yt-dlp 展开主页/列表…")
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:  # noqa: BLE001
        msg = f"{type(e).__name__}: {e}"
        auth = platform_login.looks_like_auth_error(msg)
        if auth and platform and platform_login.ready(platform):
            platform_login.mark_invalid(platform, msg)
        return {"ok": False, "items": [], "login_required": auth, "error": msg[:500]}
    if not isinstance(info, dict):
        return {"ok": False, "items": [], "login_required": False, "error": "yt-dlp 未返回可解析数据"}
    rows = list(info.get("entries") or [])
    if not rows and info.get("id"):
        rows = [info]
    items = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        got = _entry_item(row, url)
        if not got:
            continue
        key = got.get("page_url") or got.get("direct_url")
        if key in seen:
            continue
        seen.add(key)
        items.append(got)
        if len(items) >= int(limit):
            break
    # 列表模式（extract_flat）不解析详情，条目里常常没有封面 ——
    # 抓一次列表页 HTML 把缩略图补回来（匹配不上就留空，不乱配）。
    if any(not str(it.get("cover") or "").strip() for it in items):
        _backfill_covers(url, items)

    if platform == "bili" and items:
        items = _bili_enrich(items, cookie, on_note=on_note)
    return {"ok": bool(items), "items": items, "login_required": False,
            "error": "" if items else "yt-dlp 没有返回主页作品条目"}


_MEDIA_RE = re.compile(r'https?://[^"\\\s<>]+?\.(?:mp4|webm|m3u8|mov|flv)(?:\?[^"\\\s<>]*)?', re.I)
_HTTP_RE = re.compile(r'https?://[^"\\\s<>]+', re.I)


def _same_site_candidate(base_url: str, candidate: str) -> bool:
    try:
        a, b = urlparse(base_url), urlparse(candidate)
    except ValueError:
        return False
    if not b.scheme.startswith("http") or not b.hostname:
        return False
    base_platform = platform_for_url(base_url)
    if base_platform and platform_for_url(candidate) != base_platform:
        return False
    if not base_platform and (a.hostname or "").lower() != (b.hostname or "").lower():
        return False
    path = (b.path or "").strip("/")
    if not path:
        return False
    segs = [s for s in path.split("/") if s]
    if len(segs) == 1 and segs[0].lower() in _COLLECTION_SEG_NAMES:
        return False
    return len(segs) >= 2 or len(segs[-1]) >= 8


def _douyin_card(href: str, title: str = "", cover: str = "") -> dict | None:
    href = str(href or "").split("?", 1)[0]
    m = re.match(r"^https?://(?:www\.)?douyin\.com/video/(\d+)", href, re.I)
    if not m:
        return None
    return {
        "page_url": f"https://www.douyin.com/video/{m.group(1)}",
        "direct_url": "", "variants": [],
        "title": re.sub(r"\s+", " ", str(title or "")).strip()[:150],
        "cover": str(cover or ""),
        "uploader": "", "site": "douyin.com", "kind_hint": "video",
    }


def ig_author_items(url: str, *, limit: int = 30, on_note=None,
                    want_video: bool | None = None) -> dict:
    """Instagram 主页抓取：登录浏览器渲染 + 拦截 feed XHR。

    为什么不靠 gallery-dl / yt-dlp（2026-10-26 实测）：
    - gallery-dl 1.32.13 对用户主页直接崩 KeyError:'username'
      （web_profile_info 被 429 限流，响应里没有 user 字段）
    - yt-dlp instagram:user Unable to extract data
    - 通用兜底把主页链接本身当条目（8 条全是主页 URL）
    用户走代理访问 IG，共享出口 IP 被 429 限流极凶；真实浏览器
    （完整指纹 + 登录态 + 代理）比裸 API 请求稳得多。
    """
    try:
        from playwright.sync_api import sync_playwright
        from .renderer import _LAUNCH_ARGS
        from . import browser_runtime
        from .config import DEFAULT_UA
    except Exception as e:
        return {"ok": False, "items": [], "login_required": False,
                "error": f"内置浏览器不可用：{e}"}

    try:
        pu = urlparse(url)
        segs = [x for x in (pu.path or "").split("/") if x]
    except Exception:
        segs = []
    if not segs:
        return {"ok": False, "items": [], "login_required": False,
                "error": "Instagram 地址里没有用户名。"}
    username = segs[0]

    target = max(1, min(int(limit or 30), 2000))
    st = platform_login.status("ig")
    profile_dir = str(st.get("profile_dir") or "")
    if not profile_dir:
        return {"ok": False, "items": [], "login_required": True,
                "error": "Instagram 尚未建立登录 profile，请先到「平台登录」登录一次。"}

    found: dict[str, dict] = {}
    login_required = False
    ctx = None
    try:
        with sync_playwright() as p:
            kw = dict(
                user_data_dir=profile_dir,
                headless=not bool(browser_profile.headed()),
                viewport={"width": 1400, "height": 950},
                locale="en-US", user_agent=DEFAULT_UA,
                args=list(_LAUNCH_ARGS) + ["--disable-blink-features=AutomationControlled"],
                **browser_runtime.launch_kwargs(),
            )
            try:
                ctx = p.chromium.launch_persistent_context(**kw)
            except Exception as e:
                return {"ok": False, "items": [], "login_required": False,
                        "error": f"Instagram 登录 profile 正在被占用或无法打开：{e}"}
            page = ctx.pages[0] if ctx.pages else ctx.new_page()

            # 关键（2026-10-26 实测）：profile 里的登录态说丢就丢 —— 打开主页
            # 直接是游客态（"Log In Sign Up"，帖子网格根本不渲染，/p/ 链接 0 个），
            # 但 cookies_from_profile.txt 里的 sessionid 仍然有效
            # （走 API 返回 "useragent mismatch" 而非 401，说明会话活着）。
            # 所以每次开页前把保存的 cookie 强制注入上下文，不赌 profile 内置
            # cookie 的存活。实测：注入后 "Log In" 消失，滚动一轮就出 8 个帖子。
            try:
                _cks = platform_login.playwright_cookies("ig")
                if _cks:
                    ctx.add_cookies(_cks)
            except Exception:  # noqa: BLE001 —— 注入失败仍有 profile 自带 cookie 兜底
                pass

            def _put(code: str, title: str, cover: str, direct: str, is_video: bool) -> None:
                if not code:
                    return
                key = f"https://www.instagram.com/p/{code}/"
                old = found.get(key)
                if old is None:
                    found[key] = {"page_url": key, "url": direct or key,
                                  "title": (title or "")[:120], "cover": cover or "",
                                  "kind_hint": "video" if is_video else "",
                                  "is_page": not direct,
                                  "type": "video" if is_video else "image"}
                else:
                    if not old.get("cover") and cover:
                        old["cover"] = cover
                    if not old.get("title") and title:
                        old["title"] = title[:120]
                    # 后到的真直链要能覆盖「先到的页面地址」。
                    # 原来的条件是 not old.url.startswith("http")，而页面地址本身
                    # 就是 http 开头 → 永远补不进去，图片只能拿 /p/ 页面地址去下载
                    # （2026-10-27 实测：图片条目下载失败/被跳过）。
                    if direct:
                        old["url"] = direct
                        old["is_page"] = False

            def add_item(obj) -> None:
                if not isinstance(obj, dict):
                    return
                code = str(obj.get("code") or obj.get("shortcode") or "")
                if not code:
                    return
                cap = obj.get("caption") if isinstance(obj.get("caption"), dict) else None
                title = str((cap or {}).get("text") or "")
                vids = obj.get("video_versions")
                direct = ""
                if isinstance(vids, list) and vids and isinstance(vids[0], dict):
                    direct = str(vids[0].get("url") or "")
                cover = ""
                iv = obj.get("image_versions2")
                if isinstance(iv, dict):
                    cand = iv.get("candidates")
                    if isinstance(cand, list) and cand and isinstance(cand[0], dict):
                        cover = str(cand[0].get("url") or "")
                _put(code, title, cover, direct, bool(direct))

            def on_response(resp) -> None:
                low = str(resp.url or "").lower()
                if "/api/v1/feed/user/" not in low and "/graphql/query" not in low:
                    return
                try:
                    d = resp.json()
                except Exception:  # noqa: BLE001
                    return
                if not isinstance(d, dict):
                    return
                for it in (d.get("items") or []):
                    add_item(it)
                med = (((d.get("data") or {}).get("user") or {})
                       .get("edge_owner_to_timeline_media") or {})
                for edge in (med.get("edges") or []):
                    node = (edge or {}).get("node") or {}
                    code = str(node.get("shortcode") or "")
                    if code:
                        disp = str(node.get("display_url") or "")
                        is_vid = bool(node.get("is_video"))
                        vurl = str(node.get("video_url") or "") if is_vid else ""
                        _put(code, "", disp, vurl, is_vid)

            page.on("response", on_response)
            if on_note:
                on_note("正在用登录浏览器打开 Instagram 主页并持续下拉…")
            try:
                page.goto(f"https://www.instagram.com/{username}/",
                          wait_until="domcontentloaded", timeout=45000)
            except Exception:  # noqa: BLE001
                pass
            page.wait_for_timeout(4000)
            # 登录墙判断：标题/正文要求登录且页面上没有帖子链接
            try:
                ttl = (page.title() or "").lower()
                body = ""
                try:
                    body = page.locator("body").inner_text(timeout=3000)[:600].lower()
                except Exception:  # noqa: BLE001
                    pass
                if ("login" in ttl or "log in" in body[:250]
                        or "sign up" in body[:250]):
                    if not page.locator("a[href*='/p/']").count():
                        login_required = True
            except Exception:  # noqa: BLE001
                pass

            rnd = 0
            stale = 0

            def _hits() -> int:
                """按用户这次要的类型计数。

                原来是「总数到 limit 就停」——IG 主页前面常常整排是 Reels，
                于是「抓图片」只拿到零星几张（前 30 条里图片只有几条）。
                改成按目标类型计数，抓图片时会继续往下滚直到够数
                （2026-10-27 用户实测）。
                """
                if want_video is None:
                    return len(found)
                want_v = bool(want_video)
                return sum(1 for v in found.values()
                           if (v.get("type") == "video") == want_v)

            while _hits() < target and rnd < 60 and not login_required:
                rnd += 1
                before = _hits()
                try:
                    page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
                except Exception:  # noqa: BLE001
                    pass
                page.wait_for_timeout(1800)
                # DOM 兜底（2026-10-26 实测）：IG 主页已改成**服务端渲染** ——
                # 帖子数据直接写进 HTML，不再发 /api/v1/feed/user/ 这类 XHR。
                # 只监听网络响应会一条都拿不到（页面明明显示了 8 个帖子，
                # found 却是空的）。这里直接从 a[href*='/p/'] 读 shortcode + 封面。
                try:
                    dom_rows = page.evaluate(
                        """() => Array.from(document.querySelectorAll("a[href*='/p/'], a[href*='/reel/']"))
                             .map(a => {
                               const href = a.getAttribute('href') || '';
                               const img = a.querySelector('img');
                               return {
                                 code: href.split('/').filter(Boolean).pop() || '',
                                 cover: img ? (img.currentSrc || img.src || '') : '',
                                 alt: img ? (img.alt || '') : '',
                                 is_video: !!a.querySelector('video') || href.indexOf('/reel/') >= 0
                               };
                             }).filter(x => x.code)""") or []
                    for d in dom_rows:
                        # 图片条目要把封面地址当直链：IG 的 display_url 就是原图
                        # （~1080px）。留空的话 url 会退回页面地址，下载层拿不到图
                        # （2026-10-27 实测：图片条目下载失败/只能抓一部分）。
                        _is_v = bool(d.get("is_video"))
                        _cover = str(d.get("cover") or "")
                        _put(str(d.get("code") or ""), str(d.get("alt") or ""),
                             _cover, ("" if _is_v else _cover), _is_v)
                except Exception:  # noqa: BLE001
                    pass
                stale = 0 if _hits() > before else stale + 1
                if stale >= 10:
                    break
            _vals = list(found.values())
            # 目标类型排前面：即使这个账号目标类型不足，返回的也是「最相关的」那批
            if want_video is not None:
                _wv = bool(want_video)
                _vals.sort(key=lambda v: 0 if (v.get("type") == "video") == _wv else 1)
            items = _vals[:target]
            return {"ok": bool(items), "items": items, "login_required": login_required,
                    "error": "" if items else
                    ("Instagram 要求登录，请到「平台登录」重新登录一次。"
                     if login_required else
                     "Instagram 没有返回作品（大概率是出口 IP 被限流）。"
                     "稍等几分钟再试；若反复失败，请到「平台登录」刷新一次 Instagram 登录态。")}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "items": list(found.values()),
                "login_required": False, "error": f"{type(e).__name__}: {e}"}
    finally:
        if ctx is not None:
            try:
                ctx.close()
            except Exception:  # noqa: BLE001
                pass


def douyin_author_items(url: str, *, limit: int = 30, on_note=None) -> dict:
    """Bundled Douyin author crawler using Playwright and the saved platform profile."""
    try:
        from playwright.sync_api import sync_playwright
        from .renderer import _LAUNCH_ARGS
        from . import browser_runtime
        from .config import DEFAULT_UA
    except Exception as e:
        return {"ok": False, "items": [], "login_required": False,
                "error": f"内置浏览器不可用：{e}"}

    target = max(1, min(int(limit or 30), 2000))
    st = platform_login.status("dy")
    profile_dir = str(st.get("profile_dir") or "")
    if not profile_dir:
        return {"ok": False, "items": [], "login_required": True,
                "error": "抖音尚未建立独立登录 profile，请先到平台登录中心登录一次。"}

    found: dict[str, dict] = {}
    verification_required = False
    ctx = None                     # 供 finally 关闭，见下方说明
    try:
        with sync_playwright() as p:
            # 内核：包内 Chromium 优先 —— 用户电脑没有任何浏览器时抖音主页也要能读
            # V3.0：默认后台（headless）。V2.5 这里硬写 headless=False，
            # 每次抓抖音主页都会在你屏幕上弹一个窗口 —— 违反「正常抓取全部后台」。
            # 是否可见改由设置项控制（browser_headed），需要人工验证时才算例外。
            kw = dict(
                user_data_dir=profile_dir,
                headless=not bool(browser_profile.headed()),
                viewport={"width": 1400, "height": 900},
                locale="zh-CN", user_agent=DEFAULT_UA,
                args=list(_LAUNCH_ARGS) + ["--disable-blink-features=AutomationControlled"],
                **browser_runtime.launch_kwargs(),
            )
            try:
                ctx = p.chromium.launch_persistent_context(**kw)
            except Exception as e:
                return {"ok": False, "items": [], "login_required": False,
                        "error": f"抖音登录 profile 正在被占用或无法打开：{e}"}
            page = ctx.pages[0] if ctx.pages else ctx.new_page()

            def _put(card: dict) -> None:
                """合并同一条作品的两种来源：网络 JSON（通常带封面）和 DOM（可能还没懒加载出封面）。

                ⚠️ 这里**不能**用 setdefault 先到先得。
                实测（limit=100）滚动时 DOM 往往先扫到卡片、但 img 还是空的，
                一旦占了位，后面 JSON 里的封面就再也补不进来 —— 结果 81 条全没封面，
                而 30 条的小批量因为时序不同反而 30/30。
                规则：缺的用新来的补；已有没有封面的好数据不被空值覆盖。
                """
                key = card["page_url"]
                old = found.get(key)
                if old is None:
                    found[key] = card
                    return
                # 键名是 cover（_douyin_card 的返回键）。原来写成 thumbnail，
                # 这个分支永远命中不了 —— 缺封面的条目一直补不上去（2026-10-24）。
                if not old.get("cover") and card.get("cover"):
                    old["cover"] = card["cover"]
                if not old.get("title") and card.get("title"):
                    old["title"] = card["title"]

            def add_aweme(obj):
                if not isinstance(obj, dict):
                    return
                aid = str(obj.get("aweme_id") or obj.get("item_id") or "")
                if aid.isdigit():
                    video = obj.get("video") if isinstance(obj.get("video"), dict) else {}
                    cover = video.get("cover") if isinstance(video.get("cover"), dict) else {}
                    urls = cover.get("url_list") if isinstance(cover.get("url_list"), list) else []
                    author = obj.get("author") if isinstance(obj.get("author"), dict) else {}
                    # 视频直链：抖音放在 video.play_addr（高码率在 bit_rate[].play_addr）。
                    # 不取它，条目就只有「视频页地址」，下载时交给 yt-dlp 会直接
                    # Unsupported URL —— 表现为「批量抓取成功、下载全部失败」
                    # （实测 2026-10-25）。
                    direct = ""
                    for _key in ("play_addr", "play_addr_h264", "download_addr"):
                        _node = video.get(_key)
                        if not isinstance(_node, dict):
                            continue
                        for u in (_node.get("url_list") or []):
                            if isinstance(u, str) and u.startswith("http"):
                                # playwm = 带水印，换成 play 取无水印
                                direct = u.replace("/playwm/", "/play/")
                                break
                        if direct:
                            break
                    if not direct:
                        for br in (video.get("bit_rate") or []):
                            if not isinstance(br, dict):
                                continue
                            pa = br.get("play_addr") if isinstance(br.get("play_addr"), dict) else {}
                            for u in (pa.get("url_list") or []):
                                if isinstance(u, str) and u.startswith("http"):
                                    direct = u.replace("/playwm/", "/play/")
                                    break
                            if direct:
                                break
                    card = _douyin_card(
                        f"https://www.douyin.com/video/{aid}",
                        obj.get("desc") or "",
                        str(urls[0]) if urls else "",
                    )
                    if card:
                        card["uploader"] = str(author.get("nickname") or "")
                        if direct:
                            card["direct_url"] = direct
                        _put(card)
                for v in obj.values():
                    if isinstance(v, dict):
                        add_aweme(v)
                    elif isinstance(v, list):
                        for z in v:
                            if isinstance(z, dict):
                                add_aweme(z)

            def on_response(resp):
                low = resp.url.lower()
                if "/aweme/v1/web/" not in low or not any(x in low for x in ("post", "aweme", "feed")):
                    return
                try:
                    data = resp.json()
                except Exception:
                    return
                add_aweme(data)

            page.on("response", on_response)
            if on_note:
                on_note(f"正在用内置浏览器读取抖音主页，并持续下拉直到达到 {target} 个作品…")
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
            except Exception:
                pass
            page.wait_for_timeout(3500)

            stale = 0
            last_total = -1
            max_rounds = min(220, max(16, (target // 8 + 1) * 8))
            for rnd in range(max_rounds):
                try:
                    rows = page.eval_on_selector_all(
                        'a[href*="/video/"]',
                        """els => els.map(a => {
                            const im = a.querySelector('img') || a.querySelector('[style*="background-image"]');
                            let src = '';
                            if (im) {
                                src = im.src || '';
                                if (!/^https?:/i.test(src)) {
                                    src = im.getAttribute('data-src') || im.getAttribute('data-original') || '';
                                }
                                if (!src) {
                                    const ss = im.getAttribute('srcset') || im.getAttribute('data-srcset') || '';
                                    if (ss) {
                                        const first = ss.split(',')[0].trim();
                                        const sp = first.indexOf(' ');
                                        src = sp > 0 ? first.slice(0, sp) : first;
                                    }
                                }
                                if (!src) {
                                    // 纯字符串解析，不用正则：这段 JS 是嵌在 Python 三引号里的，
                                    // 写正则要过两层转义，实测很容易变成 \\( 而静默匹配不上。
                                    const st = im.getAttribute('style') || '';
                                    const k = st.indexOf('url(');
                                    if (k >= 0) {
                                        let v = st.slice(k + 4).trim();
                                        while (v && (v.charAt(0) === "'" || v.charAt(0) === '"')) v = v.slice(1);
                                        while (v && (v.slice(-1) === ')' || v.slice(-1) === "'" || v.slice(-1) === '"')) v = v.slice(0, -1);
                                        src = v.trim();
                                    }
                                }
                            }
                            return {
                                href: a.href,
                                title: (a.innerText || a.getAttribute('aria-label') || '').trim(),
                                img: /^https?:/i.test(src) ? src : ''
                            };
                        })""",
                    )
                except Exception:
                    rows = []
                for row in rows or []:
                    card = _douyin_card(
                        (row or {}).get("href"), (row or {}).get("title"), (row or {}).get("img")
                    )
                    if card:
                        _put(card)
                if len(found) >= target:
                    break

                if rnd == 4 and not found:
                    # 只有「一条都没抓到」且「页面上确实没有作品链接」时才怀疑验证码。
                    # 原来 rnd=1 就检查、且只按 body 文本匹配 —— 而「验证」这类词在
                    # 推荐流里到处都是，首屏还没渲染出来时就误判成验证码，
                    # 把本来能走通、**带视频直链**的 JSON 路径挤到了 BrowserSkill
                    # （BrowserSkill 只有 DOM，拿不到 play_addr）。
                    # 结果就是「列表能抓到、但下载全部失败」（2026-10-25 实测）。
                    _links = 0
                    try:
                        _links = int(page.evaluate(
                            "() => document.links ? Array.from(document.links)"
                            ".filter(a => a.href && a.href.includes('/video/')).length : 0") or 0)
                    except Exception:  # noqa: BLE001
                        _links = 0
                    if _links == 0:
                        try:
                            txt = (page.title() + " " + page.locator("body").inner_text(timeout=3000)).lower()
                        except Exception:  # noqa: BLE001
                            txt = ""
                        if any(x in txt for x in ("验证码", "安全验证", "验证后继续", "captcha",
                                                  "verify you are human", "滑块验证")):
                            verification_required = True
                            break

                before = len(found)
                try:
                    last = page.locator('a[href*="/video/"]').last
                    if last.count():
                        last.scroll_into_view_if_needed(timeout=3000)
                except Exception:
                    pass
                try:
                    page.evaluate("""() => {
                      const xs = Array.from(document.querySelectorAll('*')).filter(e => {
                        const s = getComputedStyle(e);
                        return e.scrollHeight > e.clientHeight + 200 &&
                               e.clientHeight > 300 &&
                               (s.overflowY === 'auto' || s.overflowY === 'scroll');
                      }).sort((a,b) => (b.clientWidth*b.clientHeight)-(a.clientWidth*a.clientHeight));
                      const e = document.querySelector('.route-scroll-container') || xs[0];
                      if (e) {
                        const step = Math.max(700, Math.floor(e.clientHeight * 0.85));
                        e.scrollTop = Math.min(e.scrollHeight, e.scrollTop + step);
                        e.dispatchEvent(new Event('scroll', {bubbles:true}));
                      }
                    }""")
                except Exception:
                    pass
                try:
                    page.mouse.move(900, 760)
                    page.mouse.wheel(0, 1800)
                except Exception:
                    pass
                page.wait_for_timeout(900)

                if on_note and (rnd + 1) % 5 == 0:
                    on_note(f"抖音已读取 {len(found)} 个作品，继续下拉加载更多…")
                total = len(found)
                if total == before and total == last_total:
                    stale += 1
                else:
                    stale = 0
                last_total = total

                # 验证码可能不是首屏出现，而是滚动若干页后才弹出。连续多轮
                # 没有新增时再检查一次页面，不要把“被验证码拦住”误判成到底了。
                if stale >= 3 and total < target:
                    try:
                        txt = (page.title() + " " + page.locator("body").inner_text(timeout=3000)).lower()
                    except Exception:
                        txt = ""
                    if any(x in txt for x in ("验证码", "安全验证", "验证后继续", "captcha",
                                              "verify you are human", "滑块验证")):
                        verification_required = True
                        break
                if stale >= 10:
                    break
    except Exception as e:
        return {"ok": False, "items": list(found.values())[:target],
                "login_required": False, "verification_required": False,
                "error": f"{type(e).__name__}: {e}"}
    finally:
        # 必须在 finally 里关。原来这行写在滚动循环之后，
        # 中途任何 return / 抛异常都会跳过它，浏览器进程就留在后台占着
        # profile 目录 —— 用户点几次抓取，后台积了 8 个 chrome 全占 dy profile，
        # 之后每次都报「profile 正在被占用」（2026-10-24 实测）。
        # 参考：用户 10-07 用同一套流程成功下过 619 个视频，说明流程本身没问题，
        # 就是这里的资源泄漏把后续抓取堵死了。
        if ctx is not None:
            try:
                ctx.close()
            except Exception:  # noqa: BLE001
                pass

    items = list(found.values())[:target]
    if verification_required:
        # 提示里已经让用户去刷新登录态，但登录中心不知道——它只看 cookie
        # 在不在。不标记失效的话界面一直显示「已登录」（2026-10-31 排查补齐）。
        platform_login.mark_invalid(
            "dy", "抖音触发安全验证（登录态已过期或被风控），请到平台登录中心刷新登录态")
    return {
        "ok": bool(items), "items": items, "login_required": False,
        "verification_required": verification_required,
        "error": "" if items else (
            "抖音触发安全验证（登录态已过期或被风控，headless 指纹无法自动通过）。"
            "请到【平台登录】页刷新一次抖音登录态（可能需要过一次滑块），"
            "刷新后趁登录态有效立刻重试。"
            if verification_required else
            "内置浏览器已打开抖音主页，但没有读到作品卡片。"
        ),
    }


def browser_skill_author_items(url: str, *, limit: int = 30, on_note=None) -> dict:
    """Optional CAPTCHA escalation only; ordinary crawling never depends on bsk."""
    try:
        from . import browser_skill
    except Exception as e:
        return {"ok": False, "items": [], "login_required": False, "error": str(e)}
    if not browser_skill.available():
        return {"ok": False, "items": [], "login_required": False,
                "error": "检测到验证码，但本机未安装/连接 BrowserSkill。"}

    sid = ""
    found: dict[str, dict] = {}
    try:
        if on_note:
            on_note("检测到验证码，正在准备可见验证窗口…")
        # V3.0：bsk / 扩展 / 内核都随包，没装过任何浏览器也能起验证窗口。
        # 可见性听「设置 → 显示浏览器窗口」：默认后台，别在用户没要求时弹窗
        # （2026-10-24 实测：抓 Instagram 撞风控会凭空弹出窗口，打断用户）。
        from . import browser_profile
        # 抖音必须用 dy 登录态的浏览器打开主页：bsk_profile 没有抖音登录，
        # 抖音对未登录访问主页只给推荐流（2026-10-25 实测：抓到 8 条全是
        # 无关视频）。用 platform_profiles/dy/profile 起的 bsk 实例才有博主内容。
        _used_dy_profile = False
        _ud = None
        try:
            from .platform_login import profile_dir_for_url as _pdu
            _p = _pdu(url, ready_only=True)
            if _p and platform_for_url(url) == "dy":
                _ud = _p
                _used_dy_profile = True
        except Exception:  # noqa: BLE001
            _ud = None
        ready = browser_skill.ensure_ready(headless=not browser_profile.headed(),
                                           timeout=40, user_data_dir=_ud)
        if not ready.get("ok"):
            _why = str(ready.get("error") or "未知原因")
            # 失败也必须让用户看见原因。原来的写法只在成功时打 note，
            # 失败时用户只等到一个「0 条」，完全不知道是验证窗口没起来
            # （2026-10-25 实测：bsk daemon 状态文件残留导致起不来，白排查好几轮）。
            if on_note:
                on_note(f"验证窗口没能启动：{_why}")
            return {"ok": False, "items": [], "login_required": False,
                    "error": f"验证窗口准备失败：{_why}"}
        if on_note:
            on_note("验证窗口已就绪，正在打开主页…")
        opened = browser_skill.start_page(
            url, name="MediaDownloader 验证辅助",
            browser_id=str(ready.get("instance_id") or ""))
        sid = str(opened.get("session_id") or "")
        if not sid:
            raise RuntimeError("BrowserSkill 未返回 session_id")
        time.sleep(2.5)
        # 开页后先看一眼标题：抖音对 headless 指纹 + 过期登录态会直接给
        # 「验证码中间页」，页面连正文都是空的 —— 这种情况下滚动循环
        # 只会白跑 60 秒（2026-10-25 实测 64s 白跑）。快速失败 + 给出
        # 可操作的指引，比静默 0 条强得多。
        try:
            _t = str(browser_skill.evaluate(
                sid, "() => document.title.slice(0, 40)", timeout=12) or "")
        except Exception:  # noqa: BLE001
            _t = ""
        if any(x in _t for x in ("验证码", "安全验证", "登录", "verify")):
            return {"ok": False, "items": [], "login_required": True,
                    "error": ("抖音要求重新验证（登录态已过期，headless 指纹无法自动通过）。"
                              "请到【平台登录】页重新刷新一次抖音登录（可能需要过一次滑块），"
                              "刷新后趁登录态有效立刻回来重试 —— 新鲜登录态下 headless 是能直接抓的。")}
        state = browser_skill.evaluate(
            sid,
            """(()=>({
              videos:Array.from(document.links).filter(a=>a.href&&a.href.includes('/video/')).length,
              text:(document.body&&document.body.innerText||'').slice(0,5000).toLowerCase()
            }))()""",
            timeout=20,
        ) or {}
        state_text = str((state or {}).get("text") or "")
        has_verify = any(x in state_text for x in (
            "验证码", "安全验证", "验证后继续", "captcha",
            "verify you are human", "滑块验证",
        ))
        if has_verify:
            clicked = browser_skill.click_simple_verification(sid)
            if not clicked:
                # 参数名必须是 timeout_seconds —— V2.5 这里写成 timeout=180，
                # 每次都 TypeError 被外层吞掉，等于「请用户验证」从来没生效过。
                browser_skill.request_human_help(
                    sid,
                    "请在这个 Agent Window 中完成验证码/滑块/安全验证。完成后 BrowserSkill 会继续读取作品。",
                    timeout_seconds=180,
                )
        elif on_note:
            on_note("BrowserSkill 当前已是正常作品页，无需人工验证码，继续下拉读取…")
        expr = """Array.from(document.links)
          .filter(a => a.href && a.href.includes('/video/'))
          .map(a => {
            let im = a.querySelector('img');
            if (!im) {
              // 抖音的作品封面常挂在 <a> 的兄弟节点或父容器里，不在 <a> 内部。
              // 只查 a 内部会拿到空封面（实测 2026-10-24：BrowserSkill 路径 10 条全无封面）。
              for (const c of [a.parentElement, a.previousElementSibling, a.nextElementSibling]) {
                if (c && c.querySelector) { im = c.querySelector('img'); if (im) break; }
              }
            }
            if (!im) im = a.querySelector('[style*="background-image"]');
            let src = '';
            if (im) {
              src = im.src || '';
              if (!/^https?:/i.test(src)) src = im.getAttribute('data-src') || im.getAttribute('data-original') || '';
              if (!src) {
                const ss = im.getAttribute('srcset') || im.getAttribute('data-srcset') || '';
                if (ss) {
                  const first = ss.split(',')[0].trim();
                  const sp = first.indexOf(' ');
                  src = sp > 0 ? first.slice(0, sp) : first;
                }
              }
              if (!src) {
                // 纯字符串解析（同 douyin_author_items 的理由：避免两层转义）
                const st = im.getAttribute('style') || '';
                const k = st.indexOf('url(');
                if (k >= 0) {
                  let v = st.slice(k + 4).trim();
                  while (v && (v.charAt(0) === "'" || v.charAt(0) === '"')) v = v.slice(1);
                  while (v && (v.slice(-1) === ')' || v.slice(-1) === "'" || v.slice(-1) === '"')) v = v.slice(0, -1);
                  src = v.trim();
                }
              }
            }
            return {href:a.href, title:(a.innerText||'').trim(),
                    img:/^https?:/i.test(src) ? src : ''};
          })"""
        target = max(1, min(int(limit or 30), 2000))
        stale = 0
        for _ in range(min(160, max(16, (target // 8 + 1) * 8))):
            rows = browser_skill.evaluate(sid, expr, timeout=30) or []
            before = len(found)
            before_covers = sum(1 for v in found.values() if v.get("cover"))
            for row in rows if isinstance(rows, list) else []:
                card = _douyin_card((row or {}).get("href"), (row or {}).get("title"),
                                     (row or {}).get("img"))
                if card:
                    # 缺封面就补。注意键名是 cover（_douyin_card 的返回键），
                    # 原来写成 thumbnail 永远命中不了 —— 实测 10 条里 7 条空封面。
                    old = found.get(card["page_url"])
                    if old is None:
                        found[card["page_url"]] = card
                    elif not old.get("cover") and card.get("cover"):
                        old["cover"] = card["cover"]
            if len(found) >= target and all(v.get("cover") for v in found.values()):
                break
            browser_skill.evaluate(
                sid,
                """(()=>{const xs=[...document.querySelectorAll('*')].filter(e=>{
                  const s=getComputedStyle(e); return e.scrollHeight>e.clientHeight+200 &&
                  e.clientHeight>300 && (s.overflowY==='auto'||s.overflowY==='scroll')});
                  const e=document.querySelector('.route-scroll-container')||xs[0];
                  if(e){e.scrollTop=Math.min(e.scrollHeight,e.scrollTop+Math.max(700,e.clientHeight*.85));
                  e.dispatchEvent(new Event('scroll',{bubbles:true})); return true}
                  window.scrollBy(0,Math.max(1000,innerHeight)); return false})()""",
                timeout=15,
            )
            # 图片是懒加载的，滚动后要给它时间加载；1 秒太短，封面补不上
            time.sleep(1.8)
            after_covers = sum(1 for v in found.values() if v.get("cover"))
            # 进度判定要把「补到封面」也算进去，否则条目数不变就一直被判成停滞
            progressed = (len(found) > before) or (after_covers > before_covers)
            stale = 0 if progressed else stale + 1
            if stale >= 10:
                break
        items = list(found.values())[:target]
        return {"ok": bool(items), "items": items, "login_required": False,
                "error": "" if items else "BrowserSkill 验证完成后仍未读到抖音作品。"}
    except Exception as e:
        return {"ok": False, "items": list(found.values()), "login_required": False,
                "error": f"{type(e).__name__}: {e}"}
    finally:
        if sid:
            browser_skill.stop(sid)
        # 用 dy profile 起的实例必须用完就关：dy profile 是排他资源，
        # 下载侧的直链解析（video_downloader._douyin_real_url_profiled）
        # 还要用 playwright 打开同一个 profile。不关会把它锁死。
        if _used_dy_profile:
            try:
                browser_skill.stop_browser()
            except Exception:  # noqa: BLE001
                pass


def browser_items(url: str, *, limit: int = 30, on_note=None) -> dict:
    from bs4 import BeautifulSoup
    from .renderer import render_page

    platform = platform_for_url(url)
    st = platform_login.status(platform) if platform else {}
    if on_note:
        on_note("正在用内置登录浏览器渲染主页并读取页面自己的网络响应…")
    rr = render_page(url, timeout_ms=60000, settle_ms=3500, scroll_rounds=8,
                     capture_types=("json", "javascript", "text"))
    if not rr.ok:
        msg = rr.error or "浏览器渲染失败"
        return {"ok": False, "items": [], "login_required": False, "error": msg}
    text = "\n".join(c.body for c in rr.captured if c.body)
    items, seen = [], set()

    for m in _MEDIA_RE.findall(text + "\n" + (rr.html or "")):
        u = m.replace("\\/", "/").rstrip("\\")
        if u in seen:
            continue
        seen.add(u)
        items.append({"page_url": url, "direct_url": u, "variants": [], "title": "",
                      "cover": "", "uploader": "", "site": (urlparse(url).hostname or ""),
                      "kind_hint": "video"})
        if len(items) >= int(limit):
            break

    if len(items) < int(limit):
        candidates = []
        try:
            soup = BeautifulSoup(rr.html or "", "html.parser")
            for a in soup.find_all("a", href=True):
                candidates.append(urljoin(rr.final_url or url, a.get("href") or ""))
        except Exception:
            pass
        for raw in _HTTP_RE.findall(text):
            candidates.append(raw.replace("\\/", "/").rstrip("\\"))
        for u in candidates:
            if u in seen or not _same_site_candidate(url, u):
                continue
            seen.add(u)
            items.append({"page_url": u, "direct_url": "", "variants": [], "title": "",
                          "cover": "", "uploader": "", "site": (urlparse(u).hostname or ""),
                          "kind_hint": "video"})
            if len(items) >= int(limit):
                break

    try:
        visible = BeautifulSoup(rr.html or "", "html.parser").get_text(" ", strip=True).lower()
    except Exception:
        visible = ""
    login_wall = any(x in visible for x in ("登录", "扫码登录", "sign in", "log in"))
    if not items and login_wall:
        msg = "页面显示登录提示，当前平台登录态不可用。"
        if platform and st.get("logged_in"):
            platform_login.mark_invalid(platform, msg)
        return {"ok": False, "items": [], "login_required": True, "error": msg}
    return {"ok": bool(items), "items": items, "login_required": False,
            "error": "" if items else "浏览器已打开主页，但没有识别到作品链接或视频地址"}


def _relogin_why(platform: str, res: dict) -> str:
    """统一的「登录态失效/需要登录」用户提示；顺带把失效标记写进登录中心。

    2026-10-31 排查：小红书 code=-100 暴露出的问题在别的平台同样存在——
    第一跳明确报了需要登录，后面的兜底继续白跑，最终错误信息里登录原因
    被覆盖掉，用户只看到「没有识别到作品」却不知道该去重新登录。
    """
    why = str(res.get("error") or "登录态不可用")
    st = platform_login.status(platform)
    name = platform_login.PLATFORMS.get(platform, {}).get("name") or platform
    if st.get("logged_in"):
        platform_login.mark_invalid(platform, why)
        return f"{name} 登录态已失效，请到「平台登录中心」重新登录。 {why}"
    return f"{name} 当前需要登录，请到「平台登录中心」登录一次。 {why}"


def list_author_items(url: str, *, limit: int = 30, want_video: bool = False,
                      on_note=None) -> tuple[list[dict], str]:
    platform = platform_for_url(url)
    if not platform:
        return [], ""
    name = PLATFORM_NAMES.get(platform, platform)
    attempts = []

    if platform == "xhs":
        # V3.0：发现页/搜索页和作者主页是两条完全不同的链路。
        # 发现页没有「用户」概念，只能先拿笔记引用再逐篇下钻。
        _xhs_kind = xhs_page_kind(url)
        if _xhs_kind in {"feed", "search"}:
            res = xhs_feed_items(url, limit=limit, want_video=want_video, on_note=on_note)
            attempts.append(("xhshow-发现页", res))
        else:
            res = (xhs_author_items(url, limit=limit, on_note=on_note)
                   if want_video else
                   xhs_author_image_items(url, limit=limit, on_note=on_note))
            attempts.append(("xhshow", res))
        # Image mode must not fall back to generic page scanning: that reintroduces
        # avatars, site UI assets and recommendation-feed images.
        if not want_video and not res.get("ok"):
            why = str(res.get("error") or "\u6ca1\u6709\u8fd4\u56de\u56fe\u6587\u7b14\u8bb0\u6b63\u6587\u56fe\u7247")
            if res.get("login_required"):
                st = platform_login.status(platform)
                if st.get("logged_in"):
                    platform_login.mark_invalid(platform, why)
                    why = f"{name} \u767b\u5f55\u6001\u5df2\u5931\u6548\uff0c\u8bf7\u5230\u300c\u5e73\u53f0\u767b\u5f55\u4e2d\u5fc3\u300d\u91cd\u65b0\u767b\u5f55\u3002 {why}"
                else:
                    why = f"{name} \u5f53\u524d\u9700\u8981\u767b\u5f55\uff0c\u8bf7\u5230\u300c\u5e73\u53f0\u767b\u5f55\u4e2d\u5fc3\u300d\u767b\u5f55\u4e00\u6b21\u3002 {why}"
            return [], why
    else:
        res = {"ok": False, "items": [], "login_required": False, "error": ""}

    if platform == "wb" and not res.get("ok"):
        res = weibo_author_items(url, limit=limit, want_video=want_video, on_note=on_note)
        attempts.append(("微博作品流", res))
        if res.get("login_required"):
            # 微博自有接口明确说「登录凭据被服务端拒绝」时就不再兜底了：
            # gallery-dl / yt-dlp 走微博同样要吃登录态，实测它们只会返回 1 条无关视频
            # 然后被当成「成功」，上层再按图片过滤成 0 条 —— 用户看到的是「0 条」而不是
            # 「请重新登录」，这个歧义正是 V3.0 要消掉的（实测 2026-10-22）。
            why = str(res.get("error") or "微博登录态不可用")
            return [], (f"微博登录态已失效，请到「平台登录」重新登录微博后再试。{why}")

    if platform == "ig" and not res.get("ok"):
        res = ig_author_items(url, limit=limit, on_note=on_note,
                              want_video=(True if want_video else False))
        attempts.append(("内置登录浏览器", res))

    if platform == "dy" and not res.get("ok"):
        res = douyin_author_items(url, limit=limit, on_note=on_note)
        attempts.append(("内置Playwright", res))
        # 「账号不存在」是确定性结论，继续兜底只会白跑三条路再给出同一个结果。
        if "账号不存在" in str(res.get("error") or ""):
            return [], str(res.get("error"))
        if res.get("verification_required") and len(res.get("items") or []) < max(1, int(limit or 30)):
            bsk_res = browser_skill_author_items(url, limit=limit, on_note=on_note)
            attempts.append(("BrowserSkill验证码辅助", bsk_res))
            if bsk_res.get("ok") or len(bsk_res.get("items") or []) > len(res.get("items") or []):
                res = bsk_res

    # B站空间页优先走 yt-dlp 的平铺列表 + 并发元数据补全。
    # gallery-dl 在这里没有收益，反而会让批量入口先卡一轮。
    if platform == "bili" and not res.get("ok"):
        res = ytdlp_items(url, limit=limit, on_note=on_note)
        attempts.append(("yt-dlp", res))

    # 登录态失效就快速失败（2026-10-31 小红书同款教训，此处覆盖
    # 微博/Instagram/抖音/B站等所有专用来源）：过期登录态下再跑
    # gallery-dl / yt-dlp / 浏览器渲染只会白耗时，甚至把登录墙页面的
    # 站点素材当作品返回。如实报「请重新登录」。
    if not res.get("ok") and res.get("login_required"):
        return [], _relogin_why(platform, res)

    if not res.get("ok") and platform in {"ig", "pin", "wb", "tiktok"}:
        gallery_limit = limit
        # Weibo feed interleaves image and video posts. In image mode scan beyond
        # the requested count, then keep only images so a run of video posts cannot
        # starve the image result set.
        if platform == "wb" and not want_video:
            gallery_limit = max(int(limit or 30) * 3, 60)
        res = gallery_dl_items(url, limit=gallery_limit, video_only=want_video,
                               retries=1, retry_wait=10, on_note=on_note)
        if res.get("ok") and platform == "wb" and not want_video:
            only_images = [
                it for it in (res.get("items") or [])
                if (it.get("kind_hint") or "") == "image"
            ][:max(1, int(limit or 30))]
            res = {**res, "ok": bool(only_images), "items": only_images,
                   "error": "" if only_images else "微博主页作品源返回了媒体，但当前批次没有图片。"}
        attempts.append(("gallery-dl", res))

    if not res.get("ok") and platform not in {"xhs", "ks"}:
        # 知乎/TikTok/Pinterest 等走 yt-dlp 后同样可能报登录失效，
        # 不能再让浏览器渲染兜底把登录原因盖掉。
        if res.get("login_required"):
            return [], _relogin_why(platform, res)
        res = ytdlp_items(url, limit=limit, on_note=on_note)
        attempts.append(("yt-dlp", res))

    # 小红书不兜底 browser_items（2026-10-31 实测）：登录态过期时主页渲染出的是
    # 登录墙，页面自带的演示视频（dc.xhscdn.com 的背景视频/钢琴视频等站点素材）
    # 会被当成「博主作品」返回，用户全选下载全是失败。与其给假结果，不如如实报错。
    if not res.get("ok") and platform != "xhs":
        res = browser_items(url, limit=limit, on_note=on_note)
        attempts.append(("browser", res))

    if res.get("ok"):
        used = next((n for n, r in reversed(attempts) if r is res), "内置来源")
        items = res.get("items") or []
        suffix = ""
        if platform == "dy" and res.get("verification_required") and len(items) < max(1, int(limit or 30)):
            suffix = f" 已抓到 {len(items)}/{max(1, int(limit or 30))} 个，继续下拉时遇到验证码；BrowserSkill 验证辅助未完成。"
        return items, f"已用{used}展开{name}主页。{suffix}"

    why = str(res.get("error") or "没有返回作品")
    # 需要验证码但 bsk 也没起来时，把 bsk 的失败原因接在后面。
    # 否则用户只看到「没有返回作品」，完全不知道卡在哪。
    if res.get("verification_required"):
        _bsk_fail = next((r for n, r in attempts
                          if n == "BrowserSkill验证码辅助" and not r.get("ok")), None)
        if _bsk_fail is not None:
            why = (f"{why}｜自动验证窗口没能启动："
                   f"{_bsk_fail.get('error') or '未知原因'}")
    if res.get("login_required"):
        return [], _relogin_why(platform, res)
    return [], why
