// tests/rss_js/test_body_rules.js —— 运行时正文规范层（lib/body_rules.js）的自测
// 跑法：node tests/rss_js/test_body_rules.js（退出码 = 失败条数，非 0 即失败）
// CI 里由 tests/site_nav/test_body_rules_parity.py::test_js_self_test_suite_passes 用退出码承接
// —— `tests/rss_js/` 全是 .js，pytest 根本不收集，没有那条桥接这就是"写着但从不跑"。
//
// 零 npm 依赖：CI 上 node_modules 不存在（update.yml 只有 node --check），
// 一旦这里 require 任何包，判据会在 runner 上直接崩，等于没有判据。
//
// 这里只放**本地子集**（自测用）。跨语言对账的样本清单只有一份，住在
// tests/site_nav/test_body_rules_parity.py —— 两边各抄一份样本就是第 5 个分叉源。
// 下面硬编码的期望值全部取自 Python 侧现算结果（账本 R28：比行为不比字面）。
'use strict';

const path = require('path');
const fs = require('fs');
const assert = require('assert');
// 变异体注入点：与本仓 STARHUB_UPDATE_YML / RSS_BUILD_SRC 同一约定，让变异只改副本
const BODY_LIB = process.env.STARHUB_BODY_RULES || path.join(__dirname, '..', '..', 'lib', 'body_rules.js');
const R = require(BODY_LIB);

const cases = [];
function test(name, fn) { cases.push([name, fn]); }

// 程序化构造控制字符：上一批金标准样本想用 `"…\\u0026…"` 与 `"…\\1…"` 测模板注入，
// 结果被 shell / 字符串层各解一次，**没真正测到**（Task 1 的 C1 就是这么漏掉的）。
const BS = String.fromCharCode(92);   // \
const TAB = String.fromCharCode(9);   // \t
const FSX = String.fromCharCode(0x1c); // \x1c：Python 的 isspace 认、JS 的 \s 不认
const BOM = String.fromCharCode(0xfeff); // \ufeff：JS 的 \s 认、Python 不认
const NEL = String.fromCharCode(0x85);  // \x85：同上
const NBSP = String.fromCharCode(0xa0);
const ZWSP = String.fromCharCode(0x200b);
const CJK = String.fromCharCode(0x56fe); // 图：Python \w 认、JS 的 \w 不认
const ARABIC3 = String.fromCharCode(0x663); // ٣：Unicode 十进制数字，Python \d 认，本仓按 ASCII 走

const BASE = 'https://mp.weixin.qq.com/s/AbC123';

