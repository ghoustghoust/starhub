# -*- coding: utf-8 -*-
"""批 6 判据：每场新增字节的健康监测要"出声但不咬人"。

曲线压平之后，真正的风险从"现在多大"变成"**哪天有人悄悄把它改回每场重写**"。
本轮实测过的阶梯是 19.83 → 2.85 → 0.05 MiB/场，回退一次就是十几 MiB/场、按月又是几 GiB，
而 git 历史不可回收 ⇒ 只有一份会被读、会被比的读数是不够的，得让它在 CI 里每天自己喊。

钉四件事，缺一件这条监测就是摆设：
  ① 测量口径是**相邻两个提交的递归 tree 差里"新 (path,sha)"的字节和**，不是 tip 增减
     （tip 看不出膨胀：被覆盖的旧 blob 仍永久留在历史里）；
  ② 判据必须能被非空输入证明它在工作（合成两棵树，一条有新增、一条完全没变，两个方向都断言）；
  ③ 阈值不是 0、也不是随手一个大数：> 6 MiB 才出声。理由是每天那**一次**日志提交合法地带来
     ~2.6 MiB（`build_logs/<今天>.jsonl` + `summary_*.json`），阈值低于它就会天天误报，
     而天天误报的告警等于没有告警；真正的回退形态是 17–20 MiB/场，6 MiB 拦得住也放过噪声；
  ④ 这一步是 advisory：**必须 `continue-on-error: true`，且工具本身绝不允许 push / 改历史**
     （本仓已经吃过两次"自己把自己的部署冻掉"）。
"""
import importlib.util
import os
import re
import subprocess
from datetime import datetime

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TOOL = os.environ.get("STARHUB_GROWTH_TOOL") or os.path.join(ROOT, "tools", "history_growth.py")
WF = os.environ.get("STARHUB_UPDATE_YML") or os.path.join(ROOT, ".github", "workflows", "update.yml")
STEP_NAME = "Monitor per-build git growth (advisory)"


