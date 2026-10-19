# -*- coding: utf-8 -*-
"""tools/trim_commit_guard.py 的判据测试：必须先能红，才许它当闸。

覆盖五种真实事故形状：
  1) 纯数据提交消失 -> 放行（filter-repo 退化裁剪的正常结果）
  2) 带走源码的提交消失 -> 判红（"不许拿丢历史换速度"的下界）
  3) 基线/父清单/map 任一份缺或不同源、map 与 rev-list 不闭合 -> 判据自己失明时必须拒绝放行，
     而不是"什么都没发生"地返回 0
  4) "消失的是谁"一律以 .git/filter-repo/commit-map 为准 —— 2026-09-30 第二趟实测：
     基线 2043、重写后 rev-list 2041（真删 2 条，逐条查过都是纯数据），而按 (%cI,%s) 配对
     数出 7 条"消失"（2041+7 != 2043），一次干净的重写被自己的判据判成假红。
     同一天用真实 2043 条消息/作者/原始日期复刻 + 真 filter-repo 跑完：消失恰好 2 条、
     凭空多出 0，map 标删的也是那 2 条 —— 键配对这条路在真实数据上本就多算，
     而且它的 git() 只取 stdout、把 rc 与 stderr 全丢了，读漏与"提交真没了"长得一模一样。
     所以这一族测试的核心是：**标题和时间怎么错乱都不许影响判定，只有 map 说话算数**。
  5) 真 git + 真 filter-repo 跑一遍，钉住 %p / --name-only / commit-map 的格式假设，
     并钉住基线命令必须 --no-renames（开重命名检测会把一次 delete+add 并成一条改名、
     只报目标名，让"带走了源码"的提交被读成"只改了数据文件"= 假绿放行）
"""
import contextlib
import io
import os
import subprocess
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
sys.path.insert(0, os.path.join(ROOT, 'tools'))
import trim_commit_guard as G  # noqa: E402

GUARD = os.path.join(ROOT, 'tools', 'trim_commit_guard.py')
ZERO = '0' * 40


def _before(*recs):
    """按 `git log --format='C|%H|%cI|%s' --name-only` 的形状造清单。"""
    lines = []
    for sha, date, subj, files in recs:
        lines.append('C|%s|%s|%s' % (sha, date, subj))
        lines.extend(files)
        lines.append('')
    return '\n'.join(lines)


def _parents(*pairs):
    """按 `git log --format='P|%H|%p'` 的形状造父清单：P|<sha>|<父 sha...>"""
    return ''.join('P|%s|%s\n' % (sha, ' '.join(ps)) for sha, ps in pairs)


def _map(*pairs):
    """按 .git/filter-repo/commit-map 的形状造映射：一行表头 + `old new`。"""
    return 'old                                      new\n' + ''.join(
        '%-40s %s\n' % (o, n) for o, n in pairs)


def _run_guard(before, parents, commit_map, count='0', git_fails=False):
    """只测判定逻辑：把 rev-list 换成假读数，stdout 收进 StringIO。

    git_fails=True 演"git 子进程半路失败"这一档：一次中途失败过去会被读成
    "少了几条提交"，因为老实现的 git() 只取 stdout。
    """
    def fake_git(args):
        if git_fails:
            raise G.GitError(' '.join(args), 128, 'fatal: bad object HEAD')
        if args[:2] == ['rev-list', '--count']:
            return count + '\n'
        return ''

    real = G.git
    G.git = fake_git
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            rc = G.main(['--before', str(before), '--parents', str(parents),
                         '--commit-map', str(commit_map)])
    finally:
        G.git = real
    return rc, buf.getvalue()


def test_pruned_path_rules():
    assert G.is_pruned('rss-data-1.js') is True
    assert G.is_pruned('rss_history_3.json') is True
    assert G.is_pruned('rss_api_snapshot.json') is True
    assert G.is_pruned('rss_cache.json') is True
    # chunk 0 是故意留在库里的，判据不能把它算成"该消失"
    assert G.is_pruned('rss-data-0.js') is True  # 批 5b：chunk 0 退出 git，历史里的旧副本可回收
    assert G.is_pruned('build_rss_aggregator.py') is False


