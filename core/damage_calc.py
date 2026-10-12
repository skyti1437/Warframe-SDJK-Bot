# -*- coding: utf-8 -*-
"""伤害计算器（v1）：配卡对敌即时伤害估算。

纯本地计算，数据来自 core/data/weapons_stats.json（WFCD warframe-items 裁剪）
与 core/data/damage_faction.json（派系倍率表）。

**机制版本基准（2026-09-16 核对英文 wiki）**
* U36《Jade Shadows》抗性重构：血量/护甲/护盾各只有一种类型，
  **弱点/抗性按派系决定**：Vulnerable = ×1.5、Resistant = ×0.5，其余 ×1
  （来源 fandom「Damage/Overview Table」）。
* 敌人护甲随等级缩放（fandom「Enemy Level Scaling」）：
      f1 = 1 + 0.005·d^1.75   （d = 当前等级 - 基准等级，d < 70）
      f2 = 1 + 0.4·d^0.75     （d > 80）
      70~80 之间按 smoothstep 过渡；**上限 2700（= 90% 减伤），下限 200**；
      钢铁之路不再加成护甲。
* 伤害减免：DR = 护甲 / (护甲 + 300)。
* MOD 乘区：基伤 → 元素（MOD 元素 = 百分比 × 上一步后的基伤总量，
  同元素相加后按 HCET（火>冰>电>毒）顺序两两合成复合元素）→ 派系 →
  目标派系弱点倍率。
* 暴击期望（等级内插值）：CC ≤ 100% 时 = 1 + CC×(CM-1)；
  CC > 100% 时 = CM × (floor(CC) + 小数部分)（红暴按层数线性叠加）。
* 爆头：人形头部 ×3（2026-10-09 B2，wiki Enemy_Body_Parts / wfsim MECHANICS.md:1001-1003）；
  指定敌人且其 head_mul 有来源时按该敌人的头部倍率（null = 未分类，按人形计）。
  2026-10-10 爆头口径批：爆头倍率 = 基数 × (1 + Σ爆头倍率加成)（加成放大整个部位倍率，
  灵化进化爆头并入 Σ，Cernos Prime 自带 +50% 独立乘算）；可暴击爆头的目标头部暴伤 ×2
  （Corpus 人形/地面单位等不触发，见 headshot_profile）。

**v1 未计入（输出会注明）**：异常状态流程（病毒加伤/腐蚀剥甲/切割 DoT）、
重击/连击、裂隙简化、Overguard、敌人伤害衰减（Sentient 类）。

**v1.1 补上的东西（2026-09-16，玩家反馈「不同元素算出来一个数」）**
U36 之后**没有「元素 × 护甲/血型」倍率表**了（血量/护甲/护盾各只剩一种类型），
元素之间的差距只剩两处来源，本模块现在都能算：
1. **派系弱点**：±50%，见下（例如 G系 弱 冲击/腐蚀）；
2. **异常状态**（数值取自 fandom「Status Effect」主表）：
   * 病毒 N 层：对**血量**伤害 ×(1 + 100% + 25%×(N-1))，N≤10（满层 +325%）
   * 磁力 N 层：对**护盾/超宏**伤害同公式（满层 +325%）
   * 腐蚀 N 层：**剥甲** 26% + 6%×(N-1)，满 10 层 −80%（先剥甲再算减伤）
   * 火剥甲：护甲 −50%
   * DoT（6 秒总量，按「每秒 ratio × 基础伤害」×6 计）：
     切割 35%（**无视护甲**）、火焰 50%、电击 50%、毒素 50%（**无视护盾**）、毒气 50%
     —— 派系 MOD 对 DoT **结算两次**（wiki 明确），本模块已按此实现
"""

from __future__ import annotations

import copy
import difflib
import functools
import json
import math
import re
from pathlib import Path
from typing import Optional

try:
    from . import matching  # core 包内正常导入
except ImportError:
    # ★ 只在**非包上下文**（离线脚本把 core/ 当顶层路径）才回退绝对导入；
    #   包内失败 = 真错误，原样抛出（2026-09-25 事故：兜底把真错掩盖成
    #   "No module named 'core'"，见 main.py 同处注释）。
    if __package__:
        raise
    import matching

_DATA = Path(__file__).resolve().parent / "data"
_cache: dict = {}


def _load() -> dict:
    if not _cache:
        _cache["weapons"] = json.loads((_DATA / "weapons_stats.json").read_text(encoding="utf-8"))
        fac = json.loads((_DATA / "damage_faction.json").read_text(encoding="utf-8"))
        # 表键统一小写：武器数据里的伤害类型键也是小写（impact/slash/…）
        _cache["fac_table"] = {k.lower(): v for k, v in fac["table"].items() if isinstance(v, dict)}
        try:
            _cache["enemies"] = json.loads((_DATA / "enemies.json").read_text(encoding="utf-8"))[
                "enemies"
            ]
        except Exception:  # noqa: BLE001
            _cache["enemies"] = {}
        try:
            raw = json.loads((_DATA / "mods_stats.json").read_text(encoding="utf-8"))
            # 名字索引（可识别但未必能算）先铺底，能算的再覆盖上去：
            # 「锁定目标」这类暂不参与计算的卡也必须认得出来，
            # 否则会被当成武器名的一部分，导致武器查不到、整条指令无输出。
            mods = {k: {**v, "numeric": False} for k, v in (raw.get("names") or {}).items()}
            mods.update({k: {**v, "numeric": True} for k, v in (raw.get("mods") or {}).items()})
            _cache["mods"] = mods
        except Exception:  # noqa: BLE001
            _cache["mods"] = {}
        try:
            # wfsim 补充元数据：家族互斥 + 结构化条件堆叠（scripts/
            # enrich_mods_from_wfsim.py 生成；key=mods_stats 的 key 小写）
            _cache["mods_extra"] = json.loads(
                (_DATA / "mods_wfsim_extra.json").read_text(encoding="utf-8")
            )
        except Exception:  # noqa: BLE001
            _cache["mods_extra"] = {}
    return _cache


def mod_extra(mod: dict) -> dict:
    """按 MOD 记录（name 字段）取 wfsim 补充元数据（family/conditionals）。"""
    if not isinstance(mod, dict):
        return {}
    nm = str(mod.get("name") or "").lower()
    return (_load().get("mods_extra") or {}).get(nm) or {}


# ---------------------------------------------------------------------------
# 敌人血量/护盾缩放（wiki「Enemy Level Scaling」；护甲见下面 armor_multiplier）
# 结构同护甲：f1（等级差<70）/ f2（>80），70-80 之间 smoothstep
# ---------------------------------------------------------------------------
HEALTH_SCALE = {  # (系数1, 指数1, 系数2, 指数2)
    "Grineer": (0.015, 2.12, 24 * 5**0.5 / 5, 0.72),
    "Corpus": (0.015, 2.12, 30 * 5**0.5 / 5, 0.55),
    "Infested": (0.0225, 2.12, 36 * 5**0.5 / 5, 0.72),
    "Orokin": (0.015, 2.10, 24 * 5**0.5 / 5, 0.685),
    "_default": (0.015, 2.00, 24 * 5**0.5 / 5, 0.5),
}
SHIELD_SCALE = {
    "Corpus": (0.02, 1.76, 2.0, 0.76),
    "Orokin": (0.02, 1.75, 2.0, 0.75),
    "Grineer": (0.02, 1.75, 1.6, 0.75),
    "_default": (0.02, 1.75, 2.0, 0.75),
}


def _scale_two(x: float, d: float, c1: float, e1: float, c2: float, e2: float) -> float:
    f1 = 1 + c1 * d**e1
    if d <= 70:
        return f1
    f2 = 1 + c2 * d**e2
    if d >= 80:
        return f2
    t = (d - 70) / 10.0
    s = 3 * t * t - 2 * t**3
    return f1 * (1 - s) + f2 * s


def health_multiplier(faction: str, level: int, base_level: int = 1) -> float:
    c = HEALTH_SCALE.get(faction, HEALTH_SCALE["_default"])
    return _scale_two(level, max(0, level - base_level), *c)


def shield_multiplier(faction: str, level: int, base_level: int = 1) -> float:
    c = SHIELD_SCALE.get(faction, SHIELD_SCALE["_default"])
    return _scale_two(level, max(0, level - base_level), *c)


def _best_by_name(query: str, items: list[dict], fuzzy: bool = True) -> Optional[dict]:
    """通用名字匹配：精确 → 子串（多个取最短、最不啰嗦的那个）→ 模糊（difflib）。

    模糊这一步是必要的：玩家会说「重机枪手」，而官方名是「重型机枪手」。
    """
    q = _norm(query)
    if not q:
        return None
    cand = sorted(items, key=lambda r: len(_norm(r.get("zh") or r.get("name"))))
    for rec in cand:
        if _norm(rec.get("zh")) == q or _norm(rec.get("name")) == q:
            return rec
    subs = [r for r in cand if q in _norm(r.get("zh")) or q in _norm(r.get("name"))]
    if subs:
        return subs[0]
    pool = {}
    for r in cand:
        for nm in (r.get("zh"), r.get("name")):
            key = _norm(nm)
            if key and key not in pool:
                pool[key] = r
    if not fuzzy:
        return None
    near = difflib.get_close_matches(q, list(pool), n=2, cutoff=0.72)
    if near:
        return pool[near[0]]
    return None


def find_enemy_exact(query: str) -> Optional[dict]:
    """敌人在**归一化后完全相等**时命中（英文名、中文名或 aliases 额外叫法）。"""
    q = _norm(query)
    if not q:
        return None
    for e in _load()["enemies"].values():
        if _norm(e.get("zh")) == q or _norm(e.get("name")) == q:
            return e
    for e in _load()["enemies"].values():
        if any(_norm(a) == q for a in e.get("aliases") or ()):
            return e
    return None


def enemy_miss_line(name: str, faction: Optional[str], enemy: Optional[dict] = None) -> str:
    """「对 XXX」认不出敌人时的卡面提示：按默认派系计，并列出常用敌人表里可用的名字。"""
    names = [e.get("zh") or e.get("name") for e in _load()["enemies"].values()]
    if enemy:  # 后面的词另外认出了敌人（如「对 Elite Lancer」→ Lancer）
        how = f"（已忽略），按「{enemy.get('zh') or enemy.get('name')}」计算"
    else:
        how = f"，已按未指定敌人（{FACTION_ZH.get(faction, faction) or '默认派系'}）计算"
    return f"⚠ 「{name}」不在常用敌人表{how}；可用的有：{'、'.join(n for n in names if n)}"


def find_enemy(query: str) -> Optional[dict]:
    """按中文名/英文名找敌人（数据来自极镜的 codex/enemy.ts 基准数值表，支持模糊）。"""
    return find_enemy_exact(query) or _best_by_name(query, list(_load()["enemies"].values()))


_arcanes_cache: dict = {}
_stances_cache: dict = {}


def stances_payload() -> dict:
    """架势表（131 条，来自 wiki `Module:Stances/data` 的 Lua 数据）。

    同样是**独立缓存**（理由同 arcanes）。
    """
    if not _stances_cache:
        try:
            d = json.loads((_DATA / "stances_stats.json").read_text(encoding="utf-8"))
            _stances_cache["stances"] = d.get("stances") or {}
        except Exception:  # noqa: BLE001
            _stances_cache["stances"] = {}
    return _stances_cache["stances"]


def resolve_stance(mod: dict) -> Optional[dict]:
    """把架势 MOD 记录解析成 wiki 的架势数据（命中不了则返回 None）。"""
    if not mod:
        return None
    pool = stances_payload()
    for cand in (_norm(mod.get("name")), _norm(mod.get("zh"))):
        if cand and cand in pool:
            return pool[cand]
    return None


def arcanes_payload() -> dict:
    """武器赋能表（54 条，带每级数值与效果原文）。

    ⚠️ 用**独立缓存**，不能借 `_cache`：`_load()` 是「_cache 为空才填充」，
    先在这里塞一个 arcanes 键会让 _load() 直接跳过 → 武器表读不到（KeyError）。
    """
    if not _arcanes_cache:
        try:
            d = json.loads((_DATA / "arcanes_stats.json").read_text(encoding="utf-8"))
            _arcanes_cache["arcanes"] = d.get("arcanes") or {}
        except Exception:  # noqa: BLE001
            _arcanes_cache["arcanes"] = {}
    return _arcanes_cache["arcanes"]


def find_arcane(query: str) -> Optional[dict]:
    """按中文名/英文名找武器赋能（支持「滑射」这种子串）。"""
    pool = arcanes_payload()
    q = _norm(query)
    if not q:
        return None
    for rec in pool.values():
        if q == _norm(rec.get("zh")) or q == _norm(rec.get("name")):
            return rec
    hits = [r for r in pool.values() if q in _norm(r.get("zh")) or q in _norm(r.get("name"))]
    if len(hits) == 1:
        return hits[0]
    near = difflib.get_close_matches(
        q, [_norm(r.get("zh")) for r in pool.values() if r.get("zh")], n=1, cutoff=0.72
    )
    if near:
        for r in pool.values():
            if _norm(r.get("zh")) == near[0]:
                return r
    return None


def find_mod_exact(query: str) -> Optional[dict]:
    """MOD 精确匹配（归一化后完全等于英文名或中文名）。"""
    q = _norm(query)
    if not q:
        return None
    for m in _load()["mods"].values():
        if _norm(m.get("zh")) == q or _norm(m.get("name")) == q:
            return m
    return None


def find_mod_sub_unique(query: str) -> Optional[dict]:
    """MOD 子串匹配，**只在唯一命中时**返回。

    为什么必须「唯一」：敌人名/武器名常是某张 MOD 名的子串
    （Butcher→Butcher's Revelry、Comba→Combat Reload、Napalm→Nightwatch Napalm），
    不设限就会把敌人悄悄换成一张卡，还顺手改掉数值。
    """
    q = _norm(query)
    if len(q) < 2:
        return None
    hits = [
        m for m in _load()["mods"].values() if q in _norm(m.get("zh")) or q in _norm(m.get("name"))
    ]
    return hits[0] if len(hits) == 1 else None


def resolve_mod(query: str) -> Optional[dict]:
    """把「膛线 / Hellfire / 地狱火」解析成 MOD 记录（数值为满级值）。

    两级：① 归一化后完全相等 ② 唯一子串命中；**不做 difflib 模糊**
    （否则武器名会被误判成某张卡，宁可匹配不上）。
    """
    return find_mod_exact(query) or find_mod_sub_unique(query)


# ---------------------------------------------------------------------------
# 常量表
# ---------------------------------------------------------------------------
TYPE_ZH = {
    "impact": "冲击",
    "puncture": "穿刺",
    "slash": "切割",
    "cold": "冰冻",
    "electricity": "电击",
    "heat": "火焰",
    "toxin": "毒素",
    "blast": "爆炸",
    "corrosive": "腐蚀",
    "gas": "毒气",
    "magnetic": "磁力",
    "radiation": "辐射",
    "viral": "病毒",
    "void": "虚空",
    "tau": "Tau",
    "true": "真实",
}
_SKIP_TYPES = {"cinematic", "shielddrain", "healthdrain", "energydrain"}

ELEM_ALIASES = {
    "电": "electricity",
    "电击": "electricity",
    "火": "heat",
    "火焰": "heat",
    "冰": "cold",
    "冰冻": "cold",
    "毒": "toxin",
    "毒素": "toxin",
}
# 物理三系（长名在前，正则用它们做交替匹配）
PHYS_ALIASES = {
    "冲击": "impact",
    "穿刺": "puncture",
    "穿": "puncture",
    "切割": "slash",
    "切": "slash",
}

# 回响（赤毒 / 信条 / 终幕）加成元素关键词 → 伤害类型（b8 批）：正则接受的 11 个词全部映射。
# 修前只查 ELEM_ALIASES（火 / 冰 / 电 / 毒），「赤毒冲击25」「赤毒辐射25」「信条磁力60」解析成
# element None ⇒ calculate 回落到可滚列表里第一个非冲击元素（Kuva Bramma 成了火焰 +46.75）。
# 冲击是合法回响元素（core/data/lich_valence.json 各武器 elements；wiki Kuva / Tenet / Coda 页
# 列出 Impact）；不在该武器可滚列表里的（病毒 / 爆炸 / 穿刺 / 切割 …）仍由 calculate 注明。
VALENCE_ALIASES = {
    **ELEM_ALIASES,
    "冲击": "impact",
    "穿刺": "puncture",
    "切割": "slash",
    "辐射": "radiation",
    "磁力": "magnetic",
    "病毒": "viral",
    "爆炸": "blast",
}

# 复合元素合成表（两两）
COMPOSITE = {
    frozenset(("heat", "cold")): "blast",
    frozenset(("heat", "electricity")): "radiation",
    frozenset(("heat", "toxin")): "gas",
    frozenset(("cold", "electricity")): "magnetic",
    frozenset(("cold", "toxin")): "viral",
    frozenset(("electricity", "toxin")): "corrosive",
}
# MOD 元素合成顺序：火 > 冰 > 电 > 毒
HCET = ["heat", "cold", "electricity", "toxin"]

FACTION_ALIASES = {
    "grineer": "Grineer",
    "g系": "Grineer",
    "g": "Grineer",
    "corpus": "Corpus",
    "c系": "Corpus",
    "c": "Corpus",
    "amalgam": "Corpus Amalgam",
    "奥布": "Corpus Amalgam",
    "合体": "Corpus Amalgam",
    "infested": "Infested",
    "i系": "Infested",
    "i": "Infested",
    "异融": "Infested",
    "deimos": "Infested Deimos",
    "魔胎": "Infested Deimos",
    "orokin": "Orokin",
    "奥罗金": "Orokin",
    "虚空": "Orokin",
    "sentient": "Sentient",
    "sentients": "Sentient",
    "感灵": "Sentient",
    "narmer": "Narmer",
    "合一众": "Narmer",
    "murmur": "The Murmur",
    "低语者": "The Murmur",
    "低语": "The Murmur",
    "zariman": "Zariman",
    "扎里曼": "Zariman",
    "scaldra": "Scaldra",
    "炽蛇军": "Scaldra",
    "techrot": "Techrot",
    "科腐者": "Techrot",
    "anarch": "Anarchs",
    "anarchs": "Anarchs",
}
FACTION_ZH = {
    "Grineer": "Grineer（G系）",
    "Kuva Grineer": "赤毒 Grineer",
    "Corpus": "Corpus（C系）",
    "Corpus Amalgam": "Corpus 合体体",
    "Infested": "Infested（I系）",
    "Infested Deimos": "魔胎之境 Infested",
    "Orokin": "Orokin（奥罗金）",
    "Sentient": "Sentient",
    "Narmer": "合一众 Narmer",
    "The Murmur": "低语者 The Murmur",
    "Zariman": "扎里曼",
    "Scaldra": "炽蛇军 Scaldra（1999）",
    "Techrot": "科腐者 Techrot（1999）",
    "Anarchs": "Anarchs（The Old Peace 新派系：冲击/电 +50%，抗辐射 −50%）",
}
# 各派系默认基准甲（中型单位量级；可用「基甲N」覆盖）
DEFAULT_BASE_ARMOR = {"Grineer": 150, "Kuva Grineer": 150, "Narmer": 150}

