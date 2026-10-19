# -*- coding: utf-8 -*-
"""star 状态与它自己的 orphan 分支 `star-state` 之间的并集两端（读端 + 写端）。

**读端**（update.yml 默认调用，无参数）：把 star-state 上的状态并集回工作树。
**写端**（star-fast.yml 用 `--merge-onto <父提交 sha>` 调用）：反方向，把分支上已有的键并进
工作树，好让本场提交的 tree 不小于上一场——见 `_merge_onto` 的注释（10-08 实测丢键）。
两个方向共用同一份 `FILES` 与地板值闸门，就是为了不让"并集"这件事出现两套写法。

为什么要分支：`star-fast.yml` 原本每 15 分钟往 `main` 提交一次 `known_categories.json` /
`known_notes.json`，而 `update.yml` 单场构建要 ~35 分钟 ⇒ 小时场的 `git push` 必然撞上它，
重试只有一次且重试本身要 5 分钟，第二次再被拒就整场红、Pages 整场不发（2026-10-07 04:00 实锤）。
让 star 状态改推 `star-state`（本车道是唯一写者 ⇒ 不需要 merge、不需要重试链），`main` 的
自动写者就只剩 update.yml 一个；小时场在构建前把状态读回来，功能上等价、时序上不再争抢。

三条不可让的约束（都对应真实事故形状）：
① **并集，绝不缩小**：star-state 的值覆盖同名键，main 独有的键一律保留。
   于是"star-state 被误清空/写坏"最坏只退化成"没拿到新分类"，不会退化成"分类表被抹掉"。
② **地板值闸门**：合并后 `known_categories` 的键数必须 ≥ FLOOR_KEYS。
   `fast_refresh.py` 是按"不在 known 表里"对全表做 diff 来决定烧多少 LLM，
   空表 = 293 条全量重烧（现取 main 上 293 键）；地板取 200 留余量。不达标就**一个字节都不写**。
③ **读不到只降级、绝不挡发布**：分支不存在 / fetch 失败 / 解析失败 ⇒ 保持 main 那份 + 报
   warning + 退出 0。本仓有"A2 blocking + Deploy 无 if"的连坐教训，新链路不许成为新的单点。

用法：
    读端（update.yml，在 Sync to latest remote 之后）  python tools/restore_star_state.py
    写端（star-fast.yml，fetch 到父提交之后、造 tree 之前）python tools/restore_star_state.py --merge-onto "$parent"
退出码恒为 0（真异常也只报 warning），除 --strict 模式外。
"""
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BRANCH = "star-state"
FILES = ("known_categories.json", "known_notes.json")
FLOOR_KEYS = 200          # 见②：现取 main 上 known_categories 有 293 键
# fast_refresh 本场写的"当前收藏 full_name 名单"。刻意放 .deploy-tmp/（已 gitignore）：
# 它是**场内传递**，不是跨场状态——写端并集是只增不减的，死键只能靠这份名单才认得出来，
# 而名单每场重算，存下来反而会多一个需要人维护的跨场文件。
LIVE_LIST_REL = os.path.join(".deploy-tmp", "star_live_names.json")
BJT = timezone(timedelta(hours=8))


def _live_names():
    """读 fast_refresh 本场写的 live 名单。读不到/坏掉/空 ⇒ None，调用方一律不剪。

    启用条件刻意保守：剪枝是"有意缩小"，与本脚本默认的"绝不缩小"方向相反，
    所以宁可漏剪也不能错剪——名单缺失或形状不对时，最坏只是"这一场没清死键"，
    而反过来会把还在收藏的键剪没（下一场全量重烧 LLM）。
    """
    try:
        with open(os.path.join(ROOT, LIVE_LIST_REL), encoding="utf-8") as f:
            d = json.load(f)
    except Exception:  # noqa: BLE001 —— 读不到就是不剪，不需要区分原因
        return None
    if not isinstance(d, list) or not d:
        return None
    return {x for x in d if isinstance(x, str) and x} or None


