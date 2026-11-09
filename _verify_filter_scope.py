# -*- coding: utf-8 -*-
"""真实浏览器验收：筛选栏按「当前页 + 当前方式」显示对应控件，不串台。"""
from __future__ import annotations

import json
import sys
from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8852"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
errs: list[str] = []
checks: list[tuple[str, bool, str]] = []


def ck(name, cond, extra=""):
    checks.append((name, bool(cond), str(extra)))
    print(("  ok   " if cond else "  FAIL ") + name + (" " + str(extra) if not cond else ""),
          flush=True)


def state(pg):
    return pg.evaluate("""() => ({
      tab: (document.querySelector('.tab.active') || {}).dataset?.tab || '',
      label: (document.getElementById('filterLabel') || {}).textContent || '',
      count: (document.getElementById('filterCount') || {}).textContent || '',
      title: (document.getElementById('drawerTitle') || {}).textContent || '',
      sub: (document.getElementById('drawerSub') || {}).textContent || '',
      chips: (document.getElementById('activeChips') || {}).textContent || '',
      drawerIds: Array.from((document.getElementById('drawerBody') || {}).children || [])
                       .map(c => c.id),
      drawerLabels: Array.from(document.querySelectorAll('#drawerBody label'))
                         .map(l => l.textContent.trim().replace(/\\s+/g, ' ')).join(' | '),
      crawlOptsVisible: (() => {
        const e = document.getElementById('imgCrawlOptions');
        if (!e) return null;
        const r = e.getBoundingClientRect();
        return r.width > 0 && r.height > 0 && getComputedStyle(e).display !== 'none';
      })(),
      hiddenVideoInDrawer: (() => {
        const b = document.getElementById('videoSearchFilterBox');
        return b ? b.hidden : null;
      })(),
    })""")


def open_drawer(pg):
    # 顶部按钮在视频页随 #panel-image 一起隐藏，用 DOM click 直接触发同一处理器
    pg.evaluate("document.getElementById('btnFilterDrawer').click()")
    pg.wait_for_timeout(350)


def close_drawer(pg):
    pg.evaluate("document.getElementById('drawerDone').click()")
    pg.wait_for_timeout(250)


