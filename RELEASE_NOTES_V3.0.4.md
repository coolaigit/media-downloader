# MediaDownloader V3.0.4

| 项 | 值 |
|---|---|
| 版本号 | `app/config.py` `APP_VERSION = "3.0.4"` / `APP_TITLE = "MediaDownloader 3.0.4"` |
| 发布日期 | 2026-11-09 |
| 代码基线 | 功能改动截止 `aae5124`；其后为发布说明、台账与回归脚本入库（tag `v3.0.4` 指向远端 main 的发布提交） |
| 构建方式 | `_venv` + PyInstaller 6.22.3（`MediaDownloader_Test.spec`，onefile 隔离测试版） |
| Release 文件 | `MediaDownloader_TEST.exe` — 89,083,082 B，FileVer `3.0.4.1` / ProductVersion `3.0.4-test` |
| SHA256 | `e01297988219b52d650b0e10bbbaa17deaaba9e86b378d0fe9e2f9cd8528243c` |
| 系统要求 | Windows 10/11 x64，系统 WebView2 运行时；测试版不装系统目录、不写正式版数据 |

## 一句话结论

V3.0.3 修的是「双击白屏」，V3.0.4 重做首页图片/视频通道与工作台版面，并把之后实测出来的
28 条问题（登录态误判、批量抓取重复、会员试看片段冒充整集、CA 丢失导致全平台 HTTPS 挂、
带语言前缀的 B 站链接下不了等）逐条冻结、修复、进回归门禁。

## 主要变化

| 主题 | 内容 |
|---|---|
| 定版与界面 | `0219cef`：`APP_VERSION`/`APP_TITLE`、`/api/health` 带 `version`、`version_info*.txt`、iss 与 `index.html` 五个版本串；首页搜索框内「图片/视频」通道 + 站点自动识别 + 单条/批量分流 + 手动锁定 + 关键词回退；工作台 `--panel-h` 撑满视口、空态居中、视频页长说明折叠、删重复控件 |
| 平台登录态 | `ec017de`、`4a53abe`：修番剧/单集页被误判「登录失效」——同页判定两侧折小写、语言前缀白名单、切断「建议重登→页面没变→判失效」自证循环，`login_invalid.json` 加 `code` 与 TTL（redirect 900s），过期落回 ready |
| 批量抓取 | `f203968`、`a61cce4`、`4a37d76`、`15c4226`、`4a53abe`：快手 feed 接口缩略图、登录墙如实报因、图片多尺寸档位归一条、知乎只交真视频并补标题封面、番剧页 16 条同片重复归 1 条（多 CDN 镜像 / 多码率 / UUID 前缀一律按归并键只留最高档），推荐流不再当本页作品 |
| 视频下载 | `ebe05e9`、`7fa1bce`、`4a53abe`、`aae5124`：文件名用作品标题而非 CDN 随机字符、知乎 `/video/` 归一 `/zvideo/`、占位标题回填真实片名、`_preview_guard` 判试看（6:00 / 51:15 必出 ⚠️ 说明）、换源带 900s 预算、B 站 `/zh-Hans/` 语言前缀先剥再交给 yt-dlp |
| CA 与网络 | `fc9c40b`：PyInstaller onefile 的 `_MEI/certifi` 被清后全平台 HTTPS 全挂 —— 启动早期把 CA 固化到 `DATA_ROOT/certs/cacert.pem`，改写 `REQUESTS_CA_BUNDLE` 等，源全丢时从 Windows 根证书库导出兜底，`/api/health` 加 `ca_bundle` 字段 |
| 界面细节 | `9ec1e27`、`4a53abe`：裸域名不再被当关键词搜图、手选模式不被覆盖；带 `#` 的文件名预览不再 403；过期缩略图（24h）不下发；`/proxy` 透传上游 4xx；站点未读角标不进标题；单集页提示不说成「主页」 |

冻结项 F-01 ~ F-28 的现象、根因、复测方式与最近验证日期，逐条记在仓库 `REGRESSION.md`。

## 回归结论（交付前，两版各跑一遍全量）

