# -*- coding: utf-8 -*-
"""R14 批2（审查复修）：降级深挖不得冒充"有深度"，越界序号不得挂旗。

依据链：
- 审查 P2-3：`_llm_phase2` 失败时返回 `{"status": "degraded"}`，但 `has_analysis_count`
  与历史落盘都只看"键在不在"。本会话整个 §2.2 的 `deep_share` 结论就是建立在这个字段上的 ——
  它虚高，结论就得重算。
- 审查 P2-5：judge 的编号域是终版榜 1..`MAX_EVENTS`，而 `_aggregate_per_event` 在草稿口径下
  拿到的 clusters 可能有 16 条。我上一轮刚加上"信任『事件N』序号前缀"，若不按 MAX_EVENTS 收口，
  幻觉出来的"事件14"会把旗子挂到根本没被评过的尾条上。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
import build_daily_insight as B

B.NOW_BJ = B._now_bj()


def _row(clusters):
    return B._build_tracking_entry({}, clusters, "T", 1.0, 10, 2, 1, 3, 100, 50, True)


def test_degraded_deep_does_not_count_as_analysis():
    cl = [{"label": "A", "deep_analysis": {"status": "degraded", "reason": "llm down"}},
          {"label": "B", "deep_analysis": {"status": "ok", "事件还原": "x"}}]
    assert _row(cl)["stats"]["has_analysis_count"] == 1, "degraded 计成有深挖会让 deep_share 虚高"


def test_missing_status_still_counts_as_analysis():
    """只排除显式 degraded，不给老数据/测试夹具加新罪。"""
    assert _row([{"label": "A", "deep_analysis": {"事件还原": "x"}}]
                )["stats"]["has_analysis_count"] == 1


def test_irrelevant_index_beyond_max_events_is_refused():
    n = B.MAX_EVENTS + 4
    cl = [{"label": "事件%d" % i, "items": []} for i in range(1, n + 1)]
    rows = B._aggregate_per_event(cl, [{"weak_events": [],
                                       "irrelevant_events": ["事件14（某幻觉条目）与 AI 无关"]}])
    assert all(r.get("irrelevant_flags", 0) == 0 for r in rows if r["index"] > B.MAX_EVENTS), \
        "judge 只评到第 %d 条，14 号旗不得挂到未评过的尾条" % B.MAX_EVENTS
    assert any(r.get("unmatched_irrelevant") for r in rows)


def test_index_within_max_events_still_works():
    cl = [{"label": "事件%d" % i, "items": []} for i in range(1, B.MAX_EVENTS + 1)]
    rows = B._aggregate_per_event(cl, [{"weak_events": [],
                                       "irrelevant_events": ["事件12（12306）与 AI 无关"]}])
    assert rows[11]["irrelevant_flags"] == 1
