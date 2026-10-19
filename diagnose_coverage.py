# -*- coding: utf-8 -*-
"""Coverage 分母归因分析脚本。

诊断每日洞察报告的 Coverage 瓶颈在聚类步骤还是生成步骤。

分析链路：
  全量 chunks → 聚类进 events？ → 出现在 summary 中？

输入：
  - daily_insight_chunks.json（全量 chunks）
  - daily-insight.json（当日报告 events + articles + summaries）

输出：
  - 聚类覆盖率：chunks 中被聚类进任何 event 的比例
  - 生成利用率：被聚类 chunks 中关键信息出现在 summary 的比例
  - 未引用 chunks 特征分布（source_type / source / score 分位）

用法：
  python diagnose_coverage.py
"""
import json
import os
import re
import sys
import io
from collections import Counter, defaultdict

# 修复 Windows 控制台编码问题
try:
    if sys.stdout.encoding != 'utf-8':
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    if sys.stderr.encoding != 'utf-8':
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
except Exception:
    pass  # 非交互式环境可能无 buffer

BJT_DIR = os.path.dirname(os.path.abspath(__file__))
CHUNKS_FILE = os.path.join(BJT_DIR, "daily_insight_chunks.json")
INSIGHT_FILE = os.path.join(BJT_DIR, "daily-insight.json")


def _simple_tokens(text):
    """简易中文分词：按非中文/非字母字符切分，保留 2-gram。"""
    if not text:
        return set()
    clean = re.sub(r'[^\u4e00-\u9fffa-zA-Z0-9]', ' ', text.lower())
    words = set(clean.split())
    # 加 bigrams
    for w in list(words):
        if len(w) >= 2:
            for i in range(len(w) - 1):
                words.add(w[i:i+2])
    return words


def _title_match_score(chunk_title, event_articles):
    """检查 chunk 标题是否与事件中任一 article 标题匹配。
    返回 (matched: bool, best_overlap: int)。"""
    if not chunk_title:
        return False, 0
    ct_tokens = _simple_tokens(chunk_title)
    if not ct_tokens:
        return False, 0
    best = 0
    for art in event_articles:
        at_tokens = _simple_tokens(art.get("title", ""))
        overlap = len(ct_tokens & at_tokens)
        if overlap > best:
            best = overlap
    return best >= 3, best


def _summary_covers(summary, chunk_text):
    """检查 summary 是否覆盖了 chunk 的关键信息。
    基于字符重叠率：summary 中有多少 chunk 关键句的字符出现。"""
    if not summary or not chunk_text:
        return False
    # 取 chunk 前 200 字符作为关键信息
    key_text = chunk_text[:200]
    key_tokens = _simple_tokens(key_text)
    sum_tokens = _simple_tokens(summary)
    if not key_tokens:
        return False
    overlap = len(key_tokens & sum_tokens)
    # 至少 20% 的关键 token 出现在 summary 中
    return overlap / len(key_tokens) >= 0.2


