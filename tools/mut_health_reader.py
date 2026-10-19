# -*- coding: utf-8 -*-
"""api/health.js 阅读器探活的变异电池：每条判据都要能被一个对应的坏改动挡住。

它钉的是 P1.5 新加的那条**内容级**探活（readerChunksOk）。这一面之所以值得单独做电池：
它唯一的失效模式是"看着在报警，其实永远不会响"或者"一抖就报"——两者都只能靠打坏改动验出来，
读文本看不出来。判据本身是纯文本断言，所以电池也是纯文本注入（不发网络请求，不需要 git/bash）。

做法与 tools/mut_star_state.py 同：真实文件一个字节不动，把 api/health.js 复制进临时目录打变异，
用 HEALTH_JS 注入点让判据读副本；每轮从干净副本重打，并要求"未变异对照组先绿"。

用法：py -3.11 tools/mut_health_reader.py
"""
import io
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HEALTH = os.path.join("api", "health.js")
TEST = os.path.join("tests", "site_nav_drift", "test_trigger_chain.py")
VERCEL = "vercel.json"

MUTATIONS = [
    # H1：把「不为 false 才拉黑」写成布尔真值 ⇒ null（不知道）被当成坏，天天假警。
    ("H1 ok 合成改成布尔真值（把『不知道』报成坏）",
     "readerOk !== false && ",
     "",
     "test_health_reader_probe_is_tri_state_not_binary"),
    # H2：块总数写死成 8。今天正好是 8 ⇒ 线上看不出问题，块数一浮动就永远只验前几块
    # （10-07 那份写死 7 个名字的带回清单就是同一个认知错误的放大版）。
    ("H2 循环上界写死数字（闭集表达开集）",
     "for (let i = 1; i < total; i++)",
     "for (let i = 1; i < 8; i++)",
     "test_health_reader_probe_reads_chunk_count_from_the_shipped_file"),
    # H3：单次超时抬到 9s ⇒ 最坏 2*9+5=23s > maxDuration 15s ⇒ 函数被杀，
    # 监控报的是「探活死了」这种最难查的假警形状。
    ("H3 单次超时抬到 9s（顶穿 maxDuration 被杀成假警）",
     "const READER_TIMEOUT_MS = 2500;",
     "const READER_TIMEOUT_MS = 9000;",
     "test_health_reader_probe_fits_inside_max_duration"),
    # H4：0 块自己没了却只报"不知道"。浏览器的 _total 和首屏数据都在这个文件里，
    # 它 404 = 整页空白，这是确定性损坏，不响就是"最严重的形状恰好没人守"。
    ("H4 0 块 404 报成『不知道』（整页空白却不响）",
     "    if (gone(r.status)) return false;   // 0 块没了",
     "    if (gone(r.status)) return null;    // 0 块没了",
     "test_health_reader_probe_is_tri_state_not_binary"),
    # H5：去掉 identity。中间层按 gzip 回 206 时，切下来的是压缩体的半截，解压抛异常
    # ⇒ 每次都走 catch ⇒ 这条面"装了但永远 null"，比不装更坏（它给人有防线的错觉）。
    ("H5 去掉 Accept-Encoding: identity（被 gzip 时整条面静默失效）",
     "    'Accept-Encoding': 'identity',",
     "",
     "test_health_reader_probe_reads_chunk_count_from_the_shipped_file"),
    # H6：上界写回 8。今天是 8 块 ⇒ 线上一点看不出问题，数据一涨到 9 块就整条面闭嘴。
    ("H6 探活上界写死成今天的块数（数据一涨就失效）",
     "const MAX_TOTAL_CHUNKS = 25;",
     "const MAX_TOTAL_CHUNKS = 8;",
     "test_health_reader_probe_reads_chunk_count_from_the_shipped_file"),
    # H7：不验空壳块。生成端"旧块清空不删除"会留下 200 的空 sources 文件——
    # 浏览器 onload 成功却没有源，正是 10-07 那个症状的另一种写法。
    ("H7 不验空壳块（200 的空 sources 被判成健康）",
     "      if (r.status === 206 && EMPTY_SOURCES.test(await r.text())) return false;",
     "",
     "test_health_reader_probe_is_tri_state_not_binary"),
    # H8 退回 content-length 快路径：首页体积正常但 emb 归零的那种坏就永远看不见
    # （P2 的整个意义所在——这一版最初就是这么写的，实测过一次慢网络假警后才改成读正文）。
    ("H8 首页探针退回 content-length 短路（不读正文就数不了 emb）",
     "    const body = await r.text();\n    const pages = body.length >= MIN_INDEX_BYTES;",
     "    const len = Number(r.headers.get('content-length') || 0);\n    if (len) return { pages: len >= MIN_INDEX_BYTES, emb: null };\n    const body = await r.text();\n    const pages = body.length >= MIN_INDEX_BYTES;",
     "test_health_emb_probe_counts_from_the_fetched_body"),
    # H9：emb 归零只报"不知道"。配额耗尽时前端整段语义召回消失，却没人响——这就是 P2 要防的。
    ("H9 emb 归零报成『不知道』（静默降级不响）",
     "    if (n === 0) emb = false;",
     "    if (n === 0) emb = null;",
     "test_health_emb_probe_counts_from_the_fetched_body"),
    # H10：把"取不到"报成坏。本机 10-08 实测过一次慢网络直接 503，那是通道的问题不是站点的。
    ("H10 网络层取不到报成坏（一次抖动=一场假警）",
     "    return { pages: null, emb: null };",
     "    return { pages: false, emb: null };",
     "test_health_probes_treat_network_failure_as_unknown_not_broken"),
]


