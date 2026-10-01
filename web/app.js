/* 媒体下载工具 - 前端交互逻辑 */
'use strict';

const $ = (id) => document.getElementById(id);
const API = '';  // 同源

// ---------- 全局状态 ----------
let searchItems = [];        // 当前搜索结果
let searchSelected = new Set();
let playlistEntries = [];
let playlistSelected = new Set();
let libFiles = [];
let libFilterType = '';
let currentVideo = null;     // 当前解析出的视频信息
let selectedFormatId = '';   // 「用此格式」精确选择的格式 ID（空 = 按清晰度档位）
let statusInfo = null;
let dockCollapsed = false;
let taskTimer = null;

// ---------- 通用工具 ----------
function toast(text, kind = '') {
  const wrap = $('toastWrap');
  const el = document.createElement('div');
  el.className = 'toast ' + kind;
  el.textContent = text;
  wrap.appendChild(el);
  setTimeout(() => {
    el.style.opacity = '0';
    el.style.transition = '.3s';
    setTimeout(() => el.remove(), 300);
  }, kind === 'err' ? 6000 : 3200);
}

function msg(elId, text, kind = 'info') {
  const el = $(elId);
  if (!el) return;
  el.className = 'msg ' + kind;
  el.innerHTML = text;
}

function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function humanSize(n) {
  if (!n || n <= 0) return '';
  const u = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0, v = Number(n);
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return (i === 0 ? v.toFixed(0) : v.toFixed(1)) + u[i];
}

