# -*- coding: utf-8 -*-
"""FastAPI 服务层：把搜图、网页扫描、视频解析、下载队列统一暴露给前端界面。"""
from __future__ import annotations

import ipaddress
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path
from urllib.parse import urlparse, unquote

import requests
from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import DATA_ROOT, DOWNLOADS_DIR, HEADERS, ROOT, WEB_DIR, ffmpeg_version, find_ffmpeg, find_js_runtime
from .concurrency import GOVERNOR
from .image_search import FILTER_OPTIONS, search_images
from .page_scanner import resolve_stream
from .stream_downloader import stream_supported
from .task_manager import manager
from .video_downloader import ffmpeg_available, probe_video, search_videos
from . import browser_profile, browser_runtime, browser_skill, cookie_store, crawl_manager, platform_login, profile_sources, store, subproc

# 「设置」里配的 cookies.txt 也要用于**网页抓取与浏览器渲染**，不只是 yt-dlp：
# 需要登录的平台（Instagram 等）匿名访问只会返回登录墙，正文里没有任何内容 ——
# 这正是「粘平台主页永远 0 条」的成因之一。这里注册读取入口，
# page_scanner / renderer 在每次请求时按需取（带 30 秒缓存，重新导出后自动生效）。
cookie_store.set_provider(lambda: (store.load().get("cookies_file") or ""))
# 登录态浏览器配置（持久化 profile + 有头模式）：抖音/小红书主页靠它才能读到内容
browser_profile.set_provider(lambda: {
    "dir": store.load().get("browser_user_data_dir") or "",
    "headed": bool(store.load().get("browser_headed")),
})

app = FastAPI(title="媒体下载工具", version="1.0.0")


@app.middleware("http")
async def _disable_frontend_cache(request, call_next):
    """桌面壳升级后禁止继续复用旧 HTML/JS/CSS。"""
    response = await call_next(request)
    if request.url.path in {"/", "/index.html", "/app.js", "/style.css"}:
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


# 首次使用向导版本。只有用户完成了“当前版本”的向导才算完成。
# 旧版只有 onboarding_complete=true、没有 version，因此升级到 2.0 后会重新展示一次。
ONBOARDING_VERSION = "2.0"


def _is_instagram_url(url: str) -> bool:
    host = (urlparse(url or "").hostname or "").lower()
    return host == "instagram.com" or host.endswith(".instagram.com")


def _is_xhs_url(url: str) -> bool:
    host = (urlparse(url or "").hostname or "").lower()
    return host == "xiaohongshu.com" or host.endswith(".xiaohongshu.com")


def _cookie_args_for_url(url: str, settings: dict) -> tuple[str | None, str | None]:
    # 2.0：已识别平台只使用自己的独立登录态，禁止把 A 平台 Cookie/Profile
    # 串给 B 平台。未识别的普通网站才继续使用高级设置里的通用 cookies。
    platform = platform_login.platform_for_url(url)
    if platform:
        own = platform_login.cookie_file_for_platform(platform)
        return (own or None), None
    return settings.get("cookies_file") or None, settings.get("cookies_from_browser") or None


def _direct_item_probe(item: dict, webpage_url: str, extractor: str) -> dict:
    """把受信来源已经解析出的真实视频直链直接映射成前端 probe 结果。"""
    urls = []
    for u in [item.get("direct_url"), *(item.get("variants") or [])]:
        u = str(u or "").strip()
        if u and u not in urls:
            urls.append(u)
    formats = [{
        "format_id": f"direct-{i+1}", "ext": "mp4", "width": None, "height": None,
        "fps": None, "vcodec": "direct", "acodec": "unknown", "has_video": True,
        "has_audio": True, "filesize": None, "tbr": None, "format_note": "平台视频直链",
        "quality_label": f"视频直链 {i+1}",
    } for i, _u in enumerate(urls)]
    return {
        "ok": bool(formats), "id": "", "title": item.get("title") or "",
        "duration": item.get("duration"), "duration_string": "", "uploader": item.get("uploader") or "",
        "upload_date": "", "view_count": None, "webpage_url": webpage_url,
        "thumbnail": item.get("cover") or "", "extractor": extractor, "description": "",
        "formats": formats, "quality_options": [], "is_live": False, "ffmpeg": True,
        "direct_source": True,
    }


@app.on_event("startup")
def _startup_reaper():
    """启动即收割上一实例留下的孤儿 ffmpeg，并常驻定时收割。

    背景（2026-09-28）：服务重启/任务卡死后 ffmpeg 变孤儿 —— 占 .part 文件
    句柄和站点连接，造成「有时成功有时失败有时没反应」。
    """
    from .win_job import reap_orphan_ffmpeg, start_reaper_thread
    try:
        n = reap_orphan_ffmpeg()
        if n:
            print(f"[startup] 清理上一实例遗留的孤儿 ffmpeg {n} 个", flush=True)
    except Exception:  # noqa: BLE001
        pass
    start_reaper_thread()

IMG_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg",
            ".avif", ".ico", ".tif", ".tiff", ".jxl"}
VID_EXTS = {".mp4", ".webm", ".mkv", ".mov", ".avi", ".flv", ".m4v",
            ".mpg", ".mpeg", ".3gp", ".ts"}
ALLOWED_MEDIA_EXTS = IMG_EXTS | VID_EXTS
MAX_THUMB_BYTES = 4 * 1024 * 1024


# ---------- 请求模型 ----------

class SearchReq(BaseModel):
    keyword: str = Field(min_length=1, max_length=200)
    limit: int = Field(default=40, ge=1, le=200)
    min_resolution: int = Field(default=0, ge=0, le=10000)
    max_resolution: int = Field(default=0, ge=0, le=20000)
    min_size_kb: int = Field(default=0, ge=0, le=200000)
    formats: str = ""
    engines: str = "bing"
    size: str = ""
    aspect: str = ""
    type: str = ""
    time: str = ""
    color: str = ""
    face: str = ""
    license: str = ""
    safesearch: str = "moderate"


class VideoProbeReq(BaseModel):
    url: str = Field(min_length=1, max_length=2000)
    include_playlist: bool = False


class VideoSearchReq(BaseModel):
    keyword: str = Field(min_length=1, max_length=200)
    # 可在搜索前选定视频源：web / domestic / foreign / youtube / bilibili /
    # 以及单站（douyin、qq、youku、iqiyi、sohu、kuaishou、weibo、xiaohongshu、
    # vimeo、dailymotion、tiktok、twitter、instagram、facebook）
    platform: str = "web"
    count: int = Field(default=10, ge=1, le=50)
    # 按下载台账隐藏已下过的视频（默认开）。用户诉求：反复搜同一个词、
    # 每次下不同的，下次不该再看到已下过的。
    hide_downloaded: bool = True


