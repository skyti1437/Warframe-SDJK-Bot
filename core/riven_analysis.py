# -*- coding: utf-8 -*-
"""紫卡属性数值分析（「紫卡分析」指令的计算层）。

DE 的紫卡属性数值不是随机的随便一个数，而是有确定区间（wiki「Riven Mods」页
Attribute Value Formula / Base Values 两节）：

    数值 = 属性基值 × 武器倾向 D × 词条数系数 × 随机系数 U

    * U ∈ [0.9, 1.1] —— 同一把武器同词条的 rolled 区间就是基值×D×系数 的 ±10%；
    * 词条数系数（正词条 / 负词条 magnitude）：
        2正0负  +0.99
        3正0负  +0.75
        2正1负  +1.2375 / 负 0.495
        3正1负  +0.9375 / 负 0.75

基值表按武器类别（Rifle/Shotgun/Pistol/Archgun/Melee）各一列。数据为静态
游戏机制（DE 上次改动这些基值是很久以前），不存在过期问题。
"""
from __future__ import annotations

from typing import Optional

# 武器类别（WM riven weapons 的 group/rivenType 归一化）→ 基值表列名
_CLASS_KEYS = {"rifle": "rifle", "shotgun": "shotgun", "pistol": "pistol",
               "archgun": "archgun", "melee": "melee", "zaw": "melee",
               "kitgun": "pistol"}

# 词条数系数表：{(正词条数, 负词条数): (正系数, 负系数 magnitude)}
FACTOR = {(2, 0): (0.99, None), (3, 0): (0.75, None),
          (2, 1): (1.2375, 0.495), (3, 1): (0.9375, 0.75)}

# 属性基值表（wiki Base Values 表的静态转录）。
# 键 = 插件标准词条 id（与 parser.RIVEN_STAT_ZH 同一套）；
# 值 = (rifle, shotgun, pistol, archgun, melee)，None = 该类别无此词条。
# 单位：除 punch_through/range（米）、combo_duration（秒）、initial_combo（整数）外
# 都是百分比，但数值本身用 wiki 表的原值（如 90 表示 +90%）。
_BASE: dict[str, tuple] = {
    "melee_damage": (165, 164.7, 219.6, 99.9, 164.7),
    "crit_chance": (149.99, 90, 149.99, 99.9, 180),
    "crit_damage": (120, 90, 90, 80.1, 90),
    "multishot": (90, 119.7, 119.7, 60.3, None),
    "fire_rate": (60.03, 90, 74.7, 60.03, 54.9),   # 近战即攻速
    "attack_speed": (60.03, 90, 74.7, 60.03, 54.9),
    "status_chance": (90, 90, 90, 60.3, 90),
    "status_duration": (99.99, 99.99, 99.99, 99.99, 99.99),
    "range": (None, None, None, None, 1.94),
    "initial_combo": (None, None, None, None, 24.5),
    "combo_duration": (None, None, None, None, 8.1),
    "heavy_attack_efficiency": (None, None, None, None, 73.44),
    "combo_efficiency": (None, None, None, None, 58.77),
    "finisher_damage": (None, None, None, None, 119.7),
    "slide_crit": (None, None, None, None, 120),
    "slash_damage": (119.97, 119.97, 119.97, 90, 119.7),
    "impact_damage": (119.97, 119.97, 119.97, 90, 119.7),
    "puncture_damage": (119.97, 119.97, 119.97, 90, 119.7),
    "heat_damage": (90, 90, 90, 119.7, 90),
    "cold_damage": (90, 90, 90, 119.7, 90),
    "toxin_damage": (90, 90, 90, 119.7, 90),
    "electric_damage": (90, 90, 90, 119.7, 90),
    # wiki Base Values：对派系伤害 = 45（百分比）。曾误记为 0.45（少 100 倍），
    # 导致带该词条的卡永远「不吻合」（2026-09-17 用户截图 冰凇 x0.55 案例）
    "damage_vs_grineer": (45, 45, 45, 45, 45),
    "damage_vs_corpus": (45, 45, 45, 45, 45),
    "damage_vs_infested": (45, 45, 45, 45, 45),
    "magazine_capacity": (50, 50, 50, 60.3, None),
    "ammo_max": (49.95, 90, 90, 99.9, None),
    "projectile_speed": (90, 90, 90, None, None),
    "punch_through": (2.7, 2.7, 2.7, 2.7, None),
    "reload_speed": (50, 50, 50, 99.9, None),
    "recoil": (90, 90, 90, 90, None),
    "zoom": (59.99, None, 80.1, 59.99, None),
}

# 百分比词条（显示时补 %）；其余按原单位（米/秒/纯值）
_PCT_IDS = set(_BASE) - {"punch_through", "range", "combo_duration",
                         "initial_combo"}

