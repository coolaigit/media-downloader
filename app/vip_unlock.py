# -*- coding: utf-8 -*-
"""会员视频兜底：把「只下到试看片段」变成「下到完整版」。

适用：B 站大会员番剧/课程、优酷/爱奇艺/腾讯视频会员片 —— 这些内容在服务端按
账号权限裁剪，没有会员态时播放地址本身就只覆盖试看区间，**不存在「改个参数
就拿到完整流」**的漏洞。所以要拿到完整版，只能换一条不受该平台会员校验约束的
链路。本模块按下面顺序尝试，第一条成功即返回：

  ① 会员 Cookie 直取（仅 B 站）：用配置的大会员 Cookie 调 playurl。
     命中 1080P+/4K 的 DASH 流时直接装；`is_preview=1` 说明这个号也没权限，跳过。
  ② 资源站换源（通用，主力）：拿页面标题去影视资源站（app/resource_sites.py）
     找同片/同集，取它的 m3u8。实测 10 个资源站可用，播放列表能下到分片。
  ③ 解析网关（可选）：用户自备的公共解析接口，返回 m3u8/mp4 就用。

判定「试看」的依据是**时长**：页面/API 标称时长 > 实际文件时长 × 0.85 即视为
被裁剪（优酷和 B 站都会给一个 6 分钟左右的 preview，标称时长却是正片长度）。
这比按平台写死规则通用得多，也符合本项目「不写站点专用规则」的红线 ——
平台相关代码只出现在「拿元数据」和「B 站 playurl」这两处能力适配里。

对外入口：
  detect_preview(expected, actual)        时长裁剪判定
  extract_meta(url)                       标题/年份/集数/标称时长
  resolve(url, meta=None)                 ①→②→③ 兜底链
  unlock(url, output_dir, ...)            解析 + 下载，一行到位
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

import requests

from . import resource_sites, store
from .config import DATA_ROOT, DEFAULT_UA, HEADERS, TIMEOUT, find_ffmpeg

# 需要会员才完整播放的平台（只用于「要不要走兜底」的判断与元数据适配）
_VIP_HOSTS = {
    "bilibili.com": "bilibili",
    "b23.tv": "bilibili",
    "youku.com": "youku",
    "iqiyi.com": "iqiyi",
    "v.qq.com": "qq",
    "mgtv.com": "mgtv",
    "sohu.com": "sohu",
    "le.com": "letv",
    "pptv.com": "pptv",
    "tudou.com": "tudou",
}

_BILI_HOST_RE = re.compile(r"(^|\.)bilibili\.com$", re.I)
_BV_RE = re.compile(r"/video/(BV[0-9A-Za-z]{10})", re.I)
_AV_RE = re.compile(r"/video/av(\d+)", re.I)
_EP_RE = re.compile(r"/bangumi/play/(ep\d+)", re.I)
_SS_RE = re.compile(r"/bangumi/play/ss(\d+)", re.I)
_EPID_RE = re.compile(r"^ep(\d+)$", re.I)

_BILI_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# 解析网关内置池：2026-11-04 实测这批公共站全部只剩广告 iframe，没有真流，
# 所以默认不启用（避免每次兜底白等十几秒）。用户有可用接口时在设置里填。
_GATEWAY_BUILTIN: list[str] = []


# ---------------------------------------------------------------- 元数据

def platform_of(url: str) -> str:
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""
    for suffix, name in _VIP_HOSTS.items():
        if host == suffix or host.endswith("." + suffix):
            return name
    return ""


def bilibili_ids(url: str) -> dict:
    """从 B 站地址里解出 bvid / aid / ep_id / ss_id / 分 P。"""
    out: dict = {}
    m = _BV_RE.search(url)
    if m:
        out["bvid"] = m.group(1)
    m = _AV_RE.search(url)
    if m:
        out["aid"] = int(m.group(1))
    m = _EP_RE.search(url)
    if m:
        out["ep_id"] = int(re.sub(r"\D", "", m.group(1)))
    m = _SS_RE.search(url)
    if m:
        out["season_id"] = int(m.group(1))
    try:
        qs = parse_qs(urlparse(url).query)
        if "p" in qs:
            out["page"] = int(qs["p"][0])
    except Exception:  # noqa: BLE001
        pass
    return out


def _get_json(url: str, *, params: dict | None = None, cookie: str = "",
              referer: str = "https://www.bilibili.com/", timeout: int = 15) -> dict:
    headers = {"User-Agent": _BILI_UA, "Referer": referer,
               "Accept": "application/json, text/plain, */*"}
    if cookie:
        headers["Cookie"] = cookie
    r = requests.get(url, params=params, headers=headers, timeout=timeout)
    return r.json()


def _text_of_html(html: str, base: str = "") -> str:
    """从页面 HTML 里取最像标题的一段（og:title > twitter:title > <title>）。"""
    if not html:
        return ""
    for pat in (
        r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']{1,300})',
        r'<meta[^>]+content=["\']([^"\']{1,300})["\'][^>]+property=["\']og:title',
        r'<meta[^>]+name=["\']twitter:title["\'][^>]+content=["\']([^"\']{1,300})',
        r"<title[^>]*>(.{1,300}?)</title>",
    ):
        m = re.search(pat, html, re.I | re.S)
        if m:
            return re.sub(r"\s+", " ", m.group(1)).strip()
    return ""


def _page_text(url: str, timeout: int = 15) -> str:
    """抓页面（只为了标题），失败返回空串 —— 标题不是必需项。"""
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout)
        if r.status_code != 200:
            return ""
        r.encoding = r.apparent_encoding or r.encoding
        return r.text or ""
    except requests.RequestException:
        return ""


def bilibili_meta(url: str) -> dict:
    """B 站元数据：标题 / 集数 / 标称时长（ms）。

    优先官方 API（不依赖页面渲染）；番剧用 pgc/season，普通投稿用 x/web-interface/view。
    """
    ids = bilibili_ids(url)
    meta: dict = {"platform": "bilibili", "title": "", "episode": None,
                  "duration": 0.0, "kind": "video", "ids": ids, "desc": ""}

    if ids.get("ep_id"):
        try:
            d = _get_json("https://api.bilibili.com/pgc/view/web/season",
                          params={"ep_id": ids["ep_id"]})
            res = d.get("result") or {}
            meta["title"] = res.get("season_title") or res.get("title") or ""
            meta["kind"] = "bangumi"
            for idx, ep in enumerate(res.get("episodes") or []):
                if str(ep.get("id")) == str(ids["ep_id"]):
                    raw_title = str(ep.get("title") or "").strip()
                    # 番剧单集的 title 就是集号（"1"），long_title 才是本集名字
                    if raw_title.isdigit():
                        meta["episode"] = int(raw_title)
                    else:
                        meta["episode"] = (
                            resource_sites.parse_episode(raw_title)
                            or resource_sites.parse_episode(str(ep.get("long_title") or ""))
                            or idx + 1
                        )
                    meta["episode_title"] = ep.get("long_title") or ""
                    # 单集时长：ep.duration 单位毫秒（部分接口没给，退回 season 的）
                    dur_ms = ep.get("duration") or 0
                    if dur_ms:
                        meta["duration"] = float(dur_ms) / 1000.0
                    meta["cover"] = ep.get("cover") or ""
                    break
            meta["desc"] = res.get("evaluate") or ""
            if not meta["title"]:
                meta["title"] = res.get("title") or ""
            # 首播年份：年份对得上能大幅降低「同名不同版」的误匹配
            pub = str((res.get("publish") or {}).get("pub_time") or "")
            ym = re.search(r"(19|20)\d{2}", pub)
            if ym:
                meta["year"] = ym.group(0)
        except Exception as e:  # noqa: BLE001
            meta["error"] = f"pgc/season: {type(e).__name__}"
        return meta

    if ids.get("bvid") or ids.get("aid"):
        params = {"bvid": ids["bvid"]} if ids.get("bvid") else {"aid": ids.get("aid")}
        try:
            d = _get_json("https://api.bilibili.com/x/web-interface/view", params=params)
            res = d.get("data") or {}
            meta["title"] = res.get("title") or ""
            meta["duration"] = float(res.get("duration") or 0)
            meta["desc"] = (res.get("desc") or "")[:300]
            meta["cover"] = res.get("pic") or ""
            meta["ids"].update({"bvid": res.get("bvid") or "", "aid": res.get("aid"),
                                "cid": res.get("cid")})
            page_n = int(ids.get("page") or 1)
            pages = res.get("pages") or []
            if 0 < page_n <= len(pages):
                meta["ids"]["cid"] = pages[page_n - 1].get("cid") or meta["ids"].get("cid")
                meta["duration"] = float(pages[page_n - 1].get("duration") or meta["duration"])
            meta["episode"] = resource_sites.parse_episode(meta["title"])
        except Exception as e:  # noqa: BLE001
            meta["error"] = f"view: {type(e).__name__}"
        return meta
    return meta


def _render_title(url: str, timeout_ms: int = 12000) -> str:
    """页面标题是站点名时，用内置内核渲染一次拿 document.title。

    实测腾讯视频的播放页 HTML 里完全没有片名（JS 外壳），og:title 就是
    「腾讯视频」；渲染后 title 才变成「庆余年2 第1集…」。只在标题不可用时
    才走这一步，平时不付渲染成本。
    """
    try:
        from .renderer import render_page, renderer_status
        if not renderer_status().get("available"):
            return ""
        r = render_page(url, timeout_ms=timeout_ms, settle_ms=2500,
                        scroll_rounds=0, capture_types=(), force_headless=True)
        return (r.title or "").strip() if r.ok else ""
    except Exception:  # noqa: BLE001
        return ""


def extract_meta(url: str, timeout: int = 15) -> dict:
    """拿页面标题/年份/集数/标称时长。平台有 API 的优先用 API。"""
    plat = platform_of(url)
    if plat == "bilibili":
        meta = bilibili_meta(url)
        if meta.get("title"):
            return meta
    html = _page_text(url, timeout=timeout)
    title = _text_of_html(html, url)
    # 标题无效（站点名/空）→ 渲染一次再取
    if not title_usable(title):
        rendered = _render_title(url)
        if title_usable(rendered):
            title = rendered
    meta = {
        "platform": plat or "web",
        "title": title,
        "episode": resource_sites.parse_episode(title),
        "year": resource_sites.guess_year(title),
        "duration": 0.0,
        "kind": "video",
        "ids": {},
        "desc": "",
    }
    # 有些站点把时长写在 meta / 页面 JSON 里（优酷的 "seconds"、爱奇艺的 duration…）。
    # 这一步是「试看判定」的关键分母：拿不到标称时长就只能拿 yt-dlp 自报的，
    # 而试看场景下 yt-dlp 自报的往往就是被裁剪后的长度，等于永远判不出来。
    candidates: list[float] = []
    m = re.search(r'<meta[^>]+(?:itemprop|property)=["\']'
                  r'(?:duration|video:duration)["\'][^>]+content=["\']([^"\']+)', html, re.I)
    if m:
        candidates.append(_parse_duration(m.group(1)))
    for pat in (r'"seconds"\s*:\s*"?(\d{2,6})',
                r'"duration"\s*:\s*"?(\d{2,6})',
                r'video:duration["\']?\s*[:=]\s*["\']?(\d{2,6})'):
        mm = re.search(pat, html, re.I)
        if mm:
            try:
                candidates.append(float(mm.group(1)))
            except ValueError:
                pass
    plausible = [v for v in candidates if 60 <= v <= 6 * 3600]
    if plausible:
        meta["duration"] = max(plausible)
    return meta


def _parse_duration(text: str) -> float:
    t = (text or "").strip()
    if not t:
        return 0.0
    # ISO8601：PT1H2M3.4S（schema.org / og:video:duration 常见）
    m = re.fullmatch(r"P(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?)?", t, re.I)
    if m and any(m.groups()):
        return (int(m.group(1) or 0) * 3600 + int(m.group(2) or 0) * 60
                + float(m.group(3) or 0))
    try:
        if re.fullmatch(r"\d+(\.\d+)?", t):
            v = float(t)
            # 毫秒判据：B 站 ep.duration 是 1494000 这种；纯秒数不会超过 20 小时
            return v / 1000.0 if v > 72000 else v
    except ValueError:
        pass
    parts = [p for p in re.split(r"[:：]", t) if p.strip()]
    try:
        nums = [float(p) for p in parts]
    except ValueError:
        return 0.0
    if len(nums) == 3:
        return nums[0] * 3600 + nums[1] * 60 + nums[2]
    if len(nums) == 2:
        return nums[0] * 60 + nums[1]
    return nums[0] if nums else 0.0


# ---------------------------------------------------------------- 试看判定

def detect_preview(expected: float, actual: float, *, min_ratio: float = 0.85,
                   min_gap: float = 90.0) -> bool:
    """实际时长明显短于标称时长 → 判定为试看片段。

    两个门槛都要满足，避免把「片头片尾被裁掉几十秒」误判成试看：
      - actual < expected * min_ratio
      - expected - actual > min_gap（默认 90 秒）
    """
    try:
        e, a = float(expected or 0), float(actual or 0)
    except (TypeError, ValueError):
        return False
    if e <= 0 or a <= 0:
        return False
    return a < e * min_ratio and (e - a) > min_gap


def probe_duration(path: str | Path) -> float:
    """读本地文件时长（秒）。

    优先 ffprobe；安装包里可能只带 ffmpeg.exe 没带 ffprobe.exe，所以再退回
    `ffmpeg -i` 的 stderr（里面一定有 `Duration: 00:24:53.12`）。
    """
    p = Path(path)
    if not p.exists() or p.stat().st_size <= 0:
        return 0.0
    ff = find_ffmpeg()
    if not ff:
        return 0.0
    exe = Path(ff).with_name("ffprobe.exe")
    if exe.exists():
        cmd = [str(exe), "-v", "error", "-show_entries", "format=duration",
               "-of", "default=nw=1:nk=1", str(p)]
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=20,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            return float((out.stdout or "").strip().splitlines()[0])
        except Exception:  # noqa: BLE001
            pass
    try:
        out = subprocess.run([str(ff), "-hide_banner", "-i", str(p)],
                             capture_output=True, text=True, timeout=20,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:  # noqa: BLE001
        return 0.0
    m = re.search(r"Duration:\s*(\d+):(\d{2}):(\d{2}(?:\.\d+)?)", out.stderr or "")
    if not m:
        return 0.0
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))


# ---------------------------------------------------------------- ① 会员 Cookie 直取

def vip_cookie_dir() -> Path:
    st = store.load()
    raw = str(st.get("vip_cookie_dir") or "").strip()
    d = Path(raw).expanduser() if raw else (DATA_ROOT / "vip_cookies")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _cookie_line(raw: str) -> str:
    """把 Netscape cookies.txt 或 `SESSDATA=...; bili_jct=...` 统一成 Cookie 请求头。"""
    text = (raw or "").strip()
    if not text:
        return ""
    if "SESSDATA" in text and "\t" in text:
        pairs = []
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            cols = line.split("\t")
            if len(cols) >= 7:
                pairs.append(f"{cols[5]}={cols[6]}")
        return "; ".join(pairs)
    if "\n" in text and "=" in text:
        # 多行 key=value
        pairs = [ln.strip() for ln in text.splitlines() if "=" in ln]
        return "; ".join(pairs)
    return text


def list_vip_cookies() -> list[dict]:
    """列出 vip_cookies 目录下每一份 Cookie 的会员状态（用于挑号）。"""
    out: list[dict] = []
    d = vip_cookie_dir()
    for p in sorted(d.glob("*.txt")):
        try:
            cookie = _cookie_line(p.read_text(encoding="utf-8", errors="ignore"))
        except OSError:
            continue
        if not cookie:
            continue
        item = {"file": p.name, "cookie": cookie, "vip": False, "uname": "", "error": ""}
        try:
            d2 = _get_json("https://api.bilibili.com/x/web-interface/nav",
                           params=None, cookie=cookie,
                           referer="https://www.bilibili.com/")
            data = d2.get("data") or {}
            item["vip"] = bool(data.get("vipStatus"))
            item["uname"] = data.get("uname") or ""
            item["vip_label"] = "大会员" if data.get("vipStatus") else "普通/未登录"
            if not data.get("isLogin"):
                item["error"] = "Cookie 未登录或已过期"
        except Exception as e:  # noqa: BLE001
            item["error"] = f"{type(e).__name__}"
        out.append(item)
    return out


def pick_bili_cookie() -> str:
    """挑一份大会员 Cookie；没有会员号时退回第一份已登录 Cookie（能解 1080P）。"""
    items = list_vip_cookies()
    for it in items:
        if it.get("vip"):
            return it["cookie"]
    for it in items:
        if not it.get("error"):
            return it["cookie"]
    # 最后退回设置里的通用 cookies（cookies.txt）里的 SESSDATA
    try:
        from . import cookie_store
        ck = cookie_store.header_for("bilibili.com")
        if ck:
            return ck
    except Exception:  # noqa: BLE001
        pass
    return ""


def bili_playurl(url: str, cookie: str = "", qn: int = 127) -> dict:
    """B 站取流（DASH 优先，退回 durl）。返回 {ok, urls[], duration, quality, preview, error}"""
    ids = bilibili_ids(url)
    cookie = cookie or pick_bili_cookie()
    params_base = {"qn": qn, "fnval": 4048, "fourk": 1, "platform": "pc"}
    try:
        if ids.get("ep_id"):
            params = dict(params_base, ep_id=ids["ep_id"])
            api = "https://api.bilibili.com/pgc/player/web/playurl"
            if ids.get("season_id"):
                params["season_id"] = ids["season_id"]
        elif ids.get("bvid") or ids.get("aid"):
            meta = bilibili_meta(url)
            cid = (meta.get("ids") or {}).get("cid")
            if not cid:
                return {"ok": False, "error": "拿不到 cid（视频可能已删除）"}
            params = dict(params_base, cid=cid)
            if ids.get("bvid"):
                params["bvid"] = ids["bvid"]
            else:
                params["avid"] = ids["aid"]
            api = "https://api.bilibili.com/x/player/playurl"
        else:
            return {"ok": False, "error": "不是可识别的 B 站视频地址"}
        d = _get_json(api, params=params, cookie=cookie)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"playurl 请求失败: {type(e).__name__}"}

    if d.get("code") not in (0, None):
        return {"ok": False, "error": f"playurl 返回 {d.get('code')}: {d.get('message')}"}
    res = d.get("result") or d.get("data") or {}
    if isinstance(res, dict) and isinstance(res.get("result"), dict):
        res = res["result"]
    preview = bool(res.get("is_preview"))
    out = {"ok": False, "urls": [], "duration": 0.0, "quality": res.get("quality"),
           "preview": preview, "cookie_used": bool(cookie), "error": ""}

    dash = res.get("dash") or {}
    videos = dash.get("video") or []
    audios = dash.get("audio") or []
    if videos:
        # 同 qn 会有多条编码（avc/hevc/av1），优先 avc（兼容性最好），再按清晰度
        def _rank(v):
            codec = str(v.get("codecs") or "")
            pref = 0 if codec.startswith("avc") else (1 if codec.startswith("hvc") else 2)
            return (-(v.get("id") or 0), pref)
        best = sorted(videos, key=_rank)[0]
        out["urls"].append(best.get("baseUrl") or best.get("base_url") or "")
        if audios:
            a = sorted(audios, key=lambda x: -(x.get("id") or 0))[0]
            out["urls"].append(a.get("baseUrl") or a.get("base_url") or "")
        out["quality"] = best.get("id") or res.get("quality")
        out["kind"] = "dash"
    elif res.get("durl"):
        out["urls"].append(res["durl"][0].get("url") or "")
        out["kind"] = "durl"
    out["urls"] = [u for u in out["urls"] if u]
    out["duration"] = float(res.get("timelength") or 0) / 1000.0
    if not out["urls"]:
        out["error"] = "playurl 没有返回可下载地址（可能需要大会员 Cookie）"
        return out
    if preview:
        out["error"] = "该 Cookie 没有大会员权限，playurl 只返回试看段"
        return out
    out["ok"] = True
    return out


def _headers_for_bili(cookie: str) -> str:
    lines = [f"User-Agent: {_BILI_UA}",
             "Referer: https://www.bilibili.com/",
             "Origin: https://www.bilibili.com",
             "Accept: */*"]
    if cookie:
        lines.append(f"Cookie: {cookie}")
    return "\r\n".join(lines) + "\r\n"


def download_bili(url: str, output_dir: Path, *, title: str = "",
                  cookie: str = "", on_progress=None) -> dict:
    """用 ffmpeg 直接装 B 站的 DASH/durl（带 Referer+Cookie）。"""
    play = bili_playurl(url, cookie)
    if not play.get("ok"):
        return {"ok": False, "error": play.get("error") or "B 站取流失败"}
    ff = find_ffmpeg()
    if not ff:
        return {"ok": False, "error": "未找到 ffmpeg，无法合并 B 站 DASH 流"}
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = resource_sites.clean_title(title) or "bilibili"
    stem = re.sub(r'[\\/:*?"<>|]+', "_", stem)[:110] or "bilibili"
    target = output_dir / f"{stem}.mp4"
    i = 1
    while target.exists():
        target = output_dir / f"{stem} ({i}).mp4"
        i += 1
    headers = _headers_for_bili(cookie or pick_bili_cookie())
    urls = play.get("urls") or []
    cmd = [str(ff), "-y", "-loglevel", "error", "-headers", headers, "-i", urls[0]]
    if len(urls) > 1:
        cmd += ["-headers", headers, "-i", urls[1]]
    cmd += ["-c", "copy", str(target)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=7200,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "B 站下载超时（2 小时）"}
    if proc.returncode != 0 or not target.exists() or target.stat().st_size < 10240:
        err = (proc.stderr or "").strip().splitlines()
        return {"ok": False, "error": "ffmpeg 合并失败: " + (err[-1][:200] if err else str(proc.returncode))}
    return {"ok": True, "path": str(target), "size": target.stat().st_size,
            "quality": play.get("quality"), "backend": "bilibili-cookie",
            "duration": play.get("duration") or 0.0}


# ---------------------------------------------------------------- ② 资源站换源

def _play_page_stream(url: str, timeout: int = 15) -> str:
    """资源站的播放地址可能是「播放页」，页里再给 m3u8。两步都处理。"""
    u = (url or "").strip()
    if not u:
        return ""
    low = u.lower().split("?")[0]
    if low.endswith((".m3u8", ".mp4")):
        return u
    try:
        r = requests.get(u, headers={**HEADERS, "Referer": "https://www.google.com/"},
                         timeout=timeout)
        body = r.text or ""
    except requests.RequestException:
        body = ""
    for pat in (r'https?:(?:\\/\\/|//)[^"\'\\\s<>]+?\.m3u8[^"\'\\\s<>]*',
                r'["\'](https?://[^"\']+?\.m3u8[^"\']*)["\']'):
        m = re.search(pat, body, re.I)
        if m:
            cand = m.group(1).replace("\\/", "/")
            if cand.startswith("//"):
                cand = "https:" + cand
            return cand
    # 常见播放页约定：/play/<id> → /play/<id>/index.m3u8
    if body:
        cand = u.rstrip("/") + "/index.m3u8"
        try:
            head = requests.get(cand, headers=HEADERS, timeout=timeout)
            if head.status_code == 200 and "#EXTM3U" in (head.text or "")[:200]:
                return cand
        except requests.RequestException:
            pass
    return ""


# 解说/影评/预告类标题不做换源：这类视频的标题里往往带着正片名字，
# 直接拿去资源站会匹配到正片，结果「想看解说，下回来一整部剧」。
_CLIP_HINTS = ("解读", "解说", "影评", "吐槽", "混剪", "预告", "花絮", "片花",
               "盘点", "reaction", "review", "trailer")

# 站点名当标题 = 页面没给真实片名（JS 渲染站常见），此时换源必然匹配错片。
_GENERIC_TITLES = {"腾讯视频", "爱奇艺", "优酷", "芒果tv", "搜狐视频", "乐视",
                   "pptv", "土豆", "视频", "在线观看", "哔哩哔哩", "bilibili",
                   "腾讯", "搜狐"}


def title_usable(raw: str) -> str:
    """把页面标题变成「能拿去换源的片名」；无效时返回空串。"""
    t = resource_sites.clean_title(raw or "", keep_season=True)
    if len(t) < 2:
        return ""
    if t.replace(" ", "").lower() in {x.lower() for x in _GENERIC_TITLES}:
        return ""
    return t


def resolve_via_resource(url: str, meta: dict | None = None, *,
                         timeout: int = 12) -> dict:
    """拿页面标题去资源站换源，返回可直接下载的 m3u8/mp4。

    会返回**多条**可用地址（不同资源站/CDN）：实测同一个源在不同 CDN 上，
    ffmpeg 的证书链接受程度不一样，多备几条才能『这条不行换下一条』。
    """
    meta = meta or extract_meta(url, timeout=timeout)
    raw_title = meta.get("title") or ""
    if any(h in raw_title.lower() for h in _CLIP_HINTS):
        return {"ok": False,
                "error": f"标题像解说/预告类视频（{raw_title[:30]}…），不做换源以免下错片"}
    if not title_usable(raw_title):
        return {"ok": False,
                "error": f"页面没给出有效片名（标题是「{raw_title[:20]}」），无法换源"}
    title = resource_sites.clean_title(raw_title)
    if not title:
        return {"ok": False, "error": "拿不到视频标题，无法换源"}
    st = store.load()
    raw_sites = str(st.get("vip_resource_sites") or "").strip()
    sites = [x.strip() for x in re.split(r"[,;\s]+", raw_sites) if x.strip()] if raw_sites else None
    found = resource_sites.resolve(
        title, title=meta.get("title") or title,
        year=meta.get("year") or "",
        episode=meta.get("episode"),
        sites=sites, timeout=timeout,
    )
    if not found.get("ok"):
        return {"ok": False, "error": f"资源站换源失败：{found.get('error')}",
                "candidates": found.get("candidates") or []}

    options = found.get("options") or [found]
    streams: list[dict] = []
    seen: set[str] = set()
    for opt in options:
        stream = _play_page_stream(opt.get("url") or "", timeout=timeout)
        if not stream or stream in seen:
            continue
        seen.add(stream)
        streams.append({"url": stream, "referer": opt.get("referer") or "",
                        "site": opt.get("site_name") or found.get("site_name")})
    if not streams:
        return {"ok": False,
                "error": f"资源站《{found.get('vod_name')}》的播放地址解析不出 m3u8",
                "candidates": found.get("candidates") or []}
    return {"ok": True, "url": streams[0]["url"],
            "urls": streams, "referer": streams[0]["referer"],
            "backend": "resource-site", "site": found.get("site_name"),
            "vod_name": found.get("vod_name"), "episode": found.get("episode"),
            "candidates": found.get("candidates") or [], "duration": 0.0}


# ---------------------------------------------------------------- ③ 解析网关

def _gateways() -> list[str]:
    try:
        st = store.load()
    except Exception:  # noqa: BLE001
        return list(_GATEWAY_BUILTIN)
    raw = str(st.get("vip_parse_endpoints") or "").strip()
    items = [x.strip() for x in re.split(r"[,;\n]+", raw) if x.strip()] if raw else []
    return items or list(_GATEWAY_BUILTIN)


def resolve_via_gateway(url: str, *, timeout: int = 15) -> dict:
    """问用户配置的解析接口要真流。支持 `{url}` 占位；没有占位就拼在末尾。"""
    endpoints = _gateways()
    if not endpoints:
        return {"ok": False, "error": "未配置解析接口"}
    errors = []
    for tpl in endpoints:
        target = tpl.replace("{url}", quote(url, safe="")) if "{url}" in tpl \
            else tpl + quote(url, safe="")
        try:
            r = requests.get(target, headers={**HEADERS, "Referer": "https://www.google.com/"},
                             timeout=timeout)
        except requests.RequestException as e:
            errors.append(f"{host_of(target)}: {type(e).__name__}")
            continue
        body = r.text or ""
        iframe = re.search(r'<iframe[^>]+src=["\']([^"\']+)', body, re.I)
        if iframe and "javascript:" not in iframe.group(1).lower():
            body += " " + (_page_text(iframe.group(1), timeout=timeout) or "")
        m = re.search(r'https?:(?:\\/\\/|//)[^"\'\\\s<>]+?\.(?:m3u8|mp4)[^"\'\\\s<>]*', body, re.I)
        if m:
            stream = m.group(0).replace("\\/", "/")
            if stream.startswith("//"):
                stream = "https:" + stream
            return {"ok": True, "url": stream, "referer": "", "backend": "gateway",
                    "site": host_of(target), "duration": 0.0}
        errors.append(f"{host_of(target)}: 无流")
    return {"ok": False, "error": "解析接口全部失败：" + "；".join(errors[:4])}


def host_of(url: str) -> str:
    try:
        return urlparse(url).hostname or url
    except ValueError:
        return url


# ---------------------------------------------------------------- 下载

# 已知「证书/连接」坏掉的 CDN 主机 -> 记录时间。6 小时内不再优先尝试。
# 实测 hongniu 的 hn.bfvvs.com：requests 能下、ffmpeg（GnuTLS）证书校验失败，
# 同一个地址两条链路表现不一致，只能记住并绕开。
_BAD_HOSTS: dict[str, float] = {}
_BAD_HOST_TTL = 6 * 3600


def _mark_bad_host(host: str) -> None:
    if host:
        _BAD_HOSTS[host.lower()] = time.time()


def _is_bad_host(host: str) -> bool:
    ts = _BAD_HOSTS.get((host or "").lower())
    return bool(ts and time.time() - ts < _BAD_HOST_TTL)


def download_resolved(res: dict, output_dir: Path | str, *, title: str = "",
                      on_progress=None, task_id: str = "",
                      quality: str = "best",
                      expected_duration: float = 0.0,
                      cancel_event=None) -> dict:
    """把 resolve() 给出的流地址落盘。

    有多条源时逐条重试：某条 CDN 证书/防盗链不通过（实测 hongniu 的
    hn.bfvvs.com 就过不了 ffmpeg 的证书校验）就换下一条，而不是整单失败。

    expected_duration > 0 时会核对落盘文件时长：实测某些资源站 CDN 会在
    中途断流，ffmpeg 带 -reconnect 时把 EOF 当正常结束（rc=0），下回来的
    只有半集（774s / 标称 1494s）。这种「假成功」必须判失败并换源，
    否则用户以为下到了完整版。
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_dir = output_dir.resolve()
    name = title or res.get("vod_name") or res.get("site") or "video"

    streams = res.get("urls") or []
    if not streams:
        u = str(res.get("url") or "")
        streams = [{"url": u, "referer": res.get("referer") or "",
                    "site": res.get("site") or ""}] if u else []
    if not streams:
        return {"ok": False, "error": "没有可下载地址"}

    from .downloader import download_file
    from .stream_downloader import download_stream, probe_stream

    errors: list[str] = []
    for idx, item in enumerate(streams, 1):
        if cancel_event is not None and cancel_event.is_set():
            return {"ok": False, "error": "已取消"}
        url = str(item.get("url") or "")
        referer = str(item.get("referer") or "")
        if not url:
            continue
        print(f"[vip] 第 {idx}/{len(streams)} 条源: {item.get('site') or host_of(url)} "
              f"{url[:100]}", flush=True)
        if _is_bad_host(host_of(url)):
            errors.append(f"{host_of(url)}: 该 CDN 证书异常，已跳过")
            continue
        lower = url.lower().split("?")[0]
        is_stream = lower.endswith((".m3u8", ".mpd"))
        if not is_stream:
            # 没有扩展名：让探测说话（资源站 CDN 常把 m3u8 藏在无后缀地址后面）
            try:
                info = probe_stream(url, referer=referer)
                is_stream = info.kind in ("hls-master", "hls-media", "dash")
            except Exception:  # noqa: BLE001
                is_stream = False
        if is_stream:
            r = download_stream(
                url, output_dir, referer=referer, title=name,
                skip_existing=False, on_progress=on_progress,
                prefer_quality=quality if quality in ("highest", "lowest") else "",
                cancel_event=cancel_event,
            )
            if cancel_event is not None and cancel_event.is_set():
                try:
                    Path(getattr(r, "path", "") or "").unlink(missing_ok=True)
                except OSError:
                    pass
                return {"ok": False, "error": "已取消"}
            if getattr(r, "ok", False):
                actual = probe_duration(getattr(r, "path", "") or "")
                if expected_duration > 0 and actual > 0 and actual < expected_duration * 0.85:
                    # 假成功：文件不完整。删掉、换下一条。
                    # 注意这里**不**把主机记成坏源：断流是单条地址的行为，
                    # 同主机的另一条源仍可能完整（TLS 失败才是主机级问题）。
                    try:
                        Path(r.path).unlink(missing_ok=True)
                    except OSError:
                        pass
                    errors.append(f"{item.get('site') or host_of(url)}: "
                                  f"只下到 {actual:.0f}s（标称 {expected_duration:.0f}s），已换源")
                    continue
                return {"ok": True, "path": r.path, "size": r.size,
                        "backend": res.get("backend") or "stream",
                        "site": item.get("site") or "",
                        "duration": actual}
            err = str(getattr(r, "error", "") or "流下载失败")
            low = err.lower()
            if "certificate" in low or "tls" in low or "ssl" in low:
                _mark_bad_host(host_of(url))
            errors.append(f"{item.get('site') or host_of(url)}: {err}")
            continue
        r = download_file(url, output_dir, skip_existing=False,
                          referer=referer, title=name)
        if getattr(r, "ok", False):
            actual = probe_duration(getattr(r, "path", "") or "")
            if expected_duration > 0 and actual > 0 and actual < expected_duration * 0.85:
                try:
                    Path(r.path).unlink(missing_ok=True)
                except OSError:
                    pass
                errors.append(f"{item.get('site') or host_of(url)}: "
                              f"只下到 {actual:.0f}s（标称 {expected_duration:.0f}s），已换源")
                continue
            return {"ok": True, "path": r.path, "size": r.size,
                    "backend": res.get("backend") or "direct",
                    "site": item.get("site") or "", "duration": actual}
        errors.append(f"{item.get('site') or host_of(url)}: {getattr(r, 'error', '') or '直链下载失败'}")
    return {"ok": False, "error": "所有源都下载失败 → " + " ｜ ".join(errors[:4])}


