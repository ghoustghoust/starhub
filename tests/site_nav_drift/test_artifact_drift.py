# -*- coding: utf-8 -*-
"""A3（advisory）：纯产物页面**不许再回到 git**。

这里原来是 `test_index_header_matches_template`——拿库里的 `index.html` 与 `template.html`
比 header 恒等。batch① 之后产物不再进 CI 的提交清单（update.yml 的 `git add` 只列
`rss-data-0.js` 与若干 json），所以那份"产物"是**冻结快照**：
这条恒等式只能在快照上成立，谁下一次改 `template.html` 的 header 它就红，
而**没有任何正当修法**（要么手改产物再入库 = 把 3.8 MB/场的历史压力请回来，要么改判据）。
实测留证：库里 index.html 280,193 B / md5 8527448558，线上 282,697 B / md5 c1e34b1acc —— 两份本就不同，
绿的只是 header 那一块。

产物既已不入库，"手改产物会被下一场构建抹掉"这个风险就**结构性消失**了，
所以这里换成真正还能守的那件事：**再入库检测**。
判据看的是 git 树，不是 workflow 文本 —— `test_no_page_html_is_committed`（A2）只看文本，
看不见"清单里没写但文件还在库里"这一半（2026-10-01 实测树里仍有 3.73 MiB）。

留在 A3 而不是挪进 A2：文件回来只是历史体积变大，不是站点坏掉；
拿它挡 blocking 闸，等于让一场部署为一笔 3 MiB 的滞留陪葬（本仓 10-01 已实测过冻两小时的代价）。
"""
import os
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 每场构建重写、无人跨场读回的**纯产物页面**（都在仓库根，按名字精确匹配）。
# 不按"所有 .html"一刀切：`template.html` 是生成端源码，而 docs/ 或子目录里将来可能放
# 真正需要入库的 html（我第一版就是按后缀全查，误把 docs/x.html 报成违规）。
GENERATED_PAGES_AT_ROOT = {
    "index.html", "rss-aggregator.html", "ai-daily.html", "daily-insight-history.html",
}


def _tracked_html():
    """`git ls-files` 拿跟踪清单；不在 git 检出里（比如只拷了源码）就返回 None，不要冒充成绿。"""
    if not os.path.isdir(os.path.join(ROOT, ".git")):
        return None
    p = subprocess.run(["git", "ls-files", "-z", "--", "*.html"], cwd=ROOT,
                       capture_output=True, text=True)
    if p.returncode != 0:
        return None
    return [x for x in p.stdout.split("\0") if x.strip()]


def test_no_generated_page_html_is_tracked():
    tracked = _tracked_html()
    if tracked is None:
        raise AssertionError(
            "拿不到 git 跟踪清单（不在 git 检出里）：这条判据的唯一职责就是看树，"
            "静默跳过会让它变成'绿但什么都没查'")
    offenders = sorted(x for x in tracked
                       if ("/" not in x and "\\" not in x and x in GENERATED_PAGES_AT_ROOT))
    assert not offenders, (
        "这些纯产物页面又回到 git 了（每场构建重写、无人跨场读回，进 git 只会永久占历史）：%s；"
        "`template.html` 是生成端源码，应当留下" % offenders)
