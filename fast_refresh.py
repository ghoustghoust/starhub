# -*- coding: utf-8 -*-
"""15 分钟新星快车道（SPEC 2026-10-02 §4-T3；2026-10-07 P0 收口执行面）。

只做三件事：拉 star（带 token）→ 与已知集合 diff → 新星分类/点评/翻译，然后把状态写回、
交给 CI 提交。**不产出任何 HTML**：站点内容的唯一通路是 update.yml（理由见 star-fast.yml 头部）。
RSS 聚合、每日洞察、Vercel 部署一概不碰；也不触发 fetch_and_build.main。

设计约束：
- 每场都用全量 repos 走 assemble_entries：分类与点评是在它内部对全表算出来的，
  「新星」只决定要不要写回、写回哪几条，不决定要不要跑这一步。
- 有新 star 才写回并让 CI 提交（避免 known_categories 的 git 增量被 15 分钟节奏放大，
  手册 §8.16 正为它的每场重写挂账）；无新星零文件写、零提交。
- 拉取失败：打 ::warning:: 后正常退出，15 分钟后下一场重新 diff 补判，数据不丢。
  这里不做重试风暴，但**也别指望 /api/health 会报**：它的 star_fast_age 数的是 run 成功年龄，
  而拉取失败同样 return 0 ⇒ run 照样绿（2026-10-07 对抗审查命中：这是一条不会响的铃）。
  断链检测统一记在 HANDOFF §8.34 的 P1.5 账上。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fetch_and_build as fab  # noqa: E402

ROOT = os.path.dirname(os.path.abspath(__file__))
KC_PATH = os.path.join(ROOT, "known_categories.json")
NOTES_PATH = os.path.join(ROOT, "known_notes.json")
DESC_PATH = os.path.join(ROOT, "descriptions_zh.json")
# 本场"当前收藏有哪些"的名单，写给 `tools/restore_star_state.py --merge-onto` 读。
# 放 .deploy-tmp/（已 gitignore）是刻意的：它是**场内传递**而不是跨场状态，每场重算，
# 存进 git 反而会多出一个需要人维护的文件。两侧路径字面量各写一份，由
# tests/tools/test_fast_refresh_prune.py::test_live_list_path_is_the_same_on_both_sides 钉住。
LIVE_LIST_PATH = os.path.join(ROOT, ".deploy-tmp", "star_live_names.json")


def _load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return default


def _write_live_list(names):
    """写 live 名单。失败只降级、绝不挡路：读不到名单时写端一律不剪，
    最坏是"这一场没清死键"，而反过来（写了错的名单）会把还在收藏的键剪没。"""
    try:
        os.makedirs(os.path.dirname(LIVE_LIST_PATH), exist_ok=True)
        tmp = LIVE_LIST_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(sorted(names), f)
        os.replace(tmp, LIVE_LIST_PATH)
    except Exception as e:  # noqa: BLE001
        print("::warning::[fast] live 名单写失败，本场 star-state 不剪枝：%s" % e)


def _atomic_write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def main():
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    repos = fab.fetch_stars(token)
    if repos is None:
        print("::warning::[fast] 拉取 star 失败（限流/网络），本场跳过——状态文件不写回，下一场重新 diff 补判")
        return 0

    live = {r.get("full_name") for r in repos if r.get("full_name")}
    _write_live_list(live)

    known = _load_json(KC_PATH, {})
    notes = _load_json(NOTES_PATH, {})
    desc_zh = _load_json(DESC_PATH, {})
    # 取消关注即删除（用户 2026-10-12 裁定）：这张表原本只增不减，仓库改名后旧名永久留在
    # star-state 上，而写端并集一定把它捞回工作树 ⇒ main 每场重剪一遍。门槛在
    # prune_star_state 内部（空集与拉取缩水一律不剪），这里不另设第二套判据，避免分叉。
    #
    # ⚠ 只剪 categories，notes 交给写端 `_prune_to_live`（它两表都剪）。这不是少做一半：
    # `main/known_notes.json` 是一份**没有写者、却参与读端并集**的冻结副本（update.yml 的
    # add 清单里有 categories 却没有 notes），star-fast 每场检出 main 就带着它那批早已不在
    # 收藏的键 ⇒ 若在这里也剪 notes，"dropped 非空"就永远成立 ⇒ 每 15 分钟往分支推一枚与
    # 上一场逐字节相同的空提交。05:30 与 05:45 两场实测各剪了同样 4 把、各推一次，就是这么来的。
    # 判据：tests/tools/test_fast_refresh_prune.py::test_notes_dead_keys_alone_never_trigger_a_write
    dropped = fab.prune_star_state(known, live)

    new_repos = [r for r in repos if r.get("full_name") and r["full_name"] not in known]
    if not new_repos and not dropped:
        print("[fast] no change, skip write（%d 条，无新星）" % len(repos))
        return 0
    if dropped:
        print("[fast] 剪枝：移除 %d 个已不在收藏的键：%s"
              % (len(dropped), ", ".join(dropped[:8])))
    if not new_repos:
        # 纯剪枝的场次（用户只取关、没关注新的）：**绝不**走 assemble_entries。它对全表做
        # 翻译与 README 摘要，原本靠上面那句 `return 0` 挡掉，现在多了这条出口就得自己挡；
        # 为剪几个键烧一轮 LLM 是本车道最不该犯的错。desc_zh 没变，所以也不写它。
        _atomic_write_json(KC_PATH, known)
        _atomic_write_json(NOTES_PATH, notes)
        print("[fast] wrote（纯剪枝 %d 键，无新星，状态已写回待 CI 提交）" % len(dropped))
        return 0

    print("[fast] 新星 %d 条：%s" % (len(new_repos), ", ".join(r["full_name"] for r in new_repos[:8])))

    cat_label = {c["key"]: c["label"] for c in fab.CATS}
    out = fab.assemble_entries(repos, known, desc_zh, cat_label, token, notes=notes)
    # 2026-10-07 P0：这里原有 render_star_attention + build_index_html + 写 index.html 三段
    # （连同它们上方"提醒条与小时场同版""排行榜回填取同一出口件"两条 rationale 注释）已整体
    # 删除——快车道不再产出 HTML，站点唯一通路是 update.yml。out 的唯一下游是下面的点评沉淀。
    # 反向判据：tests/site_nav/test_star_attention_bar.py::test_index_html_has_exactly_one_producer
    # 与 tools/mut_attention.py 的 M6 一起钉住"别再把它加回来"。

    # 新星点评沉淀（classify_repo 对老星查表命中 note=""，只有新星带点评）
    for e in out:
        if e.get("note") and not notes.get(e["full_name"]):
            notes[e["full_name"]] = e["note"]

    _atomic_write_json(KC_PATH, known)
    _atomic_write_json(NOTES_PATH, notes)
    _atomic_write_json(DESC_PATH, desc_zh)
    print("[fast] wrote %d（状态已写回，待 CI 提交）" % len(new_repos))
    return 0


if __name__ == "__main__":
    sys.exit(main())
