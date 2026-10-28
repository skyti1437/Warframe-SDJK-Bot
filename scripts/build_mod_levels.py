#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从 DE 官方逐级原文重建 mods_stats.json，保留未能重建的历史兼容字段。

取代 scripts/sync_mods_from_wfsim.py；仅使用标准库和 _lua_table，不联网。
来源：DE ExportUpgrades_en.json（ExportUpgrades / ExportAvionics）；英文 wiki
Module:Mods/data 用于按 InternalName 定位；PEP 513105d（MIT）
用于核验资产身份。没有 wiki 单页明确精确值时采用 DE 显示值，不插值。
names 是历史识别名表，只移除三项专精节点，不宣称重新核验。

    python scripts/build_mod_levels.py --official DIR --pep DIR --wiki DIR [--check]

产物自身保存需要兼容的旧字段，不另存整份旧数据库；每次从本轮实际重建路径重新
计算 _legacy_fields，并沿用旧字段的历史出处，重复生成不会将历史值洗成已验证值。
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lua_table import loads  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "core/data/mods_stats.json"
FOCUS = {"guardian break", "squad regen", "lethal levitation"}
POLARITY = {
    "AP_ATTACK": "madurai",
    "AP_DEFENSE": "vazarin",
    "AP_TACTIC": "naramon",
    "AP_POWER": "zenurik",
    "AP_PRECEPT": "penjaga",
    "AP_WARD": "unairu",
    "AP_UMBRA": "umbra",
    "AP_UNIVERSAL": "universal",
    "AP_ANY": "any",
}
FIELDS = {
    "damage": "base_dmg",
    "melee damage": "base_dmg",
    "critical chance": "crit_chance",
    "critical damage": "crit_dmg",
    "status chance": "status_chance",
    "status damage": "status_dmg",
    "fire rate": "fire_rate",
    "fire rate (x2 for bows)": "fire_rate",
    "attack speed": "fire_rate",
    "melee attack speed": "fire_rate",
    "multishot": "multishot",
    "reload speed": "reload_pct",
    "magazine capacity": "magazine_pct",
    "status duration": "status_duration",
    "heavy attack wind up speed": "heavy_windup",
    "to headshot multiplier": "headshot_bonus",
    "melee damage on heavy attack": "heavy_dmg",
    "combo duration": "combo_duration",
}
ELEMENTS = {
    "heat",
    "cold",
    "electricity",
    "toxin",
    "radiation",
    "magnetic",
    "viral",
    "corrosive",
    "gas",
    "blast",
}
PHYSICAL = {"impact", "puncture", "slash"}
FACTIONS = {
    "Corpus": "Corpus",
    "Grineer": "Grineer",
    "Infested": "Infested",
    "Orokin": "Orokin",
    "Murmur": "The Murmur",
    "Sentients": "Sentient",  # 牺牲 系（DE「x1.33 Damage to Sentients」）
}
LEGACY_SRC = "历史兼容值（本轮 DE 原文未能重建；未重新核验）"
# 与 DE 原文矛盾的历史字段：不保留（2026-10-27 Astra v1.2.7 复测 P1 + Claude 逐张对 DE）。键 = 卡名小写，
# 值 = {字段: DE 依据}。字段在逐级与满级 effects 里一并剔除。
LEGACY_DROP: dict[str, dict[str, str]] = {
    "rifle elementalist": {
        "base_dmg": "DE 满级只有「+90% Status Damage」「+0.6 Punch Through」，无基伤"
    },
    "shotgun elementalist": {
        "base_dmg": "DE 满级只有「+90% Status Damage」「+60% Magazine Capacity」，无基伤"
    },
    "amalgam furax body count": {
        "fire_rate": "DE「+45% Fire Rate for Secondary Weapons」—— 近战卡加的是副武器射速，不是本武器"
    },
    "stinging truth": {
        "magazine_pct": "DE「+40 Magazine Capacity」是绝对 40 发，不是 +40%（计算器无绝对弹匣键）"
    },
}


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def flatten(obj: dict, prefix: str = "") -> dict:
    result = {}
    for k, v in obj.items():
        path = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            result.update(flatten(v, path))
        else:
            result[path] = v
    return result


def set_path(obj: dict, path: str, value) -> None:
    keys = path.split(".")
    for key in keys[:-1]:
        obj = obj.setdefault(key, {})
    obj[keys[-1]] = value


