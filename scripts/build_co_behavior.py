# -*- coding: utf-8 -*-
"""从英文 wiki「Condition Overload (Mechanic)」Attack Catalog 生成武器的 CO 桶别 → core/data/co_behavior.json。

数据源（2026-10-26 wfsim 迁移 P2 起；之前取自 wfsim，已弃用）：wiki 该页「Attack Catalog」各表
（CC BY-NC-SA 3.0；表里只列与「Additive / +100%」不一致的攻击），按 Math/Behavior Type：
- ``Multiplying`` ⇒ ``independent``：CO 自成独立乘区（不被膛线类稀释）；
- ``N/A`` / ``Does not apply`` ⇒ ``inert``：CO 对该攻击无效；
- ``Adding``（及未列出的攻击）⇒ ``additive_with_base_damage``（缺省，不写）。
AoE 行不在这里（范围段吃不吃 CO 写在 weapon_attacks / incarnon_forms 的段上）。

CO 桶别按**攻击**给：计算器的主段 = 默认形态（「灵化」时 = 灵化形态），所以这里只收两槽 ——
``base``（与 weapons_stats 主段一致的 wiki 攻击，见 build_weapon_attacks.pick_main）与
``incarnon``；副开火 / 蓄力等非默认形态的桶别写在 weapon_attacks.json 该形态的 ``co_behavior``。
CO 页与数据模块的攻击名写法不同，按类别对（``_wiki_weapons.resolve_catalog_row``），对不上的行报出。
CO 页只列了同族某一把的，不自动推给同族其它武器（2026-10-26 维护者定：Prime / 变体是否同机制不能假定）；
逐把核实过的写在 ``_wiki_weapons.USER_VERIFIED_CO_ROWS``（每行带依据），其余按 wiki 原样（未列 = 缺省）。

key = 小写 uniqueName（= weapons_stats.json 的键）。只写非缺省：``{"base": "independent"}`` 等。

用法：python scripts/build_co_behavior.py --wiki <wiki 页面目录> [--out 路径] [--report 路径]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _wiki_weapons import catalog_by_weapon, catalog_rows, load_weapons  # noqa: E402
from build_weapon_attacks import (
    CO_DEFAULT,
    co_modes,
    entry_key,
    is_incarnon,
    pick_incarnon,
    pick_main,
    split_attacks,
)  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "core" / "data" / "co_behavior.json"
WS = ROOT / "core" / "data" / "weapons_stats.json"
# 计算器不建模的武器类别（近战 CO 另有口径；架势 / 增幅器不是武器面板）
SKIP_SECTIONS = ("Melees", "Stances", "Amps")


def build(wiki_dir: Path, ws_all: dict) -> tuple[dict, list[str]]:
    W = load_weapons(wiki_dir)
    report: list[str] = []
    weapons: dict[str, dict] = {}
    rows_all = [r for r in catalog_rows(wiki_dir) if r["section"] not in SKIP_SECTIONS]
    for wname, rows in sorted(catalog_by_weapon(rows_all).items()):
        e = W.get(f"{wname} (Atmosphere)") or W.get(wname)
        if e is None:
            report.append(f"{wname}：数据模块里没有这把武器，跳过")
            continue
        key = entry_key(wname if wname in W else f"{wname} (Atmosphere)", e)
        ws = ws_all.get(key) or {}
        if not ws:
            report.append(f"{wname}：计算器（weapons_stats）无此武器，跳过")
            continue
        roots = split_attacks(e)
        if not roots:
            continue
        main, _ok = pick_main(roots, ws, e.get("_TooltipAttackDisplay"))
        inc = pick_incarnon(roots)
        modes = co_modes(wname, roots, main, rows, report)
        rec = {}
        if modes.get(id(main), CO_DEFAULT) != CO_DEFAULT:
            rec["base"] = modes[id(main)]
        if inc is not None and modes.get(id(inc), CO_DEFAULT) != CO_DEFAULT:
            rec["incarnon"] = modes[id(inc)]
        # 灵化行写的是「Incarnon Mode」等泛称：pick_incarnon 之外的灵化攻击对上了也算灵化槽
        for r in roots:
            if (
                r is not inc
                and r is not main
                and is_incarnon(r)
                and modes.get(id(r), CO_DEFAULT) != CO_DEFAULT
            ):
                rec.setdefault("incarnon", modes[id(r)])
        if rec:
            weapons[key] = rec
    return weapons, report


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wiki", required=True, type=Path)
    ap.add_argument("--out", type=Path, default=OUT)
    ap.add_argument("--report", type=Path)
    args = ap.parse_args(argv[1:])
    weapons, report = build(args.wiki, json.loads(WS.read_text(encoding="utf-8")))
    out = {
        "_meta": {
            "source": "英文 wiki Condition Overload (Mechanic) Attack Catalog（CC BY-NC-SA 3.0）：Multiplying = "
            "independent，N/A / Does not apply = inert；只收默认主段（base）与灵化形态（incarnon）两槽；"
            "另加用户逐把核实的补充行（_wiki_weapons.USER_VERIFIED_CO_ROWS）",
            "values": "additive_with_base_damage（缺省，不写）/ independent / inert",
            "wiki_snapshot": args.wiki.name,
            "generator": "scripts/build_co_behavior.py",
        },
        "weapons": dict(sorted(weapons.items())),
    }
    args.out.write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    if args.report:
        args.report.write_text("\n".join(report) + "\n", encoding="utf-8")
    n_b = sum(1 for v in weapons.values() if "base" in v)
    n_i = sum(1 for v in weapons.values() if "incarnon" in v)
    print(f"✓ {args.out.name}：{len(weapons)} 把（原型 {n_b} / 灵化 {n_i}）；提示 {len(report)} 条")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
