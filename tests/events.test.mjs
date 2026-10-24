import test from 'node:test';
import assert from 'node:assert/strict';
import { resolveEventScope } from '../api/events.js';

test('有关注时展示关注动态', () => {
  assert.deepEqual(resolveEventScope(['alice', 'bob'], 'me'), {
    scope: 'following',
    users: ['alice', 'bob'],
    followingCount: 2,
  });
});

test('关注为空时回退到账号自己的公开动态', () => {
  assert.deepEqual(resolveEventScope([], 'me'), {
    scope: 'self',
    users: ['me'],
    followingCount: 0,
  });
});

test('没有 owner 时不伪造数据源', () => {
  assert.deepEqual(resolveEventScope([], ''), {
    scope: 'following',
    users: [],
    followingCount: 0,
  });
});
