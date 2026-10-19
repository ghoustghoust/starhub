# -*- coding: utf-8 -*-
"""A3（advisory）：`sync-agnes-env.yml` 的两道部署守门必须在位、且真的会拦。

为什么单独钉这个文件：2026-10-08 P1 排查"完全解耦还剩什么"时发现，**Vercel 生产版本有两个
部署者**——`update.yml` 的 Deploy 步（按 api/lib/vercel.json/rss_sources 四类内容哈希门控）
与本 workflow 的 `npx vercel --prod`。而两者喂给 Vercel 的输入并不相同：update.yml 是在
Prune 步删掉大产物之后才部署（`update.yml:538-549`），本 workflow 部署的是一份未 prune 的
checkout。所以只要有一场 update 正在跑，这里就可能把生产 API 主机换成"另一种输入"的版本——
与 10-07 那次 Pages 双写者同构：不是谁改错了，是两条链路各自整棵覆盖。

key 轮换确实必须重新部署才生效（env 在部署时快照），所以不能简单砍掉 `--prod`；
收法是给它加上互斥与时效守门，形状照抄本仓已有的先例 `repo-trim.yml:137-139`
（"期间 main 已前进 ⇒ 中止让人重跑"），不发明新机制。

本文件钉的是**这四条会被悄悄改掉的东西**：守门步存在、顺序在部署之前、失败方向是 exit 1
而不是 echo、checkout 版本与另两条车道一致（P0 期间发现过 `deploy-pages@v4` 与 `@v5` 分叉，
同一类"两条车道各自漂"）。
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WF = (os.environ.get("STARHUB_SYNC_YML")
      or os.path.join(ROOT, ".github", "workflows", "sync-agnes-env.yml"))

GUARD_BEFORE = "Guard — no hourly build in flight"
GUARD_AFTER = "Guard — main did not move during deploy"
UPSERT = "Upsert AGNES_API_KEY on Vercel"
DEPLOY = "Trigger production redeploy (vercel CLI)"


def _text():
    with open(WF, encoding="utf-8") as f:
        return f.read()


def _step_body(txt, name):
    """取步骤正文：从名字起，到下一个 `- name:` 或 `- uses:` 之前（不写死相邻步骤名，
    否则谁插一步进来判据就 IndexError——本仓 10-07 为这个坑改过一次抽取方式）。"""
    i = txt.index(name)
    j = len(txt)
    for pat in ("\n      - name:", "\n      - uses:"):
        k = txt.find(pat, i + len(name))
        if k != -1:
            j = min(j, k)
    assert j > i, "找不到 %r 之后的步骤边界" % name
    return txt[i:j]


def test_both_guards_exist_and_wrap_the_deploy():
    """两道守门都要在，且顺序必须是 前守门 → upsert → 部署 → 后守门。

    前守门若排在 upsert 之后，env 已经被写进去了才中止——那正是最难查的半套状态。
    """
    t = _text()
    for name in (GUARD_BEFORE, GUARD_AFTER, UPSERT, DEPLOY):
        assert name in t, "守门/部署步 %r 不见了" % name
    order = [t.index(x) for x in (GUARD_BEFORE, UPSERT, DEPLOY, GUARD_AFTER)]
    assert order == sorted(order), \
        "步骤顺序被改动：必须 前守门→upsert→部署→后守门，实际位置 %s" % order


def test_pre_guard_actually_queries_and_refuses():
    """前守门必须真去查 in_progress 的 update 场，并且非零退出——不许退化成只打印。

    退化成 echo 的守门比没有守门更糟：它会让人以为这里互斥过。
    """
    body = _step_body(_text(), GUARD_BEFORE)
    assert re.search(r"actions/runs\?[^\"']*workflow=update\.yml", body), \
        "前守门没有按 workflow=update.yml 查 run 列表 ⇒ 它挡不住小时场"
    assert "status=in_progress" in body, "前守门没查 in_progress ⇒ 正在跑的那场看不见"
    assert re.search(r'\[\s*"\$running"\s*!=\s*"0"\s*\]', body), \
        "前守门的判定式不是一句会走 exit 1 的比较 ⇒ 可能形同不存在"
    assert "exit 1" in body, "前守门发现并发时没有 exit 1 ⇒ 只是提示，不拦"
    assert "::error::" in body, "拦下了要留下可读的错，否则日志里分不清「查过没有并发」和「没查」"


def test_post_guard_compares_shas_and_refuses_on_drift():
    """后守门必须把部署前的 BASE_SHA 与部署后的 main 比，不等就 exit 1。

    不比对就等于没有：部署期间小时场推了新代码，生产就停在"旧代码 + 新 env"的组合上，
    而 run 显示 success——这是最舒服的一种错。
    """
    body = _step_body(_text(), GUARD_AFTER)
    assert "BASE_SHA" in body, "后守门没用到部署前记录的 sha ⇒ 无从判断期间有没有人推"
    assert re.search(r'\[\s*"\$LIVE"\s*!=\s*"\$BASE_SHA"\s*\]', body), \
        "后守门的比较式不对 ⇒ 可能恒不触发"
    assert "exit 1" in body and "::error::" in body, "发现漂移必须拦并出声"


def test_pre_guard_records_base_sha_before_any_write():
    """BASE_SHA 必须在 upsert（第一次写 Vercel）之前记录，否则后守门比的是"部署动作之后"的时刻。"""
    t = _text()
    assert t.index("BASE_SHA=") < t.index(UPSERT), \
        "BASE_SHA 记录点晚于 upsert ⇒ 部署前窗口没被覆盖，后守门失去意义"


def test_checkout_version_matches_the_other_lanes():
    """checkout 版本必须与另两条车道一致。

    起因是 P0 期间实测到 `deploy-pages@v4`（star-fast）与 `@v5`（update）分叉——
    两条车道各自漂是这里反复出事的形状，工具版本是同一类。
    """
    def ver(path):
        m = re.search(r"actions/checkout@v(\d+)", _text() if path == WF else open(path, encoding="utf-8").read())
        return m.group(1) if m else None
    others = [ver(os.path.join(ROOT, ".github", "workflows", n))
              for n in ("update.yml", "star-fast.yml")]
    mine = ver(WF)
    assert mine and set(others) == {mine}, \
        "checkout 版本分叉：本车道 v%s，另两条 %s ⇒ 对齐到同一个" % (mine, others)
