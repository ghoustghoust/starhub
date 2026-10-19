# tests/rss_history/test_retention_api_behavior.py
# -*- coding: utf-8 -*-
"""行为级测试：真的把 api/rss.js 的 handler 跑起来，证明运行时闸门不是空转。

为什么只有字符串断言不够（2026-09-20 对抗审查 P1-1 实测）：
把 `gateSources` 首行改成 `return sources;`（闸门整体空转），
`tests/rss_history/test_retention_js_parity.py` 的 5 例与 node 自测 13 例**全部照样绿** ——
因为它们测的是 lib/rss_retention.js 本体，而三条出口"接没接线"只靠
`js.count("gateSources(...")` 这种文本存在性，注释里留一个同名串都能满足。
用户明确禁"空函数/空转"，所以这里必须喂真数据看结果。

做法：把 api/rss.js 按 Vercel 的装载方式（ESM → CJS）转一份临时副本，
stub 掉 global fetch 让它不联网，然后直接调 handler 的 ?source= 出口。
"""
import json
import os
import re
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
JS_DIR = os.path.join(ROOT, "tests", "rss_js")
GENERATED = os.path.join(JS_DIR, "_rss_cjs_generated.cjs")

DRIVER = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[2];
const LIB_OVERRIDE = process.argv[3] || path.join(ROOT, 'lib', 'rss_retention.js');
const out = { ok: false, reason: '', items: [], header: null, status: 0 };

// ── 1. 按 Vercel 的做法把 ESM 语法的 api/rss.js 转成 CJS 再装载 ──
let src = fs.readFileSync(path.join(ROOT, 'api', 'rss.js'), 'utf8');
const before = src;
src = src.replace("import { readFileSync } from 'fs';", "const { readFileSync } = require('fs');");
src = src.replace("import { join } from 'path';", "const { join } = require('path');");
src = src.replace("export default async function handler", "module.exports = async function handler");
if (src === before) { throw new Error('转译没生效：api/rss.js 的 import/export 写法变了'); }

