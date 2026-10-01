# -*- coding: utf-8 -*-
"""浏览器渲染层：用 playwright 驱动本机 Chrome/Edge 渲染 JS 页面并拦截网络响应。

为什么需要：实测很多站点（阿里巴巴搜索页、Google 图片搜索）返回的 HTML 只是
10KB 左右的 JS 空壳，真实数据由前端调 API 拿 JSON 后渲染。纯 HTTP 抓取对这些站点
完全无效，必须执行 JavaScript。

实测结论（决定本模块设计）：
- playwright 驱动本机已装的 Chrome 可行，启动仅 0.6s，无需下载约 300MB 内核
- 同一页面：静态 HTML 10KB / 渲染后 1.04MB，可见文本 108 → 11898 字符
- 拦截网络 JSON 提取到 304 张图片，而解析渲染后 DOM 只有 94 张
  => 因此本模块同时返回「渲染后 HTML」与「拦截到的所有响应体」，两者都要用于提取

**架构要点（踩过的坑，勿改回）**：
统一使用 async API，并在**独立线程**里跑事件循环。
此前混用 sync API（sync_playwright）与 async API（asyncio.run），导致
`RuntimeError: asyncio.run() cannot be called from a running event loop`——
sync API 会占用当前线程的事件循环。而 FastAPI 的 async 端点同样有运行中的
事件循环，所以在专用线程里 asyncio.run() 是唯一对两种调用方都安全的做法。
"""
from __future__ import annotations

import asyncio
import os
import threading
import time
from dataclasses import dataclass, field

from .config import BROWSER_CANDIDATES, DEFAULT_UA
from . import browser_profile, cookie_store

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# 拦截响应体时，单个响应最大保留字节数（防止超大 JSON 拖垮内存）
MAX_BODY_BYTES = 8 * 1024 * 1024
# 最多保留多少个响应体
MAX_CAPTURED = 400

_LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",   # 降低被识别为自动化的概率
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-dev-shm-usage",
]


class RendererUnavailable(RuntimeError):
    """渲染能力不可用（未装 playwright / 无可用浏览器内核）。"""


@dataclass
class CapturedResponse:
    url: str
    status: int
    content_type: str
    body: str
    truncated: bool = False


@dataclass
class RenderResult:
    url: str
    final_url: str = ""
    title: str = ""
    html: str = ""
    captured: list[CapturedResponse] = field(default_factory=list)
    ok: bool = False
    error: str = ""
    engine: str = ""          # chrome / msedge / chromium
    elapsed: float = 0.0

    @property
    def json_bodies(self) -> list[CapturedResponse]:
        """只返回 JSON 类响应（通常含结构化数据，最有价值）。"""
        return [c for c in self.captured if "json" in c.content_type]


# ---------- 可用性探测（不启动浏览器，避免污染事件循环）----------

def _playwright_available() -> bool:
    try:
        import playwright.async_api  # noqa: F401
        return True
    except ImportError:
        return False


def _find_browser_channel() -> str:
    """探测本机已安装的浏览器内核，返回 playwright 的 channel 名；都没有则返回空。"""
    for channel, paths in BROWSER_CANDIDATES.items():
        for p in paths:
            if os.path.exists(p):
                return channel
    return ""


def _bundled_chromium_exists() -> bool:
    """检查 playwright 自带的 Chromium 是否已下载（不启动浏览器）。"""
    try:
        base = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
        if base:
            root = base
        else:
            root = os.path.join(os.path.expanduser("~"), "AppData", "Local", "ms-playwright")
        if not os.path.isdir(root):
            return False
        for name in os.listdir(root):
            if name.lower().startswith("chromium"):
                exe = os.path.join(root, name, "chrome-win", "chrome.exe")
                if os.path.exists(exe):
                    return True
        return False
    except Exception:
        return False


