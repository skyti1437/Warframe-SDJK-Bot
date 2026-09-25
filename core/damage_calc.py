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
* 爆头：非暴 ×2、暴击 ×(2·CM)，即爆头期望 = 2 × 身体期望。

**v1 未计入（输出会注明）**：异常状态流程（病毒加伤/腐蚀剥甲/切割 DoT）、
重击/连击、裂隙简化、Overguard、敌人伤害衰减（Sentient 类）。

**v1.1 补上的东西（2026-09-16，用户反馈「不同元素算出来一个数」）**
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

import difflib
import json
import math
import re
from pathlib import Path
from typing import Optional

try:
    from . import matching        # core 包内正常导入
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
        _cache["weapons"] = json.loads(
            (_DATA / "weapons_stats.json").read_text(encoding="utf-8"))
        fac = json.loads(
            (_DATA / "damage_faction.json").read_text(encoding="utf-8"))
        # 表键统一小写：武器数据里的伤害类型键也是小写（impact/slash/…）
        _cache["fac_table"] = {k.lower(): v
                               for k, v in fac["table"].items()
                               if isinstance(v, dict)}
        try:
            _cache["enemies"] = json.loads(
                (_DATA / "enemies.json").read_text(encoding="utf-8"))["enemies"]
        except Exception:  # noqa: BLE001
            _cache["enemies"] = {}
        try:
            raw = json.loads(
                (_DATA / "mods_stats.json").read_text(encoding="utf-8"))
            # 名字索引（可识别但未必能算）先铺底，能算的再覆盖上去：
            # 「锁定目标」这类暂不参与计算的卡也必须认得出来，
            # 否则会被当成武器名的一部分，导致武器查不到、整条指令无输出。
            mods = {k: {**v, "numeric": False} for k, v in
                    (raw.get("names") or {}).items()}
            mods.update({k: {**v, "numeric": True} for k, v in
                         (raw.get("mods") or {}).items()})
            _cache["mods"] = mods
        except Exception:  # noqa: BLE001
            _cache["mods"] = {}
        try:
            # wfsim 补充元数据：家族互斥 + 结构化条件堆叠（scripts/
            # enrich_mods_from_wfsim.py 生成；key=mods_stats 的 key 小写）
            _cache["mods_extra"] = json.loads(
                (_DATA / "mods_wfsim_extra.json").read_text(encoding="utf-8"))
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
HEALTH_SCALE = {   # (系数1, 指数1, 系数2, 指数2)
    "Grineer": (0.015, 2.12, 24 * 5 ** 0.5 / 5, 0.72),
    "Corpus": (0.015, 2.12, 30 * 5 ** 0.5 / 5, 0.55),
    "Infested": (0.0225, 2.12, 36 * 5 ** 0.5 / 5, 0.72),
    "Orokin": (0.015, 2.10, 24 * 5 ** 0.5 / 5, 0.685),
    "_default": (0.015, 2.00, 24 * 5 ** 0.5 / 5, 0.5),
}
SHIELD_SCALE = {
    "Corpus": (0.02, 1.76, 2.0, 0.76),
    "Orokin": (0.02, 1.75, 2.0, 0.75),
    "Grineer": (0.02, 1.75, 1.6, 0.75),
    "_default": (0.02, 1.75, 2.0, 0.75),
}


def _scale_two(x: float, d: float, c1: float, e1: float,
               c2: float, e2: float) -> float:
    f1 = 1 + c1 * d ** e1
    if d <= 70:
        return f1
    f2 = 1 + c2 * d ** e2
    if d >= 80:
        return f2
    t = (d - 70) / 10.0
    s = 3 * t * t - 2 * t ** 3
    return f1 * (1 - s) + f2 * s


def health_multiplier(faction: str, level: int, base_level: int = 1) -> float:
    c = HEALTH_SCALE.get(faction, HEALTH_SCALE["_default"])
    return _scale_two(level, max(0, level - base_level), *c)


def shield_multiplier(faction: str, level: int, base_level: int = 1) -> float:
    c = SHIELD_SCALE.get(faction, SHIELD_SCALE["_default"])
    return _scale_two(level, max(0, level - base_level), *c)


def _best_by_name(query: str, items: list[dict],
                  fuzzy: bool = True) -> Optional[dict]:
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
    subs = [r for r in cand
            if q in _norm(r.get("zh")) or q in _norm(r.get("name"))]
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
    """敌人在**归一化后完全相等**时命中（英文名或中文名）。"""
    q = _norm(query)
    if not q:
        return None
    for e in _load()["enemies"].values():
        if _norm(e.get("zh")) == q or _norm(e.get("name")) == q:
            return e
    return None


def find_enemy(query: str) -> Optional[dict]:
    """按中文名/英文名找敌人（数据来自极镜的 codex/enemy.ts 基准数值表，支持模糊）。"""
    return find_enemy_exact(query) or _best_by_name(
        query, list(_load()["enemies"].values()))


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
    hits = [r for r in pool.values()
            if q in _norm(r.get("zh")) or q in _norm(r.get("name"))]
    if len(hits) == 1:
        return hits[0]
    near = difflib.get_close_matches(
        q, [_norm(r.get("zh")) for r in pool.values() if r.get("zh")], n=1,
        cutoff=0.72)
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
    hits = [m for m in _load()["mods"].values()
            if q in _norm(m.get("zh")) or q in _norm(m.get("name"))]
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
TYPE_ZH = {"impact": "冲击", "puncture": "穿刺", "slash": "切割",
           "cold": "冰冻", "electricity": "电击", "heat": "火焰",
           "toxin": "毒素", "blast": "爆炸", "corrosive": "腐蚀",
           "gas": "毒气", "magnetic": "磁力", "radiation": "辐射",
           "viral": "病毒", "void": "虚空", "tau": "Tau", "true": "真实"}
_SKIP_TYPES = {"cinematic", "shielddrain", "healthdrain", "energydrain"}

ELEM_ALIASES = {"电": "electricity", "电击": "electricity",
                "火": "heat", "火焰": "heat",
                "冰": "cold", "冰冻": "cold",
                "毒": "toxin", "毒素": "toxin"}
# 物理三系（长名在前，正则用它们做交替匹配）
PHYS_ALIASES = {"冲击": "impact", "穿刺": "puncture", "穿": "puncture",
                "切割": "slash", "切": "slash"}

# 复合元素合成表（两两）
COMPOSITE = {frozenset(("heat", "cold")): "blast",
             frozenset(("heat", "electricity")): "radiation",
             frozenset(("heat", "toxin")): "gas",
             frozenset(("cold", "electricity")): "magnetic",
             frozenset(("cold", "toxin")): "viral",
             frozenset(("electricity", "toxin")): "corrosive"}
# MOD 元素合成顺序：火 > 冰 > 电 > 毒
HCET = ["heat", "cold", "electricity", "toxin"]

FACTION_ALIASES = {
    "grineer": "Grineer", "g系": "Grineer", "g": "Grineer",
    "corpus": "Corpus", "c系": "Corpus", "c": "Corpus",
    "amalgam": "Corpus Amalgam", "奥布": "Corpus Amalgam", "合体": "Corpus Amalgam",
    "infested": "Infested", "i系": "Infested", "i": "Infested", "异融": "Infested",
    "deimos": "Infested Deimos", "魔胎": "Infested Deimos",
    "orokin": "Orokin", "奥罗金": "Orokin", "虚空": "Orokin",
    "sentient": "Sentient", "sentients": "Sentient", "感灵": "Sentient",
    "narmer": "Narmer", "合一众": "Narmer",
    "murmur": "The Murmur", "低语者": "The Murmur", "低语": "The Murmur",
    "zariman": "Zariman", "扎里曼": "Zariman",
    "scaldra": "Scaldra", "炽蛇军": "Scaldra",
    "techrot": "Techrot", "科腐者": "Techrot",
    "anarch": "Anarchs", "anarchs": "Anarchs",
}
FACTION_ZH = {"Grineer": "Grineer（G系）", "Kuva Grineer": "赤毒 Grineer",
              "Corpus": "Corpus（C系）", "Corpus Amalgam": "Corpus 合体体",
              "Infested": "Infested（I系）", "Infested Deimos": "魔胎之境 Infested",
              "Orokin": "Orokin（奥罗金）", "Sentient": "Sentient",
              "Narmer": "合一众 Narmer", "The Murmur": "低语者 The Murmur",
              "Zariman": "扎里曼", "Scaldra": "炽蛇军 Scaldra（1999）",
              "Techrot": "科腐者 Techrot（1999）",
              "Anarchs": "Anarchs（The Old Peace 新派系：冲击/电 +50%，抗辐射 −50%）"}
# 各派系默认基准甲（中型单位量级；可用「基甲N」覆盖）
DEFAULT_BASE_ARMOR = {"Grineer": 150, "Kuva Grineer": 150, "Narmer": 150}

# DoT（持续伤害）系数：每秒伤害 = 系数 × MOD 后的基础伤害（wiki「Status Effect」）
# ⚠️ 这里只放**每秒型** DoT；爆炸（blast）是「1.5s 后一次性 30%」，不进这张表，
#    只在异常稳态模型（STATUS_TABLE 里带 instant 标记）里结算
DOT_RATIO = {"slash": 0.35, "heat": 0.5, "electricity": 0.5,
             "toxin": 0.5, "gas": 0.5}
DOT_BYPASS_ARMOR = {"slash"}      # 切割流血无视护甲
DOT_BYPASS_SHIELD = {"toxin"}     # 毒素无视护盾
DOT_SECONDS = 6                   # 表中均为「每秒 × 6 秒」
_PHYS_TYPES = ("impact", "puncture", "slash")   # 物理三系（转换类 MOD 只在它们之间搬）
HEADSHOT_BASE = 2.0               # 常规爆头倍率（个别武器/瞄准镜另有加成，未建模）
# 异常状态数值（层数上限 10）
VIRAL_BASE, VIRAL_STEP = 1.0, 0.25       # 病毒：对血 +100% / 每层 +25%
MAGNETIC_BASE, MAGNETIC_STEP = 1.0, 0.25  # 磁力：对盾/超宏 同上
CORROSIVE_BASE, CORROSIVE_STEP = 0.26, 0.06  # 腐蚀：剥甲 26% / 每层 +6%（满 −80%）
CORROSIVE_MAX = 0.80
HEAT_STRIP = 0.50                          # 火剥甲：−50% 护甲


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
            for k in (_norm(v.get("zh")), _norm(v.get("name")),
                      _norm((v.get("uniqueName") or "").rsplit("/", 1)[-1])):
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
    return re.sub(r"[\s·・•\-_'’]+", "", (s or "")).lower()


