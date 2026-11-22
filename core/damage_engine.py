# -*- coding: utf-8 -*-
"""独立伤害服务的配置、配装适配和同机运行。"""

from __future__ import annotations

import asyncio
import json
import math
import os
import platform
import re
import shutil
import signal
import sys
import zipfile
from contextlib import asynccontextmanager
from hashlib import sha256
from itertools import product
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal, TypedDict
from urllib.parse import urlsplit

import httpx

from . import damage_calc as dc
from .cache import TTLCache
from .i18n import L, is_en
from .portable_install import MAX_BUNDLE_SIZE, download_file, download_inputs, unpack
from .request_queue import QueueUnavailable, RequestQueue, send_queue_position

REPO = "https://github.com/skyti1437/warframe-damage-engine"
RELEASE = "2419c5c76a83aae34428fafc5da2eee21ec16093"
DEFAULT_URL = "http://127.0.0.1:8788"
PORTABLE_FINGERPRINT = "85ec9cdb3461c01c"
PORTABLE_DATA = "2026.08.13.13.24"
PORTABLE_TAG = f"portable-{PORTABLE_FINGERPRINT}"
PORTABLE_SOURCE_URL = f"{REPO}/tree/{PORTABLE_TAG}"
PORTABLE_DOWNLOADS = (
    f"https://raw.githubusercontent.com/skyti1437/warframe-damage-engine/{PORTABLE_TAG}/downloads"
)
PORTABLE_ASSETS = {
    "darwin-arm64": (
        "portable-runtime-mac-arm64.zip",
        "740041313e02c6bb728c066c9a1a84a6c7d7697da766afab5f40adb783aa5e87",
    ),
    "linux-x86_64": (
        "portable-runtime-linux-x86_64.zip",
        "1a527d2bddf6f854112e8af49dc6b100504a3cb57ff908e4fdbf375b781c2bf4",
    ),
}
NAME = "warframe-damage-engine"
SEARCH_PATH = "/api/compute/optimize"
SEARCH_META_PATH = "/api/meta"
_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][a-zA-Z0-9.-]+)?\Z")
_REVISION = re.compile(r"[0-9a-f]{40}\Z")
_TARGET_FIELDS = {
    "enemy",
    "faction",
    "level",
    "headshot",
    "steel_path",
    "base_armor",
    "fire_seconds",
    "heavy",
    "form",
    "lich_bonus",
}
_BUILD_FIELDS = {"mods", "arcanes", "stance", "notes", "conditional", "arcane_texts"}


class EngineError(ValueError):
    """可以显示给用户的独立引擎错误。"""


class EngineBuildError(EngineError):
    """引擎明确拒绝该配装。"""


class ScanReplayUnconfirmed(EngineError):
    """配装可见，但复算所需的形态、进化或装备还无法确认。"""


class PortableRankedEquipment(TypedDict):
    path: str
    rank: int


class PortableArcane(PortableRankedEquipment, total=False):
    stacks: int
    uptime: float


class PortableCombatScenario(TypedDict, total=False):
    aiming: bool
    heavyAttack: bool
    comboTier: int
    incarnonCycle: bool
    distance: float
    nearestEnemyDistance: float
    cloaked: bool
    triggerPolicy: Literal["emergent", "assumed_max"]
    combatState: dict[str, object]


class PortableEnemyEnhancements(TypedDict, total=False):
    eximus: bool
    archimedea: bool
    empowered: bool
    playerCount: int
    magneticStacks: int


class PortableAnalysisOptions(TypedDict, total=False):
    arcanes: list[PortableArcane]
    incarnonChoices: dict[str, str]
    innateElement: str
    innateElementValue: float
    kitgunParts: list[str]
    scenario: PortableCombatScenario


class PortableReplayOptions(PortableAnalysisOptions, total=False):
    exilus: PortableRankedEquipment
    stancePath: str
    slotPolarities: list[str | None]
    specialSlotPolarities: dict[str, str | None]
    armourStrip: float
    elementalArmourStrip: float
    externalBuffs: list[dict[str, object]]
    forcedProcs: dict[str, float]


def _with_portable_options(request, options, contract):
    """合入内部已规范化字段，并保护武器、普通槽、目标和牌面等既有输入。"""
    if not options:
        return request
    if options.keys() - contract.__optional_keys__ or any(
        key in request and request[key] != value for key, value in options.items()
    ):
        raise EngineError(
            L("引擎可选参数与既定工况不符", "Engine options conflict with the request")
        )
    return {**request, **options}


def with_portable_analysis_options(request: dict, options: PortableAnalysisOptions | None = None):
    return _with_portable_options(request, options, PortableAnalysisOptions)


def with_portable_replay_options(request: dict, options: PortableReplayOptions | None = None):
    return _with_portable_options(request, options, PortableReplayOptions)


def portable_replay_fingerprint(request: dict) -> str:
    """给同引擎同配装的离线对照生成跨主机身份，包含所有已传装备和战斗参数。"""
    identity = json.dumps([PORTABLE_FINGERPRINT, request], sort_keys=True, allow_nan=False)
    return sha256(identity.encode()).hexdigest()


def portable_damage_request(
    source: str,
    enemy: str,
    mods: list[dict],
    spec: dict,
    *,
    fire_mode: int = 0,
    enemy_enhancements: PortableEnemyEnhancements | None = None,
    options: PortableReplayOptions | None = None,
):
    return with_portable_replay_options(
        {
            "weapon": source,
            "fireMode": fire_mode,
            "enemy": enemy,
            "level": spec["level"],
            "steelPath": spec["steel_path"],
            "headshotChance": float(spec["headshot"]),
            "damageModel": "target",
            "windowSeconds": spec["fire_seconds"],
            "metric": "windowedDps",
            "mods": mods,
            "enemyEnhancements": {"eximus": False}
            if enemy_enhancements is None
            else enemy_enhancements,
        },
        options,
    )


def full_enabled(cfg) -> bool:
    return (cfg or {}).get("damage_engine_mode") == "simulation"


def portable_port(cfg) -> int:
    if not full_enabled(cfg):
        return 0
    value = (cfg or {}).get("damage_engine_portable_port", 0)
    return value if type(value) is int and 0 < value < 65536 else 0


def portable_asset():
    if sys.version_info[:2] == (3, 12):
        return PORTABLE_ASSETS.get(f"{sys.platform}-{platform.machine()}")
    return None


async def portable_request(port, path, body=None, *, timeout=12.0):
    if type(port) is not int or not 0 < port < 65536:
        raise EngineError(L("请核对三靶引擎端口", "Check the triple-target engine port"))
    try:
        async with httpx.AsyncClient(
            timeout=timeout, follow_redirects=False, trust_env=False
        ) as client:
            url = f"http://127.0.0.1:{port}{path}"
            response = await (client.get(url) if body is None else client.post(url, json=body))
            result = response.json()
    except (httpx.HTTPError, ValueError):
        raise EngineError(
            L("伤害计算暂时不可用", "Damage calculation is temporarily unavailable")
        ) from None
    if not response.is_success or not isinstance(result, dict):
        error = result.get("error") if isinstance(result, dict) else None
        detail = error.get("message") if isinstance(error, dict) else ""
        detail = " ".join(str(detail or "").split())[:120]
        message = L("暂时无法计算这组配装", "This loadout could not be calculated")
        raise EngineBuildError(f"{message} · {detail}" if detail else message)
    return result