def renderer_status() -> dict:
    """渲染能力状态（供 /api/status 与设置页展示）。"""
    has_pw = _playwright_available()
    channel = _find_browser_channel()
    bundled = _bundled_chromium_exists() if has_pw else False
    available = has_pw and bool(channel or bundled)

    hint = ""
    if not has_pw:
        hint = ("未安装 playwright，无法渲染 JS 页面（阿里巴巴等站点会抓不到内容）。"
                "安装到项目内、不写 C 盘：\n"
                '  python -m pip install --target "<项目目录>\\_pylibs" playwright')
    elif not available:
        hint = ("已装 playwright，但没有可用浏览器内核。可安装自带 Chromium：\n"
                "  python -m playwright install chromium")
    return {
        "available": available,
        "playwright_installed": has_pw,
        "system_browser": channel,
        "bundled_chromium": bundled,
        "hint": hint,
    }


# ---------- 异步实现 ----------

async def _render_async(
    urls: list[str],
    *,
    concurrency: int,
    timeout_ms: int,
    settle_ms: int,
    scroll_rounds: int,
    capture_types: tuple[str, ...],
    user_agent: str,
    cookies: list[dict] | None = None,
    on_progress,
    should_cancel,
) -> list[RenderResult]:
    from playwright.async_api import async_playwright

    results: list[RenderResult] = [RenderResult(url=u) for u in urls]
    channel = _find_browser_channel()
    sem = asyncio.Semaphore(max(1, concurrency))
    done = {"n": 0}
    cancelled = {"v": False}

    async with async_playwright() as p:
        browser = None
        pctx = None
        engine = ""
        # ---- 登录态模式：用持久化配置目录启动（浏览器里已登录的账号直接复用） ----
        #
        # 为什么需要它（2026-09-23 实测）：抖音/小红书的主页作品列表接口需要浏览器自己
        # 生成的风控参数（抖音 Uifid/a_bogus、小红书 x-s）。我们不伪造签名，而是让页面
        # 自己的 JS 发请求、我们只拦响应；但前提是**已登录**且**有头窗口** ——
        # 实测同一抖音主页：无头模式拿到空壳（标题空、0 个 video），
        # 有头模式一次拿到 3 页接口响应、1438 条视频地址。
        prof_dir = browser_profile.user_data_dir()
        if prof_dir:
            _chk = browser_profile.ensure_usable()
            if not _chk.get("ok"):
                # 配置目录不可用：直接如实说明，别让用户对着「渲染失败」猜
                for r in results:
                    r.error = _chk.get("reason") or "登录态浏览器配置不可用"
                return results
            try:
                pctx = await p.chromium.launch_persistent_context(
                    user_data_dir=prof_dir,
                    headless=not browser_profile.headed(),
                    viewport={"width": 1600, "height": 1000},
                    locale="zh-CN",
                    user_agent=user_agent or DEFAULT_UA,
                    args=list(_LAUNCH_ARGS) + ["--disable-blink-features=AutomationControlled"],
                )
                engine = "persistent-profile"
                # 同一个 profile 目录不能并发（浏览器会抢锁），登录态模式下串行更稳
                concurrency = 1
                sem = asyncio.Semaphore(1)
                # ⚠️ 持久化上下文**也要**注入「设置」里的 cookies：
                # profile 管的是「浏览器自身的登录态」，cookies.txt 管的是「用户导出的登录态」，
                # 两者是不同的来源、可以同时存在。漏了这一步会出现
                # 「配了 cookies 但因为同时开了登录态 profile，cookies 悄悄失效」
                # （实测：test_cookies_offline 的渲染通道断言失败）。
                _pw_ck = cookie_store.playwright_cookies()
                if _pw_ck:
                    try:
                        await pctx.add_cookies(_pw_ck)
                    except Exception:
                        pass
            except Exception:
                # 登录态模式失败不能把整条抓取废掉：退回一次性上下文继续跑
                pctx = None
        # 优先用本机已装浏览器：启动快（0.6s）且无需下载约 300MB 内核
        if pctx is None and channel:
            try:
                browser = await p.chromium.launch(channel=channel, headless=True,
                                                  args=_LAUNCH_ARGS)
                engine = channel
            except Exception:
                browser = None
        if browser is None and pctx is None:
            try:
                browser = await p.chromium.launch(headless=True, args=_LAUNCH_ARGS)
                engine = "chromium"
            except Exception as e:
                msg = ("没有可用的浏览器内核（%s）。请执行："
                       "python -m playwright install chromium" % type(e).__name__)
                for r in results:
                    r.error = msg
                return results

        async def _one(idx: int, url: str):
            if should_cancel:
                try:
                    if should_cancel():
                        cancelled["v"] = True
                        return
                except Exception:
                    pass
            if cancelled["v"]:
                return
            async with sem:
                if cancelled["v"]:
                    return
                r = results[idx]
                r.engine = engine
                t0 = time.time()
                ctx = None
                page = None
                try:
                    if pctx is not None:
                        # 登录态模式：复用这个持久化上下文（登录态在 profile 里）
                        ctx = pctx
                        page = await ctx.new_page()
                    else:
                        ctx = await browser.new_context(
                            user_agent=user_agent,
                            viewport={"width": 1920, "height": 1080},
                            locale="zh-CN",
                        )
                        # 带上「设置」里配好的 cookies：需要登录的站点（Instagram 等）
                        # 匿名访问只会得到登录墙 / 「主页不存在」，正文里没有任何内容。
                        if cookies:
                            try:
                                await ctx.add_cookies(cookies)
                            except Exception:
                                pass
                        page = await ctx.new_page()

                    async def _on_resp(resp):
                        if len(r.captured) >= MAX_CAPTURED:
                            return
                        try:
                            ct = (resp.headers or {}).get("content-type", "") or ""
                            if not any(t in ct for t in capture_types):
                                return
                            body = await resp.text()
                            if not body:
                                return
                            r.captured.append(CapturedResponse(
                                url=resp.url, status=resp.status,
                                content_type=ct.split(";")[0].strip().lower(),
                                body=body[:MAX_BODY_BYTES],
                                truncated=len(body) > MAX_BODY_BYTES,
                            ))
                        except Exception:
                            pass

                    # 响应体读取是异步的，用 ensure_future 避免阻塞导航
                    page.on("response", lambda resp: asyncio.ensure_future(_on_resp(resp)))

                    await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                    await page.wait_for_timeout(settle_ms)
                    for _ in range(max(0, scroll_rounds)):
                        try:
                            await page.mouse.wheel(0, 3000)
                            await page.wait_for_timeout(500)
                        except Exception:
                            break
                    r.html = await page.content()
                    r.title = (await page.title()) or ""
                    r.final_url = page.url or url
                    r.ok = bool(r.html)
                    if not r.ok:
                        r.error = "渲染后未取到 HTML"
                except Exception as e:  # noqa: BLE001
                    low = str(e).lower()
                    if pctx is not None:
                        # 登录态配置目录同一时刻只能被一个浏览器打开。上一次异常退出
                        # （或被杀进程）会留下 lockfile + 残留窗口，后续渲染会静默失败
                        # （实测：页面拿不到内容，报「render-failed」但看不出原因）。
                        # 这里把真实原因说出来，用户关掉残留窗口即可恢复。
                        try:
                            from pathlib import Path as _P
                            if (_P(prof_dir) / "lockfile").exists():
                                r.error = ("登录态配置目录正被另一个浏览器实例占用"
                                           "（同一配置只能开一个窗口）。请关闭残留的浏览器"
                                           "窗口/进程后重试，或换一个配置目录。"
                                           f"原始错误：{type(e).__name__}")
                                r.elapsed = time.time() - t0
                                return
                        except Exception:
                            pass
                    if "timeout" in low:
                        r.error = f"加载超时（{timeout_ms // 1000}s）"
                    elif "net::err_name_not_resolved" in low:
                        r.error = "域名解析失败"
                    elif "net::err_ssl_protocol_error" in low or "err_cert" in low:
                        r.error = "站点 TLS/证书异常，浏览器拒绝连接"
                    elif "net::err_connection" in low:
                        r.error = "无法连接到站点"
                    else:
                        r.error = f"渲染失败: {type(e).__name__}: {str(e)[:120]}"
                finally:
                    # 必须先关 page 再关 context，否则会打出
                    # "Task was destroyed but it is pending!" 噪音。
                    # ⚠️ 登录态模式下 ctx 是所有页面共享的 pctx，**不能在这里关掉**，
                    # 否则后面几个页面会拿到已关闭的上下文。
                    for obj in (page, None if ctx is pctx else ctx):
                        if obj is None:
                            continue
                        try:
                            await obj.close()
                        except Exception:
                            pass
                    r.elapsed = time.time() - t0
                    done["n"] += 1
                    if on_progress:
                        try:
                            on_progress(done["n"], len(urls))
                        except Exception:
                            pass

        await asyncio.gather(*[_one(i, u) for i, u in enumerate(urls)],
                             return_exceptions=True)
        if pctx is not None:
            try:
                await pctx.close()
            except Exception:
                pass
        try:
            await browser.close()
        except Exception:
            pass
        # 让 playwright 的连接任务收尾，避免退出时打印 pending task 噪音
        await asyncio.sleep(0.3)
    return results