def main():
    print("=" * 60)
    print("Coverage 分母归因分析")
    print("=" * 60)

    # 1. 加载数据
    if not os.path.exists(CHUNKS_FILE):
        print("[ERROR] 找不到 %s" % CHUNKS_FILE)
        sys.exit(1)
    if not os.path.exists(INSIGHT_FILE):
        print("[ERROR] 找不到 %s" % INSIGHT_FILE)
        sys.exit(1)

    with open(CHUNKS_FILE, "r", encoding="utf-8") as f:
        all_chunks = json.load(f)
    with open(INSIGHT_FILE, "r", encoding="utf-8") as f:
        insight = json.load(f)

    events = insight.get("events", [])
    print("\n数据概览:")
    print("  全量 chunks: %d" % len(all_chunks))
    print("  报告事件数: %d" % len(events))

    if not events:
        print("[WARN] 报告中无事件，无法分析")
        sys.exit(0)

    # 2. 模拟评估上下文：取 top 80 chunks（按 chunk_id 排序近似，
    #    因为 retrieval_score 未持久化，这里用全量 chunks 的前 80 条近似）
    #    注意：实际评估时是按 retrieval_score 排序的，此处为近似分析
    eval_context_size = min(80, len(all_chunks))
    eval_chunks = all_chunks[:eval_context_size]
    print("  评估上下文 chunks: %d (近似 top %d)" % (len(eval_chunks), eval_context_size))

    # 3. 聚类覆盖率：eval_chunks 中有多少被聚类进任何 event
    clustered = []       # (chunk, event_idx, match_score)
    not_clustered = []   # chunk

    for chunk in eval_chunks:
        chunk_title = chunk.get("title", "")
        chunk_url = chunk.get("url", "")
        best_match = False
        best_event = -1
        best_overlap = 0

        for ei, evt in enumerate(events):
            articles = evt.get("articles", [])
            # 先按 URL 精确匹配
            for art in articles:
                if chunk_url and art.get("url", "") == chunk_url:
                    best_match = True
                    best_event = ei
                    best_overlap = 999
                    break
            if best_match:
                break
            # 再按标题模糊匹配
            matched, overlap = _title_match_score(chunk_title, articles)
            if matched and overlap > best_overlap:
                best_match = True
                best_event = ei
                best_overlap = overlap

        if best_match:
            clustered.append((chunk, best_event, best_overlap))
        else:
            not_clustered.append(chunk)

    cluster_cov = len(clustered) / len(eval_chunks) if eval_chunks else 0
    print("\n── 聚类覆盖分析 ──")
    print("  评估上下文: %d chunks" % len(eval_chunks))
    print("  被聚类进事件: %d (%.1f%%)" % (len(clustered), cluster_cov * 100))
    print("  未被聚类: %d (%.1f%%)" % (len(not_clustered), (1 - cluster_cov) * 100))

    # 4. 生成利用率：被聚类 chunks 中有多少的关键信息出现在 summary 中
    covered = []
    not_covered = []

    for chunk, evt_idx, match_score in clustered:
        evt = events[evt_idx]
        summary = evt.get("summary", "")
        chunk_text = chunk.get("text", "") or chunk.get("title", "")
        if _summary_covers(summary, chunk_text):
            covered.append((chunk, evt_idx))
        else:
            not_covered.append((chunk, evt_idx))

    gen_util = len(covered) / len(clustered) if clustered else 0
    print("\n── 生成利用率分析 ──")
    print("  被聚类 chunks: %d" % len(clustered))
    print("  summary 覆盖: %d (%.1f%%)" % (len(covered), gen_util * 100))
    print("  summary 未覆盖: %d (%.1f%%)" % (len(not_covered), (1 - gen_util) * 100))

    # 5. 未引用 chunks 特征分布
    print("\n── 未聚类 chunks 分布 ──")

    # 5a. 按 source_type
    src_type_dist = Counter(c.get("source_type", "unknown") for c in not_clustered)
    print("\n  按 source_type:")
    for st, cnt in src_type_dist.most_common():
        print("    %s: %d" % (st, cnt))

    # 5b. 按 source（top 10）
    source_dist = Counter(c.get("source", "unknown") for c in not_clustered)
    print("\n  按 source (top 10):")
    for src, cnt in source_dist.most_common(10):
        print("    %s: %d" % (src, cnt))

    # 5c. 按 chunk 在 eval_context 中的位置分位
    if not_clustered:
        positions = []
        for nc in not_clustered:
            for i, ec in enumerate(eval_chunks):
                if ec.get("chunk_id") == nc.get("chunk_id"):
                    positions.append(i)
                    break
        if positions:
            positions.sort()
            print("\n  位置分布:")
            print("    最早: #%d" % positions[0])
            print("    最晚: #%d" % positions[-1])
            print("    中位: #%d" % positions[len(positions) // 2])
            # 分位统计
            q1 = positions[len(positions) // 4]
            q3 = positions[3 * len(positions) // 4]
            print("    Q1: #%d, Q3: #%d" % (q1, q3))

    # 6. 每事件 chunk 覆盖详情
    print("\n── 每事件 chunk 覆盖详情 ──")
    evt_chunk_count = Counter(evt_idx for _, evt_idx, _ in clustered)
    for ei, evt in enumerate(events):
        n_chunks = evt_chunk_count.get(ei, 0)
        n_articles = len(evt.get("articles", []))
        label = evt.get("label", "")[:40]
        summary_len = len(evt.get("summary", ""))
        print("  事件 %d: %s | chunks=%d articles=%d summary=%d字" % (
            ei + 1, label, n_chunks, n_articles, summary_len))

    # 7. 瓶颈判定（方向性参考，非结论）
    # 分母是 daily_insight_chunks.json 的前 80 条：该文件是跨构建累积的嵌入缓存，
    # 且 retrieval_score 未持久化，无法还原真实评估上下文（按当日 retrieval_score 降序取 80）。
    # 因此下面的比例只能指示方向，不能据此断言瓶颈在哪一步。
    print("\n── 瓶颈判定（分母为近似，结论仅供参考）──")
    if cluster_cov < 0.8:
        print("  ⚠ 聚类瓶颈: 仅 %.1f%% 的评估 chunks 被聚类进事件" % (cluster_cov * 100))
        print("    建议: 增加 MAX_EVENTS、降低聚类阈值、扩大检索候选池")
    else:
        print("  ✓ 聚类覆盖良好: %.1f%%" % (cluster_cov * 100))

    if gen_util < 0.7:
        print("  ⚠ 生成瓶颈: 仅 %.1f%% 的被聚类 chunks 信息出现在 summary" % (gen_util * 100))
        print("    建议: 增强 prompt 约束（每事件至少引用 3 个信息点）、传入全局上下文")
    else:
        print("  ✓ 生成利用率良好: %.1f%%" % (gen_util * 100))

    # 8. 综合 Coverage 估算
    overall = cluster_cov * gen_util
    print("\n── 综合 Coverage 估算 ──")
    print("  聚类覆盖 × 生成利用 = %.1f%% × %.1f%% = %.1f%%" % (
        cluster_cov * 100, gen_util * 100, overall * 100))
    print("  (此值为近似估算，实际 RAGAS 评分由 LLM judge 给出)")
    print("\n" + "=" * 60)


if __name__ == "__main__":
    main()
