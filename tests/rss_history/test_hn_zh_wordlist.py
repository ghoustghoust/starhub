# tests/rss_history/test_hn_zh_wordlist.py
# -*- coding: utf-8 -*-
"""R45：HN 模板摘要的**中文词表缺口**（"HN 阅读器里全是链接"没修干净的那一半）。

Task 8 把四行 hnrss 模板压成一行，但每个槽位的词表只收了**一个**中文词
（`_HN_COMMENTS` 只认 `评论数`）。运行时翻译层对同一槽位会给出一族同义词，
实测最大缺口是 `# 评论: 0` —— 半角冒号、词是"评论"而不是"评论数"。
构建期出口与实时 `api/rss.js` 出口同吃这个缺口，所以症状是"修了但用户没觉得修完"。

本文件的词表不是想象出来的，是把 `rss_history.json`（18,037 条，只读、不发请求）的
`summary`/`summary_zh` **逐行按行首形态聚合**数出来的。实测读数（2026-10-05 同一份语料）：

    改前残留模板行 1,069（summary 14 / summary_zh 1,055）
      形态 Top：`# 评论: N`×551、`评论链接：URL`×59、`# 评论数：N`×54、`得分：N`×41、
                `# 评论：N`×37、`评论区链接：URL`×29、`评论URL：URL`×23、`评论数：N`×22 …
      源分布：  hn_newest_56×588、hn_ai_7×342、hn_ask_57×74、hn_show_58×50、hn_llm_8×13
    改后残留模板行    21（summary 14 / summary_zh  7）—— **词表缺口残留 0**
      源分布：  hn_ask_57×15、hn_newest_56×3、hn_show_58×3
      残余 21 行**全部**是"整个字段只有 1 行模板行"的被 `_truncate` 截断块
      （见 test_truncated_single_line_block_is_left_alone）：门槛"至少两行"本就该放它过去。
    第二遍改动 0（幂等）；非 HN 条目被重写 0 条（无误伤面）。

    两个分法交叉（实测，别混着说）：改前 1,069 行里"词根本不在旧表"955 行、
    "词在旧表里但被同伴拖住门槛"114 行；改后剩下的 21 行**全部**同时落在
    "词在旧表"与"整字段只有 1 行模板"这两格 —— 即词表缺口与它连带拖住的门槛都已清完。

三条口径写进判据、不写进注释：
  1. **真值判据与实现解耦**：下面 `TRUTH` 是手写的形态清单，不从 `mod._HN_*` 推导 ——
     否则从词表里删一个词，判据会跟着一起变小，永远绿（派工点名的变异 ①）。
  2. **命中门槛不许为了多剥而放宽**：`test_truncated_single_line_block_is_left_alone`
     钉的就是"单行不动手"（派工点名的变异 ③ 靠 `test_zh_prose_*` 两条一起咬）。
  3. **判据不读本地已存在的产物文件**：样本全部在本文件里现造；真语料只作为
     只读复核，缺文件时那两条 skip（本仓已用三写法坐实"读现成产物再断言命中"是变异盲区）。
"""
import io
import json
import os
import re
import sys
import collections

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)
from _loader import load_build  # noqa: E402

mod = load_build()

HISTORY = os.path.join(ROOT, "rss_history.json")
FULL = chr(0xFF1A)             # 全角冒号：一律写 chr()，不贴字形（与半角 ':' 看不出区别）
FW_2 = chr(0xFF12)             # 全角２：ASCII [0-9] 口径的钉子

# 门槛锚点：用**已被旧词表覆盖**的英文形状提供"≥2 行 + 含 Points/Comments URL"，
# 于是"这一行有没有被剥掉"只取决于被测的那个词，不取决于门槛。
ANCHOR_CMT = u"Comments URL: https://news.ycombinator.com/item?id=9"
ANCHOR_PTS = u"Points: 7"


