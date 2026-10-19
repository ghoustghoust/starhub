# -*- coding: utf-8 -*-
"""`.vercelignore` 的变异电池：判据 `tests/site_nav_drift/test_vercel_ignore_scope.py` 能不能被捅穿。

约定与本仓其它电池一致：
  · 只改**副本**（判据支持 STARHUB_VERCEL_IGNORE 这个口子），绝不就地写 .vercelignore —— 就地改会与推送互斥；
  · 基线（未变异）不绿 ⇒ 直接退出，不声称覆盖率；
  · "红了但不是自己的靶"同样算问题（判据之间会互相抢红，必须点名归属）；
  · 每个变异登记它该打中哪条判据，GREEN 控制项登记"不许红"的原因 —— 只验正向会把过度收紧当成安全。

两类失败模式本电池自己就是被它抓出来的（2026-10-02）：
  · 判据问裸目录名 `lib/` ⇒ gitignore 的目录规则匹配 `lib/x.js`，"排除 lib/"这个变异当场绿（漏检）；
  · 判据带结尾斜杠问 `api/` ⇒ 被"空行"误命中，真文件反而假红。
⇒ 所以 V2/V3 这两条是判据的定位方式自己的靶，不是可选装饰。
"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IGN = os.path.join(ROOT, ".vercelignore")
COPY = os.path.join(ROOT, ".deploy-tmp", "_mut_vercelignore")
TEST = os.path.join("tests", "site_nav_drift", "test_vercel_ignore_scope.py")

SWALLOW = "test_vercelignore_must_not_swallow_the_runtime_needs"
COVERS = "test_vercel_ignore_covers_the_non_site_root_files"

src = open(IGN, encoding="utf-8").read()

# (标签, 变异后的正文, 期望, 靶判据)
CASES = [
    ("V1 把硬依赖 rss_sources.json 排除（无兜底 ⇒ /api/rss 500）",
     src + "\nrss_sources.json\n", "red", SWALLOW),
    ("V2 把 lib/ 排除（掉运行时留存闸门与实时封面）", src + "\nlib/\n", "red", SWALLOW),
    ("V3 把 api/ 整个排除（函数本体消失）", src + "\napi/\n", "red", SWALLOW),
    ("V4 去掉 .github/ 这行（部署管线重新公开）", src.replace(".github/\n", "", 1), "red", COVERS),
    ("V5 去掉 *.md 这行（HANDOFF/CLAUDE 那批文档回来）", src.replace("*.md\n", "", 1), "red", COVERS),
    ("V6 去掉 template.html 这行（模板源码回来）",
     src.replace("template.html\n", "", 1), "red", COVERS),
    # GREEN 控制：加一条**谁都不该命中**的规则，判据不许因此变红 ——
    # 否则它其实在"任何改动都红"，那等于没有判据。
    ("G1 加一条与集合无关的规则（必须绿，判据不该乱红）", src + "\nzzz-not-a-real-path-*.bin\n", "green", ""),
    # GREEN 控制：软依赖**继续被排除**是对的（loadSnapshot 有 try/catch，实测线上 404）
    ("G2 快照继续排除着（必须绿：不发是设计，不是漏）", src, "green", ""),
]


def run():
    env = dict(os.environ, STARHUB_VERCEL_IGNORE=COPY, PYTHONIOENCODING="utf-8")
    p = subprocess.run([sys.executable, "-m", "pytest", TEST, "-q", "--no-header",
                        "-p", "no:cacheprovider", "--tb=line"],
                       cwd=ROOT, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=env, timeout=600)
    lines = (p.stdout or "").strip().splitlines()
    bad = sorted({l.split("::")[-1].split(" ")[0]
                  for l in lines if l.startswith(("FAILED", "ERROR"))})
    return p.returncode != 0, (lines[-1] if lines else "?"), bad


def write(body):
    os.makedirs(os.path.dirname(COPY), exist_ok=True)
    # newline="\n" 是必需的：Windows 的 text 模式会把喂进去的 \n 翻成 \r\n，
    # git 于是拿 "HANDOFF.md\r" 去判 ⇒ 一个都不命中（判据第一版就是这么假绿的）。
    with open(COPY, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(body)


def main():
    if "\r\n" in open(IGN, "rb").read().decode("utf-8", "replace"):
        print("[NG] .vercelignore 里出现 CRLF ⇒ 先归一成 LF 再跑（行尾混了会把 75 行改动报成 3 行）")
        return 1
    write(src)
    red, tail, bad = run()
    print("基线（未变异副本）：%s  rc=%d" % (tail, 1 if red else 0))
    if red:
        print("基线不绿 ⇒ 本次不声称任何覆盖率；失败：%s" % bad)
        return 1
    problems = []
    for label, body, want, target in CASES:
        if body == src and want == "red":
            problems.append("%s INVALID（变异没落到字节上）" % label)
            continue
        write(body)
        red, tail, bad = run()
        ok = (red == (want == "red"))
        if ok and want == "red" and target and target not in bad:
            ok = False
            tail += " 红了但不是自己的靶：" + ",".join(bad)
        print("%-52s %s %s" % (label, "RED  " if red else "GREEN", tail[:60]
                              + ("" if ok else "   <- 期望%s" % ("红" if want == "red" else "绿"))))
        if not ok:
            problems.append(label + ("（没挡住/红错对象）" if want == "red" else "（误伤：合法改动也被判红）"))
    if os.path.exists(COPY):
        os.remove(COPY)
    print("\n问题条目：%s" % (problems if problems else "无"))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
