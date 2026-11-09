# REGRESSION — 已确认修复台账（冻结项）

> 规则来源：用户级总指令「已确认修复一律定死 + 回归必验」（2026-11-07 Sam 确认）。
>
> **用法**：每次修 bug / 做新功能，动手前和交付前各跑一遍本表。表内每条都必须有
> 「复测方式」，且优先用 Sam 真实复测时的那条路径，不要换成自编用例。
> 回归挂了 = 没修完，不许用「新功能通过」顶账。
>
> 项目：MediaDownloader V3.0.4（`E:\Project\media-downloader-main`）
> 基线产物：`_dist_ui/MediaDownloader_TEST.exe`（测试版，端口段 18848–18870）
> 测试沙箱：`_dist_test\MediaDownloader_TEST\data`

## 自动化回归入口（先跑这一组，全量）

```bash
cd /e/Project/media-downloader-main

# 需要实例：先起一个，再把它当 BASE 传进去
_venv/Scripts/python.exe launcher.py            # 源码版，默认 8848-8870
./_dist_ui/MediaDownloader_TEST.exe             # 打包版，18848-18870

_venv/Scripts/python.exe _verify_v305.py  http://127.0.0.1:<port>   # 18 项：登录态 / 窗口语义 / 界面
_venv/Scripts/python.exe _verify_v304.py  http://127.0.0.1:<port>   # 46 项：版本号 / 通道 / 版面
_venv/Scripts/python.exe _verify_home_ui.py      http://127.0.0.1:<port>   # 18 项
_venv/Scripts/python.exe _verify_hero_ui.py      http://127.0.0.1:<port>
_venv/Scripts/python.exe _verify_filter_scope.py http://127.0.0.1:<port>
_venv/Scripts/python.exe _verify_pkg_ui2.py      # 包内前端资源（含反向断言）
_venv/Scripts/python.exe _verify_pkg_ui.py       # 包内 PYZ 模块
_venv/Scripts/python.exe _verify_frozen_now.py   http://127.0.0.1:<port>  # F-13 / F-15
_venv/Scripts/python.exe _verify_ca_bundle.py    http://127.0.0.1:<port>  # F-20：CA 状态 + 四站 HTTPS + yt-dlp

# 离线用例（不需要实例）
_venv/Scripts/python.exe test_filter_scope_offline.py
_venv/Scripts/python.exe test_crawl_links_offline.py
_venv/Scripts/python.exe test_batch_gate_offline.py
_venv/Scripts/python.exe test_isolation_check.py
_venv/Scripts/python.exe test_dedupe_offline.py
_venv/Scripts/python.exe test_zhihu_video_offline.py
_venv/Scripts/python.exe test_direct_filename_offline.py
_venv/Scripts/python.exe test_bare_url_offline.py
_venv/Scripts/python.exe test_ca_bundle_offline.py   # F-20：固化 / 自愈 / 自定义 CA，23 项
_venv/Scripts/python.exe test_vip_offline.py            # F-17/F-24：集号片名匹配 / 试看判定 / 换源，72 项
_venv/Scripts/python.exe test_preview_guard_offline.py  # F-24：试看判定与「自动换源」开关解耦，30 项
_venv/Scripts/python.exe test_bare_url_ui.py          http://127.0.0.1:<port>  # 真实 Chrome，挂桩不联网
# F-20 加强验收（会删运行中实例的 _MEI/certifi 与持久 CA，只在排障时手动跑）：
# _venv/Scripts/python.exe _verify_ca_selfheal.py http://127.0.0.1:<port>

# 端到端（要带登录态的实例；会抓快手主页并真下 2 个小视频到
# downloads/crawl/_reg_name_test，跑完请删掉这个测试目录）
_venv/Scripts/python.exe _verify_direct_name.py  http://127.0.0.1:<port>

# 端到端 F-24（会员试看）：自己会临时关掉 vip_auto_unlock、清掉该集台账、
# 真下一条番剧，跑完恢复设置并清测试目录；要带 B 站登录态的实例
_venv/Scripts/python.exe _e2e_vip_final.py http://127.0.0.1:<port> \
    "https://www.bilibili.com/zh-Hans/bangumi/play/ep810642?from_spmid=666.25.episode.0"
```

通过判据：每行末必须是 `ALL PASS` / `结果：N 通过 / 0 失败` / `PASS`。出现任何 FAIL 就不算完成。

---

## 冻结项清单

### F-01 版本号单点定义 = 3.0.4

- **冻结**：2026-11-07（V3.0.4 定版）
- **现象**：页面标题 / 顶栏 / exe 文件属性 / 安装包版本四处对不上。
- **根因**：`app.js?v=` 的缓存穿透串没跟版本走；`version_info.txt` 落后一版；iss 写死 3.0.3。
- **复测**：`curl /api/health` 看 `version`；页面 `<title>`；`link[rel=stylesheet]` 的 `?v=`；
  `_verify_v304.py` 的 `[版本号]` 5 项。
- **最近验证**：2026-11-07 PASS

### F-02 首页图片/视频通道（自动识别 + 手动只锁当前输入）

- **冻结**：2026-11-07
- **现象**：首页只有一条搜索框，默认图片通道，贴视频链接会被当图片抓。
- **根因**：没有通道概念，全按图片走。
- **复测（Sam 的实际操作）**：首页粘贴 `https://www.kuaishou.com/profile/3xnmi2xh2pxzpag`
  → 「视频」按钮必须自动点亮，下方出现 `视频 · kuaishou.com`；再贴
  `https://www.pexels.com/search/led/` → 必须切回图片。
  `_verify_v304.py` 的 `[首页抓取通道]` 9 项。
- **最近验证**：2026-11-07 PASS

### F-03 通道归属按页签（图片页贴视频链接不跳视频页）

- **冻结**：2026-11-07（Sam 反馈「我在图片框里搜索肯定是想搜这个博主的图片」）
- **现象**：图片页 omnibox 贴小红书/快手主页，整条输入被甩到视频抓取。
- **根因**：`runOmni()` 按域名猜通道，不看当前页签。
- **复测（Sam 的实际操作）**：切到「抓取图片」→ 顶部框粘
  `https://www.xiaohongshu.com/user/profile/<id>?xsec_token=...` → 标签必须写
  `图片 · xiaohongshu.com`，点「开始抓取」必须**停在图片页**、走链接抓取。
  关键词（如「大胸甘油」）也一样：标签写 `关键词 · 搜图`，落图片页关键词模式。
  首页仍要按站点识别（这条别一起改坏）。
- **最近验证**：2026-11-07 PASS（`_verify_v304.py` + 独立 11 项）

### F-04 工作台面板撑满视口、结果区吃满余量

- **冻结**：2026-11-07（Sam 反馈「内容都在顶上，下面大块空白」）
- **根因**：面板高度由内容决定；`.workpane` 子项可收缩，结果区抢高度后搜索行被压成 1px。
- **复测**：四个工作区（图片/关键词、图片/链接、视频/关键词、视频/链接）逐个看：
  虚线空态必须撑到面板底部并居中（`--panel-h` ≈ 845px @ 950 视口）；
  视频页搜索行高度 ≥ 30px、宽度 > 600px（不能被压扁）；长说明默认折叠成一行。
  `_verify_v304.py` 的 `[工作台版面]` 20 项。
- **最近验证**：2026-11-07 PASS

### F-05 抖音单条视频解析（走渲染，不交给 yt-dlp）

- **冻结**：2026-11-07（Sam 明确「抖音已修复（确认）」）
- **现象**：`ERROR: [Douyin] <id>: Fresh cookies (not necessarily logged in) are needed`
- **根因**：yt-dlp 的 DouyinIE 直接打 `/aweme/v1/web/aweme/detail/`，该接口要 `a_bogus`
  签名（extractor 里还是 TODO），拿不到响应就抛这句——**与登录态无关**。
- **复测（Sam 的实际操作）**：抓取视频页粘
  `https://www.douyin.com/video/7635098784464778851` → 点「解析清晰度」。
  必须 15s 内出结果：标题非空、时长 235、封面是 `p3-pc-sign.douyinpic.com/tos-cn-p-*`
  （**不能是 aweme-avatar 头像**）、formats 里有一个 mp4 直链。
  API 判据：`POST /api/video/probe` 返回 `extractor == "Douyin/profiled"`。
