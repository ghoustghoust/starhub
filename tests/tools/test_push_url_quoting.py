# -*- coding: utf-8 -*-
"""推送工具的 contents URL 必须转义路径（判据先红）。

起因：本轮要清 `.qoder/repowiki/**`，147 个待删路径里 **89 个含非 ASCII**，还有含空格的目录名
（`StarHub 收藏台与 RSS_AIHOT 聚合站点根工程`）。`tools/data_api_push.py` 的三处
`"%s/contents/%s" % (REPO, p)` 都是裸拼：

- 空格会让 urllib 直接抛 `InvalidURL`/被服务端 400；
- 非 ASCII 会在请求行编码时抛 `UnicodeEncodeError`。

坏在哪个环节最要命：`verify(..., gone=...)` 是**删除的复查**。它抛异常时远端 ref 已经动完了，
于是"推成功了但工具报栈"和"删漏了"两种情况长得一模一样 —— 而这正是本轮唯一能证明
147 个路径真的从树上摘掉了的证据。所以转义是判据的一部分，不是洁癖。
"""
import importlib.util
import os
import sys
from urllib.parse import unquote

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TOOLS = os.path.join(ROOT, "tools")

CJK = ".qoder/repowiki/knowledge/zh/StarHub 收藏台与 RSS_AIHOT 聚合站点根工程/概述.md"
ASCII = ".github/workflows/update.yml"


def _load():
    spec = importlib.util.spec_from_file_location("dap_q", os.path.join(TOOLS, "data_api_push.py"))
    m = importlib.util.module_from_spec(spec)
    sys.modules["dap_q"] = m
    spec.loader.exec_module(m)
    return m


def _recorder(m):
    """把 req 换成记录器：返回它看到的 URL，并且永远回答「文件还在」。"""
    seen = []

    def fake(method, path, body=None, tries=6):
        seen.append(path)
        return {"sha": "0" * 40}
    m.req = fake
    return seen


def test_gone_recheck_sends_escaped_urls():
    m = _load()
    seen = _recorder(m)
    m.verify({}, "tag", gone=[CJK, ASCII])
    assert len(seen) == 2, "删除复查一条都没发：%s" % (seen,)
    hit = [u for u in seen if CJK.split("/")[-1] in unquote(u)][0]
    assert "%" in hit, "含空格的 CJK 路径没转义（原样发出会 InvalidURL）：%s" % hit[:80]
    assert " " not in hit, "URL 里有裸空格：%s" % hit[:80]
    assert any(ord(c) > 127 for c in hit) is False, "URL 里还有非 ASCII 字符"


def test_expect_recheck_sends_escaped_urls():
    """同一把尺子也要管「比对 blob」那条：推 CJK 文件时它同样会炸。"""
    m = _load()
    seen = _recorder(m)
    m.verify({CJK: "0" * 40, ASCII: "0" * 40}, "tag")
    assert len(seen) == 2
    hit = [u for u in seen if CJK.split("/")[-1] in unquote(u)][0]
    assert " " not in hit and "%" in hit, "比对 URL 没转义：%s" % hit[:80]


def test_escaping_is_reversible_for_ascii_paths():
    """控制组：ASCII 路径转义后必须原样不变，否则是把好端端的推送链改坏。"""
    m = _load()
    seen = _recorder(m)
    m.verify({}, "tag", gone=[ASCII])
    assert "contents/" + ASCII + "?" in seen[0], \
        "ASCII 路径被改写了（转义必须对老路径恒等）：%s" % seen[0]


def test_the_probe_bites():
    """反空转：记录器确实收到了调用，而不是 verify 提前 return 让三条判据一起「空绿」。"""
    m = _load()
    seen = _recorder(m)
    m.verify({}, "tag")
    assert seen == [], "verify 在空输入时不该发请求"
    seen2 = _recorder(m)
    m.verify({}, "tag", gone=["x.md"])
    assert len(seen2) == 1, "单条 gone 都没打到 req ⇒ 前面的判据是在空集上比"


def test_existence_check_quotes_too():
    """第三处 contents 调用（`remote_matches_local`）同样要转义 —— 它的 `except` 会把抛异常
    误分类成「远端不存在」，那是一次假阴性：门禁以为远端没这个文件，于是允许"新增"覆盖上去。

    这里只打 URL 这一层：`list_files` 直接给一个 CJK 名字，避免为了造样本往工作树里塞文件。
    """
    m = _load()
    seen = _recorder(m)
    m.list_files = lambda rel, allow_ignored=False: [CJK]
    m.remote_matches_local("docs")
    assert len(seen) == 1, "存在性检查根本没发请求 ⇒ 本判据在空集上"
    assert " " not in seen[0] and "%" in seen[0], \
        "存在性检查的 URL 没转义（会被 except 吞成「远端不存在」）：%s" % seen[0][:80]
