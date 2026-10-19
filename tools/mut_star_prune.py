# -*- coding: utf-8 -*-
"""star-state 侧剪枝的变异电池（2026-10-12）。

`mut_star_state.py` 的靶子全在 star-fast.yml 里，而这一半的逻辑落在两个 Python 文件
（`tools/restore_star_state.py` 的写端剪枝、`fast_refresh.py` 的名单与剪表），所以另起一个。
它钉的是"这套剪枝被改坏之后会怎么伤人"的七种真实形状，其中 P2/P3 两条对应的是**误删**
（比不剪更坏），P6/P7 对应的是**白烧**（本车道最贵的错误）。

真实文件一个字节不动：把被测体与两份判据复制进临时目录，每轮从干净副本重打
（变异绝不互相掩盖），并要求"整体红 + 靶判据自己红"两条同时成立。
PYTHONDONTWRITEBYTECODE 不是装饰——`mut_attention.py` 实测过：副本源文件与上一轮 pyc
的 mtime 落在同一秒里，子进程会复用旧 pyc ⇒ 变异没生效、判据"照样绿"。

用法：py -3.11 tools/mut_star_prune.py
"""
import io
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COPIES = ["fetch_and_build.py", "fast_refresh.py",
          os.path.join("tools", "restore_star_state.py"),
          os.path.join("tests", "tools", "test_restore_star_state.py"),
          os.path.join("tests", "tools", "test_fast_refresh_prune.py")]

# (id, 目标文件, 原文, 坏改动, 测试文件, 必须红的那条判据)
MUTATIONS = [
    ("P1 写端根本不剪枝", os.path.join("tools", "restore_star_state.py"),
     "    _prune_to_live(merged_out, _live_names())",
     "    pass  # _prune_to_live(merged_out, _live_names())",
     os.path.join("tests", "tools", "test_restore_star_state.py"),
     "test_write_prune_drops_branch_only_dead_key"),
    ("P2 名单缺失也照剪（降级被拆）", os.path.join("tools", "restore_star_state.py"),
     "    if not live:\n        return 0\n    trial = {name:",
     "    if False:\n        return 0\n    trial = {name:",
     os.path.join("tests", "tools", "test_restore_star_state.py"),
     "test_write_without_live_list_is_byte_identical"),
    ("P3 地板闸门放行垃圾名单", os.path.join("tools", "restore_star_state.py"),
     "    if kc is not None and len(kc) < FLOOR_KEYS:",
     "    if False:",
     os.path.join("tests", "tools", "test_restore_star_state.py"),
     "test_write_prune_refuses_when_result_under_floors"),
    ("P4 本车道不剪 categories", "fast_refresh.py",
     "    dropped = fab.prune_star_state(known, live)",
     "    dropped = []",
     os.path.join("tests", "tools", "test_fast_refresh_prune.py"),
     "test_fast_prunes_categories_and_leaves_notes_to_the_merge_side"),
    # P8 = 我自己 2026-10-12 真犯过的错（05:30/05:45 两场各推一枚内容相同的空提交）：
    # 在本车道顺手剪 notes。main 那份 notes 是冻结副本、每场都会带回来，所以"有变化"永远成立。
    ("P8 顺手在本车道剪 notes（每 15 分钟空推）", "fast_refresh.py",
     "    dropped = fab.prune_star_state(known, live)",
     "    dropped = fab.prune_star_state(known, live) + fab.prune_star_state(notes, live)",
     os.path.join("tests", "tools", "test_fast_refresh_prune.py"),
     "test_notes_dead_keys_alone_never_trigger_a_write"),

    ("P5 不写 live 名单（下游永远读不到）", "fast_refresh.py",
     "    _write_live_list(live)",
     "    pass  # _write_live_list(live)",
     os.path.join("tests", "tools", "test_fast_refresh_prune.py"),
     "test_fast_writes_live_list_for_the_merge_side"),
    ("P6 无变化也写盘（每 15 分钟白推）", "fast_refresh.py",
     "    if not new_repos and not dropped:",
     "    if not new_repos and False:",
     os.path.join("tests", "tools", "test_fast_refresh_prune.py"),
     "test_no_change_at_all_still_skips_the_write"),
    ("P7 纯剪枝场跑了 assemble（白烧 LLM）", "fast_refresh.py",
     "    if not new_repos:\n        # 纯剪枝的场次",
     "    if False:\n        # 纯剪枝的场次",
     os.path.join("tests", "tools", "test_fast_refresh_prune.py"),
     "test_pure_prune_never_runs_assemble_entries"),
]


def _stage(tmp):
    for rel in COPIES:
        dst = os.path.join(tmp, rel)
        d = os.path.dirname(dst)
        if d:
            os.makedirs(d, exist_ok=True)
        shutil.copyfile(os.path.join(ROOT, rel), dst)


def _run(tmp, test_file, name):
    cmd = [sys.executable, "-m", "pytest", test_file, "-q", "-k", name]
    r = subprocess.run(cmd, cwd=tmp, capture_output=True, text=True,
                       encoding="utf-8", errors="replace",
                       env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1",
                            "PYTHONIOENCODING": "utf-8"})
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    bad = []
    with tempfile.TemporaryDirectory(dir=os.path.join(ROOT, ".deploy-tmp")) as tmp:
        for mid, rel, old, new, test_file, must in MUTATIONS:
            _stage(tmp)
            code0, out0 = _run(tmp, test_file, must)
            if code0 != 0:
                bad.append("%s：对照组（未变异）就不绿，本条判定作废：%s" % (mid, out0.strip()[-200:]))
                continue
            p = os.path.join(tmp, rel)
            s = io.open(p, encoding="utf-8").read()
            n = s.count(old)
            if n != 1:
                bad.append("%s：靶子在 %s 里出现 %d 次 ⇒ 锚点失效，变异没打上" % (mid, rel, n))
                continue
            io.open(p, "w", encoding="utf-8", newline="\n").write(s.replace(old, new, 1))
            code, out = _run(tmp, test_file, must)
            if code == 0:
                bad.append("%s：打了坏改动，%s 仍然绿 ⇒ 空判据" % (mid, must))
                continue
            if "failed" not in out and "error" not in out.lower():
                bad.append("%s：非零退出但不是判据红（可能收集崩）：%s" % (mid, out.strip()[-200:]))
                continue
            print("  %-36s 挡住 -> %s" % (mid, must))

    if bad:
        print("\n%d 项不合格：" % len(bad))
        for b in bad:
            print("  - " + b)
        return 1
    print("\n全部 %d 个变异都被各自靶判据挡住，且每条都有未变异对照组先证明过绿。" % len(MUTATIONS))
    return 0


if __name__ == "__main__":
    sys.exit(main())
