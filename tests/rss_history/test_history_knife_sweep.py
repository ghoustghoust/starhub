# tests/rss_history/test_history_knife_sweep.py
# -*- coding: utf-8 -*-
"""R44：历史缓存出口也必须过这三把刀（HN 模板 / 粘连裸链 / 站内导航脱链）。

为什么要单独钉一条：这三把刀原本只接在**解析入口**与**快照 `s` 出口**。72h 窗口里没被
重新抓取的旧条目不会再过解析入口，于是历史里存的形状仍是入库那一刻的；快照出口虽然每次
出厂都过一遍，但 `api/rss.js` 与 chunk 通道直接读历史里的 `summary` / `full_content`
—— 出厂形状与历史存的形状分叉，就是"修了但没修完"。

三条口径写在判据里，不是写在注释里：
  1. **幂等**：第二遍改动数必须为 0。历史清扫每一场构建都会重复执行，不收敛等于每场都
     重写同一批条目（也是"每遍追加一个分隔符"那类实现唯一能被抓住的方式）。
  2. **顺序**：先 HN 刀、再粘连刀；正文侧先规范化、再脱链。与构建期三处出口同序。
  3. **判据不许读本地已存在的产物文件**：样本全部在本文件里现造。
     （本仓已用三种写法坐实"读现成产物再断言命中数>0"是变异盲区 —— 产物是旧版构建写的，
     刀被摘掉它照样绿。）
"""
import datetime
import io
import json
import os
import re
import sys
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
# 必须用**本目录**的 _loader（带 sys.modules 崩溃缓存），与 test_body_html_exits.py 同一条理由：
# build_rss_aggregator 顶层会重挂 sys.stdout，同进程多次 exec 是 exit 127 硬崩来源。
sys.path.insert(0, HERE)

from _loader import load_build  # noqa: E402

mod = load_build()

FULL = chr(0xFF1A)          # 全角冒号：一律写 chr()，不贴字形（与半角 ':' 看不出区别）

HN_EN = (u"Article URL: https://ex.test/a\n"
         u"Comments URL: https://news.ycombinator.com/item?id=1\n"
         u"Points: 254\n# Comments: 162")
HN_ZH = (u"文章网址" + FULL + u"https://ex.test/a\n"
         u"评论网址" + FULL + u"https://news.ycombinator.com/item?id=2\n"
         u"积分" + FULL + u"7\n# 评论数" + FULL + u"9")
GLUED = u"https://www.nodeseek.com/post-957723-1看了大佬的IX文章必须有行动力"
# 两把刀互相"让位"的形状：粘连的裸链接把模板行挡住 ⇒ 第一轮 HN 刀只数到 1 行（顶不住
# "至少两行"的门槛），剥掉裸链接之后 `积分：3` 才第一次长成模板行。
# ⇒ 清扫必须滚到不动点，各过一遍等于没扫干净（第二遍还会改 → 幂等判据当场红）。
UNMASKED = u"https://a.test/积分" + FULL + u"3\n# 评论数" + FULL + u"4\n正文一句"
LINK = u"https://ex.test/a"


def _one(**kw):
    """现造一条历史命中 + 清扫，返回 (改动条数, 那条命中)。"""
    hit = {"link": LINK, "summary": u"", "summary_zh": u"", "full_content": u""}
    hit.update(kw)
    hist = {"k": hit}
    return mod._renormalize_history_fulltext(hist), hist["k"]


def _flat(name):
    """函数源码压成一行：本文件是 CRLF 的 build_rss_aggregator.py，跨行断言必须先归一化。"""
    import inspect
    return inspect.getsource(getattr(mod, name)).replace("\r\n", "\n").replace("\n", "")


# ── 摘要侧：两把刀真的开火 ─────────────────────────────────────

