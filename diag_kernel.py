# -*- coding: utf-8 -*-
"""桌面窗口渲染内核诊断（排查「白窗口 / 按钮全哑」用）。

为什么有这个脚本：V3.0 在某台 Win10 上白屏，根因是 pywebview 静默把渲染内核
降级到 MSHTML(IE11)，而程序当时**一个字都没记**，用户侧完全无从排查。
这个脚本把「内核判定依据」全部摊开，出问题让人跑一次就能定死。

用法：
    python diag_kernel.py                     # 只看环境（不开窗口，秒出）
    python diag_kernel.py --gui mshtml        # 用 IE 内核开窗，验证自检能抓出来
    python diag_kernel.py --url http://127.0.0.1:8848/   # 连真实页面验前端就绪标记

判读：
    环境段出现「WebView2 运行时：无」          → 该机没装/被清理，桌面窗口必然不可用
    --gui mshtml 时内核画像判成 ok             → 自检失效（曾经就是这个问题）
    --gui mshtml 时判成 legacy（Trident）      → 自检正常
"""
from __future__ import annotations

import argparse
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import desktop_shell as ds  # noqa: E402


def report_env() -> None:
    print("=" * 66)
    print(" 环境探测")
    print("=" * 66)
    print(f"  脚本解释器        : {sys.executable}")
    print(f"  进程架构          : {_arch()}")

    try:
        import webview
        print(f"  pywebview         : {getattr(webview, '__version__', '?')}"
              f"  ({Path(webview.__file__).parent})")
    except Exception as exc:  # noqa: BLE001
        print(f"  pywebview         : 导入失败 {type(exc).__name__}: {exc}")

    rt = ds._webview2_runtime_dir()
    print(f"  WebView2 运行时   : {rt or '无（文件系统里没找到 msedgewebview2.exe）'}")
    print(f"  注册表判定        : {'有' if ds._webview2_registry_ok() else '无'}"
          f"  （pywebview 只认注册表，文件系统这一层是我们补的兜底）")
    ok, detail = ds._webview2_available()
    print(f"  综合可用          : {ok}  {detail}")

    try:
        from webview.util import interop_dll_path
        for a in ("win-x64", "win-arm64", "win-x86"):
            d = interop_dll_path(a)
            print(f"  loader {a:<10}: 存在={Path(d).is_dir()}  {d}")
    except Exception as exc:  # noqa: BLE001
        print(f"  loader 目录       : 取不到（{type(exc).__name__}: {exc}）")

    print(f"  数据目录          : {ds.DATA_ROOT}")
    print(f"  壳层日志          : {ds._log_path()}")
    print(f"  pywebview 日志    : {Path(ds.DATA_ROOT) / 'logs' / 'desktop_webview.log'}")
    print()


def _arch() -> str:
    import platform
    return f"{platform.machine()} / {platform.architecture()[0]}"


def report_window(gui: str, url: str, wait: float) -> int:
    import webview

    print("=" * 66)
    print(f" 开窗探测（gui={gui}, url={url or '内置空白页'}）")
    print("=" * 66)
    if url:
        win = webview.create_window("diag", url)
    else:
        win = webview.create_window("diag", html="<html><body><h1>diag</h1></body></html>")

    result: dict = {}

    def worker() -> None:
        time.sleep(wait)
        result["signature"] = ds._renderer_signature(win)
        print(f"  内核画像          : {result['signature']}")
        if url:
            ready = ds._wait_frontend_ready(win, 20.0)
            result["ready"] = ready
            print(f"  前端就绪标记      : {ready}")
        try:
            win.destroy()
        except Exception as exc:  # noqa: BLE001
            print(f"  关窗失败          : {exc}")

    threading.Thread(target=worker, daemon=True).start()
    webview.start(gui=gui)

    verdict = (result.get("signature") or ("unknown",))[0]
    print()
    if verdict == "ok" and result.get("ready", (True,))[0]:
        print(" 结论：这个内核可以正常跑界面。")
        return 0
    if verdict == "legacy":
        print(" 结论：内核被降级成 IE/MSHTML —— 自检能识别，程序会转浏览器模式。")
        return 2
    print(f" 结论：内核不可用（{verdict}）—— 自检能识别，程序会转浏览器模式。")
    return 3


def main() -> int:
    ap = argparse.ArgumentParser(description="桌面窗口渲染内核诊断")
    ap.add_argument("--gui", choices=["edgechromium", "mshtml"], default=None,
                    help="指定内核开窗；省略则只做环境探测")
    ap.add_argument("--url", default="", help="要加载的页面，省略用内置空白页")
    ap.add_argument("--wait", type=float, default=2.5, help="开窗后等多久再探测（秒）")
    args = ap.parse_args()

    report_env()
    if not args.gui:
        ok, _ = ds._webview2_available()
        print(" 下一步：想验证开窗行为，加 --gui edgechromium（正常）"
              "或 --gui mshtml（模拟降级）")
        return 0 if ok else 1
    return report_window(args.gui, args.url, args.wait)


if __name__ == "__main__":
    sys.exit(main())
