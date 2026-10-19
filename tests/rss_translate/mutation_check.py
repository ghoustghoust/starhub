# tests/rss_translate/mutation_check.py
# -*- coding: utf-8 -*-
"""变异测试：验证翻译管线关键守卫的鉴别力。

做法：把每一处关键守卫逐个改坏（在副本上），跑对应套件，
断言「至少有一个预期用例变红」。改坏后仍全绿 → 该行无人守卫（变异存活）。

用法：python tests/rss_translate/mutation_check.py
"""
import os, sys, shutil, subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC = os.path.join(ROOT, "build_rss_aggregator.py")
TMP = os.path.join(ROOT, ".deploy-tmp", "_mut_translate")
PY = sys.executable

TEST_ENGINES = "tests/rss_translate/test_translate_engines.py"
TEST_CACHE = "tests/rss_translate/test_translate_cache.py"

# (名称, [(原文, 替换为), ...], 测试文件, 期望变红的用例名片段)
MUTATIONS = [
    # ── 翻译降级链 ──
    ("T-mut-a 中文跳过失效（日文也被跳过）",
     [("if not has_kana and cn_chars > len(text) * 0.3:",
       "if cn_chars > len(text) * 0.3:")],
     TEST_ENGINES, ["T2 日文不跳过"]),

    ("T-mut-b 缓存命中失效（每次都重新翻译）",
     [("if text_hash in _trans_cache:",
       "if False:")],
     TEST_CACHE, ["C1 翻译成功后写入缓存", "C2 熔断期间返回原文"]),

    ("T-mut-c 熔断阈值失效（永远不熔断）",
     [("if _TRANS_FAIL_STREAK >= 5:",
       "if _TRANS_FAIL_STREAK >= 999:")],
     TEST_ENGINES, ["T5 连续全败触发熔断"]),

    ("T-mut-d Agnes 罚期判断失效（罚期内仍调用 Agnes）",
     [("if _AGNES_KEY and time.time() >= _AGNES_BLOCK_UNTIL:",
       "if _AGNES_KEY:")],
     TEST_CACHE, ["C4 Agnes 罚期期间不被调用"]),

    ("T-mut-e 翻译统计 skip 计数失效",
     [('_TRANS_STATS["skip"] += 1',
       'pass  #')],
     TEST_ENGINES, ["T1b skip 计数 +1"]),
]

# ── 执行变异测试 ──
survived = []
killed = []

for name, replacements, test_file, expected_red in MUTATIONS:
    print("\n" + "=" * 74)
    print("变异: %s" % name)
    print("=" * 74)

    # 创建副本
    os.makedirs(os.path.dirname(TMP), exist_ok=True)
    if os.path.exists(TMP):
        os.remove(TMP)
    shutil.copy2(SRC, TMP)

    # 应用变异
    with open(TMP, "r", encoding="utf-8") as f:
        content = f.read()
    for old, new in replacements:
        if old not in content:
            print("  [WARN] 原文未找到: %s" % old[:60])
            continue
        content = content.replace(old, new, 1)
    with open(TMP, "w", encoding="utf-8") as f:
        f.write(content)

    # 运行测试（通过环境变量指定副本路径）
    env = dict(os.environ)
    env["RSS_BUILD_SRC"] = TMP
    result = subprocess.run(
        [PY, test_file],
        capture_output=True, text=True, env=env, cwd=ROOT,
        timeout=60,
    )

    # 检查期望变红的用例是否确实变红
    output = result.stdout + result.stderr
    found_red = False
    for red_name in expected_red:
        if "[FAIL]" in output and red_name in output:
            found_red = True
            print("  [KILLED] %s 被检测到变红" % red_name)
            break
    if not found_red and result.returncode != 0:
        found_red = True
        print("  [KILLED] 测试以非零退出码终止")

    if found_red:
        killed.append(name)
    else:
        survived.append(name)
        print("  [SURVIVED] 变异存活！无预期用例变红")
        # 打印部分输出帮助调试
        for line in output.split('\n'):
            if '[FAIL]' in line or 'FAIL' in line:
                print("    >> %s" % line.strip())

    # 清理副本
    if os.path.exists(TMP):
        os.remove(TMP)

# ── 汇总 ──
print("\n" + "=" * 74)
print("变异测试汇总: %d 杀死 / %d 存活 / %d 总计" % (len(killed), len(survived), len(MUTATIONS)))
if survived:
    print("\n存活的变异（需要补充测试）:")
    for s in survived:
        print("  - %s" % s)
    sys.exit(1)
else:
    print("ALL MUTATIONS KILLED — 测试守卫充分")
