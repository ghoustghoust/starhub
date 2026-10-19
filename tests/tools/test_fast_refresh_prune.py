# -*- coding: utf-8 -*-
"""`fast_refresh.py` 侧的剪枝与 live 名单判据（A3）。

为什么这几个判据必须存在：main 侧的构建期剪枝（`fetch_and_build.prune_star_state`）管不到
star-state，而写端 `--merge-onto` 要剪枝就得拿到"当前收藏到底有哪些"——这份名单只有
fast_refresh 有（它是这条车道唯一 fetch_stars 的地方）。所以名单由它写、由写端读。

还有一半更容易漏：star-fast.yml 的提交步是用 `grep "no change, skip write" fast.log`
决定要不要往下走的，命中就 `exit 0`，**连 --merge-onto 都不跑**。所以"无新星但有死键"的
场次如果还打那行日志，剪枝就永远不会发生。这四条里的第三、第四条就是钉这个边界的：
有死键要往下走，什么都没变才许跳过。

全程不碰网络：fetch_stars 与 assemble_entries 都用 monkeypatch 顶掉（后者会打翻译 API
和 README，实跑就成了集成测试）。
"""
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import fast_refresh as fr  # noqa: E402
import fetch_and_build as fab  # noqa: E402

# 20 条而不是 3 条：prune_star_state 有 0.8 缩水门槛，"3 活 + 1 死"的表要剪掉 25% 会被门槛
# 正确拦下，判据就会红在夹具规模上而不是被测行为上（本仓已在 main 侧判据上栽过一次）。
LIVE = ["o%02d/keep" % i for i in range(20)]


@pytest.fixture()
def ws(tmp_path, monkeypatch):
    """一个假工作树：两张状态表里各埋一把死键，live 名单与网络全由本文件控制。"""
    paths = {name: str(tmp_path / name)
             for name in ("KC_PATH", "NOTES_PATH", "DESC_PATH", "LIVE_LIST_PATH")}
    monkeypatch.setattr(fr, "ROOT", str(tmp_path))
    for attr, p in paths.items():
        monkeypatch.setattr(fr, attr, p)
    kc = {n: "agent" for n in LIVE}
    kc["old/renamed"] = "coding"
    notes = {n: "点评" for n in LIVE}
    notes["old/renamed"] = "该剪的死键点评"
    with open(fr.KC_PATH, "w", encoding="utf-8") as f:
        json.dump(kc, f)
    with open(fr.NOTES_PATH, "w", encoding="utf-8") as f:
        json.dump(notes, f)
    with open(fr.DESC_PATH, "w", encoding="utf-8") as f:
        json.dump({}, f)
    monkeypatch.setattr(fab, "fetch_stars",
                        lambda token=None: [{"full_name": n} for n in LIVE])
    # assemble_entries 会打翻译 API 与 README，这里只要求它把 known 原样带过
    monkeypatch.setattr(fab, "assemble_entries",
                        lambda repos, known, desc, label, token, notes=None: [])
    return str(tmp_path)


