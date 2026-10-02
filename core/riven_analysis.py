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


# ---------------------------------------------------------------------------
# 卡面原文行解析（2026-09-27）
# ---------------------------------------------------------------------------
# 教训（玩家报障 → 服务器日志实证）：让 vision 模型**直接给出语义词条表**
# 不可靠 —— 4 行卡面 `+120.5% 毒素伤害 / +299.9% 伤害 / +148% 多重射击 /
# -97.2% 触发时间` 被吐成 7 条（负词条那行拆成「触发」「持续」「触发时间」
# 三份，还凭空多一条「滑暴」），词条数校验报「4 正 2 负」把整张卡挡掉。
# 现行分工：模型只负责**逐字照抄卡面文字行**（vision JSON 的 "lines"），
# 归条 / 极性 / 计数由下面两条纯函数确定性决定 —— 只认行首带极性符号的行，
# 卡面上的锁图标、行颜色（白色行）、右下角内融值、武器名与自命名一律不是词条。
_POLARITY = {"+": False, "＋": False, "负": True,
             "-": True, "−": True, "–": True, "—": True, "－": True}
# 词条行 = 极性符号开头（前面只允许装饰性符号：锁图标/圆点/括号/空白）。
# 非装饰性字符（汉字、字母、数字）开头的行**不是**词条行 —— 武器名、自命名、
# 内融值、卡面图例都靠这一条排除；而锁图标与行首那点装饰不能反而把真词条挤掉。
# ★ 2026-10-01：`x` / `×` 也算极性前缀 —— 卡面「对派系伤害」是**乘数写法**
#   （`x1.51 对 Infested 的伤害`），旧版整行被当「无极性符号」丢掉，3+1 的卡
#   被读成 2+1（玩家报障：+91.1%暴伤 / x1.51对Infested / +29.6初始连击 /
#   -115.7%处决 只认了三行，区间系数从 0.9375 错成 1.2375，整卡数值全不吻合）。
_POL_RE = re.compile(r"^[^\w]*([+＋\-−–—－]|负|[x×])\s*(.*)$", re.I)
_NUM_RE = re.compile(r"\d+(?:[.,]\d+)?")
# 全角 % 用 \uff05 转义写：它只在**输入匹配**里用到（永远不渲染到卡面），
# 写成字面量会让「仓库语料」多出一个子集字体没有的字形（test_render_overflow
# 的字库覆盖用例），逼着去重建字体子集。
_BARE_NUM_RE = re.compile(r"^[\d.,]+\s*[%\uff05]?\s*[\w米秒]*$")
# 乘数写法（卡面「x0.55 对 Corpus 的伤害」/「x1.51 对 Infested 的伤害」）
_MULT_RE = re.compile(r"[x×]\s*(\d+(?:[.,]\d+)?)|(\d+(?:[.,]\d+)?)\s*[x×]",
                      re.I)
# 乘数写法还原成 magnitude 的下限：对派系伤害基值 45 × 最小倾向 0.5 ×
# 2正1负的 0.495 ≈ 11 ⇒ 小于 10 的数字一定不是 magnitude，而是「乘数漏写了
# x / 只写了 1.51 或 0.55」，按乘数还原。
_FACTION_MIN_MAG = 10.0


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
    for raw in (lines or []):
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
    """
    pos: list = []
    neg: list = []
    notes: list = []
    for raw in merge_polarity_lines(lines):
        m = _POL_RE.match(raw)
        if not m or not m.group(2).strip():
            notes.append(f"无极性符号：{raw}")
            continue
        neg_flag = _POLARITY.get(m.group(1), False)
        body = m.group(2).strip()
        name = _strip_name(body)
        sid = resolve(name) if name else None
        if not sid:
            notes.append(f"词条名认不出：{raw}")
            continue
        mm = _MULT_RE.search(raw)
        if mm:
            # 乘数在一整行里找（`x` 可能已被上面的极性正则吃掉），极性由
            # 乘数本身决定：x1.51 是正词条、x0.55 是负词条。
            try:
                k = float((mm.group(1) or mm.group(2)).replace(",", "."))
            except ValueError:
                notes.append(f"乘数读不出：{raw}")
                continue
            value, neg_flag = faction_mult_to_mag(k)
        else:
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
        (neg if neg_flag else pos).append((sid, value))
    # 同一条词条不可能既正又负（卡面每行只出现一次）：两侧都在时以负为准
    neg_ids = {sid for sid, _ in neg}
    return (_dedup_pairs([p for p in pos if p[0] not in neg_ids]),
            _dedup_pairs(neg), notes)
