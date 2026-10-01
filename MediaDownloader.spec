# -*- mode: python ; coding: utf-8 -*-
"""MediaDownloader 正式 onedir 打包定义（1E）。

入口必须是 launcher.py（--internal 分流在那里，见 1C）。
所有路径由本 spec 文件自身位置（SPECPATH）计算 —— 不依赖当前 cwd
（1C probe 踩过的坑：add-data 相对路径是相对 --specpath 解析的）。

构建（必须用隔离构建 venv，禁止裸 python/pip）：
    cd /d "D:\\QWen Project\\media-downloader-release-work"
    .venv-build\\Scripts\\python.exe -m PyInstaller --noconfirm MediaDownloader.spec

控制台排错版（同一 spec，弹 console 窗口看 stdout）：
    set MD_SPEC_CONSOLE=1 再执行同一条构建命令。

资源白名单制：datas 只收明确列出的项（web/ + bin/ffmpeg.exe + 证书/
playwright 运行时数据）。downloads/ data/ config.json cookies profile
login_state .workbuddy .wbapp_* _pylibs MediaCrawler build-probe
.venv-build 等 sensitive/外部项从不进入 —— 它们不在白名单里。
"""
import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules, collect_data_files

SPEC_DIR = Path(SPECPATH).resolve()

# ---- 显式资源（白名单） ----
datas = [
    (str(SPEC_DIR / "web"), "web"),                      # 前端静态资源
    (str(SPEC_DIR / "bin" / "ffmpeg.exe"), "bin"),       # ffmpeg 二进制（1E 策略：发行包自带）
]
datas += collect_data_files("certifi")                   # CA 证书（requests/yt-dlp 用）
datas += collect_data_files("playwright")                # playwright 驱动（node.exe 等 Python 运行时，
                                                         #   不含浏览器 —— 浏览器复用用户系统 Chrome/Edge）
datas += collect_data_files("yt_dlp_ejs")                # 1E.2：yt-dlp EJS 挑战脚本（.js 资源）

# ---- 1E.2：第三方许可证与声明（随包分发必须） ----
# 先落在 _internal（licenses\ + THIRD_PARTY_NOTICES.txt），构建后再复制
# 一份到 RC 根目录（用户直接可见）——见 BUILD_MANIFEST 的构建命令。
datas += [
    (str(SPEC_DIR / "licenses"), "licenses"),
    (str(SPEC_DIR / "THIRD_PARTY_NOTICES.txt"), "."),
    (str(SPEC_DIR / "FFMPEG_DISTRIBUTION_NOTES.md"), "licenses"),
]

# ---- 懒加载模块必须显式收集 ----
hiddenimports = (
    collect_submodules("gallery_dl")          # gallery-dl 按字符串懒加载 extractor/*.py
    + collect_submodules("yt_dlp.extractor")  # yt-dlp 同理（提取器几百个，按名动态 import）
    + collect_submodules("app")               # ⚠️ app.main 只被 uvicorn 以 "app.main:app"
                                              # 字符串引用，静态分析链抓不到 —— 必须
                                              # 显式收集整个 app 包（1E 首构建实证：
                                              # 缺了它启动即 Could not import module）
)

a = Analysis(
    [str(SPEC_DIR / "launcher.py")],
    pathex=[str(SPEC_DIR)],
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

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="MediaDownloader",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=bool(int(os.environ.get("MD_SPEC_CONSOLE", "0"))),
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # 1E.2：正式版本资源 + 项目自有图标（app_icon.ico 由 PIL 生成，版权清晰）
    version=str(SPEC_DIR / "version_info.txt"),
    icon=str(SPEC_DIR / "app_icon.ico"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="MediaDownloader",
)
