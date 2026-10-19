# -*- coding: utf-8 -*-
"""按族淘汰 Actions 缓存旧键，回收配额（GitHub 免费版仓库缓存上限 10GB）。

为什么不是"改成稳定缓存键"：`tests/rss_history/test_history_cache.py::test_ci_save_history_cache_is_best_effort`
明确要求键里保留 `github.run_attempt`（重跑同 run 时键已存在会静默不保存），所以每场必然新增一键；
唯一能同时满足"不破坏既有语义"和"配额不再无界"的做法是**保存后按族淘汰最旧键**。

实测背景（09-20）：`actions/cache/usage` = 9.97 GiB / 10 GB、79 键，
其中 emb-cache 45 键 9.64 GB（每键 ~230MB）、rss-history 34 键仅 0.33 GB。
Save 步是 continue-on-error，配额满时会静默不保存 ⇒ 本步是悬崖前的**预防性**回收。
    ⚠ 审查实测：引入该步后 29/29 场成功 run 都有键，**尚未观察到失败**；
    真正紧迫的是剩余额度约 10 MB 而单个 emb 键均 219 MB（再一场就撞上）。

用法：
  py -3.11 tools/trim_actions_cache.py --keep emb-cache=2 --keep rss-history=5 --dry-run
  CI 里由 update.yml 在两个 Save 之后调用（continue-on-error，不连坐部署）。
"""
import argparse
import datetime
import io
import json
import os
import subprocess
import sys
import time
import urllib.request

ROOT_BYTES = 1024 ** 3
GB = 1073741824.0


def normalize_keeps(pairs):
    """把 ["emb-cache=2"] 变成 {"emb-cache": 2}；keep<=0 一律拒绝（打错一个数字就会清空整族缓存）。"""
    keeps = {}
    for p in pairs or []:
        if "=" not in p:
            raise ValueError("--keep 需要 family=N 形式，收到 %r" % p)
        fam, _, n = p.partition("=")
        fam = fam.strip()
        try:
            n = int(n)
        except ValueError:
            raise ValueError("--keep 的 N 必须是整数，收到 %r" % p)
        if not fam:
            raise ValueError("--keep 缺族名：%r" % p)
        if n <= 0:
            raise ValueError("--keep %s=%d 被拒绝：保留 0 条会让下一次构建必然冷启动" % (fam, n))
        keeps[fam] = n
    if not keeps:
        raise ValueError("至少要给一个 --keep family=N，否则本脚本无事可做（宁可不跑，也不要误删）")
    return keeps


def _ts(item):
    for k in ("created_at", "last_accessed_at"):
        v = item.get(k)
        if v:
            try:
                return datetime.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
            except ValueError:
                continue
    return datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)


def _family_of(key, families):
    """前缀匹配到某个族；匹配不上一律返回 None（白名单外不删）。"""
    best = None
    for fam in families:
        if key == fam or key.startswith(fam + "-"):
            if best is None or len(fam) > len(best):
                best = fam
    return best


def plan_deletions(caches, keeps, run_id):
    """每个族按时间倒序保留最新 N 条，返回要删的键（最旧的先删）。

    自杀守卫：键里带本场 run_id 的一律不删 —— 否则刚 Save 的历史/向量被自己删掉，
    下一场 restore 必然冷启动（这比不回收更糟）。
    """
    run_id = str(run_id or "")
    by_fam = {}
    for c in caches:
        fam = _family_of(c.get("key", ""), keeps.keys())
        if fam is None:
            continue
        by_fam.setdefault(fam, []).append(c)
    out = []
    for fam, items in by_fam.items():
        items.sort(key=lambda c: (_ts(c), int(c.get("id") or 0)), reverse=True)
        keep = keeps[fam]
        for idx, c in enumerate(items):
            if idx < keep:
                continue
            if run_id and run_id in c.get("key", ""):
                continue
            out.append(c)
    out.sort(key=lambda c: (_ts(c), int(c.get("id") or 0)))
    return out


def apply_deletions(items, delete):
    """逐条删；单条失败不连坐也不静默吞（返回 failed 供上层判退出码）。"""
    deleted, failed = [], []
    for c in items:
        cid = c.get("id")
        try:
            if delete(cid):
                deleted.append(cid)
            else:
                failed.append(cid)
        except Exception as exc:                      # 403/404/网络抖动都归到 failed
            failed.append(cid)
            print("  [NG] 删除 %s (%s) 失败：%s" % (cid, c.get("key"), exc), file=sys.stderr)
    return {"deleted": deleted, "failed": failed}


def _token():
    """CI 里用作业自带的 GITHUB_TOKEN；本机开发回退到 `gh auth token`。

    为什么不走 `gh api`：update.yml 现在完全没用过 gh，若 runner 上取不到它，
    本步会因 continue-on-error 而**静默空转**（配额照样满）—— 这正是必须避免的失败形态。
    """
    t = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    if t.strip():
        return t.strip()
    try:
        return subprocess.check_output(["gh", "auth", "token"]).decode().strip()
    except Exception:
        return ""


