# -*- coding: utf-8 -*-
"""从 wfsim 抽取武器的「异况超量（CO）桶别」→ core/data/co_behavior.json。

数据源：github.com/magenie33/wfsim data/weapons/**/*.yaml 的顶层 ``co_behavior``：
- ``additive_with_base_damage``（默认，未写即此）：CO 与基伤 MOD 同一加法括号；
- ``independent``：CO 自成独立乘区（不被膛线类稀释）；
- ``inert``：CO 对该攻击完全无效。
（wfsim engine/src/data/weapons/panel.rs:192-195「a weapon it does not list is
additive_with_base_damage, never independent」；co 只作用于直击，见 attack.rs:704-708。）

**只收默认形态与灵化形态**：co_behavior 是按**攻击**给的（catalog.rs:54-56：
Mandonel 未蓄力与蓄力两行不同）。计算器的主段 = 默认形态（「灵化」时 = 灵化形态），
副开火 / 蓄力 / 点射等形态的值若按武器名混进来，会覆盖主形态 —— 研究仓旧表
（2026-10-06）即有 16 把因此错配（如 Grimoire 主开火 additive 被记成 inert）。

key = 默认形态的 internal_name 小写（= weapons_stats.json 的 uniqueName 小写）；
灵化形态没有 internal_name，经 transforms_from 回到其原型的 internal_name。
只写非 additive 的条目：``{"base": "independent"}`` / ``{"incarnon": "inert"}``。

用法：python scripts/build_co_behavior.py [wfsim 仓库根或其 data 目录]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # noqa: BLE001 —— 插件运行本身不需要 yaml，只有数据构建脚本要
    raise SystemExit("本脚本需要 PyYAML：pip install pyyaml（插件运行时并不依赖它，仅构建数据用）")

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "core" / "data" / "co_behavior.json"
DEFAULT = "additive_with_base_damage"
VALID = {DEFAULT, "independent", "inert"}


def _data_dir(arg: str | None) -> Path:
    p = Path(arg) if arg else Path.home() / "tmp" / "wfsim"
    return p if (p / "weapons").is_dir() else p / "data"


def main(argv: list[str]) -> int:
    data = _data_dir(argv[1] if len(argv) > 1 else None)
    ents: dict[str, dict] = {}
    for p in sorted((data / "weapons").rglob("*.yaml")):
        d = yaml.safe_load(p.read_text(encoding="utf-8"))
        if isinstance(d, dict) and d.get("id"):
            ents[str(d["id"])] = d
    if not ents:
        raise SystemExit(f"没找到 wfsim 武器数据：{data / 'weapons'}")

    weapons: dict[str, dict] = {}
    for d in ents.values():
        if d.get("default_form"):
            key, slot = str(d.get("internal_name") or "").lower(), "base"
        elif d.get("form") == "incarnon":
            base = ents.get(str(d.get("transforms_from") or "")) or {}
            key, slot = str(base.get("internal_name") or "").lower(), "incarnon"
        else:
            continue  # 副开火 / 蓄力 / 点射等：不是计算器主段
        cb = str(d.get("co_behavior") or DEFAULT)
        if cb not in VALID:
            raise SystemExit(f"{d['id']}: 未知 co_behavior {cb!r}")
        if not key or cb == DEFAULT:
            continue
        weapons.setdefault(key, {})[slot] = cb

    out = {
        "_meta": {
            "source": "github.com/magenie33/wfsim data/weapons/**（顶层 co_behavior；"
            "只收 default_form 与 form: incarnon 两种形态）",
            "values": "additive_with_base_damage（缺省，不写）/ independent / inert",
            "generator": "scripts/build_co_behavior.py",
        },
        "weapons": dict(sorted(weapons.items())),
    }
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    n_b = sum(1 for v in weapons.values() if "base" in v)
    n_i = sum(1 for v in weapons.values() if "incarnon" in v)
    print(f"✓ {OUT.relative_to(ROOT)}：{len(weapons)} 把（原型 {n_b} / 灵化 {n_i}）")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