# ---------------------------------------------------------------- 真值判据（手写，不从实现推导）
# 每一项都是"行首词 + 冒号两可 + 值段"的整行形状，词来自第 1 步的形态清点。
TRUTH = [re.compile(p, re.I) for p in (
    u"^(?:article url|文章网址|文章链接|文章URL|文章地址)\\s*[:\\uFF1A]\\s*\\S+$",
    u"^(?:comments url|评论网址|评论链接|评论区链接|评论区网址|评论URL|评论地址|评论页面"
    u"|讨论区链接|讨论链接)\\s*[:\\uFF1A]\\s*\\S+$",
    u"^(?:points|积分|得分|评分|点赞数|热度|热度值|分数|点数|分值|关注度|当前得分)"
    u"\\s*[:\\uFF1A]\\s*[0-9]+$",
    u"^#?\\s*(?:comments|评论数|评论数量|评论)\\s*[:\\uFF1A]\\s*[0-9]+$",
)]


def _is_tpl(line):
    t = line.strip()
    return bool(t) and any(p.match(t) for p in TRUTH)


def residual(text):
    """重写结果里仍然存着的模板行（口径：**真值判据**说的算，不是实现说了算）。"""
    return [ln.strip() for ln in (text or u"").replace("\r\n", "\n").split("\n") if _is_tpl(ln)]


# ---------------------------------------------------------------- 实测形态清单
# (说明, 那一行的原文, 实测行数)。计数来自本文件开头那次聚合，写在这里是为了
# "缺了哪个词能直接对上号"；第 4 条就是派工点名的那条半角冒号。
REAL_LINES = [
    # ── Comments 计数槽：旧表只认 `评论数`，实测这个槽位有 4 个中文词、两种冒号 ──
    (u"# 评论 半角冒号（最大缺口，派工点名）", u"# 评论: 0", 551),
    (u"# 评论 全角冒号", u"# 评论" + FULL + u"0", 37),
    (u"#评论 井号后无空格", u"#评论" + FULL + u" 0", 12),
    (u"# 评论数 全角冒号", u"# 评论数" + FULL + u"2", 78),
    (u"评论数 无井号", u"评论数" + FULL + u"0", 28),
    (u"评论数量", u"评论数量" + FULL + u"0", 1),
    (u"# Comments 英文原形", u"# Comments: 0", 1141),
    # ── Points 槽 ──
    (u"积分 全角", u"积分" + FULL + u"588", 592),
    (u"积分 半角", u"积分: 4", 4),
    (u"得分", u"得分" + FULL + u"43", 43),
    (u"评分", u"评分" + FULL + u"18", 18),
    (u"点赞数", u"点赞数" + FULL + u"10", 10),
    (u"热度", u"热度" + FULL + u"12", 12),
    (u"热度值", u"热度值" + FULL + u"10", 10),
    (u"分数", u"分数" + FULL + u"10", 10),
    (u"点数", u"点数" + FULL + u"9", 9),
    (u"分值", u"分值" + FULL + u"1", 1),
    (u"关注度", u"关注度" + FULL + u"1", 1),
    (u"当前得分", u"当前得分" + FULL + u"1", 1),
    (u"Points 英文原形", u"Points: 1153", 1153),
    # ── Article URL 槽 ──
    (u"文章网址", u"文章网址" + FULL + u"https://ex.test/a", 534),
    (u"文章链接", u"文章链接" + FULL + u"https://ex.test/b", 84),
    (u"文章URL", u"文章URL" + FULL + u"https://ex.test/c", 25),
    (u"文章地址", u"文章地址" + FULL + u"https://ex.test/d", 1),
    (u"Article URL 英文原形", u"Article URL: https://ex.test/e", 1053),
    # ── Comments URL 槽 ──
    (u"评论网址", u"评论网址" + FULL + u"https://news.ycombinator.com/item?id=1", 588),
    (u"评论链接", u"评论链接" + FULL + u"https://news.ycombinator.com/item?id=2", 60),
    (u"评论区链接", u"评论区链接" + FULL + u"https://news.ycombinator.com/item?id=3", 29),
    (u"评论区网址", u"评论区网址" + FULL + u"https://news.ycombinator.com/item?id=4", 2),
    (u"评论URL", u"评论URL" + FULL + u"https://news.ycombinator.com/item?id=5", 26),
    (u"评论地址", u"评论地址" + FULL + u"https://news.ycombinator.com/item?id=6", 2),
    (u"评论页面", u"评论页面" + FULL + u"https://news.ycombinator.com/item?id=7", 1),
    (u"讨论区链接", u"讨论区链接" + FULL + u"https://news.ycombinator.com/item?id=8", 5),
    (u"讨论链接", u"讨论链接" + FULL + u"https://news.ycombinator.com/item?id=9", 4),
    (u"Comments URL 英文原形", ANCHOR_CMT, 1166),
]
IDS = [x[0] for x in REAL_LINES]


