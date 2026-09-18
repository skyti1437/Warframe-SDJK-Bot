# -*- coding: utf-8 -*-
"""从 wfsim 抽取 Archgun 的「双部署」面板（空战 / 地面·大气）。

wfsim 的 weapons yaml 顶层 attack 是**默认部署**（deployment: atmosphere，
即装了 Gravimag 在普通任务里用）；deployments.archwing 是空战模式下的
覆盖值（伤害/范围伤害/装填，个别武器还覆盖暴击/弹药）。

输出 core/data/weapon_deployments.json（key = 小写 uniqueName）。
"""
import glob
import json
import sys
from pathlib import Path

import yaml

WFSIM = Path(r"REDACTED_TMP_DIR/wfsim/data")
OUT = Path(__file__).resolve().parent.parent / "core" / "data" / "weapon_deployments.json"
DAMAGE_KEYS = ("impact", "puncture", "slash", "heat", "cold", "electricity",
               "toxin", "blast", "radiation", "gas", "magnetic", "viral",
               "corrosive", "void", "true")


def cd(d) -> dict:
    out = {k: float(d.get(k) or 0.0) for k in DAMAGE_KEYS if d.get(k)}
    if out:
        out["total"] = round(sum(out.values()), 4)
    return out


def main() -> None:
    out: dict = {"_meta": {
        "source": "github.com/magenie33/wfsim data/weapons/*（deployments 字段）",
        "updated": "2026-09-17",
        "note": "Archgun 两种部署：顶层 attack = 地面/大气（Gravimag）默认面板；"
                "archwing = 空战覆盖值。个别武器覆盖暴击/弹药",
    }}
    n = 0
    for f in glob.glob(str(WFSIM / "weapons" / "**" / "*.yaml"), recursive=True):
        y = yaml.safe_load(open(f, encoding="utf-8"))
        if not isinstance(y, dict) or not isinstance(y.get("deployments"), dict):
            continue
        key = str(y.get("internal_name") or "").lower()
        if not key:
            continue
        rec: dict = {"name": y.get("name"),
                     "default": y.get("deployment") or "atmosphere"}
        aw = (y["deployments"] or {}).get("archwing") or {}
        a: dict = {}
        if aw.get("damage"):
            a["damage"] = cd(aw["damage"])
        if aw.get("radial_damage"):
            a["radial_damage"] = cd(aw["radial_damage"])
        if aw.get("reload_seconds"):
            a["reload_seconds"] = float(aw["reload_seconds"])
        for k, dst in (("crit_chance", "criticalChance"),
                       ("crit_multiplier", "criticalMultiplier"),
                       ("ammo_max", "ammo_max")):
            if aw.get(k) is not None:
                a[dst] = float(aw[k])
        if aw.get("no_resupply"):
            a["no_resupply"] = True
        if a:
            rec["archwing"] = a
            out[key] = rec
            n += 1
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    print(f"✓ {OUT.name}: {n} 把双部署武器")
    for k, v in list(out.items())[1:3]:
        print("  ", v["name"], "|", json.dumps(v.get("archwing"), ensure_ascii=False)[:200])


if __name__ == "__main__":
    sys.exit(main())
