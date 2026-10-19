# -*- coding: utf-8 -*-
"""R14-9B(无需产品决策的那一半)：深度名额必须给 editor_score 最高的 N 条。

依据链：
- 第 9/10 期生产产物：终版榜**第 1 名（ed=68，全场唯一高于地板）没有 `deep_analysis`**，
  而有深度解读的是三条 ed=62 的条目；
- 成因是 `main()` 里 Phase 2 取 `clusters[0:top_n]`（**Phase 1 输出序**），
  而"按 editor_score 重排"发生在 `:5417` 的 `_order_events_for_output` —— 排序晚于名额分配；
- 后果落在 cov/rel 上：只有被深挖的条目才有"事件还原/影响分析/后续展望"，
  第 1 名没深挖 = 报告最重要的那条最薄。
这个改动不增减槽位数、不改页面长度，但**实际 Phase 2 调用次数只增不减**
（快筛不再因 Phase 1 排位空耗名额：第 6 期那种 deep=0 的场次会变成最多 3 次）。
"""
import ast
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
import build_daily_insight as B

B.NOW_BJ = B._now_bj()

_SRC = open(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "build_daily_insight.py"), encoding="utf-8").read()


def _cl(scores):
    return [{"label": "事件%d" % i, "editor_score": s, "items": [], "summary": "摘要"}
            for i, s in enumerate(scores)]


def test_picks_highest_scores_capped_at_top_n():
    assert B._deep_candidate_order(_cl([55, 90, 62, 71, 80])) == [1, 4, 3]
    assert B.DEEP_ANALYSIS_TOP_N == 3, "本测试的期望值按 TOP_N=3 写死，改预算要同步改测试"


def test_never_returns_more_than_top_n_or_len():
    assert len(B._deep_candidate_order(_cl([90, 80, 70, 60, 50]))) == B.DEEP_ANALYSIS_TOP_N
    assert len(B._deep_candidate_order(_cl([90, 80]))) == 2
    assert B._deep_candidate_order([]) == []


def test_period9_shape_gives_depth_to_the_leader():
    """生产实形：Phase1 把 68 分那条排在第 4 位，前 3 位都是 62 分。
    旧实现取 clusters[0:3] → 68 分那条拿不到深度解读；新实现必须把它选进来。"""
    clusters = _cl([62, 62, 62, 68, 62, 62])
    got = B._deep_candidate_order(clusters)
    assert 3 in got, "第 4 位的最高分条目必须拿到深度名额，got=%s" % got
    assert got[0] == 3, "它还得排第一，got=%s" % got


def test_ties_keep_phase1_order():
    """并列分不得随机换位（第 9/10 期有 9 条并列 62 分，换位会让构建不可复现）。"""
    assert B._deep_candidate_order(_cl([62, 62, 62, 62, 62])) == [0, 1, 2]
    assert B._deep_candidate_order(_cl([70, 62, 62, 62])) == [0, 1, 2]


def test_missing_or_none_score_is_zero_not_crash():
    cl = [{"label": "A", "items": []}, {"label": "B", "editor_score": None, "items": []},
          {"label": "C", "editor_score": 61, "items": []}]
    assert B._deep_candidate_order(cl) == [2, 0, 1]


def test_nan_score_cannot_grab_the_first_slot():
    """`min(100, nan)==100`：NaN 若不拦会变成最高分抢走第一个深度名额
    （与 R14 批1 在 judge 侧修掉的是同族缺陷，这侧此前无闸）。"""
    cl = _cl([60, 0, 60])
    cl[1]["editor_score"] = float("nan")
    assert B._deep_candidate_order(cl) == [0, 2, 1], "NaN 必须按 0 分排到最后"
    cl2 = _cl([60, 60])
    cl2[0]["editor_score"] = True
    assert B._deep_candidate_order(cl2) == [1, 0], "bool 不是分数"
    assert B._deep_candidate_order(["hello", {"label": "x", "editor_score": 60}]) == [1, 0]


def test_main_wires_phase2_to_the_helper_not_range():
    """防回退必须用 AST，不能用文本子串。

    审查实测三种回退都能穿透文本断言而全量绿：
      M1 把 `_deep_idx = _deep_candidate_order(clusters)` 挪到打分循环之前（此时全簇无分数→并列→退回 Phase1 序）；
      M2 保留 helper 调用但丢弃结果，`_deep_idx = list(range(min(DEEP_ANALYSIS_TOP_N, ...)))`；
      M3 只加一行含 `for i in range(top_n):` 的注释 → 反向断言假红。
    AST 版本同时锁"来源是 helper 调用"与"赋值晚于打分"，且看不见注释。
    """
    fn = next(n for n in ast.parse(_SRC).body
              if isinstance(n, ast.FunctionDef) and n.name == "main")
    loops = [n for n in ast.walk(fn)
             if isinstance(n, ast.For)
             and any(isinstance(x, ast.Name) and x.id == "MIN_DEEP_SCORE" for x in ast.walk(n))]
    assert len(loops) == 1, "深挖选择循环应当唯一，实得 %d 个" % len(loops)
    loop = loops[0]
    assert isinstance(loop.iter, ast.Name), "Phase 2 必须遍历下标变量，实得 %r" % type(loop.iter).__name__
    name = loop.iter.id
    feeder = [n for n in ast.walk(fn) if isinstance(n, ast.Assign)
              and any(isinstance(t, ast.Name) and t.id == name for t in n.targets)
              and isinstance(n.value, ast.Call)
              and getattr(n.value.func, "id", None) == "_deep_candidate_order"]
    assert feeder, "%s 必须由 _deep_candidate_order(...) 赋值，不得由 range/list(range) 供数" % name
    scorer = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
              and getattr(n.func, "id", None) == "_quick_score_event"]
    assert scorer, "找不到 editor_score 打分点，无法校验时序"
    assert feeder[0].lineno > min(n.lineno for n in scorer), \
        "名额分配必须晚于 editor_score 刷新，否则全簇并列按 0 分→退回 Phase1 序"