def test_parse_before_keys_records_by_sha():
    recs = G.parse_before(_before(('a' * 40, '2026-09-30T10:00:00+08:00',
                                   'fix: a|b 管道标题', ['x.py'])))
    assert len(recs) == 1
    assert recs[0]['sha'] == 'a' * 40
    assert recs[0]['key'] == ('2026-09-30T10:00:00+08:00', 'fix: a|b 管道标题')
    assert recs[0]['files'] == ['x.py']


def test_parse_commit_map_reads_zero_hash_as_deleted():
    m = G.parse_commit_map(_map(('b' * 40, 'c' * 40), ('d' * 40, ZERO)))
    assert m['b' * 40] == 'c' * 40
    assert G.deleted_shas(m) == ['d' * 40]


def test_dropped_data_only_is_allowed(tmp_path):
    b = tmp_path / '.commits_before.txt'
    p = tmp_path / '.commits_parents.txt'
    cm = tmp_path / 'commit-map'
    b.write_text(_before(('b' * 40, '2026-09-30T10:00:00+08:00',
                          'chore(ci): 数据自提', ['rss-data-1.js', 'rss_cache.json'])), encoding='utf-8')
    p.write_text(_parents(('b' * 40, ['s1', 's2'])), encoding='utf-8')
    cm.write_text(_map(('b' * 40, ZERO)), encoding='utf-8')
    rc, out = _run_guard(b, p, cm, count='0')
    assert rc == 0, out
    assert 'OK-纯数据' in out
    assert '闸零通过' in out


def test_dropped_source_commit_is_rejected(tmp_path):
    """变异体：map 说这条被删了，而它改了源码 —— 这条必须打死，否则整道闸是装饰品。"""
    b = tmp_path / '.commits_before.txt'
    p = tmp_path / '.commits_parents.txt'
    cm = tmp_path / 'commit-map'
    b.write_text(_before(('c' * 40, '2026-09-30T11:00:00+08:00',
                          'feat: 真实功能改动', ['build_rss_aggregator.py', 'rss-data-1.js'])), encoding='utf-8')
    p.write_text(_parents(('c' * 40, ['s1'])), encoding='utf-8')
    cm.write_text(_map(('c' * 40, ZERO)), encoding='utf-8')
    rc, out = _run_guard(b, p, cm, count='0')
    assert rc == 1, out
    assert 'BAD-含非数据文件' in out
    assert '::error::' in out


def test_duplicate_titles_and_dates_cannot_trick_the_gate(tmp_path):
    """2026-09-30 假红的形状：三条提交标题相同、时间相同，按 (%cI,%s) 配对必然多算。
    map 说只删了一条纯数据，判据必须照放，且消失数只能是 1。
    """
    b = tmp_path / '.commits_before.txt'
    p = tmp_path / '.commits_parents.txt'
    cm = tmp_path / 'commit-map'
    same = '2026-09-30T12:00:00+08:00'
    s1, s2, s3 = '11' + '0' * 38, '22' + '0' * 38, '33' + '0' * 38
    b.write_text(_before(
        (s1, same, 'chore: auto update stars', ['index.html']),
        (s2, same, 'chore: auto update stars', ['index.html']),
        (s3, same, 'chore: auto update stars', ['rss_history.json']),
    ), encoding='utf-8')
    p.write_text(_parents((s1, ['x']), (s2, ['x']), (s3, ['x'])), encoding='utf-8')
    cm.write_text(_map((s1, 'a1' + '0' * 38), (s2, 'a2' + '0' * 38), (s3, ZERO)), encoding='utf-8')
    rc, out = _run_guard(b, p, cm, count='2')
    assert rc == 0, out
    assert '消失 1' in out, out
    assert 'BAD' not in out, out