class ImageItem(BaseModel):
    url: str
    referer: str = ""
    origin_page: str = ""
    thumbnail: str = ""
    title: str = ""
    filename: str = ""
    # 同一媒体的其它档位地址（下载首选失败时自动换用）。
    # 限条数，并且端点会像 url 一样对它做内网地址校验（防 SSRF）。
    alternates: list[str] = Field(default_factory=list, max_length=10)
    # 该条目的清晰度偏好（highest / lowest / "720"），单条可覆盖整批设置
    prefer_quality: str = Field(default="", max_length=12)

    def remote_urls(self) -> list[str]:
        """本条目会请求到的全部地址（校验内网地址时用）。"""
        return [self.url, *[u for u in self.alternates if u]]


class DownloadImagesReq(BaseModel):
    items: list[ImageItem] = Field(min_length=1, max_length=2000)
    subdir: str = "images"
    concurrency: int = Field(default=6, ge=1, le=16)
    skip_existing: bool = True
    # 整批的清晰度偏好（同类媒体有多档位时生效）
    prefer_quality: str = Field(default="", max_length=12)
    title: str = ""


class DownloadVideoReq(BaseModel):
    url: str = Field(min_length=1, max_length=2000)
    subdir: str = "videos"
    quality: str = "best"
    container: str = ""
    prefer_audio_only: bool = False
    write_subtitles: bool = False
    subtitle_langs: str = "zh.*,en"
    include_playlist: bool = False
    rate_limit: str = ""
    format_id: str = Field(default="", max_length=100)
    title: str = ""


class HistoryForgetReq(BaseModel):
    """忘掉一条下载记录（二选一：页面地址 / 身份键）。"""
    url: str = Field(default="", max_length=2000)
    key: str = Field(default="", max_length=400)


class DownloadVideosReq(BaseModel):
    """批量下载「视频页面地址」—— 关键词搜视频结果里勾选的那些。

    与 DownloadDirectReq 的区别：那边收的是**媒体直链**（.mp4 等），
    这边收的是**视频页面地址**（YouTube / B 站 / 各站播放页），要交给 yt-dlp 解析。
    """
    items: list[ImageItem] = Field(min_length=1, max_length=2000)
    subdir: str = "videos"
    quality: str = "best"
    container: str = ""
    include_playlist: bool = False
    concurrency: int = Field(default=3, ge=1, le=16)


class DownloadDirectReq(BaseModel):
    """下载网页扫描出的媒体直链（图片或视频文件）。"""
    items: list[ImageItem] = Field(min_length=1, max_length=2000)
    subdir: str = "media"
    concurrency: int = Field(default=6, ge=1, le=16)
    skip_existing: bool = True
    # 整批的清晰度偏好：同类媒体带多档位地址时，据此主动挑档
    prefer_quality: str = Field(default="", max_length=12)
    title: str = ""


class CrawlReq(BaseModel):
    """批量抓取：给一个 URL，抓出该页面/该站点下的媒体资源。

    mode = "list"：列表页 → 详情页两段式（商品列表、搜索结果页）
    mode = "site"：整站遍历（同行官网这类没有统一列表页的站点）
    """
    url: str = Field(min_length=1, max_length=2000)
    mode: str = "list"                                     # list | site
    max_pages: int = Field(default=1, ge=1, le=2000)
    max_items: int = Field(default=30, ge=0, le=2000)      # 列表/主页展开与详情页下钻数量上限
    drill_down: bool = True                                # 是否下钻详情页
    drill_mode: str = "auto"                               # auto | always
    allow_render: bool = True                              # 允许用浏览器渲染 JS 页面
    detail_concurrency: int = Field(default=4, ge=1, le=8)
    kinds: str = "image,video,stream"                      # 逗号分隔的类型筛选
    originals_only: bool = False                           # 只保留原图，丢弃缩略图
    # 要视频时，先把页面交给 yt-dlp 判断「是不是一个视频列表（博主页/频道/合集）」。
    # 视频地址在平台主页上不在 HTML 里，走网页抓取必然是 0 条（详见 batch_crawler）。
    expand_listing: bool = True
    # 整站模式专用
    max_depth: int = Field(default=2, ge=1, le=5)          # 跟随链接的层数
    same_host: bool = True                                 # 只跟同域链接
    use_sitemap: bool = True                               # 用站点地图当整站遍历的种子
    allow_private: bool = False


class SettingsReq(BaseModel):
    downloads_subdir_images: str | None = None
    downloads_subdir_videos: str | None = None
    image_concurrency: int | None = Field(default=None, ge=1, le=16)
    # 流媒体下载并发（1-8）：task_manager._run_streams 每次任务启动实时读取，立即生效
    stream_concurrency: int | None = Field(default=None, ge=1, le=8)
    skip_existing: bool | None = None
    allow_private_network: bool | None = None
    cookies_file: str | None = None
    cookies_from_browser: str | None = None
    video_quality_default: str | None = None
    # 1B：以下字段前端一直在发，但此前不在模型里 —— Pydantic 静默丢弃，
    # 造成「界面显示已保存、后端根本没接」的假成功，这里全部接通
    browser_user_data_dir: str | None = Field(default=None, max_length=1000)
    browser_headed: bool | None = None
    # 下载根目录：导入期常量 DOWNLOADS_DIR 消费，改后需重启生效（端点如实返回）
    downloads_dir: str | None = Field(default=None, max_length=1000)
    # 桌面窗口右上角 X：exit=直接退出；tray=隐藏到系统托盘
    close_behavior: str | None = Field(default=None, pattern="^(exit|tray)$")
    # 基础界面模式与强调色分离；都只允许内置预设。
    ui_mode: str | None = Field(default=None, pattern="^(gray|dark|black|light)$")
    ui_accent: str | None = Field(default=None, pattern="^(blue|cyan|violet|green|orange|rose)$")


# ---------- 工具 ----------

def _is_private_url(url: str) -> bool:
    """判断 URL 是否指向内网/本机（用于代理与下载的 SSRF 防护）。"""
    try:
        host = (urlparse(url).hostname or "").strip("[]").lower()
    except Exception:
        return True
    if not host or host in ("localhost", "localhost.localdomain"):
        return True
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return True
    return False