# DoT（持续伤害）系数：每秒伤害 = 系数 × MOD 后的基础伤害（wiki「Status Effect」）
# ⚠️ 这里只放**每秒型** DoT；爆炸（blast）是「1.5s 后一次性 30%」，不进这张表，
#    只在异常稳态模型（STATUS_TABLE 里带 instant 标记）里结算
DOT_RATIO = {"slash": 0.35, "heat": 0.5, "electricity": 0.5, "toxin": 0.5, "gas": 0.5}
DOT_BYPASS_ARMOR = {"slash"}  # 切割流血无视护甲
DOT_BYPASS_SHIELD = {"toxin"}  # 毒素无视护盾
DOT_SECONDS = 6  # 表中均为「每秒 × 6 秒」
# 电 / 毒气 DoT 的「落点」层（2026-10-10 第四批）：wiki Enemy_Body_Parts §Notes 把「Electricity
# and Gas status procs」列进「always defaulting to the 1x multiplier, unaffected by acuity-like
# bonuses but affected by deadhead-like bonuses」。wfsim 的实现与实测（fight/debuffs.rs:198-205
# lands_on_a_part；fight/pellet.rs:562-564 head_landing = (1+爆头加成)×(1+武器自带独立乘区)；
# fight/procs.rs:650-665 种子照乘命中部位 part_factor、另乘 landing；docs/measurements/M100：
# 电 身体 24 / 爆头 234，毒素爆头 130）：**种子仍吃命中部位的整条头部倍率（与其它 DoT 同）**，
# 电/毒气的 tick 另外落在头上时再乘「1x 基数 × 爆头加成括号」。⇒ 无爆头加成时与其它 DoT 一样，
# 有加成时电/毒气多乘一次 (1+Σ加成)。暴击爆头只在种子里（与其它 DoT 同），落点层不含暴击。
DOT_HEAD_LANDING = frozenset(("electricity", "gas"))
# 火焰（Heat）单例累加（2026-10-10 第三批，#12）：wfsim data/debuffs/ignite.yaml:14-22,26-32
# 「Each proc does not create a new instance - it (a) ADDS its damage contribution into the
# single entity's tick value and (b) REFRESHES the entity's one shared clock」、max_stacks: null
# 「indefinite ramp while kept refreshed」。持续开火 T 秒、火触发率 r（次/秒）、相邻两次触发
# 间隔 ≤ 6s（r×6 ≥ 1，单例不过期）时，层数从 0 线性涨到 r×T，**开火窗口内平均 r×T/2 层**；
# 间隔 > 6s 时每层独立过期，平均 r×6 层（与其它 DoT 同）。默认 T = 12s ⇒ r×12/2 = r×6，
# 与修前「rate × 6」逐位相同（乘 6.0 同一个浮点数），默认数值零变化。
FIRE_SECONDS_DEFAULT = 12.0
FIRE_SECONDS_MIN, FIRE_SECONDS_MAX = 1.0, 600.0
_PHYS_TYPES = ("impact", "puncture", "slash")  # 物理三系（转换类 MOD 只在它们之间搬）
# 效果键 → spec 键（两边命名不一致的只有射速）。条件叠层（mods_wfsim_extra 的
# conditionals[].field）与赋能折算都按效果键写，必须经此映射：2026-10-09 修前
# field="fire_rate" 写进了无人读取的 spec["fire_rate"]，「满镀层」射速加成从未生效。
_EFFECT_TO_SPEC = {"fire_rate": "fire_rate_pct"}
# 弓 / 弩（射速 MOD 翻倍的武器类别，key = 小写 uniqueName）。
# 依据：射速 MOD 卡面「(x2 for Bows)」、wiki Speed_Trigger 的「Fire rate (Bows)」列恰为步枪列 2 倍；
# 弩：wiki Zhuge / Zhuge_Prime / Attica / Nagantaka / Nagantaka_Prime 均写
# 「Counts as a bow in regards to fire rate mods, doubling the fire rate bonus」。
# 名单取 wfsim data/weapons/primary/*.yaml 的 `class: bow|crossbow`（panel.rs:545-548，
# 2026-10-10 HEAD 8267f09，23 个带 internal_name 的条目；Nunchasa、Daikyu Prime
# 我方库暂无，留着无害）。我方 weapons_stats 里弓/弩的
# type 都是 "Rifle"、无类别字段，uniqueName 路径也不可靠（Ballistica/Bolto/Stug 含 Crossbow
# 却是手枪类，MK1-Paris 不含 Bow）⇒ 用显式名单。
BOW_CLASS_UNIQUE = frozenset(
    u.lower()
    for u in (
        # bow
        "/Lotus/Weapons/Corpus/Bow/Longbow/CrpBow",  # Lenz
        "/Lotus/Weapons/Corpus/Bow/Longbow/PrismaLenz/PrismaLenzWeapon",
        "/Lotus/Weapons/Grineer/Bows/GrnBow/GrnBowWeapon",  # Kuva Bramma
        "/Lotus/Weapons/Infested/Bow/InfCernosBow/InfCernos",  # Mutalist Cernos
        "/Lotus/Weapons/MK1Series/MK1Paris",
        "/Lotus/Weapons/Syndicates/RedVeil/Bows/RVCernos",  # Rakta Cernos
        "/Lotus/Weapons/Tenno/Bows/AntlerBow/AntlerBow",  # Cernos
        "/Lotus/Weapons/Tenno/Bows/AsymetricalBow/AsymetricalBow",  # Daikyu
        "/Lotus/Weapons/Tenno/Bows/DaxDuviriAsymetricalBow/DaxDuviriAsymmetricalLongBowPlayerWeapon",
        "/Lotus/Weapons/Tenno/Bows/DuelistBow/DuelistBow",  # Nunchasa
        "/Lotus/Weapons/Tenno/Bows/HuntingBow",  # Paris
        "/Lotus/Weapons/Tenno/Bows/Omicrus/OmicrusPlayerWep",  # Nataruk
        "/Lotus/Weapons/Tenno/Bows/PrimeCernos/PrimeCernos",
        "/Lotus/Weapons/Tenno/Bows/PrimeDaikyu/PrimeDaikyuBow",
        "/Lotus/Weapons/Tenno/Bows/PrimeDerelictCernos/DerelictCernos",  # Proboscis Cernos
        "/Lotus/Weapons/Tenno/Bows/PrimeHuntingBow",  # Paris Prime
        "/Lotus/Weapons/Tenno/Bows/StalkerBow",  # Dread
        "/Lotus/Weapons/Tenno/Bows/TnChoirBow/TnChoirBow",  # Evensong
        # crossbow（射速 MOD 规则上按弓算）
        "/Lotus/Weapons/Tenno/LongGuns/GarudaCrossbow/GarudaCrossbow",  # Nagantaka
        "/Lotus/Weapons/Tenno/LongGuns/PrimeNagantaka/PrimeNagantakaWeapon",
        "/Lotus/Weapons/Tenno/LongGuns/PrimeZhuge/PrimeZhugeCrossbow",
        "/Lotus/Weapons/Tenno/LongGuns/RepeatingCrossbow/RepeatingCrossbow",  # Zhuge
        "/Lotus/Weapons/Tenno/LongGuns/TnoPrmryXbow/TnoPrmryXbowWeapon",  # Attica
    )
)
# 人形头部倍率（wiki Enemy_Body_Parts「humanoid head 3.0x」，wfsim docs/MECHANICS.md:1001-1003、
# data/enemies/crewman.yaml 等 head 3.0）。2026-10-09 B2 由 2.0 改 3.0。
HEADSHOT_BASE = 3.0
# 2026-10-10：「未分类」不再用数值 2 当哨兵 —— enemies.json 的 head_mul 为 null 才是未分类
# （按人形计）；head_none=true = 没有头部（爆头不成立）；出处见各条 head_src，
# 生成/补丁脚本 scripts/merge_wfsim_enemies.py --head-only。
HEADSHOT_SOURCE = "人形头部 ×3（wiki Enemy_Body_Parts，经 wfsim 核对）"
# 暴击爆头（wiki Critical_Hit §Critical Headshots / Enemy_Body_Parts §Critical Headshots）：
# 头部暴击时暴伤 ×2 进分级公式 —— wfsim fight/pellet.rs:569-575
#   cd = 2.0 × cd_total（含 MOD 后的总暴伤），crit_multiplier = 1 + tier × (cd − 1)
# 只在「部位倍率 > 1」且该部位带 headcrit 标记时成立。派系默认（逐敌例外写在 head_crit）：
#   可触发：Grineer（含 Corrupted/Narmer/Anarchs 变体）、Scaldra、多数 Infested 与 Sentient
#   不可：Corpus 人形与地面单位（飞行单位 Osprey 类可，见 head_crit）、多数 Murmur、多数 Techrot
HEADCRIT_FACTIONS = frozenset(
    (
        "Grineer",
        "Kuva Grineer",
        "Orokin",
        "Narmer",
        "Anarchs",
        "Scaldra",
        "Infested",
        "Infested Deimos",
        "Sentient",
    )
)
NO_HEADCRIT_FACTIONS = frozenset(("Corpus", "Corpus Amalgam", "The Murmur", "Techrot"))
# 武器自带爆头加成（wiki Enemy_Body_Parts §Headshot Multiplier Bonuses / wfsim data/weapons
# headshot_damage_bonus）：uniqueName → (加成小数, 是否独立乘算)。
# 只有 Cernos Prime 是独立乘区（wiki「Unique +50% Damage Multiplier bonus on headshots,
# multiplicative with other Headshot Multiplier bonuses」；wfsim spec.rs:228-235、
# cernos_prime.yaml headshot_bonus_multiplicative: true），其余并进同一加法括号。
WEAPON_INNATE_HEADSHOT = {
    "/Lotus/Weapons/Tenno/Bows/PrimeCernos/PrimeCernos": (0.5, True),
    "/Lotus/Weapons/Grineer/LongGuns/GrnKuvaLichRifle/GrnKuvaLichRifleWeapon": (0.5, False),
    "/Lotus/Weapons/Infested/LongGuns/InfestedRifle": (0.2, False),  # Synapse
    "/Lotus/Weapons/Infested/InfestedLich/LongGuns/CodaSynapse": (0.2, False),
}
# 武器自带「头部倍率覆盖」（2026-10-10 第三批）：小写 uniqueName → {形态: (倍率, 出处)}。
# 语义（wfsim data/weapons/spec.rs:460-480 `headshot_multiplier`、fight/params.rs:808-817
# `head_value`、fight/pellet.rs:547/569）：该形态打到头部时**用武器的倍率替换敌人头部倍率**；
# 仍算爆头 ⇒ Deadhead 类「爆头倍率加成」照乘；弱点伤害（Acuity）不吃；头部倍率 ≤ 1 ⇒ 无暴击爆头。
# wiki Enemy_Body_Parts §Notes 原文：「Certain weapons do not have bonus damage on headshots,
# always defaulting to the 1x multiplier, unaffected by acuity-like bonuses but affected by
# deadhead-like bonuses」。形态键：default=默认形态（不写形态关键词）/ incarnon=灵化 /
# alt_fire=副开火 / semi_auto=半自动 —— **只在对应形态生效**（wfsim 该字段不随 inherits 继承，
# catalog.rs:61-73 INHERITED 不含它；每个形态条目各自声明）。
# 只收 wfsim 与 wiki 一致的条目；wiki 列了而 wfsim 没收、或两边矛盾的见第三批报告（未收）。
_HS1_LIST = "wiki Enemy_Body_Parts §Notes「always defaulting to the 1x multiplier」"
_HS1_PARIS = "wiki Paris Incarnon Genesis「has a headshot multiplier of 1x」"
_HS1_LEX = "wiki Lex Incarnon Genesis「has a headshot multiplier of 1x」"
WEAPON_HEADSHOT_OVERRIDE: dict = {
    # 默认形态
    "/lotus/weapons/corpus/longguns/crpshotgun/crpshotgun": {  # Arca Plasmor
        "default": (
            1.0,
            "wiki Arca Plasmor「Has a headshot multiplier of 1x」；wfsim arca_plasmor.yaml:41",
        ),
    },
    "/lotus/weapons/corpus/boardexec/primary/crpbearcaplasmor/crpbearcaplasmor": {
        "default": (  # Tenet Arca Plasmor
            1.0,
            "wiki Tenet Arca Plasmor「Has a headshot multiplier of 1x」；"
            "wfsim tenet_arca_plasmor.yaml:47",
        ),
    },
    "/lotus/weapons/tenno/longguns/tnolotuspodshotgun/tnolotuspodshotgun": {  # Steflos
        "default": (1.0, _HS1_LIST + "（Steflos）；wfsim steflos.yaml:41"),
    },
    "/lotus/weapons/tenno/longguns/gyrerifle/gyrerifleweapon": {  # Alternox 主开火
        "default": (
            1.0,
            "wiki Alternox「All firing modes have a headshot multiplier of 1x」/ "
            + _HS1_LIST
            + "（primary fire）；wfsim alternox.yaml:39",
        ),
    },
    "/lotus/weapons/tenno/longguns/primealternox/primealternoxweapon": {  # Alternox Prime
        "default": (
            1.0,
            "wiki Alternox Prime「All firing modes have a headshot multiplier of 1x」；"
            "wfsim alternox_prime.yaml:39",
        ),
    },
    # 灵化形态（Genesis 页原文）
    "/lotus/weapons/tenno/bows/huntingbow": {  # Paris
        "incarnon": (1.0, _HS1_PARIS + "；wfsim paris_incarnon.yaml:40"),
    },
    "/lotus/weapons/tenno/bows/primehuntingbow": {  # Paris Prime
        "incarnon": (1.0, _HS1_PARIS + "；wfsim paris_prime_incarnon.yaml:42"),
    },
    "/lotus/weapons/mk1series/mk1paris": {  # MK1-Paris
        "incarnon": (1.0, _HS1_PARIS + "；wfsim mk1_paris_incarnon.yaml:44"),
    },
    "/lotus/weapons/tenno/bows/stalkerbow": {  # Dread
        "incarnon": (
            1.0,
            "wiki Dread Incarnon Genesis「has a headshot multiplier of 1x」；"
            "wfsim dread_incarnon.yaml:40",
        ),
    },
    "/lotus/weapons/tenno/pistol/heavypistol": {  # Lex
        "incarnon": (1.0, _HS1_LEX + "；wfsim lex_incarnon.yaml:37"),
    },
    "/lotus/weapons/tenno/pistols/primelex/primelex": {  # Lex Prime
        "incarnon": (1.0, _HS1_LEX + "；wfsim lex_prime_incarnon.yaml:38"),
    },
    # 副开火 / 半自动
    "/lotus/weapons/tenno/pistols/tnjetturbine/tnjetturbinepistolweapon": {  # Athodai
        "alt_fire": (1.0, _HS1_LIST + "（Athodai's Alternate Fire）；wfsim athodai_alt.yaml:17"),
    },
    "/lotus/weapons/tenno/pistols/primeathodai/primeathodaipistolweapon": {  # Athodai Prime
        "alt_fire": (
            1.0,
            _HS1_LIST + "（Athodai Prime's Alternate Fire）；wfsim athodai_prime_alt.yaml:17",
        ),
    },
    "/lotus/weapons/tenno/longguns/tnwisprifle/tnwisprifle": {  # Fulmin
        "semi_auto": (
            1.0,
            "wiki Fulmin 半自动「Has a headshot multiplier of 1x」；wfsim fulmin_semi.yaml:26",
        ),
    },
    "/lotus/weapons/tenno/longguns/primefulmin/primefulmin": {  # Fulmin Prime
        "semi_auto": (
            1.0,
            "wiki Fulmin Prime 半自动「Has a headshot multiplier of 1x」；"
            "wfsim fulmin_prime_semi.yaml:21",
        ),
    },
    # 蓄力（2026-10-10 第四批：Nataruk 默认 = 快速射击，完美射击改走「蓄力」形态）
    "/lotus/weapons/tenno/bows/omicrus/omicrusplayerwep": {  # Nataruk
        "charged": (
            1.0,
            "wiki Nataruk「Charged and Perfect Shots have a headshot multiplier of 1x」/ "
            + _HS1_LIST
            + "（Nataruk's charged and perfect shots）；wfsim nataruk_perfect.yaml:21",
        ),
    },
}
# 异常状态数值（层数上限 10）
VIRAL_BASE, VIRAL_STEP = 1.0, 0.25  # 病毒：对血 +100% / 每层 +25%
MAGNETIC_BASE, MAGNETIC_STEP = 1.0, 0.25  # 磁力：对盾/超宏 同上
CORROSIVE_BASE, CORROSIVE_STEP = 0.26, 0.06  # 腐蚀：剥甲 26% / 每层 +6%（满 −80%）
CORROSIVE_MAX = 0.80
HEAT_STRIP = 0.50  # 火剥甲：−50% 护甲


# ---------------------------------------------------------------------------
# 武器查询
# ---------------------------------------------------------------------------
_NAME_INDEX: Optional[dict] = None


def _name_index() -> dict:
    """归一化名 → [武器] 索引（首次构建一次；581 把武器的正则归一很贵，
    之前每次查询都要跑 1700+ 次正则）。"""
    global _NAME_INDEX
    if _NAME_INDEX is None:
        idx: dict = {}
        for v in _load()["weapons"].values():
            for k in (
                _norm(v.get("zh")),
                _norm(v.get("name")),
                _norm((v.get("uniqueName") or "").rsplit("/", 1)[-1]),
            ):
                if k:
                    idx.setdefault(k, []).append(v)
        _NAME_INDEX = idx
    return _NAME_INDEX


def find_weapon_exact(query: str) -> Optional[dict]:
    """只做**精确**武器匹配（中/英文全名）——token 分派时优先用它，
    避免「Soma」这类英文名被敌人的模糊匹配抢走。"""
    q = _norm(query)
    if not q:
        return None
    hit = _name_index().get(q)
    return hit[0] if hit else None


def _norm(s: str) -> str:
    """匹配用的归一化：去空格/间隔号/连字符 + 小写
    （官方中文名形如「赤毒·布拉玛」「赤毒 布拉玛」，玩家输入一般不带分隔符）。"""
    return _norm_cached(s or "")


@functools.lru_cache(maxsize=16384)
def _norm_cached(s: str) -> str:
    # 记忆化：武器/MOD/敌人名是有限集合，但每次模糊查询都要对全表逐条归一；
    # 不缓存时一条 40 词的「伤害」指令要跑约 20 万次正则、在事件循环上卡 1.5 s+
    return _NORM_RE.sub("", s).lower()


_NORM_RE = re.compile(r"[\s·・•\-_'’]+")


def find_weapon(query: str) -> tuple[dict | None, list[dict]]:
    """按中文名/英文名找武器；返回 (精确或最优匹配, 同名候选)。"""
    q = _norm(query)
    if not q:
        return None, []
    exact = _name_index().get(q) or []
    if exact:
        return min(exact, key=lambda v: v.get("masteryReq", 99)), []
    # 变体等价（2026-09-23）：绝路p→绝路prime、沙皇赤毒→赤毒沙皇、kuva沙皇。
    # 只做等价变形，绝不剥变体 token 配 base（配卡计算按变体分别成立）。
    for f in dict.fromkeys(f for f in matching.expand_variants(query) if f != q):
        hit = _name_index().get(f) or []
        if hit:
            return min(hit, key=lambda v: v.get("masteryReq", 99)), []
    weapons = _load()["weapons"]
    sub = [v for v in weapons.values() if q in _norm(v.get("zh")) or q in _norm(v.get("name"))]
    if not sub:
        return None, []
    sub.sort(key=lambda v: (len(_norm(v.get("zh"))), v.get("masteryReq", 99)))
    return sub[0], sub[1:6]


def warmup() -> None:
    """预热：一次性加载全部重 JSON 并建索引（插件启动后台调用）。

    不预热时首次指令要现读 900KB 武器库 + 60 个灵化形态 + 668 个进化
    选项 + 95 把多段 + 部署表，叠加起来会让第一条指令明显变慢。
    """
    _load()
    _name_index()
    _load_evo_data()
    _load_incarnon_forms()
    _load_weapon_attacks()
    _load_deployments()
    _load_co_behavior()
    # 渲染预热已移除（2026-09-17）：字体+暗角缓存只值 ~0.5 s，而启动期
    # 容器 CPU 被占满时这次预热要 3~10 s，日志里还会显示成「预热 50 s」
    # 误导排查。首张卡多花 0.5 s 完全可接受，不值得。


# ---------------------------------------------------------------------------
# 公式
# ---------------------------------------------------------------------------
def armor_multiplier(level: int, base_level: int = 1) -> float:
    """敌人护甲随等级的乘数（U36 后公式，70-80 之间 smoothstep 过渡）。"""
    d = max(0, level - base_level)
    f1 = 1 + 0.005 * d**1.75
    if d <= 70:
        return f1
    f2 = 1 + 0.4 * d**0.75
    if d >= 80:
        return f2
    t = (d - 70) / 10.0
    s = 3 * t * t - 2 * t**3
    return f1 * (1 - s) + f2 * s


def current_armor(level: int, base_armor: float, base_level: int = 1) -> float:
    """当前护甲：上限 2700（90% 减免）、下限 200。"""
    ar = base_armor * armor_multiplier(level, base_level)
    return min(max(ar, 200.0), 2700.0)


def damage_reduction(armor: float) -> float:
    """**敌人**护甲减伤 = 90% × √(护甲/2700)（wiki《Armor》Enemy Damage Reduction）。

    ⚠️ 2026-09-16 修：原实现误用了 Tenno 公式 `护甲/(护甲+300)`（那是玩家护甲）。
    两者差得很远 —— 护甲 2033 时：敌人 **78.1%** vs Tenno 87.1%，
    即旧版把对敌伤害少报了约 40%。护甲 200（下限）→ 24.5%，2700（上限）→ 90%。
    """
    if armor <= 0:
        return 0.0
    return min(0.90 * math.sqrt(armor / 2700.0), 0.90)


def crit_expectation(cc: float, cm: float) -> float:
    """平均暴击倍率 = 1 + 总暴击率 × (总暴伤 − 1)。

    wiki「Critical Hit → Average Damage」给的就是这一条，**对超过 100% 的
    「大暴击/超暴击」同样成立**：第 k 段暴击的倍率是 1+(cm−1)·k，而段数的
    期望恰好等于总暴击率 ⇒ E[倍率] = 1+(cm−1)·cc。

    ⚠️ 2026-09-16 修：旧实现 cc>1 时写成 `cm × cc`，在多段暴击下偏高
    （绝路P 暴率 238% / 暴伤 6.6：旧 ×15.71，正确 ×14.33，高了约 9%）。
    """
    cc = max(0.0, cc)
    return 1 + cc * (cm - 1.0)


def headshot_profile(
    enemy: Optional[dict],
    faction: str,
    unique_name: str,
    bonus_pct: float,
    evo_bonus: float,
    form_kind: str = "default",
) -> dict:
    """爆头倍率与暴击爆头资格（2026-10-10 爆头口径批）。

    * 爆头倍率 = 头部基数 × (1 + Σ爆头倍率加成)。加成放大**整个**部位倍率：
      wiki Enemy_Body_Parts 例 (3 + 1.5×(3.5+0.75)) × (1 + 0.3 + 0.4) = 15.9375x；
      wfsim fight/pellet.rs:561 part_factor = wp_mult × (1+head_bonus) × (1+head_innate)。
      Σ = 「爆头加成」手写/MOD（百分数）+ 灵化进化爆头（小数，evolutions.json kind=headshot_damage，
      wfsim data/evolutions/apply.rs:504 并入 headshot_damage_bonus，与 MOD 同括号
      build/loadout/resolve.rs:1745-1746）+ 武器自带（WEAPON_INNATE_HEADSHOT，非乘算者）。
    * Cernos Prime 自带 +50% 独立乘算：× 1.5 乘在整个括号外（wiki 原文「multiplicative with
      other Headshot Multiplier bonuses」）。
    * 基数：敌人 head_mul 为数值 → 用它；null / 不指定敌人 → 人形 ×3；head_none → 无头，恒 1.0。
    * 暴击爆头：敌人 head_crit 显式标注优先，否则按派系（HEADCRIT_FACTIONS /
      NO_HEADCRIT_FACTIONS）；头部基数 ≤ 1 时不成立（wfsim pellet.rs:569 part.multiplier > 1）。
    * 武器自带头部倍率覆盖（WEAPON_HEADSHOT_OVERRIDE，2026-10-10 第三批）：当前形态
      ``form_kind`` 在表里 ⇒ 基数换成武器的倍率（替换敌人的头部倍率，仍算爆头、加成照乘），
      暴击爆头按 wfsim pellet.rs:569 `head_mult > 1 && part.multiplier > 1` 判定（×1 ⇒ 无）。
      无头部的敌人（head_none）仍按身体计（覆盖的是头部倍率，不凭空造头）。
    """
    innate, innate_mul = WEAPON_INNATE_HEADSHOT.get(unique_name, (0.0, False))
    if enemy and enemy.get("head_none"):
        return {
            "mult": 1.0,
            "landing": 1.0,
            "crit": False,
            "src": "该敌人没有头部弱点，爆头不成立，按身体计（"
            + (enemy.get("head_src") or "")
            + "）",
            "crit_note": "无暴击爆头",
            "formula": "",
        }
    hm = enemy.get("head_mul") if enemy else None
    if hm is not None:
        base = float(hm)
        src = f"该敌人头部 ×{base:g}（{enemy.get('head_src') or '敌人数据'}）"
    else:
        base = HEADSHOT_BASE
        src = HEADSHOT_SOURCE + ("；该敌人未分类，按人形计" if enemy else "")
    part_base = base  # 敌人头部部位本身的倍率（暴击爆头的部位条件看它）
    ov = WEAPON_HEADSHOT_OVERRIDE.get(str(unique_name or "").lower(), {}).get(form_kind)
    if ov:
        base = float(ov[0])
        src = (
            f"武器自带头部倍率 ×{base:g}，替换敌人头部倍率 ×{part_base:g}（爆头加成照乘；{ov[1]}）"
        )
    add = bonus_pct / 100.0 + evo_bonus + (0.0 if innate_mul else innate)
    mult = base * (1.0 + add) * (1.0 + (innate if innate_mul else 0.0))
    formula = f"头部 ×{base:g}"
    if add:
        formula += f" ×(1+{add * 100:g}% 爆头加成)"
    if innate_mul:
        formula += f" ×{1 + innate:g}（武器自带，独立乘算）"
    elif innate:
        formula += f"（含武器自带 +{innate * 100:g}%）"

    if enemy and "head_crit" in enemy:
        crit, why = bool(enemy["head_crit"]), "该敌人逐条标注"
    elif faction in HEADCRIT_FACTIONS:
        crit, why = True, f"{faction} 可触发"
    elif faction in NO_HEADCRIT_FACTIONS:
        crit, why = False, f"{faction} 人形/地面单位不触发"
    else:
        crit, why = False, f"{faction or '该派系'} 能否暴击爆头未核实"
    if crit and ov and base <= 1.0:
        crit, why = False, f"武器自带头部 ×{base:g}，不触发"
    elif crit and min(base, part_base) <= 1.0:
        crit, why = False, "头部 ×1 部位不触发"
    if crit:
        note = f"含暴击爆头（头部暴击时暴伤 ×2；{why}"
        note += "）" if enemy else "，对 Corpus 人形无暴击爆头）"
    else:
        note = f"未计暴击爆头（{why}"
        if not enemy and faction in ("Corpus", "Corpus Amalgam"):
            note += "；Osprey 等飞行单位实际可触发"
        note += "）"
    # 电/毒气 DoT 落点层（DOT_HEAD_LANDING）：1x 基数 × 爆头加成括号，不含头部基数与武器覆盖
    landing = (1.0 + add) * (1.0 + (innate if innate_mul else 0.0))
    return {
        "mult": mult,
        "landing": landing,
        "crit": crit,
        "src": src,
        "crit_note": note,
        "formula": formula,
    }


def compose_elements(singles: dict[str, float]) -> dict[str, float]:
    """按 HCET 顺序把单元素 MOD 两两合成复合元素；合成后加成取两者之和。"""
    pools: dict[str, float] = {}
    remaining = sorted(
        (e for e in singles if singles[e] > 0), key=lambda e: HCET.index(e) if e in HCET else 99
    )
    while len(remaining) >= 2:
        pair = frozenset(remaining[:2])
        if pair in COMPOSITE:
            comp = COMPOSITE[pair]
            pools[comp] = pools.get(comp, 0.0) + singles[remaining[0]] + singles[remaining[1]]
            remaining = remaining[2:]
        else:
            break
    for e in remaining:
        pools[e] = pools.get(e, 0.0) + singles[e]
    return pools


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------
_NUM = r"(\d+(?:\.\d+)?)"

# token 判定阶梯：精确全部优先于模糊/子串，避免「MOD 子串」抢走敌人名
_DISPATCH_LEVELS = (
    "weapon_exact",
    "mod_exact",
    "enemy_exact",
    "mod_sub",
    "enemy_fuzzy",
    "weapon_fuzzy",
)
_FUZZY_LEVELS = ("enemy_fuzzy", "weapon_fuzzy")


def _classify_token(tok: str, level: str):
    """按 level 判定一个 token 是武器 / MOD / 敌人；认不出返回 None。"""
    if level == "weapon_exact":
        w = find_weapon_exact(tok)
        return ("weapon", w) if w else None
    if level == "mod_exact":
        m = find_mod_exact(tok)
        return ("mod", m) if m else None
    if level == "enemy_exact":
        e = find_enemy_exact(tok)
        return ("enemy", e) if e else None
    if level == "mod_sub":
        m = find_mod_sub_unique(tok)
        return ("mod", m) if m else None
    if level == "enemy_fuzzy":
        # 单字不做模糊（会把「火」之类误判成某敌人）
        e = find_enemy(tok) if len(tok.strip()) >= 2 else None
        return ("enemy", e) if e else None
    if level == "weapon_fuzzy":
        w, _alts = find_weapon(tok)
        return ("weapon", w) if w else None
    return None


_SALVAGE_MAX_SPAN = 6  # 摘武器名时最长试几个词拼接（库内武器名最长 4 词）
# 单条指令的参数上限：整条配卡粘贴一般 20~30 个词；超出部分不解析，避免恶意长串拖垮事件循环
MAX_ARG_TOKENS = 64
MAX_TOKEN_LEN = 40  # 单个词超过该长度不可能是武器/MOD/敌人名，直接记为未识别


def _salvage_weapon(tokens: list[str], spec: dict) -> list[str]:
    """从「武器名 + 没认出来的垃圾」里把武器名摘出来。

    背景：玩家会把配卡整串粘过来，只要有一个 token 没进 MOD 名表（新卡、错字、
    别名），它就会混进武器名，整串查不到武器 → 指令看起来「没反应」。
    这里退一步找**最长的可解析片段**，其余记进 spec["unknown"] 由卡面明示。
    """
    if not tokens:
        return tokens
    joined = " ".join(tokens)
    if len(tokens) == 1 or find_weapon(joined)[0] is not None:
        return tokens
    best: Optional[tuple[int, int]] = None
    for i in range(len(tokens)):
        # 片段长度封顶：武器名最多 4 个词（含变体前缀留余量），不必试更长的拼接
        for j in range(min(len(tokens), i + _SALVAGE_MAX_SPAN), i, -1):
            if find_weapon(" ".join(tokens[i:j]))[0] is not None:
                if best is None or (j - i) > (best[1] - best[0]):
                    best = (i, j)
    if best is None:
        # 一个片段都查不到：保留第一个 token 作为武器名（让上层给「没找到」提示），
        # 其余全部标为未识别
        spec["unknown"].extend(tokens[1:])
        return tokens[:1]
    i, j = best
    spec["unknown"].extend(tokens[:i] + tokens[j:])
    return tokens[i:j]


