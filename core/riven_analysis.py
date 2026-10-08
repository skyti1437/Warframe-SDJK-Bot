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

import re
from typing import Optional

# 武器类别（WM riven weapons 的 group/rivenType 归一化）→ 基值表列名
_CLASS_KEYS = {
    "rifle": "rifle",
    "shotgun": "shotgun",
    "pistol": "pistol",
    "archgun": "archgun",
    "melee": "melee",
    "zaw": "melee",
    "kitgun": "pistol",
}

# 词条数系数表：{(正词条数, 负词条数): (正系数, 负系数 magnitude)}
FACTOR = {
    (2, 0): (0.99, None),
    (3, 0): (0.75, None),
    (2, 1): (1.2375, 0.495),
    (3, 1): (0.9375, 0.75),
}

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
    "fire_rate": (60.03, 90, 74.7, 60.03, 54.9),  # 近战即攻速
    "attack_speed": (60.03, 90, 74.7, 60.03, 54.9),
    "status_chance": (90, 90, 90, 60.3, 90),
    "status_duration": (99.99, 99.99, 99.99, 99.99, 99.99),
    "range": (None, None, None, None, 1.94),
    "initial_combo": (None, None, None, None, 24.5),
    "combo_duration": (None, None, None, None, 8.1),
    "heavy_attack_efficiency": (None, None, None, None, 73.44),
    # ★ 2026-10-02 补录（用户提供官方 wiki「Riven Mods」基值表整页存档核对）：
    #   同一 combo 家族的两侧 —— 表后 Legend 注明「¹ 仅正向（永不作为负面）」
    #   「³ 仅负向（永不作为正面）」，两行近战基值即下列两项：
    #     Additional Combo Count Chance¹ | Laci/Nus | 近战 58.77%
    #     Chance to Gain Combo Count³   | –/–     | 近战 104.85%
    #   反算自洽（报障卡 海波单剑 3+1：近战伤害 231.2/暴伤 128.8/滑暴 130.2
    #   反推 D≈1.39~1.43，58.77×1.41×0.9375×0.915=71.1 ✓ 命中卡面 +71.1%）。
    "extra_combo_count": (None, None, None, None, 58.77),
    "combo_gain_chance": (None, None, None, None, 104.85),
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
    # 后坐力：幅度 90%（官方 EN 表写 90%、中文表带方向写 -90%）—— 效果方向
    # 由 INVERTED_STATS 表达：卡面「+」= 增加后坐力 = 负面，区间按负档系数算。
    "recoil": (90, 90, 90, 90, None),
    # ★ 2026-10-07：霰弹枪格 None → **41.994**。依据：wiki `Riven_Mods` 页基值表原文
    #   `| [[Zoom]] | Hera || Lis || 59.99% || 41.994% || 80.1% || 59.99% || –`
    #   （列序 = rifle|shotgun|pistol|archgun|melee）；且 Update 44 Hotfix 44.0.3
    #   （wiki 归属 **44.0.2**，原注释误记 44.0.3）补丁说明：**「Shotgun Rivens can now roll Zoom stats」**
    #   （为 Riven Splicer 铺路，见 wiki 同页）⇒ 此前霰弹枪无 Zoom 词条，故为 None。
    #   ⚠ 同一批证据还显示：上游导出镜像（senpai @ 2026-09-29）的霰弹枪家族
    #   `LotusShotgunRandomModRare` **尚未**出现 `WeaponZoomFovMod` 条目 ⇒ 镜像落后
    #   于 44.0.3，等刷新后再复核（含它是否带 NotSentinel 限制）。
    "zoom": (59.99, 41.994, 80.1, 59.99, None),
    # ★ 2026-10-07 Riven Splicer 的 18 个新词条基值（官方 Public Export
    #   `ExportUpgrades_en.json` upgradeValues ×9000；与 wiki「Spliced Values」表 8 处吻合）。
    #   弹药效率官方存 -0.005 ⇒ 取绝对值 45（方向由卡面极性表达）【待验证：实卡定档】。
    #   格挡角度：**实拉导出 upgradeValue = 0.0089999996 ⇒ 满级 0.81**，wiki 写 8.1（差 10 倍）；
    #   wfsim 也撞到同一冲突并选择 wiki（gen_rivens.py:213 WIKI_BASES）⇒ 我方同样取 8.1，
    #   单位=纯数字（无 %）。⚠ 原注释「官方 810」是错的（×9000 误算），已订正。
    "gas_damage": (90, 90, 90, 90, 90),
    "corrosive_damage": (90, 90, 90, 90, 90),
    "viral_damage": (90, 90, 90, 90, 90),
    "radiation_damage": (90, 90, 90, 90, 90),
    "blast_damage": (90, 90, 90, 90, 90),
    "magnetic_damage": (90, 90, 90, 90, 90),
    "damage_vs_orokin": (45, 45, 45, 45, 45),
    "damage_vs_scaldra": (45, 45, 45, 45, 45),
    "damage_vs_techrot": (45, 45, 45, 45, 45),
    "weakpoint_damage": (225, 225, 225, 225, None),
    "weakpoint_crit_chance": (247.5, 247.5, 247.5, 247.5, None),
    "status_damage": (90, 90, 90, 90, 90),
    "ammo_efficiency": (45, 45, 45, 45, None),
    "reload_holstered": (90, 90, 90, None, None),
    "melee_heavy_attack_damage": (None, None, None, None, 119.7),
    "melee_heavy_attack_charge": (None, None, None, None, 119.7),
    "melee_parry_angle": (None, None, None, None, 8.1),
    "melee_slam_damage": (None, None, None, None, 119.7),
}

