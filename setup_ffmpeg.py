# -*- coding: utf-8 -*-
"""从已下载的 imageio-ffmpeg wheel 中提取 ffmpeg.exe 到项目 bin/ 目录。

用途：让视频下载工具开箱即用（支持音视频合并），无需用户手动安装 ffmpeg。
可重复运行；若 bin/ffmpeg.exe 已存在则跳过。
"""
import shutil
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WHEEL_DIR = ROOT / "_wheel"
BIN_DIR = ROOT / "bin"


def extract() -> Path | None:
    target = BIN_DIR / "ffmpeg.exe"
    if target.exists() and target.stat().st_size > 1_000_000:
        print(f"[ffmpeg] 已存在: {target} ({target.stat().st_size/1024/1024:.1f} MB)")
        return target
    wheels = sorted(WHEEL_DIR.glob("imageio_ffmpeg-*.whl"))
    if not wheels:
        print(f"[ffmpeg] 未找到 wheel，目录: {WHEEL_DIR}")
        return None
    whl = wheels[-1]
    print(f"[ffmpeg] 解析 wheel: {whl.name}")
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(whl) as z:
        exe_names = [n for n in z.namelist() if n.lower().endswith(".exe") and "binaries" in n]
        if not exe_names:
            print("[ffmpeg] wheel 内未找到 ffmpeg 可执行文件")
            return None
        chosen = max(exe_names, key=lambda n: z.getinfo(n).file_size)
        print(f"[ffmpeg] 提取: {chosen}")
        with z.open(chosen) as src, open(target, "wb") as dst:
            shutil.copyfileobj(src, dst, length=1024 * 256)
    print(f"[ffmpeg] 完成: {target} ({target.stat().st_size/1024/1024:.1f} MB)")
    return target


if __name__ == "__main__":
    extract()
