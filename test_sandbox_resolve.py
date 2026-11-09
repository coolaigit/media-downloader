# -*- coding: utf-8 -*-
"""沙箱定位离线用例（无网络、无 GUI）。

覆盖 test_portable_entry.resolve_sandbox_root 的六条分支：
    env 指定 / FRESH 强制新沙箱 / 指针接管 / 同级命中 /
    兄弟目录接管（本次事故路径）/ 全部落空新建

背景：测试版沙箱原来是「exe 所在目录 / MediaDownloader_TEST」。
打包输出从 _dist_test 换到 _dist_ui 后，用户拿到的是全新空沙箱，
七个平台登录态（390MB）和 18 条下载台账全部「消失」。
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import test_portable_entry as tpe

BASE = Path(tempfile.mkdtemp(prefix="sandbox_case_"))
PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, ok: bool, detail: object = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(("  PASS  " if ok else "  FAIL  ") + name + ("" if ok else f"  <- {detail}"))


def make_sandbox(root: Path, *, config: bool = True, history: int = 0,
                 profiles: bool = False) -> Path:
    data = root / "data"
    data.mkdir(parents=True, exist_ok=True)
    if config:
        (data / "config.json").write_text('{"ui_mode":"obsidian"}', encoding="utf-8")
    if history:
        (data / "data").mkdir(parents=True, exist_ok=True)
        recs = {f"k{i}": {"url": f"http://x/{i}", "status": "done"} for i in range(history)}
        (data / "data" / "download_history.json").write_text(
            json.dumps({"version": 2, "records": recs}, ensure_ascii=False), encoding="utf-8")
    if profiles:
        for site in ("bili", "dy", "xhs"):
            (data / "platform_profiles" / site).mkdir(parents=True, exist_ok=True)
    return root


def new_world(name: str) -> Path:
    w = BASE / name / "world"
    w.mkdir(parents=True, exist_ok=True)
    return w


def isolate(local_appdata: Path) -> None:
    os.environ["LOCALAPPDATA"] = str(local_appdata)
    os.environ.pop("MEDIA_DOWNLOADER_TEST_DATA_DIR", None)
    os.environ.pop("MEDIA_DOWNLOADER_TEST_FRESH", None)


def main() -> int:
    print("沙箱定位用例")

    # ---------- 1. env 硬指定优先级最高 ----------
    t = BASE / "case_env" / "world"
    t.mkdir(parents=True, exist_ok=True)
    iso = t / "appdata"
    iso.mkdir(parents=True, exist_ok=True)
    isolate(iso)
    forced = t / "forced_data"
    forced.mkdir(parents=True, exist_ok=True)
    os.environ["MEDIA_DOWNLOADER_TEST_DATA_DIR"] = str(forced)
    got, why = tpe.resolve_sandbox_root(t / "exe")
    check("env 指定时直接用该目录", got == forced.resolve() and why.startswith("env"), (got, why))
    os.environ.pop("MEDIA_DOWNLOADER_TEST_DATA_DIR")

    # ---------- 2. 指针接管：exe 换到新目录仍回老沙箱 ----------
    t = BASE / "case_pointer" / "world"
    t.mkdir(parents=True, exist_ok=True)
    iso = t / "appdata"
    iso.mkdir(parents=True, exist_ok=True)
    isolate(iso)
    old = make_sandbox(t / "old_build" / tpe.SANDBOX_NAME, history=18, profiles=True)
    (iso / tpe.POINTER_NAME).write_text(str(old), encoding="utf-8")
    got, why = tpe.resolve_sandbox_root(t / "brand_new_dir")
    check("指针指向的老沙箱被接管", got == old and why == "pointer", (got, why))

    # ---------- 3. 同级命中：传统便携布局 ----------
    t = BASE / "case_local" / "world"
    t.mkdir(parents=True, exist_ok=True)
    iso = t / "appdata"
    iso.mkdir(parents=True, exist_ok=True)
    isolate(iso)
    local = make_sandbox(t / tpe.SANDBOX_NAME, history=3)
    got, why = tpe.resolve_sandbox_root(t)
    check("exe 同级有数据时优先同级", got == local and why == "local", (got, why))

    # ---------- 4. 兄弟目录接管：本次事故路径 ----------
    # 结构：<root>/_dist_ui/MediaDownloader_TEST.exe
    #       <root>/_dist_test/MediaDownloader_TEST/data/{config,profiles}
    t = BASE / "case_sibling" / "world"
    t.mkdir(parents=True, exist_ok=True)
    iso = t / "appdata"
    iso.mkdir(parents=True, exist_ok=True)
    isolate(iso)
    old = make_sandbox(t / "_dist_test" / tpe.SANDBOX_NAME, history=18, profiles=True)
    newdir = t / "_dist_ui"
    newdir.mkdir(parents=True, exist_ok=True)
    got, why = tpe.resolve_sandbox_root(newdir)
    check("新输出目录自动接管兄弟老沙箱", got == old and why == "sibling", (got, why))

    # ---------- 5. 多个候选时取最新 ----------
    t = BASE / "case_newest" / "world"
    t.mkdir(parents=True, exist_ok=True)
    iso = t / "appdata"
    iso.mkdir(parents=True, exist_ok=True)
    isolate(iso)
    a = make_sandbox(t / "out_a" / tpe.SANDBOX_NAME, history=2)
    b = make_sandbox(t / "out_b" / tpe.SANDBOX_NAME, history=9)
    os.utime(a / "data" / "data" / "download_history.json", (1_700_000_000, 1_700_000_000))
    os.utime(b / "data" / "data" / "download_history.json", (1_760_000_000, 1_760_000_000))
    got, why = tpe.resolve_sandbox_root(t / "out_c")
    check("多候选取台账最新的那份", got == b and why == "sibling", (got, why))

    # ---------- 6. FRESH 强制新沙箱 ----------
    t = BASE / "case_fresh" / "world"
    t.mkdir(parents=True, exist_ok=True)
    iso = t / "appdata"
    iso.mkdir(parents=True, exist_ok=True)
    isolate(iso)
    old = make_sandbox(t / "old" / tpe.SANDBOX_NAME, history=5, profiles=True)
    (iso / tpe.POINTER_NAME).write_text(str(old), encoding="utf-8")
    os.environ["MEDIA_DOWNLOADER_TEST_FRESH"] = "1"
    got, why = tpe.resolve_sandbox_root(t / "exe")
    check("FRESH=1 忽略指针新建沙箱",
          got == (t / "exe" / tpe.SANDBOX_NAME) and why == "new", (got, why))
    os.environ.pop("MEDIA_DOWNLOADER_TEST_FRESH")

    # ---------- 7. 什么都没有时新建 ----------
    t = BASE / "case_empty" / "world"
    t.mkdir(parents=True, exist_ok=True)
    iso = t / "appdata"
    iso.mkdir(parents=True, exist_ok=True)
    isolate(iso)
    got, why = tpe.resolve_sandbox_root(t / "exe")
    check("无任何历史时新建 exe 同级沙箱",
          got == (t / "exe" / tpe.SANDBOX_NAME) and why == "new", (got, why))

    # ---------- 8. 空壳目录不算「有数据」 ----------
    t = BASE / "case_shell" / "world"
    t.mkdir(parents=True, exist_ok=True)
    iso = t / "appdata"
    iso.mkdir(parents=True, exist_ok=True)
    isolate(iso)
    shell = t / "_dist_ui" / tpe.SANDBOX_NAME / "data" / "logs"
    shell.mkdir(parents=True, exist_ok=True)
    real = make_sandbox(t / "_dist_test" / tpe.SANDBOX_NAME, history=7)
    got, why = tpe.resolve_sandbox_root(t / "_dist_ui")
    check("空壳沙箱（只有 logs）不被当成已有数据", got == real and why == "sibling", (got, why))

    print(f"\n结果：{len(PASS)} 通过 / {len(FAIL)} 失败")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
