import { CATS, state } from './state.js';

const LOCAL_KEYS = ['cat', 'lang', 'star', 'sort', 'updated', 'topic'];
const GLOBAL_KEYS = ['glang', 'gsort'];
const SORTS = new Set(['stars-desc', 'stars-asc', 'relevant', 'starred', 'recent', 'name']);
const GLOBAL_SORTS = new Set(['best-match', 'stars', 'updated']);
const STAR_RANGES = new Set(['all', 'lt1k', '1k10k', '10k100k', 'gt100k']);
const UPDATES = new Set(['week', 'month']);

export function hydrateSearchState() {
  const params = new URLSearchParams(location.search);
  state.searchMode = params.get('mode') === 'global' ? 'global' : 'local';
  const query = params.get('q') || '';
  state.q = query;
  state.searchQuery = state.searchMode === 'global' ? query : '';

  const category = params.get('cat') || '';
  state.cat = CATS.some((item) => item.key === category) ? category : '';
  state.lang = params.get('lang') || '';
  const stars = params.get('star');
  state.star = STAR_RANGES.has(stars) ? stars : 'all';
  const sort = params.get('sort');
  state.sort = SORTS.has(sort) ? sort : 'stars-desc';
  state.sortTouched = params.has('sort');
  const updated = params.get('updated');
  state.updated = UPDATES.has(updated) ? updated : '';
  state.topic = params.get('topic') || '';

  state.searchLang = params.get('glang') || '';
  const globalSort = params.get('gsort');
  state.searchSort = GLOBAL_SORTS.has(globalSort) ? globalSort : 'best-match';
}

export function syncSearchUrl() {
  const params = new URLSearchParams(location.search);
  for (const key of ['q', 'mode', ...LOCAL_KEYS, ...GLOBAL_KEYS]) params.delete(key);

  const query = state.q.trim();
  if (query) params.set('q', query);
  if (state.searchMode === 'global') params.set('mode', 'global');

  if (state.cat) params.set('cat', state.cat);
  if (state.lang) params.set('lang', state.lang);
  if (state.star && state.star !== 'all') params.set('star', state.star);
  if (state.sort && state.sort !== 'stars-desc') params.set('sort', state.sort);
  if (state.updated) params.set('updated', state.updated);
  if (state.topic) params.set('topic', state.topic);

  if (state.searchLang) params.set('glang', state.searchLang);
  if (state.searchSort && state.searchSort !== 'best-match') params.set('gsort', state.searchSort);

  const qs = params.toString();
  history.replaceState(null, '', location.pathname + (qs ? '?' + qs : '') + location.hash);
}
