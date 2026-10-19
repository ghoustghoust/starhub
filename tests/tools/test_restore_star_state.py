# -*- coding: utf-8 -*-
"""`tools/restore_star_state.py` 的行为判据（真跑 git，不读文本）。

为什么必须实跑：这个脚本的失败模式全是"静默把状态弄坏"——并集写成覆盖、地板闸门形同不存在、
分支缺失时把 main 那份清空。读源码看不出任何一条，只有把 git 搭起来跑一遍才知道。
搭法与 tests/site_nav_drift/test_star_fast_wiring.py 的撞车判据同：临时裸仓当 origin，
monkeypatch 模块的 ROOT，全程不碰网络、不碰真仓。

跑法：py -3.11 -m pytest tests/tools/test_restore_star_state.py -q
（本目录不在 A2/A3 的 CI 清单里，是本地守卫 ⇒ 改这个脚本必须手工复跑本文件。）
"""
import json
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import restore_star_state as rss  # noqa: E402


def _g(cwd, *args):
    r = subprocess.run(("git", "-C", cwd) + args, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    assert r.returncode == 0, " ".join(args) + " 失败: " + ((r.stderr or "") + (r.stdout or ""))[:300]
    return (r.stdout or "").strip()


def _read(ws, name):
    with open(os.path.join(ws, name), encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture()
def ws(tmp_path):
    """origin 裸仓 + 一个工作树（含两个状态文件与一次提交），origin 上只有 main。"""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    _g(str(origin), "symbolic-ref", "HEAD", "refs/heads/main")
    here = str(tmp_path / "ws")
    subprocess.run(["git", "clone", "-q", str(origin), here], check=True)
    _g(here, "config", "user.name", "T")
    _g(here, "config", "user.email", "t@example.com")
    _g(here, "checkout", "-q", "-b", "main")
    kc = {"old/%d" % i: "tools" for i in range(rss.FLOOR_KEYS + 20)}
    kc["dup/one"] = "coding"
    with open(os.path.join(here, "known_categories.json"), "w", encoding="utf-8") as f:
        json.dump(kc, f)
    with open(os.path.join(here, "known_notes.json"), "w", encoding="utf-8") as f:
        json.dump({"old/0": "老点评"}, f)
    _g(here, "add", "known_categories.json", "known_notes.json")
    _g(here, "commit", "-qm", "seed")
    _g(here, "push", "-q", "-u", "origin", "main")
    # 脚本用模块级 ROOT，指到工作树才算"在 CI 的工作目录里跑"
    rss.ROOT = here
    yield here
    rss.ROOT = ROOT


def _make_star_state(here, kc_payload, notes_payload=None):
    """在 origin 上造一个 orphan 的 star-state 分支，只含状态文件。"""
    idx = os.path.join(here, ".tmpidx")
    env = {**os.environ, "GIT_INDEX_FILE": idx, "GIT_AUTHOR_NAME": "S",
           "GIT_AUTHOR_EMAIL": "s@e.com", "GIT_COMMITTER_NAME": "S", "GIT_COMMITTER_EMAIL": "s@e.com"}
    if os.path.exists(idx):
        os.remove(idx)
    for path, payload in (("known_categories.json", kc_payload),
                          ("known_notes.json", notes_payload if notes_payload is not None else {})):
        blob = _g(here, "hash-object", "-w", "--stdin") if False else None
        p = os.path.join(here, ".stage_" + path)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        sha = subprocess.run(("git", "-C", here, "hash-object", "-w", p),
                             capture_output=True, text=True, check=True).stdout.strip()
        subprocess.run(("git", "-C", here, "update-index", "--add", "--cacheinfo",
                        "100644,%s,%s" % (sha, path)), env=env, check=True)
    tree = subprocess.run(("git", "-C", here, "write-tree"), env=env,
                          capture_output=True, text=True, check=True).stdout.strip()
    commit = subprocess.run(("git", "-C", here, "commit-tree", tree, "-m", "star state"),
                            env=env, capture_output=True, text=True, check=True).stdout.strip()
    _g(here, "push", "-q", "origin", "%s:refs/heads/star-state" % commit)
    os.remove(idx)


# ── 判据 ──────────────────────────────────────────────────────────────────
def test_absent_branch_is_a_safe_noop(ws, capsys):
    """分支还没有 ⇒ 不动任何文件、退出 0、报 warning。新链路绝不能挡在发布前面。"""
    assert rss.main([]) == 0
    out = capsys.readouterr().out
    assert "::warning::" in out and "star-state" in out
    assert "old/0" in _read(ws, "known_notes.json")
    assert len(_read(ws, "known_categories.json")) == rss.FLOOR_KEYS + 21


def test_union_keeps_main_only_keys_and_lets_branch_win_on_conflict(ws):
    """并集语义的两半：main 独有的键必须在，同名键以 star-state 为准（它更新）。"""
    _make_star_state(ws, {"new/star": "agent", "dup/one": "video"})
    assert rss.main([]) == 0
    kc = _read(ws, "known_categories.json")
    assert "new/star" in kc, "分支上的新分类没并进来"
    assert "old/1" in kc, "main 独有的键被覆盖掉了 ⇒ 并集写成了替换"
    assert kc["dup/one"] == "video", "同名键该由 star-state 赢"


def test_emptied_branch_never_shrinks_the_table(ws):
    """核心安全属性：star-state 被误清空成 {} 时，结果必须仍等于 main 那份。

    这一条是"绝不缩小"的全部价值所在——地板闸门只挡空表，挡不住"半空"，
    而并集连半空都不怕。写成替换语义的话，这里会直接少掉 FLOOR+21 个键。
    """
    before = _read(ws, "known_categories.json")
    _make_star_state(ws, {})
    assert rss.main([]) == 0
    assert _read(ws, "known_categories.json") == before


def test_floor_gate_refuses_to_write_anything(ws, capsys):
    """地板闸门：合并后键数不足 ⇒ 一个文件都不写（连达标的 notes 也不写，保持两边同源）。"""
    small = {"a/1": "tools", "a/2": "agent"}
    with open(os.path.join(ws, "known_categories.json"), "w", encoding="utf-8") as f:
        json.dump(small, f)
    _make_star_state(ws, {"a/3": "video"}, {"a/1": "新点评"})
    assert rss.main([]) == 0
    assert "::error::" in capsys.readouterr().out
    assert _read(ws, "known_categories.json") == small, "地板没过还写了 ⇒ 闸门形同不存在"
    assert "a/1" not in _read(ws, "known_notes.json"), "闸门拒绝后仍写了另一个文件"


def test_corrupt_branch_json_keeps_main_copy(ws, capsys):
    """分支上是不合法 JSON ⇒ 保留 main 那份并出声，不能崩、不能清空。"""
    p = os.path.join(ws, ".bad")
    with open(p, "w", encoding="utf-8") as f:
        f.write("{not json")
    env = {**os.environ, "GIT_AUTHOR_NAME": "S", "GIT_AUTHOR_EMAIL": "s@e.com",
           "GIT_COMMITTER_NAME": "S", "GIT_COMMITTER_EMAIL": "s@e.com"}
    sha = subprocess.run(("git", "-C", ws, "hash-object", "-w", p),
                         capture_output=True, text=True, check=True).stdout.strip()
    idx = os.path.join(ws, ".idx2")
    env["GIT_INDEX_FILE"] = idx
    subprocess.run(("git", "-C", ws, "read-tree", "--empty"), env=env, check=True)
    subprocess.run(("git", "-C", ws, "update-index", "--add", "--cacheinfo",
                    "100644,%s,known_categories.json" % sha), env=env, check=True)
    tree = subprocess.run(("git", "-C", ws, "write-tree"), env=env,
                          capture_output=True, text=True, check=True).stdout.strip()
    commit = subprocess.run(("git", "-C", ws, "commit-tree", tree, "-m", "bad"), env=env,
                            capture_output=True, text=True, check=True).stdout.strip()
    _g(ws, "push", "-q", "origin", "%s:refs/heads/star-state" % commit)
    before = _read(ws, "known_categories.json")
    assert rss.main([]) == 0
    assert "::warning::" in capsys.readouterr().out
    assert _read(ws, "known_categories.json") == before


def test_readback_is_recorded_in_build_logs(ws):
    """落痕：每次合并都要在 build_logs 里留下一条可查证据（否则线上无从判断读回有没有生效）。"""
    _make_star_state(ws, {"new/x": "tools"})
    assert rss.main([]) == 0
    d = os.path.join(ws, "build_logs")
    files = [n for n in os.listdir(d) if n.endswith(".jsonl")]
    assert files, "没有落痕目录/文件 ⇒ 线上无法验证这一步真跑过"
    lines = []
    for n in files:
        with open(os.path.join(d, n), encoding="utf-8") as f:
            lines += f.read().strip().splitlines()
    rec = [json.loads(x) for x in lines]
    assert any(r.get("type") == "star_state_readback" and r.get("kind") == "merged" for r in rec), \
        "落痕里没有 merged 记录"


def _branch_tip(here):
    return _g(here, "ls-remote", "origin", "refs/heads/star-state").split()[0]


def test_write_merge_recovers_branch_key_but_local_wins_on_conflict(ws):
    """写端并集（--merge-onto）与读端刻意相反的两半：分支独有的键要捞回，同键以**工作树**为准。

    方向写反就等于把本车道刚分类出来的结果用分支上的旧值盖掉；而"不并"更坏——10-08 就是这样
    真丢了 libukai/awesome-deepseek-harness 那条点评（main 上的 known_notes.json 早就没人提交，
    工作树里没有的键只能从分支捞）。这条钉住"捞回"，下一条钉住"本场赢"。
    """
    _make_star_state(ws, {"branch/only": "video", "dup/one": "分支的旧分类"},
                    {"old/9": "只有分支有的点评"})
    assert rss.main(["--merge-onto", _branch_tip(ws)]) == 0
    kc = _read(ws, "known_categories.json")
    notes = _read(ws, "known_notes.json")
    assert kc["branch/only"] == "video", "分支独有的分类没捞回 ⇒ 写端仍是'只取 sha 不取内容'"
    assert notes["old/9"] == "只有分支有的点评", "分支独有的点评没捞回 ⇒ 就是 10-08 那次真丢键"
    assert "old/1" in kc and "dup/one" in kc, "工作树独有的键不许丢"
    assert kc["dup/one"] == "coding", "同名键该由工作树（本场刚算的结果）赢，写端方向反了"
    assert notes["old/0"] == "老点评", "notes 的同名键同样以工作树为准"


def test_write_merge_floor_gate_refuses_to_write(ws, capsys):
    """地板闸门在写端一样生效：并集后仍不足 FLOOR_KEYS ⇒ 一个文件都不写。

    少这一条的话，"main 上表被谁清空了"会以'分支 tip 也被写小'的形态发生，而分支是唯一还存着
    历史键的地方——把它写小就真的没有下一场能捞回来了。
    """
    small = {"a/1": "tools", "a/2": "agent"}
    with open(os.path.join(ws, "known_categories.json"), "w", encoding="utf-8") as f:
        json.dump(small, f)
    with open(os.path.join(ws, "known_notes.json"), "w", encoding="utf-8") as f:
        json.dump({"a/1": "工作树点评"}, f)
    _make_star_state(ws, {"a/3": "video"}, {"a/9": "分支点评"})
    # 写端闸门返回 3 而不是 0：调用方必须据此**放弃提交**（返回 0 的话，bash -e 一路走到
    # git push，推上去的就是没并过的缩小 tree —— 那正是本函数要防的事）。
    assert rss.main(["--merge-onto", _branch_tip(ws)]) == 3, "写端闸门没给出可判别的非零码"
    assert "::error::" in capsys.readouterr().out, "地板没过却没报错 ⇒ 闸门形同不存在"
    assert _read(ws, "known_categories.json") == small, "闸门拒绝后仍写了 categories"
    assert _read(ws, "known_notes.json") == {"a/1": "工作树点评"}, "闸门拒绝后仍写了 notes"


def test_write_merge_refuses_when_parent_object_is_absent(ws):
    """父提交在本地解析不出（浅检出的真实形状）⇒ 返回 3 且一个字节都不写。

    这条就是 10-08 09:45 那两场的形状：ls-remote 给的是一个**本地没有对象**的 sha。
    写端必须把它当"并集没做完"，而不是"分支上没有键"——后者会让调用方照推一份缩小的 tree。
    """
    before = _read(ws, "known_categories.json")
    before_notes = _read(ws, "known_notes.json")
    assert rss.main(["--merge-onto", "0123456789abcdef0123456789abcdef01234567"]) == 3
    assert _read(ws, "known_categories.json") == before
    assert _read(ws, "known_notes.json") == before_notes


def test_write_merge_refuses_instead_of_half_merging(ws):
    """两份文件里只有一份能并 ⇒ 整体拒绝（返回 3）且连"成功的那份"也不写。

    半并是最坏的结果：categories 并了、notes 没并，调用方却只看 categories 就放行，
    推上去的 tree 里 notes 仍是 main 的陈旧副本 ⇒ 分支上那批点评照样被覆盖。
    """
    with open(os.path.join(ws, "known_notes.json"), "w", encoding="utf-8") as f:
        f.write("{坏掉的 json")
    _make_star_state(ws, {"branch/only": "video"}, {"a/1": "分支点评"})
    before = _read(ws, "known_categories.json")
    assert rss.main(["--merge-onto", _branch_tip(ws)]) == 3, "一份坏了却整体放行 ⇒ 会推半并的 tree"
    assert "branch/only" not in _read(ws, "known_categories.json"), \
        "拒绝之后仍写了能并的那份 ⇒ categories/notes 变成两套进度"


# ── star-state 侧的剪枝（2026-10-12）────────────────────────────────────────
# 为什么这一半必须存在：main 侧的构建期剪枝（fetch_and_build.prune_star_state）剪不动
# star-state —— 写端的 --merge-onto 是 dict(incoming) 打底，父提交上的死键必然被捞回来。
# 于是"每次仓库改名都要人手工清一次分支"，而这正是用户裁定要消灭的东西
# （「取消关注这个行为本身就是删除」）。
# 启用条件刻意保守：剪枝是"有意缩小"，与本脚本默认方向（绝不缩小）相反，
# 所以只有 fast_refresh 本场写的 live 名单真在场、可解析、非空时才动，
# 且剪完仍要过已有的 FLOOR_KEYS 闸门（不引入第二个阈值常量，避免两处门槛分叉）。

def _write_live_list(ws, names):
    d = os.path.join(ws, ".deploy-tmp")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "star_live_names.json"), "w", encoding="utf-8") as f:
        json.dump(names, f)