- **坑（改这里时必看）**：`<video>.currentSrc` 是 `blob:`（MSE），下载只会得到打不开的 blob；
  必须监听网络响应捡 `douyinvod.com`，并按 `content-length` 取最大（同页先吐 1.5MB 预览、
  再吐 8.1MB 正片）。直链会比元数据先就绪，duration/封面要等 `loadedmetadata` 再读。
- **最近验证**：2026-11-07 PASS（源码 6.3s / 打包版 12.8s，直链 8.1MB，带 Referer 下载 200）

### F-06 快手登录态判定（webday7 系凭据）

- **冻结**：2026-11-07（Sam 反馈「快手明明已登录，关闭窗口后还是显示未登录」）
- **根因**：`auth_cookies` 只认 `kuaishou.server.web_st`，快手网页端已换成
  `kuaishou.server.webday7_st` / `webday7_ph`（直播子域 `live.web_st/ph`）。
  实测 25 条 Cookie 一条都命中不了。
- **复测**：`GET /api/platforms/status` → `platforms.ks.state == "ready"`，
  `auth_cookie_names` 含 `webday7`；平台登录页快手卡片显示「已登录」。
- **最近验证**：2026-11-07 PASS

### F-07 「登录窗口已打开」只认登记过的 pid

- **冻结**：2026-11-07（Sam 反馈「不定时显示登录窗口已打开」）
- **根因**：判据是 profile 的 lockfile，而本程序自己的后台渲染（抓主页/解析直链/刷新 Cookie）
  用同一个 profile，同样持有 lockfile → 把「我在抓数据」报成「窗口开着」。
- **复测**：没开登录窗口时，`/api/platforms/status` 里每个平台 `in_use == false` 且
  `in_use == window_open`；解析一次抖音 / 抓一次快手主页之后**再查一次**，仍必须全 false。
  `_verify_v305.py` 的 `[「登录窗口」语义]` 7 项。
- **坑**：Windows 判活用 `GetExitCodeProcess`，**不能用 `os.kill(pid, 0)`**（会真的杀进程）。
- **最近验证**：2026-11-07 PASS

### F-08 快手主页批量抓取（含缩略图）

- **冻结**：2026-11-07 抓取通过；**2026-11-08 缩略图修复并确认**
- **复测（Sam 的实际操作）**：抓取视频页粘
  `https://www.kuaishou.com/profile/3xnmi2xh2pxzpag` → 「开始抓取视频」→
  应出现带**真实封面**的卡片（人物照 + 播放角标），标题可读，卡片可下载。
- **根因（2026-11-08 查清，两层）**
  1. **卡片不是 `<a>`**：快手主页作品是
     `div.photo-card > div.cover > div.aspect-div > img.cover-img`，
     整页只有 8 个 `<a>`，全是导航（发现/直播/关注）。所以
     `a[href*=short-video]` 恒为 0、`video` 元素恒为 0 —— 按链接或按 video 找卡片必然空手。
     这正是「抓取成功但没缩略图」的直接原因：只从原始数据里正则捞到了媒体地址。
  2. **登录态必须有效**：`/rest/v/profile/feed` 返回 `{"result":109}` 时，
     页面把作品区换成「想看更多视频，快去登录吧」。实测 cookie（`webday7_st`
     len=354）**确实发在请求头里**，是服务端不认。重新扫码登录后该接口返回
     `{"result":1,"feeds":[...]}`，作品卡片才渲染。
- **现在的实现**：`profile_sources._kuaishou_profile_items()`，`browser_items`
  检测到 `platform == "ks"` 直接走它。解析 feed 接口的
  `feeds[].photo`：`caption`→标题、`coverUrl`→封面、`photoUrls`→直链、
  `id`→详情页 `https://www.kuaishou.com/short-video/<id>`、`duration`(ms)→秒。
  作品区是**内部滚动容器 `.up`**，滚 `window` 不会触发分页。
  实测：7.5s / 8 条、前端 10 张卡片全部 `img.complete && naturalWidth > 0`。
- **踩坑记录（下次别再走回头路）**
  - 移动版三入口全废：`m.kuaishou.com/profile/<uid>`「找不到该页面」；
    `v.m.chenzhongtech.com/fw/profile/<uid>` 跳首页；
    `www.kuaishou.com/short-video/<uid>`（用 uid 冒充作品 id）跳 `m.gifshow.com`「找不到该作品」。
  - 匿名上下文同样 `result=109` —— 主页作品就是要求登录，不是环境问题。
  - 用 Playwright 的 `request.headers` 看 cookie 会得到**空数组**（浏览器注入的
    cookie 不在那份快照里）。要判断「cookie 有没有带上」必须用 CDP 的
    `Network.requestWillBeSentExtraInfo`。
  - 从视频直链换 host 推封面（`p2.a.yximgs.com` + 同路径）返回的仍是 mp4，
    换 `.jpg` 返回 400 —— 封面命名与视频不同，不能推导。
- **登录成功的判据**（下次判断用户到底登没登）：`cookies_from_profile.txt` 的 mtime 变化
  且 `kuaishou.server.webday7_st` 的 `exp` 前移（如 11-14 → 11-15）、
  `passToken` 变长。值本身可能复用，别只看 value。
- **Sam 确认（定格）**：2026-11-08 11:10「本次kuaishou修复确认通过，定格」。
  快手这条链路（主页批量抓取 + 缩略图）用户已验收，行为冻结，
  后续重构 / 优化不得改动，每次回归必须带上。
- **最近验证**：2026-11-08 PASS（源码 8860 端到端 10 张卡片缩略图全部加载）

### F-09 登录失效提示条不被侧栏遮挡

- **冻结**：2026-11-07（Sam 反馈「那个提示登录失效的红色提示框会被左侧栏挡住」）
- **根因**：`#loginAlert` 有 `class="login-alert"` 但 CSS 从未定义，是贴边裸文本；
  侧栏是 `position:fixed; width:232px; z-index:60`，直接盖住左边一截。
- **复测**：让任意平台处于失效态（例如小红书掉登录），切到「平台登录」页 →
  顶部红色提示条完整可见，左边界 ≥ 252px（侧栏 232 + 20）。
  `_verify_v305.py` 的 `[界面]` 2 项。
- **坑**：写了 `display:flex` 就必须写回 `.login-alert[hidden]{display:none}`，
  否则没有失效平台时那条空提示会一直占一行。
- **最近验证**：2026-11-07 PASS（实测 `left=252`、`overlap=false`）

### F-10 无封面卡片用图标 + 说明占位

- **冻结**：2026-11-07
- **现象**：无封面时是一块空白 + 一行灰字 `预览不可用`。
- **复测**：任意无封面卡片（快手最典型）显示「播放图标 + 暂无预览图」，竖排居中。
  `_verify_v305.py` 的 `[界面]` 3 项（断言 `noCoverPh` 存在、`display:flex`、文案）。
- **最近验证**：2026-11-07 PASS

### F-11 小红书错误必须如实分类（不许说成「该号没作品」）

- **冻结**：2026-11-07
- **根因**：小红书用 `success:true + code=-100` / `HTTP 461 + data:{}` / `code=300035`
  三套写法拒绝，旧代码只看 `success` → 逐篇详情全失败时报「没有找到图文笔记正文图片」。
- **复测**：
  - 登录态真的失效 → 报「登录态已失效…请重新扫码登录」，`login_required=true`，
    状态变 `invalid`。
  - 出口 IP 被风控 → `curl -sL https://www.xiaohongshu.com/` 返回 **HTTP 418 + 9 字节
    `risk pass`**，此时报「网络环境有风险…请更换网络」，不许让用户去重新登录。
  - 主页访问必须带 URL 里的 `xsec_token` / `xsec_source`（不带必然 461）。
- **最近验证**：2026-11-07 PASS（-100 归类已验证；418 已实测复现并单独识别）

### F-12 打包版与已装版隔离

- **冻结**：2026-11-06
- **复测**：`_venv/Scripts/python.exe test_isolation_check.py` → 端口 18848-18870 vs 8848-8870、
  data_root `_dist_test\MediaDownloader_TEST\data` vs `%LOCALAPPDATA%\MediaDownloader3`、
  实例指纹与配置互不可见。
