# -*- coding: utf-8 -*-
"""赤毒 / 信条 / 终幕武器的回响（Valence）加成范围 → core/data/lich_valence.json。

数据源（2026-10-26 wfsim 迁移 P3 起；之前取自 wfsim 各武器 valence 字段，已弃用），英文 wiki（CC BY-NC-SA 3.0）：
- 武器名单 = ``Module:Weapons/data`` 里 ``IsLichWeapon = true`` 或出厂带回响内置词条（``DefaultUpgrades`` 含
  KuvaLich/Upgrades/InnateDamageRandomMod）的条目（``is_lich_weapon``；单看一个字段都不全）；
- 规则 = 「Kuva (Variant)」「Tenet」「Coda」三页同一段原文：「bonus damage of one damage type which can either
  be Impact, Heat, Cold, Electricity, Toxin, Magnetic, or Radiation, ranging from 25-60% of the weapon's base
  damage」；上限 60% 另见「Valence Fusion」页。三类武器规则相同，所以每把都是同一组值。

key = 英文名小写、空白与撇号换成下划线（与 damage_calc._weapon_family_key / loadout_ocr._is_lich_weapon 一致）。

用法：python scripts/build_lich_valence.py --wiki <wiki 页面目录> [--out 路径]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _wiki_weapons import load_weapons  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "core" / "data" / "lich_valence.json"
ELEMENTS = ["impact", "heat", "cold", "electricity", "toxin", "magnetic", "radiation"]
# 回响加成的内置词条（赤毒武器出厂即带；Kuva Ghoulsaw 没写 IsLichWeapon，但带这条，Traits 也写了 Kuva Lich）
VALENCE_INNATE = "/KuvaLich/Upgrades/InnateDamageRandomMod"


def is_lich_weapon(e: dict) -> bool:
    """IsLichWeapon，或出厂带回响内置词条（两者并集 = Traits 含 Kuva Lich / Tenet / Coda 的 51 把）。"""
    return bool(e.get("IsLichWeapon")) or any(
        VALENCE_INNATE in str(u) for u in e.get("DefaultUpgrades") or []
    )


PCT_MIN, PCT_MAX = 25.0, 60.0


def family_key(name: str) -> str:
    return re.sub(r"[\s'’]+", "_", name.strip().lower())


def build(wiki_dir: Path) -> dict:
    out: dict = {}
    for name, e in sorted(load_weapons(wiki_dir).items()):
        if not is_lich_weapon(e) or name.endswith(" (Atmosphere)"):
            continue
        out[family_key(name)] = {
            "name": name,
            "elements": list(ELEMENTS),
            "min": PCT_MIN,
            "max": PCT_MAX,
        }
    return out


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wiki", required=True, type=Path)
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args(argv[1:])
    recs = build(args.wiki)
    out = {
        "_meta": {
            "source": "英文 wiki：武器名单 = Module:Weapons/data 的 IsLichWeapon；元素与 25–60% 范围 = "
            "Kuva (Variant) / Tenet / Coda 三页原文（上限另见 Valence Fusion），CC BY-NC-SA 3.0",
            "wiki_snapshot": args.wiki.name,
            "generator": "scripts/build_lich_valence.py",
            "note": "valence bonus：玩家回响滚出的额外元素加成，数值在 min-max 内；元素必须取 elements 列表之一。"
            "加成实现 = 总基伤×pct 计入该元素",
        }
    }
    out.update(recs)
    args.out.write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"✓ {args.out.name}：{len(recs)} 把赤毒 / 信条 / 终幕武器")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