def _safe_media_path(subpath: str) -> Path:
    """把 /files/ 下的相对路径安全解析到 downloads 目录内，拒绝路径穿越。"""
    raw = unquote(subpath or "")
    p = Path(raw)
    if p.is_absolute() or raw.startswith(("/", "\\")):
        raise HTTPException(400, "非法路径")
    target = (DOWNLOADS_DIR / p).resolve()
    base = DOWNLOADS_DIR.resolve()
    if target != base and base not in target.parents:
        raise HTTPException(403, "禁止访问下载目录之外的文件")
    if target.suffix.lower() not in ALLOWED_MEDIA_EXTS:
        raise HTTPException(403, "仅允许访问媒体文件")
    return target


def _settings() -> dict:
    return store.load()


# ---------- 状态 ----------

@app.get("/api/status")
def api_status():
    """环境与依赖状态：ffmpeg、yt-dlp 版本、下载目录、磁盘空间。"""
    ff = find_ffmpeg()
    js = find_js_runtime()
    st = _settings()
    free = None
    try:
        free = shutil.disk_usage(str(DOWNLOADS_DIR)).free
    except OSError:
        free = None
    import yt_dlp
    # 下载根目录改了配置但还没重启 → 前端要能看出来，否则「显示的目录」和
    # 「实际下载到的目录」对不上，用户找不到文件（1B 验收专门盯这点）
    cfg_dd = str(st.get("downloads_dir") or "").strip()
    dd_pending = False
    if cfg_dd:
        try:
            dd_pending = Path(cfg_dd).resolve() != DOWNLOADS_DIR.resolve()
        except OSError:
            dd_pending = True
    return {
        "ffmpeg_available": bool(ff),
        "ffmpeg_path": ff or "",
        "ffmpeg_version": ffmpeg_version(ff) or "",
        # 1E：JS runtime 状态如实暴露（yt-dlp 解 YouTube 需要；没有时部分功能降级）
        "js_runtime": {"name": js[0], "path": js[1]} if js else None,
        "ytdlp_version": yt_dlp.version.__version__,
        "downloads_dir": str(DOWNLOADS_DIR),
        "downloads_dir_restart_pending": dd_pending,
        "disk_free_bytes": free,
        "settings": st,
        # 生效中的 cookies（条数/域名）：让用户能一眼看出「配了但没生效」
        # 还是「压根没配」——平台主页抓不到时这一步最常被忽略
        "cookies": cookie_store.status(),
        # 登录态浏览器配置：让用户知道「抖音/小红书主页」这条路是否已具备前提
        "browser_profile": browser_profile.info(),
        # V3.0：浏览器内核来源。包内 Chromium / 系统 Chrome/Edge / 无可用内核。
        # 设置页要能直接看出「我这台机器到底靠什么在渲染」——
        # 否则换个电脑抓不到东西时，用户完全无从判断是缺内核还是站点改版。
        "browser": browser_runtime.status(),
        # V3.0：验证码协助能力（bsk + 扩展 + daemon 是否就绪）。
        # 这条是「遇到验证码时会怎样」的答案，用户不需要去猜装没装。
        "browser_skill": browser_skill.status(),
        # 2.0：每个平台自己的登录态。安装包不携带账号数据，首次使用按需登录。
        "platform_logins": platform_login.all_status(),
        "filter_options": FILTER_OPTIONS,
    }


# ---------- 关键词搜图 ----------

@app.post("/api/search/images")
def api_search_images(req: SearchReq):
    """关键词搜图（Bing 主源 + DuckDuckGo 备用），支持清晰度/颜色/时间等筛选。"""
    filters = {
        "min_resolution": req.min_resolution,
        "max_resolution": req.max_resolution,
        "min_size_kb": req.min_size_kb,
        "formats": req.formats,
        "engines": req.engines,
        "size": req.size,
        "aspect": req.aspect,
        "type": req.type,
        "time": req.time,
        "color": req.color,
        "face": req.face,
        "license": req.license,
        "safesearch": req.safesearch,
    }
    try:
        res = search_images(req.keyword.strip(), filters, limit=req.limit)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"搜索失败: {type(e).__name__}: {e}")
    if not res["items"] and not res["meta"].get("engine_status"):
        raise HTTPException(502, "搜索未返回任何结果")
    return res


# ---------- 关键词搜视频 ----------

@app.post("/api/search/videos")
def api_search_videos(req: VideoSearchReq):
    """关键词搜视频（多来源 / 国内主流 / 国外主流 / 单站 / YouTube / Bilibili）。"""
    try:
        return search_videos(req.keyword.strip(), req.platform, req.count,
                             hide_downloaded=req.hide_downloaded)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"视频搜索失败: {type(e).__name__}: {e}")


# ---------- 下载台账（去重记录） ----------

@app.get("/api/history")
def api_history(limit: int = Query(default=50, ge=1, le=500)):
    """下载台账：已记下多少条「下过的视频」，以及最近的记录。

    台账按**视频身份**记账（页面地址归一 / yt-dlp 的 extractor+id），
    与文件放在哪儿无关 —— 把已下载的视频挪到别处，依然算「下过」。
    """
    from . import history

    return {"stats": history.stats(), "recent": history.recent(limit)}


@app.delete("/api/history")
def api_history_clear():
    """清空台账（等于「忘掉下过的记录」，之后搜索结果里会重新出现）。"""
    from . import history

    n = history.clear()
    return {"cleared": n, "stats": history.stats()}


@app.post("/api/history/forget")
def api_history_forget(req: HistoryForgetReq):
    """忘掉一条记录（按页面地址，或直接给身份键）。"""
    from . import history

    n = history.forget(url=req.url or "", key=req.key or "")
    return {"removed": n, "stats": history.stats()}


# ---------- 流媒体解析 ----------


# ---------- 批量抓取（列表页 → 详情页） ----------

@app.post("/api/crawl/start")
def api_crawl_start(req: CrawlReq):
    """提交批量抓取任务：给一个列表页/搜索页地址，抓出该列表下的媒体资源。

    为什么要异步：一次抓取要经过「列表页 → 翻页 → 详情页下钻」，
    遇到 JS 页面还要走浏览器渲染，实测 30~300 秒。同步接口会挂死请求，
    也无法展示进度和取消，因此走「提交 → 轮询 → 取结果」三段式。
    """
    st = _settings()
    allow_private = req.allow_private or bool(st.get("allow_private_network"))
    if not allow_private and _is_private_url(req.url):
        raise HTTPException(403, "已拒绝内网地址")
    kinds = tuple(k.strip() for k in (req.kinds or "").split(",") if k.strip())
    mode = "site" if (req.mode or "").lower() == "site" else "list"
    params = {
        "mode": mode,
        "max_pages": req.max_pages,
        "max_items": req.max_items,
        "drill_down": req.drill_down,
        "drill_mode": req.drill_mode,
        "allow_render": req.allow_render,
        "detail_concurrency": req.detail_concurrency,
        "want_kinds": list(kinds) or ["image", "video", "stream"],
        "originals_only": req.originals_only,
        "max_depth": req.max_depth,
        "same_host": req.same_host,
        "use_sitemap": req.use_sitemap,
        "expand_listing": req.expand_listing,
        # 给「视频列表前置」那一步（yt-dlp）用：TikTok / YouTube / B 站这类
        # 平台主页普遍需要登录态，yt-dlp 支持「设置」里配好的 cookies。
        "cookies_file": st.get("cookies_file") or "",
        "cookies_from_browser": st.get("cookies_from_browser") or "",
    }
    jid = crawl_manager.manager.start(req.url.strip(), params)
    return {"job_id": jid}


