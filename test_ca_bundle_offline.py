# -*- coding: utf-8 -*-
"""离线回归：frozen 长期运行的 TLS CA 固化与自愈（F-20）。

不依赖实例、不联网。核心场景：
  1. 启动时把 CA 复制进 DATA_ROOT/certs，所有 HTTP 客户端指向它；
  2. %TEMP%\\_MEIxxxxxx\\certifi 被清理后仍能发 HTTPS（用持久副本）；
  3. 连持久副本也被删时能自愈（系统根证书兜底）；
  4. 用户自定义 CA 优先、MEDIA_DOWNLOADER_CA_DISABLE 可关。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import ca_bundle as cb
import certifi
import requests.adapters as adapters

CHECKS: list[tuple[str, bool, str]] = []


def ck(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, bool(ok), str(detail)[:300]))
    print(("  PASS " if ok else "  FAIL ") + name + (f"  << {detail}" if detail and not ok else ""),
          flush=True)


def make_pem(p: Path, n: int = 150) -> None:
    # _valid_pem 要求 >= 1024 B，夹具必须够大，否则会被当成「被清空的 0 字节文件」
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("-----BEGIN CERTIFICATE-----\n" + ("QUJDREVG\n" * n) +
                 "-----END CERTIFICATE-----\n", encoding="ascii")


def main() -> int:
    saved_env = {k: os.environ.get(k) for k in cb._ENV_KEYS + ("MEDIA_DOWNLOADER_CA_BUNDLE",
                                                               "MEDIA_DOWNLOADER_CA_DISABLE")}
    saved_where, saved_core_where = certifi.where, getattr(getattr(certifi, "core", None), "where", None)
    saved_default = adapters.DEFAULT_CA_BUNDLE_PATH
    saved_state = dict(cb._STATE)

    with tempfile.TemporaryDirectory(prefix="ca_bundle_test_") as td:
        root = Path(td) / "data"
        stable = root / "certs" / "cacert.pem"
        src = Path(td) / "_MEI00000" / "certifi" / "cacert.pem"
        make_pem(src)
        try:
            # --- 基本判据 ---
            ck("有效 PEM 被认出", cb._valid_pem(src))
            empty = Path(td) / "empty.pem"
            empty.write_bytes(b"")
            notpem = Path(td) / "notpem.txt"
            notpem.write_text("hello", encoding="ascii")
            ck("0 字节 / 不存在 / 非 PEM 都不算",
               not cb._valid_pem(Path(td) / "missing.pem")
               and not cb._valid_pem(empty)
               and not cb._valid_pem(notpem))

            # --- 场景 1：启动固化 ---
            for k in cb._ENV_KEYS + ("MEDIA_DOWNLOADER_CA_BUNDLE", "MEDIA_DOWNLOADER_CA_DISABLE"):
                os.environ.pop(k, None)
            with mock.patch.object(cb, "_candidate_sources", return_value=[src]):
                r = cb.install(root)
            ck("install 生成持久副本", cb._valid_pem(stable), str(r))
            ck("副本内容来自 _MEI 源", stable.read_bytes() == src.read_bytes())
            ck("状态标记为 copy 源", r["source"].startswith("copy:"), r["source"])
            ck("REQUESTS_CA_BUNDLE 指向持久副本",
               os.environ.get("REQUESTS_CA_BUNDLE") == str(stable))
            ck("CURL_CA_BUNDLE 指向持久副本",
               os.environ.get("CURL_CA_BUNDLE") == str(stable))
            ck("SSL_CERT_FILE 指向持久副本（yt-dlp 走这条）",
               os.environ.get("SSL_CERT_FILE") == str(stable))
            ck("requests.adapters 缓存常量被改写",
               adapters.DEFAULT_CA_BUNDLE_PATH == str(stable))
            ck("certifi.where() 被改写为持久副本", certifi.where() == str(stable))
            ck("health 用的 status() 可用", cb.status()["exists"] and cb.status()["size"] > 100)

            # --- 场景 2：_MEI 临时目录被清理 ---
            src.unlink()
            src.parent.rmdir()
            with mock.patch.object(cb, "_candidate_sources", return_value=[src]):
                r = cb.install(root)
            ck("_MEI 被删后仍复用持久副本", r["source"] == "persistent", r["source"])
            ck("_MEI 被删后 ca_path() 有效", cb._valid_pem(cb.ca_path()))
            ck("_MEI 被删后 env 没被改坏",
               os.environ.get("REQUESTS_CA_BUNDLE") == str(stable))

            # --- 场景 3：用户 env 指向坏路径，必须覆盖而不是沿用 ---
            os.environ["REQUESTS_CA_BUNDLE"] = str(Path(td) / "gone.pem")
            cb.install(root)
            ck("无效 env 被覆盖成有效 CA",
               cb._valid_pem(os.environ["REQUESTS_CA_BUNDLE"]),
               os.environ.get("REQUESTS_CA_BUNDLE"))

            # --- 场景 4：持久副本也被删，自愈 ---
            stable.unlink()
            with mock.patch.object(cb, "_candidate_sources", return_value=[]):
                r = cb.refresh()
            ck("refresh 后重新生成 CA", cb._valid_pem(stable), str(r))
            ck("refresh 来源为系统根证书或 persistent",
               r["source"] in ("windows-root-store", "persistent"), r["source"])
            ck("refresh 后 ca_path() 仍有效", cb._valid_pem(cb.ca_path()))

            # --- 场景 5：自定义 CA 优先 ---
            custom = Path(td) / "corp-ca.pem"
            make_pem(custom)
            os.environ["MEDIA_DOWNLOADER_CA_BUNDLE"] = str(custom)
            os.environ["SSL_CERT_FILE"] = str(Path(td) / "bad.pem")
            r = cb.install(root)
            ck("自定义 CA 被采用", r["source"] == "env:MEDIA_DOWNLOADER_CA_BUNDLE", r["source"])
            ck("自定义 CA 路径不被覆盖", r["path"] == str(custom), r["path"])
            ck("同时的坏 SSL_CERT_FILE 被修好",
               cb._valid_pem(os.environ.get("SSL_CERT_FILE")))

            # --- 场景 6：可关闭 ---
            os.environ.pop("MEDIA_DOWNLOADER_CA_BUNDLE", None)
            os.environ["MEDIA_DOWNLOADER_CA_DISABLE"] = "1"
            r = cb.install(root)
            ck("CA_DISABLE=1 时不固化", r["source"] == "disabled", r["source"])
            os.environ.pop("MEDIA_DOWNLOADER_CA_DISABLE", None)

            # --- 原子写不留临时文件 ---
            leftovers = list((root / "certs").glob("*.tmp.*"))
            ck("原子写不残留 .tmp 文件", not leftovers, str(leftovers))
        finally:
            cb._STATE.clear()
            cb._STATE.update(saved_state)
            certifi.where = saved_where
            if saved_core_where is not None and getattr(certifi, "core", None) is not None:
                certifi.core.where = saved_core_where
            adapters.DEFAULT_CA_BUNDLE_PATH = saved_default
            for k, v in saved_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    bad = [n for n, ok, _ in CHECKS if not ok]
    print(f"结果：{len(CHECKS) - len(bad)} 通过 / {len(bad)} 失败", flush=True)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