def _block(line):
    """把被测那一行配上门槛锚点，构造一个**必定满足门槛**的块。"""
    parts = [line]
    if line != ANCHOR_CMT:
        parts.append(ANCHOR_CMT)
    if line != ANCHOR_PTS:
        parts.append(ANCHOR_PTS)
    return u"\n".join(parts)


# ---------------------------------------------------------------- 1. 真形态回归判据
@pytest.mark.parametrize("line", [x[1] for x in REAL_LINES], ids=IDS)
def test_every_counted_real_form_is_stripped(line):
    """第 1 步枚举出的**每一种实际形态**都不许残留在重写结果里。

    这条是变异 ① 的靶：把新收的词从 Python 词表里删掉，对应那一条立刻变红 ——
    红的是"这个词的形状又漏回阅读器"，而不是某个计数。
    """
    got = mod._rewrite_hn_summary(_block(line), u"https://ex.test/a")
    assert line not in got, "模板行没被剥掉：%r -> %r" % (line, got)
    assert residual(got) == [], "重写结果里仍残留模板行：%r" % (residual(got),)


def test_halfwidth_colon_comment_form_is_stripped():
    """派工点名的原形状 `# 评论: 0`（半角冒号 + 词是"评论"不是"评论数"）。

    单独留一条命名清楚的：这条红了就是"用户又在阅读器里看到链接行"的直接信号，
    不需要从参数化 id 里回头找。
    """
    s = (u"文章网址" + FULL + u"https://ex.test/a\n"
         u"评论网址" + FULL + u"https://news.ycombinator.com/item?id=1\n"
         u"积分" + FULL + u"2\n# 评论: 0")
    got = mod._rewrite_hn_summary(s, u"https://ex.test/a")
    assert residual(got) == [], "真实历史里那条 summary_zh 形状仍有残留：%r" % (got,)
    assert got == u"2 分 · 0 评论 · Hacker News", got


def test_zh_fullwidth_and_hashless_variants_all_fire():
    """同族词的三种标点变体（半角/全角/井号后无空格）必须一起认。"""
    for tail in (u"# 评论: 7", u"# 评论" + FULL + u"7", u"#评论" + FULL + u" 7"):
        s = (u"文章链接" + FULL + u"https://ex.test/a\n" + ANCHOR_CMT + u"\n"
             u"得分: 5\n" + tail)
        got = mod._rewrite_hn_summary(s, u"https://ex.test/a")
        assert residual(got) == [], "%r 没被剥掉：%r" % (tail, got)
        assert u"5 分" in got and u"7 评论" in got, "计数被吃掉了：%r" % (got,)


def test_rewrite_is_idempotent_on_every_counted_form():
    """幂等：历史清扫对同一字段每场构建都重跑，不收敛等于每场重写同一批条目。"""
    for label, line, _n in REAL_LINES:
        s = _block(line)
        once = mod._rewrite_hn_summary(s, u"https://ex.test/a")
        twice = mod._rewrite_hn_summary(once, u"https://ex.test/a")
        assert once == twice, "%s 不幂等：%r -> %r" % (label, once, twice)


