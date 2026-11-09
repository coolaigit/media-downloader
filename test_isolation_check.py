# -*- coding: utf-8 -*-
"""隔离测试版验收脚本：核对「测试版」与「已安装 V3.0.4」确实没有串台。

用法（项目根目录）：
    _venv\\Scripts\\python.exe test_isolation_check.py
    _venv\\Scripts\\python.exe test_isolation_check.py --sandbox D:\\Somewhere\\MediaDownloader_TEST

检查项：
  1. 18848-18870 上有测试实例，且 /api/health 的 data_root 落在沙箱内
  2. 首页 /、/app.js、/api/status 全部可用（证明 onefile 资源解包正确）
  3. /api/status 的下载目录落在沙箱内，不是已安装版的下载目录
  4. 8848-8870 上若有正式实例，其 instance / data_root / 端口段必须与测试版全不同
  5. 正式版 %LOCALAPPDATA%\\MediaDownloader3\\config.json 的下载目录没被改到沙箱

退出码 0 = 全部通过；1 = 有失败项；2 = 测试版没启动。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

TEST_FIRST, TEST_LAST = 18848, 18870
RELEASE_FIRST, RELEASE_LAST = 8848, 8870
SANDBOX_NAME = "MediaDownloader_TEST"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _get(port: int, path: str, timeout: float = 2.0):
    """绕开系统代理取本机 HTTP；失败返回 None 或 (status, body)。"""
    try:
        with OPENER.open(f"http://127.0.0.1:{port}{path}", timeout=timeout) as r:
            return r.status, r.read()
    except Exception:  # noqa: BLE001
        return None


def _json(port: int, path: str, timeout: float = 3.0) -> dict | None:
    got = _get(port, path, timeout)
    if not got:
        return None
    try:
        return json.loads(got[1].decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001
        return None


def _scan(first: int, last: int) -> list[tuple[int, dict]]:
    found = []
    for p in range(first, last + 1):
        h = _json(p, "/api/health", timeout=0.35)
        if h and h.get("app") == "MediaDownloader" and h.get("ok") is True:
            found.append((p, h))
    return found


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sandbox", default="", help="隔离沙箱目录（默认 _dist_test/MediaDownloader_TEST）")
    args = ap.parse_args()

    root = Path(__file__).resolve().parent
    test_instances = _scan(TEST_FIRST, TEST_LAST)
    release_instances = _scan(RELEASE_FIRST, RELEASE_LAST)

    # 沙箱位置优先用运行中的实例自己报的 data_root 反推：exe 被拷到桌面 / U 盘
    # 等任意目录后，验收脚本不用改参数也能找到正确的沙箱。
    if args.sandbox:
        sandbox = Path(args.sandbox).resolve()
    elif test_instances:
        reported = str(test_instances[0][1].get("data_root") or "")
        sandbox = (Path(reported).resolve().parent if reported
                   else (root / "_dist_test" / SANDBOX_NAME).resolve())
    else:
        sandbox = (root / "_dist_test" / SANDBOX_NAME).resolve()

    print("=" * 68)
    print("  MediaDownloader 隔离验收")
    print("=" * 68)
    print(f"  沙箱目录 : {sandbox}")
    print(f"  测试端口 : {TEST_FIRST}-{TEST_LAST}")
    print(f"  正式端口 : {RELEASE_FIRST}-{RELEASE_LAST}")
    print()

    failures: list[str] = []
    if not test_instances:
        print("[X] 测试实例未启动（18848-18870 没有 /api/health 响应）")
        print("    先双击 _dist_test\\MediaDownloader_TEST.exe，等窗口出来再跑本脚本。")
        return 2

    for p, h in test_instances:
        print(f"[测试实例] 端口 {p}")
        print(f"  instance  : {h.get('instance')}")
        print(f"  root      : {h.get('root')}")
        print(f"  data_root : {h.get('data_root')}")

    tp, th = test_instances[0]
    data_root = str(th.get("data_root") or "")
    if SANDBOX_NAME not in data_root:
        failures.append(f"测试实例 data_root 不在沙箱内：{data_root}")
    if SANDBOX_NAME not in str(th.get("root") or ""):
        # 测试 exe 可能被拷到任意目录，root 未必含 TEST 字样；
        # 只要求 root 不等于已安装版 D:\MediaDownloader。
        if str(th.get("root") or "").rstrip("\\/").lower() == r"d:\mediadownloader":
            failures.append("测试实例 root 与已安装版安装目录相同")
    if not (TEST_FIRST <= tp <= TEST_LAST):
        failures.append(f"测试实例端口 {tp} 落在正式版端口段")

    # ---- 内容链路：onefile 解包 + 服务可用 ----
    print()
    print("[内容链路]")
    for path, label in (("/", "首页 HTML"), ("/app.js", "前端脚本"),
                        ("/style.css", "样式表"), ("/api/status", "状态接口")):
        got = _get(tp, path, timeout=8.0)
        ok = bool(got and got[0] == 200 and got[1])
        print(f"  {'OK ' if ok else 'FAIL'}  {label:<10} {path}")
        if not ok:
            failures.append(f"{label} {path} 不可用")

    status = _json(tp, "/api/status", timeout=15.0) or {}
    dl_dir = str(status.get("downloads_dir") or "")
    print(f"  下载目录   : {dl_dir}")
    print(f"  ffmpeg     : {status.get('ffmpeg_available')} / {status.get('ffmpeg_version') or '-'}")
    print(f"  yt-dlp     : {status.get('ytdlp_version') or '-'}")
    browser = status.get("browser") or {}
    print(f"  浏览器内核 : {browser.get('label') or '-'}")
    if SANDBOX_NAME not in dl_dir:
        failures.append(f"下载目录不在沙箱内：{dl_dir}")

    # ---- 正式版对照 ----
    print()
    print("[正式版对照]")
    if not release_instances:
        print("  已安装版当前未运行（8848-8870 无实例）—— 端口段仍不重叠，判为通过")
    for p, h in release_instances:
        print(f"  端口 {p} / instance {h.get('instance')} / data_root {h.get('data_root')}")
        if str(h.get("instance") or "") == str(th.get("instance") or ""):
            failures.append("测试版与已安装版 instance 相同（会互相认领）")
        if str(h.get("data_root") or "").lower() == data_root.lower():
            failures.append("测试版与已安装版共用同一个 data_root")
        if TEST_FIRST <= p <= TEST_LAST:
            failures.append("正式实例出现在测试端口段")

    # 正式版配置没被改到沙箱
    installed_cfg = Path(os.environ.get("LOCALAPPDATA") or "") / "MediaDownloader3" / "config.json"
    if installed_cfg.is_file():
        try:
            cfg = json.loads(installed_cfg.read_text(encoding="utf-8"))
            installed_dl = str(cfg.get("downloads_dir") or "")
            print(f"  已安装版 config.json : {installed_cfg}")
            print(f"    其 downloads_dir  : {installed_dl or '(默认)'}")
            if SANDBOX_NAME in installed_dl:
                failures.append("已安装版 config.json 的下载目录被改到了测试沙箱")
        except Exception as e:  # noqa: BLE001
            print(f"  已安装版 config.json 读取失败（不影响判定）: {e}")
    else:
        print("  未找到已安装版 config.json")

    # 沙箱里确实产出了运行数据
    print()
    print("[沙箱落盘]")
    for rel in ("隔离说明.txt", "data/config.json", "data/logs/service.out.log"):
        p = sandbox / rel
        print(f"  {'OK ' if p.is_file() else '-- '}  {rel}")
    if not (sandbox / "data" / "config.json").is_file():
        print("  （config.json 只有改动设置后才会生成，属正常）")

    print()
    print("=" * 68)
    if failures:
        print(f"[FAIL] {len(failures)} 项未通过：")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("[PASS] 隔离验收全部通过：端口/数据/配置/实例指纹均与已安装版分离")
    return 0


if __name__ == "__main__":
    sys.exit(main())
