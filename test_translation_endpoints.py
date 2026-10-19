# -*- coding: utf-8 -*-
"""Agnes 端点账本 TDD 测试（外部审查 P2-A 对齐 Zen 语义）。

运行：仓库根目录 python test_translation_endpoints.py，退出码 0 = 全过。
全部 mock urlopen，零网络。
"""
import io
import json
import sys
import threading
import time
import urllib.error

import build_rss_aggregator as m

PASS = []
FAIL = []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append((name, detail))
    print(("PASS " if cond else "FAIL ") + name + ((" | " + str(detail)) if (detail and not cond) else ""))


def reset():
    m._AGNES_BLOCK_UNTIL = 0
    m._AGNES_OFFENSES = 0
    m._AGNES_EMPTY_STREAK = 0


def urlopen_429(req, timeout):
    raise urllib.error.HTTPError("u", 429, "rate", {}, io.BytesIO(b"x"))


def urlopen_500(req, timeout):
    raise urllib.error.HTTPError("u", 500, "boom", {}, io.BytesIO(b"x"))


def urlopen_401(req, timeout):
    raise urllib.error.HTTPError("u", 401, "auth", {}, io.BytesIO(b"x"))


def urlopen_empty(req, timeout):
    class R:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"choices": [{"message": {"content": ""}}]}).encode()

    return R()


def urlopen_ok(req, timeout):
    class R:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"choices": [{"message": {"content": "译文"}}]}).encode()

    return R()


# ── 新行为（TDD red → green）──

def t1_500_enters_ledger():
    reset()
    m.urllib.request.urlopen = urlopen_500
    m._agnes_translate("x", timeout=5)
    ok = m._AGNES_OFFENSES == 1 and m._AGNES_BLOCK_UNTIL > time.time()
    check("T1 500 错误入账本（对齐 Zen Z1）", ok, (m._AGNES_OFFENSES, m._AGNES_BLOCK_UNTIL))


def t2_empty_x3_enters_ledger():
    reset()
    m.urllib.request.urlopen = urlopen_empty
    for i in range(2):
        m._agnes_translate("x", timeout=5)
    mid_block = m._AGNES_BLOCK_UNTIL  # 前 2 次不应封
    m._agnes_translate("x", timeout=5)
    ok = mid_block == 0 and m._AGNES_BLOCK_UNTIL > time.time() and m._AGNES_EMPTY_STREAK == 0
    check("T2 连续 3 次空 content 才入罚期（前 2 次放行）", ok, (mid_block, m._AGNES_BLOCK_UNTIL))


def t3_success_resets_empty_streak():
    reset()
    m.urllib.request.urlopen = urlopen_empty
    m._agnes_translate("x", timeout=5)
    m._agnes_translate("x", timeout=5)
    m.urllib.request.urlopen = urlopen_ok
    m._agnes_translate("y", timeout=5)
    streak_after_ok = m._AGNES_EMPTY_STREAK
    m.urllib.request.urlopen = urlopen_empty
    m._agnes_translate("z", timeout=5)
    m._agnes_translate("z", timeout=5)
    blocked_early = m._AGNES_BLOCK_UNTIL > time.time()  # 重置后 2 次空不应封
    ok = streak_after_ok == 0 and not blocked_early
    check("T3 成功清空 streak；重置后不误封", ok, (streak_after_ok, blocked_early))


# ── 回归（既有行为）──

def t4_concurrent_429_single_offense():
    reset()
    m.urllib.request.urlopen = urlopen_429
    def worker():
        m._agnes_translate("x", timeout=5)
    ts = [threading.Thread(target=worker) for _ in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    check("T4 并发 6×429 offenses=1（幂等回归）", m._AGNES_OFFENSES == 1, m._AGNES_OFFENSES)


def t5_401_enters_ledger():
    reset()
    m.urllib.request.urlopen = urlopen_401
    m._agnes_translate("x", timeout=5)
    check("T5 401 入账本（回归）", m._AGNES_OFFENSES == 1 and m._AGNES_BLOCK_UNTIL > time.time())


def t6_blocked_wave_no_double_count():
    reset()
    m.urllib.request.urlopen = urlopen_429
    m._agnes_translate("x", timeout=5)  # 第一次：offenses=1，罚期设置
    set_at = m._AGNES_BLOCK_UNTIL
    m._agnes_translate("y", timeout=5)  # 罚期内重复：不双计
    ok = m._AGNES_OFFENSES == 1 and m._AGNES_BLOCK_UNTIL == set_at
    check("T6 罚期内重复 429 不双计（幂等回归）", ok, m._AGNES_OFFENSES)


def main():
    t1_500_enters_ledger()
    t2_empty_x3_enters_ledger()
    t3_success_resets_empty_streak()
    t4_concurrent_429_single_offense()
    t5_401_enters_ledger()
    t6_blocked_wave_no_double_count()
    print("\n%d PASS / %d FAIL" % (len(PASS), len(FAIL)))
    if FAIL:
        for name, detail in FAIL:
            print("FAILED:", name, detail)
        sys.exit(1)


if __name__ == "__main__":
    main()
