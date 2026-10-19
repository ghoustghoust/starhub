# -*- coding: utf-8 -*-
"""T2 类目与数据面判据（A3 advisory）。T2a-1 钉 CATS 13 类结构；T2a-2 钉 health_score、
LLM 熔断（3 次/5 分钟）与 DATA 注入（note/health/stale/starred_at）。"""
import os, sys, re
import json
from datetime import datetime, timedelta, timezone

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
import fetch_and_build as fab


@pytest.fixture(autouse=True)
def _reset_llm_breaker(monkeypatch):
    """熔断是模块级状态：每个用例前后自动归零（monkeypatch 自动恢复原值）。"""
    monkeypatch.setattr(fab, "_LLM_FAIL_STREAK", 0)
    monkeypatch.setattr(fab, "_LLM_BLOCK_UNTIL", 0.0)
    yield
    fab._LLM_FAIL_STREAK = 0
    fab._LLM_BLOCK_UNTIL = 0.0

def test_cats_has_13_unique_keys():
    keys = [c["key"] for c in fab.CATS]
    assert len(keys) == 13 and len(set(keys)) == 13
    for need in ("info", "media"):
        assert need in keys
    for c in fab.CATS:
        assert c["label"] and re.match(r"^#[0-9a-fA-F]{6}$", c["color"]) and re.match(r"^#[0-9a-fA-F]{6}$", c["dark"])

def test_tools_label_narrowed():
    t = [c for c in fab.CATS if c["key"] == "tools"][0]
    assert t["label"] == "效率工具"


# ──────────────────── health_score：四维降档（时间全部相对构造，禁写死日期） ────────────────────

def _repo(days=10, archived=False, stars=500, forks=40, desc="d", topics=("ai",)):
    pushed = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    return {"full_name": "acme/x", "pushed_at": pushed, "archived": archived,
            "stargazers_count": stars, "forks": forks,
            "description": desc, "topics": list(topics)}


def test_health_score_fresh_is_green():
    assert fab.health_score(_repo(days=60)) == {"tier": "green", "stale": False}

def test_health_score_midway_is_yellow():
    assert fab.health_score(_repo(days=200))["tier"] == "yellow"

def test_health_score_old_is_red_and_stale():
    got = fab.health_score(_repo(days=400))
    assert got == {"tier": "red", "stale": True}

def test_health_score_archived_is_red_regardless():
    assert fab.health_score(_repo(days=10, archived=True))["tier"] == "red"

def test_health_score_missing_pushed_is_unknown():
    got = fab.health_score({"full_name": "a/b"})
    assert got == {"tier": "unknown", "stale": False}

def test_health_score_bad_pushed_is_unknown():
    assert fab.health_score({"pushed_at": "not-a-date"})["tier"] == "unknown"

def test_health_score_low_community_downgrades():
    # 60 天 green，但 stars<50 且 forks<5 → yellow
    assert fab.health_score(_repo(days=60, stars=10, forks=1))["tier"] == "yellow"
    # 200 天 yellow + 低社区 → red
    assert fab.health_score(_repo(days=200, stars=10, forks=1))["tier"] == "red"

def test_health_score_no_desc_no_topics_downgrades():
    assert fab.health_score(_repo(days=60, desc="", topics=[]))["tier"] == "yellow"
    # red 不再被降档逻辑二次处理（保持 red）
    assert fab.health_score(_repo(days=400, desc="", topics=[]))["tier"] == "red"


# ──────────────────── LLM 熔断：连续 3 次失败暂停 5 分钟 ────────────────────

def test_breaker_trip_after_three_llm_failures(monkeypatch):
    calls = []
    monkeypatch.setattr(fab, "classify_llm",
                        lambda *a, **k: (calls.append(1), None)[1])
    known = {}
    for _ in range(3):
        got = fab.classify_repo("acme/llm-miss-%d" % len(calls), "plain utility", "", [], known)
        assert got == ("tools", "")
    assert fab._LLM_FAIL_STREAK >= 3
    assert fab._llm_available() is False
    # 第 4 次：熔断中，LLM 桩不再被调
    fab.classify_repo("acme/llm-miss-4", "plain utility", "", [], known)
    assert len(calls) == 3

