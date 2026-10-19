# -*- coding: utf-8 -*-
"""翻译缓存轮转（2026-09-21，用户点名"机制要一样"）。

现场证据：
- 生产日志 `[缓存] 保存翻译缓存: 101643 条`；远端 `translations.json` = 20,731,886 字节，
  **且它是 update.yml add 清单里的已入库文件**（`:215/:236`），下一场照常检出 ⇒ 只涨不删。
- `build_rss_aggregator._save_caches()` 的 docstring 自己写着
  「RSS 缓存写盘前裁剪过期条目，防止文件无限膨胀」—— 同一个函数里 RSS 裁了、翻译缓存没裁。
- 每场新增 37–77 条（`trans_google`+`trans_mymemory`），命中率 99.4%（8,417 命中 / 50 未命中）
  ⇒ 缓存本身有用，不能一刀切小；要切的是"再也不会遇到"的那批。

口径：条目按 `md5(text)` 存，**没有时间戳**，所以沿用 RSS 那套 TTL 是不可能的；
改用 LRU —— 命中即"touch"（移到 dict 末尾），写盘前截断到上限、丢最久未用的头部，
并像历史侧那样单独记一本账（`_LAST_TRANS_ACCOUNT`），字段为 None 表示"这一步没跑到"，
不许用 0 冒充"跑到了但没丢"。
"""
import importlib.util
import json
import os
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def load_build():
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    src = os.environ.get("RSS_BUILD_SRC") or os.path.join(ROOT, "build_rss_aggregator.py")
    spec = importlib.util.spec_from_file_location("build_rss_aggregator", src)
    mod = importlib.util.module_from_spec(spec)
    mod.__file__ = os.path.join(ROOT, "build_rss_aggregator.py")
    sys.modules["build_rss_aggregator"] = mod
    spec.loader.exec_module(mod)
    return mod


B = load_build()


def _src():
    """读**真正被加载的那份**源码。

    变异跑在 _scratch 副本上时（RSS_BUILD_SRC），若这里仍固定读 ROOT 下的原文件，
    源码类断言就会对着一份没被改的文本作检查 ⇒ 变异体假绿。
    """
    p = os.environ.get("RSS_BUILD_SRC") or os.path.join(ROOT, "build_rss_aggregator.py")
    return open(p, encoding="utf-8").read()


def _fill(n, prefix="t"):
    """造 n 条已入库的翻译条目（照生产形状：md5 十六进制串 → 中文译文）。"""
    return {("%s%064x" % (prefix, i)): "译文%d" % i for i in range(n)}


class TestCapExists:
    def test_cap_constant_is_defined_and_positive(self):
        assert hasattr(B, "TRANS_CACHE_MAX"), "翻译缓存仍无上限 ⇒ 与 RSS 侧机制不一致"
        assert isinstance(B.TRANS_CACHE_MAX, int) and B.TRANS_CACHE_MAX > 0

    def test_cap_actually_bites_the_live_size(self):
        """上限必须低于今天的真实条数，否则"加了上限"只是装饰。"""
        assert B.TRANS_CACHE_MAX < 101643, (
            "上限 %d 不低于生产实测 101643 条 ⇒ 永远裁不到" % B.TRANS_CACHE_MAX)


class TestLruOrdering:
    def test_hit_moves_the_key_to_the_end(self):
        B._trans_cache = _fill(5)
        first = list(B._trans_cache)[0]
        B._trans_touch(first)
        assert list(B._trans_cache)[-1] == first, "命中没把 key 移到末尾 ⇒ LRU 退化成插入序"
        assert len(B._trans_cache) == 5, "touch 不该改变条数"

    def test_touch_keeps_value_intact(self):
        B._trans_cache = _fill(3)
        k = list(B._trans_cache)[1]
        v = B._trans_cache[k]
        B._trans_touch(k)
        assert B._trans_cache[k] == v


