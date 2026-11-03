# -*- coding: utf-8 -*-
"""渲染内核自检的离线用例（不需要开窗口 / 不需要 WebView2）。

背景（2026-11-03）：V3.0 在某台 Win10 上白屏，根因是 pywebview 静默降级到
MSHTML(IE11)。当时的自检写的是 `evaluate_js('1+1') == 2`，但实测 IE11 下
`1+1` **正常返回 2**（只用到 eval + JSON.stringify）—— 自检恒为真，等于没修。
现在的判据是 UA 指纹 + ES6 语法 + 前端就绪标记，本文件锁住这几条。
"""
import time

from app import desktop_shell as ds

IE11_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64; Trident/7.0; rv:11.0) "
           "like Gecko")
EDGE_CHROMIUM_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36 "
                    "Edg/156.0.0.0")
OLD_EDGE_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/70.0.3538.102 Safari/537.36 "
               "Edge/18.19045")


def test_ie11_ua_is_legacy():
    assert ds._LEGACY_UA_RE.search(IE11_UA)


def test_old_edgehtml_ua_is_legacy():
    assert ds._LEGACY_UA_RE.search(OLD_EDGE_UA)


def test_edge_chromium_ua_is_not_legacy():
    # 关键：新版 Edge 的 UA 里也有 "Edg/"，不能误判成老 Edge
    assert not ds._LEGACY_UA_RE.search(EDGE_CHROMIUM_UA)


def test_version_compare():
    assert ds._version_tuple("153.0.4234.48") >= ds._version_tuple(ds.WEBVIEW2_MIN_BUILD)
    assert ds._version_tuple("85.1.0") < ds._version_tuple(ds.WEBVIEW2_MIN_BUILD)


def test_eval_js_times_out_instead_of_hanging():
    """pywebview 的 evaluate_js 内部没有超时，卡死时必须由我们自己兜住。"""

    class HangingWindow:
        def evaluate_js(self, script, *a, **k):
            time.sleep(30)
            return 1

    t0 = time.monotonic()
    ok, _, err = ds._eval_js(HangingWindow(), "1", timeout=0.5)
    elapsed = time.monotonic() - t0
    assert ok is False
    assert "超时" in err
    assert elapsed < 3.0, f"超时保护没生效，耗时 {elapsed:.2f}s"


def test_eval_js_propagates_exception():
    class BrokenWindow:
        def evaluate_js(self, script, *a, **k):
            raise RuntimeError("boom")

    ok, _, err = ds._eval_js(BrokenWindow(), "1", timeout=2.0)
    assert ok is False
    assert "boom" in err


def test_renderer_signature_legacy_by_ua():
    class Win:
        def evaluate_js(self, script, *a, **k):
            return IE11_UA

    verdict, detail = ds._renderer_signature(Win())
    assert verdict == "legacy"
    assert "Trident" in detail


def test_renderer_signature_ok_needs_es6():
    """UA 不是 IE，但 ES6 跑不了 —— 同样必须判 legacy。"""

    class Win:
        def evaluate_js(self, script, *a, **k):
            if "userAgent" in script:
                return "Mozilla/5.0 (Windows NT 10.0) CustomBrowser/1.0"
            raise RuntimeError("SyntaxError")

    verdict, _ = ds._renderer_signature(Win())
    assert verdict == "legacy"


def test_renderer_signature_ok():
    class Win:
        def evaluate_js(self, script, *a, **k):
            if "userAgent" in script:
                return EDGE_CHROMIUM_UA
            return 1

    verdict, detail = ds._renderer_signature(Win())
    assert verdict == "ok"
    assert "Edg/156" in detail


def test_no_webview2_skips_window(monkeypatch):
    """假定本机没有 WebView2 时，不创建窗口、直接带回原因。"""
    monkeypatch.setattr(ds, "_webview2_available", lambda: (False, ""))
    holder = {}
    t0 = time.monotonic()
    assert ds._open_native("http://127.0.0.1:1/", False, holder) == -1
    assert holder.get("fallback_reason")
    assert time.monotonic() - t0 < 1.0


def test_fallback_message_is_actionable():
    text = ds._fallback_message("http://127.0.0.1:8848/", "测试原因")
    assert "测试原因" in text
    assert "127.0.0.1:8848" in text
    assert "WebView2" in text
    assert "以浏览器模式启动.cmd" in text
