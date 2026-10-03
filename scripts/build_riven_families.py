#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成紫卡家族谱系 core/data/de/riven_families.json（2026-10-03 起为正式生成器）。

数据源与规则（与 2026-10-02 首版一次性产物逐字节兼容，已对拍复现）：
1. **DE 官方导出** ExportWeapons.json（calamity-inc/warframe-public-export-plus，
   默认缓存 core/data/_cache/ExportWeapons.json，缺则从 browse.wf 拉）；
2. 显示名：name 是 /Lotus/Language/... 语言键 ⇒ 经 dict.en.json 解析；
   ★ 跳过解析名带 ``<ARCHWING>`` 前缀的条目（Archwing 近战，8 条；
   现行 842→819 的差正在这里）；
3. 家族根 = 沿 parentName 上溯，**仅当父条目本身可上紫卡（有 omegaAttenuation）
   才继续上溯**，否则自身为根；根键 = uniqueName 尾段；
4. 同名条目（NPC/玩家版、PvP 变体等 15 组）按 uniqueName 排序遍历、
   后写覆盖（与现行表取值一致：Dax 版根胜出）；
5. 人工归并补丁（DE 未给 parentName 的两族）：Hek 族 / DarkDagger 族；
6. ★ 2026-10-03 **双模式条目**：组合枪（Primary/Secondary）、空枪地面版
   （Atmosphere）、Dark Split-Sword（Dual Swords/Heavy Blade）等同一武器的
   两种形态。★ DE 公开导出**不含主武器组合枪腔体**（实测只有次要版 4+2 个
   Barrel），双模式条目以 dispositions_rivenmirror.json 的括号键为准（同源
   出自 wiki 倾向页，键为显示名如 ``Catchmoon (Primary)``）；基名在 by_name
   的 ⇒ 后缀键落到**基名同根**（例：「捕月」家族候选 = [捕月（主要）,
   捕月（次要）]）。无后缀基名键**保留不动**（向后兼容：family_key 全链路
   既有 819 件判定不受影响，差异清单里只有新增键）。

用法：
    python scripts/build_riven_families.py            # 用本地缓存导出重建
    python scripts/build_riven_families.py --diff     # 只打印与现行表的差异
