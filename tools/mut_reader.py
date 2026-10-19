# -*- coding: utf-8 -*-
"""阅读器正文/摘要可靠性判据的常驻变异电池（Task 12）。

为什么要有它：Task 1–11 与 R42–R47 的每条修复都做过"变异自证"，但那些脚本全在
`%TEMP%` 里，跑完就没了 —— 下一批改掉一行、判据静默空转，没有任何东西会拦。
本文件把那批一次性自证落成**常驻电池**：形状照抄 `tools/mut_attention.py`
（`COPIES` 列表、`MUTATIONS` 五元组、`main()` 的三重门），不自创第二套框架。

三重门（与 mut_attention 逐条同形，第三门按派工收窄到具名判据）：
  1. **baseline 必须绿** —— 开跑前把 13 个判据文件整场跑一遍（2026-10-05 现取 287 passed），副本没搭对
     时后面全不算数，直接退出 1；这一跑同时保证每条具名判据在打变异前都是绿的；
  2. `s.count(old) != 1` ⇒ 报"锚点失效"（变异根本没打上，判据绿是假的）；
  3. 打了坏改动之后：**只跑 `-k <那条具名判据>`，它必须变红**，而且 pytest 报告里要有真的
     `failed` 行 —— 只有非零退出码不算数（`-k` 名字写错 ⇒ "no tests ran" 也是非零，那不等于抓到）。
另外加一道**电池自己**的门禁（`电池自检`）：每轮打完变异后，其余可变异副本必须与真源逐字
相同 ⇒ 证明"整场重打副本"真的在起作用（实测把 `_stage(tmp, COPIES)` 摘掉后这条立刻报红，
退出码非 0；不摘掉时全程静默）。任何"锚点失效""未被抓到""自检残留"都会非零退出。

为什么在临时副本里做：判据的 ROOT 从 `__file__` 往上三层推 ⇒ 把源文件与判据复制进
tmp 再跑 pytest，ROOT 就指向 tmp。真仓一个字节不动，也不会和同时在工作的别的会话抢文件；
默认**不发网络、不写工作树**（`test_*` 里凡是抓取的入口都被 monkeypatch 打桩）。

用法：
    py -3.11 tools/mut_reader.py

**逐靶只跑它那一条具名判据**（`pytest <判据所在文件> -k <判据名>`），**不跑整目录** ——
四目录 A2 全集一轮 58 s，× 40 条靶就是 38 分钟，那正是上一轮被轮次上限打断的原因。
整目录口径归 A2 门禁；电池只回答"这条坏改动有没有被它点名那条判据咬住"，跑整场反而
会把"红在别的判据上"误算成抓到。这是手动常驻电池，**不进 CI**（沿用 `tools/mut_*.py` 现状）。

三条口径写在这里而不是注释里：
  · 判据名必须唯一存在（`_index_criteria()` 在开跑前就检查，写错名字直接报"判据不存在"，
    不许静默当成"没红"）；
  · 靶子的旧串在现源码里出现 0 次或 >1 次 ⇒ 报"锚点失效"并**继续跑完其余靶**，
    修的是靶/判据，**不是**把判据放宽让电池过；
  · JS/Python 两侧的"同一规则两份实现"分叉，一律钉在**对账判据**上
    （`test_hn_summary_rewrite_matches_python` / `test_hn_word_tables_are_the_same_ordered_list`
    / `test_normalize_body_html_matches_python`），单边改一定红。
"""
import io
import os
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ── 每轮从干净工作树重打的**可变异**文件（变异绝不互相掩盖）
# `api/article.js` 是派工 F1 补进来的：它在 ONCE（只读）时，电池**结构上**打不到它，
# 于是"出口算了但丢弃结果"这种形状没有任何一条靶能归宿 ⇒ 判据只能靠自己长牙。
COPIES = [
    "build_rss_aggregator.py",
    os.path.join("lib", "body_rules.js"),
    os.path.join("api", "rss.js"),
    os.path.join("api", "article.js"),
]
# ── 只读依赖：搭一次就够（不可变异，也省掉 64 MB 语料的每轮重拷）
ONCE = [
    os.path.join("lib", "rss_cover.js"),
    os.path.join("lib", "rel_time.js"),
    os.path.join("lib", "rss_retention.js"),
    os.path.join("api", "news.js"),
    os.path.join("api", "search.js"),
    os.path.join("api", "translate.js"),
    "build_logger.py",
    "rss_sources.json",
    "rss_history.json",
    os.path.join("tests", "rss_composite", "_loader.py"),
    os.path.join("tests", "rss_history", "_loader.py"),
    os.path.join("tests", "rss_js", "test_body_rules.js"),
]
# ── 整场跑的判据清单（电池只装"这批修复的判据"，不装全站 —— 全站是 A2 门禁的活）
TESTS = [
    os.path.join("tests", "rss_history", "test_body_html_normalize.py"),
    os.path.join("tests", "rss_history", "test_body_html_exits.py"),
    os.path.join("tests", "rss_history", "test_body_noise_strip.py"),
    os.path.join("tests", "rss_history", "test_hn_summary_rewrite.py"),
    os.path.join("tests", "rss_history", "test_hn_zh_wordlist.py"),
    os.path.join("tests", "rss_history", "test_history_knife_sweep.py"),
    os.path.join("tests", "rss_history", "test_no_inert_deep_clean_rules.py"),
    os.path.join("tests", "site_nav", "test_body_rules_parity.py"),
    os.path.join("tests", "site_nav", "test_article_contract.py"),
    os.path.join("tests", "site_nav", "test_reader_summary_dedupe.py"),
    os.path.join("tests", "site_nav", "test_reader_body_images.py"),
    os.path.join("tests", "site_nav", "test_reader_fulltext_errors.py"),
    os.path.join("tests", "site_nav", "test_reader_hn_discussion.py"),
]

FULL = chr(0xFF1A)      # 全角冒号：一律写码位，不贴字形（与半角 ':' 看不出区别）

