# MediaDownloader V3.0.5

| 项 | 值 |
|---|---|
| 版本 | `3.0.5` |
| 发布日期 | 2026-11-12 |
| 交付文件 | `MediaDownloader_V3.0.5.exe`（**安装包**，Inno Setup 6，免管理员权限） |
| 默认安装位置 | `%LOCALAPPDATA%\Programs\MediaDownloaderV3` |
| 随包组件 | 与 V3.0.4 相同：Playwright Chromium（`_internal\browsers\chromium-1234\`）、WebView2 Fixed Version Runtime、ffmpeg 9.0.2 静态版、BrowserSkill（bsk.exe + 浏览器扩展）、Playwright 驱动与 CA 证书、VC++ 运行库 |
| 运行环境 | Windows 10/11 x64；浏览器内核、WebView2 桌面壳运行时、ffmpeg 全部随包，目标机器不需要预装任何浏览器或运行库 |

## 本版更新

**YouTube 下载修复**

- 内置 yt-dlp 引擎 2026.8.19 → 2026.10.26：YouTube 侧的播放器与签名校验两三个月就变一轮，旧版提取器会导致解析不出清晰度或媒体地址被站点拒绝（403）。新版已实测通过解析（24 档清晰度）与 720p / 4K 真实下载。
- 主下载与解析链路接上「下载代理」设置：此前该设置只对会员换源兜底通道生效，粘贴链接直接下载 YouTube / TikTok / Instagram 时始终直连，国内网络必然超时失败。现在这四个海外平台会按设置里的 `download_proxy` 走代理，国内站保持直连，批量任务里国内外混选互不影响。

**其它**

- 新增离线回归用例固化代理注入逻辑（`test_download_proxy_offline.py`，13 项）。

发布前已跑全量自动化回归：界面（home 18/0、hero ALL PASS）、通道与版面（v304 46/0）、渲染语义（frozen_now 8/0）、HTTPS 与 CA（ca_bundle 全 PASS）、离线用例 8 组全 PASS；解析与下载在开发环境端到端实测通过。

**升级提示**：从 V3.0.4 直接安装覆盖即可，下载历史、登录态与设置全部保留。安装包在「设置 → 网络」里新出现了「下载代理」的生效范围说明：只对 YouTube / TikTok / Instagram / Pinterest 生效。
