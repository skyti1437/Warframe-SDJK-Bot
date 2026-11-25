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
from dataclasses import dataclass
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
from .portable_install import PORTABLE_ASSETS, download_file, download_inputs, unpack
from .request_queue import QueueUnavailable, RequestQueue, send_queue_position

REPO = "https://github.com/skyti1437/warframe-damage-engine"
RELEASE = "2419c5c76a83aae34428fafc5da2eee21ec16093"
DEFAULT_URL = "http://127.0.0.1:8788"
PORTABLE_FINGERPRINT = "535ed49f5a51149d"
PORTABLE_DATA = "2026.08.13.13.24"
PORTABLE_TAG = f"portable-{PORTABLE_FINGERPRINT}"
PORTABLE_SOURCE_URL = f"{REPO}/tree/{PORTABLE_TAG}"
PORTABLE_DOWNLOADS = (
    f"https://raw.githubusercontent.com/skyti1437/warframe-damage-engine/{PORTABLE_TAG}/downloads"
)
NAME = "warframe-damage-engine"
SEARCH_PATH = "/api/compute/optimize"
SEARCH_META_PATH = "/api/meta"
_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][a-zA-Z0-9.-]+)?\Z")
_REVISION = re.compile(r"[0-9a-f]{40}\Z")
_TARGET_FIELDS = {
    "enemy",
    "enemy_miss",
    "faction",
    "level",
    "headshot",
    "steel_path",
    "base_armor",
    "overguard",
    "overguard_mode",
    "shield_mode",
    "combat_options",
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


class EngineVersionMismatch(EngineError):
    """服务回执明确给出了不兼容的构建身份。"""


class EngineTimeout(EngineError):
    """已连接引擎，等待结果超时。"""


class EngineUnreachable(EngineError):
    """无法连接本机引擎。"""


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
    modelSpread: bool
    cloaked: bool
    triggerPolicy: Literal["emergent", "assumed_max"]
    combatState: dict[str, object]
    detonationDelaySeconds: float
    detonationBatchSize: int


class PortableEnemyEnhancements(TypedDict, total=False):
    eximus: bool
    archimedea: bool
    empowered: bool
    noOverguard: bool
    shieldOverride: float | None
    playerCount: int
    magneticStacks: int


class PortableAutoExilus(TypedDict):
    mode: Literal["auto"]


class _PortableOptions(TypedDict, total=False):
    arcanes: list[PortableArcane]
    incarnonChoices: dict[str, str]
    innateElement: str
    innateElementValue: float
    kitgunParts: list[str]
    scenario: PortableCombatScenario


class PortableBenchmarkTarget(TypedDict, total=False):
    enemy: str
    level: int
    steelPath: bool
    headshotChance: float
    windowSeconds: float
    enemyEnhancements: PortableEnemyEnhancements
    damageModel: Literal["target", "custom"]
    defenseSnapshot: dict


def validate_benchmark_target(target):
    if (
        not isinstance(target, dict)
        or target.keys() - PortableBenchmarkTarget.__optional_keys__
        or not {
            "enemy",
            "level",
            "steelPath",
            "headshotChance",
            "windowSeconds",
            "enemyEnhancements",
        }
        <= target.keys()
    ):
        raise EngineError(L("靶标参数有误", "Invalid benchmark target"))


class PortableAnalysisOptions(_PortableOptions, total=False):
    exilus: PortableRankedEquipment | PortableAutoExilus
    benchmarkTarget: PortableBenchmarkTarget


class PortableReplayOptions(_PortableOptions, total=False):
    exilus: PortableRankedEquipment
    stancePath: str
    slotPolarities: list[str | None]
    specialSlotPolarities: dict[str, str | None]
    armourStrip: float
    elementalArmourStrip: float
    externalBuffs: list[dict[str, object]]
    forcedProcs: dict[str, float]


def _field_error(label, zh, en):
    return EngineError(L(f"{label} {zh}", f"{label} {en}"))


def _bounded_number(value, *, low=0, high=None):
    try:
        return (
            type(value) in (int, float)
            and math.isfinite(value)
            and value >= low
            and (high is None or value <= high)
        )
    except OverflowError:
        return False


def _reject_custom_defense(values):
    if values.get("base_armor") is not None:
        raise _field_error(
            L("基准护甲", "Base armor"),
            "暂不支持手动覆盖",
            "cannot be overridden in portable replay",
        )
    if values.get("overguard") is not None:
        raise EngineError(
            L(
                "超宏防护 暂不支持手动覆盖，可用「有超宏」按敌人原生数值缩放",
                'Overguard cannot be overridden in portable replay. Use "overguard" to scale the native pool.',
            )
        )


def _validate_portable_context(values, *, analysis=False):
    _reject_custom_defense(values)
    if "benchmarkTarget" in values:
        validate_benchmark_target(values["benchmarkTarget"])
    scenario = values.get("scenario", {})
    if not isinstance(scenario, dict) or scenario.keys() - PortableCombatScenario.__optional_keys__:
        raise EngineError(
            L("战斗条件含有未知选项或格式有误", "Unknown or malformed combat options")
        )
    for key, label in (
        ("aiming", L("瞄准", "Aiming")),
        ("heavyAttack", L("重击", "Heavy attack")),
        ("incarnonCycle", L("灵化循环", "Incarnon cycle")),
        ("cloaked", L("隐匿", "Cloaking")),
        ("modelSpread", L("散布", "Spread modeling")),
    ):
        if key in scenario and type(scenario[key]) is not bool:
            raise _field_error(label, "须为开启或关闭", "must be a boolean")
    for key, label in (
        ("distance", L("距离", "Distance")),
        ("nearestEnemyDistance", L("最近敌人距离", "Nearest enemy distance")),
        ("detonationDelaySeconds", L("引爆延迟", "Detonation delay")),
    ):
        if key in scenario and not _bounded_number(scenario[key]):
            unit = L("秒", "seconds") if key == "detonationDelaySeconds" else L("米", "meters")
            raise _field_error(
                label, f"须为有限非负数（{unit}）", f"must be a finite nonnegative number in {unit}"
            )
    if "detonationBatchSize" in scenario and (
        type(scenario["detonationBatchSize"]) is not int
        or not 1 <= scenario["detonationBatchSize"] <= 8
    ):
        raise EngineError(
            L("引爆批量须为 1–8 的整数", "Detonation batch size must be an integer from 1 to 8")
        )
    if "comboTier" in scenario and (
        type(scenario["comboTier"]) is not int or not 0 <= scenario["comboTier"] <= 11
    ):
        raise _field_error(
            L("连击档位", "Combo tier"), "须为 0–11 的整数", "must be an integer from 0 to 11"
        )
    if "triggerPolicy" in scenario and scenario["triggerPolicy"] not in ("emergent", "assumed_max"):
        raise EngineError(L("触发策略有误", "Invalid trigger policy"))
    state = scenario.get("combatState", {})
    if not isinstance(state, dict):
        raise EngineError(L("战斗状态格式有误", "Combat state must be an object"))
    if "gaugeChargingHeadshots" in state and type(state["gaugeChargingHeadshots"]) is not bool:
        raise _field_error(
            L("攒条全弱点", "Weak-point charge"), "须为开启或关闭", "must be a boolean"
        )
    for key, label in (
        ("armourStrip", L("剥甲比例", "Armor strip fraction")),
        ("elementalArmourStrip", L("元素剥甲比例", "Elemental armor strip fraction")),
        ("headshotChance", L("弱点命中率", "Weak-point hit chance")),
    ):
        if key in values and not _bounded_number(values[key], high=1):
            raise _field_error(label, "须在 0–1 之间", "must be between 0 and 1")
    if "innateElementValue" in values and not _bounded_number(
        values["innateElementValue"], low=0.25, high=0.60
    ):
        raise _field_error(
            L("固有元素数值", "Innate element value"),
            "须为 0.25–0.60 的比例",
            "must be a fraction between 0.25 and 0.60",
        )
    if "exilus" in values:
        exilus = values["exilus"]
        if analysis and exilus == {"mode": "auto"}:
            return
        if (
            not isinstance(exilus, dict)
            or exilus.keys() != {"path", "rank"}
            or not _ranked_equipment(exilus)
        ):
            raise EngineError(L("扩展槽装备或等级有误", "Invalid Exilus equipment or rank"))


def _ranked_equipment(item):
    return (
        isinstance(item, dict)
        and isinstance(item.get("path"), str)
        and bool(item["path"].strip())
        and type(item.get("rank")) is int
        and item["rank"] >= 0
    )


def _with_portable_options(request, options, contract):
    """合入内部已规范化字段，并保护武器、普通槽、目标和牌面等既有输入。"""
    options = {} if options is None else options
    if not isinstance(options, dict):
        raise EngineError(L("引擎可选参数须为对象", "Engine options must be an object"))
    _reject_custom_defense(options)
    if options.keys() - contract.__optional_keys__ or any(
        key in request and request[key] != value for key, value in options.items()
    ):
        raise EngineError(
            L("引擎可选参数与既定工况不符", "Engine options conflict with the request")
        )
    merged = {**request, **options}
    _validate_portable_context(merged, analysis=contract is PortableAnalysisOptions)
    if "arcanes" in merged:
        arcanes = merged["arcanes"]
        if (
            not isinstance(arcanes, list)
            or len(arcanes) > 4
            or any(
                not _ranked_equipment(row)
                or row.keys()
                - (PortableArcane.__required_keys__ | PortableArcane.__optional_keys__)
                or ("stacks" in row and (type(row["stacks"]) is not int or row["stacks"] < 0))
                or ("uptime" in row and not _bounded_number(row["uptime"], high=1))
                for row in arcanes
            )
            or len({row["path"] for row in arcanes}) != len(arcanes)
        ):
            raise EngineError(
                L("赋能装备、等级或层数有误", "Invalid Arcane equipment, rank, or stacks")
            )
    return merged


def with_portable_analysis_options(request: dict, options: PortableAnalysisOptions | None = None):
    return _with_portable_options(request, options, PortableAnalysisOptions)


def with_portable_replay_options(request: dict, options: PortableReplayOptions | None = None):
    return _with_portable_options(request, options, PortableReplayOptions)


def with_gauge_charging_headshots(request):
    scenario = request.get("scenario") or {}
    target = request.get("benchmarkTarget", request)
    state = scenario.get("combatState") or {}
    if (
        scenario.get("incarnonCycle")
        and target.get("headshotChance", 0.0) < 1.0
        and "gaugeChargingHeadshots" not in state
    ):
        return {
            **request,
            "scenario": {**scenario, "combatState": {**state, "gaugeChargingHeadshots": True}},
        }
    return request


def weakpoint_target_option(tokens):
    """读取「弱点 50%」一类参数，保留用户指定的零值。"""
    for end in range(min(3, len(tokens)), 0, -1):
        label = " ".join(tokens[:end])
        if match := re.fullmatch(r"(?:弱点|weak[ -]?points?)\s*(\d+(?:\.\d+)?)\s*%", label):
            chance = float(match[1]) / 100
            if not _bounded_number(chance, high=1):
                raise EngineError(
                    L("弱点命中率须在 0–100% 之间", "Weak-point chance must be 0–100%")
                )
            return {"headshot": chance}, end
    return {}, 0


def portable_replay_fingerprint(request: dict) -> str:
    """给同引擎同配装的离线对照生成跨主机身份，包含所有已传装备和战斗参数。"""
    identity = json.dumps([PORTABLE_FINGERPRINT, request], sort_keys=True, allow_nan=False)
    return sha256(identity.encode()).hexdigest()


def target_enhancements(spec) -> PortableEnemyEnhancements:
    _reject_custom_defense(spec)
    enhancements = {"eximus": False, "noOverguard": spec.get("overguard_mode") != "on"}
    shield = dc.target_shield_override(
        spec.get("enemy", dc.find_enemy_exact(dc.DEFAULT_ENEMY)), spec.get("shield_mode")
    )
    if shield is not None:
        enhancements["shieldOverride"] = shield
    return enhancements


def portable_target_fields(enemy, spec):
    return {
        "enemy": enemy,
        "level": spec["level"],
        "steelPath": spec["steel_path"],
        "headshotChance": float(spec["headshot"]),
        "windowSeconds": spec["fire_seconds"],
        "enemyEnhancements": target_enhancements(spec),
    }


def portable_target_enemy(spec, enemies):
    if enemy := spec.get("enemy"):
        paths = enemies.get(enemy["name"], ())
        if len(paths) == 1:
            return paths[0], {}
        raise EngineError(L("该敌人暂无法精确匹配", "This enemy cannot be matched exactly"))

    target, _ = _target(spec)
    stats = target["stats"]
    faction = {
        "kuva_grineer": "RK_GRINEER_KUVA_FACTION",
        "the_murmur": "RK_MITW_FACTION",
        "scaldra": "RK_LASRIA_FACTION",
        "techrot": "RK_PROTOINFESTED_FACTION",
    }.get(target["faction"], f"RK_{target['faction'].upper()}_FACTION")
    return "__custom_enemy__", {
        "damageModel": "custom",
        "defenseSnapshot": {
            "health": stats["health"],
            "armour": stats["armor"],
            "shield": stats["shield"],
            "baseLevel": stats["base_level"],
            "healthType": "RK_HEALTH",
            "armourType": "RK_ARMOR",
            "shieldType": "RK_SHIELD",
            "factionResistance": faction,
        },
    }


def portable_defense_labels(enemy, enhancements):
    return dc.defense_labels(
        enemy, enhancements.get("noOverguard", True), enhancements.get("shieldOverride")
    )


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
    _reject_custom_defense(spec)
    if type(spec["level"]) is not int or spec["level"] < 0:
        raise _field_error(
            L("敌人等级", "Enemy level"), "须为非负整数", "must be a nonnegative integer"
        )
    if type(spec["steel_path"]) is not bool:
        raise _field_error(L("钢铁之路", "Steel Path"), "须为开启或关闭", "must be a boolean")
    if not isinstance(spec["headshot"], bool) and not _bounded_number(spec["headshot"], high=1):
        raise _field_error(
            L("弱点命中率", "Weak-point hit chance"), "须在 0–1 之间", "must be between 0 and 1"
        )
    if not _bounded_number(spec["fire_seconds"], low=0.1, high=600):
        raise _field_error(
            L("窗口时长", "Combat window"), "须为 0.1–600 秒", "must be 0.1–600 seconds"
        )
    if type(fire_mode) is not int or fire_mode < 0:
        raise EngineError(L("射击模式索引有误", "Invalid fire mode index"))
    manual = combat_replay_options(spec.get("combat_options") or {})
    if options and "scenario" in manual and isinstance(options.get("scenario"), dict):
        scenario = manual.pop("scenario")
        if any(
            key in scenario and scenario[key] != value for key, value in options["scenario"].items()
        ):
            raise EngineError(L("战斗条件重复且不一致", "Conflicting combat options"))
        manual["scenario"] = {**scenario, **options["scenario"]}
        options = {key: value for key, value in options.items() if key != "scenario"}
    target = portable_target_fields(enemy, spec)
    if enemy_enhancements is not None:
        target["enemyEnhancements"].update(enemy_enhancements)
    request = with_portable_replay_options(
        {
            "weapon": source,
            "fireMode": fire_mode,
            **target,
            "damageModel": "target",
            "metric": "windowedDps",
            "mods": mods,
            **manual,
        },
        options,
    )
    return with_gauge_charging_headshots(request)


def combat_replay_options(fields):
    fields = dict(fields)
    if "weaponAiming" in fields:
        # 工况在独立引擎的公开请求中使用 scenario.aiming。
        fields["aiming"] = fields.pop("weaponAiming")
    options = {}
    for key in ("armourStrip", "elementalArmourStrip"):
        if key in fields:
            options[key] = fields.pop(key)
    if fields:
        options["scenario"] = fields
    return options


def replay_scenario(result):
    return {
        key: value
        for key, value in (result or {}).items()
        if key in PortableCombatScenario.__optional_keys__ and value is not None
    }


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
        raise EngineError(L("请核对独立引擎端口", "Check the standalone engine port"))
    try:
        async with httpx.AsyncClient(
            timeout=timeout, follow_redirects=False, trust_env=False
        ) as client:
            url = f"http://127.0.0.1:{port}{path}"
            response = await (client.get(url) if body is None else client.post(url, json=body))
            result = response.json()
    except (httpx.ConnectError, httpx.ConnectTimeout):
        raise EngineUnreachable(
            L("独立引擎未运行或无法连接", "The standalone engine is offline or unreachable")
        ) from None
    except httpx.TimeoutException:
        raise EngineTimeout(
            L(
                "请求超时。首次计算较慢，结果可能仍在缓存中，请稍后重试。",
                "Request timed out. The first calculation can take longer. Try again shortly for a cached result.",
            )
        ) from None
    except (httpx.HTTPError, ValueError):
        raise EngineError(
            L(
                "独立引擎响应异常，请重试",
                "The standalone engine returned an invalid response. Try again.",
            )
        ) from None
    if not response.is_success or not isinstance(result, dict):
        error = result.get("error") if isinstance(result, dict) else None
        detail = error.get("message") if isinstance(error, dict) else ""
        detail = " ".join(str(detail or "").split())[:120]
        message = L("暂时无法计算这组配装", "This loadout could not be calculated")
        raise EngineBuildError(f"{message} · {detail}" if detail else message)
    return result


def find_catalog_weapon(catalog, weapon):
    if row := catalog.get(weapon.get("uniqueName")):
        return row
    matches = [row for row in catalog.values() if row.get("nameEn") == weapon.get("name")]
    return matches[0] if len(matches) == 1 else None


def _equipment_match(item, names):
    path = item.get("_source_unique_name")
    if path:
        for rows in names.values():
            for row in rows:
                if row["path"] == path:
                    return row
    matches = names.get(item["name"], ())
    return matches[0] if len(matches) == 1 else None


def catalog_equipment(item, names, label):
    row = _equipment_match(item, names)
    rank = item.get("rank", item.get("max_rank"))
    if row and _ranked_equipment({"path": row["path"], "rank": rank}) and rank <= row["maxRank"]:
        return {"path": row["path"], "rank": rank}
    name = item["name"] if is_en() else item.get("zh") or item["name"]
    raise EngineError(
        L(
            f"引擎卡库无法确认{label}「{name}」及其等级",
            f"The engine catalog cannot confirm {label} {name} at this rank",
        )
    )


def _fixed_inputs(weapon, spec, overrides, equipment):
    scanned = overrides is not None and "loadout_slots" in spec
    _reject_custom_defense(spec)
    checked_overrides = (
        {k: v for k, v in overrides.items() if k not in {"evo_keys", "form_word"}}
        if scanned
        else overrides
    )
    check_options(spec, checked_overrides, portable=True, weapon=weapon)
    melee = weapon.get("category") == "Melee"
    if (
        weapon.get("_panel_derived")
        or (scanned and melee)
        or weapon["name"].startswith(("Kuva ", "Tenet ", "Coda "))
        or spec.get("form") not in {None, "keep", "base", "incarnon", "alt_fire"}
        or (
            (
                spec.get("stance")
                or spec.get("heavy")
                or spec.get("combo_hits")
                or "comboTier" in (spec.get("combat_options") or {})
            )
            and not melee
        )
        or spec.get("deployment")
        or spec.get("lich_bonus")
        or spec.get("errors")
        or spec.get("enemy_miss")
    ):
        raise EngineError(
            L(
                "这组装备或战斗条件暂未接入固定复算",
                "These items or fight options are outside fixed replay",
            )
        )

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
    mods, options = [], {}
    for slot, mod in items:
        if overrides is None and mod.get("compat") == "Stance":
            stance = _equipment_match(mod, equipment.get("stances", {}))
            if not stance or "stancePath" in options:
                raise EngineError(
                    L("请填写一张引擎支持的架势", "Specify a single supported stance")
                )
            options["stancePath"] = stance["path"]
            continue
        if overrides is None and _equipment_match(mod, equipment.get("utilityMods", {})):
            if "exilus" in options:
                raise EngineError(L("扩展槽最多一张 MOD", "Use at most one Exilus MOD"))
            options["exilus"] = catalog_equipment(
                mod, equipment["utilityMods"], L("扩展槽", "Exilus MOD")
            )
            continue
        path = mod.get("_source_unique_name")
        rank = mod.get("rank")
        if rank is None and overrides is None:
            rank = mod.get("max_rank")
        if not path:
            # 部分随包条目有精确身份和容量数据，但缺 DE 路径。只接收目录中唯一同身份条目。
            matched = {
                row["path"]
                for row in equipment.get("manualMods", {}).get(mod["name"], ())
                if row.get("baseDrain") == mod["base_drain"]
                and row.get("maxRank") == mod["max_rank"]
                and row.get("polarity") == _REPLAY_POLARITIES.get(mod.get("polarity"))
            }
            if len(matched) == 1:
                path = matched.pop()
        if not path or type(rank) is not int or not 0 <= rank <= mod["max_rank"]:
            raise EngineError(
                L("有 MOD 的身份或等级无法确认", "A MOD identity or rank is unconfirmed")
            )
        mods.append({"slot": len(mods) if overrides is None else slot, "path": path, "rank": rank})
    if len(mods) > 8 or len({row["path"] for row in mods}) != len(mods):
        raise EngineError(
            L("普通槽位最多八张且 MOD 不得重复", "Use up to eight distinct regular MODs")
        )
    if overrides is None:
        if spec.get("arcanes"):
            options["arcanes"] = [
                catalog_equipment(arc, equipment.get("arcanes", {}), L("赋能", "Arcane"))
                for arc in spec["arcanes"]
            ]
        scenario = {}
        if spec.get("heavy"):
            scenario["heavyAttack"] = True
        if spec.get("combo_hits"):
            if "comboTier" in (spec.get("combat_options") or {}):
                raise EngineError(
                    L("连击数和档位请选择一种", "Specify either combo hits or combo tier")
                )
            tier, _ = dc.combo_multiplier(spec["combo_hits"])
            scenario["comboTier"] = int(tier) - 1
        if scenario:
            options["scenario"] = scenario
    return mods, options


async def portable_catalog(service, port, *, include_unavailable=False):
    if include_unavailable:
        weapons, enemies, _ = await _portable_catalog(service, port, include_unavailable=True)
    else:
        weapons, enemies, _ = await _portable_catalog(service, port)
    return weapons, enemies


async def _portable_catalog(service, port, *, include_unavailable=False):
    async def fetch():
        result = await portable_request(port, "/api/catalog", timeout=8)
        if result.get("buildLabel") != PORTABLE_DATA or result.get("schemaVersion") != 3:
            raise EngineError(
                L(
                    "本机独立引擎数据版本不匹配",
                    "The local standalone engine data version does not match",
                )
            )
        catalog = {row["path"]: row for row in result["weapons"]}
        fields = (
            "path",
            "nameEn",
            "nameZh",
            "weaponType",
            "fireModes",
            "polarities",
            "incarnon",
            "available",
            "buildAvailable",
            "analysisAvailable",
            "compatTags",
        )
        # 目录也会导出镜像敌人的同名武器，玩家输入应命中它继承的原武器。
        weapons = {
            path: {key: row[key] for key in fields if key in row}
            for path, row in catalog.items()
            if not (
                "doppelganger" in row.get("nameInternal", "").casefold()
                and any(
                    parent != path and catalog.get(parent, {}).get("nameEn") == row["nameEn"]
                    for parent in row.get("ancestry", ())
                )
            )
        }
        enemies = {}
        for row in result["enemies"]:
            if row.get("complete") and row.get("scalingSupported"):
                enemies.setdefault(row["nameEn"], []).append(row["path"])
        riven_stats = {
            kind: {stat["code"]: stat for stat in stats}
            for kind, stats in result.get("statsByType", {}).items()
        }
        for path, row in weapons.items():
            stats = riven_stats.get(catalog[path].get("rivenType"), {})
            row["referenceRivenBases"] = {
                stat["code"]: stat["baseValue"]
                for stat in stats.values()
                if stat.get("canPositive") and stat.get("code") in ("0", "1", "2")
            }
            if stats and row.get("weaponType") != "companion":
                row["rivenStats"] = stats
        equipment = {"utilityMods": {}, "arcanes": {}, "manualMods": {}, "stances": {}}
        for kind, names in equipment.items():
            for row in result.get(kind, ()):
                if row.get("nameEn") and row.get("path"):
                    names.setdefault(row["nameEn"], []).append(
                        {
                            key: row[key]
                            for key in ("path", "maxRank", "baseDrain", "drain", "polarity")
                            if key in row
                        }
                    )
        return weapons, enemies, equipment

    weapons, enemies, equipment = await service.portable_catalog.get_or_fetch(str(port), 600, fetch)
    if not include_unavailable:
        weapons = {
            path: row
            for path, row in weapons.items()
            if row.get("available") and row.get("buildAvailable")
        }
    return weapons, enemies, equipment


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
    # 0 值分项不提供组成证据，且部分模式只有一种有效伤害。
    rows = {key: value for key, value in observed.get("damage_types", {}).items() if value > 0}
    if not rows:
        return True
    damage = {}
    for key, value in (stats.get("damageTypes") or {}).items():
        if value > 0:
            name = key.removeprefix("DT_").lower()
            name = _PANEL_TYPE_ALIASES.get(name, name)
            damage[name] = damage.get(name, 0) + value
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


_SCAN_SECTION_FORMS = {
    "primary_fire": "base",
    "secondary_fire": "alt_fire",
    "incarnon": "incarnon",
}


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
    observed_form = _SCAN_SECTION_FORMS.get(panel.get("_section"))
    panel_modes = [mode for mode in modes if _scan_mode_matches(mode, observed_form)]
    additional = []
    for section in panel.get("_sections") or ():
        form = _SCAN_SECTION_FORMS.get(section.get("_section"))
        section_modes = [mode for mode in modes if form and _scan_mode_matches(mode, form)]
        if len(section_modes) == 1 and sum(key in section for key in _SCAN_PANEL_FIELDS) >= 2:
            additional.append((section_modes[0]["index"], section))
    if not panel_modes:
        raise _scan_context_message([], modes, tiers, catalog_weapon)
    observed_indices = {mode["index"] for mode in panel_modes} | {i for i, _ in additional}
    probe_modes = [mode for mode in modes if mode["index"] in observed_indices]
    if len(probe_modes) * math.prod(map(len, alternatives)) > 48:
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
        **portable_target_fields(dc.DEFAULT_ENEMY_PATH, dc.default_target()),
        "scenario": {
            key: value for key, value in request.get("scenario", {}).items() if key != "aiming"
        },
    }
    probe.pop("defenseSnapshot", None)
    if "arcanes" in probe:
        probe["arcanes"] = [{**arc, "stacks": 0} for arc in probe["arcanes"]]
    key = json.dumps([port, probe, panel, modes, alternatives], sort_keys=True)

    async def fetch():
        matches = []
        section_routes = [[] for _ in additional]
        deadline = asyncio.get_running_loop().time() + 16
        for choices in product(*alternatives):
            route = dict(zip(tiers, choices))
            for mode in probe_modes:
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
                stats = result.get("moddedStats") or {}
                if mode in panel_modes and _matches_scan_panel(panel, stats):
                    matches.append((mode, route))
                for (index, section), corroborated in zip(additional, section_routes):
                    if mode["index"] == index and _matches_scan_panel(section, stats):
                        corroborated.append(route)
        # 副段只能收窄主段已匹配的路线。读数残缺或不相容时仍保留主段证据。
        narrowed = [
            entry
            for entry in matches
            if all(entry[1] in routes for routes in section_routes if routes)
        ]
        return narrowed or matches

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
    """按手填配装或识卡确认的槽位复算。"""
    try:
        service = host.damage_engine
        scanned = overrides is not None and "loadout_slots" in spec
        _reject_custom_defense(spec)
        async with service.replays.slot():
            await service.ensure_portable(port)
            data, enemies, equipment = await _portable_catalog(service, port)
            mods, options = await asyncio.to_thread(
                _fixed_inputs, weapon, spec, overrides, equipment
            )
            enemy = spec.get("enemy")
            path, target_model = portable_target_enemy(spec, enemies)
            catalog_weapon = find_catalog_weapon(data, weapon)
            if not catalog_weapon:
                raise EngineError(
                    L(
                        "该武器或敌人暂无法精确匹配",
                        "This weapon or enemy cannot be matched exactly",
                    )
                )
            source = catalog_weapon["path"]
            mode = None
            context = ""
            scan_capacity = None
            optimized_codes = ()
            if scanned:
                scan_capacity = spec.get("scan_capacity")
                extra, drains = _scan_replay_equipment(spec, equipment)
                request = portable_damage_request(source, path, mods, spec, options=extra)
                request.update(target_model)
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
                modes, _, route, _ = _scan_context_candidates(catalog_weapon, spec)
                form = spec.get("form") or "base"
                matched = [mode for mode in modes if _scan_mode_matches(mode, form)]
                if len(matched) != 1:
                    raise EngineError(
                        L(
                            "所选攻击模式不可用或不唯一",
                            "The selected attack mode is unavailable or ambiguous",
                        )
                    )
                (mode,) = matched
                if route:
                    options["incarnonChoices"] = route
                request = portable_damage_request(
                    source, path, mods, spec, fire_mode=mode["index"], options=options
                )
                request.update(target_model)
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
            target_label=_target(spec)[1] if enemy is None else None,
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