def _live_of(ws, extra=()):
    """造一份"当前收藏"名单：工作树里已有的键 + extra（用来模拟分支独有的活键）。"""
    return sorted(set(_read(ws, "known_categories.json")) | set(extra))


def test_write_prune_drops_branch_only_dead_key(ws, capsys):
    """名单在场时，分支独有且不在收藏里的键必须被剪掉——这是本半的全部意义。"""
    _make_star_state(ws, {"branch/alive": "video", "branch/dead": "coding"},
                     {"branch/dead": "该剪的死键点评"})
    _write_live_list(ws, _live_of(ws, extra=["branch/alive"]))
    assert rss.main(["--merge-onto", _branch_tip(ws)]) == 0
    kc = _read(ws, "known_categories.json")
    assert "branch/alive" in kc, "名单里的活键被剪了 ⇒ 与 10-08 那条'分支独有键要捞回'直接冲突"
    assert "branch/dead" not in kc, "死键没剪 ⇒ 分支仍是只增不减的坟场，每小时都要重剪一遍"
    assert "branch/dead" not in _read(ws, "known_notes.json"), \
        "categories 剪了 notes 没剪 ⇒ 两表口径分叉"
    assert "剪枝" in capsys.readouterr().out, "剪了不出声 ⇒ 日志里分不清『本来就没死键』和『没启用』"


