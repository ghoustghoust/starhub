# -*- coding: utf-8 -*-
"""构建期 `_translate_to_zh` 的"该不该发出去"判据（node 判据的 Python 版对应物）。

现场证据（2026-10-01 探针 `.deploy-tmp/_probe_buildtime_translate_skips.py`，urlopen 换成记录器）：
    中文夹符号     送出=是   ← `cn_chars > len(text)*0.3` 的分母是整串长度，
                              78 个 `+` 与 ANSI 残片把中文占比稀释到阈值以下
    HN 元数据样板  送出=是   ← Article URL / Points 这种投稿样板被当正文翻
    纯 URL        送出=是   ← 整串只有一个链接也发出去
    正常英文       送出=是   ✓
    正常中文       送出=否   ✓（这条今天就是对的，改动不许把它弄反）

与运行时 `_needsTranslation`（我今天刚修的）是同一个洞的两个副本：
运行时剥了 ANSI/URL/标点符号并按"拉丁词"计数，构建期一个字都没剥。
判据形状刻意做成**外部可观察**（有没有发出网络请求），不是读源码里的字符串。

另一条方向相反的护栏：含平假名/片假名的日文**必须继续翻** ——
`_translate_to_zh` 现在专门用 `has_kana` 把日文排除在"中文跳过"之外（防误跳），
把"中文占比"规则推广过头会把日文一起砍掉。
"""
import io
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "tests", "rss_composite"))
sys.path.insert(0, ROOT)

from _loader import load_build  # noqa: E402

ZH_SPAM = ("💻基本信息" + "+" * 78 + "[1m硬件质量体检报告：[36m45.192.*.*[0m"
           "[4mhttps://github.com/xykt/HardwareQuality[")
HN_STUB = ("Article URL: https://huggingface.co/datasets/sinabis-group/x\n"
           "Comments URL: https://news.ycombinator.com/item?id=49915070\n"
           "Points: 1\n# Comments: 0")
URL_ONLY = "https://github.com/xykt/HardwareQuality/blob/main/report.md"
EN_TITLE = "Researchers develop fluorescent probes to detect glucose in living animals"
ZH_TITLE = "OpenAI 发布新的披露框架，涵盖六个月观察案例"


@pytest.fixture()
def net():
    """把 urlopen 换成记录器，返回 (发送文本列表, 调用翻译的函数)。"""
    B = load_build()
    sent = []

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"choices":[{"message":{"content":"\xe3\x80\x90\xe8\xaf\x91\xe3\x80\x91"}}]}'

    def fake_urlopen(req, timeout=None):
        sent.append(getattr(req, "full_url", str(req)))
        return Resp(b"")

    old_cache, old_open = B._trans_cache, B.urllib.request.urlopen
    # 熔断/罚期状态必须自己清零：load_build() 复用同一个模块实例，
    # 前面 rss_history 的用例会把 _TRANS_BLOCK_UNTIL 打开 ⇒ 本探针一条都发不出去，
    # 判据就变成"依赖机器此刻恰好干净"（单独跑绿、全量跑红就是这么来的）。
    saved = {k: getattr(B, k, None) for k in
             ("_TRANS_BLOCK_UNTIL", "_TRANS_FAIL_STREAK", "_AGNES_BLOCK_UNTIL",
              "_AGNES_OFFENSES", "_AGNES_EMPTY_STREAK")}
    counters = dict(B._TRANS_STATS)          # 别 clear()：那是预置键的普通 dict，清空即 KeyError
    B._trans_cache = {}
    for k, v in saved.items():
        if v is not None:
            setattr(B, k, 0 if isinstance(v, (int, float)) else v)
    B.urllib.request.urlopen = fake_urlopen
    try:
        yield B, sent
    finally:
        B._trans_cache = old_cache
        B.urllib.request.urlopen = old_open
        for k, v in saved.items():
            if v is not None:
                setattr(B, k, v)
        for k in counters:
            B._TRANS_STATS[k] = counters[k]


def _sent(net, text):
    B, sent = net
    before = len(sent)
    B._translate_to_zh(text)
    return len(sent) > before


def test_chinese_with_symbol_runs_is_not_sent(net):
    """变异体一：中文里夹一串符号，不许再占用一次真实外呼。"""
    assert not _sent(net, ZH_SPAM), "中文夹符号的垃圾文本仍被送出（占比被符号稀释）"


