# -*- coding: utf-8 -*-
"""影视资源站（苹果 CMS / MacCMS 采集接口）检索层。

用途：给「会员视频兜底」提供**换源**能力 —— 用户贴一个优酷/爱奇艺/B 站大会员的
页面地址，我们拿页面标题去资源站找同一部片/同一集，直接取它的 m3u8 下载。
这条链路和原平台的会员校验完全无关，因此不受「试看 6 分钟」限制。

为什么用采集接口而不是解析接口（2026-11-04 实测）：
  - 20 个公共「VIP 解析」站里，能返回播放器 iframe 的只有 3 个，且全是广告页，
    真正吐出 m3u8 的 **0 个**（`/images/async` 那套老接口早就死了）。
  - 同样的 20 个影视资源站里，10 个采集接口正常返回 JSON，`vod_play_url`
    直接带 m3u8（实测 ikun / jisu / hongniu 的播放列表还能下到分片）。
所以「换源」是当前唯一还活着的大批量路线，解析接口只留作可选补充。

接口协议（MacCMS 10 通用）：
  GET {api}?ac=detail&wd=<关键词>
  → {"code":1,"list":[{vod_id,vod_name,vod_year,vod_remarks,
                        vod_play_from:"a$$$b", vod_play_url:"第01集$url#第02集$url"}]}
  vod_play_from 与 vod_play_url 用 `$$$` 分组一一对应；组内 `#` 分集，分集用 `$` 分「名$地址」。
"""
from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from urllib.parse import quote, urlparse

import requests

from .config import HEADERS, TIMEOUT

# 只放实测能返回 JSON 的站点（2026-11-04 全量探测：20 个里 10 个可用）。
# 顺序按「搜索结果准确度 + 播放链可下载率」排，靠前的先试。
# ⚠️ hongniu 排最后：它的 hn.bfvvs.com 证书链在 ffmpeg（GnuTLS）里过不了校验，
#    而 requests/certifi 能过 —— 同一个源在两条链路上表现不一致，别让它排前面。
SITES: dict[str, dict] = {
    "ikun": {"name": "艾坤资源", "api": "https://ikunzyapi.com/api.php/provide/vod/",
             "referer": "https://ikunzy.com/"},
    "jisu": {"name": "极速资源", "api": "https://jszyapi.com/api.php/provide/vod/",
             "referer": "https://jisuzy.com/"},
    "apibdzy": {"name": "百度资源", "api": "https://api.apibdzy.com/api.php/provide/vod/",
                "referer": "https://www.apibdzy.com/"},
    "dytt": {"name": "电影天堂", "api": "https://caiji.dyttzyapi.com/api.php/provide/vod/",
             "referer": "https://www.dyttzyapi.com/"},
    "360zy": {"name": "360 资源", "api": "https://360zy.com/api.php/provide/vod/",
              "referer": "https://360zy.com/"},
    "ffzy": {"name": "非凡资源", "api": "https://cj.ffzyapi.com/api.php/provide/vod/",
             "referer": "https://ffzy.tv/",
             "mirrors": ["https://api.ffzyapi.com/api.php/provide/vod/"]},
    "bfzy": {"name": "暴风资源", "api": "https://bfzyapi.com/api.php/provide/vod/",
             "referer": "https://bfzy.tv/"},
    "lzi": {"name": "量子资源", "api": "https://cj.lziapi.com/api.php/provide/vod/",
            "referer": "https://lzizy.com/"},
    "wujin": {"name": "无尽资源", "api": "https://api.wujinapi.me/api.php/provide/vod/",
              "referer": "https://wujinzy.com/",
              "mirrors": ["https://api.wujinapi.com/api.php/provide/vod/"]},
    "ruyi": {"name": "如意资源", "api": "https://cj.rycjapi.com/api.php/provide/vod/",
             "referer": "https://ruyizy.com/"},
    "zuidazy": {"name": "最大资源", "api": "https://api.zuidapi.com/api.php/provide/vod/",
                "referer": "https://zuidazy.com/"},
    "hongniu": {"name": "红牛资源", "api": "https://www.hongniuzy2.com/api.php/provide/vod/",
                "referer": "https://www.hongniuzy2.com/"},
}

_CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}