# ---------------------------------------------------------------- 统一入口

def resolve(url: str, meta: dict | None = None, *, timeout: int = 15) -> dict:
    """按 ①会员 Cookie → ②资源站换源 → ③解析网关 顺序兜底。"""
    meta = meta or extract_meta(url, timeout=timeout)
    attempts: list[str] = []
    plat = platform_of(url)

    if plat == "bilibili":
        cookie = pick_bili_cookie()
        if cookie:
            play = bili_playurl(url, cookie)
            if play.get("ok"):
                return {"ok": True, "url": play["urls"][0], "extra_urls": play["urls"][1:],
                        "referer": "https://www.bilibili.com/", "backend": "bilibili-cookie",
                        "cookie": cookie, "quality": play.get("quality"),
                        "duration": play.get("duration") or 0.0}
            attempts.append(f"cookie: {play.get('error')}")
        else:
            attempts.append("cookie: 没有可用的会员 Cookie")

    res = resolve_via_resource(url, meta, timeout=timeout)
    if res.get("ok"):
        res["attempts"] = attempts
        return res
    attempts.append(f"resource: {res.get('error')}")

    # 海外免费源（YouTube）：国内资源站覆盖不到的记录片/冷门剧走这里。
    # 只有设置了代理、或本机本来就能直连 YouTube 时才值得尝试（失败很快）。
    ov = resolve_overseas(url, meta)
    if ov.get("ok"):
        ov["attempts"] = attempts
        return ov
    attempts.append(f"overseas: {ov.get('error')}")

    gw = resolve_via_gateway(url, timeout=timeout)
    if gw.get("ok"):
        gw["attempts"] = attempts
        return gw
    attempts.append(f"gateway: {gw.get('error')}")
    return {"ok": False, "error": "全部兜底通道失败 → " + " ｜ ".join(attempts),
            "attempts": attempts, "meta": meta}


