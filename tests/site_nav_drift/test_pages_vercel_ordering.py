# -*- coding: utf-8 -*-
"""A3（advisory）：Pages 三步的位置与守卫条件——把一次被挡回来的提案钉成知识。

2026-10-02 我提过一版"把 `Upload Pages artifact` / `Deploy to GitHub Pages` 挪到
`Deploy to Vercel` 之前"，动机是现取的：13:00 那场构建 13:22 就完成了，而 Vercel 步
（5 次重试 + `npx vercel` 上传）从 13:22 一直卡到 13:49 之后，Pages 两步全程 pending
⇒ 站点内容早生成好，却要等一个与 Pages 无关的第三方部署重试完才上线。

**这个提案被既有 A2 判据挡回来了**（在副本上彩排时当场报红，不是靠推理否掉的）：
`tests/rss_history/test_pages_deploy_wiring.py::test_pages_steps_present_and_cannot_block_vercel`
钉的是"Pages 必须排在 Vercel 之后"，理由是 Pages 两步没有 continue-on-error，
挪到前面的话**它们任何一次失败就会终止 job，把 Vercel 部署整个跳过**。
所以"改顺序"不是免费午餐：它把"Pages 被 Vercel 拖慢"换成"Vercel 被 Pages 挡掉"。

要治那条延迟，可行的方向是**给 Vercel 步本身封顶**（`timeout-minutes`，再让末步把 Vercel 的
未成功也标红），**不是**加 `continue-on-error` —— 本文件自己的 `test_vercel_step_is_not_silenced`
就禁止静默，而静默恰恰是本项目最怕的形态（写这段时我先把"封顶 + continue-on-error"当成可行方向，
是读了那条判据才发现二者互斥；改形后的方案见下面两条新判据）。
本文件因此钉的是**现状不变量**（顺序 + 两道 if 守卫 + Vercel 不许被静默），
这样将来谁要动这两步，会先看到这里的理由，而不是把不变量悄悄改掉。
"""
import ast
import os
import re

import pytest

yaml = pytest.importorskip("yaml")

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WF = os.environ.get("STARHUB_UPDATE_YML") or os.path.join(ROOT, ".github", "workflows", "update.yml")

UPLOAD = "Upload Pages artifact"
DEPLOY = "Deploy to GitHub Pages"
VERCEL = "Deploy to Vercel"
STAGE = "Stage Pages site"


def _steps():
    with open(WF, encoding="utf-8") as fh:
        doc = yaml.safe_load(fh)
    return doc["jobs"]["update"]["steps"]


def _idx(steps, want):
    hits = [i for i, s in enumerate(steps) if (s.get("name") or "").startswith(want)]
    assert len(hits) == 1, "步骤 %r 出现 %d 次（应为 1）⇒ 本文件的定序判据不作数" % (want, len(hits))
    return hits[0]


def test_pages_pair_sits_after_vercel_on_purpose():
    """现状：Stage → Save state cache → Prune → Vercel → Log → Upload → Deploy → 标红步。

    Stage 排在 Save 之前是 2026-10-02 搬的：缺件落痕（`pages_missing` 写进 build_logs）必须
    赶在缓存保存之前，否则同场 prune 一删就查不到（判据：test_pages_missing_lifecycle.py）。
    Vercel 与 Pages 两步的相对顺序是**有意**的，理由见文件头。
    """
    steps = _steps()
    si, ui, di, vi = (_idx(steps, w) for w in (STAGE, UPLOAD, DEPLOY, VERCEL))
    assert si < ui < di, "Pages 两步必须在 Stage 之后、且 Upload 早于 Deploy（否则发的是旧制品）"
    assert vi < ui, ("Vercel 步跑到了 Pages 之后 —— 这是另一条 A2 判据禁止的形状吗？"
                     "先读 tests/rss_history/test_pages_deploy_wiring.py::"
                     "test_pages_steps_present_and_cannot_block_vercel 再动")


def test_pages_steps_keep_their_guard_conditions():
    """两道守卫不许在改顺序时被顺手改掉：残缺制品不许发布。"""
    steps = _steps()
    up = steps[_idx(steps, UPLOAD)]
    dep = steps[_idx(steps, DEPLOY)]
    assert "steps.stage.outcome" in (up.get("if") or ""), \
        "Upload 不再要求 stage 成功 ⇒ 残缺 _pages 会被当有效制品发布：%r" % (up.get("if") or "")
    cond = dep.get("if") or ""
    assert "always()" in cond and "steps.pages_upload.outcome" in cond, \
        "Deploy 的 if 被改动：%r" % cond
    assert (up.get("with") or {}).get("if-no-files-found") == "error", \
        "空制品的 warn 默认值会把站点抹平"


