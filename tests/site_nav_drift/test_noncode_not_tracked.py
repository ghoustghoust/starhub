# -*- coding: utf-8 -*-
"""A3（advisory）新增：跟代码无关的整目录（docs/ 与 .qoder/）不许再出现在远端树里。

背景（用户裁定，2026-10-02）：「不要把跟代码无关的推送到 github 上」——这条已经说过几次，
本次点单清掉 docs/（56 个 / 1510.4 KiB）和 .qoder/repowiki（91 个 / 2327.6 KiB）。
清一次不等于长期成立：这两个前缀此前是被「故意」跟踪的（.gitignore 里甚至专门写了
「Qoder 本地插件配置，repowiki 知识卡片除外」来放行它们），所以防回潮要两道各自独立：

1. 规则侧——.gitignore 里必须真有 `/docs/` 和 `/.qoder/`，并且对探针路径**真的命中**
   （只 grep 文本会因为规则被更晚的否定规则覆盖而「看着有、实际不管用」）；
2. 索引侧——这两个前缀下跟踪文件数必须为 0。

两道里任何一道单独失效都会留下漏洞，所以分开断言。反空转控制同样是两条：
命中探针之外还要有「代码路径不被忽略」的负向控制，以及「扫描器在 tests/ 上扫得出来」的正向控制
——否则主判据就是在空集合上比谁都不违规。
"""
import os
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 前缀 -> (.gitignore 里期望的规则字面量, 用来验证规则真命中的探针路径)
NONCODE = {
    "docs/": ("/docs/", "docs/probe-check.md"),
    ".qoder/": ("/.qoder/", ".qoder/repowiki/probe-check.md"),
}


