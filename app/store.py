# -*- coding: utf-8 -*-
"""设置持久化：下载目录、并发数、cookie、内网开关等。"""
from __future__ import annotations

import json
import threading
from pathlib import Path

from .config import DATA_ROOT

# 配置持久化到 DATA_ROOT（%LOCALAPPDATA%\MediaDownloader），不再写程序目录；
# 老位置的 config.json 由 config._migrate_from_resource 一次性搬过来
SETTINGS_FILE = DATA_ROOT / "config.json"

DEFAULTS = {
    "downloads_subdir_images": "images",
    "downloads_subdir_videos": "videos",
    "image_concurrency": 6,
    # 流媒体（m3u8/mpd）下载并发：task_manager._run_streams 每次任务启动时
    # 实时读取本设置 —— 改完立即生效，无需重启（2026-09-28 依用户反馈加并发）
    "stream_concurrency": 4,
    "skip_existing": True,
    "allow_private_network": False,   # 默认拒绝内网地址（SSRF 防护）
    "cookies_file": "",               # Netscape 格式 cookies.txt，用于需要登录的站点
    "cookies_from_browser": "",       # 如 chrome / edge / firefox
    # 登录态浏览器配置（持久化）：抖音/小红书主页的作品列表接口依赖浏览器自己生成的
    # 风控参数，我们让页面自己发请求、只拦响应 —— 前提是这个配置里已经登录过一次。
    "browser_user_data_dir": "",
    # 是否显示浏览器窗口：实测无头模式打开抖音主页只会得到空壳，必须有头。
    "browser_headed": False,
    "video_quality_default": "best",
    # 下载根目录：空 = 默认 %USERPROFILE%\Downloads\MediaDownloader。
    # ⚠️ 消费点是 app.config 的 DOWNLOADS_DIR（导入期常量），改了要重启才生效，
    # 端点层必须如实返回 restart_required，不能让用户以为立即生效。
    "downloads_dir": "",
    # 点击桌面窗口右上角 X 的行为：exit=直接退出；tray=隐藏到系统托盘。
    # 默认保持 1.x/RC1 的直接退出行为，避免升级后悄悄常驻后台。
    "close_behavior": "exit",
    # 首次使用向导：complete 只代表用户点过“完成”，version 用于识别
    # “完成的是不是当前版本的向导”。这样升级到新的首次使用流程时会再显示一次，
    # 但同一版本反复安装不会每次骚扰用户。
    "onboarding_complete": False,
    "onboarding_version": "",
}

_LOCK = threading.RLock()


def load() -> dict:
    """读取设置，缺失字段用默认值补齐。"""
    with _LOCK:
        data = dict(DEFAULTS)
        if SETTINGS_FILE.exists():
            try:
                raw = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    data.update({k: v for k, v in raw.items() if k in DEFAULTS})
            except (json.JSONDecodeError, OSError):
                pass
        return data


def save(patch: dict) -> dict:
    """增量更新设置并写盘。只接受已知键，忽略未知键。"""
    with _LOCK:
        data = load()
        changed = False
        for k, v in (patch or {}).items():
            if k in DEFAULTS and v is not None:
                if data.get(k) != v:
                    data[k] = v
                    changed = True
        if changed:
            try:
                SETTINGS_FILE.write_text(
                    json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
                )
            except OSError:
                pass
        return data