def test_vercel_step_is_not_silenced():
    """Vercel 失败必须让整场可见（它没有 continue-on-error）。

    这条同时是"改顺序方案"的前提检查：哪天有人给 Vercel 加上 continue-on-error，
    文件头那段权衡就失效了，本文件与 A2 那条判据都要跟着重新讨论。
    """
    steps = _steps()
    ver = steps[_idx(steps, VERCEL)]
    assert ver.get("continue-on-error") is not True, \
        "Vercel 被改成 continue-on-error ⇒ 失败会被遮成 success，Pages 与 Vercel 的耦合判断要重做"
    assert "5" in (ver.get("run") or ""), "Vercel 的 5 次重试循环不见了？确认一下再改本文件的结论"


RED_FLAG = "Mark the run red"


def test_vercel_step_has_a_timeout_cap_and_id():
    """Vercel 步必须自己封顶，并有 id 让后面那步引用它的结果。

    现取的依据（2026-10-02）：正常场这一步只用 **0.5–0.7 分钟**，而 1578 跑了 38.8 分钟被并发掐掉、
    1585 跑了约 42 分钟同样被掐、1586 也被掐 ⇒ 从 18:00 场（1584）之后连续三场 Vercel 部署没落地。
    没有封顶时，这一步的挂起会把整场撑到被并发取消，而门禁与告警都不会说"Vercel 没部署成功"。
    上限取 12 分钟：是正常用时的 ~20 倍，给足 5 次退避重试（15+30+45+60=150s 的 sleep 加四次上传），
    又远小于整场被掐的时间。下限 3 分钟是防手滑写成 1 分钟把正常场全打死。
    """
    ver = _steps()[_idx(_steps(), VERCEL)]
    cap = ver.get("timeout-minutes")
    assert isinstance(cap, int) and 3 <= cap <= 12, (
        "Vercel 步没有合理的 timeout-minutes（实得 %r）⇒ 挂起时整场只会被并发掐掉，"
        "而 Pages 之后的标红步也拿不到它的结果" % cap)
    assert ver.get("id"), "Vercel 步没有 id ⇒ 末步无法用 steps.<id>.outcome 引用它，标红判不了"


def test_unsuccessful_vercel_is_marked_red_at_the_end():
    """Vercel 未成功（failure / cancelled）必须让整场标红，且不许靠 continue-on-error 静默。

    与上一条是一对：封顶负责"别拖垮整场"，这条负责"别悄悄算过"。
    现状是末步只看 Stage 的 outcome（`steps.stage.outcome == 'failure'`），
    所以 Vercel 被掐时整场显示 cancelled 却没有任何一步说明"是 Vercel 没部署成"。
    """
    steps = _steps()
    ver = steps[_idx(steps, VERCEL)]
    hits = [i for i, s in enumerate(steps) if (s.get("name") or "").startswith(RED_FLAG)]
    assert len(hits) == 1, "标红步骤应只有 1 个，实得 %d ⇒ 引用关系判不出" % len(hits)
    mark = steps[hits[0]]
    cond = (mark.get("if") or "")
    assert "steps.%s" % (ver.get("id") or "?") in cond, (
        "末步的 if 没引用 Vercel 的 outcome（实得 %r）⇒ Vercel 未成功时整场仍是绿的" % cond)
    assert "failure" in cond or "!= 'success'" in cond, (
        "末步只判断一种失败形态不够：超时被掐时 outcome 是 cancelled，实得 %r" % cond)
    assert "exit 1" in (mark.get("run") or ""), "标红步没真的 exit 1，写了 if 也不会红"
    assert ver.get("continue-on-error") is not True, (
        "Vercel 又被加了 continue-on-error ⇒ 与 test_vercel_step_is_not_silenced 冲突，二选一")


# ── 2026-10-03 新增：整场被取消那种"Pages 没发"也得出声 ──────────────────────────
# 实证（现取 run 1590，2026-10-03 00:06）：GitHub 把一个迟到的 `schedule` 事件补发，
# 撞上正在跑的 dispatch 场，concurrency.cancel-in-progress 把它掐掉 ⇒
# 第 18 步 cancelled，21–34 步（含 Commit / Stage / Upload Pages / Deploy Pages）**全 skipped**，
# 站点整整一小时没更新，而末步的 if 只认 `stage.outcome == 'failure'`，
# 于是它自己也是 skipped —— 没有一行 ::error:: 说"本场未发布"。
# 只看 run 结论是 cancelled 也不算"出声"：没人会把每小时的颜色当告警读。
_IF_TEST = re.compile(r"steps\.(\w+)\.outcome\s*(==|!=)\s*'(\w+)'")
_BODY_TEST = re.compile(r"\[\s*\"\$\{\{\s*steps\.(\w+)\.outcome\s*\}\}\"\s*(=|!=)\s*\"(\w+)\"\s*\]")


