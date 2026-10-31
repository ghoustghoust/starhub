import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

test('app.js 引用启动常量时必须显式导入', () => {
  const source = readFileSync(new URL('../assets/js/app.js', import.meta.url), 'utf8');
  const stateImport = source.match(/import\s*\{([^}]+)\}\s*from\s*'\.\/core\/state\.js'/);
  assert.ok(stateImport, 'state.js import is missing');
  const imported = new Set(stateImport[1].split(',').map(name => name.trim()));
  for (const key of ['THEME_KEY', 'VIEW_KEY', 'REFRESH_KEY']) {
    if (new RegExp(`\\b${key}\\b`).test(source)) {
      assert.ok(imported.has(key), `${key} is used but not imported from core/state.js`);
    }
  }
});

test('localStorage 写入失败时视图仍能在本次会话内往返', async () => {
  globalThis.window = { __STARHUB_BOOT__: { THEME_KEY: 't', VIEW_KEY: 'v' } };
  globalThis.localStorage = {
    getItem: () => null,
    setItem: () => { throw new Error('storage blocked'); },
    removeItem: () => {},
  };
  globalThis.document = {
    body: { classList: { toggle() {} } },
    querySelector: () => null,
    documentElement: { dataset: {} },
  };
  const { applyView, getView } = await import('../assets/js/features/preferences.js');
  applyView('list');
  assert.equal(getView(), 'list');
  applyView('card');
  assert.equal(getView(), 'card');
});

test('下载榜行渲染 Release 下载量徽标', async () => {
  globalThis.window = { __STARHUB_BOOT__: { TRENDING: {}, LANG_COLORS: {} } };
  globalThis.localStorage = {
    getItem: () => null,
    setItem: () => {},
    removeItem: () => {},
  };
  const { state } = await import('../assets/js/core/state.js');
  const { buildTrendItem } = await import('../assets/js/features/trending.js');
  state.trendTab = 'downloads';
  const html = buildTrendItem({
    html_url: 'https://github.com/a/one', owner: 'a', name: 'one', desc: 'd',
    language: 'Python', stars: 1200, downloads: 123456, reason: '累计下载 12.3万次',
  }, 0, false);
  assert.ok(html.includes('delta dl'), html);
  assert.ok(html.includes('⬇ 12.3万'), html);
  state.trendTab = 'rising';
});
