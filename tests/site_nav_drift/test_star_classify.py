# -*- coding: utf-8 -*-
"""T1 分类引擎改造判据（A3 advisory，SPEC 2026-10-02 §4-T1）。

覆盖 fetch_and_build.py 新增/改造的接口（快车道 T3 与重分类 T2 依赖）：
- classify_repo：统一入口，顺序 查表 → LLM → 规则 → 默认 ("tools", "")，任何异常不向上抛；
- classify_llm：Agnes chat completions，AGNES_API_KEY+AGNES_API_KEYS 逗号合并 key 池
  （先建池再判空）、429 换下一个 key、401/403 立即停、enable_thinking:false、
  要求模型只回 JSON，解析失败返回 None；单次尝试不重试不阻塞；
- build_index_html：纯字符串渲染，8 个占位符全部替换、不残留 __XXX__（配平断言）；
- fetch_stars：Accept: application/vnd.github.star+json → 拆 {"starred_at", "repo"} 包装，
  每个 repo dict 附 starred_at（取不到为 ""），兼容旧结构。

规则修正两条：`quant` 不再子串误伤 quantization（NVIDIA/Model-Optimizer 实锤误进
finance）；点名式补丁词退场（fincept 等——查表与 LLM 接管后不再需要）。

网络层全部打桩，不打真 API；断言不写死"今天"日期。
"""
import inspect
import json
import os
import re
import sys
import urllib.error

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import fetch_and_build as fab  # noqa: E402


# ──────────────────── 网络层打桩 ────────────────────

class _FakeResp:
    """最小 urlopen 返回物：with 语句 + read()。"""

    def __init__(self, body):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _json_resp(obj):
    return _FakeResp(json.dumps(obj).encode("utf-8"))


def _llm_body(content):
    """Agnes chat completions 形状的 200 响应。"""
    return _json_resp({"choices": [{"message": {"content": content}}]})


def _http_error(req, code):
    return urllib.error.HTTPError(req.full_url, code, "HTTP %s" % code, None, None)


# ──────────────────── classify_repo：查表 → LLM → 规则 → tools ────────────────────

def test_classify_repo_known_table_hit_skips_llm(monkeypatch):
    """① 查表命中直接返回映射值，不走 LLM（monkeypatch 计数必须为 0）。"""
    calls = []

    def _fake_llm(fn, desc, lang, topics, cats):
        calls.append(fn)
        return {"category": "video", "note": "不该被调用"}

    monkeypatch.setattr(fab, "classify_llm", _fake_llm)
    known = {"acme/redis-lite": "coding"}
    got = fab.classify_repo("acme/redis-lite", "a tiny data store", "C", [], known)
    assert got == ("coding", "")
    assert calls == []


def test_classify_repo_llm_none_falls_back_to_rules(monkeypatch):
    """② LLM 返回 None（不可用/熔断中）时降级到关键词规则。"""
    monkeypatch.setattr(fab, "classify_llm", lambda *a, **k: None)
    got = fab.classify_repo("acme/iptv-finder", "IPTV 直播源聚合工具", "", [], {})
    assert got == ("tools", "")


def test_classify_repo_rule_miss_defaults_to_tools(monkeypatch):
    """③ 规则也不命中得 tools；LLM 回复解析失败必须返回 None 而不是抛异常。"""
    # 网络层打桩：LLM 正常 200，但回复内容不是 JSON → 解析失败 → None
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda req, timeout=None: _llm_body("抱歉，我无法分类这个仓库"))
    assert fab.classify_llm("acme/plain", "a plain utility", "", [], fab.CATS) is None
    # classify_repo 全链路：LLM None → 规则未命中 → 默认 tools
    got = fab.classify_repo("acme/no-keyword-repo", "just a plain utility", "", [], {})
    assert got == ("tools", "")


def test_classify_repo_swallows_llm_crash(monkeypatch):
    """classify_llm 内部崩溃时 classify_repo 不向上抛，落到规则。"""
    def _boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(fab, "classify_llm", _boom)
    got = fab.classify_repo("acme/iptv-finder", "IPTV 直播源聚合工具", "", [], {})
    assert got == ("tools", "")


# ──────────────────── classify_llm：Agnes key 池 ────────────────────

def test_classify_llm_success_parses_json_caps_note(monkeypatch):
    """成功路径：只回 JSON 也好、围栏包裹也好都能解析；note 截到 30 字；类目必须在 CATS 内。"""
    monkeypatch.setenv("AGNES_API_KEY", "k1")
    monkeypatch.delenv("AGNES_API_KEYS", raising=False)
    seen = {}

    def _fake(req, timeout=None):
        seen["auth"] = req.headers.get("Authorization")
        payload = json.loads(req.data.decode("utf-8"))
        seen["payload"] = payload
        return _llm_body('{"category": "video", "note": "%s"}' % ("视" * 40))

    monkeypatch.setattr("urllib.request.urlopen", _fake)
    got = fab.classify_llm("acme/canvas-video", "AI video canvas", "Python", ["video"], fab.CATS)
    assert got == {"category": "video", "note": "视" * 30}
    assert seen["auth"] == "Bearer k1"
    assert seen["payload"]["chat_template_kwargs"]["enable_thinking"] is False

    # 围栏包裹的 JSON 也能解析
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda req, timeout=None: _llm_body(
                            '好的，结果如下：```json\n{"category": "coding", "note": "编程工具"}\n```'))
    assert fab.classify_llm("a/b", "d", "", [], fab.CATS) == {"category": "coding", "note": "编程工具"}

    # 类目 key 不在 CATS 内 → 视为失败
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda req, timeout=None: _llm_body('{"category": "no-such-cat", "note": "x"}'))
    assert fab.classify_llm("a/b", "d", "", [], fab.CATS) is None


