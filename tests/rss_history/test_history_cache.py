# tests/rss_fetch_state/test_history_cache.py
# -*- coding: utf-8 -*-
"""增量跳过依赖的 72h 历史必须能跨构建存活（CI 用 actions/cache 承载分块历史）。

背景: rss_history 超 40MB 后 _save_history_chunked 只写 rss_history_N.json + index,
而这两个产物从未进 CI 提交列表, 于是 git 里的 rss_history.json 冻结在 2026-09-17,
每次构建读到的是全超龄历史 → 新鲜度守卫让跳过永不触发 → 修复只剩防呆没有省时。

同时守住一个会被缓存暴露出来的缺陷: 历史一旦缩到 40MB 以下, 老实现改写主文件却不清
上一代的 chunk+index, 而 _load_history_chunked 优先信 index → 刚写的主文件被忽略。
"""
import io
import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)

from _loader import load_build  # noqa: E402

mod = load_build()

WORKFLOW = os.path.join(ROOT, ".github", "workflows", "update.yml")


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    # _loader 会缓存模块（同进程二次 exec 会硬崩），_rss_history 是模块级全局，
    # 不重置的话跨测试文件串味
    mod._rss_history = {}
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _hist(n, day="2026-09-19"):
    return {
        "http://x/%d" % i: {
            "link": "http://x/%d" % i, "source_key": "src_a", "cat": "ai",
            "title": "t%d" % i, "pub_date": "%sT08:00:00+08:00" % day,
        } for i in range(n)
    }


def test_history_roundtrips_through_chunk_layout(workdir):
    mod._rss_history = _hist(50)
    mod._save_history()
    assert os.path.exists("rss_history_index.json"), "历史未写 index，CI 缓存无从恢复"
    assert mod._load_history_chunked() == _hist(50)


def test_small_history_does_not_leave_stale_chunks(workdir):
    """缩到阈值以下时，上一代 chunk/index 必须清掉，否则新写的主文件被 index 遮蔽。"""
    mod._rss_history = _hist(60)
    mod._save_history()
    mod._rss_history = _hist(3)
    mod._save_history()
    assert mod._load_history_chunked() == _hist(3)


def test_load_history_chunked_reads_cache_restored_layout(workdir):
    """actions/cache 还原的是磁盘文件，这里直接摆出还原后的目录形状验证可读。"""
    (workdir / "rss_history_0.json").write_text(json.dumps(_hist(2)), encoding="utf-8")
    (workdir / "rss_history_1.json").write_text(
        json.dumps({"http://x/9": {"link": "http://x/9", "source_key": "src_b",
                                   "pub_date": "2026-09-19T09:00:00+08:00"}}),
        encoding="utf-8")
    (workdir / "rss_history_index.json").write_text(
        json.dumps({"chunks": 2, "total": 3}), encoding="utf-8")
    merged = mod._load_history_chunked()
    assert len(merged) == 3
    assert {v["source_key"] for v in merged.values()} == {"src_a", "src_b"}


def test_empty_index_does_not_resurrect_stale_base_file(workdir):
    """有 index 就以 index 为准：回落到主文件会把冻结的旧历史当有效数据读回来。"""
    (workdir / "rss_history_0.json").write_text("{}", encoding="utf-8")
    (workdir / "rss_history_index.json").write_text(
        json.dumps({"chunks": 1, "total": 0}), encoding="utf-8")
    (workdir / "rss_history.json").write_text(
        json.dumps(_hist(2, day="2026-09-14")), encoding="utf-8")
    assert mod._load_history_chunked() == {}


def test_shrinking_chunk_count_removes_old_chunks(workdir):
    """块数变少时旧 chunk 必须删掉：缓存会整代还原，日后块数再涨回去时
    残留文件会被新 index 一并读回，把已淘汰的文章复活。"""
    big = _hist(400)
    mod._rss_history = big
    payload = len(json.dumps(big, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    max_size = payload // 2
    mod._save_history_chunked(max_size=max_size)
    assert os.path.exists("rss_history_1.json"), "前置条件未成立：需要 2 块"

    mod._rss_history = _hist(4)
    mod._save_history_chunked(max_size=max_size)
    assert not os.path.exists("rss_history_1.json")
    assert not os.path.exists("rss_history_2.json")
    assert mod._load_history_chunked() == _hist(4)


def test_partial_chunk_set_is_treated_as_no_history(workdir):
    """缓存只还原了一半分块时不能当有效历史用：会把静默缺文当成正常数据回写。"""
    big = _hist(200)
    payload = len(json.dumps(big, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    mod._rss_history = big
    mod._save_history_chunked(max_size=payload // 2)
    assert os.path.exists("rss_history_1.json"), "前置条件未成立：需要 2 块"
    assert mod._load_history_chunked() == big
    os.remove("rss_history_1.json")
    assert mod._load_history_chunked() == {}, "半套历史必须按无历史处理，而不是少一半照用"


def test_ci_caches_history_chunks():
    text = io.open(WORKFLOW, encoding="utf-8").read()
    for step in ("Restore RSS history cache", "Save RSS history cache"):
        seg = text.split(step)[1].split("- name:")[0]
        assert "rss_history_*.json" in seg, "%s 未缓存分块本体" % step
        assert "rss_history_index.json" in seg, "%s 未缓存 index" % step
    assert text.index("Save RSS history cache") < text.index("Prune large build artifacts"), \
        "Save 必须在 prune 删掉分块之前"


def test_ci_save_history_cache_is_best_effort():
    """配额实测 7.05/10GB 且每场新增一条键：保存失败不能连坐 Vercel 部署。"""
    text = io.open(WORKFLOW, encoding="utf-8").read()
    seg = text.split("Save RSS history cache")[1].split("- name:")[0]
    assert "continue-on-error: true" in seg, "Save 步失败会阻断 Deploy，造成 Pages 与 API 分叉"
    assert "github.run_attempt" in seg, "重跑同 run 时键已存在会静默不保存"


def test_ci_no_longer_commits_base_history_file():
    """历史改由缓存承载后，64MB 主文件再进提交列表会把 .git 重新撑爆。"""
    text = io.open(WORKFLOW, encoding="utf-8").read()
    add_lines = [l.strip() for l in text.splitlines() if l.strip().startswith("git add ")]
    assert add_lines
    for line in add_lines:
        assert " rss_history.json" not in line, "rss_history.json 不应再提交: %s" % line[:120]
    ignored = io.open(os.path.join(ROOT, ".gitignore"), encoding="utf-8").read().splitlines()
    ignored = [l.strip() for l in ignored if l.strip() and not l.strip().startswith("#")]
    assert "rss_history.json" in ignored
