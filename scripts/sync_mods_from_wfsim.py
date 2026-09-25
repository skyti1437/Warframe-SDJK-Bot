# -*- coding: utf-8 -*-
"""用 wfsim 校准我们的 MOD 等级元数据（base_drain / max_rank / 每级数值）。

背景（2026-09-16 用户实测）：压迫点在我们库里 max_rank=10、满级 +200%，
实际游戏是 max_rank=5、满级 +120% —— 容量数字反推等级因此整体错位
（满级卡被标成「5/10 非满级」）。wfsim 的 MOD 数据逐卡核对过 wiki，
base_drain/max_rank/rank0/rankMax 可信，用它批量校准。

只同步：
- base_drain / max_rank
- levels[]：按 rank0→rankMax 线性插值重建「可映射的伤害数值字段」，
  不可映射的字段（条件文本等）按比例就近保留
- effects（满级视图）的同名字段同步为 rankMax 值

不动：zh/别名/条件文本/note/polarity，以及 wfsim 没有的卡。
带 --dry-run 只报告不写。
"""
import json
import sys

try:
    import yaml
except ImportError:  # noqa: BLE001 —— 插件运行本身不需要 yaml，只有这几个
    raise SystemExit(   # 数据构建脚本要；给可操作的提示而不是裸 ImportError
        "本脚本需要 PyYAML：pip install pyyaml"
        "（插件运行时并不依赖它，仅构建数据用）")
import glob
import os
from pathlib import Path      # ★ 2026-09-20 补：原来漏了这个导入，脚本一跑就
                              #   NameError（这也是 names 表长期没被校准的原因之一）

WFSIM_DIR = str(Path.home() / "tmp" / "wfsim" / "data" / "mods")
MODS = os.path.join(os.path.dirname(__file__), os.pardir, "core", "data",
                    "mods_stats.json")

# wfsim kind → (我们的字段, 是否百分比)
KIND_MAP = {
    "base_damage_bonus": ("base_dmg", True),
    "crit_chance_bonus": ("crit_chance", True),
    "crit_chance_bonus_heavy_doubled": ("crit_chance", True),
    "crit_damage_bonus": ("crit_dmg", True),
    "status_chance_bonus": ("status_chance", True),
    "status_damage_bonus": ("status_dmg", True),
    "fire_rate_bonus": ("fire_rate", True),
    "multishot_bonus": ("multishot", True),
    "faction_damage_bonus": ("faction_dmg", True),
    "heavy_attack_damage_bonus": ("heavy_dmg", True),
    "condition_overload": ("dmg_per_status", True),
    "crit_chance_per_combo": ("crit_per_combo", True),
    "status_chance_per_combo": ("status_per_combo", True),
    "punch_through_bonus": ("punch_through", False),
    "initial_combo": ("initial_combo", False),
}
ELEM_MAP = {
    "elemental_damage_bonus": "elements",
    "physical_damage_bonus": "physical",
}
# 有 condition 的效果是条件触发（如 while_aiming），不折进主数值
SKIP_IF_CONDITIONAL = True


def load_yaml(p):
    with open(p, encoding="utf-8") as f:
        return yaml.safe_load(f)


def rebuild_levels(w_effects, old_levels, max_rank):
    """按 rank0→rankMax 线性插值重建 levels；保留不可映射的旧字段。"""
    n = max_rank + 1
    plan = []           # (our_key, elem_or_None, rank0值, rankMax值, pct?)
    for e in w_effects or []:
        if not isinstance(e, dict):
            continue
        kind = e.get("kind")
        if SKIP_IF_CONDITIONAL and e.get("condition"):
            continue
        if kind in KIND_MAP:
            field, pct = KIND_MAP[kind]
            plan.append((field, None, e.get("rank0") or 0.0,
                         e.get("rankMax") or 0.0, pct))
            if kind == "crit_chance_bonus_heavy_doubled":
                plan.append(("heavy_crit_mult", None, 2.0, 2.0, False))
        elif kind in ELEM_MAP:
            el = e.get("element")
            if el:
                plan.append((ELEM_MAP[kind], el, e.get("rank0") or 0.0,
                             e.get("rankMax") or 0.0, True))
    levels = []
    for rank in range(n):
        t = rank / max_rank if max_rank else 0.0
        new = {}
        old = old_levels[rank] if rank < len(old_levels) else (
            old_levels[-1] if old_levels else {})
        if isinstance(old, dict):
            for k, v in old.items():        # 先保留旧字段（含条件文本等）
                new[k] = v
        for field, el, v0, v1, pct in plan:
            val = v0 + (v1 - v0) * t
            if pct:
                val = round(val * 100.0, 2)
            if el:
                bucket = dict(new.get(field) or {})
                bucket[el] = round(val, 2)
                new[field] = bucket
            else:
                new[field] = val
        levels.append(new)
    return levels