def _run_in_thread(coro_factory):
    """在专用线程里跑 asyncio.run()。

    为什么必须这样：调用方可能已经运行在一个事件循环里
    （FastAPI 的 async 端点、或 playwright sync API 占用的循环），
    此时在当前线程直接 asyncio.run() 会抛
    "cannot be called from a running event loop"。
    专用线程有自己独立的事件循环，对任何调用方都安全。
    """
    box: dict = {}

    def _runner():
        try:
            box["result"] = asyncio.run(coro_factory())
        except BaseException as e:  # noqa: BLE001
            box["error"] = e

    t = threading.Thread(target=_runner, daemon=True)
    t.start()
    t.join()
    if "error" in box:
        raise box["error"]
    return box.get("result")


# ---------- 对外同步接口 ----------

def render_many(
    urls: list[str],
    *,
    concurrency: int = 4,
    timeout_ms: int = 45000,
    settle_ms: int = 2500,
    scroll_rounds: int = 2,
    capture_types: tuple[str, ...] = ("json",),
    user_agent: str = _UA,
    on_progress=None,
    should_cancel=None,
) -> list[RenderResult]:
    """并发渲染多个页面，返回与输入等长、顺序一致的结果列表。"""
    urls = [u for u in (urls or []) if u]
    if not urls:
        return []
    if not _playwright_available():
        err = ("未安装 playwright，无法渲染 JS 页面。请执行："
               'python -m pip install --target "<项目目录>\\_pylibs" playwright')
        return [RenderResult(url=u, error=err) for u in urls]

    try:
        return _run_in_thread(lambda: _render_async(
            urls,
            concurrency=concurrency,
            timeout_ms=timeout_ms,
            settle_ms=settle_ms,
            scroll_rounds=scroll_rounds,
            capture_types=capture_types,
            user_agent=user_agent or DEFAULT_UA,
            cookies=cookie_store.playwright_cookies() or None,
            on_progress=on_progress,
            should_cancel=should_cancel,
        ))
    except RendererUnavailable as e:
        return [RenderResult(url=u, error=str(e)) for u in urls]
    except Exception as e:  # noqa: BLE001
        msg = f"渲染失败: {type(e).__name__}: {e}"
        return [RenderResult(url=u, error=msg) for u in urls]


def render_page(
    url: str,
    *,
    timeout_ms: int = 60000,
    settle_ms: int = 3500,
    scroll_rounds: int = 6,
    capture_types: tuple[str, ...] = ("json",),
    user_agent: str = _UA,
    on_page=None,   # 兼容旧签名，async 实现下不再支持
) -> RenderResult:
    """渲染单个页面。"""
    out = render_many(
        [url],
        concurrency=1,
        timeout_ms=timeout_ms,
        settle_ms=settle_ms,
        scroll_rounds=scroll_rounds,
        capture_types=capture_types,
        user_agent=user_agent,
    )
    return out[0] if out else RenderResult(url=url, error="未返回结果")


def shutdown_renderer() -> None:
    """兼容旧接口：浏览器实例不再跨调用复用（每次渲染自行启动与关闭），无需清理。"""
    return None