def find_weapon(query: str) -> tuple[dict | None, list[dict]]:
    """按中文名/英文名找武器；返回 (精确或最优匹配, 同名候选)。"""
    weapons = _load()["weapons"]
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
    sub = [v for v in weapons.values()
           if q in _norm(v.get("zh")) or q in _norm(v.get("name"))]
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
    # 渲染预热已移除（2026-09-17）：字体+暗角缓存只值 ~0.5 s，而启动期
    # 容器 CPU 被占满时这次预热要 3~10 s，日志里还会显示成「预热 50 s」
    # 误导排查。首张卡多花 0.5 s 完全可接受，不值得。


# ---------------------------------------------------------------------------
# 公式
# ---------------------------------------------------------------------------
def armor_multiplier(level: int, base_level: int = 1) -> float:
    """敌人护甲随等级的乘数（U36 后公式，70-80 之间 smoothstep 过渡）。"""
    d = max(0, level - base_level)
    f1 = 1 + 0.005 * d ** 1.75
    if d <= 70:
        return f1
    f2 = 1 + 0.4 * d ** 0.75
    if d >= 80:
        return f2
    t = (d - 70) / 10.0
    s = 3 * t * t - 2 * t ** 3
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


def compose_elements(singles: dict[str, float]) -> dict[str, float]:
    """按 HCET 顺序把单元素 MOD 两两合成复合元素；合成后加成取两者之和。"""
    pools: dict[str, float] = {}
    remaining = sorted((e for e in singles if singles[e] > 0),
                       key=lambda e: HCET.index(e) if e in HCET else 99)
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
_DISPATCH_LEVELS = ("weapon_exact", "mod_exact", "enemy_exact",
                    "mod_sub", "enemy_fuzzy", "weapon_fuzzy")
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
        for j in range(len(tokens), i, -1):
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
    spec: dict = {"level": 100, "faction": "Grineer", "headshot": False,
                  "steel_path": False, "base_armor": None,
                  "base_dmg": 0.0, "multishot": 0.0, "crit_chance": 0.0,
                  "crit_dmg": 0.0, "faction_dmg": 0.0, "singles": {},
                  "physical": {}, "physical_convert": {},
                  "headshot_bonus": 0.0, "fire_rate_pct": 0.0,
                  "status_dmg": 0.0, "status_chance": 0.0,
                  # v1.9：随连击/随异常层数（狂怒 / 创口溃烂 / 异况超量）
                  "crit_per_combo": 0.0, "status_per_combo": 0.0,
                  "dmg_per_status": 0.0, "status_types": None,
                  # 架势（近战专用槽位；决定每段倍率与强制异常）
                  "stance": None,
                  # 条件触发（On Kill / On Headshot …）：只展示，不折进数值
                  "conditional": [],
                  # 镀层堆叠（wfsim 结构化条件）：None=未指定，-1=满层，N=N 层
                  "galv_stacks": None, "galv_applied": [],
                  "combo_hits": 0, "heavy": False, "overguard": None,
                  "adapt": None, "_armor": 0.0,
                  # 近战专用：重击伤害 MOD（一击必杀）、初始连击（邪恶蓄力）、穿透
                  "heavy_dmg": 0.0, "initial_combo": 0.0, "punch_through": 0.0,
                  # 斩铁类「重击时 x2」：重击额外获得的暴击几率加成
                  "crit_chance_heavy": 0.0,
                  # 奋力一掷类：连续投掷每层 +X% 投掷伤害，最多 throw_max_stacks 层
                  "throw_dmg": 0.0, "throw_stacks": 3, "throw_max_stacks": 0,
                  # 赋能（武器类）：多数是条件触发，认得出名字与文本就展示；
                  # 只有能被解析成无条件数值的部分才计入（见 parse_args 的折算）
                  "arcanes": [], "arcane_texts": [],
                  # 灵化进化选项（进化 基伤/暴击/爆头…）与 lich 回响加成
                  "evo_keys": [],
                  "lich_bonus": None,
                  # 形态：None=自动（有灵化形态数据就开）/ incarnon / base / keep
                  "form": None,
                  # 部署（Archgun）：None=默认（地面/大气）/ archwing / atmosphere
                  "deployment": None,
                  "no_fire_rate_mod": False, "notes": [], "unknown": [],
                  "viral": 0, "magnetic": 0, "corrosive": 0, "heat_strip": False,
                  "enemy": None, "mods": [], "errors": []}
    name_tokens: list[str] = []
    enemy_hint_pos = None    # 「对」之后第一个名字 token 的下标（该词按敌人解析）
    tks = [t for t in tokens if t.strip()]
    i = 0
    while i < len(tks):
        t = tks[i]
        low = t.lower()
        nxt = tks[i + 1] if i + 1 < len(tks) else ""
        num_next = re.fullmatch(_NUM + r"%?", nxt)
        m = re.fullmatch(r"(\d{1,4})级", low) or re.fullmatch(r"lv(\d{1,4})", low)
        if low == "对":          # 「对 重机枪手」的介词：丢掉，但记下「下一个词是敌人」
            enemy_hint_pos = len(name_tokens)
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
        elif low in ("空战", "空战模式"):
            spec["deployment"] = "archwing"
        elif low in ("地面", "大气", "地面模式"):
            spec["deployment"] = "atmosphere"
        elif low == "进化" and tokens[i + 1:i + 2]:
            # 进化 <关键词>：灵化进化选项（下一 token），可多次出现
            spec["evo_keys"].append(str(tokens[i + 1]))
            i += 1
        elif re.fullmatch(r"进化(基伤|伤害|暴击|暴伤|触发|多重|爆头|射速|装填|连击|穿透|弹匣)", low):
            spec["evo_keys"].append(re.fullmatch(
                r"进化(基伤|伤害|暴击|暴伤|触发|多重|爆头|射速|装填|连击|穿透|弹匣)",
                low).group(1))
        elif re.fullmatch(r"(赤毒|信条|终幕|回响)(辐射|火|冰|电|毒|磁力|病毒|爆炸|冲击|穿刺|切割)?"
                          + _NUM + r"%?", low):
            m_l = re.fullmatch(r"(赤毒|信条|终幕|回响)(辐射|火|冰|电|毒|磁力|病毒|爆炸|冲击|穿刺|切割)?"
                               + _NUM + r"%?", low)
            el = ELEM_ALIASES.get(m_l.group(2)) if m_l.group(2) else None
            spec["lich_bonus"] = {"element": el,
                                  "pct": float(m_l.group(3))}
        elif re.fullmatch(r"加成" + _NUM + r"%?", low):
            spec["lich_bonus"] = {"element": None,
                                  "pct": float(re.search(_NUM, low).group(1))}
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
        elif re.fullmatch(r"(异常|异常种类|异常数)" + _NUM + r"?", low):
            # 异况超量 / 镀层战术 按「目标身上异常种类数」加成
            spec["status_types"] = int(float(re.search(_NUM, low).group(1)))
        elif re.fullmatch(r"满镀层|镀层满", low):
            # 镀层堆叠：每张已装的镀层类卡按各自的满层数计入
            spec["galv_stacks"] = -1
        elif re.fullmatch(r"镀层" + _NUM + r"?", low):
            spec["galv_stacks"] = int(float(re.search(_NUM, low).group(1)))
        elif re.fullmatch(r"连击" + _NUM + r"?", low):
            spec["combo_hits"] = int(float(re.search(_NUM, low).group(1)))
        elif low in ("重击", "重击攻击"):
            spec["heavy"] = True
        elif low == "赋能" and nxt:
            # 赋能名（中文名形如「近战·狂怒」，英文名可能带空格 → 吃两词窗口）
            rec = find_arcane(nxt) or (find_arcane(" ".join(tks[i + 1:i + 3]))
                                       if i + 2 < len(tks) else None)
            if rec:
                spec["arcanes"].append(rec)
                if rec.get("text"):
                    spec["arcane_texts"].append(
                        f"{rec.get('zh') or rec['name']}：{rec['text']}")
                i += 2 if find_arcane(nxt) else 3
                continue
            spec["notes"].append(f"未找到赋能「{nxt}」")
            i += 1
            continue
        elif re.fullmatch(r"(连投|投掷层数)" + _NUM + r"?", low):
            spec["throw_stacks"] = int(float(re.search(_NUM, low).group(1)))
        elif low in ("投掷", "投掷伤害"):
            spec["throw_stacks"] = 3          # 默认按满层算
        elif re.fullmatch(r"超宏" + _NUM + r"?", low):
            spec["overguard"] = float(re.search(_NUM, low).group(1)) if re.search(_NUM, low) else True
        elif low == "超宏":
            spec["overguard"] = True
        elif re.fullmatch(r"适应" + _NUM + r"?", low):
            spec["adapt"] = int(float(re.search(_NUM, low).group(1))) if re.search(_NUM, low) else 1
        elif low == "适应":
            spec["adapt"] = 1
        elif low in ("基伤", "多重", "暴率", "暴伤", "派系", "基甲",
                     "爆头倍率", "爆头加成", "状态伤害") and num_next:
            key = {"基伤": "base_dmg", "多重": "multishot", "暴率": "crit_chance",
                   "暴伤": "crit_dmg", "派系": "faction_dmg",
                   "基甲": "base_armor", "爆头倍率": "headshot_bonus",
                   "爆头加成": "headshot_bonus",
                   "状态伤害": "status_dmg"}[low]
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
            cand = " ".join(name_tokens[enemy_hint_pos:enemy_hint_pos + size])
            e = find_enemy_exact(cand)
            if not e and size == 1:
                e = find_enemy(cand)
            if e:
                spec["enemy"] = e
                del name_tokens[enemy_hint_pos:enemy_hint_pos + size]
                break

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

    # MOD 效果折算进 spec（与手写百分比同源：同组相加、派系 MOD 走独立乘区）
    for m in spec["mods"]:
        eff = m.get("effects") or {}
        spec["base_dmg"] += eff.get("base_dmg", 0.0)
        spec["multishot"] += eff.get("multishot", 0.0)
        spec["crit_chance"] += eff.get("crit_chance", 0.0)
        spec["crit_dmg"] += eff.get("crit_dmg", 0.0)
        spec["headshot_bonus"] += eff.get("headshot_bonus", 0.0)
        spec["fire_rate_pct"] += eff.get("fire_rate", 0.0)
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
            spec["physical_convert"][el] = \
                spec["physical_convert"].get(el, 0.0) + pct
        # 条件触发：只记录，绝不折进数值
        for c in eff.get("conditional") or []:
            spec["conditional"].append(
                f"{m.get('zh') or m.get('name')}："
                + str(c).replace("\\n", " ").replace("\n", " "))
        # 架势：单独存，不参与上面的数值折算
        if (m.get("compat") or "") == "Stance":
            spec["stance"] = m
        # 奋力一掷：连续投掷的投掷伤害加成（每层 X%，最多 N 层）
        spec["throw_dmg"] += eff.get("throw_dmg", 0.0)
        if eff.get("throw_max_stacks"):
            spec["throw_max_stacks"] = max(int(spec["throw_max_stacks"]),
                                           int(eff["throw_max_stacks"]))
        # 元素 / 物理 / 派系（这几项原先在循环外，被补丁误并进赋能循环了）
        if eff.get("faction_mul"):            # 派系 MOD 是倍率写法（x1.3）
            spec["faction_dmg"] += (eff["faction_mul"] - 1) * 100
        else:
            spec["faction_dmg"] += eff.get("faction_dmg", 0.0)
        for el, pct in (eff.get("elements") or {}).items():
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
                n = cap if spec["galv_stacks"] == -1 \
                    else max(0, min(int(spec["galv_stacks"]), cap))
                if n <= 0:
                    continue
                add = float(cond.get("value") or 0.0) * n
                spec[cond["field"]] = spec.get(cond["field"], 0.0) + add
                spec["galv_applied"].append(
                    f"{m.get('zh') or m.get('name')} "
                    f"{cond.get('grants')} +{add:.1f}%（{n}/{cap} 层）")

    # 赋能：**只有能被解析成无条件数值的部分才计入**。武器赋能绝大多数是
    # 「On X: Y% chance for +Z」这种条件触发（如 Arcane Fury），把它们当常驻加成
    # 会系统性高估 —— 所以默认只在卡面列出效果原文，条件性的不折进数值。
    for _arc in spec["arcanes"]:
        _text = (_arc.get("text") or "").lower()
        _conditional = any(k in _text for k in
                           ("chance", "on kill", "on hit", "on critical",
                            "on status", "on damaged", "on headshot", "while "))
        if _conditional:
            continue
        _eff = _arc.get("effects") or {}
        for key in ("base_dmg", "multishot", "crit_chance", "crit_dmg",
                    "fire_rate", "status_chance"):
            if _eff.get(key):
                spec[key] += float(_eff[key])
    return spec, weapon_tokens


