# -*- coding: utf-8 -*-
"""从 wfsim 数据抽取 灵化进化选项 与 赤毒/信条/终幕 valence 加成 范围。

只借事实（wfsim 引擎为 AGPL-3.0，数值本身是游戏事实）：
- core/data/evolutions.json : 每把灵化武器的 EVO I-V 层全部选项（id/名称/
  选择方式/效果列表/描述）。效果 kind 分两类：
    * 可建模面板类（flat_base_damage / flat_base_crit_chance / …）
    * 条件/暂不建模类（stacking_* / out_of_scope / unmodelled_* …，原样存档）
- core/data/lich_valence.json : 每把赤毒/信条/终幕武器的可滚 bonus 元素与
  数值范围（如 Kuva Nukor: 25%-60%，7 种元素）。

用法: python scripts/build_evolutions_and_valence.py [wfsim数据目录]
"""
import glob
import json
import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # noqa: BLE001 —— 插件运行本身不需要 yaml，只有这几个
    raise SystemExit(   # 数据构建脚本要；给可操作的提示而不是裸 ImportError
        "本脚本需要 PyYAML：pip install pyyaml"
        "（插件运行时并不依赖它，仅构建数据用）")

WFSIM = Path(sys.argv[1] if len(sys.argv) > 1 else r"REDACTED_TMP_DIR/wfsim/data")
OUT = Path(__file__).resolve().parent.parent / "core" / "data"

# 可直接折算进基础面板的效果 kind → 语义
PANEL_KINDS = {
    "flat_base_damage": "基伤平坦加成",
    "flat_base_crit_chance": "基础暴击率平坦加成",
    "flat_base_crit_multiplier": "基础暴伤倍率平坦加成",
    "flat_base_status_chance": "基础触发率平坦加成",
    "flat_base_multishot": "基础多重平坦加成",
    "flat_base_magazine": "弹匣平坦加成",
    "fire_rate_bonus": "射速加成",
    "reload_speed_bonus": "装填加成",
    "punch_through_bonus": "穿透加成",
    "accuracy_bonus": "精准加成",
    "headshot_damage": "爆头伤害加成",
    "initial_combo": "初始连击",
    "melee_range_bonus_m": "近战范围加成(米)",
    "slam_radius_bonus": "震地范围加成",
    "ammo_reserve_set": "弹药上限设定",
    "recoil_reduction": "后坐力减少",
}
MODELLABLE = set(PANEL_KINDS)


def build_evolutions() -> dict:
    out: dict = {"_meta": {
        "source": "wfsim data/evolutions (magenie33/wfsim)，逐条带 wiki 来源",
        "note": "selection: choose_one=该层多选一 / fixed=固定。effects 原样"
                "存档；modellable 标记是否被 damage_calc 折算",
        "generated": "2026-09-17",
    }}
    for f in sorted((WFSIM / "evolutions").glob("*.yaml")):
        y = yaml.safe_load(open(f, encoding="utf-8"))
        if not isinstance(y, dict) or not y.get("weapon"):
            continue
        w = str(y["weapon"]).lower()
        effects = []
        for e in y.get("effects") or []:
            if not isinstance(e, dict):
                continue
            e = dict(e)
            e["_modellable"] = e.get("kind") in MODELLABLE
            effects.append(e)
        slot = out.setdefault(w, {})
        tiers = slot.setdefault("tiers", {})
        tier = str(y.get("tier"))
        tiers.setdefault(tier, []).append({
            "id": y.get("id"),
            "name": y.get("name"),
            "selection": y.get("selection"),
            "description": y.get("description"),
            "effects": effects,
        })
    return out


def build_valence() -> dict:
    out: dict = {"_meta": {
        "source": "wfsim data/weapons/*.{kuva,tenet,coda}* valence 字段",
        "note": "valence bonus：玩家回响滚出的额外元素加成，数值在 min-max 内；"
                "元素必须取 elements 列表之一。加成实现 = 总基伤×pct 计入该元素",
        "generated": "2026-09-17",
    }}
    for f in glob.glob(str(WFSIM / "weapons" / "**" / "*.yaml"), recursive=True):
        fp = Path(f)
        n = fp.name.lower()
        if not any(n.startswith(k) for k in ("kuva_", "tenet_", "coda_")) \
                and "dual_coda" not in n:
            continue
        y = yaml.safe_load(open(f, encoding="utf-8"))
        if not isinstance(y, dict):
            continue
        v = y.get("valence")
        if not isinstance(v, dict) or not v.get("elements"):
            continue
        out[str(y.get("id") or fp.stem).lower()] = {
            "name": y.get("name"),
            "elements": list(v["elements"]),
            "min": float(v.get("min") or 0.0) * 100,
            "max": float(v.get("max") or 0.0) * 100,
        }
    return out


def main() -> None:
    evo = build_evolutions()
    n_weapons = sum(1 for k, v in evo.items() if k != "_meta")
    n_opts = sum(len(t) for k, v in evo.items() if k != "_meta"
                 for t in v.get("tiers", {}).values())
    (OUT / "evolutions.json").write_text(
        json.dumps(evo, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"evolutions.json: {n_weapons} 个武器家族, {n_opts} 个进化选项")

    val = build_valence()
    n_val = sum(1 for k in val if k != "_meta")
    (OUT / "lich_valence.json").write_text(
        json.dumps(val, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"lich_valence.json: {n_val} 把 lich 武器")

    # 抽查
    lp = evo.get("latron_prime") or {}
    t2 = (lp.get("tiers", {}).get("2") or [{}])[0]
    print("抽查 latron_prime EVO II:", t2.get("name"),
          t2["effects"][0] if t2.get("effects") else None)
    kn = val.get("kuva_nukor") or {}
    print("抽查 kuva_nukor:", kn.get("min"), "-", kn.get("max"),
          "|", len(kn.get("elements") or []), "种元素")


if __name__ == "__main__":
    main()
