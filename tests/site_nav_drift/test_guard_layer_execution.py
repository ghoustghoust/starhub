# -*- coding: utf-8 -*-
"""A3 必须真的跑 `tests/tools/`，而且这条接线永远不许升级成 blocking。

为什么这是缺陷而不只是"改进"：推送器与生产验证的自测判据（`tests/tools/` 那 8 个文件、63 条）
从写出来那天起就**不在任何一道门禁里**——2026-10-08 现取的 CI 命令行是
A2 = rss_history/rss_source_coverage/site_nav/rss_cover + 四个 rss_translate 指名文件、
A3 = site_nav_drift、B = daily_insight，`tests/tools/` 谁都不跑。
后果已经实打实发生过一次：`data_api_push.py` 引入 `import prod_verify` 之后，
用 importlib 加载它的判据整组 `ModuleNotFoundError`，**四条守卫判据连续空跑五天无人知道**
（HANDOFF §8.34 记着）。入库只解决"别人 clone 拿不到"，解决不了"没人跑"。

所以钉两条，方向分别是"别让它静默失效"与"别让它要命"：
① A3 的 `run` 必须同时跑 `tests/site_nav_drift/` 与 `tests/tools/`
   （只跑其中一个 ⇒ 另一组的判据又变回孤本，正是上面那个坑的复刻）；
② 这一步必须保持 `continue-on-error: true`
   （守卫层是元层：它红不该冻住每天几个定时档与一行热修——本仓为 blocking 闸连坐部署付过学费，
    见 `Restore star state…` 那条"A2 blocking + Deploy 无 if"的教训）。

变异自证：把 ① 的 `tests/tools/` 从命令行删掉 ⇒ test_a3_actually_runs_the_guard_layer 红；
把 ② 的 `continue-on-error` 摘掉 ⇒ test_guard_wiring_stays_advisory 红。两条都不许空跑。
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
UPDATE_YML = os.environ.get("STARHUB_UPDATE_YML") or os.path.join(
    ROOT, ".github", "workflows", "update.yml")

STEP = re.compile(r"^      - name: Quality gate A3(.*)$", re.M)
NEXT_STEP = re.compile(r"^      - name: ", re.M)


def _src():
    with open(UPDATE_YML, encoding="utf-8") as f:
        return f.read()


def _a3_block():
    """A3 那一步的正文（从它的 `- name:` 到下一个 `- name:` 之前）。找不到就抛——
    判据宁可比"返回空串"响亮：改名/删除这一步本身就是这里要防的动作。"""
    src = _src()
    m = STEP.search(src)
    if not m:
        raise AssertionError("update.yml 里找不到名为 `Quality gate A3` 的步骤：这道闸被改名或删除")
    start = src.index(m.group(0))
    nxt = NEXT_STEP.search(src[m.end():])
    end = m.end() + nxt.start() if nxt else len(src)
    return src[start:end]


def test_a3_actually_runs_the_guard_layer():
    """① A3 必须点名跑 tests/tools/（否则守卫判据又是"写了没人跑"）。"""
    blk = _a3_block()
    run = [l for l in blk.splitlines() if re.match(r"^\s+run:", l)]
    assert run, "A3 没有 run 块 ⇒ 谈不上跑什么"
    body = "\n".join(run)
    assert "pytest" in body, "A3 的 run 已经不是 pytest 了：" + body[:200]
    assert "tests/site_nav_drift/" in body, \
        "A3 不再跑 tests/site_nav_drift/ ⇒ 原来那批漂移判据变成孤本"
    assert re.search(r"tests/tools/?\b", body), (
        "A3 不跑 tests/tools/ ⇒ 推送器/生产验证的 63 条自测判据回到"
        "\u201c空跑五天无人知\u201d那个形状（本仓 10-08 已实吃过一次）")


def test_guard_wiring_stays_advisory():
    """② 这条接线不得变成 blocking：守卫层红不该把整场部署冻住。"""
    blk = _a3_block()
    assert re.search(r"^\s+continue-on-error:\s*true\s*$", blk, re.M), (
        "A3 丢了 continue-on-error ⇒ tests/tools 的环境差异会冻结整场发布；"
        "本仓的既有裁定是元层判据只出声、不拦发布")
