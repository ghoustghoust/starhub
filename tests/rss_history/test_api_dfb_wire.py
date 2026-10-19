# -*- coding: utf-8 -*-
"""/api/rss 两条实时压缩出口必须带出「收录」标记（任务 #30，门禁 A2）。

现场：`api/rss.js:255-259` 的 `datedOrCapture()` 已经算出 `{pub_date, date_fallback:true}`，
`:425/:450` 也把它挂到条目上，但 `?source=KEY` 与 `?batch=N` 两条出口都把条目重写成
压缩对象（键 t/u/s/d），标记在这一步被丢掉 —— 于是实时路径的卡片仍把"抓取时刻"
当发布时间显示，前端无从区分。构建期快照走的是另一条路（build_rss_aggregator 写
`item["date_fallback"]`），所以只有这两条出口漏了。

线上传输键名沿用快照既有口径 **长名 `date_fallback`**（浏览器侧 `buildArt`/
`_mergeRemoteSources` 读的都是这个键），不引入新键名。

本文件不做文本断言：把 api/rss.js 转成 CJS、stub 掉 fetch、**真调 handler**，
检查响应体里的条目。两条出口各一条用例（少接一处必须红，见变异体记录）。
"""
import json
import os
import subprocess
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
GEN_REL = os.path.join("tests", "rss_js", "_rss_dfb_generated.cjs").replace("\\", "/")