def _eval(cond, outcomes):
    """按 outcome 组合求真值：先把每个 `steps.X.outcome == 'v'` 换成布尔，
    再交给**只认布尔代数的小求值器**（不用 eval —— 一行 yml 不该拿到执行任意表达式的权限）。

    这条判据要的是"组合实跑"，不是 grep 文本：grep 看得见 skipped 这个词，
    看不见它是不是绑在 stage 上、也看不见正常那场会不会被误红。
    """
    expr = _IF_TEST.sub(lambda m: str(_one(m.group(1), m.group(2), m.group(3), outcomes)), cond)
    expr = expr.replace("always()", "True").replace("&&", " and ").replace("||", " or ")
    expr = re.sub(r"!(?!=)", " not ", expr)
    return _bool_tree(ast.parse(expr, mode="eval").body)


def _bool_tree(node):
    if isinstance(node, ast.Constant):
        if not isinstance(node.value, bool):
            raise AssertionError("末步 if 里出现非布尔字面量 %r ⇒ 判据拒绝求值" % (node.value,))
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return not _bool_tree(node.operand)
    if isinstance(node, ast.BoolOp):
        vals = [_bool_tree(v) for v in node.values]
        return all(vals) if isinstance(node.op, ast.And) else any(vals)
    raise AssertionError("末步 if 里出现布尔代数之外的写法（%s），判据不敢替你求值" % type(node).__name__)


def _one(sid, op, val, outcomes):
    got = outcomes.get(sid)
    assert got is not None, "末步 if 引用了 steps.%s，但测试没给这一步的 outcome" % sid
    # shell 的相等写作 `=`、表达式里是 `==` —— 两种都要认。
    # 这里原本只判 `==`，于是所有 `=` 分支都掉进"不等"那一支 ⇒ 判据自己算反，
    # 而且"上次它通过"是撞对的（假绿）。2026-10-03 复现并修，配 _body_reds 的自测。
    return (got == val) if op in ("==", "=") else (got != val)


def test_body_evaluator_is_not_backwards():
    """求值器自身的自测：`=` 与 `!=` 两个方向各打一次，正反都要有。

    末步的判断是按 outcome 组合真算的，那么"算"这一步也必须有判据 ——
    否则它算反时，所有下游断言都会因为"两边都错"而静默通过。
    """
    body = ('if [ "${{ steps.stage.outcome }}" = "skipped" ]; then\n  exit 1\nfi\n'
            'if [ "${{ steps.pages_upload.outcome }}" != "success" ]; then\n  exit 1\nfi\n')
    hit = lambda o: sorted(m.split("=")[0].split("!")[0] for m in _body_reds(body, o))
    assert hit({"stage": "skipped", "pages_upload": "success"}) == ["stage"], "`=` 方向算反了"
    assert hit({"stage": "success", "pages_upload": "success"}) == [], "都成功时不该有任何命中"
    assert hit({"stage": "success", "pages_upload": "cancelled"}) == ["pages_upload"], "`!=` 方向算反了"
    assert hit({"stage": "success", "pages_upload": "skipped"}) == ["pages_upload"], (
        "`!=` 把 skipped 也算命中——求值器必须照实算：真实末步正是靠这条性质才不许对 vercel=skipped 出声"
        "（见 test_skipped_vercel_deploy_does_not_mark_the_run_red），口径由 yml 决定，不由这里决定")


def _body_reds(mark_run, outcomes):
    """run 体里哪几条 shell 判断会命中（命中即 ::error:: + exit 1）。"""
    hits = []
    for m in _BODY_TEST.finditer(mark_run):
        if _one(m.group(1), "=" if m.group(2) == "=" else "!=", m.group(3), outcomes):
            hits.append(m.group(1) + m.group(2) + m.group(3))
    return hits


