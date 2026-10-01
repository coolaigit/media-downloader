# -*- coding: utf-8 -*-
"""下载去重台账：记住「下过哪些视频」，与文件放在哪儿无关。

## 为什么需要它（用户的真实问题）

用户会反复搜同一个关键词（如「张梓琳」），每次挑不同的视频下载。下次再搜时，
已经下过的那些**不该再出现在结果里**。要做到这一点，就必须有一个跨会话、
跨文件位置的「下过什么」记录。

## 关键设计：按「视频身份」记账，不按「文件路径」记账

项目里原先只有**文件级**跳过（`downloader.download_file` 的 `skip_existing`）：
它在下载目录里放一个标记文件、记下文件名，下次先看「标记 + 文件还在不在」。
这套逻辑适合「别把同一个文件下两遍」，但它**依赖文件位置** ——
用户把文件挪到别处（或删掉），标记就失效，会被当成没下过。

两套机制解决的是两个不同问题，因此**并存**、语义分开：

| 机制 | 回答的问题 | 依据 | 移动文件后 |
|---|---|---|---|
| `skip_existing` 标记 | 这个**文件**还在下载目录里吗？ | 标记文件 + 文件存在性 | **失效**（会重新下载） |
| 本模块的台账 | 这个**视频**我以前下过吗？ | 视频身份（页面地址 / extractor+id） | **不受影响** |

## 身份怎么算（保持通用，不写站点专用分支）

1. **yt-dlp 下到的**：`yt:<extractor>:<id>`（如 `yt:youtube:dQw4w9WgXcQ`）。
   这是最稳的身份 —— 与 URL 上的跟踪参数、清晰度档位都无关。
2. **页面地址**：域名去 `www` + 路径去末尾斜杠 + **只保留「像内容 ID」的查询参数**。
   - 必须保留查询串里的 ID：不少站点的视频 ID 就在查询串里（`watch?v=xxx`），
     全丢掉会把不同视频并成同一条。
   - 必须丢掉跟踪参数：`utm_*` / `t` / `start` / `si` / `feature` / `fbclid` 等，
     否则同一条视频换个来源就会算成新的。
3. 两条都记。搜索时用同样的算法算一遍，命中任意一条即判「下过」。

## 存储

`data/download_history.json`（纯 JSON，原子写入）。
**不放下载目录里**：下载目录可能被用户整个搬走或清空，台账要独立存活。
"""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlparse

from .config import DATA_ROOT

# 台账文件（独立于下载目录，落在 DATA_ROOT）
DATA_DIR = DATA_ROOT / "data"
HISTORY_FILE = DATA_DIR / "download_history.json"
VERSION = 1

_LOCK = threading.RLock()

# 内存缓存：每次搜索结果过滤都要读一遍台账，而批量下载会频繁写入。
# 不做缓存的话「读整个 JSON + 写整个 JSON」会随记录数增长越来越慢。
# 用**文件 mtime** 判断是否需要重读 —— 服务与诊断脚本可能同时访问，
# 不能只靠「本进程写过」来判断。
_CACHE: dict = {"mtime": None, "data": None}

# 跟踪类查询参数：这些与「是哪条视频」无关，必须丢掉，否则同一视频的
# 不同分享链接会被算成两条。用前缀/全名匹配。
_TRACKING_PARAMS = {
    "t", "start", "end", "si", "feature", "fbclid", "gclid", "spm", "from",
    "share_source", "share_medium", "share_plat", "share_tag", "timestamp",
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "refer", "referer", "ref", "source", "src", "vd_source", "spm_id_from",
    "bbid", "ts", "unique_k", "from_source", "wxfid", "scene", "clicktime",
}
_TRACKING_PREFIXES = ("utm_", "share_", "spm", "vd_", "_", "pk_")

# 「像内容 ID」的查询参数值：纯数字（≥5 位）、BV 号、长字母数字串、UUID
_ID_VALUE_RE = re.compile(
    r"^(?:"
    r"BV[0-9A-Za-z]{8,}"                      # B 站
    r"|[0-9]{5,}"                             # 抖音 / 腾讯 / vimeo 等
    r"|[0-9A-Za-z_-]{10,}"                    # 通用长 ID
    r"|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
    r")$"
)


