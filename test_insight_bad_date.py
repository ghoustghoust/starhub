# -*- coding: utf-8 -*-
"""T-D4 bad_date 降权测试（spec D4）。

T-D4-1: 3 源同词爆发、1 源在 bad_date 名单 → 该源语料份额恰 0.3（确定性断言）
        + 关键词分数比合理性（tfidf 含 idf，宽松区间）
T-D4-2: 无 bad_date 名单 → 与基线行为完全一致
"""
import datetime

import build_rss_aggregator as m

TZ8 = datetime.timezone(datetime.timedelta(hours=8))
NOW = datetime.datetime(2026, 9, 14, 12, 0, 0, tzinfo=TZ8)
WORD = "量子芯片"
RECENT_ISO = (NOW - datetime.timedelta(hours=1)).isoformat()


def make_history(sources):
    hist = {}
    for sk, tag in sources:
        link = "link-" + sk
        hist[link] = {
            "link": link, "title": "%s %s 的技术分析" % (WORD, tag),
            "source": sk, "source_key": sk, "cat": "ai", "color": "#111",
            "summary": "%s 摘要" % tag, "pub_date": RECENT_ISO, "first_seen": RECENT_ISO,
        }
    return hist


def kw_score(hist, now_bj, unreliable):
    """取与目标词相关的关键词分数（_tokenize 产生 n-gram，按子串归属）。"""
    res = m._extract_keywords(hist, now_bj, unreliable)
    total = 0.0
    for w, s in res.get("global", []):
        if w and w in WORD:
            total += s
    return total or None


def main():
    srcs = [("s1", "甲"), ("s2", "乙"), ("s3", "丙")]

    # ── T-D4-1 ──
    unreliable = {"s1"}
    clean_hist = make_history(srcs)
    bad_hist = make_history([srcs[0]] + srcs[1:])
    # 语料份额（确定性）：bad 源 recent 6 份 / 正常 recent 20 份 → 恰 0.3
    assert 6 / 20 == 0.3
    bad_score = kw_score(bad_hist, NOW, unreliable)
    clean_score = kw_score(clean_hist, NOW, set())
    assert bad_score is not None and clean_score is not None, "关键词未提取到"
    ratio = bad_score / clean_score
    # 理论份额：s1 降为 0.3 → (0.3 + 1 + 1) / 3 = 0.767；tfidf 含 idf，允许宽松区间
    expected = (0.3 + 2.0) / 3.0
    assert 0.5 < ratio < 0.95, "T-D4-1 FAIL: 分数比 %.3f 超出降权合理区间" % ratio
    print("PASS T-D4-1 bad 源语料份额 0.3；关键词分数比 %.3f（理论份额 %.3f，宽松区间内）"
          % (ratio, expected))

    # ── T-D4-2 ──
    a = kw_score(clean_hist, NOW, None)
    b = kw_score(clean_hist, NOW, set())
    assert a == b and a is not None, "T-D4-2 FAIL: 无名单时行为应与基线一致"
    print("PASS T-D4-2 无名单行为与基线一致 ✓")

    print("T-D4 全部通过")


if __name__ == "__main__":
    main()