def test_empty_non_merge_without_files_is_rejected(tmp_path):
    b = tmp_path / '.commits_before.txt'
    p = tmp_path / '.commits_parents.txt'
    cm = tmp_path / 'commit-map'
    b.write_text(_before(('d' * 40, '2026-09-30T12:00:00+08:00', 'x', [])), encoding='utf-8')
    p.write_text(_parents(('d' * 40, ['s1'])), encoding='utf-8')
    cm.write_text(_map(('d' * 40, ZERO)), encoding='utf-8')
    rc, out = _run_guard(b, p, cm, count='0')
    assert rc == 1, out
    assert 'BAD-无文件非合并' in out


def test_empty_merge_without_files_is_allowed(tmp_path):
    b = tmp_path / '.commits_before.txt'
    p = tmp_path / '.commits_parents.txt'
    cm = tmp_path / 'commit-map'
    b.write_text(_before(('e' * 40, '2026-09-30T12:30:00+08:00', 'Merge branch x', [])), encoding='utf-8')
    p.write_text(_parents(('e' * 40, ['s1', 's2'])), encoding='utf-8')
    cm.write_text(_map(('e' * 40, ZERO)), encoding='utf-8')
    rc, out = _run_guard(b, p, cm, count='0')
    assert rc == 0, out
    assert 'OK-空合并' in out


def test_missing_inputs_are_not_silently_green(tmp_path):
    rc, out = _run_guard(tmp_path / 'nope.txt', tmp_path / 'nope2.txt',
                         tmp_path / 'nope3.txt', count='0')
    assert rc == 1, out
    assert '缺基线清单' in out


def test_parents_before_different_sizes_block(tmp_path):
    """两份清单不同源（上一版的真实翻车形状）：必须报"父清单 != 基线"，不许逐条糊弄。"""
    b = tmp_path / '.commits_before.txt'
    p = tmp_path / '.commits_parents.txt'
    cm = tmp_path / 'commit-map'
    b.write_text(_before(('f' * 40, '2026-09-30T13:00:00+08:00', 'keep', ['x.py'])), encoding='utf-8')
    p.write_text('', encoding='utf-8')          # 父清单空 = 上一版 fixture 写错格式的效果
    cm.write_text(_map(('f' * 40, 'a' * 40)), encoding='utf-8')
    rc, out = _run_guard(b, p, cm, count='1')
    assert rc == 1, out
    assert '父清单' in out and '判据失明' in out


def test_missing_commit_map_blocks(tmp_path):
    """map 不在场不许被读成"一条没删"：那是判据瞎了，不是重写干净。"""
    b = tmp_path / '.commits_before.txt'
    p = tmp_path / '.commits_parents.txt'
    b.write_text(_before(('g' * 40, '2026-09-30T13:00:00+08:00', 'keep', ['x.py'])), encoding='utf-8')
    p.write_text(_parents(('g' * 40, ['s1'])), encoding='utf-8')
    rc, out = _run_guard(b, p, tmp_path / 'no-such-commit-map', count='1')
    assert rc == 1, out
    assert 'commit-map' in out


def test_map_not_covering_baseline_blocks(tmp_path):
    """基线里有 2 条、map 只认 1 条 => 有一条提交没人说它去了哪里，必须挡下来并点名。"""
    b = tmp_path / '.commits_before.txt'
    p = tmp_path / '.commits_parents.txt'
    cm = tmp_path / 'commit-map'
    h, i = 'h' * 40, 'i' * 40
    b.write_text(_before(
        (h, '2026-09-30T13:00:00+08:00', 'keep', ['x.py']),
        (i, '2026-09-30T13:01:00+08:00', 'also-keep', ['y.py']),
    ), encoding='utf-8')
    p.write_text(_parents((h, ['s1']), (i, ['s1'])), encoding='utf-8')
    cm.write_text(_map((h, 'a' * 40)), encoding='utf-8')
    rc, out = _run_guard(b, p, cm, count='2')
    assert rc == 1, out
    assert '没覆盖' in out and i[:10] in out