def test_breaker_reset_on_success(monkeypatch):
    state = {"n": 0}
    def _llm(*a, **k):
        state["n"] += 1
        return {"category": "tools", "note": "x"} if state["n"] >= 3 else None
    monkeypatch.setattr(fab, "classify_llm", _llm)
    known = {}
    fab.classify_repo("a/1", "d", "", [], known)
    fab.classify_repo("a/2", "d", "", [], known)
    assert fab._LLM_FAIL_STREAK == 2
    fab.classify_repo("a/3", "d", "", [], known)
    assert fab._LLM_FAIL_STREAK == 0
    assert fab._llm_available() is True

def test_known_table_hit_does_not_touch_llm_or_breaker(monkeypatch):
    calls = []
    monkeypatch.setattr(fab, "classify_llm",
                        lambda *a, **k: (calls.append(1), {"category": "video", "note": "n"})[1])
    got = fab.classify_repo("acme/known", "d", "", [], {"acme/known": "coding"})
    assert got == ("coding", "")
    assert calls == [] and fab._LLM_FAIL_STREAK == 0

def test_cooldown_window_reopens(monkeypatch):
    # 过期时间戳（把 _LLM_BLOCK_UNTIL 拨到过去）→ 熔断解除
    monkeypatch.setattr(fab, "_LLM_BLOCK_UNTIL", 1.0)  # 1970 年 → 已过期
    assert fab._llm_available() is True


# ──────────────────── DATA 注入：note/health/stale/starred_at ────────────────────

def _entry():
    return {"id": "acme/demo", "name": "demo", "owner": "acme", "full_name": "acme/demo",
            "html_url": "https://github.com/acme/demo", "desc": "演示", "language": "Python",
            "stars": 42, "topics": ["ai"], "pushed_at": "2026-09-01",
            "updated_today": False, "category": "coding", "categoryLabel": "AI 编程 & 工具链",
            "note": "一句话点评", "health": "green", "stale": False, "starred_at": "2026-10-01T00:00:00Z"}

def test_data_injection_carries_new_fields():
    html = fab.build_index_html([_entry()], fab.CATS)
    assert '"note"' in html and '"health"' in html and '"stale"' in html and '"starred_at"' in html
    assert "一句话点评" in html

def test_data_injection_tolerates_missing_new_fields():
    bare = {k: v for k, v in _entry().items() if k not in ("note", "health", "stale", "starred_at")}
    html = fab.build_index_html([bare], fab.CATS)  # 不炸即可（旧条目形状兼容）
    assert '"acme/demo"' in html


# ──────────────────── 存量点评：assemble_entries 必须读 known_notes ────────────────────
# 端到端验收（2026-10-04）实锤：T2 重分类写库的 289 条点评，构建侧从不读取——
# classify_repo 查表命中恒 ("cat", "")，线上 DATA 的 note 全空。点评必须在组装层回填。

def _raw_repo(fn="acme/x"):
    owner, name = fn.split("/")
    return {"full_name": fn, "name": name, "owner": owner, "description": "d",
            "language": "Python", "stargazers_count": 5, "forks": 1, "topics": ["ai"],
            "html_url": "https://github.com/%s" % fn, "pushed_at": "2026-09-01T00:00:00Z"}


def test_assemble_entries_fills_note_from_notes_file_for_table_hits(monkeypatch):
    """查表命中（存量仓主路径）note 必须从 known_notes 回填——T2 写库不读取 = 白写。"""
    monkeypatch.setattr(fab, "classify_repo", lambda *a, **k: ("tools", ""))
    got = fab.assemble_entries([_raw_repo("acme/x")], {}, {}, {}, None,
                               notes={"acme/x": "存量点评"})
    assert got[0]["note"] == "存量点评"


def test_assemble_entries_prefers_fresh_llm_note_over_file(monkeypatch):
    """LLM 新鲜点评优先：刚分类的新星带 note 时不被文件旧值覆盖。"""
    monkeypatch.setattr(fab, "classify_repo", lambda *a, **k: ("tools", "新鲜点评"))
    got = fab.assemble_entries([_raw_repo("acme/x")], {}, {}, {}, None,
                               notes={"acme/x": "文件旧点评"})
    assert got[0]["note"] == "新鲜点评"


def test_assemble_entries_notes_none_or_missing_key_tolerated(monkeypatch):
    """notes 缺参/键缺失不炸不虚填（旧调用方 source 兼容）。"""
    monkeypatch.setattr(fab, "classify_repo", lambda *a, **k: ("tools", ""))
    got = fab.assemble_entries([_raw_repo("acme/x")], {}, {}, {}, None)
    assert got[0]["note"] == ""
    got2 = fab.assemble_entries([_raw_repo("acme/y")], {}, {}, {}, None,
                                notes={"acme/x": "别的仓的"})
    assert got2[0]["note"] == ""