def test_word_tables_have_no_dead_word():
    """词表里每个词都必须被某个实测形态真正 exercise 到（防"加了个永不命中的词"）。"""
    covered = set()
    for _l, line, _n in REAL_LINES:
        for w in (mod._HN_ART_WORDS + mod._HN_CMT_WORDS +
                  mod._HN_PTS_WORDS + mod._HN_CMTS_WORDS):
            if re.search(re.escape(w), line, re.I):
                covered.add(w)
    allw = set(mod._HN_ART_WORDS + mod._HN_CMT_WORDS + mod._HN_PTS_WORDS + mod._HN_CMTS_WORDS)
    dead = sorted(allw - covered)
    assert not dead, "词表里有从未被实测形态覆盖的死词（补词请同时补 REAL_LINES）：%r" % (dead,)


# ---------------------------------------------------------------- 2. 反例判据
def test_zh_prose_comment_sentence_is_not_eaten():
    """正文里合法写着"评论数：10 条，值得讨论"这类句子时**不许被吃掉正文**。

    两个方向都要顶住：
      ① 整块只有这一句 + 一行元数据 ⇒ 门槛（≥2 行）就该放行，整串原样返回。
         把门槛降成"命中一行即可"（变异 ③）时，`积分：8` 会被当成模板吃掉 → 本条红。
      ② 这句夹在**真的**模板块里 ⇒ 块该重写，但这句一个字不许少。
    """
    prose = u"评论数" + FULL + u"10 条，值得讨论"
    # ① 门槛顶住：整块只有 1 行真模板行（那句正文不算），所以一个字都不许动。
    #    把门槛降成"命中一行即可"（变异 ③）时，`积分：8` 会被当成模板吃掉 → 本条红。
    s1 = u"本期讨论很热闹。\n" + prose + u"\n积分" + FULL + u"8"
    assert residual(s1) == [u"积分" + FULL + u"8"], \
        "真值判据前提失效：正文句 %r 被算成了模板行" % (prose,)
    assert mod._rewrite_hn_summary(s1, u"https://ex.test/a") == s1, \
        "门槛被放宽：单行元数据就把整段正文当模板重写了"
    # ② 块照旧重写，但正文行原样保留
    s2 = (u"文章网址" + FULL + u"https://ex.test/a\n评论网址" + FULL +
          u"https://news.ycombinator.com/item?id=1\n积分" + FULL + u"3\n# 评论: 0\n" +
          prose + u"\n这段是正文，得分" + FULL + u"9.5 分也不该动。")
    got = mod._rewrite_hn_summary(s2, u"https://ex.test/a")
    for keep in (prose, u"这段是正文，得分" + FULL + u"9.5 分也不该动。"):
        assert keep in got, "正文行被吃掉：%r -> %r" % (keep, got)
    assert residual(got) == [], got


def test_bare_word_forms_are_not_matched_alone():
    """"评论"进了词表，但不能因此把只有词、没有"冒号+数字"的正文行吃掉。"""
    for s in (u"评论", u"评论参与即可：", u"本文来自微信公众号： IPP评论 ，作者：郭海",
              u"当前状态：文章热度值：1；评论数：0", u"得分", u"积分说明"):
        assert not _is_tpl(s), "%r 是正文形态，不该被真值判据算成模板行" % s
        assert mod._rewrite_hn_summary(s, u"https://ex.test/a") == s, s


def test_fullwidth_digits_are_not_template_numbers():
    """ASCII `[0-9]` 口径（不是 `\\d`）：全角数字两侧同判"不是模板行"。"""
    fw_line = u"# 评论: " + FW_2
    s = (u"文章网址" + FULL + u"https://ex.test/a\n评论网址" + FULL +
         u"https://ex.test/b\n积分" + FULL + FW_2 + u"5\n" + fw_line)
    assert _is_tpl(u"# 评论: 0"), "真值判据自己先失效了"
    assert not _is_tpl(fw_line), "真值判据把全角数字当成 ASCII 数字了"
    got = mod._rewrite_hn_summary(s, u"https://ex.test/a")
    assert fw_line in got, "全角数字那行被吃掉了，口径是 ASCII [0-9]：%r" % got


