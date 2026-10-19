# -*- coding: utf-8 -*-
"""A3（advisory）：Pages 缺件必须"点名 + 留痕"——在合成树上真跑 Stage 正文。

补的是 ②（commit f156bbff16）留的那个洞：那段改动治的是"Stage 以 rc=1 结束且一行输出都没有，
Upload/Deploy 被 step 级 if 挡住，界面还显示 success"（本仓 49 场实测 1 场，≈2%）。
② 落地时是靠一次性彩排脚本（.deploy-tmp/_rehearse_fix_v2.py / _mut_fix2_v3.py）验的，
那些不入库 ⇒ 谁把它改回旧形状，CI 里没有任何东西会红。这条判据就是把这个洞堵成长期防线。

红测输入用**重写**的旧形状（`OLD_SHAPE`），而不是历史备份：`.deploy-tmp/_update_yml.bak`
确实是 ② 之前的真身（"Pages 缺件" 命中 0 次），但它用 rsync，本机没有 rsync ⇒
拿它跑出的 rc≠0 混着"工具缺失"与"缺件静默"两种原因，证据会被读反（我第一次就是这么试的）。
重写版只依赖 `cp --parents`，有无 rsync 都成立。

要求写成三条而不是一条：
① 缺件时必须非零退出（不许"少一个文件也照样发"）；
② 必须逐个点名 `::error::Pages 缺件：<file>`（不许静默）；
③ 必须在 build_logs 落一条 `pages_missing`（"这一场没发布"要跨场可查，不能只活在一次 run 的屏幕上）。
另配两条控制：树完整时必须 rc=0 且不写记录；旧形状必须两条都挡不住（防本判据空转）。
"""
import json
import os
import re
import subprocess

import pytest

yaml = pytest.importorskip("yaml")

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WF = os.environ.get("STARHUB_UPDATE_YML") or os.path.join(ROOT, ".github", "workflows", "update.yml")

SITE = ["index.html", "ai-daily.html", "rss-aggregator.html", "daily-insight-history.html",
        "template.html", "rss-data-0.js", "rss-data-1.js", "hot_snapshot.json",
        "rss_sources.json", "trending_board.json", "known_categories.json"]


def _stage_body():
    with open(WF, encoding="utf-8") as fh:
        doc = yaml.safe_load(fh)
    for job in doc["jobs"].values():
        for step in job.get("steps", []):
            if step.get("name", "").startswith("Stage Pages site") and step.get("run"):
                return step["run"]
    raise AssertionError("找不到 Stage Pages site 步骤")


