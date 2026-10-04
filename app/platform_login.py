# -*- coding: utf-8 -*-
"""Per-platform login state for the self-contained desktop build."""
from __future__ import annotations

import json
import time
from pathlib import Path
from urllib.parse import urlparse

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
           "auth_cookies": ("kuaishou.server.web_st", "userid")},
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
            "secure": str(secure).upper() == "TRUE", "expires": exp,
            "name": name, "value": value,
        })
    return rows


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
    if invalid_ts and invalid_ts >= login_ts:
        state = "invalid"
        reason = str(invalid.get("reason") or "登录态已失效")
    elif auth:
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
        "relogin_required": state in {"invalid", "expired"}, "reason": reason,
        "profile_dir": str(prof), "profile_exists": prof.exists(),
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


def mark_invalid(platform: str, reason: str) -> None:
    if platform not in PLATFORMS:
        return
    p = invalid_file(platform)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({
            "ts": int(time.time()), "reason": (reason or "登录态已失效")[:500],
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

    result = export_from_profile(str(prof), headless=True)
    now = int(time.time())
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
        try:
            state_file(platform).write_text(json.dumps({
                "ts": now,
                "profile_dir": str(prof),
                "url": PLATFORMS[platform]["login_url"],
                "domains": result.get("domains") or {},
                "note": "manual-refresh",
                "cookie_file": result.get("path") or "",
                "cookie_count": int(result.get("count") or 0),
            }, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass

    rows = _relevant(platform, _cookie_rows(cookie_file(platform)))
    if _auth_rows(platform, rows, now):
        clear_invalid(platform)

    st = status(platform)
    st["refresh_ok"] = bool(result.get("ok"))
    st["refresh_error"] = str(result.get("error") or "")
    return st
