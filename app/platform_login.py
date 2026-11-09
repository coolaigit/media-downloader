# -*- coding: utf-8 -*-
"""Per-platform login state for the self-contained desktop build."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from urllib.parse import urlparse

import requests

from .config import DATA_ROOT

PLATFORMS = {
    # auth_cookies 只列“能证明账号已登录”的 Cookie。普通匿名 Cookie（csrf、
    # device id、locale 等）不能把状态误判成“已登录”。
    "ig": {"name": "Instagram", "login_url": "https://www.instagram.com/",
           "domains": ("instagram.com",), "auth_cookies": ("sessionid", "ds_user_id")},
    "pin": {"name": "Pinterest", "login_url": "https://www.pinterest.com/",
            "domains": ("pinterest.com", "pinimg.com"),
            "auth_cookies": ("_pinterest_sess", "__secure-s_a", "_auth")},
    "dy": {"name": "抖音", "login_url": "https://www.douyin.com/",
           "domains": ("douyin.com", "iesdouyin.com"),
           "auth_cookies": ("sessionid", "sessionid_ss", "sid_guard", "uid_tt")},
    "xhs": {"name": "小红书", "login_url": "https://www.xiaohongshu.com/",
            "domains": ("xiaohongshu.com", "xhslink.com"), "auth_cookies": ("web_session",)},
    "tiktok": {"name": "TikTok", "login_url": "https://www.tiktok.com/",
               "domains": ("tiktok.com",),
               "auth_cookies": ("sessionid", "sessionid_ss", "sid_tt", "sid_guard", "uid_tt")},
    "ks": {"name": "快手", "login_url": "https://www.kuaishou.com/",
           "domains": ("kuaishou.com",),
           # 2026-11-07 实测：快手已把 Web 会话 Cookie 从 kuaishou.server.web_st
           # 换成 kuaishou.server.webday7_st（7 天有效）+ 配套的 webday7_ph，
           # 直播子域另有 live.web_st / live.web_ph。旧名单只认 server.web_st，
           # 于是登录后的真实 profile 里 25 条 Cookie 一条都命中不了 ——
           # 用户看到的就是「明明登录了却显示未登录」。
           #
           # passToken / userId 单独出现不算：它们来自 id.kuaishou.com 的身份
           # 流程（扫码/第三方登录都会写），不能证明 www.kuaishou.com 建立了
           # 可用的 Web 会话。所以仍以 *web*st / *webday7*(st|ph) 为准。
           "auth_cookies": (
               "kuaishou.server.web_st", "kuaishou.server.webday7_st",
               "kuaishou.server.webday7_ph",
               "kuaishou.live.web_st", "kuaishou.live.web_ph",
           )},
    "bili": {"name": "B站", "login_url": "https://www.bilibili.com/",
             "domains": ("bilibili.com", "b23.tv"), "auth_cookies": ("sessdata", "dedeuserid")},
    "wb": {"name": "微博", "login_url": "https://weibo.com/",
           "domains": ("weibo.com", "weibo.cn"), "auth_cookies": ("sub",)},
    "zhihu": {"name": "知乎", "login_url": "https://www.zhihu.com/",
              "domains": ("zhihu.com",), "auth_cookies": ("z_c0",)},
    "yt": {"name": "YouTube", "login_url": "https://www.youtube.com/",
           "domains": ("youtube.com", "youtu.be", "google.com"),
           "auth_cookies": ("sapisid", "__secure-1papisid", "__secure-3papisid", "login_info")},
}
PROFILE_ROOT = DATA_ROOT / "platform_profiles"

# 有些平台仅凭 Cookie 名仍可能处在“账号风控/安全验证”中。Instagram
# 已实测会出现 sessionid + ds_user_id 都存在，但页面被强制导向
# update_risky_contactpoint，此时不能向用户显示“已登录”。
ACTIVE_VERIFY_PLATFORMS = {"ig"}


def platform_for_url(url: str) -> str:
    try:
        host = (urlparse(url or "").hostname or "").lower().removeprefix("www.")
    except ValueError:
        return ""
    for key, cfg in PLATFORMS.items():
        for dom in cfg["domains"]:
            if host == dom or host.endswith("." + dom):
                return key
    return ""


def platform_root(platform: str) -> Path:
    if platform not in PLATFORMS:
        raise ValueError(f"unknown platform: {platform}")
    return PROFILE_ROOT / platform


def profile_dir(platform: str) -> Path:
    return platform_root(platform) / "profile"


def cookie_file(platform: str) -> Path:
    return platform_root(platform) / "cookies_from_profile.txt"


def state_file(platform: str) -> Path:
    return platform_root(platform) / "login_state.json"


def invalid_file(platform: str) -> Path:
    return platform_root(platform) / "login_invalid.json"


def _read_json(path: Path) -> dict:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _cookie_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return []
    for raw in lines:
        line = raw.strip("\r\n")
        if not line:
            continue
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_"):]
        elif line.lstrip().startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        dom, _inc, cpath, secure, expires, name, value = parts[:7]
        if not name:
            continue
        try:
            exp = int(float(expires))
        except (TypeError, ValueError):
            exp = 0
        rows.append({
            "domain": dom.lstrip(".").lower(), "path": cpath or "/",
            "include_subdomains": str(_inc).upper() == "TRUE" or dom.startswith("."),
            "secure": str(secure).upper() == "TRUE", "expires": exp,
            "name": name, "value": value,
        })
    return rows


def _write_cookie_rows_atomic(path: Path, rows: list[dict]) -> int:
    """以 Netscape 格式原子写回 Cookie 行。"""
    lines = ["# Netscape HTTP Cookie File", "# MediaDownloader merged refresh", ""]
    n = 0
    for r in rows or []:
        dom = str(r.get("domain") or "").strip().lstrip(".").lower()
        name = str(r.get("name") or "")
        if not dom or not name:
            continue
        cpath = str(r.get("path") or "/")
        secure = "TRUE" if r.get("secure") else "FALSE"
        try:
            exp = int(r.get("expires") or 0)
        except (TypeError, ValueError):
            exp = 0
        include_subdomains = bool(r.get("include_subdomains"))
        out_dom = ("." + dom) if include_subdomains else dom
        lines.append("\t".join([
            out_dom, "TRUE" if include_subdomains else "FALSE",
            cpath, secure, str(exp), name, str(r.get("value") or "")
        ]))
        n += 1
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return 0
    return n


def _relevant(platform: str, rows: list[dict]) -> list[dict]:
    domains = PLATFORMS[platform]["domains"]
    return [r for r in rows
            if any(r["domain"] == d or r["domain"].endswith("." + d) for d in domains)]


def _auth_rows(platform: str, rows: list[dict], now: int | None = None) -> list[dict]:
    """Return usable rows that are strong evidence of an authenticated account."""
    if platform not in PLATFORMS:
        return []
    now = int(time.time()) if now is None else int(now)
    names = {str(x).lower() for x in PLATFORMS[platform].get("auth_cookies") or ()}
    return [
        r for r in rows
        if str(r.get("name") or "").lower() in names
        and (not int(r.get("expires") or 0) or int(r.get("expires") or 0) > now)
        and str(r.get("value") or "")
    ]


def _verify_platform_auth(platform: str, rows: list[dict]) -> dict:
    """Actively verify account usability for platforms where auth cookies alone are insufficient."""
    if platform not in ACTIVE_VERIFY_PLATFORMS:
        return {"state": "not_required", "reason": ""}

    auth = _auth_rows(platform, rows)
    if not auth:
        return {"state": "not_authenticated", "reason": "没有检测到账号登录凭据"}

    if platform == "ig":
        cookies = {str(r["name"]): str(r["value"]) for r in rows if r.get("value")}

        def _fallback(reason_no_session: str, detail: str = "") -> dict:
            """接口没能确认到可用会话时的统一出口。

            sessionid 是 Instagram 的会话主凭据：它在，就说明浏览器里是真的登录状态。
            此时接口返回 401 / require_login / challenge，多半是对数据中心 IP 的风控，
            不该让用户卡在「待确认」——实测用户点一百次重新登录都没用，因为凭据本来就是好的。
            反过来，连 sessionid 都没有，就确实是没登录，如实要求重新登录。
            """
            if str(cookies.get("sessionid") or "").strip():
                extra = f"（接口返回：{detail.strip()[:60]}）" if detail.strip() else ""
                return {
                    "state": "verified",
                    "reason": ("Instagram 未开放接口校验" + extra +
                               "，但会话凭据 sessionid 完整，按已登录处理；"
                               "若抓取时报未登录，再重新登录一次。"),
                }
            return {"state": "verification_required", "reason": reason_no_session}

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/154.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://www.instagram.com/",
            # Instagram Web currently uses this public web app id for account reads.
            "X-IG-App-ID": "936619743392459",
        }
        try:
            resp = requests.get(
                "https://www.instagram.com/api/v1/accounts/current_user/?edit=true",
                cookies=cookies,
                headers=headers,
                timeout=12,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            # 网络/代理波动也算「接口没能确认」，同样交给 _fallback：
            # 有 sessionid 就按已登录处理（代理经常抖，凭据是真的就够）。
            return _fallback("Instagram 在线验证失败，请检查网络或代理后重试。",
                             f"{type(exc).__name__}")

        if resp.status_code == 200:
            try:
                data = resp.json()
            except ValueError:
                data = {}
            user = data.get("user") if isinstance(data, dict) else None
            if isinstance(user, dict) and user:
                return {"state": "verified", "reason": ""}
            if isinstance(data, dict) and (
                data.get("pk") or data.get("username")
            ):
                return {"state": "verified", "reason": ""}
            msg = str(data.get("message") or "") if isinstance(data, dict) else ""
            if "challenge" in msg.lower() or "login" in msg.lower():
                return _fallback(
                    "Instagram 登录凭据已存在，但账号仍要求安全验证/登录确认", msg)
            return _fallback(
                "Instagram 返回了登录凭据，但未确认到可用账号会话", msg)

        if resp.status_code in {301, 302, 303, 307, 308}:
            loc = str(resp.headers.get("location") or "")
            return _fallback(
                "Instagram 登录凭据已存在，但账号仍要求安全验证/登录确认",
                f"重定向到 {loc[:60]}")
        if resp.status_code in {401, 403}:
            # 401 有两种完全不同的含义，必须分开，否则用户会永远卡在「待确认」：
            #   1) 凭据真失效（连 sessionid 都没有）→ 确实要重新登录
            #   2) Instagram 的接口风控：返回 {"message":"Please wait a few minutes...",
            #      "require_login":true}。实测 2026-10-22：用户网页里明确是登录状态、
            #      sessionid 完整（77 字符），current_user 接口照样 401 / 200+require_login。
            #      这种情况下继续判「待确认」，用户点一百次「重新登录」也没用。
            try:
                m401 = str((resp.json() or {}).get("message") or "")[:80]
            except Exception:  # noqa: BLE001
                m401 = (resp.text or "")[:80]
            return _fallback(
                "Instagram 登录凭据不完整（缺少 sessionid），请在登录窗口里真正完成一次登录。",
                m401)
        return {
            "state": "unknown",
            "reason": f"Instagram 在线验证返回 HTTP {resp.status_code}",
        }

    return {"state": "unknown", "reason": "登录状态尚未验证"}


# ---------- 「登录窗口是不是真的开着」 ----------
#
# 2026-11-07 实测教训：**不能再用 profile 的 lockfile 判断登录窗口**。
# 本程序自己会在后台拿同一个 profile 起 headless Chromium 去抓主页、解析直链、
# 刷新 Cookie（app/video_downloader._douyin_real_url_profiled、
# profile_sources.*_author_items、refresh_from_profile 都是）。这些后台进程
# 同样持有 lockfile，于是 /api/platforms/status 把「我自己在跑渲染」报成
# 「登录窗口已打开」—— 用户看到的就是状态不定时地乱跳。
#
# 现在改成登记式：打开登录窗口时把 pid 写进 <profile_root>/login_window.json，
# 只有这个进程还活着才算「窗口开着」。后台渲染不登记，就不会再冒充窗口。
_WINDOW_FILE = "login_window.json"


def _window_file(platform: str) -> Path:
    if platform not in PLATFORMS:
        raise ValueError(f"unknown platform: {platform}")
    return platform_root(platform) / _WINDOW_FILE


def _pid_alive(pid: int) -> bool:
    """进程是否还活着（Windows 用 GetExitCodeProcess，其它平台 os.kill 0）。

    ⚠️ Windows 上**不能**用 os.kill(pid, 0)：Python 会把它转成
    TerminateProcess，等于顺手把登录窗口杀了。
    """
    try:
        pid = int(pid or 0)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    try:
        import ctypes

        k32 = ctypes.windll.kernel32
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return False
        try:
            code = ctypes.c_ulong(0)
            if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
                return False
            return int(code.value) == STILL_ACTIVE
        finally:
            k32.CloseHandle(h)
    except Exception:  # noqa: BLE001
        return False


def mark_login_window(platform: str, pid: int) -> None:
    """登记「这个平台现在真有一个登录窗口开着」。"""
    if platform not in PLATFORMS:
        return
    try:
        p = _window_file(platform)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"pid": int(pid), "ts": int(time.time())}),
                     encoding="utf-8")
    except (OSError, ValueError):
        pass


def clear_login_window(platform: str) -> None:
    if platform not in PLATFORMS:
        return
    try:
        _window_file(platform).unlink(missing_ok=True)
    except OSError:
        pass


def login_window_alive(platform: str) -> bool:
    """登录窗口是否开着：只认自己登记过的那个 pid。

    服务重启 / 进程已退出时会顺手把登记文件删掉，避免状态永远卡在
    「登录窗口已打开」。
    """
    if platform not in PLATFORMS:
        return False
    p = _window_file(platform)
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not _pid_alive(int((raw or {}).get("pid") or 0)):
        clear_login_window(platform)
        return False
    return True


def status(platform: str) -> dict:
    cfg = PLATFORMS.get(platform)
    if not cfg:
        return {"platform": platform, "name": platform, "state": "unsupported",
                "logged_in": False, "relogin_required": False}
    prof = profile_dir(platform)
    ck = cookie_file(platform)
    login = _read_json(state_file(platform))
    invalid = _read_json(invalid_file(platform))
    relevant = _relevant(platform, _cookie_rows(ck))
    now = int(time.time())
    login_ts = int(login.get("ts") or 0)
    invalid_ts = int(invalid.get("ts") or 0)
    verification_state = str(login.get("verification_state") or "")
    verification_reason = str(login.get("verification_reason") or "")
    verification_ts = int(login.get("verification_ts") or 0)

    usable = [r for r in relevant if not r["expires"] or r["expires"] > now]
    auth = _auth_rows(platform, relevant, now)
    expired_auth = [
        r for r in relevant
        if str(r.get("name") or "").lower()
        in {str(x).lower() for x in cfg.get("auth_cookies") or ()}
        and int(r.get("expires") or 0) > 0
        and int(r.get("expires") or 0) <= now
    ]

    state = "not_logged_in"
    reason = ""
    # 失效标记按证据强度分有效期：页面跳转只是一次观测，不能把平台钉死。
    # 老版本写下的标记没有 code 字段，但「没有停在目标页」这句话能认出来。
    _code = str(invalid.get("code") or "")
    _why = str(invalid.get("reason") or "")
    if not _code and "没有停在目标页" in _why:
        _code = "redirect"
    _ttl = INVALID_TTL.get(_code or "auth", 0)
    _invalid_active = bool(invalid_ts and invalid_ts >= login_ts
                           and not (_ttl and (now - invalid_ts) > _ttl))
    if _invalid_active:
        state = "invalid"
        reason = _why or "登录态已失效"
    elif auth:
        if platform in ACTIVE_VERIFY_PLATFORMS:
            if verification_state == "verified":
                state = "ready"
            elif verification_state == "verification_required":
                state = "verification_required"
                reason = verification_reason or "账号仍需要完成安全验证/登录确认"
            elif verification_state == "unknown":
                state = "unknown"
                reason = verification_reason or "登录凭据已存在，但在线验证未完成"
            else:
                state = "unknown"
                reason = "检测到登录凭据，但尚未完成在线验证；点击“刷新状态”重新确认"
        else:
            state = "ready"
    elif expired_auth:
        state = "expired"
        reason = "账号登录凭据已过期"
    elif login_ts:
        # 打开过登录窗口，但没有账号级认证 Cookie：明确判定“未登录”，
        # 不再一直卡在“待确认”。匿名 Cookie 不算登录成功。
        state = "not_logged_in"
        if usable:
            reason = f"检测到 {len(usable)} 条站点 Cookie，但没有检测到账号登录凭据"
        else:
            reason = "没有检测到该平台的账号登录凭据"
    elif prof.exists():
        try:
            has_data = any(prof.iterdir())
        except OSError:
            has_data = False
        if has_data:
            state = "unknown"
            reason = "检测到旧浏览器数据，点击“刷新状态”可重新识别"

    return {
        "platform": platform, "name": cfg["name"], "login_url": cfg["login_url"],
        "state": state, "logged_in": state == "ready",
        "relogin_required": state in {"invalid", "expired", "verification_required"}, "reason": reason,
        "verification_state": verification_state,
        "verification_ts": verification_ts,
        "profile_dir": str(prof), "profile_exists": prof.exists(),
        # 登录窗口是否真的开着：只认自己登记过的 pid（见 login_window_alive）。
        # main.py 的 /api/platforms/status 直接用这个字段，不再去读 lockfile ——
        # 后台渲染任务也持有 lockfile，那样会把「我在抓数据」误报成「登录窗口已打开」。
        "window_open": login_window_alive(platform),
        "cookie_file": str(ck) if ck.exists() else "", "cookie_count": len(relevant),
        "auth_cookie_count": len(auth),
        "auth_cookie_names": sorted({str(r.get("name") or "") for r in auth}),
        "last_login_ts": login_ts, "invalid_ts": invalid_ts,
    }


def all_status() -> dict:
    return {key: status(key) for key in PLATFORMS}


def ready(platform: str) -> bool:
    return bool(status(platform).get("logged_in"))


def cookie_file_for_platform(platform: str, *, ready_only: bool = True) -> str:
    if platform not in PLATFORMS:
        return ""
    if ready_only and not ready(platform):
        return ""
    p = cookie_file(platform)
    return str(p) if p.exists() else ""


def cookie_file_for_url(url: str, *, ready_only: bool = True) -> str:
    key = platform_for_url(url)
    return cookie_file_for_platform(key, ready_only=ready_only) if key else ""


def profile_dir_for_url(url: str, *, ready_only: bool = True) -> str:
    key = platform_for_url(url)
    if not key:
        return ""
    if ready_only and not ready(key):
        return ""
    p = profile_dir(key)
    return str(p) if p.exists() else ""


def requests_cookie_dict(platform: str) -> dict:
    rows = _relevant(platform, _cookie_rows(cookie_file(platform))) if platform in PLATFORMS else []
    now = int(time.time())
    return {r["name"]: r["value"] for r in rows if not r["expires"] or r["expires"] > now}


def playwright_cookies(platform: str) -> list[dict]:
    if platform not in PLATFORMS:
        return []
    rows = _relevant(platform, _cookie_rows(cookie_file(platform)))
    now = int(time.time())
    out = []
    for r in rows:
        if r["expires"] and r["expires"] <= now:
            continue
        item = {"name": r["name"], "value": r["value"],
                "domain": "." + r["domain"], "path": r["path"] or "/"}
        if r["secure"]:
            item["secure"] = True
        out.append(item)
    return out


# 失效标记的有效期（秒）。0 / 缺省 = 一直有效，直到用户重新登录刷新 login_state。
# redirect 只保留 15 分钟：它来自「这一页跳走了」的单次观测，页面 A/B、语言前缀、
# 地区风控都会触发，不该让平台在登录中心里长期显示「已失效」。
INVALID_TTL = {"redirect": 900}


def mark_invalid(platform: str, reason: str, code: str = "auth") -> None:
    """记下「这个平台登录态失效」。

    code 区分证据强弱，决定这条标记能活多久（见 INVALID_TTL）：
      - auth      鉴权接口明确拒绝 → 一直有效，直到用户重新登录
      - redirect  单次页面跳转观测   → 只活 15 分钟，避免一次风控把平台钉死
    """
    if platform not in PLATFORMS:
        return
    p = invalid_file(platform)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({
            "ts": int(time.time()), "code": code or "auth",
            "reason": (reason or "登录态已失效")[:500],
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def clear_invalid(platform: str) -> None:
    if platform not in PLATFORMS:
        return
    try:
        invalid_file(platform).unlink(missing_ok=True)
    except OSError:
        pass


def looks_like_auth_error(message: str) -> bool:
    """这段话能不能当成「登录态失效」的证据。

    先排除我们自己写的「页面被跳转到…」观测：那段提示里带「请到平台登录中心
    登录」这几个字，一旦拿它反过来做 auth 判定，就会自证 —— 页面因为任何原因
    跳走（语言前缀、A/B、地区风控）都会把平台永久标成失效，用户重新登录也
    解除不了（2026-11-08 实测：B站番剧页路径大小写误判后写出 invalid 标记）。
    """
    if "没有停在目标页" in (message or ""):
        return False
    low = (message or "").lower()
    terms = (
        "login required", "login_required", "sign in", "not logged", "authentication",
        "unauthorized", "forbidden", "session expired", "expired session",
        "登录", "未登录", "登录态", "会话失效",
    )
    return any(t in low for t in terms)


def refresh_from_profile(platform: str) -> dict:
    """Actively re-export a closed platform profile and recompute login state.

    This is used by the UI's explicit Refresh button and once after a login
    window closes. Merely reading status cannot discover cookies that were
    persisted by Chromium but not exported before the window was closed.
    """
    if platform not in PLATFORMS:
        return status(platform)
    prof = profile_dir(platform)
    if not prof.is_dir():
        return status(platform)

    # Import locally to avoid a module cycle at import time.
    from .login_browser import export_from_profile

    # 刷新前先把当前主 Cookie 读进内存。后台浏览器有时只能拿到新的匿名/
    # 设备 Cookie，却拿不到 sessionid 等认证 Cookie；这种情况下不能让刷新
    # 把原有登录态直接覆盖掉，而应当“新鲜设备 Cookie + 旧认证 Cookie”合并。
    old_rows = _cookie_rows(cookie_file(platform))
    old_auth = _auth_rows(platform, _relevant(platform, old_rows))

    result = export_from_profile(
        str(prof),
        headless=True,
        warm_url=str(PLATFORMS[platform].get("login_url") or ""),
    )
    now = int(time.time())

    new_rows = _cookie_rows(cookie_file(platform))
    new_auth = _auth_rows(platform, _relevant(platform, new_rows), now)
    if old_auth and not new_auth and new_rows:
        merged: dict[tuple[str, str, str], dict] = {}
        for row in old_rows:
            key = (str(row.get("domain") or ""), str(row.get("path") or "/"),
                   str(row.get("name") or ""))
            merged[key] = row
        for row in new_rows:
            key = (str(row.get("domain") or ""), str(row.get("path") or "/"),
                   str(row.get("name") or ""))
            merged[key] = row
        n_merged = _write_cookie_rows_atomic(cookie_file(platform), list(merged.values()))
        if n_merged:
            result["count"] = n_merged
            result["path"] = str(cookie_file(platform))
            result["preserved_auth"] = True
    root = platform_root(platform)
    root.mkdir(parents=True, exist_ok=True)

    # A successful profile open is itself a completed refresh attempt, even
    # when it contains no cookies. Persist that fact so the UI can say
    # "not logged in" instead of remaining indefinitely "unknown".
    if result.get("ok") or "没有任何 cookie" in str(result.get("error") or ""):
        if not int(result.get("count") or 0):
            try:
                cookie_file(platform).unlink(missing_ok=True)
            except OSError:
                pass

        rows = _relevant(platform, _cookie_rows(cookie_file(platform)))
        auth = _auth_rows(platform, rows, now)
        verification = (
            _verify_platform_auth(platform, rows)
            if auth else
            {"state": "not_authenticated", "reason": ""}
        )

        try:
            state_file(platform).write_text(json.dumps({
                "ts": now,
                "profile_dir": str(prof),
                "url": PLATFORMS[platform]["login_url"],
                "domains": result.get("domains") or {},
                "note": "manual-refresh",
                "cookie_file": result.get("path") or "",
                "cookie_count": int(result.get("count") or 0),
                "verification_state": verification.get("state") or "",
                "verification_reason": verification.get("reason") or "",
                "verification_ts": now if platform in ACTIVE_VERIFY_PLATFORMS and auth else 0,
            }, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass

        if auth and verification.get("state") in {"verified", "not_required"}:
            clear_invalid(platform)

    st = status(platform)
    st["refresh_ok"] = bool(result.get("ok"))
    st["refresh_error"] = str(result.get("error") or "")
    return st