def test_cancelled_run_also_marks_pages_unpublished():
    """整场被取消时 Stage 是 skipped 而不是 failure —— 那种"Pages 没发"也必须出声。

    覆盖面（说准，别当成"发布链的总闸"）：按 stage / pages_upload / vercel 三个 outcome 组合真算。
    已覆盖：Stage 缺件(failure)、整场被取消或前置判红(stage skipped)、
    Stage 成了但上传没跑(pages_upload skipped，17:00 那场静默失败就是它)、Vercel failure/cancelled。
    **仍未覆盖**：`Upload Pages artifact` 成功、但 `Deploy to GitHub Pages` 自己失败 ——
    那一步没有 id，引用不到（`tools/prod_verify.py` 从站点侧能看到"页面没更新"，是另一条路径）。
    """
    steps = _steps()
    mark = steps[_idx(steps, RED_FLAG)]
    cond = (mark.get("if") or "")
    body = mark.get("run") or ""
    ok = {"stage": "success", "vercel": "success", "pages_upload": "success"}
    # 反向那半与正向同权重：正常发布的那场绝不许红
    assert not _eval(cond, ok), "正常成功的一场末步就红了 ⇒ 条件写成了无条件，末步在制造假失败"
    # 1590 的真实形状：整场被取消，Stage 没跑成
    cancelled = {"stage": "skipped", "vercel": "skipped", "pages_upload": "skipped"}
    assert _eval(cond, cancelled), (
        "Stage 被跳过（整场取消、或前置 blocking 判红）时末步不执行 ⇒ Pages 未发布继续静默；"
        "实得条件 %r" % cond)
    assert _eval(cond, {"stage": "failure", "vercel": "success", "pages_upload": "success"}), (
        "缺件那半被改丢了：Stage 判 failure 必须仍然红")
    assert _eval(cond, {"stage": "success", "vercel": "cancelled", "pages_upload": "success"}), (
        "封顶那半被改丢了：Vercel 被掐（cancelled）必须仍然红")
    # 出声的**内容**也要对：被取消那场得说"Pages 未发布"，不能只报 Vercel
    hits = _body_reds(body, cancelled)
    assert any(h.startswith("stage") for h in hits), (
        "末步跑了但没有 Pages 那半的 ::error::（实得命中 %s）⇒ 读日志的人只知道 Vercel 没成，"
        "不知道站点这一小时没更新" % hits)

    # 第三种"没发布"的形状：Stage 成了，但 Upload Pages artifact 没执行
    # （17:00 那场静默失败正是靠 upload/deploy 的 skipped 揪出来的 —— Stage 成功 ≠ 制品上线）
    half = {"stage": "success", "vercel": "success", "pages_upload": "skipped"}
    assert _eval(cond, half), (
        "Stage 完成但上传被跳过时末步不红 ⇒ 半拉子发布继续是绿的（实得条件 %r）" % cond)
    assert any(h.startswith("pages_upload") for h in _body_reds(body, half)), (
        "这种形状只说 Vercel/缺件，读日志的人不知道是上传没跑")

    # 反向那半：Vercel 被**跳过**不该算红（封顶只在 failure/cancelled 时出声），
    # 否则这条改动会把"上游判红所以 Vercel 没跑"重复报成 Vercel 的失败
    assert not _eval(cond, {"stage": "success", "vercel": "skipped", "pages_upload": "success"}), (
        "Vercel 的 skipped 被当成未部署 ⇒ 条件被写成了 != 'success'，会在别的失败上重复报红")


def test_vercel_step_does_not_wait_for_the_server_build():
    """CI 不许站在原地等 Vercel 构建完成 —— 那是"连续 6 场部署失败"的直接原因。

    现取读数（2026-10-03 00:22，最近 8 场的步级 conclusion）：只有 18:00 那场 Vercel=success，
    1585–1589 全是 `cancelled`、1590 `skipped`；两域指纹也证实
    （Pages 已发到 07:08=1589，Vercel 停在 02:07=1584 ⇒ API 主机在生产上已停更 6 小时）。
    机制：`vercel --prod` 会**等部署构建完成**才退出，而构建要 40 分钟（实测上传只用 1.5 秒），
    workflow 每小时一次且 `cancel-in-progress: true` ⇒ 每一场都在等待途中被下一场掐掉，
    于是"永远等不到终点"。官方口径就是那条 `--no-wait`：
    "does not wait for a deployment to finish before exiting from the deploy command"。

    代价与配套（必须一起读，否则这条改动会把失败遮起来）：加了 --no-wait 之后 CI **当场看不出部署成没成**，
    落地与否改由生产侧判 —— `tools/prod_verify.py` 取 Vercel 域首页的每场必变指纹 + 各场步级结论，
    把"Vercel 连续 N 场未落地"报成 warn/blocking，并且它就是推送闸（判据
    tests/tools/test_prod_verify_verdict.py::test_vercel_landing_stall_is_warn_with_evidence）。
    也就是说：这条改动与那条门槛是**一对**，只做前半截会让 Vercel 失败重新变静默。
    """
    ver = _steps()[_idx(_steps(), VERCEL)]
    run = ver.get("run") or ""
    assert "--no-wait" in run, (
        "Vercel 步仍在等部署构建完成 ⇒ 它一定会被下一场的 cancel-in-progress 掐掉，"
        "生产上的表现就是 API 主机长期停在旧版而 CI 每场看着都'快成功了'")
    assert "--prod" in run, "--no-wait 不该顺手把生产发布也去掉"


