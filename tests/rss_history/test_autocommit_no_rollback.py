# -*- coding: utf-8 -*-
"""auto-commit 不得回滚别人源码（09-21 复盘定案，进 blocking 门禁 A2）。

事实链（都在 .github/workflows/update.yml 里可核）：
- 三处 add 清单（Commit & push 步）把源码也一起 add：
  `build_rss_aggregator.py` / `build_daily_insight.py` / `insight_engine.py` /
  `test_daily_insight.py` / `rss_sources.json` / `daily-insight-history.html`。
- 三处守卫只 checkout 了其中三个（`build_daily_insight.py insight_engine.py test_daily_insight.py`），
  **`build_rss_aggregator.py` 与 `rss_sources.json` 在射程内而无守卫**。
- 正常分支这样是安全的：CI 工作树是本 run 检出时的内容，构建脚本不会改写源码，
  `git add` 一个没变过的文件不产生 diff。
- 危险只在 push 失败的重试分支：原先写的是 `git reset --soft origin/main`。
  `--soft` 只挪 HEAD、**不动 index**，于是随后的 `git commit` 提交的是"旧那棵树的完整内容"
  —— 清单里没被本 run 改写过的源码文件会以旧检出进新提交，把别人之后推的改动原样回滚。
  这已经真发生过：09-19 03:37 第 8 轮洞察代码被 auto-commit 吞掉（:231-232 注释自己记着）。

修法口径：不用"哪些算源码"的清单（那份清单本身就是下一个漏口），而是让重试分支
**没有任何旧树可提交** —— 用 `git reset --hard origin/main` + 重跑构建（这正是兜底分支
原本已经在做的两件事），树在新 base 上重新生成，天然不可能回滚别人。
"""
import io
import os
import re

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
WORKFLOW = os.path.join(ROOT, ".github", "workflows", "update.yml")


def _wf():
    return io.open(WORKFLOW, encoding="utf-8").read()


def test_push_retry_never_keeps_a_stale_index():
    """整份 workflow 里不得出现 `git reset --soft`。

    这条锁的是机制而不是清单：只要还用 --soft，add 清单里每一个"本 run 不会改写"的
    路径（源码、清单 JSON）都变成可回滚别人的入口，而这类路径是靠人记的、必然记不全。
    """
    text = _wf()
    hits = [i + 1 for i, l in enumerate(text.splitlines())
            if re.search(r"^\s*git\s+reset\s+--soft\b", l)]
    assert not hits, (
        "update.yml 第 %s 行又用回 `git reset --soft`：它只挪 HEAD、保留旧 index，"
        "随后的 commit 会把 add 清单里未被本 run 改写的源码文件按旧检出回滚，"
        "别人刚推的改动就没了（09-19 03:37 实证）。重试分支请用 "
        "`git reset --hard origin/main` + 重跑构建" % hits)


def test_retry_path_regenerates_before_it_commits():
    """reset --hard 之后必须真的重跑构建，否则"安全"是拿"提交空产物"换来的。

    只把 --soft 换成 --hard 而不重建，工作区仍是旧检出 + 新 base 的混合，
    下一步 `git add <产物>` 会把旧产物再提一遍：不再吞别人，但站点会静默停止更新。
    """
    text = _wf()
    i_hard = text.find("git reset --hard origin/main")
    assert i_hard > -1, "重试兜底里找不到 `git reset --hard origin/main`，本判据失去抓手"
    after = text[i_hard:]
    i_commit = after.find("git commit")
    window = after[:i_commit if i_commit > -1 else len(after)]
    assert "fetch_and_build.py" in window, (
        "reset --hard 与下一次 commit 之间没有重跑构建 ⇒ 提交的是回滚后工作区里的旧产物，"
        "站点会静默冻在旧内容（这比吞档更难发现，因为 CI 是绿的）")


def test_aggregator_and_source_list_are_still_in_the_add_list():
    """防本判据变成空转：如果哪天有人把源码从 add 清单里摘掉，上面两条就该退役。

    把它们钉在这里，是为了让"退役判据"成为一次显式改动，而不是悄悄失效。
    """
    text = _wf()
    add_lines = [l for l in text.splitlines() if re.search(r"^\s*git add\b", l)]
    assert add_lines, "workflow 里找不到任何 git add，测试前提变了"
    joined = "\n".join(add_lines)
    for path in ("build_rss_aggregator.py", "rss_sources.json"):
        assert path in joined, (
            "%s 已不在 add 清单里 —— 回滚射程随之消失，"
            "本文件的 --soft 禁令可以退役为更弱的告警，但要先改这里再删判据" % path)


def test_every_run_step_is_shell_valid():
    """每个 run 步都要过 bash -n。

    起因是本次修改自身翻的车：我把 `git push || {` 删掉、留下一头无尾的 `}`，
    而 YAML 照样"解析成功、24 步不变" —— `run: |` 是 block scalar，解析器根本不看内容。
    只有 shell 语法检查抓得到这类破坏，而它的后果是整步 red ⇒ 后续部署被跳过 ⇒ 站点静默停更。

    刻意不用 PyYAML：A2 只 pip 装了 pytest，多引一个依赖就是环境差异风险
    （CI 红 / 本地绿的典型来源）。这里按本文件实际的缩进层级做纯文本抽取。
    """
    import subprocess
    import tempfile

    lines = _wf().split("\n")
    blocks, cur = [], None
    for ln in lines:
        if cur is None:
            if re.match(r"^ {8}run: *\|", ln):
                cur = []
                blocks.append(cur)
            continue
        # 块结束：遇到比内容基准（10 空格）更浅的非空行
        if ln.strip() and not ln.startswith(" " * 10):
            cur = None
            continue
        cur.append(ln[10:])
    assert len(blocks) >= 10, (
        "只抽到 %d 个 run 块，抽取器很可能失效了（实际应有 18 个）—— "
        "宁可让这条红，也不要让它静默全绿" % len(blocks))
    joined = "\n".join("\n".join(b) for b in blocks)
    for marker in ("git push || {", "trim_actions_cache.py"):
        assert marker in joined, "抽取结果里没有 %s ⇒ 抽取器漏了步" % marker

    bad = []
    for i, b in enumerate(blocks):
        script = re.sub(r"\$\{\{[^{}]*\}\}", "GH_EXPR", "\n".join(b))
        with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False,
                                         encoding="utf-8", newline="\n") as f:
            f.write(script)
            path = f.name
        try:
            r = subprocess.run(["bash", "-n", path], capture_output=True)
            if r.returncode != 0:
                err = (r.stderr or b"").decode("utf-8", "replace").strip().split("\n")
                bad.append("step#%d %s" % (i, (err[-1] if err else "?")[:120]))
        finally:
            os.unlink(path)
    assert not bad, "以下 run 步 shell 语法不合法：%s" % bad


if __name__ == "__main__":
    import sys
    import pytest
    sys.exit(pytest.main([__file__, "-q", "-s"]))
