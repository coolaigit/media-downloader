# -*- coding: utf-8 -*-
"""端到端：快手主页抓取 → 点下载 → 落盘文件名是作品标题，不是 CDN 签名串。

用法：python _verify_direct_name.py http://127.0.0.1:18701
"""
from __future__ import annotations

import json
import re
import sys
import time
import urllib.request
from pathlib import Path

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:18701"
# 默认快手主页；传第二个参数可换成小红书发现页等其它真实来源。
KS = (sys.argv[2] if len(sys.argv) > 2 else
      "https://www.kuaishou.com/profile/3xnmi2xh2pxzpag")
SUB = "crawl/_reg_name_test"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def api(path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data,
        headers={"Content-Type": "application/json"} if data else {},
        method="POST" if data else "GET")
    with OPENER.open(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def crawl(url: str) -> list[dict]:
    jid = api("/api/crawl/start", {
        "url": url, "kinds": "video,stream", "originals_only": False,
        "allow_render": True, "mode": "list", "max_pages": 1,
        "max_items": 3, "drill_down": True, "drill_mode": "auto",
    })["job_id"]
    deadline = time.monotonic() + 180
    st = {}
    while time.monotonic() < deadline:
        st = api(f"/api/crawl/status/{jid}?full=true")
        if st.get("status") in ("completed", "failed", "cancelled"):
            break
        time.sleep(2)
    print("crawl:", st.get("status"), "items=", len(st.get("items") or []),
          flush=True)
    return st.get("items") or []


def main() -> int:
    items = crawl(KS)
    if not items:
        print("FAIL 这个来源一条作品都没抓到", flush=True)
        return 1
    a = items[0]
    # 只抓到 1 条（小红书发现页常被频控限到 1 条）时跳过对照组，
    # 对照行为由 test_direct_filename_offline.py 的本地 fixture 覆盖。
    b = items[1] if len(items) > 1 else None
    print("A title:", (a.get("title") or "")[:60], flush=True)
    print("A url  :", (a.get("url") or "")[:100], flush=True)
    # 前端 crawlVideoFilename 的等价写法（该函数另有 node 离线用例实跑）。
    def filename(it):
        t = re.sub(r"\s+", " ", str(it.get("title") or "")).strip()
        if not t:
            return ""
        m = re.search(r"\.([a-z0-9]{2,5})$", str(it.get("url") or ""), re.I)
        ext = m.group(1).lower() if m and not re.match(
            r"^(jpg|jpeg|png|gif|webp|avif|bmp|svg|ico|html?|json)$", m.group(1), re.I) else "mp4"
        return t[:80] + "." + ext

    dl_items = [
        {"url": a["url"], "title": a.get("title") or "",
         "filename": filename(a), "referer": a.get("referer") or "",
         "origin_page": a.get("origin_page") or ""},
    ]
    if b is not None:
        # 对照组：不传 filename，复现「随机字符文件名」
        dl_items.append({"url": b["url"], "title": b.get("title") or "",
                         "referer": b.get("referer") or "",
                         "origin_page": b.get("origin_page") or ""})
    body = {
        "items": dl_items,
        "subdir": SUB, "concurrency": 1, "skip_existing": False,
        "prefer_quality": "highest", "title": "文件名回归测试",
    }
    tid = api("/api/download/direct", body)["task_id"]
    deadline = time.monotonic() + 180
    task = {}
    while time.monotonic() < deadline:
        task = api(f"/api/tasks/{tid}")
        if task.get("status") in ("completed", "done", "finished", "failed", "cancelled"):
            break
        time.sleep(2)
    print("task:", task.get("status"), task.get("title"), flush=True)
    print("task detail:", json.dumps(task, ensure_ascii=False)[:900], flush=True)

    status = api("/api/status")
    dl = Path(str(status.get("downloads_dir") or "")) / SUB
    files = sorted(p for p in dl.glob("*") if p.is_file()) if dl.exists() else []
    print("output dir:", dl, flush=True)
    for p in files:
        print("  file:", p.name, p.stat().st_size, "B", flush=True)
    names = [p.name for p in files]
    title_head = re.sub(r"\s+", " ", (a.get("title") or ""))[:12]
    ok = any(title_head and n.startswith(title_head) for n in names)
    # 对照组仍按 URL 名落盘：快手 BMjAy..._b_xxx.mp4 / 小红书 01eaee....mp4
    random_ok = (b is None or any(
        re.match(r"^[A-Za-z0-9_]{24,}\.mp4$", n) for n in names))
    print("RESULT", "PASS" if ok and random_ok else "FAIL",
          "| titled=", ok, "| control-random=", random_ok, flush=True)
    return 0 if ok and random_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