def validate_fixed_replay(result, request, *, require_build_label=True):
    def mod_identity(row):
        if type(row.get("rank")) is not int:
            raise EngineError(L("MOD 回放等级无效", "Invalid Mod rank in replay"))
        return row["slot"], row["path"], row["rank"]

    given = {mod_identity(row) for row in request["mods"]}
    if exilus := request.get("exilus"):
        equipped = result.get("exilus") or {}
        special = request.get("specialSlotPolarities") or {}
        if (
            equipped.get("incompatible")
            or type(equipped.get("rank")) is not int
            or type(exilus.get("rank")) is not int
            or any(equipped.get(key) != exilus[key] for key in ("path", "rank"))
            or ("exilus" in special and special["exilus"] != equipped.get("slotPolarity"))
        ):
            raise EngineError(L("扩展槽回放与配装不符", "Exilus slot does not match the build"))
        if equipped.get("catalogJoined"):
            given.add((8, exilus["path"], exilus["rank"]))
    arcanes = request.get("arcanes") or []
    reported = result.get("arcanes", ())
    if not isinstance(reported, (list, tuple)) or any(
        not isinstance(row, dict)
        or type(row.get("applied")) is not bool
        or type(row.get("rank")) is not int
        for row in reported
    ):
        raise EngineError(L("赋能回放与配装不符", "Arcanes do not match the build"))
    active = [row for row in reported if row["applied"]]
    by_identity = {(row.get("path"), row.get("rank")): row for row in active}
    if (
        len(active) != len(arcanes)
        or len(by_identity) != len(active)
        or any(
            type(arc.get("rank")) is not int
            or (matched := by_identity.get((arc["path"], arc["rank"]))) is None
            or any(
                key in arc
                and (
                    type(arc[key]) not in (int, float)
                    or type(matched.get(key)) not in (int, float)
                    or matched[key] != arc[key]
                )
                for key in ("stacks", "uptime")
            )
            for arc in arcanes
        )
    ):
        raise EngineError(L("赋能回放与配装不符", "Arcanes do not match the build"))
    if (
        request.get("stancePath")
        and (result["selection"].get("stance") or {}).get("path") != request["stancePath"]
    ):
        raise EngineError(L("架势回放与配装不符", "The stance does not match the build"))
    route = result.get("incarnonChoices") or {}
    if any(
        route.get(tier) != choice for tier, choice in request.get("incarnonChoices", {}).items()
    ):
        raise EngineError(L("进化回放与配装不符", "Evolutions do not match the build"))
    scenario = request.get("scenario") or {}
    returned = result["selection"].get("scenario") or result.get("scenario") or {}
    if scenario.get("modelSpread") and returned.get("modelSpread") is not True:
        raise EngineError(
            L("当前独立引擎未确认散布计算", "The standalone engine did not confirm spread modeling")
        )
    for key in ("distance", "nearestEnemyDistance", "heavyAttack", "comboTier", "aiming"):
        if (
            key in scenario
            and returned.get(key, False if key == "aiming" else None) != scenario[key]
        ):
            raise EngineError(
                L("独立引擎回放的战斗条件不符", "Standalone replay used different combat options")
            )
    given_defense = request.get("enemyEnhancements", {})
    used_defense = result.get("enemyEnhancements") or {}
    if used_defense.get("shieldOverride") != given_defense.get(
        "shieldOverride"
    ) or used_defense.get("noOverguard", True) != given_defense.get("noOverguard", True):
        raise EngineError(
            L(
                "独立引擎未确认目标护盾或超宏设置",
                "The standalone engine did not confirm shields or Overguard",
            )
        )
    validation = result["loadoutValidation"]
    assignments = validation["assignments"]
    used = {mod_identity(row) for row in assignments}
    window = result.get("benchmarkTimeline")
    if window is None:
        raise EngineError(
            L(
                "独立引擎暂未算出此攻击的 DPS",
                "DPS unavailable for this attack in the standalone engine",
            )
        )
    context = window["context"]
    dps = window["windowDps"]
    allowed_labels = (PORTABLE_DATA,) if require_build_label else (PORTABLE_DATA, None, "")
    if (
        result.get("buildLabel") not in allowed_labels
        or result["damageModel"] != request.get("damageModel", "target")
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
    target_label=None,
    mode=None,
    context="",
    scan_capacity=None,
    optimized_codes=(),
):
    from .portable_riven import enemy_label, evolution_lines

    window, validation = validate_fixed_replay(result, request)
    name = weapon["name"] if is_en() else weapon.get("zh") or weapon["name"]
    target = target_label or enemy_label(request["enemy"], enemy["name"], enemy.get("zh"))
    defense = " · ".join(portable_defense_labels(enemy, request.get("enemyEnhancements", {})))
    difficulty = L("钢铁之路", "Steel Path") if request["steelPath"] else L("普通", "Normal")
    chance = request["headshotChance"]
    hit = L(f"弱点 {chance:.0%}", f"Weak points {chance:.0%}") if chance else L("躯干", "Body")
    secs = window["windowSeconds"]
    melee = weapon.get("category") == "Melee"
    if melee and request.get("scenario", {}).get("heavyAttack"):
        mode_label = L("重击", "Heavy attack")
    elif melee and _scan_mode_matches(mode or {"index": 0}, "base"):
        mode_label = L("平砍", "Neutral attack")
    else:
        mode_label = _scan_mode_label(mode) if mode else L("主射", "Primary fire")
    lines = [
        f"◆ {name} · {mode_label}",
        f"· {target} Lv{request['level']} · {difficulty} · {hit} · {defense}",
        L("· 独立引擎", "· Standalone engine"),
        L(
            f"◆ {secs:g} 秒平均 DPS　{window['windowDps']:,.1f}",
            f"◆ Average DPS over {secs:g} s　{window['windowDps']:,.1f}",
        ),
    ]
    lines.extend(combat_condition_lines(request))
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
            L(f"· 攻击 {window['shots']} 次", f"· {window['shots']} attacks")
            if melee
            else L(
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
        if row["slot"] >= 8:
            continue
        name = names[row["path"]]
        lines.append(L(f"· {name} · 等级 {row['rank']}", f"· {name} · Rank {row['rank']}"))
    for label, rows in (
        (L("扩展槽", "Exilus"), [result["exilus"]] if result.get("exilus") else []),
        (L("赋能", "Arcane"), result.get("arcanes") or []),
        (
            L("架势", "Stance"),
            [result["selection"]["stance"]]
            if (result["selection"].get("stance") or {}).get("rank") is not None
            else [],
        ),
    ):
        for row in rows:
            name = (row.get("nameZh") if not is_en() else None) or row.get("nameEn") or label
            lines.append(
                L(
                    f"· {label} {name} · {row['rank']} 级",
                    f"· {label} {name} · Rank {row['rank']}",
                )
            )
    lines.extend(weapon_panel_lines(result.get("moddedStats"), melee=melee))
    lines.extend(evolution_lines(catalog_weapon, result.get("incarnonChoices")))
    if result.get("unsupported") or window.get("unsupported"):
        lines.append(
            L("※ 部分效果未计入伤害", "※ Some effects were excluded from the damage estimate")
        )
    return lines


def combat_condition_lines(request):
    scenario = request.get("scenario") or {}
    parts = []
    if scenario.get("aiming"):
        parts.append(L("瞄准", "Aiming"))
    if scenario.get("heavyAttack"):
        parts.append(L("重击", "Heavy attack"))
    if (tier := scenario.get("comboTier")) is not None:
        parts.append(L(f"连击 ×{tier + 1}", f"Combo ×{tier + 1}"))
    if (distance := scenario.get("distance")) is not None:
        parts.append(L(f"距离 {distance:g} 米", f"Distance {distance:g} m"))
    if (distance := scenario.get("nearestEnemyDistance")) is not None:
        parts.append(L(f"最近敌人 {distance:g} 米", f"Nearest enemy {distance:g} m"))
    if "modelSpread" in scenario:
        parts.append(
            L("散布开启", "Spread on") if scenario["modelSpread"] else L("散布关闭", "Spread off")
        )
    for key, label in (
        ("armourStrip", L("剥甲", "Armor strip")),
        ("elementalArmourStrip", L("元素剥甲", "Elemental armor strip")),
    ):
        if request.get(key):
            parts.append(f"{label} {request[key]:.0%}")
    return ["· " + " · ".join(parts)] if parts else []


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


def check_options(spec: dict, overrides: dict | None, *, portable=False, weapon=None) -> None:
    if spec.get("enemy_miss"):
        raise _unsupported()
    if not portable and set(spec.get("combat_options") or {}) - {"weaponAiming"}:
        raise _unsupported()
    if overrides is not None:
        if overrides.keys() - _TARGET_FIELDS or overrides.get("enemy_miss"):
            raise _unsupported()
        return
    # 手写指令的数值已经聚合。用同一解析器扣除装备自带加成，防止手填值在模拟端静默消失。
    names = [m["name"] for m in spec["mods"] if m.get("name")]
    if weapon:
        names.insert(0, weapon["name"])
    for arc in spec["arcanes"]:
        names.extend(("赋能", f"{arc['name']}@{arc.get('rank', arc.get('max_rank'))}"))
    items_only, _ = dc.parse_args(names)
    supported = _TARGET_FIELDS | _BUILD_FIELDS
    if portable:
        supported |= {"combo_hits", "evo_keys", "form_word", "combat_options"}
    for key, value in spec.items():
        if key not in supported and value != items_only.get(key):
            raise _unsupported()


def _target(spec: dict) -> tuple[dict, str]:
    enemy = spec.get("enemy", dc.find_enemy_exact(dc.DEFAULT_ENEMY))
    faction = (enemy or {}).get("faction") or spec.get("faction", dc.DEFAULT_FACTION)
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
    shield = dc.target_shield_override(enemy, spec.get("shield_mode"))
    overguard = spec.get("overguard")
    if spec.get("overguard_mode") == "on" and overguard is None:
        overguard = (base.get("base_overguard") or 0.0) * dc.overguard_multiplier(spec["level"])
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
            "shield": (base.get("base_shield") or 0) if shield is None else shield,
            "overguard": overguard or 0,
            "affinity": 0,
        },
    }, label