# ---------------------------------------------------------------------------
# 主计算
# ---------------------------------------------------------------------------
def _hit_from_damage(dmg: dict, total: float, spec: dict, fac_table: dict,
                     faction: str, dr: float, viral_mult: float,
                     mag_mult: float, n_types: int = 0,
                     stance_mult: float = 1.0) -> tuple[dict, dict, dict, float]:
    """给定**一段攻击**的伤害构成，算出 MOD 后的各类型值与对血/对盾明细。

    抽出来是为了让「普通攻击」之外的段（投掷、投掷爆炸、震地、充能投掷…）
    走**完全相同的规则**去算，而不是按普通攻击等比缩放（缩放会把元素合成、
    派系弱点、护甲减免全算错——各段的类型构成常常完全不同）。

    返回 ``(MOD 后各类型, 对血各类型, 对盾各类型, MOD 后基础总伤)``。
    """
    # 异况超量 / 镀层战术：目标身上每种异常 +N%，**进基伤组**
    # （wiki Condition Overload 原式：Total = 基础 ×[1+伤害MOD+(CO×n)]×(1+元素MOD)）
    # n_types（目标身上异常种类数）由 calculate 统一算好传进来：
    # 这里拿不到 weapon，没法判断武器自身有没有触发率
    _n_types = max(0, int(n_types or 0))
    after_base = 1 + (spec["base_dmg"]
                      + spec["dmg_per_status"] * _n_types) / 100.0
    per_type: dict[str, float] = {}
    for k, v in dmg.items():
        # ⚠️ 大小写不敏感：武器数据里是 shieldDrain/healthDrain 这种 camelCase，
        #    而 _SKIP_TYPES 是小写 —— 不加 .lower() 就会把「护盾吸取」当伤害算
        if (k in ("total",) or k.lower() in _SKIP_TYPES
                or not isinstance(v, (int, float)) or v <= 0):
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

    # 架势倍率：近战每一段都按连招倍率放大（默认取站立连招的每段平均）
    if stance_mult and stance_mult != 1.0:
        per_type = {k: v * stance_mult for k, v in per_type.items()}

    fac_mod = 1 + spec["faction_dmg"] / 100.0
    per_type_health: dict[str, float] = {}
    per_type_shield: dict[str, float] = {}
    for k, v in per_type.items():
        vm = fac_table.get(k, {}).get(faction, 1.0)
        d = v * fac_mod * vm
        # 打血：护甲减免后**每个伤害类型至少 1 点**（wiki Armor），再乘病毒
        hp_d = d * (1 - dr)
        if dr > 0:
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
                (_DATA / "weapon_attacks.json").read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            _WEAPON_ATTACKS = {}
    return _WEAPON_ATTACKS