DRIVER = r"""
'use strict';
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[2];
const MODE = process.argv[3];          // 'source' | 'batch'
const WORK = process.argv[4];          // 作为 process.cwd() 的临时目录
const out = {};

// ── 1. 转 CJS（ESM → CJS；lib 依赖改指绝对路径 —— 生成件在 tests/ 下，相对 require 会指错目录）──
let src = fs.readFileSync(path.join(ROOT, 'api', 'rss.js'), 'utf8');
const before = src;
src = src.replace("import { readFileSync } from 'fs';", "const { readFileSync } = require('fs');");
src = src.replace("import { join } from 'path';", "const { join } = require('path');");
src = src.replace("export default async function handler", "module.exports = async function handler");
if (src === before) { throw new Error('转译没生效：api/rss.js 的 import/export 写法变了'); }

// ── 1b. 自动发现 api 源码里的 `require('../lib/*.js')`，逐条改写成绝对路径 ──
// 不许再逐条硬写 lib 名单：生成件落在 tests/rss_js/ 下，`../lib/` 会被解析成 `tests/lib/`，
// Node 直接 MODULE_NOT_FOUND。老写法只登记了 retention 一条，Task 4 给 api/rss.js 新增
// **硬** require 的 body_rules.js 没人登记 ⇒ 本文件 4 条判据全红（2026-10-04 实测）。
// 扫源码 ⇒ 新增 lib 依赖**零人工登记**就能跑，这才是这一类缺陷的收口。
// 覆盖表只留给"必须换实现"的那条（retention）：它退化成普通路径的话，
// "闸门装载失败/计算异常"那两类用例就没有着力点。防漂移判据见
// tests/rss_history/test_api_lib_require_drift.py。
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
const LIB_OVERRIDES = {
  'rss_retention.js': process.env.STARHUB_RSS_LIB || path.join(ROOT, 'lib', 'rss_retention.js'),
};
const REWRITTEN = rewriteLibRequires(src, ROOT, LIB_OVERRIDES);
src = REWRITTEN.src;
if (REWRITTEN.names.indexOf('rss_retention.js') < 0) {
  throw new Error('闸门 require 改写没生效：api/rss.js 的加载写法变了，测试探针已失效');
}
if (src.indexOf(JSON.stringify(LIB_OVERRIDES['rss_retention.js'])) < 0) {
  throw new Error('闸门没指到 STARHUB_RSS_LIB 给的实现 ⇒ "换实现"的用例失去着力点');
}
fs.writeFileSync(path.join(ROOT, '__GEN__'), src);

// ── 2. 假上游：一条带 pubDate、一条**不带** pubDate，绝不打网络 ──
const pub = new Date(Date.now() - 3600000).toUTCString();
const xml = '<?xml version="1.0" encoding="UTF-8"?>'
  + '<rss version="2.0"><channel><title>T</title>'
  + '<item><title>dated one</title><link>http://e/dated</link>'
  + '<pubDate>' + pub + '</pubDate><description>d</description></item>'
  + '<item><title>undated one</title><link>http://e/undated</link>'
  + '<description>d</description></item>'
  + '</channel></rss>';
global.fetch = async () => ({
  ok: true, status: 200,
  headers: { get: () => 'application/rss+xml' },
  text: async () => xml,
});

// loadSources()/loadSnapshot() 读的是 process.cwd()，所以用 WORK 目录放一份两源的清单，
// 既让 batch 出口只抓 2 个源（不必遍历 970 源），也不把判据绑在会变的真实源清单上。
fs.writeFileSync(path.join(WORK, 'rss_sources.json'), JSON.stringify([
  { key: 'dfa', name: 'A', cat: 'c', color: '#f00', url: 'http://up/a', tier: 3 },
  { key: 'dfb', name: 'B', cat: 'c', color: '#f00', url: 'http://up/b', tier: 3 },
  // refresh 走的是"T1 实时 + T2/T3 快照"合并支，那条支用 `...item` 展开而不是手写键，
  // 所以它必须单独测：不能因为"展开会带上字段"就推断它没问题。
  { key: 'dft1', name: 'T', cat: 'c', color: '#f00', url: 'http://up/t1', tier: 1 },
  // 生产里 542/968 个源键含中文；Node 的真实 ServerResponse 会拒收非 latin1 的
  // header 值，所以"源键被写进响应头"这条路径必须被测到（见 MODE=cjk）。
  { key: '安全客_测试', name: 'C', cat: '安全', color: '#f00', url: 'http://up/c', tier: 3 },
]));

const handler = require(path.join(ROOT, '__GEN__'));
const res = {
  statusCode: 0, body: null, headers: {},
  status(c) { this.statusCode = c; return this; },
  json(b) { this.body = b; return this; },
  // 故意照抄 Node 的校验：假 res 比生产宽松，是这类缺陷能活到线上的唯一原因
  // （本地真跑 handler 全绿、线上 ?source=<中文键> 直接 500）。
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
    const key = MODE === 'cjk' ? '安全客_测试' : 'dfa';
    let query;
    if (MODE === 'batch') query = { batch: '0' };
    else if (MODE === 'refresh') query = { refresh: '1' };
    else query = { source: key };
    await handler({ query: query, headers: {} }, res);
    out.status = res.statusCode;
    out.retentionHeader = res.headers['X-RSS-Retention'];
    let items = [];
    if (res.body && res.body.sources) {
      for (const s of res.body.sources) {
        items = items.concat((s.items || []).map(it => Object.assign({ _key: s.key }, it)));
      }
    } else {
      items = (res.body && res.body.items) || [];
    }
    out.byLink = {};
    for (const x of items) {
      out.byLink[x.u || x.link] = {
        d: x.d || x.pub_date || '',
        flag: (x.date_fallback === undefined ? null : x.date_fallback),
      };
    }
    out.keys = items.length ? Object.keys(items[0]) : [];
    out.count = items.length;
    out.headersSet = Object.keys(res.headers);
    out.ok = true;
  } catch (e) {
    out.reason = String(e && e.message);
  }
  process.stdout.write("RESULT:" + JSON.stringify(out) + String.fromCharCode(10));
})();
""".replace("__GEN__", GEN_REL)

GEN = os.path.join(ROOT, GEN_REL)


def _node():
    try:
        subprocess.run(["node", "--version"], capture_output=True, timeout=20)
        return True
    except Exception:
        return False


