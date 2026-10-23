# -*- coding: utf-8 -*-
"""生成紫卡词条池证据表（`core/data/de/riven_pools.json`）。

用法：
    python scripts/build_riven_pools.py [wfsim 仓库根] [输出路径]
    python scripts/build_riven_pools.py --check      # 只校验已生成文件是否可复现

数据源（wfsim，`github.com/magenie33/wfsim`，本机在 ``~/tmp/wfsim``）：
  · ``data/rivens/{rifle,shotgun,pistol,archgun,melee}.yaml`` —— 各职业池的 stat 清单
    （DE Public Export 直出）与 **正负限制** ``bonus`` / ``malus``；
  · ``data/rivens/exceptions.yaml`` —— 6 个家族的人工例外（``rolls`` / ``never``）；
  · ``data/rivens/physical.yaml`` —— 240 个家族的**物理词条**证据（``rolls`` / ``never``）；
  · ``data/rivens/pools.yaml`` —— 26 个家族的抽样计数（``rollable`` / ``never`` / ``unclear``）；
  · ``data/weapons/**.yaml`` —— 逐武器的 ``riven_family`` / ``ammo_max``（判定「无弹药池」）。

产出结构（键一律换成**插件标准词条 id**，见 `core/parser.py::RIVEN_STAT_ZH`）::

    {"class_pools": {"<职业>": {"<词条 id>": {"bonus": bool, "malus": bool}}},
     "families": {"<我方家族码>": {"wf": "<wfsim 家族名>", "no_ammo_pool": bool,
                                  "never": [...], "rolls": [...],
                                  "survey": {"n": int, "surveyed": str,
                                             "rollable": [...], "never": [...],
                                             "unclear": [...]}}}}

口径（与 wfsim `engine/src/build/rivens/pools.rs` 对齐，差异见 `_rule`）：
  · wfsim 的**计算路径**只读 ``exceptions.yaml`` + ``physical.yaml``，``pools.yaml``
    （抽样计数）刻意不进计算 —— 怕一次坏抓取把池全改掉。**本表按维护者 2026-10-23 指令
    把 ``pools.yaml`` 也并进来**（作为逐家族证据的一档），并把该差异写进 `_rule`。
  · ``never`` = 该家族刷不出；``rolls``（wfsim 语义）= 规则说刷不出但**实际能出**；
    ``rollable``（pools.yaml 语义）= 出现率与「会出的词条」相当。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    import yaml
except ImportError:  # noqa: BLE001
    print("本脚本需要 PyYAML：pip install pyyaml（插件运行时并不依赖它）", file=sys.stderr)
    raise SystemExit(2) from None

ROOT = Path(__file__).resolve().parent.parent
OUT_DEFAULT = ROOT / "core" / "data" / "de" / "riven_pools.json"
FAMILIES = ROOT / "core" / "data" / "de" / "riven_families.json"
SURVEY = ROOT / "core" / "data" / "de" / "riven_pools_survey.json"  # 本地抽样覆盖层
WFSIM_DEFAULT = Path.home() / "tmp" / "wfsim"

# wfsim stat slug → 插件标准词条 id。逐一核对过（52 个 slug ↔ 51 个 id：
# 我方 `melee_damage` 同时覆盖 wfsim 的 `damage`（非近战）与 `melee_damage`（近战））。
SLUG_MAP = {
    "damage": "melee_damage",
    "melee_damage": "melee_damage",
    "critical_chance": "crit_chance",
    "critical_damage": "crit_damage",
    "multishot": "multishot",
    "fire_rate": "fire_rate",
    "attack_speed": "attack_speed",
    "status_chance": "status_chance",
    "status_duration": "status_duration",
    "range": "range",
    "initial_combo": "initial_combo",
    "combo_duration": "combo_duration",
    "heavy_attack_efficiency": "heavy_attack_efficiency",
    "additional_combo_count_chance": "extra_combo_count",
    "chance_to_gain_combo_count": "combo_gain_chance",
    "finisher_damage": "finisher_damage",
    "critical_chance_for_slide_attack": "slide_crit",
    "slash": "slash_damage",
    "impact": "impact_damage",
    "puncture": "puncture_damage",
    "heat": "heat_damage",
    "cold": "cold_damage",
    "toxin": "toxin_damage",
    "electricity": "electric_damage",
    "damage_to_grineer": "damage_vs_grineer",
    "damage_to_corpus": "damage_vs_corpus",
    "damage_to_infested": "damage_vs_infested",
    "damage_to_orokin": "damage_vs_orokin",
    "damage_to_scaldra": "damage_vs_scaldra",
    "damage_to_techrot": "damage_vs_techrot",
    "magazine_capacity": "magazine_capacity",
    "ammo_maximum": "ammo_max",
    "projectile_speed": "projectile_speed",
    "punch_through": "punch_through",
    "reload_speed": "reload_speed",
    "reload_while_holstered": "reload_holstered",
    "weapon_recoil": "recoil",
    "zoom": "zoom",
    "gas": "gas_damage",
    "corrosive": "corrosive_damage",
    "viral": "viral_damage",
    "radiation": "radiation_damage",
    "blast": "blast_damage",
    "magnetic": "magnetic_damage",
    "weak_point_damage": "weakpoint_damage",
    "weak_point_critical_chance": "weakpoint_crit_chance",
    "status_damage": "status_damage",
    "ammo_efficiency": "ammo_efficiency",
    "melee_damage_on_heavy_attack": "melee_heavy_attack_damage",
    "heavy_attack_wind_up_speed": "melee_heavy_attack_charge",
    "parry_angle": "melee_parry_angle",
    "slam_attack_damage": "melee_slam_damage",
}

# 物理三词条（wfsim `PHYSICAL`）：只由证据决定，不由 25% 份额规则直接删
PHYSICAL_SLUGS = ("impact", "puncture", "slash")

# ---------------------------------------------------------------------------
# 正负限制的**人工订正**：wfsim 的职业池文件由 DE Public Export 生成，个别条目
# 的 `malus` 标记与官方行为不符；这里按**独立权威源**订正，逐条附证据。
# 订正只作用于 `class_pools` 的 bonus/malus（可用性仍按池文件）。
# ---------------------------------------------------------------------------
# 2026-10-23：`channeling_damage`（= 我方 `initial_combo`）在 WM 的
# `/v2/riven/attributes` 里标 `positiveOnly: true`（实测拉全量 50 条 slug 得到），
# 而 wfsim 的 `melee.yaml` 里 `initial_combo` **没有** `malus: false` ⇒ 两源冲突。
# 取 WM + 用户口径（维护者 2026-10-23 明确「初始连击只出正面」）⇒ 订正为只出正面。
_POOL_FLAG_FIXES: dict[str, dict[str, dict[str, bool]]] = {
    "melee": {"initial_combo": {"bonus": True, "malus": False}},
}


def _norm(name: str) -> str:
    """武器名归一化（比对 wfsim `name` 用）：小写 + 去非字母数字。"""
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def _load_yaml(p: Path) -> dict:
    with p.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _stat_scalars(weapons_dir: Path) -> dict[str, dict]:
    """逐武器抽扁平标量（``id``/``name``/``riven_family``/``class``/``ammo_max``）。

    只抽顶层标量、不解析嵌套 attacks —— 段级数据用插件自己的
    ``weapons_stats.json``（同一上游、已对拍），这里只要 YAML 里有、插件数据没有的
    那几项（家族归属与弹药池）。
    """
    out: dict[str, dict] = {}
    for p in sorted(weapons_dir.rglob("*.yaml")):
        txt = p.read_text(encoding="utf-8", errors="replace")
        rec: dict = {}
        for key in ("id", "name", "riven_family", "class", "ammo_max"):
            m = re.search(rf"^{key}:\s*(.*)$", txt, re.M)
            if not m:
                continue
            v = m.group(1).split("#")[0].strip()
            if v in ("", "~", "null"):
                rec[key] = None
            elif re.fullmatch(r"-?\d+", v):
                rec[key] = int(v)
            else:
                rec[key] = v.strip('"').strip("'")
        wid = rec.get("id") or p.stem
        out.setdefault(wid, rec)
    return out


def _class_pools(rivens_dir: Path) -> dict[str, dict[str, dict[str, bool]]]:
    """各职业池的 stat 清单 + 正负限制 + 是否融合词条（``bonus`` 缺省 = True）。

    ``spliced`` = Update 44「Riven Splicer」引入的融合词条（**同一张卡最多 1 项**），
    卡面按维护者 2026-10-23 指令单列一段。
    """
    pools: dict[str, dict[str, dict[str, bool]]] = {}
    for cls in ("rifle", "shotgun", "pistol", "archgun", "melee"):
        p = rivens_dir / f"{cls}.yaml"
        if not p.exists():
            continue
        col: dict[str, dict[str, bool]] = {}
        for s in _load_yaml(p).get("stats") or []:
            sid = SLUG_MAP.get(s["id"])
            if not sid:
                continue
            # 同一 slug 映射到同一 id 时（damage/melee_damage）取并集：任一可为正/负即可
            cur = col.setdefault(sid, {"bonus": False, "malus": False, "spliced": False})
            cur["bonus"] = cur["bonus"] or (s.get("bonus") is not False)
            cur["malus"] = cur["malus"] or (s.get("malus") is not False)
            cur["spliced"] = cur["spliced"] or bool(s.get("spliced"))
        for sid, fix in (_POOL_FLAG_FIXES.get(cls) or {}).items():
            if sid in col:
                col[sid].update(fix)
        pools[cls] = col
    return pools


def survey_doc_date() -> str:
    """覆盖层的抽样日期（缺失 ⇒ 空串）。"""
    try:
        return json.loads(SURVEY.read_text(encoding="utf-8")).get("_surveyed") or ""
    except Exception:  # noqa: BLE001
        return ""


def _load_survey(path: Path) -> dict:
    """本地抽样覆盖层（`scripts/survey_riven_pools.py` 的产物）；缺失 ⇒ 空表。

    键是 **wfsim 家族名**（抽样脚本按母武器 slug 查），值 = 计数与三档判据。
    """
    if not path.exists():
        return {}
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    return doc.get("families") or {}


def _family_evidence(rivens_dir: Path, wf_name: str) -> dict:
    """一个 wfsim 家族名下三份证据文件的合并（原始 slug → 我方 id）。"""
    out: dict = {"never": [], "rolls": [], "survey": None}
    for fname, roll_key in (("exceptions.yaml", "rolls"), ("physical.yaml", "rolls")):
        p = rivens_dir / fname
        if not p.exists():
            continue
        for fam in _load_yaml(p).get("families") or []:
            if fam.get("family") != wf_name:
                continue
            for ent in fam.get(roll_key) or []:
                sid = SLUG_MAP.get(ent.get("stat") or "")
                if sid and sid not in out["rolls"]:
                    out["rolls"].append(sid)
            for ent in fam.get("never") or []:
                sid = SLUG_MAP.get(ent.get("stat") or "")
                if sid and sid not in out["never"]:
                    out["never"].append(sid)
    p = rivens_dir / "pools.yaml"
    if p.exists():
        doc = _load_yaml(p)
        for fam in doc.get("families") or []:
            if fam.get("family") != wf_name:
                continue
            conv = lambda lst: [  # noqa: E731
                sid for sid in (SLUG_MAP.get(x) for x in lst or []) if sid
            ]
            out["survey"] = {
                "n": fam.get("n") or 0,
                "surveyed": doc.get("surveyed") or "",
                "rollable": conv(fam.get("rollable")),
                "never": conv(fam.get("never")),
                "unclear": conv(fam.get("unclear")),
            }
    if not out["never"] and not out["rolls"] and out["survey"] is None:
        return {}
    return out


def build(wfsim: Path, commit: str = "") -> dict:
    rivens_dir = wfsim / "data" / "rivens"
    scalars = _stat_scalars(wfsim / "data" / "weapons")
    fam_doc = json.loads(FAMILIES.read_text(encoding="utf-8"))
    by_name = fam_doc.get("by_name") or {}
    # 英文名归一化索引 → 我方家族根（DE 官方 parentName 谱系的根键）
    root_index: dict[str, str] = {}
    for n, root in by_name.items():
        root_index.setdefault(_norm(n), root)

    def _root_of(name: str) -> str:
        """家族名 → 我方家族根，**与运行期同一套判据**（`core/matching.family_key`）。

        ★ 必须与运行期一致，否则键对不上、抽样结果静默丢失：新武器（2026 的
        Aksondol / Nunchasa 等）不在 `by_name` 表里，`family_key` 会回落到
        「英文主干」，只查表会拼出 `wf:<名>` 这种运行期永远查不到的键。
        """
        try:
            sys.path.insert(0, str(ROOT))
            from core import matching

            k = matching.family_key(name)
            if k:
                return k
        except Exception:  # noqa: BLE001 - 包导入失败时退回查表
            pass
        return root_index.get(_norm(name), "")

    # ★ 按 **wfsim 自己的家族分组**（`riven_family` 字段就是它的家族定义），
    #   而不是我方 `families` 的成员表 —— 我方 `families` 只收**多成员**家族，
    #   单成员家族（Atomos / Ocucor / Phenmor 这类，恰是用户最常问的）不在其中，
    #   只按成员表遍历会整批丢掉它们的证据与属性。
    groups: dict[str, list[dict]] = {}
    for rec in scalars.values():
        f = rec.get("riven_family")
        if f:
            groups.setdefault(f, []).append(rec)

    # wfsim 家族名 → 我方家族根：先用同名条目查（家族名就是母武器英文名），
    # 查不到再用该家族任一成员的英文名反查。
    def _our_root(wf_name: str, recs: list[dict]) -> str:
        for cand in [wf_name] + [r.get("name") or "" for r in recs]:
            hit = _root_of(cand)
            if hit:
                return hit
        return ""

    out_fams: dict[str, dict] = {}
    no_root: list[str] = []
    for wf_name, recs in sorted(groups.items()):
        root = _our_root(wf_name, recs)
        key = root or f"wf:{wf_name}"
        if not root:
            no_root.append(wf_name)
        entry: dict = {"wf": wf_name}
        # 「无弹药池」= 家族内**每个**成员都没有 ammo_max（wfsim 口径 all(is_none)）
        if any(r.get("ammo_max") is not None for r in recs):
            entry["no_ammo_pool"] = False
        else:
            entry["no_ammo_pool"] = True
        entry.update(_family_evidence(rivens_dir, wf_name))
        out_fams[key] = entry

    # 证据文件里出现过、但武器表里没有对应 `riven_family` 的家族名（仍要收进来）
    for fname in ("exceptions.yaml", "physical.yaml", "pools.yaml"):
        p = rivens_dir / fname
        if not p.exists():
            continue
        for fam in _load_yaml(p).get("families") or []:
            wf_name = fam.get("family")
            if not wf_name:
                continue
            key = _root_of(wf_name) or f"wf:{wf_name}"
            if key in out_fams:
                continue
            if not _root_of(wf_name):
                no_root.append(wf_name)
            entry = {"wf": wf_name}
            entry.update(_family_evidence(rivens_dir, wf_name))
            out_fams[key] = entry

    # —— 本地抽样覆盖层（`scripts/survey_riven_pools.py`）——
    # ★ 同一家族两份抽样都在时**以本地为准**：wfsim 的 pools.yaml 是 2026-08-08 的
    #   26 族快照，本地抽样更新更广；wfsim 那份保留在 `survey_wfsim` 里供对拍。
    survey = _load_survey(SURVEY)

    def _as_survey(local: dict) -> dict:
        return {
            "n": local.get("n") or 0,
            "surveyed": survey_doc_date(),
            "source": "local",
            "rollable": list(local.get("rollable") or []),
            "never": list(local.get("never") or []),
            "unclear": list(local.get("unclear") or []),
        }

    for key, entry in out_fams.items():
        wf_name = entry.get("wf")
        local = survey.get(wf_name) if wf_name else None
        if not local:
            continue
        if entry.get("survey"):
            entry["survey_wfsim"] = entry["survey"]
        entry["survey"] = _as_survey(local)

    # ★ 抽样里 wfsim 武器数据**没有**的家族（较新 / 冷门武器：Praedos、Stropha、
    #   Paracesis、Aksondol…）—— 上面那轮遍历碰不到它们，只按 wfsim 建表会把这批
    #   抽样结果整个丢掉（实测 24 个里只并进 11 个）。这里按我方家族根补建条目。
    for wf_name, local in sorted(survey.items()):
        key = _root_of(wf_name) or f"wf:{wf_name}"
        if key in out_fams:
            continue
        if not _root_of(wf_name):
            no_root.append(wf_name)
        out_fams[key] = {"wf": wf_name, "survey": _as_survey(local)}

    miss = sorted(set(groups) - {v.get("wf") for v in out_fams.values()})
    return {
        "_source": "github.com/magenie33/wfsim data/rivens/{rifle,shotgun,pistol,archgun,melee,"
        "exceptions,physical,pools}.yaml + data/weapons/**.yaml",
        # 上游版本：优先读 wfsim 仓的 HEAD；本机只同步了 data/（无 .git）时用 --commit 显式给
        "_wfsim_commit": commit or _git_head(wfsim),
        "_generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "_rule": (
            "词条池判据（与 wfsim engine/src/build/rivens/pools.rs 对齐）："
            "① 职业池 = 该职业的 stat 清单（本表 class_pools 的 bonus/malus 即正负限制，"
            "可用性另见 core/riven_analysis._BASE 的 None 列）；"
            "② 逐武器规则：守护武器不出变焦/后坐力、无弹药池不出弹药上限、"
            "无飞行且无伤害衰减不出弹道速度；物理三项按 25% 份额只标「未确认」不删；"
            "③ 家族证据最高优先：exceptions.yaml + physical.yaml 的 never/rolls 合并；"
            "★ 与 wfsim 的差异：wfsim 计算路径刻意不读 pools.yaml（怕坏抓取改池），"
            "本表按维护者 2026-10-23 指令把 pools.yaml 的 never/rollable 也并入逐家族证据。"
            "★ 家族键 = 我方家族根（core/matching.family_key 的取值域，可由 by_name 反查）；"
            "查不到我方家族根时退化为 `wf:<wfsim 家族名>`。"
        ),
        "_counts": {
            "class_pools": {k: len(v) for k, v in _class_pools(rivens_dir).items()},
            "families": len(out_fams),
            "with_evidence": sum(1 for v in out_fams.values() if v.get("never") or v.get("rolls")),
            "with_survey": sum(1 for v in out_fams.values() if v.get("survey")),
            "no_ammo_pool": sum(1 for v in out_fams.values() if v.get("no_ammo_pool")),
            "wf_families_without_our_root": len(no_root),
            "wf_families_unmatched": len(miss),
        },
        "class_pools": _class_pools(rivens_dir),
        "families": out_fams,
    }


def _git_head(repo: Path) -> str:
    try:
        return subprocess.run(  # noqa: S603
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except Exception:  # noqa: BLE001
        return ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("wfsim", nargs="?", default=str(WFSIM_DEFAULT))
    ap.add_argument("out", nargs="?", default=str(OUT_DEFAULT))
    ap.add_argument("--check", action="store_true", help="只校验已生成文件可复现（不写盘）")
    ap.add_argument(
        "--commit",
        default="",
        help="wfsim 上游 commit（本机只同步了 data/、没有 .git 时用它记版本）",
    )
    args = ap.parse_args()

    data = build(Path(args.wfsim), args.commit)
    body = json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True) + "\n"
    out = Path(args.out)
    if args.check:
        old = out.read_text(encoding="utf-8") if out.exists() else ""
        # _generated_at 每次都变，不参与比对
        strip = lambda s: re.sub(r'"_generated_at": "[^"]*",\n', "", s)  # noqa: E731
        ok = strip(old) == strip(body)
        print(f"[check] {'可复现 ✔' if ok else '不可复现 ✘（请重跑生成）'}")
        return 0 if ok else 1
    out.write_text(body, encoding="utf-8")
    print(
        f"写出 {out}（{len(body)} 字节，sha256 {hashlib.sha256(body.encode()).hexdigest()[:12]}）"
    )
    for k, v in data["_counts"].items():
        print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
