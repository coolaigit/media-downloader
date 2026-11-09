# -*- coding: utf-8 -*-
"""知乎抓视频：只交出真视频，且带标题/封面；纯离线，不联网不起服务。

背景（2026-11-08 Sam 反馈「抓到的有些根本不是视频，名称和封面也没搞定」）：
  实测把 https://www.zhihu.com/question/665425481 粘进「抓取视频」，
  返回 20 条 topic / people / question / answer / 专栏链接，全部标成
  type=video，title 与 thumbnail 全空。原因是 browser_items 的兜底用
  通用「作品链接」判据（长 ID + answer/question 段）放行，再硬写
  kind_hint=video。单条 /video/<id> 又只拿到 302 后的 1.3KB JS 空壳。

  这条用例把两个闸门都钉死：
    1. 裸链接候选必须过视频形态判据；
    2. 知乎视频走专属通道，元数据由解析器给出，解析不出的不留空壳。
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import profile_sources as ps
from app import video_downloader as vd

checks: list[tuple[str, bool, str]] = []


def ck(name: str, cond: bool, extra: object = "") -> None:
    checks.append((name, bool(cond), str(extra)))
    print(("  ok   " if cond else "  FAIL ") + name
          + ("" if cond else "  << " + str(extra)), flush=True)


FAKE = {
    "ok": True,
    "title": "写春联也太难了吧！",
    "thumbnail": "https://picx.zhimg.com/v2-148f884ffec11aec077e86e2bf145b1c_1440w.jpg?source=12a79843",
    "duration": 146.333,
    "uploader": "知乎用户",
    "formats": [{"format_id": "fhd"}, {"format_id": "hd"}],
}


def main() -> int:
    print("[视频形态闸门：真视频 vs 假链接]", flush=True)
    good = [
        "https://www.zhihu.com/zvideo/1342930761977176064",
        "https://www.zhihu.com/video/1342930761977176064",
        "https://video.zhihu.com/video/1342930761977176064",
        "https://www.kuaishou.com/short-video/3xexamplename",
        "https://www.xvideos.com/video.abc1234567/my_title",
        "https://cdn.example.com/upic/2026/10/24/a_b.mp4",
        "https://cdn.example.com/hls/master.m3u8?token=x",
    ]
    for u in good:
        ck(f"真视频放行：{u[:62]}", ps._looks_like_video_work_url(u))
    bad = [
        "https://www.zhihu.com/topic/19552223",
        "https://www.zhihu.com/people/july-95-31",
        "https://www.zhihu.com/question/665425481/answer/4814530975",
        "https://www.zhihu.com/question/592789431",
        "https://zhuanlan.zhihu.com/p/96956163",
        "https://www.zhihu.com/account/unhuman?type=U4E3Z1",
        "https://static.zhihu.com/heifetz/favicon.ico",
    ]
    for u in bad:
        ck(f"非视频丢弃：{u[:62]}", not ps._looks_like_video_work_url(u))
    ck("通用作品判据仍放行回答页（图片模式不受影响）",
       ps._looks_like_work_url(
           "https://www.zhihu.com/question/665425481/answer/4814530975",
           "https://www.zhihu.com/question/665425481"))

    print("[单条视频页：归一 + 元数据]", flush=True)
    url = "https://www.zhihu.com/video/1342930761977176064"
    ck("F-15 归一化仍生效",
       vd._normalize_media_url(url)
       == "https://www.zhihu.com/zvideo/1342930761977176064")
    with mock.patch("app.video_downloader.probe_video", return_value=FAKE) as probe, \
            mock.patch("app.renderer.render_page",
                       side_effect=AssertionError("单条视频页不该去渲染")):
        res = ps._zhihu_video_items(url)
    ck("单条视频页解析成功", res.get("ok") is True, res)
    ck("解析器收到的是 zvideo 地址",
       (probe.call_args[0][0] if probe.call_args else "")
       == "https://www.zhihu.com/zvideo/1342930761977176064")
    it = (res.get("items") or [{}])[0]
    ck("标题来自解析器", it.get("title") == "写春联也太难了吧！", it)
    ck("封面来自解析器", it.get("cover", "").startswith("https://picx.zhimg.com/"), it)
    ck("时长来自解析器", it.get("duration") == 146.333, it)
    ck("page_url 是 zvideo 作品页",
       it.get("page_url") == "https://www.zhihu.com/zvideo/1342930761977176064")
    ck("不给直链（交给 yt-dlp 逐条下载）", it.get("direct_url") == "")
    ck("标记为视频页 / 视频类型",
       it.get("is_page") is True and it.get("kind_hint") == "video", it)

    print("[问题/主页：只挑页面里的真视频 ID]", flush=True)
    html = """
    <a href="https://www.zhihu.com/zvideo/1342930761977176064">春联</a>
    <a href="https://www.zhihu.com/zvideo/1550000000000000001">另一条</a>
    <a href="https://www.zhihu.com/topic/19552223">话题</a>
    <a href="https://www.zhihu.com/people/july-95-31">用户</a>
    <a href="https://www.zhihu.com/question/665425481/answer/4814530975">回答</a>
    <a href="https://zhuanlan.zhihu.com/p/96956163">专栏</a>
    """
    rr = SimpleNamespace(ok=True, final_url="https://www.zhihu.com/question/665425481",
                         title="知乎问题", html=html, captured=[])
    with mock.patch("app.renderer.render_page", return_value=rr), \
            mock.patch("app.video_downloader.probe_video", return_value=FAKE) as probe:
        res = ps._zhihu_video_items("https://www.zhihu.com/question/665425481",
                                   limit=20)
    ck("只返回 2 条真视频", len(res.get("items") or []) == 2, res)
    ck("没有 topic/people/question/专栏 链接",
       all("/zvideo/" in (x.get("page_url") or "") for x in (res.get("items") or [])))
    ck("逐条解析了 2 次", probe.call_count == 2, probe.call_count)

    print("[解析失败不留空壳]", flush=True)
    with mock.patch("app.video_downloader.probe_video",
                    return_value={"ok": False, "error": "登录态失效"}):
        res = ps._zhihu_video_items(url)
    ck("全失败时 ok=false", res.get("ok") is False, res)
    ck("没有交出无标题裸链接", not (res.get("items") or []), res)
    ck("错误里带上真实原因", "登录态失效" in str(res.get("error") or ""), res)

    print("[分流：知乎视频不再走通用兜底]", flush=True)
    with mock.patch.object(ps, "_zhihu_video_items",
                           return_value={"ok": True, "items": [{"title": "A"}],
                                         "login_required": False, "error": ""}), \
            mock.patch.object(ps, "ytdlp_items",
                              side_effect=AssertionError("不该再走 yt-dlp 兜底")), \
            mock.patch.object(ps, "browser_items",
                              side_effect=AssertionError("不该再走浏览器兜底")):
        items, note = ps.list_author_items(
            "https://www.zhihu.com/question/665425481", want_video=True)
    ck("专属通道结果直接返回", len(items) == 1 and items[0]["title"] == "A", items)
    ck("说明里写明是知乎视频解析", "知乎视频" in note, note)
    with mock.patch.object(ps, "_zhihu_video_items",
                           return_value={"ok": False, "login_required": False,
                                         "error": "已按知乎视频通道确认：页面没有视频作品链接"}):
        items, note = ps.list_author_items(
            "https://www.zhihu.com/question/665425481", want_video=True)
    ck("失败时如实报原因、不返回假结果", not items and "已按知乎视频通道确认" in note, note)
    bc_src = (Path(__file__).resolve().parent / "app" / "batch_crawler.py").read_text(
        encoding="utf-8")
    ck("专属通道确定性失败会阻止网页抓取回退",
       '"已按知乎视频通道确认"' in bc_src)

    import inspect
    sig = str(inspect.signature(ps.browser_items))
    ck("browser_items 带 video_only 参数", "video_only" in sig, sig)

    ok = sum(1 for _, c, _ in checks if c)
    bad_n = len(checks) - ok
    print(f"结果：{ok} 通过 / {bad_n} 失败", flush=True)
    return 1 if bad_n else 0


if __name__ == "__main__":
    raise SystemExit(main())
