# -*- coding: utf-8 -*-
"""首页 / 落点记忆 / 筛选按钮作用域 验收（真实 Chrome）。

覆盖用户 2026-11-07 这四条：
  1. 「筛选设置」只在关键词搜图出现，粘贴链接抓取不出现
  2. 打开默认不是抓取模块，也没有常驻的「不重复下载」chip
  3. 首次打开是首页
  4. 之后打开回到上次所在的页面

用法：
    _venv/Scripts/python.exe _verify_home_ui.py [http://127.0.0.1:8852]
"""
from __future__ import annotations

import json
import sys

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8852"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"

PASS: list[str] = []
FAIL: list[str] = []


def ck(name: str, ok: bool, detail: object = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(("  PASS  " if ok else "  FAIL  ") + name + ("" if ok else f"  <- {detail}"))


def main() -> int:
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=CHROME, headless=True,
                              args=["--no-sandbox", "--disable-gpu", "--no-proxy-server"])
        ctx = b.new_context(viewport={"width": 1600, "height": 950})
        pg = ctx.new_page()
        errs: list[str] = []
        pg.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
        pg.on("pageerror", lambda e: errs.append(str(e)))

        # 搜索接口挂空结果：只验交互，不真的打外网
        pg.route("**/api/search/images", lambda r: r.fulfill(
            status=200, content_type="application/json",
            body=json.dumps({"items": [], "total": 0, "engines": []})))

        # ---------- 1. 首次打开 = 首页 ----------
        pg.goto(BASE + "/", wait_until="domcontentloaded")
        pg.wait_for_timeout(2200)
        first = pg.evaluate("() => (document.querySelector('.tab.active')||{}).dataset?.tab")
        ck("首次打开落在首页", first == "home", first)
        ck("图片页默认不激活",
           pg.evaluate("() => !document.getElementById('panel-image').classList.contains('active')"))

        vis = pg.evaluate("""() => {
          const e = document.querySelector('.home-title');
          const r = e && e.getBoundingClientRect();
          return {h: r ? Math.round(r.height) : 0,
                  txt: e ? e.innerText.replace(/\\n/g, ' / ') : ''};
        }""")
        ck("首页主标题可见且排版成两行", vis["h"] > 70, vis)

        # 筛选按钮 / chips 都不该出现在首页
        ck("首页不显示「筛选设置」",
           pg.evaluate("() => { const b=document.getElementById('btnFilterDrawer');"
                       " return !b || b.hidden || getComputedStyle(b).display==='none'; }"))
        ck("首页没有常驻 chip",
           pg.evaluate("() => !document.getElementById('activeChips').textContent.trim()"))

        # ---------- 2. 首页输入 → 跳图片页执行 ----------
        pg.fill("#homeInput", "雪山 星空 4K")
        pg.wait_for_timeout(200)
        ck("输入关键词后意图标签出现", pg.evaluate("() => !document.getElementById('homeIntent').hidden"))
        pg.click("#homeGo")
        pg.wait_for_timeout(900)
        ck("点开始抓取跳到图片页",
           pg.evaluate("() => (document.querySelector('.tab.active')||{}).dataset?.tab") == "image")
        ck("关键词已同步到图片页输入框",
           pg.evaluate("() => document.getElementById('omniInput').value") == "雪山 星空 4K")

        # ---------- 3. 图片页 keyword 模式：筛选按钮在 ----------
        pg.wait_for_timeout(600)
        ck("关键词搜图显示「筛选设置」",
           pg.evaluate("() => { const b=document.getElementById('btnFilterDrawer');"
                       " return b && !b.hidden && getComputedStyle(b).display!=='none'; }"))
        chip_txt = pg.evaluate("() => document.getElementById('activeChips').textContent")
        ck("筛选条没有「不重复下载」chip", "不重复下载" not in chip_txt, chip_txt)

        # ---------- 4. 图片页 link 模式：筛选按钮消失 ----------
        pg.click('#omniSeg button[data-mode="link"]')
        pg.wait_for_timeout(600)
        ck("粘贴链接抓取不显示「筛选设置」",
           pg.evaluate("() => { const b=document.getElementById('btnFilterDrawer');"
                       " return !b || b.hidden || getComputedStyle(b).display==='none'; }"))
        ck("切回关键词后按钮回来",
           (pg.click('#omniSeg button[data-mode="kw"]'),
            pg.wait_for_timeout(600),
            pg.evaluate("() => { const b=document.getElementById('btnFilterDrawer');"
                        " return b && !b.hidden; }"))[2])

        # ---------- 5. 记忆：停在图片页，重开还是图片页 ----------
        pg.click('.sidebar .nav-item[data-tab="video"]')
        pg.wait_for_timeout(500)
        pg.reload(wait_until="domcontentloaded")
        pg.wait_for_timeout(1800)
        ck("重开后回到上次的「抓取视频」",
           pg.evaluate("() => (document.querySelector('.tab.active')||{}).dataset?.tab") == "video")

        # ---------- 6. 清掉记忆 = 新用户 ----------
        pg.evaluate("() => localStorage.removeItem('md_last_tab')")
        pg.reload(wait_until="domcontentloaded")
        pg.wait_for_timeout(1800)
        ck("清掉记忆后回到首页",
           pg.evaluate("() => (document.querySelector('.tab.active')||{}).dataset?.tab") == "home")

        # ---------- 7. 卡片入口 ----------
        pg.click('.home-card[data-home-go="image"]')
        pg.wait_for_timeout(500)
        ck("首页卡片进入抓取图片",
           pg.evaluate("() => (document.querySelector('.tab.active')||{}).dataset?.tab") == "image")
        pg.click('.sidebar .nav-item[data-tab="home"]')
        pg.wait_for_timeout(400)
        pg.click('.home-card[data-home-go="video"]')
        pg.wait_for_timeout(500)
        ck("首页卡片进入抓取视频",
           pg.evaluate("() => (document.querySelector('.tab.active')||{}).dataset?.tab") == "video")

        # ---------- 8. 深色主题下首页仍可读 ----------
        pg.evaluate("() => { document.documentElement.dataset.mode='dark'; }")
        pg.click('.sidebar .nav-item[data-tab="home"]')
        pg.wait_for_timeout(500)
        colors = pg.evaluate("""() => {
          const t = getComputedStyle(document.querySelector('.home-title')).color;
          const bg = getComputedStyle(document.body).backgroundColor;
          return {title: t, bg: bg};
        }""")
        ck("深色主题下标题与背景不同色", colors["title"] != colors["bg"], colors)

        ck("console / pageerror 为 0", not errs, errs[:3])
        print(f"\n结果：{len(PASS)} 通过 / {len(FAIL)} 失败")
        b.close()
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