// ---------------------------------------------------------------- 懒加载提升
test('lazy promoted to src', function () {
  assert.strictEqual(R.normalizeBodyHtml('<p>x</p><img data-src="https://mmbiz.qpic.cn/a.jpg">', BASE),
    '<p>x</p><img src="https://mmbiz.qpic.cn/a.jpg" data-src="https://mmbiz.qpic.cn/a.jpg">');
});
test('placeholder src replaced', function () {
  assert.strictEqual(R.normalizeBodyHtml('<img src="data:image/gif;base64,R0" data-src="https://mmbiz.qpic.cn/b.jpg">', BASE),
    '<img src="https://mmbiz.qpic.cn/b.jpg" data-src="https://mmbiz.qpic.cn/b.jpg">');
});
test('real src never overwritten by mirror attr', function () {
  assert.strictEqual(R.normalizeBodyHtml('<img src="https://cdn.example/real.png" data-src="https://cdn.example/o.png">', BASE),
    '<img src="https://cdn.example/real.png" data-src="https://cdn.example/o.png">');
});
test('only first src attribute is rewritten', function () {
  assert.strictEqual(R.normalizeBodyHtml('<img src="data:,x" src="/b.png" data-src="L.png">', BASE),
    '<img src="https://mp.weixin.qq.com/s/L.png" src="https://mp.weixin.qq.com/b.png" data-src="L.png">');
});
test('data: in any case is a placeholder', function () {
  assert.strictEqual(R.normalizeBodyHtml('<img src="DATA:image/gif;base64,x" data-src="r.png">', BASE),
    '<img src="https://mp.weixin.qq.com/s/r.png" data-src="r.png">');
});
test('empty / whitespace-only mirror values are skipped', function () {
  assert.strictEqual(R.normalizeBodyHtml('<img data-src="" data-original="real.png">', BASE),
    '<img src="https://mp.weixin.qq.com/s/real.png" data-src="" data-original="real.png">');
  assert.strictEqual(R.normalizeBodyHtml('<img data-src="   " data-original="r.png">', BASE),
    '<img src="https://mp.weixin.qq.com/s/r.png" data-src="   " data-original="r.png">');
});
test('FS-only mirror value is skipped (Python strip 认 \\x1c，JS trim 不认)', function () {
  assert.strictEqual(R.normalizeBodyHtml('<img data-src="' + FSX + '" data-original="r.png">', BASE),
    '<img src="https://mp.weixin.qq.com/s/r.png" data-src="' + FSX + '" data-original="r.png">');
});
test('NBSP-only mirror value is skipped', function () {
  assert.strictEqual(R.normalizeBodyHtml('<img data-src="' + NBSP + '" data-original="r.png">', BASE),
    '<img src="https://mp.weixin.qq.com/s/r.png" data-src="' + NBSP + '" data-original="r.png">');
});
test('data-src value survives an embedded ampersand verbatim', function () {
  assert.strictEqual(R.normalizeBodyHtml('<img data-src="https://a&b.jpg" src="data:,">', BASE),
    '<img data-src="https://a&b.jpg" src="https://a&b.jpg">');
});
test('uppercase tag and attribute names keep their case', function () {
  assert.strictEqual(R.normalizeBodyHtml('<IMG SRC="/a.png" DATA-SRC="x">', BASE),
    '<IMG SRC="https://mp.weixin.qq.com/a.png" DATA-SRC="x">');
});
test('whitespace around = is normalized away only on rewritten attrs', function () {
  assert.strictEqual(R.normalizeBodyHtml('<img src = "/a.png">', BASE),
    '<img src="https://mp.weixin.qq.com/a.png">');
});
test('single-quoted attributes are not seen by either side', function () {
  assert.strictEqual(R.normalizeBodyHtml("<p title='q'>x</p><img data-src='r.jpg'>", BASE),
    "<p title='q'>x</p><img data-src='r.jpg'>");
});
test('a > inside an attribute value truncates the tag on both sides', function () {
  assert.strictEqual(R.normalizeBodyHtml('<img data-src="https://e.com/a>b.jpg" alt="x">', BASE),
    '<img data-src="https://e.com/a>b.jpg" alt="x">');
});
test('escaped bodies are NOT unescaped by this layer (I8 分层)', function () {
  const s = '&lt;img data-src=&quot;https://e.com/a.png&quot;&gt;';
  assert.strictEqual(R.normalizeBodyHtml(s, BASE), s);
});

// ---------------------------------------------------------------- 占位判定
test('placeholder stems match exactly, not as substrings', function () {
  const T = R.isPlaceholderSrc, yes = [], no = [];
  ['blank', 'blank.png', 'placeholder.png', 'spacer.png', 'spacer_1x1.png', '1x1.png',
   '0x0', '1x1', '0X0.png', '1X1', '0x0.jpeg?v=2', 'data:x', 'DATA:image/gif;base64,x',
   'https://e.com/blank.gif', 'https://img.example/blank.png#lazy', '0x0#blank', '_1x1_',
   '-0x0-', '1x1 ', ' 1x1', '0x0.0'].forEach(function (v) { if (!T(v)) yes.push(v); });
  ['blank2.gif', 'a.blank', 'lazy_cat_pic.png', '/0x0/', 'lazy/640x', 'x1x1', '1x0',
   'https://e.com/f/1024p0/0x0/100/New_Project_76__0.jpg',
   'https://mmbiz.qpic.cn/mmbiz_jpg/abc1x1def/960.jpg',
   'https://img.example/x?name=blank.png', 'https://s.example/a.jpg?w=1x1',
   'not-a-url-with-no-slash-blank', 'https://e.com/lazy/', ''].forEach(function (v) { if (T(v)) no.push(v); });
  assert.deepStrictEqual(yes, [], '应判占位却漏了：' + JSON.stringify(yes));
  assert.deepStrictEqual(no, [], '误把真图判成占位：' + JSON.stringify(no));
});

