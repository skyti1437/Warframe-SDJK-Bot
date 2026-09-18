# -*- coding: utf-8 -*-
"""构建伤害计算器的数据表。

产出（写入 core/data/）：
1. damage_faction.json —— 派系 × 伤害类型倍率（U36 抗性重构后：只有 ×1.5 / ×0.5 / ×1，
   弱点按派系而不是按血量类型）。来源 warframe.fandom.com「Damage/Overview Table」。
2. weapons_stats.json —— 武器基础数据（来自 WFCD warframe-items npm 包的
   Primary/Secondary/Melee 三张表，裁剪最小字段；中文名用插件自带的
   core/data/de/name_zh.json 按资产路径反查）。

用法：python build_damage_data.py   （需可访问 github/jsdelivr；或经 -x 代理）
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
CACHE = Path(r"REDACTED_TMP_DIR/dmgsrc")


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
FACTIONS = ["Tenno", "Grineer", "Kuva Grineer", "Corpus", "Corpus Amalgam",
            "Infested", "Infested Deimos", "Orokin", "Sentient", "Narmer",
            "The Murmur", "Zariman", "Scaldra", "Techrot"]

# 倍率表里会出现的伤害类型（物理 + 单元素 + 复合 + 特殊）
_KNOWN_TYPES = {
    "Impact", "Puncture", "Slash",
    "Cold", "Electricity", "Heat", "Toxin",
    "Blast", "Corrosive", "Gas", "Magnetic", "Radiation", "Viral",
    "Void", "True", "Tau", "Finisher",
}


def build_faction_table() -> dict:
    raw = _fetch_cached("https://warframe.fandom.com/api.php?action=parse"
                        "&page=Damage/Overview_Table&prop=wikitext&format=json&formatversion=2",
                        "Overview_Table.json")
    txt = json.loads(raw)["parse"]["wikitext"]

    # 表头列顺序（跳过第一列 Damage Type）
    heads = re.findall(r"skew\(50deg\)[^>]*>\s*(?:<[^>]+>\s*)*<span[^>]*>\s*\{\{D\|([^}]+)\}\}", txt)
    heads = [h.strip() for h in heads]
    if heads != FACTIONS:
        print("⚠️ 表头与预期不一致：", heads)

    table: dict[str, dict] = {}
    # {{D|X}} 模板里本身含 |，先把模板替换成纯名字再按 | 切格
    flat = re.sub(r"\{\{D\|([^}]+)\}\}", r"\1", txt)
    for block in flat.split("|-"):
        if "skew" in block:      # 表头
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
        body = cells[start:start + len(heads)]
        if len(body) < len(heads):
            continue
        vals: dict[str, float] = {}
        for name, cell in zip(heads, body):
            if "seagreen" in cell:
                vals[name] = 1.5
            elif "indianred" in cell:
                vals[name] = 0.5
        table[dtype] = vals
    out = {"_meta": {"source": "warframe.fandom.com/wiki/Damage/Overview_Table",
                     "retrieved": "2026-09-16", "rule": "U36 起：弱点/抗性按派系，+ = ×1.5，- = ×0.5，其余 ×1"},
           "factions": FACTIONS, "table": table}
    (DATA / "damage_faction.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"damage_faction.json：{len(table)} 种伤害类型")
    return table


# ---------------------------------------------------------------------------
# 2) 武器基础数据
# ---------------------------------------------------------------------------
KEEP = ("name", "uniqueName", "category", "masteryReq", "damage", "damageTypes",
        "criticalChance", "criticalMultiplier", "fireRate", "trigger",
        "multishot", "projectile", "disposition", "type", "slamAttack",
        # 持续 DPS 需要：magazineSize（弹匣）+ reloadTime（装填秒）
        "magazineSize", "reloadTime", "procChance",
        # 近战重击 / 连击 / 震地
        "heavyAttackDamage", "heavySlamAttack", "comboDuration",
        "slamAttack", "slamRadialDamage", "heavySlamRadialDamage",
        "slamRadius", "slideAttack", "windUp", "followThrough",
        "omegaAttenuation")


# 伤害类型键（用于把 attacks 里的 damage 归一成完整 dict）
_DMG_KEYS = ("impact", "puncture", "slash", "heat", "cold", "electricity",
             "toxin", "blast", "radiation", "gas", "magnetic", "viral",
             "corrosive", "void", "tau", "true")


def _collect_attacks(it: dict) -> list[dict]:
    """把 attacks 里每一段攻击判定都收下来（含伤害构成、暴击/触发、衰减、蓄力）。

    这些是「多段伤害」的原始依据：投掷命中与投掷爆炸是**两段独立判定**，
    伤害类型都可能不同，必须分别算。
    """
    out: list[dict] = []
    for a in (it.get("attacks") or []):
        raw = a.get("damage") or {}
        dmg = {k: float(v) for k, v in raw.items()
               if isinstance(v, (int, float)) and k in _DMG_KEYS}
        if not dmg:
            continue
        rec = {
            "name": (a.get("name") or "").strip(),
            "damage": dmg,
            "total": sum(dmg.values()),
        }
        for key in ("crit_chance", "crit_mult", "status_chance", "speed",
                    "shot_type", "charge_time", "shot_speed", "flight"):
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
    ats = it.get("attacks") or []
    normal = next((a for a in ats if (a.get("name") or "") == "Normal Attack"), None)
    src = ((normal or (ats[0] if ats else {})) or {}).get("damage") or {}
    for k in ("impact", "puncture", "slash"):
        v = src.get(k)
        if isinstance(v, (int, float)) and abs(float(v) - float(dmg.get(k) or 0)) > 0.05:
            dmg[k] = float(v)
    # ⚠️ warframe-items 顶层 `damage.total` 常与各成分之和**不一致**
    # （实测 581 把里 71 把：弓类 total 是蓄力值而成分是未蓄力值，差 2 倍；
    #  爆炸类 total 把范围段并了进来）。total 必须自洽 = Σ成分，
    # 蓄力/范围段由 attacks 段单独承担 —— 否则伤害链按 total 分配比例会算错。
    _sk = {"total", "cinematic", "shieldDrain", "healthDrain", "energyDrain"}
    _s = sum(float(v) for kk, v in dmg.items()
             if isinstance(v, (int, float)) and kk not in _sk)
    if _s > 0:
        dmg["total"] = round(_s, 4)
    return dmg


def build_weapons() -> dict:
    name_zh = json.loads((DATA / "de" / "name_zh.json").read_text(encoding="utf-8"))
    out: dict[str, dict] = {}
    for part in ("Primary", "Secondary", "Melee"):
        raw = _fetch_cached(
            f"https://cdn.jsdelivr.net/npm/warframe-items@latest/data/json/{part}.json",
            f"{part}.json")
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
            for _a in _ats:                       # 兼容旧字段
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
    (DATA / "weapons_stats.json").write_text(
        json.dumps(out, ensure_ascii=False), encoding="utf-8")
    print(f"weapons_stats.json：共 {len(out)} 条，{Path(DATA / 'weapons_stats.json').stat().st_size // 1024} KB")
    return out


# ---------------------------------------------------------------------------
# 3) 敌人基础数值（来源：极镜 riven-mirror 的 codex/enemy.ts）
#    [name, faction, baseLevel, baseHealth, baseShield, baseArmor, …, headMul]
#    只有「基准值 + 基准等级」是这里独有的——现行 U36 抗性规则仍走 damage_faction.json
# ---------------------------------------------------------------------------
# 极镜的 EnemyFaction 枚举序（0 Tenno / 1 Grineer / 2 Corpus / 3 Infested /
# 4 Orokin(堕落) / 5 Sentient / 6 Wild）
_FACTIONS = ["Tenno", "Grineer", "Corpus", "Infested", "Orokin",
             "Sentient", "Wild"]

# 英文名 -> 官方简中（沿用插件里那张人工核过的表，只收有把握的；其余留英文）
_ENEMY_ZH = {
    "Ancient Disruptor": "远古干扰者", "Ancient Healer": "远古治愈者",
    "Boiler": "痈裂者", "Brood Mother": "病变虫母", "Charger": "疾冲者",
    "Crawler": "爬行者", "Leaper": "奔跳者", "Runner": "狂奔者",
    "Charger": "疾冲者", "Anti MOA": "逆进恐鸟", "MOA": "恐鸟",
    "Fusion MOA": "熔岩恐鸟", "Crewman": "船员", "Elite Crewman": "精英船员",
    "Nullifier Crewman": "虚能船员", "Corrupted Nullifier": "堕落虚能者",
    "Butcher": "屠夫", "Flameblade": "焰刃", "Powerfist": "强拳",
    "Scorpion": "天蝎", "Shield Lancer": "盾枪兵", "Ballista": "弩炮",
    "Eviscerator": "开膛者", "Hellion": "行刑者", "Lancer": "枪兵",
    "Elite Lancer": "精英枪兵", "Scorch": "怒焚者", "Seeker": "追踪者",
    "Trooper": "骑兵", "Bombard": "轰击者", "Commander": "指挥官",
    "Drahk Master": "爪喀驯兽师", "Heavy Gunner": "重型机枪手",
    "Hyekka Master": "鬣猫驯兽师", "Manic": "狂躁者", "Napalm": "火焰轰击者",
    "Nox": "诺克斯", "Ghoul Auger": "尸鬼钻地者", "Ghoul Devourer": "尸鬼吞噬者",
    "Ghoul Expired": "尸鬼腐化者", "Ghoul Rictus": "尸鬼狞笑者",
    "Grineer Warden": "Grineer 典狱长", "Sensor Regulator": "传感器调节器",
    "Corrupted Ancient": "远古堕落者", "Corrupted Bombard": "堕落轰击者",
    "Corrupted Butcher": "堕落屠夫", "Corrupted Crewman": "堕落船员",
    "Corrupted Heavy Gunner": "堕落重型机枪手",
    "Corrupted Lancer": "堕落枪兵", "Corrupted MOA": "堕落恐鸟",
    "Bailiff": "法警", "Wolf of Saturn Six": "土星六号之狼",
    "Eidolon Teralyst": "夜灵兆力使", "Eidolon Gantulyst": "夜灵巨力使",
    "Eidolon Hydrolyst": "夜灵水力使", "Profit-Taker Orb": "利润收割者圆蛛",
    "Tusk Butcher": "巨牙屠夫", "Tusk Lancer": "巨牙枪兵",
    "Tusk Bombard": "巨牙轰击者", "Tusk Predator": "巨牙掠食者",
    "Kuva Lich": "赤毒玄骸", "Kuva Guardian": "赤毒守卫者",
    "Narmer Lancer": "合一众枪兵", "Deimos Carnis": "魔胎之境的肉类",
    "Juno Crewman": "朱诺船员", "Terra Crewman": "泰拉船员",
    "Terra MOA": "泰拉恐鸟", "Terra Provisor": "泰拉供给者",
    "Vapos Crewman": "瓦波斯船员", "Murex": "骨螺",
    "Thrax Centurion": "凶魂百夫长", "Thrax Legatus": "凶魂使节",
    "Corrupted Vor": "堕落的 Vor",
}


def build_enemies() -> dict:
    src = CACHE / "codex_enemy.ts"
    if not src.exists():
        print("⚠️ 跳过敌人表：缺 " + str(src))
        return {}
    txt = src.read_text(encoding="utf-8", errors="ignore")
    body = txt[txt.index("const _enemyList = ["):txt.index("] as [string")]
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
            "head_mul": g(10, 2),
        }
        out[name] = rec
    (DATA / "enemies.json").write_text(
        json.dumps({"_meta": {"source": "riven-mirror src/warframe/codex/enemy.ts",
                              "retrieved": "2026-09-16",
                              "note": "仅取基准数值与基准等级；抗性规则以 damage_faction.json 为准"},
                    "enemies": out},
                   ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"enemies.json：{len(out)} 条（有中文名 {sum(1 for v in out.values() if v['zh'])}）")
    return out


# ---------------------------------------------------------------------------
# 4) MOD 数值表（来源：WFCD warframe-items 的 Mods.json，含满级数值与官方简中名）
# ---------------------------------------------------------------------------
_ELEM_WORDS = {"Heat": "heat", "Cold": "cold", "Electricity": "electricity",
               "Toxin": "toxin", "Blast": "blast", "Corrosive": "corrosive",
               "Gas": "gas", "Magnetic": "magnetic", "Radiation": "radiation",
               "Viral": "viral", "Void": "void"}
# 物理三系 MOD（Sweeping Serration / Sawtooth Clip…）：只加成**同类型的基础值**，
# 且对没有该类型基础伤害的武器无效（wiki Damage「Physical Damage」段）
_PHYS_WORDS = {"Impact": "impact", "Puncture": "puncture", "Slash": "slash"}
# 只把这些关键词的描述留作「使用限制」提示（火炮弹幕的「射速不可修改」等）
_NOTE_KEYS = ("cannot", "only compatible", "not compatible", "exclusive")
_FACTION_WORDS = {"Grineer": "Grineer", "Corpus": "Corpus", "Infested": "Infested",
                  "Orokin": "Orokin", "Sentient": "Sentient",
                  "Sentients": "Sentient", "Murmur": "The Murmur",
                  "Murmurs": "The Murmur", "Grineers": "Grineer",
                  "Corpuss": "Corpus", "Infesteds": "Infested",
                  "Narmer": "Narmer", "Scaldra": "Scaldra", "Techrot": "Techrot"}


def parse_mod_stats(stats: list[str]) -> dict:
    """把 levelStats 的文本解析成计算器参数（只认我们能算的项）。"""
    eff: dict = {}
    # ⚠️ 逐行解析，**不能只取第一行**：像 Power Throw 的投掷伤害就写在第二行
    #    （`On Consecutive throw (Max stacks 3):\n+100% Throw Damage`），
    #    原来 `split("\n")[0]` 会把它整条丢掉（用户 2026-09-16 报的就是这个）
    for raw_stat in stats or []:
        # 换行有两种形态：真实换行、以及 DE 文本里**字面的 "\\n" 两字符**
        # （Power Throw 用的就是后者）——统一成真实换行后，再把「标题: 换行 数值」
        # 接回一行，否则 `On Consecutive throw (Max stacks 3):` 与
        # `+100% Throw Damage` 会被拆开，两边都匹配不上
        for s in str(raw_stat).replace("\\n", "\n").replace(":\n", ": ").split("\n"):
            s = s.strip()
            if not s:
                continue
            s = s.replace("<DT_FIRE_COLOR>", "").replace("<DT_FREEZE_COLOR>", "")
            s = s.replace("<DT_ELECTRICITY_COLOR>", "").replace("<DT_POISON_COLOR>", "")
            s = re.sub(r"<DT_[A-Z_]+>", "", s).strip()
            # ① 连续投掷（奋力一掷）：每层 +X% 投掷伤害，最多 N 层
            m_throw = re.match(
                r"On Consecutive throw\s*\(Max stacks (\d+)\)\s*:\s*"
                r"\+([\d.]+)%\s+Throw Damage", s, re.I)
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
                elif what in ("Melee Damage On Heavy Attack",
                              "Damage On Heavy Attack"):
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
                elif what.lower().startswith("critical chance") and                         re.search(r"combo", what, re.I):
                    # 狂怒（Blood Rush）：+40% 暴击几率「随连击倍率」叠加
                    eff["crit_per_combo"] = eff.get("crit_per_combo", 0.0) + pct
                elif what.lower().startswith("status chance") and                         re.search(r"combo", what, re.I):
                    # 创口溃烂（Weeping Wounds）：+40% 触发率「随连击倍率」
                    eff["status_per_combo"] = eff.get("status_per_combo", 0.0) + pct
                elif re.search(r"per\s+Status\s+Type", what, re.I):
                    # 异况超量 / 镀层战术等：目标身上每种异常 +N% 直伤
                    eff["dmg_per_status"] = eff.get("dmg_per_status", 0.0) + pct
                elif what in _ELEM_WORDS:
                    el = _ELEM_WORDS[what]
                    eff.setdefault("elements", {})[el] = \
                        eff.get("elements", {}).get(el, 0.0) + pct
                elif what in _PHYS_WORDS:
                    el = _PHYS_WORDS[what]
                    eff.setdefault("physical", {})[el] = \
                        eff.get("physical", {}).get(el, 0.0) + pct
                elif what.startswith("Damage to "):
                    fac = what[len("Damage to "):].strip()
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
            m_conv = re.fullmatch(
                r"([\d.]+)%\s+of\s+Damage\s+converted\s+into\s+(.+)", s, re.I)
            if m_conv:
                _to = _PHYS_WORDS.get(m_conv.group(2).strip())
                if _to:
                    eff.setdefault("physical_convert", {})[_to] =                         float(m_conv.group(1))
                continue
            # ④ 条件触发（On Kill / On Headshot / When …）：**一律不折进数值**，
            #    只在卡面列出原文。把它们当常驻加成会系统性高估。
            if re.match(r"(on\s|when\s|while\s|if\s)", s, re.I) or                     re.search(r"stacks?\s+up\s+to", s, re.I):
                eff.setdefault("conditional", []).append(str(raw_stat)[:120])
                continue
    return eff


def _mod_compat(it: dict) -> str:
    return (it.get("type") or "").replace(" Mod", "")


# 只有这些槽位的 MOD 能被塞进武器配卡（战甲/守护/赋能等不参与，免得名表里
# 混进「聚精会神」这类同名/近名条目，把武器名与 MOD 名搅在一起）
WEAPON_COMPAT = {"Primary", "Secondary", "Melee", "Shotgun", "Stance",
                 "Plexus", "Arch-Gun", "Arch-Melee", "Necramech"}


def build_mods() -> dict:
    name_zh = json.loads((DATA / "de" / "name_zh.json").read_text(encoding="utf-8"))
    items = json.loads(_fetch_cached(
        "https://cdn.jsdelivr.net/npm/warframe-items@latest/data/json/Mods.json",
        "Mods.json"))
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
                    "name": nm, "zh": zh, "compat": compat,
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
        for e in ls[:fl + 1]:
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
            "drain_at_max": (int(base_drain) + int(fl))
                             if isinstance(base_drain, int) else None,
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
        if old is None or (variant, -_eff_weight(eff)) < (old["_variant"],
                                                          -_eff_weight(old["effects"])):
            rec["_variant"] = variant
            out[key] = rec
    (DATA / "mods_stats.json").write_text(
        json.dumps({"_meta": {"source": "WFCD warframe-items Mods.json",
                              "retrieved": "2026-09-16",
                              "note": "mods=能参与计算的（满级值）；"
                                      "names=可识别的全量 MOD 名（可能无词条）"},
                    "mods": out, "names": index},
                   ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"mods_stats.json：可算 {len(out)} 条 / 可识别 {len(index)} 条"
          f"（有中文名 {sum(1 for v in index.values() if v['zh'])}）")
    return out


def _eff_weight(eff: dict) -> float:
    return (eff.get("base_dmg", 0) + eff.get("heavy_dmg", 0)
            + eff.get("throw_dmg", 0)
            + eff.get("multishot", 0)
            + eff.get("crit_chance", 0) + eff.get("crit_dmg", 0)
            + sum((eff.get("elements") or {}).values())
            + sum((eff.get("physical") or {}).values())
            + eff.get("headshot_bonus", 0)
            + eff.get("faction_dmg", 0)
            + (eff.get("faction_mul", 1.0) - 1.0) * 100)


# 武器类赋能（战甲/指挥官/增幅器的不进武器计算）
_WEAPON_ARCANE_TYPES = ("Primary Arcane", "Secondary Arcane", "Melee Arcane",
                        "Shotgun Arcane", "Bow Arcane", "Kitgun Arcane",
                        "Zaw Arcane")


def build_arcanes() -> dict:
    """构建武器赋能表（每级数值 + 效果文本）。

    赋能多为**条件触发**（"On Critical Hit: 60% chance for +60% Melee Damage"），
    这里如实保存文本与数值；是否计入由计算层决定（默认只在卡面标注）。
    """
    name_zh = json.loads((DATA / "de" / "name_zh.json").read_text(encoding="utf-8"))
    items = json.loads(_fetch_cached(
        "https://cdn.jsdelivr.net/npm/warframe-items@latest/data/json/Arcanes.json",
        "Arcanes.json"))
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
            levels.append(parse_mod_stats(((e or {}).get("stats") or [])))
        flat_text = " ".join(
            " ".join((e or {}).get("stats") or []) for e in ls).replace("\\n", " ")
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
        json.dumps({"_meta": {"source": "WFCD warframe-items Arcanes.json",
                              "retrieved": "2026-09-16",
                              "note": "只收武器类；数值为满级值"},
                    "arcanes": out},
                   ensure_ascii=False, indent=1), encoding="utf-8")
    with_zh = sum(1 for v in out.values() if v["zh"])
    print(f"arcanes_stats.json：{len(out)} 条（有中文名 {with_zh}）")
    return out


if __name__ == "__main__":
    if "-x" in sys.argv:
        PROXY = sys.argv[sys.argv.index("-x") + 1]
    build_faction_table()
    build_weapons()
    build_arcanes()
    build_enemies()
    build_mods()