# 百分比词条（显示时补 %）；其余按原单位（米/秒/纯值）
_PCT_IDS = set(_BASE) - {"punch_through", "range", "combo_duration", "initial_combo"}

# 不在基值表（wiki 尚未给基值）、但单位同样按百分比显示的词条：
# 不补进这个集合的话，「无官方基值」那行会漏掉 %，读起来像绝对值。
# ★ 2026-10-02：原成员 extra_combo_count / combo_gain_chance 已补录基值
#   （见 _BASE），本集合现为空 —— 机制保留，供将来 wiki 未收录的新词条使用。
_PCT_UNIT_ONLY: set = set()


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


# ★ 2026-10-03 晚（用户同卡双态截图实证，推翻本日早间结论）：组合枪紫卡的
#   基值列**不分模式一律手枪列** —— 墓指（主要）实卡（伤害 194.7/多重 103.5/
#   弹匣 50.2）按手枪列×倾向 1.0 反推 U=0.95/0.92/1.07 全部落带，按步枪列
#   反推 U=1.26/1.23 出带（不可能）。kitgun 在 DE 物品系统里 productCategory
#   就是 Pistols；「主要形态用霰弹枪 MOD」说的是普通 MOD 槽，不约束紫卡基值。
#   主/次形态只差**倾向值**（omega/primeOmega，按模式拆分行取）。早间按
#   「霰弹/步枪列」部署的逐腔体映射由此回退（8721dfc 的回归部分）。
_MODE_SUFFIX_RE = re.compile(r"[（(](主要|次要|大气|primary|secondary|atmosphere)[)）]\s*$", re.I)
_MODE_ALIASES = {"主要": "primary", "次要": "secondary", "大气": "atmosphere"}


def mode_of(name: str) -> Optional[str]:
    """武器名（EN 或 ZH 显示名）尾部的模式后缀 → 英文模式词；无则 None。"""
    m = _MODE_SUFFIX_RE.search((name or "").strip())
    if not m:
        return None
    w = m.group(1).lower()
    return _MODE_ALIASES.get(w, w)


def kitgun_mode_class(name: str, base_cls: str, base_riven_type: str) -> str:
    """kitgun 腔体的紫卡基值列：**不分模式一律手枪列**（见上，实卡实证）。

    函数保留签名是因为调用方（riven.py 家族候选三元组）按候选传参；
    非 kitgun 原样返回母行类别。
    """
    if (base_riven_type or "").lower() != "kitgun":
        return base_cls
    return "pistol"