def parse_args(tokens: list[str]) -> tuple[dict, list[str]]:
    """把 content tokens 拆成 (spec, 武器名 tokens)。"""
    spec: dict = {
        "level": 100,
        "faction": "Grineer",
        "headshot": False,
        "steel_path": False,
        "base_armor": None,
        "base_dmg": 0.0,
        "multishot": 0.0,
        "crit_chance": 0.0,
        "crit_dmg": 0.0,
        "faction_dmg": 0.0,
        "singles": {},
        "physical": {},
        "physical_convert": {},
        "headshot_bonus": 0.0,
        "fire_rate_pct": 0.0,
        # 其中来自**射速 MOD 卡面**的部分（弓/弩翻倍只作用于它，见 BOW_CLASS_UNIQUE）
        "fire_rate_mod_pct": 0.0,
        "status_dmg": 0.0,
        "status_chance": 0.0,
        # v1.9：随连击/随异常层数（狂怒 / 创口溃烂 / 异况超量）
        "crit_per_combo": 0.0,
        "status_per_combo": 0.0,
        "dmg_per_status": 0.0,
        "status_types": None,
        # 架势（近战专用槽位；决定每段倍率与强制异常）
        "stance": None,
        # 条件触发（On Kill / On Headshot …）：只展示，不折进数值
        "conditional": [],
        # 派系 MOD（灭亡/歼灭类）按派系分桶：[[派系名, 百分比], ...]；
        # calculate 按最终派系取匹配项并入 faction_dmg（2026-10-06 修：
        # 旧版把「灭亡 Corpus」折进通用桶，打 Grineer 也生效）
        "faction_dmg_of": [],
        # 镀层堆叠（wfsim 结构化条件）：None=未指定，-1=满层，N=N 层
        "galv_stacks": None,
        "galv_applied": [],
        "combo_hits": 0,
        "heavy": False,
        "overguard": None,
        "adapt": None,
        "_armor": 0.0,
        # 近战专用：重击伤害 MOD（一击必杀）、初始连击（邪恶蓄力）、穿透
        "heavy_dmg": 0.0,
        "initial_combo": 0.0,
        "punch_through": 0.0,
        # 斩铁类「重击时 x2」：重击额外获得的暴击几率加成
        "crit_chance_heavy": 0.0,
        # 奋力一掷类：连续投掷每层 +X% 投掷伤害，最多 throw_max_stacks 层
        "throw_dmg": 0.0,
        "throw_stacks": 3,
        "throw_max_stacks": 0,
        # 赋能（武器类）：多数是条件触发，认得出名字与文本就展示；
        # 只有能被解析成无条件数值的部分才计入（见 parse_args 的折算）
        "arcanes": [],
        "arcane_texts": [],
        # 灵化进化选项（进化 基伤/暴击/爆头…）与 lich 回响加成
        "evo_keys": [],
        "lich_bonus": None,
        # 形态：None=默认原型（军械库常态）/ incarnon=灵化 / base=原型 / keep=不动（识卡）
        # / alt_fire 副开火 / charged 蓄力 / uncharged 未蓄力 / semi_auto 半自动 / auto 全自动
        "form": None,
        # 火焰单例累加的开火时长（秒）：稳态里火的平均层数 = 触发率 × T / 2（默认 12 ⇒ 与
        # 修前 rate×6 逐位相同）
        "fire_seconds": FIRE_SECONDS_DEFAULT,
        # 部署（Archgun）：None=默认（地面/大气）/ archwing / atmosphere
        "deployment": None,
        "no_fire_rate_mod": False,
        "notes": [],
        "unknown": [],
        "enemy_miss": "",  # 「对 XXX」里认不出的敌人名（卡面提示可用敌人）
        "viral": 0,
        "magnetic": 0,
        "corrosive": 0,
        "heat_strip": False,
        "enemy": None,
        "mods": [],
        "errors": [],
    }
    name_tokens: list[str] = []
    enemy_hint_pos = None  # 「对」之后第一个名字 token 的下标（该词按敌人解析）
    enemy_hint_raw = -1  # 「对」后紧跟的原始 token 下标（紧跟 = 无派系/等级词隔开）
    enemy_hint_adjacent = False
    tks = [t for t in tokens if t.strip()]
    if len(tks) > MAX_ARG_TOKENS:
        spec["notes"].append(f"参数过多：只解析前 {MAX_ARG_TOKENS} 个词，其余忽略")
        tks = tks[:MAX_ARG_TOKENS]
    _long = [t for t in tks if len(t) > MAX_TOKEN_LEN]
    if _long:
        spec["unknown"].extend(t[:MAX_TOKEN_LEN] + "…" for t in _long)
        tks = [t for t in tks if len(t) <= MAX_TOKEN_LEN]
    tokens = tks  # 下文「进化 <关键词>」按下标取 tokens[i+1]，须与 tks 同一份列表
    i = 0
    while i < len(tks):
        t = tks[i]
        low = t.lower()
        nxt = tks[i + 1] if i + 1 < len(tks) else ""
        num_next = re.fullmatch(_NUM + r"%?", nxt)
        m = re.fullmatch(r"(\d{1,4})级", low) or re.fullmatch(r"lv(\d{1,4})", low)
        if low == "对":  # 「对 重机枪手」的介词：丢掉，但记下「下一个词是敌人」
            enemy_hint_pos = len(name_tokens)
            enemy_hint_raw = i + 1
            i += 1
            continue
        if m:
            spec["level"] = int(m.group(1))
        elif low in ("爆头", "头部"):
            spec["headshot"] = True
        elif low in ("火剥甲", "火焰剥甲"):
            spec["heat_strip"] = True
        elif re.fullmatch(r"(病毒|磁力|腐蚀)(\d{1,2})层?", low):
            m_st = re.fullmatch(r"(病毒|磁力|腐蚀)(\d{1,2})层?", low)
            key = {"病毒": "viral", "磁力": "magnetic", "腐蚀": "corrosive"}[m_st.group(1)]
            spec[key] = min(10, max(1, int(m_st.group(2))))
        elif low in ("钢路", "钢铁之路"):
            spec["steel_path"] = True
        elif low in ("灵化", "灵化形态", "开灵化"):
            spec["form"] = "incarnon"
        elif low in ("基础形态", "基础面板"):
            spec["form"] = "base"
        elif low in _ALT_FORM_WORDS:
            # 非默认形态（2026-10-10 第三批）：用 weapon_attacks.json forms 整套替换主段与额外段
            spec["form"] = _ALT_FORM_WORDS[low]
        elif re.fullmatch(r"开火" + _NUM + r"(秒|s)?", low):
            # 火焰单例累加的开火时长（2026-10-10 第三批，见 steady_state_dps）
            _fs = float(re.search(_NUM, low).group(1))
            spec["fire_seconds"] = min(FIRE_SECONDS_MAX, max(FIRE_SECONDS_MIN, _fs))
            if spec["fire_seconds"] != _fs:
                spec["notes"].append(
                    f"开火时长按 {FIRE_SECONDS_MIN:g}–{FIRE_SECONDS_MAX:g} 秒取值："
                    f"{_fs:g} → {spec['fire_seconds']:g}"
                )
        elif low in ("空战", "空战模式"):
            spec["deployment"] = "archwing"
        elif low in ("地面", "大气", "地面模式"):
            spec["deployment"] = "atmosphere"
        elif low == "进化" and tokens[i + 1 : i + 2]:
            # 进化 <关键词>：灵化进化选项（下一 token），可多次出现
            spec["evo_keys"].append(str(tokens[i + 1]))
            i += 1
        elif re.fullmatch(
            r"进化(基伤|伤害|暴击|暴伤|触发|多重|爆头|射速|装填|连击|穿透|弹匣)", low
        ):
            spec["evo_keys"].append(
                re.fullmatch(
                    r"进化(基伤|伤害|暴击|暴伤|触发|多重|爆头|射速|装填|连击|穿透|弹匣)", low
                ).group(1)
            )
        elif re.fullmatch(
            r"(赤毒|信条|终幕|回响)(辐射|火|冰|电|毒|磁力|病毒|爆炸|冲击|穿刺|切割)?"
            + _NUM
            + r"%?",
            low,
        ):
            m_l = re.fullmatch(
                r"(赤毒|信条|终幕|回响)(辐射|火|冰|电|毒|磁力|病毒|爆炸|冲击|穿刺|切割)?"
                + _NUM
                + r"%?",
                low,
            )
            el = VALENCE_ALIASES.get(m_l.group(2)) if m_l.group(2) else None
            spec["lich_bonus"] = {"element": el, "pct": float(m_l.group(3))}
        elif re.fullmatch(r"加成" + _NUM + r"%?", low):
            spec["lich_bonus"] = {"element": None, "pct": float(re.search(_NUM, low).group(1))}
        elif re.fullmatch(r"基甲" + _NUM + r"%?", low):
            spec["base_armor"] = float(re.search(_NUM, low).group(1))
        elif re.fullmatch(r"派系" + _NUM + r"%?", low):
            spec["faction_dmg"] = float(re.search(_NUM, low).group(1))
        elif re.fullmatch(r"基伤" + _NUM + r"%?", low):
            spec["base_dmg"] = float(re.search(_NUM, low).group(1))
        elif re.fullmatch(r"多重" + _NUM + r"%?", low):
            spec["multishot"] = float(re.search(_NUM, low).group(1))
        elif re.fullmatch(r"暴率" + _NUM + r"%?", low):
            spec["crit_chance"] = float(re.search(_NUM, low).group(1))
        elif re.fullmatch(r"暴伤" + _NUM + r"%?", low):
            spec["crit_dmg"] = float(re.search(_NUM, low).group(1))
        elif re.fullmatch(r"(爆头倍率|爆头加成)" + _NUM + r"%?", low):
            spec["headshot_bonus"] = float(re.search(_NUM, low).group(1))
        elif re.fullmatch(r"(状态伤害|状态强度)" + _NUM + r"%?", low):
            spec["status_dmg"] = float(re.search(_NUM, low).group(1))
        elif re.fullmatch(r"(触发|触发率|触发几率|状态几率|触发几率)" + _NUM + r"%?", low):
            spec["status_chance"] = float(re.search(_NUM, low).group(1))
        # ★ 以下四类「关键词+数字」的数字**必填**（2026-10-09 修）：原正则写成 `_NUM + "?"`，
        #   裸词「镀层 / 异常 / 连击 / 连投」也会命中，随后 re.search(...).group 对 None 崩；
        #   而「镀层 分裂膛室」这类带空格的 MOD 名首词恰是「镀层」。改为必填后裸词
        #   落回名字分派（双词窗口拼回 MOD 名），认不出的进「未识别」明示。
        elif re.fullmatch(r"(异常|异常种类|异常数)" + _NUM, low):
            # 异况超量 / 镀层战术 按「目标身上异常种类数」加成
            spec["status_types"] = int(float(re.search(_NUM, low).group(1)))
        elif re.fullmatch(r"满镀层|镀层满", low):
            # 镀层堆叠：每张已装的镀层类卡按各自的满层数计入
            spec["galv_stacks"] = -1
        elif re.fullmatch(r"镀层" + _NUM, low):
            spec["galv_stacks"] = int(float(re.search(_NUM, low).group(1)))
        elif re.fullmatch(r"连击" + _NUM, low):
            spec["combo_hits"] = int(float(re.search(_NUM, low).group(1)))
        elif low in ("重击", "重击攻击"):
            spec["heavy"] = True
        elif low == "赋能" and nxt:
            # 赋能名（中文名形如「近战·狂怒」，英文名可能带空格 → 吃两词窗口）
            rec = find_arcane(nxt) or (
                find_arcane(" ".join(tks[i + 1 : i + 3])) if i + 2 < len(tks) else None
            )
            if rec:
                spec["arcanes"].append(rec)
                if rec.get("text"):
                    spec["arcane_texts"].append(f"{rec.get('zh') or rec['name']}：{rec['text']}")
                i += 2 if find_arcane(nxt) else 3
                continue
            spec["notes"].append(f"未找到赋能「{nxt}」")
            i += 1
            continue
        elif re.fullmatch(r"(连投|投掷层数)" + _NUM, low):
            spec["throw_stacks"] = int(float(re.search(_NUM, low).group(1)))
        elif low in ("投掷", "投掷伤害"):
            spec["throw_stacks"] = 3  # 默认按满层算
        elif re.fullmatch(r"超宏" + _NUM + r"?", low):
            spec["overguard"] = (
                float(re.search(_NUM, low).group(1)) if re.search(_NUM, low) else True
            )
        elif low == "超宏":
            spec["overguard"] = True
        elif re.fullmatch(r"适应" + _NUM + r"?", low):
            spec["adapt"] = int(float(re.search(_NUM, low).group(1))) if re.search(_NUM, low) else 1
        elif low == "适应":
            spec["adapt"] = 1
        elif (
            low
            in ("基伤", "多重", "暴率", "暴伤", "派系", "基甲", "爆头倍率", "爆头加成", "状态伤害")
            and num_next
        ):
            key = {
                "基伤": "base_dmg",
                "多重": "multishot",
                "暴率": "crit_chance",
                "暴伤": "crit_dmg",
                "派系": "faction_dmg",
                "基甲": "base_armor",
                "爆头倍率": "headshot_bonus",
                "爆头加成": "headshot_bonus",
                "状态伤害": "status_dmg",
            }[low]
            spec[key] = float(num_next.group(1))
            i += 1
        else:
            m_fac = re.fullmatch(r"对?([GgCcIi])系?" + _NUM + r"%?", t)
            elem_m = re.fullmatch(r"(" + "|".join(ELEM_ALIASES) + r")" + _NUM + r"%?", t)
            phys_m = re.fullmatch(r"(" + "|".join(PHYS_ALIASES) + r")" + _NUM + r"%?", t)
            if m_fac:  # 对G30 / C30 这类派系 MOD 加成
                spec["faction_dmg"] = float(m_fac.group(2))
            elif elem_m:
                el = ELEM_ALIASES[elem_m.group(1)]
                spec["singles"][el] = spec["singles"].get(el, 0.0) + float(elem_m.group(2))
            elif phys_m:
                el = PHYS_ALIASES[phys_m.group(1)]
                spec["physical"][el] = spec["physical"].get(el, 0.0) + float(phys_m.group(2))
            elif low in ELEM_ALIASES and num_next:
                el = ELEM_ALIASES[low]
                spec["singles"][el] = spec["singles"].get(el, 0.0) + float(num_next.group(1))
                i += 1
            elif low in PHYS_ALIASES and num_next:
                el = PHYS_ALIASES[low]
                spec["physical"][el] = spec["physical"].get(el, 0.0) + float(num_next.group(1))
                i += 1
            elif low in FACTION_ALIASES:
                spec["faction"] = FACTION_ALIASES[low]
            else:
                if i == enemy_hint_raw and enemy_hint_pos == len(name_tokens):
                    enemy_hint_adjacent = True
                name_tokens.append(t)
        i += 1

    # 剩下的 token 混着「武器名 / MOD 名 / 敌人名」：按**精确优先**的阶梯判定。
    # 顺序很关键（2026-09-16 修）：MOD 的子串匹配若排在敌人精确匹配之前，
    # 「Butcher」会被 MOD「Butcher's Revelry」抢走、「Comba」被「Combat Reload」抢走
    # → 敌人识别失败、还悄悄套了一张错卡。
    # 「对 XXX」是**明确的敌人意图**：先单独把它摘出来解析，免得被同名 MOD 抢走
    # （Scorch / Seeker 这两个名字在 MOD 表和敌人表里同时存在）。
    # 先按 3/2/1 词窗口做精确匹配（英文名常带空格，如 Eidolon Teralyst），
    # 再对单词做模糊（「重机枪手」→「重型机枪手」）。
    if enemy_hint_pos is not None and enemy_hint_pos < len(name_tokens):
        for size in (3, 2, 1):
            if enemy_hint_pos + size > len(name_tokens):
                continue
            cand = " ".join(name_tokens[enemy_hint_pos : enemy_hint_pos + size])
            e = find_enemy_exact(cand)
            if not e and size == 1:
                e = find_enemy(cand)
            if e:
                spec["enemy"] = e
                del name_tokens[enemy_hint_pos : enemy_hint_pos + size]
                break
        else:
            # 「对」后的词不在敌人表（2026-10-10 敌人表精简到常用 19 个）：它是明确的敌人
            # 意图，不再落回 MOD/武器分派（修前「对 Butcher」会被 MOD 子串套上
            # Butcher's Revelry）；按未指定敌人（默认派系）计，卡面列出可用敌人。
            # 只在它不是武器名（含模糊，如「对 G系 绝路p」）时这样处理；「对」与它之间隔了
            # 派系/等级词时（「对 C系 膛线」）MOD 全名仍按 MOD 认。
            tok = name_tokens[enemy_hint_pos]
            if not find_weapon(tok)[0] and (enemy_hint_adjacent or not find_mod_exact(tok)):
                spec["enemy_miss"] = tok
                del name_tokens[enemy_hint_pos]

    weapon_tokens: list[str] = []
    pending = list(name_tokens)
    while pending:
        matched = False
        for level in _DISPATCH_LEVELS:
            # ⚠️ 模糊级别只允许**单词窗口**：两词串做 difflib 会把
            # 「绝路p 重型机枪手」整串判给敌人（相似度 0.77），顺手吞掉武器名
            sizes = (1,) if level in _FUZZY_LEVELS else (2, 1)
            for size in sizes:
                if size > len(pending):
                    continue
                tok = " ".join(pending[:size])
                hit = _classify_token(tok, level)
                if not hit:
                    continue
                kind, obj = hit
                if kind == "weapon":
                    weapon_tokens.append(tok)
                elif kind == "mod":
                    spec["mods"].append(obj)
                else:
                    spec["enemy"] = obj
                del pending[:size]
                matched = True
                break
            if matched:
                break
        if not matched:
            spec["unknown"].append(pending[0])
            del pending[:1]
    if not weapon_tokens and spec["unknown"]:
        # 一个武器片段都没认出来：把第一个未识别词当武器名交给上层，
        # 好让用户看到「没找到「XXX」」而不是一份使用说明
        weapon_tokens.append(spec["unknown"].pop(0))

    # 武器名抢救：剩下的 token 里若混进了没人认领的垃圾（例如没录进名表的
    # MOD、错别字），整串拼起来就查不到武器 → 那时退一步，挑**能查到武器的最长
    # 片段**当武器名，剩下的记进 unknown，卡面明确写出「未识别」。
    weapon_tokens = _salvage_weapon(weapon_tokens, spec)

    # 武器记录（扳机限制卡要用）：Cannonade 系「仅限半自动扳机」是装备规则，
    # 只对扳机为 Semi 的武器生效；点射（Burst）不算半自动 —— wiki Cannonade
    # 表逐武器核对过（Burston 不吃 Semi-Rifle Cannonade）。
    weapon_rec = None
    _wj = " ".join(weapon_tokens).strip()
    if _wj:
        weapon_rec = find_weapon_exact(_wj) or (find_weapon(_wj)[0] or None)
    _semi_ok = bool(weapon_rec and str(weapon_rec.get("trigger") or "").lower() == "semi")

    # MOD 效果折算进 spec（与手写百分比同源：同组相加、派系 MOD 走独立乘区）
    for m in spec["mods"]:
        eff = m.get("effects") or {}
        _note_l = str(eff.get("note") or "").lower()
        if "only compatible" in _note_l and "semi" in _note_l and not _semi_ok:
            # 扳机限制卡装在非半自动武器上：整卡不折算，明示原因
            spec["notes"].append(
                f"{m.get('zh') or m.get('name')}：仅兼容半自动扳机"
                f"（这把武器是 {weapon_rec.get('trigger') if weapon_rec else '未知'}），效果未计入"
            )
            continue
        spec["base_dmg"] += eff.get("base_dmg", 0.0)
        spec["multishot"] += eff.get("multishot", 0.0)
        spec["crit_chance"] += eff.get("crit_chance", 0.0)
        spec["crit_dmg"] += eff.get("crit_dmg", 0.0)
        spec["headshot_bonus"] += eff.get("headshot_bonus", 0.0)
        spec["fire_rate_pct"] += eff.get("fire_rate", 0.0)
        spec["fire_rate_mod_pct"] += eff.get("fire_rate", 0.0)
        # 状态伤害（Elementalist 类）只影响 DoT，不影响直伤
        spec["status_dmg"] += eff.get("status_dmg", 0.0)
        # 触发率（相对加成，作用在武器基础触发率上）→ 用于异常覆盖率
        spec["status_chance"] += eff.get("status_chance", 0.0)
        # 近战专用：重击伤害独立乘区；初始连击抬高重击倍率的起手层数
        spec["heavy_dmg"] += eff.get("heavy_dmg", 0.0)
        spec["initial_combo"] += eff.get("initial_combo", 0.0)
        spec["punch_through"] += eff.get("punch_through", 0.0)
        # 斩铁：`+120% 暴击几率（重击时 x2）` → 重击额外再吃一份
        _hm = float(eff.get("heavy_crit_mult") or 1.0)
        if _hm > 1.0:
            spec["crit_chance_heavy"] += eff.get("crit_chance", 0.0) * (_hm - 1.0)
        # v1.9：随连击加成的暴击/触发（狂怒、创口溃烂）
        spec["crit_per_combo"] += eff.get("crit_per_combo", 0.0)
        spec["status_per_combo"] += eff.get("status_per_combo", 0.0)
        # 异况超量类：目标身上每种异常 +N% 直伤（进**基伤组**，wiki CO 原式）
        spec["dmg_per_status"] += eff.get("dmg_per_status", 0.0)
        # 物理转换（彗星弹等）：把 X% 物理伤害「搬」到该类型，总量不变
        for el, pct in (eff.get("physical_convert") or {}).items():
            spec["physical_convert"][el] = spec["physical_convert"].get(el, 0.0) + pct
        # 条件触发：只记录，绝不折进数值
        for c in eff.get("conditional") or []:
            spec["conditional"].append(
                f"{m.get('zh') or m.get('name')}：" + str(c).replace("\\n", " ").replace("\n", " ")
            )
        # 架势：单独存，不参与上面的数值折算
        if (m.get("compat") or "") == "Stance":
            spec["stance"] = m
        # 奋力一掷：连续投掷的投掷伤害加成（每层 X%，最多 N 层）
        spec["throw_dmg"] += eff.get("throw_dmg", 0.0)
        if eff.get("throw_max_stacks"):
            spec["throw_max_stacks"] = max(
                int(spec["throw_max_stacks"]), int(eff["throw_max_stacks"])
            )
        # 元素 / 物理 / 派系（这几项原先在循环外，被补丁误并进赋能循环了）
        _fac_of = str(eff.get("faction_of") or "")
        if eff.get("faction_mul"):  # 派系 MOD 是倍率写法（x1.3）
            _fac_pct = (eff["faction_mul"] - 1) * 100
        else:
            _fac_pct = eff.get("faction_dmg", 0.0)
        if _fac_of and _fac_pct:
            # 灭亡/歼灭类：按派系分桶，calculate 按最终派系取用
            # （2026-10-06 修：旧版折进通用桶，打任意派系都生效）
            spec["faction_dmg_of"].append([_fac_of, _fac_pct])
        else:
            spec["faction_dmg"] += _fac_pct
        for el, pct in (eff.get("elements") or {}).items():
            if el in _PHYS_TYPES:
                # 数据层历史双写（wfsim 同步把物理类型也写进 elements）——
                # 物理加成只走 physical 通道（同类型基值乘区），这里跳过防重复
                continue
            spec["singles"][el] = spec["singles"].get(el, 0.0) + pct
        for el, pct in (eff.get("physical") or {}).items():
            spec["physical"][el] = spec["physical"].get(el, 0.0) + pct
        note = eff.get("note") or ""
        if note:
            spec["notes"].append(f"{m.get('zh') or m.get('name')}：{note}")
            if "fire rate cannot be modified" in note.lower():
                spec["no_fire_rate_mod"] = True

    # 镀层堆叠（wfsim 结构化条件，「镀层N / 满镀层」显式带入）：
    # 条件堆叠默认不计入（同赋能原则），用户指定层数才折算；
    # 每层值取满级 rankMax（伤害指令的 MOD 一律按满级口径）
    if spec.get("galv_stacks") is not None:
        for m in spec["mods"]:
            extra = mod_extra(m)
            for cond in extra.get("conditionals") or []:
                if not cond.get("mappable") or not cond.get("field"):
                    continue
                cap = int(cond.get("max_stacks") or 1)
                n = cap if spec["galv_stacks"] == -1 else max(0, min(int(spec["galv_stacks"]), cap))
                if n <= 0:
                    continue
                add = float(cond.get("value") or 0.0) * n
                _fk = _EFFECT_TO_SPEC.get(cond["field"], cond["field"])
                spec[_fk] = spec.get(_fk, 0.0) + add
                spec["galv_applied"].append(
                    f"{m.get('zh') or m.get('name')} "
                    f"{cond.get('grants')} +{add:.1f}%（{n}/{cap} 层）"
                )

    # 赋能：**只有能被解析成无条件数值的部分才计入**。武器赋能绝大多数是
    # 「On X: Y% chance for +Z」这种条件触发（如 Arcane Fury），把它们当常驻加成
    # 会系统性高估 —— 所以默认只在卡面列出效果原文，条件性的不折进数值。
    for _arc in spec["arcanes"]:
        _text = (_arc.get("text") or "").lower()
        _conditional = any(
            k in _text
            for k in (
                "chance",
                "on kill",
                "on hit",
                "on critical",
                "on status",
                "on damaged",
                "on headshot",
                "while ",
            )
        )
        if _conditional:
            continue
        _eff = _arc.get("effects") or {}
        for key in (
            "base_dmg",
            "multishot",
            "crit_chance",
            "crit_dmg",
            "fire_rate",
            "status_chance",
        ):
            if _eff.get(key):
                # fire_rate → fire_rate_pct（原写法 spec["fire_rate"] += … 会 KeyError）
                _sk = _EFFECT_TO_SPEC.get(key, key)
                spec[_sk] = spec.get(_sk, 0.0) + float(_eff[key])
    return spec, weapon_tokens


# ---------------------------------------------------------------------------
# 主计算
# ---------------------------------------------------------------------------
# 吃「本元素 MOD 括号」的 DoT（wfsim fight/procs.rs：Toxin / Electricity / Heat 取
# elem_bracket；Bleed 固定 1.0「elemental mods never scale the ticks」:731；Gas 固定 1.0
# 「a Heat or Toxin mod adds nothing to a Gas tick」:785-792）
_DOT_ELEM_BRACKET_TYPES = ("heat", "toxin", "electricity")


def _dot_elem_bracket(el: str, spec: dict) -> float:
    """DoT 每跳的本元素括号 = 1 + Σ**该元素自身**的 MOD 加成（2026-10-09 B1）。

    wfsim build/loadout/resolve.rs:569-574：elem_bonus 按 MOD 自身的元素类型累加，
    与它后来是否被合成成复合元素无关（setup.rs:1689 elem_bracket 读的就是它）。
    修前 DoT 没有这一项：+90% 火时火 DoT 少 ×1.9。
    """
    if el not in _DOT_ELEM_BRACKET_TYPES:
        return 1.0
    return max(0.0, 1.0 + float((spec.get("singles") or {}).get(el, 0.0)) / 100.0)


def _dmg_sig(d: Optional[dict]) -> tuple:
    """伤害向量签名（段去重 / 判「与主段相同」共用）：只看正值伤害类型。

    ★ 必须剔除 `total` 与非伤害类型（2026-10-09 修）：warframe-items 有 41 把武器的
    attacks 段伤害带 `total` 键，wfsim 段与主段都不带 ⇒ 签名永远对不上，
    同一段被算两次（Kuva Ayanga「Area Attack」与「范围伤害」各 82.8）、
    「与主段相同」也判不出来。
    """
    return tuple(
        sorted(
            (k, round(float(v), 3))
            for k, v in (d or {}).items()
            if isinstance(v, (int, float))
            and v > 0
            and k != "total"
            and k.lower() not in _SKIP_TYPES
        )
    )


QUANTIZATION_DENOMINATOR = 32.0  # U40 起 1/32（此前 1/16）