@app.get("/api/crawl/status/{job_id}")
def api_crawl_status(job_id: str, full: bool = Query(default=False)):
    """抓取任务进度。full=true 时一并返回完整结果（items）。"""
    job = crawl_manager.manager.get(job_id)
    if not job:
        raise HTTPException(404, "任务不存在")
    return job.public(with_result=full)


@app.get("/api/crawl/jobs")
def api_crawl_jobs(limit: int = Query(default=20, ge=1, le=100)):
    """最近的批量抓取任务列表。"""
    return {"jobs": crawl_manager.manager.list(limit)}


@app.post("/api/crawl/cancel/{job_id}")
def api_crawl_cancel(job_id: str):
    """取消进行中的抓取（已抓到的结果仍可查看）。"""
    return {"ok": crawl_manager.manager.cancel(job_id)}


# ---------------------------------------------------------------- 登录态浏览器

@app.get("/api/platforms/status")
def api_platform_status(
    refresh: bool = Query(default=False),
    platform: str = Query(default=""),
):
    """返回各平台独立登录态；refresh=true 时主动从已关闭的 Profile 重新导出 Cookie。"""
    key = str(platform or "").strip().lower()
    if refresh:
        keys = [key] if key in platform_login.PLATFORMS else list(platform_login.PLATFORMS)
        for k in keys:
            prof = str(platform_login.profile_dir(k))
            # 登录窗口仍开着时不碰 Profile，等窗口关闭后再刷新。
            if Path(prof).is_dir() and browser_profile.lock_state(prof) != "held":
                try:
                    platform_login.refresh_from_profile(k)
                except Exception:
                    pass

    items = platform_login.all_status()
    for item in items.values():
        prof = item.get("profile_dir") or ""
        item["in_use"] = bool(prof) and browser_profile.lock_state(prof) == "held"
    return {"platforms": items}


@app.post("/api/platforms/login")
def api_platform_login(payload: dict = Body(...)):
    """打开指定平台自己的持久化浏览器，不与其它平台共享 Profile/Cookies。"""
    platform = str(payload.get("platform") or "").strip().lower()
    cfg = platform_login.PLATFORMS.get(platform)
    if not cfg:
        raise HTTPException(400, "不支持的平台")
    url = str(payload.get("url") or cfg["login_url"]).strip()
    prof = str(platform_login.profile_dir(platform))
    Path(prof).mkdir(parents=True, exist_ok=True)
    cmd = subproc.build_internal_command(
        "login-browser", url, "--dir", prof,
        "--seconds", str(int(payload.get("seconds") or 900)))
    try:
        proc = subprocess.Popen(
            cmd, cwd=str(ROOT), stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env=subproc.child_env(),
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"启动登录浏览器失败：{e}")
    return {"ok": True, "pid": proc.pid, "platform": platform,
            "name": cfg["name"], "dir": prof, "url": url,
            "note": "登录完成后关闭窗口，状态会自动保存。"}


@app.get("/api/onboarding")
def api_onboarding():
    st = store.load()
    complete = (
        bool(st.get("onboarding_complete"))
        and str(st.get("onboarding_version") or "") == ONBOARDING_VERSION
    )
    return {"complete": complete,
            "onboarding_version": str(st.get("onboarding_version") or ""),
            "required_onboarding_version": ONBOARDING_VERSION,
            "downloads_dir": str(DOWNLOADS_DIR),
            "ffmpeg_available": bool(find_ffmpeg()),
            "js_runtime": bool(find_js_runtime()),
            "platforms": platform_login.all_status()}


@app.post("/api/onboarding/complete")
def api_onboarding_complete():
    store.save({
        "onboarding_complete": True,
        "onboarding_version": ONBOARDING_VERSION,
    })
    return {"ok": True, "complete": True, "onboarding_version": ONBOARDING_VERSION}


@app.post("/api/browser/login")
def api_browser_login(payload: dict = Body(...)):
    """打开一个有头浏览器让用户登录（登录态留在 profile 目录里，之后渲染复用）。

    为什么要有这个入口（2026-09-23 用户要求「Instagram/Pinterest/抖音/小红书主页要能抓到」）：
      这些平台对未登录访客只给登录墙或空壳。我们不伪造签名、也不去偷别的浏览器的登录态，
      而是给用户一个「登录一次、长期复用」的配置目录 —— 建好之后，
      渲染层会用这个 profile（有头或按设置），页面自己发签名请求、我们只拦响应。
    用**独立进程**起浏览器：不阻塞服务，也不受服务重启影响。
    """
    url = str(payload.get("url") or "").strip() or "https://www.instagram.com/"
    if not _settings().get("allow_private_network") and _is_private_url(url):
        raise HTTPException(403, "已拒绝内网地址")
    # 兼容旧入口：识别到的平台也必须落到自己的隔离目录；普通网站才允许
    # 使用高级设置里的共享 profile。
    platform = platform_login.platform_for_url(url)
    if platform:
        prof = str(platform_login.profile_dir(platform))
    else:
        prof = (str(payload.get("dir") or "").strip()
                or browser_profile.user_data_dir()
                or browser_profile.default_login_dir())
    Path(prof).mkdir(parents=True, exist_ok=True)
    # 1C：经统一 dispatcher 起 internal 子命令 —— frozen 下 sys.executable 是
    # MediaDownloader.exe，不能再假设它是 python.exe；子进程边界保持不变
    # （生命周期长、可独立终止，不改成进程内调用）。
    # CREATE_NEW_PROCESS_GROUP：登录浏览器是用户交互进程，主服务退出时不随 Job
    # 强杀（与旧 `-m` 行为一致），仅 Ctrl-C 组信号传播时可控。
    cmd = subproc.build_internal_command(
        "login-browser", url, "--dir", prof,
        "--seconds", str(int(payload.get("seconds") or 900)))
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(ROOT),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env=subproc.child_env(),
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"启动登录浏览器失败：{e}")
    return {"ok": True, "pid": proc.pid, "dir": prof, "url": url,
            "note": "浏览器窗口已打开：请完成登录，登录成功后关闭窗口即可（登录态会自动保存）。"}