- **最近验证**：2026-11-07 PASS

### F-13 下载文件名用作品标题（不落成 CDN 随机字符）

- **冻结**：2026-11-08（Sam 报「小红书和微博视频能下，但名字是随机字符」）
  ⚠️ 2026-11-08 Sam 更正：当时说的「微博」其实是**快手**，别再按微博修这条。
- **根因**：`outtmpl` 的 `%(title)s` 是 **yt-dlp 自己解析出来的**标题，不是调用方传的。
  两个站的专门 extractor 一旦失败就退回 Generic，把 URL 最后一段当标题
  （小红书 24 位笔记 id、微博 N 开头短码），落盘就是随机字符。
- **复测（Sam 的实际操作）**：抓一条小红书/微博视频 → 下载 → 文件名应是作品标题，
  形如 `<标题> [<id 前 16 位>].mp4`，不是 `1040g2sg31…mp4` 这种。
- **坑**：占位标题（视频下载/视频/download/未命名）必须排除，否则默认值被写进文件名。
- **最近验证**：2026-11-08 PASS（`YoutubeDL.prepare_filename` 渲染验证）

### F-14 同一张图的不同尺寸档位归成一条

- **冻结**：2026-11-08（Sam 报「知乎抓取图片会有重复图」）
- **根因**：知乎正文里同一张图有四种写法 —— `_720w` / `_1440w` / `_r` / 无后缀，
  URL 不同内容相同；`dedupe()` 只按完整 URL 比，于是列表里出现两三份。
- **复测**：抓一篇知乎图文回答 → 结果里不应出现同一张图的多个尺寸版本。
  `test_dedupe_offline.py`（20 项，纯离线）。
- **坑**：视频**不能**归一化 —— 不同清晰度是用户要选的，合并会丢档位。
  同一 key 下原图（`is_thumb=False`）必须压过缩略图，否则 `enrich_with_originals`
  补出的原图会和缩略图各留一份。
- **最近验证**：2026-11-08 PASS（四档同图 → 1 条原图，两张不同图不合并）

### F-15 知乎视频地址形态归一化

- **冻结**：2026-11-08（Sam 报「知乎抓取视频异常」）
- **根因**：知乎视频有两种地址 ——
  `zhihu.com/zvideo/<id>`（yt-dlp 有专门 extractor，能直接解析）；
  `zhihu.com/video/<id>`（302 到 `video.zhihu.com/video/<id>?$args`，
  只回 1.3KB 的 JS 壳，标题就俩字「知乎视频」，纯 HTTP 拿不到任何内容）。
  两者 id 相同、同一条视频。
- **修法**：`_normalize_media_url()` 把 `/video/<id>` 与 `video.zhihu.com/video/<id>`
  统一换成 `/zvideo/<id>`，`probe_video` 与 `download_video` 入口各调一次。
  只在识别出这个明确形态时改写，抖音/YouTube 等地址原样返回。
- **复测**：抓取视频页粘 `https://www.zhihu.com/video/1342930761977176064`
  → 应 1~2s 出标题「写春联也太难了吧！」和 4 档清晰度（ld/sd 478、hd 720、fhd 1080）。
- **最近验证**：2026-11-08 PASS（1.3s，4 档）；2026-11-08 复验 PASS
  （`_verify_frozen_now.py` 8/0：`/api/video/probe` 标题、封面、4 档、归一化地址全对）

### F-16 知乎抓视频只交出真视频，且带标题/封面/时长

- **冻结**：2026-11-08（Sam 报「zhihu 视频抓取的问题你还没解决，抓取到的有些根本不是视频，
  名称和封面似乎也没搞定」）
- **现象（三条都实测复现过）**
  1. 粘单条 `zhihu.com/video/<id>` → 0 条，还误报「知乎登录态已失效」：通用抓取去渲染
     302 后的 `video.zhihu.com/video/<id>?$args`，那是 1.3KB 的 JS 空壳。
  2. 粘问题页 `zhihu.com/question/665425481` → 20 条 `topic / people / question /
     answer / zhuanlan` 链接全标 `type=video`，title 与 thumbnail 全空。
  3. 另一轮回退网页抓取又带来 10 条 `vdn3.vzuu.com` 推荐流直链（同片多档重复、
     无封面、标题是通知栏文字）。
- **根因**：`browser_items` 的兜底用通用「作品链接」判据（长 ID + `answer/question`
  段即放行），再硬写 `kind_hint=video`；`list_author_items` 对知乎没有专属来源。
- **修法**：`profile_sources._zhihu_video_items()` 专属通道（先从页面挑
  `/zvideo/` `/video/` ID，再逐条 `probe_video` 拿标题/封面/时长/清晰度），
  `_looks_like_video_work_url()` 通用视频形态闸门，`browser_items(video_only=...)`，
  `list_author_items` 知乎视频模式成功/失败都不兜底，`crawl_list` 对
  「已按知乎视频通道确认」早退，不再退回网页抓推荐流。
- **复测（Sam 的实际操作）**：抓取视频页粘
  `https://www.zhihu.com/video/1342930761977176064` → 1 条，标题「写春联也太难了吧！」、
  封面 `picx/pic1.zhimg.com/...`、is_page=true、约 2s；粘
  `https://www.zhihu.com/question/665425481` → 0 条假结果 + 如实说明（约 16s）。
- **坑**：知乎 `/video/` 必须先归一成 `/zvideo/`（F-15）；撞 `account/unhuman`
  是出口 IP 风控，不要报成登录失效；解析不出元数据的条目宁可丢弃，不拿裸链接凑数。
- **最近验证**：2026-11-08 PASS（源码端到端 1 条带全元数据 / 0 条假结果；
  打包版 18848 同样 1 条带全元数据 1.0s、问题页 0 条假结果 16.6s；
  `test_zhihu_video_offline.py` 35/0；全量回归 v305 17/0、v304 46/0、
  home 18/0、hero/filter ALL PASS、isolation PASS、6 个离线用例全 PASS）

### F-17 直链下载文件名用作品标题（小红书 / 快手）

- **冻结**：2026-11-08（Sam 报「xiaohongshu 和 kuaishou（之前误说成了 weibo）的名称
  你也依然没有搞定」）
- **根因**：F-13 只修了 yt-dlp 那条下载路径；小红书/快手抓取结果是**签名直链**，
  走 `/api/download/direct`。`downloader.filename_from_url` 的占位名判据只认
  「纯字母数字 18+ 位」，快手的 `BMjAyNjEw..._b_B5f9b49955` 带下划线和日期，
  不命中 → 整串写进文件名；小红书列表接口不给 `display_title` 时连 title 都是空的。
- **修法**：`web/app.js` 的 `crawlVideoFilename()` 在提交直链下载时显式给
  `filename`（后端 `sanitize_filename` 负责非法字符、`unique_path` 负责重名）；
  `batch_crawler._xhs_note_meta()` 用笔记页 `og:title` / 内嵌 JSON 的
  `displayTitle` 补标题、`og:image` 补封面。图片条目不改名，无标题不硬造名字。
- **复测（Sam 的实际操作）**：抓快手 `kuaishou.com/profile/3xnmi2xh2pxzpag`
  → 勾一条 → 下载 → 落盘应是 `<作品标题>.mp4`，不是 `BMjAy...mp4`。
- **坑**：对照组（不传 `filename`）仍落成签名串，说明修复点在 `filename` 字段，
  别顺手把它删掉；F-13 的 yt-dlp 路径不能被这次改动带坏。
- **回归踩坑（2026-11-08）**：`_verify_direct_name.py` 真下的 mp4 会留在
  `downloads/crawl/_reg_name_test`。下一次跑 `_verify_hero_ui.py` 时切到「已下载」页
  会去请求 `/files/crawl/_reg_name_test/<作品标题>.mp4` 当缩略图，标题含空格 / `#`
  时服务端返回 403 → `console / pageerror 为 0` 挂掉。这是**测试产物污染**，不是前端回归；
  跑完端到端立刻删掉该目录再跑界面用例。真下载出来的标题文件在资源库预览会 403
  这条现象已记下，本轮未改 `/files/` 路由（不在 F-19 范围）。
