# -*- coding: utf-8 -*-
"""批 1 判据：跨场状态缓存族 `starhub-state` 的接线（顺序、键形状、名单、淘汰、冷启动出声）。

为什么做这件事（实测，2026-10-01）：一次 bot 提交新增 **14 个 blob / 19.83 MiB、消失 0 个**，
其中 17.4 MiB 是下面这 7 个"只有构建脚本自己读回"的跨场状态文件。git 的模型是
"内容变一次就永久多一份副本"，所以 `cleanup(N)`/保留天数只改斜率；**要让曲线平，
这些文件必须不再每场入库**，而它们的跨场传递要由 `actions/cache` 承接（`rss_history`、
`emb-cache` 已经是这个形状）。

本批只搭通路、不动提交清单 ⇒ 7 个文件仍然入库，零风险；下一批才从 `git add` 里摘名字。
所以这批的判据全部钉"通路是否真的接对了"，而不是"是否已退役"：

  ① Restore 必须在 `git clean -fdq`（`Restore worktree after insight tests`）之后、
     `Fetch stars & build` 之前 —— 早于 clean 会被当场删掉，等于没还原。
  ② Save 必须在 `Cleanup old build logs` 之后、`Prune large build artifacts` 之前 ——
     晚于 prune 就没目录可存（prune 会 `rm -rf build_logs`），早于 cleanup 会把过期日志存进缓存。
  ③ Restore 与 Save 的 key 必须逐字相同、含 run_id 与 run_attempt、族名前缀与
     `--keep starhub-state=N` 的 N 边对得上 —— `trim_actions_cache.py` 靠 key 前缀认族，
     名字写错就是"每场新增一键、永不被淘汰"的新无界点。
  ④ path 名单必须精确等于 7 个状态文件 + `build_logs`，多一个少一个都算接线错。
  ⑤ 冷启动必须出声（尤其 translations / analysis_snapshot 这两个"丢了要花钱"的），
     但不许拦构建 —— 判据要能区分"静默退化"与"出声退化"。
"""
import os
import subprocess

import pytest

yaml = pytest.importorskip("yaml")

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WF = os.environ.get("STARHUB_UPDATE_YML") or os.path.join(ROOT, ".github", "workflows", "update.yml")
# 变异注入口：与 STARHUB_UPDATE_YML / RSS_BUILD_SRC 同族约定。没有它，"删一行 .gitignore"
# 这类变异根本传不进判据，对账判据就成了只能证明自己对的东西（写第一版时就是这个状态）。
GITIGNORE = os.environ.get("STARHUB_GITIGNORE") or os.path.join(ROOT, ".gitignore")

FAMILY = "starhub-state"
# 这 7 个是"每场都变、且只有构建脚本自己读回"的跨场状态（实测字节见模块 docstring）
STATE_FILES = {
    "hot_history.json",
    "analysis_snapshot.json",
    "translations.json",
    "rss_trend_history.json",
    "insight_tracking_history.jsonl",
    "daily_insight_tracking_history.jsonl",
    "daily_insight_history.json",
}
LOG_DIR_NAME = "build_logs"
# 批 5a：这三个也是"先读回上一次结果、再写回"的跨场缓存，但它们的传递通道**今天只有 git checkout**
# （实测：`fetch_and_build.py:432` 读 trending_snapshot、`:486` 写回；`:722` 读 descriptions_zh、`:813` 写回；
# `build_rss_aggregator.py:8732` 在抓取失败时兜底读 hot_snapshot）。它们既不在缓存 path 也不在 .gitignore，
# 所以 批 5 若直接把它们从 add 清单摘掉 ⇒ 星标增量的基线与描述译文缓存被永久冻在最后一份提交上
# （delta 每场对着不动的基线重算，新仓库描述每场重译烧配额）。先补通路，再摘名字，顺序不能反。
CARRY_FILES = {
    "descriptions_zh.json",
    "trending_snapshot.json",
    "hot_snapshot.json",
}
# 反空转对照用：批 2 没动它，两条 `git add` 清单里都还有 ⇒ 只要工作流还在跑就一直被跟踪。
PROBE_TRACKED_CONTROL = "rss_sources.json"
PROBE_ABSENT_CONTROL = "starhub_probe_control_absent.json"