def _tracked_under(prefix, cwd=ROOT):
    """返回以 prefix 开头的已跟踪路径列表（-z 是必须的：.qoder 下全是 CJK 文件名）。"""
    r = subprocess.run(["git", "ls-files", "-z"], cwd=cwd, capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise AssertionError("git ls-files 失败 rc=%d：%s" % (r.returncode, r.stderr.strip()[:200]))
    paths = [p for p in (r.stdout or "").split("\0") if p]
    return [p for p in paths if p.startswith(prefix)]


def _ignore_rule(probe, cwd=ROOT):
    """问 git：这个探针路径被哪条规则忽略？返回规则字面量，没被忽略则返回 None。

    --no-index 不能省：默认口径先看索引，已跟踪的文件一律答「不忽略」，
    那样本判据在「规则有、文件也还跟踪着」的原始坏状态下反而报「没命中」。
    """
    r = subprocess.run(["git", "check-ignore", "--no-index", "-v", probe],
                       cwd=cwd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0:
        return None
    # -v 实测输出： <gitignore 路径>:<行号>:<规则字面量>\t<探针路径>
    # （行号那一段必须用 rsplit 从右往左切：Windows 下 gitignore 路径自带盘符冒号）
    head = (r.stdout or "").rstrip("\n").split("\t")[0]
    parts = head.rsplit(":", 2)
    if len(parts) != 3 or not parts[1].isdigit():
        raise AssertionError("check-ignore -v 的输出格式和预期不符，无法取规则字面量：%r" % (head,))
    return parts[2]


def test_probe_bites_on_a_synthetic_repo(tmp_path):
    """控制组：命中/放过两种情形都要判对，否则下面两道主判据是在空转。"""
    subprocess.run(["git", "init", "-q", "."], cwd=str(tmp_path), check=True)
    # 门禁测试必须自带 git 身份：CI 的 runner 没配 user.name
    for k, v in (("user.name", "gate"), ("user.email", "gate@example.invalid"),
                 ("commit.gpgsign", "false")):
        subprocess.run(["git", "config", k, v], cwd=str(tmp_path), check=True)
    (tmp_path / ".gitignore").write_text("/docs/\n", encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "a.md").write_text("x\n", encoding="utf-8")
    # CJK 文件名是必须的：本仓 .qoder/repowiki 下 91 个全是中文名。少了 -z，
    # git 会把它们输出成 "\344\270\255…" 这种带前引号的转义形式，startswith 直接落空
    # —— 判据就会在「其实有 91 个」的情况下读出 0 个。
    (tmp_path / "docs" / "台账示例.md").write_text("文\n", encoding="utf-8")
    (tmp_path / "keep.py").write_text("y\n", encoding="utf-8")
    subprocess.run(["git", "add", "-f", "docs/a.md", "docs/台账示例.md", "keep.py"],
                   cwd=str(tmp_path), check=True)
    subprocess.run(["git", "commit", "-qm", "c"], cwd=str(tmp_path), check=True)

    assert _ignore_rule("docs/a.md", str(tmp_path)) == "/docs/", "规则在、却没认出命中"
    assert _ignore_rule("keep.py", str(tmp_path)) is None, "代码文件被误报成忽略"
    assert sorted(_tracked_under("docs/", str(tmp_path))) == sorted(["docs/a.md", "docs/台账示例.md"]), \
        "扫描器必须连 CJK 文件名一起抓到（少 -z 就只剩 docs/a.md）"
    assert _tracked_under("nope/", str(tmp_path)) == [], "扫描器把不相干前缀也算进来了"


def test_noncode_prefix_is_ignored_by_a_real_rule():
    """主判据一：规则必须存在且真命中（防「删掉规则 ⇒ 索引侧判据独自恒绿」）。"""
    for prefix, (rule, probe) in NONCODE.items():
        got = _ignore_rule(probe)
        assert got == rule, ("%s 没被 .gitignore 真正排除：探针 %s 命中的规则是 %r，期望 %r"
                             " —— 规则被删了或被更晚的分歧规则盖住了" % (prefix, probe, got, rule))
    # 负向控制：这道探针不是「永远回答是」
    assert _ignore_rule("tools/data_api_push.py") is None, \
        "代码文件被报成忽略 ⇒ 探针失灵，上面的命中读数不可信"


def test_no_tracked_file_under_noncode_prefixes():
    """主判据二：这两个前缀下的跟踪文件数必须为 0。"""
    offenders = {}
    for prefix in NONCODE:
        hit = _tracked_under(prefix)
        if hit:
            offenders[prefix] = len(hit)
    assert not offenders, \
        "跟代码无关的目录又回到 git 树里了：%s —— 处置：tools/data_api_push.py --delete 逐路径摘除" % (offenders,)
    # 反空转：同一把尺子在代码前缀上必须量得出东西
    assert len(_tracked_under("tests/")) > 50, \
        "tests/ 下扫不到 50 个文件 ⇒ 索引是空的或扫描器坏了，主判据的「0」不作数"


def test_the_two_gates_are_not_the_same_gate():
    """接线自证：规则侧绿不代表索引侧绿（命中规则、又被 git add -f 塞回去的文件照样在树里）。

    少了这条，很容易误以为「.gitignore 写了就完事」。
    """
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        subprocess.run(["git", "init", "-q", "."], cwd=d, check=True)
        for k, v in (("user.name", "gate"), ("user.email", "gate@example.invalid"),
                     ("commit.gpgsign", "false")):
            subprocess.run(["git", "config", k, v], cwd=d, check=True)
        with open(os.path.join(d, ".gitignore"), "w", encoding="utf-8") as f:
            f.write("/docs/\n")
        os.mkdir(os.path.join(d, "docs"))
        with open(os.path.join(d, "docs", "x.md"), "w", encoding="utf-8") as f:
            f.write("z\n")
        subprocess.run(["git", "add", "-f", "docs/x.md"], cwd=d, check=True)
        subprocess.run(["git", "commit", "-qm", "c"], cwd=d, check=True)
        assert _ignore_rule("docs/x.md", d) == "/docs/", "规则侧应当判「已排除」"
        assert _tracked_under("docs/", d) == ["docs/x.md"], "索引侧应当仍判「在树里」"


def test_skip_guard_never_silently_passes():
    """如果 CI 上 ROOT 不是 git 工作树，必须显式 skip 而不是悄悄绿。"""
    if not os.path.isdir(os.path.join(ROOT, ".git")):
        pytest.skip("不在 git 工作树里，无从判断索引")
    assert subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True,
                          text=True, encoding="utf-8", errors="replace").returncode == 0
