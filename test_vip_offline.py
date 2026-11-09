# -*- coding: utf-8 -*-
"""会员视频兜底离线用例：集号/片名匹配/试看判定/播放链解析。

跑法：python test_vip_offline.py
不联网（资源站检索用构造的 Candidate，不发起真实请求）。
"""
from __future__ import annotations

import sys

from app import resource_sites as rs
from app import vip_unlock as vu

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = ""):
    if cond:
        print(f"  ok   {name}")
    else:
        FAILED.append(name)
        print(f"  FAIL {name} {detail}")


def test_episode():
    print("[集号解析]")
    cases = {
        "名侦探柯南 第1278话 云霄飞车杀人事件": 1278,
        "庆余年 第二季 第01集": 1,
        "第12期 你好星期六": 12,
        "EP05 试播集": 5,
        "12集全": 12,
        "第十三集": 13,
        "第一百零三集": 103,
        "没有集号": None,
    }
    for text, want in cases.items():
        got = rs.parse_episode(text)
        check(f"{text!r} -> {want}", got == want, f"got {got}")


def test_cn_to_int():
    print("[中文数字]")
    for text, want in (("十二", 12), ("二十", 20), ("一百零三", 103), ("三十九", 39),
                       ("七", 7), ("abc", None)):
        got = rs.cn_to_int(text)
        check(f"{text} -> {want}", got == want, f"got {got}")


def test_clean_title():
    print("[片名清洗]")
    cases = [
        ("《庆余年》第二季 第01集 在线观看 - 优酷", "庆余年"),
        ("名侦探柯南（中配）第1278话", "名侦探柯南 中配"),
        ("流浪地球2 4K高清完整版", "流浪地球"),
    ]
    for raw, want in cases:
        got = rs.clean_title(raw)
        check(f"{raw!r} -> 含 {want!r}", want in got, f"got {got!r}")


def test_similarity():
    print("[片名相似度]")
    check("完全一致 100", rs.similarity("庆余年 第二季", "庆余年 第二季") == 100.0)
    check("包含关系 >70", rs.similarity("庆余年", "庆余年第二季") > 70)
    check("无关片名 <55", rs.similarity("庆余年", "流浪地球") < 55)
    check("中配与原声低于完全一致",
          rs.similarity("名侦探柯南 中配", "名侦探柯南") > 55
          and rs.similarity("名侦探柯南 中配", "名侦探柯南") < 100)


def test_split_groups():
    print("[播放组解析]")
    groups = rs._split_groups(
        "lzyun$$$lzm3u8",
        "第01集$https://a.com/1.m3u8#第02集$https://a.com/2.m3u8$$$第01集$https://b.com/p/1")
    check("两组", len(groups) == 2, str(len(groups)))
    check("组名", groups[0][0] == "lzyun" and groups[1][0] == "lzm3u8")
    check("分集数", len(groups[0][1]) == 2 and len(groups[1][1]) == 1)
    check("分集名", groups[0][1][1].name == "第02集", groups[0][1][1].name)
    check("分集地址", groups[0][1][1].url == "https://a.com/2.m3u8")


def _cand(site, name, year, remarks, eps):
    groups = [(site + "m3u8", [rs.Episode(n, u) for n, u in eps])]
    return rs.Candidate(site=site, site_name=rs.SITES[site]["name"], vod_name=name,
                        year=year, remarks=remarks, score=0.0, groups=groups)


def test_rank_and_pick():
    print("[候选排序与选集]")
    cands = [
        _cand("ffzy", "庆余年 第二季[预告片]", "2024", "更新至预告片",
              [("预告", "https://x.com/t.m3u8")]),
        _cand("ikun", "庆余年第二季", "2024", "全36集",
              [(f"第{i:02d}集", f"https://x.com/{i}.m3u8") for i in range(1, 37)]),
        _cand("jisu", "庆余年 第一季", "2019", "已完结",
              [(f"第{i:02d}集", f"https://y.com/{i}.m3u8") for i in range(1, 47)]),
    ]
    ranked = rs.rank(cands, "庆余年 第二季", year="2024", episode=5)
    check("第二季排第一", ranked[0].vod_name == "庆余年第二季", ranked[0].vod_name)
    ep = ranked[0].pick(5)
    check("选中第 5 集", ep is not None and ep.url.endswith("/5.m3u8"), str(ep))
    by_name = {c.vod_name: c for c in ranked}
    check("第一季同样命中集号", by_name["庆余年 第一季"].pick(5) is not None)
    check("预告片没有第 5 集", by_name["庆余年 第二季[预告片]"].pick(5) is None)

    # 只有一集（电影/预告）时，任何集号都退回这条
    movie = _cand("wujin", "流浪地球2", "2023", "正片",
                  [("正片", "https://m.com/m.m3u8")])
    check("单片无集号也能取", movie.pick(None) is not None and movie.pick(9) is not None)


