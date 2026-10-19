# -*- coding: utf-8 -*-
"""全网搜索结果的「✓ 已收藏」要认**收藏池**，不能只认浏览器本地那一份。

用户报（2026-10-05）：搜 FDE 出来的结果里，他已经 star 过的仓库不再带「已收藏」标记。
根因不是渲染坏了——浏览器实测：往 localStorage 写两条再喂真函数，徽章按预期出现且只落在
对的那条上。坏的是**判定口径**：`faved = favs.has(...)`，而 `favs` 来自 localStorage
（`wb_starhub_favs_v1`），出厂种子只有 3 条 ⇒ 换浏览器、清缓存、换域（10-04 站点产物从
Vercel 域收口到 Pages 域，localStorage 按域隔离）之后整片标记消失，且再也回不来。

判据为什么用 node 跑切片而不是 JSdom：CI 上没有 `node_modules`，`require('jsdom')` 会当场崩
（硬约束与出处见 tests/site_nav/test_article_contract.py 开头）。跑的是**真产物**：从
template.html 括号配平切出 renderSearchResults 本体，配最小桩。

三档缺一不可：
  ① 在池内、不在本地收藏 ⇒ 必须有徽章（**这条就是本次修复的靶**，旧代码上必红）
  ② 两处都不在 ⇒ 必须没有（反向半，挡住"干脆整片都标"这种放宽）
  ③ 在本地收藏、不在池内 ⇒ 必须有（保住原有语义，别把置顶那半改丢）
"""
import io
import json
import os
import re
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# 变异体注入点：与本仓 STARHUB_API_RSS / STARHUB_BODY_RULES 同一约定。
TPL = os.environ.get("STARHUB_TEMPLATE_HTML") or os.path.join(ROOT, "template.html")

BADGE = "s-badge"
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
function renderSearchState(kind, msg){}
const __rows = [];
function __node(){
  return {textContent: '', innerHTML: '',
          classList: {add(){}, remove(){}},
          appendChild(r){ __rows.push(r.innerHTML); }};
}
const __list = __node();
function $(sel){ return sel === '#sList' ? __list : __node(); }
const document = {createElement: () => ({className: '', innerHTML: ''})};
const favs = new Set(%(FAVS)s);
const DATA = %(DATA)s;
"""

CALL = r"""
function __run(items){
  __rows.length = 0;
  renderSearchResults({query: 'q', total: items.length, items: items});
  return __rows.slice();
}
const A = 'someone/in-pool-only';        // 在收藏池、没在本地点过心
const B = 'someone/neither';             // 两处都不在
const C = 'someone/local-only';          // 在本地收藏、不在池内（老语义）
const mk = n => ({full_name: n, html_url: 'https://github.com/' + n, desc: 'd',
                  stars: 10, forks: 1, topics: [], language: 'Python',
                  updated_at: '2026-10-05T00:00:00Z'});
const rows = __run([mk(A), mk(B), mk(C)]);
process.stdout.write(JSON.stringify(rows));
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


def _render(favs, pool):
    txt = _read(TPL)
    body = _slice_function(txt, "renderSearchResults")
    script = (STUB % {"FAVS": json.dumps(favs),
                      "DATA": json.dumps([{"full_name": n} for n in pool])}
              + "\n" + _slice_pool_line(txt) + "\n" + body + "\n" + CALL)
    p = subprocess.run(["node", "-e", script], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    assert p.returncode == 0, "node 跑切片失败（判据在空转之前先要能跑）：%s" % p.stderr[:600]
    rows = json.loads(p.stdout)
    assert len(rows) == 3, "三档输入应产出 3 行，实得 %d ⇒ 渲染被提前 return 了" % len(rows)
    return rows


def _slice_pool_line(txt):
    """收藏池要跑**模板里的真定义**，不能由桩代劳。

    电池第一次跑就抓到一条逃逸：删掉 `const POOL_NAMES = ...` 那行，判据仍然 3 passed——
    因为桩里我自己补了一个 POOL_NAMES，于是测的是桩而不是产物。改成切片之后，
    删定义、改成空集都会红。
    """
    m = POOL_DEF.search(txt)
    assert m, ("template.html 里没有 `const POOL_NAMES = ...` ⇒ 收藏池口径没接上"
               "（或改了名，要同步这条锚点）")
    return m.group(0)


def test_in_pool_repo_shows_faved_badge():
    """① 靶：只"在收藏池里"就该标已收藏——不依赖浏览器本地存储。"""
    rows = _render(favs=[], pool=["someone/in-pool-only"])
    assert BADGE in rows[0], \
        "在收藏池里的搜索结果没标已收藏 ⇒ 口径仍只看 localStorage 那份，换浏览器就整片丢标记"
    assert BADGE not in rows[1], "两处都不在的条目也被标了已收藏 ⇒ 判定被放宽成整片都标"


def test_local_only_fav_still_badged():
    """③ 反向那半：老语义（本地点过心但不在池内）不能被这次改动弄丢。"""
    rows = _render(favs=["someone/local-only"], pool=[])
    assert BADGE in rows[2], \
        "只在本地收藏里的条目不再标已收藏 ⇒ 这次改动把原有语义也删了（应当是「或」，不是替换）"


def test_badge_text_is_not_empty_label():
    """徽章文案要还在——只钉 class 名会让一个空 span 也过关。"""
    rows = _render(favs=[], pool=["someone/in-pool-only"])
    assert "已收藏" in rows[0], "class 在但文案丢了 ⇒ 用户看不到任何标记，判据却照样绿"