def _steps():
    with open(WF, encoding="utf-8") as fh:
        doc = yaml.safe_load(fh)
    steps = doc["jobs"]["update"]["steps"]
    assert isinstance(steps, list) and len(steps) >= 20, (
        "update.yml 解析出的步骤数异常（%r）—— 判据不能建在一次失败的解析上" % (steps,))
    return steps


def _by_name(frag):
    hits = [s for s in _steps() if frag in (s.get("name") or "")]
    assert len(hits) == 1, "步骤名 %r 命中 %d 个（应为 1 个），判据的定位失效" % (frag, len(hits))
    return hits[0]


def _idx(frag):
    names = [(s.get("name") or "") for s in _steps()]
    hits = [i for i, n in enumerate(names) if frag in n]
    assert len(hits) == 1, "%r 在步骤序列里出现 %d 次，顺序判据无法定位：%s" % (frag, len(hits), names)
    return hits[0]


def _path_lines(step):
    raw = (step.get("with") or {}).get("path") or ""
    entries = [ln.strip() for ln in str(raw).splitlines() if ln.strip()]
    assert entries, "步骤 %r 的 path 是空的 —— 等于缓存了个寂寞" % step.get("name")
    return entries


def test_state_restore_step_exists_and_uses_cache_restore():
    s = _by_name("Restore cross-build state cache")
    assert s.get("uses", "").startswith("actions/cache/restore@v"), (
        "还原步必须用 actions/cache/restore（不是 actions/cache@v4，后者会把 Save 也带进还原位置）")


def test_state_save_step_exists_and_is_nonblocking():
    s = _by_name("Save cross-build state cache")
    assert s.get("uses", "").startswith("actions/cache/save@v"), (
        "保存步要用 actions/cache/save，且必须与还原步成对，否则每场只读不写、缓存永远是旧的")
    assert s.get("continue-on-error") is True, (
        "保存失败不得连坐 Vercel/Pages 部署（本仓有 A2 blocking 连坐冻两小时的前例，方向不能反过来）")


def test_state_cache_key_is_identical_on_both_ends_and_run_unique():
    r = (_by_name("Restore cross-build state cache").get("with") or {})
    w = (_by_name("Save cross-build state cache").get("with") or {})
    rk, wk = r.get("key", ""), w.get("key", "")
    assert rk and rk == wk, (
        "还原 key %r 与保存 key %r 必须逐字相同；不一致时下一场永远命中不到自己上一场写的键"
        % (rk, wk))
    assert rk.startswith(FAMILY + "-"), "key 必须以族名 %s- 开头，否则 trim 认不出这个族" % FAMILY
    for token in ("github.run_id", "github.run_attempt"):
        assert token in rk, "key 缺 %s：同 run 重跑会因'键已存在'静默不保存（rss-history 那步的实测教训）" % token


def test_state_restore_keys_fall_back_to_family_prefix():
    r = _by_name("Restore cross-build state cache").get("with") or {}
    prefix = (r.get("restore-keys") or "").strip()
    assert prefix.startswith(FAMILY + "-"), (
        "restore-keys 必须是 %s- 前缀回退，否则缓存被清一次就永久冷启动（每场重跑 LLM 重分析）" % FAMILY)


def test_state_restore_runs_after_worktree_clean_and_before_build():
    a = _idx("Restore worktree after insight tests")
    b = _idx("Restore cross-build state cache")
    c = _idx("Fetch stars & build")
    assert a < b < c, (
        "还原必须在 git clean -fdq 之后（:143 那步会把未跟踪的还原文件当垃圾删掉）、构建之前")


def test_state_save_runs_after_cleanup_and_before_prune():
    a = _idx("Cleanup old build logs")
    b = _idx("Save cross-build state cache")
    c = _idx("Prune large build artifacts")
    assert a < b < c, (
        "保存要在 cleanup 之后（否则把过期日志存进缓存）、prune 之前（prune 会 rm -rf build_logs）")