- **最近验证**：2026-11-08 PASS（`_verify_direct_name.py` 真抓真下，源码版与
  打包版 18848 均 `RESULT PASS | titled=True | control-random=True`，
  1.55MB / 1.59MB 两个真文件；`test_direct_filename_offline.py` 15/0；
  F-13 复验 8/0；小红书发现页 4 条视频标题封面齐全，其直链签名过期后
  真下载会 403，故 xhs 下载名由同一条 direct 路径的快手真下载 + 本地 fixture 覆盖）
- **Sam 确认（定格）**：2026-11-08 11:10「本次kuaishou修复确认通过，定格」；
  11:11「本次xiaohongshu修复确认通过，定格」。两站直链下载按作品标题命名
  已由用户验收通过，F-17 / F-18 的行为全部冻结；后续任何重构、统一代码风格、
  顺手清理都不得改掉，每次回归必须复验 `filename` 字段与 `_xhs_note_meta()`。

### F-18 小红书发现页直链条目不崩

- **冻结**：2026-11-08（本轮修 F-17 时实测 `https://www.xiaohongshu.com/explore`
  报 `UnboundLocalError: cannot access local variable 'blocked'`）
- **根因**：`blocked` 只在「有 is_page 笔记、走了下钻」时定义，函数结尾
  `if platform == "xhs" and blocked:` 却无条件读它；发现页给的是直链，
  整段下钻跳过，于是崩在收尾。
- **修法**：`page_items` 之后先初始化 `blocked: list[MediaItem] = []`。
- **复测**：`test_direct_filename_offline.py` 的「发现页直链条目不进下钻也不能崩」
  2 项；或抓一次发现页看不再出现抓取异常。
- **最近验证**：2026-11-08 PASS（离线 2/2）

### F-19 裸域名链接不再被当成关键词搜图，且手选模式不被覆盖

- **冻结**：2026-11-08（Sam 报「类似问题复现，抓取图片，发的是链接（选择的也是链接抓取），
  自动给我跳转到搜图按照搜图模式下载」，补一句「仅仅是 weibo，其他网站没有」）
- **现象（截图证据）**：抓取图片页，omnibox 里是
  `weibo.com/u/1844960024?tabtype=newVideo`（**没有协议头**），用户点过
  「粘贴链接抓取」，点开始抓取后按钮回到「关键词搜图」，结果区
  「找到 12 张 · 已探测 12 张的真实分辨率 · bing-ok:80（其中 53 条与关键词相关）」，
  来源 Bing —— 整条 URL 被当关键词搜了图。
- **根因（真实复现，不是猜）**：`web/shell.js` 原判据
  `isLink = v => /^(https?:\/\/|www\.)/i.test(v.trim())` 不认裸域名 →
  `link=false` → `startFrom()` 里 `setMode(link ? 'link' : 'kw')` **无条件覆盖用户手选**，
  再点 `#btnSearch` 走 Bing；`web/app.js::doCrawl()` 又要求 `^https?://`，
  裸域名即使进到抓取输入框也会被拒。带协议头的同一链接完全正常，
  所以看起来「只有微博」；实测 `douyin.com/video/7635098784464778851` 同样中招，
  是**输入形态**问题，不是微博站点特判（微博恰好在 `VIDEO_HOSTS` 里，但不是原因）。
- **修法**：
  - `web/shell.js`：`COMMON_TLD` + `BARE_URL_RE` + `isLink()` 认裸域名
    （带路径/查询/端口直接算链接，纯主机名只认常见后缀，避免「雪山.航拍」误判）；
    `normalizeUrlInput()` 给裸域名 / `www.` 补 `https://`。
  - `web/shell.js`：`imageModeSelected()` 读 `#omniSeg button.on` 当 `modeHint`，
    `startFrom(v, c, modeHint)` 图片分支用 `modeHint || (link ? 'link' : 'kw')`，
    用户选了什么就按什么走；手选链接模式却只给了词 → toast 明说，不静默改模式搜图。
    **`imageModeManual` 只有亲手点过 `#omniSeg` 才置 true**：没点过时仍按
    `isLink` 自动切「粘贴链接抓取」（F-03 原行为，v304 第 46 项就是这条闸门，
    第一版修法误伤成 45/1，已回改）。`chanManualOn()` 两边都规范化，
    首页手选通道不会被补协议冲掉。
  - `web/app.js::doCrawl()` 独立输入框同样补协议（omnibox 之外的入口）。
  - **保留**：F-03 页签即通道（`channelForTab()`）、首页按站点自动猜通道、F-02 手动优先。
- **复测（Sam 的实际操作）**：抓取图片页 → 点「粘贴链接抓取」→ 粘
  `weibo.com/u/1844960024?tabtype=newVideo` → 开始抓取 →
  必须仍在图片页、按钮仍高亮「粘贴链接抓取」、走链接抓取出该页图片，
  **不能出现 bing-ok / 关键词搜图结果**。
- **坑**：别把「只有 weibo」读成站点特判去改 `VIDEO_HOSTS` 或平台识别表；
  判据在输入形态。关键词模式（默认）仍必须走 Bing 搜图。
- **最近验证**：2026-11-08 PASS（源码版 / 打包版 `test_bare_url_ui.py` 21/0；
  `test_bare_url_offline.py` 17/0；v304 46/0 复验 F-03 未被带坏；
  包内 `_verify_pkg_ui2.py` 新增正反向断言）

---

## Sam 的复测习惯（据此设计用例，别用自编的替代）

- 喜欢**直接在界面里粘真实链接**试，不用我给的示例词：抖音用
  `douyin.com/video/7635098784464778851`，快手用 `kuaishou.com/profile/3xnmi2xh2pxzpag`，
  小红书用 `xiaohongshu.com/user/profile/6279da432000000002102032?xsec_token=...`。
- 复测顺序基本是：首页 → 抓取图片 → 抓取视频 → 平台登录 → 设置。
  所以回归要覆盖这四个页签，不能只验被改的那一个。
- **会截图回来看界面**。视觉问题（挤压、遮挡、空白、错位）他一定会发现，
  改完界面必须自己先截图核一遍再交付。
- 反馈方式很短，只给现象不给复现步骤（如「小红书图片还是没法抓取」「窗口提示 risk pass」），
  所以定位必须靠实测复现，不能靠猜。
- 平台名偶尔会说错（2026-11-08 把快手说成微博），以**抓取结果里的 URL 域名**为准核对，
  不要凭上一条消息里的站名去改代码；但更正过的名字要立刻写回台账。
- 每改一轮都会问「为什么」，所以要能给出**证据链**（curl 的原始响应码、字节数、
  DOM 计数），不能只给结论。

---

### F-20 打包版长期运行后全平台 HTTPS 不再因 CA bundle 丢失而挂掉

- **冻结**：2026-11-08（Sam 18:42 报「视频抓取出现重大错误，所有平台抓取或下载失败：
  OSError: Could not find a suitable TLS CA certificate bundle」；
  18:46 补充测试平台是**抖音、小红书、快手、B站**）
- **现象**：四个平台一起挂，抓取和下载都失败；上午同一份 exe 全量回归还全 PASS。
- **根因（证据链，不是猜）**：
  - `_dist_test/MediaDownloader_TEST/data/logs/service.err.log` 里 159 处堆栈：
    `app/main.py:1278 api_proxy → requests/adapters.py:332 cert_verify → OSError`，
    invalid path = `%TEMP%\_MEI000004202\certifi\cacert.pem`。
  - `requests` 在 **import 时**把 `certifi.where()` 缓存进
    `requests.adapters.DEFAULT_CA_BUNDLE_PATH`，frozen onefile 下该路径指向
    PyInstaller 的临时解压目录。该临时目录的 `certifi/` 子目录被外部清理
    （11:37 启动的实例，父目录 mtime 17:11、`web/` 仍在、`certifi/` 消失）。
    之后**所有**走 requests / yt-dlp 的 HTTPS 请求同时抛 OSError。
  - 对照实测：18848 = PID 8580（11:37 启动，`_MEI000004202`）→ B站/抖音/知乎
    favicon 三个全 500；18849 = PID 9568（18:26 启动，`_MEI000043102`，
    有 235,863 B 的 cacert.pem）→ 三个全 200。
  - 补回 `_MEI000004202/certifi/cacert.pem` 后，18848 的
    B站/抖音/小红书/快手 favicon 立刻全 200（进程没重启）。
  - 所以是**临时目录里的 CA 文件消失**，不是平台接口变更、不是登录态、
    不是 F-01~F-19 的任何一条被带坏；`login_invalid.json` 不存在。
