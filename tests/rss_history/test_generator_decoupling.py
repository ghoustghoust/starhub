# -*- coding: utf-8 -*-
"""生成器解耦判据：非 star 数据的生成器不许挂在 `if stars_ok:` 下面。

起因（2026-10-01 17:00 场实测）：star API 限流时 `index.html` 与 `ai-daily.html` **当场不生成**，
而这两个页面已于 `7c08d02cea` 退出 git ⇒ 工作树没有兜底副本 ⇒ Stage Pages 步缺件后 `exit 1`
（且零输出）⇒ `Upload/Deploy Pages` 双双 skipped ⇒ 站点静默停在上一份产物，构建界面还是绿的。
AI 晨报的数据源是 AIHOT（自带 API→RSS→本地 json 三级回退），与 star 毫无关系；
RSS 聚合、每日洞察同理 ⇒ 它们本就不该被 star 拉取的成功与否门控。

打在**控制流归属**上（AST），不是 grep 字面量：把分支改名成 `if repos:` 也算违规，
所以判据按"是否存在以 `stars_ok` 为条件的 If 且其子树含该调用"来判，同时把
"调用必须还在（不许用删除调用的方式蒙绿）"钉成反向条件。
"""
import ast
import os

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FAB = os.path.join(ROOT, "fetch_and_build.py")

# 这些生成器的输入与 GitHub star 数据无关，因此不许被 stars_ok 门控
NOT_STAR_GATED = (
    "build_ai_daily.main",           # AI 晨报：AIHOT API → RSS → ai_daily.json
    "build_rss_aggregator.main",     # RSS 聚合：自有信源与缓存
    "build_daily_insight.main",      # 每日洞察：自有语料
)
# index.html 是 star 数据的产物，允许（且应该）被门控 —— 但它必须仍在那里
STAR_GATED_ALLOWED = "build_trending"


def _src():
    with open(FAB, encoding="utf-8") as fh:
        return fh.read()


def _dotted(node):
    f = node.func
    if isinstance(f, ast.Attribute):
        base = f.value
        owner = getattr(base, "id", None) or getattr(getattr(base, "value", None), "id", "")
        return (owner + "." if owner else "") + f.attr
    if isinstance(f, ast.Name):
        return f.id
    return ""


def _calls(tree):
    return [(_dotted(n), n.lineno) for n in ast.walk(tree) if isinstance(n, ast.Call)]


def _stars_gated_lines(tree):
    """返回所有"位于某个以 stars_ok 为条件的 If 子树内"的行号集合。"""
    gated = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.If) and getattr(n.test, "id", None) == "stars_ok":
            for m in ast.walk(n):
                if hasattr(m, "lineno"):
                    gated.add(m.lineno)
    return gated


def test_star_unrelated_generators_are_not_gated_by_stars():
    tree = ast.parse(_src())
    gated = _stars_gated_lines(tree)
    hits = [(name, ln) for name, ln in _calls(tree) if name in NOT_STAR_GATED and ln in gated]
    assert not hits, (
        "这些与 star 无关的生成器仍挂在 if stars_ok: 下面（限流场就不生成产物，Pages 会静默不发）：%s"
        % (hits,))


def test_the_generators_still_exist_and_are_still_called():
    """反向护栏：不许靠"干脆不调用了"来让上一条变绿。"""
    tree = ast.parse(_src())
    got = {name for name, _ in _calls(tree)}
    missing = [n for n in NOT_STAR_GATED if n not in got]
    assert not missing, "生成器调用被删掉了（那不是解耦，是砍功能）：%s" % (missing,)
    assert STAR_GATED_ALLOWED in got, "star 数据生产者不见了 ⇒ 本判据的前提已变，需要重写"


def test_probe_bites_on_a_coupled_synthetic_case():
    """反空转：同一个检查器必须能抓出人工构造的耦合形状。"""
    coupled = (
        "def main():\n"
        "    stars_ok = False\n"
        "    if stars_ok:\n"
        "        try:\n"
        "            import build_ai_daily\n"
        "            build_ai_daily.main()\n"
        "        except Exception as e:\n"
        "            print(e)\n"
    )
    decoupled = (
        "def main():\n"
        "    stars_ok = False\n"
        "    if stars_ok:\n"
        "        build_trending()\n"
        "    try:\n"
        "        import build_ai_daily\n"
        "        build_ai_daily.main()\n"
        "    except Exception as e:\n"
        "        print(e)\n"
    )
    g1 = _stars_gated_lines(ast.parse(coupled))
    assert any(ln in g1 for name, ln in _calls(ast.parse(coupled)) if name == "build_ai_daily.main"), \
        "检查器抓不到构造出的耦合 ⇒ 主判据是恒绿的安慰剂"
    g2 = _stars_gated_lines(ast.parse(decoupled))
    assert all(ln not in g2 for name, ln in _calls(ast.parse(decoupled))
               if name == "build_ai_daily.main"), "检查器把已解耦的形状也报成违规"