# ── 只在 API 相关输入变化时才部署（2026-10-03 02:00 起 Vercel 开始秒拒重复部署）──────────
API_INPUTS = ("api/", "lib/", "vercel.json", "rss_sources.json")


def _step_by_name(steps, want):
    return [s for s in steps if (s.get("name") or "").startswith(want)]


def test_vercel_deploy_is_gated_on_api_relevant_changes():
    """Vercel 步必须带条件：只有 API 相关输入变了才部署。

    读数（现取 1592/1593）：1592 用 34 秒部署成功，1593 五次尝试全被
    `Error: Your deployment failed. Please retry later.` 秒拒 —— 而这两场**都没碰过
    api/ 或 lib/ 或 vercel.json**（逐提交 files[] 查过）⇒ 我们在用同一份代码每小时重复请求部署，
    撞上 Vercel 的并发/配额就变成每场一红。条件部署同时解决"噪声红"和"配额浪费"。
    """
    steps = _steps()
    ver = _step_by_name(steps, VERCEL)[0]
    cond = ver.get("if") or ""
    assert "cache-hit" in cond, (
        "Deploy to Vercel 没有 `cache-hit` 类前置条件（实得 %r）⇒ 每场都重复部署同一份 API 代码" % cond)
    marker = _step_by_name(steps, "Detect API-relevant deploy state")
    assert marker, "缺了提供 cache-hit 的缓存步 ⇒ 上面的 if 永远为假，等于永久不部署（更糟）"
    key = (marker[0].get("with") or {}).get("key") or ""
    for token in API_INPUTS:
        assert token in key, (
            "API 输入清单漏了 %s（实得 key=%r）⇒ 改了它也不部署，API 主机静默停在旧版 —— "
            "这比重复部署危险得多" % (token, key))


def test_deploy_marker_dir_stays_out_of_the_package():
    """.vercelignore 必须排掉 marker 目录，否则它会被打进部署包（多一个公开文件）。"""
    body = open(os.path.join(ROOT, ".vercelignore"), encoding="utf-8").read()
    assert ".vercel-deployed/" in body, (
        ".vercelignore 没排 .vercel-deployed/ ⇒ 记录「上次部署了什么」的标记会被上传到生产")


