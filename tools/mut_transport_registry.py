# -*- coding: utf-8 -*-
"""判据 (e) 反向登记对账的变异电池：把通路拆掉/塞错东西，看登记表是否真的在管事。

用法：py -3.11 tools/mut_transport_registry.py
约定同其它电池：只改临时副本（STARHUB_UPDATE_YML 注入），基线不绿就拒绝自评，
变异没落到字节上算 INVALID（计入未覆盖）。
"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WF = os.path.join(ROOT, ".github", "workflows", "update.yml")
TEST = os.path.join("tests", "rss_history", "test_state_transport_registry.py")
TMP = os.path.join(ROOT, ".deploy-tmp")

STATE_LINE = "            hot_snapshot.json\n"
ADD_MAIN = ("git add known_categories.json rss_sources.json build_rss_aggregator.py "
            "insight_engine.py build_daily_insight.py test_daily_insight.py")


def sub_all(text, old, new, expect):
    """要求锚点恰好命中 expect 处再全替换：命中数不符就抛错，绝不让变异静默半生效。

    R1/R5 第一版就栽在这里 —— 只删了 restore 那一处，save 那处还留着，
    于是"通路没了"的变异其实没落地，判据当然还是绿的。
    """
    n = text.count(old)
    assert n == expect, "锚点命中 %d 处（应为 %d）：%r" % (n, expect, old[:48])
    return text.replace(old, new)


def sub_nth(text, old, new, nth):
    i, seen = -1, 0
    while True:
        i = text.find(old, i + 1)
        if i < 0:
            raise AssertionError("锚点不足 %d 处" % nth)
        seen += 1
        if seen == nth:
            return text[:i] + new + text[i + len(old):]


def main():
    src = open(WF, encoding="utf-8").read()
    assert src.count(STATE_LINE) == 2, "批 5a 的 hot_snapshot 行应出现 2 次（restore+save）"
    MUTS = [
        ("R1 从缓存 path 摘掉 descriptions_zh.json 两处（restore+save 同删；字面量读写无常量 ⇒ 静默冻译文缓存）",
         src.replace("            descriptions_zh.json\n", "")),
        ("R2 把只写不读的 source_quality.json 塞进缓存 path（无界语料搬进缓存）",
         src.replace(STATE_LINE, STATE_LINE + "            source_quality.json\n", 1)),
        ("R3 把 rss_cache.json 加回提交清单（豁免名单变成掩盖真问题的口袋）",
         src.replace(ADD_MAIN, ADD_MAIN.replace("git add ", "git add rss_cache.json ", 1), 1)),
        ("R4 缓存里写一个源码不引用的名字（白花配额）",
         src.replace(STATE_LINE, "            hot_snapshot_updated.json\n", 1)),
        ("R5 两处都摘掉 trending_snapshot.json（星标增量基线失去唯一通路）",
         sub_all(src, "            trending_snapshot.json\n", "", 2)),
    ]

    def run(mut, label):
        if mut == src:
            return "%-58s INVALID（变异没落到字节上）" % label, False
        os.makedirs(TMP, exist_ok=True)
        wp = os.path.join(TMP, "_mut_reg_update.yml")
        open(wp, "w", encoding="utf-8", newline="\n").write(mut)
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", STARHUB_UPDATE_YML=wp, PYTHONIOENCODING="utf-8")
        p = subprocess.run([sys.executable, "-m", "pytest", TEST, "-q", "--no-header",
                            "-p", "no:cacheprovider"], cwd=ROOT, env=env,
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=900)
        lines = (p.stdout or "").strip().splitlines()
        failed = sorted({l.split("::")[-1].split(" ")[0] for l in lines if l.startswith("FAILED")})
        return "%-58s %s %s" % (label, "RED  " if p.returncode else "GREEN(判据漏了!)",
                               ", ".join(failed)[:78] or (lines[-1] if lines else "?")[:70]), p.returncode != 0

    base = subprocess.run([sys.executable, "-m", "pytest", TEST, "-q", "--no-header",
                           "-p", "no:cacheprovider"], cwd=ROOT,
                          capture_output=True, text=True, encoding="utf-8", timeout=900)
    print("基线（未变异）：%s rc=%d" % ((base.stdout.strip().splitlines() or ["?"])[-1], base.returncode))
    if base.returncode != 0:
        print("基线不绿 ⇒ 不声称覆盖率")
        return 1
    escapes = 0
    for label, mut in MUTS:
        out, caught = run(mut, label)
        print(out)
        if not caught:
            escapes += 1
    print("\n未被挡住的变异/无效变异：%d" % escapes)
    return 1 if escapes else 0


if __name__ == "__main__":
    sys.exit(main())