class TestTrim:
    def test_over_cap_drops_least_recently_used_only(self):
        cap = B.TRANS_CACHE_MAX
        cache = _fill(cap + 50)
        keys = list(cache)
        hot_old = keys[3]                      # 很旧但刚被命中过的，必须活下来
        B._trans_cache = cache
        B._trans_touch(hot_old)
        dropped = B._trim_trans_cache()
        assert len(B._trans_cache) == cap, "裁剪后 %d 条，应为 %d" % (len(B._trans_cache), cap)
        assert dropped == 50, "丢弃计数 %d 应为 50" % dropped
        assert hot_old in B._trans_cache, "刚命中过的条目被当成最久未用丢了 ⇒ LRU 没生效"
        assert keys[0] not in B._trans_cache, "最久未用的头部没被丢 ⇒ 丢错了方向"

    def test_under_cap_drops_nothing(self):
        """防过度淘汰：把"裁剪"实现成无条件截半，这条必红。"""
        B._trans_cache = _fill(30)
        before = dict(B._trans_cache)
        assert B._trim_trans_cache() == 0
        assert B._trans_cache == before, "未超上限却动了缓存"

    def test_account_is_written_for_the_build_log(self):
        """账本要真被填：字段恒 None 与恒 0 都算没接通（历史上这本账就是这么漏的）。"""
        cap = B.TRANS_CACHE_MAX
        B._trans_cache = _fill(cap + 7)
        B._trim_trans_cache()
        acct = B._LAST_TRANS_ACCOUNT
        assert acct.get("size") == cap, "size 没记成裁剪后的条数：%s" % acct.get("size")
        assert acct.get("trimmed") == 7, "trimmed 没记成实际丢弃数：%s" % acct.get("trimmed")


    def test_real_translate_path_touches_the_cache(self):
        """接线判据：只直接调 _trans_touch 的话，把命中路径上的调用删掉也不会红。"""
        B._trans_cache = {}
        text = "Anthropic ships a new Claude model for coding agents"
        h = B.hashlib.md5(text.encode("utf-8", errors="replace")).hexdigest()
        B._trans_cache["decoy0"] = "占位"
        B._trans_cache[h] = "Anthropic 发布新一代编程模型"
        got = B._translate_to_zh(text)
        assert got == "Anthropic 发布新一代编程模型", "命中路径没走缓存：%r" % got
        assert list(B._trans_cache)[-1] == h, (
            "真实调用链没有 touch 缓存 ⇒ 裁剪会先把最常用的译文逐掉")

    def test_account_fields_do_not_fall_back_to_zero(self):
        """`None` 与 `0` 必须是两个样子；写成 .get("size", 0) 就把"没跑到"伪装成"没丢"。"""
        src = _src()
        for k in ("size", "trimmed"):
            assert '_LAST_TRANS_ACCOUNT.get("%s")' % k in src, (
                "build_logs 里 trans_cache_%s 带了默认值 ⇒ 这一步没跑到会被伪装成 0" % k)


class TestSaveWiring:
    def test_save_trims_and_writes_compact(self, tmp_path, monkeypatch):
        """写盘必须"先裁再写"，并且不再用 indent=2 —— 20.7MB 里约三成是缩进空白。"""
        cap = B.TRANS_CACHE_MAX
        path = tmp_path / "translations.json"
        monkeypatch.setattr(B, "TRANS_CACHE_FILE", str(path))
        monkeypatch.setattr(B, "_save_rss_cache", lambda: None, raising=False)
        B._trans_cache = _fill(cap + 20)
        B._save_trans_cache()
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
        assert len(data) == cap, "落盘 %d 条，应为裁剪后的 %d" % (len(data), cap)
        assert "\n  \"" not in raw, "仍是 indent=2 缩进格式 ⇒ 白白多提交几 MB"

    def test_build_log_carries_the_two_new_fields(self):
        """观测面：条数与丢弃数都要进 build_logs，否则"有没有裁"只能靠挖日志文本。"""
        src = _src()
        for key in ("trans_cache_size", "trans_cache_trimmed"):
            assert '"%s"' % key in src, "build_logs 缺字段 %s" % key
        assert "_LAST_TRANS_ACCOUNT.get" in src, \
            "新字段没接账本（写死或用 0 兜底 ⇒ 与历史侧 §12 那本恒负账同类错误）"

    def test_empty_cache_is_a_noop_not_a_crash(self):
        B._trans_cache = {}
        assert B._trim_trans_cache() == 0
        assert B._trans_cache == {}


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q", "-s"]))
