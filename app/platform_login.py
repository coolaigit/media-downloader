# -*- coding: utf-8 -*-
"""Per-platform login state for the self-contained desktop build."""
from __future__ import annotations

import json
import time
from pathlib import Path
from urllib.parse import urlparse

from .config import DATA_ROOT

PLATFORMS = {
    "ig": {"name": "Instagram", "login_url": "https://www.instagram.com/", "domains": ("instagram.com",)},
    "pin": {"name": "Pinterest", "login_url": "https://www.pinterest.com/", "domains": ("pinterest.com", "pinimg.com")},
    "dy": {"name": "抖音", "login_url": "https://www.douyin.com/", "domains": ("douyin.com", "iesdouyin.com")},
    "xhs": {"name": "小红书", "login_url": "https://www.xiaohongshu.com/", "domains": ("xiaohongshu.com", "xhslink.com")},
    "tiktok": {"name": "TikTok", "login_url": "https://www.tiktok.com/", "domains": ("tiktok.com",)},
    "ks": {"name": "快手", "login_url": "https://www.kuaishou.com/", "domains": ("kuaishou.com",)},
    "bili": {"name": "B站", "login_url": "https://www.bilibili.com/", "domains": ("bilibili.com", "b23.tv")},
    "wb": {"name": "微博", "login_url": "https://weibo.com/", "domains": ("weibo.com", "weibo.cn")},
    "zhihu": {"name": "知乎", "login_url": "https://www.zhihu.com/", "domains": ("zhihu.com",)},
    "yt": {"name": "YouTube", "login_url": "https://www.youtube.com/", "domains": ("youtube.com", "youtu.be", "google.com")},
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

    state = "not_logged_in"
    reason = ""
    if invalid_ts and invalid_ts >= login_ts:
        state = "invalid"
        reason = str(invalid.get("reason") or "登录态已失效")
    elif relevant:
        usable = [r for r in relevant if not r["expires"] or r["expires"] > now]
        if usable:
            state = "ready"
        else:
            state = "expired"
            reason = "登录 Cookie 已过期"
    elif login_ts and prof.exists():
        # 仅仅打开过登录窗口不能算登录成功；login-browser 即使没拿到任何
        # Cookie 也会写状态文件。没有当前平台可用 Cookie 时只能标记待确认。
        state = "unknown"
        reason = "已打开过登录窗口，但没有检测到该平台的有效登录 Cookie"
    elif prof.exists():
        try:
            has_data = any(prof.iterdir())
        except OSError:
            has_data = False
        if has_data:
            state = "unknown"
            reason = "检测到浏览器数据，但尚未确认登录成功"

    return {
        "platform": platform, "name": cfg["name"], "login_url": cfg["login_url"],
        "state": state, "logged_in": state == "ready",
        "relogin_required": state in {"invalid", "expired"}, "reason": reason,
        "profile_dir": str(prof), "profile_exists": prof.exists(),
        "cookie_file": str(ck) if ck.exists() else "", "cookie_count": len(relevant),
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