def _fixed_inputs(weapon, spec, overrides):
    scanned = overrides is not None and "loadout_slots" in spec
    if scanned:
        check_options(
            spec, {k: v for k, v in overrides.items() if k not in {"evo_keys", "form_word"}}
        )
    else:
        check_options(spec, overrides)
    source = weapon.get("uniqueName")
    if (
        not source
        or weapon.get("_panel_derived")
        or weapon.get("category") == "Melee"
        or weapon["name"].startswith(("Kuva ", "Tenet ", "Coda "))
        or spec.get("form")
        not in (
            {None, "keep", "base", "incarnon", "alt_fire"} if scanned else {None, "keep", "base"}
        )
        or (spec.get("arcanes") and not scanned)
        or spec.get("stance")
        or spec.get("deployment")
        or spec.get("lich_bonus")
        or spec.get("heavy")
        or (spec.get("evo_keys") and not scanned)
        or spec.get("base_armor") is not None
        or spec.get("errors")
        or spec.get("enemy_miss")
    ):
        raise EngineError(
            L(
                "这组装备或战斗条件暂未接入固定复算",
                "These items or fight options are outside fixed replay",
            )
        )

    if not spec.get("enemy") and not (scanned and spec.get("faction") == "Grineer"):
        raise EngineError(L("固定复算需要指定敌人", "Fixed replay requires a named enemy"))

    if overrides is not None:
        slots = spec.get("confirmed_replay_slots")
        if slots is None:
            raise EngineError(
                L(
                    "固定复算需要完整确认槽位和卡片等级",
                    "Fixed replay requires confirmed slots and ranks",
                )
            )
        items = slots
    else:
        items = enumerate(spec["mods"])
    mods = []
    for slot, mod in items:
        path = mod.get("_source_unique_name")
        rank = mod.get("rank")
        if rank is None and overrides is None:
            rank = mod.get("max_rank")
        if not path or type(rank) is not int or not 0 <= rank <= mod["max_rank"]:
            raise EngineError(
                L("有 MOD 的身份或等级无法确认", "A MOD identity or rank is unconfirmed")
            )
        mods.append({"slot": slot, "path": path, "rank": rank})
    if len(mods) > 8 or len({row["path"] for row in mods}) != len(mods):
        raise EngineError(
            L("普通槽位最多八张且 MOD 不得重复", "Use up to eight distinct regular MODs")
        )
    return source, mods


async def portable_catalog(service, port):
    weapons, enemies, _ = await _portable_catalog(service, port)
    return weapons, enemies


async def _portable_catalog(service, port):
    async def fetch():
        result = await portable_request(port, "/api/catalog", timeout=8)
        if result.get("buildLabel") != PORTABLE_DATA or result.get("schemaVersion") != 3:
            raise EngineError(
                L("本机三靶引擎数据版本不匹配", "The local engine data version does not match")
            )
        # 只保留指令需要的武器及敌人身份。
        weapons = {
            row["path"]: {
                key: row[key]
                for key in ("path", "nameEn", "fireModes", "polarities", "incarnon")
                if key in row
            }
            for row in result["weapons"]
            if row.get("available") and row.get("buildAvailable")
        }
        enemies = {}
        for row in result["enemies"]:
            if row.get("complete") and row.get("scalingSupported"):
                enemies.setdefault(row["nameEn"], []).append(row["path"])
        equipment = {"utilityMods": {}, "arcanes": {}}
        for kind, names in equipment.items():
            for row in result.get(kind, ()):
                if row.get("nameEn") and row.get("path"):
                    names.setdefault(row["nameEn"], []).append(
                        {
                            key: row[key]
                            for key in ("path", "maxRank", "drain", "polarity")
                            if key in row
                        }
                    )
        return weapons, enemies, equipment

    return await service.portable_catalog.get_or_fetch(str(port), 600, fetch)


_REPLAY_POLARITIES = {
    "madurai": "AP_ATTACK",
    "naramon": "AP_TACTIC",
    "vazarin": "AP_DEFENSE",
    "zenurik": "AP_POWER",
    "unairu": "AP_WARD",
    "penjaga": "AP_PRECEPT",
    "umbra": "AP_UMBRA",
}


def _scan_slot_polarity(drain, raw, polarity):
    """容量相同即可复现；红槽的实际极性无法仅由容量唯一确定。"""
    if drain == raw:
        return "AP_UNIVERSAL", drain
    if polarity and drain == math.ceil(raw / 2):
        return polarity, drain
    if polarity and drain == int(raw * 1.25 + 0.5):
        mismatch = "AP_ATTACK" if polarity != "AP_ATTACK" else "AP_DEFENSE"
        # 截图红槽按最近整数显示，当前上游按向上取整校验容量。
        return mismatch, math.ceil(raw * 1.25)
    raise ScanReplayUnconfirmed(
        L("截图容量与引擎卡库不符", "Screenshot capacity disagrees with the engine catalog")
    )


def _scan_replay_equipment(spec, equipment):
    polarities = ["AP_UNIVERSAL"] * 8
    drains = {}
    for index, mod in spec["confirmed_replay_slots"]:
        raw = mod["base_drain"] + mod["rank"]
        drain = mod["scan_drain"]
        polarities[index], drains[index] = _scan_slot_polarity(
            drain, raw, _REPLAY_POLARITIES.get(mod.get("polarity"))
        )
    request: PortableReplayOptions = {"slotPolarities": polarities}
    for slot in spec["loadout_slots"]:
        kind = slot["kind"]
        if kind not in {"exilus", "arcane"} or slot["state"] != "installed":
            continue
        if type(slot.get("rank")) is not int or slot["rank"] < 0:
            raise ScanReplayUnconfirmed(
                L("扩展槽或赋能等级待核对", "Exilus or Arcane rank needs confirmation")
            )
        if kind == "exilus":
            name = slot.get("mod_name")
            rows = equipment.get("utilityMods", {}).get(name, ())
        else:
            arc = dc.find_arcane(slot["name"])
            name = (arc or {}).get("name")
            rows = equipment.get("arcanes", {}).get(name, ())
        if len(rows) != 1 or slot["rank"] > rows[0]["maxRank"]:
            raise ScanReplayUnconfirmed(
                L(
                    f"引擎卡库无法确认「{slot['name']}」及其等级",
                    f"The engine catalog could not confirm {name or slot['name']} at this rank",
                )
            )
        row = rows[0]
        selected = {"path": row["path"], "rank": slot["rank"]}
        if kind == "arcane":
            request["arcanes"] = [selected]
        else:
            request["exilus"] = selected
            raw = row["drain"] - row["maxRank"] + slot["rank"]
            polarity, drains[8] = _scan_slot_polarity(slot["drain"], raw, row.get("polarity"))
            request["specialSlotPolarities"] = {"exilus": polarity}
    return request, drains


_SCAN_PANEL_FIELDS = {
    "crit_chance": ("critChance", 100, 0.6),
    "crit_damage": ("critMultiplier", 1, 0.06),
    "status_chance": ("statusChance", 100, 0.6),
    "attack_speed": ("fireRate", 1, 0.06),
    "magazine": ("magazine", 1, 0.49),
    "reload": ("reloadTime", 1, 0.06),
}


def _matches_scan_panel(observed, stats):
    # 面板总伤和多重可能混有显示乘区与镀层的预计算层数。先核对独立可见属性，
    # 逐系伤害用占比核对构成，保留差异作诊断而不把它当成额外增益注入。
    scalars = [(key, *data) for key, data in _SCAN_PANEL_FIELDS.items() if key in observed]
    if len(scalars) < 2:
        return False
    for key, engine_key, multiplier, tolerance in scalars:
        value = stats.get(engine_key)
        if type(value) not in (int, float) or not math.isfinite(value):
            return False
        if abs(value * multiplier - observed[key]) > tolerance:
            return False
    rows = observed.get("damage_types", {})
    if len(rows) < 2:
        return True
    damage = {}
    for key, value in (stats.get("damageTypes") or {}).items():
        if value > 0:
            name = key.removeprefix("DT_").lower()
            damage[_PANEL_TYPE_ALIASES.get(name, name)] = value
    if not rows.keys() <= damage.keys():
        return False
    # 截图可裁掉下方若干伤害行，只比较双方均有的分项比例。
    seen_total = sum(rows.values())
    engine_total = sum(damage[k] for k in rows)
    return math.isclose(seen_total, engine_total, rel_tol=0.12) and all(
        abs(seen / seen_total - damage[key] / engine_total) <= 0.025 for key, seen in rows.items()
    )