@app.post("/api/browser/import_cdp")
def api_browser_import_cdp(payload: dict = Body(default={})):
    """从「用户正在用的 Chrome」导入登录态（需用启动器带调试端口打开 Chrome）。

    背景（2026-09-27 用户反馈「我浏览器里都登录了」）：Chrome 127+ 的 cookie 是
    应用绑定加密，运行中锁库、复制了也解不开 —— 唯一的路是让 Chrome 自己解密（CDP）。
    """
    from .chrome_cdp import import_from_cdp
    port = int(payload.get("port") or 9222)
    res = import_from_cdp(port=port)
    return res


@app.get("/api/browser/login/state")
def api_browser_login_state():
    """登录助手状态：profile 目录、是否可能还开着、上次登录覆盖了哪些域。

    顺带做一件事：登录窗口关闭时会自动导出一份 cookies.txt（给 yt-dlp 用）。
    如果设置里的「Cookies 文件」还是空的，就把它指过去 —— 否则用户会以为
    「登录了怎么 yt-dlp 还是不行」（登录态 profile 只对渲染那条路有效）。
    """
    prof = browser_profile.user_data_dir() or browser_profile.default_login_dir()
    st: dict = {}
    marker = Path(prof).resolve().parent / "login_state.json"
    if marker.exists():
        try:
            st = json.loads(marker.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            st = {}
    exported = str(st.get("cookie_file") or "")
    if exported and Path(exported).exists():
        try:
            if not str(store.load().get("cookies_file") or "").strip():
                store.save({"cookies_file": exported})
        except Exception:  # noqa: BLE001
            pass
    info = browser_profile.info()
    return {"dir": prof, "in_use": browser_profile.lock_state(prof) == "held",
            "configured": info, "last_login": st,
            "cookie_file": exported if exported and Path(exported).exists() else ""}


@app.post("/api/stream/resolve")
def api_stream_resolve(payload: dict = Body(...)):
    """解析 m3u8/mpd 播放列表，返回清晰度分支或分片清单。"""
    url = str(payload.get("url") or "").strip()
    if not url:
        raise HTTPException(400, "缺少 url")
    if not _settings().get("allow_private_network") and _is_private_url(url):
        raise HTTPException(403, "已拒绝内网地址")
    return resolve_stream(url, referer=str(payload.get("referer") or ""))


# ---------- 视频解析 ----------

@app.post("/api/video/probe")
def api_video_probe(req: VideoProbeReq):
    """解析视频链接，返回可用清晰度列表（不下载）。"""
    settings = _settings()
    if not settings.get("allow_private_network") and _is_private_url(req.url):
        raise HTTPException(403, "已拒绝内网地址")
    try:
        url = req.url.strip()
        if _is_xhs_url(url):
            xr = profile_sources.xhs_detail_item(url)
            if not xr.get("ok"):
                return {"ok": False, "error": xr.get("error") or "小红书解析失败", "url": url}
            item = xr["item"]
            res = _direct_item_probe(item, url, "XHS/" + str(xr.get("source") or "signed"))
        else:
            ck_file, ck_browser = _cookie_args_for_url(url, settings)
            res = probe_video(
                url,
                include_playlist=req.include_playlist,
                cookies_file=ck_file,
                cookies_from_browser=ck_browser,
            )
            if _is_instagram_url(url) and (not res.get("ok") or not res.get("thumbnail")):
                gd = profile_sources.gallery_dl_items(url, limit=1, video_only=True, retries=1)
                if gd.get("ok") and gd.get("items"):
                    gi = gd["items"][0]
                    if not res.get("ok") and gi.get("direct_url"):
                        res = probe_video(gi["direct_url"], include_playlist=False)
                        if res.get("ok"):
                            res["webpage_url"] = url
                            res["extractor"] = "gallery-dl/Instagram"
                    if res.get("ok"):
                        res["thumbnail"] = res.get("thumbnail") or gi.get("cover") or ""
                        res["title"] = res.get("title") or gi.get("title") or ""
            if not res.get("ok"):
                platform = platform_login.platform_for_url(url)
                err = str(res.get("error") or "")
                if platform and platform_login.looks_like_auth_error(err):
                    if platform_login.ready(platform):
                        platform_login.mark_invalid(platform, err)
                        res["relogin_required"] = True
                        res["error"] = (f"{platform_login.PLATFORMS[platform]['name']} 登录态已失效，"
                                        "请到「平台登录中心」重新登录。 " + err)
                    else:
                        res["login_required"] = True
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"解析失败: {type(e).__name__}: {e}")
    if not res.get("ok"):
        # 站点侧限制（风控/登录/地域）返回 200 + ok:false，让前端展示友好提示
        return res
    return res


# ---------- 下载任务 ----------

@app.post("/api/download/images")
def api_download_images(req: DownloadImagesReq):
    """批量下载搜索结果中的图片。"""
    st = _settings()
    if not st.get("allow_private_network"):
        for it in req.items:
            # 校验全部会请求到的地址（含候选档位），否则候选地址可绕过 SSRF 防护
            for u in it.remote_urls():
                if _is_private_url(u):
                    raise HTTPException(403, "已拒绝内网地址")
    items = [it.model_dump() for it in req.items]
    tid = manager.submit_images(
        items,
        output_subdir=req.subdir or st.get("downloads_subdir_images", "images"),
        title=req.title or f"图片 {len(items)} 张",
        concurrency=req.concurrency,
        skip_existing=req.skip_existing,
        prefer_quality=req.prefer_quality,
    )
    return {"task_id": tid}


@app.post("/api/download/direct")
def api_download_direct(req: DownloadDirectReq):
    """批量下载网页扫描出的媒体直链（图片或视频文件）。"""
    st = _settings()
    if not st.get("allow_private_network"):
        for it in req.items:
            # 校验全部会请求到的地址（含候选档位），否则候选地址可绕过 SSRF 防护
            for u in it.remote_urls():
                if _is_private_url(u):
                    raise HTTPException(403, "已拒绝内网地址")
    items = [it.model_dump() for it in req.items]
    source_platforms = {
        platform_login.platform_for_url(
            str(it.get("origin_page") or it.get("referer") or it.get("url") or "")
        )
        for it in items
    }
    source_platforms.discard("")
    direct_cookie_file = ""
    if len(source_platforms) == 1:
        direct_cookie_file = platform_login.cookie_file_for_platform(next(iter(source_platforms))) or ""
    tid = manager.submit_images(
        items,
        output_subdir=req.subdir or "media",
        title=req.title or f"直链文件 {len(items)} 个",
        concurrency=req.concurrency,
        skip_existing=req.skip_existing,
        prefer_quality=req.prefer_quality,
        cookies_file=direct_cookie_file,
    )
    return {"task_id": tid}


