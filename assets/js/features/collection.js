import { CATS, DATA, LANG_COLORS, favs, state } from '../core/state.js';
import { $, icon } from '../core/dom.js';
import { escHtml, fmtRelTime, fmtStars } from '../core/format.js';
import { parseQueryTokens, relevanceScore } from '../core/query.js';
import { getView } from './preferences.js';
import { syncSearchUrl } from '../core/search-url.js';

/* Collection rendering, filters and cards. */
// ---- 语言统计 ----
const langCount = {};
DATA.forEach(d => { const l = d.language || 'Other'; langCount[l] = (langCount[l]||0)+1; });
const langSorted = Object.entries(langCount).sort((a,b)=>b[1]-a[1]);
const topLangs = langSorted.slice(0, 8);
const topSum = topLangs.reduce((s,x)=>s+x[1],0);

export function renderLangBar(){
  const bar = $('#langBar'); bar.innerHTML='';
  topLangs.forEach(([l,n]) => {
    const i = document.createElement('i');
    i.style.width = (n/topSum*100)+'%';
    i.style.background = LANG_COLORS[l] || '#8b949e';
    i.title = l+' '+n;
    bar.appendChild(i);
  });
  const legend = $('#langLegend'); legend.innerHTML='';
  langSorted.forEach(([l,n]) => {
    const el = document.createElement('span'); el.className='li';
    el.innerHTML = '<span class="dot" style="background:'+(LANG_COLORS[l]||'#8b949e')+'"></span>'+l+' · '+n;
    legend.appendChild(el);
  });
  $('#lbCount').textContent = '共 '+langSorted.length+' 种语言';
}

// ---- 分类 tab ----
export function renderCats(){
  const cats = $('#cats'); cats.innerHTML='';
  const all = document.createElement('button');
  all.className = 'cat' + (state.cat===''?' on':'');
  all.innerHTML = '<span class="dot"></span>全部 <span class="n">'+DATA.length+'</span>';
  all.onclick = () => { state.cat=''; renderCats(); renderList(); syncSearchUrl(); };
  cats.appendChild(all);
  CATS.forEach(c => {
    const n = DATA.filter(d=>d.category===c.key).length;
    if(!n) return;
    const b = document.createElement('button');
    b.className = 'cat' + (state.cat===c.key?' on':'');
    // 类目 AI 导览（构建时生成）：悬停即读，无需点进任何详情页
    if(c.guide) b.title = c.guide;
    b.innerHTML = '<span class="dot" style="background:'+catColor(c.key)+'"></span>'+c.label+' <span class="n">'+n+'</span>';
    b.onclick = () => { state.cat = state.cat===c.key?'':c.key; renderCats(); renderList(); syncSearchUrl(); };
    cats.appendChild(b);
  });
  updateCatsMask();
}
/* 分类溢出检测：未溢出时移除渐隐蒙版，避免两端标签残缺 */
export function updateCatsMask(){
  const el = $('#cats'); if(!el) return;
  el.classList.toggle('no-mask', el.scrollWidth <= el.clientWidth + 2);
}

