"""用独立引擎复算紫卡上卡收益。战斗请求只取实卡和参考配装。"""

from __future__ import annotations

import json
import math
from functools import lru_cache
from hashlib import sha256
from pathlib import Path
from statistics import mean, stdev
from typing import NamedTuple

from . import damage_calc, damage_engine, formatters, matching, riven_analysis, search
from .i18n import L, is_en

CARD = "riven:observed"
SCREEN_SEED = 40515273
RUNS = 1024
SEED = 47162621
_DATA = Path(__file__).resolve().parent / "data"
_REFERENCE = _DATA / "riven_rating_presets.json"
_TRAIT_MODELED = 1
_TRAIT_UNCONFIRMED = 2
_TRAIT_SPLICED = 4
_TRAIT_SAME_SCORE = 8
_TRAIT_INELIGIBLE = 16
_TRAIT_REPLAY_REQUIRED = 32
SEARCH_EVALS = 1600
SEARCH_FINALISTS = 4

MODES = {
    "主射": "base",
    "平砍": "base",
    "常态": "base",
    "副射": "alternate",
    "重击": "heavy",
    "灵化循环": "cycle",
    "base": "base",
    "alternate": "alternate",
    "heavy": "heavy",
    "cycle": "cycle",
}
MODE_LABELS = {
    "base": ("常态", "Base"),
    "alternate": ("副射", "Alternate fire"),
    "heavy": ("重击", "Heavy attack"),
    "cycle": ("灵化循环", "Incarnon cycle"),
    "incarnon": ("灵化形态", "Incarnon form"),
}
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


@lru_cache(maxsize=1)
def _catalog() -> dict:
    return json.loads(_REFERENCE.read_bytes())


@lru_cache(maxsize=1)
def presets() -> dict:
    catalog = _catalog()
    defaults = catalog["battle_defaults"]
    return {
        wid: {
            mode: {
                "fight": {**defaults, **row["fight"], "weapon": wid, "mode": mode},
                "sample": {"weapon": wid, **row.get("observed", weapon["sample"])},
                "card_origin": "observed" if "observed" in row else "sample",
                "rebuild": row["rebuild"],
                "trait_checks": row.get("trait_checks", ()),
            }
            for mode, row in weapon["modes"].items()
        }
        for wid, weapon in catalog["weapons"].items()
    }


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
def _weapon_names() -> dict:
    index = {}
    for wid, row in _catalog()["weapons"].items():
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
        for wid, row in _catalog()["weapons"].items()
        if matching.family_key(row["name"]) in families
    )
    names = _weapon_names()
    return tuple(
        row
        for wid in sorted(ids)
        for row in names[matching.normalize(wid)]
        if row["engine_id"] == wid
    )


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


@lru_cache(maxsize=1)
def _trait_catalog() -> dict:
    """固定参考配装变化后，须重新生成对应数据。"""
    try:
        data = json.loads((_DATA / "riven_trait_recommendations.json").read_bytes())
        if (
            data.get("schema") == 1
            and data.get("reference_sha256") == sha256(_REFERENCE.read_bytes()).hexdigest()
        ):
            return data
    except (OSError, ValueError, AttributeError):
        pass
    return {}


@lru_cache(maxsize=1)
def _build_details() -> dict:
    """只有与随包参考配装同版的复算数据才用于回图。"""
    try:
        data = json.loads((_DATA / "riven_build_details.json").read_bytes())
        if data["referenceSha256"] == sha256(_REFERENCE.read_bytes()).hexdigest():
            return data["weapons"]
    except (OSError, ValueError, TypeError, KeyError):
        pass
    return {}


def _mode_label(mode: str, melee: bool, evolutions: list | None = None) -> str:
    if mode == "cycle" and evolutions is not None:
        return (
            L("灵化循环（含普通形态充能）", "Incarnon cycle with charging")
            if evolutions
            else L("充能与释放循环", "Charge and release cycle")
        )
    return L("平砍", "Neutral combo") if mode == "base" and melee else L(*MODE_LABELS[mode])


