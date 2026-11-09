# -*- coding: utf-8 -*-
"""TLS CA 证书束的持久化与自愈（frozen 长期运行专用）。

事故（2026-11-08 18:42，Sam 报「抖音 / 小红书 / 快手 / B站 抓取和下载全挂」）：

    OSError: Could not find a suitable TLS CA certificate bundle,
    invalid path: C:\\Users\\...\\AppData\\Local\\Temp\\_MEI000004202\\certifi\\cacert.pem

PyInstaller onefile 启动时把 certifi 的 cacert.pem 解压到 %TEMP%\\_MEIxxxxxx\\certifi\\，
requests 在 **import 时**就把这个路径缓存进 requests.adapters.DEFAULT_CA_BUNDLE_PATH。
该临时目录在进程运行 6 小时后被外部清理（实测：11:37 启动的实例，17:11 父目录 mtime
更新、web/ 子目录仍在、certifi/ 子目录消失），于是所有走 requests / yt-dlp 的 HTTPS
请求同时抛 OSError —— 表现出来的就是「所有平台一起挂」。新启动的实例重新解压，所以正常。

修法（不依赖 _MEI 是否还在）：
  1. 进程早期把 CA 固化到 DATA_ROOT/certs/cacert.pem（持久数据目录，不随 Temp 清理）；
  2. 让 REQUESTS_CA_BUNDLE / CURL_CA_BUNDLE / SSL_CERT_FILE 指向它；
  3. 同时改写 requests.adapters.DEFAULT_CA_BUNDLE_PATH 与 certifi.where()，
     覆盖「import 时缓存常量」和「直接调 certifi.where()」两类调用；
  4. 源文件与持久副本都没了时，从 Windows 系统根证书库导出一份 PEM 兜底，
     让已经跑起来的老进程也能自愈，不必重启。

环境变量（参数外置）：
  MEDIA_DOWNLOADER_CA_BUNDLE   指定自定义 CA PEM，优先于内置 certifi（企业代理 / 自签根）
  MEDIA_DOWNLOADER_CA_DISABLE=1 关闭固化，保持原样（排障用）
"""
from __future__ import annotations

import os
import ssl
import sys
import threading
import time
from pathlib import Path

_LOCK = threading.Lock()
_STATE: dict = {"path": "", "source": "", "installed": False, "root": ""}

_ENV_KEYS = ("REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "SSL_CERT_FILE")


def _valid_pem(p: Path | str | None) -> bool:
    """文件存在、够大、且确实是证书束（不是空壳 / 被安全软件清成 0 字节）。"""
    try:
        q = Path(p) if p else None
        if q is None or not q.is_file() or q.stat().st_size < 1024:
            return False
        with q.open("rb") as f:
            # Mozilla 的 bundle 开头有 ~1KB 许可注释，读 4KB 足够看到第一张证书
            return b"-----BEGIN CERTIFICATE-----" in f.read(4096)
    except (OSError, TypeError, ValueError):
        return False


def _default_root() -> Path:
    """与 app.config.DATA_ROOT 同一套解析；这里不 import config 以免循环导入。"""
    v = os.environ.get("MEDIA_DOWNLOADER_DATA_DIR")
    if v:
        return Path(v)
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "MediaDownloader3"
    return Path.home() / ".mediadownloader3"


def _log(root: Path, msg: str) -> None:
    try:
        p = root / "logs" / "ca_bundle.log"
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8", errors="replace") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} pid={os.getpid()} {msg}\n")
    except OSError:
        pass


def _candidate_sources() -> list[Path]:
    """CA 源候选，按「最可信」排序；frozen 下基本都指向 _MEI 临时目录。"""
    out: list[Path] = []

    def add(v) -> None:
        if not v:
            return
        try:
            p = Path(v)
        except (TypeError, ValueError):
            return
        if p not in out:
            out.append(p)

    try:
        import certifi
        add(certifi.where())
    except Exception:
        pass
    meipass = str(getattr(sys, "_MEIPASS", "") or "")
    if meipass:
        add(Path(meipass) / "certifi" / "cacert.pem")
        add(Path(meipass) / "certifi" / "cacert.pem")
    try:
        import requests.adapters as _adapters
        add(getattr(_adapters, "DEFAULT_CA_BUNDLE_PATH", ""))
    except Exception:
        pass
    try:
        root = (Path(sys.executable).resolve().parent
                if getattr(sys, "frozen", False)
                else Path(__file__).resolve().parent.parent)
        add(root / "certs" / "cacert.pem")
        add(root / "cacert.pem")
        add(root / "_internal" / "certifi" / "cacert.pem")
    except OSError:
        pass
    return out


def _windows_root_pem() -> bytes | None:
    """最后兜底：把 Windows 系统信任的根/中间证书导出成 PEM。

    系统证书库在 %SystemRoot% 下，永远不会被 Temp 清理策略删掉。
    复杂度 O(证书数)，通常 100~300 张、几百 KB，只在 CA 全丢时跑一次。
    """
    enum = getattr(ssl, "enum_certificates", None)
    if enum is None:
        return None
    chunks: list[str] = []
    for store in ("ROOT", "CA"):
        try:
            for der, _enc, trust in enum(store):
                if not der or trust is False:
                    continue
                try:
                    chunks.append(ssl.DER_cert_to_PEM_cert(der))
                except (ValueError, TypeError):
                    continue
        except OSError:  # 某个 store 不可读就跳过
            continue
    text = "".join(chunks)
    if text.count("-----BEGIN CERTIFICATE-----") < 5:
        return None
    return text.encode("ascii", "ignore")


