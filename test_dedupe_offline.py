# -*- coding: utf-8 -*-
"""图片去重：同一张图的不同尺寸档位必须归成一条。

背景（2026-11-08 用户报「知乎抓取图片会有重复图」）：
  知乎正文里同一张图会同时以 `v2-xxx_720w.jpg`、`v2-xxx_1440w.jpg`、
  `v2-xxx_r.jpg`、`v2-xxx.jpg` 四种写法出现，URL 各不相同但内容是同一张。
  `dedupe()` 原本只按完整 URL 比，结果列表里就有两三份一模一样的图。

这个用例是纯离线的：只喂 MediaItem，不联网、不起服务。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.extractor import MediaItem, dedupe, enrich_with_originals, strip_thumb_suffix

checks: list[tuple[str, bool, str]] = []


def ck(name: str, cond: bool, extra: object = "") -> None:
    checks.append((name, bool(cond), str(extra)))
    print(("  ok   " if cond else "  FAIL ") + name + ("" if cond else "  << " + str(extra)),
          flush=True)


def norm(url: str) -> str:
    return strip_thumb_suffix(url) or url


def main() -> int:
    print("[尺寸后缀归一化]", flush=True)
    same = [
        "https://pic1.zhimg.com/v2-abc_1440w.jpg",
        "https://pic1.zhimg.com/v2-abc_720w.jpg",
        "https://pic1.zhimg.com/v2-abc_r.jpg",
        "https://pic1.zhimg.com/v2-abc_b.jpg",
        "https://x.com/a.jpg_300x300.jpg",
        "https://x.com/b_w300.jpg",
        "https://x.com/c-300x300.jpg",
    ]
    for u in same:
        ck(f"归一化后不再含尺寸尾巴：{u.rsplit('/', 1)[-1]}",
           norm(u) != u and "1440w" not in norm(u) and "720w" not in norm(u), norm(u))

    ck("四档知乎同图归一化到同一地址",
       len({norm("https://pic1.zhimg.com/v2-abc_1440w.jpg"),
            norm("https://pic1.zhimg.com/v2-abc_720w.jpg"),
            norm("https://pic1.zhimg.com/v2-abc_r.jpg"),
            norm("https://pic1.zhimg.com/v2-abc_b.jpg")}) == 1,
       norm("https://pic1.zhimg.com/v2-abc_r.jpg"))

    print("\n[不能被误合并]", flush=True)
    for u in ("https://x.com/photo-2023-04-15.jpg",
              "https://x.com/a_b_c.jpg",
              "https://pic1.zhimg.com/v2-abc.jpg"):
        ck(f"原样保留：{u.rsplit('/', 1)[-1]}", strip_thumb_suffix(u) is None, u)
    ck("两张不同的图不能归一化到一起",
       norm("https://pic1.zhimg.com/v2-aaa_720w.jpg")
       != norm("https://pic1.zhimg.com/v2-bbb_720w.jpg"))

    print("\n[dedupe]", flush=True)
    raw = [
        MediaItem(url="https://pic1.zhimg.com/v2-abc_720w.jpg", kind="image",
                  source="body", is_thumb=True),
        MediaItem(url="https://pic1.zhimg.com/v2-abc_1440w.jpg", kind="image",
                  source="body2", is_thumb=True),
        MediaItem(url="https://pic1.zhimg.com/v2-abc_r.jpg", kind="image",
                  source="thumb-b", is_thumb=True),
        MediaItem(url="https://pic1.zhimg.com/v2-abc.jpg", kind="image",
                  source="orig", is_thumb=False),
    ]
    out = dedupe(raw)
    ck("四档同图 → 只留一条", len(out) == 1, [(i.url, i.source) for i in out])
    ck("留下的是原图（is_thumb=False）", out and out[0].is_thumb is False, out[0].is_thumb if out else None)
    ck("来源信息合并进同一条", out and "body" in out[0].source and "orig" in out[0].source,
       out[0].source if out else "")

    two = [
        MediaItem(url="https://pic1.zhimg.com/v2-aaa_720w.jpg", kind="image", source="1"),
        MediaItem(url="https://pic1.zhimg.com/v2-bbb_720w.jpg", kind="image", source="2"),
    ]
    ck("两张不同的图仍是两条", len(dedupe(two)) == 2, len(dedupe(two)))

    print("\n[视频不归一化：不同清晰度要保留]", flush=True)
    vids = [
        MediaItem(url="https://v.zhimg.com/x_720.mp4", kind="video", source="hd"),
        MediaItem(url="https://v.zhimg.com/x_1080.mp4", kind="video", source="fhd"),
    ]
    ck("两个清晰度都保留", len(dedupe(vids)) == 2, len(dedupe(vids)))
    ck("完全相同的视频地址仍合并",
       len(dedupe([MediaItem(url="https://v.zhimg.com/same.mp4", kind="video", source="a"),
                   MediaItem(url="https://v.zhimg.com/same.mp4", kind="video", source="b")])) == 1)

    print("\n[enrich + dedupe 全链路]", flush=True)
    step = dedupe([
        MediaItem(url="https://pic1.zhimg.com/v2-xyz_720w.jpg", kind="image",
                  source="thumb", is_thumb=True),
    ])
    step = enrich_with_originals(step)
    step = dedupe(step)
    ck("缩略图 + 补出的原图 → 一条", len(step) == 1, [(i.url, i.is_thumb) for i in step])
    ck("全链路后留下的是原图地址",
       step and step[0].url.endswith("v2-xyz.jpg"), step[0].url if step else "")

    bad = [c for c in checks if not c[1]]
    print("\n结果：%d 通过 / %d 失败" % (len(checks) - len(bad), len(bad)), flush=True)
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