def factor_for(n_pos: int, n_neg: int) -> tuple[float, Optional[float]]:
    """词条数系数：(正词条数, 负词条数) → (正系数, 负系数 magnitude)。"""
    return FACTOR.get((n_pos, n_neg), (0.9375, 0.75))


def stat_range(
    stat_id: str, cls: str, disposition: float, n_pos: int, n_neg: int, *, negative: bool = False
) -> tuple:
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
    return [(sid, float(v), False) for sid, v in (stats_pos or [])] + [
        (sid, float(v), True) for sid, v in (stats_neg or [])
    ]


def _fit_dev(
    entries, cls: str, pos_f: float, neg_f: Optional[float], disp: float, tol_fn
) -> Optional[float]:
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


def disp_feasible(stats_pos, stats_neg, cls, disp: float, *, tol_fn=None) -> bool:
    """给定倾向 disp，判断卡面数值是否落在其 ±10% 区间内（全词条都要过）。"""
    tol_fn = tol_fn or display_tol
    pos_f, neg_f = factor_for(len(stats_pos or []), len(stats_neg or []))
    return (
        _fit_dev(_stat_entries(stats_pos, stats_neg), cls, pos_f, neg_f, float(disp), tol_fn)
        is not None
    )


def match_disposition(stats_pos, stats_neg, cls, candidates, *, tol_fn=None) -> list:
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
    for cand in candidates or []:
        name, d = cand[0], cand[1]
        # ★ 2026-10-03：候选可带自身基值列（kitgun 主要形态=霰弹/步枪列，
        #   与母行的手枪列不同）—— 三元组 (名称, 倾向, 类别)；
        #   二元组沿用整体 cls（向后兼容）。
        cand_cls = cand[2] if len(cand) > 2 else cls
        try:
            d = float(d)
        except (TypeError, ValueError):
            continue
        if d <= 0:
            continue
        if _fit_dev(entries, cand_cls, pos_f, neg_f, d, tol_fn) is not None:
            out.append((name, d))
    return out


NEAR_MISS_MAX = 0.15


def candidate_scores(stats_pos, stats_neg, cls, candidates) -> list:
    """★ 2026-10-03 最近邻判据：给每个候选倾向打「最大相对偏差」分。

    对候选 (名称, D)：``mid_i = 基值_i × D × 系数_i``（即 `stat_range`
    区间的中点，复用现成公式）、``U_i = v_i / mid_i``（卷轴系数，理论
    ∈ [0.9, 1.1]），``score = max_i |U_i − 1|`` —— 越小越像该候选。

    为什么需要它（取证 output/取证-ZCode-组合枪倾向与判据定标-20261003.md
    §五/§六）：严格区间判据（lo ≤ v ≤ hi）是**硬边界**，组合枪双模式
    「wiki 与实机差 1%~5%」的腔体级残差会把只差 1% 的真候选判成不吻合
    （实例：捕月主要 1.1 的多重/切割各差 0.7%/1.1% ⇒ 误报「老卡」）；
    最近邻只比较**候选之间的相对远近**，对绝对偏差免疫。分档：
    score ≤ ~0.10 的候选本就落在严格区间内（`match_disposition` 会先接住，
    与现行判据等价）；0.10 < score ≤ `NEAR_MISS_MAX`（0.15）判该候选但
    卡面标注残差；> 0.15 不判（老卡/识别异常）。

    无可用基值的词条跳过；一条可用基值都没有的候选不计入。

    Returns:
        [(名称, 倾向, score), ...] 按 score 升序；空候选/全无基值返回 []。
    """
    n_pos, n_neg = len(stats_pos or []), len(stats_neg or [])
    out = []
    for cand in candidates or []:
        name, d = cand[0], cand[1]
        # ★ 2026-10-03：三元组候选自带基值列（kitgun 主要形态逐腔体霰弹/
        #   步枪列，与母行手枪列不同）；二元组沿用整体 cls（向后兼容）。
        cand_cls = cand[2] if len(cand) > 2 else cls
        try:
            d = float(d)
        except (TypeError, ValueError):
            continue
        if d <= 0:
            continue
        worst, used = 0.0, 0
        for sid, v, neg in _stat_entries(stats_pos, stats_neg):
            lo, hi = stat_range(sid, cand_cls, d, n_pos, n_neg, negative=neg)
            if lo is None or v <= 0:
                continue
            mid = (lo + hi) / 2
            if mid <= 0:
                continue
            used += 1
            worst = max(worst, abs(v / mid - 1))
        if used:
            out.append((name, d, worst))
    return sorted(out, key=lambda x: x[2])


