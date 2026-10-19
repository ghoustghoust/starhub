"""③#4（实时补抽图 + 渲染层过滤）的变异测试：只改副本，靠 env 注进判据（不碰真源文件）。

用法：py -3.11 tools/mut_realtime_cover.py
注入点：STARHUB_API_RSS / STARHUB_COVER_LIB / RSS_BUILD_SRC（本仓既有约定的同族）。
"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = os.path.join(ROOT, ".deploy-tmp")
API = os.path.join(ROOT, "api", "rss.js")
LIB = os.path.join(ROOT, "lib", "rss_cover.js")
BRA = os.path.join(ROOT, "build_rss_aggregator.py")

MUTS = [
    ("N1 RSS 分支不抽封面了", API,
     "      if (COVER) {\n        const _cv = COVER.pickItemImage(item, contentEncoded, desc);\n"
     "        if (_cv) result.img = _cv;\n      }\n", ""),
    ("N2 抽到了但短键映射不带出（响应恒空）", API,
     # 锚点带着缩进原文写：591 行后来从嵌套块里提了出来（缩进 8→2 空格），锚点就对不上了。
     # 电池报的是"SKIP（锚点没找到）"而不是绿，这是诚实的，但本仓电池不进 CI ⇒ 一漂就是几天，
     # 所以 10-08 全量扫描时它排在"未被挡住"里。改锚点，不判据。
     "  if (it.img) obj.img = it.img;", ""),
    ("N3 不解实体就找 <img>（转义描述漏抽）", LIB,
     "  var text = decodeEntities(html);", "  var text = html;"),
    ("N4 音频 enclosure 也被当封面", LIB,
     "    if (url && (type.indexOf('image') === 0 || IMG_EXT_RE.test(url))) return url;",
     "    if (url) return url;"),
    ("N5 parseFeed 只接一条分支（Atom 漏抽）", API,
     "          const _cv = COVER.pickItemImage(entry, extractTag(entry, 'content'), "
     "extractTag(entry, 'summary'));",
     "          const _cv = '';"),
    ("N7 多导一个没人用的名字（公共 API 空壳）", LIB,
     "module.exports = { pickItemImage, firstImgSrc };",
     "module.exports = { pickItemImage, firstImgSrc, decodeEntities };"),
    ("N8 合并处不接 API 的 img（实时封面落不了地）", BRA,
     "image:it.img||''", "image:''"),
    ("N9 buildArt 只认短名（合并后的长名 image 被丢）", BRA,
     "img:it.image||it.img||''", "img:it.img||''"),
    ("N6 渲染层丢掉 scheme 大小写归一（Py/JS 分叉）", BRA,
     "    var s = String(u || '').trim().toLowerCase();", "    var s = String(u || '');"),
]

ENV_KEY = {API: "STARHUB_API_RSS", LIB: "STARHUB_COVER_LIB", BRA: "RSS_BUILD_SRC"}


def run_tests(env):
    e = dict(os.environ, PYTHONIOENCODING="utf-8")
    e.update(env)
    p1 = subprocess.run(["node", os.path.join("tests", "rss_js", "test_realtime_cover.js")],
                        cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                        errors="replace", timeout=300, env=e)
    p2 = subprocess.run([sys.executable, "-m", "pytest", "tests/rss_cover", "-q", "--no-header",
                         "-p", "no:cacheprovider"], cwd=ROOT, capture_output=True, text=True,
                        encoding="utf-8", errors="replace", timeout=900, env=e)
    lines = (p2.stdout or "").strip().splitlines()
    tail = lines[-1] if lines else "?"
    failed = sorted({l.split("::")[-1].split(" ")[0] for l in lines if l.startswith("FAILED")})
    ok = p1.returncode == 0 and p2.returncode == 0
    if p1.returncode != 0:
        tail += " | node 用例集 rc=%d" % p1.returncode
    return ok, tail, failed


def main():
    if not os.path.isdir(TMP):
        os.makedirs(TMP)
    ok, tail, _ = run_tests({})
    print("基线（未变异）：%s  %s" % ("GREEN" if ok else "RED(先修这个!)", tail))
    if not ok:
        return 1
    escaped = 0
    for label, path, old, new in MUTS:
        src = open(path, encoding="utf-8").read()
        if old not in src:
            print("%-44s SKIP（变异锚点没找到 = 变异本身无效）" % label)
            escaped += 1
            continue
        copy = os.path.join(TMP, "_mut_" + os.path.basename(path))
        with open(copy, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(src.replace(old, new, 1))
        green, tail, names = run_tests({ENV_KEY[path]: copy})
        os.remove(copy)
        print("%-44s %s %s%s" % (label, "RED  " if not green else "GREEN(判据漏了!)", tail,
                                 ("  <- " + ", ".join(names[:3])) if names else ""))
        if green:
            escaped += 1
    print("未被挡住的变异/无效变异：%d" % escaped)
    return 1 if escaped else 0


if __name__ == "__main__":
    sys.exit(main())
