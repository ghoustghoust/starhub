# -*- coding: utf-8 -*-
"""多 key 时 Agnes 429 必须"轮完一圈就停手"，而不是每条文本都再撞一遍墙。

现场（2026-10-01 读码）：`_agnes_translate` 的 429 分支在 `len(_AGNES_KEYS) > 1` 时
**只轮转 key 并 return None**，永远不设 `_AGNES_BLOCK_UNTIL`；而"停手"完全依赖调用方
`_translate_to_zh` 里那句 `if _AGNES_KEYS and time.time() >= _AGNES_BLOCK_UNTIL`
—— 也就是"罚期"这件事只有记得查闸的那一个调用者能享受，函数自己照发请求。
所以所有 key 一起被限流时，构建对每一条待翻文本仍各打一轮 429：白烧时间、配额和对方网关。

修成两件事（都是"不再发请求"，**没有 sleep** —— 用户明确关心过会不会拉长构建，答案是只会变快）：
  · 轮完一整圈仍全 429 ⇒ 记一次罚期（offenses 递增，与其它失败同源口径）
  · 函数入口自带罚期闸 ⇒ 不依赖每个调用者都记得查

判据两边都钉，防止"为了修 A 把 B 砍了"：
  · 单次 429（还有没试过的 key）必须继续发 —— 否则兜底链被误杀，少翻比慢更糟
  · 中间任何一次成功都要清零连击
  · time.sleep 换成记录器，出现一次就判红
"""
import os
import sys
import urllib.error

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "tests", "rss_composite"))
sys.path.insert(0, ROOT)

from _loader import load_build  # noqa: E402

KEYS = ["k0", "k1", "k2"]


class Resp:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return b'{"choices":[{"message":{"content":"\xe8\xaf\x91\xe6\x96\x87"}}]}'


@pytest.fixture()
def agnes(monkeypatch):
    """多 key、可控 429 序列；记录真实外呼与任何 sleep。"""
    B = load_build()
    calls, sleeps = [], []
    monkeypatch.setattr(B, "_AGNES_KEYS", list(KEYS))
    monkeypatch.setattr(B, "_AGNES_KEY_IDX", 0)
    monkeypatch.setattr(B, "_AGNES_BLOCK_UNTIL", 0.0)
    monkeypatch.setattr(B, "_AGNES_OFFENSES", 0)
    monkeypatch.setattr(B, "_AGNES_EMPTY_STREAK", 0)
    monkeypatch.setattr(B, "_AGNES_429_STREAK", 0, raising=False)
    monkeypatch.setattr(B.time, "sleep", lambda s: sleeps.append(s))

    state = {"seq": [], "n": 0}

    def fake(req, timeout=None):
        state["n"] += 1
        calls.append(req.get_header("Authorization"))
        if state["seq"] and state["seq"][0] == "429":
            state["seq"].pop(0)
            raise urllib.error.HTTPError(req.full_url, 429, "rate limited", {}, None)
        if state["seq"]:
            state["seq"].pop(0)
        return Resp()

    monkeypatch.setattr(B.urllib.request, "urlopen", fake)
    B._state = state
    return B, calls, sleeps


def _drive(B, calls, pattern):
    """按 pattern（'429' / 'ok' 序列）逐条调用 Agnes。"""
    B._state["seq"] = list(pattern)
    for i in range(len(B._state["seq"])):
        B._agnes_translate("english text number %d" % i)


def test_full_cycle_of_429_triggers_penalty_and_stops_calling(agnes):
    """3 个 key 轮完一圈仍全 429 ⇒ 记罚期，且后续调用零请求。"""
    B, calls, sleeps = agnes
    _drive(B, calls, ["429"] * 3)
    assert len(calls) == 3, "一轮应该是 3 次请求，实为 %d" % len(calls)
    import time as _t
    assert B._AGNES_BLOCK_UNTIL > _t.time(), "轮完一圈全 429 却没记罚期：退避没生效"
    n_before = len(calls)
    _drive(B, calls, ["429"] * 5)
    assert len(calls) == n_before, (
        "罚期内函数自己仍在发请求（多发 %d 次）：'停手'不该只依赖调用方记得查闸"
        % (len(calls) - n_before))


def test_single_429_does_not_stop_the_chain(agnes):
    """反向护栏：只 429 一次时下一个 key 必须照试。"""
    B, calls, sleeps = agnes
    _drive(B, calls, ["429", "ok"])
    assert len(calls) == 2, "第二个 key 没被尝试（过早退避）：只发了 %d 次" % len(calls)
    import time as _t
    assert B._AGNES_BLOCK_UNTIL <= _t.time(), "还没轮完一圈就进了罚期"


def test_success_resets_the_streak(agnes):
    """中间成功一次就清零，否则零散 429 会攒成误退避。

    样本量必须 **≥ key 数（3 轮 429）**：我第一版只喂了 2 次 429，那样"不清零"也够不到阈值，
    变异 M4 因此存活过 —— 判据不 discriminating 时，绿没有任何意义。
    """
    B, calls, sleeps = agnes
    _drive(B, calls, ["429", "ok", "429", "ok", "429", "ok"])
    assert len(calls) == 6, (
        "被成功打断的 429 被攒成了退避：应发 6 次，实发 %d" % len(calls))
    import time as _t
    assert B._AGNES_BLOCK_UNTIL <= _t.time(), "三次分散的 429（每次之间都成功过）不该触发罚期"


def test_no_sleep_anywhere_in_the_429_path(agnes):
    """整条 429 路径不许出现 sleep：钉住"变快不是变慢"这个承诺。"""
    B, calls, sleeps = agnes
    _drive(B, calls, ["429"] * 6)
    assert not sleeps, "429 路径里出现了 time.sleep：%s" % sleeps
