# -*- coding: utf-8 -*-
"""F-20 加强验收：_MEI/certifi 与持久副本都被删掉后，进程不重启也要能恢复。

前置（由调用方在 shell 里做）：
  1. 删掉运行中实例的 %TEMP%\\_MEIxxxxxx\\certifi\\
  2. 删掉 DATA_ROOT\\certs\\cacert.pem
用法：python _verify_ca_selfheal.py http://127.0.0.1:18848
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:18848"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
HOSTS = (
    "https://www.bilibili.com/favicon.ico",
    "https://www.douyin.com/favicon.ico",
    "https://www.xiaohongshu.com/favicon.ico",
    "https://www.kuaishou.com/favicon.ico",
)
checks: list[tuple[str, bool, str]] = []


def ck(name: str, ok: bool, detail: str = "") -> None:
    checks.append((name, bool(ok), str(detail)[:220]))
    print(("  PASS " if ok else "  FAIL ") + name
          + ("" if ok or not detail else f"  << {str(detail)[:220]}"), flush=True)


def health() -> dict:
    with OPENER.open(BASE + "/api/health", timeout=20) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def main() -> int:
    print("[删掉 _MEI/certifi 与持久 CA 后，先发请求触发自愈]", flush=True)
    for url in HOSTS:
        host = urllib.parse.urlparse(url).hostname
        q = "/proxy?" + urllib.parse.urlencode({"url": url})
        try:
            with OPENER.open(BASE + q, timeout=35) as r:
                body = r.read()
            ck(f"{host} 自愈后 200", r.status == 200 and len(body) > 0,
               f"status={r.status} bytes={len(body)}")
        except urllib.error.HTTPError as e:
            ck(f"{host} 自愈后 200", False,
               f"HTTP {e.code} {(e.read(160) or b'').decode('utf-8', 'replace')}")
        except Exception as e:
            ck(f"{host} 自愈后 200", False, f"{type(e).__name__}: {e}")

    print("[自愈后的 health 证据]", flush=True)
    try:
        h = health()
        ca = h.get("ca_bundle") or {}
        print("   ca_bundle =", json.dumps(ca, ensure_ascii=False), flush=True)
        ck("CA 已重新固化", ca.get("exists") is True, ca)
        ck("路径仍是持久目录 /certs/",
           "/certs/" in str(ca.get("path") or "").replace("\\", "/"), ca.get("path"))
        ck("来源是可解释的自愈路径",
           str(ca.get("source") or "").startswith(
               ("windows-root-store", "persistent", "copy:", "env:")), ca.get("source"))
    except Exception as e:
        ck("health 可读且带 CA 证据", False, e)

    ok = sum(1 for _, c, _ in checks if c)
    bad = len(checks) - ok
    print(f"结果：{ok} 通过 / {bad} 失败", flush=True)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
