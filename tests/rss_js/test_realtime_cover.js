// tests/rss_js/test_realtime_cover.js —— 实时链路封面抽取（lib/rss_cover.js）的自测
// 跑法：node tests/rss_js/test_realtime_cover.js（退出码非 0 即失败）
// CI 里由 tests/rss_cover/test_realtime_cover_js.py 调用，从而进 A2 blocking 闸。
// 优先级必须与构建期 _pick_item_image 一致：enclosure > media:content > media:thumbnail > 正文首图 > 描述首图。
'use strict';

const assert = require('assert');
const path = require('path');
// 变异体注入点：与本仓 STARHUB_UPDATE_YML / RSS_BUILD_SRC 同一约定，让变异只改副本
const COVER_LIB = process.env.STARHUB_COVER_LIB
  || path.join(__dirname, '..', '..', 'lib', 'rss_cover.js');
const C = require(COVER_LIB);

const cases = [];
function test(name, fn) { cases.push([name, fn]); }

test('enclosure with image type wins', () => {
  const e = '<item><enclosure url="https://a/x.jpg" type="image/jpeg" length="1"/>'
    + '<media:content url="https://m/later.jpg" medium="image"/></item>';
  assert.strictEqual(C.pickItemImage(e, '', ''), 'https://a/x.jpg');
});

test('enclosure without type is accepted by extension', () => {
  const e = '<item><enclosure url="https://a/x.png?w=640" type=""/></item>';
  assert.strictEqual(C.pickItemImage(e, '', ''), 'https://a/x.png?w=640');
});

test('audio enclosure is not a cover, media:content is', () => {
  const e = '<item><enclosure url="https://a/s.mp3" type="audio/mpeg"/>'
    + '<media:content url="https://m/c.jpg" medium="image"/></item>';
  assert.strictEqual(C.pickItemImage(e, '', ''), 'https://m/c.jpg');
});

test('media:content video falls through to media:thumbnail', () => {
  const e = '<item><media:content url="https://m/a.mp4" medium="video"/>'
    + '<media:thumbnail url="https://m/t.jpg"/></item>';
  assert.strictEqual(C.pickItemImage(e, '', ''), 'https://m/t.jpg');
});

test('media:content without medium counts as image', () => {
  const e = '<item><media:content url="https://m/c.jpg"/></item>';
  assert.strictEqual(C.pickItemImage(e, '', ''), 'https://m/c.jpg');
});

test('m: prefix variant is recognized', () => {
  const e = '<item><m:content url="https://m/c.jpg" type="photo"/></item>';
  assert.strictEqual(C.pickItemImage(e, '', ''), 'https://m/c.jpg');
});

test('content first img beats desc img', () => {
  const e = '<item></item>';
  const got = C.pickItemImage(e, '<p><img src="https://c/1.jpg"></p>', '<img src="https://d/2.jpg">');
  assert.strictEqual(got, 'https://c/1.jpg');
});

test('desc img used when content has none', () => {
  const got = C.pickItemImage('<item></item>', '<p>no img</p>', '<img alt="x" src="https://d/2.webp">');
  assert.strictEqual(got, 'https://d/2.webp');
});

test('&amp; inside src is decoded, not truncated', () => {
  const got = C.firstImgSrc('<img src="https://c/1.jpg?a=1&amp;b=2">');
  assert.strictEqual(got, 'https://c/1.jpg?a=1&b=2');
});

test('entity-escaped description still yields a cover', () => {
  // 主流 feed 的 description 是转义过的 HTML；不先解锁死就是实时刷新后封面消失
  const esc = '&lt;p&gt;&lt;img src="https://c.test/2.png"&gt;&lt;/p&gt;';
  assert.strictEqual(C.firstImgSrc(esc), 'https://c.test/2.png');
  assert.strictEqual(C.pickItemImage('<item></item>', '', esc), 'https://c.test/2.png');
});

test('double-escaped markup is not promoted to real tags', () => {
  // &amp;lt; 一次解码后仍是 &lt; 文本，不该被当成 <img> 标签
  assert.strictEqual(C.firstImgSrc('&amp;lt;img src="https://x/1.jpg"&amp;gt;'), '');
});

test('relative and protocol-less src are rejected', () => {
  assert.strictEqual(C.firstImgSrc('<img src="/local.png">'), '');
  assert.strictEqual(C.firstImgSrc('<img src="//cdn/x.png">'), '');
  assert.strictEqual(C.firstImgSrc('<img src="data:image/png;base64,AA">'), '');
});

test('no cover at all returns empty string', () => {
  assert.strictEqual(C.pickItemImage('<item><title>t</title></item>', '', ''), '');
  assert.strictEqual(C.pickItemImage('', '', ''), '');
});

// 反空跑：这套判据必须真的在跑东西（样本数、命中数都非零），否则全绿也什么都不是
test('harness actually ran a non-empty case list', () => {
  assert.ok(cases.length >= 11, '用例数异常：%d' % cases.length);
});

let failed = 0;
for (const [name, fn] of cases) {
  try {
    fn();
    console.log('ok   ' + name);
  } catch (e) {
    failed++;
    console.error('FAIL ' + name + ' :: ' + (e && e.message));
  }
}
console.log(failed ? '\n' + failed + ' 个用例失败' : '\n全部 ' + cases.length + ' 个用例通过');
process.exit(failed ? 1 : 0);
