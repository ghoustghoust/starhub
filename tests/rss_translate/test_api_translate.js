// tests/rss_translate/test_api_translate.js
// 服务端翻译降级链集成测试
// Run: node --test tests/rss_translate/test_api_translate.js

import { describe, it } from 'node:test';
import assert from 'node:assert/strict';

// 设置环境变量（必须在 import 模块之前）
process.env.AGNES_API_KEY = 'test-key-1';
process.env.AGNES_API_KEYS = 'test-key-2';

// ── 辅助：构造 mock fetch 响应 ──
function agnesResp(text) {
  return { ok: true, status: 200, json: () => Promise.resolve({
    choices: [{ message: { content: text } }]
  })};
}
function zenResp(text) {
  return { ok: true, status: 200, json: () => Promise.resolve({
    choices: [{ message: { content: text } }]
  })};
}
function mymemoryResp(text) {
  return { ok: true, status: 200, json: () => Promise.resolve({
    responseData: { translatedText: text }
  })};
}
function errResp(status) {
  return { ok: false, status, json: () => Promise.resolve({ error: 'mock' }) };
}

// ── mock req/res ──
function mockReq(body, origin = 'https://kwei168.github.io') {
  return {
    method: 'POST',
    headers: { origin, 'x-real-ip': '1.2.3.4' },
    body,
  };
}
function mockRes() {
  const res = {
    _status: 200, _body: null, _headers: {},
    status(s) { res._status = s; return res; },
    json(b) { res._body = b; return res; },
    setHeader(k, v) { res._headers[k] = v; },
    end() {},
  };
  return res;
}

describe('api/translate.js 降级链', () => {

  // 每个 test 独立设置 mock fetch 后再动态导入模块
  // 确保模块加载时 fetch 已被替换

  it('多 Agnes key 轮询：key1 返回 429 时自动切换到 key2', async () => {
    const calls = [];
    global.fetch = async (url, opts) => {
      if (String(url).includes('agnes-ai')) {
        const key = opts.headers.Authorization.replace('Bearer ', '');
        calls.push(key);
        if (key === 'test-key-1') return errResp(429);
        return agnesResp('译文 from key2');
      }
      return errResp(500);
    };
    const url = new URL('../../api/translate.js', import.meta.url);
    url.searchParams.set('_t', 'mk1');
    const { default: handler } = await import(url.href);
    const res = mockRes();
    await handler(mockReq({ texts: ['hello world'], mode: 'full' }), res);
    assert.equal(res._status, 200, 'status should be 200');
    assert.equal(res._body.ok, true);
    assert.equal(res._body.translations[0], '译文 from key2');
    assert.ok(calls.includes('test-key-1'), 'key1 被尝试');
    assert.ok(calls.includes('test-key-2'), 'key2 被尝试');
  });

  it('Agnes 失败后降级到 Zen', async () => {
    global.fetch = async (url) => {
      if (String(url).includes('agnes-ai')) return errResp(429);
      if (String(url).includes('opencode')) return zenResp('Zen 译文');
      return errResp(500);
    };
    const url = new URL('../../api/translate.js', import.meta.url);
    url.searchParams.set('_t', 'zen1');
    const { default: handler } = await import(url.href);
    const res = mockRes();
    await handler(mockReq({ texts: ['test text zen'], mode: 'full' }), res);
    assert.equal(res._status, 200);
    assert.equal(res._body.translations[0], 'Zen 译文');
    assert.equal(res._body.engine, 'zen');
  });

  it('GTX+Agnes+Zen 失败后降级到 MyMemory', async () => {
    global.fetch = async (url) => {
      if (String(url).includes('agnes-ai') || String(url).includes('opencode')) return errResp(500);
      if (String(url).includes('googleapis')) return errResp(429);
      if (String(url).includes('mymemory')) return mymemoryResp('MyMemory 译文');
      return errResp(500);
    };
    const url = new URL('../../api/translate.js', import.meta.url);
    url.searchParams.set('_t', 'mm1');
    const { default: handler } = await import(url.href);
    const res = mockRes();
    await handler(mockReq({ texts: ['test fallback mm'], mode: 'full' }), res);
    assert.equal(res._status, 200);
    assert.equal(res._body.translations[0], 'MyMemory 译文');
    assert.equal(res._body.engine, 'mymemory');
  });

  it('全端点失败返回 502', async () => {
    global.fetch = async () => errResp(500);
    const url = new URL('../../api/translate.js', import.meta.url);
    url.searchParams.set('_t', 'fail1');
    const { default: handler } = await import(url.href);
    const res = mockRes();
    await handler(mockReq({ texts: ['fail test xyz'], mode: 'full' }), res);
    assert.equal(res._status, 502);
  });

  it('缓存命中不调用上游', async () => {
    let callCount = 0;
    global.fetch = async (url) => {
      callCount++;
      if (String(url).includes('agnes-ai')) return agnesResp('缓存测试译文');
      return errResp(500);
    };
    const url = new URL('../../api/translate.js', import.meta.url);
    url.searchParams.set('_t', 'cache1');
    const { default: handler } = await import(url.href);
    const res1 = mockRes();
    await handler(mockReq({ texts: ['cache test unique'], mode: 'full' }), res1);
    assert.equal(res1._status, 200);
    const firstCallCount = callCount;
    const res2 = mockRes();
    await handler(mockReq({ texts: ['cache test unique'], mode: 'full' }), res2);
    assert.equal(res2._status, 200);
    assert.equal(res2._body.translations[0], '缓存测试译文');
    assert.equal(callCount, firstCallCount, '缓存命中后无额外上游调用');
  });
});
