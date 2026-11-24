"""用归档输入复现旧版紫卡单词条模拟。"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from itertools import product
import math
from pathlib import Path
from queue import Empty, Queue
from statistics import mean, stdev
import subprocess
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import riven_rating  # noqa: E402

SCHEMA = 1
RUNS = 128
SEED = 47162621
CARD = "riven:p4-marginal"
SHAPE = {"rank": 8, "polarity": "madurai", "bonuses": [{}, {}, {}], "malus": {}}
HOLSTER_TRAIT = "reload_while_holstered"
HOLSTER_SECONDS = 2
# 单武器资料与冻结数据里的固有回填比例。进化表仅补尚未进入 indirect 的条目。
HOLSTER_INNATE = {
    **dict.fromkeys(
        (
            "afentis",
            "afentis_prime",
            "ferrox",
            "tenet_ferrox",
            "javlok",
            "scourge",
            "scourge_prime",
        ),
        0.33,
    ),
    "tenet_envoy": 0.10,
    "tenet_diplos": 0.10,
}
HOLSTER_EVOLUTIONS = {
    **dict.fromkeys(
        (
            "dera_evolved_autoloader",
            "dera_vandal_evolved_autoloader",
            "felarx_evolved_autoloader",
            "gammacor_evolved_autoloader",
            "synoid_gammacor_evolved_autoloader",
        ),
        0.50,
    ),
    "vasto_awakened_readiness": 0.20,
    "vasto_prime_awakened_readiness": 0.20,
}


def json_text(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    tmp.write_text(json_text(value) + "\n", encoding="utf-8")
    tmp.replace(path)


class Engine:
    """通过本地引擎的 JSON 行接口在每个计算进程内顺序复算。"""

    def __init__(self, binary: Path) -> None:
        self.binary = binary
        self.calls = 0

    def __enter__(self) -> Engine:
        self.process = subprocess.Popen(
            [self.binary], stdin=subprocess.PIPE, stdout=subprocess.PIPE, encoding="utf-8"
        )
        return self

    def __exit__(self, kind, *_trace) -> None:
        self.process.stdin.close()
        if kind is not None:
            self.process.terminate()
        self.process.stdout.close()
        code = self.process.wait()
        if kind is None and code:
            raise RuntimeError(f"伤害引擎退出，状态码 {code}")

    def call(self, path: str, body: dict) -> dict:
        self.calls += 1
        message = {"id": self.calls, "path": path, "body": body}
        self.process.stdin.write(json_text(message) + "\n")
        self.process.stdin.flush()
        line = self.process.stdout.readline()
        if not line:
            raise RuntimeError(f"伤害引擎提前退出 {path}")
        packet = json.loads(line)
        if packet.get("id") != self.calls:
            raise ValueError(f"回执标识不匹配 {path}")
        result = packet["response"]
        if result.get("ok") is False:
            raise ValueError(f"{path} {result.get('error')}")
        return result


def legal_traits(meta: dict, weapon: dict) -> list[tuple[str, dict]]:
    excluded = set(weapon["riven_excludes"])
    return [
        (sign, trait)
        for sign, trait in product(("bonus", "malus"), meta["riven_stats"][weapon["riven_class"]])
        if trait[sign] and trait["id"] not in excluded
    ]


def trial_card(fight: dict, spec: dict) -> dict:
    return {
        **fight,
        "mods": [*fight["mods"], CARD],
        "rivens": [{"id": CARD.removeprefix("riven:"), "spec": spec}],
    }


def simulate(engine: Engine, fight: dict) -> dict:
    raw = engine.call("/api/simulate", fight)
    scores = raw["score_runs"]
    if len(scores) != fight["runs"] or raw["duration"] != fight["duration"]:
        raise ValueError("模拟回执的轮数或战斗时长不匹配")
    return raw


def measure_scene(engine: Engine, fight: dict, traits: list[tuple[str, dict]], runs: int) -> dict:
    fight = {**fight, "runs": runs, "seed": SEED, "run_series": True}
    baseline = simulate(engine, fight)
    if baseline["forma"]["legal"] is not True:
        raise ValueError("无紫卡参考配装容量不合法")
    blank_spec = engine.call("/api/riven", {"weapon": fight["weapon"], **SHAPE})
    if blank_spec["illegal"] or blank_spec["stats"]:
        raise ValueError("空白对照含无效或非零词条")
    blank = simulate(engine, trial_card(fight, SHAPE))
    if blank["score_runs"] != baseline["score_runs"]:
        raise ValueError("空白对照改变了基准成绩")

    def kpm(raw: dict) -> float:
        return mean(raw["score_runs"]) * 60 / raw["duration"]

    rows = []
    for sign, trait in traits:
        line = {"id": trait["id"], "roll": 1.0}
        fields = {"bonuses": [line, {}, {}]} if sign == "bonus" else {"malus": line}
        spec = {**SHAPE, **fields}
        formula = engine.call("/api/riven", {"weapon": fight["weapon"], **spec})
        if formula["illegal"] or len(formula["stats"]) != 1:
            raise ValueError(f"词条池与数值接口不一致 {trait['id']} {sign} {formula['illegal']}")
        (stat,) = formula["stats"]
        if stat["id"] != trait["id"] or stat["bonus"] != (sign == "bonus"):
            raise ValueError(f"试验词条回执不匹配 {trait['id']} {sign}")

        # 用数值接口的 modeled 判定，避免把引擎未支持的词条当成无损。
        row = {
            "id": stat["id"],
            "sign": sign,
            "shown": stat["shown"],
            "unit": stat["unit"],
            "spliced": trait["spliced"],
            "unconfirmed": stat["id"] in formula["unconfirmed"],
            "modeled": stat["modeled"],
        }
        if stat["modeled"]:
            trial = simulate(engine, trial_card(fight, spec))
            row.update(paired_gain(trial, baseline), kpm=kpm(trial))
            row["same_series"] = trial["score_runs"] == baseline["score_runs"]
        rows.append(row)
    return {"baseline_kpm": kpm(baseline), "blank_identical": True, "traits": rows}


def rounded(value: float | None) -> float | None:
    return round(value, 2) if value is not None else None


def compact_kpm(value: float | None) -> float | None:
    if value is None or value >= 0.01:
        return rounded(value)
    return float(f"{value:.2g}")


def compact(row: dict) -> list:
    flags = (
        int(row["modeled"])
        | (int(row["unconfirmed"]) << 1)
        | (int(row["spliced"]) << 2)
        | (int(row.get("same_series", False)) << 3)
    )
    low, high = row.get("ci95") or (None, None)
    return [
        row["id"],
        round(row["shown"], 2 if row["unit"] == "x" else 1),
        row["unit"],
        flags,
        compact_kpm(row.get("kpm")),
        rounded(row.get("pct")),
        rounded(low),
        rounded(high),
    ]


def holster_scenes(doc: dict) -> set[tuple[str, str]]:
    return {
        (weapon, mode)
        for weapon, modes in doc["weapons"].items()
        for mode, scene in modes.items()
        if any(row[0] == HOLSTER_TRAIT for row in scene["positive"])
    }


def holster_time(profile: dict, rate: float, seconds: int) -> tuple[float, float]:
    """切回时的弹数、补至满仓剩余秒数。词条回填按完整秒累计整发。"""
    magazine = profile["magazine"]
    loaded = min(magazine, math.floor(magazine * rate * seconds + 1e-9))
    if battery := profile["battery"]:
        delay, charge_rate = battery["delay"], battery["rate"]
        full_at = delay + (magazine - loaded) / charge_rate if loaded < magazine else 0
        remaining = max(0, max(full_at, battery["animation"]) - seconds)
        return min(magazine, loaded + max(0, seconds - delay) * charge_rate), remaining
    if loaded == magazine:
        return loaded, 0
    if shells := profile["by_round_reload"]:
        start, per_round, end = shells
        return loaded, start + (magazine - loaded) * per_round + end
    return loaded, profile["reload_partial" if loaded else "reload_empty"]


def measure_holster(engine: Engine, fight: dict, seconds: int = HOLSTER_SECONDS) -> dict:
    weapon, mode = fight["weapon"], fight["mode"]
    if not fight.get("infinite_ammo") or fight.get("assume_max") is not False:
        raise ValueError(f"收枪对照需要备弹充足、未叠战斗触发的预设 {weapon}/{mode}")
    if any(mod.startswith("riven:") for mod in fight["mods"]):
        raise ValueError(f"参考配装含紫卡 {weapon}/{mode}")
    formula = engine.call(
        "/api/riven",
        {**SHAPE, "weapon": weapon, "bonuses": [{"id": HOLSTER_TRAIT, "roll": 1}, {}, {}]},
    )
    (stat,) = formula["stats"]
    if formula["illegal"] or (stat["id"], stat["bonus"], stat["unit"]) != (
        HOLSTER_TRAIT,
        True,
        "%",
    ):
        raise ValueError(f"收枪词条回执不一致 {weapon}/{mode}")
    profile = engine.call("/api/holster-ammo", fight)
    if (profile["weapon"], profile["mode"]) != (weapon, mode):
        raise ValueError(f"收枪场景回执不一致 {weapon}/{mode}")
    result = {"phase": profile["phase"], "shown": stat["shown"], "profile": profile}
    for inactive, kind in (
        (profile["sentinel"], "sentinel"),
        (profile["phase"] == "incarnon_charge", "incarnon_charge"),
        (profile["bow_nock"], "bow_nock"),
        # 冻结引擎以单发和固定冷却表示这段副射，不能从它推出真实电池的收枪回充。
        (profile["form_id"] == "tenet_plinx_charged", "battery_cooldown"),
        (profile["magazine"] <= 0, "no_magazine"),
    ):
        if inactive:
            return {**result, "kind": kind}
    base_rate = (
        profile["holstered_reload_rate"]
        + HOLSTER_INNATE.get(weapon, 0)
        + sum(HOLSTER_EVOLUTIONS.get(evo, 0) for evo in fight["evolutions"])
    )
    kind = (
        "battery"
        if profile["battery"]
        else "per_round"
        if profile["by_round_reload"]
        else "magazine"
    )
    loaded_without, without = holster_time(profile, base_rate, seconds)
    loaded_with, with_trait = holster_time(profile, base_rate + stat["value"], seconds)
    return {
        **result,
        "kind": kind,
        "innate_and_mod_rate": base_rate,
        "trait_rate": stat["value"],
        "loaded_without": loaded_without,
        "loaded_with": loaded_with,
        "without": without,
        "with": with_trait,
        "saved": without - with_trait,
    }


def apply_holster(doc: dict, report: dict) -> None:
    rows = report["scenes"]
    if set(rows) != {f"{w}/{m}" for w, m in holster_scenes(doc)}:
        raise ValueError("收枪装填结果与当前单词条场景不一致")
    for name, row in rows.items():
        weapon, mode = name.split("/", 1)
        compact = {key: row[key] for key in ("kind", "phase")}
        if "saved" in row:
            compact.update({key: rounded(row[key]) for key in ("without", "with", "saved")})
        doc["weapons"][weapon][mode]["holster_reload"] = compact
    doc["holster_reload_protocol"] = {key: data for key, data in report.items() if key != "scenes"}


def apply_pool_audit(doc: dict, audit: dict) -> None:
    """把已核验的正负词条资格写回原场景，KPM 和实值沿用原回执。"""
    if not (
        audit["reference_sha256"] == doc["reference_sha256"]
        and audit["data_revision"] == doc["engine"]["data_revision"]
    ):
        raise ValueError("词条资格核对与参考配装或引擎数据版本不一致")
    applied = doc.get("roll_pool_audit")
    if applied == audit:
        return
    if applied:
        raise ValueError("请从对应的原始模拟表更新已有资格核对")

    pending = doc["flags"]["unconfirmed"]
    ineligible = riven_rating._TRAIT_INELIGIBLE
    updates = []
    keys = set()
    for weapon, mode, direction, stat, verdict in audit["decisions"]:
        scene = doc["weapons"][weapon][mode]
        (row,) = (line for line in scene[direction] if line[0] == stat)
        key = (weapon, mode, direction, stat)
        if (
            key in keys
            or direction not in {"positive", "negative"}
            or stat not in {"impact", "puncture", "slash"}
            or row[3] & (pending | ineligible) != pending
            or verdict not in {"rollable", "no_riven"}
        ):
            raise ValueError(f"词条资格回写与原场景不一致 {key}")
        keys.add(key)
        updates.append((row, verdict))
    counts = Counter(verdict for _, verdict in updates)
    if doc["counts"]["unconfirmed"] != audit["reviewed"]:
        raise ValueError("词条资格核对覆盖数与原模拟表不一致")
    for row, verdict in updates:
        row[3] &= ~pending
        if verdict == "no_riven":
            row[3] |= ineligible
    doc["flags"]["ineligible"] = ineligible
    doc["counts"]["unconfirmed"] -= len(updates)
    doc["counts"]["ineligible"] = counts["no_riven"]
    doc["roll_pool_audit"] = audit


def reference_scenes(reference):
    return {
        f"{wid}/{mode}": {
            **reference["battle_defaults"],
            **row["fight"],
            "weapon": wid,
            "mode": mode,
        }
        for wid, weapon in reference["weapons"].items()
        for mode, row in weapon["modes"].items()
    }


def paired_gain(test, base):
    first, second = mean(test["score_runs"]), mean(base["score_runs"])
    if second <= 0:
        return {"pct": None, "ci95": None}
    ratio = first / second
    pct = 100 * (ratio - 1)
    series = test["score_runs"]
    paired = stdev(x - ratio * y for x, y in zip(series, base["score_runs"], strict=True))
    radius = 1.96 * 100 * paired / (math.sqrt(len(series)) * second)
    return {"pct": pct, "ci95": (pct - radius, pct + radius)}


def build_holster(args, parser, binary: Path, reference: dict) -> None:
    doc = json.loads((args.existing or args.output).read_bytes())
    reference_hash = digest(args.reference.read_bytes())
    if doc["reference_sha256"] != reference_hash:
        raise ValueError("单词条数据与参考配装版本不一致")
    fights = reference_scenes(reference)
    scenes = holster_scenes(doc)
    selected = {f"{weapon}/{mode}" for weapon, mode in scenes}
    if args.scene:
        if missing := set(args.scene) - selected:
            parser.error(f"收枪词条场景未找到 {sorted(missing)}")
        selected = set(args.scene)
    results = {}
    with Engine(binary) as engine:
        health = engine.call("/healthz", {})
        if health.get("data_revision") != reference["data_revision"]:
            raise ValueError("引擎数据版本和参考配装版本不一致")
        for name in sorted(selected):
            weapon, mode = name.split("/", 1)
            row = measure_holster(engine, fights[name])
            (combat_row,) = (
                r for r in doc["weapons"][weapon][mode]["positive"] if r[0] == HOLSTER_TRAIT
            )
            if round(row["shown"], 1) != combat_row[1]:
                raise ValueError(f"词条实值与 P4 对照不一致 {name}")
            results[name] = row
    report = {
        "seconds": HOLSTER_SECONDS,
        "metric": "seconds_after_return_until_full",
        "start": "empty_regular_magazine_with_reserve",
        "rounding": "whole_rounds_after_elapsed_full_seconds",
        "combat_buffs": "untriggered",
        "reference_sha256": reference_hash,
        "engine_binary_sha256": digest(binary.read_bytes()),
        "engine_data_revision": health["data_revision"],
        "counts": dict(Counter(row["kind"] for row in results.values())),
        "scenes": results,
    }
    if args.scene:
        write_json(args.output, report)
    else:
        apply_holster(doc, report)
        write_json(args.output, doc)
        if args.report:
            write_json(args.report, report)
    print(f"收枪装填 {len(results)} 项，收枪 {HOLSTER_SECONDS} 秒，{report['counts']}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", type=Path, required=True)
    parser.add_argument("--cache", type=Path)
    parser.add_argument("--reference", type=Path, required=True, help="归档的参考配装 JSON")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--existing", type=Path, help="收枪装填复算所用的归档单词条 JSON")
    parser.add_argument("--runs", type=int, choices=(64, 128, 256, 512, 1024), default=RUNS)
    parser.add_argument("--lanes", type=int, default=5)
    parser.add_argument("--scene", action="append", metavar="WEAPON/MODE")
    parser.add_argument("--holster-reload", action="store_true", help="用固定切枪节奏复算收枪装填")
    parser.add_argument("--report", type=Path, help="另存收枪装填原始回执")
    args = parser.parse_args()
    if (
        args.scene
        and args.holster_reload
        and (not args.existing or args.output.resolve() == args.existing.resolve())
    ):
        parser.error("收枪装填小样须用 --existing 指定归档文件，另存输出")
    if args.report and args.report.resolve() == args.output.resolve():
        parser.error("原始回执请另指定文件")
    if not args.holster_reload and not args.cache:
        parser.error("KPM 批次需要 --cache")
    if args.lanes < 1:
        parser.error("--lanes 至少为 1")
    binary = args.engine.resolve(strict=True)
    reference = json.loads(args.reference.read_bytes())
    if args.holster_reload:
        build_holster(args, parser, binary, reference)
        return
    with Engine(binary) as engine:
        health = engine.call("/healthz", {})
        meta = engine.call("/api/meta", {})
    if health.get("data_revision") != reference["data_revision"]:
        raise ValueError("引擎数据版本和参考配装版本不一致")

    weapons = {weapon["id"]: weapon for weapon in meta["weapons"]}
    scenes = reference_scenes(reference)
    if args.scene:
        if missing := set(args.scene) - scenes.keys():
            parser.error(f"参考配装未找到 {sorted(missing)}")
        scenes = {name: scenes[name] for name in sorted(set(args.scene))}

    protocol = {
        "runs": args.runs,
        "seed": SEED,
        "rank": SHAPE["rank"],
        "bonus_slots": len(SHAPE["bonuses"]),
        "malus_slots": 1,
        "roll": 1,
        "treatment": "one_trait_appended_after_reference_mods",
        "denominator": "no_riven_reference_mean_kpm",
    }
    provenance = {
        "schema": SCHEMA,
        "reference_sha256": digest(args.reference.read_bytes()),
        "engine_binary_sha256": digest(binary.read_bytes()),
        "engine": health,
        "protocol": protocol,
    }
    cache_version = digest(
        json_text({**provenance, "runner_sha256": digest(Path(__file__).read_bytes())}).encode()
    )
    cache = args.cache / cache_version
    cache.mkdir(parents=True, exist_ok=True)
    jobs: Queue[str] = Queue()

    for name, fight in scenes.items():
        weapon = weapons[fight["weapon"]]
        if weapon["no_riven"] or not weapon["riven_class"]:
            raise ValueError(f"参考武器缺少紫卡词条池 {name}")
        if not (cache / f"{name.replace('/', '__')}.json").exists():
            jobs.put(name)

    print(
        f"场景 {len(scenes)}，待跑 {jobs.qsize()}，每词条 {args.runs} 轮，缓存 {cache}", flush=True
    )

    def run_lane() -> list[tuple[str, str]]:
        errors = []
        with Engine(binary) as worker:
            while True:
                try:
                    name = jobs.get_nowait()
                except Empty:
                    return errors
                fight = scenes[name]
                try:
                    traits = legal_traits(meta, weapons[fight["weapon"]])
                    row = measure_scene(worker, fight, traits, args.runs)
                    write_json(cache / f"{name.replace('/', '__')}.json", row)
                except (ValueError, KeyError) as error:
                    errors.append((name, str(error)))

    if not jobs.empty():
        with ThreadPoolExecutor(max_workers=args.lanes) as pool:
            errors = [
                error
                for lane in pool.map(lambda _: run_lane(), range(args.lanes))
                for error in lane
            ]
        if errors:
            for name, message in errors:
                print(f"失败 {name} {message}", file=sys.stderr)
            raise SystemExit(1)

    result: dict[str, dict] = {}
    counts: Counter = Counter()
    for name, fight in scenes.items():
        wid, mode = name.split("/", 1)
        file = cache / f"{name.replace('/', '__')}.json"
        row = json.loads(file.read_text(encoding="utf-8"))
        expected = legal_traits(meta, weapons[wid])
        wanted = {(sign, trait["id"]) for sign, trait in expected}
        actual = [(trait["sign"], trait["id"]) for trait in row["traits"]]
        if (
            len(actual) != len(wanted)
            or set(actual) != wanted
            or row["blank_identical"] is not True
        ):
            raise ValueError(f"覆盖或空白对照校验失败 {name}")
        data = {
            "baseline_kpm": compact_kpm(row["baseline_kpm"]),
            "positive": [compact(trait) for trait in row["traits"] if trait["sign"] == "bonus"],
            "negative": [compact(trait) for trait in row["traits"] if trait["sign"] == "malus"],
        }
        for trait in row["traits"]:
            counts["entries"] += 1
            counts["modeled" if trait["modeled"] else "unmodeled"] += 1
            counts[trait["sign"]] += 1
            counts["unconfirmed"] += int(trait["unconfirmed"])
            counts["missing_denominator"] += int(trait["modeled"] and trait["pct"] is None)
        result.setdefault(wid, {})[mode] = data

    doc = {
        **provenance,
        "columns": [
            "stat",
            "shown",
            "unit",
            "flags",
            "kpm",
            "kpm_change_pct",
            "ci95_low",
            "ci95_high",
        ],
        "flags": {"modeled": 1, "unconfirmed": 2, "spliced": 4, "same_score_series": 8},
        "counts": dict(counts),
        "weapons": result,
    }
    if not args.scene and args.output.exists():
        existing = json.loads(args.output.read_bytes())
        if (
            (audit := existing.get("roll_pool_audit"))
            and audit["reference_sha256"] == doc["reference_sha256"]
            and audit["data_revision"] == doc["engine"]["data_revision"]
        ):
            apply_pool_audit(doc, audit)
        if (
            (protocol := existing.get("holster_reload_protocol"))
            and protocol["reference_sha256"] == doc["reference_sha256"]
            and holster_scenes(existing) == holster_scenes(doc)
        ):
            prior = {
                f"{w}/{m}": existing["weapons"][w][m]["holster_reload"]
                for w, m in holster_scenes(doc)
            }
            apply_holster(doc, {**protocol, "scenes": prior})
    write_json(args.output, doc)
    print(f"武器 {len(result)}，场景 {len(scenes)}，{dict(counts)}", flush=True)


if __name__ == "__main__":
    main()
