# BUILD_MANIFEST — MediaDownloader Release Candidate

> 每次正式发布前更新本文件。构建必须用隔离 venv，禁止裸 python/pip。
>
> ⚠️ **2026-11-03 历史改写**：为把开发期的一次性脚本与内部记录从公开仓库里彻底
> 移除，整个历史被重写（文件内容未变，提交 SHA 全部改变）。因此**本节之前各版本
> 段落里引用的 commit SHA 已失效**，按 SHA 找不到属正常；查证请按提交标题搜。
> 改写前完整备份：`D:\Project\_md_repo_backup_before_rewrite.bundle`。

## V3.0.3（2026-11-03）

| 项 | 值 |
|---|---|
| 版本号 | `app/config.py` `APP_VERSION = "3.0.3"` |
| 源码分支 | `v3.0-work` @ `99d2eb3`（定版提交 `404dbe4`） |
| 构建方式 | `.venv-build` 隔离 venv + PyInstaller（MediaDownloader.spec）+ Inno Setup 6 |
| 安装包 | `installer_out/MediaDownloader_V3.0.3_Setup.exe` — 241.2 MB，构建于 2026-11-03 22:08 |
| SHA256 | `189D59306E57497383ED0087A4DB55BAC81BFCCC3CCED3B71276DEE0B4321DB1` |

### V3.0.2 → V3.0.3 变更清单

| 提交 | 内容 |
|---|---|
| `63a55d5` | **白屏根因修复**。V3.0.2 那轮「内核自检」判据 `evaluate_js('1+1') == 2` 是无效的：IE11 下 `1+1` 正常返回 2（只用到 eval + JSON.stringify），自检恒真 → 降级到 IE 依然白屏。改用 **UA 指纹 + ES6 语法 + 前端就绪标记 `__MD_APP_READY__`** 三重判据；所有 evaluate_js 探测加超时（pywebview 内部 Semaphore 无超时，内核半死会永久阻塞自检线程）；开窗前按「文件系统 + 注册表」快速判死，没装 WebView2 就不给白窗；转浏览器时弹窗写明原因/URL/下载地址；`index.html` 加纯 HTML/CSS 内核提示块（JS 跑不起来时 1.6s 后浮现）；frozen 版自动生成「以浏览器模式启动.cmd」；新增 `diag_kernel.py` |
| `a28873d` | 修掉两个长期红灯的陈旧用例（`test_desktop_close` 托盘异常预期写反、`test_data_migration` 硬编码 3.0.0），回归清单纳入新用例 |
| `404dbe4` | 定版 3.0.3；`version_info.txt` 从 3.0.0 跟进到 3.0.3（此前 exe 文件属性与安装包版本对不上） |
| `99d2eb3` | 修 `构建V3安装包.bat` 第 5 步写死旧包名导致每次定版都「复制失败」 |
| `0563305` | 修 `构建V3安装包.bat` **双击跑不起来**：① 老 bug —— `%ISCC%` 在 `if (...)` 块里展开，值中的 `(x86)` 把块提前闭合，报 `\Inno was unexpected at this time`（该脚本此前从未跑完整过）；② 上一版误存成 UTF-8+LF，cmd 解析错行、exit 255 零输出。现统一 GBK+CRLF + 延迟展开 `!VAR!` + 通配符复制包 |
| `d313ead` | 开源：定 MIT 许可证（署名 Illumi Tools）、补 LICENSE/DECISIONS D-010/README 三节 |
| `317f07d`+`7176ef0` | 开源瘦身：179 个开发过程文件移出仓库（本地保留），跟踪文件 390 → 211 |
| 本轮新增 | **安装包打错源目录**：iss 的 `SrcDir` 是 `..\dist\MediaDownloader`（本地在跑的绿色版），而 bat 把新包构建到 `dist_new` → 打出「Setup 版本号 3.0.3、内容却是 3.0.0」的假包。实测：`dist\...\MediaDownloader.exe` FileVer=3.0.0.0、`app.js` 无 `__MD_APP_READY__`。改为 `SrcDir = ..\dist_new\MediaDownloader`，安装器永远打刚构建的那份 |


### 验证记录（本机）