def test_truncated_single_line_block_is_left_alone():
    """命中门槛不放宽：整字段只剩**一行**模板行（被 `_truncate` 截断那类）⇒ 原样返回。

    改后残余的 21 行**全是**这一类（实测：每个残留字段的模板行数都等于 1）。
    把它们也剥掉只需要把门槛降成"命中一行"，而那正是上一条反例判据顶不住的形状 ——
    残余数写在这里，就是不许有人用"降到 0"的名义去动门槛。
    """
    for s in (u"这是一段 Ask HN 正文。\n" + ANCHOR_CMT,
              u"正文两句。\n评论网址" + FULL + u"https://news.ycombinator.com/item?id=1"):
        assert len(residual(s)) == 1, "样本前提失效（必须正好 1 行模板行）：%r" % s
        assert mod._rewrite_hn_summary(s, u"https://ex.test/a") == s, \
            "单行模板就被重写了，门槛被降：%r" % s


# ---------------------------------------------------------------- 3. 历史出口判据
def _hist_entry(summary=u"", summary_zh=u"", link=u"https://ex.test/a"):
    return {"link": link, "summary": summary, "summary_zh": summary_zh, "full_content": u"",
            "source_key": u"hn_newest_56"}


def test_history_exit_clears_the_counted_zh_shapes():
    """历史出口判据（**内存现造**，不读任何本地产物文件）：

    用真实历史里那批 `summary_zh` 形状现造一整套条目过 `_renormalize_history_fulltext`，
    断言重写后模板行残留 = 0、且第二遍改动 = 0。
    """
    hist = {}
    for i, (_label, line, _n) in enumerate(REAL_LINES):
        hist[u"k%d" % i] = _hist_entry(summary_zh=_block(line))
    # 真实历史里那条最典型的整块形状（半角冒号 + 评论）单列一条
    hist[u"real_shape"] = _hist_entry(summary_zh=(
        u"文章网址" + FULL + u"https://ex.test/a\n评论网址" + FULL +
        u"https://news.ycombinator.com/item?id=1\n积分" + FULL + u"2\n# 评论: 0"))
    changed = mod._renormalize_history_fulltext(hist)
    left = []
    for k, h in hist.items():
        for f in ("summary", "summary_zh"):
            for r in residual(h.get(f) or u""):
                left.append((k, f, r))
    assert changed >= 1, "历史出口一条都没改，说明刀没接上：%r" % (changed,)
    assert left == [], "历史出口仍有 %d 条模板行残留：%r" % (len(left), left[:6])
    # 幂等：第二遍改动必须为 0
    assert mod._renormalize_history_fulltext(hist) == 0, "第二遍仍在改动 ⇒ 清扫不收敛"


