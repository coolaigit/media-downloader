# -*- coding: utf-8 -*-
"""校验打包后的 EXE 里，前端资源确实是本轮修好的版本。

EXE 是 onefile，web/* 被打进 CArchive 的 datas 段，直接按名字取出比对文本。
"""
from __future__ import annotations

import sys
from pathlib import Path

from PyInstaller.archive.readers import CArchiveReader

EXE = Path(sys.argv[1] if len(sys.argv) > 1 else
           r"E:\Project\media-downloader-main\_dist_ui\MediaDownloader_TEST.exe")

NEED = {
    "web/index.html": [
        'class="opt-row crawl-opts" id="imgCrawlOptions"',
        'id="imgCPageCount"', 'id="imgCMaxItems"', 'id="imgCDrill"',
        '>筛选设置<', '不重复下载同一张图',
        # V3.0.4：版本号 + 首页通道控件 + 四个工作区标记
        "<title>MediaDownloader 3.0.4</title>", "style.css?v=3.0.4-kernel",
        'id="homeChan"', 'class="home-meta"', 'id="homeIntent"',
        "workpane",
    ],
    "web/style.css": [
        ".crawl-opts", ".inline-opt", ".hstat-clickable",
        ".is-pressed", ".btn.primary:disabled",
        # V3.0.4：面板撑满 + 通道选中态 + 折叠说明 + 空态示例词
        "--panel-h", ".workpane > *", ".chan-seg", ".pane-note", ".empty-samples",
        # V3.0.5：失效提示条（有侧栏时让位）+ 无封面占位
        ".login-alert", "body.has-sidebar .login-alert",
    ],
    "web/shell.js": [
        "function activeGrid()", "chipHtml(", "STAT_ACTIONS",
        "不重复下载",
        # V3.0.4：通道自动识别 / 手动优先 / 高度同步 / 种子空态
        "syncPanelHeight", "VIDEO_HOSTS", "SINGLE_VIDEO_PAT",
        "chanManualFor", "function startFrom(", "seedEmptyHints", "PANE_SEEDS",
        # V3.0.8：裸域名识别 + 手选模式优先 + URL 补协议
        "COMMON_TLD", "BARE_URL_RE", "function isLink(", "function normalizeUrlInput(",
        "function imageModeSelected(", "const mode = modeHint || (link ? 'link' : 'kw')",
        "startFrom(v, channelForTab(), modeHint)",
        "let imageModeManual = false;", "setMode(b.dataset.mode, true)",
    ],
    "web/app.js": [
        "共 ${st.items.length} 个媒体", "if (typeof setOmniBusy === 'function')",
        "const sumEl = $(E('crawlOptSummary'))",
        # V3.0.4：图片模式切换抽成函数（模式条已删，只留顶部 omniSeg）
        "function applyImageMode(", "function restoreVideoEmpty(",
        # V3.0.5：无封面占位（图标 + 说明）
        "function noCoverPh(",
        # V3.0.7：直链下载按作品标题命名
        "function crawlVideoFilename(",
        "filename: crawlVideoFilename(i)",
        # V3.0.8：独立抓取输入框同样补协议
        "opt.url = 'https://' + opt.url",
    ],
}

NEGATIVE = {
    "web/shell.js": [
        "setMode(link ? 'link' : 'kw');",   # V3.0.8 删掉：会覆盖用户手选的方式
    ],
    "web/index.html": [
        '<details class="filters" id="imgCrawlOptions">',
        'id="imageModeSwitch"',       # V3.0.4 删掉的重复模式条
        "app.js?v=3.0.3-kernel",      # 缓存穿透串必须跟着版本走
    ],
}


def main() -> int:
    r = CArchiveReader(str(EXE))
    # Windows 下 datas 的条目名是反斜杠（web\\index.html），统一成 / 再比对
    names = {n.replace("\\", "/"): n for n in r.toc}
    print("EXE", EXE.name, EXE.stat().st_size, "B", "entries", len(names))
    bad = []
    for name, needles in NEED.items():
        real = names.get(name)
        if not real:
            bad.append(name + ": 不在包里")
            print("  MISS", name)
            continue
        raw = r.extract(real)
        if isinstance(raw, (bytes, bytearray)):
            text = raw.decode("utf-8", "replace")
        else:
            text = str(raw)
        miss = [n for n in needles if n not in text]
        print(f"  {name}: {len(needles) - len(miss)}/{len(needles)}",
              "MISSING " + " | ".join(miss) if miss else "OK")
        bad.extend(f"{name}:{m}" for m in miss)
        for n in NEGATIVE.get(name, []):
            if n in text:
                print(f"  {name}: 仍残留旧结构 {n}")
                bad.append(f"{name}:残留 {n}")
    if bad:
        print("FAIL", bad)
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
