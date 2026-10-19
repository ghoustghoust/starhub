# -*- coding: utf-8 -*-
"""growth_history 判据的变异电池：把读回端的四处关键语义逐个改掉，看判据抓不抓。

用法：py -3.11 tools/mut_growth_history.py
约定同其它电池：只改临时副本（STARHUB_GROWTH_HISTORY 注入）、基线不绿就拒绝自评、
PYTHONDONTWRITEBYTECODE=1 避开"同长度变异 + 同一秒"的 pyc 假绿。
"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(ROOT, "tools", "growth_history.py")
TEST = os.path.join("tests", "rss_history", "test_growth_history.py")
TMP = os.path.join(ROOT, ".deploy-tmp")

MUTS = [
    ("GH1 去掉 type 过滤（任何日志行都被当成读数）",
     ('rec.get("type") == "growth"', "True")),
    ("GH2 max_bytes 用均值冒充（最坏那一场被抹平）",
     ('"max_bytes": max(sizes),', '"max_bytes": int(round(sum(sizes) / len(sizes))),')),
    ("GH3 半截 JSON 行改成抛错（一行坏数据毁掉整份读数）",
     ('        except Exception:\n            continue', '        except Exception:\n            raise')),
    ("GH4 忽略本地目录、永远走远端（每次问一句都要联网，且 CI 内看不到当天）",
     ('    if local_dir and os.path.exists(p):', "    if False:")),
]


def main():
    src = open(TOOL, encoding="utf-8").read()
    os.makedirs(TMP, exist_ok=True)

    def run(old, new, label):
        if src.count(old) != 1:
            return "%-56s INVALID（锚点命中 %d 次）" % (label, src.count(old)), False
        tp = os.path.join(TMP, "_mut_growth_history_%d.py" % (abs(hash(label)) % 100000))
        open(tp, "w", encoding="utf-8", newline="\n").write(src.replace(old, new, 1))
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", STARHUB_GROWTH_HISTORY=tp,
                   PYTHONIOENCODING="utf-8")
        p = subprocess.run([sys.executable, "-m", "pytest", TEST, "-q", "--no-header",
                            "-p", "no:cacheprovider"], cwd=ROOT, env=env,
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=600)
        lines = (p.stdout or "").strip().splitlines()
        failed = sorted({l.split("::")[-1].split(" ")[0] for l in lines if l.startswith("FAILED")})
        try:
            os.remove(tp)
        except OSError:
            pass
        return "%-56s %s %s" % (label, "RED  " if p.returncode else "GREEN(判据漏了!)",
                                ", ".join(x.split(".")[-1] for x in failed)[:70]
                                or (lines[-1] if lines else "?")[:60]), p.returncode != 0

    base = subprocess.run([sys.executable, "-m", "pytest", TEST, "-q", "--no-header",
                           "-p", "no:cacheprovider"], cwd=ROOT,
                          capture_output=True, text=True, encoding="utf-8", timeout=600)
    print("基线（未变异）：%s rc=%d" % ((base.stdout.strip().splitlines() or ["?"])[-1], base.returncode))
    if base.returncode != 0:
        print(base.stdout[-1200:])
        print("基线不绿 ⇒ 不声称覆盖率")
        return 1
    escapes = 0
    for label, (old, new) in MUTS:
        out, caught = run(old, new, label)
        print(out)
        if not caught:
            escapes += 1
    print("\n未被挡住的变异/无效变异：%d" % escapes)
    return 1 if escapes else 0


if __name__ == "__main__":
    sys.exit(main())
