#!/usr/bin/env python3
"""用固定 DE / PEP / 英文 wiki 快照生成 MOD 直接互斥、条件和武器排除规则。

取代 enrich_mods_from_wfsim.py；只用标准库和 _lua_table，不联网。
用法：python scripts/build_mod_rules.py --official /path/to/de-export \
    --pep /path/to/pep-513105d --wiki /path/to/wiki/mods [--check]

协议基线冻结于 e3eaf2c：保存直接互斥范围、既有 family 标识、既有条件/排除
卡名与 grants_map 协议，避免把上次输出回读为本次输入。新数值只解析源文案；
唯一例外是维护者要求保留的 Primed Deadly Efficiency 未决原值，明确标 pending。
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import itertools
import json
import re
from collections import defaultdict
from pathlib import Path

import _lua_table

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "core/data/mods_wfsim_extra.json"
# 冻结的协议/扫描范围，不是游戏数值源。
BASELINE = json.loads(r"""{
 "scope": [
  "accelerated blast",
  "accelerated isotope",
  "adaptation",
  "aerial ace",
  "amalgam argonak metal auger",
  "amalgam barrel diffusion",
  "amalgam daikyu target acquired",
  "amalgam furax body count",
  "amalgam javlok magazine warp",
  "amalgam organ shatter",
  "amalgam ripkas true steel",
  "amalgam serration",
  "amalgam shotgun barrage",
  "amanata pressure",
  "amar's contempt",
  "ambush",
  "ammo stock",
  "anemic agility",
  "archgun ace",
  "argon scope",
  "astral slash",
  "atomic fallout",
  "auger strike",
  "augur pact",
  "augur seeker",
  "automatic trigger",
  "bane of corpus",
  "bane of grineer",
  "bane of infested",
  "bane of orokin",
  "bane of the murmur",
  "barrel diffusion",
  "berserker fury",
  "bhisaj-bal",
  "biotic rounds",
  "bite",
  "blade of truth",
  "bladed rounds",
  "blaze",
  "blazing steel",
  "bleeding edge",
  "blood rush",
  "blunderbuss",
  "bore",
  "boreal's contempt",
  "brain storm",
  "breach loader",
  "bright purity",
  "brute conditioning",
  "burdened magazine",
  "burning claws",
  "burning hate",
  "buzz kill",
  "calculated victory",
  "carnis mandible",
  "carnis stinger",
  "catalyzer link",
  "charged bullets",
  "charged shell",
  "chilling claws",
  "chilling grasp",
  "chilling reload",
  "cleanse corpus",
  "cleanse grineer",
  "cleanse infested",
  "cleanse orokin",
  "cleanse the murmur",
  "clip delegation",
  "collision force",
  "combat reload",
  "combo fury",
  "combo killer",
  "combustion rounds",
  "comet blast",
  "comet rounds",
  "concussion rounds",
  "condition overload",
  "conductive blade",
  "contagious bond",
  "contagious spread",
  "containment breach",
  "contamination casing",
  "continuous misery",
  "convulsion",
  "corrupt charge",
  "counterweight",
  "covert lethality",
  "crash course",
  "crash shot",
  "creeping bullseye",
  "critical deceleration",
  "critical delay",
  "critical meltdown",
  "cryo coating",
  "cryo rounds",
  "cull the weak",
  "cutting edge",
  "damzav-vati",
  "deadly efficiency",
  "deadly maneuvers",
  "deadly sequence",
  "deep freeze",
  "deft tempo",
  "depleted reload",
  "disabling conditioning",
  "dispatch overdrive",
  "disruptor",
  "dizzying rounds",
  "double tap",
  "draining gloom",
  "drifting contact",
  "dual rounds",
  "efficient beams",
  "electrified barrel",
  "electromagnetic shielding",
  "embedded catalyzer",
  "emergent aftermath",
  "eroding blight",
  "expel corpus",
  "expel grineer",
  "expel infested",
  "expel orokin",
  "expel the murmur",
  "fanged fusillade",
  "fast hands",
  "feathered arrows",
  "fever strike",
  "final act",
  "final tap",
  "fired up",
  "flak shot",
  "flame gland",
  "flechette",
  "focus energy",
  "focus radon",
  "fortifying fire",
  "frail momentum",
  "frigid blast",
  "frost jaw",
  "frostbite",
  "full capacity",
  "full contact",
  "furor",
  "fury",
  "galvanized acceleration",
  "galvanized aptitude",
  "galvanized chamber",
  "galvanized crosshairs",
  "galvanized diffusion",
  "galvanized elementalist",
  "galvanized hell",
  "galvanized reflex",
  "galvanized savvy",
  "galvanized scope",
  "galvanized shot",
  "galvanized steel",
  "gilded truth",
  "glacial edge",
  "gladiator might",
  "gladiator vice",
  "gleaming blight",
  "gorgon frenzy",
  "growing power",
  "guardian break",
  "guided ordnance",
  "gunslinger",
  "hammer shot",
  "heated charge",
  "heavy caliber",
  "heavy trauma",
  "hell's chamber",
  "hellfire",
  "higasa serration",
  "high voltage",
  "hollow point",
  "hollowed bullets",
  "holster amp",
  "hornet strike",
  "hunter track",
  "hydraulic barrel",
  "hydraulic chamber",
  "hydraulic crosshairs",
  "hydraulic gauge",
  "hypothermic shell",
  "ice storm",
  "immunity resistance",
  "impaler munitions",
  "impenetrable offense",
  "incendiary coat",
  "infected clip",
  "infectious injection",
  "ion infusion",
  "jagged edge",
  "jolt",
  "jugulus barbs",
  "jugulus spines",
  "justice blades",
  "kill switch",
  "killing blow",
  "laser sight",
  "lasting sting",
  "leaded gas",
  "lethal levitation",
  "lethal torrent",
  "lie in wait",
  "lingering torment",
  "loaded capacity",
  "loose chamber",
  "loose hatch",
  "loose magazine",
  "loyal companion",
  "magazine extension",
  "magazine warp",
  "magma chamber",
  "magnetic capacity",
  "magnetic might",
  "magnetic strafe",
  "magnetized core",
  "magnetized cycle",
  "magnum force",
  "maim",
  "malignant force",
  "mark of the beast",
  "martial fury",
  "maul",
  "maximum capacity",
  "measured burst",
  "mecha overdrive",
  "melee elementalist",
  "melee prowess",
  "merciless gunfight",
  "metal auger",
  "metamorphic magazine",
  "meteor crash",
  "meteor munitions",
  "modified munitions",
  "molten impact",
  "momentary pause",
  "nano-applicator",
  "narrow barrel",
  "nebula bore",
  "necramech pressure point",
  "netra invocation",
  "nira's contempt",
  "no return",
  "north wind",
  "odomedic",
  "organ shatter",
  "parallax scope",
  "pathogen rounds",
  "perpetual agony",
  "photon overcharge",
  "piercing caliber",
  "piercing hit",
  "pistol elementalist",
  "pistol gambit",
  "pistol pestilence",
  "plan b",
  "point blank",
  "point strike",
  "poisonous sting",
  "polar magazine",
  "power throw",
  "precision conditioning",
  "pressure point",
  "pressurized magazine",
  "primed ammo stock",
  "primed bane of corpus",
  "primed bane of grineer",
  "primed bane of infested",
  "primed bane of orokin",
  "primed blunderbuss",
  "primed charged shell",
  "primed chilling grasp",
  "primed cleanse corpus",
  "primed cleanse grineer",
  "primed cleanse infested",
  "primed cleanse orokin",
  "primed convulsion",
  "primed cryo rounds",
  "primed deadly efficiency",
  "primed dual rounds",
  "primed expel corpus",
  "primed expel grineer",
  "primed expel infested",
  "primed expel orokin",
  "primed fast hands",
  "primed fever strike",
  "primed fury",
  "primed heated charge",
  "primed heavy trauma",
  "primed magazine warp",
  "primed pistol gambit",
  "primed point blank",
  "primed pressure point",
  "primed quickdraw",
  "primed ravage",
  "primed rubedo-lined barrel",
  "primed shred",
  "primed slip magazine",
  "primed smite corpus",
  "primed smite grineer",
  "primed smite infested",
  "primed smite orokin",
  "primed tactical pump",
  "primed target cracker",
  "prize kill",
  "pummel",
  "quasar drill",
  "quick reload",
  "quickdraw",
  "quickening",
  "radiated reload",
  "radon claws",
  "ravage",
  "razor munitions",
  "razor shot",
  "razorwing blitz",
  "recover",
  "recuperate",
  "reflex draw",
  "reinforced bond",
  "relentless assault",
  "rending strike",
  "repeater clip",
  "retribution",
  "rifle aptitude",
  "rifle elementalist",
  "rime rounds",
  "ripper rounds",
  "ris invocation",
  "rolling guard",
  "rubedo-lined barrel",
  "rupture",
  "sabot rounds",
  "sacrificial pressure",
  "sacrificial steel",
  "sawtooth clip",
  "saxum spittle",
  "saxum thorax",
  "scattered justice",
  "scattering inferno",
  "scorch",
  "searing steel",
  "secondary wind",
  "seeker",
  "seeking force",
  "seeking fury",
  "seismic bond",
  "semi-pistol cannonade",
  "semi-rifle cannonade",
  "semi-shotgun cannonade",
  "sepsis claws",
  "serrated edges",
  "serrated rounds",
  "serration",
  "sharpened blade",
  "sharpened bullets",
  "sharpshooter",
  "shattering justice",
  "shell shock",
  "shivering contagion",
  "shock collar",
  "shocking claws",
  "shocking touch",
  "shotgun barrage",
  "shotgun elementalist",
  "shotgun savvy",
  "shrapnel rounds",
  "shrapnel shot",
  "shred",
  "shred shot",
  "shredder",
  "skull shots",
  "slip magazine",
  "smite corpus",
  "smite grineer",
  "smite infested",
  "smite orokin",
  "smite the murmur",
  "soaring strike",
  "soaring truth",
  "soft hands",
  "speed trigger",
  "split chamber",
  "split flights",
  "spoiled strike",
  "spring-loaded blade",
  "spring-loaded broadhead",
  "spring-loaded chamber",
  "squad regen",
  "stinging truth",
  "stockpiled blight",
  "stormbringer",
  "stunning speed",
  "sudden impact",
  "sudden justice",
  "sundering strike",
  "sure shot",
  "survival instinct",
  "sweeping serration",
  "synth reflex",
  "tactical pump",
  "tactical retreat",
  "tainted clip",
  "tainted mag",
  "tainted shell",
  "target acquired",
  "target cracker",
  "targeting subsystem",
  "tempered blade",
  "the relentless lost",
  "thermite rounds",
  "thundermiter",
  "toxic barrage",
  "toxic blight",
  "toxic sequence",
  "transfusion",
  "triple tap",
  "true punishment",
  "true steel",
  "twitch",
  "vanquished prey",
  "venom teeth",
  "venomous clip",
  "vicious frost",
  "vicious spread",
  "vigilante armaments",
  "vigilante fervor",
  "vigilante offense",
  "vigorous swap",
  "vile acceleration",
  "vile precision",
  "virulent scourge",
  "vital sense",
  "volcanic edge",
  "voltaic strike",
  "vome invocation",
  "weeping wounds",
  "wildfire",
  "xata invocation",
  "zodiac shred"
 ],
 "families": {
  "primed bane of infested": "bane_of_infested",
  "galvanized scope": "argon_scope",
  "gilded truth": "gilded_truth",
  "galvanized chamber": "split_chamber",
  "primed cryo rounds": "cryo_rounds",
  "serration": "serration",
  "double tap": "double_tap",
  "fast hands": "fast_hands",
  "bane of infested": "bane_of_infested",
  "bane of corpus": "bane_of_corpus",
  "primed magazine warp": "magazine_warp",
  "deadly sequence": "deadly_sequence",
  "amalgam serration": "serration",
  "primed bane of orokin": "bane_of_orokin",
  "point strike": "point_strike",
  "bane of orokin": "bane_of_orokin",
  "shred": "shred",
  "bane of grineer": "bane_of_grineer",
  "split chamber": "split_chamber",
  "metamorphic magazine": "metamorphic_magazine",
  "magazine warp": "magazine_warp",
  "bane of the murmur": "bane_of_the_murmur",
  "higasa serration": "serration",
  "cryo rounds": "cryo_rounds",
  "primed shred": "shred",
  "primed bane of grineer": "bane_of_grineer",
  "primed fast hands": "fast_hands",
  "primed bane of corpus": "bane_of_corpus",
  "bhisaj-bal": "bhisaj_bal",
  "critical delay": "point_strike",
  "argon scope": "argon_scope",
  "galvanized aptitude": "rifle_aptitude",
  "rifle aptitude": "rifle_aptitude",
  "split flights": "split_chamber",
  "expel infested": "expel_infested",
  "expel orokin": "expel_orokin",
  "creeping bullseye": "pistol_gambit",
  "convulsion": "convulsion",
  "primed heated charge": "heated_charge",
  "barrel diffusion": "barrel_diffusion",
  "amalgam barrel diffusion": "barrel_diffusion",
  "primed convulsion": "convulsion",
  "dizzying rounds": "dizzying_rounds",
  "slip magazine": "slip_magazine",
  "expel corpus": "expel_corpus",
  "hydraulic crosshairs": "hydraulic_crosshairs",
  "primed expel corpus": "expel_corpus",
  "pistol gambit": "pistol_gambit",
  "primed expel orokin": "expel_orokin",
  "expel the murmur": "expel_the_murmur",
  "primed target cracker": "target_cracker",
  "quickdraw": "quickdraw",
  "galvanized shot": "sure_shot",
  "target cracker": "target_cracker",
  "primed expel infested": "expel_infested",
  "expel grineer": "expel_grineer",
  "leaded gas": "leaded_gas",
  "primed quickdraw": "quickdraw",
  "primed expel grineer": "expel_grineer",
  "primed pistol gambit": "pistol_gambit",
  "galvanized crosshairs": "hydraulic_crosshairs",
  "sure shot": "sure_shot",
  "stockpiled blight": "stockpiled_blight",
  "galvanized diffusion": "barrel_diffusion",
  "primed slip magazine": "slip_magazine",
  "damzav-vati": "damzav_vati",
  "heated charge": "heated_charge",
  "primed dual rounds": "dual_rounds",
  "primed deadly efficiency": "deadly_efficiency",
  "rubedo-lined barrel": "rubedo_lined_barrel",
  "primed rubedo-lined barrel": "rubedo_lined_barrel",
  "deadly efficiency": "deadly_efficiency",
  "dual rounds": "dual_rounds",
  "combustion rounds": "combustion_rounds",
  "venomous clip": "venomous_clip",
  "electrified barrel": "electrified_barrel",
  "polar magazine": "polar_magazine",
  "primed charged shell": "charged_shell",
  "primed cleanse orokin": "cleanse_orokin",
  "amalgam shotgun barrage": "shotgun_barrage",
  "primed cleanse grineer": "cleanse_grineer",
  "cleanse orokin": "cleanse_orokin",
  "galvanized hell": "hells_chamber",
  "shotgun savvy": "shotgun_savvy",
  "primed ammo stock": "ammo_stock",
  "cleanse grineer": "cleanse_grineer",
  "primed tactical pump": "tactical_pump",
  "point blank": "point_blank",
  "cleanse infested": "cleanse_infested",
  "charged shell": "charged_shell",
  "cleanse the murmur": "cleanse_the_murmur",
  "primed point blank": "point_blank",
  "shotgun barrage": "shotgun_barrage",
  "cleanse corpus": "cleanse_corpus",
  "galvanized savvy": "shotgun_savvy",
  "hell's chamber": "hells_chamber",
  "primed ravage": "ravage",
  "tactical pump": "tactical_pump",
  "primed cleanse corpus": "cleanse_corpus",
  "ravage": "ravage",
  "primed chilling grasp": "chilling_grasp",
  "chilling grasp": "chilling_grasp",
  "primed cleanse infested": "cleanse_infested",
  "ammo stock": "ammo_stock",
  "smite grineer": "smite_grineer",
  "melee elementalist": "melee_elementalist",
  "organ shatter": "organ_shatter",
  "primed fever strike": "fever_strike",
  "drifting contact": "combo_duration",
  "sundering strike": "sundering_strike",
  "sacrificial pressure": "pressure_point",
  "smite the murmur": "smite_the_murmur",
  "fever strike": "fever_strike",
  "smite orokin": "smite_corrupted",
  "primed smite corpus": "smite_corpus",
  "galvanized reflex": "reflex_coil",
  "melee prowess": "melee_prowess",
  "berserker fury": "fury",
  "smite infested": "smite_infested",
  "pressure point": "pressure_point",
  "smite corpus": "smite_corpus",
  "galvanized elementalist": "melee_elementalist",
  "amalgam organ shatter": "organ_shatter",
  "primed smite grineer": "smite_grineer",
  "primed smite orokin": "smite_corrupted",
  "primed heavy trauma": "heavy_trauma",
  "galvanized steel": "true_steel",
  "true steel": "true_steel",
  "primed smite infested": "smite_infested",
  "heavy trauma": "heavy_trauma",
  "primed fury": "fury",
  "jagged edge": "jagged_edge",
  "fury": "fury",
  "primed pressure point": "pressure_point",
  "sacrificial steel": "true_steel"
 },
 "conditionals": [
  "archgun ace",
  "argon scope",
  "berserker fury",
  "bladed rounds",
  "brain storm",
  "catalyzer link",
  "clip delegation",
  "deadly efficiency",
  "embedded catalyzer",
  "galvanized acceleration",
  "galvanized aptitude",
  "galvanized chamber",
  "galvanized crosshairs",
  "galvanized diffusion",
  "galvanized elementalist",
  "galvanized hell",
  "galvanized reflex",
  "galvanized savvy",
  "galvanized scope",
  "galvanized shot",
  "galvanized steel",
  "hydraulic crosshairs",
  "laser sight",
  "leaded gas",
  "nano-applicator",
  "narrow barrel",
  "pressurized magazine",
  "primed deadly efficiency",
  "reflex draw",
  "repeater clip",
  "sharpened bullets",
  "shrapnel shot",
  "skull shots",
  "soft hands",
  "split flights",
  "spring-loaded blade",
  "spring-loaded chamber",
  "targeting subsystem",
  "twitch"
 ],
 "excludes": [
  "aerial ace",
  "amalgam argonak metal auger",
  "amalgam barrel diffusion",
  "amalgam daikyu target acquired",
  "amalgam javlok magazine warp",
  "amalgam organ shatter",
  "amalgam serration",
  "amalgam shotgun barrage",
  "galvanized crosshairs",
  "semi-rifle cannonade",
  "split flights"
 ],
 "grants_map": {
  "condition_overload": "dmg_per_status",
  "multishot": "multishot",
  "crit_chance": "crit_chance",
  "crit_damage": "crit_dmg",
  "damage": "base_dmg",
  "base_damage": "base_dmg",
  "fire_rate": "fire_rate",
  "status_chance": "status_chance",
  "status_damage": "status_dmg",
  "punch_through": "punch_through"
 },
 "pending": [
  {
   "trigger": "reload_complete",
   "grants": "base_damage",
   "field": "base_dmg",
   "mappable": true,
   "value": 220.0,
   "max_stacks": 1,
   "duration": 16.5
  }
 ]
}""")


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_wiki(folder):
    path = folder / "Module_Mods_data.wiki"
    text = path.read_text(encoding="utf-8")
    lua = text.replace("local modData = {", "return {", 1).rsplit("return modData", 1)[0]
    mods = _lua_table.loads(lua)["Mods"]
    return {e["Name"].lower(): e for e in mods.values() if isinstance(e, dict) and e.get("Name")}


def wiki_source(folder, page="Module_Mods_data"):
    meta = read_json(folder / f"{page}.meta.json")
    oldid = meta.get("oldid") or meta.get("revid") or meta.get("revision_id")
    if not oldid:
        raise ValueError(f"wiki快照没有固定oldid：{page}")
    title = "Module:Mods/data" if page == "Module_Mods_data" else page.replace("_", " ")
    return f"wiki {title} oldid={oldid}"


def build_families(wiki, source):
    scope = set(BASELINE["scope"])
    pairs = {}
    adjacency = defaultdict(set)
    for name in sorted(scope & wiki.keys()):
        for other in wiki[name].get("Incompatible", []) or []:
            other = other.lower()
            if other in scope and other != name:
                pair = tuple(sorted((name, other)))
                pairs.setdefault(pair, f"{source} Mods[{wiki[name]['Name']!r}].Incompatible")
                adjacency[name].add(other)
                adjacency[other].add(name)
    result, seen = {}, set()
    for name in sorted(adjacency):
        if name in seen:
            continue
        group, pending = set(), [name]
        while pending:
            current = pending.pop()
            if current not in group:
                group.add(current)
                pending.extend(adjacency[current] - group)
        seen.update(group)
        # 分量仅用来寻找候选；逐对直接关系有一对缺失就失败，绝不作传递闭包。
        all_pairs = list(itertools.combinations(sorted(group), 2))
        if any(pair not in pairs for pair in all_pairs):
            raise ValueError(f"family不能安全表达非完全直接互斥关系：{sorted(group)}")
        labels = {BASELINE["families"][n] for n in group if n in BASELINE["families"]}
        if len(labels) > 1:
            raise ValueError(f"旧family标识冲突，待裁决：{sorted(group)}")
        family = next(iter(labels)) if labels else re.sub(r"[^a-z0-9]+", "_", min(group)).strip("_")
        evidence = "; ".join(pairs[pair] + " => " + " / ".join(pair) for pair in all_pairs)
        for member in sorted(group):
            result[member] = {
                "family": family,
                "incompatible_with": sorted(group - {member}),
                "src": {"family": evidence, "incompatible_with": evidence},
            }
    # 无范围内配对的旧标识只保留协议兼容，不声称存在未证明的互斥关系。
    for name, family in BASELINE["families"].items():
        if name not in result:
            if name not in wiki:
                raise ValueError(f"旧family成员不在wiki：{name}")
            result[name] = {
                "family": family,
                "incompatible_with": [],
                "family_status": "singleton_protocol_identifier",
                "src": {
                    "family": f"{source} Mods[{wiki[name]['Name']!r}].Name（扫描范围内单成员标识，无互斥断言）",
                    "incompatible_with": f"{source} Mods[{wiki[name]['Name']!r}].Incompatible × 固定计算卡范围",
                },
            }
    return result, len(pairs)


TRIGGERS = {
    "weak point hit": "weak_point_hit",
    "weak point kill": "weak_point_kill",
    "headshot": "headshot",
    "headshot kill": "headshot_kill",
    "kill": "kill",
    "melee kill": "melee_kill",
    "hit": "hit",
    "equip": "equip",
    "ability cast": "ability_cast",
    "reload": "reload_complete",
    "reload from empty": "reload_complete",
    "status effect": "status_applied",
    "critical hit": "critical_hit",
}
# 描述字段 → 旧 grants 协议，数值按原单位，不统一乘100。
GRANTS = {
    "critical chance": "crit_chance",
    "critical damage": "crit_damage",
    "direct damage per status type affecting the target": "condition_overload",
    "multishot": "multishot",
    "weapon recoil": "recoil",
    "accuracy": "accuracy",
    "status chance": "status_chance",
    "fire rate": "fire_rate",
    "fire/charge rate": "fire_rate",
    "attack speed": "fire_rate",
    "reload speed": "reload_speed",
    "projectile speed": "projectile_speed",
    "beam range": "beam_range_percent",
    "initial combo": "initial_combo",
    "range": "melee_range",
    "damage": "base_damage",
    "gas damage and status chance": "element_and_status_chance",
}
EFFECT = re.compile(
    r"([+-]\d+(?:\.\d+)?)(%?)\s+("
    + "|".join(re.escape(k) for k in sorted(GRANTS, key=len, reverse=True))
    + r")\b",
    re.I,
)


def condition_blocks(stats):
    # 每个 On ... 块独立解析时长/层数，禁止把子效果声明套给主效果。
    text = re.sub(r"<[^>]+>", "", "\n".join(stats)).replace("\r", "")
    matches = list(re.finditer(r"\bOn\s+([^:\n]+):\s*", text, re.I))
    for i, match in enumerate(matches):
        trigger_text = match.group(1).strip().lower()
        if trigger_text not in TRIGGERS:
            raise ValueError(f"未建模触发：{trigger_text}")
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        yield TRIGGERS[trigger_text], trigger_text, text[match.end() : end].strip()


def parse_conditionals(entry, source):
    stats = entry["levelStats"][entry["fusionLimit"]]["stats"]
    output = []
    for trigger, trigger_text, block in condition_blocks(stats):
        duration = re.search(r"\bfor\s+(\d+(?:\.\d+)?)s\b", block, re.I)
        stacks = re.search(r"(?:Stacks up to|Max)\s+(\d+)(?:x|\s+(?:times|stacks))", block, re.I)
        conditions = []
        if "when Aiming" in block:
            conditions.append("when Aiming")
        if trigger_text == "reload from empty":
            conditions.append("from empty")
        if "per shot landed with current Magazine" in block:
            conditions.append("per shot landed with current Magazine; applies to next Magazine")
        if "(Non-AOE Bows)" in block:
            conditions.append("Non-AOE Bows")
        effects = [
            (GRANTS[m.group(3).lower()], float(m.group(1)), m.group(0))
            for m in EFFECT.finditer(block)
        ]
        # 同一源文案把Gas Damage与Status Chance共用数值，分别保留语义。
        effects = [item for item in effects if item[0] != "element_and_status_chance"]
        gas = re.search(r"([+-]\d+(?:\.\d+)?)% Gas Damage and Status Chance", block)
        if gas:
            effects.extend(
                (g, float(gas.group(1)), gas.group(0)) for g in ("gas_damage", "status_chance")
            )
        chance = re.search(r"(\d+(?:\.\d+)?)% chance for Infinite Ammo", block, re.I)
        if chance:
            effects.append(("ammo_efficiency", None, chance.group(0)))
        # 文案将两个加成共用一个15%；不从旧表取值。
        shared = re.search(r"Status Chance and Multishot increased by (\d+(?:\.\d+)?)%", block)
        if shared:
            effects.extend(
                (g, float(shared.group(1)), shared.group(0)) for g in ("status_chance", "multishot")
            )
        shield = re.search(r"Replenish (\d+(?:\.\d+)?)% of Shields", block, re.I)
        if shield:
            effects.append(("shield_restore_percent", float(shield.group(1)), shield.group(0)))
        if not effects:
            raise ValueError(f"未解析条件效果：{source}: {block}")
        for grant, value, raw in effects:
            field = BASELINE["grants_map"].get(grant)
            item = {
                "trigger": trigger,
                "grants": grant,
                "field": field,
                "mappable": bool(field),
                "value": value,
                "max_stacks": int(stacks.group(1)) if stacks else None,
                "duration": float(duration.group(1)) if duration else None,
                "max_stacks_status": "source_explicit" if stacks else "unknown_not_declared",
                "duration_status": "source_explicit" if duration else "unknown_not_declared",
                "raw": raw,
                "trigger_block": block,
                "condition_text": "; ".join(conditions) if conditions else None,
                "condition_model_status": "unmodeled_condition_text"
                if conditions
                else "no_additional_condition_text",
                "src": {
                    k: source
                    for k in (
                        "trigger",
                        "grants",
                        "value",
                        "max_stacks",
                        "duration",
                        "condition_text",
                    )
                },
            }
            if chance:
                item["chance"] = float(chance.group(1))
                item["value_status"] = "unknown_not_declared_infinite_ammo_not_numeric_efficiency"
                item["src"]["chance"] = source
            output.append(item)
    if not output:
        raise ValueError(f"没有条件块：{source}")
    return output


def parse_scanned_conditionals(entry, source):
    """仅接完整效果句；未知作用对象不降格为自增。"""
    text = re.sub(
        r"<[^>]+>", "", "\n".join(entry["levelStats"][entry["fusionLimit"]]["stats"])
    ).replace("\r", "")
    matches = list(re.finditer(r"\bOn\s+([^:\n]+):\s*", text, re.I))
    output, audit = [], []
    for i, match in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        trigger_text = match.group(1).strip()
        block = text[match.end() : end].strip()
        trigger = TRIGGERS.get(trigger_text.lower())
        if not trigger and re.fullmatch(r"\d+ Hits within \d+(?:\.\d+)?s", trigger_text, re.I):
            trigger = "hits_within_window"
        reason = None
        if not trigger:
            reason = "unsupported_trigger"
        elif re.search(r"Secondary Weapon|Squad|owner|Throw Damage|Headshot Critical", block, re.I):
            reason = "unsupported_effect_target_or_damage_subset"
        effect = re.fullmatch(
            r"([+-]\d+(?:\.\d+)?)% (Damage|Fire Rate|Critical Chance|Critical Damage|Status Chance|Multishot)(?: for (\d+(?:\.\d+)?)s)?",
            block,
            re.I,
        )
        double = re.fullmatch(
            r"(\d+(?:\.\d+)?)% Bonus Damage on next Shot for (\d+(?:\.\d+)?)s\.\nStacks up to (\d+)x outside of Conclave\.",
            block,
            re.I,
        )
        if not reason and not (effect or double):
            reason = "unsupported_effect_sentence_or_unmapped_field"
        if reason:
            audit.append(
                {
                    "status": "skipped",
                    "reason": reason,
                    "raw": match.group(0) + block,
                    "src": source,
                }
            )
            continue
        if effect:
            grant, value, duration, stacks = (
                GRANTS[effect[2].lower()],
                float(effect[1]),
                float(effect[3]) if effect[3] else None,
                None,
            )
        elif double:
            grant, value, duration, stacks = (
                "base_damage",
                float(double[1]),
                float(double[2]),
                int(double[3]),
            )
        output.append(
            {
                "trigger": trigger,
                "grants": grant,
                "field": BASELINE["grants_map"][grant],
                "mappable": True,
                "value": value,
                "max_stacks": stacks,
                "duration": duration,
                "max_stacks_status": "source_explicit"
                if stacks is not None
                else "unknown_not_declared",
                "duration_status": "source_explicit"
                if duration is not None
                else "unknown_not_declared",
                "raw": (effect or double).group(0),
                "trigger_block": block,
                "condition_text": match.group(0).strip() + " " + block,
                "condition_model_status": "unmodeled_condition_text",
                "src": {
                    k: source
                    for k in (
                        "trigger",
                        "grants",
                        "value",
                        "max_stacks",
                        "duration",
                        "condition_text",
                    )
                },
            }
        )
    if not matches and re.search(r"\b(when|while|after|per|on)\b", text, re.I):
        audit.append(
            {
                "status": "skipped",
                "reason": "unsupported_non_on_block_condition",
                "raw": text,
                "src": source,
            }
        )
    return output, audit


def generate(official, pep, wiki_dir):
    official_file = official if official.is_file() else official / "ExportUpgrades_en.json"
    official_dir = official_file.parent
    wiki = load_wiki(wiki_dir)
    wiki_src = wiki_source(wiki_dir)
    de = read_json(official_file)
    de_index = {}
    for section in ("ExportUpgrades", "ExportAvionics"):
        for i, entry in enumerate(de[section]):
            de_index[entry["uniqueName"]] = (entry, section, i)
    mods_pep = read_json(pep / "ExportUpgrades.json")
    weapons_pep = read_json(pep / "ExportWeapons.json")
    weapon_names_file = official_dir / "ExportWeapons_en.json"
    weapon_names = {
        e["uniqueName"]: e["name"].lower() for e in read_json(weapon_names_file)["ExportWeapons"]
    }
    result, pairs = build_families(wiki, wiki_src)
    for name in BASELINE["conditionals"] + ["fortifying fire"]:
        rule = result.setdefault(name, {"src": {}})
        item = wiki.get(name)
        uid = item.get("InternalName") if item else None
        if name == "primed deadly efficiency":
            # 特批：数值原样保留；两来源冲突以及未声明层数不作新事实认证。
            rule["conditionals"] = copy.deepcopy(BASELINE["pending"])
            rule["status"] = "pending"
            rule["pending_reason"] = (
                "wiki 16.5s / DE 17s 未裁决，且 InternalName 不同；依用户指令保留旧值"
            )
            rule["src"]["conditionals"] = (
                f"{wiki_src} Mods['Primed Deadly Efficiency'].Description 与 DE ExportUpgrades uniqueName=/Lotus/Upgrades/Mods/Archwing/Rifle/PrimedArchwingDamageOnReloadMod levelStats[10].stats 冲突（保留旧值，非确认）"
            )
            for cond in rule["conditionals"]:
                cond["status"] = "pending"
                cond["src"] = {
                    "value": f"{wiki_src} Mods['Primed Deadly Efficiency'].Description",
                    "duration": rule["src"]["conditionals"],
                    "max_stacks": "DE ExportUpgrades_en.json ExportUpgrades uniqueName=/Lotus/Upgrades/Mods/Archwing/Rifle/PrimedArchwingDamageOnReloadMod levelStats[10].stats 未声明层数（依用户指令保留旧值1，非确认）",
                }
            continue
        if name == "fortifying fire":
            matches = [
                (e, "ExportAvionics", i)
                for i, e in enumerate(de["ExportAvionics"])
                if e.get("name", "").lower() == name
            ]
            if len(matches) != 1:
                raise ValueError("Fortifying Fire DE身份不唯一")
            entry, section, i = matches[0]
        else:
            if uid not in de_index:
                raise ValueError(f"wiki InternalName未精确匹配DE：{name}: {uid}")
            entry, section, i = de_index[uid]
        source = f"DE ExportUpgrades_en.json {section}[{i}] uniqueName={entry['uniqueName']} levelStats[{entry['fusionLimit']}].stats"
        rule["conditionals"] = parse_conditionals(entry, source)
        rule["src"]["conditionals"] = source
    # 只从计算卡表取扫描范围；条件数值直接读DE，不读旧extra产物。
    scope_file = ROOT / "core/data/mods_stats.json"
    scan_scope = sorted(read_json(scope_file)["mods"])
    legacy = set(BASELINE["conditionals"] + ["fortifying fire"])
    audit = []
    for name in scan_scope:
        if name in legacy:
            continue
        item = wiki.get(name)
        uid = item.get("InternalName") if item else None
        if uid not in de_index:
            audit.append(
                {"name": name, "status": "skipped", "reason": "no_exact_wiki_internal_name_in_de"}
            )
            continue
        entry, section, i = de_index[uid]
        source = f"DE ExportUpgrades_en.json {section}[{i}] uniqueName={entry['uniqueName']} levelStats[{entry['fusionLimit']}].stats"
        conds, skipped = parse_scanned_conditionals(entry, source)
        audit.extend({"name": name, **row} for row in skipped)
        if conds:
            rule = result.setdefault(name, {"src": {}})
            rule["conditionals"] = conds
            rule["src"]["conditionals"] = source
    for name in BASELINE["excludes"]:
        rule = result.setdefault(name, {"src": {}})
        uid = wiki[name]["InternalName"]
        tags = sorted(mods_pep[uid].get("incompatibilityTags", []) or [])
        blocked = {}
        for weapon_uid, entry in weapons_pep.items():
            matches = sorted(set(tags) & set(entry.get("compatibilityTags", []) or []))
            if matches and weapon_uid in weapon_names:
                blocked[weapon_uid] = {"name": weapon_names[weapon_uid], "tags": matches}
        rule["excludes_tags"] = tags
        rule["excludes_weapons"] = sorted({e["name"] for e in blocked.values()})
        source = f"PEP commit=513105d5c4e56d2a3238b7c7914e8ef4e87e829a ExportUpgrades.json[{uid!r}].incompatibilityTags"
        rule["src"]["excludes_tags"] = source
        rule["src"]["excludes_weapons"] = (
            source
            + " × ExportWeapons.json[*].compatibilityTags，uniqueName精确匹配DE ExportWeapons_en.json[*].name"
        )
        rule["excluded_weapon_evidence"] = blocked
    meta = {
        "source": "DE Public Export + PEP 513105d + 英文wiki固定修订",
        "wiki_snapshot": str(read_json(wiki_dir / "Module_Mods_data.meta.json")["oldid"]),
        "updated": "2026-10-26",
        "baseline_commit": "e3eaf2cd49342ab5a7f85670344a5c8bf46a24c1",
        "grants_map": BASELINE["grants_map"],
        "input_sha256": {
            "DE ExportUpgrades_en.json": digest(official_file),
            "DE ExportWeapons_en.json": digest(weapon_names_file),
            "PEP ExportUpgrades.json": digest(pep / "ExportUpgrades.json"),
            "PEP ExportWeapons.json": digest(pep / "ExportWeapons.json"),
            "wiki Module:Mods/data": digest(wiki_dir / "Module_Mods_data.wiki"),
        },
        "family_direct_pairs": pairs,
        "conditional_scan_cards": len(scan_scope),
        "conditional_scan_scope_sha256": digest(scope_file),
        "conditional_scan_audit": audit,
        "scope_note": "直接互斥和排除保持原范围；条件扫描mods_stats全部计算卡，既有条件保留，仅新增DE明确数值且grants_map可映射的完整效果句。未支持语句见conditional_scan_audit；mappable仅指数值字段映射，触发及附加条件见condition_text。",
    }
    return {"_meta": meta, **{name: result[name] for name in sorted(result)}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--official", type=Path, required=True)
    parser.add_argument("--pep", type=Path, required=True)
    parser.add_argument("--wiki", type=Path, required=True)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    result = generate(args.official, args.pep, args.wiki)
    content = (json.dumps(result, ensure_ascii=False, indent=1) + "\n").encode()
    if args.check:
        same = OUT.exists() and OUT.read_bytes() == content
        print("[OK] MOD规则与固定输入一致" if same else "[DIFF] MOD规则与固定输入不一致")
        return 0 if same else 1
    OUT.write_bytes(content)
    families = {e["family"] for e in result.values() if "family" in e}
    conds = [c for e in result.values() for c in e.get("conditionals", [])]
    print(
        f"records={len(result) - 1} families={len(families)} members={sum('family' in e for e in result.values())} direct_pairs={result['_meta']['family_direct_pairs']} conditional_cards={sum('conditionals' in e for e in result.values())} effects={len(conds)} excluded_cards={sum('excludes_tags' in e for e in result.values())}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