def _scan_mode_label(row):
    if row.get("isIncarnonForm"):
        return L("灵化形态", "Incarnon form")
    if row.get("isAlternateFire"):
        return L("次要射击", "Alternate fire")
    if row["index"] == 0:
        return L("主射", "Primary fire")
    return L(f"模式 {row['index'] + 1}", f"Mode {row['index'] + 1}")


def _scan_mode_matches(mode, form):
    if form == "incarnon":
        return mode.get("isIncarnonForm")
    if form == "alt_fire":
        return mode.get("isAlternateFire") and not mode.get("isIncarnonForm")
    if form == "base":
        return mode["index"] == 0 and not (
            mode.get("isIncarnonForm") or mode.get("isAlternateFire")
        )
    return True


def _scan_context_candidates(catalog_weapon, spec):
    modes = [m for m in catalog_weapon.get("fireModes", ()) if m.get("available")]
    tiers = {
        f"tier{tier['tier']}": [option["id"] for option in tier["options"]]
        for tier in catalog_weapon.get("incarnon", ())
        if tier.get("selectable")
    }
    explicit = {}
    for code in spec.get("evo_keys") or ():
        key = f"tier{code.split('-', 1)[0]}"
        if key in explicit or code not in tiers.get(key, ()):
            raise ScanReplayUnconfirmed(
                L(
                    "进化编号无效或同一阶重复，格式如「进化 2-2」",
                    'The evolution code is invalid or repeated for a tier. Use a code such as "evolve 2-2".',
                )
            )
        explicit[key] = code
    alternatives = [
        ([explicit[key]] if key in explicit else options) for key, options in tiers.items()
    ]
    return modes, tiers, explicit, alternatives


def _scan_context_message(matches, modes, tiers, catalog_weapon, *, few_reads=False):
    if matches:
        modes_used = {mode["index"]: mode for mode, _ in matches}
        fixed = [
            values.pop()
            for key in tiers
            if len(values := {route[key] for _, route in matches}) == 1
        ]
        confirmed = (
            [_scan_mode_label(next(iter(modes_used.values())))] if len(modes_used) == 1 else []
        )
        options = {
            option["id"]: option
            for tier in catalog_weapon.get("incarnon", ())
            for option in tier["options"]
        }
        for code in fixed:
            label = options.get(code, {}).get("labelEn" if is_en() else "label")
            confirmed.append(f"{code} {label}" if label and label != code else code)
        if confirmed:
            line = L(
                "面板可确认 " + "、".join(confirmed) + "，其余选项待核对",
                "Panel matches " + ", ".join(confirmed) + "; other choices need confirmation",
            )
        else:
            line = L(
                "多套射击模式或进化的面板相同", "Several modes or evolution routes match the panel"
            )
    elif few_reads:
        line = L(
            "面板可用数值不足，暂不能反推射击模式或进化",
            "Too few usable panel stats to infer the mode or evolutions",
        )
    else:
        line = L(
            "面板与引擎候选对不上，射击模式或进化待核对",
            "Panel stats disagree with the engine candidates",
        )
    samples = matches[0][1] if matches else {key: values[0] for key, values in tiers.items()}
    if samples:
        example = " ".join(f"进化 {code}" for code in samples.values())
        example_en = " ".join(f"evolve {code}" for code in samples.values())
        example_mode = matches[0][0] if matches else modes[0]
        if example_mode.get("isIncarnonForm"):
            mode, mode_en = " 灵化", " incarnon"
        elif example_mode.get("isAlternateFire"):
            mode, mode_en = " 副开火", " alt fire"
        else:
            mode, mode_en = " 基础形态", " base form"
        line += L(
            f"。确认实际路线后可用「识卡伤害{mode} {example}」",
            f'. After verifying your choices, use "scandamage{mode_en} {example_en}"',
        )
    elif len(modes) != 1:
        line += L(
            "。可加「基础形态」「副开火」或「灵化」确认模式",
            '. Specify "base form", "alt fire" or "incarnon" to confirm the mode',
        )
    return ScanReplayUnconfirmed(line)


async def _resolve_scan_context(service, port, request, catalog_weapon, spec):
    modes, tiers, explicit, alternatives = _scan_context_candidates(catalog_weapon, spec)
    requested_modes = [m for m in modes if _scan_mode_matches(m, spec.get("form"))]
    if not requested_modes:
        raise ScanReplayUnconfirmed(
            L("所选射击模式不可用", "The selected fire mode is unavailable")
        )
    if len(explicit) == len(tiers) and len(requested_modes) == 1:
        return (
            requested_modes[0],
            explicit,
            "explicit" if explicit or spec.get("form") != "keep" else "single_mode",
        )
    panel = spec.get("scan_panel") or {}
    if sum(key in panel for key in _SCAN_PANEL_FIELDS) < 2:
        raise _scan_context_message([], modes, tiers, catalog_weapon, few_reads=True)
    if len(modes) * math.prod(map(len, alternatives)) > 48:
        raise ScanReplayUnconfirmed(
            L(
                "进化候选较多，请直接填写已选进化",
                "Specify your evolutions to narrow the candidate set",
            )
        )
    probe = {
        **request,
        "damageModel": "panel",
        "metric": "shotDamage",
        "headshotChance": 0.0,
        "enemy": "/Lotus/Types/Enemies/Orokin/OrokinHeavyFemaleAvatar",
        "level": 100,
        "steelPath": False,
        "windowSeconds": 12,
    }
    if "arcanes" in probe:
        probe["arcanes"] = [{**arc, "stacks": 0} for arc in probe["arcanes"]]
    key = json.dumps([port, probe, panel, modes, alternatives], sort_keys=True)

    async def fetch():
        matches = []
        deadline = asyncio.get_running_loop().time() + 16
        for choices in product(*alternatives):
            route = dict(zip(tiers, choices))
            for mode in modes:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise ScanReplayUnconfirmed(
                        L(
                            "面板核对超时，请手填进化后复算",
                            "Panel matching timed out; specify your evolutions",
                        )
                    )
                try:
                    result = await _finish_compute(
                        portable_request(
                            port,
                            "/api/simulate",
                            {**probe, "fireMode": mode["index"], "incarnonChoices": route},
                            timeout=min(4.0, remaining),
                        )
                    )
                except EngineError as exc:
                    raise ScanReplayUnconfirmed(
                        L(
                            "自动面板核对暂不可用，可手填已选模式和进化复算",
                            "Panel matching is temporarily unavailable. Specify your mode and evolutions to recalculate.",
                        )
                    ) from exc
                if (
                    result.get("buildLabel") != PORTABLE_DATA
                    or result.get("selection", {}).get("fireMode") != mode["index"]
                    or (result.get("incarnonChoices") or {}) != route
                ):
                    raise ScanReplayUnconfirmed(
                        L(
                            "引擎返回的进化与候选不符",
                            "The engine returned a different evolution route",
                        )
                    )
                if _matches_scan_panel(panel, result.get("moddedStats") or {}):
                    matches.append((mode, route))
        return matches

    matches = await service.portable_scan_context.get_or_fetch(key, 600, fetch)
    if len({mode["index"] for mode, _ in matches}) > 1 and len(requested_modes) == 1:
        matches = [entry for entry in matches if entry[0]["index"] == requested_modes[0]["index"]]
    if not matches or len({mode["index"] for mode, _ in matches}) != 1:
        raise _scan_context_message(matches, modes, tiers, catalog_weapon)
    inferred_mode, route = matches[0]
    mode = requested_modes[0] if len(requested_modes) == 1 else inferred_mode
    if len(matches) == 1:
        return mode, route, "panel" if mode == inferred_mode else "explicit"
    return mode, [candidate for _, candidate in matches], "optimized"


