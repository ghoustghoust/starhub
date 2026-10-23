import test from 'node:test';
import assert from 'node:assert/strict';
import { buildQueryPlan, collectHighlightTerms, parseSearchParams, spamReason, tokenizeSearchQuery } from '../api/search.js';
import { applyCors, setPublicCache } from '../api/_lib/http.js';
import { normText, parseQueryTokens, relevanceScore } from '../assets/js/core/query.js';

test('限定符与引号短语不会被当成关键词', () => {
  const tokens = tokenizeSearchQuery('topic:"machine learning" -视频 stars:>1k');
  assert.deepEqual(tokens.map(t => [t.kind, t.text || t.raw]), [
    ['qualifier', 'topic:"machine learning"'],
    ['word', '视频'],
    ['qualifier', 'stars:>1k'],
  ]);
  assert.equal(tokens[1].negated, true);
  const negatedPhrase = tokenizeSearchQuery('-"machine learning"')[0];
  assert.equal(negatedPhrase.kind, 'word');
  assert.equal(negatedPhrase.text, 'machine learning');
  assert.equal(negatedPhrase.negated, true);
});

test('单中文词生成中英 OR 查询', () => {
  const plans = buildQueryPlan('视频创作', new Map([['视频创作', 'video creation']]));
  assert.equal(plans[0], '视频创作 OR "video creation"');
  assert.ok(plans.includes('"video creation"'));
});

test('限定符在每一档查询中原样保留', () => {
  const plans = buildQueryPlan('视频 language:Python stars:>100', new Map([['视频', 'video']]));
  assert.ok(plans.length >= 2);
  for (const plan of plans) {
    assert.ok(plan.includes('language:Python stars:>100'), plan);
    assert.ok(!plan.includes('Python:') && !plan.includes('翻译'), plan);
  }
  assert.equal(plans[0], '视频 OR video language:Python stars:>100');
});

test('多词查询包含 AND、短语与 OR 回退', () => {
  const plans = buildQueryPlan('视频 生成', new Map([['视频', 'video'], ['生成', 'generation']]));
  assert.ok(plans.includes('video generation'));
  assert.ok(plans.includes('"视频 生成" OR "video generation"'));
  assert.ok(plans.includes('video OR generation'));
});

test('负数词不被翻译，避免否定语义漂移', () => {
  const plans = buildQueryPlan('视频 -广告', new Map([['视频', 'video'], ['广告', 'ad']]));
  assert.ok(plans.some(p => p.includes('-广告')));
  assert.ok(!plans.some(p => p.includes('-ad')));
});

test('纯限定符与引号短语查询保持原样', () => {
  assert.deepEqual(buildQueryPlan('language:Python stars:>100'), ['language:Python stars:>100']);
  assert.deepEqual(buildQueryPlan('"machine learning"'), ['"machine learning"']);
});

test('查询计划去重', () => {
  const plans = buildQueryPlan('video generation');
  assert.equal(new Set(plans).size, plans.length);
});

test('高亮词包含原文与译文，排除限定符和否定词', () => {
  const terms = collectHighlightTerms('视频 -广告 language:Python', new Map([['视频', 'video'], ['广告', 'ad']]));
  assert.deepEqual(terms, ['视频', 'video']);
});

test('GET 查询参数解析', () => {
  const params = parseSearchParams('/api/search?q=%E8%A7%86%E9%A2%91&sort=stars&lang=Python&page=2');
  assert.deepEqual(params, { q: '视频', sort: 'stars', lang: 'Python', page: '2' });
  assert.equal(parseSearchParams('/api/search').page, 1);
});

test('超长描述仍判为垃圾结果', () => {
  assert.equal(spamReason({ description: 'x'.repeat(401) }), 'desc_len');
  assert.equal(spamReason({ description: 'x'.repeat(400) }), null);
});

test('本地引号短语作为整体参与匹配', () => {
  const { filters, words } = parseQueryTokens('"machine learning" stars:>1000 in:name');
  assert.deepEqual(words, ['machine learning']);
  assert.equal(filters.starsMin, 1000);
  assert.equal(filters.inName, true);
});

test('连字符与空格等价', () => {
  assert.equal(normText('Machine-Learning_Tool.js'), 'machine learning tool js');
  const repo = { name: 'machine-learning', owner: 'x', full_name: 'x/machine-learning', desc: '', topics: [], language: 'Python' };
  const descOnly = { name: 'other', owner: 'y', full_name: 'y/other', desc: 'machine learning toolkit', topics: [], language: 'Python' };
  assert.ok(relevanceScore(repo, ['machine learning']) > relevanceScore(descOnly, ['machine learning']));
  assert.equal(relevanceScore(repo, ['machine-learning']), relevanceScore(repo, ['machine learning']));
});

test('本地相关度：精确名 > 前缀 > 子串', () => {
  const base = { owner: 'x', desc: 'ui', topics: [], language: 'JavaScript' };
  const exact = relevanceScore({ ...base, name: 'react', full_name: 'facebook/react' }, ['react']);
  const prefix = relevanceScore({ ...base, name: 'reactive', full_name: 'x/reactive' }, ['react']);
  const substring = relevanceScore({ ...base, name: 'preact', full_name: 'x/preact' }, ['react']);
  assert.ok(exact > prefix);
  assert.ok(prefix > substring);
});

function fakeRes() {
  return { headers: {}, setHeader(key, value) { this.headers[key] = value; } };
}

test('公开只读响应允许任意 Origin，并可切换为 CDN 缓存', () => {
  const res = fakeRes();
  const allowed = applyCors({ headers: { origin: 'https://example.com' } }, res, { publicCors: true });
  assert.equal(allowed, true);
  assert.equal(res.headers['Access-Control-Allow-Origin'], '*');
  assert.equal(res.headers['Cache-Control'], 'no-store');
  setPublicCache(res, 600, 60);
  assert.equal(res.headers['Cache-Control'], 'public, s-maxage=600, stale-while-revalidate=60');
});

test('私有响应仍只回显白名单 Origin', () => {
  const res = fakeRes();
  const allowed = applyCors({ headers: { origin: 'https://ghoustghoust.github.io' } }, res);
  assert.equal(allowed, true);
  assert.equal(res.headers['Access-Control-Allow-Origin'], 'https://ghoustghoust.github.io');
  assert.equal(res.headers['Cache-Control'], 'no-store');
});
