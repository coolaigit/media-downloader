# -*- coding: utf-8 -*-
"""全局配置与共享常量：路径、User-Agent、超时、ffmpeg 定位、目录安全校验。"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

# ---------- 运行环境判定与三大根目录（1A 路径体系） ----------
#
# RESOURCE_ROOT：只读程序资源（web/、bin/、_pylibs/、gallery-dl/），随发行包分发
# DATA_ROOT    ：可写运行数据（config.json、data/、logs/、浏览器 Profile、Cookies）
# DOWNLOADS_DIR：用户下载根目录（默认 %USERPROFILE%\Downloads\MediaDownloader）
#
# 三种运行形态的解析：
#   源码运行        RESOURCE_ROOT = 项目根（app/ 的上一级）
#   PyInstaller     RESOURCE_ROOT = exe 所在目录；打包资源在 _internal/ 下
#   onedir 安装版   同 PyInstaller；数据一律写 DATA_ROOT，严禁写安装目录
#                   （安装到 Program Files 时普通用户无写权限）
#
# 环境变量覆盖（部署/测试用，优先级最高）：
#   MEDIA_DOWNLOADER_DATA_DIR      覆盖 DATA_ROOT
#   MEDIA_DOWNLOADER_DOWNLOAD_DIR  覆盖下载根目录

FROZEN = bool(getattr(sys, "frozen", False))

if FROZEN:
    RESOURCE_ROOT = Path(sys.executable).resolve().parent
    _meipass = str(getattr(sys, "_MEIPASS", "") or "")
    _bundle = Path(_meipass) if _meipass else RESOURCE_ROOT / "_internal"
    BUNDLE_DIR = _bundle if _bundle.exists() else RESOURCE_ROOT
else:
    RESOURCE_ROOT = Path(__file__).resolve().parent.parent
    BUNDLE_DIR = RESOURCE_ROOT

# 兼容别名：老代码里的 ROOT 语义 = 程序资源根（源码模式即项目根）
ROOT = RESOURCE_ROOT

BIN_DIR = BUNDLE_DIR / "bin"
WEB_DIR = BUNDLE_DIR / "web"
PYLIBS_DIR = BUNDLE_DIR / "_pylibs"


def _default_data_root() -> Path:
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "MediaDownloader"
    return Path.home() / ".mediadownloader"


DATA_ROOT = Path(os.environ.get("MEDIA_DOWNLOADER_DATA_DIR") or _default_data_root())


def _migrate_from_resource(rel: Path) -> None:
    """一次性迁移：老版本把 config.json / data/ 放在程序目录，搬去 DATA_ROOT（不覆盖已有）。"""
    src = RESOURCE_ROOT / rel
    dst = DATA_ROOT / rel
    if src.is_file() and not dst.exists():
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        except OSError:
            pass


_migrate_from_resource(Path("config.json"))
_migrate_from_resource(Path("data") / "download_history.json")


def _setting_downloads_dir() -> str:
    """从 DATA_ROOT/config.json 读用户自选下载目录（不经 store，避免循环导入）。"""
    try:
        raw = json.loads((DATA_ROOT / "config.json").read_text(encoding="utf-8"))
        return str((raw or {}).get("downloads_dir") or "").strip()
    except Exception:
        return ""


def _default_download_root() -> Path:
    if os.name == "nt":
        return Path(os.path.expandvars(r"%USERPROFILE%\Downloads")) / "MediaDownloader"
    return Path.home() / "Downloads" / "MediaDownloader"


_env_dl = os.environ.get("MEDIA_DOWNLOADER_DOWNLOAD_DIR", "").strip()
_cfg_dl = _setting_downloads_dir()
DOWNLOADS_DIR = Path(_env_dl or _cfg_dl or _default_download_root())

DATA_ROOT.mkdir(parents=True, exist_ok=True)
(DATA_ROOT / "logs").mkdir(parents=True, exist_ok=True)
(DATA_ROOT / "data").mkdir(parents=True, exist_ok=True)
DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
# 注意：BIN_DIR 是 RESOURCE/BUNDLE 内容（bin/ffmpeg.exe 随包分发），
# 只读，严禁运行时创建/写入；找不到就由 find_ffmpeg() 回退到 FFMPEG_PATH / PATH。

if PYLIBS_DIR.exists():
    _pylibs = str(PYLIBS_DIR)
    if _pylibs not in sys.path:
        # 放在末尾：优先用全局环境里已装好的版本
        sys.path.append(_pylibs)

# 本机已安装的浏览器内核（playwright 可直接复用，无需下载 ~300MB 内核）
BROWSER_CANDIDATES = {
    "chrome": [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ],
    "msedge": [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ],
}

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

HEADERS = {
    "User-Agent": DEFAULT_UA,
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

TIMEOUT = 25
PROBE_TIMEOUT = 12
# 探测图片宽高时读取的字节数：先读头部，解析不出再续读到上限
# （JPEG 的 SOF 段可能位于文件中部，大图/多缩略图 EXIF 需要更多字节才能拿到真实宽高）
PROBE_FIRST_CHUNK = 256 * 1024
MAX_PROBE_BYTES = 3 * 1024 * 1024

# 允许用户自定义下载根目录（写入 config.json，见 store.py）；
# 默认 %USERPROFILE%\Downloads\MediaDownloader（源码/安装版一致，不写程序目录）
DOWNLOADS_SETTING_KEY = "downloads_dir"


def find_js_runtime() -> tuple[str, str] | None:
    """找一个可用的 JavaScript 运行时给 yt-dlp 用，返回 (名字, 可执行文件路径)。

    为什么需要它（2026-09-21 实测）：yt-dlp 2026.07.04 对 YouTube 的 JS 挑战
    （n-sig / PO token）**必须有 JS 运行时**才能解。缺了它 yt-dlp 会打印

        WARNING: No supported JavaScript runtime could be found.
        ... YouTube extraction without a JS runtime has been deprecated

    并退化到 `android vr` 客户端 —— 拿到的媒体地址直接 **HTTP 403 Forbidden**，
    用户看到的就是「站点拒绝了请求（403），可能存在防盗链」。
    本机没有 deno（yt-dlp 默认只启用 deno），但有 Node，所以显式接上。

    按 yt-dlp 的优先级尝试：deno > node > quickjs > bun。
    """
    import glob as _glob

    candidates: list[tuple[str, list[str]]] = [
        ("deno", ["deno"]),
        ("node", ["node"]),
        ("quickjs", ["qjs", "quickjs"]),
        ("bun", ["bun"]),
    ]
    for name, exes in candidates:
        for exe in exes:
            hit = shutil.which(exe)
            if hit:
                return name, hit

    # PATH 里没有时，找几个常见安装位置。
    # ⚠️ 1E 策略：WorkBuddy 托管 node 只是【源码开发机】的便利候选，
    # frozen 发布版绝不能把它当隐性发布依赖（开发机找得到、用户机器没有，
    # YouTube 就会「开发时正常、发布后 403」）—— FROZEN 下跳过。
    # 发布版只认：用户 PATH 里的 deno/node/quickjs/bun + Node.js 官方安装位置。
    # 找不到时 YouTube 等部分功能明确降级（/api/status 会显示 js_runtime 状态）。
    patterns = [
        r"E:\nodejs\node.exe",
        r"C:\Program Files\nodejs\node.exe",
        r"C:\Program Files (x86)\nodejs\node.exe",
        r"%LOCALAPPDATA%\Programs\nodejs\node.exe",
    ]
    if not FROZEN:
        patterns.append(
            r"C:\Users\*\AppData\Roaming\npm\..\..\.workbuddy\binaries\node\versions\*\node.exe"
        )
    for pat in patterns:
        p = os.path.expandvars(pat)
        for hit in ([p] if os.path.sep in p and "*" not in p else _glob.glob(p)):
            if hit and Path(hit).exists():
                return "node", hit
    return None


def find_ffmpeg() -> str | None:
    """按优先级查找 ffmpeg 可执行文件：项目 bin/ffmpeg.exe > 环境变量 > PATH。"""
    local = BIN_DIR / "ffmpeg.exe"
    if local.exists():
        return str(local)
    env = os.environ.get("FFMPEG_PATH")
    if env and Path(env).exists():
        return env
    which = shutil.which("ffmpeg")
    return which


def ffmpeg_version(ffmpeg_path: str | None = None) -> str | None:
    """返回 ffmpeg 版本字符串，失败返回 None（用于前端状态提示）。"""
    path = ffmpeg_path or find_ffmpeg()
    if not path:
        return None
    try:
        out = subprocess.run(
            [path, "-version"],
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        first = (out.stdout or "").splitlines()
        return first[0] if first else "unknown"
    except Exception:
        return None


def is_safe_download_path(path: str) -> bool:
    """仅允许写入默认下载目录及其子目录，防止路径穿越写到其它位置。"""
    try:
        p = Path(path).resolve()
        base = DOWNLOADS_DIR.resolve()
        return base == p or base in p.parents
    except Exception:
        return False
