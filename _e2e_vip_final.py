# -*- coding: utf-8 -*-
"""F-24 端到端：关掉「自动换源」后，试看片段仍必须被点名。

用法：_venv/Scripts/python.exe _e2e_vip_final.py http://127.0.0.1:18848
不联网代理（本机服务），会临时改 vip_auto_unlock，跑完恢复。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import time
import urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:18848").rstrip("/")
URL = (sys.argv[2] if len(sys.argv) > 2 else
       "https://www.bilibili.com/zh-Hans/bangumi/play/ep810642?from_spmid=666.25.episode.0")
SUBDIR = "videos/_e2e_final"

op = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def api(path: str, body=None, timeout: int = 90):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data,
        headers={"Content-Type": "application/json"} if data else {},
        method="POST" if data is not None else "GET")
    with op.open(req, timeout=timeout) as r:
        raw = r.read().decode("utf-8", "replace")
    try:
        return json.loads(raw)
    except ValueError:
        return {"_raw": raw[:400]}


def main() -> int:
    old = api("/api/settings")
    had = old.get("vip_auto_unlock", True)
    print("settings.vip_auto_unlock =", had, flush=True)
    api("/api/settings", {"vip_auto_unlock": False})
    print("已临时关掉自动换源:", api("/api/settings").get("vip_auto_unlock"), flush=True)

    try:
        api("/api/history/forget", {"url": URL})
        # 番剧的台账键是 yt:bilibilibangumi:<ep>，只按 URL 忘不掉（url: 键和 yt: 键
        # 是两条），不显式删掉就会被「跳过已存在」顶回来。
        import re as _re
        _ep = _re.search(r"ep(\d+)", URL)
        if _ep:
            for _k in ("yt:bilibilibangumi:" + _ep.group(1),
                       "yt:bilibili:" + _ep.group(1),
                       "url:bilibili.com/bangumi/play/ep" + _ep.group(1)):
                print("forget", _k, api("/api/history/forget", {"key": _k}).get("removed"),
                      flush=True)
        body = {"url": URL, "subdir": SUBDIR, "quality": "best", "container": "mp4",
                "prefer_audio_only": False, "write_subtitles": False,
                "subtitle_langs": "zh.*,en", "include_playlist": False,
                "rate_limit": "", "format_id": "", "title": "",
                # 台账里可能已有同一条（归一后是同一个 key），这里必须关掉跳过
                "skip_existing": False}
        r = api("/api/download/video", body)
        tid = r.get("task_id")
        print("submit:", r, flush=True)
        if not tid:
            return 1
        deadline = time.monotonic() + 300
        st = {}
        while time.monotonic() < deadline:
            time.sleep(4)
            st = api("/api/tasks/" + str(tid))
            print("  status=%-10s done=%s failed=%s err=%s"
                  % (st.get("status"), st.get("done"), st.get("failed"),
                     str(st.get("error") or "")[:120]), flush=True)
            if st.get("status") in ("completed", "done", "finished", "failed",
                                   "cancelled", "error"):
                break
        note = str(st.get("note") or "")
        print("\nnote:", note or "(空)", flush=True)
        files = st.get("files") or []
        for f in files:
            print("file:", f.get("name"), f.get("size"), flush=True)

        ok = True
        if not files:
            print("FAIL 没有产出文件", flush=True)
            ok = False
        if "⚠️" not in note or "不是完整正片" not in note:
            print("FAIL 关掉换源后没有给出试看提示（F-24 回退）", flush=True)
            ok = False
        name = (files[0].get("name") if files else "") or ""
        if "视频下载 [" in name:
            print("FAIL 文件名还是占位符:", name, flush=True)
            ok = False
        print("\nRESULT", "PASS" if ok else "FAIL", flush=True)
        return 0 if ok else 1
    finally:
        api("/api/settings", {"vip_auto_unlock": bool(had)})
        print("已恢复 vip_auto_unlock =", bool(had), flush=True)
        root = (api("/api/settings").get("downloads_dir") or "").strip()
        for cand in {root, os.path.join(os.getcwd(), "_dist_test",
                                        "MediaDownloader_TEST", "downloads")}:
            if not cand:
                continue
            p = os.path.join(cand, *SUBDIR.split("/"))
            if os.path.isdir(p):
                shutil.rmtree(p, ignore_errors=True)
                print("清理测试目录:", p, flush=True)


if __name__ == "__main__":
    sys.exit(main())
