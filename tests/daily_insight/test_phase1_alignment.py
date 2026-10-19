# -*- coding: utf-8 -*-
"""P0 回归：Phase 1 事件必须按 event_num 配对。
08:31 期实证错位（标题=美军登检船只，正文=Grok Voice Transcribe）：
LLM 跳过某事件后按数组下标整体错位一格。"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
import build_daily_insight as B

B.NOW_BJ = B._now_bj()


class _MockLLM:
    def __init__(self, payload):
        import json as _json
        self._p = _json.dumps(payload) if not isinstance(payload, str) else payload

    def complete(self, messages, temperature=0.3, max_tokens=2000):
        return self._p


def _clusters(n=4):
    return [{"label": "原标%d" % i, "summary": "", "score": 5.0 - i * 0.1,
             "items": [{"title": "t%d" % i, "text": "素材" * 30,
                        "source_type": "rss", "url": "https://x/%d" % i}],
             "source_types": ["rss"]} for i in range(n)]


def _mk(n, num=None):
    e = {"label": "LLM标%d" % n, "category": "research",
         "summary": "LLM摘要%d" % n, "significance": "意义%d" % n,
         "key_links": ["https://x/%d" % n]}
    if num is not None:
        e["event_num"] = num
    return e


def test_skipped_event_does_not_shift_the_rest():
    """LLM 漏答事件 3：cluster[2] 必须保持原文，cluster[3] 拿事件 4 的内容。"""
    c = _clusters(4)
    payload = {"theme": "T", "events": [_mk(1, 1), _mk(2, 2), _mk(4, 4)]}
    r = B._llm_phase1(_MockLLM(payload), c)
    assert r
    B._apply_phase1_result(c, r)
    assert c[0]["label"] == "LLM标1"
    assert c[1]["label"] == "LLM标2"
    assert c[2]["label"].startswith("原标"), "被跳过的事件不得吃邻居内容（错位根因）"
    assert B._is_insufficient(c[2]["summary"]), "缺号事件须带占位标记"
    assert c[3]["label"] == "LLM标4" and c[3]["summary"] == "LLM摘要4"


def test_string_and_out_of_order_event_num_normalized():
    c = _clusters(3)
    payload = {"theme": "T", "events": [_mk(2, "2"), _mk(1, 1), _mk(3, 3)]}
    r = B._llm_phase1(_MockLLM(payload), c)
    B._apply_phase1_result(c, r)
    assert c[0]["label"] == "LLM标1" and c[1]["label"] == "LLM标2" and c[2]["label"] == "LLM标3"


def test_legacy_unnumbered_falls_back_to_positional():
    c = _clusters(3)
    payload = {"theme": "T", "events": [_mk(1), _mk(2), _mk(3)]}
    r = B._llm_phase1(_MockLLM(payload), c)
    B._apply_phase1_result(c, r)
    assert c[0]["label"] == "LLM标1" and c[2]["label"] == "LLM标3"


def test_prompt_forbids_skipping_numbers():
    src = open(os.path.join(os.path.dirname(__file__), "..", "..",
                            "build_daily_insight.py"), encoding="utf-8").read()
    assert "event_num 必须与素材事件编号一一对应，禁止跳过或合并条目" in src


def test_zero_based_event_num_shifted_not_silently_off_by_one():
    """LLM 常见漂移：0-based 编号。旧实现会整列错位且只 warn 末尾。"""
    c = _clusters(3)
    payload = {"theme": "T", "events": [_mk(1, 0), _mk(2, 1), _mk(3, 2)]}
    r = B._llm_phase1(_MockLLM(payload), c)
    B._apply_phase1_result(c, r)
    assert c[0]["label"] == "LLM标1" and c[1]["label"] == "LLM标2" and c[2]["label"] == "LLM标3", \
        "0-based 编号须整体平移对齐，不得错位一格"


def test_missing_event_marked_insufficient_not_empty_card():
    """缺号事件保持原文=空正文卡；须打占位标记交给占位闸。"""
    c = _clusters(3)
    payload = {"theme": "T", "events": [_mk(1, 1), _mk(3, 3)]}  # 跳过 2
    r = B._llm_phase1(_MockLLM(payload), c)
    B._apply_phase1_result(c, r)
    assert B._is_insufficient(c[1].get("summary", "")), "缺号事件必须可被占位闸识别"
    assert c[0]["label"] == "LLM标1" and c[2]["label"] == "LLM标3"
