#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""size_tripwire —— 拦住"某个进 git 的文件又开始无上限长大"。

为什么需要它（2026-09-30 存量清理的直接教训）：远端 5.21 GiB 里 84% 是四族没有体积上限的
产物（rss-data-1.js 每场 55MB，rss_history / rss_api_snapshot / rss_cache 同理），
它们静静长了一年多，没有任何一道检查会红。轮转窗口（7 天 / 14 天 / 条数上限）是各自构建
脚本里的行为，一旦谁把窗口调大、或新增一个不设限的产物，git 侧不会有任何人喊停。

这道闸只看"这一场将要提交的东西"，不看历史：
  - 单个文件 > 16 MiB ⇒ 判红（轮转破了才会出现的形状；当前最大的是 hot_history 约 8.3 MiB）
  - 本场入仓合计 > 25 MiB ⇒ 只报警不拦（数据正常波动不该冻结部署，但趋势要有人看见）
判红会挡住 Commit 与后续部署，这是故意的：宁可少发一场，也不要每天再写 55MB 进不可回收的历史。

用法（在仓库根、git add 之后 git commit 之前）：python3 tools/size_tripwire.py
退出码 0 = 可以提交；1 = 有文件超限，或读不到暂存区（判据失明时不放行）。
"""
import argparse
import subprocess
import sys

MB = 1024 * 1024


def _git(args, cwd=None, inp=None):
    r = subprocess.run(['git'] + list(args), cwd=cwd, input=inp, capture_output=True,
                       text=True, encoding='utf-8', errors='replace')
    if r.returncode != 0:
        raise RuntimeError('git %s 失败 rc=%s：%s' % (' '.join(args[:2]), r.returncode,
                                                     (r.stderr or '').strip()[:200]))
    return r.stdout


def staged_blobs(cwd=None):
    """返回 [(路径, 字节数)]，体积取**暂存区里那份 blob**，不是工作目录的文件。

    为什么要区分：工作目录可能躺着别人未提交的改动，而这场真正写进不可回收历史的是 index。
    为什么不用 `git cat-file -s ':(path)'`：路径魔法会把括号当字面量，实测直接 fatal；
      所以一律走 `-z` 的机器可读输出 + `--batch-check`。
    读不到就抛错，绝不返回空表：空表会被调用方读成"本场没文件"= 放行，
      而"git 调用失败"与"确实没有文件"是两回事（闸零同一天刚为这个形状判过一次假红）。
    """
    changed = [p for p in _git(['diff', '--cached', '--name-only', '-z'],
                               cwd=cwd).split('\0') if p.strip()]
    if not changed:
        return []
    idx = {}
    for rec in _git(['ls-files', '--stage', '-z'], cwd=cwd).split('\0'):
        if not rec.strip():
            continue
        meta, _, path = rec.partition('\t')
        bits = meta.split(' ')
        if len(bits) > 1 and bits[1]:
            idx[path] = bits[1]
    sizes = {}
    shas = [idx[p] for p in changed if p in idx]
    if shas:
        out = _git(['cat-file', '--batch-check'], cwd=cwd, inp='\n'.join(shas) + '\n')
        for line in out.splitlines():
            bits = line.split(' ')
            if len(bits) >= 3 and bits[1] == 'blob' and bits[2].isdigit():
                sizes[bits[0]] = int(bits[2])
    res, missing = [], []
    for p in changed:
        sha = idx.get(p)
        if sha is None:
            continue                     # 本场删除的文件：不进历史，不贡献体积
        if sha not in sizes:
            missing.append(p)
            continue
        res.append((p, sizes[sha]))
    if missing:
        raise RuntimeError('%d 个暂存路径取不到 blob 体积（如 %s）' % (len(missing), missing[0]))
    return res


def check(pairs, per_file_mb=16.0, total_mb=25.0):
    """-> (超限文件, 告警文本, 报表行)。超限文件非空就该判红。"""
    big = [(p, s) for p, s in pairs if s > per_file_mb * MB]
    total = sum(s for _, s in pairs)
    warn = []
    if total > total_mb * MB:
        warn.append('本场入仓合计 %.1f MiB，超过 %.0f MiB 的观察线（不拦，但趋势要有人看见）'
                    % (total / MB, total_mb))
    lines = ['   %10.2f MiB  %s' % (s / MB, p)
             for p, s in sorted(pairs, key=lambda x: -x[1])[:8]]
    return big, warn, lines


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument('--per-file-mb', type=float, default=16.0)
    ap.add_argument('--total-mb', type=float, default=25.0)
    ap.add_argument('--allow-paths', default='',
                    help='逗号分隔的前缀：这些路径 exempt（必须显式声明，不许静默放行）')
    a = ap.parse_args(argv)
    try:
        pairs = staged_blobs()
    except RuntimeError as e:
        # 读不到就是判据失明。闸瞎着的时候必须挡住提交，而不是放行
        print('::error::体积闸读不到暂存区：%s' % e)
        return 1
    exempt = [p.strip() for p in a.allow_paths.split(',') if p.strip()]
    pairs = [(p, s) for p, s in pairs if not any(p.startswith(e) for e in exempt)]
    if not pairs:
        print('体积闸：本场暂存区没有新增体积（无文件，或全部在 exempt 名单里）')
        return 0
    big, warn, lines = check(pairs, a.per_file_mb, a.total_mb)
    print('体积闸：%d 个文件合计 %.2f MiB（单文件上限 %.0f MiB）' % (
        len(pairs), sum(s for _, s in pairs) / MB, a.per_file_mb))
    print('\n'.join(lines))
    for p, s in big:
        print('::error::%s 单文件 %.2f MiB 超过 %.0f MiB 上限 —— '
              '轮转窗口破了，或有新增的不设限产物，绝不入仓（git 历史不可回收，手册 §8.15）'
              % (p, s / MB, a.per_file_mb))
    for w in warn:
        print('::warning::' + w)
    return 1 if big else 0


if __name__ == '__main__':
    sys.exit(main())