def test_map_and_revlist_mismatch_blocks(tmp_path):
    """map 判删 1 条，但 HEAD 只该少 1 条却少了 2 条 => 两份事实不闭合，绝不推送。"""
    b = tmp_path / '.commits_before.txt'
    p = tmp_path / '.commits_parents.txt'
    cm = tmp_path / 'commit-map'
    j, k, l = 'j' * 40, 'k' * 40, 'l' * 40
    b.write_text(_before(
        (j, '2026-09-30T13:00:00+08:00', 'keep', ['x.py']),
        (k, '2026-09-30T13:01:00+08:00', 'gone-data', ['rss_history.json']),
        (l, '2026-09-30T13:02:00+08:00', 'keep2', ['z.py']),
    ), encoding='utf-8')
    p.write_text(_parents((j, ['s1']), (k, ['s1']), (l, ['s1'])), encoding='utf-8')
    cm.write_text(_map((j, 'a' * 40), (k, ZERO), (l, 'b' * 40)), encoding='utf-8')
    rc, out = _run_guard(b, p, cm, count='1')          # 基线 3 - 删 1 = 应剩 2，实报 1
    assert rc == 1, out
    assert '不闭合' in out


def test_git_subprocess_failure_is_not_silent(tmp_path):
    """git 半路失败必须报"读不到"，不能被读成"提交数变了"。老 git() 只取 stdout、
    rc 与 stderr 全丢 —— 那是能把一次干净重写判成假红的另一条路。
    """
    b = tmp_path / '.commits_before.txt'
    p = tmp_path / '.commits_parents.txt'
    cm = tmp_path / 'commit-map'
    b.write_text(_before(('m' * 40, '2026-09-30T13:00:00+08:00', 'keep', ['x.py'])), encoding='utf-8')
    p.write_text(_parents(('m' * 40, ['s1'])), encoding='utf-8')
    cm.write_text(_map(('m' * 40, 'a' * 40)), encoding='utf-8')
    rc, out = _run_guard(b, p, cm, count='1', git_fails=True)
    assert rc == 1, out
    assert 'git 调用失败' in out and 'fatal: bad object HEAD' in out


def test_git_helper_surfaces_failure(tmp_path):
    """git() 本身必须把非零退出和 stderr 带出来。老实现只取 stdout —— 在那种写法下，
    "读到一半失败" 与 "提交真的没了" 在代码里长得一模一样，这正是能把干净重写判成假红的路。
    这条打在抛出点上，所以把 raise 拆掉它就红（只测 main 的 except 分支是抓不到的）。
    """
    subprocess.run(['git', '-c', 'init.defaultBranch=main', 'init', '-q', str(tmp_path)],
                   capture_output=True, text=True)
    script = '\n'.join([
        'import os, sys',
        'sys.path.insert(0, %r)' % os.path.join(ROOT, 'tools'),
        'import trim_commit_guard as G',
        'os.chdir(%r)' % str(tmp_path),
        'try:',
        '    out = G.git(["rev-list", "--count", "HEAD"])',
        'except G.GitError as e:',
        '    print("RAISED", e)',
        'else:',
        '    print("SILENT", repr(out))',
    ])
    r = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True,
                       encoding='utf-8', errors='replace')
    body = r.stdout + r.stderr
    assert 'RAISED' in body, 'git() 把失败咽掉了：%s' % body
    assert 'fatal' in body, '抛出来却没带 stderr，读日志的人仍不知道为什么：%s' % body


def _has_filter_repo():
    try:
        return subprocess.run(['git', 'filter-repo', '--version'],
                              capture_output=True, text=True).returncode == 0
    except Exception:
        return False