def main() -> int:
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=CHROME, headless=True,
                              args=["--no-sandbox", "--disable-gpu",
                                    "--no-proxy-server"])
        pg = b.new_page(viewport={"width": 1600, "height": 950})
        pg.on("pageerror", lambda e: errs.append("pageerror: " + str(e)[:200]))
        pg.on("console", lambda m: errs.append("console: " + m.text[:200])
              if m.type == "error" else None)
        pg.goto(BASE + "/", wait_until="domcontentloaded")
        pg.wait_for_timeout(2000)
        pg.evaluate("switchTab('image')")
        pg.wait_for_timeout(500)

        s = state(pg)
        print("image:kw", json.dumps(s, ensure_ascii=False), flush=True)
        ck("图片/关键词：按钮写「图片筛选」", s["label"] == "筛选设置", s)
        ck("图片/关键词：计数为图片筛选控件数", s["count"] == "15", s)
        ck("图片/关键词：chips 不含视频选项",
           "隐藏已下载" not in s["chips"] and "时长" not in s["chips"], s)
        open_drawer(pg)
        s1 = state(pg)
        ck("图片/关键词：抽屉搬 filterBox", s1["drawerIds"] == ["filterBox"], s1)
        ck("图片/关键词：抽屉标题是图片筛选", s1["title"] == "图片筛选设置", s1)
        for word in ("安全搜索", "数量上限", "长宽比", "清晰度", "文件格式"):
            ck(f"图片/关键词：抽屉含「{word}」", word in s1["drawerLabels"], s1["drawerLabels"][:200])
        for word in ("抓取模式", "抓取页数", "详情页下钻", "允许浏览器渲染"):
            ck(f"图片/关键词：抽屉不含「{word}」", word not in s1["drawerLabels"], s1["drawerLabels"][:200])
        close_drawer(pg)

        pg.click('#omniSeg button[data-mode="link"]')
        pg.wait_for_timeout(450)
        s = state(pg)
        print("image:link", json.dumps(s, ensure_ascii=False), flush=True)
        # 2026-11-07 用户反馈：粘贴链接抓取不经过搜索引擎，
        # 安全搜索 / 长宽比 / 颜色这些参数一项都不生效，入口不该出现。
        hidden = pg.evaluate("() => { const b=document.getElementById('btnFilterDrawer');"
                             " return !b || b.hidden || getComputedStyle(b).display==='none'; }")
        ck("图片/链接：筛选按钮隐藏", hidden is True, s)
        ck("图片/链接：chips 为空（没有常驻开关）", not (s["chips"] or "").strip(), s)
        ck("图片/链接：抓取选项回到页面上可见",
           s["crawlOptsVisible"] is True, s)
        open_drawer(pg)
        s1 = state(pg)
        ck("图片/链接：筛选抽屉不会被拉起", s1["drawerIds"] == [], s1)
        close_drawer(pg)

        pg.click('.sidebar .nav-item[data-tab="video"]')
        pg.wait_for_timeout(450)
        # 顶部筛选栏只挂在图片页；视频页用 DOM click 验证 scope() 的另外两个分支
        pg.click('#videoModeSwitch .mode-btn[data-mode="kw"]')
        pg.wait_for_timeout(450)
        s = state(pg)
        print("video:kw", json.dumps(s, ensure_ascii=False), flush=True)
        ck("视频/关键词：按钮写「视频筛选」", s["label"] == "视频筛选", s)
        ck("视频/关键词：抽屉标题是视频结果筛选",
           s["title"] == "视频结果筛选", s)
        ck("视频/关键词：chips 不含图片选项",
           "不重复下载" not in s["chips"] and "≥" not in s["chips"], s)
        open_drawer(pg)
        s1 = state(pg)
        ck("视频/关键词：抽屉搬 videoSearchFilterBox",
           s1["drawerIds"] == ["videoSearchFilterBox"], s1)
        ck("视频/关键词：抽屉里该面板不再 hidden",
           s1["hiddenVideoInDrawer"] is False, s1)
        close_drawer(pg)
        s2 = state(pg)
        ck("关闭抽屉后视频筛选恢复 hidden",
           s2["hiddenVideoInDrawer"] is True, s2)

        pg.click('#videoModeSwitch .mode-btn[data-mode="link"]')
        pg.wait_for_timeout(450)
        s = state(pg)
        print("video:link", json.dumps(s, ensure_ascii=False), flush=True)
        ck("视频/链接：按钮写「视频筛选」", s["label"] == "视频筛选", s)
        open_drawer(pg)
        s1 = state(pg)
        ck("视频/链接：抽屉搬 videoSearchFilterBox（不放抓取选项）",
           s1["drawerIds"] == ["videoSearchFilterBox"], s1)
        ck("视频/链接：抽屉不含抓取选项",
           "抓取模式" not in s1["drawerLabels"], s1["drawerLabels"][:200])
        close_drawer(pg)

        pg.click('.sidebar .nav-item[data-tab="image"]')
        pg.wait_for_timeout(450)
        s = state(pg)
        print("back-to-image", json.dumps(s, ensure_ascii=False), flush=True)
        ck("切回图片页后标签恢复筛选设置", s["label"] == "筛选设置", s)
        ck("切回图片页后 chips 不含视频选项",
           "隐藏已下载" not in s["chips"], s)

        pg.screenshot(path="_shot_filter_scope.png", full_page=False)
        b.close()

    print("ERRS", json.dumps(errs[:8], ensure_ascii=False), flush=True)
    ck("console / pageerror 为 0", not errs, errs[:3])
    bad = [n for n, ok, _ in checks if not ok]
    print("RESULT", "ALL PASS" if not bad else "FAILED " + str(bad), flush=True)
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
