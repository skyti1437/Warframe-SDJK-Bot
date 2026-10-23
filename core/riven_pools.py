# -*- coding: utf-8 -*-
"""紫卡**词条池**：按武器判断哪些词条刷得出（「词条范围」指令的过滤层）。

口径照搬 wfsim（`engine/src/build/rivens/pools.rs`），三层证据，**优先级从高到低**：

1. **逐家族证据**（`core/data/de/riven_pools.json` 的 `never` / `rolls` / `survey`）——
   来自 WM 在售紫卡抽样与人工实卡（wfsim 的 `exceptions.yaml` / `physical.yaml` /
   `pools.yaml`）。`never` = 该家族刷不出；`rolls` = 规则说刷不出但**实际能出**。
2. **按武器自身推导**（本模块 `derived_for`）：守护武器无变焦 / 后坐力；无弹药池
   无弹药上限；无飞行弹道且无伤害衰减无弹道速度；物理三项按 25% 份额**只标未确认**。
3. **职业池**（`core/riven_analysis._BASE` 的 None 列 + 本文件的 `class_pools`
   正负限制）—— 该职业根本没有这个词条就列不出来。

★ 与 wfsim 的一处**有意差异**：wfsim 的计算路径刻意不读 `pools.yaml`（怕一次坏抓取
把池全改掉），本插件按用户 2026-10-23 指令把它的 `never` / `rollable` 也并进逐家族
证据。理由：那份抽样已**冻结进仓库**（不是运行期抓取），且它只**移除**「500 条挂单
里一条都没出现」的词条 —— 方向与「宁可少列不可错列」一致。

★ 物理三项（冲击 / 穿刺 / 切割）**不由规则删除**：wiki 那条「占比 ≤25% 刷不出」
两个方向都有反例（Ocucor 0% 切割能出、Phenmor 30% 穿刺不出），所以规则命中的物理
词条进 `unconfirmed` —— 卡上照列但标「?」，等家族证据表态。
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


# 职业池的**例外**：DE 的导出池文件（`class_pools`）滞后于热修时按这里放行。
# 2026-10-07 实测依据：Update 44 Hotfix 44.0.3 补丁说明原文
# 「Shotgun Rivens can now roll Zoom stats」（为 Riven Splicer 铺路）；
# 我方 `_BASE["zoom"]` 的霰弹枪列（41.994）即按此补录，而 wfsim 的
# `data/rivens/shotgun.yaml` 尚未含 `WeaponZoomFovMod` ⇒ 上游镜像落后。
_POOL_EXTRA = {"shotgun": {"zoom"}}


def class_pool(cls: str) -> dict:
    """该职业池的词条正负限制：``{词条 id: {"bonus": bool, "malus": bool}}``。"""
    return (_load_pools().get("class_pools") or {}).get(cls) or {}


def _pool_ids(cls: str) -> set:
    """该职业**有**哪些词条：以 DE 导出的职业池为真源（+ `_POOL_EXTRA` 热修例外）。

    为什么不用 `_BASE` 的非 None 列当唯一判据：`_BASE` 是 wiki 表的手工转录，
    `attack_speed` / `fire_rate` 两行数值相同且**列列都有值** ⇒ 单用它会让霰弹枪
    同时列出「射速」与「攻速」（只有近战才有攻速）。DE 的导出池是按职业列的真源。
    """
    ids = set(class_pool(cls))
    ids |= _POOL_EXTRA.get(cls) or set()
    return ids


def family_entry(family_root: str) -> dict:
    """家族证据行（按我方家族根查；查不到返回 {}）。"""
    if not family_root:
        return {}
    fams = _load_pools().get("families") or {}
    return fams.get(family_root) or fams.get(f"wf:{family_root}") or {}


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


def derived_for(cls: str, family_root: str, members: list[str]) -> set[str]:
    """**规则推导**出的排除项（不含物理三项，物理只进 `unconfirmed_for`）。

    Args:
        cls: 武器基值列名（rifle / shotgun / pistol / archgun / melee）。
        family_root: 我方家族根（查「无弹药池」用）。
        members: 家族成员英文名（判守护 / 飞行 / 物理占比用；一张紫卡全家族通用）。
    """
    recs = _family_weapons(members)
    out: set[str] = set()
    # 守护武器：没人给守护武器瞄准 ⇒ 无变焦、无后坐力（家族**每个**成员都是守护才算）
    if recs and all(_SENTINEL_CAT in (v.get("category") or "").lower() for v in recs):
        out |= {"zoom", "recoil"}
    # 无弹药池：无穷大的百分比不是词条（证据表里的 no_ammo_pool 由 wfsim 武器数据算出）
    if (family_entry(family_root) or {}).get("no_ammo_pool"):
        out.add("ammo_max")
    # 没有东西可作用：既不是弹道、也没有伤害衰减 ⇒ 弹道速度无意义
    if recs and not _flies(recs):
        out.add("projectile_speed")
    return out


def unconfirmed_for(cls: str, family_root: str, members: list[str]) -> set[str]:
    """**未确认**的物理词条：25% 份额规则会拒绝、但家族证据没表态的那些。"""
    ev = family_entry(family_root)
    spoken = set(ev.get("never") or []) | set(ev.get("rolls") or [])
    survey = ev.get("survey") or {}
    spoken |= set(survey.get("never") or []) | set(survey.get("rollable") or [])
    recs = _family_weapons(members)
    if not recs:
        return set()
    shares = _physical_shares(recs)
    out = set()
    for sid in PHYSICAL_STATS:
        slug = sid.split("_")[0]  # impact_damage → impact
        if shares.get(slug, 0.0) <= 0.25 and sid not in spoken:
            out.add(sid)
    return out


def excluded_for(cls: str, family_root: str, members: list[str]) -> set[str]:
    """该武器刷不出的词条（规则 + 逐家族证据合并；物理三项只由证据拒绝）。"""
    out = {s for s in derived_for(cls, family_root, members) if s not in PHYSICAL_STATS}
    ev = family_entry(family_root)
    for sid in ev.get("rolls") or []:  # 证据说能出 ⇒ 从排除里去掉（压过规则）
        out.discard(sid)
    for sid in ev.get("never") or []:
        out.add(sid)
    survey = ev.get("survey") or {}
    for sid in survey.get("rollable") or []:
        out.discard(sid)
    for sid in survey.get("never") or []:
        out.add(sid)
    return out


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
    excl = excluded_for(cls, family_root, members)
    unc = unconfirmed_for(cls, family_root, members)
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