def test_preview_detection():
    print("[试看判定]")
    check("6 分钟 vs 24 分钟 = 试看", vu.detect_preview(1494, 360))
    check("完整 24 分钟 = 不是试看", not vu.detect_preview(1494, 1493))
    check("只差 60 秒 = 不是试看（门槛 90s）", not vu.detect_preview(1494, 1434))
    check("缺时长数据不误判", not vu.detect_preview(0, 360))
    check("实际比标称长不误判", not vu.detect_preview(600, 1494))


def test_bili_ids():
    print("[B 站地址解析]")
    cases = [
        ("https://www.bilibili.com/video/BV1GJ411x7h7?p=2", {"bvid": "BV1GJ411x7h7", "page": 2}),
        ("https://www.bilibili.com/video/av170001", {"aid": 170001}),
        ("https://www.bilibili.com/bangumi/play/ep323085", {"ep_id": 323085}),
        ("https://www.bilibili.com/bangumi/play/ss28747", {"season_id": 28747}),
    ]
    for url, want in cases:
        got = vu.bilibili_ids(url)
        ok = all(got.get(k) == v for k, v in want.items())
        check(url, ok, str(got))


def test_cookie_line():
    print("[Cookie 归一]")
    netscape = ("# Netscape HTTP Cookie File\n"
                ".bilibili.com\tTRUE\t/\tFALSE\t0\tSESSDATA\tabc%2Cdef\n"
                ".bilibili.com\tTRUE\t/\tFALSE\t0\tbili_jct\txyz\n")
    got = vu._cookie_line(netscape)
    check("Netscape -> 请求头", "SESSDATA=abc%2Cdef" in got and "bili_jct=xyz" in got, got)
    check("一行 Cookie 原样通过",
          vu._cookie_line("SESSDATA=1; bili_jct=2") == "SESSDATA=1; bili_jct=2")


def test_platform():
    print("[平台识别]")
    check("B 站", vu.platform_of("https://www.bilibili.com/video/BV1xx") == "bilibili")
    check("b23 短链", vu.platform_of("https://b23.tv/abc") == "bilibili")
    check("优酷", vu.platform_of("https://v.youku.com/v_show/id_XNT.html") == "youku")
    check("爱奇艺", vu.platform_of("https://www.iqiyi.com/v_abc.html") == "iqiyi")
    check("普通站不算会员站", vu.platform_of("https://example.com/a.mp4") == "")


def test_replace_with_full():
    print("[试看片段替换]")
    import tempfile
    from pathlib import Path
    from app import video_downloader as vd
    from app import vip_unlock as vu

    tmp = Path(tempfile.mkdtemp(prefix="md_vip_test_"))
    preview = tmp / "preview.mp4"
    preview.write_bytes(b"x" * 1024)
    full = tmp / "full.mp4"
    full.write_bytes(b"y" * 8192)

    orig = (vu.extract_meta, vu.probe_duration, vu.unlock, vd._vip_auto_enabled)
    vu.extract_meta = lambda u: {"title": "测试片", "duration": 1494.0}
    vu.probe_duration = lambda p: 360.0
    vu.unlock = lambda *a, **k: {"ok": True, "path": str(full), "size": 8192, "note": "换源成功"}
    vd._vip_auto_enabled = lambda: True
    try:
        res = vd._vip_replace_with_full(
            "https://v.youku.com/v_show/id_XLjE.html", tmp, task_id=None, title="测试片",
            info={"duration": 1494}, old_files=[{"path": str(preview), "name": "preview.mp4", "size": 1024}],
            on_progress=None)
        check("判定试看后换源成功", bool(res and res.get("ok")), str(res))
        check("交回完整版文件", bool(res) and res["files"][0]["path"] == str(full))
        check("试看片段已删除", not preview.exists())
        check("写了 .done 标记", any(p.name.endswith(".done") for p in tmp.iterdir()))

        # 时长正常（完整版）时不该触发换源
        vu.probe_duration = lambda p: 1493.0
        res2 = vd._vip_replace_with_full(
            "https://v.youku.com/v_show/id_XLjE.html", tmp, task_id=None, title="测试片",
            info={"duration": 1494}, old_files=[{"path": str(full), "name": "full.mp4", "size": 8192}],
            on_progress=None)
        check("时长正常不触发换源", res2 is None, str(res2))

        # B 站普通投稿（UGC）不做时长比对
        res3 = vd._vip_replace_with_full(
            "https://www.bilibili.com/video/BV1xx411c7mD", tmp, task_id=None, title="v",
            info={"duration": 600}, old_files=[{"path": str(full), "name": "full.mp4", "size": 8192}],
            on_progress=None)
        check("B 站 UGC 跳过比对", res3 is None, str(res3))
    finally:
        vu.extract_meta, vu.probe_duration, vu.unlock, vd._vip_auto_enabled = orig


