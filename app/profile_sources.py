# -*- coding: utf-8 -*-
"""平台主页作品列表：把「抖音 / 小红书 / 快手 / B站 / 微博 / 知乎 的个人主页」交给
本机已安装并登录的 **MediaCrawler**（GitHub: NanmiCoder/MediaCrawler）来展开。

## 为什么不自己写解析（2026-09-23 实测结论）

这些平台的主页接口都要**签名参数 + 登录态**，而且是对抗性的：

| 尝试过的路子 | 实测结果 |
|---|---|
| 我们的渲染引擎渲染主页 HTML | 抖音 793KB 页面里 `aweme_id` **0 个**、`/video/` 链接 **0 个**（作品列表全靠签名接口异步拿）；标题能拿到 |
| 抖音免签名老接口 `iesdouyin.com/web/api/v2/aweme/post/` | HTTP 200 但**响应体 0 字节**（已停用） |
| 抖音 web 接口 `/aweme/v1/web/aweme/post/` | **403 `Blocked by ArgusSecurityPlugin Uifid Not Found`** |
| 抖音分享页 `iesdouyin.com/share/user/<sec_uid>` | 返回 72KB **混淆 JS 反爬盾**页，无数据 |
| MediaCrawler creator 模式（旧版 d6f7c5b） | 同样 403 Uifid Not Found |
| **MediaCrawler 更新到上游 380b426** | ✅ **成功**：抖音主页 35 条作品，每条都带签名直链 `video_download_url` |

所以这里的做法是**升级并使用现成工具**，而不是重写 a_bogus / x-s 签名：
上游 380b426 的提交名就是 `fix(dy): 补上 ArgusSecurityPlugin 要求的 x-tt-argus 请求头`。

## 本模块的职责（**不是**站点解析规则）

1. **发现工具**：定位 MediaCrawler 根目录 / 它的 python / 数据目录（设置 → 环境变量 → 常见路径）
2. **认平台**：URL 宿主 → MediaCrawler 的平台短名（dy/xhs/ks/bili/wb/zhihu）
   —— 这只是「把地址交给哪个现成工具」的映射，不是内容解析规则
3. **查登录态**：browser_data/{platform}_user_data_dir
4. **调用 + **读结果**：<MC>/wrapper.py --type creator ... → 读 JSONL → 统一成项目条目
5. 失败时**如实说明**（没装 / 未登录 / 被风控 / 超时），并给出下一步怎么做
"""
from __future__ import annotations

import glob
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

import requests

from .config import DATA_ROOT, FROZEN, RESOURCE_ROOT
from . import subproc

# URL 宿主 → MediaCrawler 平台短名（现成工具的平台键，不是解析规则）
_HOST_TO_PLATFORM = {
    "douyin.com": "dy",
    "iesdouyin.com": "dy",
    "xiaohongshu.com": "xhs",
    "xhslink.com": "xhs",
    "kuaishou.com": "ks",
    "bilibili.com": "bili",
    "weibo.com": "wb",
    "weibo.cn": "wb",
    "zhihu.com": "zhihu",
    "pinterest.com": "pin",
    "instagram.com": "ig",
}
PLATFORM_NAMES = {"dy": "抖音", "xhs": "小红书", "ks": "快手",
                  "bili": "B站", "wb": "微博", "zhihu": "知乎",
                  "pin": "Pinterest", "ig": "Instagram"}
# 平台短名 → store 层写出的数据目录名
_PLATFORM_DIR = {"dy": "douyin", "xhs": "xhs", "ks": "kuaishou",
                 "bili": "bilibili", "wb": "weibo", "zhihu": "zhihu"}

_search_cache: tuple[float, dict] | None = None
SEARCH_TTL = 60.0

# MediaCrawler 用的是**独占的**浏览器 profile 目录（browser_data/<platform>_user_data_dir），
# 同时跑两个实例会直接崩：实测（2026-09-23）并发时的报错是
#   BrowserType.launch_persistent_context: Target page, context or browser has been closed
# 外加 httpx.ReadTimeout —— 于是「明明能抓的平台突然什么都抓不到」。
# 所以这里必须串行：进程内用锁，跨进程再用一个带时间戳的锁文件兜底。
_run_lock = threading.Lock()
_LOCK_FILE = DATA_ROOT / "data" / "mediacrawler.lock"
_LOCK_STALE_S = 20 * 60
_lock_local = threading.local()


def _pid_alive(pid: int) -> bool:
    """进程还活着吗（Windows 用 OpenProcess，不引入 psutil）。

    为什么需要：抓取被 Ctrl-C / 超时杀掉时，锁文件会留在磁盘上，而旧逻辑要等
    20 分钟才算「陈旧」—— 这 20 分钟里所有抓取都被拒（实测本轮连续两次被自己的
    僵尸锁挡住，白等 300 秒）。这里直接用「进程是否还在」判断，僵尸锁立刻可抢。
    """
    if not pid or pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    try:
        import ctypes
        from ctypes import wintypes
        SYNCHRONIZE = 0x00100000
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(SYNCHRONIZE, False, int(pid))
        if not h:
            return False
        try:
            # 0 超时：WAIT_TIMEOUT(258) = 还在跑；WAIT_OBJECT_0(0) = 已退出
            return k32.WaitForSingleObject(h, 0) == 0x00000102
        finally:
            k32.CloseHandle(h)
    except Exception:  # noqa: BLE001
        return True          # 判断不了就当它活着（保守）


def acquire_run_slot(timeout_s: float = 600.0) -> tuple[bool, str]:
    """拿到「跑一次 MediaCrawler」的名额（串行）。返回 (ok, 说明)。"""
    got = _run_lock.acquire(timeout=timeout_s)
    if not got:
        return False, "另一个抓取任务正在使用外部工具，请稍后再试。"
    _lock_local.held = True
    try:
        _LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
        if _LOCK_FILE.exists():
            try:
                info = json.loads(_LOCK_FILE.read_text(encoding="utf-8"))
                age = time.time() - float(info.get("ts") or 0)
            except Exception:  # noqa: BLE001
                info, age = {}, _LOCK_STALE_S + 1
            other = info.get("pid")
            if other and other != os.getpid():
                if _pid_alive(other) and age < _LOCK_STALE_S:
                    # 跨进程占用（比如另一条 import 了本模块的进程/脚本）
                    _release_run_slot()
                    return False, (f"外部工具正被另一个进程占用（pid={other}，"
                                   f"{int(age)} 秒前开始）。请等它跑完再试。")
                # 进程已经不在（被 Ctrl-C / 超时杀掉）→ 僵尸锁，直接抢过来
                _LOCK_FILE.unlink(missing_ok=True)
        _LOCK_FILE.write_text(json.dumps({"pid": os.getpid(), "ts": time.time()}),
                              encoding="utf-8")
    except OSError:
        pass
    return True, ""


def _release_run_slot() -> None:
    if getattr(_lock_local, "held", False):
        _lock_local.held = False
        try:
            _LOCK_FILE.unlink()
        except OSError:
            pass
        try:
            _run_lock.release()
        except RuntimeError:
            pass


