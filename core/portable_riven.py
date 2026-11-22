"""用已验收的工况锁定实卡，并复用三靶标准求解结果。"""

import asyncio
import hashlib
import json
import math
from functools import lru_cache
from pathlib import Path

from . import damage_engine, riven_rating
from .damage_calc import find_enemy_exact
from .formatters import dps_removal_columns
from .i18n import L, is_en
from .parser import _STAT_LOOKUP
from .request_queue import QueueUnavailable

# 上游的 weapon path 和 fireMode 与插件的武器 ID、玩法名各自独立。
FINGERPRINT = damage_engine.PORTABLE_FINGERPRINT
TARGET = "/Lotus/Types/Enemies/Duviri/Avatars/DuviriMeleeAvatar"
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
MODE_ALIASES = {"灵化形态": "incarnon", "incarnon": "incarnon"}
FIRE_MODE_LABELS = {0: ("主射", "Primary fire"), 1: ("灵化形态", "Incarnon form")}
POLARITIES = {"madurai": "AP_ATTACK", "naramon": "AP_TACTIC", "vazarin": "AP_DEFENSE"}
POLARITY_KEYS = {code: key for key, code in POLARITIES.items()}
RESULT_TTL_S = 600
_HEAVY_CALIBER = "/Lotus/Upgrades/Mods/Rifle/DualStat/CorruptedDamageRecoilRifle"
_SERRATION = "/Lotus/Upgrades/Mods/Rifle/WeaponDamageAmountMod"

INCARNON_ZH = {
    ("Braton Prime", "2-2"): "基础伤害 +2，多重射击 +20%。额外弹丸消耗备弹，伤害 +54%。",
    ("Braton Prime", "3-1"): "基础弹药上限设为 1125 发。",
    ("Braton Prime", "4-2"): "暴击几率低于 50% 时，基础暴击倍率 +3.0×。",
    ("Boar Prime", "2-1"): "基础伤害 +10，空匣装填后再增加 14 点基础伤害。",
    ("Boar Prime", "3-2"): "基础弹药上限设为 195 发。",
    ("Boar Prime", "4-1"): "每颗弹丸基础触发几率 +12 个百分点，灵化形态 +96 个百分点。",
    (
        "Boar Prime",
        "4-3",
    ): "基础暴击几率 +10 个百分点。每颗弹丸基础触发几率 +6 个百分点，灵化形态 +48 个百分点。",
}

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


@lru_cache(maxsize=4)
def _prepared_entries(path, modified, size):
    # 安装时用原子替换，文件身份用于进程内失效。
    del modified, size
    raw = json.loads(path.read_text())
    return raw["entries"] if raw.get("engineFingerprint") == FINGERPRINT else []


def _prepared_result(path, mode):
    weapon, fire_mode, hit = mode
    try:
        info = path.stat()
        if info.st_size > 8_000_000:
            return None
        prepared = _prepared_entries(path, info.st_mtime_ns, info.st_size)
        for entry in prepared:
            if entry["weapon"]["path"] != weapon or entry["fireMode"] != fire_mode:
                continue
            if _valid_target(entry, hit):
                return {**entry, "engineFingerprint": FINGERPRINT}
    except (OSError, ValueError, TypeError, KeyError):
        return None
    return None


async def prepared_result(host, mode):
    service = host.damage_engine
    view_root = service.portable_release if service.portable_installed() else service.root
    return await asyncio.to_thread(_prepared_result, view_root / "portable-views.json", mode)


def _valid_target(result, headshot):
    target = result["evaluationTarget"]
    return (
        target["enemyPath"] == TARGET
        and target["level"] == 170
        and target["steelPath"] is True
        and target["enemyEnhancements"] == {"eximus": False, "noOverguard": True}
        and target["headshotChance"] == headshot
        and target["windowSeconds"] == 30
        and target["armourStrip"] == target["elementalArmourStrip"] == 0
        and target["hitLocation"] == "body"
        and result["scoreMetric"] == "windowedDps"
        and len(result["targetCalibration"]["hardnessNodes"]) == 3
    )


