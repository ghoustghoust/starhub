"""Pages 发布接线的结构判据：大产物不得再进 git，且切换本身不得扩大公开面。

为什么钉这里而不是靠人记：仓库膨胀的唯一主因是每场把 55MB 的 rss-data-1.js 提交进 git
（实测远端全量历史 5.23 GiB，data-* 解包占 45.71 GB）。改由 Pages artifact 发布之后，
只要有人把分块加回 update.yml 的 add 清单，膨胀就静默回来，而构建照样绿。

被测文件路径可用 STARHUB_UPDATE_YML / STARHUB_VERCELIGNORE 覆盖，供变异体在副本上自证能红
（就地改 update.yml 会与推送互斥，见 feedback-mutation-push-mutex）。
"""
import fnmatch
import os
import re

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WF = os.environ.get("STARHUB_UPDATE_YML") or os.path.join(ROOT, ".github", "workflows", "update.yml")
IGN = os.environ.get("STARHUB_VERCELIGNORE") or os.path.join(ROOT, ".vercelignore")

# 分块里除 chunk 0 以外的任何一个：rss-data-1.js / rss-data-2.js / …
_CHUNK_GT0 = re.compile(r"rss-data-([1-9]\d*)\.js")
# 批 5b 的两组，按"前端是否真取用"分（实测：rss-aggregator.html:680/781 preload+script 取
# rss-data-0.js，:681/3407 preload+fetch 取 hot_snapshot.json；另两个名字在 48 个可发布文件里零命中）
SITE_ARTIFACTS = (          # 退出 git，但必须按名发布且各有 test -s
    "rss-data-0.js",        # 599,472 B/场
    "hot_snapshot.json",    #  62,936 B/场（首页侧栏 fetch 的数据源）
)
# Stage 的发布白名单（唯一口径）。A3 那边 `test_pages_artifact_scope.PUBLISH` 必须与它逐个相同
# —— 两处各写一份名单正是本仓反复付过学费的分叉源（翻译判据 5/14、解析器盲点两次都是这个形状）。
STAGE_ALLOWLIST = (
    "index.html",
    "ai-daily.html",
    "rss-aggregator.html",
    "daily-insight-history.html",
    "rss-data-*.js",
    "hot_snapshot.json",
    "rss_sources.json",
    "trending_board.json",
)

STATE_ONLY_ARTIFACTS = (    # 纯跨场态：退出 git、进缓存，且**不许**被发布
    "trending_snapshot.json",   # 14,399 B/场，星标增量基线（fetch_and_build.py:432/486）
    "descriptions_zh.json",     # 77,007 B，描述译文缓存（fetch_and_build.py:722/813）
    # 2026-10-02 树差实测：把 7 个状态文件与站点产物都摘完之后，**每场新增的那 1 个 blob 就是它**
    # （22,518~37,860 B/场，当日 growth 合计 0.627 MiB）。它不需要进缓存族：
    # build_daily_insight.py:4570 只在同场写、没有任何跨场读方；`.vercelignore:17` 早就排除了它；
    # 线上 index/ai-daily/rss-aggregator/daily-insight-history 四个页面里 "daily-insight.json" 命中 0 次
    # ⇒ 退出 git 不必修发布名单，反而**不许**进发布名单（进了只是扩大公开面）。
    # 也不顺手改缓存 path 清单：改清单=换族，前缀回退也会全落空（批 5a 因此冷启动烧过一次重译）。
    "daily-insight.json",
)


