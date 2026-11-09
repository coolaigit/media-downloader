# -*- coding: utf-8 -*-
"""批量抓取链接选路离线用例（不联网）。

覆盖 2026-11-06 修掉的两类回归：
  1) 搜索/结果页被误判成「单条内容页」→ 跳过 yt-dlp 列表展开，整批变成 240P 直链；
  2) 渲染列表页把 `/c/Slug-14` 这类分类页当成视频页 → 整批链接下不了。
判据必须只靠地址形态，不含站点名；用例里也用通用域名复验同一形状。
跑法：./_venv/Scripts/python.exe test_crawl_links_offline.py
"""
from __future__ import annotations

import sys
from pathlib import Path

from app import batch_crawler as bc
from app.downloader import download_file
from app.extractor import MediaItem
from app.video_downloader import (_choose_ts_group, _hls_seg_entry,
                                  _index_ts_playlist, _parse_hls_master,
                                  _parse_hls_segments, _ts_integrity_error,
                                  _ts_plan, has_specific_extractor)

ROOT = Path(__file__).resolve().parent
FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = ""):
    if cond:
        print(f"  ok   {name}")
    else:
        FAILED.append(name)
        print(f"  FAIL {name} {detail}")


def test_collection_page():
    print("[集合页识别：搜索页 / 内容页]")
    list_urls = [
        "https://cn.pornhub.com/video/search?search=%E5%A4%9C%E5%BA%97",
        "https://www.pornhub.com/video/search?search=nightclub",
        "https://www.youtube.com/results?search_query=test",
        "https://www.bilibili.com/search?keyword=test",
        "https://www.tiktok.com/@kvi.lighting",
        "https://www.xvideos.com/c/Ass-14",
        "https://www.xvideos.com/?k=ass",
        "https://example.com/videos/search?q=abc",
    ]
    for u in list_urls:
        check(f"集合页 {u[:58]}", bc._looks_like_collection_page(u) is True, u)

    content_urls = [
        "https://www.pornhub.com/view_video.php?viewkey=689236c3afffa",
        "https://www.bilibili.com/video/BV1xx411c7mD",
        "https://example.com/video/7539022/title",
        "https://www.youtube.com/watch?v=abcdefghijk",
        "https://example.com/watch/123456",
    ]
    for u in content_urls:
        check(f"内容页 {u[:58]}", bc._looks_like_collection_page(u) is False, u)


def test_video_detail_link():
    print("[渲染列表链接筛选：分类页 / 视频页]")
    base = "https://www.xvideos.com/c/Ass-14"
    bad = [
        "https://www.xvideos.com/c/Ass-14#",
        "https://www.xvideos.com/c/Arab-159",
        "https://www.xvideos.com/c/Fucked_Up_Family-81",
        "https://www.xvideos.com/tags/ass",
        "https://www.xvideos.com/best",
        "https://www.xvideos.com/?k=ass&top",
        "https://www.xvideos.com/account/create",
        "https://www.xvideos.com/video.ucuhied1d4e/slug?sort=date",
    ]
    for u in bad:
        check(f"排除 {u[:62]}", bc._is_video_detail_link(u, base) is False, u)

    good = [
        "https://www.xvideos.com/video.ucuhied1d4e/julesjordan.com_-_some_slug_",
        "https://www.xvideos.com/video.hpiefkh38ac/tongue_fucking_ava_addams_asshole",
        "https://www.xvideos.com/video71632981/some_slug",
    ]
    for u in good:
        check(f"保留 {u[:62]}", bc._is_video_detail_link(u, base) is True, u)

    # 同一形状换域名：证明这不是站点专用规则
    other = "https://hornyleak.tv/models/someone/"
    check("其它站 内容页 /video/33006/slug",
          bc._is_video_detail_link("https://hornyleak.tv/video/33006/comatozze-xxx/", other) is True)
    check("其它站 内容页 /videos/33006/slug",
          bc._is_video_detail_link("https://hornyleak.tv/videos/33006/some-slug/", other) is True)
    check("其它站 集合页 /models/other/",
          bc._is_video_detail_link("https://hornyleak.tv/models/other/", other) is False)
    check("其它站 分类页 /c/Foo-159/",
          bc._is_video_detail_link("https://hornyleak.tv/c/Foo-159/", other) is False)
    check("跨域名不放行",
          bc._is_video_detail_link("https://other.example/video/33006/slug", other) is False)


