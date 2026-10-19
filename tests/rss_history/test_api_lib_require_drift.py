# tests/rss_history/test_api_lib_require_drift.py
# -*- coding: utf-8 -*-
"""防漂移判据：`api/*.js` 里每一个 `require('../lib/X.js')` 都必须有归宿（A2 已接线目录）。

现场（2026-10-04 实测，6 条 A2 判据红）：Task 4 给 `api/rss.js` 新增了**硬** require
`../lib/body_rules.js`，而两个 CJS 转译 harness（`test_api_dfb_wire.py`、
`test_retention_api_behavior.py`）是**逐条硬写 lib 名单**改写的，只登记了 retention 一条。
转译产物落在 `tests/rss_js/` 下 ⇒ `../lib/` 被 Node 解析成 `tests/lib/` ⇒ `MODULE_NOT_FOUND`。

为什么这条回归能活到提交：`api/rss.js` 里同样没被改写的 `require('../lib/rss_cover.js')`
**不崩**，因为它在 api 侧是 try/catch 的可选降级写法 —— 于是"漏登记"看起来是无害的，
只有硬 require 才炸。本判据把这个不对称钉死：

① 每个 `require('../lib/X.js')` 要么 (a) 被可选降级包裹（try + 不 rethrow 且**出声**的
   catch，且必须在本文件的 OPTIONAL_OK 里写明是哪一条、为什么可以降级），
   要么 (b) 被转译层的"扫源码自动发现"覆盖到、且 lib 文件真实存在（= 已登记）；
② 两个转译 harness 必须继续用自动发现，不许退回逐条硬写名单；
   **派工 F3 之后这一条不再只是 grep**：四条文本断言仍在（一条没删），但它们后面
   多了"把 harness 里那份 `rewriteLibRequires` 切出来在 node 里真跑一次"，
   并与本文件的 Python 镜像逐字段对账 ⇒ 名单式实现会在行为上立刻对不上
   （`test_hardcoded_lib_list_turns_the_harness_red` 就是这次改动的红证据）；
③ `body_rules` 必须是**硬 require**：不许出现 `try { … body_rules … } catch` 的形状。
   包成可选 = 正文不规范化就出厂（半个标签 / 丢图），正是本批要消灭的那个故障本身，
   所以"绕过"这条路要判红，而不是让它绿过去。

反例自证（都在 pytest 的 %TEMP% 副本上做，绝不写工作树）：
- 往 api/rss.js 副本加一句 `require('../lib/_not_registered_probe.js')` ⇒ 判据必须变红；
- 把 body_rules 包成 try/catch ⇒ 判据必须变红；
- 把 harness 的替换函数换成硬编码三 lib 名单 ⇒ 行为半必须变红（F3）。
"""
import json
import os
import re
import shutil
import subprocess
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
API_DIR = os.path.join(ROOT, "api")
LIB_DIR = os.path.join(ROOT, "lib")

# 转译层自动发现用的那条正则（与两个 harness 里的 JS 字面量**逐字相同**，见
# test_transpile_harnesses_use_autodiscovery_not_a_hardcoded_list：两边写歪一个字就红）。
HARNESS_RE_DECL = r'''const LIB_REQUIRE_RE = /require\(\s*(['"])\.\.\/lib\/([A-Za-z0-9_.\-]+\.js)\1\s*\)/g;'''
_JS_BODY = re.search(r"const LIB_REQUIRE_RE = /(.*)/g;", HARNESS_RE_DECL).group(1)
LIB_REQUIRE_RE = re.compile(_JS_BODY.replace(r"\/", "/"))

# harness 里"换实现"的覆盖表：retention 必须是唯一保留覆盖语义的一条。
HARNESS_OVERRIDE_KEY = "'rss_retention.js': "

# 允许降级的 lib（(a) 分支）。理由必须写在这里 —— 「悄悄包成可选」就是本批翻过的那个形态。
OPTIONAL_OK = {
    "rss_retention.js":
        "运行时留存闸门：装载失败只是「少过滤一轮」，条目照旧出厂；api/rss.js 会 console.error "
        "+ ::warning 出声，并在 X-RSS-Retention 里留 unavailable（计算期异常同样留痕）。"
        "「宁可放过不可 500」是 2026-09-20 对抗审查 P0-2/P1-2 定下的口径：闸门挂掉不该把端点打死。",
    "rss_cover.js":
        "实时封面抽取：装载失败只影响「新文章少一张封面卡」，标题/链接/时间/正文都还在，"
        "属于可出声放过的非致命降级（catch 里 console.error + ::warning 留痕）。",
}