def trait_recommendation_lines(weapon: dict, mode: str) -> list[str]:
    catalog = _trait_catalog()
    wid = weapon.get("engine_id")
    scene = catalog.get("weapons", {}).get(wid, {}).get(mode)
    if scene is None:
        return []

    fight = presets()[wid][mode]["fight"]
    melee = weapon["category"] == "Melee"
    name = _mode_label(mode, melee, fight["evolutions"])
    distance = math.dist(fight["player_at"], fight["target_at"])
    baseline = scene["baseline_kpm"]
    baseline_text = f"{baseline:.2f}" if baseline >= 0.01 else f"{baseline:.2g}"
    out = [
        L("◆ 单词条模拟推荐", "◆ Simulated trait recommendations"),
        L(
            f"· {name} · {fight['level']} 级钢铁之路堕落重型机枪手 · {distance:g} 米 · {fight['duration']:g} 秒",
            f"· {name} · Lv {fight['level']} Steel Path Corrupted Heavy Gunner · {distance:g} m · {fight['duration']:g} s",
        ),
        L(
            f"· 随包参考 {baseline_text} KPM",
            f"· Bundled reference {baseline_text} KPM",
        ),
    ]

    def render(row: _TraitResult) -> str:
        label = stat_label(row.stat, melee, full=True)
        change = "≈0%" if abs(row.pct) < 0.05 else f"{row.pct:+.1f}%"
        tint = "n" if abs(row.pct) < 0.05 else "y"
        return f"⟦w⟧{label}⟦/w⟧ ⟦{tint}⟧{change}⟦/{tint}⟧"

    positive = [_TraitResult(*row) for row in scene["positive"]]
    negative = [_TraitResult(*row) for row in scene["negative"]]
    market_samples = catalog.get("roll_pool_audit", {}).get("market_samples", {})
    few_samples = {
        stat: count
        for stat, count in market_samples.get("by_weapon", {}).get(wid, {}).items()
        if count < market_samples["min_cards"]
    }

    def confirmed(rows: list[_TraitResult]) -> list[_TraitResult]:
        return [
            row
            for row in rows
            if row.low is not None
            and row.pct is not None
            and not row.flags & (_TRAIT_UNCONFIRMED | _TRAIT_INELIGIBLE)
            and row.stat not in few_samples
            # 收枪装填留待实机验证。
            and row.stat != "reload_while_holstered"
            # 未命中目标派系的零收益属于场景限制，避免挤掉其它低损候选。
            and not (row.stat.startswith("damage_to_") and row.flags & _TRAIT_SAME_SCORE)
        ]

    beneficial = sorted(
        (row for row in confirmed(positive) if row.low > 0),
        key=lambda row: row.pct,
        reverse=True,
    )
    near_zero = sorted(
        (row for row in confirmed(negative) if row.low > -3 and row.high < 3),
        key=lambda row: abs(row.pct),
    )
    for title, picks in (
        (
            L("可洗正词条", "Rollable bonuses"),
            [row for row in beneficial if not row.flags & _TRAIT_SPLICED][:3],
        ),
        (
            L("融合参考", "Splice option"),
            [row for row in beneficial if row.flags & _TRAIT_SPLICED][:1],
        ),
        (L("本场近零负词条", "Near-zero maluses here"), near_zero[:3]),
    ):
        if picks:
            out.append(f"· ⟦w⟧{title}⟦/w⟧　" + " / ".join(render(row) for row in picks))
    present = {row.stat for row in (*positive, *negative) if not row.flags & _TRAIT_INELIGIBLE}
    questions = []
    for stat in sorted(present & few_samples.keys()):
        label = stat_label(stat, melee)
        count = few_samples[stat]
        questions.append(L(f"{label} ?（{count} 张）", f"{label} ? ({count} cards)"))
    if questions:
        out.append(L("· 市场样本少 ", "· Small market sample ") + L("、", ", ").join(questions))
    return out


