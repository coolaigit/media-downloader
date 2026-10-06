# -*- coding: utf-8 -*-
"""Optional BrowserSkill bridge used only as anti-bot/login fallback.

Normal crawling remains HTTP/Playwright-first. When a rendered page is a
verification wall, this module can borrow the user's connected real browser
through Tencent BrowserSkill. It may click one obvious verification button;
if a real captcha/login step remains, BrowserSkill request-help hands control
to the user and returns after completion.

BrowserSkill is optional: normal operation keeps working when bsk is absent.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

_BSK = shutil.which("bsk") or shutil.which("bsk.exe")
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

_SIMPLE_VERIFY_LABELS = {
    "verify", "verify you are human", "continue", "continue verification",
    "confirm", "i am human", "i'm not a robot",
    "验证", "继续", "继续验证", "开始验证", "确认", "我是人类", "我不是机器人",
}
_REF_LINE = re.compile(r'(@e\d+)\s+(?:button|link)\s+"([^"]+)"', re.I)


def available() -> bool:
    return bool(_BSK)


def _run(args: list[str], *, timeout: int = 45, check: bool = True) -> subprocess.CompletedProcess:
    if not _BSK:
        raise RuntimeError("BrowserSkill (bsk) 未安装")
    cp = subprocess.run(
        [_BSK, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        creationflags=_CREATE_NO_WINDOW,
    )
    if check and cp.returncode != 0:
        msg = (cp.stderr or cp.stdout or f"bsk exit {cp.returncode}").strip()
        raise RuntimeError(msg[:600])
    return cp


def _json(args: list[str], *, timeout: int = 45) -> dict:
    cp = _run([*args, "--json"], timeout=timeout)
    raw = (cp.stdout or "").strip()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"BrowserSkill 返回了无效 JSON: {e}") from e


def start_page(url: str, *, name: str = "MediaDownloader 验证辅助") -> dict:
    sess = _json(["session", "start", "--name", name], timeout=30)
    sid = str(sess.get("session_id") or "")
    if not sid:
        raise RuntimeError("BrowserSkill 未返回 session_id")
    try:
        nav = _json(
            ["navigate", url, "--session", sid, "--wait-until", "domcontentloaded", "--timeout", "45s"],
            timeout=55,
        )
    except Exception:
        nav = {}
    return {
        "session_id": sid,
        "tab_id": nav.get("tab_id"),
        "url": str(nav.get("final_url") or nav.get("url") or url),
    }


def observe(session_id: str) -> str:
    data = _json(["observe", "--session", session_id, "--max-tokens", "5000"], timeout=30)
    return str(data.get("text") or "")


def evaluate(session_id: str, expression: str, *, timeout: int = 30):
    data = _json(
        ["evaluate", "--session", session_id, expression, "--timeout", f"{timeout}s"],
        timeout=timeout + 5,
    )
    if data.get("ok") is False:
        err = data.get("error") or {}
        raise RuntimeError(str(err.get("text") or err or "BrowserSkill evaluate failed"))
    return data.get("value")


def click_simple_verification(session_id: str) -> str:
    """Click one obvious verification/continue control and return its label."""
    text = observe(session_id)
    candidates: list[tuple[str, str]] = []
    for ref, label in _REF_LINE.findall(text):
        norm = re.sub(r"\s+", " ", label).strip().lower()
        if norm in _SIMPLE_VERIFY_LABELS:
            candidates.append((ref, label.strip()))
    if len(candidates) != 1:
        return ""
    ref, label = candidates[0]
    _json(["click", ref, "--session", session_id, "--timeout", "20s"], timeout=25)
    time.sleep(1.5)
    return label


def get_html(session_id: str, *, max_bytes: int = 8 * 1024 * 1024) -> str:
    fd, raw_path = tempfile.mkstemp(prefix="media_bsk_", suffix=".html")
    os.close(fd)
    path = Path(raw_path)
    try:
        _run(
            ["get-html", "--session", session_id, "--max-bytes", str(max_bytes), "--out", str(path)],
            timeout=45,
        )
        return path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
    finally:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def request_human_help(session_id: str, prompt: str, *, timeout_seconds: int = 180) -> bool:
    """Ask the user to complete an actual captcha/login step in the Agent Window."""
    timeout_seconds = max(30, min(int(timeout_seconds or 180), 300))
    cp = _run(
        [
            "request-help",
            "--session", session_id,
            "--title", "Media Downloader 需要完成网页验证",
            "--prompt", prompt,
            "--timeout", f"{timeout_seconds}s",
        ],
        timeout=timeout_seconds + 15,
        check=False,
    )
    return cp.returncode == 0


def stop(session_id: str) -> None:
    if not session_id or not _BSK:
        return
    try:
        _run(["session", "stop", session_id], timeout=20, check=False)
    except Exception:
        pass