# (变异 id, 目标文件, 原文, 坏改动, 必须被哪条判据挡住)
MUTATIONS = [
    # ── ① 懒加载提升：`(?<![\w-])` 是"src 属性"的唯一口径 ────────────────────
    ("R01 IMG_SRC 去守卫", COPIES[0],
     u"_IMG_SRC_ATTR_RE = re.compile(r'(?<![\\w-])src\\s*=\\s*\"([^\"]*)\"', re.IGNORECASE)",
     u"_IMG_SRC_ATTR_RE = re.compile(r'src\\s*=\\s*\"([^\"]*)\"', re.IGNORECASE)",
     u"test_lazy_src_promoted_when_src_absent"),
    ("R02 URL_ATTR 去守卫", COPIES[0],
     u"_URL_ATTR_RE = re.compile(r'(?<![\\w-])(src|href|poster)\\s*=\\s*\"([^\"]*)\"', re.IGNORECASE)",
     u"_URL_ATTR_RE = re.compile(r'(src|href|poster)\\s*=\\s*\"([^\"]*)\"', re.IGNORECASE)",
     u"test_normalize_body_html_matches_python"),

    # ── ② 占位判定：关掉就等于把真图当占位换掉（或反之，方向一律朝"保留原 src"）──
    ("R03 占位判定关掉", COPIES[0],
     u"        if cur_v and not _is_placeholder_src(cur_v):",
     u"        if cur_v:",
     u"test_placeholder_src_replaced_by_lazy_value"),

    # ── ③ 残缺标签：`<[^>]*>$` 方向反了，只命中完整标签，半截标签逃掉 ────────
    ("R04 残缺标签方向反", COPIES[0],
     u'_DANGLING_TAG_RE = re.compile(r"<[^>]*$")',
     u'_DANGLING_TAG_RE = re.compile(r"<[^>]*>$")',
     u"test_snapshot_exit_caps_without_dangling_tag"),

    # ── ④ 实体刀：三件事各自钉一条（后继字符 / 名段级联 / 无界数字改成受窗口限制）──
    ("R05 实体要求后继字符", COPIES[0],
     u'_DANGLING_ENTITY_RE = re.compile(r"&[#A-Za-z0-9]{0,8}$")',
     u'_DANGLING_ENTITY_RE = re.compile(r"&[#A-Za-z0-9]{1,8}$")',
     # 裸 `&` 在**行为**上今天被两处同时兜着（名段正则的 `{0,8}` + `_unambiguous_entity_start`
     # 的 `&$` 分支），单改量词谁都不漏 ⇒ 真正咬得住的是"窗口宽度是从这条正则的量词**推**出来
     # 的"那笔账：`test_cap_entity_window_is_wide_enough_for_eight_name_chars` 直接从 pattern
     # 里读 `{0,N}`，量词一改就读不到 ⇒ 当场红。派工点名的"裸 & 逃过"这一半登记在报告里。
     u"test_cap_entity_window_is_wide_enough_for_eight_name_chars"),
    ("R06 名段刀做成可级联", COPIES[0],
     u"            first_entity_knife = False",
     u"            first_entity_knife = True",
     u"test_cap_second_knife_strips_only_unambiguous_dangling_entity"),
    ("R07 无界数字刀受9字符窗口限制", COPIES[0],
     u"        nxt = _unambiguous_entity_start(out, tail)\n",
     (u"        _w0 = max(0, tail - _ENTITY_TAIL_WINDOW)\n"
      u"        _mw = _DANGLING_ENTITY_UNAMBIG_RE.search(out[_w0:tail])\n"
      u"        nxt = (_w0 + _mw.start()) if _mw else None\n"),
     u"test_cap_body_matches_python"),

    # ── ⑤ 懒加载优先级就是元组顺序 ──────────────────────────────────────────
    ("R08 懒加载顺序反转", COPIES[0],
     u'_LAZY_SRC_ATTRS = ("data-src", "data-original", "data-lazy-src", "data-croporisrc")',
     u'_LAZY_SRC_ATTRS = ("data-croporisrc", "data-lazy-src", "data-original", "data-src")',
     u"test_lazy_attr_lists_are_the_same_ordered_list"),

    # ── ⑥ 截断：早返回 / 上限比较 / 窗口宽度 ────────────────────────────────
    ("R09 截断早返回去掉", COPIES[0],
     u"    if len(text) <= limit:\n        return text\n",
     u"",
     u"test_cap_leaves_untruncated_body_byte_for_byte"),
    ("R10 上限比较改成严格小于", COPIES[0],
     u"    if len(text) <= limit:\n        return text\n",
     u"    if len(text) < limit:\n        return text\n",
     u"test_cap_leaves_exactly_at_limit_body_with_a_dangling_tail"),
    ("R11 实体窗口改小", COPIES[0],
     u"_ENTITY_TAIL_WINDOW = 9",
     u"_ENTITY_TAIL_WINDOW = 4",
     u"test_cap_entity_window_is_wide_enough_for_eight_name_chars"),

    # ── ⑦ 历史重写：不接线 / 函数体空转（正文侧与摘要侧各一条）────────────
    ("R12 历史重写不接线", COPIES[0],
     u"    _hist_normed = _renormalize_history_fulltext(_rss_history)",
     u"    _hist_normed = 0",
     u"test_history_sweep_is_wired_into_the_build"),
    ("R13 正文侧重写空转", COPIES[0],
     u"            _new = _delink_nav_links(_norm)",
     u"            _new = _fc",
     u"test_sweep_knives_are_called_from_the_sweep_function"),
    ("R14 摘要侧重写空转", COPIES[0],
     u"            _new = _clean_history_summary(_old, _link, _t)",
     u"            _new = _old",
     u"test_history_sweep_rewrites_hn_template_english"),

    # ── ⑧ 解析入口三层顺序（RSS 与 Atom 是两段代码，各钉一条）/ 快照裸切片 ──
    # R15 钉**行为**判据而不是链形 grep：`test_normalize_runs_before_sanitize` 只做全文子串
    # 比对，折行写的倒置（`_normalize_body_html(\n  _sanitize_html(`）它看不见（实测照样绿），
    # 而 `_unescape → normalize → sanitize` 这条链一倒，微信转义正文里的懒加载 img 就在出口
    # 死掉 —— 行为判据当场红。grep 判据的这格盲区登记在报告里。
    ("R15 RSS 入口 normalize 挪到 sanitize 之后", COPIES[0],
     (u"        content_encoded = _deep_clean_html(_sanitize_html(_normalize_body_html(\n"
      u"            _unescape_escaped_body(content_raw), link)))"),
     (u"        content_encoded = _deep_clean_html(_normalize_body_html(\n"
      u"            _sanitize_html(_unescape_escaped_body(content_raw)), link))"),
     u"test_rss_parse_entry_fixes_escaped_wechat_body"),
    ("R16 Atom 入口 normalize 挪到 sanitize 之后", COPIES[0],
     (u"            atom_content = _deep_clean_html(_sanitize_html(_normalize_body_html(\n"
      u"                _unescape_escaped_body(content_raw), link)))"),
     (u"            atom_content = _deep_clean_html(_normalize_body_html(\n"
      u"                _sanitize_html(_unescape_escaped_body(content_raw)), link))"),
     u"test_atom_parse_entry_fixes_escaped_wechat_body"),
    ("R17 快照退回裸切片", COPIES[0],
     u'                item["fc"] = _strip_oss_signature(_cap_body(fc))',
     u'                item["fc"] = _strip_oss_signature(fc[:50000])',
     u"test_snapshot_exit_is_wired"),

    # ── ⑨ 双重转义正文：解一层这一步被拆掉 ──────────────────────────────────
    ("R18 RSS 入口拆掉 _unescape_escaped_body", COPIES[0],
     u"        content_encoded = _deep_clean_html(_sanitize_html(_normalize_body_html(\n"
     u"            _unescape_escaped_body(content_raw), link)))",
     u"        content_encoded = _deep_clean_html(_sanitize_html(_normalize_body_html(\n"
     u"            content_raw, link)))",
     u"test_rss_parse_entry_fixes_escaped_wechat_body"),

    # ── ⑩ 实时出口 `api/rss.js`：三把刀逐个摘 + 顺序倒置 ────────────────────
    ("R19 rss.js 摘掉 HN 刀", COPIES[2],
     u"return collapseRuns(BODY.stripGluedUrl(BODY.rewriteHnSummary(stripHtmlKeepLines(raw), link)));",
     u"return collapseRuns(BODY.stripGluedUrl(stripHtmlKeepLines(raw)));",
     u"test_realtime_rss_summary_rewrites_the_hn_template"),
    ("R20 rss.js 摘掉粘连刀", COPIES[2],
     u"return collapseRuns(BODY.stripGluedUrl(BODY.rewriteHnSummary(stripHtmlKeepLines(raw), link)));",
     u"return collapseRuns(BODY.rewriteHnSummary(stripHtmlKeepLines(raw), link));",
     u"test_realtime_rss_summary_strips_the_glued_url"),
    ("R21 rss.js 刀序倒置（先粘连后 HN）", COPIES[2],
     u"return collapseRuns(BODY.stripGluedUrl(BODY.rewriteHnSummary(stripHtmlKeepLines(raw), link)));",
     u"return collapseRuns(BODY.rewriteHnSummary(BODY.stripGluedUrl(stripHtmlKeepLines(raw)), link));",
     u"test_realtime_summary_knives_run_in_the_buildtime_order"),
    ("R22 rss.js 摘掉脱链刀", COPIES[2],
     u"  text = BODY.delinkNavLinks(text);",
     u"  // text = BODY.delinkNavLinks(text);",
     u"test_realtime_body_delinks_nav_links_and_keeps_external"),

    # ── ⑪ HN 词表只留英文（中文变体必红）/ 门槛降到一行 ─────────────────────
    ("R23 CMTS 词表只留英文", COPIES[0],
     u'_HN_CMTS_WORDS = (u"comments", u"评论数量", u"评论数", u"评论")',
     u'_HN_CMTS_WORDS = (u"comments",)',
     u"test_every_counted_real_form_is_stripped"),
    ("R24 PTS 词表只留英文", COPIES[0],
     (u'_HN_PTS_WORDS = (u"points", u"积分", u"当前得分", u"得分", u"评分", u"点赞数",\n'
      u'                 u"热度值", u"热度", u"分数", u"点数", u"分值", u"关注度")'),
     u'_HN_PTS_WORDS = (u"points",)',
     u"test_every_counted_real_form_is_stripped"),
    ("R25 门槛降到命中一行", COPIES[0],
     u"    if hits < 2:\n        return desc",
     u"    if hits < 1:\n        return desc",
     u"test_one_meta_line_alone_is_not_enough"),

    # ── ⑫ JS 端口只改一边 / 两侧"一起改"其实不等价 ─────────────────────────
    ("R26 JS 词表单边少一个中文词", COPIES[1],
     u'const HN_CMTS_WORDS = ["comments", "\\u8bc4\\u8bba\\u6570\\u91cf", "\\u8bc4\\u8bba\\u6570", "\\u8bc4\\u8bba"];',
     u'const HN_CMTS_WORDS = ["comments"];',
     u"test_hn_word_tables_are_the_same_ordered_list"),
    ("R27 Python 数字段改回 \\d（Unicode 口径）", COPIES[0],
     u'_HN_POINTS = re.compile(r"^(?:%s)\\s*[:\\uFF1A]\\s*([0-9]+)\\s*$"',
     u'_HN_POINTS = re.compile(r"^(?:%s)\\s*[:\\uFF1A]\\s*(\\d+)\\s*$"',
     u"test_hn_summary_rewrite_matches_python"),
    ("R28 JS 粘连刀的空白类改用引擎 \\s", COPIES[1],
     u'  "^" + WS_STAR + "https?://[\\\\x21-\\\\x7e]+?',
     u'  "^\\\\s*https?://[\\\\x21-\\\\x7e]+?',
     u"test_glued_url_strip_matches_python"),

    # ── ⑬ 前端（住在 build_rss_aggregator.py 的产物 JS 里）──────────────────
    ("R29 失败又被缓存", COPIES[0],
     (u"      if(d.ok&&d.content){\n"
      u"        /* 只缓存成功：把失败也缓存的话，会话内每次重试都拿回同一份失败并永久静默 */\n"
      u"        _articleCache[a.u]=d;"),
     (u"      _articleCache[a.u]=d;\n"
      u"      if(d.ok&&d.content){\n"
      u"        /* 只缓存成功：把失败也缓存的话，会话内每次重试都拿回同一份失败并永久静默 */"),
     u"test_failure_result_is_not_cached"),
    ("R30 _FT_NOTES 少一个码", COPIES[0],
     u"    'timeout':'抓取原文超时（对方站点响应过慢），下面保留的是信源自带摘要。',\n",
     u"",
     u"test_error_code_table_has_no_off_contract_entries"),
    ("R31 harden 定义了不调用", COPIES[0],
     u"    _hardenBodyImages(div,curArt&&curArt.u);\n",
     u"",
     u"test_harden_is_called_from_fulltext_insert"),
    ("R32 referrer 改子串匹配", COPIES[0],
     u"      if (host === d || host.slice(-(d.length + 1)) === '.' + d) return true;",
     u"      if (host.indexOf(d) >= 0) return true;",
     u"test_lookalike_host_is_not_matched_as_substring"),
    ("R33 空策略写成 attr= 而非 removeAttribute", COPIES[0],
     u"      if(pol) im.setAttribute('referrerpolicy',pol); else im.removeAttribute('referrerpolicy');",
     u"      im.setAttribute('referrerpolicy', pol || '');",
     u"test_blank_policy_uses_remove_attribute_not_empty_attr"),

    # ── ⑭ 加回一条开不了火的 class/id 刀（空转刀闸必须红）───────────────────
    ("R34 加回空转的容器 class 刀", COPIES[0],
     u'    # 1.5 移除 wechat2rss / link-proxy 跳转链接（"跳转微信打开"等）\n',
     (u'    text = re.sub(r\'<(?:div|aside)\\b[^>]*class="[^"]*(?:related-posts|advert)[^"]*"[^>]*>'
      u'[\u5220]'
      u'</(?:div|aside)>\', \'\', text, flags=re.IGNORECASE)\n'
      u'    # 1.5 移除 wechat2rss / link-proxy 跳转链接（"跳转微信打开"等）\n').replace(u"\u5220", u"[\\s\\S]*?"),
     u"test_every_deep_clean_rule_actually_fires"),

    # ── ⑮ lib/body_rules.js 新增一个无调用方的导出 ───────────────────────────
    ("R36 JS 新增孤儿导出", COPIES[1],
     u"  delinkNavLinks: delinkNavLinks,",
     u"  delinkNavLinks: delinkNavLinks,\n  ENTITY_NAME_MAX: ENTITY_NAME_MAX,",
     u"test_body_rules_exports_all_have_callers"),

    # ── ⑯ R47：HN 形状内剥裸 URL 行 ─────────────────────────────────────────
    ("R37 摘掉 R47 那把刀", COPIES[0],
     u"    keep = [ln for ln in keep if not _HN_BARE_URL_LINE.match(ln)]",
     u"    keep = list(keep)",
     u"test_hn_shape_bare_url_lines_are_stripped"),
    ("R38 R47 做成通用剥 URL 行", COPIES[0],
     (u'    if hits < 2:\n        return desc\n'
      u'    if not (pts or cmt):\n        return desc'),
     (u'    keep = [ln for ln in keep if not _HN_BARE_URL_LINE.match(ln)]\n'
      u'    while keep and not keep[-1]:\n        keep.pop()\n'
      u'    if hits < 2:\n        return "\\n".join(keep) if keep else desc\n'
      u'    if not (pts or cmt):\n        return "\\n".join(keep) if keep else desc'),
     u"test_bare_url_line_from_an_ordinary_summary_is_kept"),
    ("R39 R47 值段退回 \\S（吃掉粘连行）", COPIES[0],
     u'_HN_BARE_URL_LINE = re.compile(r"^https?://[\\x21-\\x7e]+$", re.IGNORECASE)',
     u'_HN_BARE_URL_LINE = re.compile(r"^https?://\\S+$", re.IGNORECASE)',
     u"test_hn_shape_bare_url_lines_are_stripped"),
    ("R40 JS 侧 R47 刀单边摘掉", COPIES[1],
     u"    if (!HN_BARE_URL_LINE.test(keep[i])) kept.push(keep[i]);",
     u"    kept.push(keep[i]);",
     u"test_hn_summary_rewrite_matches_python"),
    ("R41 JS 侧 R47 值段单边改宽", COPIES[1],
     u'const HN_BARE_URL_LINE = new RegExp("^https?://[\\\\x21-\\\\x7e]+$", "iu");',
     u'const HN_BARE_URL_LINE = new RegExp("^https?://" + NOT_WS_STAR + "+$", "iu");',
     u"test_hn_summary_rewrite_matches_python"),
    # 单边靶成对：R40 只动 JS、R42 只动 Python，两条都钉在**对账判据**上 ⇒ 两侧任一侧漏掉
    # R47 那把刀都红。行为判据（R37/R39）只看得到"自己这一侧"，分叉必须靠对账判据收口。
    ("R42 Python 侧 R47 刀单边摘掉", COPIES[0],
     u"    keep = [ln for ln in keep if not _HN_BARE_URL_LINE.match(ln)]",
     u"    keep = list(keep)",
     u"test_hn_summary_rewrite_matches_python"),

    # ── ⑰ 粘连刀与 HN 刀的**先后**（三处出口各一条，倒序在行为上不可见的地方链形判据兜住）──
    # 契约：`_rewrite_hn_summary` 在内、`_strip_glued_url` 在外、最外层 `_truncate`。
    # 剥在前会改掉 HN 重写要看的行首形状（两把刀互相吞），三处出口漏任一处 ⇒ 那一半照旧露。
    ("R43 RSS item 出口刀序倒置", COPIES[0],
     (u'        "title": title, "link": link, '
      u'"summary": _truncate(_strip_glued_url(_rewrite_hn_summary(desc, link))),\n'
      u'        "full_content": full_content, "image": img,'),
     (u'        "title": title, "link": link, '
      u'"summary": _truncate(_rewrite_hn_summary(_strip_glued_url(desc), link)),\n'
      u'        "full_content": full_content, "image": img,'),
     u"test_rss_parse_entry_wraps_glued_strip_in_the_right_order"),
    ("R44 Atom 出口刀序倒置", COPIES[0],
     (u'                "title": title, "link": link, '
      u'"summary": _truncate(_strip_glued_url(_rewrite_hn_summary(desc, link))),\n'
      u'                "full_content": full_content, '
      u'"image": _upgrade_img_url(_pick_item_image(e, summary_raw, content_raw)),'),
     (u'                "title": title, "link": link, '
      u'"summary": _truncate(_rewrite_hn_summary(_strip_glued_url(desc), link)),\n'
      u'                "full_content": full_content, '
      u'"image": _upgrade_img_url(_pick_item_image(e, summary_raw, content_raw)),'),
     u"test_rss_parse_entry_wraps_glued_strip_in_the_right_order"),
    ("R45 快照 s 出口刀序倒置", COPIES[0],
     (u'                "s": _strip_oss_signature(_strip_glued_url(\n'
      u'                    _rewrite_hn_summary('),
     (u'                "s": _strip_oss_signature(_rewrite_hn_summary(\n'
      u'                    _strip_glued_url('),
     u"test_snapshot_exit_also_wraps_glued_strip"),

    # ⑱ 同一个 R15 倒置的**第二种写法**：链形 grep 口径必须留着（派工：不许丢任何一条已有点名）。
    # `test_normalize_runs_before_sanitize` 是全文子串比对，**看不见折行**写的倒置（实测 R15 那样
    # 写它照样绿）⇒ R15 改钉行为判据，这条把倒置写成单行，grep 口径照红。两条合起来才是
    # "倒置无论怎么写都红"，谁都没被放宽。
    ("R46 RSS 入口倒置（单行写法，钉 grep 口径）", COPIES[0],
     (u"        content_encoded = _deep_clean_html(_sanitize_html(_normalize_body_html(\n"
      u"            _unescape_escaped_body(content_raw), link)))"),
     (u"        content_encoded = _deep_clean_html(_normalize_body_html("
      u"_sanitize_html(_unescape_escaped_body(content_raw)), link))"),
     u"test_normalize_runs_before_sanitize"),

    # ── ⑲ 对抗审查 ①：Atom 实时出口的四把刀（以前这段只造 {t,u,s,d}）──────────
    ("R47 Atom 出口摘掉 fc", COPIES[2],
     u"      const cleanedBody = contentEncoded ? buildFullContent(contentEncoded, link) : '';",
     u"      const cleanedBody = contentEncoded;",
     u"test_realtime_atom_entry_produces_full_content"),
    ("R48 Atom 出口整条摘掉清洗链", COPIES[2],
     u"        if (fullContent) item.fullContent = fullContent;",
     u"",
     u"test_realtime_atom_and_rss_exits_agree_on_the_same_body"),

    # ── ⑳ 对抗审查 ④：构建期规则 2.6（空锚点/根链接整枚删）接到实时出口 ────────
    # 靶 A：那一刀从实时出口摘掉 ⇒ 同一批样本两侧逐条分叉（对账判据咬）。
    # 靶 B：形状放宽成任意 href ⇒ 正文里的页内锚点 `#sec-2` 被连文字吃掉
    #        （出口形状判据咬；构建期同名反例见 test_body_noise_strip.py 的
    #         test_in_page_fragment_link_is_not_dropped，两侧同一条红线）。
    ("R49 实时出口摘掉空锚点刀", COPIES[2],
     u"  text = BODY.dropEmptyAnchorLinks(text);",
     u"",
     u"test_realtime_deep_clean_matches_buildtime_on_empty_anchor_shapes"),
    ("R50 空锚点刀放宽成任意 href", COPIES[1],
     u'href=\\"(?:#|/)\\"',
     u'href=\\"[^\\"]*\\"',
     u"test_realtime_fulltext_exit_has_no_empty_anchor_shell"),

    # ── 21 对抗审查 ②：清扫改的对象 与 三个消费者读的对象，必须是同一个 ──────
    # 缺陷形状：`_accumulate_history` 在 :909 用 `dict(item)` 造**出厂副本**，主流程的清扫
    # 在那之后只改 `_rss_history` ⇒ 历史干净、出厂仍是旧形状。R51 打"函数体空转"（行为半），
    # R52 打"主流程那行被摘掉"（接线半）—— 两条各咬一半，缺任一条就留下"grep 绿而货是旧的"
    # 或"函数写着没人调"的盲区（原判据 `test_history_sweep_is_wired_into_the_build` 只有
    # grep 半，所以 R51 那种空转它照样放过去）。
    ("R51 出厂副本清扫空转", COPIES[0],
     (u"    _by_id = {str(id(_it)): _it\n"
      u"              for _src in sources_with_items\n"
      u"              for _it in (_src.get(\"items\") or [])}"),
     u"    _by_id = {}",
     u"test_outgoing_copies_are_swept_not_just_the_history_dict"),
    ("R52 出厂清扫那行从主流程摘掉", COPIES[0],
     u"    _out_normed = _renormalize_outgoing_fulltext(sources_with_items)",
     u"    _out_normed = 0",
     u"test_history_sweep_is_wired_into_the_build"),

    # ── 22 对抗审查 ③：运行时实体解码（裁定 R50 的最小集合）────────────────
    # 端口在 `lib/body_rules.js`、接线在 `api/rss.js` 的摘要那一格，两边各自钉：
    #   R53 打"未知命名实体被替成空串"（那才是本批修的缺陷，不是"少解一个字符"），
    #   R54 打"数字实体被替成空串"（旧实现吃 `&#20998;` 这种汉字），
    #   R55 打"分号形态不认了"（真语料实测到 `&gt；` 走的就是可省分号那一格），
    #   R56/R57 打坏码表的**分支顺序**（三张表有重叠区，顺序换了 `&#128;` 就变空串），
    #   R58 打接线（端口写对但出口没调用 = 等于没修，本仓点名的"定义了不调用"形状）。
    # 每条都点名一条**具名判据**：§同面 / §穷举 / §分叉 / §出口 四条各管一格，不许互相顶。
    ("R53 未知命名实体又被吃成空串", COPIES[1],
     u"    return _decodeNumericEntity(digits);\n  });",
     u"    return _decodeNumericEntity(digits);\n  }).replace(/&[a-z]+;/gi, \"\");",
     # 锚点 2026-10-05 换形（**语义没动**）：P0-3 扩表把回调体从
     # `if (typeof name === "string") return _ENT_MIN[name];` 改成带 `_ENT_SEMI_ONLY` 分支的
     # 多行体，旧锚点在现源码里出现 0 次 ⇒ 变异根本打不上（电池会报"锚点失效"）。
     # 现在钉的是"整个 replace 之后再接一刀"这一格，坏改动与判据归宿都与换形前逐字相同。
     u"test_entity_named_divergence_is_declared_and_eats_nothing"),
    ("R54 数字实体整枚被替成空串", COPIES[1],
     u"    return _decodeNumericEntity(digits);",
     u'    return "";',
     u"test_entity_unescape_matches_python"),
    ("R55 可省分号的 legacy 形态不认了", COPIES[1],
     u'  "&#([0-9]+|[xX][0-9a-fA-F]+);?|&(amp|AMP|lt|LT|gt|GT|quot|QUOT|nbsp|apos|rsquo|rarr|ldquo|rdquo);?", "gu");',
     u'  "&#([0-9]+|[xX][0-9a-fA-F]+)|&(amp|AMP|lt|LT|gt|GT|quot|QUOT|nbsp|apos|rsquo|rarr|ldquo|rdquo)", "gu");',
     # 锚点 2026-10-05 跟着 P0-3 的扩表换形（**语义没动**：两支的 `;?` 一起去掉）：
     # 旧锚是扩表前那一行的字面量，在现源码里已经 0 次。
     u"test_entity_port_matches_python_on_every_known_form"),
    ("R56 坏码位表整张关掉", COPIES[1],
     u"  if ((n >= 0x01 && n <= 0x08) || n === 0x0b || (n >= 0x0e && n <= 0x1f) || n === 0x7f) return true;",
     u"  if (false && (n >= 0x01 && n <= 0x08)) return true;",
     u"test_entity_port_matches_python_on_every_known_form"),
    ("R57 规范映射那一格被当成坏码位（分支顺序写反）", COPIES[1],
     u"  if (num >= 0x80 && num <= 0x9f) return _ENT_CP1252.charAt(num - 0x80);",
     u'  if (num >= 0x80 && num <= 0x9f) return "";',
     u"test_entity_port_matches_python_on_every_known_form"),
    ("R58 实时出口摘掉实体端口（退回旧级联）", COPIES[2],
     u"  return stripTagsKeepLines(BODY.decodeEntities(unwrapCdata(text)));",
     u"  return stripTagsKeepLines(legacyEntityCascade(unwrapCdata(text)));",
     u"test_realtime_summary_exit_shares_the_python_entity_port"),

    # ── 24 审查 ⑤ + 派工 F1：`/api/article` 现抓通道的出口整形（COPIES[3] 是这一批新增的）──
    # 这三条靶同时是"电池能不能打得到 article.js"这件事的第一份证据：以前它在 ONCE（只读），
    # F1 那种"算了但丢弃结果"的假绿在电池里**没有归宿**，判据只能靠实跑切片自己长牙。
    # F01 = 审查 ⑤ 的缺陷形状（脱链刀零调用）；F02 = 派工点名的原样变异（算过但不落到出口）；
    # F03 = cap 从出口摘掉（中途算过不算，产物仍是 50029 字符）。
    ("F01 article 现抓通道摘掉脱链刀", COPIES[3],
     u"    const content = stripRemoteExecutables(BODY.capBody(BODY.delinkNavLinks(BODY.normalizeBodyHtml(result.content, url))));",
     u"    const content = stripRemoteExecutables(BODY.capBody(BODY.normalizeBodyHtml(result.content, url)));",
     u"test_article_live_channel_delinks_insite_nav_links"),
    # 注：裁定 ④ 之后出口那一行外面多了一层 `stripRemoteExecutables(...)` ⇒ 两条靶的锚点跟着走，
    # 坏改动里**保留**那层壳，这样 F02/F03 各自量的还是它们本来的那一格（丢规范化 / 不截断）。
    ("F02 article 规范化算了但丢弃", COPIES[3],
     u"    const content = stripRemoteExecutables(BODY.capBody(BODY.delinkNavLinks(BODY.normalizeBodyHtml(result.content, url))));",
     (u"    const _normalized = BODY.delinkNavLinks(BODY.normalizeBodyHtml(result.content, url));\n"
      u"    const content = stripRemoteExecutables(BODY.capBody(result.content));"),
     u"test_article_output_is_normalized_and_capped"),
    ("F03 article 出口没截断", COPIES[3],
     u"    const content = stripRemoteExecutables(BODY.capBody(BODY.delinkNavLinks(BODY.normalizeBodyHtml(result.content, url))));",
     u"    const content = stripRemoteExecutables(BODY.delinkNavLinks(BODY.normalizeBodyHtml(result.content, url)));",
     u"test_article_output_is_normalized_and_capped"),

    # ── 25 派工 F2：空转刀闸以前只认 `re.sub(r"…", …)`，编译型 `_X.sub('', text)` 两侧隐形。
    # R59 就是审查那个注入形状（按 class 匹配的容器刀、写成编译型常量、塞进 `_deep_clean_html`）：
    # 旧口径下闸自己 4 passed、电池全绿、退出码 0；现在两条判据都要红
    # （`_static_rule_patterns` 认得出编译型接收者 ⇒ 点名判据红；动态探针的代理也记得到 ⇒ 主闸红）。
    ("R59 加回编译型写法的空转容器刀", COPIES[0],
     u'    # 1.5 移除 wechat2rss / link-proxy 跳转链接（"跳转微信打开"等）\n',
     (u"    _INERT_COMPILED_KNIFE = re.compile(\n"
      u"        r'<(?:div|aside)\\b[^>]*\\b(?:class|id)\\s*=\\s*\"[^\"]*"
      u"\\b(?:related-posts|advert)[^\"]*\"[^>]*>[\\s\\S]*?</(?:div|aside)>', re.IGNORECASE)\n"
      u"    text = _INERT_COMPILED_KNIFE.sub('', text)\n"
      u'    # 1.5 移除 wechat2rss / link-proxy 跳转链接（"跳转微信打开"等）\n'),
     u"test_no_rule_keys_on_attributes_the_sanitizer_cannot_emit"),

    # ── 26 复评 B1+B2：JS Atom 出口的 s / fc 与 Python 不同判（上一波自己带进去的）──
    # B01 = 把 `summary` 改回"回退到 content"（派工点名的靶）；B02 = 把 `fc` 门槛改回
    # 比两个**原始标签**的长度（另一派工点名的靶，只换基准、不动清洗链，免得和 R47 混成一件事）；
    # B03 = 门槛右边换成会压内部空白的 `stripHtml`（参照物 `_strip_html` 只 `.strip()` 首尾，
    #       压了就短一截、HN 四行那种多行摘要会把"该不该出 fc"翻面）。
    ("B01 Atom 摘要又回退到 content", COPIES[2],
     u"      const summary = summaryTag;",
     u"      const summary = summaryTag || extractTag(entry, 'content');",
     u"test_realtime_atom_never_ships_the_same_body_as_both_summary_and_fulltext"),
    ("B02 Atom 门槛退回比原始标签长度", COPIES[2],
     u"      const fullContent = cleanedBody.length > summaryPlain.length ? cleanedBody : '';",
     u"      const fullContent = contentEncoded.length > summaryTag.length ? cleanedBody : '';",
     u"test_realtime_atom_exit_matches_python_s_and_fc_shape_by_shape"),
    ("B03 Atom 门槛右边换成压空白的 stripHtml", COPIES[2],
     u"      const summaryPlain = stripHtmlKeepLines(summaryTag).trim();",
     u"      const summaryPlain = stripHtml(summaryTag);",
     u"test_realtime_atom_exit_matches_python_s_and_fc_shape_by_shape"),
    # B04 = 复评 B1 的**同一格在 RSS 分支**的那一半（派工点名的靶）：把摘要改回
    # `desc || contentEncoded` ⇒ `<description>` 缺失时条目带着"正文前 200 字"的摘要出厂，
    # 与参照物（`desc = _strip_html(desc_raw)`，**不回退**）不同判，阅读器里同一份正文两遍。
    # 钉**行为对账**那条（三面量：参照物 summary / parseFeed 的 summary / 出厂卡片的 s），
    # 不钉 grep 形状 —— 那行改回退后字符串仍然"像对的"。
    ("B04 RSS 摘要又回退到正文", COPIES[2],
     u"        summary: truncate(stripHtml(desc), SUMMARY_CAP),",
     u"        summary: truncate(stripHtml(desc || contentEncoded), SUMMARY_CAP),",
     u"test_realtime_rss_exit_matches_python_summary_shape_by_shape"),
    # 同一对缺陷在 **RSS 那一半**：摘要回退那一格（B04）本批收了，靶跟着登记在这里；
    # **门槛那一格仍然没有靶**（实测 py `s=''` / js `fc` 门槛比的是两个原始标签长度，
    # CDATA 白送 12）—— 把"先清洗再定门槛"搬到 RSS 分支会让 `parseFeed` 对每条带
    # `<content:encoded>` 的条目都过 `BODY`，而两条只注入 `COVER` 的他人线 node 切片判据
    # （`tests/rss_cover/test_realtime_cover_js.py`、`tests/rss_source_coverage/test_dateless_source_guard.py`）
    # 当场红在 `ReferenceError: BODY is not defined`（实测 7 条红）。
    # 台账与前置动作写在 tests/site_nav/test_article_contract.py 的"RSS 出口那一半"那段登记里；
    # 前置到位后 B05 这条靶的锚是
    # `const fullContent = cleanedBody.length > descPlain.length ? cleanedBody : '';`。
    # （`summaryRaw: desc,` 那一行另有同一格的第二种写法：新判据是三面量的，实测单改它那行
    #   两条都红，见 fixwaveB-report §B4；本电池按派工只登记 B04 这一条靶。）

    # ── 27 复评 B3：CI 上真正生效的那道实体闸（§合成）───────────────────────
    # 与 R58 打的是**同一份坏改动**（摘要出口退回旧顺序级联），但归宿不同，两条都不许省：
    #   R58 → §出口 那条接线判据，读的是 `_ENT_EXIT_SAMPLES`（两侧同源的那一圈形状）；
    #   R60 → 新增的合成闸 `test_runtime_entity_exit_gate_runs_without_any_corpus`，
    #         读的是表外命名实体（`&copy;` 被吃名字那格）+ 零语料 ⇒ CI 每场必跑。
    # 实测这条为什么必须有：同一份 R58 变异件上，旧 §哨兵（本地大语料度量）**完全无感**
    # —— 它只喂 `decodeEntities` 端口、不喂出口，而且没有 `rss_history.json` 就 skip
    # （`1 passed / 1 skipped`），所以"零暴露"那句话从来不是 CI 上的闸。
    ("R60 摘要出口退回旧级联（合成闸这一格）", COPIES[2],
     u"  return stripTagsKeepLines(BODY.decodeEntities(unwrapCdata(text)));",
     u"  return stripTagsKeepLines(legacyEntityCascade(unwrapCdata(text)));",
     u"test_runtime_entity_exit_gate_runs_without_any_corpus"),

    # ── 28 复核 P0-3 + P0-2（2026-10-05 两处修复各自的靶）─────────────────────
    # P0-3 把实体表按**现取普查**（89 个上游源的原始 feed）扩了 6 个名字并按实测分了三档分号规则；
    # P0-2 把四条出厂出口的"第二趟解码"收进唯一一份 `shipSummary`。三把靶各钉一条**新**判据：
    #   R61 从表里删 nbsp（普查里 241 处那个）→ 判据 1 的名字×形态矩阵；
    #   R62 把"必须带分号"那一档清空（= `&apos` 无分号也解）→ 还是判据 1；
    #   R63 把 `?batch=` 那条出口改回第二趟解码 → 判据 2 的出口实跑。
    # 为什么这三条必须由**新**判据抓：旧 §同面/§穷举 的样本是手写清单（没有 nbsp 的无分号形态、
    # 也没有大写 nbsp 的对照），旧接线判据只看 `cleanSummary` 那一格（四条出口它压根没看过）。
    # 现取变异自证（.deploy-tmp 的一次性脚本，读数抄在这儿）：干净副本上 A/B 两组都绿；
    #   R61 → A 组（本批没动的判据，= parity 里 `entity and not name_matrix`）**也红 2 条**
    #        （§同面、§合成）—— 因为本批按派工第 4 条把它们取数的清单换成与 `_ENT_MIN` 同源；
    #        B 组（判据 1）红 2 条，且只有它点名到"名=nbsp 形态=bare/semi/…具体哪一格"。
    #   R62 → A 组红 1 条（§同面），B 组红 2 条（判据 1，点名 apos/rsquo/rarr/ldquo/rdquo）。
    #   R63 → A 组 **38 条全绿**（contract 里除本批三条之外的一条都没碰这四条出口），
    #        B 组红 2 条（判据 2 的行为 + 接线；反证那条仍绿 = 定义还在，符合预期）。
    ("R61 nbsp 从实体表里删掉（普查 241 处那个）", COPIES[1],
     u"quot|QUOT|nbsp|apos",
     u"quot|QUOT|apos",
     # 一条靶只有一个**连续**锚点：`_ENT_RE` 与 `_ENT_MIN` 中间隔着带中文注释的两行，
     # 把它们缝成一个锚点就是把靶子挂在注释字面上（注释一改靶就"锚点失效"，本仓 R53/R55
     # 今天这样）。删掉正则里这一项之后 `t["nbsp"] = \xa0` 那格成为**不可达键**，行为上
     # "nbsp 不在这张表里"与两处一起删是同一件事 —— 判据量的是行为，正好。
     u"test_entity_name_matrix_matches_python_per_form"),
    ("R62 「必须带分号」那一档被清空", COPIES[1],
     u'const _ENT_SEMI_ONLY = new Set(["apos", "rsquo", "rarr", "ldquo", "rdquo"]);',
     u'const _ENT_SEMI_ONLY = new Set([]);',
     u"test_entity_name_matrix_matches_python_per_form"),
    ("R63 一条出口退回第二趟解码", COPIES[2],
     u"s: shipSummary(it.s),",
     u"s: truncate(stripHtml(it.s), 200),",
     u"test_summary_exits_decode_exactly_once"),

    # R64（2026-10-05 自查，任务 #31）：只删掉 shipSummary 的**剥标签**那一半（折叠留着），以此和 R65 各管一半。
    # 这一格和我这批刚犯过的错逐字相同（把 stripHtml 的两半边一起删了），旧三条出口判据全都
    # 抓不到 —— 实测 R64 下 test_summary_exits_decode_exactly_once / 接线 / 反证 三条仍全绿，
    # 只有新加的 test_summary_exit_still_strips_tags_but_never_decodes_twice 会红。
    # 锚点是函数体那两行连成的**连续**串；函数体一改就要重核锚点（本批 R64 因我改体被电池当场报"命中判据 0 条"两次 ⇒ 空靶由电池自己抓出来，不是人看出来的）。
    ("R64 出厂摘要不再剥标签（防御被我删过头）", COPIES[2],
     u"  const t = unwrapCdata(String(x || '')).replace(/<[^>]+>/g, '').replace(/<[^>]*$/, '');" + chr(10) +     u"  return truncate(collapseRuns(t), SUMMARY_CAP);",
     u"  const t = unwrapCdata(String(x || ''));" + chr(10) +     u"  return truncate(collapseRuns(t), SUMMARY_CAP);",
     u"test_summary_exit_still_strips_tags_but_never_decodes_twice"),

    # R65：把 shipSummary 里的折叠那步摘掉（= 我 R67 之前一次删过头的另一种形状）。
    # 只有 test_summary_exit_shape_equals_the_pre_batch_exit 守得住：它拿"改动前那条出口写法"
    # 当参照物逐条比字节，本地真快照 9,308 条实测当前形状与改动前 diff=0，摘掉折叠就会散开。
    # 锚点已对真源码断言命中 1 次（登记靶之后立刻核，别让空靶活到下一批）。
    ("R65 出厂摘要不再折叠空白（形状漂回改动前之外）", COPIES[2],
     u"  return truncate(collapseRuns(t), SUMMARY_CAP);",
     u"  return truncate(t, SUMMARY_CAP);",
     u"test_summary_exit_shape_equals_the_pre_batch_exit"),

    # ── 29 复评第 2/1 条（2026-10-05 补判据之后新长的三把牙）────────────────────
    # 编号说明：账本里 R66/R67 已被占（R67 = "出厂摘要不再剥标签"那一半，落在此处电池靶 R64，
    # docstring 见 tests/site_nav/test_article_contract.py 的 R67/R67b 两条），所以本轮三条从
    # R68 起编，不占用已被账本记过的号。三条的锚点登记后立刻对真源码数过一遍：**各命中 1 次**
    # （现取读数：R68=1、R69=1、R70=1，坏改动串在真源码里 0 次 ⇒ 不是"改了个已经存在的东西"）。
    #
    # R68：把表里 nbsp 的值改成空串（解成""而不是 U+00A0）。这一格旧判据是**软的**——复评实测
    # 旧的两条断言（产物里不含 &nbsp 字面量 / 产物非空且不等于输入）全部穿过：他说+NBSP+好 变成
    # 他说好，两条都满足。判据改成**比产物**（参照物 _strip_html 的产物再把 NBSP 折成普通空格）
    # 之后，本轮现取对照（.deploy-tmp 的一次性场景脚本，读数抄在这儿）：
    #   S1 新判据 ⇒ 1 failed（点名 nbsp 出口那一格与参照物不同形）
    #   S2 同一坏改动、把比产物那三行摘掉（= 改前判据）⇒ 1 passed（这就是复评说的那半）
    ("R68 nbsp 解成空串（旧软判据抓不到）", COPIES[1],
     u't["nbsp"] = "' + chr(160) + u'";',
     u't["nbsp"] = "";',
     u"test_runtime_entity_exit_gate_runs_without_any_corpus"),

    # R69：出口那步折叠空白的字符类里去掉 NBSP（只认 ASCII 空白那一串）。同一格旧判据也穿过
    # （产物留着 U+00A0，既不含 &nbsp 又非空又不等于输入）⇒ 现取对照：
    #   S3 新判据 ⇒ 1 failed（同一名话：与参照物不同形）
    #   S4 同一坏改动、改前判据 ⇒ 1 passed
    # 注：这条测的是**空白口径**那一格，与实体口径的差已在 parity 注释里登记；把它打进电池是因为
    # 复评点名的坏形状②正是"从 collapseRuns 的空白类里删掉 NBSP"。
    ("R69 出口折叠空白不再吃 NBSP", COPIES[2],
     u"  return text.replace(/\\s+/g, ' ').trim();",
     u"  return text.replace(/[ \\t\\r\\n\\f\\v]+/g, ' ').trim();",
     u"test_runtime_entity_exit_gate_runs_without_any_corpus"),

    # R70：把实体正则的 flags 从 "gu" 改成 "giu"（= 把"逐名大小写"当成一条全局规则来"修"，
    # 正是复评第 1 条点名的错法：大写对 nbsp/apos/rsquo/ldquo/rdquo 不解、对 legacy 四个解、
    # 对 Lt/Gt/Rarr 这些**另外的实体**又解）。加了 i 之后交替支能命中 `&Amp;` 这类名字，
    # 而 `_ENT_MIN` 里没有这些键 ⇒ 回调给出 undefined、正文里被塞进字符串 "undefined"（吃内容），
    # 名字 × 形态矩阵当场红。现取读数：S6 ⇒ 1 failed，"实体矩阵分叉 72 条，首条 = 名=amp
    # 形态=Title; in='&Amp;' py='&Amp;' js='undefined'"。这一格的裁定是**不扩表**，
    # 已声明分叉写死在 parity 的 `_ENT_DECLARED_TITLECASE` 与 `_ENT_OFF_TABLE_NAMES` 里。
    ("R70 实体正则改成大小写不敏感（逐名口径被当成全局规则）", COPIES[1],
     u'rdquo);?", "gu");',
     u'rdquo);?", "giu");',
     u"test_entity_name_matrix_matches_python_per_form"),

    # ── 30 症状③那一格（摘要与内嵌全文同段 ⇒ 整对隐掉）此前**没进清单也没靶**：
    # `TESTS` 里漏了 test_reader_summary_dedupe.py ⇒ 电池结构性打不到这条通道（R81 查出的登记缺口）。
    # R71：把 `原文/翻译`那一块从守卫里放出去。用 `} {` 追加一个裸块接住原有括号 ⇒ **括号守恒**，
    # node 不会用语法错冒充"挡住"（本仓第五种失败模式）。命中判据 = 同步断言 tog == has。
    ("R71 摘要隐了但翻译按钮漏出守卫", COPIES[0],
     u"h+='<div class=\"r2-summary\">'+formattedSummary+'</div>';",
     u"h+='<div class=\"r2-summary\">'+formattedSummary+'</div>'; } {",
     u"test_reader_hides_summary_when_fulltext_repeats_it"),

    # R72：守卫永不开火（等价于把这条修复摘掉，但保持括号与形状）。命中的是**开火形状**那一格
    # （dup 两格该没有 r2-summary）。这一条是"接线被拆"的探测器：R71 只盯得住成对，盯不住整条不干活。
    # R72：拆接线探测器。原先打在 `if(!_dupFt){` 这个字面锚上 ⇒ 红是红在判据第 99 行的
    # "守卫不存在"，而不是行为（复评第 4 轮点名）。改成在**调用处**把 fc 换成空串：
    # 守卫字面量原样在位、判据的锚点断言全过，红只能来自"同段却没隐"这一格行为。
    ("R72 调用处喂空 fc（守卫永不开火）", COPIES[0],
     u"var _dupFt = _summaryDuplicatedByFulltext(a.s, a.fc);",
     u"var _dupFt = _summaryDuplicatedByFulltext(a.s, '');",
     u"test_reader_hides_summary_when_fulltext_repeats_it"),

    # R73：把"隐摘要"与 `fetchFullArticle` 的 `fc.length>100` 门槛解耦（写成永不生效）。
    # 这条测的是**空面板**那一格：fc 短于门槛时根本不入屏，摘要若被隐掉，那块面板就一个字都不剩。
    # 现网今天 93 条同段全是长正文 ⇒ 只有合成边界（原始长度正好 100 / 101 / 37）能盯住它。
    ("R73 隐摘要不再对齐 fc 入屏门槛", COPIES[0],
     u"    if(String(fc).length <= 100) return false;",
     u"    if(String(fc).length <= 0) return false;",
     u"test_reader_dedupe_helper_truth_table"),

    # R74：shipSummary 漏掉 unwrapCdata（= 我自己上一批"只删第二遍解码"时顺手删掉的第四件）。
    # 少了这一步，`<[^>]+>` 会把 `<![CDATA[正文]]>` **整段当一个标签**吃掉 ⇒ 出厂空串。
    # 现网暴露面 0（2,412 条里 `s`/`fc` 含 CDATA 记号 0 条），钉的是"与改动前逐字节同形"那句承诺。
    ("R74 shipSummary 漏掉 unwrapCdata", COPIES[2],
     u"  const t = unwrapCdata(String(x || ''))",
     u"  const t = String(x || '')",
     u"test_summary_exit_shape_equals_the_pre_batch_exit"),

    # R75：只收"逐字相等"、不收"正文开头的前缀"（= 用户裁定 ① 之前那半边的形状）。
    # 线上 7,184 条里相等 97 条、前缀且不等 71 条 ⇒ 少这一半就等于漏掉 42% 的那格症状。
    ("R75 前缀重复不再算重复", COPIES[0],
     u"    return a.length > 24 && f.slice(0, a.length) === a;",
     u"    return a.length > 24 && f === a;",
     u"test_reader_dedupe_helper_truth_table"),

    # B05：RSS 支的全文门槛退回"两个**原始标签**的长度"（复评 B2 的旧判据）。
    # 这一格的左边带 CDATA 包装与正文标签 ⇒ 比清洗后的摘要长 ⇒ js 出 fc、Python 不出，
    # 正是"同一份正文在阅读器里两遍"的上游来源之一。语料第 2/3/5 格各咬一种形状。
    # 注：这一格能落地的前提是他人线 harness 补了 `BODY` require（它以前只注入 COVER，
    # 任何让 RSS 支过清洗链的改动都红在 `ReferenceError: BODY is not defined`）。
    ("B05 RSS 门槛退回原始标签长度", COPIES[2],
     u"    const fullContent = cleanedBody.length > descPlain.length ? cleanedBody : '';",
     u"    const fullContent = contentEncoded.length > desc.length ? cleanedBody : '';",
     u"test_realtime_rss_exit_matches_python_s_and_fc_shape_by_shape"),

    # R77：把出口上限退回写死 200 ⇒ 与构建期 `_truncate` 默认不同数
    ("R77 出口上限退回写死 200（与构建期又分叉）", COPIES[2],
     u"const SUMMARY_CAP = 500;",
     u"const SUMMARY_CAP = 200;",
     u"test_summary_exit_cap_matches_buildtime_default"),

    # R78：裁定 ④ 的可执行面刀写成恒等（非空输入原样吐回）。
    # 行为判据必须红 —— 它跑的是**真切出来的那段刀**，所以这条量的是刀本身。
    # 注：api/article.js 是 CRLF ⇒ 锚点一律取单行，别拿跨行字面量去绑。
    ("R78 可执行面刀变恒等", COPIES[3],
     u"  if (!html) return '';",
     u"  if (html) return String(html);",
     u"test_article_exit_strips_executable_surface"),

    # R79：刀还在、但出口那一行不接（= "定义了不调用"那一族，本仓点过两次名）。
    # 这一条只能由接线判据咬住：行为判据切片自跑，看不见调用点。
    ("R79 现抓出口不接可执行面刀", COPIES[3],
     u"const content = stripRemoteExecutables(BODY.capBody(BODY.delinkNavLinks(BODY.normalizeBodyHtml(result.content, url))));",
     u"const content = BODY.capBody(BODY.delinkNavLinks(BODY.normalizeBodyHtml(result.content, url)));",
     u"test_article_exit_wires_the_scrub_at_the_single_choke_point"),]


