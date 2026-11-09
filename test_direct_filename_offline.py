# -*- coding: utf-8 -*-
"""直链下载命名：作品标题必须落到磁盘上，不能落成 CDN 签名串。

背景（2026-11-08 Sam 反馈「xiaohongshu 和 kuaishou 的名称没搞定」）：
  快手主页作品是带签名的直链，末段形如
    BMjAyNjEwMjQwMDIzNDBfNDM0MzEzMjM2NF8yMTMyMTg4NDY3OTZfMV8z_b_B5f9b49955
  downloader.filename_from_url 的「占位名」判据只认纯字母数字 18+ 位，
  带下划线和日期的签名串不在里面，于是整条被当文件名写盘。
  修法：抓取结果有标题时，前端在提交 /api/download/direct 时显式给 filename；
  小红书下钻再用笔记页 og:title 把列表接口缺的标题补上。

  本用例纯本地：fixture HTTP 服务 + 真实 download_file + 真实前端 JS 函数。
"""
from __future__ import annotations

import http.server
import json
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import batch_crawler as bc
from app import profile_sources as ps
from app.downloader import download_file

checks: list[tuple[str, bool, str]] = []


def ck(name: str, cond: bool, extra: object = "") -> None:
    checks.append((name, bool(cond), str(extra)))
    print(("  ok   " if cond else "  FAIL ") + name
          + ("" if cond else "  << " + str(extra)), flush=True)


SIGN = "BMjAyNjEwMjQwMDIzNDBfNDM0MzEzMjM2NF8yMTMyMTg4NDY3OTZfMV8z_b_B5f9b49955"
TITLE = "对你而言 我是一颗星吗 #侯王子"


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = b"\x00\x00\x00\x18ftypmp42" + b"x" * 256
        self.send_response(200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        return


def main() -> int:
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    url = f"{base}/upic/2026/10/24/{SIGN}"
    try:
        print("[真实下载：修复前后对比]", flush=True)
        with tempfile.TemporaryDirectory() as d1, tempfile.TemporaryDirectory() as d2:
            r1 = download_file(url, d1, multipart=False, skip_existing=False)
            f1 = Path(r1.path).name if r1.ok and r1.path else ""
            ck("不带 filename 时确实落成签名串（复现问题）", SIGN in f1, f1 or r1.error)
            r2 = download_file(url, d2, title=TITLE,
                               filename=f"{TITLE}.mp4",
                               multipart=False, skip_existing=False)
            f2 = Path(r2.path).name if r2.ok and r2.path else ""
            ck("带 filename 时按作品标题落盘", f2.startswith("对你而言 我是一颗星吗"), f2 or r2.error)
            ck("扩展名保留 .mp4", f2.endswith(".mp4"), f2)

        print("[前端：提交直链下载时带 filename]", flush=True)
        js = Path(__file__).resolve().parent / "web" / "app.js"
        src = js.read_text(encoding="utf-8")
        m = re.search(r"function crawlVideoFilename\(it\) \{.*?\n\}", src, re.S)
        ck("app.js 里有 crawlVideoFilename", bool(m))
        ck("直链批次传了 filename",
           "filename: crawlVideoFilename(i)" in src)
        node = shutil.which("node")
        if node and m:
            payload = (m.group(0)
                       + "\nconsole.log(JSON.stringify(["
                       + "crawlVideoFilename({type:'video',title:"
                       + json.dumps(TITLE, ensure_ascii=False)
                       + ",url:" + json.dumps(url) + "}),"
                       + "crawlVideoFilename({type:'image',title:'图',url:'https://x/a.jpg'}),"
                       + "crawlVideoFilename({type:'video',title:'',url:'https://x/a.mp4'})"
                       + "]));")
            out = subprocess.run([node, "-e", payload], capture_output=True,
                                 text=True, timeout=20, check=False)
            vals = json.loads(out.stdout.strip()) if out.returncode == 0 else []
            ck("视频条目返回 标题.mp4",
               bool(vals) and vals[0] == f"{TITLE}.mp4", vals or out.stderr[:200])
            ck("图片条目不改名", bool(vals) and vals[1] == "", vals)
            ck("无标题视频不硬造名字", bool(vals) and vals[2] == "", vals)
        else:
            ck("node 可用（前端函数实跑）", False, "node 不在 PATH")

        print("[小红书下钻：列表没标题时用笔记页 og:title]", flush=True)
        html = ("<html><head>"
                '<meta property="og:title" content="今天也要好好吃饭 #美食 - 小红书">'
                '<meta property="og:image" content="https://sns-webpic-qc.xhscdn.com/cover.jpg">'
                "</head><body>笔记正文</body></html>")
        t, cover = bc._xhs_note_meta(html)
        ck("取到笔记标题", t.startswith("今天也要好好吃饭"), t)
        ck("去掉站点后缀「小红书」", "小红书" not in t, t)
        ck("取到 og:image 封面", cover.startswith("https://sns-webpic-qc.xhscdn.com/"), cover)
        html2 = ('<html><head><meta property="og:title" content="">'
                 '</head><script>window.__INITIAL_STATE__={"displayTitle":"周末 vlog"};'
                 '</script></html>')
        t2, _ = bc._xhs_note_meta(html2)
        ck("og:title 为空时从内嵌 JSON 兜底", t2 == "周末 vlog", t2)
        ck("空 HTML 不炸", bc._xhs_note_meta("") == ("", ""))

        print("[小红书发现页：直链条目不进下钻也不能崩]", flush=True)
        row = {"direct_url": "https://sns-video-hw.xhscdn.com/stream/110/259/abc.mp4",
               "page_url": "https://www.xiaohongshu.com/explore/abc",
               "title": "周末 vlog", "cover": "https://sns-webpic-qc.xhscdn.com/a.jpg",
               "kind_hint": "video", "site": "xiaohongshu.com"}
        with mock.patch.object(ps, "list_author_items", return_value=([row], "ok")):
            exp, _ = bc._expand_via_platform_source(
                "https://www.xiaohongshu.com/explore",
                limit=3, want_kinds=("video", "stream"))
        ck("发现页返回结果而不是抛异常", exp is not None and len(exp["items"]) == 1,
           type(exp).__name__ if exp is not None else None)
        ck("直链条目标题保留", bool(exp) and exp["items"][0]["title"] == "周末 vlog")
    finally:
        srv.shutdown()
        srv.server_close()

    ok = sum(1 for _, c, _ in checks if c)
    bad = len(checks) - ok
    print(f"结果：{ok} 通过 / {bad} 失败", flush=True)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
