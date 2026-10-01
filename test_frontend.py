# -*- coding: utf-8 -*-
"""静态一致性检查：前端 JS 引用的 DOM id 是否都存在于 HTML 中。

这类不一致不会造成语法错误，但会在运行时导致按钮失灵、数据填不进去。
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"

html = (WEB / "index.html").read_text(encoding="utf-8")
js = (WEB / "app.js").read_text(encoding="utf-8")
css = (WEB / "style.css").read_text(encoding="utf-8")

html_ids = set(re.findall(r'\bid="([^"]+)"', html))
# JS 里通过 $('xxx') / getElementById('xxx') / dataset 引用的 id
js_ids = set(re.findall(r"\$\(\s*'([^']+)'\s*\)", js))
js_ids |= set(re.findall(r'getElementById\(\s*["\']([^"\']+)["\']\s*\)', js))
# 模板字符串里的 id="xxx"
js_ids |= set(re.findall(r'\bid="([^"]+)"', js))

print(f"HTML 中定义的 id: {len(html_ids)}")
print(f"JS 中引用的 id:   {len(js_ids)}")

missing = sorted(js_ids - html_ids)
# JS 内部动态创建的元素（如任务面板）不算缺失
print("\n--- JS 引用但 HTML 中不存在的 id ---")
if missing:
    for m in missing:
        # 找出引用位置
        ctx = ""
        for line in js.splitlines():
            if f"'{m}'" in line or f'"{m}"' in line:
                ctx = line.strip()[:100]
                break
        print(f"  [MISSING] {m:26} <- {ctx}")
else:
    print("  （无）")

unused = sorted(html_ids - js_ids)
print("\n--- HTML 定义但 JS 未引用的 id（可能是纯展示或遗漏绑定）---")
for u in unused:
    print(f"  {u}")

# 检查 JS 里绑定的事件处理器对应的元素是否存在
print("\n--- 事件绑定检查 ---")
binds = re.findall(r"\$\('([^']+)'\)\.(?:onclick|onchange|addEventListener)", js)
bad_binds = [b for b in set(binds) if b not in html_ids]
if bad_binds:
    for b in sorted(bad_binds):
        print(f"  [FAIL] 绑定到不存在的元素: {b}")
else:
    print(f"  OK  {len(set(binds))} 处事件绑定的目标元素均存在")

# 检查 CSS 里引用的 class 是否在 HTML/JS 中出现
print("\n--- 关键 class 检查 ---")
key_classes = ["card", "grid", "tab", "panel", "task", "toast", "vitem", "vcard", "badge", "thumb"]
for c in key_classes:
    in_css = f".{c}" in css
    in_html = f'class="{c}' in html or f'"{c} ' in html or f' {c}"' in html
    in_js = f"'{c}" in js or f'"{c}' in js or f"class {c}" in js
    status = "OK" if in_css and (in_html or in_js) else "CHECK"
    print(f"  [{status}] .{c:10} css={in_css} html={in_html} js={in_js}")

print("\n--- 汇总 ---")
print(f"缺失 id: {len(missing)} | 绑定错误: {len(bad_binds)}")