def _atomic_write(dst: Path, data: bytes) -> bool:
    """先写临时文件再原子替换：多实例共用 DATA_ROOT 时也不会读到半截 PEM。"""
    tmp = dst.with_name(dst.name + f".tmp.{os.getpid()}")
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_bytes(data)
        os.replace(tmp, dst)
        return _valid_pem(dst)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False


def _publish(path: Path, source: str) -> None:
    """让所有 HTTP 客户端都指向稳定 CA。"""
    path_str = str(path)
    _STATE.update({"path": path_str, "source": source, "installed": True})

    # 用户显式设过且路径有效 → 不动；设过但指向已被删掉的文件 → 必须覆盖，
    # 否则 requests.merge_environment_settings 会把坏路径塞进 verify 再次抛 OSError。
    for key in _ENV_KEYS:
        cur = os.environ.get(key)
        if not cur or not _valid_pem(cur):
            os.environ[key] = path_str

    # import 时缓存的常量 / 顶层 from-import 绑定，都要一并改掉。
    try:
        import requests.adapters as _adapters
        _adapters.DEFAULT_CA_BUNDLE_PATH = path_str
    except Exception:
        pass
    try:
        import requests.certs as _certs
        _certs.where = lambda: path_str
    except Exception:
        pass
    try:
        import certifi
        certifi.where = lambda: path_str
        core = getattr(certifi, "core", None)
        if core is not None:
            core.where = lambda: path_str
    except Exception:
        pass


def status() -> dict:
    p = Path(_STATE.get("path") or "")
    ok = _valid_pem(p)
    return {"path": str(p), "exists": ok,
            "source": str(_STATE.get("source") or ""),
            "size": p.stat().st_size if ok else 0,
            "installed": bool(_STATE.get("installed"))}


def install(data_root: Path | str | None = None, *, force: bool = False) -> dict:
    """固化 CA 并改写全部入口。返回 status()，失败也不抛（不能让启动崩掉）。"""
    if os.environ.get("MEDIA_DOWNLOADER_CA_DISABLE") == "1":
        _STATE.update({"installed": True, "source": "disabled"})
        return status()
    with _LOCK:
        root = Path(data_root) if data_root else _default_root()
        _STATE["root"] = str(root)
        stable = root / "certs" / "cacert.pem"
        custom = os.environ.get("MEDIA_DOWNLOADER_CA_BUNDLE")

        # 1) 自定义 CA（企业代理 / 自签根）：有效就用，且不去覆盖用户 env
        if custom and _valid_pem(custom):
            _publish(Path(custom), "env:MEDIA_DOWNLOADER_CA_BUNDLE")
            return status()

        # 2) 用户显式设的**外部** CA 要尊重；但 env 若已经被我们改成稳定副本，
        #    不能反过来被当成「用户设置」而跳过稳定副本分支。
        if not force:
            for key in _ENV_KEYS:
                v = os.environ.get(key)
                if not v or not _valid_pem(v):
                    continue
                try:
                    is_stable = Path(v).resolve() == stable.resolve()
                except (OSError, ValueError):
                    is_stable = False
                if not is_stable:
                    _publish(Path(v), "env:" + key)
                    return status()

        # 3) 已有持久副本：正常启动路径（_MEI 早已被清理也不会受影响）
        if _valid_pem(stable) and not force:
            _publish(stable, "persistent")
            _log(root, f"reuse {stable} ({stable.stat().st_size} B)")
            return status()

        # 3) 从 certifi / _MEI / 发行目录复制一份到持久目录
        data: bytes | None = None
        source = ""
        for src in _candidate_sources():
            if _valid_pem(src):
                try:
                    data = src.read_bytes()
                    source = "copy:" + str(src)
                    break
                except OSError:
                    continue

        # 4) 源都没了但持久副本还在（force 重验时常见）：别把好的删掉
        if data is None and _valid_pem(stable):
            _publish(stable, "persistent")
            return status()

        # 5) 系统根证书兜底：让已经坏了的老进程无需重启也能恢复
        if data is None:
            pem = _windows_root_pem()
            if pem:
                data, source = pem, "windows-root-store"

        if data and _atomic_write(stable, data):
            _publish(stable, source)
            _log(root, f"installed {stable} ({len(data)} B) from {source}")
        else:
            # 找不到任何可用 CA：保持 env 干净，让请求明确报缺依赖而不是报坏路径
            _STATE.update({"path": str(stable), "source": "unavailable",
                           "installed": True})
            _log(root, "ERROR no usable CA bundle (certifi/_MEI/system store all missing)")
        return status()


def refresh() -> dict:
    """CA 文件被外部清理后重新固化；供请求失败时自愈调用。"""
    return install(_STATE.get("root") or None, force=True)


def ca_path() -> str:
    """返回当前可用 CA 路径；文件消失时先自愈一次再返回。"""
    p = str(_STATE.get("path") or "")
    if p and _valid_pem(p):
        return p
    return str(refresh().get("path") or "")
