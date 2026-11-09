# Google 搜图接入 & 会员视频换源（2026-11-04）

本文记录这次新增的两块能力：**Google 关键词搜图** 和 **会员视频「试看 → 完整版」兜底**。
内容全部来自本机实测，不是设计稿。

---

## 一、Google 搜图：两条通道，自动降级

### 1.1 以前为什么接不上

本机出口是机房 IP（实测 `45.66.157.223`），Google 对这类 IP 的处理是**三连封**：

| 尝试 | 实测结果 |
|---|---|
| 纯 HTTP 抓 `tbm=isch` | 93 KB 的 `enablejs` 空壳，0 张图 |
| `gbv=1` 老版基础 HTML | 2.5 KB，正文是 "Your browser isn't supported anymore" |
| `/images/async` 老 AJAX 接口 | 404 |
| Playwright 渲染 | 302 到 `/sorry/index`，页面直接写出「系统检测到您的计算机网络存在异常流量」+ 出口 IP |
| CSE `element/v1` 无 key | 403 `Sorry...` |
| 公共 SearXNG 实例 | 绝大多数 429（本机 IP 被公共实例限流） |
| 官方 Custom Search JSON API | **2026 年起对新客户关闭**（官方原文 "closed to new customers"，存量客户 2027-01-01 停服），新项目拿不到 key |

结论：**Google 不存在「一个 HTTP 请求就能拿结果」的路线了**（2024 年后彻底 JS 化）；
官方 API 也走不通（不是代码问题，是 Google 不再给新客户开）。所以只保留两条真能用的通道。

### 1.2 两条通道

| 通道 | 实现 | 适用场景 | 关键点 |
|---|---|---|---|
| `render` | `search_google_render()`，Playwright 渲染 + DOM 提取 | 住家 IP / 干净节点 | 从 `a[href*="imgurl="]` 直接取「原图 + 来源页 + 缩略图」，比正则扫 1 MB HTML 稳；支持单独配代理 |
| `searx` | `search_google_searx()`，并发问 SearXNG 实例 | 手头有可用实例 | 实例服务器代抓 Google，本机 IP 封不封无所谓；**留空不启用**（内置公共实例实测基本都 429） |

自动选路（`_transport_order`）：

```
默认（auto）            →  render → searx
google_transport 强制   →  只跑指定的那条
```

两条全挂时，错误信息会把**每条通道的真实原因**拼在一句里：

```
Google 全部通道失败 → render: Google 判定当前出口 IP 存在异常流量（/sorry 验证页）… ｜ searx: SearXNG 全部失败：…
```

### 1.3 怎么配

设置页 →「Google 搜图接入」：

- **渲染通道代理**：`http://127.0.0.1:7890` 这种，只作用于 Google 渲染通道，不影响其它站点。
- **通道选择**：自动 / 只用渲染 / 只用 SearXNG。
- **SearXNG 实例**：自建或可用的公共实例，逗号分隔；留空不启用。

住家网络一般配好 Chrome 内核就能直接渲染，不需要代理。
机房 / 被 Google 判为异常流量的节点，配一个干净代理（渲染通道代理）即可。

> **2026-11-05 实测**：官方概览页已写明 Custom Search JSON API 对**新客户关闭**，
> 新账号在 Cloud Console 启用会被拒 —— 三分钟建 key 的流程对新项目不成立。
> 所以 API 通道与「API Key / cx」设置项已整体移除，不再给用户一个配不成的入口。
> 同日实测 `search_google("猫")` 走 render 通道 16.5s 出 5 张图，链路可用。

### 1.4 实测

```
POST /api/search/images {"keyword":"樱花","engines":"google","limit":5}
→ engine_status: {"google":"ok:40·render（其中 35 条与关键词相关）"}
→ 500x750 tsunagujapan.com / 1080x1620 shanwei.gov.cn / 923x1440 allabout-japan.com
```

- 渲染通道在本机可用（`/sorry` 是短时风控，冷却后恢复；必要时换出口或配代理）。
- 过滤：`min_resolution` 直接吃 Google 自带的宽高，**0 次探测请求**。
- 相关性的数字是项目原有的 `relevance.audit` 结果，不是过滤 —— 条目一条不删。

### 1.5 改动点

