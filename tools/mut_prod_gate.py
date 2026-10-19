# -*- coding: utf-8 -*-
"""生产验证门槛的变异电池：`tests/tools/test_prod_verify_verdict.py` 能不能被捅穿。

靶是**两个模块**（`tools/prod_verify.py` 的结论表 + `tools/data_api_push.py` 的接线），
所以副本目录里两份都要放；判据通过 `STARHUB_TOOLS_DIR` 指到副本（与 mut_push_gate 同形状，
import 型电池必须每次清 __pycache__，否则两条改完字节数相同的变异会互相污染 —— 2026-10-02 实踩）。

为什么这道门槛特别需要变异：它的作用是"拦住我"，而拦不住人的门槛和没有一样
（要么误伤到逼人加绕过路径，要么一看就永远不响）。所以正向（该拦的拦住）与
反向（不该拦的别拦）都要有靶。
"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PV_SRC = os.path.join(ROOT, "tools", "prod_verify.py")
DP_SRC = os.path.join(ROOT, "tools", "data_api_push.py")
DIR = os.path.join(ROOT, ".deploy-tmp", "_mut_prod_gate")
TEST = os.path.join("tests", "tools", "test_prod_verify_verdict.py")

NO_STAMP = "test_pusher_refuses_when_no_prod_stamp"
BLOCKING = "test_pusher_refuses_when_production_is_blocking"
STALE = "test_pusher_refuses_on_stale_stamp"
WARN_SHOWN = "test_pusher_allows_warn_but_shows_the_reason"
DEAD_PAGE = "test_dead_page_is_blocking"
EMPTY = "test_empty_readings_are_blocking_not_ok"
FRESH = "test_stamp_shape_and_freshness"
OK = "test_healthy_production_is_ok"
SEL1 = "test_internal_selector_keeps_only_real_exposure"
SEL2 = "test_internal_selector_is_not_silently_empty"
MISS = "test_missing_baseline_is_not_read_as_closed"
INFLIGHT = "test_in_flight_run_is_not_blamed_for_not_publishing"
ONLYFLIGHT = "test_only_in_flight_run_is_not_ok"
CHANNEL_API = "test_channel_blocked_is_not_read_as_broken"
CHANNEL_PAGES = "test_channel_blocked_pages_still_block"

pv = open(PV_SRC, encoding="utf-8").read()
dp = open(DP_SRC, encoding="utf-8").read()


def sub(text, old, new):
    return text.replace(old, new, 1) if old in text else None


# 每条变异改的是 (prod_verify 或 data_api_push) 的哪一处，靶是哪些判据
CASES = [
    ("P1 没跑过验证也放行（门槛形同虚设）",
     ("dp", sub(dp, '        return False, ("没做过生产验证',
                    '        return True, ("没做过生产验证')),
     "red", [NO_STAMP]),
    ("P2 调用点不再检查时间戳（过期照样推）",
     ("dp", sub(dp, "    if not PV.stamp_is_fresh(stamp):", "    if False:")),
     "red", [STALE]),
    ("P2b 时间戳新鲜度判断永远返回真（一次验证永久复用）",
     ("pv", sub(pv, "    return isinstance(ts, (int, float)) and (now - ts) < MAX_AGE_S",
                    "    return True")),
     "red", [STALE, FRESH]),
    ("P3 站点 404 只降级成 warn（拦不住坏站点）",
     ("pv", sub(pv, '            return "blocking", "站点页面 %s 是 %s',
                       '            return "warn", "站点页面 %s 是 %s')),
     "red", [DEAD_PAGE, CHANNEL_PAGES]),
    ("P4 空读数判 ok（网络一抖就放行）",
     ("pv", sub(pv, '    if not (r.get("pages") and r.get("api")):\n        return "blocking"',
                  '    if not (r.get("pages") and r.get("api")):\n        return "ok"')),
     "red", [EMPTY]),
    ("P5 放行时不带 warn 原文（日志里看不到生产状态）",
     ("dp", sub(dp, '    note = "；提醒：%s" % stamp.get("why") if stamp.get("level") == "warn" else ""',
                  '    note = ""')),
     "red", [WARN_SHOWN]),
    ("P6 blocking 的理由被吞（只剩一句形容词）",
     ("dp", sub(dp, '        return False, "生产验证判为 blocking：%s" % stamp.get("why", "（没时间戳里的理由）")',
                  '        return False, "生产验证判为 blocking"')),
     "red", [BLOCKING]),
    ("P7 内部项筛法一条都不挑（公开面被假报成已关）",
     ("pv", sub(pv, '            if v == "200" and not k.startswith(RUNTIME_KEEP)',
                  '            if False and not k.startswith(RUNTIME_KEEP)')),
     "red", [SEL1, SEL2]),
    ("P8 改前基线缺失时返回空（问不出来=没问题）",
     ("pv", sub(pv, '        return ["(改前基线缺失：无法核对公开面，先重跑 _verify_build 的枚举再谈收口)"]',
                  "        return []")),
     "red", [MISS]),
    ("P9 把在飞的场当已结束（Pages 结论 null 也拿去判）",
     ("pv", sub(pv, "    done = [x for x in runs if x.get(\"pages\") is not None]",
                  "    done = runs")),
     "red", [INFLIGHT, ONLYFLIGHT]),
    ("P10 全场都在跑时判 ok（没有发布事实却说没事）",
     ("pv", sub(pv, '    if not done:\n        return "blocking"',
                  '    if not done:\n        return "ok"')),
     "red", [ONLYFLIGHT]),
    # ── 反向控制：不该拦的别拦，否则门槛明天就被绕过 ──
    ("G1 一切正常仍判 ok（必须绿：门槛不许误伤）",
     ("pv", pv.replace('    stuck = [x for x in finished_vercel if x.get("vercel") != "success"]',
                       '    stuck = [x for x in finished_vercel if x.get("vercel") not in ("success", "skipped")]', 1)),
     "green", ""),
    ("G2 文案改词（必须绿：判据钉的是行为不是措辞）",
     ("dp", sub(dp, "生产验证时间戳已过期", "生产验证已经过期很久")),
     "green", ""),
    ("G3 运行时白名单多列一项（必须绿：放宽清单不该误红）",
     ("pv", sub(pv, 'RUNTIME_KEEP = PAGES + API + ("rss_sources.json",)',
                  'RUNTIME_KEEP = PAGES + API + ("rss_sources.json", "hot_snapshot.json")')),
     "green", ""),
]


def run():
    env = dict(os.environ, STARHUB_TOOLS_DIR=DIR, PYTHONIOENCODING="utf-8",
               PYTHONDONTWRITEBYTECODE="1")
    p = subprocess.run([sys.executable, "-B", "-m", "pytest", TEST, "-q", "--no-header",
                        "-p", "no:cacheprovider", "--tb=line"],
                       cwd=ROOT, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=env, timeout=600)
    lines = (p.stdout or "").strip().splitlines()
    bad = sorted({l.split("::")[-1].split(" ")[0]
                  for l in lines if l.startswith(("FAILED", "ERROR"))})
    return p.returncode != 0, (lines[-1] if lines else "?"), bad


def write(target, body):
    """两份都要落地：data_api_push 会 `import prod_verify`，缺一份就变成 import 失败而不是变异。
    清 __pycache__ 是必须的（见模块 docstring：两条改完字节数相同的变异会互相污染）。"""
    os.makedirs(DIR, exist_ok=True)
    cache = os.path.join(DIR, "__pycache__")
    if os.path.isdir(cache):
        for f in os.listdir(cache):
            os.remove(os.path.join(cache, f))
        os.rmdir(cache)
    with open(os.path.join(DIR, "prod_verify.py"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(body if target == "pv" else pv)
    with open(os.path.join(DIR, "data_api_push.py"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(body if target == "dp" else dp)
    real = "prod_verify.py" if target == "pv" else "data_api_push.py"
    if open(os.path.join(DIR, real), encoding="utf-8").read() != body:
        raise AssertionError("副本写进去和读出来不一致 ⇒ 变异没落地，这次结果不作数")
    cp = subprocess.run([sys.executable, "-B", "-m", "py_compile",
                         os.path.join(DIR, "data_api_push.py"),
                         os.path.join(DIR, "prod_verify.py")], capture_output=True, text=True)
    if cp.returncode:
        # 语法错的副本会让 pytest 报"1 error"，看起来像红、其实什么都没测（2026-10-03 实踩）
        raise AssertionError("变异副本编译不过 ⇒ 这条变异是无效红，不算挡住：%s"
                             % (cp.stderr or "").strip()[-200:])


def main():
    write("dp", dp)
    red, tail, bad = run()
    print("基线（未变异副本）：%s  rc=%d" % (tail, 1 if red else 0))
    if red:
        print("基线不绿 ⇒ 本次不声称任何覆盖率；失败：%s" % bad)
        return 1
    problems = []
    for label, spec, want, targets in CASES:
        target, body = spec
        if body is None:
            problems.append("%s INVALID（锚不在当前文件里）" % label)
            continue
        base = pv if target == "pv" else dp
        if body == base:
            # GREEN 用例落空同样是无效：一条"什么都没改还必然绿"的控制项等于没有控制项
            problems.append("%s INVALID（变异没落到字节上，%s用例都不作数）"
                            % (label, "红" if want == "red" else "绿"))
            continue
        write(target, body)
        red, tail, bad = run()
        ok = (red == (want == "red"))
        if ok and want == "red":
            missed = [t for t in targets if t not in bad]
            extra = sorted(set(bad) - set(targets))
            if missed:
                ok = False
                tail += " 该红的没红：" + ",".join(missed)
            if extra:
                ok = False
                tail += " 红了但不是自己的靶：" + ",".join(extra)
        print("%-56s %s %s" % (label, "RED  " if red else "GREEN", tail[:58]
                              + ("" if ok else "   <- 期望%s" % ("红" if want == "red" else "绿"))))
        if not ok:
            problems.append(label + ("（没挡住/红错对象）" if want == "red" else "（误伤）"))
    for f in ("prod_verify.py", "data_api_push.py"):
        fp = os.path.join(DIR, f)
        if os.path.exists(fp):
            os.remove(fp)
    cache = os.path.join(DIR, "__pycache__")
    if os.path.isdir(cache):
        for f in os.listdir(cache):
            os.remove(os.path.join(cache, f))
    print("\n问题条目：%s" % (problems if problems else "无"))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
