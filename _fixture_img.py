# -*- coding: utf-8 -*-
"""图片抓取夹具：列表页 -> 6 个作品页 -> 每个 2 张图，共 12 张，用于端到端验收统计条。"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 18899
# 1x1 PNG（真实可解码，避免宽高探测直接判废）
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c6360000002000100ffff03000006000557bfabd4"
    "0000000049454e44ae426082"
)

LIST = ("<!doctype html><html><head><title>gallery</title></head><body>"
        + "".join(f'<a href="/p/{i}"><img src="/thumb/{i}.png" alt="item {i}">item {i}</a>'
                  for i in range(1, 7))
        + "</body></html>").encode()


def page(n: int) -> bytes:
    imgs = "".join(
        f'<img src="/img/{n}_{k}.png" width="800" height="600" alt="pic {n}-{k}">'
        for k in (1, 2)
    )
    return (f"<!doctype html><html><head><title>work {n}</title></head>"
            f"<body><h1>work {n}</h1>{imgs}</body></html>").encode()


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        p = self.path
        if p.startswith("/img/") or p.startswith("/thumb/"):
            body, ct = PNG, "image/png"
        elif p.startswith("/p/"):
            try:
                body, ct = page(int(p.split("/")[2].split(".")[0])), "text/html; charset=utf-8"
            except Exception:
                body, ct = b"not found", "text/plain"
        else:
            body, ct = LIST, "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
