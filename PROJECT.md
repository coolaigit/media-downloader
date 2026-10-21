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

## 3. 并发控制现状（Phase 2 的改造对象）

| 位置 | 现状 | 问题 |
|---|---|---|
| `task_manager.py:77` `_VIDEO_GATE` | 全局 3 路可调 | 不分平台，抖音降速会连累 B站 |
| `main.py:896` | 抖音 ≥100 条 → `min(concurrency, 2)` | 硬编码，永远 2 路，无法恢复 |
| `profile_sources.py:1092` | B站元数据/缩略图 `min(4, …)` | 硬编码，影响视频下载吗？不会，但也不会上调 |
| `downloader.py batch_download_images` | `max_workers=concurrency` | 固定，风控后不降 |
| `task_manager.py:784` | 流媒体 1–8 路 | 固定 |
| `batch_crawler.py` | `detail_concurrency=4` | 固定 |

**结论：V2.5 的并发是「静态参数」，没有反馈回路。这就是 V3.0 要换掉的东西。**

## 4. 下载失败明细现状（Phase 1.5 的改造对象）

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

## 7. 本机环境

| 项 | 值 |
|---|---|
| Python（项目脚本） | `C:\Python314\python.exe`（受管 3.13.12 缺 requests/yt-dlp） |
| 缺失可选依赖 | `pywebview`、`pystray` 未装在 Python314（只影响桌面壳模式，Web 模式可跑） |
| Inno Setup | `C:\Program Files (x86)\Inno Setup 6\ISCC.exe` |
| bsk（外部） | `C:\Users\admin\.local\bin\bsk.exe` v0.3.2, 15 MB |
| Playwright 浏览器 | `ms-playwright` 里**只有 marker 文件，没有 Chromium** |
| 系统浏览器 | Chrome + Edge 都在（所以 V2.5 才能靠系统浏览器跑） |
| 服务启动 | `C:\Python314\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8848` |
