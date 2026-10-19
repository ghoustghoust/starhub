# -*- coding: utf-8 -*-
"""Task 3: 破茧栏 v2 — 非科技候选池 + 热度排序 + 类别兜底。"""
import sys
import os
import datetime
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
import build_daily_insight as bdi
if bdi.NOW_BJ is None:
    bdi.NOW_BJ = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=8)


def _chunk(title, tag, hot=0, days_ago=0, url=None):
    pub = (bdi.NOW_BJ - datetime.timedelta(days=days_ago)).isoformat()
    return {"title": title, "text": "正文" * 30, "topic_tag": tag, "hot": hot,
            "pub_date": pub, "url": url or ("https://u/" + title), "source": "test",
            "source_type": "rss"}


class TestBubbleV2:
    def setup_method(self):
        self.clusters = [{"label": "AI大事件", "category": "ai-models", "summary": "s",
                          "items": [{"url": "https://u/ai1", "title": "AI大事件"}]}]
        self.profile = {"ai-models": 10}

    def test_selects_non_tech_only(self):
        pool = [_chunk("央行宣布降准", "finance", hot=8),
                _chunk("某模型发布", "ai", hot=9)]
        out = bdi._select_bubble_events(self.clusters, self.profile, all_chunks=pool, top_n=5)
        assert [c["category"] for c in out] == ["finance"]  # ai 内容被拒

    def test_heat_ordering(self):
        pool = [_chunk("台风登陆", "society", hot=2), _chunk("大选开票", "geopolitics", hot=9)]
        out = bdi._select_bubble_events(self.clusters, self.profile, all_chunks=pool, top_n=2)
        assert out[0]["category"] == "geopolitics"

    def test_excludes_main_event_urls(self):
        pool = [_chunk("央行降准", "finance", hot=5, url="https://u/ai1")]
        out = bdi._select_bubble_events(self.clusters, self.profile, all_chunks=pool, top_n=5)
        assert all("ai1" not in c["url"] for c in out)

    def test_dedup_similar_titles(self):
        pool = [_chunk("央行宣布降准落地", "finance", hot=8),
                _chunk("央行宣布降准 释放流动性", "finance", hot=7)]
        out = bdi._select_bubble_events(self.clusters, self.profile, all_chunks=pool, top_n=5)
        assert len(out) == 1

    def test_card_schema(self):
        out = bdi._select_bubble_events(self.clusters, self.profile,
                                        all_chunks=[_chunk("诺贝尔文学奖揭晓", "culture")], top_n=1)
        assert set(out[0].keys()) >= {"label", "summary", "category", "score", "reason", "url", "source"}
        assert "科技" in out[0]["reason"] or "常读" in out[0]["reason"]

    def test_empty_pool_falls_back_to_cluster_categories(self):
        # 无 all_chunks 无 residual → 按 category 与 read_profile 新颖度从 clusters 兜底
        clusters = self.clusters + [{"label": "农业热点", "category": "agriculture",
                                     "summary": "s2", "items": []}]
        out = bdi._select_bubble_events(clusters, self.profile, top_n=1)
        assert len(out) == 1 and out[0]["category"] == "agriculture"

    def test_main_categories_never_bubble(self):
        # 对抗审查 P2-4：policy/funding 等 8 个主类别是 Phase 1 真实枚举，
        # 兜底路径不得把主事件原样复制进破茧栏
        clusters = self.clusters + [{"label": "融资新闻", "category": "funding",
                                     "summary": "s3", "items": [], "key_links": []},
                                    {"label": "监管新政", "category": "policy",
                                     "summary": "s4", "items": [], "key_links": []}]
        out = bdi._select_bubble_events(clusters, self.profile, top_n=3)
        assert all(c["category"] not in ("funding", "policy", "industry", "consumer",
                                         "research", "ai-models", "ai-products",
                                         "developer") for c in out)

    def test_null_label_no_crash(self):
        clusters = self.clusters + [{"label": None, "category": "agriculture",
                                     "summary": "s", "items": []}]
        out = bdi._select_bubble_events(clusters, self.profile, top_n=1)
        assert isinstance(out, list)  # 不抛 TypeError

    def test_stale_chunk_excluded(self):
        # 对抗审查 P2-5：all_chunks 实为 merged_chunks（含 7 天前缓存），
        # 超龄素材不得回潮上破茧栏
        pool = [_chunk("30天前的旧新闻", "finance", hot=9, days_ago=30),
                _chunk("昨日降准", "finance", hot=3, days_ago=1)]
        out = bdi._select_bubble_events(self.clusters, self.profile, all_chunks=pool, top_n=5)
        assert [c["label"] for c in out] == ["昨日降准"]
