# BUILD_MANIFEST — MediaDownloader Release Candidate

> 每次正式发布前更新本文件。构建必须用隔离 venv，禁止裸 python/pip。

## Installer RC（1F，2026-10-01/02）

| 项 | 值 |
|---|---|
| 安装器脚本 | `installer\MediaDownloader.iss`（Inno 只装 `dist\MediaDownloader\*`，不弥补 RC 缺文件） |
| Inno Setup | **6.7.3**（`C:\Program Files (x86)\Inno Setup 6\ISCC.exe`，本机已有，未重装） |
| 固定 AppId | `{3A40E79A-76F4-4EEA-B328-5D122EFE7F5F}`（生成一次后固化；**升级版本必须沿用，禁止重新生成**） |
| Installer 文件名 | `MediaDownloader-Setup-1.0.0-RC.exe` |
| 外发测试副本 | `D:\\MediaDownloader-Setup-1.0.0-RC2.exe`（与当前 Installer 内容一致） |
| Installer 大小 | 84,904,801 B（≈81.0 MB；onedir 277,811,783 B，Compression=lzma2 + SolidCompression=yes，无 UPX/无裁剪） |
| Installer SHA256 | `f1b1a15761c5588989d37ddfd5613c0246611ba170de337e33f5c076869f0d08` |
| 权限/安装位置 | `PrivilegesRequired=admin`；`ArchitecturesInstallIn64BitMode=x64compatible`（**必须**，否则 {autopf} 落到 Program Files (x86)——首构建实测踩坑已卸载重装）；默认 `{autopf}\MediaDownloader` = `C:\Program Files\MediaDownloader` |
| 快捷方式 | 开始菜单始终创建；桌面图标 = 可选任务 `desktopicon` **默认不勾选**；无自启/计划任务/Run 注册表 |
| 运行中升级 | `CloseApplications=yes`（Restart Manager 标准机制检测占用并提示关闭；无 taskkill）。实测：应用运行中覆盖安装，Restart Manager 干净关闭实例，安装器退出码 0 |
| 覆盖安装 | `[InstallDelete]` 仅 `{app}\*`（旧程序文件清理，运行数据已不在 {app}，安全）。实测：config.json sha256/mtime 逐字节不变、卸载项唯一、383 文件完整 |
| 卸载行为 | 默认只删 `{app}`；**绝不删** `%LOCALAPPDATA%\MediaDownloader` 与 `%USERPROFILE%\Downloads\MediaDownloader`。实测 7 项数据保留全过；重装后原 config 直接可读。⚠️ 已知外观级残留：卸载后偶余**空目录壳**（0 文件，非提权 shell 删不掉，无功能影响） |
| 敏感扫描 | 安装后目录 + RC2 dist 全量内容扫描 8 类关键词，命中 **0**（"admin" 既有误报同 RC2 记录） |
| Code signing | **pending**（安装器未签名，交互安装会出现 Unknown Publisher，属当前预期） |
| Publisher | **pending**（AppPublisher 故意留空，正式发布前由用户定案） |
| 项目 License | **pending**（不设 LicenseFile；`LICENSE-TODO.md` 保留；第三方声明随包：THIRD_PARTY_NOTICES.txt + licenses\ + FFMPEG_DISTRIBUTION_NOTES.md） |
| 安装验收 | 安装后核心 25 项全过（脚本 `_test_1f.py`，含 YouTube metadata + 474KB 实际下载、login-browser、bundled Node、ffmpeg、gallery-dl 1.32.13、PF 零运行时写入）；卸载硬门禁 7/7 |

## Instagram / 小红书修复 RC（2026-10-03）

| 项 | 结果 |
|---|---|
| Instagram 批量 | Frozen 实测主页 `lis_manyonok`：21 秒返回 3 条 Reel，条目封面存在；gallery-dl + 登录 cookies |
| Instagram 单条 | Frozen/安装版实测 `C5WtxeLIS1R`：`ok=true`、5 个格式、thumbnail=true；实际下载 19,712,828 B MP4 |
| 小红书批量 | Frozen 实测主页：3.5 秒返回 4 条视频，条目封面存在；xhshow 主路径，MediaCrawler 登录态回退 |
| 小红书单条 | Frozen/安装版实测：`ok=true`、thumbnail=true、`XHS/xhshow`；实际下载 3,858,365 B MP4 |
| 其它平台回归 | `test_profile_tools` 59/59、cookies 20/20、video search 116/116；quality/download fallback/variant/render gate 全 PASS |
| Frozen 修复 | spec 改为直接枚举 `app/*.py` hidden imports，解决 `app.main` 未进入 PYZ 的发布级问题 |
| EXE | SHA256 `f8e72aca8f3167f0933ffd6700e21e8e147dea1802c9b3d9e4e93bfb0edb1aea`，18,822,212 B |
| 依赖 lock | 42 项；SHA256 `ea94bcefdf613fea716366ea101a82e7425b23b6be84dd80a1b3cc594c9b2f1a` |
| Installer | 84,904,801 B；SHA256 `f1b1a15761c5588989d37ddfd5613c0246611ba170de337e33f5c076869f0d08` |
| 本机升级 | `D:\MediaDownloader` 覆盖安装 exit=0；config SHA256 不变；原 23 个下载文件保留；安装 EXE 与 RC 哈希一致 |

