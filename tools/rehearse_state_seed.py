"""回灌步的本地彩排：把 update.yml 里那一步的 run 正文原样抽出来，在临时空目录真跑两遍。

第一遍：7 个文件都不存在 ⇒ 必须走"下载"分支，尺寸应等于历史副本；
第二遍：marker 已写 ⇒ 必须走早退分支（"跳过" + exit 0），且不再发第二次 HEAD 之外的请求。
这验的是 shell 本身，不是 YAML 结构 —— 判据能钉形状，钉不出 `[ -lt ]` 写错这种事。
"""
import os
import shutil
import subprocess
import sys
import tempfile

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STEP = "Seed cross-build state from history (cold-start repair)"
FILES = ["hot_history.json", "analysis_snapshot.json", "translations.json", "rss_trend_history.json",
         "insight_tracking_history.jsonl", "daily_insight_tracking_history.jsonl",
         "daily_insight_history.json"]
EXPECT = {"hot_history.json": 8280186, "analysis_snapshot.json": 3650232,
          "translations.json": 3315740, "rss_trend_history.json": 1156862,
          "insight_tracking_history.jsonl": 781303,
          "daily_insight_tracking_history.jsonl": 408781, "daily_insight_history.json": 263475}

doc = yaml.safe_load(open(os.path.join(ROOT, ".github", "workflows", "update.yml"), encoding="utf-8"))
body = [s.get("run") or "" for s in doc["jobs"]["update"]["steps"] if (s.get("name") or "") == STEP][0]
assert body.strip(), "抽不到 run 正文"
script = os.path.join(tempfile.mkdtemp(prefix="seed_rehearse_"), "seed.sh")
open(script, "w", encoding="utf-8", newline="\n").write(body)
work = os.path.dirname(script)
print("彩排目录:", work)
print("正文行数:", len(body.splitlines()))

r1 = subprocess.run(["bash", script], cwd=work, capture_output=True, text=True,
                    encoding="utf-8", errors="replace", timeout=900)
print("--- 第一遍 rc=%d ---" % r1.returncode)
for l in (r1.stdout or "").splitlines():
    print("   ", l[:120])
if r1.stderr.strip():
    print("   stderr:", r1.stderr.strip()[:300])

bad = []
for f in FILES:
    p = os.path.join(work, f)
    if not os.path.exists(p):
        bad.append((f, "没落盘"))
        continue
    n = os.path.getsize(p)
    if n != EXPECT[f]:
        bad.append((f, "%d != 历史 %d" % (n, EXPECT[f])))
print("下载核对:", "全部 7 个文件字节数与历史副本一致" if not bad else bad)

r2 = subprocess.run(["bash", script], cwd=work, capture_output=True, text=True,
                    encoding="utf-8", errors="replace", timeout=300)
out2 = (r2.stdout or "").splitlines()
print("--- 第二遍 rc=%d ---" % r2.returncode)
for l in out2[:4]:
    print("   ", l[:120])
skipped = any("跳过" in l for l in out2)
redownloaded = any("SEED " in l for l in out2)
print("早退分支生效:", skipped, "| 没有重复下载:", not redownloaded, "| rc=0:", r2.returncode == 0)
marker = os.path.join(work, "build_logs", ".state_seed.done")
print("marker 内容:", open(marker, encoding="utf-8").read().strip()[:50] if os.path.exists(marker) else "缺")

# 第三遍 = CI 现场的真实形状：文件都在盘上（缓存还原成功）但内容塌了。
# 前两遍只验过 L=0（缺失）与早退；如果 -lt 分支在这种形状下不动手，v1 的错误就重演一次。
os.remove(marker)
塌 = {"hot_history.json": 47073, "translations.json": 198958, "analysis_snapshot.json": 122213}
for f, n in 塌.items():
    open(os.path.join(work, f), "wb").write(b"x" * n)
r3 = subprocess.run(["bash", script], cwd=work, capture_output=True, text=True,
                    encoding="utf-8", errors="replace", timeout=900)
out3 = (r3.stdout or "").splitlines()
print("--- 第三遍（塌了但不缺失）rc=%d ---" % r3.returncode)
for l in out3:
    print("   ", l[:120])
got_seed = {f for f in 塌 if any(l.strip().startswith("SEED " + f) for l in out3)}
kept = {f for l in out3 if l.strip().startswith("keep ") for f in [l.split()[1]] if f in FILES}
sizes_ok = all(os.path.getsize(os.path.join(work, f)) == EXPECT[f] for f in 塌)
print("该回的回了:", sorted(got_seed) == sorted(塌), "| 没该回的没动(keep):", len(kept) == len(FILES) - len(塌),
      "| 回灌后尺寸对:", sizes_ok)
shutil.rmtree(os.path.dirname(work), ignore_errors=True)
ok = (not bad and skipped and not redownloaded and r1.returncode == 0 and r2.returncode == 0
      and r3.returncode == 0 and got_seed == set(塌) and sizes_ok
      and len(kept) == len(FILES) - len(塌))
print("彩排结论:", "三个分支都按预期工作" if ok else "有分支不符合预期")
sys.exit(0 if ok else 1)
