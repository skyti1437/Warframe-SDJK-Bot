"""紫卡武器识别、词条显示与实卡输入。"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import NamedTuple

from . import damage_calc, damage_engine, formatters, matching, riven_analysis
from .i18n import L, is_en

_DATA = Path(__file__).resolve().parent / "data"
_TRAIT_MODELED = 1
_TRAIT_UNCONFIRMED = 2
_TRAIT_SPLICED = 4
_TRAIT_INELIGIBLE = 16
_TRAIT_REPLAY_REQUIRED = 32
_TRAIT_NEUTRAL = 64
_TRAIT_WEAKPOINT = 128
_TRAIT_REOPTIMIZED = 256
_ELEMENT_BUILD_LABELS = {
    "corrosive": ("腐蚀套", "Corrosive"),
    "viral": ("病毒套", "Viral"),
    "heat": ("火焰套", "Heat"),
    "blast": ("爆炸套", "Blast"),
    "gas": ("毒气套", "Gas"),
    "magnetic": ("磁力套", "Magnetic"),
    "radiation": ("辐射套", "Radiation"),
    "electricity": ("纯电套", "Electricity"),
    "cold": ("纯冰套", "Cold"),
    "toxin": ("纯毒套", "Toxin"),
}
_RESERVE_AMMO_STATS = {"ammo_max", "ammo_maximum"}
_INCARNON_AMMO_STATS = {
    "magazine_capacity",
    "reload_speed",
    "ammo_efficiency",
} | _RESERVE_AMMO_STATS
_CONTEXTUAL_MALUSES = {"recoil", "weapon_recoil", "projectile_speed"} | _RESERVE_AMMO_STATS

MODES = {
    alias: mode
    for mode, aliases in {
        "base": (
            "主射",
            "主要",
            "主要射击",
            "主形态",
            "普通形态",
            "常态",
            "原型",
            "基础",
            "基础形态",
            "平砍",
            "平a",
            "轻击",
            "base",
            "primary",
            "pri",
            "neutral",
        ),
        "incarnon": ("灵化形态", "灵化模式", "纯灵化", "incarnon", "inc"),
        "alternate": (
            "副射",
            "次要",
            "次要射击",
            "副开火",
            "副形态",
            "alternate",
            "alt",
            "secondary",
            "sec",
        ),
        "heavy": ("重击", "重攻击", "重砍", "heavy"),
        "slam": ("震地", "重击震地", "下砸", "slam", "heavyslam"),
        "cycle": ("灵化", "灵化循环", "循环", "cycle"),
    }.items()
    for alias in aliases
}
MODE_LABELS = {
    "base": ("常态", "Base"),
    "alternate": ("副射", "Alternate fire"),
    "heavy": ("重击", "Heavy attack"),
    "slam": ("重击震地", "Heavy slam"),
    "cycle": ("灵化循环", "Incarnon cycle"),
    "incarnon": ("灵化形态", "Incarnon form"),
}
MELEE_BASE_LABEL = ("平砍", "Base")
POLARITIES = {
    "madurai": "madurai",
    "v": "madurai",
    "vazarin": "vazarin",
    "d": "vazarin",
    "naramon": "naramon",
    "-": "naramon",
}
_STAT = {
    "crit_chance": "critical_chance",
    "crit_damage": "critical_damage",
    "recoil": "weapon_recoil",
    "ammo_max": "ammo_maximum",
    "slide_crit": "critical_chance_for_slide_attack",
    "weakpoint_damage": "weak_point_damage",
    "weakpoint_crit_chance": "weak_point_critical_chance",
    "reload_holstered": "reload_while_holstered",
    "melee_heavy_attack_damage": "melee_damage_on_heavy_attack",
    "melee_heavy_attack_charge": "heavy_attack_wind_up_speed",
    "melee_parry_angle": "parry_angle",
    "melee_slam_damage": "slam_attack_damage",
    "combo_gain_chance": "chance_to_gain_combo_count",
    "extra_combo_count": "additional_combo_count_chance",
    "electric_damage": "electricity",
    **{
        f"{kind}_damage": kind
        for kind in (
            "blast",
            "cold",
            "corrosive",
            "gas",
            "heat",
            "impact",
            "magnetic",
            "puncture",
            "radiation",
            "slash",
            "toxin",
            "viral",
        )
    },
}
_ENGINE_STAT = {engine: plugin for plugin, engine in _STAT.items()}


def melee_route(scenario):
    return ((scenario or {}).get("combatState") or {}).get("meleeRoute")


def incarnon_cycle_label(weapon, scenario, headshot_chance):
    # 实时目录没有充能类型字段。当前灵化资料中，托里德是唯一的直接命中充能武器。
    if (
        headshot_chance < 1.0
        and (scenario.get("combatState") or {}).get("gaugeChargingHeadshots") is True
        and weapon.get("chargeOn") not in {"direct_hits", "kills"}
        and weapon.get("path") != "/Lotus/Weapons/ClanTech/Bio/BioWeapon"
    ):
        return L("灵化循环（攒条全弱点）", "Incarnon Cycle (weakpoint charge)")
    return L(*MODE_LABELS["cycle"])


def melee_scenario_mode_label(scenario):
    route = melee_route(scenario)
    if route == "glaive":
        return L("重击投掷", "Glaive throw")
    if route in {"slam", "slamBullet", "slamContinuous"}:
        return L(*MODE_LABELS["slam"])
    if scenario.get("heavyAttack"):
        return L(*MODE_LABELS["heavy"])
    if route == "combo":
        return L(*MELEE_BASE_LABEL)
    return None


@lru_cache(maxsize=1)
def display_names() -> dict:
    try:
        data = json.loads((_DATA / "portable_display_names.json").read_bytes())
        if data["source"]["fingerprint"] == damage_engine.PORTABLE_FINGERPRINT:
            return data
    except (OSError, TypeError, ValueError, KeyError):
        pass
    return {}


@lru_cache(maxsize=1)
def _weapon_index() -> dict:
    return json.loads((_DATA / "riven_weapon_index.json").read_bytes())


def physical_evidence(family: str, *, positive: bool) -> dict[str, bool]:
    evidence = _weapon_index()["range_evidence"].get(family.lower(), {})
    return evidence.get("positive" if positive else "negative", {})


@lru_cache(maxsize=1)
def _weapon_names() -> dict:
    index = {}
    for wid, row in _weapon_index()["weapons"].items():
        weapon = {key: row[key] for key in ("name", "zh", "category")} | {"engine_id": wid}
        weapon["label"] = row.get("label", (row["zh"], row["name"]))
        for alias in dict.fromkeys((wid, row["name"], row["zh"], *row.get("aliases", ()))):
            if key := matching.normalize(alias):
                choices = index.setdefault(key, {})
                choices[wid] = weapon
    return {key: tuple(choices.values()) for key, choices in index.items()}


def match_weapon(query: str) -> tuple[dict, ...]:
    names = _weapon_names()
    for key in dict.fromkeys((matching.normalize(query), *matching.expand_variants(query))):
        if key in names:
            return names[key]
    weapon = damage_calc.find_weapon_exact(query)
    return names.get(matching.normalize(weapon["name"]), ()) if weapon else ()


def match_portable_weapon(query, catalog):
    key = matching.normalize(query)
    if not key:
        return ()
    matches = [
        row
        for row in catalog.values()
        if key
        in {matching.normalize(row.get(field) or "") for field in ("path", "nameEn", "nameZh")}
        and row.get("nameEn")
    ]
    return tuple(
        {
            "engine_id": row["path"],
            "name": row["nameEn"],
            "zh": row.get("nameZh"),
            "category": {"melee": "Melee", "zaw": "Melee"}.get(row.get("weaponType"), ""),
        }
        for row in matches
    )


def card_weapon_name(title: str) -> str:
    """按精确武器名核对 OCR 标题，允许末尾带卡片拉丁词缀。"""
    title = title.strip(" ·?？")
    if match_weapon(title):
        return title
    prefix, _, suffix = title.rpartition(" ")
    clean = "".join(suffix.split("-"))
    return prefix if clean.isascii() and clean.isalpha() and match_weapon(prefix) else ""


def related_weapons(choices: tuple[dict, ...]) -> tuple[dict, ...]:
    ids = {choice["engine_id"] for choice in choices}
    families = {matching.family_key(choice["name"]) for choice in choices} - {""}
    ids.update(
        wid
        for wid, row in _weapon_index()["weapons"].items()
        if matching.family_key(row["name"]) in families
    )
    names = _weapon_names()
    return tuple(
        row
        for wid in sorted(ids)
        for row in names[matching.normalize(wid)]
        if row["engine_id"] == wid
    )


@lru_cache(maxsize=1)
def _historical_dispositions():
    try:
        return json.loads((_DATA / "dispositions_rivenmirror.json").read_bytes())["entries"]
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def rank_context(weapon):
    name = weapon["name"]
    entries = _historical_dispositions()
    family = matching.family_key(name)
    history = tuple(
        row["disposition"]
        for variant, row in entries.items()
        if matching.family_key(variant) == family and row.get("disposition")
    )
    identity = entries.get(name, {})
    stats = damage_calc.find_weapon_exact(name) or {}
    return {
        "cls": riven_analysis.weapon_class(
            identity.get("riven_type") or stats.get("type", ""),
            identity.get("group") or weapon["category"],
        ),
        "historical_dispositions": history,
        "stat_multipliers": riven_analysis.weapon_stat_multipliers(name),
    }


def weapon_label(weapon: dict) -> str:
    return L(*weapon.get("label", (weapon.get("zh") or weapon["name"], weapon["name"])))


def stat_label(sid: str, melee: bool = False, *, full: bool = False) -> str:
    plugin = _ENGINE_STAT.get(sid, sid)
    if melee and plugin in {"fire_rate", "attack_speed"}:
        return damage_calc.attack_rate_label(melee)
    if sid == "damage":
        plugin = "melee_damage"
    elif sid.startswith("damage_to_"):
        plugin = sid.replace("damage_to_", "damage_vs_", 1)
    if full and not is_en():
        names = {
            "melee_damage": "近战伤害" if melee else "基础伤害",
            "crit_damage": "暴击伤害",
            "status_duration": "触发持续时间",
            "projectile_speed": "投射物速度",
            "reload_holstered": "收枪装填",
            "slide_crit": "滑行攻击暴击几率",
        }
        full_name = names.get(plugin) or formatters._splice_full(plugin)
        if full_name:
            return full_name
    return formatters._stat_name(plugin, "melee" if melee else "rifle")


def _trait_amount(stat: str, value: float, unit: str) -> str:
    if unit == "x":
        return f"×{value:.2f}"
    if unit == "percent":
        unit = "%"
    elif unit in ("", "flat"):
        unit = {
            "range": " m",
            "punch_through": " m",
            "combo_duration": " s",
            "parry_angle": "°",
        }.get(stat, "")
    if unit in {"m", "s"}:
        unit = " " + unit
    if unit == " m" and not is_en():
        unit = " 米"
    elif unit == " s" and not is_en():
        unit = " 秒"
    return f"{value:+.1f}{unit}"


class _TraitResult(NamedTuple):
    stat: str
    shown: float
    unit: str
    flags: int
    kpm: float | None
    pct: float | None
    low: float | None
    high: float | None
    elem_build: str | None = None


def _contextual_trait(stat, change, *, positive, scene):
    cycle = scene.get("incarnon_cycle")
    if not cycle and scene.get("incarnon_form") and stat in _INCARNON_AMMO_STATS:
        return L("灵化无效", "Incarnon N/A"), "n"
    if change is not None and abs(change) >= 0.05:
        return None
    if stat in {"recoil", "weapon_recoil"}:
        return L("看手感", "Handling"), "v"
    if stat == "projectile_speed":
        return L("看弹道/衰减", "Ballistics"), "v"
    if stat in _RESERVE_AMMO_STATS:
        if not positive and scene.get("low_ammo"):
            return L("影响续航", "Low ammo"), "m"
        return (L("仅充能期", "Charge only") if cycle else L("看续航", "Reserve")), "v"
    return None


def trait_matrix(
    weapon: dict, complete: dict, *, bundled_roll=None, bundled_variant="default", live=False
) -> list[str]:
    """同一条词条只列一次，按模式显示固定配装的变化。"""
    from .portable_riven import _is_melee_weapon, _melee_incarnon_tier1_notes

    melee = _is_melee_weapon(weapon)
    labels = {}
    rows: dict[tuple[bool, str], dict[str, tuple[_TraitResult, bool]]] = {}
    for mode, (label, scene) in complete.items():
        labels[mode] = L(*MODE_LABELS["cycle"]) if scene.get("incarnon_cycle") else label
        for positive, direction in ((True, "positive"), (False, "negative")):
            for values in scene[direction]:
                trait = _TraitResult(*values)
                if trait.flags & _TRAIT_INELIGIBLE:
                    continue
                uncertain = bool(trait.flags & _TRAIT_UNCONFIRMED)
                rows.setdefault((positive, trait.stat), {})[mode] = (trait, uncertain)
    if not rows:
        return []

    has_weakpoint = any(
        trait.flags & _TRAIT_WEAKPOINT
        for measurements in rows.values()
        for trait, _ in measurements.values()
    )
    multiple = len(labels) > 1
    sections: list[list[str]] = [[], [], [], []]
    for (positive, stat), measurements in rows.items():
        name = stat_label(stat, melee, full=True)
        element = stat.removesuffix("_damage")
        elemental = element in _ELEMENT_BUILD_LABELS or element == "electric"
        fusion = any(trait.flags & _TRAIT_SPLICED for trait, _ in measurements.values())
        uncertain = all(flag for _, flag in measurements.values())
        group = 3 if uncertain else 2 if fusion else 0 if positive else 1
        amounts = {
            mode: _trait_amount(stat, trait.shown, trait.unit)
            for mode, (trait, _) in measurements.items()
        }
        same_amount = len(set(amounts.values())) == 1
        value = next(iter(amounts.values())) if same_amount else L("见各模式", "See modes")
        if uncertain:
            name = ("+ " if positive else "− ") + name
        columns = [f"⟦w⟧{name}⟦/w⟧", f"⟦n⟧{value}⟦/n⟧"]
        for mode in labels:
            if mode not in measurements:
                columns.append("⟦n⟧—⟦/n⟧")
                continue
            trait, flag = measurements[mode]
            scene = complete[mode][1]
            change = trait.pct
            high = change if trait.high is None else trait.high
            note, note_color = "", "n"
            effect = "⟦n⟧—⟦/n⟧"
            if flag:
                note = L("资格待确认", "Unconfirmed")
            elif not bundled_roll and stat.startswith("damage_to_"):
                note = L("按派系", "Faction")
            elif trait.flags & _TRAIT_REPLAY_REQUIRED:
                note = L("待复算", "Recalculation pending")
            elif contextual := _contextual_trait(stat, change, positive=positive, scene=scene):
                note, note_color = contextual
            elif change is None or stat == "reload_while_holstered":
                note = L("未模拟", "Unmodeled")
            else:
                low = change if trait.low is None else trait.low
                element_note = (
                    _ELEMENT_BUILD_LABELS.get(trait.elem_build)
                    if positive and elemental and change >= -0.05
                    else None
                )
                if element_note:
                    effect = f"⟦y⟧{change:+.1f}%⟦/y⟧" if change >= 0.05 else "⟦n⟧0.0%⟦/n⟧"
                elif not (positive and elemental and change < -0.05):
                    color = "n" if abs(change) < 0.05 else "y" if change > 0 else "m"
                    effect = (
                        f"⟦{color}⟧{abs(change):.1f}%⟦/{color}⟧"
                        if color == "n"
                        else f"⟦{color}⟧{change:+.1f}%⟦/{color}⟧"
                    )
                if element_note:
                    note, note_color = L(*element_note), "v"
                elif stat.startswith("damage_to_"):
                    note = L("按派系", "Faction")
                elif trait.flags & _TRAIT_WEAKPOINT:
                    note, note_color = L("弱点命中", "Weakpoint"), "v"
                elif trait.flags & _TRAIT_REOPTIMIZED or elemental or fusion:
                    note, note_color = L("看组合", "Build-specific"), "v"
                elif not positive and high <= -25:
                    note, note_color = L("高风险", "High risk"), "m"
                elif not positive and (trait.flags & _TRAIT_NEUTRAL or low > -3 and high < 3):
                    note = L("低影响", "Low impact")
                elif not positive:
                    note = L("需权衡", "Trade-off")
            if (
                scene.get("incarnon_cycle")
                and stat in _INCARNON_AMMO_STATS
                and stat not in _RESERVE_AMMO_STATS
                and not flag
                and not trait.flags & _TRAIT_REPLAY_REQUIRED
            ):
                note, note_color = (
                    (L("高风险", "High risk"), "m")
                    if not positive and high is not None and high <= -25
                    else (L("仅充能期", "Charge only"), "v")
                )
            if not same_amount:
                effect += f" ⟦n⟧({amounts[mode]})⟦/n⟧"
            annotation = f"⟦{note_color}⟧{note}⟦/{note_color}⟧" if note else ""
            if multiple:
                columns.append(" ".join((effect, annotation)).strip())
            else:
                columns.extend((effect, annotation or "⟦n⟧—⟦/n⟧"))
        sections[group].append("· " + "　".join(columns))

    headings = [L("词条", "Trait"), L("牌面中位值", "Median roll")]
    metric = "KPM" if bundled_roll else "DPS"
    if multiple:
        headings.extend(f"{label} {metric}" for label in labels.values())
    else:
        headings.extend((L(f"{metric} 变化", f"{metric} change"), L("备注", "Notes")))
    if bundled_roll:
        roll = bundled_roll.replace("-", "−")
        hint = L(
            f"※ 按满级 {roll} 中位值试算；「XX套」为对应元素配装同套对照。",
            f"※ Median max-rank {roll} rolls; named element tags use a matching no-Riven build.",
        )
        if bundled_variant == "overguard":
            hint += (
                L("可加「2+1-」「护盾」，", ' Add "2+1-" or "shield".')
                if bundled_roll == "3+1-"
                else L("默认看 3+1−，", " The default is 3+1−.")
            ) + L("去「超宏」看默认。", ' Omit "overguard" for the default target.')
        elif bundled_variant == "shield":
            hint += (
                L("可加「2+1-」「超宏」，", ' Add "2+1-" or "overguard".')
                if bundled_roll == "3+1-"
                else L("默认看 3+1−，", " The default is 3+1−.")
            ) + L("去「护盾」看默认。", ' Omit "shield" for the default target.')
        else:
            hint += (
                L("可加「2+1-」「超宏」「护盾」。", ' Add "2+1-", "overguard", or "shield".')
                if bundled_roll == "3+1-"
                else L(
                    "默认看 3+1−，可加「超宏」「护盾」。",
                    ' The default is 3+1−. Add "overguard" or "shield".',
                )
            )
        lines = [
            L("◆ 实时参考配装 · 全词条", "◆ Live reference builds · All traits")
            if live
            else L("◆ 随包参考配装 · 全词条", "◆ Bundled reference builds · All traits"),
            hint,
        ]
        if has_weakpoint:
            lines.append(
                L(
                    "※ 弱点边界：仅「弱点伤害/弱点暴击几率」按同套配装 100% 弱点命中单独试算，"
                    "代表满弱点上限，不与躯干词条直比；不含弱点专属换卡/赋能，无弱点攻击（如范围爆炸）不生效。",
                    "※ Weakpoint scope: only Weak Point Damage/Critical Chance use 100% weakpoint hits "
                    "on the same build (upper bound; not directly comparable to body-hit traits); "
                    "excludes weakpoint mod/arcane swaps, and hits without weakpoints (e.g., AoE blasts) gain 0%.",
                )
            )
        lines.append(L("※ KPM 为每分钟击杀数。", "※ KPM means kills per minute."))
    else:
        lines = [
            L("◆ 全词条对照", "◆ All traits"),
            L(
                "※ 固定配装，按满级三正一负中位值试算。",
                "※ Fixed build with median max-rank 3+/1− rolls.",
            ),
        ]
    lines.extend(_melee_incarnon_tier1_notes(weapon))
    for (heading, color), group in zip(
        (
            (L("正词条", "Bonuses"), "y"),
            (L("负词条", "Maluses"), "m"),
            (L("融合词条", "Fusion traits"), "v"),
            (L("可洗资格待确认", "Roll eligibility unconfirmed"), "n"),
        ),
        sections,
    ):
        if group:
            lines.extend(
                (f"◆ ⟦{color}⟧{heading}⟦/{color}⟧ · {len(group)}", "　".join(headings), *group)
            )
    return lines


def typed_card(weapon, positive, negative, rank, polarity, *, faction_magnitudes=False):
    if len(positive) not in {2, 3} or len(negative) > 1:
        raise damage_engine.EngineError(
            L("请填完整的 2 或 3 条正面和至多 1 条负面", "Enter 2 or 3 bonuses and at most 1 malus")
        )

    def line(sid, magnitude, penalty):
        sign = -1 if penalty != riven_analysis.is_inverted(sid) else 1
        if sid.startswith("damage_vs_"):
            stat = sid.replace("damage_vs_", "damage_to_", 1)
            value = (
                1 + sign * magnitude / 100 if faction_magnitudes or magnitude >= 3 else magnitude
            )
            return {"id": stat, "value": value}
        stat = _STAT.get(sid, sid)
        if sid == "melee_damage" and weapon["category"] != "Melee":
            stat = "damage"
        return {"id": stat, "value": sign * magnitude}

    return {
        "weapon": weapon["engine_id"],
        "rank": rank,
        "polarity": polarity,
        "bonuses": [line(sid, value, False) for sid, value in positive],
        "malus": line(*negative[0], True) if negative else None,
    }


class CardError(damage_engine.EngineError):
    """紫卡指令参数不完整或无效。"""
