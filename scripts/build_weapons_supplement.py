# -*- coding: utf-8 -*-
"""补全计算器武器表 weapons_stats.json：WFCD warframe-items 没有的武器，由英文 wiki + DE 官方导出生成。

为什么要补：weapons_stats 的主体来自 WFCD warframe-items（build_damage_data.py），但它的最新版也没收
新武器（Daikyu Prime / Perigale Prime / Thornbak …），Archgun 与守护武器整类缺失 —— 这些武器「伤害 …」查不到。

两类目标（2026-10-26，取代 scripts/build_archgun_sentinel.py 的 wfsim 来源）：
- ``NEW_WEAPONS``：维护者核查过已上线的新武器（待查清单 §八，23 把）；
- 表里 ``source`` 写着 wfsim 的条目（Archgun 20 / 守护武器 21 / Valkyr Talons）：改由本脚本按 wiki 重新生成。

数据怎么取：
- 主面板 = wiki ``Module:Weapons/data`` 该武器 ``_TooltipAttackDisplay`` 指的攻击（游戏军械库展示的那个，
  没有该标记取第一个根攻击；Archgun 取「(Atmosphere)」大气条目，空战面板在 weapon_deployments.json）：伤害 / 暴击 / 暴伤 / 触发 / 射速 / 多重 /
  耗弹 / 点射；弹匣 / 装填 / 扳机 / 类别 / 精通等级取武器级字段。
- ``attacks``：wiki 全部攻击，按 WFCD 格式（暴击、触发写百分数）；近战的重击 / 震地 / 滑砍等取武器级字段。
- 键与 uniqueName 以 **DE 官方导出**为准（wiki 个别 InternalName 过时：Quassus Prime / Kuva Ghoulsaw）；
  紫卡倾向 omegaAttenuation 取 DE 官方值。中文名取 core/data/de/name_zh.json（DE 官方简中），没收的回落
  DE 导出简中版（``--official`` 目录下的 ExportWeapons_zh.json）。
- Vinquibus 枪 / 近战两形态共用一个 uniqueName：近战形态键加 ``#melee``。

用法：python scripts/build_weapons_supplement.py --wiki <wiki 页面目录> --official <DE 导出目录> [--check]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _wiki_weapons import damage, load_weapons  # noqa: E402
from build_weapon_attacks import NAME_KEYS, fixed_entry, roots_and_children  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
WS = ROOT / "core" / "data" / "weapons_stats.json"
NAME_ZH = ROOT / "core" / "data" / "de" / "name_zh.json"
SOURCE = "英文 wiki Module:Weapons/data + DE Public Export（scripts/build_weapons_supplement.py）"

# 待查清单 §八（维护者核查：均已上线）。值 = wiki 条目名
NEW_WEAPONS = [
    "Afentis Prime",
    "Alternox Prime",
    "Daikyu Prime",
    "Perigale Prime",
    "Steflos Prime",
    "Vadarya Prime",
    "Coda Bubonico",
    "Tenet Quanta",
    "Haalvu",
    "Thornbak",
    "Nunchasa",
    "Vinquibus (Primary)",
    "Athodai Prime",
    "Kompressa Prime",
    "Sagek Prime",
    "Aksondol",
    "Scyotid",
    "Corufell Prime",
    "Galariak Prime",
    "Sarofang Prime",
    "Venato Prime",
    "Vinquibus (Melee)",
    "Quassus Prime",
    "Kuva Ghoulsaw",
]
CATEGORY = {
    "Primary": "Primary",
    "Secondary": "Secondary",
    "Melee": "Melee",
    "Robotic": "Sentinel",
    "Archgun (Atmosphere)": "Archgun",
    "Archgun": "Archgun",
}
# wiki 没写 _TooltipAttackDisplay、需指定主段的：wiki 名 → 攻击名
# Mandonel：沿用原表默认（蓄力），与同类蓄力 Archgun（Corvas / Velocitus，wiki 展示攻击均为满蓄力）一致
MAIN_ATTACK = {"Mandonel (Atmosphere)": "Charge"}
# 条目级修正：wiki 名 → {字段: 值}（每条写依据）
# Arbucep：一次齐射 6 枚火箭（DE 导出 multishot 6、弹匣 6；wiki 把 6 枚拆成 1st…6th Attack 各一条）⇒
#   多重 6、每发耗 6 弹（同 build_damage_data.AMMO_COST_FIX）；6 枚各带不同元素未建模，按第一枚爆炸算
ENTRY_FIX = {"Arbucep (Atmosphere)": {"multishot": 6.0, "ammoCost": 6.0}}
# 近战武器级字段 → weapons_stats 字段
MELEE_FIELDS = {
    "HeavyAttack": "heavyAttackDamage",
    "SlideAttack": "slideAttack",
    "WindUp": "windUp",
    "FollowThrough": "followThrough",
    "ComboDur": "comboDuration",
}


def disposition_dots(omega: float) -> int:
    """紫卡倾向数值 → 点数（wiki Riven Mods §Disposition：<0.7 / <0.9 / ≤1.1 / ≤1.3 / 更高）。"""
    if omega < 0.7:
        return 1
    if omega < 0.9:
        return 2
    if omega <= 1.1:
        return 3
    if omega <= 1.3:
        return 4
    return 5


def wfcd_attack(a: dict) -> dict:
    """wiki 攻击 → WFCD attacks 格式（暴击 / 触发写百分数，与 warframe-items 一致）。"""
    d = {k: v for k, v in damage(a).items() if k != "total"}
    out = {
        "name": a.get("AttackName"),
        "damage": d,
        "total": round(sum(d.values()), 4),
        "crit_chance": round(float(a.get("CritChance") or 0.0) * 100, 4),
        "crit_mult": float(a.get("CritMultiplier") or 1.0),
        "status_chance": round(float(a.get("StatusChance") or 0.0) * 100, 4),
        "speed": float(a.get("FireRate") or 0.0),
    }
    if a.get("ShotType"):
        out["shot_type"] = a["ShotType"]
    fo = a.get("Falloff") or {}
    if fo.get("EndRange") is not None and float(fo["EndRange"]) <= 50:
        out["falloff"] = {
            "start": float(fo.get("StartRange") or 0.0),
            "end": float(fo["EndRange"]),
            "reduction": float(fo.get("Reduction") or 0.0),
        }
    return out


def resolve_unique(wname: str, e: dict, de_by_key: dict, de_by_name: dict) -> str:
    """DE 官方 uniqueName（原大小写）。wiki InternalName 不在 DE 导出里时按名字找。"""
    k = str(e.get("InternalName") or "").lower() or NAME_KEYS.get(wname, "")
    if k in de_by_key:
        return de_by_key[k]["uniqueName"]
    base = wname.replace(" (Atmosphere)", "").replace(" (Primary)", "").split(" (")[0].lower()
    hit = de_by_name.get(base)
    return hit["uniqueName"] if hit else ""


# DE 官方导出 damagePerShot 的类型顺序（20 项；第 15 项 Tau 之后是过场 / 吸盾等，计算器不建模）
DE_DAMAGE_ORDER = (
    "impact", "puncture", "slash", "heat", "cold", "electricity", "toxin", "blast", "radiation",
    "gas", "magnetic", "viral", "corrosive", "void", "tau",
)  # fmt: skip


def de_damage(de: dict) -> dict:
    """DE 官方主面板分元素伤害（wiki 主攻击没写伤害时的回落：Haalvu 打 Tau 33）。"""
    out = {k: float(v) for k, v in zip(DE_DAMAGE_ORDER, de.get("damagePerShot") or []) if v}
    if out:
        out["total"] = round(sum(out.values()), 4)
    return out


def build_entry(wname: str, e: dict, uname: str, de: dict, zh: dict) -> dict:
    e = fixed_entry(e)
    roots, _kids = roots_and_children(e)
    # 主段 = wiki ``_TooltipAttackDisplay`` 指的攻击（= 游戏军械库展示的那个，DE 导出主面板同此；
    # 蓄力 Archgun 为满蓄力，与原表一致）；没有该标记取第一个根攻击
    tip = e.get("_TooltipAttackDisplay")
    main = next(
        (a for a in roots if tip is not None and int(a["AttackIndex"]) == int(tip)), roots[0]
    )
    if wname in MAIN_ATTACK:
        main = next((a for a in roots if a.get("AttackName") == MAIN_ATTACK[wname]), main)
    cat = CATEGORY[e["Slot"]]
    dmg = damage(main) or de_damage(de)
    trig = str(main.get("Trigger") or e.get("Trigger") or "").split("/")[0].strip()
    omega = float(de.get("omegaAttenuation") or e.get("Disposition") or 1.0)
    base = wname.replace(" (Atmosphere)", "").replace(" (Primary)", "")
    rec: dict = {
        "name": base,
        "uniqueName": uname,
        "category": cat,
        "masteryReq": int(e.get("Mastery") or de.get("masteryReq") or 0),
        "damage": dmg,
        "criticalChance": float(main.get("CritChance") or 0.0),
        "criticalMultiplier": float(main.get("CritMultiplier") or 1.0),
        "procChance": float(main.get("StatusChance") or 0.0),
        "fireRate": float(main.get("FireRate") or 0.0),
        "omegaAttenuation": round(omega, 4),
        "disposition": disposition_dots(omega),
        "type": str(e.get("Class") or ""),
        "zh": zh.get(uname.lower()) or "",
        # 主段排第一（WFCD 惯例：attacks[0] = 顶层面板；build_damage_data.repair_weapons 按它核物理伤害）
        "attacks": [wfcd_attack(main)]
        + [
            wfcd_attack(a)
            for a in e.get("Attacks") or []
            if a is not main and a.get("AttackIndex") != main.get("AttackIndex")
        ],
        "source": SOURCE,
    }
    if cat != "Melee":
        rec["multishot"] = float(main.get("Multishot") or 1.0)
        rec["trigger"] = trig
        if e.get("Magazine") is not None:
            rec["magazineSize"] = float(e["Magazine"])
        if e.get("Reload") is not None:
            rec["reloadTime"] = float(e["Reload"])
        ac = main.get("AmmoCost")
        if ac is not None and float(ac) != 1.0:
            rec["ammoCost"] = float(ac)
        if int(main.get("BurstCount") or 0) > 1:
            rec["burstCount"] = int(main["BurstCount"])
            rec["burstDelay"] = float(main.get("BurstDelay") or 0.0)
    else:
        for wk, ok in MELEE_FIELDS.items():
            if e.get(wk) is not None:
                rec[ok] = float(e[wk])
        for a in roots:
            n = str(a.get("AttackName") or "")
            if n == "Slam Attack":
                rec["slamAttack"] = damage(a).get("total") or 0.0
                if a.get("Range"):
                    rec["slamRadius"] = float(a["Range"])
            elif n == "Heavy Slam Attack":
                rec["heavySlamAttack"] = damage(a).get("total") or 0.0
    rec.update(ENTRY_FIX.get(wname, {}))
    if cat == "Archgun":
        rec["slot"] = "archgun"
    if cat == "Sentinel":
        rec["slot"] = "sentinel"
    return rec


def build(wiki_dir: Path, official_dir: Path, ws: dict) -> tuple[dict, list[str]]:
    W = load_weapons(wiki_dir)
    de_list = json.loads(
        (official_dir / "ExportWeapons_en.json").read_text(encoding="utf-8"), strict=False
    )["ExportWeapons"]
    de_by_key = {w["uniqueName"].lower(): w for w in de_list}
    de_by_name = {w["name"].lower(): w for w in de_list}
    zh = json.loads(NAME_ZH.read_text(encoding="utf-8"))
    # 官方简中表还没收的新武器（Nunchasa / Aksondol …）：回落 DE 官方导出简中版 ExportWeapons_zh.json
    zh_path = official_dir / "ExportWeapons_zh.json"
    if zh_path.exists():
        for w in json.loads(zh_path.read_text(encoding="utf-8"), strict=False)["ExportWeapons"]:
            zh.setdefault(w["uniqueName"].lower(), w["name"])
    report: list[str] = []
    targets: list[tuple[str, str]] = []  # (wiki 名, 目标键)
    for n in NEW_WEAPONS:
        targets.append((n, ""))
    for k, v in ws.items():
        if "wfsim" in str(v.get("source") or ""):
            nm = str(v.get("name") or "")
            wn = f"{nm} (Atmosphere)" if f"{nm} (Atmosphere)" in W else nm
            targets.append((wn, k))
    out: dict = {}
    for wname, key in targets:
        e = W.get(wname)
        if e is None:
            report.append(f"{wname}：wiki 数据模块里没有，跳过")
            continue
        uname = resolve_unique(wname, e, de_by_key, de_by_name)
        if not uname:
            report.append(f"{wname}：DE 官方导出里找不到，跳过")
            continue
        de = de_by_key.get(uname.lower(), {})
        rec = build_entry(wname, e, uname, de, zh)
        k = key or uname.lower()
        if wname.endswith("(Melee)") and e.get("Slot") == "Melee":
            k = uname.lower() + "#melee"
            rec["name"] = wname
            rec["zh"] = (rec["zh"] + "（近战）") if rec["zh"] else ""
        if not rec["zh"]:
            report.append(f"{wname}：官方简中表没有中文名")
        # DE 官方主面板对照（DE 只有一组面板，蓄力武器等可能取的是另一攻击，只提示不改）
        if de and not key.endswith("#melee"):
            dt = float(de.get("totalDamage") or 0.0)
            wt = float(rec["damage"].get("total") or 0.0)
            if dt and abs(dt - wt) > 0.02 * max(dt, 1.0):
                report.append(f"{wname}：主面板伤害 wiki {wt:g} ≠ DE {dt:g}（按 wiki 展示攻击）")
        out[k] = rec
    return out, report


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wiki", required=True, type=Path)
    ap.add_argument("--official", required=True, type=Path)
    ap.add_argument("--check", action="store_true", help="只校验现有表与重新生成的一致，不写")
    ap.add_argument("--report", type=Path)
    args = ap.parse_args(argv[1:])
    ws = json.loads(WS.read_text(encoding="utf-8"))
    recs, report = build(args.wiki, args.official, ws)
    if args.check:
        bad = [k for k, v in recs.items() if ws.get(k) != v]
        print("可复现 ✔" if not bad else f"✗ {len(bad)} 条与重新生成不一致：{bad[:5]}")
        return 1 if bad else 0
    ws.update(recs)
    # 与 build_damage_data 写出的格式一致（indent=1、无末尾换行），避免整文件重排
    WS.write_text(json.dumps(ws, ensure_ascii=False, indent=1), encoding="utf-8")
    if args.report:
        args.report.write_text("\n".join(report) + "\n", encoding="utf-8")
    print(
        f"✓ weapons_stats.json：写入 {len(recs)} 条（含新武器与原 wfsim 来源条目）；提示 {len(report)} 条"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