// ---------------------------------------------------------------- 绝对化
test('relative absolutized, anchors and protocol-relative untouched', function () {
  assert.strictEqual(R.normalizeBodyHtml('<a href="../b.html">t</a>', BASE),
    '<a href="https://mp.weixin.qq.com/b.html">t</a>');
  assert.strictEqual(R.normalizeBodyHtml('<a href="../up/../b?q=1">t</a>', 'https://blog.example/dir/post.html'),
    '<a href="https://blog.example/b?q=1">t</a>');
  assert.strictEqual(R.normalizeBodyHtml('<a href="#sec">t</a>', BASE), '<a href="#sec">t</a>');
  assert.strictEqual(R.normalizeBodyHtml('<img src="//cdn.example/c.png"><a href="#sec">s</a>', BASE),
    '<img src="//cdn.example/c.png"><a href="#sec">s</a>');
  assert.strictEqual(R.normalizeBodyHtml('<a href="HTTPS://EX.COM/A">t</a>', BASE), '<a href="HTTPS://EX.COM/A">t</a>');
});
test('no base leaves the path alone', function () {
  assert.strictEqual(R.normalizeBodyHtml('<img src="/i/a.png">', ''), '<img src="/i/a.png">');
  assert.strictEqual(R.normalizeBodyHtml('<img src="/i/a.png">', 'ftp://e.com/x'), '<img src="/i/a.png">');
  // M4：base 里带裸引号 ⇒ 按"没有 base"处理，宁可 404 也不要出厂变形
  assert.strictEqual(R.normalizeBodyHtml('<img src="/a.png">', 'https://e.com/d"q/x'), '<img src="/a.png">');
  assert.strictEqual(R.normalizeBodyHtml('', BASE), '');
});
test('base with a leading FS is stripped the Python way', function () {
  assert.strictEqual(R.normalizeBodyHtml('<img src="/a.png">', FSX + BASE),
    '<img src="https://mp.weixin.qq.com/a.png">');
});
test('space in a path is NOT percent-encoded (new URL 会编成 %20)', function () {
  assert.strictEqual(R.normalizeBodyHtml('<img src="/i/a b.png">', BASE),
    '<img src="https://mp.weixin.qq.com/i/a b.png">');
});
test('%2e%2e is NOT folded (new URL 会把它当点段折叠)', function () {
  assert.strictEqual(R.normalizeBodyHtml('<a href="%2e%2e/b">t</a>', BASE),
    '<a href="https://mp.weixin.qq.com/s/%2e%2e/b">t</a>');
});
test('tab inside the value is removed by the join, not encoded', function () {
  assert.strictEqual(R.normalizeBodyHtml('<img src="/a' + TAB + 'b.png">', BASE),
    '<img src="https://mp.weixin.qq.com/ab.png">');
});
test('leading C0 control is lstripped by the join', function () {
  assert.strictEqual(R.normalizeBodyHtml('<a href="' + String.fromCharCode(1) + 'a.png">t</a>', BASE),
    '<a href="https://mp.weixin.qq.com/s/a.png">t</a>');
});
test('non-ASCII path bytes are preserved (urljoin 口径)', function () {
  assert.strictEqual(R.normalizeBodyHtml('<a href="' + CJK + '/a.png">t</a>', BASE),
    '<a href="https://mp.weixin.qq.com/s/' + CJK + '/a.png">t</a>');
});
test('entity-escaped slashes stay escaped through the join', function () {
  assert.strictEqual(R.normalizeBodyHtml('<img src="&#47;i/a.png">', BASE),
    '<img src="https://mp.weixin.qq.com/s/&#47;i/a.png">');
  assert.strictEqual(R.normalizeBodyHtml('<img src="x&#47;a.png" data-src="L.png">', BASE),
    '<img src="https://mp.weixin.qq.com/s/x&#47;a.png" data-src="L.png">');
});
test('urljoin branches: dot segments, empty path, params, fragments', function () {
  assert.strictEqual(R.normalizeBodyHtml('<a href=".">t</a>', 'https://blog.example/dir/x.html'),
    '<a href="https://blog.example/dir/">t</a>');
  assert.strictEqual(R.normalizeBodyHtml('<a href="..">t</a>', 'https://blog.example/dir/x.html'),
    '<a href="https://blog.example/">t</a>');
  assert.strictEqual(R.normalizeBodyHtml('<a href="?q=1">t</a>', BASE),
    '<a href="https://mp.weixin.qq.com/s/AbC123?q=1">t</a>');
  assert.strictEqual(R.normalizeBodyHtml('<a href="b/c;p=1?q=2#f">t</a>', BASE),
    '<a href="https://mp.weixin.qq.com/s/b/c;p=1?q=2#f">t</a>');
  assert.strictEqual(R.normalizeBodyHtml('<a href="/a#frag">t</a>', BASE),
    '<a href="https://mp.weixin.qq.com/a#frag">t</a>');
  assert.strictEqual(R.normalizeBodyHtml('<a href="sub/./b/../c/">t</a>', BASE),
    '<a href="https://mp.weixin.qq.com/s/sub/c/">t</a>');
  assert.strictEqual(R.normalizeBodyHtml('<a href="/">t</a>', BASE), '<a href="https://mp.weixin.qq.com/">t</a>');
});
test('other schemes and mail-ish values are returned untouched', function () {
  assert.strictEqual(R.normalizeBodyHtml('<a href="javascript:void(0)">t</a>', BASE),
    '<a href="javascript:void(0)">t</a>');
  assert.strictEqual(R.normalizeBodyHtml('<a href="tel:123">t</a><a href="mailto:a@b">t</a><a href="data:text/plain,x">t</a>', BASE),
    '<a href="tel:123">t</a><a href="mailto:a@b">t</a><a href="data:text/plain,x">t</a>');
});
test('poster is absolutized too, srcset is not', function () {
  assert.strictEqual(R.normalizeBodyHtml('<video poster="/p.mp4" src="/v.mp4"></video>', BASE),
    '<video poster="https://mp.weixin.qq.com/p.mp4" src="https://mp.weixin.qq.com/v.mp4"></video>');
  assert.strictEqual(R.normalizeBodyHtml('<img src="/a.png" srcset="/b.png 2x">', BASE),
    '<img src="https://mp.weixin.qq.com/a.png" srcset="/b.png 2x">');
});
test('backslash paths are ordinary segments (Python urljoin 口径)', function () {
  assert.strictEqual(R.normalizeBodyHtml('<a href="' + BS + 'a.png">t</a>', BASE),
    '<a href="https://mp.weixin.qq.com/s/' + BS + 'a.png">t</a>');
  assert.strictEqual(R.normalizeBodyHtml('<a href="a' + BS + 'b/../c.png">t</a>', BASE),
    '<a href="https://mp.weixin.qq.com/s/c.png">t</a>');
});