def _wfsim_index():
    """把 wfsim 的 mod yaml 读成 {小写英文名: dict}。"""
    out = {}
    for f in glob.glob(os.path.join(WFSIM_DIR, "**", "*.yaml"), recursive=True):
        y = load_yaml(f)
        if isinstance(y, dict) and y.get("name"):
            out[str(y["name"]).lower()] = y
    return out


def sync_names_table(ours, wf, report):
    """校准**仅识别表**（``names``）：只改 base_drain / max_rank / polarity。

    ★ 为什么必须做（2026-09-20 用户实测报障）：这张表以前**从没被校准过** ——
      旧脚本只管 ``mods``（442 条可算卡），而 ``names``（727 条仅识别卡）
      直接沿用初始抓取值。用户「剑风满级就 3，你这个 5 级哪里来的」就是它：
      库里 ``reach`` 的 max_rank 是 5、实际 3 → 卡面显示「3/5★非满级」。
      全量审计（对拍 387 条）发现 **14 条 max_rank 错、4 条 base_drain 错**，
      其中还包括 `serration`（膛线，5→10）、`hornet strike`（黄蜂螫刺，5→10）。

    ⚠️ 姿态卡在这张表里用 ``base_drain = -2`` 作**哨兵**（``infer_rank`` 靠
      ``base < 0`` 输出「姿态卡（不参与伤害折算）」、``rank_candidates`` 靠它
      返回空表）→ 这类条目**整条跳过**，不要用 wfsim 的 0 覆盖掉哨兵。
    """
    tbl = ours.get("names") or {}
    n = 0
    for key, rec in tbl.items():
        w = wf.get(str(rec.get("name") or "").lower())
        if not w:
            continue
        b_old = rec.get("base_drain")
        if isinstance(b_old, int) and b_old < 0:
            continue                      # 姿态卡哨兵，整条跳过
        changed = []
        for field, wkey in (("base_drain", "base_drain"),
                            ("max_rank", "max_rank"),
                            ("polarity", "polarity")):
            new = w.get(wkey)
            old = rec.get(field)
            if new is None or new == old:
                continue
            if field == "polarity" and isinstance(old, str) \
                    and old.lower() == str(new).lower():
                continue
            changed.append(f"{field} {old}→{new}")
            rec[field] = new
        if changed:
            n += 1
            report.append(f"[仅识别] {rec.get('zh') or key}: " + "、".join(changed))
    return n


def main(dry_run):
    ours = json.load(open(MODS, encoding="utf-8"))
    mods = ours["mods"]
    keys = {k.lower() for k in mods}
    names = {str(v.get("name") or "").lower(): k
             for k, v in mods.items()}
    n_change = 0
    report = []
    for f in glob.glob(os.path.join(WFSIM_DIR, "**", "*.yaml"), recursive=True):
        y = load_yaml(f)
        if not isinstance(y, dict) or not y.get("name"):
            continue
        nm = str(y["name"]).lower()
        key = nm if nm in keys else names.get(nm)
        if not key:
            continue
        rec = mods[key]
        b_new = y.get("base_drain")
        m_new = y.get("max_rank")
        b_old, m_old = rec.get("base_drain"), rec.get("max_rank")
        changed = []
        if b_new is not None and b_new != b_old:
            changed.append(f"base_drain {b_old}→{b_new}")
            rec["base_drain"] = b_new
        if m_new is not None and m_new != m_old:
            changed.append(f"max_rank {m_old}→{m_new}")
            rec["max_rank"] = m_new
        if changed or m_new is not None:
            m_rank = m_new if m_new is not None else (m_old or 0)
            old_levels = rec.get("levels") or []
            new_levels = rebuild_levels(y.get("effects") or [],
                                        old_levels, int(m_rank))
            if new_levels != old_levels:
                changed.append(f"levels 重建（{len(old_levels)}→{len(new_levels)} 级）")
                rec["levels"] = new_levels
                # 满级视图同步
                eff = rec.get("effects")
                if isinstance(eff, dict) and new_levels:
                    for k, v in new_levels[-1].items():
                        eff[k] = v
        if changed:
            n_change += 1
            report.append(f"{rec.get('zh') or key}: " + "、".join(changed))
    # ★ 仅识别表（names）也要校准 —— 见 sync_names_table 的说明
    n_names = sync_names_table(ours, _wfsim_index(), report)
    print("\n".join(report))
    print(f"—— 共 {n_change} 张可算卡 + {n_names} 张仅识别卡有变化"
          + ("（dry-run，未写入）" if dry_run else ""))
    if not dry_run:
        json.dump(ours, open(MODS, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
        print("✓ 已写回", MODS)


if __name__ == "__main__":
    main("--dry-run" in sys.argv)