def test_video_identity():
    print("[同一部片的多条页面地址去重]")
    variants = [
        "https://www.xvideos.com/video.olkvokmc5d9/27/slug",
        "https://www.xvideos.com/video.olkvokmc5d9/THUMBNUM/slug",
        "https://www.xvideos.com/video.olkvokmc5d9/slug",
    ]
    keys = {bc._video_page_identity(u) for u in variants}
    check("三种变体归成一个键", len(keys) == 1, keys)
    check("不同视频不会被合并",
          bc._video_page_identity("https://www.xvideos.com/video.aaaaaaaaaa/x")
          != bc._video_page_identity("https://www.xvideos.com/video.bbbbbbbbbb/x"))
    check("pornhub viewkey 也归并",
          bc._video_page_identity("https://a.pornhub.com/view_video.php?viewkey=X1&t=1")
          == bc._video_page_identity("https://a.pornhub.com/view_video.php?viewkey=X1"))


def test_probe_url():
    print("[单条解析入口：所有站点的视频条目都有 probe_url]")
    page_item = MediaItem(url="https://ex.com/video/7539022", kind="video",
                          extra={"is_page": True})
    check("视频页条目 probe_url=自己",
          page_item.to_dict().get("probe_url") == page_item.url,
          page_item.to_dict().get("probe_url"))

    direct = MediaItem(url="https://cdn.ex.com/a/240P_1.mp4", kind="video",
                       extra={"detail_page": "https://ex.com/video/7539022"})
    check("直链条目 probe_url=详情页",
          direct.to_dict().get("probe_url") == "https://ex.com/video/7539022",
          direct.to_dict().get("probe_url"))

    img = MediaItem(url="https://cdn.ex.com/a.jpg", kind="image",
                    extra={"detail_page": "https://ex.com/p/1"})
    check("图片不产生视频解析入口", img.to_dict().get("probe_url") == "")

    src = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    check("前端优先读 it.probe_url", "if (it.probe_url) return it.probe_url;" in src)


def test_m3u8_guard():
    print("[HLS 清单不再被当成视频保存]")
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import tempfile

    body = b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=815104\nhls-480p.m3u8\n"

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.apple.mpegurl")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 18897), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with tempfile.TemporaryDirectory() as d:
            r = download_file("http://127.0.0.1:18897/playlist.m3u8", d,
                              skip_existing=False)
            check("拒绝保存 m3u8 清单", r.ok is False and "HLS" in (r.error or ""),
                  f"ok={r.ok} err={r.error}")
            check("没有落盘假视频", list(Path(d).glob("*.mp4")) == [],
                  str(list(Path(d).iterdir())))
    finally:
        srv.shutdown()
        srv.server_close()


def test_direct_quality():
    print("[详情页多档直链：选最高一条]")
    check("360p 从 URL 读出",
          bc._direct_media_height(MediaItem(url="https://x/video_360p.mp4", kind="video")) == 360)
    check("1280x720 从 URL 读出",
          bc._direct_media_height(MediaItem(url="https://x/1280x720.mp4", kind="video")) == 720)
    check("height 字段优先",
          bc._direct_media_height(MediaItem(url="https://x/240p.mp4", kind="video",
                                            height=1080)) == 1080)
    src = (ROOT / "app" / "batch_crawler.py").read_text(encoding="utf-8")
    check("下钻选档按高度取最大",
          "max(trusted, key=_direct_media_height)" in src)


def test_hls_quality_pick():
    print("[分片流按清晰度选档：默认最高]")
    master = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=4100800,NAME="FULLHD",RESOLUTION=1920x1080
https://p.example/m3u/fullhd
#EXT-X-STREAM-INF:BANDWIDTH=2912800,NAME="HD",RESOLUTION=1280x720
https://p.example/m3u/hd
#EXT-X-STREAM-INF:BANDWIDTH=815104,NAME="SD",RESOLUTION=854x480
https://p.example/m3u/sd
"""
    variants = _parse_hls_master(master, "https://p.example/list/x")
    check("master 解析出 3 档", [v["height"] for v in variants] == [1080, 720, 480],
          [v["height"] for v in variants])
    check("子清单地址按 base 补齐", variants[0]["url"] == "https://p.example/m3u/fullhd",
          variants[0]["url"])

    child = """#EXTM3U
