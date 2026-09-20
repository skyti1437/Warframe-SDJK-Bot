# -*- coding: utf-8 -*-
"""拿 **wiki 的 `Module:Mods/data`** 全量对拍我们的 MOD 库。

为什么要有它（2026-09-20 用户实测报障）：
    用户报「剑风（非 p）满级就 3，你这个 5 级哪里来的」——
    查下去发现 `mods_stats.json` 的**仅识别表从没被校准过**，14 条 max_rank 错。
    当时是用 wfsim dump 校准的；用户随后指出 wiki 的 `Module:Mods/data`
    本身就带 BaseDrain / MaxRank / Polarity / **Rarity** / Type，是更权威的一手源。

★★ 实测结论：**这份模块的 `BaseDrain` 不能用来校准游戏内数值**（2026-09-20 逐项验证）：
    用用户自己的配卡截图裁决 ——
      · 分裂膛室（Split Chamber）：卡面 **15**、亮豆 5 颗（rank 5）。
        我们的 base=10 → 10+5 = 15 ✓；模块说 4 → 会是 9 ✗
      · 另一张卡面 **8**、亮豆 4 颗：我们的 base=4 → 4+4 = 8 ✓；模块 2 → 6 ✗
      · 压迫点 / 剑风 / 北风 / 牺牲斩铁 / 热病打击 Prime：模块与我们的值**一致** ✓
    19 条 base_drain 差异全部是「模块偏低」（6→4、10→4、4→2…），规律可疑；
    因此本脚本**只报告、不写回** base_drain。
    同理 `MaxRank` 也有 6 条与 DE 自己的导出（WFCD，按 uniqueName 关联）冲突，
    wfsim 的 yaml 里明确记过「the module is wrong here」+「wiki PAGE 的等级表也与我们一致」
    → 这 6 条列在 `KNOWN_MODULE_WRONG`，**永不应用**。

用法::

    python scripts/audit_mods_vs_wiki.py <wiki_mods_data.lua> [--apply] [--rarity]

* 不带 `--apply`：只报告差异（默认）
* `--apply`：把 **MaxRank / Polarity** 写回（BaseDrain 永远不写）
* `--rarity`：同时把 `Rarity` 写进库里（`rarity` 字段，供卡面边框/稀有度用）

★ 姿态卡哨兵：库里 `base_drain < 0`（-2）表示「姿态卡，不参与折算」，
  这是 `loadout_ocr` 的判据 —— **整条跳过，不要用 wiki 的 0 覆盖**。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODS = ROOT / "core" / "data" / "mods_stats.json"

# ★ 已核实「wiki 模块写错、我们的值才是对的」—— 永不应用（见模块 docstring）。
#   wfsim 的 yaml 对这 6 条都留了说明：DE 导出 fusionLimit=3、wiki **正文**的等级表
#   也一致（例：Eagle Eye 是 +10%→+40% Zoom，4 个等级）。
KNOWN_MODULE_WRONG = {
    "Charged Chamber", "Eagle Eye", "Energy Channel", "Finishing Touch",
    "Hawk Eye", "Steady Hands",
}

# wiki 的 Polarity 写法 → 我们库里的写法（小写）
POLARITY = {
    "madurai": "madurai", "vazarin": "vazarin", "naramon": "naramon",
    "zenurik": "zenurik", "umbra": "umbra", "penjaga": "penjaga",
    "unairu": "unairu", "universal": "universal", "any": "any",
    "": "", "none": "",
}


def parse_wiki(path: Path) -> dict:
    """解析 wiki 的 Lua `Mods` 表 → {英文名: {字段…}}。

    Lua 文本极规整（`["名"] = {` + `键 = 值,`），直接用正则解析；不引第三方库。
    """
    txt = path.read_text(encoding="utf-8", errors="ignore")
    i = txt.find("Mods = {")
    if i < 0:
        raise SystemExit("没找到 Mods 表（文件不对？）")
    seg = txt[i:]
    # 每个条目：["Name"] = { ... },
    entries = re.split(r'\n\t\t\["', seg)
    out: dict = {}
    for e in entries[1:]:
        if "] = {" not in e:
            continue
        name, rest = e.split('"] = {', 1)
        body = rest.split("\n\t\t},", 1)[0]
        rec: dict = {}
        for key, val in re.findall(r"\n\t\t\t(\w+) = (.+?),?\s*$", body, re.M):
            v = val.strip().rstrip(",")
            if v.startswith('"') and v.endswith('"'):
                rec[key] = v[1:-1]
            elif v in ("true", "false"):
                rec[key] = v == "true"
            elif re.fullmatch(r"-?\d+", v):
                rec[key] = int(v)
        if rec:
            out[name] = rec
    return out


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    apply = "--apply" in sys.argv
    want_rarity = "--rarity" in sys.argv
    if not args:
        raise SystemExit(__doc__)
    wiki = parse_wiki(Path(args[0]))
    print(f"wiki 侧解析出 {len(wiki)} 个 mod")

    data = json.loads(MODS.read_text(encoding="utf-8"))
    problems = {"base_drain": [], "max_rank": [], "polarity": [], "rarity": []}
    n_fix = 0
    for tbl in ("mods", "names"):
        for key, rec in (data.get(tbl) or {}).items():
            name = str(rec.get("name") or "")
            w = wiki.get(name)
            if not w:
                continue
            if isinstance(rec.get("base_drain"), int) and rec["base_drain"] < 0:
                continue                      # ★ 姿态卡哨兵，整条跳过
            for field, wkey in (("base_drain", "BaseDrain"),
                                ("max_rank", "MaxRank"),
                                ("polarity", "Polarity")):
                new = w.get(wkey)
                if new is None:
                    continue
                if field == "polarity":
                    new = POLARITY.get(str(new).lower(), str(new).lower())
                old = rec.get(field)
                if field == "polarity" and isinstance(old, str) \
                        and old.lower() == str(new).lower():
                    continue
                if old != new:
                    problems[field].append((tbl, key, old, new))
                    # ★ base_drain 永不写回（模块的值与游戏不符，见模块 docstring）；
                    #   max_rank 对已核实的 6 条例外也不写。
                    if not apply:
                        continue
                    if field == "base_drain":
                        continue
                    if field == "max_rank" and name in KNOWN_MODULE_WRONG:
                        continue
                    rec[field] = new
                    n_fix += 1
            if want_rarity and w.get("Rarity"):
                if rec.get("rarity") != w["Rarity"]:
                    problems["rarity"].append((tbl, key, rec.get("rarity"), w["Rarity"]))
                    if apply:
                        rec["rarity"] = w["Rarity"]
                        n_fix += 1

    for field, rows in problems.items():
        print(f"\n=== {field} 不一致：{len(rows)} 条 ===")
        for tbl, key, old, new in rows[:40]:
            print(f"  [{tbl}] {key:28s} {old} → {new}")
        if len(rows) > 40:
            print(f"  …还有 {len(rows) - 40} 条")

    if apply and n_fix:
        json.dump(data, MODS.open("w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print(f"\n✓ 已写回 {MODS}（{n_fix} 处）")
    elif not apply:
        print("\n（dry-run；要写回加 --apply）")
    total = sum(len(v) for k, v in problems.items() if k != "rarity")
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
