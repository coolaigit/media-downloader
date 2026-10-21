# -*- coding: utf-8 -*-
"""并发治理：按「平台 × 任务类型」独立升降速。

V2.5 的并发是**静态参数**：
- 抖音 ≥100 条硬降成 2 路（`main.py:896`）
- B站元数据/缩略图硬限 4 路（`profile_sources.py:1092`）
- 视频下载全局 3 路（`task_manager.py:77`）
稳是稳了，但属于因噎废食：抖音触发风控时 B站/微博/YouTube 一起被拖慢，
而且降下去就再也回不来，除非重启程序。

V3.0 改成：**用户设置的是上限，实际并发由这里按实时反馈调节。**

三条硬约束（都来自真实翻车）：
1. **平台隔离**：抖音 412 只降抖音，不许连累其它平台。
2. **类型隔离**：B站缩略图 API 限流，只降 B站 thumbnail，不许把 B站视频下载也降下来。
3. **不许永久停留低并发**：连续稳定成功后必须逐级升回去。

几个刻意的取舍（别改，改了会重蹈覆辙）：

- **普通网络错误不降速**。代理抽一下、家里网抖一下就把并发砍半，
  会让「稳定后自动恢复」变成永远在恢复的路上。只有平台明确的频控/验证信号才算。
- **降速不设冷却，升速设冷却**。连续 412 说明还没降够，必须能继续降；
  反过来风控刚过去就猛升，等于自己再撞一次。
- **降速不杀在途请求**。capacity 变小只是不再放新名额，已经在跑的自然结束 ——
  强行中断会把半截文件留在下载目录里。
- **已知脆弱组合保守起步**（抖音长批次、B站缩略图），稳定后再自己升到用户设的上限。
  这不是「限死」，是「别一上来就忘掉历史教训」。
"""
from __future__ import annotations

import threading
import time

# ---------- 信号 ----------

OK = "ok"                  # 成功
RATE_LIMIT = "rate_limit"  # 412 / 429 / fresh cookies / 明确频控
VERIFY = "verify"          # 验证码 / 滑块 / 安全验证 / verify you are human
TIMEOUT = "timeout"        # 超时（单次不算，连续两次才降）
NETWORK = "network"        # 普通网络错误（代理抖动、DNS）—— 不降速也不计成功

SIGNAL_LABEL = {
    OK: "正常",
    RATE_LIMIT: "平台限流",
    VERIFY: "人机验证",
    TIMEOUT: "请求超时",
    NETWORK: "网络异常",
}

_VERIFY_PATTERNS = (
    "captcha", "验证码", "滑块", "滑动验证", "安全验证", "人机验证",
    "verify you are human", "verify you're human", "are you a robot",
    "i'm not a robot", "challenge-platform", "cf-challenge", "turnstile",
    "just a moment", "checking your browser", "访问过于频繁", "请完成验证",
)

_RATE_LIMIT_PATTERNS = (
    "http 412", "http error 412", "412 precondition", "status 412", " 412",
    "http 429", "http error 429", "too many requests", "429 too many",
    "fresh cookies", "rate limit", "ratelimit", "rate-limit",
    "请求过于频繁", "请求太快", "访问频率", "操作频繁", "被限流", "限流",
    "too many attempts", "请求被拒绝", "风控",
)

_TIMEOUT_PATTERNS = (
    "timeout", "timed out", "超时", "readtimeout", "connecttimeout",
    "read timed out", "connection timed out",
)

_NETWORK_PATTERNS = (
    "proxyerror", "connectionerror", "connection aborted", "connection reset",
    "remote end closed", "ssl", "name or service not known", "getaddrinfo",
    "temporary failure in name resolution", "网络错误", "连接被重置",
    "nodename nor servname", "errno 11001", "errno 10054",
)