def clean(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text).replace("\\r\\n", "\n").replace("\r\n", "\n").strip()


def parse(text: str) -> dict | None:
    """只认完整独立效果行；组合/异常条件保留已有专用计算字段。"""
    s = clean(text)
    for regex, field in (
        (r"\+([\d.]+)% Critical Chance stacks with Combo Multiplier", "crit_per_combo"),
        (r"\+([\d.]+)% Status Chance per Combo Multiplier", "status_per_combo"),
        (r"\+([\d.]+)% Melee Damage per Status Type affecting the target", "dmg_per_status"),
    ):
        if m := re.fullmatch(regex, s, re.I):
            return {field: float(m[1])}
    if m := re.fullmatch(
        r"On Consecutive throw \(Max stacks (\d+)\):\n\+([\d.]+)% Throw Damage", s, re.I
    ):
        return {"throw_max_stacks": int(m[1]), "throw_dmg": float(m[2])}
    if m := re.fullmatch(r"([+-]?[\d.]+)%\s+(.+)", s):
        value, label = float(m[1]), m[2].lower()
        if label in FIELDS:
            return {FIELDS[label]: value}
        if label == "critical chance (x2 for heavy attacks)":
            return {"crit_chance": value, "heavy_crit_mult": 2.0}
        if label in ELEMENTS | PHYSICAL:
            return {"physical" if label in PHYSICAL else "elements": {label: value}}
        if label.startswith("damage to ") and m[2][10:] in FACTIONS:
            return {"faction_dmg": value, "faction_of": FACTIONS[m[2][10:]]}
    if m := re.fullmatch(
        r"x([\d.]+) Damage to (Corpus|Grineer|Infested|Orokin|Murmur|Sentients)", s
    ):
        # faction_dmg = 同一乘数的百分数写法（计算器两键都读），由 DE 乘数换算，不是另一个来源
        mul = float(m[1])
        return {
            "faction_mul": mul,
            "faction_of": FACTIONS[m[2]],
            "faction_dmg": round((mul - 1) * 100, 6),
        }
    if m := re.fullmatch(r"([\d.]+)% of Damage converted into (Impact|Puncture|Slash)", s):
        return {"physical_convert": {m[2].lower(): float(m[1])}}
    if m := re.fullmatch(r"\+([\d.]+)(s)? (Punch Through|Initial Combo|Combo Duration)", s):
        return {
            {
                "Punch Through": "punch_through",
                "Initial Combo": "initial_combo",
                "Combo Duration": "combo_duration",
            }[m[3]]: float(m[1])
        }
    return None


def is_conditional(text: str) -> bool:
    return bool(
        re.search(
            r"\b(on|when|while|if)\b|stacks? up to|per Status Type|per Combo Multiplier|stacks with Combo|for every|per hit",
            clean(text),
            re.I,
        )
    )


def conditional(text: str, source: str) -> dict:
    """未映射条件保留原文；不从有多个子效果的句子猜层数、时长或数值。"""
    s = clean(text)
    rec = {
        "trigger": "unmapped",
        "grants": s,
        "field": None,
        "mappable": False,
        "value": None,
        "max_stacks": None,
        "duration": None,
        "condition_text": s,
        "max_stacks_status": "unknown_not_declared",
        "duration_status": "unknown_not_declared",
        "src": {"condition_text": source},
    }
    if m := re.fullmatch(r"ON CRITICAL HIT:\nReplenish ([\d.]+)% of Shields", s, re.I):
        rec.update(trigger="critical_hit", grants="shield_restore_percent", value=float(m[1]))
        rec["src"]["value"] = source
    elif m := re.fullmatch(r"\+([\d.]+)% Fire Rate when (Crouching|Airborne)", s, re.I):
        rec.update(
            trigger="while_" + m[2].lower(),
            grants="fire_rate_bonus",
            field="fire_rate",
            mappable=True,
            value=float(m[1]),
        )
        rec["src"]["value"] = source
    return rec


def load_wiki(path: Path) -> tuple[dict, dict]:
    raw = (path / "Module_Mods_data.wiki").read_text(encoding="utf-8")
    # 模块只有 local modData = 字面量表 + return modData，不执行 Lua。
    table = loads(raw[raw.index("{") : raw.rfind("return")])
    meta = json.loads((path / "Module_Mods_data.meta.json").read_text(encoding="utf-8"))
    return {norm(r.get("Name") or key): r for key, r in table["Mods"].items()}, meta


