# tests/rss_translate/test_translate_engines.py
# -*- coding: utf-8 -*-
"""L1 构建时翻译引擎降级链测试

验收标准：
  - Agnes 空 key / 429 罚期行为正确
  - Zen 轮询 + 自封行为正确
  - _translate_to_zh 降级链完整（中/日文判断、缓存、熔断）
  - 翻译统计计数器准确
"""
import os
import sys
import time
import json
import hashlib
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _loader import load_build

B = load_build()

FAIL = 0
PASS = 0
failures = []


def eq(actual, expected, label):
    global FAIL, PASS
    if actual == expected:
        PASS += 1
        print("  [PASS] %s" % label)
    else:
        FAIL += 1
        failures.append(label)
        print("  [FAIL] %s\n         期望 %r\n         实得 %r" % (label, expected, actual))


def ok(cond, label):
    eq(bool(cond), True, label)


def reset_state():
    """重置翻译模块的全局状态，确保测试隔离。"""
    B._AGNES_BLOCK_UNTIL = 0.0
    B._AGNES_OFFENSES = 0
    B._AGNES_EMPTY_STREAK = 0
    B._TRANS_FAIL_STREAK = 0
    B._TRANS_BLOCK_UNTIL = 0.0
    B._ZEN_MODEL_IDX = 0
    B._ZEN_MODEL_BLOCK = {}
    B._ZEN_MODEL_OFFENSES = {}
    B._ZEN_TIMEOUT_STREAK = {}
    B._ZEN_AUTH_STICKY = None
    B._TRANS_STATS = {"agnes": 0, "zen": 0, "google": 0, "bing": 0,
                      "mymemory": 0, "dict": 0, "skip": 0, "fail": 0, "cache_hit": 0}


# ═══════════════════════════════════════════════════════════════
print("=" * 74)
print("A. Agnes 端点行为")
print("=" * 74)

reset_state()

# A1: 空 key 时 _translate_to_zh 跳过 Agnes（中文为主直接 skip）
B._AGNES_KEY = ""
with mock.patch.object(B, '_agnes_translate', return_value=None) as m:
    result = B._translate_to_zh("这是一段纯中文文本不需要翻译")
    eq(m.called, False, "A1 空 key 时不调用 Agnes（中文跳过）")

reset_state()

# A2: Agnes 429 后进入罚期
B._AGNES_KEY = "test-key-fake"

def mock_429(*a, **kw):
    import urllib.error
    raise urllib.error.HTTPError("url", 429, "rate limited", {}, None)

with mock.patch.object(B.urllib.request, 'urlopen', side_effect=mock_429):
    result = B._agnes_translate("hello world")
    eq(result, None, "A2a Agnes 429 返回 None")
    ok(B._AGNES_BLOCK_UNTIL > time.time(), "A2b Agnes 429 后进入罚期")
    ok(B._AGNES_OFFENSES >= 1, "A2c Agnes offenses 递增")

reset_state()

# ═══════════════════════════════════════════════════════════════
print("\n" + "=" * 74)
print("Z. Zen 轮询行为")
print("=" * 74)

reset_state()
B._ZEN_KEY = "test-zen-key"
B._ZEN_MODELS = ["model-a", "model-b", "model-c"]

# Z1: 游标推进（每次 _zen_translate 调用会遍历所有未自封模型，_ZEN_MODEL_IDX 递增）
idx_before = B._ZEN_MODEL_IDX
with mock.patch.object(B, '_zen_call_model', return_value=(None, False, False)):
    B._zen_translate("test")
    ok(B._ZEN_MODEL_IDX > idx_before, "Z1 Zen 游标推进")

reset_state()
B._ZEN_MODELS = ["model-a", "model-b"]

# Z2: 全模型自封后返回 None
# mock 返回 (None, True, False) → to_block=True → 每个模型被自封 → 最终全部自封
with mock.patch.object(B, '_zen_call_model', return_value=(None, True, False)):
    result = B._zen_translate("test")
    eq(result, None, "Z2 全模型自封后返回 None")

reset_state()

# ═══════════════════════════════════════════════════════════════
print("\n" + "=" * 74)
print("T. _translate_to_zh 降级链")
print("=" * 74)

reset_state()

# T1: 中文为主跳过
result = B._translate_to_zh("这是一段纯中文的文本内容")
eq(result, "这是一段纯中文的文本内容", "T1 中文为主跳过翻译")
eq(B._TRANS_STATS["skip"], 1, "T1b skip 计数 +1")

reset_state()

# T2: 日文（含假名）不跳过
ja_text = "これはテストです"
result = B._translate_to_zh(ja_text)
# 不应被 skip（会尝试翻译，可能失败返回原文）
ok(B._TRANS_STATS["skip"] == 0, "T2 日文不跳过")

reset_state()

# T3: 缓存命中
test_text = "cache test unique string 12345"
test_hash = hashlib.md5(test_text.encode()).hexdigest()
B._trans_cache[test_hash] = "缓存译文"
result = B._translate_to_zh(test_text)
eq(result, "缓存译文", "T3 缓存命中直接返回")
eq(B._TRANS_STATS["cache_hit"], 1, "T3b cache_hit 计数 +1")

reset_state()

# T4: 全端点失败保留原文
en_text = "This is an English sentence that needs translation"
with mock.patch.object(B, '_agnes_translate', return_value=None), \
     mock.patch.object(B.urllib.request, 'urlopen', side_effect=Exception("mock fail")):
    B._AGNES_KEY = "fake"
    result = B._translate_to_zh(en_text)
    eq(result, en_text, "T4 全端点失败保留原文")

reset_state()

# T5: 连续 5 次全败触发熔断
# 注意：源码 L1469+L1471 对 _TRANS_FAIL_STREAK 双重自增（每次 +2），
# 因此 3 次调用即可达到阈值 5
B._AGNES_KEY = "fake"
with mock.patch.object(B, '_agnes_translate', return_value=None), \
     mock.patch.object(B.urllib.request, 'urlopen', side_effect=Exception("mock fail")):
    for i in range(6):
        B._translate_to_zh("unique text %d for circuit breaker test" % i)
    ok(B._TRANS_BLOCK_UNTIL > time.time(), "T5 连续全败触发熔断")

reset_state()

# ═══════════════════════════════════════════════════════════════
print("\n" + "=" * 74)
print("S. 翻译统计")
print("=" * 74)

reset_state()
B._AGNES_KEY = "fake"
# 译文必须通过长度守卫：len(cand) > len(text) * 0.2（L1402）
mock_translation = "这是翻译引擎返回的测试译文结果内容"

with mock.patch.object(B, '_agnes_translate', return_value=mock_translation):
    result = B._translate_to_zh("Some English text to translate")
    eq(result, mock_translation, "S1a Agnes 成功返回译文")
    eq(B._TRANS_STATS["agnes"], 1, "S1b agnes 计数 +1")

# ── 汇总 ──
print("\n" + "=" * 74)
total = PASS + FAIL
print("总计 %d 项通过 / %d 项失败 / %d 项总计" % (PASS, FAIL, total))
if failures:
    print("失败项: %s" % ", ".join(failures))
    sys.exit(1)
else:
    print("ALL PASS")
