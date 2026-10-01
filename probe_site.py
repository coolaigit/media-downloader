# -*- coding: utf-8 -*-
"""单站点抓取诊断：投喂一个 URL，输出分层诊断报告与结论。

用途：用户点对点发来「这个站抓不到」时，一条命令跑出全部证据，
不用每次重新摸索。报告同时给出「哪一层出问题 + 为什么 + 下一步」。

用法：
    python probe_site.py <url>
    python probe_site.py <url> --pages 3 --drill --items 10
    python probe_site.py <url> --no-render          # 跳过浏览器渲染（只看静态层）

产出：
    终端输出 + _site_report.txt（可读报告） + _site_report.json（结构化数据）
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import requests  # noqa: E402

from app.batch_crawler import (  # noqa: E402
    FetchResult,
    _count_meaningful_media,
    _visible_text_len,
    crawl_list,
    detect_pagination,
    fetch_page,
)
from app.config import HEADERS, TIMEOUT  # noqa: E402
from app.extractor import is_decoration  # noqa: E402
from app.renderer import renderer_status  # noqa: E402

REPORT_TXT = ROOT / "_site_report.txt"
REPORT_JSON = ROOT / "_site_report.json"

lines: list[str] = []


def say(s: str = ""):
    print(s, flush=True)
    lines.append(s)


# ---------- 第 0 层：网络可达性 ----------

def probe_network(url: str, timeout: int = TIMEOUT) -> dict:
    """裸 HTTP 请求一次，看「不借助任何特殊手段，这个站能不能连上」。

    这一层单独做，是为了把「网络不通」和「页面抓不到」区分开 ——
    两者表现都是「没结果」，但处置方式完全不同。
    """
    out = {"ok": False, "status": None, "ctype": "", "bytes": 0,
           "visible_text": 0, "media": 0, "final_url": "", "error": "",
           "elapsed": 0.0, "needs_render": None}
    t0 = time.time()
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout, allow_redirects=True)
        out["status"] = r.status_code
        out["final_url"] = r.url or url
        ct = (r.headers.get("Content-Type") or "").lower()
        out["ctype"] = ct.split(";")[0].strip()
        if r.status_code < 400 and ("html" in ct or "xml" in ct or not ct):
            from app.page_scanner import decode_html
            html = decode_html(r, 8 * 1024 * 1024)
            out["ok"] = True
            out["bytes"] = len(html)
            out["visible_text"] = _visible_text_len(html)
            out["media"] = _count_meaningful_media(html, out["final_url"])
            from app.batch_crawler import _needs_render
            out["needs_render"] = _needs_render(
                html, out["media"], len(html), out["visible_text"])
        else:
            out["error"] = f"HTTP {r.status_code}" if r.status_code >= 400 else \
                f"非网页内容（{out['ctype']}）"
    except Exception as e:  # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {str(e)[:200]}"
    out["elapsed"] = round(time.time() - t0, 1)
    return out


# ---------- 第 1 层：渲染能力 ----------

def probe_render(url: str) -> tuple[dict, str, list[str]]:
    """单独跑一次浏览器渲染，用于区分「静态层拿不到」和「渲染也拿不到」。

    返回 (统计信息, 渲染后的 HTML, 拦截到的 JSON 响应体) ——
    后两者会被交给第 2 层复用，避免同一个页面被请求两次（会触发风控）。
    """
    from app.renderer import render_page

    out = {"ok": False, "bytes": 0, "visible_text": 0, "title": "",
           "captured_json": 0, "error": "", "elapsed": 0.0, "engine": "", "final_url": ""}
    rr = render_page(url, settle_ms=3500, scroll_rounds=3)
    out["ok"] = rr.ok
    out["bytes"] = len(rr.html or "")
    out["visible_text"] = _visible_text_len(rr.html or "")
    out["title"] = (rr.title or "")[:120]
    out["captured_json"] = len(rr.captured or [])
    out["engine"] = rr.engine
    out["final_url"] = rr.final_url
    out["error"] = rr.error
    out["elapsed"] = round(rr.elapsed, 1)
    return out, (rr.html or ""), [c.body for c in (rr.captured or []) if c.body]


# ---------- 结论生成 ----------

# 人机验证页 / 等待页的通用特征（中英常见文案）
_CHALLENGE_MARKERS = (
    "请稍候", "验证中", "安全验证", "人机验证", "正在检查",
    "just a moment", "checking your browser", "verify you are human",
    "attention required", "ddos protection", "cf-browser-verification",
    "enable javascript and cookies to continue", "one more step",
)


def _looks_like_challenge(title: str, html: str = "") -> bool:
    """页面是不是「人机验证 / 等待页」而不是真实内容。

    为什么必须单独判：这类页面渲染**是成功的**（有 HTML、有标题），
    若只看「渲染成功」就下结论，会得出「渲染可用、无需处理」的错误归因 ——
    实测 pixabay 被限流时渲染出的标题是「请稍候…」。
    """
    hay = ((title or "") + " " + (html or "")[:8000]).lower()
    return any(m in hay for m in _CHALLENGE_MARKERS)


def verdict(net: dict, render: dict, crawl: dict, kwargs: dict) -> list[str]:
    """把各层现象翻译成「问题在哪 + 为什么 + 下一步」。"""
    v: list[str] = []
    summary = crawl.get("summary") or {}
    pages = crawl.get("pages") or []
    stats = crawl.get("stats") or {}
    total = summary.get("total", 0)
    method = pages[0].get("method", "") if pages else ""
    render_vt = render.get("visible_text", 0) or 0
    page_vt = (pages[0].get("visible_text") if pages else 0) or 0
    seen_media = (pages[0].get("media") if pages else 0) or 0
    vt = render_vt if render.get("ok") else page_vt
    challenge = _looks_like_challenge(render.get("title") or "")

    # 1) 连不上
    if not net["ok"] and not render["ok"]:
        v.append("【站点不可达】静态请求与浏览器渲染双双失败。")
        if "SSL" in net["error"] or "SSL" in render["error"]:
            v.append("  → TLS/证书握手失败。多为网络出口、代理、或该站对该 IP 的限制，"
                     "不是抓取逻辑问题。先确认浏览器能否人工打开。")
        elif "Proxy" in net["error"] or "502" in net["error"]:
            v.append("  → 代理层报错（本机走代理时常见）。属于网络环境问题。")
        elif "Timeout" in net["error"]:
            v.append("  → 连接超时。站点可能被墙，或需要更长超时。")
        else:
            v.append(f"  → 静态层: {net['error']}；渲染层: {render['error']}")
        return v

    # 2) 渲染出来的是人机验证页 / 空壳 —— 必须排在「静态被拒但渲染可用」之前，
    #    否则会把「渲染到验证页」误判成「渲染可用、无需处理」。
    if render.get("ok") and (challenge or (vt < 100 and seen_media <= 1 and total <= 1)):
        title = (render.get("title") or "").strip()
        v.append("【疑似人机验证 / 反爬拦截页】")
        if title:
            v.append(f"  → 渲染出来的页面标题是「{title}」，不是目标内容 —— "
                     "这是验证等待页的典型特征。")
        else:
            v.append(f"  → 渲染后页面可见正文仅 {vt} 字符，没有真实内容。")
        v.append("  → 最常见的原因：短时间内抓得太频繁被限流（同一个页面对它请求多次也会）。")
        v.append("  → 下一步：①等几分钟再试 ②确认浏览器人工打开是否正常 "
                 "③需要登录则在设置里配 Cookies。工具不做验证码绕过。")
        if total:
            v.append(f"  → 当前抓到的 {total} 条是站点 UI 资源，不是内容。")
        return v

    # 3) 静态被拒但渲染拿到了正常内容
    if not net["ok"] and render.get("ok"):
        v.append("【静态被拒、渲染可用】纯 HTTP 被站点拒绝（反爬/UA 校验/JS 校验），"
                 "但浏览器渲染拿到了正常内容。工具会自动走渲染，无需额外处理。")

    # 4) 结果全是装饰资源
    if total and all(is_decoration(i.get("url", "")) for i in (crawl.get("items") or [])):
        v.append("【结果全是站点装饰资源】抓到的都是图标/横幅，没有真实内容媒体。")
        v.append("  → 通常是拦截页或空壳页；也可能是该页真的没有内容媒体。")
        return v

    # 5) 页面正常但没抓到东西
    if total == 0:
        links = sum(p.get("links", 0) for p in pages)
        if links > 0:
            v.append(f"【列表页无媒体，但找到 {links} 个详情链接】")
            v.append("  → 该站媒体在详情页里。开启「详情页下钻」即可。")
        else:
            v.append("【页面正常但没有可识别的媒体】")
            v.append("  → 可能是：媒体地址由 JS 运行时拼装、藏在 WebSocket/加密数据里，"
                     "或页面本身不含媒体。")
            v.append("  → 下一步：人工打开页面确认媒体是否真的存在；"
                     "若有，把该页面连同「你看到的媒体在哪」一起发我。")

    # 5.5) 列表页有媒体，但一条视频都没有，而详情链接很多
    #      → 视频在详情页播放器里，必须下钻。
    #      实测 avjoy.me：列表页 63 条全是缩略图（`/tmb/` 目录）+ 120 个详情链接，
    #      真 mp4 全在各详情页的 <video><source> 里。此时只报「抓取正常」会误导用户
    #      ——用户以为抓到了，其实一个能播的都没有。
    if (total > 0 and summary.get("video", 0) == 0
            and summary.get("stream", 0) == 0):
        links = sum(p.get("links", 0) for p in pages)
        if links >= 5 and not kwargs.get("drill"):
            v.append(f"【列表页有 {total} 个媒体，但没有任何视频；找到 {links} 个详情链接】")
            v.append("  → 这是个「列表页只放缩略图、视频在详情页播放器里」的站点。")
            v.append("  → 下一步：加 --drill 再跑一次，或界面上把「详情页下钻」"
                     "设为「智能判断 / 总是下钻」。")

    # 6) 正常
    if total > 0:
        v.append(f"【抓取正常】{total} 个媒体"
                 f"（图片 {summary.get('image', 0)}、视频 {summary.get('video', 0)}、"
                 f"流媒体 {summary.get('stream', 0)}）。")
        if method:
            v.append(f"  → 走的是：{method}"
                     + ("（浏览器渲染）" if method.startswith("render") else "（静态请求，快）"))
        if stats.get("variants_collapsed"):
            v.append(f"  → 合并了 {stats['variants_collapsed']} 个重复尺寸版本。")

    # 7) 下钻情况
    dp = crawl.get("detail_pages") or {}
    if dp.get("total"):
        v.append(f"【下钻】详情页 {dp.get('ok')}/{dp.get('total')} 成功"
                 + (f"，失败 {dp['failed']} 个" if dp.get("failed") else "") + "。")
    elif kwargs.get("drill"):
        v.append("【下钻】没有提取到详情页链接，无法下钻。")

    # 8) 分页
    if pages:
        pg = pages[-1].get("_pagination")
        if pg:
            if pg.get("mode"):
                v.append(f"【分页】方式={pg['mode']} 参数={pg.get('param')} "
                         f"步长={pg.get('step')} 识别页码={sorted((pg.get('page_urls') or {}).keys())[:10]}")
            else:
                v.append("【分页】未识别到任何分页信息 → 该站可能是无限滚动加载，"
                         "只能抓到当前已渲染的部分。")

    return v


# ---------- 主流程 ----------

def main() -> None:
    ap = argparse.ArgumentParser(description="单站点抓取诊断")
    ap.add_argument("url")
    ap.add_argument("--pages", type=int, default=1)
    ap.add_argument("--items", type=int, default=8, help="下钻详情页数量上限")
    ap.add_argument("--drill", action="store_true", help="强制下钻详情页")
    ap.add_argument("--no-render", action="store_true", help="跳过浏览器渲染")
    ap.add_argument("--skip-crawl", action="store_true", help="只做分层诊断，不跑完整抓取")
    args = ap.parse_args()

    url = args.url.strip()
    data: dict = {"url": url, "started": time.strftime("%Y-%m-%d %H:%M:%S"),
                  "renderer": renderer_status()}

    say("=" * 66)
    say(f"站点抓取诊断: {url}")
    say("=" * 66)
    host = urlparse(url).hostname or url
    say(f"域名: {host}")
    rs = data["renderer"]
    say(f"渲染能力: {'可用' if rs.get('available') else '不可用'}"
        + (f" (内核={rs.get('system_browser') or 'bundled'})" if rs.get("available") else ""))

    # ---- 第 0 层：网络 ----
    say()
    say("--- 第 0 层：静态 HTTP ---")
    net = probe_network(url)
    data["network"] = net
    if net["ok"]:
        say(f"  可达  HTTP {net['status']}  {net['bytes']} 字节  "
            f"可见文本 {net['visible_text']}  媒体 {net['media']}  用时 {net['elapsed']}s")
        say(f"  是否判定需要渲染: {net['needs_render']}")
    else:
        say(f"  不可达  {net['error']}  用时 {net['elapsed']}s")

    # ---- 第 1 层：渲染（仅在需要且允许时跑）----
    render: dict = {"ok": False, "error": "未执行（不需要或已禁用）"}
    render_html = ""
    render_json: list[str] = []
    # 第 1 层只在「静态层完全失败」时执行，目的是把「网络不通」和「渲染可用」区分开。
    # 静态层成功的情况交给第 2 层的完整抓取（crawl_list 内部会自己决定是否渲染）——
    # 否则同一个页面要渲染两次，像 example.com 这种会白等将近 50 秒。
    need_render = (not args.no_render) and (not net["ok"])
    if args.no_render:
        say()
        say("--- 第 1 层：浏览器渲染（已按参数跳过）---")
    elif not rs.get("available"):
        say()
        say("--- 第 1 层：浏览器渲染 ---")
        say("  渲染能力不可用，跳过。")
    elif not need_render:
        say()
        say("--- 第 1 层：浏览器渲染（静态层已够用，无需渲染）---")
    else:
        say()
        say("--- 第 1 层：浏览器渲染 ---")
        render, render_html, render_json = probe_render(url)
        data["render"] = render
        if render["ok"]:
            say(f"  成功  渲染后 {render['bytes']} 字节  可见文本 {render['visible_text']}  "
                f"拦截 JSON {render['captured_json']} 个  用时 {render['elapsed']}s")
            say(f"  标题: {render['title']}")
            if net["ok"] and net["bytes"]:
                say(f"  对比: 静态 {net['bytes']} 字节 → 渲染后 {render['bytes']} 字节"
                    f"（{render['bytes'] / max(1, net['bytes']):.1f}×）")
        else:
            say(f"  失败  {render['error']}")
    if "render" not in data:
        data["render"] = render

    # ---- 第 2 层：完整抓取 ----
    crawl: dict = {}
    # 第 1 层已经渲染好的页面交给第 2 层复用，不要再请求一次。
    # 实测必要性：pixabay 第一次渲染拿到 1MB 的正常页面，紧接着第二次请求
    # 就返回 28KB 的拦截页 —— 重复请求会触发站点风控，不只是浪费时间。
    prefetched: dict = {}
    if render.get("ok") and render_html:
        prefetched[url] = FetchResult(
            url=url,
            final_url=render.get("final_url") or url,
            html=render_html,
            json_texts=render_json,
            method="render",
            ok=True,
            visible_text_len=render.get("visible_text", 0),
        )
        say()
        say(f"  → 第 2 层将复用上面这次渲染结果（{len(render_html)} 字节），不再重复请求")
    if not args.skip_crawl:
        say()
        say("--- 第 2 层：完整抓取（列表页" +
            ("+ 详情页下钻" if args.drill else "") + "）---")
        t0 = time.time()
        try:
            crawl = crawl_list(
                url,
                max_pages=max(1, args.pages),
                max_items=args.items if args.drill else 0,
                drill_down=bool(args.drill),
                drill_mode="always" if args.drill else "auto",
                allow_render=not args.no_render,
                prefetched=prefetched,
            )
            data["crawl"] = {
                "summary": crawl.get("summary"), "stats": crawl.get("stats"),
                "pages": crawl.get("pages"), "detail_pages": crawl.get("detail_pages"),
                "warnings": crawl.get("warnings"),
            }
            say(f"  结果 {len(crawl.get('items') or [])} 个媒体  用时 {time.time() - t0:.1f}s")
            for p in (crawl.get("pages") or []):
                say(f"    第 {p['page']} 页: 方式={p['method']} 大小={p['html_bytes']} "
                    f"媒体={p['media']} 详情链接={p['links']} 可见文本={p['visible_text']} "
                    f"用时={p['elapsed']}s")
            for w in (crawl.get("warnings") or []):
                say(f"    ! {w}")
            items = crawl.get("items") or []
            if items:
                say("  前几条结果:")
                for it in items[:8]:
                    say(f"    [{it.get('type')}] {(it.get('url') or '')[:110]}")
            # 分页探测：优先复用已抓到的页面，避免对同一 URL 再发一次请求
            try:
                fr = prefetched.get(url)
                if fr is None:
                    fr = fetch_page(url, allow_render=not args.no_render)
                pg = detect_pagination(fr.html, fr.final_url or url, 1)
                if crawl.get("pages"):
                    crawl["pages"][-1]["_pagination"] = pg
                data["pagination"] = pg
            except Exception:
                pass
        except Exception as e:  # noqa: BLE001
            crawl = {"warnings": [f"抓取异常: {type(e).__name__}: {e}"]}
            say(f"  异常: {type(e).__name__}: {e}")

    # ---- 结论 ----
    say()
    say("=" * 66)
    say("诊断结论")
    say("=" * 66)
    for line in verdict(net, render, crawl, vars(args)):
        say(line)

    say()
    say(f"报告已写入 {REPORT_TXT.name} / {REPORT_JSON.name}")
    REPORT_TXT.write_text("\n".join(lines), encoding="utf-8")
    REPORT_JSON.write_text(json.dumps(data, ensure_ascii=False, indent=2,
                                      default=str), encoding="utf-8")


if __name__ == "__main__":
    main()