## RC2（1E.2，2026-10-01）

| 项 | 值 |
|---|---|
| RC2 git commit（release-work） | 见 `git log --oneline -1`（1E.2 提交；RC1 存档于 `dist-archive\RC1-0aaf468\`） |
| 基线参照 | `media-downloader` @ `fb573e0`（只读，全程零改动） |
| 构建解释器 | `.venv-build\Scripts\python.exe`（源自 `C:\Python314\python.exe` 3.14.6） |
| PyInstaller | 6.22.2（onedir + windowed；排错版 `MD_SPEC_CONSOLE=1` 同 spec） |
| spec | `MediaDownloader.spec`（入口 `launcher.py`，SPECPATH 计算路径；含 version resource + icon） |
| 构建命令 | `.venv-build\Scripts\python.exe -m PyInstaller --noconfirm MediaDownloader.spec`<br>后处理：复制 `licenses\`、`THIRD_PARTY_NOTICES.txt`、`FFMPEG_DISTRIBUTION_NOTES.md` 到 `dist\MediaDownloader\` 根（1F 起ffmpeg notes 纳入根目录清单；此前只在 `_internal\licenses\`，已补齐——安装包输入源=RC2 整体，Inno 不单独补文件） |
| 依赖 lock | `requirements.lock.txt`（42 项），SHA256 `ea94bcefdf613fea716366ea101a82e7425b23b6be84dd80a1b3cc594c9b2f1a` |
| EXE VersionInfo | FileDescription/ProductName=MediaDownloader · FileVersion=1.0.0.0 · ProductVersion=1.0.0 · OriginalFilename=MediaDownloader.exe · CompanyName/版权留空（**Publisher 待用户确认**） |
| Icon | `app_icon.ico`（PIL 程序生成，项目自有、版权清晰） |
| EXE SHA256 | `20e84f7d3f8d3da77994412f59843e20cbb4613c0f375d7e1da19ecd6c14d52a`（18,822,212 B） |
| 产物总大小 | `dist\MediaDownloader\` 278,204,309 B（约 265.3 MiB；大头为 Playwright 驱动 node.exe + ffmpeg） |
| Code signing | **pending**（未签名，已知事实；正式发布前决定） |
| 敏感扫描 | 文件名 13 类 + 文本内容 7 类模式，真实命中 **0**（唯一 "admin" 命中为 Playwright 驱动自带文档示例字符串，非本机用户名） |

## 关键依赖（锁定版本 = 已验证基线）

fastapi 0.141.1 · uvicorn 0.52.4 · requests 2.34.2 · beautifulsoup4 4.15.0 ·
lxml 6.1.1 · pillow 12.3.0 · **yt-dlp[default] 2026.7.4**（runtime：yt-dlp-ejs 0.8.0 /
websockets 17.1 / brotli 1.2.0 / mutagen 1.48.1 / pycryptodomex 3.23.0 / certifi 2026.7.22）·
pydantic 2.13.4 · playwright 1.62.0（pyee 13.0.1 / greenlet 3.5.6）·
gallery_dl 1.32.13 · ddgs 9.16.0 · **xhshow 0.2.0**（XHS 签名）· pycryptodome 3.23.0

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

1. `app.main` 仅被 uvicorn 以 `"app.main:app"` 字符串引用。**不能只依赖**
   `collect_submodules("app")`：spec 评估阶段的 `sys.path` 与 `Analysis.pathex`
   生效阶段不同，2026-10-03 实测会导致 `app.main` 根本不进 PYZ。
   当前 spec 直接枚举项目 `app/*.py` 生成 hidden imports；Frozen 启动已复验 PASS。
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