def _prune_to_live(merged_out, live):
    """把并集结果剪成 live 名单的投影。返回剪掉的键数，0 表示没动。

    为什么必须在这一侧剪：`--merge-onto` 是 `dict(incoming)` 打底，父提交上的死键一定会被
    捞回来，所以在 `fast_refresh` 里剪是白剪——那是本仓"每次改名都要人手工清一次分支"的
    唯一原因（main 侧的 prune_star_state 管不到这条车道）。

    剪完仍不足 FLOOR_KEYS ⇒ 判定名单不可信，**只放弃缩小、不放弃并集**：返回 0 而不是 3，
    因为并集本身是完整的，返回 3 会让调用方连分类结果都不提交，把"名单坏了"升级成"丢分类"。
    """
    if not live:
        return 0
    trial = {name: {k: v for k, v in d.items() if k in live}
             for name, d in merged_out.items()}
    kc = trial.get("known_categories.json")
    if kc is not None and len(kc) < FLOOR_KEYS:
        print("::warning::[star-state] 拒绝剪枝：按 live 名单剪后 known_categories 只剩 %d 键"
              "（地板 %d，名单 %d 条）⇒ 判定名单不可信，本场只并集不缩小"
              % (len(kc), FLOOR_KEYS, len(live)))
        _stamp("prune_refused_under_floor",
               "after=%d floor=%d live=%d" % (len(kc), FLOOR_KEYS, len(live)))
        return 0
    dropped = sum(len(merged_out[n]) - len(trial[n]) for n in merged_out)
    if not dropped:
        return 0
    for name in merged_out:
        merged_out[name] = trial[name]
    print("[star-state] 剪枝：按本场 live 名单（%d 条）移除 %d 个已不在收藏的键"
          % (len(live), dropped))
    _stamp("pruned", "live=%d dropped=%d" % (len(live), dropped))
    return dropped



def _sh(*args):
    """跑 git，返回 (rc, stdout+stderr)。不抛异常——本步的任何失败都只该降级。"""
    try:
        r = subprocess.run(("git", "-C", ROOT) + args, capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=120)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except Exception as exc:  # noqa: BLE001
        return 124, "launcher error: %s" % exc


def _load(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001
        return None


def _show(rev, name):
    """从某个 rev 取文件内容（走 git show，不动工作树）。"""
    rc, out = _sh("show", "%s:%s" % (rev, name))
    return out if rc == 0 else None


def _atomic_write(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _stamp(kind, detail):
    """落痕到 build_logs/（形状与 update.yml:511-514 的 pages_missing 一致，便于同一套读法）。"""
    try:
        d = os.path.join(ROOT, "build_logs")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, datetime.now(BJT).strftime("%Y-%m-%d") + ".jsonl"),
                  "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": datetime.now(BJT).isoformat(),
                                "type": "star_state_readback", "kind": kind,
                                "detail": detail}, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 —— 落痕失败不该影响主流程，但也不能静默到查不到
        sys.stderr.write("[star-state] 落痕失败（build_logs 不可写）\n")