def classify_error(text: str) -> str:
    """把一条错误文本归类成信号。认不出来的一律当 NETWORK（不降速）。

    宁可漏降，不可误降：把「403 防盗链」或「图片 404」错当成频控去降速，
    表现就是下几张图突然变成龟速，用户完全不知道为什么。
    """
    low = str(text or "").lower()
    if not low.strip():
        return OK
    # 验证类优先：验证码页面里经常同时出现 412/限流字样，得先认出来
    for pat in _VERIFY_PATTERNS:
        if pat in low:
            return VERIFY
    for pat in _RATE_LIMIT_PATTERNS:
        if pat in low:
            return RATE_LIMIT
    for pat in _TIMEOUT_PATTERNS:
        if pat in low:
            return TIMEOUT
    for pat in _NETWORK_PATTERNS:
        if pat in low:
            return NETWORK
    return NETWORK


# ---------- 单个闸门 ----------

class AdaptiveGate:
    """容量可调的闸门 + 自适应升降速。

    用法：
        if gate.acquire(cancelled=...):
            try:
                ...发请求...
                gate.release(ok=True)
            except ...:
                gate.release(signal=classify_error(msg))

    线程池那边按**用户上限**建 worker，每个任务进来先 acquire。
    降速 = 缩小 capacity，多出来的 worker 会阻塞在 acquire，不会重建线程池、
    也不会打断在途请求。
    """

    def __init__(self, name: str, ceiling: int = 4, *, floor: int = 1,
                 start_at: int | None = None, recover_after: int = 6,
                 cooldown: float = 20.0):
        self._cv = threading.Condition()
        self._name = name
        self._floor = max(1, int(floor))
        self._ceiling = max(self._floor, int(ceiling or 1))
        # 初始容量：默认直接按上限跑；已知脆弱的组合由调用方给更保守的 start_at
        self._capacity = self._clamp(start_at if start_at else self._ceiling)
        self._running = 0
        self._waiting = 0
        self._streak = 0            # 连续成功次数（升速依据）
        self._soft_fail = 0         # 连续超时次数
        self._recover_after = max(1, int(recover_after))
        self._cooldown = max(0.0, float(cooldown))
        self._last_drop = 0.0
        self._state = "healthy"
        self._reason = ""
        self._drops = 0
        self._raises = 0

    # ---- 内部 ----

    def _clamp(self, n: int) -> int:
        return max(self._floor, min(int(n), self._ceiling))

    # ---- 属性 ----

    @property
    def name(self) -> str:
        return self._name

    @property
    def capacity(self) -> int:
        with self._cv:
            return self._capacity

    @property
    def ceiling(self) -> int:
        with self._cv:
            return self._ceiling

    def set_ceiling(self, n: int) -> int:
        """用户改「最大并发」时调用。立刻生效，等待中的线程会被唤醒。"""
        with self._cv:
            self._ceiling = max(self._floor, min(int(n or 1), 32))
            if self._capacity > self._ceiling:
                self._capacity = self._ceiling
            self._cv.notify_all()
            return self._ceiling

    # ---- 名额 ----

    def acquire(self, cancelled=None) -> bool:
        """等到有名额再进入。返回 False 表示等待期间被取消。

        上限 0.2s 的等待是为了能及时看到「容量被调大」与「被取消」——
        用无限期 wait 的话，容量调整要等到下一次 release 才生效。
        """
        with self._cv:
            self._waiting += 1
            try:
                while self._running >= self._capacity:
                    if cancelled is not None and cancelled():
                        return False
                    self._cv.wait(timeout=0.2)
                    # 容量可能在等待期间被调大，这里每轮重新判断
                self._running += 1
                return True
            finally:
                self._waiting -= 1

    def release(self, signal: str | None = OK) -> None:
        """释放名额。signal=None 表示「纯释放」——不反馈成功也不反馈失败
        （用户取消、任务被移除这类情况不该影响升降速判断）。"""
        with self._cv:
            self._running = max(0, self._running - 1)
            self._feed(signal)
            self._cv.notify_all()

    # ---- 升降速 ----

    def _feed(self, signal: str | None) -> None:
        if signal is None:
            return
        now = time.time()
        if signal in (RATE_LIMIT, VERIFY):
            self._drop(signal, now)
            return
        if signal == TIMEOUT:
            # 单次超时可能只是网络抖动；连续两次才认定为「这条路不稳」
            self._soft_fail += 1
            if self._soft_fail >= 2:
                self._soft_fail = 0
                self._drop(signal, now)
            return
        if signal == NETWORK:
            # 既不算成功也不算失败：本地网络问题不该影响对平台的判断
            return

        # ---- 成功 ----
        self._soft_fail = 0
        if self._capacity >= self._ceiling:
            self._streak = 0
            self._state = "healthy"
            return
        if now - self._last_drop < self._cooldown:
            self._streak = 0      # 冷却期内攒不了 streak，避免刚降完就往回冲
            return
        self._streak += 1
        if self._streak >= self._recover_after:
            self._streak = 0
            self._capacity = self._clamp(min(self._ceiling, self._capacity * 2))
            self._raises += 1
            if self._capacity >= self._ceiling:
                self._state = "healthy"
                self._reason = ""
            else:
                self._state = "recovering"

    def _drop(self, signal: str, now: float) -> None:
        self._last_drop = now
        self._streak = 0
        self._state = "throttled"
        self._reason = signal
        new_cap = self._clamp(self._capacity // 2)
        if new_cap < self._capacity:
            self._capacity = new_cap
            self._drops += 1
        self._cv.notify_all()

    # ---- 观测 ----

    def snapshot(self) -> dict:
        with self._cv:
            return {
                "name": self._name,
                "ceiling": self._ceiling,
                "capacity": self._capacity,
                "running": self._running,
                "waiting": self._waiting,
                "state": self._state,
                "reason": self._reason,
                "reason_label": SIGNAL_LABEL.get(self._reason, ""),
                "streak": self._streak,
                "recover_after": self._recover_after,
                "cooldown_s": self._cooldown,
                "drops": self._drops,
                "raises": self._raises,
            }

    def stats(self) -> dict:
        """给测试/诊断用的精简计数。"""
        with self._cv:
            return {"capacity": self._capacity, "ceiling": self._ceiling,
                    "running": self._running, "state": self._state,
                    "reason": self._reason, "drops": self._drops,
                    "raises": self._raises}


# ---------- 治理器 ----------

# 任务类型。分开的理由：它们的底层限流来源完全不同 ——
# 缩略图走 view API、视频走 playurl/解析、图片走 CDN 直链，
# 某一种被限流不该连坐另一种。
KIND_LIST_CRAWL = "list_crawl"        # 列表页抓取（主页作品列表、翻页）
KIND_METADATA = "metadata"            # 元数据补全（标题、时长、作者）
KIND_THUMBNAIL = "thumbnail"          # 缩略图/封面
KIND_VIDEO_RESOLVE = "video_resolve"  # 视频地址解析（yt-dlp 提取信息）
KIND_VIDEO_DOWNLOAD = "video_download"
KIND_IMAGE_DOWNLOAD = "image_download"
KIND_STREAM_DOWNLOAD = "stream_download"   # m3u8/mpd 流

ALL_KINDS = (KIND_LIST_CRAWL, KIND_METADATA, KIND_THUMBNAIL, KIND_VIDEO_RESOLVE,
             KIND_VIDEO_DOWNLOAD, KIND_IMAGE_DOWNLOAD, KIND_STREAM_DOWNLOAD)

KIND_LABEL = {
    KIND_LIST_CRAWL: "列表抓取",
    KIND_METADATA: "元数据",
    KIND_THUMBNAIL: "缩略图",
    KIND_VIDEO_RESOLVE: "视频解析",
    KIND_VIDEO_DOWNLOAD: "视频下载",
    KIND_IMAGE_DOWNLOAD: "图片下载",
    KIND_STREAM_DOWNLOAD: "流下载",
}

# 已知脆弱组合的**起步容量**（不是硬上限！稳定后会自动升到用户设的上限）。
# 这些数字来自 V2.5 的真实翻车：
#   抖音几百条连续解析 → fresh cookies / 412；
#   B站 view API 16 路并发 → 整批空缩略图；
#   流媒体单路就能吃满带宽，多开收益低、失败重传代价高。
_START_AT = {
    ("dy", KIND_VIDEO_DOWNLOAD): 2,
    ("dy", KIND_VIDEO_RESOLVE): 2,
    ("dy", KIND_LIST_CRAWL): 2,
    ("bili", KIND_METADATA): 4,
    ("bili", KIND_THUMBNAIL): 4,
}

# 每类任务的默认上限（settings 没给值时用）
_DEFAULT_CEILING = {
    KIND_LIST_CRAWL: 4,
    KIND_METADATA: 6,
    KIND_THUMBNAIL: 8,
    KIND_VIDEO_RESOLVE: 4,
    KIND_VIDEO_DOWNLOAD: 3,
    KIND_IMAGE_DOWNLOAD: 6,
    KIND_STREAM_DOWNLOAD: 4,
}

# 各类型的冷却/恢复节奏：视频单条耗时长，用「连续几个成功」当恢复依据会比
# 图片下载苛刻得多（下 3 个视频可能要几分钟），所以按任务粒度分别设。
_RECOVER_AFTER = {
    KIND_VIDEO_DOWNLOAD: 3,
    KIND_VIDEO_RESOLVE: 3,
    KIND_STREAM_DOWNLOAD: 3,
    KIND_LIST_CRAWL: 4,
}
_DEFAULT_RECOVER_AFTER = 6


class ConcurrencyGovernor:
    """按 (platform, kind) 管一堆 AdaptiveGate。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._gates: dict[tuple[str, str], AdaptiveGate] = {}
        # 用户在设置页改的是「这一类任务的上限」，对所有平台生效；
        # 记下默认值，之后新建的闸门（换了平台）也按这个上限建。
        self._default_ceiling = dict(_DEFAULT_CEILING)

    def gate(self, platform: str, kind: str, *, ceiling: int | None = None) -> AdaptiveGate:
        key = (str(platform or "other"), str(kind or "other"))
        with self._lock:
            g = self._gates.get(key)
            if g is not None:
                if ceiling:
                    g.set_ceiling(ceiling)
                return g
            cap = int(ceiling or self._default_ceiling.get(key[1], 4))
            g = AdaptiveGate(
                name=f"{key[0]}/{key[1]}",
                ceiling=cap,
                start_at=_START_AT.get(key),
                recover_after=_RECOVER_AFTER.get(key[1], _DEFAULT_RECOVER_AFTER),
            )
            self._gates[key] = g
            return g

    def set_ceiling_for_kind(self, kind: str, n: int) -> int:
        """设置某一类任务的上限（所有平台同时生效）。"""
        n = max(1, min(int(n or 1), 32))
        with self._lock:
            self._default_ceiling[kind] = n
            for (plat, k), g in self._gates.items():
                if k == kind:
                    g.set_ceiling(n)
        return n

    def ceiling_for_kind(self, kind: str) -> int:
        with self._lock:
            return int(self._default_ceiling.get(kind, 4))

    def gate_for_url(self, url: str, kind: str, *, ceiling: int | None = None) -> AdaptiveGate:
        return self.gate(_platform_of(url), kind, ceiling=ceiling)

    def set_ceiling(self, platform: str, kind: str, n: int) -> int:
        return self.gate(platform, kind).set_ceiling(n)

    def snapshot(self) -> dict:
        with self._lock:
            items = [g.snapshot() for g in self._gates.values()]
        # 只把「正在用」或「被降过速」的排前面，UI 上不用翻一堆空闸门
        items.sort(key=lambda x: (0 if x["running"] or x["waiting"] else 1,
                                  x["state"] != "healthy", x["name"]))
        return {
            "gates": items,
            "throttled": [x for x in items if x["state"] == "throttled"],
        }

    def reset(self) -> None:
        """测试用：清空全部闸门。"""
        with self._lock:
            self._gates.clear()


def _platform_of(url: str) -> str:
    """从地址判断平台。延迟 import 避免与 platform_login 循环依赖。"""
    try:
        from . import platform_login
        return platform_login.platform_for_url(url) or "other"
    except Exception:  # noqa: BLE001
        return "other"


GOVERNOR = ConcurrencyGovernor()