def test_lang_penalty():
    print("[语言版本惩罚]")
    cands = [
        _cand("ikun", "名侦探柯南 粤语版", "1996", "第1278集",
              [("第01集", "https://x.com/yue.m3u8")]),
        _cand("jisu", "名侦探柯南（中配）", "1996", "第1278集",
              [("第01集", "https://x.com/cn.m3u8")]),
    ]
    ranked = rs.rank(cands, "名侦探柯南 中配", year="1996", episode=1)
    check("中配排第一", ranked[0].vod_name == "名侦探柯南（中配）", ranked[0].vod_name)
    check("粤语被压分", ranked[1].score < ranked[0].score - 20,
          f"{ranked[0].score} vs {ranked[1].score}")
    check("语言归一", rs.lang_bucket("国语版") == rs.lang_bucket("中配") == "cn")


def test_season_matching():
    print("[季号匹配]")
    check("第二季解析", rs.parse_season("人间世 第二季") == 2)
    check("S02 解析", rs.parse_season("… S02EP06") == 2)
    check("无季号", rs.parse_season("谷雨 第2集") is None)
    cands = [
        _cand("ikun", "人间世第一季", "2016", "全10集",
              [("第01集", "https://x.com/s1.m3u8")]),
        _cand("jisu", "人间世第二季", "2024", "全10集",
              [("第01集", "https://x.com/s2.m3u8")]),
    ]
    ranked = rs.rank(cands, "人间世 第二季", year="2024", episode=1)
    check("第二季排第一", ranked[0].vod_name == "人间世第二季", ranked[0].vod_name)
    check("第一季被季号重罚", ranked[1].score < ranked[0].score - 50,
          f"{ranked[0].score} vs {ranked[1].score}")


def test_alt_source_scoring():
    print("[海外源评分]")
    from app import alt_sources

    def e(title, dur):
        return {"title": title, "duration": dur}

    # 对不上集号的一律拒绝
    check("EP06 不能当第 1 集",
          alt_sources._score(e("…S02EP06", 3136), "人间世 第二季", 1, 3121) < 0)
    check("无集号不能当分集",
          alt_sources._score(e("人间世第二季 合集", 3121), "人间世 第二季", 1, 3121) < 0)
    # 对得上集号 + 时长接近 → 高分
    good = alt_sources._score(e("【谷雨】EP02 全片", 2800), "谷雨 第2集 剧情", 2, 2725)
    check("EP02 + 时长接近 高分", good >= 80, str(good))
    # 预告片重罚
    bad = alt_sources._score(e("谷雨 EP02 预告片", 60), "谷雨 第2集 剧情", 2, 2725)
    check("预告片低分", bad < 60, str(bad))


def test_should_try():
    print("[何时触发换源]")
    from app import video_downloader as vd

    orig = vd._vip_auto_enabled
    vd._vip_auto_enabled = lambda: True
    try:
        u = "https://www.bilibili.com/bangumi/play/ep810204"
        check("会员页 412 风控也触发", vd._vip_should_try(u, "HTTP 412: 站点风控拦截"))
        check("会员页 403 也触发", vd._vip_should_try(u, "HTTP Error 403: Forbidden"))
        check("会员页登录墙也触发", vd._vip_should_try(u, "need to login"))
        check("404 不触发", not vd._vip_should_try(u, "HTTP Error 404: Not Found"))
        check("unsupported 不触发", not vd._vip_should_try(u, "Unsupported URL"))
        check("普通站不触发", not vd._vip_should_try("https://example.com/v/1", "HTTP 412"))
        check("无错误文本时（下载完核对）触发", vd._vip_should_try(u))
    finally:
        vd._vip_auto_enabled = orig


def main():
    test_episode()
    test_cn_to_int()
    test_clean_title()
    test_similarity()
    test_split_groups()
    test_rank_and_pick()
    test_preview_detection()
    test_bili_ids()
    test_cookie_line()
    test_platform()
    test_replace_with_full()
    test_lang_penalty()
    test_season_matching()
    test_alt_source_scoring()
    test_should_try()
    print()
    if FAILED:
        print(f"FAILED {len(FAILED)}: {FAILED}")
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    main()
