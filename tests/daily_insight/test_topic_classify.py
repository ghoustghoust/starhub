# -*- coding: utf-8 -*-
"""Task 2: 主题分类层。"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
import build_daily_insight as bdi


class TestClassifyTopic:
    def test_ai_hits_main(self):
        assert bdi._classify_topic("OpenAI 发布 GPT-6 大模型", "", "") == "ai"

    def test_ai_covers_chip_vendor(self):
        assert bdi._classify_topic("英伟达发布新一代 GPU 训练芯片", "", "") == "ai"

    def test_programming_hits_main(self):
        assert bdi._classify_topic("GitHub 推出新开源框架", "", "") == "programming"

    def test_finance_hits_bubble(self):
        assert bdi._classify_topic("央行宣布降准 0.5 个百分点", "", "") == "finance"

    def test_geopolitics_hits_bubble(self):
        assert bdi._classify_topic("联合国安理会召开紧急会议讨论制裁", "", "") == "geopolitics"

    def test_society_hits_bubble(self):
        assert bdi._classify_topic("高考报名人数再创历史新高", "", "") == "society"

    def test_culture_hits_bubble(self):
        assert bdi._classify_topic("诺贝尔文学奖揭晓", "", "") == "culture"

    def test_no_hit_is_other(self):
        assert bdi._classify_topic("今天天气不错", "", "") == "other"

    def test_ai_priority_over_finance(self):
        # 优先级：ai 在 finance 前，"英伟达股价"类科技金融交叉事件留在主报告
        assert bdi._classify_topic("英伟达股价创历史新高", "", "") == "ai"

    def test_rss_cat_ai_hint(self):
        # 无关键词命中但 RSS cat=ai → ai
        assert bdi._classify_topic("某条短讯", "", "", cat="ai") == "ai"

    def test_rss_cat_news_stays_other(self):
        # cat=news 不给主报告暗示，保持 other（破茧要求正向识别）
        assert bdi._classify_topic("某条短讯", "", "", cat="news") == "other"

    def test_bubble_and_main_topics_partitioned(self):
        assert bdi.MAIN_TOPICS.isdisjoint(bdi.BUBBLE_TOPICS)
        assert "finance" in bdi.BUBBLE_TOPICS and "ai" in bdi.MAIN_TOPICS

    def test_taxonomy_covers_all_declared_topics(self):
        declared = {tag for tag, _ in bdi.TOPIC_TAXONOMY}
        assert bdi.MAIN_TOPICS | bdi.BUBBLE_TOPICS == declared


class TestChunkTopicTag:
    def test_chunk_documents_tags_all_chunks(self):
        rss = [{"title": "央行宣布降准", "link": "https://a/1", "source": "s",
                "summary": "", "full_content": "", "pub_date": "", "cat": "news",
                "source_key": "k"}]
        hot = [{"title": "OpenAI 发布新模型", "url": "https://b/2", "platform": "weibo"}]
        chunks = bdi._chunk_documents(rss, hot, [], [])
        assert chunks and chunks[0]["topic_tag"] == "finance"
        assert any(c.get("topic_tag") == "ai" for c in chunks[1:])

    def test_rss_cat_hint_propagates(self):
        rss = [{"title": "某条无关键词短讯", "link": "https://a/2", "source": "s",
                "summary": "", "full_content": "", "pub_date": "", "cat": "ai",
                "source_key": "k"}]
        chunks = bdi._chunk_documents(rss, [], [], [])
        assert chunks[0]["topic_tag"] == "ai"
