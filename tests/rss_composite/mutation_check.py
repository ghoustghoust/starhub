# tests/rss_composite/mutation_check.py
# -*- coding: utf-8 -*-
"""变异测试（对抗性审查）：验证 C1 / C2 / M5 三条修复的守卫是否真的具备鉴别力。

做法：把每一处关键守卫逐个改坏（在**副本**上，不动真实源码），跑对应套件，
断言「至少有一个预期用例变红」。改坏后仍全绿 → 该行无人守卫（变异存活）。

前轮教训（本文件存在的直接原因）：
  C1 的首版 E 段用例用「5 源 × 6 篇」均衡夹具，去掉同源抑制后**仍然全绿**
  —— 测试写了等于没写。本文件把「变异存活」变成显式失败，
  以后任何守卫被削弱都会被立刻发现。

用法：python tests/rss_composite/mutation_check.py
"""
import io
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC = os.path.join(ROOT, "build_rss_aggregator.py")
TMP = os.path.join(ROOT, ".deploy-tmp", "_mut_composite")
PY = r"C:\Users\40832\.workbuddy\binaries\python\versions\3.13.12\python.exe"

JS_DIVERSE = "tests/rss_composite/test_diverse_js.py"
PY_TAG = "tests/rss_composite/test_tag_semantics.py"
JS_TAGS = "tests/rss_composite/test_refresh_tags_js.py"
PY_SNAP = "tests/rss_composite/test_snapshot_tags.py"
PY_SCORE = "tests/rss_composite/test_score_sources_default.py"
PY_DATE_UNIFORM = "tests/rss_composite/test_date_uniform.py"