# 必须是硬 require 的 lib（不许出现在 OPTIONAL_OK 里）。
HARD_REQUIRED = {
    "body_rules.js":
        "运行时正文规范层：降级就等于正文不规范化、不码点截断就出厂（半个标签 / 丢图），"
        "这正是 Task 4 要消灭的故障本身，所以它只能「装不上就 500」，不许套可选降级。",
}

# 老写法（逐条硬写名单）的形状：一旦出现就说明有人把自动发现改回去了。
BANNED_HARDCODE_SHAPE = "src.replace(\"require('../lib/"

HARNESS_FILES = (
    os.path.join("tests", "rss_history", "test_api_dfb_wire.py"),
    os.path.join("tests", "rss_history", "test_retention_api_behavior.py"),
)

AUDIBLE = ("console.error", "console.warn", "console.log", "::warning")


# ─────────────────────────── 源码扫描（注释/字符串安全）───────────────────────────

def _mask(text, drop_strings):
    """把注释（以及可选地把字符串字面量内容）替换成空格，长度与位置逐字对齐。"""
    out = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if c == "/" and nxt == "/":
            j = text.find("\n", i)
            j = n if j < 0 else j
            out.append("".join(ch if ch == "\n" else " " for ch in text[i:j]))
            i = j
        elif c == "/" and nxt == "*":
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out.append("".join(ch if ch == "\n" else " " for ch in text[i:j]))
            i = j
        elif c in ("\"", "'", "`"):
            j = i + 1
            while j < n:
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] == c:
                    break
                j += 1
            end = min(j + 1, n)
            seg = text[i:end]
            if drop_strings and len(seg) > 1:
                out.append(seg[0] + "".join(ch if ch == "\n" else " " for ch in seg[1:-1]) + seg[-1])
            else:
                out.append(seg)
            i = end
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _match_brace(view, open_idx):
    """view 已抹掉字符串内容 ⇒ 这里的 {} 一定是真代码括号。"""
    depth = 0
    i = open_idx
    while i < len(view):
        ch = view[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _optional_degradation_at(code, view, pos):
    """pos 是否落在"try { … } catch (e) { 不出 throw 且出声 }"里。返回 (是否, 说明)。"""
    best = None
    for tm in re.finditer(r"\btry\s*\{", view):
        open_i = tm.end() - 1
        close_i = _match_brace(view, open_i)
        if close_i < 0 or not (open_i < pos < close_i):
            continue
        after = view[close_i + 1:]
        lead = len(after) - len(after.lstrip())
        cm = re.match(r"catch\s*(?:\([^)]*\))?\s*\{", after[lead:])
        if not cm:
            continue          # try/finally 或 try 单独出现 ⇒ 没有降级分支
        c_open = close_i + 1 + lead + cm.end() - 1
        c_close = _match_brace(view, c_open)
        if c_close < 0:
            continue
        if best is None or open_i > best[0]:
            best = (open_i, code[c_open + 1:c_close])
    if best is None:
        return False, "不在任何 try/catch 里"
    body = best[1]
    if re.search(r"\bthrow\b", body):
        return False, "catch 里 rethrow ⇒ 不是降级"
    if not any(sig in body for sig in AUDIBLE):
        return False, "catch 里不出声 ⇒ 静默降级（本项目付过两次代价的形态）"
    return True, "可选降级（try + 不 rethrow 且出声的 catch）"


def lib_requires(text):
    """扫出一个 api 源文件里所有 require('../lib/X.js')：[{name, line, optional, note}]。"""
    code = _mask(text, drop_strings=False)     # 注释抹掉、字符串留着（正则要看得到路径）
    view = _mask(text, drop_strings=True)      # 注释与字符串都抹掉（{} 才是真括号）
    found = []
    for m in LIB_REQUIRE_RE.finditer(code):
        optional, note = _optional_degradation_at(code, view, m.start())
        found.append({
            "name": m.group(2),
            "line": code.count("\n", 0, m.start()) + 1,
            "optional": optional,
            "note": note,
            "raw": m.group(0),
        })
    return found


def api_sources(api_dir=API_DIR):
    for name in sorted(os.listdir(api_dir)):
        if name.endswith(".js"):
            yield name, open(os.path.join(api_dir, name), encoding="utf-8").read()


# ─────────────────────────── 判据核心：审计（可对工作树外的副本复用）───────────────────────────

def rewrite_lib_requires(source, root, overrides):
    """Python 版转译层自动发现（与 harness 的 rewriteLibRequires 同口径，用于反例自证）。"""
    names = []

    def _sub(m):
        name = m.group(2)
        names.append(name)
        target = overrides[name] if name in overrides else os.path.join(root, "lib", name)
        return "require(%s)" % json.dumps(target.replace("\\", "/"))

    out = LIB_REQUIRE_RE.sub(_sub, source)
    if re.search(r"""require\(\s*['"]\.\./lib/""", out):
        raise AssertionError("转译后仍有相对 ../lib 依赖：自动发现口径没盖住这种写法")
    return out, names


def transpile_for_cjs(source, root, overrides):
    """harness 的完整转译：ESM → CJS + lib 自动发现。"""
    out = source.replace("import { readFileSync } from 'fs';", "const { readFileSync } = require('fs');")
    out = out.replace("import { join } from 'path';", "const { join } = require('path');")
    out = out.replace("export default async function handler", "module.exports = async function handler")
    return rewrite_lib_requires(out, root, overrides)


def audit(text, label, lib_dir=LIB_DIR):
    """返回问题清单（空 = 每一条 require 都有归宿）。"""
    problems = []
    for req in lib_requires(text):
        target = os.path.join(lib_dir, req["name"])
        if req["optional"]:
            if req["name"] not in OPTIONAL_OK:
                problems.append(
                    "%s:%d ../lib/%s 被包成了可选降级，但它不在 OPTIONAL_OK 白名单里 —— "
                    "谁批准降级的？硬 require 才是正文规范层的默认（白名单：%s）"
                    % (label, req["line"], req["name"], sorted(OPTIONAL_OK)))
            continue
        # (b) 分支：必须被自动发现覆盖到，且目标文件真实存在（= 有人登记了这个 lib）
        covered, names = rewrite_lib_requires(text, ROOT, {"rss_retention.js": "OVERRIDE"})
        if req["name"] not in names:
            problems.append("%s:%d ../lib/%s 没被转译层自动发现覆盖到 ⇒ 生成件里的相对路径会指向 tests/lib/"
                            % (label, req["line"], req["name"]))
        if not os.path.isfile(target):
            problems.append("%s:%d 硬 require ../lib/%s，但 %s 不存在 ⇒ 又加了一个没人登记的 lib"
                            "（构建期 Vercel 只带 lib/ 里的真实文件，缺一个上线就 500）"
                            % (label, req["line"], req["name"], os.path.join("lib", req["name"])))
    for name, why in HARD_REQUIRED.items():
        if any(r["name"] == name and r["optional"] for r in lib_requires(text)):
            problems.append("%s 把 ../lib/%s 包成了 try/catch 可选降级 —— 不允许：%s" % (label, name, why))
    return problems


# ─────────────────────────── harness 的**实跑**口径（派工 F3）───────────────────────────
# 四条文本断言（存在性子串）钉不住"函数体还在不在"：把 rewriteLibRequires 换成硬编码三 lib
# 名单，只要 `const LIB_REQUIRE_RE = /…/g;` 那行逐字留着，四条全绿（审查实测）。
# 所以这里补上真正的执行：从 harness 源文件里切出**那份实现**，在 node 里跑同一份探针输入，
# 再与本文件的 Python 镜像逐字段对账 ⇒ 名单变了必须反映在行为上。
PROBE_SRC = (u"const BODY = require('../lib/body_rules.js');\n"
             u"const BRAND = require('../lib/brand_new_probe.js');\n"
             u"const RET = require('../lib/rss_retention.js');\n")
PROBE_OVERRIDES = {"rss_retention.js": "OVERRIDE_RETENTION"}
# 探针里三条 require 的**顺序**就是 names 的期望顺序（自动发现是扫描式，不是名单式）。
PROBE_NAMES = ["body_rules.js", "brand_new_probe.js", "rss_retention.js"]
PROBE_ROOT = "PROBE_ROOT"


def _js_block(text, start):
    """text[start] 处那个 `function … { … }` 的整体（花括号配平）。

    harness 这段里没有"字符串里带花括号"的写法；真出现了就会配不平 ⇒ 下面的断言直接红，
    而不是静默切出一截（静默切短就是这类判据最常见的自欺）。
    """
    i = text.index("{", start)
    depth = 0
    j = i
    while j < len(text):
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                return text[start:j + 1]
        j += 1
    raise AssertionError("rewriteLibRequires 的花括号配不平 ⇒ harness 的形状变了")


def _harness_discovery_js(path):
    """从 harness 源文件里切出自动发现的**可执行片段**（两条正则声明 + 替换函数）。"""
    text = open(path, encoding="utf-8").read()
    decls = [ln for ln in text.split("\n")
             if ln.startswith("const LIB_REQUIRE_RE =")
             or ln.startswith("const LIB_REQUIRE_LEFTOVER =")]
    assert len(decls) == 2, "%s 里那两条正则声明的形状变了：%s" % (path, decls)
    a = text.find("function rewriteLibRequires(")
    assert a >= 0, "%s 里没有 function rewriteLibRequires ⇒ 自动发现的实现被删了" % path
    fn = _js_block(text, a)
    assert "LIB_REQUIRE_LEFTOVER.test(out)" in fn, (
        "%s 的 rewriteLibRequires 里没有了 leftover 硬抛 ⇒ 函数体被换过（F3 的缺陷形状）" % path)
    assert "return { src: out, names: names }" in fn, (
        "%s 的 rewriteLibRequires 返回值形状变了，本判据的探针无法对齐" % path)
    return "\n".join(decls) + "\n" + fn


def _run_discovery_js(js_fragment, tmp_path):
    """node 里真跑一次那份实现，返回 {ok, names, out} 或 {ok:false, err}。"""
    script = tmp_path / "harness_discovery_probe.js"
    script.write_text(
        "const path = require('path');\n"
        + js_fragment + "\n"
        "const SRC = %s;\n"
        "try {\n"
        "  const r = rewriteLibRequires(SRC, %s, {'rss_retention.js': 'OVERRIDE_RETENTION'});\n"
        "  process.stdout.write(JSON.stringify({ok: true, names: r.names, out: r.src}));\n"
        "} catch (e) {\n"
        "  process.stdout.write(JSON.stringify({ok: false, err: String(e && e.message)}));\n"
        "}\n"
        % (json.dumps(PROBE_SRC), json.dumps(PROBE_ROOT)),
        encoding="utf-8", newline="\n")
    r = subprocess.run(["node", str(script)], capture_output=True, encoding="utf-8",
                       errors="replace", timeout=120, cwd=str(tmp_path))
    assert r.returncode == 0, "node 跑 harness 切片崩了：%s" % (
        (r.stdout or "")[-300:] + (r.stderr or "")[-600:])
    return json.loads(r.stdout)


def _require_paths(text):
    """产物里每条 `require("…")` 的**参数值**（反解 JSON 转义 + 折平分隔符）。

    为什么比"参数值"而不是比整段文本：JS 的 `path.join` 给反斜杠、Python 镜像给正斜杠，
    而且同一段路径在"转义文本"与"解析后的串"两种形态下反斜杠个数还不一样 ——
    那种差异不是分叉，让它红就是狼来了；真正要钉的分叉是**改写到了哪些目标**。
    """
    out = []
    for m in re.finditer(r'''require\(\s*("(?:[^"\\]|\\.)*")\s*\)''', text):
        out.append(json.loads(m.group(1)).replace("\\", "/"))
    return out


def _skeleton(text):
    """把每条 require 的参数换成占位符，只留"源码骨架" ⇒ 与平台写法无关的形状比对。"""
    return re.sub(r'''require\(\s*"(?:[^"\\]|\\.)*"\s*\)''', "require(<T>)", text)


# ─────────────────────────── 判据 ───────────────────────────

def test_every_api_lib_require_has_a_home():
    """①：api/*.js 里每条 require('../lib/X.js') 要么 (a) 已登记的可选降级，要么 (b) 自动发现+文件存在。"""
    rows = []
    for name, text in api_sources():
        problems = audit(text, "api/%s" % name)
        assert not problems, "api/%s 的 lib 依赖没归宿：\n%s" % (name, "\n".join(problems))
        for r in lib_requires(text):
            rows.append((name, r["name"], r["line"], r["optional"], r["note"]))
    assert rows, "api/*.js 里一条 require('../lib/…') 都没扫到 ⇒ 本判据已经空转"
    # (a) 分支点名：可选降级只允许这两条，且必须与 OPTIONAL_OK 逐字对上
    optional = sorted({n for _f, n, _l, opt, _note in rows if opt})
    assert optional == sorted(OPTIONAL_OK), (
        "实际带出声降级的 lib 是 %s，判据白名单是 %s —— 两边必须一致：\n%s"
        % (optional, sorted(OPTIONAL_OK),
           "\n".join("%s: %s" % (k, v) for k, v in OPTIONAL_OK.items())))


def test_body_rules_is_a_hard_require_not_an_optional_degradation():
    """③：body_rules 必须是硬 require —— 不许出现 try { … body_rules … } catch 的形状。"""
    shape = re.compile(r"\btry\s*\{[^{}]*body_rules[^{}]*\}\s*catch", re.S)
    hits = []
    for name, text in api_sources():
        code = _mask(text, drop_strings=False)
        if shape.search(code):
            hits.append("api/%s" % name)
        for r in lib_requires(text):
            if r["name"] in HARD_REQUIRED and r["optional"]:
                hits.append("api/%s:%d（%s）" % (name, r["line"], r["note"]))
    assert not hits, ("正文规范层被包成了可选降级 %s：降级 = 正文不规范化就出厂，正是 Task 4 要消灭的"
                      "故障，不许用「改成可选」来绕过转译层：%s" % (hits, HARD_REQUIRED["body_rules.js"]))


def test_transpile_harnesses_use_autodiscovery_not_a_hardcoded_list(tmp_path):
    """②：两个 CJS 转译 harness 必须继续扫源码自动发现，不许退回逐条硬写 lib 名单。

    派工 F3：光验文本不够 —— 把函数体换成硬编码三 lib 名单，只要那行正则常量逐字留着，
    四条存在性断言全绿。所以这里在四条文本断言**之后**再执行一次真实现（不删任何一条）：
    切出 harness 里的 `rewriteLibRequires`，用同一份探针输入在 node 里跑，
    与 Python 镜像 `rewrite_lib_requires` 逐字段对账 ⇒ 名单一变，行为立刻对不上。
    """
    for rel in HARNESS_FILES:
        path = os.path.join(ROOT, rel)
        text = open(path, encoding="utf-8").read()
        assert HARNESS_RE_DECL in text, (
            "%s 里没有那条自动发现正则（逐字要求：%s）⇒ 有人又把它改回硬名单了" % (rel, HARNESS_RE_DECL))
        assert BANNED_HARDCODE_SHAPE not in text, (
            "%s 又出现了逐条硬写名单的老写法 %r —— 新增 lib 依赖必须零人工登记，"
            "否则下一个 body_rules 就会重演 6 条红" % (rel, BANNED_HARDCODE_SHAPE))
        assert "function rewriteLibRequires(" in text, "%s 的自动发现函数被删了" % rel
        assert HARNESS_OVERRIDE_KEY in text, (
            "%s 丢了 retention 的覆盖语义：换实现（STARHUB_RSS_LIB / LIB_OVERRIDE）的探针会失去着力点" % rel)

        # ── 行为半（F3 补的就是这一格）──
        if not _node():
            pytest.skip("本机没有 node（CI 的 ubuntu runner 一定有）⇒ 行为半跑不了")
        res = _run_discovery_js(_harness_discovery_js(path), tmp_path)
        assert res["ok"], "%s 的自动发现实现跑崩 ⇒ 它已经不是「扫源码」了：%s" % (rel, res.get("err"))
        assert res["names"] == PROBE_NAMES, (
            "%s 的自动发现只认得 %s，探针给的三条 require 是 %s ⇒ 名单式实现"
            % (rel, res["names"], PROBE_NAMES))
        py_out, py_names = rewrite_lib_requires(PROBE_SRC, PROBE_ROOT, PROBE_OVERRIDES)
        assert py_names == res["names"], (
            "%s 与 Python 镜像扫到的 lib 名单不一致（JS=%s / py=%s）⇒ 两侧口径分叉"
            % (rel, res["names"], py_names))
        assert _require_paths(res["out"]) == _require_paths(py_out), (
            "%s 的改写目标与 Python 镜像不同（JS=%s / py=%s）⇒ 转译层与判据不再是同一套规则"
            % (rel, _require_paths(res["out"]), _require_paths(py_out)))
        assert _skeleton(res["out"]) == _skeleton(py_out), (
            "%s 的产物骨架与 Python 镜像不同 ⇒ 除改写目标外还动了别的东西" % rel)
        assert '"OVERRIDE_RETENTION"' in res["out"], "%s 的覆盖语义在行为上丢了" % rel
        assert "brand_new_probe.js" in res["out"], (
            "%s 没把**任何名单里都没有**的新 lib 改写掉 ⇒ 退回逐条登记" % rel)
        assert not [p for p in _require_paths(res["out"]) if p.startswith("../")], (
            "%s 的产物里仍有相对 ../lib ⇒ 生成到 tests/ 深度就会 MODULE_NOT_FOUND" % rel)


def test_hardcoded_lib_list_turns_the_harness_red(tmp_path):
    """F3 的点名对账：把 harness 的替换函数换成"硬编码三 lib 名单" ⇒ 必须红。

    上一判据的四条文本断言对这种改动**全绿**（那行 `const LIB_REQUIRE_RE = /…/g;` 逐字留着），
    所以这里直接在 %TEMP% 的副本里做这次替换，再用同一个行为探针跑它：
    要么 leftover 硬抛（ok:false），要么新增 lib 没进 names —— 两个都算红。
    对照：现网两份 harness 跑同一探针都是绿的（上一条判据已经钉着）。
    """
    hardcoded = (
        "function rewriteLibRequires(source, root, overrides) {\n"
        "  const REGISTERED = ['rss_retention.js', 'body_rules.js', 'rss_cover.js'];\n"
        "  const names = [];\n"
        "  const out = source.replace(LIB_REQUIRE_RE, function (_all, _q, name) {\n"
        "    if (REGISTERED.indexOf(name) < 0) { return 'require(\"../lib/\" + name)'; }\n"
        "    names.push(name);\n"
        "    const target = Object.prototype.hasOwnProperty.call(overrides, name)\n"
        "      ? overrides[name] : path.join(root, 'lib', name);\n"
        "    return 'require(' + JSON.stringify(target) + ')';\n"
        "  });\n"
        "  if (LIB_REQUIRE_LEFTOVER.test(out)) {\n"
        "    throw new Error('还有相对 ../lib 依赖没被改写（硬编码名单漏了新增 lib）');\n"
        "  }\n"
        "  return { src: out, names: names };\n"
        "}")
    if not _node():
        pytest.skip("本机没有 node（CI 的 ubuntu runner 一定有）")
    for rel in HARNESS_FILES:
        text = open(os.path.join(ROOT, rel), encoding="utf-8").read()
        a = text.find("function rewriteLibRequires(")
        assert a >= 0, "%s 的函数没了，变异没法打" % rel
        orig = _js_block(text, a)
        variant = os.path.join(str(tmp_path), os.path.basename(rel) + ".hardcoded.py")
        with open(variant, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text[:a] + hardcoded + text[a + len(orig):])
        # 变异件仍要过"文本四条"（证明文本口径确实拦不住它），再验行为口径拦得住
        vtext = open(variant, encoding="utf-8").read()
        assert HARNESS_RE_DECL in vtext and "function rewriteLibRequires(" in vtext \
            and BANNED_HARDCODE_SHAPE not in vtext, (
            "变异件没通过那四条文本断言 ⇒ 这次自证证明不了什么，换个形状重打")
        res = _run_discovery_js(_harness_discovery_js(variant), tmp_path)
        assert (not res["ok"]) or PROBE_NAMES != res["names"], (
            "%s 换成硬编码三 lib 名单后行为探针居然是绿的 ⇒ 行为口径也空转了：%s"
            % (rel, res))
        if res["ok"]:
            assert "brand_new_probe.js" not in res["names"], res


def test_autodiscovery_rewrites_every_relative_lib_require():
    """转译层对真实 api/rss.js 的产物里不许剩任何相对 ../lib（这就是原缺陷的直接形状）。"""
    src = open(os.path.join(API_DIR, "rss.js"), encoding="utf-8").read()
    out, names = transpile_for_cjs(src, ROOT, {"rss_retention.js": "OVERRIDE_RETENTION"})
    assert re.search(r"""require\(\s*['"]OVERRIDE_RETENTION['"]\s*\)""", out), "覆盖语义丢了"
    assert "../lib/" not in out, "产物里仍有相对 ../lib ⇒ 生成到 tests/ 下就会 MODULE_NOT_FOUND"
    assert sorted(set(names)) == sorted(r["name"] for r in lib_requires(src)), (
        "自动发现扫到的 lib 名单与判据口径不一致：%s" % names)
    for name in set(names):
        assert os.path.isfile(os.path.join(LIB_DIR, name)), "lib/%s 不存在（没人登记）" % name


def _node():
    try:
        subprocess.run(["node", "--version"], capture_output=True, timeout=20)
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _node(), reason="本机没有 node（CI 上有）")
def test_transpiled_cjs_loads_from_the_tests_depth(tmp_path):
    """端到端：把转译产物放成 tests/rss_js/ 的同层结构，node require 必须成功（新增 lib 零登记）。"""
    src = open(os.path.join(API_DIR, "rss.js"), encoding="utf-8").read()
    out, names = transpile_for_cjs(src, ROOT, {})
    gen = tmp_path / "tests" / "rss_js" / "_drift_probe.cjs"
    gen.parent.mkdir(parents=True, exist_ok=True)
    gen.write_text(out, encoding="utf-8")
    drv = tmp_path / "load.cjs"
    drv.write_text("require(%s);process.stdout.write('LOADED');" % json.dumps(str(gen).replace("\\", "/")),
                   encoding="utf-8")
    r = subprocess.run(["node", str(drv)], capture_output=True, encoding="utf-8",
                       errors="replace", timeout=120, cwd=str(tmp_path))
    assert r.returncode == 0, "转译产物在 tests/ 深度装载失败（原缺陷形状）：%s" % (r.stderr or r.stdout)[-600:]
    assert "LOADED" in r.stdout
    assert "body_rules.js" in names, "自动发现没扫到 body_rules ⇒ 它又被写成逐条登记了：%s" % names
    assert "../lib/" not in out


