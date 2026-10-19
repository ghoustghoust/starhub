# -*- coding: utf-8 -*-
"""R14：irrelevant_events 的归属必须真能落到事件上（R13 阶段 C 的实测缺陷）.

依据链：
- 阶段 C 上线后第 8 期生产实测（run 22:11 CN）：judge 点了 3 条无关事件，
  `per_event[].irrelevant_flags` 全为 0，三条全部堆进 `unmatched_irrelevant: 3`；
- 原因是两处：① judge 写的是「事件12（铁路12306…）」这种**带序号前缀**的自由文本，
  而代码只做标签相似度，序号白给不用；② 用 Jaccard 拿"整段理由"比"短标签"，
  分母被理由撑大，交集再准也过不了 0.2 门槛 —— 该用包含度（overlap/|标签|）。
- 错误的归因比没有归因更坏，但**永远归不上等于这一半埋点白做**：所以序号前缀优先、文本包含兜底，
  两者都不可信时仍然落进 unmatched 行。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
import build_daily_insight as B

B.NOW_BJ = B._now_bj()


def _clusters(n=12):
    labels = ["OpenAI 发布新模型", "AI 幻觉假情报", "Neuralink 失语患者意念发声",
              "华为昇腾加速路线图", "LLM 监督同行评审", "路透社深度报道",
              "AI 生成女演员采访", "Gary Marcus 讽刺泡沫", "A16Z 投资 Vals",
              "Excel 建议更易被接受", "Claude 兼容 OpenAI", "12306 拒绝恶意抢票"]
    out = []
    for i in range(n):
        lab = labels[i] if i < len(labels) else "事件%d" % i
        out.append({"label": lab, "items": [{"source_type": "rss", "url": "http://a/%d" % i}],
                    "summary": "摘要" * 20})
    return out


def _rows(clusters, texts):
    return B._aggregate_per_event(clusters, [{"weak_events": [], "irrelevant_events": texts}])


def _flags(rows):
    return {r["index"]: r.get("irrelevant_flags", 0) for r in rows if r["index"] != 0}


def test_index_prefix_attributes_directly():
    """生产原话：「事件12（铁路12306…）」—— 序号是 judge 自己给的，必须优先采用。"""
    cl = _clusters()
    rows = _rows(cl, ["事件12（铁路12306打击恶意抢票）：属于纯商业/民生话题，与主题无关，占据篇幅"])
    assert _flags(rows).get(12) == 1, _flags(rows)
    assert not any(r["index"] == 0 for r in rows), "已归属就不该再留 unmatched 行"


def test_english_index_prefix():
    rows = _rows(_clusters(), ["Event 2: an opinion column unrelated to AI"])
    assert _flags(rows).get(2) == 1, _flags(rows)


def test_index_prefix_is_1_based_and_range_checked():
    cl = _clusters(3)
    rows = _rows(cl, ["事件 99 与 AI 无关"])   # 越界序号不得写进不存在的行
    assert all(r["index"] != 99 for r in rows)
    assert any(r.get("unmatched_irrelevant") for r in rows), "越界必须落回 unmatched"
    assert sum(_flags(rows).values()) == 0


def test_long_reason_matches_by_containment_not_jaccard():
    """没有序号时靠文本兜底：理由长、标签短，Jaccard 必然被分母压死，要用包含度。"""
    cl = _clusters()
    rows = _rows(cl, ["路透社发布深度报道回顾改变AI进程的十天 这类二手综述文章不应占据主榜篇幅，"
                      "它只是对过去十天行业事件的回顾性总结，没有新增事实与信源"])
    assert _flags(rows).get(6) == 1, _flags(rows)


def test_ambiguous_text_still_refuses_to_guess():
    """同名/近义两条并存时不得按序号硬塞 —— 这条守住阶段 C 当初写死的余量规则。"""
    cl = [{"label": "阿里发布全模态Agent模型Qwen3.8", "items": []},
          {"label": "阿里发布Qwen3.8全模态模型", "items": []}]
    rows = _rows(cl, ["阿里发布全模态模型 属于重复报道"])
    assert any(r.get("unmatched_irrelevant") for r in rows), "二义性必须落 unmatched"


def test_counting_survives_across_three_samples():
    cl = _clusters()
    samples = [{"irrelevant_events": ["事件12（12306）无关"]},
               {"irrelevant_events": ["事件12 民生话题"]},
               {"irrelevant_events": ["12306 拒绝恶意抢票 与 AI 无关"]}]
    rows = B._aggregate_per_event(cl, samples)
    assert _flags(rows).get(12) == 3, _flags(rows)