def _run(tmp, name):
    env = {**os.environ, "HEALTH_JS": os.path.join(tmp, HEALTH),
           "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"}
    r = subprocess.run([sys.executable, "-m", "pytest", TEST, "-q", "-k", name],
                       cwd=tmp, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", env=env)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def _stage(tmp):
    # 判据读三份：被变异的 health.js、注入点所在的测试文件、以及 maxDuration 来源的 vercel.json。
    # 少 copy 任何一份都会变成 FileNotFoundError，被下面的"非零退出"误报成"判据不合格"。
    for rel in (HEALTH, TEST, VERCEL, "build_rss_aggregator.py"):
        dst = os.path.join(tmp, rel)
        d = os.path.dirname(dst)
        if d:
            os.makedirs(d, exist_ok=True)
        shutil.copyfile(os.path.join(ROOT, rel), dst)


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    bad = []
    with tempfile.TemporaryDirectory(dir=os.path.join(ROOT, ".deploy-tmp")) as tmp:
        for mid, old, new, must in MUTATIONS:
            _stage(tmp)  # 每轮从干净副本重打（变异绝不互相掩盖）
            code0, out0 = _run(tmp, must)
            if code0 != 0:
                bad.append("%s：对照组（未变异）就不绿，本条判定作废：%s" % (mid, out0.strip()[-220:]))
                continue
            p = os.path.join(tmp, HEALTH)
            s = io.open(p, encoding="utf-8").read()
            n = s.count(old)
            if n != 1:
                bad.append("%s：靶子在 health.js 里出现 %d 次 ⇒ 锚点失效，变异没打上" % (mid, n))
                continue
            io.open(p, "w", encoding="utf-8", newline="\n").write(s.replace(old, new, 1))
            code, out = _run(tmp, must)
            if code == 0:
                bad.append("%s：打了坏改动，%s 仍然绿 ⇒ 空判据" % (mid, must))
                continue
            if "failed" not in out and "error" not in out.lower():
                bad.append("%s：非零退出但不是判据红（可能是收集崩）：%s" % (mid, out.strip()[-220:]))
                continue
            print("  %-46s 挡住 -> %s" % (mid, must))

    if bad:
        print("\n%d 项不合格：" % len(bad))
        for b in bad:
            print("  - " + b)
        return 1
    print("\n全部 %d 个变异都被各自靶判据挡住，且每条都有未变异对照组先证明过绿。" % len(MUTATIONS))
    return 0


if __name__ == "__main__":
    sys.exit(main())
