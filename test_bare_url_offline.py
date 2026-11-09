# -*- coding: utf-8 -*-
"""V3.0.8 离线验收：裸域名判链接 / 手选模式优先 / URL 补协议。

不启服务、不开浏览器。判据部分是**从 web/shell.js 原样截取**交给 node 执行，
测的是真正会发布的那段代码，不是复制品；模式与兜底部分用源码结构断言。

Sam 2026-11-08 实测：图片页选「粘贴链接抓取」、粘
`weibo.com/u/1844960024?tabtype=newVideo`（不带协议头），
结果被当成关键词送进 Bing 搜图，模式按钮还被强制切回「关键词搜图」。

用法：_venv/Scripts/python.exe test_bare_url_offline.py
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SHELL = (ROOT / "web" / "shell.js").read_text(encoding="utf-8")
APP = (ROOT / "web" / "app.js").read_text(encoding="utf-8")

PASS: list[str] = []
FAIL: list[str] = []


def ck(name: str, ok: bool, detail: object = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(("  ok   " if ok else "  FAIL ") + name + ("" if ok else f"  <- {detail}"))


def main() -> int:
    # ---------- 1. 用 node 跑 shell.js 里真实的判据代码 ----------
    start = SHELL.find("const COMMON_TLD")
    end = SHELL.find("// 当前激活页签", start)
    ck("能从 shell.js 截出判据代码块", start > 0 and end > start,
       f"start={start} end={end}")
    snippet = SHELL[start:end] if start > 0 and end > start else ""

    js_cases = r"""
const LINK_CASES = [
  ['weibo.com/u/1844960024?tabtype=newVideo', true],       // Sam 实测的那条
  ['douyin.com/video/7635098784464778851', true],          // 别的站裸域名一样要认
  ['xiaohongshu.com/user/profile/6279da43', true],
  ['weibo.com', true],                                     // 纯主机名常见后缀
  ['https://weibo.com/u/1844960024', true],
  ['http://weibo.cn/u/1844960024', true],
  ['www.kuaishou.com/profile/3xnmi2xh2pxzpag', true],
  ['m.weibo.cn/status/1234567890', true],
  ['weibo.com:8080/u/1', true],
  ['风景 壁纸 4K', false],                                 // 关键词不能被误判
  ['雪山 航拍', false],
  ['cat', false],
  ['cat.dog', false],                                      // 非 TLD 后缀
  ['example.invalidtld', false],
  ['hello.world', false],
  ['', false],
];
const NORM_CASES = [
  ['weibo.com/u/1844960024?tabtype=newVideo', 'https://weibo.com/u/1844960024?tabtype=newVideo'],
  ['douyin.com/video/7635098784464778851', 'https://douyin.com/video/7635098784464778851'],
  ['www.weibo.com/u/1', 'https://www.weibo.com/u/1'],
  ['https://weibo.com/u/1', 'https://weibo.com/u/1'],      // 已带协议不动
  ['http://weibo.com/u/1', 'http://weibo.com/u/1'],
  ['风景 壁纸 4K', '风景 壁纸 4K'],                        // 关键词不动
  ['', ''],
];
let bad = [];
for (const [v, want] of LINK_CASES) {
  const got = isLink(v);
  if (got !== want) bad.push('isLink(' + JSON.stringify(v) + ')=' + got + ' want ' + want);
}
for (const [v, want] of NORM_CASES) {
  const got = normalizeUrlInput(v);
  if (got !== want) bad.push('normalizeUrlInput(' + JSON.stringify(v) + ')=' + JSON.stringify(got));
}
if (bad.length) { console.log('BAD\n' + bad.join('\n')); process.exit(1); }
console.log('NODE_OK ' + (LINK_CASES.length + NORM_CASES.length));
"""
    if snippet:
        r = subprocess.run(["node", "-e", snippet + js_cases],
                           capture_output=True, text=True, encoding="utf-8",
                           cwd=str(ROOT))
        out = (r.stdout or "").strip()
        detail = out + (" | " + (r.stderr or "").strip() if r.returncode else "")
        ck("裸域名 / 关键词判据 16 例 + URL 补全 7 例", r.returncode == 0 and "NODE_OK 23" in out,
           detail[:600])
    else:
        ck("裸域名 / 关键词判据 16 例 + URL 补全 7 例", False, "源码片段截取失败")

    # ---------- 2. 手选模式不再被 isLink 覆盖 ----------
    ck("startFrom 接收 modeHint", "function startFrom(v, c, modeHint)" in SHELL)
    ck("图片页按手选模式分派", "const mode = modeHint || (link ? 'link' : 'kw')" in SHELL)
    ck("存在 imageModeSelected()", "function imageModeSelected()" in SHELL)
    ck("只有手点过模式按钮才锁住", "let imageModeManual = false;" in SHELL
       and "setMode(b.dataset.mode, true)" in SHELL
       and "(tab === 'image' && imageModeManual) ? imageModeSelected() : null" in SHELL)
    ck("runOmni 传 modeHint", "startFrom(v, channelForTab(), modeHint)" in SHELL)
    ck("runOmni 先规范化输入", "const v = normalizeUrlInput(raw);" in SHELL)
    ck("手选链接模式却给词时明确提示，不静默搜图",
       "modeHint === 'link' && !link" in SHELL and "要按词搜图请切回「关键词搜图」" in SHELL)
    ck("旧的无条件覆盖已删除", "setMode(link ? 'link' : 'kw');" not in SHELL)
    ck("startFrom 内部也补协议", "v = normalizeUrlInput(v);" in SHELL)
    ck("首页手选通道比较时两边都规范化",
       "normalizeUrlInput(chanManualFor) === normalizeUrlInput(String(v).trim())" in SHELL)

    # ---------- 3. 页签通道冻结项不能被这次改动带坏 ----------
    ck("F-03 页签通道仍生效", "return tab === 'image' ? 'image' : (tab === 'video' ? 'video' : chan);" in SHELL)
    ck("只有首页按站点自动猜通道", "tab === 'home' && !chanManualOn(v)" in SHELL)
    ck("图片分支仍走图片页", "switchTab('image');" in SHELL)

    # ---------- 4. 独立抓取输入框的兜底 ----------
    ck("doCrawl 给裸域名补协议", "opt.url = 'https://' + opt.url" in APP)
    ck("doCrawl 仍拒绝非 URL 输入", "toast('链接需要以 http:// 或 https:// 开头', 'err')" in APP)

    print(f"结果：{len(PASS)} 通过 / {len(FAIL)} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