# ── 门本身必须是"试过就留痕"，否则失败场每小时重撞（2026-10-03 现取：连红 6 场）──────────
def test_deploy_gate_survives_a_failed_deploy():
    """留痕必须在**部署失败的那场**也写进缓存，否则条件门等于没有门。

    现取读数（run 1595–1600，head 全部是加了门的 a4596261e5）：`Deploy to Vercel` 连 6 场 failure，
    每场 5 次尝试都被 `Error: Your deployment failed. Please retry later.` 秒拒；
    而 `Post Detect API-relevant deploy state` 每场 **skipped** ⇒ 缓存从未落过一个键 ⇒
    下一场 restore 必然 miss ⇒ 再撞 5 次。第一版门把"部署成功后才留痕"当成了前提，
    于是它在唯一需要它的场景（持续失败）里退化成"每场重复部署"——比没有门更糟，
    因为它同时让整场每场一红。
    三条形状要求（都是这条判据的内容）：
    ① 提供 cache-hit 的步用 `cache/restore@v4`（一体 `cache@v4` 的 post-save 在失败场不跑）；
    ② 另有一个**显式** `cache/save@v4` 步、key 与 ① 完全一致、带 `always()`、排在 Vercel 之后
       —— 显式步跑在 job 结论之前，不受"post 被跳过"影响；
    ③ run 体里写 marker 的那两行必须早于任何 `exit 1`：先留痕，再决定要不要红。
    """
    steps = _steps()
    gate = _step_by_name(steps, "Detect API-relevant deploy state")
    assert gate, "门步不见了"
    uses = gate[0].get("uses") or ""
    assert "cache/restore@v4" in uses, (
        "门步用的是 %r（post-save 在失败场被跳过）⇒ 失败场不落键，下一场照样重撞 5 次" % uses)
    key = (gate[0].get("with") or {}).get("key") or ""

    ver_i = _idx(steps, VERCEL)
    savers = [(i, s) for i, s in enumerate(steps)
              if "cache/save@v4" in (s.get("uses") or "")
              and ".vercel-deployed" in str((s.get("with") or {}).get("path") or "")]
    assert savers, (
        "没有显式保存 marker 目录的 cache/save 步 ⇒ 门只有读、没有写，等于永久 miss")
    i, save = savers[0]
    assert i > ver_i, (
        "显式保存步排在 Vercel 之前 ⇒ 它存的是本场开始前的旧内容，永远记不住刚试过的那次")
    assert ((save.get("with") or {}).get("key") or "") == key, (
        "save 的 key（%r）与 restore 的 key（%r）不一致 ⇒ 存的键和读的键不是同一个，门永不命中"
        % ((save.get("with") or {}).get("key"), key))
    assert "always()" in (save.get("if") or ""), (
        "显式保存步没带 always() ⇒ Vercel 失败让 job 变红后这一步被跳过，正好在最需要留痕的那场没留痕")

    run = steps[ver_i].get("run") or ""
    # 只数会执行的 shell 行：第一版用 run.find(...)，被注释里那句"排在 exit 1 之前"骗了 ⇒ 判据假红。
    code = [(n, s) for n, s in enumerate(run.splitlines())
            if s.strip() and not s.strip().startswith("#")]
    at_mark = next((n for n, s in code if "mkdir -p .vercel-deployed" in s), None)
    at_exit = next((n for n, s in code if "exit 1" in s), None)
    assert at_mark is not None, "Vercel 步不再写 marker ⇒ 门没有可保存的内容"
    assert at_exit is None or at_mark < at_exit, (
        "写 marker 排在 exit 1 之后 ⇒ 失败路径直接退出、不留痕，下一场重撞（现得 mark=%s exit=%s）"
        % (at_mark, at_exit))


def test_skipped_vercel_deploy_does_not_mark_the_run_red():
    """条件门生效后「跳过部署」是正常状态，末步的 run 体不许把它报成 Vercel 未部署。

    外层 `if:` 早已精确到 failure/cancelled（见上一条判据的反向那半），但 run 体当时写的是
    `!= "success"`——那在"每场都部署"的年代等价，在门生效后不等价：命中缓存的那场
    `steps.vercel.outcome` 是 skipped ⇒ body 走到那半就 `exit 1` ⇒ **门每生效一场就红一场**，
    把"消除噪声红"的改动本身变成新的恒红源。两处口径必须一致，这是同一条判据的两半。
    """
    steps = _steps()
    mark = steps[_idx(steps, RED_FLAG)]
    body = mark.get("run") or ""
    ok = {"stage": "success", "vercel": "success", "pages_upload": "success"}

    hit = _body_reds(body, {"stage": "success", "vercel": "skipped", "pages_upload": "success"})
    assert not hit, (
        "Vercel 因内容未变被跳过时，末步 run 体仍出声（命中 %s）⇒ 条件门一生效就每场一红" % hit)
    for shape in ("failure", "cancelled"):
        hits = _body_reds(body, {"stage": "success", "vercel": shape, "pages_upload": "success"})
        assert any(h.startswith("vercel") for h in hits), (
            "Vercel=%s 时 run 体没有 vercel 那半（实得 %s）⇒ 未部署重新变静默" % (shape, hits))
    assert not _body_reds(body, ok), "一切正常的那场不许有 shell 命中"


# ── Vercel 的授权判定看的是"部署时工作树 HEAD 的作者"（2026-10-03 定性，见手册 §8.21）──────
BOT_EMAIL = "41898282+github-actions[bot]@users.noreply.github.com"
BOT_NAME = "github-actions[bot]"
# 按步名定位，**不要**拿 `--allow-empty` 当锚点：Commit 步的 push 重试分支里本来就有它
# （update.yml:379），用它当锚点会把那条既有分支误认成这个守卫（判据第一版就是这么错的）。
BOT_HEAD_GUARD = "Ensure Vercel sees a bot-authored commit"


def _guard_step(steps):
    hits = [s for s in steps if (s.get("name") or "").strip().startswith(BOT_HEAD_GUARD)]
    assert len(hits) <= 1, "守卫步出现 %d 次 ⇒ 定位不作数" % len(hits)
    return hits[0] if hits else None


