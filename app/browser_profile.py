# -*- coding: utf-8 -*-
"""登录态浏览器配置：持久化 profile 目录 + 有头模式。

为什么需要（2026-09-23 实测）：
  抖音 / 小红书的「用户主页作品列表」接口需要浏览器自己生成的风控参数
  （抖音 Uifid / a_bogus、小红书 x-s）。**我们不去伪造签名**，而是让页面自己的 JS
  去请求、我们只拦截响应（渲染层的 JSON 捕获已经在做这件事）。
  但这条路有两个硬前提：
    1) 一个**已登录**的浏览器配置：未登录时主页只给登录墙或空壳；
    2) **有头模式**：实测无头浏览器打开抖音主页得到的是空壳（标题为空、0 个 video），
       换成有头窗口后同一个地址一次拿到 3 页接口响应、1438 条视频地址。

  所以这里提供「持久化配置目录 + 是否显示窗口」两个开关，由 app.main 注册读取入口，
  renderer 按需取用（与 cookies 同一套 provider 模式，避免模块间循环依赖）。
"""
from __future__ import annotations

import os
from pathlib import Path

_provider = None            # () -> dict: {"dir": str, "headed": bool}


def set_provider(fn) -> None:
    global _provider
    _provider = fn


def _from_store() -> dict:
    """没注册 provider 时的兜底：直接读 config.json。

    为什么必须有这层兜底：provider 原本只在 app.main 里注册，于是
    「独立脚本 / 诊断工具 / 测试」里渲染器根本拿不到这份配置 ——
    表现为「设置明明配了登录态目录，却仍然走无头一次性上下文」，
    而且很难察觉（实测本轮就因此白跑两次）。这里直接读配置，任何入口都一致。
    """
    try:
        from .store import load
        cfg = load()
        return {"dir": cfg.get("browser_user_data_dir") or "",
                "headed": bool(cfg.get("browser_headed"))}
    except Exception:  # noqa: BLE001
        return {"dir": "", "headed": False}


def _cfg() -> dict:
    if not _provider:
        return _from_store()
    try:
        d = _provider() or {}
        return {"dir": str(d.get("dir") or "").strip(),
                "headed": bool(d.get("headed"))}
    except Exception:  # noqa: BLE001
        return {"dir": "", "headed": False}


def user_data_dir() -> str:
    """持久化浏览器配置目录（空 = 不用，退回一次性上下文）。"""
    return _cfg()["dir"]


def headed() -> bool:
    """是否显示浏览器窗口（部分平台会识别无头浏览器并返回空壳）。"""
    return _cfg()["headed"]


def enabled() -> bool:
    return bool(user_data_dir())


def info() -> dict:
    """给界面/诊断用：当前配置是否有效。"""
    d = user_data_dir()
    if not d:
        return {"enabled": False, "dir": "", "headed": headed(),
                "exists": False, "has_default_profile": False}
    p = Path(d).expanduser()
    return {
        "enabled": True,
        "dir": str(p),
        "headed": headed(),
        "exists": p.is_dir(),
        # Chromium 把登录态放在 <profile>/Default 下；有它才说明是「用过的」配置
        "has_default_profile": (p / "Default").is_dir() or (p / "Default").exists(),
    }


def default_login_dir() -> str:
    """建议的登录态目录（放在 DATA_ROOT 下，避免污染用户浏览器与程序目录）。"""
    from .config import DATA_ROOT  # 局部导入避免环

    return str(DATA_ROOT / "browser_profile")

def lock_state(profile_dir: str) -> str:
    """配置目录的占用状态：free（无锁）/ stale（有锁但没人持有）/ held（正被占用）。

    判定方式刻意用「**能不能删掉这个 lockfile**」：
    Chrome 在 Windows 上把 lockfile 以**独占句柄**方式打开（大小 0、读都会
    PermissionError），所以「删不掉」= 真的有浏览器在用它；
    「能删掉」= 上次崩溃/被强杀残留的陈旧锁，直接清掉即可。
    这比按 PID 猜测可靠，也不需要额外依赖。
    """
    lk = Path(profile_dir) / "lockfile"
    if not lk.exists():
        return "free"
    try:
        lk.unlink()
        return "stale"
    except OSError:
        return "held"


def _chrome_holding(profile_dir: str) -> bool:
    """是否有浏览器进程正在占用这个配置目录。"""
    return lock_state(profile_dir) == "held"


def clear_stale_lock() -> bool:
    """清理陈旧锁（能删掉的 lockfile 说明没人持有）。返回是否清理了。"""
    d = user_data_dir()
    if not d:
        return False
    return lock_state(str(Path(d).expanduser())) == "stale"


def _chrome_holding(profile_dir: str) -> bool:
    """是否有浏览器进程正在占用这个配置目录（Windows 用 tasklist + wmic 替代方案）。

    不引入 psutil：本机没装，而读进程命令行用标准库做不到 —— 退一步：
    只判断 lockfile 是否存在，并由调用方在「渲染失败」时提示用户关闭窗口。
    这里做的是**保守**处理：只要看到 lockfile 就认为可能被占用，不主动删。
    """
    return (Path(profile_dir) / "lockfile").is_file()


def ensure_usable() -> dict:
    """启动前检查：配置目录是否存在、是否可能被占用。

    返回 {"ok": bool, "reason": str}。ok=False 时渲染层会直接给出可读原因，
    而不是让页面静默渲染成空壳（实测：目录被上次残留窗口占用时，
    页面能启动但拿不到任何内容，错误信息只有 "render-failed"，无法定位）。
    """
    d = user_data_dir()
    if not d:
        return {"ok": True, "reason": ""}
    p = Path(d).expanduser()
    if not p.is_dir():
        return {"ok": False,
                "reason": f"登录态配置目录不存在：{p}（请填一个已登录过的浏览器配置目录，"
                          "或先用「打开浏览器去登录」创建它）"}
    # ⚠️ 这里**只判断目录存在性**，不用 lockfile 阻断启动。
    #
    # 为什么不阻断（2026-09-23 实测教训）：
    #   1) 本机沙箱会对「删除文件」做安全拦截，用「能不能删掉 lockfile」判断占用会
    #      把可用的目录误判成被占用，直接卡住渲染；
    #   2) 更稳的做法是「先试」，真渲染失败时再把 lockfile 这条线索给用户
    #      （渲染层的失败分支已经会补这句提示）。
    return {"ok": True, "reason": ""}