def test_history_sweep_rewrites_hn_template_english():
    n, hit = _one(summary=HN_EN)
    assert n == 1, "英文模板行没被清扫（改动条数 %d）" % n
    assert "Article URL" not in hit["summary"] and "Comments URL" not in hit["summary"]
    assert "news.ycombinator.com" not in hit["summary"], "裸链接没剥：%r" % hit["summary"]
    assert "254" in hit["summary"] and "162" in hit["summary"]
    assert "Hacker News" in hit["summary"], hit["summary"]


def test_history_sweep_rewrites_hn_template_chinese():
    """中文变体（文章网址/评论网址/积分/评论数 + 全角冒号）同判 —— 判形状不判语言。"""
    n, hit = _one(summary=HN_ZH)
    assert n == 1, "中文模板行没被清扫"
    assert u"文章网址" not in hit["summary"] and u"评论网址" not in hit["summary"]
    assert u"7" in hit["summary"] and u"9" in hit["summary"]
    assert "Hacker News" in hit["summary"], hit["summary"]


def test_history_sweep_strips_glued_url_from_stored_summary():
    n, hit = _one(summary=GLUED, link=u"https://www.nodeseek.com/post-957723-1")
    assert n == 1, "粘连裸链接没被清扫"
    assert "nodeseek.com" not in hit["summary"], hit["summary"]
    assert hit["summary"].startswith(u"看了大佬"), "正文被吃掉了一部分：%r" % hit["summary"]


def test_history_sweep_cleans_summary_zh_too():
    """`summary_zh` 必须一起扫：快照 `s` 取的是 `summary_zh or summary`，
    只扫 `summary` 的话，翻译过的 HN 条目出厂仍是四行模板（半个断链）。"""
    n, hit = _one(summary=HN_EN, summary_zh=HN_ZH)
    assert n == 1
    assert u"文章网址" not in hit["summary_zh"], hit["summary_zh"]
    assert "Article URL" not in hit["summary"], hit["summary"]


def test_history_sweep_leaves_a_separated_url_alone():
    """反例：空格分隔的"链接 + 正文"不许剥（剥多了就是把摘要改坏）。"""
    clean = u"https://ex.test/a 看了大佬的文章"
    n, hit = _one(summary=clean)
    assert n == 0, "不该动的形状被改了：%r" % hit["summary"]
    assert hit["summary"] == clean


def test_history_sweep_leaves_prose_mentioning_points_alone():
    """门槛（≥2 行且含 Points/Comments URL）不许在历史侧被绕过。"""
    prose = u"评分说明\nPoints: 254"
    n, hit = _one(summary=prose)
    assert n == 0, "普通正文被当成模板吃掉了：%r" % hit["summary"]
    assert hit["summary"] == prose


# ── 幂等与顺序（派工点名的两条硬口径）─────────────────────────

def test_history_sweep_is_idempotent_second_pass_changes_zero():
    """第二遍改动数必须为 0（变异 ⑥ 的红点：把清扫改成每遍追加一个分隔符）。"""
    hist = {
        "k1": {"link": LINK, "summary": HN_EN},
        "k2": {"link": LINK, "summary": HN_ZH, "summary_zh": HN_ZH},
        "k3": {"link": LINK, "summary": GLUED},
        "k4": {"link": LINK, "summary": UNMASKED},
        "k5": {"link": u"https://blog.example/post/1",
               "full_content": u'<p>我们用 <a href="/tag/kubernetes">Kubernetes</a> 部署</p>'},
    }
    first = mod._renormalize_history_fulltext(hist)
    assert first == 5, "第一遍应改 5 条，实际 %d（有刀没开火）" % first
    snapshot = {k: dict(v) for k, v in hist.items()}
    assert mod._renormalize_history_fulltext(hist) == 0, "第二遍又改了 = 清扫不收敛"
    assert hist == snapshot, "第二遍改了值却没计数（分账与实况不符）"