# 不在基值表（wiki 尚未给基值）、但单位同样按百分比显示的词条：
# 不补进这个集合的话，「暂无基值数据」那行会漏掉 %，读起来像绝对值。
_PCT_UNIT_ONLY = {"extra_combo_count", "combo_gain_chance"}


_CLASS_IDX = {"rifle": 0, "shotgun": 1, "pistol": 2, "archgun": 3, "melee": 4}


def weapon_class(riven_type: str = "", group: str = "") -> Optional[str]:
    """WM 紫卡武器的 rivenType/group → 基值表列名。

    守护(Robotic)武器按 wiki 规则各自沿用关联类别（Sweeper=霰弹枪 等），
    无法一概而论，这里回落步枪并在卡面注明近似。

    ★ 2026-09-24：**先看 group 再看 rivenType**——WM 的 rivenType 只记
    「MOD 适用类别」，曲翼枪械（翠雀 Larkspur / 凯旋将军 Imperator 等）
    在 WM 数据里 rivenType 也是 ``rifle``，先看 rivenType 会拿步枪基值
    算曲翼枪械（暴伤基值 120 vs 80.1，差 50%）。
    """
    for key in (group, riven_type):
        k = (key or "").lower()
        for name, col in _CLASS_KEYS.items():
            if name in k:
                return col
    return "rifle"


def factor_for(n_pos: int, n_neg: int) -> tuple[float, Optional[float]]:
    """词条数系数：(正词条数, 负词条数) → (正系数, 负系数 magnitude)。"""
    return FACTOR.get((n_pos, n_neg), (0.9375, 0.75))


def stat_range(stat_id: str, cls: str, disposition: float,
               n_pos: int, n_neg: int, *, negative: bool = False) -> tuple:
    """单词条的取值区间 (min, max)。

    Args:
        stat_id: 插件标准词条 id。
        cls: 武器基值列名（weapon_class 的产物）。
        disposition: 武器倾向数值（如 1.25）。
        n_pos / n_neg: 正/负词条数量（决定系数）。
        negative: 该词条是否为负词条。

    Returns:
        (min, max)；负词条返回的是 magnitude 的区间（显示时再补负号）。
        基值缺失返回 (None, None)。
    """
    base = _BASE.get(stat_id)
    if not base:
        return (None, None)
    idx = {"rifle": 0, "shotgun": 1, "pistol": 2, "archgun": 3, "melee": 4}[cls]
    b = base[idx]
    if b is None:
        return (None, None)
    pos_f, neg_f = factor_for(n_pos, n_neg)
    f = neg_f if negative else pos_f
    mid = b * disposition * f
    return (round(mid * 0.9, 2), round(mid * 1.1, 2))


def deviation_pct(value: float, lo: float, hi: float) -> float:
    """数值偏离区间中值的百分比（正=高卷，负=低卷）。"""
    if not lo or not hi:
        return 0.0
    mid = (lo + hi) / 2
    return round((value - mid) / mid * 100, 1)


def range_position(value: float, lo: float, hi: float) -> int:
    """数值在区间里的位置百分位（0=贴下限，100=贴上限）。"""
    if not lo or not hi or hi <= lo:
        return 50
    return max(0, min(100, round((value - lo) / (hi - lo) * 100)))


def fmt_value(stat_id: str, v: float) -> str:
    """按词条单位显示数值（符号由调用方处理）。"""
    if stat_id in ("punch_through", "range"):
        return f"{v:g}m"
    if stat_id == "combo_duration":
        return f"{v:g}s"
    if stat_id in _PCT_IDS or stat_id in _PCT_UNIT_ONLY:
        return f"{v:g}%"
    return f"{v:g}"


# ---------------------------------------------------------------------------
# 数值反推倾向（变体自动判定）
#
# 公式可以反着用：v = 基值 × D × 词条系数 × U（U ∈ [0.9, 1.1]）
#   =>  D ∈ [v/(基值·系数·1.1), v/(基值·系数·0.9)]
# 每个词条都给出一个 D 区间，取交集即这张卡唯一可能吃的倾向。游戏内紫卡
# 卡面**只写母武器名**（变体信息根本不在截图里），但数值一定落在某个倾向
# 的 ±10% 区间内 —— 于是「是不是变体」可以从数值判定出来，无需手输。
# ---------------------------------------------------------------------------

# 卡面显示精度：词条普遍 1 位小数（±0.05 的不确定度），
# initial_combo 显示整数（±0.5）。反解时把这点舍入余量算进去，
# 否则「+1.6m」这种本身就有约 3% 不确定度的数值会把结论卡死。
_DISPLAY_TOL = 0.05


def _base_value(stat_id: str, cls: str) -> Optional[float]:
    """属性基值（按类别列）；该类别无此词条时返回 None。"""
    base = _BASE.get(stat_id)
    idx = _CLASS_IDX.get(cls)
    if not base or idx is None:
        return None
    return base[idx]


