# -*- coding: utf-8 -*-
"""从英文 wiki 武器数据模块生成枪械灵化形态的基础面板 → core/data/incarnon_forms.json。

数据源（2026-10-26 wfsim 迁移 P2 起；之前取自 wfsim，已弃用）：
- wiki ``Module:Weapons/data/*``（CC BY-NC-SA 3.0）：原型武器条目里名字带「Incarnon」的根攻击
  = 灵化形态（优先名为「Incarnon Form」的那条；Paris 系只有「Incarnon Form Charged Shot」等
  则取第一条）。伤害 / 暴击 / 触发 / 射速 / 多重 / 蓄力 / 点射 / 强制异常取该攻击；范围段与
  weapon_attacks 同一拆法（``build_weapon_attacks.split_attacks`` / ``segments_of``），吃 CO /
  不吃多重按 CO 页 Attack Catalog（``apply_catalog``）。
- 灵化弹池 ``pseudo_reload``：magazine = 该攻击的 ``IncarnonCharges``（wiki；模块缺的取 Incarnon Genesis
  页原文，见 ``CHARGES_FROM_PAGE``）；reload_seconds =
  原型装填时间 + ``SWITCH_SECONDS``。后者是**我方建模约定**（持续 DPS 把「打空弹池 → 切回原型
  装填 → 再切灵化」当一次循环，变形动画按 1 s 估；wiki 无此数据），沿用 2026-10-09 C 批口径。
- ricochet：只有 Dual Toxocyst（wiki「Dual Toxocyst Incarnon Genesis」：「shots now ricochet to
  1 nearby enemy per shot」）。
- 中文名：weapons_stats.json 的 zh（DE 官方简中），形态名 = 「<原型> (灵化形态)」。

属性是灵化形态的基础面板，不含灵化进化 perk（各人选择不同，识卡侧从面板反推吸收）。
key = 原型的小写 uniqueName（= weapons_stats.json 的键）。只收枪械（近战灵化计算器尚未建模）。

用法：python scripts/build_incarnon_forms.py --wiki <wiki 页面目录> [--out 路径]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _wiki_weapons import catalog_by_weapon, catalog_rows, damage, load_weapons  # noqa: E402
from build_weapon_attacks import (  # noqa: E402
    GUN_SLOTS,
    apply_catalog,
    base_name,
    entry_key,
    pick_incarnon,
    procs,
    segments_of,
    split_attacks,
)

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "core" / "data" / "incarnon_forms.json"
WS = ROOT / "core" / "data" / "weapons_stats.json"
SWITCH_SECONDS = 1.0
RICOCHET = {"Dual Toxocyst": {"bounces": 1}}
# 数据模块没写 IncarnonCharges 的：取该武器 Incarnon Genesis 页原文
CHARGES_FROM_PAGE = {
    "Miter": 20,  # 「Miter Incarnon Genesis」：each charge producing 5 rounds to a maximum of 20
    "Stug": 120,  # 「Stug Incarnon Genesis」：each charge producing 4 rounds to a maximum of 120
}


def build(wiki_dir: Path, ws_all: dict) -> tuple[dict, list[str]]:
    W = load_weapons(wiki_dir)
    catalog = catalog_by_weapon(catalog_rows(wiki_dir))
    out: dict = {}
    report: list[str] = []
    for wname, e in sorted(W.items()):
        if e.get("Slot") not in GUN_SLOTS:
            continue
        if e.get("Slot") == "Archgun" and f"{wname} (Atmosphere)" in W:
            continue
        roots = split_attacks(e)
        a = pick_incarnon(roots)
        if a is None:
            continue
        key = entry_key(wname, e)
        ws = ws_all.get(key) or {}
        if not ws:
            report.append(f"{wname}：计算器（weapons_stats）无此武器，跳过")
            continue
        segs_of = {id(r): segments_of(r, wname, report) for r in roots}
        apply_catalog(wname, roots, roots[0], segs_of, catalog.get(base_name(wname), []), report)
        dmg = damage(a)
        if not dmg.get("total"):
            report.append(f"{wname}：灵化攻击「{a.get('AttackName')}」无伤害，跳过")
            continue
        bname = base_name(wname)
        zh = str(ws.get("zh") or "")
        rec: dict = {
            "form_name": f"{bname} (Incarnon Form)",
            "form_zh": f"{zh} (灵化形态)" if zh else "",
            "base_name": bname,
            "base_zh": zh,
            "attack": a.get("AttackName"),
            "damage": dmg,
            "criticalChance": float(a.get("CritChance") or 0.0),
            "criticalMultiplier": float(a.get("CritMultiplier") or 1.0),
            "procChance": float(a.get("StatusChance") or 0.0),
            "fireRate": float(a.get("FireRate") or 0.0),
            "multishot": float(a.get("Multishot") or 1.0),
        }
        rad = next((s for s in segs_of[id(a)] if s["id"] == "radial"), None)
        if rad:
            rec["radial"] = {
                k: v for k, v in rad.items() if k not in ("id", "name", "falloff_start_m")
            }
        if procs(a):
            rec["forced_procs"] = procs(a)
        if int(a.get("BurstCount") or 0) > 1:
            rec["burst"] = {
                "count": int(a["BurstCount"]),
                "delay_seconds": float(a.get("BurstDelay") or 0.0),
            }
        if a.get("ChargeTime"):
            rec["charge_seconds"] = float(a["ChargeTime"])
        charges = a.get("IncarnonCharges") or CHARGES_FROM_PAGE.get(bname)
        reload_s = e.get("Reload")
        if charges and reload_s:
            rec["pseudo_reload"] = {
                "magazine": float(charges),
                "reload_seconds": round(float(reload_s) + SWITCH_SECONDS, 4),
            }
        else:
            report.append(
                f"{wname}：wiki 无 IncarnonCharges / Reload，灵化弹池不写（沿用原型弹匣）"
            )
        if bname in RICOCHET:
            rec["ricochet"] = dict(RICOCHET[bname])
        out[key] = rec
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
            "source": "英文 wiki Module:Weapons/data（原型条目里的 Incarnon 攻击）+ Condition Overload (Mechanic) "
            "Attack Catalog，CC BY-NC-SA 3.0；中文名为 DE 官方简中（weapons_stats.json）",
            "wiki_snapshot": args.wiki.name,
            "generator": "scripts/build_incarnon_forms.py",
            "scope": "枪械灵化（近战灵化计算器尚未建模）；不含灵化进化 perk",
            "pseudo_reload": f"magazine = wiki IncarnonCharges；reload_seconds = 原型装填 + {SWITCH_SECONDS:g} s"
            "（我方建模约定：打空弹池后切回原型装填再切灵化算一次循环，变形按 1 s 估）",
            "radial": "暴击/触发取 wiki 该范围子攻击；takes_multishot 只写有出处的 false；takes_condition_overload 按 CO 页",
        }
    }
    out.update(dict(sorted(recs.items())))
    args.out.write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    if args.report:
        args.report.write_text("\n".join(report) + "\n", encoding="utf-8")
    print(f"✓ {args.out.name}: {len(recs)} 个灵化形态；提示 {len(report)} 条")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
