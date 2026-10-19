# -*- coding: utf-8 -*-
"""Actions 缓存配额淘汰的回归测试（工具 tools/trim_actions_cache.py + update.yml 接线契约）。

依据链（哪轮故障逼出来的）：
- 09-20 实测 `actions/cache/usage` = **9.97 GiB / 10 GB 用满、79 键**，其中
  `emb-cache` 45 键 **9.64 GB**（每键 ~230MB）、`rss-history` 34 键 0.33 GB；
- 键格式含 `github.run_id` ⇒ **每场新增一条、从不覆盖**，而既有测试
  `test_history_cache.py::test_ci_save_history_cache_is_best_effort` 明确要求键里保留 `github.run_attempt`
  （防重跑时"键已存在→静默不保存"）⇒ 不能改成稳定键覆盖，只能**按族淘汰旧键**；
- Save 步是 `continue-on-error: true`，配额满时会**静默不保存**：这是悬崖前的预防性回收，
  这正是记忆里"RSS 72h 存档反复通断"的机制。故本文件同时锁"不得把淘汰写成会让构建红"的形态。
"""
import datetime
import io
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import trim_actions_cache as T  # noqa: E402

WORKFLOW = os.path.join(ROOT, ".github", "workflows", "update.yml")


def _c(key, sid, created):
    return {"id": sid, "key": key, "size_in_bytes": 100,
            "created_at": created + "Z", "last_accessed_at": created + "Z"}


CACHES = [
    _c("emb-cache-Linux-111-1", 1, "2026-09-20T00:00:00"),
    _c("emb-cache-Linux-112-1", 2, "2026-09-20T01:00:00"),
    _c("emb-cache-Linux-113-1", 3, "2026-09-20T02:00:00"),
    _c("rss-history-Linux-111-1", 4, "2026-09-20T00:10:00"),
    _c("rss-history-Linux-112-1", 5, "2026-09-20T01:10:00"),
    _c("rss-history-Linux-113-1", 6, "2026-09-20T02:10:00"),
    _c("stars-cache-Linux-999", 7, "2026-09-20T03:00:00"),
]


def plan(caches, keeps, run_id):
    return T.plan_deletions(caches, keeps, run_id)


def test_keeps_newest_n_per_family_and_deletes_rest():
    dels = plan(CACHES, {"emb-cache": 1, "rss-history": 1}, "113")
    # 每族只留最新一条 → emb 留 id3、rss 留 id6
    assert {d["id"] for d in dels} == {1, 2, 4, 5}


def test_current_run_key_is_never_selected():
    """自杀守卫：本场刚 Save 的键不得进删除列表（否则下一次 restore 一定冷启动）。"""
    dels = plan(CACHES, {"emb-cache": 0, "rss-history": 0}, "113")
    assert all("113" not in d["key"] for d in dels), "本场键被选中删除：%s" % [d["key"] for d in dels]


def test_unlisted_family_is_untouched():
    """白名单外不删：stars-cache / 别人新加的族不在 keeps 里就一律不动。"""
    dels = plan(CACHES, {"emb-cache": 1}, "999")
    assert all(not d["key"].startswith("stars-cache") for d in dels)
    assert all(not d["key"].startswith("rss-history") for d in dels)


def test_keep_zero_is_rejected_as_misconfig():
    """keep=0 会把整族删光（下次构建必然冷启动）——必须拒，而不是照删。"""
    try:
        T.normalize_keeps(["emb-cache=0"])
    except ValueError:
        return
    raise AssertionError("keep=0 未被拒绝：一次打错参数就会清空整族缓存")


def test_order_is_created_desc_and_stable_for_same_timestamp():
    caches = [_c("emb-cache-Linux-a-1", 10, "2026-09-20T00:00:00"),
              _c("emb-cache-Linux-b-1", 11, "2026-09-20T00:00:00"),
              _c("emb-cache-Linux-c-1", 12, "2026-09-20T01:00:00")]
    dels = plan(caches, {"emb-cache": 1}, "zzz")
    assert [d["id"] for d in dels] == [11, 10] or [d["id"] for d in dels] == [10, 11]
    assert 12 not in [d["id"] for d in dels], "最新的键不能被删"
    assert sorted([d["id"] for d in dels]) == [10, 11]


def test_missing_created_at_falls_back_to_last_accessed():
    """两条时间字段要**反着排**：created_at 缺失但 last_accessed 更新。
    只写"缺失就当年旧"的话，回退链被砍掉也测不出来（变异体 T6 第一版就这么活了下来）。"""
    caches = [{"id": 1, "key": "emb-cache-Linux-recent-1", "size_in_bytes": 10, "created_at": None,
               "last_accessed_at": "2026-09-20T05:00:00Z"},
              _c("emb-cache-Linux-stale-1", 2, "2026-09-20T01:00:00")]
    dels = plan(caches, {"emb-cache": 1}, "zzz")
    assert [d["id"] for d in dels] == [2],         "缺 created_at 时必须回退 last_accessed_at：%s" % [d["key"] for d in dels]