def test_bare_url_is_not_sent(net):
    """整串只有一个链接：翻它没有任何产出。"""
    assert not _sent(net, URL_ONLY), "纯 URL 仍被送出翻译"


def test_metadata_stub_is_not_sent(net):
    """HN 投稿的元数据样板（全是 URL 与数字）不该当正文翻译。"""
    assert not _sent(net, HN_STUB), "HN 元数据样板仍被送出翻译"


def test_plain_english_still_sent(net):
    """反向护栏 1：把垃圾挡掉的同时不许把正常英文也挡了。"""
    assert _sent(net, EN_TITLE), "正常英文标题不再送翻译 —— 那是把功能改坏"


def test_japanese_still_sent(net):
    """反向护栏 2：含平假名/片假名的日文必须继续翻（现有代码专门用 has_kana 排除误跳）。

    两个样本缺一不可：第一个拉丁词很多，去掉 has_kana 早返回也照样会被翻（判据不 discriminating，
    变异 M4 因此存活过）；第二个是**汉字占多数、假名很少**的日文，只有它才能证明早返回在起作用。
    """
    JA_MIXED = "OpenAI が新しい disclosure framework を公開しました"
    JA_KANJI_HEAVY = "当社の新技術発表会のご案内です"      # 汉字 12 : 假名 4，无拉丁词
    assert _sent(net, JA_MIXED), "日文不再被翻译 —— 跳过口径越过了 has_kana 那条既有规则"
    assert _sent(net, JA_KANJI_HEAVY), (
        "汉字占多数的日文被当成中文跳过了：has_kana 那条早返回没起作用（它不是装饰）")


def test_korean_still_sent(net):
    """反向护栏 2b：谚文/西里尔这类"既不是中文也不是拉丁"的标题不许被顺手挡掉。

    这条是我自己写第一版时引进来的回归：门槛写成"必须有拉丁词"，韩文标题一个拉丁字母都没有
    ⇒ 被跳过；而旧口径照翻。真实首屏 360 条里看不出这个洞（没有谚文标题），所以必须靠判据兜住。
    """
    KO = "OpenAI 가 새로운 공개 프레임워크 를 발표 했습니다"        # 谚文正文，含一个拉丁词
    KO_PURE = "새로운 공개 프레임워크 를 발표 했습니다"            # 纯谚文，一个拉丁字母都没有
    assert _sent(net, KO_PURE), "纯谚文标题被跳过了（把'没有拉丁词'当成了'不用翻译'）"
    assert _sent(net, KO), "含谚文的标题被跳过了"


def test_cyrillic_still_sent(net):
    """反向护栏 2c：西里尔同理 —— 门槛必须是"有没有非中文的字母"，不是"有没有拉丁字母"。"""
    RU = "Исследователи представили новый метод обучения моделей"
    assert _sent(net, RU), "西里尔标题被跳过了"


def test_plain_chinese_still_skipped(net):
    """反向护栏 3：正常中文今天就是跳过的，改动不许把它变成发送。"""
    assert not _sent(net, ZH_TITLE), "正常中文标题开始被送出（跳过口径被改坏了）"


# ── 运行时 JS 与构建期 Python 的同规则判据 ─────────────────────────────────
# 为什么单独钉：docstring 一直声称两边"同规则"，但 2026-10-01 实测 14 条语料分叉 5 条
# （纯假名 / 假名夹汉字 / 纯谚文 / 西里尔 / 阿拉伯：PY=send，JS=skip）。
# 摘要是运行时专属（09-16 起移出构建期），所以这类分叉不是"少发一次请求"，
# 而是**日韩俄阿的摘要在浏览器里永不翻**。注释会过时，判据不会。
PARITY_CORPUS = [
    ("纯中文", "人工智能新规发布"),
    ("中英混(专名多)", "Google DeepMind 东京再招人：语音对话与多语言 LLM 方向"),
    ("英文", "OpenAI releases a new reasoning model"),
    ("纯假名", "当社の新技術発表会のご案内です"),
    ("假名夹汉字", "東京の新製品発表会について"),
    ("纯谚文", "새로운 공개 프레임워크가 오늘 출시되었습니다"),
    ("西里尔", "Новая модель машинного обучения"),
    ("阿拉伯", "إصدار جديد للذكاء الاصطناعي"),
    ("纯URL", "https://example.com/a/b?x=1"),
    ("ANSI垃圾", "发布 [1m[36m新版[0m"),
    ("HN样板", "Article URL: https://x.com\nComments URL: https://y.com\nPoints: 12\nSubmitted by bob"),
    ("空串", ""),
    ("纯数字标点", "2026-10-01 (12:00)"),
    ("日汉夹假名短", "新技術発表会"),
]
# 期望值两边都得满足：非拉丁文字必须送（旧 JS 就是把这类整类跳过的），垃圾必须跳。
PARITY_SEND = {"纯假名", "假名夹汉字", "纯谚文", "西里尔", "阿拉伯", "英文", "中英混(专名多)"}
PARITY_SKIP = {"纯中文", "纯URL", "ANSI垃圾", "HN样板", "空串", "纯数字标点", "日汉夹假名短"}


