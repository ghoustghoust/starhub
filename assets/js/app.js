import { API_BASE, DATA, POOL_NAMES, favs, state } from './core/state.js';
import { $ } from './core/dom.js';
import { hydrateSearchState, syncSearchUrl } from './core/search-url.js';
import { escHtml } from './core/format.js';
import { copyRepoInfo, toast } from './core/ui.js';
import { getTheme, applyTheme, getView, applyView } from './features/preferences.js';
import { renderLangBar, renderCats, renderLangSel, renderList, updateCatsMask } from './features/collection.js';
import { renderSearchHint, openSearchModal, closeSearchModal, runSearch, editSearchQuery } from './features/search.js';
import { renderTrending, switchTrendTab, renderTrendingFull, openTrendModal, closeTrendModal } from './features/trending.js';
import { renderFeed, refreshFeed, refreshTrendingBoard } from './features/feed.js';
import { loadAihot, loadAihotMore, startFeedPolling } from './features/ai-feed.js';
import { toggleFav, exportJson, importJson } from './features/favorites.js';

/* Event wiring and page initialization. */
// ---- 事件绑定 ----
function switchSideTab(tab){
  state.sideTab = tab;
  document.querySelectorAll('.side-tab').forEach(x => x.classList.toggle('on', x.dataset.side === tab));
  const trending = $('.trending'), feed = $('.feed'), ai = $('#aihotWrap');
  if(trending) trending.classList.toggle('on', tab === 'trend');
  if(feed) feed.classList.toggle('on', tab === 'feed');
  if(ai) ai.classList.toggle('on', tab === 'aihot');
  if(tab === 'aihot') loadAihot();
}

function setSearchMode(mode, opts = {}){
  state.searchMode = mode === 'global' ? 'global' : 'local';
  document.querySelectorAll('#searchMode button').forEach(b => {
    b.classList.toggle('on', b.dataset.mode === state.searchMode);
    b.setAttribute('aria-selected', String(b.dataset.mode === state.searchMode));
  });
  if(state.searchMode === 'local') closeSearchModal();
  renderSearchHint();
  if(!opts.skipRender) renderList();
  if(!opts.skipUrl) syncSearchUrl();
}