async def _best_scan_replay(port, request, routes, drains):
    """所有不能由面板区分的进化，以完全相同的配装、靶和窗口比较。"""
    best = None
    for route in routes:
        body = {**request, "incarnonChoices": route}
        result = await _finish_compute(portable_request(port, "/api/simulate", body))
        if (result.get("incarnonChoices") or {}) != route:
            raise ScanReplayUnconfirmed(
                L("引擎回放与选定的进化不符", "The replay differs from the selected evolutions")
            )
        window, validation = validate_fixed_replay(result, body)
        actual = {slot["slot"]: slot["drain"] for slot in validation["assignments"]}
        if result.get("exilus"):
            actual[8] = result["exilus"]["drain"]
        if actual != drains:
            raise ScanReplayUnconfirmed(
                L("回放容量与截图不一致", "Replay capacity differs from the screenshot")
            )
        if best is None or window["windowDps"] > best[0]:
            best = window["windowDps"], body, result
    return best[1], best[2]


async def portable_fixed_lines(host, port, weapon, spec, overrides=None):
    """按固定装备复算，识卡路径可使用已确认的辅助装备和进化。"""
    try:
        source, mods = await asyncio.to_thread(_fixed_inputs, weapon, spec, overrides)
        service = host.damage_engine
        scanned = overrides is not None and "loadout_slots" in spec
        async with service.replays.slot():
            await service.ensure_portable(port)
            if scanned:
                data, enemies, equipment = await _portable_catalog(service, port)
            else:
                data, enemies = await portable_catalog(service, port)
            enemy = spec["enemy"] or dc.find_enemy_exact("Heavy Gunner")
            paths = enemies.get(enemy["name"], [])
            catalog_weapon = data.get(source)
            if len(paths) != 1 or not catalog_weapon:
                raise EngineError(
                    L(
                        "该武器或敌人暂无法精确匹配",
                        "This weapon or enemy cannot be matched exactly",
                    )
                )
            request = portable_damage_request(source, paths[0], mods, spec)
            mode = None
            context = ""
            scan_capacity = None
            optimized_codes = ()
            if scanned:
                scan_capacity = spec.get("scan_capacity")
                extra, drains = _scan_replay_equipment(spec, equipment)
                request = with_portable_replay_options(request, extra)
                mode, route, context = await _resolve_scan_context(
                    service, port, request, catalog_weapon, spec
                )
                request["fireMode"] = mode["index"]
                routes = route if context == "optimized" else [route]
                request, result = await _best_scan_replay(port, request, routes, drains)
                if context == "optimized":
                    optimized_codes = tuple(
                        code
                        for tier, code in request["incarnonChoices"].items()
                        if len({candidate[tier] for candidate in routes}) > 1
                    )
            else:
                if not any(
                    m.get("available") and _scan_mode_matches(m, "base")
                    for m in catalog_weapon.get("fireModes", ())
                ):
                    raise EngineError(
                        L("该武器主射暂不可用", "Primary fire is unavailable for this weapon")
                    )
                result = await _finish_compute(portable_request(port, "/api/simulate", request))
                if overrides is not None and result.get("incarnonChoices"):
                    raise EngineError(
                        L("截图的进化选择未确认", "Incarnon choices are unconfirmed in the scan")
                    )
        return _fixed_reply(
            result,
            request,
            weapon,
            enemy,
            catalog_weapon,
            mode=mode,
            context=context,
            scan_capacity=scan_capacity,
            optimized_codes=optimized_codes,
        )
    except EngineError:
        raise
    except (KeyError, TypeError, ValueError):
        raise EngineError(
            L("本次未能算出配装伤害，请重试", "Could not estimate loadout damage. Try again.")
        ) from None


def validate_fixed_replay(result, request):
    given = {(row["slot"], row["path"], row["rank"]) for row in request["mods"]}
    if exilus := request.get("exilus"):
        equipped = result.get("exilus") or {}
        special = request.get("specialSlotPolarities") or {}
        if (
            equipped.get("incompatible")
            or any(equipped.get(key) != exilus[key] for key in ("path", "rank"))
            or ("exilus" in special and special["exilus"] != equipped.get("slotPolarity"))
        ):
            raise EngineError(L("扩展槽回放与配装不符", "Exilus slot does not match the build"))
        if equipped.get("catalogJoined"):
            given.add((8, exilus["path"], exilus["rank"]))
    if arcanes := request.get("arcanes"):
        given_arcanes = {(row["path"], row["rank"]) for row in arcanes}
        active = {
            (row["path"], row["rank"]) for row in result.get("arcanes", ()) if row.get("applied")
        }
        if active != given_arcanes:
            raise EngineError(L("赋能回放与配装不符", "Arcanes do not match the build"))
    validation = result["loadoutValidation"]
    assignments = validation["assignments"]
    used = {(row["slot"], row["path"], row["rank"]) for row in assignments}
    window = result["benchmarkTimeline"]
    context = window["context"]
    dps = window["windowDps"]
    if (
        result["buildLabel"] != PORTABLE_DATA
        or result["damageModel"] != "target"
        or result["selection"]["weapon"]["path"] != request["weapon"]
        or len(given) != len(assignments)
        or used != given
        or window["windowSeconds"] != request["windowSeconds"]
        or context["enemyPath"] != request["enemy"]
        or context["level"] != request["level"]
        or context["steelPath"] != request["steelPath"]
        or result["selection"]["fireMode"] != request["fireMode"]
        or (result.get("headshotMix") or {}).get("chance", 0) != request["headshotChance"]
        or type(dps) not in (int, float)
        or not math.isfinite(dps)
        or dps < 0
    ):
        raise EngineError(
            L(
                "暂时无法按所选的配装和敌人计算伤害",
                "Could not estimate damage with the selected build and enemy",
            )
        )
    return window, validation


PANEL_FIELDS = (
    "totalDamage",
    "critChance",
    "critMultiplier",
    "statusChance",
    "fireRate",
    "multishot",
)

_PANEL_TYPE_ALIASES = {
    "fire": "heat",
    "freeze": "cold",
    "poison": "toxin",
    "explosion": "blast",
    "radiant": "void",
    "sentient": "tau",
}
_PANEL_PHYSICAL_TYPES = frozenset(dc.PHYS_ALIASES.values())
_PANEL_ELEMENTAL_TYPES = frozenset((*dc.ELEM_ALIASES.values(), *dc.COMPOSITE.values()))


def _panel_damage_lines(damage_types, total, multishot, *, show_empty_groups=False):
    if not isinstance(damage_types, dict) or total <= 0:
        return []
    # 模拟器返回单弹丸分项。面板总伤害已乘同一套多重系数。
    multiplier = max(multishot, 1.0)
    amounts = {}
    for code, value in damage_types.items():
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            return []
        if value:
            name = code.removeprefix("DT_").lower()
            name = _PANEL_TYPE_ALIASES.get(name, name)
            kind = name if name in dc.TYPE_ZH else "other"
            amounts[kind] = amounts.get(kind, 0) + value * multiplier
    if not math.isclose(sum(amounts.values()), total, rel_tol=1e-6):
        return []

    physical, elemental, special = [], [], []
    for kind in (*dc.TYPE_ZH, "other"):
        if kind not in amounts:
            continue
        amount = amounts[kind]
        label = L("其他伤害", "Other damage") if kind == "other" else dc._ty(kind)
        if kind in _PANEL_PHYSICAL_TYPES:
            parts = physical
        elif kind in _PANEL_ELEMENTAL_TYPES:
            parts = elemental
        else:
            parts = special
        parts.append((label, amount))

    lines = []
    for label, parts, show_empty in (
        (L("物理", "Physical"), physical, show_empty_groups),
        (L("元素", "Elemental"), elemental, show_empty_groups),
        (L("特殊", "Special"), special, False),
    ):
        if parts:
            group_share = sum(amount for _, amount in parts) / total
            text = " / ".join(
                f"{name} {amount:,.1f} ({amount / total:.1%})" for name, amount in parts
            )
            lines.append(f"· {label} {group_share:.1%} · {text}")
        elif show_empty:
            lines.append(f"· {label} 0.0%")
    return lines