def _merge_onto(rev, strict=False):
    """写端并集：以**工作树那份为准**补上分支独有的键，绝不缩小。

    为什么策略和读端相反（读端是分支赢）：读端的 base 是小时场自己的旧表，分支上才是快车道的
    新分类 ⇒ 分支赢；写端的 base 里含**本场刚算出来的**分类结果 ⇒ 本场赢，分支只用来把
    main 上已经缺失的键捞回来。

    这条补的是 2026-10-08 实测的真丢键：写端原先只取父提交的 sha 当 parent、不取它的内容，
    而本车道的底本来自 main——main 上 `known_notes.json` 最后一次被写是 06:46Z（P1 落地之前），
    因为 `update.yml` 的 add 清单（:342/:377）里有 `known_categories.json` **却没有**
    `known_notes.json` ⇒ 分支每被重写一次，上一场写进去的点评就没了
    （`libukai/awesome-deepseek-harness` 那条在 main 与新 tip 里双双不存在，只在被取代的
    6728c902 里活过一场）。

    返回 3 = "并集没做完，调用方必须放弃本场提交"。写端与读端在这里的区别是致命的：读端不写
    很安全（main 那份本来就在），写端不写会一路走到 `git push`，推上去的就是没并过的缩小 tree。
    所以"读不出基线"的每种情形（rev 在本地解析不出、git 命令失败、工作树/分支那份坏了）
    一律给 3，绝不跟"分支上确实没有这个文件"折成同一种静默放行——那是审查时实测到的假绿形状。
    """
    rc, err = _sh("cat-file", "-e", "%s^{commit}" % rev)
    if rc != 0:
        # 浅检出里没有这个对象（10-08 09:45/10:00 那两场的同一个形状：拿远端 sha 当本地对象用）。
        print("::error::[star-state] --merge-onto 的 rev 在本地解析不出提交（%s）⇒ 并集没做"
              % ((err.strip()[:120] or rev[:40])))
        _stamp("write_rev_unresolvable", rev[:40])
        return 1 if strict else 3

    merged_info = {}
    merged_out = {}
    refused = []
    for name in FILES:
        base = _load(os.path.join(ROOT, name))
        if base is None:
            # 工作树这份就是待推的载荷：它坏了还推上去，等于把坏数据写进分支（审查实测过）。
            print("::error::[star-state] 工作树 %s 读不出可解析的 JSON ⇒ 并集没做完，不许提交" % name)
            _stamp("write_base_unreadable", name)
            refused.append(name)
            continue
        rc_ls, out_ls = _sh("ls-tree", rev, "--", name)
        if rc_ls != 0:
            print("::error::[star-state] ls-tree %s %s 失败（%s）⇒ 并集没做完，不许提交"
                  % (rev[:10], name, out_ls.strip()[:120]))
            _stamp("write_ls_tree_failed", name)
            refused.append(name)
            continue
        if not out_ls.strip():
            # 分支上确实没有这个文件（与"读失败"到这里已经分开）：没有要捞的键，保留工作树那份。
            print("[star-state] %s 不在父提交 %s 上 ⇒ 无键可捞，保留工作树那份" % (name, rev[:10]))
            merged_info[name] = ("kept", len(base), 0, len(base), 0)
            _stamp("write_rev_missing_file", name)
            continue
        raw = _show(rev, name)
        if raw is None:
            print("::error::[star-state] %s 在父提交上存在却读不出来 ⇒ 并集没做完，不许提交" % name)
            _stamp("write_show_failed", name)
            refused.append(name)
            continue
        try:
            incoming = json.loads(raw)
        except Exception:  # noqa: BLE01 — 分支上那份坏了：既不能拿坏数据覆盖，也不能当"没有"放行
            print("::error::[star-state] %s 在 %s 上不是合法 JSON ⇒ 并集没做完，不许提交"
                  % (name, rev[:10]))
            _stamp("write_incoming_unparseable", name)
            refused.append(name)
            continue
        if not isinstance(incoming, dict):
            print("::error::[star-state] %s 在 %s 上形状不是对象 ⇒ 并集没做完，不许提交"
                  % (name, rev[:10]))
            _stamp("write_incoming_bad_shape", name)
            refused.append(name)
            continue
        merged = dict(incoming)
        merged.update(base)          # 同键：本场（更新的分类结果）赢
        merged_out[name] = merged
        merged_info[name] = ("merged", len(merged), len(merged) - len(base), len(base), len(incoming))

    if refused:
        return 1 if strict else 3

    kc = merged_info.get("known_categories.json")
    if kc is None or kc[0] != "merged":
        # 地板闸门量不到 categories 就没资格放行：categories 没进并集结果的场次不该推上去。
        print("::error::[star-state] known_categories 没进入并集结果 ⇒ 并集没做完，不许提交")
        _stamp("write_categories_not_merged", str(sorted(merged_info)))
        return 1 if strict else 3
    if kc[1] < FLOOR_KEYS:
        print("::error::[star-state] 写端并集后 known_categories 只有 %d 键（地板 %d）——"
              "判定为异常状态，一个文件都不写" % (kc[1], FLOOR_KEYS))
        _stamp("write_floor_rejected", "merged=%d floor=%d" % (kc[1], FLOOR_KEYS))
        # 返回 3（不是 0）是刻意的：读端"不写"很安全（main 那份本来就在），写端"不写"却会一路
        # 走到"照样把没并过的 tree 推上去"——那正是本函数要防的缩小。调用方（star-fast.yml）
        # 见非零就放弃本场提交并出声；下一场重新 diff 会补上，数据不丢。
        return 3 if not strict else 1

    # 剪枝排在地板闸门**之后**：先验"并集没缩小"，再执行有意缩小。反过来会让一份垃圾名单
    # 把表剪到地板以下还照样推上去，而分支是唯一还存着历史键的地方。
    _prune_to_live(merged_out, _live_names())
    # 剪枝改动了键数 ⇒ 刷新计数，否则下面 print 的"→ N 键"是剪之前的，读日志的人会算错账
    for name, info in merged_info.items():
        if info[0] == "merged" and name in merged_out:
            merged_info[name] = ("merged", len(merged_out[name]), info[2], info[3], info[4])

    for name, info in merged_info.items():
        if info[0] != "merged":
            continue
        # 直接用上面已经并好的那份写，不再回头 `_show` 重取一次：重取会把"这一场已经判定可以推"
        # 的东西再交给一次 git/JSON 失败面（而这里任何中途失败都意味着推出去的是半并的 tree）。
        _atomic_write(os.path.join(ROOT, name), merged_out[name])
        print("[star-state] 写端并集 %s：工作树 %d 键 + 分支 %d 键 → %d 键（捞回 %d 个分支独有键）"
              % (name, info[3], info[4], info[1], info[2]))
        _stamp("write_merged", "%s local=%d branch=%d union=%d recovered=%d rev=%s"
               % (name, info[3], info[4], info[1], info[2], rev[:10]))
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    strict = "--strict" in argv       # --strict：任何降级都非零退出（判据用，CI 不用）

    # 写端入口（star-fast 车道）：与读端相反的方向——把 star-state 上已有的键并进**工作树**，
    # 让本场提交的 tree 至少不小于分支上一场。放在最前面，因为它不需要 ls-remote/fetch：
    # 调用方已经把父提交 fetch 到本地，直接给它 sha 就行。
    if "--merge-onto" in argv:
        return _merge_onto(argv[argv.index("--merge-onto") + 1], strict)

    rc, out = _sh("ls-remote", "--exit-code", "--heads", "origin", BRANCH)
    if rc != 0:
        print("::warning::[star-state] 远端无 %s 分支（还没启用或已删）——本场用 main 上的副本" % BRANCH)
        _stamp("branch_absent", "keep main copy")
        return 1 if strict else 0

    tip = out.split()[0]
    rc, err = _sh("fetch", "-q", "origin", "+refs/heads/%s:refs/remotes/origin/%s" % (BRANCH, BRANCH))
    if rc != 0:
        print("::warning::[star-state] fetch 失败：%s——本场用 main 上的副本" % err.strip()[:160])
        _stamp("fetch_failed", err.strip()[:160])
        return 1 if strict else 0

    merged_any = {}
    for name in FILES:
        base = _load(os.path.join(ROOT, name))
        if base is None:
            print("::warning::[star-state] 工作树 %s 读不出可解析的 JSON——跳过该文件（不动它）" % name)
            _stamp("base_unreadable", name)
            continue
        raw = _show("refs/remotes/%s/%s" % ("origin", BRANCH), name)
        if raw is None:
            print("::warning::[star-state] %s 不在 %s 上——保留 main 那份" % (name, BRANCH))
            merged_any[name] = ("kept", len(base))
            continue
        try:
            incoming = json.loads(raw)
        except Exception:  # noqa: BLE001
            print("::warning::[star-state] %s 在 %s 上不是合法 JSON——保留 main 那份" % (name, BRANCH))
            _stamp("incoming_unparseable", name)
            merged_any[name] = ("kept", len(base))
            continue
        if not isinstance(incoming, dict):
            print("::warning::[star-state] %s 形状不是对象（%s）——保留 main 那份" % (name, BRANCH, type(incoming).__name__))
            _stamp("incoming_bad_shape", name)
            merged_any[name] = ("kept", len(base))
            continue
        # ① 并集：incoming 覆盖同名键，base 独有的键一律保留 ⇒ 合并结果只会 ≥ 两边
        merged = dict(base)
        added = 0
        for k, v in incoming.items():
            if k not in merged:
                added += 1
            merged[k] = v
        merged_any[name] = ("merged", len(merged), added, len(base), len(incoming))

    # ② 地板值闸门：以 known_categories 的键数为准（它决定烧多少 LLM）
    kc = merged_any.get("known_categories.json")
    if kc and kc[0] == "merged" and kc[1] < FLOOR_KEYS:
        print("::error::[star-state] 合并后 known_categories 只有 %d 键（地板 %d）——"
              "判定为异常状态，一个文件都不写，本场退回 main 副本" % (kc[1], FLOOR_KEYS))
        _stamp("floor_rejected", "merged=%d floor=%d" % (kc[1], FLOOR_KEYS))
        return 1 if strict else 0

    for name, info in merged_any.items():
        if info[0] != "merged":
            continue
        # 重新读一次 base 做写回（上面只算了计数，值在 incoming/base 里；重读保证幂等与单一来源）
        base = _load(os.path.join(ROOT, name))
        raw = _show("origin/%s" % BRANCH, name)
        incoming = json.loads(raw)
        merged = dict(base)
        merged.update(incoming)
        _atomic_write(os.path.join(ROOT, name), merged)
        print("[star-state] %s：main %d 键 + 分支 %d 键 → 并集 %d 键（本场新增 %d）"
              % (name, info[3], info[4], info[1], info[2]))
        _stamp("merged", "%s base=%d branch=%d union=%d added=%d tip=%s"
               % (name, info[3], info[4], info[1], info[2], tip[:10]))

    if not merged_any:
        print("::warning::[star-state] 两个文件都没能合并（见上面的 warning）——本场用 main 副本")
    return 0


if __name__ == "__main__":
    sys.exit(main())