def test_sweep_reaches_a_fixed_point_when_knives_unmask_each_other():
    """两把刀互相让位的那条形状：**一遍清扫就要扫干净**，不许留到下一场。

    实现若写成"HN 刀、粘连刀各过一遍"就在此处红（第一遍剥完裸链接才露出模板行，那两行
    仍未被压）；滚到不动点的实现到这里已经收敛。
    """
    hist = {"k": {"link": LINK, "summary": UNMASKED}}
    assert mod._renormalize_history_fulltext(hist) == 1
    got = hist["k"]["summary"]
    assert mod._clean_history_summary(got, LINK) == got, "一遍没扫到不动点：%r" % got
    assert u"积分" not in got and u"评论数" not in got, "模板行还在：%r" % got
    assert got.startswith(u"正文一句"), "正文被吃掉：%r" % got


def test_knife_sweep_runs_hn_before_glued():
    """顺序是契约：粘连刀包在 HN 刀**外面**（HN 先跑），与构建期三处出口同序。"""
    src = _flat("_clean_history_summary")
    assert re.search(r"_strip_glued_url\(\s*_rewrite_hn_summary\(", src) or \
        re.search(r"_hn\s*=\s*_rewrite_hn_summary\(.*?_strip_glued_url\(\s*_hn", src), (
            "_clean_history_summary 不是「先 HN 后粘连」的形状")
    assert not re.search(r"_rewrite_hn_summary\(\s*_strip_glued_url\(", src), "顺序倒置了"


def test_clean_history_summary_is_idempotent_on_its_own():
    for text in (HN_EN, HN_ZH, GLUED, UNMASKED, u"", u"纯正文",
                 u"https://a.test/xhttps://b.test/y看了"):
        once = mod._clean_history_summary(text, LINK)
        assert mod._clean_history_summary(once, LINK) == once, "不收敛：%r → %r" % (text, once)


# ── 正文侧：第三把刀 ───────────────────────────────────────────

def test_history_body_sweep_delinks_nav_links_and_keeps_external():
    body = (u'<p>我们用 <a href="/tag/kubernetes">Kubernetes</a> 部署，'
            u'参考 <a href="https://openai.com/blog/x">原始公告</a></p>')
    n, hit = _one(full_content=body, link=u"https://blog.example/post/1")
    assert n == 1, "站内导航链接没被脱（第三把刀没接进历史清扫）"
    fc = hit["full_content"]
    assert u"Kubernetes" in fc and u"原始公告" in fc, "正文词被吃了：%r" % fc
    assert "/tag/kubernetes" not in fc, fc
    assert 'href="https://openai.com/blog/x"' in fc, "外部正文链接被脱了：%r" % fc


def test_history_body_sweep_absolutizes_before_delinking():
    """顺序实测：无斜杠的相对形状 `author/anna` 只有先被 `_normalize_body_html` 绝对化成
    `https://blog.example/author/anna`，脱链接才看得见"host 之后的第一个路径段"。
    倒过来（先脱再规范化）时它是"纯相对"，判定明确不动它 ⇒ 链接原地留下。
    与构建期 `_deep_clean_html` 跑在规范化之后同一口径。"""
    hist = {"k": {"link": u"https://blog.example/rss.xml",
                  "full_content": u'<p><a href="author/anna">anna</a></p>'}}
    assert mod._renormalize_history_fulltext(hist) == 1
    fc = hist["k"]["full_content"]
    assert "<a" not in fc, "先脱后规范化才会留下的形状：%r" % fc
    assert "author/anna" not in fc, "绝对化的 URL 泄漏进正文：%r" % fc
    assert u"anna" in fc, "链接文字被吃掉了：%r" % fc


def test_history_body_sweep_is_idempotent_too():
    hist = {"k": {"link": u"https://blog.example/post/1",
                  "full_content": u'<p><a href="/tag/x">X</a> 与 '
                                  u'<a href="https://ext.example/y">Y</a></p>'}}
    mod._renormalize_history_fulltext(hist)
    once = dict(hist["k"])
    assert mod._renormalize_history_fulltext(hist) == 0, "正文侧第二遍又改了"
    assert hist["k"]["full_content"] == once["full_content"]


