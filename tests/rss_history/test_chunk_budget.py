# tests/rss_history/test_chunk_budget.py
# -*- coding: utf-8 -*-
"""后台分块必须有体积上限：一块巨石 = 慢网络取不到 = 页面只剩首屏那几十源。

真实事故（2026-09-30 线上取证）：`MAX_CHUNK_BYTES` 写的是 80MB，而后台数据实测 50.6MB
⇒ `n_chunks` 恒等于 1 ⇒ "分块"实际只有一块 60,378,325 B 的 `rss-data-1.js`。
浏览器取不到这一整块时，script 的 onload 照样触发但 `__CHUNKS[1]` 从未被赋值，
页面只剩 chunk0 首屏的 61 个源，控制台明写
`[starhub] chunk1 onload 但 __CHUNKS[1] 缺失（文件截断或内容异常）`。
所以这里的判据不是"分了几块"好看，而是：**任何一块都不许大到取不动，且拆分不许丢条目。**
"""
import io
import json
import os
import re
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)

from _loader import load_build  # noqa: E402

mod = load_build()

# 每块的硬上限：比 MAX_CHUNK_BYTES 留 2MB 余量（转义与 JS 包装会撑大一点）
HARD_CEILING = 8 * 1024 * 1024


def _sources(n_sources=700, per=15, blob=900):
    """造约 50MB 的数据：与 2026-09-30 实测的"699 源 10293 篇 50.6 MB"同量级。"""
    out = []
    for i in range(n_sources):
        out.append({
            "key": "src_%d" % i, "name": "Src %d" % i, "cat": "ai", "tier": 2,
            "items": [{"title": "t%d-%d" % (i, j),
                       "link": "http://x/%d-%d" % (i, j),
                       "summary": "s" * blob,
                       "pub_date": "2026-09-30T%02d:%02d:00+08:00" % (j % 24, j % 60)}
                      for j in range(per)],
        })
    return out


def _write(tmp_path, sources, chunk0_size=None):
    old_out = mod.OUT
    old_cwd = os.getcwd()
    try:
        mod.OUT = os.path.join(str(tmp_path), "rss-aggregator.html")
        os.chdir(str(tmp_path))
        if chunk0_size is None:
            mod.write_data_chunks(sources)
        else:
            mod.write_data_chunks(sources, chunk0_size=chunk0_size)
    finally:
        mod.OUT = old_out
        os.chdir(old_cwd)
    return sorted(f for f in os.listdir(str(tmp_path)) if re.match(r"rss-data-\d+\.js$", f))


def _payload(path):
    text = io.open(path, encoding="utf-8").read()
    body = text[text.index("{", text.index("=")):].rstrip()
    if body.endswith(";"):
        body = body[:-1]
    try:
        return json.loads(body)
    except ValueError:
        return json.loads(body.replace("{sources:", '{"sources":'))


def test_background_data_is_split_into_many_small_chunks(tmp_path):
    src = _sources()
    files = _write(tmp_path, src)
    sizes = {f: os.path.getsize(os.path.join(str(tmp_path), f)) for f in files}
    bg = [f for f in files if f != "rss-data-0.js"]
    assert len(bg) > 1, (
        "后台数据只出了一块 %r，等于没分块：慢网络取不到就只剩首屏源" % sizes)
    for f, sz in sizes.items():
        assert sz <= HARD_CEILING, "%s = %.1f MB，超过单块硬上限 %.1f MB" % (
            f, sz / 1048576.0, HARD_CEILING / 1048576.0)


def test_splitting_loses_no_item(tmp_path):
    src = _sources()
    want = sum(len(s["items"]) for s in src)
    files = _write(tmp_path, src)
    got = 0
    for f in files:
        for s in _payload(os.path.join(str(tmp_path), f)).get("sources", []):
            got += len(s.get("items", []))
    assert got == want, "拆分丢了 %d 篇（出厂 %d / 应为 %d）" % (want - got, got, want)


def test_every_chunk_is_closed_and_indexes_its_slot(tmp_path):
    """每块都必须以 `;` 收尾且给正确的槽位赋值 —— 截断的块正是线上那句"文件截断"的形状。"""
    files = _write(tmp_path, _sources())
    for name in files:
        idx = int(re.search(r"rss-data-(\d+)\.js", name).group(1))
        raw = io.open(os.path.join(str(tmp_path), name), encoding="utf-8").read()
        assert raw.rstrip().endswith(";"), "%s 没有以分号收尾（疑似截断）" % name
        assert ("(window.__CHUNKS=window.__CHUNKS||[])[%d]=" % idx) in raw, (
            "%s 没给 __CHUNKS[%d] 赋值，页面取不到它" % (name, idx))


def test_chunk_count_is_clamped(tmp_path, monkeypatch):
    """块数有上限：数据再大也不许一次生成上百个文件（staging 与提交都要扫这些名字）。"""
    monkeypatch.setattr(mod, "CHUNK0_SIZE", 1)
    files = _write(tmp_path, _sources(n_sources=1200, per=20, blob=1200), chunk0_size=1)
    assert len(files) - 1 <= 24, "后台块数失控：%d 块" % (len(files) - 1)