def test_a_bot_authored_commit_precedes_the_vercel_deploy():
    """真要部署之前，工作树 HEAD 必须是 **bot 的提交**——否则 Vercel 判 Blocked。

    现取因果（三场日志硬对照，同一天）：
      run 1592 Commit 步有 `chore: auto update stars` + `-> main` ⇒ `Deploy to Vercel = success`；
      run 1602 / 1607 只有 `No changes, skip commit.` ⇒ `Deploy to Vercel = failure`，
      面板原文：`Kwei168@users.noreply.github.com attempted to deploy a commit to
      guiyingyi2021's projects on Vercel through the Vercel CLI, but they're not a member of the team.`
    机制：`npx vercel --prod` 把**当前工作树 HEAD 的 commit 作者**当作部署发起者。历史上每场构建
    都会由 `Commit & push if changed` 以 bot 身份提交一次（那时状态文件、页面 HTML 还在 add 清单里），
    所以部署永远挂 bot ⇒ 通过；而为了压平 git 膨胀把这些文件退出清单后，多数场次"无变化可提交"
    ⇒ HEAD 停在我推的 commit 上 ⇒ 被拒。**Vercel 账号与 GitHub 账号不同名从项目第一天就如此**，
    以前能成靠的就是这个 bot 提交，不是账号对得上。

    位置与条件都要钉住：
    - 必须在 `Detect API-relevant deploy state` 之后、`Deploy to Vercel` 之前（前面拿不到 cache-hit，
      后面补了也来不及）；
    - `if` 必须带 cache-hit 条件 ⇒ **只在真要部署时补**。写成无条件就等于把"每场一个提交"加回来，
      §8.16/8.17 压平的曲线会原地复活（那是这批改动唯一的目的）。
    """
    steps = _steps()
    det = _idx(steps, "Detect API-relevant deploy state")
    ver = _idx(steps, VERCEL)
    i = next((n for n, s in enumerate(steps)
              if (s.get("name") or "").strip().startswith(BOT_HEAD_GUARD)), None)
    assert i is not None, (
        "没有「%s」这一步 ⇒ 只要那一场没有内容变化，Vercel 就会因为 HEAD 作者是 Kwei168"
        "（非 guiyingyi2021 团队成员）而把部署判 Blocked" % BOT_HEAD_GUARD)
    assert det < i < ver, (
        "守卫步位置不对（实得 %d；Detect=%d，Deploy to Vercel=%d）"
        "⇒ 放 Detect 之前拿不到 cache-hit，放 Deploy 之后补了也来不及" % (i, det, ver))
    s = steps[i]
    run = s.get("run") or ""
    assert BOT_EMAIL in run and BOT_NAME in run, (
        "补的提交必须是 bot 身份（缺 %r / %r）——用 Kwei168 的身份补，Vercel 照样拒" % (BOT_NAME, BOT_EMAIL))
    assert "--allow-empty" in run, (
        "守卫步里没有 --allow-empty ⇒ 无变化可提交时补不出东西来")
    assert "git log -1" in run and "%ae" in run, (
        "必须先读 HEAD 的 author email 再决定补不补 ⇒ 否则连「本来就是 bot 提交」的那场也白补一个提交")
    assert "git push" in run, "补了不推上去，Vercel 读到的还是旧 HEAD，等于没修"
    assert "cache-hit" in (s.get("if") or ""), (
        "这一步没挂 cache-hit 条件 ⇒ 每场都补提交，把刚压平的 git 膨胀加回来（实得 if=%r）"
        % (s.get("if"),))


def test_the_bot_commit_step_cannot_block_pages():
    """这一步失败**不许终止 job**——Pages 排在 Vercel 之后，本仓为"自己把部署冻掉"付过两次代价。

    `continue-on-error` 会把失败遮成 success，所以必须同时有 `::warning::` 出声；
    真正的失败可见性仍由 Vercel 那一步与末步负责（它们会红）。
    """
    steps = _steps()
    s = _guard_step(steps)
    assert s is not None, "守卫步不见了 ⇒ 这条防连坐判据不作数"
    assert s.get("continue-on-error") is True, (
        "补提交步没有 continue-on-error ⇒ push 一失败就终止 job，Pages 那场跟着不发"
        "（§8.16 的连坐教训：A2 blocking + Deploy 无 if 曾把整场部署冻掉）")
    assert "::warning" in (s.get("run") or ""), (
        "补提交失败/跳过必须出声；continue-on-error 不加警告 = 把「部署上不去」重新捂回静默")


