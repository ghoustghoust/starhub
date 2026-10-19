# -*- coding: utf-8 -*-
"""批 5b 变异电池：站点产物"退出 git、按名发布、各有 test -s"这三条接缝能不能被捅穿。

用法：python tools/mut_site_artifacts.py
判据文件读 STARHUB_UPDATE_YML，所以变异体写在临时副本上，绝不就地改 update.yml
（就地改会与推送互斥，见 feedback-mutation-push-mutex）。

约定与 mut_state_cache.py 一致：
  · 基线（未变异）不绿 ⇒ 直接退出，绝不声称覆盖率（一条恒红的判据会让任何变异"看起来被挡住"）；
  · 变异没落到字节上 = INVALID，计入未覆盖，不当"已挡住"。
"""
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WF = os.path.join(ROOT, ".github", "workflows", "update.yml")
TEST = os.path.join("tests", "rss_history", "test_pages_deploy_wiring.py")
# 发布名单与非空守卫的**行为**判据在 A3（合成树上真跑正文），只看 A2 会漏：
# B3/B4 那种"名单少一项 / -s 改 -e"在 A2 里只是文本对账，改法稍一绕就看不出。
TESTS = [TEST,
         os.path.join("tests", "site_nav_drift", "test_pages_missing_report.py"),
         os.path.join("tests", "site_nav_drift", "test_pages_artifact_scope.py")]
TMP = os.path.join(ROOT, ".deploy-tmp")

ADD_MAIN = ("git add known_categories.json rss_sources.json build_rss_aggregator.py "
            "insight_engine.py build_daily_insight.py test_daily_insight.py")
# 锚点按 2026-10-02 白名单化之后的形状重挂。旧那两条（`for f in hot_snapshot.json; do` 与
# 每个名字一行 `test -s _pages/x`）早在 ② 就不存在了 ⇒ B3/B4/B5 一直在报 INVALID，
# 而 INVALID 被算进"未挡住"是对的 —— 电池不假装覆盖，是人得去读它那一行。
# 名单必须是**单行** `for f in …; do`：A2 那几条判据按 `^\s*for f in (.+); do$` 抽名单，
# 拆成续行会让 4 条判据同时红（今天试过，正文"好看"了但契约断了）。要动形状就得同批改解析器。
# 定位方式按**行正则**而不是字面量：这一行的内部空格数不属于契约（正文里曾出现两空格与一空格两种形状，
# 绑字面量的锚点会一处也匹配不到 ⇒ 变异"没落到字节上"，而 INVALID 混在汇总里最容易被跳过）。
# 必须把 index.html 写进模式本身：正文里还有别的 `for f in …; do`（嵌入缓存那三个 .npy/.index），
# 只按形状 search 会先命中它们 —— 那是"锚点抓错行"，比抓不到更危险，因为变异会打在无关步骤上。
BY_NAME_RE = re.compile(r"^([ \t]*for f in )([^\n]*index\.html[^\n]*?)([ \t]*; do[ \t]*)$", re.M)


def _byname_line(src):
    m = BY_NAME_RE.search(src)
    assert m and "index.html" in m.group(2), "找不到 Stage 的按名发布行 ⇒ 本电池的靶不存在，别自证干净"
    return m


def _byname_src(src, tokens):
    m = _byname_line(src)
    return src[:m.start()] + m.group(1) + " ".join(tokens) + m.group(3) + src[m.end():]


def _base_tokens():
    m = _byname_line(OPEN_WF())
    return m.group(2).split()


# 按 ② 之后的真实形状挂锚：守卫是**一行** `if [ ! -s "_pages/$must" ]; then`（10 空格缩进），
# 以前那条锚把引号写在 `-s` 后面（`! -s("_pages/…`），漏掉了 test 与 [ 之间的空格 ⇒ 一处也匹配不到，
# B4 长期 INVALID。这里只锁"操作符 + $must + ; then"三要素，缩进与空格宽度都不参与匹配。
GUARD_RE = re.compile(r'^([ \t]*if \[ )! -s(\s*"[^"\n]*\$must[^"\n]*"\s*\]; then[ \t]*)$', re.M)


def _guard_to_e(src):
    m = GUARD_RE.search(src)
    assert m, '找不到非空守卫行（`if [ ! -s "_pages/$must" ]; then`）⇒ B4 无从下手，别把它算作覆盖'
    return src[:m.start()] + m.group(1) + '! -e' + m.group(2) + src[m.end():]


