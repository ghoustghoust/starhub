# -*- coding: utf-8 -*-
"""存量 star 全量 LLM 重分类（SPEC 2026-10-02 §4-T2）。

用法：
  python tools/reclassify_all.py --dry-run              # 对照表 + topic 报告，不写库（默认）
  python tools/reclassify_all.py --apply                # 确认对照表后写回 known_categories.json + known_notes.json
  python tools/reclassify_all.py --source remote        # 从远端 main 拉最新映射做基线（默认 local）

行为：
- 基线映射 = --source 指定的 known_categories.json（local 工作树 / remote main blob）；
- 逐条调 fetch_and_build.classify_llm（复用其 Agnes key 池与解析），每条间隔 ≥1.2s 限速；
- LLM 失败（含熔断）的条目保持旧类不动，记入 `failed` 名单；
- --dry-run 输出 markdown 对照表到 _t1_baseline/reclassify_diff.md（旧类→新类全量 + 变动清单 + topic 分布 top30）；
- --apply 原子写回 known_categories.json（LLM 类目）与 known_notes.json（点评，空点评不覆盖旧值），
  known_categories 保留基线里 LLM 无法重判（failed）的旧值——文件只增不减的教训见手册 §8.16。
"""
import argparse
import base64
import json
import os
import re
import subprocess
import sys
import time
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import fetch_and_build as fab  # noqa: E402

OUT_DIR = os.path.join(ROOT, "_t1_baseline")
KC_PATH = os.path.join(ROOT, "known_categories.json")
NOTES_PATH = os.path.join(ROOT, "known_notes.json")
DIFF_PATH = os.path.join(OUT_DIR, "reclassify_diff.md")
STATE_PATH = os.path.join(OUT_DIR, "reclassify_state.json")
SLEEP_SECONDS = 1.2
# LLM 失败重试（指数退避）：单 key 池跑全量必然撞配额窗口，慢可以、丢不行；
# 配合 STATE_PATH 断点续跑，多跑几轮自然收敛到全量成功。
RETRY_DELAYS = (30, 60, 120)


import urllib.request

LIVE_INDEX_URL = "https://kwei168.github.io/starhub/index.html"


def load_live_index():
    """从线上首页内联 DATA 提取全字段索引（desc 覆盖 100%，topics ~60%）。
    这是重分类的信息来源——只给 LLM 仓库名会瞎猜（首轮实测 185/297 失败/误判）。"""
    with urllib.request.urlopen(LIVE_INDEX_URL, timeout=90) as resp:
        html = resp.read().decode("utf-8")
    m = re.search(r"const DATA = (\[.*?\]);?\s*\n", html, re.S) or re.search(r"const DATA = (\[.*?\])\s*;", html, re.S)
    if not m:
        raise RuntimeError("线上 index.html 中未找到 const DATA（页面结构变了？）")
    data = json.loads(m.group(1))
    idx = {}
    for d in data:
        fn = d.get("full_name")
        if fn:
            idx[fn] = d
    print("[reclassify] 线上索引：%d 条（desc %d / topics %d）"
          % (len(idx), sum(1 for v in idx.values() if v.get("desc")),
             sum(1 for v in idx.values() if v.get("topics"))))
    return idx


def load_local():
    with open(KC_PATH, encoding="utf-8") as f:
        return json.load(f)


def load_remote():
    out = subprocess.run(
        ["gh", "api", "repos/Kwei168/starhub/contents/known_categories.json", "--jq", ".content"],
        capture_output=True, text=True, check=True).stdout
    return json.loads(base64.b64decode(out).decode("utf-8"))