def analysis_request(
    mode,
    card,
    polarity,
    *,
    source_disposition=None,
    options: damage_engine.PortableAnalysisOptions | None = None,
):
    weapon, fire_mode, headshot = mode
    if card["rank"] != 8 or polarity not in POLARITIES:
        raise damage_engine.EngineError(
            L(
                "请确认紫卡满级并填写极性，可补「等级8 极性=naramon」",
                "Enter a max-rank Riven and its polarity; add rank=8 polarity=naramon",
            )
        )
    if source_disposition is not None and (
        not math.isfinite(source_disposition) or not 0 < source_disposition <= 2
    ):
        raise damage_engine.EngineError(
            L("请核对截图时的来源倾向", "Check the disposition when the card was captured")
        )
    stats = []
    for negative, lines in ((False, card["bonuses"]), (True, (card["malus"],))):
        for line in lines:
            if line is None:
                continue
            stat, value = line["id"], line["value"]
            if stat not in GUN_CODES:
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
            stats.append({"code": GUN_CODES[stat], "value": magnitude, "negative": negative})
    if len({stat["code"] for stat in stats}) != len(stats):
        raise damage_engine.EngineError(L("同一词条重复填写", "A trait is repeated"))
    request = {
        "weapon": weapon,
        "fireMode": fire_mode,
        "disposition": REFERENCE_DISPOSITION,
        "stats": stats,
        "rivenCard": {"rank": card["rank"], "polarity": POLARITIES[polarity]},
        "profile": "standard",
        "headshotChance": headshot,
        "_reuseIncarnonWitness": True,
        "_useBakedIncarnonBaselines": True,
    }
    if source_disposition is not None:
        request["sourceDisposition"] = source_disposition
    return damage_engine.with_portable_analysis_options(request, options)


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
        or any(returned[key] != value for key, value in request["rivenCard"].items())
        or returned["drain"] != 10 + returned["rank"]
        or not _valid_target(result, request["headshotChance"])
        or type(result["searchWork"]["limited"]) is not int
    ):
        raise damage_engine.EngineError(
            L(
                "本次计算没有匹配这张牌面或所选模式，请重试",
                "The estimate did not match this card and mode. Try again.",
            )
        )
    _placement(result["actual"])


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
):
    request = analysis_request(
        mode, card, polarity, source_disposition=source_disposition, options=options
    )
    key_data = json.dumps([FINGERPRINT, port, request], sort_keys=True)
    key = "portable-result:" + hashlib.sha256(key_data.encode()).hexdigest()

    async def solve():
        async with damage_engine.simulation_session(host, event, managed=False):
            await host.damage_engine.ensure_portable(port)
            try:
                result = await damage_engine._finish_compute(
                    damage_engine.portable_request(port, "/api/analyze", request, timeout=105)
                )
            except damage_engine.EngineBuildError as exc:
                if "Riven stat" in str(exc) and "outside" in str(exc):
                    hint = L(
                        "词条数值与提交倾向的区间不符。旧卡可按已核实的来源倾向重试，例如「倾向1.3」，也可提供当前卡面。",
                        "Traits fall outside the entered disposition range. Provide the confirmed original disposition (e.g. d1.3) or the current card.",
                    )
                    raise damage_engine.EngineBuildError(hint) from None
                raise
        check_result(result, request)
        return {**result, "cardSourceDisposition": source_disposition}

    return await host.damage_engine.riven_build_cache.get_or_fetch(
        key, RESULT_TTL_S, solve, cache_if=lambda result: result["searchWork"]["limited"] == 0
    )


def _polarity_label(code):
    key = POLARITY_KEYS.get(code)
    return f"⟦pol:{key}⟧{key.capitalize()}" if key else code


