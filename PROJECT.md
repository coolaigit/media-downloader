# PROJECT — Media Downloader V3.0

## 1. 仓库与基线

| 角色 | 路径 | 状态 |
|---|---|---|
| 正式旧版源码 | `D:\QWen Project\media-downloader` | **只读**，任何原因都不得修改 |
| V2.5 工作副本 | `D:\QWen Project\media-downloader-2.5-work` | **冻结**（`89a542c`），V3.0 开发期间不再修改 |
| V3.0 工作副本 | `D:\QWen Project\media-downloader-3.0-work` | 当前开发目录，分支 `v3.0-work` |
| V2.5 最终安装包 | `D:\MediaDownloader-Setup-2.5.0-Final.exe` | 参照物 |

V3.0 副本由 `git clone` 自 V2.5 工作副本得到，**保留完整历史**（可 diff / 可回溯）。
副本不带入任何未跟踪文件（`dist/` `build/` `installer/output/` `downloads/` `data/` 全在 `.gitignore` 里），
只额外补了运行时必需的 `bin/ffmpeg.exe`（87 MB，不入库）。

## 2. 代码结构（V2.5 实测）

```
app/                       约 1.9 MB，33 个模块
  main.py          55 KB   FastAPI 路由层（所有 /api/*）
  batch_crawler.py 127 KB  批量抓取核心（列表/详情/媒体解析）
  profile_sources.py 77 KB 各平台主页作品列表
  video_downloader.py 82 KB 视频页面解析
  extractor.py     75 KB   通用字段/媒体提取
  task_manager.py  39 KB   任务队列、并发闸门、Cookie 快照
  downloader.py    25 KB   图片/文件下载 + 重试 + 记史
  stream_downloader.py 23 KB HLS/直链流下载（ffmpeg 链路）
  history.py       13 KB   跨任务去重台账
  browser_skill.py  5 KB   bsk 桥（外部命令，V3.0 要内置）
  browser_profile.py 6 KB  独立浏览器 Profile
  desktop_shell.py  8 KB   pywebview 桌面壳 + pystray 托盘
web/
  app.js          104 KB   2552 行
  index.html       52 KB   1020 行
  style.css        30 KB    811 行
```

## 3. 并发控制（V3.0 Phase 2 已完成）

**V2.5（已废弃）**：六处全是静态参数，降下去回不来，且不区分平台/类型。

**V3.0 现状**：全部收敛到 `app/concurrency.py` 的 `GOVERNOR`，
按 `(平台 × 任务类型)` 各立一个自适应闸门；用户设置的数字一律降级为**上限**。

| 调用点 | V2.5 | V3.0 |
|---|---|---|
| 视频下载 `task_manager._run_video` | 全局 `_VIDEO_GATE` 3 路 | 按平台闸门（`video_download`） |
| 抖音 ≥100 条 `main.py` | 硬砍 `min(2)` | 删除；抖音闸门本来起步 2 路，撞 412 继续降、稳定后升回上限 |
| B站元数据/缩略图 `profile_sources._bili_enrich` | 硬限 4 路 | 起步 4 路（有 Cookie）/ 1 路（匿名），稳定后升到上限 6 |
| 图片下载 `downloader.batch_download_images` | `max_workers=N` 固定 | `N` 为上限，实际名额由闸门控制 |
| 流下载 `task_manager._run_streams` | 1–8 固定 | 同上，按平台闸门（`stream_download`） |
| 列表抓取 `batch_crawler.fetch_many` | `concurrency*2` 封顶 16 | 为上限，详情页下钻先拿名额 |

**机制**：降速立即（412/429/验证码减半，连续就一路降到底）；恢复谨慎（冷却期 20s + 连续 N 次成功才翻倍）；
普通 404/代理抖动**不降速**；降速不杀掉在途请求；取消能打断排队。

**可观测**：`GET /api/concurrency` + 任务面板头部状态条
（`平台·类型 2/8 平台限流`，hover 给出「用户最大并发 / 当前实际 / 原因 / 连续稳定后自动恢复」）。