# ---------------------------------------------------------------------------
# 卡面原文行解析（2026-09-27）
# ---------------------------------------------------------------------------
# 教训（用户报障 → 服务器日志实证）：让 vision 模型**直接给出语义词条表**
# 不可靠 —— 4 行卡面 `+120.5% 毒素伤害 / +299.9% 伤害 / +148% 多重射击 /
# -97.2% 触发时间` 被吐成 7 条（负词条那行拆成「触发」「持续」「触发时间」
# 三份，还凭空多一条「滑暴」），词条数校验报「4 正 2 负」把整张卡挡掉。
# 现行分工：模型只负责**逐字照抄卡面文字行**（vision JSON 的 "lines"），
# 归条 / 极性 / 计数由下面两条纯函数确定性决定 —— 只认行首带极性符号的行，
# 卡面上的锁图标、行颜色（白色行）、右下角内融值、武器名与自命名一律不是词条。
_POLARITY = {
    "+": False,
    "＋": False,
    "负": True,
    "-": True,
    "−": True,
    "–": True,
    "—": True,
    "－": True,
}
# ★ 极性**反转**词条（2026-10-02 用户报障「盗贼 Visi-fevacan」四行全 `+`，其中
#   「+95.4% 武器后坐力」实为负面）：卡面符号与收益方向**相反** —— `+` 是负面、
#   `-` 是正面。依据（双重证据）：
#     ① WM 拍卖 1500 条 / 32 个词条字段实测（读 `item.attributes[].positive`）：
#        **只有 recoil 反转** —— positive=true 的值为负（-9.0/-10.5/-16.1），
#        positive=false 的值为正（+6.6/+5.9/+81.8）；其余 31 个词条（含 zoom）
#        正号占比 1.00、负号占比 1.00 ⇒ 符号即极性。
#     ② 区间反推（不依赖 WM）：盗贼 = Furis（手枪 倾向 1.35）× 3+1 系数下，
#        95.4 按负词条 ∈ [82.01, 100.24] ✅、按正词条 [102.52, 125.3] ❌。
#   ⚠️ 新增条目必须先有实测证据 + 配套断言，禁止凭「词条名像负面」推断。
INVERTED_STATS = {"recoil"}


def is_inverted(sid: str) -> bool:
    """该词条的卡面符号是否与极性相反（`+` 实为负面、`-` 实为正面）。"""
    return sid in INVERTED_STATS


# ★ 「仅负向」词条（2026-10-02，官方 wiki「Riven Mods」基值表 Legend：³ 仅负向、
#   永不作为正面）：「几率来获得连击数」（chance_to_gain_combo_count）——
#   卡面只会出现「-X% 的几率来获得连击数」。
#   锁定的理由：模型/OCR 丢符号是实测过的故障模式（曾把 ±43 后坐力都塞进正面槽），
#   若把该词条读成正数放进正面，会给出一个现实中不存在的正向区间。
#   （对应另一侧「额外连击数几率」= ¹ 仅正向，但正向本就是默认路径、不做强制。）
NEGATIVE_ONLY = {"combo_gain_chance"}


def is_negative_only(sid: str) -> bool:
    """该词条是否只以负面形式出现（卡面恒为 `-`）。"""
    return sid in NEGATIVE_ONLY


