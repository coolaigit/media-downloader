# -*- coding: utf-8 -*-
"""从「用户正在使用的 Chrome」直接导入登录态（CDP 方式）。

为什么需要它（2026-09-27 用户反馈）：
  用户在自己的 Chrome 里登录了小红书 / Instagram / 抖音，希望工具直接复用。
  但 Chrome 127+ 把 cookie 用「应用绑定加密」锁死（实测 v20 前缀），运行中连
  cookie 库都复制不了（Permission denied），yt-dlp / gallery-dl / 我们都读不出来。

  CDP（Chrome DevTools Protocol）是唯一可行的路：**让 Chrome 自己解密**。
  做法：用调试端口启动 Chrome（附带启动器），我们连上去读 cookie。
  代价：Chrome 要用启动器重启一次；之后登录态照常使用，导出长期有效。

用法：
  1) 双击项目根目录的「启动Chrome-可导入登录态.bat」（会先退出 Chrome 再带调试端口重启）
  2) 设置页点「从正在运行的 Chrome 导入登录态」
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from .login_browser import _export_netscape

CDP_PORT = 9222


def import_from_cdp(port: int = CDP_PORT, out_file: str | None = None) -> dict:
    """连上带调试端口的 Chrome，把它的全部登录 cookie 导出成 cookies.txt。"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {"ok": False, "count": 0, "path": "", "domains": {},
                "error": "本机没有 playwright，无法连接 Chrome"}

    if out_file is None:
        from .config import DATA_ROOT  # 局部导入避免环

        out_file = str(DATA_ROOT / "data" / "cookies_chrome_cdp.txt")

    try:
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}", timeout=8000)
            ctx = browser.contexts[0] if browser.contexts else None
            if ctx is None:
                return {"ok": False, "count": 0, "path": "", "domains": {},
                        "error": "Chrome 已连上但没有打开任何上下文"}
            cookies = ctx.cookies()
            browser.close()
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        if "ECONNREFUSED" in msg or "connect" in msg.lower():
            return {"ok": False, "count": 0, "path": "", "domains": {},
                    "error": "连不上 Chrome（127.0.0.1:%d）。请先用项目根目录的"
                             "「启动Chrome-可导入登录态.bat」打开 Chrome（普通方式打开的 Chrome "
                             "不允许读取）。" % port}
        return {"ok": False, "count": 0, "path": "", "domains": {},
                "error": f"{type(e).__name__}: {msg[:160]}"}

    domains: dict = {}
    for c in cookies:
        d = (c.get("domain") or "").lstrip(".")
        if d:
            domains[d] = domains.get(d, 0) + 1
    dst = Path(out_file)
    n = _export_netscape(cookies, dst)
    if not n:
        return {"ok": False, "count": 0, "path": str(dst), "domains": domains,
                "error": "Chrome 里没有读到任何 cookie"}
    # 把设置里的 cookies_file 指向这份导出（下游 gallery-dl / 签名接口都用它）
    try:
        from . import store
        st = store.load()
        if not str(st.get("cookies_file") or "").strip():
            store.save({"cookies_file": str(dst)})
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True, "count": n, "path": str(dst), "domains": domains, "error": ""}


def write_launcher_bat(chrome_exe: str | None = None, dst: str | None = None) -> str:
    """生成一键启动器：退出 Chrome → 带调试端口重启（恢复上次会话）。"""
    import os
    if not chrome_exe:
        for cand in (r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                     r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"):
            if os.path.exists(cand):
                chrome_exe = cand
                break
    if not chrome_exe:
        return ""
    if not dst:
        from .config import DATA_ROOT  # 局部导入避免环

        dst = str(DATA_ROOT / "启动Chrome-可导入登录态.bat")
    content = "\r\n".join([
        "@echo off",
        "chcp 65001 >nul",
        "echo 正在关闭 Chrome（页面会自动恢复）...",
        'taskkill /f /im chrome.exe >nul 2>&1',
        "timeout /t 2 /nobreak >nul",
        f'start "" "{chrome_exe}" --remote-debugging-port={CDP_PORT} --restore-last-session',
        "echo Chrome 已带「可导入登录态」模式启动。回到工具设置页点「从正在运行的 Chrome 导入登录态」。",
        "pause",
    ]) + "\r\n"
    Path(dst).write_text(content, encoding="utf-8-sig")
    return dst
