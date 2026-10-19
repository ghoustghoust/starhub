# -*- coding: utf-8 -*-
"""②（Pages 缺件点名 + 留痕）的变异电池。

单独一个文件是因为 `tools/mut_artifact_scope.py` 正在推送队列里（批 10），
不能就地改它 —— 这条约束本身就是本仓的老规矩："就地变异与推送互斥"。

三个变异各打一条断言：
  A1 去掉 `::error::` 前缀 ⇒ test_missing_file_fails_and_names_it 必须红（GitHub 不再显示为错误）
  A2 不写 build_logs 记录 ⇒ test_missing_file_leaves_a_cross_build_record 必须红（跨场查不到）
  A3 缺件也不 exit 1      ⇒ 两条 rc 断言必须红（把缺文件的站点发上线 = 死链）
被测正文走 `STARHUB_UPDATE_YML` 副本，真 workflow 一个字节都不动。
"""
import hashlib
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WF = os.path.join(ROOT, ".github", "workflows", "update.yml")
COPY = os.path.join(ROOT, ".deploy-tmp", "_mut_pages_missing.yml")
TEST = os.path.join("tests", "site_nav_drift", "test_pages_missing_report.py")

A1 = 'echo "::error::Pages 缺件：$must（这场不发，站点停在上一份制品）"'
A1_OFF = 'echo "缺件：$must"'
A2 = '>> "build_logs/$(TZ=Asia/Shanghai date +%F).jsonl" || true'
A2_OFF = ">/dev/null || true"
A3 = 'echo "缺件清单：$MISS"\n          exit 1'
A3_OFF = 'echo "缺件清单：$MISS"'
A4 = 'case " $MISS " in *" $must "*) ;; *) MISS="$MISS $must";; esac'
A4_OFF = 'MISS="$MISS $must"'

CASES = [
    ("A1 去掉 ::error:: 前缀（CI 里不再是错误）", A1, A1_OFF,
     "test_missing_file_fails_and_names_it"),
    ("A2 缺件不写跨场记录", A2, A2_OFF,
     "test_missing_file_leaves_a_cross_build_record"),
    ("A3 缺件也不退出（把缺件站点发上线）", A3, A3_OFF,
     "test_missing_file_fails_and_names_it"),
    # 这条打的是"顺手简化"：判重删掉后名字会列两遍，而真正的代价在另一侧 ——
    # 0 字节件不再被累加进 MISS，空文件会被当有效件发出去。
    ("A4 去掉 MISS 判重（重复列表 + 空件漏报的同一处）", A4, A4_OFF,
     "test_missing_file_leaves_a_cross_build_record"),
]


def run():
    env = dict(os.environ, STARHUB_UPDATE_YML=COPY, PYTHONIOENCODING="utf-8")
    p = subprocess.run([sys.executable, "-m", "pytest", TEST, "-q", "--no-header",
                        "-p", "no:cacheprovider"],
                       cwd=ROOT, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=env, timeout=900)
    lines = (p.stdout or "").strip().splitlines()
    failed = sorted({l.split("::")[-1].split(" ")[0] for l in lines if l.startswith("FAILED")})
    return p.returncode != 0, (lines[-1] if lines else "?"), failed


src = open(WF, encoding="utf-8").read()
h0 = hashlib.sha256(open(WF, "rb").read()).hexdigest()

open(COPY, "w", encoding="utf-8", newline="").write(src)
red, tail, failed = run()
print("基线（未变异副本）：%s" % tail)
if red:
    print("基线不绿 ⇒ 不作任何覆盖率声称；失败：%s" % failed)
    sys.exit(1)

bad = []
for label, a, b, target in CASES:
    n = src.count(a)
    if n != 1:
        bad.append("%s INVALID（锚点命中 %d 次）" % (label, n))
        continue
    open(COPY, "w", encoding="utf-8", newline="").write(src.replace(a, b, 1))
    red, tail, failed = run()
    print("%-40s %s %s%s" % (label, "RED  " if red else "GREEN(判据漏了!)", tail[:22],
                             ("  <- " + ", ".join(failed)) if failed else ""))
    if not red:
        bad.append(label + "（没挡住）")
    elif target not in failed:
        bad.append("%s 红了但不是自己的靶（%s）" % (label, failed))

os.remove(COPY)
same = hashlib.sha256(open(WF, "rb").read()).hexdigest() == h0
print("真 workflow 未被改动：%s" % ("是" if same else "否"))
print("问题条目：%s" % (bad if bad else "无"))
sys.exit(1 if bad or not same else 0)