@app.post("/api/download/stream")
def api_download_stream(req: DownloadDirectReq):
    """批量下载流媒体（m3u8 / mpd）：拉分片并用 ffmpeg 合并成可播放文件。

    为什么要单独一个接口：m3u8 不是「一个文件」而是分片清单，
    走普通直链下载只会拿到几 KB 的文本清单，看着成功、其实没有内容。
    """
    st = _settings()
    if not st.get("allow_private_network"):
        for it in req.items:
            if _is_private_url(it.url):
                raise HTTPException(403, "已拒绝内网地址")
    items = [it.model_dump() for it in req.items]
    tid = manager.submit_streams(
        items,
        output_subdir=req.subdir or "streams",
        title=req.title or f"流媒体 {len(items)} 个",
        skip_existing=req.skip_existing,
        prefer_quality=req.prefer_quality,
    )
    return {"task_id": tid, "stream": stream_supported()}


@app.post("/api/download/video")
def api_download_video(req: DownloadVideoReq):
    """下载视频（支持清晰度选择、字幕、播放列表）。"""
    st = _settings()
    if not st.get("allow_private_network") and _is_private_url(req.url):
        raise HTTPException(403, "已拒绝内网地址")
    if not ffmpeg_available() and req.quality in ("best", ""):
        # 无 ffmpeg 时「最佳画质」通常需要合并分离流，提前告知
        pass
    download_url = req.url.strip()
    download_title = req.title
    if _is_xhs_url(download_url):
        xr = profile_sources.xhs_detail_item(download_url)
        if not xr.get("ok"):
            raise HTTPException(400, xr.get("error") or "小红书解析失败")
        item = xr["item"]
        download_url = item["direct_url"]
        download_title = download_title or item.get("title") or ""
    # 其它平台直接交给内置 yt-dlp；需要账号时由各自独立 Cookie 提供登录态。
    ck_file, ck_browser = _cookie_args_for_url(req.url.strip(), st)
    tid = manager.submit_video(
        download_url,
        output_subdir=req.subdir or st.get("downloads_subdir_videos", "videos"),
        title=download_title,
        quality=req.quality,
        container=req.container,
        prefer_audio_only=req.prefer_audio_only,
        write_subtitles=req.write_subtitles,
        subtitle_langs=req.subtitle_langs,
        include_playlist=req.include_playlist,
        cookies_file=ck_file or "",
        cookies_from_browser=ck_browser or "",
        rate_limit=req.rate_limit,
        format_id=req.format_id,
    )
    return {"task_id": tid, "ffmpeg_available": ffmpeg_available()}


@app.post("/api/download/videos")
def api_download_videos(req: DownloadVideosReq):
    """批量下载视频页面地址（关键词搜视频里勾选的条目）。

    逐条校验内网地址（SSRF），与单条下载同一套规则 —— 这批地址来自搜索结果，
    但**绝不能因为「来自搜索结果」就跳过校验**。
    """
    st = _settings()
    allow_private = bool(st.get("allow_private_network"))
    items = []
    for it in req.items:
        url = (it.url or "").strip()
        if not url:
            continue
        if not allow_private and _is_private_url(url):
            raise HTTPException(403, f"已拒绝内网地址：{url[:80]}")
        items.append({"url": url, "title": (it.title or "")[:60]})
    if not items:
        raise HTTPException(400, "没有可下载的地址")

    batch_ck_file = st.get("cookies_file") or ""
    batch_ck_browser = st.get("cookies_from_browser") or ""
    batch_platforms = {platform_login.platform_for_url(x["url"]) for x in items}
    batch_platforms.discard("")
    # Batch downloads must follow the same per-platform login isolation as a
    # single download. Previously only Instagram did this, so Bilibili batches
    # silently lost SESSDATA/DedeUserID and hit anti-bot/412.
    batch_concurrency = req.concurrency
    if len(batch_platforms) == 1:
        batch_ck_file, batch_ck_browser = _cookie_args_for_url(items[0]["url"], st)
        # 这里原本有一条 V2.5 的硬规则：抖音 ≥100 条时 max_workers 砍到 2。
        # 它确实避免了数百条后集中触发 fresh-cookie/412，代价是**永远只有 2 路**，
        # 400 条要跑很久，而且再也不会自己恢复。
        # V3.0 改为交给并发治理器：抖音 video_download 闸门本来就保守起步（2 路），
        # 真撞上 412 会立刻继续降，连续稳定成功后再逐级升回用户设的上限。
        # 既保留了「别一上来就冲」的保护，又不会永久卡在 2 路。
    tids = manager.submit_videos(
        items,
        output_subdir=req.subdir or st.get("downloads_subdir_videos", "videos"),
        quality=req.quality,
        container=req.container,
        include_playlist=req.include_playlist,
        concurrency=batch_concurrency,
        cookies_file=batch_ck_file or "",
        cookies_from_browser=batch_ck_browser or "",
    )
    return {"count": len(tids), "task_ids": tids,
            "concurrency": manager.video_concurrency(),
            "ffmpeg_available": ffmpeg_available()}


@app.get("/api/concurrency")
def api_concurrency():
    """并发治理状态：每个「平台 × 任务类型」闸门的上限 / 当前 / 状态 / 原因。

    前端拿它显示「用户最大并发 8，当前 2，平台限流，原因 HTTP 412，
    连续稳定后自动恢复」—— 用户必须知道程序为什么主动降速，
    否则看到下载变慢只会以为程序坏了。
    """
    return GOVERNOR.snapshot()


@app.get("/api/tasks")
def api_tasks(limit: int = Query(default=50, ge=1, le=2000)):
    """任务列表（含进度）。列表可截断，但总数/活动数按完整队列统计。"""
    return {"tasks": manager.list(limit), **manager.counts()}


@app.get("/api/tasks/{task_id}")
def api_task(task_id: str):
    """单个任务详情（含每张文件明细）。"""
    t = manager.get(task_id)
    if not t:
        raise HTTPException(404, "任务不存在")
    return t.public()


@app.post("/api/tasks/{task_id}/cancel")
def api_task_cancel(task_id: str):
    """取消任务。"""
    ok = manager.cancel(task_id)
    return {"ok": ok, "cancelled": ok}


