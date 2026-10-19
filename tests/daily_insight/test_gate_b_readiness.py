# -*- coding: utf-8 -*-
"""门禁 B 的就绪守卫（advisory 侧，接线判据）。

实测背景（2026-10-02，09/10/11 三场日志）：`Quality gate B` 每场都以 exit code 1 收场，
被 `continue-on-error: true` 遮成 success。失败点是 `test_daily_insight.py:106`
`assert len(chunks) > 0`，而这道闸跑在 `Fetch stars & build` **之前**：
那个 job 里既没有 rss 历史也没有热榜快照（都是 .gitignore 排除的跨场态）⇒ 0 chunks 是必然。
"改动前就红"这条我也核了：07:00 那场（本轮推送之前）同样红。所以它不是回归，是**恒红**——
恒红的闸等于没有闸：真回归混在里面看不见，而绿/红的读数还会被人引用。

处置（用户批准的方案 a）：数据未就绪时**响亮地跳过并退出 0**，而不是硬断言失败。
为什么不是"用合成样本继续跑"：合成条目要穿过 `_filter_noise`/`_chunk_documents` 的真实阈值，
一旦样本被判空，报出来的红就又变成噪声；而今天的实际覆盖是"106 行之后一节都没跑"，
所以早退出**不减少任何现有覆盖**，只是把"必然红"换成"必然播报"。

这里只钉接线（不真跑那个脚本：它会 import build_daily_insight 并可能打真实 API，
本地裸跑会挂住烧配额，见 memory: gate-b-no-local-run）：
守卫必须在 chunk 断言**之前**、必须 exit 0、必须打印可 grep 的标记，且原来的断言一条不许少。
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(ROOT, "test_daily_insight.py")


def _src():
    with open(SCRIPT, encoding="utf-8") as fh:
        return fh.read()


def test_guard_exists_before_the_chunk_assert():
    """守卫必须在 `assert len(chunks) > 0` 之前，否则永远轮不到它说话。"""
    src = _src()
    m = re.search(r"assert len\(chunks\) > 0", src)
    assert m, "chunk 断言本体不见了 —— 不许用删断言的办法让门禁变绿"
    guard = re.search(r"if not \(rss_items or hot_items\):[\s\S]{0,400}?sys\.exit\(0\)", src)
    assert guard, "没有'数据未就绪 ⇒ 播报并 exit 0'的守卫"
    assert guard.start() < m.start(), "守卫写在 chunk 断言之后，等于没写"


def test_guard_broadcasts_a_greppable_marker():
    """跳过必须留话：CI 日志里要能一把 grep 到，否则"跳过了"和"根本没跑"分不开。

    注意这个文件里本来就有别的 `[SKIP] print`（FAISS 不可用那类运行时降级），
    所以不能只找第一个 `[SKIP]`——要找**带原因和去向**的那一条。
    """
    src = _src()
    hits = re.findall(r"print\(\"([^\"]*)\"\)", src)
    mine = [h for h in hits if h.startswith("[SKIP]") and "构建前" in h]
    assert mine, "没有以 [SKIP] 开头、且说明'构建前'的播报 print"
    body = mine[0]
    assert "覆盖" in body or "接管" in body, \
        "[SKIP] 文案没说谁在构建后接管，读日志的人会以为这一节被砍掉了：%s" % body[:140]


def test_guard_exits_zero_and_keeps_the_real_assert():
    src = _src()
    assert re.search(r"if not \(rss_items or hot_items\):", src), "就绪判据不是'两族都空'"
    # 真断言必须还在，且带"有数据却切不出 chunk"的语义（这才是它该挡的东西）
    assert re.search(r"assert len\(chunks\) > 0, \"有数据却切不出", src), \
        "原断言被改弱或被删：恒绿比恒红更糟"
    before = src.count("assert ")
    # 2026-10-02 实测本文件 113 条断言。留 3 条余量给别人的正常新增，但不许往下掉：
    # 恒绿最省事的来路就是删断言。
    assert before >= 110, "本文件断言总数掉到 %d，疑似被删断言换绿" % before


def test_exit_zero_not_silent_failure():
    """exit 0 而不是 raise SystemExit 的其它形态：门禁 step 用 bash -e，非零即红。"""
    src = _src()
    guard_body = re.search(r"if not \(rss_items or hot_items\):([\s\S]{0,600}?)\n# ", src)
    assert guard_body, "取不到守卫段正文"
    assert "sys.exit(0)" in guard_body.group(1), "守卫没退出 0"
    assert "sys.exit(1)" not in guard_body.group(1), "守卫退 1 ⇒ 又把恒红请回来了"