# ── 计数口径与不越界 ───────────────────────────────────────────

def test_sweep_counts_entries_not_fields():
    """一条命中里 summary 与 summary_zh 都被改，也只算一条（返回值是"改动条数"）。"""
    n, hit = _one(summary=HN_EN, summary_zh=HN_EN)
    assert n == 1, n
    assert hit["summary"] != HN_EN and hit["summary_zh"] != HN_EN


def test_sweep_does_not_touch_fields_outside_the_contract():
    hit_in = {"link": LINK, "title": HN_EN, "title_zh": HN_ZH,
              "pub_date": u"2026-10-05T00:00:00", "first_seen": u"2026-10-04T00:00:00",
              "image": GLUED, "media_url": GLUED, "media_type": u"image",
              "source": u"S", "source_key": u"s_1", "cat": u"other", "color": u"#6366f1",
              "summary": HN_EN, "summary_zh": u"", "full_content": u""}
    hist = {"k": dict(hit_in)}
    assert mod._renormalize_history_fulltext(hist) == 1
    hit = hist["k"]
    for field in ("title", "title_zh", "link", "pub_date", "first_seen", "image", "media_url",
                  "media_type", "source", "source_key", "cat", "color"):
        assert hit[field] == hit_in[field], "%s 被越界改了：%r" % (field, hit[field])
    assert hit["summary"] != HN_EN
    assert hit["summary_zh"] == u"", "空值必须保持空，不许被写成别的形状"
    assert "full_content" in hit and hit["full_content"] == u""


def test_sweep_returns_zero_and_reports_zero_on_clean_history():
    """反空转：干净历史必须一条都不改，且分账同步归零（播的数不能是上一场剩的）。"""
    hist = {"k": {"link": LINK, "summary": u"正常的摘要", "summary_zh": u"正常的中文摘要",
                  "full_content": u"<p>正常正文</p>"}}
    assert mod._renormalize_history_fulltext(hist) == 0
    assert mod._LAST_HISTORY_SWEEP["entries"] == 0
    assert sum(v for k, v in mod._LAST_HISTORY_SWEEP.items() if k != "entries") == 0


def test_sweep_account_matches_the_work_it_did():
    """分账必须与真开火的刀次对上（那条 print 播的就是这几个数，错了就是假播报）。"""
    hist = {
        "k1": {"link": LINK, "summary": HN_EN},
        "k2": {"link": LINK, "summary_zh": HN_ZH},
        "k3": {"link": LINK, "summary": GLUED},
        "k4": {"link": LINK, "summary": UNMASKED},
        "k5": {"link": u"https://blog.example/p/1",
               "full_content": u'<p><a href="/donate/x">d</a></p>'},
    }
    n = mod._renormalize_history_fulltext(hist)
    acct = dict(mod._LAST_HISTORY_SWEEP)
    assert n == 5 and acct["entries"] == 5, (n, acct)
    # k4 的 HN 刀是在**第二轮**开的火：只数第一轮就会把分账播少一条（这条就是那个盲区）
    assert acct["summary_hn"] == 3, acct
    assert acct["summary_glued"] == 2, acct
    assert acct["body_delink"] == 1, acct
    assert acct["fulltext"] == 1, acct


# ── 接线（函数自己有测试 ≠ 构建时会调它）───────────────────────

def _build_source():
    p = os.environ.get("RSS_BUILD_SRC") or os.path.join(ROOT, "build_rss_aggregator.py")
    return io.open(p, encoding="utf-8", newline="").read().replace("\r\n", "\n")


