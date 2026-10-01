# BUILD_MANIFEST — MediaDownloader Release Candidate

> 每次正式发布前更新本文件。构建必须用隔离 venv，禁止裸 python/pip。

## RC2（1E.2，2026-10-01）

| 项 | 值 |
|---|---|
| RC2 git commit（release-work） | 见 `git log --oneline -1`（1E.2 提交；RC1 存档于 `dist-archive\RC1-0aaf468\`） |
| 基线参照 | `media-downloader` @ `fb573e0`（只读，全程零改动） |
| 构建解释器 | `.venv-build\Scripts\python.exe`（源自 `C:\Python314\python.exe` 3.14.6） |
| PyInstaller | 6.22.2（onedir + windowed；排错版 `MD_SPEC_CONSOLE=1` 同 spec） |
| spec | `MediaDownloader.spec`（入口 `launcher.py`，SPECPATH 计算路径；含 version resource + icon） |
| 构建命令 | `.venv-build\Scripts\python.exe -m PyInstaller --noconfirm MediaDownloader.spec`<br>后处理：复制 `licenses\`、`THIRD_PARTY_NOTICES.txt` 到 `dist\MediaDownloader\` 根 |
| 依赖 lock | `requirements.lock.txt`（40 项），SHA256 `93e40dcc3089447a370f61ee227f08d978937e4b0c97bea642bb2e73b47c0795` |
| EXE VersionInfo | FileDescription/ProductName=MediaDownloader · FileVersion=1.0.0.0 · ProductVersion=1.0.0 · OriginalFilename=MediaDownloader.exe · CompanyName/版权留空（**Publisher 待用户确认**） |
| Icon | `app_icon.ico`（PIL 程序生成，项目自有、版权清晰） |
| EXE SHA256 | `1a5a1eeac752a3ed56a29ad4e10b947840a2c734e240eead3872b6d666ec0396`（18,472,313 B） |
| 产物总大小 | `dist\MediaDownloader\` 265 MB（大头为 Playwright 驱动 node.exe + ffmpeg） |
| Code signing | **pending**（未签名，已知事实；正式发布前决定） |
| 敏感扫描 | 文件名 13 类 + 文本内容 7 类模式，真实命中 **0**（唯一 "admin" 命中为 Playwright 驱动自带文档示例字符串，非本机用户名） |

## 关键依赖（锁定版本 = 已验证基线）

fastapi 0.141.1 · uvicorn 0.52.4 · requests 2.34.2 · beautifulsoup4 4.15.0 ·
lxml 6.1.1 · pillow 12.3.0 · **yt-dlp[default] 2026.7.4**（runtime：yt-dlp-ejs 0.8.0 /
websockets 17.1 / brotli 1.2.0 / mutagen 1.48.1 / pycryptodomex 3.23.0 / certifi 2026.7.22）·
pydantic 2.13.4 · playwright 1.62.0（pyee 13.0.1 / greenlet 3.5.6）·
gallery_dl 1.32.13 · ddgs 9.16.0

`curl_cffi` 继续不装：yt-dlp 可选 extra（非 default），HTTP(S) 请求统一走
requests/urllib3 handler，当前功能不需要；warn 文件中的 missing 属预期 optional。

## 二进制与外部资源

| 项 | 值 | 策略 |
|---|---|---|
| ffmpeg | 7.1-essentials_build-www.gyan.dev，SHA256 `2ce797a0f88d7f067180338fb227f7b1928ea727bd9a4d7a1d022f7c52af71a3`（87,638,016 B），**GPLv3**（configure 含 --enable-gpl --enable-version3） | spec 自带 `bin/ffmpeg.exe`；回退 FFMPEG_PATH > PATH。分发合规见 `FFMPEG_DISTRIBUTION_NOTICES.md` → `licenses\FFmpeg\COPYING.GPLv3` |
| Playwright 浏览器 | **不打包浏览器** | 复用用户系统 Chrome/Edge；驱动运行时随包（含 node.exe） |
| JS runtime | **复用 Playwright bundled Node** | frozen 下 `find_js_runtime()` 最高优先返回 `_internal\playwright\driver\node.exe`（**v24.18.1**，实测命中）；其次用户 PATH / Node 官方位置；frozen 排除 WorkBuddy node。缺失时 `/api/status`+UI 如实降级 |
| MediaCrawler | **不进 EXE** | Desktop 可选外部依赖；未配置时优雅降级 |

## spec 排除（从未进入产物，白名单制收集）

downloads/ · data/ · config.json · cookies · Browser Profile · login_state ·
.workbuddy/ · .wbapp_* · .playwright-mcp/ · .pytest_cache/ · `_*.log/txt/png` ·
probe/diag 产物 · build-probe/ · release-secrets · MediaCrawler 全项目 ·
.venv-build/ · dist-archive/ · _pylibs/（源码模式保留 fallback，EXE 不带）

## 构建期实证教训（累计）

1. `app.main` 仅被 uvicorn 以 `"app.main:app"` 字符串引用 → 必须
   `collect_submodules("app")`（1E）。
2. **RC2 首构建**：`win_job.reap_orphan_ffmpeg` 用 `text=True` 读 PowerShell
   （Get-CimInstance 全系统进程表，输出含中文进程名 GBK 字节）→ utf-8 读取线程
   UnicodeDecodeError 死亡 → communicate() 永久挂起 → startup 事件卡死 →
   服务永不就绪。修复：显式 `encoding="mbcs", errors="replace"`。
   ⚠️ frozen windowed 下任何 `subprocess.run(..., text=True)` 读外部命令输出
   都必须显式指定编码。
3. yt-dlp 必须带 `[default]` extra，否则 frozen 缺 yt_dlp_ejs → YouTube JS 挑战无解（1E.2）。

## RC2 Frozen 验证记录

完整 22 项见 `.workbuddy/memory/2026-10-01.md` 阶段 1E.2 节。要点：

- 双击式（DETACHED）启动 t≈25s 就绪 · 自动端口 8848 · health/静态资源 ✓
- **YouTube metadata+formats+mediaURL ok:true**（Me at the zoo, jNQXAC9IVRw）✓
- **YouTube 实际下载 474KB webm 落盘成功**（任务 status=completed）✓
- 全程日志 0 条 "No supported JavaScript runtime"（EJS+bundled Node 生效）✓
- yt_dlp_ejs/websockets/mutagen/Cryptodome/app.main 全部在 exe 内嵌 PYZ ✓
- ejs 求解器数据（core.min.js/lib.min.js）在 `_internal\yt_dlp_ejs\` ✓
- exe 目录启动前后零新增文件 ✓
- ⚠️ 单次出现过 YouTube bot-check（Sign in to confirm you're not a bot）——
  IP 信誉波动，非打包问题；cookies 预检按设计降级。干净机器 YouTube 验收
  列为阶段 6 强制项。
