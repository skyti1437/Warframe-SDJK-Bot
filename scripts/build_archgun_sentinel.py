# -*- coding: utf-8 -*-
"""补全武器库：从 wfsim 抽取 Archgun（空战枪）与 Sentinel（守护武器）。

我们 weapons_stats.json 只收了 Primary/Secondary/Melee（540 把），
空战枪与守护武器整类缺失 —— 「伤害 赤毒阿扬加」这类查询会直接查不到。

数据源：wfsim data/weapons/{archgun,sentinel}/*.yaml（attack 块 = 默认
（地面/大气）面板；deployments.archwing 由 weapon_deployments.json 管）。
zh 名取自 wfsim i18n/zh/names.yaml（DE 官方客户端串）。

只新增不覆盖：同名 key 已存在时跳过。
"""
import glob
import json
import sys
from pathlib import Path

import yaml

WFSIM = Path(r"REDACTED_TMP_DIR/wfsim/data")
OUT = Path(__file__).resolve().parent.parent / "core" / "data" / "weapons_stats.json"
DAMAGE_KEYS = ("impact", "puncture", "slash", "heat", "cold", "electricity",
               "toxin", "blast", "radiation", "gas", "magnetic", "viral",
               "corrosive", "void", "true")


def cd(d) -> dict:
    out = {k: float(d.get(k) or 0.0) for k in DAMAGE_KEYS if d.get(k)}
    if out:
        out["total"] = round(sum(out.values()), 4)
    return out


def main() -> None:
    ours = json.loads(OUT.read_text(encoding="utf-8"))
    names = (yaml.safe_load((WFSIM / "i18n" / "zh" / "names.yaml")
                            .read_text(encoding="utf-8")).get("weapons") or {})
    added = []
    for cat, label in (("archgun", "Archgun"), ("sentinel", "Sentinel")):
        for f in sorted((WFSIM / "weapons" / cat).glob("*.yaml")):
            y = yaml.safe_load(open(f, encoding="utf-8"))
            if not isinstance(y, dict):
                continue
            a = y.get("attack") or {}
            dmg = cd(a.get("damage") or {})
            if not dmg:
                continue
            key = str(y.get("internal_name") or "").lower()
            if not key or key in ours:
                continue
            attacks = [{"name": "Normal Attack", "damage": dmg,
                        "total": dmg["total"],
                        "crit_chance": float(a.get("crit_chance") or 0) * 100,
                        "crit_mult": float(a.get("crit_multiplier") or 1),
                        "status_chance": float(a.get("status_chance") or 0) * 100,
                        "speed": a.get("fire_rate"),
                        "shot_type": a.get("shot_type")}]
            rad = a.get("radial")
            if isinstance(rad, dict) and rad.get("damage"):
                rd = cd(rad["damage"])
                if rd:
                    attacks.append({
                        "name": "Area Attack", "damage": rd, "total": rd["total"],
                        "crit_chance": float(rad.get("crit_chance") or 0) * 100,
                        "crit_mult": float(rad.get("crit_multiplier") or 1),
                        "status_chance":
                            float(rad.get("status_chance") or 0) * 100,
                        "shot_type": "AoE"})
            ours[key] = {
                "name": y.get("name"),
                "zh": names.get(y.get("id")) or None,
                "uniqueName": y.get("internal_name"),
                "slot": y.get("slot"),
                "category": label,
                "masteryReq": y.get("mastery_rank"),
                "criticalChance": float(a.get("crit_chance") or 0),
                "criticalMultiplier": float(a.get("crit_multiplier") or 1),
                "procChance": float(a.get("status_chance") or 0),
                "fireRate": float(a.get("fire_rate") or 0),
                "magazineSize": float(y.get("magazine") or a.get("magazine") or 0),
                "reloadTime": float(y.get("reload_seconds")
                                    or a.get("reload_seconds") or 0),
                "multishot": float(a.get("multishot") or 1),
                "damage": dmg,
                "attacks": attacks,
                "source": "wfsim data/weapons（wiki/实测整理）",
            }
            added.append(f"{y.get('name')}({names.get(y.get('id')) or '—'})")
    OUT.write_text(json.dumps(ours, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    print(f"✓ 新增 {len(added)} 把武器")
    for x in added:
        print("   ", x)


if __name__ == "__main__":
    sys.exit(main())
