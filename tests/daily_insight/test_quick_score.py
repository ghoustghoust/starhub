# -*- coding: utf-8 -*-
"""Task 5: 事件快筛评分（BestBlogs v4 权重 40/30/20/10）。"""
import sys
import os
import datetime
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
import build_daily_insight as bdi
if bdi.NOW_BJ is None:
    bdi.NOW_BJ = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=8)


def _item(src_type, tag, text="发布了 3 款产品，融资 1.2 亿美元", hours=2, url=""):
    pub = (bdi.NOW_BJ - datetime.timedelta(hours=hours)).isoformat()
    return {"source_type": src_type, "topic_tag": tag, "text": text,
            "title": "某事件", "pub_date": pub, "url": url or ("https://u/" + src_type + str(hours))}


class TestQuickScore:
    def test_strong_event_above_gate(self):
        items = ([_item("rss", "ai", url="https://a/%d" % i) for i in range(4)]
                 + [_item("agihunt", "ai"), _item("hot", "ai"), _item("aihot", "ai")])
        cluster = {"label": "OpenAI 发布 GPT-6", "items": items}
        assert bdi._quick_score_event(cluster) >= bdi.MIN_DEEP_SCORE

    def test_single_item_event_below_gate(self):
        cluster = {"label": "小消息", "items": [_item("rss", "ai", text="一句话", hours=1)]}
        assert bdi._quick_score_event(cluster) < bdi.MIN_DEEP_SCORE

    def test_repeat_deducted(self):
        items = [_item("rss", "ai", url="https://a/%d" % i) for i in range(3)]
        cluster = {"label": "OpenAI 发布 GPT-6", "items": items}
        base = bdi._quick_score_event(cluster)
        repeat = bdi._quick_score_event(cluster, yesterday_labels={"OpenAI 发布 GPT-6"})
        assert repeat <= base - 10

    def test_identical_urls_deducted(self):
        items = [_item("rss", "ai", url="https://same/1") for _ in range(4)]
        cluster = {"label": "同质事件", "items": items}
        no_dedup = bdi._quick_score_event(cluster)
        # 4 素材全同 URL：raw=79.8 减 10 分 → ≤70
        assert no_dedup <= 70

    def test_score_bounded(self):
        items = [_item(t, "ai", url="https://x/%s%d" % (t, i))
                 for t in ("rss", "hot", "aihot", "agihunt") for i in range(4)]
        cluster = {"label": "超级大事件，营收 30 亿美元，增长 150%", "items": items}
        assert 0 <= bdi._quick_score_event(cluster) <= 100

    def test_legacy_chunks_without_topic_tag_neutral(self):
        # 旧缓存 chunk 无 topic_tag → 相关性给中性分，不应清零
        items = []
        for st in ("rss", "hot", "aihot", "agihunt"):
            it = _item(st, "ai")
            del it["topic_tag"]
            items.append(it)
        cluster = {"label": "旧素材事件", "items": items}
        with_tag = bdi._quick_score_event({"label": "旧素材事件",
                                           "items": [_item(s, "ai", url="https://n/" + s) for s in ("rss", "hot", "aihot", "agihunt")]})
        legacy = bdi._quick_score_event(cluster)
        assert abs(with_tag - legacy) <= 12  # 中性 vs 全 main 的差距有限

    def test_empty_cluster_zero(self):
        assert bdi._quick_score_event({"label": "x", "items": []}) == 0

    def test_non_tech_topics_lower_relevance(self):
        items = [_item("rss", "finance", url="https://f/%d" % i) for i in range(4)]
        cluster = {"label": "财经事件", "items": items}
        assert bdi._quick_score_event(cluster) <= 85

    def test_null_label_no_crash(self):
        # 对抗审查 P1-1：label 为 None（LLM 输出 null 时 :4203 直接赋 None）
        items = [_item("rss", "ai", url="https://a/%d" % i) for i in range(3)]
        cluster = {"label": None, "items": items}
        assert bdi._quick_score_event(cluster, yesterday_labels={"X"}) >= 0

    def test_low_tag_coverage_neutral(self):
        # 对抗审查 P1-2：缓存过渡期 8 条旧无 tag + 1 条标 other，
        # 不能让唯一一条新标记把相关性打到 0 而误杀深度分析
        items = []
        for i in range(7):
            it = _item("rss", "ai", url="https://mix/%d" % i)
            del it["topic_tag"]
            items.append(it)
        for st in ("hot", "aihot"):
            it = _item(st, "ai", url="https://mix/" + st)
            del it["topic_tag"]
            items.append(it)
        items.append(_item("agihunt", "other", url="https://mix/new"))
        cluster = {"label": "过渡期事件", "items": items}
        assert bdi._quick_score_event(cluster) >= bdi.MIN_DEEP_SCORE