_EP_PATTERNS = [
    re.compile(r"第\s*([0-9]{1,5})\s*[集话話期回章]"),
    re.compile(r"第\s*([零〇一二两三四五六七八九十百]{1,6})\s*[集话話期回章]"),
    # S02EP06 / EP06 / E06：YouTube 标题里常见「… S02EP06」，EP 前没有分隔符
    re.compile(r"[Ee][Pp]\s*([0-9]{1,4})"),
    re.compile(r"(?:^|[\s\[\(（_-])[Ee]\s*([0-9]{1,4})(?:[^0-9]|$)"),
    re.compile(r"(?:^|[\s\[\(（_-])([0-9]{1,4})\s*[集话話期](?:[^0-9]|$)"),
]

# 发布版常见的噪音，检索前必须去掉，否则资源站匹配不上
_NOISE_WORDS = (
    "在线观看", "高清完整版", "完整版", "未删减", "无删减", "国语版", "粤语版",
    "中字版", "中文字幕", "高清", "超清", "蓝光", "4K", "1080P", "720P", "HD",
    "HDTV", "WEB-DL", "免费观看", "全集", "更新至", "抢先版", "预告片", "花絮",
)
_NOISE_RE = re.compile("|".join(re.escape(w) for w in _NOISE_WORDS), re.I)
_BRACKET_RE = re.compile(r"[《》【】\[\]（）()「」『』<>〈〉]")


@dataclass
class Episode:
    name: str
    url: str


@dataclass
class Candidate:
    site: str
    site_name: str
    vod_name: str
    year: str
    remarks: str
    score: float
    groups: list[tuple[str, list[Episode]]] = field(default_factory=list)

    def pick(self, episode: int | None) -> Episode | None:
        """按集数挑一条播放地址；挑不到返回 None。"""
        # 预告片/花絮对不上正片集号 —— 用户要第 5 集时不能拿预告顶上
        if episode is not None and ("预告" in (self.remarks or "")
                                    or "预告" in (self.vod_name or "")):
            return None
        # 优先 m3u8 组（其它组多是网盘/外链，下载不可控）
        ranked = sorted(self.groups, key=lambda g: (0 if "m3u8" in g[0].lower() else 1))
        for _from, eps in ranked:
            eps = [e for e in eps if e.url]
            if not eps:
                continue
            if episode is not None:
                named = [e for e in eps if parse_episode(e.name) is not None]
                for e in named:
                    if parse_episode(e.name) == episode:
                        return e
                if named:
                    # 这组有明确集号但没有目标集 → 换下一组，别拿第 1 集充数
                    continue
                # 名称里没有集号：单片/整部一个地址，只能取这条
                if len(eps) == 1:
                    return eps[0]
                if len(eps) >= episode >= 1:
                    return eps[episode - 1]
                continue
            return eps[0]
        return None


# ---------------------------------------------------------------- 文本工具

def cn_to_int(text: str) -> int | None:
    """把「十二」「一百零三」这类中文数字转成 int（够用即可，不追求完整语法）。"""
    s = (text or "").strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    total, section, num = 0, 0, 0
    for ch in s:
        if ch in _CN_DIGITS:
            num = _CN_DIGITS[ch]
        elif ch == "十":
            section += (num or 1) * 10
            num = 0
        elif ch == "百":
            section += (num or 1) * 100
            num = 0
        else:
            return None
    total = section + num
    return total or None


def parse_episode(text: str) -> int | None:
    """从标题/分集名里抽集号：第12集 / 第12话 / EP12 / E12 / 12集。"""
    t = text or ""
    for pat in _EP_PATTERNS:
        for m in pat.finditer(t):
            raw = m.group(1)
            n = cn_to_int(raw) if not raw.isdigit() else int(raw)
            if n and 0 < n <= 9999:
                return n
    return None


_SEASON_PATTERNS = (
    re.compile(r"第\s*([0-9零〇一二两三四五六七八九十]{1,4})\s*[季部]"),
    re.compile(r"[Ss]eason\s*([0-9]{1,2})"),
    re.compile(r"(?:^|[^A-Za-z])S([0-9]{1,2})(?![0-9])"),
)