- **修法**：
  - 新增 `app/ca_bundle.py`：进程早期把 CA 固化到
    `DATA_ROOT/certs/cacert.pem`（持久目录，不随 Temp 清理），
    改写 `REQUESTS_CA_BUNDLE` / `CURL_CA_BUNDLE` / `SSL_CERT_FILE`、
    `requests.adapters.DEFAULT_CA_BUNDLE_PATH`、`certifi.where()`；
    源和副本全丢时从 Windows 系统根证书库导出 PEM 兜底，
    已跑起来的老进程也能自愈。参数外置：`MEDIA_DOWNLOADER_CA_BUNDLE`
    （自定义 CA）、`MEDIA_DOWNLOADER_CA_DISABLE=1`（排障关闭）。
  - `app/config.py` 在 `DATA_ROOT` 定下后立刻 `ca_bundle.install(DATA_ROOT)`，
    保证在任何网络模块发请求之前生效；失败也不让启动崩。
  - `app/main.py`：`api_proxy` 显式 `proxy_session.verify = ca_bundle.ca_path()`，
    `cert_verify` 抛裸 OSError 时 `refresh()` 自愈并重试；
    `/api/health` 增加 `ca_bundle` 字段（路径 / 是否存在 / 来源 / 大小）作排障证据。
- **复测（Sam 的实际操作）**：抓取视频 / 抓取图片里分别粘
  抖音、小红书、快手、B站 的真实链接，四站都要能抓能下；
  打包版**连续运行数小时后**再复测一次（这正是原事故的时间条件）。
  自动化：
  ```bash
  _venv/Scripts/python.exe test_ca_bundle_offline.py
  _venv/Scripts/python.exe _verify_ca_bundle.py http://127.0.0.1:18848
  ```
  验收加强项：把新实例 `%TEMP%\_MEIxxxxxx\certifi\` 删掉、
  甚至把 `DATA_ROOT/certs/cacert.pem` 也删掉，四站图片代理仍须 200（自愈）。
- **坑**：别用 `verify=False` 关校验顶事；别只靠「重启 exe 又好了」结案；
  别把 CA 放回 Temp 目录；别去改平台识别表或登录态。
- **最近验证**：2026-11-08 PASS（`test_ca_bundle_offline.py` 23/0；
  源码版 18701 `_verify_ca_bundle.py` 10/0，B站 probe ok=True 15 档；
  四站 favicon 全 200；打包版删 _MEI/certifi 与持久副本后仍恢复，详见下方验收记录）

---

### F-21 番剧/单集页不再被误判成「登录态失效」

- **冻结**：2026-11-08（复现番剧页批量抓取时发现：页面明明正常打开，
  平台登录中心却把 B站 标成「已失效」，点重登也没用）
- **现象**：抓 `https://www.bilibili.com/zh-Hans/bangumi/play/ep810642`，
  warning 是「页面被跳转到「<和请求完全相同的 URL>」，没有停在目标页」，
  `/api/platforms/status` 里 bili = `invalid`，`login_invalid.json` 被写下。
- **根因（三层，都要修）**
  1. `_same_target_page` 只把请求侧路径转小写、最终 URL 侧保留原样，
     `zh-Hans` 对 `zh-hans` 永远不等 —— **同一个 URL 和自己比都判 False**。
  2. 这句提示里含「请到平台登录中心登录」，而 `looks_like_auth_error` 靠
     「登录」二字判定 auth 错误 —— 我们自己的建议文案反过来当成了证据（自证循环）。
  3. `login_invalid.json` 一旦写入就一直有效（`invalid_ts >= login_ts`），
     一次误判会把平台钉死到下次登录刷新。
- **修法**：两侧都折小写再比 + 语言前缀白名单（`/zh-Hans/`、`/en/` 加了/剥了都算同页，
  `/tv/123` 不算）；`looks_like_auth_error` 见到「没有停在目标页」直接返回 False；
  失效标记加 `code`，`redirect` 类只活 15 分钟（`INVALID_TTL`），
  老标记按 reason 里的「没有停在目标页」认成 redirect 一并过期。
- **复测（Sam 的实际操作）**：粘番剧/单集地址抓一次 → ① 不再出现「登录态已失效」；
  ② 平台登录中心里该平台仍是「已登录」；③ `data/platform_profiles/<平台>/login_invalid.json`
  里没有含「没有停在目标页」的新标记。
- **自动化**：`_same_target_page` 11 条离线用例（同页/语言前缀/验证页/登录页/首页/不同视频）；
  `platform_login.status` 四条 TTL 用例（redirect 20 分钟前 → ready，auth 20 分钟前 → invalid）。
- **最近验证**：2026-11-08 PASS（`_verify_v305.py` 17/0；番剧页 batch 不再带登录失效 warning）

### F-22 同一部片的多 CDN 镜像 / 多码率只出一条

- **冻结**：2026-11-08（番剧页批量抓 1 集，结果里出现 16 条「不同视频」）
- **现象**：`ep810642` 一集的 16 条地址全指向同一个 cid `1401558311`，
  只是 CDN 主机不同（estgcos / estgoss / mirrorcos / mirroralib / mirrorhwb…）、
  档位号不同（400/416/448/464）。知乎问题页同理：同一部片 HD/SD/hevc 三份，
  一个问题页列出 14 条同一部片。
- **根因**：`_video_variant_info` 只认两种档位命名（`240P_1000K_<id>.mp4` 前缀式、
  `video_720p.mp4` 后缀式），`<id>-<轨道>-<档位>.mp4` 和「UUID 开头 + 档位在目录」都不认。
- **修法**：加两条归并键 —— `bid:<id>-<轨道>`（第三段 100–4320 才当档位，避免吃掉分片序号）、
  `uuid:<文件名开头的 UUID>`（清晰度从 `/HD/` `/SD/` `/hevc/HD/` 目录猜，只影响选哪份）。
  `extract_from_json.add()` 在没有 `uri`/`video_id` 字段时也退回用这个键归并。
- **复测**：抓番剧单集 → 结果 1 条；抓知乎问题页 → 同一部片不再重复出现。
- **自动化**：10 条镜像地址 `collapse_video_variants` 后剩 1 条（保留 464 档）。
- **最近验证**：2026-11-08 PASS

### F-23 站点未读角标不进标题

- **冻结**：2026-11-08（知乎问题页结果每条都叫「(9 封私信) 身材比例 - 知乎」）
- **根因**：站内未读角标被拼进 `document.title`，`page_title()` 原样返回，
  再被当成每条媒体的标题套下去。
- **修法**：`_clean_title` 开头剥掉 `(9 封私信)` / `(12条消息)` / `（3 条通知）` 这类前缀；
  只认带消息类量词的形态，`(9) 第一章` 不动。
- **复测**：抓有未读消息的知乎账号 → 结果标题不带「(N 封私信)」。
- **自动化**：`_clean_title` 8 条用例（含 `(9) 不应剥掉` 反例）。
- **最近验证**：2026-11-08 PASS

### F-24 会员只拿到试看片段时必须说出来

- **冻结**：2026-11-08（下「人间世 第二季第 8 集」，交付 15.7MB / 6:00 的文件，
  界面显示成功；正片 51:15）
- **现象**：文件名是「视频下载 [2eb6bd8e].mp4」，note 只有「已用内置浏览器解析出真实播放地址」，
  没有任何「这是试看」的说明。另有一次换源卡在单条 CDN 上 33 分钟，
  任务一直 `running`、没有进度说明。
- **根因**
  1. 「浏览器解析真实播放地址 → 直接下载 → 立刻 return」这条分支**跳过了**
     后面的试看时长核对；HLS 分支同样。
  2. `task_manager` 会给空标题塞默认值「视频下载」，标题回填逻辑只判 `if not title`，
     于是永远不触发，落盘名退化。
  3. 换源要连试几个资源站，没有总预算，单条卡住就整任务挂死。