def _stage(tmp, rel_list):
    for rel in rel_list:
        dst = os.path.join(tmp, rel)
        d = os.path.dirname(dst)
        if d:
            os.makedirs(d, exist_ok=True)
        shutil.copyfile(os.path.join(ROOT, rel), dst)


def _index_criteria():
    """判据名 → 所在测试文件。写错判据名必须当场发现，不许静默当成"没红"。"""
    idx = {}
    for rel in TESTS:
        p = os.path.join(ROOT, rel)
        with io.open(p, encoding="utf-8") as fh:
            t = fh.read()
        for line in t.split("\n"):
            if line.startswith("def test_"):
                name = line[4:].split("(")[0]
                idx.setdefault(name, []).append(rel)
    return idx


def _run(tmp, files, name=None):
    cmd = [sys.executable, "-m", "pytest"] + files + ["-q", "-p", "no:cacheprovider"]
    if name:
        cmd += ["-k", name]
    # 必须禁写字节码：上一轮的 pyc 与本轮 copyfile 写回的源文件落在同一 mtime 里，
    # 子进程会直接复用旧 pyc ⇒ 变异没生效、判据"照样绿"（mut_attention.py 的实测教训）。
    r = subprocess.run(cmd, cwd=tmp, capture_output=True, text=True,
                       encoding="utf-8", errors="replace",
                       env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1",
                            "PYTHONIOENCODING": "utf-8"})
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def _read(p):
    with io.open(p, encoding="utf-8") as fh:
        return fh.read()