def _is_tracking(name: str) -> bool:
    n = (name or "").lower()
    if n in _TRACKING_PARAMS:
        return True
    return any(n.startswith(p) for p in _TRACKING_PREFIXES)


def url_identity(url: str) -> str:
    """把视频页面地址归一成稳定身份串（域名 + 路径 + 有意义的内容 ID 参数）。

    故意**不做**任何站点分支：判断依据是「参数名像不像跟踪参数」和
    「参数值像不像内容 ID」，对所有站点一视同仁。
    """
    raw = (url or "").strip()
    if not raw:
        return ""
    try:
        p = urlparse(raw)
    except ValueError:
        return ""
    host = (p.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = (p.path or "").rstrip("/").lower()

    keep: list[tuple[str, str]] = []
    for name, value in parse_qsl(p.query or "", keep_blank_values=False):
        if _is_tracking(name):
            continue
        if _ID_VALUE_RE.match(value or ""):
            keep.append((name.lower(), value))
    keep.sort()

    ident = host + path
    if keep:
        ident += "?" + "&".join(f"{k}={v}" for k, v in keep)
    return ident


def video_keys(url: str = "", info: dict | None = None) -> list[str]:
    """算出一条视频的全部候选身份键（去重、保序）。

    `info` 传 yt-dlp 的 info 字典时额外产出最稳的 `yt:<extractor>:<id>` 键。
    """
    keys: list[str] = []

    if isinstance(info, dict):
        extractor = str(info.get("extractor_key") or info.get("extractor") or "").lower()
        vid = str(info.get("id") or "").strip()
        if extractor and vid:
            keys.append(f"yt:{extractor}:{vid.lower()}")

    ident = url_identity(url)
    if ident:
        keys.append("url:" + ident)

    # 去重保序
    seen: set[str] = set()
    out: list[str] = []
    for k in keys:
        if k and k not in seen:
            seen.add(k)
            out.append(k)
    return out


def _mtime() -> int | None:
    try:
        return HISTORY_FILE.stat().st_mtime_ns
    except OSError:
        return None


def _load() -> dict:
    m = _mtime()
    with _LOCK:
        if _CACHE["data"] is not None and _CACHE["mtime"] == m:
            return _CACHE["data"]
    if not HISTORY_FILE.exists():
        data = {"version": VERSION, "records": {}}
    else:
        try:
            raw = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
            data = raw if (isinstance(raw, dict) and isinstance(raw.get("records"), dict)) \
                else {"version": VERSION, "records": {}}
        except (json.JSONDecodeError, OSError):
            data = {"version": VERSION, "records": {}}
    with _LOCK:
        _CACHE["mtime"] = m
        _CACHE["data"] = data
    return data


def _save(data: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = HISTORY_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(HISTORY_FILE)      # 原子替换：写一半被打断也不会毁掉台账
    with _LOCK:
        _CACHE["mtime"] = _mtime()
        _CACHE["data"] = data


def reset_cache() -> None:
    """清掉内存缓存（测试或外部改过文件后用）。"""
    with _LOCK:
        _CACHE["mtime"] = None
        _CACHE["data"] = None


def record(url: str = "", info: dict | None = None, *, title: str = "",
           kind: str = "video", path: str = "", size: int = 0) -> list[str]:
    """记下「这个视频下过了」。返回写进去的身份键。

    `path` 只是**备注**（界面上显示「下到哪儿了」），**不参与身份判定** ——
    这正是「移动文件后仍算已下载」的原因。
    """
    keys = video_keys(url, info)
    if not keys:
        return []
    turl = url or str((info or {}).get("webpage_url") or "")
    meta = {
        "ts": time.time(),
        "title": (title or str((info or {}).get("title") or ""))[:200],
        "kind": kind,
        "url": turl[:600],
        "path": str(path or "")[:600],
        "size": int(size or 0),
        "keys": keys,
    }
    with _LOCK:
        data = _load()
        records = data["records"]
        for k in keys:
            records[k] = meta
        _save(data)
    return keys


def is_downloaded(url: str = "", info: dict | None = None) -> bool:
    """这个视频以前下过吗（只看身份，不看文件还在不在）。"""
    if not url and not info:
        return False
    keys = video_keys(url, info)
    if not keys:
        return False
    with _LOCK:
        records = _load()["records"]
    return any(k in records for k in keys)


def downloaded_keys() -> set[str]:
    """全部已下载身份键（搜索时批量过滤用，避免逐条读文件）。"""
    with _LOCK:
        return set(_load()["records"].keys())


def filter_new(items: list[dict], url_field: str = "webpage_url") -> tuple[list[dict], int]:
    """从一批搜索结果里剔除已下载过的，返回 (保留的, 被剔除的条数)。

    与文件位置无关：用户把已下载的视频挪走，这里依然会判为「下过」。
    """
    keys = downloaded_keys()
    if not keys:
        return items, 0
    kept, dropped = [], 0
    for it in items or []:
        u = str((it or {}).get(url_field) or "")
        cand = video_keys(u)
        if cand and any(k in keys for k in cand):
            dropped += 1
            continue
        kept.append(it)
    return kept, dropped


def _unique_records(records: dict) -> list[dict]:
    """把「一条视频记了多个键」的记录收敛成一条。

    ⚠️ 不能用 `id(meta)` 去重：同一个视频的多个键在**写盘再读回**之后会变成
    各自独立的 dict 对象，`id()` 全不相同 —— 实测表现为「下了 1 条，台账却显示
    2 条」（有几个键就算几条）。必须按记录内容本身取签名。
    """
    uniq: dict[tuple, dict] = {}
    for meta in records.values():
        if not isinstance(meta, dict):
            continue
        sig = tuple(sorted(meta.get("keys") or [meta.get("url") or ""]))
        if not sig:
            sig = (str(meta.get("ts") or ""), str(meta.get("path") or ""))
        # 同一个视频的多个键内容一致，取先出现的即可
        uniq.setdefault(sig, meta)
    return list(uniq.values())


def stats() -> dict:
    with _LOCK:
        records = _load()["records"]
    rows = _unique_records(records)
    by_kind: dict[str, int] = {}
    for meta in rows:
        k = str(meta.get("kind") or "unknown")
        by_kind[k] = by_kind.get(k, 0) + 1
    return {
        "keys": len(records),
        "items": len(rows),
        "by_kind": by_kind,
        "file": str(HISTORY_FILE),
        "exists": HISTORY_FILE.exists(),
    }


def recent(limit: int = 50) -> list[dict]:
    """最近下载记录（按时间倒序，同一个视频只出一条）。"""
    with _LOCK:
        records = _load()["records"]
    rows = sorted(_unique_records(records), key=lambda m: -(m.get("ts") or 0))
    out = []
    for m in rows[: max(1, limit)]:
        out.append({
            "title": m.get("title") or "",
            "kind": m.get("kind") or "video",
            "url": m.get("url") or "",
            "path": m.get("path") or "",
            "size": m.get("size") or 0,
            "ts": m.get("ts") or 0,
            "keys": m.get("keys") or [],
        })
    return out


def forget(url: str = "", info: dict | None = None, key: str = "") -> int:
    """忘掉一条（或指定键）下载记录。返回删掉的键数。"""
    targets = {key} if key else set(video_keys(url, info))
    if not targets:
        return 0
    with _LOCK:
        data = _load()
        records = data["records"]
        n = 0
        for t in targets:
            if t in records:
                records.pop(t, None)
                n += 1
        if n:
            _save(data)
    return n


def clear() -> int:
    """清空台账（用户说「重新开始记」）。返回清掉的键数。"""
    with _LOCK:
        data = _load()
        n = len(data["records"])
        data["records"] = {}
        _save(data)
    return n