# ── 第二半：运行时输入也不许依赖 star 那一路的产物 ──────────────────────────
# 上面那组判据只回答"调用挂在哪棵子树下"。它证不了运行时的另一半：
# 若 build_ai_daily.py 去读 star 成功分支才写出的文件（index.html / known_categories.json /
# descriptions_zh.json），限流场里调用虽然在场外、照样缺输入
# ——而"限流场仍产出日报"至今没有近期自然样本，缺样本的断言就该由静态不变量兜住。
# 2026-10-08 实测的当前事实：日报侧真正的路径操作数只有 `ai-daily.html`（写）与
# `ai_daily.json`（RSS 失败时的兜底读，文件不存在就返回 None），都不在 star 子树的写出集合里。
#
# 入口可用环境变量指到副本（与电池约定一致，绝不对真实文件就地变异）：
#   STARHUB_BUILD_SRC → fetch_and_build.py    STARHUB_DAILY_SRC → build_ai_daily.py
BUILD_SRC = os.environ.get("STARHUB_BUILD_SRC") or os.path.join(ROOT, "fetch_and_build.py")
DAILY_SRC = os.environ.get("STARHUB_DAILY_SRC") or os.path.join(ROOT, "build_ai_daily.py")

# 只认"作为路径操作数出现"的字符串：open()/json.load() 的第一参数，以及
# os.path.exists/isfile/getsize/remove/stat 的第一参数（允许经模块级常量一跳）。
# 曾经踩过：日报模板里有 `<a href="index.html">`，字面量全扫会把它当依赖 ⇒ 交集假非空。
_PATH_FUNCS = ("open", "load", "exists", "isfile", "getsize", "remove", "stat")


def _call_name(node):
    f = node.func
    if isinstance(f, ast.Attribute):
        return f.attr
    if isinstance(f, ast.Name):
        return f.id
    return ""


def _consts(tree):
    """模块级 `NAME = "字面量"`：把 OUT / FALLBACK_SRC 这类变量解回文件名。"""
    out = {}
    for n in tree.body:
        if isinstance(n, ast.Assign) and isinstance(n.value, ast.Constant) and isinstance(n.value.value, str):
            for t in n.targets:
                if isinstance(t, ast.Name):
                    out[t.id] = os.path.basename(str(n.value.value).replace("\\", "/"))
    return out


def _basename(node, consts):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return os.path.basename(node.value.replace("\\", "/"))
    if isinstance(node, ast.Name) and node.id in consts:
        return consts[node.id]
    return ""


def _path_operand_names(tree, consts):
    out = set()
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call) or not n.args:
            continue
        if _call_name(n) not in _PATH_FUNCS:
            continue
        v = _basename(n.args[0], consts)
        if v:
            out.add(v)
    return out


def _star_written(tree, consts):
    """`if stars_ok:` 子树里被以写模式打开的文件名集合。"""
    out = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        if not isinstance(node.test, ast.Name) or node.test.id != "stars_ok":
            continue
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Call) or len(sub.args) < 2:
                continue
            if _call_name(sub) != "open":   # 裸 open() 与 io.open() 都要认，只认一种会静默空集
                continue
            mode = sub.args[1]
            if not (isinstance(mode, ast.Constant) and "w" in str(mode.value)):
                continue
            v = _basename(sub.args[0], consts)
            if v:
                out.add(v)
    return out


def test_daily_generator_reads_nothing_star_gated():
    with open(BUILD_SRC, encoding="utf-8") as fh:
        built_tree = ast.parse(fh.read(), filename=BUILD_SRC)
    written = _star_written(built_tree, _consts(built_tree))
    assert "index.html" in written, (
        "抽取器在真文件上都抓不到 index.html 了 ⇒ 下面那条不相交断言会退化成空集安慰剂"
        "（star 罩子里的写法变了，先更新这里再说）")
    with open(DAILY_SRC, encoding="utf-8") as fh:
        daily_tree = ast.parse(fh.read(), filename=DAILY_SRC)
    used = _path_operand_names(daily_tree, _consts(daily_tree))
    assert used, "日报侧一个路径操作数都没抓到 ⇒ 抽取器失明，这条判据恒绿没有意义"
    dep = sorted(used & written)
    assert not dep, (
        "build_ai_daily 读了 star 成功分支才写出的文件 %s ⇒ star 限流场里日报虽然被罩外调用，"
        "仍会缺输入（2026-10-01 17:00 那一类的另一半；日报侧当前集合=%s，star 侧=%s）"
        % (", ".join(dep), sorted(used), sorted(written)))


def test_disjointness_checker_catches_a_constructed_dependency():
    """反向控制：真实依赖必须被抓到，href 必须不被误抓，两种 open 形状都要认。"""
    coupled = (
        "OUT = 'known_notes.json'\n"
        "def main():\n"
        "    with open(OUT, encoding='utf-8') as f:\n"
        "        pass\n"
    )
    tree = ast.parse(coupled)
    assert "known_notes.json" in _path_operand_names(tree, _consts(tree)), \
        "抓不到经常量一跳的路径 ⇒ 主判据看不见真实依赖"

    href_only = (
        "def render():\n"
        "    return '<a href=\"index.html\">back</a>'\n"
    )
    tree2 = ast.parse(href_only)
    assert "index.html" not in _path_operand_names(tree2, _consts(tree2)), \
        "把模板里的 href 当成路径依赖 ⇒ 假阳性（2026-10-08 实测真踩过）"

    gated = (
        "def main():\n"
        "    stars_ok = True\n"
        "    if stars_ok:\n"
        "        open('index.html', 'w', encoding='utf-8').close()\n"
        "        io.open('known_categories.json', 'w').close()\n"
    )
    tree3 = ast.parse(gated)
    assert _star_written(tree3, _consts(tree3)) == {"index.html", "known_categories.json"}, \
        "写端抽取器只认某一种 open 形状 ⇒ 交集会因漏认而假绿"
