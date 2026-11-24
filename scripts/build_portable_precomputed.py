"""离线构建便携引擎参考配装和单词条矩阵。

每台机器使用同一份插件代码和引擎运行包，指定相同的 --shard-total，
分别传入从 0 开始的 --shard-index。中断后用原命令继续。全量合并时列出
各片的目录，无需按顺序传入。小样生成和合并都用 --only-weapons 限定范围。
用 --only-extra-modes 分片补跑灵化循环、重击和重击震地，再与常规场景一起合并。
用 --backfill-missing-traits 在已有常规场景中补算非单元素词条，保留参考配装和既有词条结果。
用 --backfill-element-baselines 为已有场景中收益冲突的正元素词条补算同套对照。
用 --overguard 或 --shield 在各档独立的逐场景目录生成、回填和合并。
macOS 和 Linux 运行包均使用 CPython 3.12 及随包的原生扩展。
解压包缺少上游数据时，可用 --inputs-dir 指定同版源码或已齐备的本地镜像。
未指定本地镜像时，从发布清单所用的固定上游提交补齐到临时目录。

随包表的 catalog 字典以列名和行数组存储装备。配装的 mods 和 without
数组按普通槽下标排列，空槽及无可比较拆卡数据用 null 表示。词条表首列
是 stats 字典行的下标，units 存放第二列的单位。耗时仅打印到终端。
对应基准 KPM 为零时没有可定义的相对收益，百分比列留空。
场景的 elem_builds 以词条下标为键、element_builds 下标为值。
仅一档通过时，值为 [元素套下标, 31 或 21]。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib
import json
import math
import multiprocessing
import os
import platform
import re
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass, replace
from functools import lru_cache
from multiprocessing.connection import wait
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import (  # noqa: E402
    damage_engine,
    formatters,
    parser,
    portable_install,
    portable_riven,
    riven_analysis,
    riven_pools,
    riven_rating,
)

FINGERPRINT = "535ed49f5a51149d"
BUILD_LABEL = "2026.08.13.13.24"
DEFAULT_BUILDS_OUT = ROOT / "core/data" / portable_riven.BUNDLED_BUILDS
DEFAULT_TRAITS_OUT = ROOT / "core/data" / portable_riven.BUNDLED_TRAITS
OVERGUARD_BUILDS_OUT = ROOT / "core/data" / portable_riven.BUNDLED_BUILDS_OVERGUARD
OVERGUARD_TRAITS_OUT = ROOT / "core/data" / portable_riven.BUNDLED_TRAITS_OVERGUARD
SHIELD_BUILDS_OUT = ROOT / "core/data" / portable_riven.BUNDLED_BUILDS_SHIELD
SHIELD_TRAITS_OUT = ROOT / "core/data" / portable_riven.BUNDLED_TRAITS_SHIELD
SCHEMA = 1
WEAPON_FIELDS = ("path", "nameEn", "nameZh", "weaponType", "disposition", "incarnon")
MODE_FIELDS = ("index", "kind", "nameEn", "nameZh", "trigger", "isAlternateFire", "isIncarnonForm")
MOD_FIELDS = ("path", "nameEn", "nameZh", "polarity")
EXILUS_FIELDS = (
    "path",
    "rank",
    "maxRank",
    "drain",
    "rawDrain",
    "polarity",
    "slotPolarity",
    "nameEn",
    "nameZh",
    "modSet",
    "setNameZh",
)
STANCE_FIELDS = (
    "path",
    "nameEn",
    "nameZh",
    "polarity",
    "drain",
    "baseDrain",
    "slotPolarity",
    "capacityGain",
    "builtIn",
)
ARCANE_FIELDS = ("path", "rank", "nameEn", "nameZh", "stacks", "uptime")
PANEL_FIELDS = (*damage_engine.PANEL_FIELDS, "damageTypes")
STAT_COLUMNS = ("code", "stat_id", "nameEn", "nameZh")
TRAIT_COLUMNS = ("stat", "unit", "flags", "shown31", "pct31", "shown21", "pct21")
FLAGS = {
    "spliced": 1,
    "modeled": 2,
    "neutral": 4,
    "weakpoint": 8,
    "reoptimized": 16,
}
ELEMENT_CODES = {"4", "5", "6", "7"}
COMPOUND_ELEMENT_TARGETS = {
    f"{element}_damage": (element,)
    for element in ("corrosive", "viral", "blast", "gas", "magnetic", "radiation")
}
ELEMENT_TARGETS = {
    "heat_damage": ("heat", "blast", "gas", "radiation"),
    "cold_damage": ("viral", "blast", "magnetic", "cold"),
    "toxin_damage": ("viral", "corrosive", "gas", "toxin"),
    "electric_damage": ("corrosive", "magnetic", "radiation", "electricity"),
    **COMPOUND_ELEMENT_TARGETS,
}
ELEMENT_BUILDS = (
    "corrosive",
    "viral",
    "heat",
    "blast",
    "gas",
    "magnetic",
    "radiation",
    "electricity",
    "cold",
    "toxin",
)
ROLL_LABELS = ("31", "21")
TRAIT_ROW_FIELDS = {
    *STAT_COLUMNS,
    *FLAGS,
    "unit",
    "elem_build",
    "elem_build_roll",
    *(f"{field}_{roll}" for field in ("shown", "pct") for roll in ROLL_LABELS),
}
WEAKPOINT_CODES = {"W", "Y"}
TRAIT_SIGNS = ("positive", "negative")
MELEE_TYPES = {"melee", "zaw"}
EXTRA_MODES = ("cycle", "heavy", "slam")
INCARNON_CYCLE_UNAVAILABLE = "incarnon-cycle-data-unavailable"
LEGACY_RANGED_FINGERPRINT = "19ac1fa5017c9aaa"
# 当前导出没有备弹字段。保留已核对的备弹值，和弹匣、拾取值分开。
RESERVE_AMMO = {
    "/Lotus/Weapons/Corpus/Bow/Longbow/CrpBow": 6,
    "/Lotus/Weapons/Grineer/Bows/GrnBow/GrnBowWeapon": 5,
    "/Lotus/Weapons/Grineer/KuvaLich/LongGuns/Zarr/KuvaZarr": 5,
    "/Lotus/Weapons/Grineer/KuvaLich/LongGuns/Ogris/KuvaOgris": 7,
    "/Lotus/Weapons/Grineer/KuvaLich/LongGuns/Tonkor/KuvaTonkor": 11,
    "/Lotus/Weapons/Infested/Bow/InfCernosBow/InfCernos": 72,
    "/Lotus/Weapons/Tenno/Bows/PrimeDerelictCernos/DerelictCernos": 7,
    "/Lotus/Weapons/Corpus/LongGuns/CrpBriefcaseLauncher/CrpBriefcaseLauncher": 16,
}
# 初始连击为计数，招架角度为角度。派系词条使用引擎卡面百分比。
UNITS = ("%", "x", "m", "s", "", "°")
SAMPLE_TRAITS = {"D", "K", "1", "S", "VI", "H", "7", "W", "Y"}
APP = None
OUT_DIR = None
TARGET = {}
STAT_DEFS = {}
TEMPLATES = {}


def json_bytes(value) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def digest(value) -> str:
    return hashlib.sha256(json_bytes(value)).hexdigest()


def write_json(path: Path, document, *, mode=None) -> int:
    data = json_bytes(document) + b"\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".precompute-", delete=False) as f:
            temporary = Path(f.name)
            f.write(data)
            f.flush()
            if mode is not None:
                os.fchmod(f.fileno(), mode)
            os.fsync(f.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return len(data)


def fields(row, keys):
    return {key: row.get(key) for key in keys}


def rounded(value, places):
    if not math.isfinite(value):
        raise ValueError(f"引擎返回非有限数 {value}")
    result = round(value, places)
    return int(result) if result == int(result) else result


async def download_runtime_inputs(dest, files, remaining):
    import httpx

    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(30.0)) as client:
            await portable_install.download_inputs(client, dest, files, remaining)
    except (httpx.HTTPError, OSError, ValueError) as exc:
        raise ValueError(f"补齐运行包数据失败，可用 --inputs-dir 指定本地镜像 ({exc})") from exc


def prepare_engine(engine_dir, inputs_dir, staging):
    manifest_path = engine_dir / "manifest.json"
    if not manifest_path.is_file():
        return engine_dir
    manifest = json.loads(manifest_path.read_bytes())
    if (
        manifest["fingerprint"] != FINGERPRINT
        or manifest["dataVersion"] != BUILD_LABEL
        or manifest["platform"] != f"{sys.platform}-{platform.machine()}"
        or manifest["python"] != f"{sys.version_info.major}.{sys.version_info.minor}"
        or manifest["upstream"] != portable_install.UPSTREAM_REVISION
    ):
        raise ValueError("解压运行包的版本、平台或 Python ABI 不匹配")
    files = manifest["files"]
    upstream = set(manifest["upstreamFiles"])
    if not upstream <= files.keys() or any(not name.startswith("data/") for name in upstream):
        raise ValueError("运行包的上游数据清单无效")
    resolved = {}
    to_download = {}
    size = 0
    for name, expected in files.items():
        relative = PurePosixPath(name)
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or relative.as_posix() != name
            or "\\" in name
            or not portable_install.SHA256.fullmatch(expected)
        ):
            raise ValueError(f"运行包的文件清单无效 {name}")
        entry = engine_dir / relative
        if not entry.is_file() and name in upstream:
            if inputs_dir is None:
                to_download[name] = expected
                continue
            entry = inputs_dir / relative
        if not entry.is_file():
            raise ValueError(f"运行包缺少文件 {entry}")
        size += entry.stat().st_size
        if size > portable_install.MAX_UNPACKED_SIZE:
            raise ValueError("运行包数据超过体积上限")
        with entry.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != expected:
                raise ValueError(f"运行包文件摘要不符 {entry}")
        resolved[name] = entry
    if not to_download and all(entry == engine_dir / name for name, entry in resolved.items()):
        return engine_dir
    for name, entry in resolved.items():
        dest = staging / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(entry, dest)
    if to_download:
        print(f"补齐 {len(to_download)} 个固定版本的上游数据文件", flush=True)
        asyncio.run(
            download_runtime_inputs(staging, to_download, portable_install.MAX_UNPACKED_SIZE - size)
        )
    return staging


def setup_engine(engine_dir: Path, out_dir: Path | None = None, *, variant="default"):
    global APP, OUT_DIR, TARGET, STAT_DEFS, TEMPLATES
    os.environ["WF_ANALYZER_SERIAL"] = "1"
    os.environ["WF_ENGINE_WORKERS"] = "1"
    os.environ["WF_RIVEN_ROOT"] = str(engine_dir)
    os.environ["WF_ANALYSIS_STATICS_CACHE_PATH"] = str(engine_dir / "analysis-statics.json")
    if not (engine_dir / "app.py").is_file():
        raise ValueError(f"引擎目录缺少 app.py {engine_dir}")
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(engine_dir))
    APP = importlib.import_module("app")
    if Path(APP.__file__).resolve() != (engine_dir / "app.py").resolve():
        raise ValueError(f"导入了其他目录的引擎 {APP.__file__}")
    actual = (APP.ANALYSIS_STATICS_RUNTIME_INPUTS, APP.EXPORTS["buildLabel"])
    if actual != (FINGERPRINT, BUILD_LABEL):
        raise ValueError(f"引擎运行指纹或数据版本不符 {actual}，需匹配 {FINGERPRINT}/{BUILD_LABEL}")
    if (damage_engine.PORTABLE_FINGERPRINT, damage_engine.PORTABLE_DATA) != actual:
        raise ValueError("插件的引擎版本与本脚本不一致")
    APP._disable_analysis_statics_persistence()
    TARGET = portable_riven.bundled_reference_target(variant=variant)
    TEMPLATES = APP.EXPORTS.get("rivenTemplates") or {}
    OUT_DIR = out_dir
    # 工具和插件两侧的 ID 都在此核对。D/K 共用插件的「基伤」输入 ID，
    # 英文显示名随武器族消歧，输出保留各自的引擎代码。
    pool_ids = set().union(
        *(riven_pools.class_pool(key) for key in APP.optimizer.RIVEN_BASE_VALUES)
    )
    STAT_DEFS = {}
    for code in sorted(APP.optimizer.RIVEN_RULES):
        engine_id = portable_riven.TRAIT_IDS[code]
        sid = riven_rating._ENGINE_STAT.get(engine_id, engine_id)
        if code == "D":
            sid = "melee_damage"
        if sid.startswith("damage_to_"):
            sid = sid.replace("damage_to_", "damage_vs_", 1)
        if sid not in parser.RIVEN_STAT_ALIASES or sid not in pool_ids:
            raise ValueError(f"未知插件词条 {code} -> {sid}")
        family = "melee" if code == "K" else "rifle"
        STAT_DEFS[code] = {
            "code": code,
            "stat_id": sid,
            "nameEn": formatters._riven_stat_en(
                sid, "base_damage_/_melee_damage" if code in {"D", "K"} else sid, family
            ),
            "nameZh": parser.RIVEN_STAT_ZH[sid],
        }


@dataclass(frozen=True)
class Scene:
    weapon: dict
    mode: dict
    build_issue: str | None

    @property
    def fire_mode(self):
        return self.mode["index"]

    @property
    def mode_key(self):
        kind = self.mode.get("kind")
        return kind if kind in EXTRA_MODES else str(self.fire_mode)

    @property
    def mode_suffix(self):
        key = self.mode_key
        return key if key in EXTRA_MODES else f"fm{key}"

    @property
    def key(self):
        return self.weapon["path"], self.mode_key

    @property
    def name(self):
        return f"{self.weapon['nameEn']} {self.mode_suffix}"

    @property
    def filename(self):
        path = self.weapon["path"]
        stem = re.sub(r"[^A-Za-z0-9_-]", "_", path.rsplit("/", 1)[-1])[:80]
        suffix = hashlib.sha256(path.encode("utf-8")).hexdigest()[:16]
        return f"{stem}_{suffix}__{self.mode_suffix}.json"

    @property
    def scenario_options(self):
        return {"scenario": self.mode["scenario"]} if "scenario" in self.mode else {}

    @property
    def target(self):
        target = dict(TARGET)
        if "headshotChance" in self.mode:
            target["headshotChance"] = self.mode["headshotChance"]
        return target


def is_exalted_cast(weapon):
    # 施法型记录的 exaltedCast 在引擎应用技能规则后才生成，目录已标记类型。
    return (weapon.get("exalted") or {}).get("tier") == "cast"


def extra_scenes(weapon, info_weapon, issue, rec, headshot_mode):
    modes = weapon["fireModes"]
    if weapon["weaponType"] not in MELEE_TYPES:
        # 同一武器的多个灵化攻击共用一个循环场景，取目录中首个灵化形态。
        incarnon = next((mode for mode in modes if mode.get("isIncarnonForm")), None)
        if incarnon is not None:
            from wfarsenal.manual_combat import reference as combat_reference

            # 窗口回放缺少这份数据时会使用常驻灵化。
            profile = combat_reference()["incarnon"].get(weapon["path"])
            if issue is None and not profile:
                issue = INCARNON_CYCLE_UNAVAILABLE
            scenario = {"incarnonCycle": True}
            if (
                profile
                and profile.get("chargeOn") not in {"direct_hits", "kills"}
                and headshot_mode.get("headshotChance", TARGET.get("headshotChance", 0.0)) < 1.0
            ):
                scenario["combatState"] = {"gaugeChargingHeadshots": True}
            mode = {
                **fields(incarnon, MODE_FIELDS),
                **headshot_mode,
                "kind": "cycle",
                "scenario": scenario,
            }
            yield Scene(info_weapon, mode, issue)
        return

    if rec is None:
        return
    from wfarsenal import melee_sequence

    row = melee_sequence.weapon_row(rec) or {}
    base = next((mode for mode in modes if mode["index"] == 0), None)
    if not row or base is None:
        return
    glaive = "GLAIVES_STANCE" in (rec.get("compatTags") or ()) and row.get("glaive")
    heavy_route = "glaive" if glaive else "combo"
    slam_route = "slamBullet" if melee_sequence.is_hammer(rec) else "slam"
    has_slam = any(
        event.get("event") in ("HeavySlam", "MeleeSlam") for event in row.get("slams") or ()
    )
    for kind, route, eligible in (
        ("heavy", heavy_route, weapon["buildAvailable"]),
        ("slam", slam_route, has_slam),
    ):
        if eligible:
            schedule = melee_sequence.build_schedule(rec, route=route, heavy_attack=True)
            if schedule and schedule["route"] == route:
                mode = {
                    **fields(base, MODE_FIELDS),
                    "kind": kind,
                    "scenario": {"heavyAttack": True, "combatState": {"meleeRoute": route}},
                }
                yield Scene(info_weapon, mode, issue)


def list_scenes(only_weapons: str | None, *, only_extra_modes=False):
    scenes = []
    extras = []
    weapons = APP.CATALOG["weapons"]
    requested = (
        {name.strip().casefold() for name in only_weapons.split(",")} if only_weapons else set()
    )
    seen = set()
    for weapon in weapons:
        if requested:
            identities = {
                str(weapon.get(key) or "").casefold() for key in ("path", "nameEn", "nameZh")
            }
            if not requested & identities:
                continue
            seen.update(requested & identities)
        info_weapon = fields(weapon, WEAPON_FIELDS)
        headshot_mode = (
            {"headshotChance": 1.0}
            if weapon["path"] in portable_riven.HEADSHOT_REQUIRED_WEAPONS
            else {}
        )
        issue = None
        if not weapon["buildAvailable"]:
            issue = weapon["buildIssue"] or "weapon build is unavailable"
        rec = None
        if weapon["weaponType"] in MELEE_TYPES and not is_exalted_cast(weapon):
            rec = APP.optimizer.resolve_record(
                APP.EXPORTS["weapons"], weapon["path"], kind="weapon"
            )
            if rec.get("exaltedCast"):
                rec = None
        for mode in () if only_extra_modes else weapon["fireModes"]:
            if not mode.get("available", True):
                continue
            info = {**fields(mode, MODE_FIELDS), **headshot_mode}
            if not info["kind"]:
                info["kind"] = (
                    "incarnon"
                    if info["isIncarnonForm"]
                    else "alternate"
                    if info["isAlternateFire"]
                    else "base"
                )
            if rec is not None and info["index"] == 0 and info["kind"] == "base":
                from wfarsenal import melee_sequence

                if melee_sequence.default_route(rec) != "combo":
                    schedule = melee_sequence.build_schedule(rec, route="combo")
                    if schedule and schedule["route"] == "combo":
                        info["scenario"] = {"combatState": {"meleeRoute": "combo"}}
            scenes.append(Scene(info_weapon, info, issue))
        extras.extend(extra_scenes(weapon, info_weapon, issue, rec, headshot_mode))
    if missing := requested - seen:
        raise ValueError(f"目录未找到指定武器 {', '.join(sorted(missing))}")
    scenes.extend(extras)
    if (not scenes and not only_extra_modes) or len({scene.filename for scene in scenes}) != len(
        scenes
    ):
        raise ValueError("场景目录为空或文件名重复")
    return scenes


@lru_cache(maxsize=None)
def reserve_ammo(path):
    from wfarsenal.event_profiles import battery_profiles

    if APP.CATALOG_WEAPONS_BY_PATH[path]["weaponType"] in MELEE_TYPES or path in battery_profiles():
        return None
    record = APP.optimizer.resolve_record(APP.EXPORTS["weapons"], path, kind="weapon")
    for key in ("reserveAmmo", "reserve", "ammoCapacity", "maxAmmo", "AmmoMax"):
        value = record.get(key)
        if type(value) is int and value > 0:
            return value
    return RESERVE_AMMO.get(path)


def identity(scene: Scene):
    return {
        "schema": SCHEMA,
        "engineFingerprint": FINGERPRINT,
        "buildLabel": BUILD_LABEL,
        "target": TARGET,
        "weapon": scene.weapon,
        "fireMode": scene.fire_mode,
        "mode": scene.mode,
        "buildIssue": scene.build_issue,
    }


def read_scene(path: Path, scene: Scene, *, allow_missing_traits=False):
    doc = json.loads(path.read_bytes())
    if not isinstance(doc, dict) or any(
        doc.get(key) != val for key, val in identity(scene).items() if key != "buildIssue"
    ):
        raise ValueError(f"场景元数据不符 {path}")
    expected = doc.pop("sha256", None)
    if expected != digest(doc):
        raise ValueError(f"场景校验和不符 {path}")
    build, traits = doc["build"], doc["traits"]
    if doc.get("status") == "unsupported":
        issue = doc["buildIssue"]
        if (
            build is not None
            or traits is not None
            or not isinstance(issue, str)
            or not (issue == scene.build_issue or missing_damage(issue))
        ):
            raise ValueError(f"不可配装场景状态不符 {path}")
        return doc
    if doc.get("status") != "ok" or doc["buildIssue"] != scene.build_issue or scene.build_issue:
        raise ValueError(f"场景状态不符 {path}")
    slots = [row["slot"] for row in build["mods"]]
    without = {int(slot) for slot in build["without"]}
    if sorted(set(slots)) != slots or not set(slots) <= set(range(8)):
        raise ValueError(f"普通槽位无效 {path}")
    if without | set(build["unmodeledSlots"]) != set(slots) or without & set(
        build["unmodeledSlots"]
    ):
        raise ValueError(f"拆槽回执不完整 {path}")
    if build["capacity"]["valid"] is not True or not all(
        math.isfinite(build[key]) and build[key] >= 0 for key in ("full_dps", "kpm")
    ):
        raise ValueError(f"回放结果无效 {path}")
    if build["stance"] and not all(key in build["stance"] for key in STANCE_FIELDS):
        raise ValueError(f"架势字段缺失 {path}")
    if not math.isfinite(traits["baseline_kpm"]) or traits["baseline_dps"] != build["full_dps"]:
        raise ValueError(f"词条基准回执不符 {path}")
    expected_codes = candidate_sets(scene.weapon["path"], scene.fire_mode)[0]
    for sign, lines in zip(TRAIT_SIGNS, expected_codes, strict=True):
        rows = traits[sign]
        codes = [row["code"] for row in rows]
        actual = set(codes)
        missing = lines.keys() - actual
        if (
            codes != sorted(actual)
            or actual - lines.keys()
            or (missing and not allow_missing_traits)
        ):
            raise ValueError(f"词条矩阵无效 {path} {sign}，缺项 {sorted(missing)}")
        for row in rows:
            if unexpected := row.keys() - TRAIT_ROW_FIELDS:
                raise ValueError(f"词条包含未知字段 {path} {row['code']} {sorted(unexpected)}")
            if row["stat_id"] != STAT_DEFS[row["code"]]["stat_id"] or row["unit"] not in UNITS:
                raise ValueError(f"词条 ID 或单位不符 {path} {row['code']}")
            gains = (row["pct_31"], row["pct_21"])
            no_gain = all(gain is None for gain in gains)
            baseline = "baseline_weakpoint_kpm" if row["weakpoint"] else "baseline_kpm"
            valid_gain = row["modeled"] and (
                all(gain is not None for gain in gains)
                or (traits[baseline] == 0 and (no_gain or "elem_build_roll" in row))
            )
            if not (valid_gain or (not row["modeled"] and no_gain)):
                raise ValueError(f"词条收益缺项 {path} {row['code']}")
            if "elem_build" in row or "elem_build_roll" in row:
                roll = row.get("elem_build_roll")
                if (
                    sign != "positive"
                    or row.get("elem_build") not in ELEMENT_TARGETS.get(row["stat_id"], ())
                    or roll not in (None, *ROLL_LABELS)
                    or not row["modeled"]
                    or any(
                        row[f"pct_{label}"] is None or row[f"pct_{label}"] < -0.05
                        for label in ((roll,) if roll else ROLL_LABELS)
                    )
                ):
                    raise ValueError(f"同套元素对照无效 {path} {row['code']}")
    return doc


def make_request(scene, result, ctx):
    optimizer = APP.optimizer
    ev, weapon = ctx["evaluation"], ctx["weapon"]
    stance, exilus = result.get("stance"), result.get("exilus")
    summary = optimizer._forma_summary(
        weapon, ev.cards, stance=stance, exilus=exilus, include_placement=True
    )
    if not summary["fits"]:
        raise ValueError("引擎给出的参考配装无法放进容量")
    mods = result["build"]["mods"]
    slot_polarities = [optimizer._slot_polarity(p) for p in weapon.get("polarities", ())[:8]]
    slot_polarities += [None] * (8 - len(slot_polarities))
    equipped = []
    for slot, placement in enumerate(summary["placement"]):
        if placement is not None:
            slot_polarities[slot] = placement["polarity"]
            equipped.append({**mods[placement["origin"]], "slot": slot})
    chosen = ((result.get("autoLoadout") or {}).get("arcane") or {}).get("chosen") or []
    arcanes = []
    for arc in chosen:
        meta = APP.MANUAL_ARCANES_BY_PATH[arc["path"]]
        arcanes.append(
            {
                **arc,
                "nameEn": meta["nameEn"],
                "nameZh": meta["nameZh"],
                "rank": arc.get("rank", meta["maxRank"]),
            }
        )
    # 与插件使用同一套灵化、赋能、架势和扩展槽回放选项。
    options = portable_riven._build_replay_options({**result, "arcanes": arcanes}, result["build"])
    portable_riven._check_scenario(options.get("scenario", {}), scene.scenario_options)
    if summary["stance"]:
        options.setdefault("specialSlotPolarities", {})["stance"] = stance["polarity"]
    if summary["exilus"]:
        options.setdefault("specialSlotPolarities", {})["exilus"] = exilus["polarity"]
    request = {
        **scene.target,
        "weapon": scene.key[0],
        "fireMode": scene.fire_mode,
        "metric": "windowedDps",
        "damageModel": "target",
        "slotPolarities": [p or "AP_UNIVERSAL" for p in slot_polarities],
        "incarnonChoices": result.get("incarnonChoices") or {},
        "mods": [fields(row, ("path", "rank", "slot")) for row in equipped],
    }
    return damage_engine.with_portable_replay_options(request, options), arcanes, summary


def replay(request):
    response = APP.simulate_manual_payload(request)
    window, validation = damage_engine.validate_fixed_replay(response, request)
    if (response.get("incarnonChoices") or {}) != request["incarnonChoices"]:
        raise ValueError("固定回放的灵化路线不符")
    scenario = request.get("scenario") or {}
    if scenario.get("incarnonCycle") and (
        INCARNON_CYCLE_UNAVAILABLE in window["unsupported"]
        or any(point.get("form") == "selected" for point in window["points"])
    ):
        raise ValueError(INCARNON_CYCLE_UNAVAILABLE)
    route = (scenario.get("combatState") or {}).get("meleeRoute")
    if route:
        melee = window.get("melee") or {}
        if melee.get("route") != route or (
            scenario.get("heavyAttack")
            and route == "combo"
            and melee.get("label") != "heavy-attack"
        ):
            raise ValueError(f"固定回放的近战路线不符 {route}，实际为 {melee}")
    return window, validation, response


def reference_replay(request, result, arcanes, summary):
    window, capacity, response = replay(request)
    fitted = 0
    if not capacity["valid"]:
        fitted_request = portable_riven._fit_reference_slots(request, capacity)
        fitted = sum(
            a != b
            for a, b in zip(
                fitted_request["slotPolarities"], request["slotPolarities"], strict=True
            )
        )
        request = fitted_request
        window, capacity, response = replay(request)
    if not capacity["valid"]:
        raise ValueError("参考配装固定回放超过容量")
    replaced = None
    if alternate := portable_riven._serration_alternative(request):
        slot, alternative = alternate
        alt_window, alt_capacity, alt_response = replay(alternative)
        if (
            alt_capacity["valid"]
            and alt_capacity["capacity"] < capacity["capacity"]
            and math.isclose(window["windowDps"], alt_window["windowDps"], rel_tol=1e-12)
        ):
            request, window, capacity, response = (
                alternative,
                alt_window,
                alt_capacity,
                alt_response,
            )
            replaced = slot
    active_stacks = {
        arc["path"]
        for arc in response.get("arcanes", ())
        if arc.get("applied") and (arc.get("stacks") or 0) > 0
    }
    panel = response["moddedStats"]
    if active_stacks and request.get("arcanes"):
        neutral = [
            {**arc, "stacks": 0} if arc["path"] in active_stacks else arc
            for arc in request["arcanes"]
        ]
        _, _, initial = replay({**request, "arcanes": neutral})
        panel = initial["moddedStats"] or panel
    without = {}
    unmodeled = []
    unsupported = set(response.get("unsupported") or ())
    for mod in request["mods"]:
        slot, path = mod["slot"], mod["path"]
        if f"weapon-mod-mechanic-unmodeled:{path}" in unsupported:
            unmodeled.append(slot)
            continue
        removal = {**request, "mods": [row for row in request["mods"] if row["slot"] != slot]}
        dropped, validation, _ = replay(removal)
        if not validation["valid"]:
            raise ValueError(f"拆下槽 {slot} 后回放容量无效")
        without[str(slot)] = rounded(dropped["windowDps"], 2)
    mods = []
    for row in sorted(capacity["assignments"], key=lambda item: item["slot"]):
        if row["slot"] >= 8:
            continue
        meta = APP.MANUAL_MODS_BY_PATH[row["path"]]
        mods.append(
            {
                **fields(row, ("slot", "path", "rank", "drain", "slotPolarity")),
                **fields(meta, ("nameEn", "nameZh")),
                "polarity": row["modPolarity"],
            }
        )
    stats = {key: panel.get(key) for key in PANEL_FIELDS}
    stats = {
        key: {kind: rounded(number, 5) for kind, number in value.items()}
        if isinstance(value, dict)
        else rounded(value, 5)
        if value is not None
        else None
        for key, value in stats.items()
    }
    stance = response.get("stance") or result.get("stance")
    if stance:
        stance = {**stance, "drain": stance.get("drain", -int(stance.get("capacityGain") or 0))}
    exilus = result.get("exilus")
    return {
        "incarnonChoices": request["incarnonChoices"],
        "innateElement": result.get("innateElement"),
        "stance": fields(stance, STANCE_FIELDS) if stance else None,
        "exilus": fields({**exilus, **(response.get("exilus") or {})}, EXILUS_FIELDS)
        if exilus
        else None,
        "arcanes": [{key: arc[key] for key in ARCANE_FIELDS if key in arc} for arc in arcanes],
        "full_dps": rounded(window["windowDps"], 2),
        "kpm": rounded(window["killsPerMinute"], 4),
        "capacity": {
            "used": capacity["capacity"],
            "limit": capacity["capacityLimit"],
            "forma": summary["total"] + fitted,
            "valid": capacity["valid"],
        },
        "stats": stats,
        "mods": mods,
        "without": without,
        "unmodeledSlots": unmodeled,
        "serrationSlot": replaced,
    }


@lru_cache(maxsize=64)
def candidate_sets(path, fire_mode):
    optimizer = APP.optimizer
    meta = APP.CATALOG_WEAPONS_BY_PATH[path]
    record = optimizer.resolve_record(APP.EXPORTS["weapons"], path, kind="weapon")
    kind = optimizer._riven_type(meta["rivenType"], record)
    empty = ({}, {})
    # 目录无倾向性的异常/NPC/显赫条目不能合法装紫卡。
    if not meta.get("disposition") or kind not in optimizer.RIVEN_BASE_VALUES:
        return empty, empty
    selected, _ = optimizer._select_fire_mode(record, fire_mode, weapon_type=meta["weaponType"])
    bases = {
        **optimizer.RIVEN_BASE_VALUES.get(kind, {}),
        **optimizer._template_bases(TEMPLATES, kind),
    }
    if kind == "melee":
        # 模板以百分比尺度存 0.009，词条范围的基值是 81，上游兜底仍是旧值 8.1。
        bases["PA"] = riven_analysis._base_value("melee_parry_angle", kind)
    fusion = optimizer._fusion_codes_from_templates(TEMPLATES, kind)
    allowed = [
        optimizer._riven_allowed_codes(TEMPLATES, kind, selected, negative=negative)
        for negative in (False, True)
    ]
    pools = []
    for count in (3, 2):
        signs = []
        for negative, expected in zip((False, True), allowed, strict=True):
            lines, _ = optimizer._candidate_lines(
                kind,
                meta["disposition"],
                (),
                negative=negative,
                bases=bases,
                available_fusion_codes=fusion,
                structure_scale=optimizer.theoretical_structure_scale(
                    count, True, negative=negative
                )
                / optimizer.RIVEN_ROLL_HIGH,
                allowed_codes=expected,
            )
            candidates = {line.code: line for line in lines}
            if candidates.keys() != expected:
                raise ValueError(
                    f"合法词条候选不完整 {meta['nameEn']} fm{fire_mode} {count}+1- "
                    f"{TRAIT_SIGNS[negative]}，缺项 {sorted(expected - candidates.keys())}"
                )
            signs.append(candidates)
        pools.append(tuple(signs))
    return tuple(pools)


def trait_unit(line):
    if line.unit == "percent":
        return "%"
    if line.unit == "flat":
        return {"P": "m", "T": "m", "N": "s", "B": "", "PA": "°"}[line.code]
    if line.unit == "x":
        return "x"
    raise ValueError(f"未知词条单位 {line.code} {line.unit}")


def element_search_pool(optimizer, candidates, cards):
    # 保留参考卡的同族替换和每种元素的强卡，限制事件回放的候选数。
    families = {optimizer._mod_family(card) for card in cards}
    keys = {card.key for card in cards}
    for element in sorted(optimizer.BASE_ELEMENTS):
        amounts = [
            (sum(effect.value for effect in card.effects if effect.damage_type == element), card)
            for card in candidates
        ]
        ranked = sorted(
            ((amount, card) for amount, card in amounts if amount > 0),
            key=lambda item: (-item[0], item[1].drain, item[1].key),
        )
        keys.update(card.key for _, card in ranked[:2])
    return [
        card
        for card in candidates
        if card.required or card.key in keys or optimizer._mod_family(card) in families
    ]


def exilus_trial(ctx, special):
    special_card = (
        APP._utility_card_at_rank(
            APP.UTILITY_MODS_BY_PATH[special["path"]], ctx["scenario"], special["rank"]
        )
        if special
        else None
    )
    return {
        "special_cards": (special_card,) if special_card else (),
        "special_capacity_cost": int(special.get("drain") or 0) if special else 0,
    }


def trait_matrix(scene, ctx, build, *, missing=None):
    optimizer = APP.optimizer
    if ctx["metric"] != "killsPerMinute":
        raise ValueError(f"求解评分口径不是 KPM {ctx['metric']}")
    if scene.scenario_options:
        portable_riven._check_scenario(
            optimizer._scenario_context(ctx["scenario"]), scene.scenario_options
        )
    groups31, groups21 = candidate_sets(scene.weapon["path"], scene.fire_mode)
    codes = [set(group) for group in groups31] if missing is None else missing
    weapon = ctx["weapon"]
    records = [
        optimizer.resolve_record(APP.EXPORTS["mods"], mod["path"], kind="mod")
        for mod in build["mods"]
    ]
    shells = optimizer._set_shells(APP.EXPORTS["mods"])
    cards = tuple(
        optimizer._mod_card(
            record,
            ctx["scenario"],
            allow_empty=True,
            rank=mod["rank"],
            set_shell=shells.get(optimizer._set_key(record.get("modSet"))),
        )
        for record, mod in zip(records, build["mods"], strict=True)
    )
    evaluate = dict(
        scenario=ctx["scenario"],
        metric=ctx["metric"],
        passives=ctx["passives"],
        slot_limit=len(cards) + 1,
        capacity_limit=999,
        target_context=ctx["benchmarkContext"],
        reorder_elements=False,
        **exilus_trial(ctx, build["exilus"]),
    )
    ev = optimizer._evaluate(weapon, cards, None, **evaluate)
    weakpoint_codes = WEAKPOINT_CODES & codes[0] if scene.target["headshotChance"] < 1.0 else set()
    baseline_wp = None
    if weakpoint_codes:
        weakpoint_evaluate = {
            **evaluate,
            "target_context": {
                **evaluate["target_context"],
                "headshotChance": 1.0,
                "hit_location": "head",
            },
        }
        baseline_wp = optimizer._evaluate(weapon, cards, None, **weakpoint_evaluate)
    reserve = reserve_ammo(scene.key[0])
    matrix = {
        "baseline_kpm": rounded(ev.score, 4),
        "baseline_dps": build["full_dps"],
        "baseline_weakpoint_kpm": rounded(baseline_wp.score, 4) if baseline_wp else None,
        "reserve_ammo": reserve,
        "low_ammo": reserve is not None and reserve <= 30,
    }
    element_pool, seeds = (), ()
    if ELEMENT_CODES & codes[0]:
        element_pool = element_search_pool(optimizer, ctx["candidates"], cards)
        # 优先保留可换元素配对的留槽方案和拆卡损失最小的普通卡。
        least_impact = sorted(
            range(len(cards)),
            key=lambda i: -build["without"].get(str(build["mods"][i]["slot"]), -1),
        )[:2]
        seeds = (
            [cards]
            if len(cards) < 8
            else [
                cards[:i] + cards[i + 1 :]
                for i, card in enumerate(cards)
                if i in least_impact
                or any(effect.damage_type in optimizer.BASE_ELEMENTS for effect in card.effects)
            ]
        )
    neutral = optimizer._neutral_negative_codes(weapon)
    for i, sign in enumerate(TRAIT_SIGNS):
        matrix[sign] = rows = []
        for code in sorted(codes[i]):
            line31, line21 = groups31[i][code], groups21[i][code]
            supported = [bool(line.effect and line.effect.supported) for line in (line31, line21)]
            if supported[0] != supported[1]:
                raise ValueError(f"两种结构的词条支持状态不一致 {code}")
            row = {
                **STAT_DEFS[code],
                "unit": trait_unit(line31),
                "spliced": code in optimizer._FUSION_LINE_CODES,
                "modeled": supported[0],
                "neutral": sign == "negative" and code in neutral,
                "weakpoint": supported[0] and sign == "positive" and code in weakpoint_codes,
                "reoptimized": supported[0] and sign == "positive" and code in ELEMENT_CODES,
            }
            options, baseline = evaluate, ev
            if row["weakpoint"]:
                options, baseline = weakpoint_evaluate, baseline_wp
            for label, line in (("31", line31), ("21", line21)):
                row[f"shown_{label}"] = rounded(line.value * (-1 if line.negative else 1), 2)
                pct = None
                if row["modeled"]:
                    riven = optimizer._card_from_lines([line])
                    evaluated = optimizer._evaluate(
                        weapon,
                        cards,
                        riven,
                        fixed_riven_index=len(cards),
                        **options,
                    )
                    if evaluated is None:
                        raise ValueError(f"单词条回放失败 {code} {sign} {label}")
                    score = evaluated.score
                    if row["reoptimized"]:
                        score = max(
                            score,
                            element_reoptimized_score(ctx, element_pool, seeds, riven, evaluate),
                        )
                    if baseline.score > 0:
                        pct = rounded(optimizer._percent_gain(score, baseline.score), 2)
                row[f"pct_{label}"] = pct
            rows.append(row)
    return matrix


def element_search_options(ctx, exilus):
    optimizer = APP.optimizer
    passives = ctx["passives"]
    if special := exilus["special_cards"]:
        holder = passives or optimizer._Card("__passives__", None, 0, (), (), 0, None)
        passives = replace(holder, special_cards=(*holder.special_cards, *special))
    return dict(
        scenario=ctx["scenario"],
        metric=ctx["metric"],
        passives=passives,
        # 搜索器把扩展槽效果放在被动里，先从总容量预留扩展槽消耗。
        capacity_limit=ctx["capacityLimit"] - exilus["special_capacity_cost"],
        target_context=ctx["benchmarkContext"],
    )


def element_reoptimized_score(ctx, candidates, seeds, riven, evaluate):
    optimizer = APP.optimizer
    weapon = ctx["weapon"]
    score_options = element_search_options(ctx, evaluate)
    seed = max(
        (
            trial
            for mods in seeds
            if (trial := optimizer._evaluate(weapon, mods, riven, slot_limit=8, **score_options))
        ),
        key=lambda trial: trial.score,
        default=None,
    )
    optimized = optimizer.optimize_for_element(
        weapon,
        candidates,
        riven,
        slot_count=8,
        profile="fast",
        beam_width=1,
        shortlist=8,
        seed_cards=seed.cards if seed else None,
        **score_options,
    )
    return max(optimized.score, seed.score) if seed else optimized.score


def element_baseline_rows(document):
    if document["status"] != "ok":
        return []
    rows = []
    for row in document["traits"]["positive"]:
        if not row["modeled"] or "elem_build" in row:
            continue
        gains = [row[f"pct_{roll}"] for roll in ROLL_LABELS]
        negative = any(gain is not None and gain < -0.05 for gain in gains)
        if row["stat_id"] in COMPOUND_ELEMENT_TARGETS:
            if negative and not document.get("compound_element_baselines_checked"):
                rows.append(row)
        elif row["code"] in ELEMENT_CODES and not document.get("element_baselines_checked"):
            if negative or all(gain is None or gain <= 0 for gain in gains):
                rows.append(row)
    return rows


def element_trial_gain(optimizer, weapon, baseline, riven, damage_type, options):
    cards = baseline.cards
    damage = {key: value for key, value in baseline.stats.damage_types.items() if value > 0}
    # 与默认词条试算一样，临时增加试算槽，固定所有普通 MOD。
    # 每个位置单独回放，仅比较保留整套元素、且指定元素确实得到增强的位置。
    evaluate = {
        **options,
        "capacity_limit": options["capacity_limit"] + optimizer._capacity((riven,)),
        "slot_limit": len(cards) + 1,
        "reorder_elements": False,
        "required_damage_type": damage_type,
    }
    best_score = None
    for position in range(len(cards) + 1):
        trial = optimizer._evaluate(weapon, cards, riven, fixed_riven_index=position, **evaluate)
        if trial is None:
            continue
        trial_damage = trial.stats.damage_types
        if {
            key for key, value in trial_damage.items() if value > 0
        } == damage.keys() and trial_damage[damage_type] > damage[damage_type]:
            best_score = trial.score if best_score is None else max(best_score, trial.score)
    return None if best_score is None else optimizer._percent_gain(best_score, baseline.score)


def apply_element_baselines(scene, document, rows):
    if not rows:
        return
    optimizer = APP.optimizer
    build = document["build"]
    ctx = fixed_reference_context(scene, build)
    if ctx["metric"] != "killsPerMinute":
        raise ValueError(f"求解评分口径不是 KPM {ctx['metric']}")
    weapon = ctx["weapon"]
    options = element_search_options(ctx, exilus_trial(ctx, build["exilus"]))
    target_types = optimizer._ELEMENT_TARGET_TYPES

    @lru_cache(maxsize=None)
    def baseline(damage_type):
        try:
            trial = optimizer.optimize_for_element(
                weapon,
                ctx["candidates"],
                riven=None,
                required_damage_type=damage_type,
                profile="fast",
                slot_count=8,
                **options,
            )
        except optimizer.SimulationError as exc:
            if str(exc) != f"no capacity-valid build satisfies damage type {damage_type}":
                raise
            return None
        return trial if trial.score > 0 else None

    groups = candidate_sets(scene.weapon["path"], scene.fire_mode)
    for row in rows:
        compound = row["stat_id"] in COMPOUND_ELEMENT_TARGETS
        candidates = (
            (element, trial)
            for element in ELEMENT_TARGETS[row["stat_id"]]
            if (trial := baseline(target_types[element])) is not None
        )
        rivens = [optimizer._card_from_lines([positive[row["code"]]]) for positive, _ in groups]
        for element, trial in sorted(candidates, key=lambda pair: pair[1].score, reverse=True):
            damage_type = target_types[element]
            gains = [
                element_trial_gain(optimizer, weapon, trial, riven, damage_type, options)
                for riven in rivens
            ]
            # 固有或融合 MOD 元素也能命中引擎约束，需确认紫卡能加入该元素。
            if all(gain is None for gain in gains):
                continue
            accepted = []
            for roll, gain in zip(ROLL_LABELS, gains, strict=True):
                default_gain = row[f"pct_{roll}"]
                # 另一档可能已有合法的默认基准收益，保留该档的直接对照。
                if compound and (default_gain is None or default_gain >= -0.05):
                    continue
                if gain is not None and gain >= -0.05:
                    row[f"pct_{roll}"] = rounded(gain, 2)
                    accepted.append(roll)
            if accepted:
                row["elem_build"] = element
                if len(accepted) == 1:
                    row["elem_build_roll"] = accepted[0]
            break
        # 分别记录已试算，即便该场景没有找到合法或非负的对照。
        if compound:
            document["compound_element_baselines_checked"] = True
        else:
            document["element_baselines_checked"] = True


def missing_damage(issue):
    return issue.endswith((" has no damage data", " has no direct damage types"))


def read_ranged_seed(directory, scene):
    if (
        directory is None
        or scene.weapon["weaponType"] in MELEE_TYPES
        or scene.scenario_options
        or scene.target != TARGET
    ):
        return None
    path = directory / scene.filename
    try:
        doc = json.loads(path.read_bytes())
        checksum = doc.pop("sha256", None)
        if checksum != digest(doc):
            raise ValueError("校验和不符")
        if (
            doc["schema"] != SCHEMA
            or doc["buildLabel"] != BUILD_LABEL
            or doc["target"] != TARGET
            or doc["weapon"]["path"] != scene.weapon["path"]
            or doc["fireMode"] != scene.fire_mode
            or doc.get("buildIssue")
            or doc.get(
                "status", "ok" if doc["engineFingerprint"] == LEGACY_RANGED_FINGERPRINT else None
            )
            != "ok"
        ):
            return None
        build = doc["build"]
        if not isinstance(build, dict) or not build.get("mods"):
            raise ValueError("缺少普通 MOD 配装")
        return build
    except FileNotFoundError:
        return None
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        print(f"跳过无效旧配装 {path} ({exc})", flush=True)
        return None


def reuse_ranged_build(payload, seed):
    # 旧表记录的是已替换的膛线。先还原同槽重口径，让新引擎重做替换判定。
    mods = [fields(row, ("path", "rank", "slot")) for row in seed["mods"]]
    for row in mods:
        if row["slot"] == seed["serrationSlot"]:
            row["path"] = portable_riven._HEAVY_CALIBER
    arcanes = seed["arcanes"]
    options = portable_riven._build_replay_options(seed, seed)
    result, ctx = APP.build_weapon_payload(
        {
            **payload,
            **options,
            "autoArcane": False,
            "autoIncarnon": False,
            "incarnonChoices": seed["incarnonChoices"],
        },
        return_engine_context=True,
        fixed_mods=mods,
    )
    # 保留旧配装的槽位与极化方案，所有输出 DPS、拆槽和容量用新引擎回放。
    polarities = list(ctx["weapon"].get("polarities", ())[:8])
    polarities += [None] * (8 - len(polarities))
    for row in seed["mods"]:
        polarities[row["slot"]] = row["slotPolarity"]
    request = {
        **TARGET,
        "weapon": payload["weapon"],
        "fireMode": payload["fireMode"],
        "metric": "windowedDps",
        "damageModel": "target",
        "slotPolarities": [p or "AP_UNIVERSAL" for p in polarities],
        "incarnonChoices": result["incarnonChoices"],
        "mods": mods,
    }
    result["exilus"] = seed["exilus"]
    return result, ctx, damage_engine.with_portable_replay_options(request, options), arcanes


def scene_payload(scene):
    target = scene.target
    if target.get("headshotChance") == 1.0:
        # /api/build 在只传 hitLocation 时把基准设为 head，并归一出 1.0 的弱点率。
        target.pop("headshotChance")
        target["hitLocation"] = "head"
    return {
        **target,
        "weapon": scene.key[0],
        "fireMode": scene.fire_mode,
        **scene.scenario_options,
        "metric": "attackDps" if scene.weapon["weaponType"] in MELEE_TYPES else "sustainedDps",
        "profile": "standard",
    }


def build_scene(scene: Scene, reuse_ranged_dir=None):
    path = OUT_DIR / scene.filename
    started = time.monotonic()
    document = identity(scene)
    document.update(status="unsupported", build=None, traits=None)
    if not scene.build_issue:
        payload = {**scene_payload(scene), "autoArcane": True, "autoIncarnon": True}
        try:
            seed = read_ranged_seed(reuse_ranged_dir, scene)
            if seed:
                try:
                    result, ctx, request, arcanes = reuse_ranged_build(payload, seed)
                    build = reference_replay(
                        request, result, arcanes, {"total": seed["capacity"]["forma"]}
                    )
                except (ValueError, TypeError, KeyError, IndexError, AttributeError) as exc:
                    print(f"{scene.name} | 旧配装无法复算，重新搜索 ({exc})", flush=True)
                    seed = None
                else:
                    print(f"{scene.name} | 已复用旧远程配装并回放，重新计算词条", flush=True)
            if seed is None:
                result, ctx = APP.build_weapon_payload(payload, return_engine_context=True)
                request, arcanes, summary = make_request(scene, result, ctx)
                build = reference_replay(request, result, arcanes, summary)
            document.update(status="ok", build=build, traits=trait_matrix(scene, ctx, build))
        except ValueError as exc:
            if not missing_damage(str(exc)):
                raise
            document["buildIssue"] = str(exc)
    apply_element_baselines(scene, document, element_baseline_rows(document))
    write_json(path, {**document, "sha256": digest(document)})
    return document, time.monotonic() - started


def missing_trait_codes(scene, document):
    if document["status"] == "unsupported":
        return set(), set()
    missing = tuple(
        lines.keys() - {row["code"] for row in document["traits"][sign]}
        for sign, lines in zip(
            TRAIT_SIGNS, candidate_sets(scene.weapon["path"], scene.fire_mode)[0], strict=True
        )
    )
    if elements := ELEMENT_CODES & missing[0]:
        raise ValueError(f"{scene.name} 缺少需要重新配装的正单元素词条 {sorted(elements)}")
    return missing


def fixed_reference_context(scene, build):
    mods = [fields(row, ("path", "rank", "slot")) for row in build["mods"]]
    options = portable_riven._build_replay_options(build, build)
    # 固定配装入口会按目录满级容量预扣扩展槽，词条试算用存档中的等级和实扣容量。
    options.pop("exilus", None)
    payload = {
        **options,
        **scene_payload(scene),
        "incarnonChoices": build["incarnonChoices"],
        "autoArcane": False,
        "autoIncarnon": False,
        "minFill": 0,
    }
    _, ctx = APP.build_weapon_payload(payload, return_engine_context=True, fixed_mods=mods)
    if scene.scenario_options:
        portable_riven._check_scenario(
            APP.optimizer._scenario_context(ctx["scenario"]), scene.scenario_options
        )
    return ctx


def backfill_scene(scene):
    path = OUT_DIR / scene.filename
    started = time.monotonic()
    document = read_scene(path, scene, allow_missing_traits=True)
    missing = missing_trait_codes(scene, document)
    if not any(missing):
        return document, None
    build, traits = document["build"], document["traits"]
    ctx = fixed_reference_context(scene, build)
    updates = trait_matrix(scene, ctx, build, missing=missing)
    if updates["baseline_kpm"] != traits["baseline_kpm"]:
        raise ValueError(
            f"{scene.name} 固定配装 KPM 不符 {updates['baseline_kpm']} != {traits['baseline_kpm']}"
        )
    if (score := updates["baseline_weakpoint_kpm"]) is not None:
        if traits.get("baseline_weakpoint_kpm") not in (None, score):
            raise ValueError(f"{scene.name} 固定配装弱点 KPM 不符")
        traits["baseline_weakpoint_kpm"] = score
    for sign in TRAIT_SIGNS:
        traits[sign] = sorted([*traits[sign], *updates[sign]], key=lambda row: row["code"])
    write_json(path, {**document, "sha256": digest(document)})
    return document, time.monotonic() - started


def backfill_element_scene(scene):
    path = OUT_DIR / scene.filename
    started = time.monotonic()
    document = read_scene(path, scene)
    if not (rows := element_baseline_rows(document)):
        return document, None
    apply_element_baselines(scene, document, rows)
    write_json(path, {**document, "sha256": digest(document)})
    return document, time.monotonic() - started


def show_scene(scene, document, seconds):
    time_label = "复用" if seconds is None else f"{seconds:.2f} 秒"
    if not (build := document["build"]):
        print(f"{scene.name} | {time_label} | {document['buildIssue']}", flush=True)
        return
    arcanes = ", ".join(arc["nameEn"] or arc["path"] for arc in build["arcanes"]) or "无"
    exilus = build["exilus"] or {}
    print(
        f"{scene.name} | {time_label} | 赋能 {arcanes} | 扩展槽 "
        f"{exilus.get('nameEn') or '无'} modSet={exilus.get('modSet')} | "
        f"DPS {build['full_dps']:,.2f} | KPM {build['kpm']:.4f} | "
        f"拆卡 {len(build['without'])}/{len(build['mods'])}",
        flush=True,
    )
    if (
        scene.weapon["nameEn"] in {"Nikana Prime", "Rubico Prime", "Burston Prime"}
        and seconds is not None
    ):
        for sign in ("positive", "negative"):
            for row in document["traits"][sign]:
                if row["code"] in SAMPLE_TRAITS:
                    pct31 = "—" if row["pct_31"] is None else f"{row['pct_31']:.2f}%"
                    pct21 = "—" if row["pct_21"] is None else f"{row['pct_21']:.2f}%"
                    print(
                        f"  {sign} {row['code']} -> {row['stat_id']} | "
                        f"3+1- {row['shown_31']}{row['unit']} / {pct31} | "
                        f"2+1- {row['shown_21']}{row['unit']} / {pct21}",
                        flush=True,
                    )


def run_shard(args, scenes):
    pending = []
    backfill = args.backfill_missing_traits or args.backfill_element_baselines
    additions = 0
    for scene in scenes:
        path = args.out_dir / scene.filename
        if args.backfill_missing_traits:
            doc = read_scene(path, scene, allow_missing_traits=True)
            if any(missing := missing_trait_codes(scene, doc)):
                additions += sum(map(len, missing))
                pending.append((scene, "traits"))
            continue
        try:
            doc = read_scene(path, scene)
        except FileNotFoundError:
            if backfill:
                raise
            pending.append((scene, "build"))
        except (ValueError, TypeError, KeyError, IndexError, AttributeError) as exc:
            if backfill:
                raise
            print(f"重算无效场景 {path} ({exc})", flush=True)
            pending.append((scene, "build"))
        else:
            if rows := element_baseline_rows(doc):
                additions += len(rows)
                pending.append((scene, "elements"))
            elif not backfill:
                show_scene(scene, doc, None)
    if backfill:
        checked = len(scenes)
        print(
            f"已检查 {checked} 个场景，{len(pending)} 个场景待补 {additions} 行词条，"
            f"其余 {checked - len(pending)} 个场景已齐备或不适用",
            flush=True,
        )
    else:
        print(f"本片 {len(scenes)} 个场景，待算 {len(pending)} 个", flush=True)
    if not pending:
        return
    errors = []
    context = multiprocessing.get_context("spawn")
    # Pipe 和 wait 不分配 SemLock，每个进程一次只接收一个场景。
    workers = {}
    running = {}
    jobs = iter(pending)

    def dispatch(conn):
        job = next(jobs, None)
        try:
            conn.send(job)
        except (BrokenPipeError, ConnectionResetError):
            conn.close()
            if job is not None:
                scene, _ = job
                errors.append(scene.key)
                print(f"场景失败 {scene.name} {scene.key} (工作进程已退出)", file=sys.stderr)
            return
        if job is not None:
            running[conn] = job[0]

    try:
        for _ in range(min(args.workers, len(pending))):
            conn, child = context.Pipe()
            process = context.Process(
                target=worker,
                args=(child, args.engine_dir, args.out_dir, args.reuse_ranged_dir),
                kwargs={"variant": args.variant},
            )
            with child:
                process.start()
            workers[conn] = process
            dispatch(conn)
        while running:
            for conn in wait(running):
                scene = running.pop(conn)
                try:
                    error, doc, seconds = conn.recv()
                except (EOFError, ConnectionResetError):
                    error, doc, seconds = "计算进程提前退出", None, None
                    conn.close()
                if error is None:
                    if not backfill:
                        show_scene(scene, doc, seconds)
                    elif args.backfill_element_baselines:
                        print(f"{scene.name} | 同套试算 {seconds or 0:.2f} 秒", flush=True)
                else:
                    print(
                        f"场景失败 {scene.name} {scene.key} ({error})", file=sys.stderr, flush=True
                    )
                    errors.append(scene.key)
                if not conn.closed:
                    dispatch(conn)
    finally:
        for conn, process in workers.items():
            conn.close()
            if process.is_alive() and (running or sys.exc_info()[0] is not None):
                process.terminate()
            process.join()
            process.close()
    failed = len(errors) + sum(1 for _ in jobs)
    if failed:
        raise ValueError(f"{failed} 个场景未完成，可续跑")
    if backfill:
        print(f"已完成 {len(pending)} 个场景的词条回填", flush=True)


def worker(conn, engine_dir, out_dir, reuse_ranged_dir, *, variant):
    with conn:
        setup_engine(engine_dir, out_dir, variant=variant)
        while (job := conn.recv()) is not None:
            scene, action = job
            try:
                if action == "elements":
                    document, seconds = backfill_element_scene(scene)
                elif action == "traits":
                    document, seconds = backfill_scene(scene)
                else:
                    document, seconds = build_scene(scene, reuse_ranged_dir)
            except Exception as exc:
                conn.send((f"{type(exc).__name__}: {exc}", None, None))
            else:
                conn.send((None, document, seconds))


class Catalog:
    def __init__(self, columns):
        self.columns = columns
        self.rows = []
        self.index = {}

    def intern(self, row):
        if row is None:
            return None
        values = [row[key] for key in self.columns]
        key = json_bytes(values)
        if key not in self.index:
            self.index[key] = len(self.rows)
            self.rows.append(values)
        return self.index[key]

    def export(self):
        return {"columns": self.columns, "rows": self.rows}


def compact_documents(scenes, records):
    provenance = {
        "schema": SCHEMA,
        "engineFingerprint": FINGERPRINT,
        "buildLabel": BUILD_LABEL,
        "target": TARGET,
    }
    catalogs = {
        "weapons": Catalog(WEAPON_FIELDS[1:]),
        "mods": Catalog(MOD_FIELDS),
        "arcanes": Catalog(ARCANE_FIELDS),
        "exilus": Catalog(EXILUS_FIELDS),
        "stance": Catalog(STANCE_FIELDS),
    }
    builds = {
        **provenance,
        "modColumns": ("mod", "rank", "drain", "slotPolarity"),
        "statsColumns": PANEL_FIELDS,
        "capacityColumns": ("used", "limit", "forma", "valid"),
        "weapons": {},
    }
    stat_index = {code: i for i, code in enumerate(STAT_DEFS)}
    traits = {
        **provenance,
        "columns": TRAIT_COLUMNS,
        "flags": FLAGS,
        "units": UNITS,
        "statColumns": STAT_COLUMNS,
        "stats": [[row[key] for key in STAT_COLUMNS] for row in STAT_DEFS.values()],
        "element_builds": ELEMENT_BUILDS,
        "treatment": "reference_mods_with_single_element_reoptimization_and_weakpoint_trials",
        "weapons": {},
    }
    # 先收录常规模式，保留已有装备字典的下标。
    for scene in sorted(
        scenes,
        key=lambda item: (
            item.mode_key in EXTRA_MODES,
            item.weapon["path"],
            item.fire_mode,
            item.mode_key,
        ),
    ):
        path, mode_key = scene.key
        doc = records[scene.filename]
        modes = builds["weapons"].setdefault(path, {})
        descriptor = {"weapon": catalogs["weapons"].intern(scene.weapon), "mode": scene.mode}
        trait_modes = traits["weapons"].setdefault(path, {})
        if doc["status"] == "unsupported":
            modes[mode_key] = {**descriptor, "buildIssue": doc["buildIssue"]}
            trait_modes[mode_key] = {
                "baseline_kpm": None,
                "baseline_dps": None,
                "positive": [],
                "negative": [],
            }
            continue
        build = doc["build"]
        mods = [None] * 8
        for row in build["mods"]:
            mods[row["slot"]] = [
                catalogs["mods"].intern(row),
                row["rank"],
                row["drain"],
                row["slotPolarity"],
            ]
        modes[mode_key] = {
            **fields(
                build, ("incarnonChoices", "innateElement", "full_dps", "kpm", "serrationSlot")
            ),
            **descriptor,
            "stance": catalogs["stance"].intern(build["stance"]),
            "exilus": catalogs["exilus"].intern(build["exilus"]),
            "arcanes": [
                catalogs["arcanes"].intern(fields(row, ARCANE_FIELDS)) for row in build["arcanes"]
            ],
            "capacity": [build["capacity"][key] for key in builds["capacityColumns"]],
            "stats": [build["stats"][key] for key in PANEL_FIELDS],
            "mods": mods,
            "without": [build["without"].get(str(slot)) for slot in range(8)],
        }
        matrix = doc["traits"]
        trait_modes[mode_key] = fields(
            matrix,
            (
                "baseline_kpm",
                "baseline_dps",
                "baseline_weakpoint_kpm",
                "reserve_ammo",
                "low_ammo",
            ),
        )
        if "headshotChance" in scene.mode:
            trait_modes[mode_key]["mode"] = fields(scene.mode, ("headshotChance",))
        for sign in TRAIT_SIGNS:
            trait_modes[mode_key][sign] = [
                [
                    stat_index[row["code"]],
                    UNITS.index(row["unit"]),
                    sum(mask for field, mask in FLAGS.items() if row.get(field)),
                    row["shown_31"],
                    row["pct_31"],
                    row["shown_21"],
                    row["pct_21"],
                ]
                for row in matrix[sign]
            ]
        elem_builds = {}
        for row in matrix["positive"]:
            if element := row.get("elem_build"):
                element_index = ELEMENT_BUILDS.index(element)
                roll = row.get("elem_build_roll")
                elem_builds[str(stat_index[row["code"]])] = (
                    [element_index, int(roll)] if roll else element_index
                )
        if elem_builds:
            trait_modes[mode_key]["elem_builds"] = elem_builds
    builds["catalog"] = {key: table.export() for key, table in catalogs.items()}
    return builds, traits


def merge(args, scenes, all_scenes):
    expected = {scene.filename: scene for scene in scenes}
    known = {scene.filename for scene in all_scenes}
    # 允许沿用含旧施法型重击文件的分片目录。
    known.update(
        Scene(weapon, {"index": 0, "kind": kind}, None).filename
        for weapon in APP.CATALOG["weapons"]
        if weapon["weaponType"] in MELEE_TYPES and is_exalted_cast(weapon)
        for kind in ("heavy", "slam")
    )
    records = {}
    for directory in args.in_dir:
        if not directory.is_dir():
            raise ValueError(f"输入目录不存在 {directory}")
        for path in sorted(
            path for suffix in ("fm*", *EXTRA_MODES) for path in directory.glob(f"*__{suffix}.json")
        ):
            if path.name not in known:
                raise ValueError(f"输入包含未知场景 {path}")
            if path.name not in expected:
                continue
            document = read_scene(path, expected[path.name])
            if path.name in records and json_bytes(records[path.name]) != json_bytes(document):
                raise ValueError(f"重复场景的结果不同 {path}")
            records[path.name] = document
    if missing := expected.keys() - records.keys():
        sample = ", ".join(expected[name].name for name in sorted(missing)[:12])
        raise ValueError(f"缺少 {len(missing)} 个场景 ({sample})")
    builds, traits = compact_documents(scenes, records)
    count = sum(doc["build"] is not None for doc in records.values())
    print(f"合并 {len(scenes)} 个场景，配装成功 {count} 个，已校验 {len(STAT_DEFS)} 个词条映射")
    for path, document in ((args.out_builds, builds), (args.out_traits, traits)):
        size = write_json(path, document, mode=0o644)
        message = f"{path} | {size:,} 字节"
        if len(scenes) != len(all_scenes):
            # 固定头部只计一份。参考配装的目录字典按样本比例外推。
            variable = len(json_bytes(document["weapons"]))
            if document is builds:
                variable += len(json_bytes(document["catalog"]))
            estimate = size - variable + variable * len(all_scenes) / len(scenes)
            message += f" | {len(all_scenes)} 场景线性估算 {estimate / 1e6:.2f} MB"
        print(message)


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--engine-dir", type=Path, required=True)
    cli.add_argument("--inputs-dir", type=Path, help="解压运行包所需的固定版上游数据镜像")
    cli.add_argument("--only-weapons", help="逗号分隔的精确中英文武器名或引擎路径")
    cli.add_argument("--out-dir", type=Path, help="逐场景输出目录")
    cli.add_argument("--reuse-ranged-dir", type=Path, help="从同工况的旧场景复用远程配装")
    cli.add_argument("--workers", type=int, default=1)
    cli.add_argument("--shard-total", type=int, default=1)
    cli.add_argument("--shard-index", type=int, default=0)
    target = cli.add_mutually_exclusive_group()
    target.add_argument(
        "--overguard",
        dest="variant",
        action="store_const",
        const="overguard",
        help="选择带超宏标准靶及其随包表",
    )
    target.add_argument(
        "--shield",
        dest="variant",
        action="store_const",
        const="shield",
        help="选择带模拟护盾的标准靶及其随包表",
    )
    cli.set_defaults(variant="default")
    mode = cli.add_mutually_exclusive_group()
    mode.add_argument("--merge", "--merge-only", action="store_true")
    mode.add_argument(
        "--only-extra-modes",
        action="store_true",
        help="只计算灵化循环、重击和重击震地场景",
    )
    mode.add_argument(
        "--backfill-missing-traits",
        action="store_true",
        help="复用常规场景文件的固定配装，只回填缺失的非单元素重配词条",
    )
    mode.add_argument(
        "--backfill-element-baselines",
        action="store_true",
        help="复用逐场景文件的固定配装，回填正单元素和复合元素的同套对照",
    )
    cli.add_argument("--in-dir", type=Path, action="extend", nargs="+")
    cli.add_argument("--out-builds", "--builds-out", type=Path)
    cli.add_argument("--out-traits", "--traits-out", type=Path)
    args = cli.parse_args()
    if args.workers < 1 or not 0 <= args.shard_index < args.shard_total:
        cli.error("workers 和 shard-total 须为正整数，shard-index 从 0 开始")
    if args.merge:
        if not args.in_dir or args.shard_total != 1 or args.reuse_ranged_dir is not None:
            cli.error("合并需要 --in-dir 并覆盖全部所选场景")
        builds_out, traits_out = {
            "default": (DEFAULT_BUILDS_OUT, DEFAULT_TRAITS_OUT),
            "overguard": (OVERGUARD_BUILDS_OUT, OVERGUARD_TRAITS_OUT),
            "shield": (SHIELD_BUILDS_OUT, SHIELD_TRAITS_OUT),
        }[args.variant]
        args.out_builds = args.out_builds or builds_out
        args.out_traits = args.out_traits or traits_out
        if args.out_builds.resolve() == args.out_traits.resolve():
            cli.error("两份输出须使用不同路径")
    elif args.out_dir is None or args.in_dir:
        cli.error("生成需要 --out-dir，--in-dir 仅用于合并")
    if (args.backfill_missing_traits or args.backfill_element_baselines) and (
        args.reuse_ranged_dir is not None
    ):
        cli.error("回填仅读取 --out-dir，不能同时指定 --reuse-ranged-dir")
    if args.reuse_ranged_dir is not None and not args.reuse_ranged_dir.is_dir():
        cli.error("旧远程场景目录不存在")
    # 在首次导入前固定哈希种子，并让 spawn 子进程继承单引擎工位。
    os.environ["WF_ANALYZER_SERIAL"] = "1"
    os.environ["WF_ENGINE_WORKERS"] = "1"
    if os.environ.get("PYTHONHASHSEED") != "0":
        os.environ["PYTHONHASHSEED"] = "0"
        os.execv(sys.executable, [sys.executable, str(Path(__file__).resolve()), *sys.argv[1:]])
    try:
        with tempfile.TemporaryDirectory(prefix="portable-precompute-") as stage:
            args.engine_dir = prepare_engine(
                args.engine_dir.resolve(), args.inputs_dir, Path(stage)
            )
            setup_engine(args.engine_dir, variant=args.variant)
            all_scenes = list_scenes(None, only_extra_modes=args.only_extra_modes)
            if args.backfill_missing_traits:
                all_scenes = [scene for scene in all_scenes if scene.mode_key not in EXTRA_MODES]
            if args.only_weapons:
                selected = {
                    scene.key
                    for scene in list_scenes(
                        args.only_weapons, only_extra_modes=args.only_extra_modes
                    )
                }
            else:
                selected = {scene.key for scene in all_scenes}
            if args.merge:
                merge(args, [scene for scene in all_scenes if scene.key in selected], all_scenes)
            else:
                run_shard(
                    args,
                    [
                        scene
                        for i, scene in enumerate(all_scenes)
                        if i % args.shard_total == args.shard_index and scene.key in selected
                    ],
                )
    except (OSError, ValueError, TypeError, KeyError, IndexError, AttributeError) as exc:
        cli.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()
