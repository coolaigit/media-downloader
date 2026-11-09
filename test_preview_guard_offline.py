# -*- coding: utf-8 -*-
"""F-24 离线用例：会员试看段的判定必须与「自动换源」开关解耦。

背景（2026-11-08 用户报「人间世第二季第 8 集只下到 6 分钟，却当整集交付，
文件名是『视频下载 [指纹].mp4』」）：
  会员平台的页面播放地址本身就被账号权限裁剪过，浏览器解析出的「真实地址」
  同样是 6 分钟试看段，于是下载「成功」。守卫 `_preview_guard()` 的第一版把
  「要不要核对」写进了 `_vip_should_try()` 的条件里 —— 用户关掉自动换源，
  核对也一起被跳过，6 分钟片段照样静默交付。

这个用例是纯离线的：不联网、不起服务。三个层面：
  1. `_preview_scope()`：哪些地址需要核对（B 站 UGC 不裁剪，不核对）；
  2. `_dur_text()` / `_PLACEHOLDER_TITLES`：提示文案与占位标题；
  3. `_preview_guard()`：关掉换源仍然要出 ⚠️ 提示（核心回归点）。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import video_downloader as vd
from app import vip_unlock

checks: list[tuple[str, bool, str]] = []


def ck(name: str, cond: bool, extra: object = "") -> None:
    checks.append((name, bool(cond), str(extra)))
    print(("  ok   " if cond else "  FAIL ") + name
          + ("" if cond else "  << " + str(extra)), flush=True)


def test_scope() -> None:
    print("[核对范围]", flush=True)
    ck("B 站番剧页要核对",
       vd._preview_scope("https://www.bilibili.com/bangumi/play/ep810642"))
    ck("B 站课堂要核对",
       vd._preview_scope("https://www.bilibili.com/cheese/play/ep12345"))
    ck("带 ep_id 的播放页要核对",
       vd._preview_scope("https://www.bilibili.com/video/BV1xx411c7mD?ep_id=810642"))
    ck("B 站普通投稿不核对（UGC 不裁剪）",
       not vd._preview_scope("https://www.bilibili.com/video/BV1xx411c7mD"))
    ck("非会员站不核对",
       not vd._preview_scope("https://example.com/movie/1"))
    ck("空地址不核对", not vd._preview_scope(""))


def test_duration_text() -> None:
    print("[时长文案]", flush=True)
    ck("360 → 6:00", vd._dur_text(360) == "6:00", vd._dur_text(360))
    ck("3075 → 51:15", vd._dur_text(3075) == "51:15", vd._dur_text(3075))
    ck("3723 → 1:02:03", vd._dur_text(3723) == "1:02:03", vd._dur_text(3723))
    ck("0 → 0:00", vd._dur_text(0) == "0:00", vd._dur_text(0))
    ck("负数不炸", vd._dur_text(-5) == "0:00", vd._dur_text(-5))
    ck("小数四舍五入", vd._dur_text(359.6) == "6:00", vd._dur_text(359.6))


def test_placeholder_titles() -> None:
    print("[占位标题]", flush=True)
    for t in ("视频下载", "视频", "download", "video", "未命名", "untitled"):
        ck("占位标题认得「%s」" % t, t in vd._PLACEHOLDER_TITLES)
    for t in ("人间世 第二季 第8集 儿科医生", "【官方 MV】Never Gonna Give You Up"):
        ck("真片名不当占位「%s」" % t[:12], t not in vd._PLACEHOLDER_TITLES)


def test_guard_with_switch_off() -> None:
    """核心回归：关掉自动换源，判定照样执行，提示照样出。

    extract_meta / probe_duration 都挂桩，不联网；
    _vip_should_try 挂成 False 模拟用户在设置里关掉了自动换源。
    """
    print("[守卫 × 换源开关]", flush=True)
    url = "https://www.bilibili.com/bangumi/play/ep810642"

    old_meta = getattr(vip_unlock, "extract_meta", None)
    old_probe = getattr(vip_unlock, "probe_duration", None)
    old_try = vd._vip_should_try
    old_replace = vd._vip_replace_with_full
    called = {"replace": 0}

    def fake_replace(*a, **kw):
        called["replace"] += 1
        return None

    try:
        vip_unlock.extract_meta = lambda u: {"duration": 3075}       # 正片 51:15
        vip_unlock.probe_duration = lambda p: 360.0                  # 实下 6:00
        vd._vip_should_try = lambda u, error_text="": False          # 用户关掉换源
        vd._vip_replace_with_full = fake_replace

        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "clip.mp4"
            p.write_bytes(b"x" * 64)
            res, note = vd._preview_guard(url, Path(td), [{"path": str(p)}],
                                          task_id="t1", title="人间世", info=None,
                                          on_progress=None)
        ck("关掉换源时守卫仍返回 None（表示没换到完整版）", res is None, res)
        ck("关掉换源时仍然出提示（不是静默放行）", bool(note), note)
        ck("提示带 ⚠️", note.startswith("⚠️"), note)
        ck("提示写明实际时长 6:00", "6:00" in note, note)
        ck("提示写明正片时长 51:15", "51:15" in note, note)
        ck("提示明确说不是完整正片", "不是完整正片" in note, note)
        ck("关掉换源时不去调换源", called["replace"] == 0, called)

        # 完整正片：不该有提示
        vip_unlock.probe_duration = lambda p: 3070.0
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "full.mp4"
            p.write_bytes(b"x" * 64)
            res2, note2 = vd._preview_guard(url, Path(td), [{"path": str(p)}],
                                            task_id="t2", title="人间世", info=None,
                                            on_progress=None)
        ck("完整正片不出提示", res2 is None and not note2, (res2, note2))

        # 没有任何文件：不该碰网络
        res3, note3 = vd._preview_guard(url, Path("."), [], task_id="t3",
                                        title="x", info=None, on_progress=None)
        ck("没有文件时直接返回", res3 is None and not note3, (res3, note3))

        # B 站普通投稿：跳过核对（把 extract_meta 换成会抛的桩，证明没被调用）
        def boom(u):
            raise AssertionError("普通投稿不该请求元数据")

        vip_unlock.extract_meta = boom
        res4, note4 = vd._preview_guard("https://www.bilibili.com/video/BV1xx411c7mD",
                                        Path("."), [{"path": "nope.mp4"}], task_id="t4",
                                        title="x", info=None, on_progress=None)
        ck("普通投稿跳过核对且不抛", res4 is None and not note4, (res4, note4))
    finally:
        if old_meta is not None:
            vip_unlock.extract_meta = old_meta
        if old_probe is not None:
            vip_unlock.probe_duration = old_probe
        vd._vip_should_try = old_try
        vd._vip_replace_with_full = old_replace


def test_lang_prefix() -> None:
    """F-28：带语言前缀的 B 站地址必须先剥成 yt-dlp 认的形态。

    yt-dlp 的 bilibili extractor 只认 `/bangumi/play/ep<id>`；带 `/zh-Hans/`
    时直接报 unsupported url，界面显示「该链接不是受支持的视频页面」。
    """
    print("[语言前缀归一]", flush=True)
    nm = vd._normalize_media_url
    ck("zh-Hans 被剥掉",
       nm("https://www.bilibili.com/zh-Hans/bangumi/play/ep810642")
       == "https://www.bilibili.com/bangumi/play/ep810642",
       nm("https://www.bilibili.com/zh-Hans/bangumi/play/ep810642"))
    ck("大写 ZH-HANS 也剥",
       nm("https://www.bilibili.com/ZH-HANS/bangumi/play/ep810642")
       == "https://www.bilibili.com/bangumi/play/ep810642")
    ck("查询串保留",
       nm("https://www.bilibili.com/zh-Hans/bangumi/play/ep810642?from_spmid=1")
       == "https://www.bilibili.com/bangumi/play/ep810642?from_spmid=1")
    ck("没有语言段时不动",
       nm("https://www.bilibili.com/bangumi/play/ep810642")
       == "https://www.bilibili.com/bangumi/play/ep810642")
    ck("/tv/123 不被当成语言段",
       nm("https://www.bilibili.com/tv/123") == "https://www.bilibili.com/tv/123")
    ck("单段 /zh-Hans 不动",
       nm("https://www.bilibili.com/zh-Hans") == "https://www.bilibili.com/zh-Hans")
    ck("别的站点不动",
       nm("https://example.com/zh-Hans/a/1") == "https://example.com/zh-Hans/a/1")
    ck("zhihu /video/ 归一仍生效",
       nm("https://www.zhihu.com/video/1342930761977176064")
       == "https://www.zhihu.com/zvideo/1342930761977176064")
    ck("主机名混淆不误伤（evilbilibili.com）",
       nm("https://evilbilibili.com/zh-Hans/x/1")
       == "https://evilbilibili.com/zh-Hans/x/1")


def main() -> int:
    test_scope()
    test_duration_text()
    test_placeholder_titles()
    test_guard_with_switch_off()
    test_lang_prefix()
    bad = [c for c in checks if not c[1]]
    print("\n结果：%d 通过 / %d 失败" % (len(checks) - len(bad), len(bad)), flush=True)
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
