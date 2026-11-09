(function () {
'use strict';
const $ = s => document.querySelector(s);
const $$ = s => Array.prototype.slice.call(document.querySelectorAll(s));
const live = () => { const e = $('#shellLive'); return e || { textContent: '' }; };
const say = t => { live().textContent = t; };

document.body.classList.add('has-sidebar');

/* ---------- 面板高度：让「抓取图片 / 抓取视频」吃满一屏 ----------
   以前面板高度由内容决定，没结果时内容全挤在顶部、下面大片空白。
   这里把「视口 − 顶栏 − main 内边距」算成 --panel-h 交给 CSS，
   面板与结果画布按这个高度撑开，空态才能落在正中。 */
function syncPanelHeight() {
  const main = document.querySelector('main');
  if (!main) return;
  const cs = getComputedStyle(main);
  const padY = (parseFloat(cs.paddingTop) || 0) + (parseFloat(cs.paddingBottom) || 0);
  const top = document.querySelector('.topbar');
  // 顶栏是 sticky（仍在文档流里），用实测高度；滚动时不会变
  const topH = top ? top.getBoundingClientRect().height : 0;
  const h = Math.max(360, Math.round(window.innerHeight - topH - padY - 4));
  document.documentElement.style.setProperty('--panel-h', h + 'px');
}
syncPanelHeight();
window.addEventListener('resize', syncPanelHeight);
window.addEventListener('orientationchange', syncPanelHeight);
if (document.fonts && document.fonts.ready) document.fonts.ready.then(syncPanelHeight);

/* ---------- 侧栏：调用现有 switchTab，不覆盖它 ---------- */
$$('.sidebar .nav-item').forEach(btn => {
  btn.addEventListener('click', () => {
    if (typeof switchTab === 'function') switchTab(btn.dataset.tab);
    say('已切换到' + btn.textContent.trim());
  });
});
function syncSidebar() {
  const active = $('.tab.active') && $('.tab.active').dataset.tab;
  $$('.sidebar .nav-item').forEach(b => b.classList.toggle('on', b.dataset.tab === active));
}
const tabsEl = $('#tabs');
if (tabsEl && window.MutationObserver) {
  new MutationObserver(syncSidebar).observe(tabsEl, { subtree: true, attributes: true, attributeFilter: ['class'] });
}
syncSidebar();
$('#sideTasksBtn').addEventListener('click', () => { const b = $('#btnToggleDock'); if (b) b.click(); });

/* ---------- 统计条：只读当前作用域的真实 DOM，且结果/勾选变化立即刷新 ----------
   旧实现两处硬伤（用户 2026-11-07 反馈「4 列没一个能用」）：
   1) #imgCrawlStat 从来没有任何写入，所以「本次结果」永远是「—」；
   2) (sel.textContent) || (crawSel.textContent) 里关键词页的 #selCount
      恒为 '0' 且 truthy，把真实选中数 #imgCrawlSelCount 永远挡在后面。 */
function visibleEl(ids) {
  for (const id of ids) {
    const e = document.getElementById(id);
    if (!e || e.closest('[hidden]')) continue;
    const r = e.getBoundingClientRect();
    if (r.width || r.height) return e;
  }
  return null;
}
function activeGrid() {
  return visibleEl(['imgCrawlGrid', 'searchGrid', 'vidCrawlGrid', 'videoGrid']) ||
         $$('.grid').find(g => !g.closest('[hidden]') && g.querySelector('.card'));
}
function syncStats() {
  const grid = activeGrid();
  const cards = grid ? grid.querySelectorAll('.card').length : 0;
  // 摘要里有准确总数（>400 条时网格只渲染前 400），没有就数真实卡片
  const statEl = visibleEl(['imgCrawlStat', 'searchStat',
                            'vidCrawlStat', 'videoSearchStat']);
  const mt = ((statEl && statEl.textContent) || '').match(/(\d[\d,]*)/);
  const n = mt ? mt[1] : String(cards || 0);
  $('#statResult').textContent = (n && n !== '0') ? n : '—';
  const selEl = visibleEl(['imgCrawlSelCount', 'selCount',
                           'vidCrawlSelCount', 'videoKwSelCount']);
  $('#statSel').textContent = selEl ? (selEl.textContent.trim() || '0') : '0';
  const tc = $('#taskCount');
  $('#statTasks').textContent = tc ? tc.textContent : '0';
  $('#sideTaskCount').textContent = tc ? tc.textContent : '0';
  const lib = $$('#libGrid .card, #libGrid .vitem').length;
  $('#statLib').textContent = lib ? String(lib) : '—';
  $('#sideLibCount').textContent = String(lib);
  const disk = $('#envDisk');
  if (disk) $('#sideDisk').textContent = disk.textContent.trim();
  const cs = $('#fEngines');
  if (cs) {
    const opt = cs.options[cs.selectedIndex];
    $('#engineHint').textContent = opt ? '来源：' + opt.textContent.trim() : '';
  }
  // 进度环与速度只读 pollTasks 生成的真实 DOM，不自己造数
  const running = $$('#taskList .task').filter(t => t.querySelector('.task-status.running'));
  let pct = 0;
  if (running.length) {
    const vals = running.map(t => {
      const bar = t.querySelector('.task-bar i');
      return bar ? parseFloat(bar.style.width) || 0 : 0;
    });
    pct = Math.round(vals.reduce((a, b) => a + b, 0) / vals.length);
  }
  const ring = $('#sideRing'), ringNum = $('#sideRingNum');
  if (ring) ring.style.setProperty('--p', String(pct));
  if (ringNum) ringNum.textContent = pct + '%';
  let speed = '';
  for (const t of running) {
    const m = (t.querySelector('.task-sub') || {}).textContent || '';
    const hit = m.match(/([\d.]+\s*[KMGT]?B\/s)/i);
    if (hit) { speed = hit[1]; break; }
  }
  const sp = $('#sideSpeed');
  if (sp) sp.textContent = speed ? speed + ' · ' + running.length + ' 个进行中'
    : (running.length ? running.length + ' 个进行中' : '暂无进行中任务');
}
/* 4 张卡都可点：结果 / 已选 / 任务 / 资源库各自跳到对应位置 */
const STAT_ACTIONS = [
  () => { const g = activeGrid(); if (g) g.scrollIntoView({ behavior: 'smooth', block: 'start' }); },
  () => { const g = activeGrid(); if (g) g.scrollIntoView({ behavior: 'smooth', block: 'start' }); },
  () => { const b = $('#btnToggleDock'); if (b) b.click(); },
  () => { if (typeof switchTab === 'function') switchTab('library'); },
];
$$('.hstat').forEach((el, i) => {
  el.classList.add('hstat-clickable');
  el.title = ['查看本次抓取结果', '查看已选中的资源', '展开下载任务', '打开资源库'][i] || '';
  el.addEventListener('click', STAT_ACTIONS[i] || (() => {}));
});

/* 结果 / 勾选 / 任务 / 资源库一变就刷新，不用等 1.5 秒轮询 */
if (window.MutationObserver) {
  let statPending = false;
  const statMo = new MutationObserver(() => {
    if (statPending) return;
    statPending = true;
    requestAnimationFrame(() => {
      statPending = false;
      syncStats();
      // 只有「真的出现结果卡片」才结束抓取中态。搜索一开始会清空网格，
      // 那一帧就恢复的话按钮会在 0.1 秒内闪回原标题，用户还是看不到反馈。
      const g = activeGrid();
      if (g && g.querySelector('.card') && typeof setOmniBusy === 'function') {
        setOmniBusy(false);
      }
    });
  });
  ['imgCrawlGrid', 'searchGrid', 'vidCrawlGrid', 'taskList', 'libGrid',
   'imgCrawlToolbar', 'searchToolbar'].forEach(id => {
    const e = document.getElementById(id);
    if (e) statMo.observe(e, { childList: true, subtree: true, characterData: true });
  });
}
syncStats();
setInterval(syncStats, 1500);

/* ---------- 页面感知的筛选栏 ----------
   顶部「筛选」按钮 / chips / 抽屉属于全局壳。以前它只认图片面板，
   却把 change 监听绑到了所有 .filter-grid select（含视频面板），
   结果「抓取图片」页会出现视频那套筛选项（2026-11-07 用户反馈）。

   ⚠️ 图片页无论「关键词搜图」还是「粘贴链接抓取」，筛选抽屉都必须
   搬 #filterBox（安全搜索 / 数量上限 / 长宽比 / 清晰度 / 尺寸 / 颜色 /
   时间 / 格式 …）。★抓取选项（页数 / 下钻 / 渲染）不是筛选★ ——
   它们长在页面上自己的 <details> 里，和视频页那套抓取选项同形，
   一度被误搬进抽屉，用户看到的正是「图片筛选被视频抓取选项覆盖」。 */
const IMG_FILTER = {
  box: 'filterBox', reset: 'btnResetFilters',
  label: '筛选设置', title: '图片筛选设置',
  sub: '清晰度 / 尺寸 / 长宽比 / 颜色 / 时间 / 格式 / 安全搜索 / 数量上限',
};
const FILTER_SCOPES = {
  'image:kw':   Object.assign({}, IMG_FILTER),
  'image:link': Object.assign({}, IMG_FILTER),
  'video:kw':   { box: 'videoSearchFilterBox', reset: 'videoFilterReset',
                  label: '视频筛选', title: '视频结果筛选',
                  sub: '关键词搜视频 · 时长 / 来源 / 标题 / 排序' },
  // 抽屉只装「筛选」，抓取选项（vidCrawlOptions）永远留在页面上
  'video:link': { box: 'videoSearchFilterBox', reset: 'videoFilterReset',
                  label: '视频筛选', title: '视频结果筛选',
                  sub: '时长 / 来源 / 标题 / 排序' },
};
function scope() {
  const activeTab = document.querySelector('.tab.active');
  const tab = (activeTab && activeTab.dataset.tab) || 'image';
  // 首页 / 已下载 / 平台登录 / 设置 / 说明：没有抓取作用域，
  // 返回一份空壳，调用方按 tab 判断要不要用。
  if (tab !== 'image' && tab !== 'video') {
    return { tab: tab, mode: '', key: tab,
             label: '筛选设置', title: '筛选设置', sub: '' };
  }
  const kwPanel = document.getElementById(tab === 'video' ? 'videoKwMode' : 'imageKwMode');
  const mode = kwPanel && !kwPanel.hidden ? 'kw' : 'link';
  return Object.assign({ tab, mode, key: tab + ':' + mode },
                       FILTER_SCOPES[tab + ':' + mode]);
}

const IMG_CHIP_FIELDS = [
  ['fResPreset', '≥'], ['fResMax', '≤'], ['fSize', ''], ['fAspect', ''],
  ['fType', ''], ['fTime', ''], ['fColor', ''], ['fFace', ''],
  ['fLicense', ''], ['fFormats', ''], ['fMinSize', ''],
  ['fSafe', ''],
];
const VID_CHIP_FIELDS = [
  ['videoFilterDur', ''], ['videoFilterSite', '来源：'],
  ['videoFilterSort', '排序：'], ['videoFilterText', '标题：'],
];

function chipText(el, prefix) {
  if (!el) return '';
  const v = String(el.value || '').trim();
  if (!v || v === '0' || v === 'moderate' || v === 'default') return '';
  if (el.tagName === 'SELECT') {
    const opt = el.options[el.selectedIndex];
    if (!opt || !String(opt.value || '').trim()) return '';
    return prefix + opt.textContent.trim();
  }
  return prefix + v;
}

function chipHtml(text, title, toggle) {
  return '<button type="button" class="chip' + (toggle ? ' chip-toggle' : '') + '"'
       + (toggle ? ' data-toggle="' + toggle + '"' : ' data-open-filter="1"')
       + ' title="' + esc(title) + '"><span class="chip-dot">'
       + (toggle ? '✓' : '•') + '</span>' + esc(text) + '</button>';
}
function renderChips() {
  const box = $('#activeChips');
  if (!box) return;
  const s = scope();
  const html = [];
  (s.tab === 'image' ? IMG_CHIP_FIELDS : VID_CHIP_FIELDS).forEach(([id, prefix]) => {
    const t = chipText(document.getElementById(id), prefix);
    if (t) html.push(chipHtml(t, '点击打开筛选设置修改这一项', ''));
  });
  if (s.tab === 'image') {
    // 40 张是默认值，不该占一个 chip（旧逻辑也是变化后才显示）
    const limit = $('#fLimit');
    if (limit && limit.value && limit.value !== '40')
      html.push(chipHtml(limit.value + ' 张', '本次最多下载多少张图片', ''));
    // 「不重复下载」不再常驻成 chip（用户 2026-11-07 反馈）：
    // 它是默认开启的选项，出现在筛选条上等于把默认值当成「用户设过的东西」；
    // 而且 chip 是 toggle，点一下取消勾选就整个消失，看起来像被删掉了。
    // 开关本体留在筛选抽屉 / 设置里，需要时在那里改。
  }
  box.innerHTML = html.slice(0, 4).join('');
}
function esc(t) { const d = document.createElement('div'); d.textContent = t; return d.innerHTML; }

// chips 不是死文本：带 toggle 的当场开关，其余点击打开筛选设置
const chipsBox = $('#activeChips');
if (chipsBox) chipsBox.addEventListener('click', e => {
  const c = e.target.closest && e.target.closest('.chip');
  if (!c) return;
  const t = c.dataset.toggle;
  if (t) {
    const el = document.getElementById(t);
    if (!el) return;
    el.checked = !el.checked;
    el.dispatchEvent(new Event('change', { bubbles: true }));
    renderChips();
    say((el.checked ? '已开启：' : '已关闭：') + c.textContent.trim());
  } else if (typeof openDrawer === 'function') {
    openDrawer();
  }
});

function filterCount() {
  const el = document.getElementById(scope().box);
  if (!el) return 0;
  return el.querySelectorAll('select, input[type="checkbox"], input[type="number"], input[type="text"]').length;
}

function syncFilterBar() {
  const s = scope();
  const crawl = s.tab === 'image' || s.tab === 'video';
  /* 「筛选设置」只在真正会用到它的地方出现（用户 2026-11-07 反馈）：
     1) 首页 / 已下载 / 设置等非抓取页 —— 没有结果可筛，按钮是噪音；
     2) 图片页的「粘贴链接抓取」—— 抓的是网页里现成的图，
        安全搜索 / 长宽比 / 颜色 / 许可证这些搜索引擎参数对它一项都不生效，
        留在那儿只会让人以为设置没起作用。 */
  const btn = $('#btnFilterDrawer');
  if (btn) {
    const hide = !crawl || s.key === 'image:link';
    btn.hidden = hide;
    btn.setAttribute('aria-hidden', hide ? 'true' : 'false');
  }
  if (!crawl) {
    const box0 = $('#activeChips');
    if (box0) box0.innerHTML = '';
    const dr = $('#filterDrawer');
    if (dr && dr.classList.contains('on')) closeDrawer(false);
    return;
  }
  const label = $('#filterLabel'), count = $('#filterCount');
  if (label) label.textContent = s.label;
  if (count) count.textContent = String(filterCount());
  const dt = $('#drawerTitle'), ds = $('#drawerSub');
  if (dt) dt.textContent = s.title;
  if (ds) ds.textContent = s.sub + ' · 原控件直接搬入，设置与结果完全同步';
  renderChips();
  // 作用域变化时抽屉里还是旧面板，先收起来，避免标题与内容对不上
  const drawer = $('#filterDrawer');
  if (drawer && drawer.classList.contains('on')) closeDrawer(false);
}
// 只响应当前作用域里的控件：视频面板的 select 不再串到图片 chips
$$('.filter-grid select, .filter-grid input').forEach(el => {
  el.addEventListener('change', () => {
    const box = document.getElementById(scope().box);
    if (box && (el === box || box.contains(el))) renderChips();
  });
});
$$('#omniSeg button').forEach(b =>
  b.addEventListener('click', () => setTimeout(syncFilterBar, 0)));
if (window.MutationObserver) {
  const scopeMo = new MutationObserver(() => syncFilterBar());
  const tabsEl2 = $('#tabs');
  if (tabsEl2) scopeMo.observe(tabsEl2, { subtree: true, attributes: true,
                                          attributeFilter: ['class'] });
  ['imageKwMode', 'imgLinkMode', 'videoKwMode', 'vidLinkMode'].forEach(id => {
    const el = document.getElementById(id);
    if (el) scopeMo.observe(el, { attributes: true, attributeFilter: ['hidden'] });
  });
}
syncFilterBar();
$('#drawerReset').addEventListener('click', () => {
  const s = scope();
  const b = s.reset ? document.getElementById(s.reset) : null;
  if (b) b.click();
  renderChips(); say(s.title + '已重置');
});

/* ---------- 抓取通道：图片 / 视频 ----------
   用户 2026-11-07 问「首页默认是图片通道吗？我贴视频链接怎么办」——
   之前只判断「是不是链接」，链接一律丢给 #imgBtnCrawl，
   快手 / B 站 / 抖音这些主页链接会被当图片页去抓，结果一条都抓不到。

   现在的规则：
   1. 默认图片通道（多数人进来是找图）；
   2. 粘贴视频站链接 → 自动切到视频通道，并同步意图标签；
   3. 用户手动点过通道 → 本次会话不再被自动识别覆盖；
   4. 视频站里再分两种：单个视频页 → 解析清晰度挑画质；
      主页 / 列表页 / 频道页 → 批量抓出整批。 */
const CHAN_KEY = 'md_chan';
const VIDEO_HOSTS = [
  'kuaishou.com', 'douyin.com', 'iesdouyin.com', 'bilibili.com', 'b23.tv',
  'youtube.com', 'youtu.be', 'xiaohongshu.com', 'xhslink.com',
  'weibo.com', 'weibo.cn', 'tiktok.com', 'vimeo.com', 'dailymotion.com',
  'iqiyi.com', 'youku.com', 'v.qq.com', 'tv.sohu.com',
  'twitter.com', 'x.com', 'instagram.com', 'facebook.com', 'fb.watch',
  'pornhub.com', 'eporner.com',
];
// 单集视频页的地址特征（命中就是「一个视频」，否则按批量列表处理）
const SINGLE_VIDEO_PAT = [
  /youtu\.be\/[\w-]{6,}/i,
  /[?&]v=[\w-]{6,}/i,
  /\/video\/(?:BV[0-9A-Za-z]{8,}|av\d+|\d{6,})/i,
  /\/short-video\//i,
  /\/shorts\//i,
  /\/watch\b/i,
  /\/reel\//i,
  /\/status\/\d+/i,
  /\/explore\/[0-9a-z]{20,}/i,
  /\/discovery\/item\/[0-9a-z]{20,}/i,
  /\/sp\/\d+/i,
  /\/v\/\d+/i,
];

function hostOf(v) {
  const m = String(v).trim().match(/^(?:https?:\/\/)?([^/?#]+)/i);
  return m ? m[1].toLowerCase().replace(/^www\./, '').replace(/:\d+$/, '') : '';
}
function isVideoHost(host) {
  return !!host && VIDEO_HOSTS.some(d => host === d || host.endsWith('.' + d));
}
function isSingleVideo(v) { return SINGLE_VIDEO_PAT.some(re => re.test(v)); }

let chan = 'image';
// 手动选择只锁定「当时那个输入」：用户点了「视频」之后，如果他换了一条新链接，
// 说明这是新任务，应该重新按站点判断。否则点过一次「图片」，
// 后面所有快手 / B 站链接都会被当成图片页去抓（2026-11-07 实测踩到）。
let chanManualFor = '';
// 当前通道是不是「按链接自动切过来的」。粘完 B 站链接再改搜关键词时，
// 这个自动切出来的视频通道就不成立了，要回到默认图片通道；
// 用户自己点过的通道不受影响。
let chanAuto = true;
try {
  const saved = localStorage.getItem(CHAN_KEY);
  if (saved === 'image' || saved === 'video') chan = saved;
} catch (e) { /* 隐私模式禁 localStorage 时按默认走 */ }
const currentChan = () => chan;

function paintChan() {
  $$('#homeChan button').forEach(b => {
    const on = b.dataset.chan === chan;
    b.classList.toggle('on', on);
    b.setAttribute('aria-pressed', on ? 'true' : 'false');
  });
}
function setChan(next, manual, forValue) {
  if (next !== 'image' && next !== 'video') return;
  chan = next;
  chanAuto = !manual;
  if (manual) {
    const v = forValue != null ? forValue : (($('#homeInput') || {}).value || '');
    chanManualFor = String(v).trim();
  }
  try { localStorage.setItem(CHAN_KEY, chan); } catch (e) {}
  paintChan();
  // 占位文案跟着通道走：视频通道下还写着「粘贴链接」会让人以为只能抓图
  const inp = $('#homeInput');
  if (inp) {
    inp.placeholder = chan === 'video'
      ? '粘贴视频 / 博主主页链接，或输入关键词，如：雪山 航拍 4K'
      : '粘贴链接，或输入关键词，如：雪山 星空 4K';
  }
}
// 当前输入是否被用户手动指定过通道。两边都补协议再比：用户在首页粘裸域名
// 手选通道后，runOmni 会把它规范化成 https://...，直接比字符串会把手选作废。
const chanManualOn = v => chanManualFor !== '' &&
  normalizeUrlInput(chanManualFor) === normalizeUrlInput(String(v).trim());
// 链接按站点自动判通道；关键词不改变用户已选的通道
function guessChan(v) {
  if (!isLink(v)) return chan;
  return isVideoHost(hostOf(v)) ? 'video' : 'image';
}

/* 真正开工：把同一个输入按通道分派到对应页面与按钮。
   图片和视频的链路完全不同（图片走抓取器，视频走解析器 / 批量抓取器），
   但用户只需要在一个框里输入一次。 */
function startFrom(v, c, modeHint) {
  v = normalizeUrlInput(v);
  const link = isLink(v);
  if (c === 'video') {
    if (typeof switchTab === 'function') switchTab('video');
    const linkBtn = document.querySelector('#videoModeSwitch .mode-btn[data-mode="link"]');
    const kwBtn = document.querySelector('#videoModeSwitch .mode-btn[data-mode="kw"]');
    if (link) {
      if (isSingleVideo(v) && typeof gotoVideoProbe === 'function') {
        gotoVideoProbe(v);
        say('已定位到视频解析，确认清晰度后下载');
        return 'probe';
      }
      if (linkBtn && !linkBtn.classList.contains('active')) linkBtn.click();
      const inp = $('#vidCrawlInput');
      if (inp) inp.value = v;
      const btn = $('#vidBtnCrawl');
      if (btn) { btn.click(); say('已开始抓取该页视频'); }
      return 'crawl';
    }
    if (kwBtn && !kwBtn.classList.contains('active')) kwBtn.click();
    const kw = $('#videoKw');
    if (kw) kw.value = v;
    const sb = $('#btnVideoSearch');
    if (sb) { sb.click(); say('已开始按关键词搜视频'); }
    return 'search';
  }
  if (typeof switchTab === 'function') switchTab('image');
  // modeHint 是用户在这一页亲手选的方式（#omniSeg 的 .on）。以前这里无条件
  // setMode(link ? 'link' : 'kw')，粘裸域名时 link=false，手选的「粘贴链接抓取」
  // 会被静默改回「关键词搜图」并跑去 Bing 搜图。用户选了什么就按什么走。
  const mode = modeHint || (link ? 'link' : 'kw');
  setMode(mode);
  if (mode === 'link') {
    const inp = $('#imgCrawlInput');
    if (inp) inp.value = v;
    const btn = $('#imgBtnCrawl');
    if (btn) { btn.click(); say('已开始抓取链接图片'); }
    return 'crawl';
  }
  const kw = $('#kwInput');
  if (kw) kw.value = v;
  const btn = $('#btnSearch');
  if (btn) { btn.click(); say('已开始按关键词搜图'); }
  return 'search';
}

/* ---------- 意图识别 + 输入代理 ---------- */
const omni = $('#omniInput'), intent = $('#omniIntent');

/* 链接判据：除了 http(s):// / www.，还要认「裸域名」。
   2026-11-08 Sam 实测：粘 weibo.com/u/1844960024?tabtype=newVideo（分享串不带协议头），
   原判据 /^(https?:\/\/|www\.)/ 判 false → 整条被当成关键词送进 Bing 搜图，
   startFrom 还用 setMode(link ? 'link' : 'kw') 把用户手选的「粘贴链接抓取」
   强行改回「关键词搜图」。带 https:// 的同站点链接完全正常，所以看起来像
   「只有微博会这样」，实际是输入形态问题，抖音/xhs 的裸域名一样会中招。
   纯主机名时只认常见后缀，避免「雪山.航拍」「cat.dog」这类关键词被误判成网址。 */
const COMMON_TLD = new Set((
  'com cn net org io co me tv cc xyz top vip app dev ai hk tw jp kr uk de fr ru '
  + 'info biz site online club shop link live video photo mobi edu gov wang ren '
  + 'ltd inc icu sbs fun blog ink studio global pro name asia cloud tech store '
  + 'space design media group center'
).split(' '));
const BARE_URL_RE = /^((?:[a-z0-9-]+\.)+[a-z]{2,24})(?::\d{2,5})?(\/[^\s]*|[?#][^\s]*)?$/i;
function isLink(v) {
  const s = String(v || '').trim();
  if (/^https?:\/\//i.test(s) || /^www\./i.test(s)) return true;
  const m = BARE_URL_RE.exec(s);
  if (!m) return false;
  if (m[2]) return true;                 // 带路径 / 查询 / 片段，不可能是搜索词
  return COMMON_TLD.has(m[1].split('.').pop().toLowerCase());
}
/** 后端 /api/crawl/start 和 doCrawl 都要求 http(s):// 开头，裸域名在这里补齐。 */
function normalizeUrlInput(v) {
  const s = String(v || '').trim();
  if (!s || /^https?:\/\//i.test(s) || !isLink(s)) return s;
  return 'https://' + s;
}
// 当前激活页签：home / image / video / ...
// 通道自动识别（按站点猜图片还是视频）只在首页成立；进了图片页或视频页，
// 页签本身就是用户的通道选择，不能再按域名把输入甩到另一页。
function activeTabKey() {
  const t = document.querySelector('.tab.active');
  return (t && t.dataset && t.dataset.tab) || 'home';
}
function channelForTab() {
  const tab = activeTabKey();
  return tab === 'image' ? 'image' : (tab === 'video' ? 'video' : chan);
}
function syncInput() {
  const v = omni.value;
  const kw = $('#kwInput'), link = $('#imgCrawlInput');
  if (kw) kw.value = v;
  if (link) link.value = v;
}
function updateIntent() {
  const v = omni.value.trim();
  // 空输入时把标签整个藏掉：一个恒亮的「等待输入」既不提供信息，
  // 还占着搜索框里最贵的一段宽度
  if (!v) {
    intent.hidden = true;
    intent.textContent = '等待输入';
    intent.className = 'intent';
  } else if (isLink(v)) {
    // 标签说的是「这一条会被怎么处理」，所以必须跟**当前页**一致：
    // 2026-11-07 用户实测——在「抓取图片」的框里贴小红书博主主页，
    // 标签写「视频 · xiaohongshu.com」、点下去还整条跳到视频抓取，
    // 与「我在图片框里搜这个博主的图」完全相反。
    const c = channelForTab();
    intent.hidden = false;
    intent.textContent = (c === 'video' ? '视频 · ' : '图片 · ') + (hostOf(v) || 'URL');
    intent.className = 'intent link' + (c === 'video' ? ' video' : '');
  } else {
    intent.hidden = false;
    intent.textContent = channelForTab() === 'video' ? '关键词 · 搜视频' : '关键词 · 搜图';
    intent.className = 'intent';
  }
  syncInput();
}
omni.addEventListener('input', updateIntent);
omni.addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); runOmni(); }
});

/* 图片页的模式切换：以前去点 #imageModeSwitch 的按钮，那条重复的模式条
   已经删掉（上面 omnibox 的 #omniSeg 才是唯一入口），改为直接切面板。 */
// 「用户亲手点过模式按钮」才把手选锁住。没点过时（含首页 / 历史记录等入口）
// 仍按 isLink 自动切：粘链接自动进「粘贴链接抓取」是 F-03 的既有行为，
// 不能被 F-19 带坏（v304 的「贴视频链接仍走图片抓取」就是这条闸门）。
let imageModeManual = false;
function setMode(mode, manual) {
  $$('#omniSeg button').forEach(b => b.classList.toggle('on', b.dataset.mode === mode));
  if (manual) imageModeManual = true;
  if (typeof applyImageMode === 'function') applyImageMode(mode);
}
/** 图片页当前被选中的方式，交给 startFrom 当 modeHint。 */
function imageModeSelected() {
  const b = document.querySelector('#omniSeg button.on');
  return (b && b.dataset.mode) || 'kw';
}
$$('#omniSeg button').forEach(b => b.addEventListener(
  'click', () => setMode(b.dataset.mode, true)));

function runOmni() {
  const raw = omni.value.trim();
  if (!raw) {
    if (typeof toast === 'function') toast('请先输入关键词或粘贴链接', 'warn');
    omni.focus(); return;
  }
  const tab = activeTabKey();
  // 图片页：只有用户亲手点过 #omniSeg 才把手选方式锁死；没点过时保持
  // 「粘链接自动进链接抓取」的原行为，避免把 F-03 的自动分派改坏。
  const modeHint = (tab === 'image' && imageModeManual) ? imageModeSelected() : null;
  const v = normalizeUrlInput(raw);
  if (v !== raw) omni.value = v;      // 裸域名补协议，界面与真正提交的值保持一致
  syncInput();
  const link = isLink(v);
  if (modeHint === 'link' && !link) {
    // 手选「粘贴链接抓取」却只给了词：明说，不偷偷切回搜图模式。
    if (typeof toast === 'function')
      toast('「粘贴链接抓取」需要一条网址；要按词搜图请切回「关键词搜图」', 'warn');
    if (typeof setOmniBusy === 'function') setOmniBusy(false);
    omni.focus(); return;
  }
  rememberSearch(v, link);
  // 通道：只有**首页**需要按链接自动猜。
  //
  // 原实现是「贴视频站链接就整条切到视频页执行」，本意是不让 B 站/快手主页
  // 被图片抓取器空跑。但 2026-11-07 用户实测出反效果：在「抓取图片」页的
  // 框里贴小红书博主主页，被认成视频、整条输入跳到视频抓取 ——
  // 用户在图片框里搜的显然是想抓这页的图。
  //
  // 现在的规则：进了图片页/视频页，页签就是通道；首页才按站点判断。
  if (tab === 'home' && !chanManualOn(v) && guessChan(v) !== chan) {
    setChan(guessChan(v), false);
  }
  startFrom(v, channelForTab(), modeHint);
}
$('#omniGo').addEventListener('click', () => {
  // 先置抓取中态再执行搜索：runOmni 会同步触发 doSearch 清空网格，
  // 反过来的话 observer 会先跑一遍把刚设上的状态抹掉。
  if (omni.value.trim()) setOmniBusy(true);
  runOmni();
});

/* ---------- 按钮反馈 ----------
   以前所有按钮只有 hover，点下去没有任何即时变化，用户以为没点上。
   1) 所有 button 按下时给 200ms 的 .is-pressed；
   2) 「开始抓取」进入抓取中态，结果网格一变（或 15s 兜底）自动恢复。 */
document.addEventListener('pointerdown', e => {
  const b = e.target.closest && e.target.closest('button');
  if (!b || b.disabled) return;
  b.classList.add('is-pressed');
  setTimeout(() => b.classList.remove('is-pressed'), 200);
});
let omniBusyTimer = null;
// 首页和图片页各有一个「开始抓取」，两处都要有即时反馈
const BUSY_BTNS = ['#omniGo', '#homeGo'];
function setOmniBusy(on) {
  let started = false;
  BUSY_BTNS.forEach(sel => {
    const btn = $(sel);
    if (!btn) return;
    if (on) {
      if (btn.dataset.busy === '1') return;
      btn.dataset.busy = '1';
      btn.dataset.prevHtml = btn.innerHTML;
      btn.classList.add('is-loading');
      btn.innerHTML = '<span class="spin"></span>抓取中…';
      started = true;
    } else {
      if (btn.dataset.busy !== '1') return;
      delete btn.dataset.busy;
      btn.classList.remove('is-loading');
      btn.innerHTML = btn.dataset.prevHtml || '';
    }
  });
  clearTimeout(omniBusyTimer);
  if (on && started) omniBusyTimer = setTimeout(() => setOmniBusy(false), 15000);
}
$('#omniBox').addEventListener('click', e => {
  if (e.target === e.currentTarget) omni.focus();
});

/* ---------- 筛选抽屉：把现有 details 搬入再搬回（同一节点，事件不丢） ---------- */
let drawerBox = null, drawerAnchor = null, drawerParent = null;
let drawerWasHidden = false;
function currentFilterBox() {
  return document.getElementById(scope().box);
}
function openDrawer() {
  // 筛选属于两个抓取页；从「已下载 / 设置」等页面唤起时先切回图片页
  const active = document.querySelector('.tab.active');
  if (active && active.dataset.tab !== 'image' && active.dataset.tab !== 'video') {
    if (typeof switchTab === 'function') switchTab('image');
    if (typeof toast === 'function') toast('筛选只用于抓取页，已为你切过去', 'warn');
  }
  /* 粘贴链接抓取没有「筛选」这件事：安全搜索 / 长宽比 / 颜色都是搜索引擎参数。
     按钮已隐藏，但快捷键 F 和脚本点击仍会走到这里，所以再守一道。 */
  if (scope().key === 'image:link') {
    if (typeof toast === 'function') toast('粘贴链接抓取不用筛选设置；切到「关键词搜图」才有', 'warn');
    return;
  }
  syncFilterBar();
  const s = scope();
  const box = currentFilterBox();
  if (!box) return;
  if (drawerBox && drawerBox !== box) restoreDrawer();
  drawerBox = box;
  drawerWasHidden = box.hidden;
  drawerAnchor = document.createComment('filter-anchor');
  drawerParent = box.parentNode;
  drawerParent.insertBefore(drawerAnchor, box);
  $('#drawerBody').appendChild(box);
  // 视频结果筛选平时带 hidden，搬进抽屉后要临时摘掉，否则一直空白
  box.hidden = false;
  box.style.display = 'block';
  box.open = true;
  $('#drawerOverlay').classList.add('on');
  $('#filterDrawer').classList.add('on');
  $('#btnFilterDrawer').classList.add('on');
  $('#drawerClose').focus();
  say(s.title + '已打开，共 ' + filterCount() + ' 项参数');
}
function restoreDrawer() {
  if (drawerBox && drawerParent) {
    drawerParent.insertBefore(drawerBox, drawerAnchor);
    drawerBox.style.display = '';
    drawerBox.hidden = drawerWasHidden;
    if (drawerAnchor.parentNode) drawerAnchor.parentNode.removeChild(drawerAnchor);
  }
  drawerBox = null; drawerAnchor = null; drawerParent = null;
}
function closeDrawer(restoreFocus) {
  restoreDrawer();
  $('#drawerOverlay').classList.remove('on');
  $('#filterDrawer').classList.remove('on');
  $('#btnFilterDrawer').classList.remove('on');
  if (restoreFocus !== false) $('#btnFilterDrawer').focus();
}
$('#btnFilterDrawer').addEventListener('click', openDrawer);
$('#drawerClose').addEventListener('click', () => closeDrawer());
$('#drawerDone').addEventListener('click', () => closeDrawer());
$('#drawerOverlay').addEventListener('click', () => closeDrawer());

/* ---------- 密度：只改栅格列数，不动卡片结构 ---------- */
let dens = 0;
$('#btnDensity').addEventListener('click', () => {
  dens = (dens + 1) % 3;
  document.body.dataset.density = ['normal', 'wide', 'compact'][dens];
  $('#densLabel').textContent = ['标准', '宽松', '紧凑'][dens];
  say('结果密度：' + ['标准', '宽松', '紧凑'][dens]);
});

/* ---------- 命令面板：全部转发到现有函数 / 按钮 ---------- */
const SEARCH_KEY = 'md_shell_search_history';
let recentDownloads = [];

function loadRecentHistory() {
  if (typeof fetch !== 'function') return;
  fetch('/api/history?limit=8')
    .then(r => r.ok ? r.json() : null)
    .then(d => {
      if (d && Array.isArray(d.recent)) {
        recentDownloads = d.recent.filter(x => x && (x.source_url || x.url));
      }
    })
    .catch(() => {});
}
loadRecentHistory();
setInterval(loadRecentHistory, 60000);

function getSearchHistory() {
  try { const a = JSON.parse(localStorage.getItem(SEARCH_KEY) || '[]'); return Array.isArray(a) ? a : []; }
  catch (e) { return []; }
}
function rememberSearch(v, link) {
  try {
    const a = getSearchHistory().filter(x => x && x.v !== v);
    a.unshift({ v: v, link: !!link, ts: Date.now() });
    localStorage.setItem(SEARCH_KEY, JSON.stringify(a.slice(0, 8)));
  } catch (e) {}
}

function buildCommands() {
  const base = [
    { t: '首页', k: '', g: '页面', f: () => switchTab('home') },
    { t: '抓取图片', k: '', g: '页面', f: () => switchTab('image') },
    { t: '抓取视频', k: '', g: '页面', f: () => switchTab('video') },
    { t: '已下载', k: '', g: '页面', f: () => switchTab('library') },
    { t: '平台登录', k: '', g: '页面', f: () => switchTab('login') },
    { t: '设置', k: '', g: '页面', f: () => switchTab('settings') },
    { t: '使用说明', k: '', g: '页面', f: () => switchTab('help') },
    { t: '打开筛选（15 项）', k: 'F', g: '操作', f: () => openDrawer() },
    { t: '全选结果', k: '', g: '操作', f: () => { const b = $('#btnSelAll'); if (b) b.click(); } },
    { t: '下载选中', k: '', g: '操作', f: () => { const b = $('#btnDownloadSel'); if (b) b.click(); } },
    { t: '重置筛选', k: '', g: '操作', f: () => { const b = $('#btnResetFilters'); if (b) b.click(); renderChips(); } },
    { t: '展开 / 收起下载任务', k: '', g: '操作', f: () => { const b = $('#btnToggleDock'); if (b) b.click(); } },
    { t: '清除已完成任务', k: '', g: '操作', f: () => { const b = $('#btnClearTasks'); if (b) b.click(); } },
    { t: '保存设置', k: '', g: '操作', f: () => { switchTab('settings'); const b = $('#btnSaveSettings'); if (b) b.scrollIntoView({ block: 'center' }); } },
  ];
  const hist = getSearchHistory().slice(0, 5).map(h => ({
    t: '搜索：' + h.v, k: '', g: '最近搜索',
    f: () => { switchTab('image'); $('#omniInput').value = h.v; updateIntent(); $('#omniGo').click(); }
  }));
  const down = recentDownloads.slice(0, 5).map(d => ({
    t: '下载：' + String(d.title || d.source_url || d.url).slice(0, 30), k: '', g: '最近下载',
    f: () => { switchTab('image'); $('#omniInput').value = d.source_url || d.url; updateIntent(); $('#omniInput').focus(); }
  }));
  // 最近搜索优先展示：命令面板默认视口有限，历史项必须在首屏可见，
  // 否则用户以为没有历史功能；固定命令与最近下载依次排后，可滚动/过滤到达。
  return hist.slice(0, 3).concat(base, down.slice(0, 4));
}
let CMDS = buildCommands();
let cIdx = 0;
function renderCmd(q) {
  const list = $('#cmdList');
  const items = CMDS.map((c, i) => ({ c: c, i: i }))
    .filter(x => x.c.t.toLowerCase().indexOf(q.toLowerCase()) >= 0);
  if (items.length && !items.some(x => x.i === cIdx)) cIdx = items[0].i;
  let html = '', lastG = '';
  items.forEach(x => {
    const g = x.c.g || '命令';
    if (g !== lastG) { html += '<div class="cmd-group">' + esc(g) + '</div>'; lastG = g; }
    html += '<div class="cmd-item' + (x.i === cIdx ? ' on' : '') + '" data-i="' + x.i + '">' +
      '<svg viewBox="0 0 24 24"><path d="M5 12h14m-6-6l6 6-6 6"/></svg>' + esc(x.c.t) +
      '<kbd>' + (x.c.k || '·') + '</kbd></div>';
  });
  list.innerHTML = html || '<div class="cmd-item">没有匹配的命令</div>';
  $$('#cmdList .cmd-item[data-i]').forEach(el => {
    el.addEventListener('mouseenter', () => {
      const j = +el.dataset.i;
      if (j !== cIdx) { cIdx = j; renderCmd($('#cmdInput').value); }
    });
    el.addEventListener('click', () => execCmd(+el.dataset.i));
  });
}
function openCmd() {
  closeDrawer(false);
  $('#cmdOverlay').classList.add('on');
  $('#cmdPal').classList.add('on');
  CMDS = buildCommands();
  $('#cmdInput').value = ''; cIdx = 0; renderCmd('');
  $('#cmdInput').focus(); say('命令面板已打开');
}
function closeCmd(restoreFocus) {
  $('#cmdOverlay').classList.remove('on');
  $('#cmdPal').classList.remove('on');
  if (restoreFocus !== false) $('#cmdTrigger').focus();
}
function execCmd(i) { closeCmd(false); setTimeout(() => CMDS[i].f(), 50); }
$('#cmdTrigger').addEventListener('click', openCmd);
$('#cmdInput').addEventListener('input', e => { cIdx = 0; renderCmd(e.target.value); });
$('#cmdInput').addEventListener('keydown', e => {
  if (e.key === 'ArrowDown') { e.preventDefault(); cIdx = (cIdx + 1) % CMDS.length; renderCmd(e.target.value); }
  else if (e.key === 'ArrowUp') { e.preventDefault(); cIdx = (cIdx - 1 + CMDS.length) % CMDS.length; renderCmd(e.target.value); }
  else if (e.key === 'Enter') {
    e.preventDefault();
    const on = $('#cmdList .cmd-item.on');
    const first = $('#cmdList .cmd-item[data-i]');
    execCmd(on ? +on.dataset.i : (first ? +first.dataset.i : 0));
  }
});
$('#cmdOverlay').addEventListener('click', () => closeCmd());

/* ---------- 全局键盘 ---------- */
document.addEventListener('keydown', e => {
  const typing = /INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName);
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); openCmd(); return; }
  if (e.key === 'Escape') { closeCmd(false); closeDrawer(false); return; }
  if (typing) return;
  if (e.key === '/' || e.key === 'k') { e.preventDefault(); openCmd(); }
  else if (e.key === 'f' || e.key === 'F') { e.preventDefault(); openDrawer(); }
  else if (e.key === '1') { document.body.dataset.density = 'normal'; $('#densLabel').textContent = '标准'; }
  else if (e.key === '2') { document.body.dataset.density = 'wide'; $('#densLabel').textContent = '宽松'; }
  else if (e.key === '3') { document.body.dataset.density = 'compact'; $('#densLabel').textContent = '紧凑'; }
});

/* ---------- 图片卡片 hover 操作层由 CSS 承担；这里只同步选中数 ---------- */

/* 初始空态：结果画布在还没有结果时也该有归属。
   以前网格是纯空的，flex 撑开后就是一片什么都没有的深色块，
   用户既不知道结果会长在哪，也看不出这块区域有用。 */
const PANE_SEEDS = [
  ['searchGrid', '输入关键词后，搜到的图片会排在这里',
    ['风景 壁纸', '城市夜景', '4K 壁纸']],
  ['imgCrawlGrid', '粘贴一个链接，抓到的图片会排在这里', null],
  ['videoSearchResult', '选好视频源、输入关键词后，结果会排在这里',
    ['风景 航拍 4K', '产品演示']],
  ['vidCrawlGrid', '粘贴视频页或博主主页，抓到的视频会排在这里', null],
];

/* 空态里的示例不是装饰：点一下直接按对应通道开跑，
   省掉「先切页、再选模式、再输一遍」这一串操作。 */
function runSample(id, value) {
  const c = (id === 'videoSearchResult' || id === 'vidCrawlGrid') ? 'video' : 'image';
  if (typeof startFrom === 'function') startFrom(value, c);
}

function seedEmptyHints() {
  PANE_SEEDS.forEach(([id, text, samples]) => {
    const el = document.getElementById(id);
    if (!el || el.children.length) return;
    const d = document.createElement('div');
    d.className = 'empty';
    d.dataset.seed = '1';
    d.textContent = text;
    if (samples && samples.length) {
      const row = document.createElement('div');
      row.className = 'empty-samples';
      samples.forEach(s => {
        const b = document.createElement('button');
        b.type = 'button';
        b.textContent = s;
        b.title = '点一下直接搜「' + s + '」';
        b.addEventListener('click', () => runSample(id, s));
        row.appendChild(b);
      });
      d.appendChild(row);
    }
    el.appendChild(d);
  });
}

function enhanceEmpty() {
  $$('.grid > .empty, .vlist > .empty').forEach(el => {
    if (el.dataset.enhanced) return;
    el.dataset.enhanced = '1';
    // 初始引导不是「筛完没结果」，不该挂「清空筛选条件」两个按钮
    if (el.dataset.seed === '1') return;
    const row = document.createElement('div');
    row.className = 'empty-actions';
    const b1 = document.createElement('button');
    b1.type = 'button'; b1.textContent = '清空筛选条件';
    b1.onclick = () => { const b = $('#btnResetFilters'); if (b) b.click(); renderChips(); };
    const b2 = document.createElement('button');
    b2.type = 'button'; b2.textContent = '查看使用说明';
    b2.onclick = () => switchTab('help');
    row.appendChild(b1); row.appendChild(b2); el.appendChild(row);
  });
}
function enhanceCards() {
  $$('.grid .card-links a').forEach(a => {
    if (a.dataset.enhanced) return;
    a.dataset.enhanced = '1';
    if (!a.title) a.title = a.textContent.trim();
  });
}
function enhanceAll() { syncStats(); seedEmptyHints(); enhanceEmpty(); enhanceCards(); }
if (window.MutationObserver) {
  const targets = ['#searchGrid', '#imgCrawlGrid', '#libGrid', '#vidCrawlGrid', '#videoSearchResult'];
  targets.forEach(sel => {
    const el = $(sel);
    if (el) new MutationObserver(() => enhanceAll()).observe(el, { childList: true });
  });
}
/* ---------- 首页：意图识别 / 一键进入 / 真实状态 ----------
   首页不是第二套功能，只是把工作台那条链路搬到首屏：
   输入框的值写回 omnibox，再走同一个 updateIntent + startFrom，
   意图判断、通道分派、抓取中态、结果轮询全都只有一份实现。 */
const HOME_LAST_KEY = 'md_last_tab';
const homeInput = $('#homeInput'), homeIntentEl = $('#homeIntent');

function syncHomeIntent() {
  if (!homeInput || !homeIntentEl) return;
  const v = homeInput.value.trim();
  // 空输入时不摆一个「等待输入」的静态标签：它不提供信息，只占位置
  if (!v) { homeIntentEl.hidden = true; homeIntentEl.textContent = ''; homeIntentEl.className = 'home-intent'; return; }
  homeIntentEl.hidden = false;
  if (isLink(v)) {
    // 自动切通道：贴的是快手 / B 站 / YouTube 的链接，就不该继续停在图片通道。
    // 用户为这一条输入手动选过通道时不覆盖，标签也按最终通道显示，
    // 否则会出现「按钮在视频、标签写链接」这种自相矛盾的界面。
    if (!chanManualOn(v) && guessChan(v) !== chan) setChan(guessChan(v), false);
    homeIntentEl.textContent = (chan === 'video' ? '视频 · ' : '链接 · ') + (hostOf(v) || 'URL');
    homeIntentEl.className = 'home-intent link' + (chan === 'video' ? ' video' : '');
  } else {
    // 关键词不带站点信息：刚才按链接自动切出来的视频通道到这里不成立了，
    // 回默认图片；手动点过通道的用户保持自己的选择
    if (chanAuto && chan === 'video') setChan('image', false);
    homeIntentEl.textContent = chan === 'video' ? '关键词 · 搜视频' : '关键词';
    homeIntentEl.className = 'home-intent';
  }
}

function goFromHome() {
  const v = normalizeUrlInput(homeInput ? homeInput.value.trim() : '');
  if (homeInput && v) homeInput.value = v;
  if (!v) {
    if (typeof toast === 'function') toast('先输入关键词，或粘贴一个链接', 'warn');
    if (homeInput) homeInput.focus();
    return;
  }
  const box = $('#omniInput');
  if (box) box.value = v;
  updateIntent();
  setOmniBusy(true);
  const c = chan;
  // 首页的输入可能就是视频，不能再无脑 switchTab('image')
  setTimeout(() => startFrom(v, c), 40);
}

if (homeInput) {
  homeInput.addEventListener('input', syncHomeIntent);
  homeInput.addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); goFromHome(); }
  });
}
$$('#homeChan button').forEach(b => b.addEventListener('click', () => {
  setChan(b.dataset.chan, true, normalizeUrlInput(homeInput ? homeInput.value : ''));
  syncHomeIntent();
  say('抓取通道：' + (chan === 'video' ? '视频' : '图片'));
  if (homeInput) homeInput.focus();
}));
paintChan();
setChan(chan, false);
const homeGoBtn = $('#homeGo');
if (homeGoBtn) homeGoBtn.addEventListener('click', goFromHome);
$$('[data-home-go]').forEach(b => {
  b.addEventListener('click', () => switchTab(b.dataset.homeGo));
});

