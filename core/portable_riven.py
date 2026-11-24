"""用已验收的工况锁定实卡，并复用标准靶求解结果。"""

import asyncio
import hashlib
import json
import math
import re
from functools import lru_cache
from itertools import permutations
from pathlib import Path
from threading import Lock

import httpx

from . import damage_calc as dc
from . import damage_engine, paths, riven_analysis, riven_rating
from .damage_calc import _load_evo_data, _weapon_family_key, find_enemy_exact, find_weapon_exact
from .formatters import dps_removal_columns
from .i18n import L, is_en
from .logging_compat import logger
from .parser import _STAT_LOOKUP
from .paths import atomic_write_text
from .request_queue import QueueUnavailable

# 上游的 weapon path 和 fireMode 与插件的武器 ID、玩法名各自独立。
FINGERPRINT = damage_engine.PORTABLE_FINGERPRINT
TARGET = dc.DEFAULT_ENEMY_PATH
BUNDLED_BUILDS = "portable_reference_builds.json"
BUNDLED_TRAITS = "portable_trait_matrix.json"
BUNDLED_BUILDS_OVERGUARD = "portable_reference_builds_overguard.json"
BUNDLED_TRAITS_OVERGUARD = "portable_trait_matrix_overguard.json"
BUNDLED_BUILDS_SHIELD = "portable_reference_builds_shield.json"
BUNDLED_TRAITS_SHIELD = "portable_trait_matrix_shield.json"
_BUNDLED_FILES = {
    "default": (BUNDLED_BUILDS, BUNDLED_TRAITS),
    "overguard": (BUNDLED_BUILDS_OVERGUARD, BUNDLED_TRAITS_OVERGUARD),
    "shield": (BUNDLED_BUILDS_SHIELD, BUNDLED_TRAITS_SHIELD),
}
_BUNDLED_TARGET_OPTIONS = {"overguard": "超宏", "shield": "护盾"}
REFERENCE_DISPOSITION = 1.2
MODES = {
    ("braton_prime", "base"): ("/Lotus/Weapons/Tenno/Rifle/BratonPrime", 0, 0.0),
    ("boar_prime", "incarnon"): ("/Lotus/Weapons/Tenno/Shotgun/PrimeBoar", 1, 0.0),
    ("latron_prime", "incarnon"): ("/Lotus/Weapons/Tenno/Rifle/LatronPrime", 1, 0.0),
    ("ocucor", "base"): (
        "/Lotus/Weapons/Corpus/Pistols/CrpSentExperimentPistol/CrpSentExperimentPistol",
        0,
        0.0,
    ),
    ("grimoire", "base"): ("/Lotus/Weapons/Tenno/Grimoire/TnGrimoire", 0, 0.0),
    ("burston_prime", "base"): (
        "/Lotus/Weapons/Tenno/LongGuns/PrimeBurston/PrimeBurston",
        0,
        0.5,
    ),
}
BUNDLED_BUILD_MODES = MODES | {
    ("amprex", "base"): (
        "/Lotus/Weapons/Corpus/LongGuns/ChainLightningGun/ChainLightningRifle",
        0,
        0.5,
    )
}
POLARITIES = {"madurai": "AP_ATTACK", "naramon": "AP_TACTIC", "vazarin": "AP_DEFENSE"}
# 引擎只接受三种实卡极性。未知时用受支持的值回放，展示和极化计划单独标记。
_RIVEN_POLARITY_PLACEHOLDER = POLARITIES["naramon"]
POLARITY_KEYS = {
    **{code: key for key, code in POLARITIES.items()},
    "AP_POWER": "zenurik",
    "AP_WARD": "unairu",
    "AP_PRECEPT": "penjaga",
    "AP_UMBRA": "umbra",
    "AP_ANY": "universal",
}
_NO_POLARITY = frozenset({None, "", "AP_UNIVERSAL", "AP_NONE", "NONE", "NULL"})
HEADSHOT_REQUIRED_WEAPONS = frozenset(
    {
        "/Lotus/Weapons/Tenno/Pistols/TnPriestPistolScope/TnPriestPistolWeapon",
        "/Lotus/Weapons/Tenno/Pistols/PrimeKnell/PrimeKnellWeapon",
        "/Lotus/Weapons/Infested/Pistols/InfVomitGun/InfVomitGunWep",
        "/Lotus/Weapons/Infested/Pistols/InfUzi/InfUziWeapon",
        "/Lotus/Weapons/Tenno/LongGuns/TnQuadSniper/TnQuadSniper",
        "/Lotus/Weapons/Tenno/LongGuns/PrimePerigale/PrimePerigaleSniper",
    }
)
RESULT_TTL_S = 600
LIVE_CACHE_ENTRY_BYTES = 512 * 1024
SOLVE_TIMEOUT_S = 600
SOLVE_QUEUE_TIMEOUT_S = 180
_TRAIT_MATRIX_ENDPOINT = "/api/trait-matrix"
_MELEE_MODE_KEYS = {"base": "0", "heavy": "heavy", "slam": "slam"}
_INCARNON_MODES = ("base", "incarnon", "cycle")
_TRAIT_FLAGS = {
    "spliced": riven_rating._TRAIT_SPLICED,
    "neutral": riven_rating._TRAIT_NEUTRAL,
    "weakpoint": riven_rating._TRAIT_WEAKPOINT,
    "reoptimized": riven_rating._TRAIT_REOPTIMIZED,
}
_LIVE_CACHE_WRITE_LOCK = Lock()
_HEAVY_CALIBER = "/Lotus/Upgrades/Mods/Rifle/DualStat/CorruptedDamageRecoilRifle"
_SERRATION = "/Lotus/Upgrades/Mods/Rifle/WeaponDamageAmountMod"

# 词条 ID 对应上游 RIVEN_RULES，效应、投影和资格由 /api/analyze 判定。
FUSION_CODES = {
    "gas": "GA",
    "corrosive": "CO",
    "viral": "VI",
    "radiation": "RA",
    "blast": "BL",
    "magnetic": "MA",
    "status_damage": "SD",
    "ammo_efficiency": "AE",
    "reload_while_holstered": "HR",
    "damage_to_orokin": "FO",
    "damage_to_techrot": "FT",
    "damage_to_scaldra": "FS",
    "weak_point_damage": "W",
    "weak_point_critical_chance": "Y",
}
GUN_CODES = {
    "critical_chance": "0",
    "critical_damage": "1",
    "status_chance": "2",
    "status_duration": "3",
    "heat": "4",
    "cold": "5",
    "toxin": "6",
    "electricity": "7",
    "impact": "8",
    "puncture": "9",
    "slash": "A",
    "damage_to_corpus": "C",
    "damage": "D",
    "reload_speed": "F",
    "damage_to_grineer": "G",
    "zoom": "H",
    "damage_to_infested": "I",
    "magazine_capacity": "L",
    "ammo_maximum": "M",
    "punch_through": "P",
    "fire_rate": "R",
    "multishot": "S",
    "projectile_speed": "V",
    "weapon_recoil": "Z",
    **FUSION_CODES,
}
MELEE_FUSION_CODES = {
    "heavy_attack_wind_up_speed": "HC",
    "melee_damage_on_heavy_attack": "HD",
    "slam_attack_damage": "SL",
    "parry_angle": "PA",
}
MELEE_CODES = {
    "attack_speed": "J",
    "melee_damage": "K",
    "range": "T",
    "finisher_damage": "X",
    "initial_combo": "B",
    "combo_duration": "N",
    "additional_combo_count_chance": "O",
    "chance_to_gain_combo_count": "Q",
    "critical_chance_for_slide_attack": "E",
    "heavy_attack_efficiency": "U",
    **MELEE_FUSION_CODES,
}
# 反查使用 typed_card 的 ID，保留 D 和 K 各自的语义。
TRAIT_IDS = {code: stat for stat, code in (GUN_CODES | MELEE_CODES).items()}
MELEE_ALIASES = {
    "extra_combo_chance": "additional_combo_count_chance",
    "combo_gain_chance": "chance_to_gain_combo_count",
    "slide_crit_chance": "critical_chance_for_slide_attack",
    "heavy_attack_wind_up": "heavy_attack_wind_up_speed",
    "heavy_attack_damage": "melee_damage_on_heavy_attack",
    "slam_damage": "slam_attack_damage",
}


class CoverageUnavailable(damage_engine.EngineBuildError):
    """便携引擎明确拒绝该武器的分析。"""


async def discover_weapons(host, port, choices):
    service = getattr(host, "damage_engine", None)
    if service is None:
        return {}
    try:
        await service.ensure_portable(port)
        catalog, _ = await damage_engine.portable_catalog(service, port)
    except (damage_engine.EngineError, QueueUnavailable, KeyError, TypeError, ValueError):
        return {}
    return _match_catalog(catalog, choices)


def _match_catalog(catalog, choices):
    found = {}
    for weapon in choices:
        stats = find_weapon_exact(weapon["name"]) or {}
        row = damage_engine.find_catalog_weapon(catalog, stats or weapon)
        if row:
            found[weapon["engine_id"]] = row
    return found


async def recommendation_weapons(host, port, query, choices):
    service = getattr(host, "damage_engine", None)
    if service is None:
        raise damage_engine.EngineUnreachable(
            L("独立引擎未运行或无法连接", "The standalone engine is offline or unreachable")
        )
    await service.ensure_portable(port)
    catalog, _ = await damage_engine.portable_catalog(service, port, include_unavailable=True)
    if choices:
        return choices, _match_catalog(catalog, choices)
    choices = riven_rating.match_portable_weapon(query, catalog)
    return choices, {weapon["engine_id"]: catalog[weapon["engine_id"]] for weapon in choices}


def _is_melee_weapon(weapon):
    return weapon.get("weaponType") in {"melee", "zaw"} or weapon.get("category") == "Melee"


def _has_unmodeled_melee_incarnon_tier1(weapon_meta):
    return _is_melee_weapon(weapon_meta) and any(
        option.get("unsupported") or option.get("complete") is False
        for tier in (weapon_meta.get("incarnon") or ())
        if tier.get("tier") == 1
        for option in (tier.get("options") or ())
    )


def _melee_incarnon_tier1_notes(weapon):
    if not _has_unmodeled_melee_incarnon_tier1(weapon):
        return []
    return [
        L(
            "※ 灵化近战提示：已计入进化 2–4，进化 1 灵化变身期间的临时加成上游尚未建模，未计入当前数值。",
            "※ Incarnon Melee note: Tier 2–4 evolutions are modeled; "
            "temporary Tier 1 Incarnon form bonuses are not yet modeled upstream.",
        )
    ]


def _mode_kind(row):
    for kind, form in (("incarnon", "incarnon"), ("alternate", "alt_fire"), ("base", "base")):
        if damage_engine._scan_mode_matches(row, form):
            return kind
    return "other"


def weapon_modes(weapon_id, catalog_weapon, mode, *, require_analysis=True):
    defaults = {label: trial for (wid, label), trial in MODES.items() if wid == weapon_id}
    fire_modes = {
        row["index"]: row
        for row in (catalog_weapon or {}).get("fireModes", ())
        if type(row.get("index")) is int and row["index"] >= 0
    }
    trials = {}
    for label, (path, index, hit) in defaults.items():
        if fire_modes:
            if index not in fire_modes or _mode_kind(fire_modes[index]) != label:
                continue
            path = catalog_weapon["path"]
        if index not in fire_modes or _searchable_mode(fire_modes[index], require_analysis):
            trials[path, index, hit] = label
    if fire_modes:
        path = catalog_weapon["path"]
        for index, row in fire_modes.items():
            if not _searchable_mode(row, require_analysis):
                continue
            label = _mode_kind(row)
            hit = defaults.get(label, (None, None, 0.0))[2]
            trials.setdefault((path, index, hit), label)
    return [(label, trial) for trial, label in trials.items() if mode is None or label == mode]


def _searchable_mode(row, require_analysis):
    return (
        row.get("available") is not False
        and row.get("buildAvailable") is not False
        and (not require_analysis or row.get("analysisAvailable") is not False)
    )


def _cycle_available(catalog_weapon):
    # 实时目录只标记灵化攻击。用同引擎版本的随包场景核对循环是否可用。
    document = _load_bundled(BUNDLED_BUILDS)
    entries = (document or {}).get("weapons", {}).get(catalog_weapon["path"])
    if entries is None:
        return True
    cycle = entries.get("cycle")
    return bool(cycle and not cycle.get("buildIssue"))


def recommendation_mode(weapon, catalog_weapon, mode, *, require_analysis=True, prefer_cycle=True):
    capability = "analysisAvailable" if require_analysis else "buildAvailable"
    if not catalog_weapon or any(
        catalog_weapon.get(key) is False for key in ("available", capability)
    ):
        raise CoverageUnavailable(
            L(
                "独立引擎暂不支持这把武器",
                "The standalone engine does not support this weapon yet",
            )
        )
    modes = weapon_modes(
        weapon["engine_id"], catalog_weapon, None, require_analysis=require_analysis
    )
    available = {label for label, _ in modes}
    melee = (
        weapon["category"] == "Melee"
        and (catalog_weapon.get("exalted") or {}).get("tier") != "cast"
    )
    if "incarnon" in available and (not prefer_cycle or melee or _cycle_available(catalog_weapon)):
        available.add("cycle")
        if prefer_cycle and mode is None and not melee:
            mode = "cycle"
    tags = set()
    if "base" in available and melee:
        available.update(("heavy", "slam"))
        tags = {str(tag).upper() for tag in catalog_weapon.get("compatTags") or ()}
        if mode is None:
            # 实时目录按上游默认路线所用的架势标签选择。
            if "GLAIVES_STANCE" in tags:
                mode = "heavy"
            elif "HAMMERS_STANCE" in tags:
                mode = "slam"
            else:
                mode = "base"
    scenario = {}
    selected = mode
    if mode in {"heavy", "slam"} and mode in available:
        selected = "base"
        if mode == "slam":
            route = "slamBullet" if "HAMMERS_STANCE" in tags else "slam"
        else:
            route = "glaive" if "GLAIVES_STANCE" in tags else "combo"
        scenario = {"heavyAttack": True, "combatState": {"meleeRoute": route}}
    elif mode == "cycle" and "cycle" in available:
        selected, scenario = "incarnon", {"incarnonCycle": True}
    if selected is None:
        modes.sort(key=lambda entry: (entry[0] != "incarnon", entry[0] != "base", entry[1][1]))
    else:
        modes = [entry for entry in modes if entry[0] == selected]
    if not modes:
        labels = L("、", ", ").join(
            L(*(riven_rating.MELEE_BASE_LABEL if melee and kind == "base" else names))
            for kind, names in riven_rating.MODE_LABELS.items()
            if kind in available
        ) or L("无", "None")
        raise CoverageUnavailable(
            L(
                f"该武器不支持所选模式。可用模式：{labels}",
                f"This weapon does not support the selected mode. Available modes: {labels}",
            )
        )
    label, scope = modes[0]
    if label == "base" and not scenario and melee:
        scenario["combatState"] = {"meleeRoute": "combo"}
    return mode or label, scope, scenario


