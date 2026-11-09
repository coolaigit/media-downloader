#!/usr/bin/env python
"""V3.0.4 验收：版本号 / 首页抓取通道 / 工作台版面。

用法：
    _venv/Scripts/python.exe _verify_v304.py                 # 默认 http://127.0.0.1:8852
    _venv/Scripts/python.exe _verify_v304.py http://127.0.0.1:18870   # 打包版

覆盖 2026-11-07 用户三条反馈：
1. 版本号定版 3.0.4（health / 页面标题 / 首屏 kicker / 顶栏副标 / 样式版本串）；
2. 首页「抓取默认是图片通道吗？贴视频链接怎么办」—— 通道控件 + 站点自动识别 + 手动优先；
3. 「抓取图片 / 抓取视频内容都在顶上，下面大块空白」—— 面板撑满视口、
   结果区吃掉余量、空态居中、视频页长说明收进折叠。
"""
from __future__ import annotations

import json
import sys
import urllib.request

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8852"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
EXPECT_VERSION = "3.0.4"

results: list[tuple[str, bool, str]] = []


def ck(name: str, ok: bool, detail: object = "") -> None:
    results.append((name, bool(ok), str(detail)))
    print(("  PASS  " if ok else "  FAIL  ") + name + ("" if ok else "  << " + str(detail)), flush=True)


def api(path: str) -> dict:
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with op.open(BASE + path, timeout=8) as r:
        return json.loads(r.read().decode("utf-8"))


# 前端会轮询 /api/status、/api/history、/api/crawl/status/...，全部兜住，
# 只验前端分派与版面，不真的碰后端（也就不会因为假 job_id 刷 404 到 console）
STUB = ('{"ok":true,"job_id":"verify","status":"completed","items":[],"count":0,'
        '"stats":{},"platforms":{},"files":[],"root":"","note":""}')


