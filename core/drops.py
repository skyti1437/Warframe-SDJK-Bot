# -*- coding: utf-8 -*-
"""遗物掉落查询（数据源：core/data/drops.json）。

本模块只服务**遗物**相关指令（遗物入库 / 出库 / 出处）。
原先的通用「掉落 物品名」指令已废弃，对应的 ``fmt_drops`` / ``entries`` /
``resolve`` / ``suggest`` 等一并删除 —— 那些查询走 ``core/search.py``。

drops.json 由 WFCD `warframe-drop-data/all.slim.json`（DE 官方掉落表精简版）
构建，本模块用到的字段：
  places       {pid: "水星 · Apollodorus · 生存 · B轮"}
  relic_drops  {relic_lower: [[pid, rarity, chance], ...]}   # = 当前未入库遗物
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Optional

DATA_FILE = Path(__file__).resolve().parent / "data" / "drops.json"

_DB: Optional[dict] = None

MAX_ROWS = 200          # 单遗物最多展示的出处数（防爆卡）

# 缓存：出处三元组 / 推荐刷取点（都是纯函数，数据不变就不必重算）
_PLACES_CACHE: dict[str, list[tuple[str, str, str]]] = {}
_FARM_CACHE: Optional[dict[str, list[str]]] = None


def load() -> dict:
    global _DB
    if _DB is None:
        try:
            _DB = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            _DB = {}
    return _DB


def relic_source_lines(relic_key: str,
                       limit: Optional[int] = None) -> list[str]:
    """遗物的掉落位置，按「星球 · 节点 · 任务」合并轮次。

    原始出处把每个轮次都当成独立一条（防御 A/B/C 轮算三条），直接铺到卡片上
    既占地方又难读；合并成 ``水星 · Lares · 防御（A/B/C轮）`` 更接近玩家的问法
    ——「这遗物在哪刷」，而不是「第几轮出」。

    Args:
        relic_key: 掉落库里的遗物键，如 ``neo t11 relic``。
        limit: 最多返回几条；``None`` 表示全部（调用方自己切片时可以拿
            ``len()`` 当总数用）。

    Returns:
        合并轮次后的位置列表。
    """
    if not relic_key:
        return []
    rots: dict[str, list[str]] = {}
    order: list[str] = []
    for place in relic_sources(relic_key, limit=MAX_ROWS):
        parts = place.split(" · ")
        head, rot = place, ""
        # 末段形如 "C轮" 就是轮次。注意不能假定有 4 段 ——
        # 「霍瓦尼亚 · Legacyte Harvest · B轮」只有 3 段，早先按 >=4 判断会漏掉。
        if len(parts) >= 2 and parts[-1].endswith("轮"):
            head, rot = " · ".join(parts[:-1]), parts[-1][0]
        bucket = rots.setdefault(head, [])
        if rot and rot not in bucket:
            bucket.append(rot)
        if head not in order:
            order.append(head)
    merged = [f"{h}（{'/'.join(rots[h])}轮）" if rots[h] else h for h in order]
    return merged if limit is None else merged[:limit]


def unvaulted_relics() -> dict[str, list[str]]:
    """当前仍在掉落池（= 未入库）的遗物 -> 中文名列表。"""
    db = load()
    out = {}
    for key in (db.get("relic_drops") or {}):
        m = key.split()
        if len(m) >= 3:
            tier_cn = {"lith": "古纪", "meso": "前纪", "neo": "中纪",
                       "axi": "后纪", "requiem": "安魂"}.get(m[0], m[0])
            out[key] = f"{tier_cn} {m[1].upper()}"
    return out


def droppable_keys() -> set[str]:
    """当前掉落表里的遗物键前缀（``{"lith a12", …}``），供「能不能获取」判定。

    与 :func:`unvaulted_relics` 的区别只是形态：那边是 ``{键: 中文名}``
    （要展示），这边是纯集合（要判定）。部件反查卡每行都要判一次状态。
    """
    return {k.rsplit(" relic", 1)[0] for k in unvaulted_relics()}


def relic_sources(relic_key: str, limit: int = 12) -> list[str]:
    """遗物的原始出处串（未合并轮次），按掉落表顺序去重。"""
    db = load()
    places = db.get("places") or {}
    rows = (db.get("relic_drops") or {}).get(relic_key) or []
    seen, out = set(), []
    for pid, rarity, chance in rows:
        p = places.get(str(pid), "?")
        if p in seen:
            continue
        seen.add(p)
        out.append(p)
    return out[:limit]


# ---------------------------------------------------------------------------
# 刷取位置建议（2026-09-17 用户要求：出库卡按「图 4」那种形态给推荐点，
# 并且「只在指定位置能获取」的遗物要单独标记）
# ---------------------------------------------------------------------------

# 出处「很窄」的分界：常规遗物能在一两百个节点掉（实测最少 57），
# 而九重天储藏库那类只有 4~17 个节点。取 25 留足余量。
_NARROW_SOURCE_MAX = 25
# 速刷优先的任务类型（只用于同覆盖率时的排序）
_FAST_MISSIONS = ("破坏", "捕获", "歼灭", "中断", "防御", "生存", "拦截",
                  "间谍", "挖掘", "救援", "移动防御", "破坏（航道星舰）")

_TIER_CN_FROM_EN = {"lith": "古纪", "meso": "前纪", "neo": "中纪",
                    "axi": "后纪", "requiem": "安魂", "omnia": "全能"}


def relic_places(relic_key: str) -> list[tuple[str, str, str]]:
    """遗物的掉落地点，(星球, 节点, 任务类型) 去重后的列表。

    ★ limit 必须给足：`relic_source_lines` 的 MAX_ROWS（200）是给**展示**用的
      上限，而这里的计数需要完整出处 —— 截断会让「某节点能掉几个遗物」的
      统计偏小，进而把推荐刷取点选错（实测：截到 200 条时火星 · Olympus
      从中断类的最高覆盖掉到选不中）。
    ★ 结果缓存：出库卡要按遗物逐条问，重复解析很浪费（每次都要 split 上千行）。
    """
    if relic_key in _PLACES_CACHE:
        return _PLACES_CACHE[relic_key]
    out: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for place in relic_sources(relic_key, limit=10 ** 6):
        parts = place.split(" · ")
        if len(parts) < 3:
            continue
        t = (parts[0], parts[1], parts[2])
        if t not in seen:
            seen.add(t)
            out.append(t)
    _PLACES_CACHE[relic_key] = out
    return out


def special_source(relic_key: str) -> str:
    """出处很窄的遗物 → 返回简短说明（如「仅金星比邻星域 · 储藏库」）。

    常规遗物（出处遍地）返回空串 —— 它们用纪元级的「推荐刷取」即可覆盖。
    """
    places = relic_places(relic_key)
    if not places or len(places) > _NARROW_SOURCE_MAX:
        return ""
    combos: list[tuple[str, str]] = []
    for p, _node, m in places:
        if (p, m) not in combos:
            combos.append((p, m))
    # 同一任务类型、多个星球 → 合并成「仅A / B · 类型」（比逐个列短得多）
    if len({m for _p, m in combos}) == 1:
        mission = combos[0][1]
        planets: list[str] = []
        for p, _m in combos:
            if p not in planets:
                planets.append(p)
        return f"仅{' / '.join(planets[:2])} · {mission}"
    if len(combos) == 1:
        p, m = combos[0]
        return f"仅{p} · {m}"
    return "仅" + "、".join(f"{p} · {m}" for p, m in combos[:2])


def farm_hints(top: int = 4) -> dict[str, list[str]]:
    """按纪元给出**推荐刷取点**（数据驱动，随掉落表自动更新）。

    口径：统计该纪元当前可掉落的遗物各自出现在哪些节点，取「覆盖遗物数最多」
    的节点；同分时优先速刷任务类型，并且**每种任务类型只取一个**（速刷与耐力
    兼顾）。实测结果与社区公认刷取点一致：前纪 → 虚空 · Ani · 生存、
    中纪 → 天王星 · Ur · 中断、后纪 → 阋神星 · Xini · 拦截。

    Returns:
        ``{"古纪": ["虚空 · Taranis · 防御", …]}``
    """
    global _FARM_CACHE
    if _FARM_CACHE is not None:
        return _FARM_CACHE
    db = load()
    per_tier: dict[str, "Counter[str]"] = {}
    for key in (db.get("relic_drops") or {}):
        m = key.split()
        if len(m) < 2:
            continue
        tier = _TIER_CN_FROM_EN.get(m[0], m[0])
        cnt = per_tier.setdefault(tier, Counter())
        for p, node, mission in relic_places(key):
            # ★ 只把「能当刷取目标」的任务类型计入候选：
            #   九重天的「储藏库」/「空战」这类附赠掉落覆盖的遗物反而最多
            #   （实测古纪 8/8 都出现在金星比邻星域的储藏库表里），
            #   不排除掉它们就会把「储藏库」推成推荐点 —— 那不是玩家会去刷的地方。
            if mission not in _FAST_MISSIONS:
                continue
            cnt[f"{p} · {node} · {mission}"] += 1

    out: dict[str, list[str]] = {}
    for tier, cnt in per_tier.items():
        def _rank(item: tuple[str, int]):
            node, n = item
            mission = node.split(" · ")[-1]
            order = _FAST_MISSIONS.index(mission) if mission in _FAST_MISSIONS else 99
            return (-n, order)
        picked: list[str] = []
        # 多样性约束：星球与任务类型都**不重复**。
        # 否则同一颗星球/同一种任务会占满 4 个推荐位（实测中纪会给出
        # 「火星 · Olympus · 中断 / 木星 · Ganymede · 中断 / 天王星 · Ur · 中断」
        # 三个中断），对玩家没有参考价值。
        used_missions: set[str] = set()
        used_planets: set[str] = set()
        for node, _n in sorted(cnt.items(), key=_rank):
            parts = node.split(" · ")
            planet, mission = parts[0], parts[-1]
            if mission in used_missions or planet in used_planets:
                continue
            used_missions.add(mission)
            used_planets.add(planet)
            picked.append(node)
            if len(picked) >= top:
                break
        out[tier] = picked
    _FARM_CACHE = out
    return out