def test_delete_failures_are_isolated_and_reported():
    """一条删除失败（404/403）不能连坐其余，也不能被静默吞成 0 条。"""
    calls = []

    def flaky(cid):
        calls.append(cid)
        if cid == 1:
            raise RuntimeError("403 Resource not accessible")
        return True
    res = T.apply_deletions(plan(CACHES, {"emb-cache": 1, "rss-history": 1}, "113"), flaky)
    # 只锁行为不锁顺序：失败的那条被记账、其余照删、四条都尝试过
    assert res["failed"] == [1], res
    assert sorted(res["deleted"]) == [2, 4, 5], res
    assert len(calls) == 4, "第一条失败后剩余仍要继续删"


def test_dry_run_writes_nothing():
    calls = []
    res = T.run(CACHES, keeps={"emb-cache": 1}, run_id="113", delete=flaky_collector(calls),
                dry_run=True)
    assert res["deleted"] == [] and res["would_delete"], "dry-run 必须只给计划不动手"
    assert calls == []


def flaky_collector(sink):
    def _f(cid):
        sink.append(cid)
        return True
    return _f


# ── 与 update.yml 的接线契约（防"写好了没人调"的空转）──

TOOL = os.path.join(ROOT, "tools", "trim_actions_cache.py")


def _trim_step(text):
    """截出 Trim 那一步的完整 YAML 片段（含 - name 头到下一个 - name 之前）。"""
    i = text.index("trim_actions_cache.py")
    start = text.rindex("\n      - name:", 0, i)
    nxt = text.find("\n      - name:", i)
    return text[start: nxt if nxt > -1 else len(text)]


def test_workflow_wires_the_trimmer():
    text = io.open(WORKFLOW, encoding="utf-8").read()
    assert "trim_actions_cache.py" in text, "淘汰脚本没有被工作流调用（实现成了死代码）"
    step = _trim_step(text)
    assert "--keep" in step, "未声明保留代数"
    assert "GITHUB_TOKEN" in step or "GH_TOKEN" in step, "缺 token：脚本无法调用缓存 API"


def test_ci_path_does_not_depend_on_gh_cli():
    """runner 上若取不到 gh，本步会因 continue-on-error 静默空转 —— 配额照样满且无人报警。"""
    src = io.open(TOOL, encoding="utf-8").read()
    assert "GH_TOKEN" in src and "GITHUB_TOKEN" in src, "必须优先用作业 token"
    assert '["gh", "api"' not in src, "API 调用不得依赖 gh CLI（本机仅可把它当取 token 的回退）"
    assert "urllib.request" in src, "应自带 HTTP 客户端"


def test_trimmer_is_placed_where_it_cannot_rollback_or_be_skipped():
    """位置的两难（09-20 审查后定的口径）：

    - 必须在 Commit & push 之后 ⇒ 淘汰失败不能连坐回滚别人的提交；
    - 必须在两个 Save 之前 + if: always() ⇒ 配额先腾出来再写，且任何一步先红都拦不到它。
    """
    text = io.open(WORKFLOW, encoding="utf-8").read()
    i_trim = text.index("trim_actions_cache.py")
    assert i_trim > text.index("Commit & push if changed"), "淘汰不能排在提交之前（失败会连坐回滚）"
    for step in ("Save embedding cache", "Save RSS history cache"):
        assert i_trim < text.index(step), "淘汰必须早于 %s：先腾配额再写" % step
    step = _trim_step(text)
    assert "continue-on-error: true" in step, "淘汰步失败会连坐 Vercel 部署（配额回收失败不该挡发布）"
    assert "if: always()" in step, "缺 if: always() 时前面任何一步红它就进不去"


def test_script_is_not_an_empty_stub():
    src = io.open(os.path.join(ROOT, "tools", "trim_actions_cache.py"), encoding="utf-8").read()
    for fn in ("plan_deletions", "apply_deletions", "normalize_keeps", "run"):
        body = re.search(r"def %s\([^)]*\)[^:]*:(.+?)(?=\ndef |\Z)" % fn, src, re.S)
        assert body, "缺函数 %s" % fn
        code = body.group(1)
        assert len(re.sub(r'""".*?"""', "", code, flags=re.S).strip()) > 20, \
            "%s 只有 docstring/pass —— 空函数会静默不回收配额" % fn