| 文件 | 改动 |
|---|---|
| `app/search_google.py` | 重写：`search_google_render` / `search_google_searx` / `_transport_order` / `parse_google_imgres` / `parse_google_html` / `_diagnose_render` / `google_status`；移除 API 通道 |
| `app/renderer.py` | `render_page/render_many/_render_async` 增加 `proxy`、`extra_cookies`、`extract_js`、`scroll_wait_ms`；结果加 `js` 字段 |
| `app/image_search.py` | 状态文案追加 `·<engine_detail>`（如 `ok:40·render`） |
| `app/main.py` / `app/store.py` | 设置项、`/api/status` 的 google 块 |
| `web/index.html` / `web/app.js` | Google 设置区（代理 / 通道 / SearXNG）与状态显示 |

## 二、会员视频：试看 6 分钟 → 完整版

### 2.1 先明确一件事

B 站大会员番剧、优酷/爱奇艺会员片，**播放地址本身就是按账号权限裁剪过的**：

- 非会员请求 `pgc/player/web/playurl`，`is_preview=1`，`durl` 只覆盖前 6 分钟；
- 这不是「参数没带对」，改 `qn`/`fnval`/`platform`/`fourk` 都拿不到完整流；
- 也没有服务端漏洞可钻（CDN 只发它被授权的那一段）。

要拿到完整版，只能**换一条不受该平台会员校验约束的链路**。所以这次做的是四层兜底。

### 2.2 四层兜底

| 顺序 | 通道 | 说明 | 实测 |
|---|---|---|---|
| ① | 会员 Cookie 直取 | `vip_cookies/*.txt` 放大会员 Cookie，调 B 站 `playurl` 直接装 DASH（1080P+/4K） | 无会员号，未能实测成功分支；`is_preview` 判定与 DASH/avc 优选逻辑已就位 |
| ② | **资源站换源（主力）** | 页面标题 → 影视资源站（苹果 CMS 采集接口）找同片/同集 → 取它的 m3u8 | **实测通过**，见下 |
| ③ | **海外免费源（YouTube）** | yt-dlp 搜同片/同集 → 标题/季号/集号/时长四重匹配 → 下载 | 需要代理（设置里的「海外源下载代理」）；匹配逻辑离线用例覆盖 |
| ④ | 解析网关（可选） | 用户自备的公共解析接口，从返回页/iframe 里抽 m3u8 | 20 个公共解析站实测**全部只剩广告 iframe**，默认不启用，只在设置里填了才跑 |

海外源匹配规则（`app/alt_sources.py`，通用启发式，不写平台规则）：

- 从单集标题里截出**剧名**再去搜（优酷标题「谷雨 第2集 李阿红…」不能整串搜，否则 0 条）；
- 季号必须一致（S02/第二季），集号必须一致（第 N 集 / EP N / 第 N 期），
  **标题里没有集号的一律不当分集**（否则会把 S02EP06 当第 1 集下回来）；
- 时长与标称时长比对：YouTube 常带片头片尾，容忍 -20% ~ +30%；
- 标题命中「解说/预告/花絮/盘点/切片/reaction」直接排除；官方频道轻微加分；
- 只对会员平台生效，且只在国内资源站找不到时才跑。

触发方式（`download_video` 内自动）：

- **下完核对时长**：`ffprobe` 读实际文件时长，与「页面/API 标称时长」比；
  实际 < 标称 × 0.85 且差值 > 90 秒 → 判定为试看，删掉片段、换源重下完整版。
- **下载直接失败**：只要不是 404/链接本身没内容，会员平台的失败都试一次换源 ——
  包括 412 风控、403、地区限制、登录墙。实测 B 站大会员页在本机 yt-dlp 直接 412，
  错误文本里没有「会员」二字，旧逻辑因此不触发兜底，表现就是「切换源没成功」。
- 资源站全部 CDN 都失败时，最后再试一次海外源。

只对会员平台（bilibili / youku / iqiyi / qq / mgtv / sohu / le / pptv / tudou）生效，
普通站点不会多花一次探测。

### 2.3 资源站（2026-11-05 复测：12 个可用）

| key | 名称 | 状态 |
|---|---|---|
| ikun | 艾坤资源 | ✅ JSON + m3u8 |
| jisu | 极速资源 | ✅ 播放页 → `/index.m3u8` |
| apibdzy | 百度资源 | ✅（2026-11-05 新增） |
| dytt | 电影天堂 | ✅（2026-11-05 新增） |
| ffzy | 非凡资源 | ✅（主域名 + 镜像） |
| bfzy | 暴风资源 | ✅ |
| 360zy | 360 资源 | ✅ 直链 m3u8 |
| lzi | 量子资源 | ✅ |
| wujin | 无尽资源 | ✅（主域名 + 镜像，偶发超时） |
| ruyi | 如意资源 | ✅ |
| zuidazy | 最大资源 | ✅ |
| hongniu | 红牛资源 | ✅（CDN 证书链不完整，下载端会换源） |

