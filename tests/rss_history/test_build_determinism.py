# -*- coding: utf-8 -*-
"""出厂路径非确定性判据（产物稳定性计划 Task 0）。

为什么值得一条常驻测试：后续所有推论都建立在"内容不变 ⇒ git 不新增对象"上，
而这句话只有在构建可重复时才成立。若存在与时间无关的随机源（set/dict 迭代序、
并发完成顺序、未补 tie-breaker 的排序），Task 1-3 的收益上限就被它锁死——
再怎么改字段也是每场全量重写。

判据分两半，缺一不可：
  A 真实出厂路径在"同输入 + 钉住时钟 + 跨进程 + 换 PYTHONHASHSEED"下必须逐字节相同
  B 注入 set 迭代序的变异体必须让 A 判红 —— 证明这条判据不是恒真的
只有 A 的测试等于没测。

本目录被 CI 以 `python -m pytest tests/rss_history/ -q -s` 点名，因此必须是函数式断言：
模块级 sys.exit 会让整个收集阶段崩（历史上炸过一次，红的是别人家的门禁）。
"""
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PROBE = os.path.join(ROOT, "tools", "build_determinism.py")


def _run_probe(*extra):
    cmd = [sys.executable, PROBE] + list(extra)
    p = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", cwd=ROOT)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def test_chunk_path_is_reproducible_across_hash_seeds():
    rc, out = _run_probe()
    assert rc == 0, "两趟同输入构建产物不一致，exit=%d\n%s" % (rc, out[-1500:])


def test_probe_actually_ran_and_returned_a_verdict():
    """钉住 exit=0 还不够：worker 悄悄失败也会 exit=0，那时上面的断言是假绿。"""
    rc, out = _run_probe()
    assert "VERDICT: DETERMINISTIC" in out, (
        "探针没给出确定性裁决，说明 worker 未跑完成出厂路径\n%s" % out[-1500:])


def test_probe_catches_hash_order_mutation():
    """B：判据的资格证明。抓不住这个变异体，上面的绿就没有意义。"""
    rc, out = _run_probe("--mutate")
    assert rc != 0 and "NON-DETERMINISTIC" in out, (
        "注入 set 迭代序后探针仍判绿 -> 该判据恒真，Task 1-3 的稳定性前提未经检验\n%s"
        % out[-1500:])


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q", "-s"]))