def _fire_mode(weapon, index):
    for row in weapon.get("fireModes") or ():
        if row.get("index") == index:
            return row
    # 部分旧预计算只存武器路径和索引。
    incarnon = any(
        path == weapon.get("path") and fire == index and kind == "incarnon"
        for (_, kind), (path, fire, _) in MODES.items()
    )
    return {"index": index, "isIncarnonForm": incarnon}


def _mode_label(weapon, index, *, melee=False):
    row = _fire_mode(weapon, index)
    if (
        melee
        and damage_engine._scan_mode_matches(row, "base")
        and not any(row.get(key) for key in ("nameZh", "nameEn", "name"))
    ):
        return L(*riven_rating.MELEE_BASE_LABEL)
    label = damage_engine._scan_mode_label(row)
    name = L(
        row.get("nameZh") or row.get("name") or row.get("nameEn"),
        row.get("nameEn") or row.get("name"),
    )
    return f"{label}（{name}）" if name and name.casefold() != label.casefold() else label


def comparison_context(result, catalog_weapon, mode):
    target = result["evaluationTarget"]
    row = _fire_mode(catalog_weapon or result.get("weapon", {}), result["fireMode"])
    kind = mode
    if mode != "cycle" and (catalog_weapon or result.get("weapon", {}).get("fireModes")):
        kind = _mode_kind(row)
    # 同一武器的多个副射或其它形态也可能使用不同攻击。
    form = (
        (kind, row.get("nameEn") or row.get("name") or row["index"])
        if kind in {"alternate", "other"}
        else (kind,)
    )
    return (
        form,
        result["scoreMetric"],
        *(
            target[key]
            for key in (
                "enemyPath",
                "level",
                "steelPath",
                "headshotChance",
                "windowSeconds",
                "hitLocation",
            )
        ),
    )


def bundled_reference_target(*, variant="default"):
    spec = dc.target_option("标准靶")
    if variant != "default":
        spec.update(dc.target_option(_BUNDLED_TARGET_OPTIONS[variant]))
    target = damage_engine.portable_target_fields(TARGET, spec)
    if variant != "shield":
        # 标准靶本身无盾，沿用已生成场景的靶况字段。
        target["enemyEnhancements"].pop("shieldOverride", None)
    return target


@lru_cache(maxsize=6)
def _bundled_document(path, modified, size):
    # 原子替换文件后，下次查询会重新读取。随包表与实时缓存各自独立。
    del modified, size
    data = json.loads(path.read_bytes())
    variant = next((key for key, names in _BUNDLED_FILES.items() if path.name in names), None)
    if (
        variant is not None
        and type(data["schema"]) is int
        and data["schema"] == 1
        and data["engineFingerprint"] == FINGERPRINT
        and data["buildLabel"] == damage_engine.PORTABLE_DATA
        and data["target"] == bundled_reference_target(variant=variant)
        and isinstance(data["weapons"], dict)
    ):
        return data
    return None


def _load_bundled(name):
    path = paths.PKG_DATA / name
    try:
        info = path.stat()
        return _bundled_document(path, info.st_mtime_ns, info.st_size)
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _catalog_row(document, name, index):
    table = document["catalog"][name]
    return dict(zip(table["columns"], table["rows"][index], strict=True))


@lru_cache(maxsize=3)
def _bundled_weapons(path, modified, size):
    document = _bundled_document(path, modified, size)
    if document is None:
        return None, {}
    catalog = {}
    for weapon, modes in document["weapons"].items():
        index = next(iter(modes.values()))["weapon"]
        catalog[weapon] = {
            **_catalog_row(document, "weapons", index),
            "path": weapon,
            "fireModes": [row["mode"] for key, row in modes.items() if key.isdecimal()],
        }
    return document, catalog


def bundled_weapons(query, choices, target, options):
    """从随包目录解析武器，沿用指令的武器别名和变体规则。"""
    variant = bundled_variant(target, options)
    if not query or variant is None:
        return choices, None, {}
    name, _ = _BUNDLED_FILES[variant]
    path = paths.PKG_DATA / name
    try:
        info = path.stat()
        document, catalog = _bundled_weapons(path, info.st_mtime_ns, info.st_size)
        if not choices:
            choices = riven_rating.match_portable_weapon(query, catalog)
            return (
                choices,
                document,
                {weapon["engine_id"]: catalog[weapon["engine_id"]] for weapon in choices},
            )
        return choices, document, _match_catalog(catalog, choices)
    except (OSError, AttributeError, IndexError, StopIteration, KeyError, TypeError, ValueError):
        return choices, None, {}


def bundled_variant(target, options):
    if options:
        return None
    base = dc.target_option("标准靶")
    # 靶况档位先确定护盾与超宏，弱点率由各模式自己的预计算结果核对。
    target = {key: value for key, value in (target or {}).items() if key != "headshot"}
    base.pop("headshot")
    if not target or target == base:
        return "default"
    # 单独加开关时，指令只传覆盖字段。显式加「标准靶」则传完整预设。
    for variant, token in _BUNDLED_TARGET_OPTIONS.items():
        overrides = dc.target_option(token)
        if target == overrides or target == {**base, **overrides}:
            return variant
    return None


def standard_reference_query(target, options):
    return bundled_variant(target, options) is not None


def _bundled_melee_default_key(entries):
    # 随包模式记录了用于复现上游默认路线的动作类型。
    for key, route in (("slam", "slamBullet"), ("heavy", "glaive")):
        scenario = entries.get(key, {}).get("mode", {}).get("scenario")
        if riven_rating.melee_route(scenario) == route:
            return key
    return _MELEE_MODE_KEYS["base"]


def _bundled_modes(entries, mode, *, melee):
    if melee and (mode is None or mode in _MELEE_MODE_KEYS):
        key = _bundled_melee_default_key(entries) if mode is None else _MELEE_MODE_KEYS[mode]
        row = entries.get(key)
        return [(key, row)] if row and not row.get("buildIssue") else []

    available = [
        (key, row)
        for key, row in entries.items()
        if not row.get("buildIssue") and (mode is None or row["mode"]["kind"] == mode)
    ]
    order = (
        _INCARNON_MODES
        if any(row["mode"]["kind"] in {"incarnon", "cycle"} for _, row in available)
        else ()
    )
    if mode is None and order:
        available = sorted(
            ((key, row) for key, row in available if row["mode"]["kind"] in order),
            key=lambda entry: order.index(entry[1]["mode"]["kind"]),
        )
    return available


def _other_bundled_melee_modes(entries, selected):
    for mode, key in _MELEE_MODE_KEYS.items():
        if key != selected and (row := entries.get(key)) and not row.get("buildIssue"):
            yield mode, key, row


def _bundled_mode_hint(modes, *, melee=False, default_cycle=False):
    aliases = (
        {"base": ("平砍", "base"), "heavy": ("重击", "heavy"), "slam": ("震地", "slam")}
        if melee
        else {
            "base": ("主射", "base"),
            "incarnon": ("灵化形态", "incarnon"),
            "cycle": ("灵化", "cycle"),
        }
    )
    zh = "".join(f"「{aliases[mode][0]}」" for mode in modes)
    en = ", ".join(f'"{aliases[mode][1]}"' for mode in modes)
    if melee:
        return L(f"※ 可加{zh}切换路线。", f"※ Add {en} to switch routes.")
    if default_cycle:
        return L(
            f"※ 默认展示灵化循环，可加{zh}切换。",
            f"※ Showing Incarnon cycle by default. Add {en} to switch modes.",
        )
    return L(f"※ 可加{zh}切换模式。", f"※ Add {en} to switch modes.")


def live_cycle_hint(weapon, catalog, *, require_analysis=True):
    modes = weapon_modes(weapon["engine_id"], catalog, None, require_analysis=require_analysis)
    kinds = {label for label, _ in modes}
    alternatives = [kind for kind in ("base", "incarnon") if kind in kinds]
    return [_bundled_mode_hint(alternatives, default_cycle=True)] if alternatives else []


def _headshot_chance(weapon, target):
    return float(target.get("headshot", weapon in HEADSHOT_REQUIRED_WEAPONS or dc.DEFAULT_HEADSHOT))


def _bundled_headshot_chance(document, row):
    chance = (row or {}).get("mode", {}).get("headshotChance", document["target"]["headshotChance"])
    if not damage_engine._bounded_number(chance, high=1):
        raise ValueError("随包场景弱点率无效")
    return chance


def _bundled_target(document, row=None):
    fields = document["target"]
    return {
        "enemyPath": fields["enemy"],
        "enemyName": dc.DEFAULT_ENEMY,
        "headshotChance": _bundled_headshot_chance(document, row),
        "level": fields["level"],
        "steelPath": fields["steelPath"],
        "windowSeconds": fields["windowSeconds"],
        "enemyEnhancements": fields["enemyEnhancements"],
    }


def _bundled_entries(document, weapon, target):
    chance = _headshot_chance(weapon["path"], target or {})
    return {
        key: row
        for key, row in document["weapons"][weapon["path"]].items()
        if _bundled_headshot_chance(document, row) == chance
    }


async def loadout_references(target):
    if (variant := bundled_variant(target, {})) is None:
        return None
    name, _ = _BUNDLED_FILES[variant]
    return await asyncio.to_thread(_load_bundled, name)


def loadout_reference(document, mode, scope, target, options):
    """读取同一攻击、靶况及战斗条件下的无卡 DPS 和推荐赋能。"""
    if document is None or options.keys() & {"innateElement", "innateElementValue", "kitgunParts"}:
        return None
    path, index, _ = scope
    scenario = damage_engine.with_gauge_charging_headshots(
        {"scenario": options.get("scenario", {}), "headshotChance": _headshot_chance(path, target)}
    )["scenario"]
    if scenario.get("aiming") is False:
        scenario = {key: value for key, value in scenario.items() if key != "aiming"}
    try:
        key = mode if mode in {"heavy", "slam", "cycle"} else str(index)
        row = _bundled_entries(document, {"path": path}, target).get(key)
        if (
            not row
            or row.get("buildIssue")
            or row["mode"]["kind"] != mode
            or row["mode"]["index"] != index
            or row["mode"].get("scenario", {}) != scenario
            or row["capacity"][document["capacityColumns"].index("valid")] is not True
            or not damage_engine._bounded_number(row["full_dps"])
        ):
            return None
        arcanes = [
            {
                key: value
                for key, value in _catalog_row(document, "arcanes", i).items()
                if key in ("path", "rank", "stacks", "uptime") and value is not None
            }
            for i in row["arcanes"]
        ]
        exilus = (
            _catalog_row(document, "exilus", row["exilus"]) if row["exilus"] is not None else {}
        )
        same_equipment = (
            ("arcanes" not in options or options["arcanes"] == arcanes)
            and (
                options.get("exilus") in (None, {"mode": "auto"})
                or all(exilus.get(key) == value for key, value in options["exilus"].items())
            )
            and (
                "incarnonChoices" not in options
                or options["incarnonChoices"] == row["incarnonChoices"]
            )
        )
        return {"dps": row["full_dps"] if same_equipment else None, "arcanes": arcanes}
    except (IndexError, KeyError, TypeError, ValueError):
        return None


def bundled_build_lines(document, weapon, mode, *, target=None):
    if weapon is None:
        return None
    try:
        melee = weapon["weaponType"] in {"melee", "zaw"}
        entries = _bundled_entries(document, weapon, target)
        valid = document["capacityColumns"].index("valid")
        modes = [
            (key, row)
            for key, row in _bundled_modes(entries, mode, melee=melee)
            if row["capacity"][valid] is True
        ]
        if not modes:
            return None
        kinds = tuple(dict.fromkeys(row["mode"]["kind"] for _, row in modes))
        auto = mode is None and not melee and any(kind in {"incarnon", "cycle"} for kind in kinds)
        priority = {"cycle": 0, "incarnon": 1, "base": 2} if auto else {"base": 0}
        selected, row = min(
            modes,
            key=lambda entry: (
                priority.get(entry[1]["mode"]["kind"], len(priority)),
                entry[1]["mode"]["index"],
            ),
        )
        alternatives = tuple(kind for kind in kinds if auto and kind != row["mode"]["kind"])
        if melee:
            alternatives = tuple(
                kind
                for kind, _, other in _other_bundled_melee_modes(entries, selected)
                if other["capacity"][valid] is True
            )
        capacity = dict(zip(document["capacityColumns"], row["capacity"], strict=True))
        slots = []
        for mod in row["mods"]:
            if mod is None:
                slots.append(None)
                continue
            values = dict(zip(document["modColumns"], mod, strict=True))
            slots.append({**_catalog_row(document, "mods", values.pop("mod")), **values})
        if len(slots) != 8 or len(row["without"]) != 8:
            return None
        equipment = {
            name: _catalog_row(document, name, row[name]) if row[name] is not None else None
            for name in ("exilus", "stance")
        }
        equipment["arcanes"] = [_catalog_row(document, "arcanes", arc) for arc in row["arcanes"]]
        target = _bundled_target(document, row)
        replay = {
            "full": row["full_dps"],
            "without": {i: dps for i, dps in enumerate(row["without"]) if dps is not None},
            "stats": dict(zip(document["statsColumns"], row["stats"], strict=True)),
            "capacity": {
                "capacity": capacity["used"],
                "capacityLimit": capacity["limit"],
                "assignments": [
                    {"slot": i, "drain": mod["drain"]}
                    for i, mod in enumerate(slots)
                    if mod is not None
                ],
            },
            "equipment": equipment,
            "has_overguard": not target["enemyEnhancements"].get("noOverguard", True),
            "serration_slot": row["serrationSlot"],
        }
        result = {
            "weapon": {**weapon, "category": "Melee" if melee else ""},
            "fireMode": row["mode"]["index"],
            "evaluationScenario": row["mode"].get("scenario"),
            "evaluationTarget": target,
        }
        return [
            scenario_line(result),
            *_fixed_build_lines(
                target, slots, {"total": capacity["forma"]}, replay, (), melee=melee, bundled=True
            ),
            *evolution_lines(weapon, row["incarnonChoices"], reference=True),
            *(
                [
                    _bundled_mode_hint(
                        alternatives,
                        melee=melee,
                        default_cycle=auto and row["mode"]["kind"] == "cycle",
                    )
                ]
                if alternatives
                else []
            ),
        ]
    except (AttributeError, IndexError, KeyError, TypeError, ValueError):
        return None