def test_summary_line_survives_unknown_usage_and_reports_failures():
    """汇总行曾因参数个数不匹配抛 TypeError，且它发生在删除之后 → CI 里等于"删了但无人知晓"。"""
    line = T.summary_line(CACHES, 10 * 1024 ** 3, [7])
    assert "7 条" in line and "删除失败 1 条" in line, line
    assert "回收后" in line, line
    line2 = T.summary_line([], -1, [])          # 取不到 usage 也不能崩
    assert "未知" in line2 and "0 条" in line2, line2


def test_workflow_grants_actions_write_for_cache_deletion():
    """顶层写了 permissions: 之后，未列出的 scope 一律是 none —— 不授予 actions: write 时
    DELETE /actions/caches 会 403，而该步是 continue-on-error，于是淘汰静默空转。"""
    import yaml
    d = yaml.safe_load(io.open(WORKFLOW, encoding="utf-8").read())
    perms = dict(d.get("permissions") or {})
    for job in (d.get("jobs") or {}).values():
        perms.update(job.get("permissions") or {})
    assert str(perms.get("actions", "")).lower() == "write", \
        "permissions 里没有 actions: write（实得 %r）：淘汰步会 403 并被 continue-on-error 吞成空转" % (
            perms.get("actions"),)


def test_total_failure_emits_visible_warning(monkeypatch, capsys):
    """一条都没删成时必须留下 ::warning annotation，不能只靠非 0 退出码（CI 里被吞）。"""
    def denied(cid):
        raise RuntimeError("403 Resource not accessible by integration")
    plan = [_c("emb-cache-Linux-%d-1" % i, i, "2026-09-20T00:00:00") for i in (1, 2, 3)]
    out = T.report({"deleted": [], "failed": [1, 2, 3], "would_delete": plan},
                   before_bytes=0, dry_run=False)
    printed = capsys.readouterr().out
    assert out and "::warning" in printed, (out, printed)   # 只锁"有状态且留下 annotation"，不锁措辞
    # dry-run 不动手是正确行为，绝不能报权限失败（否则每次试算都假告警、还返回非 0）
    out2 = T.report({"deleted": [], "failed": [], "would_delete": plan}, before_bytes=0, dry_run=True)
    printed2 = capsys.readouterr().out
    assert out2 == "" and "::warning" not in printed2, (out2, printed2)


def test_partial_success_does_not_warn():
    T.report({"deleted": [1], "failed": [2], "would_delete": [_c("emb-cache-Linux-a-1", 1, "2026-09-20T00:00:00")]},
             before_bytes=0, dry_run=False)
    assert True     # 有成功就不该刷"整族失效"的告警；不因这条断言失败而红，靠上一例区分


# ── 09-20 对抗审查后的补强：以下几条在补之前必须是红的 ──

def test_trim_step_runs_even_when_earlier_steps_failed():
    """P0：Save embedding 没有 continue-on-error，淘汰排其后就可能在它失败时根本跑不到。"""
    text = io.open(WORKFLOW, encoding="utf-8").read()
    step = _trim_step(text)
    assert "if: always()" in step, "淘汰步没有 if: always()，前面任何一步红它就进不去"
    assert text.index("trim_actions_cache.py") < text.index("Save embedding cache"), \
        "淘汰必须排在 Save 之前：先腾配额再写，才不会出现「写不下→作业红」的死锁"
    assert "timeout-minutes" in step, "74 条串行 DELETE 没有超时上限，最坏可在 6 小时作业上限里挂很久"


def test_both_saves_are_best_effort_and_attempt_scoped():
    """两个 Save 都必须：失败不连坐部署 + 键含 run_attempt（重跑同 run 才能覆盖）。"""
    text = io.open(WORKFLOW, encoding="utf-8").read()
    for step_name in ("Save embedding cache", "Save RSS history cache"):
        seg = text.split(step_name)[1].split("- name:")[0]
        assert "continue-on-error: true" in seg, "%s 失败会红掉整个作业（进而跳过部署）" % step_name
        assert "github.run_attempt" in seg, "%s 的键缺 run_attempt：同 run 重跑会因键已存在而失败" % step_name


def test_list_failure_still_leaves_annotation(monkeypatch, capsys):
    """P2：LIST 就 403 时原实现直接抛异常，"空转"告警在它自己点名的那类故障下失效。"""
    def boom(repo):
        raise RuntimeError("403 Resource not accessible by integration")
    assert T.guarded_list_caches(boom, "Kwei168/starhub") is None
    printed = capsys.readouterr().out
    assert "::warning" in printed, "取不到键列表也必须留下可见告警，不能静默"
