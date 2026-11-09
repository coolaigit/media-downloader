# -*- coding: utf-8 -*-
"""筛选栏页面感知离线用例（不联网）：图片页只显示图片选项，视频页只显示视频选项。

复现：2026-11-07 用户截图反馈「抓取图片，筛选栏却是视频的选项」。
根因：顶部筛选 chips / 抽屉属于全局壳，但 renderChips 绑定了所有
.filter-grid select（含视频面板），且抽屉只认图片面板。
跑法：./_venv/Scripts/python.exe test_filter_scope_offline.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = ""):
    if cond:
        print(f"  ok   {name}")
    else:
        FAILED.append(name)
        print(f"  FAIL {name} {detail}")


def main():
    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    js = (ROOT / "web" / "shell.js").read_text(encoding="utf-8")
    css = (ROOT / "web" / "style.css").read_text(encoding="utf-8")
    appjs = (ROOT / "web" / "app.js").read_text(encoding="utf-8")

    print("[筛选栏 DOM]")
    for eid in ("filterLabel", "filterCount", "drawerTitle", "drawerSub"):
        check(f"存在 #{eid}", f'id="{eid}"' in html)
    check("图片与视频筛选面板都还在",
          all(f'id="{x}"' in html for x in
              ("filterBox", "imgCrawlOptions", "videoSearchFilterBox",
               "vidCrawlOptions")))
    check("视频结果筛选默认隐藏", 'id="videoSearchFilterBox" open hidden' in html)

    print("[页面感知实现]")
    check("四种作用域都有定义",
          all(f"'{k}':" in js for k in
              ("image:kw", "image:link", "video:kw", "video:link")))
    check("图片页两种模式都搬图片筛选 filterBox",
          "'image:kw':   Object.assign({}, IMG_FILTER)" in js
          and "'image:link': Object.assign({}, IMG_FILTER)" in js
          and "box: 'filterBox'" in js)
    check("抽屉不再搬任何抓取选项",
          "box: 'imgCrawlOptions'" not in js
          and "box: 'vidCrawlOptions'" not in js
          and "title: '图片抓取选项'" not in js)
    check("视频页两种模式都用视频结果筛选",
          js.count("box: 'videoSearchFilterBox'") == 2)
    check("抓取选项恢复在页面上显示",
          "#filterBox, #imgCrawlOptions" not in css
          and "#filterBox { display: none; }" in css)
    check("scope() 按 tab + 方式判定", "function scope()" in js
          and "kwPanel.hidden" in js)
    check("图片 / 视频 chips 字段分离",
          "IMG_CHIP_FIELDS" in js and "VID_CHIP_FIELDS" in js
          and "s.tab === 'image' ? IMG_CHIP_FIELDS : VID_CHIP_FIELDS" in js)
    check("默认开关不再常驻成 chip（点击即消失的交互已移除）",
          "html.push(chipHtml('不重复下载'" not in js
          and "html.push(chipHtml('隐藏已下载'" not in js)
    check("筛选按钮在图片链接模式下隐藏",
          "s.key === 'image:link'" in js and "btn.hidden = hide" in js)
    check("change 只响应当前作用域", "box.contains(el)" in js)
    check("筛选计数改为动态", "function filterCount()" in js
          and "共 15 项参数" not in js)
    check("抽屉标题随作用域更新",
          "syncFilterBar()" in js and "$('#drawerTitle')" in js)
    check("抽屉按作用域搬面板", "document.getElementById(scope().box)" in js)
    check("视频筛选面板进抽屉时摘掉 hidden", "box.hidden = false" in js)
    check("关闭抽屉后恢复 hidden", "drawerBox.hidden = drawerWasHidden" in js)
    check("重置按作用域找按钮", "s.reset ? document.getElementById(s.reset)" in js)
    check("切 tab / 切方式会重绘",
          "new MutationObserver(() => syncFilterBar())" in js
          and "#omniSeg button" in js)

    print("[统计卡 / 顶部控件 / 按钮反馈 / 抓取参数]")
    check("统计卡读当前作用域数据", "function visibleEl(ids)" in js
          and "function activeGrid()" in js
          and "imgCrawlSelCount" in js)
    check("选中数不再被关键词页 0 覆盖",
          "(sel && sel.textContent) || (crawSel" not in js)
    check("结果/勾选/任务 DOM 变化立即刷新",
          "'imgCrawlGrid'" in js and "statMo.observe" in js)
    check("4 张统计卡可点击", "STAT_ACTIONS" in js
          and "hstat-clickable" in js and "switchTab('library')" in js)
    check("chips 改成可点击按钮", "chipHtml(" in js
          and "data-toggle" in js and "chipsBox.addEventListener" in js)
    check("筛选按钮改名", "label: '筛选设置'" in js
          and "title: '图片筛选设置'" in js and ">筛选设置<" in html)
    check("按钮有按下与抓取中反馈", ".is-pressed" in js
          and "setOmniBusy" in js and ":active" in css
          and "is-loading" in css)
    check("顶部控件字号与选中态增强",
          "font-size: 13.5px" in css and "font-weight: 700" in css)
    check("抓取参数默认折叠",
          'id="imgCrawlOptions" open' not in html
          and 'id="vidCrawlOptions" open' not in html
          and "抓取参数" in html)
    # 图片页那一整块视频式面板必须去掉：只留 4 项一行内联
    img_panel = html.split('id="panel-image"', 1)[1].split('id="panel-video"', 1)[0]
    vid_panel = html.split('id="panel-video"', 1)[1]
    check("图片抓取参数改成一行内联",
          '<div class="opt-row crawl-opts" id="imgCrawlOptions">' in img_panel
          and '<details class="filters" id="imgCrawlOptions">' not in html)
    check("图片页不再出现视频式抓取术语",
          all(w not in img_panel for w in
              ("抓取模式", "全站抓取", "跟随链接层数", "最多作品", "两种模式是两件事")))
    check("图片页保留会调的 4 项",
          all(e in img_panel for e in
              ('id="imgCPageCount"', 'id="imgCMaxItems"', 'id="imgCDrill"',
               'id="imgCOriginals"')))
    check("视频页抓取面板保持完整",
          'id="vidCrawlOptions"' in vid_panel and "全站抓取" in vid_panel
          and "最多作品" in vid_panel)
    check("共用抓取器读取的隐藏字段仍在",
          all(e in img_panel for e in
              ('id="imgCMode"', 'id="imgCSitePages"', 'id="imgCDepth"',
               'id="imgCUseSitemap"', 'id="imgCRender"')))
    check("updateModeUI 对缺失的 summary 判空",
          "const sumEl = $(E('crawlOptSummary'))" in appjs and "if (sumEl)" in appjs)

    print()
    if FAILED:
        print(f"FAILED {len(FAILED)}: {FAILED}")
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    main()
