# -*- coding: utf-8 -*-
r"""闸：`_deep_clean_html` 里不许留"开不了火的刀"（R42 的收口判据）。

起因（2026-10-05 裁定）：Task 10 往 `_deep_clean_html` 里加的推荐块/作者块**容器规则**
（`<(\w+)[^>]*\b(?:class|id)\s*=\s*"[^"]*\b(?:…|related-posts|recommend|read-next|
article-tags|author-box|byline|toc-|table-of-contents|meta-info)…"`）在生产上是空转的：
唯一入口链是 `_deep_clean_html(_sanitize_html(_normalize_body_html(...)))`，
而 `_sanitize_html` 的属性白名单 `_KEPT_ATTRS` 不保留 `class`/`id` ⇒ 等这条规则拿到
`text` 时，`<div class="related-posts">` 早就是 `<div>`，它的命中数**恒为 0**，
与语料无关。留着的代价不是"没用"，而是**假绿**：下一个人读到词表就以为有防线。

所以这条闸把"这条刀真的开过火"变成可执行判据：

  对 `_deep_clean_html` 执行到的每一条 `re.sub` 规则，用**生产顺序**
  （`_normalize_body_html → _sanitize_html → _deep_clean_html`）跑仓库真语料
  （`rss_history.json` 里带 `full_content` 的条目，只读、不发任何网络请求）
  加一份 witness 语料，断言每条规则**至少真的改写过一条样本**；
  命中 0 的规则一律红，并打印是哪条。

为什么"命中 0"要算 corpus ∪ witness 而不是只算 corpus：
`rss_history.json` 的 `full_content` 是**出厂之后**的正文，有些刀在入库时已经把
自己要找的形状清掉了（压缩连续空行那条在这份语料上就是 0 命中 —— 它 0 命中恰恰
是它自己在解析入口开过火的证据）。这类刀**能**开火，只是语料里已看不到形状；
而容器 class 那一刀在任何输入上都开不了火，因为它要的属性被 sanitizer 吃定。
⇒ 判据同时要求：每条规则要么在真语料上开过火，要么在 witness 上开过火；
两者都开不了火的才是空转刀。

变异自证（`test_gate_flags_the_readded_inert_container_rule`）：把刚删掉的那条容器规则
塞回一份**临时副本**里，这条闸必须当场把它点出来 —— 证明它挡得住"再塞一把空转刀"。

行尾：本文件用 LF（本目录新判据的既有惯例）。
"""
import ast
import io
import json
import os
import re as REAL_RE
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HERE = os.path.dirname(os.path.abspath(__file__))
BUILD = os.environ.get("RSS_BUILD_SRC") or os.path.join(ROOT, "build_rss_aggregator.py")
HISTORY = os.path.join(ROOT, "rss_history.json")
sys.path.insert(0, HERE)
from _loader import load_build  # noqa: E402

mod = load_build()

# ---------------------------------------------------------------- 语料
# 真语料：仓库那份 rss_history.json（18037 条，其中带 full_content 的 5405 条）。
# 只读；文件是 gitignore 的本地产物，缺省时退化成"只跑 witness"，闸依然咬得住
# （空转刀在 witness 上也开不了火，见 §模块开头那条理由）。
_BASE_LINK = "https://blog.example/p/%d"

_HISTORY_CACHE = {}


def _real_corpus():
    """[(full_content, link)]，只取非空正文。缺文件返回 []（不静默当成"语料空所以全绿"）。"""
    if "corpus" in _HISTORY_CACHE:
        return _HISTORY_CACHE["corpus"]
    if not os.path.exists(HISTORY):
        _HISTORY_CACHE["corpus"] = []
        return []
    with io.open(HISTORY, encoding="utf-8") as fh:
        hist = json.load(fh)
    corpus = [(_h.get("full_content") or "", _h.get("link") or _BASE_LINK % i)
              for i, _h in enumerate(hist.values()) if (_h.get("full_content") or "")]
    _HISTORY_CACHE["corpus"] = corpus
    _HISTORY_CACHE["history"] = hist
    return corpus