def display_tol(stat_id: str, value: float) -> float:
    """卡面显示舍入带来的取值不确定度（± 半个末位）。

    Args:
        stat_id: 插件标准词条 id。
        value: 卡面显示值（magnitude）。

    Returns:
        该数值的不确定度绝对值（1 位小数 → 0.05；initial_combo → 0.5）。
    """
    if stat_id == "initial_combo":
        return 0.5
    return _DISPLAY_TOL


def _stat_entries(stats_pos, stats_neg) -> list:
    """词条扁平化 → [(stat_id, value, is_negative)]（值一律为 magnitude）。"""
    return ([(sid, float(v), False) for sid, v in (stats_pos or [])]
            + [(sid, float(v), True) for sid, v in (stats_neg or [])])


def _fit_dev(entries, cls: str, pos_f: float, neg_f: Optional[float],
             disp: float, tol_fn) -> Optional[float]:
    """候选倾向能否解释全部词条：能则返回最大 |U-1|（越小越居中），否则 None。

    Args:
        entries: _stat_entries 的产物。
        cls: 基值列名。
        pos_f / neg_f: 正/负词条系数。
        disp: 候选倾向。
        tol_fn: (stat_id, value) → 显示不确定度。

    Returns:
        可行时返回各词条 U 偏离 1.0 的最大值；有词条超出区间返回 None。
    """
    worst, used = 0.0, 0
    for sid, v, neg in entries:
        b = _base_value(sid, cls)
        f = neg_f if neg else pos_f
        if not b or not f or v <= 0 or disp <= 0:
            continue
        t = tol_fn(sid, v)
        denom = b * f * disp
        # 该词条反推的 U 区间必须与 [0.9, 1.1] 有交集
        if (v + t) / denom < 0.9 or (v - t) / denom > 1.1:
            return None
        worst = max(worst, abs(v / denom - 1.0))
        used += 1
    return worst if used else None


def disposition_interval(stats_pos, stats_neg, cls, *, tol_fn=None) -> tuple:
    """由卡面数值反解倾向的可行区间。

    Args:
        stats_pos / stats_neg: [(stat_id, value)]，负词条存 magnitude。
        cls: 武器基值列名（weapon_class 的产物）。
        tol_fn: 可选的显示不确定度函数（测试用）。

    Returns:
        (lo, hi) 可行区间；词条全都无可用基值、或各词条互相矛盾
        （交集为空）时返回 (None, None)。
    """
    tol_fn = tol_fn or display_tol
    pos_f, neg_f = factor_for(len(stats_pos or []), len(stats_neg or []))
    lo, hi, used = 0.0, 1e9, 0
    for sid, v, neg in _stat_entries(stats_pos, stats_neg):
        b = _base_value(sid, cls)
        f = neg_f if neg else pos_f
        if not b or not f or v <= 0:
            continue
        t = tol_fn(sid, v)
        lo = max(lo, (v - t) / (b * f * 1.1))
        hi = min(hi, (v + t) / (b * f * 0.9))
        used += 1
    if not used or hi < lo:
        return (None, None)
    return (round(lo, 4), round(hi, 4))


def disp_feasible(stats_pos, stats_neg, cls, disp: float,
                  *, tol_fn=None) -> bool:
    """给定倾向 disp，判断卡面数值是否落在其 ±10% 区间内（全词条都要过）。"""
    tol_fn = tol_fn or display_tol
    pos_f, neg_f = factor_for(len(stats_pos or []), len(stats_neg or []))
    return _fit_dev(_stat_entries(stats_pos, stats_neg), cls, pos_f, neg_f,
                    float(disp), tol_fn) is not None


def match_disposition(stats_pos, stats_neg, cls, candidates,
                      *, tol_fn=None) -> list:
    """在候选 [(名称, 倾向)] 中筛出与卡面数值吻合的项（保持原顺序）。

    变体名不在截图里，但数值一定落在某个倾向的 ±10% 区间内；家族内
    （母武器 + 棱晶/Prime/亡魂…）通常只有一个候选能解释全部词条，
    据此即可自动判定该按谁的倾向计算。

    Args:
        candidates: [(显示名, 倾向值), ...]。

    Returns:
        [(显示名, 倾向值), ...] —— 可行的子集；无可行项返回 []。
    """
    tol_fn = tol_fn or display_tol
    entries = _stat_entries(stats_pos, stats_neg)
    pos_f, neg_f = factor_for(len(stats_pos or []), len(stats_neg or []))
    out = []
    for name, d in (candidates or []):
        try:
            d = float(d)
        except (TypeError, ValueError):
            continue
        if d <= 0:
            continue
        if _fit_dev(entries, cls, pos_f, neg_f, d, tol_fn) is not None:
            out.append((name, d))
    return out