@pytest.mark.parametrize("step_name", ["Restore cross-build state cache",
                                        "Save cross-build state cache"])
def test_state_path_list_is_exactly_the_family_plus_carry_plus_log_dir(step_name):
    entries = set(_path_lines(_by_name(step_name)))
    want = STATE_FILES | CARRY_FILES | {LOG_DIR_NAME}
    assert entries == want, (
        "%s 的 path 名单与（7 个状态文件 ∪ 3 个批 5a 补通路的读回文件 ∪ build_logs）不一致："
        "多出来=%s，少了=%s" % (step_name, sorted(entries - want), sorted(want - entries)))


def test_two_path_lists_are_identical():
    r = set(_path_lines(_by_name("Restore cross-build state cache")))
    w = set(_path_lines(_by_name("Save cross-build state cache")))
    assert r == w, "还原与保存的名单必须同一份，否则某族文件会被'保存但不还原'或反之"


def test_trim_keeps_starhub_state_family_with_a_real_budget():
    text = open(WF, encoding="utf-8").read()
    kv = dict()
    for m in __import__("re").finditer(r"--keep\s+([A-Za-z0-9_-]+)=(\d+)", text):
        kv[m.group(1)] = int(m.group(2))
    assert FAMILY in kv, (
        "键里含 run_id ⇒ 每场新增一条键从不覆盖；不给这个族配 --keep 就是新造一个无界增长点")
    assert kv[FAMILY] >= 2, "--keep %s=%d 太少：缓存被清一次以上就没得回退" % (FAMILY, kv[FAMILY])
    # 族名必须真的是 key 的前缀，否则 trim 按前缀认族时会把它当"不认识的族"跳过
    key = ((_by_name("Save cross-build state cache").get("with") or {}).get("key") or "")
    assert key.startswith(kv and FAMILY + "-"), "key 前缀与 --keep 族名不一致：%r" % key


def test_diagnose_step_reports_bytes_and_warns_on_the_expensive_two():
    """冷启动必须出声：这两个文件丢了不是"少点信息"，是真花钱/真重跑 LLM。"""
    s = _by_name("Diagnose cross-build state cache")
    body = s.get("run") or ""
    assert body.startswith("\n") or body, "Diagnose 步没有 run 内容"
    assert "wc -c" in body, "诊断必须打印真实字节数 —— cache-hit/matched-key 在前缀命中时不可信（实测）"
    assert "::warning" in body, "冷启动必须出声；静默退化是本仓付过两次代价的形态"
    for expensive in ("translations.json", "analysis_snapshot.json"):
        assert expensive in body, "%s 的冷启动代价最贵，诊断必须点名它" % expensive
    for f in sorted(STATE_FILES):
        assert f in body, "诊断没覆盖 %s：那它是否在盘上无人知道" % f


def _added_names():
    """解析出所有 `git add <操作数>` 里真正被 add 的路径名。

    不能用"这一行有没有这个名字"来判断：条件式 `if [ -f x ]; then git add y; fi` 里
    `[ -f ]` 那一半也带着 x，用整行子串会被骗过（S13 变异体实测就是这么逃掉的）。
    """
    import re
    text = open(WF, encoding="utf-8").read()
    names = set()
    for m in re.finditer(r"git add ([^\n;|&]+)", text):
        for tok in m.group(1).split():
            if tok.startswith("-"):
                continue
            names.add(tok.rstrip("/"))
    assert names, "一个 git add 操作数都没解析到 —— 判据在空集合上跑"
    return names


def _gitignore_names():
    """只收"单文件名"形式的条目（不含目录、不含通配），用于与状态族名单对账。"""
    names = set()
    with open(GITIGNORE, encoding="utf-8") as fh:
        for ln in fh:
            s = ln.strip()
            if not s or s.startswith("#") or "*" in s:
                continue
            names.add(s.rstrip("/"))
    return names


# ── 批 2：状态文件退役（本文件里这三条是批 1 那条"仍然入库"的反向版本）──

