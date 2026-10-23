const boot = window.__STARHUB_BOOT__ || {};

export const DATA = boot.DATA || [];
export const CATS = boot.CATS || [];
export const LANG_COLORS = boot.LANG_COLORS || {};
export const DEFAULT_FAVS = boot.DEFAULT_FAVS || [];
export const STORE_KEY = boot.STORE_KEY || 'wb_starhub_favs_v1';
export const THEME_KEY = boot.THEME_KEY || 'wb_starhub_theme_v1';
export const VIEW_KEY = boot.VIEW_KEY || 'wb_starhub_view_v1';
export const API_BASE = boot.API_BASE || '';
export const REFRESH_KEY = boot.REFRESH_KEY || '';
export const SEARCH_KEY = boot.SEARCH_KEY || REFRESH_KEY;
export const UPDATED = boot.UPDATED || '';
export const TRENDING = boot.TRENDING || {};
export const FEED = boot.FEED || [];

export default boot;
