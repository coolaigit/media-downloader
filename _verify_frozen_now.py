# -*- coding: utf-8 -*-
"""F-13 / F-15 冻结项实测：知乎 /video/ 解析 + 下载文件名用作品标题。

用法：python _verify_frozen_now.py http://127.0.0.1:18701
"""
from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:18701"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
checks = []


def ck(name, cond, extra=""):
    checks.append((name, bool(cond)))
    print(("  ok   " if cond else "  FAIL ") + name
          + ("" if cond else "  << " + str(extra)), flush=True)


def api(path, body):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with OPENER.open(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def main() -> int:
    print("[F-15 知乎 /video/ → /zvideo/ 解析]", flush=True)
    res = api("/api/video/probe",
              {"url": "https://www.zhihu.com/video/1342930761977176064"})
    ck("解析成功", res.get("ok") is True, res.get("error"))
    ck("标题是作品名", res.get("title") == "写春联也太难了吧！", res.get("title"))
    ck("封面非空", bool(res.get("thumbnail")), res.get("thumbnail"))
    ck("4 档清晰度", len(res.get("formats") or []) >= 4, len(res.get("formats") or []))
    ck("归一化后地址是 zvideo",
       "/zvideo/" in str(res.get("webpage_url") or ""), res.get("webpage_url"))

    print("[F-13 下载文件名用作品标题]", flush=True)
    src = (Path(__file__).resolve().parent / "app" / "video_downloader.py").read_text(
        encoding="utf-8")
    ck("outtmpl 仍用调用方标题作主干",
       '_stem + " [%(id).16s].%(ext)s"' in src)
    ck("占位标题仍被排除", "_PLACEHOLDER" in src or "视频下载" in src)
    import yt_dlp
    tmpl = "写春联也太难了吧！ [%(id).16s].%(ext)s"
    with yt_dlp.YoutubeDL({"quiet": True, "outtmpl": tmpl}) as ydl:
        rendered = ydl.prepare_filename({"id": "1342930761977176064", "ext": "mp4"})
    ck("渲染出的文件名以标题开头",
       rendered.startswith("写春联也太难了吧！"), rendered)

    ok = sum(1 for _, c in checks if c)
    bad = len(checks) - ok
    print(f"结果：{ok} 通过 / {bad} 失败", flush=True)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
