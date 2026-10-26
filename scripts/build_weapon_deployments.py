# -*- coding: utf-8 -*-
"""从英文 wiki 武器数据模块生成 Archgun 的「双部署」面板 → core/data/weapon_deployments.json。

数据源（2026-10-26 wfsim 迁移 P2 起；之前取自 wfsim，已弃用）：wiki ``Module:Weapons/data/archwing``
（CC BY-NC-SA 3.0）里同一把 archgun 有两条：``<名> (Atmosphere)`` = 地面 / 大气部署（装 Gravimag
在普通任务里用，= 我方 weapons_stats 主面板，计算器默认）；``<名>`` = 空战（Archwing）部署。
两条按**名字**配对（wiki「Corvas Prime」空战条目的 InternalName 抄成了 Corvas 的，不能按键配）。

默认主段 = 大气条目里与 weapons_stats 主段一致的攻击（build_weapon_attacks.pick_main），空战取同名攻击；
``archwing`` 只写与大气不同的项：damage / radial_damage（主段范围子段）/ criticalChance /
criticalMultiplier / reload_seconds / magazine。

key = 小写 uniqueName（取大气条目的）。用法：python scripts/build_weapon_deployments.py --wiki <目录> [--out 路径]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _wiki_weapons import damage, load_weapons  # noqa: E402
from build_weapon_attacks import _close, base_name, entry_key, is_cloud, pick_main, split_attacks  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "core" / "data" / "weapon_deployments.json"
WS = ROOT / "core" / "data" / "weapons_stats.json"


def _radial(root: dict) -> dict:
    aoe = [a for a in root["_aoe"] if not is_cloud(a)]
    aoe.sort(key=lambda a: -(damage(a).get("total") or 0.0))
    return damage(aoe[0]) if aoe else {}


def _same_dmg(a: dict, b: dict) -> bool:
    a = {k: v for k, v in a.items() if k != "total"}
    b = {k: v for k, v in b.items() if k != "total"}
    return set(a) == set(b) and all(_close(a[k], b[k], 0.001) for k in a)


def build(wiki_dir: Path, ws_all: dict) -> tuple[dict, list[str]]:
    W = load_weapons(wiki_dir)
    out: dict = {}
    report: list[str] = []
    for aname, atm in sorted(W.items()):
        if not aname.endswith(" (Atmosphere)"):
            continue
        name = base_name(aname)
        space = W.get(name)
        if space is None:
            report.append(f"{aname}：没有对应的空战条目")
            continue
        key = entry_key(aname, atm)
        ws = ws_all.get(key) or {}
        if not ws:
            report.append(f"{aname}：计算器（weapons_stats）无此武器，跳过")
            continue
        ar, sr = split_attacks(atm), split_attacks(space)
        main, ok = pick_main(ar, ws, atm.get("_TooltipAttackDisplay"))
        if not ok:
            report.append(
                f"{aname}：大气条目没有与 weapons_stats 主段一致的攻击，取第一个「{main.get('AttackName')}」"
            )
        sm = next((r for r in sr if r.get("AttackName") == main.get("AttackName")), None)
        if sm is None:
            report.append(f"{name}：空战条目没有「{main.get('AttackName')}」，跳过")
            continue
        aw: dict = {}
        if not _same_dmg(damage(sm), damage(main)):
            aw["damage"] = damage(sm)
        if _radial(sm) and not _same_dmg(_radial(sm), _radial(main)):
            aw["radial_damage"] = _radial(sm)
        for f, dst in (("CritChance", "criticalChance"), ("CritMultiplier", "criticalMultiplier")):
            if sm.get(f) is not None and not _close(sm.get(f), main.get(f), 0.001):
                aw[dst] = float(sm[f])
        if space.get("Reload") is not None and not _close(
            space.get("Reload"), atm.get("Reload"), 0.001
        ):
            aw["reload_seconds"] = float(space["Reload"])
        if space.get("Magazine") and not _close(space.get("Magazine"), atm.get("Magazine"), 0.001):
            aw["magazine"] = float(space["Magazine"])
        if not aw:
            continue
        out[key] = {"name": name, "default": "atmosphere", "archwing": aw}
    return out, report


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wiki", required=True, type=Path)
    ap.add_argument("--out", type=Path, default=OUT)
    ap.add_argument("--report", type=Path)
    args = ap.parse_args(argv[1:])
    recs, report = build(args.wiki, json.loads(WS.read_text(encoding="utf-8")))
    out = {
        "_meta": {
            "source": "英文 wiki Module:Weapons/data/archwing（<名> (Atmosphere) = 大气部署 / <名> = 空战部署），CC BY-NC-SA 3.0",
            "wiki_snapshot": args.wiki.name,
            "generator": "scripts/build_weapon_deployments.py",
            "note": "顶层面板 = 大气（Gravimag）默认部署；archwing = 空战覆盖值，只写与大气不同的项",
        }
    }
    out.update(dict(sorted(recs.items())))
    args.out.write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    if args.report:
        args.report.write_text("\n".join(report) + "\n", encoding="utf-8")
    print(f"✓ {args.out.name}: {len(recs)} 把 archgun；提示 {len(report)} 条")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
