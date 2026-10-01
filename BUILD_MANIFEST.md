# BUILD_MANIFEST — MediaDownloader Release Candidate

> 每次正式发布前更新本文件。构建必须用隔离 venv，禁止裸 python/pip。

## 本次构建（1E Candidate，2026-10-01）

| 项 | 值 |
|---|---|
| Git commit（release-work） | 见 `git log --oneline -1`（1E 提交） |
| 基线参照 | `media-downloader` @ `fb573e0`（只读，全程零改动） |
| 构建解释器 | `.venv-build\Scripts\python.exe`（源自 `C:\Python314\python.exe` 3.14.6） |
| PyInstaller | 6.22.2（onedir + windowed；排错版 `MD_SPEC_CONSOLE=1` 同 spec） |
| spec | `MediaDownloader.spec`（入口 `launcher.py`，路径由 SPECPATH 计算） |
| 构建命令 | `.venv-build\Scripts\python.exe -m PyInstaller --noconfirm MediaDownloader.spec` |
| 依赖 lock | `requirements.lock.txt`（35 项），SHA256 `367d07cfe75a6aeb6d939c599bb8136e8482a299dda03e3eeb88f2f51c8fbc7b` |
| 产物 | `dist\MediaDownloader\`，约 260 MB（大头为 Playwright 驱动 node.exe） |

## 关键依赖（锁定版本 = 已验证基线）

fastapi 0.141.1 · uvicorn 0.52.4 · requests 2.34.2 · beautifulsoup4 4.15.0 ·
lxml 6.1.1 · pillow 12.3.0 · yt-dlp 2026.7.4 · pydantic 2.13.4 ·
playwright 1.62.0（pyee 13.0.1 / greenlet 3.5.6）· gallery_dl 1.32.13 · ddgs 9.16.0

## 二进制与外部资源

| 项 | 值 | 策略 |
|---|---|---|
| ffmpeg | 7.1-essentials_build-www.gyan.dev，SHA256 `2ce797a0f88d7f067180338fb227f7b1928ea727bd9a4d7a1d022f7c52af71a3`（87,638,016 B） | spec 自带 `bin/ffmpeg.exe`；回退 FFMPEG_PATH > PATH。License：LGPL/GPL 混合（essentials build），阶段 4 汇总 THIRD_PARTY_NOTICES |
| Playwright 浏览器 | **不打包浏览器** | 复用用户系统 Chrome/Edge（现有代码已支持）；驱动 node.exe 属 Python 运行时随包 |
| JS runtime | **不内置** | 只认用户 PATH / Node.js 官方安装位置；frozen 下排除 WorkBuddy 托管 node（app/config.py `find_js_runtime` FROZEN 门控）。缺失时 UI/`/api/status` 明确显示降级（`js_runtime: null`） |
| MediaCrawler | **不进 EXE** | Desktop 可选外部依赖：自动发现 / `mediacrawler_root` 设置 / `MEDIACRAWLER_PYTHON`；未配置时优雅降级 |

## spec 排除（从未进入产物，白名单制收集）

downloads/ · data/ · config.json · cookies · Browser Profile · login_state ·
.workbuddy/ · .wbapp_* · .playwright-mcp/ · .pytest_cache/ · `_*.log/txt/png` ·
probe/diag 产物 · build-probe/ · release-secrets · MediaCrawler 全项目 · .venv-build/ · _pylibs/（源码模式保留 fallback，EXE 不带）

## 首构建实证教训

`app.main` 仅被 uvicorn 以 `"app.main:app"` 字符串引用，静态分析链抓不到 →
缺 hiddenimport 时启动即 `Could not import module "app.main"`。spec 用
`collect_submodules("app")` 显式收集整个 app 包。

## Frozen 验证记录（本次）

gallery-dl internal `--version` 1.32.13 exit 0；真实 metadata 提取
（commons.wikimedia.org 文件页，wikimediacommons extractor 动态模块生效）exit 0。
yt-dlp 2026.07.04 frozen 导入 + 真实解析（Wikimedia Big_Buck_Bunny_4K.webm，ok=true，
cookies 预检按设计降级）。DETACHED（双击）启动 / 自动选端口 / health / 静态资源 /
图片搜索下载 / login-browser internal 真启动（pid）/ exe 目录零运行时写入 —— 全过，
明细见 `.workbuddy/memory/2026-10-01.md` 阶段 1E 节。
