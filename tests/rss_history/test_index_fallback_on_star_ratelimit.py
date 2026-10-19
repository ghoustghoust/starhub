# -*- coding: utf-8 -*-
"""star 限流那一场必须有首页兜底 —— 否则缺的不是"首页旧一小时"，而是整站那一场不发。

为什么这是缺陷而不是"降级一下就好"（2026-10-03 现取代码与清单）：
  · `fetch_and_build.py:779` 的 `if stars_ok:` 罩子里才有 `open("index.html","w")` ⇒ 限流场**根本不写首页**；
  · 4 个页面 HTML 已于 `7c08d02cea` 退出 git ⇒ 工作树里没有兜底副本，那句旧注释"index.html 保持不变"
    的前提早就不存在；
  · Stage 步的必检清单里就有 `index.html` ⇒ 缺件 ⇒ `::error::Pages 缺件` 且该步判失败 ⇒
    `Upload/Deploy Pages` 的 `if steps.stage.outcome == 'success'` 判假 ⇒ **那一小时整站不发布**。
日报侧已经解耦（判据 `test_generator_decoupling.py` 钉着），首页不能同样解耦 —— 它的数据主体就是星标列表，
所以这里的正确解法是**兜底**：优先从线上 Pages 取回上一版（Pages 不吃 GitHub 限流），
取不到才写最小占位页。两条路径都必须"宁可不美，不可缺件"。
"""
import ast
import importlib.util
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SPEC = importlib.util.spec_from_file_location("fab", os.path.join(ROOT, "fetch_and_build.py"))
fab = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fab)

PAGES_MARK = "<!DOCTYPE html>"


def _real_page(n=60000):
    return PAGES_MARK + "\n<html><head><title>GitHub Star 收藏台</title></head><body>" + ("x" * n) + "</body></html>"


def test_fallback_uses_the_live_page_when_it_is_a_real_page(tmp_path, monkeypatch):
    got = {}

    def fake_fetch(url):
        got["url"] = url
        return _real_page()
    monkeypatch.setattr(fab, "fetch_live_index", fake_fetch)
    src = fab.write_index_fallback(out_dir=str(tmp_path))
    assert src == "live", "线上有合格首页却走了占位 ⇒ 用户会看到功能被砍的空页"
    body = (tmp_path / "index.html").read_text(encoding="utf-8")
    assert body.startswith(PAGES_MARK) and "收藏台" in body
    assert "github.io" in got["url"], "兜底要从 Pages 域取（它不吃 GitHub API 限流），实得 %s" % got["url"]


def test_fallback_refuses_an_error_page_and_still_ships_a_page(tmp_path, monkeypatch):
    """关键反向：把 404/错误页当"上一版首页"发出去，比缺件更糟（缺件至少会红）。"""
    for bad in ("", "<html><body>404</body></html>", PAGES_MARK + "<html>" + "y" * 100 + "</html>"):
        monkeypatch.setattr(fab, "fetch_live_index", lambda url, _b=bad: _b)
        src = fab.write_index_fallback(out_dir=str(tmp_path))
        assert src != "live", "不合格响应被当成上一版首页收下（长度 %d）⇒ 会发布错误页" % len(bad)
    body = (tmp_path / "index.html").read_text(encoding="utf-8")
    assert "星标数据暂缺" in body and "ai-daily" in body, (
        "占位页必须说明状态并留一条去日报页的路（实得前 80 字：%r）" % body[:80])


def test_fallback_does_not_overwrite_a_good_index(tmp_path):
    keep = _real_page(70000)
    (tmp_path / "index.html").write_text(keep, encoding="utf-8")
    assert fab.write_index_fallback(out_dir=str(tmp_path)) is None, (
        "首页已经生成时不许碰它 —— 兜底只在缺件时生效，否则限流场会把刚做好的新页换成旧页")
    assert (tmp_path / "index.html").read_text(encoding="utf-8") == keep


def test_fallback_call_sits_outside_the_stars_ok_branch():
    """接线反向控制：兜底一旦被人挪进**成功**分支（`if stars_ok:`），限流场就正好执行不到它。

    只禁 `if stars_ok:`，允许 `if not stars_ok:` —— 后者正是它该在的地方（只在没拉到数据时兜底），
    拿 size 猜"有没有首页"会把瘦身后的新页误覆盖成线上旧页，所以不許写成无条件调用。
    """
    src = open(os.path.join(ROOT, "fetch_and_build.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    # 每个调用外层最近的 If 是谁包的（而不是"行号在不在某棵子树里"）——
    # 只有 `if not stars_ok:` 算正确接线；无条件调用会被首页瘦身后误覆盖新页。
    def encloses(node):
        return isinstance(node, ast.Call) and getattr(node.func, "id", "") == "write_index_fallback"

    wrappers = {}

    def walk(node, holder):
        for ch in ast.iter_child_nodes(node):
            if encloses(ch):
                wrappers[ch] = holder
            walk(ch, ch if isinstance(ch, ast.If) else holder)

    walk(tree, None)
    assert wrappers, "fetch_and_build.py 里没有 write_index_fallback() 的调用 ⇒ 兜底根本没接上，判据在验空气"

    def is_not_stars_ok(test):
        return (isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not)
                and isinstance(test.operand, ast.Name) and test.operand.id == "stars_ok")

    for call, holder in wrappers.items():
        assert holder is not None and is_not_stars_ok(holder.test), (
            "write_index_fallback() 的包绕条件是 %r（行 %d）⇒ 既没锁在限流分支里，"
            "就可能把刚生成的新首页覆盖成线上旧页" % (ast.dump(holder.test) if holder else None, call.lineno))


def test_stage_still_requires_index_html():
    """钉住前提：Stage 的必检清单里有 index.html。

    有人若为了"让限流场也能发"把它从清单里摘掉，问题会从"不发"变成"发出半坏站点"，
    那更糟 —— 所以这条要红给他看，逼他去修生成端。
    """
    body = open(os.path.join(ROOT, ".github", "workflows", "update.yml"), encoding="utf-8").read()
    line = [l for l in body.splitlines() if "for must in" in l]
    assert line and "index.html" in " ".join(line), (
        "Stage 必检清单不再包含 index.html ⇒ 缺件会静默发出没有首页的站点：%s" % line[:1])
