# -*- mode: python ; coding: utf-8 -*-
r"""隔离测试版 onefile（单文件 EXE）打包定义。

与正式 MediaDownloader.spec 的区别：
  - 入口换成 test_portable_entry.py（在任何 app.* 导入前注入隔离环境变量）
  - EXE 直接吃下 a.binaries / a.datas，不再 COLLECT → 只有一个 exe 文件
  - 名字 / 版本资源 / 端口段 / 数据目录全部带 TEST 标识
  - 不带 bin/ffmpeg.exe、browsers/Chromium、bsk.exe、bsk-extension：
    本仓库当前没有这些资产，测试版用系统 ffmpeg / 系统浏览器 / WebView2，
    BrowserSkill 由入口禁用（不连全局 daemon）。

构建（项目根目录执行）：
    _venv\Scripts\python.exe -m PyInstaller --noconfirm ^
        --distpath _dist_test --workpath _build_test MediaDownloader_Test.spec

产物：_dist_test\MediaDownloader_TEST.exe

控制台排错版（同一 spec，弹 console 窗口看 stdout）：
    set MD_TEST_CONSOLE=1 再执行同一条构建命令。
"""
import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules, collect_data_files

SPEC_DIR = Path(SPECPATH).resolve()
PROJECT = SPEC_DIR


def _safe_data(pkg: str) -> list:
    try:
        return collect_data_files(pkg)
    except Exception as e:  # noqa: BLE001 —— 可选依赖缺失不该让整个构建崩
        print(f"!! 跳过 {pkg} 的数据文件: {type(e).__name__}: {e}")
        return []


def _safe_submodules(pkg: str) -> list:
    try:
        return collect_submodules(pkg)
    except Exception as e:  # noqa: BLE001
        print(f"!! 跳过 {pkg} 的懒加载模块: {type(e).__name__}: {e}")
        return []


# ---- 显式资源（白名单） ----
datas = [
    (str(PROJECT / "web"), "web"),                    # 前端静态资源（必需）
    (str(PROJECT / "app_icon.ico"), "."),             # 托盘图标
    (str(PROJECT / "THIRD_PARTY_NOTICES.txt"), "."),
]

# playwright 的 driver/node.exe 给 yt-dlp 当 JS runtime（frozen 下优先用它）；
# webview 的 lib/*.dll 是 pywebview WinForms/EdgeChromium 后端必需的。
for _pkg in ("certifi", "playwright", "yt_dlp_ejs", "webview", "pystray"):
    datas += _safe_data(_pkg)

# 本地 app 包不能只靠 collect_submodules("app")：spec 评估阶段的 sys.path
# 与 Analysis.pathex 生效阶段不同，实测可能返回空，导致 frozen 启动报
# "Could not import module app.main"。按项目文件枚举最可靠。
_app_hidden = ["app"] + [
    f"app.{p.stem}" for p in sorted((PROJECT / "app").glob("*.py"))
    if p.name != "__init__.py"
]

hiddenimports = (
    _safe_submodules("gallery_dl")           # 按字符串懒加载 extractor/*.py
    + _safe_submodules("xhshow")
    + _safe_submodules("yt_dlp.extractor")
    + _safe_submodules("webview")            # 后端按运行环境动态导入
    + _safe_submodules("pystray")
    + _safe_submodules("clr_loader")         # pythonnet 的 .NET 加载器
    + _app_hidden
    + ["launcher", "serve", "clr"]
)

a = Analysis(
    [str(PROJECT / "test_portable_entry.py")],
    pathex=[str(PROJECT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

# onefile：把 binaries/datas 一起塞进 EXE，不生成 _internal 目录、不 COLLECT。
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="MediaDownloader_TEST",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=bool(int(os.environ.get("MD_TEST_CONSOLE", "0"))),
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version=str(PROJECT / "version_info_test.txt"),
    icon=str(PROJECT / "app_icon.ico"),
)
