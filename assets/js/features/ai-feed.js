/* AI news feed. */
// ---- AI 动态流（AIHOT 公开 API + /api/news 多源代理，浏览器直连，无需 Key） ----
let aihotLoaded = false;
let aihotLoading = false;
let aihotMoreLoading = false;
let aihotItems = [];      // 已加载全量条目（AIHOT + 代理多源合并去重后）
let aihotCursor = '';     // AIHOT 游标分页（page.nextCursor），空串表示无下一页
let aihotFilter = 'all';  // 分类筛选：'all' 或 AIHOT_CATS 键

function normTitle(t){ return (t || '').toLowerCase().replace(/[^\p{L}\p{N}]+/gu, ''); }

// 36氪/Redis 等 AI 资讯流（/api/news 多源代理）：归一化为 AIHOT 条目结构，每条携带 source 标记；
// 仅保留近 24 小时前 15 条（避免盖过 AIHOT 主源）；失败静默返回空数组
async function fetchNewsItems(){
  try{
    const r = await fetch(NEWS_API);
    if(!r.ok) return [];
    const j = await r.json();
    const cutoff = Date.now() - 24 * 3600 * 1000;
    return (j.items || []).filter(it => it.title && it.link && (!it.publishedAt || new Date(it.publishedAt).getTime() >= cutoff))
      .map(it => ({
        title: it.title, category: 'industry',
        source: { name: it.source || '36氪' },
        links: { original: it.link },
        publishedAt: it.publishedAt || '', selected: false,
      }))
      .slice(0, 15);
  }catch(e){ return []; }
}

// 渲染筛选条：分类计数随已加载条目实时刷新，当前选中高亮
function renderAihotFilter(){
  const box = $('#aihotFilter');
  if(!box) return;
  if(!aihotItems.length){ box.style.display = 'none'; return; }
  box.style.display = '';
  const cats = [['all', '全部']].concat(Object.keys(AIHOT_CATS).map(k => [k, AIHOT_CATS[k][0]]));
  box.innerHTML = cats.map(([k, label]) => {
    const n = k === 'all' ? aihotItems.length : aihotItems.filter(it => it.category === k).length;
    return '<button class="fchip' + (aihotFilter === k ? ' on' : '') + '" data-cat="' + k + '">' + label + ' <span class="n">' + n + '</span></button>';
  }).join('');
}

// 「加载更多」仅在无筛选且 AIHOT 还有下一页时显示；站外完整流入口始终保留兜底
function updateAihotMore(){
  const loadMore = $('#aihotLoadMore');
  if(loadMore) loadMore.style.display = (aihotCursor && aihotFilter === 'all') ? '' : 'none';
}

// 检测标题是否主要为非中文（需要翻译）
function _needsTranslation(t){ if(!t) return false; const cjk=(t.match(/[\u4e00-\u9fff\u3400-\u4dbf]/g)||[]).length; return cjk < t.replace(/[\s\d\p{P}]/gu,'').length * 0.3; }
// 批量翻译 AI 动态流英文标题（Google Translate GTX 端点；5s 超时兜底，防 GFW 挂死）
function _translateAihotItems(){
  const toTranslate = aihotItems.filter(it => !it._zh && _needsTranslation(it.title));
  if(!toTranslate.length) return;
  const texts = toTranslate.map(it => it.title);
  const chunks = []; for(let i=0;i<texts.length;i+=10) chunks.push(texts.slice(i,i+10));
  let done = 0;
  chunks.forEach(chunk => {
    const ctrl = typeof AbortController === 'function' ? new AbortController() : null;
    const tmr = ctrl ? setTimeout(() => ctrl.abort(), 5000) : null;
    const url = 'https://translate.googleapis.com/translate_a/single?client=gtx&sl=auto&tl=zh-CN&dt=t&q=' + encodeURIComponent(chunk.join('\n'));
    fetch(url, ctrl ? { signal: ctrl.signal } : {}).then(r => r.json()).then(j => {
      if(tmr) clearTimeout(tmr);
      const translated = []; try{ j[0].forEach(s => translated.push(s[0])); }catch(e){}
      const offset = chunks.indexOf(chunk) * 10;
      translated.forEach((zh, idx) => { if(zh && toTranslate[offset+idx]) toTranslate[offset+idx]._zh = zh; });
      if(++done >= chunks.length) renderAihot();
    }).catch(() => { if(tmr) clearTimeout(tmr); if(++done >= chunks.length) renderAihot(); });
  });
}

function renderAihot(){
  const list = $('#aihotList');
  renderAihotFilter();
  const items = aihotFilter === 'all' ? aihotItems : aihotItems.filter(it => it.category === aihotFilter);
  list.innerHTML = '';
  if(!items.length){
    list.innerHTML = '<div style="color:var(--muted);font-size:13px;text-align:center;padding:16px 0">' + (aihotItems.length ? '该分类暂无动态' : '暂无动态') + '</div>';
    updateAihotMore();
    return;
  }
  items.forEach(it => {
    const cat = AIHOT_CATS[it.category] || ['动态','#8b949e'];
    const url = (it.links && (it.links.original || it.links.aihot)) || '#';
    const src = (it.source && it.source.name) || '';
    const tmStr = fmtRelTime(it.publishedAt); // 相对时间（如「3 小时前」），复用全网搜索的函数
    const row = document.createElement('div');
    row.className = 'feed-ai-item';
    row.innerHTML =
      '<span class="cat" style="color:'+cat[1]+';background:'+cat[1]+'1a">'+escHtml(cat[0])+'</span>' +
      '<div class="body">' +
        '<a class="t" href="'+escHtml(url)+'" target="_blank" rel="noopener" title="'+escHtml(it.title||'')+'">'+escHtml(it._zh || it.title||'')+'</a>' +
        '<span class="meta">'+escHtml(src)+(src && tmStr ? ' · ' : '')+escHtml(tmStr)+(it.selected ? ' · ★ 精选' : '')+'</span>' +
      '</div>';
    list.appendChild(row);
  });
  updateAihotMore();
}

