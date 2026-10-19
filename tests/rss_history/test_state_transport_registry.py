# -*- coding: utf-8 -*-
"""判据 (e)：反向登记对账 —— 从源码自动发现"读回又写回"的文件，强制它必须有跨场通路。

为什么要有这条（批 5a 的学费）：`trending_snapshot.json` / `descriptions_zh.json` 是 批 5 关联分析
才发现的"读回上一次再写回"型缓存，而它们的跨场通道**只有 git checkout**（既不在缓存 path 也不在
.gitignore）。人肉盘点会漏，漏了就是"基线冻死而构建全绿"。所以把盘点本身做成判据。

第一版按 `*_FILE` 常量名分类，实测漏掉 `descriptions_zh.json` —— 它在 `fetch_and_build.py:722/813`
是**字面量**打开的，没有常量。所以现在按"文件名"解析调用点：参数位置允许 `Name`（查顶层常量表）
或 `Constant`（字面量），两条路径都算这个文件的使用点。

三桶互斥、每桶非空，并留一个控制项证明分桶不是橡皮图章：`source_quality.json` 只写不读，
若有人把它塞进缓存 path，`test_control_write_only_file_has_no_cache_home` 就红。

已知局限（写在这里而不是假装没有）：只认 `open()` 的模式参数、`os.path.exists/isfile/getmtime`
作为读、以及函数名含 write/dump/save 的调用作为写；`Path(x).write_text(...)`、
把文件名再拼一层目录（`os.path.join(dir, NAME)`）之类的间接形状暂时看不见。要扩先举一个
真会被漏掉的例子，别凭想象加规则。
"""
import ast
import fnmatch
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WF = os.environ.get("STARHUB_UPDATE_YML") or os.path.join(ROOT, ".github", "workflows", "update.yml")
SOURCES = ("build_rss_aggregator.py", "build_daily_insight.py", "fetch_and_build.py",
           "insight_engine.py", "build_logger.py")

STATE_EXT = (".json", ".jsonl")

# 显式豁免：每场重新生成、且没有跨场读回需求的名字。理由必须写在这里，否则豁免名单
# 会退化成一个谁都能塞的口袋（`test_exempt_list_is_justified_and_not_a_pocket` 逐条验）。
EXEMPT = {
    "ai-daily.html": "页面产物：每场由 template.html 重生成，发布走 Stage 的按名拷贝（批①）",
    "daily_insight_snapshot.json": "构建期抓取缓存：单场内使用，冷启动只多花一次抓取",
    "build_config.json": "CI 生成的一次性配置快照，无人读回",
    "rss_cache.json": "RSS 原始抓取语料（110 MB 级）：故意不进 git，缓存族按目录/通配承担",
    "rss_history.json": "72 小时语料：同上，由 rss-history 缓存族承担",
    "daily_insight_faiss.index": "Faiss 索引：由 emb-cache 缓存族承担",
    "daily_insight_vectors.npy": "向量缓存：由 emb-cache 缓存族承担",
    "emb_cache.json": "嵌入缓存：由 emb-cache 缓存族承担",
}


def _parse(rel):
    p = os.path.join(ROOT, rel)
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as fh:
        return ast.parse(fh.read())


def top_constants(tree):
    """模块顶层 `NAME = "裸文件名"` —— 用于把调用点的 Name 解析成文件名。"""
    out = {}
    if tree is None:
        return out
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for t in node.targets:
            if isinstance(t, ast.Name) and isinstance(node.value, ast.Constant) \
               and isinstance(node.value.value, str):
                out[t.id] = node.value.value
    return out


def _resolves_to(arg, consts, name):
    if isinstance(arg, ast.Name):
        base = os.path.basename(consts.get(arg.id, ""))
        return base == name
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return os.path.basename(arg.value) == name
    return False


def usages():
    """{文件名: (read, write, 出处)}，覆盖常量与字面量两种写法。"""
    found = {}
    for rel in SOURCES:
        tree = _parse(rel)
        if tree is None:
            continue
        consts = top_constants(tree)
        names = {os.path.basename(v) for v in consts.values() if v}
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if "." in node.value and os.path.basename(node.value).endswith(
                        STATE_EXT + (".js", ".html", ".npy", ".index")) and "/" not in node.value:
                    names.add(node.value)
        for name in names:
            if not name or name.startswith(".") or "${" in name:
                continue
            r = w = False
            for call in (n for n in ast.walk(tree) if isinstance(n, ast.Call)):
                fname = getattr(call.func, "id", None) or getattr(call.func, "attr", None) or ""
                args = call.args
                if fname == "open" and args and _resolves_to(args[0], consts, name):
                    mode = ""
                    if len(args) >= 2 and isinstance(args[1], ast.Constant):
                        mode = str(args[1].value)
                    for kw in call.keywords:
                        if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                            mode = str(kw.value.value)
                    if "w" in mode or "a" in mode:
                        w = True
                    else:
                        r = True
                elif fname in ("exists", "isfile", "isdir", "getmtime") and args \
                        and _resolves_to(args[0], consts, name):
                    r = True
                elif re.search(r"write|dump|save", fname) and any(
                        _resolves_to(a, consts, name) for a in args):
                    w = True
            cur = found.get(name, (False, False, set()))
            found[name] = (cur[0] or r, cur[1] or w, cur[2] | ({rel} if (r or w) else set()))
    return found


