# -*- coding: utf-8 -*-
"""A3（advisory）：Vercel 部署包里不许有源码/文档/调试图——与 Pages 那条同源，但那是另一个主机。

背景（2026-10-02 20:29 现取）：Pages 那边 Stage 已翻成白名单（`cee84b1666`），
但 **Vercel 是 `npx vercel --prod` 直接从工作目录打包**，走的是 `.vercelignore`（排除式）。
41 个跟踪的仓库根文件里实测 **16 个仍 200**：`HANDOFF.md` 84,323 B、`CLAUDE.md` 4,895 B、
`REFRESH_VERIFICATION_REPORT.md`、`template.html` 116,509 B、`LICENSE`、`vendor_qrcode.min.js`、
`ai_daily.json`、`build_config.json`、`predictions.jsonl`、三张 `failed-run-364-*.png`、
`actions-step5-expanded.png`、两个 `daily-deep-*.json`、`rss_sources.json`、`index.html`。

三个口径上的讲究（都是本仓付过学费的地方）：
1. **"运行时需要的名字"从 `api/*.js` 的源码里推**，不另抄一份名单 —— 抄一份就必然与代码分叉
   （Pages 那条判据同理：`test_frontend_fetched_names_are_served` 也是从生成端抽）。
2. **匹配语义交给 `git check-ignore --no-index --exclude-from`**，不自己实现 gitignore 匹配器：
   手写的那份会把 `*.py`、`docs/`、锚定 `/` 这些形状判错，而判错的方向是"以为没覆盖"或"以为覆盖了"。
   `--no-index` 必需（默认先看索引，已跟踪文件一律答"不忽略"，正好漏掉要找的那一类）。
3. HTML 页面**不在本判据的射程内**：用户 2026-09-30 定案"入口就是 Pages，Vercel 那份 RSS 空壳保持不动"
   （见 memory: vercel-pages-hosting-split），所以根级 `.html` 是**刻意保留**的公开面，不是漏。
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
IGNORE = os.environ.get("STARHUB_VERCEL_IGNORE") or os.path.join(REPO_ROOT, ".vercelignore")

# 刻意保留的公开面（各有理由，不是"先想到的那一类"）：
#   · 站点页面：用户 2026-09-30 定案"入口就是 Pages，Vercel 那份 RSS 空壳保持不动"（memory: vercel-pages-hosting-split）
#   · `vercel.json`：Vercel 自己的部署配置，忽略它等于改部署行为，不在本判据射程内
# 注意别用"根级 *.html 一律保留"这种后缀规则：本仓的 HTML 页面已停止提交，
# 根级唯一被跟踪的 `.html` 就是**源码模板** `template.html` —— 后缀规则等于专门替它开门。
PUBLISHED_PAGES = {"index.html", "ai-daily.html", "rss-aggregator.html", "daily-insight-history.html"}
VERCEL_KEEP = PUBLISHED_PAGES | {"vercel.json"}

_CWD_NAME = re.compile(r"""join\(\s*process\.cwd\(\)\s*,\s*['"]([^'"]+)['"]""")
_REL_REQUIRE = re.compile(r"""require\(\s*['"]\.\./([^'"]+)['"]""")


def _runtime_needs():
    """从 Vercel 函数源码里抽出它运行时要读的文件名（含相对路径的末段目录）。

    `join(process.cwd(), 'x')` 是仓库根的文件；`require('../lib/y.js')` 要的是目录。
    两个都抽：漏了 `require` 那一半就会把 `lib/` 判成"该忽略"，而 `api/rss.js` 真的 require 它。
    """
    api_dir = os.path.join(REPO_ROOT, "api")
    names, dirs = set(), set()
    for fn in sorted(os.listdir(api_dir)):
        if not fn.endswith(".js"):
            continue
        text = open(os.path.join(api_dir, fn), encoding="utf-8", errors="replace").read()
        for m in _CWD_NAME.finditer(text):
            names.add(os.path.basename(m.group(1).strip("/")))
        for m in _REL_REQUIRE.finditer(text):
            parts = m.group(1).split("/")
            if len(parts) > 1:
                dirs.add(parts[0] + "/")
            names.add(os.path.basename(m.group(1)))
    assert names, "api/*.js 里一个运行时文件名都没抽到 ⇒ 抽取器失明，这条判据在空集合上跑"
    return names, dirs


def _tracked_paths():
    """全部跟踪路径，不只仓库根。

    第一版只数根级文件，于是漏掉了 `.github/workflows/update.yml` —— 那正是本仓在排除式名单上
    连漏三次（`.md` / `lib/` / 根级 junk）的同款毛病：**按"先想到的那一层"枚举就等于没枚举**。
    实测（2026-10-02 20:38）`starhub-refresh.vercel.app/.github/workflows/update.yml` 是 200 / 36,632 B，
    整条部署管线公开；而根级点文件 `.gitignore`/`.vercelignore` 反而 404 ⇒ 点目录会上传、点文件不会，
    不能按直觉合并处理。
    """
    out = subprocess.run(["git", "ls-files"], cwd=REPO_ROOT, capture_output=True,
                         text=True, encoding="utf-8", errors="replace", timeout=120)
    if out.returncode != 0:
        raise AssertionError("git ls-files 失败：%s" % (out.stderr or "")[:160])
    return [l.strip() for l in out.stdout.splitlines() if l.strip()]


def _covered_by_ignore(paths, ignore_file=None):
    """用 git 自己的 gitignore 语义判"这个路径会不会被 .vercelignore 排除"。

    为什么开一个临时仓：`git check-ignore` 没有 `--exclude-from`（那是 status/ls-files 的选项，
    第一版这么写当场 129），而直接在主仓里跑会同时吃到仓库自己的 `.gitignore` 与
    `core.excludesFile`（全局忽略）⇒ 命中集合会把".vercelignore 没盖住但 .gitignore 盖住了"
    误报成"已覆盖"，那是假绿。临时仓里 `.gitignore` 只等于我们喂进去的那份，并把
    `core.excludesFile` 指到一个空文件，两边都不串味。
    """
    src = ignore_file or IGNORE
    if not paths:
        return set()
    tmp = tempfile.mkdtemp(prefix="vercelignore-probe-")
    try:
        repo = os.path.join(tmp, "r")
        os.makedirs(repo)
        empty = os.path.join(tmp, "empty-excludes")
        open(empty, "w", encoding="utf-8").close()
        r_init = subprocess.run(["git", "init", "-q", "."], cwd=repo,
                                capture_output=True, text=True, timeout=120)
        assert r_init.returncode == 0, "临时仓建不起来：%s" % (r_init.stderr or "")[:200]
        shutil.copyfile(src, os.path.join(repo, ".gitignore"))
        # **必须去掉结尾斜杠再问**：`git check-ignore --stdin` 把 "api/" 解析成"目录 api + 空文件名"，
        # 而空行（gitignore 里的 no-op）恰好匹配空成分 ⇒ git 会回一条"被 .gitignore:47 命中"的假读数。
        # 2026-10-02 实测：问 `api/`、`lib/` 都"已被排除"，问 `api`、`lib` 则都不命中。
        asked = [p.rstrip("/") for p in paths]
        r = subprocess.run(["git", "-c", "core.excludesFile=" + empty,
                            "check-ignore", "--no-index", "--stdin"],
                           cwd=repo, input=("\n".join(asked) + "\n").encode("utf-8"),
                           capture_output=True, timeout=120)
        # stdin 走**字节**：Windows 上 text 模式会把喂进去的 \n 翻成 \r\n，
        # git 于是拿 "HANDOFF.md\r" 去判 ⇒ 一个都不命中（第一版就是这么假绿的）。
        out = (r.stdout or b"").decode("utf-8", "replace")
        err = (r.stderr or b"").decode("utf-8", "replace")
        if r.returncode not in (0, 1):
            raise AssertionError("git check-ignore 返回 %d：%s" % (r.returncode, err[:200]))
        return {l.strip().strip('"') for l in out.splitlines() if l.strip()}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# 运行时读到的名字要分两档，依据是**调用点自己有没有兜底**（现读 api/rss.js，不是猜）：
#   · 硬依赖：`loadSources()` 直接 `readFileSync(join(process.cwd(),'rss_sources.json'))` 且**没有 try/catch**
#     ⇒ 被 .vercelignore 盖住就是 `/api/rss` 当场 500。
#   · 软依赖：`loadSnapshot()` 外面包着 try/catch，注释写着 "Snapshot not found, falling back to live fetch"，
#     而 `.vercelignore:5` 本来就排它，实测 `starhub-refresh.vercel.app/rss_api_snapshot.json` **404**
#     ⇒ "不发"是设计，不是漏（`lib/` 那两个 require 也在 try/catch 里，但缺了会掉留存闸门与封面，
#     所以目录这一档仍按不许排除处理）。
HARD_NEEDS = {"rss_sources.json": "api/rss.js loadSources() 无 try/catch",
              # 2026-10-05 阅读器正文规范：api/rss.js:52 与 api/article.js:8 都是**顶层 require、无 try/catch**
              # （不像 rss_retention/rss_cover 那样有降级），缺件就是 ?source= / ?batch= / 现抓正文三条出口当场 500。
              "body_rules.js": "api/rss.js、api/article.js 顶层 require 无 try/catch ⇒ 缺件两接口全部 500"}
# 有 try/catch，但缺了不是"少个优化"而是**功能掉档**：`lib/rss_retention.js` 是运行时的第二道留存闸门
# （缺 ⇒ `?batch=`/`?source=` 现抓的旧文整批回来，2026-09-20 对抗审查 P0-2 那条），
# `lib/rss_cover.js` 是实时封面抽取（缺 ⇒ 新文章从出生就没封面）。加载失败会打 ::warning 并在响应头留痕，
# 但那是"出声的退化"，不是可以接受的状态 ⇒ 与硬依赖同权重，仍不许被排除。
GUARDED_CRITICAL = {"rss_retention.js": "try/catch + ::warning，缺了掉运行时留存闸门",
                    "rss_cover.js": "try/catch + ::warning，缺了实时条目没有封面"}
SOFT_EXEMPT = {"rss_api_snapshot.json": "loadSnapshot() 有 try/catch，实测线上 404 = 设计上不上传"}


def test_vercelignore_must_not_swallow_the_runtime_needs():
    """反向那道：keep 里的东西**不许**被 .vercelignore 盖住。

    没有这一条，判据只剩"该忽略的有没有漏"，而另一侧的错它看不见：谁把 `rss_sources.json` 或 `lib/`
    加进 `.vercelignore`，它们就从"该覆盖集合"里消失了（keep 是减法算出来的），判据照绿，
    而那个读取点没有兜底 ⇒ `/api/rss` 当场 500。
    """
    names, dirs = _runtime_needs()
    # 分类不许烂掉：抽取器新认出一个运行时读取点，就必须在这里归入三档之一，否则本判据会静默少守卫一个名字
    buckets = set(HARD_NEEDS) | set(GUARDED_CRITICAL) | set(SOFT_EXEMPT)
    unclassified = sorted(names - buckets)
    assert not unclassified, (
        "这些运行时读到的名字没归入硬/兜底关键/软依赖分类：%s ⇒ 请就地定档（读点有没有 try/catch、掉了损失什么）"
        % unclassified)
    assert buckets <= names, (
        "分类里有些名字抽取器已经认不到了：%s ⇒ 代码改过了，就地重读调用点" % sorted(buckets - names))
    # 问的是**真实跟踪路径**，不是裸目录名：gitignore 里 `lib/` 这类目录规则匹配的是 `lib/x.js`，
    # 问 `lib` 不会命中 ⇒ 第一版这么问，"把 lib/ 加进 .vercelignore"这个变异当场绿了（漏检）。
    must_protect = set(HARD_NEEDS) | set(GUARDED_CRITICAL)
    protect = sorted(p for p in _tracked_paths()
                     if p.startswith(tuple(sorted(dirs | {"api/"}))) or os.path.basename(p) in must_protect)
    assert len(protect) >= 5, (
        "要保护的运行时路径只有 %d 条：%s ⇒ 口径空了，这条等于没跑" % (len(protect), protect))
    covered = _covered_by_ignore(protect)
    assert not covered, (
        "这些是 Vercel 函数运行时要读的路径，被 .vercelignore 盖住了：%s"
        "（`rss_sources.json` 那档没有兜底 ⇒ /api/rss 会 500；`lib/` 那档会掉留存闸门与实时封面）"
        % sorted(covered)[:8])
    # 软依赖反向也要能说清：它现在确实被排除，且排除是有意的（若哪天改成需要上传，这条会提醒重新归档）
    assert _covered_by_ignore(sorted(SOFT_EXEMPT)) == set(SOFT_EXEMPT), (
        "软依赖 %s 现在不被排除了 ⇒ 分类要重读调用点确认（可能是加上了不带兜底的读取）"
        % sorted(SOFT_EXEMPT))


def test_runtime_needs_extractor_sees_both_shapes():
    """抽取器自己的双向控制：真引用要认，文档名不许被当成运行时依赖。"""
    names, dirs = _runtime_needs()
    assert "rss_sources.json" in names, (
        "api/rss.js 的 loadSources() 用 join(process.cwd(),'rss_sources.json') 且**没有 try/catch** ⇒ "
        "这条认不出就是把首页数据源判成'可忽略'，上线即 api/rss 500")
    assert "lib/" in dirs, "api/rss.js require('../lib/…') ⇒ 目录也要认，漏了会清空 Vercel 侧的 lib"
    assert "HANDOFF.md" not in names and "template.html" not in names, (
        "文档与 Pages 模板被当成运行时依赖 ⇒ 判据会把公开面当必需品放过")


def test_vercel_ignore_covers_the_non_site_root_files():
    names, dirs = _runtime_needs()
    # 两边不是同一个集合，所以**不验相等**：那份 PUBLISH 是"站点运行时全集"（含 `hot_snapshot.json`、
    # `rss_sources.json`），这边只是"Vercel 上刻意保留的页面"（`hot_snapshot.json` 早被 .vercelignore 排除，
    # `rss_sources.json` 是靠 api/rss.js 的运行时推导留下的，不属于"页面"）。
    # 有意义的关系是**子集**：Vercel 保留的每个页面都必须是已发布的站点页面，
    # 否则就是往 keep 里塞了一个谁都不认的名字（名单分叉的老形状）。
    other = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "test_pages_artifact_scope.py"), encoding="utf-8").read()
    j = other.index("PUBLISH = {")
    a3_pages = set(re.findall(r'"([^"]+)"', other[j:other.index("}", j)]))
    assert PUBLISHED_PAGES <= a3_pages, (
        "Vercel 保留的页面不都在站点运行时集合里：%s ⇒ keep 里混进了没人认的名字"
        % sorted(PUBLISHED_PAGES - a3_pages))
    tracked = _tracked_paths()
    assert tracked, "一个跟踪文件都没有？口径不成立，别把空集当'全部已覆盖'"
    # keep 的三半各有一个"必须看得见"的理由：
    #   · `api/`、`lib/` 与 api/*.js 运行时读到的名字 —— Vercel 的函数与它的数据源；
    #   · VERCEL_KEEP（四个站点页面 + vercel.json）—— 用户定案保留的空壳与部署配置；
    #   · `.vercel/` —— Vercel CLI 自己的项目 linkage，排除它等于赌 `vercel --prod` 还能认这个目录；
    #     而且实测 `starhub-refresh.vercel.app/.vercel/project.json` 是 **404**（不发），
    #     所以它既不需要公开面收窄、也承担不起被排除的后果。
    # 其余**每一条跟踪路径**都必须被 .vercelignore 盖住，包括 `.github/workflows/*.yml` 这种"没人会先想到"的一层。
    keep_prefix = ("api/", "lib/", ".vercel/")
    keep = set(names) | VERCEL_KEEP
    must = sorted(p for p in tracked
                  if not p.startswith(keep_prefix) and os.path.basename(p) not in keep)
    assert must, "该忽略的路径为空 ⇒ 判据在空集上自证，先查 keep 是不是把整棵树都放行了"
    assert ".vercel/project.json" not in must, ".vercel/ 是被刻意放行的（CLI linkage + 实测不发），别把它列进该忽略集合"
    assert any(p.startswith(".github/") for p in must), (
        "`.github/` 不在该忽略集合里 ⇒ 枚举又退回仓库根那一层了；实测它线上 200/36,632 B")
    assert "template.html" in must, (
        "`template.html` 是根级唯一被跟踪的 .html（四个页面 HTML 已停止提交）且线上实测 200/116,509 B —— "
        "它不在该忽略集合里说明 keep 又退化成后缀规则了")
    # 2026-10-14 用户裁定「GitHub 只放代码」：HANDOFF.md / CLAUDE.md 已退出索引，不在仓里就不可能
    # 随部署包公开，这比"在仓里但被忽略"更强。留这条是为了它们哪天被加回来时，不许悄悄漏在
    # 忽略集合之外（实测曾 200：HANDOFF.md 84,323 B / CLAUDE.md 4,8xx B）。
    for _doc in ("HANDOFF.md", "CLAUDE.md"):
        assert _doc not in tracked or _doc in must, (
            "%s 又回到索引却不在该忽略集合里 ⇒ 会随 Vercel 部署包公开" % _doc)
    covered = _covered_by_ignore(must)
    naked = sorted(set(must) - covered)
    assert not naked, (
        "这些跟踪路径会随 Vercel 部署包公开（.vercelignore 没盖住）：%s" % naked[:12])


def test_ignore_matcher_is_not_blind_or_loose(tmp_path):
    """`_covered_by_ignore` 自己要有正反案例：恒"已覆盖"会静默放行，恒"未覆盖"会把已排除的东西报成漏。

    第三向是隔离性：`ai_daily.json` 在**仓库真 .gitignore** 里是被排除的，
    合成 ignore 里没有它 ⇒ 若临时仓串吃到主仓的 .gitignore，这条会把它误判成"已覆盖"（假绿）。
    """
    ign = tmp_path / "vercelignore"
    ign.write_text("*.py\ndocs/\nknown_categories.json\n", encoding="utf-8")
    probe = ["HANDOFF.md", "fetch_and_build.py", "docs/a.md", "known_categories.json",
             "index.html", "ai_daily.json"]
    got = _covered_by_ignore(probe, str(ign))
    assert got == {"fetch_and_build.py", "docs/a.md", "known_categories.json"}, (
        "合成 ignore 的命中集合应当只有这三项，实得 %s ⇒ 匹配语义不对或临时仓串吃了主仓的 .gitignore"
        % sorted(got))
    assert "HANDOFF.md" in set(probe) - got and "index.html" in set(probe) - got, (
        "未匹配项判丢了 ⇒ 判据会把真漏报成已覆盖")
    assert "ai_daily.json" in set(probe) - got, (
        "它在主仓 .gitignore 里、不在合成的这份里 ⇒ 被命中说明临时仓没隔离干净，判据会假绿")
