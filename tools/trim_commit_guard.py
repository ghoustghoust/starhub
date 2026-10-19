#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""trim_commit_guard —— 证明"历史重写后少掉的那些提交，每一个都没有带走人写的代码"。

为什么需要它（2026-09-30 实测逼出来的）：repo-trim 的 dry-run 里 `.git` 从 5.3G 掉到 368M 很漂亮，
但提交数 2043 -> 2041 悄悄少了 2 条。原先我只钉了"路径不许少"——路径守恒对提交守恒毫无约束：
git-filter-repo 默认把改动全落在被剔族里的提交当退化提交丢掉。少 2 条可以接受，
前提是逐条说得清"少的是谁、它当时改过哪些文件"。所以这道闸不问"少了几条"，问每一条少的下落。

"少的是谁"以 .git/filter-repo/commit-map 为准，不按 (提交时间, 标题) 配对。第二趟就是这么翻车的：
真删 2 条（逐条查过都是纯数据），按标题+时间却数出 7 条"消失"，于是 2041+7 != 2043，
一次干净的重写被判成假红。当天用真实 2043 条消息/作者/原始日期复刻一遍真 filter-repo：
按 sha 走 map 恰好 2 条标删、键配对也恰好 2 条——漂移不在真实数据里，在配对这条路本身。
map 是 filter-repo 自己按 sha 逐条写的（被删的写全零 sha），不需要猜身份，也没有第二套计数。

用法（在仓库根、重写之后运行）：python3 tools/trim_commit_guard.py
前置（repo-trim.yml 的"记录基线"步骤必须在重写之前抓好）：
    git log --format='C|%H|%cI|%s' --no-renames --name-only > .commits_before.txt
    git log --format='P|%H|%p'                            > .commits_parents.txt
