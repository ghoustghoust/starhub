# -*- coding: utf-8 -*-
"""Pages 制品面收窄的变异电池：少一道过滤要红，过度收窄也要红。

被测正文来自 `STARHUB_UPDATE_YML` 指向的**副本**（判据自己支持这个口子，就地改 update.yml
会与推送互斥）。每个变异只动一行管道，然后跑 tests/site_nav_drift/test_pages_artifact_scope.py。
"""
import hashlib
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WF = os.path.join(ROOT, ".github", "workflows", "update.yml")
COPY = os.path.join(ROOT, ".deploy-tmp", "_mut_scope.yml")
TEST = os.path.join("tests", "site_nav_drift")   # 整目录：白名单的变异会打到不同判据上

# 白名单化之后锚点全部跟着换（2026-10-02 18:12）：旧那几条挡的是"排除表少一行"，
# 现在正文里已经没有排除表了 —— 留着它们电池只会报 INVALID 而不是红（那正是它该有的行为）。
# 锚点一律按**单行**定位，不写跨行字符串：跨行字面量在这个环境里被转义吃过两次。
# 锚点必须跟着发布清单走：10-05 那次给清单加了 trending_board.json，本行没同步 ⇒
# W1 长期打不上变异（10-08 复跑才暴露：锚点在 update.yml 里命中 0 次）。
# 失修的电池比没有电池更坏——它让人以为这一面有人守。
BY_NAME = "                   hot_snapshot.json rss_sources.json trending_board.json; do"
NULLGLOB = "          shopt -s nullglob"
HEAD_LOOP = ("          for f in index.html ai-daily.html rss-aggregator.html "
             "daily-insight-history.html")

MUTS = [
    # W1/W2 是同一枚硬币的两面：白名单少一项 = 站点发缺，多一项 = 垃圾公开。
    ("W1 白名单少一项（首页侧栏的数据源不发）", BY_NAME,
     "                   hot_snapshot.json; do",
     "test_site_files_are_still_published"),
    ("W2 白名单多一项（把构建期的 known_categories 发上线）", HEAD_LOOP,
     HEAD_LOOP + " known_categories.json",
     "test_published_set_is_exactly_the_allowlist"),
    # 去掉 nullglob 后 `for f in rss-data-*.js` 会拿字面模式去 cp，在 bash -e 下当场中止：
    # rc 非零但没有点名 —— 正是 ② 治的那类静默，靶判据在 test_pages_missing_report.py。
    ("W3 去掉 nullglob（分块全缺时字面模式撞 cp ⇒ 非零却没点名）", NULLGLOB,
     "          : # 没有 nullglob ⇒ glob 保留字面量",
     "test_a_missing_chunk_is_named_too"),
]


def run(k=None):
    cmd = [sys.executable, "-m", "pytest", TEST, "-q", "--no-header", "-p", "no:cacheprovider"]
    if k:
        cmd += ["-k", k]
    env = dict(os.environ, STARHUB_UPDATE_YML=COPY, PYTHONIOENCODING="utf-8")
    p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=env, timeout=900)
    lines = (p.stdout or "").strip().splitlines()
    # FAILED 之外还要收 ERROR：判据的 fixture 自己炸掉（比如 Stage 在合成树上就 rc≠0）
    # 同样是"这个变异被抓住了"。只数 FAILED 会把这种误记成"红了但不是自己的靶"。
    failed = sorted({l.split("::")[-1].split(" ")[0]
                     for l in lines
                     if l.startswith(("FAILED", "ERROR"))})
    return p.returncode != 0, (lines[-1] if lines else "?"), failed


if hasattr(sys.stdout, "reconfigure"):
    # 同 mut_star_state：本机 GBK 控制台下一个『⇒』就能让电池崩在 print 上，
    # 而 rc=1 看起来像「某个变式没挡住」——那是打印失败，不是判据失守。
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
src = open(WF, encoding="utf-8").read()
open(COPY, "w", encoding="utf-8", newline="").write(src)
red, tail, failed = run()
print("基线（未变异副本）：%s" % tail)
if red:
    print("基线不绿 ⇒ 不声称覆盖率；失败：%s" % failed)
    sys.exit(1)

bad = []
for label, a, b, target in MUTS:
    n = src.count(a)
    if n != 1:
        bad.append("%s INVALID（锚点 %d 次）" % (label, n))
        continue
    open(COPY, "w", encoding="utf-8", newline="").write(src.replace(a, b, 1))
    red, tail, failed = run()
    print("%-46s %s %s%s" % (label, "RED  " if red else "GREEN(判据漏了!)", tail[:22],
                            ("  <- " + ", ".join(failed)) if failed else ""))
    if not red:
        bad.append(label + "（没挡住）")
    elif target not in failed:
        bad.append("%s 红了但不是自己的靶（%s）" % (label, failed))
os.remove(COPY)

# 批 11 的另一半：`api/` 关出制品之后，页面里出现**同源** `/api/x` 调用就是 Pages 域 404。
# 这条前提判据（test_client_sources_make_no_same_origin_api_calls）也得能被变异打到，
# 否则它和红测输入之间又只剩下"我本地跑过一次"这种证据。
CLIENT = os.path.join(ROOT, ".deploy-tmp", "_mut_template.html")
SRC_TMPL = open(os.path.join(ROOT, "template.html"), encoding="utf-8", errors="replace").read()
OTHER = os.path.join(ROOT, "build_rss_aggregator.py")
K = "test_client_sources_make_no_same_origin_api_calls"

