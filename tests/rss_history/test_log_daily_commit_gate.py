# -*- coding: utf-8 -*-
"""批 4 判据：构建日志改为"每天只提交一次"，并退役每小时摘要工作流。

动机（实测）：`build_logs/<今天>.jsonl` 每场给 git 历史永久新增 1.7-2.1 MiB（一天 22 行里
最大一行 112,637 B，其中 `per_source` 占 111,924 B）。你要的是"在 GitHub 上还能看构建日志"，
所以**不删日志、只降频**：每天提交一次，其余场次靠 `starhub-state` 缓存累积。

必须同时处理的耦合（这是本批的重点，漏一条就是静默坏功能）：
  · `build-log-summary.yml` 每小时从 git 读 jsonl 再生成 `summary_*.json` 并 `git add` 提交。
    jsonl 一旦变成每日一提交，它读到的就是**旧** jsonl ⇒ 会把主构建生成的正确摘要覆盖成
    `builds=0` 并提交。所以它必须与"降频"同批退役，摘要改由主构建在 Commit 之前生成。
  · `git add build_logs/` 这类通配/目录 add **不会 stage 删除** ⇒ `cleanup(5)` 删掉的旧摘要
    会永远留在树里（tip 每天 +180 KB 的新无界点）。所以要配 `git ls-files -d` + `git rm --cached`。
  · 判定"今天是否已提交"不能靠 `git log -- <path>`：主构建的 checkout 是 `fetch-depth: 0`
    (`update.yml:45`)，但那是偶然条件 —— 谁以后改成 1，`git log` 返回空会被读成"到期了"，
    于是静默退回每场提交。改成读一个自己提交的小标记文件，与 depth 无关。

闸门逻辑做成可执行脚本 `tools/daily_commit_gate.sh`：判据**真的运行它**（喂合成 marker），
而不是在 yml 文本上找字符串 —— 后者只能证明文字在，不能证明行为对。
"""
import os
import subprocess

import pytest

yaml = pytest.importorskip("yaml")

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WF = os.environ.get("STARHUB_UPDATE_YML") or os.path.join(ROOT, ".github", "workflows", "update.yml")
GATE = os.environ.get("STARHUB_COMMIT_GATE") or os.path.join(ROOT, "tools", "daily_commit_gate.sh")
SUMMARY_WF = os.path.join(ROOT, ".github", "workflows", "build-log-summary.yml")


def _run_gate(marker_content, today="2026-10-01", create=True):
    """跑闸门：返回 (stdout 首词, 退出码)。marker_content=None 表示标记文件不存在。"""
    import tempfile
    d = tempfile.mkdtemp()
    marker = os.path.join(d, ".committed_date")
    if create:
        with open(marker, "w", encoding="utf-8") as fh:
            fh.write(marker_content)
    r = subprocess.run(["bash", GATE, marker, today], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=60)
    return (r.stdout or "").strip(), r.returncode


def _step_names():
    with open(WF, encoding="utf-8") as fh:
        doc = yaml.safe_load(fh)
    return [(s.get("name") or "") for s in doc["jobs"]["update"]["steps"]]


def _run_block(name):
    """取某个 run 步骤的脚本正文（用来判断 add 是否真在闸门之内，而不是只看先后）。"""
    with open(WF, encoding="utf-8") as fh:
        doc = yaml.safe_load(fh)
    for s in doc["jobs"]["update"]["steps"]:
        if (s.get("name") or "").startswith(name):
            body = s.get("run")
            assert body, "步骤 %r 没有 run 正文" % name
            return body
    raise AssertionError("找不到步骤 %r" % name)


# ── 闸门脚本自身的真行为 ──

def test_gate_opens_when_marker_absent():
    assert os.path.isfile(GATE), "闸门脚本 tools/daily_commit_gate.sh 还不存在"
    out, rc = _run_gate(None, create=False)
    assert rc == 0 and out == "open", "标记文件不存在时应开闸（今天还没提交过），实际 %r rc=%d" % (out, rc)


def test_gate_closes_when_marker_is_today():
    out, rc = _run_gate("2026-10-01\n", "2026-10-01")
    assert rc == 0 and out == "closed", "今天已提交过必须关闸（否则每天 10 场照旧提交，等于没改），实际 %r" % out


def test_gate_opens_for_yesterday():
    out, rc = _run_gate("2026-09-30\n", "2026-10-01")
    assert rc == 0 and out == "open", "跨天必须重新开闸，实际 %r" % out


