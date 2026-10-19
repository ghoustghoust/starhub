"""封面判空表的变异测试：每个变异都必须让至少一条判据变红。

用法：py -3.11 tools/mut_cover_table.py
约定：变异体只改副本，靠 RSS_BUILD_SRC 生效（本仓既有约定）。
"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "build_rss_aggregator.py")
TMP_DIR = os.path.join(ROOT, ".deploy-tmp")
TEST = os.path.join("tests", "rss_cover", "test_bad_cover_domain_table.py")

MUTS = [
    ("M1 判空没接进 _upgrade_img_url（生产路径绕过表）",
     '    url = _drop_unloadable_cover(url)\n    if not url:\n        return ""\n',
     ''),
    ("M2 误收 pbs.twimg.com（代理侧能出图的域名被清掉）",
     '    "npr.brightspotcdn.com",',
     '    "pbs.twimg.com",'),
    ("M3 表被清空（防线自我注销）",
     '_BAD_COVER_HOSTS = frozenset({',
     '_BAD_COVER_HOSTS = frozenset() and frozenset({'),
    ("M4 把 Referer 白名单域名也判空（③#2 变死代码）",
     '_BAD_COVER_HOSTS = frozenset({',
     '_BAD_COVER_HOSTS = frozenset({"caifuzhongwen.com",'),
    ("M5 卫报升级规则留在表里（永不触发的死配置）",
     '    ("redd.it",          _query_width_upgrade),',
     '    ("redd.it",          _query_width_upgrade),\n    ("i.guim.co.uk",    _query_width_upgrade),'),
    ("M6 0 命中不再出声（表过期无人知）",
     '"[封面判空] 0 张命中',
     '"[图片] 0 张命中'),
    ("M7 只判精确 host、不判子域（live.x.i.guim.co.uk 漏判）",
     '        if host == d or host.endswith("." + d):',
     '        if host == d:'),
    ("M8 域名匹配改成子串匹配（noti.guim.co.uk 这类前缀相似域名被误伤）",
     '        if host == d or host.endswith("." + d):',
     '        if d in host:'),
    ("M9 历史清理调用点被删（函数变死代码）",
     '    _hist_purged = _purge_bad_covers_in_history(_rss_history)',
     '    _hist_purged = 0'),
    ("M10 host 解析吃掉端口/userinfo 的写法回退（整串当 host）",
     '    netloc = (s.partition("//")[2].split("/")[0] or "").split("@")[-1].split(":")[0]',
     '    netloc = (s.partition("//")[2].split("/")[0] or "")'),
    ("M11 渲染点退回未过滤的 a.img（实时封面绕过判空）",
     "var _cv=_dropBadCover(a.img);\n      var hasImg=!!_cv;",
     "var _cv=a.img;\n      var hasImg=!!_cv;"),
    ("M12 src 用未过滤值、但保留 hasImg（只坏一半）",
     "h+='<img class=\"cover-img\" src=\"'+esc(_cv)+'\"",
     "h+='<img class=\"cover-img\" src=\"'+esc(a.img)+'\""),
]


def run(label, old, new):
    raw = open(SRC, encoding="utf-8").read()
    if old not in raw:
        return "%-56s SKIP（变异锚点没找到 = 变异本身无效）" % label
    mutated = raw.replace(old, new, 1)
    if mutated == raw:
        return "%-56s SKIP（替换后无变化）" % label
    out = os.path.join(TMP_DIR, "_mut_bra.py")
    with open(out, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(mutated)
    env = dict(os.environ, RSS_BUILD_SRC=out, PYTHONIOENCODING="utf-8")
    p = subprocess.run([sys.executable, "-m", "pytest", TEST, "-q", "--no-header",
                        "-p", "no:cacheprovider"],
                       cwd=ROOT, env=env, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=900)
    tail = (p.stdout or "").strip().splitlines()
    summary = tail[-1] if tail else "(无输出)"
    names = sorted({ln.split("::")[-1].split(" ")[0] for ln in tail if ln.startswith("FAILED")})
    verdict = "RED   " if p.returncode != 0 else "GREEN(判据漏了!)"
    return "%-56s %s %s%s" % (label, verdict, summary,
                              ("  <- " + ", ".join(names[:3])) if names else "")


def main():
    if not os.path.isdir(TMP_DIR):
        os.makedirs(TMP_DIR)
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    env.pop("RSS_BUILD_SRC", None)
    base = subprocess.run([sys.executable, "-m", "pytest", TEST, "-q", "--no-header",
                           "-p", "no:cacheprovider"], cwd=ROOT, env=env,
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=900)
    bl = (base.stdout.strip().splitlines() or ["(无输出)"])
    print("基线（未变异）：%s  rc=%d" % (bl[-1], base.returncode))
    if base.returncode != 0:
        print("\n".join(bl[-30:]))
        return 1
    bad = 0
    for label, old, new in MUTS:
        line = run(label, old, new)
        print(line)
        if "GREEN" in line or "SKIP" in line:
            bad += 1
    out = os.path.join(TMP_DIR, "_mut_bra.py")
    if os.path.isfile(out):
        os.remove(out)
    print("\n未被任何判据挡住的变异/无效变异：%d" % bad)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
