# -*- coding: utf-8 -*-
"""不许出现"写着但从不跑"的孤儿测试目录。

为什么钉这条（2026-10-01 实测）：`tests/rss_translate/` 里有运行时翻译守卫与翻译引擎判据，
但 `grep -rl rss_translate .github/workflows/` 是 **0 命中** —— 也就是说翻译链的回归
可以完全静默落地，而我一直以为"有测试挡着"。同形状的孤儿还有 rss_composite / rss_date /
rss_sort / tools / trim_guard，共 6 个目录。

判据分两层，缺一层都会假绿：
  ① 必须挡住的目录，要真的出现在 A2（blocking）那条 pytest 命令里；
  ② 其余含 `test_*.py` 的目录必须在 UNWIRED 里点名并写原因 ——
     新增目录却忘了接线时，这条会红，而不是又多一个静默孤儿。
"""
import io
import os
import re

import pytest

yaml = pytest.importorskip("yaml")

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WF = os.environ.get("STARHUB_UPDATE_YML") or os.path.join(ROOT, ".github", "workflows", "update.yml")

# 必须进 blocking 闸的判据路径（都是"回归会静默上线"的那类）。
# rss_translate 只到**文件**粒度：同目录的 test_translate_engines.py 是脚本风格，接整个目录
# 会在 09:00 那场把 A2 变成 INTERNALERROR（详见 update.yml 的注释）。
# 不含 trim_guard：它排练的是 repo-trim.yml（真跑 git-filter-repo），ubuntu 行为未验证，
# 放进每场构建的 blocking 闸就是我上次"自己把整场冻掉"的形状 —— 它已挂到 repo-trim.yml 里。
MUST_BLOCK = ["tests/rss_history/", "tests/rss_source_coverage/", "tests/site_nav/",
              "tests/rss_cover/",
              "tests/rss_translate/test_runtime_translate_guards.py",
              "tests/rss_translate/test_wall_queue_window.py",
              "tests/rss_translate/test_buildtime_skip_guard.py",
              # 接进 A2 却不在这里点名 = 谁把它从命令里删掉都没人报警（"已接线"这件事本身也要钉住）。
              "tests/rss_translate/test_agnes_429_backoff.py"]

# 已知未接线的目录 + 原因。别顺手往里加：每一条都意味着一类无人监督的判据。
# 原因必须自立——写"同上"的人（我）过不了 test_unwired_entries_are_explained。
UNWIRED = {
    "rss_composite": "脚本风格（模块级 sys.exit），pytest 收集即 INTERNALERROR；要接先得改造成 test 函数",
    "rss_date": "脚本风格：文件末尾 sys.exit(0)，被 pytest 当测试模块导入会打崩整场收集",
    "rss_sort": "单文件脚本风格，同样是 import 期执行 + sys.exit；改造前不进闸",
    # "tools" 于 2026-10-08 从本表**移出**（不是漏删）：当时的理由是"慢在临时仓里真跑 git，不值得每场跑"
    # （旧登记：57 条 160.3s，与远端这行原文一致）。这个理由被事实否掉了——正因为没人跑，
    # `data_api_push.py` 引入 `import prod_verify` 之后整组判据 ModuleNotFoundError 空跑了五天
    # （HANDOFF §8.34），而"值不值得"的前提恰恰是"它在跑"。今天现跑 A3 的实跑范围——
    # `tests/tools/` 整目录 12 个文件 103 条 = 146.2s（本机，与旧登记 160.3s 同量级）；
    # 同场 A3 的另一组 163 条在 CI 只花 8.81s ⇒ CI 侧大概率落在几十秒这一档。
    # 接的是 A3（advisory）而不是 A2：INTERNALERROR 只会红一道出声的闸，不会冻部署——
    # 上面三条留在表里正是因为它们有这个风险。接线由 tests/site_nav_drift/
    # test_guard_layer_execution.py 两条判据钉住（必须真跑 / 必须保持 advisory）。
}

# 闸内"真会在收集期退出"的文件：登记后允许存在，但新接进来的必须可收集。
# 空集是**当前实测**（2026-10-01 重扫，排除 `if __name__ == '__main__'` 守卫后）：
# rss_history / rss_source_coverage / site_nav / site_nav_drift / daily_insight 全为 0，
# 仅有的 2 个（tests/rss_translate/test_translate_cache.py、test_translate_engines.py）
# 在 A2 里没被收集 —— 因为 A2 对 rss_translate 是按文件点名的。
# 我第一版把 `__main__` 守卫里的 exit 也算成地雷，报了"闸内 7 个地雷"，是误判：
# `if __name__ == "__main__": sys.exit(...)` 在 pytest 导入时根本不执行，是标准写法。
KNOWN_FRAGILE = set()


def _a2_cmd():
    with open(WF, encoding="utf-8") as fh:
        doc = yaml.safe_load(fh)
    for st in doc["jobs"]["update"]["steps"]:
        name = st.get("name") or ""
        if name.startswith("Quality gate A2"):
            return st.get("run") or ""
    pytest.fail("update.yml 里找不到 Quality gate A2 步骤：%s" % WF)