def test_gate_survives_garbage_marker_without_failing():
    """标记文件坏了/是空文件时**必须**放行而不是非零退出。

    闸门在 Commit 步里；它红一次就是把整场构建和两个部署一起冻住（本仓 10-01 刚付过一次学费）。
    宁可每天多提交一次，也不能因为它自己出问题而停部署。
    """
    for junk in ("", "  ", "not-a-date", "2026-13-45"):
        out, rc = _run_gate(junk, "2026-10-01")
        assert rc == 0, "marker=%r 时闸门退出码 %d —— 会把 Commit 步连带弄红" % (junk, rc)
        assert out in ("open", "closed"), "marker=%r 输出应为 open/closed，实际 %r" % (junk, out)


# ── 工作流接线 ──

def test_build_logs_add_is_inside_the_gate():
    body = _run_block("Commit & push if changed")
    n_add = body.count("git add build_logs/")
    n_gate = body.count("daily_commit_gate.sh")
    assert n_add >= 1, "看不到 build_logs 的 add：那日志还怎么入库？"
    assert n_gate == n_add, (
        "闸门 %d 处、add %d 处 —— 数量不等就是「只包了一处」。主路径与 push 被拒的重试分支各有一份"
        "清单，只改一处时另一处照旧每场提交，而且现场看不出来（第一版判据就是这么假绿的）" % (n_gate, n_add))
    # 第一处 add 之前必须已经出现过闸门调用（G1 那类"add 在闸门外"的形状）
    head = body.split("daily_commit_gate.sh", 1)
    assert len(head) == 2 and "git add build_logs/" not in head[0], (
        "第一处 git add build_logs/ 出现在闸门之前 ⇒ 无条件提交，闸门等于装饰")
    assert "= open ]" in body, "看不出 add 被包在闸门的条件里"


def test_summary_is_generated_before_commit():
    names = _step_names()
    i_sum = next((i for i, n in enumerate(names) if n.startswith("Generate daily summary")), None)
    i_commit = next(i for i, n in enumerate(names) if n.startswith("Commit & push if changed"))
    i_build = next(i for i, n in enumerate(names) if n.startswith("Fetch stars & build"))
    assert i_sum is not None, "没有生成摘要的步骤"
    assert i_build < i_sum < i_commit, (
        "摘要必须在构建之后、Commit 之前 —— 今天它在 Commit 与部署之后，"
        "所以主构建从来没把自己生成的摘要提交上去，摘要一直靠每小时那个工作流")


def test_deleted_log_files_are_untracked_too():
    body = _run_block("Commit & push if changed")
    n_add = body.count("git add build_logs/")
    assert body.count("git ls-files -d") == n_add and body.count("git rm --cached") == n_add, (
        "目录式 add 不 stage 删除：每处 add 都要配一次 ls-files -d + git rm --cached，"
        "否则 cleanup(5) 删掉的旧 jsonl/summary 永远留在树里（每天 +180 KB 的新无界点）。"
        "实测 add=%d、ls-files -d=%d、rm --cached=%d" % (
            n_add, body.count("git ls-files -d"), body.count("git rm --cached")))


def test_hourly_summary_workflow_is_not_tracked():
    """每小时摘要工作流必须退役（不再被 git 跟踪）。

    为什么钉"不被跟踪"而不是"磁盘上没有"：CI 跑的是干净检出，两者在 CI 里等价；而本地
    即便远端已删，工作树里那份要等索引对齐才会消失（与 `tests/site_nav_drift/` 那条
    "产物页面不许被跟踪"同形）。留在磁盘上的未跟踪副本不会被任何 workflow 触发，
    但**一旦被重新 add 回来**，它就会从旧 jsonl 生成摘要并 force-push，把正确摘要覆盖成
    `builds=0` —— 那正是本批评据要挡的动作。
    """
    r = subprocess.run(["git", "ls-files", "--error-unmatch",
                        os.path.relpath(SUMMARY_WF, ROOT).replace(os.sep, "/")],
                       capture_output=True, text=True, cwd=ROOT)
    assert r.returncode != 0, (
        "build-log-summary.yml 仍被 git 跟踪：它会从 git 里的旧 jsonl 生成摘要并覆盖主构建的"
        "正确摘要（builds=0）。退役方式：data_api_push.py --delete .github/workflows/build-log-summary.yml")


def test_gate_marker_itself_is_committed():
    body = _run_block("Commit & push if changed")
    n_add = body.count("git add build_logs/")
    n_write = body.count("> build_logs/.committed_date")
    assert n_write == n_add, (
        "标记文件不入库就等于每次都是冷状态：闸门永远开，日志照旧每场提交。"
        "实测 add=%d、marker 写入=%d" % (n_add, n_write))
    assert "build_logs/.committed_date" in body and "daily_commit_gate.sh" in body