def weapon_panel_lines(stats, *, show_empty_groups=False, melee=False):
    if not stats:
        return []
    values = [stats.get(key) for key in PANEL_FIELDS]
    if any(type(value) not in (int, float) or not math.isfinite(value) for value in values):
        return []
    damage, crit, multiplier, status, rate, multishot = values
    radial = stats.get("damageComponent") == "radial"
    label = (
        L("范围爆炸伤害（单次命中）", "Explosion damage per hit") if radial else L("伤害", "Damage")
    )
    rate_label = dc.attack_rate_label(melee)
    rate_text = L(f"{rate_label} {rate:.2f}/秒", f"{rate_label} {rate:.2f}/s")
    if not melee:
        rate_text += L(f" · 多重射击 {multishot:.2f}", f" · Multishot {multishot:.2f}")
    return [
        L("◆ 武器面板", "◆ Weapon stats"),
        L(
            f"· {label} {damage:,.1f} · 暴击几率 {crit:.1%} · 暴击倍率 ×{multiplier:.2f}",
            f"· {label} {damage:,.1f} · Critical chance {crit:.1%} · Critical multiplier ×{multiplier:.2f}",
        ),
        L(
            f"· 触发几率 {status:.1%} · {rate_text}",
            f"· Status chance {status:.1%} · {rate_text}",
        ),
        *_panel_damage_lines(
            stats.get("damageTypes"),
            damage,
            1.0 if radial else multishot,
            show_empty_groups=show_empty_groups,
        ),
    ]


def _fixed_reply(
    result,
    request,
    weapon,
    enemy,
    catalog_weapon,
    *,
    mode=None,
    context="",
    scan_capacity=None,
    optimized_codes=(),
):
    from .portable_riven import enemy_label, evolution_lines

    window, validation = validate_fixed_replay(result, request)
    name = weapon["name"] if is_en() else weapon.get("zh") or weapon["name"]
    target = enemy_label(request.get("enemy"), enemy["name"], enemy.get("zh"))
    difficulty = L("钢铁之路", "Steel Path") if request["steelPath"] else L("普通", "Normal")
    hit = L("弱点", "Weak points") if request["headshotChance"] else L("躯干", "Body")
    secs = window["windowSeconds"]
    lines = [
        f"◆ {name} · {_scan_mode_label(mode) if mode else L('主射', 'Primary fire')}",
        f"· {target} Lv{request['level']} · {difficulty} · {hit}",
        L("· 计算：独立引擎", "· Engine: Standalone engine"),
        L(
            f"◆ {secs:g} 秒平均 DPS　{window['windowDps']:,.1f}",
            f"◆ Average DPS over {secs:g} s　{window['windowDps']:,.1f}",
        ),
    ]
    if context == "panel":
        lines.append(
            L(
                "· 射击模式与进化按截图面板核对",
                "· Mode and evolutions matched to the screenshot panel",
            )
        )
    elif context == "optimized":
        codes = ", ".join(optimized_codes)
        lines.append(
            L(
                f"· 面板无法区分的进化按当前目标选最高窗口 DPS · {codes}",
                f"· Same-panel evolutions selected by highest window DPS on this target · {codes}",
            )
        )
    elif context == "explicit":
        lines.append(
            L("· 射击模式或进化按指令设定", "· Mode or evolutions specified in the command")
        )
    cap = validation["capacity"]
    limit = validation["capacityLimit"]
    if not validation["valid"] and scan_capacity is None:
        lines.append(
            L(
                "※ 默认极性下超容量，实际装配需核对",
                "※ Over capacity with default polarities. Confirm your installed slots.",
            )
        )
    lines.extend(
        (
            L(
                f"· 截图容量 {scan_capacity[0]}/{scan_capacity[1]}",
                f"· Screenshot capacity {scan_capacity[0]}/{scan_capacity[1]}",
            )
            if scan_capacity is not None
            else L(
                f"· 默认极性容量 {cap}/{limit}", f"· Capacity with default polarities {cap}/{limit}"
            ),
            L(
                f"· 开火 {window['shots']} 次 · 装填 {window['reloads']} 次",
                f"· {window['shots']} shots · {window['reloads']} reloads",
            ),
        )
    )
    names = {
        row["path"]: (row.get("nameEn") if is_en() else row.get("nameZh")) or row["nameEn"]
        for row in result["selection"]["mods"]
    }
    for row in sorted(validation["assignments"], key=lambda mod: mod["slot"]):
        if row["slot"] >= 8 and mode:
            continue
        name = names[row["path"]]
        lines.append(L(f"· {name} · 等级 {row['rank']}", f"· {name} · Rank {row['rank']}"))
    if mode:
        for label, rows in (
            (L("扩展槽", "Exilus"), [result["exilus"]] if result.get("exilus") else []),
            (L("赋能", "Arcane"), result.get("arcanes") or []),
        ):
            for row in rows:
                name = (
                    (row.get("nameZh") if not is_en() else None) or row.get("nameEn") or row["path"]
                )
                lines.append(
                    L(
                        f"· {label} {name} · {row['rank']} 级",
                        f"· {label} {name} · Rank {row['rank']}",
                    )
                )
    lines.extend(weapon_panel_lines(result.get("moddedStats")))
    lines.extend(evolution_lines(catalog_weapon, result.get("incarnonChoices")))
    if result.get("unsupported") or window.get("unsupported"):
        lines.append(
            L("※ 部分效果未计入伤害", "※ Some effects were excluded from the damage estimate")
        )
    return lines


def engine_url(value: str) -> str:
    url = value.strip().rstrip("/")
    try:
        parsed = urlsplit(url)
        port = parsed.port
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.query
            or parsed.fragment
            or port == 0
            or any(ch.isspace() or ord(ch) < 32 or ch == "\\" for ch in url)
        ):
            raise ValueError
    except ValueError:
        raise EngineError(
            L("请填写有效的 HTTP 或 HTTPS 服务地址", "Use a valid HTTP or HTTPS service URL")
        ) from None
    return url


def _id(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower().replace("’", "'").replace("'", "")).strip("_")


def _unsupported():
    return EngineError(
        L(
            "这组手填增益或战斗条件暂未接入完整模拟，可切回内置估算",
            "These manual bonuses or fight options are unavailable in Simulation. Use Built-in Estimate.",
        )
    )


def check_options(spec: dict, overrides: dict | None) -> None:
    if overrides is not None:
        if overrides.keys() - _TARGET_FIELDS or overrides.get("enemy_miss"):
            raise _unsupported()
        return
    # 手写指令的数值已经聚合。用同一解析器扣除装备自带加成，防止手填值在模拟端静默消失。
    names = [m["name"] for m in spec["mods"] if m.get("name")]
    for arc in spec["arcanes"]:
        names.extend(("赋能", arc["name"]))
    items_only, _ = dc.parse_args(names)
    for key, value in spec.items():
        if key not in _TARGET_FIELDS | _BUILD_FIELDS and value != items_only.get(key):
            raise _unsupported()


