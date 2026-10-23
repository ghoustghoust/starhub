import { $ } from '../core/dom.js';
import { THEME_KEY, VIEW_KEY } from '../core/state.js';

export function getTheme() {
  try {
    return localStorage.getItem(THEME_KEY)
      || (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
  } catch {
    return 'light';
  }
}

export function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  const toggle = $('#btnTheme');
  if (toggle) toggle.setAttribute('aria-pressed', String(theme === 'dark'));
  const element = $('#themeIcon');
  if (!element) return;
  element.innerHTML = theme === 'dark'
    ? '<circle cx="12" cy="12" r="5"/><line x1="12" y1="1" x2="12" y2="3"/><line x1="12" y1="21" x2="12" y2="23"/><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/><line x1="1" y1="12" x2="3" y2="12"/><line x1="21" y1="12" x2="23" y2="12"/><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/>'
    : '<path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/>';
}

// 内存态优先：localStorage 被隐私模式/策略拦截时，写入会静默失败。
// 如果 getView 每次都回读存储，按钮会一直算出 card→list，表现为“切过去回不来”。
let currentView = null;

export function getView() {
  if (currentView === 'list' || currentView === 'card') return currentView;
  try {
    currentView = localStorage.getItem(VIEW_KEY) === 'list' ? 'list' : 'card';
  } catch {
    currentView = 'card';
  }
  return currentView;
}

export function applyView(view) {
  currentView = view === 'list' ? 'list' : 'card';
  document.body.classList.toggle('view-list', currentView === 'list');
  const toggle = $('#btnView');
  if (toggle) toggle.setAttribute('aria-pressed', String(currentView === 'list'));
  const element = $('#viewIcon');
  if (!element) return;
  element.innerHTML = currentView === 'list'
    ? '<rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/>'
    : '<line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/><line x1="8" y1="18" x2="21" y2="18"/><line x1="3" y1="6" x2="3.01" y2="6"/><line x1="3" y1="12" x2="3.01" y2="12"/><line x1="3" y1="18" x2="3.01" y2="18"/>';
}
