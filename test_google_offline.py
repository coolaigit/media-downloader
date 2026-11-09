# -*- coding: utf-8 -*-
"""Google 搜图离线用例：解析结构与通道选路。

跑法：python test_google_offline.py
不联网 —— 渲染/SearXNG 都是模拟响应，避免把「Google 今天封没封我」变成测试结果。
Google 官方 JSON API 已对新客户关闭（2026），API 通道与设置项已移除。
"""
from __future__ import annotations

import sys

import app.search_google as sg

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = ""):
    if cond:
        print(f"  ok   {name}")
    else:
        FAILED.append(name)
        print(f"  FAIL {name} {detail}")


def test_build_tbs():
    print("[build_tbs]")
    tbs = sg.build_tbs({"size": "large", "time": "week", "color": "blue",
                        "type": "photo", "safesearch": "off"})
    for token in ("isz:l", "qdr:w", "ic:specific,isc:#00f", "itp:photo"):
        check(f"含 {token}", token in tbs, tbs)
    # 分辨率达到 1080 且没手动选尺寸时，自动叠加大图约束
    tbs2 = sg.build_tbs({"min_resolution": 1080})
    check("1080 自动升级到 qhd/4mp", "islt:qhd" in tbs2 or "islt:4mp" in tbs2, tbs2)


def test_imgres_parse():
    print("[parse_google_imgres]")
    html = """
    <a href="/imgres?imgurl=https%3A%2F%2Fex.com%2Fa.jpg&amp;imgrefurl=https%3A%2F%2Fsite.com%2Fp%2F1&amp;tbnid=TBN1">
      <img src="https://encrypted-tbn0.gstatic.com/images?q=tbn:AAA" alt="一只猫">
    </a>
    <a href="https://www.google.com/imgres?imgurl=https://ex.com/b.png&imgrefurl=https://site.com/p/2&tbnid=TBN2">
      <img src="https://encrypted-tbn0.gstatic.com/images?q=tbn:BBB" alt="cat 2">
    </a>
    <a href="/imgres?imgurl=https%3A%2F%2Fwww.gstatic.com%2Flogo.png&amp;tbnid=X">self</a>
    """
    items = sg.parse_google_imgres(html)
    check("解析出 2 条", len(items) == 2, str(len(items)))
    check("URL 解码正确", items[0]["url"] == "https://ex.com/a.jpg", items[0]["url"])
    check("来源页", items[0]["source_page"] == "https://site.com/p/1", items[0]["source_page"])
    check("标题取 alt", items[0]["title"] == "一只猫", items[0]["title"])
    check("缩略图", "gstatic" in items[0]["thumbnail"], items[0]["thumbnail"])
    check("gsatic 自身资源被过滤", all("google" not in i["url"] and "gstatic" not in i["url"]
                                   for i in items))
    check("engine_detail=render", items[0]["engine_detail"] == "render")


def test_triple_parse():
    print("[parse_google_html 三元组]")
    html = ('xx["https://ex.com/c.jpg",1500,1138],null,0,"rgb(0,0,0)",null,0,'
            '{"2000":[null,"www.esperoart.com","1.7MB"],'
            '"2003":[null,"DOCID","https://www.esperoart.com/xxx","山景",null,0,null,"thumb"]}yy')
    items = sg.parse_google_html(html)
    check("解析出 1 条", len(items) == 1, str(len(items)))
    it = items[0]
    check("宽高", (it["width"], it["height"]) == (1500, 1138), str((it["width"], it["height"])))
    check("体积", it["size_bytes"] == int(1.7 * 1024 * 1024), str(it["size_bytes"]))
    check("来源页", it["source_page"] == "https://www.esperoart.com/xxx", it["source_page"])
    check("站点", it["source_site"] == "www.esperoart.com", it["source_site"])
    check("格式", it["format"] == "jpg", it["format"])
    check("去重：同一 URL 不重复", len(sg.parse_google_html(html + html)) == 1)


def test_transport_order():
    print("[通道顺序]")
    orig = sg._settings
    try:
        sg._settings = lambda: {}
        check("默认渲染优先", sg._transport_order({})[0] == "render")
        check("auto = 渲染 → SearXNG", sg._transport_order({}) == ["render", "searx"])
        sg._settings = lambda: {"google_transport": "searx"}
        check("强制 SearXNG 生效", sg._transport_order({}) == ["searx"])
        sg._settings = lambda: {"google_transport": "render"}
        check("强制渲染生效", sg._transport_order({}) == ["render"])
        sg._settings = lambda: {"google_transport": "api"}
        check("旧的 api 配置回落到 auto", sg._transport_order({}) == ["render", "searx"])
    finally:
        sg._settings = orig


def test_failure_message():
    print("[全通道失败信息]")
    orig_order, orig_render, orig_searx = (
        sg._transport_order, sg.search_google_render, sg.search_google_searx)
    sg._transport_order = lambda f: ["render", "searx"]
    sg.search_google_render = lambda *a, **k: ([], "渲染被拦")
    sg.search_google_searx = lambda *a, **k: ([], "实例 429")
    try:
        items, err = sg.search_google("cat", {})
    finally:
        sg._transport_order = orig_order
        sg.search_google_render, sg.search_google_searx = orig_render, orig_searx
    check("空结果", not items)
    check("错误里带两条原因", all(x in (err or "") for x in ("渲染被拦", "429")), str(err))


def main():
    test_build_tbs()
    test_imgres_parse()
    test_triple_parse()
    test_transport_order()
    test_failure_message()
    print()
    if FAILED:
        print(f"FAILED {len(FAILED)}: {FAILED}")
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    main()
