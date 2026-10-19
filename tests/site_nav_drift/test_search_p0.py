# -*- coding: utf-8 -*-
"""T6 搜索 P0 判据（A3 advisory）。

用 node 直接 import api/search.js 测 buildQueryPlan（真 ESM，打真 translate.js 的
模块加载面但不打网络——buildQueryPlan 是纯函数）。Python 只做驱动与断言收集。
钉住的行为：
- 单词中文 → ["zh" OR "en"]（与旧版一致）；
- 多词中文 → 第一档是逐词英文 AND **不加引号**（旧版整串引号短语是 0 结果根因）；
- 降级档齐全（短语兜底 → 仅英文主词 → 仅首中文词）；
- 翻译全失败（en=null）时档位退化为中文档，不产出 "null" 字符串；
- 限流常量与 429 出口存在；统一链 import 存在（对齐 §8.7）。"""
import json
import subprocess
import os

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
NODE_SCRIPT = r"""
import assert from 'node:assert';
const m = await import(new URL('file:///REPO/api/search.js').href);
const plan = m.buildQueryPlan;

// ① 多词中文（翻译成功）：第一档逐词英文 AND、不加引号
const p1 = plan('视频 创作', new Map([['视频','video'],['创作','creation']]));
assert.equal(p1[0], 'video creation', '多词第一档必须是不加引号的英文 AND，实得: ' + p1[0]);
assert.ok(p1.includes('"视频 创作" OR "video creation"'), '第二档短语兜底');
assert.ok(p1.includes('video'), '仅英文主词档');
assert.ok(p1.includes('视频'), '仅首中文词档');

// ② 单词中文：保持旧版 [zh OR en]
const p2 = plan('视频', new Map([['视频','video']]));
assert.deepEqual(p2, ['视频 OR video']);

// ③ 翻译全失败：退化中文档，不出 null/undefined
const p3 = plan('视频 创作', new Map([['视频',null],['创作',null]]));
assert.ok(p3.every(q => q && !/null|undefined/.test(q)), JSON.stringify(p3));

// ④ 纯英文多词：不翻译也必须以“逐词 AND（无引号）”为首档，短语只作降级档
const p4 = plan('rust cli', new Map());
assert.equal(p4[0], 'rust cli', '纯英文多词首档不许引号短语化: ' + p4[0]);
assert.ok(p4.includes('"rust cli"'), '短语只作降级档');

// ⑤ 统一链 import 与限流出口（源码面）
const fs = await import('node:fs');
const src = fs.readFileSync(new URL('file:///REPO/api/search.js'), 'utf8');
assert.ok(src.includes("from './translate.js'"), '必须复用 translate.js 统一链');
assert.ok(src.includes('429'), '限流 429 出口');
assert.ok(/RATE_IP = 10/.test(src) && /RATE_GLOBAL = 25/.test(src), '限流常量');
console.log('ALL_SEARCH_P0_OK');
""".replace('REPO', ROOT.replace('\\', '/'))


def test_build_query_plan_and_wiring():
    r = subprocess.run(['node', '--input-type=module', '-e', NODE_SCRIPT],
                       capture_output=True, text=True, timeout=60, cwd=ROOT)
    out = (r.stdout or '') + (r.stderr or '')
    assert 'ALL_SEARCH_P0_OK' in r.stdout, 'node 断言未通过:\n' + out[-2000:]