// ---------------------------------------------------------------- 模板注入回归门（C1 同类）
test('backslash-u / backslash-1 in a mirror value do not throw and stay verbatim', function () {
  const a = '<img data-src="https://e.com/a' + BS + 'u0026b.jpg">';
  assert.strictEqual(R.normalizeBodyHtml(a, BASE),
    '<img src="https://e.com/a' + BS + 'u0026b.jpg" data-src="https://e.com/a' + BS + 'u0026b.jpg">');
  const b = '<img data-src="https://e.com/a' + BS + '1b.jpg">';
  assert.strictEqual(R.normalizeBodyHtml(b, BASE),
    '<img src="https://e.com/a' + BS + '1b.jpg" data-src="https://e.com/a' + BS + '1b.jpg">');
  const c = '<img data-src="https://e.com/a' + BS + BS + 'b.jpg">';
  assert.strictEqual(R.normalizeBodyHtml(c, BASE),
    '<img src="https://e.com/a' + BS + BS + 'b.jpg" data-src="https://e.com/a' + BS + BS + 'b.jpg">');
});
test('JS replacement patterns ($& $1 $` $\') do not inject', function () {
  const cases2 = ['$&', '$1', '$`', "$'", '$$', '&amp;'];
  cases2.forEach(function (tk) {
    const s = '<img data-src="https://e.com/a' + tk + 'b.jpg">';
    const got = R.normalizeBodyHtml(s, BASE);
    assert.ok(got.indexOf(tk) >= 0, tk + ' 被替换串展开掉了：' + got);
    assert.strictEqual(got, '<img src="https://e.com/a' + tk + 'b.jpg" data-src="https://e.com/a' + tk + 'b.jpg">');
    const s2 = '<img src="data:," data-src="https://e.com/a' + tk + 'b.jpg">';
    assert.strictEqual(R.normalizeBodyHtml(s2, BASE),
      '<img src="https://e.com/a' + tk + 'b.jpg" data-src="https://e.com/a' + tk + 'b.jpg">');
  });
});
test('untrusted attribute names cannot poison the map', function () {
  assert.strictEqual(R.normalizeBodyHtml('<img __proto__="p" data-src="y.png">', BASE),
    '<img src="https://mp.weixin.qq.com/s/y.png" __proto__="p" data-src="y.png">');
  assert.strictEqual(R.normalizeBodyHtml('<img toString="t" constructor="c" data-croporisrc="z.png">', BASE),
    '<img src="https://mp.weixin.qq.com/s/z.png" toString="t" constructor="c" data-croporisrc="z.png">');
  const m = R.parseImgAttrs('<img __proto__="p" toString="t">');
  assert.strictEqual(Object.getPrototypeOf(m), null, '属性名映射表必须是 null 原型（裸对象会踩 __proto__）');
  assert.strictEqual(m['__proto__'], 'p');
  // 键两侧都按小写存（Python 侧 `name.lower()`），所以这里查的是 `tostring`
  assert.strictEqual(m['tostring'], 't');
  assert.strictEqual(m['toString'], undefined, '表里不许有原型链上的东西');
});
test('python-\\w-only characters are word chars (\\b 与 (?<![\\w-]) 同判)', function () {
  assert.strictEqual(R.normalizeBodyHtml('<img' + CJK + '"1" data-src="q.jpg">', BASE),
    '<img' + CJK + '"1" data-src="q.jpg">');
  assert.strictEqual(R.normalizeBodyHtml('<img ' + CJK + 'data-src="q.jpg">', BASE),
    '<img ' + CJK + 'data-src="q.jpg">');
  assert.strictEqual(R.normalizeBodyHtml('<img data-src="L.png" ' + CJK + '-src="q">', BASE),
    '<img src="https://mp.weixin.qq.com/s/L.png" data-src="L.png" ' + CJK + '-src="q">');
});