def _api(method, path, tries=4):
    tok = _token()
    if not tok:
        raise RuntimeError("没有可用 token（CI 需传 secrets.GITHUB_TOKEN，本机需 gh 已登录）")
    last = None
    for k in range(tries):
        try:
            r = urllib.request.Request("https://api.github.com/" + path, method=method,
                                       headers={"Authorization": "Bearer " + tok,
                                                "Accept": "application/vnd.github+json",
                                                "X-GitHub-Api-Version": "2022-11-28"})
            with urllib.request.urlopen(r, None, timeout=60) as f:
                body = f.read().decode()
            return json.loads(body) if body.strip() else {}
        except Exception as exc:
            last = exc
            time.sleep(2 + 2 * k)
    raise last


def list_caches(repo):
    """分页拉全部缓存键。total_count 与本页条数不符时会告警——漏页就等于漏删，配额照样满。"""
    out, page, total = [], 1, None
    while True:
        d = _api("GET", "repos/%s/actions/caches?per_page=100&page=%d" % (repo, page))
        page_items = d.get("actions_caches", [])
        total = d.get("total_count", 0) if total is None else total
        out.extend(page_items)
        if not page_items or len(out) >= int(total or 0):
            break
        page += 1
        if page > 40:
            print("  [NG] 翻页超过 40 页即停（异常分页），可能仍有键未纳入淘汰计划", file=sys.stderr)
            break
    if total is not None and len(out) < int(total):
        print("  [NG] 只取到 %d/%s 条键，剩余未参与淘汰" % (len(out), total), file=sys.stderr)
    return out


def make_deleter(repo):
    def _delete(cid):
        _api("DELETE", "repos/%s/actions/caches/%s" % (repo, cid))
        return True
    return _delete


def guarded_list_caches(lister, repo):
    """列键失败时留下 annotation 再返回 None。

    原实现在 LIST 就 403 时直接抛异常，于是"空转"告警恰恰在它自己点名的那类权限故障下失效。
    """
    try:
        return lister(repo)
    except Exception as exc:
        # annotation 必须走 stdout：GitHub 的 workflow command 只从标准输出解析
        print("::warning title=Actions 缓存淘汰失败::读取缓存键列表失败（多为令牌缺 actions: write，API 返回 403）"
              "：%s —— 本次未回收任何配额" % exc)
        return None


def usage_bytes(repo):
    try:
        d = _api("GET", "repos/%s/actions/cache/usage" % repo)
        return int(d.get("active_caches_size_in_bytes") or 0)
    except Exception:
        return -1


def summary_line(items, before_bytes, failed):
    """汇总行。单独成函数并被测试覆盖，是因为它原先把 `TypeError: not enough arguments for format string`
    抛在**删除动作之后** —— CI 里被 continue-on-error 吞掉，就成了"删了 9GB 但没人知道删了多少"。"""
    freed = sum(int(c.get("size_in_bytes") or 0) for c in items)
    parts = ["%d 条" % len(items), "回收 %.2f GB" % (freed / GB)]
    if before_bytes > 0:
        parts.append("当前 %.2f GB → 回收后 %.2f GB" % (before_bytes / GB, (before_bytes - freed) / GB))
    else:
        parts.append("当前用量未知")
    parts.append("删除失败 %d 条" % len(failed))
    return "；".join(parts)


def report(res, before_bytes=0, dry_run=False):
    """打印结果并在"有计划却一条都没删成"时留 ::warning annotation。

    为什么必须有：这步是 continue-on-error，令牌缺 `actions: write` 时全部 403 也只会被静默吞掉，
    配额照旧满、72h 历史照旧退化，而构建仍是绿的 —— 正是"空转"。
    返回状态串（空串表示正常）。
    """
    items = res.get("would_delete") or []
    deleted = res.get("deleted") or []
    failed = res.get("failed") or []
    print("%s：%s" % ("待删(dry-run)" if dry_run else "已删", summary_line(items, before_bytes, failed)))
    # dry-run 本来就不动手，"一条没删"不是失败 —— 否则每次试算都会误报权限问题
    if items and not deleted and not dry_run:
        status = "整批删除失败（最常见是令牌缺 actions: write 权限，API 返回 403）"
        print("::warning title=Actions 缓存淘汰失败::%s —— 配额不会被回收，"
              "72h 历史与向量缓存会持续退化，需要人工修 permissions" % status)
        return status
    return ""


def run(caches, keeps, run_id, delete, dry_run=False):
    items = plan_deletions(caches, keeps, run_id)
    if dry_run:
        return {"deleted": [], "failed": [], "would_delete": items}
    res = apply_deletions(items, delete)
    res["would_delete"] = items
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default="Kwei168/starhub")
    ap.add_argument("--keep", action="append", default=[], help="family=N，可重复；未列出的族不动")
    ap.add_argument("--run-id", default="", help="本场 run_id（键里含它的永不删除）")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    keeps = normalize_keeps(a.keep)

    caches = guarded_list_caches(list_caches, a.repo)
    if caches is None:
        return 1
    before = usage_bytes(a.repo)
    res = run(caches, keeps, a.run_id, make_deleter(a.repo), dry_run=a.dry_run)
    items = res["would_delete"]
    for c in items[:12]:
        print("  %-46s %6.0f MB  %s" % (c.get("key", "")[:46],
                                        int(c.get("size_in_bytes") or 0) / 1048576,
                                        str(c.get("created_at"))[:16]))
    if len(items) > 12:
        print("  … 共 %d 条" % len(items))
    status = report(res, before_bytes=before, dry_run=a.dry_run)
    if status:
        return 1
    if res["failed"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.exit(main())
