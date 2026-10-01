# -*- coding: utf-8 -*-
"""启动前自检：检查依赖、ffmpeg，必要时自动安装/提取。

供「打开工具.bat」调用。把复杂逻辑放在 Python 里，避免批处理文件的
引号嵌套、代码页与行尾问题（这些是 cmd 脚本最常见的失败原因）。

退出码：0 = 可以继续启动；非 0 = 存在阻塞问题。
"""
from __future__ import annotations

import importlib
import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _align_console_encoding() -> None:
    """把 stdout/stderr 编码对齐到 CMD 控制台的实际代码页。

    为什么需要：Python 源码是 UTF-8，默认按 UTF-8 输出中文；
    而中文 Windows 的 CMD 控制台代码页通常是 936(GBK)，
    直接输出会变成「濯掍綋涓嬭浇宸ュ叿」这类乱码。
    """
    try:
        import ctypes

        cp = ctypes.windll.kernel32.GetConsoleOutputCP()
    except Exception:
        cp = 0
    if not cp:
        return
    enc = f"cp{cp}"
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding=enc, errors="replace")
        except Exception:
            # 无控制台（被重定向）时 reconfigure 可能失败，忽略即可
            pass

# 必需依赖：导入名 -> pip 包名
REQUIRED = [
    ("fastapi", "fastapi"),
    ("uvicorn", "uvicorn"),
    ("requests", "requests"),
    ("bs4", "beautifulsoup4"),
    ("lxml", "lxml"),
    ("PIL", "pillow"),
    ("yt_dlp", "yt-dlp"),
    ("pydantic", "pydantic"),
]

# 可选依赖：缺失只影响备用搜图源，不阻塞启动
OPTIONAL = [("ddgs", "ddgs")]


def _run(cmd: list[str]) -> int:
    print("  $ " + " ".join(cmd), flush=True)
    return subprocess.call(cmd)


def check_python() -> bool:
    v = sys.version_info
    print(f"[1/4] Python {v.major}.{v.minor}.{v.micro}")
    if v < (3, 10):
        print(f"      需要 Python 3.10 或更高版本，当前 {v.major}.{v.minor}。")
        return False
    print("      OK")
    return True


def check_required() -> bool:
    print("[2/4] 检查必需依赖…")
    missing = []
    for mod, pkg in REQUIRED:
        try:
            importlib.import_module(mod)
        except ImportError:
            missing.append(pkg)
    if not missing:
        print("      全部就绪")
        return True

    print(f"      缺失: {', '.join(missing)}")
    print("      正在自动安装…")
    code = _run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", *missing])
    if code != 0:
        print("      自动安装失败。请手动执行：")
        print(f"        {sys.executable} -m pip install {' '.join(missing)}")
        return False

    still = []
    for mod, pkg in REQUIRED:
        try:
            importlib.import_module(mod)
        except ImportError:
            still.append(pkg)
    if still:
        print(f"      安装后仍缺失: {', '.join(still)}")
        return False
    print("      安装完成")
    return True


def check_optional() -> None:
    missing = [pkg for mod, pkg in OPTIONAL if importlib.util.find_spec(mod) is None]
    if not missing:
        return
    print(f"[提示] 备用搜图源缺少 {', '.join(missing)}（不影响主功能，Bing 搜索可正常使用）")
    print(f"       如需启用可执行: {sys.executable} -m pip install {' '.join(missing)}")


def check_ffmpeg() -> None:
    print("[3/4] 检查 ffmpeg…")
    target = ROOT / "bin" / "ffmpeg.exe"
    if target.exists() and target.stat().st_size > 1_000_000:
        print(f"      OK  {target} ({target.stat().st_size / 1024 / 1024:.0f} MB)")
        return
    if shutil.which("ffmpeg"):
        print(f"      OK  系统 PATH 中的 ffmpeg: {shutil.which('ffmpeg')}")
        return
    setup = ROOT / "setup_ffmpeg.py"
    wheel_dir = ROOT / "_wheel"
    if setup.exists() and wheel_dir.exists() and any(wheel_dir.glob("imageio_ffmpeg-*.whl")):
        print("      未找到，正在从本地安装包提取…")
        code = _run([sys.executable, str(setup)])
        if code == 0 and target.exists():
            print("      提取完成")
            return
    print("      未找到 ffmpeg。仍可正常使用，但高清晰度视频（音视频分离流）无法合并，")
    print("      工具会自动退回到自带音轨的完整格式，可能拿不到最高画质。")


def check_writable() -> bool:
    print("[4/4] 检查下载目录可写…")
    dl = ROOT / "downloads"
    try:
        dl.mkdir(parents=True, exist_ok=True)
        probe = dl / ".write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        print(f"      OK  {dl}")
        return True
    except OSError as e:
        print(f"      下载目录不可写: {e}")
        return False


def main() -> int:
    _align_console_encoding()
    print("=" * 52)
    print("            媒体下载工具 - 启动自检")
    print("=" * 52)
    ok = check_python()
    if not ok:
        return 1
    if not check_required():
        return 2
    check_optional()
    check_ffmpeg()
    if not check_writable():
        return 3
    print("-" * 52)
    print("自检通过，正在启动服务…")
    print("=" * 52)
    return 0


if __name__ == "__main__":
    sys.exit(main())