def _load_incarnon_forms() -> dict:
    """灵化形态基础面板（wfsim 抽取，key = 小写 uniqueName）。"""
    global _INC_FORMS
    if _INC_FORMS is None:
        try:
            _INC_FORMS = json.loads(
                (_DATA / "incarnon_forms.json").read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            _INC_FORMS = {}
    return _INC_FORMS


def _apply_incarnon_form(spec: dict, w: dict) -> dict:
    """灵化形态切换：**有灵化形态数据的武器默认开灵化**。

    spec["form"]：None=自动（有数据就开）/ "incarnon"=强制开 /
    "base"=强制基础形态 / "keep"=不动（识卡用：面板已反推过）。
    灵化形态是另一套基础面板（伤害向量/暴击/触发/射速），MOD 照常叠算；
    带范围（radial）段的形态在卡面标注 AoE 值（不并入主段数值）。
    """
    form = spec.get("form")
    if form == "keep":
        return w
    key = str(w.get("uniqueName") or "").lower()
    inc = _load_incarnon_forms().get(key)
    if not inc:
        if form == "incarnon":
            spec["notes"].append("该武器没有灵化形态数据（近战灵化尚未收录），"
                                 "仍按基础形态计算")
        return w
    if form == "base":
        spec["notes"].append("形态：基础形态（可用「灵化」切到灵化形态）")
        return w
    w2 = dict(w)
    dmg = dict(inc.get("damage") or {})
    w2["damage"] = dmg
    for src, dst in (("criticalChance", "criticalChance"),
                     ("criticalMultiplier", "criticalMultiplier"),
                     ("procChance", "procChance"),
                     ("fireRate", "fireRate"), ("multishot", "multishot")):
        if inc.get(src) is not None:
            w2[dst] = inc[src]
    txt = (f"形态：灵化形态（基础 {dmg.get('total', 0):g}）")
    rad = inc.get("radial") or {}
    if rad.get("damage"):
        txt += (f" + 范围段 {rad['damage'].get('total', 0):g}"
                f"（半径 {rad.get('radius_m', 0):g}m，单独结算）")
    if inc.get("forced_procs"):
        txt += "；强制异常：" + "、".join(
            TYPE_ZH.get(x, x) for x in inc["forced_procs"])
    rc = inc.get("ricochet") or {}
    if rc.get("bounces"):
        txt += (f"；弹跳 ×{rc['bounces']}"
                f"（弱点率假设 {rc.get('headshot_chance', 0) * 100:g}%）")
    spec["notes"].append(txt)
    return w2


def _load_evo_data() -> tuple[dict, dict]:
    """懒加载 evolutions.json / lich_valence.json（缺失时给空表）。"""
    global _EVO_DATA, _LICH_DATA
    if _EVO_DATA is None:
        try:
            _EVO_DATA = json.loads(
                (_DATA / "evolutions.json").read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            _EVO_DATA = {}
    if _LICH_DATA is None:
        try:
            _LICH_DATA = json.loads(
                (_DATA / "lich_valence.json").read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            _LICH_DATA = {}
    return _EVO_DATA, _LICH_DATA


# 「进化 <关键词>」→ 要找的效果 kind（按序匹配；一条选项可含多个效果）
_EVO_KEY_KINDS = [
    ("基伤", "flat_base_damage"), ("伤害", "flat_base_damage"),
    ("暴击", "flat_base_crit_chance"), ("暴伤", "flat_base_crit_multiplier"),
    ("触发", "flat_base_status_chance"), ("多重", "flat_base_multishot"),
    ("爆头", "headshot_damage"), ("射速", "fire_rate_bonus"),
    ("装填", "reload_speed_bonus"), ("连击", "initial_combo"),
    ("穿透", "punch_through_bonus"), ("弹匣", "flat_base_magazine"),
]


def _weapon_family_key(weapon: dict) -> str:
    """武器 → wfsim 进化/valence 数据的 key（Latron Prime → latron_prime）。"""
    for src in (weapon.get("name"), weapon.get("zh")):
        if src:
            return re.sub(r"[\s'’]+", "_", str(src).strip().lower())
    return ""


_DEPLOYMENTS: Optional[dict] = None


def _load_deployments() -> dict:
    """Archgun 双部署面板（wfsim deployments，key = 小写 uniqueName）。"""
    global _DEPLOYMENTS
    if _DEPLOYMENTS is None:
        try:
            _DEPLOYMENTS = json.loads(
                (_DATA / "weapon_deployments.json").read_text(encoding="utf-8"))
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
        for src, dst in (("criticalChance", "criticalChance"),
                         ("criticalMultiplier", "criticalMultiplier")):
            if a.get(src) is not None:
                w2[dst] = a[src]
        if a.get("reload_seconds"):
            w2["reloadTime"] = a["reload_seconds"]
        if a.get("ammo_max"):
            w2["magazineSize"] = a["ammo_max"]
        if a.get("radial_damage"):
            atks = list(w2.get("attacks") or [])
            atks.append({"name": "空战范围伤害",
                         "damage": dict(a["radial_damage"]),
                         "total": a["radial_damage"].get("total"),
                         "shot_type": "AoE"})
            w2["attacks"] = atks
        spec["notes"].append(
            "部署：空战（Archwing）面板"
            + ("；另有空战范围伤害段" if a.get("radial_damage") else "")
            + ("；弹匣按空战值" if a.get("ammo_max") else ""))
        return w2
    if mode == "atmosphere":
        spec["notes"].append("部署：地面/大气（Gravimag 默认面板）"
                             "（可用「空战」切到空战面板）")
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
    dmg = dict(w.get("damage") or {})
    evo, lich = _load_evo_data()
    w = _apply_incarnon_form(spec, w)      # ⓿ 形态（灵化默认开）
    w = _apply_deployment(spec, w)         # ⓿b 空战/地面部署（Archgun）
    dmg = dict(w.get("damage") or {})

    total0 = sum(float(v) for k, v in dmg.items()
                 if isinstance(v, (int, float)) and v > 0
                 and k not in ("total", "cinematic", "shieldDrain",
                               "healthDrain", "energyDrain"))

    def scale_damage(add_flat: float, elem: Optional[str] = None) -> None:
        """把 add_flat 加进基础伤害：指定元素直加，否则按原构成比例摊。"""
        nonlocal total0
        if add_flat <= 0 or total0 <= 0:
            return
        if elem:
            dmg[elem] = float(dmg.get(elem) or 0.0) + add_flat
        else:
            for k, v in list(dmg.items()):
                if isinstance(v, (int, float)) and v > 0 \
                        and k not in ("total", "cinematic", "shieldDrain",
                                      "healthDrain", "energyDrain"):
                    dmg[k] = v + add_flat * v / total0
        total0 += add_flat

    # ---- ① 灵化进化选项 ----
    used_ids: set = set()
    spec["evo_headshot"] = 0.0
    for key in spec.get("evo_keys") or []:
        kind = next((k for kw, k in _EVO_KEY_KINDS if kw in key), None)
        if not kind:
            spec["notes"].append(f"未认识的进化关键词「{key}」"
                                 "（可用：基伤/暴击/暴伤/触发/多重/爆头/射速/装填/连击）")
            continue
        fam = evo.get(_weapon_family_key(weapon)) or {}
        tiers = fam.get("tiers") or {}
        picked = None
        for tier in sorted(tiers, key=lambda x: int(x) if x.isdigit() else 99):
            if tier == "1":          # EVO I 是灵化形态解锁，非选项
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
                w["criticalMultiplier"] = float(
                    w.get("criticalMultiplier") or 1.0) + v
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
                spec["initial_combo"] = float(
                    spec.get("initial_combo") or 0.0) + v
                applied.append(f"初始连击+{v:g}")
            elif kind2 == "flat_base_magazine":
                w["magazineSize"] = float(w.get("magazineSize") or 0.0) + v
                applied.append(f"弹匣+{v:g}")
            else:
                shown.append(e)
        line = f"EVO{tier} {opt.get('name')}：{'、'.join(applied)}" if applied \
            else f"EVO{tier} {opt.get('name')}：未计入"
        spec["notes"].append(line + ("｜未计入：" + "；".join(
            f"{e.get('kind')}" for e in shown) if shown else ""))
        spec.setdefault("conditional", []).append(
            f"EVO{tier} {opt.get('name')}（条件部分未计入）") if shown else None

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
                elem = next((e for e in val["elements"] if e != "impact"),
                            val["elements"][0])
                spec["notes"].append(
                    f"回响加成元素未指定，按 {elem} 估算"
                    "（可写 赤毒元素名N 指定）")
            elif elem not in val["elements"]:
                spec["notes"].append(
                    f"回响加成元素 {elem} 不在可滚列表里（"
                    + "、".join(val["elements"]) + "），已按原值应用")
            scale_damage(total0 * lb["pct"] / 100.0, elem=elem)
            spec["notes"].append(
                f"回响加成：{elem} +{lb['pct']:g}%（{val['min']:g}-{val['max']:g}%）")

    if dmg:
        dmg["total"] = sum(float(v) for k, v in dmg.items()
                           if isinstance(v, (int, float)) and v > 0
                           and k not in ("total", "cinematic", "shieldDrain",
                                         "healthDrain", "energyDrain"))
        w["damage"] = dmg
    return w


def calculate(spec: dict, weapon: dict) -> dict:
    """按 v1 公式算对敌伤害；返回 dict（handler 负责拼卡面）。

    ⚠️ 绝不抛异常：武器缺失/数据异常时返回 {"ok": False, "error": ...}，
    由卡面把它显示出来 —— 群聊里「什么都不显示」比「算错」更难排查。
    """
    if not isinstance(weapon, dict) or not isinstance(weapon.get("damage"), dict):
        return {"ok": False, "mods": spec.get("mods") or [],
                "unknown": spec.get("unknown") or [],
                "error": "没拿到这把武器的基础数据（武器名为空或数据缺失）"}
    _weapon_lib = weapon          # 形态/进化改造前的库值（段计算要算基础暴击增量）
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
        base_armor = (enemy.get("base_armor") if enemy
                      else DEFAULT_BASE_ARMOR.get(faction, 0.0)) or 0.0
    armor = current_armor(level, base_armor, base_level) if base_armor > 0 else 0.0

    # 剥甲：腐蚀层数（26% + 6%×每层，封顶 80%）与火剥甲（−50%）先后相乘
    strip = 0.0
    if spec["corrosive"]:
        strip = min(CORROSIVE_BASE + CORROSIVE_STEP * (spec["corrosive"] - 1),
                    CORROSIVE_MAX)
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
    _types = {k.lower() for k in dmg
              if k.lower() in STATUS_TABLE
              and isinstance(dmg.get(k), (int, float)) and dmg.get(k) > 0}
    _can_proc = ((weapon.get("procChance") or 0.0) > 0
                 or spec["status_chance"] > 0
                 or spec["status_per_combo"] > 0
                 or bool(spec["singles"]) or bool(_st_procs))
    _n_types = max(0, int(spec["status_types"] if spec.get("status_types") is not None
                         else (len(_types | set(_st_procs)) if _can_proc else 0)))

    # 1)+2)+3)+4) 见 _hit_from_damage：基础各类型×基伤、物理 MOD 只加同类型、
    #    元素 MOD 按基伤总量加成并合成、派系 MOD×倍率、护甲减免、病毒/磁力
    per_type, per_type_health, per_type_shield, base_total_after = _hit_from_damage(
        dmg, float(dmg.get("total") or 0.0), spec, fac_table, faction,
        dr, viral_mult, mag_mult, n_types=_n_types, stance_mult=_st_mult)
    health = sum(per_type_health.values())
    shield = sum(per_type_shield.values())

    # 暴击与爆头（放在 DoT 之前算：DoT 要继承初始命中的暴击/爆头加成）
    # 连击层数（近战）：狂怒/创口溃烂都按「连击倍率那一列」算
    _is_venka = "venka prime" in (weapon.get("name") or "").lower()
    _combo_tier = combo_multiplier(int(spec["combo_hits"])
                                   + int(spec["initial_combo"]),
                                   venka=_is_venka)[0]

    # ⚠️ 暴击率 MOD 是**相对基础值**的百分比加成，不是加绝对值：
    #    wiki 口径「+150% Critical Chance」让 38% 变成 95%（38×2.5），
    #    不是 188%。写成加法的话，斩铁（+120%）会把 20% 算成 140%，
    #    暴击期望从 ×2.58 虚高到 ×5.98（2026-09-16 配卡截图识别时暴露）。
    #    狂怒（Blood Rush）是额外一项，**按 (连击倍率−1) 缩放**：
    #    wiki 原式 CC = 基础 × [1 + MOD加成 + 狂怒×(连击倍率 − 1)]
    cc_base = weapon.get("criticalChance") or 0.0
    cc = cc_base * (1 + (spec["crit_chance"]
                         + spec["crit_per_combo"] * (_combo_tier - 1.0)) / 100.0)
    cm = (weapon.get("criticalMultiplier") or 1.0) * (1 + spec["crit_dmg"] / 100.0)
    e_body = crit_expectation(cc, cm)
    # 重击的暴击期望：斩铁这类卡的暴击几率加成在重击时翻倍（wiki 26.0.7）
    cc_heavy = cc_base * (1 + (spec["crit_chance"]
                               + spec["crit_chance_heavy"]) / 100.0)
    e_heavy = crit_expectation(cc_heavy, cm)
    # 爆头倍率基数：默认 2×；**指定敌人时用它的 head_mul**（幼体/巨兽类 = ×1，
    # 即不吃爆头加成，这是它们与普通单位的真实差异）。「爆头倍率」类 MOD
    # 只放大**超出 1 的部分**（wiki Target Acquired）
    head_base = HEADSHOT_BASE
    if enemy and enemy.get("head_mul"):
        head_base = float(enemy["head_mul"])
    head_mult = head_base
    if spec["headshot_bonus"]:
        head_mult = 1 + (head_base - 1) * (1 + spec["headshot_bonus"] / 100.0)
    head_mult += float(spec.get("evo_headshot") or 0.0)   # 灵化进化爆头加成
    e_head = head_mult * e_body

    # 5) 异常 DoT（6 秒总量）：每秒 = ratio × MOD 后基础伤害 × 派系MOD²
    #    × 状态伤害加成 × 暴击期望（× 爆头倍率，指定爆头时）
    dots: dict[str, float] = {}
    # wiki《Status Effect → DoT Damage Scaling》：初始命中吃到的加成，DoT 同样享受 ——
    # ① 状态伤害 MOD（Elementalist 类，独立乘区）② 暴击（按期望值）
    # ③ 爆头倍率（指定爆头时才乘）④ 派系 MOD 结算两次（下面 fac_mod²）
    status_mult = 1 + spec["status_dmg"] / 100.0
    for el, ratio in DOT_RATIO.items():
        if per_type.get(el, 0.0) <= 0:
            continue
        vm = fac_table.get(el, {}).get(faction, 1.0)
        # DoT 累加器起点为 1（wfsim 实测 M58：(ΣSᵢ+1)×C×M）——
        # 单次 proc 即 (基础+1)×系数，基础大时可忽略但结构上应有
        tick = ((base_total_after + 1.0) * ratio
                * fac_mod * fac_mod * vm * status_mult * e_body)
        if spec["headshot"]:
            tick *= head_mult
        if el in DOT_BYPASS_ARMOR:
            dmg_tick = tick * viral_mult                    # 流血无视护甲
        elif el in DOT_BYPASS_SHIELD:
            dmg_tick = tick * (1 - dr) * viral_mult          # 毒 DoT 打血
        else:
            dmg_tick = tick * (1 - dr) * viral_mult
        dots[el] = dmg_tick * DOT_SECONDS

    ms = (weapon.get("multishot") or 1) * (1 + spec["multishot"] / 100.0)
    fire_rate = weapon.get("fireRate") or 0.0
    # 持续 DPS = 爆发 DPS × 弹匣时间/(弹匣时间+装填)；近战/无弹匣数据则为 None
    magazine = weapon.get("magazineSize") or 0
    reload_t = weapon.get("reloadTime") or 0.0
    dps_sustained = None
    if magazine > 0 and fire_rate > 0 and reload_t > 0:
        mag_time = magazine / fire_rate
        dps_sustained = (health * ms * fire_rate) * mag_time / (mag_time + reload_t)
    fr_note = ""
    if spec["no_fire_rate_mod"]:
        fr_note = "射速不可修改，增减已忽略"
    elif spec["fire_rate_pct"]:
        # 只按基础射速乘算（不含其它射速 MOD 的叠加细节）
        fire_rate *= max(0.05, 1 + spec["fire_rate_pct"] / 100.0)
        fr_note = f"含射速 {spec['fire_rate_pct']:+g}%"

    # 敌人血量/护盾（指定敌人时）：按派系公式缩放；钢路 ×2（血量与护盾）
    hp = shield_hp = None
    shots = None
    if enemy:
        sp_mult = 2.0 if spec["steel_path"] else 1.0
        hp = (enemy.get("base_health") or 0.0) * \
            health_multiplier(faction, level, base_level) * sp_mult
        shield_hp = (enemy.get("base_shield") or 0.0) * \
            shield_multiplier(faction, level, base_level) * sp_mult
        # 击杀发数：护盾段（不吃护甲减免）+ 血量段（吃减免与病毒），
        # 两段都按**暴击期望**后的单发伤害算（即平均发数）
        avg_health = health * e_body
        avg_shield = shield * e_body
        n_shield = math.ceil(shield_hp / avg_shield) if shield_hp > 0 and avg_shield > 0 else 0
        n_health = math.ceil(hp / avg_health) if hp > 0 and avg_health > 0 else 0
        shots = n_shield + n_health

    # 派系弱点提示：本武器用到的类型里哪些吃了 +50%
    weak = sorted({k for k in per_type
                   if fac_table.get(k, {}).get(faction, 1.0) > 1.0})
    resist = sorted({k for k in per_type
                     if fac_table.get(k, {}).get(faction, 1.0) < 1.0})
    fac_weak = sorted(k for k, v in fac_table.items() if v.get(faction, 1.0) > 1.0)
    fac_resist = sorted(k for k, v in fac_table.items() if v.get(faction, 1.0) < 1.0)

    # ------------------------------------------------------------------
    # v1.6：异常稳态 / 近战重击 / 超宏 / Sentient 适应
    # ------------------------------------------------------------------
    spec["_armor"] = armor                      # 稳态模型要用「未剥甲」的原始护甲
    # wiki Weeping Wounds 原式：SC = 基础 ×(1+MOD) ×(1 + 创口溃烂 × 连击倍率)
    status_chance_total = ((weapon.get("procChance") or 0.0)
                           * (1 + spec["status_chance"] / 100.0)
                           * (1 + spec["status_per_combo"] * _combo_tier / 100.0))
    procs = status_procs_per_sec(fire_rate, ms, status_chance_total)
    ss = steady_state_dps(per_type, base_total_after, fac_mod, fac_table, faction,
                          procs, 1 + spec["status_dmg"] / 100.0, cc, cm, head_mult,
                          ms, fire_rate, spec, strip)
    ss["status_chance"] = status_chance_total

    # 近战重击：wiki《Melee Combo》——重击吃满连击倍率、并消耗连击数
    heavy_info = None
    if spec["heavy"]:
        hv = weapon.get("heavyAttackDamage") or 0.0
        if hv > 0:
            total_base = dmg.get("total") or 1.0
            is_venka = "venka prime" in (weapon.get("name") or "").lower()
            # 初始连击（邪恶蓄力）抬高起手层数；重击伤害 MOD（一击必杀）独立乘区
            hits = int(spec["combo_hits"]) + int(spec["initial_combo"])
            mul_h, _mul_n = combo_multiplier(hits, venka=is_venka)
            scale = hv / total_base * mul_h * (1 + spec["heavy_dmg"] / 100.0)
            heavy_info = {
                "base": hv, "multiplier": mul_h, "combo_hits": hits,
                "dmg_bonus": spec["heavy_dmg"],
                "crit_cc": cc_heavy, "crit_exp": e_heavy,
                "health": health * scale,
                "health_crit": health * scale * e_heavy,
                "head": health * scale * e_heavy * head_mult,
            }
        else:
            spec["notes"].append("该武器没有重击数据（只有近战有）")

    # 多段判定：一把武器常有多段独立伤害（投掷命中 / 投掷爆炸 / 充能投掷 /
    # 震地 / 重击震地 / 灵化形态 / 次级开火…），**各段单独走一遍完整公式**。
    # 卡面只挑「与普通攻击构成不同」的段展示，避免同值噪音。
    mode_segments: list[dict] = []
    _main_sig = tuple(sorted((k, round(float(v), 3)) for k, v in dmg.items()
                             if isinstance(v, (int, float)) and v > 0
                             and k not in ("total",) and k.lower() not in _SKIP_TYPES))
    _inc_this = _load_incarnon_forms().get(
        str(weapon.get("uniqueName") or "").lower())
    for _atk in (weapon.get("attacks") or []):
        _nm = (_atk.get("name") or "").strip()
        _md = {k: float(v) for k, v in (_atk.get("damage") or {}).items()
               if isinstance(v, (int, float)) and v > 0}
        if not _nm or not _md or _nm == "Normal Attack":
            continue
        # 灵化武器的 attacks 里带「Incarnon Form / Incarnon Form AoE」：
        # 形态替换后主段已等于 Incarnon 主段（重复计一次会翻倍）；
        # 「基础形态」参数下则明确不算灵化段。
        if _inc_this and "incarnon" in _nm.lower():
            if spec.get("form") == "base":
                continue
            if "aoe" not in _nm.lower():
                continue
        _mt = float(_atk.get("total") or sum(_md.values()))
        _pt, _pth, _pts, _ = _hit_from_damage(_md, _mt, spec, fac_table, faction,
                                              dr, viral_mult, mag_mult,
                                              n_types=_n_types,
                                              stance_mult=_st_mult)
        # 该段自己的暴击参数（充能投掷常比普通高，如 Xoris 22%/20%）
        _cc_raw = float(_atk.get("crit_chance") or 0.0) / 100.0 or cc_base
        _cm_raw = float(_atk.get("crit_mult") or 0.0) or 1.0
        _mcc = _cc_raw * (1 + spec["crit_chance"] / 100.0)
        _mcm = _cm_raw * (1 + spec["crit_dmg"] / 100.0)
        _me = crit_expectation(_mcc, _mcm)
        _sig = tuple(sorted((k, round(v, 3)) for k, v in _md.items()))
        mode_segments.append({
            "name": _nm, "damage": _md, "total": _mt,
            "health": sum(_pth.values()), "shield": sum(_pts.values()),
            "health_crit": sum(_pth.values()) * _me,
            "per_type": _pth, "crit_cc": _mcc, "crit_exp": _me,
            "shot_type": _atk.get("shot_type"),
            "falloff": _atk.get("falloff"),
            "same_as_main": _sig == _main_sig,
            "per_trigger_health": sum(_pth.values()) * _me * ms,
        })

    # 近战震地：**部分武器**在 attacks 里带 "Slam Attack"（那是最准的），
    # 其余只有顶层 slamAttack/heavySlamAttack 两个裸数值（没有伤害类型信息）。
    # wiki：投掷类（Glaive）的震地范围伤害「只造成电击」，故兜底时按电击算并标注。
    _has_slam = any("slam" in (m.get("name") or "").lower() for m in mode_segments)
    if not _has_slam:
        _slam_type = "electricity" if weapon.get("throwDamage") else "impact"
        for _lbl, _key in (("震地攻击（估算）", "slamAttack"),
                           ("重击震地（估算）", "heavySlamAttack")):
            _sv = float(weapon.get(_key) or 0.0)
            if _sv <= 0:
                continue
            _sd = {_slam_type: _sv}
            _pt, _pth, _pts, _ = _hit_from_damage(_sd, _sv, spec, fac_table,
                                                  faction, dr, viral_mult,
                                                  mag_mult,
                                                  n_types=_n_types,
                                                  stance_mult=_st_mult)
            mode_segments.append({
                "name": _lbl, "damage": _sd, "total": _sv,
                "health": sum(_pth.values()), "shield": sum(_pts.values()),
                "health_crit": sum(_pth.values()) * e_body,
                "per_type": _pth, "crit_cc": cc, "crit_exp": e_body,
                "shot_type": "AoE", "falloff": None, "same_as_main": False,
            })

    # ---- wfsim 结构化额外段（范围/集束）：与 attacks 段同规则并入 ----
    # warframe-items 的 attacks 对爆炸武器常缺 radial 明细，wfsim 的 radial
    # 块（伤害向量/自带宽高暴击/R 半径/衰减/是否吃多重）更完整。按伤害
    # 签名去重，避免与上面 attacks 的段重复。
    _wa = (_load_weapon_attacks().get(
        str(weapon.get("uniqueName") or "").lower()) or {})
    _seen_sig = {tuple(sorted((k, round(float(v), 3)) for k, v in
                              (m.get("damage") or {}).items()))
                 for m in mode_segments}
    _cc_delta = float(weapon.get("criticalChance") or 0.0) - float(
        _weapon_lib.get("criticalChance") or 0.0)
    _cm_delta = float(weapon.get("criticalMultiplier") or 1.0) - float(
        _weapon_lib.get("criticalMultiplier") or 1.0)
    for _seg in _wa.get("segments") or []:
        _sd = {k: float(v) for k, v in (_seg.get("damage") or {}).items()
               if isinstance(v, (int, float)) and v > 0 and k != "total"}
        if not _sd:
            continue
        _sig = tuple(sorted((k, round(v, 3)) for k, v in _sd.items()))
        if _sig in _seen_sig:
            continue
        _seen_sig.add(_sig)
        _sv = float((_seg.get("damage") or {}).get("total")
                    or sum(_sd.values()))
        _pt, _pth, _pts, _ = _hit_from_damage(_sd, _sv, spec, fac_table,
                                              faction, dr, viral_mult,
                                              mag_mult, n_types=_n_types,
                                              stance_mult=_st_mult)
        # 段自带宽高暴击（与主段常不同）；进化/形态引入的基础暴击增量一并计入
        _cc_raw = float(_seg.get("criticalChance") or 0.0) + _cc_delta
        _cm_raw = float(_seg.get("criticalMultiplier") or 1.0) + _cm_delta
        _mcc = _cc_raw * (1 + spec["crit_chance"] / 100.0)
        _mcm = _cm_raw * (1 + spec["crit_dmg"] / 100.0)
        _me = crit_expectation(_mcc, _mcm)
        _takes_ms = bool(_seg.get("takes_multishot", True))
        mode_segments.append({
            "name": _seg.get("name") or "范围伤害",
            "damage": _sd, "total": _sv,
            "health": sum(_pth.values()), "shield": sum(_pts.values()),
            "health_crit": sum(_pth.values()) * _me,
            "per_type": _pth, "crit_cc": _mcc, "crit_exp": _me,
            "shot_type": "AoE", "falloff": _seg.get("falloff_reduction"),
            "same_as_main": False,
            "takes_multishot": _takes_ms,
            "radius_m": _seg.get("radius_m"),
            "per_trigger_health": sum(_pth.values()) * _me
            * (ms if _takes_ms else 1.0),
        })

    # 含段合计：主段每次扳机 + 各段每次扳机（口径=同一敌人/同一 MOD 乘区）
    _seg_sum = sum(float(m.get("per_trigger_health") or 0.0)
                   for m in mode_segments
                   if m.get("per_trigger_health"))
    segments_total = {
        "main": health * ms * e_body,
        "segments": _seg_sum,
        "total": health * ms * e_body + _seg_sum,
        "n_segments": len(mode_segments),
    } if mode_segments else None

    # 投掷（Glaive 类）：奋力一掷「连续投掷时每层 +X% 投掷伤害，最多 N 层」。
    # 投掷基础伤害取武器的 Throw 攻击（数据里单独存着），与普通攻击分开算。
    throw_info = None
    _td = float(spec.get("throw_dmg") or 0.0)
    _tw = weapon.get("throwDamage") or {}
    # ⚠️ 不同批次的数据里 throwDamage 有时带 total、有时只有各类型分量
    _tw_total = float(_tw.get("total") or 0.0) or sum(
        v for k, v in _tw.items()
        if isinstance(v, (int, float)) and v > 0 and k.lower() not in _SKIP_TYPES)
    if _td > 0 and _tw_total > 0:
        _cap = max(1, int(spec.get("throw_max_stacks") or 1))
        _stacks = max(0, min(int(spec.get("throw_stacks") or 0), _cap))
        _ratio = _tw_total / (dmg.get("total") or 1.0)
        _mult = 1 + _td * _stacks / 100.0
        throw_info = {
            "base": _tw_total, "per_stack": _td,
            "stacks": _stacks, "max_stacks": _cap, "mult": _mult,
            "health": health * _ratio * _mult,
            "health_crit": health * _ratio * _mult * e_body,
            "head": health * _ratio * _mult * e_body * head_mult,
        }

    # 超宏 Overguard：不吃护甲与派系弱点/抗性，虚空 +50%，磁力加成
    og_info = None
    if spec["overguard"]:
        flat = sum(per_type.values()) * fac_mod
        og_info = {"damage": overguard_damage(flat, mag_mult, per_type),
                   "crit": overguard_damage(flat, mag_mult, per_type) * e_body,
                   "mag_mult": mag_mult,
                   "pool": spec["overguard"] if isinstance(spec["overguard"], float) else None}

    # Sentient 适应：按伤害占比前 N 个类型各吃 90/80/75/70% 抗性
    adapt_info = None
    if spec["adapt"]:
        _sum_h = sum(per_type_health.values())
        eff, resisted = adapt_damage(per_type_health, spec["adapt"])
        adapt_info = {"levels": min(len(ADAPT_RESIST), spec["adapt"]),
                      "resisted": resisted,
                      "factor": (eff / _sum_h) if _sum_h else 1.0}

    return {
        "ok": True,
        "faction": faction, "level": level, "armor": armor, "dr": dr,
        "dr_raw": dr_raw,
        "armor_eff": armor_eff, "strip": strip, "strip_note": strip_note,
        "base_armor": base_armor,
        "per_type_health": per_type_health, "per_type_shield": per_type_shield,
        "health": health, "shield": shield,
        "head_health": health * e_head if spec["headshot"] else None,
        "headshot": spec["headshot"], "head_mult": head_mult,
        "crit_cc": cc, "crit_cm": cm, "crit_exp": e_body,
        "multishot_total": ms, "fire_rate": fire_rate, "fire_note": fr_note,
        "per_trigger_health": health * ms,
        "dps": health * ms * fire_rate,
        "dps_sustained": dps_sustained,
        "galv_applied": spec.get("galv_applied") or [],
        "magazine": magazine, "reload": reload_t,
        "pools": pools, "steel_path": spec["steel_path"],
        "dots": dots, "viral_mult": viral_mult, "mag_mult": mag_mult,
        "viral": spec["viral"], "magnetic": spec["magnetic"],
        "hit_weak": weak, "hit_resist": resist,
        "fac_weak": fac_weak, "fac_resist": fac_resist,
        "enemy": enemy, "hp": hp, "shield_hp": shield_hp, "shots": shots,
        "base_level": base_level, "mods": spec.get("mods") or [],
        "physical": spec["physical"], "notes": spec.get("notes") or [],
        "unknown": spec.get("unknown") or [],
        "steady": ss, "heavy": heavy_info, "overguard": og_info,
        "stance": ({"name": _st.get("name"), "zh": _st.get("zh"),
                    "combo": _st_combo.get("name"),
                    "mult": _st_mult, "procs": _st_procs} if _st else None),
        "status_types": _n_types, "conditional": spec.get("conditional") or [],
        "adapt": adapt_info, "throw": throw_info, "modes": mode_segments,
        "segments_total": segments_total,
        "arcanes": spec.get("arcanes") or [],
        "crit_cc_heavy": cc_heavy, "crit_exp_heavy": e_heavy,
    }


# 段名中文（warframe-items 段名是英文；wfsim 段名已中文）
SEG_ZH = {
    "Rocket Impact": "火箭直击", "Rocket Explosion": "火箭爆炸",
    "Area Attack": "范围攻击", "Charged Attack": "蓄力攻击",
    "Charged Explosion": "蓄力爆炸", "Incarnon Form": "灵化形态",
    "Incarnon Form AoE": "灵化形态范围", "Slam Attack": "震地攻击",
    "Heavy Slam": "重击震地", "Throw": "投掷", "Throw Explosion": "投掷爆炸",
    "Explosion": "爆炸", "Radial": "范围伤害", "Cluster": "集束子炸弹",
    # —— 投掷类（Glaive/战刃）：warframe-items 的段名是英文，逐条汉化 ——
    "Normal Attack": "普通攻击",
    "Throw Impact": "投掷命中", "Throw Bounce": "投掷弹跳",
    "Throw Bounce Explosion": "投掷弹跳爆炸",
    "Throw Recall": "投掷回旋", "Throw Recall Explosion": "投掷回旋爆炸",
    "Charged Throw": "充能投掷",
    "Charged Throw Impact": "充能投掷命中",
    "Charged Throw Explosion": "充能投掷爆炸",
    "Charged Throw Bounce Explosion": "充能投掷弹跳爆炸",
    "Charged Throw Recall Explosion": "充能投掷回旋爆炸",
    "Power Throw": "奋力一掷", "Glaive Radial Attack": "战刃范围攻击",
    "Charge": "蓄力", "Charged Shot": "蓄力射击", "Uncharged Shot": "未蓄力射击",
    "Quick Shot": "速射", "Perfect Shot": "完美射击",
    "Full Auto Mode": "全自动模式", "Alt-Fire": "次要开火",
    "Alt-Fire Explosion": "次要开火爆炸", "Secondary Fire": "次要开火",
    "Secondary Fire AoE": "次要开火范围", "Radial Attack": "范围攻击",
    "Cannon Mode Projectile": "炮击模式直击",
    "Cannon Mode Explosion": "炮击模式爆炸",
    "Cannon Mode Cluster Bomb Contact": "炮击集束弹接触",
    "Cannon Mode Cluster Bomb Explosion": "炮击集束弹爆炸",
    "Glass Explosion": "玻璃爆炸", "Slug Impact": "弹丸直击",
    "Grenade Impact": "榴弹直击", "Grenade Detonation": "榴弹引爆",
    "Projectile Impact": "弹丸直击", "Orb Merging Damage": "球体融合伤害",
    "Poison Cloud": "毒气云", "Corrosive DoT": "腐蚀持续伤害",
    "Toxin Cloud": "毒素云", "Spear Throw": "长矛投掷",
    "Auto": "全自动", "Semi": "半自动", "Burst": "点射",
    "Slug": "弹丸", "Charged": "蓄力",
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
    "impact":      {"dur": 1.0,  "max": 5},
    "puncture":    {"dur": 10.0, "max": 5},
    "slash":       {"dur": 6.0,  "max": None, "ratio": 0.35, "bypass_armor": True},
    "heat":        {"dur": 6.0,  "max": None, "ratio": 0.50},
    "cold":        {"dur": 6.0,  "max": 10},
    "electricity": {"dur": 6.0,  "max": None, "ratio": 0.50},
    "toxin":       {"dur": 6.0,  "max": None, "ratio": 0.50, "bypass_shield": True},
    "blast":       {"dur": 1.5,  "max": 10,   "ratio": 0.30, "instant": True},
    "corrosive":   {"dur": 8.0,  "max": 10},
    "gas":         {"dur": 6.0,  "max": 10,   "ratio": 0.50},
    "magnetic":    {"dur": 6.0,  "max": 10},
    "radiation":   {"dur": 12.0, "max": 10},
    "viral":       {"dur": 6.0,  "max": 10},
    "void":        {"dur": 3.0,  "max": None},
    "tau":         {"dur": 8.0,  "max": 10},
}
# Sentient 适应：血线 25/45/65/80% 各适应一次，按「打得最多的类型」排序取抗性
ADAPT_RESIST = (0.90, 0.80, 0.75, 0.70)
OVERGUARD_VOID_BONUS = 0.50      # 超宏：虚空 +50%，其余类型不吃派系倍率
COMBO_STEP = 20                  # 近战连击：每 20 连击一层
COMBO_CAP_TIER = 12              # 220 连击（12 层）封顶；Venka Prime 可到 13 层未建模


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


def steady_state_dps(per_type: dict[str, float], base_after: float,
                     fac_mod: float, fac_table: dict, faction: str,
                     procs_per_sec: float, status_mult: float,
                     crit_cc: float, crit_cm: float, head_mult: float,
                     multishot_total: float, fire_rate: float,
                     spec: dict, strip_explicit: float = 0.0) -> dict:
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
    if cold_stacks >= 1:
        cm_bonus = min(0.5, 0.1 + 0.05 * (cold_stacks - 1))
    pun_stacks = stacks_of("puncture")
    cc_bonus = min(0.25, 0.05 * pun_stacks) if pun_stacks >= 1 else 0.0
    e_crit_ss = crit_expectation(crit_cc + cc_bonus, crit_cm + cm_bonus)

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
    dots_detail: dict[str, float] = {}
    dot_dps = 0.0
    for el, cfg in STATUS_TABLE.items():
        ratio = cfg.get("ratio")
        if not ratio or rate.get(el, 0.0) <= 0:
            continue
        vm = fac_table.get(el, {}).get(faction, 1.0)
        per_stack = (base_after * ratio * fac_mod * fac_mod * vm
                     * status_mult * e_crit_ss)
        if spec.get("headshot"):
            per_stack *= head_mult
        if cfg.get("instant"):                    # 爆炸：1.5s 后一次性结算
            contribution = rate[el] * per_stack
        else:
            active = rate[el] * cfg["dur"]
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
        "viral_stacks": viral_stacks, "viral_mult": viral_mult,
        "mag_stacks": mag_stacks, "mag_mult": mag_mult,
        "corrosive_stacks": cor_stacks, "strip": strip, "dr": dr_ss,
        "cold_stacks": cold_stacks, "cm_bonus": cm_bonus,
        "puncture_stacks": pun_stacks, "cc_bonus": cc_bonus,
        "crit_exp": e_crit_ss,
        "dps_direct": direct_dps, "dps_dot": dot_dps,
        "dps_total": direct_dps + dot_dps,
        "dots_detail": dots_detail,
    }


def overguard_damage(per_type_flat: float, mag_mult: float,
                     per_type: dict[str, float]) -> float:
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
    order = sorted((k for k, v in per_type.items() if v > 0),
                   key=lambda k: -per_type[k])
    resisted = {}
    remaining = dict(per_type)
    for i, k in enumerate(order[:levels]):
        r = ADAPT_RESIST[i]
        resisted[k] = r
        remaining[k] = remaining[k] * (1 - r)
    return sum(remaining.values()), resisted


def card_lines(weapon: dict, spec: dict, res: dict,
               alts: Optional[list] = None) -> list[str]:
    """伤害计算卡的正文行（独立成模块函数：便于离线渲染探针与测试复用）。

    U36 抗性重构后，元素之间的差异只有两处：**派系弱点 ±50%** 与**异常状态**
    （病毒/磁力加伤、腐蚀与火剥甲、DoT）。卡面把这两处显式写出来，
    免得用户以为「换了元素数值却一样」是算错了。
    """
    if res and res.get("ok") is False:          # 绝不返回空：出错也要有卡面
        out = [f"❗ {res.get('error') or '数据缺失，无法计算'}"]
        if res.get("unknown"):
            out.append("未识别（已忽略）：" + "、".join(res["unknown"]))
        out.append("换英文名或完整中文名再试；「伤害」不带参数可看用法。")
        return out
    dt = (weapon or {}).get("damage") or {}
    base_parts = "、".join(
        f"{TYPE_ZH.get(k, k)}{round(v, 1):g}" for k, v in dt.items()
        if isinstance(v, (int, float)) and v > 0
        and k not in ("total", "cinematic", "shieldDrain", "healthDrain",
                      "energyDrain"))
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
        mod_bits.append("合成 " + "、".join(f"{TYPE_ZH.get(k, k)}+{v:g}%"
                                           for k, v in res["pools"].items()))
    for el, pct in res["physical"].items():
        mod_bits.append(f"{TYPE_ZH.get(el, el)}+{pct:g}%")
    if spec.get("headshot_bonus"):
        mod_bits.append(f"爆头倍率+{spec['headshot_bonus']:g}%")
    cc = weapon.get("criticalChance") or 0.0
    cm = weapon.get("criticalMultiplier") or 1.0
    ms = weapon.get("multishot") or 1

    lines = [
        f"◆ 武器：{weapon.get('zh') or weapon['name']}（{weapon['name']}）"
        f"｜MR{weapon.get('masteryReq', 0)}",
        f"　基伤 {dt.get('total', 0):g}"
        + (f"（{base_parts}）" if base_parts else "")
        + f"｜暴击 {cc * 100:g}% ×{cm:g}"
        + f"｜射速 {res['fire_rate']:.2f}".rstrip("0").rstrip(".")
        + (f"（{res['fire_note']}）" if res.get("fire_note") else "")
        + (f"｜多重 {ms:g}" if ms and ms > 1 else ""),
    ]
    if mod_bits:
        lines.append(f"◆ 配卡：{'、'.join(mod_bits)}")
    lines.append(
        f"◆ 目标：{FACTION_ZH.get(res['faction'], res['faction'])}"
        f" {res['level']}级｜护甲 {res['armor']:.0f}"
        + (f"（减免 {res.get('dr_raw', res['dr']) * 100:.1f}%）"
           + ("［已达 2700 上限］" if res["armor"] >= 2700 else "")
           if res["armor"] > 0 else "（无护甲）"))
    en = res.get("enemy")
    if en:
        nm = en.get("zh") or en.get("name")
        lines.append(
            f"　敌人：{nm}（基准 {en.get('base_level')} 级 / "
            f"血量 {en.get('base_health'):.0f}"
            + (f" / 护盾 {en.get('base_shield'):.0f}" if en.get("base_shield") else "")
            + (f" / 护甲 {en.get('base_armor'):.0f}" if en.get("base_armor") else "")
            + "）")
    if res.get("arcanes"):
        _cond = [a for a in res["arcanes"]
                 if any(k in (a.get("text") or "").lower() for k in
                        ("chance", "on kill", "on hit", "on critical", "on status",
                         "on damaged", "on headshot", "while "))]
        _plain = [a for a in res["arcanes"] if a not in _cond]
        lines.append("◆ 赋能：" + "、".join(
            (a.get("zh") or a["name"]) for a in res["arcanes"])
            + ("（条件触发，未计入数值）" if _cond and not _plain else ""))
        for _a in res["arcanes"][:2]:
            if _a.get("text"):
                lines.append(f"　{_a.get('zh') or _a['name']}：{_a['text'][:110]}")
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
            if eff.get("physical_convert"):
                where.append("物理转换·总量不变")
            if eff.get("conditional"):
                where.append("含条件触发(未计入)")
            tag.append(f"{name}（{'、'.join(where) or '未计入'}）")
        lines.append("◆ MOD：" + "｜".join(tag))
    _stc = res.get("stance")
    if _stc:
        _p = "、".join(TYPE_ZH.get(k, k) for k in _stc["procs"])
        lines.append(f"◆ 架势：{_stc['zh'] or _stc['name']}（{_stc['combo']}）"
                     f" 每段 ×{_stc['mult']:.2f}"
                     + (f"｜强制异常 {_p}" if _p else "｜无强制异常"))
    if res.get("status_types"):
        lines.append(f"◆ 异况超量类：按目标身上 {res['status_types']} 种异常计"
                     "（用「异常N」改）")
    _cond = res.get("conditional") or []
    for _c in _cond[:3]:
        lines.append(f"✎ 条件触发·未计入：{_c}".replace("\\n", " "))
    if len(_cond) > 3:
        lines.append(f"✎ …另有 {len(_cond) - 3} 条条件触发未计入")
    _galv = res.get("galv_applied") or []
    if _galv:
        lines.append("◆ 镀层堆叠已计入：" + "｜".join(_galv[:4])
                     + ("｜…" if len(_galv) > 4 else ""))
    if res["fac_weak"] or res["fac_resist"]:
        weak_txt = "、".join(TYPE_ZH.get(k, k) for k in res["fac_weak"]) or "无"
        res_txt = "、".join(TYPE_ZH.get(k, k) for k in res["fac_resist"]) or "无"
        lines.append(f"◆ 派系弱点：{weak_txt} ×1.5｜抗性：{res_txt} ×0.5")
        if res["fac_weak"]:
            hit = [TYPE_ZH.get(k, k) for k in res["hit_weak"]] or ["（本配卡没用到）"]
            lines.append(f"　本配卡吃到加成的：{'、'.join(hit)}")
    if res["strip_note"]:
        lines.append(f"◆ 剥甲：{'、'.join(res['strip_note'])}"
                     f" → 护甲 {res['armor']:.0f}→{res['armor_eff']:.0f}"
                     f"（减免 {res['dr_raw'] * 100:.1f}%→{res['dr'] * 100:.1f}%）")
    st_bits = []
    if res["viral"]:
        st_bits.append(f"病毒{res['viral']}层（对血 ×{res['viral_mult']:.2f}）")
    if res["magnetic"]:
        st_bits.append(f"磁力{res['magnetic']}层（对盾 ×{res['mag_mult']:.2f}）")
    if st_bits:
        lines.append(f"◆ 状态加伤：{'、'.join(st_bits)}")
    lines.append(f"◆ 单发对血：{res['health']:.0f}（无暴击）"
                 f"｜单发对盾：{res['shield']:.0f}")
    top = sorted(res["per_type_health"].items(), key=lambda kv: -kv[1])[:4]
    if len(top) > 1:
        lines.append("　明细：" + "、".join(
            f"{TYPE_ZH.get(k, k)} {v:.0f}" for k, v in top if v > 0))
    if res["dots"]:
        lines.append("◆ 异常 DoT（6s 总量，含暴击期望）：" + "、".join(
            f"{TYPE_ZH.get(k, k)} {v:.0f}"
            for k, v in sorted(res["dots"].items(), key=lambda kv: -kv[1])))
    crit_exp = res["crit_exp"]
    lines.append(
        f"◆ 暴击期望：×{crit_exp:.2f}"
        f"（暴率 {res['crit_cc'] * 100:.0f}% ×暴伤 {res['crit_cm']:.2f}）"
        f"｜单发 {res['health'] * crit_exp:.0f}")
    lines.append(
        f"◆ 爆头期望：{res['health'] * crit_exp * res['head_mult']:.0f}"
        f"（含暴击；爆头倍率 ×{res['head_mult']:.2f}）")
    if res.get("hp") is not None:
        pool = f"　血量 {res['hp']:.0f}"
        if res.get("shield_hp"):
            pool = f"　护盾 {res['shield_hp']:.0f} → " + pool.strip()
        lines.append(f"◆ 该目标：{pool.strip()}"
                     f"｜平均 {res['shots']} 发击杀（身体、含暴击期望）")
    # 每次扳机 / DPS 都给两个口径：含暴击期望（换卡会变）+ 无暴击（白字）
    pt = res["per_trigger_health"]
    dps = res["dps"]
    crit_txt = f"｜{pt:.0f}（无暴击）" if crit_exp != 1.0 else ""
    if res["multishot_total"] > 1:
        lines.append(f"◆ 每次扳机（多重 ×{res['multishot_total']:.2f}）："
                     f"{pt * crit_exp:.0f}（含暴击）{crit_txt}")
    else:
        lines.append(f"◆ 单发：{pt * crit_exp:.0f}（含暴击）{crit_txt}")
    lines.append(f"◆ DPS 爆发：{dps * crit_exp:.0f}（含暴击）"
                 + (f"｜{dps:.0f}（无暴击）" if crit_exp != 1.0 else "")
                 + f"｜射速 {res['fire_rate']:.2f}".rstrip("0").rstrip("."))
    if res.get("dps_sustained"):
        lines.append(f"◆ DPS 持续 ≈ {res['dps_sustained'] * crit_exp:.0f}"
                     f"（{res['magazine']:.0f} 发弹匣 + 装填 {res['reload']:.1f}s 循环）")
    # 面板口径：顶层伤害取自 attacks 里哪一段（弓箭=蓄力、爆炸=含范围段…）
    # —— 不同计算器对「基础伤害」的口径常不同（wfsim 面板对弓显示未蓄力、
    # 对 lich 默认套 +60% 回响、爆炸只显示直击段），显式标注免得被当算错。
    _ats = weapon.get("attacks") or []
    if len(_ats) > 1:
        _main_d = {k: float(v) for k, v in (weapon.get("damage") or {}).items()
                   if isinstance(v, (int, float)) and v > 0 and k != "total"}
        _main_sig = tuple(sorted((k, round(v, 3)) for k, v in _main_d.items()))
        _basis = next((a.get("name") for a in _ats
                       if tuple(sorted((k, round(float(v), 3))
                                       for k, v in (a.get("damage") or {}).items()))
                       == _main_sig), None)
        _all_names = [a.get("name") for a in _ats if a.get("name")][:4]
        if _basis:
            lines.append(f"◆ 面板口径：本表主段 = {SEG_ZH.get(_basis, _basis)}"
                         f"（该武器另有段：{'、'.join(SEG_ZH.get(n, n) for n in _all_names if n != _basis)}）")
        else:
            # 顶层伤害不等于任何单段 → 它把多段合并了（爆炸/蓄力武器常见，
            # warframe-items 的顶层 damage 就是这么给的）。这是数据源口径，
            # 逐把核对 wiki 前先如实标注，避免被当成算错。
            lines.append("◆ 面板口径：本表基础伤害含多段合并（"
                         + "、".join(SEG_ZH.get(n, n) for n in _all_names)
                         + "）——下限请以「含段合计」行拆分为准")
    # 多段合计：主段 + 各额外段（范围/AoE/集束…），段吃同样的 MOD 乘区
    _st = res.get("segments_total")
    if _st and _st.get("n_segments"):
        _parts = [f"主段 {_st['main']:.0f}"]
        _detail = []
        for _m in res.get("modes") or []:
            _pv = float(_m.get("per_trigger_health") or 0.0)
            if _pv <= 0:
                continue
            _extra = []
            if _m.get("radius_m"):
                _extra.append(f"半径 {_m['radius_m']:g}m")
            if _m.get("takes_multishot") is False:
                _extra.append("不吃多重")
            _detail.append(f"{SEG_ZH.get(_m['name'], _m['name'])} {_pv:.0f}"
                           + (f"（{'、'.join(_extra)}）" if _extra else ""))
        lines.append("◆ 含段合计（每次扳机）："
                     f"{_st['total']:.0f}（含暴击）"
                     f"＝" + " + ".join(_parts + _detail))
        lines.append("　※ 段是独立命中实例，各吃同一套 MOD 乘区；"
                     "范围段按命中中心（100%）计，边缘会衰减")
    # v1.6：实战（异常稳态）/ 重击 / 超宏 / 适应
    ssr = res.get("steady") or {}
    if ssr.get("dps_total"):
        lines.append(f"◆ 实战（稳态，异常已建立）：DPS ≈ {ssr['dps_total']:.0f}"
                     f"｜直伤 {ssr['dps_direct']:.0f} + DoT {ssr['dps_dot']:.0f}")
        bits = [f"触发 {ssr['status_chance'] * 100:.0f}%（{ssr['procs_per_sec']:.2f} 次/秒）"]
        if ssr.get("viral_stacks"):
            bits.append(f"病毒 {ssr['viral_stacks']:.0f} 层 ×{ssr['viral_mult']:.2f}")
        if ssr.get("corrosive_stacks"):
            bits.append(f"腐蚀剥甲 {ssr['strip'] * 100:.0f}%")
        if ssr.get("cm_bonus"):
            bits.append(f"冰 +{ssr['cm_bonus']:.2f}暴伤")
        if ssr.get("cc_bonus"):
            bits.append(f"穿刺 +{ssr['cc_bonus'] * 100:.0f}%暴率")
        det = "、".join(f"{TYPE_ZH.get(k, k)} {v:.0f}"
                        for k, v in sorted(ssr["dots_detail"].items(),
                                           key=lambda kv: -kv[1])[:3] if v > 0)
        if det:
            bits.append(f"DoT：{det}")
        lines.append("　" + "｜".join(bits))
    hv = res.get("heavy")
    if hv:
        lines.append(f"◆ 重击（连击 {hv['combo_hits']} → ×{hv['multiplier']:.1f}）："
                     f"单发 {hv['health']:.0f}｜含暴击 {hv['health_crit']:.0f}"
                     f"｜爆头 {hv['head']:.0f}"
                     + "（重击会清空连击数）")
    og = res.get("overguard")
    if og:
        line = (f"◆ 对超宏：{og['damage']:.0f}（含暴击 {og['crit']:.0f}）"
                f"｜磁力 ×{og['mag_mult']:.2f}"
                "｜超宏不吃护甲与派系弱点、免疫异常（虚空 +50%）")
        if og.get("pool"):
            line += f"｜超宏池 {og['pool']:.0f} → " \
                    f"{math.ceil(og['pool'] / og['crit']) if og['crit'] > 0 else '—'} 发打穿"
        lines.append(line)
    ad = res.get("adapt")
    if ad:
        who = "、".join(f"{TYPE_ZH.get(k, k)} −{v * 100:.0f}%"
                        for k, v in ad["resisted"].items())
        lines.append(f"◆ 适应（Sentient {ad['levels']} 层）：{who}"
                     f" → 有效伤害 ×{ad['factor']:.2f}"
                     "（虚空伤害可重置适应）")
    # 多段判定：投掷 / 投掷爆炸 / 充能投掷 / 震地…（各段类型构成常常完全不同）
    modes = res.get("modes") or []
    if modes:
        lines.append(f"◆ 其它段判定（{len(modes)} 段，含元素/派系/护甲）")
        for m in modes[:8]:
            _top = max((m["damage"] or {"?": 0}).items(), key=lambda kv: kv[1])
            _tag = "同普通" if m.get("same_as_main") else TYPE_ZH.get(_top[0], _top[0])
            line = (f"　{m['name']}（{_tag}{m['total']:g}）"
                    f" → {m['health']:.0f}｜含暴击 {m['health_crit']:.0f}")
            _redu = float((m.get("falloff") or {}).get("reduction") or 0.0)
            if _redu > 0:
                line += f"｜射程内衰减 {_redu * 100:.0f}%"
            lines.append(line)
        if len(modes) > 8:
            lines.append(f"　…另 {len(modes) - 8} 段（次级开火/灵化等）")
    if alts:
        lines.append("同类候选：" + "、".join(
            v.get("zh") or v.get("name", "") for v in alts))
    for n in (res.get("notes") or []):
        lines.append(f"✎ {_note_zh(n)}")
    if res.get("unknown"):
        lines.append("⚠ 未识别（已忽略）：" + "、".join(res["unknown"])
                     + "　—— 该词条没进 MOD 名表，如有误请换官方名再试")
    lines.append("※ U36 起血量/护甲不再分类型，元素差异只有两处："
                 "①派系弱点 ±50% ②异常状态（病毒/磁力/腐蚀剥甲/DoT）")
    lines.append("※ 敌人护甲减伤 = 90%×√(护甲/2700)（与玩家护甲公式不同）；"
                 "复合元素按 火>冰>电>毒 两两配对（槽位顺序无法从指令推断）")
    lines.append("※ 乘区：基伤/多重/暴率/暴伤各组内相加，元素同元素相加后合成，"
                 "派系 MOD 独立乘区；暴击按期望值（含超 100% 的多段暴击，"
                 "公式 1+暴率×(暴伤−1)），每次扳机/DPS 同时给「含暴击」与「无暴击」")
    lines.append("※ 「实战 DPS」是**稳态近似**：异常层数已建立、目标持续挨打；"
                 "未计入前几秒的爬升、换目标掉层、目标死亡截断与 Boss 伤害衰减。"
                 "公式来源 wiki（wiki.warframe.com），2026-09-16 核对")
    if res["steel_path"]:
        lines.append("※ 钢路：敌人血量/护盾 +100%（护甲已不加成），本卡未乘算")
    return lines
