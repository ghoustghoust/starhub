# -*- coding: utf-8 -*-
"""Rehearsal: actually EXECUTE repo-trim.yml's shell steps against a synthetic repo.

Why this exists (2026-09-30, pass 2 died twice for reasons a reader cannot see):
  1. I added --verbose to the filter-repo line. The binary rejects it. Reading YAML finds nothing.
  2. That line piped into `tail -40`. Actions default shell has no pipefail, so tail swallowed the
     exit code: step 5 looked green and the job only blew up at step 6's `git remote add origin`
     (filter-repo removes the origin remote only when it succeeds).
Both are "runs red, reads fine" bugs, so the workflow text is executed here, not reviewed.

Scope: everything up to but excluding the push. The push lines are replaced with ':' and the
decision step is fed inputs.dry_run=true, so no remote is ever written by this test.
"""
import io
import os
import re
import shutil
import subprocess
import sys

import pytest

yaml = pytest.importorskip("yaml")

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
YML = os.path.join(ROOT, '.github', 'workflows', 'repo-trim.yml')
GUARD = os.path.join(ROOT, 'tools', 'trim_commit_guard.py')

# '安装 git-filter-repo'：pip/network bound，本地二进制顶上。
# '判据自测…'：这一步的内容就是"跑判据"，合成仓里根本没有 tests/ 目录，
#   排练它必然以 "file or directory not found" 收场 —— 而且它排在最前面，会把
#   "变异体必须被 filter-repo 那步抓住"那条反向判据的注意力引到错误的步骤上（2026-10-01 实测双红）。
SKIP_STEP_NAMES = ('安装 git-filter-repo',
                   '判据自测（trim_guard，destructive 步骤之前的最后一道纸面检查）')


def _bash():
    p = shutil.which('bash')
    if not p:
        return None
    r = subprocess.run([p, '-c', 'command -v comm >/dev/null && command -v awk >/dev/null && echo ok'],
                       capture_output=True, text=True)
    return p if r.stdout.strip() == 'ok' else None


BASH = _bash()
pytestmark = pytest.mark.skipif(BASH is None, reason='need Git Bash with comm/awk to rehearse')


def steps_from_text(text):
    d = yaml.safe_load(text)
    return [s for s in d['jobs']['trim']['steps']
            if s.get('run') and (s.get('name') or '').strip() not in SKIP_STEP_NAMES]


def load_steps():
    return steps_from_text(io.open(YML, encoding='utf-8').read())


def render(script, repo_name='Kwei168/starhub', dry_run='true', confirm=''):
    """Substitute the GitHub expressions the way the runner would, then defuse the push."""
    s = script.replace('${{ github.repository }}', repo_name)
    s = s.replace('${{ inputs.dry_run }}', dry_run)
    s = s.replace('${{ inputs.confirm }}', confirm)
    s = s.replace('${{ secrets.GITHUB_TOKEN }}', 'REHEARSAL_NO_TOKEN')
    s = re.sub(r'(?m)^\s*git push .*$', '  :', s)          # never push from a rehearsal
    s = s.replace('git filter-repo --version', 'git filter-repo --version')
    return s


def env_for(tmpdir):
    e = dict(os.environ)
    e.update({'GIT_AUTHOR_NAME': 'R', 'GIT_AUTHOR_EMAIL': 'r@e',
              'GIT_COMMITTER_NAME': 'R', 'GIT_COMMITTER_EMAIL': 'r@e',
              'PYTHONIOENCODING': 'utf-8', 'PATH': e.get('PATH', '')})
    return e


def bash(cwd, script, env, pipefail=True):
    # 三个独立 argv：把 "-eo pipefail" 当成一个 token 交给 bash 会报 invalid option name
    flags = ['-e', '-o', 'pipefail'] if pipefail else ['-e']
    return subprocess.run([BASH, '--noprofile', '--norc'] + flags + ['-c', script],
                          cwd=str(cwd), env=env, capture_output=True, text=True,
                          encoding='utf-8', errors='replace')


