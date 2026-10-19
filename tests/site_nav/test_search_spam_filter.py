# -*- coding: utf-8 -*-
"""服务端屏蔽低质结果后，前端两件事不许坏：分页与「屏蔽可见」。

起因（2026-10-08）：全网搜索要屏蔽靠堆砌关键词骗排名的仓库（实测样本 description 64,765
字符，正常页 30 条最长 138）。但 `api/search.js` 一旦过滤，返回的 items 就少于 30，
而 `renderSearchResults` 判断"还有下一页"用的正是 `items.length === SEARCH_PER_PAGE`
⇒ 屏蔽会顺手把「滚动加载更多」判成到底了。**这个坑旧代码里那段 `!translated` 兜底已经踩了**
（它同样会把 30 条滤成更少），只是过去很少触发。

所以计数必须与内容分离：响应带 `raw_count`（GitHub 该页原始条数）与 `filtered_out`
（被剔掉的条数），hasMore 只看 raw_count，页脚把屏蔽条数显示出来 —— 屏蔽要看得见才可回溯。

为什么用 node 跑切片而不是 JSdom：CI 无 `node_modules`，`require('jsdom')` 会当场崩
（同 tests/site_nav/test_article_contract.py 开头记的硬约束）。跑的是**真产物**：
从 template.html 括号配平切出 renderSearchResults 本体，POOL_NAMES 也切真定义。
"""
import io
import json
import os
import re
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TPL = os.environ.get("STARHUB_TEMPLATE_HTML") or os.path.join(ROOT, "template.html")

POOL_DEF = re.compile(r"^const POOL_NAMES\s*=.*$", re.M)

STUB = r"""
const LANG_COLORS = {};
const icon = {link: '<i></i>'};
function escHtml(s){return String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
function hl(s){return String(s);}
function fmtStars(n){return String(n);}
function fmtRelTime(x){return '刚刚';}
let searchLoaded = 0, searchHasMore = false;
const SEARCH_PER_PAGE = 30;
let __state = [];
function renderSearchState(kind, msg){ __state.push(kind); __foot.textContent = ''; }
const __foot = {textContent: ''};
const __rows = [];
function __node(){
  return {textContent: '', innerHTML: '',
          classList: {add(){}, remove(){}},
          appendChild(r){ __rows.push(r.innerHTML); }};
}
const __list = __node();
function $(sel){ return sel === '#sList' ? __list : (sel === '#sFoot' ? __foot : __node()); }
const document = {createElement: () => ({className: '', innerHTML: ''})};
const favs = new Set();
const DATA = [];
"""

CALL = r"""
const mk = n => ({full_name: n, html_url: 'https://github.com/' + n, desc: 'd',
                  stars: 10, topics: [], language: 'Python', updated_at: '2026-10-08T00:00:00Z'});
const page = (n) => Array.from({length: n}, (_, i) => mk('o/r' + i));

function __scenario(resp){
  searchLoaded = 0; searchHasMore = false; __rows.length = 0; __state = [];
  renderSearchResults(resp);
  return {hasMore: searchHasMore, foot: __foot.textContent, rows: __rows.length, state: __state.slice()};
}

const out = {
  // A：过滤后只剩 22 条，但该页原始 30 条 ⇒ 分页不许断
  A: __scenario({query: 'q', total: 15309, items: page(22), raw_count: 30, filtered_out: 8}),
  // B：一条没屏蔽 ⇒ 不许出现"已屏蔽"字样（假警比无警更糟）
  B: __scenario({query: 'q', total: 15309, items: page(30), raw_count: 30, filtered_out: 0}),
  // C：旧形状响应（无 raw_count）⇒ 回退到 items.length，不许把分页弄坏
  C: __scenario({query: 'q', total: 15309, items: page(30)}),
  // D：整页都被屏蔽 ⇒ 不许只留一句「未找到相关仓库」
  D: __scenario({query: 'q', total: 15309, items: [], raw_count: 30, filtered_out: 30}),
};
process.stdout.write(JSON.stringify(out));
"""


def _read(path):
    with io.open(path, encoding="utf-8") as fh:
        return fh.read()


def _slice_function(txt, name):
    """按括号配平切出 `function name(...) { ... }` 本体——判据要跑真产物，不是抄一份。"""
    i = txt.find("function " + name + "(")
    assert i >= 0, "template.html 里没有 %s()：判据的锚点先失效了（改名要同步这里）" % name
    j = txt.find("{", i)
    assert j > 0, "%s 找不到函数体起点" % name
    depth = 0
    for k in range(j, len(txt)):
        ch = txt[k]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return txt[i:k + 1]
    raise AssertionError("%s 括号不配平" % name)


def _run():
    txt = _read(TPL)
    m = POOL_DEF.search(txt)
    assert m, "template.html 里没有 `const POOL_NAMES = ...` ⇒ 锚点先失效"
    script = (STUB + "\n" + m.group(0) + "\n"
              + _slice_function(txt, "renderSearchResults") + "\n" + CALL)
    p = subprocess.run(["node", "-e", script], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    assert p.returncode == 0, "node 跑切片失败（判据在空转之前先要能跑）：%s" % p.stderr[:600]
    return json.loads(p.stdout)


def test_filtered_page_keeps_pagination_alive():
    """A：服务端滤掉 8 条后，「还有下一页」必须仍然为真。

    旧代码 `hasMore = items.length === 30` 在这一档必红 ⇒ 这条就是本次改动的靶。"""
    a = _run()["A"]
    assert a["rows"] == 22, "应渲染 22 行，实得 %d" % a["rows"]
    assert a["hasMore"] is True, \
        "过滤后 items 少于 30 就判成「没有下一页」⇒ 滚动加载更多直接断掉（必须看 raw_count）"


def test_filtered_count_is_visible_to_user():
    """屏蔽条数要显示出来：静默变少会被当成「GitHub 就这么多结果」，无法回溯。"""
    a = _run()["A"]
    assert "屏蔽" in a["foot"] and "8" in a["foot"], \
        "页脚没交代剔了几条 ⇒ 用户看到 22 条会以为总共就 22 条，实得: %r" % a["foot"]


def test_no_fake_alarm_when_nothing_filtered():
    """B：一条都没屏蔽时不许出现屏蔽文案。"""
    b = _run()["B"]
    assert "屏蔽" not in b["foot"], "filtered_out=0 仍报屏蔽 ⇒ 假警: %r" % b["foot"]
    assert b["hasMore"] is True, "无屏蔽的满页必须仍有下一页"


def test_legacy_response_shape_still_works():
    """C：响应缺 raw_count（旧缓存/旧部署）时按 items.length 回退，分页不许坏。"""
    c = _run()["C"]
    assert c["hasMore"] is True, "回退逻辑没接上 ⇒ 旧形状响应会让加载更多消失"
    assert "屏蔽" not in c["foot"], "没有 filtered_out 字段时不许凭空造出屏蔽文案"


def test_whole_page_filtered_is_not_reported_as_no_result():
    """D：整页被屏蔽时，页脚要交代原因，不能只留一句「未找到相关仓库」。"""
    d = _run()["D"]
    assert d["rows"] == 0, "整页屏蔽后不该还画出行"
    assert "屏蔽" in d["foot"], \
        "total=15309 却只报「未找到相关仓库」⇒ 用户会以为换词能救，实际是被过滤了；实得 %r" % d["foot"]
