# -*- coding: utf-8 -*-
"""验证 V3.0 的数据目录隔离与「首次从 V2.5 复制」逻辑。

要证明三件事：
1. 默认数据目录是 MediaDownloader3（不再和 V2.5 共用 MediaDownloader）
2. 首次启动会把 V2.5 的 config / 台账 / 平台登录态**复制**过来
3. 只迁移一次；显式指定 MEDIA_DOWNLOADER_DATA_DIR 时完全不迁移（测试/部署必须可控）

做法：把 LOCALAPPDATA 指到一个临时目录并造一个假的 V2.5 数据目录，
在子进程里 import config（因为 DATA_ROOT 是模块级常量，同一进程改不了）。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent
FAILS = []


def check(cond, msg):
    print(("  [OK ] " if cond else "  [FAIL] ") + msg)
    if not cond:
        FAILS.append(msg)


def run_config(env_extra: dict) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.pop("MEDIA_DOWNLOADER_DATA_DIR", None)
    env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, '.');"
         "from app import config;"
         "print('DATA_ROOT=' + str(config.DATA_ROOT));"
         "print('TITLE=' + config.APP_TITLE);"
         "print('VERSION=' + config.APP_VERSION)"],
        cwd=str(REPO), env=env, capture_output=True, text=True, timeout=180)


tmp = Path(tempfile.mkdtemp(prefix="mdtest_"))
try:
    legacy = tmp / "MediaDownloader"
    (legacy / "data").mkdir(parents=True)
    (legacy / "config.json").write_text(
        json.dumps({"downloads_dir": r"D:\劳彬钿", "skip_existing": True}, ensure_ascii=False),
        encoding="utf-8")
    (legacy / "data" / "download_history.json").write_text(
        json.dumps({"version": 1, "records": {"k1": {"ts": 1791201923.0, "title": "t"}}}),
        encoding="utf-8")
    wb = legacy / "platform_profiles" / "wb"
    wb.mkdir(parents=True)
    (wb / "cookies_from_profile.txt").write_text("# netscape cookie file\n", encoding="utf-8")
    (wb / "login_state.json").write_text('{"ts": 1}', encoding="utf-8")
    # 干扰项：logs / tmp 不该被迁移
    (legacy / "logs").mkdir()
    (legacy / "logs" / "old.log").write_text("x", encoding="utf-8")

    print("=== 1. 首次启动：应复制 V2.5 数据到 MediaDownloader3 ===")
    r = run_config({"LOCALAPPDATA": str(tmp)})
    out = r.stdout or ""
    if r.returncode != 0:
        print("  子进程失败:", (r.stderr or "")[-400:])
    newroot = tmp / "MediaDownloader3"
    check(f"DATA_ROOT={newroot}" in out, "默认数据目录是 MediaDownloader3")
    check("TITLE=MediaDownloader 3.0" in out, "产品名是 MediaDownloader 3.0")
    check("VERSION=3.0.0" in out, "版本号 3.0.0")
    check((newroot / "config.json").is_file(), "config.json 已复制")
    check((newroot / "data" / "download_history.json").is_file(), "下载台账已复制")
    check((newroot / "platform_profiles" / "wb" / "cookies_from_profile.txt").is_file(),
          "平台登录态已复制")
    check(not (newroot / "logs" / "old.log").exists(),
          "旧日志内容不被迁移（logs 目录本身由程序自己创建）")
    check((newroot / ".migrated_from_v25").is_file(), "写了迁移标记")

    cfg = json.loads((newroot / "config.json").read_text(encoding="utf-8"))
    check(cfg.get("downloads_dir") == r"D:\劳彬钿", "下载目录原样继承（V3.0 仍下到同一处）")

    print("=== 2. 源目录还在，但不应重复迁移 ===")
    marker_before = (newroot / ".migrated_from_v25").stat().st_mtime
    (newroot / "config.json").write_text('{"downloads_dir":"我改过的"}', encoding="utf-8")
    r2 = run_config({"LOCALAPPDATA": str(tmp)})
    cfg2 = json.loads((newroot / "config.json").read_text(encoding="utf-8"))
    check(cfg2.get("downloads_dir") == "我改过的", "第二次启动没有用旧数据覆盖用户改动")
    check((newroot / ".migrated_from_v25").stat().st_mtime == marker_before,
          "迁移标记没被重写（确实只迁移一次）")

    print("=== 3. 显式指定数据目录时绝不迁移 ===")
    other = tmp / "explicit_dir"
    r3 = run_config({"LOCALAPPDATA": str(tmp),
                     "MEDIA_DOWNLOADER_DATA_DIR": str(other)})
    check(f"DATA_ROOT={other}" in (r3.stdout or ""), "DATA_ROOT 用显式指定的路径")
    check(not (other / ".migrated_from_v25").exists(), "显式指定时不写迁移标记")
    check(not (other / "platform_profiles").exists(), "显式指定时不复制平台数据")

    print("=== 4. 没有 V2.5 数据时不报错 ===")
    empty = tmp / "empty_localappdata"
    empty.mkdir()
    r4 = run_config({"LOCALAPPDATA": str(empty)})
    check(r4.returncode == 0, "全新机器（无 V2.5）也能正常启动")
    check((empty / "MediaDownloader3").is_dir(), "建了新的数据目录")
finally:
    shutil.rmtree(tmp, ignore_errors=True)

if FAILS:
    print(f"\n[FAIL] {len(FAILS)} 项未通过")
    sys.exit(1)
print("\n数据目录隔离与首次迁移验证通过")