# 词条行 = 极性符号开头（前面只允许装饰性符号：锁图标/圆点/括号/空白）。
# 非装饰性字符（汉字、字母、数字）开头的行**不是**词条行 —— 武器名、自命名、
# 内融值、卡面图例都靠这一条排除；而锁图标与行首那点装饰不能反而把真词条挤掉。
# ★ 2026-10-01：`x` / `×` 也算极性前缀 —— 卡面「对派系伤害」是**乘数写法**
#   （`x1.51 对 Infested 的伤害`），旧版整行被当「无极性符号」丢掉，3+1 的卡
#   被读成 2+1（用户报障：+91.1%暴伤 / x1.51对Infested / +29.6初始连击 /
#   -115.7%处决 只认了三行，区间系数从 0.9375 错成 1.2375，整卡数值全不吻合）。
_POL_RE = re.compile(r"^[^\w]*([+＋\-−–—－]|负|[x×])\s*(.*)$", re.I)
_NUM_RE = re.compile(r"\d+(?:[.,]\d+)?")
# 全角 % 用 \uff05 转义写：它只在**输入匹配**里用到（永远不渲染到卡面），
# 写成字面量会让「仓库语料」多出一个子集字体没有的字形（test_render_overflow
# 的字库覆盖用例），逼着去重建字体子集。
_BARE_NUM_RE = re.compile(r"^[\d.,]+\s*[%\uff05]?\s*[\w米秒]*$")
# 乘数写法（卡面「x0.55 对 Corpus 的伤害」/「x1.51 对 Infested 的伤害」）
_MULT_RE = re.compile(r"[x×]\s*(\d+(?:[.,]\d+)?)|(\d+(?:[.,]\d+)?)\s*[x×]", re.I)
# ★ 括号后缀（「暴击几率（重击时 x2）」「射速（弓类武器效果加倍）」）是**卡面说明文案**，
#   不是词条数值。搜乘数前必须先剥掉：2026-10-05 P0 —— 近战紫卡常见的
#   「+211.4% 暴击几率（重击时 x2）」里那个 x2 命中 _MULT_RE，k=2.0 落在
#   is_faction_mult 域内 ⇒ 被当派系乘数换算成 100.0，**211.4 静默丢失**
#   （条数判据仍合法，整卡区间/评级全错）。同类：`+150.0% 连击持续时间 x2`。
_PARA_RE = re.compile(r"[（(][^）)]*[）)]")
# 乘数写法还原成 magnitude 的下限：对派系伤害基值 45 × 最小倾向 0.5 ×
# 2正1负的 0.495 ≈ 11 ⇒ 小于 10 的数字一定不是 magnitude，而是「乘数漏写了
# x / 只写了 1.51 或 0.55」，按乘数还原。
_FACTION_MIN_MAG = 10.0
# ★ 派系乘数（净伤害倍率）的合理取值域：卡面这行是 1 ± 基值45%×倾向×系数
#   ×(0.9~1.1) ⇒ 理论 ≈0.42~1.95，实测样本 0.55/0.63/0.79/0.8/0.83/1.25/1.51。
#   2026-10-02 线上实证：窄读会把词条图标抄成 `×`（「×59% 多重射击」），旧实现
#   一律按乘数换算 ⇒ |1−59|×100 = **5800%**。出界（<0.35 或 >2.05）的 x/×
#   不是乘数 —— 按普通数值读，不做乘法换算。
_FACTION_MULT_LO = 0.35
_FACTION_MULT_HI = 2.05


def is_faction_mult(k: float) -> bool:
    """该数值是否是卡面的派系乘数（净伤害倍率）写法。"""
    return _FACTION_MULT_LO <= k <= _FACTION_MULT_HI


def faction_mult_to_mag(k: float) -> tuple:
    """卡面乘数 k → (magnitude, 是否负词条)。

    卡面「对 X 的伤害」显示的是**净伤害倍率**：x1.51 = +51%（正词条）、
    x0.55 = −45%（负词条）⇒ 极性由乘数本身决定（k>1 加伤、k<1 减伤），
    不能一律按负词条收。
    """
    return (round(abs(1.0 - k) * 100, 2), k < 1.0)