def test_history_sweep_is_wired_into_the_build():
    """加强版（对抗审查 ② 点名要改的那条）。

    **原来那条为什么弱**：它只 grep `_hist_normed = _renormalize_history_fulltext(_rss_history)`
    这一行在不在、有没有被复制成两处。而那一行字面一模一样地待在原地、每场照跑，
    缺陷照样发生 —— 因为它改的对象（`_rss_history` 里的字典）与三个消费者读的对象
    （`_accumulate_history` :909 用 `entry = dict(item)` 造的出厂副本）**不是同一个**。
    grep 只看"调没调"，看不见"改的是哪一份"，所以它对本次这个缺陷是完全盲的
    （把 `_renormalize_history_fulltext` 的函数体整格摘空，它也照样绿了一半）。

    **现在这条凭什么强**：两半都在，谁都不许少。
      1. 行为半（新，另两条判据演）：整链跑完出厂的 `fc`/`s` 必须已过刀
         （::test_outgoing_copies_are_swept_not_just_the_history_dict），
         且历史字典那一份也同步清干净；出厂清扫不收敛则 ::test_outgoing_sweep_is_idempotent_second_pass_changes_zero 红。
      2. 接线半（覆盖面比原来大一倍）：主流程里历史侧与出厂侧**两行都要在且只在一处**，
         出厂侧必须排在 `_accumulate_history` **之后**（副本还没造出来就扫不到副本），
         两本分账都必须无条件播报（摘掉任一行、把出厂清扫挪到累加之前、少播一本，都红）。
    """
    src = _build_source()
    hist_call = "_hist_normed = _renormalize_history_fulltext(_rss_history)"
    out_call = "_out_normed = _renormalize_outgoing_fulltext(sources_with_items)"
    assert src.count(hist_call) == 1, "历史清扫没在主流程里调用（或被复制成两处）"
    assert src.count(out_call) == 1, "出厂副本清扫没在主流程里调用（或被复制成两处）"
    assert 0 <= src.find(hist_call) < src.find(out_call), (
        "出厂侧没紧跟在历史侧后面 —— 两把同批清扫分头落点，下次只会改到一个")
    acc = "sources_with_items, total_items = _accumulate_history(sources_with_items)"
    assert 0 <= src.find(acc) < src.find(out_call), (
        "出厂清扫排在 `_accumulate_history` 之前 ⇒ 出厂副本还没被造出来，这一格扫的是空气")
    seg = src[src.find(hist_call):]
    assert "_LAST_HISTORY_SWEEP[" in seg[:1500], (
        "清扫分账没被播报：某把刀被摘掉、命中数掉到 0 时构建日志看不出来")
    assert "_LAST_OUTGOING_SWEEP[" in seg[:1500], (
        "出厂那一本分账没被播报 —— 审查 ② 之所以静默，就是因为出厂侧没有这本账")


def test_sweep_knives_are_called_from_the_sweep_function():
    """三把刀必须真的在 `_renormalize_history_fulltext` 这一格里，而不是只写在别处。"""
    src = _flat("_renormalize_history_fulltext")
    for knife in ("_normalize_body_html(", "_delink_nav_links(", "_clean_history_summary("):
        assert knife in src, "历史清扫里没有调 %s —— 那把刀在历史出口上是断链" % knife
    clean = _flat("_clean_history_summary").replace(" ", "")
    assert "_strip_glued_url(_hn)" in clean or "_strip_glued_url(_rewrite_hn_summary(" in clean, (
        "_clean_history_summary 没按「HN → 粘连」的顺序调两把刀")


def test_sweep_max_rounds_is_a_guard_not_a_semantic_limit():
    """轮数是护栏：真数据实测一轮就收敛（第二遍 0 改动），所以上限必须 ≥2 且**有限**。"""
    assert 2 <= mod._SUMMARY_SWEEP_MAX_ROUNDS <= 8, mod._SUMMARY_SWEEP_MAX_ROUNDS


