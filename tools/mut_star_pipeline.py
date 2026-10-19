# -*- coding: utf-8 -*-
"""T1/T2/T3 star 管线判据的变异测试：每个变异都必须让至少一条判据变红。

用法：py -3.11 tools/mut_star_pipeline.py

与本目录其他电池的惯例差异（读我再跑）：mut_state_cache 等钉的是"读文件的判据"，
能走 STARHUB_UPDATE_YML 副本注入；star 判据（test_star_classify/enrich/fast_refresh/
star_frontend）是 **import 真模块 + 网络打桩**，没有 env 覆盖钩子 ⇒ 只能就地变异。
安全措施：字节级备份 → finally 强制复原 → 复原后与 git 工作树 diff 必须为空
（这三个文件工作树==远端==HEAD，任何残留 diff 都说明复原失败，电池自报 ESCAPED）。

变异面（锚全部取自 2026-10-04 工作树，锚失配报 INVALID——先怀疑锚再怀疑判据）：
  S1  key 池砍掉 AGNES_API_KEYS 附加段（§8.14 先建池再判空）
  S2  enable_thinking False→True
  S3  429 不轮转直接放弃
  S4  401/403 不立即停（继续烧池）
  S5  LLM 熔断阈值 3→30
  S6  classify_repo 异常上抛（"任何异常不向上抛"红线）
  S7  查表旁路（known_categories 失效 → 每仓库烧 LLM）
  S8  health_score 365→3650（stale/红档永远不触发）
  S9  archived 不再直接 red
  S10 fetch_stars 的 star+json Accept 头变异（starred_at 断供）
  S11 类目导览缓存旁路（成员未变也重烧 LLM）
  S12 note 长度截断去掉
  S13 fast_refresh 红线（源码出现 fab.main( 调用形状）
  S14 template.html 搜索历史 localStorage 键变异
"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAB = os.path.join(ROOT, "fetch_and_build.py")
FR = os.path.join(ROOT, "fast_refresh.py")
TPL = os.path.join(ROOT, "template.html")
TESTS = [
    os.path.join("tests", "site_nav_drift", "test_star_classify.py"),
    os.path.join("tests", "site_nav_drift", "test_star_enrich.py"),
    os.path.join("tests", "site_nav_drift", "test_fast_refresh.py"),
    os.path.join("tests", "site_nav_drift", "test_star_frontend.py"),
]

# (编号, 文件, [(旧, 新, 次数)], 说明)
MUTATIONS = [
    ("S1", FAB, [(
        '    keys += [k.strip() for k in (os.environ.get("AGNES_API_KEYS") or "").split(",") if k.strip()]',
        '    keys += []  # MUT', 1)], "key池砍附加key"),
    ("S2", FAB, [(
        '"chat_template_kwargs": {"enable_thinking": False},',
        '"chat_template_kwargs": {"enable_thinking": True},  # MUT', 1)], "enable_thinking反转"),
    ("S3", FAB, [(
        "                continue  # 限流：换下一个 key（单轮，不等待）",
        "                return None  # MUT", 1)], "429不轮转"),
    ("S4", FAB, [(
        "            if exc.code in (401, 403):\n                return None  # key 无效：直接停",
        "            if exc.code in (401, 403):\n                continue  # MUT", 1)], "401/403烧池"),
    ("S5", FAB, [(
        "_LLM_FAIL_LIMIT = 3", "_LLM_FAIL_LIMIT = 30  # MUT", 1)], "熔断阈值3→30"),
    ("S6", FAB, [(
        '    except Exception as e:  # noqa: BLE001\n        print("[classify_repo] %s 分类异常: %s" % (fn, e), file=sys.stderr)\n        return ("tools", "")',
        '    except Exception as e:  # noqa: BLE001\n        print("[classify_repo] %s 分类异常: %s" % (fn, e), file=sys.stderr)\n        raise', 1)], "异常上抛"),
    ("S7", FAB, [(
        "        cat = (known or {}).get(fn)",
        "        cat = None  # MUT", 1)], "查表旁路"),
    ("S8", FAB, [(
        "    stale = age_days > 365", "    stale = age_days > 3650  # MUT", 1),
        ("    if age_days > 365:", "    if age_days > 3650:  # MUT", 1)], "健康分365→3650"),
    ("S9", FAB, [(
        '    if bool(repo.get("archived")):', "    if False:  # MUT", 1)], "archived不红"),
    ("S10", FAB, [(
        '        headers["Accept"] = "application/vnd.github.star+json"  # 覆盖默认 Accept',
        '        headers["Accept"] = "application/vnd.github.star+json-MUT"  # MUT', 1)], "star+json断供"),
    ("S11", FAB, [(
        '        if hit and hit.get("members_hash") == digest and hit.get("text"):',
        '        if False and hit and hit.get("members_hash") == digest and hit.get("text"):  # MUT', 1)], "导览缓存旁路"),
    ("S12", FAB, [(
        '        note = str(parsed.get("note") or "").strip()[:_NOTE_MAX_CHARS]',
        '        note = str(parsed.get("note") or "").strip()', 1)], "note截断去掉"),
    ("S15", FAB, [(
        '        if not note:\n            note = (notes or {}).get(fn) or ""',
        '        if False:\n            note = (notes or {}).get(fn) or ""', 1)], "存量点评回填旁路"),
    ("S13", FR, [(
        None, "\n\nif False:\n    fab.main()\n", 1)], "fast_refresh红线形状"),
    ("S14", TPL, [(
        "starhub_search_history", "starhub_search_hist_MUT", 1)], "搜索历史键变异"),
]


def run_tests():
    r = subprocess.run(
        [sys.executable, "-m", "pytest"] + TESTS + ["-q", "--no-header", "-p", "no:cacheprovider"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600)
    out = r.stdout + r.stderr
    for line in out.splitlines():
        if line.strip().endswith("failed") or " passed" in line:
            return line.strip()
    return "rc=%d" % r.returncode


def baseline_green():
    r = subprocess.run(
        [sys.executable, "-m", "pytest"] + TESTS + ["-q", "--no-header", "-p", "no:cacheprovider"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600)
    return r.returncode == 0, (r.stdout + r.stderr)[-400:]


def main():
    # 复原基线 = 电池启动瞬间的工作树字节（可能带未提交的合法改动——比如点评回填修复
    # 在提交前跑电池），不是 git HEAD；HEAD 对比只在文件恰好干净时等价。
    snapshots = {}
    for path in sorted({p for _, p, _, _ in MUTATIONS}):
        with open(path, "rb") as f:
            snapshots[path] = f.read()

    ok, tail = baseline_green()
    if not ok:
        print("[基线] 判据本身是红的，变异读数无意义：\n%s" % tail)
        return 2
    print("[基线] 4 个 star 判据文件全绿 ✓")

    escaped, invalid, caught = [], [], []
    for mid, path, subs, label in MUTATIONS:
        with open(path, "rb") as f:
            original = f.read()
        try:
            text = original.decode("utf-8")
            mutated = text
            landed = True
            for old, new, count in subs:
                if old is None:  # 追加型变异
                    mutated = mutated + new
                    continue
                if old not in mutated:
                    landed = False
                    break
                mutated = mutated.replace(old, new, count)
            if not landed:
                invalid.append((mid, label))
                print("[%s] INVALID —— 锚失配（%s），先查锚再查判据" % (mid, label))
                continue
            with open(path, "wb") as f:
                f.write(mutated.encode("utf-8"))
            summary = run_tests()
            red = ("failed" in summary and " passed" in summary) or " error" in summary
            if red:
                caught.append(mid)
                print("[%s] red ✓ %s —— %s" % (mid, label, summary))
            else:
                escaped.append((mid, label))
                print("[%s] ESCAPED ✗✗ %s —— %s" % (mid, label, summary))
        finally:
            with open(path, "wb") as f:
                f.write(original)

    # 复原自证：每个被变异文件必须与电池启动快照逐字节一致
    dirty = []
    for path, snap in snapshots.items():
        with open(path, "rb") as f:
            if f.read() != snap:
                dirty.append(os.path.relpath(path, ROOT))
    if dirty:
        print("[复原] ✗✗ 工作树与启动快照不一致，电池必须立刻修：%s" % dirty)
        return 3
    print("[复原] %d 个被变异文件与启动快照逐字节一致 ✓" % len(snapshots))

    print("\n=== 总结：抓住 %d / 逃逸 %d / INVALID %d ===" % (len(caught), len(escaped), len(invalid)))
    for mid, label in escaped:
        print("  逃逸: %s %s" % (mid, label))
    for mid, label in invalid:
        print("  INVALID: %s %s" % (mid, label))
    return 1 if (escaped or invalid) else 0


if __name__ == "__main__":
    sys.exit(main())
