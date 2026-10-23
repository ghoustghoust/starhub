import { API_BASE, LANG_COLORS, POOL_NAMES, SEARCH_KEY, favs, state } from '../core/state.js';
import { $, icon } from '../core/dom.js';
import { escHtml, fmtRelTime, fmtStars } from '../core/format.js';
import { toast } from '../core/ui.js';
import { filterData, hl } from './collection.js';
import { syncSearchUrl } from '../core/search-url.js';

const SEARCH_API = API_BASE + '/api/search';

/* Global GitHub search modal. */
// ---- 全网仓库搜索 ----
const SEARCH_LANGS = ['JavaScript','TypeScript','Python','Go','Rust','Java','C++','C','C#','Swift','Kotlin','Ruby','PHP','Shell','HTML','CSS','Dart','Lua'];
const SEARCH_PER_PAGE = 30; // 与 api/search.js PER_PAGE 一致，用于判断是否还有下一页
const CJK_RE = /[\u3400-\u9fff]/;
const RESULT_CACHE_TTL = 5 * 60 * 1000;
const RESULT_CACHE_MAX = 24;
const resultCache = new Map();
let searchAbort = null;
let sEditing = false;
let searchHasMore = false;
let searchLoading = false;
let searchLoaded = 0;
let lastFocused = null;

function resultCacheKey(q, sort, lang, page){
  return [q.trim().toLowerCase(), sort, lang, page].join('|');
}
function getCachedResult(key){
  const hit = resultCache.get(key);
  if(!hit) return null;
  if(Date.now() - hit.t > RESULT_CACHE_TTL){ resultCache.delete(key); return null; }
  return hit.data;
}
function setCachedResult(key, data){
  if(resultCache.size >= RESULT_CACHE_MAX) resultCache.delete(resultCache.keys().next().value);
  resultCache.set(key, {t: Date.now(), data});
}

const searchModalEl = document.getElementById('searchModal');
if(searchModalEl){
  searchModalEl.addEventListener('keydown', e => {
    if(e.key === 'Escape'){ closeSearchModal(); return; }
    if(e.key !== 'Tab') return;
    const focusable = [...searchModalEl.querySelectorAll('a[href],button:not([disabled]),input,select,[tabindex]:not([tabindex="-1"])')]
      .filter(el => el.offsetParent !== null);
    if(!focusable.length) return;
    const first = focusable[0], last = focusable[focusable.length - 1];
    if(e.shiftKey && document.activeElement === first){ e.preventDefault(); last.focus(); }
    else if(!e.shiftKey && document.activeElement === last){ e.preventDefault(); first.focus(); }
  });
}

export function renderSearchHint(){
  const btn = $('#btnSearchAll'), hint = $('#searchHint');
  const q = state.q.trim();
  const isGlobal = state.searchMode === 'global';
  btn.classList.toggle('hidden', q.length < 2);
  btn.textContent = isGlobal ? '搜索 GitHub' : '去 GitHub 搜';
  btn.title = isGlobal ? '在整个 GitHub 中搜索（跨语言）' : '切到全 GitHub 搜索';
  if(q.length < 2){
    hint.innerHTML = isGlobal ? '输入至少 2 个字符后按 Enter 搜索整个 GitHub' : '在收藏中筛选；切到全 GitHub 可搜索整个站点';
    return;
  }
  if(isGlobal){
    hint.innerHTML = '按 Enter 或点击按钮，在 GitHub 全站搜索 <span class="n">' + escHtml(q) + '</span>';
    return;
  }
  const { faved, rest } = filterData();
  hint.innerHTML = '收藏池匹配 <span class="n">' + (faved.length + rest.length) + '</span> 个 · 点「去 GitHub 搜」扩展';
}


export function openSearchModal(){
  state.searchQuery = state.q.trim();
  if(state.searchQuery.length < 2){ toast('请输入至少 2 个字符'); return; }
  lastFocused = document.activeElement;
  $('#sTitleQ').textContent = state.searchQuery;
  const lang = $('#sLang');
  if(lang.options.length <= 1){
    lang.innerHTML = '<option value="">全部语言</option>' + SEARCH_LANGS.map(l => '<option value="'+l+'">'+l+'</option>').join('');
  }
  lang.value = state.searchLang || '';
  document.querySelectorAll('#sTabs .trend-tab').forEach(x => x.classList.toggle('on', x.dataset.sort === state.searchSort));
  $('#searchModal').classList.add('open');
  // 立即显示原词，避免加载期间查询词框空白（翻译完成后再覆盖为真实查询词）
  $('#sQuery').textContent = state.searchQuery;
  setTimeout(() => {
    const close = $('#searchModal .close');
    if(close) close.focus();
  }, 0);
  syncSearchUrl();
  runSearch('new');
}

export function closeSearchModal(){
  $('#searchModal').classList.remove('open');
  if(searchAbort){ searchAbort.abort(); searchAbort = null; }
  if(lastFocused && typeof lastFocused.focus === 'function') lastFocused.focus();
  lastFocused = null;
}

