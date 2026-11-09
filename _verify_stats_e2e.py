# -*- coding: utf-8 -*-
"""端到端验收：真抓一次本地图片站，看 4 列统计是不是真的跟着结果走。

用户 2026-11-07 蓝框反馈：4 列统计「徒有其表」，选中图片也不变。
这里不注入假 DOM，走完整链路：提交抓取 -> 后端抓 -> 前端渲染 -> 读统计。
"""
from __future__ import annotations

import json
import sys

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8852"
FIXTURE = "https://www.pexels.com/search/led/"
# 后端拒绝内网地址（SSRF 防护），夹具站点只能用来验离线逻辑，
# 端到端这条必须用公网站点；pexels 是纯 JS 渲染，抓取时前端默认勾了「允许渲染」。
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"

checks: list[tuple[str, bool, str]] = []
errs: list[str] = []


def ck(name, cond, extra=""):
    checks.append((name, bool(cond), str(extra)))
    print(("  ok   " if cond else "  FAIL ") + name + (" " + str(extra) if not cond else ""),
          flush=True)


def main() -> int:
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=CHROME, headless=True,
                              args=["--no-sandbox", "--disable-gpu", "--no-proxy-server"])
        pg = b.new_page(viewport={"width": 1600, "height": 1000})
        pg.on("pageerror", lambda e: errs.append("pageerror: " + str(e)[:200]))
        pg.on("console", lambda m: errs.append("console: " + m.text[:200])
              if m.type == "error" else None)
        pg.goto(BASE + "/", wait_until="domcontentloaded")
        pg.wait_for_timeout(1500)

        pg.click('#omniSeg button[data-mode="link"]')
        pg.wait_for_timeout(300)
        pg.fill("#omniInput", FIXTURE)
        pg.wait_for_timeout(200)
        pg.click("#omniGo")

        # 最多等 90 秒抓完（本地站点通常 3~10 秒）
        ok = False
        for _ in range(100):
            pg.wait_for_timeout(1000)
            st = pg.evaluate("""() => {
              const t = document.getElementById('imgCrawlToolbar');
              const s = document.getElementById('imgCrawlStat');
              const done = document.getElementById('imgCrawlProgress');
              return {toolbar: !t.hidden, stat: (s.textContent || '').trim(),
                      cards: document.querySelectorAll('#imgCrawlGrid .card').length,
                      prog: (done.hidden ? '' : (document.getElementById('imgCrawlStage').textContent || '').trim())};
            }""")
            if st["toolbar"] and st["cards"] > 0:
                ok = True
                break
        print("crawl", json.dumps(st, ensure_ascii=False), flush=True)
        ck("抓取结束且渲染出结果卡片", ok and st["cards"] >= 6, st)
        ck("抓取摘要里有真实数量", any(ch.isdigit() for ch in st["stat"]), st["stat"])

        pg.wait_for_timeout(600)
        stats = pg.evaluate("""() => ({
          result: document.getElementById('statResult').textContent.trim(),
          sel: document.getElementById('statSel').textContent.trim(),
          cards: document.querySelectorAll('#imgCrawlGrid .card').length,
        })""")
        print("stats-after-crawl", json.dumps(stats, ensure_ascii=False), flush=True)
        ck("「本次结果」等于真实卡片数",
           stats["result"] == str(stats["cards"]) and stats["cards"] > 0, stats)
        ck("「已选」初始为 0", stats["sel"] == "0", stats)

        # 点第一张卡：统计必须立刻 +1（卡片用 .card-check，不是 checkbox）
        pg.click("#imgCrawlGrid .card")
        pg.wait_for_timeout(400)
        sel = pg.evaluate("""() => ({
          statSel: document.getElementById('statSel').textContent.trim(),
          real: (document.getElementById('imgCrawlSelCount').textContent || '').trim(),
        })""")
        print("stats-after-select", json.dumps(sel, ensure_ascii=False), flush=True)
        ck("勾一张后「已选」立刻变 1", sel["statSel"] == "1", sel)

        # 全选
        pg.click("#imgBtnCrawlSelAll")
        pg.wait_for_timeout(500)
        allsel = pg.evaluate("""() => ({
          statSel: document.getElementById('statSel').textContent.trim(),
          real: (document.getElementById('imgCrawlSelCount').textContent || '').trim(),
          cards: document.querySelectorAll('#imgCrawlGrid .card').length,
        })""")
        print("stats-after-all", json.dumps(allsel, ensure_ascii=False), flush=True)
        ck("全选后「已选」等于卡片数",
           allsel["statSel"] == allsel["real"] and int(allsel["statSel"] or 0) == allsel["cards"],
           allsel)

        pg.screenshot(path="_shot_stats.png", full_page=False)
        b.close()

    print("ERRS", json.dumps(errs[:6], ensure_ascii=False), flush=True)
    ck("console / pageerror 为 0", not errs, errs[:3])
    bad = [n for n, ok, _ in checks if not ok]
    print("RESULT", "ALL PASS" if not bad else "FAILED " + str(bad), flush=True)
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
