# -*- coding: utf-8 -*-
"""从英文 wiki 固定快照生成枪械灵化面板与近战 Genesis 灵化加成 → core/data/incarnon_forms.json。

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

枪械属性是灵化形态的基础面板，不含可选灵化进化 perk（各人选择不同，识卡侧从面板反推吸收）。
近战第一批只收下方 MELEE_GENESIS_PAGES 里 14 个 Genesis 页表头明确列出的变体，且必须在武器模块中为 Melee、
能用 entry_key 对应当前 weapons_stats，并在 EVO1 明写「+100% Melee Damage」。写成 melee_buff 的
damage_mod_pct=100.0（基伤 MOD 桶），不替换基础面板，不建模特殊攻击、激活时间或持续时间。
原生灵化 Ruvox 仅收 EVO1 普通命中的冲击转穿刺、独立攻速倍率与范围；IPS MOD、重击、震地、
虚空尖刺与可选进化攻速不建模。Innodem / Praedos、当前库缺失的 Thalys 均不接入。
key = 具体武器的小写 uniqueName（= weapons_stats.json 的键）。

用法：python scripts/build_incarnon_forms.py --wiki <wiki 页面目录> [--out 路径]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _wiki_weapons import catalog_by_weapon, catalog_rows, damage, load_weapons  # noqa: E402
from build_evolutions import clean, parse_page, split_cells  # noqa: E402
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


# 首批范围锁定；每个变体仍须从该页的 Evolutions 表头取得，绝不按家族扩展。
MELEE_GENESIS_PAGES = (
    "Ack & Brunt",
    "Anku",
    "Bo",
    "Ceramic Dagger",
    "Destreza",
    "Dual Ichor",
    "Furax",
    "Hate",
    "Magistar",
    "Nami Solo",
    "Obex",
    "Okina",
    "Sibear",
    "Skana",
)
_MELEE_DAMAGE_100 = re.compile(r"(?<![\d.])\+100(?:\.0+)?%\s+Melee Damage\b", re.I)


def build_melee_forms(wiki_dir: Path, weapons: dict, ws_all: dict, report: list[str]) -> dict:
    """只提取首批 Genesis EVO1 明确的基伤 MOD 加成；其它字段保留原文作出处。"""
    out: dict = {}
    allowed_files = {
        f"{name.replace(' ', '_')}_Incarnon_Genesis.wiki" for name in MELEE_GENESIS_PAGES
    }
    for path in sorted((wiki_dir / "evo").glob("*.wiki")):
        if path.name in allowed_files:
            continue
        page_name = path.stem.replace("_", " ")
        if page_name == "Ruvox":
            continue  # 原生灵化由 build_native_melee_forms 独立校验，不能走 Genesis 基伤桶。
        if page_name in {"Innodem", "Praedos", "Thalys"}:
            report.append(f"{page_name}：原生灵化，不在首批 Genesis 范围，不接入")
        elif path.stem.endswith("_Incarnon_Genesis"):
            variants, _ = parse_page(path.read_text(encoding="utf-8"))
            if any(weapons.get(name, {}).get("Slot") == "Melee" for name in variants):
                report.append(f"{page_name}：近战 Genesis 页不在首批允许清单，不接入")
    for page_weapon in MELEE_GENESIS_PAGES:
        page_name = f"{page_weapon} Incarnon Genesis"
        path = wiki_dir / "evo" / f"{page_name.replace(' ', '_')}.wiki"
        if not path.is_file():
            report.append(f"{page_name}：缺少页面源码，跳过")
            continue
        raw = path.read_bytes()
        variants, rows = parse_page(raw.decode("utf-8"))
        if variants == [""]:
            variants = [page_weapon]
            report.append(f"{page_name}：Evolutions 表头为空，仅回退该页武器 {page_weapon}")
        if not variants or any(not name for name in variants):
            report.append(f"{page_name}：无完整变体表头，跳过")
            continue
        evo1 = [row for row in rows if row["tier"] == "1"]
        if len(evo1) != 1:
            report.append(f"{page_name}：EVO1 行数为 {len(evo1)}，应恰有一行，跳过")
            continue
        description = " ".join(clean(line.lstrip("* ")) for line in evo1[0]["lines"])
        if not _MELEE_DAMAGE_100.search(description):
            report.append(f"{page_name}：EVO1 未明确 +100% Melee Damage，跳过：{description}")
            continue
        accepted = 0
        for wname in variants:
            entry = weapons.get(wname)
            if entry is None:
                report.append(f"{page_name} / {wname}：武器模块无此明确变体，跳过")
                continue
            if entry.get("Slot") != "Melee":
                report.append(
                    f"{page_name} / {wname}：Slot={entry.get('Slot')!r}，不是 Melee，跳过"
                )
                continue
            key = entry_key(wname, entry)
            ws = ws_all.get(key) or {}
            if not key or not ws:
                report.append(f"{page_name} / {wname}：entry_key 未匹配当前 weapons_stats，跳过")
                continue
            zh = str(ws.get("zh") or "")
            if not zh:
                report.append(f"{page_name} / {wname}：weapons_stats 缺中文名，跳过")
                continue
            if key in out:
                report.append(f"{page_name} / {wname}：重复 entry_key={key}，保留首条并跳过")
                continue
            out[key] = {
                "kind": "melee_buff",
                "damage_mod_pct": 100.0,
                "base_name": wname,
                "base_zh": zh,
                "form_name": f"{wname} (Incarnon Form)",
                "form_zh": f"{zh} (灵化形态)",
                "source": {
                    "wiki_page": page_name,
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "snapshot": wiki_dir.name,
                },
                "evo1_description": description,
            }
            accepted += 1
        report.append(
            f"{page_name}：表头明确变体 {len(variants)} 个，接入 {accepted} 个；"
            "仅提取 EVO1 +100% Melee Damage，特殊攻击/时长不建模"
        )
    return out


def build_native_melee_forms(
    wiki_dir: Path, weapons: dict, ws_all: dict, report: list[str]
) -> dict:
    """仅接入固定出处确认的 Ruvox 普通命中；任何关键语义漂移均跳过。"""
    page = wiki_dir / "evo" / "Ruvox.wiki"
    module = wiki_dir / "melee.lua"
    entry = weapons.get("Ruvox") or {}
    internal_name = "/Lotus/Weapons/Thanotech/EntFistIncarnon/EntFistIncarnon"

    def reject(reason: str) -> dict:
        report.append(f"Ruvox：{reason}，原生灵化不接入")
        return {}

    if not page.is_file() or not module.is_file():
        return reject("缺少 EVO 页或 melee.lua 固定源码")
    if entry.get("Slot") != "Melee" or entry.get("InternalName") != internal_name:
        return reject("Slot / InternalName 与指定武器不一致")
    key = entry_key("Ruvox", entry)
    ws = ws_all.get(key) or {}
    if key != internal_name.lower() or ws.get("name") != "Ruvox" or not ws.get("zh"):
        return reject("entry_key / weapons_stats 身份或中文名未匹配")
    raw = page.read_bytes()
    text = raw.decode("utf-8")
    variants, rows = parse_page(text)
    evo1 = [row for row in rows if row["tier"] == "1"]
    if variants not in ([""], ["Ruvox"]) or len(evo1) != 1:
        return reject("EVO1 或变体表头不唯一")
    row = evo1[0]
    lines = [clean(line.lstrip("* ")) for line in row["lines"]]
    required = {
        "Reach 6x Combo and then Heavy Attack to activate Incarnon Form.",
        "Heavy Slam attacks impale nearby enemies on Void Spikes.",
        "+3 Range",
        "-35% Max Melee Attack Speed",
        "+100% of Impact Damage converted to Puncture Damage",
    }
    # 完整语句而非数字匹配，避免反向转换、其它字段的 35/100 或条件化描述误入。
    if row["name"] != "Incarnon Form" or set(lines) != required or len(lines) != len(required):
        return reject("EVO1 激活 / 范围 / 攻速 / 转换语句与已核出处不一致")
    blocks = re.split(r"\n\|-[^\n]*", text)
    evo1_blocks = [block for block in blocks if re.search(r"^!\s*EVO1\s*$", block, re.M)]
    if len(evo1_blocks) != 1:
        return reject("无法唯一定位 EVO1 备注")
    cells = split_cells(evo1_blocks[0])
    note_lines = (
        [clean(line.lstrip("* ")) for line in cells[-1][1].splitlines()] if len(cells) == 4 else []
    )
    if (
        "Melee Attack Speed curse is multiplicative to all other attack speed buffs."
        not in note_lines
    ):
        return reject("EVO1 备注未明确独立乘算攻速")
    report.append(
        "Ruvox：接入 1 个原生灵化，仅普通命中转换、独立 ×0.65 攻速与 +3 m 范围；"
        "IPS MOD/重击/震地/虚空尖刺/进化攻速未覆盖"
    )
    zh = str(ws["zh"])
    return {
        key: {
            "kind": "melee_native",
            "damage_conversion": {"from": "impact", "to": "puncture", "ratio": 1.0},
            "attack_speed_multiplier": 0.65,
            "range_bonus_m": 3.0,
            "supported_attacks": ["Normal Attack"],
            "base_name": "Ruvox",
            "base_zh": zh,
            "form_name": "Ruvox (Incarnon Form)",
            "form_zh": f"{zh} (灵化形态)",
            "source": {
                "wiki_page": "Ruvox",
                "sha256": hashlib.sha256(raw).hexdigest(),
                "weapon_module_sha256": hashlib.sha256(module.read_bytes()).hexdigest(),
                "snapshot": wiki_dir.name,
            },
            "evo1_description": " ".join(lines),
        }
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
    out.update(build_melee_forms(wiki_dir, W, ws_all, report))
    out.update(build_native_melee_forms(wiki_dir, W, ws_all, report))
    return out, report


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wiki", required=True, type=Path)
    ap.add_argument("--out", type=Path, default=OUT)
    ap.add_argument("--report", type=Path)
    args = ap.parse_args(argv[1:])
    recs, report = build(args.wiki, json.loads(WS.read_text(encoding="utf-8")))
    melee_count = sum(rec.get("kind") == "melee_buff" for rec in recs.values())
    native_count = sum(rec.get("kind") == "melee_native" for rec in recs.values())
    out = {
        "_meta": {
            "source": "英文 wiki Module:Weapons/data（原型条目里的 Incarnon 攻击）+ Condition Overload (Mechanic) "
            "Attack Catalog；近战加成取指定 Incarnon Genesis 页 EVO1；Ruvox 取原生灵化页 EVO1"
            "及 melee.lua（逐条附源码 SHA256），"
            "CC BY-NC-SA 3.0；中文名为 DE 官方简中（weapons_stats.json）",
            "wiki_snapshot": args.wiki.name,
            "generator": "scripts/build_incarnon_forms.py",
            "scope": "枪械灵化基础面板（不含可选进化 perk）+ 首批 Genesis 近战"
            f"（允许 {len(MELEE_GENESIS_PAGES)} 页，本次收录 {melee_count} 个明确变体；"
            "仅 EVO1 +100% Melee Damage，基伤 MOD 桶）；不自动扩展同族，"
            f"原生 Ruvox {native_count} 个（仅普通命中转换、独立攻速与范围；IPS MOD/重击/震地/"
            f"虚空尖刺/进化攻速未覆盖）；本次共 {len(recs)} 个形态；"
            "不含 Innodem/Praedos/Thalys/其它特殊攻击/激活时间/持续时间",
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