// ---------------------------------------------------------------- 懒加载优先级（元组序）
test('LAZY_SRC_ATTRS order is the priority order', function () {
  assert.deepStrictEqual(R.LAZY_SRC_ATTRS, ['data-src', 'data-original', 'data-lazy-src', 'data-croporisrc']);
  const P = function (tag, want) {
    assert.strictEqual(R.normalizeBodyHtml(tag, BASE),
      '<img src="https://mp.weixin.qq.com/s/' + want + '" ' + tag.slice(5));
  };
  P('<img data-original="o.png" data-src="s.png">', 's.png');
  P('<img data-lazy-src="l.png" data-original="o.png">', 'o.png');
  P('<img data-croporisrc="c.png" data-lazy-src="l.png">', 'l.png');
  P('<img data-src="" data-original="o.png">', 'o.png');
  P('<img data-src="s.png" data-original="o.png" data-lazy-src="l.png" data-croporisrc="c.png">', 's.png');
});

// ---------------------------------------------------------------- capBody
test('cap: code points, not UTF-16 units', function () {
  const long = '<p>' + 'z'.repeat(49970) + '</p><img src="https://e.com/a.jpg" alt="x">';
  const capped = R.capBody(long, 50000);
  assert.ok(Array.from(capped).length <= 50000);
  assert.ok(!/<[^>]*$/.test(capped), '出口挂着半个标签：' + JSON.stringify(capped.slice(-12)));
  assert.strictEqual(R.capBody('🐪'.repeat(403) + '<p>x</p>', 400), '🐪'.repeat(400));
  assert.strictEqual(R.capBody('🐪'.repeat(399) + '<p>x</p>', 400), '🐪'.repeat(399));
  assert.strictEqual(R.capBody('<div>' + '多'.repeat(200), 205), '<div>' + '多'.repeat(200));
  assert.strictEqual(R.capBody('<div>' + '多'.repeat(200), 202), '<div>' + '多'.repeat(197));
});
test('cap: untouched inputs come back byte-for-byte (R14)', function () {
  ['ends with <span', 'Tom&Jerry', 'trailing   ', 'x&#12&am', 'x&#xab&am', 'x&  &ab',
   'A&' + NBSP + 'nbsp', 'ab&am' + FSX, 'x&  &ab', '短正文'].forEach(function (t) {
    assert.strictEqual(R.capBody(t, 50), t, '未截断却被收口：' + JSON.stringify(t));
  });
  // 恰好等于上限 = 未截断 ⇒ 尾部那条裸 `&` 也不许动（账本 R18 的口径）
  assert.strictEqual(R.capBody('y'.repeat(49999) + '&', 50000), 'y'.repeat(49999) + '&');
  assert.strictEqual(R.capBody('y'.repeat(50000), 50000), 'y'.repeat(50000));
  assert.strictEqual(R.capBody('短正文'), '短正文');
  assert.strictEqual(R.capBody(''), '');
  // `limit || BODY_CAP` 那一档写法会把 0 上限悄悄变成 50000：Python 侧 `_cap_body(x, 0)` 是空串
  assert.strictEqual(R.capBody('abc', 0), '');
});
test('cap: name segment is stripped at most once (NEW-3)', function () {
  assert.strictEqual(R.capBody('A&B &am ', 7), 'A&B');
  assert.strictEqual(R.capBody('a&&b&am', 7), 'a&&b&am');
  assert.strictEqual(R.capBody('Tom&Jerryzz', 7), 'Tom');
  assert.strictEqual(R.capBody('R&Dnbspz', 7), 'R');
  assert.strictEqual(R.capBody('x&xab&am', 8), 'x&xab&am');
  assert.strictEqual(R.capBody('a&&&&&b', 6), 'a');
  assert.strictEqual(R.capBody('<p>aaa</p><im', 9), '<p>aaa');
});
test('cap: unambiguous entities are stripped unboundedly (R27)', function () {
  assert.strictEqual(R.capBody('a&#000000000z', 11), 'a');
  assert.strictEqual(R.capBody('a&#123456789012z', 15), 'a');
  assert.strictEqual(R.capBody('a&#x00000000fq', 13), 'a');
  assert.strictEqual(R.capBody('&#xABCDEF0123456789z', 19), '');
  assert.strictEqual(R.capBody('&#1&#2&#3x', 9), '');
  assert.strictEqual(R.capBody('& &m', 3), '');
  assert.strictEqual(R.capBody('ab  &mz', 6), 'ab');
  assert.strictEqual(R.capBody('价格' + NBSP + '&nbspz', 7), '价格');
  assert.strictEqual(R.capBody('zz&nbsp' + FSX + 'x', 6), 'zz');
  assert.strictEqual(R.capBody('&#38&#x26&#x', 14), '&#38&#x26&#x');
  assert.strictEqual(R.capBody('x&#;&#x0;zz', 11), 'x&#;&#x0;zz');
  assert.strictEqual(R.capBody('a&#x;' + ARABIC3 + 'z', 6), 'a&#x;' + ARABIC3);
});
test('cap: digits are ASCII only (R25 少剥那一侧)', function () {
  assert.strictEqual(R.capBody('x&#' + ARABIC3 + 'z', 4), 'x&#' + ARABIC3);
});
test('cap: scanner branches (& / # / x) and the digit→hex fall-through', function () {
  assert.strictEqual(R.capBody('x&#8;zz', 6), 'x&#8;z');
  assert.strictEqual(R.capBody('r&#x1&#x2&#x3', 13), 'r&#x1&#x2&#x3');
  assert.strictEqual(R.capBody('a&#x12z', 6), 'a');
  assert.strictEqual(R.capBody('<span>x&#1', 9), '<span>x');
  assert.strictEqual(R.capBody('<b>&#38;&#x26;&nbsp&nbsp', 18), '<b>&#38;&#x26;');
});
test('cap: whitespace cursor is the explicit table, never \\s', function () {
  assert.strictEqual(R.capBody('ab&am' + FSX, 5), 'ab');
  assert.strictEqual(R.capBody('ab&am' + NEL, 5), 'ab');
  assert.strictEqual(R.capBody('ab&am' + NEL + 'x', 6), 'ab');
  assert.strictEqual(R.capBody('ab&am' + BOM + 'q', 6), 'ab&am' + BOM);
  assert.strictEqual(R.capBody('ab&am' + ZWSP + 'q', 6), 'ab&am' + ZWSP);
  assert.strictEqual(R.capBody('zzzz&mz' + NBSP, 8), 'zzzz&mz' + NBSP);
});
test('cap: default limit is shared with Python', function () {
  assert.strictEqual(R.BODY_CAP, 50000);
  assert.strictEqual(R.capBody('y'.repeat(50001)).length, 50000);
});
test('cap: degenerate runs stay linear', function () {
  const t0 = Date.now();
  assert.strictEqual(R.capBody('&'.repeat(50001), 50000), '');
  assert.strictEqual(R.capBody('<'.repeat(50001), 50000), '');
  assert.strictEqual(R.capBody('a&#' + '0'.repeat(49998), 50000), 'a');
  assert.strictEqual(R.capBody('a&#' + '0'.repeat(49900), 50000), 'a&#' + '0'.repeat(49900));
  const mixed = R.capBody('y&nbsp'.repeat(8400), 50000);
  assert.strictEqual(mixed.length, 49999);
  assert.ok(/y$/.test(mixed), 'y&nbsp 连排的切点分叉：' + JSON.stringify(mixed.slice(-12)));
  const dt = Date.now() - t0;
  assert.ok(dt < 4000, '退化输入耗时 ' + dt + 'ms —— 刀二又退化成整串重扫了');
});