def release_run_slot() -> None:
    _release_run_slot()


def platform_for_url(url: str) -> str:
    """URL 属于哪个平台（不认识返回空串）。只按域名后缀匹配，避免 notyoutube.com 这类误判。"""
    try:
        host = (urlparse(url or "").hostname or "").lower()
    except ValueError:
        return ""
    if host.startswith("www."):
        host = host[4:]
    for dom, key in _HOST_TO_PLATFORM.items():
        if host == dom or host.endswith("." + dom):
            return key
    return ""


_SKIP_DIRS = {
    "node_modules", ".git", ".venv", "venv", "site-packages", "__pycache__",
    "windows", "program files", "program files (x86)", "programdata", "appdata",
    "$recycle.bin", "system volume information", "miniconda3", "anaconda3",
    "playwright-browsers", "ms-playwright", "cache", "temp", "tmp",
}


def _scan_for_tool(root: Path, max_depth: int = 3, max_dirs: int = 4000,
                   budget_s: float = 2.5) -> list[Path]:
    """在 root 下**有限深度**地找名为 MediaCrawler 的目录。

    ⚠️ 血泪教训（2026-09-23）：第一版用 `root.glob("**/MediaCrawler")` 扫 E:/ 和 D:/，
    直接把进程卡死（用户等了两分钟没反应）。全盘递归搜索绝不能出现在请求路径上 ——
    现在限制深度 + 跳过常见大目录 + 目录数上限 + 时间预算。
    """
    found: list[Path] = []
    if not root.exists():
        return found
    t0 = time.time()
    stack: list[tuple[Path, int]] = [(root, 0)]
    seen = 0
    while stack:
        d, depth = stack.pop()
        if time.time() - t0 > budget_s or seen > max_dirs:
            break
        try:
            entries = list(os.scandir(d))
        except OSError:
            continue
        for e in entries:
            seen += 1
            if seen > max_dirs:
                break
            try:
                if not e.is_dir(follow_symlinks=False):
                    continue
            except OSError:
                continue
            name = e.name.lower()
            if name in ("mediacrawler", "mediacrawler-mcp"):
                found.append(Path(e.path))
            if depth + 1 < max_depth and name not in _SKIP_DIRS and not name.startswith("$"):
                stack.append((Path(e.path), depth + 1))
    return found


def _candidates_root() -> list[Path]:
    """可能藏着 MediaCrawler 的目录（按可靠性排序，先便宜后贵）。"""
    out: list[Path] = []
    env = os.environ.get("MEDIACRAWLER_ROOT")
    if env:
        out.append(Path(env))
    try:
        from . import store
        cfg = store.load().get("mediacrawler_root")
        if cfg:
            out.append(Path(str(cfg)))
    except Exception:  # noqa: BLE001
        pass
    here = RESOURCE_ROOT
    out.append(here / "MediaCrawler")
    out.append(here.parent / "MediaCrawler")
    return out


def _discover_root() -> Path | None:
    """先查便宜位置，再在少量根目录里做**有限深度**扫描。"""
    for cand in _candidates_root():
        if _check_root(cand):
            return cand
    roots = [Path("E:/"), Path("D:/"), Path.home() / "Documents",
             RESOURCE_ROOT.parent]
    for root in roots:
        for hit in _scan_for_tool(root):
            if _check_root(hit):
                return hit
    return None


def _check_root(root: Path) -> dict | None:
    """一个目录是不是可用的 MediaCrawler。"""
    if not root or not (root / "main.py").exists():
        return None
    # wrapper.py 由 MCP 维护（无头模式必需）；没有它也能跑 main.py，只是风控更严
    wrapper = root / "wrapper.py"
    py_env = os.environ.get("MEDIACRAWLER_PYTHON")
    # ⚠️ 1C：frozen 下 sys.executable 是 MediaDownloader.exe，拿它跑 MediaCrawler
    # 的 main.py 等于「用自己跑别人的脚本」—— 必须排除，只认外部解释器：
    # MEDIACRAWLER_PYTHON > MediaCrawler 自己的 .venv > （源码模式才允许）当前解释器。
    # 都没有时如实报「未找到」，宁可降级也不伪造（找不到的提示语见 find_mediacrawler）。
    for py in ([Path(py_env)] if py_env else []) + [
            root / ".venv" / "Scripts" / "python.exe",
            root / ".venv" / "bin" / "python",
            *([] if FROZEN else [Path(sys.executable)])]:
        if py and Path(py).exists():
            data_env = os.environ.get("MEDIACRAWLER_DATA_DIR")
            sib = root.parent / "data"
            data_dir = Path(data_env) if data_env else (sib if sib.exists() else root / "data")
            return {"root": root, "python": Path(py), "data_dir": data_dir,
                    "wrapper": wrapper if wrapper.exists() else None}
    return None


def invalidate_cache() -> None:
    """设置里改了 mediacrawler_root 后调用：下一次查找立即重扫，不等 SEARCH_TTL。"""
    global _search_cache
    _search_cache = None


def find_mediacrawler(force: bool = False) -> dict:
    """定位 MediaCrawler（带缓存）。返回 {found, root, python, data_dir, wrapper, reason}。"""
    global _search_cache
    if not force and _search_cache and time.time() - _search_cache[0] < SEARCH_TTL:
        return _search_cache[1]
    info: dict = {"found": False, "root": None, "python": None, "data_dir": None,
                  "wrapper": None, "reason": ""}
    root = _discover_root()
    ok = _check_root(root) if root else None
    if ok:
        # 统一转成字符串：这个 dict 会进 /api/status 之类的 JSON 响应，
        # Path 对象不可序列化（实测踩过：诊断脚本 json.dumps 直接抛 TypeError）
        info.update({k: (str(v) if v is not None else None) for k, v in ok.items()})
        info["found"] = True
    else:
        info["reason"] = ("未找到 MediaCrawler（需要它的 main.py 与虚拟环境）。"
                          "安装：git clone https://github.com/NanmiCoder/MediaCrawler，"
                          "装好依赖后在本工具的「设置」里填它的根目录（mediacrawler_root）。")
    _search_cache = (time.time(), info)
    return info


def login_cached(platform: str) -> bool:
    """该平台是否已有登录态缓存（browser_data/{platform}_user_data_dir）。"""
    info = find_mediacrawler()
    if not info["found"]:
        return False
    d = Path(info["root"]) / "browser_data" / f"{platform}_user_data_dir"
    try:
        return d.exists() and any(d.iterdir())
    except OSError:
        return False


def status() -> dict:
    """给设置页/接口用：工具是否就绪、各平台登录态。"""
    info = find_mediacrawler()
    plats = {}
    if info["found"]:
        for key, name in PLATFORM_NAMES.items():
            plats[key] = {"name": name, "logged_in": login_cached(key)}
    return {"found": info["found"], "root": str(info["root"] or ""),
            "python": str(info["python"] or ""), "data_dir": str(info["data_dir"] or ""),
            "reason": info["reason"], "platforms": plats}


