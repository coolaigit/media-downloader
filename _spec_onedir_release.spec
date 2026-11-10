# -*- mode: python ; coding: utf-8 -*-
"""MediaDownloader 正式 onedir 打包定义（1E）。

入口必须是 launcher.py（--internal 分流在那里，见 1C）。
所有路径由本 spec 文件自身位置（SPECPATH）计算 —— 不依赖当前 cwd
（1C probe 踩过的坑：add-data 相对路径是相对 --specpath 解析的）。

构建（必须用隔离构建 venv，禁止裸 python/pip）：
    cd /d "D:\\QWen Project\\media-downloader-2.0-work"
    .venv-build\\Scripts\\python.exe -m PyInstaller --noconfirm MediaDownloader.spec

控制台排错版（同一 spec，弹 console 窗口看 stdout）：
    set MD_SPEC_CONSOLE=1 再执行同一条构建命令。

资源白名单制：datas 只收明确列出的项（web/ + bin/ffmpeg.exe + 证书/
playwright 运行时数据）。downloads/ data/ config.json cookies profile
login_state .workbuddy .wbapp_* _pylibs build-probe
.venv-build 等 sensitive/外部项从不进入 —— 它们不在白名单里。
"""
import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules, collect_data_files

SPEC_DIR = Path(SPECPATH).resolve()

# ---- 显式资源（白名单） ----
datas = [
    (str(SPEC_DIR / "web"), "web"),                      # 前端静态资源
    (str(SPEC_DIR / "bin" / "ffmpeg.exe"), "bin"),       # 静态 ffmpeg 9.0.2（随包分发）
    (str(SPEC_DIR / "使用说明.md"), "."),                    # 跨电脑安装/登录态/故障说明
    (str(SPEC_DIR / "app_icon.ico"), "."),                       # 系统托盘复用应用图标
]

# ---- V3.0：BrowserSkill（验证码时的人工协助）----
# bsk.exe 管 daemon 和会话，bsk-extension 给内置 Chromium 用（--load-extension）。
# 两样缺一个，验证码升级链路就断，所以缺了要在构建时吼出来而不是静默少打。
if (SPEC_DIR / "bin" / "bsk.exe").is_file():
    datas.append((str(SPEC_DIR / "bin" / "bsk.exe"), "bin"))
else:
    print("!! 警告：bin/bsk.exe 不存在 —— 打出来的包没有 BrowserSkill，验证码场景无法协助")

if (SPEC_DIR / "bsk-extension" / "manifest.json").is_file():
    datas.append((str(SPEC_DIR / "bsk-extension"), "bsk-extension"))
else:
    print("!! 警告：bsk-extension/ 不存在 —— 内置浏览器无法承载 BrowserSkill 扩展")

# ---- V3.0：内置 Playwright Chromium（约 430 MB）----
# 目的：用户电脑没有任何浏览器（只有 360/QQ 或什么都没有）也必须能做 JS 渲染和登录窗口。
# 只带完整 chromium，不带 chromium_headless_shell —— headless 用 --headless 跑完整内核，
# 省 272 MB。代价是运行时**必须**传 executable_path（见 app/browser_runtime.py）。
if (SPEC_DIR / "browsers").is_dir():
    # 只收 chromium-*/，跳过可能残留的 headless_shell / ffmpeg / winldd
    for _b in sorted((SPEC_DIR / "browsers").iterdir()):
        if _b.is_dir() and _b.name.startswith("chromium-"):
            datas.append((str(_b), f"browsers/{_b.name}"))
else:
    print("!! 警告：browsers/ 不存在，打出来的包没有内置 Chromium（只能靠用户系统浏览器）")
    print("!! 补下载：set PLAYWRIGHT_BROWSERS_PATH=%CD%\\browsers && python -m playwright install chromium")
datas += collect_data_files("certifi")                   # CA 证书（requests/yt-dlp 用）
datas += collect_data_files("playwright")                # playwright 驱动（node.exe 等）
                                                         #   浏览器内核见上面的 browsers/ 段（V3.0 起自带，
                                                         #   不再依赖用户系统 Chrome/Edge）

# ---- V3.0.4 补齐：WebView2 Fixed Version Runtime 随包 ----
# NuGet 上 Microsoft.Web.WebView2.FixedVersionRuntime 包已下架，改从本机
# Evergreen 安装（C:\Program Files (x86)\Microsoft\EdgeWebView\Application\<ver>\）
# 整目录拷到构建 staging —— Fixed Version 本质就是自包含目录拷贝，微软官方定位如此。
# desktop_shell._webview2_runtime_dir() 会在系统路径之前优先探测
# {app}\_internal\webview2\<ver>\msedgewebview2.exe，注册表坏了也能用。
# staging 缺失不阻断构建（只警告），打包行为与 V3.0.3 一致（靠目标机器系统运行时）。
# staging 路径可用 MD_WEBVIEW2_STAGE 覆盖：CI（GitHub Actions）上目录结构与本机
# 不同，不能把 E 盘绝对路径带进流水线；不设变量时行为与本地构建完全一致。
_wvr_stage = Path(
    os.environ.get("MD_WEBVIEW2_STAGE") or r"E:\_md_installer_build\webview2_staging\webview2"
)
if _wvr_stage.is_dir():
    datas.append((str(_wvr_stage), "webview2"))
else:
    print("!! 警告：webview2 staging 不存在（E:\\_md_installer_build\\webview2_staging\\webview2）")
    print("!!       安装包不带 WebView2 Fixed Runtime，桌面壳在注册表被破坏的机器上会降级 IE11")
datas += collect_data_files("yt_dlp_ejs")                # 1E.2：yt-dlp EJS 挑战脚本（.js 资源）
datas += collect_data_files("webview")                      # 桌面壳运行时资源（如有）

# ---- 1E.2：第三方许可证与声明（随包分发必须） ----
# 先落在 _internal（licenses\ + THIRD_PARTY_NOTICES.txt），构建后再复制
# 一份到 RC 根目录（用户直接可见）——见 BUILD_MANIFEST 的构建命令。
datas += [
    (str(SPEC_DIR / "licenses"), "licenses"),
    (str(SPEC_DIR / "THIRD_PARTY_NOTICES.txt"), "."),
    (str(SPEC_DIR / "FFMPEG_DISTRIBUTION_NOTES.md"), "licenses"),
]

# ---- 懒加载模块必须显式收集 ----
# 本地 app 包不能依赖 collect_submodules("app")：spec 评估阶段的 sys.path
# 与 Analysis.pathex 生效阶段不同，实测它可能返回空/错误包，导致 app.main 未进 PYZ，
# Frozen 启动即 “Could not import module app.main”。直接按项目文件枚举最可靠。
_app_hidden = ["app"] + [
    f"app.{p.stem}" for p in sorted((SPEC_DIR / "app").glob("*.py"))
    if p.name != "__init__.py"
]
hiddenimports = (
    collect_submodules("gallery_dl")          # gallery-dl 按字符串懒加载 extractor/*.py
    + collect_submodules("xhshow")            # 小红书签名库
    + collect_submodules("yt_dlp.extractor")  # yt-dlp 同理
    + collect_submodules("webview")           # pywebview 后端按运行环境动态导入
    + collect_submodules("pystray")           # 系统托盘后端按平台动态导入
    + _app_hidden
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