# ── 低质/骗排名仓库屏蔽（2026-10-08）────────────────────────────────
# 靶：cirosantilli/china-dictatorship 这类靠**把整篇正文塞进 description 字段**骗搜索排名
# 导流的仓库。实测数字（打真 GitHub API 拿的，不是估的）：
#   该仓库 description = 64,765 字符，且 /search/repositories **原样返回**整段
#     ⇒ 一条垃圾就把整页响应撑到 63KB，前端渲染成一堵文字墙；
#   三页真样本（video generation / ai tools / 靶）里合法仓库 description 最长 350 字符
#     —— GitHub UI 的写入上限就是 350 ⇒ >400 只能是绕开 UI 用 API 塞进来的。
#
# 这里刻意钉一批**「不许杀」真样本**，它们比「该杀的」更重要：
#   ① 曾有判据②「topics 顶格 20 + 描述 >200 + 无代码语言」，第二页就把 144,081 星的
#      x1xhlol/system-prompts-and-models-of-ai-tools 误杀了（它恰好三条全中）。已删除。
#   ② 判据③「无代码/只有 README 就屏蔽」按用户裁定不做：language 为空的条目里全是合法
#      论文/工具清单（showlab/Awesome-Video-Diffusion 5803★ 等 15 条）。
# 两条都是"修老问题造新问题"的形状，锚点留在这里挡住后人重新加回来。
NODE_SCRIPT_SPAM = r"""
import assert from 'node:assert';
const m = await import(new URL('file:///REPO/api/search.js').href);
const spam = m.spamReason;
assert.equal(typeof spam, 'function', 'api/search.js 必须导出 spamReason(原始 item)');

const mk = o => Object.assign({full_name: 'o/r', description: 'normal desc',
                               language: 'Python', topics: [], size: 1000, stargazers_count: 100}, o);

// ── 该杀的 ──
assert.equal(spam(mk({description: 'x'.repeat(64765)})), 'desc_len', '实测靶样本必须剔');
// 边界方向钉死：>400 才剔。实现写成 >= 的话，400 这条会红。
assert.equal(spam(mk({description: 'z'.repeat(400)})), null, '恰好 400 必须放行（钉住 > 与 >= 的方向）');
assert.equal(spam(mk({description: 'z'.repeat(401)})), 'desc_len', '401 必须剔');

// ── 不许杀的（全部是真样本复刻，缺一条判据就跑偏）──
const REAL_KEEP = [
  // 144,081★ 合法清单：topics 顶格 + 长描述 + 无代码语言 —— 判据②的三条它全中
  ['x1xhlol/system-prompts-and-models-of-ai-tools',
   {description: 'p'.repeat(328), language: null, topics: Array(20).fill('t'), stargazers_count: 144081}],
  // 5,803★ 论文清单：无代码语言，判据③单独看它就该杀
  ['showlab/Awesome-Video-Diffusion',
   {description: 'A curated list of recent diffusion models for video generation, editing, and various other applications.',
    language: null, topics: ['a', 'b', 'c'], size: 884, stargazers_count: 5803}],
  // 实测合法上限：350 字符（GitHub UI 顶格写的描述）
  ['legit-max-ui-desc/r', {description: 'q'.repeat(350), language: 'Python'}],
  // 无描述、无 topics 的正常小仓库
  ['bare/repo', {description: null, topics: null, language: null}],
];
for (const [name, o] of REAL_KEEP) {
  assert.equal(spam(mk(Object.assign({full_name: name}, o))), null,
               '合法真样本被误杀 ⇒ 判据越界了: ' + name);
}

// 源码面：保留条目也要截断 + 响应要带可回溯计数
// 锚点写成"字段: 来源表达式"的完整形态 —— 只 /raw_count/ 的话，改名成 raw_count_removed 也能绕过。
const fs = await import('node:fs');
const src = fs.readFileSync(new URL('file:///REPO/api/search.js'), 'utf8');
assert.ok(/raw_count:\s*raw\.length/.test(src),
          '响应的 raw_count 必须来自 GitHub 原始条数：分页判据与「屏蔽可见」都依赖它');
assert.ok(/filtered_out:\s*spamOut\s*\+\s*relevanceOut/.test(src),
          'filtered_out 必须同时计入低质屏蔽与相关性兜底，漏一半就是报少了');
assert.ok(/slice\(0,\s*DESC_KEEP\)/.test(src),
          '保留条目的描述必须截断，否则一条长文本仍能撑爆整页响应');
console.log('ALL_SEARCH_SPAM_OK');
""".replace('REPO', ROOT.replace('\\', '/'))


def test_spam_reason_filters_bait_repos():
    # encoding 必须显式给 utf-8：断言消息是中文，Windows 默认 GBK 会让 reader 线程
    # 直接 UnicodeDecodeError ⇒ stdout 变空串，红是红了但看不到红在哪一条。
    r = subprocess.run(['node', '--input-type=module', '-e', NODE_SCRIPT_SPAM],
                       capture_output=True, text=True, encoding='utf-8', errors='replace',
                       timeout=60, cwd=ROOT)
    out = (r.stdout or '') + (r.stderr or '')
    assert 'ALL_SEARCH_SPAM_OK' in r.stdout, 'node 断言未通过:\n' + out[-2000:]
