# -*- coding: utf-8 -*-
"""打包版「继承旧沙箱」验收。

验的是用户 2026-11-07 报的那条：
    新包放到 _dist_ui，双击后登录态和下载记录全没了。

断言链：
    1. exe 起来后 /api/health 的 data_root 指向 _dist_test\\MediaDownloader_TEST
    2. /api/history 读出 18 条台账（旧沙箱里的真实记录）
    3. platform_profiles 七个平台目录在，体积 > 100MB（登录态）
    4. %LOCALAPPDATA%\\MediaDownloader_TEST.sandbox 写入了旧沙箱路径
    5. 隔离说明.txt 的「沙箱来源」是 sibling / pointer
    6. _dist_ui 下不再新生成空沙箱

用法：
    _venv/Scripts/python.exe _verify_sandbox_inherit.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
EXE = ROOT / "_dist_ui" / "MediaDownloader_TEST.exe"
OLD = ROOT / "_dist_test" / "MediaDownloader_TEST"
EMPTY = ROOT / "_dist_ui" / "MediaDownloader_TEST"
PORT = 18866
PTR = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "MediaDownloader_TEST.sandbox"

PASS: list[str] = []
FAIL: list[str] = []


def ck(name: str, ok: bool, detail: object = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(("  PASS  " if ok else "  FAIL  ") + name + ("" if ok else f"  <- {detail}"))


def api(path: str, timeout: float = 6.0) -> dict:
    # 本机代理会让 127.0.0.1 走代理，显式绕开
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with op.open(f"http://127.0.0.1:{PORT}{path}", timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def stop_running() -> None:
    subprocess.run(["taskkill", "/IM", "MediaDownloader_TEST.exe", "/F"],
                   capture_output=True, check=False)
    time.sleep(1.5)


def main() -> int:
    if not EXE.is_file():
        print(f"缺少 {EXE}")
        return 1
    if not (OLD / "data" / "config.json").is_file():
        print(f"旧沙箱不存在或不完整：{OLD}")
        return 1

    before_profiles = len(list((OLD / "data" / "platform_profiles").glob("*")))
    before_history = (OLD / "data" / "data" / "download_history.json").stat().st_mtime
    print(f"旧沙箱：{OLD}")
    print(f"  平台登录态 {before_profiles} 个 / 台账 mtime {before_history:.0f}")

    stop_running()
    if PTR.exists():
        PTR.unlink()

    env = dict(os.environ)
    # 关键：两条覆盖变量都必须清掉，否则测的是「指定目录」而不是「自动继承」
    env.pop("MEDIA_DOWNLOADER_TEST_DATA_DIR", None)
    env.pop("MEDIA_DOWNLOADER_TEST_FRESH", None)
    env["MEDIA_DOWNLOADER_PORT"] = str(PORT)
    env["MEDIA_DOWNLOADER_NO_BROWSER"] = "1"
    env["NO_PROXY"] = "127.0.0.1,localhost"

    proc = subprocess.Popen([str(EXE)], env=env, cwd=str(EXE.parent),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    health: dict = {}
    for _ in range(40):
        time.sleep(1.0)
        if proc.poll() is not None:
            print(f"exe 已退出，code={proc.returncode}")
            break
        try:
            health = api("/api/health", timeout=2.0)
            break
        except Exception:  # noqa: BLE001
            continue

    ck("打包版启动成功", bool(health.get("ok")), health)
    if health:
        data_root = Path(health.get("data_root", "")).resolve()
        # config.DATA_ROOT = <沙箱>/data，不是沙箱根；台账/登录态都在它下面
        ck("data_root 指向旧沙箱的 data 目录", data_root == (OLD / "data").resolve(),
           f"{data_root} != {OLD / 'data'}")
        ck("resource_root 仍是新包目录",
           Path(health.get("root", "")).resolve() == EXE.parent.resolve(), health.get("root"))

    # ---- 台账 ----
    hist = {}
    try:
        hist = api("/api/history")
    except Exception as e:  # noqa: BLE001
        print("history 读取失败", e)
    stats = hist.get("stats") or {}
    ck("下载台账 18 条被继承", stats.get("keys") == 18, stats)

    # ---- 登录态 ----
    prof_dir = OLD / "data" / "platform_profiles"
    sites = sorted(p.name for p in prof_dir.glob("*") if p.is_dir())
    size_mb = sum(f.stat().st_size for f in prof_dir.rglob("*") if f.is_file()) / 1048576
    ck("七个平台登录态都在", len(sites) >= 7, sites)
    ck("登录态体积 > 100MB（真数据，不是空壳）", size_mb > 100, f"{size_mb:.0f}MB")

    # ---- 指针与说明 ----
    ck("指针文件已写入", PTR.is_file(), str(PTR))
    if PTR.is_file():
        ck("指针内容 = 旧沙箱", Path(PTR.read_text(encoding="utf-8").strip()).resolve() == OLD.resolve(),
           PTR.read_text(encoding="utf-8"))

    doc = OLD / "隔离说明.txt"
    ck("隔离说明写在旧沙箱", doc.is_file(), str(doc))
    if doc.is_file():
        txt = doc.read_text(encoding="utf-8")
        ck("说明里标注来源为自动接管", "沙箱来源   : sibling" in txt or "沙箱来源   : pointer" in txt, txt[:400])

    # ---- 不该再建空壳 ----
    ck("_dist_ui 下没有新建空沙箱", not EMPTY.exists(), str(EMPTY))

    # ---- 旧沙箱日志确实在写 ----
    logs = sorted((OLD / "data" / "logs").glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    fresh = bool(logs) and (time.time() - logs[0].stat().st_mtime) < 120
    ck("本次启动日志写在旧沙箱", fresh, logs[0].name if logs else "无日志")

    stop_running()
    print(f"\n结果：{len(PASS)} 通过 / {len(FAIL)} 失败")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
