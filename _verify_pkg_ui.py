# -*- coding: utf-8 -*-
"""校验新 EXE 的 PYZ 里带上了本次「进度 + 无窗口」修复的模块与函数。"""
from __future__ import annotations

import marshal
import sys
import tempfile
from pathlib import Path

from PyInstaller.archive.readers import CArchiveReader, ZlibArchiveReader

EXE = Path(sys.argv[1] if len(sys.argv) > 1 else
           r"E:\Project\media-downloader-main\_dist_ui\MediaDownloader_TEST.exe")

NEED = {
    "app.video_downloader": [
        "_download_ts_via_browser", "_index_ts_playlist", "_ts_plan",
        "_ts_integrity_error", "_http_fetch_ts", "_cleanup_parts",
        "run_external", "download_video",
        # V3.0.5：抖音改走渲染（yt-dlp 的 DouyinIE 要 a_bogus，已废）
        "_douyin_snapshot", "douyin_item_probe",
        # V3.0.6：知乎 /video/<id> 归一成 /zvideo/<id>；下载文件名用作品标题
        "_normalize_media_url",
    ],
    "app.extractor": ["_dedupe_key", "strip_thumb_suffix", "dedupe",
                      "enrich_with_originals"],
    "app.task_manager": ["apply_video_progress", "TaskManager"],
    "app.config": ["ensure_quiet_subprocess", "CA_BUNDLE", "ca_bundle"],
    # V3.0.8：frozen onefile 的 _MEI/certifi 被清理导致全平台 HTTPS 挂掉
    "app.ca_bundle": ["install", "refresh", "ca_path", "status",
                      "_windows_root_pem", "_atomic_write", "_publish"],
    "app.main": ["ca_bundle", "ca_path", "refresh"],
    "app.win_console": ["ensure_quiet_subprocess"],
    "app.profile_sources": ["_looks_like_work_url", "_wall_from_html",
                            "browser_items", "xhs_author_image_items",
                            # V3.0.5：418 / risk pass 识别
                            "parse_qs",
                            # V3.0.7：知乎真视频通道 + 视频形态闸门
                            "_zhihu_video_items", "_looks_like_video_work_url"],
    "app.batch_crawler": ["_xhs_note_meta"],
    "app.platform_login": ["login_window_alive", "mark_login_window", "_pid_alive"],
    "app.login_browser": ["_STEALTH_JS"],
}


def to_code(data):
    if hasattr(data, "co_names"):
        return data
    for raw in (data, data[16:] if isinstance(data, (bytes, bytearray)) else b""):
        try:
            return marshal.loads(raw)
        except Exception:  # noqa: BLE001
            continue
    return None


def names_of(code) -> set:
    out: set = set()
    if not code:
        return out
    out.update(code.co_names)
    for c in code.co_consts:
        if hasattr(c, "co_name"):
            out.add(c.co_name)
            out.update(c.co_names)          # 嵌套函数里引用的名字
            for d in c.co_consts:
                if hasattr(d, "co_name"):
                    out.add(d.co_name)
    return out


r = CArchiveReader(str(EXE))
pyz_name = next(n for n in r.toc if n.upper().endswith(".PYZ"))
with tempfile.TemporaryDirectory() as td:
    p = Path(td) / "PYZ-00.pyz"
    p.write_bytes(r.extract(pyz_name))
    z = ZlibArchiveReader(str(p))
    print("EXE", EXE.name, EXE.stat().st_size, "B", "PYZ 模块", len(z.toc))
    bad = []
    for mod, syms in NEED.items():
        if mod not in z.toc:
            bad.append(f"{mod}: 模块缺失")
            continue
        got = names_of(to_code(z.extract(mod)))
        miss = [s for s in syms if s not in got]
        print(f"  {mod}: {len(syms) - len(miss)}/{len(syms)}",
              "MISSING " + ",".join(miss) if miss else "OK")
        bad.extend(f"{mod}:{s}" for s in miss)
    if bad:
        print("FAIL", bad)
        sys.exit(1)
    print("PASS")
