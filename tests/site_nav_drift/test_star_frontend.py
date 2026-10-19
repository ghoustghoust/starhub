# -*- coding: utf-8 -*-
"""T7 前端增强判据（A3 advisory）。template.html 文本断言（不引浏览器）：
- 搜索语法解析/相关性打分/语义扩展函数存在；
- 限定符不进高亮词表；
- 防抖 + 搜索历史（datalist + localStorage）；
- 排序 relevant/starred 档与 sortTouched 状态机；
- 卡片：健康徽章/stale 置灰/AI 点评/topic·org 可点标签/语义角标；
- 导览 title 注入。"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TPL = os.path.join(ROOT, "template.html")


def _tpl():
    with open(TPL, encoding="utf-8") as f:
        return f.read()


def test_search_syntax_parser_exists():
    js = _tpl()
    for fn in ("function parseQueryTokens(", "function relevanceScore("):
        assert fn in js
    for q in ("stars?:", "forks?:", "language:", "license:", "pushed:>", "org:", "topic:", "in:name", "in:desc"):
        assert q in js, "限定符 %s 缺失" % q


def test_highlight_excludes_qualifiers():
    js = _tpl()
    m = re.search(r"function hl\(s\)\{.*?\n\}", js, re.S)
    assert m and "parseQueryTokens(state.q).words" in m.group(0), "hl 必须只高亮普通词（限定符会污染高亮）"


def test_relevant_sort_uses_sorttouched_state_machine():
    js = _tpl()
    assert "state.sortTouched" in js
    assert "sortTouched) ? 'relevant' : state.sort" in js, "用户显式选过排序档则不被自动相关度打扰"
    assert "sortSel').addEventListener('change', e => { state.sort = e.target.value; state.sortTouched = true;" in js


def test_semantic_expand_uses_centroid_not_query_vector():
    js = _tpl()
    assert "semantic" in js and "centroid" in js
    # 质心来自命中集（无需给查询词做 embedding）；无 emb 时整段退化
    assert "DATA.some(d=>d.emb)" in js


def test_semantic_centroid_filters_by_exact_dim():
    """P3 修复判据：维度守卫必须是【精确等于 dim】，不是"长度非 0"。
    一条维度不符的长向量会把 centroid 越界项累成 NaN ⇒ 所有 sim=NaN ⇒
    语义扩展整体静默归零（2026-10-04 对抗性审查发现）。命中集与候选集两处都要守。"""
    js = _tpl()
    assert js.count("emb.length===dim") >= 2, \
        "命中集与候选集两处过滤都必须用 emb.length===dim（长度非 0 的过滤挡不住维度不符）"


def test_debounce_and_history():
    js = _tpl()
    assert "setTimeout(" in js and "clearTimeout(_qTimer)" in js
    assert "starhub_search_history" in js and 'id="qHistory"' in js
    assert "loadHistory().map" in js


def test_card_badges_and_clickable_tags():
    js = _tpl()
    assert "hb-'+d.health" in js
    assert "card.stale" in js or "d.stale?' stale'" in js
    assert "note-line" in js and "sem-badge" in js
    assert 'data-act="topictag"' in js and 'data-act="orgtag"' in js
    assert "act === 'topictag'" in js and "act === 'orgtag'" in js  # 委托处理存在


def test_guide_title_injected_in_cats_nav():
    js = _tpl()
    assert "if(c.guide) b.title = c.guide;" in js


def test_updated_filter_and_starred_sort_present():
    js = _tpl()
    assert 'id="updatedSel"' in js and "state.updated==='week'" in js
    assert "'starred': (a,b)=>(b.starred_at||'').localeCompare" in js
