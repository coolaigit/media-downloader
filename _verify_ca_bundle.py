# -*- coding: utf-8 -*-
"""F-20 端到端：CA 固化 + 抖音/小红书/快手/B站 的 HTTPS 链路。

用法：python _verify_ca_bundle.py http://127.0.0.1:18701
判定：health 里的 ca_bundle 来自持久目录；四站图片代理 200；
      yt-dlp 解析不出现任何证书类错误（站点侧其它错误不算 CA 回归）。
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:18701"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
CHECKS: list[tuple[str, bool, str]] = []

HOSTS = (
    "https://www.bilibili.com/favicon.ico",
    "https://www.douyin.com/favicon.ico",
    "https://www.xiaohongshu.com/favicon.ico",
    "https://www.kuaishou.com/favicon.ico",
)
CA_WORDS = ("ca certificate", "cacert", "certificate verify failed",
            "unable to get local issuer", "tls ca")


def ck(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, bool(ok), str(detail)[:220]))
    print(("  PASS " if ok else "  FAIL ") + name
          + ("" if ok or not detail else f"  << {str(detail)[:220]}"), flush=True)


def get(path: str, timeout: int = 30):
    with OPENER.open(BASE + path, timeout=timeout) as r:
        return r.status, r.read()


def post(path: str, body: dict, timeout: int = 90):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    with OPENER.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def main() -> int:
    print("[health: CA 固化状态]", flush=True)
    try:
        h = json.loads(get("/api/health")[1])
    except Exception as e:
        ck("health 可读", False, e)
        h = {}
    ca = h.get("ca_bundle") or {}
    print("   ca_bundle =", json.dumps(ca, ensure_ascii=False), flush=True)
    ck("health 带 ca_bundle 字段", bool(ca))
    ck("持久 CA 存在且非空", ca.get("exists") is True and (ca.get("size") or 0) > 1024, ca)
    ck("CA 路径落在 data_root/certs 下",
       "/certs/" in str(ca.get("path") or "").replace("\\", "/"), ca.get("path"))
    ck("CA 来源不是已失效的 _MEI 临时路径",
       "_MEI" not in str(ca.get("path") or ""), ca.get("path"))
    ck("source 有效（copy / persistent / 系统根证书 / 自定义）",
       str(ca.get("source") or "").startswith(("copy:", "persistent",
                                               "windows-root-store", "env:")), ca.get("source"))

    print("[四站 HTTPS 图片代理：requests 链路]", flush=True)
    for url in HOSTS:
        host = urllib.parse.urlparse(url).hostname
        q = "/proxy?" + urllib.parse.urlencode({"url": url})
        try:
            st, body = get(q, timeout=30)
            ck(f"{host} 图片代理 200", st == 200 and len(body) > 0, f"status={st} bytes={len(body)}")
        except urllib.error.HTTPError as e:
            ck(f"{host} 图片代理 200", False,
               f"HTTP {e.code} {(e.read(160) or b'').decode('utf-8', 'replace')}")
        except Exception as e:
            ck(f"{host} 图片代理 200", False, f"{type(e).__name__}: {e}")

    print("[yt-dlp 解析链路：不得出现证书错误]", flush=True)
    try:
        r = post("/api/video/probe",
                 {"url": "https://www.bilibili.com/video/BV1GJ411x7h7"})
        err = str(r.get("error") or "")
        low = err.lower()
        ck("解析响应里没有 CA / 证书类错误", not any(w in low for w in CA_WORDS), err[:200])
        print(f"   probe ok={r.get('ok')} title={str(r.get('title') or '')[:40]} "
              f"formats={len(r.get('formats') or [])} error={err[:120]}", flush=True)
    except Exception as e:
        low = str(e).lower()
        ck("解析响应里没有 CA / 证书类错误", not any(w in low for w in CA_WORDS), e)

    ok = sum(1 for _, c, _ in CHECKS if c)
    bad = len(CHECKS) - ok
    print(f"结果：{ok} 通过 / {bad} 失败", flush=True)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