def _git(cwd, *args):
    import subprocess
    # encoding 必须显式给：Windows 上 text=True 会按 GBK 解码，git 输出里的 UTF-8 中文
    # （比如守卫 shell 里那两行 ::warning::）会直接把测试打崩成 UnicodeDecodeError。
    r = subprocess.run(["git"] + list(args), cwd=cwd, capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    assert r.returncode == 0, "git %s 失败：%s%s" % (" ".join(args), r.stdout, r.stderr)
    return (r.stdout or "").strip()


def test_guard_shell_rewrites_head_to_bot_and_pushes(tmp_path):
    """**真跑**那段 shell：HEAD 是别人提交时，它必须补出一个 bot 作者的空提交并推上去。

    为什么必须有这条：前面两条判据读的是 yml 文本，文本对了不代表 shell 跑得通
    （`git log -1 --pretty=%ae` 拼错、比较条件写反、push 少 refspec 都能让"守卫"形同不存在，
    而 CI 上这一步是 continue-on-error ⇒ 它坏了只会静默）。
    这里在临时目录里建一个**本地裸仓**当远端，全程不碰网络、不碰 GitHub。
    反向那半同样重要：HEAD 本来就是 bot 时**不许再补**，否则每场白涨一个提交。
    """
    import shutil
    import subprocess
    if shutil.which("bash") is None:
        pytest.skip("本机没有 bash：这段 shell 的行为只能到 CI 的 ubuntu 上验")
    body = _step_by_name(_steps(), BOT_HEAD_GUARD)[0]["run"]
    sh = tmp_path / "guard.sh"
    sh.write_text(body, encoding="utf-8")
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    work = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True)
    w = str(work)
    # 分支名必须对齐 CI：actions/checkout 检出出来就是 main 且跟踪 origin/main，
    # 而 clone 一个空裸仓得到的是 init.defaultBranch（本机是 master）⇒ 守卫里那句无参数
    # `git push` 在 simple 模式下会推去 master、被远端拒掉，测试就会报"没推到远端"。
    _git(w, "checkout", "-q", "-b", "main")
    # 身份必须自带：CI 的 ubuntu 上没有全局 user.name/email，缺身份会让 commit 直接失败
    _git(w, "config", "user.name", "Kwei168")
    _git(w, "config", "user.email", "83650072+Kwei168@users.noreply.github.com")
    (work / "a.txt").write_text("hi\n", encoding="utf-8")
    _git(w, "add", "a.txt")
    _git(w, "commit", "-qm", "mine")
    # 首次推送要建上游跟踪：CI 里 actions/checkout 得到的 main 本来就跟踪 origin/main，
    # 守卫脚本用的是无参数 `git push`，没有 upstream 时它会直接报 "no upstream branch"。
    _git(w, "push", "-q", "-u", "origin", "main")
    assert _git(w, "log", "-1", "--pretty=%ae") != BOT_EMAIL, "前置条件不成立：HEAD 已经是 bot"

    r = subprocess.run(["bash", str(sh)], cwd=w, capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    assert r.returncode == 0, "守卫 shell 非零退出：%s%s" % (r.stdout, r.stderr)
    assert _git(w, "log", "-1", "--pretty=%ae") == BOT_EMAIL, (
        "跑完之后 HEAD 作者仍不是 bot ⇒ Vercel 照旧按「非团队成员」拒绝")
    assert _git(w, "ls-remote", "origin", "main").split()[0] == _git(w, "rev-parse", "HEAD"), (
        "补出来的提交没推到远端 ⇒ Vercel 读到的还是旧 HEAD，等于没修")
    assert _git(w, "diff", "HEAD^", "HEAD", "--name-only") == "", (
        "补的不是空提交 ⇒ 会把内容变化混进来，膨胀与责任都说不清")

    before = _git(w, "rev-parse", "HEAD")
    r2 = subprocess.run(["bash", str(sh)], cwd=w, capture_output=True,
                        text=True, encoding="utf-8", errors="replace")
    assert r2.returncode == 0, "第二次跑（HEAD 已是 bot）非零退出：%s%s" % (r2.stdout, r2.stderr)
    assert _git(w, "rev-parse", "HEAD") == before, (
        "本来就是 bot 提交的那场又补了一个 ⇒ 每场白涨一个提交，压平的曲线会回来")
    assert "已是 bot 提交" in (r2.stdout or ""), (
        "不补的分支要留下可读的一行，否则日志里分不清「判过了不补」和「整步没跑」")