def main():
    t0 = time.time()
    criteria = _index_criteria()
    unknown = sorted({m[4] for m in MUTATIONS if m[4] not in criteria})
    if unknown:
        print("电池自己写错了：这些判据不存在 ⇒ %s" % unknown)
        return 1
    dup = sorted(n for n, w in criteria.items() if len(w) > 1 and any(m[4] == n for m in MUTATIONS))
    if dup:
        print("判据名在本电池覆盖的目录里不唯一 ⇒ %s" % dup)
        return 1

    bad = []
    with tempfile.TemporaryDirectory(dir=os.path.join(ROOT, ".deploy-tmp")) as tmp:
        _stage(tmp, COPIES + ONCE + TESTS)
        code, out = _run(tmp, TESTS)
        if code != 0:
            print("基线未绿 ⇒ 副本没搭对，后面全不算数：\n%s" % out[-2500:])
            return 1
        print("基线：副本内 %s（判据 %d 个文件 / 变异 %d 条）"
              % (out.strip().splitlines()[-1], len(TESTS), len(MUTATIONS)))

        for mid, rel, old, new, must_fail in MUTATIONS:
            _stage(tmp, COPIES)                  # 每轮把**全部**可变异副本重打干净
            # ↑ 不是保险起见，是必需：只重打 `rel` 那一个文件时，上一轮留在**另一个**文件里的
            #   变异会跟着这一轮一起跑（实测 R39「Python 值段退回 `\S`」把 R41「JS 值段改宽」
            #   盖成"两侧一致 ⇒ 判据绿"的假象）。两条各打各的才叫变异绝不互相掩盖。
            p = os.path.join(tmp, rel)
            with io.open(p, encoding="utf-8") as fh:
                s = fh.read()
            if s.count(old) != 1:
                bad.append("%s：靶子在副本里出现 %d 次（锚点失效，变异根本没打上）"
                           % (mid, s.count(old)))
                continue
            with io.open(p, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(s.replace(old, new))
            # 电池**自己**的门禁（派工点名的"整场重打副本"靶）：本轮只有 `rel` 允许与真源不同，
            # 其余可变异副本必须逐字干净。把上面那句 `_stage(tmp, COPIES)` 收窄成
            # `_stage(tmp, [rel])`，这里当场报"上一轮变异残留"⇒ 这条门禁证明"重打"这件事
            # 真的在起作用（R39/R41 互相掩盖那一格就是靠它兜住的）。
            for other in COPIES:
                if other == rel:
                    continue
                if _read(os.path.join(tmp, other)) != _read(os.path.join(ROOT, other)):
                    bad.append("电池自检：%s 这一轮里 %s 还带着上一轮的变异残留"
                               "（整场重打副本被改动过）" % (mid, other))
            code, out = _run(tmp, criteria[must_fail], must_fail)
            low = out.lower()
            if code == 0:
                bad.append("%s：打了坏改动、%s 照样绿 ⇒ 空判据\n%s"
                           % (mid, must_fail, out.strip().splitlines()[-1] if out.strip() else ""))
                continue
            if "failed" not in low and "error" not in low:
                # 非零退出但报告里没有失败行 ⇒ 判据压根没跑起来（名字写错、收集期崩、-k 全
                #  deselect）。这**不算**抓到，否则"登记了但从不跑"的假靶又活了。
                bad.append("%s：pytest 非零退出但报告里没有 failed/error（判据没跑起来，"
                           "不算抓到）\n%s" % (mid, out[-800:]))
                continue
            print("  %-40s 挡住 → %s" % (mid, must_fail))
    if bad:
        print("\n%d 个变异没被对应判据挡住：" % len(bad))
        for b in bad:
            print("  - " + b)
        print("总耗时 %.1f s" % (time.time() - t0))
        return 1
    print("变异 %d/%d 全部被抓到（baseline 绿），总耗时 %.1f s"
          % (len(MUTATIONS), len(MUTATIONS), time.time() - t0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