def trait_matrix(weapon: dict, modes: list[str], complete: dict | None = None) -> list[str]:
    """同一条词条只列一次，按模式显示固定配装的变化。"""
    complete = complete or {}
    catalog = _trait_catalog()
    wid = weapon["engine_id"]
    scenes = catalog.get("weapons", {}).get(wid, {})
    samples = catalog.get("roll_pool_audit", {}).get("market_samples", {})
    counts = samples.get("by_weapon", {}).get(wid, {})
    minimum = samples.get("min_cards", 5)
    melee = weapon["category"] == "Melee"
    labels = {}
    rows: dict[tuple[bool, str], dict[str, tuple[_TraitResult, bool]]] = {}
    for mode in modes:
        if mode in complete:
            label, scene = complete[mode]
            metric = "DPS"
        elif mode in scenes:
            scene = scenes[mode]
            label = L(*MODE_LABELS[mode])
            if mode == "base":
                label = L("平砍", "Neutral") if melee else L("主射", "Primary")
            if mode == "cycle" and not presets()[wid][mode]["fight"]["evolutions"]:
                label = L("充能循环", "Charge cycle")
            metric = "KPM"
        else:
            continue
        labels[mode] = (label, metric)
        for positive, direction in ((True, "positive"), (False, "negative")):
            for values in scene[direction]:
                trait = _TraitResult(*values)
                if trait.flags & _TRAIT_INELIGIBLE:
                    continue
                uncertain = bool(trait.flags & _TRAIT_UNCONFIRMED) or (
                    counts.get(trait.stat, minimum) < minimum
                )
                rows.setdefault((positive, trait.stat), {})[mode] = (trait, uncertain)
    if not rows:
        return []

    multiple = len(labels) > 1
    sections: list[list[str]] = [[], [], [], []]
    elements = {"heat", "cold", "electricity", "toxin"}
    for (positive, stat), measurements in rows.items():
        name = stat_label(stat, melee, full=True)
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
            note, note_color = "", "n"
            effect = "⟦n⟧—⟦/n⟧"
            if flag:
                note = L("资格待确认", "Unconfirmed")
            elif stat.startswith("damage_to_"):
                note = L("按派系", "Faction")
            elif trait.flags & _TRAIT_REPLAY_REQUIRED:
                note = L("待复算", "Recalculation pending")
            elif trait.pct is None or stat == "reload_while_holstered":
                note = L("未模拟", "Unmodeled")
            else:
                change = trait.pct
                low = change if trait.low is None else trait.low
                high = change if trait.high is None else trait.high
                color = "n" if abs(change) < 0.05 else "y" if change > 0 else "m"
                effect = (
                    f"⟦{color}⟧{abs(change):.1f}%⟦/{color}⟧"
                    if color == "n"
                    else f"⟦{color}⟧{change:+.1f}%⟦/{color}⟧"
                )
                if stat in elements or fusion:
                    note, note_color = L("看组合", "Build-specific"), "v"
                elif not positive and high <= -25:
                    note, note_color = L("高风险", "High risk"), "m"
                elif not positive and low > -3 and high < 3:
                    note = L("低影响", "Low impact")
                elif not positive:
                    note = L("需权衡", "Trade-off")
            if not same_amount:
                effect += f" ⟦n⟧({amounts[mode]})⟦/n⟧"
            annotation = f"⟦{note_color}⟧{note}⟦/{note_color}⟧" if note else ""
            if multiple:
                columns.append(" ".join((effect, annotation)).strip())
            else:
                columns.extend((effect, annotation or "⟦n⟧—⟦/n⟧"))
        sections[group].append("· " + "　".join(columns))

    headings = [L("词条", "Trait"), L("牌面中位值", "Median roll")]
    if multiple:
        headings.extend(f"{label} {metric}" for label, metric in labels.values())
    else:
        ((_, metric),) = labels.values()
        headings.extend((L(f"{metric} 变化", f"{metric} change"), L("备注", "Notes")))
    lines = [
        L("◆ 全词条对照", "◆ All traits")
        if complete
        else L("◆ 随包参考配装 · 全词条", "◆ Bundled reference builds · All traits"),
        L(
            "※ 固定配装，按满级三正一负中位值试算。",
            "※ Fixed build with median max-rank 3+/1− rolls.",
        ),
    ]
    if any(metric == "KPM" for _, metric in labels.values()):
        lines.append(L("※ KPM 为每分钟击杀数。", "※ KPM means kills per minute."))
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


def _audited_unconfirmed(query, fight) -> frozenset[str]:
    """按本次搜索的武器、模式和卡片槽位使用 P4 核实过的洗练资格。"""
    if query["weapon"] != fight["weapon"]:
        return frozenset()
    directions = {entry["id"]: "positive" for entry in query["bonuses"]}
    if query["malus"]:
        directions[query["malus"]["id"]] = "negative"
    audit = _trait_catalog().get("roll_pool_audit", {})
    return frozenset(
        stat
        for weapon, mode, direction, stat, verdict in audit.get("decisions", ())
        if weapon == fight["weapon"]
        and mode == fight["mode"]
        and verdict == "rollable"
        and directions.get(stat) == direction
    )


