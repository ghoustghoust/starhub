# -*- coding: utf-8 -*-
"""构建日志保留天数：钉在 1~7 天之间，两个方向都要挡。

为什么值得钉（2026-10-01 实测）：远端跟踪内容合计 72.99 MiB，`build_logs/` 一家 **42.52 MiB = 58%**
（15 个 `.jsonl`，单日 2.58~2.80 MiB，由 `cleanup(14)` 决定）。这是存量清理之后
**唯一还值得压的膨胀点**，也是最便宜的一个：没有任何读方读 5 天前的日志
（`build-log-summary.yml` 只写当天 summary），历史里的旧文件仍可随时回捞。

上界挡"顺手改回 14"；下界挡另一种更隐蔽的错法 —— 改成 `cleanup(0)` 以为"零保留最省"，
那会让**当天这场构建自己写的日志在同场被自己删掉**，排查只剩 Actions 临时日志（60 天后过期）。
"""
import io
import os
import re

import pytest

yaml = pytest.importorskip("yaml")

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WF = os.path.join(ROOT, ".github", "workflows", "update.yml")
MAX_KEEP_DAYS = 7


def _cleanup_calls():
    if not os.path.isfile(WF):
        pytest.skip("update.yml 不在（不在仓库检出里）")
    doc = yaml.safe_load(io.open(WF, encoding="utf-8").read())
    calls = []
    for job in doc["jobs"].values():
        for st in job.get("steps") or []:
            body = st.get("run") or ""
            calls += [int(n) for n in re.findall(r"build_logger\.cleanup\(\s*(\d+)\s*\)", body)]
    return calls


def test_retention_is_called_and_within_budget():
    calls = _cleanup_calls()
    assert calls, "没有任何 build_logger.cleanup(N) 调用：日志会无限堆进 git"
    over = [n for n in calls if n > MAX_KEEP_DAYS]
    assert not over, (
        "日志保留 %s 天 > %d 天：build_logs 是清理后剩下的最大膨胀点（占跟踪内容 58%%），"
        "要加长保留请先在手册里给出体积代价" % (over, MAX_KEEP_DAYS))


def test_retention_keeps_at_least_today():
    zero = [n for n in _cleanup_calls() if n < 1]
    assert not zero, (
        "cleanup(%s) 会让本场构建刚写的当天日志在同场被删掉（cutoff=今天），"
        "构建期排查就只剩 Actions 那份 60 天后过期的临时日志" % zero)
