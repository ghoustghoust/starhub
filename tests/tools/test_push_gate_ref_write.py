# -*- coding: utf-8 -*-
"""推送闸与推送后复查都必须判「这场还会不会写 main」，而不是「这场还没跑完」。

为什么这是缺陷而不是保守（2026-10-02 实测）：`update.yml` 的 Vercel 步连场挂 40+ 分钟，
于是**任何时刻都有一只 status != completed 的 run** ⇒ 旧判据下 `gate()` 恒拒、
`--wait-window` 每轮 30 分钟必然白等（用户读数：4 小时没推上任何东西）。
而全场唯一写 main 的 ref 的动作是那只 run 的 `Commit & push if changed` 步，
它在开场几分钟后就结束了 —— 现取 run 1589 步级状态：
step22 `Commit & push if changed` = completed/success，step30 `Deploy to Vercel` = in_progress。

为什么「Commit 步之后推」确实安全（读实现不是猜）：`update.yml:346` 用的是**普通 `git push`（无 --force）**，
被拒后走 `fetch + reset --hard origin/main + 重建 + 重新 add` 的重试路径 ⇒
即便抢在它 fetch 之前推过去，它也会基于我的 commit 重跑，谁也不吞谁（09-19 那次吞代码正是因为旧写法
`reset --soft` 保留了旧 index，已修）。真正的危险只剩该步**内部**那几秒。

判不了的情形一律算「还会写」（宁可等，也不推出去被回滚）：jobs 取不到、job 还没开始（steps 空）、
步骤名对不上（别的 workflow，或有人改了步名 ⇒ 这条判据会变恒拒，正是希望它出声的地方）。
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.environ.get("STARHUB_TOOLS_DIR") or os.path.join(ROOT, "tools"))
import data_api_push as D  # noqa: E402

STEP = "Commit & push if changed"


def _run(status="in_progress", created="2026-10-02T23:00:31Z", rid=1589):
    return {"id": rid, "status": status, "created_at": created}


def _jobs(steps):
    return [{"id": 1, "steps": steps}]


def _step(name, status="completed", conclusion="success"):
    return {"name": name, "status": status, "conclusion": conclusion}


def _hang_jobs():
    return _jobs([_step("Checkout"), _step(STEP), _step("Deploy to Vercel", "in_progress", None)])


def test_ref_write_step_is_named_in_update_yml():
    """钉住模块常量与真实 workflow 的对应关系：改名 ⇒ 恒拒，别让它悄悄变成空转的绿。"""
    body = open(os.path.join(ROOT, ".github", "workflows", "update.yml"), encoding="utf-8").read()
    assert D.REF_WRITE_STEP == STEP, (
        "模块里的 ref 写入步名是 %r，测试钉的是 %r ⇒ 两边已经分叉，判据在拿一个不存在的步骤名做比较"
        % (getattr(D, "REF_WRITE_STEP", None), STEP))
    assert ("- name: " + D.REF_WRITE_STEP) in body, (
        "update.yml 里已经没有名为「%s」的步骤 ⇒ will_write_main 会对每一场都判「还会写」，"
        "推送闸变成恒拒；要么同步改常量，要么确认新的 ref 写入步名" % D.REF_WRITE_STEP)


def test_hanging_after_the_commit_step_is_not_a_writer():
    """本缺陷的主案：Commit 步已完成、Vercel 步还在挂 ⇒ 它不会再写 main。"""
    assert D.will_write_main(_hang_jobs()) is False, (
        "Commit 步都结束了还判它会回滚 ⇒ 推送闸会把每一场都挡下（就是 4 小时白等那个形状）")


def test_commit_step_in_flight_is_a_writer():
    assert D.will_write_main(_jobs([_step("Checkout"), _step(STEP, "in_progress", None)])) is True, (
        "它正在 fetch→commit→push 中间，此刻推过去就是 09-19 那种被回滚的形状")


def test_commit_step_not_started_yet_is_a_writer():
    jobs = _jobs([_step("Checkout", "in_progress", None), _step(STEP, "pending", None)])
    assert D.will_write_main(jobs) is True


def test_job_without_steps_is_a_writer():
    """刚建 job（含 queued 那一场）拿不到步序列：判不了 ⇒ 等，别放行。"""
    assert D.will_write_main(_jobs([])) is True, (
        "看不到步骤序列就当它不会写 ref ⇒ 开场那几分钟的推送会被回滚")


def test_unreadable_jobs_are_writers():
    assert D.will_write_main(None) is True, "取不到 jobs 就当它会写：保守方向不能反"


def test_run_without_the_step_is_a_writer():
    """别的 workflow（repo-trim 等）或步名被改：判不了就等，绝不放行。"""
    jobs = _jobs([_step("Checkout"), _step("Rewrite history"), _step("Commit trimmed repo")])
    assert D.will_write_main(jobs) is True, "认不出的步骤序列被放行 = 拿「不知道」当「安全」"


def test_failed_and_skipped_commit_steps_are_not_writers():
    """size_tripwire 判红 ⇒ 该步 completed/failure，它不会再推；日志闸门关着 ⇒ skipped 同理。"""
    for concl in ("failure", "skipped"):
        jobs = _jobs([_step(STEP, "completed", concl),
                      _step("Deploy to Vercel", "in_progress", None)])
        assert D.will_write_main(jobs) is False, "conclusion=%s 的 Commit 步不会再碰 ref" % concl


def test_gate_passes_when_the_only_busy_run_is_past_its_commit_step(monkeypatch):
    """闸的接线：把网络打桩，确认它用的是步级判据而不是 status。"""
    monkeypatch.setattr(D, "runs", lambda per_page=10: [_run(), _run("completed", rid=1588)])
    monkeypatch.setattr(D, "jobs_of", lambda rid: _hang_jobs())
    ok, why = D.gate()
    assert ok, "挂的是 Vercel 步、Commit 步已结束，闸仍拒 ⇒ 就是那个 4 小时白等的 bug：%s" % why
    assert STEP in why, "放行必须说清凭什么放行（哪个步骤结束了），否则下次没人敢信这条绿"


def test_gate_still_blocks_when_a_busy_run_has_not_committed(monkeypatch):
    monkeypatch.setattr(D, "runs", lambda per_page=10: [_run()])
    monkeypatch.setattr(D, "jobs_of", lambda rid: _jobs([_step(STEP, "in_progress", None)]))
    ok, why = D.gate()
    assert not ok, "Commit 步还在飞就放行 ⇒ 推上去会被它的旧检出回滚"


def test_gate_blocks_a_queued_run(monkeypatch):
    """排队的场次还没开始、没有步序列 ⇒ 必须拒（它之后一定会提交）。"""
    monkeypatch.setattr(D, "runs", lambda per_page=10: [_run("queued", rid=1590)])
    monkeypatch.setattr(D, "jobs_of", lambda rid: _jobs([]))
    ok, _ = D.gate()
    assert not ok, "queued 的 run 被放行 = 它一开跑就把我们刚推的内容按旧检出回滚"


def test_risky_runs_only_counts_those_that_can_still_commit(monkeypatch):
    """推送后的等待用的同一判据：挂着的 Vercel 步不该再让工具空等 20 分钟。"""
    hang = _run(rid=1589)
    early = _run(rid=1590, created="2026-10-03T00:00:31Z")
    monkeypatch.setattr(D, "runs", lambda per_page=10: [hang, early])

    def jobs_of(rid):
        return _jobs([_step(STEP)]) if rid == 1589 else _jobs([_step(STEP, "pending", None)])
    monkeypatch.setattr(D, "jobs_of", jobs_of)
    assert [r["id"] for r in D.risky_runs("2026-10-02T23:50:00Z")] == [], (
        "两只 run 都不可能再提交（一只 Commit 步已结束、一只创建于本次推送之后）⇒ 不该算风险")

    late = _run(rid=1587, created="2026-10-02T22:00:29Z")
    monkeypatch.setattr(D, "runs", lambda per_page=10: [late])
    monkeypatch.setattr(D, "jobs_of", lambda rid: _jobs([_step(STEP, "in_progress", None)]))
    assert [r["id"] for r in D.risky_runs("2026-10-02T23:50:00Z")] == [1587], (
        "早于推送、Commit 步还在飞的那场必须算风险 ⇒ 漏了它，推后复查会在它覆盖之前判「内容一致」")


def test_the_only_remote_writers_are_inside_the_commit_step():
    """守门的整个前提 = "update.yml 里唯一写 main 的动作都在那一步里"。

    这条不钉着，将来谁在别的步骤加一次 `git push`（或让第 4 步顺手推回去），
    will_write_main() 会照样在 Commit 步结束后放行 —— 判据看着绿，前提已经没了。
    解析器必须**先证明它看得见**：一处 push 都没抓到就是探针失效，不许当成"没有写入点"。
    """
    wf = os.environ.get("STARHUB_UPDATE_YML") or os.path.join(ROOT, ".github", "workflows", "update.yml")
    text = open(wf, encoding="utf-8").read()
    lines = text.splitlines()
    starts = [i for i, l in enumerate(lines) if re.match(r"^      - name: ", l)]
    step_of = {}
    for n, i in enumerate(starts):
        end = starts[n + 1] if n + 1 < len(starts) else len(lines)
        step_of[i] = (lines[i].split("name:", 1)[1].strip(), lines[i:end])
    writers = [name for name, body in step_of.values() if any(re.search(r"^\s*git push\b", l) for l in body)]
    assert writers, (
        "整个 workflow 里没抓到任何 `git push` ⇒ 是这个解析器瞎了（正文写法变了？），"
        "不能据此说『只有 Commit 步会写远端』")
    # 除主提交步外还有一个写者：`Ensure Vercel sees a bot-authored commit`（Vercel 身份标记，
    # 10-03 加的）。它排在主提交步**之后**，所以守门按主提交步收口是对的：那一步推的是
    # `--allow-empty` 的空提交、无 --force、且 `continue-on-error: true` ⇒ 若我在这之后推过，
    # 它的 push 只会 non-fast-forward 失败并被容忍，**顶不掉我的提交**（最坏是那场 Vercel 被 Blocked）。
    # 这条判据从 10-03 起就红着（电池不进 CI ⇒ 没人发现），今天才按作者留的指令把集合补全。
    assert set(writers) == {D.REF_WRITE_STEP, "Ensure Vercel sees a bot-authored commit"}, \
        "写 main 的步骤集合变了：%s ⇒ 先确认新写者能不能顶掉别人的推送，再更新这里的期望值" % sorted(writers)
    # 上面那段"它顶不掉别人的提交"不许只当注释：有人给那一步加个 --force，我的论证立刻失效，
    # 而判据照绿 ⇒ 守卫看着更绿、实际更松。所以把前提的两个支点拆成断言。
    marker = dict(step_of.values())["Ensure Vercel sees a bot-authored commit"]
    mtext = "\n".join(marker)
    assert not re.search(r"--force(?!-with-lease)", mtext), \
        "Vercel 标记步出现裸 --force ⇒ 它能覆盖别人的提交，守门只盯主提交步就不再成立"
    assert "--allow-empty" in mtext, \
        "Vercel 标记步不再只推空提交 ⇒ 『只会被 non-fast-forward 拒、顶不掉别人』要重新论证"
