# -*- coding: utf-8 -*-
"""从英文 wiki 武器数据模块生成武器的「多段攻击」结构 → core/data/weapon_attacks.json。

数据源（2026-10-26 wfsim 迁移 P2 起；之前取自 wfsim，已弃用）：
- wiki ``Module:Weapons/data/*`` 的 ``Attacks``（CC BY-NC-SA 3.0）：每个攻击各自的伤害向量 /
  暴击 / 暴伤 / 触发 / 射速 / 多重 / 蓄力 / 点射 / 耗弹 / 衰减；``AttackParentIndex`` 指向母攻击的
  是它的子段（爆炸、云团）。
- wiki「Condition Overload (Mechanic)」Attack Catalog（``_wiki_weapons.co_catalog``）：表里列出的
  AoE 攻击且行为为 Adding / Multiplying ⇒ 该范围段吃 CO（takes_condition_overload）；备注
  「AoE does not scale off multishot」/「Not affected by multishot」⇒ takes_multishot false；
  非 AoE 行的 Multiplying / N/A ⇒ 该形态的 co_behavior（independent / inert，见 ``co_mode_of``）。
  CO 页只列了同族某一把的，不自动推给同族其它武器（2026-10-26 维护者定：Prime / 变体是否同机制不能假定）；
  逐把核实过的写在 ``_wiki_weapons.USER_VERIFIED_CO_ROWS``（每行带依据），其余按 wiki 原样（未列 = 缺省）。
- 我方 ``weapons_stats.json`` 主段面板（DE 导出）：用来认出哪个 wiki 攻击是我方的「默认主段」。

每把武器的攻击怎么拆（``split_attacks``）：
- 根攻击 = 无 AttackParentIndex 的攻击。范围攻击（ShotType AoE / DoT，或没写 ShotType 但名字带
  Radial / AoE / Explosion）若平铺成根攻击（Ferrox「Radial Attack」、Opticor「Charged Shot AoE」、
  Komorex「3.5x Zoom Radial Attack」），挂回前一个根攻击；名字带「Cluster」的根攻击（Kuva Bramma /
  Kulstar / Zarr / Phantasma）作为前一个根攻击的集束子炸弹；灵化与非灵化各自配对。
- 子攻击名以另一根攻击名开头的，挂到那个根攻击（Battacor「Secondary Fire AoE」wiki 写成
  AttackParentIndex 1，实属副开火）。
- 默认主段 = 与 weapons_stats 主段伤害合计 / 暴击 / 触发一致的根攻击；多个一致时先按每发耗弹
  （weapons_stats ammoCost）再按 wiki ``_TooltipAttackDisplay`` 裁（Staticor 未蓄力 / 满蓄力同为 44，
  我方主段耗 5 弹 = 满蓄力）；都不一致取第一个并报出。
- ``ATTACK_FIX`` / ``EXTRA_CHILD`` / ``EXTRA_STAGES``：wiki 数据模块的逐攻击修正与补充（每条写依据；
  2026-10-26 维护者核查定），在拆分之前套用（``fixed_entry``），三个生成器共用。
- 其余根攻击按名字 / 扳机分形态（``form_kind``）：incarnon / charged / base（未蓄力）/ semi_auto /
  auto / alt_fire；同种多条只收第一条（完美射击例外，与普通满蓄力并存），其余列在报告里。

输出口径（与运行期 core/damage_calc.py 对齐，沿用旧表 schema）：
- 顶层 segments / main / charge_seconds / forced_procs 只描述默认主段；
- forms 键 ``<种类>:<wiki 攻击名 slug>``，只在用户显式选了该形态时整套替换主段与额外段；
  灵化形态只收带额外段的（主段面板在 incarnon_forms.json）；
- 一个攻击有多个范围子段时，伤害最高的当 radial，其余列在报告里；只在爆头时触发的爆炸单收
  headshot_radial（headshot_only，计算器只在「爆头」时计入）；云团（DoT / 带 EffectDuration）
  段 merge_only；cluster 段带 count（= 该攻击的 Multishot）；段的暴击 / 触发取 wiki 该子攻击的值；
- 「Mag Burst」扳机（一次打空弹匣）没写 BurstCount 的，点射数 = 弹匣容量。

用法：python scripts/build_weapon_attacks.py --wiki <wiki 页面目录> [--out 路径] [--report 路径]
（目录取法见 scripts/_wiki_weapons.py fetch；只在本机 / Mac 取，不在服务器上抓 wiki）
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _wiki_weapons import (  # noqa: E402
    catalog_by_weapon,
    catalog_rows,
    damage,
    key_of,
    load_weapons,
    resolve_catalog_row,
    roots_and_children,
    slug,
)

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "core" / "data" / "weapon_attacks.json"
WS = ROOT / "core" / "data" / "weapons_stats.json"

# 有空战 / 大气两条的 archgun：我方主面板 = 大气条目（与 weapons_stats 一致），空战条目进 weapon_deployments
GUN_SLOTS = ("Primary", "Secondary", "Robotic", "Archgun (Atmosphere)", "Archgun")
STATUS_PROCS = {
    "Impact": "impact",
    "Puncture": "puncture",
    "Slash": "slash",
    "Heat": "heat",
    "Cold": "cold",
    "Electricity": "electricity",
    "Toxin": "toxin",
    "Blast": "blast",
    "Radiation": "radiation",
    "Gas": "gas",
    "Magnetic": "magnetic",
    "Viral": "viral",
    "Corrosive": "corrosive",
    "Void": "void",
}
# 与主段同值的连发子弹（Arbucep 一次 6 发分别列成 1st…6th Attack）：不当形态
_VOLLEY = re.compile(r"^(\d+(st|nd|rd|th)|second|third|fourth|fifth|sixth) attack$", re.I)
# 不是玩家可选开火方式的根攻击（子弹 / 召唤物的附带伤害）：不收进 forms
FORM_SKIP = {
    ("Zymos", "Homing Spore Contact"),
    ("Azima", "Turret Expiry"),
}
# wiki AttackParentIndex 写错的子攻击：(wiki 武器名, 子攻击名) → 实际母攻击名
PARENT_FIX = {
    # 模块把蓄力爆炸挂在持续光束（Held Damage）下；名字与 Charge Attack 成对
    ("Onos", "Incarnon Form Charge Radial Attack"): "Incarnon Form Charge Attack",
}
# wiki 数据模块的逐攻击修正：(wiki 武器名, 攻击名) → 修正项。每条写明依据；2026-10-26 维护者核查
# （待查清单执行报告，wiki 正文 / CO 页 / 游戏数据导出 behaviours）定。修正项：
#   damage_scale 伤害倍数 · falloff_reduction / radius 衰减与半径 · forced_procs_add 补强制异常 ·
#   burst_count 点射发数
ATTACK_FIX: dict[tuple[str, str], dict] = {
    # 模块记 100 冲击 / 100 辐射；CO 页「Alt Fire Impact 1000 … Scales properly with magazine size」、
    # 武器页「multiplied by the magazine capacity」⇒ 基础弹匣 10 发时直击 1000、爆炸 1000（弹匣 MOD 缩放未建模）
    ("Tenet Plinx", "Alt-Fire"): {"damage_scale": 10.0},
    ("Tenet Plinx", "Alt-Fire AoE"): {"damage_scale": 10.0},
    # 模块普通版写衰减 0，Burston Prime 写 1；两把共用的灵化页正文「100% → 0%」⇒ 1.0
    ("Burston", "Incarnon Form Radial Attack"): {"falloff_reduction": 1.0},
    # 模块缺 ForcedProcs；灵化页正文「guaranteed Impact」（Latron Prime 模块已有）
    ("Latron", "Incarnon Form"): {"forced_procs_add": ["Impact"]},
    ("Latron Wraith", "Incarnon Form"): {"forced_procs_add": ["Impact"]},
    # 模块只写 BurstDelay 0.06；武器页「burst-fire 2 shots … consumes 4 ammo (2 per pistol)」
    ("Tenet Diplos", "Lock-on Mode"): {"burst_count": 2},
    # 模块无半径；wiki 正文 8 m、边缘衰减 0.4
    ("Mausolon (Atmosphere)", "Charged Shot Explosion"): {"radius": 8.0, "falloff_reduction": 0.4},
}
# 模块只挂在一个母攻击下、实际另一射法也有的子攻击：(wiki 武器名, 子攻击名) → 另挂的母攻击名
# Mutalist Cernos：CO 页两行「Uncharged / Charged AoE Toxin Cloud」⇒ 未蓄力也留毒云（模块只挂在蓄力下）
EXTRA_CHILD = {("Mutalist Cernos", "Toxin Cloud"): "Uncharged Shot"}
# 一发射击的第二个伤害阶段（与 radial 同发都触发，计入含段合计）：(wiki 武器名, 子攻击名) → 次数
EXTRA_STAGES = {
    ("Lenz", "Initial Blast"): 1.0,  # 命中即冰爆 10，1.3 s 后 Bubble Collapse
    ("Prisma Lenz", "Initial Blast"): 1.0,
    ("Proboscis Cernos", "Appendages"): 2.0,  # 触手每发跳 2 次（wiki 正文「tick damage twice」）
}
# 半径上限：wiki 个别范围子段的 Falloff 抄的是母弹的距离衰减（Arbucep 200–270 m），不是爆炸半径
MAX_RADIUS_M = 50.0
# wiki 没写 InternalName 的条目：wiki 名 → 我方 uniqueName（按 weapons_stats 名字核对过）
NAME_KEYS = {
    "Enkaus": "/lotus/weapons/tenno/longguns/tninkrifle/tninkrifleweapon",
    "Mandonel (Atmosphere)": "/lotus/weapons/tenno/archwing/primary/tnconcretearchgun/tnconcretearchgunweapon",
    "Mandonel": "/lotus/weapons/tenno/archwing/primary/tnconcretearchgun/tnconcretearchgunweapon",
}


def entry_key(name: str, e: dict) -> str:
    return (
        key_of(e) if str(e.get("InternalName") or "").startswith("/") else NAME_KEYS.get(name, "")
    )


def base_name(wname: str) -> str:
    return wname.replace(" (Atmosphere)", "")


def procs(atk: dict) -> list[str]:
    return [STATUS_PROCS[p] for p in (atk.get("ForcedProcs") or []) if p in STATUS_PROCS]


def _name(a: dict) -> str:
    return str(a.get("AttackName") or "")


def is_aoe(a: dict) -> bool:
    st = str(a.get("ShotType") or "")
    if st in ("AoE", "DoT"):
        return True
    return not st and bool(re.search(r"radial|\baoe\b|explosion", _name(a), re.I))


def is_cloud(a: dict) -> bool:
    return a.get("ShotType") == "DoT" or bool(a.get("EffectDuration"))


def is_incarnon(a: dict) -> bool:
    return "incarnon" in _name(a).lower()


def fixed_entry(e: dict) -> dict:
    """套用 ATTACK_FIX / EXTRA_CHILD 后的条目副本（不改原条目）。"""
    wname = str(e.get("Name") or "")
    atks = [dict(a) for a in (e.get("Attacks") or [])]
    for i, a in enumerate(atks, 1):
        a.setdefault("AttackIndex", i)
        fx = ATTACK_FIX.get((wname, _name(a))) or {}
        if fx.get("damage_scale"):
            a["Damage"] = {k: v * fx["damage_scale"] for k, v in (a.get("Damage") or {}).items()}
        if "falloff_reduction" in fx or "radius" in fx:
            fo = dict(a.get("Falloff") or {})
            if "radius" in fx:
                fo.setdefault("StartRange", 0)
                fo["EndRange"] = fx["radius"]
            if "falloff_reduction" in fx:
                fo["Reduction"] = fx["falloff_reduction"]
            a["Falloff"] = fo
        if fx.get("forced_procs_add"):
            a["ForcedProcs"] = list(a.get("ForcedProcs") or []) + [
                p for p in fx["forced_procs_add"] if p not in (a.get("ForcedProcs") or [])
            ]
        if fx.get("burst_count"):
            a["BurstCount"] = fx["burst_count"]
        # 武器扳机含「Mag Burst」、该攻击写 Burst 却没给发数（Athodai 副开火）⇒ 一次打空弹匣
        if (
            str(a.get("Trigger") or "") == "Burst"
            and not a.get("BurstCount")
            and "mag burst" in str(e.get("Trigger") or "").lower()
        ):
            a["Trigger"] = "Mag Burst"
    for (wn, child), parent in EXTRA_CHILD.items():
        if wn != wname:
            continue
        p = next((a for a in atks if _name(a) == parent), None)
        c = next((a for a in atks if _name(a) == child), None)
        if p and c:
            atks.append({**c, "AttackIndex": len(atks) + 1, "AttackParentIndex": p["AttackIndex"]})
    return {**e, "Attacks": atks}


def split_attacks(e: dict) -> list[dict]:
    """根攻击列表，每个带 ``_aoe``（范围 / 云团子段）与 ``_cluster``（集束子炸弹根攻击）。"""
    e = fixed_entry(e)
    roots, kids = roots_and_children(e)
    # 子攻击名以另一根攻击名开头 ⇒ 改挂那个根攻击（取最长前缀）；PARENT_FIX 显式修正优先
    for p, ks in list(kids.items()):
        for k in list(ks):
            fix = PARENT_FIX.get((str(e.get("Name") or ""), _name(k)))
            best = max(
                (
                    r
                    for r in roots
                    if r is not k and _name(k).lower().startswith(_name(r).lower() + " ")
                ),
                key=lambda r: len(_name(r)),
                default=None,
            )
            if fix:
                best = next((r for r in roots if _name(r) == fix), best)
            if best is not None and int(best["AttackIndex"]) != p:
                ks.remove(k)
                kids.setdefault(int(best["AttackIndex"]), []).append(k)
    out: list[dict] = []
    for a in roots:
        a = dict(a)
        a["_aoe"] = [k for k in kids.get(int(a["AttackIndex"]), []) if is_aoe(k)]
        a["_cluster"] = []
        prev = next((r for r in reversed(out) if is_incarnon(r) == is_incarnon(a)), None)
        if prev is not None and is_aoe(a):
            tgt = prev["_cluster"][-1] if prev["_cluster"] else prev
            tgt["_aoe"].append(a)
            continue
        if prev is not None and "cluster" in _name(a).lower():
            prev["_cluster"].append(a)
            continue
        out.append(a)
    return out


def _close(x, y, tol=0.011) -> bool:
    if x is None or y is None:
        return False
    return abs(float(x) - float(y)) <= tol * max(abs(float(y)), 1.0)


def pick_main(roots: list[dict], ws: dict, tooltip) -> tuple[dict, bool]:
    """与 weapons_stats 主段一致的根攻击（多个一致取 _TooltipAttackDisplay 那个）；
    找不到返回 (第一个, False)。"""
    tot = (ws.get("damage") or {}).get("total")
    hits = [
        a
        for a in roots
        if _close(damage(a).get("total"), tot)
        and _close(a.get("CritChance"), ws.get("criticalChance"), 0.02)
        and _close(a.get("StatusChance"), ws.get("procChance"), 0.02)
    ]
    if not hits:
        return roots[0], False
    # 并列（Staticor 未蓄力 / 满蓄力同为 44）：先按每发耗弹（weapons_stats ammoCost）裁，再按 tooltip
    ac = float(ws.get("ammoCost") or 1.0)
    hits = [a for a in hits if float(a.get("AmmoCost", 1) or 0) == ac] or hits
    pref = [a for a in hits if tooltip is not None and int(a["AttackIndex"]) == int(tooltip)]
    return (pref or hits)[0], True


def pick_incarnon(roots: list[dict]):
    """灵化形态的主攻击：优先名为「Incarnon Form」的根攻击，否则第一个带 Incarnon 的。"""
    inc = [r for r in roots if is_incarnon(r)]
    exact = [r for r in inc if _name(r).strip().lower() == "incarnon form"]
    return (exact or inc or [None])[0]


def form_kind(a: dict, main: dict, weapon_trigger: str) -> str:
    """非默认根攻击 → 形态种类（空串 = 不收）。"""
    n = _name(a).lower()
    if is_incarnon(a):
        return "incarnon"
    if _VOLLEY.match(n):
        return ""
    if "perfect" in n:
        return "charged"
    if re.search(r"uncharged|quick shot|partially charged|normal shot", n):
        return "base"
    if "charge" in n:
        return "charged"
    if re.search(r"\balt\b|alt-fire|alternate|secondary fire|throw|grenade|harpoon|lock-on", n):
        return "alt_fire"
    if re.search(r"\bsemi\b|semi-auto", n):
        return "semi_auto"
    if re.search(r"(^|\s)(full[- ])?auto( mode)?$|^full-auto$", n):
        return "auto"
    main_charges = (
        bool(main.get("ChargeTime")) or "charge" in str(main.get("Trigger") or "").lower()
    )
    if main_charges and "charge" in weapon_trigger.lower() and not a.get("ChargeTime"):
        return "base"  # 纯蓄力武器的另一种不蓄力射法（Ballistica 点射、Mandonel 横向散射）
    if not main_charges and (
        a.get("ChargeTime") or "charge" in str(a.get("Trigger") or "").lower()
    ):
        return "charged"
    return "alt_fire"


def main_kind(main: dict, first: dict, forms: dict) -> str:
    """我方默认主段的形态种类（写进 main_form；空串 = 不写）。计算器据此把「蓄力 / 未蓄力 / 全自动」
    等关键词认作「就是默认主段」，而不是报「没有该形态数据」。"""
    n = _name(main).lower()
    if "charge" in n and "uncharged" not in n:
        return "charged"
    if re.search(r"uncharged|quick shot|partially charged|normal shot", n):
        return "base"
    if re.search(r"\bsemi\b|semi-auto", n):
        return "semi_auto"
    if re.search(r"(^|\s)(full[- ])?auto( mode)?$|^full-auto$", n):
        return "auto"
    if main is first and any(k.startswith("charged:") for k in forms):
        return "base"
    return ""


def seg(a: dict, sid: str, name: str) -> dict:
    s = {
        "id": sid,
        "name": name,
        "attack": a.get("AttackName"),
        "damage": damage(a),
        "criticalChance": float(a.get("CritChance") or 0.0),
        "criticalMultiplier": float(a.get("CritMultiplier") or 1.0),
        "procChance": float(a.get("StatusChance") or 0.0),
    }
    fo = a.get("Falloff") or {}
    if fo.get("EndRange") is not None and float(fo["EndRange"]) <= MAX_RADIUS_M:
        s["radius_m"] = float(fo["EndRange"])
        s["falloff_start_m"] = float(fo.get("StartRange") or 0.0)
        s["falloff_reduction"] = float(fo.get("Reduction") or 0.0)
    elif a.get("Range") is not None and float(a["Range"]) <= MAX_RADIUS_M:
        # 没写衰减（或 Falloff 是母弹的弹道衰减，Arbucep）：半径取该子攻击的 Range，衰减不写
        s["radius_m"] = float(a["Range"])
    return s


def segments_of(root: dict, wname: str, report: list) -> list[dict]:
    """一个根攻击的额外段：伤害最高的非云团、非爆头范围子段 = radial；只在爆头时触发的爆炸
    = headshot_radial（``headshot_only``，计算器只在「爆头」时计入，2026-10-26 维护者定）；
    云团 = lingering（merge_only）；集束 = cluster(+cluster_radial)。"""
    segs: list[dict] = []
    aoe = [a for a in root["_aoe"] if not is_cloud(a)]
    aoe.sort(key=lambda a: -(damage(a).get("total") or 0.0))
    regular = [a for a in aoe if "headshot" not in _name(a).lower()]
    head = [a for a in aoe if "headshot" in _name(a).lower()]
    for i, a in enumerate(regular):
        if i == 0:
            segs.append(seg(a, "radial", "范围伤害"))
            fo = a.get("Falloff") or {}
            if fo.get("EndRange") is not None and float(fo["EndRange"]) > MAX_RADIUS_M:
                report.append(f"{wname}：「{_name(a)}」Falloff {fo} 不像爆炸半径，半径取 Range")
        elif (base_name(wname), _name(a)) in EXTRA_STAGES:
            s = seg(a, f"radial_{i + 1}", "范围伤害（第二阶段）")
            s["count"] = EXTRA_STAGES[(base_name(wname), _name(a))]
            segs.append(s)
        else:
            report.append(
                f"{wname}：「{_name(root)}」另有范围段「{_name(a)}」（{damage(a).get('total')}）未收"
            )
    for i, a in enumerate(head):
        if i == 0:
            s = seg(a, "headshot_radial", "爆头爆炸")
            s["headshot_only"] = True
            segs.append(s)
        else:
            report.append(f"{wname}：「{_name(root)}」另有爆头范围段「{_name(a)}」未收")
    for a in root["_aoe"]:
        if is_cloud(a):
            s = seg(a, "lingering", "残留云团")
            s["merge_only"] = True
            segs.append(s)
    for c in root["_cluster"]:
        n = float(c.get("Multishot") or 1.0)
        s = seg(c, "cluster", "集束子炸弹（接触）")
        s["count"] = n
        segs.append(s)
        for b in c["_aoe"][:1]:
            s = seg(b, "cluster_radial", "集束子炸弹（爆炸）")
            s["count"] = n
            segs.append(s)
    return segs


def apply_catalog(
    wname: str, roots: list[dict], main: dict, segs_of: dict, rows: list[dict], report: list
) -> None:
    """把 CO 页 Attack Catalog 的 AoE 行落到对应根攻击的范围 / 云团段上（就地改 segs_of 里的段）。"""
    for r in rows:
        if "aoe" not in r["type"].lower():
            continue
        notes = r["notes"].lower()
        fl = {}
        if r["behavior"] in ("Adding", "Multiplying"):
            fl["takes_condition_overload"] = True
        if "does not scale off multishot" in notes or "not affected by multishot" in notes:
            fl["takes_multishot"] = False
        if not fl:
            continue
        cands = [
            s
            for root in roots
            for s in segs_of[id(root)]
            if s["id"] in ("radial", "lingering", "cluster_radial")
        ]
        hit = [s for s in cands if str(s.get("attack") or "").lower() == r["attack"].lower()]
        if not hit:
            root = resolve_catalog_row(r["attack"], roots, main, lambda x: bool(segs_of[id(x)]))
            if root is not None:
                want = (
                    "lingering"
                    if "cloud" in r["attack"].lower() or "dot" in r["attack"].lower().split()
                    else None
                )
                ss = segs_of[id(root)]
                hit = (
                    [s for s in ss if s["id"] == want]
                    if want
                    else [s for s in ss if s["id"] == "radial"]
                )
                hit = hit or [s for s in ss if s["id"] in ("radial", "lingering")][:1]
        if not hit:
            report.append(f"{wname}：CO 页「{r['attack']}」{fl} 对不上任何范围段")
            continue
        for s in hit[:1]:
            s.update(fl)


CO_DEFAULT = "additive_with_base_damage"


def co_mode_of(row: dict) -> str:
    """CO 页一行的 Math/Behavior Type → 我方 CO 桶别：Multiplying = independent（自成乘区）；
    N/A / Does not apply = inert（CO 对该攻击无效）；Adding = 缺省（与基伤同一加法括号）。"""
    b = row["behavior"].strip().lower()
    if b == "multiplying":
        return "independent"
    if b in ("n/a", "does not apply") or (
        not b and row["notes"].lower().startswith("does not apply")
    ):
        return "inert"
    return CO_DEFAULT


# CO 页攻击名与数据模块攻击名对不上、类别也推不出的：(wiki 武器名, CO 页攻击名) → 模块攻击名
CATALOG_ALIAS = {
    # Sancti Castanas 两行写作 Mid-Flight Detonation / Embedded Detonation，Castanas 一行笼统写 Normal Attack
    ("Castanas", "Normal Attack"): "Mid-Flight Detonation",
    # Mandonel 的未蓄力射法模块里叫 Horizontal Spread
    ("Mandonel", "Uncharged Attack"): "Horizontal Spread",
}


def co_modes(
    wname: str, roots: list[dict], main: dict, rows: list[dict], report: list
) -> dict[int, str]:
    """CO 页非 AoE 行 → {id(根攻击): 桶别}（只含对上的行；同一攻击多行冲突时报出、取第一行）。"""
    out: dict[int, str] = {}
    for r in rows:
        name = CATALOG_ALIAS.get((wname.replace(" (Atmosphere)", ""), r["attack"]), r["attack"])
        hit = [x for x in roots if _name(x).lower() == name.lower()]
        if "aoe" in r["type"].lower() and not hit:
            continue  # 范围子段的行由 apply_catalog 处理；整把是爆炸弹（Castanas）的按根攻击名对上才算
        root = hit[0] if hit else resolve_catalog_row(name, roots, main)
        if root is None:
            report.append(f"{wname}：CO 页「{r['attack']}」（{r['behavior']}）对不上任何攻击")
            continue
        mode = co_mode_of(r)
        if id(root) in out and out[id(root)] != mode:
            report.append(
                f"{wname}：CO 页多行落到「{_name(root)}」且桶别不同（{out[id(root)]} / {mode}），取前者"
            )
            continue
        out.setdefault(id(root), mode)
    return out


def form_record(a: dict, wname: str, segs: list[dict], magazine) -> dict:
    dmg = damage(a)
    rec: dict = {
        "main": {
            "damage": dmg,
            "criticalChance": float(a.get("CritChance") or 0.0),
            "criticalMultiplier": float(a.get("CritMultiplier") or 1.0),
            "procChance": float(a.get("StatusChance") or 0.0),
            "fireRate": float(a.get("FireRate") or 0.0),
            "multishot": float(a.get("Multishot") or 1.0),
        },
        "name": base_name(wname),
        "attack": a.get("AttackName"),
    }
    if segs:
        rec["segments"] = segs
    if a.get("ChargeTime"):
        rec["charge_seconds"] = float(a["ChargeTime"])
    count = int(a.get("BurstCount") or 0)
    delay = float(a.get("BurstDelay") or 0.0)
    if not count and str(a.get("Trigger") or "") == "Mag Burst" and magazine:
        count = int(magazine)
        if not delay and a.get("FireRate") and float(a["FireRate"]) < float("inf"):
            # 一次打空弹匣、没写发间隔（Athodai 副开火）：发间隔 = 1 / 该攻击射速（wiki 副开火 12 发/秒）
            delay = round(1.0 / float(a["FireRate"]), 4)
    if count > 1:
        rec["burst"] = {"count": count, "delay_seconds": delay}
    if procs(a):
        rec["forced_procs"] = procs(a)
    ac = a.get("AmmoCost", a.get("Ammocost"))
    if ac is not None and float(ac) != 1.0:
        rec["ammo_cost"] = float(ac)
    return rec


def build(wiki_dir: Path, ws_all: dict) -> tuple[dict, list[str]]:
    W = load_weapons(wiki_dir)
    catalog = catalog_by_weapon(catalog_rows(wiki_dir))
    report: list[str] = []
    out: dict = {}
    for wname, e in sorted(W.items()):
        if e.get("Slot") not in GUN_SLOTS:
            continue
        if e.get("Slot") == "Archgun" and f"{wname} (Atmosphere)" in W:
            continue  # 空战面板：进 weapon_deployments
        key = entry_key(wname, e)
        if not key:
            report.append(f"{wname}：wiki 无 InternalName，跳过")
            continue
        roots = split_attacks(e)
        if not roots:
            continue
        ws = ws_all.get(key) or {}
        if not ws:
            report.append(f"{wname}：计算器（weapons_stats）无此武器，跳过")
            continue
        main, ok = pick_main(roots, ws, e.get("_TooltipAttackDisplay"))
        if not ok:
            report.append(
                f"{wname}：没有与 weapons_stats 主段（{(ws.get('damage') or {}).get('total')} / "
                f"{ws.get('criticalChance')} / {ws.get('procChance')}）一致的 wiki 攻击，取第一个「{_name(main)}」"
            )
        segs_of = {id(r): segments_of(r, wname, report) for r in roots}
        rows = catalog.get(base_name(wname), [])
        apply_catalog(wname, roots, main, segs_of, rows, report)
        modes = co_modes(wname, roots, main, rows, [])  # 对不上的行由 build_co_behavior 报
        rec: dict = {}
        segs = segs_of[id(main)]
        if main.get("ChargeTime"):
            rec["charge_seconds"] = float(main["ChargeTime"])
        if procs(main):
            rec["forced_procs"] = procs(main)
        if segs:
            rec["segments"] = segs
            rec["main"] = {"damage": damage(main)}
        rec["name"] = base_name(wname)
        rec["wiki"] = wname
        rec["main_attack"] = main.get("AttackName")
        forms: dict = {}
        inc_main = pick_incarnon(roots)
        for a in roots:
            if a is main or (base_name(wname), _name(a)) in FORM_SKIP:
                continue
            if not a.get("ShotType") and a.get("EffectDuration"):
                continue  # 附着后的持续伤害（Paracyst「Infested Harpoon Contact」），不是开火方式
            kind = form_kind(a, main, str(e.get("Trigger") or ""))
            if not kind:
                continue
            fsegs = segs_of[id(a)]
            # 灵化形态只收与 incarnon_forms 主段同一攻击的那条（其余灵化攻击如 Onos 蓄力不收）
            if kind == "incarnon" and (a is not inc_main or not fsegs):
                continue
            perfect = "perfect" in _name(a).lower()
            if any(k.startswith(kind + ":") and ("perfect" in k) == perfect for k in forms):
                report.append(f"{wname}：同种形态（{kind}）已有，「{_name(a)}」未收")
                continue
            fr = form_record(a, wname, fsegs, e.get("Magazine"))
            if modes.get(id(a), CO_DEFAULT) != CO_DEFAULT:
                fr["co_behavior"] = modes[id(a)]
            forms[f"{kind}:{slug(_name(a))}"] = fr
        if any(not k.startswith("incarnon:") for k in forms):
            mk = main_kind(main, roots[0], forms)
            if mk:
                rec["main_form"] = mk
        if forms:
            rec["forms"] = dict(sorted(forms.items()))
        if not (segs or rec.get("charge_seconds") or forms):
            continue
        if key in out:
            report.append(f"{wname}：键 {key} 已由 {out[key]['wiki']} 占用，跳过")
            continue
        out[key] = rec
    return out, report


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wiki", required=True, type=Path)
    ap.add_argument("--out", type=Path, default=OUT)
    ap.add_argument("--report", type=Path)
    args = ap.parse_args(argv[1:])
    ws_all = json.loads(WS.read_text(encoding="utf-8"))
    recs, report = build(args.wiki, ws_all)
    out = {
        "_meta": {
            "source": "英文 wiki Module:Weapons/data（各攻击 Attacks）+ Condition Overload (Mechanic) "
            "Attack Catalog（范围段吃 CO / 不吃多重的出处），CC BY-NC-SA 3.0；默认主段按 weapons_stats（DE 导出）认",
            "wiki_snapshot": args.wiki.name,
            "generator": "scripts/build_weapon_attacks.py",
            "scope": "顶层 segments / main / charge_seconds 只描述默认主段（main_attack = 与 weapons_stats 主段一致的 "
            "wiki 攻击）；非默认形态收在 forms（键 <种类>:<wiki 攻击名>），只在用户显式选了该形态时整套替换主段与额外段；"
            "灵化形态只收带额外段的（主段面板在 incarnon_forms.json）；archgun 取大气条目；近战不收",
            "note": "段是独立命中实例，各吃同样的 MOD 乘区；段的暴击/触发取 wiki 该子攻击的值；takes_multishot "
            "只写有出处的 false（缺省 = 吃多重）；cluster 段带 count；lingering 段 merge_only",
        }
    }
    out.update(dict(sorted(recs.items())))
    args.out.write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    if args.report:
        args.report.write_text("\n".join(report) + "\n", encoding="utf-8")
    vals = list(recs.values())
    print(
        f"✓ {args.out.name}: {len(vals)} 把武器 / {sum(len(v.get('segments') or []) for v in vals)} 个额外段 / "
        f"{sum(len(v.get('forms') or {}) for v in vals)} 个非默认形态；提示 {len(report)} 条"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
