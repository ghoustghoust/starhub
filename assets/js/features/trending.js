import { LANG_COLORS, UPDATED, siteData, state } from '../core/state.js';
import { $ } from '../core/dom.js';
import { escHtml, fmtStars } from '../core/format.js';

/* Daily trending board. */
// ---- 每日 AI 排行榜 ----
export function buildTrendItem(p, i, full){
  const hasDelta = p.delta !== null && p.delta !== undefined;
  const deltaHtml = state.trendTab === 'rising'
    ? (hasDelta
        ? (p.delta > 0
            ? '<span class="delta">▲ +' + p.delta + '</span>'
            : (p.delta === 0
                ? '<span class="delta flat">➖</span>'
                : '<span class="delta neg">▼ ' + Math.abs(p.delta) + '</span>'))
        : '<span class="delta new">新</span>')
    : '';
  const rank = '<span class="rank">' + (i+1) + '</span>';
  const name = '<a class="tn" href="' + escHtml(p.html_url) + '" title="' + escHtml(p.owner) + '/' + escHtml(p.name) + '" target="_blank" rel="noopener">' + escHtml(p.owner) + '/' + escHtml(p.name) + '</a>';
  const lang = p.language
    ? '<span class="lg"><span class="c" style="background:' + (LANG_COLORS[p.language] || '#8b949e') + '"></span>' + escHtml(p.language) + '</span>'
    : '';
  const stars = '<span class="stars">★ ' + fmtStars(p.stars) + '</span>';
  const reason = '<span class="reason">' + escHtml(p.reason || '') + '</span>';
  if(full){
    return rank + '<div class="ti">' + name +
      '<span class="td" title="' + escHtml(p.desc || '暂无简介') + '">' + escHtml(p.desc || '暂无简介') + '</span>' + reason +
      '</div>' + '<div class="tm">' + deltaHtml + stars + '</div>';
  }
  return rank + '<div class="ti">' + name +
    '<span class="meta">' + lang + stars + '</span>' +
    '</div>' + '<div class="tm">' + deltaHtml + '</div>';
}

export function renderTrending(){
  const list = $('#trendList');
  const items = (siteData.TRENDING && siteData.TRENDING[state.trendTab]) || [];
  const box = $('.trending'); if(box) box.setAttribute('data-tab', state.trendTab);
  let sub = '更新于 ' + UPDATED;
  if(state.trendTab === 'rising'){
    const src = siteData.TRENDING && siteData.TRENDING.source;
    sub += src === 'trending' ? ' · GitHub Trending 今日涨星' : (src === 'snapshot' ? ' · 快照差值' : '');
  } else {
    sub += ' · Star 池统计';
  }
  $('#trendUpdated').textContent = sub;
  list.innerHTML = '';
  if(!items.length){
    const msg = state.trendTab === 'rising'
      ? '首次运行正在建立基线，明天起显示每日涨星变化'
      : '暂无数据';
    list.innerHTML = '<div style="color:var(--muted);font-size:13px;text-align:center;padding:24px 0">'+msg+'</div>';
    return;
  }
  items.slice(0, 10).forEach((p, i) => {
    const row = document.createElement('div');
    row.className = 'trend-item';
    row.innerHTML = buildTrendItem(p, i, false);
    list.appendChild(row);
  });
}

export function switchTrendTab(tab, scope){
  state.trendTab = tab;
  document.querySelectorAll((scope || '#trendTabs') + ' .trend-tab').forEach(x=>{
    const on = x.dataset.tab === tab;
    x.classList.toggle('on', on);
    x.setAttribute('aria-pressed', String(on));
  });
  renderTrending();
}


export function renderTrendingFull(){
  const list = $('#mTrendList');
  const items = (siteData.TRENDING && siteData.TRENDING[state.trendTab]) || [];
  list.innerHTML = '';
  if(!items.length){
    list.innerHTML = '<div style="color:var(--muted);font-size:13px;text-align:center;padding:40px 0">暂无数据</div>';
    return;
  }
  items.slice(0, 20).forEach((p, i) => {
    const row = document.createElement('div');
    row.className = 'trend-item';
    row.innerHTML = buildTrendItem(p, i, true);
    list.appendChild(row);
  });
}

export function openTrendModal(){
  $('#trendModal').classList.add('open');
  document.querySelectorAll('#mTrendTabs .trend-tab').forEach(x=>x.classList.toggle('on', x.dataset.tab===state.trendTab));
  renderTrendingFull();
}

export function closeTrendModal(){ $('#trendModal').classList.remove('open'); }