def _run(mode, tmp_path):
    work = str(tmp_path).replace("\\", "/")
    drv = tmp_path / ("driver_%s.cjs" % mode)
    drv.write_text(DRIVER, encoding="utf-8")
    env = dict(os.environ)
    env.setdefault("PYTHONUTF8", "1")
    env["STARHUB_RSS_LIB"] = os.path.join(ROOT, "lib", "rss_retention.js").replace("\\", "/")
    try:
        r = subprocess.run(["node", str(drv), ROOT, mode, work], capture_output=True,
                           encoding="utf-8", errors="replace", timeout=240, cwd=work, env=env)
        assert r.returncode == 0, "driver 跑挂: rc=%s %s %s" % (
            r.returncode, r.stdout[-400:], r.stderr[-400:])
        line = [l for l in r.stdout.splitlines() if l.startswith("RESULT:")]
        assert line, "没有结果行，stdout 前 400 字：%s stderr 前 400 字：%s" % (
            r.stdout[:400], r.stderr[:400])
        got = json.loads(line[-1][len("RESULT:"):])
        assert got.get("ok"), "handler 抛异常: %s" % got.get("reason")
        assert got["status"] == 200, (
            "请求没走到 200（%s）；handler 内部被 catch 成 500 时这里能看到 body 形状：%s"
            % (got["status"], json.dumps(got, ensure_ascii=False)[:300]))
        # 闸门必须真的在跑：装载失败时它会打印 unavailable，测试就会测到一条没过滤的路径
        assert got.get("retentionHeader") == "on", got
        return got
    finally:
        # 生成物不许留在工作树：CI 的 git add 按文件名匹配，脏文件可能被顺手提交
        if os.path.exists(GEN):
            os.remove(GEN)


def _assert_marker(got):
    und = got["byLink"].get("http://e/undated")
    den = got["byLink"].get("http://e/dated")
    assert und, "无日期条目没出现在响应里（它应靠收录时刻入窗）：%s" % list(got["byLink"])
    assert den, "带日期条目丢了：%s" % list(got["byLink"])
    assert und.get("flag"), (
        "无日期条目出网时没带 date_fallback ⇒ 前端把抓取时刻当发布时间显示；"
        "该条目键=%s" % got["keys"])
    assert not den.get("flag"), "带真实 pubDate 的条目被误标成收录"


@pytest.mark.skipif(not _node(), reason="本机没有 node（CI 上有）")
def test_single_source_wire_ships_the_capture_marker(tmp_path):
    """?source=KEY —— 抽屉实时抓取出口（fetchOne 的压缩映射）。"""
    _assert_marker(_run("source", tmp_path))


@pytest.mark.skipif(not _node(), reason="本机没有 node（CI 上有）")
def test_batch_wire_ships_the_capture_marker(tmp_path):
    """?batch=N —— 分批刷新出口（formattedSources 的压缩映射）。

    这条是独立的一处丢字段点：两处映射各自重写条目对象，只修一处必然另一处仍错。
    """
    _assert_marker(_run("batch", tmp_path))


@pytest.mark.skipif(not _node(), reason="本机没有 node（CI 上有）")
def test_refresh_merged_wire_ships_the_capture_marker(tmp_path):
    """?refresh=1 —— "T1 实时 + T2/T3 快照"合并支。

    这一支用 `...item` 展开，形状上"标记应该会自己带过来"，但**应该会过**正是
    要单独造判据的理由：谁把手写键的另两处抄过来（或反过来把展开改成手写键列表），
    这条就会红。三支出口各自重写条目对象，缺一条就是缺一条。
    """
    _assert_marker(_run("refresh", tmp_path))


@pytest.mark.skipif(not _node(), reason="本机没有 node（CI 上有）")
def test_cjk_source_key_does_not_500(tmp_path):
    """?source=<中文键> 必须走通：源键被写进 X-RSS-Single，而 Node 拒收非 latin1 头值。

    生产实测（2026-09-21 23:50 本地时间）：
        GET https://starhub-refresh.vercel.app/api/rss?source=安全客_664
        → 500 {"error":"Internal server error"}
    本地 handler 却返回 200 —— 差别在测试假 res.setHeader 不做校验。
    rss_sources.json 里 **542/968 个源键含中文**，抽屉对这一多半源"刷新没反应"
    （页面那段 fetch 带 .catch(function(){})，错误被静默吞掉）。
    """
    got = _run("cjk", tmp_path)
    assert got["count"] >= 2, "中文键源没拿到条目：%s" % json.dumps(got, ensure_ascii=False)[:200]
    assert "X-RSS-Single" in got["headersSet"], (
        "源键响应头没写成功 ⇒ 头值校验会把它打成 500：%s" % got["headersSet"])


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q", "-s"]))