# witness 语料：每条都写着"它准备让哪把刀开火"。它们都是**入库前**的形状，
# 走的是与生产完全一致的顺序（不是直接调 `_deep_clean_html`，那会绕过 sanitize 这一格、
# 让容器 class 那一刀假装自己开得了火）。
WITNESSES = [
    # 1.5a wechat2rss / link-proxy / 微信域跳转链接：整枚删
    ('<p>正文一</p><a href="https://mp.weixin.qq.com/s?__biz=abc">阅读原文</a>', _BASE_LINK % 1),
    ('<p>正文二</p><a href="https://wechat2rss.example.com/feed/x">link</a>', _BASE_LINK % 2),
    ('<p>正文三</p><a href="https://proxy.example/link-proxy/9">open</a>', _BASE_LINK % 3),
    # 1.5b "跳转微信"字样在**链接文字**上而 href 不在上面 ⇒ 只有这条能吃
    ('<p>正文四</p><a href="https://other.example/open">跳转微信打开</a>', _BASE_LINK % 4),
    # 2 逐块推广检测（同时让内层剥标签那条 `<[^>]+>` 开火：块里有 <b>）
    ('<div>长按 <b>二维码</b> 关注我们的公众号</div>', _BASE_LINK % 5),
    # 2.5 站内导航链接：去链接留文字（绝对化后仍要认，host 剥离那一刀也在这里开火）
    ('<p>我们用 <a href="/tag/kubernetes">Kubernetes</a> 部署</p>', _BASE_LINK % 6),
    ('<p>作者 <a href="https://blog.example/author/anna">anna</a> 说</p>', _BASE_LINK % 7),
    # 2.6 空锚点 / 站点根链接的壳
    ('<p>见 <a href="#">这里</a> 与 <a href="/">首页</a></p>', _BASE_LINK % 8),
    # 3 清洗后残留的空块元素
    ('<p>正文九</p><p></p><div><br/></div>', _BASE_LINK % 9),
    # 4 压缩连续空行（真语料上 0 命中：入库时已经被它自己清过一遍）
    ('<p>正文十</p>\n\n\n\n<p>正文十一</p>', _BASE_LINK % 10),
    # ── R42 那一刀的形状，留在语料里当**反例**：容器名写在 class 上。
    #    生产顺序下它到不了 `_deep_clean_html`（sanitize 吃属性），
    #    所以任何按 class/id 匹配的规则在这些样本上命中都是 0。
    ('<p>正文十二</p><div class="related-posts">推荐阅读：'
     '<a href="https://blog.example/x">别处</a></div>', _BASE_LINK % 12),
    ('<section class="author-box">署名 <span class="byline">anna</span></section>'
     '<p>正文十三</p>', _BASE_LINK % 13),
    ('<aside id="comments">读者评论如下</aside><p>正文十四</p>', _BASE_LINK % 14),
]

# 规则名的可读别名（只用于报错信息，不参与判定；按 pattern 子串匹配）。
_NAME_HINTS = [
    ("link-proxy", "1.5a 微信/link-proxy 跳转链接整枚删"),
    ("跳转微信", "1.5b “跳转微信”锚点整枚删"),
    ("(p|div)\\b[^>]*>[\\s\\S]*?", "2 逐块推广检测（含内层剥标签）"),
    ("(?P<href>", "2.5 站内导航链接去 href 留文字"),
    ('href="(?:#|/)"', "2.6 空锚点/站点根链接整枚删"),
    ("<br\\s*/?>", "3 残留空块元素"),
    ("(?:\\s*\\n){3,}", "4 压缩连续空行"),
    ("<[^>]+>", "2 的内层：剥掉块内联标签"),
    ("(?:[a-z][a-z0-9+.-]*:)?//", "2.5 的内层：剥掉 scheme+host"),
    ("(?:class|id)", "已删除的容器 class/id 规则（开不了火）"),
]


def _rule_name(pattern):
    for hint, name in _NAME_HINTS:
        if hint in pattern:
            return name
    return "未命名规则"


