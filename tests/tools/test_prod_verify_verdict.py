# -*- coding: utf-8 -*-
"""推下一批之前必须先跑的生产验证：判据只判"读数→结论"这一层（不发网络请求）。

为什么要有这道门槛（2026-10-03 用户的定性批评，我认）：
Vercel 部署**连续 6 场没落地**（现取步级 conclusion：1584 success，1585–1589 cancelled，1590 skipped），
而我照样一批接一批往下推 —— 等于把"CI 绿"当成"改动生效"。日志和线上读数一直拿得到，
我没有把它们变成**阻塞条件**，所以事故是你发现的、不是我发现的。

门槛的形状（`tools/prod_verify.py:verdict()`）：
  · blocking —— 站点或 API 现在是坏的（四页任一非 200、或 Vercel 函数端点非 200）⇒ 拒绝推送；
  · warn     —— 站点活着但上一场没发布 / Vercel 长期没落地 / 该关的公开面还没关 ⇒ 允许推送，
                但必须把话写在推送日志里（已知未修的问题不该永久卡住进度，那只会逼人绕过门槛）；
  · ok       —— 都没问题。
本文件还钉两条反向控制：**取不到读数绝不等于 ok**（空读数必须判 blocking），
以及"站点全绿 + 公开面仍开着"不许被读成 ok ⇒ 它至少是 warn。
"""
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# 必须走 STARHUB_TOOLS_DIR：这一层写死真实 tools/ 的话，电池改的是副本、判据读的是原件，
# 于是"站点 404 降级成 warn"这种致命变异永远不会红（2026-10-03 由 mut_prod_gate 的 P3/P4 当场抓到）
sys.path.insert(0, os.environ.get("STARHUB_TOOLS_DIR") or os.path.join(ROOT, "tools"))
import prod_verify as V  # noqa: E402


def _readings(pages=None, api=None, last_runs=None, public=(), fingerprint_gap=None):
    return {
        "pages": pages if pages is not None else {"index.html": 200, "ai-daily.html": 200,
                                                  "rss-aggregator.html": 200,
                                                  "daily-insight-history.html": 200},
        "api": api if api is not None else {"api/rss": 200, "api/news": 200},
        "last_runs": last_runs if last_runs is not None else [
            {"num": 1591, "pages": "success", "vercel": "success"}],
        "public_internal": list(public),
        "fingerprint_gap_hours": fingerprint_gap if fingerprint_gap is not None else 0.0,
    }


def test_healthy_production_is_ok():
    level, why = V.verdict(_readings())
    assert level == "ok", "一切正常却被判成 %s（%s）⇒ 门槛会误伤，明天就有人想绕过它" % (level, why)


def test_dead_page_is_blocking():
    r = _readings(pages={"index.html": 404, "ai-daily.html": 200,
                         "rss-aggregator.html": 200, "daily-insight-history.html": 200})
    level, why = V.verdict(r)
    assert level == "blocking" and "index.html" in why, (
        "首页 404 还不是 blocking ⇒ 这门槛拦不住任何东西")


def test_dead_api_function_is_blocking():
    level, why = V.verdict(_readings(api={"api/rss": 500, "api/news": 200}))
    assert level == "blocking" and "api/rss" in why, (
        "Vercel 函数已经 500 还不 blocking ⇒ 站点半坏时我们还往生产推东西")


def test_last_run_not_published_is_warn_not_ok():
    """站点这一小时没更新（1590 那种被掐掉的形状）：可以推，但必须把话说出来。"""
    level, why = V.verdict(_readings(last_runs=[{"num": 1590, "pages": "skipped", "vercel": "skipped"}]))
    assert level == "warn", "Pages 上一场没发却读成 ok ⇒ 这正好是我们 6 小时没发现 Vercel 停更的形状"
    assert "1590" in why


def test_vercel_landing_stall_is_warn_with_evidence():
    runs = [{"num": n, "pages": "success", "vercel": "cancelled"} for n in range(1585, 1591)]
    level, why = V.verdict(_readings(last_runs=runs))
    assert level == "warn", "Vercel 连 6 场没落地还读成 ok ⇒ 就是这次的事故"
    assert "6" in why or "1585" in why, "warn 里必须带场次证据（%s），不然只是一句形容词" % why


def test_open_public_surface_is_not_ok():
    """四页都 200、上一场也发了，但该关的还开着 ⇒ 不能算 ok（这是本批未完成的验收）。"""
    level, why = V.verdict(_readings(public=["HANDOFF.md", ".github/workflows/update.yml"]))
    assert level == "warn" and "HANDOFF.md" in why, (
        "公开面还开着却读成 ok ⇒ 验收会把『平台自带排除』当成我们的功劳")