def _referenced_dirs():
    """任何 workflow 文本里出现过的 `tests/<dir>` 都算"已接线"。

    不能只盯 A2：daily_insight 归 B 闸、site_nav_drift 归 A3，它们不是孤儿。
    """
    wf_dir = os.path.join(ROOT, ".github", "workflows")
    refs = set()
    for fn in os.listdir(wf_dir):
        if not fn.endswith((".yml", ".yaml")):
            continue
        text = open(os.path.join(wf_dir, fn), encoding="utf-8").read()
        refs |= set(re.findall(r"tests/([A-Za-z0-9_]+)", text))
    return refs


def test_required_dirs_are_actually_run_by_the_blocking_gate():
    cmd = _a2_cmd()
    missing = [d for d in MUST_BLOCK if d not in cmd]
    assert not missing, (
        "这些目录的判据不在 blocking 闸里，回归会静默上线：%s（A2 命令：%s）" % (missing, cmd))


def test_no_new_orphan_test_directory():
    """含 test_*.py 的目录要么被某个 workflow 引用，要么在 UNWIRED 里点名并给原因。"""
    tests_dir = os.path.join(ROOT, "tests")
    present = set()
    for d in os.listdir(tests_dir):
        p = os.path.join(tests_dir, d)
        if not os.path.isdir(p):
            continue
        if any(f.startswith("test_") and f.endswith(".py") for f in os.listdir(p)):
            present.add(d)
    unknown = sorted(present - _referenced_dirs() - set(UNWIRED))
    assert not unknown, (
        "这些测试目录既没被任何 workflow 引用、也没在 UNWIRED 里说明原因（= 新的静默孤儿）：%s" % unknown)


def test_unwired_entries_are_explained():
    bad = [d for d, why in UNWIRED.items() if len(why) < 12]
    assert not bad, "UNWIRED 里这些条目没有像样的原因：%s" % bad


def _a2_paths():
    """A2 命令行里的 pytest 路径（用来把"接了什么"落实到文件清单）。"""
    cmd = _a2_cmd()
    i = cmd.find("pytest")
    assert i >= 0, "A2 里没有 pytest 命令：%s" % cmd
    toks = [t for t in cmd[i:].split() if t.startswith("tests/")]
    assert toks, "A2 的 pytest 没有测试路径：%s" % cmd
    return toks


def _module_level_exits(path):
    """返回"被 pytest 当模块导入时就会执行到的 sys.exit"行号。

    只看模块级语句（含模块级 if/try/for 的体），两类不算：
      · 函数与类体内的 exit —— 那是测试自己控制的控制流；
      · `if __name__ == "__main__": sys.exit(...)` —— pytest 导入时不执行，是标准写法。
        我第一版没排除这一类，把 5 个健康的 pytest 文件误判成"地雷"并写进了手册与
        KNOWN_FRAGILE，等于用一条假事实去指导别人的判断。
    """
    import ast
    tree = ast.parse(io.open(path, encoding="utf-8").read(), filename=path)
    found = []

    def is_main_guard(node):
        if not isinstance(node, ast.If):
            return False
        t = node.test
        return (isinstance(t, ast.Compare) and isinstance(t.left, ast.Name)
                and t.left.id == "__name__"
                and ast.unparse(t.comparators[0]) in ("'__main__'", '"__main__"'))

    def walk(body, in_main):
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            mine = in_main or is_main_guard(node)
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
                call = ast.unparse(node.value.func)
                if call in ("sys.exit", "os._exit", "exit", "quit") and not mine:
                    found.append(node.lineno)
            for field in ("body", "orelse", "finalbody"):
                sub = getattr(node, field, None)
                if isinstance(sub, list):
                    walk(sub, mine)
            for h in getattr(node, "handlers", []) or []:
                walk(h.body, mine)

    walk(tree.body, False)
    return found


def _collected_files():
    out = []
    for tok in _a2_paths():
        p = os.path.join(ROOT, tok.replace("/", os.sep))
        assert os.path.exists(p), "A2 里的路径不存在（打错一个字母 = 该闸静默不收任何东西）：%s" % tok
        if os.path.isdir(p):
            out += [os.path.join(p, f) for f in sorted(os.listdir(p))
                    if f.startswith("test_") and f.endswith(".py")]
        else:
            out.append(p)
    return out


def test_wired_paths_are_pytest_collectible():
    """接进 blocking 闸的文件不许在 import 期 exit：红了要的是可读的红，不是 INTERNALERROR。

    INTERNALERROR 会让整个 A2 步骤失败 ⇒ 后续构建与部署全部跳过（2026-10-01 09:00 实测：
    一场部署都没落地，站点数据从 08:19 起停更）。名单外的新文件一律判红。
    """
    rel = lambda p: os.path.relpath(p, ROOT).replace(os.sep, "/")
    risky = [rel(p) for p in _collected_files() if _module_level_exits(p)]
    fresh = sorted(set(risky) - KNOWN_FRAGILE)
    assert not fresh, (
        "这些文件被接进了 blocking 闸，却在模块级 sys.exit ⇒ 判据一红就是 INTERNALERROR + 冻部署：%s"
        % ", ".join(fresh))
    stale = sorted(KNOWN_FRAGILE - set(risky))
    assert not stale, (
        "KNOWN_FRAGILE 里这些已经不再有风险（改成 test 函数了？把名单划掉，别留假登记）：%s"
        % ", ".join(stale))