def _placement(actual):
    plan = actual["forma"]
    riven = actual["riven"]
    placement = plan["placement"]
    by_origin = {mod["slot"]: mod for mod in actual["mods"]}
    origins = [slot["origin"] for slot in placement if slot is not None]
    if (
        plan["fits"] is not True
        or plan["limit"] != 60
        or plan["drain"] != sum(slot["drain"] for slot in placement if slot is not None)
        or not 0 <= plan["drain"] <= plan["limit"]
        or len(placement) != 8
        or riven["slot"] in by_origin
        or len(by_origin) != len(actual["mods"])
        or len(origins) != len(set(origins))
        or set(origins) != {*by_origin, riven["slot"]}
    ):
        raise ValueError("配装容量或槽位无效")
    return plan, riven, placement, by_origin


def enemy_label(path, english, fallback=""):
    if is_en():
        return english
    name = riven_rating.display_names().get("enemies", {}).get(path)
    return name or fallback or (find_enemy_exact(english) or {}).get("zh") or english


def scenario_line(result, *, show_defense=True):
    target = result["evaluationTarget"]
    mode = L(*FIRE_MODE_LABELS[result["fireMode"]])
    weakpoint_chance = target["headshotChance"]
    hit = (
        L(f"弱点 {weakpoint_chance:.0%}", f"Weak points {weakpoint_chance:.0%}")
        if weakpoint_chance
        else L("躯干", "body")
    )
    english = target["enemyName"]
    name = enemy_label(target.get("enemyPath"), english)
    if (distance := target.get("distance")) is not None:
        hit += L(f" · {distance:g} 米", f" · {distance:g} m")
    defense = L(" · 无超宏防护", " · no Overguard") if show_defense else ""
    return L(
        f"◆ {mode} · 钢铁之路 {name} Lv{target['level']} · {hit}{defense}",
        f"◆ {mode} · Steel Path {english} Lv{target['level']} · {hit}{defense}",
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


def evolution_lines(weapon, choices):
    """列出本次实际采用的进化，英文按同版本的武器路径与分支取显示数据。"""
    choices = choices or {}
    options = {
        option["id"]: option
        for tier in (weapon.get("incarnon") or ())
        for option in tier["options"]
    }
    language = is_en()
    display_key = "incarnon" if language else "incarnonZh"
    descriptions = riven_rating.display_names().get(display_key, {}).get(weapon.get("path"), {})
    codes = [choices[key] for key in sorted(choices)]
    if "1-1" in options and "1-1" not in codes:
        codes.insert(0, "1-1")
    if not codes:
        return []
    lines = [L("◆ 灵化进化", "◆ Incarnon evolutions")]
    for code in codes:
        option = options.get(code, {})
        if language:
            label, effect = descriptions.get(code, ("", ""))
            label = label or option.get("labelEn", "")
            effect = effect or option.get("descriptionEn") or "Effect description unavailable"
        else:
            label = option.get("label", "")
            effect = descriptions.get(code) or INCARNON_ZH.get(
                (weapon.get("nameEn"), code), option.get("description", "")
            )
            effect = effect.replace("。；", "。").replace("；", "，").replace("。：", "，")
        if code == "1-1" and (not label or label == code):
            label = L("灵化形态", "Incarnon Form")
        name = f"{code} {label}" if label and label != code else code
        lines.append(f"· {name} · {effect}" if effect else f"· {name}")
    return lines


def _build_slots(build, *, with_riven):
    slots = [None] * 8
    if with_riven:
        _, card, placement, mods = _placement(build)
        for index, slot in enumerate(placement):
            if slot is None:
                continue
            origin = slot["origin"]
            placed = card if origin == card["slot"] else mods[origin]
            slots[index] = {**placed, "slot": index}
    else:
        if build["includesRiven"] or not build["forma"]["fits"]:
            raise ValueError("无紫卡底表装配不可用")
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
    """同槽试算满级膛线。散布导致的命中损失尚未计入引擎评分。"""
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


async def _mod_gains(service, port, request):
    """用完整配装和各槽留空时的同靶 DPS 作对照。"""
    identity = hashlib.sha256(json.dumps([FINGERPRINT, port, request], sort_keys=True).encode())
    key = identity.hexdigest()

    async def fetch():
        body = request
        replacement_slot = None
        capacity_saved = 0
        comparison_complete = True

        async def replay(body):
            response = await damage_engine._finish_compute(
                damage_engine.portable_request(port, "/api/simulate", body, timeout=5)
            )
            window, validation = damage_engine.validate_fixed_replay(response, body)
            if (response.get("incarnonChoices") or {}) != body["incarnonChoices"]:
                raise damage_engine.EngineError("固定回放的灵化路线不符")
            return window["windowDps"], validation, response

        async with service.replays.slot():
            await service.ensure_portable(port)
            full, validation, response = await replay(body)
            if not validation["valid"] and "riven" not in body:
                body = _fit_reference_slots(body, validation)
                full, validation, response = await replay(body)
            if not validation["valid"]:
                raise damage_engine.EngineBuildError("装配超过容量")
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
        }
        return {
            "full": full,
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
        "enemyEnhancements": {"eximus": target["enemyEnhancements"]["eximus"]},
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
    return damage_engine.with_portable_replay_options(request, options)


async def build_view(host, port, result, *, with_riven=False):
    """将三靶选出的配装、单卡影响与灵化放入同一张回图。"""
    build = result["actual" if with_riven else "baseline"]
    plan = build["forma"]
    slots = _build_slots(build, with_riven=with_riven)
    lines = [scenario_line(result, show_defense=False)]
    limited = with_riven and result["searchWork"]["limited"] > 0
    if with_riven:
        gain = result["actualGain"]
        lines.append(
            L("· 暂未算出整卡收益", "· Overall Riven comparison pending")
            if limited
            else L(
                f"· 对比无紫卡配装（无超宏防护） · 预计输出 {gain:+.1f}%",
                f"· Estimated output vs a no-Riven build (no Overguard) {gain:+.1f}%",
            )
        )
        lines.append(_card_basis_line(result))
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
        weapon = weapons[weapon["path"]]
        original_polarities = (list(weapon.get("polarities") or ()) + [None] * 9)[:9]
        polarities = original_polarities[:8]
        for slot, polarity in plan["slots"].items():
            polarities[int(slot)] = polarity
        request = build_replay_request(
            result, weapon["path"], slots, polarities, route, with_riven=with_riven
        )
        replay = await _mod_gains(service, port, request)
    except (damage_engine.EngineError, QueueUnavailable, KeyError, TypeError, ValueError):
        # 局部回放不可用时，已确认的配装、推荐极性及进化仍然可以展示。
        pass

    lines.extend(
        _fixed_build_lines(
            target, slots, plan, replay, original_polarities, with_riven=with_riven, limited=limited
        )
    )
    return lines + evolution_lines(weapon, route)


def _fixed_build_lines(
    target,
    slots,
    plan,
    replay,
    original_polarities,
    *,
    with_riven=False,
    limited=False,
    bundled=False,
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
        seconds = L(f"{secs:g} 秒", f"{secs:g} s")
        lines.append(
            f"· {source} · {seconds} · {L('整套 DPS', 'Full build DPS')} {full:,.0f}{defense}"
        )
    else:
        lines.append(
            L("· 独立引擎 · 整套 DPS 暂缺", "· Standalone engine · Full build DPS unavailable")
        )
    lines.extend(
        damage_engine.weapon_panel_lines(replay.get("stats"), show_empty_groups=True)
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
    if exilus and exilus.get("slotPolarity"):
        innate = original_polarities[8] if len(original_polarities) > 8 else None
        forms += int(exilus["slotPolarity"] != innate)
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
        polarity = changes.get(str(index)) or (mod["polarity"] if riven else None)
        if not polarity and index < len(original_polarities):
            polarity = original_polarities[index]
        polarity = _polarity_label(polarity) if polarity in POLARITY_KEYS else "—"
        dps, change = dps_removal_columns(full, without.get(index))
        lines.append(f"· {number}　{name}　{mod['rank']}　{used_slot}　{polarity}　{dps}　{change}")
    lines.extend(_equipment_lines(equipment, verified=bool(replay)))
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
        utility = L(
            f"{name}（{exilus['rank']}级，容量 {exilus['drain']}）",
            f"{name} (rank {exilus['rank']}, drain {exilus['drain']})",
        )
        if polarity := exilus.get("slotPolarity"):
            utility += f" · {_polarity_label(polarity)}"
    arcs = []
    for arc in equipment.get("arcanes") or ():
        name = L(arc.get("nameZh") or arc["nameEn"], arc["nameEn"])
        arcs.append(L(f"{name}（{arc['rank']}级）", f"{name} (rank {arc['rank']})"))
    return [
        L("◆ 辅助装备", "◆ Additional equipment"),
        L(f"· 扩展槽　{utility}", f"· Exilus　{utility}"),
        L(f"· 赋能　{' / '.join(arcs) or empty}", f"· Arcane　{' / '.join(arcs) or empty}"),
    ]


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


async def reference_build_lines(host, port, mode, *, live_analysis=False):
    """优先读取已验证的固定配装。带回放请求的数据也可用独立引擎现算。"""
    if not mode:
        return []
    path, fire_mode, hit = mode
    for (weapon_path, fire, _), entry in _comparison_catalog().items():
        if weapon_path != path or fire != fire_mode or not (view := entry.get("noRivenBuild")):
            continue
        try:
            target = view["target"]
            if target["headshotChance"] != hit:
                continue
            if not port and view.get("independentOnly"):
                return []
            weapon = view["weapon"]
            if weapon["path"] != path:
                continue
            replay = view["replay"]
            replay = {**replay, "without": {int(k): v for k, v in replay["without"].items()}}
            bundled = True
            if port and (request := view.get("request")):
                try:
                    replay = await _mod_gains(host.damage_engine, port, request)
                    bundled = False
                except (
                    damage_engine.EngineError,
                    QueueUnavailable,
                    KeyError,
                    TypeError,
                    ValueError,
                ):
                    if view.get("independentOnly"):
                        return []
            elif live_analysis:
                return []
            initial = (list(weapon.get("polarities") or ()) + [None] * 9)[:9]
            scenario = {"evaluationTarget": target, "fireMode": fire}
            return [
                scenario_line(scenario, show_defense=False),
                *_fixed_build_lines(target, view["slots"], None, replay, initial, bundled=bundled),
                *evolution_lines(weapon, view["route"]),
            ]
        except (KeyError, ValueError, TypeError):
            return []
    return []


def prepared_traits(result, *, replay_request=None):
    """新工况可指定原固定配装请求，仅读取具有相同请求指纹的预计算。"""
    if result.get("engineFingerprint") != FINGERPRINT or result.get("scoreMetric") != "windowedDps":
        return None
    try:
        key = (result["weapon"]["path"], result["fireMode"], result["contextHash"])
        reference = _comparison_catalog()[key]["fixedTraitReference"]
        if replay_request is not None and reference.get("replayContextHash") != (
            damage_engine.portable_replay_fingerprint(replay_request)
        ):
            return None
        baseline = reference["baselineScore"]
        if type(baseline) not in (float, int) or not math.isfinite(baseline) or baseline <= 0:
            return None
        title = L(*FIRE_MODE_LABELS[result["fireMode"]])
        rows = {"positive": [], "negative": []}
        ids = {code: stat for stat, code in GUN_CODES.items()}
        for line in reference["rows"]:
            sid = ids[line["code"]]
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
    return title, rows


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
        ids = {code: stat for stat, code in GUN_CODES.items()}
        traits = []
        for line in entry["stats"]:
            sid = ids[line["code"]]
            label = riven_rating.stat_label(sid)
            value = line["value"]
            direction = L("负", "−") if line["negative"] else L("正", "+")
            if sid.startswith("damage_to_"):
                multiplier = 1 + (-1 if line["negative"] else 1) * value / 100
                shown = f"×{multiplier:.2f}"
            else:
                unit = "m" if sid == "punch_through" else "%"
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
            group = "fusion" if row["code"] in FUSION_CODES.values() else "rollable"
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

    if negatives := result["recommendedNegative"]:
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
        return []
    if any(groups.values()):
        lines.append(
            L(
                "※ 建议收益按词条重配后试算。",
                "※ Suggested gains use builds reoptimized for each trait.",
            )
        )
    return lines