# 「按 class/id 找容器」这把刀的形状。F2 之后它只有**一份**口径：静态清单收两种写法
# （`re.sub(r"…", …)` 与编译型 `_X.sub(…)`），两条判据都读这同一个函数 ——
# 两处各写一份正则，就是下一次"一侧认得、另一侧看不见"的来源。
_CLASS_ID_SHAPE = REAL_RE.compile(r"\(\?:class\|id\)|\bclass\\s\*=|\bid\\s\*=")


def _class_id_offenders(patterns):
    return [p for p in patterns if _CLASS_ID_SHAPE.search(p)]


# ---------------------------------------------------------------- 探针
# 派工 F2：一把刀有两种写得出来的形状，闸**两种都要认**：
#   A. `re.sub(r"…", repl, text)` —— 首参是字符串常量；
#   B. 模块级编译型常量 `_X_RE = re.compile(...)` 之后 `_X_RE.sub(repl, text)`
#      （本文件已经用着 `_IMG_SRC_ATTR_RE` / `_DANGLING_TAG_RE` / `_HN_BARE_URL_LINE`
#       这一类编译型正则 ⇒ 下一把刀八成就长这样）。
# 旧口径两侧只认 A（动态要求 `isinstance(pattern, str)`、静态要求 `re` + 字符串首参），
# 于是 B 写法在**清单与开火计数上同时隐形**：审查实测注入一把"B 形状、按 class 匹配"的
# 容器刀，闸自己 4 passed、电池 249 passed、退出码 0 —— 完全没抓到。
# CPython 的 `Pattern` 方法不可覆盖（C 级只读），所以 B 的探针换的是**模块里绑定的名字**：
# `_deep_clean_html` 执行期间把 `m._X_RE` 换成记账代理，退出窗口立刻还原。
_COMPILED_TYPE = type(REAL_RE.compile(u""))       # 即 re.Pattern（不依赖版本别名）


class _Recorder(object):
    """记录"某条正则规则这次有没有真的改写文本"。键统一用 pattern 字符串，
    于是 A 与 B 两种写法落在同一张表上（B 的键来自被编译的那个字面量）。"""

    def __init__(self):
        self.active = False
        self.seen = []
        self.hits = {}

    def record(self, pattern, args, kw, call):
        out = call()
        if self.active:
            key = pattern if isinstance(pattern, str) else getattr(pattern, "pattern", None)
            if isinstance(key, str):
                if key not in self.hits:
                    self.hits[key] = 0
                    self.seen.append(key)
                before = args[1] if len(args) > 1 else kw.get("string")
                if isinstance(before, str) and out != before:
                    self.hits[key] += 1
        return out


class _ReShim(object):
    """假的模块级 `re`：一切照转，只在 `_deep_clean_html` 执行期间记录每条 re.sub 的
    pattern 与"这次有没有真的改写文本"。判据因此量的是**开火**，不是"正则跑过"。"""

    def __init__(self, rec):
        self._rec = rec

    def sub(self, pattern, *a, **kw):
        # 名字被换成代理之后，`re.sub(_X_RE, repl, text)` 这种"把编译常量喂给 re.sub"的
        # 写法收到的是 _PatternShim —— 真 `re.sub` 会 TypeError，所以先剥回真对象。
        if isinstance(pattern, _PatternShim):
            pattern = pattern._pat
        return self._rec.record(pattern, a, kw, lambda: REAL_RE.sub(pattern, *a, **kw))

    def __getattr__(self, name):
        return getattr(REAL_RE, name)


class _PatternShim(object):
    """编译型常量的替身：`.sub` 记账，其余（search/split/match/pattern/flags/…）原样转发。"""

    def __init__(self, pat, rec):
        self._pat = pat
        self._rec = rec

    def sub(self, *a, **kw):
        # `compiled.sub(repl, string)` 里没有 pattern 这一格 ⇒ 键取被编译的那个字面量
        return self._rec.record(self._pat, a, kw, lambda: self._pat.sub(*a, **kw))

    def __getattr__(self, name):
        return getattr(self._pat, name)