export async function runSearch(mode){
  if(searchLoading) return; // 防止并发（含滚动触发的加载更多）
  const page = mode === 'more' ? state.searchPage + 1 : 1;
  const query = state.searchQuery;
  const cacheKey = resultCacheKey(query, state.searchSort, state.searchLang, page);
  if(mode !== 'more'){
    state.searchPage = 1;
    searchLoaded = 0; searchHasMore = false;
  }
  const cached = getCachedResult(cacheKey);
  if(cached){
    state.searchPage = page;
    renderSearchResults(cached, mode === 'more');
    syncSearchUrl();
    return;
  }
  searchLoading = true;
  if(searchAbort) searchAbort.abort();
  const ac = new AbortController(); searchAbort = ac;
  if(mode !== 'more'){
    renderSearchState('loading');
  } else {
    $('#sFoot').textContent = '正在加载更多…';
  }
  try{
    const r = await fetch(SEARCH_API, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Search-Key': SEARCH_KEY },
      body: JSON.stringify({ q: query, sort: state.searchSort, lang: state.searchLang, page }),
      signal: ac.signal,
    });
    const j = await r.json().catch(() => ({}));
    if(r.ok){
      state.searchPage = page;
      setCachedResult(cacheKey, j);
      renderSearchResults(j, mode === 'more');
      syncSearchUrl();
    } else if(mode === 'more'){
      searchMoreFail();
    } else if(r.status === 503){
      renderSearchState('error', '搜索太频繁，请稍后再试');
    } else {
      renderSearchState('error', (j.error || '搜索失败') + '（HTTP ' + r.status + '）');
    }
  }catch(e){
    if(e.name !== 'AbortError'){
      if(mode === 'more') searchMoreFail();
      else renderSearchState('error', '网络错误，请检查连接后重试');
    }
  }
  searchLoading = false;
}

export function searchMoreFail(){
  $('#sFoot').innerHTML = '加载更多失败，<button class="s-retry" id="sMoreRetry">点击重试</button>';
  const rb = $('#sMoreRetry'); if(rb) rb.onclick = () => runSearch('more');
}

export function renderSearchState(kind, msg){
  const list = $('#sList'); $('#sFoot').textContent = '';
  if(kind === 'loading'){
    const tip = $('#sTip'); if(tip){ tip.classList.add('hidden'); tip.classList.remove('ok'); tip.innerHTML = ''; }
    list.innerHTML = '<div class="s-loading"><span class="spinner"></span>正在搜索 GitHub 全网…</div>';
  }else if(kind === 'empty'){
    const hint = msg || '试试缩短关键词、换英文词，或清除语言过滤';
    const clearLang = state.searchLang
      ? '<button class="btn s-clear-lang" id="sClearLang">清除语言过滤</button>'
      : '';
    list.innerHTML = '<div class="s-state"><svg class="icon" viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg><span>未找到相关仓库</span><span style="font-size:12px">' + escHtml(hint) + '</span>' + clearLang + '</div>';
    const clear = $('#sClearLang');
    if(clear) clear.onclick = () => {
      state.searchLang = '';
      const sel = $('#sLang'); if(sel) sel.value = '';
      runSearch('new');
    };
  }else{
    const tip = $('#sTip'); if(tip){ tip.classList.add('hidden'); tip.classList.remove('ok'); tip.innerHTML = ''; }
    list.innerHTML = '<div class="s-state"><svg class="icon" viewBox="0 0 24 24"><path d="M12 9v4"/><path d="M12 17h.01"/><circle cx="12" cy="12" r="10"/></svg><span>' + (msg || '搜索失败，请稍后重试') + '</span><button class="btn" id="sRetry">重试</button></div>';
    const retry = $('#sRetry'); if(retry) retry.onclick = () => runSearch('new');
  }
}