def test_classify_llm_429_rotates_to_next_key(monkeypatch):
    """429 换下一个 key（key 池 = AGNES_API_KEY 逗号分隔 + AGNES_API_KEYS 合并）。"""
    monkeypatch.setenv("AGNES_API_KEY", "k1,k2")
    monkeypatch.setenv("AGNES_API_KEYS", "k3")
    auth_seen = []

    def _fake(req, timeout=None):
        auth_seen.append(req.headers.get("Authorization"))
        if len(auth_seen) == 1:
            raise _http_error(req, 429)
        return _llm_body('{"category": "tools", "note": "工具"}')

    monkeypatch.setattr("urllib.request.urlopen", _fake)
    got = fab.classify_llm("a/b", "d", "", [], fab.CATS)
    assert got == {"category": "tools", "note": "工具"}
    assert auth_seen[0] == "Bearer k1"
    assert auth_seen[1] == "Bearer k2"


def test_classify_llm_all_keys_429_returns_none(monkeypatch):
    """全池 429：单轮轮转后放弃，返回 None（不重试不阻塞）。"""
    monkeypatch.setenv("AGNES_API_KEY", "k1,k2")
    monkeypatch.delenv("AGNES_API_KEYS", raising=False)
    auth_seen = []

    def _fake(req, timeout=None):
        auth_seen.append(req.headers.get("Authorization"))
        raise _http_error(req, 429)

    monkeypatch.setattr("urllib.request.urlopen", _fake)
    assert fab.classify_llm("a/b", "d", "", [], fab.CATS) is None
    assert auth_seen == ["Bearer k1", "Bearer k2"]


def test_classify_llm_401_403_stop_immediately(monkeypatch):
    """401/403 key 无效：立即停，不烧池里剩下的 key。"""
    monkeypatch.setenv("AGNES_API_KEY", "k1,k2")
    monkeypatch.delenv("AGNES_API_KEYS", raising=False)
    auth_seen = []

    def _fake(req, timeout=None):
        auth_seen.append(req.headers.get("Authorization"))
        raise _http_error(req, 401)

    monkeypatch.setattr("urllib.request.urlopen", _fake)
    assert fab.classify_llm("a/b", "d", "", [], fab.CATS) is None
    assert auth_seen == ["Bearer k1"]


def test_classify_llm_empty_pool_returns_none_without_request(monkeypatch):
    """先建池再判空：池空直接 None，一个请求都不发。"""
    monkeypatch.delenv("AGNES_API_KEY", raising=False)
    monkeypatch.delenv("AGNES_API_KEYS", raising=False)

    def _boom(req, timeout=None):
        raise AssertionError("池空不应发起任何请求")

    monkeypatch.setattr("urllib.request.urlopen", _boom)
    assert fab.classify_llm("a/b", "d", "", [], fab.CATS) is None


# ──────────────────── classify_new：规则修正 ────────────────────

def test_classify_new_quantization_no_longer_finance():
    """④ "quant" 子串误伤：'quantization' 不得再进 finance（NVIDIA/Model-Optimizer 实锤）。"""
    got = fab.classify_new("a/b", "A model quantization toolkit for LLM inference", "", [])
    assert got != "finance"
    # 真·量化金融项目不受影响（trading/金融/交易 等通用词仍在）
    assert fab.classify_new("x/quantlib", "Quantitative trading library", "", []) == "finance"


_NAMED_WORDS_GONE = [
    "fincept", "officecli", "reasonix", "hellogithub", "qwenpaw", "nuwax",
    "opensquilla", "cc-connect", "sub2api", "freellmapi", "mimo", "2api",
]


def test_classify_new_named_patch_words_removed():
    """⑤ 点名式补丁词退场：词表与行为面双重断言（通用语义词不在本断言范围）。"""
    src = inspect.getsource(fab.classify_new)
    for word in _NAMED_WORDS_GONE:
        assert word not in src, "点名式补丁词 %r 应已删除（查表与 LLM 接管）" % word
    # 行为面：点名词不再触发原类目
    assert fab.classify_new("x/fincept", "portfolio analytics dashboard", "", []) != "finance"