# ══════════════════════════════════════════════════════════════
# 审查 ②：清扫改的对象，与三个消费者读的对象，必须是同一份
# ══════════════════════════════════════════════════════════════
# `_accumulate_history` 在 :909 用 `entry = dict(item)` 给每条历史命中造**出厂副本**，
# 而主流程的清扫（:9478）在那之后只改 `_rss_history` 里的那份字典 ⇒
# 三个消费者（`_save_api_snapshot` / `build_html` / `write_data_chunks`）读到的仍是
# 入库那一刻的形状。实测证据（2026-10-05，同一份内存语料）：
#   历史 fc = '<p>正文</p>Python'                     ← 刀开火了
#   出厂 fc = '<p>正文</p><a href="/tag/python">…'    ← 副本没动
#   出厂 s  = 'https://a.test/积分：3\n# 评论数：4\n正文一句'  ← 快照出口单趟顶不住"让位"形状
#   entry is hist = False                             ← 两条链各自一份对象
# 判据口径（派工点名）：样本**全部在内存里现造**，跑整链（解析 → 累加 → 清扫 → 出厂快照），
# 断言出厂的 `fc`/`s` 已过刀；不读任何本地已存在的产物文件（那种判据本仓已用三写法坐实
# 是变异盲区）。快照写在 `tmp_path` 里、由本轮自己生成，读它读的是"这轮厂里出的货"。

STALE_LINK = u"https://blog.example/post/stale"
CTRL_LINK = u"https://other.example/post/plain"
FRESH_LINK = u"https://blog.example/post/fresh"
SRC_KEY = u"sweep_chain_1"

# 正文侧：站内导航锚（`_delink_nav_links` 要脱）+ 无斜杠相对 URL（`_normalize_body_html`
# 要先绝对化）—— 两把刀都在 full_content 上，出厂必须看不见
STALE_FC = (u'<p>\u6b63\u6587</p><a href="/tag/python">Python</a>'
            u' \u4e0e <a href="https://openai.com/blog/x">\u539f\u516c\u544a</a>')
# 摘要侧：粘连裸链把模板行挡住 ⇒ 快照出口那一趟（HN → 粘连）扫不干净，
# 只有清扫那把"滚到不动点"的刀能压平 —— 用这条形状才能让 `s` 成为**有判别力**的断言
STALE_SUMMARY = (u"https://a.test/\u79ef\u5206" + FULL + u"3\n# \u8bc4\u8bba\u6570" + FULL
                 + u"4\n\u6b63\u6587\u4e00\u53e5")
# 控制条：非 HN、非微信、正文里一个站内导航段都没有 ⇒ 整链走完必须逐字节不动
CTRL_SUMMARY = u"\u666e\u901a\u6458\u8981\uff0c\u4e0d\u662f\u6a21\u677f\u884c"
CTRL_FC = u'<p>\u666e\u901a\u6b63\u6587</p><a href="https://ext.example/y">\u5916\u94fe</a>'


def _iso(hours_ago):
    return (mod._now_bj().replace(tzinfo=None)
            - datetime.timedelta(hours=hours_ago)).isoformat()


def _hist_hit(link, summary, fc):
    """历史里一条**没被重抓**的旧条目（入库那一刻的形状）。"""
    return {"link": link, "source": u"\u6e05\u626b\u94fe\u5224\u636e", "source_key": SRC_KEY,
            "cat": u"ai", "color": u"#fff", "title": u"title " + link, "title_zh": u"",
            "summary": summary, "summary_zh": u"", "full_content": fc,
            "image": u"", "media_url": u"", "media_type": u"",
            "pub_date": _iso(2.0), "first_seen": _iso(2.0)}