**实测**（本地 412 服务）：8 → 4 → 2 → 1 自动降；风控解除后 8 次成功一轮，1 → 2 → 4 → 8 自动恢复。

## 4. 下载失败明细（V3.0 Phase 1.5 已完成）

- 后端**已记录**每项明细：图片链路 `task_manager.py:584`、流媒体链路 `task_manager.py:856`，
  字段 `{index, url, ok, skipped, name, size, error}`。
- `Task.public()` 回传 `items[-200:]`（只留最近 200 条，大批量会丢早期明细）。
- 前端 `pollTasks()`（`web/app.js:1968`）**只渲染** `成功 X · 跳过 Y · 失败 Z · 共 N`，
  **完全不渲染 items**，也没有重试入口。
- 去重有两层：`downloader.download_file(skip_existing=True)` 文件级标记 + `history.py` 跨任务台账。

→ 用户「不知道哪几个失败」的根因是**前端没展示已存在的数据**，不是后端缺数据。

## 5. 必须保留的 V2.5 能力（回归红线）

### B站
批量 30 条正常 · 30/30 缩略图 · CDN 封面直连优先 · 本地 proxy 兜底 · view API 退避重试 · 打开/来源页正常

### 微博
批量 30 视频正确返回 · 图片预览 · 视频稳定下载 · 不依赖易过期签名 CDN · 原页面重新解析真实下载地址

### 抖音
主页持续滚动（不能只抓首屏预加载）· 正常抓取由程序自身完成 · 验证码才升级 BrowserSkill ·
每任务 Cookie 快照 · 主 Cookie 原子写 · Fresh cookies / 412 刷新重试

### YouTube
单视频解析 · 批量下载 · 清晰度解析与选择

### 下载系统
ffmpeg 内置 · 音视频合并 · 下载历史 · 已下载去重 · 新任务列表刷新

### 外链统一
批量结果「打开」/「来源页」、单点视频「打开原页面」、已下载记录「来源页」
→ Windows 桌面版统一 `ShellExecuteW`，**不依赖** `target="_blank"` / `window.open`（WebView2 不稳）

### 系统托盘（V2.5 Final 专项修复，V3.0 不得回归）
构建环境真实装 `pystray` · PyInstaller 真实打包 · 托盘初始化失败**禁止**自动 fallback 到 exit ·
用户选 tray 时即使托盘异常也**取消关闭**而不是退出 ·
frozen EXE 真实 WM_CLOSE 测试：进程存活 + 主窗口隐藏 + 托盘可恢复

### 关键词搜图
Bing / DuckDuckGo / Google + SafeSearch / 分辨率 / 大小 / 格式 / 颜色 / 时间 / 横竖比 / 类型 / 原图 / 来源页。
正常搜图后台运行 · 单引擎失败不拖死全局 · 错误信息能说明具体来源。
**没有复现的问题不要顺手重构。**

## 6. 浏览器弹窗规则（V3.0 统一）

- 正常操作一律后台：搜图、搜视频、批量抓取、滚动、渲染、Cookie 刷新、元数据补全、普通解析。
- **禁止**为图省事用 `headless=False`。
- 只有真需要人来时才显示：验证码、滑块、登录失效、BrowserSkill 请求协助、平台明确要求。
- BrowserSkill 只是兜底，**不能成为普通抓取的必需链路**。

## 7. 浏览器内核（V3.0 Phase 3）

**V2.5**：复用用户电脑上的 Chrome / Edge。用户只装 360 / QQ 浏览器，或者一个都没有时，
所有需要 JS 渲染的站点直接不可用 —— 而且「我这能抓、他那不能抓」这种问题只有换台机器才复现。

**V3.0**：安装包自带 Playwright 官方 Chromium。

