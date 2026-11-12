# -*- coding: utf-8 -*-
"""F-25 离线用例：主下载/解析链路的 download_proxy 代理注入。

冻结 2026-11-12：设置里的 download_proxy 此前只有 vip 兜底通道在用，
主链路（解析/下载）全程直连，国内网络直连 YouTube/TikTok/IG 必然失败。
后续重构不得改动 _overseas_proxy 的平台集合与空值语义。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.main import _overseas_proxy, _OVERSEAS_PLATFORMS  # noqa: E402
from app.task_manager import TaskManager  # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}")


ST = {"download_proxy": "http://127.0.0.1:7890"}

# ---- 平台集合 ----
check("海外集合恰好是 yt/tiktok/ig/pin",
      _OVERSEAS_PLATFORMS == {"yt", "tiktok", "ig", "pin"})

# ---- _overseas_proxy：海外注入 / 国内直连 / 未配代理直连 ----
cases = [
    ("https://www.youtube.com/watch?v=jNQXAC9IVRw", ST, "http://127.0.0.1:7890"),
    ("https://youtu.be/jNQXAC9IVRw", ST, "http://127.0.0.1:7890"),
    ("https://www.tiktok.com/@u/video/1", ST, "http://127.0.0.1:7890"),
    ("https://www.instagram.com/p/abc/", ST, "http://127.0.0.1:7890"),
    ("https://www.pinterest.com/pin/1/", ST, "http://127.0.0.1:7890"),
    # 国内站：配了代理也直连
    ("https://www.bilibili.com/video/BV1x", ST, ""),
    ("https://www.douyin.com/video/1", ST, ""),
    ("https://www.xiaohongshu.com/explore/1", ST, ""),
    # 海外站但没配代理：直连（不硬造一个死代理）
    ("https://www.youtube.com/watch?v=x", {}, ""),
]
for url, st, exp in cases:
    check(f"_overseas_proxy({url[:44]})", _overseas_proxy(url, st) == exp)

# ---- 空白代理值容忍 ----
check("download_proxy 纯空白视同未配",
      _overseas_proxy("https://www.youtube.com/watch?v=x",
                      {"download_proxy": "   "}) == "")

# ---- submit_video / submit_videos 存储代理 ----
mgr = TaskManager()
tid = mgr.submit_video("https://www.youtube.com/watch?v=x", proxy="http://p:1")
check("submit_video 透传 proxy 进 options",
      mgr.get(tid).options.get("proxy") == "http://p:1")

ids = mgr.submit_videos(
    [{"url": "https://www.youtube.com/watch?v=y", "proxy": "http://p:2"},
     {"url": "https://www.bilibili.com/video/BVx", "proxy": ""}],
    concurrency=1)
check("submit_videos 每条各自带 proxy",
      mgr.get(ids[0]).options.get("proxy") == "http://p:2"
      and mgr.get(ids[1]).options.get("proxy") == "")

# 清理：别把测试任务留在运行队列里（都还没开始跑，直接置取消）
for i in [tid, *ids]:
    mgr.cancel(i)

print(f"结果：{PASS} 通过 / {FAIL} 失败")
sys.exit(1 if FAIL else 0)
