# -*- coding: utf-8 -*-
"""变异测试：验证测试用例是否真的具备鉴别力。

做法：把每一处关键守卫逐个改坏（在**副本**上，不改真实源码），
跑测试，断言「至少有一个预期用例变红」。若改坏后测试仍全绿 → 该行无人守卫。

用法：python tests/rss_sort/mutation_check.py
"""
import io
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC = os.path.join(ROOT, "build_rss_aggregator.py")
TMP = os.path.join(ROOT, ".deploy-tmp", "_mut")
PY = r"C:\Users\40832\.workbuddy\binaries\python\versions\3.13.12\python.exe"

# (名称, 原文, 替换为, 期望变红的用例前缀)
MUTATIONS = [
    ("M1 丢掉 tx===ty 守卫",
     "    if(tx===ty) return 0;\n",
     "",
     ["T10 相等日期比较返回 0", "T10 等价时区表示返回 0"]),
    ("M2 重演原始缺陷（newest 反向调用 _dateCmp）",
     "    else ART.sort(function(a,b){ return _dateCmpDesc(a.date,b.date); });",
     "    else ART.sort(function(a,b){ return _dateCmp(b.date,a.date); });",
     ["T1 无日期沉底", "T3 无日期保持输入顺序"]),
    ("M3 丢掉 quality 例外",
     "if(sortMode!=='quality' && _head>0 && _bad*2>_head)",
     "if(_head>0 && _bad*2>_head)",
     ["T9 quality 模式零重渲染"]),
    ("M4 active 分支参数反转（我曾犯的错）",
     "        if(sa!==sb) return _dateCmpDesc(sa,sb);",
     "        if(sa!==sb) return _dateCmpDesc(sb,sa);",
     ["T5 active 首位必须有日期"]),
    ("M5 刷新路径第二处排序回退字符串比较",
     "\n      applySort();\n",
     "\n      ART.sort(function(a,b){ return (b.date||'').localeCompare(a.date||''); });\n",
     ["T7 02:00Z(=10:00+08) 应排在 01:00Z(=09:00+08) 之前"]),
    ("M6 钳制退回「所有未来条目折叠成同一时刻」（首屏同刻扎堆的成因）",
     "        a.date=new Date(nowMs-(_fmax-new Date(a.date).getTime())).toISOString();",
     "        a.date=new Date(nowMs).toISOString();",
     ["T13a 两条未来条目不得同刻（旧实现此处相等）", "T13b 保持原始先后顺序"]),
]

# Python 侧变异：(名称, 原文, 替换为, 用例文件, 期望变红的用例名)
PY_MUTATIONS = [
    ("P1 解析器丢弃时区（把 +0800 当 UTC）",
     'tzinfo=datetime.timezone(_rss_tz_offset(m.group("tz")))',
     "tzinfo=datetime.timezone(datetime.timedelta(0))",
     "tests/rss_date/test_parse_rss_date.py",
     ["A1 JS Date.toString() + GMT+0800 + 括号时区名", "A4a 数字时区 +0800"]),
    ("P2 取不到日期时不留降级键（回到「空日期」）",
     "            _fb = _fallback_date_iso(entry)\n",
     '            _fb = ""\n',
     "tests/rss_date/test_stale_history.py",
     ["C2 标记为降级（前端显示「收录」）", "C3 降级键非空 → 可参与时间排序"]),
    ("P3 切块选片退回字符串比较（时区盲）",
     "    flat.sort(key=lambda x: _chrono_key(x[1]), reverse=True)",
     '    flat.sort(key=lambda x: x[1].get("pub_date") or "", reverse=True)',
     "tests/rss_date/test_split_chunks.py",
     ["A1 混合时区按绝对时间选片（字符串序会错选 plus8）"]),
    ("P4 不回写真实日期 → 陈旧条目靠 first_seen 续命",
     "                _old = _rss_history.get(link)\n                if _old is not None:\n                    _old[\"pub_date\"] = pd_str\n",
     "",
     "tests/rss_date/test_stale_history.py",
     ["A1 陈旧条目已从历史中清除"]),
    ("P5 输出阶段不应用时区校正（声明了也没用）",
     "                _shifted = _shift_iso_minutes(entry[\"pub_date\"], _off)",
     "                _shifted = entry[\"pub_date\"]",
     "tests/rss_date/test_tz_correction.py",
     ["C4 校正后没有任何条目晚于 first_seen（物理可行）",
      "C6 输出时间 == 原始时间前移 480min（校正值精确）"]),
    ("P6 审计退回原始 pub_date（时区问题被误判成内容不可信）",
     "        pd = _effective_pub_dt(item, _pd_offsets)",
     "        pd = _parse_hist_dt(item.get(\"pub_date\"))",
     "tests/rss_date/test_tz_correction.py",
     ["C3 校正后不再被标不可信（审计跑在校正值上）"]),
    ("P7 丢掉未来日期判伪分支（错误日期照旧参与排序）",
     "            if not _fb and _pub_date_falsified(entry, _pd_offsets):",
     "            if False:",
     "tests/rss_date/test_tz_correction.py",
     ["C8 已被证伪的日期 → 标记降级"]),
    ("P8 发现器丢掉倒挂率门槛（逢源就告警）",
     "        if ratio < TZ_ANOMALY_INVERT_RATIO:",
     "        if False:",
     "tests/rss_date/test_tz_correction.py",
     ["A5 宽阔带（倒挂率 50%，arxiv/播客形态）→ 不报"]),
    ("P9 时区平移方向写反",
     "    return (d + datetime.timedelta(minutes=minutes)).isoformat()",
     "    return (d - datetime.timedelta(minutes=minutes)).isoformat()",
     "tests/rss_date/test_tz_correction.py",
     ["A3 -480min 把被标成 UTC 的北京时间还原为真实发布时间",
      "C6 输出时间 == 原始时间前移 480min（校正值精确）"]),
    ("P10 判伪退回「整批写同一个 first_seen」（同批次折叠成同一时刻 → 重演首屏扎堆）",
     '                _shift = _falsify_shifts.get(item.get("link"), 0)\n',
     '                _shift = 0\n',
     "tests/rss_date/test_tz_correction.py",
     ["D3 回拉后 5 条时刻互不相同（不折叠为同一时刻）",
      "D5 组内最早的一条 = 最新锚点 - 组内最大间隔（7h）",
      "D6 组内相对先后被保留（与 feed 声称的时间顺序一致）"]),
    ("P11 回拉方向写反（往未来推）",
     "            shifts[link] = -int(round(newest_adv - adv))",
     "            shifts[link] = int(round(newest_adv - adv))",
     "tests/rss_date/test_tz_correction.py",
     ["D4 组内最新的一条恰好锚在 first_seen",
      "D7 回拉后没有任何一条晚于 first_seen + 容忍阈值"]),
    ("P12 锚点取组内最旧那条（而非最新）→ 最新者仍留在未来",
     "        newest_adv = max(a for _l, a in arr)",
     "        newest_adv = min(a for _l, a in arr)",
     "tests/rss_date/test_tz_correction.py",
     ["D4 组内最新的一条恰好锚在 first_seen",
      "D7 回拉后没有任何一条晚于 first_seen + 容忍阈值"]),
    ("P13 丢掉 72h 窗口下限保护（回拉把条目推出窗口）",
     "                if _fbd is None or _fbd < _cutoff_aware:",
     "                if False:",
     "tests/rss_date/test_tz_correction.py",
     ["D13 回拉下限被钳在 72h 窗口上（不会把条目推出窗口）"]),
]