def resolve_overseas(url: str, meta: dict | None = None, *, timeout: int = 25) -> dict:
    """海外免费源（YouTube）。只有开了开关才走；代理取设置里的 download_proxy。"""
    st = store.load()
    if not st.get("vip_overseas", True):
        return {"ok": False, "error": "海外源已关闭（设置 → 会员视频兜底）"}
    meta = meta or extract_meta(url, timeout=timeout)
    raw = str(meta.get("title") or "")
    if any(h in raw.lower() for h in _CLIP_HINTS):
        return {"ok": False,
                "error": f"标题像解说/预告类视频（{raw[:30]}…），不做换源以免下错片"}
    if not title_usable(raw):
        return {"ok": False, "error": f"页面没给出有效片名（标题是「{raw[:20]}」），无法搜索海外源"}
    proxy = str(st.get("download_proxy") or "").strip()
    try:
        from . import alt_sources
        return alt_sources.resolve(meta, proxy=proxy)
    except Exception as e:  # noqa: BLE001 —— 海外源只是兜底，失败不能拖垮整单
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def _download_overseas(res: dict, output_dir: Path | str, *, title: str = "",
                       task_id: str = "", on_progress=None,
                       expected: float = 0.0) -> dict:
    """用 yt-dlp 下海外源，并核对时长。"""
    from .video_downloader import download_video

    st = store.load()
    proxy = str(st.get("download_proxy") or "").strip()
    try:
        dl = download_video(
            str(res.get("url") or ""), output_dir, quality="best",
            title=title or res.get("vod_name") or "", skip_existing=False,
            include_playlist=False, on_progress=on_progress, proxy=proxy,
            task_id=(task_id or None),
        )
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    if not dl.get("ok"):
        return {"ok": False, "error": dl.get("error") or "海外源下载失败"}
    files = dl.get("files") or []
    path = str((files[0] or {}).get("path") or "")
    if not path:
        return {"ok": False, "error": "海外源下载完成但没找到文件"}
    actual = probe_duration(path)
    exp = expected or float(res.get("duration") or 0)
    if exp > 0 and actual > 0 and actual < exp * 0.85:
        try:
            Path(path).unlink(missing_ok=True)
        except OSError:
            pass
        return {"ok": False,
                "error": f"海外源只下到 {actual:.0f}s（标称 {exp:.0f}s），已丢弃"}
    return {"ok": True, "path": path, "size": int((files[0] or {}).get("size") or 0),
            "duration": actual, "backend": "youtube"}