def _bundled_trait_rows(document, scene, sign, roll):
    stats = document["stats"]
    flags = document["flags"]
    columns = document["columns"]
    stat_id = document["statColumns"].index("stat_id")
    suffix = roll[0] + "1"
    element_builds = document.get("element_builds", ())
    element_indices = scene.get("elem_builds", {}) if sign == "positive" else {}
    decoded = []
    for raw in scene[sign]:
        row = dict(zip(columns, raw, strict=True))
        unit = document["units"][row["unit"]]
        bits = row["flags"]
        element_index = element_indices.get(str(row["stat"]))
        if isinstance(element_index, list):
            index, tagged_roll = element_index
            element_index = index if tagged_roll == int(suffix) else None
        decoded.append(
            _trait_result(
                stats[row["stat"]][stat_id],
                row["shown" + suffix],
                unit,
                sum(flag for name, flag in _TRAIT_FLAGS.items() if bits & flags.get(name, 0)),
                row["pct" + suffix] if bits & flags["modeled"] else None,
                element_builds[element_index] if element_index is not None else None,
            )
        )
    return decoded


def _trait_result(stat, shown, unit, flags, pct, element_build):
    if stat.startswith("damage_vs_"):
        stat = stat.replace("damage_vs_", "damage_to_", 1)
    return riven_rating._TraitResult(stat, shown, unit, flags, None, pct, None, None, element_build)


def _bundled_trait_summary(result, scene, rows, roll, *, melee):
    ammo_stats = riven_rating._INCARNON_AMMO_STATS
    pure_form = scene.get("incarnon_form")
    positive = sorted(
        (
            row
            for row in rows["positive"]
            if row.elem_build is None
            and row.pct is not None
            and row.pct > 0
            and not (pure_form and row.stat in ammo_stats)
        ),
        key=lambda row: row.pct,
        reverse=True,
    )
    negative = sorted(
        (
            row
            for row in rows["negative"]
            if row.pct is not None
            and not row.stat.startswith("damage_to_")
            and row.stat not in riven_rating._CONTEXTUAL_MALUSES
            and not (pure_form and row.stat in ammo_stats)
            and (row.flags & riven_rating._TRAIT_NEUTRAL or abs(row.pct) < 3)
        ),
        key=lambda row: abs(row.pct),
    )
    dps, kpm = scene["baseline_dps"], scene["baseline_kpm"]
    lines = [
        L(f"◆ 单词条模拟推荐 · {roll}", f"◆ Single-trait simulation · {roll}"),
        "· " + scenario_line(result).removeprefix("◆ "),
        L(
            f"· 参考 DPS {dps:,.0f} · KPM {kpm:,.2f}",
            f"· Reference DPS {dps:,.0f} · KPM {kpm:,.2f}",
        ),
    ]
    weakpoint_note = L("⟦n⟧（100%弱点）⟦/n⟧", " ⟦n⟧(100% weakpoint)⟦/n⟧")
    charge_note = L("⟦v⟧仅充能期⟦/v⟧", "⟦v⟧Charge only⟦/v⟧")
    for label, picks, color in (
        (
            L("可洗正词条", "Rollable bonuses"),
            [row for row in positive if not row.flags & riven_rating._TRAIT_SPLICED],
            "y",
        ),
        (
            L("融合选项", "Fusion options"),
            [row for row in positive if row.flags & riven_rating._TRAIT_SPLICED],
            "v",
        ),
        (L("低影响负词条", "Low-impact maluses"), negative, "n"),
    ):
        values = []
        for row in picks[:3]:
            value = f"{riven_rating.stat_label(row.stat, melee)} ⟦{color}⟧{row.pct:+.1f}%⟦/{color}⟧"
            if row.flags & riven_rating._TRAIT_WEAKPOINT:
                value += weakpoint_note
            if scene.get("incarnon_cycle") and row.stat in ammo_stats:
                value += " " + charge_note
            values.append(value)
        if values:
            lines.append(f"· ⟦w⟧{label}⟦/w⟧  " + " / ".join(values))
    if scene.get("incarnon_cycle") and not any(charge_note in line for line in lines):
        lines.append(
            L(
                "· 弹匣、装填、弹药效率及弹药上限 ",
                "· Magazine, reload, ammo efficiency and reserve: ",
            )
            + charge_note
        )
    return lines


def _trait_condition_lines(result):
    options = result.get("confirmedAnalysisOptions") or {}
    if (result.get("evaluationScenario") or {}).get("heavyAttack"):
        scenario = dict(options.get("scenario") or {})
        scenario.pop("heavyAttack", None)
        options = {**options, "scenario": scenario}
    return damage_engine.combat_condition_lines(options)


def _trait_card_lines(weapon, entries, roll, *, live=False):
    lines, matrix = [], {}
    enhancements = entries[0][1]["evaluationTarget"]["enemyEnhancements"]
    variant = "default"
    if not enhancements.get("noOverguard", True):
        variant = "overguard"
    elif enhancements.get("shieldOverride"):
        variant = "shield"
    for key, result, scene, rows in entries:
        cycle = bool((result.get("evaluationScenario") or {}).get("incarnonCycle"))
        scene = {
            **scene,
            "incarnon_cycle": cycle,
            "incarnon_form": not cycle
            and bool(_fire_mode(weapon, result["fireMode"]).get("isIncarnonForm")),
        }
        lines.extend(
            _bundled_trait_summary(result, scene, rows, roll, melee=_is_melee_weapon(weapon))
        )
        matrix[key] = (_scenario_mode_label(result), {**scene, **rows})
        if live:
            lines.extend(_trait_condition_lines(result))
    return lines + riven_rating.trait_matrix(
        weapon, matrix, bundled_roll=roll, bundled_variant=variant, live=live
    )


def bundled_trait_lines(builds, document, weapon, mode, roll, *, target=None):
    if document is None or weapon is None:
        return None
    try:
        if builds["target"] != document["target"]:
            return None
        scenes = document["weapons"].get(weapon["path"], {})
        build_modes = _bundled_entries(builds, weapon, target)
        entries = []
        displayed = set()
        melee = weapon["weaponType"] in {"melee", "zaw"}
        identity = {**weapon, "category": "Melee" if melee else ""}
        for key, build in _bundled_modes(build_modes, mode, melee=melee):
            kind = build["mode"]["kind"]
            if key not in scenes or (mode is None and kind in displayed):
                continue
            scene = scenes[key]
            if scene["baseline_dps"] != build["full_dps"] or _bundled_headshot_chance(
                document, scene
            ) != _bundled_headshot_chance(builds, build):
                return None
            rows = {
                sign: _bundled_trait_rows(document, scene, sign, roll)
                for sign in ("positive", "negative")
            }
            if any(rows.values()):
                if kind in _INCARNON_MODES:
                    displayed.add(kind)
                entries.append(
                    (
                        key,
                        {
                            "weapon": identity,
                            "evaluationTarget": _bundled_target(builds, build),
                            "fireMode": build["mode"]["index"],
                            "evaluationScenario": build["mode"].get("scenario"),
                        },
                        scene,
                        rows,
                    )
                )
                if len(entries) == 3:
                    break
        if not entries:
            return None
        lines = _trait_card_lines(identity, entries, roll)
        if melee:
            alternatives = [
                kind
                for kind, key, build in _other_bundled_melee_modes(build_modes, entries[0][0])
                if (scene := scenes.get(key))
                and scene["baseline_dps"] == build["full_dps"]
                and (scene["positive"] or scene["negative"])
            ]
            if alternatives:
                lines.append(_bundled_mode_hint(alternatives, melee=True))
        return lines
    except (AttributeError, IndexError, KeyError, TypeError, ValueError):
        return None


async def bundled_traits(target=None, options=None):
    variant = bundled_variant(target, options)
    if variant is None:
        return None
    _, name = _BUNDLED_FILES[variant]
    return await asyncio.to_thread(_load_bundled, name)


def _read_live_result(path):
    try:
        if path.stat().st_size <= LIVE_CACHE_ENTRY_BYTES:
            result = json.loads(path.read_bytes())
            if isinstance(result, dict) and result.get("engineFingerprint") == FINGERPRINT:
                return result
    except (OSError, ValueError):
        pass
    return None


def _check_reference_result(result, request):
    check_result(result, request)
    _build_slots(result["baseline"], with_riven=False)
    for key in (
        "recommendedPositiveTiers",
        "recommendedNegative",
        "fatalNegative",
        "fatalNegativeAbove",
    ):
        if key not in result:
            raise ValueError("缺少词条推荐回执")