def _compiled_names(m):
    """模块里所有已编译正则常量（B 形状的候选）。"""
    return {n: v for n, v in list(vars(m).items()) if isinstance(v, _COMPILED_TYPE)}


def _restore(m, owner, swapped):
    if owner is None:
        try:
            del m.re
        except AttributeError:
            pass
    else:
        m.re = owner
    for name, original in swapped.items():
        setattr(m, name, original)


def _chain_pre(m, html, link):
    """生产顺序的前两格：normalize → sanitize（这两格的 re 调用**不算**本函数的刀）。"""
    return m._sanitize_html(m._normalize_body_html(html, link))


def _collect_rule_hits(m, corpus, witnesses):
    """两遍跑（真语料 / witness 各一遍），返回 [(pattern, 总命中, real, witness)]。

    顺序 = 首次执行顺序。"命中"= 这条规则把文本真的改写了，不是"跑过"。
    探针只覆盖 `_deep_clean_html` 的执行窗口：sanitize/normalize 里那些同名 pattern
    （`<[^>]+>`、`<script…>`）不是本函数的刀，混进来会把清单撑歪。
    A（`re.sub`）与 B（`_X_RE.sub`）两种写法都在这个窗口里记账。
    """
    owner = getattr(m, "re", None)

    def run(src):
        rec = _Recorder()
        swapped = {}
        for name, pat in _compiled_names(m).items():
            swapped[name] = pat
            setattr(m, name, _PatternShim(pat, rec))
        m.re = _ReShim(rec)
        try:
            for html, link in src:
                pre = _chain_pre(m, html, link)      # active=False：不算刀
                rec.active = True
                try:
                    m._deep_clean_html(pre)
                finally:
                    rec.active = False
        finally:
            _restore(m, owner, swapped)
        return rec

    a = run(corpus) if corpus else _Recorder()
    b = run(witnesses)
    order = list(a.seen) + [p for p in b.seen if p not in a.hits]
    return [(p, a.hits.get(p, 0) + b.hits.get(p, 0),
             a.hits.get(p, 0), b.hits.get(p, 0)) for p in order]


# `_deep_clean_html` 的刀源所在函数：R43 起规则 2.5 抽成了 `_delink_nav_links`
# （为了让运行时 JS 端口能逐条对账），静态清单要一起扫它。
# **新增被 `_deep_clean_html` 调用的持刀函数，就往这个元组里加一项**，
# 否则那条刀只剩动态发现（"写在永不执行的分支里"这种死刀就漏掉了）。
_KNIFE_FUNCTIONS = ("_deep_clean_html", "_delink_nav_links")


def _module_compiled_names(tree):
    """模块级 `X = re.compile(...)` 的名字集合（B 形状的登记表，AST 口径）。"""
    names = set()
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call) \
                and isinstance(node.value.func, ast.Attribute) \
                and node.value.func.attr == "compile" \
                and isinstance(node.value.func.value, ast.Name) \
                and node.value.func.value.id == "re":
            for tgt in node.targets:
                if isinstance(tgt, ast.Name):
                    names.add(tgt.id)
    return names


def _local_compiled_names(fn):
    """函数体内 `X = re.compile("字面量")` 的名字 → 那个字面量（B 形状的函数内变体）。"""
    out = {}
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call) \
                and isinstance(node.value.func, ast.Attribute) \
                and node.value.func.attr == "compile":
            lit = _pattern_literal(node.value)
            if lit is not None:
                for tgt in node.targets:
                    if isinstance(tgt, ast.Name):
                        out[tgt.id] = lit
    return out


def _pattern_literal(call):
    """从 `re.compile(...)` 的调用里取出 pattern 字面量（首参或 `pattern=` 关键字）。"""
    node = call.args[0] if call.args else next(
        (kw.value for kw in call.keywords if kw.arg == "pattern"), None)
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _pattern_of(m, name):
    """取模块级编译常量的 pattern 字面量（探针窗口内名字可能正被代理换掉 ⇒ 先剥回真对象）。"""
    v = getattr(m, name)
    return getattr(v, "_pat", v).pattern