@app.post("/api/tasks/{task_id}/retry-failed")
def api_task_retry_failed(task_id: str):
    """只重试这个任务里失败的条目，返回新任务 id。

    前端「重试失败 N 条」按钮走这里。不重试跳过项（文件已存在/历史上已下过），
    也不重试用户主动取消的条目 —— 那是用户自己放弃的，不是失败。
    """
    try:
        res = manager.retry_failed(task_id)
    except KeyError:
        raise HTTPException(404, "任务不存在")
    if not res.get("ok"):
        raise HTTPException(400, res.get("message") or "没有可重试的失败条目")
    return res


@app.delete("/api/tasks/{task_id}")
def api_task_remove(task_id: str):
    """移除任务记录（不删除已下载的文件）。"""
    return {"ok": manager.remove(task_id)}


@app.post("/api/tasks/clear-finished")
def api_tasks_clear():
    """清空所有已结束的任务记录。"""
    return {"removed": manager.clear_finished()}


# ---------- 设置 ----------

@app.get("/api/settings")
def api_get_settings():
    return _settings()


@app.post("/api/settings")
def api_set_settings(req: SettingsReq):
    patch = {k: v for k, v in req.model_dump().items() if v is not None}
    # 下载根目录：留空 = 回到默认；填了必须是绝对路径（相对路径会让
    # is_safe_download_path / /files 的目录安全校验语义变得不可预测）
    dd = str(patch.get("downloads_dir") or "").strip()
    if dd and not Path(dd).is_absolute():
        raise HTTPException(400, "下载根目录必须是绝对路径（如 D:\\Downloads\\MediaDownloader）")
    data = store.save(patch)
    # 下载根目录由导入期常量 DOWNLOADS_DIR 消费，改了配置当前进程也不会跟着走 ——
    # 必须明确告诉前端「重启后才生效」，杜绝「显示已保存但仍在写旧目录」的静默分叉
    cfg_dd = str(data.get("downloads_dir") or "").strip()
    restart_required = False
    if cfg_dd:
        try:
            restart_required = Path(cfg_dd).resolve() != DOWNLOADS_DIR.resolve()
        except OSError:
            restart_required = True
    return {"ok": True, "settings": data, "restart_required": restart_required}


@app.post("/api/open-url")
def api_open_url(payload: dict = Body(...)):
    """用系统默认浏览器打开 http(s) 外部页面。

    pywebview/WebView2 对 target=_blank 的处理不稳定，桌面版统一经后端调用
    系统默认浏览器，避免“打开/来源页”点击无反应。
    """
    url = str(payload.get("url") or "").strip()
    try:
        parsed = urlparse(url)
    except ValueError:
        parsed = None
    if not parsed or parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(400, "仅允许打开 http(s) 外部地址")
    if len(url) > 2000:
        raise HTTPException(400, "地址过长")
    opened = False
    method = ""
    if os.name == "nt":
        # Windows 桌面版优先直接走 ShellExecute。它与资源管理器“打开网页”
        # 使用同一套系统关联，比 WebView2 的 target=_blank / window.open 稳定。
        try:
            import ctypes
            rc = int(ctypes.windll.shell32.ShellExecuteW(
                None, "open", url, None, None, 1
            ))
            opened = rc > 32
            method = "ShellExecuteW" if opened else ""
        except Exception:
            opened = False
        if not opened:
            try:
                os.startfile(url)  # type: ignore[attr-defined]
                opened = True
                method = "os.startfile"
            except Exception:
                opened = False
    if not opened:
        try:
            opened = bool(webbrowser.open_new_tab(url))
            method = "webbrowser" if opened else ""
        except Exception:
            opened = False
    if not opened:
        raise HTTPException(500, "系统默认浏览器未能打开该地址")
    return {"ok": True, "method": method}


# ---------- 文件访问与代理 ----------

@app.get("/files/{subpath:path}")
def api_get_file(subpath: str):
    """访问下载目录内的媒体文件（本地预览/打开）。"""
    target = _safe_media_path(subpath)
    if not target.exists() or not target.is_file():
        raise HTTPException(404, "文件不存在")
    return FileResponse(str(target), filename=target.name)


