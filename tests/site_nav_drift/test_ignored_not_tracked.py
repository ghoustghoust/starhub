# -*- coding: utf-8 -*-
"""A3（advisory）新增：命中 .gitignore 却仍被跟踪的文件不许增长。

起因是本轮实测：远端树里躺着一个 411,404 B 的 `_check_js_temp.js`（qrcode 压缩件拼上站点片段的
Scratch 产物），`.gitignore:18` 明明写了它，它却还在树里 —— 而 `(^|/)[._]` 前缀的文件既不进 Pages
制品（Stage 步就把它滤掉了），也没有任何业务代码引用它。这种文件的特点是：**它不会每场重写**，
所以它不动那条 0.03 MiB/场 的斜率，它只是永久驻留；等下一次历史清理不会再有（用户已明确不再第三次重写），
所以唯一可行的处置是"从现在起不许再多"。

同一个探针第一次跑还差点把结论读反：`git check-ignore` 默认**先看索引**，已跟踪的文件一律答"不忽略"，
于是"ignored 却已跟踪"这一类会被它自己吞掉、报成 0 命中 —— 必须 `--no-index`。
本文件用的 `git ls-files -i -c` 是另一个口径（索引 ∩ 忽略规则），两条口径独立给出同一份 5 项清单，
这条读数才算可信；也正因为它容易空跑，下面配了一条双向控制（反空转）。
"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 已知的历史驻留项（2026-10-01 实测字节数）。删掉一个就把这里同步删一行 ——
# 名单过期本身要判红，否则它会慢慢变成"什么都允许"的挡箭牌。
# 2026-10-02：_check_js_temp.js（411,404 B）与 _fix_quotes.py（1,039 B）已随批 6 之后
# 用 --delete 逐路径摘掉，所以两行从名单里删除；名单里剩下的是两件刻意不动的：
# .workbuddy/*（另一条工作线的用户内容，不碰）与 .vercel/project.json（120 B，
# CI 走 VERCEL_PROJECT_ID 环境变量不依赖它，但用户本地 vercel CLI 可能要 ⇒ 不为 120 B 冒断供风险）。
LEGACY = {
    ".vercel/project.json",                             # 120 B：Vercel 项目链接，删前先确认 CLI 不需要
}


def _offenders(cwd):
    """返回"已跟踪但命中忽略规则"的路径集合（-c = cached，-i = ignored）。"""
    r = subprocess.run(["git", "ls-files", "-i", "-c", "--exclude-standard", "-z"],
                       cwd=cwd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise AssertionError("git ls-files 失败 rc=%d：%s" % (r.returncode, r.stderr.strip()[:200]))
    return {p for p in (r.stdout or "").split("\0") if p}


def test_probe_sees_an_ignored_tracked_file_in_both_directions(tmp_path):
    """控制组：探针必须能抓到，也必须能放过 —— 否则下面的主判据就是在空集合上比谁都不违规。"""
    subprocess.run(["git", "init", "-q", "."], cwd=str(tmp_path), check=True)
    # 门禁测试必须自带 git 身份：CI 的 runner 没配 user.name，裸 commit 会当场崩
    for k, v in (("user.name", "gate"), ("user.email", "gate@example.invalid"),
                 ("commit.gpgsign", "false")):
        subprocess.run(["git", "config", k, v], cwd=str(tmp_path), check=True)
    (tmp_path / ".gitignore").write_text("*.ignored\n", encoding="utf-8")
    (tmp_path / "keep.ignored").write_text("x\n", encoding="utf-8")
    (tmp_path / "clean.txt").write_text("y\n", encoding="utf-8")
    subprocess.run(["git", "add", "-f", "keep.ignored"], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "add", "clean.txt"], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "commit", "-qm", "c"], cwd=str(tmp_path), check=True)

    got = _offenders(str(tmp_path))
    assert got == {"keep.ignored"}, "探针在控制仓上就不对（应抓到 keep.ignored，放过 clean.txt）：%s" % (got,)

    (tmp_path / "second.ignored").write_text("z\n", encoding="utf-8")
    subprocess.run(["git", "add", "-f", "second.ignored"], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "commit", "-qm", "c2"], cwd=str(tmp_path), check=True)
    assert _offenders(str(tmp_path)) == {"keep.ignored", "second.ignored"}, "新增一个就该多抓一个"


def test_no_new_ignored_file_is_tracked():
    """主判据：树里"被忽略却已跟踪"的集合不许超出 LEGACY 名单，名单也不许过期。"""
    if not os.path.isdir(os.path.join(ROOT, ".git")):
        pytest_skip("不在 git 工作树里，无从判断索引")
    got = _offenders(ROOT)
    new = sorted(got - LEGACY)
    assert not new, "混进了被 .gitignore 忽略却已被跟踪的新文件：%s —— 它不会每场重写，" \
                    "但会永久占着 tip；要么 git rm --cached，要么从 .gitignore 里撤掉那条规则" % (new,)
    stale = sorted(LEGACY - got)
    assert not stale, "LEGACY 名单过期（这些已经不跟踪了）：%s —— 请把对应行删掉" % (stale,)
    # 反空转：这条工作树的索引确实看得到东西，否则上面两个 assert 会在空集上"恒绿"
    assert len(subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True,
                              text=True, encoding="utf-8", errors="replace").stdout.splitlines()) > 50, \
        "索引本身是空的 ⇒ 本判据没在测任何东西"


def pytest_skip(msg):
    import pytest
    pytest.skip(msg)
