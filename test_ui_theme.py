# -*- coding: utf-8 -*-
"""UI 配色系统静态一致性检查（离线，无网络/无服务依赖）。

覆盖：4 套基础外观 × 8 种强调色在 CSS / app.js / index.html / 后端白名单
四处必须一致；旧配置 key 必须继续被接受；语义 Token 与骨架组件必须存在。
这类不一致不会报语法错，但会让用户「点了色板却没生效」或「保存设置 422」。
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MODES = ["obsidian", "slate", "ivory", "midnight"]
ACCENTS = ["blue", "cyan", "violet", "green", "orange", "rose", "indigo", "teal"]
OLD_MODES = ["gray", "dark", "black", "light"]
OLD_ACCENTS = ["blue", "cyan", "violet", "green", "orange", "rose"]

css = (ROOT / "web/style.css").read_text(encoding="utf-8")
js = (ROOT / "web/app.js").read_text(encoding="utf-8")
html = (ROOT / "web/index.html").read_text(encoding="utf-8")
main = (ROOT / "app/main.py").read_text(encoding="utf-8")

results = []


def ck(name, cond, extra=""):
    results.append((name, bool(cond), extra))


# 1. 4 套主题块
for m in MODES:
    ck(f"CSS 主题块 {m}", f'html[data-mode="{m}"]' in css)

# 2. 8 色在 JS 数据表里
for a in ACCENTS:
    ck(f"JS 强调色 {a}", re.search(rf"^\s*{a}:\s*\{{", js, re.M) is not None)

# 3. JS 基础外观集合 + 旧 key 别名
for m in MODES:
    ck(f"JS BASE_THEMES 含 {m}", m in js.split("BASE_THEMES = new Set(")[1].split(")")[0])
for old in OLD_MODES:
    ck(f"JS 旧 key 映射 {old}", f"{old}:" in js.split("BASE_ALIAS = {")[1].split("}")[0])

# 4. 设置页控件数量
ck("HTML 4 张基础外观卡", html.count('class="base-theme-card"') == 4,
   str(html.count('class="base-theme-card"')))
ck("HTML 8 个强调色板", html.count('class="theme-swatch"') == 8,
   str(html.count('class="theme-swatch"')))
ui_sel = re.search(r'<select id="sUiMode".*?</select>', html, re.S)
ac_sel = re.search(r'<select id="sAccent".*?</select>', html, re.S)
ck("sUiMode 4 个选项", ui_sel and len(re.findall(r"<option", ui_sel.group(0))) == 4)
ck("sAccent 8 个选项", ac_sel and len(re.findall(r"<option", ac_sel.group(0))) == 8)

# 5. 后端白名单同时包含新旧值
mode_pat = re.search(r'ui_mode:.*?pattern="\^\((.*?)\)\$"', main)
ac_pat = re.search(r'ui_accent:.*?pattern="\^\((.*?)\)\$"', main)
mode_vals = mode_pat.group(1).split("|") if mode_pat else []
ac_vals = ac_pat.group(1).split("|") if ac_pat else []
for m in MODES + OLD_MODES:
    ck(f"后端 ui_mode 白名单含 {m}", m in mode_vals)
for a in ACCENTS:
    ck(f"后端 ui_accent 白名单含 {a}", a in ac_vals)

# 6. 语义 Token / 高级效果 / 无障碍
for token in ["--surface-1", "--surface-2", "--surface-3", "--shadow-3",
              "--motion-enter", "--motion-exit"]:
    ck(f"语义 Token {token}", token in css)
ck("骨架微光组件", ".skeleton-card" in css and "@keyframes skShimmer" in css)
ck("doSearch 挂载骨架", "skeletonCards(8)" in js)
ck("焦点可见", ":focus-visible" in css)
ck("减少动态效果", "prefers-reduced-motion" in css)
ck("主按钮文字随亮度反转", "--accent-on" in js and "--accent-on" in css)

print(f"检查项 {len(results)} 个")
failed = [(n, e) for n, ok, e in results if not ok]
for n, ok, e in results:
    if not ok:
        print(f"  [FAIL] {n} {e}")
if failed:
    print(f"FAILED: {len(failed)}/{len(results)}")
    sys.exit(1)
print(f"ALL PASS: {len(results)}/{len(results)}")