// ── 1b. 自动发现 api 源码里的 `require('../lib/*.js')`，逐条改写成绝对路径 ──
// 不许再逐条硬写 lib 名单：生成件落在 tests/rss_js/ 下，`../lib/` 会被解析成 `tests/lib/`，
// Node 直接 MODULE_NOT_FOUND。老写法只登记了 retention 一条，Task 4 给 api/rss.js 新增
// **硬** require 的 body_rules.js 没人登记 ⇒ 本文件 2 条判据全红（2026-10-04 实测）。
// 扫源码 ⇒ 新增 lib 依赖**零人工登记**就能跑，这才是这一类缺陷的收口。
// 覆盖表只留给"必须换实现"的那条（retention）：LIB_OVERRIDE 是 stub 闸门的路径，
// 把它退化成普通路径的话 test_gate_computation_failure_degrades_open_not_500 就没着力点。
// 防漂移判据见 tests/rss_history/test_api_lib_require_drift.py。
const LIB_REQUIRE_RE = /require\(\s*(['"])\.\.\/lib\/([A-Za-z0-9_.\-]+\.js)\1\s*\)/g;
const LIB_REQUIRE_LEFTOVER = /require\(\s*['"]\.\.\/lib\//;
function rewriteLibRequires(source, root, overrides) {
  const names = [];
  const out = source.replace(LIB_REQUIRE_RE, function (_all, _q, name) {
    names.push(name);
    const target = Object.prototype.hasOwnProperty.call(overrides, name)
      ? overrides[name] : path.join(root, 'lib', name);
    return 'require(' + JSON.stringify(target) + ')';
  });
  if (LIB_REQUIRE_LEFTOVER.test(out)) {
    throw new Error('还有相对 ../lib 依赖没被改写：新增 lib 的写法超出了自动发现的口径，去补正则而不是加一条硬编码');
  }
  if (!names.length) {
    throw new Error('自动发现一条 ../lib 依赖都没扫到 ⇒ api/rss.js 的加载写法变了，转译探针已失效');
  }
  return { src: out, names: names };
}
const LIB_OVERRIDES = { 'rss_retention.js': LIB_OVERRIDE };
const REWRITTEN = rewriteLibRequires(src, ROOT, LIB_OVERRIDES);
src = REWRITTEN.src;
if (REWRITTEN.names.indexOf('rss_retention.js') < 0) {
  throw new Error('闸门 require 改写没生效：api/rss.js 的加载写法变了，测试探针已失效');
}
if (src.indexOf(JSON.stringify(LIB_OVERRIDE)) < 0) {
  throw new Error('闸门没指到 LIB_OVERRIDE 给的实现 ⇒ "换实现"的用例失去着力点');
}
const gen = path.join(ROOT, 'tests', 'rss_js', '_rss_cjs_generated.cjs');
fs.writeFileSync(gen, src);

// ── 2. 假上游：一条 200 小时前的旧文 + 一条 1 小时前的新文，绝不打网络 ──
function pubDate(hoursAgo) {
  return new Date(Date.now() - hoursAgo * 3600000).toUTCString();
}
const xml = '<?xml version="1.0" encoding="UTF-8"?>'
  + '<rss version="2.0"><channel><title>T</title>'
  + '<item><title>fresh</title><link>http://e/new</link>'
  + '<pubDate>' + pubDate(1) + '</pubDate><description>d</description></item>'
  + '<item><title>ancient</title><link>http://e/old</link>'
  + '<pubDate>' + pubDate(200) + '</pubDate><description>d</description></item>'
  + '</channel></rss>';
global.fetch = async () => ({
  ok: true, status: 200,
  headers: { get: () => 'application/rss+xml' },
  text: async () => xml,
});

const handler = require(gen);
const sources = JSON.parse(fs.readFileSync(path.join(ROOT, 'rss_sources.json'), 'utf8'));
const key = sources[0].key;

const res = {
  statusCode: 0, body: null, headers: {},
  status(c) { this.statusCode = c; return this; },
  json(b) { this.body = b; return this; },
  // 与 api/rss.js 那份驱动同一口径：假 res 一旦比生产宽松，"源键写进响应头"这类
  // 缺陷就只在生产炸（中文源键 -> ERR_INVALID_CHAR -> 500，2026-09-21 实测 542/968 源）。
  setHeader(k, v) {
    if (typeof v === 'string' && /[^\t\x20-\x7e\x80-\xff]/.test(v)) {
      const e = new Error('Cannot convert argument to a ByteString: ' + k);
      e.code = 'ERR_INVALID_CHAR';
      throw e;
    }
    this.headers[k] = v; return this;
  },
};

(async () => {
  try {
    await handler({ query: { source: key }, headers: {} }, res);
    out.status = res.statusCode;
    out.header = res.headers['X-RSS-Retention'] || null;
    const its = (res.body && res.body.items) || [];
    out.items = its.map((x) => x.link || x.u);
    out.ok = true;
  } catch (e) {
    out.reason = String(e && e.message);
  }
  process.stdout.write("RESULT:" + JSON.stringify(out) + String.fromCharCode(10));
})();
"""


def _node():
    try:
        subprocess.run(["node", "--version"], capture_output=True, timeout=20)
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _node(), reason="本机没有 node（CI 上有）")
def test_single_source_endpoint_actually_drops_stale_item(tmp_path):
    os.environ.setdefault("PYTHONUTF8", "1")
    drv = tmp_path / "driver.cjs"
    drv.write_text(DRIVER, encoding="utf-8")
    try:
        r = subprocess.run(["node", str(drv), ROOT], capture_output=True, encoding="utf-8", errors="replace",
                           timeout=240, cwd=ROOT)
        assert r.returncode == 0, "driver 跑挂: rc=%s %s %s" % (r.returncode, r.stdout[-400:], r.stderr[-400:])
        line = [l for l in r.stdout.splitlines() if l.startswith("RESULT:")]
        assert line, "驱动没输出结果行，stdout 前 400 字：%s" % r.stdout[:400]
        got = json.loads(line[-1][len("RESULT:"):])
        assert got["ok"], "handler 抛异常: %s" % got["reason"]
        assert got["status"] == 200, "端点没返回 200，而是 %s" % got["status"]
        assert got["header"] == "on", \
            "X-RSS-Retention=%r —— 闸门模块没装载成功，运行时又在出厂老内容" % got["header"]
        assert "http://e/new" in got["items"], "窗口内条目被误删: %s" % got["items"]
        assert "http://e/old" not in got["items"], \
            "200h 前的条目仍然出厂：闸门接了线但空转（这正是本测试要抓的形态）"
    finally:
        # 生成物不许留在工作树里：CI 的 git add 清单是按文件名匹配的，脏文件可能被顺手提交
        if os.path.exists(GENERATED):
            os.remove(GENERATED)


THROWING_LIB = """
'use strict';
module.exports = {
  applyRetention: function () { throw new Error('boom: 闸门内部异常'); },
};
"""


@pytest.mark.skipif(not _node(), reason="本机没有 node（CI 上有）")
def test_gate_computation_failure_degrades_open_not_500(tmp_path):
    """闸门计算抛异常时必须降级为「不过闸 + 头里留痕」，不能把端点打成 500。

    审查 P1-2 实测：旧写法只兜 require，lib 一抛就 500 —— 而注释承诺的是「宁可放过不可 500」。
    """
    drv = tmp_path / "driver2.cjs"
    drv.write_text(DRIVER, encoding="utf-8")
    stub = tmp_path / "throwing_lib.cjs"
    stub.write_text(THROWING_LIB, encoding="utf-8")
    try:
        r = subprocess.run(["node", str(drv), ROOT, str(stub)], capture_output=True,
                           encoding="utf-8", errors="replace", timeout=300, cwd=ROOT)
        assert r.returncode == 0, "驱动跑挂: %s" % r.stderr[-400:]
        line = [l for l in r.stdout.splitlines() if l.startswith("RESULT:")]
        assert line, "没输出结果行: %s" % r.stdout[:400]
        got = json.loads(line[-1][len("RESULT:"):])
        assert got["ok"], "异常冒到调用方了: %s" % got["reason"]
        assert got["status"] == 200, "闸门异常把端点打成了 %s（承诺是 fail-open）" % got["status"]
        assert got["header"] == "unavailable", (
            "降级没留痕（头是 %r）：静默不过闸等于把失效藏起来" % got["header"])
        assert "http://e/old" in got["items"], "降级后连内容都不出了: %s" % got["items"]
    finally:
        if os.path.exists(GENERATED):
            os.remove(GENERATED)


def test_generated_probe_is_not_left_in_worktree():
    """上一条用例若中途崩在 finally 之前，残留的 _rss_cjs_generated.cjs 会被当源码提交。

    单独一条断言让它至少会被 CI 抓出来，而不是静静躺在仓库里。
    """
    assert not os.path.exists(GENERATED), "上一次行为测试的转译副本没清掉: %s" % GENERATED


@pytest.mark.skipif(not _node(), reason="本机没有 node（CI 上有）")
def test_lib_survives_malformed_items(tmp_path):
    """lib 层边界：items 为 null 这类畸形结构不该抛异常。

    这条只管"别抛"，不管端点降级 —— 端点的 fail-open 由
    test_gate_computation_failure_degrades_open_not_500 用真 handler 验（那条才是审查 P1-2 的对策）。
    """
    js = r"""
const path = require('path');
const ROOT = process.argv[2];
const R = require(path.join(ROOT, 'lib', 'rss_retention.js'));
// applyRetention 收到非数组 items 时不该抛（真上游偶发结构异常），
// 而 gateSources 外面必须有 try/catch —— 这里直接验证 lib 层的健壮性边界。
try {
  const out = R.applyRetention([{ key: 'k', items: null }], { nowMs: Date.now() });
  process.stdout.write(JSON.stringify({ threw: false, n: out.sources[0].items.length }));
} catch (e) {
  process.stdout.write(JSON.stringify({ threw: true, msg: String(e.message) }));
}
"""
    drv = tmp_path / "robust.cjs"
    drv.write_text(js, encoding="utf-8")
    r = subprocess.run(["node", str(drv), ROOT], capture_output=True, encoding="utf-8", errors="replace", timeout=120, cwd=ROOT)
    assert r.returncode == 0, r.stderr[-300:]
    got = json.loads(r.stdout.strip().splitlines()[-1])
    assert not got["threw"], "items 为 null 时闸门抛了（%s）：gateSources 必须兜住，否则端点 500" % got.get("msg")
    assert got["n"] == 0


@pytest.mark.skipif(not _node(), reason="本机没有 node（CI 上有）")
def test_wiring_assertions_ignore_comments(tmp_path):
    """接线断言不许被注释满足：审查用"删掉调用、注释里留着同名串"骗过了旧版检查。"""
    js = open(os.path.join(ROOT, "api", "rss.js"), encoding="utf-8").read()
    code = re.sub(r"/\*[\s\S]*?\*/", "", js)
    code = "\n".join(l for l in code.splitlines() if not l.strip().startswith("//"))
    for pat in ("gateSources(results", "gateSources([result]", "gateSources(mergedSources"):
        assert code.count(pat) >= 1, "运行时出口 %s 其实不存在（只在注释里出现）" % pat