| 优先级 | 来源 | 判定 |
|---|---|---|
| 1 | `browsers/chromium-*/chrome-win64/chrome.exe` | `browser_runtime.pick()["kind"] == "bundled"` |
| 2 | 系统 Chrome | `kind == "system"`，`channel="chrome"` |
| 3 | 系统 Edge | `kind == "system"`，`channel="msedge"` |
| 4 | 都没有 | `kind == "none"` + 一段能照做的中文提示 |

**只带完整 Chromium，不带 `chromium_headless_shell`**（省 272 MB）。
实测四种启动方式（headless / headed / 指定 exe / persistent context）全部可用，
指定 `executable_path` 反而更快（1.4s vs 2.8s）。

⚠️ **代价**：没有 headless shell 之后，`chromium.launch(headless=True)` 不带路径会直接报
`Executable doesn't exist`。所以只要包内有 Chromium，**必须**传 `executable_path`——
统一走 `browser_runtime.launch_kwargs()`，不要在调用点自己拼 channel。

`browsers/` 不入库（`.gitignore`）。新机器/新 clone 要补下载：

```bat
set PLAYWRIGHT_BROWSERS_PATH=%CD%\browsers
python -m playwright install chromium
:: 装完删掉这三样可省 275MB，程序会用完整内核跑 headless
rmdir /s /q browsers\chromium_headless_shell-*  browsers\ffmpeg-*  browsers\winldd-*
```

## 8. BrowserSkill（V3.0 Phase 4）

**定位**：只在「撞上验证码 / 滑块 / 安全验证」时升级，**绝不进普通抓取链路**。
日常抓取走 HTTP 或内置 Chromium（headless），全程无窗口。

**三件资产全部随包**：

| 资产 | 位置 | 说明 |
|---|---|---|
| bsk 命令行 + daemon | `bin/bsk.exe` | v0.3.3，17 MB（同 ffmpeg 策略：不入库） |
| 浏览器扩展 | `bsk-extension/` | MV3 v0.3.3，3.3 MB，**入库** |
| 承载扩展的内核 | `browsers/chromium-*` | Phase 3 |

**为什么能自包含**：扩展 manifest 里带 `key`，`--load-extension` 加载后扩展 ID 保持
`hhcmgoofomhgciiibhipgmgkgnoenaoi` 不变，daemon 靠这个 ID + 协议 1.3 配对。
实测 headless / 有头两种模式都能加载，启动到 daemon 可见约 1.8 秒。

**升级链路**：
`检测到墙 → ensure_ready(headless=False) → start_page(browser_id=…) → get_html →
（必要时）click_simple_verification → request_human_help → 拿到正常页面 → 继续原任务`

`ensure_browser()` 的取舍：daemon 里已有浏览器就直接用（用户自己的 Chrome），
一个都没有才用内置 Chromium + 内置扩展起一个；收尾只 terminate 自己 Popen 的 pid。

⚠️ **扩展带下划线开头的 chunk**（`chunks/_virtual_wxt-*.js`）曾被 `.gitignore` 的
`_*` 规则吃掉，已在 `.gitignore` 里加了 `!bsk-extension/**/_*` 例外。删这个例外会导致
打包出来的扩展缺文件。

## 9. Google 搜图与会员视频兜底（2026-11）

### 9.1 Google 搜图：两条通道

纯 HTTP 路线已被 Google 关闭（`enablejs` 空壳 / `gbv=1` 报浏览器不支持 /
`images/async` 404）；机房 IP 渲染会 302 到 `/sorry`。官方 Custom Search
JSON API 2026 年起对新客户关闭，无法再申请 key，所以不再提供 API 配置。
`app/search_google.py` 保留两通道自动降级，完整实测记录见
`说明-Google接入与会员换源.md`。

| 通道 | 入口 | 依赖 |
|---|---|---|
| `render` | `search_google_render()` | playwright 内核；可从 `google_proxy` 单独走代理 |
| `searx` | `search_google_searx()` | `google_searx` 实例列表（留空不启用） |