def merge_polarity_lines(lines) -> list:
    """把被拆行的词条行拼回一行（极性符号与数值不在同一行时）。

    两种情形合并（2026-09-27 用户口径）：
      · 纯极性符号行（``+`` / ``-`` / ``负``）→ 与下一行拼成一条；
      · 带极性但没数值的行（``+毒素伤害``）→ 与下一行**纯数值行**拼成一条。
    最多缓存一条待合并片段：宁可让残缺行单独被丢弃，也不把两条词条的名称/
    数值串到一起（串行会让名称与数值交叉配错，比漏读更难发现）。
    """
    out: list = []
    buf = ""
    for raw in lines or []:
        t = str(raw or "").strip()
        if not t:
            continue
        m = _POL_RE.match(t)
        if m:
            body = m.group(2).strip()
            if not body or not _NUM_RE.search(body):
                if buf:
                    out.append(buf)
                buf = t
                continue
            out.append(f"{buf} {t}".strip() if buf else t)
            buf = ""
            continue
        if buf and _BARE_NUM_RE.match(t):
            out.append(f"{buf} {t}")
            buf = ""
            continue
        if buf:
            out.append(buf)
            buf = ""
        out.append(t)
    if buf:
        out.append(buf)
    return out


def _strip_name(body: str) -> str:
    """词条行去掉数值/乘数/单位后剩下的词条名（去空格便于对表）。"""
    name = _MULT_RE.sub(" ", body)
    name = _NUM_RE.sub(" ", name)
    return re.sub(r"[%\uff05x×\s　·、:：米秒]", "", name, flags=re.I)


def _strip_parens(text: str) -> str:
    """剥掉括号后缀（连同括号内容），只留主文案。

    ★ 只用于**搜乘数**，不参与词条名解析：`_strip_name` 对
    「211.4% 暴击几率（重击时 x2）」产出「暴击几率（重击时）」且照旧能 resolve 成
    crit_chance（`_stat_id_from_name` 走子串包含），保持原样以免动到认名链路。
    """
    return _PARA_RE.sub(" ", text or "")


def _dedup_pairs(pairs: list) -> list:
    seen: set = set()
    out: list = []
    for p in pairs:
        if p in seen:
            continue
        seen.add(p)
        out.append(p)
    return out


