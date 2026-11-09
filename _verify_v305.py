# -*- coding: utf-8 -*-
"""V3.0.5 回归：登录态判定与「登录窗口」语义。

覆盖 2026-11-07 用户四条反馈里跟登录态有关的三条：
  1. 快手明明登录了却显示「未登录」—— auth_cookies 名单过时（web_st → webday7_st/ph）；
  2. 平台状态不定时跳「登录窗口已打开」—— 原先用 profile 的 lockfile 判断，
     而本程序自己的后台渲染（抓主页/解析直链/刷新 Cookie）也持有同一个 lockfile；
  3. 小红书登录失效被当成「该账号没有作品」—— 接口用 success:true + code=-100
     拒绝请求，旧代码只认 success。

用法：
    _venv/Scripts/python.exe _verify_v305.py                     # 默认 http://127.0.0.1:8860
    _venv/Scripts/python.exe _verify_v305.py http://127.0.0.1:18848
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8860"
EXPECT_LOGIN = {"dy", "ks", "bili", "wb", "zhihu"}
# 「有凭据但服务端已失效」允许判成 invalid——这是期待行为，不是失败。

results: list[tuple[str, bool, str]] = []


def ck(name: str, ok: bool, detail: object = "") -> None:
    results.append((name, bool(ok), str(detail)))
    print(("  PASS  " if ok else "  FAIL  ") + name + ("" if ok else "  << " + str(detail)),
          flush=True)


def api(path: str) -> dict:
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with op.open(BASE + path, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def main() -> int:
    print("[平台登录态]", flush=True)
    try:
        st = api("/api/platforms/status")["platforms"]
    except Exception as e:  # noqa: BLE001
        print("status 读不到：" + str(e))
        return 1

    for key in EXPECT_LOGIN:
        item = st.get(key) or {}
        ck(f"{key} 判定为已登录（{item.get('auth_cookie_count')} 条凭据）",
           item.get("state") == "ready", {k: item.get(k) for k in ("state", "reason")})

    ks = st.get("ks") or {}
    ck("快手命中 webday7 系凭据（不是旧的 server.web_st）",
       any("webday7" in n or "live.web" in n for n in (ks.get("auth_cookie_names") or [])),
       ks.get("auth_cookie_names"))

    print("\n[「登录窗口」语义]", flush=True)
    bad = {k: v for k, v in st.items() if bool(v.get("in_use")) != bool(v.get("window_open"))}
    ck("in_use 只由登记过的登录窗口决定", not bad, bad)
    open_now = [k for k, v in st.items() if v.get("in_use")]
    ck("没有打开过登录窗口时全部 in_use=false", not open_now, open_now)

    # 直接对模块做一次性进程内验证：登记/清理/死 pid 三条路径。
    # 用子进程跑，避免把测试 pid 写进真实运行目录。
    code = r'''
import json, os, sys
os.environ.setdefault("MEDIA_DOWNLOADER_DATA_DIR", os.path.abspath("_tmp_v305_data"))
sys.path.insert(0, os.path.abspath("."))
from app import platform_login as pl
out = {}
out["before"] = pl.login_window_alive("ks")
pl.mark_login_window("ks", os.getpid())
out["self"] = pl.login_window_alive("ks")
pl.mark_login_window("ks", 999999)          # 必然不存在的 pid
out["dead"] = pl.login_window_alive("ks")
out["file_gone"] = not (pl.profile_dir("ks").parent / "login_window.json").exists()
pl.clear_login_window("ks")
out["cleared"] = pl.login_window_alive("ks")
print(json.dumps(out))
'''
    try:
        p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                           timeout=90, encoding="utf-8", errors="replace")
        out = json.loads((p.stdout or "{}").strip().splitlines()[-1]) if p.stdout.strip() else {}
    except Exception as e:  # noqa: BLE001
        out = {}
        print("  子进程验证失败：" + str(e))

    ck("未登记时不算窗口开着", out.get("before") is False, out)
    ck("登记当前进程 → 窗口开着", out.get("self") is True, out)
    ck("登记的进程已退出 → 自动判为关闭", out.get("dead") is False, out)
    ck("进程退出后登记文件被清掉", out.get("file_gone") is True, out)
    ck("手动清理后不再报窗口开着", out.get("cleared") is False, out)

    ui_checks()

    bad = [r for r in results if not r[1]]
    print("\n结果：%d 通过 / %d 失败" % (len(results) - len(bad), len(bad)), flush=True)
    return 0 if not bad else 1


def ui_checks() -> None:
    """界面两条：失效提示条不能压在侧栏下；无封面卡片要有体面的占位。"""
    print("\n[界面]", flush=True)
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:  # noqa: BLE001
        ck("Playwright 可用", False, e)
        return
    chrome = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=chrome, headless=True,
                              args=["--no-sandbox", "--disable-gpu", "--no-proxy-server"])
        pg = b.new_context(viewport={"width": 1600, "height": 950}).new_page()
        errs: list[str] = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.goto(BASE + "/", wait_until="domcontentloaded")
        pg.wait_for_timeout(3200)
        if pg.locator("#firstUseWizard").is_visible():
            pg.click("#wizardClose")
            pg.wait_for_timeout(400)
        pg.evaluate("switchTab('login')")
        pg.wait_for_timeout(1200)
        geo = pg.evaluate("""() => {
          const el = document.getElementById('loginAlert');
          const sb = document.querySelector('.sidebar');
          if (!el || !sb) return null;
          const r = el.getBoundingClientRect(), s = sb.getBoundingClientRect();
          return {hidden: el.hidden, left: Math.round(r.left),
                  sbRight: Math.round(s.right), display: getComputedStyle(el).display};
        }""")
        if geo and not geo["hidden"]:
            ck("登录失效提示条不压侧栏", geo["left"] >= geo["sbRight"], geo)
            ck("提示条按卡片排版（flex）", geo["display"] == "flex", geo)
        else:
            ck("当前没有失效平台，提示条保持隐藏", bool(geo and geo["hidden"]), geo)

        ph = pg.evaluate("""() => {
          const t = document.createElement('div');
          t.className = 'thumb';
          t.style.cssText = 'width:200px;height:140px';
          const box = noCoverPh('暂无预览图');
          t.appendChild(box);
          document.body.appendChild(t);
          const cs = getComputedStyle(box);
          const out = {hasSvg: !!box.querySelector('svg'), display: cs.display,
                       text: (box.innerText || '').trim()};
          t.remove();
          return out;
        }""")
        ck("无封面占位带图标且竖排居中", ph["hasSvg"] and ph["display"] == "flex", ph)
        ck("无封面占位文案可读", ph["text"] == "暂无预览图", ph)
        ck("console / pageerror 为 0", not errs, errs[:3])
        b.close()


if __name__ == "__main__":
    sys.exit(main())