def atomic_write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="写回映射（缺省 dry-run）")
    ap.add_argument("--source", choices=["local", "remote"], default="local")
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 条（调试用）")
    args = ap.parse_args()

    baseline = load_remote() if args.source == "remote" else load_local()
    fn_list = sorted(baseline.keys())
    if args.limit:
        fn_list = fn_list[: args.limit]
    live = load_live_index()
    print("[reclassify] 基线 %d 条（source=%s），LLM 池就绪=%s"
          % (len(fn_list), args.source, bool(os.environ.get("AGNES_API_KEY") or os.environ.get("AGNES_API_KEYS"))))

    # 断点续跑：上一轮已成功的结果直接复用（失败的不记录，重跑自动重试）
    state = {}
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, encoding="utf-8") as f:
            state = json.load(f)
        print("[reclassify] 断点：%d 条已有结果，跳过" % len(state))

    new_cat, new_note, failed, lowinfo, unchanged, changed = {}, {}, [], [], 0, 0
    state_dirty = False
    consec_fail = 0
    for i, fn in enumerate(fn_list, 1):
        old = baseline[fn]
        if isinstance(old, dict):  # 未来结构兼容：value 变 dict 时取 cat 键
            old = old.get("cat", "")
        if fn in state and state[fn].get("category"):
            res = state[fn]
        else:
            info = live.get(fn) or {}
            desc = info.get("desc") or ""
            if not desc:
                # 无描述 → LLM 只能瞎猜（首轮实证），保留旧值进 low-info 名单
                new_cat[fn] = old
                lowinfo.append(fn)
                continue
            res = None
            for delay in (None,) + RETRY_DELAYS:  # 首试 + 退避重试
                if delay:
                    print("[reclassify] %s 失败，退避 %ds 后重试" % (fn, delay), flush=True)
                    time.sleep(delay)
                try:
                    res = fab.classify_llm(fn, desc, info.get("language") or "",
                                           info.get("topics") or [], fab.CATS)
                except Exception as e:  # noqa: BLE001
                    print("[reclassify] %s 异常: %s" % (fn, e), file=sys.stderr)
                    res = None
                if res and res.get("category"):
                    break
                # 全局熔断：连败说明配额窗口整体没恢复，逐条独立退避会拖到天亮——
                # 暂停一段再继续，并把已完成结果先落盘（断点续跑的断点意义）
                consec_fail += 1
                if consec_fail >= 5:
                    atomic_write_json(STATE_PATH, state)
                    state_dirty = False
                    print("[reclassify] 连续 %d 条失败，全局暂停 %ds 让配额窗口恢复" % (consec_fail, RETRY_DELAYS[-1]), flush=True)
                    time.sleep(RETRY_DELAYS[-1])
                    consec_fail = 0
            if res and res.get("category"):
                consec_fail = 0
                state[fn] = {"category": res["category"], "note": (res.get("note") or "").strip()}
                atomic_write_json(STATE_PATH, state)  # 每条落盘：中断不丢已成功的
                state_dirty = False
        if res and res.get("category"):
            new_cat[fn] = res["category"]
            note = (res.get("note") or "").strip()
            if note:
                new_note[fn] = note
            if res["category"] == old:
                unchanged += 1
            else:
                changed += 1
                print("[%d/%d] %s: %s -> %s%s" % (i, len(fn_list), fn, old, res["category"],
                                                  ("（%s）" % note) if note else ""), flush=True)
        else:
            failed.append(fn)
            new_cat[fn] = old  # 保持旧值（文件只增不减，§8.16 教训）
            print("[%d/%d] %s: LLM 失败，保留 %s" % (i, len(fn_list), fn, old), flush=True)
            continue
        time.sleep(SLEEP_SECONDS)

    if state_dirty:
        atomic_write_json(STATE_PATH, state)

    moved = [(fn, baseline[fn], new_cat[fn]) for fn in fn_list
             if fn in new_cat and new_cat[fn] != baseline[fn]]
    topic_rows = _topic_report()

    os.makedirs(OUT_DIR, exist_ok=True)
    with open(DIFF_PATH, "w", encoding="utf-8") as f:
        f.write("# 存量重分类对照表（dry-run %s）\n\n" % time.strftime("%Y-%m-%d %H:%M"))
        f.write("- 基线：%d 条（source=%s）；LLM 成功 %d、失败保留旧值 %d、无描述跳过 %d\n"
                % (len(fn_list), args.source, len(fn_list) - len(failed) - len(lowinfo), len(failed), len(lowinfo)))
        f.write("- **类目变动 %d 条、维持 %d 条**；分布对比：\n\n" % (changed, unchanged))
        f.write("| 旧类分布 | 新类分布 |\n|---|---|\n")
        old_c = Counter(str(v) for v in baseline.values())
        new_c = Counter(new_cat.values())
        for k in sorted(set(old_c) | set(new_c), key=lambda x: -(old_c.get(x, 0) + new_c.get(x, 0))):
            f.write("| %s %d | %s %d |\n" % (k, old_c.get(k, 0), k, new_c.get(k, 0)))
        f.write("\n## 变动清单（旧 → 新）\n\n| repo | 旧 | 新 | 点评 |\n|---|---|---|---|\n")
        for fn, o, n in moved:
            f.write("| %s | %s | %s | %s |\n" % (fn, o, n, new_note.get(fn, "")))
        f.write("\n## LLM 失败名单（保留旧值）\n\n" + (", ".join(failed) or "（无）") + "\n")
        if lowinfo:
            f.write("\n## 无描述跳过名单（保留旧值，不瞎猜）\n\n" + ", ".join(lowinfo) + "\n")
        f.write("\n" + topic_rows)
    print("[reclassify] 对照表已写 %s（变动 %d / 维持 %d / 失败 %d / 无描述 %d）"
          % (DIFF_PATH, changed, unchanged, len(failed), len(lowinfo)))

    if args.apply:
        if failed:
            print("[reclassify] 有 %d 条失败，仍按计划保留旧值写回；重跑可继续补判" % len(failed))
        old_notes = {}
        if os.path.exists(NOTES_PATH):
            with open(NOTES_PATH, encoding="utf-8") as f:
                old_notes = json.load(f)
        merged_notes = dict(old_notes)
        merged_notes.update(new_note)
        atomic_write_json(KC_PATH, new_cat)
        atomic_write_json(NOTES_PATH, merged_notes)
        print("[reclassify] 已写回 %s（%d 条）与 %s（%d 条）" % (KC_PATH, len(new_cat), NOTES_PATH, len(merged_notes)))
    else:
        print("[reclassify] dry-run 完成——过目对照表后加 --apply 写库")


def _topic_report():
    """topic 分布 top30：从远端 starred 拉不到了（匿名配额），改读本地描述缓存不可行——
    topic 数据在构建期由 GitHub API 响应带出，此处用最近一次构建的内联 DATA 不可得；
    退而求其次：对 known_categories 键的 repo 名与已知 topic 词表做共现统计（方向性参考）。"""
    rows = ["\n## topic 共现（方向性参考，非全量）\n"]
    try:
        with open(KC_PATH, encoding="utf-8") as f:
            keys = list(json.load(f).keys())
        words = Counter()
        for k in keys:
            for w in k.lower().replace("-", " ").replace("_", " ").split("/")[1 if "/" in k else 0].split():
                if len(w) >= 4 and w not in ("awesome", "skill", "skills", "agent", "tools", "open", "source"):
                    words[w] += 1
        for w, n in words.most_common(30):
            rows.append("- `%s` ×%d" % (w, n))
    except Exception as e:  # noqa: BLE001
        rows.append("（生成失败: %s）" % e)
    return "\n".join(rows)


if __name__ == "__main__":
    main()