def _static_rule_patterns(path=BUILD, m=None):
    """刀源函数源码里**写着**的每一条正则替换规则（AST 口径，两种写法都收）。

    动态发现看不到"写在永不执行的分支里"的刀，所以静态清单单独再要一次：
    静态出现过的 pattern 必须也出现在动态执行集合里。
    F2 之后这里不再有"看不见的方向"：认不出的 `.sub` 接收者**当场 assert 红**，
    而不是静默跳过 —— 静默跳过就是审查实测那 4 passed 的来源。
    """
    m = m if m is not None else mod
    with io.open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=path)
    module_compiled = _module_compiled_names(tree)
    out = []
    for want in _KNIFE_FUNCTIONS:
        fn = next((n for n in tree.body
                   if isinstance(n, ast.FunctionDef) and n.name == want), None)
        assert fn is not None, "源码里找不到 def %s(：函数被改名或删了" % want
        local_compiled = _local_compiled_names(fn)
        for node in ast.walk(fn):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "sub"):
                continue
            base = node.func.value
            if isinstance(base, ast.Name) and base.id == "re":
                first = node.args[0] if node.args else next(
                    (kw.value for kw in node.keywords if kw.arg == "pattern"), None)
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    out.append(first.value)                    # A：字符串常量
                    continue
                if isinstance(first, ast.Name):
                    assert first.id in module_compiled, (
                        "%s 里 re.sub(%s, …) 的首参既不是字面量也不是模块级编译常量 ⇒ "
                        "闸的口径认不出这把刀，需要显式扩口径，不许静默跳过" % (want, first.id))
                    out.append(getattr(m, first.id).pattern)    # B 的 re.sub(_X_RE, …) 变体
                    continue
                raise AssertionError(
                    "%s 里 re.sub 的首参形状不认识（%s）⇒ 这条刀本闸看不见，必须扩口径"
                    % (want, ast.dump(first) if first is not None else "缺首参"))
            assert isinstance(base, ast.Name), (
                "%s 里有 %s.sub(...) 的接收者不是简单名字（%s）⇒ 本闸认不出它，"
                "必须扩口径而不是放过（放过就是 F2 那把刀的来源）"
                % (want, ast.dump(base)[:40], want))
            if base.id in module_compiled:
                out.append(getattr(m, base.id).pattern)         # B：模块级编译常量
                continue
            if base.id in local_compiled:
                out.append(local_compiled[base.id])             # B：函数内编译常量（字面量）
                continue
            raise AssertionError(
                "%s 里 %s.sub(...) 的接收者既不是模块级也不是函数内的编译正则常量 ⇒ "
                "本闸认不出这把刀，必须扩口径" % (want, base.id))
    return out


# ---------------------------------------------------------------- 判据
def test_gate_has_a_real_corpus_at_all():
    """闸的语料必须是**仓库真语料**，不能悄悄退化成只有十几条手写样本。"""
    corpus = _real_corpus()
    if not os.path.exists(HISTORY):
        pytest.skip("本仓没有 %s（它是 gitignore 的本地产物），"
                    "此条只在有真语料的环境里量“真语料规模”；"
                    "空转刀那一条判据不依赖它" % os.path.basename(HISTORY))
    assert len(corpus) >= 1000, "真语料只有 %d 条，不足以说明“每条刀都开过火”" % len(corpus)