def _target(spec: dict) -> tuple[dict, str]:
    enemy = spec.get("enemy")
    if enemy is None and spec["faction"] == "Grineer":
        enemy = dc.find_enemy("Heavy Gunner")
    faction = (enemy or {}).get("faction") or spec["faction"]
    fac = _id(faction)
    scaling = {
        "grineer": "grineer",
        "kuva_grineer": "grineer",
        "corpus": "corpus",
        "infested": "infested",
        "orokin": "corrupted",
        "corrupted": "corrupted",
        "techrot": "techrot",
    }.get(fac, "unaffiliated")
    base = enemy or {}
    armor = spec.get("base_armor")
    if armor is None:
        armor = base.get("base_armor") if enemy else dc.DEFAULT_BASE_ARMOR.get(faction, 0)
    hp = float(base.get("base_health") or 700)
    name = base.get("name") or f"{faction} test target"
    label = (base.get("zh") or name) if not is_en() else name
    if not enemy:
        label = L(f"{faction} 合成靶（基准血量 700）", f"{faction} test target (700 base health)")
    body = [
        {"name": "body", "multiplier": 1.0},
        {
            "name": "head",
            "multiplier": float(base.get("head_mul", 3.0)),
            "is_head": True,
            "crit_bonus": base.get("head_crit", True),
        },
    ]
    return {
        "id": "plugin_target",
        "name": name,
        "synthetic": not enemy,
        "faction": fac,
        "scaling_faction": scaling,
        "body_parts": body,
        "stats": {
            "base_level": base.get("base_level", 1),
            "health": hp,
            "armor": armor or 0,
            "shield": base.get("base_shield") or 0,
            "overguard": 0,
            "affinity": 0,
        },
    }, label


def simulation_request(weapon: dict, spec: dict) -> tuple[dict, str]:
    name = weapon.get("name") or ""
    if not name or weapon.get("_panel_derived") or spec.get("evo_keys") or spec.get("deployment"):
        raise _unsupported()
    if name.startswith(("Kuva ", "Tenet ", "Coda ")) and not spec.get("lich_bonus"):
        raise EngineError(
            L(
                "完整模拟需要已确认的效价种类和数值",
                "Simulation requires a confirmed valence element and value",
            )
        )
    ids = []
    for mod in spec["mods"]:
        card = _id(mod["name"])
        rank = mod.get("rank")
        if rank is not None and rank < mod["max_rank"]:
            card = f"{card}@{rank}"
        ids.append(card)
    arcane, arcane_rank = [], []
    for arc in spec["arcanes"]:
        # 识卡装着但等级不明的赋能只留在 loadout_slots，手写指令按卡库等级。
        rank = arc.get("rank", arc.get("max_rank"))
        if rank is None:
            raise EngineError(L("赋能等级未确认", "Unconfirmed Arcane rank"))
        arcane.append(_id(arc["name"]))
        arcane_rank.append(rank)
    form = spec.get("form")
    if form not in {None, "keep", "base"}:
        raise _unsupported()
    target, label = _target(spec)
    req = {
        "weapon": _id(name),
        "mods": ids,
        "arcane": arcane,
        "arcane_rank": arcane_rank,
        "form": "heavy" if spec["heavy"] and weapon.get("category") == "Melee" else "base",
        "evolutions": [],
        "evolutions_as_given": True,
        "enemy": target["id"],
        "custom_enemies": [target],
        "level": max(1, min(9999, spec["level"])),
        "headshot_pct": 100 if spec["headshot"] else 0,
        "steel_path": spec["steel_path"],
        "eximus": False,
        "assume_max": False,
        "infinite_ammo": True,
        "distance": 0,
        "seed": 1,
        "runs": 16,
        "duration": max(1, min(60, spec["fire_seconds"])),
    }
    valence = spec.get("lich_bonus")
    if valence:
        if not valence.get("element"):
            raise EngineError(L("效价元素未指定", "Specify the valence element"))
        req.update(valence_element=valence["element"], valence_bonus=valence["pct"] / 100)
    return req, label


async def _request(cfg: dict, path: str, body: dict | None = None, timeout=12.0):
    url = engine_url(str(cfg.get("damage_engine_url") or DEFAULT_URL))
    token = cfg.get("damage_engine_token") or ""
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False, trust_env=False) as c:
            reply = await (
                c.get(url + path, headers=headers)
                if body is None
                else c.post(url + path, json=body, headers=headers)
            )
        data = reply.json()
    except (httpx.HTTPError, ValueError):
        raise EngineError(
            L(
                "完整模拟服务无法连接或响应无效",
                "Simulation service unreachable or returned invalid data",
            )
        ) from None
    if reply.status_code == 401:
        raise EngineError(L("完整模拟令牌未通过验证", "Simulation service rejected the token"))
    if reply.status_code == 404 and path in {SEARCH_PATH, SEARCH_META_PATH}:
        raise EngineError(
            L("请更新所连引擎以启用配卡搜索", "Update the connected engine to enable build search")
        )
    if not isinstance(data, dict) or not reply.is_success or data.get("ok") is not True:
        if reply.status_code == 400 and isinstance(data, dict):
            # 引擎的拒绝原因只用于诊断，不能释放响应中的令牌或任意长文本。
            error = str(data.get("error") or "")[:150]
            if token:
                error = error.replace(token, "…")
            raise EngineBuildError(
                L("完整模拟拒绝配装", "Simulation rejected the build")
                + " · "
                + " ".join(error.split())[:120]
            )
        raise EngineError(L("完整模拟服务暂时不可用", "Simulation service unavailable"))
    return data


def _version(data: dict) -> dict:
    version, revision = data.get("version", ""), data.get("data_revision", "")
    if (
        data.get("engine") != NAME
        or not isinstance(version, str)
        or not _VERSION.fullmatch(version)
        or not isinstance(revision, str)
        or not _REVISION.fullmatch(revision)
    ):
        raise EngineError(
            L("服务身份或版本回执不匹配", "Unexpected service or missing engine version")
        )
    return {"version": version, "data_revision": revision}


async def _finish_compute(request):
    # 断开 HTTP 客户端不会停止服务里的计算。调用方取消时也要等本次计算收尾再让位。
    task = asyncio.create_task(request)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:  # noqa: BLE001 - 保留调用方取消，消费结束的请求异常
                break
        if not task.cancelled():
            task.exception()
        raise


async def _run_simulation(cfg: dict, request: dict):
    return await _finish_compute(_request(cfg, "/api/simulate", request, timeout=32.0))


async def search_request(host, cfg, path, body):
    """托管版直接调用同一冻结引擎的搜索入口，外接版沿用 HTTP。"""
    timeout = 70 if path == SEARCH_PATH else 12
    service = getattr(host, "damage_engine", None)
    if (
        service is not None
        and service.installed()
        and engine_url(str(cfg.get("damage_engine_url") or DEFAULT_URL)) == DEFAULT_URL
    ):
        return await service.search_request(path, body, timeout=timeout)
    return await _finish_compute(_request(cfg, path, body, timeout=timeout))


@asynccontextmanager
async def simulation_session(host, event=None, *, managed=True):
    """整组模拟共用一次排位和同一份配置。"""
    service = getattr(host, "damage_engine", None)
    if service is None:
        yield dict(getattr(host, "cfg", {}) or {})
        return

    async def notify(position):
        await send_queue_position(
            event, getattr(host, "_messaging", None), L("完整模拟", "Simulation"), position
        )

    async with service.simulations.slot(notify):
        cfg = dict(getattr(host, "cfg", {}) or {})
        if managed:
            await service.ensure_running(cfg)
        yield cfg


