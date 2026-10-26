# -*- coding: utf-8 -*-
"""紫卡**词条池**：按武器判断哪些词条刷得出（「词条范围」指令的过滤层）。

★ 2026-10-25 起**不再使用 wfsim 数据**（维护者定：wfsim 派生数据改为从游戏数据自己生成；
Astra 评估许可 L1）。数据表 `core/data/de/riven_pools.json` 由 `scripts/build_riven_pools.py`
从 DE 官方 Public Export + warframe-public-export-plus（MIT）+ 我方 WM 抽样生成。判据：

1. **职业池**（`class_pools`）：DE 官方现行紫卡模板的词条清单；每个词条能否正 / 负、限制条件
   （DamageTypes / NotSentinel / NotGrimoire）；`_BASE` 该列为 None 的不列（拿不到基值画不出区间）。
2. **DE 规则排除**（`derived_for`）：守护武器 / 魔典按限制条件排除；没有 PROJECTILE 标签的
   不出弹道速度。
3. **物理三项**（冲击 / 穿刺 / 切割）按 DE 的 ``DamageTypes`` 限制：武器该伤害占比 ≤25% 不列
   （wiki 与 riven-roll 同一口径）。2026-10-25 维护者定：wfsim 物理证据去掉后按规则删、再用我方抽样纠偏
   —— 规则与旧证据 84% 一致，两向各约 50 条分歧（Ocucor 0% 切割能出、Phenmor 30% 穿刺不出这类），
   待服务器上对物理词条补一轮 WM 抽样后由 `survey` 纠正。
4. **我方抽样**（`survey`）：见过 ⇒ 照列（实证压过规则，含物理三项）；没见过 ⇒ 标「?」照列，注明样本时间。
"""

from __future__ import annotations

import json
import re
from typing import Optional

from . import paths
from .logging_compat import logger
from .riven_analysis import _BASE, _CLASS_IDX

POOLS_NAME = "de/riven_pools.json"

# 物理三词条（我方 id）——wfsim `PHYSICAL` 的对应
PHYSICAL_STATS = ("impact_damage", "puncture_damage", "slash_damage")

# 武器「守护」类别（weapons_stats.json 的 category）
_SENTINEL_CAT = "sentinel"

_POOLS: Optional[dict] = None
_WEAPONS: Optional[dict] = None  # 归一化英文名 → weapons_stats 条目列表


def _load_pools() -> dict:
    """惰性加载词条池证据表；缺失/损坏 ⇒ 空表（退化为「只按职业池列」，不阻断查询）。"""
    global _POOLS
    if _POOLS is None:
        try:
            raw = json.loads(paths.read_path(POOLS_NAME).read_text(encoding="utf-8"))
            _POOLS = raw if isinstance(raw, dict) else {}
        except Exception:  # noqa: BLE001 - 表缺失退回职业池，不阻断
            logger.warning("[sdjk] 紫卡词条池表缺失/损坏，退化为仅按职业池过滤")
            _POOLS = {}
        logger.info(
            "[sdjk] 紫卡词条池已加载：家族 %d 条",
            len((_POOLS.get("families") or {}) if _POOLS else {}),
        )
    return _POOLS


def _load_weapons() -> dict:
    """武器伤害数据按英文名索引（判守护 / 飞行 / 物理占比用）。"""
    global _WEAPONS
    if _WEAPONS is None:
        idx: dict[str, list[dict]] = {}
        try:
            raw = json.loads(paths.read_path("weapons_stats.json").read_text(encoding="utf-8"))
            for v in (raw or {}).values():
                if not isinstance(v, dict):
                    continue
                n = _norm(v.get("name") or "")
                if n:
                    idx.setdefault(n, []).append(v)
        except Exception:  # noqa: BLE001
            logger.warning("[sdjk] weapons_stats.json 读取失败，词条池按武器推导降级")
            idx = {}
        _WEAPONS = idx
    return _WEAPONS


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def class_pool(cls: str) -> dict:
    """该职业池：``{词条 id: {"bonus", "malus", "spliced", "buff_restr"?, "curse_restr"?}}``（副本）。

    2026-10-25 起由 `scripts/build_riven_pools.py` 从 DE 官方导出（现行词条清单）+ PEP（正负与限制）
    生成，已含 44.0.3 霰弹枪变焦、近战 9 个融合词条、近战「近战伤害」不出负面 —— 原先手工打的三处
    补丁（_POOL_EXTRA / _POOL_PATCH / _POOL_OVERRIDE）与新数据逐项一致，已删除。
    """
    return {
        k: dict(v) for k, v in ((_load_pools().get("class_pools") or {}).get(cls) or {}).items()
    }


def _pool_ids(cls: str) -> set:
    """该职业**有**哪些词条（DE 官方现行紫卡模板）。

    为什么不用 `_BASE` 的非 None 列当唯一判据：`_BASE` 是 wiki 表的手工转录，
    `attack_speed` / `fire_rate` 两行数值相同且**列列都有值** ⇒ 单用它会让霰弹枪
    同时列出「射速」与「攻速」（只有近战才有攻速）。DE 的导出池是按职业列的真源。
    """
    return set(class_pool(cls))


