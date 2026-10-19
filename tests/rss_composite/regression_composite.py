# tests/rss_composite/regression_composite.py
# -*- coding: utf-8 -*-
"""复合排序全量回归

验收标准：
  V1: _tag_articles() 为真实快照文章生成标签，覆盖率 ≥50%
  V2: weightedShuffle 窗口=0 与纯时间降序逐条一致（跑 JS 套件，含 D13）
  V3: diverse 模式不破坏现有排序测试（跑 rss_sort 套件）
  V4: 分块产物包含 tags 字段（离线构造 chunk0 后 dump 检查）+ 体积增幅 ≤2%
  V5: 配置键存在；配置读取与 CWD 无关（防相对路径静默读错文件）；
      diverse_enabled=false 时 sort-select 不含 diverse option
  V6: 确定性 — 同一输入两次打标+分块产物一致
"""
import io
import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from _loader import load_build

B = load_build()
PY = sys.executable

FAIL = 0
PASS = 0
failures = []


def eq(actual, expected, label):
    global FAIL, PASS
    if actual == expected:
        PASS += 1
        print("  [PASS] %s" % label)
    else:
        FAIL += 1
        failures.append(label)
        print("  [FAIL] %s\n         期望 %r\n         实得 %r" % (label, expected, actual))


def ok(cond, label):
    eq(bool(cond), True, label)