def test_empty_readings_are_blocking_not_ok():
    """取不到数 ≠ 没问题。这条是门槛自己的反向控制（本仓反复栽在空读数冒充绿）。"""
    level, why = V.verdict({"pages": {}, "api": {}, "last_runs": [], "public_internal": [],
                            "fingerprint_gap_hours": None})
    assert level == "blocking", "空读数被读成 ok ⇒ 网络一抖门槛就放行"
    assert "取不到" in why or "取数" in why


def test_stamp_shape_and_freshness():
    """门槛靠时间戳生效：过期的验证不算验证。"""
    assert V.stamp_is_fresh({"ts": 1000}, now=1000 + 60), "60 秒前刚验过却判过期"
    assert not V.stamp_is_fresh({"ts": 1000}, now=1000 + V.MAX_AGE_S + 1), (
        "过期戳还算新鲜 ⇒ 门槛可以一次性做完然后永久复用")
    assert not V.stamp_is_fresh({}, now=2000), "没有 ts 字段也算新鲜是空转"


# ── 门槛必须接在推送器上，否则它只是另一个"我会记得跑的"东西 ──────────────────────
import subprocess
sys.path.insert(0, os.environ.get("STARHUB_TOOLS_DIR") or os.path.join(ROOT, "tools"))
import data_api_push as D  # noqa: E402


def _drive_push(tmp_path, monkeypatch, stamp_body=None):
    """只驱动到"过没过门槛"这一步：--check-only 会在写完结论后立刻返回。

    必须把 ROOT 也钉回真仓库根：电池是**从副本目录导入模块**的，
    而推送器的 ROOT = 自己文件的上上级 ⇒ 在 `.deploy-tmp/_mut_*/` 里会变成 `.deploy-tmp`，
    于是示例路径被当成"不存在且被 gitignore"，在走到门槛之前就先退了（读数会像门槛失效）。
    示例路径也必须是**仍在索引里且未被忽略**的那类（2026-10-14 起 `HANDOFF.md` 已按
    「GitHub 只放代码」退出索引，再拿它当示例会先撞 `--allow-ignored` 那道拒推）。
    """
    stamp = tmp_path / "_prod_verify.json"
    if stamp_body is not None:
        stamp.write_text(json.dumps(stamp_body), encoding="utf-8")
    monkeypatch.setattr(D, "ROOT", ROOT)
    monkeypatch.setattr(D, "PROD_STAMP", str(stamp))
    monkeypatch.setattr(D, "gate", lambda: (True, "测试守门通过"))
    monkeypatch.setattr(D, "runs", lambda per_page=10: [])
    monkeypatch.setattr(sys, "argv", ["data_api_push.py", "--check-only", "README.md"])
    return D.main()


def test_pusher_refuses_when_no_prod_stamp(tmp_path, monkeypatch, capsys):
    assert _drive_push(tmp_path, monkeypatch, None) == 1
    assert "prod_verify" in capsys.readouterr().out, (
        "拒绝得说清去做什么：没提示跑 tools/prod_verify.py 的话，下次只会加一个 --skip")


def test_pusher_refuses_when_production_is_blocking(tmp_path, monkeypatch, capsys):
    assert _drive_push(tmp_path, monkeypatch, {"ts": 9 ** 10, "level": "blocking",
                                                "why": "index.html 是 404"}) == 1
    assert "index.html" in capsys.readouterr().out, "blocking 的理由必须原样传给推送者看"


def test_pusher_refuses_on_stale_stamp(tmp_path, monkeypatch):
    assert _drive_push(tmp_path, monkeypatch, {"ts": time.time() - D.MAX_AGE_S - 10,
                                                "level": "ok"}) == 1


def test_pusher_allows_warn_but_shows_the_reason(tmp_path, monkeypatch, capsys):
    """已知未修的问题不该永久卡住进度（那只会逼人绕门槛），但必须把话说在日志里。"""
    rc = _drive_push(tmp_path, monkeypatch, {"ts": time.time(), "level": "warn",
                                             "why": "Vercel 连续 6 场（1585–1590）未落地"})
    assert rc == 0, "warn 被判成拒绝 ⇒ 门槛在 #27 修好之前会一直挡路，明天就有人加 --skip"
    assert "1585" in capsys.readouterr().out, "放行时没带上 warn 的原文 ⇒ 推送日志里没有生产状态"