def test_every_deep_clean_rule_actually_fires():
    """主闸：`_deep_clean_html` 的每条 re.sub 规则都必须在生产顺序下真的改写过样本。

    命中 0 = 这把刀接不到出口 ⇒ 红，并打印是哪条。
    """
    corpus = _real_corpus()
    rows = _collect_rule_hits(mod, corpus, WITNESSES)
    assert rows, "跑完一条规则都没发现：探针或 `_deep_clean_html` 的形状变了"

    static = _static_rule_patterns()
    seen = {p for p, _h, _r, _w in rows}
    missing = [p for p in static if p not in seen]
    assert not missing, (
        "源码里写着、但**一次都没执行到**的规则：%s ⇒ 它连开火的机会都没有"
        % [( _rule_name(p), p[:60]) for p in missing])

    dead = [(p, hits, r, w) for p, hits, r, w in rows if hits == 0]
    assert not dead, (
        "这些规则在生产顺序（normalize→sanitize→deep_clean）下一条样本都没改写过 ⇒ "
        "它们是开不了火的空转刀，删掉它，别留下“看着有防线、实际为 0”的假绿：\n%s"
        % "\n".join("  [%s] real=0 witness=0 pattern=%r" % (_rule_name(p), p[:120])
                    for p, hits, r, w in dead))

    # 反空转：闸本身也得有牙 —— 语料必须至少喂进去 1000 条真样本（有真语料时），
    # 且每条规则至少有一处开火来源。
    if corpus:
        assert len(corpus) >= 1000
    for p, hits, r, w in rows:
        assert (r > 0 or w > 0), "命中数>0 但来源为 0，探针自相矛盾：%r" % p[:80]
    # 读数进报告（pytest -s 或 --capture=no 时可见；断言已经全部在上面）
    print("[deep_clean 刀的开火数] 真语料 %d 条 + witness %d 条，规则 %d 条"
          % (len(corpus), len(WITNESSES), len(rows)))
    for p, hits, r, w in rows:
        print("  real=%-6d witness=%-4d %s | %s" % (r, w, _rule_name(p), p[:78]))


def test_no_rule_keys_on_attributes_the_sanitizer_cannot_emit():
    """R42 的点名判据：链路上没有任何规则再按 `class`/`id` 找容器。

    `_KEPT_ATTRS` 只给 a 留 href、给 img 留 src/alt，class/id 到不了 `_deep_clean_html`；
    ⇒ 按它们匹配的规则命中恒为 0。这条与上一条独立：上条要样本，这条只看形状，
    所以哪怕语料换了一批也照样咬得住"再塞一把容器名刀"。
    """
    offenders = _class_id_offenders(_static_rule_patterns())
    assert not offenders, (
        "这些规则按 class/id 匹配，而 `_sanitize_html` 的白名单根本不保留 class/id "
        "⇒ 它们在生产上永远是 0 命中：%s" % [( _rule_name(p), p[:90]) for p in offenders])
    # 口径事实的另一半：sanitizer 确实不吃 class（这条若变了，上面那条才允许一起松绑）
    got = mod._sanitize_html('<div class="related-posts"><p>正文</p></div>')
    assert "class" not in got and "正文" in got, got


def test_gate_flags_the_readded_inert_container_rule(tmp_path):
    """变异自证：把已删的那条容器 class/id 规则塞回**一份临时副本**，闸必须点出它。

    这条判据不修改仓库文件：它在 tmp_path 里造一份 build_rss_aggregator.py 的副本、
    注入那把空转刀、用同一个探针跑同一批 witness，断言它 0 命中且被报成死刀。
    """
    with io.open(BUILD, encoding="utf-8") as fh:
        src = fh.read()
    anchor = "    # 1.5 移除 wechat2rss / link-proxy 跳转链接"
    assert anchor in src.replace("\r\n", "\n"), "注入锚点没了，副本变异没被装上去"
    inert = (
        "    text = re.sub(\n"
        "        r'<(\\w+)[^>]*\\b(?:class|id)\\s*=\\s*\"[^\"]*\\b"
        "(?:related-posts|recommend|read-next|article-tags|author-box|byline)[^\"]*\""
        "[^>]*>[\\s\\S]*?</\\1>',\n"
        "        '', text, flags=re.IGNORECASE)\n")
    variant = src.replace("\r\n", "\n").replace(
        anchor, inert + anchor, 1)
    assert inert in variant, "注入失败：副本里没找到那条刀"
    p = tmp_path / "bra_with_inert_rule.py"
    p.write_text(variant, encoding="utf-8", newline="\n")

    import importlib.util
    spec = importlib.util.spec_from_file_location("bra_inert_variant", str(p))
    vmod = importlib.util.module_from_spec(spec)
    vmod.__file__ = BUILD
    spec.loader.exec_module(vmod)

    rows = _collect_rule_hits(vmod, [], WITNESSES)
    dead = [(_rule_name(q), q) for q, h, _r, _w in rows if h == 0]
    assert dead, "把空转刀加回去，闸却没红：这条闸挡不住“再塞一把开不了火的刀”"
    assert any("(?:class|id)" in q for _n, q in dead), (
        "闸红在别的规则上（%s），没点到那条容器刀" % [n for n, _q in dead])
    # 对照：现网源码同一套 witness 跑出来一条死刀都没有
    base_rows = _collect_rule_hits(mod, [], WITNESSES)
    assert [q for q, h, _r, _w in base_rows if h == 0] == [], (
        "对照组就红了：witness 语料覆盖不住现有规则")


