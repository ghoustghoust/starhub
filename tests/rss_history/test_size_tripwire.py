# tests/rss_history/test_size_tripwire.py
# -*- coding: utf-8 -*-
"""体积闸 tools/size_tripwire.py 的判据 + 它在 update.yml 里的接线。

为什么钉这条：5.21 GiB 存量里 84% 是四族**没有体积上限**的产物（rss-data-1.js 每场 55MB 等），
它们静静长了一年多，没有任何检查会红。轮转窗口是各构建脚本里的行为，谁把窗口调大、
或新增一个不设限的产物，git 侧不会有人喊停 —— 这道闸就是把"喊停"补上。

必须用真 git 真暂存区跑：判据读的是 **index 里的 blob 体积**，不是工作目录，
fixture 造不出来（照着想象造 fixture 是这个项目反复踩过的坑）。
"""
import io
import os
import subprocess
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
sys.path.insert(0, os.path.join(ROOT, 'tools'))
import size_tripwire as T  # noqa: E402

TOOL = os.path.join(ROOT, 'tools', 'size_tripwire.py')
WF = os.path.join(ROOT, '.github', 'workflows', 'update.yml')


# CI 的全局 git 配置里没有 user.name/user.email（workflow 只在 Commit 步里现设），
# 而本地有 —— 夹具若依赖环境身份就会"本地绿、CI 红"（CLAUDE.md 明写这条不许依赖环境）。
# 所以身份必须由夹具自己显式给，且用 -c 只作用于这一条命令，不碰任何全局配置。
_ID = ['-c', 'user.name=T', '-c', 'user.email=t@e']


def _git(cwd, *a):
    r = subprocess.run(['git'] + list(a), cwd=cwd, capture_output=True, text=True,
                       encoding='utf-8', errors='replace')
    assert r.returncode == 0, (a, r.stdout[-300:], r.stderr[-300:])
    return r.stdout


def _repo(tmp_path):
    d = str(tmp_path)
    _git(d, 'init', '-q', '-b', 'main', d)
    # 真 CI 永远有 HEAD；`git diff --cached` 在 unborn HEAD 上会失败，
    # 所以夹具必须先有一条基线提交，否则测的是仓库状态而不是判据。
    _stage(d, 'seed.txt', 8)
    _git(d, *_ID, 'commit', '-q', '-m', 'seed')
    return d


def _stage(d, name, size):
    p = os.path.join(d, name)
    with open(p, 'wb') as f:
        f.write(b'x' * size)
    _git(d, 'add', name)
    return p


def _run(d, *args):
    env = dict(os.environ)
    env['PYTHONIOENCODING'] = 'utf-8'
    r = subprocess.run([sys.executable, TOOL] + list(args), cwd=d, env=env,
                       capture_output=True, text=True, encoding='utf-8', errors='replace')
    return r.returncode, r.stdout + r.stderr


def test_small_staged_file_passes(tmp_path):
    d = _repo(tmp_path)
    _stage(d, 'a.py', 2048)
    rc, out = _run(d)
    assert rc == 0, out
    assert '合计' in out


def test_oversized_staged_file_blocks(tmp_path):
    """单文件超过上限必须判红并点名 —— 这就是 55MB 分块那类事故的形状。"""
    d = _repo(tmp_path)
    _stage(d, 'big.js', int(17 * 1024 * 1024))
    rc, out = _run(d)
    assert rc == 1, out
    assert '::error::' in out and 'big.js' in out


def test_total_over_observer_line_only_warns(tmp_path):
    """总量过观察线只报警不拦：数据 legitimately 波动不该冻结部署。"""
    d = _repo(tmp_path)
    for i in range(4):
        _stage(d, 'part%d.bin' % i, int(8 * 1024 * 1024))   # 合计 32MiB，单个 8MiB
    rc, out = _run(d)
    assert rc == 0, out
    assert '::warning::' in out and '::error::' not in out


def test_exempt_must_be_declared(tmp_path):
    d = _repo(tmp_path)
    _stage(d, 'big.js', int(17 * 1024 * 1024))
    rc_blocked, _ = _run(d)
    rc_ok, out = _run(d, '--allow-paths', 'big.js')
    assert rc_blocked == 1
    assert rc_ok == 0, out


def test_reads_index_not_worktree(tmp_path):
    """工作目录里再改大不算，判的是这场真正要进历史的 blob。"""
    d = _repo(tmp_path)
    p = _stage(d, 'a.bin', int(1 * 1024 * 1024))
    with open(p, 'wb') as f:            # 暂存之后把磁盘上的文件撑到 20MB，但不 git add
        f.write(b'y' * int(20 * 1024 * 1024))
    rc, out = _run(d)
    assert rc == 0, out
    assert '1.00 MiB' in out, out


def test_unreadable_index_blocks(tmp_path):
    """读不到暂存区（这里用非仓库目录演）必须 rc=1，不许被读成"本场没文件"= 放行。

    这条就是闸零同一天踩过的形状：把"读漏"当成"没有"，干净的重写会被判成假红，
    反过来瞎掉的闸会把无上限产物当成合格放行。
    """
    d = str(tmp_path)
    rc, out = _run(d)
    assert rc == 1, out
    assert '读不到暂存区' in out and '::error::' in out


def test_staged_blobs_raises_instead_of_returning_empty(tmp_path):
    """直接钉 `staged_blobs()` 本身：读不到要抛 RuntimeError，**不许返回空表**。

    只在 main 的退出码上断言是不够的 —— 把 `_git` 的 raise 拆掉之后，
    空表会一路走到"本场没文件"那支照样放行。这条打在函数边界上，
    任何"咽下 git 失败"的改法都会当场红。
    """
    with pytest.raises(RuntimeError):
        T.staged_blobs(cwd=str(tmp_path))
    # 而空仓库（有 HEAD、没暂存改动）返回的确实是空表 —— 两者必须是不同路径
    d = _repo(tmp_path)
    assert T.staged_blobs(cwd=d) == []


def test_wired_into_both_commit_branches_before_commit():
    """正常分支与 push 冲突重试分支都要有这道闸，且必须排在 git commit 之前。

    只接一处 = 冲突重试那场照样能把无上限产物写进历史，而重试恰恰是数据最容易堆大的时候。
    """
    text = io.open(WF, encoding='utf-8').read()
    lines = text.splitlines()
    hits = [i for i, l in enumerate(lines) if 'size_tripwire.py' in l]
    assert len(hits) >= 2, "体积闸只接进了 %d 处 add 分支（要 2 处）" % len(hits)
    for i in hits:
        after = "\n".join(lines[i:])
        assert 'git commit' in after.split('if ! python3 tools/size_tripwire.py')[0] or \
            'git commit' in after[:after.find('exit 1')] or 'git commit' in after, \
            "体积闸后面找不到 git commit，接线位置不对（第 %d 行）" % (i + 1)
        assert 'exit 1' in "\n".join(lines[i:i + 4]), \
            "体积闸判红后必须 exit 1 挡住提交（第 %d 行）" % (i + 1)
