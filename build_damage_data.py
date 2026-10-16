# -*- coding: utf-8 -*-
"""构建伤害计算器的数据表。

产出（写入 core/data/）：
1. damage_faction.json —— 派系 × 伤害类型倍率（U36 抗性重构后：只有 ×1.5 / ×0.5 / ×1，
   弱点按派系而不是按血量类型）。来源 warframe.fandom.com「Damage/Overview Table」。
2. weapons_stats.json —— 武器基础数据（来自 WFCD warframe-items npm 包的
   Primary/Secondary/Melee 三张表，裁剪最小字段；中文名用插件自带的
   core/data/de/name_zh.json 按资产路径反查）。

用法：python build_damage_data.py   （需可访问 github/jsdelivr；或经 -x 代理）
      python build_damage_data.py --repair-weapons   （只对现有 weapons_stats.json 重放
      源头修正：物理对调残留 + MAIN_ATTACK_FIX 主段按攻击段取值；不联网、幂等）
"""

from __future__ import annotations

import json
import re
import sys
import urllib.request
from pathlib import Path

DATA = Path(__file__).resolve().parent / "core" / "data"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126.0"
PROXY = None  # 例如 "http://127.0.0.1:7897"
# 本地缓存目录（用 curl --proxy 先下好的文件放这里，脚本优先读缓存）
CACHE = Path.home() / "tmp" / "dmgsrc"


def _fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    if PROXY:
        req.set_proxy(PROXY.replace("http://", ""), "http")
        req.set_proxy(PROXY.replace("http://", ""), "https")
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode("utf-8", "ignore")


def _fetch_cached(url: str, cache_name: str) -> str:
    """优先读本地缓存（curl 下好放 CACHE 目录），没有才直连。"""
    local = CACHE / cache_name
    if local.exists() and local.stat().st_size > 1000:
        return local.read_text(encoding="utf-8", errors="ignore")
    return _fetch(url)


# ---------------------------------------------------------------------------
# 1) 派系倍率表
# ---------------------------------------------------------------------------
FACTIONS = [
    "Tenno",
    "Grineer",
    "Kuva Grineer",
    "Corpus",
    "Corpus Amalgam",
    "Infested",
    "Infested Deimos",
    "Orokin",
    "Sentient",
    "Narmer",
    "The Murmur",
    "Zariman",
    "Scaldra",
    "Techrot",
]

# 倍率表里会出现的伤害类型（物理 + 单元素 + 复合 + 特殊）
_KNOWN_TYPES = {
    "Impact",
    "Puncture",
    "Slash",
    "Cold",
    "Electricity",
    "Heat",
    "Toxin",
    "Blast",
    "Corrosive",
    "Gas",
    "Magnetic",
    "Radiation",
    "Viral",
    "Void",
    "True",
    "Tau",
    "Finisher",
}