@pytest.fixture()
def fake_repo(tmp_path):
    """一个有源码提交、纯数据提交、空合并的小仓，并配好 origin/main 与真实 origin 远端。"""
    d = str(tmp_path / 'wk')
    os.makedirs(d)
    env = env_for(d)

    def git(*a):
        return subprocess.run(['git'] + list(a), cwd=d, env=env, capture_output=True,
                              text=True, encoding='utf-8', errors='replace')

    assert git('init', '-q', d).returncode == 0
    for name, body in [('keep.py', 'print(1)\n'), ('rss-data-1.js', 'x' * 200 + '\n'),
                       ('rss_history_0.json', '{}\n'), ('rss_cache.json', '{}\n'),
                       ('rss_api_snapshot.json', '{}\n'), ('final.py', 'print(2)\n')]:
        io.open(os.path.join(d, name), 'w', encoding='utf-8').write(body)
        git('add', name)
        assert git('commit', '-q', '-m', 'c %s' % name).returncode == 0
    # 空合并：自身不改任何文件，用来验 "OK-空合并" 那一支
    git('checkout', '-q', '-b', 'side', 'HEAD~3')
    io.open(os.path.join(d, 'side.py'), 'w', encoding='utf-8').write('print(3)\n')
    git('add', 'side.py')
    git('commit', '-q', '-m', 'side commit')
    main = git('rev-parse', '--abbrev-ref', 'HEAD').stdout.strip()
    git('checkout', '-q', main if main in ('main', 'master') else 'master')
    assert git('merge', '-q', '--no-ff', '-m', 'Merge side (no own change)', 'side').returncode == 0

    origin = str(tmp_path / 'origin.git').replace('\\', '/')
    assert git('init', '-q', '--bare', origin).returncode == 0
    git('remote', 'add', 'origin', origin)
    br = git('rev-parse', '--abbrev-ref', 'HEAD').stdout.strip()
    assert git('push', '-q', '-u', 'origin', 'HEAD:refs/heads/main').returncode == 0
    # CI 里仓库本身就带着判据模块，排练必须照搬进合成仓，否则最后一步找不到文件——
    # 那是排练的缺陷，不是 workflow 的缺陷，别让它冒充成红。
    os.makedirs(os.path.join(d, 'tools'))
    shutil.copy(GUARD, os.path.join(d, 'tools', 'trim_commit_guard.py'))
    return d, env, br


def test_rehearsal_runs_every_step_green(fake_repo):
    d, env, br = fake_repo
    n_before = int(subprocess.run(['git', 'rev-list', '--count', 'HEAD'], cwd=d, env=env,
                                  capture_output=True, text=True).stdout.strip())
    for st in load_steps():
        r = bash(d, render(st['run']), env)
        assert r.returncode == 0, '[%s] rc=%s\nSTDOUT\n%s\nSTDERR\n%s' % (
            st.get('name'), r.returncode, r.stdout[-2500:], r.stderr[-1200:])
        blob = r.stdout
        if '重写前' in (st.get('name') or '') or '记录基线' in (st.get('name') or ''):
            assert 'commits_before 行数 =' in blob and 'commits_parents 行数 =' in blob, blob[-1500:]
        if '决策' in (st.get('name') or ''):
            assert u'闸零通过' in blob, blob[-2500:]
            assert 'dry-run' in blob, blob[-800:]
            real = [l for l in blob.split('\n') if '::error::' in l and 'echo ' not in l]
            assert not real, real
    tree = subprocess.run(['git', 'ls-tree', '-r', '--name-only', 'HEAD'], cwd=d, env=env,
                          capture_output=True, text=True).stdout
    for keep in ('keep.py', 'final.py', 'side.py'):
        assert keep in tree, '%s 不该被剔掉' % keep
    for gone in ('rss-data-1.js', 'rss_history_0.json', 'rss_cache.json', 'rss_api_snapshot.json'):
        assert gone not in tree, '%s 应已从 tip 消失' % gone
    n_after = int(subprocess.run(['git', 'rev-list', '--count', 'HEAD'], cwd=d, env=env,
                                 capture_output=True, text=True).stdout.strip())
    assert n_after < n_before, '纯数据提交应被裁掉（before=%s after=%s）' % (n_before, n_after)