def _doc():
    with open(WF, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _steps():
    job = _doc()["jobs"]["update"]
    names = [s.get("name", "") for s in job["steps"]]
    return names, job["steps"]


def _runs():
    """所有 run 脚本的 (步骤名, 正文) —— 判据统计在正文里，不在 YAML 结构里。"""
    return [(s.get("name", ""), s.get("run") or "") for s in _steps()[1] if s.get("run")]


_SHELL_WORDS = {"then", "fi", "do", "done", "else"}


def _add_names(lines):
    """从若干 `git add` 行里抽出文件名。

    只取 `git add` 之后那段并按 `;` 断开：条件式 add 的整行是
    `if [ -f X ]; then git add X; fi`，按整行切会把 `];`/`then`/`fi` 当成文件名。
    """
    names = set()
    for ln in lines:
        tail = ln.split("git add", 1)[1] if "git add" in ln else ""
        for tok in tail.replace(";", " ").split():
            if tok.startswith("-") or tok in _SHELL_WORDS:
                continue
            names.add(os.path.basename(tok.rstrip("/").strip('"').strip("'")))
    return names


def test_add_names_parser_catches_the_conditional_form():
    """解析口径自己的判据：条件式 add 必须被看见（本轮实测它就是把 daily-insight.json 每场提交进去的那一行）。

    同时把"为什么行首匹配会瞎"钉成断言而不是注释：`re.match(r"\\s*git add\\b")` 对整行以 `if`
    开头的条件式返回 None —— 于是 `SITE_ARTIFACTS`/`STATE_ONLY_ARTIFACTS` 两组循环全部在空集上跑，
    2026-10-02 我把 daily-insight.json 加进名单时它就是**当场判绿**的（15 passed），
    换成 search 口径才报红。这条判据保证下次不会再退回去。
    """
    cond = '          if [ -f daily-insight.json ]; then git add daily-insight.json; fi'
    plain = "          git add known_categories.json rss_sources.json"
    assert _add_names([cond]) == {"daily-insight.json"}, _add_names([cond])
    assert _add_names([plain]) == {"known_categories.json", "rss_sources.json"}
    assert _add_names([cond, plain]) == {"daily-insight.json", "known_categories.json",
                                         "rss_sources.json"}
    # 旧口径的盲点本身也要断言（不是写给人看的注释）
    assert re.match(r"\s*git add\b", cond) is None, \
        "行首匹配现在却能看见条件式了 ⇒ 说明口径已变，本判据要跟着改，别留着当假证据"
    assert re.search(r"\bgit add\b", cond) is not None


def _add_lines():
    """抽所有 `git add` 行。**不能用行首匹配**：本轮实测 `if [ -f daily-insight.json ]; then git add …; fi`
    这种"条件式 add"整行以 `if` 开头，行首口径把它读成不存在 ⇒ 判据在明明每场提交的情况下报绿
    （SITE/STATE 两组断言全部空跑）。注释行要跳过，否则正文里提到"git add 清单"的说明会被当清单。
    """
    out = []
    for _, body in _runs():
        for line in body.splitlines():
            s = line.strip()
            if s.startswith("#"):
                continue
            if re.search(r"\bgit add\b", line):
                out.append(line)
    return out


def _any_add_text():
    """所有出现过 `git add` 的行，含 `if [ -f x ]; then git add x; fi` 这种一行的条件式。

    _add_lines() 只认行首，条件式会从指缝里漏掉 —— 把 HTML 塞进 `if ... git add` 同样能
    把膨胀请回来，所以这条判据必须看见全部 git add。
    """
    out = []
    for _, body in _runs():
        out += [l for l in body.splitlines() if re.search(r"\bgit add\b", l)]
    return out


# 4 个纯产物页面：同场生成、无人跨场读回，退出 git 后由 staging 按名字从工作目录取。
_PAGE_HTML = ("index.html", "ai-daily.html", "rss-aggregator.html",
              "daily-insight-history.html")


def test_no_page_html_is_committed():
    """4 个页面 HTML 不许再进任何 git add（含条件式）。它们在 update.yml 里曾占约 3.8MB/场。"""
    lines = _any_add_text()
    assert lines, "没找到任何 git add 行（update.yml 结构变了，本判据失去意义）"
    for name in _PAGE_HTML:
        hits = [ln.strip() for ln in lines if re.search(r"(^|\s)%s(\s|$)" % re.escape(name), ln)]
        assert not hits, "%s 被加回提交清单，每场 %s 会重新攒进 git 历史：%s" % (
            name, name, hits[0][:160])


def test_pages_html_ship_from_workdir_not_from_git():
    """staging 必须按名字把 4 个页面从工作目录补进制品，不能只靠 `git ls-files`。

    它们已停止提交，main 上那份是过期副本：只按 git 清单取文件 = 把旧页面发上线；
    而存量清理要把这些过期副本从历史里剔除，届时它们连"被跟踪"都不是。
    """
    stage = [b for n, b in _runs() if n.startswith("Stage Pages site")]
    assert stage, "没有 staging 步骤"
    body = stage[0]
    loop = [ln for ln in body.splitlines()
            if re.match(r"\s*for f in .*html.*; do", ln)]
    assert loop, "staging 不再按名字补页面 HTML，git 里的过期副本会被发上线：%s" % body
    listed = loop[0]
    for name in _PAGE_HTML:
        assert name in listed, "%s 不在页面补入的名单里：%s" % (name, listed.strip())
    assert 'cp -f "$f" _pages/' in body, "页面补入的那步被改动，_pages 里不会有新鲜页面：%s" % body


def _guarded_names(body):
    """staging 正文里"必须有内容"这条硬断言实际守到了哪些文件名。

    认两种写法，但**要求本身不放松**（名单仍须覆盖全部站点产物，缺一个就判红）：
      ① 字面 `test -s _pages/x`（旧形状，一条一行）；
      ② `for must in a b c; do … [ ! -s "_pages/$must" ] … done`（②缺件点名的新形状，
         每个缺件都 `::error::` 并写进 build_logs ⇒ 语义比 ① 更强）。
    只认 ① 的后果是：一次改进被自己的判据判红（2026-10-02 实测就是如此），
    而那等于用判据把实现钉死在最旧的写法上 —— 该收紧的是识别，不是要求。
    """
    names = set(re.findall(r"^\s*test -s _pages/(\S+)$", body, re.M))
    m = re.search(r"for must in ([^;]+); ?do([\s\S]{0,600}?)\ndone", body)
    if m and re.search(r'-s\s*"?_pages/\$must', m.group(2)):
        names.update(t for t in re.split(r"\s+", m.group(1).replace("\\", "")) if t)
    return names


def test_guarded_names_extractor_recognizes_both_shapes():
    """抽取器自己的判据：两种形状都抽得出、且都不像时必须是空集（不许把"没守卫"读成"有守卫"）。

    用例里的正文一律写成**解析后的形状**：`_runs()` 交出来的是 YAML 块标量，
    公共缩进已被 yaml 吃掉（这里若带 10 空格缩进，测的就不是判据真正会读到的东西）。
    """
    literal = "test -s _pages/index.html\ntest -s _pages/ai-daily.html\n"
    loop = ('for must in index.html ai-daily.html; do\n'
            'if [ ! -s "_pages/$must" ]; then\n'
            'echo "::error::Pages 缺件：$must"\n'
            'fi\n'
            'done\n')
    assert _guarded_names(literal) == {"index.html", "ai-daily.html"}
    assert _guarded_names(loop) == {"index.html", "ai-daily.html"}
    # 跨行续行（update.yml 里就是 `... index.html \` + 换行）也要抽得到，否则名单会被腰斩
    cont = ('for must in index.html \\\n          ai-daily.html; do\n'
            'if [ ! -s "_pages/$must" ]; then\nfi\ndone\n')
    assert _guarded_names(cont) == {"index.html", "ai-daily.html"}, _guarded_names(cont)
    assert _guarded_names("cp -f index.html _pages/\n") == set(), \
        "没有任何非空守卫却抽出了名字 ⇒ 抽取器在替实现圆场"
    # 只有循环、但循环里没有 -s 检查（例如退化成 echo）⇒ 必须抽不出名字
    fake = ('for must in index.html; do\n'
            'echo "$must"\n'
            'done\n')
    assert _guarded_names(fake) == set(), "把 echo 当成非空守卫了"


def test_staging_refuses_empty_artifact():
    """空制品必须在 staging 就红，而不是发布出一个空站点。

    页面 HTML 出仓之后，这条是"生成器没跑出来 = 不发空站点"的唯一防线：缺哪一个都必须
    当场红，而不是让 deploy-pages 把缺文件的那一份当成新站点替换上去（= 死链）。
    """
    stage = [b for n, b in _runs() if n.startswith("Stage Pages site")]
    assert stage, "没有 staging 步骤"
    guarded = _guarded_names(stage[0])
    assert guarded, "staging 里既没有字面 `test -s`，也没有带 -s 的 must 循环 ⇒ 空制品会被当成功发布"
    for must in ("rss-data-0.js", "rss-data-1.js",
                 "rss-aggregator.html", "index.html",
                 "ai-daily.html", "daily-insight-history.html"):
        assert must in guarded, "staging 缺少 %s 的非空断言，空制品会被当成功发布：%s" % (
            must, sorted(guarded))


def test_no_big_chunk_is_committed():
    """提交清单里不许出现任何 `rss-data-*.js` 分块（含 chunk 0）——发布走按名拷贝名单。

    只禁字面 `rss-data-*.js` 是不够的：显式写 `rss-data-1.js` 同样把 55MB 塞回历史，
    而按文件名过滤行会让 `git add -A` 整条绕过判据 —— 两个洞都在这条里堵掉。

    批 5b 翻转了这里最后那条：原判据"chunk 0 必须还在清单，否则 tests/rss_composite 读不到已入库数据"
    的理由**经实测是失真的 —— 没有任何测试读库里那份**：`tests/rss_composite/test_diverse_realdata.py:32`
    是 `os.path.join(ROOT, "rss-data-0.js")` 的普通工作目录读，而那个目录并未接进任何门禁步；
    `tools/trim_commit_guard.py:28` 的 `KEEP_ALWAYS` 只做路径名分类，从不打开文件。
    保留那条正向钉 = 每场把 0.6 MB 产物继续写进不可回收的历史，而它换不到任何测试覆盖。
    """
    lines = _add_lines()
    assert lines, "没找到任何 git add 行（update.yml 结构变了，本判据失去意义）"
    for ln in lines:
        assert not re.search(r"git add\b[^\n]*\s(-A|\.)\b", ln), (
            "出现 git add -A/.：会把 _pages 暂存目录与全部未 ignore 文件一起提交：%s" % ln)
    joined = "\n".join(lines)
    assert "rss-data-*" not in joined, (
        "分块通配被加回提交清单，每场 55MB 会重新攒进 git 历史")
    hits = _CHUNK_GT0.findall(joined)
    assert not hits, "分块 chunk%s 被加回提交清单，每场 55MB 会重新攒进 git 历史" % ",".join(hits)
    assert not any("rss-data-0.js" in ln for ln in lines), (
        "chunk 0 仍在提交清单：它每场重写（实测 599,472 B/场），而它的发布已由 Stage 的 "
        "`for f in rss-data-*.js` 按名从工作目录取，进 git 换不到任何读方")


def test_site_artifacts_are_published_not_committed():
    """批 5b：四个每场重写的站点产物必须"不在 add 清单、在发布名单、各有 test -s"三者同时成立。

    三个方向各挡一类事故：
      · 回到 add 清单 ⇒ 每场 0.72 MiB 的最后一处无界增长复活；
      · 发布名单漏名 ⇒ `git ls-files` 那批文件不再包含它们（已退出 git），Pages 上直接 404，
        其中 `hot_snapshot.json` 是首页侧栏 fetch 的数据源（rss-aggregator.html:3407），漏了就是空数据；
      · 少 `test -s` ⇒ 生成器某场没产出时，空制品会被当成功站点发布（= 全站空文件）。
    名单与 add 行都先断非空，禁止"两边都空所以判绿"。
    """
    lines = _add_lines()
    assert lines, "没解析到 git add 行"
    add_names = _add_names(lines)
    stage = [b for n, b in _runs() if n.startswith("Stage Pages site")]
    assert stage, "没有 Stage Pages site 步骤"
    body = stage[0]
    published = set(re.findall(r"^\s*for f in (.+); do$", body, re.M))
    all_names = set()
    for grp in published:
        all_names.update(grp.split())
    all_names.update(re.findall(r"cp -f (\S+) _pages/", body))
    checks = _guarded_names(body)
    assert published and checks, "发布名单(%d)或非空守卫(%d) 为空 —— 判据在空集合上跑" % (
        len(published), len(checks))

    def _covered(name):
        # shell 的 `for f in rss-data-*.js` 会展开出 rss-data-0.js，所以按模式匹配判"是否被发布"，
        # 否则判据会把真实存在的覆盖读成漏项（反过来也一样：写死字面名会漏掉 glob 的覆盖）。
        return any(fnmatch.fnmatch(name, tok) for tok in all_names)

    for name in SITE_ARTIFACTS:
        assert name not in add_names, "%s 又回到提交清单：每场重写重新进历史" % name
        assert _covered(name), "%s 不在按名发布名单：它已退出 git，ls-files 不会再带它上线" % name
        assert name in checks, "%s 缺非空守卫：空文件会被当成功发布" % name
    for name in STATE_ONLY_ARTIFACTS:
        assert name not in add_names, "%s 又回到提交清单：跨场累积改由缓存承担后它不必入库" % name
        assert not _covered(name), (
            "%s 被放进发布名单：它没有任何前端读方（48 个可发布文件零命中），上线只会扩大公开面" % name)


def test_frontend_fetched_names_are_served():
    """计划判据 (g) 的第二半：生成端往页面里塞的每个 `fetch('x.json')`，都必须有上线的来源。

    为什么从**生成端源码**抽而不是读 `rss-aggregator.html`：A2 跑在 `Fetch stars & build` 之前，
    那时产物还不存在（在 CI 里读产物 = 判据读上一场的旧文件，正是本仓反复踩过的"代理信号绿"）。
    注意源码里那些 JS 是写在 Python 字符串里的，引号被转义成 `\\'`，所以模式要能吃掉反斜杠 ——
    第一版没吃，`hot_snapshot.json` 就直接漏了。

    允许的来源**只有 Stage 的白名单**这一种（2026-10-02 收紧）：白名单化之后"还在 `git add` 清单里"
    不再等于"会被发布" —— `git ls-files` 那条管道已经没有了，跟踪中的 `build_config.json` /
    `known_categories.json` 就是现取的反例（跟踪中、被构建期读、但不上线）。
    **"库里有份冻结副本"同样不算来源** —— 那正是 批 3 清掉的形状：
    文件不再更新，页面却每场都拿旧数据当新的。
    """
    gen = os.path.join(ROOT, "build_rss_aggregator.py")
    src = open(gen, encoding="utf-8", errors="replace").read()
    fetched = set(re.findall(r"fetch\(\s*\\?['\"]([A-Za-z0-9_.\-]+\.(?:json|js))\\?['\"]", src))
    fetched |= {os.path.basename(p) for p in
                re.findall(r"fetch\(\s*\\?['\"][^'\"]*?/([A-Za-z0-9_.\-]+\.json)\\?['\"]", src)}
    assert fetched, "生成端一个 fetch 目标都没抽到 —— 抽取器失明，这条判据在空集合上跑"
    known = {"hot_snapshot.json", "rss_sources.json"}
    assert known <= fetched, (
        "抽取结果 %s 少了 %s —— 模式退化（这两个是实测在前端 fetch 的名字，"
        "hot_snapshot 那份 JS 写在 Python 字符串里、引号是转义的，最容易漏抽）"
        % (sorted(fetched), sorted(known - fetched)))

    # 共用 _add_lines() + _add_names() 这一个口径：这里原来另写了一份行首匹配，
    # 与 _add_lines 修过的盲点是同一类（条件式 add 整行以 if 开头，行首匹配读不到）。
    # 两处各写一份解析器 = 一处修好、另一处继续瞎，且两边读数会互相矛盾。
    adds = _add_names(_add_lines())
    stage = [b for n, b in _runs() if n.startswith("Stage Pages site")]
    assert stage, "没有 Stage Pages site 步骤"
    pub = set()
    for grp in re.findall(r"^\s*for f in (.+); do$", stage[0], re.M):
        pub.update(grp.split())
    pub.update(os.path.basename(p) for p in re.findall(r"cp -f (\S+) _pages/", stage[0]))
    assert adds and pub, "add 清单(%d)或发布名单(%d) 为空 —— 判据在空集合上跑" % (len(adds), len(pub))

    naked = sorted(n for n in fetched
                   if not any(fnmatch.fnmatch(n, p) for p in pub))
    assert not naked, (
        "这些名字被前端 fetch，却不在 Stage 的白名单里 ⇒ 上线就是 404/空数据：%s"
        % naked)


# 被有意"发布但不提交"的东西（批① 四个 HTML、批 5b 的分块与侧栏快照）：
# 它们的 git 副本是过期产物，正因为如此才从提交清单里摘掉、改由工作目录取新鲜的一份。
PUBLISHED_NOT_COMMITTED = {
    "index.html", "ai-daily.html", "rss-aggregator.html", "daily-insight-history.html",
    "rss-data-0.js", "rss-data-1.js", "hot_snapshot.json",
}


def test_tracked_published_names_stay_in_both_add_lists():
    """既被 Stage 发布、又是仓库跟踪输入的件，必须留在**两条** git add 清单里。

    白名单化之后，Stage 是从工作目录取文件的，所以"摘掉 add"不会再让站点 404 ——
    但它会让 git 里那份与线上那份**长期不一致**（构建端会改写它，比如 fetch_and_build
    往里追加新信源）。这种漂移最难查：站点看着是对的，仓库里那份却停在旧内容，
    任何从 GitHub/raw 读它的人拿到的是过期数据。
    PUBLISHED_NOT_COMMITTED 是显式豁免（那些件的 git 副本本来就是过期产物，故意不更新）；
    豁免之外的每一项都必须同时出现在主路径与重试分支两条清单里 —— 只摘一条是最常见的错法。
    """
    stage = [b for n, b in _runs() if n.startswith("Stage Pages site")]
    assert stage, "没有 staging 步骤"
    toks = set()
    for grp in re.findall(r"^\s*for f in (.+); do$", stage[0], re.M):
        toks.update(grp.split())
    toks.update(os.path.basename(x) for x in re.findall(r"cp -f (\S+) _pages/", stage[0]))
    toks = {t for t in toks if "$" not in t and "*" not in t}
    assert toks, "抽不出发布名 ⇒ 本判据在空集合上跑"
    tracked_published = {t for t in toks if os.path.exists(os.path.join(ROOT, t))} - PUBLISHED_NOT_COMMITTED
    assert tracked_published, "一个'被发布且仓库里有副本'的名字都没认出 ⇒ 抽取器失明"
    # 注意：_runs() 给的是 YAML 解析后的正文（已去掉 workflow 那 10 空格缩进），
    # 所以这里按"行首可选空白"匹配，不能照抄电池里针对**原始文件**的 `^          git add`。
    lines = [ln for ln in _add_lines()
             if re.match(r"^git add \S+(?: \S+){3,}$", ln.lstrip())]
    assert lines, "抽不出任何一条多操作数 git add 清单 ⇒ 本判据在空集合上跑"
    names = _add_names(lines)
    missing = [n for n in sorted(tracked_published)
               if not all(n in re.split(r"\s+", ln.split("git add", 1)[1]) for ln in lines)]
    assert not missing, (
        "这些件在 git 里是构建端会改写的跟踪输入，却没同时留在**每一条** add 清单里：%s"
        " ⇒ 线上会拿工作目录的新内容，仓库里那份从此停在旧数据（清单实测 %d 条：%s）"
        % (missing, len(lines), sorted(names)[:3]))


def _code_lines(body):
    """staging 正文里的**命令行**（剔掉注释）。

    为什么必须剔：2026-10-02 这条判据差点被我的注释骗过去 —— 收窄之后 rsync 调用已经没了，
    但注释里写了"rsync 的 --files-from 反而绕"，于是 `"--files-from" in body` 与
    `"rsync" in ln` 双双"巧合成立"，判据看着绿、其实什么都没看。
    判据读的是行为，不能读关键词。
    """
    out = []
    for ln in body.splitlines():
        s = ln.strip()
        if s and not s.startswith("#"):
            out.append(s)
    return out


def _staging_copy_lines(body):
    """所有往 `_pages` 里拷文件的命令行（rsync 或 cp 都算）。"""
    return [ln for ln in _code_lines(body)
            if "_pages" in ln and re.search(r"\b(rsync|cp)\b", ln)]


def test_staging_copy_lines_probe_bites():
    """抽取器自己的判据：两种合法写法都要抽到，两种整拷写法也都要抽到（由主判据去拒）。"""
    ok_rsync = "git ls-files -z | grep -zv -E '(^|/)[._]' | rsync -a --files-from=- --from0 ./ _pages/"
    ok_cp = "git ls-files -z | grep -zv '\\.py$' | xargs -0 -r cp --parents -t _pages/"
    bad_rsync = "rsync -a --exclude _pages ./ _pages/"
    bad_cp = "cp -r . _pages"
    body = "# rsync 的 --files-from 反而绕\n%s\n%s\n%s\n%s\n" % (ok_rsync, ok_cp, bad_rsync, bad_cp)
    got = _staging_copy_lines(body)
    assert len(got) == 4, "抽到的拷贝行不对（注释漏进来或漏抽）：%s" % got
    assert _listings_from_git_tree(ok_rsync) and _listings_from_git_tree(ok_cp), "合法写法被拒"
    assert not _listings_from_git_tree(bad_rsync), "rsync 整拷没被拒"
    assert not _listings_from_git_tree(bad_cp), "cp -r 整拷没被拒"
    assert not _listings_from_git_tree("# cp -r . _pages 这种写法很危险"), "注释被当成命令行拒了"


def _listings_from_git_tree(copy_line):
    """这一行拷贝是不是"由 git 清单喂出来"的（而不是整拷工作目录）。"""
    if "rsync" in copy_line:
        return "--files-from" in copy_line
    if "cp" in copy_line:
        # cp 的合法形状：`xargs -0 -r cp --parents -t _pages/`（名单来自 stdin 的 git ls-files）
        # 非法形状：`cp -r . _pages` / `cp -a ./ _pages/`（整拷，会带出被 ignore 的语料）
        if re.search(r"\bcp\s+(-[a-zA-Z]*[ra][a-zA-Z]*\s+|-r\b|-a\b)", copy_line):
            return False
        return "cp --parents" in copy_line or re.search(r"\bcp\s+-f\b", copy_line)
    return False


def test_staging_publishes_only_the_site_allowlist():
    """staging 只许发"站点运行时白名单"，白名单之外一律不发 —— 钉的是集合，不是取文件的手法。

    前身是 `test_staging_takes_the_git_tree_not_the_worktree`（钉"清单必须来自 `git ls-files`"）。
    2026-10-02 18:12 全量枚举证明那条不变量**保不住它的初衷**：按"git 树 − 排除表"跑出来的公开集
    仍有 15 项／0.48 MiB，里面是 4 张调试截图、两个过期 `daily-deep-*.json`、`build_config.json`、
    `predictions.jsonl`、`vercel.json`/`package.json`/`LICENSE`/`known_categories.json`/
    `vendor_qrcode.min.js` —— 排除表只能挡"先想到的那一类"。于是设计翻成白名单（用户裁决），
    本判据跟着把靶从"手法"换成"结果"：**发布集合必须是白名单的子集，且不许出现 `.`/`_` 开头项**。
    它真正防的那件事（工作目录里有 cache restore-keys 放回、被 .gitignore 排除的 398 MB 语料：
    rss_cache 110M / rss_history 62M / rss_api_snapshot 41M / daily_insight_* 146M）现在由
    `test_staging_never_copies_the_worktree_wholesale` 那条继续钉 —— 两条都在 A2 里。
    """
    stage = [b for n, b in _runs() if n.startswith("Stage Pages site")]
    assert stage, "没有 staging 步骤"
    body = stage[0]
    code = _code_lines(body)
    assert not any("git ls-files" in ln for ln in code), (
        "staging 又回到「git 树 - 排除表」的取法：白名单之外的一切会静默重新公开")
    toks = set()
    for grp in re.findall(r"^\s*for f in (.+); do$", body, re.M):
        toks.update(grp.split())
    toks.update(os.path.basename(p) for p in re.findall(r"cp -f (\S+) _pages/", body))
    # `cp -f "$f" _pages/` 是循环体里的变量，不是发布名：留它会以"白名单之外的名字"假红。
    toks = {t for t in toks if "$" not in t and not t.startswith("`")}
    assert toks, "抽不出任何发布名 ⇒ 本判据在空集合上跑"
    stray = sorted(t for t in toks if t not in STAGE_ALLOWLIST)
    assert not stray, (
        "Stage 里出现了白名单之外的名字 %s：加名字之前先证明前端真的 fetch 它"
        "（判据 test_frontend_fetched_names_are_served 只认 Stage 名单这一个来源）" % stray)
    missing = sorted(t for t in STAGE_ALLOWLIST if not any(fnmatch.fnmatch(t, k) for k in toks))
    assert not missing, "白名单里的东西没在正文里发出去：%s ⇒ 某个入口会缺数据" % missing
    for t in toks:
        assert not t.startswith((".", "_")), (
            "发布名以 `.`/`_` 开头会让公开面比今天更宽：%s" % t)
    # 名单只许有一份。A3 的 `PUBLISH` 与本文件的 `STAGE_ALLOWLIST` 必须逐个相同 ——
    # 两处各写一份早晚分叉（本仓在翻译判据 Py/JS 两边、解析器两份实现上都付过这个学费）。
    other = open(os.path.join(ROOT, "tests", "site_nav_drift",
                              "test_pages_artifact_scope.py"), encoding="utf-8").read()
    j = other.index("PUBLISH = {")
    a3 = set(re.findall(r'"([^"]+)"', other[j:other.index("}", j)]))
    assert a3 | {"rss-data-*.js"} == set(STAGE_ALLOWLIST), (
        "A2 与 A3 的发布白名单分叉了：A3=%s A2=%s" % (sorted(a3), sorted(STAGE_ALLOWLIST)))


def test_staging_never_copies_the_worktree_wholesale():
    """往 _pages 里拷文件只许按名字单拷；出现递归整拷就是把被 ignore 的语料公开发布。

    页面 HTML 出仓之后，staging 里多了两个"按名字补文件"的循环，这正是最容易顺手写成
    `cp -rf . _pages` 的地方 —— 而 rsync 那条判据看不见 cp。被 .gitignore 排除、由
    cache restore-keys 放回磁盘的语料（实测本地合计 398M）会第一次进 Pages。
    """
    stage = [b for n, b in _runs() if n.startswith("Stage Pages site")]
    assert stage, "没有 staging 步骤"
    bad = [ln.strip() for ln in stage[0].splitlines()
           if re.search(r"\bcp\s+(-[a-zA-Z]*[ra][a-zA-Z]*\s+|-r\b|-a\b)", ln)
           and '_pages' in ln and '--files-from' not in ln
           and not re.search(r'cp\s+-f\s+"\$f"\s+_pages/', ln)]
    assert not bad, "staging 里有递归整拷进 _pages 的写法，会把被 gitignore 的语料发布出去：%s" % bad


def test_chunks_ship_without_being_tracked():
    """分块必须按名字模式进制品，不能只靠 `git ls-files`。

    chunk1+ 自 2026-09-30 起不再提交，main 上那份是停在旧流程的过期副本：
    只按 git 清单取文件 = 把过期块发上线；而存量清理要把这些过期副本从历史里剔除，
    届时它们连"被跟踪"都不是，页面会直接少一块。
    """
    stage = [b for n, b in _runs() if n.startswith("Stage Pages site")]
    assert stage, "没有 staging 步骤"
    body = stage[0]
    assert re.search(r"for f in rss-data-\*\.js", body), (
        "staging 不再按模式补分块，剔除历史里的过期副本后页面会缺块：%s" % body)
    assert 'cp -f "$f" _pages/' in body, "模式补块的那步被改动，_pages 里不会有新鲜 chunk1：%s" % body


def test_pages_steps_present_and_cannot_block_vercel():
    """Pages 两步必须在，且排在 Vercel 之后 —— Pages 失败不许连坐 Vercel 部署。"""
    names, steps = _steps()
    assert any(n.startswith("Upload Pages artifact") for n in names), names
    assert any(n.startswith("Deploy to GitHub Pages") for n in names), names
    i_vercel = [i for i, n in enumerate(names) if n == "Deploy to Vercel"]
    i_pages = [i for i, n in enumerate(names) if n.startswith("Upload Pages artifact")]
    assert i_vercel and i_pages, "缺少 Vercel 或 Pages 步骤"
    assert i_pages[0] > i_vercel[0], "Pages 发布排到了 Vercel 之前，它一红就会跳过 Vercel 部署"
    deploy = [s for s in steps if (s.get("name") or "").startswith("Deploy to GitHub Pages")][0]
    assert "steps.pages_upload.outcome" in (deploy.get("if") or ""), (
        "deploy 步不检查 upload 结果，upload 失败照样跑 deploy：%s" % deploy.get("if"))


def test_upload_fails_on_empty_artifact():
    """actions/upload-pages-artifact 默认 if-no-files-found=warn —— 不显式设 error 就是空发布。"""
    _, steps = _steps()
    up = [s for s in steps if (s.get("name") or "").startswith("Upload Pages artifact")]
    assert up, "没有 upload 步骤"
    assert (up[0].get("with") or {}).get("if-no-files-found") == "error", up[0].get("with")


def test_staging_runs_before_prune():
    """staging 必须早于 prune：prune 会把大产物从工作目录删掉，晚一步制品就是空的。"""
    names, _ = _steps()
    i_stage = [i for i, n in enumerate(names) if n.startswith("Stage Pages site")]
    i_prune = [i for i, n in enumerate(names) if n.startswith("Prune large build artifacts")]
    assert i_stage and i_prune, "缺少 staging 或 prune 步骤"
    assert i_stage[0] < i_prune[0], "staging 跑在 prune 之后，Pages 制品里不会有大产物"


def test_pages_permissions_granted():
    perms = _doc()["permissions"]
    assert perms.get("pages") == "write", perms
    assert perms.get("id-token") == "write", "缺 id-token 时 deploy-pages 拿不到 OIDC 令牌"


def test_staging_cannot_block_vercel():
    """staging 排在 Vercel 之前（prune 会删产物，没得选），所以它必须不能连坐 Vercel。

    两条一起才成立：staging 设 continue-on-error（失败不阻断后续），
    同时 upload 依赖 steps.stage.outcome（否则残缺的 _pages 会被照常发布，站点半坏）。
    """
    _, steps = _steps()
    names = [s.get("name", "") for s in steps]
    i_vercel = names.index("Deploy to Vercel")
    stage = [s for s in steps if (s.get("name") or "").startswith("Stage Pages site")][0]
    assert stage.get("continue-on-error") is True, (
        "staging 排在 Vercel 之前却没有 continue-on-error，它一红就跳过 Vercel 部署")
    assert stage.get("id"), "staging 没有 id，后面的 if 无法引用它的 outcome"
    i_stage = names.index(stage.get("name"))
    assert i_stage < i_vercel, "staging 必须在 prune/Vercel 之前，否则制品里没有大产物"
    up = [s for s in steps if (s.get("name") or "").startswith("Upload Pages artifact")][0]
    assert "steps.%s.outcome" % stage["id"] in (up.get("if") or ""), (
        "upload 不检查 staging 结果，残缺制品会被发布：%s" % up.get("if"))


def test_staging_dir_is_hidden_from_vercel():
    """_pages/ 必须在 .vercelignore 里：它是产物的全量副本，且活到 Vercel 部署那一步。

    prune 步骤删掉的正是那些大文件，但 staging 跑在 prune 之前，副本已经落进 _pages ——
    少了这一行 ignore，prune 的体积保护等于没做，会重新撞上它注释里写的上传超限。
    """
    with open(IGN, encoding="utf-8") as fh:
        lines = [l.strip() for l in fh if not l.strip().startswith("#")]
    assert "_pages/" in lines or "_pages" in lines, (
        ".vercelignore 里没有 _pages/，Vercel 会把 Pages 暂存目录整个打包：%s" % lines)