function bindEvents(){
  // 导航下拉菜单：点击切换，点击外部关闭（支持多个独立下拉）
  var navDrops = document.querySelectorAll('.nav-drop');
  navDrops.forEach(function(drop){
    var btn = drop.querySelector('.nav-drop-btn');
    if(!btn) return;
    btn.addEventListener('click', function(e){
      e.stopPropagation();
      // 关闭其他已打开的下拉
      navDrops.forEach(function(other){
        if(other !== drop){
          other.classList.remove('open');
          var ob = other.querySelector('.nav-drop-btn');
          if(ob) ob.setAttribute('aria-expanded','false');
        }
      });
      var open = drop.classList.toggle('open');
      btn.setAttribute('aria-expanded', String(open));
    });
  });
  document.addEventListener('click', function(e){
    navDrops.forEach(function(drop){
      if(!drop.contains(e.target)){
        drop.classList.remove('open');
        var b = drop.querySelector('.nav-drop-btn');
        if(b) b.setAttribute('aria-expanded','false');
      }
    });
  });
  // 最近查询 + 常用 topic/owner/language，作为原生 datalist 建议
  const HIST_KEY = 'starhub_search_history';
  function loadHistory(){ try{ return JSON.parse(localStorage.getItem(HIST_KEY)||'[]'); }catch(e){ return []; } }
  function topValues(values, limit){
    const counts = {};
    values.forEach(v => { if(v) counts[v] = (counts[v]||0)+1; });
    return Object.entries(counts).sort((a,b)=>b[1]-a[1]).slice(0,limit).map(x=>x[0]);
  }
  function rebuildSearchSuggestions(){
    const history = loadHistory();
    const topics = topValues(DATA.flatMap(d=>d.topics||[]), 5).map(t=>'topic:'+t);
    const owners = topValues(DATA.map(d=>d.owner), 4).map(o=>'org:'+o);
    const languages = topValues(DATA.map(d=>d.language), 4).map(l=>'language:'+l);
    const suggestions = [...new Set([...history, ...topics, ...owners, ...languages])].slice(0,20);
    $('#qHistory').innerHTML = suggestions.map(x=>'<option value="'+escHtml(x)+'">').join('');
  }
  function saveHistory(q){
    q = (q||'').trim();
    if(q.length < 2) return;
    const h = [q].concat(loadHistory().filter(x=>x!==q)).slice(0,8);
    try{ localStorage.setItem(HIST_KEY, JSON.stringify(h)); }catch(e){}
    rebuildSearchSuggestions();
  }
  rebuildSearchSuggestions();

  let _qTimer = null;
  $('#q').addEventListener('input', e => {
    state.q = e.target.value;
    $('#btnClear').style.display = state.q ? 'block':'none';
    clearTimeout(_qTimer);
    _qTimer = setTimeout(() => {   // 120ms 防抖：条目增长后输入不至于每键全量重渲染
      renderSearchHint();
      if(state.searchMode === 'local') renderList();
      if(state.q.trim()) saveHistory(state.q);
      syncSearchUrl();
    }, 120);
  });
  $('#q').addEventListener('keydown', e => {
    if(e.key === 'Enter' && state.q.trim().length >= 2 && state.searchMode === 'global'){
      e.preventDefault(); openSearchModal();
    }
  });
  $('#btnClear').onclick = () => {
    $('#q').value=''; state.q=''; state.searchQuery=''; $('#btnClear').style.display='none';
    renderSearchHint(); renderList(); syncSearchUrl(); $('#q').focus();
  };
  $('#btnSearchAll').onclick = () => {
    if(state.searchMode !== 'global') setSearchMode('global');
    openSearchModal();
  };
  const modeBar = document.getElementById('searchMode');
  if(modeBar) modeBar.addEventListener('click', e => {
    const b = e.target.closest('[data-mode]'); if(b) setSearchMode(b.dataset.mode);
  });
  $('#btnResetAll').onclick = () => {
    state.q=''; state.searchQuery=''; state.cat=''; state.lang=''; state.star='all'; state.updated=''; state.topic='';
    $('#q').value=''; $('#btnClear').style.display='none';
    const us = $('#updatedSel'); if(us) us.value='';
    renderSearchHint();
    renderCats(); renderLangSel();
    document.querySelectorAll('#starSel button').forEach(x=>x.classList.toggle('on', x.dataset.v==='all'));
    renderList(); syncSearchUrl();
  };
  $('#langSel').addEventListener('change', e => { state.lang = e.target.value; renderList(); syncSearchUrl(); });
  $('#sortSel').addEventListener('change', e => { state.sort = e.target.value; state.sortTouched = true; renderList(); syncSearchUrl(); });
  const updatedSel = $('#updatedSel');
  if(updatedSel) updatedSel.addEventListener('change', e => { state.updated = e.target.value; renderList(); syncSearchUrl(); });
  $('#starSel').addEventListener('click', e => {
    const b = e.target.closest('button'); if(!b) return;
    state.star = b.dataset.v;
    document.querySelectorAll('#starSel button').forEach(x=>x.classList.toggle('on', x===b));
    renderList(); syncSearchUrl();
  });
  document.addEventListener('click', e => {
    const el = e.target.closest('[data-act]'); if(!el) return;
    const act = el.dataset.act;
    const card = el.closest('.card, .row');
    const nameEl = card ? card.querySelector('.name') : null;
    const ownerEl = card ? card.querySelector('.owner') : null;
    const fn = (nameEl && ownerEl) ? (ownerEl.textContent + '/' + nameEl.textContent) : '';
    if(act === 'fav') toggleFav(fn);
    if(act === 'cattag'){ state.cat = el.dataset.cat; renderCats(); renderList(); }
    // topic/org 标签点击 = 写入限定符（所有筛选语法化进搜索框，功能零耦合）
    if(act === 'topictag'){
      const t = 'topic:'+el.dataset.topic;
      $('#q').value = ($('#q').value.trim()+' '+t).trim(); state.q = $('#q').value;
      $('#btnClear').style.display='block'; renderSearchHint(); renderList();
    }
    if(act === 'orgtag'){
      const o = 'org:'+el.dataset.org;
      $('#q').value = ($('#q').value.trim()+' '+o).trim(); state.q = $('#q').value;
      $('#btnClear').style.display='block'; renderSearchHint(); renderList();
    }
    if(act === 'expand'){ const d = card ? card.querySelector('.desc') : null; if(!d) return; const expanded = d.classList.toggle('expand'); el.textContent = expanded ? '收起' : '展开'; }
    if(act === 'copy'){
      const nameA = card ? card.querySelector('a.name') : null;
      if(fn && nameA && nameA.href) copyRepoInfo(fn, nameA.href);
    }
  });

  window.addEventListener('resize', updateCatsMask);
  const resultList = $('#sList');
  if(resultList){
    resultList.addEventListener('click', e => {
      const btn = e.target.closest('[data-sact]'); if(!btn) return;
      const fullName = btn.dataset.fn;
      if(btn.dataset.sact === 'copy'){ if(fullName) copyRepoInfo(fullName, btn.dataset.url || ''); return; }
      if(btn.dataset.sact === 'fav' && fullName){
        toggleFav(fullName);
        const pinned = favs.has(fullName), starred = POOL_NAMES.has(fullName);
        btn.classList.toggle('on', pinned);
        btn.textContent = pinned ? '★' : '☆';
        const row = btn.closest('.s-item');
        let badge = row ? row.querySelector('.s-badge') : null;
        const label = pinned ? (starred ? '✓ 已 star · 置顶' : '★ 置顶') : (starred ? '✓ 已 star' : '');
        if(row && !badge && label){
          badge = document.createElement('span');
          badge.className = 's-badge';
          const name = row.querySelector('.s-name');
          if(name) name.after(badge); else row.querySelector('.s-top')?.prepend(badge);
        }
        if(badge){ badge.textContent = label; badge.style.display = label ? '' : 'none'; }
      }
    });
  }

  $('#btnExport').onclick = exportJson;
  $('#btnImport').onclick = () => $('#fileInput').click();
  // Vercel Function 中转触发 GitHub Actions（GH_TOKEN 存于 Vercel 环境变量）
  const REFRESH_API = API_BASE + '/api/refresh';
  let refreshCooldown = 0;
  $('#btnRefresh').onclick = async () => {
    const b = $('#btnRefresh');
    const now = Date.now();
    if (b.disabled) return;
    if (now - refreshCooldown < 60000) { toast('刷新请求已提交，请稍候再试'); return; }
    b.disabled = true;
    b.classList.add('loading');
    try {
      const r = await fetch(REFRESH_API, { method: 'POST', headers: { 'X-Refresh-Key': REFRESH_KEY } });
      const j = await r.json().catch(() => ({}));
      if (r.ok) { toast('已提交刷新请求，约 1-2 分钟后上线'); refreshCooldown = now; }
      else toast('刷新失败：' + (j.error || ('HTTP ' + r.status)));
    } catch(e) { toast('网络错误，请稍后重试'); }
    b.disabled = false;
    b.classList.remove('loading');
  };
  $('#fileInput').addEventListener('change', importJson);

  $('#btnTheme').onclick = () => {
    const t = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
    try{ localStorage.setItem(THEME_KEY, t); }catch(e){}
    applyTheme(t); renderCats(); renderList();
  };
  $('#btnView').onclick = () => {
    const v = getView() === 'list' ? 'card' : 'list';
    try{ localStorage.setItem(VIEW_KEY, v); }catch(e){}
    applyView(v); renderList();
  };
  $('#langToggle').addEventListener('click', () => {
    const t = $('#langToggle'), body = $('#langBody');
    const open = t.getAttribute('aria-expanded') === 'true';
    t.setAttribute('aria-expanded', String(!open));
    body.hidden = open;
  });
  $('#trendTabs').addEventListener('click', e => {
    const b = e.target.closest('[data-tab]'); if(!b) return;
    switchTrendTab(b.dataset.tab);
  });
  document.querySelectorAll('.side-tab').forEach(b => b.addEventListener('click', () => switchSideTab(b.dataset.side)));
  $('#mTrendTabs').addEventListener('click', e => {
    const b = e.target.closest('[data-tab]'); if(!b) return;
    switchTrendTab(b.dataset.tab, '#mTrendTabs');
    renderTrendingFull();
  });
  $('#sTabs').addEventListener('click', e => {
    const b = e.target.closest('[data-sort]'); if(!b) return;
    state.searchSort = b.dataset.sort;
    document.querySelectorAll('#sTabs .trend-tab').forEach(x => x.classList.toggle('on', x === b));
    runSearch('new');
  });
  $('#sLang').addEventListener('change', e => { state.searchLang = e.target.value; runSearch('new'); });
  $('#sEditQ').addEventListener('click', editSearchQuery);
  // 无限滚动：接近列表底部时加载下一页
  $('#sList').addEventListener('scroll', () => {
    const el = $('#sList');
    if(!searchHasMore || searchLoading) return;
    if(el.scrollHeight - el.scrollTop - el.clientHeight < 120) runSearch('more');
  });
  $('#trendMore').addEventListener('click', openTrendModal);
  // AI 动态：分类筛选（纯前端过滤）与游标加载更多
  $('#aihotLoadMore').addEventListener('click', loadAihotMore);
  $('#aihotFilter').addEventListener('click', e => {
    const b = e.target.closest('.fchip'); if(!b) return;
    aihotFilter = b.dataset.cat;
    renderAihot();
  });
  document.addEventListener('click', e => {
    if(e.target.closest('[data-close]')){ closeTrendModal(); closeSearchModal(); return; }
    if(e.target.classList.contains('modal')){ closeTrendModal(); closeSearchModal(); }
  });
  document.addEventListener('keydown', e => {
    const tag = (e.target.tagName || '').toLowerCase();
    const typing = tag === 'input' || tag === 'textarea' || e.target.isContentEditable;
    if(e.key === '/' && !typing){
      if($('#searchModal').classList.contains('open')) return; // 搜索工作台打开时不抢焦点
      e.preventDefault(); $('#q').focus();
    }
    if((e.key === 'd' || e.key === 'D') && !typing && !e.ctrlKey && !e.metaKey && !e.altKey){
      if($('#searchModal').classList.contains('open')) return;
      $('#btnTheme').click(); // 复用现有切换逻辑（含 localStorage 与重渲染）
    }
    if(e.key === '?' && !typing && !e.ctrlKey && !e.metaKey && !e.altKey){
      if($('#searchModal').classList.contains('open')) return;
      e.preventDefault();
      toast('快捷键：/ 聚焦搜索 · d 切换主题 · ? 本说明 · Esc 关闭弹层');
    }
    if(e.key === 'Escape'){
      if($('#trendModal').classList.contains('open')){ closeTrendModal(); return; }
      if($('#searchModal').classList.contains('open')){ closeSearchModal(); return; }
      if(document.activeElement === $('#q')){
        $('#q').value=''; state.q=''; $('#btnClear').style.display='none'; renderSearchHint(); renderList(); $('#q').blur();
      }
    }
  });
}

