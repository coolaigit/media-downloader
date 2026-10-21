# ROADMAP — Media Downloader V3.0

> 基线：V2.5 Final（`media-downloader-2.5-work` @ `89a542c`）
> V3.0 工作副本：`D:\QWen Project\media-downloader-3.0-work`（分支 `v3.0-work`）
> 正式旧版 `D:\QWen Project\media-downloader` 保持**只读**，V2.5 工作副本**不再修改**。

## 发布流程（不可跳步）

```
V2.5 Final → V3.0 工作副本 → 开发 → 自动测试 → 真实平台测试
→ frozen EXE 测试 → 安装包测试 → 独立验收智能体全面验收
→ 用户人工验收 → 才允许转正
```

---

## Phase 1：建立 V3.0 基线

| 项 | 内容 |
|---|---|
| 目标 | 从 V2.5 Final 建立独立工作副本与分支，跑通完整基线，确认「起点是干净的」 |
| 前置 | V2.5 Final 提交 `89a542c` 工作区干净 |
| 任务 | ① `git clone` 建立副本 ② 新分支 `v3.0-work` ③ 补齐运行时资产（`bin/ffmpeg.exe`）④ Python 语法检查 ⑤ JS 语法检查 ⑥ `run_regress.py` 全量回归 ⑦ 启动服务冒烟 |
| 输出 | 可运行的 V3.0 副本 + 基线报告 |
| 验收 | 回归全 PASS（或历史已知失败如实列出）；服务能起、UI 能开 |
| 下一阶段交接 | 基线报告里的失败项 → 进入 Phase 2 前必须先判定「是否 V2.5 遗留」 |

## Phase 1.5：下载失败可视化 + 一键重试（用户新增需求）

| 项 | 内容 |
|---|---|
| 目标 | 批量下载失败时，能看清**是哪几条失败、为什么失败**，并能只重下失败项 |
| 现状 | 后端 `Task.items` 已记录每项 `{index,url,ok,skipped,error}`（图片/流媒体两条链路都有），但前端 `pollTasks()` **只渲染统计数字，从不渲染 items**；也没有重试入口 |
| 任务 | ① 任务卡片增加「失败 N 条」可展开明细（标题/URL/失败原因）② 明细支持复制 URL ③ 「重试失败项」按钮 → 用同一批参数只重提失败条目 ④ 重试时 `skip_existing` 与 history 去重的交互要正确（失败项本来就没入库，不会被误跳过）⑤ 大批量时 `items[-200:]` 截断问题要处理（保留全部失败项，可截断成功项） |
| 输出 | 前端失败明细面板 + `/api/tasks/{id}/retry-failed` |
| 验收 | 构造 5 条成功 3 条失败 → UI 能列出 3 条失败及原因 → 点重试 → 只发 3 条 |
| 依赖 | Phase 1 基线通过 |
| 风险 | 大任务 items 回传体积；重试可能重复写 history |

## Phase 2：动态并发控制器

| 项 | 内容 |
|---|---|
| 目标 | 用户设「最大并发」，程序按平台实时风控状态自动升降，不永久停留低并发 |
| 现状（实测） | ① `_VIDEO_GATE` 全局 3 路可调（`task_manager.py:77`），**不分平台** ② 抖音 ≥100 条硬降 `min(2)`（`main.py:896`）③ B站元数据/缩略图硬限 4 路（`profile_sources.py:1092`）④ 图片批量 `max_workers=concurrency`（`downloader.py`）⑤ 流媒体 1–8 路（`task_manager.py:784`）⑥ 列表抓取 `detail_concurrency=4`（`batch_crawler.py`） |
| 缺口 | 并发全是**静态参数**，没有任何「风控信号 → 降速 → 稳定 → 恢复」的反馈回路，也没有平台维度/任务类型维度 |
| 任务 | ① 新建 `app/concurrency.py`：`(platform, task_kind)` 双维度可调闸门 ② 风控信号识别（412/429/fresh cookies/timeout/captcha/滑块/安全验证/CDN拒绝/连续失败）③ 降速：立即减半，带冷却 ④ 恢复：连续 N 次成功 → 逐级翻倍回上限 ⑤ 替换 V2.5 三处硬限并发为「上限」语义 ⑥ `/api/concurrency/status` + UI 显示「上限/当前/状态/原因」 |
| 验收 | 模拟 412 → 目标平台降低、其它平台不变；连续成功 → 逐级恢复；UI 数字与实测并发一致 |
| 风险 | 降速过度变性能回归；线程池 worker 数不能动态改（需用「固定 worker + 可变名额」实现） |

## Phase 3：内置 Playwright Chromium

| 项 | 内容 |
|---|---|
| 目标 | 用户电脑没有 Chrome/Edge/任何浏览器，也能跑浏览器自动化 |
| 现状 | `ms-playwright` 缓存里**只有 marker 文件，没有实际 Chromium**；依赖系统 Chrome/Edge |
| 任务 | ① 下载官方 Chromium 并随包分发 ② `PLAYWRIGHT_BROWSERS_PATH` 指向包内目录 ③ frozen 环境路径解析 ④ 无系统浏览器的干净机验证 |
| 验收 | 干净机（无 Chrome/Edge）装包后能完成一次需要渲染的抓取 |
| 风险 | 包体积 +150MB 起；PyInstaller 路径解析；license 记录 |

## Phase 4：BrowserSkill 内置

| 项 | 内容 |
|---|---|
| 目标 | 后台 Chromium → 检测到验证 → 可见窗口/BrowserSkill → 验证完成后**继续原任务** |
| 现状 | `app/browser_skill.py` 依赖 `shutil.which("bsk")`，当前 `bsk.exe` 在 `C:\Users\admin\.local\bin`（**外部工具**） |
| 任务 | ① 内置 `bsk.exe`（v0.3.2, 15MB）② 独立浏览器 Profile ③ 验证检测（验证码/滑块/verify you are human）④ 验证升级与恢复任务 ⑤ BrowserSkill 缺失时**正常抓取不受影响** |
| 验收 | 正常抓取全程无弹窗；只有验证时才会出现可见窗口；验证后可恢复 |
| 风险 | 弹窗规则回归（V2.5 已修好的「不许图省事 headless=False」） |

## Phase 5：完整平台回归

抖音 / B站 / 微博 / 小红书 / YouTube / Instagram / TikTok / Pinterest / 快手 / 关键词搜图。
重点：V2.5 已修好的功能一条都不许回归（见 `PROJECT.md` §保留清单）。

## Phase 6：测试安装包

`MediaDownloader-Setup-3.0.0-Test.exe`（**不转正式版**）。
必须真机验证：pystray 已打包、Chromium 存在、ffmpeg 存在、BrowserSkill 存在、资源路径、WebView、托盘、升级安装、用户数据保留。

## Phase 7：独立验收智能体全面验收

与开发智能体**分开**的 QA 智能体，只审计/测试/找 Bug/出证据，默认不改代码。
必须参考历史人工测试痕迹建立长期回归矩阵，输出 PASS / FAIL / NOT TESTED，结论只能是
`PASS — 可以进入用户人工验收` 或 `FAIL — 必须返回开发修复`。

## Phase 8：用户最终人工验收

只做真正需要人判断的体验验收，不再承担第一轮全面找 Bug。