# ─────────────────────────── 反例自证 / 变异：这条闸真的咬得住 ───────────────────────────

@pytest.mark.skipif(not _node(), reason="本机没有 node（CI 上有）")
def test_brand_new_lib_needs_zero_registration(tmp_path):
    """根因自证：给 api 副本新增 `require('../lib/brand_new.js')` 且**不做任何登记**，
    转译产物放成 tests/rss_js/ 同层也必须能装载 —— 老写法（逐条硬写名单）在这里必红，
    因为 `../lib/` 会被解析成 `tests/lib/`。这一条钉的就是"新增 lib 零人工登记"这个目标。
    """
    src = open(os.path.join(API_DIR, "rss.js"), encoding="utf-8").read()
    fake_root = tmp_path / "root"
    (fake_root / "lib").mkdir(parents=True)
    # lib/ 里先放齐工作树真实存在的那几个（rss.js 硬 require 的 body_rules 缺一个就装载失败，
    # 这正是 audit() 那条"目标文件必须存在"的判据所钉的形态），再**只**多一个全新的 brand_new.js。
    for fname in sorted(os.listdir(LIB_DIR)):
        if fname.endswith(".js"):
            shutil.copyfile(os.path.join(LIB_DIR, fname), str(fake_root / "lib" / fname))
    (fake_root / "lib" / "brand_new.js").write_text("module.exports = { marker: 1 };", encoding="utf-8")
    mutated = src + "\nconst BRAND_NEW = require('../lib/brand_new.js');\n"
    out, names = transpile_for_cjs(mutated, str(fake_root), {})
    assert "brand_new.js" in names, "自动发现没扫到新增 lib ⇒ 又退回逐条登记了：%s" % names
    assert "../lib/" not in out, "产物里仍有相对 ../lib"
    gen = fake_root / "tests" / "rss_js" / "_zero_reg.cjs"
    gen.parent.mkdir(parents=True, exist_ok=True)
    gen.write_text(out, encoding="utf-8")
    drv = tmp_path / "zero.cjs"
    drv.write_text("const M = require(%s);process.stdout.write('LOADED:' + typeof M + ':' "
                   "+ String(M && typeof M.BRAND_NEW));"
                   % json.dumps(str(gen).replace("\\", "/")), encoding="utf-8")
    r = subprocess.run(["node", str(drv)], capture_output=True, encoding="utf-8",
                       errors="replace", timeout=120, cwd=str(tmp_path))
    assert r.returncode == 0, "新增 lib 没登记就跑不起来（根因复发）：%s" % (r.stderr or r.stdout)[-600:]
    assert "LOADED:function" in r.stdout, r.stdout[-300:] + r.stderr[-300:]