- **修法**：新增 `_preview_guard()`，HLS / 直链 / 浏览器解析三个出口和 yt-dlp 主路径
  全部经过它（试看 → 换源 → 换不成返回带 ⚠️ 的说明）；`_PLACEHOLDER_TITLES`
  把「视频下载」这类占位名当空标题，回填「<剧名> 第N集 <集名>」；
  `_unlock_with_budget()` 给整段换源 900 秒预算（`MEDIA_DOWNLOADER_VIP_BUDGET` 可调），
  到点置 cancel_event 让下载层收手。
- **复测（Sam 的实际操作）**：下同一集 → 文件名应是
  `人间世 第二季 第8集 儿科医生 [810642].mp4`；note 必须写明
  「⚠️ 只拿到试看片段（6:00 / 正片 51:15）」；任务不能一直挂在 running。
- **自动化**：`_preview_guard` 三分支（换源失败带 ⚠️ / 换源成功 / 完整版不动作）；
  `_unlock_with_budget` 超时用例（2 秒预算 → 2.2 秒返回、cancel_event 已置位）。
- **最近验证**：2026-11-08 PASS（端到端：42 秒完成，文件名与 note 均符合）

### F-25 单集页的提示不说成「主页」

- **冻结**：2026-11-08（番剧播放页的提示是「这是一个B站主页，已用内置来源展开 1 条作品」）
- **根因**：`_expand_via_platform_source` 的文案写死「主页」，没区分单作品页与列表页。
- **修法**：`is_single_work_url()`（`/bangumi/play/ep…`、`/video/<id>`、`watch?v=`、
  `zvideo/`、`explore/` 等）→ 文案改成「这是B站的一个作品页…解析出 N 条可下载地址」；
  `list_author_items` 的 note 同步改成「展开B站作品页」。
- **复测**：粘番剧地址抓一次 → warning 里是「作品页」，不是「主页」。
- **自动化**：`is_single_work_url` 10 条用例（抖音/YouTube/知乎视频 = True；
  空间页/微博主页/知乎问题页 = False）。
- **最近验证**：2026-11-08 PASS

### F-26 文件名带 `#` 时预览不再 403

- **冻结**：2026-11-08（`_verify_hero_ui.py` 连续三次挂在 6 个
  `403 /files/crawl/douyin.com/...`，URL 结尾是空格、没有扩展名）
- **现象**：下载库 / 首页示例区的抖音、微博卡片全是裂图，请求路径是
  「标题前半截 + 空格」，服务端按「仅允许访问媒体文件」回 403。
- **根因**：`/api/downloads/tree` 直接拼 `f"/files/{rel}"`，而抖音/微博标题里
  大量带 `#`。浏览器把 `#` 当片段分隔符，只把前半截发出去 —— 落点是
  `…可爱ฅ՞Ⱉ՞ฅ `（无扩展名），必然 403。多字节字符被截断后还会显示成乱码。
- **修法**：`href` 逐段 `quote()`（`#` → `%23`），扩展名和整段文件名都保住。
- **复测**：下载库里任意带 `#` 的抖音/微博文件，缩略图/视频预览应正常显示。
- **自动化**：`_verify_hero_ui.py` ALL PASS（`ERRS [] BAD_RESP []`）；
  `_verify_v304.py` 46/0 覆盖版面。
- **最近验证**：2026-11-08 PASS

### F-27 过期缩略图不再请求，代理透传上游状态码

- **冻结**：2026-11-08
- **现象**：小红书历史记录隔天再看，缩略图全是 502；`/proxy` 日志里一片
  `源站返回 403`，但对外报的是 502，看起来像代理或 CA 坏了。
- **根因**：
  1. 台账把带签名的 CDN 缩略图地址长期保存，签名过期后前端必然请求失败；
  2. `/proxy` 把上游所有 `>=400` 一律包成 502，丢掉了「过期(403)/已删(404)」的区别。
- **修法**：`history.recent()` 对超过 24 小时（`_THUMB_TTL_SEC`）的记录不再下发
  缩略图，前端直接显示占位；`/proxy` 对上游 4xx 原样透传，只有 5xx 才算 502。
- **复测**：隔一天打开首页 / 下载库，旧记录显示占位而不是裂图；
  `/proxy?url=<过期图>` 返回 403（不是 502）。
- **最近验证**：2026-11-08 PASS（`_verify_hero_ui.py` / `_verify_ca_bundle.py` 10/0）

### F-28 带语言前缀的 B 站地址不再被判「不是受支持的视频页面」

- **冻结**：2026-11-09（00:30 端到端实测：`/zh-Hans/bangumi/play/ep810642`
  提交后 4 秒失败，报「该链接不是受支持的视频页面」；同一时刻把 `/zh-Hans/`
  去掉，同一条 ep 立刻下到 6:00 试看片并给出提示）
- **现象**：从浏览器地址栏复制出来的 B 站番剧链接常带语言段（`/zh-Hans/`
  是登录态下 B 站自己加的）。同一集用带前缀的地址下载直接失败，用户会以为
  这集不能下。
- **根因**：yt-dlp 的 bilibili extractor 正则只认紧贴域名的
  `/bangumi/play/ep<id>`，路径第一段是语言码时匹配不上，回 `unsupported url`，
  被 `_friendly_error()` 翻成「不是受支持的视频页面」。这跟 F-21 是同一类问题
  （语言段被当成真路径），只是一个在抓取判据里、一个在下载解析里。
- **修法**：`app/video_downloader.py` 新增 `_strip_lang_path()`，在
  `_normalize_media_url()` 末尾调用：只对白名单站点（当前 `bilibili.com`）、
  且第一段在白名单语言码里才剥；`/tv/123`、单段 `/zh-Hans`、别的站点、
  `evilbilibili.com` 这类混淆域名都不动。
- **复测**：用带 `/zh-Hans/` 的 ep 地址在下载页提交，应正常开始下载并给出
  试看/完整片的真实结论，不再报「不是受支持的视频页面」。
- **自动化**：`test_preview_guard_offline.py` 新增「语言前缀归一」9 项（39/0）；
  端到端 `_e2e_vip_final.py`（关掉自动换源）拿到
  `人间世 第二季 第8集 儿科医生 [810642].mp4` + 试看提示。
- **最近验证**：2026-11-09 PASS

---

## 版本验收记录（Sam 口述的结论，不拿我自测的用例顶账）

### 2026-11-08 16:50 · V3.0.4 测试版

- **Sam 原话**：已测常见H站、国内登录平台（知乎视频暂未）、 国外平台待验。
- **已测通过**：常见 H 站、国内登录平台。
- **明确未测**：知乎视频（暂未测，**不计 PASS**）；国外平台（待验，**不计 PASS**）。
  这两块后续要单独复测，本条记录不构成它们的验收依据；F-15 / F-16 属自动化回归通过，
  不等于 Sam 对知乎视频的用户验收。
- **对应代码基线**：`9ec1e27`（本条记录不改任何功能代码）。
- **对应产物**：`_dist_ui/MediaDownloader_TEST.exe`，89,064,363 B，
  SHA256 `1106514a9111d797934f185e656fec035f4a293e3430b0aab2fdb4e0f8174882`，
  运行实例 18848，`APP_VERSION=3.0.4`。
- **交付前自动化回归**：2026-11-08 16:5x 对 18848 打包版跑全量，
  日志 `_reg_v304_signoff.log`。v304 46/0、home 18/0、hero / filter ALL PASS、
  frozen 8/0、pkgui / pkgui2 PASS、isolation `[PASS]`、7 个离线用例全 PASS
  （20/0、35/0、15/0、17/0 及其余 ALL PASS）、`test_bare_url_ui.py` 21/0。