// 加载更多：AIHOT 游标分页（&cursor=page.nextCursor），追加条目同样按规范化标题去重；
// window=24h 参数不变，翻页内容仍在近 24 小时窗口内；失败静默提示不影响已有列表
async function loadAihotMore(){
  if(aihotMoreLoading || !aihotCursor) return;
  aihotMoreLoading = true;
  const btn = $('#aihotLoadMore');
  if(btn){ btn.disabled = true; btn.textContent = '加载中…'; }
  try{
    const r = await fetch(AIHOT_API + '&cursor=' + encodeURIComponent(aihotCursor));
    if(!r.ok) throw new Error(r.status);
    const j = await r.json();
    const page = j.page || {};
    aihotCursor = page.hasMore ? (page.nextCursor || '') : '';
    const seen = new Set(aihotItems.map(x => normTitle(x.title)));
    (j.items || []).forEach(it => { const k = normTitle(it.title); if(!seen.has(k)){ seen.add(k); aihotItems.push(it); } });
    renderAihot();
    _translateAihotItems();
  }catch(e){
    toast('加载更多失败，请稍后重试');
  }finally{
    aihotMoreLoading = false;
    if(btn){ btn.disabled = false; btn.textContent = '加载更多'; }
  }
}

async function loadAihot(){
  if(aihotLoaded || aihotLoading) return;
  aihotLoading = true;
  const list = $('#aihotList');
  $('#aihotUpdated').textContent = '加载中…';
  list.innerHTML = '';
  try{
    // ① 主源 AIHOT 先拉取、先渲染；超时 10s 放弃（移动端弱网兜底）
    const aiCtrl = typeof AbortController === 'function' ? new AbortController() : null;
    const aiTimer = aiCtrl ? setTimeout(() => aiCtrl.abort(), 10000) : null;
    const aiRes = await fetch(AIHOT_API, aiCtrl ? { signal: aiCtrl.signal } : {})
      .then(r => r.ok ? r.json() : null)
      .catch(() => null);
    if(aiTimer) clearTimeout(aiTimer);
    let items = (aiRes && aiRes.items) ? aiRes.items.slice() : [];
    // 空数据友好提示（凌晨/维护期间 AIHOT 可能返回 0 条，不应视为错误）
    if(!items.length){
      aihotLoaded = true;
      $('#aihotUpdated').textContent = '数据源：AIHOT 公开池';
      list.innerHTML = '<div style="color:var(--muted);font-size:13px;text-align:center;padding:16px 0">近 24 小时暂无新动态</div>';
      return;
    }
    aihotItems = items;
    aihotCursor = (aiRes.page && aiRes.page.hasMore) ? (aiRes.page.nextCursor || '') : '';
    renderAihot();
    _translateAihotItems();
    $('#aihotUpdated').textContent = '数据源：AIHOT 公开池 · 近 24 小时 · 更新于 ' + new Date().toLocaleTimeString('zh-CN', {hour12:false});
    aihotLoaded = true;

    // ② 新闻源（36/Redis）异步追加，不阻塞首屏；超时 12s 放弃
    const newsItems = await Promise.race([
      fetchNewsItems(),
      new Promise(resolve => setTimeout(() => resolve([]), 12000)),
    ]).catch(() => []);
    if(newsItems && newsItems.length){
      const seen = new Set(aihotItems.map(x => normTitle(x.title)));
      newsItems.forEach(n => { const k = normTitle(n.title); if(!seen.has(k)){ seen.add(k); aihotItems.push(n); } });
      aihotItems.sort((a, b) => (b.publishedAt || '').localeCompare(a.publishedAt || ''));
      renderAihot();
      _translateAihotItems();
      const extraSrcs = [...new Set(newsItems.map(n => n.source.name))];
      $('#aihotUpdated').textContent = '数据源：AIHOT 公开池 / ' + extraSrcs.join(' / ') + ' · 近 24 小时 · 更新于 ' + new Date().toLocaleTimeString('zh-CN', {hour12:false});
    }
  }catch(e){
    $('#aihotUpdated').textContent = '数据源：AIHOT 公开池';
    list.innerHTML = '<div style="color:var(--muted);font-size:13px;text-align:center;padding:16px 0">动态流加载失败，请检查网络后点击 AI 动态标签重试</div>';
  }finally{
    aihotLoading = false;
  }
}

// 30 分钟轮询：页面不可见时暂停（回调内检查 document.hidden），回到页面立即拉取一次
function startFeedPolling(){
  if(feedTimer) return;
  feedTimer = setInterval(() => {
    if(document.hidden) return;
    refreshFeed();
    if(aihotLoaded){ aihotLoaded = false; loadAihot(); }
  }, 30 * 60 * 1000);
  document.addEventListener('visibilitychange', () => {
    if(document.hidden) return;
    refreshFeed();
    if(state.sideTab === 'aihot'){ aihotLoaded = false; loadAihot(); }
  });
}
