# -*- coding: utf-8 -*-
"""隔离测试版 onefile 入口。正式版入口仍是 launcher.py，本文件只给测试构建用。

与已安装的 MediaDownloader V3.0.4 的隔离清单（逐项对应真实共用点）：

  数据目录    MEDIA_DOWNLOADER_DATA_DIR -> <exe目录>/MediaDownloader_TEST/data
              （已安装版用 %LOCALAPPDATA%\\MediaDownloader3；设了本变量后
               config._migrate_from_v25 直接 return，绝不读已安装版的数据）
  下载目录    MEDIA_DOWNLOADER_DOWNLOAD_DIR -> <exe目录>/MediaDownloader_TEST/downloads
              （已安装版用 %USERPROFILE%\\Downloads\\MediaDownloader）
  端口段      18848-18870（正式版 8848-8870 / 源码版 18700-18720，三方不重叠）
  实例指纹    instance_id = sha1(DATA_ROOT|RESOURCE_ROOT)，数据目录不同必然不同，
              /api/health 认领时不会把窗口挂到已安装版上
  窗口标题    desktop_shell.TITLE 加 [隔离测试版]，托盘/消息框共用这个常量
  孤儿 ffmpeg win_job 的收割按「命令行里的 ffmpeg 路径」匹配；测试版不带
              bin/ffmpeg.exe，用的是系统 PATH 里同一个 ffmpeg（可能与已安装版
              相同）→ 停掉收割，避免测试实例杀掉另一实例的残留进程。
              Job 绑定保留（只管自己进程树）。
  bsk daemon  BrowserSkill 的 daemon 是全局单例，测试版不参与；
              普通抓取链路本来就不经过它。
  浏览器内核  WebView2 / 系统 Chrome 由系统提供，Profile、Cookie、webview
              storage 全部落在隔离 DATA_ROOT 内。

所有 patch 都在 launcher.main() 之前完成。launcher / serve / app.main 里的
`from app.xxx import ...` 拿的是同一个模块对象，属性替换对后续导入生效。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

SANDBOX_NAME = "MediaDownloader_TEST"
TEST_PORT_FIRST = 18848
TEST_PORT_LAST = 18870
TEST_TITLE = "MediaDownloader 3.0.4 [隔离测试版]"

# 沙箱位置的记忆文件：放在 %LOCALAPPDATA% 而不是沙箱里 —— 沙箱本身还没定位时
# 就要读它，放进去会变成鸡生蛋。文件名带 .sandbox 后缀，和目录区分开。
POINTER_NAME = "MediaDownloader_TEST.sandbox"

# 顶层 import：让 PyInstaller 的静态分析明确把 launcher 收进 PYZ。
# launcher 模块顶层只 import os/sys，不会在任何环境变量生效前碰 app.*。
import launcher  # noqa: E402


def _exe_base() -> Path:
    # onefile 下 sys.executable 是真实 exe 路径（不是 _MEIPASS 临时目录），
    # 所以基准位置每次启动都稳定。
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _pointer_path() -> Path | None:
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        return None
    return Path(base) / POINTER_NAME


def _has_payload(root: Path) -> bool:
    """沙箱里是否已有用户数据。

    判据按「丢了最疼」的顺序取，任意一条成立即算已初始化：
      config.json        → 用户改过的设置（界面主题/代理/下载目录）
      platform_profiles  → 七个平台的登录态（390MB 量级，丢了要重新扫码）
      download_history   → 下载台账
    """
    data = root / "data"
    return (
        (data / "config.json").is_file()
        or (data / "platform_profiles").is_dir()
        or (data / "data" / "download_history.json").is_file()
    )


def _freshness(root: Path) -> float:
    """沙箱最近活跃时间，用来在多个候选里挑用户真正在用的那份。"""
    best = 0.0
    for rel in ("data/data/download_history.json",
                "data/config.json",
                "data/platform_profiles"):
        try:
            best = max(best, (root / rel).stat().st_mtime)
        except OSError:
            continue
    return best


def _sibling_candidates(base: Path) -> list[Path]:
    """exe 同级 + 向上 2 层里的兄弟目录，找叫 MediaDownloader_TEST 的沙箱。

    只扫两层：`_dist_ui/` → 项目根（命中 `_dist_test/`）一层就够，
    第二层覆盖「exe 埋在 _dist_xxx/build/ 下」的结构。再往上会扫到大目录，
    既慢又容易误接管无关的旧沙箱。
    """
    out: list[Path] = []
    cur = base
    for _ in range(2):
        parent = cur.parent
        if parent == cur:
            break
        try:
            entries = sorted(parent.iterdir())
        except OSError:
            break
        for child in entries:
            if not child.is_dir():
                continue
            cand = child / SANDBOX_NAME
            if cand.is_dir() and cand != base / SANDBOX_NAME:
                out.append(cand)
        cur = parent
    return out


def resolve_sandbox_root(base: Path | None = None) -> tuple[Path, str]:
    """决定本次启动用哪个沙箱，返回 (根目录, 来源说明)。

    优先级（从高到低）：
      1. MEDIA_DOWNLOADER_TEST_DATA_DIR  自动化验收硬指定
      2. MEDIA_DOWNLOADER_TEST_FRESH=1   强制新沙箱（忽略下面全部）
      3. 指针文件记录的沙箱              跨目录/跨盘拷 exe 也不丢数据
      4. exe 同级沙箱                    传统便携布局
      5. 兄弟目录里最新的已初始化沙箱    新包放到新目录时接管旧数据
      6. 新建 exe 同级沙箱               首次运行

    为什么要有 3 和 5：以前沙箱严格等于「exe 所在目录/MediaDownloader_TEST」，
    换个输出目录（_dist_test → _dist_ui）就等于是全新安装，登录态和台账全空。
    用户 2026-11-07 反馈的「没有继承原来的版本」就是这条路径。
    """
    base = Path(base) if base is not None else _exe_base()
    if base.name == SANDBOX_NAME:
        base = base.parent          # 传进来的是沙箱目录本身时归一到上级

    forced = os.environ.get("MEDIA_DOWNLOADER_TEST_DATA_DIR")
    if forced:
        return Path(forced).resolve(), "env:MEDIA_DOWNLOADER_TEST_DATA_DIR"

    fresh = os.environ.get("MEDIA_DOWNLOADER_TEST_FRESH") == "1"
    if not fresh:
        ptr = _pointer_path()
        if ptr is not None:
            try:
                saved = Path(ptr.read_text(encoding="utf-8").strip())
            except (OSError, ValueError):
                saved = None
            if saved and saved.is_dir() and _has_payload(saved):
                return saved, "pointer"

        local = base / SANDBOX_NAME
        if _has_payload(local):
            return local, "local"

        siblings = [c for c in _sibling_candidates(base) if _has_payload(c)]
        if siblings:
            best = max(siblings, key=_freshness)
            return best, "sibling"

    return base / SANDBOX_NAME, "new"


def _sandbox_root() -> Path:
    # 兼容旧调用点：真正逻辑在 resolve_sandbox_root
    return resolve_sandbox_root()[0]


def _bootstrap() -> None:
    root, why = resolve_sandbox_root()
    data_dir = root / "data"
    dl_dir = root / "downloads"
    data_dir.mkdir(parents=True, exist_ok=True)
    dl_dir.mkdir(parents=True, exist_ok=True)

    # 记住本次选定的沙箱：下次不管 exe 从哪个目录启动都回来读这一份。
    # 写失败不影响运行（比如 LOCALAPPDATA 只读的极端环境），只是少一层保险。
    ptr = _pointer_path()
    if ptr is not None:
        try:
            ptr.parent.mkdir(parents=True, exist_ok=True)
            ptr.write_text(str(root), encoding="utf-8")
        except OSError:
            pass

    # app.config 在导入期就把这两个值固化成模块常量，必须先设环境变量。
    # 用覆盖而不是 setdefault：--internal 子进程继承父进程 env 后再次执行
    # 本函数，两边算出来的仍是同一套路径，幂等。
    os.environ["MEDIA_DOWNLOADER_DATA_DIR"] = str(data_dir)
    os.environ["MEDIA_DOWNLOADER_DOWNLOAD_DIR"] = str(dl_dir)
    # 故意不设 MEDIA_DOWNLOADER_PORT：让 resolve_port 走下面改过的独立扫描段，
    # 这样同一测试版的第二次启动仍能靠 instance_id 正确认领，不会双开。

    # 自证文件：双击之后不用猜，直接看到这个实例用的是哪套目录/端口/标识。
    try:
        (root / "隔离说明.txt").write_text(
            "\n".join([
                "MediaDownloader 隔离测试版",
                "",
                f"数据目录   : {data_dir}",
                f"下载目录   : {dl_dir}",
                f"沙箱来源   : {why}",
                f"端口段     : {TEST_PORT_FIRST}-{TEST_PORT_LAST}",
                f"窗口标题   : {TEST_TITLE}",
                f"实例根目录 : {Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent}",
                "",
                "正式版（V3.0.4）使用的是：",
                r"  %LOCALAPPDATA%\MediaDownloader3",
                r"  %USERPROFILE%\Downloads\MediaDownloader",
                "  端口 8848-8870",
                "两者没有任何共享目录、共享端口或共享守护进程。",
                "",
                "日志在本目录 data\\logs 下；删除整个 MediaDownloader_TEST 目录即可彻底卸载测试态。",
                "",
            ]),
            encoding="utf-8",
        )
    except OSError:
        pass

    # ---- 端口段隔离 ----
    import app.ports as ports

    ports.PORT_RANGE = range(TEST_PORT_FIRST, TEST_PORT_LAST + 1)
    ports.DEV_PORT_RANGE = range(TEST_PORT_FIRST, TEST_PORT_LAST + 1)

    # ---- 桌面壳标识隔离 ----
    import app.desktop_shell as shell

    shell.TITLE = TEST_TITLE
    # 兜底 .cmd 里写死的是 MediaDownloader.exe；测试版必须指向自己的文件名，
    # 否则用户双击那个 .cmd 会启动不到东西（或误启动正式版）。
    exe_name = Path(sys.executable).name
    shell._browser_launcher_doc = lambda: (
        "@echo off\r\n"
        "rem Isolated test build - open the UI in the default browser.\r\n"
        f'start "" "%~dp0{exe_name}" --browser\r\n'
    )

    # ---- 进程级全局副作用隔离 ----
    import app.win_job as win_job

    win_job.reap_orphan_ffmpeg = lambda *a, **kw: 0
    win_job.start_reaper_thread = lambda *a, **kw: None

    import app.browser_skill as browser_skill

    browser_skill.bsk_binary = lambda: ""


def main() -> int:
    here = str(Path(__file__).resolve().parent)
    if here not in sys.path:
        sys.path.insert(0, here)
    _bootstrap()
    return launcher.main()


if __name__ == "__main__":
    sys.exit(main())