def test_unregistered_new_lib_require_turns_the_gate_red():
    """反例自证：副本里加一句 require('../lib/_not_registered_probe.js') ⇒ 判据必须变红。

    在 %TEMP%（pytest 的 tmp_path）的副本上做，**绝不写工作树**。
    """
    src = open(os.path.join(API_DIR, "rss.js"), encoding="utf-8").read()
    assert audit(src, "api/rss.js") == [], "工作树本身就该红，反例没意义：%s" % audit(src, "api/rss.js")
    mutated = src + "\nconst PROBE = require('../lib/_not_registered_probe.js');\n"
    problems = audit(mutated, "api/rss.js（加了未登记探针的副本）")
    assert problems, "加了没人登记的 lib 依赖，判据却没咬住 ⇒ 这条闸是空转的"
    assert any("_not_registered_probe.js" in p and "没人登记" in p for p in problems), problems


def test_unregistered_probe_fails_end_to_end_not_silently(tmp_path):
    """反例自证（端到端）：探针被自动发现改成绝对路径后仍必须失败，且失败原因指向 lib 缺失。"""
    if not _node():
        pytest.skip("本机没有 node（CI 上有）")
    src = open(os.path.join(API_DIR, "rss.js"), encoding="utf-8").read()
    mutated = src + "\nconst PROBE = require('../lib/_not_registered_probe.js');\n"
    out, names = transpile_for_cjs(mutated, ROOT, {})
    assert "_not_registered_probe.js" in names, "自动发现没覆盖探针 ⇒ 又回到 tests/lib 的错误解析"
    assert "../lib/" not in out, "产物里仍有相对路径"
    gen = tmp_path / "tests" / "rss_js" / "_drift_probe_mutated.cjs"
    gen.parent.mkdir(parents=True, exist_ok=True)
    gen.write_text(out, encoding="utf-8")
    drv = tmp_path / "load2.cjs"
    drv.write_text("try{require(%s);process.stdout.write('LOADED');}catch(e){"
                   "process.stdout.write('FAILED:'+e.code+':'+e.message);}"
                   % json.dumps(str(gen).replace("\\", "/")), encoding="utf-8")
    r = subprocess.run(["node", str(drv)], capture_output=True, encoding="utf-8",
                       errors="replace", timeout=120, cwd=str(tmp_path))
    blob = r.stdout + r.stderr
    assert "LOADED" not in blob, "未登记的 lib 居然装载成功 ⇒ 探针其实存在，判据是假的"
    assert "MODULE_NOT_FOUND" in blob and "_not_registered_probe.js" in blob, (
        "失败原因不是「lib 没登记」而是别的（说明相对路径其实没被改写）：%s" % blob[-600:])