"""
from __future__ import annotations

import argparse
import json
import re
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / "core" / "data" / "_cache"
EXPORT = CACHE / "ExportWeapons.json"
DICT_EN = CACHE / "dict.en.json"
RM = ROOT / "core" / "data" / "dispositions_rivenmirror.json"
OUT = ROOT / "core" / "data" / "de" / "riven_families.json"

EXPORT_URL = "https://browse.wf/warframe-public-export-plus/ExportWeapons.json"

# 与现行表一致的人工归并（DE 未给 parentName：Kuva/集团变体各自成根）
PATCHES = {
    "reason": "DE 未给这两族设 parentName（Kuva/集团变体各自成根），按同基础武器人工归并",
    "entries": {"Hek": "Hek", "Kuva Hek": "Hek", "Vaykor Hek": "Hek",
                "Dark Dagger": "DarkDagger",
                "Rakta Dark Dagger": "DarkDagger"},
}

# 双模式后缀（基名 + 后缀 ⇒ 同根）。仅认这些模式词，其它括号键跳过并登记。
MODE_RE = re.compile(r"^(?P<base>.+?)\s*\((?P<mode>Primary|Secondary|"
                     r"Atmosphere|Melee|Dual Swords|Heavy Blade)\)$", re.I)


def _load_export(refresh: bool) -> dict:
    if EXPORT.exists() and not refresh:
        return json.loads(EXPORT.read_text(encoding="utf-8"))
    CACHE.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(EXPORT_URL, headers={"User-Agent": "sdjk-build/1.0"})
    data = urllib.request.urlopen(req, timeout=180).read()
    EXPORT.write_bytes(data)
    return json.loads(data)


def _display(name_key: str, den: dict) -> str:
    if name_key.startswith("/Lotus/Language/"):
        return den.get(name_key, name_key.rsplit("/", 1)[-1])
    return name_key


def build() -> tuple[dict, list[str]]:
    export = _load_export(refresh=False)
    den = json.loads(DICT_EN.read_text(encoding="utf-8"))

    capable = {k: v for k, v in export.items() if v.get("omegaAttenuation")}
    root: dict[str, str] = {}
    for k, v in capable.items():
        r, p, seen = k, v.get("parentName"), set()
        while p and p in capable and p not in seen:
            seen.add(p)
            r = p
            p = capable[p].get("parentName")
        root[k] = r.rsplit("/", 1)[-1]

    by_name: dict[str, str] = {}
    skipped_arch = 0
    for k in sorted(capable):
        name = _display(capable[k].get("name") or "", den)
        if name.startswith("<ARCHWING>"):
            skipped_arch += 1          # Archwing 近战：现行表口径即不含
            continue
        by_name[name] = root[k]        # 同名后写覆盖（NPC/玩家版、PvP 变体）
    for name, r in PATCHES["entries"].items():
        by_name[name] = r

    base_count = len(by_name)

    # ★ 双模式第一步：导出里自带括号名的条目（实测：Vinquibus (Melee)，
    #   dict.en 显示名就带后缀、独立成根）—— 基名在表 ⇒ **并入基名根**。
    #   （有意变更：现行表它是独立根，紫卡倾向列不出同族；并入后与
    #   wiki 的 Vinquibus (Primary)/(Melee) 双形态口径一致。）
    remapped = []
    for name in list(by_name):
        m = MODE_RE.match(name)
        if not m:
            continue
        base = m.group("base").strip()
        hit = next((b for b in by_name if b.lower() == base.lower()), None)
        if hit is not None and by_name[name] != by_name[hit]:
            remapped.append(name)
            by_name[name] = by_name[hit]

    # ★ 双模式第二步：rivenmirror 括号键（显示名），基名在表 ⇒ 同根。
    #   主武器组合枪腔体不在 DE 公开导出里，来源是 wiki 倾向页（经
    #   dispositions_rivenmirror.json，键为显示名如 Catchmoon (Primary)）。
    entries = json.loads(RM.read_text(encoding="utf-8")).get("entries") or {}
    log: list[str] = []
    mode_added: dict[str, str] = {}
    for en in sorted(entries):
        m = MODE_RE.match(en)
        if not m:
            continue
        base = m.group("base").strip()
        hit = next((b for b in by_name if b.lower() == base.lower()), None)
        if hit is None:
            log.append(f"[跳过] 基名不在表：{en}")
            continue
        if en not in by_name:
            mode_added[en] = by_name[hit]
        by_name[en] = by_name[hit]

    families: dict[str, list] = defaultdict(list)
    for name, r in by_name.items():
        families[r].append(name)
    families = {r: sorted(ms) for r, ms in sorted(families.items()) if len(ms) > 1}

    multi = sum(1 for ms in families.values() if len(ms) > 1)
    payload = {
        "_source": "calamity-inc/warframe-public-export-plus @ExportWeapons.json "
                   "(DE 官方导出) + 双模式括号键（dispositions_rivenmirror.json，"
                   "同源 wiki 倾向页）",
        "_rule": "家族 = 沿 parentName 上溯，仅在 parent 本身「可上紫卡」"
                 "(有 omegaAttenuation) 时继续；否则自身为根。同名条目（NPC/玩家版、"
                 "PvP 变体）按 uniqueName 排序后写覆盖；跳过 <ARCHWING> 前缀条目。"
                 "★ 2026-10-03：双模式条目（组合枪 Primary/Secondary、Atmosphere、"
                 "Dark Split-Sword 双形态、Vinquibus (Melee) 等）基名同根，"
                 "无后缀基名键保留（向后兼容）。",
        "_generated_at": datetime.now(timezone.utc).date().isoformat(),
        "_counts": {"weapons": len(by_name), "families": len(families),
                    "multi_member": multi, "dual_mode": len(mode_added) + len(remapped),
                    "dual_mode_added": len(mode_added),
                    "dual_mode_remapped": sorted(remapped),
                    "base_before_dual_mode": base_count},
        "_patches": PATCHES,
        "by_name": dict(sorted(by_name.items())),
        "families": families,
    }
    for line in log:
        print(" ", line)
    return payload, log


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--diff", action="store_true",
                    help="只打印与现行 riven_families.json 的差异，不写文件")
    args = ap.parse_args()

    payload, _log = build()
    print(f"by_name {payload['_counts']['weapons']} 条"
          f"（双模式 +{payload['_counts']['dual_mode']}）| "
          f"families {payload['_counts']['families']} | "
          f"multi_member {payload['_counts']['multi_member']}")

    cur = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {}
    cur_bn = cur.get("by_name") or {}
    added = {k: v for k, v in payload["by_name"].items() if k not in cur_bn}
    removed = sorted(set(cur_bn) - set(payload["by_name"]))
    changed = [k for k in cur_bn
               if k in payload["by_name"] and payload["by_name"][k] != cur_bn[k]]

    def _is_dual_merge(k: str) -> bool:
        """值变是否属于「双模式并入基名根」（有意变更，允许）。"""
        m = MODE_RE.match(k)
        if not m:
            return False
        base = m.group("base").strip()
        hit = next((b for b in payload["by_name"]
                    if b.lower() == base.lower()), None)
        return hit is not None and \
            payload["by_name"][k] == payload["by_name"][hit]

    changed_ok = [k for k in changed if _is_dual_merge(k)]
    changed_bad = [k for k in changed if k not in changed_ok]
    print(f"与现行差异：新增 {len(added)} | 移除 {len(removed)} | "
          f"值变 {len(changed)}（其中双模式并入 {len(changed_ok)}）")
    for k in sorted(added):
        print(f"  + {k} → {added[k]}")
    for k in removed:
        print(f"  - {k}")
    for k in changed_ok:
        print(f"  ~ {k}: {cur_bn[k]} → {payload['by_name'][k]}（双模式并入）")
    for k in changed_bad:
        print(f"  ~ {k}: {cur_bn[k]} → {payload['by_name'][k]}")

    if args.diff:
        print("[diff] 未写入文件")
        return 0
    if removed or changed_bad:
        raise SystemExit("★ 出现移除/非双模式值变 —— 现行判定被改动，"
                         "须人工核对后才能写入")
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1,
                              sort_keys=False) + "\n", encoding="utf-8")
    print(f"[OK] {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