def _usable_card(reply, length, *, allowed_unconfirmed=frozenset()):
    return (
        not reply.get("illegal")
        and set(reply.get("unconfirmed") or ()) <= allowed_unconfirmed
        and len(reply.get("stats", [])) == length
        and all(row.get("modeled") is True for row in reply["stats"])
    )


class CardError(damage_engine.EngineError):
    """卡面还不能按原词条、等级和数值交给引擎。"""


async def _confirmed_card(cfg, query, *, allowed_unconfirmed=frozenset()):
    expected = query["bonuses"] + ([query["malus"]] if query["malus"] else [])
    if len({entry["id"] for entry in expected}) != len(expected):
        raise CardError(L("同一词条重复填写", "A trait is repeated"))
    raw = await damage_engine._request(cfg, "/api/riven", query)
    if not _usable_card(raw, len(expected), allowed_unconfirmed=allowed_unconfirmed):
        raise CardError(
            L(
                "这些词条暂无法在完整模拟中按原卡复算",
                "The engine cannot simulate all these traits as entered",
            )
        )
    for entered, row in zip(expected, raw["stats"]):
        if row["id"] != entered["id"] or not math.isclose(
            row["shown"], entered["value"], rel_tol=0, abs_tol=10 ** -row["decimals"] / 2 + 1e-8
        ):
            raise CardError(
                L("词条数值超出该等级区间，请核对卡面", "Trait values do not match this rank")
            )
    stats = [{"id": row["id"], "roll": row["roll"]} for row in raw["stats"]]
    locked = {
        "id": "observed",
        "spec": {
            "rank": query["rank"],
            "polarity": query["polarity"],
            "bonuses": stats[: len(query["bonuses"])],
            "malus": stats[-1] if query["malus"] else None,
        },
    }
    return locked, raw["stats"]


async def _lock_search_card(cfg, query, *, allowed_unconfirmed=frozenset()):
    if query["rank"] is not None:
        return await _confirmed_card(cfg, query, allowed_unconfirmed=allowed_unconfirmed)
    matches = []
    for rank in range(9):
        try:
            matches.append(
                await _confirmed_card(
                    cfg, {**query, "rank": rank}, allowed_unconfirmed=allowed_unconfirmed
                )
            )
        except CardError:
            continue
    if len(matches) != 1:
        raise CardError(
            L(
                "请核对卡图并补上强化等级，例如 等级8",
                "Check the card and supply its upgrade rank, for example rank=8",
            )
        )
    return matches[0]


def _arcane_pairs(build) -> set:
    return {
        (card, rank)
        for card, rank in zip(build["arcane"], build["arcane_rank"], strict=True)
        if card != "none"
    }


def _search_scope(fight, locked, meta, candidates):
    """固定配装预设的姿态、扩展槽、赋能、进化和战斗参数，搜索常规 MOD。"""
    (weapon,) = [item for item in meta["weapons"] if item["id"] == fight["weapon"]]
    stances = {
        row["id"] for pool in meta["mod_pools"].values() for row in pool if row.get("stance")
    }
    extra = fight["mods"][8:]
    (stance,) = [mid for mid in extra if mid in stances] or [""]
    (exilus,) = [mid for mid in extra if mid not in stances] or ["none"]
    mod_ids = {
        row["id"]
        for row in candidates
        if row["id"] != "none" and row["id"] not in stances and not row["id"].startswith("riven:")
    }
    max_ranks = {row["id"]: row["max_rank"] for row in meta["arcanes"]}
    arcane_marks = {
        card if rank == max_ranks[card] else f"{card}@{rank}": "fixed"
        for card, rank in _arcane_pairs(fight)
    }
    fixed_evos = {}
    for tier in weapon["evolutions"]:
        options = [row["id"] for row in tier["options"] if row["id"] in fight["evolutions"]]
        if options:
            fixed_evos[str(tier["tier"])] = options
    if {eid for options in fixed_evos.values() for eid in options} != set(fight["evolutions"]):
        raise ValueError("reference evolutions do not match engine")
    return {
        **fight,
        "mods": dict.fromkeys(sorted(mod_ids), "search") | {CARD: "fixed"},
        "rivens": [locked],
        "modes": {fight["mode"]: "fixed"},
        "exilus": {exilus: "fixed"},
        "stance": stance,
        "arcanes": arcane_marks,
        "evolutions": fixed_evos,
        "build_min": 8,
        "build_size": 8,
        "strategy": "quick",
        "candidate_runs": 8,
        "max_evals": SEARCH_EVALS,
        "finalists": SEARCH_FINALISTS,
        "threads": 1,
        "runs": 64,
        "seed": SCREEN_SEED,
        "run_series": False,
    }