def unlock(url: str, output_dir: Path | str, *, title: str = "",
           meta: dict | None = None, on_progress=None, task_id: str = "",
           quality: str = "best", cancel_event=None) -> dict:
    """解析 + 下载一步到位：给一个会员视频页面，拿回完整版文件。

    cancel_event 可以外部传进来：换源要连着试几个资源站，每个站都可能
    慢慢吞吞拉半小时，上层需要一个「预算到了就收手」的把手（见
    video_downloader._vip_replace_with_full）。
    """
    meta = meta or extract_meta(url)
    res = resolve(url, meta)
    if not res.get("ok"):
        return {"ok": False, "error": res.get("error") or "兜底解析失败", "meta": meta}
    expected = float(meta.get("duration") or 0) or float(res.get("duration") or 0)
    backend = res.get("backend") or ""
    # 文件名优先用页面标题：任务标题经常是「视频下载」这种默认值，
    # 落到磁盘上就分不清是哪一集了。
    name = str(meta.get("title") or title or res.get("vod_name") or "")
    if cancel_event is None and task_id:
        try:
            from .video_downloader import get_cancel_event
            cancel_event = get_cancel_event(task_id)
        except Exception:  # noqa: BLE001
            cancel_event = None

    if backend == "youtube":
        dl = _download_overseas(res, output_dir,
                                title=name, task_id=task_id,
                                on_progress=on_progress, expected=expected)
    elif backend == "bilibili-cookie":
        dl = download_bili(url, Path(output_dir), title=name,
                           cookie=res.get("cookie") or "", on_progress=on_progress)
        if dl.get("ok"):
            actual = probe_duration(dl.get("path") or "")
            if expected > 0 and actual > 0 and actual < expected * 0.85:
                try:
                    Path(dl["path"]).unlink(missing_ok=True)
                except OSError:
                    pass
                return {"ok": False,
                        "error": f"会员 Cookie 直取只拿到 {actual:.0f}s（标称 {expected:.0f}s），"
                                 "可能这个号没有该片权限", "meta": meta}
            dl["duration"] = actual
    else:
        dl = download_resolved(res, output_dir, title=name,
                               on_progress=on_progress, task_id=task_id,
                               quality=quality, expected_duration=expected,
                               cancel_event=cancel_event)
        # 资源站所有 CDN 都失败（证书/断流）时，最后再试一次海外源。
        if not dl.get("ok"):
            if cancel_event is not None and cancel_event.is_set():
                return {"ok": False, "error": "已取消", "meta": meta}
            ov = resolve_overseas(url, meta)
            if ov.get("ok"):
                ov_dl = _download_overseas(
                    ov, output_dir, title=name, task_id=task_id,
                    on_progress=on_progress, expected=expected)
                if ov_dl.get("ok"):
                    res, dl = ov, ov_dl

    if not dl.get("ok"):
        return {"ok": False, "error": dl.get("error") or "兜底下载失败", "meta": meta}
    dl["note"] = (f"会员内容已通过「{res.get('backend')}」换源下载完整版"
                  f"（{res.get('site') or res.get('vod_name') or '已解析'}）")
    dl["meta"] = meta
    return dl


def status(check: bool = False) -> dict:
    """给设置页/状态接口：三条兜底通道各自就绪情况。

    check=False 时**不联网**（只看目录里有几份 Cookie）—— /api/status 会被
    前端频繁轮询，每次都去请求 B 站 nav 接口会被风控。
    """
    d = vip_cookie_dir()
    files = sorted(p.name for p in d.glob("*.txt"))
    items = [{"file": f, "vip": None, "uname": "", "error": ""} for f in files]
    if check and files:
        try:
            items = [{"file": i["file"], "vip": i.get("vip"), "uname": i.get("uname"),
                      "error": i.get("error")} for i in list_vip_cookies()]
        except Exception:  # noqa: BLE001
            pass
    return {
        "auto_unlock": bool(store.load().get("vip_auto_unlock", True)),
        "cookie_dir": str(d),
        "cookie_files": files,
        "vip_cookies": sum(1 for i in items if i.get("vip")),
        "cookies": items,
        "resource_sites": len(resource_sites.SITES),
        "gateways": len(_gateways()),
        "overseas": bool(store.load().get("vip_overseas", True)),
        "download_proxy": str(store.load().get("download_proxy") or ""),
        "ffmpeg": bool(find_ffmpeg()),
    }