# ── 取数层的形状也要钉：探哪些名字是"内部暴露"，哪些是"本来就该公开" ──────────────
# 这层如果哪天悄悄返回空集，verdict() 会把"公开面已关"报成 OK —— 网络代码没法测，
# 所以把"名单怎么筛"抽成纯函数，判据钉它，网络层只负责照着探。
def test_internal_selector_keeps_only_real_exposure():
    baseline = {
        "HANDOFF.md": "200", ".github/workflows/update.yml": "200",
        "tools/daily_commit_gate.sh": "200", "template.html": "200",
        "index.html": "200", "rss-aggregator.html": "200",          # 站点页：本该公开
        "api/rss.js": "200", "lib/rss_cover.js": "200",             # 运行时：函数要它
        "rss_sources.json": "200",                                   # 硬依赖
        "README.md": "404", "package.json": "404",                   # 改前就已 404 ⇒ 不算我们的功劳
    }
    got = sorted(V.internal_names(baseline))
    assert got == sorted(["HANDOFF.md", ".github/workflows/update.yml",
                          "tools/daily_commit_gate.sh", "template.html"]), (
        "筛出来的内部项不对：%s ⇒ 要么把站点页/运行时依赖当成暴露（误报），"
        "要么把文档与 CI 管线漏掉（假阴性）" % got)


def test_internal_selector_is_not_silently_empty():
    """反向那半：有 200 的内部项却筛出空集 ⇒ 必须炸，不能让门槛拿空集去报 OK。"""
    assert V.internal_names({"HANDOFF.md": "200"}) == ["HANDOFF.md"]
    assert V.internal_names({"index.html": "200", "api/rss.js": "200"}) == [], (
        "全是合法公开项时该返回空 —— 空集本身不是错，错的是把空集当成『一定已收口』")


def test_missing_baseline_is_not_read_as_closed(tmp_path, monkeypatch):
    """改前基线文件不在的时候，取数层不许返回空集（空 = 门槛会读成"公开面已关"）。"""
    monkeypatch.setattr(V, "ROOT", str(tmp_path))
    got = V.read_public_internal()
    assert got, "基线缺失却返回空 ⇒ 门槛会报 OK；这是把『问不出来』读成『没问题』"


def test_in_flight_run_is_not_blamed_for_not_publishing():
    """刚触发的场还没走到 Pages ⇒ conclusion 是 null，那不是"未发布"。

    2026-10-03 01:44 实跑撞上：我 01:41 手动触发 1593，门槛立刻报
    "上一场 1593 未发布 Pages（=None）"⇒ 假警。假警的下一站就是有人把门槛关掉。
    """
    runs = [{"num": 1592, "pages": "success", "vercel": "success"},
            {"num": 1593, "pages": None, "vercel": None}]
    level, why = V.verdict(_readings(last_runs=runs))
    assert level == "ok", "上一场（1592）明明发了，只因最新一场还在跑就报 warn：%s" % why
    assert "1593" in why and "跑" in why, "忽略在飞的场次要说清楚忽略了谁（实得 %r）" % why


def test_only_in_flight_run_is_not_ok():
    """一场都没跑完 ⇒ 没有可判的发布事实，按"取不到"处理，不许读成 ok。"""
    level, why = V.verdict(_readings(last_runs=[{"num": 1593, "pages": None, "vercel": None}]))
    assert level == "blocking", "只有还在跑的一场却报 ok ⇒ 门槛在最需要它的时候没有依据"


def test_channel_blocked_is_not_read_as_broken():
    """403 = Vercel Bot Protection 拦住了本机 curl，不是函数坏了（01:49 实跑撞上）。

    门槛因此必须能表达"我读不到"：读不到 ⇒ warn 并点名是哪一条通道，
    既不许把它当成"坏了"（假警会永久卡住所有推送），也不许当成"没问题"（那是假绿）。
    """
    level, why = V.verdict(_readings(api={"api/rss": None, "api/news": 200}))
    assert level == "warn", "通道被拦却被读成没事 ⇒ %s" % why
    assert "api/rss" in why and ("读不到" in why or "被拦" in why), (
        "warn 里必须点名哪条读不到（实得 %r）" % why)


def test_channel_blocked_pages_still_block():
    """站点页读不到就是真出事：Pages 域没有 Bot Protection，读不到只可能是没发出来。"""
    level, why = V.verdict(_readings(pages={"index.html": None, "ai-daily.html": 200,
                                             "rss-aggregator.html": 200,
                                             "daily-insight-history.html": 200}))
    assert level == "blocking", "首页读不到还放行 ⇒ 站点停了也可能推东西上去（实得 %s）" % why


