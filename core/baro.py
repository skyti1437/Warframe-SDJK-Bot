# -*- coding: utf-8 -*-
"""Baro Ki'Teer 库存「预测」（数据源：wiki 的历史上架记录）。

⚠️ **这不是官方预测**。DE 从不公布下期库存，本模块做的是**统计推测**：
   把 wiki 记载的历次上架日期整理成「到访日历 + 每件物品何时上过」，
   然后按「距上次上架已经过去多少次到访 / 它自己的平均上架间隔」排序 ——
   也就是**最该回归的候选**。历史上确实常有按固定节奏轮换的物品
   （Prime MOD 那批），但也有完全不规律的，所以只给候选与依据，不打包票。
   卡片上必须写明这是推测，让用户自己判断（项目铁律：推荐类内容要给来源）。

数据由 ``scripts/build_baro_history.py`` 从 ``Module:Baro/data`` 生成。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

DATA_FILE = Path(__file__).resolve().parent / "data" / "baro_history.json"

_DB: Optional[dict] = None
_NAMES: Optional[dict] = None

# 类型 → 中文（Baro 的类型值来自 wiki 的 Type 字段）
TYPE_CN = {
    "Primed Mod (Pistol)": "主要 MOD（手枪）",
    "Primed Mod (Rifle)": "主要 MOD（步枪）",
    "Primed Mod (Shotgun)": "主要 MOD（霰弹枪）",
    "Primed Mod (Melee)": "主要 MOD（近战）",
    "Primed Mod (Archgun)": "主要 MOD（空战枪）",
    "Primed Mod": "主要 MOD",
    "Mod (Pistol)": "MOD（手枪）",
    "Mod (Rifle)": "MOD（步枪）",
    "Mod (Shotgun)": "MOD（霰弹枪）",
    "Mod (Melee)": "MOD（近战）",
    "Mod (Stance)": "MOD（架式）",
    "Mod": "MOD",
    "Weapon": "武器",
    "Void Relic": "虚空遗物",
    "Relic": "遗物",
    "Decoration": "装饰",
    "Glyph": "浮印",
    "Somachord": "音乐片段",
    "Color Palette": "配色",
    "Consumable": "消耗品",
    "Booster": "助燃剂",
    "Bundle": "礼包",
    "Captura Scene": "摄影棚场景",
    "Cosmetic (Armor)": "护甲外观",
    "Cosmetic (Warframe Armor)": "护甲外观",
    "Cosmetic (Warframe Skin)": "战甲外观",
    "Cosmetic (Weapon)": "武器外观",
    "Cosmetic (Weapon Skin)": "武器外观",
    "Cosmetic (Sentinel)": "守护外观",
    "Cosmetic (Operator)": "指挥官外观",
    "Cosmetic (Sigil)": "纹章",
    "Cosmetic (Syandana)": "披饰",
    "Cosmetic (Emblem)": "徽章",
    "Cosmetic (Landing Craft)": "登陆艇外观",
    "Cosmetic (Ephemera)": "幻纹",
    "Cosmetic (Archwing)": "空战外观",
    "Cosmetic (Orbiter)": "轨道飞行器外观",
    "Cosmetic": "外观",
}
# 卡片分组顺序（借鉴社区预测站的分类习惯）
GROUP_ORDER = ("MOD", "武器", "遗物", "装饰", "外观", "其他")


def type_cn(t: str) -> str:
    """类型英文 → 中文（查不到就原样返回，不猜）。"""
    return TYPE_CN.get((t or "").strip(), (t or "").strip())


def group_of(t: str) -> str:
    """类型 → 卡片分组。"""
    t = (t or "").strip()
    if "Mod" in t:
        return "MOD"
    if t == "Weapon":
        return "武器"
    if "Relic" in t:
        return "遗物"
    if t in ("Decoration", "Somachord", "Captura Scene", "Color Palette"):
        return "装饰"
    if t.startswith("Cosmetic"):
        return "外观"
    return "其他"


def names_zh() -> dict:
    """Baro 物品的官方简中名（由 scripts/build_baro_names.py 生成）。"""
    global _NAMES
    if _NAMES is None:
        try:
            _NAMES = json.loads((DATA_FILE.parent / "baro_names_zh.json")
                                .read_text(encoding="utf-8"))
        except Exception:                        # noqa: BLE001
            _NAMES = {}
    return _NAMES
# 不参与轮换的物品（每次都在卖 / 特殊活动项），预测它们没意义
_SKIP_TYPES = ("", "AlwaysAvailable", "ExtraItems")


def load() -> dict:
    global _DB
    if _DB is None:
        try:
            _DB = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        except Exception:                        # noqa: BLE001
            _DB = {}
    return _DB


def visits() -> list[str]:
    """历次到访日期（升序）。"""
    return list(load().get("visits") or [])


def last_visit() -> Optional[str]:
    v = visits()
    return v[-1] if v else None


def next_visit_est() -> Optional[str]:
    """下次到访的**估计**日期（上一次 + period_days，默认 14 天）。

    Baro 是每两周的周五 13:00 UTC 到访、停留 48 小时。这里是纯日期估算，
    精确到小时的排期由 worldstate 的 voidTrader 提供（奸商指令主卡片会给）。
    """
    last = last_visit()
    if not last:
        return None
    period = int(load().get("period_days") or 14)
    try:
        d = datetime.fromisoformat(last).replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return (d + timedelta(days=period)).strftime("%Y-%m-%d")


def predict(limit: int = 12, min_seen: int = 3, active_within: int = 120
            ) -> list[dict]:
    """最可能在下期回归的物品（按「逾期程度」降序）。

    口径（全部可复算，卡片上也会写）：
      · ``gap``    = 历史相邻上架之间的“到访次数”平均值
      · ``silent`` = 距上次上架已过去的到访次数
      · ``score``  = silent / gap —— >1 表示已超过它自己的平均节奏

    ★ 怎么区分「该回归」与「已停售」：只看 score 会把**早就停售**的老物件
      顶到最前（实测排第一的是「均隔 1 次却静默 23 次」的皮肤 —— 那不是
      「快来了」，而是「不会再来了」）。所以再加两条：
        · 停售嫌疑：``silent > 历史最大间隔 × 2`` → 剔除（这类给再高的分也没用）
        · 只要「刚过节奏」的：``score ≤ 3.0``
      过滤后按 score 降序，越接近 1 越说明它按自己的老节奏该来了。

    过滤：至少上架 min_seen 次、且最近 active_within 次到访内活跃过
    （否则会把 2015 年就绝版的老物件排满，没有参考意义）。
    """
    db = load()
    vs = db.get("visits") or []
    items = db.get("items") or {}
    if not vs or not items:
        return []
    now_i = len(vs) - 1
    out: list[dict] = []
    for name, rec in items.items():
        if (rec.get("t") or "") in _SKIP_TYPES:
            continue
        vidx = sorted(set(rec.get("v") or []))
        if len(vidx) < min_seen:
            continue
        last_i = vidx[-1]
        silent = now_i - last_i
        if silent <= 0 or silent > active_within:
            continue
        gaps = [b - a for a, b in zip(vidx, vidx[1:]) if b > a]
        if not gaps:
            continue
        gap = sum(gaps) / len(gaps)
        if gap <= 0:
            continue
        score = silent / gap
        # 停售嫌疑：静默时长是它自己历史最长间隔的两倍以上
        if silent > max(gaps) * 2 and score > 3.0:
            continue
        if score > 3.0:
            continue
        t_en = rec.get("t") or ""
        out.append({
            "name": name,
            "name_cn": names_zh().get(name, ""),
            "type": t_en,
            "type_cn": type_cn(t_en),
            "group": group_of(t_en),
            "ducats": rec.get("d") or 0,
            "credits": rec.get("c") or 0,
            "last": vs[last_i],
            "silent": silent,
            "gap": round(gap, 1),
            "max_gap": max(gaps),      # 历史最长间隔（停售嫌疑的判据，测试要复算）
            "score": round(score, 2),
            "times": len(vidx),
        })
    # 越接近 1 越「按节奏该来」；同分时更常上架的优先
    out.sort(key=lambda r: (abs(r["score"] - 1.0), -r["times"], r["name"]))
    return out[:limit]
