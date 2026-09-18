# -*- coding: utf-8 -*-
"""构建玄骸武器表（赤毒 Kuva / 信条 Tenet / 科达 Coda）。

数据链路（全本地，无需联网）：
  core/data/weapons_stats.json   武器清单（含 uniqueName，来自 warframe-items）
    → 按英文名前缀筛出 Kuva*/Tenet*/Coda*
  core/data/de/name_zh.json      DE 官方简中（键 = uniqueName 小写）
    → 得到官方译名「赤毒·布拉玛」

产出：
  core/data/lich_weapons.json    {slug: {type, en, zh, zh_nodot, alias[]}}
  core/data/aliases.json         lich_items 由本脚本重建（别名 → slug）

为什么需要它（2026-09-18 用户反馈）：
  lich_items 原来只有 30 条手写赤毒武器，**信条一把都没有** →
  「xh 信条弧电离子枪」必然查不到；「赤毒海克」也不在表里。
  改为从数据自动生成，赤毒/信条/科达全覆盖，且随武器库更新自动跟进。

用法：
    python scripts/build_lich_names.py            # 生成
    python scripts/build_lich_names.py --check    # 只看统计不写文件
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "core" / "data"

# 英文名前缀 → (WM 拍卖类型, 官方简中前缀)
PREFIX = {
    "Kuva": ("lich", "赤毒"),
    "Tenet": ("sister", "信条"),
    # ★ DE 官方简中把 Coda 译作「终幕」（core/data/rotations.json 里也是这个口径），
    #   不是音译的「科达」—— 但玩家两种都叫，所以两个前缀都进别名。
    "Coda": ("coda", "终幕"),
}
# 同一类型在玩家口中的其它叫法（生成为别名，不必改官方译名）
EXTRA_ZH_PREFIX = {"coda": ("科达",)}
# 官方简中里的分隔符（「赤毒·布拉玛」）——玩家口语不带点，两种都要能查
SEP = re.compile(r"[·・\s]+")


def slug_of(en: str) -> str:
    """英文名 → WM slug：Kuva Bramma → kuva_bramma（Tenet Arca Plasmor → tenet_arca_plasmor）。"""
    return re.sub(r"[^a-z0-9]+", "_", en.lower()).strip("_")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()

    stats = json.loads((DATA / "weapons_stats.json").read_text(encoding="utf-8"))
    zh_map = json.loads((DATA / "de" / "name_zh.json").read_text(encoding="utf-8"))

    out: dict[str, dict] = {}
    missing_zh: list[str] = []
    for v in stats.values():
        if not isinstance(v, dict):
            continue
        en = v.get("name") or ""
        pre = next((p for p in PREFIX if en.startswith(p + " ")), None)
        if not pre:
            continue
        kind, zh_pre = PREFIX[pre]
        un = (v.get("uniqueName") or "").lower()
        zh = zh_map.get(un) or ""
        if not zh:
            missing_zh.append(en)
        short = en[len(pre) + 1:]                      # 去掉前缀的英文名
        zh_nodot = SEP.sub("", zh) if zh else ""
        # 别名集合：官方名、去点官方名、纯英文、去掉前缀的英文/中文
        alias = {en, en.lower(), short, short.lower()}
        if zh:
            alias |= {zh, zh_nodot}
            if zh_nodot.startswith(zh_pre):
                tail = zh_nodot[len(zh_pre):]          # 「布拉玛」
                alias.add(tail)
                for alt in EXTRA_ZH_PREFIX.get(kind, ()):   # 「科达血肢」
                    alias.add(alt + tail)
        out[slug_of(en)] = {
            "type": kind,
            "en": en,
            "zh": zh or f"{zh_pre}·{short}",           # 兜底：前缀+英文名
            "zh_nodot": zh_nodot or f"{zh_pre}{short}",
            "alias": sorted(a for a in alias if a),
        }

    by_kind: dict[str, int] = {}
    for r in out.values():
        by_kind[r["type"]] = by_kind.get(r["type"], 0) + 1
    print(f"玄骸武器共 {len(out)} 把：{by_kind}")
    if missing_zh:
        print(f"⚠ {len(missing_zh)} 把没有官方简中，已用「前缀+英文名」兜底：{missing_zh[:5]}")
    for s in list(out)[:4]:
        r = out[s]
        print(f"  {s:26s} {r['type']:7s} {r['zh']:14s} alias={r['alias'][:4]}")

    if args.check:
        return 0

    (DATA / "lich_weapons.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"\n已写 core/data/lich_weapons.json（{len(out)} 把）")

    # aliases.json 的 lich_items 由本表重建：别名 → slug（保留原有手工别名）
    ap_path = DATA / "aliases.json"
    aliases = json.loads(ap_path.read_text(encoding="utf-8"))
    old = aliases.get("lich_items") or {}
    merged: dict[str, str] = {}
    for slug, r in out.items():
        for a in r["alias"]:
            merged[a] = slug
    for k, v in old.items():                            # 手工别名优先（黑话更准）
        if isinstance(v, str) and v:
            merged[k] = v
    aliases["lich_items"] = dict(sorted(merged.items()))
    aliases.setdefault("_说明", {})
    ap_path.write_text(json.dumps(aliases, ensure_ascii=False, indent=1) + "\n",
                       encoding="utf-8")
    print(f"已更新 aliases.json → lich_items：{len(old)} 条 → {len(merged)} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
