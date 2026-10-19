# tests/rss_translate/test_translate_cache.py
# -*- coding: utf-8 -*-
"""翻译缓存 + 熔断 + 统计计数器测试"""
import os, sys, time, hashlib, json
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _loader import load_build

B = load_build()
FAIL = 0; PASS = 0; failures = []

def eq(a, e, l):
    global FAIL, PASS
    if a == e: PASS += 1; print("  [PASS] %s" % l)
    else: FAIL += 1; failures.append(l); print("  [FAIL] %s\n         期望 %r\n         实得 %r" % (l, e, a))

def ok(c, l): eq(bool(c), True, l)

def reset():
    B._AGNES_BLOCK_UNTIL = 0.0; B._AGNES_OFFENSES = 0; B._AGNES_EMPTY_STREAK = 0
    B._TRANS_FAIL_STREAK = 0; B._TRANS_BLOCK_UNTIL = 0.0
    B._ZEN_MODEL_IDX = 0; B._ZEN_MODEL_BLOCK = {}; B._ZEN_MODEL_OFFENSES = {}
    B._ZEN_TIMEOUT_STREAK = {}; B._ZEN_AUTH_STICKY = None
    B._TRANS_STATS = {"agnes":0,"zen":0,"google":0,"bing":0,"mymemory":0,"dict":0,"skip":0,"fail":0,"cache_hit":0}

print("=" * 74)
print("C. 缓存行为")
print("=" * 74)

reset()
# C1: 翻译成功后写入 _trans_cache
# 译文必须通过长度守卫：len(cand) > len(text) * 0.2（L1402）
B._AGNES_KEY = "fake"
mock_zh = "这是一段用于测试缓存写入功能的翻译文本"
with mock.patch.object(B, '_agnes_translate', return_value=mock_zh):
    B._translate_to_zh("unique cache write test")
    h = hashlib.md5("unique cache write test".encode()).hexdigest()
    ok(h in B._trans_cache, "C1 翻译成功后写入缓存")
    eq(B._trans_cache[h], mock_zh, "C1b 缓存值正确")

reset()
# C2: 熔断期间返回原文
B._TRANS_BLOCK_UNTIL = time.time() + 9999
result = B._translate_to_zh("should be blocked by circuit breaker")
eq(result, "should be blocked by circuit breaker", "C2 熔断期间返回原文")
eq(B._TRANS_STATS["fail"], 1, "C2b 熔断期间 fail 计数 +1")

reset()
# C4: Agnes 罚期期间 _translate_to_zh 跳过 Agnes 走后续端点
B._AGNES_KEY = "fake"
B._AGNES_BLOCK_UNTIL = time.time() + 9999
with mock.patch.object(B, '_agnes_translate', return_value="should not reach") as m:
    # 用 mock 让后续端点也失败，确认 Agnes 不被调用
    with mock.patch.object(B.urllib.request, 'urlopen', side_effect=Exception("fail")):
        B._translate_to_zh("test agnes blocked")
        eq(m.called, False, "C4 Agnes 罚期期间不被调用")

# ── 汇总 ──
print("\n" + "=" * 74)
total = PASS + FAIL
print("总计 %d 项通过 / %d 项失败 / %d 项总计" % (PASS, FAIL, total))
if failures:
    print("失败项: %s" % ", ".join(failures)); sys.exit(1)
else:
    print("ALL PASS")
