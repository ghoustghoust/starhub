import { API_BASE, UPDATED, feedState, siteData } from '../core/state.js';
import { $, icon } from '../core/dom.js';
import { escHtml } from '../core/format.js';
import { renderTrending } from './trending.js';

const EVENTS_API = API_BASE + '/api/events?v=' + encodeURIComponent(UPDATED || 'dev');

/* Following activity feed. */
// ---- 近 24 小时关注动态 ----
const FEED_COLORS = { repo:'#0969da', star:'#e3a008', follow:'#8250df', pr:'#1a7f37', release:'#bf3989', public:'#656d76', push:'#d73a49' };
export function renderFeed(){
  const list = $('#feedList');
  const items = siteData.FEED || [];
  const isSelf = feedState.scope === 'self';
  const title = $('#feedTitle');
  if(title) title.textContent = isSelf ? '我的最近公开动态' : '近 24 小时关注动态';
  const sideTab = $('.side-tab[data-side="feed"]');
  if(sideTab) sideTab.textContent = isSelf ? '我的动态' : '关注动态';
  $('#feedUpdated').textContent = (isSelf
    ? '你还没有关注任何人 · 以下是你的最近公开动态 · '
    : '近 24 小时滚动窗口 · ') + (feedState.live ? '实时更新于 ' : '更新于 ') + feedState.updated;
  list.innerHTML = '';
  if(!items.length){
    const follow = (isSelf && feedState.owner)
      ? ' <a href="https://github.com/' + encodeURIComponent(feedState.owner) + '?tab=following" target="_blank" rel="noopener" style="color:var(--brand)">去 GitHub 关注</a>'
      : '';
    list.innerHTML = '<div style="color:var(--muted);font-size:13px;text-align:center;padding:16px 0">' +
      (isSelf ? '你还没有关注任何人，近 24 小时也没有公开动态。' : '近 24 小时暂无关注动态。') + follow + '</div>';
    return;
  }
  items.forEach(f => {
    const color = FEED_COLORS[f.kind] || '#8b949e';
    let verb = '', target = '';
    if(f.kind === 'repo'){ verb = '新建了仓库'; target = f.repo; }
    else if(f.kind === 'star'){ verb = 'star 了'; target = f.repo; }
    else if(f.kind === 'follow'){ verb = '关注了'; target = f.target; }
    else if(f.kind === 'pr'){ verb = '提交了 PR'; target = f.title || f.repo; }
    else if(f.kind === 'release'){ verb = '发布了 release'; target = f.tag || f.repo; }
    else if(f.kind === 'public'){ verb = '公开了仓库'; target = f.repo; }
    else if(f.kind === 'push'){ verb = '推送了提交'; target = f.repo + ' (' + f.size + ' 个)'; }
    /* 类型视觉：star 用金色星形，其余用类型色圆点 + 同色光晕 */
    const fd = f.kind === 'star'
      ? '<span class="fd" style="color:'+color+'">'+icon.star+'</span>'
      : '<span class="fd" style="background:'+color+';box-shadow:0 0 0 4px '+color+'33"></span>';
    const row = document.createElement('div');
    row.className = 'feed-item';
    row.innerHTML =
      fd +
      '<span class="fa">'+escHtml(f.actor)+'</span>' +
      '<span class="fv">'+verb+'</span>' +
      '<a class="ft" href="'+escHtml(f.url||'')+'" target="_blank" rel="noopener">'+escHtml(target)+'</a>' +
      '<span class="tm">'+escHtml((f.day ? f.day+' ' : '') + f.time)+'</span>';
    list.appendChild(row);
  });
}

// 实时拉取关注动态（Vercel 代理聚合）：成功替换 siteData.FEED 重渲染，失败静默保持静态数据
export async function refreshFeed(){
  try{
    const r = await fetch(EVENTS_API);
    if(!r.ok) return;
    const j = await r.json();
    if(!j || !Array.isArray(j.items)) return;
    siteData.FEED = j.items;
    feedState.updated = j.updated_at || '';
    feedState.scope = j.scope === 'self' ? 'self' : 'following';
    feedState.followingCount = typeof j.following_count === 'number' ? j.following_count : null;
    feedState.owner = j.owner || '';
    feedState.live = true;
    renderFeed();
  }catch(e){ /* 静默降级：网络异常时保持静态数据 */ }
}

// 排行榜数据出口（同源站点件，由小时场写）：成功替换 siteData.TRENDING 重渲染，失败静默保留内联兜底。
// 为什么必须有它：index.html 是单体文件，快车道每 15 分钟重建整页时并不拥有排行榜
// （它只算 star 列表），拿不到就把内联值写成 {} ⇒ 2026-10-04 18:01 线上空榜的根因。
// 有了这条注入路径，页面里那份内联就只是首屏兜底，覆盖不再伤人。
export async function refreshTrendingBoard(){
  try{
    const r = await fetch('trending_board.json', {cache:'no-store'});
    if(!r.ok) return;
    const j = await r.json();
    if(!j || !j.trending) return;
    siteData.TRENDING = j.trending;
    const slot = document.getElementById('aiSummarySlot');
    if(slot && j.ai_summary_html) slot.innerHTML = j.ai_summary_html;
    renderTrending();
  }catch(e){ /* 静默降级：取不到就继续用内联那份，不许把榜单清空 */ }
}
