#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""重建 core/data/relic_index.json + relic_inverse.json（遗物奖励 / 部件反查）。

数据源（与 KB 04_遗物.md 同源，见 warframe-kb-build 技能）：
  items/Relics.json（WFCD）+ kb_lib.Sources 的官方简中译名链
  （含 scripts/kb/zh_overrides_items.json 生成覆盖层）。

产出：
  relic_index.json    {"后纪 Axi V12": {"常见": [...], "罕见": [...], "稀有": [...]}}
  relic_inverse.json  {"逐电 Prime 枪管": [{"relic": "后纪 Axi V12", "rarity": "稀有"}, ...]}

口径（2026-09-24 固化）：
- 奖励内容取 **Intact（完整）** 档（四档奖励相同、仅概率不同）。
- 稀有度按 Intact 概率分组：>20% → 常见、>5% → 罕见、其余 → 稀有
  （实测全部 799 组签名统一为 2/11/11/25.33/25.33/25.33）。
- 键形态「中文纪元 英文代号」（如「后纪 Axi V12」）与 main.py::_norm_relic 消费端一致。
- 部件名走 S.name()（官方简中，缺失回落英文显示名）。
- 排序：遗物按纪元 + 编号自然序；每档奖励按概率降序。

用法：
    export WF_KB_DATA=<解包数据目录>
    python scripts/kb/build_relic_index.py [--out-dir core/data] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
from kb_lib import Sources, RELIC_ERA_ZH              # noqa: E402

_ERA_ORDER = ["古纪", "前纪", "中纪", "后纪", "安魂", "先锋", "全能", "虚空"]
_RELIC_NAME_RE = re.compile(
    r"^(.+?)\s+(Intact|Exceptional|Flawless|Radiant)$")


def _relic_sort_key(key: str):
    """「后纪 Axi V12」→ (纪元序, 代号字母, 编号) 自然序。"""
    parts = key.split()
    era = _ERA_ORDER.index(parts[0]) if parts[0] in _ERA_ORDER else 99
    code = parts[-1]
    m = re.match(r"^([A-Za-z]+)(\d+)$", code)
    return (era, m.group(1) if m else code, int(m.group(2)) if m else 0)


def rarity_of(chance: float) -> str:
    if (chance or 0) > 20:
        return "常见"
    if (chance or 0) > 5:
        return "罕见"
    return "稀有"


def build(S: Sources):
    rel = S.items["Relics"]
    groups = {}
    for x in rel:
        m = _RELIC_NAME_RE.match(x.get("name") or "")
        if m:
            groups.setdefault(m.group(1), {})[m.group(2)] = x
        else:
            groups.setdefault(x.get("name") or "?", {})["__other__"] = x
    groups = {k: v for k, v in groups.items()
              if any(x.get("rewards") for x in v.values())}

    index, inverse = {}, defaultdict(dict)
    for gname, g in groups.items():
        rep = g.get("Intact") or next(iter(g.values()))
        # 键：中文纪元 + 英文代号（Requiem Eterna Relic → 安魂 Requiem Eterna）
        parts = gname.replace(" Relic", "").split()
        era_zh = RELIC_ERA_ZH.get(parts[0], parts[0])
        code = parts[1] if len(parts) > 1 else ""
        key = f"{era_zh} {parts[0]} {code}".strip()
        slots = defaultdict(list)
        for rw in sorted(rep.get("rewards") or [],
                         key=lambda a: -(a.get("chance") or 0)):
            it = rw.get("item") or {}
            u, en = it.get("uniqueName"), it.get("name")
            if not u:
                continue
            nm = S.name(u) or en or "?"
            rar = rarity_of(rw.get("chance") or 0)
            slots[rar].append((-(rw.get("chance") or 0), nm))
            prev = inverse[nm].get(key)
            ch = rw.get("chance") or 0
            if prev is None or ch > prev[0]:
                inverse[nm][key] = (ch, rar)
        index[key] = {r: [nm for _c, nm in sorted(v)]
                      for r, v in slots.items()}
    return index, dict(inverse)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", default=str(ROOT / "core" / "data"))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    S = Sources()
    index, inverse = build(S)
    index = {k: index[k] for k in sorted(index, key=_relic_sort_key)}
    inv_payload = {
        nm: [{"relic": k, "rarity": v[1]}
             for k, v in sorted(orig.items(), key=lambda kv: _relic_sort_key(kv[0]))]
        for nm, orig in sorted(inverse.items())
    }

    out = Path(args.out_dir)
    old_idx_p = out / "relic_index.json"
    old_inv_p = out / "relic_inverse.json"
    old_idx = json.loads(old_idx_p.read_text(encoding="utf-8")) if old_idx_p.exists() else {}
    old_inv = json.loads(old_inv_p.read_text(encoding="utf-8")) if old_inv_p.exists() else {}

    print(f"遗物 {len(old_idx)} → {len(index)}"
          f"（新增 {len(set(index) - set(old_idx))}、移除 {len(set(old_idx) - set(index))}）")
    print(f"部件 {len(old_inv)} → {len(inv_payload)}"
          f"（新增 {len(set(inv_payload) - set(old_inv))}、移除 {len(set(old_inv) - set(inv_payload))}）")
    added = sorted(set(index) - set(old_idx), key=_relic_sort_key)
    if added:
        print("  新遗物样例:", added[:8])
    removed = sorted(set(old_idx) - set(index), key=_relic_sort_key)
    if removed:
        print("  ⚠ 移除:", removed[:8])
    no_zh = [nm for nm in inv_payload if not re.search(r"[\u4e00-\u9fff]", nm)]
    print(f"  部件名纯英文（安魂 MOD 等预期）: {len(no_zh)} {no_zh[:5]}")

    if args.dry_run:
        return 0
    old_idx_p.write_text(json.dumps(index, ensure_ascii=False, indent=1),
                         encoding="utf-8")
    old_inv_p.write_text(json.dumps(inv_payload, ensure_ascii=False, indent=1),
                         encoding="utf-8")
    print("[OK]", old_idx_p, "|", old_inv_p)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