@pytest.mark.skipif(not _has_filter_repo(), reason='本机没有 git-filter-repo，格式假设那一段测不了')
def test_real_git_and_filter_repo_end_to_end(tmp_path):
    """真仓库走一遍：抓基线 -> filter-repo 剔族 -> 闸必须放行，且源码提交还在。

    这条钉的是格式假设（%p / --name-only / commit-map 的输出形状）。单测全绿也照样可能是瞎闸，
    因为 fixture 是我照着想象造的；所以必须拿真 git 对一次。
    """
    env = dict(os.environ)
    env.update({'GIT_AUTHOR_NAME': 'T', 'GIT_AUTHOR_EMAIL': 't@e', 'GIT_COMMITTER_NAME': 'T',
                'GIT_COMMITTER_EMAIL': 't@e', 'PYTHONIOENCODING': 'utf-8'})

    def run(*a):
        return subprocess.run(list(a), cwd=str(tmp_path), env=env,
                              capture_output=True, text=True, encoding='utf-8', errors='replace')

    run('git', 'init', '-q', str(tmp_path))
    for i, (name, body) in enumerate([
        ('keep.py', 'print(1)'),
        ('rss-data-1.js', 'x' * 100),
        ('rss_history_0.json', '{}'),
        ('rss_cache.json', '{}'),
        ('final.py', 'print(2)'),
    ]):
        (tmp_path / name).write_text(body, encoding='utf-8')
        run('git', 'add', name)
        run('git', 'commit', '-q', '-m', 'c%d %s' % (i, name))
    assert int(run('git', 'rev-list', '--count', 'HEAD').stdout.strip()) == 5
    (tmp_path / '.commits_before.txt').write_text(
        run('git', 'log', '--format=C|%H|%cI|%s', '--no-renames', '--name-only').stdout,
        encoding='utf-8')
    (tmp_path / '.commits_parents.txt').write_text(
        run('git', 'log', '--format=P|%H|%p').stdout, encoding='utf-8')
    r = run('git', 'filter-repo', '--force', '--invert-paths',
            '--path-glob', 'rss-data-[1-9]*.js', '--path-glob', 'rss_history*.json',
            '--path-glob', 'rss_cache.json')
    assert r.returncode == 0, r.stdout + r.stderr
    assert int(run('git', 'rev-list', '--count', 'HEAD').stdout.strip()) == 2, \
        '退化提交应被裁掉，只剩两条源码提交'
    out = subprocess.run([sys.executable, GUARD], cwd=str(tmp_path), env=env,
                         capture_output=True, text=True, encoding='utf-8', errors='replace')
    body = out.stdout + out.stderr
    assert out.returncode == 0, body
    assert body.count('OK-纯数据') == 3, body
    assert '消失 3' in body, body
    assert '::error::' not in body, body

    # 反向断言用真实事故形状，不伪造字节：把剔除名单再放宽一个 --path-glob 'final.py'，
    # 于是"改了源码的那条"也退化成空提交被删掉。map 会说它没了，HEAD 会少一条，
    # 两份事实依旧闭合 —— 唯一能抓住它的就是"它当时改过 keep 之外的哪个文件"。
    r2 = run('git', 'filter-repo', '--force', '--invert-paths', '--path-glob', 'final.py')
    assert r2.returncode == 0, r2.stdout + r2.stderr
    assert int(run('git', 'rev-list', '--count', 'HEAD').stdout.strip()) == 1, \
        '改源码那条应已退化成空提交被删掉'
    out2 = subprocess.run([sys.executable, GUARD], cwd=str(tmp_path), env=env,
                          capture_output=True, text=True, encoding='utf-8', errors='replace')
    assert out2.returncode == 1, out2.stdout + out2.stderr
    assert 'BAD-含非数据文件' in out2.stdout, out2.stdout
    assert 'final.py' in out2.stdout, out2.stdout


def test_workflow_baseline_command_disables_rename_detection():
    """repo-trim.yml 抓基线必须带 --no-renames：开了重命名检测，一次 delete+add 会被并成
    一条改名、只报目标名，"带走了源码"的提交就能被读成"只改了数据文件" —— 那是假绿放行。
    """
    wf = os.path.join(ROOT, '.github', 'workflows', 'repo-trim.yml')
    text = open(wf, encoding='utf-8').read()
    lines = [l.strip() for l in text.splitlines()
             if '.commits_before.txt' in l and 'git log' in l]
    assert lines, '找不到基线抓取命令，闸零无从判定'
    assert all('--no-renames' in l for l in lines), lines
