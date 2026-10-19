# -*- coding: utf-8 -*-
"""分块数量要由 chunk0 自己声明：加载器不许再靠"探到 404 为止"找边界。

线上形状（2026-10-01 08:0x Pages 版控制台）：`rss-data-10.js` 每次加载必 404。
不是数据坏了，是加载器写成"success 就 idx+1 继续，失败才收口" —— 块与块之间没有终点，
于是**每个访客都发一次注定 404 的请求**，还把 toast 挂在"出错"那条分支上。

判据两条方向：
  1. 生成端把总块数写进 chunk0 的 payload（写进 JSON，不许追加语句 ——
     test_chunk_budget._payload() 是按"整文件=一个 JSON"解析的，追加会打爆它）；
  2. 加载端按声明的块数收口；**读不到声明时（用户还拿着旧 chunk0 缓存）必须退回今天的探测行为**，
     否则老缓存用户会被停在第一块，那是比 404 严重得多的回归。
"""
import io
import json
import os
import re
import subprocess
import sys
import tempfile

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)

from _loader import load_build  # noqa: E402

mod = load_build()

BUILD = os.environ.get("RSS_BUILD_SRC") or os.path.join(ROOT, "build_rss_aggregator.py")

# 抽取加载器：_loadRest 活在 _bootWith(cs) 里，单独包一层才能当函数调用
L_START = "var _loadRest=function(){"
L_END = "      _loadNext(1);"


def _sources(n_sources=40, per=12, blob=900):
    out = []
    for i in range(n_sources):
        out.append({
            "key": "src_%d" % i, "name": "Src %d" % i, "cat": "ai", "tier": 2,
            "items": [{"title": "t%d-%d" % (i, j), "link": "http://x/%d-%d" % (i, j),
                       "summary": "s" * blob,
                       "pub_date": "2026-09-30T%02d:%02d:00+08:00" % (j % 24, j % 60)}
                      for j in range(per)],
        })
    return out


def _write(tmp_path, sources):
    old_out, old_cwd = mod.OUT, os.getcwd()
    try:
        mod.OUT = os.path.join(str(tmp_path), "rss-aggregator.html")
        os.chdir(str(tmp_path))
        mod.write_data_chunks(sources)
    finally:
        mod.OUT = old_out
        os.chdir(old_cwd)
    return [f for f in os.listdir(str(tmp_path)) if re.match(r"rss-data-\d+\.js$", f)]


def _payload_obj(path):
    text = io.open(path, encoding="utf-8").read()
    body = text[text.index("{", text.index("=")):].rstrip()
    if body.endswith(";"):
        body = body[:-1]
    return json.loads(body.replace("{sources:", '{"sources":'))


def test_chunk0_declares_the_number_of_files(tmp_path):
    files = _write(tmp_path, _sources())
    obj = _payload_obj(os.path.join(str(tmp_path), "rss-data-0.js"))
    assert "_total" in obj, (
        "chunk0 没声明块数，加载器只能靠 404 找终点（每个访客一次死请求）")
    assert obj["_total"] == len(files), (
        "声明的块数 %r != 实际文件数 %d —— 声明失真比不声明更糟，加载器会少合并尾巴"
        % (obj.get("_total"), len(files)))


def test_declared_total_survives_the_no_background_case(tmp_path):
    """只有首屏时也要声明：那时仍然写了空的 rss-data-1.js，块数是 2 不是 1。"""
    files = _write(tmp_path, _sources(n_sources=2, per=1, blob=8))
    obj = _payload_obj(os.path.join(str(tmp_path), "rss-data-0.js"))
    assert obj.get("_total") == len(files), (
        "无后台数据这一支的块数声明错了（文件 %r，声明 %r）" % (sorted(files), obj.get("_total")))


def _loader_src():
    text = io.open(BUILD, encoding="utf-8").read()
    i = text.find(L_START)
    assert i >= 0, "找不到 _loadRest 锚点，本判据失去意义"
    j = text.find(L_END, i)
    assert j > i, "找不到 _loadNext(1) 结束锚点"
    return text[i:j + len(L_END)] + "\n    };"


def _run_loader(total=None, n_files=4):
    """用 node 真跑 _loadRest：记录它请求了哪些块号。"""
    chunk0 = ("{sources:[], _total:%d}" % total) if total is not None else "{sources:[]}"
    src = """
var ART = [], requested = [], merged = 0, toasts = [];
var window = { __CHUNKS: [%s] };
function _mergeChunk(c){ merged += (c && c.sources) ? c.sources.length : 0; return 1; }
function toast(m){ toasts.push(m); }
function loadChunk(i){
  requested.push(i);
  if (window.__CHUNKS[i]) return Promise.resolve();
  if (i < %d) { window.__CHUNKS[i] = {sources:[]}; return Promise.resolve(); }
  return Promise.reject(new Error('404'));
}
function _bootWith(){
%s
  _loadRest();
}
_bootWith();
setTimeout(function(){
  process.stdout.write(JSON.stringify({requested: requested, toasts: toasts}) + "\\n");
}, 300);
""" % (chunk0, n_files, _loader_src())
    p = os.path.join(tempfile.gettempdir(), "_chunk_total_wire.js")
    io.open(p, "w", encoding="utf-8").write(src)
    r = subprocess.run(["node", p], capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=60)
    assert r.returncode == 0, "node 判据本身崩了：%s\n%s" % (r.stdout[-1200:], r.stderr[-1200:])
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_loader_stops_at_declared_total():
    got = _run_loader(total=4, n_files=4)
    assert 4 not in got["requested"], (
        "仍去请求第 4 块（共 4 块，索引 0..3）：那次 404 就是控制台的孤儿请求 —— %r"
        % got["requested"])
    assert got["requested"] == [1, 2, 3], "该合并的块没合并全：%r" % got["requested"]


def test_loader_reports_completion_without_an_error():
    """toast 现在挂在"出错"分支上；按声明收口后，正常路径也该有完成提示。"""
    got = _run_loader(total=4, n_files=4)
    assert got["toasts"], "全部块正常合并完却没有任何完成提示（提示只存在于 404 分支）"
    assert 4 not in got["requested"], (
        "完成提示是靠 404 分支发出来的，不是靠按声明收口 —— 这条判据不许靠错误路径蒙过")


def test_loader_falls_back_to_probing_for_stale_chunk0():
    """用户手里还缓存着没有 _total 的旧 chunk0 时，必须退回今天的探测式加载。

    方向别搞反：如果按"读不到 _total 就只加载 chunk0"来实现，老缓存用户会只剩首屏 ——
    那正是 2026-09-30 那次"页面只有 61 个源"的形状。
    """
    got = _run_loader(total=None, n_files=4)
    assert got["requested"][:3] == [1, 2, 3], (
        "没有 _total 时不再后台合并了（老缓存用户会被砍成只剩首屏）：%r" % got["requested"])
