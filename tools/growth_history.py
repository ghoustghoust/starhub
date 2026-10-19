# -*- coding: utf-8 -*-
"""Read the per-build growth events back out of build_logs (offline first, remote fallback).

Companion to history_growth.py: that one measures a single build and appends a `growth` line;
this one answers "is it still growing" from the repository itself instead of scraping Actions logs
(which expire). Read-only.

Timeliness caveat (measured, not guessed): the growth step runs AFTER the commit step, so a build's
own line can only reach git at the next day-boundary commit -- build_logs is committed once a day,
while the appended lines do ride along in the starhub-state cache. So `--dir build_logs` inside CI
sees today, while the remote fallback is T+1. Print it, don't silently average it.
"""
import argparse
import json
import os
import statistics
import sys
import urllib.error
import urllib.request

RAW = "https://raw.githubusercontent.com/Kwei168/starhub/main/build_logs/%s.jsonl"


def fetch_remote(day):
    """取远端某天日志的原文。

    这里**不**吞异常：403 / 超时 / 被墙 与「这一天确实没有日志」是两回事，
    把它们都返回成空串就等于把「没取到」读成「没长」——本工具回答的是否题，容不下这种假绿。
    404 的翻译与异常的归类统一放在 `_fetch_text`，别在两个地方各写一遍 404。
    """
    with urllib.request.urlopen(RAW % day, timeout=60) as fh:
        return fh.read().decode("utf-8", "replace")


def _fetch_text(day, fetch):
    """404 ⇒ 空串（那天没日志）；其它异常原样抛给调用方去说「取数失败」。"""
    try:
        return fetch(day)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return ""
        raise


def _events_from_text(text):
    out = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue                      # 半截行：跳过，不让一行坏数据毁掉整份读数
        if isinstance(rec, dict) and rec.get("type") == "growth":
            # 读数缺失/为 null 的行**不算事件**：`summarize` 里 `or 0` 会把它当成"这场没长"，
            # 于是一行坏数据就能把中位数拖向 0，让"曲线是平的"变成假话。0 是合法读数
            # （相邻提交没新增 blob 时就是 0），但 null / 字符串 / bool 不是。
            nb = rec.get("new_bytes")
            if isinstance(nb, int) and not isinstance(nb, bool):
                out.append(rec)
    return out


def iter_growth_events(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return iter(())
    return iter(_events_from_text(text))


def day_reading(day, local_dir="build_logs", fetch=None):
    """返回 `(events, note)`：note 必须说清这份读数是从哪来的、缺在哪。

    三种"空"长得一样但意思完全不同，混起来就是把「没看到」读成「没长」：
    ① 本地有文件、有 growth ⇒ 直接用本地（不白跑网络）；
    ② 本地有文件、0 条 growth ⇒ **必须再问远端**：这条工作树的 .git 是浅取副本，
       远端新写的行不会出现在本地，把本地的空当结论就是假绿；
    ③ 远端 404 ⇒ 那天确实没有日志；远端非 404 异常 ⇒ 取数失败，读数缺失。
    """
    fetch = fetch or fetch_remote
    p = os.path.join(local_dir or "", "%s.jsonl" % day)
    if local_dir and os.path.exists(p):
        local = list(iter_growth_events(p))
        if local:
            return local, "本地"
        try:
            remote = _events_from_text(_fetch_text(day, fetch))
        except Exception as e:
            return local, "取数失败（%s）⇒ 本地副本无 growth 且无法复核，读数缺失不代表没长" % type(e).__name__
        if remote:
            return remote, "本地副本陈旧：本地 0 条、远端 %d 条" % len(remote)
        return local, "本地与远端都无 growth 事件"
    try:
        text = _fetch_text(day, fetch)
    except Exception as e:
        return [], "取数失败（%s）⇒ 读数缺失，不代表没长" % type(e).__name__
    if not text:
        return [], "无该日日志"
    return _events_from_text(text), "远端"


def growth_events_for_day(day, local_dir="build_logs", fetch=None):
    """兼容旧签名：只要事件，口径说明走 `day_reading`。"""
    return day_reading(day, local_dir=local_dir, fetch=fetch)[0]


def summarize(events):
    events = list(events)
    if not events:
        return {"total_builds": 0, "days": [], "max_bytes": None, "median_bytes": None,
                "note": "没有 growth 事件 —— 这不代表曲线是平的，只代表还没测到"}
    by_day = {}
    for e in events:
        day = str(e.get("ts", ""))[:10]
        by_day.setdefault(day, []).append(int(e.get("new_bytes") or 0))
    sizes = [int(e.get("new_bytes") or 0) for e in events]
    days = [{"date": d, "builds": len(v), "sum_bytes": sum(v), "max_bytes": max(v),
             "mean_bytes": int(round(sum(v) / len(v)))} for d, v in sorted(by_day.items())]
    return {"total_builds": len(events),
            "total_bytes": sum(sizes),
            "max_bytes": max(sizes),
            "median_bytes": int(statistics.median(sizes)),
            "mean_bytes": int(round(sum(sizes) / len(sizes))),
            "days": days}


def recent_days(n=7):
    from datetime import date, timedelta
    today = date.today()
    return [(today - timedelta(days=i)).isoformat() for i in range(n)]


def main(fetch=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="build_logs")
    ap.add_argument("--days", type=int, default=7, help="回看几天（本地缺的日期走远端）")
    a = ap.parse_args()
    events, notes = [], []
    for day in recent_days(a.days):
        evs, note = day_reading(day, local_dir=a.dir, fetch=fetch)
        events.extend(evs)
        notes.append((day, note))
    failed = [d for d, n in notes if "取数失败" in n]
    stale = [d for d, n in notes if "陈旧" in n]
    s = summarize(events)
    if not s["total_builds"]:
        print(s.get("note", "没有 growth 事件"))
    else:
        print("growth 事件 %d 场｜中位 %.3f MiB｜均值 %.3f MiB｜最坏 %.3f MiB" % (
            s["total_builds"], s["median_bytes"] / 1048576, s["mean_bytes"] / 1048576,
            s["max_bytes"] / 1048576))
        print("最坏那一场通常是日界那次日志入库，不是每场重写")
        for d in s["days"]:
            print("  %-12s builds=%-3d 合计 %8.3f MiB  最大单场 %8.3f MiB" % (
                d["date"], d["builds"], d["sum_bytes"] / 1048576, d["max_bytes"] / 1048576))
    # 故障与"没长"必须分开口径播：空读数如果和断网同框，这道监测就只剩安慰作用。
    if failed:
        print("⚠ %d 天取数失败（%s）⇒ 这些天的读数缺失，不代表没长" % (
            len(failed), ", ".join(failed[:3])))
    if stale:
        print("已用远端纠正本地陈旧副本：%d 天（%s）" % (len(stale), ", ".join(stale[:3])))
    print("口径提醒：从 git 读是 T+1（growth 步跑在 Commit 之后，当天的行要等下一次日界提交）；"
          "在 CI 里直接读 build_logs/ 才是当天。")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