# ──────────────────── 语义向量：量化与批量注入 ────────────────────

def test_quantize_normalized_round_trip_direction():
    v = [0.3, -0.4, 0.5, 0.0, -0.1] * 205 + [0.1]  # 1026 维任意向量
    q = fab.quantize_normalized(v)
    assert all(isinstance(x, int) and -127 <= x <= 127 for x in q)
    # 归一化后余弦==点积：量化前后与自身夹角方向一致（负向量的点积应为负）
    q2 = fab.quantize_normalized([-x for x in v])
    dot = sum(a * b for a, b in zip(q, q2))
    assert dot < 0  # 反向向量点积为负 = 方向信息保留
    dot_self = sum(a * a for a in q)
    assert dot_self > 0

def test_embed_star_entries_success_path(monkeypatch):
    def _fake_embed(texts):
        base = [0.1] * 1024
        return [list(base) for _ in texts]
    monkeypatch.setattr(fab, "embed_texts", _fake_embed)
    entries = [{"full_name": "a/b", "desc": "d1"}, {"full_name": "c/d", "desc": "d2"}]
    got = fab.embed_star_entries(entries)
    assert len(got[0]["emb"]) == 1024 and all(isinstance(x, int) for x in got[0]["emb"])

def test_embed_star_entries_degrades_on_failure(monkeypatch):
    monkeypatch.setattr(fab, "embed_texts", lambda *a, **k: None)
    entries = [{"full_name": "a/b", "desc": "d1"}]
    got = fab.embed_star_entries(entries)
    assert "emb" not in got[0]  # 降级：键缺省，前端退化纯关键词

def test_embed_texts_no_key_returns_none(monkeypatch):
    monkeypatch.delenv("SILICONFLOW_API_KEY", raising=False)
    assert fab.embed_texts(["x"]) is None

def test_embed_texts_shape_mismatch_returns_none(monkeypatch):
    monkeypatch.setenv("SILICONFLOW_API_KEY", "sk-test")
    body = {"data": [{"index": 0, "embedding": [0.1, 0.2]}]}
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda req, timeout=None: _fake_resp_json(body))
    assert fab.embed_texts(["one", "two"]) is None  # 2 条输入 1 条返回 → None


class _fake_resp_json:
    def __init__(self, obj):
        self._b = json.dumps(obj).encode()
    def read(self):
        return self._b
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


# ──────────────────── 类目导览：成员 hash 缓存 ────────────────────

def test_guides_cache_hit_skips_llm(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(fab, "_agnes_generate", lambda s, u, max_tokens=300: (calls.append(1), "导览文本")[1])
    cache_file = tmp_path / "guides.json"
    known = {"a/1": "tools", "a/2": "tools"}
    cats = [{"key": "tools", "label": "效率工具"}]
    got1 = fab.build_category_guides(cats, known, cache_path=str(cache_file))
    assert got1["tools"] == "导览文本" and calls == [1] and cache_file.exists()
    got2 = fab.build_category_guides(cats, known, cache_path=str(cache_file))
    assert got2["tools"] == "导览文本" and calls == [1]  # 成员未变 → 缓存命中，LLM 不再被调

def test_guides_member_change_regenerates(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(fab, "_agnes_generate", lambda s, u, max_tokens=300: (calls.append(1), "新导览")[1])
    cache_file = tmp_path / "guides.json"
    cats = [{"key": "tools", "label": "效率工具"}]
    fab.build_category_guides(cats, {"a/1": "tools"}, cache_path=str(cache_file))
    fab.build_category_guides(cats, {"a/1": "tools", "a/2": "tools"}, cache_path=str(cache_file))
    assert len(calls) == 2  # 成员变了 → 重新生成

def test_guides_llm_failure_is_silent(tmp_path, monkeypatch):
    monkeypatch.setattr(fab, "_agnes_generate", lambda *a, **k: None)
    got = fab.build_category_guides([{"key": "tools", "label": "效率工具"}], {"a/1": "tools"},
                                    cache_path=str(tmp_path / "g.json"))
    assert got == {}  # 失败静默：无导览键，前端不渲染