def official_index(data: dict) -> tuple[dict, dict]:
    by_path, by_name = {}, {}
    # ExportFocusUpgrades 不进入 MOD 数据域。
    for section in ("ExportUpgrades", "ExportAvionics"):
        for i, row in enumerate(data[section]):
            item = (row, f"ExportUpgrades_en.json {section}[{i}] ({row['uniqueName']})")
            by_path.setdefault(row["uniqueName"], []).append(item)
            by_name.setdefault(norm(row.get("name", "")), []).append(item)
    return by_path, by_name


def match(old: dict, wiki: dict, by_path: dict, by_name: dict) -> tuple | None:
    w = wiki.get(norm(old["name"]), {})
    candidates = by_path.get(w.get("InternalName"), []) or by_name.get(norm(old["name"]), [])
    if len(candidates) > 1:
        ordinary = [
            r
            for r in candidates
            if not re.search(r"/(Beginner|Intermediate|Expert|Flawed|Damaged)/", r[0]["uniqueName"])
        ]
        if len(ordinary) == 1:
            candidates = ordinary
    if not candidates:
        return None
    sigs = {json.dumps(r[0], sort_keys=True) for r in candidates}
    return candidates[0] if len(sigs) == 1 else None


def build(official: Path, pep_dir: Path, wiki_dir: Path, old: dict) -> dict:
    op = official / "ExportUpgrades_en.json" if official.is_dir() else official
    official_data = json.loads(op.read_text(encoding="utf-8"))
    pep = json.loads((pep_dir / "ExportUpgrades.json").read_text(encoding="utf-8"))
    wiki, wiki_meta = load_wiki(wiki_dir)
    by_path, by_name = official_index(official_data)
    result = copy.deepcopy(old)
    result["mods"] = {}
    result["names"] = {
        k: v
        for k, v in old["names"].items()
        if k.lower() not in FOCUS and str(v.get("name", "")).lower() not in FOCUS
    }
    counts = Counter()
    for key, prior in old["mods"].items():
        if key.lower() in FOCUS or prior["name"].lower() in FOCUS:
            continue
        rec = copy.deepcopy(prior)
        src, legacy = {}, []
        item = match(prior, wiki, by_path, by_name)
        for field in ("name", "zh", "_variant"):
            src[field] = "历史名称/变体兼容标记（沿用，未重新核验）"
        if not item:
            for field in (
                "compat",
                "polarity",
                "rarity",
                "drain",
                "base_drain",
                "drain_at_max",
                "max_rank",
            ):
                if field in rec:
                    src[field] = LEGACY_SRC
            legacy.extend(flatten(rec.get("effects") or {}, "effects"))
            for rank, level in enumerate(rec.get("levels") or []):
                legacy.extend(flatten(level, f"levels[{rank}]"))
            counts["unmatched"] += 1
            for field in ("zh", "compat", "polarity", "rarity"):
                counts[field + "_retained"] += 1
        else:
            row, source = item
            counts[
                "pep_identity_present" if row["uniqueName"] in pep else "pep_identity_absent"
            ] += 1
            counts["matched"] += 1
            rec["name"] = row["name"]
            src["name"] = source + ".name"
            for field, dekey in (
                ("base_drain", "baseDrain"),
                ("drain", "baseDrain"),
                ("max_rank", "fusionLimit"),
            ):
                rec[field] = row[dekey]
                src[field] = source + "." + dekey
            rec["drain_at_max"] = rec["base_drain"] + rec["max_rank"]
            src["drain_at_max"] = source + ".baseDrain + fusionLimit"
            for field, dekey in (
                ("zh", "zh"),
                ("polarity", "polarity"),
                ("rarity", "rarity"),
                ("compat", "compatName"),
            ):
                value = row.get(dekey)
                if value is None and field == "compat":
                    value = row.get("type")
                    if value is not None:
                        value = value.title()
                        dekey = "type"
                if value is not None:
                    if field == "polarity":
                        value = POLARITY[value]
                    if field == "rarity":
                        value = value.title()
                    rec[field] = value
                    src[field] = source + "." + dekey
                    counts[field + "_official"] += 1
                else:
                    src[field] = LEGACY_SRC
                    counts[field + "_retained"] += 1
            levels, level_sources, unparsed = [], [], []
            raw_levels = row.get("levelStats") or []
            if len(raw_levels) < rec["max_rank"] + 1:
                raise ValueError(f"DE 合法等级原文不足：{key}")
            for rank in range(rec["max_rank"] + 1):
                parsed, parsed_src, conds, conditional_texts = {}, {}, [], []
                stats = raw_levels[rank].get("stats") or []
                if key == "retribution":
                    # DE 把触发概率与绝对电伤拆成两行；不是常驻元素百分比。
                    texts = [clean(t) for t in stats]
                    if len(texts) != 2:
                        raise ValueError("Retribution 条件原文行数改变，需重新核验")
                    chance = re.fullmatch(
                        r"\+([\d.]+)% Chance to deal Electrical Damage when shield struck by melee enemies\.",
                        texts[0],
                    )
                    amount = re.fullmatch(r"\+([\d.]+) Electricity", texts[1])
                    if len(texts) != 2 or not chance or not amount:
                        raise ValueError("Retribution 条件原文格式改变，需重新核验")
                    csrc = f"{source}.levelStats[{rank}].stats"
                    cond = conditional("\n".join(texts), csrc)
                    cond.update(
                        trigger="when_shield_struck_by_melee",
                        grants="electricity_damage",
                        value=float(amount[1]),
                        chance_pct=float(chance[1]),
                        units="flat_damage",
                    )
                    cond["src"].update(value=csrc + "[1]", chance_pct=csrc + "[0]")
                    conds.append(cond)
                    conditional_texts.append("\n".join(stats).replace("\r\n", "\n"))
                    counts["conditional_lines"] += len(stats)
                for n, text in enumerate([] if key == "retribution" else stats):
                    path = f"{source}.levelStats[{rank}].stats[{n}]"
                    obj = parse(text)
                    if obj is not None:
                        for f, value in flatten(obj).items():
                            set_path(parsed, f, value)
                            parsed_src[f] = path
                        counts["parsed_lines"] += 1
                    elif is_conditional(text):
                        conds.append(conditional(text, path))
                        conditional_texts.append(text.replace("\r\n", "\n"))
                        counts["conditional_lines"] += 1
                    else:
                        unparsed.append({"rank": rank, "text": text, "src": path})
                        counts["unparsed_lines"] += 1
                old_levels = prior.get("levels") or []
                before = old_levels[rank] if rank < len(old_levels) else {}
                level = copy.deepcopy(before)
                for _f in LEGACY_DROP.get(key, {}):
                    level.pop(_f, None)
                # 两张已有常驻误分类的卡：DE 明确限定 when，不能继续保留常驻射速。
                if any(c["field"] == "fire_rate" for c in conds):
                    level.pop("fire_rate", None)
                if key == "retribution":
                    if isinstance(level.get("elements"), dict):
                        level["elements"].pop("electricity", None)
                        if not level["elements"]:
                            level.pop("elements")
                if key == "fortifying fire":
                    level = {}
                if conditional_texts and key != "fortifying fire":
                    parsed["conditional"] = conditional_texts
                    parsed_src["conditional"] = "; ".join(c["src"]["condition_text"] for c in conds)
                for f, value in flatten(parsed).items():
                    set_path(level, f, value)
                if conds and key in {
                    "fortifying fire",
                    "lie in wait",
                    "soaring strike",
                    "retribution",
                }:
                    level["conditionals"] = conds
                else:
                    level.pop("conditionals", None)
                for f in flatten(level):
                    full = f"levels[{rank}].{f}"
                    if f == "conditionals":
                        src[full] = "; ".join(c["src"]["condition_text"] for c in conds)
                    elif f in parsed_src:
                        src[full] = parsed_src[f]
                    else:
                        legacy.append(full)
                        src[full] = LEGACY_SRC
                levels.append(level)
                level_sources.append(parsed_src)
            rec["levels"] = levels
            # 满级 effects 与最后一个等级一致；保留仅 effects 有的旧字段并标历史。
            effects = copy.deepcopy(prior.get("effects") or {})
            for _f in LEGACY_DROP.get(key, {}):
                effects.pop(_f, None)
            if any(c.get("field") == "fire_rate" for c in levels[-1].get("conditionals", [])):
                effects.pop("fire_rate", None)
            if key == "retribution":
                if isinstance(effects.get("elements"), dict):
                    effects["elements"].pop("electricity", None)
                    if not effects["elements"]:
                        effects.pop("elements")
            if key == "fortifying fire":
                effects = {}
            for f, value in flatten(
                {k: v for k, v in levels[-1].items() if k != "conditionals"}
            ).items():
                set_path(effects, f, value)
            rec["effects"] = effects
            if levels[-1].get("conditionals"):
                rec["conditionals"] = copy.deepcopy(levels[-1]["conditionals"])
                src["conditionals"] = src[f"levels[{rec['max_rank']}].conditionals"]
            else:
                rec.pop("conditionals", None)
            for f in flatten(effects):
                if f in level_sources[-1]:
                    src["effects." + f] = level_sources[-1][f]
                else:
                    legacy.append("effects." + f)
                    src["effects." + f] = LEGACY_SRC
            rec["_unparsed_stats"] = unparsed
            rec["_source_unique_name"] = row["uniqueName"]
            if len(raw_levels) > rec["max_rank"] + 1:
                rec["_ignored_level_stats"] = len(raw_levels) - rec["max_rank"] - 1
            else:
                rec.pop("_ignored_level_stats", None)
        rec["_legacy_fields"] = sorted(legacy)
        for field in legacy:
            src.setdefault(field, LEGACY_SRC)
        rec["src"] = src
        result["mods"][key] = rec
    result["_meta"] = {
        "source": "DE 官方 ExportUpgrades_en.json ExportUpgrades / ExportAvionics；wiki Module:Mods/data 按 InternalName 定位；PEP ExportUpgrades 核验资产身份（MIT）",
        "generator": "scripts/build_mod_levels.py",
        "official_sha256": hashlib.sha256(op.read_bytes()).hexdigest(),
        "pep_sha256": hashlib.sha256((pep_dir / "ExportUpgrades.json").read_bytes()).hexdigest(),
        "pep_commit": "513105d5c4e56d2a3238b7c7914e8ef4e87e829a",
        "wiki_snapshot": "Module:Mods/data oldid "
        + str(wiki_meta.get("oldid") or wiki_meta.get("revision")),
        "wiki_sha256": hashlib.sha256(
            (wiki_dir / "Module_Mods_data.wiki").read_bytes()
        ).hexdigest(),
        "names_status": "沿用历史识别名表，仅移除 Guardian Break / Squad Regen / Lethal Levitation，未重新核验",
        "legacy_policy": "_legacy_fields 为本轮未由 DE 重建的兼容字段路径；值沿用，不代表已核验。每次按重建集合计算，避免重复生成洗掉标记。",
        "conditional_policy": "明确条件不作常驻；已有组合/异常/连投专用字段沿用计算协议。普通条件仅保留 DE 原文，结构化规则由 mods_wfsim_extra.json 提供；仅四张迁移卡在 conditionals / levels[N].conditionals 保存条件。null 表示未映射或未知。",
        "units": "百分数用百分点；派系 xN 用倍率；穿透米、初始连击与持续秒数保留原文单位；逐级读取，不插值",
        "counts": dict(sorted(counts.items())),
    }
    return result


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--official", type=Path, required=True)
    ap.add_argument("--pep", type=Path, required=True)
    ap.add_argument("--wiki", type=Path, required=True)
    ap.add_argument("--check", action="store_true", help="只比对，不写盘；不同则退出 1")
    args = ap.parse_args()
    data = build(args.official, args.pep, args.wiki, json.loads(OUT.read_text(encoding="utf-8")))
    text = json.dumps(data, ensure_ascii=False, indent=1) + "\n"
    if args.check:
        same = OUT.read_bytes() == text.encode("utf-8")
        print("mods_stats.json 与生成结果" + ("一致" if same else "不一致"))
        raise SystemExit(0 if same else 1)
    OUT.write_bytes(text.encode("utf-8"))
    print(
        f"写入 {OUT.relative_to(ROOT)}：{len(data['mods'])} 张 MOD / {len(data['names'])} 个识别名"
    )
    print(json.dumps(data["_meta"]["counts"], ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