// ---------------------------------------------------------------- 挑战页 / 付费墙
test('isChallengePage: WAF and CDN shells', function () {
  assert.strictEqual(R.isChallengePage('<html><head><script src="https://s.waf/aliyun_waf.js"></script></head><body>verify</body></html>'), true);
  assert.strictEqual(R.isChallengePage('<html><head><title>Just a moment...</title></head><body>Checking your browser</body></html>'), true);
  assert.strictEqual(R.isChallengePage('<html><head><meta http-equiv="refresh" content="0"><script>x</script></head><body>ok</body></html>'), true);
  assert.strictEqual(R.isChallengePage(''), false);
});
test('isChallengePage: real pages are not eaten', function () {
  assert.strictEqual(R.isChallengePage('<html><head><title>x</title></head><body><p>hello world</p></body></html>'), false);
  assert.strictEqual(R.isChallengePage('<html><body>' + '正文'.repeat(500) + '</body></html>'), false);
  assert.strictEqual(R.isChallengePage('<html><body>' + '关注我们的公众号 ' + 'x'.repeat(400) + '</body></html>'), false);
});
test('classifyBody: paywall before length', function () {
  assert.strictEqual(R.classifyBody(725, '<html><body>' + '字'.repeat(725) + '付费阅读</body></html>'), 'paywall');
  assert.strictEqual(R.classifyBody(900, '<html><body>Subscribe to read the full article</body></html>'), 'paywall');
  assert.strictEqual(R.classifyBody(40000, '<html><body>Members Only and more ' + 'y'.repeat(40000) + '</body></html>'), 'paywall');
});
test('classifyBody: short vs full vs empty', function () {
  assert.strictEqual(R.classifyBody(300, '<html><body>普通短页</body></html>'), 'short');
  assert.strictEqual(R.classifyBody(1199, '<html><body>ok</body></html>'), 'short');
  assert.strictEqual(R.classifyBody(1200, '<html><body>ok</body></html>'), null);
  assert.strictEqual(R.classifyBody(40000, '<html><body>' + '内容'.repeat(9000) + '</body></html>'), null);
  assert.strictEqual(R.classifyBody(0, '<html><body>x</body></html>'), null);
  assert.strictEqual(R.SHORT_BODY_CHARS, 1200);
});
test('classifyBody return domain is only paywall / short / null', function () {
  // Task 6 的前端按 degraded 的**取值域**写文案表（_FT_NOTES）。返回域一旦漂出这三格，
  // 前端就落到"未知错误"。用 6×N 的网格扫长度 × 是否带付费墙词，把域钉死而不是靠几个样本。
  const lens = [1, 99, 100, 101, 599, 1199, 1200, 1201, 49999];
  const htmls = ['', '<p>普通正文</p>', '<p>Subscribe to read</p>', '<p>会员可见</p>',
                 '<p>试读</p>', '<p>paywall</p>'];
  const allowed = { paywall: 1, short: 1 };
  let sawPaywall = 0, sawShort = 0, sawNull = 0;
  for (const n of lens) for (const h of htmls) {
    const v = R.classifyBody(n, h);
    if (v === null) { sawNull++; continue; }
    assert.ok(typeof v === 'string' && allowed[v],
      'classifyBody(' + n + ', …) 返回了取值域外的值：' + JSON.stringify(v));
    if (v === 'paywall') sawPaywall++; else sawShort++;
  }
  assert.ok(sawPaywall > 0 && sawShort > 0 && sawNull > 0,
    '三格全空 = 判据在空跑：' + [sawPaywall, sawShort, sawNull].join('/'));
  // 付费墙优先于长度：付费墙 + 长正文仍要标注（长不等于完整）
  assert.strictEqual(R.classifyBody(49999, '<p>members only</p>'), 'paywall');
});

