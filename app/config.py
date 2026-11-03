# -*- coding: utf-8 -*-
"""全局配置与共享常量：路径、User-Agent、超时、ffmpeg 定位、目录安全校验。"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

# ---------- 产品标识 ----------
# V3.0 用独立的数据目录，绝不和 V2.5 共用。
# 原因（2026-10-22 事故）：两版 config 结构会各自演进，共用同一个 config.json
# 必然互相覆盖；再加上端口认领问题，用户会看到「装好的客户端被改了」的假象。
APP_TITLE = "MediaDownloader 3.0"       # 窗口标题 / 任务栏 / 开始菜单（与 V2.5 明确区分）
APP_VERSION = "3.0.3"
DATA_DIR_NAME = "MediaDownloader3"      # 本版数据目录名
LEGACY_DATA_DIR_NAME = "MediaDownloader"  # V2.5 的数据目录名（只读，用于首次迁移）

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
        return Path(base) / DATA_DIR_NAME
    return Path.home() / ".mediadownloader3"


DATA_ROOT = Path(os.environ.get("MEDIA_DOWNLOADER_DATA_DIR") or _default_data_root())


def _migrate_from_v25() -> None:
    """V3.0 首次启动：把 V2.5 的数据**复制**一份过来（复制，不是共用）。

    为什么不用同一个目录：两版 config 结构会各自演进，共用必然互相覆盖
    （2026-10-22 真实事故）。复制之后两套彻底独立，可以同时安装、同时运行。

    规则：
    - 显式指定了 MEDIA_DOWNLOADER_DATA_DIR（测试/部署）→ 不迁移
    - 已经有迁移标记 → 不重复迁移
    - 目标目录里已有对应内容 → 不覆盖（用户自己放过东西）
    - 只迁移配置 / 台账 / 平台登录态，不迁移 logs、tmp 这类临时物
    """
    if os.environ.get("MEDIA_DOWNLOADER_DATA_DIR"):
        return
    if os.name != "nt":
        return
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    legacy = Path(base) / LEGACY_DATA_DIR_NAME
    if not legacy.is_dir():
        return
    marker = DATA_ROOT / ".migrated_from_v25"
    if marker.exists():
        return
    try:
        DATA_ROOT.mkdir(parents=True, exist_ok=True)
    except OSError:
        return

    log: list[str] = []
    started = time.time()
    for name in ("config.json", "data", "platform_profiles", "site_profiles"):
        src = legacy / name
        dst = DATA_ROOT / name
        if not src.exists():
            continue
        try:
            if src.is_file():
                if not dst.exists():
                    shutil.copy2(src, dst)
                    log.append(f"复制 {name}")
            else:
                if dst.exists() and any(dst.iterdir()):
                    log.append(f"跳过 {name}（目标已有内容）")
                    continue
                shutil.copytree(src, dst, symlinks=True, dirs_exist_ok=True)
                n = sum(1 for _ in dst.rglob("*"))
                log.append(f"复制 {name}（{n} 项）")
        except Exception as e:  # noqa: BLE001
            log.append(f"复制 {name} 失败：{type(e).__name__}: {e}")

    try:
        marker.write_text(json.dumps({
            "from": str(legacy), "ts": int(time.time()),
            "elapsed": round(time.time() - started, 1), "actions": log,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


_migrate_from_v25()


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

# ---- V3.0：内置 Playwright Chromium ----
# 安装包自带一份 Chromium（约 428 MB），用户电脑即使一个浏览器都没装，
# 也能做 JS 渲染和登录窗口。路径解析见 app/browser_runtime.py。
#
# 只带完整版、不带 chromium_headless_shell（那玩意另外 272 MB）：
# headless 时用 --headless 跑完整内核即可，实测渲染结果完全一致。
# 代价是**必须显式传 executable_path** —— playwright 默认会去找 headless shell，
# 找不到就报 "Executable doesn't exist"，这是只保留一份的必然后果。
BROWSERS_DIR = BUNDLE_DIR / "browsers"
if BROWSERS_DIR.is_dir():
    # playwright 内部解析内核时也走这个目录（我们主要用 executable_path，这里双保险）
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(BROWSERS_DIR)

# 本机已安装的浏览器内核（包内 Chromium 不可用时的兜底）
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

    # ⚠️ 1E.2：frozen 下最优候选是随包分发的 Playwright 驱动自带 node.exe
    # （_internal\playwright\driver\node.exe，v24+）—— 不需要用户装任何东西，
    # 也不引入第二份 Node。必须放在 PATH 之前：用户 PATH 里的 node 版本不可控。
    if FROZEN:
        bundled = BUNDLE_DIR / "playwright" / "driver" / "node.exe"
        if bundled.exists():
            return "node", str(bundled)

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
    # YouTube 就会「开发时正常、发布后 403」）—— FROZEN 下跳过（1E.2 起
    # frozen 优先用上面随包的 Playwright bundled node）。
    # 发布版只认：随包 node > 用户 PATH 里的 deno/node/quickjs/bun + Node.js 官方安装位置。
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