def parse_riven_lines(lines, resolve) -> tuple:
    """卡面原文行 → (正词条, 负词条, 备注)。

    Args:
        lines: 卡面词条行的原文（vision 模型逐字照抄的结果）。
        resolve: 词条名 → 标准词条 id 的回调（认不出返回 None）。

    Returns:
        (pos, neg, notes)；pos/neg 是 [(stat_id, float), ...]，语义与
        ``main._normalize_llm_stats`` 一致；notes 供日志（哪一行因何被跳过）。

    只认**行首带极性符号**的行：锁图标、行颜色（白色行）、右下角内融值、
    武器名与自命名因为没有极性符号，天然被排除。名称与数值取**同一行**——
    不做跨行配对，跨行配对正是模型把两条词条的名称/数值交叉配错的来源。
    ★ 2026-10-02 两处兼容：① 行首无极性符号时**先试整行乘数**（窄读会把
    「x1.51 对 Infested 的伤害」抄成「对 X 的伤害 x1.51」，x 挪到行尾）；
    ② 反转词条（recoil）按 `INVERTED_STATS` 翻转极性。
    """
    pos: list = []
    neg: list = []
    notes: list = []
    for raw in merge_polarity_lines(lines):
        m = _POL_RE.match(raw)
        if not m or not m.group(2).strip():
            # ★ 2026-10-02 派系行兼容：行首无极性符号时先试整行乘数 ——
            #   兼容 `x1.51 对 X 的伤害` 与 `对 X 的伤害 x1.51` 两种抄写
            #   顺序（线上 16:18 实证：三条派系行因 x 在行尾被整条丢弃）。
            # ★ 2026-10-05 P0：先剥括号后缀再搜乘数，且只认 damage_vs_* 词条 ——
            #   否则「（重击时 x2）」的 x2 会把非派系词条的数值换成 100.0。
            mm2 = _MULT_RE.search(_strip_parens(raw))
            if mm2:
                try:
                    k2 = float((mm2.group(1) or mm2.group(2)).replace(",", "."))
                except ValueError:
                    notes.append(f"乘数读不出：{raw}")
                    continue
                name2 = _strip_name(raw)
                sid2 = resolve(name2) if name2 else None
                if sid2 and sid2.startswith("damage_vs_") and is_faction_mult(k2):
                    value2, neg2 = faction_mult_to_mag(k2)
                    if is_inverted(sid2):
                        neg2 = not neg2
                    (neg if neg2 else pos).append((sid2, value2))
                    continue
                # 出界的 x/×（如「多重射击 ×59」）：不猜数值，保留「无极性符号」
                # 跳过（与旧行为一致）；上面主路径的同类出界按普通数值读。
            notes.append(f"无极性符号：{raw}")
            continue
        neg_flag = _POLARITY.get(m.group(1), False)
        body = m.group(2).strip()
        name = _strip_name(body)
        sid = resolve(name) if name else None
        if not sid:
            notes.append(f"词条名认不出：{raw}")
            continue
        mm = _MULT_RE.search(_strip_parens(raw))
        k = None
        if mm:
            # 乘数在一整行里找（`x` 可能已被上面的极性正则吃掉），极性由
            # 乘数本身决定：x1.51 是正词条、x0.55 是负词条。
            # ★ 2026-10-05 P0：搜的是剥掉括号后缀的文案 —— 括号里的
            #   「重击时 x2」不是乘数，参与匹配会把 211.4 换成 100.0。
            try:
                k = float((mm.group(1) or mm.group(2)).replace(",", "."))
            except ValueError:
                notes.append(f"乘数读不出：{raw}")
                continue
        if k is not None and sid.startswith("damage_vs_") and is_faction_mult(k):
            # ★ 2026-10-05 P0：乘数分支**仅**对 damage_vs_* 启用（与下面
            #   `value < _FACTION_MIN_MAG and sid.startswith("damage_vs_")`
            #   的兜底同口径）；其余词条一律走普通数值分支，禁止拿乘数换算。
            value, neg_flag = faction_mult_to_mag(k)
        else:
            # 普通数值分支。也覆盖「x/× 出界」：窄读把词条图标抄成 × 的行
            # （线上实证「×59% 多重射击」）——出界的 x 不是派系乘数，数字按
            # 普通数值读（旧实现按乘数换算成 |1−59|×100 = 5800%）。
            mn = _NUM_RE.search(body)
            if not mn:
                notes.append(f"无数值：{raw}")
                continue
            try:
                value = float(mn.group(0).replace(",", "."))
            except ValueError:
                notes.append(f"数值读不出：{raw}")
                continue
            if value < _FACTION_MIN_MAG and sid.startswith("damage_vs_"):
                # 乘数漏写 x（或只写了 1.51 / 0.55）：对派系真 magnitude ≥ 11，
                # 按乘数还原并据乘数定极性（旧版一律当负词条 ⇒ 正词条会算错）。
                value, neg_flag = faction_mult_to_mag(value)
        # ★ 2026-10-02：反转词条翻转极性 —— 「+95.4% 武器后坐力」是负面、
        #   「-20% 武器后坐力」是正面（证据见 INVERTED_STATS 注释）；
        #   「仅负向」词条（连击获取）无论读到什么符号都归负面。
        if is_negative_only(sid):
            neg_flag = True
        elif is_inverted(sid):
            neg_flag = not neg_flag
        (neg if neg_flag else pos).append((sid, value))
    # 同一条词条不可能既正又负（卡面每行只出现一次）：两侧都在时以负为准
    neg_ids = {sid for sid, _ in neg}
    return (_dedup_pairs([p for p in pos if p[0] not in neg_ids]), _dedup_pairs(neg), notes)