def parse_season(text: str) -> int | None:
    """从标题里抽季号：第二季 / 第2部 / Season 2 / S02。"""
    t = text or ""
    for pat in _SEASON_PATTERNS:
        m = pat.search(t)
        if not m:
            continue
        raw = m.group(1)
        n = cn_to_int(raw) if not raw.isdigit() else int(raw)
        if n and 0 < n <= 99:
            return n
    return None


def clean_title(text: str, keep_season: bool = False) -> str:
    """去掉片名里对检索无益的部分，保留可用于匹配的主标题。

    keep_season=True 时保留「第X季 / Season N」—— 资源站按季分条目，
    检索词里丢掉季号会直接搜到第一季（实测《人间世 第二季》被匹配成第一季）。
    """
    t = _BRACKET_RE.sub(" ", text or "")
    t = _NOISE_RE.sub(" ", t)
    # 去掉「第X集」「更新至第X集」这类后缀；季号按需保留
    t = re.sub(r"第\s*[0-9零〇一二两三四五六七八九十百]{1,6}\s*[集话話期回]", " ", t)
    if not keep_season:
        t = re.sub(r"第\s*[0-9零〇一二两三四五六七八九十百]{1,6}\s*[季部]", " ", t)
        t = re.sub(r"[Ss]eason\s*[0-9]{1,2}", " ", t)
    t = re.sub(r"(20|19)\d{2}\s*年?", " ", t)
    t = re.sub(r"\s*[-_|·].*$", " ", t)      # 「片名 - 优酷」这种尾巴
    t = re.sub(r"[^\w\u4e00-\u9fff]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def guess_year(text: str) -> str:
    m = re.search(r"(19|20)\d{2}", text or "")
    return m.group(0) if m else ""


_LANG_BUCKETS = (
    (("中配", "国语", "普通话", "国配"), "cn"),
    (("粤语", "粤配"), "yue"),
    (("台配", "台湾配音"), "tw"),
    (("日语", "原声", "日配"), "jp"),
    (("英语", "英配"), "en"),
)


def lang_bucket(text: str) -> str:
    """片名里的语言标记归一：中配/国语 -> cn，粤语 -> yue，台配 -> tw …"""
    t = text or ""
    for words, bucket in _LANG_BUCKETS:
        if any(w in t for w in words):
            return bucket
    return ""


def _norm(name: str) -> str:
    t = clean_title(name)
    return t.replace(" ", "").lower()


def similarity(a: str, b: str) -> float:
    """片名相似度 0~100。完全一致 100；包含关系 70~95；其余按序列相似度。"""
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 100.0
    if na in nb or nb in na:
        short, long_ = (na, nb) if len(na) <= len(nb) else (nb, na)
        return 70.0 + 25.0 * (len(short) / max(1, len(long_)))
    return SequenceMatcher(None, na, nb).ratio() * 65.0


def _split_groups(play_from: str, play_url: str) -> list[tuple[str, list[Episode]]]:
    froms = [x.strip() for x in (play_from or "").split("$$$")]
    groups_raw = (play_url or "").split("$$$")
    out: list[tuple[str, list[Episode]]] = []
    for i, raw in enumerate(groups_raw):
        frm = froms[i] if i < len(froms) else f"line{i}"
        eps: list[Episode] = []
        for chunk in raw.split("#"):
            chunk = chunk.strip()
            if not chunk:
                continue
            if "$" in chunk:
                name, _, url = chunk.partition("$")
            else:
                name, url = "", chunk
            url = url.strip()
            if url.startswith(("http://", "https://")):
                eps.append(Episode(name.strip(), url))
        if eps:
            out.append((frm, eps))
    return out


# ---------------------------------------------------------------- 站点检索

def _search_one(key: str, keyword: str, timeout: int, attempts: int = 1) -> list[Candidate]:
    site = SITES[key]
    params = {"ac": "detail", "wd": keyword}
    last: Exception | None = None
    data = None
    # 主域名失败时依次试镜像（同一套 CMS 数据的不同入口）。
    # 只对「连接失败/超时」换镜像；返回 200 但没有结果就认这个站没片。
    endpoints = [site["api"], *(site.get("mirrors") or [])]
    for base in endpoints:
        for _ in range(max(1, attempts)):
            try:
                r = requests.get(base, params=params,
                                 headers={**HEADERS, "Accept": "application/json,text/plain,*/*",
                                          "Referer": site.get("referer") or ""},
                                 timeout=timeout)
                if r.status_code != 200:
                    last = RuntimeError(f"HTTP {r.status_code}")
                    break
                data = r.json()
                break
            except (requests.RequestException, ValueError) as e:
                last = e
        if data is not None:
            break
    if data is None:
        raise last if last else RuntimeError("请求失败")

    out: list[Candidate] = []
    for v in (data.get("list") or [])[:20]:
        groups = _split_groups(v.get("vod_play_from") or "", v.get("vod_play_url") or "")
        if not groups:
            continue
        out.append(Candidate(
            site=key, site_name=site["name"],
            vod_name=str(v.get("vod_name") or ""),
            year=str(v.get("vod_year") or ""),
            remarks=str(v.get("vod_remarks") or ""),
            score=0.0,
            groups=groups,
        ))
    return out


def search(keyword: str, *, sites: list[str] | None = None,
           timeout: int = 8, max_workers: int = 6, deadline: float = 10.0,
           enough: int = 30) -> tuple[list[Candidate], dict[str, str]]:
    """并发问多个资源站，返回 (候选列表, 每站错误/状态)。

    deadline：整体截止时间（秒）。资源站里总有挂掉的，等满所有站会拖到 30 秒；
    拿到 enough 条候选、或到了截止时间就提前收工 —— 排序只看片名/年份/集数，
    前几个站的候选已经足够排序。
    """
    keys = [k for k in (sites or list(SITES)) if k in SITES]
    results: list[Candidate] = []
    status: dict[str, str] = {}
    if not keys:
        return [], {"": "没有可用的资源站"}
    started = time.time()
    ex = ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(keys))))
    futs = {ex.submit(_search_one, k, keyword, timeout): k for k in keys}
    try:
        try:
            for fut in as_completed(futs, timeout=deadline):
                k = futs[fut]
                try:
                    got = fut.result()
                    status[k] = f"ok:{len(got)}"
                    results.extend(got)
                except Exception as e:  # noqa: BLE001
                    status[k] = f"{type(e).__name__}: {str(e)[:60]}"
                if len(results) >= enough and len(status) >= 3:
                    break
                if time.time() - started > deadline:
                    break
        except TimeoutError:
            pass
    finally:
        # 不等慢站：关掉线程池但不等在途请求跑完（否则 deadline 形同虚设，
        # 实测 10 个站里有 1 个挂起就能把一次换源拖到 20 秒）
        for f, k in futs.items():
            if not f.done():
                f.cancel()
                status.setdefault(k, "超时")
        ex.shutdown(wait=False, cancel_futures=True)
    return results, status


