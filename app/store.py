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