# ── 取样口径：门槛的"发布事实"只能来自会写 Pages 的那条车道 ─────────────────
# 2026-10-08 22:19 实测到的自伤：`read_runs()` 不分 workflow，直接取最近 8 场。
# 而 star-fast 每 15 分钟一场（现取近 12 场 = 10 场 Star Fast Refresh + 2 场 Update Star Hub），
# 那些场**根本没有** `Deploy to GitHub Pages` 步 ⇒ 每条读数都是 `pages=None`；
# `verdict()` 把 None 解释成"那场还没走到 Pages"（对在飞场是正确的）⇒ `done` 为空
# ⇒ 判 BLOCKING「读到的场都还在跑」。后果不是误报那么简单：小时场在飞的 20~40 分钟里
# 门槛**每次**都挡，正好挡在我需要推送的那道缝上（闸门挡自己人，下一站就是有人加绕过开关）。


def _mix(update=2, fast=10):
    """造一份与生产同形的 run 清单（GitHub 返回的顺序是新→旧）。"""
    rows = [{u"name": V.PUBLISH_WORKFLOW, u"run_number": 1760 - i} for i in range(update)]
    rows += [{u"name": u"Star Fast Refresh", u"run_number": 900 - i} for i in range(fast)]
    return rows


def test_sampling_ignores_lanes_that_never_publish_pages():
    """① 取样必须只留会写 Pages 的那条车道，且输出顺序是旧→新（verdict 用 done[-1] 当"最近一场"）。"""
    sel = V.select_publish_runs(_mix())
    assert len(sel) == 2, (
        u"取样 %d 条（应为 2）⇒ 门槛又在把没有 Pages 步的场读成\u201c还在跑\u201d" % len(sel))
    assert all(x.get(u"name") == V.PUBLISH_WORKFLOW for x in sel), (
        u"样本里混进了不发布 Pages 的车道：%s" % [x.get(u"name") for x in sel])
    assert sel[0][u"run_number"] < sel[-1][u"run_number"], (
        u"输出必须是旧→新，否则 verdict 的 done[-1] 取到的是最旧一场")
    # 反向控制：过滤不许把 update 场自己滤掉（若哪天 run 里没了 name 键，这里必须炸而不是静默空集）
    nameless = [{u"run_number": 1}, {u"run_number": 2}]
    assert V.select_publish_runs(nameless) == [], (
        u"没有 name 的样本被当成发布场收下了 ⇒ 未来的取样会静默收下任意车道")


def test_publish_lane_name_is_derived_not_handwaved():
    """② `PUBLISH_WORKFLOW` 这个常量必须真的等于"含 Deploy to GitHub Pages 步"的那个 workflow 名。

    写死一个名字 = 有人改名后门槛又开始稀释取样（本条就是防这件事）。这里不引第三方 YAML 库，
    按 `name:` 出现在文件首个非注释行这一事实取，避免把 jobs 下的同名键误当工作流名。
    """
    wf_dir = os.path.join(ROOT, u".github", u"workflows")
    holders = set()
    for fn in sorted(os.listdir(wf_dir)):
        if not fn.endswith((u".yml", u".yaml")):
            continue
        text = open(os.path.join(wf_dir, fn), encoding=u"utf-8").read()
        if u"Deploy to GitHub Pages" not in text:
            continue
        for line in text.splitlines():
            s = line.strip()
            if s.startswith(u"#") or not s:
                continue
            if s.startswith(u"name:"):
                holders.add(s.split(u":", 1)[1].strip().strip(u"\"'"))
            break
    assert holders, u"没有任何 workflow 含 `Deploy to GitHub Pages` 步 ⇒ 发布链被挪走了，门槛口径要重定"
    assert V.PUBLISH_WORKFLOW in holders, (
        u"PUBLISH_WORKFLOW=%r 与实际发布车道 %r 不符 ⇒ 取样会再次被 star-fast 挤空"
        % (V.PUBLISH_WORKFLOW, sorted(holders)))


def test_empty_publish_sample_is_blocking_not_ok():
    """③ 反向护栏：过滤后若无任何发布场，门槛必须响亮地判 blocking，而不是把空样本读成"没问题"。"""
    only_fast = [{u"name": u"Star Fast Refresh", u"run_number": 900 - i} for i in range(12)]
    assert V.select_publish_runs(only_fast) == [], u"star-fast 场不该进发布样本"
    level, why = V.verdict(_readings(last_runs=[]))
    assert level == "blocking", u"发布样本为空却判 %s ⇒ 空读数被当成发布事实（实得 %r）" % (level, why)
    assert u"读不到" in why or u"一场都" in why, u"blocking 必须说清是取不到，不是站点坏了：%r" % why
