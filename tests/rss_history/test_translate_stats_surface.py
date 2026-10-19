# -*- coding: utf-8 -*-
"""翻译统计的观测面（2026-09-27）。

现场证据：今天 26 场 build 的日志里翻译字段只有
`trans_cache_hit/size/trimmed/google/mymemory/dict/skip/fail` ——
**`agnes` 与 `zen` 这两个 LLM 兜底引擎的计数压根没进日志**，只 print 在 stdout 上。
于是"这场有没有走 LLM 兜底、兜底占多少"在构建日志里问不出答案，
而用户恰恰是靠这层兜底判断"大陆用户不是永远翻不了"。

判据写成通用不变量：凡 `_TRANS_STATS["<引擎>"] += 1` 出现过的引擎，
都必须有对应的 `"trans_<引擎>"` 落进 build 事件。
这样新增引擎却不配观测字段会直接红，删掉某个字段也会红（两头都堵）。
"""
import os
import re

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SRC = os.environ.get("RSS_BUILD_SRC") or os.path.join(ROOT, "build_rss_aggregator.py")


def _text():
    assert os.path.isfile(SRC), "被测脚本读不到（%s），这条判据就成了恒真" % SRC
    with open(SRC, encoding="utf-8") as fh:
        return fh.read()


def _build_event_block(text):
    """取 build_logger.append({...}) 的花括号体。"""
    i = text.find("build_logger.append(")
    assert i >= 0, "找不到 build_logger.append(...)，日志写入方式变了，判据需同步"
    start = text.find("{", i)
    depth, j = 0, start
    while j < len(text):
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                return text[start:j + 1]
        j += 1
    raise AssertionError("build_logger.append 的花括号没闭合")


def test_every_counted_engine_has_a_log_field():
    text = _text()
    counted = set(re.findall(r'_TRANS_STATS\["(\w+)"\]\s*\+=', text))
    assert counted, "脚本里已经没有 _TRANS_STATS 计数点了，这条判据失去意义"
    block = _build_event_block(text)
    logged = set(re.findall(r'"trans_(\w+)"\s*:', block))
    missing = sorted(counted - logged)
    assert not missing, (
        "这些翻译计数只印 stdout、没进构建日志：%s —— "
        "线上只能靠 grep 日志文本才能回答\u201c这场有没有走该引擎\u201d" % ", ".join(missing))


def test_llm_fallback_engines_are_logged_by_name():
    """Agnes/Zen 是"摘要为什么还能翻出来"的答案所在，单独点名，防止被当成可选字段删掉。"""
    block = _build_event_block(_text())
    for engine in ("agnes", "zen"):
        assert '"trans_%s"' % engine in block, (
            "trans_%s 不在 build 事件里：LLM 兜底用量再次变成只存在于 stdout 的隐形数字" % engine)