- `gui='mshtml'`：旧判据 `1+1==2` 通过（证明旧自检无效）；新判据 `('legacy','Trident/7.0; rv:11.0')`；`diag_kernel.py --gui mshtml` 退出码 2。
- `gui='edgechromium'` 连真实页面：`('ok','Edg/156.0.0.0')` + 前端就绪 True。
- 真实 `launcher.py` 启动：`desktop_webview.log` 出现 `Using custom WebView2 runtime`，`desktop_shell.log` 出现「内核自检通过」「前端自检通过」，`msedgewebview2.exe` 子进程存在、`EBWebView` 生成。
- 前端未就绪场景（指向 JSON 接口）：19s 内按 15s 期限判失败，未卡死。
- Playwright 双向验证 `#kernelWarn`：JS 开启时节点被移除；JS 关闭时 `visibility=visible / opacity=1`（截图人工确认）。
- 回归：`run_regress.py` 28/29 PASS，唯一失败的 `test_data_migration` 已由 `a28873d` 修好并复跑通过。
- **打包版实跑（决定性证据）**：`dist_new\MediaDownloader\MediaDownloader.exe` 启动后
  `desktop_shell.log` 依次出现「已生成浏览器模式启动器」→「WebView2 运行时（文件系统探测）」
  →「WebView2Loader 目录已前置到 PATH」→「内核自检通过（Chromium）Edg/156」
  →「前端自检通过（__MD_APP_READY__ 已置位）」，退出后无残留进程。
  这些日志**只有 3.0.3 才有**，可确认包内是修复后的代码。
- 安装包与载荷时序核对：载荷 exe（22:05:47，FileVer 3.0.3.0）早于 Setup 生成（22:08:23）。
- **仍未覆盖**：故障机（注册表被清理的那台 Win10）无法远程复现，需在真机装 3.0.3 跑 `diag_kernel.py` 确认；
  安装包未在本机做真实安装（会覆盖用户正在用的 3.0.2），最终以目标机安装为准。

## V3.0.2（2026-11-01）

| 项 | 值 |
|---|---|
| 版本号 | `app/config.py` `APP_VERSION = "3.0.2"` |
| 源码分支 | `v3.0-work` @ 7bb8776 + 定版提交（V3.0.2） |
| 构建方式 | `.venv-build` 隔离 venv + PyInstaller（MediaDownloader.spec） |

### V3.0.1 → V3.0.2 变更清单（4 项功能修复 + 1 项性能修复，来自 worktree main-2189c84a 分支）

| 日期 | 来源提交 | 内容 |
|---|---|---|
| 10-29 | `dbb4eec` | pornhao「能抓不能下」：① 无 yt-dlp 提取器站点下钻详情页取直链（旧版给视频页地址 → yt-dlp Unsupported URL 全灭）；② 签名直链过期自动回源刷新；③ 大文件 4MB 切片多连接并发下载（实测 9KB/s → 499KB/s）；④ 任务进度按字节实时计算（旧版 192MB 片几十分钟 0%） |
| 10-30 | `166d0ce` | 下钻直链可信度校验：广告位尺寸命名 / preview 等预览词 / 跨页同 URL 三条通用判据，全部不可信回退视频页 —— hornyleak「6 条全是同一广告文件」根因 |
| 10-30 | `7b4cf62` | 新增「txt 分片流」下载：正片藏在「详情页→embed iframe→第三方播放器→JS 解密分片列表」三层最里层的站（hornyleak），伪 txt 递增编号分片在**同一浏览器会话**内逐片取回拼接，ffmpeg 转封装 MP4；端到端实测 24:28 正片 |
| 10-31 | `87165ba`/`c02fb41` | 取消要真的停下（分段/单连接每块检查 + 档位回退短路 + 取消不再假显示 100%）；桌面壳 WebView2 三道防线（文件系统探测运行时绕开坏注册表 + JS 引擎自检失败转浏览器 + pywebview 日志落盘）—— 故障报告《WebView2内核降级》P0/P1 |
| 11-01 | `7bb8776` | /api/status 冷启动首屏 >8s → startup 后台预热（故障报告 P2） |
| 11-01 | `45291d7` | 去重失效修复：签名直链每次抓取全变（st/e/host/档位），URL 级去重形同虚设 → 台账加 mediaid:主域:站内数字ID 稳定身份键 + 跳过链接入台账 + 存量台账迁移（用户重抓重下不再重复下载） |

### 验证记录

- 打包版 hornyleak.tv 首页抓取：4 条全部为视频页地址（广告位/预览片段被识别剔除），警告如实说明；
- 打包版 pornhao 大文件批量下载：分段并发 ~400KB/s，进度实时；24 条中 2 条正片完整落盘（229/244MB）；
- 打包版桌面壳日志：`desktop_shell.log` 每次启动记录「WebView2 运行时（文件系统探测）」与「渲染内核自检通过」；`desktop_webview.log` 记录内核选择全过程；
- WebView2 白屏的最终验收需在故障机（注册表被清理的那台 Win10）装 V3.0.2 实测：正常应直接出界面；若该机真无 WebView2，应自动转浏览器而非白屏。
- 回归：worktree 侧 run_regress.py 26 项通过 / 2 项环境性失败（test_browser_skill 缺 bsk 产物、test_extractor 缺测试数据，与代码无关）。

## V3.0.1（2026-11-01）

| 项 | 值 |
|---|---|
| 版本号 | `app/config.py` `APP_VERSION = "3.0.1"` |
| 源码分支 | `v3.0-work` @ 9e2035e + 定版提交（V3.0.1） |
| 构建方式 | `.venv-build` 隔离 venv + PyInstaller（MediaDownloader.spec），补齐 msvcp140 系列运行库 |