- **本轮唯一 FAIL（环境项，不是功能回退）**：`_verify_v305.py` 17 通过 / 1 失败，
  失败项是「bili 判定为已登录（0 条凭据）」，状态 `expired`。
  证据：`platform_profiles/bili/cookies_from_profile.txt` 里 `SESSDATA` 与
  `DedeUserID` 的 `expires=1794120785`（2026-11-08 14:53），跑回归时
  `date +%s=1794128139`（16:55），已过期 7354 秒；profile 内
  `Default/Network/Cookies` 的 `SESSDATA` 也是同一个过期时间，说明是站点侧
  会话自然到期，不是误写 `login_invalid.json`（该文件不存在），也不是本轮代码改动。
  11:36 那份打包产物的 v305 是 18/0，代码基线同为 `9ec1e27`。
- **恢复路径**：平台登录 → B站 → 重新登录（需 Sam 本人扫码）；
  重登后复跑 `_venv/Scripts/python.exe _verify_v305.py http://127.0.0.1:18848`
  应回到 18 通过 / 0 失败。**不改判据、不清标记、不把这一项记成 PASS**。

### 2026-11-08 19:20 · F-20 CA 固化修复交付验收

- **对应产物**：`_dist_ui/MediaDownloader_TEST.exe` 89,073,650 B（19:15 重建），
  实例 18848；持久 CA `DATA_ROOT/certs/cacert.pem` 235,863 B，
  `source=persistent`；`/api/health` 的 `ca_bundle` 字段可直接核对。
- **动手前基线**：18848（CA 已丢）hero 两项 FAIL，日志里 8 条
  `500 /proxy?url=https://...`（B站 / 抖音 / 快手 / 微博图床），
  同一时刻 18849（CA 完整）全 200。这就是 F-20 的复现证据。
- **交付前全量回归（两版各跑一遍，全 PASS）**：
  - 打包版 18848：v305 17/0、v304 46/0、home 18/0、hero / filter ALL PASS、
    frozen 8/0、pkgui 11 个模块全 OK（含 `app.ca_bundle` 7/7）、pkgui2 4 个资源全 OK、
    isolation `[PASS]`、8 个离线用例全 PASS（新增 `test_ca_bundle_offline.py` 23/0）、
    `test_bare_url_ui.py` 21/0、`_verify_ca_bundle.py` 10/0、`_verify_direct_name.py` PASS。
    日志 `_reg_pkg_ca_final.log`。
  - 源码版 18701：同组全 PASS，日志 `_reg_src_ca_final.log`。
- **加强验收（不重启自愈）**：删掉运行中实例的 `_MEI000046082/certifi/`
  **和** `DATA_ROOT/certs/cacert.pem` 后，`_verify_ca_selfheal.py` 7/0 ——
  抖音 / 小红书 / 快手 / B站 四站图片代理仍 200，health 显示
  `source=windows-root-store`、71,968 B（系统根证书导出的兜底 CA）。
  随后把标准 certifi bundle 235,863 B 恢复回持久目录并重启实例，
  source 回到 `persistent`，`_verify_ca_bundle.py` 10/0（B站 yt-dlp probe
  ok=True、15 档清晰度，无证书类错误）。
- **仍未由 Sam 用户验收**：知乎视频、国外平台（沿用 16:50 那条记录，不计 PASS）。

### 2026-11-08 23:56 · V3.0.4 补丁包（F-21 ~ F-27）交付验收

- **对应产物**：`_dist_ui/MediaDownloader_TEST.exe`，89,083,164 B，
  SHA256 `0049cf84364c285f7d763f4a5d8541da7441fca79847a7ee94da45bc0d8a5c0c`，
  构建时间 2026-11-08 23:56；打包版实例 18848，源码版实例 18849，
  两版同为 `APP_VERSION=3.0.4`、同一 `DATA_ROOT`。
  > 本条产物已由下面 00:29 的重建版取代（补了 `_preview_scope` 与离线门禁），
  > 拿安装包请用 00:29 那条的 SHA256。
- **对应代码基线**：`4a53abe`（F-21~F-27 + 本条台账），无未提交改动。
- **本轮冻结的七条**（都在本节之前的冻结项清单里有复现方式和验收方式）：
  F-21 番剧/单集页不再误判登录态失效（markInvalid 自证循环 + 无 TTL）、
  F-22 同一部片多 CDN 镜像 / 多码率只出一条、F-23 站点未读角标不进标题、
  F-24 会员只拿到试看片段必须说出来、F-25 单集页提示不说成「主页」、
  F-26 文件名带 `#` 时预览不再 403、F-27 过期缩略图不下发 + 代理透传上游状态码。
- **交付前全量回归 · 打包版 18848（日志 `_reg_v312_signoff.log`）**：
  `_verify_v305` 17/0、`_verify_v304` 46/0、`_verify_frozen_now` 8/0、
  `_verify_ca_bundle` 10/0、`_verify_home_ui` 18/0、
  `_verify_hero_ui` ALL PASS（`ERRS [] BAD_RESP []`）、`_verify_filter_scope` ALL PASS、
  `_verify_pkg_ui` PASS、`_verify_pkg_ui2` PASS；
  9 组离线用例全 PASS：filter_scope / crawl_links / batch_gate ALL PASS、
  isolation `[PASS]`、dedupe 20/0、zhihu_video 35/0、direct_filename 15/0、
  bare_url 17/0、ca_bundle 23/0；`test_bare_url_ui` 21/0。
- **源码版 18849 同组复跑（日志 `_reg_src_v312_signoff.log`）**：v305 17/0、
  v304 46/0、frozen 8/0、ca_bundle 10/0（B站 yt-dlp probe ok=True / 15 档）、
  home 18/0、hero ALL PASS、filter ALL PASS、bare_url_ui 21/0；
  9 组离线用例同打包版一致（ALL PASS / 20-35-15-17-23 全 0 失败）。两版结论一致。
  **这一遍是重启后跑的**：原 18849 实例起于 23:16，而 `app/main.py` 改到 23:54，
  `launcher.py` 没带 reload，那个进程跑的是旧代码 —— 停掉（PID 17388 / 9816）后
  用同一 `MEDIA_DOWNLOADER_DATA_DIR` 在 00:10 重启（PID 13180）再跑。
- **端到端证据（会员试看，F-24）**：番剧 `ep_id=810642` 实下得到
  `人间世 第二季 第8集 儿科医生 [810642].mp4`，note 为
  `⚠️ 只拿到试看片段（6:00 / 正片 51:15），换源也没拿到完整版；这个文件不是完整正片。`
  文件名不再回退成 `视频下载 [指纹].mp4`。
- **本轮新踩的坑，记在这里免得下次重犯**：
  1. PyInstaller 增量构建会吃缓存 —— 改完源码要 `touch` 改过的文件再 `--clean` 之外，
     还要核对产物 mtime，12 秒跑完的构建基本等于没重新打包。
  2. `_same_target_page` 先只把请求侧折小写，自己跟自己比能返回 False；
     现在两侧都折，并且 `/zh-Hans/` 这类语言段用 `_LANG_SEGS` 白名单单独处理，
     别用「剥首段」的通配写法（会把 `/tv/123` 吃成 `123`）。
  3. `_preview_guard` 的**判定**不受 `_vip_should_try` 控制，只有「换源」这个动作受控；
     关掉换源仍然要出提示，否则用户照样拿到 6 分钟片段还以为是整集。
  4. 文件名里带 `#`，浏览器当片段分隔符，`/files/` 的 `href` 必须逐段 `quote()`。
  5. **跑回归前先核对实例启动时间**：`launcher.py` 没有 reload，实例起得比源码改动早，
     回归测的就是旧代码（本轮差点拿 23:16 的进程给 23:54 的代码背书）。
     判据：进程 `StartTime` 必须晚于 `app/*.py` 的最新 mtime —— 打包版用
     `Get-Process MediaDownloader_TEST | Select StartTime`，源码版用
     `Get-CimInstance Win32_Process | ? {$_.CommandLine -match 'launcher.py'}`。
- **仍未由 Sam 用户验收**：知乎视频、国外平台（沿用 16:50 那条记录，不计 PASS）。
  自动化回归覆盖的是 F-15 / F-16 的判据，不等于用户验收。

### 2026-11-09 00:29 · 重建交付（F-24 守卫拆出 `_preview_scope` + 补离线用例）

- **为什么重建**：23:56 那版里 `_preview_guard` 的「要不要核对」条件混在
  `_vip_should_try` 同一段里，没有独立入口可测；而且 9 组离线用例里根本没有
  `test_vip_offline.py`（F-17/F-24 的 72 项），等于最该守的两条没进门禁。