# ---------------------------------------------------------------- F2：编译型写法的变异自证
# 一把刀"写出来"有两种形状，旧闸只认 `re.sub(r"…", …)`，于是审查注入了另一种：
# 模块级编译常量 + `_X_RE.sub('', text)`。旧口径下它**两侧同时隐形** ——
# 动态侧：`.sub` 根本不经过模块级 `re`，`isinstance(pattern, str)` 也把它挡在门外；
# 静态侧：接收者是 `_X_RE` 而不是 `re`，`continue` 掉。实测闸自己 4 passed、
# 电池 249 passed、退出码 0。下面这条就是"那种形状再出现时必须红"的对账。
_INERT_TOKEN = "zz-inert-compiled-probe"      # 只出现在被注入的那把刀里，用来点名

# 被注入那把刀的 pattern **原文**（raw 字符串 ⇒ 写进副本的 `r'''…'''` 里逐字不动，
# 也不给本文件留 `\s` 这种无效转义 —— 那在 3.12 就是 SyntaxWarning）。
_INERT_PATTERN = (r'<(\w+)\b[^>]*\b(?:class|id)\s*=\s*"[^"]*\b(?:related-posts|advert|'
                  r'author-box|' + _INERT_TOKEN + r')[^"]*"[^>]*>[\s\S]*?</\1>')

_COMPILED_CONST_BLOCK = (
    u"_INERT_COMPILED_RE = re.compile(\n"
    u"    r'''" + _INERT_PATTERN + u"''',\n"
    u"    re.IGNORECASE)\n"
    u"\n"
    u"def _deep_clean_html(text):\n")

# 两把刀用同一个编译常量：B 形状本体 + `re.sub(编译常量, …)` 这一格（同样是旧口径的盲区）
_COMPILED_KNIFE_LINES = (
    u"    text = _INERT_COMPILED_RE.sub('', text)\n"
    u"    text = re.sub(_INERT_COMPILED_RE, '', text)\n")


def _compiled_form_source():
    """把审查那个注入形状装进一份**源码副本**（绝不写工作树）。"""
    with io.open(BUILD, encoding="utf-8") as fh:
        src = fh.read().replace("\r\n", "\n")
    fn_anchor = u"def _deep_clean_html(text):\n"
    line_anchor = u"    # 1.5 移除 wechat2rss / link-proxy 跳转链接"
    assert src.count(fn_anchor) == 1, "编译型注入的函数锚点变了（def _deep_clean_html）"
    assert src.count(line_anchor) == 1, "编译型注入的行锚点变了（# 1.5 移除 wechat2rss…）"
    src = src.replace(fn_anchor, _COMPILED_CONST_BLOCK, 1)
    src = src.replace(line_anchor, _COMPILED_KNIFE_LINES + line_anchor, 1)
    assert _INERT_TOKEN in src, "注入失败：副本里没有那把刀"
    return src


def _load_variant(path, tag):
    import importlib.util
    spec = importlib.util.spec_from_file_location(tag, str(path))
    vmod = importlib.util.module_from_spec(spec)
    vmod.__file__ = BUILD
    spec.loader.exec_module(vmod)
    return vmod