- 渲染通道从 DOM 的 `a[href*="imgurl="]` 取原图 + 来源页，比正则扫 HTML 稳。
- `renderer.render_page()` 新增 `proxy / extra_cookies / extract_js / scroll_wait_ms`，
  结果多一个 `RenderResult.js`。
- 通道全挂时，`engine_status` 会逐条列出真实原因。
- 前端引擎状态带实际通道：`ok:40·render`。

### 9.2 会员视频：试看 → 完整版

会员内容的播放地址是服务端按账号裁剪的，**没有「改参数拿完整流」的漏洞**。
`app/vip_unlock.py` 做四层兜底：

| 顺序 | 通道 | 说明 |
|---|---|---|
| ① | 会员 Cookie 直取 | `DATA_ROOT/vip_cookies/*.txt` 放大会员 Cookie，B 站走 DASH |
| ② | 资源站换源（主力） | `app/resource_sites.py`，12 个实测可用 CMS 采集接口，同片名多源备份 |
| ③ | 海外免费源 | `app/alt_sources.py`，YouTube（yt-dlp 搜索）；需 `download_proxy`，按季/集/时长匹配 |
| ④ | 解析网关（可选） | 用户自填接口；20 个公共解析站实测全挂，默认不启用 |

触发：下载报会员错误，**或**下完实测时长 < 标称 ×0.85 且差 >90s。
B 站普通投稿（UGC）不做时长比对——它没有试看，只有清晰度受限。

实测（2026-11-04）：`ep323085`（名侦探柯南 中配 第1话，大会员）→ 元数据 1494s
→ 极速资源换源 `vv.jisuzyv.com` → 流 1493.12s / 285 分片
→ 完整下载 76,119,583 字节 / 1493.146s / H.264 720x576 + AAC（603s 跑完）。

2026-11-05 复测用户给的两条链接：B 站 `ep810204`《人间世 第二季》第1集
→ 命中「人间世第二季」→ 3216.1s / 1594 分片；优酷《谷雨》第2集
→ 命中「谷雨」第2集 → 2725.1s / 651 分片，两条 30 秒限量下载均出片。

### 9.3 顺带修掉的老问题

- **`win_job.reap_orphan_ffmpeg` 误杀**：活进程集合原来只有 ffmpeg 自己，
  父进程 python 永远「不存在」，收割线程每 5 分钟把正在下载的 ffmpeg 杀掉，
  表现是下载停在 0 字节的 `.part`。现改为全系统进程集合 + 项目 ffmpeg 路径匹配。
- **`stream_downloader` 看门狗失效**：ffmpeg 卡在拉分片时仍会吐 `progress=continue`，
  只看「有没有输出」永远不触发。现额外跟踪 `out_time`，120 秒不前进即中止；
- **取消不生效**：换源下载没接 `cancel_event`，取消后还会继续试源、搜 YouTube；
  视频任务取消又被记成 `failed`。现在取消会删掉分片并在几秒内标成 `cancelled`。
  另加 `-rw_timeout 30s` 与 TLS 校验兜底（部分资源站 CDN 证书链 ffmpeg 不认）。

## 10. 本机环境

| 项 | 值 |
| Python（项目脚本） | `C:\Python314\python.exe`（受管 3.13.12 缺 requests/yt-dlp） |
| 缺失可选依赖 | `pywebview`、`pystray` 未装在 Python314（只影响桌面壳模式，Web 模式可跑） |
| Inno Setup | `C:\Program Files (x86)\Inno Setup 6\ISCC.exe` |
| bsk（外部） | `C:\Users\admin\.local\bin\bsk.exe` v0.3.2, 15 MB |
| Playwright 浏览器 | **V3.0 起自带**：`browsers/chromium-1234/`（约 430 MB，Chromium 151）。系统 Chrome/Edge 只作兜底 |
| 系统浏览器 | Chrome + Edge 都在（所以 V2.5 才能靠系统浏览器跑） |
| 服务启动 | `C:\Python314\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8848` |