def _search_build(replay, fight, locked, scope):
    mods = replay["mods"]
    extras = set(scope["exilus"]) - {"none"}
    if scope["stance"]:
        extras.add(scope["stance"])
    if (
        replay["rivens"] != [locked]
        or mods.count(CARD) != 1
        or len(mods) != len(set(mods))
        or len(mods) != 8 + len(extras)
        or not extras <= set(mods)
        or any(mid not in scope["mods"] for mid in mods if mid not in extras)
        or _arcane_pairs(replay) != _arcane_pairs(fight)
        or set(replay["evolutions"]) != set(fight["evolutions"])
        or any(
            replay.get(key) != value
            for key, value in fight.items()
            if key not in {"mods", "arcane", "arcane_rank", "evolutions"}
        )
    ):
        raise ValueError("search replay changed a locked field")
    return {**fight, "mods": mods, "rivens": [locked]}


async def recommend(host, cfg, preset, query):
    """每次用已回读的实值开始一次新搜索，并用相同种子复算候选。"""
    fight = preset["fight"]
    locked, stats = await _lock_search_card(
        cfg, query, allowed_unconfirmed=_audited_unconfirmed(query, fight)
    )
    meta = await damage_engine.search_request(host, cfg, damage_engine.SEARCH_META_PATH, {})
    candidate_query = {
        **{key: fight[key] for key in ("weapon", "mode", "evolutions", "assembly") if key in fight},
        "axis": {"kind": "mods", "idx": 0},
    }
    candidates = await damage_engine.search_request(host, cfg, "/api/candidates", candidate_query)
    scope = _search_scope(fight, locked, meta, candidates["candidates"])
    reply = await damage_engine.search_request(host, cfg, damage_engine.SEARCH_PATH, scope)
    if reply.get("cancelled") or not reply.get("results") or not reply.get("searched"):
        raise damage_engine.EngineBuildError(
            L("未找到可用的锁卡配装", "No legal build found with the card locked")
        )
    best = None
    seen = set()
    for row in reply["results"][:SEARCH_FINALISTS]:
        body = _search_build(row["replay"], fight, locked, scope)
        ordered = tuple(body["mods"])
        if ordered in seen:
            continue
        seen.add(ordered)
        raw = await _fight(cfg, body)
        if best is None or _kpm(raw) > _kpm(best[1]):
            best = (body, raw)
    if best is None:
        raise damage_engine.EngineBuildError(
            L("引擎没有返回可复算的配装", "The engine returned no build to replay")
        )
    selected, scored = best
    baseline = await _fight(cfg, fight)
    return {
        "locked": locked,
        "stats": stats,
        "build": selected,
        "score": scored,
        "baseline": baseline,
        "gain": _gain(scored, baseline),
        "searched": reply["searched"],
        "finalists": len(seen),
    }


def _kpm(raw):
    return raw["score"] * 60 / raw["duration"]


def _gain(test, base):
    a, b = test["score_runs"], base["score_runs"]
    first, second = mean(a), mean(b)
    if second <= 0:
        return {"pct": None, "ci95": None}
    ratio = first / second
    pct = 100 * (ratio - 1)
    paired = stdev(x - ratio * y for x, y in zip(a, b, strict=True))
    radius = 1.96 * 100 * paired / (math.sqrt(len(a)) * second)
    return {"pct": pct, "ci95": (pct - radius, pct + radius)}


async def _fight(cfg, body):
    wire = {**body, "seed": SEED, "runs": RUNS, "run_series": True}
    raw = await damage_engine._run_simulation(cfg, wire)
    if raw.get("forma", {}).get("legal") is not True:
        raise damage_engine.EngineBuildError(L("配装容量不合法", "Build exceeds capacity"))
    if len(raw.get("score_runs", [])) != RUNS:
        raise damage_engine.EngineError(
            L("引擎未返回逐轮成绩", "Engine did not return per-trial scores")
        )
    return raw


def _mod_name(key):
    key, sep, rank = key.partition("@")
    plain = key.replace("_", " ")
    resolved = damage_calc.find_mod_exact(plain) or {}
    name = (resolved.get("name") if is_en() else resolved.get("zh")) or plain
    return name + (L(f"（{rank}级）", f" (rank {rank})") if sep and rank else "")