def login_hint(platform: str) -> str:
    """未登录时给用户的一句可执行指引。"""
    info = find_mediacrawler()
    if not info["found"]:
        return info["reason"]
    name = PLATFORM_NAMES.get(platform, platform)
    return (f"{name}尚未登录。在终端执行一次扫码登录即可（登录态会缓存复用）：\n"
            f'  cd "{info["root"]}"\n'
            f'  "{info["python"]}" main.py --platform {platform} --lt qrcode '
            f'--type search --keywords "测试"')


def _run(argv: list[str], cwd: Path, timeout: int, log_path: Path) -> tuple[int, str]:
    """跑子进程，stdout/stderr 落文件（便于超时后排查），返回 (返回码, 日志尾部)。"""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    # playwright 浏览器路径：子进程不继承时容易找不到浏览器（与 MCP 的处理一致）
    if not env.get("PLAYWRIGHT_BROWSERS_PATH"):
        for p in (r"E:\AI\playwright-browsers",
                  str(Path.home() / "AppData/Local/ms-playwright")):
            if os.path.isdir(p):
                env["PLAYWRIGHT_BROWSERS_PATH"] = p
                break
    with open(log_path, "ab") as f:
        f.write(f"\n[{time.strftime('%H:%M:%S')}] cmd={' '.join(argv[-8:])}\n".encode())
        try:
            from .win_job import run_bound
            # 父死子灭：服务重启/被杀时 MediaCrawler 不留孤儿
            proc = run_bound(argv, timeout=timeout, cwd=str(cwd),
                             stdin=subprocess.DEVNULL, stdout=f, stderr=f,
                             env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except subprocess.TimeoutExpired:
            return -1, f"执行超时（超过 {timeout} 秒）"
        except Exception as e:  # noqa: BLE001
            return -2, f"启动失败：{type(e).__name__}: {e}"
    tail = ""
    try:
        with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
            tail = "\n".join([l for l in f.read().splitlines() if l.strip()][-40:])[-1500:]
    except OSError:
        pass
    return proc.returncode, tail


def _read_jsonl(platform: str, kind: str = "contents", prefer: str = "",
                newer_than: float = 0.0) -> list[dict]:
    """读本次抓取的 JSONL（文件名形如 {type}_{kind}_{日期}.jsonl）。

    两个必须的约束（2026-09-23 实测踩到）：
      prefer —— 只认本次用的类型前缀（creator_ / search_ / detail_）。
        工具把不同类型的输出放在**同一个目录**、文件名前缀不同；
        旧逻辑取「最新修改的那个」，于是失败的 creator 抓取会把上一次 search
        的结果当成「主页作品」返回（假成功，比 0 条更糟）。
      newer_than —— 只认本次开跑之后改动过的文件，彻底排除陈年结果。
    """
    info = find_mediacrawler()
    if not info["found"]:
        return []
    dirs = [Path(info["data_dir"]) / _PLATFORM_DIR.get(platform, platform) / "jsonl",
            Path(info["root"]) / "data" / _PLATFORM_DIR.get(platform, platform) / "jsonl"]
    files: list[str] = []
    for d in dirs:
        files += glob.glob(str(d / f"*_{kind}_*.jsonl"))
    if prefer:
        picked = [f for f in files if os.path.basename(f).startswith(prefer + "_")]
        if picked:
            files = picked
    if newer_than:
        fresh = [f for f in files if os.path.getmtime(f) >= newer_than]
        if fresh:
            files = fresh
        else:
            return []          # 没有本次产生的结果 → 如实返回空，不拿旧的凑数
    if not files:
        return []
    latest = max(files, key=os.path.getmtime)
    rows: list[dict] = []
    with open(latest, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def _clear_jsonl(platform: str) -> None:
    """清掉该平台旧结果，避免把上一次的内容当成这一次的。"""
    info = find_mediacrawler()
    if not info["found"]:
        return
    for d in (Path(info["data_dir"]) / _PLATFORM_DIR.get(platform, platform) / "jsonl",
              Path(info["root"]) / "data" / _PLATFORM_DIR.get(platform, platform) / "jsonl"):
        for p in glob.glob(str(d / "*.jsonl")):
            try:
                os.remove(p)
            except OSError:
                pass


_COLLECTION_SEG_NAMES = frozenset((
    "videos", "shorts", "streams", "space", "profile", "profiles", "posts",
    "media", "channel", "channels", "user", "users", "c", "u", "home", "index",
    "people", "note", "notes", "video", "detail", "index.html", "main",
))


# 作品列表**只存在于签名接口**里的平台（网页抓取必然 0 条或只剩站点自己的宣传片）。
# 实测依据：抖音主页 793KB HTML 里 aweme_id 0 个、/video/ 链接 0 个；
# 快手同类。对它们，外部工具失败时应如实收尾，不要回退网页抓取制造垃圾。
#
# ⚠️ 小红书**不在**这个名单里（2026-09-26 实测更正）：它的主页页面上有 100+ 条
# `/explore/<id>?xsec_token=…` 笔记链接，而且**登录态渲染的笔记页里直接带 mp4 主地址**
# （`masterUrl: …/xxx_259.mp4?sign=…`）。所以小红书走「网页抓取 + 下钻」这条路是**能拿到视频**的，
# 只是前提是渲染用的是一个已登录的配置（browser_user_data_dir）。
_AUTHORITATIVE = frozenset(("dy", "ks", "xhs", "pin", "ig"))


def authoritative(url: str) -> bool:
    """这个平台的「主页作品列表」是否只能靠外部来源（回退网页抓取没有意义）。

    dy/ks：作品列表只在签名接口里，网页抓取全是站点宣传片（实测）。
    pin/ig：未登录就是登录墙、登录后数据也在接口里 —— 我们自己的网页管线
    实测拿不过 gallery-dl（用户的画板明明有视频 pin，网页管线 0 条）。
    """
    return platform_for_url(url) in _AUTHORITATIVE


def source_name(platform: str) -> str:
    """给界面看的来源名（哪个平台用哪个工具）。"""
    return {
        "dy": "MediaCrawler", "ks": "MediaCrawler", "bili": "MediaCrawler",
        "wb": "MediaCrawler", "zhihu": "MediaCrawler",
        "xhs": "xhshow / MediaCrawler",
        "pin": "gallery-dl", "ig": "gallery-dl",
    }.get(platform, "外部来源")


def creator_id_from_url(url: str) -> str:
    """从主页地址里取出「创作者标识」，取不到就返回空串。

    为什么要这一步（2026-09-23 实测）：把**整条 URL** 交给 MediaCrawler 的
    `--creator_id` 时，它内部会先做一次网络请求去解析地址，实测在抖音主页上
    `httpx.ReadTimeout`（跑到 111 秒后整条失败）；而直接给它标识（如抖音的
    sec_uid）秒级成功。这不是站点解析规则，只是「地址里本来就带着标识，别让它再解析一遍」：
    取最后一个像标识的路径段（够长 / 纯数字 / 含大小写混合都算像），
    跳过集合名词段（/user/profile/<id> 取 <id>，/@x/videos 取 @x）。
    """
    try:
        p = urlparse(url or "")
    except ValueError:
        return ""
    segs = [s for s in (p.path or "").split("/") if s]
    # 有些平台把标识放在查询串里（?uid= / ?id=）
    q = dict(kv.split("=", 1) for kv in (p.query or "").split("&") if "=" in kv)
    for k in ("sec_uid", "uid", "user_id", "id", "mid", "creator_id"):
        v = q.get(k)
        if v and len(v) >= 4:
            return v
    for seg in reversed(segs):
        s = seg.strip()
        if not s or s.lower() in _COLLECTION_SEG_NAMES:
            continue
        if s.startswith(("#", "?")):
            continue
        if len(s) >= 6 or s.isdigit():
            return s
    return ""


def normalize_row(platform: str, r: dict) -> dict | None:
    """把外部工具的一行 JSONL 映射成我们的条目（字段名各平台不同，这里统一）。

    实测字段差异：
      抖音   aweme_url + video_download_url
      小红书 note_url  + video_url（多个档位用逗号拼在一个字段里）
    只认一种会让另一个平台永远 0 条 —— 所以两种都认、逗号拆开。
    """
    page = (r.get("aweme_url") or r.get("note_url") or "").strip()
    raw_direct = (r.get("video_download_url") or r.get("video_url") or "").strip()
    alts = [u.strip() for u in raw_direct.split(",") if u.strip()]
    direct = alts[0] if alts else ""
    if not page and not direct:
        return None
    return {
        "page_url": page,
        "direct_url": direct,
        "variants": alts[1:],
        "title": (r.get("title") or r.get("desc") or "").strip()[:150],
        "cover": (r.get("cover_url") or "").strip(),
        "uploader": (r.get("nickname") or "").strip(),
        "site": (urlparse(page).hostname or "").replace("www.", ""),
        "duration": r.get("duration"),
        "stats": {k: r.get(k) for k in ("liked_count", "comment_count",
                                       "collected_count", "share_count")
                  if r.get(k) is not None},
    }


# ---------------------------------------------------------------- 来源二：gallery-dl
# Pinterest / Instagram 交给成熟的开源下载器 gallery-dl（PyPI，长期维护）。
# 它自带：登录态处理、翻页、HLS 视频解析（内部用 yt-dlp 合并音视频）。
# 我们只做元数据模式（--dump-json，不落盘），拿到直链后仍走自己的下载队列。

_VIDEO_EXTS = {"mp4", "m3u8", "mov", "webm", "mkv", "flv"}


def _candidate_cookie_files() -> list[str]:
    """可能装有登录态的 cookies 文件（按优先级）。

    1. 设置里配置的 cookies_file
    2. 登录助手导出的那份（跟登录态 profile 放在一起）
    """
    out: list[str] = []
    # Instagram 使用 MediaDownloader 自己的独立登录态，避免和小红书/MediaCrawler profile 混用。
    site_cookie = DATA_ROOT / "site_profiles" / "cookies_from_profile.txt"
    if site_cookie.exists():
        out.append(str(site_cookie))
    try:
        from . import store
        f = str(store.load().get("cookies_file") or "").strip()
        if f:
            out.append(f)
    except Exception:  # noqa: BLE001
        pass
    try:
        from . import browser_profile
        d = browser_profile.user_data_dir() or browser_profile.default_login_dir()
        cand = str(Path(d).resolve().parent / "cookies_from_profile.txt")
        if cand not in out:
            out.append(cand)
    except Exception:  # noqa: BLE001
        pass
    return [f for f in out if Path(f).expanduser().exists()]


def _cookie_file_has_domain(cookie_file: str, keyword: str) -> bool:
    try:
        txt = Path(cookie_file).read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    return any(keyword in line and not line.startswith("#")
               for line in txt.splitlines())


def gallery_dl_items(url: str, *, limit: int = 60, timeout: int = 300,
                     video_only: bool = False, retries: int = 0,
                     retry_wait: int = 60, on_note=None) -> dict:
    """gallery-dl 入口（带整体重试）。

    为什么要整体重试（2026-09-28）：Instagram 对自动化请求限流很凶
    （web_profile_info 持续 429，时好时坏），单次跑经常空手而归；
    限流窗口通常几分钟内解除，所以失败后等一段时间再整体重跑。
    """
    cookie_file = ""
    for f in _candidate_cookie_files():
        cookie_file = f
        break                      # 有就带上（Pinterest 匿名也能用，带上无害）
    argv_extra = ["-o", "sleep-429=30"]   # 429 时每 30 秒重试，别误报「用户不存在」
    res: dict = {}
    for attempt in range(1, max(1, int(retries)) + 1):
        res = _gallery_dl_once(url, limit=limit, timeout=timeout, video_only=video_only,
                               cookie_file=cookie_file, argv_extra=argv_extra,
                               on_note=on_note)
        if res.get("ok") or res.get("login_required") or attempt >= max(1, int(retries)):
            return res
        if on_note:
            on_note(f"第 {attempt} 次没有拿到条目（多半是限流），{retry_wait} 秒后重试…")
        time.sleep(retry_wait)
    return res


def _gallery_dl_once(url: str, *, limit: int, timeout: int, video_only: bool,
                     cookie_file: str, argv_extra: list, on_note=None) -> dict:
    """用 gallery-dl 的元数据模式（--dump-json，不落盘）拿一批直链条目。

    输出结构：JSON 数组，元素是 [kind, data]：
      kind=2 → 作品元数据 dict（标题等上下文）
      kind=3 → 下载地址字符串
    返回 {"ok", "items", "error", "login_required"}，条目结构与 list_creator_items 一致。
    """
    try:
        import gallery_dl  # noqa: F401
    except ImportError:
        return {"ok": False, "items": [], "login_required": False,
                "error": "本机未安装 gallery-dl（pip install gallery-dl）。"
                         "它负责 Pinterest / Instagram 的批量提取。"}
    # 要视频时必须扫更大的范围：画板/主页的文件是图和视频混排的，
    # 前 N 个文件可能全是图（实测 USKYLEDS/architecture 前 22 个全是图，
    # 视频 pin 混在后面）。只扫 limit 个会得出「没有视频」的错误结论。
    # 要视频时必须扫更大的范围：画板/主页的文件是图和视频混排的，
    # 前 N 个文件可能全是图（实测 USKYLEDS/architecture 前 22 个全是图，
    # 视频 pin 混在后面）。只扫 limit 个会得出「没有视频」的错误结论。
    scan = max(int(limit) * 6, 120) if video_only else max(1, min(int(limit), 200))
    # 1C：经统一 dispatcher 调 gallery-dl（internal 子命令内部就是
    # gallery_dl.main()，CLI 参数/退出码/输出语义 100% 保持）——
    # frozen 下 sys.executable 是本程序 EXE，老的 `-m gallery_dl` 写法不成立
    argv = subproc.build_internal_command("gallery-dl", "--dump-json",
                                          "--range", f"1-{min(scan, 400)}")
    if cookie_file:
        argv += ["--cookies", cookie_file]
    argv += list(argv_extra or [])
    # gallery-dl 1.32.x 对 Instagram 用户根页只返回 kind=6 的队列跳转
    # （<user>/ -> <user>/posts/），DataJob 不会替我们继续展开。只对纯用户主页
    # 补 /posts/，reel/p/tv/stories 等单条链接保持原样。
    run_url = url
    try:
        pu = urlparse(url)
        host = (pu.hostname or "").lower()
        segs = [x for x in (pu.path or "").split("/") if x]
        if (host == "instagram.com" or host.endswith(".instagram.com")) and len(segs) == 1:
            run_url = f"{pu.scheme or 'https'}://{pu.netloc}/{segs[0]}/posts/"
    except Exception:
        run_url = url
    argv.append(run_url)
    if on_note:
        on_note("正在用 gallery-dl 读取该页的作品列表（成熟开源下载器，只取元数据不下载）…")
    try:
        from .win_job import run_bound
        # 父死子灭：同 MediaCrawler
        proc = run_bound(argv, timeout=timeout, stdin=subprocess.DEVNULL,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         cwd=str(RESOURCE_ROOT),
                         env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    except subprocess.TimeoutExpired:
        return {"ok": False, "items": [], "login_required": False,
                "error": f"gallery-dl 超时（{timeout} 秒）"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "items": [], "login_required": False,
                "error": f"{type(e).__name__}: {e}"}
    err = (proc.stderr or b"").decode("utf-8", "replace")
    if proc.returncode != 0 and not proc.stdout:
        low = err.lower()
        login_required = ("login" in low or "auth" in low or "429" in low
                          or "rate" in low or "require" in low)
        first = next((l for l in err.splitlines() if l.strip()), "")[:220]
        return {"ok": False, "items": [], "login_required": login_required,
                "error": first or f"gallery-dl 退出码 {proc.returncode}"}
    try:
        arr = json.loads((proc.stdout or b"").decode("utf-8", "replace"))
    except json.JSONDecodeError:
        return {"ok": False, "items": [], "login_required": False,
                "error": "gallery-dl 输出无法解析" + (f"：{err[:160]}" if err else "")}
    items: list[dict] = []
    ctx: dict = {}
    seen: set[str] = set()
    for pair in arr if isinstance(arr, list) else []:
        if not isinstance(pair, list) or not pair:
            continue
        kind, data = pair[0], pair[1] if len(pair) > 1 else None
        if kind == 2 and isinstance(data, dict):
            # 取作品封面：卡片没有预览图用户会以为「下载的东西是坏的」（实测反馈）。
            # Pinterest 的封面在 images.<尺寸>.url，优先高清档；没有就兜底找
            # 元数据里第一张 i.pinimg.com 的图（story pin 的封面埋得更深）。
            cover = ""
            # Instagram/gallery-dl 常见字段；Pinterest 仍继续走下面原有 images 逻辑。
            for k in ("display_url", "thumbnail", "preview_url", "image_url", "thumb"):
                v = data.get(k)
                if isinstance(v, str) and v.startswith("http"):
                    cover = v
                    break
            imgs = data.get("images")
            if isinstance(imgs, dict):
                for pref in ("orig", "1200x", "736x", "600x", "474x"):
                    v = imgs.get(pref)
                    if isinstance(v, dict) and (v.get("url") or "").startswith("http"):
                        cover = v["url"]
                        break
                if not cover:
                    for v in imgs.values():
                        if isinstance(v, dict) and (v.get("url") or "").startswith("http"):
                            cover = v["url"]
                            break
            if not cover:
                m = re.search(r'https:\\?/\\?/i\.pinimg\.com[^"\\\\ ]+?\.(?:jpg|png|webp)',
                              json.dumps(data, ensure_ascii=False))
                if m:
                    cover = m.group(0).replace("\\/", "/")
            ctx = {"title": (data.get("title") or data.get("grid_title")
                             or data.get("full_name") or data.get("username") or ""),
                   "cover": cover}
        elif kind == 3 and isinstance(data, str):
            meta = pair[2] if len(pair) > 2 and isinstance(pair[2], dict) else {}
            # Instagram 的 video 项是 ytdl:<作品页>/1.mp4：这是 gallery-dl 给 yt-dlp 的
            # 交接标记，不是真实 MP4。必须还原为作品页，让我们自己的 yt-dlp + 登录态处理。
            ytdl = re.match(r"^ytdl:(https?://.+)", data, re.I)
            if ytdl and "instagram.com" in ytdl.group(1).lower():
                page = (meta.get("post_url") or ytdl.group(1)).strip()
                page = re.sub(r"/\d+\.mp4(?:\?.*)?$", "/", page)
                key = "page:" + page
                if page and key not in seen:
                    seen.add(key)
                    items.append({
                        "page_url": page,
                        "direct_url": "",
                        "variants": [],
                        "title": (meta.get("description") or meta.get("fullname") or
                                  ctx.get("title") or "").strip()[:150],
                        "cover": (meta.get("display_url") or ctx.get("cover") or "").strip(),
                        "uploader": (meta.get("username") or "").strip(),
                        "site": "instagram.com",
                        "kind_hint": "video",
                    })
                continue
            # Pinterest 等的 ytdl: 前缀后面是真实 HLS/媒体地址，保留原行为。
            m = re.match(r"^[a-z][a-z0-9_]*:(https?://.+)", data, re.I)
            data = m.group(1) if m else data
            if not data.startswith("http") or data in seen:
                continue
            seen.add(data)
            ext = data.rsplit("?", 1)[0].rsplit(".", 1)[-1].lower()
            kind_i = "video" if ext in _VIDEO_EXTS else "image"
            items.append({
                "page_url": url,
                "direct_url": data,
                "variants": [],
                "title": ctx.get("title") or "",
                "cover": ctx.get("cover") or "",
                "uploader": "",
                "site": (urlparse(url).hostname or "").replace("www.", ""),
                "kind_hint": kind_i,
            })
    if video_only:
        vids = [it for it in items if it.get("kind_hint") == "video"]
        if not vids and not items:
            # 一个文件都没拿到：这不是「没有视频」，而是登录墙 / 风控 / 地址问题
            low = err.lower()
            login_required = ("login" in low or "auth" in low or "429" in low
                              or "rate" in low or "require" in low
                              or "could not be found" in low)
            first = next((l for l in err.splitlines() if l.strip()), "")[:200]
            return {"ok": False, "items": [], "login_required": login_required,
                    "error": first or "该页一个文件都没拿到（可能需要登录或被风控）"}
        if not vids:
            return {"ok": False, "items": [], "login_required": False,
                    "error": (f"该页前 {min(scan, 400)} 个文件里没有视频（图片 {len(items)} 个）。"
                              "视频 pin 可能排得更靠后，或这个页确实没有视频。")}
        return {"ok": True, "items": vids[:limit], "error": "", "login_required": False}
    if not items:
        # 元数据模式 0 条：多半是登录墙 / 风控 / 地址问题，给可操作的错误
        low = err.lower()
        login_required = ("login" in low or "auth" in low or "429" in low
                          or "rate" in low or "require" in low
                          or "could not be found" in low)
        first = next((l for l in err.splitlines() if l.strip()), "")[:200]
        return {"ok": False, "items": [], "login_required": login_required,
                "error": first or "gallery-dl 没有返回任何条目（该页可能为空、需要登录，或地址写错）"}
    return {"ok": bool(items), "items": items[:limit], "error": "",
            "login_required": False}


def _load_cookie_dict(keyword: str) -> tuple[dict, str]:
    """从候选 cookies 文件里取出指定域的 cookie dict（给签名接口用）。"""
    for f in _candidate_cookie_files():
        if not _cookie_file_has_domain(f, keyword):
            continue
        ck: dict = {}
        try:
            for line in Path(f).read_text(encoding="utf-8", errors="ignore").splitlines():
                # ⚠️ `#HttpOnly_` 开头的行是**数据**不是注释 —— 小红书的 web_session
                # 恰好就是 HttpOnly cookie，把它当注释跳过 = 永远「登录态失效」。
                if not line or (line.startswith("#") and not line.startswith("#HttpOnly_")):
                    continue
                parts_ = line.split("\t")
                if len(parts_) < 7 or keyword not in parts_[0]:
                    continue
                ck[parts_[5]] = parts_[6]
        except OSError:
            continue
        if ck:
            return ck, f
    return {}, ""


# ---------------------------------------------------------------- 来源三：xhshow 签名接口
# 小红书「账号作品列表」官方接口需要 x-s / x-t 签名。自己实现签名是打地鼠，
# 这里用开源签名库 xhshow（XHS-Downloader 同款依赖，专为小红书接口签名而生）。

_XHS_USER_API = "https://edith.xiaohongshu.com/api/sns/web/v1/user_posted"


def xhs_author_items(url: str, *, limit: int = 30, timeout: int = 90,
                     on_note=None) -> dict:
    """用签名的官方接口拿小红书账号的作品列表（含每篇笔记的 xsec_token）。

    返回的条目是**笔记页地址**（带令牌），由既有下钻逻辑逐篇打开取视频 ——
    实测带令牌的笔记页纯 HTTP 就能拿到 mp4 主地址（无需渲染）。
    登录要求：接口要 web_session（扫码登录一次）；没有登录态时如实返回 login_required。
    """
    try:
        from xhshow import Xhshow
    except ImportError:
        return {"ok": False, "items": [], "login_required": False,
                "error": "本机未安装 xhshow（pip install xhshow）——"
                         "它负责小红书接口签名。"}
    uid = creator_id_from_url(url)
    if not uid:
        return {"ok": False, "items": [], "login_required": False,
                "error": "没能从地址里识别出作者 ID（应为 /user/profile/<24位ID>）"}
    ck, _ck_file = _load_cookie_dict("xiaohongshu")
    if not ck.get("web_session"):
        return {"ok": False, "items": [], "login_required": True,
                "error": "小红书的作品列表接口要求登录态（web_session）。"
                         "请在「设置」→「打开浏览器去登录」选小红书扫码登录一次，"
                         "登录态会自动保存并长期复用。"}
    if on_note:
        on_note("正在用签名接口读取该账号的作品列表（官方接口 + 开源签名库 xhshow）…")
    params = {"num": 30, "cursor": "", "user_id": uid, "image_formats": "jpg"}
    signer = Xhshow()
    items: list[dict] = []
    cursor = ""
    for _page in range(max(1, (int(limit) + 29) // 30)):
        if cursor:
            params["cursor"] = cursor
        try:
            headers = signer.sign_headers_get(uri=_XHS_USER_API, cookies=ck, params=params)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "items": [], "login_required": False,
                    "error": f"签名失败：{e}"}
        headers.update({
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126.0.0.0 Safari/537.36",
            "accept": "application/json, text/plain, */*",
            "origin": "https://www.xiaohongshu.com",
            "referer": f"https://www.xiaohongshu.com/user/profile/{uid}",
        })
        try:
            # 代理 → 直连互为兜底（本机代理会偶发卡死 / ProxyError，实测
            # edith.xiaohongshu.com 走代理直接 ReadTimeout，直连秒回）。
            resp = None
            last_exc: Exception | None = None
            for _attempt, trust in enumerate((True, False), 1):
                try:
                    with requests.Session() as sess:
                        sess.trust_env = trust
                        sess.headers.update(headers)
                        resp = sess.get(_XHS_USER_API, params=params, cookies=ck,
                                        timeout=min(timeout, 30))
                    break
                except requests.exceptions.RequestException as e:
                    last_exc = e
                    continue
            if resp is None:
                raise last_exc or RuntimeError("请求失败")
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "items": [], "login_required": False,
                    "error": f"{type(e).__name__}: {e}（代理与直连都试过了）"}
        try:
            data = resp.json()
        except ValueError:
            return {"ok": False, "items": [], "login_required": False,
                    "error": f"接口返回非 JSON（HTTP {resp.status_code}）：{resp.text[:120]}"}
        if not data.get("success"):
            code = data.get("code")
            msg = (data.get("msg") or "")[:80]
            if code == -101:
                return {"ok": False, "items": [], "login_required": True,
                        "error": f"登录态已失效（{msg}）。请重新扫码登录一次小红书。"}
            return {"ok": False, "items": [], "login_required": False,
                    "error": f"接口报错（code={code}）：{msg}"}
        notes = (data.get("data") or {}).get("notes") or []
        for n in notes:
            nid = n.get("note_id") or ""
            tok = n.get("xsec_token") or ""
            if not nid or not tok:
                continue
            page = (f"https://www.xiaohongshu.com/explore/{nid}"
                    f"?xsec_token={tok}&xsec_source=pc_user")
            cover = n.get("cover") if isinstance(n.get("cover"), dict) else {}
            user = n.get("user") if isinstance(n.get("user"), dict) else {}
            items.append({
                "page_url": page,
                "direct_url": "",
                "variants": [],
                "title": (n.get("display_title") or "").strip()[:120],
                "cover": (cover.get("url_default") or ""),
                "uploader": (user.get("nickname") or ""),
                "site": "xiaohongshu.com",
                "is_page": True,
            })
            if len(items) >= int(limit):
                break
        cursor = (data.get("data") or {}).get("cursor") or ""
        if not cursor or not notes or len(items) >= int(limit):
            break
    if not items:
        return {"ok": False, "items": [], "login_required": False,
                "error": ("接口正常但没有可解析的作品（可能该账号只有图文笔记，"
                          "或作品列表为空）。")}
    return {"ok": True, "items": items[:limit], "error": "", "login_required": False}


def _xhs_signed_detail_item(url: str, *, timeout: int = 60) -> dict:
    """用 xhshow + 已导出的登录 cookie 直接解析一条小红书笔记，不依赖 MediaCrawler。"""
    try:
        from xhshow import Xhshow
        from urllib.parse import parse_qs
    except ImportError:
        return {"ok": False, "item": None, "error": "内置 xhshow 不可用"}

    try:
        pu = urlparse(url)
        segs = [x for x in (pu.path or "").split("/") if x]
        note_id = segs[-1] if segs else ""
        qs = parse_qs(pu.query or "")
        xsec_token = (qs.get("xsec_token") or [""])[0]
        xsec_source = (qs.get("xsec_source") or ["pc_search"])[0] or "pc_search"
    except Exception as e:
        return {"ok": False, "item": None, "error": f"小红书地址解析失败：{e}"}
    if not note_id or not xsec_token:
        return {"ok": False, "item": None,
                "error": "小红书单条链接缺少 xsec_token；请使用浏览器分享/地址栏中的完整链接。"}

    ck, _ = _load_cookie_dict("xiaohongshu")
    if not ck.get("web_session"):
        return {"ok": False, "item": None,
                "error": "小红书需要登录态。请在「设置」→「打开浏览器去登录」登录一次小红书。"}

    endpoint = "https://edith.xiaohongshu.com/api/sns/web/v1/feed"
    payload = {
        "source_note_id": note_id,
        "image_formats": ["jpg", "webp", "avif"],
        "extra": {"need_body_topic": 1},
        "xsec_source": xsec_source,
        "xsec_token": xsec_token,
    }
    try:
        headers = Xhshow().sign_headers_post(uri=endpoint, cookies=ck, payload=payload, x_rap=True)
        headers.update({
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126.0.0.0 Safari/537.36",
            "accept": "application/json, text/plain, */*",
            "origin": "https://www.xiaohongshu.com",
            "referer": url,
        })
        resp = None
        last_exc = None
        for trust in (True, False):
            try:
                with requests.Session() as sess:
                    sess.trust_env = trust
                    resp = sess.post(endpoint, json=payload, cookies=ck, headers=headers,
                                     timeout=min(int(timeout), 30))
                break
            except requests.exceptions.RequestException as e:
                last_exc = e
        if resp is None:
            raise last_exc or RuntimeError("请求失败")
        data = resp.json()
    except Exception as e:
        return {"ok": False, "item": None, "error": f"小红书详情接口失败：{type(e).__name__}: {e}"}

    if not data.get("success"):
        code = data.get("code")
        msg = (data.get("msg") or "")[:100]
        return {"ok": False, "item": None,
                "error": f"小红书详情接口报错（code={code}）：{msg}"}

    entries = (data.get("data") or {}).get("items") or []
    card = (entries[0].get("note_card") if entries and isinstance(entries[0], dict) else None) or {}
    if card.get("type") != "video":
        return {"ok": False, "item": None, "error": "这条小红书笔记不是视频笔记。"}

    urls = []
    video = card.get("video") if isinstance(card.get("video"), dict) else {}
    media_v2 = video.get("media_v2")
    try:
        mv = json.loads(media_v2) if isinstance(media_v2, str) else (media_v2 or {})
    except (TypeError, ValueError):
        mv = {}
    stream = ((mv.get("video") or {}).get("stream") or {}) if isinstance(mv, dict) else {}
    for codec_group in stream.values() if isinstance(stream, dict) else []:
        if not isinstance(codec_group, list):
            continue
        for fmt in codec_group:
            if not isinstance(fmt, dict):
                continue
            u = (fmt.get("master_url") or "").strip()
            if u and u not in urls:
                urls.append(u)
            for bu in fmt.get("backup_urls") or []:
                if isinstance(bu, str) and bu.startswith("http") and bu not in urls:
                    urls.append(bu)
    # 老结构兜底：递归找 master_url。
    if not urls:
        def walk(obj):
            if isinstance(obj, dict):
                u = obj.get("master_url")
                if isinstance(u, str) and u.startswith("http") and u not in urls:
                    urls.append(u)
                for v in obj.values():
                    walk(v)
            elif isinstance(obj, list):
                for v in obj:
                    walk(v)
        walk(video)

    if not urls:
        return {"ok": False, "item": None, "error": "详情接口成功，但没有找到视频流地址。"}

    cover = ""
    images = card.get("image_list") or []
    for im in images if isinstance(images, list) else []:
        if not isinstance(im, dict):
            continue
        for k in ("url_default", "url_pre", "url"):
            v = im.get(k)
            if isinstance(v, str) and v.startswith("http"):
                cover = v
                break
        if cover:
            break
    user = card.get("user") if isinstance(card.get("user"), dict) else {}
    item = {
        "page_url": url,
        "direct_url": urls[0],
        "variants": urls[1:],
        "title": (card.get("title") or card.get("desc") or "").strip()[:150],
        "cover": cover,
        "uploader": (user.get("nickname") or user.get("nick_name") or "").strip(),
        "site": "xiaohongshu.com",
        "duration": ((video.get("capa") or {}).get("duration")
                     if isinstance(video.get("capa"), dict) else None),
    }
    return {"ok": True, "item": item, "error": "", "source": "xhshow"}


def xhs_detail_item(url: str, *, timeout: int = 300, on_note=None) -> dict:
    """解析一条小红书笔记：内置 xhshow 优先，外部 MediaCrawler 仅作备用。"""
    direct = _xhs_signed_detail_item(url, timeout=min(timeout, 60))
    if direct.get("ok"):
        return direct

    info = find_mediacrawler()
    if not info["found"] or not login_cached("xhs"):
        return direct
    ok_slot, why = acquire_run_slot()
    if not ok_slot:
        return direct if direct.get("error") else {"ok": False, "item": None, "error": why}
    log_path = Path(info["data_dir"]) / "crawler_run.log"
    _clear_jsonl("xhs")
    argv = [str(info["python"]),
            str(info["wrapper"] or (Path(info["root"]) / "main.py")),
            "--platform", "xhs", "--lt", "qrcode",
            "--type", "detail", "--specified_id", url,
            "--save_data_option", "jsonl", "--save_data_path", str(info["data_dir"]),
            "--get_comment", "no", "--get_media", "no", "--headless", "yes"]
    t_start = time.time()
    try:
        if on_note:
            on_note("内置接口未成功，正在回退到已登录的 MediaCrawler…")
        rc, tail = _run(argv, Path(info["root"]), timeout, log_path)
        rows = _read_jsonl("xhs", "contents", prefer="detail", newer_than=t_start - 2)
    finally:
        release_run_slot()
    for row in rows:
        got = normalize_row("xhs", row)
        if got and got.get("direct_url"):
            return {"ok": True, "item": got, "error": "", "rc": rc, "source": "MediaCrawler"}
    why2 = tail or f"MediaCrawler 未返回可用视频（返回码 {rc}）"
    return {"ok": False, "item": None,
            "error": (direct.get("error") or "") + "；MediaCrawler 回退也失败：" + why2[-400:]}


# ---------------------------------------------------------------- 统一入口：按平台挑来源

def list_author_items(url: str, *, limit: int = 30, want_video: bool = False,
                      on_note=None) -> tuple[list[dict], str]:
    """主页作品列表的统一入口：按平台挑最可靠的来源，失败时给可操作的原因。

    来源优先级（都实测过）：
      抖音/快手/B站/微博/知乎 → MediaCrawler（本工具已集成、已登录）
      小红书                  → xhshow 签名接口（MediaCrawler 的 xhs 主页解析已坏）
      Pinterest / Instagram   → gallery-dl（登录态走 cookies，Pinterest 匿名也可）
    返回 (条目, 说明)。条目统一结构：page_url / direct_url / variants / title / cover。
    """
    platform = platform_for_url(url)
    if not platform:
        return [], ""
    name = PLATFORM_NAMES.get(platform, platform)

    if platform == "xhs":
        # 批量优先走轻量 xhshow（直接签名接口）；失败时才回退到已登录 MediaCrawler。
        res = xhs_author_items(url, limit=limit, on_note=on_note)
        if not res.get("ok") and find_mediacrawler()["found"] and login_cached("xhs"):
            mc_items, mc_note = list_creator_items(url, limit=limit, on_note=on_note)
            if mc_items:
                return mc_items, ("xhshow 未成功，已回退 MediaCrawler。" +
                                  (f" {mc_note}" if mc_note else ""))
    elif platform in ("pin", "ig"):
        res = gallery_dl_items(url, limit=limit, video_only=want_video,
                               retries=3 if platform == "ig" else 1,
                               retry_wait=60, on_note=on_note)
        # Instagram 对未登录访问要么 404 要么空数据 —— 拿不到任何文件时按「需要登录」处理，
        # 否则用户会看到「没有视频」这种误导性结论。
        if (platform == "ig" and not res.get("ok")
                and not res.get("items") and not res.get("login_required")):
            res["login_required"] = True
        if not res.get("ok") and res.get("login_required"):
            res["error"] = (
                f"{name} 对未登录访问只返回登录墙。请在「设置」→「打开浏览器去登录」"
                f"选 {name} 登录一次（登录态会自动保存并导出 cookies），之后长期复用。")
    else:
        # MediaCrawler 系
        if not find_mediacrawler()["found"]:
            return [], find_mediacrawler()["reason"]
        if not login_cached(platform):
            return [], login_hint(platform)
        return list_creator_items(url, limit=limit, on_note=on_note)

    if res.get("ok"):
        return res["items"], res.get("error") or ""
    why = res.get("error") or "未知原因"
    if res.get("login_required"):
        return [], why
    # 失败时带上来源名，方便用户判断是哪条路出的问题
    return [], f"{name} 主页展开失败：{why}"


def list_creator_items(
    url: str,
    *,
    limit: int = 30,
    timeout: int = 600,
    on_note=None,
) -> tuple[list[dict], str]:
    """展开某个平台主页的作品列表，返回 (条目, 错误说明)。

    条目统一结构（供 batch_crawler 直接转成项目里的媒体条目）：
      {page_url, direct_url, title, cover, uploader, site, duration, stats}
    `direct_url` 是工具给出的**带签名直链**（会过期，立即下载即可）；
    没有它时退回 `page_url`（页面地址，交给 yt-dlp）。
    """
    platform = platform_for_url(url)
    if not platform:
        return [], ""
    info = find_mediacrawler()
    if not info["found"]:
        return [], info["reason"]
    if not login_cached(platform):
        return [], login_hint(platform)

    ok_slot, why = acquire_run_slot()
    if not ok_slot:
        return [], why
    log_path = Path(info["data_dir"]) / "crawler_run.log"
    _clear_jsonl(platform)
    if on_note:
        on_note(f"正在用外部工具（MediaCrawler）展开"
                f"{PLATFORM_NAMES.get(platform, platform)}主页作品，需要登录态…")
    argv = [str(info["python"]),
            str(info["wrapper"] or (Path(info["root"]) / "main.py")),
            "--platform", platform,
            "--lt", "qrcode",                 # 有登录态缓存时自动跳过扫码
            "--type", "creator",
            # 优先给「标识」而不是整条 URL：给 URL 时工具会自己再发一次请求解析，
            # 实测抖音上会 httpx.ReadTimeout 把整次抓取拖死（111 秒后 0 条）。
            # ⚠️ 例外：地址里带风控令牌（如小红书 xsec_token）时必须把**整条 URL** 给它 ——
            # 令牌就是它解析主页的凭据，剥掉就只剩裸 id，工具会报
            # `Failed to parse creator URL: Expecting value`。
            "--creator_id", (url if ("xsec_token=" in url) else (creator_id_from_url(url) or url)),
            "--crawler_max_notes_count", str(max(1, int(limit))),
            *(["--get_comment", "no"] if platform == "xhs" else []),
            "--save_data_option", "jsonl",
            "--save_data_path", str(info["data_dir"]),
            # ⚠️ 这两个参数**不能省**（2026-09-23 实测）：
            # 不传 --get_media no 时，工具会自己去下载每条作品的媒体文件，
            # 经过本机代理时 httpx 读超时，整次抓取以 ReadTimeout 失败、0 条结果；
            # 不传 --headless yes 时，风控更严且行为不稳定。
            # 媒体下载由我们自己的下载队列负责，这里只要元数据。
            "--get_media", "no",
            "--headless", "yes"]
    t_start = time.time()
    try:
        rc, tail = _run(argv, Path(info["root"]), timeout, log_path)
        rows = _read_jsonl(platform, "contents", prefer="creator",
                           newer_than=t_start - 2)
    finally:
        release_run_slot()

    items: list[dict] = []
    for r in rows:
        got = normalize_row(platform, r)
        if got:
            items.append(got)

    if items:
        note = ""
        if rc != 0:
            # 实测：抖音 creator 跑到一半浏览器页面被关闭（TargetClosedError），
            # 但已抓到的部分已经落盘 —— 这种情况必须说明是「部分结果」，不能假装完整。
            note = (f"外部工具中途退出（返回码 {rc}），以下 {len(items)} 条是**已抓到的部分**。"
                    f"日志尾部：{tail[-200:] if tail else '（无）'}")
        return items, note
    # 没结果：把日志尾部带出去，让用户能判断是风控、未登录还是别的
    err = "外部工具没有返回作品列表。"
    if rc == -1:
        err = tail or "执行超时"
    elif tail:
        low = tail.lower()
        if "uifid" in low or "argus" in low:
            err += "疑似撞上平台风控（uifid/argus）。可把 MediaCrawler 更新到最新版后重试。"
        elif "login" in low or "登录" in tail:
            err += "疑似登录态失效，需要重新扫码登录。"
        elif "parse creator url" in low or "expecting value" in low:
            # 小红书专属（工具自己的输入要求，不是我们编的规则）：
            # 它的 author 解析需要主页地址里的 xsec_token；只给裸 user_id 会解析失败
            # （实测报 "Failed to parse creator URL: Expecting value"）。
            if platform == "xhs":
                err += ("小红书的主页地址需要带 xsec_token：在浏览器里登录小红书、点进该主页，"
                        "把地址栏的整条链接（含 ?xsec_token=…）复制过来粘贴抓取即可。")
            else:
                err += "外部工具解析这个主页地址失败，请换用浏览器地址栏里的完整链接重试。"
        err += f" 日志尾部：{tail[-300:]}"
    return [], err