def test_write_without_live_list_is_byte_identical(ws):
    """名单缺失 ⇒ 一个键都不剪，行为与加这个功能之前逐字节同形。

    这条是"降级即兼容"的凭据：star-fast.yml 那 6 条实跑 shell 的行为判据都没写名单文件，
    所以它们必须继续绿；否则就是我用新判据去推翻旧判据保护的真东西。
    """
    _make_star_state(ws, {"branch/dead": "coding"}, {"branch/dead": "点评"})
    assert rss.main(["--merge-onto", _branch_tip(ws)]) == 0
    kc = _read(ws, "known_categories.json")
    assert "branch/dead" in kc, "没有名单却动了剪枝 ⇒ 降级路径没生效，会误删还在收藏的键"


def test_write_prune_refuses_when_list_is_empty(ws, capsys):
    """空名单 = 拉取异常，不是"用户取消了所有收藏"：一律不剪（与 main 侧同源的那条免疫）。"""
    _make_star_state(ws, {"branch/dead": "coding"})
    _write_live_list(ws, [])
    assert rss.main(["--merge-onto", _branch_tip(ws)]) == 0
    assert "branch/dead" in _read(ws, "known_categories.json"), "空名单把整张表剪空了"


def test_write_prune_refuses_when_result_under_floors(ws, capsys):
    """名单是垃圾（剪完不足 FLOOR_KEYS）⇒ 拒绝剪并出声，但**并集结果照常写**。

    刻意不返回 3：并集本身是完整的，拒绝的只是缩小这一步。返回 3 会让调用方放弃整场提交，
    把"名单坏了"升级成"分类结果没落库"，那是更坏的结果。
    """
    _make_star_state(ws, {"branch/only": "video"})
    _write_live_list(ws, ["a/1", "a/2"])
    assert rss.main(["--merge-onto", _branch_tip(ws)]) == 0
    out = capsys.readouterr().out
    assert "branch/only" in _read(ws, "known_categories.json"), "门槛没拦住，把表剪到 FLOOR 以下了"
    assert "拒绝剪枝" in out, "拦住了却不出声 ⇒ 与『没有死键可剪』混在一起，线上查不出来"


def test_write_prune_survives_corrupt_list(ws):
    """名单文件存在但不是合法 JSON ⇒ 按缺失处理（不剪、不崩、返回 0）。"""
    _make_star_state(ws, {"branch/dead": "coding"})
    d = os.path.join(ws, ".deploy-tmp")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "star_live_names.json"), "w", encoding="utf-8") as f:
        f.write("[坏掉")
    assert rss.main(["--merge-onto", _branch_tip(ws)]) == 0
    assert "branch/dead" in _read(ws, "known_categories.json")