def main() -> int:
    try:
        health = api("/api/health")
    except Exception as e:  # noqa: BLE001
        print("health 读不到：" + str(e))
        health = {}

    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=CHROME, headless=True,
                              args=["--no-sandbox", "--disable-gpu", "--no-proxy-server"])
        ctx = b.new_context(viewport={"width": 1600, "height": 950})
        pg = ctx.new_page()
        errs: list[str] = []
        pg.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.route("**/api/**", lambda r: r.fulfill(status=200, content_type="application/json", body=STUB))

        pg.goto(BASE + "/", wait_until="domcontentloaded")
        pg.wait_for_timeout(3200)
        if pg.locator("#firstUseWizard").is_visible():
            pg.click("#wizardClose")
            pg.wait_for_timeout(400)

        # ---------- 一、版本号 ----------
        print("\n[版本号]", flush=True)
        ck("health 返回 version=" + EXPECT_VERSION,
           health.get("version") == EXPECT_VERSION, health.get("version"))
        ck("页面标题带 " + EXPECT_VERSION, EXPECT_VERSION in pg.title(), pg.title())
        kicker = pg.evaluate("() => (document.querySelector('.home-kicker')||{}).textContent || ''")
        ck("首屏 kicker 带版本号", EXPECT_VERSION in kicker, kicker)
        brand = pg.evaluate("() => (document.querySelector('.brand h1')||{}).textContent || ''")
        ck("顶栏副标显示 " + EXPECT_VERSION, EXPECT_VERSION in brand, brand)
        href = pg.evaluate("() => (document.querySelector('link[rel=stylesheet]')||{}).getAttribute('href') || ''")
        ck("样式版本串跟着升级", EXPECT_VERSION in href, href)

        # ---------- 二、首页抓取通道 ----------
        print("\n[首页抓取通道]", flush=True)
        chan = pg.evaluate("""() => {
          const box = document.getElementById('homeChan');
          const on = box && box.querySelector('button.on');
          return {exists: !!box, count: box ? box.querySelectorAll('button').length : 0,
                  on: on ? on.dataset.chan : null};
        }""")
        ck("首页有图片/视频两个通道按钮", chan["exists"] and chan["count"] == 2, chan)
        ck("默认落在图片通道", chan["on"] == "image", chan)

        def fill(url: str) -> dict:
            pg.evaluate("switchTab('home')")
            pg.wait_for_timeout(200)
            pg.fill("#homeInput", url)
            pg.wait_for_timeout(350)
            return pg.evaluate("""() => ({
              chan: (document.querySelector('#homeChan button.on')||{}).dataset?.chan,
              intent: (document.getElementById('homeIntent')||{}).textContent || '',
              cls: (document.getElementById('homeIntent')||{}).className || '',
            })""")

        s = fill("https://www.kuaishou.com/profile/3xnmi2xh2pxzpag")
        ck("粘贴快手主页 → 自动切视频通道", s["chan"] == "video", s)
        ck("意图标签写明是视频", "视频" in s["intent"] and "video" in s["cls"], s)
        s = fill("https://www.bilibili.com/video/BV1xx411c7mD")
        ck("粘贴 B 站链接 → 视频通道", s["chan"] == "video", s)
        s = fill("https://www.pexels.com/search/led/")
        ck("粘贴图片站链接 → 回到图片通道", s["chan"] == "image", s)
        s = fill("雪山 星空 4K")
        ck("关键词不改变已选通道", s["chan"] == "image", s)

        # 手动选择只锁当前这条输入
        pg.click('#homeChan button[data-chan="video"]')
        pg.wait_for_timeout(250)
        s = pg.evaluate("() => document.querySelector('#homeChan button.on').dataset.chan")
        ck("点「视频」当场生效", s == "video", s)
        pg.fill("#homeInput", "https://www.pexels.com/search/led/")
        pg.wait_for_timeout(350)
        s = pg.evaluate("() => document.querySelector('#homeChan button.on').dataset.chan")
        ck("换了一条新链接 → 重新按站点判断", s == "image", s)

        # ---------- 三、一条输入按通道分派 ----------
        print("\n[通道分派]", flush=True)

        def dispatch(url: str) -> dict:
            pg.evaluate("switchTab('home')")
            pg.wait_for_timeout(250)
            pg.fill("#homeInput", url)
            pg.wait_for_timeout(400)
            pg.click("#homeGo")
            pg.wait_for_timeout(900)
            return pg.evaluate("""() => ({
              tab: (document.querySelector('.tab.active')||{}).dataset?.tab,
              imgLink: !document.getElementById('imgLinkMode').hidden,
              vidLink: !document.getElementById('vidLinkMode').hidden,
              imgCrawl: document.getElementById('imgCrawlInput').value,
              vidCrawl: document.getElementById('vidCrawlInput').value,
              probe: document.getElementById('videoInput').value,
              kwImg: document.getElementById('kwInput').value,
            })""")

        d = dispatch("https://www.pexels.com/search/led/")
        ck("图片链接 → 图片页 + 链接抓取", d["tab"] == "image" and d["imgLink"], d)
        ck("图片链接已填进抓取输入框", d["imgCrawl"].endswith("/search/led/"), d)

        d = dispatch("https://www.kuaishou.com/profile/3xnmi2xh2pxzpag")
        ck("快手主页 → 视频页 + 批量抓取", d["tab"] == "video" and d["vidLink"], d)
        ck("快手主页已填进视频抓取框", d["vidCrawl"].endswith("/profile/3xnmi2xh2pxzpag"), d)

        d = dispatch("https://www.bilibili.com/video/BV1xx411c7mD")
        ck("B 站单个视频 → 走解析而不是批量抓",
           d["tab"] == "video" and d["probe"].endswith("BV1xx411c7mD"), d)

        d = dispatch("雪山 星空 4K")
        ck("关键词 + 图片通道 → 图片页关键词模式", d["tab"] == "image" and not d["imgLink"], d)
        ck("关键词已写入图片搜索框", d["kwImg"] == "雪山 星空 4K", d)

        # 图片页 omnibox 的输入属于**图片通道**：贴视频链接也不能把整条输入
        # 甩到视频页。2026-11-07 用户实测：在「抓取图片」的框里贴小红书博主主页，
        # 被自动识别成视频、整条跳到视频抓取，与「我要抓这页的图」相反。
        # 自动识别只在首页成立（首页有可见的通道控件）。
        pg.evaluate("switchTab('image')")
        pg.wait_for_timeout(250)
        pg.fill("#omniInput", "https://www.kuaishou.com/profile/3xnmi2xh2pxzpag")
        pg.wait_for_timeout(350)
        intent = pg.evaluate("() => (document.getElementById('omniIntent')||{}).textContent || ''")
        ck("图片页 omnibox 贴视频链接 → 标签仍写图片", "图片" in intent, intent)
        pg.click("#omniGo")
        pg.wait_for_timeout(900)
        d = pg.evaluate("""() => {
          // 不能只看 .hidden：hidden 加在 .panel 上，内部 workpane 的 hidden
          // 一直是 false。要的是「实际可见」，用 offsetParent 判。
          const vis = el => !!el && !el.hidden && el.offsetParent !== null;
          return {tab: (document.querySelector('.tab.active')||{}).dataset?.tab,
                  imgLink: vis(document.getElementById('imgLinkMode')),
                  vidLink: vis(document.getElementById('vidLinkMode'))};
        }""")
        ck("图片页 omnibox 贴视频链接 → 仍走图片抓取",
           d["tab"] == "image" and d["imgLink"] and not d["vidLink"], d)

        # ---------- 四、版面：面板撑满，空白有归属 ----------
        print("\n[工作台版面]", flush=True)
        ph = pg.evaluate("() => getComputedStyle(document.documentElement).getPropertyValue('--panel-h').trim()")
        ck("--panel-h 已按视口算出", ph.endswith("px") and int(ph[:-2]) > 400, ph)

        def pane(tab: str, mode: str) -> dict:
            pg.evaluate("switchTab('" + tab + "')")
            pg.wait_for_timeout(300)
            if tab == "image":
                pg.click('#omniSeg button[data-mode="' + mode + '"]')
            else:
                pg.click('#videoModeSwitch .mode-btn[data-mode="' + mode + '"]')
            pg.wait_for_timeout(650)
            # 切页 / 定位会把页面滚下去，测量前面板位置就不准了
            pg.evaluate("window.scrollTo(0, 0)")
            pg.wait_for_timeout(150)
            return pg.evaluate("""() => {
              const panel = document.querySelector('.panel.active');
              const pr = panel.getBoundingClientRect();
              const pane = panel.querySelector('.workpane:not([hidden])');
              // 同一个 workpane 里可能同时存在 .grid 与 .vlist，取真正有高度的那个
              const cands = pane ? [...pane.querySelectorAll('.grid, .vlist')] : [];
              const grid = cands.find(e => e.getBoundingClientRect().height > 0) || cands[0] || null;
              const empty = grid ? grid.querySelector('.empty') : null;
              // 图片页用 .searchbar（sr-only 隐藏），视频关键词模式用 .video-search
              const sb = pane && pane.querySelector('.searchbar, .video-search');
              const bar = sb ? sb.getBoundingClientRect() : null;
              const note = pane && pane.querySelector('.pane-note');
              const nr = note ? note.getBoundingClientRect() : null;
              return {
                panelTop: Math.round(pr.top), panelH: Math.round(pr.height),
                winH: window.innerHeight,
                gridTop: grid ? Math.round(grid.getBoundingClientRect().top) : null,
                gridH: grid ? Math.round(grid.getBoundingClientRect().height) : null,
                emptyH: empty ? Math.round(empty.getBoundingClientRect().height) : 0,
                emptyW: empty ? Math.round(empty.getBoundingClientRect().width) : 0,
                searchH: bar ? Math.round(bar.height) : null,
                searchW: bar ? Math.round(bar.width) : null,
                dupSwitch: !!panel.querySelector('#imageModeSwitch'),
                noteOpen: note ? note.open : null,
                noteH: nr ? Math.round(nr.height) : null,
              };
            }""")

        for tab, mode in (("image", "kw"), ("image", "link"), ("video", "kw"), ("video", "link")):
            m = pane(tab, mode)
            tag = tab + "/" + mode
            ck(tag + "：面板吃满视口",
               m["panelH"] >= m["winH"] - m["panelTop"] - 24, m)
            ck(tag + "：结果区撑到面板底部",
               m["gridH"] is not None and m["gridH"] >= 200
               and abs((m["gridTop"] + m["gridH"]) - (m["panelTop"] + m["panelH"])) <= 48, m)
            ck(tag + "：空态占满结果区并居中",
               m["emptyH"] > 0 and m["emptyW"] > 400 and abs(m["emptyH"] - m["gridH"]) <= 4, m)
            ck(tag + "：没有重复的模式条", not m["dupSwitch"], m)
            if tab == "video":
                # 图片页的两条 .searchbar 是 sr-only 隐藏的（输入走顶部 omnibox），
                # 只有视频页的搜索行是可见控件，这里只对它做「不被压扁」断言
                ck(tag + "：搜索行没有被压扁",
                   (m["searchH"] or 0) >= 30 and (m["searchW"] or 0) > 600, m)
                if mode == "kw":
                    ck(tag + "：长说明默认折叠成一行",
                       m["noteOpen"] is False and 20 <= (m["noteH"] or 0) <= 60, m)

        # ---------- 五、没有回归 ----------
        print("\n[回归]", flush=True)
        pg.evaluate("switchTab('image')")
        pg.wait_for_timeout(300)
        pg.click('#omniSeg button[data-mode="kw"]')
        pg.wait_for_timeout(300)
        ck("关键词模式筛选按钮可见",
           pg.evaluate("() => { const b=document.getElementById('btnFilterDrawer'); return !!b && !b.hidden; }"))
        pg.click('#omniSeg button[data-mode="link"]')
        pg.wait_for_timeout(300)
        ck("链接模式筛选按钮隐藏",
           pg.evaluate("() => { const b=document.getElementById('btnFilterDrawer'); return !b || b.hidden; }"))
        ck("console / pageerror 为 0", not errs, errs[:3])

        b.close()

    bad = [r for r in results if not r[1]]
    print("\n结果：%d 通过 / %d 失败" % (len(results) - len(bad), len(bad)), flush=True)
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