def rank(candidates: list[Candidate], title: str, year: str = "",
         episode: int | None = None) -> list[Candidate]:
    """按片名/年份/季/集数给候选打分，从高到低排序。"""
    want_year = guess_year(year)
    want_lang = lang_bucket(title)
    want_season = parse_season(title)
    priority = {k: i for i, k in enumerate(SITES)}
    for c in candidates:
        s = similarity(title, c.vod_name)
        cy = guess_year(c.year) or guess_year(c.vod_name)
        if want_year and cy:
            s += 18.0 if cy == want_year else -18.0
        # 语言版本对不上（想看粤语却给台配）比片名轻微差异更致命，重罚
        c_lang = lang_bucket(c.vod_name)
        if want_lang and c_lang and c_lang != want_lang:
            s -= 45.0
        # 季号：要第二季却给第一季 = 完全不同的剧集，必须重罚
        if want_season:
            c_season = parse_season(c.vod_name)
            if c_season and c_season != want_season:
                s -= 60.0
            elif not c_season:
                s -= 8.0
        if episode is not None and c.pick(episode) is not None:
            s += 10.0
        # 备注里写了「全36集」说明是剧集，找剧集时加分
        if episode is not None and re.search(r"[全更]|集", c.remarks or ""):
            s += 4.0
        c.score = round(s, 1)
    # 同分时按站点优先级（越靠前越可信），再按片名长度（更精确的片名优先）
    return sorted(candidates, key=lambda c: (-c.score, priority.get(c.site, 99),
                                             len(c.vod_name)))