def build_faction_table() -> dict:
    raw = _fetch_cached(
        "https://warframe.fandom.com/api.php?action=parse"
        "&page=Damage/Overview_Table&prop=wikitext&format=json&formatversion=2",
        "Overview_Table.json",
    )
    txt = json.loads(raw)["parse"]["wikitext"]

    # 表头列顺序（跳过第一列 Damage Type）
    heads = re.findall(
        r"skew\(50deg\)[^>]*>\s*(?:<[^>]+>\s*)*<span[^>]*>\s*\{\{D\|([^}]+)\}\}", txt
    )
    heads = [h.strip() for h in heads]
    if heads != FACTIONS:
        print("⚠️ 表头与预期不一致：", heads)

    table: dict[str, dict] = {}
    # {{D|X}} 模板里本身含 |，先把模板替换成纯名字再按 | 切格
    flat = re.sub(r"\{\{D\|([^}]+)\}\}", r"\1", txt)
    for block in flat.split("|-"):
        if "skew" in block:  # 表头
            continue
        cells = [c.strip() for c in block.split("|")]
        cells = [c for c in cells if c != "" or True][1:]  # 去掉行首空段
        # 找到伤害类型格（第一个命中已知类型的格；复合行带 <br />(A + B) 后缀）
        dtype = None
        for i, c in enumerate(cells):
            m = re.match(r"([A-Za-z ]+?)\s*(?:<br.*)?$", c)
            if m and m.group(1).strip() in _KNOWN_TYPES:
                dtype, start = m.group(1).strip(), i + 1
                break
        if not dtype:
            continue
        body = cells[start : start + len(heads)]
        if len(body) < len(heads):
            continue
        vals: dict[str, float] = {}
        for name, cell in zip(heads, body):
            if "seagreen" in cell:
                vals[name] = 1.5
            elif "indianred" in cell:
                vals[name] = 0.5
        table[dtype] = vals
    out = {
        "_meta": {
            "source": "warframe.fandom.com/wiki/Damage/Overview_Table",
            "retrieved": "2026-09-16",
            "rule": "U36 起：弱点/抗性按派系，+ = ×1.5，- = ×0.5，其余 ×1",
        },
        "factions": FACTIONS,
        "table": table,
    }
    (DATA / "damage_faction.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print(f"damage_faction.json：{len(table)} 种伤害类型")
    return table


# ---------------------------------------------------------------------------
# 2) 武器基础数据
# ---------------------------------------------------------------------------
KEEP = (
    "name",
    "uniqueName",
    "category",
    "masteryReq",
    "damage",
    "damageTypes",
    "criticalChance",
    "criticalMultiplier",
    "fireRate",
    "trigger",
    "multishot",
    "projectile",
    "disposition",
    "type",
    "slamAttack",
    # 持续 DPS 需要：magazineSize（弹匣）+ reloadTime（装填秒）
    "magazineSize",
    "reloadTime",
    "procChance",
    # 近战重击 / 连击 / 震地
    "heavyAttackDamage",
    "heavySlamAttack",
    "comboDuration",
    "slamAttack",
    "slamRadialDamage",
    "heavySlamRadialDamage",
    "slamRadius",
    "slideAttack",
    "windUp",
    "followThrough",
    "omegaAttenuation",
)


# 伤害类型键（用于把 attacks 里的 damage 归一成完整 dict）
_DMG_KEYS = (
    "impact",
    "puncture",
    "slash",
    "heat",
    "cold",
    "electricity",
    "toxin",
    "blast",
    "radiation",
    "gas",
    "magnetic",
    "viral",
    "corrosive",
    "void",
    "tau",
    "true",
)


def _collect_attacks(it: dict) -> list[dict]:
    """把 attacks 里每一段攻击判定都收下来（含伤害构成、暴击/触发、衰减、蓄力）。

    这些是「多段伤害」的原始依据：投掷命中与投掷爆炸是**两段独立判定**，
    伤害类型都可能不同，必须分别算。
    """
    out: list[dict] = []
    for a in it.get("attacks") or []:
        raw = a.get("damage") or {}
        dmg = {
            k: float(v) for k, v in raw.items() if isinstance(v, (int, float)) and k in _DMG_KEYS
        }
        if not dmg:
            continue
        rec = {
            "name": (a.get("name") or "").strip(),
            "damage": dmg,
            "total": sum(dmg.values()),
        }
        for key in (
            "crit_chance",
            "crit_mult",
            "status_chance",
            "speed",
            "shot_type",
            "charge_time",
            "shot_speed",
            "flight",
        ):
            if a.get(key) is not None:
                rec[key] = a[key]
        if a.get("falloff"):
            rec["falloff"] = a["falloff"]
        slam = a.get("slam") or {}
        if slam:
            rec["slam"] = slam
        out.append(rec)
    return out


def _fix_physical(it: dict) -> dict:
    """用 attacks 的伤害修正顶层 damage 的物理三类型。

    ⚠️ warframe-items 顶层 `damage` 的 **puncture / slash 大面积写反**：
    实测 184 件 Primary 里 109 件、Secondary 93 件、Melee 174 件与
    `attacks[].damage` 互为对调；`attacks` 的数值才与游戏内面板一致
    （例：Xoris 面板「穿刺 40.8 / 切割 55.2」，顶层写的是 55.2/40.8）。
    元素键（heat/cold/…）顶层更全，因此只覆盖物理三项。
    """
    dmg = dict(it.get("damage") or {})
    _fix_physical_from(dmg, it.get("attacks") or [])
    _retotal(dmg)
    return dmg


_PHYS = ("impact", "puncture", "slash")


def _ref_attack(ats: list) -> dict:
    """顶层面板对应的攻击段：名为 Normal Attack 的，否则第一段。"""
    normal = next((a for a in ats if (a.get("name") or "") == "Normal Attack"), None)
    return (normal or (ats[0] if ats else {})) or {}


def _fix_physical_from(dmg: dict, ats: list) -> list[str]:
    """就地修 ``dmg`` 的物理三类型（参照段 = ``_ref_attack``），返回改动过的键（不重算 total）。

    ★ 对调残留（2026-10-10 b5 批）：顶层 puncture / slash 写反时，参照段往往**只有其中
    一项**（Tetra 段 = 冲击 6.4 + 穿刺 25.6，顶层 = 冲击 6.4 + 切割 25.6）。修前只把段里有的
    键覆盖回顶层，段里没有的那项（切割 25.6）原样留下 ⇒ 同一份伤害记两次（Tetra 57.6，
    wiki 32，+80%）。规则：参照段带了物理键时，顶层里**段没有**的物理键清零。
    weapons_stats 实测命中 22 把（Tetra / Bolto / Vesper 77 / Dorrclave / Sydon …），逐把对
    wiki Module:Weapons/data 该武器 AttackIndex 1 的 Damage 一致。
    """
    src = _ref_attack(ats).get("damage") or {}
    changed: list[str] = []
    for k in _PHYS:
        v = src.get(k)
        if isinstance(v, (int, float)) and abs(float(v) - float(dmg.get(k) or 0)) > 0.05:
            dmg[k] = float(v)
            changed.append(k)
    if any(float(src.get(k) or 0) > 0 for k in _PHYS):
        for k in _PHYS:
            if float(src.get(k) or 0) <= 0 and float(dmg.get(k) or 0) > 0:
                dmg[k] = 0
                changed.append(k)
    return changed


def _retotal(dmg: dict) -> None:
    # ⚠️ warframe-items 顶层 `damage.total` 常与各成分之和**不一致**
    # （实测 581 把里 71 把：弓类 total 是蓄力值而成分是未蓄力值，差 2 倍；
    #  爆炸类 total 把范围段并了进来）。total 必须自洽 = Σ成分，
    # 蓄力/范围段由 attacks 段单独承担 —— 否则伤害链按 total 分配比例会算错。
    _sk = {"total", "cinematic", "shieldDrain", "healthDrain", "energyDrain"}
    _s = sum(float(v) for kk, v in dmg.items() if isinstance(v, (int, float)) and kk not in _sk)
    if _s > 0:
        dmg["total"] = round(_s, 4)


# ---------------------------------------------------------------------------
# 主段按攻击段取值的修正表（2026-10-10 b5 批）
# ---------------------------------------------------------------------------
# warframe-items 顶层的 criticalChance / criticalMultiplier / procChance / fireRate 不保证
# 来自同一段攻击：多段武器常是「伤害取 A 段、暴击取 B 段、触发取 C 段」。我方主段 = 顶层
# damage 对应的那一段（第四批起 Nataruk = 快速射击），其余字段必须取**同一段**的值。
# 键 = 小写 uniqueName；values = 该段面板（wiki Module:Weapons/data 的 Attacks 原值）；
# attack = 我方 attacks 里同一段的名字（生成时核对 WFCD 该段与 values 一致，不一致打印
# 「!!」，check_attack=False 的是 WFCD 段本身也错的）。damage 写全该段成分（其余伤害键清零）。
# 可选键（b6 批起）：attacks = 整表替换攻击段（WFCD 段本身是旧值时）；segment_patch = 按段名
# 覆盖个别段的字段；throwDamage = 同步旧字段；values 里 multishot / ammoCost 不在攻击段里核对。


def _seg(name: str, dmg: dict, cc: float, cm: float, sc: float, speed, **extra) -> dict:
    """按 weapons_stats.attacks 的口径（暴击 / 触发写百分数）拼一个攻击段。"""
    d = {k: float(v) for k, v in dmg.items()}
    out = {
        "name": name,
        "damage": d,
        "total": float(sum(d.values())),
        "crit_chance": round(cc * 100, 4),
        "crit_mult": cm,
        "status_chance": round(sc * 100, 4),
    }
    if speed is not None:
        out["speed"] = speed
    out.update(extra)
    return out


MAIN_ATTACK_FIX: dict[str, dict] = {
    "/lotus/weapons/tenno/bows/omicrus/omicrusplayerwep": {
        "name": "Nataruk",
        "attack": "Quick Shot",
        "values": {"procChance": 0.3, "magazineSize": 1, "reloadTime": 0.6},
        "source": "wiki Module:Weapons/data/primary Nataruk AttackIndex 1 Quick Shot "
        "StatusChance 0.3（顶层 0.5 = Charged/Perfect Shot）；wfsim nataruk.yaml status_chance 0.3。"
        "b7 批：弹匣 1 / 上弦 0.6s = wiki primary:8013 Magazine 1、:8018 Reload 0.6，wiki Nataruk "
        "§Notes「Despite listing 0.1s reload time (0.05s rounded up), the reload time is actually "
        "0.6s」（WFCD 无弹匣、装填 0.05）；wfsim nataruk.yaml magazine 1 / reload_seconds 0.6",
    },
    "/lotus/weapons/tenno/bows/daxduviriasymetricalbow/daxduviriasymmetricallongbowplayerweapon": {
        "name": "Cinta",
        "attack": "Quick Shot",
        "values": {"procChance": 0.3},
        "source": "wiki primary Cinta AttackIndex 1 Quick Shot StatusChance 0.3（顶层 0.32 = "
        "Charged Shot）；wfsim cinta.yaml status_chance 0.3",
    },
    "/lotus/weapons/tenno/longguns/tnwisprifle/tnwisprifle": {
        "name": "Fulmin",
        "attack": "Full Auto Mode",
        "values": {
            "criticalChance": 0.28,
            "criticalMultiplier": 2.4,
            "procChance": 0.1,
            "fireRate": 9.33,
        },
        "source": "wiki primary Fulmin AttackIndex 1 Full Auto Mode（伤害 33 已是该段；暴击/触发/"
        "射速原取 Semi-Auto Mode 0.3/2.2/0.16/2.17）；wfsim fulmin.yaml（default_form 全自动）",
    },
    "/lotus/weapons/tenno/longguns/primefulmin/primefulmin": {
        "name": "Fulmin Prime",
        "attack": "Full Auto Mode",
        "values": {
            "criticalChance": 0.34,
            "criticalMultiplier": 2.4,
            "procChance": 0.1,
            "fireRate": 9.33,
        },
        "source": "wiki primary Fulmin Prime AttackIndex 1 Full Auto Mode（原暴击/触发/射速取 "
        "Semi-Auto 0.3/2.2/0.22/2.17）；wfsim fulmin_prime.yaml",
    },
    "/lotus/weapons/clantech/energy/railgun": {
        "name": "Lanka",
        "attack": "Charged Shot",
        "values": {"criticalChance": 0.25},
        "source": "wiki primary Lanka AttackIndex 2 Charged Shot CritChance 0.25（伤害 525 已是该段；"
        "原暴击 0.2 = Partially Charged Shot）；wfsim lanka.yaml crit_chance 0.25",
    },
    "/lotus/weapons/tenno/pistols/tnwraithesidearm/tnwraithesidearmweapon": {
        "name": "Epitaph",
        "attack": "Charged Shot",
        "values": {"criticalChance": 0.48, "criticalMultiplier": 2.6},
        # b8 批：WFCD「Uncharged AoE」暴击 0 / 1x，wiki secondary:3539 起该段 CritChance 0.02 /
        # CritMultiplier 1.2（与 Uncharged Direct Hit 同；wfsim epitaph_uncharged.yaml radial 同值）
        "segment_patch": {"Uncharged AoE": {"crit_chance": 2, "crit_mult": 1.2}},
        "source": "wiki secondary Epitaph AttackIndex 1 Charged Shot 0.48/2.6（伤害 300、触发 0.04 "
        "已是该段；原暴击 0.02/1.2 = Uncharged Direct Hit）；wfsim epitaph.yaml",
    },
    "/lotus/weapons/tenno/pistols/primeepitaph/primeepitaphsidearmweapon": {
        "name": "Epitaph Prime",
        "attack": "Charged Shot",
        "values": {"criticalChance": 0.5, "criticalMultiplier": 3.0},
        # b8 批：WFCD「Uncharged AoE」暴击 0 / 1x，wiki secondary:3626 起该段 0.04 / 1.8
        "segment_patch": {"Uncharged AoE": {"crit_chance": 4, "crit_mult": 1.8}},
        "source": "wiki secondary Epitaph Prime AttackIndex 1 Charged Shot 0.5/3（原 0.04/1.8 = "
        "Uncharged Direct Hit）；wfsim epitaph_prime.yaml",
    },
    "/lotus/weapons/tenno/pistols/automatichandcrossbow/autocrossbow": {
        "name": "Ballistica",
        "attack": "Charged Shot",
        "values": {"criticalChance": 0.15},
        "source": "wiki secondary Ballistica AttackIndex 2 Charged Shot CritChance 0.15（伤害 100 "
        "已是该段；原 0.025 = Burst Shot）；wfsim ballistica.yaml",
    },
    "/lotus/weapons/syndicates/redveil/pistols/rvballistica": {
        "name": "Rakta Ballistica",
        "attack": "Charged Shot",
        "values": {"criticalChance": 0.2},
        "source": "wiki secondary Rakta Ballistica AttackIndex 2 Charged Shot 0.2（原 0.05 = Burst "
        "Shot）；wfsim rakta_ballistica.yaml",
    },
    "/lotus/weapons/grineer/longguns/grineersawbladegun/sawbladegun": {
        "name": "Miter",
        "attack": "Uncharged Shot",
        "values": {"procChance": 0.2},
        "source": "wiki primary Miter AttackIndex 1 Uncharged Shot StatusChance 0.2（伤害 100、"
        "暴击 0.05 已是该段；原 0.5 = Charged Shot）",
    },
    "/lotus/weapons/grineer/longguns/grineerflakcannon/flakcannon": {
        "name": "Drakgoon",
        "attack": "Uncharged Shot",
        "values": {"procChance": 0.063},
        "source": "wiki primary Drakgoon AttackIndex 1 Uncharged Shot StatusChance 0.063（原 0.069 = "
        "Charged Shot）；wfsim drakgoon_uncharged.yaml status_chance 0.063",
    },
    "/lotus/weapons/infested/bow/infcernosbow/infcernos": {
        "name": "Mutalist Cernos",
        "attack": "Uncharged Shot",
        "values": {"damage": {"impact": 184.5, "puncture": 10.25, "slash": 10.25}},
        "source": "wiki primary Mutalist Cernos AttackIndex 1 Uncharged Shot = 205（顶层多出的毒素 5 "
        "是 AttackIndex 3 Toxin Cloud，另有 attacks 段）；wfsim mutalist_cernos_uncharged.yaml",
    },
    "/lotus/weapons/operator/melee/driftertazer/driftertazerplayerwep": {
        "name": "Rumblejack",
        "attack": "Normal Attack",
        "values": {"criticalChance": 0.13},
        "source": "wiki melee Rumblejack AttackIndex 1 Normal Attack CritChance 0.13（原 0.14 = "
        "Slam Attack）；wfsim rumblejack.yaml crit_chance 0.13",
    },
    "/lotus/weapons/corpus/pistols/crpairpistol/crpairpistolarray": {
        "name": "Sonicor",
        "attack": "Projectile Impact",
        "values": {"criticalChance": 0.0, "criticalMultiplier": 1.0},
        "source": "wiki secondary Sonicor AttackIndex 1 Projectile Impact 0/1（原 0.1/2 = Explosion，"
        "Explosion 另有 attacks 段）；wfsim sonicor.yaml crit_chance 0",
    },
    "/lotus/weapons/grineer/pistols/grineercrossbow/grineergoogun": {
        "name": "Stug",
        "attack": "Blob Embed",
        "check_attack": False,
        "values": {"procChance": 0.1},
        "source": "wiki secondary Stug AttackIndex 1 Blob Embed StatusChance 0.1；wfsim stug.yaml "
        "status_chance 0.1（WFCD 顶层与该段同写 0，属上游缺值）",
    },
    "/lotus/weapons/tenno/melee/swords/darksword/darksworddaggerhybridweapon": {
        "name": "Dark Split-Sword",
        "attack": "Normal Attack",
        "values": {
            "damage": {"puncture": 78.0, "slash": 52.0, "radiation": 100.0},
            "criticalChance": 0.15,
            "criticalMultiplier": 2.0,
            "procChance": 0.25,
            "fireRate": 0.917,
            "heavyAttackDamage": 1380,
            "slamAttack": 460,
            "heavySlamAttack": 690,
            "slamRadius": 8,
            "slideAttack": 460,
            "windUp": 1.1,
            "followThrough": 0.6,
            "comboDuration": 5,
        },
        "attacks": [
            _seg(
                "Normal Attack",
                {"puncture": 78, "slash": 52, "radiation": 100},
                0.15,
                2.0,
                0.25,
                0.917,
            ),
            _seg(
                "Slam Attack",
                {"impact": 460},
                0.15,
                2.0,
                0.1,
                0.917,
                shot_type="AoE",
                falloff={"start": 0, "end": 8, "reduction": 0.5},
                forced_procs=["impact"],
            ),
            _seg(
                "Heavy Slam Attack",
                {"blast": 690},
                0.15,
                2.0,
                0.1,
                0.917,
                shot_type="AoE",
                falloff={"start": 0, "end": 9, "reduction": 0.3},
            ),
            _seg(
                "Dual Swords Normal Attack",
                {"puncture": 56, "slash": 28, "radiation": 32},
                0.25,
                2.5,
                0.15,
                1.17,
            ),
            _seg(
                "Dual Swords Slam Attack",
                {"impact": 232},
                0.25,
                2.5,
                0.1,
                1.17,
                shot_type="AoE",
                falloff={"start": 0, "end": 8, "reduction": 0.5},
                forced_procs=["impact"],
            ),
            _seg(
                "Dual Swords Heavy Slam Attack",
                {"blast": 462},
                0.25,
                2.5,
                0.1,
                1.17,
                shot_type="AoE",
                falloff={"start": 0, "end": 9, "reduction": 0.3},
            ),
        ],
        "source": "b7 批（维护者定）：默认形态 = 重剑 —— wiki melee「Dark Split-Sword (Heavy Blade)」"
        "melee:2856 AttackIndex 1 Normal Attack（穿刺 78 / 切割 52 / 辐射 100 = 230，0.15 / 2 / 0.25，"
        "0.917）、Slam 460 / Heavy Slam 690、HeavyAttack 1380、SlideAttack 460、WindUp 1.1、"
        "FollowThrough 0.6；wfsim dark_split_sword.yaml（default_form = 重剑，230）。双剑形态 = wiki "
        "melee:2786「Dark Split-Sword (Dual Swords)」（116 / 0.25 / 2.5 / 0.15 / 1.17、Slam 232、"
        "Heavy Slam 462）只作「其它段」展示（形态关键词体系只覆盖枪械，wfsim 也只收重剑）。"
        "b5 批曾取双剑段为主段",
    },
    # ---- b7 批（2026-10-10，维护者定）：主段 = wiki 面板口径 ----
    "/lotus/weapons/cephalon/primary/cephprimary/cephprimary": {
        "name": "Simulor",
        "attack": "Orb Launch",
        "values": {"damage": {"impact": 0.0}},
        "attacks": [
            _seg(
                "Orb Launch",
                {"impact": 0},
                0.12,
                2.0,
                0.3,
                3.0,
                shot_type="Projectile",
                shot_speed=80,
            ),
            _seg("Orb Merging Damage", {"magnetic": 100}, 0.12, 2.0, 0.3, 3.0, shot_type="AoE"),
            _seg(
                "Orb Explosion",
                {"magnetic": 200},
                0.12,
                2.0,
                0.3,
                3.0,
                shot_type="AoE",
                falloff={"start": 0, "end": 5, "reduction": 0.6},
                forced_procs=["electricity"],
            ),
        ],
        "source": "wiki primary:10757 Simulor（_TooltipAttackDisplay = 3）：AttackIndex 1 Orb Launch "
        "直击 冲击 0（0.12 / 2 / 0.3）、2 Orb Merging Damage 磁力 100（球体融合，另一种打法）、"
        "3 Orb Explosion 磁力 200（范围，0.6 衰减、强制电击）。主段 = 直击 0，爆炸由 weapon_attacks "
        "范围段计入含段合计 / DPS。修前主段 = Orb Merging 100。wfsim simulor.yaml damage impact 0 + "
        "radial 200",
    },
    "/lotus/weapons/syndicates/cephalonsuda/longguns/cssimulor": {
        "name": "Synoid Simulor",
        "attack": "Orb Launch",
        "values": {
            "damage": {"impact": 0.0},
            "criticalChance": 0.0,
            "criticalMultiplier": 1.0,
            "procChance": 0.0,
        },
        "attacks": [
            _seg(
                "Orb Launch",
                {"impact": 0},
                0.0,
                1.0,
                0.0,
                3.33,
                shot_type="Projectile",
            ),
            _seg(
                "Orb Merging Damage",
                {"magnetic": 125},
                0.14,
                2.0,
                0.35,
                3.33,
                shot_type="AoE",
                falloff={"start": 0, "end": 4, "reduction": 0},
            ),
            _seg(
                "Orb Explosion",
                {"magnetic": 240},
                0.14,
                2.0,
                0.35,
                3.33,
                shot_type="AoE",
                falloff={"start": 0, "end": 5, "reduction": 1},
                forced_procs=["electricity"],
            ),
            _seg(
                "Fully Stacked Orb Singularity",
                {"magnetic": 50},
                0.0,
                1.0,
                0.0,
                4.0,
                shot_type="DoT",
                falloff={"start": 0, "end": 5, "reduction": 0},
            ),
        ],
        "source": "wiki primary:12074 Synoid Simulor（_TooltipAttackDisplay = 3）：AttackIndex 1 Orb "
        "Launch 直击 0（0 / 1 / 0）、2 Orb Merging Damage 125、3 Orb Explosion 240（范围，强制电击）、"
        "4 Fully Stacked Orb Singularity 50 / 0.25s DoT。修前主段 = Orb Merging 125（14% / 2 / 35%）。"
        "wfsim synoid_simulor.yaml 直击 0 / 1 / 0 + radial 240",
    },
    "/lotus/weapons/tenno/throwingweapons/u18throwingknives/u18throwingknives": {
        "name": "Talons",
        "attack": "Embedded Detonation",
        "values": {"damage": {"blast": 250.0}, "procChance": 0.1},
        "attacks": [
            _seg(
                "Mid-Flight Detonation",
                {"blast": 120},
                0.22,
                2.0,
                0.26,
                3.33,
                shot_type="AoE",
                shot_speed=60,
                falloff={"start": 0, "end": 4, "reduction": 0.5},
            ),
            _seg(
                "Embedded Detonation",
                {"blast": 250},
                0.22,
                2.0,
                0.1,
                3.33,
                shot_type="AoE",
                shot_speed=60,
                falloff={"start": 0, "end": 4, "reduction": 0.5},
                forced_procs=["puncture", "ragdoll"],
            ),
        ],
        "source": "wiki secondary:8150 Talons（_TooltipAttackDisplay = 2）：AttackIndex 2 Embedded "
        "Detonation 爆炸 250（0.22 / 2 / 0.1，4m 衰减 0.5，强制穿刺 + 布娃娃）= 面板显示段；"
        "AttackIndex 1 Mid-Flight Detonation 120 / 0.26 改为其它段。修前主段 = Mid-Flight（bf71990 "
        "「两者都对」时保留了 WFCD 主段）。wfsim talons.yaml 250 / 0.1",
    },
    # ---- b6 批（2026-10-10）：WFCD 滞后 / 混段，按 wiki Module:Weapons/data 原值 ----
    "/lotus/weapons/infested/pistols/infesteddartpistol/infesteddartpistol": {
        "name": "Tysis",
        "attack": "Normal Attack",
        "values": {"damage": {"impact": 9.0, "puncture": 23.0, "slash": 17.0}},
        "attacks": [
            _seg(
                "Normal Attack",
                {"impact": 9, "puncture": 23, "slash": 17},
                0.03,
                1.5,
                0.5,
                2.5,
                shot_type="Projectile",
                shot_speed=75,
                flight=75,
            ),
            _seg("Corrosive DoT", {"corrosive": 27}, 0.03, 1.5, 0.5, None, shot_type="DoT"),
        ],
        "source": "wiki secondary Tysis AttackIndex 1 Normal Attack（冲击 9 / 穿刺 23 / 切割 17 = 49）"
        "+ AttackIndex 2 Corrosive DoT（腐蚀 27，EffectDuration 2）；wiki Tysis §Notes「81 腐蚀分 3 跳、"
        "每跳 27、2 秒内」「arsenal 把两段并成一段是错的」。修前主段 103 = 49 + 腐蚀 27 + 物理键取了 "
        "Coda 段；WFCD attacks 是 Coda Tysis 的数值（76 / 59）。wfsim tysis.yaml 49，DoT 列 unmodeled",
    },
    "/lotus/weapons/infested/infestedlich/pistols/codatysis": {
        "name": "Coda Tysis",
        "attack": "Normal Attack",
        "values": {"damage": {"impact": 18.0, "puncture": 32.0, "slash": 26.0}},
        "attacks": [
            _seg(
                "Normal Attack",
                {"impact": 18, "puncture": 32, "slash": 26},
                0.13,
                2.0,
                0.5,
                2.5,
                shot_type="Projectile",
            ),
            _seg("Corrosive DoT", {"corrosive": 59}, 0.13, 2.0, 0.5, None, shot_type="DoT"),
        ],
        "source": "wiki secondary Coda Tysis AttackIndex 1 Normal Attack（18 / 32 / 26 = 76）+ "
        "AttackIndex 2 Corrosive DoT（59）；wiki Coda Tysis §Notes「codex 的 135 是两段之和，实为 "
        "76 与 59 两段」。修前主段 135、WFCD 无 attacks。wfsim coda_tysis.yaml 76",
    },
    "/lotus/weapons/corpus/longguns/crparsniper/crparsniperrifle": {
        "name": "Ambassador",
        "attack": "Auto",
        "values": {"damage": {"electricity": 29.0}},
        "segment_patch": {"Auto": {"damage": {"electricity": 29.0}, "total": 29.0}},
        "source": "wiki primary Ambassador AttackIndex 1 Auto（电击 29）；wiki Ambassador 更新 39"
        "「Increased damage on auto fire from 24 to 29」——WFCD 仍是改前 24。wfsim ambassador.yaml 29",
    },
    "/lotus/weapons/infested/melee/infboomerang/infboomerangweapon": {
        "name": "Pathocyst",
        "attack": "Normal Attack",
        "values": {"damage": {"impact": 57.0, "puncture": 55.0, "slash": 61.0, "viral": 89.0}},
        "attacks": [
            _seg(
                "Normal Attack",
                {"impact": 57, "puncture": 55, "slash": 61, "viral": 89},
                0.15,
                2.1,
                0.3,
                0.667,
            ),
            _seg(
                "Throw",
                {"impact": 49, "puncture": 43, "slash": 78, "viral": 118},
                0.17,
                2.3,
                0.33,
                0.667,
                shot_type="Thrown",
                shot_speed=30,
                flight=30,
            ),
            _seg(
                "Throw Bounce Explosion",
                {"viral": 393},
                0.17,
                2.3,
                0.33,
                0.667,
                shot_type="AoE",
                falloff={"start": 0, "end": 4.9, "reduction": 0.5},
            ),
            _seg(
                "Throw Recall Explosion",
                {"viral": 786},
                0.17,
                2.3,
                0.33,
                0.667,
                shot_type="AoE",
                falloff={"start": 0, "end": 4.9, "reduction": 0},
            ),
            _seg(
                "Charged Throw",
                {"impact": 127, "puncture": 121, "slash": 135, "viral": 193},
                0.21,
                2.5,
                0.35,
                0.833,
                shot_type="Thrown",
                charge_time=1.192,
                shot_speed=30,
                flight=30,
            ),
            _seg(
                "Charged Throw Bounce Explosion",
                {"viral": 786},
                0.21,
                2.5,
                0.35,
                0.833,
                shot_type="AoE",
                charge_time=1.192,
                falloff={"start": 0, "end": 4.9, "reduction": 0.5},
            ),
            _seg(
                "Charged Throw Recall Explosion",
                {"viral": 1572},
                0.21,
                2.5,
                0.35,
                0.833,
                shot_type="AoE",
                charge_time=1.192,
                falloff={"start": 0, "end": 4.9, "reduction": 0},
            ),
            _seg(
                "Slam Attack",
                {"viral": 524},
                0.15,
                2.1,
                0.3,
                0.667,
                shot_type="AoE",
                falloff={"start": 0, "end": 5, "reduction": 0.5},
            ),
            _seg(
                "Heavy Slam Attack",
                {"viral": 786},
                0.15,
                2.1,
                0.3,
                0.667,
                shot_type="AoE",
                falloff={"start": 0, "end": 6, "reduction": 0.3},
            ),
        ],
        "throwDamage": {"impact": 49.0, "puncture": 43.0, "slash": 78.0, "viral": 118.0},
        "source": "wiki melee Pathocyst AttackIndex 1–9（Normal Attack 57/55/61/89 = 262，Throw 288 / "
        "爆炸 393、786，Charged Throw 576 / 786、1572，Slam 524 / 786）；DE 导出顶层同为 262（WFCD "
        "Melee.json damage.total 262），WFCD attacks 是旧版（270 / 297 / 405 …），修前主段按旧段取了 "
        "物理键 + 毒化 92。wfsim pathocyst.yaml 262",
    },
    "/lotus/weapons/grineer/longguns/grnspark/grnsparkrifle": {
        "name": "Kohm",
        "attack": "Fully Spooled",
        "values": {"procChance": 0.0625, "multishot": 12.0, "ammoCost": 4.0},
        "source": "wiki primary Kohm AttackIndex 2 Fully Spooled（Multishot 12 / StatusChance 0.0625 / "
        "AmmoCost 4）；wiki Kohm「满转 12 弹片、每发耗 4 弹」「首发触发按弹片数均分」⇒ 0.75 / 12。"
        "修前 WFCD 9 弹片 × 0.0833（= 0.75 / 9）、每发耗 1 弹。wfsim kohm.yaml 同 wiki",
    },
    "/lotus/weapons/grineer/kuvalich/longguns/kohm/kuvakohm": {
        "name": "Kuva Kohm",
        "attack": "Fully Spooled",
        "values": {"procChance": 0.075, "multishot": 12.0, "ammoCost": 4.0},
        "source": "wiki primary Kuva Kohm AttackIndex 2 Fully Spooled（Multishot 12 / StatusChance "
        "0.075 / AmmoCost 4）= 0.9 / 12。修前 WFCD 9 弹片 × 0.1。wfsim kuva_kohm.yaml 同 wiki",
    },
    "/lotus/weapons/tenno/pistols/conclaveleverpistol/conclaveleverpistol": {
        "name": "Zylok",
        "attack": "Normal Attack",
        "values": {"fireRate": 1.8},
        "source": "wiki secondary Zylok AttackIndex 1 Normal Attack FireRate 1.8（WFCD 顶层 1.5，"
        "其 attacks 段同为 1.8）；wfsim zylok.yaml 1.8。双发扳机（Duplex）一按两发未建模（wfsim 同样"
        "未建模），按面板每秒 1.8 发计",
    },
    "/lotus/weapons/tenno/pistols/primezylok/zylokprimepistol": {
        "name": "Zylok Prime",
        "attack": "Normal Attack",
        "values": {"fireRate": 1.8},
        "source": "wiki secondary Zylok Prime AttackIndex 1 Normal Attack FireRate 1.8（WFCD 顶层 "
        "1.5）；wfsim zylok_prime.yaml 1.8",
    },
}

# ---------------------------------------------------------------------------
# 射击节奏（2026-10-10 b6 批）：点射 / 蓄力的「有效射速」所需字段
# ---------------------------------------------------------------------------
# wiki「Fire Rate」§Fire Rate and Burst Weapons 原文：Fire Rate =「Number of bursts per second if
# Burst Delay is 0 seconds … This is the "Fire Rate" value listed in the arsenal」；
#   Effective Fire Rate = Burst Count / (1/Fire Rate + (Burst Count − 1) × Burst Delay)
# 「Burst Delay is not affected by net negative Fire Rate bonuses」。wfsim 同式
# （engine/src/model/weapon.rs:490-500、build/loadout/resolve.rs:1736-1739、fight/melee.rs:541-547）。
# WFCD 顶层 fireRate 对点射武器是 N / (1/FR + N × D)（多算一次发间延迟：Burston 3/(0.2+3×0.061)
# = 7.83，wiki 式 9.32），且各类别口径不一（空战 / 守护武器写的是面板值）⇒ 一律改存 wiki 面板
# 值 + burstCount / burstDelay，由计算器换算。值 = (名字, 面板 FireRate, BurstCount, BurstDelay,
# 出处)，出处 = wiki Module:Weapons/data/<模块>（2026-10-10 抓取）该攻击段起始行；逐把与 wfsim
# attack.burst / fire_rate 一致。
BURST_FIX: dict[str, tuple] = {
    "/lotus/weapons/tenno/pistols/sapientpistol/sapientpistol": (
        "Akarius",
        4.33,
        2,
        0.12,
        "secondary:213 Rocket Impact",
    ),
    "/lotus/weapons/tenno/pistols/primeakarius/primeakariusweapon": (
        "Akarius Prime",
        3.667,
        2,
        0.12,
        "secondary:285 Rocket Impact",
    ),
    "/lotus/weapons/tenno/pistols/tigrisredeemersetpistol/tnobladedpistols": (
        "Akjagara",
        5.0,
        2,
        0.0,
        "secondary:550 Normal Attack",
    ),
    "/lotus/weapons/tenno/pistols/primeakjagara/akjagaraprime": (
        "Akjagara Prime",
        5.0,
        2,
        0.0,
        "secondary:598 Normal Attack",
    ),
    "/lotus/weapons/corpus/longguns/crpsentexperimentrifle/crpsentexperimentrifle": (
        "Battacor",
        2.5,
        2,
        0.08,
        "primary:1438 Normal Attack",
    ),
    "/lotus/weapons/tenno/rifle/burstrifle": (
        "Burston",
        5.0,
        3,
        0.061,
        "primary:2231 Normal Attack",
    ),
    "/lotus/weapons/tenno/longguns/primeburston/primeburston": (
        "Burston Prime",
        5.0,
        3,
        0.04,
        "primary:2320 Normal Attack",
    ),
    "/lotus/weapons/infested/infestedlich/longguns/codahema": (
        "Coda Hema",
        5.33,
        3,
        0.1,
        "primary:3087 Normal Attack",
    ),
    "/lotus/weapons/tenno/archwing/primary/archburstgun/archburstgun": (
        "Cyngas",
        6.67,
        3,
        0.06,
        "archwing:1091 Cyngas (Atmosphere) Normal Attack",
    ),
    "/lotus/weapons/tenno/longguns/dexthethird/dexthethird": (
        "Dex Sybaris",
        3.33,
        2,
        0.09,
        "primary:3748 Normal Attack",
    ),
    "/lotus/weapons/grineer/longguns/grnharpoongun/grnharpoongun": (
        "Harpak",
        5.0,
        3,
        0.1,
        "primary:5101 Normal Attack",
    ),
    "/lotus/types/friendly/pets/moapets/moapetcomponents/swarmerweapon": (
        "Helstrum",
        0.25,
        4,
        0.1,
        "companion:475 Normal Attack",
    ),
    "/lotus/weapons/infested/longguns/infwfaccompanyingpri/infestedburstrifle": (
        "Hema",
        5.0,
        3,
        0.1,
        "primary:5221 Normal Attack",
    ),
    "/lotus/weapons/tenno/longguns/gunbrella/shrinemaidengunbrellaweapon": (
        "Higasa",
        2.5,
        5,
        0.1,
        "primary:5269 Auto",
    ),
    "/lotus/weapons/grineer/longguns/burstrifle/grnburstrifle": (
        "Hind",
        5.0,
        5,
        0.12,
        "primary:5329 Burst Mode",
    ),
    "/lotus/weapons/grineer/grineerpistol/grnheavypistol": (
        "Kraken",
        2.83,
        2,
        0.05,
        "secondary:4719 Normal Attack",
    ),
    "/lotus/weapons/grineer/kuvalich/longguns/hind/kuvahind": (
        "Kuva Hind",
        5.0,
        5,
        0.07,
        "primary:6151 Burst",
    ),
    "/lotus/weapons/grineer/kuvalich/secondaries/kraken/kuvakraken": (
        "Kuva Kraken",
        2.83,
        3,
        0.03,
        "secondary:4982 Normal Attack",
    ),
    "/lotus/weapons/tenno/archwing/primary/thanotechgrenadelaunch/thanotechgrenadelauncher": (
        "Morgha",
        3.0,
        2,
        0.0,
        "archwing:2762 Morgha (Atmosphere) Auto-Burst",
    ),
    "/lotus/types/friendly/pets/moapets/moapetcomponents/hextraweapon": (
        "Multron",
        2.5,
        3,
        0.4,
        "companion:590 Projectile Impact",
    ),
    "/lotus/weapons/infested/longguns/quantafullyinfested/infquantarifle": (
        "Paracyst",
        8.33,
        3,
        0.05,
        "primary:8562 Normal Attack",
    ),
    "/lotus/weapons/tenno/longguns/tnquadsniper/tnquadsniper": (
        "Perigale",
        2.0,
        4,
        0.04,
        "primary:8884 Normal Attack",
    ),
    "/lotus/weapons/grineer/longguns/grnfourbarrelrifle/grnfourbarrelrifleweapon": (
        "Quartakk",
        1.58,
        4,
        0.0,
        "primary:10000 Burst-Fire",
    ),
    "/lotus/weapons/thanotech/thanopistol/thanotechpistol": (
        "Sepulcrum",
        1.83,
        2,
        0.0,
        "secondary:7307 Primary-Fire",
    ),
    "/lotus/weapons/tenno/pistol/burstpistol": (
        "Sicarus",
        3.5,
        3,
        0.04,
        "secondary:7410 Normal Attack",
    ),
    "/lotus/weapons/tenno/pistols/primesicarus/primesicaruspistol": (
        "Sicarus Prime",
        5.0,
        3,
        0.04,
        "secondary:7480 Normal Attack",
    ),
    "/lotus/weapons/tenno/longguns/tnoleveraction/tnoleveractionrifle": (
        "Sybaris",
        3.33,
        2,
        0.101,
        "primary:11888 Normal Attack",
    ),
    "/lotus/weapons/tenno/longguns/primesybaris/primesybarisrifle": (
        "Sybaris Prime",
        3.33,
        2,
        0.062,
        "primary:11959 Normal Attack",
    ),
    "/lotus/weapons/tenno/longguns/drakerifle/drakerifle": (
        "Tiberon",
        6.67,
        3,
        0.06,
        "primary:12985 Normal Attack",
    ),
    "/lotus/weapons/tenno/longguns/primetiberon/primetiberonrifle": (
        "Tiberon Prime",
        5.0,
        3,
        0.08,
        "primary:13034 Burst",
    ),
}

# 蓄力节奏：wiki「Fire Rate」§Fire Rate and Charge Time 三条式子——
#   一般蓄力武器 1 / (蓄力时间 + 1/射速)（「with the exception of bows, Epitaph, and Lanka」）；
#   弓 1 / (蓄力时间 + 装填时间)（弹匣 1，每发后装填）；Lanka 1 / 蓄力时间（「does not have a delay
#   between charged shots」）。下表 = 不付「1/射速」那一项的（chargeCadence = "draw"）：wiki 模块
#   Class = "Bow" 的全部 + Lanka。Epitaph / Epitaph Prime（b7 批移出）：wiki 只说它是一般式的例外、
#   没给公式 ⇒ 按 wfsim：非弓类一律 DrawThenRate = 蓄力 + 1/射速（engine/src/data/weapons/panel.rs:
#   510-515、fight/melee.rs:530-540；Epitaph 无实测）；b6 批按弓式处理属推断。
#   ★ b10 批（视频逐帧实测）定案：满蓄力 = 一般式 蓄力 + 1/射速，且不装填（见下方 NO_RELOAD_FIX；
#   b8 批的 max(蓄力, 1/射速) 推断已被实测否定）。
#   弓的「未蓄力」默认主段（蓄力 0）：wiki 未单列 ⇒ 按 wfsim「只付上弦」（*_uncharged.yaml
#   charge_seconds 0.0 + DrawOnly，M16 只有方案、未出结果），计算器 cadence_fire_rate 实现。
#   蓄力时间本身取 weapon_attacks.json 的
#   charge_seconds（wfsim；逐把与 wiki ChargeTime 一致，唯一差异 Prisma Lenz 0.8：wiki 模块抄了
#   Lenz 的 1.2，wiki Fire Rate 列表与 Prisma Lenz 页「faster Charge Time」均为 0.8）。
#   值 = (名字, 出处 wiki Module:Weapons/data/<模块>:<武器块起始行>)。
CHARGE_DRAW_ONLY: dict[str, tuple] = {
    "/lotus/weapons/tenno/bows/antlerbow/antlerbow": ("Cernos", "primary:2709 Class=Bow"),
    "/lotus/weapons/tenno/bows/primecernos/primecernos": ("Cernos Prime", "primary:2774 Class=Bow"),
    "/lotus/weapons/tenno/bows/daxduviriasymetricalbow/daxduviriasymmetricallongbowplayerweapon": (
        "Cinta",
        "primary:2843 Class=Bow",
    ),
    "/lotus/weapons/tenno/bows/asymetricalbow/asymetricalbow": ("Daikyu", "primary:3485 Class=Bow"),
    "/lotus/weapons/tenno/bows/stalkerbow": ("Dread", "primary:3876 Class=Bow"),
    "/lotus/weapons/tenno/bows/tnchoirbow/tnchoirbow": ("Evensong", "primary:4113 Class=Bow"),
    "/lotus/weapons/grineer/bows/grnbow/grnbowweapon": ("Kuva Bramma", "primary:5829 Class=Bow"),
    "/lotus/weapons/clantech/energy/railgun": (
        "Lanka",
        "primary:6728 Fire Rate 页：1 / Modded Charge Time",
    ),
    "/lotus/weapons/corpus/bow/longbow/crpbow": ("Lenz", "primary:7060 Class=Bow"),
    "/lotus/weapons/mk1series/mk1paris": ("Mk1-Paris", "primary:7440 Class=Bow"),
    "/lotus/weapons/infested/bow/infcernosbow/infcernos": (
        "Mutalist Cernos",
        "primary:7613 Class=Bow",
    ),
    "/lotus/weapons/tenno/bows/omicrus/omicrusplayerwep": ("Nataruk", "primary:7940 Class=Bow"),
    "/lotus/weapons/tenno/bows/huntingbow": ("Paris", "primary:8640 Class=Bow"),
    "/lotus/weapons/tenno/bows/primehuntingbow": ("Paris Prime", "primary:8727 Class=Bow"),
    "/lotus/weapons/corpus/bow/longbow/prismalenz/prismalenzweapon": (
        "Prisma Lenz",
        "primary:9522 Class=Bow",
    ),
    "/lotus/weapons/tenno/bows/primederelictcernos/derelictcernos": (
        "Proboscis Cernos",
        "primary:9660 Class=Bow",
    ),
    "/lotus/weapons/syndicates/redveil/bows/rvcernos": ("Rakta Cernos", "primary:10111 Class=Bow"),
}


# 每发耗弹（2026-10-10 b7 批）：wiki Module:Weapons/data 主段（= 我方顶层面板那一段）AmmoCost ≠ 1
# 的武器（Kohm / Kuva Kohm 在 MAIN_ATTACK_FIX，不重复）。持续射击类 0.5 = wiki Fire Rate「for
# continuous weapons 0.5 rounds per tick」。持续 DPS 的弹匣时间 = 弹匣 / (有效射速 × 每发耗弹)。
# 值 = (名字, AmmoCost, 出处 wiki Module:Weapons/data/<模块>:<该攻击段起始行> <段名>)；逐把与
# wfsim attack.ammo_cost 一致，唯一分歧 Kuva Nukor（wfsim 0.5、wiki secondary:5055 为 1 ⇒ 按 wiki 不填）。
AMMO_COST_FIX: dict[str, tuple] = {
    # 2026-10-16：wiki《Arbucep》Codex「simultaneously fires six homing rockets」（每枚独立攻击、弹匣 36）
    #   ⇒ 一次扣扳机耗 6 发；wfsim d73ecfc 将 arbucep.yaml ammo_cost 1→6（实测 M112）。
    #   原先按 1 发算：弹匣时间 24 s（实为 4 s），持续 DPS 高估约 38%。
    "/lotus/weapons/tenno/archwing/primary/nokkoarchgun/nokkoarchgun": (
        "Arbucep",
        6,
        "archgun: Codex six rockets + wfsim M112",
    ),
    "/lotus/weapons/corpus/longguns/chainlightninggun/chainlightningrifle": (
        "Amprex",
        0.5,
        "primary:741 Normal Attack",
    ),
    "/lotus/types/sentinels/sentinelweapons/gremlin": ("Artax", 0.5, "companion:7 Normal Attack"),
    "/lotus/weapons/tenno/pistols/primeballistica/primeballistica": (
        "Ballistica Prime",
        4,
        "secondary:1931 Normal Shot",
    ),
    "/lotus/weapons/infested/pistols/infbeampistol/infbeampistol": (
        "Catabolyst",
        0.5,
        "secondary:2367 Primary",
    ),
    "/lotus/weapons/infested/infestedlich/pistols/codacatabolyst": (
        "Coda Catabolyst",
        0.5,
        "secondary:2550 Primary",
    ),
    "/lotus/weapons/infested/infestedlich/longguns/codasynapse": (
        "Coda Synapse",
        0.5,
        "primary:3208 Normal Attack",
    ),
    "/lotus/weapons/corpus/longguns/crpsplitlaser/crpsplitlaser": (
        "Convectrix",
        0.5,
        "primary:3258 Normal Attack",
    ),
    "/lotus/weapons/tenno/archwing/primary/thanotecharchgun/thanotecharchgun": (
        "Cortege",
        0.5,
        "archwing:642 Held；Atmosphere 版 archwing:716 同为 0.5",
    ),
    "/lotus/weapons/corpus/pistols/crpchargegun/crpchargegun": (
        "Cycron",
        0.5,
        "secondary:2881 Normal Attack",
    ),
    "/lotus/weapons/infested/pistols/infestedpistol": (
        "Embolist",
        0.5,
        "secondary:3451 Normal Attack",
    ),
    "/lotus/weapons/clantech/energy/crplaserrifle": (
        "Flux Rifle",
        0.5,
        "primary:4421 Normal Attack",
    ),
    "/lotus/weapons/syndicates/cephalonsuda/pistols/csdroidarray": (
        "Gammacor",
        0.5,
        "secondary:3874 Normal Attack",
    ),
    "/lotus/weapons/corpus/longguns/crpfreezeray/crpfreezerayrifle": (
        "Glaxion",
        0.5,
        "primary:4617 Normal Attack",
    ),
    "/lotus/weapons/corpus/longguns/crpfreezeray/vandal/crpfreezerayvandalrifle": (
        "Glaxion Vandal",
        0.5,
        "primary:4664 Normal Attack",
    ),
    "/lotus/weapons/clantech/chemical/flamethrower": ("Ignis", 0.5, "primary:5403 Normal Attack"),
    "/lotus/weapons/clantech/chemical/flamethrowerwraith": (
        "Ignis Wraith",
        0.5,
        "primary:5450 Normal Attack",
    ),
    "/lotus/weapons/grineer/pistols/grnkohmpistol/grnkohmpistol": (
        "Kohmak",
        1.67,
        "secondary:4538 Fully Spooled",
    ),
    "/lotus/weapons/tenno/archwing/primary/tnshieldframearchgun/tnshieldframearchgun": (
        "Larkspur",
        0.5,
        "archwing:2052 Normal Attack；Atmosphere 版 archwing:2130 同为 0.5",
    ),
    "/lotus/weapons/tenno/archwing/primary/primelarkspur/primelarkspurweapon": (
        "Larkspur Prime",
        0.5,
        "archwing:2233 Normal Attack",
    ),
    "/lotus/weapons/grineer/pistols/grineermicrowavegun/grnmicrowavepistol": (
        "Nukor",
        0.5,
        "secondary:6038 Normal Attack",
    ),
    "/lotus/weapons/corpus/pistols/crpsentexperimentpistol/crpsentexperimentpistol": (
        "Ocucor",
        0.5,
        "secondary:6085 Normal Attack",
    ),
    "/lotus/weapons/tenno/longguns/miter/tnomiter": ("Panthera", 2, "primary:8411 Normal Attack"),
    "/lotus/weapons/tenno/longguns/primepanthera/primepanthera": (
        "Panthera Prime",
        2,
        "primary:8478 Normal Attack",
    ),
    "/lotus/weapons/infested/longguns/tentacluster/infestedshotgun": (
        "Phage",
        0.5,
        "primary:8992 Normal Attack",
    ),
    "/lotus/weapons/tenno/longguns/revenantshotgun/revenantshotgun": (
        "Phantasma",
        0.5,
        "primary:9039 Beam",
    ),
    "/lotus/weapons/tenno/longguns/primephantasma/phantasmaprimeshotgun": (
        "Phantasma Prime",
        0.5,
        "primary:9156 Beam",
    ),
    "/lotus/weapons/corpus/longguns/crpshockrifle/crpshockrifle": (
        "Quanta",
        0.5,
        "primary:9802 Beam",
    ),
    "/lotus/weapons/corpus/longguns/crpshockrifle/quantavandal": (
        "Quanta Vandal",
        0.5,
        "primary:9900 Beam",
    ),
    "/lotus/weapons/clantech/energy/crplaserpistol": (
        "Spectra",
        0.5,
        "secondary:7618 Normal Attack",
    ),
    "/lotus/weapons/corpus/pistols/corpusmodularpistol/vandal/crplaserpistolvandal": (
        "Spectra Vandal",
        0.5,
        "secondary:7664 Normal Attack",
    ),
    "/lotus/weapons/corpus/pistols/crpelectromag/crpelectromag": (
        "Staticor",
        5,
        "secondary:7840 Fully Charged Projectile（我方默认 = 满蓄力：weapon_attacks main_form "
        "charged、wiki _TooltipAttackDisplay = 4；未蓄力 secondary:7807 为 1，形态 forms 自带）",
    ),
    "/lotus/weapons/infested/longguns/infestedrifle": (
        "Synapse",
        0.5,
        "primary:12032 Normal Attack",
    ),
    "/lotus/weapons/syndicates/cephalonsuda/pistols/cssynoidgammacor": (
        "Synoid Gammacor",
        0.5,
        "secondary:8065 Normal Attack",
    ),
    "/lotus/types/friendly/pets/moapets/moapetcomponents/tazronweapon": (
        "Tazicor",
        0.5,
        "companion:927 Normal Attack",
    ),
    "/lotus/weapons/corpus/boardexec/secondary/crpbecycron/crpbecycron": (
        "Tenet Cycron",
        0.5,
        "secondary:8275 Normal Attack",
    ),
    "/lotus/weapons/corpus/boardexec/primary/crpbeglaxion/crpbeglaxion": (
        "Tenet Glaxion",
        0.5,
        "primary:12516 Normal Attack",
    ),
    "/lotus/weapons/grineer/longguns/grineerassaultrifle/twingrakatas": (
        "Twin Grakatas",
        2,
        "secondary:8637 Normal Attack",
    ),
    "/lotus/weapons/grineer/pistols/grndwuniques/grntwinkohmaks": (
        "Twin Kohmak",
        1.67,
        "secondary:8749 Fully Spooled",
    ),
}


# 弹匣口径（2026-10-10 b8 批）：持续 DPS 的弹匣时间 = 弹匣 / (有效射速 × 每发耗弹)，弹匣必须是
# **弹药**口径（wiki Module:Weapons/data 的 Magazine 字段）；WFCD magazineSize 对下列武器给的是
# 「发数」（= wiki 弹药 ÷ 每发耗弹，电池武器则是 1/10），与 AMMO_COST_FIX / 形态 ammo_cost 叠乘
# 就重复压低了持续 DPS（Panthera 修前 30 ÷ 2 = 15 发，实为 60 ÷ 2 = 30 发）。
# 全量复查口径：wiki 任一攻击段 AmmoCost ≠ 1、或我方顶层 / 形态 ammoCost ≠ 1 的 74 行记录（含
# 形态；Cortege / Larkspur / Mausolon / Morgha 太空 + 大气各一行），逐条比 wiki Magazine，
# 不一致的只有本表 8 把（Convectrix 是 WFCD 缺 magazineSize）。
# 值 = (名字, wiki Magazine, 出处 wiki Module:Weapons/data/<模块>:<Magazine 行> + 说明)；
# wfsim data/weapons/*/<武器>.yaml 的 magazine 逐把相同。
MAGAZINE_FIX: dict[str, tuple] = {
    "/lotus/weapons/tenno/longguns/tnwisprifle/tnwisprifle": (
        "Fulmin",
        60,
        "primary:4523 Magazine = 60（WFCD 6）；电池：全自动每发 1、半自动每发 10（primary:4473 / 4491 "
        "AmmoCost）；wiki Fulmin「regenerates 30 ammo per second」，Reload 3 = 停火 1s + 60/30",
    ),
    "/lotus/weapons/tenno/longguns/primefulmin/primefulmin": (
        "Fulmin Prime",
        80,
        "primary:4595 Magazine = 80（WFCD 8）；AmmoCost 1 / 10（primary:4545 / 4563）；"
        "wiki「regenerates 40 ammo per second」，Reload 2.7 = 停火 0.7s + 80/40",
    ),
    "/lotus/weapons/tenno/longguns/miter/tnomiter": (
        "Panthera",
        60,
        "primary:8460 Magazine = 60（WFCD 30）；Normal Attack AmmoCost 2（primary:8411）",
    ),
    "/lotus/weapons/tenno/longguns/primepanthera/primepanthera": (
        "Panthera Prime",
        80,
        "primary:8544 Magazine = 80（WFCD 40）；Normal Attack AmmoCost 2（primary:8478）",
    ),
    "/lotus/weapons/tenno/pistols/primeballistica/primeballistica": (
        "Ballistica Prime",
        32,
        "secondary:2001 Magazine = 32（WFCD 8）；Normal Shot AmmoCost 4（secondary:1931），"
        "wiki Ballistica Prime「Consumes 4 ammo per shot … resulting in an 8-shot magazine」",
    ),
    "/lotus/weapons/corpus/pistols/crpelectromag/crpelectromag": (
        "Staticor",
        48,
        "secondary:7885 Magazine = 48（WFCD 12）；满蓄力 AmmoCost 5（secondary:7840）、未蓄力 1",
    ),
    "/lotus/weapons/grineer/longguns/grineerassaultrifle/twingrakatas": (
        "Twin Grakatas",
        120,
        "secondary:8665 Magazine = 120（WFCD 60）；Normal Attack AmmoCost 2（secondary:8637）",
    ),
    "/lotus/weapons/corpus/longguns/crpsplitlaser/crpsplitlaser": (
        "Convectrix",
        70,
        "primary:3304 Magazine = 70（WFCD 缺 magazineSize ⇒ 修前无持续 DPS）；"
        "Normal Attack AmmoCost 0.5（primary:3258）",
    ),
}


# 不装填循环（2026-10-10 b8 批）：wiki Epitaph §Notes「Epitaph draws ammo directly from the full
# ammo pool similar to bows, but does not "reload" between shots like them. The delay between shots
# is all based on its 1.5 fire-rate (0.67s), and Epitaph's displayed 0.6s reload time has no
# purpose.」；两页 §Advantages 同为「No magazine; does not need to reload.」（Epitaph Prime 页无
# Notes 段 ⇒ 按同族同机制处理，属推论）。⇒ noReload：持续 DPS = 爆发（不付每发 0.6s 装填）。
# ★ 2026-10-11 视频逐帧实测改口径（HUD 弹药递减点，60fps）：满蓄力射击间隔 = 蓄力 + 1/射速
#   （一般式），**不是** b8 批推断的 max(蓄力, 1/射速)。关键样本 YouTube imMX8IsPVF0（2022-02-05，
#   模拟仓连发，面板射速 2.10 / 蓄力 0.26，红暴确认是蓄力弹）：27 个间隔最小 0.700 / 中位 0.867，
#   一般式预测 0.733 落在下限、max 式 0.476 无一接近；每发装填（wfsim）预测 1.333 被三个视频否定
#   （WPrehuYiobc 实测 0.433 < 面板装填 0.6）。快射 = 1/射速、不装填（BV1eX9sBaEYd、WPrehuYiobc）。
#   wiki Fire Rate 的「排除」指的是不装填（弹药直接取自储备），并非蓄力不计时。
# 值 = (名字, 出处)。
NO_RELOAD_FIX: dict[str, tuple] = {
    "/lotus/weapons/tenno/pistols/tnwraithesidearm/tnwraithesidearmweapon": (
        "Epitaph",
        "wiki Epitaph §Notes / §Advantages；Module:Weapons/data/secondary:3492 Magazine 1 / "
        "Reload 0.6 / FireRate 1.5 / ChargeTime 0.36",
    ),
    "/lotus/weapons/tenno/pistols/primeepitaph/primeepitaphsidearmweapon": (
        "Epitaph Prime",
        "wiki Epitaph Prime §Advantages；Module:Weapons/data/secondary:3579 Magazine 1 / "
        "Reload 0.6 / FireRate 1.75 / ChargeTime 0.36",
    ),
}


# 拟成枪系多重「出膛预合并」（2026-10-10 b8 批）：orbMultishotCap = 4 ⇒ 爆炸伤害 × min(多重, 4)
# （期望），只一次爆炸、暴击 / 触发按一次判定；合并伤害 / 奇点 DoT 不吃多重（计算器 calculate）。
# 出处：wiki Synoid Simulor §Notes「Deals 240 / 480 / 720 / 960 Magnetic damage depending on
# multishot (capped at 960)」、「This wastes the additional 3rd pre-merged orb generated from the
# Multishot」；wiki Simulor §Notes「Multishot will create duplicate orbs, however, due to the
# Simulor's perfect accuracy the second orb will be merged with the original orb」；Simulor 页
# Update 27.4「increased the orb stack count to reach a fully powered vortex from 3 to 4」。
# 普通版 200/400/600/800 按同机制推论（wiki 未单列数值）。wfsim 两把都是 radial
# takes_multishot: false（多重完全无效），属有意分歧。值 = (名字, 上限, 出处)。
ORB_PREMERGE_FIX: dict[str, tuple] = {
    "/lotus/weapons/cephalon/primary/cephprimary/cephprimary": (
        "Simulor",
        4,
        "wiki Simulor §Notes（复制球并进原球）+ Synoid Simulor 同机制推论",
    ),
    "/lotus/weapons/syndicates/cephalonsuda/longguns/cssimulor": (
        "Synoid Simulor",
        4,
        "wiki Synoid Simulor §Notes 240 / 480 / 720 / 960（上限 960）",
    ),
}


def _apply_cadence_fix(key: str, rec: dict, log: list[str]) -> None:
    """BURST_FIX / CHARGE_DRAW_ONLY / AMMO_COST_FIX / MAGAZINE_FIX / NO_RELOAD_FIX 落进记录（幂等：
    已是目标值则不记改动）。
    不在 CHARGE_DRAW_ONLY 里的记录去掉残留的 chargeCadence（b7 批 Epitaph 系移出；b10 批去掉其 "rate"）。"""
    want: dict = {}
    if key in BURST_FIX:
        _nm, fr, n, d, _src = BURST_FIX[key]
        want = {"fireRate": fr, "burstCount": n, "burstDelay": d}
    if key in NO_RELOAD_FIX:
        want["noReload"] = True
    if key in CHARGE_DRAW_ONLY:
        want["chargeCadence"] = "draw"
    elif "chargeCadence" in rec:
        log.append(f"{rec.get('name')}.chargeCadence: {rec['chargeCadence']} → （去掉）")
        del rec["chargeCadence"]
    if key in AMMO_COST_FIX:
        want["ammoCost"] = float(AMMO_COST_FIX[key][1])
    if key in MAGAZINE_FIX:
        want["magazineSize"] = MAGAZINE_FIX[key][1]
    if key in ORB_PREMERGE_FIX:
        want["orbMultishotCap"] = ORB_PREMERGE_FIX[key][1]
    for f, v in want.items():
        if rec.get(f) != v:
            log.append(f"{rec.get('name')}.{f}: {rec.get(f)} → {v}")
            rec[f] = v


_ATTACK_FIELD = {
    "criticalChance": ("crit_chance", 0.01),
    "criticalMultiplier": ("crit_mult", 1.0),
    "procChance": ("status_chance", 0.01),
    "fireRate": ("speed", 1.0),
}


def _apply_main_attack_fix(key: str, rec: dict, log: list[str]) -> None:
    fix = MAIN_ATTACK_FIX.get(key)
    if not fix:
        return
    vals = fix["values"]
    if "attacks" in fix and rec.get("attacks") != fix["attacks"]:
        rec["attacks"] = json.loads(json.dumps(fix["attacks"]))
        log.append(f"{fix['name']}.attacks → wiki {len(fix['attacks'])} 段")
    for _sn, _patch in (fix.get("segment_patch") or {}).items():
        _s = next((a for a in rec.get("attacks") or [] if a.get("name") == _sn), None)
        if _s is None:
            raise SystemExit(f"MAIN_ATTACK_FIX：{fix['name']} 的 attacks 里没有段「{_sn}」")
        for _f, _v in _patch.items():
            if _s.get(_f) != _v:
                _s[_f] = json.loads(json.dumps(_v))
                log.append(f"{fix['name']}.attacks[{_sn}].{_f} → {_v}")
    if "throwDamage" in fix and rec.get("throwDamage") != fix["throwDamage"]:
        rec["throwDamage"] = dict(fix["throwDamage"])
        log.append(f"{fix['name']}.throwDamage → {fix['throwDamage']}")
    seg = next((a for a in rec.get("attacks") or [] if a.get("name") == fix["attack"]), None)
    if seg is None:
        raise SystemExit(f"MAIN_ATTACK_FIX：{fix['name']} 的 attacks 里没有段「{fix['attack']}」")
    if fix.get("check_attack", True):
        for f, v in vals.items():
            if f == "damage":
                got = {k: float(x) for k, x in (seg.get("damage") or {}).items() if x}
                ok = all(abs(got.get(k, 0.0) - float(x)) <= 0.01 for k, x in v.items()) and set(
                    got
                ) <= set(v)
            elif f not in _ATTACK_FIELD:
                continue  # multishot / ammoCost：WFCD 攻击段不带，出处见 source
            else:
                src, scale = _ATTACK_FIELD[f]
                ok = seg.get(src) is not None and abs(float(seg[src]) * scale - float(v)) <= 0.006
            if not ok:
                print(
                    f"  !! MAIN_ATTACK_FIX {fix['name']}.{f}={v} 与 WFCD 段「{fix['attack']}」不一致"
                )
    for f, v in vals.items():
        if f == "damage":
            dmg = rec.setdefault("damage", {})
            snap = dict(dmg)
            for k in list(dmg):
                if k != "total" and isinstance(dmg[k], (int, float)) and k not in v and dmg[k]:
                    dmg[k] = 0
            dmg.update({k: float(x) for k, x in v.items()})
            _retotal(dmg)
            if not any(float(x) for x in v.values()):
                dmg["total"] = 0.0  # 直击 0（Simulor 系，b7 批）：_retotal 只在合计 > 0 时回写
            if dmg == snap:
                continue
            new = dmg["total"]
        else:
            new = v
            old = rec.get(f)
            if old is not None and abs(float(old) - float(v)) <= 1e-9:
                continue
            rec[f] = v
        log.append(f"{fix['name']}.{f} → {new}")


def repair_weapons(out: dict) -> list[str]:
    """武器表的源头修正（build_weapons 与 ``--repair-weapons`` 共用，幂等）：
    ① 物理对调残留清零（``_fix_physical_from``，按记录自带的 attacks）；
    ② ``MAIN_ATTACK_FIX``：主段的暴击 / 暴伤 / 触发 / 射速（及少数伤害构成）按同一攻击段取值；
    ③ ``BURST_FIX`` / ``CHARGE_DRAW_ONLY``：点射面板射速 + 发数 / 发间延迟、蓄力节奏（b6 批）；
       ``AMMO_COST_FIX``：每发耗弹（b7 批）；``MAGAZINE_FIX``：弹匣按 wiki 弹药口径；
       ``NO_RELOAD_FIX``：葬铭系不装填；``ORB_PREMERGE_FIX``：拟成枪系多重预合并（b8 批）。
    返回改动清单（重跑一次应为空）。"""
    log: list[str] = []
    for key, rec in out.items():
        if key not in MAIN_ATTACK_FIX and rec.get("attacks") and rec.get("damage"):
            before = rec["damage"].get("total")
            changed = _fix_physical_from(rec["damage"], rec["attacks"])
            if changed:
                _retotal(rec["damage"])
                log.append(
                    f"{rec.get('name')}: 物理键 {changed}（{before} → {rec['damage']['total']}）"
                )
        _apply_main_attack_fix(key, rec, log)
        _apply_cadence_fix(key, rec, log)
    missing = sorted(
        (
            set(MAIN_ATTACK_FIX)
            | set(BURST_FIX)
            | set(CHARGE_DRAW_ONLY)
            | set(AMMO_COST_FIX)
            | set(MAGAZINE_FIX)
            | set(NO_RELOAD_FIX)
            | set(ORB_PREMERGE_FIX)
        )
        - set(out)
    )
    if missing:
        raise SystemExit(
            "MAIN_ATTACK_FIX / BURST_FIX / CHARGE_DRAW_ONLY / AMMO_COST_FIX / MAGAZINE_FIX / "
            "NO_RELOAD_FIX / ORB_PREMERGE_FIX "
            f"键不在武器表里：{missing}"
        )
    return log


def _write_weapons(out: dict) -> None:
    # indent=1：与现行 weapons_stats.json 的排版一致（scripts/repair_panel_basis.py 同此），
    # 免得一次重跑把整文件改成单行、diff 无法审阅
    (DATA / "weapons_stats.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8"
    )


def repair_weapons_file() -> list[str]:
    """``--repair-weapons``：只对现有 weapons_stats.json 跑 ``repair_weapons``（不联网、幂等）。

    现行表在 WFCD 生成之后还叠过 scripts/repair_panel_basis.py 与少量手工补录（Talons /
    Valkyr 利爪，见 git log），整表从 WFCD 重抓会丢这些 ⇒ 源头修正同时提供就地重放入口。"""
    out = json.loads((DATA / "weapons_stats.json").read_text(encoding="utf-8"))
    log = repair_weapons(out)
    if log:
        _write_weapons(out)
    return log


def build_weapons() -> dict:
    name_zh = json.loads((DATA / "de" / "name_zh.json").read_text(encoding="utf-8"))
    out: dict[str, dict] = {}
    for part in ("Primary", "Secondary", "Melee"):
        raw = _fetch_cached(
            f"https://cdn.jsdelivr.net/npm/warframe-items@latest/data/json/{part}.json",
            f"{part}.json",
        )
        items = json.loads(raw)
        n = 0
        for it in items:
            if not it.get("damage"):
                continue
            key = it.get("uniqueName", "").lower()
            rec = {k: it.get(k) for k in KEEP if it.get(k) not in (None, [], {})}
            rec["damage"] = _fix_physical(it)
            # ⚠️ 一把武器常常有**好几段伤害判定**（普通/投掷/充能投掷/投掷爆炸/
            #    震地/重击震地/灵化形态…），各段的伤害类型构成完全可能不同
            #    （Glaive Prime 爆炸是爆炸伤害、Cerata 是毒素、Xoris 是电击），
            #    所以**全量存下来**，计算时按模式分别算，不能拿普通攻击缩放糊弄。
            _ats = _collect_attacks(it)
            if _ats:
                rec["attacks"] = _ats
            for _a in _ats:  # 兼容旧字段
                if _a["name"].strip().lower() == "throw":
                    rec["throwDamage"] = dict(_a["damage"])
                    break
            zh = name_zh.get(key)
            if not zh:
                # 有的条目 uniqueName 大小写不同，再试一次原样
                zh = name_zh.get(it.get("uniqueName", ""))
            if zh:
                rec["zh"] = zh
            out[key] = rec
            n += 1
        print(f"{part}: {n} 条")
    for line in repair_weapons(out):
        print("  修正", line)
    _write_weapons(out)
    print(
        f"weapons_stats.json：共 {len(out)} 条，{Path(DATA / 'weapons_stats.json').stat().st_size // 1024} KB"
    )
    return out


# ---------------------------------------------------------------------------
# 3) 敌人基础数值（来源：极镜 riven-mirror 的 codex/enemy.ts）
#    [name, faction, baseLevel, baseHealth, baseShield, baseArmor, …, headMul]
#    只有「基准值 + 基准等级」是这里独有的——现行 U36 抗性规则仍走 damage_faction.json
# ---------------------------------------------------------------------------
# 极镜的 EnemyFaction 枚举序（0 Tenno / 1 Grineer / 2 Corpus / 3 Infested /
# 4 Orokin(堕落) / 5 Sentient / 6 Wild）
_FACTIONS = ["Tenno", "Grineer", "Corpus", "Infested", "Orokin", "Sentient", "Wild"]

# 英文名 -> 官方简中（沿用插件里那张人工核过的表，只收有把握的；其余留英文）
_ENEMY_ZH = {
    "Ancient Disruptor": "远古干扰者",
    "Ancient Healer": "远古治愈者",
    "Boiler": "痈裂者",
    "Brood Mother": "病变虫母",
    "Charger": "疾冲者",
    "Crawler": "爬行者",
    "Leaper": "奔跳者",
    "Runner": "狂奔者",
    "Anti MOA": "逆进恐鸟",
    "MOA": "恐鸟",
    "Fusion MOA": "熔岩恐鸟",
    "Crewman": "船员",
    "Elite Crewman": "精英船员",
    "Nullifier Crewman": "虚能船员",
    "Corrupted Nullifier": "堕落虚能者",
    "Butcher": "屠夫",
    "Flameblade": "焰刃",
    "Powerfist": "强拳",
    "Scorpion": "天蝎",
    "Shield Lancer": "盾枪兵",
    "Ballista": "弩炮",
    "Eviscerator": "开膛者",
    "Hellion": "行刑者",
    "Lancer": "枪兵",
    "Elite Lancer": "精英枪兵",
    "Scorch": "怒焚者",
    "Seeker": "追踪者",
    "Trooper": "骑兵",
    "Bombard": "轰击者",
    "Commander": "指挥官",
    "Drahk Master": "爪喀驯兽师",
    "Heavy Gunner": "重型机枪手",
    "Hyekka Master": "鬣猫驯兽师",
    "Manic": "狂躁者",
    "Napalm": "火焰轰击者",
    "Nox": "诺克斯",
    "Ghoul Auger": "尸鬼钻地者",
    "Ghoul Devourer": "尸鬼吞噬者",
    "Ghoul Expired": "尸鬼腐化者",
    "Ghoul Rictus": "尸鬼狞笑者",
    "Grineer Warden": "Grineer 典狱长",
    "Sensor Regulator": "传感器调节器",
    "Corrupted Ancient": "远古堕落者",
    "Corrupted Bombard": "堕落轰击者",
    "Corrupted Butcher": "堕落屠夫",
    "Corrupted Crewman": "堕落船员",
    "Corrupted Heavy Gunner": "堕落重型机枪手",
    "Corrupted Lancer": "堕落枪兵",
    "Corrupted MOA": "堕落恐鸟",
    "Bailiff": "法警",
    "Wolf of Saturn Six": "土星六号之狼",
    "Eidolon Teralyst": "夜灵兆力使",
    "Eidolon Gantulyst": "夜灵巨力使",
    "Eidolon Hydrolyst": "夜灵水力使",
    "Profit-Taker Orb": "利润收割者圆蛛",
    "Tusk Butcher": "巨牙屠夫",
    "Tusk Lancer": "巨牙枪兵",
    "Tusk Bombard": "巨牙轰击者",
    "Tusk Predator": "巨牙掠食者",
    "Kuva Lich": "赤毒玄骸",
    "Kuva Guardian": "赤毒守卫者",
    "Narmer Lancer": "合一众枪兵",
    "Deimos Carnis": "魔胎之境的肉类",
    "Juno Crewman": "朱诺船员",
    "Terra Crewman": "泰拉船员",
    "Terra MOA": "泰拉恐鸟",
    "Terra Provisor": "泰拉供给者",
    "Vapos Crewman": "瓦波斯船员",
    "Murex": "骨螺",
    "Thrax Centurion": "凶魂百夫长",
    "Thrax Legatus": "凶魂使节",
    "Corrupted Vor": "堕落的 Vor",
}


def build_enemies() -> dict:
    # ⚠ 这里写出的是 riven-mirror **全表**（约 92 条）；必须再跑 scripts/merge_wfsim_enemies.py，
    #   它合并 wfsim、打爆头补丁，并按 KEEP_ENEMIES（2026-10-10 维护者定的 19 个常用敌人）
    #   删掉其余条目、补官方中文名 —— 不跑它，删掉的敌人会被加回来。
    src = CACHE / "codex_enemy.ts"
    if not src.exists():
        print("⚠️ 跳过敌人表：缺 " + str(src))
        return {}
    txt = src.read_text(encoding="utf-8", errors="ignore")
    body = txt[txt.index("const _enemyList = [") : txt.index("] as [string")]
    rows = re.findall(r'\["([^"]+)"((?:\s*,\s*[^,\]]*)*)\]', body)
    out: dict[str, dict] = {}
    for name, tail in rows:
        parts = [p.strip() for p in tail.split(",")][1:]  # 第一段是空串
        vals = [None if p == "" else float(p) for p in parts]

        def g(i, default=0.0):
            return vals[i] if i < len(vals) and vals[i] is not None else default

        rec = {
            "name": name,
            "zh": _ENEMY_ZH.get(name, ""),
            "faction": _FACTIONS[int(g(0))] if vals and vals[0] is not None else "",
            "base_level": int(g(1, 1)),
            "base_health": g(2),
            "base_shield": g(3),
            "base_armor": g(4),
            # 2 是 riven-mirror 的通用约定、非逐敌值；重建后须再跑
            # scripts/merge_wfsim_enemies.py（含 --head-only 爆头补丁）改成真实值或 null
            "head_mul": g(10, 2),
        }
        out[name] = rec
    (DATA / "enemies.json").write_text(
        json.dumps(
            {
                "_meta": {
                    "source": "riven-mirror src/warframe/codex/enemy.ts",
                    "retrieved": "2026-09-16",
                    "note": "仅取基准数值与基准等级；抗性规则以 damage_faction.json 为准",
                },
                "enemies": out,
            },
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )
    print(f"enemies.json：{len(out)} 条（有中文名 {sum(1 for v in out.values() if v['zh'])}）")
    return out


# ---------------------------------------------------------------------------
# 4) MOD 数值表（来源：WFCD warframe-items 的 Mods.json，含满级数值与官方简中名）
# ---------------------------------------------------------------------------
_ELEM_WORDS = {
    "Heat": "heat",
    "Cold": "cold",
    "Electricity": "electricity",
    "Toxin": "toxin",
    "Blast": "blast",
    "Corrosive": "corrosive",
    "Gas": "gas",
    "Magnetic": "magnetic",
    "Radiation": "radiation",
    "Viral": "viral",
    "Void": "void",
}
# 物理三系 MOD（Sweeping Serration / Sawtooth Clip…）：只加成**同类型的基础值**，
# 且对没有该类型基础伤害的武器无效（wiki Damage「Physical Damage」段）
_PHYS_WORDS = {"Impact": "impact", "Puncture": "puncture", "Slash": "slash"}
# 只把这些关键词的描述留作「使用限制」提示（火炮弹幕的「射速不可修改」等）
_NOTE_KEYS = ("cannot", "only compatible", "not compatible", "exclusive")
_FACTION_WORDS = {
    "Grineer": "Grineer",
    "Corpus": "Corpus",
    "Infested": "Infested",
    "Orokin": "Orokin",
    "Sentient": "Sentient",
    "Sentients": "Sentient",
    "Murmur": "The Murmur",
    "Murmurs": "The Murmur",
    "Grineers": "Grineer",
    "Corpuss": "Corpus",
    "Infesteds": "Infested",
    "Narmer": "Narmer",
    "Scaldra": "Scaldra",
    "Techrot": "Techrot",
}


def parse_mod_stats(stats: list[str], dedupe_lines: bool = False) -> dict:
    """把 levelStats 的文本解析成计算器参数（只认我们能算的项）。

    ``dedupe_lines``：同一级 stats 里**重复出现的同一行**只计一次。warframe-items 的
    赋能满级 stats 会把被动行再单列一遍（Primary Deadhead 满级 =
    ``["On Precision Headshot Kill:\\n…\\n+30% to Headshot Multiplier…",
    "+30% to Headshot Multiplier", …]``），逐行累加会把 +30% 记成 +60%
    （wiki 原文 +30%，2026-10-10 核对）。赋能表开启；MOD 表保持原行为。
    """
    eff: dict = {}
    seen_lines: set[str] = set()
    # ⚠️ 逐行解析，**不能只取第一行**：像 Power Throw 的投掷伤害就写在第二行
    #    （`On Consecutive throw (Max stacks 3):\n+100% Throw Damage`），
    #    原来 `split("\n")[0]` 会把它整条丢掉（维护者 2026-09-16 报的就是这个）
    for raw_stat in stats or []:
        # 换行有两种形态：真实换行、以及 DE 文本里**字面的 "\\n" 两字符**
        # （Power Throw 用的就是后者）——统一成真实换行后，再把「标题: 换行 数值」
        # 接回一行，否则 `On Consecutive throw (Max stacks 3):` 与
        # `+100% Throw Damage` 会被拆开，两边都匹配不上
        for s in str(raw_stat).replace("\\n", "\n").replace(":\n", ": ").split("\n"):
            s = s.strip()
            if not s:
                continue
            if dedupe_lines:
                if s in seen_lines:
                    continue
                seen_lines.add(s)
            s = s.replace("<DT_FIRE_COLOR>", "").replace("<DT_FREEZE_COLOR>", "")
            s = s.replace("<DT_ELECTRICITY_COLOR>", "").replace("<DT_POISON_COLOR>", "")
            s = re.sub(r"<DT_[A-Z_]+>", "", s).strip()
            # ① 连续投掷（奋力一掷）：每层 +X% 投掷伤害，最多 N 层
            m_throw = re.match(
                r"On Consecutive throw\s*\(Max stacks (\d+)\)\s*:\s*"
                r"\+([\d.]+)%\s+Throw Damage",
                s,
                re.I,
            )
            if m_throw:
                eff["throw_max_stacks"] = int(m_throw.group(1))
                eff["throw_dmg"] = float(m_throw.group(2))
                continue
            # ② 先把括号里的**限定条件**抠出来存着，再剥括号 ——
            #    「(x2 for Heavy Attacks)」这类限定丢了就会算错重击
            brackets = " ".join(re.findall(r"\(([^)]*)\)", s))
            s = re.sub(r"\s*\([^)]*\)\s*", " ", s).strip()
            #    「%」设成可选：穿透 / 初始连击这类数值本身不带百分号
            m = re.fullmatch(r"([+-])([\d.]+)(?:%)?\s+(.+)", s)
            if m:
                sign = -1.0 if m.group(1) == "-" else 1.0
                pct, what = sign * float(m.group(2)), m.group(3).strip()
                if what in ("Damage", "Melee Damage"):
                    eff["base_dmg"] = eff.get("base_dmg", 0.0) + pct
                elif what == "Multishot":
                    eff["multishot"] = eff.get("multishot", 0.0) + pct
                elif what == "Critical Chance":
                    eff["crit_chance"] = eff.get("crit_chance", 0.0) + pct
                    # 斩铁：`+120% Critical Chance (x2 for Heavy Attacks)`
                    # → 重击时该卡的暴击几率加成翻倍（wiki 26.0.7 改动原文）
                    if re.search(r"x2\s+for\s+Heavy\s+Attack", brackets, re.I):
                        eff["heavy_crit_mult"] = 2.0
                elif what == "Critical Damage":
                    eff["crit_dmg"] = eff.get("crit_dmg", 0.0) + pct
                elif what == "Status Chance":
                    eff["status_chance"] = eff.get("status_chance", 0.0) + pct
                elif what == "Status Damage":
                    eff["status_dmg"] = eff.get("status_dmg", 0.0) + pct
                elif what.startswith("Fire Rate"):
                    # 数据里带后缀，如「-20% Fire Rate (x2 for Bows)」
                    eff["fire_rate"] = eff.get("fire_rate", 0.0) + pct
                elif what in ("Melee Damage On Heavy Attack", "Damage On Heavy Attack"):
                    # 一击必杀（Killing Blow）：只加重击，不动普通攻击
                    eff["heavy_dmg"] = eff.get("heavy_dmg", 0.0) + pct
                elif what == "Magazine Capacity":
                    eff["magazine_pct"] = eff.get("magazine_pct", 0.0) + pct
                elif what == "Reload Speed":
                    # 装填「速度」加成 → 实际装填时间 = 原时间 /(1+pct/100)
                    eff["reload_pct"] = eff.get("reload_pct", 0.0) + pct
                elif what == "Attack Speed":
                    # 近战用 Attack Speed 表达射速（原来只认 Fire Rate，整类漏掉）
                    eff["fire_rate"] = eff.get("fire_rate", 0.0) + pct
                elif what == "Status Duration":
                    eff["status_duration"] = eff.get("status_duration", 0.0) + pct
                elif what == "Punch Through":
                    # 奋力一掷：影响贯穿，不参与伤害数值
                    eff["punch_through"] = eff.get("punch_through", 0.0) + pct
                elif what == "Initial Combo":
                    # 邪恶蓄力：提高起手连击数，影响重击倍率起点
                    eff["initial_combo"] = eff.get("initial_combo", 0.0) + pct
                elif what == "Heavy Attack Wind Up Speed":
                    # 增幅斩铁类：只影响蓄力速度，不影响伤害（记录备查）
                    eff["heavy_windup"] = eff.get("heavy_windup", 0.0) + pct
                elif what == "Combo Duration":
                    eff["combo_duration"] = eff.get("combo_duration", 0.0) + pct
                elif what == "to Headshot Multiplier":
                    eff["headshot_bonus"] = eff.get("headshot_bonus", 0.0) + pct
                elif what.lower().startswith("critical chance") and re.search(r"combo", what, re.I):
                    # 狂怒（Blood Rush）：+40% 暴击几率「随连击倍率」叠加
                    eff["crit_per_combo"] = eff.get("crit_per_combo", 0.0) + pct
                elif what.lower().startswith("status chance") and re.search(r"combo", what, re.I):
                    # 创口溃烂（Weeping Wounds）：+40% 触发率「随连击倍率」
                    eff["status_per_combo"] = eff.get("status_per_combo", 0.0) + pct
                elif re.search(r"per\s+Status\s+Type", what, re.I):
                    # 异况超量 / 镀层战术等：目标身上每种异常 +N% 直伤
                    eff["dmg_per_status"] = eff.get("dmg_per_status", 0.0) + pct
                elif what in _ELEM_WORDS:
                    el = _ELEM_WORDS[what]
                    eff.setdefault("elements", {})[el] = eff.get("elements", {}).get(el, 0.0) + pct
                elif what in _PHYS_WORDS:
                    el = _PHYS_WORDS[what]
                    eff.setdefault("physical", {})[el] = eff.get("physical", {}).get(el, 0.0) + pct
                elif what.startswith("Damage to "):
                    fac = what[len("Damage to ") :].strip()
                    if fac in _FACTION_WORDS:
                        eff["faction_dmg"] = eff.get("faction_dmg", 0.0) + pct
                        eff["faction_of"] = _FACTION_WORDS[fac]
            m2 = re.fullmatch(r"x([\d.]+)\s+Damage to (.+)", s)
            if m2:  # 派系 MOD 在新版数据里是倍率写法：x1.55 Damage to Grineer
                fac = m2.group(2).strip()
                if fac in _FACTION_WORDS:
                    eff["faction_mul"] = float(m2.group(1))
                    eff["faction_of"] = _FACTION_WORDS[fac]
                continue
            # ③ 物理伤害转换：`20% of Damage converted into Impact`
            #    （彗星弹 / 锯齿弹 / 穿刺弹 / 剃刀弹药 …）——会把总伤的 20%
            #    「搬」到该物理类型上，直接改变 IPS 分布与派系弱点命中
            m_conv = re.fullmatch(r"([\d.]+)%\s+of\s+Damage\s+converted\s+into\s+(.+)", s, re.I)
            if m_conv:
                _to = _PHYS_WORDS.get(m_conv.group(2).strip())
                if _to:
                    eff.setdefault("physical_convert", {})[_to] = float(m_conv.group(1))
                continue
            # ④ 条件触发（On Kill / On Headshot / When …）：**一律不折进数值**，
            #    只在卡面列出原文。把它们当常驻加成会系统性高估。
            if re.match(r"(on\s|when\s|while\s|if\s)", s, re.I) or re.search(
                r"stacks?\s+up\s+to", s, re.I
            ):
                eff.setdefault("conditional", []).append(str(raw_stat)[:120])
                continue
    return eff


def _mod_compat(it: dict) -> str:
    return (it.get("type") or "").replace(" Mod", "")


# 只有这些槽位的 MOD 能被塞进武器配卡（战甲/守护/赋能等不参与，免得名表里
# 混进「聚精会神」这类同名/近名条目，把武器名与 MOD 名搅在一起）
WEAPON_COMPAT = {
    "Primary",
    "Secondary",
    "Melee",
    "Shotgun",
    "Stance",
    "Plexus",
    "Arch-Gun",
    "Arch-Melee",
    "Necramech",
}


def build_mods() -> dict:
    name_zh = json.loads((DATA / "de" / "name_zh.json").read_text(encoding="utf-8"))
    items = json.loads(
        _fetch_cached(
            "https://cdn.jsdelivr.net/npm/warframe-items@latest/data/json/Mods.json", "Mods.json"
        )
    )
    out: dict[str, dict] = {}
    index: dict[str, dict] = {}
    for it in items:
        nm = it.get("name") or ""
        zh = name_zh.get((it.get("uniqueName") or "").lower()) or ""
        compat = _mod_compat(it)
        # ① 全量名索引：**不管能不能算**，只要能上身武器就收，
        #    这样「锁定目标」这类暂不参与计算的卡也不会被当成武器名
        path_l = (it.get("uniqueName") or "").lower()
        variant = 1 if ("/beginner/" in path_l or "/expert/" in path_l) else 0
        if nm and compat in WEAPON_COMPAT:
            prev = index.get(nm.lower())
            if prev is None or variant < prev.get("_variant", 1):
                index[nm.lower()] = {
                    "name": nm,
                    "zh": zh,
                    "compat": compat,
                    "polarity": it.get("polarity"),
                    "base_drain": it.get("baseDrain"),
                    "max_rank": it.get("fusionLimit"),
                    "_variant": variant,
                }

        ls = it.get("levelStats") or []
        # ⚠️ 不能取 levelStats[-1]：同一条 MOD 在数据里有**多份**（含 11 级的变体），
        # 真实满级数值是 fusionLimit 那一档（如分裂膛室 → levelStats[5] = +90%）
        fl = int(it.get("fusionLimit") or 0)
        entry = ls[min(fl, len(ls) - 1)] if ls else {}
        stats = (entry or {}).get("stats") or []
        eff = parse_mod_stats(stats)
        if not eff:
            continue
        desc = " ".join((it.get("description") or "").split())
        if desc and any(k in desc.lower() for k in _NOTE_KEYS):
            eff["note"] = desc[:90]
        key = nm.lower()
        # 每级数值：截图识别要靠它折算「这张卡不是满级」时的真实加成
        # （levels[i] = 第 i 级的 effects；索引即等级）
        levels: list[dict] = []
        for e in ls[: fl + 1]:
            levels.append(parse_mod_stats(((e or {}).get("stats") or [])))
        base_drain = it.get("baseDrain")
        rec = {
            "name": nm,
            "zh": zh,
            "compat": compat,
            "polarity": it.get("polarity"),
            "rarity": it.get("rarity"),
            "drain": base_drain,
            "base_drain": base_drain,
            "drain_at_max": (int(base_drain) + int(fl)) if isinstance(base_drain, int) else None,
            "max_rank": it.get("fusionLimit"),
            "effects": eff,
            "levels": levels,
        }
        # 同名（Beginner/Expert/正式版 三份）优先取**正式版**：
        # data 里 /Mods/.../Beginner/ 是弱化版、/Expert/ 是强化版（数值高得离谱，
        # 不是玩家手里那张卡），只有正式路径缺失时才退回其它版本
        path = (it.get("uniqueName") or "").lower()
        variant = 1 if ("/beginner/" in path or "/expert/" in path) else 0
        old = out.get(key)
        if old is None or (variant, -_eff_weight(eff)) < (
            old["_variant"],
            -_eff_weight(old["effects"]),
        ):
            rec["_variant"] = variant
            out[key] = rec
    (DATA / "mods_stats.json").write_text(
        json.dumps(
            {
                "_meta": {
                    "source": "WFCD warframe-items Mods.json",
                    "retrieved": "2026-09-16",
                    "note": "mods=能参与计算的（满级值）；names=可识别的全量 MOD 名（可能无词条）",
                },
                "mods": out,
                "names": index,
            },
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )
    print(
        f"mods_stats.json：可算 {len(out)} 条 / 可识别 {len(index)} 条"
        f"（有中文名 {sum(1 for v in index.values() if v['zh'])}）"
    )
    return out


def _eff_weight(eff: dict) -> float:
    return (
        eff.get("base_dmg", 0)
        + eff.get("heavy_dmg", 0)
        + eff.get("throw_dmg", 0)
        + eff.get("multishot", 0)
        + eff.get("crit_chance", 0)
        + eff.get("crit_dmg", 0)
        + sum((eff.get("elements") or {}).values())
        + sum((eff.get("physical") or {}).values())
        + eff.get("headshot_bonus", 0)
        + eff.get("faction_dmg", 0)
        + (eff.get("faction_mul", 1.0) - 1.0) * 100
    )


# 武器类赋能（战甲/指挥官/增幅器的不进武器计算）
_WEAPON_ARCANE_TYPES = (
    "Primary Arcane",
    "Secondary Arcane",
    "Melee Arcane",
    "Shotgun Arcane",
    "Bow Arcane",
    "Kitgun Arcane",
    "Zaw Arcane",
)


def build_arcanes() -> dict:
    """构建武器赋能表（每级数值 + 效果文本）。

    赋能多为**条件触发**（"On Critical Hit: 60% chance for +60% Melee Damage"），
    这里如实保存文本与数值；是否计入由计算层决定（默认只在卡面标注）。
    """
    name_zh = json.loads((DATA / "de" / "name_zh.json").read_text(encoding="utf-8"))
    items = json.loads(
        _fetch_cached(
            "https://cdn.jsdelivr.net/npm/warframe-items@latest/data/json/Arcanes.json",
            "Arcanes.json",
        )
    )
    out: dict[str, dict] = {}
    for it in items:
        if (it.get("type") or "") not in _WEAPON_ARCANE_TYPES:
            continue
        nm = it.get("name") or ""
        if not nm:
            continue
        ls = it.get("levelStats") or []
        levels = []
        for e in ls:
            levels.append(parse_mod_stats(((e or {}).get("stats") or []), dedupe_lines=True))
        flat_text = " ".join(" ".join((e or {}).get("stats") or []) for e in ls).replace("\\n", " ")
        rec = {
            "name": nm,
            "zh": name_zh.get((it.get("uniqueName") or "").lower()) or "",
            "type": it.get("type"),
            "rarity": it.get("rarity"),
            "max_rank": max(0, len(ls) - 1),
            "effects": levels[-1] if levels else {},
            "levels": levels,
            "text": " ".join(flat_text.split())[:260],
        }
        out[nm.lower()] = rec
    (DATA / "arcanes_stats.json").write_text(
        json.dumps(
            {
                "_meta": {
                    "source": "WFCD warframe-items Arcanes.json",
                    "retrieved": "2026-09-16",
                    "note": "只收武器类；数值为满级值",
                },
                "arcanes": out,
            },
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )
    with_zh = sum(1 for v in out.values() if v["zh"])
    print(f"arcanes_stats.json：{len(out)} 条（有中文名 {with_zh}）")
    return out


if __name__ == "__main__":
    if "--repair-weapons" in sys.argv:
        _log = repair_weapons_file()
        for _line in _log:
            print("  修正", _line)
        print(f"weapons_stats.json：{len(_log)} 处修正" + ("" if _log else "（已是修正后状态）"))
        sys.exit(0)
    if "-x" in sys.argv:
        PROXY = sys.argv[sys.argv.index("-x") + 1]
    build_faction_table()
    build_weapons()
    build_arcanes()
    build_enemies()
    build_mods()