export function renderSearchResults(j, append){
  $('#sQuery').textContent = j.query || state.searchQuery;
  const original = j.original_query || state.searchQuery;
  const needsTranslation = typeof j.needs_translation === 'boolean' ? j.needs_translation : CJK_RE.test(original);
  const translatedTerms = Array.isArray(j.translated_terms) ? j.translated_terms : [];
  const tip = $('#sTip');
  tip.classList.remove('ok');
  if(needsTranslation && j.translated === false){
    tip.classList.remove('hidden');
    tip.innerHTML = '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 9v4"/><path d="M12 17h.01"/><circle cx="12" cy="12" r="10"/></svg>翻译服务暂不可用，已按原词搜索；改用英文关键词可提升召回';
  } else if(needsTranslation && j.translated && translatedTerms.length){
    tip.classList.remove('hidden');
    tip.classList.add('ok');
    tip.innerHTML = '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M20 6L9 17l-5-5"/></svg>已翻译为 ' + escHtml(translatedTerms.slice(0, 4).join(' / ')) + '，同时搜索中英文结果';
  } else {
    tip.classList.add('hidden'); tip.innerHTML = '';
  }
  const highlightQuery = (Array.isArray(j.highlight_terms) && j.highlight_terms.length)
    ? j.highlight_terms.join(' ')
    : original;
  const list = $('#sList');
  if(!append) list.innerHTML = '';
  if(!j.items || !j.items.length){
    if(!append){
      const hint = (needsTranslation && j.translated === false)
        ? '翻译暂不可用，试试用英文关键词或缩短查询'
        : (state.searchLang ? '试试换关键词，或清除语言过滤' : '试试缩短关键词或换英文词');
      renderSearchState('empty', hint);
      // total>0 却一条没剩下时，只报「未找到相关仓库」会把人支去换词——得说明是被屏蔽了
      const fo = j.filtered_out || 0;
      if(fo) $('#sFoot').textContent = '本页 ' + fo + ' 条结果已屏蔽（低质或不相关），试试换更具体的关键词';
    }
    return;
  }
  j.items.forEach(x => {
    const row = document.createElement('div');
    row.className = 's-item';
    const starred = POOL_NAMES.has(x.full_name);
    const pinned = favs.has(x.full_name);
    const badge = pinned ? (starred ? '✓ 已 star · 置顶' : '★ 置顶') : (starred ? '✓ 已 star' : '');
    const fullName = escHtml(x.full_name || '');
    const url = escHtml(x.html_url || '#');
    const topics = (x.topics || []).map(t => '<span class="s-tag">' + escHtml(t) + '</span>').join('');
    row.innerHTML =
      '<div class="s-top">' +
        '<a class="s-name" href="' + url + '" target="_blank" rel="noopener">' + hl(x.full_name || '', highlightQuery) + '</a>' +
        (badge ? '<span class="s-badge">' + badge + '</span>' : '') +
        '<div class="s-actions">' +
          '<button class="s-act' + (pinned ? ' on' : '') + '" data-sact="fav" data-fn="' + fullName + '" title="收藏置顶">' + (pinned ? '★' : '☆') + '</button>' +
          '<button class="s-act" data-sact="copy" data-fn="' + fullName + '" data-url="' + url + '">复制</button>' +
          '<a class="s-open" href="' + url + '" target="_blank" rel="noopener">' + icon.link + '打开</a>' +
        '</div>' +
      '</div>' +
      (x.desc ? '<div class="s-desc">' + hl(x.desc, highlightQuery) + '</div>' : '') +
      '<div class="s-meta">' +
        (x.language ? '<span class="lg"><span class="c" style="background:' + (LANG_COLORS[x.language] || '#8b949e') + '"></span>' + escHtml(x.language) + '</span>' : '') +
        '<span>★ ' + fmtStars(x.stars || 0) + '</span>' +
        '<span>' + fmtRelTime(x.updated_at) + ' 更新</span>' +
        (topics ? '<span class="s-tags">' + topics + '</span>' : '') +
      '</div>';
    list.appendChild(row);
  });
  // 分页状态：已加载条数 + 是否还有下一页（滚动触发加载更多）
  searchLoaded += (j.items || []).length;
  const total = j.total || 0;
  // 「还有下一页」看服务端原始条数，不能看过滤后的 items.length —— 后者一屏蔽就误判到底。
  // 旧部署/旧缓存没这个字段时回退成 items.length，保持与改动前同形。
  const rawCount = (typeof j.raw_count === 'number') ? j.raw_count : (j.items || []).length;
  searchHasMore = rawCount === SEARCH_PER_PAGE && searchLoaded < Math.min(total, 1000);
  const spamNote = j.filtered_out ? ' · 已屏蔽 ' + j.filtered_out + ' 条低质或不相关结果' : '';
  $('#sFoot').textContent = (searchHasMore
    ? '已显示 ' + searchLoaded + ' / 共 ' + Math.min(total, 1000) + ' 条 · 滚动加载更多'
    : (total > 1000 ? '已显示前 ' + searchLoaded + ' 条（共 ' + total + ' 个结果，GitHub 最多返回前 1000 条）' : '已显示全部 ' + searchLoaded + ' 条')) + spamNote;
}

export function editSearchQuery(){
  if(sEditing) return;
  sEditing = true;
  const box = $('#sQuery');
  const cur = box.textContent;
  box.innerHTML = '<input class="s-q-input" id="sQInput" value="' + escHtml(cur) + '" spellcheck="false">';
  const inp = $('#sQInput');
  inp.focus(); inp.select();
  const done = () => {
    if(!sEditing) return;
    sEditing = false;
    const v = inp.value.trim();
    box.textContent = v || cur;
    if(v && v !== cur){
      state.searchQuery = v;
      state.q = v;
      const heroInput = $('#q');
      if(heroInput){ heroInput.value = v; $('#btnClear').style.display = 'block'; }
      $('#sTitleQ').textContent = v;
      renderSearchHint();
      syncSearchUrl();
      runSearch('new');
    }
  };
  inp.addEventListener('keydown', e => {
    if(e.key === 'Enter'){ e.preventDefault(); done(); }
    else if(e.key === 'Escape'){ sEditing = false; box.textContent = cur; }
  });
  inp.addEventListener('blur', done);
}