def quantize_damage(per_type: dict[str, float], modded_base: float) -> dict[str, float]:
    """伤害量化：每个类型的值取整到 ``modded_base / 32`` 的整数倍（四舍五入 floor(y+0.5)）。

    依据：wiki Damage/Calculation §Quantization —— ``Scale = Modded Base Damage / 32``，
    ``Quantized(x) = sign(x)·floor(|x|·32 + 0.5)/32``，量化后才乘其它倍率（派系等）。
    ★ 分母是 ModdedBase = 基础 ×(1 + 基伤 MOD [+ 加法型 CO])，**不含元素、不含物理单项 MOD**
    （wfsim engine/src/rules/damage.rs:280 quantized_against；实测 docs/measurements/M57
    Braton Prime 四档、M80 Ballistica Prime 1783/2341 —— 均为游戏内读数）。
    纯物理也会动：各类型占比不是 1/32 整数倍时同样取整（wiki 例 30/30/40 ⇒ 100 面板实打
    103.125）；混元素 / 物理单项 MOD 时偏差更明显（± 几个百分点）。
    量化到 0 的类型直接去掉（否则护甲后「每类型至少 1 点」会凭空造出伤害）。
    """
    if modded_base <= 0:
        return per_type
    sc = modded_base / QUANTIZATION_DENOMINATOR
    out: dict[str, float] = {}
    for k, v in per_type.items():
        q = math.floor(abs(v) / sc + 0.5) * sc
        if q > 0:
            out[k] = q if v >= 0 else -q
    return out


def _hit_from_damage(
    dmg: dict,
    total: float,
    spec: dict,
    fac_table: dict,
    faction: str,
    dr: float,
    viral_mult: float,
    mag_mult: float,
    n_types: int = 0,
    stance_mult: float = 1.0,
    with_co: bool = True,
    co_mode: str = "additive_with_base_damage",
    quantize: bool = True,
) -> tuple[dict, dict, dict, float]:
    """给定**一段攻击**的伤害构成，算出 MOD 后的各类型值与对血/对盾明细。

    抽出来是为了让「普通攻击」之外的段（投掷、投掷爆炸、震地、充能投掷…）
    走**完全相同的规则**去算，而不是按普通攻击等比缩放（缩放会把元素合成、
    派系弱点、护甲减免全算错——各段的类型构成常常完全不同）。

    返回 ``(MOD 后各类型, 对血各类型, 对盾各类型, MOD 后基础总伤)``。

    ``with_co=False``：不计异况超量类加成（CO 只作用于**直击**，2026-10-09 修，
    对齐 wfsim：DoT 种子取 CO 之前的 ``mb_live``（fight/pellet.rs:394-404），范围段
    默认不吃 CO（data/weapons/attack.rs:704-708「the mods say direct hits only」）。
    修前 CO 0→80 时 Braton Prime 切割 DoT 84.7→282.2、Kuva Ogris 爆炸段 150.5→511.6。

    ``quantize=False``：不做伤害量化 —— 只给「军械库面板」复算用（面板显示的是量化前的值，
    识卡截图实测 Xoris「总计 156>264」= 未量化和；量化后实打 262.5）。

    ``co_mode``（wfsim co_behavior，B3）：``additive_with_base_damage`` = 与基伤同一加法
    括号；``independent`` = 独立乘区 ×(1+CO)；``inert`` = CO 无效（wfsim scale.rs:147-183）。
    """
    # 异况超量 / 镀层战术：目标身上每种异常 +N%，**进基伤组**
    # （wiki Condition Overload 原式：Total = 基础 ×[1+伤害MOD+(CO×n)]×(1+元素MOD)）
    # n_types（目标身上异常种类数）由 calculate 统一算好传进来：
    # 这里拿不到 weapon，没法判断武器自身有没有触发率
    _n_types = max(0, int(n_types or 0))
    _co = spec["dmg_per_status"] * _n_types if (with_co and co_mode != "inert") else 0.0
    if co_mode == "independent":
        after_base = 1 + spec["base_dmg"] / 100.0
        _co_indep = 1 + _co / 100.0
    else:
        after_base = 1 + (spec["base_dmg"] + _co) / 100.0
        _co_indep = 1.0
    per_type: dict[str, float] = {}
    for k, v in dmg.items():
        # ⚠️ 大小写不敏感：武器数据里是 shieldDrain/healthDrain 这种 camelCase，
        #    而 _SKIP_TYPES 是小写 —— 不加 .lower() 就会把「护盾吸取」当伤害算
        if k in ("total",) or k.lower() in _SKIP_TYPES or not isinstance(v, (int, float)) or v <= 0:
            continue
        phys = 1 + spec["physical"].get(k, 0.0) / 100.0
        per_type[k] = per_type.get(k, 0.0) + v * after_base * phys
    base_total_after = float(total or sum(per_type.values())) * after_base
    for el, pct in compose_elements(spec["singles"]).items():
        per_type[el] = per_type.get(el, 0.0) + base_total_after * pct / 100.0

    # 物理转换（彗星弹/锯齿弹/穿刺弹…）：把 X% 的**物理伤害**搬到目标类型，
    # 总量不变（wiki：only converts physical damage；且同时只能装一张）。
    if spec["physical_convert"]:
        _phys_now = {k: v for k, v in per_type.items() if k in _PHYS_TYPES}
        _ptot = sum(_phys_now.values())
        if _ptot > 0:
            for el, pct in spec["physical_convert"].items():
                amount = _ptot * pct / 100.0
                for k in _phys_now:
                    per_type[k] -= amount * (per_type[k] / _ptot)
                per_type[el] = per_type.get(el, 0.0) + amount

    # ★ 伤害量化（2026-10-10 加）：每个类型取整到 ModdedBase/32 的整数倍，再进 CO 独立乘区 /
    #   派系 / 护甲 / 暴击。见 quantize_damage。
    if quantize:
        per_type = quantize_damage(per_type, base_total_after)

    # CO 独立乘区（co_mode == independent）：整段直击 ×(1+CO)，不被基伤 MOD 稀释
    #   —— 在量化**之后**乘（wfsim docs/measurements/M81：四档实测全按「先量化再乘」）
    if _co_indep != 1.0:
        per_type = {k: v * _co_indep for k, v in per_type.items()}

    # 架势倍率：近战每一段都按连招倍率放大（默认取站立连招的每段平均）
    if stance_mult and stance_mult != 1.0:
        per_type = {k: v * stance_mult for k, v in per_type.items()}

    fac_mod = 1 + spec["faction_dmg"] / 100.0
    per_type_health: dict[str, float] = {}
    per_type_shield: dict[str, float] = {}
    for k, v in per_type.items():
        vm = fac_table.get(k, {}).get(faction, 1.0)
        d = v * fac_mod * vm
        # 打血：护甲减免后**每个伤害类型至少 1 点**（wiki Armor），再乘病毒。
        # 只对有伤害的类型兜底：直击 0 的段（Simulor 系 Orb Launch，b7 批）加元素 MOD 后各类型仍是
        # 0，修前被抬成每类型 1 点（重力奇点拟成枪 火 90 + 触发 90 ⇒ 直击 1.12）
        hp_d = d * (1 - dr)
        if dr > 0 and d > 0:
            hp_d = max(hp_d, 1.0)
        per_type_health[k] = hp_d * viral_mult
        per_type_shield[k] = 0.0 if k == "toxin" else d * mag_mult
    return per_type, per_type_health, per_type_shield, base_total_after


_EVO_DATA: Optional[dict] = None
_LICH_DATA: Optional[dict] = None
_INC_FORMS: Optional[dict] = None


_WEAPON_ATTACKS: Optional[dict] = None


def _load_weapon_attacks() -> dict:
    """武器的结构化额外段（wfsim radial/cluster 等，key = 小写 uniqueName）。"""
    global _WEAPON_ATTACKS
    if _WEAPON_ATTACKS is None:
        try:
            _WEAPON_ATTACKS = json.loads(
                (_DATA / "weapon_attacks.json").read_text(encoding="utf-8")
            )
        except Exception:  # noqa: BLE001
            _WEAPON_ATTACKS = {}
    return _WEAPON_ATTACKS


