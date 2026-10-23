import {
  API_BASE,
  CATS,
  DATA,
  DEFAULT_FAVS,
  FEED,
  LANG_COLORS,
  REFRESH_KEY,
  SEARCH_KEY,
  STORE_KEY,
  THEME_KEY,
  TRENDING,
  UPDATED,
  VIEW_KEY,
} from './boot.js';

export {
  API_BASE,
  CATS,
  DATA,
  DEFAULT_FAVS,
  LANG_COLORS,
  REFRESH_KEY,
  SEARCH_KEY,
  STORE_KEY,
  THEME_KEY,
  UPDATED,
  VIEW_KEY,
};

export const favs = new Set();
try {
  const raw = JSON.parse(localStorage.getItem(STORE_KEY) || 'null');
  const initial = Array.isArray(raw) ? raw : DEFAULT_FAVS;
  initial.forEach((fullName) => favs.add(fullName));
} catch {
  DEFAULT_FAVS.forEach((fullName) => favs.add(fullName));
}

export const POOL_NAMES = new Set(DATA.map((item) => item.full_name));

export const state = {
  q: '',
  cat: '',
  lang: '',
  star: 'all',
  sort: 'stars-desc',
  trendTab: 'rising',
  sideTab: 'trend',
  searchSort: 'best-match',
  searchLang: '',
  searchQuery: '',
  updated: '',
  topic: '',
  sortTouched: false,
  counted: false,
};

export const siteData = {
  TRENDING: TRENDING || {},
  FEED: FEED || [],
};

export const feedState = {
  updated: UPDATED,
  live: false,
  timer: null,
};