def test_state_family_files_are_not_committed_anymore():
    """这 7 个跨场状态必须**不再**出现在 `git add` 的操作数里 —— 它们就是 17.4 MiB/场的来源。"""
    names = _added_names()
    still = sorted(f for f in STATE_FILES if f in names)
    assert not still, (
        "这些跨场状态仍每场提交（每场给 git 历史永久多一份副本）：%s" % ", ".join(still))


def test_state_files_are_in_both_gitignore_and_cache_paths():
    """双向对账：每个状态文件必须**同时**在 `.gitignore` 与缓存 path 里。

    少 `.gitignore` ⇒ 它以"未跟踪但存在"的形态污染 git status，并可能被下一次 `git add <显式名>`
    请回库里；少缓存 path ⇒ 它既不在 git 也不在缓存 = 下一场直接冷启动，而这条链路是**静默**的
    （构建照样绿，只是没人发现译文缓存没了）。所以两个集合都断言非空，禁"两边都空所以相等"。
    """
    ign = _gitignore_names()
    cached = set(_path_lines(_by_name("Save cross-build state cache")))
    assert ign and cached, "对账输入为空（.gitignore=%d, path=%d）—— 判据在空集合上跑" % (
        len(ign), len(cached))
    not_ignored = sorted(f for f in STATE_FILES if f not in ign)
    not_cached = sorted(f for f in STATE_FILES if f not in cached)
    assert not not_ignored and not not_cached, (
        ".gitignore 缺少=%s；缓存 path 缺少=%s" % (", ".join(not_ignored) or "无",
                                                   ", ".join(not_cached) or "无"))


def test_carry_files_have_a_cache_home_before_they_leave_git():
    """批 5a：三个"读回型"文件必须先有缓存通路（path + .gitignore + 诊断出声），才谈得上摘 add 名字。

    与上一条判据同构，但防的是另一类事故：**通路没搭就摘名字**。那种情况下构建照样绿，
    只是 `trending_snapshot.json` 从此停在库里那份旧基线，星标增量每场对着静止基线重算 ⇒
    数字一路虚高，而没有任何一处会报错。所以三个方向都断言，且输入非空。
    """
    ign = _gitignore_names()
    cached = set(_path_lines(_by_name("Save cross-build state cache")))
    body = (_by_name("Diagnose cross-build state cache").get("run") or "")
    assert ign and cached and body, "对账输入为空（.gitignore=%d, path=%d, 诊断=%d）—— 判据在空集合上跑" % (
        len(ign), len(cached), len(body))
    not_ignored = sorted(f for f in CARRY_FILES if f not in ign)
    not_cached = sorted(f for f in CARRY_FILES if f not in cached)
    not_diagnosed = sorted(f for f in CARRY_FILES if f not in body)
    assert not not_ignored and not not_cached and not not_diagnosed, (
        "批 5a 通路未补齐：.gitignore 缺少=%s；缓存 path 缺少=%s；诊断未覆盖=%s" % (
            ", ".join(not_ignored) or "无", ", ".join(not_cached) or "无",
            ", ".join(not_diagnosed) or "无"))


def test_tracking_probe_works_in_both_directions():
    """批 3 那条"不再被跟踪"判据的反空转守卫：同一个探针必须在两个方向都能出活。

    `git ls-files --error-unmatch` 返回非 0 有两种完全不同的原因：文件确实没被跟踪，
    和 git 根本没法问（坏索引、浅检出、换机器、不在仓里）。前者是真退役，后者是**假绿**——
    而假绿的判据此后永远拦不住"谁把状态文件 add 回来"。
    所以这里双向都钉：一个必须"看得见"（在 add 清单里、批 2 没动的 rss_sources.json），
    一个必须"看不见"（仓里压根没有的名字）。任一侧失配 ⇒ 探针失效，批 3 判据不可信。
    """
    def code(name):
        return subprocess.run(["git", "ls-files", "--error-unmatch", name],
                              capture_output=True, text=True, cwd=ROOT).returncode

    tracked = code(PROBE_TRACKED_CONTROL)
    assert tracked == 0, (
        "探针看不见 %s（它仍在两条 git add 清单里，理应被跟踪）—— git 侧出问题了，"
        "此时 test_state_files_are_not_tracked 的「绿」是假绿：%s" % (PROBE_TRACKED_CONTROL, "探针失效"))
    absent = code(PROBE_ABSENT_CONTROL)
    assert absent != 0, (
        "探针说 %s 被跟踪，而这个名字在仓里根本不存在 ⇒ git 侧对任何名字都返回 0，"
        "批 3 那条判据会在「全都跟踪」的仓里判绿" % PROBE_ABSENT_CONTROL)


