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
let crawlProbeOrigin = null; // 从批量抓取卡片进入单条解析时，记住原卡片位置
let statusInfo = null;
let dockCollapsed = true;
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

/** 没有封面时的占位：图标 + 一句说明。
 *  比一块空白或一行纯文字更容易看懂「这里本该有图，但站点没给」。
 *  快手这类站点主页只吐直链、不给封面（封面在接口里被风控挡着），
 *  列表里就会大量走到这里。 */
function noCoverPh(text) {
  const ph = document.createElement('div');
  ph.className = 'ph';
  ph.innerHTML =
    '<svg viewBox="0 0 24 24" width="32" height="32" fill="none" stroke="currentColor"' +
    ' stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<rect x="2.5" y="4.5" width="19" height="15" rx="3"/>' +
    '<path d="M10 9.6v5.2l4.6-2.6z" fill="currentColor" stroke="none"/></svg>' +
    '<span>' + esc(text || '暂无预览图') + '</span>';
  return ph;
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

async function openExternalUrl(url, event = null) {
  if (event) {
    event.preventDefault();
    event.stopPropagation();
  }
  if (!/^https?:\/\//i.test(String(url || ''))) {
    toast('外部地址无效', 'err');
    return;
  }
  try {
    await api('/api/open-url', { method: 'POST', body: { url } });
  } catch (e) {
    const w = window.open(url, '_blank', 'noopener');
    if (!w) toast('无法打开外部页面：' + e.message, 'err');
  }
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
  // 记住用户最后停在哪个页面：下次打开直接回到这里（首页只在从没来过时出现）
  try { localStorage.setItem('md_last_tab', name); } catch (e) {}
  if (name === 'library') loadLibrary();
  if (name === 'login') loadPlatformStatuses();
}

/**
 * 一级页面内的「方式切换」：关键词搜索 / 粘贴链接抓取。
 *
 * 两块内容是各自独立的一整块（各自的输入框、结果区、轮询状态），
 * 切换只是整块显隐 —— 不重建 DOM，所以切回来时之前的结果还在。
 */
/**
 * 图片页的「关键词搜图 / 粘贴链接抓取」只有一套入口：顶部 omnibox 里的 #omniSeg。
 * 页面中部原本还叠了一条一模一样的模式条（#imageModeSwitch），两块按钮切的是
 * 同一对 hidden，纯重复占高，2026-11-07 已删除。这里只负责把面板切过去。
 */
function applyImageMode(mode) {
  const kw = $('imageKwMode'), link = $('imgLinkMode');
  if (kw) kw.hidden = (mode !== 'kw');
  if (link) link.hidden = (mode !== 'link');
  // 顶部 omnibox 是图片页唯一的输入口（页面里那两条 .searchbar 是 sr-only 隐藏的），
  // 切模式时必须把占位提示换掉，否则用户不知道该往框里放链接还是关键词。
  const omni = $('omniInput');
  if (omni) {
    omni.placeholder = mode === 'link'
      ? '粘贴图片列表页 / 相册 / 任意网页链接'
      : '输入关键词搜图，如：风景 壁纸 4K';
  }
}

function bindModeSwitches() {
  const configs = [
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
  // 1E：JS runtime 状态如实显示（yt-dlp 解 YouTube 需要；缺失时部分功能降级）
  const js = statusInfo.js_runtime;
  $('sJsRuntime').textContent = js ? (js.name + ' 可用') : '未找到（YouTube 等部分视频可能解析失败）';
  $('sJsRuntime').style.color = js ? 'var(--ok)' : 'var(--warn, #d80)';
  // V3.0：浏览器内核来源。内置 = 安装包自带、换电脑行为一致；
  // 系统 = 复用你装的 Chrome/Edge（那台机器卸了浏览器就会失效）。
  const br = statusInfo.browser;
  if (br) {
    const kindText = { bundled: '内置 Chromium', system: '系统浏览器', none: '不可用' }[br.kind] || br.kind;
    const el = $('sBrowserKernel');
    if (el) {
      el.textContent = kindText + (br.kind === 'system' && br.label ? `（${br.label.replace('系统 ', '')}）` : '');
      el.style.color = br.kind === 'none' ? 'var(--err)' : 'var(--ok)';
      el.title = br.exe || br.bundled_dir || '';
    }
  }
  // V3.0：验证码协助。只有「遇到验证码」才会用到，平时不影响普通抓取。
  const bsk = statusInfo.browser_skill;
  if (bsk) {
    const el = $('sBrowserSkill');
    if (el) {
      let text = '可用';
      let color = 'var(--ok)';
      let tip = [];
      if (!bsk.available) { text = '未包含（包不完整）'; color = 'var(--err)'; }
      else if (!bsk.extension_ok) { text = '扩展缺失'; color = 'var(--err)'; }
      else if (!bsk.daemon_running) { text = '待启动（首次遇到验证码时自动启动）'; color = 'var(--txt-dim)'; }
      if (bsk.available) {
        tip.push(`bsk: ${bsk.bsk_path}${bsk.bsk_bundled ? '（包内）' : '（系统）'}`);
        tip.push(`扩展: ${bsk.extension_dir}`);
        tip.push(`daemon: ${bsk.daemon_running ? '运行中 ' + bsk.daemon_version : '未运行'}`);
        tip.push(`已连接浏览器: ${bsk.browser_count}`);
      }
      el.textContent = text;
      el.style.color = color;
      el.title = tip.join('\n');
    }
  }
  $('sYtdlp').textContent = statusInfo.ytdlp_version;
  $('sDownloads').textContent = statusInfo.downloads_dir
    + (statusInfo.downloads_dir_restart_pending ? '（已改配置，重启程序后生效）' : '');
  $('sDisk').textContent = free ? humanSize(free) : '未知';
  renderServiceStatus(statusInfo);
  renderHelpEnv(statusInfo);
}

// Google 两条通道 / 会员兜底的状态展示（2026-11）
function renderServiceStatus(st) {
  if (!st) return;
  const g = st.google;
  const gel = $('sGoogleStat');
  if (g && gel) {
    const parts = [];
    if (g.render_available) parts.push('渲染可用');
    if (g.searx && g.searx.length) parts.push(`SearXNG ${g.searx.length} 个`);
    const mode = { auto: '自动', render: '仅渲染', searx: '仅 SearXNG' }[g.transport] || g.transport;
    gel.textContent = parts.length ? `${mode} · ${parts.join(' / ')}` : `${mode} · 两条通道都还没就绪`;
    gel.style.color = parts.length ? 'var(--ok)' : 'var(--warn, #d80)';
    gel.title = [g.proxy ? `渲染代理: ${g.proxy}` : '渲染代理: 直连',
      (g.searx || []).length ? `自备实例: ${(g.searx || []).join(', ')}` : '自备实例: 未配置'].join('\n');
  }
  const v = st.vip;
  const vel = $('sVipStat');
  if (v && vel) {
    const bits = [`资源站 ${v.resource_sites || 0} 个`];
    if (v.gateways) bits.push(`解析网关 ${v.gateways} 个`);
    bits.push(`海外源${v.overseas ? '开' : '关'}${v.download_proxy ? '（已配代理）' : '（未配代理）'}`);
    bits.push(`会员 Cookie ${(v.cookie_files || []).length} 份`);
    vel.textContent = (v.auto_unlock ? '自动兜底开' : '自动兜底关') + ' · ' + bits.join(' / ');
    vel.style.color = v.auto_unlock ? 'var(--ok)' : 'var(--txt-dim)';
    vel.title = `Cookie 目录: ${v.cookie_dir || ''}`;
  }
}

function renderLoginSummary(items) {
  const vals = Object.values(items || {});
  const ready = vals.filter(x => x.logged_in).length;
  const bad = vals.filter(x => x.relogin_required).length;
  const total = vals.length;
  const text = bad
    ? `${ready} / ${total} 个平台已登录 · ${bad} 个登录状态需要处理`
    : `${ready} / ${total} 个平台已登录`;
  const msg = $('platformLoginMsg');
  const help = $('hLoginEnv');
  if (msg) msg.textContent = text;
  if (help) help.textContent = text;
  return { ready, bad, total };
}

function renderHelpEnv(st) {
  const core = $('hCoreEnv'), ck = $('hCookieEnv');
  if (!st) return;
  if (core) core.textContent = `ffmpeg ${st.ffmpeg_available ? '✓' : '×'} · yt-dlp ${st.ytdlp_version || '未知'} · JS ${st.js_runtime ? '✓' : '×'}`;
  if (ck) ck.textContent = (st.cookies && st.cookies.count) ? `通用 Cookies ${st.cookies.count} 条` : '通用 Cookies 未配置';
  renderLoginSummary(st.platform_logins || {});
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

// 骨架微光：搜索发起时先占位，结果回来由 renderSearchResults / renderEmpty 覆盖
function skeletonCards(n) {
  return Array.from({ length: n }, () =>
    '<div class="skeleton-card"><div class="sk-thumb"></div>' +
    '<div class="sk-line"></div><div class="sk-line short"></div></div>').join('');
}

async function doSearch() {
  const kw = $('kwInput').value.trim();
  if (!kw) { toast('请先输入关键词', 'warn'); $('kwInput').focus(); return; }
  const btn = $('btnSearch');
  setBusy(btn, true, '搜索中…');
  msg('searchMsg', '<span class="spin"></span>正在搜索并探测原图真实分辨率…（清晰度筛选越严格越慢）');
  $('searchGrid').innerHTML = skeletonCards(8);
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
    // 顶部「开始抓取」的抓取中态在这里收尾：搜索失败/空结果也要恢复，
    // 不能只靠 15 秒兜底，否则按钮会僵在那儿。
    if (typeof setOmniBusy === 'function') setOmniBusy(false);
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
  img.src = it.thumbnail ? proxyUrl(it.thumbnail, it.referer || it.origin_page || '') : proxyUrl(it.url, it.referer || it.origin_page || '');
  img.onerror = function () {
    if (!this.dataset.fallback) {
      this.dataset.fallback = '1';
      this.src = proxyUrl(it.url, it.referer || it.origin_page || '');
    } else {
      this.replaceWith(noCoverPh('站点防盗链或需登录，没有预览图'));
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
  a1.href = it.url ? proxyUrl(it.url, it.referer || it.origin_page || '') : '#';
  a1.target = '_blank'; a1.rel = 'noopener';
  a1.textContent = '原图';
  a1.onclick = (e) => e.stopPropagation();
  links.appendChild(a1);
  if (it.source_page) {
    const a2 = document.createElement('a');
    a2.href = it.source_page; a2.target = '_blank'; a2.rel = 'noopener';
    a2.textContent = '来源页';
    a2.onclick = (e) => openExternalUrl(it.source_page, e);
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
        // 图片也默认最高档：同一张图有多个尺寸时挑最大的一份。
        prefer_quality: 'highest',
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
  const st = { items: [], sel: new Set(), jobId: null, timer: null,
               qualityTouched: false };

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

  // 抓取参数默认折叠成一行，摘要里必须写明「可以点开」，否则用户以为只是说明文字。
  // 图片页已改成一行内联参数、没有 summary，这里必须判空，否则 null.textContent 直接抛错。
  const sumEl = $(E('crawlOptSummary'));
  if (sumEl) {
    sumEl.textContent = isSite
      ? '（全站抓取：独立站整站走一遍 · 点开可调整）'
      : '（列表页 / 博主页逐条下钻 · 点开可调整）';
  }
  // 输入框示例必须按页面类型给（图片页用图片站、视频页用视频站）。
  // 原来是写死的电商链接，导致视频页显示「例：alibaba.com/trade/search…」——
  // 用户看到的示例跟当前页面完全对不上。
  $(E('crawlInput')).placeholder = isSite ? PH.site : PH.list;
}

function crawlSiteName() {
  try { return new URL(crawlOptions().url).hostname.replace(/^www\./, ''); }
  catch (e) { return 'site'; }
}

function clearCrawlResults(hideProgress = true) {
  stopCrawlPoll();
  st.jobId = '';
  st.items = [];
  st.sel = new Set();
  const grid = $(E('crawlGrid'));
  if (grid) grid.innerHTML = '';
  $(E('crawlToolbar')).hidden = true;
  $(E('crawlWarn')).hidden = true;
  $(E('crawlDetailBox')).hidden = true;
  if (hideProgress) $(E('crawlProgress')).hidden = true;
  $(E('crawlCount')).textContent = '';
  $(E('crawlNote')).textContent = '';
  updateCrawlSelCount();
}

async function doCrawl() {
  const opt = crawlOptions();
  if (!opt.url) { toast('请先粘贴列表页链接', 'err'); return; }
  // 两个页面各自的输入框（#imgCrawlInput / #vidCrawlInput）也能直接粘裸域名。
  // omnibox 那条链路在 shell.js 已补协议，这里补同一个兜底，
  // 免得「明明粘了 weibo.com/u/...」却弹「链接需要以 http:// 开头」。
  if (!/^https?:\/\//i.test(opt.url) &&
      /^(?:www\.|(?:[a-z0-9-]+\.)+[a-z]{2,24}(?:[:/?#]|$))/i.test(opt.url)) {
    opt.url = 'https://' + opt.url;
    const inp = $(E('crawlInput'));
    if (inp) inp.value = opt.url;
  }
  if (!/^https?:\/\//i.test(opt.url)) {
    toast('链接需要以 http:// 或 https:// 开头', 'err');
    return;
  }

  // 新任务默认清掉上一轮结果，避免几百张旧卡片继续堆在页面里。
  clearCrawlResults(true);
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
    if (typeof setOmniBusy === 'function') setOmniBusy(false);
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
    if (typeof setOmniBusy === 'function') setOmniBusy(false);
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
    if (typeof setOmniBusy === 'function') setOmniBusy(false);
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

  // 工具栏左侧摘要：#crawlStat 以前从来没人写入，顶部「本次结果」只能退回去数卡片，
  // 结果超过 400 条（网格只渲染前 400）就会显示错。这里给它真实总数与分类。
  const statEl = $(E('crawlStat'));
  if (statEl) {
    const s2 = full.summary || {};
    let brief = `共 ${st.items.length} 个媒体`;
    if (s2.image) brief += ` · 图片 ${s2.image}`;
    if (s2.video) brief += ` · 视频 ${s2.video}`;
    if (s2.stream) brief += ` · 流媒体 ${s2.stream}`;
    if (s2.thumbs) brief += ` · 缩略图 ${s2.thumbs}`;
    statEl.textContent = brief;
  }

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
  // 记住用户有没有手动改过清晰度。没改过就默认「最高」——视频和图片都一样：
  // 站点在列表/详情页里声明的 og:video 常是 240P 这一档，下拉停在「原样」
  // 等于默认下载最小清晰度（用户反馈的 V3.0.3 行为回退）。
  if (!sel.dataset.qBound) {
    sel.dataset.qBound = '1';
    sel.addEventListener('change', () => { st.qualityTouched = true; });
  }
  const hs = new Set();
  st.items.forEach(it => qualityHeightsOf(it).forEach(h => hs.add(h)));
  const heights = [...hs].sort((a, b) => b - a);
  const cur = sel.value;
  let opts = '<option value="">原样（抓到的档位）</option>'
    + '<option value="highest">最高</option><option value="lowest">最低</option>';
  heights.forEach(h => { opts += `<option value="${h}">${h}p</option>`; });
  sel.innerHTML = opts;
  // 尽量保留用户已选的值；这批结果里没有该档位就回落到默认档
  sel.value = [...sel.options].some(o => o.value === cur) ? cur : '';
  if (!st.qualityTouched && sel.value === '') sel.value = 'highest';
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

/**
 * 视频直链落盘用的文件名。
 *
 * CDN 地址最后一段是签名串 / base64（快手 BMjAy..._B5f9b49955、
 * 小红书 1040g2sg31...），后端 filename_from_url 只在「纯字母数字占位名」
 * 时才会改用标题；带下划线和日期的签名串不在那套判据里，于是下载下来
 * 就是一串随机字符。这里在提交下载时把作品标题显式写进 filename，
 * 后端的 sanitize_filename 负责清 Windows 非法字符、unique_path 负责重名。
 */
function crawlVideoFilename(it) {
  if (it.type !== 'video') return '';
  const t = String(it.title || '').replace(/\s+/g, ' ').trim();
  if (!t) return '';
  let ext = 'mp4';
  try {
    const m = new URL(it.url).pathname.match(/\.([a-z0-9]{2,5})$/i);
    if (m && !/^(jpg|jpeg|png|gif|webp|avif|bmp|svg|ico|html?|json)$/i.test(m[1])) {
      ext = m[1].toLowerCase();
    }
  } catch (e) { /* 地址无扩展名时用 mp4 */ }
  return t.slice(0, 80) + '.' + ext;
}

/**
 * 这一条批量结果能不能「单独解析」，能的话解析哪个地址。
 *
 * - is_page 条目：url 本身就是视频页地址，直接解析。
 * - 直链条目：若来自详情页下钻（origin_page 与 url 不同、且地址形态像单条
 *   内容页），解析它的来源视频页。下钻拿到的直链常是站点声明的最低档，
 *   用户要的是能进「单个视频链接」挑清晰度，而不是只有一条 240P 可下。
 *
 * 判据只看地址形态（内容 ID 参数 / 长数字 ID / video.<id> 段），不写站点规则；
 * 列表页 / 搜索页那种集合地址不会误当成单条视频页。
 */
function crawlProbeTarget(it) {
  // 后端统一给出的解析页地址（视频页条目 = 自己；直链条目 = 下钻到的详情页）。
  // 这是「所有站点都支持单条解析」的主路径，前端不再靠猜。
  if (it.probe_url) return it.probe_url;
  if (it.is_page) return it.url || '';
  const p = it.origin_page || '';
  if (!p || p === it.url) return '';
  try {
    const u = new URL(p);
    const segs = u.pathname.split('/').filter(Boolean);
    const idParam = /[?&](v|id|vid|video_?id|content_?id|media_?id|viewkey|watch)=/i.test(u.search);
    const idSeg = segs.some(s => /\d{5,}/.test(s) || /^video[.\-_][a-z0-9]{6,}/i.test(s));
    if (idParam || idSeg) return p;
  } catch (e) { /* 不是合法 URL，不给入口 */ }
  return '';
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
  // 重绘卡片时从选择集合恢复勾选视觉，避免“已选中但框里没勾”。
  const initiallySelected = st.sel.has(idx);
  card.classList.toggle('selected', initiallySelected);
  check.textContent = initiallySelected ? '✓' : '';
  card.appendChild(check);

  const thumb = document.createElement('div');
  thumb.className = 'thumb';
  let cover = it.thumbnail || '';
  if (/^http:\/\/[^/]*hdslb\.com\//i.test(cover)) {
    cover = cover.replace(/^http:/i, 'https:');
  }
  const coverReferer = it.origin_page || it.referer || '';
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
    const biliCover = /^https?:\/\/[^/]*(?:hdslb|bilivideo)\.com\//i.test(cover || '');
    // B站 CDN 封面本身可直接作为 <img> 加载。30张同时绕本地 /proxy
    // 会把同步代理线程拖慢，表现成“整批没有缩略图”；B站改成直连优先，
    // 失败时再回退代理。微博等需要 Cookie 的图床仍保持代理优先。
    img.src = biliCover ? cover : proxyUrl(cover || it.url, coverReferer);
    img.onerror = function () {
      if (biliCover && !this.dataset.proxyRetry && cover) {
        this.dataset.proxyRetry = '1';
        this.src = proxyUrl(cover, coverReferer);
        return;
      }
      if (!this.dataset.noRefRetry && cover) {
        this.dataset.noRefRetry = '1';
        this.src = proxyUrl(cover, '');
        return;
      }
      if (!this.dataset.directRetry && !biliCover && /^https?:\/\//i.test(cover || '')) {
        this.dataset.directRetry = '1';
        this.src = cover;
        return;
      }
      this.replaceWith(noCoverPh('暂无预览图'));
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
  a1.onclick = (e) => openExternalUrl(it.url, e);
  links.appendChild(a1);
  const sourcePage = it.origin_page || it.referer || (isPage ? it.url : '');
  if (sourcePage) {
    const a2 = document.createElement('a');
    a2.href = sourcePage; a2.target = '_blank'; a2.rel = 'noopener';
    a2.textContent = '来源页';
    a2.onclick = (e) => openExternalUrl(sourcePage, e);
    links.appendChild(a2);
  }
  // 「解析清晰度」入口 —— 跳到「单个视频链接」下载口。
  // 视频页条目解析自身；直链条目若来自详情页下钻，解析它的来源视频页。
  // 实测回归：站点搜索页退回网页抓取时整批都是 240P 直链（is_page=false），
  // 旧逻辑只给 is_page 加入口，用户看到的就是「批量抓的视频没法单独解析」。
  const probeUrl = crawlProbeTarget(it);
  if (isVideo && probeUrl) {
    const bp = document.createElement('button');
    bp.type = 'button';
    bp.className = 'btn tiny probe-btn';
    bp.textContent = '解析清晰度';
    bp.title = '跳到「单个视频链接」下载口，查看可选清晰度，挑好再下载';
    bp.onclick = (e) => {
      e.stopPropagation();
      crawlProbeOrigin = { prefix: P, idx, url: probeUrl };
      placeVideoProbeAboveBatch();
      gotoVideoProbe(probeUrl);
      doProbe();
    };
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
  // createCrawler 同时服务图片/视频两套实例，结果网格 ID 分别是
  // imgCrawlGrid / vidCrawlGrid。旧代码写死 #crawlGrid，导致“全选”只改了
  // st.sel 和数字，实际卡片完全没收到 selected/✓ 视觉状态。
  const grid = $(E('crawlGrid'));
  if (grid) grid.querySelectorAll('.card').forEach(c => {
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
  // 图片工具栏没有清晰度下拉（图片本来就没有「档位」概念），但后端支持
  // 按 URL 里的尺寸标记挑最大的一份。默认给 highest，保证图片也抓最高档。
  const preferQ = qualEl ? qualEl.value : 'highest';

  try {
    if (pages.length) {
      // 视频页 → yt-dlp。大批量必须分块提交：旧版一次传 300 条时会撞上
      // 请求模型 200 条上限而直接 422；同时也不能为每条瞬间创建线程。
      // 后端现在有固定 worker 队列，这里再按 500 条分块，数量再大也只是排队。
      const q = (preferQ === 'lowest') ? 'worst'
        : (preferQ === 'highest' || !preferQ) ? 'best' : preferQ;
      for (let off = 0; off < pages.length; off += 500) {
        const batch = pages.slice(off, off + 500);
        const r = await api('/api/download/videos', {
          method: 'POST',
          body: {
            items: batch.map(i => ({ url: i.url, title: (i.title || '').slice(0, 60) })),
            subdir: sub,
            quality: q,
            concurrency: Math.max(1, Math.min(8, conc)),
          },
        });
        submitted += (r && r.count) || batch.length;
      }
    }
    if (plain.length) {
      for (let off = 0; off < plain.length; off += 500) {
        const batch = plain.slice(off, off + 500);
        await api('/api/download/direct', {
          method: 'POST',
          body: {
            items: batch.map(i => ({
              url: i.url,
              referer: i.referer || '',
              title: i.title || '',
              origin_page: i.origin_page || '',
              thumbnail: i.thumbnail || '',
              // 有作品标题时按标题落盘，不用 CDN 的签名串当文件名。
              filename: crawlVideoFilename(i),
              // 同一媒体的其它档位地址：首选地址下不了时后端会自动换用。
              alternates: i.variants || [],
            })),
            subdir: sub,
            concurrency: conc,
            skip_existing: skip,
            prefer_quality: preferQ,
            title: `批量抓取 ${batch.length} 个文件`,
          },
        });
        submitted += batch.length;
      }
    }
    if (streams.length) {
      // 流媒体也分块，避免极大请求体；每块在后端仍按流媒体并发设置执行。
      for (let off = 0; off < streams.length; off += 200) {
        const batch = streams.slice(off, off + 200);
        await api('/api/download/stream', {
          method: 'POST',
          body: {
            items: batch.map(i => ({
              url: i.url, referer: i.referer || '', title: i.title || '',
            })),
            subdir: sub + '/streams',
            prefer_quality: preferQ === 'highest' ? 'highest'
              : preferQ === 'lowest' ? 'lowest' : preferQ,
          },
        });
        submitted += batch.length;
      }
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
    clear: clearCrawlResults,
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
function placeVideoProbeAboveBatch() {
  const toolbar = $('vidCrawlToolbar');
  const msgEl = $('videoMsg');
  const infoEl = $('videoInfo');
  if (!toolbar || !toolbar.parentNode || !msgEl || !infoEl) return;
  toolbar.parentNode.insertBefore(msgEl, toolbar);
  toolbar.parentNode.insertBefore(infoEl, toolbar);
}

function restoreVideoProbeHome() {
  const home = $('videoInfoHome');
  const msgEl = $('videoMsg');
  const infoEl = $('videoInfo');
  if (!home || !home.parentNode || !msgEl || !infoEl) return;
  home.parentNode.insertBefore(msgEl, home.nextSibling);
  home.parentNode.insertBefore(infoEl, msgEl.nextSibling);
}

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
  const pageLink = $('vPageLink');
  pageLink.href = res.webpage_url || '#';
  pageLink.onclick = (e) => {
    if (res.webpage_url) openExternalUrl(res.webpage_url, e);
    else {
      e.preventDefault();
      e.stopPropagation();
    }
  };

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
    const hasCrawl = !!crawlProbeOrigin;
    backBar.hidden = !(hasList || hasCrawl);
    const hint = $('backToPlaylistHint');
    if (hint) {
      hint.textContent = hasCrawl
        ? '可返回刚才的批量抓取结果，并自动定位到这条视频'
        : `列表里还有 ${playlistEntries.length} 个视频，可返回继续挑选`;
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

/**
 * 搜索结束却没有结果时，把结果区恢复成「有归属的空态」。
 * 原来 doVideoSearch 一上来就 box.hidden = true，失败或 0 条时再也不打开，
 * 用户看到的是一整块什么都没有的空白（2026-11-07 版面整改时发现）。
 */
function restoreVideoEmpty() {
  const box = $('videoSearchResult');
  if (!box || box.querySelector('.vitem')) return;
  box.hidden = false;
  box.innerHTML = '';
  const d = document.createElement('div');
  d.className = 'empty';
  d.dataset.seed = '1';
  d.textContent = '没搜到结果；换个关键词，或把视频源换成「全网（多来源）」再试';
  box.appendChild(d);
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
    if (!videoSearchItems.length) restoreVideoEmpty();
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

    const addVideoFallback = () => {
      const v = document.createElement('video');
      v.src = API + f.href;
      v.preload = 'metadata';
      v.muted = true;
      v.style.width = '100%';
      v.style.height = '100%';
      v.style.objectFit = 'cover';
      v.onerror = function () {
        const ph = document.createElement('div');
        ph.className = 'ph';
        ph.textContent = '视频';
        this.replaceWith(ph);
      };
      thumb.appendChild(v);
    };

    if (!f.is_video) {
      const img = document.createElement('img');
      img.loading = 'lazy';
      img.src = API + f.href;
      img.onerror = function () {
        this.replaceWith(noCoverPh('暂无预览图'));
      };
      thumb.appendChild(img);
    } else if (f.thumbnail) {
      const img = document.createElement('img');
      img.loading = 'lazy';
      img.referrerPolicy = 'no-referrer';
      img.src = proxyUrl(f.thumbnail, f.source_url || '');
      img.onerror = function () {
        this.remove();
        addVideoFallback();
      };
      thumb.appendChild(img);
    } else {
      addVideoFallback();
    }
    card.appendChild(thumb);

    const body = document.createElement('div');
    body.className = 'card-body';
    const t = document.createElement('div');
    t.className = 'card-title';
    t.textContent = f.title || f.name;
    t.title = f.rel;
    body.appendChild(t);

    const m = document.createElement('div');
    m.className = 'card-meta';
    m.innerHTML = `<span class="badge ${f.is_video ? 'video' : ''}">${f.is_video ? '视频' : '图片'}</span>
      <span class="badge">${esc(humanSize(f.size))}</span>
      ${f.dir ? `<span class="badge" title="${esc(f.dir)}">${esc(f.dir)}</span>` : ''}`;
    body.appendChild(m);

    const links = document.createElement('div');
    links.className = 'card-links';

    const reveal = document.createElement('button');
    reveal.type = 'button';
    reveal.className = 'btn tiny';
    reveal.textContent = '打开文件';
    reveal.onclick = async (e) => {
      e.stopPropagation();
      try {
        await api('/api/downloads/reveal', { method: 'POST', body: { rel: f.rel } });
      } catch (err) { toast('打开失败：' + err.message, 'err'); }
    };
    links.appendChild(reveal);

    const openDir = document.createElement('button');
    openDir.type = 'button';
    openDir.className = 'btn tiny ghost';
    openDir.textContent = '目录';
    openDir.onclick = async (e) => {
      e.stopPropagation();
      try {
        await api('/api/downloads/open', { method: 'POST', body: { sub: f.dir || '' } });
      } catch (err) { toast('打开目录失败：' + err.message, 'err'); }
    };
    links.appendChild(openDir);

    const preview = document.createElement('a');
    preview.href = API + f.href;
    preview.target = '_blank';
    preview.rel = 'noopener';
    preview.textContent = '预览';
    preview.onclick = (e) => e.stopPropagation();
    links.appendChild(preview);

    if (/^https?:\/\//i.test(f.source_url || '')) {
      const source = document.createElement('a');
      source.href = f.source_url;
      source.target = '_blank';
      source.rel = 'noopener';
      source.textContent = '来源页';
      source.onclick = (e) => openExternalUrl(f.source_url, e);
      links.appendChild(source);
    }

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

// ---------- 并发状态 ----------
// 用户设的是「最大并发」，实际同时跑几个由后端按平台风控状态实时调节。
// 必须显示出来：不显示的话，用户看到下载莫名变慢只会以为程序坏了，
// 而实际上很可能是程序察觉到 412 主动降速在保护账号。
let concTimer = null;

function concTitle(g) {
  const base = `用户最大并发 ${g.ceiling}，当前实际并发 ${g.capacity}`;
  if (g.state !== 'throttled') return `${g.platform_label}·${g.kind_label}：${base}，正常`;
  const why = g.reason_label ? `，原因：${g.reason_label}` : '';
  const tail = g.reason === 'rate_limit' || g.reason === 'verify'
    ? `。连续稳定后会自动恢复到 ${g.ceiling}` : '';
  return `${g.platform_label}·${g.kind_label}：${base}，已被自动降速${why}${tail}`;
}

async function pollConcurrency() {
  let res;
  try { res = await api('/api/concurrency'); } catch (e) { return; }
  const bar = $('concBar');
  if (!bar) return;
  // 只报「被降过速」和「正在跑」的闸门；一堆空闲闸门没必要占地方
  const gates = (res.gates || []).filter(
    g => g.state === 'throttled' || (g.running || 0) > 0);
  if (!gates.length) {
    bar.hidden = true;
    bar.innerHTML = '';
    bar.title = '';
    return;
  }
  const chips = gates.slice(0, 2).map(g => g.state === 'throttled'
    ? `<span class="conc-item warn" title="${esc(concTitle(g))}">${esc(g.platform_label)}·${esc(g.kind_label)} ${g.capacity}/${g.ceiling} ${esc(g.reason_label || '限速')}</span>`
    : `<span class="conc-item" title="${esc(concTitle(g))}">${esc(g.platform_label)}·${esc(g.kind_label)} ${g.capacity}/${g.ceiling}${g.running ? `（跑 ${g.running}）` : ''}</span>`);
  if (gates.length > 2) chips.push(`<span class="conc-item">+${gates.length - 2}</span>`);
  bar.hidden = false;
  bar.innerHTML = chips.join('');
  bar.title = gates.slice(0, 6).map(concTitle).join('\n');
}

function startConcPolling() {
  if (concTimer) return;
  pollConcurrency();
  concTimer = setInterval(pollConcurrency, 5000);
}

// ---------- 任务面板 ----------
// 哪些任务的「失败明细」处于展开状态。必须放在渲染函数外面：
// pollTasks 每 1.2 秒重建整个列表，状态存在 DOM 里会被下一次刷新抹掉。
const EXPANDED_FAILS = new Set();

async function copyToClipboard(text) {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch (e) { /* 落到下面的兜底 */ }
  // WebView2 / 非安全上下文下 clipboard API 可能不可用，用隐藏 textarea 兜底
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.style.position = 'fixed';
  ta.style.opacity = '0';
  document.body.appendChild(ta);
  ta.select();
  let ok = false;
  try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
  document.body.removeChild(ta);
  if (!ok) throw new Error('浏览器拒绝了剪贴板写入');
  return true;
}

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
  const totalTasks = Number.isFinite(Number(res.total)) ? Number(res.total) : tasks.length;
  $('taskCount').textContent = totalTasks;

  // 面板只渲染最近 30 条，但是否继续轮询必须看完整队列。
  // 否则 300 条批量任务里“最新 30 条”先结束后，前端会误以为全部结束并停止刷新。
  const running = Number(res.active || 0) > 0
    || tasks.some(t => t.status === 'running' || t.status === 'queued');
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

    // 失败明细：图片/流媒体任务逐条记在 items 里；视频批量是「一条视频一个任务」，
    // 失败信息在 t.error 上，items 为空。两种都要能列出来，否则用户只知道失败了几条。
    const fails = (t.items || []).filter(it => !it.ok && String(it.error || '') !== '已取消');
    const videoFailed = t.kind !== 'image' && t.kind !== 'stream' && t.status === 'failed';
    const failCount = fails.length || (videoFailed ? 1 : 0);
    const expanded = EXPANDED_FAILS.has(t.id);

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

    // 失败明细区块：默认收起，点一下展开。轮询每 1.2 秒重建整个列表，
    // 所以展开状态必须存在 EXPANDED_FAILS 里，否则刚点开就被下一次刷新收回去。
    let failBlock = '';
    if (failCount > 0) {
      const rows = fails.length
        ? fails.map(it => `
            <div class="fail-row">
              <div class="fail-url" title="${esc(it.url || '')}">${esc(it.url || '(无地址)')}</div>
              <div class="fail-why">${esc(it.error || '未知原因')}</div>
            </div>`).join('')
        : `<div class="fail-row">
             <div class="fail-url" title="${esc(t.video_url || '')}">${esc(t.video_url || '(无地址)')}</div>
             <div class="fail-why">${esc(t.error || '未知原因')}</div>
           </div>`;
      failBlock = `
        <div class="task-fails">
          <div class="fail-head">
            <button class="btn tiny ghost" data-act="toggle">${expanded ? '▾' : '▸'} ${failCount} 条失败明细</button>
            <button class="btn tiny ghost" data-act="copy">复制失败地址</button>
            ${t.status === 'running' || t.status === 'queued'
              ? '' : `<button class="btn tiny" data-act="retry">重试失败 ${failCount} 条</button>`}
          </div>
          <div class="fail-list"${expanded ? '' : ' hidden'}>${rows}</div>
        </div>`;
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
      ${failBlock}
      ${t.files && t.files.length ? `<div class="task-files">已保存 ${t.files.length} 个文件到 ${esc(t.output_dir || '')}</div>` : ''}`;

    el.querySelectorAll('button').forEach(b => {
      b.onclick = async () => {
        const act = b.dataset.act;
        try {
          if (act === 'cancel') { await api(`/api/tasks/${t.id}/cancel`, { method: 'POST' }); toast('已请求取消', 'info'); }
          else if (act === 'remove') { EXPANDED_FAILS.delete(t.id); await api(`/api/tasks/${t.id}`, { method: 'DELETE' }); pollTasks(); }
          else if (act === 'open') {
            await api('/api/downloads/open', { method: 'POST', body: { path: t.output_dir || '' } });
            toast('已打开下载目录', 'ok');
          }
          else if (act === 'toggle') {
            if (EXPANDED_FAILS.has(t.id)) EXPANDED_FAILS.delete(t.id);
            else EXPANDED_FAILS.add(t.id);
            const on = EXPANDED_FAILS.has(t.id);
            const list = el.querySelector('.fail-list');
            if (list) list.hidden = !on;
            b.textContent = `${on ? '▾' : '▸'} ${failCount} 条失败明细`;
          }
          else if (act === 'copy') {
            const urls = (fails.length ? fails.map(it => it.url) : [t.video_url]).filter(Boolean);
            if (!urls.length) { toast('没有可复制的地址', 'warn'); return; }
            await copyToClipboard(urls.join('\n'));
            toast(`已复制 ${urls.length} 个失败地址`, 'ok');
          }
          else if (act === 'retry') {
            const r = await api(`/api/tasks/${t.id}/retry-failed`, { method: 'POST' });
            toast(`已重新提交 ${r.count} 条失败下载`, 'ok');
            EXPANDED_FAILS.delete(t.id);
            startTaskPolling();      // 重试产生新任务，轮询必须在跑
            pollTasks();
          }
        } catch (e) { toast('操作失败：' + e.message, 'err'); }
      };
    });
    box.appendChild(el);
  });
}

// ---------- 平台登录中心 ----------
const PLATFORM_ORDER = ['ig', 'xhs', 'dy', 'ks', 'tiktok', 'pin', 'bili', 'wb', 'zhihu', 'yt'];
let platformStatusCache = {};

function platformStateLabel(st) {
  if (st.in_use) return ['登录窗口已打开', 'active'];
  if (st.state === 'ready') return ['已登录', 'ready'];
  if (st.state === 'verification_required') return ['需验证', 'bad'];
  if (st.state === 'invalid') return ['登录失效', 'bad'];
  if (st.state === 'expired') return ['已过期', 'bad'];
  if (st.state === 'unknown') return ['待确认', 'warn'];
  return ['未登录', 'idle'];
}

function renderPlatformCards(targetId, items) {
  const box = $(targetId);
  if (!box) return;
  box.innerHTML = '';
  PLATFORM_ORDER.forEach(key => {
    const st = items[key];
    if (!st) return;
    const [label, cls] = platformStateLabel(st);
    const el = document.createElement('div');
    el.className = 'platform-login-card';
    const when = st.last_login_ts ? new Date(st.last_login_ts * 1000).toLocaleString() : '';
    el.innerHTML = `
      <div class="platform-login-head">
        <b>${esc(st.name || key)}</b>
        <span class="login-state ${cls}">${esc(label)}</span>
      </div>
      <div class="hint">${st.reason ? esc(st.reason) :
        (when ? '上次登录：' + esc(when) : '尚未建立本机登录态')}</div>
      <div class="hint">Cookies：${Number(st.cookie_count || 0)} 条 · 登录凭据：${Number(st.auth_cookie_count || 0)} 条</div>
      <button class="btn tiny ${st.relogin_required ? 'danger' : ''}" data-platform="${key}">
        ${st.state === 'verification_required' ? '继续验证' :
          (st.logged_in && !st.relogin_required ? '重新登录' : '登录')}
      </button>`;
    el.querySelector('button').onclick = () => loginPlatform(key);
    box.appendChild(el);
  });
}

function renderLoginAlert(items) {
  const el = $('loginAlert');
  if (!el) return;
  const bad = Object.values(items || {}).filter(x => x.relogin_required);
  if (!bad.length) {
    el.hidden = true;
    el.innerHTML = '';
    return;
  }
  el.hidden = false;
  el.innerHTML = `检测到 <b>${bad.map(x => esc(x.name)).join('、')}</b> 登录状态需要处理。请打开“平台登录”查看具体状态。`;
}

async function loadPlatformStatuses(force = false, platform = '') {
  try {
    let url = '/api/platforms/status';
    if (force) {
      const q = new URLSearchParams({ refresh: 'true' });
      if (platform) q.set('platform', platform);
      url += '?' + q.toString();
    }
    const r = await api(url);
    platformStatusCache = r.platforms || {};
    renderPlatformCards('platformLoginGrid', platformStatusCache);
    renderPlatformCards('wizardLoginGrid', platformStatusCache);
    renderLoginAlert(platformStatusCache);
    renderLoginSummary(platformStatusCache);
    return platformStatusCache;
  } catch (e) {
    const msg = $('platformLoginMsg');
    if (msg) msg.textContent = '状态读取失败：' + e.message;
    return {};
  }
}

async function watchPlatformLogin(platform) {
  let rounds = 0;
  let seenOpen = false;
  const timer = setInterval(async () => {
    rounds += 1;
    try {
      const r = await api('/api/platforms/status?platform=' + encodeURIComponent(platform));
      const st = (r.platforms || {})[platform];
      if (!st) return;
      if (st.in_use) seenOpen = true;
      platformStatusCache = r.platforms || platformStatusCache;
      renderPlatformCards('platformLoginGrid', platformStatusCache);
      renderPlatformCards('wizardLoginGrid', platformStatusCache);
      renderLoginAlert(platformStatusCache);
      renderLoginSummary(platformStatusCache);
      // 子进程刚启动时 Chrome 可能还没来得及持有 Profile 锁；不能看到一次
      // in_use=false 就误判“登录窗口已经关闭”。至少观察到打开过，或给 12 秒启动宽限。
      if (!st.in_use && (seenOpen || rounds >= 8)) {
        clearInterval(timer);
        await loadPlatformStatuses(true, platform);
      } else if (rounds >= 600) {
        clearInterval(timer);
      }
    } catch (e) { /* 下一轮再试 */ }
  }, 1500);
}

async function loginPlatform(platform) {
  try {
    const r = await api('/api/platforms/login', { method: 'POST', body: { platform } });
    toast(`${r.name || platform} 登录窗口已打开；登录完成后关闭窗口即可`, 'ok');
    setTimeout(() => loadPlatformStatuses(false, platform), 800);
    watchPlatformLogin(platform);
  } catch (e) {
    toast('打开登录窗口失败：' + e.message, 'err');
  }
}

// ---------- 设置 ----------
// ---------- 设置 ----------
// 4 套基础外观。旧配置里的 gray / dark / black / light 仍能被接受，
// 通过 BASE_ALIAS 平滑映射到新主题，避免老用户升级后外观跳变。
const BASE_ALIAS = { black: 'obsidian', gray: 'slate', dark: 'obsidian', light: 'ivory' };
const BASE_THEMES = new Set(['obsidian', 'slate', 'ivory', 'midnight']);
const DARK_MODES = new Set(['obsidian', 'slate', 'midnight']);

const ACCENT_THEMES = {
  blue:   { accent: '#4f8cff', hi: '#74a6ff', lightHi: '#2563d9' },
  cyan:   { accent: '#18a8c9', hi: '#4cc6df', lightHi: '#0f7f98' },
  violet: { accent: '#8b5cf6', hi: '#a98bff', lightHi: '#6d3de0' },
  green:  { accent: '#2faa6e', hi: '#58c98f', lightHi: '#16794b' },
  orange: { accent: '#e28735', hi: '#f1a45d', lightHi: '#b85f12' },
  rose:   { accent: '#db5c86', hi: '#ef82a7', lightHi: '#b53e68' },
  indigo: { accent: '#6366f1', hi: '#9b9dfa', lightHi: '#4338ca' },
  teal:   { accent: '#4cc9c0', hi: '#8ce0da', lightHi: '#0f8f88' },
};

function applyBaseTheme(name) {
  const requested = BASE_ALIAS[name] || name;
  const key = BASE_THEMES.has(requested) ? requested : 'ivory';
  document.documentElement.dataset.mode = key;
  const currentAccent = document.documentElement.dataset.accent || 'blue';
  const sel = $('sUiMode');
  if (sel && sel.value !== key) sel.value = key;
  document.querySelectorAll('.base-theme-card').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.mode === key);
  });
  applyAccentTheme(currentAccent);
  return key;
}

function applyAccentTheme(name) {
  const key = ACCENT_THEMES[name] ? name : 'blue';
  const t = ACCENT_THEMES[key];
  const root = document.documentElement.style;
  // 深色底用亮一档的强调字色，浅色底用深一档，保证 4.5:1 可读
  const dark = DARK_MODES.has(document.documentElement.dataset.mode);
  root.setProperty('--accent', t.accent);
  root.setProperty('--accent-hi', dark ? t.hi : t.lightHi);
  // 亮强调色（青/绿/橙/石墨青）上白字达不到 3:1，按相对亮度自动切深色字，
  // 保证 8 种强调色的主按钮文字都过 WCAG AA 大字阈值。
  const ch = t.accent.slice(1).match(/../g).map(x => parseInt(x, 16) / 255)
    .map(v => v <= .03928 ? v / 12.92 : Math.pow((v + .055) / 1.055, 2.4));
  const L = .2126 * ch[0] + .7152 * ch[1] + .0722 * ch[2];
  const whiteContrast = 1.05 / (L + .05);
  const darkContrast = (L + .05) / (.012 + .05);
  root.setProperty('--accent-on', whiteContrast >= darkContrast ? '#ffffff' : '#172033');
  root.setProperty('--accent-soft', `color-mix(in srgb, ${t.accent} 16%, var(--card))`);
  root.setProperty('--accent-border', `color-mix(in srgb, ${t.accent} 48%, var(--line))`);
  document.documentElement.dataset.accent = key;
  const sel = $('sAccent');
  if (sel && sel.value !== key) sel.value = key;
  document.querySelectorAll('.theme-swatch').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.theme === key);
  });
  return key;
}

function renderCookiesStat(ck) {
  const el = $('sCookiesStat');
  if (!el) return;
  if (!ck || !ck.count) {
    el.textContent = (ck && ck.file) ? '文件读不到或没有条目（检查路径与格式）' : '未配置';
    return;
  }
  const doms = (ck.domains || []).slice(0, 4).join('、');
  el.textContent = `${ck.count} 条，来自 ${ck.file.split(/[\\/]/).pop()}` + (doms ? `（含 ${doms}）` : '');
}

function fillSettings(s) {
  $('sSubImages').value = s.downloads_subdir_images || 'images';
  $('sSubVideos').value = s.downloads_subdir_videos || 'videos';
  $('sDownloadsDir').value = s.downloads_dir || '';
  $('sConcurrency').value = s.image_concurrency || 6;
  $('sStreamConcurrency').value = s.stream_concurrency || 4;
  $('sQuality').value = s.video_quality_default || 'best';
  $('sSkip').checked = !!s.skip_existing;
  $('sCookiesFile').value = s.cookies_file || '';
  $('sCookiesBrowser').value = s.cookies_from_browser || '';
  $('sAllowPrivate').checked = !!s.allow_private_network;
  if ($('sCloseBehavior')) $('sCloseBehavior').value = s.close_behavior || 'exit';
  // 2026-11：Google 搜图接入 / 会员视频兜底
  if ($('sGoogleProxy')) $('sGoogleProxy').value = s.google_proxy || '';
  if ($('sGoogleTransport')) $('sGoogleTransport').value = s.google_transport || 'auto';
  if ($('sGoogleSearx')) $('sGoogleSearx').value = s.google_searx || '';
  if ($('sVipAuto')) $('sVipAuto').checked = s.vip_auto_unlock !== false;
  if ($('sVipCookieDir')) $('sVipCookieDir').value = s.vip_cookie_dir || '';
  if ($('sVipParse')) $('sVipParse').value = s.vip_parse_endpoints || '';
  if ($('sVipResourceSites')) $('sVipResourceSites').value = s.vip_resource_sites || '';
  if ($('sVipOverseas')) $('sVipOverseas').checked = s.vip_overseas !== false;
  if ($('sDownloadProxy')) $('sDownloadProxy').value = s.download_proxy || '';
  applyBaseTheme(s.ui_mode || 'light');
  applyAccentTheme(s.ui_accent || 'blue');
  renderCookiesStat(statusInfo && statusInfo.cookies);
  renderServiceStatus(statusInfo);
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
    downloads_dir: $('sDownloadsDir').value.trim(),
    image_concurrency: parseInt($('sConcurrency').value, 10) || 6,
    stream_concurrency: parseInt($('sStreamConcurrency').value, 10) || 4,
    video_quality_default: $('sQuality').value,
    skip_existing: $('sSkip').checked,
    cookies_file: $('sCookiesFile').value.trim(),
    cookies_from_browser: $('sCookiesBrowser').value,
    allow_private_network: $('sAllowPrivate').checked,
    close_behavior: $('sCloseBehavior') ? $('sCloseBehavior').value : 'exit',
    ui_mode: $('sUiMode') ? $('sUiMode').value : 'light',
    ui_accent: $('sAccent') ? $('sAccent').value : 'blue',
    // 2026-11：Google 搜图接入 / 会员视频兜底
    google_proxy: $('sGoogleProxy') ? $('sGoogleProxy').value.trim() : '',
    google_transport: $('sGoogleTransport') ? $('sGoogleTransport').value : 'auto',
    google_searx: $('sGoogleSearx') ? $('sGoogleSearx').value.trim() : '',
    vip_auto_unlock: $('sVipAuto') ? $('sVipAuto').checked : true,
    vip_cookie_dir: $('sVipCookieDir') ? $('sVipCookieDir').value.trim() : '',
    vip_parse_endpoints: $('sVipParse') ? $('sVipParse').value.trim() : '',
    vip_resource_sites: $('sVipResourceSites') ? $('sVipResourceSites').value.trim() : '',
    vip_overseas: $('sVipOverseas') ? $('sVipOverseas').checked : true,
    download_proxy: $('sDownloadProxy') ? $('sDownloadProxy').value.trim() : '',
  };
  try {
    const r = await api('/api/settings', { method: 'POST', body });
    fillSettings(r.settings || body);
    $('settingsMsg').textContent = r.restart_required
      ? '已保存（下载根目录将在重启程序后生效）' : '已保存';
    $('settingsMsg').style.color = r.restart_required ? 'var(--warn)' : 'var(--ok)';
    toast(r.restart_required ? '已保存；下载根目录重启后生效' : '设置已保存', 'ok');
    await loadStatus();
    setTimeout(() => { $('settingsMsg').textContent = ''; }, 4000);
  } catch (e) {
    $('settingsMsg').textContent = '保存失败：' + e.message;
    $('settingsMsg').style.color = 'var(--err)';
  }
}

// ---------- 首次使用向导 ----------
let wizardStep = 1;

function showWizardStep(n) {
  wizardStep = Math.max(1, Math.min(3, n));
  [1, 2, 3].forEach(i => {
    const el = $('wizardStep' + i);
    if (el) el.hidden = i !== wizardStep;
  });
  $('wizardStepLabel').textContent = `步骤 ${wizardStep} / 3`;
  $('wizardBack').hidden = wizardStep === 1;
  $('wizardNext').hidden = wizardStep === 3;
  $('wizardFinish').hidden = wizardStep !== 3;
}

async function loadOnboarding() {
  try {
    const r = await api('/api/onboarding');
    $('wFfmpeg').textContent = r.ffmpeg_available ? '可用 ✓' : '缺失 ×';
    $('wJs').textContent = r.js_runtime ? '可用 ✓' : '缺失 ×';
    $('wDownloadNow').textContent = r.downloads_dir || '未知';
    platformStatusCache = r.platforms || platformStatusCache;
    renderPlatformCards('wizardLoginGrid', platformStatusCache);
    if (!r.complete) {
      $('firstUseWizard').hidden = false;
      showWizardStep(1);
    }
  } catch (e) {
    console.warn('onboarding status failed', e);
  }
}

async function wizardNext() {
  if (wizardStep === 2) {
    const dd = $('wDownloadsDir').value.trim();
    try {
      await api('/api/settings', { method: 'POST', body: { downloads_dir: dd } });
    } catch (e) {
      toast('下载目录设置失败：' + e.message, 'err');
      return;
    }
  }
  showWizardStep(wizardStep + 1);
  if (wizardStep === 3) loadPlatformStatuses();
}

async function finishWizard() {
  try {
    await api('/api/onboarding/complete', { method: 'POST' });
    $('firstUseWizard').hidden = true;
    toast('首次使用设置已完成', 'ok');
  } catch (e) {
    toast('保存向导状态失败：' + e.message, 'err');
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
    const clearBtn = $(E('btnCrawlClear'));
    if (clearBtn) clearBtn.onclick = () => c.clear(true);
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
      if (crawlProbeOrigin) {
        const origin = crawlProbeOrigin;
        const crawler = crawlers[origin.prefix];
        const grid = crawler ? $(crawler.E('crawlGrid')) : null;
        const card = grid ? grid.querySelector(`[data-idx="${origin.idx}"]`) : null;
        if (card) {
          card.scrollIntoView({ behavior: 'smooth', block: 'center' });
          card.classList.add('probe-return');
          setTimeout(() => card.classList.remove('probe-return'), 1800);
        }
        crawlProbeOrigin = null;
        restoreVideoProbeHome();
        msg('videoMsg', '已返回批量抓取结果，并定位到刚才解析的视频。', 'info');
        return;
      }
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
  $('btnReloadSettings').onclick = loadSettings;
  // 会员 Cookie 校验：只读本地目录 + 调一次 B 站 nav 接口，不发任何账号信息出去
  if ($('btnVipCheck')) $('btnVipCheck').onclick = async () => {
    const lbl = $('vipStatMsg');
    if (lbl) lbl.textContent = '正在校验…';
    try {
      const r = await api('/api/vip/check-cookies', { method: 'POST', body: {} });
      if (!r.count) {
        if (lbl) lbl.textContent = '目录里还没有 Cookie 文件（把 .txt 放进 Cookie 目录后重试）。';
        return;
      }
      const lines = (r.items || []).map(it =>
        `${it.file}: ${it.vip ? '大会员' : (it.vip_label || '普通/未登录')}${it.uname ? ' · ' + it.uname : ''}${it.error ? ' · ' + it.error : ''}`);
      if (lbl) lbl.innerHTML = lines.join('<br>');
      toast(`校验完成：${r.vip} / ${r.count} 份是大会员`, r.vip ? 'ok' : 'warn');
    } catch (e) {
      if (lbl) lbl.textContent = '校验失败：' + e.message;
    }
  };
  if ($('sUiMode')) $('sUiMode').onchange = () => applyBaseTheme($('sUiMode').value);
  document.querySelectorAll('.base-theme-card').forEach(btn => {
    btn.onclick = () => applyBaseTheme(btn.dataset.mode || 'light');
  });
  if ($('sAccent')) $('sAccent').onchange = () => applyAccentTheme($('sAccent').value);
  document.querySelectorAll('.theme-swatch').forEach(btn => {
    btn.onclick = () => applyAccentTheme(btn.dataset.theme || 'blue');
  });
  if ($('btnRefreshPlatforms')) $('btnRefreshPlatforms').onclick = () => loadPlatformStatuses(true);
  if ($('wizardNext')) $('wizardNext').onclick = wizardNext;
  if ($('wizardBack')) $('wizardBack').onclick = () => showWizardStep(wizardStep - 1);
  if ($('wizardFinish')) $('wizardFinish').onclick = finishWizard;
  if ($('wizardClose')) $('wizardClose').onclick = () => { $('firstUseWizard').hidden = true; };

  $('btnClearTasks').onclick = async () => {
    try { const r = await api('/api/tasks/clear-finished', { method: 'POST' }); toast(`已清除 ${r.removed} 条`, 'ok'); pollTasks(); }
    catch (e) { toast('清除失败：' + e.message, 'err'); }
  };
  $('btnToggleDock').onclick = (e) => {
    e.stopPropagation();
    dockCollapsed = !dockCollapsed;
    $('taskDock').classList.toggle('collapsed', dockCollapsed);
    document.body.classList.toggle('dock-collapsed', dockCollapsed);
    $('btnToggleDock').textContent = dockCollapsed ? '展开' : '收起';
  };
  $('taskDockHead').onclick = (e) => {
    if (e.target.tagName === 'BUTTON') return;
    $('btnToggleDock').click();
  };
  startConcPolling();

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
    // 搜图面板的实际 id 是 panel-image（旧代码写的 panel-search 在 HTML 里不存在，
    // 一按 Ctrl+A 就 TypeError，布尔短路根本走不到 selectAllSearch）
    const searchPanel = $('panel-image');
    if ((e.ctrlKey || e.metaKey) && e.key === 'a' && searchPanel && searchPanel.classList.contains('active')
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

  // ---------- 桌面壳自检握手（不要删） ----------
  // app/desktop_shell.py 会轮询 window.__MD_APP_READY__：
  //   置位 → 认为「内核是 Chromium 且前端脚本确实跑起来了」；
  //   一直不置位 → 判定界面不可用，关掉桌面窗口改用默认浏览器。
  // 放在所有 await 之前：同步的事件绑定做完，界面就已经可以点了。
  // 这样 IE11（整份脚本在解析阶段就失败）永远不会置位 → 一定触发兜底。
  window.__MD_APP_READY__ = true;
  try {
    const warn = document.getElementById('kernelWarn');
    if (warn) warn.remove();
  } catch (e) { /* 提示块不存在也无所谓 */ }

  await loadStatus();
  await loadSettings();
  await loadPlatformStatuses();
  await loadOnboarding();
  setInterval(loadPlatformStatuses, 20000);
  startTaskPolling();
  msg('searchMsg', '输入关键词开始搜索。默认仅启用 Bing；清晰度筛选会探测原图真实分辨率，请按需开启。', 'info');
})();
