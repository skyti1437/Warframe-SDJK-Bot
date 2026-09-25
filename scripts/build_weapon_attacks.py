# -*- coding: utf-8 -*-
"""从 wfsim 抽取武器的「多段攻击」结构（主段 + 范围/集束等额外段）。

数据源：github.com/magenie33/wfsim data/weapons/**/*.yaml 的 attack 块。
每把武器的 attack 可能含：
- damage/crit_*/status_chance/fire_rate   → 主段（直接命中）
- radial                                  → 范围（AoE）段，有自己的
  crit/status、半径、边缘衰减、是否吃多重/爆炸范围 MOD
- cluster                                 → 集束子炸弹（如 Kuva Bramma）
- charge_seconds                          → 蓄力时间
- forced_procs                            → 强制异常

输出 core/data/weapon_attacks.json（key = 小写 uniqueName，与 weapons_stats /
incarnon_forms 对齐）。只收有额外段或蓄力的武器。

许可：只取事实数值（伤害向量/暴击/触发/半径），按我们自己的 schema 重录。
"""
import json
import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # noqa: BLE001 —— 插件运行本身不需要 yaml，只有这几个
    raise SystemExit(   # 数据构建脚本要；给可操作的提示而不是裸 ImportError
        "本脚本需要 PyYAML：pip install pyyaml"
        "（插件运行时并不依赖它，仅构建数据用）")

WFSIM = (Path.home() / "tmp" / "wfsim" / "data")
OUT = Path(__file__).resolve().parent.parent / "core" / "data" / "weapon_attacks.json"
DAMAGE_KEYS = ("impact", "puncture", "slash", "heat", "cold", "electricity",
               "toxin", "blast", "radiation", "gas", "magnetic", "viral",
               "corrosive", "void", "true")


def clean_damage(d) -> dict:
    out = {k: float(d.get(k) or 0.0) for k in DAMAGE_KEYS if d.get(k)}
    if out:
        out["total"] = round(sum(out.values()), 4)
    return out


def seg_of(rad: dict, sid: str, name: str) -> dict:
    seg = {
        "id": sid,
        "name": name,
        "damage": clean_damage(rad.get("damage") or {}),
        "criticalChance": float(rad.get("crit_chance") or 0.0),
        "criticalMultiplier": float(rad.get("crit_multiplier") or 1.0),
        "procChance": float(rad.get("status_chance") or 0.0),
    }
    for k, dst in (("radius_m", "radius_m"),
                   ("falloff_start_m", "falloff_start_m"),
                   ("falloff_reduction", "falloff_reduction")):
        if rad.get(k) is not None:
            seg[dst] = float(rad[k])
    for k in ("takes_multishot", "takes_blast_radius_mods"):
        if rad.get(k) is not None:
            seg[k] = bool(rad[k])
    return seg


def main() -> None:
    out: dict = {"_meta": {
        "source": "github.com/magenie33/wfsim data/weapons/*（attack 块），"
                  "其数值按 wiki/实测整理",
        "updated": "2026-09-17",
        "note": "只收有额外段（范围/集束）或蓄力的武器；主段仍用 weapons_stats/"
                "incarnon_forms 的面板。段是独立命中实例，各吃同样的 MOD 乘区",
    }}
    n = 0
    for cat in ("primary", "secondary", "melee", "sentinel", "archgun",
                "archmelee", "kitgun"):
        base = WFSIM / "weapons" / cat
        if not base.exists():
            continue
        for p in sorted(base.glob("*.yaml")):
            y = yaml.safe_load(open(p, encoding="utf-8"))
            if not isinstance(y, dict) or not isinstance(y.get("attack"), dict):
                continue
            a = y["attack"]
            segs = []
            if a.get("radial"):
                segs.append(seg_of(a["radial"], "radial", "范围伤害"))
            if a.get("cluster"):
                segs.append(seg_of(a["cluster"], "cluster", "集束子炸弹"))
            if not segs and not a.get("charge_seconds"):
                continue
            key = str(y.get("internal_name") or "").lower()
            if not key:
                continue
            rec = {}
            if a.get("charge_seconds"):
                rec["charge_seconds"] = float(a["charge_seconds"])
            if a.get("forced_procs"):
                rec["forced_procs"] = [str(x) for x in a["forced_procs"]]
            if segs:
                rec["segments"] = segs
                rec["main"] = {"damage": clean_damage(a.get("damage") or {})}
            if not rec:
                continue
            rec["name"] = y.get("name")
            out[key] = rec
            n += 1
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    n_seg = sum(len(v.get("segments") or []) for k, v in out.items()
                if k != "_meta")
    print(f"✓ {OUT.name}: {n} 把武器 / {n_seg} 个额外段")
    for k in ("/lotus/weapons/grineer/longguns/kuvagrenadelauncherrocket",
              "/lotus/weapons/grineer/longguns/kuvabow"):
        if k in out:
            e = out[k]
            print("  抽查", e["name"], "→", json.dumps(
                (e.get("segments") or [{}])[0].get("damage"),
                ensure_ascii=False))


if __name__ == "__main__":
    sys.exit(main())