def _tree(tmp, drop=()):
    subprocess.run(["git", "init", "-q", "."], cwd=tmp, check=True)
    for k, v in (("user.name", "gate"), ("user.email", "g@example.invalid"),
                 ("commit.gpgsign", "false")):
        subprocess.run(["git", "config", k, v], cwd=tmp, check=True)
    for rel in SITE:
        if rel in drop:
            continue
        p = os.path.join(tmp, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(p) or tmp, exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("x" * 64)
    subprocess.run(["git", "add", "-f", "--", "."], cwd=tmp, check=True)
    subprocess.run(["git", "commit", "-qm", "c"], cwd=tmp, check=True)
    return tmp


def _run(tmp, body):
    r = subprocess.run(["bash", "-e", "-c", body], cwd=tmp, capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=300)
    return r.returncode, ((r.stdout or "") + (r.stderr or ""))


def _records(tmp):
    out = []
    logdir = os.path.join(tmp, "build_logs")
    if not os.path.isdir(logdir):
        return out
    for fn in os.listdir(logdir):
        if not fn.endswith(".jsonl"):
            continue
        with open(os.path.join(logdir, fn), encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if isinstance(rec, dict) and rec.get("type") == "pages_missing":
                    out.append(rec)
    return out


def test_missing_file_fails_and_names_it(tmp_path):
    """缺 index.html 时：必须非零退出，并且逐个点名。"""
    d = _tree(str(tmp_path), drop=("index.html",))
    rc, out = _run(d, _stage_body())
    assert rc != 0, "缺件还退出 0 ⇒ 会把缺文件的站点发上线（死链），而且日志一片祥和：%s" % out[-300:]
    assert "::error::Pages 缺件：index.html" in out, \
        "非零退出但没点名 ⇒ 正是 ② 要治的静默失败：%s" % out[-400:]


def test_missing_file_leaves_a_cross_build_record(tmp_path):
    """缺件必须写进 build_logs，且每个名字只列一次。

    "只列一次"这条不是洁癖：MISS 由两处累加——按名补件那步（文件根本不存在）与非空守卫那步
    （`_pages/x` 不存在或 0 字节）。同一个缺文件的名字会**两处都命中**，靠正文里那行 `case` 判重去掉。
    我为了"消除重复列表"顺手删过它一次，副作用是 0 字节件不再进 MISS ⇒ 空文件被当有效件发出去
    （正是要防的那类静默）。所以这里既断言不重复、也断言名字确实还在。
    """
    d = _tree(str(tmp_path), drop=("ai-daily.html",))
    rc, out = _run(d, _stage_body())
    assert rc != 0, out[-300:]
    recs = _records(d)
    assert recs, "没有 pages_missing 记录 ⇒ 这一场没发布在跨场日志里查不到：%s" % out[-300:]
    blob = json.dumps(recs, ensure_ascii=False)
    assert "ai-daily.html" in blob, "记录了缺件却没记到这个文件名：%s" % blob[:200]
    listed = " ".join(str(r.get("files", "")) for r in recs).split()
    assert len(listed) == len(set(listed)), \
        "同一个缺件被列了两遍（说明正文里的判重被删了，顺带会漏掉 0 字节件）：%s" % listed
    assert listed.count("ai-daily.html") == 1, listed


def test_complete_tree_stages_cleanly(tmp_path):
    """反向控制（不许"只要红就好"）：树完整时必须 rc=0，且不写任何 pages_missing 记录。"""
    d = _tree(str(tmp_path))
    rc, out = _run(d, _stage_body())
    assert rc == 0, "完整的树被判缺件 ⇒ 每场都会静默不发站点：%s" % out[-400:]
    assert not _records(d), "rc=0 却也写了 pages_missing ⇒ 这条读数从此不可信"
    assert os.path.isdir(os.path.join(d, "_pages"))


OLD_SHAPE = """mkdir -p _pages
git ls-files -z | grep -zv -E '(^|/)[._]' | xargs -0 -r cp --parents -t _pages/
for f in rss-data-*.js index.html ai-daily.html rss-aggregator.html daily-insight-history.html hot_snapshot.json; do
  [ -f "$f" ] && cp -f "$f" _pages/
done
test -s _pages/index.html
test -s _pages/rss-data-0.js
echo "Staged site size:"
du -sh _pages
"""


def test_old_shape_would_not_have_caught_this(tmp_path):
    """判据自己的反空转：② 之前的形状必须挡不住上面两条（否则这两条是在空转）。

    这里**重写**旧形状而不是读 `.deploy-tmp/_update_yml.bak`：那份备份是历史真身，
    但它用 rsync，而本机没有 rsync —— 拿它测出来的 rc≠0 是"工具缺失"不是"缺件静默"，
    证据会被读反（我第一次就是这么试的）。重写版只用 `cp`，在有无 rsync 的机器上都成立。
    """
    d = _tree(str(tmp_path), drop=("index.html",))
    rc, out = _run(d, OLD_SHAPE)
    assert rc != 0, "旧形状在缺件时反而退出 0 ⇒ 本控制不成立（说明合成树没建对）：%s" % out[-300:]
    assert "Pages 缺件" not in out, "旧形状竟然也能点名 ⇒ 上面那条断言不是 ② 的专属防线"
    assert not _records(d), "旧形状竟然也留了跨场记录 ⇒ 同上"


def test_old_shape_passes_on_a_complete_tree(tmp_path):
    """同一支旧正文在完整的树上必须 rc=0 —— 证明上一条的 rc≠0 是缺件造成的，不是写法跑不通。"""
    d = _tree(str(tmp_path))
    rc, out = _run(d, OLD_SHAPE)
    assert rc == 0, "旧形状连完整站点都过不了 ⇒ 合成树或正文有问题，上一条控制不作数：%s" % out[-300:]


def test_stage_body_is_parsed_not_assumed():
    """接线自证：抽取器确实从 workflow 里拿到了那段 shell（非空、含 git ls-files）。"""
    body = _stage_body()
    assert "git ls-files" in body, "抽出来的正文不像 Stage 正文：%r" % body[:120]
    assert len(body.splitlines()) >= 10, "正文只有 %d 行，疑似被截断" % len(body.splitlines())
    assert re.search(r"_pages", body), "正文里没有 _pages ⇒ 抽错了步骤"


def test_a_missing_chunk_is_named_too(tmp_path):
    """分块整族不存在时也必须点名，而不是靠 `cp` 失败把整步炸掉（那又是一次静默）。

    白名单里分块是 `shopt -s nullglob` + glob 循环补进来的。把 nullglob 去掉的话，
    `for f in rss-data-*.js` 会拿字面模式去 `cp`，在 `bash -e` 下当场中止 ——
    rc 是非零没错，但**没有点名**，正是 ② 要治的那种形状（读日志的人只能看到"进程退出码 1"）。
    这条判据就是那个变异的靶。
    """
    d = _tree(str(tmp_path), drop=("rss-data-0.js", "rss-data-1.js"))
    rc, out = _run(d, _stage_body())
    assert rc != 0, "分块全缺却退出 0 ⇒ 会发一份没有卡片数据的站点：%s" % out[-300:]
    assert "::error::Pages 缺件：rss-data-0.js" in out, (
        "非零退出但没点名分块（多半是 nullglob 被去掉了，字面模式撞 cp）：%s" % out[-420:])


def test_a_zero_byte_artifact_is_rejected_and_named(tmp_path):
    """存在但 0 字节必须当缺件处理 —— 守卫用 `-s` 而不是 `-e`，这条就是那半个字母的靶。

    来历：② 彩排时我为"消除重复列表"顺手删了累加，副作用正是 0 字节件被当有效件发出去；
    当时靠 R3 案例抓住，但**写进仓库的判据一直没有这一类**：把 `-s` 改成 `-e`，
    本文件其它六条全绿。⇒ 空制品发布 = 那个入口整页空白，比少发一场更糟。
    """
    d = _tree(str(tmp_path))
    with open(os.path.join(d, "ai-daily.html"), "w", encoding="utf-8") as fh:
        fh.write("")                      # 存在、但是空的
    rc, out = _run(d, _stage_body())
    assert rc != 0, "0 字节的 ai-daily.html 被当成有效件放行了 ⇒ 上线是一份空页面：%s" % out[-300:]
    assert "::error::Pages 缺件：ai-daily.html" in out, (
        "非零退出但没点名 0 字节件（多半是 `-s` 被换成 `-e`）：%s" % out[-420:])
    assert any(r.get("type") == "pages_missing" and "ai-daily.html" in str(r.get("files", ""))
               for r in _records(d)), "0 字节件没进跨场记录"