- **改了什么**：
  1. `app/video_downloader.py` 把范围判据抽成 `_preview_scope(url)`：
     非会员平台 / B 站普通投稿返回 False，其余 True；`_preview_guard` 只调它，
     `_vip_should_try` 仍然只决定「换不换源」。行为不变，只是判断与动作分开，
     可以离线断言。
  2. 新增 `test_preview_guard_offline.py` 30 项：核对范围 6 项、时长文案 6 项、
     占位标题 8 项、**守卫 × 换源开关 10 项**（关掉换源仍出 ⚠️、不去调换源、
     完整正片不出提示、空文件不碰网络、普通投稿跳核对）。
  3. `test_vip_offline.py`（72 项）与 `test_preview_guard_offline.py`（30 项）
     正式写进上面「自动化回归入口」，离线用例从 9 组变 11 组。
- **对应产物**：`_dist_ui/MediaDownloader_TEST.exe`，89,083,529 B，
  SHA256 `e4c72611e1e514adfd9158ccdd1d011922e00db148fed77a3b74f83e403a1601`，
  构建于 2026-11-09 00:19（`_build_v317.log`，BUILD_EXIT=0）。
  23:56 那版（89,083,164 B / `0049cf84…`）已被本版取代，不再作为交付物。
- **实例**：打包版 18848 进程起于 00:20:27；源码版 18849 进程起于 00:20:15，
  都晚于最后一次源码改动（`app/video_downloader.py` 00:16:48）—— 按本页坑 5 核过。
- **交付前全量回归**：
  - 打包版 18848（日志 `_reg_v317_signoff.log`）：v305 17/0、v304 46/0、
    frozen 8/0、ca_bundle 10/0、home 18/0、hero ALL PASS、filter ALL PASS、
    pkg_ui PASS、pkg_ui2 PASS、bare_url_ui 21/0；11 组离线用例全 PASS
    （dedupe 20/0、zhihu 35/0、direct_filename 15/0、bare_url 17/0、
    ca_bundle 23/0、preview_guard 30/0、vip ALL PASS，其余 ALL PASS）。
  - 源码版 18849（日志 `_reg_src_v317_signoff.log`）：同组逐项一致，全 PASS。
- **仍未由 Sam 用户验收**：知乎视频、国外平台（不计 PASS）。
  > 本版已被 00:45 那版取代（F-28 语言前缀），安装包用 00:45 的 SHA256。

### 2026-11-09 00:45 · 重建交付（F-28 语言前缀 + 39 项守卫门禁）

- **为什么又重建**：00:30 用带 `/zh-Hans/` 的真实番剧地址跑端到端，暴露 F-28
  （yt-dlp 认不出带语言段的 bangumi 地址，界面报「不是受支持的视频页面」）。
  这条不修，用户从地址栏复制的链接就直接下不了。
- **改了什么**：`_strip_lang_path()` + 在 `_normalize_media_url()` 里调用（白名单
  站点 + 白名单语言码）；`test_preview_guard_offline.py` 增加「语言前缀归一」9 项，
  总数 30 → 39。
- **对应产物**：`_dist_ui/MediaDownloader_TEST.exe`，89,083,082 B，
  SHA256 `e01297988219b52d650b0e10bbbaa17deaaba9e86b378d0fe9e2f9cd8528243c`，
  构建于 2026-11-09 00:35（`_build_v318.log`，BUILD_EXIT=0）。
  00:19 那版（`e4c72611…`）与 23:56 那版（`0049cf84…`）都被本版取代。
- **实例**：打包版 18848 进程起于 00:35:13/17；源码版 18849 起于 00:35:23，
  源码最后改动 00:32:43 —— 启动时间都在改动之后。
- **交付前全量回归**：
  - 打包版 18848（日志 `_reg_v318_signoff.log`）：v305 17/0、v304 46/0、
    frozen 8/0、ca_bundle 10/0、home 18/0、hero ALL PASS、filter ALL PASS、
    pkg_ui PASS、pkg_ui2 PASS、bare_url_ui 21/0；11 组离线用例全 PASS
    （dedupe 20/0、zhihu 35/0、direct_filename 15/0、bare_url 17/0、
    ca_bundle 23/0、preview_guard 39/0、vip ALL PASS，其余 ALL PASS）。
  - 源码版 18849（日志 `_reg_src_v318_signoff.log`）：同组逐项一致，全 PASS。
- **端到端（打包版 18848，关掉自动换源）**：
  `https://www.bilibili.com/zh-Hans/bangumi/play/ep810642?from_spmid=666.25.episode.0`
  → `completed`，文件 `人间世 第二季 第8集 儿科医生 [810642].mp4`（15,788,305 B），
  note `⚠️ 只拿到试看片段（6:00 / 正片 51:15），换源也没拿到完整版；这个文件不是完整正片。`
  `_e2e_vip_final.py` RESULT PASS。**同一条地址在 00:30 还是「不是受支持的视频页面」，
  这次是同一台机器、同一账号、同一实例的对照结果。**
- **实例后况（07:36 记）**：带窗口的 18848 进程在无人值守时自行退出（桌面壳随
  交互会话结束），07:35:57 用 `--no-browser` 重启，仍是同一个 exe（SHA 未变），
  `/api/health` 正常。复测直接开 `http://127.0.0.1:18848`；要窗口界面就手动双击 exe。
- **仍未由 Sam 用户验收**：知乎视频、国外平台（不计 PASS）。

### 2026-11-09 · V3.0.4 发布到 GitHub（含 Release 文件）

- **仓库**：`coolaigit/media-downloader` 的 `main`。本地 68 个提交与远端 main
  没有共同祖先（2026-11-03 改写历史后重新导入），所以**没有强推**：
  `publish_release_v304.py` 用 Git Data API 在远端当前树上叠一个提交，
  远端历史完整保留。发布提交 `e8b4646162af65496f3b1a4edaf1f037fef82758`，
  共写入 72 个文件（26 改 / 46 新增）。
- **Tag / Release**：`v3.0.4` → `e8b46461…`；
  https://github.com/coolaigit/media-downloader/releases/tag/v3.0.4
  （非 draft、非 prerelease）。
- **Release 文件**：`MediaDownloader_TEST.exe`，89,083,082 B，
  SHA256 `e01297988219b52d650b0e10bbbaa17deaaba9e86b378d0fe9e2f9cd8528243c`。
  从 Release 下载链接重新下载 89,083,082 B 复算 SHA256，与本地产物 **MATCH**
  （`state=uploaded`，下载直链同页可见）。
- **Release 正文**：`RELEASE_NOTES_V3.0.4.md`，含 F-01~F-28 摘要、两版全量回归、
  已知限制，以及 **deepseek harness 会话
  `session-c4d7b847-0d3a-4922-b659-cde96a34b050` 的更新记录说明**：
  会话正文不在本仓库，按可查证提交整理出 `2cf97ef` → `05f71d9` → `fe8eaa8`
  （一键配 Google 官方搜图 API）→ 同日 `531acaf` 因官方对新客户关闭而整体回退
  → 保留 render/searx 兜底这条完整链，ID 只作溯源，不推测会话内容。
- **同步范围**：本地索引里的文本文件；有意保留远端的 `.gitignore`、
  `.gitattributes`（开源瘦身规则）与 `licenses/`（第三方许可证原文），未覆盖。
- **顺带补了一个入库漏洞**：`test_preview_guard_offline.py`（39 项）与
  `_e2e_vip_final.py` 一直被 `test_*.py` / `_*` 忽略规则吞掉，`902d6a5` 与
  `aae5124` 两次提交实际都没把它们带进版本库 —— 台账写着回归入口、仓库里却没有文件。
  本次放行并随发布一起入库。
- **发布后复核**：tag 与发布提交一致；Release body 含会话 ID、产物 SHA256、
  `fe8eaa8` / `531acaf` / F-28；抽查 7 个远端文件的 blob SHA 与本地索引逐一相同
  （`.gitignore` 为有意保留的远端版）。
- **仍未由 Sam 用户验收**：知乎视频、国外平台（不计 PASS）。
