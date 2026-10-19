# -*- coding: utf-8 -*-
"""sync-agnes-env 两道部署守门的变异电池：每条判据都要能被一个对应的坏改动挡住。

与 tools/mut_star_state.py 同形：真实文件一字节不动，把 workflow + 判据 + 另两条车道的
workflow（checkout 版本判据要读它们）复制进临时目录打变异，用 STARHUB_SYNC_YML 注入点
让判据读副本。每轮从干净副本重打，并要求"未变异对照组先绿 + 变异后靶判据自己红"。

用法：py -3.11 tools/mut_sync_agnes.py
"""
import io
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
YML = os.path.join(".github", "workflows", "sync-agnes-env.yml")
TEST = os.path.join("tests", "site_nav_drift", "test_sync_agnes_guards.py")
COPIES = (YML, TEST,
          os.path.join(".github", "workflows", "update.yml"),
          os.path.join(".github", "workflows", "star-fast.yml"))

MUTATIONS = [
    ("G1 前守门整步消失",
     "      - name: Guard — no hourly build in flight",
     "      - name: Nothing to see here",
     "test_both_guards_exist_and_wrap_the_deploy"),
    ("G2 前守门退化成只打印（比没有更糟）",
     '            exit 1\n          fi\n          echo "BASE_SHA=',
     '            echo "（查过了，算了）"\n          fi\n          echo "BASE_SHA=',
     "test_pre_guard_actually_queries_and_refuses"),
    ("G3 BASE_SHA 不再在 upsert 前记录",
     '          echo "BASE_SHA=$(git rev-parse HEAD)" >> "$GITHUB_ENV"',
     "          true",
     "test_pre_guard_records_base_sha_before_any_write"),
    ("G4 后守门比较式写反（恒不触发）",
     'if [ "$LIVE" != "$BASE_SHA" ]; then',
     'if [ "$LIVE" = "$BASE_SHA" ]; then',
     "test_post_guard_compares_shas_and_refuses_on_drift"),
    ("G5 checkout 漂回 v4（与另两条车道分叉）",
     "actions/checkout@v5", "actions/checkout@v4",
     "test_checkout_version_matches_the_other_lanes"),
]


def _stage(tmp):
    for rel in COPIES:
        dst = os.path.join(tmp, rel)
        d = os.path.dirname(dst)
        if d:
            os.makedirs(d, exist_ok=True)
        shutil.copyfile(os.path.join(ROOT, rel), dst)


def _run(tmp, name):
    env = {**os.environ, "STARHUB_SYNC_YML": os.path.join(tmp, YML),
           "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"}
    r = subprocess.run([sys.executable, "-m", "pytest", TEST, "-q", "-k", name],
                       cwd=tmp, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", env=env)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def main():
    bad = []
    with tempfile.TemporaryDirectory(dir=os.path.join(ROOT, ".deploy-tmp")) as tmp:
        for mid, old, new, must in MUTATIONS:
            _stage(tmp)
            code0, out0 = _run(tmp, must)
            if code0 != 0:
                bad.append("%s：对照组（未变异）就不绿，本条判定作废：%s" % (mid, out0.strip()[-200:]))
                continue
            p = os.path.join(tmp, YML)
            s = io.open(p, encoding="utf-8").read()
            n = s.count(old)
            if n != 1:
                bad.append("%s：靶子在 yml 里出现 %d 次 ⇒ 锚点失效，变异没打上" % (mid, n))
                continue
            io.open(p, "w", encoding="utf-8", newline="\n").write(s.replace(old, new, 1))
            code, out = _run(tmp, must)
            if code == 0:
                bad.append("%s：打了坏改动，%s 仍然绿 ⇒ 空判据" % (mid, must))
                continue
            if "failed" not in out and "error" not in out.lower():
                bad.append("%s：非零退出但不是判据红（可能收集崩）：%s" % (mid, out.strip()[-200:]))
                continue
            print("  %-44s 挡住 -> %s" % (mid, must))

    if bad:
        print("\n%d 项不合格：" % len(bad))
        for b in bad:
            print("  - " + b)
        return 1
    print("\n全部 %d 个变异都被各自靶判据挡住，且每条都有未变异对照组先证明过绿。" % len(MUTATIONS))
    return 0


if __name__ == "__main__":
    sys.exit(main())