def _read(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def test_fast_writes_live_list_for_the_merge_side(ws, capsys):
    """名单必须真的落到场内文件，且内容就是当前收藏的 full_name 集合。

    写端 `--merge-onto` 只认这个文件；它读不到就整场不剪（降级），所以"写没写、写对没有"
    是这条链的第一环，必须单独钉，不能靠下游判据间接证明。
    """
    assert fr.main() == 0
    assert os.path.exists(fr.LIVE_LIST_PATH), \
        "没写 live 名单 ⇒ 写端并集读不到，star-state 永远不会剪枝（等于这半没做）"
    assert sorted(_read(fr.LIVE_LIST_PATH)) == sorted(LIVE), "名单内容与收藏不一致"


def test_fast_prunes_categories_and_leaves_notes_to_the_merge_side(ws):
    """本车道只剪 categories；notes 交给写端并集剪（那边两表都剪）。

    分工的理由见 fast_refresh 里那条注释：notes 那份冻结副本会让"剪 notes"永远判成有变化，
    从而每 15 分钟推一枚内容没变的空提交。两表口径最终仍一致，由
    test_restore_star_state.py::test_write_prune_drops_branch_only_dead_key 钉住。
    """
    assert fr.main() == 0
    assert "old/renamed" not in _read(fr.KC_PATH), "categories 里的死键没剪"
    assert "old/renamed" in _read(fr.NOTES_PATH), \
        "本车道动了 notes ⇒ 会造出每 15 分钟一次的空提交（见 test_notes_dead_keys_alone_never_trigger_a_write）"
    for n in LIVE:
        assert n in _read(fr.KC_PATH), "活键被误删：%s" % n



def test_unstar_without_new_star_still_writes(ws, capsys):
    """无新星但**有死键**时不许打 "no change, skip write"。

    那行日志是 star-fast.yml 提交步的 grep 锚点，命中就 exit 0 且连 --merge-onto 都不跑
    ⇒ 打了它就等于"用户取消关注这件事，这条车道永远不处理"，正是本次要消灭的形态。
    """
    assert fr.main() == 0
    out = capsys.readouterr().out
    assert "no change, skip write" not in out, \
        "有死键可剪却仍报无变化 ⇒ 提交步会提前 exit，写端剪枝根本没机会跑"
    assert os.path.exists(fr.KC_PATH)
    assert "old/renamed" not in _read(fr.KC_PATH)


def test_no_change_at_all_still_skips_the_write(ws, monkeypatch, capsys):
    """反向那半：无新星**且**无死键 ⇒ 仍须打那行并零文件写。

    实测 96% 的场次走这条（`test_no_new_star_pushes_nothing_at_all` 钉的是 shell 侧，
    本条钉的是产生那行日志的 Python 侧）。放宽它就等于每 15 分钟白涨一次提交，
    正是手册里"别把 known_categories 的 git 增量放大到 15 分钟节奏"那条账。
    """
    clean = {n: "agent" for n in LIVE}
    with open(fr.KC_PATH, "w", encoding="utf-8") as f:
        json.dump(clean, f)
    with open(fr.NOTES_PATH, "w", encoding="utf-8") as f:
        json.dump({n: "点评" for n in LIVE}, f)
    before = open(fr.KC_PATH, "rb").read()
    assert fr.main() == 0
    assert "no change, skip write" in capsys.readouterr().out, \
        "什么都没变却不报跳过 ⇒ 本车道会开始每 15 分钟推一次空提交"
    assert open(fr.KC_PATH, "rb").read() == before, "跳过路径上却写了文件"


def test_fetch_failure_writes_no_list(ws, monkeypatch, capsys):
    """拉取失败时不许写名单：下游若读到一份"空名单"，最坏会把两张表剪空。"""
    monkeypatch.setattr(fab, "fetch_stars", lambda token=None: None)
    assert fr.main() == 0
    assert not os.path.exists(fr.LIVE_LIST_PATH), \
        "拉取失败却写了名单 ⇒ 写端可能拿它当依据把表剪小"


def test_pure_prune_never_runs_assemble_entries(ws, monkeypatch):
    """无新星、只有死键的场次**绝不能**跑 assemble_entries。

    它对全表做翻译与 README 摘要（原本靠 `if not new_repos: return 0` 挡掉），为剪几个键
    把 341 条全过一遍 LLM 是本车道最贵的错误。上面那条 fixture 把它 mock 成恒等，
    所以"跑没跑"必须在这里显式数调用次数，否则整条链没人盯。
    """
    hits = []
    monkeypatch.setattr(fab, "assemble_entries",
                        lambda *a, **k: hits.append(1) or [])
    assert fr.main() == 0
    assert "old/renamed" not in _read(fr.KC_PATH), "夹具没造出死键 ⇒ 本条会空转"
    assert not hits, "纯剪枝场跑了 assemble_entries ⇒ 白烧一轮翻译与 LLM"


def test_notes_dead_keys_alone_never_trigger_a_write(ws, monkeypatch, capsys):
    """notes 里有死键、categories 干净、又没有新星 ⇒ 必须走跳过路径，一个字都不写。

    这条是修我自己犯的那个错：2026-10-12 05:45 与 05:30 两场各剪了"同样 4 把"并各推一次
    star-state，而推出去的 tree 与上一场**完全相同**。根因是 `main/known_notes.json` 是一份
    没人写、却参与读端并集的冻结副本（update.yml 的 add 清单里没有它），star-fast 检出 main
    就每场带着它那 4 把死键 ⇒ 用"dropped 非空"当"有变化"会永远判成有变化，
    变成每 15 分钟一枚空提交（手册 test_no_new_star_pushes_nothing_at_all 明确反对的形态）。
    notes 的剪枝归 merge-onto 管（那边两表都剪，已由 test_write_prune_drops_branch_only_dead_key 钉住）。
    """
    clean = {n: "agent" for n in LIVE}
    with open(fr.KC_PATH, "w", encoding="utf-8") as f:
        json.dump(clean, f)
    dirty_notes = {n: "点评" for n in LIVE}
    dirty_notes["old/renamed"] = "main 冻结副本带来的死键"
    with open(fr.NOTES_PATH, "w", encoding="utf-8") as f:
        json.dump(dirty_notes, f)
    kc_before = open(fr.KC_PATH, "rb").read()
    notes_before = open(fr.NOTES_PATH, "rb").read()
    assert fr.main() == 0
    assert "no change, skip write" in capsys.readouterr().out, \
        "只因为 notes 有死键就不跳过 ⇒ 每 15 分钟往 star-state 推一枚内容没变的空提交"
    assert open(fr.KC_PATH, "rb").read() == kc_before, "跳过路径上却写了 categories"
    assert open(fr.NOTES_PATH, "rb").read() == notes_before, "fast_refresh 不该动 notes（归 merge-onto）"


def test_live_list_path_is_the_same_on_both_sides():
    """写名单的 `fast_refresh` 与读名单的 `restore_star_state` 必须指向同一个路径。

    两侧各写一份字面量是刻意的：`restore_star_state` 不 import 任何东西，因为它的约束③ 是
    "读不到只降级、绝不挡发布"，多一个 import 就多一个崩点（它连 fetch_and_build 都不碰）。
    代价是路径分叉的风险，而分叉的表现是"永远不剪"且全程零报错 —— 比崩更难查，所以必须钉。
    """
    sys.path.insert(0, os.path.join(ROOT, "tools"))
    import restore_star_state as rss
    assert os.path.join(ROOT, rss.LIVE_LIST_REL) == fr.LIVE_LIST_PATH, \
        "两侧分叉：写到 %s，读端找 %s" % (fr.LIVE_LIST_PATH,
                                          os.path.join(ROOT, rss.LIVE_LIST_REL))