// ---- 语言下拉 ----
export function renderLangSel(){
  const sel = $('#langSel');
  const cur = state.lang;
  sel.innerHTML = '<option value="">全部语言</option>' +
    langSorted.map(([l,n])=>'<option value="'+l.replace(/"/g,'&quot;')+'"'+(l===cur?' selected':'')+'>'+l+' ('+n+')</option>').join('');
}

// ---- 模糊搜索 ----
export function hl(s, query){
  const t = escHtml(s);
  const activeQuery = query === undefined ? state.q : query;
  if(!activeQuery) return t;
  if(query === undefined && state.searchMode === 'global') return t;
  // 限定符（stars:>1k 等）不进高亮词表，只高亮普通关键词
  const tokens = parseQueryTokens(activeQuery).words
    .map(x => escHtml(x).replace(/[.*+?^${}()|[\]\\]/g,'\\$&'));
  if(!tokens.length) return t;
  tokens.sort((a,b)=>b.length-a.length);
  try{
    return t.replace(new RegExp('('+tokens.join('|')+')','gi'), '<mark>$1</mark>');
  }catch(e){ return t; }
}

export function fuzzyMatch(d, q){
  const hay = (d.name+' '+d.owner+' '+d.full_name+' '+(d.desc||'')+' '+(d.language||'')+' '+(d.categoryLabel||'')+' '+(d.topics||[]).join(' ')).toLowerCase();
  const tokens = q.toLowerCase().split(/\s+/).filter(Boolean);
  return tokens.every(t => hay.includes(t));
}

export function _withinDays(pushed, days){
  if(!pushed) return false;
  const t = new Date(pushed).getTime();
  if(isNaN(t)) return false;
  return (Date.now()-t) <= days*864e5;
}
export function filterData(){
  const localQuery = state.searchMode === 'global' ? '' : state.q;
  const {filters, words} = parseQueryTokens(localQuery);
  let list = DATA.filter(d => {
    // 限定符过滤（stars:/forks:/language:/license:/pushed:/org:/topic:/in:）
    if(filters.starsMin!=null && !(d.stars>=filters.starsMin)) return false;
    if(filters.starsMax!=null && !(d.stars<filters.starsMax)) return false;
    if(filters.forksMin!=null && !((d.forks||0)>=filters.forksMin)) return false;
    if(filters.lang && (d.language||'').toLowerCase()!==filters.lang.toLowerCase()) return false;
    if(filters.license && (d.license||'').toLowerCase()!==filters.license.toLowerCase()) return false;
    if(filters.pushedDays!=null && !_withinDays(d.pushed_at, filters.pushedDays)) return false;
    if(filters.org && (d.owner||'').toLowerCase()!==filters.org.toLowerCase()) return false;
    if(filters.topic && !(d.topics||[]).some(t=>t.toLowerCase()===filters.topic.toLowerCase())) return false;
    if(filters.inName && !words.every(w=>(d.name||'').toLowerCase().includes(w))) return false;
    // topic 横切（点卡片 topic 标签设置）：跨类目过滤
    if(state.topic && !(d.topics||[]).some(t=>t.toLowerCase()===state.topic.toLowerCase())) return false;
    // 更新时间档
    if(state.updated==='week' && !_withinDays(d.pushed_at,7)) return false;
    if(state.updated==='month' && !_withinDays(d.pushed_at,30)) return false;
    if(state.cat && d.category !== state.cat) return false;
    if(state.lang && d.language !== state.lang) return false;
    if(state.star === 'lt1k' && d.stars >= 1000) return false;
    if(state.star === '1k10k' && (d.stars < 1000 || d.stars >= 10000)) return false;
    if(state.star === '10k100k' && (d.stars < 10000 || d.stars >= 100000)) return false;
    if(state.star === 'gt100k' && d.stars < 100000) return false;
    if(words.length){
      if(filters.inDesc){
        // in:desc 已在上面钉了 name 词；这里要求描述也全含
        if(!words.every(w=>(d.desc||'').toLowerCase().includes(w))) return false;
      }
      const hay = (d.name+' '+d.owner+' '+d.full_name+' '+(d.desc||'')+' '+(d.language||'')+' '+(d.categoryLabel||'')+' '+(d.topics||[]).join(' ')).toLowerCase();
      if(!words.every(t => hay.includes(t))) return false;
    }
    return true;
  });
  // 相关性打分（q 非空时），供 relevant 排序档与语义扩展使用
  if(words.length) list.forEach(d => { d._score = relevanceScore(d, words); });
  // q 非空且用户未手动选过排序档 → 自动按相关度（显式选择过的用户不被打扰）
  const effSort = (words.length && !state.sortTouched) ? 'relevant' : state.sort;
  const cmp = {
    'relevant': (a,b)=>(b._score||0)-(a._score||0) || b.stars-a.stars,
    'starred': (a,b)=>(b.starred_at||'').localeCompare(a.starred_at||''),
    'stars-desc': (a,b)=>b.stars-a.stars,
    'stars-asc': (a,b)=>a.stars-b.stars,
    'recent': (a,b)=>(b.pushed_at||'').localeCompare(a.pushed_at||''),
    'name': (a,b)=>a.name.localeCompare(b.name),
  }[effSort] || ((a,b)=>b.stars-a.stars);
  list.sort(cmp);
  // 语义扩展（T7c）：关键词命中太少且向量可用时，用命中集质心从未命中里召回语义近邻。
  // 无 query 向量依赖（不需要给查询词做 embedding），零后端。
  let semantic = [];
  if(words.length && list.length>0 && list.length<6 && DATA.some(d=>d.emb)){
    const emb0 = list.find(d=>Array.isArray(d.emb)&&d.emb.length);
    if(emb0){
      const dim = emb0.emb.length;
      // 维度守卫必须是 ===dim：一条长度不符的向量会把质心越界项累成 NaN，
      // 所有 sim 静默归零（整段语义扩展消失）——两处过滤都按精确维度收
      const withEmb = list.filter(d=>Array.isArray(d.emb)&&d.emb.length===dim);
      if(withEmb.length){
        const centroid = new Array(dim).fill(0);
        withEmb.forEach(d=>d.emb.forEach((v,i)=>centroid[i]+=v/withEmb.length));
        const seen = new Set(list.map(d=>d.full_name));
        semantic = DATA.filter(d=>Array.isArray(d.emb)&&d.emb.length===dim&&!seen.has(d.full_name))
        .map(d=>({d, sim:d.emb.reduce((acc,v,i)=>acc+v*centroid[i],0)}))
        .filter(x=>x.sim>0.3)
        .sort((a,b)=>b.sim-a.sim)
        .slice(0,5)
        .map(x=>Object.assign({}, x.d, {_sem:true, _score:undefined}));
      }
    }
  }
  const faved = list.filter(d=>favs.has(d.full_name));
  const rest = list.filter(d=>!favs.has(d.full_name));
  return { faved, rest, semantic };
}

export function catColor(key){
  const c = CATS.find(x=>x.key===key);
  if(!c) return '#8b949e';
  const raw = document.documentElement.dataset.theme === 'dark' ? (c.dark || c.color) : c.color;
  return clampChroma(raw, document.documentElement.dataset.theme === 'dark' ? 0.65 : 0.30, document.documentElement.dataset.theme === 'dark' ? 0.78 : 0.48);
}
/* 分类色亮度钳制：浅色主题保证文字对比度下限，暗色主题保证可辨亮度 */
export function clampChroma(hex, lo, hi){
  const m = /^#([0-9a-f]{6})$/i.exec(hex); if(!m) return hex;
  let r=parseInt(m[1].slice(0,2),16)/255, g=parseInt(m[1].slice(2,4),16)/255, b=parseInt(m[1].slice(4,6),16)/255;
  const mx=Math.max(r,g,b), mn=Math.min(r,g,b);
  let h=0, s=0, l=(mx+mn)/2;
  if(mx!==mn){const d=mx-mn; s=l>.5?d/(2-mx-mn):d/(mx+mn);
    if(mx===r)h=(g-b)/d+(g<b?6:0); else if(mx===g)h=(b-r)/d+2; else h=(r-g)/d+4;
    h/=6;}
  if(l>=lo && l<=hi) return hex;
  l=Math.min(Math.max(l,lo),hi);
  if(s===0) {const v=Math.round(l*255).toString(16).padStart(2,'0'); return '#'+v+v+v;}
  const q=l<.5?l*(1+s):l+s-l*s, p=2*l-q;
  const hue2rgb=(t)=>{if(t<0)t+=1; if(t>1)t-=1; if(t<1/6)return p+(q-p)*6*t; if(t<1/2)return q; if(t<2/3)return p+(q-p)*(2/3-t)*6; return p;};
  return '#'+[h+1/3,h,h-1/3].map(x=>Math.round(hue2rgb(x)*255).toString(16).padStart(2,'0')).join('');
}

export function buildCard(d, isFav){
  const div = document.createElement('article');
  div.className = 'card cover-card' + (isFav?' faved':'') + (d.stale?' stale':'');
  const catColorVal = catColor(d.category);
  const coverSrc = d.cover_image || ('https://opengraph.githubassets.com/1/' + d.full_name);
  const relUpd = fmtRelTime(d.pushed_at);
  const desc = d.desc || '暂无简介';
  const descHtml = desc.length > 60
    ? '<span class="desc-text">'+hl(desc)+'</span><button class="more" data-act="expand">展开</button>'
    : '<span class="desc-text">'+hl(desc)+'</span>';
  const HB_LABEL = {green:'健康', yellow:'一般', red:'停滞'};
  const healthBadge = (d.health && HB_LABEL[d.health])
    ? '<span class="hb hb-'+d.health+'" title="仓库健康度（构建时按更新/社区/文档/归档状态计算）">'+HB_LABEL[d.health]+'</span>' : '';
  const noteLine = d.note ? '<div class="note-line" title="AI 一句话点评">✦ '+hl(d.note)+'</div>' : '';
  const semBadge = d._sem ? '<span class="sem-badge" title="按向量相似度扩展召回">语义相关</span>' : '';
  const topicTags = (d.topics||[]).slice(0,3).map(t =>
    '<span class="tp" data-act="topictag" data-topic="'+escHtml(t)+'" title="筛选该 topic">'+escHtml(t)+'</span>').join('');
  div.innerHTML =
    '<a class="cover" href="'+d.html_url+'" target="_blank" rel="noopener" tabindex="-1" aria-hidden="true">' +
      '<img class="cover-img" src="'+coverSrc+'" alt="" loading="lazy" decoding="async">' +
      '<span class="cover-fallback" aria-hidden="true"><span class="mono-mark">'+escHtml((d.name||'#').charAt(0))+'</span></span>' +
      (d.updated_today ? '<span class="badge-update cover-badge">今日更新</span>' : '') +
    '</a>' +
    '<button class="fav'+(isFav?' on':'')+'" data-act="fav" title="收藏/取消收藏">'+(isFav?icon.starFilled:icon.star)+'</button>' +
    '<div class="body">' +
      '<div class="top"><div class="id">' +
        '<div class="owner-line"><span class="owner" data-act="orgtag" data-org="'+escHtml(d.owner)+'" title="筛选该作者">'+escHtml(d.owner)+'</span>' +
          (isFav ? '<span class="pin-chip">置顶</span>' : '') +
        '</div>' +
        '<a class="name" href="'+d.html_url+'" target="_blank" rel="noopener">'+hl(d.name)+'</a>' +
      '</div></div>' +
      '<div class="desc" data-act="desc">'+descHtml+'</div>' +
      noteLine +
      (topicTags ? '<div class="topics">'+topicTags+'</div>' : '') +
    '</div>' +
    '<div class="foot">' +
      '<div class="meta">' +
        (d.language ? '<span class="lg"><span class="c" style="background:'+(LANG_COLORS[d.language]||'#8b949e')+'"></span>'+d.language+'</span>' : '') +
        '<span class="st">'+icon.starFilled+fmtStars(d.stars)+'</span>' +
        '<span class="tm">'+(relUpd ? relUpd+' 更新' : '—')+'</span>' +
        healthBadge + semBadge +
      '</div>' +
      '<div class="foot-row">' +
        '<a class="cat-tag" style="color:'+catColorVal+';background:'+catColorVal+'1f" href="javascript:void 0" data-act="cattag" data-cat="'+d.category+'">' +
          '<span class="dot"></span>'+d.categoryLabel+'</a>' +
        '<button class="copy-btn" data-act="copy" title="复制名称与链接">'+icon.copy+'复制</button>' +
        '<a class="open" href="'+d.html_url+'" target="_blank" rel="noopener">'+icon.link+'打开</a>' +
      '</div>' +
    '</div>';
  return div;
}

export function buildRow(d, isFav){
  const div = document.createElement('div');
  div.className = 'row' + (isFav?' faved':'');
  const catColorVal = catColor(d.category);
  const desc = d.desc || '暂无简介';
  div.innerHTML =
    '<button class="fav'+(isFav?' on':'')+'" data-act="fav" title="收藏/取消收藏">'+(isFav?icon.starFilled:icon.star)+'</button>' +
    '<div class="nm"><span class="owner">'+escHtml(d.owner)+'</span>' +
      '<a class="name" href="'+d.html_url+'" target="_blank" rel="noopener">'+hl(d.name)+'</a></div>' +
    '<span class="dsc" title="'+escHtml(desc)+'">'+hl(desc)+'</span>' +
    '<div class="meta">' +
      (d.updated_today ? '<span class="badge-update">今日更新</span>' : '') +
      (d.language ? '<span class="lg"><span class="c" style="background:'+(LANG_COLORS[d.language]||'#8b949e')+'"></span>'+d.language+'</span>' : '') +
      '<span class="st">'+icon.starFilled+''+fmtStars(d.stars)+'</span>' +
      '<span class="cat-tag" style="color:'+catColorVal+';background:'+catColorVal+'1f"><span class="dot"></span>'+d.categoryLabel+'</span>' +
      '<button class="copy-btn" data-act="copy" title="复制名称与链接">'+icon.copy+'复制</button>' +
      '<a class="st" style="color:var(--accent);font-weight:600" href="'+d.html_url+'" target="_blank" rel="noopener">'+icon.link+'打开</a>' +
    '</div>';
  return div;
}


export function renderList(){
  const { faved, rest, semantic } = filterData();
  // 语义扩展条目追加在结果尾部（带"语义相关"角标，_sem 由 buildCard 渲染）
  const semBadged = (semantic||[]).map(d=>{ const c=Object.assign({},d); c._sem=true; return c; });
  const restAll = rest.concat(semBadged);
  const pinnedWrap = $('#pinnedWrap'), pinned = $('#pinned'), grid = $('#grid'), empty = $('#empty');
  const isList = getView() === 'list';
  const build = isList ? buildRow : buildCard;

  // 置顶区
  if(faved.length){
    pinnedWrap.style.display='';
    pinned.innerHTML='';
    faved.forEach(d => pinned.appendChild(build(d, true)));
    $('#pinnedCount').textContent = '('+faved.length+')';
  } else {
    pinnedWrap.style.display='none';
  }

  // 主列表
  grid.innerHTML='';
  restAll.forEach(d => grid.appendChild(build(d, false)));
  // 只改标题文本节点（firstChild），保留 span#listCount 子节点：
  // textContent 整体赋值会销毁子节点，使 #listCount 从 DOM 消失、下次查询为 null
  const titleEl = $('#listTitle'), countEl = $('#listCount');
  if(titleEl.firstChild) titleEl.firstChild.textContent = state.cat
    ? (CATS.find(c=>c.key===state.cat)||{}).label || '全部项目'
    : '全部项目';
  if(countEl) countEl.textContent = '('+restAll.length+')';

  const hasFilter = state.cat || state.lang || state.star !== 'all' || state.updated || state.topic || state.q;
  empty.style.display = (faved.length + restAll.length === 0) ? 'block' : 'none';
  if(faved.length + restAll.length === 0){
    $('#emptyHint').textContent = hasFilter ? '当前有筛选在生效，可能挡住了结果（试试限定符 topic:xxx 或换关键词）' : '试试换一个关键词';
    $('#btnResetAll').style.display = hasFilter ? 'inline-flex' : 'none';
  }

  // 统计
  $('#stTotal').textContent = state.counted ? DATA.length : '0';
  $('#stCats').textContent = CATS.length;
  $('#stLangs').textContent = langSorted.length;
  $('#stFav').textContent = favs.size;
}
