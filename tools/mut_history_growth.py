# -*- coding: utf-8 -*-
"""批 6 变异电池：健康监测这几条接缝能不能被静默捅穿。

用法：python tools/mut_history_growth.py
判据文件读 STARHUB_GROWTH_TOOL / STARHUB_UPDATE_YML ⇒ 变异体只写临时副本，绝不就地改工具或 workflow。
与 mut_state_cache.py 同一约定：基线不绿就拒绝自评；变异没落到字节上算 INVALID（计为未覆盖）。
"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(ROOT, "tools", "history_growth.py")
WF = os.path.join(ROOT, ".github", "workflows", "update.yml")
TEST = os.path.join("tests", "rss_history", "test_history_growth.py")
TMP = os.path.join(ROOT, ".deploy-tmp")
STEP_MARK = "      - name: Monitor per-build git growth (advisory)\n"


def main():
    tool = open(TOOL, encoding="utf-8").read()
    wf = open(WF, encoding="utf-8").read()
    assert tool.count("def threshold():") == 1 and wf.count(STEP_MARK) == 1, "锚点自检失败"

    MUTS = [
        ("H1 阈值写成 0（天天误报=没人看的告警）", (tool.replace("    return DEFAULT_THRESHOLD", "    return 0", 1), wf)),
        ("H2 verdict 永远 ok（回退 20 MiB/场 也不出声）",
         (tool.replace('    return "warn" if new_bytes >= threshold() else "ok"', '    return "ok"', 1), wf)),
        ("H3 只数新增、不数内容变化（旧 blob 被覆盖仍是永久多一份）",
         (tool.replace("        if prev.get(path, (None,))[0] != sha:",
                       "        if path not in prev:", 1), wf)),
        ("H4 ls-tree 取错列（把 sha 当大小 ⇒ 读数恒 0）",
         (tool.replace('size = 0 if cols[3] == "-" else int(cols[3])', "size = 0", 1), wf)),
        ("H5 播报里去掉 ::warning（只在 stdout 打印会被漏看）",
         (tool.replace('out.append("::warning title=每场新增字节超阈值::', 'out.append("note: ', 1), wf)),
        ("H6 去掉 continue-on-error（advisory 步一红就连坐两次部署）",
         (tool, wf.replace(STEP_MARK + "        #", STEP_MARK + "        #", 1)
               .replace("        continue-on-error: true\n        run: |\n          python tools/history_growth.py",
                        "        run: |\n          python tools/history_growth.py", 1))),
        ("H7 把测量步挪到 Commit 之前（量到上一场，本场膨胀恰好看不见）",
         (tool, wf.replace(STEP_MARK, "", 1).replace(
             "      - name: Commit & push if changed", STEP_MARK + "      - name: Commit & push if changed", 1))),
        ("H8 监测步不再 --log（读数只活在 Actions 的一次性输出里）",
         (tool, wf.replace(' --log "build_logs/$(TZ=Asia/Shanghai date +%F).jsonl"', ""))),
        ("H9 log_line 改成覆盖写（每场把前一场的读数抹掉）",
         (tool.replace('with open(path, "a", encoding="utf-8"', 'with open(path, "w", encoding="utf-8"'), wf)),
        ("H10 growth 事件换个 type 名（下游按 type 取数就取不到了）",
         (tool.replace('"type": "growth"', '"type": "gzz"', 1), wf)),
        ("H11 growth 事件的 type 写成 build（每日摘要的 builds 计数被污染）",
         (tool.replace('"type": "growth"', '"type": "build"', 1), wf)),
        ("H12 log_line 不再写 new_bytes（有事件但没读数）",
         (tool.replace('"new_bytes": int(new_bytes), ', ""), wf)),
        # ↓ 三条打的是 2026-10-02 新加的"归属"这一层：读数算不算本场。
        # 没有它们，`attribution()` 反判、播报少了免责说明、记录里丢掉 committed_by_run
        # 都不会有人红 —— 而这三条恰恰是"把别人的提交折进每场曲线"这条噪声的防线。
        ("H13 归属反判（HEAD==检出 sha 却说本场提交了）",
         (tool.replace("    return run_sha.strip()[:40] != head_sha.strip()[:40]",
                       "    return run_sha.strip()[:40] == head_sha.strip()[:40]", 1), wf)),
        ("H14 没提交也不说明（数字照播，读表的人无从折扣）",
         (tool.replace("    if attributed is False:", "    if False:", 1), wf)),
        ("H15 结构化记录里丢掉 committed_by_run（跨场查归属就没了字段）",
         (tool.replace('    if attributed is not None:\n        rec["committed_by_run"] = bool(attributed)',
                       '    if attributed is not None:\n        pass', 1), wf)),
        # 来自早先一份独立电池草稿（X3）：本仓反复栽在"默认值冒充读数"。
        # 归属未知（本地跑 / 浅取 / git 读失败）时字段必须**缺席**；写成 False 就把
        # "没测到"伪装成"测到了：本场没提交"，读表的人会当成事实。
        ("H16 未知归属被填成默认 False（「没测到」伪装成读数）",
         (tool.replace('        rec["committed_by_run"] = bool(attributed)',
                       '        rec["committed_by_run"] = False', 1), wf)),
    ]

    def run(mut_tool, mut_wf, label):
        if (mut_tool == tool) and (mut_wf == wf):
            return "%-56s INVALID（变异没落到任何字节上）" % label, False
        os.makedirs(TMP, exist_ok=True)
        tp = os.path.join(TMP, "_mut_hg_%d.py" % (abs(hash(label)) % 100000))
        wp = os.path.join(TMP, "_mut_hg_update.yml")
        open(tp, "w", encoding="utf-8", newline="\n").write(mut_tool)
        open(wp, "w", encoding="utf-8", newline="\n").write(mut_wf)
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", STARHUB_GROWTH_TOOL=tp, STARHUB_UPDATE_YML=wp, PYTHONIOENCODING="utf-8")
        p = subprocess.run([sys.executable, "-m", "pytest", TEST, "-q", "--no-header",
                            "-p", "no:cacheprovider"], cwd=ROOT, env=env,
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=600)
        lines = (p.stdout or "").strip().splitlines()
        failed = sorted({l.split("::")[-1].split(" ")[0] for l in lines if l.startswith("FAILED")})
        return "%-56s %s %s%s" % (label, "RED  " if p.returncode else "GREEN(判据漏了!)",
                                  (lines[-1] if lines else "?")[:56],
                                  ("  <- " + ", ".join(failed[:2])) if failed else ""), p.returncode != 0

    base = subprocess.run([sys.executable, "-m", "pytest", TEST, "-q", "--no-header",
                           "-p", "no:cacheprovider"], cwd=ROOT,
                          capture_output=True, text=True, encoding="utf-8", timeout=600)
    print("基线（未变异）：%s  rc=%d" % ((base.stdout.strip().splitlines() or ["?"])[-1], base.returncode))
    if base.returncode != 0:
        print(base.stdout[-1500:])
        print("基线不绿 ⇒ 不声称任何覆盖率")
        return 1
    escapes = 0
    for label, (mt, mw) in MUTS:
        out, caught = run(mt, mw, label)
        print(out)
        if not caught:
            escapes += 1
    print("\n未被挡住的变异/无效变异：%d" % escapes)
    return 1 if escapes else 0


if __name__ == "__main__":
    sys.exit(main())