def test_state_files_are_not_tracked():
    """批 3：这 7 个状态文件必须**不再被 git 跟踪**。

    与 `test_state_family_files_are_not_committed_anymore` 是两道不同的闸：
      · 不 commit（批 2）挡的是"工作流再把它们写进提交"；
      · 不 tracked（批 3）挡的是"库里那份旧副本被谁 git add 一下又活过来"——
        只要它们还在树里，每场重写就仍然给不可回收的历史加 17.4 MiB。
    CI 跑干净检出 ⇒ 本地与 CI 都等价于"树里没有这些路径"。
    """
    offenders = []
    for f in sorted(STATE_FILES):
        r = subprocess.run(["git", "ls-files", "--error-unmatch", f],
                           capture_output=True, text=True, cwd=ROOT)
        if r.returncode == 0:
            offenders.append(f)
    assert not offenders, (
        "这些跨场状态仍被 git 跟踪（每场重写 = 永久多一份副本）：%s；退役方式："
        "data_api_push.py --delete 逐个列名，再本地 git rm --cached 对齐索引" % ", ".join(offenders))


def test_build_logs_dir_is_cache_carried():
    """build_logs 必须在缓存 path 里 —— 它是摘要"每天一次"能成立的前提。

    批 4 之后 jsonl 不再每场入库（每天一次快照），所以跨场累积**只能**由缓存承担；
    缓存里少这一项的话，`build_logger.summary(today)` 每场都会看到空目录，摘要恒成 builds=1。
    """
    cached = set(_path_lines(_by_name("Save cross-build state cache")))
    assert LOG_DIR_NAME in cached, (
        "build_logs 不在缓存 path：批 4 的每日闸门让 jsonl 退出每场提交后，就没有任何东西承载累积了")

def test_state_path_list_has_a_frozen_record_copy():
    """批 5a 付过学费的接缝：缓存 path 名单是**族身份的一部分**，改名单 = 换族 = 旧缓存全不可达。

    实测证据（2026-10-01 14:00 场，head=批 5a）：`Cache not found for input keys:
    starhub-state-Linux-36872974114-1, starhub-state-Linux-` —— 前缀回退也没命中，而库里明明还躺着
    12:20 / 12:56 / 13:38 三份 4.48 MB 的同族缓存。唯一变化就是 path 从 7 条变 10 条。
    后果当场可见：那一场把 translations 从零重建到 30,000 上限、撞了 34 次 429，
    而 hot_history / insight_tracking 这类**append-only 历史**是重建不出来的（7 天窗口要从头积）。

    所以名单不许"顺手加一行"：改名单必须同时改这里，并在同一批里安排**播种**（从历史 blob 回灌）
    或明确接受一次冷启动。判据本身用变异自证能红（tools/mut_state_cache.py 的 C6）。
    """
    frozen = {"hot_history.json", "analysis_snapshot.json", "translations.json",
              "rss_trend_history.json", "insight_tracking_history.jsonl",
              "daily_insight_tracking_history.jsonl", "daily_insight_history.json",
              "descriptions_zh.json", "trending_snapshot.json", "hot_snapshot.json",
              LOG_DIR_NAME}
    now = set(_path_lines(_by_name("Restore cross-build state cache")))
    assert now == frozen, (
        "缓存 path 名单与登记副本不一致（多=%s，少=%s）。这会让整族旧缓存瞬间不可达 ⇒ 冷启动。"
        "要么改回名单，要么在同一批里 bump 本判据并安排播种/写明接受冷启动" % (
            sorted(now - frozen), sorted(frozen - now)))