def _parsed_fresh_item():
    """走真解析入口 `_parse_rss_item` 的一条当次抓取条目（微信式转义正文 + 懒加载图）。"""
    raw = ('<item xmlns:content="http://purl.org/rss/1.0/modules/content/">'
           '<title>fresh</title><link>' + FRESH_LINK + '</link>'
           '<description>\u6b63\u5e38\u6458\u8981</description>'
           '<content:encoded>&lt;p&gt;\u6b63\u6587 &lt;img data-src="/lazy.png"&gt;&lt;/p&gt;'
           '</content:encoded>'
           '<pubDate>' + _iso(0.5) + '</pubDate></item>')
    items = []
    mod._parse_rss_item(ET.fromstring(raw), u"\u6e05\u626b\u94fe\u5224\u636e", SRC_KEY, u"ai", items)
    assert len(items) == 1, "\u89e3\u6790\u5165\u53e3\u6ca1\u4ea7\u51fa\u6761\u76ee\uff0c\u5224\u636e\u5728\u7a7a\u8dd1"
    # 真实出厂形状：翻译阶段已把 pub_date 落成 ISO 串（datetime 进不了 json）
    items[0]["pub_date"] = _iso(0.5)
    return items[0]


def _run_factory_chain(monkeypatch, tmp_path):
    """解析 → 累加 → 清扫 → 出厂快照，返回 (出厂条目 dict, 历史命中 dict, 快照卡片 dict)。"""
    monkeypatch.chdir(tmp_path)          # 快照写 tmp，绝不在工作树留产物
    monkeypatch.setattr(mod, "_rss_history", {})
    mod._rss_history[STALE_LINK] = _hist_hit(STALE_LINK, STALE_SUMMARY, STALE_FC)
    mod._rss_history[CTRL_LINK] = _hist_hit(CTRL_LINK, CTRL_SUMMARY, CTRL_FC)
    fresh = _parsed_fresh_item()
    out, _total = mod._accumulate_history(
        [{"key": SRC_KEY, "name": u"\u6e05\u626b\u94fe\u5224\u636e", "cat": u"ai",
          "color": u"#fff", "url": u"https://blog.example/rss", "tier": 1,
          "items": [fresh]}])
    # 主流程那一步清扫：与 build_rss_aggregator.py 里相邻的两行逐字同序（接线由
    # test_history_sweep_is_wired_into_the_build 钉，行为由本函数下面这几条钉）
    mod._renormalize_history_fulltext(mod._rss_history)
    mod._renormalize_outgoing_fulltext(out)
    by_link = {}
    for src in out:
        for it in src.get("items", []):
            by_link[it["link"]] = it
    mod._save_api_snapshot(out)
    snap = json.loads((tmp_path / "rss_api_snapshot.json").read_text(encoding="utf-8"))
    cards = {}
    for s in snap["sources"]:
        for c in s["items"]:
            cards[c["u"]] = c
    return by_link, cards


