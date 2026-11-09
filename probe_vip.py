# -*- coding: utf-8 -*-
"""会员视频兜底诊断：把一个「只能试看」的页面跑一遍换源链，把每一步摊开。

用法：
    python probe_vip.py "<视频页面地址>"                # 只解析，不下载
    python probe_vip.py "<地址>" --download --limit 60  # 下前 60 秒验证链路

它和网页端走的是同一套函数（app.vip_unlock），所以这里通 = 界面上也能通。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import vip_unlock  # noqa: E402


def _p(tag: str, data) -> None:
    print(f"\n=== {tag}")
    print(json.dumps(data, ensure_ascii=False, indent=2) if not isinstance(data, str) else data)


def main() -> int:
    ap = argparse.ArgumentParser(description="会员视频换源诊断")
    ap.add_argument("url", help="视频页面地址（B 站/优酷/爱奇艺/腾讯视频…）")
    ap.add_argument("--download", action="store_true", help="解析成功后真的下载")
    ap.add_argument("--limit", type=int, default=0, help="只下前 N 秒（0=完整）")
    ap.add_argument("--out", default="_vip_probe", help="下载目录（默认 _vip_probe）")
    args = ap.parse_args()

    t0 = time.time()
    meta = vip_unlock.extract_meta(args.url)
    _p("① 页面元数据", {k: meta.get(k) for k in
                       ("platform", "title", "episode", "episode_title", "year",
                        "duration", "kind", "error")})
    platform = vip_unlock.platform_of(args.url)
    if not platform:
        print("\n[提示] 这个域名不在会员平台清单里，仍会尝试换源，但通常用不上。")

    res = vip_unlock.resolve(args.url, meta)
    _p("② 兜底解析", {k: v for k, v in res.items() if k not in ("meta", "urls")})
    for i, s in enumerate(res.get("urls") or [], 1):
        print(f"  源{i}: [{s.get('site')}] {s.get('url')}")

    if not res.get("ok"):
        print("\n结论：没有拿到完整版播放地址。")
        print("可能原因：资源站没有这部片 / 标题匹配不上 / 平台 Cookie 也没有权限。")
        print("可操作的下一步：")
        print("  1) 把片名手动简化后重试（去掉「第X集」「完整版」等后缀）")
        print("    python probe_vip.py \"<同一地址>\"  # 或在设置页填资源站白名单再试")
        print("  2) 把大会员 Cookie 放进 vip_cookies 目录后重试")
        return 1

    stream = (res.get("urls") or [{}])[0].get("url") or res.get("url")
    from app.stream_downloader import probe_stream
    info = probe_stream(stream or "", referer=res.get("referer") or "")
    print(f"\n=== ③ 流探测\n  kind={info.kind} duration={info.duration:.1f}s "
          f"segments={len(info.segments)} variants={len(info.variants)} error={info.error}")
    if res.get("duration"):
        ok = vip_unlock.detect_preview(float(meta.get("duration") or 0), info.duration)
        print(f"  与标称时长比对：{'像试看' if ok else '完整'}")

    if args.download:
        out = Path(args.out).resolve()
        out.mkdir(parents=True, exist_ok=True)
        if args.limit and info.kind in ("hls-media", "hls-master", "dash"):
            # 限量下载：只验证链路能出片，不跑完整条
            from app import stream_downloader as sd
            print(f"\n=== ④ 限量下载（前 {args.limit}s）")
            target = out / "probe_part.mp4"
            ok, err, produced = sd._ffmpeg_download(  # noqa: SLF001 —— 诊断脚本直接用内部实现
                stream or "", target, res.get("referer") or "", info.duration,
                None, None, out, limit_sec=args.limit)
            size = produced.stat().st_size if ok and produced.exists() else 0
            print(f"  ok={ok} size={size} err={err or '-'}")
            rc = 0 if ok and size > 10240 else 1
        else:
            print("\n=== ④ 完整下载")
            dl = vip_unlock.download_resolved(
                res, out, title=meta.get("title") or "",
                expected_duration=float(meta.get("duration") or 0))
            _p("结果", dl)
            rc = 0 if dl.get("ok") else 1
    else:
        rc = 0

    print(f"\n总耗时 {time.time() - t0:.1f}s")
    return rc


if __name__ == "__main__":
    sys.exit(main())