def test_gate_flags_the_compiled_form_inert_container_rule(tmp_path):
    """F2 的变异自证：同一把空转容器刀**写成编译型常量**，闸的两侧都必须点出来。

    三条账各自独立，缺任一侧就是审查实测那种"4 passed 的假绿"：
      · 动态发现 —— 编译型刀必须进规则表，且 0 命中 ⇒ 报成死刀；
      · 静态清单 —— 同一条刀在"写着"的清单里也要出现（否则写在永不执行的分支里
        那一半仍然看不见，`test_every_deep_clean_rule_actually_fires` 的 missing 检查落空）；
      · class/id 点名 —— 不依赖语料的形状判据也要认得它。
    对照组：现网源码同一套 witness 跑出来一条死刀都没有、清单里也没有这个 token。
    """
    p = tmp_path / "bra_with_compiled_inert_rule.py"
    p.write_text(_compiled_form_source(), encoding="utf-8", newline="\n")
    vmod = _load_variant(p, "bra_compiled_variant")

    # ── ① 动态侧 ──
    rows = _collect_rule_hits(vmod, [], WITNESSES)
    hits_by_pat = {q: h for q, h, _r, _w in rows}
    found = [q for q in hits_by_pat if _INERT_TOKEN in q]
    assert found, (
        "编译型常量写的刀（`_INERT_COMPILED_RE.sub('', text)` 与 `re.sub(_INERT_COMPILED_RE, …)`）"
        "在动态探针里一条都没被发现 ⇒ F2 那个盲区还在")
    dead = [q for q, h, _r, _w in rows if h == 0]
    assert any(_INERT_TOKEN in q for q in dead), (
        "编译型刀被发现了、却没报成死刀（命中数=%s）⇒ 空转刀闸挡不住 B 形状"
        % [(q[:40], hits_by_pat[q]) for q in found])

    # ── ② 静态侧：同一份副本的"写着"清单必须也认得它 ──
    static = _static_rule_patterns(str(p), vmod)
    assert any(_INERT_TOKEN in q for q in static), (
        "静态清单只认 re.sub+字符串常量 ⇒ 编译型刀不在清单里；"
        "它在 `test_every_deep_clean_rule_actually_fires` 的 missing 检查里是隐形的")

    # ── ③ 形状点名判据也要认得（与语料无关的那一半牙，与上一条判据同一份口径）──
    offenders = _class_id_offenders(static)
    assert any(_INERT_TOKEN in q for q in offenders), (
        "class/id 点名判据看不见编译型写法：%s" % [q[:60] for q in offenders])

    # ── 对照组：现网源码既没有这把刀，也没有任何死刀 ──
    base_rows = _collect_rule_hits(mod, [], WITNESSES)
    assert [q for q, h, _r, _w in base_rows if h == 0] == [], "对照组就红了：witness 覆盖不住现有规则"
    assert not any(_INERT_TOKEN in q for q in _static_rule_patterns()), "现网源码里怎么会有探针 token"


def test_static_gate_rejects_an_unrecognisable_knife_shape(tmp_path):
    """闸不许"认不出就静默跳过"：这正是 F2 假绿的根因形状，必须当场出声。

    注入一把接收者**不在登记表里**的 `.sub`（名字既不是模块级 `re.compile` 常量、
    也不是函数内的字面量编译常量 —— 例如正则在别处编译、从参数递进来）。
    旧口径对它的处理是 `continue`（看不见 ⇒ 也不说），于是下一把这种形状的刀照样活下来；
    现在必须抛错并点名那条名字。
    """
    with io.open(BUILD, encoding="utf-8") as fh:
        src = fh.read().replace("\r\n", "\n")
    line_anchor = u"    # 1.5 移除 wechat2rss / link-proxy 跳转链接"
    assert src.count(line_anchor) == 1
    weird = u"    text = _INHERITED_KNIFE.sub('', text)\n"
    p = tmp_path / "bra_with_unrecognised_knife.py"
    p.write_text(src.replace(line_anchor, weird + line_anchor, 1), encoding="utf-8", newline="\n")
    vmod = _load_variant(p, "bra_unrecognised_knife")
    with pytest.raises(AssertionError) as ei:
        _static_rule_patterns(str(p), vmod)
    assert "_INHERITED_KNIFE" in str(ei.value), (
        "闸红了，但红得没指向那条刀：%s" % str(ei.value)[:200])
