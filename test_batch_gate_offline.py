# -*- coding: utf-8 -*-
"""批量抓取「验证页 / 登录页素材被当作品」通用闸门离线用例（不联网）。

复现证据（2026-11-07，按前端 kinds 真实跑 crawl_list）：
  - 快手个人页视频：16 条「视频」全是 /new-reco、/help、登录 passToken 等导航；
  - 知乎问题页视频：30 条「视频」全是 static.zhihu.com 的 js/css/png/ico；
  - 知乎问题页图片：6 张全是安全验证页自己的 zhimg 装饰图；
  - 小红书个人页视频：0 条，页面停在扫码登录页。
修法必须靠地址形态 / 可见文本，不含任何站点专用规则。
跑法：./_venv/Scripts/python.exe test_batch_gate_offline.py
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.profile_sources import (_looks_like_work_url, _same_target_page,
                                 _wall_from_html, browser_items)

ROOT = Path(__file__).resolve().parent
FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = ""):
    if cond:
        print(f"  ok   {name}")
    else:
        FAILED.append(name)
        print(f"  FAIL {name} {detail}")


BASE_KS = "https://www.kuaishou.com/profile/3xnmi2xh2pxzpag"
BASE_ZH = "https://www.zhihu.com/question/665425481"


def test_bad_links():
    print("[导航 / 静态资源 / 验证页素材一律不算作品]")
    bad = [
        "https://www.kuaishou.com/new-reco",
        "https://www.kuaishou.com/myFollow",
        "https://www.kuaishou.com/about/contact",
        "https://www.kuaishou.com/help/report",
        "https://www.kuaishou.com/about/policy",
        "https://www.kuaishou.com/committee/",
        "https://www.kuaishou.com/official/material-lib",
        "https://cp.kuaishou.com/article/publish/video?origin=www.kuaishou.com",
        "https://cp.kuaishou.com/article/manage/video",
        "https://docs.corp.kuaishou.com/k/home/VbQx2ulc6v3M/fcADLOKqHyHpSr7hiCKKQVtVM",
        "https://live.kuaishou.com/live-partner",
        "https://m.kuaishou.com/download/kwai?platform=android",
        "https://id.kuaishou.com/pass/kuaishou/login/passToken?sid=x",
        "https://p66-plat.wskwai.com/kos/nlav111422/ks-web/manifest.webm",
        "https://static.zhihu.com/zse-ck/v4/bf2133dccac55466aa141a646b00b.js",
        "https://static.zhihu.com/heifetz/favicon.ico",
        "https://static.zhihu.com/heifetz/assets/apple-touch-icon-152.81060cab.png",
        "https://static.zhihu.com/heifetz/528.216a26f4.32050ee4e64e9808af65.css",
        "https://static.zhihu.com/event/react@17.0.2/umd/react.production.min.js",
        "https://apm.zhihu.com/collector/web_json",
        "https://www.zhihu.com/sc-assets-tracker",
        "https://www.zhihu.com/appview/v2/badge_center/detail?userToken=",
        "https://web-render.zhihu.com/api/v3/feed/topstory/recommend?",
        "https://billboard-er.zhihu.com/api/v3/feed/topstory/hot-lists/total?",
        "https://www.zhihu.com/messages/3d198a56310c02c4a83efb9f4a4c027e?message=",
        "https://www.zhihu.com/creator/explore/academy/rights/rights",
        "https://www.zhihu.com/term/video",
        "https://zhstatic.zhihu.com/assets/report/infringement_letter_v2.docx",
    ]
    for u in bad:
        check(f"拒绝 {u.split('/')[-1][:42]}",
              _looks_like_work_url(u, BASE_KS) is False, u)
    check("共 28 条垃圾链接全部拒绝", len(bad) == 28)


def test_good_links():
    print("[真作品页 / 真媒体直链必须放行]")
    good = [
        ("https://www.xiaohongshu.com/explore/64b1f2ab000000001203c8de", BASE_KS),
        ("https://www.kuaishou.com/short-video/3xnmi2xh2pxzpag", BASE_KS),
        ("https://www.zhihu.com/question/665425481/answer/3123456789", BASE_ZH),
        ("https://www.douyin.com/video/7293847561029384756", BASE_KS),
        ("https://sns-video-hw.xhscdn.com/stream/123456789.mp4", BASE_KS),
        ("https://cdn.example.com/vod/2026/11/film_1080p.m3u8?sign=abc", BASE_ZH),
        ("https://example.com/videos/33006/some-slug", BASE_ZH),
    ]
    for u, base in good:
        check(f"放行 {u.split('/')[-1][:42]}",
              _looks_like_work_url(u, base) is True, u)
    check("列表页自身不算作品",
          _looks_like_work_url(BASE_KS, BASE_KS) is False)


def test_wall_detect():
    print("[验证墙 / 登录墙只看标题 + 可见文本]")
    zh_html = ("<html><head><title>安全验证 - 知乎</title></head><body>"
               "<div>请稍候，正在检查访问环境</div></body></html>")
    xhs_html = ("<html><head><title>小红书 - 你的生活兴趣社区</title></head>"
                "<body>登录后推荐更懂你的笔记 可用 小红书 或 微信 扫码 "
                "手机号登录 获取验证码</body></html>")
    ok_html = ("<html><head><title>作品列表</title></head><body>"
               "<a href='/video/123456789'>第一集</a> 正文内容足够长的一段说明"
               "</body></html>")
    k1, q1 = _wall_from_html(zh_html)
    k2, q2 = _wall_from_html(xhs_html)
    k3, _ = _wall_from_html(ok_html)
    check("安全验证页 → verification", k1 == "verification", k1)
    check("验证页原话带出来", "安全验证" in q1, q1[:80])
    check("扫码登录页 → login", k2 == "login", k2)
    check("登录页原话带出来", "扫码" in q2 or "登录" in q2, q2[:80])
    check("正常页面不误报", k3 == "", k3)
    # 脚本里出现 captcha 这类前端功能名不能误报（工程手册 2026-09-22 的坑）
    script_html = ("<html><head><title>作品列表</title></head><body>正文"
                   "<script>var captchaUrl='/verify';</script></body></html>")
    check("只看可见文本，不扫脚本", _wall_from_html(script_html)[0] == "")


def test_browser_items_gate():
    print("[browser_items 撞墙时不制造假作品]")
    zh_html = """<html><head><title>安全验证 - 知乎</title></head><body>
      <a href='/sc-assets-tracker'>t</a>
      <script src='https://static.zhihu.com/zse-ck/v4/abc.js'></script>
      <img src='https://static.zhihu.com/heifetz/favicon.ico'>
      <a href='https://www.zhihu.com/account/unhuman?type=U4E3Z1'>验证</a>
      <div>安全验证</div></body></html>"""
    fake = SimpleNamespace(ok=True, html=zh_html, title="安全验证 - 知乎",
                           final_url=BASE_ZH, captured=[],
                           error="", elapsed=1.0)
    with patch("app.renderer.render_page", return_value=fake):
        res = browser_items(BASE_ZH, limit=30)
    check("验证页不再返回 30 条假视频", not res.get("items"),
          str(res.get("items"))[:120])
    check("明确标 verification_required",
          bool(res.get("verification_required")), str(res)[:160])
    check("错误文案带页面原话", "安全验证" in str(res.get("error") or ""),
          str(res.get("error"))[:160])
    check("给出可操作路径",
          "Cookies" in str(res.get("error") or "")
          or "网络出口" in str(res.get("error") or ""))

    xhs_html = """<html><head><title>小红书</title></head><body>
      <a href='/login?redirectPath=x'>登录</a>
      <a href='https://www.xiaohongshu.com/help/feedback'>帮助</a>
      <div>扫码登录 登录后推荐更懂你的笔记</div></body></html>"""
    fake2 = SimpleNamespace(ok=True, html=xhs_html, title="小红书",
                            final_url="https://www.xiaohongshu.com/user/profile/abc123456",
                            captured=[], error="", elapsed=1.0)
    with patch("app.renderer.render_page", return_value=fake2):
        res2 = browser_items("https://www.xiaohongshu.com/user/profile/abc123456",
                             limit=30)
    check("登录页不返回导航链接", not res2.get("items"),
          str(res2.get("items"))[:120])
    check("明确标 login_required", bool(res2.get("login_required")))
    check("指引去平台登录中心",
          "平台登录中心" in str(res2.get("error") or ""),
          str(res2.get("error"))[:160])

    # 真作品不能被闸门误杀
    # 刻意用**通用站点**：browser_items 对快手已有专属通道（解析它自己的
    # feed 接口，2026-11-08），不再经过这道闸门，用快手地址测不到本意。
    BASE_GEN = "https://www.example.com/profile/1234567890"
    good_html = """<html><head><title>作品列表</title></head><body>
      <a href='https://www.example.com/video/3xabc123def'>作品一</a>
      <a href='https://www.example.com/video/3xghi456jkl'>作品二</a>
      <div>这里是正常的作品列表正文，用来确认没有被误判成登录页</div>
      </body></html>"""
    fake3 = SimpleNamespace(ok=True, html=good_html, title="作品列表",
                            final_url=BASE_GEN, captured=[], error="", elapsed=1.0)
    with patch("app.renderer.render_page", return_value=fake3):
        res3 = browser_items(BASE_GEN, limit=30)
    check("正常作品页仍照常返回", res3.get("ok") is True
          and len(res3.get("items") or []) >= 2,
          str(res3)[:200])


def test_redirect_gate():
    print("[被赶到验证/登录页时，捕获响应里的裸 URL 也不能当作品]")
    check("目标页被换成 /account/unhuman → 不一致",
          _same_target_page(
              BASE_ZH, "https://www.zhihu.com/account/unhuman?type=U4E3Z1") is False)
    check("目标页被换成首页 → 不一致",
          _same_target_page(BASE_KS, "https://www.kuaishou.com/") is False)
    check("停在目标页 → 一致", _same_target_page(BASE_KS, BASE_KS) is True)
    check("目标页下钻到子路径 → 一致",
          _same_target_page(BASE_ZH, BASE_ZH + "/answer/3123456789") is True)

    html = ("<html><head><title>安全验证 - 知乎</title></head><body>"
            "<a href='/login'>登录</a><div>安全验证</div></body></html>")
    captured = [SimpleNamespace(body=(
        '{"urls":['
        '"https://web-render.zhihu.com/api/v3/feed/topstory/recommend",'
        '"https://zhuanlan.zhihu.com/p/96956163",'
        '"https://www.zhihu.com/question/1802492974/answer/12754771901"'
        ']}\n'))]
    fake = SimpleNamespace(
        ok=True, html=html, title="安全验证 - 知乎",
        final_url="https://www.zhihu.com/account/unhuman?type=U4E3Z1",
        captured=captured, error="", elapsed=1.0)
    with patch("app.renderer.render_page", return_value=fake):
        res = browser_items(BASE_ZH, limit=30)
    check("重定向后 0 条作品", not res.get("items"),
          str(res.get("items"))[:160])
    check("重定向明确标验证", bool(res.get("verification_required")),
          str(res)[:160])
    check("错误说明没有停在目标页",
          "没有停在目标页" in str(res.get("error") or "")
          and "account/unhuman" in str(res.get("error") or ""),
          str(res.get("error"))[:180])


def test_source_signatures():
    print("[源码签名：闸门接在两条路径上，且不写站点专用规则]")
    ps = (ROOT / "app" / "profile_sources.py").read_text(encoding="utf-8")
    bc = (ROOT / "app" / "batch_crawler.py").read_text(encoding="utf-8")
    check("媒体直链与候选链接都过形态判据",
          ps.count("_looks_like_work_url(u, url)") >= 2)
    check("平台来源撞验证页直接早退",
          'res.get("verification_required")' in ps)
    check("重定向到验证/登录页直接失败",
          "def _same_target_page" in ps
          and "没有停在目标页" in ps)
    check("验证/登录页素材不进结果", "wall_stop" in bc
          and "该页的图标/脚本不是作品" in bc
          and "all_media = []" in bc and "detail_media = []" in bc)
    check("平台来源明确失败时停止回退", "def _empty(ws: list[str])" in bc
          and 'return _empty(warnings + [pt_note])' in bc)
    check("失败原因不再被『核对账号名』盖掉",
          '"平台登录中心", "安全验证"' in bc)
    # 判据函数体里不得出现任何站点名（红线：只靠地址形态 + 字段语义）
    body = ps.split("def _looks_like_work_url", 1)[1].split(
        "def _wall_from_html", 1)[0]
    check("判据实现不含站点专用分支",
          not any(w in body for w in
                  ("kuaishou", "zhihu", "xiaohongshu", "douyin", "instagram")))


if __name__ == "__main__":
    test_bad_links()
    test_good_links()
    test_wall_detect()
    test_browser_items_gate()
    test_redirect_gate()
    test_source_signatures()
    print()
    if FAILED:
        print(f"FAILED {len(FAILED)}: {FAILED}")
        sys.exit(1)
    print("ALL PASS")