已确认不可用、未收入：`heimuer` / `tiankong` / `wolong` / `suoni` / `yayazy` / `maotaizy`
（返回 HTML 或「暂不支持搜索」）、`doubi` / `huya` / `kczy` / `mozhu`（连接失败/超时）。

排序 + 匹配规则（`resource_sites.py`）：

- 片名归一：去《》【】、去「在线观看/高清/完整版/4K/更新至…」、去「第X集」；
- **季号必须一致**：`第X季` / `Season N` / `S02` 冲突重罚 60 分
  （实测修 bug 前《人间世 第二季》会被匹配成《人间世第一季》）；
- 语言版本：中配/国语=cn、粤语=yue、台配=tw，冲突重罚 45 分；
- 相似度：完全一致 100，包含关系 70–95，其余按序列相似度 ×65；
- 年份匹配 ±18，集数存在 +10，剧集备注 +4；
- 标题像「解说/预告/片花」的直接拒绝换源，避免「看解说却下回来一整部剧」；
- 同分按站点优先级；从高分往下找**第一个真有这一集**的候选（排第一的可能是预告片，会被 `pick()` 拒掉）。

### 2.4 实测记录

输入：

```
https://www.bilibili.com/bangumi/play/ep323085   （名侦探柯南 中配 第1话，大会员）
```

元数据（B 站 API，2.8s）：

```json
{"title":"名侦探柯南（中配）","episode":1,"duration":1494.0,"episode_title":"云霄飞车杀人事件"}
```

换源（19.5s，含等待资源站）：

```json
{"ok":true,"backend":"resource-site","site":"极速资源","vod_name":"名侦探柯南（中配）",
 "episode":1,"url":"https://vv.jisuzyv.com/play/7ax465na/index.m3u8"}
```

流探测 + 完整落盘（2026-11-04 23:22 跑完，原始日志在 `%TEMP%\md_stream_debug.log`）：

```
probe: hls-media 1493.12s / 285 个分片        ← 与 B 站标称 1494s 一致
下载:  76,119,583 字节 / 1493.146s / 603.4s
编码:  H.264 720x576 + AAC（ffprobe 实测）
```

也就是说：**页面是大会员的，换源拿到并下完的是完整 24:53 正片，不是 6 分钟试看。**

> ⚠️ 实测踩到的坑：同一批源里，有的 CDN 会在中途断流，ffmpeg 带
> `-reconnect` 时把 EOF 当正常结束（`rc=0`），落盘只有 774s（12:54），
> 看起来「下载成功」其实是半集。所以 `download_resolved` 会**下完核对时长**：
> 低于标称 85% 就删文件、换下一条；同时看门狗盯输出文件大小，
> 连续 120 秒不增长就判卡死。宁可明确报失败，不给半集。

### 2.4.1 2026-11-05 复测（用户给的两条链接）

| 链接 | 元数据 | 换源结果 | 流探测 | 限量下载（30s） |
|---|---|---|---|---|
| B 站 `ep810204` | 人间世 第二季 第1集，标称 3121s | 艾坤资源「人间世第二季」第1集（5 条源） | hls-master 3216.1s / 1594 分片 | 4,918,843 字节，可解码 |
| 优酷 `XjY3NDQwMTcyMA` | 谷雨 第2集，标称 2725s | 极速资源「谷雨」第2集（3 条源） | hls-media 2725.1s / 651 分片 | 9,643,910 字节，可解码 |

两条都命中了**正确的季/集**；优酷流时长与标称 2725s 完全一致。
另外实测了爱奇艺/腾讯视频页面：标题是「片花」或只有站点名时，换源会**明确拒绝**
（不再硬匹配错片）。腾讯视频播放页 HTML 里没有片名，渲染后仍只拿到站点名
（境外 IP + 登录墙），这类页面目前拿不到有效片名，会如实报错而不是下错。

### 2.5 接口

```http
POST /api/vip/resolve
{"url":"https://www.bilibili.com/bangumi/play/ep323085","download":false}
→ 只解析：返回命中的资源站、片名、集数、播放地址（用于核对是否找错片）

POST /api/vip/resolve
{"url":"https://v.youku.com/v_show/id_XXX.html","download":true,"title":"片名"}
→ 解析 + 下载到「视频」目录

GET  /api/vip/status?check=1        # 四层兜底状态 + Cookie 会员态
POST /api/vip/check-cookies         # 只校验 vip_cookies 目录里的 Cookie
```

### 2.6 顺手修掉的几个老问题

