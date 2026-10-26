# -*- coding: utf-8 -*-
"""生成紫卡词条池表（`core/data/de/riven_pools.json`）—— 只用 DE 官方导出 + 我方抽样。

★ 2026-10-25 重写（维护者定：wfsim 派生数据改为从游戏数据自己生成；Astra 评估许可 L1）。
旧版读 wfsim（AGPL-3.0-or-later）的 `data/rivens/*.yaml` 与 `data/weapons/**.yaml`，已全部移除。

用法::

    python scripts/build_riven_pools.py --official <目录> --pep <目录> [--out 路径]
    python scripts/build_riven_pools.py --official <目录> --pep <目录> --check   # 只校验可复现

输入（都是**静态文件**，本脚本不联网）：

  · ``--official``：DE 官方 Public Export（`content.warframe.com/PublicExport/Manifest/`，
    清单见 `origin.warframe.com/PublicExport/index_en.txt.lzma`）的 ``ExportUpgrades_en.json`` 与
    ``ExportWeapons_en.json``。提供**现行**的紫卡词条清单（含 Update 44 融合词条、44.0.3 霰弹枪变焦）、
    武器英文名、类别（守护武器 = ``SentinelWeapons``）、唯一名（魔典 = ``/Grimoire/``）。
  · ``--pep``：calamity-inc/warframe-public-export-plus（MIT）的 ``ExportUpgrades.json`` 与
    ``ExportWeapons.json``。官方导出**没有**每个词条的「能否正 / 负」与限制条件
    （``DamageTypes`` / ``NotSentinel`` / ``NotGrimoire``），也没有武器兼容标签（``PROJECTILE``）——
    这些取自 PEP（它从游戏文件补齐）。PEP 的紫卡模板停在融合器之前：原有 36 种词条用它，
    官方新增的词条按下面的规则补。
  · ``core/data/de/riven_pools_survey.json``：我方 WM 挂单抽样（`scripts/survey_riven_pools.py`）。
  · ``core/data/de/riven_families.json``：家族表（DE parentName 谱系，我方生成）。

产出结构（键为插件标准词条 id，见 `core/parser.py::RIVEN_STAT_ZH`）::

    {"class_pools": {"<职业>": {"<词条 id>": {"bonus": bool, "malus": bool, "spliced": bool,
                                           "buff_restr": [...], "curse_restr": [...]}}},
     "families": {"<我方家族根>": {"sentinel": true?, "grimoire": true?, "projectile": bool?,
                                  "survey": {"n", "surveyed", "rollable", "never", "unclear"}?}}}

规则：
  · 融合词条（`riven_analysis.SPLICE_RECIPES` 的结果，英文 wiki「Riven Splicer」表）：只出正面、无限制
    （wiki：「spliced stats will always be positive」）。
  · 官方有、PEP 该职业没有的非融合词条（目前只有 44.0.3 起的霰弹枪变焦）：沿用 PEP 里其它枪械
    同一词条的正负与限制。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OUT_DEFAULT = ROOT / "core" / "data" / "de" / "riven_pools.json"
FAMILIES = ROOT / "core" / "data" / "de" / "riven_families.json"
SURVEY = ROOT / "core" / "data" / "de" / "riven_pools_survey.json"

# 官方紫卡模板 → 我方职业（组合枪 / Zaw 模板不单列职业池：运行期按主体职业取）
TEMPLATES = {
    "rifle": "LotusRifleRandomModRare",
    "shotgun": "LotusShotgunRandomModRare",
    "pistol": "LotusPistolRandomModRare",
    "archgun": "LotusArchgunRandomModRare",
    "melee": "PlayerMeleeWeaponRandomModRare",
}

# DE 词条 tag → 插件标准词条 id（近战的 WeaponFireRateMod 是攻速）
TAG_TO_ID = {
    "ComboDurationMod": "combo_duration",
    "SlideAttackCritChanceMod": "slide_crit",
    "WeaponAmmoEfficiency": "ammo_efficiency",
    "WeaponAmmoMaxMod": "ammo_max",
    "WeaponArmorPiercingDamageMod": "puncture_damage",
    "WeaponBlastDamageMod": "blast_damage",
    "WeaponClipMaxMod": "magazine_capacity",
    "WeaponCorrosiveDamageMod": "corrosive_damage",
    "WeaponCritChanceMod": "crit_chance",
    "WeaponCritDamageMod": "crit_damage",
    "WeaponDamageAmountMod": "melee_damage",
    "WeaponElectricityDamageMod": "electric_damage",
    "WeaponFactionDamageCorpus": "damage_vs_corpus",
    "WeaponFactionDamageGrineer": "damage_vs_grineer",
    "WeaponFactionDamageInfested": "damage_vs_infested",
    "WeaponFactionDamageOrokin": "damage_vs_orokin",
    "WeaponFactionDamageScaldra": "damage_vs_scaldra",
    "WeaponFactionDamageTechrot": "damage_vs_techrot",
    "WeaponFireDamageMod": "heat_damage",
    "WeaponFireIterationsMod": "multishot",
    "WeaponFireRateMod": "fire_rate",
    "WeaponFreezeDamageMod": "cold_damage",
    "WeaponGasDamageMod": "gas_damage",
    "WeaponImpactDamageMod": "impact_damage",
    "WeaponMagazineReloadHolstered": "reload_holstered",
    "WeaponMagneticDamageMod": "magnetic_damage",
    "WeaponMeleeComboBonusOnHitMod": "extra_combo_count",
    "WeaponMeleeComboEfficiencyMod": "heavy_attack_efficiency",
    "WeaponMeleeComboInitialBonusMod": "initial_combo",
    "WeaponMeleeComboPointsOnHitMod": "combo_gain_chance",
    "WeaponMeleeDamageMod": "melee_damage",
    "WeaponMeleeFactionDamageCorpus": "damage_vs_corpus",
    "WeaponMeleeFactionDamageGrineer": "damage_vs_grineer",
    "WeaponMeleeFactionDamageInfested": "damage_vs_infested",
    "WeaponMeleeFactionDamageOrokin": "damage_vs_orokin",
    "WeaponMeleeFactionDamageScaldra": "damage_vs_scaldra",
    "WeaponMeleeFactionDamageTechrot": "damage_vs_techrot",
    "WeaponMeleeFinisherDamageMod": "finisher_damage",
    "WeaponMeleeHeavyAttackChargeMod": "melee_heavy_attack_charge",
    "WeaponMeleeHeavyAttackDamageMod": "melee_heavy_attack_damage",
    "WeaponMeleeParryAngleMod": "melee_parry_angle",
    "WeaponMeleeRangeIncMod": "range",
    "WeaponMeleeSlamDamageMod": "melee_slam_damage",
    "WeaponProcTimeMod": "status_duration",
    "WeaponProjectileSpeedMod": "projectile_speed",
    "WeaponPunctureDepthMod": "punch_through",
    "WeaponRadiationDamageMod": "radiation_damage",
    "WeaponRecoilReductionMod": "recoil",
    "WeaponReloadSpeedMod": "reload_speed",
    "WeaponSlashDamageMod": "slash_damage",
    "WeaponStatusDamage": "status_damage",
    "WeaponStunChanceMod": "status_chance",
    "WeaponToxinDamageMod": "toxin_damage",
    "WeaponViralDamageMod": "viral_damage",
    "WeaponWeakpointCriticalChance": "weakpoint_crit_chance",
    "WeaponWeakpointDamage": "weakpoint_damage",
    "WeaponZoomFovMod": "zoom",
}


def _sid(tag: str, cls: str) -> str:
    sid = TAG_TO_ID.get(tag, "")
    return "attack_speed" if (cls == "melee" and sid == "fire_rate") else sid


def _load(path: Path) -> object:
    # DE 官方导出含未转义控制字符 ⇒ strict=False
    return json.loads(path.read_text(encoding="utf-8", errors="replace"), strict=False)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def class_pools(official: dict, pep: dict) -> tuple[dict, list[str]]:
    from core.riven_analysis import SPLICE_RECIPES

    spliced = {r[2] for r in SPLICE_RECIPES}
    off = {
        x.get("uniqueName", "").rsplit("/", 1)[-1]: x for x in official.get("ExportUpgrades") or []
    }
    pep_by = {k.rsplit("/", 1)[-1]: v for k, v in pep.items()}
    # PEP 里同一 tag 在其它职业的正负 / 限制（官方新增、PEP 该职业没有时借用）。
    # ★ 借用顺序固定：步枪 → 手枪 → 霰弹枪 → 近战 → 空战。空战枪的同名词条**没有**
    #   NotSentinel 限制（守护武器不会用空战紫卡），借到它会让守护霰弹枪（冰凇）漏掉「不出变焦」。
    any_tag: dict[str, dict] = {}
    for cls in ("rifle", "pistol", "shotgun", "melee", "archgun"):
        for e in (pep_by.get(TEMPLATES[cls]) or {}).get("upgradeEntries") or []:
            any_tag.setdefault(e.get("tag"), e)
    out: dict[str, dict] = {}
    unknown: list[str] = []
    for cls, tpl in TEMPLATES.items():
        pep_entries = {e["tag"]: e for e in (pep_by.get(tpl) or {}).get("upgradeEntries") or []}
        pool: dict[str, dict] = {}
        for e in (off.get(tpl) or {}).get("upgradeEntries") or []:
            tag = e.get("tag") or ""
            sid = _sid(tag, cls)
            if not sid:
                unknown.append(f"{cls}:{tag}")
                continue
            if sid in spliced:
                pool[sid] = {"bonus": True, "malus": False, "spliced": True}
                continue
            src = pep_entries.get(tag) or any_tag.get(tag)
            if not src:
                unknown.append(f"{cls}:{tag}（PEP 无正负数据）")
                continue
            row = {
                "bonus": bool(src.get("canBeBuff")),
                "malus": bool(src.get("canBeCurse")),
                "spliced": False,
            }
            if src.get("buffRestrictions"):
                row["buff_restr"] = sorted(src["buffRestrictions"])
            if src.get("curseRestrictions"):
                row["curse_restr"] = sorted(src["curseRestrictions"])
            if tag not in pep_entries:
                row["borrowed"] = True  # 官方新增、PEP 该职业缺：借其它职业同 tag
            pool[sid] = row
        out[cls] = dict(sorted(pool.items()))
    return out, unknown


def families(official_w: dict, pep_w: dict) -> tuple[dict, dict]:
    fam_doc = json.loads(FAMILIES.read_text(encoding="utf-8"))
    by_name: dict[str, str] = fam_doc.get("by_name") or {}
    off_by_name: dict[str, list[dict]] = {}
    for w in official_w.get("ExportWeapons") or []:
        off_by_name.setdefault(str(w.get("name") or "").strip().lower(), []).append(w)
    pep_tags = {k: set(v.get("compatibilityTags") or []) for k, v in pep_w.items()}

    roots: dict[str, list[str]] = {}
    for name, root in by_name.items():
        roots.setdefault(root, []).append(name)
    out: dict[str, dict] = {}
    stats = {"no_weapon_data": 0, "no_projectile_tag_data": 0}
    for root, names in sorted(roots.items()):
        recs = [w for n in names for w in off_by_name.get(n.strip().lower(), [])]
        if not recs:
            stats["no_weapon_data"] += 1
            continue
        entry: dict = {}
        if all(w.get("productCategory") == "SentinelWeapons" for w in recs):
            entry["sentinel"] = True
        if any("/Grimoire/" in (w.get("uniqueName") or "") for w in recs):
            entry["grimoire"] = True
        tagged = [pep_tags[w["uniqueName"]] for w in recs if w.get("uniqueName") in pep_tags]
        if tagged:
            entry["projectile"] = any("PROJECTILE" in t for t in tagged)
        else:
            stats["no_projectile_tag_data"] += 1
        if entry:
            out[root] = entry

    # 我方抽样覆盖层（键 = 母武器英文名；按家族表换成家族根）
    survey_doc = json.loads(SURVEY.read_text(encoding="utf-8")) if SURVEY.exists() else {}
    surveyed = survey_doc.get("_surveyed") or ""
    lower = {k.lower(): v for k, v in by_name.items()}
    stats["survey_without_root"] = 0
    for name, s in sorted((survey_doc.get("families") or {}).items()):
        root = by_name.get(name) or lower.get(name.lower())
        if not root:
            # 家族表里还没有的新武器（Aksondol / Nunchasa…）：按名字作键保留，运行期按名字查
            stats["survey_without_root"] += 1
            root = f"name:{name}"
        out.setdefault(root, {})["survey"] = {
            "n": s.get("n") or 0,
            # 家族自带抽样日期优先（多轮抽样合并时各家族日期不同），没有才用文件级 _surveyed
            "surveyed": s.get("surveyed") or surveyed,
            "source": "local",
            "family": name,  # 抽样时用的家族名（母武器英文名），防串家族用
            "rollable": list(s.get("rollable") or []),
            "never": list(s.get("never") or []),
            "unclear": list(s.get("unclear") or []),
        }
    return dict(sorted(out.items())), stats


def build(official_dir: Path, pep_dir: Path) -> dict:
    up_off = official_dir / "ExportUpgrades_en.json"
    w_off = official_dir / "ExportWeapons_en.json"
    up_pep = pep_dir / "ExportUpgrades.json"
    w_pep = pep_dir / "ExportWeapons.json"
    pools, unknown = class_pools(_load(up_off), _load(up_pep))
    fams, fstats = families(_load(w_off), _load(w_pep))
    return {
        "_source": (
            "DE 官方 Public Export（ExportUpgrades_en / ExportWeapons_en，content.warframe.com）"
            " + calamity-inc/warframe-public-export-plus（MIT；词条正负与限制、武器兼容标签）"
            " + 我方 WM 挂单抽样（riven_pools_survey.json）；生成器 scripts/build_riven_pools.py"
        ),
        "_inputs": {
            "official_ExportUpgrades_en": _sha(up_off),
            "official_ExportWeapons_en": _sha(w_off),
            "pep_ExportUpgrades": _sha(up_pep),
            "pep_ExportWeapons": _sha(w_pep),
        },
        "_generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "_rule": (
            "① 职业池 = 官方现行紫卡模板的词条清单；正负与限制（DamageTypes / NotSentinel / NotGrimoire）取 PEP，"
            "融合词条只出正面；② 家族属性：守护武器（全员 SentinelWeapons）、魔典（/Grimoire/）、"
            "有弹道（PEP 兼容标签含 PROJECTILE）；③ 物理三项按武器伤害占比 ≤25% 不列（DE DamageTypes 规则，"
            "2026-10-25 维护者定「按规则删，再用自己抽样纠偏」）；④ 我方抽样：见过 ⇒ 照列（压过规则），"
            "规则保留但没见过 ⇒ 列出并标「?」；规则排除且没见过 ⇒ 不列。"
            "2026-10-25 起不再使用 wfsim 数据。"
        ),
        "_counts": {
            "class_pools": {k: len(v) for k, v in pools.items()},
            "families": len(fams),
            "with_survey": sum(1 for v in fams.values() if v.get("survey")),
            "sentinel": sum(1 for v in fams.values() if v.get("sentinel")),
            "grimoire": sum(1 for v in fams.values() if v.get("grimoire")),
            "no_projectile": sum(1 for v in fams.values() if v.get("projectile") is False),
            **fstats,
            "unknown_tags": unknown,
        },
        "class_pools": pools,
        "families": fams,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--official", required=True, help="DE 官方导出目录（ExportUpgrades_en.json 等）"
    )
    ap.add_argument(
        "--pep", required=True, help="warframe-public-export-plus 目录（ExportUpgrades.json 等）"
    )
    ap.add_argument("--out", default=str(OUT_DEFAULT))
    ap.add_argument("--check", action="store_true", help="只校验已生成文件可复现（不写盘）")
    a = ap.parse_args()
    doc = build(Path(a.official), Path(a.pep))
    out = Path(a.out)
    if a.check:
        old = json.loads(out.read_text(encoding="utf-8"))
        same = all(old.get(k) == doc.get(k) for k in ("class_pools", "families", "_inputs"))
        print("可复现 ✔" if same else "✗ 与已生成文件不一致")
        return 0 if same else 1
    out.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"写出 {out}：{doc['_counts']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