let homeFetchedAt = 0;
// 任务数 / 存储余量直接读顶栏的实时值：不发请求，也不受下面 20 秒节流限制 ——
// 否则首页会长时间停在「磁盘 …」这种初始文案上
function syncHomeLocal() {
  const tasks = $('#homeTasks');
  if (tasks) tasks.textContent = ($('#sideTaskCount') || {}).textContent || '0';
  const disk = $('#homeDisk');
  if (disk) disk.textContent = (($('#envDisk') || {}).textContent || '—').trim();
}
async function syncHome(force) {
  const panel = document.getElementById('panel-home');
  if (!panel) return;
  syncHomeLocal();
  if (!force && Date.now() - homeFetchedAt < 20000) return;
  homeFetchedAt = Date.now();

  // 台账比扫下载目录便宜得多：只读一个 json，首页不必为两个数字遍历 14GB
  const imgCount = $('#homeImgCount'), vidCount = $('#homeVidCount');
  try {
    const d = await fetch('/api/history?limit=1').then(r => r.json());
    const by = (d.stats || {}).by_kind || {};
    if (imgCount) imgCount.textContent = '已存 ' + (by.image || 0) + ' 张';
    if (vidCount) vidCount.textContent = '已存 ' + (by.video || 0) + ' 个';
  } catch (e) {
    if (imgCount) imgCount.textContent = '已存 —';
    if (vidCount) vidCount.textContent = '已存 —';
  }

  try {
    const d = await fetch('/api/platforms/status').then(r => r.json());
    const ps = d.platforms || {};
    const keys = Object.keys(ps);
    const on = keys.filter(k => ps[k] && ps[k].logged_in).length;
    const el = $('#homeLogin');
    if (el) el.textContent = keys.length ? on + '/' + keys.length : '—';
  } catch (e) {
    const el = $('#homeLogin');
    if (el) el.textContent = '—';
  }

  syncHomeLocal();
}

/* ---------- 落点：上次关掉的页面，或首页 ---------- */
function restoreLastTab() {
  let last = null;
  try { last = localStorage.getItem(HOME_LAST_KEY); } catch (e) {}
  const panel = last && document.getElementById('panel-' + last);
  switchTab(panel ? last : 'home');
}
restoreLastTab();
syncHome(true);
setInterval(() => {
  const a = document.querySelector('.tab.active');
  if (a && a.dataset.tab === 'home') syncHome(false);
}, 5000);

enhanceAll();
updateIntent();
})();