# (名称, [(原文, 替换为), ...], 测试文件, 期望变红的用例名片段, 输出标记)
# 多点变异用列表表达：有些守卫是「双保险」（同一契约在两处实现），
# 只改一处时另一处仍然兜住 → 变异存活，会误判成「无人守卫」。
MUTATIONS = [
    # ── C1：同源抑制（weightedShuffle）────────────────────────────────────
    ("C1-a 同源抑制过滤失效（重演「打散零达成」）",
     [("if (_srcGap(lastAt, result.length, remaining[k].sk) > SRC_GAP) pool.push(remaining[k]);",
       "pool.push(remaining[k]);")],
     JS_DIVERSE, ["E1 同源最长连续 ≤2", "E5a diverse 模式同源最长连续 ≤2", "E8b 连出长度 ≤8"], "FAIL "),

    ("C1-b 抑制间隔由 2 位放宽为 1 位（同源可隔 1 位重复）",
     [("  var SRC_GAP = 2;", "  var SRC_GAP = 1;")],
     JS_DIVERSE, ["E9 同源最小间隔 ≥3"], "FAIL "),

    ("C1-c 池枯竭时反向选源（挑刚用过的源而非最久未用的）",
     [("            if (gk > maxGap) maxGap = gk;",
       "            if (gk < maxGap || maxGap < 0) maxGap = gk;")],
     JS_DIVERSE, ["E10b 双源严格交替"], "FAIL "),

    # ── C2：标签语义提取（_tag_articles）──────────────────────────────────
    ("C2-a 摘要也切中文段（碎片化根因回归）",
     [("        for pool, allow_runs in ((title, True), (summary, False)):",
       "        for pool, allow_runs in ((title, True), (summary, True)):")],
     PY_TAG, ["E7a 摘要不切中文段", "E7d allow_runs=False 时不切中文段"], "[FAIL] "),

    ("C2-b 词典优先失效（P1 不再产出候选）",
     [("        if _tag_ok(w, src_norm, low):\n            picks.append(w)\n",
       "        if False:\n            picks.append(w)\n")],
     PY_TAG, ["B1a 词典术语 '人工智能' 整词命中", "B3 高频词典术语排首位", "F5 词典术语子串率"], "[FAIL] "),

    ("C2-c 台标抑制失效（源名自指 / 源内高频台标照旧成标签）",
     [("    if _is_tag_station(tok, src_norm):\n        return False\n",
       "    if False:\n        return False\n"),
      ("        if _is_tag_station(w, src_norm):\n            continue\n",
       "        if False:\n            continue\n"),
      ("            boiler = set(t for t, c in df.items() if c >= need)",
       "            boiler = set()")],
     PY_TAG, ["D1 源名自指台标", "D2 源内高频台标"], "[FAIL] "),

    ("C2-d 边界虚词校验失效（'的星尘' 类切片放行）",
     [("    if not _tag_edge_ok(tok) or _is_numeric_unit_phrase(tok):",
       "    if False:")],
     PY_TAG, ["C2 标签首/尾均非虚词功能字", "F3 边界违例率"], "[FAIL] "),

    ("C2-e 中文段长度上界整体失效（含 P3 取词上界）",
     [("    if not tok.isascii() and len(tok) > _TAG_MAX_LEN:",
       "    if False:"),
      ("            if has_any and (L > _TAG_MAX_LEN or (eff and max(len(d) for d in eff) >= L - 1)):",
       "            if False:"),
      ("            if 4 <= L <= _TAG_MAX_LEN and _tag_ok(run, src_norm, low):",
       "            if 4 <= L and _tag_ok(run, src_norm, low):")],
     PY_TAG, ["C5 中文标签长度 ≤", "F3 边界违例率"], "[FAIL] "),

    # ── M5：tags 三通道穿透 ───────────────────────────────────────────────
    ("M5-a 构建期通道 buildArt 丢掉 tags",
     [(", dfb:!!it.date_fallback, tags:_tagsOf(it)});",
       ", dfb:!!it.date_fallback});")],
     JS_TAGS, ["M5-1b tags 穿透 buildArt"], "FAIL "),

    ("M5-b 刷新通道 _mergeRemoteSources 丢掉 tags",
     [(", dfb:!!it.date_fallback, tags:_tagsOf(it)};",
       ", dfb:!!it.date_fallback};")],
     JS_TAGS, ["M5-4c tags 穿透刷新通道"], "FAIL "),

    ("M5-c 快照通道 _save_api_snapshot 丢掉 tags",
     [("            _tags = it.get(\"tags\")\n            if _tags:\n                item[\"tags\"] = _tags\n",
       "")],
     PY_SNAP, ["A2 带 tags 的条目：tags 穿透到快照", "A6 有 tags 的条目含 tags 键"], "[FAIL] "),

    ("M5-d 快照调用被提到打标之前（顺序回退）",
     [("        _tag_articles(sources_with_items)",
       "        _save_api_snapshot(sources_with_items, meta=meta)\n        _tag_articles(sources_with_items)")],
     PY_SNAP, ["B4 _tag_articles() 必须先于 _save_api_snapshot()"], "[FAIL] "),

    # ── RC：_applyRunCap 同源抑制（Phase 1）────────────────────────────────
    ("RC-a active 模式 _applyRunCap 调用移除",
     [("      _applyRunCap(ART, 3, SOURCE_FAMILIES);\n    }\n    /* 集成契约",
       "    }\n    /* 集成契约")],
     JS_DIVERSE, ["D18 active 模式 maxRun"], "FAIL "),

    ("RC-b newest 模式 _applyRunCap 调用移除",
     [("      _applyRunCap(ART, 3, SOURCE_FAMILIES);\n    }\n  }", "      /*removed*/\n    }\n  }")],
     JS_DIVERSE, ["D24 newest"], "FAIL "),

    # ── TM：_srcWeight tier 乘子（Phase 2）────────────────────────────────
    ("TM-a tier 乘子退化为恒 1.0（T1/T2 优势消失）",
     [("  var TIER_MULT = { 1: 1.5, 2: 1.2 };",
       "  var TIER_MULT = { 1: 1.0, 2: 1.0 };")],
     JS_DIVERSE, ["D20c", "D20d", "D20e"], "FAIL "),

    # ── TI：tierInterleave diverse 守卫（Phase 2）─────────────────────────
    # 注：tierInterleave 只在 _mergeChunk/_mergeRemoteSources 后调用，
    # 单元测试 harness 不触发合并流程，故无法通过单测变异验证。
    # 该守卫由集成测试（线上页面实测）覆盖，不纳入变异测试。

    # ── SS：_score_sources tier 默认分（Phase 2）──────────────────────────
    ("SS-a 无历史源回退为 0 分（505 源 quality=0 复现）",
     [("            _tier = src.get('tier', 3)\n            _default = {1: 60, 2: 40}.get(_tier, 25)",
       "            _tier = src.get('tier', 3)\n            _default = 0")],
     PY_SCORE,
     ["SS-1 T1 源无历史", "SS-2 T2 源无历史", "SS-3 T3 源无历史"], "[PASS] "),

    # ── DU：同日期伪造检测（Phase 3）────────────────────────────────
    ("DU-a 同日期检测阈值被放宽到 100%（80% 源漏检）",
     [("        if ratio >= threshold:",
       "        if ratio >= 1.0001:")],
     PY_DATE_UNIFORM,
     ["DU-7"], "FAIL "),

    # ── FC：族群配额（Phase 3）──────────────────────────────────────
    ("FC-a SOURCE_FAMILIES 为空对象（族群 cap 失效）",
     [("  var SOURCE_FAMILIES = {\n    'v2ex_all_50': 'v2ex',",
       "  var SOURCE_FAMILIES = {\n    /* removed */")],
     JS_DIVERSE, ["F1"], "FAIL "),

    ("FC-b _applyRunCap 不接收 families 参数",
     [("  function _applyRunCap(arr, cap, families) {",
       "  function _applyRunCap(arr, cap) {")],
     JS_DIVERSE, ["F2"], "FAIL "),
]