def _reference_mod_entry(key):
    mod_id, _, requested = key.partition("@")
    name = mod_id.replace("_", " ")
    mod = damage_calc.find_mod_exact(name) or display_names().get("referenceMods", {}).get(
        mod_id, {}
    )
    rank = int(requested) if requested.isdecimal() else mod.get("max_rank")
    return name, mod, rank


def _reference_mod(key):
    name, mod, rank = _reference_mod_entry(key)
    base = mod.get("base_drain")
    drain = base + rank if type(base) is int and type(rank) is int else None
    label = L(mod.get("zh") or mod.get("name") or name, mod.get("name") or name)
    return label, rank, drain, mod.get("polarity")


def _shown_trait(row, melee):
    name = stat_label(row["id"], melee)
    amount, unit = row["shown"], row["unit"]
    if unit == "x":
        return f"{name} ×{amount:.{row['decimals']}f}"
    if not unit:
        unit = {"range": "m", "punch_through": "m", "combo_duration": "s"}.get(row["id"], "")
    return f"{name} {amount:+.{row['decimals']}f}{unit}"


def _build_context_line(fight, melee):
    mode = fight["mode"]
    name = (
        L("主射", "Primary fire")
        if mode == "base" and not melee
        else _mode_label(mode, melee, fight["evolutions"])
    )
    distance = math.dist(fight["player_at"], fight["target_at"])
    pct = fight["headshot_pct"]
    hit = L(f"弱点 {pct:g}%", f"Weak points {pct:g}%") if pct else L("躯干", "Body")
    return L(
        f"◆ {name} · 钢铁之路 堕落重型机枪手 Lv{fight['level']} · {hit} · {distance:g} 米",
        f"◆ {name} · Steel Path Corrupted Heavy Gunner Lv{fight['level']} · {hit} · {distance:g} m",
    )


