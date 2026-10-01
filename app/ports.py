# -*- coding: utf-8 -*-
"""端口决策（1D）：CLI > 环境变量 > 自动扫描 —— 全程序唯一的端口决策点。

为什么存在：此前 bat 扫 8848-8870、ps1 又扫一遍、serve.py 硬编码 8848，
三方各自决定端口必然漂移（bat 挑好的端口 serve.py 不知道）。现在
bat / PowerShell / launcher.py / serve.py 直启 全部经 resolve_port()，
启动器自身只负责把决策结果跑起来。

优先级语义：
  1. CLI `--port N`        —— 显式指定：只准用 N。被本工具实例占用 → 识别为
                              「已在运行」；被其它程序占用 → 明确报错退出，
                              绝不悄悄挪端口（用户点名了端口，挪了就是违背）。
  2. 环境变量 MEDIA_DOWNLOADER_PORT —— 与 CLI 同语义（显式指定）。
  3. 都没有                —— 先扫 8848-8870 找「本工具已有实例」（识别到就
                              打开页面、绝不双实例），再扫空闲端口启动。

已有实例识别：探测 /api/health 并校验 app 标记 —— 不能只看端口有没有人听，
恰好有别的程序占着 8848 时不能误认成自己。
"""
from __future__ import annotations

import json
import os
import socket
import urllib.request

PORT_FIRST = 8848
PORT_LAST = 8870
PORT_RANGE = range(PORT_FIRST, PORT_LAST + 1)
ENV_PORT = "MEDIA_DOWNLOADER_PORT"
ENV_NO_BROWSER = "MEDIA_DOWNLOADER_NO_BROWSER"
APP_MARKER = "MediaDownloader"


def _fetch_health(port: int, timeout: float) -> dict | None:
    """取 127.0.0.1:port/api/health。必须绕开系统代理（本机代理常驻环境变量，
    不绕开的话 127.0.0.1 探测会被代理吃掉——2026-10-01 实测）。"""
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{port}/api/health", timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001 —— 探测失败一律按「不是本工具」处理
        return None


def probe_running(port: int, timeout: float = 1.0) -> bool:
    """该端口上是否跑着**本工具**的实例（健康检查 + app 标记双重确认）。"""
    data = _fetch_health(port, timeout)
    return bool(data) and data.get("app") == APP_MARKER and data.get("ok") is True


def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def resolve_port(cli_port: int | None = None) -> tuple[str, int | None]:
    """统一端口决策。返回 (mode, port)：

    - ("serve", p)       —— 在 p 上启动服务
    - ("running", p)     —— p 上已有本工具实例（调用方应打开页面后退出，绝不双实例）
    - ("busy-foreign", p) —— 显式指定（CLI/env）的端口被**其它程序**占用
    - ("exhausted", None) —— 8848-8870 全部被占
    """
    explicit = cli_port
    if explicit is None:
        env = os.environ.get(ENV_PORT, "").strip()
        if env.isdigit() and 0 < int(env) < 65536:
            explicit = int(env)

    if explicit is not None:  # 显式指定：只用它，成败分明
        if probe_running(explicit):
            return ("running", explicit)
        if port_free(explicit):
            return ("serve", explicit)
        return ("busy-foreign", explicit)

    # 默认：先认领已有实例，再找空闲端口
    for p in PORT_RANGE:
        if probe_running(p):
            return ("running", p)
    for p in PORT_RANGE:
        if port_free(p):
            return ("serve", p)
    return ("exhausted", None)


class InvalidPortError(ValueError):
    """用户显式给了非法端口（--port abc / 0 / 65536 / 缺值）。"""


def parse_launch_flags(argv: list[str]) -> tuple[int | None, bool]:
    """解析顶层启动旗标（launcher.py / serve.py 直启共用，校验只此一处）。

    支持：--port N / --port=N / -p N / --no-browser。
    环境变量 MEDIA_DOWNLOADER_NO_BROWSER=1 同样关闭自动开页面
    （自动化/测试场景用，避免无头环境真去拉浏览器）。

    返回 (port, open_browser)，port=None 表示用户没有指定端口。
    用户指定了**非法**端口（非数字 / 0 / 65536+ / 缺值）→ 抛 InvalidPortError，
    调用方必须 exit 2 并给出明确错误 —— 绝不允许静默回退到自动扫描 8848
    （1D.1：launcher.py 与 serve.py 对非法端口行为完全一致）。
    """
    port: int | None = None
    port_given = False
    open_browser = os.environ.get(ENV_NO_BROWSER, "").strip() not in ("1", "true", "yes")
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--port", "-p"):
            port_given = True
            if i + 1 >= len(argv):
                raise InvalidPortError(f"{a} 缺少端口号")
            port = _to_port(argv[i + 1])
            i += 2
            continue
        if a.startswith("--port="):
            port_given = True
            port = _to_port(a.split("=", 1)[1])
            i += 1
            continue
        if a == "--no-browser":
            open_browser = False
            i += 1
            continue
        i += 1
    if port_given and port is None:
        raise InvalidPortError("端口必须是 1-65535 的数字")
    return port, open_browser


def _to_port(text: str) -> int | None:
    text = text.strip()
    if text.isdigit():
        n = int(text)
        if 0 < n < 65536:
            return n
    return None