@app.get("/proxy")
def api_proxy(url: str = Query(..., max_length=2000), referer: str = Query(default="", max_length=2000)):
    """图片代理：解决图床防盗链（需要 Referer）导致前端缩略图空白的问题。

    安全：默认拒绝内网/本机地址，避免本接口被当作 SSRF 跳板。
    实现：缓冲后一次性返回（上限 4MB）。不用 StreamingResponse 的原因是，
    上游中途断开时流式响应会把异常泄漏进响应体，浏览器会收到损坏的
    chunked 数据（Response ended prematurely）而不是一个干净的错误。
    """
    if not url.startswith(("http://", "https://")):
        raise HTTPException(400, "仅支持 http(s) 地址")
    if not _settings().get("allow_private_network") and _is_private_url(url):
        raise HTTPException(403, "已拒绝代理内网地址")

    headers = dict(HEADERS)
    if referer:
        headers["Referer"] = referer

    # 预览代理与真实下载必须使用同一平台登录态。微博/Sina 图床在匿名
    # 预览时可能 403，但同一图片通过带 Cookie 的下载器却能正常下载。
    proxy_session = requests.Session()
    proxy_platform = platform_login.platform_for_url(referer or "") or platform_login.platform_for_url(url)
    if proxy_platform:
        ck_file = platform_login.cookie_file_for_platform(proxy_platform) or ""
        if ck_file:
            try:
                from http.cookiejar import MozillaCookieJar
                jar = MozillaCookieJar(str(ck_file))
                jar.load(ignore_discard=True, ignore_expires=True)
                proxy_session.cookies.update(jar)
            except Exception:
                pass

    # 瞬时错误必须重试，且超时不能太紧。
    # 实测（avjoy.me 封面）：同一个文件串行请求 3/3 成功、1~2 秒返回，
    # 但**并发 9 张时有 2 张 ReadTimeout** —— 上游 CDN 在并发下会偶发卡住。
    # 不重试就直接 502，前端只能显示「预览不可用」。
    # 这与 fetch_page / fetch_many 的纪律保持一致：瞬时错误先重试，再判失败。
    TRANSIENT = (requests.exceptions.Timeout,
                 requests.exceptions.ConnectionError,
                 requests.exceptions.SSLError,
                 requests.exceptions.ChunkedEncodingError)
    r = None
    last_err = ""
    for attempt in range(1, 4):
        try:
            r = proxy_session.get(url, headers=headers, timeout=20, stream=True,
                                    allow_redirects=True)
            if r.status_code == 403 and referer:
                try:
                    src = urlparse(referer)
                    root_ref = f"{src.scheme or 'https'}://{src.netloc}/" if src.netloc else ""
                except Exception:
                    root_ref = ""
                if root_ref and root_ref != headers.get("Referer"):
                    r.close()
                    h2 = dict(headers)
                    h2["Referer"] = root_ref
                    r = proxy_session.get(url, headers=h2, timeout=20, stream=True,
                                            allow_redirects=True)
            break
        except TRANSIENT as e:
            last_err = type(e).__name__
            r = None
            if attempt < 3:
                time.sleep(0.3 * attempt)
        except requests.exceptions.RequestException as e:
            last_err = type(e).__name__
            r = None
            break
    if r is None:
        raise HTTPException(502, f"代理请求失败: {last_err or '未知错误'}")

    try:
        if r.status_code >= 400:
            raise HTTPException(502, f"源站返回 {r.status_code}")

        ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype and not (ctype.startswith("image/") or ctype.startswith("video/")
                          or ctype.startswith("audio/") or ctype == "application/octet-stream"):
            raise HTTPException(415, f"目标不是媒体文件（{ctype}）")

        # 边读边限流：达到上限即停止，避免被超大文件拖垮
        buf = bytearray()
        truncated = False
        try:
            for chunk in r.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                if len(buf) + len(chunk) > MAX_THUMB_BYTES:
                    buf.extend(chunk[: MAX_THUMB_BYTES - len(buf)])
                    truncated = True
                    break
                buf.extend(chunk)
        except requests.exceptions.RequestException:
            # 上游中途断开：已拿到足够可解码的数据就照常返回，否则干净报错
            if len(buf) < 1024:
                raise HTTPException(502, "源站连接中断")

        if not buf:
            raise HTTPException(502, "源站返回空内容")

        # 截断的媒体无法解码，直接告知而不是回传半截文件
        if truncated:
            raise HTTPException(413, f"媒体文件超过 {MAX_THUMB_BYTES // 1024 // 1024}MB 代理上限")

        resp_headers = {"Cache-Control": "public, max-age=3600"}
        if ctype == "image/svg+xml":
            # SVG 可能含脚本，禁止内联执行
            resp_headers["Content-Security-Policy"] = "default-src 'none'; sandbox"
            resp_headers["X-Content-Type-Options"] = "nosniff"
        return Response(content=bytes(buf), media_type=ctype or "application/octet-stream",
                        headers=resp_headers)
    finally:
        r.close()


@app.get("/api/downloads/tree")
def api_downloads_tree():
    """列出下载目录中的媒体，并附带可用的来源页/缩略图元数据。"""
    root = DOWNLOADS_DIR.resolve()
    out: list[dict] = []

    # 下载历史的 path 只是展示元数据，不参与“是否下载过”的身份判断。
    # 这里反向用它给已下载文件补来源页和远端封面。
    meta_by_path: dict[str, dict] = {}
    try:
        from . import history
        for row in history.recent(5000):
            hp = str((row or {}).get("path") or "").strip()
            if not hp:
                continue
            try:
                # recent() is newest-first. Keep the first row for a path so an
                # older duplicate record cannot erase a newer source/thumbnail.
                meta_by_path.setdefault(str(Path(hp).resolve()).lower(), row)
            except OSError:
                continue
    except Exception:
        meta_by_path = {}

    try:
        for p in sorted(root.rglob("*")):
            if not p.is_file() or p.name.startswith("."):
                continue
            if p.suffix.lower() not in ALLOWED_MEDIA_EXTS:
                continue
            if ".part." in p.name or p.name.endswith(".part"):
                continue
            try:
                rel = p.relative_to(root).as_posix()
            except ValueError:
                continue
            st = p.stat()
            meta = meta_by_path.get(str(p.resolve()).lower(), {})
            out.append({
                "name": p.name,
                "title": str(meta.get("title") or ""),
                "rel": rel,
                "href": f"/files/{rel}",
                "size": st.st_size,
                "mtime": st.st_mtime,
                "dir": str(p.parent.relative_to(root).as_posix()) if p.parent != root else "",
                "is_video": p.suffix.lower() in VID_EXTS,
                "source_url": str(meta.get("source_url") or meta.get("url") or ""),
                "thumbnail": str(meta.get("thumbnail") or ""),
            })
    except OSError:
        pass
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return {"root": str(root), "count": len(out), "files": out[:1000]}

@app.post("/api/downloads/open")
def api_open_downloads(payload: dict = Body(default={})):
    """在 Explorer 中打开下载根目录、子目录，或任务给出的绝对输出目录。"""
    root = DOWNLOADS_DIR.resolve()
    raw_path = str(payload.get("path") or "").strip()
    sub = str(payload.get("sub") or "").strip()

    if raw_path:
        cand = Path(raw_path).resolve()
        if cand != root and root not in cand.parents:
            raise HTTPException(403, "禁止打开下载目录之外的位置")
        target = cand if cand.is_dir() else cand.parent
    elif sub:
        cand = (root / sub).resolve()
        if cand != root and root not in cand.parents:
            raise HTTPException(403, "禁止打开下载目录之外的位置")
        target = cand
    else:
        target = root

    if not target.exists():
        raise HTTPException(404, "目录不存在")
    try:
        subprocess.Popen(["explorer", str(target)],
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return {"ok": True, "path": str(target)}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"打开失败: {type(e).__name__}")


@app.post("/api/downloads/reveal")
def api_reveal_download(payload: dict = Body(default={})):
    """在 Explorer 中定位一个已下载文件。路径仍受 _safe_media_path 约束。"""
    rel = str(payload.get("rel") or "").strip()
    if not rel:
        raise HTTPException(400, "缺少文件路径")
    target = _safe_media_path(rel)
    if not target.exists() or not target.is_file():
        raise HTTPException(404, "文件不存在")
    try:
        subprocess.Popen(["explorer", f"/select,{target}"],
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return {"ok": True, "path": str(target)}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"定位失败: {type(e).__name__}")

@app.get("/api/health")
def api_health():
    # app 标记（1D）：启动器靠它区分「本工具已在运行」与「其它程序恰好在监听端口」，
    # 见 app/ports.probe_running —— 不要删。
    return {"ok": True, "app": "MediaDownloader", "root": str(ROOT)}


# ---------- 前端静态资源 ----------
# 必须放在所有 API 路由之后：Starlette 按注册顺序匹配，
# "/" 挂载点会拦截其后注册的一切路径（含 /api/health）。

if WEB_DIR.exists():
    app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