- 打包版 18848：v305 17/0、v304 46/0、frozen 8/0、ca_bundle 10/0、home 18/0、
  hero / filter / pkg_ui / pkg_ui2 ALL PASS、bare_url_ui 21/0；11 组离线用例全 PASS
  （dedupe 20/0、zhihu 35/0、direct_filename 15/0、bare_url 17/0、ca_bundle 23/0、
  preview_guard 39/0、vip ALL PASS）。日志 `_reg_v318_signoff.log`。
- 源码版 18849：同组逐项一致，全 PASS。日志 `_reg_src_v318_signoff.log`。
- 端到端：带 `/zh-Hans/` 的 `ep810642` 实下得到
  `人间世 第二季 第8集 儿科医生 [810642].mp4`（15,788,305 B），note
  `⚠️ 只拿到试看片段（6:00 / 正片 51:15），换源也没拿到完整版；这个文件不是完整正片。`
- 两版实例进程都起于最后一次源码改动之后（`REGRESSION.md` 坑 5）。

## 关于 deepseek harness 会话的更新记录

**会话 ID：`session-c4d7b847-0d3a-4922-b659-cde96a34b050`**

该会话的正文不在本仓库，也不在 WorkBuddy 历史会话与本地日志里 —— 本地只有用户转述时
留下的这个 ID（见 `.workbuddy/memory/2026-11-05.md`）。因此下面按**本仓库可查证的提交与
文档**整理它对应的那条更新链，ID 只作溯源，不据它推断会话里说过什么。

| 日期 | 提交 | 可查证的记录 |
|---|---|---|
| 2026-11-04 | `2cf97ef` | Google 搜图三通道（API / 渲染 / SearXNG）自动降级 |
| 2026-11-04 | `05f71d9` | 接通 Google 与会员兜底的设置 / 状态 / 接口 |
| 2026-11-05 | `fe8eaa8` | 按会话转述的「三分钟配 API」方案落地 `setup_google_api.py`：写盘前真实调用 `customsearch/v1` 校验 key/cx，通过后原子写两份 config 并留备份，配套双击 bat 与 6 条离线用例；说明文档补 2026-11-05 实测 |
| 2026-11-05 | `531acaf` | 同日复核后整体回退：Google 官方概览原文 `The Custom Search JSON API is closed to new customers`，新项目无法启用、存量客户 2027-01-01 停服。删除上面三个文件，`search_google.py` 去掉 api 通道，旧配置 `google_transport=api` 回落 `auto`，离线用例补「旧 api 配置回落」 |
| 2026-11-05 之后 | `82f7602` / `996e56d` / `9b7ee1f` / `e4dd380` | SearXNG 改显式启用、补 Google/会员兜底离线用例与实测说明、补全文档；`说明-Google接入与会员换源.md` 记录两条通道与本机实测 |
| 2026-11-08 | `9ec1e27` | 裸域名不再被当成关键词搜图（同属这条链的后续修正） |

**净结果**：官方 API 通道被实测证伪并撤回，一键配置脚本与其用例随撤回一起删除，不留死代码；
V3.0.4 里保留的是 `render → searx` 自动降级、`app/search_google.py`、
`说明-Google接入与会员换源.md`、`test_google_offline.py`。
该会话还转述过一次同类产品 UI/UX 只读调研（`_ui_research/UI设计调研报告.html`，本地产物、
`_` 前缀不进仓库），本轮**未实施**，V3.0.4 界面改动来自 `0219cef` 等本仓库提交，与它无关。

## 已知限制

- 正式安装包（Inno Setup 6）未构建，Release 里提供的是**隔离测试版单文件 exe**；
  `iss` 脚本的版本号已跟进 `MediaDownloader_V3.0.4_Setup`，需要正式包时执行 `构建V3安装包.bat`。
- 知乎视频、国外平台仍未由用户（Sam）验收，`REGRESSION.md` 明确不计 PASS。
- 会员正片换源依赖使用者自己的登录态；只拿到试看片段时工具会明确写出时长与「不是完整正片」，
  不会静默当整集交付。
