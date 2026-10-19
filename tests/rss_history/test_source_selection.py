# tests/rss_history/test_source_selection.py
# -*- coding: utf-8 -*-
"""抓取策略守卫：每场构建抓取清单里的全部源，不做任何按时间的跳过。

Why: 跳过一旦生效，被跳过的源在页面上的内容就完全依赖 72 小时存档与抓取状态能否跨构建
存活 —— 而这两条持久化链路在 2026-09-17 同日断裂后，故障静默了两天才被查出。RSS 也没有
"只取新文章"的协议，跳过省下的只是请求数，代价是把正确性押在存档上。
这些断言不是测行为，是防止跳过机制被悄悄加回来。
"""
import io
import os
import sys
import types

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)

from _loader import load_build  # noqa: E402

mod = load_build()

BUILD = os.path.join(ROOT, "build_rss_aggregator.py")
WORKFLOW = os.path.join(ROOT, ".github", "workflows", "update.yml")


@pytest.fixture
def offline_build(tmp_path, monkeypatch):
    """让 main() 能整体跑一遍：全部源与网络调用改本地假实现。"""
    fetched = []
    srcs = [
        {"key": "alpha_1", "name": "Alpha", "cat": "ai", "url": "http://a/feed", "tier": 3},
        {"key": "beta_2", "name": "Beta", "cat": "news", "url": "http://b/feed", "tier": 1},
    ]
    now = mod._now_bj().isoformat()

    def fake_fetch(source, timeout=None):
        fetched.append(source["key"])
        return [{"title": "t-" + source["key"], "link": "http://x/" + source["key"],
                 "summary": "s", "pub_date": now}]

    monkeypatch.setattr(mod, "RSS_SOURCES", srcs)
    monkeypatch.setattr(mod, "_fetch_rss", fake_fetch)
    for name in ("_translate_source_items", "_tag_articles", "write_data_chunks",
                 "_save_api_snapshot", "_save_caches", "_load_caches", "_save_history",
                 "_audit_image_quality", "_accumulate_hot_history"):
        monkeypatch.setattr(mod, name, lambda *a, **k: None)
    monkeypatch.setattr(mod, "fetch_newsnow_snapshot", lambda *a, **k: [])
    monkeypatch.setattr(mod, "_run_analysis", lambda *a, **k: {})
    monkeypatch.setattr(mod, "build_html", lambda *a, **k: "<html></html>")
    monkeypatch.setattr(mod, "build_logger",
                        types.SimpleNamespace(append=lambda *a, **k: None, cleanup=lambda n: 0))
    # 兜底：门禁里任何漏 stub 的出网调用都立刻失败，而不是留下 35 秒超时与偶发红
    def _no_network(*a, **k):
        raise OSError("门禁不应发起网络请求：" + str(a[:1]))
    monkeypatch.setattr(mod.urllib.request, "urlopen", _no_network)
    monkeypatch.chdir(tmp_path)
    return fetched


def test_main_fetches_every_source_in_incremental_mode(offline_build):
    """真实跑一遍 main()：既验证"每场抓全部源"（含 T1），也是删除跳过时
    误删变量初始化那类崩溃的唯一防线（py_compile 与文本断言都查不出来）。"""
    mod.main(mode="incremental")
    assert sorted(offline_build) == ["alpha_1", "beta_2"], "有源未被抓取"


def test_main_fetches_every_source_in_full_mode(offline_build):
    mod.main(mode="full")
    assert sorted(offline_build) == ["alpha_1", "beta_2"]


def _main_source():
    text = io.open(BUILD, encoding="utf-8").read()
    start = text.index("def main(mode=")
    end = text.index('if __name__ == "__main__":', start)
    return text[start:end]


def test_no_source_is_skipped_by_time_or_history():
    src = _main_source()
    for gone in ("skipped_cached", "skipped_t1", "last_fetch", "_history_has_fresh_item"):
        assert gone not in src, "跳过机制回来了（%s），与既定策略冲突" % gone
    assert "_to_fetch" in src, "候选源列表缺失"


def test_fetch_state_mechanism_is_fully_removed():
    text = io.open(BUILD, encoding="utf-8").read()
    assert "_load_fetch_state" not in text and "rss_fetch_state" not in text, \
        "抓取状态机制只服务于跳过，留着就是没人读的死数据"
    assert not os.path.exists(os.path.join(ROOT, "rss_fetch_state.json"))


def test_ci_does_not_commit_fetch_state():
    text = io.open(WORKFLOW, encoding="utf-8").read()
    assert "rss_fetch_state" not in text


def test_ci_runs_this_gate_without_output_capture():
    """构建脚本导入时会重挂 sys.stdout；pytest 开着 fd 捕获时该对象回收会关掉共用 fd，
    整场 job 在收尾处报 Bad file descriptor。-s 是唯一让它 behave 如本地的开关。"""
    text = io.open(WORKFLOW, encoding="utf-8").read()
    seg = text.split("Quality gate A2")[1].split("- name:")[0]
    assert "-s" in seg, "门禁 A2 必须带 -s，否则 CI 收尾崩"