def _load():
    spec = importlib.util.spec_from_file_location("history_growth", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _steps():
    with open(WF, encoding="utf-8") as fh:
        doc = yaml.safe_load(fh)
    return doc["jobs"]["update"]["steps"]


def test_tool_exists_and_pure_functions_are_loadable():
    assert os.path.isfile(TOOL), "缺少 %s：曲线回退没人盯着" % TOOL
    m = _load()
    for fn in ("parse_ls_tree", "new_blob_bytes", "verdict"):
        assert hasattr(m, fn), "history_growth 缺 %s —— 口径没法单独验证" % fn


def test_parse_ls_tree_reads_real_git_output_shape():
    """`git ls-tree -rl` 的列形是 `<mode> blob <sha> <size>\\t<path>`（size 可能是 `-`）。

    解析必须按字节数与路径取，不能被路径里的空格或 `-` 骗掉；这里合成 4 行、断言解析到 4 条，
    禁"空集合上判绿"。
    """
    m = _load()
    txt = "\n".join([
        "100644 blob aaa1111 1234\thot_snapshot.json",
        "100644 blob bbb2222 99\tdocs/a b/c.md",
        "100644 blob ccc3333 -\tempty.json",
        "100755 blob ddd4444 7\ttools/x.sh",
        "",
    ])
    got = m.parse_ls_tree(txt)
    assert len(got) == 4, "解析到 %d 条（应为 4）—— 列形变了，后面的字节统计全不可信" % len(got)
    assert got["hot_snapshot.json"] == ("aaa1111", 1234)
    assert got["docs/a b/c.md"][1] == 99, "路径含空格时取错了列：%r" % (got["docs/a b/c.md"],)
    assert got["empty.json"][1] == 0, "`-`（空 blob）应算 0 字节"
    # 反向：垃圾输入必须解析出 0 条，而不是把噪声当文件
    assert m.parse_ls_tree("not a tree line\n") == {}


def test_new_blob_bytes_counts_added_and_changed_only():
    m = _load()
    prev = {"a.js": ("1", 100), "b.js": ("2", 200), "gone.js": ("3", 50)}
    cur = {"a.js": ("1", 100), "b.js": ("9", 300), "new.js": ("4", 70)}
    got = m.new_blob_bytes(prev, cur)
    assert sorted(p for p, _ in got) == ["b.js", "new.js"], "应只报新增与内容变化：%r" % (got,)
    assert dict(got)["b.js"] == 300, "变化项应按**新**副本计（旧 blob 仍永久留在历史里）"
    assert sum(s for _, s in m.new_blob_bytes(prev, prev)) == 0, "同一棵树必须算出 0 新增"


def test_verdict_uses_a_real_threshold_in_both_directions():
    m = _load()
    assert m.threshold() > 0, "阈值不能是 0：那样每天日志那一次合法提交也会被咬"
    assert m.verdict(0) == "ok"
    assert m.verdict(m.threshold() - 1) == "ok", "阈值边界下沿不该误报"
    assert m.verdict(m.threshold()) != "ok", "到阈值必须出声，否则判据是摆设"
    assert m.verdict(19_990_000) != "ok", "回退到本轮改造前的 19.83 MiB/场 必须被抓"


def test_growth_step_is_advisory_and_after_commit():
    steps = _steps()
    hits = [s for s in steps if (s.get("name") or "") == STEP_NAME]
    assert len(hits) == 1, "步骤名 %r 命中 %d 个（应为 1）" % (STEP_NAME, len(hits))
    st = hits[0]
    assert st.get("continue-on-error") is True, (
        "缺 continue-on-error：advisory 步一红就连坐两次部署，这个仓已经付过两次代价")
    body = st.get("run") or ""
    assert body.startswith("\n") or body, "步骤没有 run 正文"
    assert "history_growth.py" in body, "这一步没调用测量工具，只是个空壳"
    assert "history_growth.py" in body
    idx = [s.get("name") for s in steps]
    assert idx.index("Commit & push if changed") < idx.index(STEP_NAME), (
        "测量必须在 Commit 之后：否则量到的是上一场，本场的膨胀恰好看不见")


def test_growth_event_does_not_disturb_the_daily_summary(tmp_path, monkeypatch):
    """我往 `build_logs/<今天>.jsonl` 里塞了新事件类型，就必须证明它不动既有读数。

    `build_logger.summary()` 是按 `e["type"]` 分桶计数的，理论上"growth"会被忽略 ——
    但"理论上"不是证据：真要写错（比如把 type 写成 build），摘要里的 `builds`
    就会把每场的监测当成一次构建，而那个数字是用户每天看构建日志时唯一在意的东西。
    这里喂真文件、调真 `summary()`，同时断言两件事：既有计数不变、growth 事件可查。
    """
    import json as _json

    import build_logger
    monkeypatch.chdir(tmp_path)
    os.makedirs("build_logs", exist_ok=True)
    today = datetime.now(build_logger.BJT).strftime("%Y-%m-%d")
    day = os.path.join("build_logs", "%s.jsonl" % today)
    with open(day, "a", encoding="utf-8") as fh:
        for i, ev in enumerate((("build", 1200), ("trigger", None), ("build", 1300), ("deploy", None))):
            rec = {"ts": "2026-10-02T0%d:00:00+08:00" % i, "type": ev[0]}
            if ev[0] == "build":
                rec["items_snapshot"] = ev[1]
            fh.write(_json.dumps(rec, ensure_ascii=False) + "\n")
    before = build_logger.summary(today)
    m = _load()
    for i in range(3):
        m.log_line(1000 + i, [("a.js", 1000 + i)], day, commit="c%d" % i)
    after = build_logger.summary(today)
    for key in ("builds", "triggers", "deploys", "total_items_latest", "items_delta"):
        assert after[key] == before[key], "混入 growth 事件后 %s 从 %r 变成 %r" % (
            key, before[key], after[key])
    assert after["builds"] == 2, "样本本身就该有 2 条 build，实测 %r —— 这条判据在空样本上跑" % after["builds"]
    lines = [_json.loads(l) for l in open(day, encoding="utf-8").read().splitlines() if l.strip()]
    growth = [r for r in lines if r.get("type") == "growth"]
    assert len(growth) == 3, "growth 事件应各占一行，实测 %d" % len(growth)
    assert [r["new_bytes"] for r in growth] == [1000, 1001, 1002], (
        "监测读数必须可按写入顺序回查：%s" % growth)


def test_report_warns_only_above_threshold():
    """出声的那段文本必须是真会被 Actions 读到的形状，而不是测试里拼的字符串。"""
    m = _load()
    ok = m.report(1024, [("a.js", 1024)])
    assert "::warning" not in ok, "正常场次不该出声：%s" % ok
    assert "[growth]" in ok, "读数必须打出来，否则这块面板是黑的"
    warn = m.report(m.threshold(), [("hot_history.json", m.threshold())])
    assert "::warning" in warn and "title=" in warn, (
        "到阈值必须以 ::warning 出声（只在 stdout 打印会被漏看）：%s" % warn)


def test_tool_never_writes_to_the_repository():
    """监测工具只能读。自动"瘦身"就是第二次 repo-trim，而它是在每场构建里跑的。"""
    src = open(TOOL, encoding="utf-8").read()
    for forbidden in ("git push", "filter-repo", "rebase", "reset --hard", "commit -m"):
        assert forbidden not in src, "history_growth.py 里出现 %r：健康监测绝不能改仓库状态" % forbidden
    # 反向守卫：这条不是空断言 —— 真的执行一次只读命令，确认它能跑
    assert re.search(r"git ls-tree", src), "工具必须用 `git ls-tree -rl` 取字节，而不是猜"
    r = subprocess.run(["python", TOOL, "--self-test"], capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0, "--self-test 必须可跑（合成树，不碰仓库）：%s%s" % (r.stdout, r.stderr)


def test_log_line_is_appended_as_structured_event(tmp_path):
    """批 6 的后半：读数必须落到 `build_logs/<今天>.jsonl`，不然"持续状态"只活在一次性日志里。

    现在只有 Actions 日志里有这一行，过 90 天就查不到了；写进构建日志（每天一次进 git）后，
    `growth` 事件就和 trigger/deploy 事件同表，能直接回答"哪一场开始又长回去了"。
    这里喂真实函数，验的是落盘形状，不是 grep 源码。
    """
    import json as _json
    m = _load()
    path = tmp_path / "g.jsonl"
    m.log_line(1234, [("a.js", 1234)], str(path), commit="deadbeefdead")
    m.log_line(5, [("b.js", 5)], str(path), commit="beadfeedbead")
    lines = [l for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines) == 2, "append 语义没生效，实测 %d 行" % len(lines)
    rec = [_json.loads(l) for l in lines]
    assert all(r["type"] == "growth" for r in rec), "事件类型必须是 growth：%s" % rec
    assert rec[0]["new_bytes"] == 1234 and rec[0]["new_blobs"] == 1
    assert rec[1]["new_bytes"] == 5, "第二次必须写自己的数，不能复用上一条"
    assert rec[0]["commit"] == "deadbeefdead"


def test_log_line_survives_a_bad_path():
    """写日志是附带行为，绝不能把监测本身弄红（这一步是 advisory，但红在工具里也没意义）。"""
    m = _load()
    m.log_line(1, [("a", 1)], os.path.join(os.path.sep.join(["__no_such_dir__", "x.jsonl"])))


def test_growth_step_logs_into_today_build_log():
    body = next(s for s in _steps() if (s.get("name") or "").startswith(STEP_NAME)).get("run") or ""
    assert "--log" in body, "监测步没把读数写进日志：那这些数只存在于 Actions 的一次性输出里"
    assert "build_logs/" in body, "--log 的目标必须在 build_logs/ 下（它每天一次进 git，也在缓存里）"


def test_attribution_distinguishes_a_no_commit_build(tmp_path):
    """读数必须说清"这条增长算不算本场"（2026-10-02 实测到的真缺陷）。

    13:00 那场日志里同时有 `No changes, skip commit.` 和 `[growth] new blobs=2 0.037 MiB` ——
    后者其实是**我 12:52 推的批 9** 被算成了"这场的增长"。监测的全部意义就是回答
    "有没有人把每场重写的文件加回提交清单"，把别人的提交记成本场的，等于在这条曲线上造假噪声；
    更糟的是反向情形：某场真提交了却没算进来，曲线会假装平静。
    判据不打网络：`attribution()` 是纯函数，喂两个 sha 就够。
    """
    m = _load()
    assert m.attribution("abc123", "abc123") is False, "HEAD 就是检出的那个 sha ⇒ 本场没提交"
    assert m.attribution("abc123", "def456") is True, "HEAD 变了才是本场（或本场期间）提交的"
    assert m.attribution("", "def456") is None, "不知道基线（本地跑）时不许猜"
    assert m.attribution("abc123", "") is None, "HEAD 取不到时同样不许猜"


def test_report_says_so_when_the_build_committed_nothing(tmp_path):
    m = _load()
    txt = m.report(39000, [("a.js", 39000)], attributed=False)
    assert "本场未提交" in txt, "没提交却要报 0.037 MiB/场 ⇒ 播报必须自带归属，否则读的人无从折扣：%s" % txt
    assert "new blobs=1" in txt, "归属说明不许吃掉数字本身"
    assert "本场未提交" not in m.report(39000, [("a.js", 39000)], attributed=True)
    assert "本场未提交" not in m.report(39000, [("a.js", 39000)]), \
        "不知道归属时不该硬说「本场未提交」（那是假话），只报数就行"


def test_log_line_records_the_attribution(tmp_path):
    m = _load()
    import json as _json
    p = tmp_path / "g.jsonl"
    m.log_line(39000, [("a.js", 39000)], str(p), commit="abc123",
               head="abc123", head_subject="perf(pages): 收窄制品", attributed=False)
    rec = _json.loads(p.read_text(encoding="utf-8").splitlines()[-1])
    assert rec["committed_by_run"] is False, rec
    assert rec["head"] == "abc123" and rec["head_subject"] == "perf(pages): 收窄制品", rec
    # True 这一侧必须单独验：H16（把赋值改成写死 False）只破坏这一侧，
    # 只测 False + 缺席两个形状时它会全绿活下来 —— 电池逮到的正是判据自己的缺口。
    m.log_line(7, [("c.js", 7)], str(p), commit="c", head="zzz", attributed=True)
    rec3 = _json.loads(p.read_text(encoding="utf-8").splitlines()[-1])
    assert rec3["committed_by_run"] is True, rec3
    # 未知归属要写成 null，不能默认成 True/False（默认值会把"没测到"伪装成测到了）
    m.log_line(5, [("b.js", 5)], str(p), commit="x", head="y")
    rec2 = _json.loads(p.read_text(encoding="utf-8").splitlines()[-1])
    assert "committed_by_run" not in rec2 or rec2["committed_by_run"] is None, rec2
