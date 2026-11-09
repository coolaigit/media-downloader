# -*- coding: utf-8 -*-
"""真实浏览器验收：统计卡可用、顶部控件可读可点、按钮有反馈、抓取参数默认折叠。"""
from __future__ import annotations

import json
import sys
from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8852"
# 传参可指向打包版实例（8871），源码版默认 8852
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
errs: list[str] = []
bad_resp: list[str] = []
checks: list[tuple[str, bool, str]] = []


def ck(name, cond, extra=""):
    checks.append((name, bool(cond), str(extra)))
    print(("  ok   " if cond else "  FAIL ") + name + (" " + str(extra) if not cond else ""),
          flush=True)


def main() -> int:
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=CHROME, headless=True,
                              args=["--no-sandbox", "--disable-gpu",
                                    "--no-proxy-server"])
        pg = b.new_page(viewport={"width": 1600, "height": 950})
        pg.on("pageerror", lambda e: errs.append("pageerror: " + str(e)[:200]))
        pg.on("console", lambda m: errs.append("console: " + m.text[:200])
              if m.type == "error" else None)
        # console 的「Failed to load resource」不带 URL，出问题时没法定位是哪个请求
        pg.on("response", lambda r: bad_resp.append(f"{r.status} {r.url[:160]}")
              if r.status >= 400 else None)
        pg.goto(BASE + "/", wait_until="domcontentloaded")
        pg.wait_for_timeout(2000)
        # 打包版首次运行会弹「首次使用向导」，挡住所有点击，先关掉
        if pg.locator("#firstUseWizard").is_visible():
            pg.click("#wizardClose")
            pg.wait_for_timeout(300)
        # 2026-11-07 起默认落点是首页（有记忆则回上次页面），
        # 下面验的是图片工作台，先显式切过去。
        pg.evaluate("switchTab('image')")
        pg.wait_for_timeout(500)
        pg.click('#omniSeg button[data-mode="link"]')
        pg.wait_for_timeout(400)

        # ---- 红框：图片页不再有视频式抓取面板，只留一行 4 项 ----
        s = pg.evaluate("""() => {
          const e = document.getElementById('imgCrawlOptions');
          const txt = document.getElementById('panel-image').innerText;
          const vis = id => { const n = document.getElementById(id);
                              if (!n) return null; const r = n.getBoundingClientRect();
                              return {w: Math.round(r.width), h: Math.round(r.height),
                                      shown: !!n.offsetParent}; };
          return {tag: e.tagName, height: Math.round(e.getBoundingClientRect().height),
                  summary: !!e.querySelector('summary'),
                  filterLabel: document.getElementById('filterLabel').textContent,
                  vidWords: ['抓取模式', '全站抓取', '跟随链接层数', '最多作品']
                              .filter(w => txt.indexOf(w) >= 0),
                  opts: {pages: vis('imgCPageCount'), max: vis('imgCMaxItems'),
                         drill: vis('imgCDrill'), originals: vis('imgCOriginals')},
                  hiddenField: vis('imgCMode')};
        }""")
        print("crawl-opts", json.dumps(s, ensure_ascii=False), flush=True)
        ck("图片参数从大面板改成一行内联",
           s["tag"] == "DIV" and not s["summary"] and s["height"] < 70, s)
        ck("图片页不再出现视频式抓取术语", not s["vidWords"], s["vidWords"])
        ck("4 个图片参数控件可见",
           all(o and o["shown"] and o["h"] >= 24 for o in s["opts"].values()), s["opts"])
        ck("共用抓取器的隐藏字段不占位",
           s["hiddenField"] is None or not s["hiddenField"]["shown"], s["hiddenField"])
        ck("筛选按钮已改名「筛选设置」", s["filterLabel"] == "筛选设置", s)

        # 视频页那份完整面板仍然保留（视频确实要整站 / 层级 / 下钻）
        pg.click('.sidebar .nav-item[data-tab="video"]')
        pg.wait_for_timeout(400)
        v = pg.evaluate("""() => {
          const e = document.getElementById('vidCrawlOptions');
          // 视频页当时在「关键词搜视频」，抓取面板所在容器是 hidden，
          // innerText 拿不到隐藏文本，必须用 textContent。
          return {tag: e.tagName, open: e.open,
                  txt: e.textContent,
                  shown: !!e.offsetParent};
        }""")
        ck("视频页保留完整抓取面板且默认收起",
           v["tag"] == "DETAILS" and v["open"] is False
           and "全站抓取" in v["txt"] and "最多作品" in v["txt"], v)
        # 切到视频页的「粘贴链接抓取」，确认面板真的可见（不是只存在于 DOM）
        pg.click('#panel-video .modeswitch button[data-mode="link"]')
        pg.wait_for_timeout(350)
        vv = pg.evaluate("""() => {
          const e = document.getElementById('vidCrawlOptions');
          const r = e.getBoundingClientRect();
          return {shown: !!e.offsetParent, h: Math.round(r.height),
                  summary: (e.querySelector('summary')||{}).textContent || ''};
        }""")
        ck("视频链接模式下抓取面板可见且收起成一行",
           vv["shown"] and vv["h"] < 90 and "抓取参数" in vv["summary"], vv)
        pg.click('#panel-video .modeswitch button[data-mode="kw"]')
        pg.wait_for_timeout(300)
        pg.click('.sidebar .nav-item[data-tab="image"]')
        pg.wait_for_timeout(300)

        # ---- 绿框：字号 / 选中态 / chip 可交互 ----
        ui = pg.evaluate("""() => {
          const on = document.querySelector('#omniSeg button.on');
          const off = document.querySelector('#omniSeg button:not(.on)');
          const cs = e => { const x = getComputedStyle(e); return {fs: x.fontSize, color: x.color, weight: x.fontWeight}; };
          return {on: cs(on), off: cs(off),
                  chips: Array.from(document.querySelectorAll('#activeChips .chip'))
                              .map(c => ({tag: c.tagName, text: c.textContent.trim(),
                                          title: c.title, toggle: c.dataset.toggle || ''}))};
        }""")
        print("topbar", json.dumps(ui, ensure_ascii=False), flush=True)
        ck("模式按钮字号放大到 13.5px", ui["on"]["fs"] == "13.5px", ui)
        ck("选中态加粗且颜色不同于未选中",
           ui["on"]["weight"] == "700" and ui["on"]["color"] != ui["off"]["color"], ui)
        ck("已选条件 chip 是按钮且带说明",
           all(c["tag"] == "BUTTON" and c["title"] for c in ui["chips"]), ui)
        # 2026-11-07 用户反馈：默认开启的「不重复下载」不该常驻在筛选条上，
        # 而且它点一下取消勾选就整个消失，看着像被删掉了。开关本体留在抽屉里。
        ck("筛选条不常驻「不重复下载」",
           all("不重复下载" not in c["text"] for c in ui["chips"])
           and pg.evaluate("document.getElementById('fSkipExisting').checked") is True, ui["chips"])

        # ---- 蓝框：结果/已选实时联动 + 4 张卡可点 ----
        pg.evaluate("""() => {
          const t = document.getElementById('imgCrawlToolbar'); t.hidden = false;
          const g = document.getElementById('imgCrawlGrid');
          g.innerHTML = '<div class="card"></div><div class="card"></div><div class="card"></div>';
          document.getElementById('imgCrawlSelCount').textContent = '2';
        }""")
        pg.wait_for_timeout(500)
        stats = pg.evaluate("""() => ({
          result: document.getElementById('statResult').textContent,
          sel: document.getElementById('statSel').textContent,
          clickable: document.querySelectorAll('.hstat-clickable').length,
        })""")
        print("stats", json.dumps(stats, ensure_ascii=False), flush=True)
        ck("本次结果跟随卡片数变成 3", stats["result"] == "3", stats)
        ck("已选跟随勾选数变成 2", stats["sel"] == "2", stats)
        ck("4 张统计卡都绑定了点击", stats["clickable"] == 4, stats)
        pg.click('.hstat:nth-child(4)')
        pg.wait_for_timeout(350)
        tab = pg.evaluate("(document.querySelector('.tab.active')||{}).dataset?.tab || ''")
        ck("点资源库卡切到已下载页", tab == "library", tab)
        pg.click('.sidebar .nav-item[data-tab="image"]')
        pg.wait_for_timeout(300)

        # ---- 按钮反馈 ----
        pg.evaluate("""() => document.getElementById('omniGo')
                       .dispatchEvent(new PointerEvent('pointerdown', {bubbles:true}))""")
        pg.wait_for_timeout(60)
        pressed = pg.evaluate("document.getElementById('omniGo').classList.contains('is-pressed')")
        ck("按下瞬间有 is-pressed 反馈", pressed is True, pressed)
        pg.wait_for_timeout(300)
        released = pg.evaluate("document.getElementById('omniGo').classList.contains('is-pressed')")
        ck("反馈会自动收起", released is False, released)
        loading = pg.evaluate("""() => {
          const b = document.getElementById('omniGo');
          b.classList.add('is-loading');
          const pe = getComputedStyle(b).pointerEvents;
          b.classList.remove('is-loading');
          return pe;
        }""")
        ck("抓取中态会挡住重复点击", loading == "none", loading)

        # ---- 顶部「开始抓取」：点下去必须立刻有可见变化 ----
        # 图片页粘贴链接模式的 .searchbar 是隐藏的（输入走顶部 omnibox），
        # 用户实际点的就是 #omniGo。把搜图接口换成永不返回的桩，观察按钮的抓取中态。
        pg.click('#omniSeg button[data-mode="kw"]')
        pg.wait_for_timeout(250)
        pg.evaluate("""() => {
          const orig = window.fetch;
          window.fetch = (u, o) => String(u).indexOf('/api/search/images') >= 0
            ? new Promise(() => {}) : orig(u, o);
        }""")
        pg.fill('#omniInput', '风景 壁纸 4K')
        pg.click('#omniGo')
        pg.wait_for_timeout(400)
        busy = pg.evaluate("""() => {
          const b = document.getElementById('omniGo');
          const cs = getComputedStyle(b);
          return {text: b.textContent.trim(), loading: b.classList.contains('is-loading'),
                  opacity: cs.opacity, spin: !!b.querySelector('.spin'),
                  prev: (b.dataset.prevHtml || '').indexOf('开始抓取') >= 0};
        }""")
        print("omni-btn", json.dumps(busy, ensure_ascii=False), flush=True)
        ck("点「开始抓取」立刻变成抓取中",
           busy["loading"] and "抓取中" in busy["text"] and busy["spin"], busy)
        ck("抓取中按钮不变灰", float(busy["opacity"]) >= 0.9, busy)
        ck("结束后能恢复原文案", busy["prev"], busy)

        b.close()
    print("ERRS", json.dumps(errs[:8], ensure_ascii=False),
          "BAD_RESP", json.dumps(bad_resp[:8], ensure_ascii=False), flush=True)
    ck("console / pageerror 为 0", not errs, errs[:3])
    bad = [n for n, ok, _ in checks if not ok]
    print("RESULT", "ALL PASS" if not bad else "FAILED " + str(bad), flush=True)
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