### V3.0.0 → V3.0.1 变更清单（全部为修复，无新功能）

| 日期 | 提交 | 内容 |
|---|---|---|
| 10-31 | `ca89931` | 小红书：主页抓取遇 code=-100「登录已过期」不识别、不提示重登，兜底还拿登录墙演示视频充数 → -100/-101 同等处理 + 取消 browser 兜底 |
| 10-31 | `f494c61` | 全平台横向排查：登录失效时兜底链吞掉重登提示（IG/抖音/B站/知乎/TikTok/Pinterest）；gallery-dl / 抖音验证码检测到登录错误不标记失效 → 统一 `_relogin_why` 快速失败 + mark_invalid |
| 11-01 | `3cea3ab` | 小红书：批量链路视频模式恒 0——类型过滤把未分类的笔记页条目全丢（下钻前执行）→ 放行 is_page 条目交给下钻分类 |
| 11-01 | `080e8ed` | 登录提示规则补严：已登录 + 页面打开 + 0 作品（无"登录"字样的登录墙形态）→ 标记失效并明确提示重登（douyin + browser_items 两处） |
| 11-01 | `9e2035e` | 小红书：下钻被频控拦截的页面误判成「图文笔记」（视频越抓越少还不自知）→ 并发 6→3、按特征词区分图文/拦截、拦截自动降速补抓、计数如实拆分并提示可能不完整 |

### 已知事项
- 平台登录态过期属正常现象（服务端主动过期），程序现在会明确提示到「平台登录中心」重登；
- 同一主页连续快速重抓会触发平台频控，程序会提示等几分钟再抓，首次抓取结果最全；
- 本机 DNS 曾被 TAP 虚拟网卡（VPN 残留，DNS=8.8.8.8）间歇搅乱导致抓取全 0，属环境问题与程序无关。

## RC3 Final（2026-10-04）

| 项 | 值 |
|---|---|
| 最终源码 HEAD | `48939bc` |
| 桌面启动 | 默认 pywebview + WebView2 独立窗口；失败回退浏览器；`--browser` 可强制浏览器模式 |
| EXE | 22,939,630 B；SHA256 `ED2569590AD7E8568EF4D5A0232913ED8C0BD0D9F2D76E8DF6C54BF8BFDCB710` |
| dist | 421 files；289,472,762 B |
| Installer | `MediaDownloader-Setup-1.0.0-RC3.exe`；91,323,074 B；SHA256 `4841D34F32B326F03121C6037CF3A55F872A24DE41E7ADC47844BDB89C1AADD6` |
| 外发测试副本 | `D:\\MediaDownloader-Setup-1.0.0-RC3.exe` |
| requirements.lock | 54 项；SHA256 `27947EE8A7FD6681B13BEEED090C6810132ADCFA7B177B769F72EDC4E35F82FD` |
| 关键新依赖 | yt-dlp 2026.08.19 · curl-cffi 0.15.0 · pywebview 6.2.1 |
| 使用说明 | 软件内“使用说明”页 + 安装目录根部 `使用说明.md` |
| 图片安装版 QA | 12 PASS / 0 FAIL（6 种搜图条件，全部实际下载） |
| 主流视频安装版 QA | 32 PASS / 6 BLOCKED / 0 FAIL（严格平台域名；能解析者真实下载） |
| 批量/成人/非主流 QA | 10 PASS / 0 FAIL |
| 退出 | 关闭窗口后进程 0、本地 HTTP 服务同步退出 |
| 覆盖安装 | exit=0；config SHA256 不变；安装 EXE 与 dist EXE SHA256 一致 |
| 卸载 | exit=0；程序文件删除；LOCALAPPDATA 配置和 Downloads 数据均保留 |
| 重装 | exit=0；config 继续复用；抖音/小红书登录态解析再次 PASS |
| 完整 QA 记录 | `RELEASE_QA_RC3.md` |
| Code signing / Publisher / 项目 License | pending |

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
lxml 6.1.1 · pillow 12.3.0 · **yt-dlp[default,curl-cffi] 2026.8.19**（runtime：yt-dlp-ejs 0.8.0 /
websockets 17.1 / brotli 1.2.0 / mutagen 1.48.1 / pycryptodomex 3.23.0 / certifi 2026.7.22 /
**curl-cffi 0.15.0**）· pydantic 2.13.4 · playwright 1.62.0（pyee 13.0.1 / greenlet 3.5.6）·
**pywebview 6.2.1**（pythonnet/clr_loader/cffi）· gallery_dl 1.32.13 · ddgs 9.16.0 ·
**xhshow 0.2.0**（XHS 签名）· pycryptodome 3.23.0

RC3 已启用 curl-cffi 浏览器 impersonation，实测用于修复 TikTok/Dailymotion 等站点变化；
相关依赖已锁定并进入 Frozen 包。

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