def test_classify_new_keeps_generic_relay_word():
    """「中转」是通用语义词（API 中转站类项目），不在点名词删除授权范围，须保留进 coding。"""
    assert fab.classify_new("acme/api-relay", "LLM API 中转站，聚合多家模型接口", "", []) == "coding"


# ──────────────────── fetch_stars：starred_at 适配 ────────────────────

def test_fetch_stars_unwraps_starred_at_and_keeps_legacy(monkeypatch):
    """带 Accept: star+json 后元素是 {"starred_at", "repo"} 包装：拆包、附 starred_at、
    同页混入的旧结构（无包装）也能兼容并补 starred_at=""。"""
    wrapper = {
        "starred_at": "2026-09-30T12:00:00Z",
        "repo": {"full_name": "acme/one", "name": "one", "description": "d1",
                 "language": "Python", "stargazers_count": 5, "topics": ["ai"],
                 "html_url": "https://github.com/acme/one",
                 "pushed_at": "2026-09-01T00:00:00Z"},
    }
    legacy = {"full_name": "acme/two", "name": "two", "description": "d2",
              "language": "Go", "stargazers_count": 7, "topics": [],
              "html_url": "https://github.com/acme/two",
              "pushed_at": "2026-09-02T00:00:00Z"}
    seen_accept = []

    def _fake(req, timeout=None):
        seen_accept.append(req.headers.get("Accept"))
        return _json_resp([wrapper, legacy])

    monkeypatch.setattr("urllib.request.urlopen", _fake)
    monkeypatch.setattr(fab.time, "sleep", lambda s: None)
    repos = fab.fetch_stars(token="t")
    assert seen_accept == ["application/vnd.github.star+json"]
    assert len(repos) == 2
    # 包装元素：拆成 repo dict，starred_at 保留，包装键不外漏
    assert repos[0]["full_name"] == "acme/one"
    assert repos[0]["starred_at"] == "2026-09-30T12:00:00Z"
    assert "repo" not in repos[0]
    # 旧结构兼容：原样保留 + 补 starred_at=""
    assert repos[1]["full_name"] == "acme/two"
    assert repos[1]["starred_at"] == ""
    # main 的二级取值路径不受影响（元素仍是 repo dict）
    assert repos[0].get("description") == "d1"
    assert repos[0].get("stargazers_count") == 5


def test_fetch_stars_failure_returns_none(monkeypatch):
    """拉取失败返回 None 的既有契约不变（stars_ok 兜底判断依赖它）。"""
    def _fake(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 403, "rate limited", None, None)

    monkeypatch.setattr("urllib.request.urlopen", _fake)
    assert fab.fetch_stars(token="t") is None


# ──────────────────── build_index_html：纯渲染 + 占位符配平 ────────────────────

def _entry(fn="acme/demo", cat="coding", label="AI 编程 & 工具链"):
    owner, name = fn.split("/")
    return {
        "id": fn, "name": name, "owner": owner, "full_name": fn,
        "html_url": "https://github.com/%s" % fn, "desc": "演示项目",
        "language": "Python", "stars": 42, "topics": ["ai"], "pushed_at": "2026-09-01",
        "updated_today": False, "category": cat, "categoryLabel": label,
    }


def test_build_index_html_renders_and_replaces_all_placeholders():
    """⑥ 输出含 const DATA =；__DATA__/__CATS__/__UPDATED__ 等全部占位符被替换，
    生成文本不残留任何 __XXX__ 占位符（配平断言）。"""
    html = fab.build_index_html([_entry()], fab.CATS)
    assert "const DATA = " in html
    assert '"acme/demo"' in html                      # 条目进了 DATA
    assert "const CATS = " in html and '"finance"' in html   # CATS 注入
    for ph in ("__DATA__", "__CATS__", "__UPDATED__", "__LANGS__",
               "__FAVS__", "__TRENDING__", "__FEED__", "__AI_SUMMARY__"):
        assert ph not in html
    assert not re.search(r"__[A-Z][A-Z_]*__", html)   # 配平：不残留任何占位符


def test_build_index_html_is_pure_no_write_no_network():
    """纯函数约束：不写文件、不发网络（缺省参数渲染空态即可完成）。"""
    html1 = fab.build_index_html([_entry()], fab.CATS)
    html2 = fab.build_index_html([], fab.CATS)
    assert "const DATA = []" in html2                 # 空条目也是合法产物
    assert html1 != html2


def test_classify_llm_keys_only_extra_key_still_works(monkeypatch):
    """§8.14：主 key 缺失时附加 key 仍生效（AGNES_API_KEYS-only 也必须能发请求）。"""
    monkeypatch.delenv("AGNES_API_KEY", raising=False)
    monkeypatch.setenv("AGNES_API_KEYS", "k9")

    def _fake_urlopen(req, timeout=None):
        seen.append(req.headers.get("Authorization"))
        return _llm_body('{"category": "tools", "note": "工具"}')

    seen = []
    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
    got = fab.classify_llm("a/b", "d", "", [], fab.CATS)
    assert got == {"category": "tools", "note": "工具"}
    assert seen == ["Bearer k9"]