退出码 0 = 每一条消失都可解释；1 = 说不清（或判据自己失明），绝不许推送。
"""
import argparse
import io
import os
import subprocess
import sys

KEEP_ALWAYS = ()  # 批 5b 起 chunk 0 也不再入库；原注释"真实分块测试读已入库副本"经实测失真：
#   tests/rss_composite/test_diverse_realdata.py:32 读的是工作目录那份（该目录未接进任何门禁步），
#   没有任何测试用 git 取库内副本 ⇒ 把它留在白名单只会永久挡住这批历史 blob 的回收。
PRUNED_PREFIX = ('rss-data-', 'rss_history', 'rss_api_snapshot')
PRUNED_EXACT = ('rss_cache.json',)
DELETED = '0' * 40                        # filter-repo 在 map 里给被删提交写的占位 sha


class GitError(Exception):
    """git 子进程非零退出。老实现只取 stdout、把 rc 与 stderr 全丢了，
    于是"读到一半失败"和"提交真的没了"在代码里长得一模一样。"""

    def __init__(self, cmd, rc, err):
        Exception.__init__(self, 'git 调用失败 rc=%s：%s%s' % (
            rc, cmd, ('\n' + err.strip()[:400]) if err and err.strip() else ''))


def is_pruned(path):
    """这个路径是不是本次重写要剔掉的死数据族。chunk 0 明确不算。"""
    if path in KEEP_ALWAYS:
        return False
    if path in PRUNED_EXACT:
        return True
    return path.startswith(PRUNED_PREFIX)


def parse_before(text):
    """.commits_before.txt -> [{'sha','key':(committer_iso, subject),'files':[...]}]

    由 `git log --format='C|%%H|%%cI|%%s' --no-renames --name-only` 产生：一行 C| 头 + 若干文件名 + 空行。
    标题里出现 '|' 不会被切碎（split 限 3 次，标题整段落在最后一项）。
    基线必须 --no-renames：开了重命名检测，一次 delete+add 会被并成一条改名、只报目标名，
    "带走了源码"的提交就能被读成"只改了数据文件"。
    merge 提交 git 默认不列文件，所以 files 可能为空——空不等于没问题，另按父提交数判定。
    """
    out, cur = [], None
    for line in text.split('\n'):
        if line.startswith('C|'):
            p = line.split('|', 3)
            if len(p) < 4:
                continue
            cur = {'sha': p[1], 'key': (p[2], p[3]), 'files': []}
            out.append(cur)
        elif cur is not None and line.strip():
            cur['files'].append(line.strip())
    return out


def parse_parents(text):
    """.commits_parents.txt（`P|<sha>|<父 sha...>`）-> {sha: [父, ...]}；空列表=根提交，>=2=merge"""
    m = {}
    for line in text.split('\n'):
        if not line.startswith('P|'):
            continue
        p = line.split('|', 2)
        if len(p) < 3:
            continue
        m[p[1]] = p[2].split() if p[2].strip() else []
    return m


def parse_commit_map(text):
    """.git/filter-repo/commit-map -> {旧 sha: 新 sha 或 DELETED}

    首行是表头（`old new`，且随语言环境翻译，所以无条件跳过）。此后一行一条提交，
    覆盖 filter-repo 这次处理的每一条——不需要它"改过"，只要它在历史里。
    """
    rows = [l for l in text.split('\n') if l.strip()]
    if len(rows) < 2:
        raise ValueError('commit-map 只有 %d 行，没读到任何 old->new 映射' % len(rows))
    m = {}
    for line in rows[1:]:
        p = line.split()
        if len(p) != 2 or len(p[0]) != 40 or len(p[1]) != 40:
            raise ValueError('commit-map 有读不懂的行：%r' % line[:80])
        m[p[0]] = p[1]
    return m


def deleted_shas(mapping):
    return [old for old, new in mapping.items() if new == DELETED]


def verdict(rec, parents):
    """一条消失的提交 -> ('OK-*'|'BAD-*', 理由)。"""
    files = rec['files']
    if rec['sha'] not in parents:
        return 'BAD-父清单缺这条', '基线没抓到它的父提交，无法判断是不是合并'
    ps = parents[rec['sha']]
    if files and all(is_pruned(f) for f in files):
        return 'OK-纯数据', '%d 个文件全在被剔族里' % len(files)
    if not files and len(ps) >= 2:
        return 'OK-空合并', 'merge 且自身不改任何文件，两个父都在世'
    if not files:
        return 'BAD-无文件非合并', '不是 merge 却不列文件，说不清它带走了什么'
    return 'BAD-含非数据文件', '留下了 %s' % ', '.join(f for f in files if not is_pruned(f))[:120]


def git(args):
    r = subprocess.run(['git'] + list(args), capture_output=True,
                       text=True, encoding='utf-8', errors='replace')
    if r.returncode != 0:
        raise GitError(' '.join(args), r.returncode, r.stderr)
    return r.stdout


def read(path, what):
    if not os.path.exists(path):
        raise IOError(what)
    return io.open(path, encoding='utf-8', errors='replace').read()


def main(argv=None):
    # 输出必须是 UTF-8：Windows 控制台默认 cp936，中文判定标签会糊成一屏 U+FFFD，
    # 让读日志的人（和读子进程输出的测试）都看不见真相。
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument('--before', default='.commits_before.txt')
    ap.add_argument('--parents', default='.commits_parents.txt')
    ap.add_argument('--commit-map', default='.git/filter-repo/commit-map')
    a = ap.parse_args(argv)
    for f in (a.before, a.parents):
        if not os.path.exists(f):
            print('::error::缺基线清单 %s（应在重写前由"记录基线"步骤生成），判据无法成立，绝不推送' % f)
            return 1
    if not os.path.exists(a.commit_map):
        # 缺 map 不等于"一条没删"。那是判据瞎了——不许把读不到当成通过。
        print('::error::缺 commit-map %s（filter-repo 重写后必然留下 old->new 映射），'
              '没人说清哪几条被删，绝不推送' % a.commit_map)
        return 1
    try:
        before = parse_before(read(a.before, 'before'))
        parents = parse_parents(read(a.parents, 'parents'))
        mapping = parse_commit_map(read(a.commit_map, 'commit-map'))
    except (IOError, ValueError) as e:
        print('::error::清单读不出来：%s，判据失明，绝不推送' % e)
        return 1

    # 三份事实必须同源：父清单与基线逐条对齐，map 必须覆盖基线的每一条 sha
    if len(parents) != len(before):
        print('::error::父清单 %d 条 != 基线提交 %d 条，两份清单不同源，判据失明，绝不推送' % (
            len(parents), len(before)))
        return 1
    blind = [r['sha'] for r in before if r['sha'] not in mapping]
    if blind:
        print('::error::commit-map 没覆盖基线的 %d 条（如 %s），说不清它们去了哪里，绝不推送' % (
            len(blind), ', '.join(s[:10] for s in blind[:5])))
        return 1
    gone = [r for r in before if mapping[r['sha']] == DELETED]
    try:
        n_after = int((git(['rev-list', '--count', 'HEAD']).strip() or '0'))
    except (GitError, ValueError) as e:
        print('::error::读不到重写后的提交数：%s，绝不推送' % e)
        return 1
    print('基线清单 %d 条，父清单 %d 条，commit-map %d 条；重写后 HEAD %d 条，map 判删 %d 条' % (
        len(before), len(parents), len(mapping), n_after, len(gone)))
    # 两条独立来源（map 与 HEAD 实际可达数）必须互相对上，对不上就是判据失明而不是重写有问题
    if len(before) != n_after + len(gone):
        print('::error::条数不闭合（基线 %d != 重写后 %d + map 判删 %d），两份事实对不上，绝不推送' % (
            len(before), n_after, len(gone)))
        return 1
    print('重写后消失 %d 条，逐条判定：' % len(gone))
    bad = []
    for rec in gone:
        tag, why = verdict(rec, parents)
        print('   [%-16s] %s %s  %s' % (tag, rec['sha'][:10], rec['key'][0][:19], why))
        if tag.startswith('BAD'):
            bad.append(rec['sha'])
    if bad:
        print('::error::有 %d 条提交带走了不该带的东西：%s —— 绝不推送' % (
            len(bad), ','.join(x[:10] for x in bad)))
        return 1
    if not gone:
        print('   （一条没少，提交数守恒）')
    print('闸零通过：消失的提交全部可解释为纯数据或空合并')
    return 0


if __name__ == '__main__':
    sys.exit(main())