def run(src_path, harness_name):
    env = dict(os.environ)
    env["RSS_BUILD_SRC"] = src_path
    env["RSS_HARNESS_NAME"] = harness_name
    p = subprocess.run([PY, os.path.join(ROOT, "tests", "rss_sort", "test_rss_sort.py")],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)
    return p.stdout or ""


def run_py(src_path, test_rel):
    env = dict(os.environ)
    env["RSS_BUILD_SRC"] = src_path
    env["PYTHONIOENCODING"] = "utf-8"
    p = subprocess.run([PY, os.path.join(ROOT, test_rel.replace("/", os.sep))],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)
    return (p.stdout or "") + (p.stderr or "")


def main():
    os.makedirs(TMP, exist_ok=True)
    with open(SRC, "r", encoding="utf-8") as f:
        base = f.read()

    print("基线：未变异源码")
    out = run(SRC, "_harness.js")
    if "0 failed" not in out:
        print("基线就红了，终止。\n" + out)
        return 2
    print("  基线全绿 ✓\n")

    caught = 0
    total = len(MUTATIONS) + len(PY_MUTATIONS)
    for i, (name, old, new, expect) in enumerate(MUTATIONS, 1):
        if old not in base:
            print("[跳过] %s —— 未找到变异点（锚点已变更）" % name)
            continue
        mut_src = os.path.join(TMP, "mut%d.py" % i)
        with open(mut_src, "w", encoding="utf-8") as f:
            f.write(base.replace(old, new, 1))
        out = run(mut_src, "_mut_harness%d.js" % i)
        caught_any = [e for e in expect if ("FAIL " + e) in out]
        status = "CAUGHT" if caught_any else "SURVIVED  ← 无人守卫"
        if caught_any:
            caught += 1
        print("[%s] %s" % (status, name))
        print("        期望变红的用例: %s" % (caught_any or "（无）"))
        if "RESULT:" in out:
            print("        %s" % [l for l in out.splitlines() if l.startswith("RESULT:")][0])

    for j, (name, old, new, test_rel, expect) in enumerate(PY_MUTATIONS, 1):
        if old not in base:
            print("[跳过] %s —— 未找到变异点（锚点已变更）" % name)
            continue
        mut_src = os.path.join(TMP, "mutpy%d.py" % j)
        with open(mut_src, "w", encoding="utf-8") as f:
            f.write(base.replace(old, new, 1))
        out = run_py(mut_src, test_rel)
        caught_any = [e for e in expect if ("[FAIL] " + e) in out]
        status = "CAUGHT" if caught_any else "SURVIVED  ← 无人守卫"
        if caught_any:
            caught += 1
        print("[%s] %s" % (status, name))
        print("        期望变红的用例: %s" % (caught_any or "（无）"))
        line = [l for l in out.splitlines() if l.startswith("RESULT:")]
        if line:
            print("        %s" % line[0])

    print("\n变异检出率: %d/%d" % (caught, total))
    shutil.rmtree(TMP, ignore_errors=True)
    return 0 if caught == total else 1


if __name__ == "__main__":
    sys.exit(main())
