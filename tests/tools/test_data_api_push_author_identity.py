# -*- coding: utf-8 -*-
"""推送器建的提交必须**显式带 author/committer**，且邮箱是不带用户 ID 前缀的 noreply 形式。

根因（2026-10-03 用户从 Vercel 控制台读到的原文）：
    The deployment was blocked because the commit email
    83650072+Kwei168@users.noreply.github.com could not be matched to a GitHub account.
    Ensure your git email matches your GitHub account.

这条为什么值 300+ 个提交的历史账：Vercel 的 "Block deployments from unverified users" 拿
**部署所记 commit 的 author 邮箱**去匹配 GitHub 账号。`tools/data_api_push.py` 走 Git Data API 建提交时
只发 `{message, tree, parents}`，author 由 GitHub 按 token 账号自动填成 `83650072+Kwei168@…`
（GitHub 的"邮箱保密"新格式），而 Vercel 认不出这个形式 ⇒ **凡挂在我提交上的 Vercel 部署一律 Blocked**。

现取证据（不是推断）：
- 最近 400 个提交的 author 邮箱只有两种：bot `41898282+github-actions[bot]@…`（263 次）与
  我这个 `83650072+Kwei168@…`（137 次）⇒ 从来没有第三种，也就是我经手的提交从没被 Vercel 收下过。
- Vercel 面板里唯二两条 Ready 的部署（`c4731bd`、`2c6ad48`）提交信息都是 `chore: auto update stars`，
  即 CI 里 bot 自己那次提交；其余全 Blocked，全是我这几批的提交信息。
- 决定性的一条：1592 之所以"突然成功"（34 秒），是因为那场 bot 先提交了 `c4731bd`（01:16:42Z），
  Vercel 01:17 部署时记录的 commit 就是 bot 的；1593 起有些场次没有产生 bot 提交，
  commit 又回到我的 ⇒ 又被拒。**Ready/Blocked 交错与 Vercel 状态、内容是否变化、timeout/--no-wait 全都无关。**

⇒ 我此前把这件事一路描述成"CLI 原地等构建被掐""Vercel 间歇秒拒""外部原因未知"，并据此改了
timeout-minutes、加了 --no-wait、又加了"内容没变就不部署"的门 —— 三条都是围着症状转，
真正的因从第一次推送就在。这条判据存在的意义就是不让它再被当成外部故障。

口径说明：这里断言的是**请求体形状**（AST 读 `git/commits` 那个 POST 的 dict），不是发真请求 ——
真请求要写远端，测试不许碰；而"GitHub 会不会仍用默认身份"恰恰取决于这个 dict 有没有 author 键。
"""
import ast
import io
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
# 留注入口子：变异自证要改副本再跑，写死真实路径的判据永远测不出"变异没落到目标上"。
PUSHER = os.environ.get("DATA_API_PUSH_SRC") or os.path.join(ROOT, "tools", "data_api_push.py")

# 期望值：不带用户 ID 前缀的 noreply 形式。GitHub 两种 noreply 都指向同一账号，
# 但 Vercel 的邮箱↔账号匹配只认这一种（实测证据见模块 docstring）。
WANT_EMAIL = "Kwei168@users.noreply.github.com"
BAD_FORM = re.compile(r"^\d+\+")          # 83650072+Kwei168@… 这种就是 Vercel 认不出的形状


def _tree():
    return ast.parse(io.open(PUSHER, encoding="utf-8").read())


def _commit_call():
    """找到 POST git/commits 那一次调用，返回它的 body dict 节点。"""
    hits = []
    for node in ast.walk(_tree()):
        if not isinstance(node, ast.Call):
            continue
        fname = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
        if fname != "req" or len(node.args) < 2:
            continue
        first = node.args[0]
        second = node.args[1]
        if not (isinstance(first, ast.Constant) and first.value == "POST"):
            continue
        src = ast.dump(second)
        if "git/commits" not in src:
            continue
        for arg in node.args[2:]:
            if isinstance(arg, ast.Dict):
                hits.append(arg)
    assert hits, "找不到 `req(\"POST\", …git/commits…, <dict>)` ⇒ 推送器改形了，这条判据要一起更新"
    assert len(hits) == 1, "git/commits 的 body dict 有 %d 处 ⇒ 无法确定该看哪个" % len(hits)
    return hits[0]