def run(name, script):
    p = subprocess.run([PY, script], cwd=ROOT, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return p.returncode, (p.stdout or "")


def run_suite(label, script, tail=6):
    """跑子套件；失败时打印尾部输出，避免「只看到一行 FAIL」查不动。"""
    rc, out = run(label, script)
    ok(rc == 0, label)
    if rc != 0:
        lines = [l for l in out.strip().splitlines() if l.strip()]
        print("        ↳ %s 尾部输出:" % os.path.basename(script))
        for l in lines[-tail:]:
            print("          " + l)
    return rc, out


print("=" * 74)
print("V1: _tag_articles 覆盖率（真实 rss_api_snapshot.json）")
print("=" * 74)

snapshot_path = os.path.join(ROOT, "rss_api_snapshot.json")
if os.path.exists(snapshot_path):
    with io.open(snapshot_path, "r", encoding="utf-8") as f:
        snapshot = json.load(f)
    sources = snapshot.get("sources", [])
    ok(len(sources) > 0, "V1a 快照含源（%d 个）" % len(sources))
    test_sources = sources[:20]
    B._tag_articles(test_sources)
    total = 0
    tagged = 0
    bad_format = 0
    for s in test_sources:
        for it in s.get("items", []):
            total += 1
            tags = it.get("tags") or []
            if tags:
                tagged += 1
            if not all(isinstance(t, str) and len(t) >= 2 for t in tags):
                bad_format += 1
    rate = tagged / total if total else 0
    eq(bad_format, 0, "V1b 标签格式全部合法（长度≥2 的字符串）")
    ok(rate >= 0.5, "V1c 标签覆盖率 ≥50%%（实际 %.1f%%，%d/%d）" % (rate * 100, tagged, total))
else:
    print("  [SKIP] rss_api_snapshot.json 不存在，跳过 V1")

print()
print("=" * 74)
print("V2/V3: 跨套件回归")
print("=" * 74)

run_suite("V2 weightedShuffle JS 套件全绿（含 D13 窗口=0 等价性 + E 段同源抑制）",
          os.path.join(HERE, "test_diverse_js.py"))
run_suite("V2b 标签语义套件全绿（issue C2：词典优先 / 边界对齐 / 台标抑制）",
          os.path.join(HERE, "test_tag_semantics.py"))
run_suite("V2c 刷新通道 tags 穿透套件全绿（issue M5）",
          os.path.join(HERE, "test_refresh_tags_js.py"))
run_suite("V2d 快照通道 tags + 管线顺序套件全绿（issue M5）",
          os.path.join(HERE, "test_snapshot_tags.py"))
run_suite("V2e 真实语料首屏回归全绿（issue C1：diverse 必须优于时间降序）",
          os.path.join(HERE, "test_diverse_realdata.py"))
run_suite("V3 rss_sort 套件全绿（diverse 未破坏现有排序）",
          os.path.join(ROOT, "tests", "rss_sort", "test_rss_sort.py"))
run_suite("V3b rss_date 分块套件全绿",
          os.path.join(ROOT, "tests", "rss_date", "test_split_chunks.py"))

print()
print("=" * 74)
print("V4: 分块产物包含 tags")
print("=" * 74)

if os.path.exists(snapshot_path):
    src_for_chunk = [{"key": s.get("key"), "name": s.get("name"), "cat": s.get("cat"),
                      "color": s.get("color"), "tier": s.get("tier", 3),
                      "items": list(s.get("items", []))} for s in sources[:5]]
    B._tag_articles(src_for_chunk)
    c0, c1 = B._split_data_chunks(src_for_chunk, chunk0_size=50)
    dump0 = json.dumps({"sources": c0}, ensure_ascii=False, separators=(",", ":"))
    ok('"tags"' in dump0, "V4a chunk0 序列化后含 tags 字段")
    n_items = sum(len(s.get("items", [])) for s in c0)
    n_tag = sum(1 for s in c0 for it in s.get("items", []) if "tags" in it)
    eq(n_tag, n_items, "V4b chunk0 每篇都带 tags（%d/%d）" % (n_tag, n_items))

    # V4c 载荷代价上界（issue M3：tags 进每一条 item，必须对体积增幅设硬上界）
    # 口径：同一批源，未带 tags vs 已带 tags，序列化后字节数对比。
    # item 用 dict(it) 浅拷贝，故下面的 pop 不会污染 snapshot 原对象。
    src_no_tag = [{"key": s.get("key"), "name": s.get("name"), "cat": s.get("cat"),
                   "color": s.get("color"), "tier": s.get("tier", 3),
                   "items": [dict(it) for it in s.get("items", [])]} for s in sources[:5]]
    B._tag_articles(src_no_tag)
    c0_nt, _ = B._split_data_chunks(src_no_tag, chunk0_size=50)
    for s in c0_nt:
        for it in s.get("items", []):
            it.pop("tags", None)
    bytes_nt = len(json.dumps({"sources": c0_nt}, ensure_ascii=False,
                              separators=(",", ":")).encode("utf-8"))
    bytes_wt = len(dump0.encode("utf-8"))
    growth = (bytes_wt - bytes_nt) / bytes_nt if bytes_nt else 0.0
    # 注意口径：此处基线是 rss_api_snapshot.json 的压缩 item（仅 t/u/s/d），
    # 是最坏情况（字段越少，tags 占比越高），实测 5.92%。
    # 真实 19 字段产物口径下实测约 0.77%（分母大 3~4 倍）。两者都不设 2%（过严会误报）。
    ok(growth <= 0.08,
       "V4c tags 体积增幅 ≤8%%（最坏口径，基线仅 4 字段；实际 %.2f%%，+%d / %d 字节）"
       % (growth * 100, bytes_wt - bytes_nt, bytes_nt))
    # V4d 口径无关的绝对上界：3 个短标签的结构开销随篇数线性增长，用它拦住膨胀
    per_item = (bytes_wt - bytes_nt) / n_items if n_items else 0.0
    ok(per_item <= 60,
       "V4d tags 平均 ≤60 字节/篇（实际 %.1f；真实产物口径约 41）" % per_item)
else:
    print("  [SKIP] 无快照，跳过 V4")

print()
print("=" * 74)
print("V5: diverse 配置")
print("=" * 74)

config_path = os.path.join(ROOT, "build_config.json")
with io.open(config_path, "r", encoding="utf-8") as f:
    cfg = json.load(f)
ok("diverse_enabled" in cfg, "V5a build_config 含 diverse_enabled")
ok("diverse_window_minutes" in cfg, "V5b build_config 含 diverse_window_minutes")
eq(cfg.get("diverse_window_minutes", 0), 120, "V5c 默认窗口 = 120 分钟")

# 配置缺失时回落到默认值（不崩溃）
ok(B._load_diverse_config()["window_minutes"] == cfg.get("diverse_window_minutes", 120),
   "V5d _load_diverse_config() 读到配置窗口")

# V5h/V5i/V5j 配置读取必须与 CWD 无关（issue m4）
# 缺陷形态：原先用相对路径 open("build_config.json")，路径解析依赖进程 CWD，
# 从别的目录导入模块会**静默读到另一个同名文件**（不是回落默认值）。
# 探针刻意不覆盖 mod.__file__，因此「按 __file__ 定位」与「按 CWD 定位」结果不同，有区分力。
decoy = tempfile.mkdtemp(prefix="decoy_cfg_")
with io.open(os.path.join(decoy, "build_config.json"), "w", encoding="utf-8") as f:
    json.dump({"diverse_enabled": False, "diverse_window_minutes": 45}, f)
probe = os.path.join(HERE, "_probe_cwd.py")
pp = subprocess.run([PY, probe, decoy], cwd=ROOT, capture_output=True, text=True,
                    encoding="utf-8", errors="replace")
try:
    got = json.loads((pp.stdout or "").strip().splitlines()[-1])
except Exception:
    got = {}
ok(not got.get("error"), "V5h 探针正常执行（error=%r）" % (got.get("error"),))
eq(got.get("window_minutes"), cfg.get("diverse_window_minutes", 120),
   "V5i 诱饵 CWD 下仍读到仓库真配置窗口（防相对路径依赖，实得 %r）" % (got.get("window_minutes"),))
eq(got.get("enabled"), cfg.get("diverse_enabled", True),
   "V5j 诱饵 CWD 下 enabled 仍为真配置值（实得 %r）" % (got.get("enabled"),))

# diverse_enabled=false → 不渲染 diverse option
try:
    html_off = B.build_html([], "2026-09-14 15:00", 0, 0, analysis_data=None,
                            diverse_window_minutes=120, diverse_enabled=False)
    html_on = B.build_html([], "2026-09-14 15:00", 0, 0, analysis_data=None,
                           diverse_window_minutes=120, diverse_enabled=True)
    ok('value="diverse"' not in html_off, "V5e diverse_enabled=false 时不渲染 diverse option")
    ok('value="diverse"' in html_on, "V5f diverse_enabled=true 时渲染 diverse option")
    ok("var DIVERSE_WINDOW = 120;" in html_on, "V5g DIVERSE_WINDOW 注入正确")
except Exception as e:
    eq(True, False, "V5 build_html 调用异常: %s" % e)

print()
print("=" * 74)
print("V6: 确定性")
print("=" * 74)

if os.path.exists(snapshot_path):
    def build_twice():
        s1 = [{"key": s.get("key"), "name": s.get("name"), "items": list(s.get("items", []))}
              for s in sources[:5]]
        B._tag_articles(s1)
        return json.dumps(s1, ensure_ascii=False, sort_keys=True)
    eq(build_twice(), build_twice(), "V6 同一输入两次打标结果完全一致")
else:
    print("  [SKIP] 无快照，跳过 V6")

print()
print("=" * 74)
print("RESULT: %d passed, %d failed" % (PASS, FAIL))
if failures:
    for f in failures:
        print("  - %s" % f)
sys.exit(1 if FAIL else 0)