def _write_live_result(path, result):
    try:
        payload = json.dumps(result, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        if len(payload.encode()) > LIVE_CACHE_ENTRY_BYTES:
            return
        with _LIVE_CACHE_WRITE_LOCK:
            atomic_write_text(path, payload)
    except (OSError, ValueError):
        logger.warning("[sdjk] 无法保存独立引擎推荐缓存")


def _reference_analysis_request(mode, catalog_weapon, options, roll):
    # 样卡指定搜索的正词条数。回图只取无卡基线和理论词条推荐。
    bases = catalog_weapon.get("referenceRivenBases") or {}
    codes = ("0", "1", "2")
    if not all(damage_engine._bounded_number(bases.get(code), low=1e-9) for code in codes):
        raise CoverageUnavailable(
            L(
                "独立引擎缺少该武器的词条模型",
                "The standalone engine has no trait model for this weapon",
            )
        )
    weapon, fire_mode, headshot = mode
    weights = (1.2375, 1.2375, -0.495) if roll == "2+1-" else (0.75,) * 3
    request = {
        "weapon": weapon,
        "fireMode": fire_mode,
        "headshotChance": headshot,
        "disposition": REFERENCE_DISPOSITION,
        "rivenCard": {"rank": 8, "polarity": POLARITIES["naramon"]},
        "stats": [
            {
                "code": code,
                "value": bases[code] * REFERENCE_DISPOSITION * abs(weight),
                "negative": weight < 0,
            }
            for code, weight in zip(codes, weights, strict=True)
        ],
        "profile": "standard",
        "_reuseIncarnonWitness": True,
        "_useBakedIncarnonBaselines": True,
    }
    return damage_engine.with_portable_analysis_options(request, options)


async def _trait_matrix_request(port, request):
    # 只有 404 代表版本不支持。通用便携请求会把 HTTP 状态压成同一种错误。
    try:
        async with httpx.AsyncClient(
            timeout=SOLVE_TIMEOUT_S, follow_redirects=False, trust_env=False
        ) as client:
            response = await client.post(
                f"http://127.0.0.1:{port}{_TRAIT_MATRIX_ENDPOINT}", json=request
            )
        if response.status_code == 404:
            return None
        result = response.json()
    except (httpx.ConnectError, httpx.ConnectTimeout):
        raise damage_engine.EngineUnreachable(
            L("独立引擎未运行或无法连接", "The standalone engine is offline or unreachable")
        ) from None
    except httpx.TimeoutException:
        raise damage_engine.EngineTimeout(
            L("请求超时，请稍后重试。", "The request timed out. Try again shortly.")
        ) from None
    except (httpx.HTTPError, ValueError):
        raise damage_engine.EngineError(
            L(
                "独立引擎响应异常，请重试",
                "The standalone engine returned an invalid response. Try again.",
            )
        ) from None
    if not response.is_success or not isinstance(result, dict):
        error = result.get("error") if isinstance(result, dict) else None
        detail = error.get("message") if isinstance(error, dict) else ""
        message = L("暂时无法计算这组词条", "These traits could not be calculated")
        detail = " ".join(str(detail or "").split())[:120]
        raise damage_engine.EngineBuildError(f"{message} · {detail}" if detail else message)
    return result


def _check_trait_matrix(result, request):
    expected = request["benchmarkTarget"]
    target = result["evaluationTarget"]
    build = result["build"]
    scene = result["matrix"]
    if (
        result["weapon"]["path"] != request["weapon"]
        or result["fireMode"] != request["fireMode"]
        or target["enemyPath"] != expected["enemy"]
        or target["steelPath"] is not expected["steelPath"]
        or any(target[key] != expected[key] for key in ("level", "headshotChance", "windowSeconds"))
        or any(
            target[key] != request.get(key, 0) for key in ("armourStrip", "elementalArmourStrip")
        )
        or any(
            target["enemyEnhancements"].get(key) != value
            for key, value in expected["enemyEnhancements"].items()
        )
        or scene["baseline_dps"] != build["full_dps"]
        or scene["baseline_kpm"] != build["kpm"]
        or not build["capacity"]["valid"]
    ):
        raise ValueError("词条回执与所选工况不符")
    _check_scenario(result["evaluationScenario"], request)
    _live_trait_rows(scene, request["roll"])


def _live_trait_rows(scene, roll, *, headshot_chance=0.0):
    suffix = roll[0] + "1"
    return {
        sign: [
            _trait_result(
                row["stat_id"],
                row[f"shown_{suffix}"],
                row["unit"],
                sum(
                    flag
                    for name, flag in _TRAIT_FLAGS.items()
                    if row.get(name) and (name != "weakpoint" or headshot_chance < 1.0)
                ),
                row[f"pct_{suffix}"] if row["modeled"] else None,
                row.get("elem_build"),
            )
            for row in scene[sign]
        ]
        for sign in ("positive", "negative")
    }


def live_trait_lines(result, roll):
    if (scene := result.get("matrix")) is None:
        lines = [
            f"{scenario_line(result)} · {roll}",
            *_trait_condition_lines(result),
            *trait_lines(result),
        ]
        if result["searchWork"]["limited"]:
            lines.append(
                L("※ 旧版引擎搜索未完成。", "※ The older engine did not finish the search.")
            )
        lines.extend(_melee_incarnon_tier1_notes(result["weapon"]))
        return lines
    return _trait_card_lines(
        result["weapon"],
        [
            (
                result["fireMode"],
                result,
                scene,
                _live_trait_rows(
                    scene, roll, headshot_chance=result["evaluationTarget"]["headshotChance"]
                ),
            )
        ],
        roll,
        live=True,
    )


async def _reference_traits(host, event, port, mode, catalog_weapon, options, roll, on_solve):
    weapon, fire_mode, _ = mode
    request = damage_engine.with_portable_analysis_options(
        {"weapon": weapon, "fireMode": fire_mode, "profile": "standard", "roll": roll}, options
    )
    identity = damage_engine.portable_replay_fingerprint(
        {"endpoint": _TRAIT_MATRIX_ENDPOINT, "request": request}
    )
    service = host.damage_engine
    path = service.root / "portable-live-cache" / f"{identity}.json"

    async def solve():
        if (cached := await asyncio.to_thread(_read_live_result, path)) is not None:
            try:
                _check_trait_matrix(cached, request)
                return cached
            except (KeyError, TypeError, ValueError, OverflowError):
                pass
        async with damage_engine.simulation_session(
            host, event, managed=False, wait_timeout=SOLVE_QUEUE_TIMEOUT_S
        ):
            await service.ensure_portable(port)
            if on_solve is not None:
                await on_solve()
            result = await damage_engine._finish_compute(_trait_matrix_request(port, request))
            if result is None:
                legacy = _reference_analysis_request(mode, catalog_weapon, options, roll)
                result = await _solve_analysis(port, legacy)
                _check_reference_result(result, legacy)
            else:
                _check_trait_matrix(result, request)
                target = result["evaluationTarget"]
                result = {
                    **result,
                    "engineFingerprint": FINGERPRINT,
                    "evaluationTarget": {**target, "enemyName": target["nameEn"]},
                }
        result = {**result, "confirmedAnalysisOptions": options}
        # 旧版摘要由短期缓存复用，运行时更新后还能重新探测完整矩阵。
        if "matrix" in result:
            await asyncio.to_thread(_write_live_result, path, result)
        return result

    return await service.riven_build_cache.get_or_fetch(
        f"portable-traits:{port}:{identity}",
        RESULT_TTL_S,
        solve,
        cache_if=lambda result: result.get("searchWork", {}).get("limited", 0) == 0,
    )


async def recommendation_result(
    host,
    event,
    port,
    scope,
    catalog_weapon,
    options,
    *,
    traits=True,
    roll="3+1-",
    on_solve=None,
):
    options = damage_engine.with_gauge_charging_headshots(options)
    mode = (*scope[:2], options["benchmarkTarget"]["headshotChance"])
    if not traits:
        return await _reference_build(
            host, event, port, mode, catalog_weapon, options, on_solve=on_solve
        )
    return await _reference_traits(host, event, port, mode, catalog_weapon, options, roll, on_solve)


async def _reference_build(host, event, port, mode, catalog_weapon, options, *, on_solve=None):
    weapon, fire_mode, _ = mode
    fields = damage_engine.with_portable_analysis_options({}, options)
    target = fields.pop("benchmarkTarget")
    metric = "attackDps" if catalog_weapon.get("weaponType") in {"melee", "zaw"} else "sustainedDps"
    request = {
        **target,
        **fields,
        "weapon": weapon,
        "fireMode": fire_mode,
        "metric": metric,
        "profile": "standard",
    }
    if "arcanes" not in fields:
        request["autoArcane"] = True
    if "incarnonChoices" not in fields:
        request["autoIncarnon"] = True
    identity = damage_engine.portable_replay_fingerprint(
        {"endpoint": "/api/build", "request": request}
    )
    service = host.damage_engine
    path = service.root / "portable-live-cache" / f"{identity}.json"

    async def solve():
        if (cached := await asyncio.to_thread(_read_live_result, path)) is not None:
            try:
                _build_slots(cached["baseline"], with_riven=False)
                return cached
            except (KeyError, TypeError, ValueError):
                pass
        async with damage_engine.simulation_session(
            host, event, managed=False, wait_timeout=SOLVE_QUEUE_TIMEOUT_S
        ):
            await service.ensure_portable(port)
            if on_solve is not None:
                await on_solve()
            reply = await damage_engine._finish_compute(
                damage_engine.portable_request(port, "/api/build", request, timeout=SOLVE_TIMEOUT_S)
            )
        result = _reference_build_result(reply, request, options)
        await asyncio.to_thread(_write_live_result, path, result)
        return result

    return await service.riven_build_cache.get_or_fetch(
        f"portable-build:{port}:{identity}", RESULT_TTL_S, solve
    )


def _reference_build_result(reply, request, options, *, with_riven=False):
    context = reply["evaluationContext"]
    target = reply["evaluationTarget"]
    scenario = reply["evaluationScenario"]
    enhancements = context["enemy_enhancements"]
    if (
        reply["buildLabel"] != damage_engine.PORTABLE_DATA
        or reply.get("engineFingerprint", FINGERPRINT) != FINGERPRINT
        or reply["weapon"]["path"] != request["weapon"]
        or reply["fireMode"] != request["fireMode"]
        or reply["scoreMetric"] != "killsPerMinute"
        or target["path"] != request["enemy"]
        or context["level"] != request["level"]
        or context["steel_path"] is not request["steelPath"]
        or context["headshotChance"] != request["headshotChance"]
        or context["windowSeconds"] != request["windowSeconds"]
        or context["armour_strip"] != request.get("armourStrip", 0)
        or context["elemental_armour_strip"] != request.get("elementalArmourStrip", 0)
        or any(
            enhancements.get(key) != value for key, value in request["enemyEnhancements"].items()
        )
    ):
        raise ValueError("配装回执与所选工况不符")
    _check_scenario(scenario, request)
    route = reply.get("incarnonChoices") or {}
    if any(
        route.get(tier) != branch for tier, branch in request.get("incarnonChoices", {}).items()
    ):
        raise ValueError("配装回执的灵化路线与指定选择不符")
    build = {**reply["build"], "incarnonChoices": route}
    if not with_riven:
        _build_slots(build, with_riven=False)
    chosen = ((reply.get("autoLoadout") or {}).get("arcane") or {}).get("chosen") or ()
    arcanes = []
    for arc in chosen:
        name_zh, name_en = arc.get("nameZh"), arc.get("nameEn")
        if not name_en:
            name_en = (dc.find_arcane(name_zh or "", exact=True) or {}).get("name")
        name_en = name_en or arc["path"].rsplit("/", 1)[-1]
        arcanes.append(
            {
                "path": arc["path"],
                "rank": arc["rank"],
                **{key: arc[key] for key in ("stacks", "uptime") if key in arc},
                "nameZh": name_zh or name_en,
                "nameEn": name_en,
            }
        )
    return {
        "engineFingerprint": FINGERPRINT,
        "weapon": reply["weapon"],
        "fireMode": reply["fireMode"],
        "stance": reply.get("stance"),
        "exilus": reply.get("exilus"),
        "arcanes": arcanes,
        "innateElement": reply.get("innateElement"),
        ("actual" if with_riven else "baseline"): build,
        "evaluationScenario": scenario,
        "evaluationTarget": {
            "enemyPath": target["path"],
            "enemyName": target["nameEn"],
            "level": context["level"],
            "steelPath": context["steel_path"],
            "headshotChance": context["headshotChance"],
            "windowSeconds": context["windowSeconds"],
            "hitLocation": context["hit_location"],
            "armourStrip": context["armour_strip"],
            "elementalArmourStrip": context["elemental_armour_strip"],
            "enemyEnhancements": request["enemyEnhancements"],
        },
        "confirmedAnalysisOptions": options,
    }


def _valid_target(result, headshot, requested=None, *, armour_strip=0, elemental_strip=0):
    expected = (
        damage_engine.portable_target_fields(TARGET, dc.default_target())
        if requested is None
        else requested
    )
    target = result["evaluationTarget"]
    return (
        target["enemyPath"] == expected["enemy"]
        and target["level"] == expected["level"]
        and target["steelPath"] is expected["steelPath"]
        and target["enemyEnhancements"] == expected["enemyEnhancements"]
        and target["headshotChance"] == headshot
        and target["windowSeconds"] == expected["windowSeconds"]
        and target["armourStrip"] == armour_strip
        and target["elementalArmourStrip"] == elemental_strip
        and target["hitLocation"] == "body"
        and result["scoreMetric"] == "windowedDps"
        and (requested is not None or len(result["targetCalibration"]["hardnessNodes"]) == 3)
    )


async def benchmark_options(host, port, target):
    spec = {**dc.default_target(), **target}
    enemies = {dc.DEFAULT_ENEMY: [TARGET]}
    if spec["enemy"] and spec["enemy"]["name"] != dc.DEFAULT_ENEMY:
        service = host.damage_engine
        await service.ensure_portable(port)
        _, enemies = await damage_engine.portable_catalog(service, port)
    path, model = damage_engine.portable_target_enemy(spec, enemies)
    return {
        "benchmarkTarget": {**damage_engine.portable_target_fields(path, spec), **model},
        **damage_engine.combat_replay_options(spec.get("combat_options", {})),
    }


def weapon_benchmark_options(options, weapon, target):
    if "benchmarkTarget" not in options:
        return options
    benchmark = {**options["benchmarkTarget"], "headshotChance": _headshot_chance(weapon, target)}
    return {**options, "benchmarkTarget": benchmark}


def analysis_request(
    mode,
    card,
    polarity,
    *,
    source_disposition=None,
    options: damage_engine.PortableAnalysisOptions | None = None,
):
    weapon, fire_mode, _ = mode
    if (
        type(card.get("rank")) is not int
        or not 0 <= card["rank"] <= 8
        or (polarity is not None and polarity not in POLARITIES)
    ):
        raise damage_engine.EngineError(
            L(
                "请填写紫卡强化等级（0–8）和卡面极性",
                "Enter the Riven upgrade rank (0–8) and polarity",
            )
        )
    if source_disposition is not None and (
        not math.isfinite(source_disposition) or not 0 < source_disposition <= 2
    ):
        raise damage_engine.EngineError(
            L("请核对截图时的来源倾向", "Check the disposition when the card was captured")
        )
    stats = []
    card_weapon = card.get("weapon")
    melee = card_weapon and any(
        row["category"] == "Melee" for row in riven_rating.match_weapon(card_weapon)
    )
    for negative, lines in ((False, card["bonuses"]), (True, (card["malus"],))):
        for line in lines:
            if line is None:
                continue
            stat, value = line["id"], line["value"]
            if stat == "damage" and melee:
                stat = "melee_damage"
            stat = MELEE_ALIASES.get(stat, riven_rating._STAT.get(stat, stat))
            code = MELEE_CODES.get(stat) or GUN_CODES.get(stat)
            if code is None:
                raise damage_engine.EngineError(
                    L(
                        f"这个词条暂不能参加计算（{stat}）",
                        f"This stat cannot be used in the damage estimate ({stat})",
                    )
                )
            if stat.startswith("damage_to_") and (value > 1) == negative:
                raise damage_engine.EngineError(
                    L(
                        "请核对派系倍率与词条方向",
                        "Check the faction multiplier and trait direction",
                    )
                )
            magnitude = (
                round(abs(value - 1) * 100, 8) if stat.startswith("damage_to_") else abs(value)
            )
            if not math.isfinite(magnitude) or not magnitude > 0:
                raise damage_engine.EngineError(L("请核对词条数值", "Check the trait values"))
            stats.append({"code": code, "value": magnitude, "negative": negative})
    if len({stat["code"] for stat in stats}) != len(stats):
        raise damage_engine.EngineError(L("同一词条重复填写", "A trait is repeated"))
    request = {
        "weapon": weapon,
        "fireMode": fire_mode,
        "disposition": REFERENCE_DISPOSITION,
        "stats": stats,
        "rivenCard": {
            "rank": card["rank"],
            "polarity": POLARITIES.get(polarity, _RIVEN_POLARITY_PLACEHOLDER),
        },
        "profile": "standard",
        "headshotChance": (options or {})
        .get("benchmarkTarget", {})
        .get("headshotChance", _headshot_chance(weapon, {})),
        "_reuseIncarnonWitness": True,
        "_useBakedIncarnonBaselines": True,
    }
    if source_disposition is not None:
        request["sourceDisposition"] = source_disposition
    request = damage_engine.with_portable_analysis_options(request, options)
    return damage_engine.with_gauge_charging_headshots(request)


def check_result(result, request):
    returned = result["actual"]["riven"]
    target_disposition = request["disposition"]
    scale = target_disposition / request.get("sourceDisposition", target_disposition)
    expected_stats, actual_stats = request["stats"], returned["stats"]
    same_stats = len(expected_stats) == len(actual_stats) and all(
        before["code"] == after["code"]
        and before["negative"] is after["negative"]
        and math.isclose(before["value"] * scale, after["value"], rel_tol=1e-12, abs_tol=1e-8)
        for before, after in zip(expected_stats, actual_stats)
    )
    if (
        result["engineFingerprint"] != FINGERPRINT
        or result["weapon"]["path"] != request["weapon"]
        or result["fireMode"] != request["fireMode"]
        or result["scoreMetric"] != "windowedDps"
        or result["disposition"] != target_disposition
        or not same_stats
        or type(returned["rank"]) is not int
        or any(returned[key] != value for key, value in request["rivenCard"].items())
        or type(returned["drain"]) is not int
        or returned["drain"] != 10 + returned["rank"]
        or not _valid_target(
            result,
            request["headshotChance"],
            request.get("benchmarkTarget"),
            armour_strip=request.get("armourStrip", 0),
            elemental_strip=request.get("elementalArmourStrip", 0),
        )
        or type(result["searchWork"]["limited"]) is not int
    ):
        raise damage_engine.EngineError(
            L(
                "本次计算没有匹配这张牌面或所选模式，请重试",
                "The estimate did not match this card and mode. Try again.",
            )
        )
    _placement(result["actual"], stance=result.get("stance"))
    if requested := request.get("exilus"):
        selected = result.get("exilus") or result["actual"].get("exilus")
        acknowledged = "exilus" in result or "exilus" in result["actual"]
        if not acknowledged or (
            requested.get("mode") != "auto"
            and any((selected or {}).get(key) != value for key, value in requested.items())
        ):
            raise damage_engine.EngineError(
                L(
                    "当前独立引擎未确认配卡扩展槽",
                    "The standalone engine did not confirm the analysis Exilus slot",
                )
            )
    if arcanes := request.get("arcanes"):
        active = {
            (row["path"], row["rank"])
            for row in result.get("arcaneStates", ())
            if row.get("applied")
        }
        if active != {(row["path"], row["rank"]) for row in arcanes}:
            raise damage_engine.EngineError(
                L("配卡搜索采用的赋能与输入不符", "The build search used different Arcanes")
            )
    _check_scenario(result.get("evaluationScenario") or {}, request)


def _check_scenario(actual_scenario, request):
    for key, value in request.get("scenario", {}).items():
        actual = actual_scenario.get(key, False if key == "modelSpread" else None)
        if key == "combatState" and isinstance(actual, dict):
            actual = {name: actual.get(name) for name in value}
        if actual != value:
            raise damage_engine.EngineError(
                L(
                    "独立引擎未按所选战斗条件搜索",
                    "The standalone engine did not use the selected combat options",
                )
            )


async def _solve_analysis(port, request, *, rank=None):
    try:
        return await damage_engine._finish_compute(
            damage_engine.portable_request(port, "/api/analyze", request, timeout=SOLVE_TIMEOUT_S)
        )
    except damage_engine.EngineBuildError as exc:
        detail = str(exc)
        if any(
            text in detail.casefold()
            for text in (
                "weapon is not in the portable riven catalog",
                "weapon analysis is unavailable",
                "weapon is unavailable",
            )
        ):
            raise CoverageUnavailable(detail) from None
        if (
            rank is not None
            and rank < 8
            and re.search(
                r"\b(?:max[ -]?rank|rank\s*(?:must be|[:=])?\s*8|full[ -]?rank)\b",
                detail,
                re.I,
            )
        ):
            raise damage_engine.EngineBuildError(
                L(
                    "当前便携引擎只支持满级紫卡。请更新引擎后再搜索非满级配装。",
                    "This portable engine only supports max-rank Rivens. Update it to search with a lower-rank card.",
                )
            ) from None
        if "Riven stat" in detail and "outside" in detail:
            hint = L(
                "词条数值与提交倾向的区间不符。旧卡可按已核实的来源倾向重试，例如「倾向1.3」，也可提供当前卡面。",
                "Traits fall outside the entered disposition range. Provide the confirmed original disposition (e.g. d1.3) or the current card.",
            )
            raise damage_engine.EngineBuildError(hint) from None
        raise


async def analyze(
    host,
    event,
    port,
    mode,
    card,
    polarity,
    *,
    source_disposition=None,
    options: damage_engine.PortableAnalysisOptions | None = None,
    on_solve=None,
):
    request = analysis_request(
        mode, card, polarity, source_disposition=source_disposition, options=options
    )
    unspecified_polarity = polarity is None
    identity = damage_engine.portable_replay_fingerprint(
        {"request": request, "unspecifiedPolarity": unspecified_polarity}
    )
    service = host.damage_engine

    async def solve():
        async with damage_engine.simulation_session(
            host, event, managed=False, wait_timeout=SOLVE_QUEUE_TIMEOUT_S
        ):
            await service.ensure_portable(port)
            if on_solve is not None:
                await on_solve()
            result = await _solve_analysis(port, request, rank=card["rank"])
        check_result(result, request)
        if unspecified_polarity:
            result = _fit_unspecified_riven_polarity(result)
        return {
            **result,
            "cardSourceDisposition": source_disposition,
            **({"confirmedAnalysisOptions": options} if options else {}),
        }

    return await service.riven_build_cache.get_or_fetch(
        f"portable-result:{port}:{identity}",
        RESULT_TTL_S,
        solve,
        cache_if=lambda result: result["searchWork"]["limited"] == 0,
    )


def _riven_build_request(analysis, catalog):
    """只有目录可核对牌面资格和数值时，才使用只消费实值的配装接口。"""
    if analysis["rivenCard"]["rank"] != 8 or not catalog.get("rivenStats"):
        return None
    stats = analysis["stats"]
    negatives = sum(row["negative"] for row in stats)
    factors = riven_analysis.FACTOR.get((len(stats) - negatives, negatives))
    if factors is None:
        return None
    disposition = analysis["disposition"]
    source = analysis.get("sourceDisposition", disposition)
    fusion = 0
    for row in stats:
        rule = catalog["rivenStats"].get(row["code"], {})
        negative = row["negative"]
        base = rule.get("baseValue")
        if rule.get(
            "canNegative" if negative else "canPositive"
        ) is not True or not damage_engine._bounded_number(base, low=1e-12):
            return None
        fusion += bool(rule.get("isFusion"))
        nominal = base * source * factors[negative]
        if (negative and rule.get("isFusion")) or not (
            nominal * 0.9 - 0.11 <= row["value"] <= nominal * 1.1 + 0.11
        ):
            return None
    if fusion > 1:
        return None
    target = analysis.get("benchmarkTarget") or {
        **damage_engine.portable_target_fields(TARGET, dc.default_target()),
        "headshotChance": analysis["headshotChance"],
    }
    options = {
        key: value
        for key, value in analysis.items()
        if key in damage_engine.PortableAnalysisOptions.__optional_keys__
        and key != "benchmarkTarget"
    }
    return {
        **target,
        **options,
        "weapon": analysis["weapon"],
        "fireMode": analysis["fireMode"],
        "profile": analysis["profile"],
        "metric": "killsPerMinute",
        "autoArcane": "arcanes" not in options,
        "autoIncarnon": "incarnonChoices" not in options,
        "riven": {
            **analysis["rivenCard"],
            "stats": [{**row, "value": row["value"] * (disposition / source)} for row in stats],
        },
    }


async def build_riven(
    host, event, port, mode, card, polarity, catalog, *, source_disposition, options, on_solve
):
    """走标准档配装搜索，使用固定回放恢复可实际使用的槽序。"""
    analysis = analysis_request(
        mode, card, polarity, source_disposition=source_disposition, options=options
    )
    request = _riven_build_request(analysis, catalog)
    if request is None:
        return None
    service = host.damage_engine
    identity = damage_engine.portable_replay_fingerprint(
        {
            "endpoint": "/api/build",
            "request": request,
            "sourceDisposition": source_disposition,
            "unspecifiedPolarity": polarity is None,
        }
    )

    async def solve():
        async with damage_engine.simulation_session(
            host, event, managed=False, wait_timeout=SOLVE_QUEUE_TIMEOUT_S
        ):
            await service.ensure_portable(port)
            await on_solve()
            reply = await damage_engine._finish_compute(
                damage_engine.portable_request(port, "/api/build", request, timeout=SOLVE_TIMEOUT_S)
            )
        result = _reference_build_result(reply, request, options, with_riven=True)
        actual = result["actual"]
        returned = actual["riven"]
        exilus = request.get("exilus")
        auto = reply.get("autoLoadout") or {}
        score = actual["objectiveScore"]
        if (
            actual["includesRiven"] is not True
            or any(returned.get(key) != value for key, value in request["riven"].items())
            or not damage_engine._bounded_number(score)
            or (
                request["autoArcane"]
                and not isinstance((auto.get("arcane") or {}).get("chosen"), list)
            )
            or (request["autoIncarnon"] and actual["incarnonChoices"] and not auto.get("incarnon"))
            or (
                exilus
                and (
                    "exilus" not in reply
                    or (
                        exilus.get("mode") != "auto"
                        and any(
                            (result["exilus"] or {}).get(key) != value
                            for key, value in exilus.items()
                        )
                    )
                )
            )
        ):
            raise ValueError("带卡配装回执与输入不符")
        actual["riven"] = {**returned, "drain": 10 + returned["rank"]}
        if polarity is None:
            result["unspecifiedRivenPolarity"] = True
        async with service.replays.slot():
            actual["forma"] = await _riven_slot_plan(port, result, catalog)
        _placement(actual, stance=result.get("stance"))
        return {
            **result,
            "disposition": analysis["disposition"],
            "cardSourceDisposition": source_disposition,
            "scoreMetric": reply["scoreMetric"],
            "scoringValues": {"actual": score},
            "searchWork": {"limited": 0},
        }

    return await service.riven_build_cache.get_or_fetch(
        f"portable-riven-build:{port}:{identity}", RESULT_TTL_S, solve
    )


def _polarity_label(code):
    key = POLARITY_KEYS.get(code)
    if key is None:
        return None
    name = L("通用", "Universal") if key == "universal" else key.capitalize()
    return f"⟦pol:{key}⟧{name}"


def _placement(actual, *, stance=None):
    plan = actual["forma"]
    riven = actual["riven"]
    placement = plan["placement"]
    by_origin = {mod["slot"]: mod for mod in actual["mods"]}
    origins = [slot["origin"] for slot in placement if slot is not None]
    card_slots = [
        slot for slot in placement if slot is not None and slot["origin"] == riven["slot"]
    ]
    raw_drain = riven["drain"]
    exilus_drain = (actual.get("exilus") or {}).get("drain", 0)
    stance = stance or actual.get("stance")
    limit = actual.get("capacityLimit")
    expected_limit = 60
    if stance:
        gain = stance.get("capacityGain", 0)
        if type(gain) is not int or gain < 0:
            raise ValueError("架势容量增益无效")
        expected_limit = limit if limit is not None else 60 + gain
    if (
        plan["fits"] is not True
        or type(exilus_drain) is not int
        or exilus_drain < 0
        or type(expected_limit) is not int
        or expected_limit < 60
        or (limit is not None and (type(limit) is not int or limit != expected_limit))
        or type(plan["limit"]) is not int
        or plan["limit"] != expected_limit
        or plan["drain"]
        != exilus_drain + sum(slot["drain"] for slot in placement if slot is not None)
        or not 0 <= plan["drain"] <= plan["limit"]
        or len(placement) != 8
        or riven["slot"] in by_origin
        or len(by_origin) != len(actual["mods"])
        or len(origins) != len(set(origins))
        or set(origins) != {*by_origin, riven["slot"]}
        or any(
            type(slot["drain"]) is not int or slot["drain"] < 0
            for slot in placement
            if slot is not None
        )
        or len(card_slots) != 1
        or card_slots[0]["drain"]
        not in (math.ceil(raw_drain / 2), raw_drain, math.ceil(raw_drain * 1.25))
    ):
        raise ValueError("配装容量或槽位无效")
    return plan, riven, placement, by_origin


def _fit_unspecified_riven_polarity(result):
    """未知极性不能预先匹配原生槽，除非该槽本来就是通用极性。"""
    actual = result["actual"]
    plan, riven, placement, _ = _placement(actual, stance=result.get("stance"))
    index, slot = next(
        (index, slot)
        for index, slot in enumerate(placement)
        if slot is not None and slot["origin"] == riven["slot"]
    )
    if slot.get("polarity") != "AP_ANY":
        changes = plan.get("slots", {})
        added = int(str(index) not in changes)
        half = (riven["drain"] + 1) // 2
        placement = list(placement)
        placement[index] = {**slot, "drain": half, "polarity": riven["polarity"]}
        plan = {
            **plan,
            "drain": plan["drain"] - slot["drain"] + half,
            "total": plan["total"] + added,
            "slots": {**changes, str(index): riven["polarity"]},
            "placement": placement,
        }
        if "regular" in plan:
            plan["regular"] += added
        actual = {**actual, "forma": plan}
        _placement(actual, stance=result.get("stance"))
    return {**result, "actual": actual, "unspecifiedRivenPolarity": True}


def enemy_label(path, english, fallback=""):
    if is_en():
        return english
    name = riven_rating.display_names().get("enemies", {}).get(path)
    return name or fallback or (find_enemy_exact(english) or {}).get("zh") or english


def _scenario_mode_label(result):
    weapon = result.get("weapon") or {}
    scenario = result.get("evaluationScenario") or {}
    if label := riven_rating.melee_scenario_mode_label(scenario):
        return label
    if scenario.get("incarnonCycle"):
        return riven_rating.incarnon_cycle_label(
            weapon, scenario, (result.get("evaluationTarget") or {}).get("headshotChance", 0.0)
        )
    melee = (
        bool(result.get("stance"))
        or weapon.get("category") == "Melee"
        or weapon.get("weaponType") in {"melee", "zaw"}
    )
    return _mode_label(weapon, result["fireMode"], melee=melee)


def scenario_line(result, *, show_defense=True):
    target = result["evaluationTarget"]
    mode = _scenario_mode_label(result)
    weakpoint_chance = target["headshotChance"]
    hit = (
        L(f"弱点 {weakpoint_chance:.0%}", f"Weak points {weakpoint_chance:.0%}")
        if weakpoint_chance
        else L("躯干", "body")
    )
    if weakpoint_chance == 1.0 and (
        (result.get("weapon") or {}).get("path") in HEADSHOT_REQUIRED_WEAPONS
    ):
        hit += L("（机制需爆头）", " (headshot-triggered)")
    english = target["enemyName"]
    name = enemy_label(target.get("enemyPath"), english)
    if (distance := target.get("distance")) is not None:
        hit += L(f" · {distance:g} 米", f" · {distance:g} m")
    difficulty = (
        L("钢铁之路", "Steel Path") if target.get("steelPath", True) else L("普通", "Normal")
    )
    defense = " · " + _defense_line(target) if show_defense else ""
    if show_defense and (seconds := target.get("windowSeconds")) is not None:
        defense += L(f" · {seconds:g} 秒", f" · {seconds:g} s")
    return f"◆ {mode} · {difficulty} {name} Lv{target['level']} · {hit}{defense}"


def _defense_line(target):
    return " · ".join(
        damage_engine.portable_defense_labels(
            find_enemy_exact(target["enemyName"]), target.get("enemyEnhancements") or {}
        )
    )


def _card_basis_line(result):
    target = result["disposition"]
    source = result.get("cardSourceDisposition")
    if source is None:
        return L(
            "· 按卡面数值计算 · 原卡倾向待核对",
            "· Uses the card values shown · Original disposition unverified",
        )
    return L(
        f"· 原卡倾向 {source:g} → 当前参考 {target:g}",
        f"· Original disposition {source:g} → Reference {target:g}",
    )


def evolution_lines(weapon, choices, *, reference=False):
    """列出本次实际采用的进化，英文按同版本的武器路径与分支取显示数据。"""
    choices = choices or {}
    options = {
        option["id"]: option
        for tier in (weapon.get("incarnon") or ())
        for option in tier["options"]
    }
    codes = [choices[key] for key in sorted(choices)]
    if "1-1" in options and "1-1" not in codes:
        codes.insert(0, "1-1")
    notes = _melee_incarnon_tier1_notes(weapon) if reference else []
    if not codes and not notes:
        return []
    names = riven_rating.display_names()
    path = weapon.get("path")
    descriptions = names.get("incarnon", {}).get(path, {})
    descriptions_zh = names.get("incarnonZh", {}).get(path, {})
    evo, _ = _load_evo_data()
    tiers = evo.get(_weapon_family_key({"name": weapon.get("nameEn")}), {}).get("tiers", {})
    lines = [L("◆ 灵化进化", "◆ Incarnon evolutions"), *notes]
    for code in codes:
        option = options.get(code, {})
        en_label, en_effect = descriptions.get(code, ("", ""))
        tier, _, position = code.partition("-")
        fallback_options = tiers.get(tier, ())
        fallback = (
            fallback_options[int(position) - 1]
            if position.isdecimal() and 1 <= int(position) <= len(fallback_options)
            else {}
        )
        if is_en():
            label = (
                en_label or option.get("labelEn") or option.get("label") or fallback.get("name", "")
            )
            effect = (
                en_effect
                or option.get("descriptionEn")
                or option.get("description")
                or fallback.get("description")
            )
            effect = effect or "Effect description unavailable"
        else:
            label = option.get("label") or en_label or fallback.get("name", "")
            effect = (
                descriptions_zh.get(code)
                or option.get("description")
                or fallback.get("description", "")
            )
            effect = effect.replace("。；", "。").replace("；", "，").replace("。：", "，")
        if code == "1-1" and (not label or label == code):
            label = L("灵化形态", "Incarnon Form")
        name = f"{code} {label}" if label and label != code else code
        lines.append(f"· {name} · {effect}" if effect else f"· {name}")
    return lines


def _build_slots(build, *, with_riven, stance=None):
    slots = [None] * 8
    if with_riven:
        _, card, placement, mods = _placement(build, stance=stance)
        for index, slot in enumerate(placement):
            if slot is None:
                continue
            origin = slot["origin"]
            placed = card if origin == card["slot"] else mods[origin]
            slots[index] = {**placed, "slot": index}
    else:
        if build["includesRiven"] or not build["forma"]["fits"]:
            raise ValueError("无紫卡底表装配不可用")
        if placement := build["forma"].get("placement"):
            mods = {mod["slot"]: mod for mod in build["mods"]}
            origins = [slot["origin"] for slot in placement if slot is not None]
            if len(placement) != 8 or len(origins) != len(mods) or set(origins) != mods.keys():
                raise ValueError("无紫卡底表槽位无效")
            return [
                {**mods[slot["origin"]], "slot": index} if slot is not None else None
                for index, slot in enumerate(placement)
            ]
        for mod in build["mods"]:
            index = mod["slot"]
            if not 0 <= index < len(slots) or slots[index] is not None:
                raise ValueError("无紫卡底表槽位无效")
            slots[index] = mod
    return slots


def _fit_reference_slots(request, validation):
    """旧参考表缺少移槽后的槽序，按现有槽序补足极化。"""
    deficit = validation["capacity"] - validation["capacityLimit"]
    polarities = list(request["slotPolarities"])
    changes = sorted(
        (
            (row["drain"] - (row["rawDrain"] + 1) // 2, row["slot"], row["modPolarity"])
            for row in validation["assignments"]
            if row["modPolarity"] and row["slot"] < len(polarities)
        ),
        reverse=True,
    )
    for saving, slot, polarity in changes:
        if saving <= 0:
            break
        polarities[slot] = polarity
        deficit -= saving
        if deficit <= 0:
            return {**request, "slotPolarities": polarities}
    raise damage_engine.EngineBuildError("参考配装超过容量")


def _serration_alternative(request):
    """在原工况同槽试算满级膛线。"""
    mods = request["mods"]
    if "riven" in request or any(mod["path"] == _SERRATION for mod in mods):
        return None
    for index, mod in enumerate(mods):
        if mod["path"] == _HEAVY_CALIBER and mod["rank"] == 10:
            replacement = {**mod, "path": _SERRATION}
            return mod["slot"], {
                **request,
                "mods": [*mods[:index], replacement, *mods[index + 1 :]],
            }
    return None


async def _mod_gains(
    service, port, request, *, baseline_request=None, persistent=False, unspecified_polarity=False
):
    """用完整配装和各槽留空时的同靶 DPS 作对照。"""
    fields = [FINGERPRINT, port, request, baseline_request]
    if unspecified_polarity:
        fields.append("unspecifiedRivenPolarity")
    identity = hashlib.sha256(json.dumps(fields, sort_keys=True).encode())
    key = identity.hexdigest()

    async def fetch():
        if persistent:
            disk_key = damage_engine.portable_replay_fingerprint(
                {"endpoint": "/api/simulate", "request": request}
            )
            path = service.root / "portable-live-cache" / f"{disk_key}.json"
            if (cached := await asyncio.to_thread(_read_live_result, path)) is not None:
                try:
                    return {
                        **cached,
                        "without": {int(slot): dps for slot, dps in cached["without"].items()},
                    }
                except (AttributeError, KeyError, ValueError):
                    pass
        body = request
        replacement_slot = None
        capacity_saved = 0
        comparison_complete = True
        baseline = None

        async def replay(body):
            response = await damage_engine._finish_compute(
                damage_engine.portable_request(port, "/api/simulate", body, timeout=5)
            )
            window, validation = damage_engine.validate_fixed_replay(response, body)
            if (response.get("incarnonChoices") or {}) != body["incarnonChoices"]:
                raise damage_engine.EngineError("固定回放的灵化路线不符")
            return window["windowDps"], validation, response

        async def fitted(body):
            measurement = await replay(body)
            if not measurement[1]["valid"] and "riven" not in body:
                body = _fit_reference_slots(body, measurement[1])
                measurement = await replay(body)
            if not measurement[1]["valid"]:
                raise damage_engine.EngineBuildError(
                    L("装配超过容量", "The build exceeds capacity")
                )
            return body, measurement

        async with service.replays.slot():
            await service.ensure_portable(port)
            body, (full, validation, response) = await fitted(body)
            if baseline_request is not None:
                try:
                    _, (baseline, _, _) = await fitted(baseline_request)
                except damage_engine.EngineError:
                    comparison_complete = False
            if (alternative := _serration_alternative(body)) is not None:
                slot, alternate_body = alternative
                try:
                    alternate_dps, alternate_loadout, alternate_response = await replay(
                        alternate_body
                    )
                    saving = validation["capacity"] - alternate_loadout["capacity"]
                    valid = alternate_loadout["valid"]
                except (KeyError, TypeError, ValueError):
                    comparison_complete = False
                else:
                    if valid and saving > 0 and math.isclose(full, alternate_dps, rel_tol=1e-12):
                        body, full = alternate_body, alternate_dps
                        validation, response = alternate_loadout, alternate_response
                        replacement_slot, capacity_saved = slot, saving
            stats = response.get("moddedStats") or {}
            zero_stack_panel = None
            stacked = [
                arc
                for arc in response.get("arcanes", ())
                if arc.get("applied") and (arc.get("stacks") or 0) > 0
            ]
            if stacked and body.get("arcanes"):
                paths = {arc["path"] for arc in stacked}
                neutral = [
                    {**arc, "stacks": 0} if arc["path"] in paths else arc for arc in body["arcanes"]
                ]
                try:
                    _, _, initial = await replay({**body, "arcanes": neutral})
                    zero_stack_panel = initial.get("moddedStats")
                except damage_engine.EngineError:
                    comparison_complete = False
            unsupported = set(response.get("unsupported") or ())
            variants = {}
            for mod in body["mods"]:
                replaced = [row for row in body["mods"] if row["slot"] != mod["slot"]]
                if f"weapon-mod-mechanic-unmodeled:{mod['path']}" not in unsupported:
                    variants[mod["slot"]] = {**body, "mods": replaced}
            if card := body.get("riven"):
                variants[card["slot"]] = {k: v for k, v in body.items() if k != "riven"}
            without = {}
            for slot, variant in variants.items():
                try:
                    dps, variant_loadout, _ = await replay(variant)
                    if variant_loadout["valid"]:
                        without[slot] = dps
                except damage_engine.EngineBuildError:
                    continue
                except damage_engine.EngineError:
                    break
        shown = zero_stack_panel or stats
        equipment = {
            "exilus": response.get("exilus") if body.get("exilus") else None,
            "arcanes": response.get("arcanes", []) if body.get("arcanes") else [],
            "stance": response.get("stance") or (response.get("selection") or {}).get("stance"),
        }
        result = {
            "full": full,
            "baseline": baseline,
            "without": without,
            "capacity": validation,
            "stats": {key: shown.get(key) for key in (*damage_engine.PANEL_FIELDS, "damageTypes")},
            "panel_stack_assumptions": [
                {key: arc.get(key) for key in ("nameEn", "nameZh", "stacks")} for arc in stacked
            ],
            "panel_includes_stacks": bool(stacked and not zero_stack_panel),
            "stacked_panel": {
                key: stats[key]
                for key in damage_engine.PANEL_FIELDS
                if zero_stack_panel and key in stats and zero_stack_panel.get(key) != stats[key]
            },
            "has_overguard": bool((response.get("targetDamage") or {}).get("overguard")),
            "polarities": body["slotPolarities"],
            "equipment": equipment,
            "serration_slot": replacement_slot,
            "capacity_saved": capacity_saved,
            "complete": comparison_complete and len(without) == len(variants),
        }
        if persistent and result["complete"]:
            await asyncio.to_thread(
                _write_live_result, path, {**result, "engineFingerprint": FINGERPRINT}
            )
        return result

    return await service.portable_gains.get_or_fetch(
        key, RESULT_TTL_S, fetch, cache_if=lambda result: result["complete"]
    )


def build_replay_request(
    result,
    weapon_path,
    slots,
    polarities,
    route,
    *,
    with_riven=False,
    options: damage_engine.PortableReplayOptions | None = None,
):
    """整套配装和拆卡对照共用请求。调用方先核对辅助装备属于本次求解工况。"""
    target = result["evaluationTarget"]
    request = {
        "weapon": weapon_path,
        "fireMode": result["fireMode"],
        "enemy": target["enemyPath"],
        "level": target["level"],
        "steelPath": target["steelPath"],
        "headshotChance": target["headshotChance"],
        "armourStrip": target["armourStrip"],
        "elementalArmourStrip": target["elementalArmourStrip"],
        "enemyEnhancements": {
            key: value
            for key, value in target["enemyEnhancements"].items()
            if key in damage_engine.PortableEnemyEnhancements.__optional_keys__
        },
        "windowSeconds": target["windowSeconds"],
        "metric": "windowedDps",
        "damageModel": "target",
        "slotPolarities": polarities,
        "incarnonChoices": route,
        "mods": [
            {key: row[key] for key in ("path", "rank", "slot")}
            for row in slots
            if row is not None and "path" in row
        ],
    }
    if with_riven:
        request["riven"] = next(row for row in slots if row is not None and "path" not in row)
    request = damage_engine.with_portable_replay_options(request, options)
    return damage_engine.with_gauge_charging_headshots(request)


def _build_replay_options(result, build, confirmed=None):
    confirmed = {**(result.get("confirmedAnalysisOptions") or {}), **(confirmed or {})}
    options = {
        key: confirmed[key]
        for key in ("arcanes", "innateElement", "innateElementValue", "kitgunParts")
        if key in confirmed
    }
    if innate := result.get("innateElement"):
        options.setdefault("innateElement", innate["damageType"])
        options.setdefault("innateElementValue", innate["value"])
    if "arcanes" not in options:
        selected_arcanes = result.get("arcanes")
        arcanes = selected_arcanes or [
            arc for arc in result.get("arcaneStates", ()) if arc.get("applied")
        ]
        if arcanes:
            keys = ("path", "rank", "stacks", "uptime") if selected_arcanes else ("path", "rank")
            options["arcanes"] = [
                {key: arc[key] for key in keys if arc.get(key) is not None} for arc in arcanes
            ]
    selected = build.get("exilus") or result.get("exilus")
    fixed = confirmed.get("exilus") or {}
    exilus = selected or (fixed if fixed.get("path") else None)
    if exilus:
        options["exilus"] = {key: exilus[key] for key in ("path", "rank")}
        if (polarity := exilus.get("slotPolarity")) is not None:
            options["specialSlotPolarities"] = {"exilus": polarity}
    if (stance := result.get("stance")) and stance.get("path") and not stance.get("builtIn"):
        options["stancePath"] = stance["path"]
    scenario = damage_engine.replay_scenario(
        result.get("evaluationScenario") or result.get("scenario")
    )
    scenario.update(confirmed.get("scenario") or {})
    if scenario:
        options["scenario"] = scenario
    return options


def _request_for_build(result, weapon, *, with_riven=False, confirmed=None):
    build = result["actual" if with_riven else "baseline"]
    plan = build["forma"]
    slots = _build_slots(build, with_riven=with_riven, stance=result.get("stance"))
    original = (list(weapon.get("polarities") or ()) + [None] * 10)[:10]
    polarities = original[:8]
    for slot, polarity in plan.get("slots", {}).items():
        polarities[int(slot)] = polarity
    route = build.get("incarnonChoices") or {}
    options = _build_replay_options(result, build, confirmed=confirmed)
    request = build_replay_request(
        result, weapon["path"], slots, polarities, route, with_riven=with_riven, options=options
    )
    benchmark = (confirmed or {}).get("benchmarkTarget") or (
        result.get("confirmedAnalysisOptions") or {}
    ).get("benchmarkTarget", {})
    if benchmark.get("damageModel") == "custom":
        request.update(damageModel="custom", defenseSnapshot=benchmark["defenseSnapshot"])
    return request, original


async def _riven_slot_plan(port, result, weapon):
    # /api/build 给出求解顺序和极化摘要，摘要里的槽位对应移槽后的顺序。
    # 先在原顺序回放以取得每张卡的原始容量，再求保留元素顺序的可装布局。
    actual = result["actual"]
    riven = actual["riven"]
    mods = {mod["slot"]: mod for mod in actual["mods"]}
    cards = {**mods, riven["slot"]: riven}
    if len(cards) != len(mods) + 1 or not cards.keys() <= set(range(8)):
        raise ValueError("配装槽位无效")
    polarities = (list(weapon.get("polarities") or ()) + [None] * 8)[:8]
    options = _build_replay_options(result, actual)

    async def replay(order, slot_polarities):
        slots = [
            {**cards[origin], "slot": slot} if origin is not None else None
            for slot, origin in enumerate(order)
        ]
        request = build_replay_request(
            result,
            weapon["path"],
            slots,
            slot_polarities,
            actual["incarnonChoices"],
            with_riven=True,
            options=options,
        )
        reply = await damage_engine._finish_compute(
            damage_engine.portable_request(port, "/api/simulate", request, timeout=5)
        )
        window, validation = damage_engine.validate_fixed_replay(reply, request)
        return window["windowDps"], validation, reply.get("exilus")

    original_order = [index if index in cards else None for index in range(8)]
    full, validation, exilus = await replay(original_order, polarities)
    riven_slot = riven["slot"]
    costs = {
        row["slot"]: (row["rawDrain"], row["modPolarity"])
        for row in validation["assignments"]
        if row["slot"] in mods
    }
    costs[riven_slot] = (riven["drain"], riven["polarity"])
    # 保留化合所依赖的单元素卡顺序，最终还要与原顺序的完整伤害对拍。
    elements = {"DT_FIRE", "DT_FREEZE", "DT_ELECTRICITY", "DT_POISON"}
    elemental = {
        index
        for index, mod in mods.items()
        if any(row["damageType"] in elements and row["value"] > 0 for row in mod["displayEffects"])
    }
    if any(row["code"] in {"4", "5", "6", "7"} and not row["negative"] for row in riven["stats"]):
        elemental.add(riven_slot)
    elemental_order = sorted(elemental)
    exilus_drain = (exilus or {}).get("drain", 0)
    limit = validation["capacityLimit"]
    best = None
    for order in permutations(original_order):
        if [origin for origin in order if origin in elemental] != elemental_order:
            continue
        drain = exilus_drain
        placement = [None] * 8
        savings = []
        changes = {}
        for slot, origin in enumerate(order):
            if origin is None:
                continue
            raw, mod_polarity = costs[origin]
            polarity = polarities[slot]
            half = (raw + 1) // 2
            used = raw
            if (
                result.get("unspecifiedRivenPolarity")
                and origin == riven_slot
                and polarity != "AP_ANY"
            ):
                used, polarity = half, mod_polarity
                changes[str(slot)] = polarity
            elif mod_polarity not in _NO_POLARITY and polarity not in _NO_POLARITY:
                used = (
                    half
                    if "AP_ANY" in (mod_polarity, polarity) or mod_polarity == polarity
                    else (5 * raw + 3) // 4
                )
            drain += used
            placement[slot] = {"origin": origin, "drain": used, "polarity": polarity}
            if used > half and mod_polarity not in _NO_POLARITY:
                savings.append((used - half, slot, mod_polarity, half))
        for saving, slot, polarity, half in sorted(savings, reverse=True):
            if drain <= limit:
                break
            changes[str(slot)] = polarity
            placement[slot].update(drain=half, polarity=polarity)
            drain -= saving
        if drain > limit:
            continue
        # 极化次数相同时优先保留靠前槽位的原生极性，再尽量少移动卡片。
        key = (
            len(changes),
            sum(1 << (7 - int(slot)) for slot in changes),
            sum(before != after for before, after in zip(original_order, order)),
        )
        if best is None or key < best[0]:
            best = (key, order, changes, placement, drain)
    if best is None:
        raise ValueError("原生辅助槽下配装容量不足")
    _, order, changes, placement, drain = best
    fitted = [changes.get(str(slot), value) for slot, value in enumerate(polarities)]
    fitted_dps, checked, _ = await replay(order, fitted)
    if (
        not checked["valid"]
        or checked["capacity"] != drain
        or not math.isclose(full, fitted_dps, rel_tol=1e-10)
    ):
        raise ValueError("移槽后的配装容量或伤害不符")
    return {
        "fits": True,
        "slots": changes,
        "placement": placement,
        "limit": limit,
        "drain": drain,
        "total": len(changes),
    }


async def build_view(host, port, result, *, with_riven=False, options=None):
    """展示按目标选出的配装、单卡影响与灵化。"""
    build = result["actual" if with_riven else "baseline"]
    plan = build["forma"]
    slots = _build_slots(build, with_riven=with_riven, stance=result.get("stance"))
    lines = []
    limited = result.get("searchWork", {}).get("limited", 0) > 0
    unspecified_polarity = with_riven and result.get("unspecifiedRivenPolarity", False)
    replay_options = {"unspecified_polarity": True} if unspecified_polarity else {}
    customized = bool(
        options
        or result.get("confirmedAnalysisOptions")
        or result.get("exilus")
        or result.get("arcaneStates")
        or result.get("referenceDps")
    )
    if with_riven:
        lines.append(_card_basis_line(result))
        if not customized:
            lines.extend(comparison_lines(result))

    service = host.damage_engine
    weapon = result["weapon"]
    target = result["evaluationTarget"]
    route = build.get("incarnonChoices") or {}
    replay = {}
    original_polarities = []
    try:
        await service.ensure_portable(port)
        weapons, _ = await damage_engine.portable_catalog(service, port)
        weapon = {**weapon, **weapons[weapon["path"]]}
        request, original_polarities = _request_for_build(
            result, weapon, with_riven=with_riven, confirmed=options
        )
        if with_riven and customized and not limited and result.get("baseline"):
            baseline, _ = _request_for_build(result, weapon, confirmed=options)
            # 无卡整套保留同一份辅助装备和工况，采用该套自己的普通槽位与进化。
            for key in ("arcanes", "exilus", "specialSlotPolarities", "scenario", "stancePath"):
                if key in request:
                    baseline[key] = request[key]
                else:
                    baseline.pop(key, None)
            replay = await _mod_gains(
                service, port, request, baseline_request=baseline, **replay_options
            )
        else:
            replay = await _mod_gains(
                service,
                port,
                request,
                **({"persistent": True} if not with_riven else replay_options),
            )
    except (damage_engine.EngineError, QueueUnavailable, KeyError, TypeError, ValueError):
        # 局部回放不可用时，已确认的配装、推荐极性及进化仍然可以展示。
        pass

    if with_riven:
        gain_line = L("· 暂未算出整卡收益", "· Overall Riven comparison pending")
        if not limited and not customized:
            gain = result["actualGain"]
            gain_line = L(
                f"· 对比无紫卡配装（无超宏防护） · 预计输出 {gain:+.1f}%",
                f"· Estimated output vs a no-Riven build (no Overguard) {gain:+.1f}%",
            )
        elif not limited and (baseline := replay.get("baseline")) is not None and baseline > 0:
            gain = (replay["full"] / baseline - 1) * 100
            gain_line = L(
                f"· 同工况无紫卡配装 {baseline:,.0f} DPS · 带卡 {gain:+.1f}%",
                f"· No-Riven build under the same conditions {baseline:,.0f} DPS · With Riven {gain:+.1f}%",
            )
        elif not limited and (baseline := result.get("referenceDps", 0)) > 0 and "full" in replay:
            gain = (replay["full"] / baseline - 1) * 100
            gain_line = L(
                f"· 同工况随包无紫卡配装 {baseline:,.0f} DPS · 带卡 {gain:+.1f}%",
                f"· Bundled no-Riven build under the same conditions {baseline:,.0f} DPS · With Riven {gain:+.1f}%",
            )
        lines.insert(0, gain_line)

    context = _build_replay_options(result, build, confirmed=options)
    shown_scenario = {
        key: val for key, val in context.get("scenario", {}).items() if key != "comboTier" or val
    }
    lines[:0] = [
        scenario_line({**result, "weapon": weapon}, show_defense=False),
        "· " + _defense_line(target),
        *damage_engine.combat_condition_lines({"scenario": shown_scenario}),
    ]
    analysis_equipment = None
    if not replay:
        analysis_equipment = {
            "exilus": build.get("exilus") or result.get("exilus"),
            "arcanes": result.get("arcanes")
            or [row for row in result.get("arcaneStates", ()) if row.get("applied")],
            "stance": result.get("stance") or build.get("stance"),
        }
    lines.extend(
        _fixed_build_lines(
            target,
            slots,
            plan,
            replay,
            original_polarities,
            with_riven=with_riven,
            limited=limited,
            analysis_equipment=analysis_equipment,
            melee=bool(result.get("stance")) or _is_melee_weapon(weapon),
            unspecified_polarity=unspecified_polarity,
        )
    )
    return lines + evolution_lines(weapon, route, reference=not with_riven)


def _fixed_build_lines(
    target,
    slots,
    plan,
    replay,
    original_polarities,
    *,
    with_riven=False,
    limited=False,
    analysis_equipment=None,
    melee=False,
    bundled=False,
    unspecified_polarity=False,
):
    lines = []
    full = replay.get("full")
    without = replay.get("without", {})
    if full is not None:
        secs = target["windowSeconds"]
        defense = L("（含超宏防护）", " (with Overguard)") if replay["has_overguard"] else ""
        source = (
            L("随包参考", "Bundled reference") if bundled else L("独立引擎", "Standalone engine")
        )
        lines.append(
            L(
                f"· {source} · {secs:g} 秒平均 DPS {full:,.0f}{defense}",
                f"· {source} · Average DPS over {secs:g} s {full:,.0f}{defense}",
            )
        )
    else:
        lines.append(L("· 独立引擎 · 暂缺 DPS", "· Standalone engine · DPS unavailable"))
    lines.extend(
        damage_engine.weapon_panel_lines(replay.get("stats"), show_empty_groups=True, melee=melee)
        or [L("◆ 武器面板", "◆ Weapon stats"), L("· 暂缺面板数据", "· Panel unavailable")]
    )
    if replay.get("panel_includes_stacks"):
        lines[1] += L(" · 含预设赋能层数", " · Includes assumed Arcane stacks")
    validation = replay.get("capacity", {})
    if with_riven:
        changes = plan["slots"]
        used = plan["drain"]
        limit = plan["limit"]
        drains = {
            index: slot["drain"] for index, slot in enumerate(plan["placement"]) if slot is not None
        }
    else:
        used = validation.get("capacity")
        limit = validation.get("capacityLimit", 60)
        drains = {slot["slot"]: slot["drain"] for slot in validation.get("assignments", ())}
        changes = {
            str(index): changed
            for index, (original, changed) in enumerate(
                zip(original_polarities, replay.get("polarities", ()))
            )
            if changed != original
        }
    heading = (
        L("暂定配装 · 计算未完成", "Provisional build · Calculation incomplete")
        if limited
        else L("推荐配装", "Recommended build")
        if with_riven
        else L("无紫卡参考配装", "No-Riven reference build")
    )
    capacity = L(f"容量 {used}/{limit}", f"Capacity {used}/{limit}") if used is not None else ""
    forms = plan["total"] if with_riven else len(changes)
    equipment = replay.get("equipment") or {}
    exilus = equipment.get("exilus")
    if exilus and exilus.get("slotPolarity") not in _NO_POLARITY:
        index = 9 if melee else 8
        innate = original_polarities[index] if len(original_polarities) > index else None
        forms += int(exilus["slotPolarity"] != innate)
    if bundled:
        forms = plan["total"]
    forma = L(f"极化 {forms} 次", f"Forma ×{forms}") if with_riven or replay else ""
    lines.append("◆ " + " · ".join(part for part in (heading, capacity, forma) if part))
    lines.append(
        L(
            "　槽位　MOD　等级　容量　极性　拆下后 DPS　变化",
            "　Slot　MOD　Rank　Drain　Polarity　DPS without　Change",
        )
    )
    for index, mod in enumerate(slots):
        number = f"{index + 1:02d}"
        if mod is None:
            lines.append(f"· {number}　{L('空槽', 'Empty')}　—　—　—　—　—")
            continue
        riven = "path" not in mod
        if riven:
            name = L("你的紫卡", "Your Riven")
        elif index == replay.get("serration_slot"):
            name = L("膛线", "Serration")
        else:
            name = L(mod.get("nameZh") or mod["nameEn"], mod["nameEn"])
        used_slot = str(drains.get(index, "—"))
        polarity = mod["slotPolarity"] if bundled else changes.get(str(index))
        if with_riven and "polarity" in plan["placement"][index]:
            polarity = plan["placement"][index]["polarity"]
        elif not bundled and polarity is None and index < len(original_polarities):
            polarity = original_polarities[index]
        polarity = (
            L("紫卡极性", "Riven polarity")
            if riven and unspecified_polarity
            else _polarity_label(polarity) or "—"
        )
        dps, change = dps_removal_columns(full, without.get(index))
        lines.append(f"· {number}　{name}　{mod['rank']}　{used_slot}　{polarity}　{dps}　{change}")
    lines.extend(_equipment_lines(equipment or analysis_equipment or {}, verified=bool(replay)))
    if stacked_panel := replay.get("stacked_panel"):
        buffs = []
        for arc in replay["panel_stack_assumptions"]:
            name = L(arc.get("nameZh") or arc["nameEn"], arc["nameEn"])
            stacks = arc["stacks"]
            buffs.append(L(f"{name} {stacks:g} 层", f"{name} at {stacks:g} stacks"))
        joined = L("、", ", ").join(buffs)
        values = []
        if (chance := stacked_panel.get("statusChance")) is not None:
            values.append(L(f"触发几率 {chance:.1%}", f"status chance {chance:.1%}"))
        if (multishot := stacked_panel.get("multishot")) is not None:
            values.append(L(f"多重射击 {multishot:.2f}", f"multishot {multishot:.2f}"))
        effects = L("、", ", ").join(values)
        detail = (
            L(f"时，{effects}。", f" gives {effects}. ")
            if effects
            else L("为模拟采用的层数。", " is the assumed Arcane state. ")
        )
        lines.append(
            L(
                f"※ {joined}{detail}武器面板显示叠层前数值。",
                f"※ {joined}{detail}Weapon stats show values before stacking.",
            )
        )
    if saving := replay.get("capacity_saved"):
        lines.append(
            L(
                f"※ 同槽换用膛线，模拟 DPS 相同，少占 {saving} 点容量。",
                f"※ Serration in the same slot gives equal simulated DPS and saves {saving} capacity.",
            )
        )
    if without:
        lines.append(
            L(
                "※ 拆下这一张后的 DPS 与上方整套对比。其他 MOD、辅助装备和进化照原配装。",
                "※ Each row compares DPS with that slot empty against the full build shown above.",
            )
        )
    if len(without) < sum(mod is not None for mod in slots):
        lines.append(L("※ — 表示这张卡暂时算不出影响", "※ — means the impact is unavailable"))
    return lines


def _equipment_lines(equipment, *, verified):
    empty = L("未装备", "Empty") if verified else L("待确认", "Unconfirmed")
    exilus = equipment.get("exilus")
    utility = empty
    if exilus:
        name = L(exilus.get("nameZh") or exilus["nameEn"], exilus["nameEn"])
        set_bonus = ""
        if exilus["rank"] == 0 and (exilus.get("modSet") or exilus.get("setNameZh")):
            set_bonus = L(
                "；套装特效 0 级满收益、省极化", "; full set bonus at rank 0, saves Forma"
            )
        utility = L(
            f"{name}（{exilus['rank']}级，容量 {exilus['drain']}{set_bonus}）",
            f"{name} (rank {exilus['rank']}, drain {exilus['drain']}{set_bonus})",
        )
        if polarity := _polarity_label(exilus.get("slotPolarity")):
            utility += f" · {polarity}"
    arcs = []
    for arc in equipment.get("arcanes") or ():
        name = L(arc.get("nameZh") or arc["nameEn"], arc["nameEn"])
        arcs.append(L(f"{name}（{arc['rank']}级）", f"{name} (rank {arc['rank']})"))
    lines = [
        L("◆ 辅助装备", "◆ Additional equipment"),
        L(f"· 扩展槽　{utility}", f"· Exilus　{utility}"),
        L(f"· 赋能　{' / '.join(arcs) or empty}", f"· Arcane　{' / '.join(arcs) or empty}"),
    ]
    if stance := equipment.get("stance"):
        name = L(
            stance.get("nameZh") or stance.get("nameEn"),
            stance.get("nameEn") or stance.get("nameZh"),
        )
        name = name or stance.get("path") or empty
        if gain := stance.get("capacityGain"):
            name += L(f"（容量 +{gain}）", f" (capacity +{gain})")
        if polarity := _polarity_label(stance.get("slotPolarity")):
            name += f" · {polarity}"
        lines.append(L(f"· 架势　{name}", f"· Stance　{name}"))
    return lines


@lru_cache(maxsize=1)
def _comparison_catalog():
    try:
        path = Path(__file__).resolve().parent / "data" / "riven_reroll_comparisons.json"
        raw = json.loads(path.read_bytes())
        if raw["engineFingerprint"] == FINGERPRINT and raw["metric"] == "windowedDps":
            return {
                (entry["weapon"], entry["fireMode"], entry["contextHash"]): entry
                for entry in raw["entries"]
            }
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return {}


def prepared_replay_request(result, options):
    """按预计算底表的实际槽位重建请求，再叠加本次指定的辅装和工况。"""
    if not isinstance(
        options, dict
    ) or options.keys() - damage_engine.PortableReplayOptions.__optional_keys__ - {
        "benchmarkTarget"
    }:
        raise damage_engine.EngineError(
            L("引擎可选参数与既定工况不符", "Engine options conflict with the request")
        )
    key = (result["weapon"]["path"], result["fireMode"], result["contextHash"])
    view = _comparison_catalog().get(key, {}).get("noRivenBuild")
    if view is None:
        return None
    if options.get("exilus") == {"mode": "auto"}:
        return None
    request = view.get("request")
    if request is None:
        request = build_replay_request(
            {**result, "evaluationTarget": view["target"]},
            view["weapon"]["path"],
            view["slots"],
            view["replay"]["polarities"],
            view["route"],
        )
    if "benchmarkTarget" in options:
        target = options["benchmarkTarget"]
        damage_engine.validate_benchmark_target(target)
        request = {**request, **target}
    options = {key: value for key, value in options.items() if key != "benchmarkTarget"}
    if isinstance(options.get("scenario"), dict):
        request = dict(request)
        scenario = request.pop("scenario", {})
        options = {**options, "scenario": {**scenario, **options["scenario"]}}
    request = damage_engine.with_portable_replay_options(request, options)
    return damage_engine.with_gauge_charging_headshots(request)


def prepared_traits(result, *, replay_request=None):
    """新工况可指定原固定配装请求，仅读取具有相同请求指纹的预计算。"""
    if result.get("engineFingerprint") != FINGERPRINT or result.get("scoreMetric") != "windowedDps":
        return None
    try:
        key = (result["weapon"]["path"], result["fireMode"], result["contextHash"])
        reference = _comparison_catalog()[key]["fixedTraitReference"]
        if replay_request is not None:
            required = {
                "weapon",
                "fireMode",
                "enemy",
                "level",
                "steelPath",
                "headshotChance",
                "windowSeconds",
                "metric",
                "damageModel",
                "mods",
                "slotPolarities",
                "enemyEnhancements",
                "armourStrip",
                "elementalArmourStrip",
                "incarnonChoices",
            }
            if (
                not required <= replay_request.keys()
                or (replay_request["weapon"], replay_request["fireMode"]) != key[:2]
                or reference.get("replayContextHash")
                != damage_engine.portable_replay_fingerprint(replay_request)
            ):
                return None
        baseline = reference["baselineScore"]
        if type(baseline) not in (float, int) or not math.isfinite(baseline) or baseline <= 0:
            return None
        scenario = result.get("evaluationScenario") or {}
        cycle = bool(scenario.get("incarnonCycle"))
        mode_meta = _fire_mode(result["weapon"], result["fireMode"])
        title = _scenario_mode_label(result)
        rows = {"positive": [], "negative": []}
        for line in reference["rows"]:
            sid = TRAIT_IDS[line["code"]]
            negative = line["negative"]
            inverted = sid == "weapon_recoil"
            signed = -line["value"] if negative != inverted else line["value"]
            if sid.startswith("damage_to_"):
                shown, unit = 1 + signed / 100, "x"
            else:
                shown, unit = signed, line["unit"]
            flags = riven_rating._TRAIT_SPLICED if line["fusion"] else 0
            score = line.get("score")
            change = None
            if type(score) in (float, int) and math.isfinite(score) and score >= 0:
                change = (score / baseline - 1) * 100
                if not math.isfinite(change):
                    change = None
            if change is None and (score is not None or line.get("pct") is not None):
                flags |= riven_rating._TRAIT_REPLAY_REQUIRED
            direction = "negative" if negative else "positive"
            rows[direction].append((sid, shown, unit, flags, None, change, None, None))
    except (KeyError, OverflowError, TypeError, ValueError):
        return None
    return title, {
        **rows,
        "incarnon_form": not cycle and bool(mode_meta.get("isIncarnonForm")),
        "incarnon_cycle": cycle,
    }


def comparison_lines(result):
    """展示与本次完整模拟同工况的离线候选，沿用实时计算的无卡与实卡分数。"""
    if (
        result.get("engineFingerprint") != FINGERPRINT
        or result.get("scoreMetric") != "windowedDps"
        or result.get("searchWork", {}).get("limited") != 0
    ):
        return []
    try:
        key = (result["weapon"]["path"], result["fireMode"], result["contextHash"])
        entry = _comparison_catalog().get(key)
        if entry is None:
            return []
        scores = result["scoringValues"]
        base, actual, candidate = scores["baseline"], scores["actual"], entry["score"]
        if any(
            type(score) not in (float, int) or not math.isfinite(score) or score < 0
            for score in (base, actual, candidate)
        ):
            return []
        traits = []
        for line in entry["stats"]:
            sid = TRAIT_IDS[line["code"]]
            label = riven_rating.stat_label(sid, melee=sid in MELEE_CODES)
            value = line["value"]
            direction = L("负", "−") if line["negative"] else L("正", "+")
            if sid.startswith("damage_to_"):
                multiplier = 1 + (-1 if line["negative"] else 1) * value / 100
                shown = f"×{multiplier:.2f}"
            else:
                unit = {
                    "punch_through": "m",
                    "range": "m",
                    "combo_duration": "s",
                    "initial_combo": "",
                    "parry_angle": "°",
                }.get(sid, "%")
                shown = f"{value:.1f}{unit}"
            traits.append(f"{direction} {label} {shown}")
    except (KeyError, TypeError, ValueError, OverflowError):
        return []
    return [
        L("◆ 同工况输出对照", "◆ Same-scenario comparison"),
        L(
            f"· 无紫卡 {base:,.0f} / 当前紫卡 {actual:,.0f} / 参考紫卡 {candidate:,.0f} DPS",
            f"· No Riven {base:,.0f} / Current Riven {actual:,.0f} / Reference Riven {candidate:,.0f} DPS",
        ),
        L("· 候选词条（模拟满值） ", "· Candidate traits (modeled max rolls) ")
        + " / ".join(traits),
    ]


def _recommendation_label(label):
    sid = _STAT_LOOKUP.get(label.casefold())
    return riven_rating.stat_label(sid, full=True) if sid else label


def trait_lines(result):
    groups = {"rollable": [], "fusion": []}
    for tier in result["recommendedPositiveTiers"]:
        for row in tier["stats"]:
            label = _recommendation_label(row["label"])
            gain = row["gain"]
            group = (
                "fusion"
                if row["code"] in FUSION_CODES.values()
                or row["code"] in MELEE_FUSION_CODES.values()
                else "rollable"
            )
            color = "v" if group == "fusion" else "y"
            groups[group].append(f"⟦w⟧{label}⟦/w⟧ ⟦{color}⟧{gain:+.1f}%⟦/{color}⟧")

    lines = [L("◆ 洗卡优先看", "◆ Reroll priorities")]
    for group, title in (
        ("rollable", L("可洗正词条", "Rollable bonuses")),
        ("fusion", L("融合选项", "Fusion options")),
    ):
        picks = groups[group]
        for start in range(0, len(picks), 3):
            label = f"⟦w⟧{title}⟦/w⟧" if not start else ""
            lines.append(f"· {label}　" + " / ".join(picks[start : start + 3]))

    negatives = [
        label
        for label in result["recommendedNegative"]
        if _STAT_LOOKUP.get(label.casefold()) not in riven_rating._CONTEXTUAL_MALUSES
    ]
    if negatives:
        names = " / ".join(map(_recommendation_label, negatives))
        lines.append(L(f"· 低影响负面　{names}", f"· Low-impact maluses　{names}"))
    thresholds = result["fatalNegativeAbove"]
    warnings = [label for label in result["fatalNegative"] if label not in thresholds]
    if warnings:
        names = " / ".join(map(_recommendation_label, warnings))
        lines.append(L(f"· 高风险负面　{names}", f"· High-risk maluses　{names}"))
    for label, threshold in thresholds.items():
        name = _recommendation_label(label)
        lines.append(
            L(
                f"· 高风险负面　{name}减幅超过 ⟦m⟧{threshold:g}%⟦/m⟧",
                f"· High-risk malus　{name} reduced by more than ⟦m⟧{threshold:g}%⟦/m⟧",
            )
        )
    if len(lines) == 1:
        lines.clear()
    if any(groups.values()):
        lines.append(
            L(
                "※ 建议收益按词条重配后试算。",
                "※ Suggested gains use builds reoptimized for each trait.",
            )
        )
    scenario = result.get("evaluationScenario") or {}
    if scenario.get("incarnonCycle") and result.get("weapon", {}).get("category") != "Melee":
        lines.append(
            L(
                "· 弹匣容量、装填速度、弹药效率、弹药最大值 ⟦v⟧仅充能期⟦/v⟧",
                "· Magazine Capacity, Reload Speed, Ammo Efficiency, Ammo Maximum ⟦v⟧Charge only⟦/v⟧",
            )
        )
    return lines