1. **孤儿 ffmpeg 收割器会误杀正在下载的 ffmpeg**（`app/win_job.py`）。
   原实现把「活进程集合」写成只含 ffmpeg，而 ffmpeg 的父进程是 python ——
   父进程永远「不存在」，于是每 5 分钟把在跑的 ffmpeg 杀一次，表现就是
   下载永远停在 0 字节的 `.part`。现在活进程集合改为**全系统进程**，
   并且只匹配本项目的 ffmpeg 路径。
2. **流下载看门狗只看「有没有输出」**（`app/stream_downloader.py`）。
   ffmpeg 卡在拉分片时仍会每 0.5 秒吐 `progress=continue`，看门狗永远不触发。
   现在额外跟踪 `out_time`，120 秒不前进就判卡死、掐掉换下一条封装。
3. **「progress 照报、out_time 前进、文件 0 字节」挂 18 分钟**。
   再加一道**输出文件大小看门狗**：连续 120 秒没增长直接 kill。
4. **`_vip_replace_with_full` 里用了局部导入的 `_url_fp`**：换源一成功就
   NameError，整条任务反而失败。已修 + 离线用例覆盖。
5. **《人间世 第二季》被匹配成第一季**：`clean_title` 把季号一起删了。
   现在季号参与检索与打分，冲突重罚 60 分。
6. **取消换源下载不生效**：`download_resolved` 没接 `cancel_event`，取消后还会
   继续试下一条源、甚至去搜 YouTube；视频任务取消又被记成 `failed`。
   现在取消会删分片、几秒内标成 `cancelled`。

---

## 三、已知限制（不粉饰）

- **优酷已实测全链路**：用户给的《谷雨》第 2 集，换源命中极速资源，流 2725.1s，
  30 秒限量下载 9.6MB。爱奇艺/腾讯视频的播放页在境外 IP 下拿不到真实片名
  （HTML 里就是「腾讯视频」/「片花」），程序会**明确拒绝换源**而不是下错片；
  这类页面要等登录态/国内 IP 才能进一步验证。
- **海外源（YouTube）需要代理**：设置里的「海外源下载代理」只作用于 YouTube，
  国内资源站仍直连。YouTube 上的中文剧集多为搬运/合集，画质和音轨不由我们控制；
  匹配只保证「季/集/时长对得上」，不保证是官方源。
- **资源站画质/音轨不可控**：换源拿到的是资源站自己压的版本，通常 720P~1080P，
  不一定等于原平台的 4K/HDR。要原画质只能走 ① 会员 Cookie。
- **B 站大会员 Cookie 直取**：需要用户自己提供 Cookie（`vip_cookies/*.txt`）；
  没有会员号时这条会自动跳过，不会假装成功。
- **B 站普通投稿在本机 412**：机房 IP 风控，属环境问题；VIP 换源不依赖 B 站登录态。
- **Google 渲染**：机房 IP 会周期性触发 `/sorry`，这是 Google 侧策略，
  程序只能如实报告并降级；配渲染代理或 SearXNG 实例可绕开。

---

## 四、验证清单

```bat
:: 1) 离线用例（不联网）
_venv\Scripts\python.exe test_google_offline.py
_venv\Scripts\python.exe test_vip_offline.py

:: 2) 单个会员页一键诊断（走的是网页端同一套函数）
_venv\Scripts\python.exe probe_vip.py "https://www.bilibili.com/bangumi/play/ep323085"
::    加上 --download --limit 60 可以只下前 60 秒，验证链路能出片

:: 3) 起服务
set MEDIA_DOWNLOADER_DATA_DIR=%CD%\_data
set MEDIA_DOWNLOADER_DOWNLOAD_DIR=%CD%\_downloads
_venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8848

:: 3) Google 单引擎
curl -s -X POST http://127.0.0.1:8848/api/search/images ^
  -H "Content-Type: application/json" -d "{\"keyword\":\"猫\",\"engines\":\"google\",\"limit\":5}"

:: 4) 会员换源（只解析）
curl -s -X POST http://127.0.0.1:8848/api/vip/resolve ^
  -H "Content-Type: application/json" -d "{\"url\":\"https://www.bilibili.com/bangumi/play/ep323085\"}"

:: 5) 单文件测试版（隔离目录 / 端口 18848+，不动已安装的 V3.0.3）
_venv\Scripts\python.exe -m PyInstaller --noconfirm ^
  --distpath _dist_test --workpath _build_test MediaDownloader_Test.spec
:: 产物：_dist_test\MediaDownloader_TEST.exe
```