// ---------------------------------------------------------------- 零依赖红线（字面闸）
test('lib is dependency-free and does not ship new URL / trimEnd / \\s', function () {
  const src = fs.readFileSync(BODY_LIB, 'utf8');
  assert.ok(src.indexOf('require(') < 0, 'lib 里出现 require ⇒ 判据在 CI 上直接崩');
  const code = src.split('\n').filter(function (ln) {
    const t = ln.trim();
    return t.indexOf('*') !== 0 && t.indexOf('//') !== 0 && t.indexOf('/*') !== 0;
  }).join('\n');
  assert.ok(code.indexOf('new URL(') < 0, '出厂值不许来自 new URL()（它会百分号编码）');
  ['trimEnd(', 'trimRight(', 'trimStart(', 'trimLeft(', '.trim()'].forEach(function (b) {
    assert.ok(code.indexOf(b) < 0, b + ' 的空白集与 Python 不同');
  });
  assert.ok(code.indexOf('\\s') < 0, 'JS 的 \\s 与 Python isspace 差 6 个码位，必须用显式表');
});
test('shared tables are exported and non-empty', function () {
  assert.ok(Array.isArray(R.CHALLENGE_MARKERS) && R.CHALLENGE_MARKERS.length >= 6);
  assert.ok(Array.isArray(R.PAYWALL_MARKERS) && R.PAYWALL_MARKERS.length >= 6);
  assert.deepStrictEqual(R.PLACEHOLDER_SRC_NAMES, ['blank', 'lazy', 'placeholder', 'spacer']);
  assert.strictEqual(R.ENTITY_TAIL_WINDOW, 9);
  R.CHALLENGE_MARKERS.concat(R.PAYWALL_MARKERS).forEach(function (m) {
    assert.strictEqual(m, m.toLowerCase(), '特征表里不许有大写项（比对的是 lower 后的原文）：' + m);
  });
  assert.ok(R.WHITESPACE_CODE_POINTS.indexOf(0x1c) >= 0);
  assert.ok(R.WHITESPACE_CODE_POINTS.indexOf(0x85) >= 0);
  assert.ok(R.WHITESPACE_CODE_POINTS.indexOf(0xfeff) < 0);
  assert.strictEqual(R.isWordChar(0x4e2d), true);
  assert.strictEqual(R.isWordChar(0x200b), false);
});
test('the regex whitespace class and the lookup table are the same set', function () {
  // WS_CLASS（正则用）与 WHITESPACE_CODE_POINTS（游标/trim 查表用）必须由同一份表派生且同判，
  // 否则"刀二游标与出口 trim 共用同一份常量"就只是注释里的一句话。
  const re = new RegExp('^' + R.WS_CLASS + '$', 'u');
  const table = new Set(R.WHITESPACE_CODE_POINTS);
  let bad = [];
  for (let cp = 0; cp <= 0x3400; cp++) {
    const byRe = re.test(String.fromCodePoint(cp));
    const byTable = table.has(cp);
    if (byRe !== byTable) bad.push(cp.toString(16));
  }
  assert.deepStrictEqual(bad, [], '两条空白判定分叉：' + bad.slice(0, 12).join(','));
});

// ---------------------------------------------------------------- 跑
let fails = 0;
cases.forEach(function (c) {
  try {
    c[1]();
    console.log('ok ' + c[0]);
  } catch (e) {
    fails++;
    console.error('FAIL ' + c[0] + '\n  ' + (e && e.message ? String(e.message).slice(0, 600) : e));
  }
});
console.log(fails ? ('FAILED ' + fails + ' / ' + cases.length) : ('ALL PASS ' + cases.length));
process.exit(fails ? 1 : 0);