def _wf_text():
    return open(WF, encoding="utf-8").read()


def cache_entries():
    """所有 actions/cache 步的 path 条目原样收集（含目录与通配）。"""
    text = _wf_text()
    out = set()
    for m in re.finditer(r"^ {6,16}(\S+)$", text, re.M):
        tok = m.group(1)
        if tok.startswith("- "):
            tok = tok[2:]
        if "/" in tok or tok.endswith("/") or "*" in tok or tok.endswith(STATE_EXT):
            out.add(tok)
    return out


def cache_names():
    """缓存 path 里出现的**裸文件名**（不含目录/通配），用于孤儿检查。"""
    return {t for t in cache_entries() if "/" not in t and "*" not in t and not t.endswith("/")}


def add_list_names():
    names = set()
    for m in re.finditer(r"git add ([^\n;|&]+)", _wf_text()):
        for tok in m.group(1).split():
            if not tok.startswith("-"):
                names.add(os.path.basename(tok.rstrip("/")))
    return names


def _has_transport(name, adds, centries):
    if name in adds:
        return True
    for pat in centries:
        if pat.endswith("/") or "/" in pat:
            if fnmatch.fnmatch(name, os.path.basename(pat.rstrip("/"))) and name.endswith(STATE_EXT):
                # 目录项承担不了"裸文件名"，但语料族是靠通配进缓存的，这里交给下面通配分支
                continue
        if fnmatch.fnmatch(name, pat) or fnmatch.fnmatch(name, os.path.basename(pat)):
            return True
    return False


def test_every_read_and_written_state_file_has_a_transport():
    adds, centries = add_list_names(), cache_entries()
    use = usages()
    assert use, "一个文件使用点都没解析到 —— 抽取器失明，这条判据在空集合上跑"
    pairs = sorted(n for n, (r, w, _) in use.items() if r and w)
    assert len(pairs) >= 6, (
        "「读回又写回」的文件只有 %d 个（%s）—— 分类器退化，批 5a 之前实测至少 9 个"
        % (len(pairs), pairs))
    naked = [n for n in pairs if n not in EXEMPT and not _has_transport(n, adds, centries)]
    assert not naked, (
        "这些文件既被读回又被写回，却没有任何跨场通路（add 清单 / 缓存 path）；"
        "一旦停止提交就会静默冻基线：%s" % naked)


def test_exempt_list_is_justified_and_not_a_pocket():
    adds, centries = add_list_names(), cache_entries()
    thin = [n for n, why in EXEMPT.items() if not why or len(why) < 12]
    assert not thin, "这些豁免没写理由（或理由太短）：%s" % thin
    redundant = sorted(n for n in EXEMPT if _has_transport(n, adds, centries))
    assert not redundant, (
        "这些豁免项其实已经有通路了，豁免是多余的、只会掩盖真问题：%s" % redundant)
    unused = sorted(n for n in EXEMPT if n not in usages())
    assert not unused, (
        "这些豁免项在源码里根本没有使用点（过期条目会掩盖将来同名的真问题）：%s" % unused)


def test_control_write_only_file_has_no_cache_home():
    """控制项：`source_quality.json` 只写不读 ⇒ 它不该在缓存 path 里。

    若有人把它塞进缓存（看起来"更安全"），这条会红 —— 于是三桶的边界真在管事，
    而不是"凡是文件都往缓存里丢"。
    """
    use = usages()
    assert "source_quality.json" in use, "控制项 source_quality.json 不见了：反向守卫失去参照"
    r, w, _ = use["source_quality.json"]
    assert w and not r, "控制项形状变了（读=%s 写=%s）：它必须仍是「只写不读」" % (r, w)
    assert not _has_transport("source_quality.json", set(), cache_entries()), (
        "source_quality.json 被放进了缓存 path：只写不读的语料进缓存只会把无界增长搬到另一个存储")


def test_no_cache_entry_without_a_source_reference():
    """反向另一半：缓存 path 里的裸文件名必须真被源码引用（防止缓存了谁也不读的东西）。"""
    refs = set(usages())
    orphans = sorted(n for n in cache_names() if n not in refs and not n.endswith(".npy"))
    assert not orphans, (
        "这些名字在缓存 path 里、但源码没有任何读写点（缓存它们等于白花配额）：%s" % orphans)