def test_optional_degradation_of_body_rules_turns_the_gate_red():
    """变异自证：把 body_rules 包成 try/catch 可选降级 ⇒ 判据必须变红（不许用降级绕过转译层）。"""
    src = open(os.path.join(API_DIR, "rss.js"), encoding="utf-8").read()
    hard = "const BODY = require('../lib/body_rules.js');"
    assert hard in src, "api/rss.js 里 body_rules 的硬 require 写法变了 ⇒ 本变异用例已失效"
    mutated = src.replace(hard, "let BODY = null;\ntry {\n  BODY = require('../lib/body_rules.js');\n"
                               "} catch (e) {\n  console.error('[rss] 正文规范层不可用:', e && e.message);\n}")
    problems = audit(mutated, "api/rss.js（body_rules 被包成可选的副本）")
    assert problems, "把正文规范层包成可选降级，判据没咬住 ⇒ 这条闸空转"
    assert any("body_rules" in p and "可选降级" in p for p in problems), problems
    # 且工作树那份必须是硬 require（同一口径的反向确认）
    assert audit(src, "api/rss.js") == []


def test_silent_optional_degradation_is_not_a_degradation():
    """出声是降级的准入条件：catch 里不 console 的"静默放过"不能算 (a) 分支。"""
    src = open(os.path.join(API_DIR, "rss.js"), encoding="utf-8").read()
    mutated = src.replace("  RETENTION = require('../lib/rss_retention.js');\n} catch (e) {\n"
                          "  console.error('[rss] 留存闸门模块加载失败:', e && e.message);\n"
                          "  console.log('::warning title=RSS 运行时留存闸门不可用::' + (e && e.message));\n",
                          "  RETENTION = require('../lib/rss_retention.js');\n} catch (e) {\n  // 静默放过\n")
    assert mutated != src, "api/rss.js 的 retention 降级写法变了 ⇒ 本判据的口径要跟着改"
    reqs = [r for r in lib_requires(mutated) if r["name"] == "rss_retention.js"]
    assert reqs and not reqs[0]["optional"], "静默 catch 被当成了可选降级 ⇒ (a) 分支准入形同虚设"
    assert "不出声" in reqs[0]["note"], reqs[0]["note"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q", "-s"]))