#EXTINF:8.0,
https://cdn.example/v/1080p_0.txt
#EXTINF:8.0,
https://cdn.example/v/1080p_1.txt
"""
    segs = _parse_hls_segments(child, "https://p.example/m3u/fullhd")
    check("子清单解析出 2 片", len(segs) == 2 and segs[0].endswith("1080p_0.txt"), segs)
    check("分片归组", _hls_seg_entry(segs[0]) == (("/v/", "1080p"), 0),
          _hls_seg_entry(segs[0]))

    # 关键回归：480P 分片更短、数量更多；旧逻辑「数量最多」会选到 480P
    seg_map = {
        ("/v/", "480p"): {i: [f"https://cdn/sd_{i}.txt"] for i in range(5)},
        ("/v/", "720p"): {i: [f"https://cdn/hd_{i}.txt"] for i in range(5)},
        ("/v/", "1080p"): {i: [f"https://cdn/fh_{i}.txt"] for i in range(4)},
    }
    check("默认选 1080p", _choose_ts_group(seg_map)["prefix"] == "1080p",
          _choose_ts_group(seg_map)["prefix"])
    check("选最低选 480p", _choose_ts_group(seg_map, "lowest")["prefix"] == "480p",
          _choose_ts_group(seg_map, "lowest")["prefix"])
    check("指定 720 选 720p", _choose_ts_group(seg_map, "720")["prefix"] == "720p",
          _choose_ts_group(seg_map, "720")["prefix"])


def test_hls_completeness():
    """2026-11-07 回归：hornyleak 1080P 只下 9/115 段（65.8s/911.8s）却报成功。"""
    print("[分片流完整性：清单权威 / 截断必须判失败]")
    lines = ["#EXTM3U", "#EXT-X-TARGETDURATION:8"]
    for i in range(115):
        lines.append("#EXTINF:7.933333,")
        lines.append(f"https://cdn.example/v/1080p_{i}.txt")
    child = "\n".join(lines) + "\n"
    seg_map: dict = {}
    meta_all: dict = {}
    n = _index_ts_playlist(child, "https://p.example/m3u/fullhd",
                           seg_map, meta_all, {"height": 1080, "bandwidth": 4100800})
    key = ("/v/", "1080p")
    meta = meta_all[key]
    check("清单登记 115 段", n == 115 and meta["expected"] == 115, (n, meta["expected"]))
    check("清单累计时长 >900 秒", 900 < meta["duration"] < 930, meta["duration"])
    check("选档取 master 声明的 1080", meta["height"] == 1080, meta["height"])
    plan = _ts_plan(meta, seg_map[key])
    check("按清单顺序展开 115 个地址",
          len(plan) == 115 and plan[0].endswith("1080p_0.txt")
          and plan[-1].endswith("1080p_114.txt"), (len(plan), plan[:1], plan[-1:]))
    check("旧行为 9/115 被拦下",
          "分片不完整" in _ts_integrity_error(9, 115))
    check("旧行为 65.8s/911.8s 被时长闸门拦下",
          "下载不完整" in _ts_integrity_error(
              115, 115, actual_dur=65.8, expected_dur=911.8))
    check("完整片通过闸门",
          _ts_integrity_error(115, 115, actual_dur=911.8,
                              expected_dur=911.8) == "")
    check("max_segments 短样本不误判",
          _ts_integrity_error(2, 2, partial=True) == "")

    # 路径不带 1080p 字样的站点：靠 master 元数据选档，不能退化成按段数选
    seg_map2: dict = {}
    meta2: dict = {}
    low = "\n".join(["#EXTM3U", "#EXTINF:4.0,",
                     "https://cdn.example/v/lo_0.txt", "#EXTINF:4.0,",
                     "https://cdn.example/v/lo_1.txt", "#EXTINF:4.0,",
                     "https://cdn.example/v/lo_2.txt", "#EXTINF:4.0,",
                     "https://cdn.example/v/lo_3.txt"]) + "\n"
    high = "\n".join(["#EXTM3U", "#EXTINF:8.0,",
                      "https://cdn.example/v/hi_0.txt", "#EXTINF:8.0,",
                      "https://cdn.example/v/hi_1.txt"]) + "\n"
    _index_ts_playlist(low, "https://p.example/m3u/sd", seg_map2, meta2,
                       {"height": 480, "bandwidth": 900000})
    _index_ts_playlist(high, "https://p.example/m3u/fullhd", seg_map2, meta2,
                       {"height": 1080, "bandwidth": 4100800})
    check("无档位后缀时按 master 高度选高档",
          _choose_ts_group(seg_map2, "highest", meta2)["prefix"] == "hi",
          _choose_ts_group(seg_map2, "highest", meta2)["prefix"])

    src = (ROOT / "app" / "video_downloader.py").read_text(encoding="utf-8")
    check("截断不回退预览直链",
          'if _parsed.get("kind") == "ts" or _ts.get("truncated"):' in src)
    check("分片走 HTTP 并发并校验 TS 魔数",
          "_http_fetch_ts(cu, referer)" in src
          and 'r.content[:1] == b"\\x47"' in src)
    check("前置渲染解析失败时仍有一体式分片兜底",
          "前置渲染解析拿不到播放器时" in src
          and src.count("_download_ts_via_browser(") >= 3)
    check("事件监听不再把二进制分片读进内存",
          "伪 txt 分片是二进制 MPEG-TS" in src
          and 'r"_\\d{1,4}\\.txt$"' in src)
    check("清理失败不影响下载成功",
          "def _cleanup_parts" in src and "except BaseException" in src)


def test_keep_page_branch():
    print("[渲染列表分支：单条视频页 yt-dlp 认识时保留页面地址]")
    check("认识 /video.<id>/ 单条视频页",
          has_specific_extractor("https://www.xvideos.com/video.hpbcota8c08/some_slug") is True)
    check("不认识中小站的视频页（继续下钻取直链）",
          has_specific_extractor("https://hornyleak.tv/video/33006/comatozze-xxx/") is False)
    src = (ROOT / "app" / "batch_crawler.py").read_text(encoding="utf-8")
    check("渲染列表分支按 yt-dlp 能力选路",
          "all(has_specific_extractor(u) for u in sample)" in src)
    check("保留页面地址时标记 is_page", '"via": "render-listing"' in src)


def test_frontend_signatures():
    print("[前端静态签名：默认最高清晰度 / 直链也能单独解析]")
    js = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    css = (ROOT / "web" / "style.css").read_text(encoding="utf-8")
    check("视频/图片都默认选最高档",
          "if (!st.qualityTouched && sel.value === '') sel.value = 'highest';" in js)
    check("默认最高档不再区分抓取类型", "/video|stream/.test(KINDS)" not in js)
    check("手动改过清晰度后不再覆盖", "st.qualityTouched = true" in js)
    check("图片批量下载默认 highest", "qualEl ? qualEl.value : 'highest'" in js)
    check("图片搜索下载也带 highest", "prefer_quality: 'highest'" in js)
    check("直链条目也计算解析目标", "function crawlProbeTarget(it)" in js)
    check("解析按钮使用 probe-btn", "bp.className = 'btn tiny probe-btn'" in js)
    check("解析按钮用来源视频页地址", "gotoVideoProbe(probeUrl)" in js)
    check("HD 图标样式存在且优先级够高",
          ".btn.tiny.probe-btn::after" in css and '"HD"' in css)


def test_progress_and_console():
    print("[下载进度条与控制台弹窗]")
    from types import SimpleNamespace

    from app.task_manager import apply_video_progress

    t = SimpleNamespace(percent=0.0, speed=0.0, current="", done=0, total=1)
    apply_video_progress(t, {"status": "downloading", "percent": 0.0,
                             "filename": "正在解析播放清单…"})
    check("解析阶段也显示状态文字", t.current == "正在解析播放清单…"
          and t.percent == 0.0)
    apply_video_progress(t, {"status": "downloading", "percent": 42.5,
                             "progressed_bytes": 50_000_000,
                             "total_bytes": 120_000_000, "speed": 3_000_000,
                             "note": "1080P · 分片 49/115"})
    check("分片进度写进 percent", 42.0 < t.percent < 43.0)
    check("分片说明显示在任务行", "分片 49/115" in t.current)
    check("速度透传给界面", t.speed == 3_000_000.0)
    check("下载中不算已完成", t.done == 0)
    apply_video_progress(t, {"status": "downloading", "percent": 100.0,
                             "note": "正在合并分片并转封装…"})
    check("完成前进度封顶 99.9%", t.percent == 99.9)

    src = (ROOT / "app" / "video_downloader.py").read_text(encoding="utf-8")
    check("分片回调必须带 percent/speed/filename",
          '"percent": round(min(99.0' in src and '"filename": label' in src)
    check("最后一片强制上报（短片源不留白）", "or i + 1 == expected" in src)
    check("合并阶段也有进度反馈", "正在合并分片并转封装" in src)
    check("ffmpeg / ffprobe 全部走 run_external",
          "subprocess.run(" not in src and src.count("run_external(") >= 3)
    wc = (ROOT / "app" / "win_console.py").read_text(encoding="utf-8")
    cfg = (ROOT / "app" / "config.py").read_text(encoding="utf-8")
    sp = (ROOT / "app" / "subproc.py").read_text(encoding="utf-8")
    check("全局 Popen 默认补 CREATE_NO_WINDOW",
          'kwargs.setdefault("creationflags", _FLAG)' in wc)
    check("config 导入时即启用无窗口补丁",
          "ensure_quiet_subprocess()" in cfg)
    check("外部程序统一无窗口标志",
          '_BASE_CREATIONFLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)' in sp)


if __name__ == "__main__":
    test_collection_page()
    test_video_detail_link()
    test_video_identity()
    test_probe_url()
    test_m3u8_guard()
    test_direct_quality()
    test_hls_quality_pick()
    test_hls_completeness()
    test_keep_page_branch()
    test_frontend_signatures()
    test_progress_and_console()
    print()
    if FAILED:
        print(f"FAILED {len(FAILED)}: {FAILED}")
        sys.exit(1)
    print("ALL PASS")