def _load_incarnon_forms() -> dict:
    """灵化形态基础面板（wfsim 抽取，key = 小写 uniqueName）。"""
    global _INC_FORMS
    if _INC_FORMS is None:
        try:
            _INC_FORMS = json.loads((_DATA / "incarnon_forms.json").read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            _INC_FORMS = {}
    return _INC_FORMS


def _apply_incarnon_form(spec: dict, w: dict) -> dict:
    """灵化形态切换：**默认原型，写「灵化」才切灵化形态**。

    spec["form"]：None=默认（原型）/ "incarnon"=灵化 / "base"=原型（显式）/
    "keep"=不动（识卡用：面板已反推过）。
    ★ 2026-10-09 改（维护者定，推翻 2026-09-17「默认开灵化」）：灵化是临时形态（弱点命中
    充能、备用开火切入、打空切回），军械库常态是原型 —— wfsim 74 个可转灵化的原型条目
    全部标 `default_form: true`，69 个灵化条目无一标默认（latron_prime.yaml 射速 4.17 /
    latron_prime_incarnon.yaml 3.33）。旧默认让「拉特昂 Prime + 关键延迟」得 2.66 而非
    游戏面板的 3.33。
    灵化形态是另一套基础面板（伤害向量/暴击/触发/射速），MOD 照常叠算；
    带范围（radial）段的形态在卡面标注 AoE 值（不并入主段数值）。
    """
    form = spec.get("form")
    if form == "keep" or form in ALT_FORM_KINDS:
        return w  # 副开火 / 蓄力 等非默认形态走 _apply_alt_form
    key = str(w.get("uniqueName") or "").lower()
    inc = _load_incarnon_forms().get(key)
    if not inc:
        if form == "incarnon":
            spec["notes"].append("该武器没有灵化形态数据（近战灵化尚未收录），仍按基础形态计算")
        return w
    if form == "base":
        spec["notes"].append("形态：基础形态（可用「灵化」切到灵化形态）")
        return w
    if form != "incarnon":  # None = 默认原型
        spec["notes"].append("形态：基础形态（默认；该武器有灵化形态，可加「灵化」切换）")
        return w
    w2 = dict(w)
    dmg = dict(inc.get("damage") or {})
    w2["damage"] = dmg
    for src, dst in (
        ("criticalChance", "criticalChance"),
        ("criticalMultiplier", "criticalMultiplier"),
        ("procChance", "procChance"),
        ("fireRate", "fireRate"),
        ("multishot", "multishot"),
    ):
        if inc.get(src) is not None:
            w2[dst] = inc[src]
    set_form_cadence(w2, inc)  # 点射 / 蓄力随灵化形态（b6 批）
    # 灵化弹池（2026-10-09 C 批）：灵化形态的子弹由能量槽供给，不是弹匣 ——
    # 持续 DPS 的「弹匣 + 装填」循环按 wfsim pseudo_reload（弹池上限 / 切出 + 切入时长），
    # wfsim engine/src/data/weapons/panel.rs:197-200。修前沿用原型弹匣 / 装填
    # （拉特昂 Prime 灵化按 15 发 / 2.4s 算，应为 40 发 / 3.4s）
    pr = inc.get("pseudo_reload") or {}
    if pr.get("magazine") and pr.get("reload_seconds"):
        w2["magazineSize"] = float(pr["magazine"])
        w2["reloadTime"] = float(pr["reload_seconds"])
    txt = f"形态：灵化形态（基础 {dmg.get('total', 0):g}）"
    rad = inc.get("radial") or {}
    if rad.get("damage"):
        txt += (
            f" + 范围段 {rad['damage'].get('total', 0):g}"
            f"（半径 {rad.get('radius_m', 0):g}m，单独结算）"
        )
    if inc.get("forced_procs"):
        txt += "；强制异常：" + "、".join(TYPE_ZH.get(x, x) for x in inc["forced_procs"])
    rc = inc.get("ricochet") or {}
    if rc.get("bounces"):
        txt += f"；弹跳 ×{rc['bounces']}（弱点率假设 {rc.get('headshot_chance', 0) * 100:g}%）"
    spec["notes"].append(txt)
    return w2


# 形态关键词 → spec["form"]（parse_args 用）
_ALT_FORM_WORDS = {
    "副开火": "alt_fire",
    "次要开火": "alt_fire",
    "备用开火": "alt_fire",
    "蓄力": "charged",
    "蓄力射击": "charged",
    "满蓄力": "charged",
    "未蓄力": "uncharged",
    "不蓄力": "uncharged",
    "半自动": "semi_auto",
    "全自动": "auto",
}
# 非默认形态（2026-10-10 第三批）：spec["form"] → wfsim form 种类（weapon_attacks.json 的
# forms 键前缀 <kind>:<yaml_id>）。只在用户显式写了关键词时生效；默认口径不读 forms。
ALT_FORM_KINDS = {
    "alt_fire": "副开火",
    "charged": "蓄力",
    "uncharged": "未蓄力",
    "semi_auto": "半自动",
    "auto": "全自动",
}
_ALT_FORM_WFSIM = {"uncharged": "base"}  # 我方关键词 → wfsim form 名（其余同名）


def _weapon_attacks_rec(weapon: dict) -> dict:
    return _load_weapon_attacks().get(str((weapon or {}).get("uniqueName") or "").lower()) or {}


def selected_form(spec: dict, weapon: dict) -> tuple[str, dict]:
    """当前实际计算的形态：("default", {}) / ("incarnon", <forms 里的灵化记录或 {}>) /
    (<非默认形态种类>, <forms 记录>)。选了形态但该武器没有对应数据 ⇒ 回落 default。"""
    form = spec.get("form")
    forms = _weapon_attacks_rec(weapon).get("forms") or {}
    if form == "incarnon":
        if not _load_incarnon_forms().get(str(weapon.get("uniqueName") or "").lower()):
            return "default", {}
        hit = sorted(k for k in forms if k.startswith("incarnon:"))
        return "incarnon", (forms[hit[0]] if hit else {})
    if form in ALT_FORM_KINDS:
        kind = _ALT_FORM_WFSIM.get(form, form)
        hit = sorted(k for k in forms if k.startswith(kind + ":"))
        if hit:
            return form, forms[hit[0]]
    return "default", {}


# ---------------------------------------------------------------------------
# 射击节奏（2026-10-10 b6 批）：点射 / 蓄力 → 每秒有效射弹数
# ---------------------------------------------------------------------------
# 武器字典上的节奏字段：burstCount / burstDelay（点射，weapons_stats 由 build_damage_data.py
# BURST_FIX 写入；形态来自 weapon_attacks / incarnon_forms 的 burst）、chargeTime（蓄力秒数，
# 默认形态取 weapon_attacks.json 顶层 charge_seconds，形态取该形态的）、chargeCadence（"draw" =
# 弓 / Lanka，不付「1/射速」那一项，CHARGE_DRAW_ONLY）、ammoCost（每发耗弹：weapons_stats 由
# build_damage_data.py AMMO_COST_FIX 写入，形态取 weapon_attacks forms 的 ammo_cost，b7 批）。
_CADENCE_KEYS = ("burstCount", "burstDelay", "chargeTime", "ammoCost")


def set_form_cadence(w: dict, rec: dict) -> None:
    """形态整套替换主段时，节奏字段随形态走：形态带 burst / charge_seconds 就写，没有就去掉
    母武器的（Burston 灵化是全自动，不能沿用原型的 3 连发）。每发耗弹取形态自己的 ammo_cost
    （b7 批：Zenith 半自动 3、Staticor 未蓄力 1、Cedo 副开火 0…），不带则回到 1（灵化形态对
    能量槽每发 1，wfsim *_incarnon.yaml ammo_cost 1）。"""
    for k in _CADENCE_KEYS:
        w.pop(k, None)
    b = rec.get("burst") or {}
    if int(b.get("count") or 0) > 1:
        w["burstCount"] = int(b["count"])
        w["burstDelay"] = float(b.get("delay_seconds") or 0.0)
    # 0.0 而不是删键：_apply_panel_overrides 见到键就不再回填默认形态的蓄力秒数（识卡面板路径）
    w["chargeTime"] = float(rec.get("charge_seconds") or 0.0)
    if rec.get("ammo_cost") is not None:
        w["ammoCost"] = float(rec["ammo_cost"])


def bow_cycle(w: dict) -> bool:
    """弓的整循环（chargeCadence draw、弹匣 1、有装填；含蓄力与未蓄力）：每发 = 蓄力 + 上弦，
    爆发 DPS 打不出「只算蓄力」的射速 ⇒ 有效射速按 wiki 弓式 1/(蓄力 + 装填)，持续 = 爆发（b8 批）。
    Lanka（draw、弹匣 10）不在此列。"""
    return (
        w.get("chargeCadence") == "draw"
        and float(w.get("magazineSize") or 0.0) == 1.0
        and float(w.get("reloadTime") or 0.0) > 0
    )


def nock_only(w: dict) -> bool:
    """弓的未蓄力点按（chargeCadence draw、蓄力 0、弹匣 1、有装填）：一发 = 一次上弦（b7 批）。"""
    return (
        w.get("chargeCadence") == "draw"
        and float(w.get("chargeTime") or 0.0) <= 0
        and float(w.get("magazineSize") or 0.0) == 1.0
        and float(w.get("reloadTime") or 0.0) > 0
    )


def cadence_fire_rate(w: dict, fr_mult: float = 1.0) -> tuple[float, str]:
    """(每秒有效射弹数, 说明)。fr_mult = 射速加成后的倍率（1 + 射速%，弓的 MOD 部分已 ×2）。

    wiki「Fire Rate」原式（wfsim 同，fight/melee.rs:530-547）：
    · 点射：N / (1/(FR·m) + (N−1)·D / max(1, m))——面板 FR = 每秒点射次数（发间延迟为 0 时），
      射速加成同时缩短发间延迟，但「Burst Delay is not affected by net negative Fire Rate bonuses」；
    · 一般蓄力武器：1 / (蓄力/m + 1/(FR·m))；
    · 弓（chargeCadence = "draw"、弹匣 1，bow_cycle）：wiki 弓式 1/(蓄力/m + 装填)，上弦直接计入
      有效射速 ⇒ 爆发 = 持续（b8 批；修前爆发只算 1/蓄力、上弦放在持续 DPS 的装填项）；
    · Lanka（draw、弹匣 10）：1 / (蓄力/m)（「does not have a delay between charged shots」）；
    · Epitaph 系（b10 批，视频实测）：满蓄力按一般式 1/(蓄力/m + 1/(FR·m))，不装填（noReload）
    · 弓的「未蓄力」（draw 且蓄力 0、弹匣 1）：只付上弦 ⇒ 1 / 装填，射速加成不加快（wiki 未单列；
      wfsim *_uncharged.yaml charge_seconds 0.0 + DrawOnly 的实现口径，docs/measurements/M16 只有
      实测方案、未出结果）——这一循环已含上弦，持续 DPS = 爆发 DPS（见 nock_only）；
    · 「charge time cannot go above 10 times the base charge time」⇒ m 下限 0.1。
    其余武器 = FR·m，说明为空。"""
    fr = float(w.get("fireRate") or 0.0)
    m = float(fr_mult)
    ct = float(w.get("chargeTime") or 0.0)
    if nock_only(w):
        rl = float(w.get("reloadTime") or 0.0)
        return 1.0 / rl, (
            f"未蓄力点按：只付上弦 {rl:.3g}s ⇒ 有效射速 = 1/上弦 = {1.0 / rl:.3g} 发/s，射速加成不加快"
            "（wiki 未单列；按 wfsim 弓未蓄力实现口径，M16 未出实测）；持续 DPS = 爆发"
        )
    if ct > 0:
        ct_mod = ct / max(m, 0.1)
        if bow_cycle(w):
            # wiki Fire Rate「Effective Fire Rate = 1 / (Modded Charge Time + Modded Reload Time)
            # — Calculation for true fire rate for bow weapons」（b8 批：修前爆发只算 1/蓄力，
            # 弹匣 1 的弓打不出这个射速；上弦改计进有效射速，持续 = 爆发）
            rl = float(w.get("reloadTime") or 0.0)
            eff = 1.0 / (ct_mod + rl)
            return eff, (
                f"蓄力 {ct_mod:.3g}s + 上弦 {rl:.3g}s：有效射速 = 1/(蓄力 + 装填) = {eff:.3g} 发/s"
                "（wiki Fire Rate 弓式；弹匣 1，爆发 = 持续）"
            )
        if w.get("chargeCadence") == "draw" or fr <= 0:
            eff = 1.0 / ct_mod
            return eff, (
                f"蓄力 {ct_mod:.3g}s：有效射速 = 1/蓄力 = {eff:.3g} 发/s"
                "（wiki Fire Rate：弓 / Lanka 不另付射速间隔；弓的上弦计入持续 DPS）"
            )
        eff = 1.0 / (ct_mod + 1.0 / (fr * m))
        return eff, (
            f"蓄力 {ct_mod:.3g}s：有效射速 = 1/(蓄力 + 1/射速) = {eff:.3g} 发/s"
            f"（面板射速 {fr * m:.3g}；wiki Fire Rate 蓄力武器式）"
        )
    n = int(w.get("burstCount") or 0)
    if n > 1 and fr > 0:
        d = float(w.get("burstDelay") or 0.0)
        eff = n / (1.0 / (fr * m) + (n - 1) * d / max(1.0, m))
        return eff, (
            f"点射 {n} 发/次、发间 {d:g}s：有效射速 = {n}/(1/{fr * m:.3g} + {n - 1}×{d:g}"
            + (f"/{m:.3g}" if m > 1 else "")
            + f") = {eff:.3g} 发/s（面板射速 = 每秒点射次数；wiki Fire Rate §Burst）"
        )
    return fr * m, ""


def _apply_alt_form(spec: dict, w: dict) -> dict:
    """非默认形态（副开火 / 蓄力 / 未蓄力 / 半自动 / 全自动）：用 weapon_attacks.json
    forms 里该形态的整套面板替换主段（伤害向量 / 暴击 / 触发 / 射速 / 多重），额外段在
    calculate 里同样整套换成该形态的（原型的范围段不参与）。弹匣 / 装填沿用母武器（wfsim
    形态继承顶层键，catalog.rs:61-78）。没有该形态数据 ⇒ 按默认形态算并注明。"""
    form = spec.get("form")
    if form not in ALT_FORM_KINDS:
        return w
    kind, rec = selected_form(spec, w)
    label = ALT_FORM_KINDS[form]
    if kind == "default":
        if _weapon_attacks_rec(w).get("main_form") == _ALT_FORM_WFSIM.get(form, form):
            # 默认主段本身就是这一形态（如 Nataruk 默认 = 快速射击 = 未蓄力）
            spec["notes"].append(f"「{label}」即该武器的默认主段，按默认形态计算")
        else:
            spec["notes"].append(f"该武器没有「{label}」形态数据（wfsim 未收录），仍按默认形态计算")
        return w
    main = rec.get("main") or {}
    w2 = dict(w)
    if main.get("damage"):
        w2["damage"] = dict(main["damage"])
    for src in ("criticalChance", "criticalMultiplier", "procChance", "fireRate", "multishot"):
        if main.get(src) is not None:
            w2[src] = main[src]
    set_form_cadence(w2, rec)
    txt = f"形态：{label}（{rec.get('name') or ''} 基础 {(main.get('damage') or {}).get('total', 0):g}"
    if rec.get("charge_seconds"):
        # 蓄力节奏（b6 批起建模）：cadence_fire_rate 按 wiki Fire Rate 换算有效射速
        txt += f"，蓄力 {rec['charge_seconds']:g}s，DPS 已计蓄力时间"
    if (rec.get("burst") or {}).get("count", 0) > 1:
        txt += f"，点射 {rec['burst']['count']} 发/次"
    txt += "）"
    if rec.get("segments"):
        txt += "；范围/集束段换成该形态的"
    if rec.get("forced_procs"):
        txt += "；强制异常：" + "、".join(TYPE_ZH.get(x, x) for x in rec["forced_procs"])
    spec["notes"].append(txt)
    return w2


def _load_evo_data() -> tuple[dict, dict]:
    """懒加载 evolutions.json / lich_valence.json（缺失时给空表）。"""
    global _EVO_DATA, _LICH_DATA
    if _EVO_DATA is None:
        try:
            _EVO_DATA = json.loads((_DATA / "evolutions.json").read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            _EVO_DATA = {}
    if _LICH_DATA is None:
        try:
            _LICH_DATA = json.loads((_DATA / "lich_valence.json").read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            _LICH_DATA = {}
    return _EVO_DATA, _LICH_DATA


# 「进化 <关键词>」→ 要找的效果 kind（按序匹配；一条选项可含多个效果）
_EVO_KEY_KINDS = [
    ("基伤", "flat_base_damage"),
    ("伤害", "flat_base_damage"),
    ("暴击", "flat_base_crit_chance"),
    ("暴伤", "flat_base_crit_multiplier"),
    ("触发", "flat_base_status_chance"),
    ("多重", "flat_base_multishot"),
    ("爆头", "headshot_damage"),
    ("射速", "fire_rate_bonus"),
    ("装填", "reload_speed_bonus"),
    ("连击", "initial_combo"),
    ("穿透", "punch_through_bonus"),
    ("弹匣", "flat_base_magazine"),
]


def _weapon_family_key(weapon: dict) -> str:
    """武器 → wfsim 进化/valence 数据的 key（Latron Prime → latron_prime）。"""
    for src in (weapon.get("name"), weapon.get("zh")):
        if src:
            return re.sub(r"[\s'’]+", "_", str(src).strip().lower())
    return ""


_CO_BEHAVIOR: Optional[dict] = None


def _load_co_behavior() -> dict:
    """CO 桶别表（wfsim co_behavior，scripts/build_co_behavior.py 生成；key = 小写 uniqueName）。"""
    global _CO_BEHAVIOR
    if _CO_BEHAVIOR is None:
        try:
            _CO_BEHAVIOR = json.loads((_DATA / "co_behavior.json").read_text(encoding="utf-8"))[
                "weapons"
            ]
        except Exception:  # noqa: BLE001
            _CO_BEHAVIOR = {}
    return _CO_BEHAVIOR


def co_behavior_of(weapon: dict, incarnon: bool = False) -> str:
    """该武器（当前形态）的 CO 桶别：additive_with_base_damage / independent / inert。"""
    rec = _load_co_behavior().get(str((weapon or {}).get("uniqueName") or "").lower()) or {}
    return rec.get("incarnon" if incarnon else "base") or "additive_with_base_damage"


_DEPLOYMENTS: Optional[dict] = None


def _load_deployments() -> dict:
    """Archgun 双部署面板（wfsim deployments，key = 小写 uniqueName）。"""
    global _DEPLOYMENTS
    if _DEPLOYMENTS is None:
        try:
            _DEPLOYMENTS = json.loads(
                (_DATA / "weapon_deployments.json").read_text(encoding="utf-8")
            )
        except Exception:  # noqa: BLE001
            _DEPLOYMENTS = {}
    return _DEPLOYMENTS


def _apply_deployment(spec: dict, w: dict) -> dict:
    """Archgun 部署切换：空战（archwing）覆盖面板 / 地面·大气（默认）。

    这类武器（Gravimag 空战枪）两套面板不同：默认（atmosphere）= 顶层
    attack；空战模式用 deployments.archwing 的伤害/范围伤害/装填覆盖，
    个别武器还覆盖暴击与弹药。空战的范围伤害作为独立段并入 attacks。
    """
    dep = _load_deployments().get(str(w.get("uniqueName") or "").lower())
    if not dep:
        return w
    mode = spec.get("deployment")
    if mode == "archwing":
        a = dep.get("archwing") or {}
        w2 = dict(w)
        if a.get("damage"):
            w2["damage"] = dict(a["damage"])
        for src, dst in (
            ("criticalChance", "criticalChance"),
            ("criticalMultiplier", "criticalMultiplier"),
        ):
            if a.get(src) is not None:
                w2[dst] = a[src]
        if a.get("reload_seconds"):
            w2["reloadTime"] = a["reload_seconds"]
        if a.get("ammo_max"):
            w2["magazineSize"] = a["ammo_max"]
        if a.get("radial_damage"):
            atks = list(w2.get("attacks") or [])
            atks.append(
                {
                    "name": "空战范围伤害",
                    "damage": dict(a["radial_damage"]),
                    "total": a["radial_damage"].get("total"),
                    "shot_type": "AoE",
                }
            )
            w2["attacks"] = atks
        spec["notes"].append(
            "部署：空战（Archwing）面板"
            + ("；另有空战范围伤害段" if a.get("radial_damage") else "")
            + ("；弹匣按空战值" if a.get("ammo_max") else "")
        )
        return w2
    if mode == "atmosphere":
        spec["notes"].append("部署：地面/大气（Gravimag 默认面板）（可用「空战」切到空战面板）")
    return w


def _apply_panel_overrides(spec: dict, weapon: dict) -> dict:
    """灵化进化选项 + 赤毒/信条/终幕 valence bonus → 改武器基础面板。

    进化（wfsim evolutions 数据）：flat_base_damage/crit/multishot 等
    平坦效果直接改基础面板（MOD 乘区随后作用其上 —— wiki 语义：进化
    改的是武器自身面板）。条件类效果只展示不计入。
    lich bonus：valence 元素额外伤害 = 原总基伤 × pct，计入该元素
    （随基伤 MOD 放大，wiki Valence Fusion 口径）。
    """
    w = dict(weapon)
    if "chargeTime" not in w:
        # 默认形态的蓄力秒数（weapon_attacks.json 顶层只收默认形态 / 我方主段，b6 批）
        _cs = _weapon_attacks_rec(w).get("charge_seconds")
        if _cs:
            w["chargeTime"] = float(_cs)
    dmg = dict(w.get("damage") or {})
    evo, lich = _load_evo_data()
    w = _apply_incarnon_form(spec, w)  # ⓿ 形态（默认原型；写「灵化」才切）
    w = _apply_alt_form(spec, w)  # ⓿a 非默认形态（副开火 / 蓄力 …，显式选了才切）
    w = _apply_deployment(spec, w)  # ⓿b 空战/地面部署（Archgun）
    dmg = dict(w.get("damage") or {})

    total0 = sum(
        float(v)
        for k, v in dmg.items()
        if isinstance(v, (int, float))
        and v > 0
        and k not in ("total", "cinematic", "shieldDrain", "healthDrain", "energyDrain")
    )

    def scale_damage(add_flat: float, elem: Optional[str] = None) -> None:
        """把 add_flat 加进基础伤害：指定元素直加，否则按原构成比例摊。"""
        nonlocal total0
        if add_flat <= 0 or total0 <= 0:
            return
        if elem:
            dmg[elem] = float(dmg.get(elem) or 0.0) + add_flat
        else:
            for k, v in list(dmg.items()):
                if (
                    isinstance(v, (int, float))
                    and v > 0
                    and k not in ("total", "cinematic", "shieldDrain", "healthDrain", "energyDrain")
                ):
                    dmg[k] = v + add_flat * v / total0
        total0 += add_flat

    # ---- ① 灵化进化选项 ----
    used_ids: set = set()
    spec["evo_headshot"] = 0.0
    for key in spec.get("evo_keys") or []:
        kind = next((k for kw, k in _EVO_KEY_KINDS if kw in key), None)
        if not kind:
            spec["notes"].append(
                f"未认识的进化关键词「{key}」（可用：基伤/暴击/暴伤/触发/多重/爆头/射速/装填/连击）"
            )
            continue
        fam = evo.get(_weapon_family_key(weapon)) or {}
        tiers = fam.get("tiers") or {}
        picked = None
        for tier in sorted(tiers, key=lambda x: int(x) if x.isdigit() else 99):
            if tier == "1":  # EVO I 是灵化形态解锁，非选项
                continue
            for opt in tiers[tier]:
                if opt.get("id") in used_ids:
                    continue
                if any(e.get("kind") == kind for e in opt.get("effects") or []):
                    picked = (tier, opt)
                    break
            if picked:
                break
        if not picked:
            spec["notes"].append(f"进化「{key}」：该武器没有匹配的选项（或已用）")
            continue
        tier, opt = picked
        used_ids.add(opt.get("id"))
        applied, shown = [], []
        for e in opt.get("effects") or []:
            kind2, v = e.get("kind"), float(e.get("value") or 0.0)
            if kind2 == "flat_base_damage":
                scale_damage(v)
                applied.append(f"基伤+{v:g}")
            elif kind2 == "flat_base_crit_chance":
                w["criticalChance"] = float(w.get("criticalChance") or 0.0) + v
                applied.append(f"暴击率+{v * 100:g}%")
            elif kind2 == "flat_base_crit_multiplier":
                w["criticalMultiplier"] = float(w.get("criticalMultiplier") or 1.0) + v
                applied.append(f"暴伤+{v:g}x")
            elif kind2 == "flat_base_status_chance":
                w["procChance"] = float(w.get("procChance") or 0.0) + v
                applied.append(f"触发+{v * 100:g}%")
            elif kind2 == "flat_base_multishot":
                w["multishot"] = float(w.get("multishot") or 1.0) + v
                applied.append(f"多重+{v:g}")
            elif kind2 == "fire_rate_bonus":
                w["fireRate"] = float(w.get("fireRate") or 0.0) * (1 + v)
                applied.append(f"射速+{v * 100:g}%")
            elif kind2 == "headshot_damage":
                spec["evo_headshot"] += v
                applied.append(f"爆头伤害+{v * 100:g}%")
            elif kind2 == "initial_combo":
                spec["initial_combo"] = float(spec.get("initial_combo") or 0.0) + v
                applied.append(f"初始连击+{v:g}")
            elif kind2 == "flat_base_magazine":
                w["magazineSize"] = float(w.get("magazineSize") or 0.0) + v
                applied.append(f"弹匣+{v:g}")
            else:
                shown.append(e)
        line = (
            f"EVO{tier} {opt.get('name')}：{'、'.join(applied)}"
            if applied
            else f"EVO{tier} {opt.get('name')}：未计入"
        )
        spec["notes"].append(
            line + ("｜未计入：" + "；".join(f"{e.get('kind')}" for e in shown) if shown else "")
        )
        spec.setdefault("conditional", []).append(
            f"EVO{tier} {opt.get('name')}（条件部分未计入）"
        ) if shown else None

    # ---- ② 赤毒/信条/终幕 valence bonus ----
    lb = spec.get("lich_bonus")
    if lb and lb.get("pct"):
        wkey = _weapon_family_key(weapon)
        val = lich.get(wkey)
        if not val:
            spec["notes"].append("该武器不是赤毒/信条/终幕系列，无回响加成")
        else:
            elem = lb.get("element")
            if not elem:
                elem = next((e for e in val["elements"] if e != "impact"), val["elements"][0])
                spec["notes"].append(f"回响加成元素未指定，按 {elem} 估算（可写 赤毒元素名N 指定）")
            elif elem not in val["elements"]:
                spec["notes"].append(
                    f"回响加成元素 {elem} 不在可滚列表里（"
                    + "、".join(val["elements"])
                    + "），已按原值应用"
                )
            scale_damage(total0 * lb["pct"] / 100.0, elem=elem)
            spec["notes"].append(
                f"回响加成：{elem} +{lb['pct']:g}%（{val['min']:g}-{val['max']:g}%）"
            )

    if dmg:
        dmg["total"] = sum(
            float(v)
            for k, v in dmg.items()
            if isinstance(v, (int, float))
            and v > 0
            and k not in ("total", "cinematic", "shieldDrain", "healthDrain", "energyDrain")
        )
        w["damage"] = dmg
    return w


def calculate(spec: dict, weapon: dict) -> dict:
    """按 v1 公式算对敌伤害；返回 dict（handler 负责拼卡面）。

    ⚠️ 绝不抛异常：武器缺失/数据异常时返回 {"ok": False, "error": ...}，
    由卡面把它显示出来 —— 群聊里「什么都不显示」比「算错」更难排查。

    ★ 不改动入参 spec：入口深拷贝，形态/进化/重击等 note 与 `_armor`、
    `evo_headshot` 只写进副本（经 res["notes"] 等返回）。此前直接写入参，
    同一份 spec 复算会让 notes 逐次累积（「识卡伤害」缓存复用即踩中）。
    """
    spec = copy.deepcopy(spec)
    if not isinstance(weapon, dict) or not isinstance(weapon.get("damage"), dict):
        return {
            "ok": False,
            "mods": spec.get("mods") or [],
            "unknown": spec.get("unknown") or [],
            "error": "没拿到这把武器的基础数据（武器名为空或数据缺失）",
        }
    _weapon_lib = weapon  # 形态/进化改造前的库值（段计算要算基础暴击增量）
    weapon = _apply_panel_overrides(spec, weapon)
    dmg: dict = weapon["damage"]
    fac_table = _load()["fac_table"]
    faction = spec["faction"]
    level = spec["level"]
    enemy = spec.get("enemy")
    base_level = 1
    if enemy:
        # 指定了具体敌人：派系、基准甲、基准等级都按它的真实数值来
        faction = enemy.get("faction") or faction
        base_level = int(enemy.get("base_level") or 1)

    base_armor = spec["base_armor"]
    if base_armor is None:
        base_armor = (
            enemy.get("base_armor") if enemy else DEFAULT_BASE_ARMOR.get(faction, 0.0)
        ) or 0.0
    armor = current_armor(level, base_armor, base_level) if base_armor > 0 else 0.0

    # 派系 MOD（灭亡/歼灭类）按最终派系取用：faction_of 与派系名是包含关系即生效
    # （「Grineer」 ⊂ 「Kuva Grineer」；与 fac_table 按子派系分列的口径一致）。
    # 2026-10-06 修：旧版把派系卡折进通用桶，打任意派系都生效。
    _fac_of_extra = sum(
        float(p)
        for f, p in (spec.get("faction_dmg_of") or [])
        if str(f).lower() in str(faction).lower()
    )
    if _fac_of_extra:
        spec = {**spec, "faction_dmg": float(spec.get("faction_dmg") or 0.0) + _fac_of_extra}

    # 剥甲：腐蚀层数（26% + 6%×每层，封顶 80%）与火剥甲（−50%）先后相乘
    strip = 0.0
    if spec["corrosive"]:
        strip = min(CORROSIVE_BASE + CORROSIVE_STEP * (spec["corrosive"] - 1), CORROSIVE_MAX)
    strip_note = []
    if strip:
        strip_note.append(f"腐蚀{spec['corrosive']}层 −{strip * 100:.0f}%")
    if spec["heat_strip"]:
        strip = 1 - (1 - strip) * (1 - HEAT_STRIP)
        strip_note.append("火剥甲 −50%")
    armor_eff = armor * (1 - strip)
    dr = damage_reduction(armor_eff) if armor_eff > 0 else 0.0
    dr_raw = damage_reduction(armor) if armor > 0 else 0.0

    # 异常状态加伤：病毒 → 打血；磁力 → 打盾/超宏（均 +100% 起，每层 +25%）
    viral_mult = 1.0
    if spec["viral"]:
        viral_mult = 1 + VIRAL_BASE + VIRAL_STEP * (spec["viral"] - 1)
    mag_mult = 1.0
    if spec["magnetic"]:
        mag_mult = 1 + MAGNETIC_BASE + MAGNETIC_STEP * (spec["magnetic"] - 1)

    if spec["steel_path"]:
        # 钢路：血量/护盾 +100%（护甲自 U36 起不再加成）——只影响「击杀发数」，
        # 本卡是对敌伤害，故不乘算，仅提示。
        pass

    pools = compose_elements(spec["singles"])
    fac_mod = 1 + spec["faction_dmg"] / 100.0

    # 架势（近战专用槽）：决定每段倍率 + 强制异常。warframe-items 里架势没有任何
    # 数值，这块只能来自 wiki；默认取「Neutral（站立连招）」的每段平均倍率。
    _st = resolve_stance(spec.get("stance"))
    _st_combo = ((_st or {}).get("combos") or {}).get("Neutral") or {}
    _st_mult = float(_st_combo.get("avg_mult") or 100.0) / 100.0 if _st else 1.0
    _st_procs = list(_st_combo.get("procs") or [])

    # 目标身上异常种类数（异况超量 / 镀层战术的加成基数）
    _types = {
        k.lower()
        for k in dmg
        if k.lower() in STATUS_TABLE and isinstance(dmg.get(k), (int, float)) and dmg.get(k) > 0
    }
    # ★ MOD 加成的元素（合成后）与物理转换目标也会触发 ⇒ 计入种类数（2026-10-09 B3；
    #   wfsim fight/debuffs.rs distinct_statuses 数的是目标身上实际挂着的异常）。
    #   修前只数武器原生类型：Braton Prime 加火90 后仍是 3 种（应 4 种）。
    _types |= {k for k in compose_elements(spec["singles"]) if k in STATUS_TABLE}
    _types |= {k for k in (spec.get("physical_convert") or {}) if k in STATUS_TABLE}
    # CO 桶别按**当前实际计算的形态**取：开了「灵化」且确有灵化数据才查灵化那一行
    _inc_form_on = spec.get("form") == "incarnon" and bool(
        _load_incarnon_forms().get(str(_weapon_lib.get("uniqueName") or "").lower())
    )
    _co_mode = co_behavior_of(_weapon_lib, incarnon=_inc_form_on)
    # 非默认形态（副开火 / 蓄力 …）：CO 桶别是按攻击给的（wfsim catalog.rs:54-56），取该形态
    # 自己的（forms 记录只写非 additive 的）
    _form_kind, _form_rec = selected_form(spec, _weapon_lib)
    if _form_kind in ALT_FORM_KINDS:
        _co_mode = str(_form_rec.get("co_behavior") or "additive_with_base_damage")
    _can_proc = (
        (weapon.get("procChance") or 0.0) > 0
        or spec["status_chance"] > 0
        or spec["status_per_combo"] > 0
        or bool(spec["singles"])
        or bool(_st_procs)
    )
    _n_types = max(
        0,
        int(
            spec["status_types"]
            if spec.get("status_types") is not None
            else (len(_types | set(_st_procs)) if _can_proc else 0)
        ),
    )

    if spec["dmg_per_status"] and _n_types and _co_mode != "additive_with_base_damage":
        spec["notes"].append(
            "异况超量类口径：该武器"
            + ("为独立乘区（不与基伤 MOD 相加）" if _co_mode == "independent" else "不吃此类加成")
            + "（wfsim co_behavior）"
        )

    # 1)+2)+3)+4) 见 _hit_from_damage：基础各类型×基伤、物理 MOD 只加同类型、
    #    元素 MOD 按基伤总量加成并合成、派系 MOD×倍率、护甲减免、病毒/磁力
    per_type, per_type_health, per_type_shield, _ = _hit_from_damage(
        dmg,
        float(dmg.get("total") or 0.0),
        spec,
        fac_table,
        faction,
        dr,
        viral_mult,
        mag_mult,
        n_types=_n_types,
        stance_mult=_st_mult,
        co_mode=_co_mode,
    )
    health = sum(per_type_health.values())
    shield = sum(per_type_shield.values())
    # DoT 种子的基数**不含 CO**（CO 只作用于直击；见 _hit_from_damage 的 with_co）
    _, _, _, base_total_dot = _hit_from_damage(
        dmg,
        float(dmg.get("total") or 0.0),
        spec,
        fac_table,
        faction,
        dr,
        viral_mult,
        mag_mult,
        n_types=_n_types,
        stance_mult=_st_mult,
        co_mode=_co_mode,
        with_co=False,
    )

    # 暴击与爆头（放在 DoT 之前算：DoT 要继承初始命中的暴击/爆头加成）
    # 连击层数（近战）：狂怒/创口溃烂都按「连击倍率那一列」算
    _is_venka = "venka prime" in (weapon.get("name") or "").lower()
    # ★ 初始连击是**下限**，不与连击数相加（2026-10-10 修）：wiki Melee「Initial Combo grants
    #   a minimum value of combo points when idle or after a combo reset」；wfsim
    #   fight/melee.rs:50-61 `held.max(initial.min(held + regen))`（每秒回 40，稳态取满）。
    #   修前相加：连击40 + 邪恶蓄力30 = 70（×4）；应为 max(40, 30) = 40（×3）。
    #   狂怒 / 创口溃烂 / 重击三处统一读这一个数。
    _combo_eff = max(int(spec["combo_hits"]), int(spec["initial_combo"]))
    _combo_tier = combo_multiplier(_combo_eff, venka=_is_venka)[0]
    if float(spec.get("initial_combo") or 0.0) > 0:
        spec["notes"].append(
            f"初始连击 {int(spec['initial_combo'])} 是连击数下限：按 max(连击 "
            f"{int(spec['combo_hits'])}, 初始 {int(spec['initial_combo'])}) = {_combo_eff} 计，"
            "不与连击数相加（wiki Melee）"
        )

    # ⚠️ 暴击率 MOD 是**相对基础值**的百分比加成，不是加绝对值：
    #    wiki 口径「+150% Critical Chance」让 38% 变成 95%（38×2.5），
    #    不是 188%。写成加法的话，斩铁（+120%）会把 20% 算成 140%，
    #    暴击期望从 ×2.58 虚高到 ×5.98（2026-09-16 配卡截图识别时暴露）。
    #    狂怒（Blood Rush）是额外一项，**按 (连击倍率−1) 缩放**：
    #    wiki 原式 CC = 基础 × [1 + MOD加成 + 狂怒×(连击倍率 − 1)]
    cc_base = weapon.get("criticalChance") or 0.0
    cc = cc_base * (
        1 + (spec["crit_chance"] + spec["crit_per_combo"] * (_combo_tier - 1.0)) / 100.0
    )
    cm = (weapon.get("criticalMultiplier") or 1.0) * (1 + spec["crit_dmg"] / 100.0)
    e_body = crit_expectation(cc, cm)
    # 重击的暴击期望：斩铁这类卡的暴击几率加成在重击时翻倍（wiki 26.0.7）
    cc_heavy = cc_base * (1 + (spec["crit_chance"] + spec["crit_chance_heavy"]) / 100.0)
    e_heavy = crit_expectation(cc_heavy, cm)
    # 爆头（2026-10-10 爆头口径批，见 headshot_profile）：
    #   爆头倍率 = 头部基数 × (1 + Σ爆头倍率加成) [× Cernos Prime 自带 1.5]
    #   头部暴击期望 = 1 + 暴率 × (2 × 总暴伤 − 1)（可暴击爆头时；否则同身体）
    _hp = headshot_profile(
        enemy,
        faction,
        str(_weapon_lib.get("uniqueName") or ""),
        float(spec["headshot_bonus"] or 0.0),
        float(spec.get("evo_headshot") or 0.0),
        form_kind=_form_kind,
    )
    head_mult = _hp["mult"]
    head_landing = _hp.get("landing", 1.0)
    head_src = _hp["src"]
    head_crit = _hp["crit"]
    e_head_crit = crit_expectation(cc, 2.0 * cm) if head_crit else e_body
    e_heavy_head = crit_expectation(cc_heavy, 2.0 * cm) if head_crit else e_heavy
    # 头部相对身体的暴击期望比（DoT 继承初始命中的暴击，爆头时同样继承暴击爆头）
    head_crit_ratio = e_head_crit / e_body if e_body else 1.0
    e_head = head_mult * e_head_crit

    # 5) 异常 DoT（6 秒总量）：每秒 = ratio × MOD 后基础伤害 × 派系MOD²
    #    × 状态伤害加成 × 暴击期望（× 爆头倍率，指定爆头时）
    dots: dict[str, float] = {}
    # wiki《Status Effect → DoT Damage Scaling》：初始命中吃到的加成，DoT 同样享受 ——
    # ① 状态伤害 MOD（Elementalist 类，独立乘区）② 暴击（按期望值）
    # ③ 爆头倍率（指定爆头时才乘；电/毒气另乘落点层，见 DOT_HEAD_LANDING）
    # ④ 派系 MOD 结算两次（下面 fac_mod²）
    status_mult = 1 + spec["status_dmg"] / 100.0
    for el, ratio in DOT_RATIO.items():
        if per_type.get(el, 0.0) <= 0:
            continue
        vm = fac_table.get(el, {}).get(faction, 1.0)
        # DoT 累加器起点为 1（wfsim 实测 M58：(ΣSᵢ+1)×C×M）——
        # 单次 proc 即 (基础+1)×系数，基础大时可忽略但结构上应有
        tick = (
            (base_total_dot + 1.0)
            * ratio
            * fac_mod
            * fac_mod
            * vm
            * status_mult
            * e_body
            * _dot_elem_bracket(el, spec)
        )
        if spec["headshot"]:
            tick *= head_mult * head_crit_ratio
            if el in DOT_HEAD_LANDING:
                tick *= head_landing
        if el in DOT_BYPASS_ARMOR:
            dmg_tick = tick * viral_mult  # 流血无视护甲
        elif el in DOT_BYPASS_SHIELD:
            dmg_tick = tick * (1 - dr) * viral_mult  # 毒 DoT 打血
        else:
            dmg_tick = tick * (1 - dr) * viral_mult
        dots[el] = dmg_tick * DOT_SECONDS
    if spec["headshot"] and head_landing != 1.0 and any(e in dots for e in DOT_HEAD_LANDING):
        spec["notes"].append(
            f"电/毒气 DoT 爆头另乘落点层 ×{head_landing:g}（1x 基数 × 爆头加成；"
            "wiki Enemy_Body_Parts §Notes / wfsim M100）"
        )

    ms = (weapon.get("multishot") or 1) * (1 + spec["multishot"] / 100.0)
    # 拟成枪系「出膛预合并」（b8 批，build_damage_data.ORB_PREMERGE_FIX）：多重不多发球，
    # 复制球出膛即并进原球 ⇒ 只有一次爆炸，伤害 = 基础 × min(k, 4)，k = floor / ceil(多重)
    # （取 ceil 概率 = 小数部分）⇒ 期望 = 基础 × min(多重, 4)；暴击 / 触发按这一次爆炸判定，
    # 不随多重放大。wiki Synoid Simulor §Notes「Deals 240 / 480 / 720 / 960 Magnetic damage
    # depending on multishot (capped at 960)」、Simulor §Notes「Multishot will create duplicate
    # orbs … merged with the original orb」。合并伤害 / 奇点 DoT 不吃多重。
    _orb_cap = float(weapon.get("orbMultishotCap") or 0.0)
    _seg_ms = min(ms, _orb_cap) if _orb_cap > 0 else ms
    if _orb_cap > 0:
        spec["notes"].append(
            f"拟成枪系多重（当前 ×{ms:.3g}）= 出膛预合并：只一次爆炸，伤害 × min(多重, {_orb_cap:g}) = ×{_seg_ms:.3g}"
            "（期望值），暴击 / 触发按一次判定；合并伤害、奇点 DoT 不吃多重、未计入 DPS"
            "（wiki Synoid Simulor：240/480/720/960，上限 960；普通拟成枪按同机制 200/400/600/800"
            " 属推论）"
        )
    fire_rate = weapon.get("fireRate") or 0.0
    # ★ 射速 MOD **先**乘进来，再算持续 DPS（2026-10-06 修）。
    # 原来 dps_sustained 用**基础**射速、而下面的 dps 用改造后射速，于是
    # 「持续/爆发」比值会随射速反向缩小：射速 MOD 的收益被抵消（实测：恐惧
    # +90% 射速在指标上收益 ≈0，被自动配卡错误淘汰；而 -20% 的负射速卡反而
    # 被高估）。持续 DPS 的定义就该是「爆发 DPS × 弹匣时间/(弹匣时间+装填)」，
    # 两项同用改造后射速。
    fr_note = ""
    _fr_mult = 1.0
    if spec["no_fire_rate_mod"]:
        fr_note = "射速不可修改，增减已忽略"
    elif spec["fire_rate_pct"]:
        # 只按基础射速乘算（不含其它射速 MOD 的叠加细节）
        _fr_pct = float(spec["fire_rate_pct"])
        _fr_mod = float(spec.get("fire_rate_mod_pct") or 0.0)
        _is_bow = str(_weapon_lib.get("uniqueName") or "").lower() in BOW_CLASS_UNIQUE
        if _is_bow and _fr_mod:
            # 弓/弩：射速 MOD 卡面「(x2 for Bows)」⇒ MOD 部分翻倍（负射速卡同样翻倍）；
            # 赋能/镀层叠层/进化等非 MOD 卡来源不翻倍（wfsim build/loadout/resolve.rs:175、534）
            _fr_pct += _fr_mod
            fr_note = f"含射速 {_fr_pct:+g}%（弓/弩：射速 MOD {_fr_mod:+g}% ×2）"
        else:
            fr_note = f"含射速 {_fr_pct:+g}%"
        _fr_mult = max(0.05, 1 + _fr_pct / 100.0)
        fire_rate *= _fr_mult
    # ★ 每秒有效射弹数（b6 批）：点射 / 蓄力按 wiki Fire Rate 换算；fire_rate 仍是面板射速（卡面
    #   「射速」= 军械库口径），DPS / 持续 DPS / 异常频率一律用 fire_rate_eff
    fire_rate_eff, _cad_note = cadence_fire_rate(weapon, _fr_mult)
    if _cad_note:
        spec["notes"].append(_cad_note)
    # 持续 DPS = 爆发 DPS × 弹匣时间/(弹匣时间+装填)；近战/无弹匣数据则为 None
    # 弹匣时间 = 弹匣 / (有效射速 × 每发耗弹)（wiki AmmoCost：持续射击 0.5、Kohm 满转 4…，b7 批）；
    # 每发耗弹 0（wiki：Cedo 副开火 / Cortege 榴弹等不耗弹）或弓的未蓄力点按（循环已含上弦）⇒ 无装填
    # 循环，持续 = 爆发
    magazine = weapon.get("magazineSize") or 0
    reload_t = weapon.get("reloadTime") or 0.0
    _ac_raw = weapon.get("ammoCost")
    _ammo_cost = 1.0 if _ac_raw is None else float(_ac_raw)
    # noReload：葬铭系不装填（wiki Epitaph §Notes「does not "reload" between shots」，b8 批）
    _no_cycle = _ammo_cost <= 0 or bow_cycle(weapon) or bool(weapon.get("noReload"))
    dps_sustained = None
    if magazine > 0 and fire_rate_eff > 0 and reload_t > 0:
        if _no_cycle:
            dps_sustained = health * ms * fire_rate_eff
        else:
            mag_time = magazine / (fire_rate_eff * _ammo_cost)
            dps_sustained = (health * ms * fire_rate_eff) * mag_time / (mag_time + reload_t)

    # 敌人血量/护盾（指定敌人时）：按派系公式缩放；钢路 ×2（血量与护盾）
    hp = shield_hp = None
    shots = None
    if enemy:
        sp_mult = 2.0 if spec["steel_path"] else 1.0
        hp = (
            (enemy.get("base_health") or 0.0)
            * health_multiplier(faction, level, base_level)
            * sp_mult
        )
        shield_hp = (
            (enemy.get("base_shield") or 0.0)
            * shield_multiplier(faction, level, base_level)
            * sp_mult
        )
        # 击杀发数：护盾段（不吃护甲减免）+ 血量段（吃减免与病毒），
        # 两段都按**暴击期望**后的单发伤害算（即平均发数）
        avg_health = health * e_body
        avg_shield = shield * e_body
        n_shield = math.ceil(shield_hp / avg_shield) if shield_hp > 0 and avg_shield > 0 else 0
        n_health = math.ceil(hp / avg_health) if hp > 0 and avg_health > 0 else 0
        shots = n_shield + n_health

    # 派系弱点提示：本武器用到的类型里哪些吃了 +50%
    weak = sorted({k for k in per_type if fac_table.get(k, {}).get(faction, 1.0) > 1.0})
    resist = sorted({k for k in per_type if fac_table.get(k, {}).get(faction, 1.0) < 1.0})
    fac_weak = sorted(k for k, v in fac_table.items() if v.get(faction, 1.0) > 1.0)
    fac_resist = sorted(k for k, v in fac_table.items() if v.get(faction, 1.0) < 1.0)

    # ------------------------------------------------------------------
    # v1.6：异常稳态 / 近战重击 / 超宏 / Sentient 适应
    # ------------------------------------------------------------------
    spec["_armor"] = armor  # 稳态模型要用「未剥甲」的原始护甲
    # wiki Weeping Wounds 原式（2026-10-09 订正，wfsim fight/resolve.rs:248-253 引用）：
    #   SC = 基础 ×[1 + MOD + 创口溃烂 ×(连击倍率 − 1)] —— 与狂怒（暴击侧）同构、同一括号。
    # 修前写成 基础 ×(1+MOD) ×(1 + 创口 × 连击倍率)：独立乘区且少了 −1，
    # MOD +90%、创口 +40%、12 层时 11.02× 对 6.3×（高估约 75%）。
    status_chance_total = (weapon.get("procChance") or 0.0) * (
        1 + (spec["status_chance"] + spec["status_per_combo"] * (_combo_tier - 1.0)) / 100.0
    )
    # 拟成枪系：一次扳机一次爆炸 ⇒ 触发判定次数不乘多重（b8 批）
    procs = status_procs_per_sec(fire_rate_eff, 1.0 if _orb_cap > 0 else ms, status_chance_total)
    ss = steady_state_dps(
        per_type,
        base_total_dot,  # 稳态里它只用于 DoT 种子 ⇒ 同样不含 CO
        fac_mod,
        fac_table,
        faction,
        procs,
        1 + spec["status_dmg"] / 100.0,
        cc,
        cm,
        head_mult,
        ms,
        fire_rate_eff,
        spec,
        strip,
        head_crit=head_crit,
        head_landing=head_landing,
    )
    ss["status_chance"] = status_chance_total

    # 近战重击：wiki《Melee Combo》——重击吃满连击倍率、并消耗连击数
    heavy_info = None
    if spec["heavy"]:
        hv = weapon.get("heavyAttackDamage") or 0.0
        if hv > 0:
            total_base = float(dmg.get("total") or 0.0) or 1.0
            is_venka = "venka prime" in (weapon.get("name") or "").lower()
            # 重击读的连击数 = 上面同一个 _combo_eff（初始连击是下限，不相加）
            hits = _combo_eff
            mul_h, _mul_n = combo_multiplier(hits, venka=is_venka)
            # ★ 一击必杀类「重击伤害」**进基伤加法组**（2026-10-10 修）：wiki Killing Blow
            #   「Damage bonus is additive to mods such as Pressure Point」；wfsim
            #   fight/pellet.rs:368 把 heavy_attack_base_damage（melee.rs:64-78）加进
            #   arcane_base_damage，再按 co_behavior 进 CO 括号（scale.rs gunco_bucket：
            #   additive ⇒ 1+基伤+重击+CO；independent ⇒ (1+基伤+重击)×(1+CO)）。
            #   修前 ×(1+重击) 独立乘区，压迫点 +165%、一击必杀 +120% 时高估 ≈ 1.6 倍。
            # ★ 重击按**自己的伤害构成整段重算**（不再拿普通攻击的「对血」等比缩放）：
            #   护甲后的「每类型至少 1 点」是非线性的，缩放会把被抬到 1 的小分量一起放大
            #   （Dual Zoren 冲击/穿刺 3.5 即踩中）。架势倍率不进重击：重击用的是武器类别的
            #   重击倍率（heavyAttackDamage 已含），不是架势那一段（wfsim MELEE.md §5b）。
            #   连击倍率是攻击本身的倍率（护甲之前），一并乘进伤害构成再过护甲。
            _hk = hv / total_base * mul_h
            _hdmg = {
                k: float(v) * _hk
                for k, v in dmg.items()
                if k != "total" and isinstance(v, (int, float)) and v > 0
            }
            _hspec = {**spec, "base_dmg": spec["base_dmg"] + spec["heavy_dmg"]}
            _, _h_types, _, _ = _hit_from_damage(
                _hdmg,
                float(hv) * mul_h,
                _hspec,
                fac_table,
                faction,
                dr,
                viral_mult,
                mag_mult,
                n_types=_n_types,
                stance_mult=1.0,
                co_mode=_co_mode,
            )
            _h_health = sum(_h_types.values())
            heavy_info = {
                "base": hv,
                "multiplier": mul_h,
                "combo_hits": hits,
                "initial_combo": int(spec["initial_combo"]),
                "dmg_bonus": spec["heavy_dmg"],
                "crit_cc": cc_heavy,
                "crit_exp": e_heavy,
                "health": _h_health,
                "health_crit": _h_health * e_heavy,
                # 爆头时重击的暴击期望改用暴击爆头口径（e_heavy_head，爆头批）
                "head": _h_health * e_heavy_head * head_mult,
            }
        else:
            spec["notes"].append("该武器没有重击数据（只有近战有）")

    # 多段判定：一把武器常有多段独立伤害（投掷命中 / 投掷爆炸 / 充能投掷 /
    # 震地 / 重击震地 / 灵化形态 / 次级开火…），**各段单独走一遍完整公式**。
    # 卡面只挑「与普通攻击构成不同」的段展示，避免同值噪音。
    # ★ 含段合计只算**默认开火方式在一次扳机里的各阶段**（2026-10-09 修，对齐 wfsim：
    #   每发 = 直击 → 范围 → 集束各一次，MECHANICS.md「stage」；其它开火方式是另一个
    #   play mode，不相加）。段的 `in_total` 标记决定是否计入：
    #   · 与主段相同的段 —— 就是主段本身，不计（修前 134 把武器重复加一次）；
    #   · wfsim 结构化段（weapon_attacks.json，生成器只收带 internal_name 的条目 =
    #     默认形态）—— 计；与某个 attacks 段同签名时只算一次（标在该 attacks 段上）；
    #   · 开「灵化」时灵化形态自己的 AoE 段 —— 计；
    #   · 其余 attacks 段（副开火 / 蓄力 / 投掷 / 开镜档位 / 震地…）—— 只展示不计
    #     （修前 Komorex 把 Unzoomed/2x/3.5x 三档相加）。
    mode_segments: list[dict] = []
    _main_sig = _dmg_sig(dmg)
    _inc_this = _load_incarnon_forms().get(str(weapon.get("uniqueName") or "").lower())
    for _atk in weapon.get("attacks") or []:
        _nm = (_atk.get("name") or "").strip()
        _md = {
            k: float(v)
            for k, v in (_atk.get("damage") or {}).items()
            if isinstance(v, (int, float)) and v > 0 and k != "total"
        }
        if not _nm or not _md or _nm == "Normal Attack":
            continue
        _inc_aoe = False
        _inc_rad: dict = {}  # 与 incarnon_forms.radial 同签名 ⇒ 带上 wfsim 的段标记
        # 灵化武器的 attacks 里带「Incarnon Form / Incarnon Form AoE / … Radial Attack」：
        # 形态替换后主段已等于 Incarnon 主段（重复计一次会翻倍）；
        # 原型（默认或「基础形态」）下不算任何灵化段 —— 灵化段只在「灵化」时成立。
        if _inc_this and "incarnon" in _nm.lower():
            if spec.get("form") != "incarnon":
                continue
            # 2026-10-10：名字叫「… Radial Attack」的（Zylok / Burston / Gammacor 等灵化）
            # 修前被漏掉；签名对上 wfsim 灵化 radial（incarnon_forms.radial）才认作该形态的
            # 范围阶段（Onos「Incarnon Mode Charge Radial Attack」是蓄力模式，wfsim 无此
            # radial ⇒ 不收），并用它的 takes_multishot / takes_condition_overload。
            # Burston 灵化范围段与直击同为 heat 3 —— 是两个阶段，不能按「同主段」去掉。
            _r = _inc_this.get("radial") or {}
            if (
                ("radial" in _nm.lower() or "aoe" in _nm.lower())
                and _r.get("damage")
                and _dmg_sig(_r["damage"]) == _dmg_sig(_md)
            ):
                _inc_rad = _r
            if "aoe" not in _nm.lower() and not _inc_rad:
                continue
            _inc_aoe = True  # 灵化形态自己的范围段：属于该形态一次扳机的阶段
        # total 优先取段自带值（含伤害字典里的 total 键），否则按分量求和
        # —— 修前 total 键混在 _md 里，兜底求和会把它自己也加进去（基数翻倍）
        _mt = float(
            _atk.get("total") or (_atk.get("damage") or {}).get("total") or sum(_md.values())
        )
        _atk_spec = spec
        if _nm.lower() == "heavy slam attack":
            # 重击震地（b8 批）：wiki Melee §Heavy Slams「additionally benefits from the Heavy
            # Attack combo multiplier」「calculated similarly to Heavy Attacks, except that the
            # Heavy Attack Multiplier is always 3x weapon damage」；「Melee Damage on Heavy
            # Attack」（一击必杀类）「only applies when the attack is a Heavy Attack or Heavy Slam」
            _md = {k: v * _combo_tier for k, v in _md.items()}
            _mt *= _combo_tier
            _atk_spec = {**spec, "base_dmg": spec["base_dmg"] + spec["heavy_dmg"]}
        _pt, _pth, _pts, _ = _hit_from_damage(
            _md,
            _mt,
            _atk_spec,
            fac_table,
            faction,
            dr,
            viral_mult,
            mag_mult,
            n_types=_n_types,
            stance_mult=_st_mult,
            co_mode=_co_mode,
            # 范围段不吃 CO（attack.rs:704-708）；直击类段（副开火/投掷…）照常；
            # wfsim 声明 takes_condition_overload 的范围段例外（灵化 Braton / Burston / Zylok）
            with_co=str(_atk.get("shot_type") or "") != "AoE"
            or bool(_inc_rad.get("takes_condition_overload")),
        )
        # 该段自己的暴击参数（充能投掷常比普通高，如 Xoris 22%/20%）
        # 段写明 0 就是 0（b8 批：葬铭「Uncharged AoE」crit_chance 0 被 `or cc_base` 回落成
        # 主段 0.48，卡面字段显示错；暴击期望因暴伤 1 不受影响）；缺字段才继承主段
        _cc_raw = cc_base if _atk.get("crit_chance") is None else float(_atk["crit_chance"]) / 100.0
        _cm_raw = float(_atk.get("crit_mult") or 0.0) or 1.0
        _mcc = _cc_raw * (1 + spec["crit_chance"] / 100.0)
        _mcm = _cm_raw * (1 + spec["crit_dmg"] / 100.0)
        _me = crit_expectation(_mcc, _mcm)
        _sig = _dmg_sig(_md)
        # 有出处的「不吃多重」（一次扳机一次爆炸）只来自 wfsim 灵化 radial 白名单
        _ms_row = ms if _inc_rad.get("takes_multishot", True) else 1.0
        if _orb_cap > 0:
            _ms_row = 1.0  # 拟成枪系：合并伤害 / 奇点 DoT 不吃多重；爆炸段在下面按 min(多重, 4)
        _row = {
            "name": _nm,
            "damage": _md,
            "total": _mt,
            "health": sum(_pth.values()),
            "shield": sum(_pts.values()),
            "health_crit": sum(_pth.values()) * _me,
            "per_type": _pth,
            "crit_cc": _mcc,
            "crit_exp": _me,
            "shot_type": _atk.get("shot_type"),
            "falloff": _atk.get("falloff"),
            "same_as_main": _sig == _main_sig and not _inc_rad,
            "in_total": _inc_aoe and (_sig != _main_sig or bool(_inc_rad)),
            "per_trigger_health": sum(_pth.values()) * _me * _ms_row,
        }
        if _inc_rad:
            _row["takes_multishot"] = bool(_inc_rad.get("takes_multishot", True))
            if _inc_rad.get("takes_condition_overload"):
                _row["takes_condition_overload"] = True
        mode_segments.append(_row)

    # 近战震地：**部分武器**在 attacks 里带 "Slam Attack"（那是最准的），
    # 其余按 wiki 公式估算（b8 批）：wiki Melee §Slam Attack「Slam attacks deal 2x weapon damage
    # (3x for Heavy Slams), with a single element base damage composition (Impact typically, and
    # Blast for Heavy Slams, but the element can vary depending on the weapon)」——wiki
    # Module:Weapons/data/melee 的 Slam / Heavy Slam Attack 逐把核对：2× 210/211、3× 208/211。
    # 修前用 WFCD 顶层 slamAttack / heavySlamAttack（旧版 3× / 4×：Kronen Prime 重击震地 848，
    # wiki 636 爆炸），且重击震地不乘连击倍率。类型按 wiki 典型（冲击 / 爆炸；投掷类沿用电击），
    # 约 66 把与典型不同（wiki 模块逐把列出），未逐把建表 ⇒ 仍标「估算」。
    _has_slam = any("slam" in (m.get("name") or "").lower() for m in mode_segments)
    _wtot = float(dmg.get("total") or 0.0)
    if not _has_slam and _wtot > 0 and (weapon.get("slamAttack") or weapon.get("heavySlamAttack")):
        _slam_type = "electricity" if weapon.get("throwDamage") else "impact"
        _hslam_type = "electricity" if weapon.get("throwDamage") else "blast"
        _hspec_slam = {**spec, "base_dmg": spec["base_dmg"] + spec["heavy_dmg"]}
        for _lbl, _sv, _ty, _sp in (
            ("震地攻击（估算）", 2.0 * _wtot, _slam_type, spec),
            # 重击震地 = 3× 武器伤害 × 重击连击倍率，吃「重击伤害」加成（同上 attacks 段口径）
            ("重击震地（估算）", 3.0 * _wtot * _combo_tier, _hslam_type, _hspec_slam),
        ):
            _sd = {_ty: _sv}
            _pt, _pth, _pts, _ = _hit_from_damage(
                _sd,
                _sv,
                _sp,
                fac_table,
                faction,
                dr,
                viral_mult,
                mag_mult,
                n_types=_n_types,
                stance_mult=_st_mult,
                co_mode=_co_mode,
            )
            mode_segments.append(
                {
                    "name": _lbl,
                    "damage": _sd,
                    "total": _sv,
                    "health": sum(_pth.values()),
                    "shield": sum(_pts.values()),
                    "health_crit": sum(_pth.values()) * e_body,
                    "per_type": _pth,
                    "crit_cc": cc,
                    "crit_exp": e_body,
                    "shot_type": "AoE",
                    "falloff": None,
                    "same_as_main": False,
                }
            )

    # ---- wfsim 结构化额外段（范围/集束）：与 attacks 段同规则并入 ----
    # warframe-items 的 attacks 对爆炸武器常缺 radial 明细，wfsim 的 radial
    # 块（伤害向量/自带宽高暴击/R 半径/衰减/是否吃多重）更完整。按伤害
    # 签名去重，避免与上面 attacks 的段重复。
    _wa = _weapon_attacks_rec(weapon)
    if _form_kind != "default":
        # 选了形态（灵化 / 副开火 / 蓄力 …）：原型的结构化段是**原型**一次扳机的阶段，不参与；
        # 换成该形态自己的（weapon_attacks.json forms，2026-10-10 第三批）。灵化形态没有
        # forms 记录（无额外段）⇒ 空，与修前一致。
        _wa = _form_rec
    _by_sig = {_dmg_sig(m.get("damage")): m for m in mode_segments}
    _seen_sig = set(_by_sig) | {_main_sig}
    # 段暴击 = 段自带值 + 进化 / 回响带来的基础暴击增量。增量的参照是**当前形态**未改造的
    # 面板（默认 = 库值；灵化 = incarnon_forms；副开火等 = forms 主段）—— 段的值本身已是
    # 该形态的（wfsim panel.rs:268-271 缺省继承该形态直击段），不能再加一次形态差。
    _ref = _weapon_lib
    if _form_kind == "incarnon":
        _ref = _inc_this or _weapon_lib
    elif _form_kind != "default":
        _ref = _form_rec.get("main") or _weapon_lib
    _cc_delta = float(weapon.get("criticalChance") or 0.0) - float(
        _ref.get("criticalChance") or 0.0
    )
    _cm_delta = float(weapon.get("criticalMultiplier") or 1.0) - float(
        _ref.get("criticalMultiplier") or 1.0
    )
    for _seg in _wa.get("segments") or []:
        _sd = {
            k: float(v)
            for k, v in (_seg.get("damage") or {}).items()
            if isinstance(v, (int, float)) and v > 0 and k != "total"
        }
        if not _sd:
            continue
        # 段的「每次扳机」口径（2026-10-10 生成器批）：
        #   · takes_multishot 缺省吃多重，只有白名单（有出处）的 false 才会出现在数据里；
        #   · count = 集束子炸弹颗数（attacks 的 Cluster Bomb Contact/Explosion 是单颗值）；
        #   · takes_condition_overload = wfsim 声明吃 CO 的范围段 / 云团（attack.rs:704-708
        #     的例外；桶别随武器 co_behavior，fight/pellet.rs:420-428）；
        #   · merge_only = 云团（lingering）：只给同签名 attacks 段带 CO，不建段、不进合计。
        _takes_ms = bool(_seg.get("takes_multishot", True))
        _count = float(_seg.get("count") or 1.0)
        _seg_co = bool(_seg.get("takes_condition_overload"))
        _merge_only = bool(_seg.get("merge_only"))
        _sig = _dmg_sig(_sd)
        if _sig in _seen_sig:
            # 已有同签名段：不重复建段，但它是默认开火的一个阶段 ⇒ 让那一段计入合计
            # （与主段同签名则就是主段本身，不另计）
            _dup = _by_sig.get(_sig)
            if _dup is not None and not _dup.get("same_as_main"):
                # 会改数值的字段合并到该 attacks 段上（修前只补衰减 / 半径展示元数据，
                # Trumna 等 wfsim 声明吃 CO 的范围段被 attacks 的「AoE 不吃 CO」吃掉）。
                # 段暴击仍取 attacks 段自己的。
                if _seg_co and str(_dup.get("shot_type") or "") == "AoE":
                    _pt, _pth, _pts, _ = _hit_from_damage(
                        _dup["damage"],
                        float(_dup.get("total") or 0.0),
                        spec,
                        fac_table,
                        faction,
                        dr,
                        viral_mult,
                        mag_mult,
                        n_types=_n_types,
                        stance_mult=_st_mult,
                        co_mode=_co_mode,
                        with_co=True,
                    )
                    _dup["health"] = sum(_pth.values())
                    _dup["shield"] = sum(_pts.values())
                    _dup["health_crit"] = _dup["health"] * float(_dup.get("crit_exp") or 1.0)
                    _dup["per_type"] = _pth
                if _seg_co:
                    _dup["takes_condition_overload"] = True
                if _merge_only:
                    _dup["per_trigger_health"] = float(_dup.get("health_crit") or 0.0) * ms
                    continue
                _dup["in_total"] = True
                _dup["takes_multishot"] = _takes_ms
                if _count != 1.0:
                    _dup["count"] = _count
                _dup["per_trigger_health"] = (
                    float(_dup.get("health_crit") or 0.0) * _count * (_seg_ms if _takes_ms else 1.0)
                )
                if _orb_cap > 0:
                    _dup["orb_premerge"] = _orb_cap
                if not _dup.get("falloff") and isinstance(
                    _seg.get("falloff_reduction"), (int, float)
                ):
                    _dup["falloff"] = {
                        "start": _seg.get("falloff_start_m"),
                        "end": _seg.get("radius_m"),
                        "reduction": float(_seg["falloff_reduction"]),
                    }
                if not _dup.get("radius_m") and _seg.get("radius_m"):
                    _dup["radius_m"] = _seg.get("radius_m")
            continue
        if _merge_only:
            continue  # 云团没有对应的 attacks 段：不凭空建段（只为 CO 标记而收）
        _seen_sig.add(_sig)
        _sv = float((_seg.get("damage") or {}).get("total") or sum(_sd.values()))
        _pt, _pth, _pts, _ = _hit_from_damage(
            _sd,
            _sv,
            spec,
            fac_table,
            faction,
            dr,
            viral_mult,
            mag_mult,
            n_types=_n_types,
            stance_mult=_st_mult,
            co_mode=_co_mode,
            with_co=_seg_co,  # 范围 / 集束默认不吃 CO，wfsim 声明的例外才吃
        )
        # 段自带宽高暴击（与主段常不同）；进化/形态引入的基础暴击增量一并计入
        _cc_raw = float(_seg.get("criticalChance") or 0.0) + _cc_delta
        _cm_raw = float(_seg.get("criticalMultiplier") or 1.0) + _cm_delta
        _mcc = _cc_raw * (1 + spec["crit_chance"] / 100.0)
        _mcm = _cm_raw * (1 + spec["crit_dmg"] / 100.0)
        _me = crit_expectation(_mcc, _mcm)
        mode_segments.append(
            {
                "name": _seg.get("name") or "范围伤害",
                "damage": _sd,
                "total": _sv,
                "health": sum(_pth.values()),
                "shield": sum(_pts.values()),
                "health_crit": sum(_pth.values()) * _me,
                "per_type": _pth,
                "crit_cc": _mcc,
                "crit_exp": _me,
                # 子炸弹的接触命中不是范围伤害（wfsim 把它建成一个身位半径的球，panel.rs:313-331）
                "shot_type": "Projectile" if _seg.get("id") == "cluster" else "AoE",
                # ★ 与 attacks 段同构（{start, end, reduction}）：wfsim 给的是裸数字
                #   falloff_reduction（0–1，与 warframe-items 的 reduction 同义），
                #   原样塞进来会让 card_lines 的 .get 崩（Kuva Ayanga 等 9 把默认即崩）
                "falloff": (
                    {
                        "start": _seg.get("falloff_start_m"),
                        "end": _seg.get("radius_m"),
                        "reduction": float(_seg["falloff_reduction"]),
                    }
                    if isinstance(_seg.get("falloff_reduction"), (int, float))
                    else None
                ),
                "same_as_main": False,
                "in_total": True,
                "takes_multishot": _takes_ms,
                "takes_condition_overload": _seg_co,
                "count": _count,
                "radius_m": _seg.get("radius_m"),
                "per_trigger_health": (
                    sum(_pth.values()) * _me * _count * (_seg_ms if _takes_ms else 1.0)
                ),
            }
        )

    # 含段合计：主段每次扳机 + 同一扳机的其它阶段（in_total，见上方口径）
    _in_total = [m for m in mode_segments if m.get("in_total") and m.get("per_trigger_health")]
    _seg_sum = sum(float(m["per_trigger_health"]) for m in _in_total)
    segments_total = (
        {
            "main": health * ms * e_body,
            "segments": _seg_sum,
            "total": health * ms * e_body + _seg_sum,
            "n_segments": len(_in_total),
        }
        if _in_total
        else None
    )

    # 直击 0 的武器（Simulor 系主段 = Orb Launch，伤害全在范围段；b7 批）：DPS 按含段合计算，
    # 否则爆发 / 持续 DPS 恒为 0。含暴击值 = 含段合计（各段自带暴击期望），无暴击值 = 各段除去
    # 自己的暴击期望。其它武器的 DPS 口径不变（仍为主段；范围段见「含段合计（每次扳机）」）。
    dps_seg = None
    _main_base = sum(
        float(v)
        for k, v in dmg.items()
        if isinstance(v, (int, float)) and v > 0 and k != "total" and k.lower() not in _SKIP_TYPES
    )
    if _main_base <= 0 and segments_total:
        _nocrit = sum(
            float(m["per_trigger_health"]) / (float(m.get("crit_exp") or 1.0) or 1.0)
            for m in _in_total
        )
        _sus_ratio = None  # 持续 / 爆发：与主段同一弹匣循环
        if dps_sustained is not None:
            if _no_cycle:
                _sus_ratio = 1.0
            else:
                _mt = magazine / (fire_rate_eff * _ammo_cost)
                _sus_ratio = _mt / (_mt + reload_t)
        dps_seg = {
            "crit": segments_total["total"] * fire_rate_eff,
            "nocrit": _nocrit * fire_rate_eff,
            "sustained_crit": (
                segments_total["total"] * fire_rate_eff * _sus_ratio
                if _sus_ratio is not None
                else None
            ),
        }

    # 投掷（Glaive 类）：奋力一掷「连续投掷时每层 +X% 投掷伤害，最多 N 层」。
    # 投掷基础伤害取武器的 Throw 攻击（数据里单独存着），与普通攻击分开算。
    throw_info = None
    _td = float(spec.get("throw_dmg") or 0.0)
    _tw = weapon.get("throwDamage") or {}
    # ⚠️ 不同批次的数据里 throwDamage 有时带 total、有时只有各类型分量
    _tw_total = float(_tw.get("total") or 0.0) or sum(
        v
        for k, v in _tw.items()
        if isinstance(v, (int, float)) and v > 0 and k.lower() not in _SKIP_TYPES
    )
    if _td > 0 and _tw_total > 0:
        _cap = max(1, int(spec.get("throw_max_stacks") or 1))
        _stacks = max(0, min(int(spec.get("throw_stacks") or 0), _cap))
        _ratio = _tw_total / (dmg.get("total") or 1.0)
        _mult = 1 + _td * _stacks / 100.0
        throw_info = {
            "base": _tw_total,
            "per_stack": _td,
            "stacks": _stacks,
            "max_stacks": _cap,
            "mult": _mult,
            "health": health * _ratio * _mult,
            "health_crit": health * _ratio * _mult * e_body,
            "head": health * _ratio * _mult * e_head_crit * head_mult,
        }

    # 超宏 Overguard：不吃护甲与派系弱点/抗性，虚空 +50%，磁力加成
    og_info = None
    if spec["overguard"]:
        flat = sum(per_type.values()) * fac_mod
        og_info = {
            "damage": overguard_damage(flat, mag_mult, per_type),
            "crit": overguard_damage(flat, mag_mult, per_type) * e_body,
            "mag_mult": mag_mult,
            "pool": spec["overguard"] if isinstance(spec["overguard"], float) else None,
        }

    # Sentient 适应：按伤害占比前 N 个类型各吃 90/80/75/70% 抗性
    adapt_info = None
    if spec["adapt"]:
        _sum_h = sum(per_type_health.values())
        eff, resisted = adapt_damage(per_type_health, spec["adapt"])
        adapt_info = {
            "levels": min(len(ADAPT_RESIST), spec["adapt"]),
            "resisted": resisted,
            "factor": (eff / _sum_h) if _sum_h else 1.0,
        }

    return {
        "ok": True,
        "faction": faction,
        "level": level,
        "armor": armor,
        "dr": dr,
        "dr_raw": dr_raw,
        "armor_eff": armor_eff,
        "strip": strip,
        "strip_note": strip_note,
        "base_armor": base_armor,
        "per_type_health": per_type_health,
        "per_type_shield": per_type_shield,
        "health": health,
        "shield": shield,
        "head_health": health * e_head if spec["headshot"] else None,
        "headshot": spec["headshot"],
        "head_mult": head_mult,
        "head_src": head_src,
        "head_crit": head_crit,
        "head_crit_exp": e_head_crit,
        "head_crit_note": _hp["crit_note"],
        "head_formula": _hp["formula"],
        "crit_cc": cc,
        "crit_cm": cm,
        "crit_exp": e_body,
        "multishot_total": ms,
        "fire_rate": fire_rate,
        "fire_rate_eff": fire_rate_eff,
        "fire_note": fr_note,
        "per_trigger_health": health * ms,
        "dps": health * ms * fire_rate_eff,
        "dps_sustained": dps_sustained,
        "dps_seg": dps_seg,
        "galv_applied": spec.get("galv_applied") or [],
        "magazine": magazine,
        "ammo_cost": _ammo_cost,
        "no_cycle": _no_cycle,
        "reload": reload_t,
        "pools": pools,
        "steel_path": spec["steel_path"],
        "dots": dots,
        "viral_mult": viral_mult,
        "mag_mult": mag_mult,
        "viral": spec["viral"],
        "magnetic": spec["magnetic"],
        "hit_weak": weak,
        "hit_resist": resist,
        "fac_weak": fac_weak,
        "fac_resist": fac_resist,
        "enemy": enemy,
        "hp": hp,
        "shield_hp": shield_hp,
        "shots": shots,
        "base_level": base_level,
        "mods": spec.get("mods") or [],
        "physical": spec["physical"],
        "notes": spec.get("notes") or [],
        "unknown": spec.get("unknown") or [],
        "steady": ss,
        "heavy": heavy_info,
        "overguard": og_info,
        "stance": (
            {
                "name": _st.get("name"),
                "zh": _st.get("zh"),
                "combo": _st_combo.get("name"),
                "mult": _st_mult,
                "procs": _st_procs,
            }
            if _st
            else None
        ),
        "status_types": _n_types,
        "conditional": spec.get("conditional") or [],
        "adapt": adapt_info,
        "throw": throw_info,
        "modes": mode_segments,
        "segments_total": segments_total,
        "arcanes": spec.get("arcanes") or [],
        "crit_cc_heavy": cc_heavy,
        "crit_exp_heavy": e_heavy,
        # 实际参与计算的武器面板（已套形态/部署/进化/回响）。卡面头部必须读它：
        # 2026-10-09 修前头部读库值（拉特昂 Prime 普通形态 基伤 90 / 暴击 22%），而射速与
        # 全部数值按当时默认开的灵化形态（基础 50 / 射速 3.33）算 ⇒「射速 2.66（含 −20%）」
        # 看着像 4.17 被多乘了一次 −20%（同日默认已改回原型，见 _apply_incarnon_form）。
        "weapon_eff": weapon,
    }


# 段名中文（warframe-items 段名是英文；wfsim 段名已中文）
SEG_ZH = {
    "Rocket Impact": "火箭直击",
    "Rocket Explosion": "火箭爆炸",
    "Area Attack": "范围攻击",
    "Charged Attack": "蓄力攻击",
    "Charged Explosion": "蓄力爆炸",
    "Incarnon Form": "灵化形态",
    "Incarnon Form AoE": "灵化形态范围",
    "Slam Attack": "震地攻击",
    "Heavy Slam": "重击震地",
    "Throw": "投掷",
    "Throw Explosion": "投掷爆炸",
    "Explosion": "爆炸",
    "Radial": "范围伤害",
    "Cluster": "集束子炸弹",
    # —— 投掷类（Glaive/战刃）：warframe-items 的段名是英文，逐条汉化 ——
    "Normal Attack": "普通攻击",
    "Throw Impact": "投掷命中",
    "Throw Bounce": "投掷弹跳",
    "Throw Bounce Explosion": "投掷弹跳爆炸",
    "Throw Recall": "投掷回旋",
    "Throw Recall Explosion": "投掷回旋爆炸",
    "Charged Throw": "充能投掷",
    "Charged Throw Impact": "充能投掷命中",
    "Charged Throw Explosion": "充能投掷爆炸",
    "Charged Throw Bounce Explosion": "充能投掷弹跳爆炸",
    "Charged Throw Recall Explosion": "充能投掷回旋爆炸",
    "Power Throw": "奋力一掷",
    "Glaive Radial Attack": "战刃范围攻击",
    "Charge": "蓄力",
    "Charged Shot": "蓄力射击",
    "Uncharged Shot": "未蓄力射击",
    "Quick Shot": "速射",
    "Perfect Shot": "完美射击",
    "Full Auto Mode": "全自动模式",
    "Alt-Fire": "次要开火",
    "Alt-Fire Explosion": "次要开火爆炸",
    "Secondary Fire": "次要开火",
    "Secondary Fire AoE": "次要开火范围",
    "Radial Attack": "范围攻击",
    "Cannon Mode Projectile": "炮击模式直击",
    "Cannon Mode Explosion": "炮击模式爆炸",
    "Cannon Mode Cluster Bomb Contact": "炮击集束弹接触",
    "Cannon Mode Cluster Bomb Explosion": "炮击集束弹爆炸",
    "Glass Explosion": "玻璃爆炸",
    "Slug Impact": "弹丸直击",
    "Grenade Impact": "榴弹直击",
    "Grenade Detonation": "榴弹引爆",
    "Projectile Impact": "弹丸直击",
    "Orb Merging Damage": "球体融合伤害",
    "Orb Launch": "发射球体（直击）",
    "Orb Explosion": "球体爆炸",
    "Fully Stacked Orb Singularity": "满层奇点（持续）",
    "Mid-Flight Detonation": "飞行中引爆",
    "Embedded Detonation": "黏附后引爆",
    "Dual Swords Normal Attack": "双剑形态·普通攻击",
    "Dual Swords Slam Attack": "双剑形态·震地攻击",
    "Dual Swords Heavy Slam Attack": "双剑形态·重击震地",
    "Poison Cloud": "毒气云",
    "Corrosive DoT": "腐蚀持续伤害",
    "Toxin Cloud": "毒素云",
    "Spear Throw": "长矛投掷",
    "Auto": "全自动",
    "Semi": "半自动",
    "Burst": "点射",
    "Slug": "弹丸",
    "Charged": "蓄力",
}

_NOTE_ZH = (
    ("Fire Rate cannot be modified", "射速不可修改"),
    ("Only compatible with Semi-Auto Trigger", "仅限半自动扳机"),
    ("Only compatible with Semi-Auto", "仅限半自动"),
)


def _note_zh(note: str) -> str:
    """MOD 的使用限制说明在游戏数据里是英文，翻一下常用几条。"""
    out = note
    for en, zh in _NOTE_ZH:
        out = out.replace(en, zh)
    return out.replace(". ", "；").rstrip(".").strip()


# ---------------------------------------------------------------------------
# 异常状态表 / 近战连击 / Sentient 适应（数据源：英文 wiki，2026-09-16 抓取）
# ---------------------------------------------------------------------------
STATUS_TABLE: dict[str, dict] = {
    #  dur：单层持续时间(s)；max：层数上限（None = 不限）；ratio：DoT 每秒系数
    # 冲击 6s（wiki Damage/Impact_Damage；wfsim data/debuffs/stagger.yaml:19 duration_seconds 6）。
    # 2026-10-10 由 1.0 订正；冲击层数在稳态模型里不参与任何数值（只影响处决阈值），改动不动数。
    "impact": {"dur": 6.0, "max": 5},
    "puncture": {"dur": 10.0, "max": 5},
    "slash": {"dur": 6.0, "max": None, "ratio": 0.35, "bypass_armor": True},
    "heat": {"dur": 6.0, "max": None, "ratio": 0.50},
    "cold": {"dur": 6.0, "max": 10},
    "electricity": {"dur": 6.0, "max": None, "ratio": 0.50},
    "toxin": {"dur": 6.0, "max": None, "ratio": 0.50, "bypass_shield": True},
    "blast": {"dur": 1.5, "max": 10, "ratio": 0.30, "instant": True},
    "corrosive": {"dur": 8.0, "max": 10},
    "gas": {"dur": 6.0, "max": 10, "ratio": 0.50},
    "magnetic": {"dur": 6.0, "max": 10},
    "radiation": {"dur": 12.0, "max": 10},
    "viral": {"dur": 6.0, "max": 10},
    "void": {"dur": 3.0, "max": None},
    "tau": {"dur": 8.0, "max": 10},
}
# Sentient 适应：血线 25/45/65/80% 各适应一次，按「打得最多的类型」排序取抗性
ADAPT_RESIST = (0.90, 0.80, 0.75, 0.70)
OVERGUARD_VOID_BONUS = 0.50  # 超宏：虚空 +50%，其余类型不吃派系倍率
COMBO_STEP = 20  # 近战连击：每 20 连击一层
# 冰冻（wiki Damage/Cold_Damage；wfsim data/debuffs/frozen.yaml）
FROZEN_STACKS = 10  # 第 10 层冰 ⇒ 冻结
FROZEN_SECONDS = 3.0  # 冻结持续
FROZEN_THAW_STACKS = 3  # 解冻后剩 3 层
FROZEN_CM_BONUS = 1.0  # 冻结期间暴伤 +1.0x（替换阶梯的 +0.5，不叠加）
COMBO_CAP_TIER = 12  # 220 连击（12 层）封顶；Venka Prime 可到 13 层未建模


def combo_multiplier(hits: int, venka: bool = False) -> tuple[float, float]:
    """近战连击倍率 → (重击倍率, 普通近战倍率)（wiki《Melee Combo》表）。

    20 连击起：重击 2.0x / 普通近战 1.25x；每 +20 连击再加 1.0x / 0.25x，
    220 连击（12 层）封顶 → 12.0x / 3.75x（Venka Prime 240 连击可到 13 层）。
    """
    # 表里「层数 T」对应 (T−1)×20 连击：20→2 层、40→3 层、220→12 层、240→13 层
    cap = 13 if venka else COMBO_CAP_TIER
    hits = max(0, int(hits))
    if hits < COMBO_STEP:
        return 1.0, 1.0
    tier = min(cap, hits // COMBO_STEP + 1)
    return float(tier), 1.0 + 0.25 * (tier - 1)


def status_procs_per_sec(fire_rate: float, multishot: float, chance: float) -> float:
    """每秒异常触发次数 = 射速 × 多重 × 触发率（多重弹片各自判定）。"""
    return max(0.0, fire_rate) * max(0.0, multishot) * max(0.0, chance)


def _stack_multiplier(base: float, step: float, stacks: int, cap: int = 10) -> float:
    """病毒/磁力那类「首层 +base，之后每层 +step」的加伤倍率。"""
    n = min(cap, max(0, int(stacks)))
    if n <= 0:
        return 1.0
    return 1.0 + base + step * (n - 1)


def steady_state_dps(
    per_type: dict[str, float],
    base_after: float,
    fac_mod: float,
    fac_table: dict,
    faction: str,
    procs_per_sec: float,
    status_mult: float,
    crit_cc: float,
    crit_cm: float,
    head_mult: float,
    multishot_total: float,
    fire_rate: float,
    spec: dict,
    strip_explicit: float = 0.0,
    head_crit: bool = False,
    head_landing: float = 1.0,
) -> dict:
    """异常稳态模型：把「持续打同一目标」的实战 DPS 拆成 直伤 + DoT 两部分。

    这是**稳态近似**：假设目标已经在挨打（病毒/腐蚀层数已建立、DoT 已叠满），
    不模拟前几秒的爬升，也不模拟目标死亡/换目标。Proc 类型按伤害占比分配
    （wiki：proc 类型由伤害分布决定）。

    Returns:
        含 `dps_total` / `dps_direct` / `dps_dot` / 层数与剥甲细节 的 dict。
    """
    total = sum(v for v in per_type.values() if v > 0)
    if total <= 0 or procs_per_sec <= 0:
        return {"procs_per_sec": procs_per_sec, "dps_total": None}

    # 每个伤害类型的 proc 频率
    rate = {k: procs_per_sec * (v / total) for k, v in per_type.items() if v > 0}

    def stacks_of(el: str) -> float:
        cfg = STATUS_TABLE.get(el)
        if not cfg:
            return 0.0
        r = rate.get(el, 0.0)
        active = r * cfg["dur"]
        if cfg["max"] is not None:
            active = min(cfg["max"], active)
        return active

    # ① 病毒 / 磁力：层数 → 加伤倍率
    viral_stacks = stacks_of("viral")
    viral_mult = _stack_multiplier(VIRAL_BASE, VIRAL_STEP, viral_stacks)
    mag_stacks = stacks_of("magnetic")
    mag_mult = _stack_multiplier(MAGNETIC_BASE, MAGNETIC_STEP, mag_stacks)

    # ② 腐蚀（8s / 10 层，26%+6%/层，封顶 80%）与火（逐步剥 50%）
    cor_stacks = stacks_of("corrosive")
    strip = 0.0
    if cor_stacks >= 1:
        strip = min(CORROSIVE_BASE + CORROSIVE_STEP * (cor_stacks - 1), CORROSIVE_MAX)
    if spec.get("heat_strip") or rate.get("heat", 0.0) > 0:
        strip = 1 - (1 - strip) * (1 - HEAT_STRIP)
    # 与玩家手写的剥甲（腐蚀N / 火剥甲）合并：两者独立相乘
    strip = 1 - (1 - strip) * (1 - max(0.0, min(1.0, strip_explicit)))
    armor_ss = float(spec.get("_armor") or 0.0) * (1 - strip)
    dr_ss = damage_reduction(armor_ss) if armor_ss > 0 else 0.0

    # ③ 冰 +0.5 暴伤、穿刺 +25% 暴率（封顶，加法叠加在 MOD 之后）
    cold_stacks = stacks_of("cold")
    cm_bonus = 0.0
    frozen_share = 0.0
    if cold_stacks >= FROZEN_STACKS:
        # ★ 满 10 层 = 冻结（2026-10-10 修）：wiki Damage/Cold_Damage「On the 10th stack,
        #   enemies become Frozen for 3 seconds … increasing the Critical Damage Multiplier
        #   bonus to +1.0x. After the Frozen effects wears off, 3 Cold stacks will remain」+
        #   「Frozen enemies cannot receive additional Cold stacks」；wfsim
        #   data/debuffs/frozen.yaml:20-24（+1.00 **替换**阶梯的 +0.50、只作用武器直击）。
        #   稳态 = 循环「冻结 3s（+1.0）→ 解冻剩 3 层 → 再挨 7 次冰触发爬回 10 层」：
        #   爬升段每层停留 1/r 秒、值 = 阶梯 3..9 层（0.20..0.50，和 2.45）。冻结期间命中照常
        #   （伤害与时间成正比，暴击期望对暴伤线性）⇒ 按时间加权平均：
        #     (3×1.0 + 2.45/r) / (3 + 7/r)，r = 冰触发/秒（r×6 ≥ 10 时爬升 7/r ≤ 4.2s < 6s，
        #   解冻剩下的 3 层不会先过期，循环成立）。修前封顶 +0.5（从不冻结）。
        r_cold = rate.get("cold", 0.0)
        climb = (FROZEN_STACKS - FROZEN_THAW_STACKS) / r_cold
        ladder = sum(
            0.1 + 0.05 * (k - 1) for k in range(FROZEN_THAW_STACKS, FROZEN_STACKS)
        )  # 3..9 层
        cycle = FROZEN_SECONDS + climb
        cm_bonus = (FROZEN_SECONDS * FROZEN_CM_BONUS + ladder / r_cold) / cycle
        frozen_share = FROZEN_SECONDS / cycle
    elif cold_stacks >= 1:
        cm_bonus = min(0.5, 0.1 + 0.05 * (cold_stacks - 1))
    pun_stacks = stacks_of("puncture")
    cc_bonus = min(0.25, 0.05 * pun_stacks) if pun_stacks >= 1 else 0.0
    e_crit_ss = crit_expectation(crit_cc + cc_bonus, crit_cm + cm_bonus)
    # 爆头时 DoT 继承暴击爆头（暴伤 ×2，wfsim pellet.rs:569-575 的 crit_multiplier 传入 proc）
    _head_crit_ratio = (
        crit_expectation(crit_cc + cc_bonus, 2.0 * (crit_cm + cm_bonus)) / e_crit_ss
        if head_crit and e_crit_ss
        else 1.0
    )

    # ④ 直伤（稳态层数下的白字 × 稳态暴击 × 病毒）
    direct = 0.0
    for k, v in per_type.items():
        vm = fac_table.get(k, {}).get(faction, 1.0)
        d = v * fac_mod * vm
        if dr_ss > 0 and k != "toxin":
            d = max(d * (1 - dr_ss), 1.0)
        direct += d
    direct_dps = direct * viral_mult * e_crit_ss * multishot_total * fire_rate

    # ⑤ DoT 流：每种异常单独的「活跃层数 × 每秒每层伤害」
    # 火焰单例累加（2026-10-10 第三批，见 FIRE_SECONDS_DEFAULT 的口径）：开火 T 秒内的平均层数
    fire_seconds = float(spec.get("fire_seconds") or FIRE_SECONDS_DEFAULT)
    r_heat = rate.get("heat", 0.0)
    heat_refreshed = r_heat * STATUS_TABLE["heat"]["dur"] >= 1.0
    heat_stacks = (
        r_heat * (fire_seconds / 2.0) if heat_refreshed else r_heat * STATUS_TABLE["heat"]["dur"]
    )
    dots_detail: dict[str, float] = {}
    dot_dps = 0.0
    for el, cfg in STATUS_TABLE.items():
        ratio = cfg.get("ratio")
        if not ratio or rate.get(el, 0.0) <= 0:
            continue
        vm = fac_table.get(el, {}).get(faction, 1.0)
        per_stack = (
            base_after
            * ratio
            * fac_mod
            * fac_mod
            * vm
            * status_mult
            * e_crit_ss
            * _dot_elem_bracket(el, spec)
        )
        if spec.get("headshot"):
            per_stack *= head_mult * _head_crit_ratio
            if el in DOT_HEAD_LANDING:  # 电/毒气落点层（见 DOT_HEAD_LANDING）
                per_stack *= head_landing
        if cfg.get("instant"):  # 爆炸：1.5s 后一次性结算
            contribution = rate[el] * per_stack
        else:
            active = heat_stacks if el == "heat" else rate[el] * cfg["dur"]
            if cfg["max"] is not None:
                active = min(cfg["max"], active)
            if cfg.get("bypass_armor"):
                contribution = active * per_stack
            else:
                contribution = active * per_stack * (1 - dr_ss)
        dots_detail[el] = contribution * viral_mult
        dot_dps += contribution * viral_mult

    return {
        "procs_per_sec": procs_per_sec,
        "viral_stacks": viral_stacks,
        "viral_mult": viral_mult,
        "mag_stacks": mag_stacks,
        "mag_mult": mag_mult,
        "corrosive_stacks": cor_stacks,
        "strip": strip,
        "dr": dr_ss,
        "cold_stacks": cold_stacks,
        "cm_bonus": cm_bonus,
        "frozen_share": frozen_share,
        "puncture_stacks": pun_stacks,
        "cc_bonus": cc_bonus,
        "crit_exp": e_crit_ss,
        "dps_direct": direct_dps,
        "dps_dot": dot_dps,
        "dps_total": direct_dps + dot_dps,
        "dots_detail": dots_detail,
        "fire_seconds": fire_seconds,
        "heat_stacks": heat_stacks if r_heat > 0 else 0.0,
        "heat_refreshed": heat_refreshed,
    }


def overguard_damage(per_type_flat: float, mag_mult: float, per_type: dict[str, float]) -> float:
    """对超宏（Overguard）伤害：不吃护甲减免、不吃派系倍率，虚空 +50%。

    wiki《Overguard》：neutral to all damage types except +50% from Void；
    Magnetic 对其加伤与护盾同理；免疫异常状态（因此病毒/剥甲/DoT 都不作用于它）。
    """
    void_share = per_type.get("void", 0.0)
    total = sum(v for k, v in per_type.items() if k != "void")
    dmg = total + void_share * (1 + OVERGUARD_VOID_BONUS)
    return dmg * mag_mult


def adapt_damage(per_type: dict[str, float], levels: int) -> tuple[float, dict]:
    """Sentient 适应：按伤害占比从高到低，前 N 个类型各吃 90/80/75/70% 抗性。

    wiki《Sentient》：血线 25/45/65/80% 各适应一次，最多 4 种；
    每次抗性 = 90/80/75/70%（依次递减）。虚空伤害可重置适应（卡面提示）。
    """
    levels = max(0, min(len(ADAPT_RESIST), int(levels)))
    order = sorted((k for k, v in per_type.items() if v > 0), key=lambda k: -per_type[k])
    resisted = {}
    remaining = dict(per_type)
    for i, k in enumerate(order[:levels]):
        r = ADAPT_RESIST[i]
        resisted[k] = r
        remaining[k] = remaining[k] * (1 - r)
    return sum(remaining.values()), resisted


def card_lines(weapon: dict, spec: dict, res: dict, alts: Optional[list] = None) -> list[str]:
    """伤害计算卡的正文行（独立成模块函数：便于离线渲染探针与测试复用）。

    U36 抗性重构后，元素之间的差异只有两处：**派系弱点 ±50%** 与**异常状态**
    （病毒/磁力加伤、腐蚀与火剥甲、DoT）。卡面把这两处显式写出来，
    免得用户以为「换了元素数值却一样」是算错了。
    """
    if res and res.get("ok") is False:  # 绝不返回空：出错也要有卡面
        out = [f"◆ 原因：{res.get('error') or '数据缺失，无法计算'}"]
        if res.get("unknown"):
            out.append("◆ 未识别（已忽略）：" + "、".join(res["unknown"]))
        out.append("※ ⚠ 怎么改：换英文名或完整中文名再试；「伤害」不带参数可看用法。")
        return out
    # 头部与「面板口径」都按**实际参与计算**的面板（形态/进化已套用），库值只取名字与 MR
    pw = res.get("weapon_eff") or weapon or {}
    dt = pw.get("damage") or {}
    base_parts = "、".join(
        f"{TYPE_ZH.get(k, k)}{round(v, 1):g}"
        for k, v in dt.items()
        if isinstance(v, (int, float))
        and v > 0
        and k not in ("total", "cinematic", "shieldDrain", "healthDrain", "energyDrain")
    )
    mod_bits = []
    if spec["base_dmg"]:
        mod_bits.append(f"基伤+{spec['base_dmg']:g}%")
    if spec["multishot"]:
        mod_bits.append(f"多重+{spec['multishot']:g}%")
    if spec["crit_chance"]:
        mod_bits.append(f"暴率+{spec['crit_chance']:g}%")
    if spec["crit_dmg"]:
        mod_bits.append(f"暴伤+{spec['crit_dmg']:g}%")
    if spec["faction_dmg"]:
        mod_bits.append(f"派系+{spec['faction_dmg']:g}%")
    _singles = spec.get("singles") or {}
    for el, pct in _singles.items():
        mod_bits.append(f"{TYPE_ZH.get(el, el)}+{pct:g}%")
    if len(_singles) > 1 and res.get("pools"):
        # 顺带标出合成结果，免得用户以为「火90冰30」是两项独立伤害
        mod_bits.append(
            "合成 " + "、".join(f"{TYPE_ZH.get(k, k)}+{v:g}%" for k, v in res["pools"].items())
        )
    for el, pct in res["physical"].items():
        mod_bits.append(f"{TYPE_ZH.get(el, el)}+{pct:g}%")
    if spec.get("headshot_bonus"):
        mod_bits.append(f"爆头倍率+{spec['headshot_bonus']:g}%")
    cc = pw.get("criticalChance") or 0.0
    cm = pw.get("criticalMultiplier") or 1.0
    ms = pw.get("multishot") or 1
    # 射速按游戏升级界面「基础 ▸ 终值」两列写：只有射速 MOD 生效时才出现箭头
    _fr_base = float(pw.get("fireRate") or 0.0)
    _fr_txt = f"{res['fire_rate']:.2f}".rstrip("0").rstrip(".")
    if _fr_base and abs(_fr_base - res["fire_rate"]) > 1e-6:
        _fr_txt = f"{_fr_base:.2f}".rstrip("0").rstrip(".") + " ▸ " + _fr_txt
    if not _fr_base and not res["fire_rate"]:
        _fr_txt = "—"  # Lanka：wiki 面板射速 math.huge（不限），节奏全由蓄力决定
    _fr_eff = res.get("fire_rate_eff")
    if _fr_eff is not None and abs(_fr_eff - res["fire_rate"]) > 1e-6:
        # 点射 / 蓄力：面板射速 ≠ 每秒有效射弹数（DPS 用后者，算式见备注）
        _fr_txt += f"（有效 {_fr_eff:.3g} 发/s）"

    ssr = res.get("steady") or {}
    crit_exp = res["crit_exp"]
    pt = res["per_trigger_health"]
    dps = res["dps"]
    _dseg = res.get("dps_seg")

    # ==================================================================
    # 行文法（2026-10-11 卡片改版）：卡首 KV 答案 → 单发 → 专项 → 输入回显 → 分段表 → 注脚
    # ==================================================================
    # —— 卡首答案（≤4 行）：实战 / 爆发 / 持续 DPS + 击杀 ——
    kv: list[str] = []
    if ssr.get("dps_total"):
        kv.append(
            f"◆ 实战 DPS：≈ {ssr['dps_total']:.0f}"
            f"｜直伤 {ssr['dps_direct']:.0f} + DoT {ssr['dps_dot']:.0f}"
        )
    if _dseg:
        # 直击 0（Simulor 系，b7 批）：DPS 按含段合计（各段自带暴击期望）
        kv.append(
            f"◆ 爆发 DPS：{_dseg['crit']:.0f}（含暴击）"
            + (
                f"｜{_dseg['nocrit']:.0f}（无暴击）"
                if abs(_dseg["crit"] - _dseg["nocrit"]) > 1e-9
                else ""
            )
            + "｜直击 0，按含段合计"
        )
    else:
        kv.append(
            f"◆ 爆发 DPS：{dps * crit_exp:.0f}（含暴击）"
            + (f"｜{dps:.0f}（无暴击）" if crit_exp != 1.0 else "")
        )
    if _dseg:
        _sus = _dseg.get("sustained_crit")
    else:
        _sus = res["dps_sustained"] * crit_exp if res.get("dps_sustained") else None
    if _sus:
        _ac = float(1.0 if res.get("ammo_cost") is None else res["ammo_cost"])
        if res.get("no_cycle"):
            kv.append(
                f"◆ 持续 DPS：≈ {_sus:.0f}"
                + (
                    "（该形态不耗弹，无装填循环）"
                    if _ac <= 0
                    else "（不装填：射击间隔只看射速，面板装填时间不起作用；wiki 葬铭 Notes）"
                    if pw.get("noReload")
                    else "（未蓄力点按：每发一次上弦，循环已含装填）"
                    if nock_only(pw)
                    else "（弓：有效射速已按 1/(蓄力 + 上弦) 含装填，爆发 = 持续）"
                )
            )
        else:
            kv.append(
                f"◆ 持续 DPS：≈ {_sus:.0f}"
                # 弹匣 = wiki 弹药口径（b8 批 MAGAZINE_FIX）；每发耗弹 ≠ 1 时写出折算发数
                + (
                    f"（弹匣 {res['magazine']:g} ÷ 每发耗 {_ac:g} 弹 = "
                    f"{res['magazine'] / _ac:.3g} 发"
                    if _ac != 1.0
                    else f"（{res['magazine']:.0f} 发弹匣"
                )
                + f" + 装填 {res['reload']:.1f}s 循环）"
            )
    if res.get("hp") is not None:
        _pool = f"血量 {res['hp']:.0f}"
        if res.get("shield_hp"):
            _pool = f"护盾 {res['shield_hp']:.0f} → " + _pool
        kv.append(f"◆ 击杀：平均 {res['shots']} 发（身体、含暴击期望）｜目标{_pool}")
    lines: list[str] = kv[:]

    # —— 单发：对血 / 对盾 / 每次扳机 / 爆头 / 暴击期望 / 明细 / DoT / 触发 ——
    sing: list[str] = []
    _mult = res["multishot_total"]
    _merged = _mult <= 1 and abs(pt - res["health"]) < 0.5
    _row = f"· 对血　{res['health']:.0f}（无暴击）"
    if _merged and crit_exp != 1.0:
        _row += f"｜{pt * crit_exp:.0f}（含暴击）"
    sing.append(_row)
    sing.append(f"· 对盾　{res['shield']:.0f}")
    if not _merged:
        _crit_txt = f"｜{pt:.0f}（无暴击）" if crit_exp != 1.0 else ""
        _lab = f"每次扳机（多重 ×{_mult:.2f}）" if _mult > 1 else "单发"
        sing.append(f"· {_lab}　{pt * crit_exp:.0f}（含暴击）{_crit_txt}")
    # 多段合计：主段 + 各额外段（范围/AoE/集束…），段吃同样的 MOD 乘区
    _st = res.get("segments_total")
    if _st and _st.get("n_segments"):
        _parts = [f"主段 {_st['main']:.0f}"]
        _detail = []
        for _m in res.get("modes") or []:
            _pv = float(_m.get("per_trigger_health") or 0.0)
            if _pv <= 0 or not _m.get("in_total"):
                continue
            _extra = []
            if _m.get("radius_m"):
                _extra.append(f"半径 {_m['radius_m']:g}m")
            if float(_m.get("count") or 1.0) != 1.0:
                _extra.append(f"×{float(_m['count']):g} 颗")
            if _m.get("takes_multishot") is False:
                _extra.append("不吃多重")
            if _m.get("takes_condition_overload"):
                _extra.append("吃异况超量")
            _detail.append(
                f"{SEG_ZH.get(_m['name'], _m['name'])} {_pv:.0f}"
                + (f"（{'、'.join(_extra)}）" if _extra else "")
            )
        sing.append(f"· 含段合计　{_st['total']:.0f}（含暴击）＝" + " + ".join(_parts + _detail))
        sing.append(
            "　段是独立命中实例，各吃同一套 MOD 乘区；范围段按命中中心（100%）计，边缘会衰减"
        )
    _hce = res.get("head_crit_exp") or crit_exp
    sing.append(
        f"· 爆头期望　{res['health'] * _hce * res['head_mult']:.0f}"
        f"（含暴击{'与暴击爆头' if res.get('head_crit') else ''}；爆头倍率 ×{res['head_mult']:.2f}）"
    )
    if res.get("head_src"):
        _hl = f"　爆头口径：{res['head_src']}"
        if res.get("head_formula"):
            _hl += f"；倍率 = {res['head_formula']}"
        if res.get("head_crit_note"):
            _hl += f"；{res['head_crit_note']}"
        sing.append(_hl)
    sing.append(
        f"· 暴击期望　×{crit_exp:.2f}"
        f"（暴率 {res['crit_cc'] * 100:.0f}% ×暴伤 {res['crit_cm']:.2f}）"
        f"｜单发 {res['health'] * crit_exp:.0f}"
    )
    top = sorted(res["per_type_health"].items(), key=lambda kv_: -kv_[1])[:4]
    if len(top) > 1:
        sing.append("· 明细　" + "、".join(f"{TYPE_ZH.get(k, k)} {v:.0f}" for k, v in top if v > 0))
    if res["dots"]:
        sing.append(
            "· 异常 DoT　"
            + "、".join(
                f"{TYPE_ZH.get(k, k)} {v:.0f}"
                for k, v in sorted(res["dots"].items(), key=lambda kv_: -kv_[1])
            )
            + "（6s 总量，含暴击期望）"
        )
    if ssr.get("dps_total"):
        bits = [f"{ssr['status_chance'] * 100:.0f}%（{ssr['procs_per_sec']:.2f} 次/秒）"]
        if ssr.get("viral_stacks"):
            bits.append(f"病毒 {ssr['viral_stacks']:.0f} 层 ×{ssr['viral_mult']:.2f}")
        if ssr.get("corrosive_stacks"):
            bits.append(f"腐蚀剥甲 {ssr['strip'] * 100:.0f}%")
        if ssr.get("cm_bonus"):
            if ssr.get("frozen_share"):
                bits.append(
                    f"冰满层冻结 {ssr['frozen_share'] * 100:.0f}% 时间"
                    f"（+1.0/阶梯均值 +{ssr['cm_bonus']:.2f}暴伤）"
                )
            else:
                bits.append(f"冰 +{ssr['cm_bonus']:.2f}暴伤")
        if ssr.get("cc_bonus"):
            bits.append(f"穿刺 +{ssr['cc_bonus'] * 100:.0f}%暴率")
        if ssr.get("heat_stacks"):
            bits.append(
                f"火 {ssr['heat_stacks']:.1f} 层"
                + (
                    f"（单例累加：开火 {ssr['fire_seconds']:g}s 内平均 = 触发率×{ssr['fire_seconds']:g}/2）"
                    if ssr.get("heat_refreshed")
                    else "（触发间隔 > 6s，每层独立过期 = 触发率×6）"
                )
            )
        det = "、".join(
            f"{TYPE_ZH.get(k, k)} {v:.0f}"
            for k, v in sorted(ssr["dots_detail"].items(), key=lambda kv_: -kv_[1])[:3]
            if v > 0
        )
        if det:
            bits.append(f"DoT：{det}")
        sing.append("· 触发　" + "｜".join(bits))
    lines.append("◆ 单发")
    lines += sing

    # —— 专项：重击 / 对超宏 / 适应 ——
    spec_rows: list[str] = []
    hv = res.get("heavy")
    if hv:
        spec_rows.append(
            f"· 重击　连击 {hv['combo_hits']} → ×{hv['multiplier']:.1f}"
            f"｜单发 {hv['health']:.0f}｜含暴击 {hv['health_crit']:.0f}"
            f"｜爆头 {hv['head']:.0f}" + "（重击会清空连击数）"
        )
        if hv.get("initial_combo"):
            spec_rows.append(
                f"　初始连击 {hv['initial_combo']} 是下限（清空后按每秒 40 回到该值），"
                "不与连击数相加"
            )
    og = res.get("overguard")
    if og:
        line = (
            f"· 对超宏　{og['damage']:.0f}（含暴击 {og['crit']:.0f}）"
            f"｜磁力 ×{og['mag_mult']:.2f}"
            "｜超宏不吃护甲与派系弱点、免疫异常（虚空 +50%）"
        )
        if og.get("pool"):
            line += (
                f"｜超宏池 {og['pool']:.0f} → "
                f"{math.ceil(og['pool'] / og['crit']) if og['crit'] > 0 else '—'} 发打穿"
            )
        spec_rows.append(line)
    ad = res.get("adapt")
    if ad:
        who = "、".join(f"{TYPE_ZH.get(k, k)} −{v * 100:.0f}%" for k, v in ad["resisted"].items())
        spec_rows.append(
            f"· 适应　Sentient {ad['levels']} 层：{who}"
            f" → 有效伤害 ×{ad['factor']:.2f}"
            "（虚空伤害可重置适应）"
        )
    if spec_rows:
        lines.append("◆ 专项判定")
        lines += spec_rows

    # —— 输入回显：武器 / 面板 / 配卡 / 目标 / 弱点 / MOD / 赋能 / 架势 … ——
    inp: list[str] = []
    inp.append(
        f"· 武器　{weapon.get('zh') or weapon['name']}（{weapon['name']}）"
        f"｜MR{weapon.get('masteryReq', 0)}"
    )
    inp.append(
        f"· 面板　基伤 {dt.get('total', 0):g}"
        + (f"（{base_parts}）" if base_parts else "")
        + f"｜暴击 {cc * 100:g}% ×{cm:g}"
        + f"｜射速 {_fr_txt}"
        + (f"（{res['fire_note']}）" if res.get("fire_note") else "")
        + (f"｜多重 {ms:g}" if ms and ms > 1 else "")
    )
    if mod_bits:
        inp.append(f"· 配卡　{'、'.join(mod_bits)}")
    inp.append(
        f"· 目标　{FACTION_ZH.get(res['faction'], res['faction'])}"
        f" {res['level']}级｜护甲 {res['armor']:.0f}"
        + (
            f"（减免 {res.get('dr_raw', res['dr']) * 100:.1f}%）"
            + ("［已达 2700 上限］" if res["armor"] >= 2700 else "")
            if res["armor"] > 0
            else "（无护甲）"
        )
    )
    en = res.get("enemy")
    if en:
        nm = en.get("zh") or en.get("name")
        inp.append(
            f"　敌人：{nm}（基准 {en.get('base_level')} 级 / "
            f"血量 {en.get('base_health'):.0f}"
            + (f" / 护盾 {en.get('base_shield'):.0f}" if en.get("base_shield") else "")
            + (f" / 护甲 {en.get('base_armor'):.0f}" if en.get("base_armor") else "")
            + "）"
        )
    if res["fac_weak"] or res["fac_resist"]:
        weak_txt = "、".join(TYPE_ZH.get(k, k) for k in res["fac_weak"]) or "无"
        res_txt = "、".join(TYPE_ZH.get(k, k) for k in res["fac_resist"]) or "无"
        inp.append(f"· 派系弱点　{weak_txt} ×1.5｜抗性：{res_txt} ×0.5")
        if res["fac_weak"]:
            hit = [TYPE_ZH.get(k, k) for k in res["hit_weak"]] or ["（本配卡没用到）"]
            inp.append(f"　本配卡吃到加成的：{'、'.join(hit)}")
    if res.get("arcanes"):
        _cond = [
            a
            for a in res["arcanes"]
            if any(
                k in (a.get("text") or "").lower()
                for k in (
                    "chance",
                    "on kill",
                    "on hit",
                    "on critical",
                    "on status",
                    "on damaged",
                    "on headshot",
                    "while ",
                )
            )
        ]
        _plain = [a for a in res["arcanes"] if a not in _cond]
        inp.append(
            "· 赋能　"
            + "、".join((a.get("zh") or a["name"]) for a in res["arcanes"])
            + ("（条件触发，未计入数值）" if _cond and not _plain else "")
        )
        for _a in res["arcanes"][:2]:
            if _a.get("text"):
                inp.append(f"　{_a.get('zh') or _a['name']}：{_a['text'][:110]}")
    if res.get("mods"):
        # 注释乘区：直接回答「这张卡属于哪个乘区、和谁相加」；
        # 认得出但算不动的卡（射速/装填/弹匣…）标明「未计入」，不装作算过
        tag = []
        for m in res["mods"]:
            eff = m.get("effects") or {}
            name = f"{m.get('zh') or m.get('name')}"
            # 架势没有数值效果（数据在 wiki 侧），但有独立的「架势」行，先认出来
            if (m.get("compat") or "") == "Stance":
                tag.append(f"{name}（架势）")
                continue
            if not m.get("numeric") or not eff:
                tag.append(f"{name}（未计入）")
                continue
            where = []
            if eff.get("base_dmg"):
                where.append("基伤·加算")
            if eff.get("multishot"):
                where.append("多重·加算")
            if eff.get("crit_chance"):
                where.append("暴率·加算")
            if eff.get("crit_dmg"):
                where.append("暴伤·加算")
            if eff.get("elements"):
                where.append("元素·先相加后合成")
            if eff.get("physical"):
                where.append("物理·仅同类型")
            if eff.get("headshot_bonus"):
                where.append("爆头倍率")
            if eff.get("faction_mul") or eff.get("faction_dmg"):
                where.append("派系·独立乘区")
            if eff.get("dmg_per_status"):
                where.append("基伤组·按异常种类数")
            if eff.get("crit_per_combo"):
                where.append("暴率·随连击倍率")
            if eff.get("status_per_combo"):
                where.append("触发率·随连击倍率")
            if eff.get("heavy_dmg"):
                where.append("基伤组·仅重击")
            if eff.get("initial_combo"):
                where.append("初始连击·下限")
            if eff.get("physical_convert"):
                where.append("物理转换·总量不变")
            if eff.get("conditional"):
                where.append("含条件触发(未计入)")
            tag.append(f"{name}（{'、'.join(where) or '未计入'}）")
        inp.append("· MOD　" + "｜".join(tag))
    _stc = res.get("stance")
    if _stc:
        _p = "、".join(TYPE_ZH.get(k, k) for k in _stc["procs"])
        inp.append(
            f"· 架势　{_stc['zh'] or _stc['name']}（{_stc['combo']}）"
            f" 每段 ×{_stc['mult']:.2f}" + (f"｜强制异常 {_p}" if _p else "｜无强制异常")
        )
    if res.get("status_types"):
        inp.append(f"· 异况超量类　按目标身上 {res['status_types']} 种异常计（用「异常N」改）")
    _cond = res.get("conditional") or []
    for _c in _cond[:3]:
        inp.append(f"· 条件触发　未计入：{_c}".replace("\\n", " "))
    if len(_cond) > 3:
        inp.append(f"· 条件触发　…另有 {len(_cond) - 3} 条条件触发未计入")
    _galv = res.get("galv_applied") or []
    if _galv:
        inp.append(
            "· 镀层　堆叠已计入：" + "｜".join(_galv[:4]) + ("｜…" if len(_galv) > 4 else "")
        )
    if res["strip_note"]:
        inp.append(
            f"· 剥甲　{'、'.join(res['strip_note'])}"
            f" → 护甲 {res['armor']:.0f}→{res['armor_eff']:.0f}"
            f"（减免 {res['dr_raw'] * 100:.1f}%→{res['dr'] * 100:.1f}%）"
        )
    st_bits = []
    if res["viral"]:
        st_bits.append(f"病毒{res['viral']}层（对血 ×{res['viral_mult']:.2f}）")
    if res["magnetic"]:
        st_bits.append(f"磁力{res['magnetic']}层（对盾 ×{res['mag_mult']:.2f}）")
    if st_bits:
        inp.append(f"· 状态加伤　{'、'.join(st_bits)}")
    # 面板口径：顶层伤害取自 attacks 里哪一段（弓箭=蓄力、爆炸=含范围段…）
    # —— 不同计算器对「基础伤害」的口径常不同（wfsim 面板对弓显示未蓄力、
    # 对 lich 默认套 +60% 回响、爆炸只显示直击段），显式标注免得被当算错。
    _ats = pw.get("attacks") or []
    _basis_name = None  # 找到「本表主段」时并进「分段」区块标题
    _basis_row = ""
    # 识卡从面板反推的基础（loadout_ocr 打 _panel_derived）就是面板那一种攻击，
    # 与库内 attacks 的多段口径无关 —— 不标，免得把 96 说成「含多段合并」
    if len(_ats) > 1 and not pw.get("_panel_derived"):
        _main_d = {
            k: float(v)
            for k, v in (pw.get("damage") or {}).items()
            if isinstance(v, (int, float)) and v > 0 and k != "total"
        }
        _main_sig = tuple(sorted((k, round(v, 3)) for k, v in _main_d.items()))
        # 段伤害同样只取 > 0 的分量（Simulor 系 Orb Launch = 冲击 0 ⇒ 签名为空，与直击 0 的主段对上）
        _cands = [
            a
            for a in _ats
            if tuple(
                sorted(
                    (k, round(float(v), 3))
                    for k, v in (a.get("damage") or {}).items()
                    if isinstance(v, (int, float)) and v > 0 and k != "total"
                )
            )
            == _main_sig
        ]
        # 同伤害的几段（Kohm 单弹片 / 满转）按触发几率认主段（b6 批：主段 = 满转 6.25%）
        # 同伤害同触发的蓄力 / 未蓄力两段（Staticor 44 辐射）按是否蓄力认（b8 批：默认满蓄力，
        # 修前取第一段写成「Uncharged Projectile」）
        _charged = float(pw.get("chargeTime") or 0.0) > 0
        _cands = [a for a in _cands if bool(a.get("charge_time")) == _charged] or _cands
        _pc = float(pw.get("procChance") or 0.0)
        _basis = next(
            (
                a.get("name")
                for a in _cands
                if abs(float(a.get("status_chance") or -100) / 100.0 - _pc) < 0.002
            ),
            _cands[0].get("name") if _cands else None,
        )
        _all_names = [a.get("name") for a in _ats if a.get("name")][:4]
        if _basis:
            _basis_name = SEG_ZH.get(_basis, _basis)
            _basis_row = (
                f"· 面板口径　本表主段 = {_basis_name}"
                f"（该武器另有段：{'、'.join(SEG_ZH.get(n, n) for n in _all_names if n != _basis)}）"
            )
        else:
            # 顶层伤害不等于任何单段 → 它把多段合并了（爆炸/蓄力武器常见，
            # warframe-items 的顶层 damage 就是这么给的）。这是数据源口径，
            # 逐把核对 wiki 前先如实标注，避免被当成算错。
            _basis_row = (
                "· 面板口径　本表基础伤害含多段合并（"
                + "、".join(SEG_ZH.get(n, n) for n in _all_names)
                + "）——下限请以「含段合计」行拆分为准"
            )
    modes = res.get("modes") or []
    if _basis_row and not (_basis_name and modes):
        inp.append(_basis_row)  # 有分段表且认出主段时，并进分段区块标题
    if alts:
        inp.append("· 同类候选　" + "、".join(v.get("zh") or v.get("name", "") for v in alts))
    for n in res.get("notes") or []:
        inp.append(f"· MOD 说明　{_note_zh(n)}")
    if res["steel_path"]:
        inp.append("· 钢路　敌人血量/护盾 +100%（护甲已不加成），本卡未乘算")
    lines.append("◆ 输入")
    lines += inp

    # —— 分段表：多段判定（投掷 / 投掷爆炸 / 充能投掷 / 震地…各段类型构成常常完全不同）——
    if modes:
        lines.append(
            f"◆ 分段（{len(modes)} 段"
            + (f"，主段 {_basis_name}" if _basis_name else "")
            + "，含元素/派系/护甲）"
        )
        recs = []
        for m in modes[:8]:
            _top = max((m["damage"] or {"?": 0}).items(), key=lambda kv_: kv_[1])
            _tag = "同普通" if m.get("same_as_main") else TYPE_ZH.get(_top[0], _top[0])
            _fo = m.get("falloff")
            # 兜底：段结构来自多份数据源，裸数字也按衰减比例读（不许因形状不同崩卡）
            _rv = _fo.get("reduction") if isinstance(_fo, dict) else _fo
            _redu = float(_rv) if isinstance(_rv, (int, float)) else 0.0
            recs.append(
                (
                    f"{m['name']}（{_tag}{m['total']:g}）",
                    f"{m['health']:.0f}",
                    f"{m['health_crit']:.0f}",
                    f"{_redu * 100:.0f}%" if _redu > 0 else "",
                    "持续伤害单跳值，未计入 DPS" if m.get("shot_type") == "DoT" else "",
                )
            )
        _has_fo = any(r[3] for r in recs)
        _has_dot = any(r[4] for r in recs)
        lines.append(
            "　段　单发　含暴击"
            + ("　射程衰减" if _has_fo else "")
            + ("　备注" if _has_dot else "")
        )
        for r in recs:
            cells = [r[0], r[1], r[2]]
            if _has_fo:
                cells.append(r[3])
            if _has_dot:
                cells.append(r[4])
            lines.append("· " + "　".join(cells).rstrip("　"))
        if len(modes) > 8:
            lines.append(f"· 另 {len(modes) - 8} 段　次级开火/灵化等未列出")

    # —— 警示 / 注脚（≤3 行）——
    if spec.get("enemy_miss"):
        lines.append(
            "※ "
            + enemy_miss_line(
                spec["enemy_miss"], res.get("faction") or spec.get("faction"), res.get("enemy")
            )
        )
    if res.get("unknown"):
        lines.append(
            "※ ⚠ 未识别（已忽略）："
            + "、".join(res["unknown"])
            + "；该词条没进 MOD 名表，如有误请换官方名再试"
        )
    lines.append(
        "※ 实战 DPS 为稳态近似：异常层已建立、持续挨打；未计爬升/掉层/击杀截断与 Boss 衰减；"
        "火焰按单例累加、每次触发刷新计时（wfsim ignite.yaml），取「开火 T 秒」窗口平均"
        f"（默认 {FIRE_SECONDS_DEFAULT:g}s，可写「开火N秒」）"
    )
    # #6 口径（2026-10-09，不改算法）：游戏与 wfsim 按 MOD 槽位顺序配对、武器自带元素排
    # 最后参与合成（wfsim rules/elements.rs:119-153）；本计算器固定顺序且自带元素不合成
    lines.append(
        "※ 护甲减伤 = 90%×√(护甲/2700)；元素合成口径：按 火>冰>电>毒 两两配对（游戏内按 MOD 槽位顺序配对、"
        "自带元素也参与合成，多元素配卡以游戏面板为准）；U36 起元素差异只有派系弱点 ±50% 与异常状态"
    )
    lines.append(
        "※ 乘区：基伤/多重/暴率/暴伤组内相加，元素先相加后合成，派系独立乘区；"
        "暴击按期望 1+暴率×(暴伤−1)；公式来源 wiki，2026-09-16 核对"
    )
    return lines


def card_footer(weapon: Optional[dict], res: dict) -> str:
    """伤害卡页脚：武器与敌人回显（卡面输入区之外再给一眼能确认「算的是谁」）。

    形如「绝路（Rubico） 对 重型机枪手 · Grineer 100级」；出错 / 无武器返回空串。
    """
    if not weapon or not res or res.get("ok") is False:
        return ""
    zh, en_name = weapon.get("zh") or "", weapon.get("name") or ""
    wtxt = f"{zh}（{en_name}）" if zh and en_name else (zh or en_name)
    fac = re.sub(
        r"（.*?）", "", FACTION_ZH.get(res.get("faction"), res.get("faction") or "")
    ).strip()
    en = res.get("enemy")
    who = (en.get("zh") or en.get("name")) if en else ""
    tgt = f"{fac} {res.get('level', '')}级".strip()
    return f"{wtxt} 对 {who} · {tgt}" if who else f"{wtxt} 对 {tgt}"