function humanDur(sec) {
  if (!sec || sec <= 0) return '';
  sec = Math.round(sec);
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  return h ? `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
           : `${m}:${String(s).padStart(2, '0')}`;
}

/** 缩略图统一走后端代理：多数图床靠 Referer 防盗链，前端直连会显示空白 */
function proxyUrl(url, referer = '') {
  const p = new URLSearchParams({ url });
  if (referer) p.set('referer', referer);
  return `${API}/proxy?${p.toString()}`;
}

async function api(path, options = {}) {
  const opt = { headers: { 'Content-Type': 'application/json' }, ...options };
  if (opt.body && typeof opt.body !== 'string') opt.body = JSON.stringify(opt.body);
  let r;
  try {
    r = await fetch(API + path, opt);
    // 服务活着 → 撤掉「服务已停止」横幅（若在显示）
    const dead = document.getElementById('deadBanner');
    if (dead) dead.style.display = 'none';
  } catch (e) {
    // 连不上本地服务：服务进程已退出（控制台被关/崩溃）。必须让用户看见，
    // 否则页面停在旧状态里点什么都「没动静」（实测用户被坑过）。
    const dead = document.getElementById('deadBanner');
    if (dead) dead.style.display = 'block';
    throw new Error('无法连接本地服务：' + e.message);
  }
  const ct = r.headers.get('content-type') || '';
  const data = ct.includes('application/json') ? await r.json() : await r.text();
  if (!r.ok) {
    const detail = (data && (data.detail || data.error)) || `HTTP ${r.status}`;
    const err = new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
    err.status = r.status;
    throw err;
  }
  return data;
}

function setBusy(btn, busy, textBusy) {
  if (!btn) return;
  if (busy) {
    btn.dataset.oldText = btn.textContent;
    btn.disabled = true;
    btn.innerHTML = '<span class="spin"></span>' + (textBusy || '处理中…');
  } else {
    btn.disabled = false;
    if (btn.dataset.oldText) btn.textContent = btn.dataset.oldText;
  }
}

// ---------- 标签页 ----------
function switchTab(name) {
  document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.dataset.tab === name));
  document.querySelectorAll('.panel').forEach(p => p.classList.toggle('active', p.id === 'panel-' + name));
  if (name === 'library') loadLibrary();
}

/**
 * 一级页面内的「方式切换」：关键词搜索 / 粘贴链接抓取。
 *
 * 两块内容是各自独立的一整块（各自的输入框、结果区、轮询状态），
 * 切换只是整块显隐 —— 不重建 DOM，所以切回来时之前的结果还在。
 */
function bindModeSwitches() {
  const configs = [
    ['imageModeSwitch',
      { kw: 'imageKwMode', link: 'imgLinkMode' },
      { kw: '按关键词在搜索引擎里搜图',
        link: '粘贴网页链接，批量抓出图片（列表页 / 相册 / 整站）' }],
    ['videoModeSwitch',
      { kw: 'videoKwMode', link: 'vidLinkMode' },
      { kw: '按关键词搜视频，或粘贴单个视频链接解析清晰度',
        link: '粘贴列表页 / 博主主页，批量抓出视频（支持整站遍历）' }],
  ];

  configs.forEach(([swId, modes, hints]) => {
    const sw = $(swId);
    if (!sw) return;
    const hint = $(swId + 'Hint');
    sw.querySelectorAll('.mode-btn').forEach(btn => {
      btn.onclick = () => {
        const mode = btn.dataset.mode;
        sw.querySelectorAll('.mode-btn').forEach(b => b.classList.toggle('active', b === btn));
        Object.keys(modes).forEach(k => {
          const el = $(modes[k]);
          if (el) el.hidden = (k !== mode);
        });
        if (hint && hints[mode]) hint.textContent = hints[mode];
      };
    });
  });
}

// ---------- 环境状态 ----------
async function loadStatus() {
  try {
    statusInfo = await api('/api/status');
  } catch (e) {
    $('envFfmpeg').textContent = '服务未连接';
    $('envFfmpeg').className = 'env-item bad';
    return;
  }
  const ff = $('envFfmpeg');
  ff.textContent = statusInfo.ffmpeg_available ? 'ffmpeg 可用' : 'ffmpeg 缺失';
  ff.className = 'env-item ' + (statusInfo.ffmpeg_available ? 'ok' : 'bad');
  ff.title = statusInfo.ffmpeg_version || '未找到 ffmpeg';

  $('envYtdlp').textContent = 'yt-dlp ' + statusInfo.ytdlp_version;
  $('envYtdlp').className = 'env-item ok';

  const free = statusInfo.disk_free_bytes;
  $('envDisk').textContent = free ? '可用 ' + humanSize(free) : '磁盘未知';

  $('sFfmpeg').textContent = statusInfo.ffmpeg_available
    ? (statusInfo.ffmpeg_version || '可用') : '缺失（视频需合并音视频时会失败）';
  $('sYtdlp').textContent = statusInfo.ytdlp_version;
  $('sDownloads').textContent = statusInfo.downloads_dir;
  $('sDisk').textContent = free ? humanSize(free) : '未知';
}

// ---------- 关键词搜图 ----------
function collectFilters() {
  return {
    min_resolution: parseInt($('fResPreset').value, 10) || 0,
    max_resolution: parseInt($('fResMax').value, 10) || 0,
    min_size_kb: parseInt($('fMinSize').value, 10) || 0,
    formats: $('fFormats').value,
    engines: $('fEngines').value,
    size: $('fSize').value,
    aspect: $('fAspect').value,
    type: $('fType').value,
    time: $('fTime').value,
    color: $('fColor').value,
    face: $('fFace').value,
    license: $('fLicense').value,
    safesearch: $('fSafe').value,
  };
}

function activeFilterCount() {
  const f = collectFilters();
  return Object.values(f).filter(v => v !== '' && v !== 0 && v !== 'moderate' && v !== 'bing').length;
}

async function doSearch() {
  const kw = $('kwInput').value.trim();
  if (!kw) { toast('请先输入关键词', 'warn'); $('kwInput').focus(); return; }
  const btn = $('btnSearch');
  setBusy(btn, true, '搜索中…');
  msg('searchMsg', '<span class="spin"></span>正在搜索并探测原图真实分辨率…（清晰度筛选越严格越慢）');
  $('searchGrid').innerHTML = '';
  $('searchToolbar').hidden = true;

  try {
    const body = { keyword: kw, limit: parseInt($('fLimit').value, 10) || 40, ...collectFilters() };
    const res = await api('/api/search/images', { method: 'POST', body });
    searchItems = res.items || [];
    searchSelected = new Set();
    renderSearchResults(res);
  } catch (e) {
    msg('searchMsg', '搜索失败：' + esc(e.message), 'err');
    searchItems = [];
    renderEmpty('searchGrid', '未获取到结果');
  } finally {
    setBusy(btn, false);
  }
}

function renderSearchResults(res) {
  const meta = res.meta || {};
  const st = meta.engine_status || {};
  const parts = [];
  for (const [k, v] of Object.entries(st)) {
    parts.push(`${k}: ${esc(String(v))}`);
  }
  const rel = meta.relevance || {};
  let txt = `找到 <b>${searchItems.length}</b> 张`;
  if (meta.unique && meta.unique > searchItems.length) txt += `（候选 ${meta.unique} 张）`;
  if (meta.probed) txt += ` · 已探测 ${meta.probed} 张的真实分辨率`;
  if (rel.fallback_used) txt += ` · 已自动改用 Bing 引擎`;
  if (rel.blocked) txt += ` · <b>已拦截 ${rel.blocked} 条与关键词无关的结果</b>`;
  if (parts.length) txt += ` · ${parts.join(' / ')}`;
  msg('searchMsg', txt, searchItems.length ? 'ok' : 'warn');

  if (!searchItems.length) {
    // 引擎确实返回了结果，但一条都与关键词无关：这是上游返回了错内容，
    // 不是筛选条件太严，必须说清楚，避免用户去调筛选白费功夫。
    if (rel.all_blocked) {
      msg('searchMsg',
        `⚠ 搜索引擎返回的结果与关键词「${esc(meta.query || '')}」完全无关，已全部拦截（共 ${rel.blocked} 条）。`
        + ' 这通常是搜索引擎临时降级或网络链路异常导致的，与筛选条件无关。'
        + ' 请稍后重试，或把右上角「搜索引擎」换成 Bing 再试。',
        'err');
      renderEmpty('searchGrid', '结果与关键词无关，已拦截');
      $('searchToolbar').hidden = true;
      return;
    }
    const f = collectFilters();
    let tip = '没有符合条件的图片。';
    if (f.min_resolution >= 1080 || f.max_resolution) tip += ' 建议放宽清晰度条件——搜索接口的原图很多达不到高分辨率。';
    if (f.time) tip += ' 也可以放宽发布时间（时间越近结果越少）。';
    if (String(st.bing || '').startsWith('ok:0')) tip += ' Bing 对该筛选组合返回为空。';
    msg('searchMsg', esc(tip), 'warn');
    renderEmpty('searchGrid', '无结果');
    $('searchToolbar').hidden = true;
    return;
  }

  $('searchToolbar').hidden = false;
  updateSelCount();

  const grid = $('searchGrid');
  grid.innerHTML = '';
  const frag = document.createDocumentFragment();
  searchItems.forEach((it, idx) => frag.appendChild(buildImageCard(it, idx)));
  grid.appendChild(frag);
}

function buildImageCard(it, idx) {
  const card = document.createElement('div');
  card.className = 'card';
  card.dataset.idx = idx;

  const check = document.createElement('div');
  check.className = 'card-check';
  card.appendChild(check);

  const thumb = document.createElement('div');
  thumb.className = 'thumb';
  const img = document.createElement('img');
  img.loading = 'lazy';
  img.referrerPolicy = 'no-referrer';
  img.decoding = 'async';
  img.alt = it.title || '';
  // 优先用搜索接口的缩略图（快），失败时回退到后端代理原图
  img.src = it.thumbnail ? proxyUrl(it.thumbnail) : proxyUrl(it.url, it.referer);
  img.onerror = function () {
    if (!this.dataset.fallback) {
      this.dataset.fallback = '1';
      this.src = proxyUrl(it.url, it.referer);
    } else {
      const ph = document.createElement('div');
      ph.className = 'ph';
      ph.textContent = '预览不可用\n（站点防盗链或需登录）';
      ph.style.whiteSpace = 'pre-line';
      this.replaceWith(ph);
    }
  };
  thumb.appendChild(img);
  card.appendChild(thumb);

  const body = document.createElement('div');
  body.className = 'card-body';

  const title = document.createElement('div');
  title.className = 'card-title';
  title.textContent = it.title || it.source_site || '图片';
  title.title = it.title || '';
  body.appendChild(title);

  const meta = document.createElement('div');
  meta.className = 'card-meta';
  if (it.width && it.height) {
    const b = document.createElement('span');
    b.className = 'badge res';
    b.textContent = `${it.width}×${it.height}`;
    meta.appendChild(b);
  } else {
    const b = document.createElement('span');
    b.className = 'badge';
    b.textContent = '未探测';
    meta.appendChild(b);
  }
  if (it.size_bytes) {
    const b = document.createElement('span');
    b.className = 'badge';
    b.textContent = humanSize(it.size_bytes);
    meta.appendChild(b);
  }
  if (it.format) {
    const b = document.createElement('span');
    b.className = 'badge';
    b.textContent = it.format.toUpperCase();
    meta.appendChild(b);
  }
  if (it.source_site) {
    const b = document.createElement('span');
    b.className = 'badge';
    b.textContent = it.source_site.replace(/^www\./, '');
    b.title = it.source_site;
    meta.appendChild(b);
  }
  body.appendChild(meta);

  const links = document.createElement('div');
  links.className = 'card-links';
  const a1 = document.createElement('a');
  a1.href = it.url; a1.target = '_blank'; a1.rel = 'noopener';
  a1.textContent = '原图';
  a1.onclick = (e) => e.stopPropagation();
  links.appendChild(a1);
  if (it.source_page) {
    const a2 = document.createElement('a');
    a2.href = it.source_page; a2.target = '_blank'; a2.rel = 'noopener';
    a2.textContent = '来源页';
    a2.onclick = (e) => e.stopPropagation();
    links.appendChild(a2);
  }
  body.appendChild(links);
  card.appendChild(body);

  card.onclick = () => toggleSelect(idx, card);
  return card;
}

function toggleSelect(idx, cardEl) {
  if (searchSelected.has(idx)) {
    searchSelected.delete(idx);
    cardEl.classList.remove('selected');
    cardEl.querySelector('.card-check').textContent = '';
  } else {
    searchSelected.add(idx);
    cardEl.classList.add('selected');
    cardEl.querySelector('.card-check').textContent = '✓';
  }
  updateSelCount();
}

function updateSelCount() {
  $('selCount').textContent = searchSelected.size;
  const stat = $('searchStat');
  if (stat) {
    stat.textContent = searchItems.length
      ? `共 ${searchItems.length} 张 · 已选 ${searchSelected.size} 张`
      : '';
  }
}

function selectAllSearch() {
  searchSelected = new Set(searchItems.map((_, i) => i));
  applySelectionUI();
}
function invertSearch() {
  const next = new Set();
  searchItems.forEach((_, i) => { if (!searchSelected.has(i)) next.add(i); });
  searchSelected = next;
  applySelectionUI();
}
function clearSearch() { searchSelected = new Set(); applySelectionUI(); }

function applySelectionUI() {
  document.querySelectorAll('#searchGrid .card').forEach(c => {
    const i = parseInt(c.dataset.idx, 10);
    const on = searchSelected.has(i);
    c.classList.toggle('selected', on);
    c.querySelector('.card-check').textContent = on ? '✓' : '';
  });
  updateSelCount();
}

async function downloadSelectedImages() {
  if (!searchSelected.size) { toast('请先选择要下载的图片', 'warn'); return; }
  const items = [...searchSelected].sort((a, b) => a - b).map(i => {
    const it = searchItems[i];
    return {
      url: it.url,
      referer: it.referer || it.source_page || '',
      title: it.title || '',
    };
  }).filter(x => x.url);

  const btn = $('btnDownloadSel');
  setBusy(btn, true, '提交中…');
  try {
    const sub = (statusInfo && statusInfo.settings && statusInfo.settings.downloads_subdir_images) || 'images';
    const r = await api('/api/download/images', {
      method: 'POST',
      body: {
        items,
        subdir: sub + '/' + safeSub($('kwInput').value.trim()),
        concurrency: (statusInfo && statusInfo.settings && statusInfo.settings.image_concurrency) || 6,
        skip_existing: $('fSkipExisting').checked,
        title: `「${$('kwInput').value.trim()}」${items.length} 张`,
      },
    });
    toast(`已提交 ${items.length} 张图片下载`, 'ok');
    startTaskPolling();
  } catch (e) {
    toast('提交失败：' + e.message, 'err');
  } finally {
    setBusy(btn, false);
  }
}

function safeSub(name) {
  return String(name || '').replace(/[<>:"/\\|?*\x00-\x1f]/g, '_').trim().slice(0, 40) || 'search';
}

function renderEmpty(containerId, text) {
  const c = $(containerId);
  c.innerHTML = '';
  const d = document.createElement('div');
  d.className = 'empty';
  d.style.gridColumn = '1 / -1';
  d.textContent = text;
  c.appendChild(d);
}

// ---------- 抓取器（图片页 / 视频页各一个独立实例） ----------
/**
 * 创建一个抓取器实例。
 *   prefix  元素 ID 前缀（img / vid），HTML 里两套 UI 靠它区分
 *   kinds   本页固定抓取的媒体类型（图片页只要图片、视频页只要视频+流）
 *
 * 状态全部收在闭包里，两个页面互不干扰。
 */
function createCrawler(cfg) {
  const P = cfg.prefix;
  const KINDS = cfg.kinds;
  // 输入框示例（列表模式 / 整站模式各一条），按页面类型传入 ——
  // 图片页给图片站示例、视频页给视频站示例，别再给电商链接了。
  const PH = cfg.ph || {
    list: '粘贴列表页 / 搜索结果页链接',
    site: '独立站整站抓取：粘贴网站任意页面（通常首页），会走一遍全站',
  };
  /** 把逻辑名映射成本实例的真实元素 ID：crawlGrid -> imgCrawlGrid */
  const E = (name) => P + name.charAt(0).toUpperCase() + name.slice(1);
  const st = { items: [], sel: new Set(), jobId: null, timer: null };

// ---------- 批量抓取（列表页 → 详情页） ----------

function crawlMode() {
  return $(E('cMode')).value;
}

function crawlOptions() {
  const base = {
    url: $(E('crawlInput')).value.trim(),
    kinds: KINDS,          // 本页固定类型，不再读下拉（合并后由页面定位决定）
    originals_only: $(E('cOriginals')).value === '1',
    allow_render: $(E('cRender')).checked,
  };
  if (crawlMode() === 'site') {
    // 全站抓取：没有列表页/详情页的概念，靠站点地图 + 同域链接把站点走一遍
    return Object.assign(base, {
      mode: 'site',
      max_pages: parseInt($(E('cSitePages')).value, 10) || 30,
      max_depth: parseInt($(E('cDepth')).value, 10) || 2,
      same_host: true,
      use_sitemap: $(E('cUseSitemap')).checked,
    });
  }
  const drill = $(E('cDrill')).value;
  return Object.assign(base, {
    mode: 'list',
    max_pages: parseInt($(E('cPageCount')).value, 10) || 1,
    max_items: parseInt($(E('cMaxItems')).value, 10) || 30,
    drill_down: drill !== 'never',
    drill_mode: drill === 'always' ? 'always' : 'auto',
  });
}

/** 两种抓取模式的选项差异较大，切换时同步显隐与默认值，避免用户看到无效选项。 */
function updateCrawlModeUI() {
  const isSite = crawlMode() === 'site';
  $(E('cPagesLabelList')).hidden = isSite;
  $(E('cPagesLabelSite')).hidden = !isSite;
  $(E('cDepthLabel')).hidden = !isSite;
  $(E('cSitemapLabel')).hidden = !isSite;
  $(E('cDrillLabel')).hidden = isSite;
  $(E('cMaxItemsLabel')).hidden = isSite;

  // 整站模式**也开着渲染**：渲染层是逐页自适应的 —— 先走普通 HTTP（约 1 秒/页），
  // 只有判定为 JS 空壳的页面才上浏览器（约 10~35 秒），不会把每一页都渲染一遍。
  // 原实现一律给整站模式关掉渲染，结果 JS 建的独立站（现在的常态）整站抓下来是空的 ——
  // 用户反馈的「全站抓取根本没有全站抓取的能力」就是这个原因。
  $(E('cRender')).checked = true;

  $(E('crawlOptSummary')).textContent = isSite
    ? '（全站抓取：独立站整站走一遍，用站点地图 + 同域链接）'
    : '（列表页 / 博主页逐条下钻：商品列表 / 搜索结果页 / 博主主页用这个）';
  // 输入框示例必须按页面类型给（图片页用图片站、视频页用视频站）。
  // 原来是写死的电商链接，导致视频页显示「例：alibaba.com/trade/search…」——
  // 用户看到的示例跟当前页面完全对不上。
  $(E('crawlInput')).placeholder = isSite ? PH.site : PH.list;
}

function crawlSiteName() {
  try { return new URL(crawlOptions().url).hostname.replace(/^www\./, ''); }
  catch (e) { return 'site'; }
}

async function doCrawl() {
  const opt = crawlOptions();
  if (!opt.url) { toast('请先粘贴列表页链接', 'err'); return; }
  if (!/^https?:\/\//i.test(opt.url)) {
    toast('链接需要以 http:// 或 https:// 开头', 'err');
    return;
  }

  st.items = [];
  st.sel = new Set();
  $(E('crawlGrid')).innerHTML = '';
  $(E('crawlToolbar')).hidden = true;
  $(E('crawlWarn')).hidden = true;
  $(E('crawlDetailBox')).hidden = true;
  msg(E('crawlMsg'), '正在提交抓取任务…', 'info');

  setBusy($(E('btnCrawl')), true, '抓取中…');
  try {
    const r = await api('/api/crawl/start', { method: 'POST', body: opt });
    st.jobId = r.job_id;
    $(E('crawlProgress')).hidden = false;
    setCrawlProgressDone(false);          // 复位上一轮的收尾态
    $(E('crawlBar')).style.width = '5%';
    startCrawlPoll();
  } catch (e) {
    setBusy($(E('btnCrawl')), false);
    msg(E('crawlMsg'), '提交失败：' + esc(e.message), 'err');
  }
}

function startCrawlPoll() {
  stopCrawlPoll();
  st.timer = setInterval(pollCrawl, 900);
  pollCrawl();
}

function stopCrawlPoll() {
  if (st.timer) { clearInterval(st.timer); st.timer = null; }
}

/**
 * 切换抓取进度区的「收尾态」。
 *
 * 任务结束后必须把转圈动画停掉、把「取消」按钮藏掉，
 * 否则进度条会一直停在上一个阶段（原来会永远停在「汇总去重」）——
 * 用户看到的就是「去重完成后还显示去重」。
 */
function setCrawlProgressDone(done) {
  const box = $(E('crawlProgress'));
  if (!box) return;
  box.classList.toggle('done', !!done);
  const spin = box.querySelector('.spin');
  if (spin) spin.hidden = !!done;
  const cancel = $(E('btnCrawlCancel'));
  if (cancel) cancel.hidden = !!done;
}

async function pollCrawl() {
  if (!st.jobId) return;
  // 注意变量名：这里存的是「后端返回的状态快照」，必须叫 snap 之类的名字，
  // 不能叫 st —— st 是外面的实例状态，用同名局部变量会把它遮蔽掉，
  // 函数第一行的 st.jobId 就会因为暂时性死区直接抛
  // 「Cannot access 'st' before initialization」，轮询永远跑不起来。
  let snap;
  try {
    snap = await api('/api/crawl/status/' + st.jobId);
  } catch (e) {
    stopCrawlPoll();
    setBusy($(E('btnCrawl')), false);
    msg(E('crawlMsg'), '查询进度失败：' + esc(e.message), 'err');
    return;
  }

  const terminal = (snap.status === 'completed' || snap.status === 'failed'
    || snap.status === 'cancelled');

  $(E('crawlStage')).textContent = snap.stage_label || (terminal ? '已结束' : '抓取中…');
  const total = snap.stage_total || 0;
  const done = snap.stage_done || 0;
  let pct = total > 0 ? Math.round(done * 100 / total) : (snap.status === 'running' ? 6 : 100);
  if (snap.status === 'running') pct = Math.max(6, Math.min(pct, 96));
  $(E('crawlBar')).style.width = pct + '%';
  $(E('crawlCount')).textContent =
    `已发现 ${snap.media_found || 0} 个媒体 · ${snap.elapsed || 0}s`;
  $(E('crawlNote')).textContent = snap.note || '';

  if (terminal) {
    stopCrawlPoll();
    setBusy($(E('btnCrawl')), false);
    $(E('crawlProgress')).hidden = false;
    $(E('crawlBar')).style.width = '100%';
    setCrawlProgressDone(true);
    await showCrawlResult(snap);
  }
}

async function showCrawlResult(snap) {
  let full = snap;
  try {
    full = await api('/api/crawl/status/' + st.jobId + '?full=1');
  } catch (e) { /* 用轻量数据兜底 */ }

  st.items = full.items || [];
  st.sel = new Set();

  const warns = full.warnings || [];
  if (warns.length) {
    $(E('crawlWarn')).hidden = false;
    $(E('crawlWarn')).innerHTML = warns.map(w => '• ' + esc(w)).join('<br>');
  } else {
    $(E('crawlWarn')).hidden = true;
  }

  renderCrawlDetail(full);
  renderCrawlResults(full);

  if (snap.status === 'failed') {
    msg(E('crawlMsg'), '抓取失败：' + esc(snap.error || '未知错误'), 'err');
  } else if (snap.status === 'cancelled') {
    msg(E('crawlMsg'), `已取消，抓到 ${st.items.length} 个媒体（仍可下载）。`, 'warn');
  } else {
    const s = full.summary || {};
    const d = full.detail_pages || {};
    let txt = `抓取完成：共 <b>${st.items.length}</b> 个媒体`;
    if (s.image) txt += ` · 图片 ${s.image}`;
    if (s.video) txt += ` · 视频 ${s.video}`;
    if (s.stream) txt += ` · 流媒体 ${s.stream}`;
    if (s.thumbs) txt += ` · 其中缩略图 ${s.thumbs}`;
    if (d.total) txt += ` · 下钻详情页 ${d.ok}/${d.total} 成功`;
    txt += ` · 用时 ${(full.stats || {}).elapsed || snap.elapsed || 0}s`;
    msg(E('crawlMsg'), txt, st.items.length ? 'ok' : 'warn');
  }
}

function renderCrawlDetail(full) {
  const box = $(E('crawlDetail'));
  const pages = full.pages || [];
  const dp = full.detail_pages || {};
  if (!pages.length && !dp.total) { $(E('crawlDetailBox')).hidden = true; return; }

  const rows = [];
  rows.push('<table class="dtable"><thead><tr>'
    + '<th>页</th><th>方式</th><th>大小</th><th>媒体</th><th>详情链接</th><th>耗时</th>'
    + '</tr></thead><tbody>');
  pages.forEach(p => {
    rows.push(`<tr><td>${p.page}</td><td>${esc(p.method || '')}</td>`
      + `<td>${humanSize(p.html_bytes)}</td><td>${p.media}</td>`
      + `<td>${p.links}</td><td>${p.elapsed}s</td></tr>`);
  });
  rows.push('</tbody></table>');
  if (dp.total) {
    rows.push(`<p class="hint">详情页：成功 ${dp.ok} / 共 ${dp.total}`
      + (dp.failed ? `，失败 ${dp.failed}` : '') + '</p>');
  }
  box.innerHTML = rows.join('');
  $(E('crawlDetailBox')).hidden = false;
}

/** 从一条媒体（首选 + 候选档位 + 流条目探测档位）里读出可用的清晰度高度，降序返回。 */
function qualityHeightsOf(it) {
  const hs = new Set();
  if (it && Array.isArray(it.stream_heights)) {
    it.stream_heights.forEach(h => { h = parseInt(h, 10); if (h) hs.add(h); });
  }
  if (it && it.height) { const h = parseInt(it.height, 10); if (h) hs.add(h); }
  [it && it.url, ...((it && it.variants) || [])].forEach(u => {
    const m = /[_\-.](\d{3,4})[pP](?![a-z0-9])/.exec(u || '');
    if (m) hs.add(parseInt(m[1], 10));
  });
  return [...hs].sort((a, b) => b - a);
}

/** 清晰度下拉按本次抓到的真实档位重建。
 *  为什么：写死的 1080/720/480/240 跟站点实际给的对不上 —— xvideos 只有
 *  240/360，用户选了个 720 根本不知道会发生什么（静默就近匹配）。
 *  按结果动态生成后，下拉里全是这次真能下的档位。 */
function rebuildQualityOptions() {
  const sel = $(E('dlQuality'));
  if (!sel) return;
  const hs = new Set();
  st.items.forEach(it => qualityHeightsOf(it).forEach(h => hs.add(h)));
  const heights = [...hs].sort((a, b) => b - a);
  const cur = sel.value;
  let opts = '<option value="">原样（抓到的档位）</option>'
    + '<option value="highest">最高</option><option value="lowest">最低</option>';
  heights.forEach(h => { opts += `<option value="${h}">${h}p</option>`; });
  sel.innerHTML = opts;
  // 尽量保留用户已选的值；这批结果里没有该档位就回落到「原样」
  sel.value = [...sel.options].some(o => o.value === cur) ? cur : '';
}

function renderCrawlResults(full) {
  if (!st.items.length) {
    $(E('crawlToolbar')).hidden = true;
    renderEmpty(E('crawlGrid'), '没抓到媒体');
    return;
  }
  $(E('crawlToolbar')).hidden = false;
  rebuildQualityOptions();
  updateCrawlSelCount();

  // 结果可能上千条，一次性铺满 DOM 会让页面卡死。
  // 只渲染前 N 条（索引与 st.items 保持一致，勾选和下载不受影响）。
  const MAX_RENDER = 400;
  if (st.items.length > MAX_RENDER) {
    // 写到进度区，不覆盖上面的结果摘要
    $(E('crawlNote')).textContent =
      `结果共 ${st.items.length} 条，为保持流畅只显示前 ${MAX_RENDER} 条；「全选」仍会选中全部。`;
  }

  const grid = $(E('crawlGrid'));
  grid.innerHTML = '';
  const frag = document.createDocumentFragment();
  st.items.slice(0, MAX_RENDER).forEach((it, idx) => frag.appendChild(buildCrawlCard(it, idx)));
  grid.appendChild(frag);
}

/**
 * 卡片标题：优先用抓到的标题；标题为空或是垃圾（纯时长）时退回文件名。
 *
 * 为什么需要判「垃圾标题」：有些站点同一个详情页有两个 <a>，
 * 包缩略图的那个 inner text 只有覆盖层上的时长（如 "HD 10:00"），
 * 取到它当作标题，卡片上就是一片空白 —— 用户看到「视频没有名称」。
 */
function crawlCardTitle(it) {
  const raw = String(it.title || '').replace(/\s+/g, ' ').trim();
  const useless = /^(HD|SD|4K|FHD|UHD)?\s*[\d:：]{3,14}$/i.test(raw);
  if (raw && !useless) return raw;
  const tail = String(it.url || '').split('?')[0].split('/').pop() || '';
  let name = tail;
  try { name = decodeURIComponent(tail); } catch (e) { /* 保持原样 */ }
  if (!name) name = (it.type === 'stream') ? '流媒体' : '媒体文件';
  return name;
}

function buildCrawlCard(it, idx) {
  const kind = it.type || 'unknown';
  // 视频页条目（is_page）：url 是**视频页面地址**而不是媒体直链，
  // 由 yt-dlp 逐条解析下载。视觉上仍按视频处理（要封面、要 ▶ 角标）。
  const isPage = !!it.is_page;
  const isVideo = kind === 'video' || kind === 'stream' || isPage;

  const card = document.createElement('div');
  card.className = 'card';
  card.dataset.idx = idx;

  const check = document.createElement('div');
  check.className = 'card-check';
  card.appendChild(check);

  const thumb = document.createElement('div');
  thumb.className = 'thumb';
  const cover = it.thumbnail || '';
  if (isVideo && !cover) {
    // 只有在拿不到封面时才退回类型占位。
    // 以前无条件占位，导致「仅视频」模式下整屏都是空白框，看不出是哪个片子。
    const ph = document.createElement('div');
    ph.className = 'ph';
    ph.textContent = kind === 'stream' ? '流媒体（m3u8/mpd）' : '视频文件';
    thumb.appendChild(ph);
  } else {
    const img = document.createElement('img');
    img.loading = 'lazy';
    img.referrerPolicy = 'no-referrer';
    img.decoding = 'async';
    img.alt = it.title || '';
    img.src = proxyUrl(cover || it.url, it.referer);
    img.onerror = function () {
      const ph = document.createElement('div');
      ph.className = 'ph';
      ph.textContent = '预览不可用';
      this.replaceWith(ph);
    };
    thumb.appendChild(img);
    if (isVideo) {
      const play = document.createElement('span');
      play.className = 'play-badge';
      play.textContent = '▶';
      thumb.appendChild(play);
    }
  }
  card.appendChild(thumb);

  const body = document.createElement('div');
  body.className = 'card-body';

  const title = document.createElement('div');
  title.className = 'card-title';
  title.textContent = crawlCardTitle(it).slice(0, 100);
  title.title = it.url || '';
  body.appendChild(title);

  const meta = document.createElement('div');
  meta.className = 'card-meta';
  const kb = document.createElement('span');
  kb.className = 'badge' + (isVideo ? ' video' : '');
  kb.textContent = kind === 'stream' ? '流' : (isVideo ? '视频' : '图片');
  meta.appendChild(kb);

  if (isPage) {
    const b = document.createElement('span');
    b.className = 'badge';
    b.title = '这是视频页面地址，下载时由 yt-dlp 解析';
    b.textContent = '视频页';
    meta.appendChild(b);
  }
  if (it.duration_string) {
    const b = document.createElement('span');
    b.className = 'badge';
    b.textContent = it.duration_string;
    meta.appendChild(b);
  }
  // 卡片上直接标出可用档位：用户不点开下拉也能知道「这部有 1080p 可选」
  const heights = qualityHeightsOf(it);
  if (isVideo && heights.length > 1) {
    const b = document.createElement('span');
    b.className = 'badge';
    b.title = '可用清晰度；换档请在上方「清晰度」下拉里选好再点下载'
      + (kind === 'stream' ? '。流媒体会自动取最高档。' : '');
    b.textContent = heights.slice(0, 3).map(h => h + 'p').join('/')
      + (heights.length > 3 ? `+${heights.length - 3}` : '');
    meta.appendChild(b);
  }
  if (it.site) {
    const b = document.createElement('span');
    b.className = 'badge';
    b.textContent = String(it.site);
    meta.appendChild(b);
  }

  if (it.ext) {
    const b = document.createElement('span');
    b.className = 'badge';
    b.textContent = String(it.ext).toUpperCase();
    meta.appendChild(b);
  }
  if (it.width && it.height) {
    const b = document.createElement('span');
    b.className = 'badge res';
    b.textContent = `${it.width}×${it.height}`;
    meta.appendChild(b);
  }
  if (it.is_thumb) {
    const b = document.createElement('span');
    b.className = 'badge';
    b.textContent = '缩略图';
    meta.appendChild(b);
  }
  if (it.source) {
    const b = document.createElement('span');
    b.className = 'badge';
    b.textContent = String(it.source).split(',')[0];
    meta.appendChild(b);
  }
  body.appendChild(meta);

  const links = document.createElement('div');
  links.className = 'card-links';
  const a1 = document.createElement('a');
  a1.href = it.url; a1.target = '_blank'; a1.rel = 'noopener';
  a1.textContent = '打开';
  a1.onclick = (e) => e.stopPropagation();
  links.appendChild(a1);
  if (it.origin_page) {
    const a2 = document.createElement('a');
    a2.href = it.origin_page; a2.target = '_blank'; a2.rel = 'noopener';
    a2.textContent = '来源页';
    a2.onclick = (e) => e.stopPropagation();
    links.appendChild(a2);
  }
  // 视频页条目（is_page）：给「解析清晰度」入口 —— 跳到「单个视频链接」下载口。
  //
  // 为什么补它：用户反馈「以前能跳过去，现在点了没反应」。抓取结果里的视频页条目
  // 本身没有解析入口（直链条目不需要，它们用「下载选中」即可），而列表里的
  // 「解析」又把值写进了隐藏的输入框（见 gotoVideoProbe）。这里把入口补回卡片上。
  if (isPage) {
    const bp = document.createElement('button');
    bp.type = 'button';
    bp.className = 'btn tiny';
    bp.textContent = '解析清晰度';
    bp.title = '跳到「单个视频链接」下载口，查看可选清晰度，挑好再下载';
    bp.onclick = (e) => { e.stopPropagation(); gotoVideoProbe(it.url); doProbe(); };
    links.appendChild(bp);
  }
  body.appendChild(links);
  card.appendChild(body);

  card.onclick = () => {
    if (st.sel.has(idx)) st.sel.delete(idx);
    else st.sel.add(idx);
    card.classList.toggle('selected', st.sel.has(idx));
    check.textContent = st.sel.has(idx) ? '✓' : '';
    updateCrawlSelCount();
  };
  return card;
}

function updateCrawlSelCount() {
  $(E('crawlSelCount')).textContent = st.sel.size;
}

function applyCrawlSelectionUI() {
  document.querySelectorAll('#crawlGrid .card').forEach(c => {
    const i = parseInt(c.dataset.idx, 10);
    const on = st.sel.has(i);
    c.classList.toggle('selected', on);
    const ck = c.querySelector('.card-check');
    if (ck) ck.textContent = on ? '✓' : '';
  });
  updateCrawlSelCount();
}

async function downloadCrawlSelected() {
  const picked = st.items.filter((_, i) => st.sel.has(i));
  if (!picked.length) { toast('还没有勾选任何资源', 'err'); return; }

  const sub = 'crawl/' + safeSub(crawlSiteName());
  const streams = picked.filter(i => i.type === 'stream');
  // 视频页条目：url 是**视频页面地址**，必须交给 yt-dlp 解析，不能当直链下
  const pages = picked.filter(i => i.is_page && i.type !== 'stream');
  const plain = picked.filter(i => !i.is_page && i.type !== 'stream');
  let submitted = 0;

  // 下载设置：工具栏里可就地调整（原来固定吃全局默认值，批量下载前改不了）
  const concEl = $(E('dlConcurrency'));
  const skipEl = $(E('dlSkip'));
  const qualEl = $(E('dlQuality'));
  const conc = Math.max(1, Math.min(16, parseInt(concEl && concEl.value, 10) || 6));
  const skip = skipEl ? skipEl.checked : true;
  // 清晰度偏好：抓取结果里同一条媒体可能带多个档位地址（归并时被合并的那些），
  // 选了就在这里主动挑，而不是只能下抓到的第一条（用户反馈的「默认 240P」）。
  const preferQ = qualEl ? qualEl.value : '';

  try {
    if (pages.length) {
      // 视频页 → yt-dlp。清晰度取值与直链那套不同（yt-dlp 用 best/worst/高度），
      // 所以在这里映射一次，避免把 ""/"highest" 直接丢给 yt-dlp。
      const q = (preferQ === 'lowest') ? 'worst'
        : (preferQ === 'highest' || !preferQ) ? 'best' : preferQ;
      const r = await api('/api/download/videos', {
        method: 'POST',
        body: {
          items: pages.map(i => ({ url: i.url, title: (i.title || '').slice(0, 60) })),
          subdir: sub,
          quality: q,
          concurrency: Math.max(1, Math.min(8, conc)),
        },
      });
      submitted += (r && r.count) || pages.length;
    }
    if (plain.length) {
      await api('/api/download/direct', {
        method: 'POST',
        body: {
          items: plain.map(i => ({
            url: i.url,
            referer: i.referer || '',
            title: i.title || '',
            // 同一媒体的其它档位地址：首选地址下不了时后端会自动换用，
            // 避免「看起来最高清的那条其实拿不到」直接变成下载失败。
            alternates: i.variants || [],
          })),
          subdir: sub,
          concurrency: conc,
          skip_existing: skip,
          prefer_quality: preferQ,
          title: `批量抓取 ${plain.length} 个文件`,
        },
      });
      submitted += plain.length;
    }
    if (streams.length) {
      // 流媒体（m3u8/mpd）需要先拉分片再用 ffmpeg 合并，走后端专用接口
      await api('/api/download/stream', {
        method: 'POST',
        body: {
          items: streams.map(i => ({
            url: i.url, referer: i.referer || '', title: i.title || '',
          })),
          subdir: sub + '/streams',
          // 清晰度偏好对 master playlist 生效：后端从变体里挑对应档位
          prefer_quality: preferQ === 'highest' ? 'highest'
            : preferQ === 'lowest' ? 'lowest' : preferQ,
        },
      });
      submitted += streams.length;
    }
    toast(`已提交 ${submitted} 个资源到下载队列`, 'ok');
    // 必须立即启动任务轮询，否则底部「下载任务」栏要等下一次轮询周期
    // （或用户手动刷新页面）才会出现新任务 —— 用户反馈的
    // 「点下载后必须刷新才能看到任务」就是这个原因。
    startTaskPolling();
  } catch (e) {
    toast('下载提交失败：' + e.message, 'err');
  }
}

  return {
    prefix: P, state: st, E: E,
    start: doCrawl,
    options: crawlOptions,
    siteName: crawlSiteName,
    updateModeUI: updateCrawlModeUI,
    rebuildQualityOptions: rebuildQualityOptions,
    download: downloadCrawlSelected,
    selAll: () => { st.sel = new Set(st.items.map((_, i) => i)); applyCrawlSelectionUI(); },
    selInv: () => {
      const next = new Set();
      st.items.forEach((_, i) => { if (!st.sel.has(i)) next.add(i); });
      st.sel = next; applyCrawlSelectionUI();
    },
    selNone: () => { st.sel = new Set(); applyCrawlSelectionUI(); },
  };
}

const crawlerImg = createCrawler({
  prefix: 'img',
  kinds: 'image',
  ph: {
    list: '粘贴图片列表页 / 相册链接，例：https://www.pexels.com/search/led/',
    site: '独立站整站抓取：粘贴网站任意页面（通常首页），会用 sitemap + 同域链接走一遍',
  },
});
const crawlerVid = createCrawler({
  prefix: 'vid',
  kinds: 'video,stream',
  ph: {
    list: '粘贴视频列表页 / 博主主页链接，例：https://www.tiktok.com/@某人 或某站的视频搜索结果页',
    site: '独立站整站抓取：粘贴网站任意页面（通常首页），会用 sitemap + 同域链接走一遍',
  },
});
const crawlers = { img: crawlerImg, vid: crawlerVid };

// ---------- 视频 ----------
/**
 * 跳到「单个视频链接」下载口：切到「粘贴链接抓取」方式 + 填地址 + 滚动聚焦。
 *
 * 为什么必须切模式：`#videoInput` 属于「粘贴链接抓取」的一整块（#vidLinkMode）。
 * 在「关键词搜视频」方式下那块是 hidden 的 —— 只往里写值不切模式，用户看不到
 * 任何变化，感受就是「点解析没反应」（用户反馈的 bug）。
 * 本机实测：那时 videoInput 的 getBoundingClientRect 全是 0（隐藏元素特征），
 * 页面也完全没滚动。
 */
function gotoVideoProbe(url) {
  const linkBtn = document.querySelector('#videoModeSwitch .mode-btn[data-mode="link"]');
  // 已经是「粘贴链接抓取」时不重复点（避免多余重排）
  if (linkBtn && !linkBtn.classList.contains('active')) linkBtn.click();
  const inp = $('videoInput');
  if (!inp) return;
  inp.value = url;
  const anchor = document.querySelector('#vidLinkMode .subsec-title') || inp;
  try { anchor.scrollIntoView({ behavior: 'smooth', block: 'start' }); }
  catch (e) { try { inp.scrollIntoView(); } catch (e2) { /* 忽略 */ } }
  try { inp.focus({ preventScroll: true }); } catch (e) { /* 忽略 */ }
}

async function doProbe() {
  const url = $('videoInput').value.trim();
  if (!url) { toast('请先输入视频页面链接', 'warn'); $('videoInput').focus(); return; }
  const btn = $('btnProbe');
  setBusy(btn, true, '解析中…');
  msg('videoMsg', '<span class="spin"></span>正在解析可用清晰度…（首次解析某些站点较慢）');
  $('videoInfo').hidden = true;
  // 不要无条件隐藏批量列表：如果列表里还有内容，说明用户是从列表点「单独解析」
  // 进来的，保留它并在视频信息区给出「返回批量列表」入口。
  // （原实现无条件隐藏且无回退入口，用户只能把整个列表重新解析一遍。）
  if (!playlistEntries.length) $('playlistBox').hidden = true;
  currentVideo = null;
  selectedFormatId = '';

  try {
    const res = await api('/api/video/probe', {
      method: 'POST',
      body: { url, include_playlist: $('vPlaylist').checked },
    });
    if (!res.ok) {
      msg('videoMsg', '无法解析：' + esc(res.error || '未知错误'), 'err');
      if (String(res.error || '').includes('登录')) {
        msg('videoMsg', '无法解析：' + esc(res.error) +
          '<div class="warn" style="margin-top:8px">可在「设置」里配置 Cookies 文件以复用浏览器登录态后重试。</div>', 'err');
      }
      return;
    }
    if (res.is_playlist) { renderPlaylist(res); return; }
    currentVideo = res;
    renderVideoInfo(res);
    msg('videoMsg', '解析成功，共 ' + (res.formats || []).length + ' 种格式可选。', 'ok');
  } catch (e) {
    msg('videoMsg', '解析失败：' + esc(e.message), 'err');
  } finally {
    setBusy(btn, false);
  }
}

function renderVideoInfo(res) {
  $('videoInfo').hidden = false;
  selectedFormatId = '';
  $('vTitle').textContent = res.title || res.id || '未命名视频';
  const subParts = [];
  if (res.uploader) subParts.push(res.uploader);
  if (res.duration_string || res.duration) subParts.push(humanDur(res.duration));
  if (res.upload_date) subParts.push(res.upload_date);
  if (res.view_count != null) subParts.push(Number(res.view_count).toLocaleString() + ' 次观看');
  if (res.extractor) subParts.push('来源：' + res.extractor);
  $('vSub').textContent = subParts.filter(Boolean).join(' · ');

  $('vThumb').src = res.thumbnail ? proxyUrl(res.thumbnail, res.webpage_url) : '';
  $('vThumb').onerror = function () { this.style.visibility = 'hidden'; };
  $('vPageLink').href = res.webpage_url || '#';

  // 清晰度下拉。两处刻意调整：
  //  1) 用 optgroup 分组 —— 原生 select 展开时列表会覆盖在选择框上，
  //     若列表第一项与选择框当前显示的值同名（都是「最佳画质」），
  //     视觉上就像出现了两个重复项（用户截图反馈过）。分组标题占住第一行即可消除。
  //  2) 不再放「仅音频」—— 右侧已有同名复选框，两个入口属于功能重复。
  const q = $('vQuality');
  q.innerHTML = '';
  const def = (statusInfo && statusInfo.settings && statusInfo.settings.video_quality_default) || 'best';
  const groups = [
    ['预设', [{ value: 'best', label: '最佳画质' },
              { value: 'worst', label: '最小文件' }]],
    ['按分辨率', (res.quality_options || []).map(o => ({ value: o.value, label: o.label }))],
  ];
  groups.forEach(([label, arr]) => {
    if (!arr.length) return;
    const g = document.createElement('optgroup');
    g.label = label;
    arr.forEach(o => {
      const el = document.createElement('option');
      el.value = o.value; el.textContent = o.label;
      if (o.value === def) el.selected = true;
      g.appendChild(el);
    });
    q.appendChild(g);
  });
  // 兜底：默认值不在可选项里时（例如上次存的档位这次没有），退回第一项
  if (q.selectedIndex < 0 && q.options.length) q.options[0].selected = true;
  // 手动改清晰度 = 放弃「用此格式」的精确选择
  q.onchange = () => { selectedFormatId = ''; };

  // 格式表
  const tbody = $('vFmtTable').querySelector('tbody');
  tbody.innerHTML = '';
  (res.formats || []).forEach(f => {
    const tr = document.createElement('tr');
    const dim = f.width && f.height ? `${f.width}×${f.height}` : '-';
    tr.innerHTML = `<td>${esc(f.format_id)}</td><td>${esc(dim)}${f.fps ? ' ' + f.fps + 'fps' : ''}</td>
      <td>${esc(f.vcodec || '-')}</td><td>${esc(f.acodec || '-')}</td>
      <td>${esc(f.ext)}</td><td>${esc(humanSize(f.filesize))}</td>
      <td>${esc(f.quality_label)}</td><td></td>`;
    const td = tr.lastElementChild;
    const b = document.createElement('button');
    b.className = 'btn tiny';
    b.textContent = '用此格式';
    b.onclick = () => { q.value = f.height ? String(f.height) : 'best'; toast('已选择 ' + f.quality_label, 'ok'); };
    td.appendChild(b);
    tbody.appendChild(tr);
  });
  $('vFmtCount').textContent = (res.formats || []).length;

  // ffmpeg 提示
  const warn = $('vFfmpegWarn');
  if (!res.ffmpeg) {
    warn.hidden = false;
    warn.innerHTML = '⚠ 未检测到 ffmpeg：高清晰度通常是「视频流+音频流」分离的，需要 ffmpeg 合并。' +
      '缺失时会自动退回到自带音轨的完整格式，可能拿不到最高画质。' +
      '工具已内置 ffmpeg 到 bin 目录，若仍缺失请确认该文件未被杀毒软件删除。';
  } else {
    warn.hidden = true;
  }
  if (res.is_live) {
    warn.hidden = false;
    warn.innerHTML = '⚠ 这是直播流：下载会持续到直播结束或你手动取消，文件可能非常大。';
  }

  // 若此前解析出一个批量列表，说明用户是从列表点「单独解析」进来的，
  // 这里给出返回入口（列表本身仍保留在 DOM 里，只是被本区块遮住）。
  const backBar = $('backToPlaylistBar');
  if (backBar) {
    const hasList = playlistEntries.length > 0;
    backBar.hidden = !hasList;
    if (hasList) {
      const hint = $('backToPlaylistHint');
      if (hint) hint.textContent =
        `列表里还有 ${playlistEntries.length} 个视频，可返回继续挑选`;
    }
  }
}

function renderPlaylist(res) {
  $('playlistBox').hidden = false;
  playlistEntries = res.entries || [];
  playlistSelected = new Set();
  $('plStat').textContent = `「${res.playlist_title || '播放列表'}」共 ${res.playlist_count} 个视频，已列出前 ${playlistEntries.length} 个`;
  const box = $('playlistList');
  box.innerHTML = '';
  playlistEntries.forEach((e, i) => {
    const row = document.createElement('div');
    row.className = 'vitem';
    row.innerHTML = `
      <input type="checkbox" style="width:auto">
      <img loading="lazy" referrerpolicy="no-referrer" src="${e.thumbnail ? esc(proxyUrl(e.thumbnail)) : ''}" alt="">
      <div class="vbody">
        <div class="vtitle">${esc(e.title || '(无标题)')}</div>
        <div class="vsub2">${esc(e.uploader || '')} ${e.duration ? ' · ' + esc(humanDur(e.duration)) : ''}</div>
      </div>`;
    const cb = row.querySelector('input');
    cb.onchange = () => {
      if (cb.checked) playlistSelected.add(i); else playlistSelected.delete(i);
      $('plSelCount').textContent = playlistSelected.size;
    };
    row.onclick = (ev) => { if (ev.target !== cb) { cb.checked = !cb.checked; cb.onchange(); } };
    const btn = document.createElement('button');
    btn.className = 'btn tiny';
    btn.textContent = '单独解析';
    btn.onclick = (ev) => { ev.stopPropagation(); gotoVideoProbe(e.webpage_url); doProbe(); };
    row.appendChild(btn);
    box.appendChild(row);
  });
  msg('videoMsg', '这是一个播放列表，请勾选要下载的视频。', 'ok');
}

async function downloadPlaylistSelected() {
  if (!playlistSelected.size) { toast('请先勾选视频', 'warn'); return; }
  const urls = [...playlistSelected].sort((a, b) => a - b)
    .map(i => (playlistEntries[i] || {}).webpage_url).filter(Boolean);
  const btn = $('btnPlDownload');
  setBusy(btn, true, '提交中…');
  let ok = 0, fail = 0;
  for (const u of urls) {
    try {
      await api('/api/download/video', { method: 'POST', body: buildVideoBody(u) });
      ok++;
    } catch (e) { fail++; }
  }
  setBusy(btn, false);
  toast(`已提交 ${ok} 个视频` + (fail ? `，${fail} 个失败` : ''), fail ? 'warn' : 'ok');
  startTaskPolling();
}

function buildVideoBody(url) {
  return {
    url,
    subdir: ((statusInfo && statusInfo.settings && statusInfo.settings.downloads_subdir_videos) || 'videos'),
    quality: $('vQuality').value || 'best',
    container: $('vContainer').value,
    prefer_audio_only: $('vAudioOnly').checked,
    write_subtitles: $('vSubs').checked,
    subtitle_langs: 'zh.*,en',
    include_playlist: $('vPlaylist').checked,
    rate_limit: $('vRate').value,
    format_id: selectedFormatId || '',
    title: (currentVideo && currentVideo.title) ? currentVideo.title.slice(0, 60) : '',
  };
}

async function doVideoDownload() {
  const url = $('videoInput').value.trim();
  if (!url) { toast('请先输入视频链接', 'warn'); return; }
  const btn = $('btnVideoDownload');
  setBusy(btn, true, '提交中…');
  try {
    const r = await api('/api/download/video', { method: 'POST', body: buildVideoBody(url) });
    toast('已提交视频下载任务', 'ok');
    if (r && r.ffmpeg_available === false) {
      toast('未检测到 ffmpeg，将下载自带音轨的完整格式（可能非最高画质）', 'warn');
    }
    startTaskPolling();
  } catch (e) {
    toast('提交失败：' + e.message, 'err');
  } finally {
    setBusy(btn, false);
  }
}

// ---------- 下载台账（去重记录）----------
// 台账按**视频身份**记账，与文件放在哪儿无关。这里只负责
// 「看有多少条 / 清空 / 查看列表」，判定逻辑全在后端 app/history.py。
async function loadHistory(showList) {
  const stat = $('historyStat');
  if (stat) stat.textContent = '读取中…';
  try {
    const res = await api('/api/history?limit=100');
    const s = res.stats || {};
    const byKind = s.by_kind || {};
    const parts = Object.keys(byKind).map(k =>
      `${k === 'video' ? '视频' : k === 'image' ? '图片' : k} ${byKind[k]}`);
    if (stat) {
      stat.textContent = `已记录 ${s.items || 0} 条下载`
        + (parts.length ? `（${parts.join('，')}）` : '');
    }
    const box = $('historyBox');
    if (box && (showList || !box.hidden)) {
      const rows = res.recent || [];
      box.innerHTML = rows.length
        ? '<table><tr><th>时间</th><th>类型</th><th>标题</th><th>保存位置</th></tr>'
          + rows.map(r => `<tr><td>${esc(new Date((r.ts || 0) * 1000).toLocaleString())}</td>`
            + `<td>${esc(r.kind || '')}</td>`
            + `<td title="${esc(r.url || '')}">${esc((r.title || '').slice(0, 60) || '(无标题)')}</td>`
            + `<td title="${esc(r.path || '')}">${esc((r.path || '').replace(/^.*[\\/]/, '') || '—')}</td></tr>`).join('')
          + '</table>'
        : '<p class="hint">还没有任何下载记录。</p>';
      box.hidden = false;
    }
  } catch (e) {
    if (stat) stat.textContent = '读取失败：' + e.message;
  }
}

// ---------- 关键词搜视频：结果列表 + 勾选批量下载 ----------
// 搜索结果每条都是**视频页面地址**（YouTube / B 站 / 各站播放页），下载要走 yt-dlp
// 逐条解析，所以「批量」= 提交多条独立任务、由后端限制同时开跑的个数，
// 而不是像直链那样一次性并发拉一堆文件。
let videoSearchItems = [];
let videoSearchSel = new Set();

function updateVideoSearchSelCount() {
  const el = $('videoKwSelCount');
  if (el) el.textContent = videoSearchSel.size;
}

// 当前筛选条件下的条目（带原始下标，勾选状态用原始下标记录，
// 这样筛选/排序变化不会把「已勾选」对应到别的视频上）。
function videoFilteredEntries(forceKeepUnknown) {
  const durVal = $('videoFilterDur') ? $('videoFilterDur').value : '';
  const site = $('videoFilterSite') ? $('videoFilterSite').value : '';
  const text = $('videoFilterText') ? $('videoFilterText').value.trim().toLowerCase() : '';
  const sort = $('videoFilterSort') ? $('videoFilterSort').value : 'default';
  const keepUnknown = forceKeepUnknown === true
    || ($('videoFilterKeepUnknown') ? $('videoFilterKeepUnknown').checked : true);

  let lo = null, hi = null, durActive = false;
  if (durVal === 'custom') {
    const a = parseInt($('videoFilterMin').value, 10);
    const b = parseInt($('videoFilterMax').value, 10);
    lo = Number.isFinite(a) ? a : null;
    hi = Number.isFinite(b) ? b : null;
    durActive = lo !== null || hi !== null;
  } else if (durVal) {
    const parts = durVal.split('-');
    lo = parts[0] === '' ? null : Number(parts[0]);
    hi = parts[1] === '' ? null : Number(parts[1]);
    durActive = true;
  }

  const entries = videoSearchItems.map((it, i) => ({ it, i })).filter(({ it }) => {
    if (site && String(it.source_site || '') !== site) return false;
    if (text && !String(it.title || '').toLowerCase().includes(text)) return false;
    if (durActive) {
      const d = typeof it.duration === 'number' ? it.duration : null;
      // 没有时长信息的条目无法判断是否落在区间里：默认**保留**（宁可多给不可误删），
      // 用户可按需关掉，关掉后这类条目会被排除并如实告知排除了几条。
      if (d === null || d <= 0) return keepUnknown;
      if (lo !== null && d < lo) return false;
      if (hi !== null && d > hi) return false;
    }
    return true;
  });

  if (sort === 'dur_asc') {
    entries.sort((a, b) => (a.it.duration || Number.MAX_SAFE_INTEGER)
      - (b.it.duration || Number.MAX_SAFE_INTEGER));
  } else if (sort === 'dur_desc') {
    entries.sort((a, b) => (b.it.duration || -1) - (a.it.duration || -1));
  }
  return entries;
}

function applyVideoSearchSelectionUI() {
  document.querySelectorAll('#videoSearchResult .vitem').forEach(row => {
    const i = parseInt(row.dataset.idx, 10);
    const on = videoSearchSel.has(i);
    row.classList.toggle('selected', on);
    const ck = row.querySelector('.vitem-check');
    if (ck) ck.checked = on;
  });
  updateVideoSearchSelCount();
}

function renderVideoSearchItems(entries) {
  const box = $('videoSearchResult');
  box.innerHTML = '';
  entries.forEach(({ it, i: idx }) => {
    const row = document.createElement('div');
    row.className = 'vitem';
    row.dataset.idx = String(idx);
    // 把时长/来源挂到 dataset 上：筛选逻辑与验收脚本都能直接读，
    // 不必去解析展示文本（文本里几项是拼起来的，解析容易出错）
    row.dataset.dur = (typeof it.duration === 'number' && it.duration > 0) ? String(it.duration) : '';
    row.dataset.site = it.source_site || '';
    // 全网搜索的结果来自不同站点，把来源站点显式标出来（YouTube / Vimeo / 某站 …），
    // 用户才知道这条是哪儿来的、值不值得点。
    const sub = [it.source_site, it.uploader, it.duration_string]
      .filter(Boolean).join(' · ');

    const ck = document.createElement('input');
    ck.type = 'checkbox';
    ck.className = 'vitem-check';
    ck.title = '勾选后可批量下载';
    ck.checked = videoSearchSel.has(idx);
    if (ck.checked) row.classList.add('selected');
    ck.onclick = (e) => e.stopPropagation();
    ck.onchange = () => {
      if (ck.checked) videoSearchSel.add(idx); else videoSearchSel.delete(idx);
      row.classList.toggle('selected', ck.checked);
      updateVideoSearchSelCount();
    };
    row.appendChild(ck);

    const img = document.createElement('img');
    img.loading = 'lazy';
    img.referrerPolicy = 'no-referrer';
    if (it.thumbnail) img.src = proxyUrl(it.thumbnail);
    row.appendChild(img);

    const body = document.createElement('div');
    body.className = 'vbody';
    const t = document.createElement('div');
    t.className = 'vtitle';
    t.textContent = it.title || '(无标题)';
    const s = document.createElement('div');
    s.className = 'vsub2';
    s.textContent = sub;
    body.appendChild(t); body.appendChild(s);
    row.appendChild(body);

    const b1 = document.createElement('button');
    b1.className = 'btn tiny primary'; b1.textContent = '解析';
    b1.onclick = (e) => { e.stopPropagation(); gotoVideoProbe(it.webpage_url); doProbe(); };
    const b2 = document.createElement('button');
    b2.className = 'btn tiny'; b2.textContent = '直接下载';
    b2.onclick = async (e) => {
      e.stopPropagation();
      $('videoInput').value = it.webpage_url;
      setBusy(b2, true, '提交…');
      try {
        await api('/api/download/video', { method: 'POST', body: buildVideoBody(it.webpage_url) });
        toast('已提交下载：' + (it.title || '').slice(0, 30), 'ok');
        startTaskPolling();
      } catch (err) { toast('提交失败：' + err.message, 'err'); }
      finally { setBusy(b2, false); }
    };
    row.appendChild(b1); row.appendChild(b2);
    // 点整行也能勾选（复选框以外的区域），与批量抓取那边的手感保持一致
    row.onclick = () => { ck.checked = !ck.checked; ck.onchange(); };
    box.appendChild(row);
  });
  box.hidden = false;
  const bar = $('videoSearchToolbar');
  if (bar) bar.hidden = false;
  const fbox = $('videoSearchFilterBox');
  if (fbox) fbox.hidden = false;
  updateVideoSearchSelCount();
}

// 文本输入用防抖，避免每敲一个字就整列重绘
let videoFilterTimer = null;
function debounceVideoFilter() {
  if (videoFilterTimer) clearTimeout(videoFilterTimer);
  videoFilterTimer = setTimeout(() => { videoFilterTimer = null; applyVideoSearchFilter(); }, 250);
}

// 按当前筛选条件重绘列表，并同步统计/说明。
// 被筛掉的条目会**从勾选里移除** —— 否则「筛出 5 分钟以内的 → 点下载」
// 会连带把刚被筛掉的其它条目一起下走，这种「看不见却被提交」的行为必须避免。
function applyVideoSearchFilter() {
  if (!videoSearchItems.length) return;
  // 「自定义时长」的两个输入框只在选中该项时启用，避免用户以为填了就算数
  const custom = $('videoFilterDur').value === 'custom';
  ['videoFilterMin', 'videoFilterMax'].forEach(id => {
    const el = $(id);
    if (el) el.disabled = !custom;
  });
  const entries = videoFilteredEntries();
  const keep = new Set(entries.map(e => e.i));
  videoSearchSel.forEach(i => { if (!keep.has(i)) videoSearchSel.delete(i); });
  renderVideoSearchItems(entries);

  const stat = $('videoSearchStat');
  if (stat) {
    stat.textContent = entries.length === videoSearchItems.length
      ? `共 ${videoSearchItems.length} 条`
      : `筛出 ${entries.length} / 共 ${videoSearchItems.length} 条`;
  }
  // 如实说明「有多少条因为缺时长信息被排除」，不让静默丢结果
  const note = $('videoFilterNote');
  if (note) {
    const durActive = $('videoFilterDur').value !== '';
    const keepUnknown = $('videoFilterKeepUnknown').checked;
    if (durActive && !keepUnknown) {
      const excluded = videoFilteredEntries(true).length - entries.length;
      note.textContent = excluded
        ? `已按「不保留无时长信息」排除 ${excluded} 条（搜索引擎里有些结果没有时长）。`
        : '';
    } else {
      note.textContent = '';
    }
  }
  const sel = $('videoSearchFilterSummary');
  if (sel) {
    const dur = $('videoFilterDur');
    const durTxt = dur && dur.value ? dur.options[dur.selectedIndex].textContent.trim() : '';
    const site = $('videoFilterSite').value;
    const parts = [];
    if (durTxt && durTxt !== '不限') parts.push('时长：' + durTxt);
    if (site) parts.push('来源：' + site);
    if ($('videoFilterText').value.trim()) parts.push('标题含「' + $('videoFilterText').value.trim() + '」');
    sel.textContent = parts.length ? `（${parts.join(' · ')}）` : '（时长 / 来源 / 标题 / 排序）';
  }
}

function resetVideoSearchFilter() {
  $('videoFilterDur').value = '';
  $('videoFilterMin').value = '';
  $('videoFilterMax').value = '';
  $('videoFilterSite').value = '';
  $('videoFilterText').value = '';
  $('videoFilterSort').value = 'default';
  $('videoFilterKeepUnknown').checked = true;
  applyVideoSearchFilter();
}

// 每次搜索结果变了就重建「来源站点」下拉，避免留着上一轮的站点
function fillVideoSiteOptions() {
  const sel = $('videoFilterSite');
  if (!sel) return;
  const sites = [...new Set(videoSearchItems.map(i => i.source_site).filter(Boolean))].sort();
  sel.innerHTML = '<option value="">全部</option>'
    + sites.map(s => `<option value="${esc(s)}">${esc(s)}</option>`).join('');
}

async function downloadVideoSearchSelected() {
  const picked = videoSearchItems.filter((_, i) => videoSearchSel.has(i));
  if (!picked.length) { toast('还没有勾选任何视频', 'err'); return; }
  const btn = $('videoKwDownload');
  setBusy(btn, true, '提交中…');
  try {
    const res = await api('/api/download/videos', {
      method: 'POST',
      body: {
        items: picked.map(i => ({ url: i.webpage_url, title: (i.title || '').slice(0, 60) })),
        subdir: ((statusInfo && statusInfo.settings && statusInfo.settings.downloads_subdir_videos) || 'videos'),
        quality: $('videoKwQuality').value || 'best',
        concurrency: Math.max(1, Math.min(8, parseInt($('videoKwConcurrency').value, 10) || 3)),
      },
    });
    toast(`已提交 ${res.count} 个视频到下载队列（同时最多 ${res.concurrency} 个）`, 'ok');
    if (res.ffmpeg_available === false) {
      toast('未检测到 ffmpeg，将下载自带音轨的完整格式（可能非最高画质）', 'warn');
    }
    // 必须立即启动任务轮询，否则底部「下载任务」栏要等下一个轮询周期才出现
    startTaskPolling();
  } catch (e) {
    toast('批量下载失败：' + e.message, 'err');
  } finally {
    setBusy(btn, false);
  }
}

async function doVideoSearch() {
  const kw = $('videoKw').value.trim();
  if (!kw) { toast('请输入视频关键词', 'warn'); return; }
  const btn = $('btnVideoSearch');
  setBusy(btn, true, '搜索中…');
  msg('videoMsg', '<span class="spin"></span>正在搜索视频…');
  const box = $('videoSearchResult');
  box.hidden = true; box.innerHTML = '';
  const bar = $('videoSearchToolbar');
  if (bar) bar.hidden = true;
  const fbox = $('videoSearchFilterBox');
  if (fbox) fbox.hidden = true;
  videoSearchItems = [];
  videoSearchSel = new Set();
  updateVideoSearchSelCount();

  try {
    const res = await api('/api/search/videos', {
      method: 'POST',
      body: {
        keyword: kw,
        platform: $('videoPlatform').value,
        count: parseInt($('videoKwCount').value, 10) || 20,
        // 去重：让后端按下载台账把已下过的过滤掉（默认开）。
        // 放在后端做而不是前端过滤，是因为判定要用「身份键」，
        // 而且过滤后要如实告诉用户隐藏了几条。
        hide_downloaded: $('videoFilterHideDownloaded')
          ? $('videoFilterHideDownloaded').checked : true,
      },
    });
    if (!res.ok) {
      msg('videoMsg', '视频搜索失败：' + esc(res.error || '未知错误'), 'err');
      return;
    }
    if (!res.count) {
      // 全部被「隐藏已下载」挡掉时要区分开 —— 说「没搜到」会误导用户
      if (res.hidden_downloaded) {
        msg('videoMsg', esc(res.hint || `搜到的结果都是你已下载过的（${res.hidden_downloaded} 条），已全部隐藏。`),
          'warn');
      } else {
        msg('videoMsg', '没有搜到视频。', 'warn');
      }
      return;
    }
    const base = `搜到 ${res.count} 个视频，可点「解析」逐个确认，也可勾选后批量下载。`;
    // hint 有三种含义，一律如实展示，不静默：
    //   ① low_relevance —— 上游返回了一批与关键词无关的内容（按 warn 提示）
    //   ② rewritten_query —— 原词取不到结果，已自动改写查询重搜
    //   ③ fallback / 某来源被丢弃 —— 平台搜索不可用时换了来源，或某来源被判无关
    let html = esc(base);
    if (res.hint) {
      const cls = res.low_relevance ? 'warn' : 'hint';
      html += `<div class="${cls}" style="margin-top:8px">${esc(res.hint)}</div>`;
    }
    msg('videoMsg', html, res.low_relevance ? 'warn' : 'ok');
    videoSearchItems = res.items || [];
    fillVideoSiteOptions();      // 重建来源下拉，别留着上一轮的站点
    resetVideoSearchFilter();    // 新的一批结果默认不筛
  } catch (e) {
    msg('videoMsg', '视频搜索失败：' + esc(e.message), 'err');
  } finally {
    setBusy(btn, false);
  }
}

// ---------- 已下载 ----------
async function loadLibrary() {
  msg('libMsg', '<span class="spin"></span>正在读取下载目录…');
  $('libGrid').innerHTML = '';
  try {
    const res = await api('/api/downloads/tree');
    libFiles = res.files || [];
    $('libStat').textContent = `${res.count} 个媒体文件 · 目录：${res.root}`;
    msg('libMsg', '', 'info');
    renderLibrary();
  } catch (e) {
    msg('libMsg', '读取失败：' + esc(e.message), 'err');
  }
}

function renderLibrary() {
  const grid = $('libGrid');
  grid.innerHTML = '';
  const items = libFiles.filter(f => {
    if (libFilterType === 'video') return f.is_video;
    if (libFilterType === 'image') return !f.is_video;
    return true;
  });
  if (!items.length) { renderEmpty('libGrid', '还没有下载任何文件'); return; }
  items.forEach(f => {
    const card = document.createElement('div');
    card.className = 'card';
    const thumb = document.createElement('div');
    thumb.className = 'thumb';
    if (!f.is_video) {
      const img = document.createElement('img');
      img.loading = 'lazy';
      img.src = API + f.href;
      img.onerror = function () {
        const ph = document.createElement('div'); ph.className = 'ph';
        ph.textContent = '预览不可用'; this.replaceWith(ph);
      };
      thumb.appendChild(img);
    } else {
      const v = document.createElement('video');
      v.src = API + f.href; v.preload = 'metadata'; v.muted = true;
      v.style.width = '100%'; v.style.height = '100%'; v.style.objectFit = 'cover';
      v.onerror = function () {
        const ph = document.createElement('div'); ph.className = 'ph';
        ph.textContent = '视频'; this.replaceWith(ph);
      };
      thumb.appendChild(v);
    }
    card.appendChild(thumb);

    const body = document.createElement('div');
    body.className = 'card-body';
    const t = document.createElement('div');
    t.className = 'card-title'; t.textContent = f.name; t.title = f.rel;
    body.appendChild(t);
    const m = document.createElement('div');
    m.className = 'card-meta';
    m.innerHTML = `<span class="badge ${f.is_video ? 'video' : ''}">${f.is_video ? '视频' : '图片'}</span>
      <span class="badge">${esc(humanSize(f.size))}</span>
      ${f.dir ? `<span class="badge" title="${esc(f.dir)}">${esc(f.dir)}</span>` : ''}`;
    body.appendChild(m);
    const links = document.createElement('div');
    links.className = 'card-links';
    links.innerHTML = `<a href="${esc(API + f.href)}" target="_blank" rel="noopener">打开</a>
      <a href="${esc(API + f.href)}" download>保存</a>`;
    body.appendChild(links);
    card.appendChild(body);
    grid.appendChild(card);
  });
}

async function openDownloads() {
  try {
    await api('/api/downloads/open', { method: 'POST', body: { sub: '' } });
    toast('已在文件管理器中打开下载目录', 'ok');
  } catch (e) { toast('打开失败：' + e.message, 'err'); }
}

// ---------- 任务面板 ----------
function startTaskPolling() {
  if (taskTimer) return;
  pollTasks();
  taskTimer = setInterval(pollTasks, 1200);
}

async function pollTasks() {
  let res;
  try { res = await api('/api/tasks?limit=30'); }
  catch (e) { return; }
  const tasks = res.tasks || [];
  $('taskCount').textContent = tasks.length;

  const running = tasks.some(t => t.status === 'running' || t.status === 'queued');
  if (!running && taskTimer) {
    clearInterval(taskTimer);
    taskTimer = null;
    // 任务全部结束后刷新已下载列表（若正在该页）
    if ($('panel-library').classList.contains('active')) loadLibrary();
  }

  const box = $('taskList');
  if (!tasks.length) {
    box.innerHTML = '<div class="empty" style="padding:12px">暂无下载任务</div>';
    return;
  }
  box.innerHTML = '';
  tasks.forEach(t => {
    const el = document.createElement('div');
    el.className = 'task';
    const stLabel = { queued: '排队中', running: '下载中', completed: '已完成', failed: '失败', cancelled: '已取消' }[t.status] || t.status;
    const barCls = t.status === 'completed' ? 'done' : (t.status === 'failed' || t.status === 'cancelled' ? 'fail' : '');
    const pct = Math.round(t.percent || 0);

    let sub = '';
    if (t.kind === 'image') {
      sub = `成功 ${t.done} · 跳过 ${t.skipped} · 失败 ${t.failed} · 共 ${t.total}`;
      // 「首选档位下不了、自动换了同媒体其它档位」要说清楚，
      // 否则用户会以为拿到的就是首选那条（清晰度可能不同）。
      if (t.alt_used) sub += ` · ${t.alt_used} 个已自动改用其它档位`;
    } else {
      sub = t.current ? esc(t.current) : '准备中…';
      if (t.speed) sub += ` · ${humanSize(t.speed)}/s`;
      // 兜底说明（如「已自动改用 mweb 客户端，清晰度受限」）必须显示出来，
      // 与「自动换档位」同一个原则：清晰度可能变了，不许静默。
      if (t.note) sub += ` · ${esc(t.note)}`;
    }

    el.innerHTML = `
      <div class="task-head">
        <span class="task-status ${esc(t.status)}">${esc(stLabel)}</span>
        <span class="task-title" title="${esc(t.title)}">${esc(t.title || (t.kind === 'video' ? '视频下载' : '图片下载'))}</span>
        <span class="stat">${pct}%</span>
        ${t.status === 'running' || t.status === 'queued'
          ? '<button class="btn tiny danger" data-act="cancel">取消</button>' : ''}
        <button class="btn tiny ghost" data-act="open">打开目录</button>
        <button class="btn tiny ghost" data-act="remove">移除</button>
      </div>
      <div class="task-bar"><i class="${barCls}" style="width:${pct}%"></i></div>
      <div class="task-sub">${sub}</div>
      ${t.error ? `<div class="task-err">${esc(t.error)}</div>` : ''}
      ${t.files && t.files.length ? `<div class="task-files">已保存 ${t.files.length} 个文件到 ${esc(t.output_dir || '')}</div>` : ''}`;

    el.querySelectorAll('button').forEach(b => {
      b.onclick = async () => {
        const act = b.dataset.act;
        try {
          if (act === 'cancel') { await api(`/api/tasks/${t.id}/cancel`, { method: 'POST' }); toast('已请求取消', 'info'); }
          else if (act === 'remove') { await api(`/api/tasks/${t.id}`, { method: 'DELETE' }); pollTasks(); }
          else if (act === 'open') {
            await api('/api/downloads/open', { method: 'POST', body: { sub: '' } });
            toast('已打开下载目录', 'ok');
          }
        } catch (e) { toast('操作失败：' + e.message, 'err'); }
      };
    });
    box.appendChild(el);
  });
}

// ---------- 设置 ----------
function renderProfileStat(bp) {
  const el = $('sProfileStat');
  if (!el) return;
  if (!bp || !bp.enabled) {
    el.textContent = '未启用（每次用干净浏览器；抖音/小红书主页会拿不到内容）';
    return;
  }
  if (!bp.exists) { el.textContent = '目录不存在：' + bp.dir; return; }
  el.textContent = `已启用 ${bp.dir}` + (bp.headed ? '（有头窗口）' : '（无头）')
    + (bp.has_default_profile ? '，检测到已有登录数据' : '，该目录还没有登录数据');
}

async function openLoginBrowser() {
  const url = ($('sLoginSite') && $('sLoginSite').value) || 'https://www.instagram.com/';
  const dir = ($('sProfileDir') && $('sProfileDir').value.trim()) || '';
  const msg = $('sLoginMsg');
  if (msg) msg.textContent = '正在打开浏览器窗口…';
  try {
    const r = await api('/api/browser/login', { method: 'POST', body: { url, dir } });
    if (msg) {
      msg.textContent = `已打开 ${url} 的登录窗口（pid ${r.pid}）：登录完成后**关闭该窗口**即可，`
        + `登录态会保存到 ${r.dir}`;
    }
    toast('浏览器窗口已打开：登录完成后关闭窗口即可', 'ok');
  } catch (e) {
    if (msg) msg.textContent = '打开失败：' + e.message;
    toast('打开登录浏览器失败：' + e.message, 'err');
  }
}

async function refreshLoginState() {
  try {
    const r = await api('/api/browser/login/state');
    const msg = $('sLoginMsg');
    const st = r.last_login || {};
    const doms = Object.keys(st.domains || {});
    if (msg && doms.length) {
      const when = st.ts ? new Date(st.ts * 1000).toLocaleString() : '';
      msg.textContent = `上次登录：${when}，覆盖 ${doms.slice(0, 4).join('、')}`
        + `（共 ${doms.length} 个域）`;
    }
  } catch (e) { /* 状态拿不到不影响使用 */ }
}

function renderCookiesStat(ck) {
  const el = $('sCookiesStat');
  if (!el) return;
  if (!ck || !ck.count) {
    el.textContent = (ck && ck.file) ? '文件读不到或没有条目（检查路径与格式）' : '未配置（匿名访问）';
    return;
  }
  const doms = (ck.domains || []).slice(0, 4).join('、');
  el.textContent = `${ck.count} 条，来自 ${ck.file.split(/[\\/]/).pop()}` + (doms ? `（含 ${doms}）` : '');
}

function fillSettings(s) {
  $('sSubImages').value = s.downloads_subdir_images || 'images';
  $('sSubVideos').value = s.downloads_subdir_videos || 'videos';
  $('sConcurrency').value = s.image_concurrency || 6;
  $('sStreamConcurrency').value = s.stream_concurrency || 4;
  $('sQuality').value = s.video_quality_default || 'best';
  $('sSkip').checked = !!s.skip_existing;
  $('sCookiesFile').value = s.cookies_file || '';
  $('sCookiesBrowser').value = s.cookies_from_browser || '';
  // 保存后立即刷新「当前生效的 Cookies」，否则用户改完看不出有没有生效
  loadStatus().then(() => renderCookiesStat(statusInfo && statusInfo.cookies)).catch(() => {});
  renderCookiesStat(statusInfo && statusInfo.cookies);
  $('sProfileDir').value = s.browser_user_data_dir || '';
  $('sProfileHeaded').checked = !!s.browser_headed;
  refreshLoginState();
  renderProfileStat(statusInfo && statusInfo.browser_profile);
  $('sAllowPrivate').checked = !!s.allow_private_network;
}

async function loadSettings() {
  try {
    const s = await api('/api/settings');
    fillSettings(s);
  } catch (e) {
    $('settingsMsg').textContent = '读取设置失败：' + e.message;
  }
}

async function saveSettings() {
  const body = {
    downloads_subdir_images: $('sSubImages').value.trim(),
    downloads_subdir_videos: $('sSubVideos').value.trim(),
    image_concurrency: parseInt($('sConcurrency').value, 10) || 6,
    stream_concurrency: parseInt($('sStreamConcurrency').value, 10) || 4,
    video_quality_default: $('sQuality').value,
    skip_existing: $('sSkip').checked,
    browser_user_data_dir: $('sProfileDir').value.trim(),
    browser_headed: $('sProfileHeaded').checked,
    cookies_file: $('sCookiesFile').value.trim(),
    cookies_from_browser: $('sCookiesBrowser').value,
    allow_private_network: $('sAllowPrivate').checked,
  };
  try {
    const r = await api('/api/settings', { method: 'POST', body });
    fillSettings(r.settings || body);
    $('settingsMsg').textContent = '已保存';
    $('settingsMsg').style.color = 'var(--ok)';
    setTimeout(() => { $('settingsMsg').textContent = ''; }, 2500);
    toast('设置已保存', 'ok');
    loadStatus();
  } catch (e) {
    $('settingsMsg').textContent = '保存失败：' + e.message;
    $('settingsMsg').style.color = 'var(--err)';
  }
}

// ---------- 事件绑定 ----------
function bindEvents() {
  document.querySelectorAll('.tab').forEach(t => t.onclick = () => switchTab(t.dataset.tab));

  $('btnSearch').onclick = doSearch;
  $('kwInput').addEventListener('keydown', e => { if (e.key === 'Enter') doSearch(); });
  // 原来这里有个「搜视频」按钮把关键词带到视频页，已按用户要求移除：
  // 两个页面的关键词搜索各自独立，不再互相跳转。
  $('btnResetFilters').onclick = () => {
    const RESET = {
      fResPreset: '0', fResMax: '0', fMinSize: '0',
      fSize: '', fAspect: '', fType: '', fTime: '', fColor: '',
      fFace: '', fLicense: '', fFormats: '',
      fLimit: '40', fSafe: 'moderate', fEngines: 'bing',
    };
    Object.entries(RESET).forEach(([id, val]) => { if ($(id)) $(id).value = val; });
    $('fSkipExisting').checked = true;
    updateFilterSummary();
    toast('筛选已重置', 'info');
  };
  ['fResPreset', 'fResMax', 'fSize', 'fAspect', 'fType', 'fTime', 'fColor',
   'fFace', 'fLicense', 'fFormats', 'fMinSize', 'fEngines', 'fSafe'].forEach(id => {
    $(id).addEventListener('change', updateFilterSummary);
  });

  $('btnSelAll').onclick = selectAllSearch;
  $('btnSelInv').onclick = invertSearch;
  $('btnSelNone').onclick = clearSearch;
  $('btnDownloadSel').onclick = downloadSelectedImages;

  // 两个抓取器实例（图片页 / 视频页）各自绑定一套事件。
  // 原先是硬编码的单套 ID，合并后每个页面都有自己的那套 UI。
  [crawlerImg, crawlerVid].forEach(c => {
    const E = c.E;
    $(E('btnCrawl')).onclick = c.start;
    $(E('crawlInput')).addEventListener('keydown', ev => {
      if (ev.key === 'Enter') c.start();
    });
    $(E('cMode')).onchange = c.updateModeUI;
    c.updateModeUI();                     // 初始显隐与默认值
    $(E('btnCrawlCancel')).onclick = async () => {
      if (!c.state.jobId) return;
      try {
        await api('/api/crawl/cancel/' + c.state.jobId, { method: 'POST' });
        toast('已请求取消，正在收尾…', 'info');
      } catch (e) { toast('取消失败：' + e.message, 'err'); }
    };
    $(E('btnCrawlSelAll')).onclick = c.selAll;
    $(E('btnCrawlSelInv')).onclick = c.selInv;
    $(E('btnCrawlSelNone')).onclick = c.selNone;
    $(E('btnCrawlDownload')).onclick = c.download;
  });


  $('btnProbe').onclick = doProbe;
  $('videoInput').addEventListener('keydown', e => { if (e.key === 'Enter') doProbe(); });
  $('btnVideoDownload').onclick = doVideoDownload;
  $('btnVideoSearch').onclick = doVideoSearch;
  $('videoKw').addEventListener('keydown', e => { if (e.key === 'Enter') doVideoSearch(); });

  // 从批量列表「单独解析」后回到列表：只切显隐，列表数据一直在，不会丢勾选
  const backBtn = $('btnBackToPlaylist');
  if (backBtn) {
    backBtn.onclick = () => {
      $('videoInfo').hidden = true;
      $('playlistBox').hidden = false;
      $('videoInput').value = '';
      msg('videoMsg', '已返回批量列表，可继续勾选其它视频。', 'info');
    };
  }
  $('btnPlDownload').onclick = downloadPlaylistSelected;
  $('btnPlAll').onclick = () => {
    playlistSelected = new Set(playlistEntries.map((_, i) => i));
    document.querySelectorAll('#playlistList input[type=checkbox]').forEach(c => { c.checked = true; });
    $('plSelCount').textContent = playlistSelected.size;
  };
  $('btnPlNone').onclick = () => {
    playlistSelected = new Set();
    document.querySelectorAll('#playlistList input[type=checkbox]').forEach(c => { c.checked = false; });
    $('plSelCount').textContent = 0;
  };

  $('btnLibRefresh').onclick = loadLibrary;
  $('btnLibOpen').onclick = openDownloads;
  $('libFilter').onchange = () => { libFilterType = $('libFilter').value; renderLibrary(); };

  $('btnSaveSettings').onclick = saveSettings;
  if ($('btnOpenLogin')) $('btnOpenLogin').onclick = openLoginBrowser;
  $('btnReloadSettings').onclick = loadSettings;

  $('btnClearTasks').onclick = async () => {
    try { const r = await api('/api/tasks/clear-finished', { method: 'POST' }); toast(`已清除 ${r.removed} 条`, 'ok'); pollTasks(); }
    catch (e) { toast('清除失败：' + e.message, 'err'); }
  };
  $('btnToggleDock').onclick = (e) => {
    e.stopPropagation();
    dockCollapsed = !dockCollapsed;
    $('taskDock').classList.toggle('collapsed', dockCollapsed);
    $('btnToggleDock').textContent = dockCollapsed ? '展开' : '收起';
  };
  $('taskDockHead').onclick = (e) => {
    if (e.target.tagName === 'BUTTON') return;
    $('btnToggleDock').click();
  };

  // 关键词搜视频结果的勾选操作栏。
  // 「全选 / 反选」只作用于**当前筛出来的**条目 —— 这样「筛出 5 分钟以内的 → 全选 → 下载」
  // 就是一条顺手的批量流程；被筛掉的条目不会在背后被选中。
  $('videoKwSelAll').onclick = () => {
    videoFilteredEntries().forEach(e => videoSearchSel.add(e.i));
    applyVideoSearchSelectionUI();
  };
  $('videoKwSelInv').onclick = () => {
    videoFilteredEntries().forEach(e => {
      if (videoSearchSel.has(e.i)) videoSearchSel.delete(e.i);
      else videoSearchSel.add(e.i);
    });
    applyVideoSearchSelectionUI();
  };
  $('videoKwSelNone').onclick = () => {
    videoSearchSel = new Set();
    applyVideoSearchSelectionUI();
  };
  $('videoKwDownload').onclick = downloadVideoSearchSelected;

  // 筛选控件：改动即生效
  ['videoFilterDur', 'videoFilterSite', 'videoFilterSort', 'videoFilterKeepUnknown']
    .forEach(id => { const el = $(id); if (el) el.onchange = applyVideoSearchFilter; });
  ['videoFilterText', 'videoFilterMin', 'videoFilterMax'].forEach(id => {
    const el = $(id);
    if (el) el.oninput = () => { debounceVideoFilter(); };
  });
  $('videoFilterReset').onclick = resetVideoSearchFilter;

  // 「隐藏已下载」是**后端**过滤（要靠身份键判定），所以改它必须重搜一次，
  // 不能像别的筛选那样在本地重绘 —— 否则取消勾选后看不到被隐藏的那些条目。
  const hideDl = $('videoFilterHideDownloaded');
  if (hideDl) {
    hideDl.onchange = () => {
      if ($('videoKw').value.trim()) doVideoSearch();
    };
  }

  // 下载记录（台账）管理
  const btnManage = $('btnHistoryManage');
  if (btnManage) {
    btnManage.onclick = () => {
      switchTab('settings');
      loadHistory(true);
      const box = $('historyBox');
      if (box) box.scrollIntoView({ behavior: 'smooth', block: 'center' });
    };
  }
  const btnHR = $('btnHistoryRefresh');
  if (btnHR) btnHR.onclick = () => loadHistory(true);
  const btnHC = $('btnHistoryClear');
  if (btnHC) {
    btnHC.onclick = async () => {
      if (!confirm('确定要清空下载记录吗？\n\n清空后，以前下过的视频会在搜索结果里重新出现（文件本身不会被删除）。')) return;
      try {
        const r = await api('/api/history', { method: 'DELETE' });
        toast(`已清空 ${r.cleared} 条下载记录`, 'ok');
        loadHistory(true);
      } catch (e) { toast('清空失败：' + e.message, 'err'); }
    };
  }

  // 全局快捷键
  document.addEventListener('keydown', e => {
    if ((e.ctrlKey || e.metaKey) && e.key === 'a' && $('panel-search').classList.contains('active')
        && document.activeElement.tagName !== 'INPUT') {
      e.preventDefault(); selectAllSearch();
    }
  });
}

function updateFilterSummary() {
  const n = activeFilterCount();
  $('filterSummary').textContent = n ? `（已启用 ${n} 项筛选）` : '（清晰度 / 尺寸 / 颜色 / 时间 …）';
  $('filterSummary').style.color = n ? 'var(--warn)' : '';
}

// ---------- 启动 ----------
(async function init() {
  bindEvents();
  updateFilterSummary();
  bindModeSwitches();
  await loadStatus();
  await loadSettings();
  startTaskPolling();
  msg('searchMsg', '输入关键词开始搜索。默认仅启用 Bing；清晰度筛选会探测原图真实分辨率，请按需开启。', 'info');
})();