CASES2 = [
    ("C1 页面新增同源 /api/rss 调用（必须红）", SRC_TMPL + "\nfetch('/api/rss')\n", True),
    ("C2 外部域名的 /api/ 引用不该管（必须绿）",
     SRC_TMPL + "\nfetch('https://other.example/api/rss')\n", False),
]
for label, content, want_red in CASES2:
    open(CLIENT, "w", encoding="utf-8", errors="replace", newline="\n").write(content)
    env_backup = os.environ.get("STARHUB_CLIENT_SOURCES")
    os.environ["STARHUB_CLIENT_SOURCES"] = CLIENT + os.pathsep + OTHER
    try:
        red, tail, failed = run(k=K)
    finally:
        if env_backup is None:
            os.environ.pop("STARHUB_CLIENT_SOURCES", None)
        else:
            os.environ["STARHUB_CLIENT_SOURCES"] = env_backup
    ok = (red == want_red)
    print("%-46s %s %s%s" % (label, "RED  " if red else "GREEN", tail[:22],
                            "" if ok else "  <- 期望%s，判据识别有问题" % ("红" if want_red else "绿")))
    if not ok:
        bad.append(label + ("（没挡住）" if want_red else "（误伤：外部域名也被判红）"))
os.remove(CLIENT)

# 批 12 的另一半：白名单只发那 6 个名字 + 分块 ⇒ 页面请求一个名单外的名字就是线上 404，
# 而"两份手抄名单相等"那条天生看不见（缺的那项会两边一起缺）。这条判据要有自己的变异。
# 注意被测清单要**四份真生成端 + 一份合成副本**：只喂合成那份，`known <= refs` 那条
# 模式退化控制会先红，于是"红了"红的是不相干的断言（本仓在解析器上算过这类账）。
REAL_CLIENTS = [os.path.join(ROOT, p) for p in
                ("template.html", "build_rss_aggregator.py", "build_ai_daily.py", "build_daily_insight.py")]
K2 = "test_every_browser_referenced_name_is_published"
CASES3 = [
    ("R1 页面新增一个白名单外的浏览器请求（必须红）",
     SRC_TMPL + "\nvar q = fetch('secret_state.json');\n", True),
    ("R2 页面新增一个分块请求 rss-data-3.js（拼接形状，必须绿）",
     SRC_TMPL + "\nsc.src='rss-data-'+i+'.js?v='+BUILD_TS;\n", False),
    ("R3 外部域名的 .json 请求不该管（必须绿）",
     SRC_TMPL + "\nfetch('https://other.example/x.json')\n", False),
    # 语境扩到 CSS 之后要有自己的靶：只按 src/href/fetch 抽的话这条会绿（=漏），
    # 而"绿"在这里不是好消息 —— 白名单外的样式文件线上同样 404。
    ("R4 CSS url() 引一个白名单外的样式（必须红）",
     SRC_TMPL + "\n<style>.c{background:url(lib/cover.css)}</style>\n", True),
    ("R5 data: URI 与外部字体不该管（必须绿）",
     SRC_TMPL + "\n<style>.c{background:url(\"data:image/png;base64,iVBOR\")"
                ";font:url(https://cdn.example.com/a.woff2)}</style>\n", False),
    # 注释剥离的**那一半**：只在注释里出现的 href 例子不该把 A3 判红（判据按关键词读正文
    # 会被注释喂假读数，本仓在 _needs_rsync 上栽过）。与 R1 是同一段文本去掉 `//` 的两面。
    ("R6 注释里的 href 例子不该算浏览器请求（必须绿）",
     SRC_TMPL + "\n// 举例：<a href=\"secret_state.json\">demo</a>\n", False),
]
for label, content, want_red in CASES3:
    open(CLIENT, "w", encoding="utf-8", errors="replace", newline="\n").write(content)
    # 新判据要用 staged fixture（真跑 Stage 正文），所以 workflow 副本必须还在。
    open(COPY, "w", encoding="utf-8", newline="\n").write(src)
    backup = os.environ.get("STARHUB_CLIENT_SOURCES")
    os.environ["STARHUB_CLIENT_SOURCES"] = os.pathsep.join([CLIENT] + REAL_CLIENTS)
    try:
        red, tail, failed = run(k=K2)
    finally:
        if backup is None:
            os.environ.pop("STARHUB_CLIENT_SOURCES", None)
        else:
            os.environ["STARHUB_CLIENT_SOURCES"] = backup
    ok = (red == want_red)
    if want_red and ok and K2 not in failed:
        ok = False     # 红了但不是自己的靶
    print("%-46s %s %s%s" % (label, "RED  " if red else "GREEN", tail[:22],
                            "" if ok else "  <- 期望%s，判据有问题（红错对象也算）" % ("红" if want_red else "绿")))
    if not ok:
        bad.append(label + ("（没挡住/红错对象）" if want_red else "（误伤：合法请求也被判红）"))
os.remove(CLIENT)
os.remove(COPY)
print("问题条目：%s" % (bad if bad else "无"))
sys.exit(1 if bad else 0)