def _runtime_js_predicate():
    """从**生成的产物**里取 `_needsTranslation` 及其三个正则依赖。

    取产物而不是取 .py 源码：Python 三引号会吃掉 `\b`、`\r?\n` 这类转义，
    历史上正是"源码看着对、产物是坏的"（见 [[js-in-python-string-escapes]]）。
    """
    import json as _json  # 局部即可，别污染上面的导入面
    html = load_build().build_html([], "2026-10-01 12:20", 0, 0, analysis_data=None,
                                   diverse_window_minutes=120, diverse_enabled=True)

    def cut(a, b):
        i = html.find(a)
        assert i >= 0, "产物里找不到 %r（运行时判据被改名或删掉，同规则判据失去对象）" % a
        j = html.find(b, i)
        assert j > i, "%r 在产物里没有结束边界" % a
        return html[i:j + len(b)]

    return "\n".join([
        cut("var _CJK_R = ", ";"),
        cut("var _KANA_R = ", ";"),
        cut("var _LETTER_R = ", ";"),
        cut("var _META_STUB = ", ";"),
        cut("function _needsTranslation(", "\n  }"),
    ])


def test_runtime_and_buildtime_predicates_agree(tmp_path):
    """同规则必须是机器证明的：两边对同一批语料的 verdict 逐条相等。"""
    import json
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        pytest.skip("本机没有 node，运行时判据跑不了")
    B = load_build()
    texts = [t for _, t in PARITY_CORPUS]
    js = ("%s\nvar C=%s;\nconsole.log(JSON.stringify(C.map(_needsTranslation)));\n"
          % (_runtime_js_predicate(), json.dumps(texts, ensure_ascii=False)))
    p = tmp_path / "_parity_corpus.js"
    p.write_text(js, encoding="utf-8")
    r = subprocess.run([node, str(p)], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=60)
    assert r.returncode == 0, "运行时判据在 node 里跑崩（产物里的正则坏了）：\n%s" % (r.stderr or "")[-900:]
    got = json.loads(r.stdout.strip())
    assert len(got) == len(PARITY_CORPUS), "语料与返回条数不等，判据没逐条对齐"

    Bv = {label: bool(B._needs_translation(text)) for label, text in PARITY_CORPUS}
    Jv = {label: bool(v) for (label, _), v in zip(PARITY_CORPUS, got)}
    diverge = ["%s: PY=%s JS=%s" % (k, Bv[k], Jv[k]) for k in Bv if Bv[k] != Jv[k]]
    assert not diverge, (
        "构建期与运行时判据分叉（分叉的那类文本在某一条路径上永不翻译）：%s" % "; ".join(diverge))
    # 反向防空跑：语料不许塌成"两边都 skip"或"两边都 send"，那会让上面那条永远成立
    assert sum(Bv.values()) >= len(PARITY_SEND), (
        "语料里该送的那类没送够（%d 条），同规则判据可能已经空转" % sum(Bv.values()))
    wrong_send = [k for k in PARITY_SKIP if Bv[k] or Jv[k]]
    assert not wrong_send, "这些垃圾类文本被两边送进翻译了（跳过口径失守）：%s" % wrong_send
    wrong_skip = [k for k in PARITY_SEND if not Bv[k] or not Jv[k]]
    assert not wrong_skip, (
        "这些非拉丁/外语文本被跳过（正是旧 JS 用拉丁词当门槛的错法，摘要是运行时专属 ⇒ 永不翻）：%s"
        % wrong_skip)