def _query_variants(title: str) -> list[str]:
    """检索词降级链：带季号的片名 → 去语言标记 → 主标题。

    实测资源站对「名侦探柯南 中配」这种带语言后缀的查询有时只回 0~3 条，
    去掉「中配/国语/粤语」后再查能稳定命中同一部片（只是音轨可能不同）。
    ⚠️ 第一档必须保留「第X季」：丢掉季号会直接搜到第一季（实测《人间世 第二季》
    被匹配成《人间世第一季》）。
    """
    base = clean_title(title, keep_season=True) or (title or "").strip()
    out = [base] if base else []
    stripped = clean_title(re.sub(r"(中配|国语|粤语|原声|普通话|台配|日语|英语)(版)?", " ", title or ""))
    if stripped and stripped not in out:
        out.append(stripped)
    head = re.split(r"\s+", stripped or base)[0] if (stripped or base) else ""
    if head and len(head) >= 2 and head not in out:
        out.append(head)
    return out[:3]


def resolve(keyword: str, *, title: str = "", year: str = "", episode: int | None = None,
            sites: list[str] | None = None, timeout: int = 12,
            min_score: float = 55.0) -> dict:
    """一站式：搜资源站 → 打分 → 挑出可直接下载的播放地址。

    返回 {ok, url, urls[], referer, site, site_name, vod_name, episode, score,
          candidates, options[], status, error}

    会返回多条候选源（不同资源站），下载端逐条重试 —— 资源站与 CDN 都不稳定，
    只给一条地址等于把「某站今天挂了」直接变成「用户下载失败」。
    """
    queries = _query_variants(keyword)
    if not queries:
        return {"ok": False, "error": "没有可检索的片名", "status": {}}

    cands: list[Candidate] = []
    status: dict[str, str] = {}
    ranked: list[Candidate] = []
    queries_tried: list[str] = []
    for q in queries:
        queries_tried.append(q)
        c, st = search(q, sites=sites, timeout=timeout)
        status.update(st)
        cands.extend(c)
        ranked = rank(cands, title or q, year=year, episode=episode)
        # 已经找到「分数够 + 有这一集」的候选就不再降级查询
        if any(x.score >= min_score and x.pick(episode) is not None for x in ranked[:8]):
            break

    if not ranked:
        errs = "；".join(f"{k}:{v}" for k, v in list(status.items())[:4])
        return {"ok": False, "error": f"资源站没有返回结果（{errs}）", "status": status}

    summary = [{"site": c.site_name, "name": c.vod_name, "year": c.year,
                "remarks": c.remarks, "score": c.score} for c in ranked[:5]]
    # 从高分往下收集**所有**真有这一集的候选，而不是只取第一名：
    # 不同资源站的 CDN 差异很大（实测 hongniu 的 hn.bfvvs.com 证书链 ffmpeg 不认，
    # jisu 的 vv.jisuzyv.com 正常），多备几条源可以让下载端逐条重试。
    options: list[dict] = []
    first_norm = ""
    for top in ranked:
        if top.score < min_score or len(options) >= 5:
            break
        ep = top.pick(episode)
        if ep is None:
            continue
        # 后备源只收「同一个片名」的条目：中配和原声名字只差两个字，
        # 拿原声当后备会让用户下到另一个语言版本（片名归一后比较）。
        norm = _norm(top.vod_name)
        if not first_norm:
            first_norm = norm
        elif norm != first_norm:
            continue
        options.append({
            "url": ep.url, "referer": SITES[top.site].get("referer") or "",
            "site": top.site, "site_name": top.site_name, "vod_name": top.vod_name,
            "episode": parse_episode(ep.name) or episode,
            "episode_name": ep.name, "score": top.score,
        })
    if options:
        first = dict(options[0])
        first.update({"ok": True, "options": options, "candidates": summary,
                      "status": status, "queries": queries_tried})
        return first
    if top_score := (ranked[0].score if ranked else 0):
        return {"ok": False,
                "error": (f"匹配到《{ranked[0].vod_name}》（{ranked[0].score} 分）"
                          f"但没有第 {episode} 集" if episode is not None
                          else f"没有足够匹配的片源（最高分 {top_score}）"),
                "candidates": summary, "status": status, "queries": queries_tried}
    return {"ok": False, "error": "没有足够匹配的片源", "candidates": summary,
            "status": status, "queries": queries_tried}