async def simulate(host, weapon: dict, spec: dict, overrides: dict | None = None, *, event=None):
    check_options(spec, overrides)
    req, label = simulation_request(weapon, spec)
    async with simulation_session(host, event) as cfg:
        return await _run_simulation(cfg, req), req, label


async def simulation_lines(host, weapon, spec, overrides=None, *, event=None):
    try:
        result, req, target = await simulate(host, weapon, spec, overrides, event=event)
        dps, kills = float(result["dps"]), float(result["kills"])
    except (EngineError, QueueUnavailable) as exc:
        return [L("◆ 完整模拟不可用", "◆ Simulation unavailable"), f"· {exc}"], False
    except (ValueError, TypeError, KeyError):
        return [
            L("◆ 完整模拟回执缺少伤害数据", "◆ Simulation returned incomplete damage data")
        ], False
    name = (weapon.get("zh") or weapon["name"]) if not is_en() else weapon["name"]
    shot = L("重击", "heavy attack") if req["form"] == "heavy" else L("默认攻击", "default attack")
    lines = [
        L(f"◆ {name}｜完整模拟", f"◆ {name} | Simulation"),
        L(f"· 目标 {target} {req['level']} 级｜{shot}", f"· {target} Lv {req['level']} | {shot}"),
        L(
            f"· {req['duration']:g} 秒 × {req['runs']} 轮｜平均 DPS {dps:,.1f}｜平均击杀 {kills:,.2f}",
            f"· {req['duration']:g}s × {req['runs']} runs | mean DPS {dps:,.1f} | mean kills {kills:,.2f}",
        ),
    ]
    for arc, arc_id, rank in zip(spec["arcanes"], req["arcane"], req["arcane_rank"]):
        shown = arc["name"] if is_en() else arc.get("zh") or arc["name"]
        lines.append(L(f"· {shown} {rank} 级", f"· {shown} rank {rank}"))
        if arc_id == "melee_exposure":
            lines.append(
                L(
                    "※ 此版引擎按四层常驻计算近战·暴露",
                    "※ This engine models Melee Exposure at four permanent stacks.",
                )
            )
    if spec.get("loadout_slots"):
        for s in spec["loadout_slots"]:
            if s["kind"] == "arcane" and s["state"] == "installed" and s["rank"] is None:
                raw = s["name"]
                en = (dc.find_arcane(raw) or {}).get("name", raw)
                lines.append(
                    L(
                        f"※ {raw}等级未知，本次仅模拟已确认配装",
                        f"※ {en} rank is unknown. Confirmed equipment only.",
                    )
                )
        if spec.get("simulation_panel_uncertain"):
            lines.append(
                L(
                    "※ 原图面板还有差值，模拟使用已确认槽位和引擎基础数据",
                    "※ Screenshot stats differ. Simulation uses the confirmed slots and engine base stats.",
                )
            )
    return lines, True


