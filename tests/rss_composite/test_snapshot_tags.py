# tests/rss_composite/test_snapshot_tags.py
# -*- coding: utf-8 -*-
"""tags 穿透构建期快照通道 + 管线顺序守卫（issue M5）

M5 的完整链路有三处会丢 tags，缺一处都会让打标成为死数据：
  1. _save_api_snapshot()   构造快照 item 时字段白名单只有 t/u/s/d
  2. buildArt()             客户端构造 ART 时字段白名单不含 tags
  3. _mergeRemoteSources()  客户端合并刷新结果时字段白名单不含 tags
（2/3 由 test_refresh_tags_js.py 覆盖，本文件覆盖 1 与管线顺序。）

另有一条顺序不变量：_save_api_snapshot() 必须晚于 _tag_articles()，
否则快照构造时 tags 还没被算出来（现网就是写成在前，tags 恒为空）。
"""
import io
import json
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HERE = os.path.dirname(os.path.abspath(__file__))

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


print("=" * 74)
print("A. _save_api_snapshot 携带 tags（临时 CWD，不碰仓库真快照）")
print("=" * 74)

tmp = tempfile.mkdtemp(prefix="api_snap_")
probe = os.path.join(HERE, "_probe_api_snapshot.py")
p = subprocess.run([sys.executable, probe, tmp], cwd=ROOT, capture_output=True, text=True,
                   encoding="utf-8", errors="replace")
raw = (p.stdout or "").strip().splitlines()
try:
    got = json.loads(raw[-1]) if raw else {}
except Exception as e:
    got = {"error": "解析探针输出失败: %s / %r" % (e, raw)}

ok(not got.get("error"), "A0 探针正常执行（error=%r）" % (got.get("error"),))
eq(got.get("n"), 3, "A1 快照含 3 篇")
eq(got.get("tags", [None, None, None])[0], ["openai", "大模型"],
   "A2 带 tags 的条目：tags 穿透到快照")
ok(got.get("tags", ["x"])[1] in ("<absent>", None, []),
   "A3 无 tags 的条目：不伪造（实得 %r）" % (got.get("tags", ["x"])[1],))
ok(got.get("tags", ["x"])[2] in ("<absent>", None, []),
   "A4 空 tags 列表：不写入空数组（省字节，实得 %r）" % (got.get("tags", ["x"])[2],))
ok("tags" not in (got.get("keys1") or ["tags"]),
   "A5 无 tags 的条目连键都不出现（不增加无谓体积，keys=%r）" % (got.get("keys1"),))
ok("tags" in (got.get("keys0") or []),
   "A6 有 tags 的条目含 tags 键（keys=%r）" % (got.get("keys0"),))

print()
print("=" * 74)
print("B. 管线顺序：打标必须先于快照构造与分块写出")
print("=" * 74)

_src_path = os.environ.get("RSS_BUILD_SRC") or os.path.join(ROOT, "build_rss_aggregator.py")
src_text = io.open(_src_path, encoding="utf-8").read()


def _call_pos(body, name):
    """定位**语句级**调用位置（返回首个匹配的偏移，找不到返回 -1）。

    为什么不能直接用 body.find(name + "(")：
    main() 里有一行注释写着「调用位置在 _tag_articles() 之后：…」，
    它恰好排在 `_save_api_snapshot(` 之前 —— 用裸 find 会把注释里的名字
    当成调用位置，于是「快照先于打标」这种回退**骗过** B4 顺序断言。
    本函数要求名字出现在行首缩进之后（即语句位置），注释行（# 打头）不匹配。
    自检见 B7。
    """
    for m in re.finditer(r"^[ \t]*%s[ \t]*\(" % re.escape(name), body, re.M):
        return m.start()
    return -1


mi = src_text.find("def main(")
ok(mi >= 0, "B1 源码中存在 main()")
main_body = src_text[mi:] if mi >= 0 else ""
i_tag = _call_pos(main_body, "_tag_articles")
i_snap = _call_pos(main_body, "_save_api_snapshot")
i_wdc = _call_pos(main_body, "write_data_chunks")
ok(i_tag >= 0, "B2 main() 调用了 _tag_articles()")
ok(i_snap >= 0, "B3 main() 调用了 _save_api_snapshot()")
ok(i_tag >= 0 and i_snap >= 0 and i_tag < i_snap,
   "B4 _tag_articles() 必须先于 _save_api_snapshot()（tag=%d, snap=%d）" % (i_tag, i_snap))
ok(i_tag >= 0 and i_wdc >= 0 and i_tag < i_wdc,
   "B5 _tag_articles() 必须先于 write_data_chunks()（tag=%d, chunk=%d）" % (i_tag, i_wdc))

# B7：定位器自检 —— 注释中的同名调用不得被当成调用位置
_probe = ("    # 调用位置在 _tag_articles() 之后：快照构造只搬用白名单字段\n"
          "    _save_api_snapshot(x)\n"
          "    _tag_articles(y)\n")
ok(_call_pos(_probe, "_tag_articles") > _call_pos(_probe, "_save_api_snapshot"),
   "B7 定位器跳过注释中的同名调用（防 B4 被注释骗过）")

# B6：快照 item 必须真的搬用 tags（静态守卫：防止只调顺序、忘了搬字段）
seg = main_body  # 快照构造在 _save_api_snapshot 函数体内，不在 main
fi = src_text.find("def _save_api_snapshot(")
fj = src_text.find("def ", fi + 5)
fn_body = src_text[fi:fj] if fi >= 0 and fj > fi else ""
ok('"tags"' in fn_body, "B6 _save_api_snapshot() 函数体搬用 tags 字段")

print()
print("=" * 74)
print("RESULT: %d passed, %d failed" % (PASS, FAIL))
if failures:
    for f in failures:
        print("  - %s" % f)
sys.exit(1 if FAIL else 0)