def test_rehearsal_catches_unknown_filter_repo_option(fake_repo):
    """变异体一：把 --verbose 塞回真 workflow 文本，排练必须当场红。第二次失败就是这么来的。"""
    d, env, br = fake_repo
    src = io.open(YML, encoding='utf-8').read()
    mutated = src.replace('git filter-repo --force --invert-paths',
                          'git filter-repo --force --verbose --invert-paths', 1)
    assert mutated != src, '变异锚点没命中，本测试无从证明'
    hit = None
    for st in steps_from_text(mutated):
        r = bash(d, render(st['run']), env)
        if r.returncode != 0:
            hit = (st.get('name'), r.stderr + r.stdout)
            break
    assert hit is not None, '变异后的 workflow 仍全程跑绿 => 排练没在执行那行命令'
    name, out = hit
    assert 'unrecognized arguments' in out, '在 [%s] 红了但原因不是不认的开关：%s' % (name, out[:600])


def test_pipefail_line_is_load_bearing(fake_repo):
    """变异体二：证明 `set -eo pipefail` 是承重墙而不是装饰。

    同一条坏命令，管道接 tail：没有 pipefail 时 rc==0（失败被吞，就是第二趟第 5 步"绿"的机理），
    加了 pipefail 才红。这条测试钉的是"那行 set 不许被删"。
    """
    d, env, br = fake_repo
    bad = ("git filter-repo --force --verbose --invert-paths --path-glob 'rss-data-*' | tail -5")
    loose = bash(d, bad, env, pipefail=False)
    tight = bash(d, 'set -eo pipefail\n' + bad, env, pipefail=True)
    assert loose.returncode == 0, (
        '对照组不成立：没有 pipefail 时这条命令已经红了，说明 tail 没吞退出码，本判据不该冒充证据')
    assert tight.returncode != 0, '加了 pipefail 仍不红 => pipefail 那行是装饰'
    src = io.open(YML, encoding='utf-8').read()
    for step_body in [s['run'] for s in load_steps() if 'filter-repo --force' in s['run']]:
        assert 'pipefail' in step_body, 'filter-repo 那步带管道却没有 pipefail'


def test_workflow_does_not_reintroduce_banned_option():
    """静态兜底：命令行里不许再出现 --verbose（注释里提到它是要留教训，不算）。"""
    src = io.open(YML, encoding='utf-8').read()
    for raw in src.split('\n'):
        if raw.lstrip().startswith('#'):
            continue
        if '--verbose' in raw.split('#', 1)[0]:
            pytest.fail('命令行（非注释）里出现 --verbose：' + raw.strip())


def test_skip_list_cannot_absorb_a_real_step():
    """反向判据：SKIP 只能装"碰不到仓"的步骤，且每一项必须仍指向真实存在的步骤。

    上一跳我把"跑判据"那步塞进排练，双红（合成仓没有 tests/，而它排在最前会把变异体的
    注意力从 filter-repo 那步引开）。把它排除是对的；但排除表本身是个后门 ——
    谁哪天把 `filter-repo`/`git push` 那两步塞进来，排练就"全绿"了却什么都没验。
    所以这里钉两条：跳过项的内容不许涉及重写/推送；写错名字（步骤改名后残留）当场红。
    """
    real = {(s.get('name') or '').strip()
            for s in yaml.safe_load(io.open(YML, encoding='utf-8').read())
            ['jobs']['trim']['steps'] if s.get('run')}
    stale = [n for n in SKIP_STEP_NAMES if n not in real]
    assert not stale, 'SKIP_STEP_NAMES 里有已不存在的步骤名（步骤被改名/删除）：%s' % stale
    by_name = {(s.get('name') or '').strip(): s['run']
               for s in yaml.safe_load(io.open(YML, encoding='utf-8').read())
               ['jobs']['trim']['steps'] if s.get('run')}
    for n in SKIP_STEP_NAMES:
        body = by_name[n]
        for banned in ('filter-repo --force', 'git push', 'update-ref', 'force-with-lease'):
            assert banned not in body, (
                '被跳过的步骤 [%s] 其实会动仓（命中 %s）=> 它必须在排练里真跑，'
                '否则"每步都绿"这句话是假的' % (n, banned))
