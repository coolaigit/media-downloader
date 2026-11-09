# -*- coding: utf-8 -*-
"""V3.0.8 端到端（真实 Chrome）：裸域名不再被当成关键词搜图，手选模式不被覆盖。

覆盖 Sam 2026-11-08 报的三件事：
  1. 图片页粘 `weibo.com/u/1844960024?tabtype=newVideo` 选「粘贴链接抓取」
     → 必须走 /api/crawl/start，且模式按钮保持「粘贴链接抓取」；
  2. 不能跳到视频页（F-03：页签就是通道）；
  3. 带协议头的链接、其它站裸域名、纯关键词的行为都不能被带坏。

不真打外网：搜索 / 抓取 / 轮询三个接口全挂桩，只记录调用与请求体。

用法：_venv/Scripts/python.exe test_bare_url_ui.py [http://127.0.0.1:18848]
"""
from __future__ import annotations

import json
import sys

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:18848"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"

PASS: list[str] = []
FAIL: list[str] = []
CALLS: list[tuple[str, str]] = []


def ck(name: str, ok: bool, detail: object = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(("  PASS  " if ok else "  FAIL  ") + name + ("" if ok else f"  <- {detail}"))


def main() -> int:
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=CHROME, headless=True,
                              args=["--no-sandbox", "--disable-gpu", "--no-proxy-server"])
        ctx = b.new_context(viewport={"width": 1600, "height": 950})
        pg = ctx.new_page()

        def record(route) -> None:
            req = route.request
            CALLS.append((req.url.split("/api/")[-1].split("?")[0],
                          req.post_data or ""))
            # status 必须给 status=completed，前端才会停轮询并解除抓取按钮的 busy；
            # 否则下一个 case 点按钮是 disabled，crawl/start 根本发不出去。
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"items": [], "total": 0,
                                           "engines": [], "job_id": "stub",
                                           "state": "done", "status": "completed",
                                           "stage_label": "已完成", "progress": 100,
                                           "media_found": 0, "elapsed": 0,
                                           "warnings": [], "summary": {},
                                           "detail_pages": {},
                                           "result": {"items": []}}))

        for pat in ("**/api/search/images", "**/api/crawl/start",
                    "**/api/crawl/status/**"):
            pg.route(pat, record)

        pg.goto(BASE + "/", wait_until="domcontentloaded")
        pg.wait_for_timeout(1800)
        pg.evaluate("() => { const t=document.querySelector('.tab[data-tab=\"image\"]'); if(t) t.click(); }")
        pg.wait_for_timeout(300)

        def reset(mode: str, value: str) -> None:
            CALLS.clear()
            pg.evaluate("""(m) => {
                const b=document.querySelector('#omniSeg button[data-mode="'+m+'"]');
                if(b) b.click();
                const w=document.getElementById('toastWrap'); if(w) w.innerHTML='';
                const cb=document.getElementById('imgBtnCrawl');
                if(cb){cb.disabled=false; delete cb.dataset.busy;}
                const o=document.getElementById('omniInput'); o.value='';
                o.dispatchEvent(new Event('input',{bubbles:true}));
            }""", mode)
            pg.fill("#omniInput", value)
            pg.wait_for_timeout(120)

        def snap() -> dict:
            return pg.evaluate("""() => ({
                mode: (document.querySelector('#omniSeg button.on')||{}).dataset?.mode,
                tab: (document.querySelector('.tab.active')||{}).dataset?.tab,
                omni: document.getElementById('omniInput').value,
                crawl: (document.getElementById('imgCrawlInput')||{}).value,
                kw: (document.getElementById('kwInput')||{}).value,
                intent: document.getElementById('omniIntent').textContent,
                toast: (document.querySelector('#toastWrap .toast')||{}).textContent || '',
            })""")

        # ---------- 1. 用户实测的那条：裸 weibo + 手选「粘贴链接抓取」 ----------
        reset("link", "weibo.com/u/1844960024?tabtype=newVideo")
        pg.click("#omniGo")
        pg.wait_for_timeout(900)
        s = snap()
        apis = [c[0] for c in CALLS]
        bodies = [c[1] for c in CALLS if c[0] == "crawl/start"]
        ck("裸 weibo 走 /api/crawl/start", "crawl/start" in apis, apis)
        ck("裸 weibo 不再调 Bing 搜图", "search/images" not in apis, apis)
        ck("手选「粘贴链接抓取」没被改回", s["mode"] == "link", s["mode"])
        ck("仍停在抓取图片页（F-03）", s["tab"] == "image", s["tab"])
        ck("意图标签按图片链接显示", "图片 · weibo.com" in (s["intent"] or ""), s["intent"])
        ck("输入框已补成 https://", (s["omni"] or "").startswith("https://weibo.com/"), s["omni"])
        ck("抓取输入框同步补协议", (s["crawl"] or "").startswith("https://weibo.com/"), s["crawl"])
        ok_body = False
        if bodies:
            try:
                ok_body = json.loads(bodies[0]).get("url", "").startswith("https://weibo.com/")
            except Exception:
                pass
        ck("提交后端的 url 是完整 https 地址", ok_body, bodies[:1])

        # ---------- 2. 带协议头的同一条链接（对照，不能带坏） ----------
        reset("link", "https://weibo.com/u/1844960024?tabtype=newVideo")
        pg.click("#omniGo")
        pg.wait_for_timeout(700)
        apis = [c[0] for c in CALLS]
        s = snap()
        ck("带协议的 weibo 仍走抓取", "crawl/start" in apis and "search/images" not in apis, apis)
        ck("带协议时模式保持 link", s["mode"] == "link", s["mode"])

        # ---------- 3. 别的站裸域名也一样（不是微博专属） ----------
        reset("link", "douyin.com/video/7635098784464778851")
        pg.click("#omniGo")
        pg.wait_for_timeout(700)
        apis = [c[0] for c in CALLS]
        s = snap()
        ck("裸 douyin 走抓取、不搜图", "crawl/start" in apis and "search/images" not in apis, apis)
        ck("裸 douyin 不跳视频页（F-03）", s["tab"] == "image", s["tab"])
        ck("裸 douyin 也补协议", (s["omni"] or "").startswith("https://douyin.com/"), s["omni"])

        # ---------- 4. 关键词搜图（默认模式）不受影响 ----------
        reset("kw", "风景 壁纸 4K")
        pg.click("#omniGo")
        pg.wait_for_timeout(700)
        apis = [c[0] for c in CALLS]
        s = snap()
        ck("纯关键词仍走 Bing 搜图", "search/images" in apis and "crawl/start" not in apis, apis)
        ck("纯关键词模式保持 kw", s["mode"] == "kw", s["mode"])
        ck("关键词意图标签正确", "关键词 · 搜图" in (s["intent"] or ""), s["intent"])

        # ---------- 5. 手选链接模式却只给了词：明确提示，不静默搜图 ----------
        reset("link", "风景 壁纸 4K")
        pg.click("#omniGo")
        pg.wait_for_timeout(400)
        apis = [c[0] for c in CALLS]
        s = snap()
        ck("手选链接模式给词时两条 API 都不调", not any(
            a in ("search/images", "crawl/start") for a in apis), apis)
        ck("给出明确提示而不是偷偷搜图", "需要一条网址" in (s["toast"] or ""), s["toast"])
        ck("模式没被偷偷切走", s["mode"] == "link", s["mode"])

        # ---------- 6. 没手点过模式按钮时（F-03 原行为）仍自动进链接抓取 ----------
        # reload 把 imageModeManual 复位成 false；v304 的「贴视频链接仍走图片抓取」
        # 就靠这条，F-19 不能把它改坏。
        pg.reload(wait_until="domcontentloaded")
        pg.wait_for_timeout(1800)
        pg.evaluate("() => { const t=document.querySelector('.tab[data-tab=\"image\"]'); if(t) t.click(); }")
        pg.wait_for_timeout(300)
        CALLS.clear()
        pg.fill("#omniInput", "weibo.com/u/1844960024?tabtype=newVideo")
        pg.wait_for_timeout(120)
        pg.click("#omniGo")
        pg.wait_for_timeout(900)
        apis = [c[0] for c in CALLS]
        s = snap()
        ck("默认未手选时裸域名自动进链接抓取",
           s["mode"] == "link" and "crawl/start" in apis and "search/images" not in apis,
           (apis, s["mode"]))
        ck("默认模式也不跳视频页（F-03）", s["tab"] == "image", s["tab"])

        b.close()

    print(f"结果：{len(PASS)} 通过 / {len(FAIL)} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