def test_real_history_zh_residual_is_zero_except_gate_kept_lines():
    """真语料复核（只读 `rss_history.json`，不发请求）：词表缺口残留必须为 0。

    口径分两层，别混着报：
      * **词表缺口**残留 = 0（改前：纯缺词 955 行 + 缺词连带拖住门槛的 114 行 = 1,069 行）；
      * 总残留 = 21，且这 21 行必须**全部**是"整个字段只有 1 行模板行"的门槛保留项。
    第二层比第一层更容易被"顺手放宽门槛"糊过去，所以这里把"每个字段只有 1 行"写死。
    """
    if not os.path.exists(HISTORY):
        pytest.skip("%s 是 gitignore 的本地语料，缺省时本条不跑（现造那两条已经覆盖形状）"
                    % os.path.basename(HISTORY))
    with io.open(HISTORY, encoding="utf-8") as fh:
        hist = json.load(fh)
    assert len(hist) >= 15000, "语料小到这不像那份历史：%d" % len(hist)
    gap = []
    gate_kept = []
    bysrc = collections.Counter()
    for k, v in hist.items():
        if not isinstance(v, dict):
            continue
        for f in ("summary", "summary_zh"):
            s = v.get(f) or u""
            if not s:
                continue
            out = mod._rewrite_hn_summary(s, v.get("link") or u"")
            post = residual(out)
            if not post:
                continue
            sk = v.get("source_key") or u"?"
            bysrc[sk] += len(post)
            pre_n = len(residual(s))
            (gate_kept if pre_n == 1 else gap).append((sk, f, post[0]))
    assert gap == [], "词表缺口仍在漏（%d 行）：%r" % (len(gap), gap[:6])
    assert len(gate_kept) <= 40, \
        "门槛保留项涨到 %d 行，超出实测 21 行：多半是新形态又没进词表：%r" % (
            len(gate_kept), gate_kept[:6])
    print(u"[R45 真语料] 词表缺口残留=0；门槛保留=%d 行；源分布=%s"
          % (len(gate_kept), dict(bysrc)))


def test_hn_word_list_has_no_non_hn_surface():
    """每个收进来的词，在真语料的**非 HN 条目**上命中数必须是 0。

    这是收词的第二条依据，也是"要不要把 `得分`/`热度`/`评论` 这种通用词收进来"的
    唯一诚实答案 —— 想当然地加词与打死不加词都是猜，这里用 18,037 条实测。
    缺语料时退化成一份手写 witness 面（不退化成"跳过即通过"）。
    """
    hn_key = re.compile(r"^(?:hn_|hackernews)")
    pats = [(w, re.compile(u"^(?:%s)\\s*[:\\uFF1A]\\s*\\S+$" % re.escape(w), re.I))
            for w in mod._HN_ART_WORDS + mod._HN_CMT_WORDS]
    pats += [(w, re.compile(u"^(?:%s)\\s*[:\\uFF1A]\\s*[0-9]+$" % re.escape(w), re.I))
             for w in mod._HN_PTS_WORDS]
    pats += [(w, re.compile(u"^#?\\s*(?:%s)\\s*[:\\uFF1A]\\s*[0-9]+$" % re.escape(w), re.I))
             for w in mod._HN_CMTS_WORDS]
    if os.path.exists(HISTORY):
        with io.open(HISTORY, encoding="utf-8") as fh:
            hist = json.load(fh)
        hits = collections.Counter()
        for v in hist.values():
            if not isinstance(v, dict) or hn_key.match(v.get("source_key") or u""):
                continue
            for f in ("summary", "summary_zh"):
                for ln in (v.get(f) or u"").replace("\r\n", "\n").split("\n"):
                    t = ln.strip()
                    if not t:
                        continue
                    for w, p in pats:
                        if p.match(t):
                            hits[w] += 1
        assert not hits, "这些词在非 HN 条目上有命中面，收它就是在吃正文：%r" % dict(hits)
        print(u"[R45 误伤面] 非 HN 命中=0（%d 条语料 / %d 个词）" % (len(hist), len(pats)))
        return
    # 无语料时的手写反例面：每条都是"正文里合法长着这个词的句子"，一条都不许命中。
    prose = [u"得分：9.5 分，是本季度最高", u"评论数：10 条，值得讨论", u"热度：本季度上升 3 倍",
             u"文章链接如下所示。", u"积分说明见文末。", u"分数与分数之间相差无几。"]
    for s in prose:
        assert not _is_tpl(s), "真值判据先把正文句算成模板行：%r" % s
        assert mod._rewrite_hn_summary(s, u"https://ex.test/a") == s, s
    print(u"[R45 误伤面] 缺 %s，退化到 %d 条手写 witness（不是跳过即通过）"
          % (os.path.basename(HISTORY), len(prose)))