def run_suite(src_path, test_rel):
    env = dict(os.environ)
    env["RSS_BUILD_SRC"] = src_path
    env["PYTHONIOENCODING"] = "utf-8"
    p = subprocess.run([PY, os.path.join(ROOT, test_rel.replace("/", os.sep))],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", env=env)
    return (p.stdout or "") + (p.stderr or "")


def main():
    os.makedirs(TMP, exist_ok=True)
    src = io.open(SRC, encoding="utf-8").read()

    base_src = os.path.join(TMP, "baseline.py")
    io.open(base_src, "w", encoding="utf-8").write(src)
    print("基线：未变异源码")
    for rel in (JS_DIVERSE, PY_TAG, JS_TAGS, PY_SNAP, PY_SCORE, PY_DATE_UNIFORM):
        out = run_suite(base_src, rel)
        red = ("failed" in out and ", 0 failed" not in out)
        print("  %-46s %s" % (rel, "红 ← 基线就红了，终止" if red else "全绿"))
        if red:
            return 2
    print()

    caught = 0
    skipped = 0
    for i, (name, edits, test_rel, expect, marker) in enumerate(MUTATIONS, 1):
        # 全部编辑点都必须命中，否则跳过（锚点漂移时宁可显式跳过，也不要静默半变异）
        missing = [old for old, _new in edits if old not in src]
        if missing:
            print("[跳过] %s —— %d/%d 个变异点未命中（锚点已变更）"
                  % (name, len(missing), len(edits)))
            for m in missing:
                print("        缺: %r" % (m[:80],))
            skipped += 1
            continue
        mutated = src
        for old, new in edits:
            mutated = mutated.replace(old, new, 1)
        mut_src = os.path.join(TMP, "mut%02d.py" % i)
        io.open(mut_src, "w", encoding="utf-8").write(mutated)
        out = run_suite(mut_src, test_rel)
        hits = [e for e in expect if (marker + e) in out]
        if hits:
            caught += 1
        print("[%s] %s" % ("CAUGHT" if hits else "SURVIVED  ← 无人守卫", name))
        print("        变异点 %d 处 / 套件: %s" % (len(edits), test_rel))
        print("        命中用例: %s" % (hits or "（无）"))
        res = [l for l in out.splitlines() if l.strip().startswith("RESULT:")] or \
              [l for l in out.splitlines() if l.strip().startswith("RESULT ")]
        if res:
            print("        %s" % res[0].strip())

    total = len(MUTATIONS) - skipped
    print("\n变异检出率: %d/%d%s" % (caught, total, "（%d 处锚点缺失已跳过）" % skipped if skipped else ""))
    shutil.rmtree(TMP, ignore_errors=True)
    return 0 if caught == total else 1


if __name__ == "__main__":
    sys.exit(main())