_KEY_INDEX: Optional[dict] = None


def _family_key_index() -> dict:
    """``families`` 键的**小写索引**（小写键 → 真实键）。

    为什么需要：生成器给「家族表里还没有的新武器」按 ``name:<英文名>`` 存键并**保留原大小写**
    （如 ``name:Aksondol``），而 `matching.family_key` 对查不到的武器名返回**小写**形式
    （``aksondol``）⇒ 精确查键必然落空，整条抽样证据被静默丢掉。
    2026-10-26 部署实测：Aksondol（P1 新增武器）的「抽样注脚」从来不出现。
    """
    global _KEY_INDEX
    if _KEY_INDEX is None:
        fams = _load_pools().get("families") or {}
        _KEY_INDEX = {str(k).lower(): k for k in fams}
    return _KEY_INDEX


def family_entry(family_root: str) -> dict:
    """家族证据行（按我方家族根查；查不到返回 {}）。"""
    if not family_root:
        return {}
    fams = _load_pools().get("families") or {}
    # 家族表里还没有的新武器，生成器按 `name:<英文名>` 存（family_members 对它们返回原名作根）
    for key in (family_root, f"name:{family_root}"):
        if key in fams:
            return fams[key]
    # 兜底：大小写无关（`family_key` 对新武器返回小写、生成器保留原大小写）
    low = str(family_root).lower()
    idx = _family_key_index()
    real = idx.get(low) or idx.get(f"name:{low}")
    return (fams.get(real) or {}) if real else {}


_FAM_TABLE: Optional[dict] = None


def _family_table() -> dict:
    global _FAM_TABLE
    if _FAM_TABLE is None:
        try:
            _FAM_TABLE = json.loads(
                paths.read_path("de/riven_families.json").read_text(encoding="utf-8")
            )
        except Exception:  # noqa: BLE001 - 表缺失退化为「单成员家族」
            logger.warning("[sdjk] 紫卡家族表读取失败，词条池按单成员家族处理")
            _FAM_TABLE = {}
    return _FAM_TABLE


def family_members(en: str) -> tuple[str, list[str]]:
    """英文名 → ``(家族根, 家族成员英文名列表)``（一张紫卡全家族通用）。

    ★ 单成员家族（Atomos / Ocucor / Phenmor 这类）**不在** `families` 成员表里 ——
    该表只收多成员家族；它们的家族根就是自己在 `by_name` 里的取值。所以查不到
    成员表时要按 `by_name` 反查同根的名字，否则整批单成员武器会退化成「无家族」
    （证据与家族属性全丢）。
    """
    from . import matching

    root = matching.family_key(en)
    if not root:
        return "", [en] if en else []
    table = _family_table()
    members = (table.get("families") or {}).get(root)
    if members:
        return root, list(members)
    same = [n for n, r in (table.get("by_name") or {}).items() if r == root]
    return root, (same or ([en] if en else []))


def _family_weapons(members: list[str]) -> list[dict]:
    """家族成员的伤害数据条目（成员英文名 → weapons_stats 条目）。"""
    idx = _load_weapons()
    out = []
    for m in members:
        out.extend(idx.get(_norm(m)) or [])
    return out


def _physical_shares(recs: list[dict]) -> dict[str, float]:
    """家族内**任一形态**上每种物理伤害的最大占比（wfsim：取家族所有形态的并集）。"""
    best = {"impact": 0.0, "puncture": 0.0, "slash": 0.0}
    for v in recs:
        for a in v.get("attacks") or []:
            dmg = a.get("damage") or {}
            tot = sum(x for x in dmg.values() if isinstance(x, (int, float)))
            if tot <= 0:
                continue
            for k in best:
                x = dmg.get(k) or 0
                if isinstance(x, (int, float)):
                    best[k] = max(best[k], x / tot)
    return best


def _flies(recs: list[dict]) -> bool:
    """家族内是否有会飞的弹道（弹道型段），或有伤害衰减段。

    wfsim 的两条判据：``shot_type == Projectile`` 的段算飞；**有伤害衰减也算** ——
    wiki 原文说投射物速度词条会整体移动衰减区间，所以霰弹枪也吃这个词条
    （Boar 保留 50% 到 25m，区间一动就有意义）。只看弹道型会把所有霰弹枪判成「不飞」。
    """
    for v in recs:
        for a in v.get("attacks") or []:
            if (a.get("shot_type") or "").strip().lower() == "projectile":
                return True
            if a.get("falloff"):
                return True
    return False