def test_outgoing_copies_are_swept_not_just_the_history_dict(monkeypatch, tmp_path):
    """整链判据：出厂的 `fc`/`s` 必须已过刀，且历史字典同步清干净（两个对象都得对）。"""
    by_link, cards = _run_factory_chain(monkeypatch, tmp_path)
    entry = by_link[STALE_LINK]
    hist = mod._rss_history[STALE_LINK]
    # 前提：这里比的确实是**两个对象**，否则"改了历史就等于改了出厂"是假象
    assert entry is not hist, (
        "\u51fa\u5382\u526f\u672c\u4e0e\u5386\u53f2\u547d\u4e2d\u662f\u540c\u4e00\u4e2a\u5bf9\u8c61\uff0c\u5224\u636e\u6293\u4e0d\u5230\u5f62\u72b6")

    card = cards[STALE_LINK]
    assert "/tag/python" not in card["fc"], (
        "\u51fa\u5382\u526f\u672c\u7684 fc \u4ecd\u662f\u5165\u5e93\u90a3\u4e00\u523b\u7684\u5f62\u72b6\uff08\u5bfc\u822a\u94fe\u6ca1\u8131\uff09\uff1a%r"
        % card["fc"])
    assert u"Python" in card["fc"], "\u8131\u94fe\u628a\u94fe\u63a5\u6587\u5b57\u4e5f\u5403\u4e86\uff1a%r" % card["fc"]
    assert "https://openai.com/blog/x" in card["fc"], u"\u5916\u90e8\u6b63\u6587\u94fe\u63a5\u88ab\u8131\u4e86"
    assert "/blog.example/author" not in card["fc"]
    assert u"\u79ef\u5206" not in card["s"] and "Article URL" not in card["s"], (
        "\u51fa\u5382\u7684 s \u4ecd\u662f\u6a21\u677f\u884c\uff08\u526f\u672c\u6ca1\u8fc7\u5200\uff09\uff1a%r" % card["s"])
    assert "a.test" not in card["s"], "\u7c98\u8fde\u88f8\u94fe\u8fd8\u5728\uff1a%r" % card["s"]
    assert card["s"].startswith(u"\u6b63\u6587\u4e00\u53e5"), "\u6b63\u6587\u88ab\u5403\u6389\uff1a%r" % card["s"]
    # 历史侧（存盘那一份）同批清干净 —— 否则每一场都要重扫一遍同样的形状
    assert "/tag/python" not in hist["full_content"], hist["full_content"]
    assert u"\u79ef\u5206" not in hist["summary"]


def test_outgoing_sweep_leaves_non_hn_non_wechat_entries_untouched(monkeypatch, tmp_path):
    """零改动红线（派工点名不许削弱）：非 HN、非微信、无站内导航段的一条都不许动。"""
    by_link, cards = _run_factory_chain(monkeypatch, tmp_path)
    ctrl = by_link[CTRL_LINK]
    assert ctrl["full_content"] == CTRL_FC, "\u63a7\u5236\u6761\u6b63\u6587\u88ab\u6539\u5199\uff1a%r" % ctrl["full_content"]
    assert ctrl["summary"] == CTRL_SUMMARY, "\u63a7\u5236\u6761\u6458\u8981\u88ab\u6539\u5199\uff1a%r" % ctrl["summary"]
    assert cards[CTRL_LINK]["fc"] == CTRL_FC and cards[CTRL_LINK]["s"] == CTRL_SUMMARY
    # 出厂副本与控制条的历史侧同样逐字节不动
    assert mod._rss_history[CTRL_LINK]["full_content"] == CTRL_FC
    assert mod._rss_history[CTRL_LINK]["summary"] == CTRL_SUMMARY


def test_outgoing_sweep_is_idempotent_second_pass_changes_zero(monkeypatch, tmp_path):
    """幂等（派工点名不许削弱）：整链走完再过一遍出厂清扫，改动数必须为 0。"""
    by_link, _cards = _run_factory_chain(monkeypatch, tmp_path)
    out = [{"key": SRC_KEY, "name": u"\u6e05\u626b\u94fe\u5224\u636e", "cat": u"ai",
            "color": u"#fff", "tier": 1, "items": list(by_link.values())}]
    assert mod._renormalize_outgoing_fulltext(out) == 0, (
        "\u51fa\u5382\u6e05\u626b\u4e0d\u6536\u655b\uff1a\u6bcf\u4e00\u573a\u6784\u5efa\u90fd\u4f1a\u91cd\u5199\u540c\u4e00\u6279\u6761\u76ee")
    assert mod._renormalize_history_fulltext(mod._rss_history) == 0, (
        "\u5386\u53f2\u4fa7\u7b2c\u4e8c\u904d\u53c8\u6539\u4e86")
    assert sum(v for k, v in mod._LAST_OUTGOING_SWEEP.items() if k != "entries") == 0, (
        "\u51fa\u5382\u5206\u8d26\u4e0e\u5b9e\u51b5\u4e0d\u7b26\uff1a%r" % dict(mod._LAST_OUTGOING_SWEEP))