def OPEN_WF():
    return open(WF, encoding="utf-8").read()


def sub_nth(text, old, new, nth):
    """只替换第 nth 处（1 起）：主路径与重试分支的清单逐字相同，必须能分别下手。"""
    i, seen = -1, 0
    while True:
        i = text.find(old, i + 1)
        if i < 0:
            raise AssertionError("锚点不足 %d 处" % nth)
        seen += 1
        if seen == nth:
            return text[:i] + new + text[i + len(old):]


def main():
    src = open(WF, encoding="utf-8").read()
    assert src.count(ADD_MAIN) == 2, "两条 add 清单未逐字同一（实测 %d 处）" % src.count(ADD_MAIN)
    withchunk0 = ADD_MAIN.replace("git add ", "git add rss-data-0.js ", 1)
    withhot = ADD_MAIN.replace("git add ", "git add hot_snapshot.json ", 1)
    MUTS = [
        ("B1 把 rss-data-0.js 加回主路径清单（0.6 MiB/场 复活）",
         sub_nth(src, ADD_MAIN, withchunk0, 1), None),
        ("B2 只摘主路径、重试分支仍提交 hot_snapshot",
         sub_nth(src, ADD_MAIN, withhot, 2), None),
        ("B3 发布名单漏掉 hot_snapshot.json（首页侧栏空数据）",
         _byname_src(src, [t for t in _byname_line(src).group(2).split() if t != "hot_snapshot.json"]), None),
        # 只把守卫的 `-s` 换成 `-e`：存在但 0 字节的产物会被当有效件发上线（那入口整页空白）。
        # 这条以前打的是已经不存在的 `test -s _pages/x` 行 ⇒ 一直 INVALID。
        ("B4 非空守卫改成 -e（0 字节产物被当成功发布）",
         _guard_to_e(src), None),
        ("B5 把纯跨场态 descriptions_zh.json 塞进发布名单（扩大公开面）",
         _byname_src(src, _byname_line(src).group(2).split() + ["descriptions_zh.json"]), None),
        ("P1 从**两条** add 清单一起摘掉 rss_sources.json（前端 fetch 它 ⇒ 上线就是 404/空侧栏）",
         sub_nth(src, ADD_MAIN, ADD_MAIN.replace(" rss_sources.json", ""), 1)
            .replace(ADD_MAIN, ADD_MAIN.replace(" rss_sources.json", "")), None),
    ]

    def run(mut, label):
        if mut == src:
            return "%-56s INVALID（变异没落到字节上）" % label, False
        os.makedirs(TMP, exist_ok=True)
        wfp = os.path.join(TMP, "_mut_site_update.yml")
        with open(wfp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(mut)
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", STARHUB_UPDATE_YML=wfp, PYTHONIOENCODING="utf-8")
        p = subprocess.run([sys.executable, "-m", "pytest", *TESTS, "-q", "--no-header",
                            "-p", "no:cacheprovider"], cwd=ROOT, env=env,
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=600)
        lines = (p.stdout or "").strip().splitlines()
        failed = sorted({l.split("::")[-1].split(" ")[0] for l in lines if l.startswith("FAILED")})
        tail = lines[-1] if lines else "?"
        return "%-56s %s %s%s" % (label, "RED  " if p.returncode else "GREEN(判据漏了!)", tail,
                                  ("  <- " + ", ".join(failed[:2])) if failed else ""), p.returncode != 0

    base = subprocess.run([sys.executable, "-m", "pytest", *TESTS, "-q", "--no-header",
                           "-p", "no:cacheprovider"], cwd=ROOT,
                          capture_output=True, text=True, encoding="utf-8", timeout=600)
    print("基线（未变异）：%s  rc=%d" % ((base.stdout.strip().splitlines() or ["?"])[-1], base.returncode))
    if base.returncode != 0:
        print(base.stdout[-1800:])
        print("基线不绿 ⇒ 本次不声称任何覆盖率")
        return 1
    escapes = 0
    for label, mut in [(m[0], m[1]) for m in MUTS]:
        out, caught = run(mut, label)
        print(out)
        if not caught:
            escapes += 1
    print("\n未被挡住的变异/无效变异：%d" % escapes)
    return 1 if escapes else 0


if __name__ == "__main__":
    sys.exit(main())