def derived_for(
    cls: str, family_root: str, members: list[str], *, positive: bool = True
) -> set[str]:
    """**按 DE 规则**排除的词条（不含物理三项，物理只进 `unconfirmed_for`）。

    2026-10-25 起按 DE 自己的限制条件判（`class_pools` 的 buff_restr / curse_restr）：
      · 守护武器（家族全员 SentinelWeapons）⇒ 排除带 ``NotSentinel`` 的词条（弹药上限 / 后坐力 / 变焦，
        近战的连击类、滑行暴击、处决伤害等）；
      · 魔典 ⇒ 排除带 ``NotGrimoire`` 的词条；
      · 没有 ``PROJECTILE`` 兼容标签 ⇒ 排除弹道速度（DE 的弹道速度词条要求该标签）。
    家族表里没有这些属性（武器数据缺失）时退回按 weapons_stats 推断。原「无弹药池不出弹药上限」
    是 wfsim 的推断，DE 规则里没有这条限制，已删除。
    """
    fam = family_entry(family_root)
    has_data = "projectile" in fam
    recs = [] if has_data else _family_weapons(members)
    sentinel = (
        bool(fam.get("sentinel"))
        if has_data
        else bool(recs) and all(_SENTINEL_CAT in (v.get("category") or "").lower() for v in recs)
    )
    grimoire = bool(fam.get("grimoire"))
    key = "buff_restr" if positive else "curse_restr"
    out: set[str] = set()
    for sid, lim in class_pool(cls).items():
        restr = set(lim.get(key) or [])
        if (sentinel and "NotSentinel" in restr) or (grimoire and "NotGrimoire" in restr):
            out.add(sid)
    projectile = fam.get("projectile") if has_data else (bool(recs) and _flies(recs))
    if not projectile and (has_data or recs):
        out.add("projectile_speed")
    return out


def physical_excluded(members: list[str]) -> set[str]:
    """物理三项里**占比 ≤25%** 的（DE DamageTypes 限制；份额取家族内任一形态的最大值）。"""
    recs = _family_weapons(members)
    if not recs:
        return set()
    shares = _physical_shares(recs)
    return {sid for sid in PHYSICAL_STATS if shares.get(sid.split("_")[0], 0.0) <= 0.25}


def unconfirmed_for(cls: str, family_root: str, members: list[str]) -> set[str]:
    """**未确认**的词条（卡上标「?」）。2026-10-25 起物理三项按规则直接删，这里不再有规则项；
    「?」只剩 `survey_unseen`（我方抽样没见过的）。保留函数供调用方合并。"""
    return set()


def excluded_for(
    cls: str, family_root: str, members: list[str], *, positive: bool = True
) -> set[str]:
    """该武器刷不出的词条：DE 规则排除；我方抽样**见过**的词条照列（实证压过规则）。

    抽样「没见过」不排除（`survey_unseen` 标「?」照列，2026-10-25 Astra 评估）。
    """
    out = {
        s
        for s in derived_for(cls, family_root, members, positive=positive)
        if s not in PHYSICAL_STATS
    }
    out |= physical_excluded(members)
    survey = family_entry(family_root).get("survey") or {}
    for sid in survey.get("rollable") or []:
        out.discard(sid)
    return out


def survey_unseen(family_root: str) -> tuple[set, dict]:
    """我方 WM 挂单抽样里没见过的词条 ``(id 集合, {"surveyed": 日期, "n": 样本量})``。"""
    survey = family_entry(family_root).get("survey") or {}
    ids = set(survey.get("never") or []) - set(survey.get("rollable") or [])
    return ids, {"surveyed": survey.get("surveyed") or "", "n": survey.get("n") or 0}


def available(
    cls: str,
    family_root: str,
    members: list[str],
    *,
    positive: bool,
) -> tuple[list[str], set[str]]:
    """该武器**可列**的词条：``(可用 id 列表, 未确认 id 集合)``。

    ``positive=True`` 取正面表、``False`` 取负面表。判据三层叠加：
      ① 职业池有这个词条（DE 导出池 `_pool_ids`，另要求 `_BASE` 该列非 None ——
         拿不到基值就画不出区间）；
      ② 正负限制允许（`class_pools` 的 ``bonus`` / ``malus``）；
      ③ 不在 `excluded_for` 里。
    物理未确认项**照列**（由调用方标「?」）。
    """
    idx = _CLASS_IDX.get(cls)
    if idx is None:
        return [], set()
    excl = excluded_for(cls, family_root, members, positive=positive)
    unc = unconfirmed_for(cls, family_root, members) | survey_unseen(family_root)[0]
    pool = class_pool(cls)
    in_pool = _pool_ids(cls)
    out = []
    for sid, base in _BASE.items():
        if base[idx] is None:  # 该职业拿不到基值
            continue
        if sid not in in_pool:  # 该职业的池子里没有这个词条
            continue
        lim = pool.get(sid) or {}
        if positive and lim.get("bonus") is False:
            continue
        if not positive and lim.get("malus") is False:
            continue
        if sid in excl:
            continue
        out.append(sid)
    return out, unc