def _keys(body):
    return [k.value for k in body.keys if isinstance(k, ast.Constant)]


def _value_for(body, key):
    for k, v in zip(body.keys, body.values):
        if isinstance(k, ast.Constant) and k.value == key:
            return v
    return None


def _resolve_dict(node):
    """允许实现写成 `author": COMMIT_IDENTITY`（引用模块常量），这里把常量解析成 dict 节点。"""
    if isinstance(node, ast.Dict):
        return node
    if isinstance(node, ast.Name):
        for st in _tree().body:
            if isinstance(st, ast.Assign):
                for t in st.targets:
                    if isinstance(t, ast.Name) and t.id == node.id:
                        return _resolve_dict(st.value)
    return None


def _field(node, key):
    d = _resolve_dict(node)
    if d is None:
        return None
    for k, v in zip(d.keys, d.values):
        if isinstance(k, ast.Constant) and k.value == key and isinstance(v, ast.Constant):
            return v.value
    return None


def _literal_email(node):
    """author/committer 里的 email 字面值。"""
    return _field(node, "email")


def test_commit_request_must_carry_author_and_committer():
    body = _commit_call()
    keys = _keys(body)
    for who in ("author", "committer"):
        assert who in keys, (
            "`git/commits` 请求体里没有 %s（实得键 %s）⇒ GitHub 会按 token 账号自动填成 "
            "`83650072+Kwei168@users.noreply.github.com`，Vercel 匹配不到账号，"
            "挂在这个提交上的部署会被判 Blocked" % (who, keys))


def test_author_email_is_the_id_free_noreply_form():
    body = _commit_call()
    for who in ("author", "committer"):
        email = _literal_email(_value_for(body, who))
        assert email, "%s 里挖不出字面 email ⇒ 判据无法确认身份，请写成显式常量" % who
        assert email == WANT_EMAIL, (
            "%s.email = %r，期望 %r" % (who, email, WANT_EMAIL))
        assert not BAD_FORM.match(email.split("@")[0]), (
            "%s.email 用了 `数字+login@` 这种 GitHub 保密新格式（%r）——"
            "这正是 Vercel 报 'could not be matched to a GitHub account' 的那个值" % (who, email))


def _docstring_nodes(tree):
    """模块/类/函数各自的 docstring 节点 —— 它们承载的是历史与理由，不是会被发出去的字符串。"""
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                out.add(id(body[0].value))
    return out


def test_the_bad_email_is_not_reintroduced_as_a_string_in_the_pusher():
    """兜底：不许在**代码里**再把带 ID 前缀的 noreply 当身份写出去。

    只扫字符串字面量、且排除 docstring —— 本文件与推送器的注释里都要留下那个坏邮箱当证据，
    第一版我扫的是整个源文件文本，结果被自己写的解释注释判红（判据不准，不是注释该删）。
    """
    tree = _tree()
    docstrings = _docstring_nodes(tree)
    bad = set()
    for node in ast.walk(tree):
        if id(node) in docstrings:
            continue
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            for m in re.findall(r"\b\d{6,}\+[A-Za-z0-9.\[\]\-]+@users\.noreply\.github\.com", node.value):
                if m != "41898282+github-actions[bot]@users.noreply.github.com":   # CI 里 bot 的身份
                    bad.add(m)
    assert not bad, (
        "推送器的代码字符串里出现带用户 ID 前缀的 noreply 邮箱 %s ⇒ 会被 Vercel 判 Blocked" % sorted(bad))


def test_author_name_is_the_account_login_not_a_local_git_name():
    """name 也要显式：本机 git 的 user.name 是空的，垃圾作者名以前在别的仓踩过。"""
    body = _commit_call()
    for who in ("author", "committer"):
        name = _field(_value_for(body, who), "name")
        assert name == "Kwei168", "%s.name = %r，期望账号登录名 'Kwei168'" % (who, name)
