/* Shared boot state and API constants. */
// 实时关注动态：Vercel 代理聚合 GitHub Events API（近 24 小时），打开页面即拉取 + 每 30 分钟轮询
const EVENTS_API = API_BASE + '/api/events';
// AI 动态流：AIHOT 公开 API v1（匿名只读、CORS 全开）+ 36氪/Redis 等资讯流（经 /api/news 代理，
// 官方 RSS 有反爬、RSSHub 镜像 CORS 不合法故需服务端中转）；浏览器直连，切到该 Tab 时懒加载 + 30 分钟轮询
const AIHOT_API = 'https://aihot.virxact.com/api/v1/items?mode=all&window=24h&limit=40';
const NEWS_API = API_BASE + '/api/news';
const AIHOT_CATS = { 'ai-models':['AI 模型','#2563eb'], 'ai-products':['AI 产品','#7c3aed'], industry:['行业动态','#0891b2'], paper:['论文','#d97706'], tip:['技巧观点','#dc2626'] };
let feedUpdated = UPDATED;
let feedLive = false;
let feedTimer = null;

// ---- state ----
let favs = new Set();
try {
  const raw = JSON.parse(localStorage.getItem(STORE_KEY) || 'null');
  // 仅接受数组：JSON 字符串等非法类型会污染收藏集合并被写回覆盖，不可逆丢失
  favs = new Set(Array.isArray(raw) ? raw : DEFAULT_FAVS);
}
catch(e){ favs = new Set(DEFAULT_FAVS); }

// 搜索结果里的「✓ 已收藏」不能只认 favs：那份存在 localStorage，换浏览器、清缓存、
// 换域（站点产物 10-04 从 Vercel 域收口到 Pages 域，而 localStorage 按域隔离）之后标记整片消失，
// 且用户没法恢复。"这个仓库我已经 star 过"这件事本来就在构建期数据里 ⇒ 用它兜住。
const POOL_NAMES = new Set(DATA.map(d => d.full_name));

const state = { q:'', cat:'', lang:'', star:'all', sort:'stars-desc', trendTab:'rising', sideTab:'trend', searchSort:'best-match', searchLang:'', searchQuery:'', updated:'', topic:'', sortTouched:false };
