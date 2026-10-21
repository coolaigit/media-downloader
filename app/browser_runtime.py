# -*- coding: utf-8 -*-
"""浏览器内核解析：包内 Chromium 优先，系统 Chrome/Edge 兜底。

V2.5 的做法是「复用用户电脑上已装的 Chrome / Edge」，理由是启动快、不用下载内核。
问题在于：用户电脑上可能只有 360 / QQ 浏览器（都不是 Chromium 的内置路径），
或者干脆一个都没有 —— 那时所有需要 JS 渲染的站点直接不可用。

V3.0 改成随安装包带一份 Playwright 官方 Chromium，
**用户电脑一个浏览器都没装也能跑**。跨电脑行为也一致，
不会出现「我这能抓、他那不能抓」这种只有换机器才复现的问题。

三种情况：
1. 包内有 Chromium（正常安装版）→ 用 executable_path 直接指过去。
2. 包内没有（源码模式没下载 / 精简包）→ 用系统 Chrome，再退 Edge。
3. 都没有 → 返回空，由调用方给出**能照做**的提示，不要只抛一句英文异常。

⚠️ 为什么处处都要传 executable_path：
为了让安装包少 272 MB，我们没带 chromium_headless_shell（playwright 默认的
headless 内核）。少了它之后 `p.chromium.launch(headless=True)` 会直接报
"Executable doesn't exist"，所以只要包内有 Chromium，就必须显式给路径。
"""
from __future__ import annotations

import os
from pathlib import Path

from .config import BROWSER_CANDIDATES, BROWSERS_DIR

_KIND_BUNDLED = "bundled"
_KIND_SYSTEM = "system"
_KIND_NONE = "none"


def bundled_chromium() -> Path | None:
    """包内 Chromium 的 chrome.exe。找不到返回 None（不抛异常）。"""
    try:
        if not BROWSERS_DIR.is_dir():
            return None
        # 目录名带版本号（chromium-1234），所以按前缀找，不写死版本
        for entry in sorted(BROWSERS_DIR.iterdir()):
            if not entry.is_dir() or not entry.name.startswith("chromium-"):
                continue
            for rel in ("chrome-win64/chrome.exe", "chrome-win/chrome.exe"):
                exe = entry / rel
                if exe.exists():
                    return exe
    except OSError:
        return None
    return None


def system_browser() -> tuple[str, str]:
    """系统 Chrome / Edge，返回 (channel, exe路径)。都没有返回 ("", "")。"""
    for channel, paths in BROWSER_CANDIDATES.items():
        for p in paths:
            if os.path.exists(p):
                return channel, p
    return "", ""


def pick() -> dict:
    """决定用哪个内核。

    返回 {"kind", "channel", "exe", "label"}：
    - kind=bundled → channel 为空、exe 有值（必须传 executable_path）
    - kind=system  → channel 有值、exe 有值（只需要传 channel）
    - kind=none    → 都为空
    """
    exe = bundled_chromium()
    if exe is not None:
        return {"kind": _KIND_BUNDLED, "channel": "", "exe": str(exe),
                "label": f"内置 Chromium（{exe.parent.parent.name}）"}
    channel, sys_exe = system_browser()
    if channel:
        return {"kind": _KIND_SYSTEM, "channel": channel, "exe": sys_exe,
                "label": f"系统 {channel}"}
    return {"kind": _KIND_NONE, "channel": "", "exe": "", "label": "无可用内核"}


def launch_kwargs(target: dict | None = None) -> dict:
    """给 `chromium.launch()` / `launch_persistent_context()` 用的内核参数。

    只返回与内核选择有关的两个键（executable_path / channel），
    headless、args、locale 那些由调用方自己决定，避免这里越权改行为。
    """
    t = target or pick()
    if t["kind"] == _KIND_BUNDLED:
        return {"executable_path": t["exe"]}
    if t["kind"] == _KIND_SYSTEM:
        return {"channel": t["channel"]}
    return {}


def unavailable_message() -> str:
    """没有可用内核时给用户看的提示 —— 必须能照做，不能只丢英文异常。"""
    return (
        "没有可用的浏览器内核：安装包内应自带 Chromium，但没找到。\n"
        "请重新安装完整版；若在用源码运行，执行：\n"
        "    set PLAYWRIGHT_BROWSERS_PATH=%CD%\\browsers\n"
        "    python -m playwright install chromium\n"
        "（装好后删掉 browsers\\chromium_headless_shell-* 与 browsers\\ffmpeg-* 可省 270MB，"
        "程序会直接用完整内核跑 headless）"
    )


def status() -> dict:
    """给 /api/status 与设置页展示。"""
    target = pick()
    return {
        "kind": target["kind"],
        "label": target["label"],
        "exe": target["exe"],
        "bundled_dir": str(BROWSERS_DIR),
        "bundled_dir_exists": BROWSERS_DIR.is_dir(),
    }