def reference_build_lines(preset: dict, melee: bool) -> list[str]:
    fight = preset["fight"]
    out = [_build_context_line(fight, melee)]
    replay = _build_details().get(fight["weapon"], {}).get(fight["mode"], {})
    full = replay.get("full")
    seconds = fight["duration"]
    if full is None:
        headline = L(
            "· 随包参考 · 整套 DPS 暂缺", "· Bundled reference · Full build DPS unavailable"
        )
    else:
        headline = L(
            f"· 随包参考 · {seconds:g} 秒 · 整套 DPS {full:,.0f}",
            f"· Bundled reference · {seconds:g} s · Full build DPS {full:,.0f}",
        )
    baseline = _trait_catalog().get("weapons", {}).get(fight["weapon"], {}).get(fight["mode"])
    if baseline:
        kpm = baseline["baseline_kpm"]
        score = f"{kpm:.2f}" if kpm >= 0.01 else f"{kpm:.2g}"
        headline += f" · {score} KPM"
    out.append(headline)
    out.extend(
        damage_engine.weapon_panel_lines(replay.get("stats"), show_empty_groups=True, melee=melee)
        or [L("◆ 武器面板", "◆ Weapon stats"), L("· 暂缺面板数据", "· Panel unavailable")]
    )

    mods = [_reference_mod(mid) for mid in fight["mods"][:8]]
    layout = replay.get("slots", ())
    header = L("◆ 无紫卡参考配装", "◆ No-Riven reference build")
    if capacity := replay.get("cap"):
        used, limit, forma = capacity
        header += L(
            f" · 容量 {used}/{limit} · 极化 {forma} 次",
            f" · Capacity {used}/{limit} · Forma ×{forma}",
        )
    elif mods and all(cost is not None for _, _, cost, _ in mods):
        total = sum(cost for _, _, cost, _ in mods)
        header += L(f" · 主槽原耗 {total}", f" · Regular slots raw drain {total}")
    out.extend(
        (
            header,
            L(
                f"　槽位　MOD　等级　{'容量' if layout else '原始容量'}　极性　拆下后 DPS　变化",
                f"　Slot　MOD　Rank　{'Drain' if layout else 'Raw drain'}　Polarity　DPS without　Change",
            ),
        )
    )
    for slot, (name, rank, drain, polarity) in enumerate(mods, 1):
        if slot <= len(layout):
            drain, polarity = layout[slot - 1]
        rank_text = str(rank) if rank is not None else "—"
        cost = str(drain) if drain is not None else "—"
        polarity_text = (
            f"⟦pol:{polarity}⟧{polarity.capitalize()}" if polarity and polarity != "none" else "—"
        )
        without, settled = replay.get("without", {}).get(str(slot - 1), (None, False))
        dps, change = formatters.dps_removal_columns(full, without, settled=settled)
        out.append(f"· {slot:02d}　{name}　{rank_text}　{cost}　{polarity_text}　{dps}　{change}")
    extras = []
    exilus = False
    for mid in fight["mods"][8:]:
        plain, mod, rank = _reference_mod_entry(mid)
        stance = mod.get("compat") == "Stance"
        exilus |= not stance
        label = L("架势", "Stance") if stance else L("扩展槽", "Exilus")
        english = mod.get("name") or plain
        name = L(mod.get("zh") or search.page_name_from_slug(english), english)
        if rank is not None:
            if not stance and (exilus_slot := replay.get("exilus")):
                drain, polarity = exilus_slot
                name += L(f"（{rank}级，容量 {drain}）", f" (rank {rank}, drain {drain})")
                if polarity:
                    name += f" · ⟦pol:{polarity}⟧{polarity.capitalize()}"
            else:
                name += L(f"（{rank}级）", f" (rank {rank})")
        extras.append(f"· {label}　{name}")
    if not exilus:
        extras.append(L("· 扩展槽　未装备", "· Exilus　Empty"))
    arcanes = []
    for key, rank in zip(fight["arcane"], fight["arcane_rank"], strict=True):
        if key == "none":
            continue
        plain = key.replace("_", " ").title()
        data = damage_calc.find_arcane(plain) or {}
        english = data.get("name") or plain
        name = L(data.get("zh") or search.page_name_from_slug(key), english)
        arcanes.append(L(f"· 赋能　{name}（{rank}级）", f"· Arcane　{name} (rank {rank})"))
    out.extend((L("◆ 辅助装备", "◆ Additional equipment"), *extras))
    out.extend(arcanes or [L("· 赋能　未装备", "· Arcane　Empty")])
    note = L(
        "拆下这一张后的 DPS 与上方整套对比。其他 MOD、辅助装备和进化照原配装。",
        "Each row compares DPS with that slot empty against the full build shown above.",
    )
    if not layout:
        note = L("原始容量按卡面显示。", "Raw drain precedes polarity adjustments. ") + note
    out.append(f"※ {note}")
    if fight["evolutions"]:
        out.append(L("◆ 灵化进化", "◆ Incarnon evolutions"))
        descriptions = display_names().get("referenceEvolutions", {})
        prefix = f"{fight['weapon']}_"
        for key in fight["evolutions"]:
            if row := descriptions.get(key):
                code, name_zh, effect_zh, name_en, effect_en = row
                out.append(f"· {code} {L(name_zh, name_en)} · {L(effect_zh, effect_en)}")
                continue
            name = key.removeprefix(prefix).removeprefix("evo1_").replace("_", " ").title()
            if name == "Incarnon Form":
                name = L("灵化形态", "Incarnon Form")
            out.append(f"· {name}")
    return out


def build_recommendation_lines(result, melee):
    body = result["build"]
    rank = result["locked"]["spec"]["rank"]
    polarity = result["locked"]["spec"]["polarity"]
    shown = " / ".join(_shown_trait(row, melee) for row in result["stats"])
    mods = [
        L("锁定紫卡", "Locked Riven") if mid == CARD else _mod_name(mid) for mid in body["mods"]
    ]
    base, kpm = _kpm(result["baseline"]), _kpm(result["score"])
    gained = result["gain"]["pct"]
    delta = "" if gained is None else f"（{gained:+.1f}%）"
    return [
        _build_context_line(body, melee),
        L(f"· 紫卡已锁定 · {rank} 级 · {polarity}", f"· Riven locked · rank {rank} · {polarity}"),
        f"· {shown}",
        L("◆ 配装顺序", "◆ Build order"),
        "· " + " / ".join(mods),
        L(
            f"· 无紫卡基准 {base:.2f} → 锁卡重配 {kpm:.2f} KPM {delta}",
            f"· No-Riven reference {base:.2f} → locked rebuild {kpm:.2f} KPM {delta}",
        ),
        L(
            f"· 搜索 {result['searched']} 套候选，复算 {result['finalists']} 套，每套 {RUNS} 轮",
            f"· Searched {result['searched']} candidates, replayed {result['finalists']} with {RUNS} trials each",
        ),
    ]