class EngineService:
    """按需下载固定版服务，托管进程由插件生命周期管理。"""

    def __init__(self, data_dir: Path):
        self.root = data_dir / "damage-engine"
        self.release = self.root / RELEASE
        self.task = None
        self.child = None
        self.portable_child = None
        self.portable_child_port = 0
        self._configured_port: int | None = None
        self._installing_port = 0
        self.portable_release = self.root / PORTABLE_TAG
        self.token = None
        self.closed = False
        self.error = ""
        self.lock = asyncio.Lock()
        self.simulations = RequestQueue(workers=1, max_waiting=4)
        self.riven_build_cache = TTLCache(maxsize=32)
        self.portable_gains = TTLCache(maxsize=32)
        self.portable_catalog = TTLCache(maxsize=2)
        self.portable_scan_context = TTLCache(maxsize=24)
        self.replays = RequestQueue(workers=1, max_waiting=2)

    @property
    def binary(self) -> Path:
        return self.release / f"target/release/{NAME}"

    def installed(self) -> bool:
        return (self.release / "server.py").is_file() and (
            self.release / f"target/release/{NAME}"
        ).is_file()

    def status(self, cfg=None) -> dict:
        if portable_port(cfg):
            return {
                "source_url": PORTABLE_SOURCE_URL,
                "installed": self.portable_installed(),
                "running": self.task is not None and not self.task.done(),
                "started": self.portable_child is not None
                and self.portable_child.returncode is None,
                "error": self.error,
                "release": PORTABLE_FINGERPRINT,
                "build": PORTABLE_DATA,
                "backend": "portable",
                "local_bundle": (self.root / "portable-runtime.zip").is_file(),
                "download_supported": portable_asset() is not None,
            }
        return {
            "source_url": PORTABLE_SOURCE_URL,
            "installed": self.installed(),
            "running": self.task is not None and not self.task.done(),
            "started": self.child is not None and self.child.returncode is None,
            "error": self.error,
            "release": RELEASE,
        }

    async def search_request(self, path, body, *, timeout):
        packet = json.dumps({"path": path, "body": body}, allow_nan=False).encode() + b"\n"
        try:
            raw = await self._run(
                self.release,
                str(self.release / f"target/release/{NAME}"),
                timeout=timeout,
                input_bytes=packet,
            )
            result = json.loads(raw)["response"]
            if not isinstance(result, dict):
                raise ValueError("missing response")
        except asyncio.TimeoutError:
            raise EngineError(L("配卡搜索超时", "Build search timed out")) from None
        except (ValueError, OSError, KeyError):
            raise EngineError(
                L("本机引擎搜索服务不可用", "Managed build search is unavailable")
            ) from None
        if result.get("ok") is False:
            raise EngineBuildError(
                L("完整引擎拒绝搜索输入", "Engine rejected the search input")
                + " · "
                + " ".join(str(result.get("error") or "").split())[:120]
            )
        return result

    async def check(self, cfg: dict) -> dict:
        if port := portable_port(cfg):
            await self.ensure_portable(port)
            data = await self._portable_health(port, require_fingerprint=self.portable_installed())
            return {
                **data,
                "version": data.get("engineFingerprint")
                or L("服务未上报指纹", "Fingerprint unavailable"),
                "data_revision": data["buildLabel"],
            }
        async with self.lock:
            if self.portable_child is not None:
                await self._stop(self.portable_child)
                self.portable_child = None
        await self.ensure_running(cfg)
        version = _version(await _request(cfg, "/healthz", timeout=4))
        # 健康接口公开版本，计算接口再证明令牌能实际使用。
        await _request(cfg, "/api/panel", {"weapon": "braton_prime", "mods": []}, timeout=6)
        return {"ok": True, **version}

    def install(self, cfg: dict) -> dict:
        if port := portable_port(cfg):
            if not self.closed and (self.task is None or self.task.done()):
                self.error = ""
                self._installing_port = port
                self.task = asyncio.create_task(self._install_portable(cfg, port))
            return self.status(cfg)
        if self.closed or (self.task is not None and not self.task.done()) or self.installed():
            return self.status()
        if os.name != "posix" or not all(
            shutil.which(cmd) for cmd in ("git", "bash", "cargo", "rustc")
        ):
            self.error = "自动部署需要在插件主机准备 Git、Bash 和 Rust 1.97 或更新版本"
            return self.status()
        self.error = ""
        self._installing_port = 0
        self.task = asyncio.create_task(self._install(cfg))
        return self.status()

    def portable_installed(self):
        return (self.portable_release / "manifest.json").is_file()

    async def _install_portable(self, cfg, port):
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            if not self.portable_installed():
                with TemporaryDirectory(prefix="portable-install-", dir=self.root) as tmp:
                    dest = Path(tmp) / "runtime"
                    dest.mkdir()
                    bundle = self.root / "portable-runtime.zip"
                    expected = (
                        str(cfg.get("damage_engine_portable_bundle_sha256") or "").strip().lower()
                    )
                    async with httpx.AsyncClient(
                        follow_redirects=True, timeout=httpx.Timeout(20.0, connect=6.0)
                    ) as client:
                        if not bundle.is_file():
                            asset = portable_asset()
                            if asset is None:
                                raise ValueError(
                                    "公开运行包支持 Python 3.12 的 Linux x86_64 与 macOS arm64"
                                )
                            name, expected = asset
                            bundle = Path(tmp) / name
                            await download_file(
                                client, f"{PORTABLE_DOWNLOADS}/{name}", bundle, MAX_BUNDLE_SIZE
                            )
                        inputs, remaining = await asyncio.to_thread(
                            unpack, bundle, dest, expected, PORTABLE_FINGERPRINT, PORTABLE_DATA
                        )
                        if inputs:
                            await asyncio.wait_for(
                                download_inputs(client, dest, inputs, remaining), 100
                            )
                    raw = await self._run(
                        dest, sys.executable, "launch.py", "--probe", input_bytes=b"", timeout=25
                    )
                    probe = json.loads(raw)
                    if (
                        probe.get("engineFingerprint") != PORTABLE_FINGERPRINT
                        or probe.get("buildLabel") != PORTABLE_DATA
                    ):
                        raise ValueError("运行包探针的版本不符")
                    dest.rename(self.portable_release)
            await self.ensure_portable(port)
        except (
            EngineError,
            httpx.HTTPError,
            ValueError,
            asyncio.TimeoutError,
            OSError,
            KeyError,
            zipfile.BadZipFile,
        ) as exc:
            self.error = f"完整模拟引擎安装未完成（{exc}）"

    async def _portable_health(self, port, *, require_fingerprint):
        value = await portable_request(port, "/api/health", timeout=0.8)
        if (
            value.get("ok") is not True
            or value.get("buildLabel") != PORTABLE_DATA
            or value.get("schemaVersion") != 3
            or (require_fingerprint and value.get("engineFingerprint") != PORTABLE_FINGERPRINT)
        ):
            raise EngineError(
                L("本机三靶版本不匹配", "The local triple-target version does not match")
            )
        return value

    async def reconfigure_portable(self, port):
        """让过期请求退出，停止属于旧端口的安装或本机进程。"""
        self._configured_port = port
        task = self.task
        if self._installing_port and self._installing_port != port and task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        async with self.lock:
            if self.portable_child is not None and self.portable_child_port != port:
                await self._stop(self.portable_child)
                self.portable_child = None
                self.portable_child_port = 0

    async def ensure_portable(self, port):
        async with self.lock:
            if self.closed:
                raise EngineError(L("引擎正在关闭", "The engine is shutting down"))
            if self._configured_port is not None and port != self._configured_port:
                raise EngineError(L("完整模拟设置已更新", "Full simulation settings have changed"))
            if not self.portable_installed():
                return
            child = self.portable_child
            if child is not None and child.returncode is None and self.portable_child_port == port:
                return
            try:
                await self._portable_health(port, require_fingerprint=True)
            except EngineError:
                pass
            else:
                return
            if child is not None:
                await self._stop(child)
            try:
                with (self.root / "install.log").open("ab") as log:
                    self.portable_child = await asyncio.create_subprocess_exec(
                        sys.executable,
                        "launch.py",
                        "--port",
                        str(port),
                        cwd=self.portable_release,
                        start_new_session=True,
                        stdout=log,
                        stderr=log,
                    )
            except OSError:
                raise EngineError(
                    L("本机三靶服务无法启动", "Cannot start the local triple-target service")
                ) from None
            self.portable_child_port = port
            now = asyncio.get_running_loop().time
            deadline = now() + 20
            while now() < deadline:
                try:
                    await self._portable_health(port, require_fingerprint=True)
                    return
                except EngineError:
                    if self.portable_child.returncode is not None:
                        break
                    await asyncio.sleep(0.25)
            await self._stop(self.portable_child)
            raise EngineError(
                L(
                    "本机三靶服务启动检查失败",
                    "The local triple-target service failed its startup check",
                )
            )

    async def _install(self, cfg: dict):
        phase = "下载引擎源码"
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            with TemporaryDirectory(prefix="install-", dir=self.root) as tmp:
                dest = Path(tmp) / "source"
                dest.mkdir()
                await self._run(dest, "git", "init", "--quiet")
                await self._run(
                    dest, "git", "fetch", "--depth=1", "--no-tags", REPO + ".git", RELEASE
                )
                await self._run(dest, "git", "checkout", "--quiet", "--detach", RELEASE)
                phase = "编译引擎"
                await self._run(dest, "bash", "build.sh", timeout=1200)
                phase = "验证引擎版本"
                raw = await self._run(
                    dest,
                    str(dest / f"target/release/{NAME}"),
                    input_bytes=b'{"path":"/healthz","body":{}}\n',
                )
                _version(json.loads(raw)["response"])
                if not self.installed():
                    dest.rename(self.release)
            phase = "启动本机引擎"
            local_cfg = dict(cfg)
            if engine_url(str(local_cfg.get("damage_engine_url") or DEFAULT_URL)) != DEFAULT_URL:
                # 已有服务的令牌只用于该服务。同机部署固定使用回环地址。
                local_cfg = {"damage_engine_url": DEFAULT_URL}
            await self.check(local_cfg)
        except EngineError as exc:
            self.error = f"{phase}失败（{exc}）"
        except (ValueError, OSError, asyncio.TimeoutError, KeyError) as exc:
            self.error = f"{phase}失败（{type(exc).__name__}）"

    async def _run(self, cwd, *args, timeout=90, input_bytes=None):
        with (self.root / "install.log").open("ab") as log:
            proc = await asyncio.create_subprocess_exec(
                *args,
                cwd=cwd,
                start_new_session=True,
                stdin=asyncio.subprocess.PIPE
                if input_bytes is not None
                else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE if input_bytes is not None else log,
                stderr=log,
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            )
            try:
                out, _ = await asyncio.wait_for(proc.communicate(input_bytes), timeout)
            finally:
                if proc.returncode is None:
                    await self._stop(proc)
            if proc.returncode:
                raise ValueError("引擎构建子进程失败")
        return out

    @staticmethod
    async def _stop(proc):
        if proc.returncode is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await proc.wait()

    async def ensure_running(self, cfg):
        if (
            not self.installed()
            or engine_url(str(cfg.get("damage_engine_url") or DEFAULT_URL)) != DEFAULT_URL
        ):
            return
        async with self.lock:
            if self.closed:
                raise EngineError("伤害引擎管理器正在关闭")
            token = cfg.get("damage_engine_token") or ""
            if self.child is not None and self.child.returncode is None and token != self.token:
                await self._stop(self.child)
            try:
                _version(await _request(cfg, "/healthz", timeout=0.6))
                return
            except EngineError:
                if self.child is not None and self.child.returncode is None:
                    return
            try:
                env = {**os.environ, "WARFRAME_ENGINE_TOKEN": token}
                self.child = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "server.py",
                    cwd=self.release,
                    env=env,
                    start_new_session=True,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                self.token = token
            except OSError:
                raise EngineError("独立引擎服务无法启动") from None
            for _ in range(15):
                try:
                    _version(await _request(cfg, "/healthz", timeout=0.6))
                    return
                except EngineError:
                    if self.child.returncode is not None:
                        break
                    await asyncio.sleep(0.1)
            raise EngineError(
                L("本机独立引擎启动失败", "Managed simulation service failed to start")
            )

    async def close(self):
        self.closed = True
        self.simulations.close()
        self.replays.close()
        if self.task is not None and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        async with self.lock:
            if self.child is not None:
                await self._stop(self.child)
            if self.portable_child is not None:
                await self._stop(self.portable_child)