// ---- init ----
// 统计数字滚动动效：仅在首次进入视口时触发一次

function countUp(el, target, dur){
  dur = dur || 700;
  const t0 = performance.now();
  const step = now => {
    const p = Math.min((now - t0) / dur, 1);
    el.textContent = Math.round(target * (1 - Math.pow(1 - p, 3)));
    if(p < 1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}
if('IntersectionObserver' in window){
  const io = new IntersectionObserver(entries => {
    if(entries[0].isIntersecting && !state.counted){
      state.counted = true;
      countUp($('#stTotal'), DATA.length);
      io.disconnect();
    }
  }, {threshold:.4});
  io.observe($('#stTotal').parentElement.parentElement);
}else{ state.counted = true; }
hydrateSearchState();
applyTheme(getTheme());
applyView(getView());
renderLangBar();
renderCats();
renderLangSel();
$('#q').value = state.q;
$('#btnClear').style.display = state.q ? 'block' : 'none';
$('#langSel').value = state.lang;
$('#sortSel').value = state.sort;
const initialUpdated = $('#updatedSel'); if(initialUpdated) initialUpdated.value = state.updated;
document.querySelectorAll('#starSel button').forEach(x=>x.classList.toggle('on', x.dataset.v === state.star));
// 封面图 error 监听必须在 renderList 之前注册，否则首次渲染的图片加载错误会漏掉
// 加 500ms 延迟确认 + 一次重试，避免 lazy/async 加载的瞬时误判
document.addEventListener('error', e => {
  const t = e.target;
  if (t && t.classList && t.classList.contains('cover-img') && !t.dataset.errRetried) {
    t.dataset.errRetried = '1';
    setTimeout(() => { if (!t.complete || t.naturalWidth === 0) { t.src = t.src; } }, 500);
    return;
  }
  if (t && t.classList && t.classList.contains('cover-img')) {
    const c = t.closest('.cover-card'); if (c) c.classList.add('no-img');
  }
}, true);
renderList();
renderTrending();
renderFeed();
refreshFeed(); // 打开页面即拉取实时关注动态（静默失败，静态数据兜底）
refreshTrendingBoard(); // 排行榜走同源数据出口（内联那份只是首屏兜底）
startFeedPolling(); // 每 30 分钟轮询（页面不可见时暂停）
switchSideTab(state.sideTab);
bindEvents();
setSearchMode(state.searchMode, {skipRender: true, skipUrl: true});
syncSearchUrl();
if(state.searchMode === 'global' && state.q.trim().length >= 2) openSearchModal();
/* back to top */
(function(){
  var bt=document.getElementById('backTop');
  if(!bt)return;
  var onScroll=function(){bt.classList.toggle('show',window.scrollY>800)};
  window.addEventListener('scroll',onScroll,{passive:true});
  onScroll();
  bt.addEventListener('click',function(){window.scrollTo({top:0,behavior:'smooth'})});
})();