def simulation_target_fields(spec):
    target, label = _target(spec)
    return {
        "enemy": target["id"],
        "custom_enemies": [target],
        "level": max(1, min(9999, spec["level"])),
        "headshot_pct": round(float(spec["headshot"]) * 100),
        "steel_path": spec["steel_path"],
        "eximus": False,
        "duration": spec["fire_seconds"],
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
    _reject_custom_defense({"overguard": spec.get("overguard")})
    if (spec.get("combat_options") or {}).get("weaponAiming"):
        raise EngineError(L("瞄准工况需使用便携引擎", "Aiming requires the portable engine"))
    target, label = simulation_target_fields(spec)
    req = {
        "weapon": _id(name),
        "mods": ids,
        "arcane": arcane,
        "arcane_rank": arcane_rank,
        "form": "heavy" if spec["heavy"] and weapon.get("category") == "Melee" else "base",
        "evolutions": [],
        "evolutions_as_given": True,
        **target,
        "assume_max": False,
        "infinite_ammo": True,
        "distance": 0,
        "seed": 1,
        "runs": 16,
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
async def simulation_session(host, event=None, *, managed=True, wait_timeout=None):
    """整组模拟共用一次排位和同一份配置。"""
    service = getattr(host, "damage_engine", None)
    if service is None:
        yield dict(getattr(host, "cfg", {}) or {})
        return

    async def notify(position):
        await send_queue_position(
            event, getattr(host, "_messaging", None), L("完整模拟", "Simulation"), position
        )

    async with service.simulations.slot(notify, wait_timeout=wait_timeout):
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
    enemy = spec.get("enemy")
    shield = dc.target_shield_override(enemy, spec.get("shield_mode"))
    defense = " · ".join(
        dc.defense_labels(enemy, not req["custom_enemies"][0]["stats"]["overguard"], shield)
    )
    difficulty = L("钢铁之路", "Steel Path") if req["steel_path"] else L("普通", "Normal")
    lines = [
        L(f"◆ {name}｜完整模拟", f"◆ {name} | Simulation"),
        L(
            f"· 目标 {target} {req['level']} 级 · {difficulty} · {shot} · {defense}",
            f"· {target} Lv {req['level']} · {difficulty} · {shot} · {defense}",
        ),
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


@dataclass
class _ManagedHealth:
    last_success: float = 0
    failures: int = 0
    restart_after: float = 0
    port: int | None = None


class EngineService:
    """按需下载固定版服务，托管进程由插件生命周期管理。"""

    def __init__(self, data_dir: Path):
        self.root = data_dir / "damage-engine"
        self.release = self.root / RELEASE
        self.task = None
        self.child = None
        self.portable_child = None
        self._managed_health = {name: _ManagedHealth() for name in ("child", "portable_child")}
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
                await self._discard_child("portable_child")
                self._managed_health["portable_child"] = _ManagedHealth()
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
                            name, expected, size = asset
                            bundle = Path(tmp) / name
                            received = await download_file(
                                client, f"{PORTABLE_DOWNLOADS}/{name}", bundle, size, expected
                            )
                            if received != size:
                                raise ValueError("引擎运行包字节数不一致")
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
        message = L("本机独立引擎版本不匹配", "The local standalone engine version does not match")
        if ("buildLabel" in value and value["buildLabel"] != PORTABLE_DATA) or (
            "schemaVersion" in value and value["schemaVersion"] != 3
        ):
            raise EngineVersionMismatch(message)
        if (
            value.get("ok") is not True
            or "buildLabel" not in value
            or "schemaVersion" not in value
            or (require_fingerprint and value.get("engineFingerprint") != PORTABLE_FINGERPRINT)
        ):
            raise EngineError(message)
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
                await self._discard_child("portable_child")

    async def ensure_portable(self, port):
        async with self.lock:
            if self.closed:
                raise EngineError(L("引擎正在关闭", "The engine is shutting down"))
            if self._configured_port is not None and port != self._configured_port:
                raise EngineError(L("完整模拟设置已更新", "Full simulation settings have changed"))
            if not self.portable_installed():
                return
            if self.portable_child is not None and self.portable_child_port != port:
                await self._discard_child("portable_child")
            if self._managed_health["portable_child"].port != port:
                self._managed_health["portable_child"] = _ManagedHealth(port=port)

            async def spawn():
                with (self.root / "install.log").open("ab") as log:
                    proc = await asyncio.create_subprocess_exec(
                        sys.executable,
                        "launch.py",
                        "--port",
                        str(port),
                        cwd=self.portable_release,
                        start_new_session=True,
                        stdout=log,
                        stderr=log,
                    )
                self.portable_child_port = port
                return proc

            await self._ensure_healthy(
                "portable_child",
                lambda: self._portable_health(port, require_fingerprint=True),
                spawn,
                attempts=80,
                interval=0.25,
                startup_timeout=20,
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
            if self.child is not None and token != self.token:
                await self._discard_child("child")
                self._managed_health["child"] = _ManagedHealth()

            async def probe():
                _version(await _request(cfg, "/healthz", timeout=0.6))

            async def spawn():
                proc = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "server.py",
                    cwd=self.release,
                    env={**os.environ, "WARFRAME_ENGINE_TOKEN": token},
                    start_new_session=True,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                self.token = token
                return proc

            await self._ensure_healthy("child", probe, spawn, attempts=15, interval=0.1)

    async def _discard_child(self, name):
        child = getattr(self, name)
        if child is not None:
            await self._stop(child)
            setattr(self, name, None)
        if name == "portable_child":
            self.portable_child_port = 0

    async def _ensure_healthy(
        self, name, probe, spawn, *, attempts, interval, startup_timeout=None
    ):
        """在生命周期锁内探活。只有本实例创建并保存的句柄可以终止。"""
        now = asyncio.get_running_loop().time
        health = self._managed_health[name]

        async def healthy():
            try:
                await probe()
            except EngineError:
                health.failures += 1
                return False
            health.last_success = now()
            health.failures = 0
            return True

        child = getattr(self, name)
        if child is not None and child.returncode is not None:
            await self._discard_child(name)
        if await healthy():
            return
        if getattr(self, name) is not None:
            while health.failures < 3:
                await asyncio.sleep(interval)
                if await healthy():
                    return
            await self._discard_child(name)
        if now() < health.restart_after:
            raise EngineError(
                L("引擎重启冷却中，请稍后重试", "Engine restart pending, retry shortly")
            )
        health.restart_after = now() + 5
        try:
            child = await spawn()
        except OSError:
            raise EngineError(
                L("本机独立引擎无法启动", "Cannot start the managed engine")
            ) from None
        setattr(self, name, child)
        deadline = now() + startup_timeout if startup_timeout else None
        try:
            for _ in range(attempts):
                if child.returncode is not None or deadline is not None and now() >= deadline:
                    break
                if await healthy():
                    return
                await asyncio.sleep(interval)
            raise EngineError(
                L("本机独立引擎启动检查失败", "Managed engine failed its startup check")
            )
        except BaseException:
            await self._discard_child(name)
            health.restart_after = now() + 5
            raise

    async def close(self):
        self.closed = True
        self.simulations.close()
        self.replays.close()
        if self.task is not None and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        async with self.lock:
            await self._discard_child("child")
            await self._discard_child("portable_child")
