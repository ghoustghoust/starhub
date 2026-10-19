# -*- coding: utf-8 -*-
"""G2: 素材正文优先 + 近似转述去重 + 证据包不重复注入。"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
import build_daily_insight as B

B.NOW_BJ = B._now_bj()

FULL = ("英伟达发布Rubin GPU架构，单卡FP4算力达到50PFLOPS，HBM4显存容量288GB。"
        "发布会现场黄仁勋表示该架构将于2026年第四季度量产，首批客户包括微软与Meta。"
        "台积电3nm工艺良率已提升至92%，成本较Blackwell下降约18%。" * 8)

RSS_BY = {B._norm_url("https://r/1"): {"source": "test", "title": "Rubin发布",
            "full_content": FULL, "summary_zh": ""}}


def _item(u, body):
    return {"source_type": "rss", "source": "s", "title": "标题", "url": u, "text": body}


def test_fulltext_replaces_800_char_feed():
    cluster = {"label": "L", "items": [_item("https://r/1", "英伟达发布Rubin。" + "导语" * 300)]}
    mat = B._build_event_material(cluster, rss_by_url=RSS_BY)
    assert "HBM4显存容量288GB" in mat, "命中 full_content 时素材应使用正文主体"
    assert len(mat) > 800


def test_no_rss_by_url_keeps_legacy_behavior():
    cluster = {"label": "L", "items": [_item("https://r/1", "短导语" * 400)]}
    mat = B._build_event_material(cluster)
    assert "HBM4" not in mat


def test_paraphrase_dedup_keeps_one():
    body = "OpenAI发布GPT-5.6支持百万上下文窗口定价每百万token输入2.5美元输出12美元"
    cluster = {"label": "L", "items": [
        _item("https://a/1", body + "详细报道一" * 8),
        _item("https://a/2", body + "详细报道二" * 8),
        _item("https://a/3", body + "详细报道三" * 8),
        _item("https://a/4", "完全不同的主题：欧盟通过AI法案实施细则合规期限十八个月"),
    ]}
    mat = B._build_event_material(cluster, rss_by_url={})
    assert mat.count("OpenAI发布GPT-5.6") == 1, "近似转述(jaccard>0.7)只保留一条"
    assert "欧盟通过AI法案" in mat, "不同主题不受去重影响"


def test_evidence_pack_skips_inlined_urls():
    hits = B._fulltext_hit_urls({"items": [_item("https://r/1", "x")]}, RSS_BY)
    assert B._norm_url("https://r/1") in hits
    ev = B._build_evidence_pack({"items": [_item("https://r/1", "x")]}, RSS_BY,
                                skip_urls=hits)
    assert ev == "", "素材已内联正文的 URL 证据包不再重复注入"


def test_truncate_at_paragraph():
    t = ("段落一" * 100 + "\n\n" + "段落二" * 100)
    cut = B._truncate_at_paragraph(t, 500)
    assert cut == "段落一" * 100, "应在段落边界截断而非腰斩"


def test_paraphrase_dedup_only_on_phase2_path():
    """P1-4：MMR 仅在传入 rss_by_url（Phase2 深度解读路径）时启用；
    Phase1/自审/修正环素材必须保持原样。"""
    body = "OpenAI发布GPT-5.6支持百万上下文窗口定价每百万token输入2.5美元输出12美元"
    cluster = {"label": "L", "items": [
        _item("https://a/1", body + "报道一" * 8),
        _item("https://a/2", body + "报道二" * 8),
    ]}
    mat_legacy = B._build_event_material(cluster)
    assert mat_legacy.count("OpenAI发布GPT-5.6") == 2, "无 rss_by_url 时不得裁剪素材"
    mat_p2 = B._build_event_material(cluster, rss_by_url={})
    assert mat_p2.count("OpenAI发布GPT-5.6") == 1


def test_numbering_sequential_after_skip():
    """P1-3：MMR 跳过后 [n] 编号必须连续，否则模型按可见顺序报序数→引用被静默剔除。"""
    body = "OpenAI发布GPT-5.6支持百万上下文窗口定价每百万token输入2.5美元输出12美元"
    cluster = {"label": "L", "items": [
        _item("https://a/1", body + "报道一" * 8),
        _item("https://a/2", body + "报道二" * 8),
        _item("https://a/4", "完全不同的主题欧盟通过AI法案实施细则合规期限十八个月" + "内容" * 40),
    ]}
    mat, refs = B._build_event_material(cluster, numbered=True, rss_by_url={})
    assert [r["index"] for r in refs] == [1, 2], "编号必须连续 [1][2]"
    assert "[1]" in mat and "[2]" in mat and "[3]" not in mat
    urls = {r["url"] for r in refs}
    assert "https://a/2" not in urls
