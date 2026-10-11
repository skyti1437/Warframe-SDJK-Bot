# -*- coding: utf-8 -*-
"""wfsim 对照 A 批回归（python3 tests/test_wfsim_a_batch.py）

出处：研究仓 docs/wfsim-伤害计算对照-20261009.md §5.3 / §4.2-3 / §5.2 / §7.3
（wfsim github.com/magenie33/wfsim @8f3e6a5，只读核对）。三项都是**确定的高估**：

  #1 含段合计重复：与主段同签名的段又加一遍（134 把）、attacks 段伤害带 `total`
     键致签名去重漏判（Kuva Ayanga 82.8 算两次）、互斥开火方式相加（Komorex 三档）。
     口径对齐 wfsim「每发 = 默认开火方式的直击 → 范围 → 集束各一次」。
  #2 CO（异况超量类）只作用于直击：DoT 种子与范围段都不吃
     （wfsim fight/pellet.rs:394-404、data/weapons/attack.rs:704-708）。
  #3 创口溃烂：SC = 基础 ×[1 + MOD + 创口 ×(连击倍率 − 1)]（wfsim fight/resolve.rs:248-253）。

全部走 dc.parse_args → dc.calculate 真实入口，不联网。

#4（2026-10-10 近战与异常准确性批，wfsim HEAD 8267f09）：初始连击下限、一击必杀进基伤组、
弓/弩射速 MOD ×2、冰冻满层 +1.0、冲击 6s、伤害量化（M57/M80/M81 游戏实测读数）。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import damage_calc as dc  # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  ✓ " if cond else "  ✗ ") + name + ("" if cond else f"  —— {detail}"))
    if not cond:
        FAILED.append(name)


def close(a: float, b: float, tol: float = 1e-6) -> bool:
    return abs(a - b) <= tol * max(1.0, abs(b))


def calc(name: str, args=(), **over) -> tuple[dict, dict]:
    w, _ = dc.find_weapon(name)
    assert w, name
    s, _ = dc.parse_args(list(args))
    s.update(over)
    return w, dc.calculate(s, w)


def _in_total(r: dict) -> list[str]:
    return [m["name"] for m in r["modes"] if m.get("in_total")]


# ---------------------------------------------------------------------------
# #1 含段合计
# ---------------------------------------------------------------------------
print("#1 含段合计：只算默认开火方式一次扳机的各阶段")
_w, _r = calc("Kuva Ogris")
_st = _r["segments_total"]
check(
    "Kuva Ogris：合计 = 主段 + 火箭爆炸（直击段与主段同签名，不再出现第二个主段值）",
    _in_total(_r) == ["Rocket Explosion"] and _st["n_segments"] == 1,
    f"{_in_total(_r)} / {_st}",
)
check(
    "Kuva Ogris：Rocket Impact 仍展示但标为与主段相同、不计入",
    any(
        m["name"] == "Rocket Impact" and m["same_as_main"] and not m.get("in_total")
        for m in _r["modes"]
    ),
)
_card = "\n".join(dc.card_lines(_w, dc.parse_args([])[0], _r, []))
check(
    "Kuva Ogris 卡面合计行不再含「火箭直击」",
    "含段合计" in _card and "火箭直击 " not in _card.split("含段合计")[1].split("\n")[0],
)

_w, _r = calc("Komorex")
check(
    "Komorex：开镜三档（Unzoomed / 2x / 3.5x）不相加，只计默认开火的范围段",
    _in_total(_r) == ["范围伤害"],
    str(_in_total(_r)),
)
check(
    "Komorex：三档仍在卡面展示（不丢信息）",
    {"Unzoomed", "2x Zoom", "3.5x Zoom Mode"} <= {m["name"] for m in _r["modes"]},
)

_w, _r = calc("Kuva Ayanga")
check(
    "Kuva Ayanga：attacks 段带 total 键也能与 wfsim 段同签名去重（82.8 只算一次）",
    len(_r["modes"]) == 1 and _r["segments_total"]["n_segments"] == 1,
    str([(m["name"], round(m["health"], 1)) for m in _r["modes"]]),
)
check(
    "Kuva Ayanga：去重后保留段补上 wfsim 的衰减与半径",
    (_r["modes"][0].get("falloff") or {}).get("reduction") == 0.5
    and _r["modes"][0].get("radius_m"),
    str(_r["modes"][0].get("falloff")),
)
check(
    "签名函数剔除 total 与非伤害类型",
    dc._dmg_sig({"blast": 280.0, "total": 280.0, "shieldDrain": 5, "cold": 0})
    == dc._dmg_sig({"blast": 280.0}),
)

_n_dup = 0
_n_seen = 0
for _wx in dc._load()["weapons"].values():
    _rx = dc.calculate(dc.parse_args([])[0], _wx)
    if any(m.get("same_as_main") for m in _rx["modes"]):
        _n_seen += 1
    if any(m.get("same_as_main") and m.get("in_total") for m in _rx["modes"]):
        _n_dup += 1
check(
    f"全武器：与主段相同的段一律不计入合计（有此类段的武器 {_n_seen} 把，计入 0 把）",
    _n_dup == 0 and _n_seen > 0,
    f"{_n_dup}",
)

_w, _r = calc("拉特昂 Prime", ["灵化"])
check(
    "拉特昂 Prime 灵化：灵化形态的范围段计入",
    _in_total(_r) == ["Incarnon Form AoE"],
    str(_in_total(_r)),
)
_w, _r = calc("拉特昂 Prime")
check("拉特昂 Prime 原型：无灵化段、无合计", not _r["segments_total"], str(_r["segments_total"]))

# ---------------------------------------------------------------------------
# #2 CO 只作用于直击
# ---------------------------------------------------------------------------
print("#2 异况超量类：DoT 与范围段不吃")
_, _r0 = calc("Braton Prime", ["火90"], dmg_per_status=0.0)
_, _r8 = calc("Braton Prime", ["火90"], dmg_per_status=80.0)
check(
    "Braton Prime 火90：CO 0 → 80 时各 DoT 逐值不变（修前切割 84.672 → 282.24）",
    _r0["dots"].keys() == _r8["dots"].keys()
    and all(close(_r0["dots"][k], _r8["dots"][k]) for k in _r0["dots"])
    and close(_r0["dots"]["slash"], 84.672, 1e-4),
    f"{_r0['dots']} vs {_r8['dots']}",
)
check(
    "Braton Prime：稳态 DoT 也不变",
    close(_r0["steady"]["dps_dot"], _r8["steady"]["dps_dot"]),
    f"{_r0['steady']['dps_dot']} vs {_r8['steady']['dps_dot']}",
)
check(
    "Braton Prime：直击照常吃 CO（3 种异常 × 80% ⇒ 单发上升）",
    _r8["health"] > _r0["health"] * 2,
    f"{_r0['health']} → {_r8['health']}",
)
_, _o0 = calc("Kuva Ogris", status_types=3, dmg_per_status=0.0)
_, _o8 = calc("Kuva Ogris", status_types=3, dmg_per_status=80.0)
_seg0 = {m["name"]: m["health"] for m in _o0["modes"]}
_seg8 = {m["name"]: m["health"] for m in _o8["modes"]}
check(
    "Kuva Ogris：范围段（火箭爆炸）CO 0 → 80 不变（修前 150.5 → 511.6）",
    close(_seg0["Rocket Explosion"], _seg8["Rocket Explosion"]),
    f"{_seg0['Rocket Explosion']} → {_seg8['Rocket Explosion']}",
)
check(
    "Kuva Ogris：直击段（火箭直击）照常吃 CO",
    _seg8["Rocket Impact"] > _seg0["Rocket Impact"] * 2,
    f"{_seg0['Rocket Impact']} → {_seg8['Rocket Impact']}",
)

# ---------------------------------------------------------------------------
# #3 创口溃烂
# ---------------------------------------------------------------------------
print("#3 创口溃烂：SC = 基础 ×[1 + MOD + 创口 ×(连击倍率 − 1)]")
_w, _r = calc("Skana", status_chance=90.0, status_per_combo=40.0, combo_hits=220)
_ratio = _r["steady"]["status_chance"] / _w["procChance"]
check("12 层、MOD +90%、创口 +40% ⇒ ×6.3（修前 ×11.02）", close(_ratio, 6.3), str(_ratio))
_w, _r = calc("Skana", status_chance=90.0, status_per_combo=40.0, combo_hits=0)
check(
    "连击 0（倍率 1.0）时创口不加成 ⇒ ×1.9",
    close(_r["steady"]["status_chance"] / _w["procChance"], 1.9),
)

# ---------------------------------------------------------------------------
# #4 近战与异常准确性（2026-10-10）：初始连击下限 / 一击必杀进基伤组 / 弓弩射速 MOD ×2 /
#    冰冻满层 / 冲击 6s / 伤害量化
# ---------------------------------------------------------------------------
print("#4a 初始连击是下限：eff = max(连击, 初始连击)（wfsim fight/melee.rs:50-61）")
_w, _r = calc("Kronen Prime", heavy=True, combo_hits=40, initial_combo=30.0)
check(
    "Kronen Prime 连击40 + 邪恶蓄力30：重击读 40 连击 ×3（修前相加 70 ×4）",
    _r["heavy"]["combo_hits"] == 40 and _r["heavy"]["multiplier"] == 3.0,
    str((_r["heavy"]["combo_hits"], _r["heavy"]["multiplier"])),
)
_w, _r = calc("Kronen Prime", heavy=True, combo_hits=10, initial_combo=30.0)
check("连击10 < 初始30 ⇒ 取 30（×2）", _r["heavy"]["combo_hits"] == 30)
_w, _r = calc("Galatine Prime", crit_per_combo=40.0, combo_hits=30, initial_combo=30.0)
check(
    "狂怒：连击30 + 初始30 ⇒ 倍率 2 ⇒ 暴率 = 基础×1.4（修前倍率 4 ⇒ ×2.2）",
    close(_r["crit_cc"], _w["criticalChance"] * 1.4),
    f"{_r['crit_cc']} vs {_w['criticalChance'] * 1.4}",
)
_w, _r = calc("Skana", status_per_combo=40.0, combo_hits=0, initial_combo=30.0)
check(
    "创口溃烂：连击0 + 初始30 ⇒ 倍率 2 ⇒ 触发 = 基础×1.4",
    close(_r["steady"]["status_chance"] / _w["procChance"], 1.4),
)
_w, _r = calc("Kronen Prime", ["邪恶蓄力", "重击", "连击40"])
check(
    "卡面注明「初始连击是下限」",
    any("初始连击 30 是下限" in ln for ln in dc.card_lines(_w, dc.parse_args([])[0], _r)),
)

print(
    "#4b 一击必杀进基伤加法组（wiki Killing_Blow:29；wfsim pellet.rs:368 / scale.rs gunco_bucket）"
)
# 无甲（基甲0）⇒ 线性；量化对基伤组是纯倍率、不改比值
_, _h0 = calc("Kronen Prime", heavy=True, base_dmg=120.0, base_armor=0.0)
_, _h1 = calc("Kronen Prime", heavy=True, base_dmg=120.0, heavy_dmg=120.0, base_armor=0.0)
_rk = _h1["heavy"]["health"] / _h0["heavy"]["health"]
check(
    "压迫点+120、一击必杀+120：重击 ×(1+1.2+1.2)/(1+1.2)=1.5455（修前独立 ×2.2）",
    close(_rk, 3.4 / 2.2),
    str(_rk),
)
check("普通攻击不吃一击必杀", close(_h1["health"], _h0["health"]))
# CO：additive ⇒ 同括号；independent ⇒ (1+基伤+重击)×(1+CO)（直接验 _hit_from_damage 的括号）
_d = {"slash": 100.0, "total": 100.0}
_sp, _ = dc.parse_args([])


def _hit(co_mode: str, bd: float) -> float:
    _s = {**_sp, "base_dmg": bd, "dmg_per_status": 60.0}
    pt, _, _, _ = dc._hit_from_damage(
        _d, 100.0, _s, {}, "Grineer", 0.0, 1.0, 1.0, n_types=2, co_mode=co_mode
    )
    return sum(pt.values())


check(
    "additive：基伤100+重击100+CO120 同括号 ⇒ 100×4.2",
    close(_hit("additive_with_base_damage", 200.0), 420.0),
)
check(
    "independent：(1+1+1)×(1+1.2) ⇒ 100×6.6",
    close(_hit("independent", 200.0), 660.0),
)
# 有甲时重击按自己的构成重算：护甲后「每类型至少 1 点」不再被等比放大（Dual Zoren 穿刺 3.5）
_w, _r = calc("Dual Zoren", heavy=True, heavy_dmg=120.0)
_k = _w["heavyAttackDamage"] / _w["damage"]["total"]
_raw = {
    k: float(v) * _k * 2.2
    for k, v in _w["damage"].items()
    if k in ("impact", "puncture", "slash") and v
}
_q = dc.quantize_damage(_raw, _w["heavyAttackDamage"] * 2.2)
_ft = dc._load()["fac_table"]
_exp = sum(max(v * _ft.get(k, {}).get("Grineer", 1.0) * (1 - _r["dr"]), 1.0) for k, v in _q.items())
check(
    "Dual Zoren 一击必杀重击 = Σ max(量化类型×派系×(1−DR), 1)（修前按普通对血缩放偏高 1.5%）",
    close(_r["heavy"]["health"], _exp),
    f"{_r['heavy']['health']} vs {_exp}",
)

print("#4c 弓/弩射速 MOD ×2（卡面「x2 for Bows」；wfsim panel.rs:545-548、resolve.rs:534）")
_w, _r = calc("Paris Prime", ["灵敏扳机"])
check(
    "Paris Prime 灵敏扳机 +60% ⇒ 1 × 2.2（修前 1.6）",
    close(_r["fire_rate"], 2.2),
    str(_r["fire_rate"]),
)
_w, _r = calc("Zhuge Prime", ["灵敏扳机"])
check("弩 Zhuge Prime ⇒ 5.5 × 2.2", close(_r["fire_rate"], _w["fireRate"] * 2.2))
_w, _r = calc("Paris Prime", ["关键延迟"])
check("负射速卡同样翻倍：关键延迟 −20% ⇒ ×0.6", close(_r["fire_rate"], _w["fireRate"] * 0.6))
_w, _r = calc("Braton Prime", ["灵敏扳机"])
check("步枪不翻倍：Braton Prime ×1.6", close(_r["fire_rate"], _w["fireRate"] * 1.6))
_w, _r = calc("Ballistica", fire_rate_pct=60.0, fire_rate_mod_pct=60.0)
check("路径含 Crossbow 的手枪 Ballistica 不翻倍", close(_r["fire_rate"], _w["fireRate"] * 1.6))
_w, _r = calc("Paris Prime", fire_rate_pct=60.0)
check("弓上的非 MOD 卡来源（赋能/镀层）不翻倍", close(_r["fire_rate"], 1.6))
from core import loadout_ocr as _lo  # noqa: E402

_wp, _ = dc.find_weapon("Paris Prime")
_ls = _lo.to_damage_spec({"weapon": _wp, "totals": {"fire_rate": 60.0}})
check(
    "识卡路径：MOD 库值射速记为 MOD 部分 ⇒ 弓同样翻倍",
    close(_ls["fire_rate_mod_pct"], 60.0) and close(dc.calculate(_ls, _wp)["fire_rate"], 2.2),
)

print("#4d 冰冻满层（wiki Damage/Cold_Damage；wfsim data/debuffs/frozen.yaml:20-24）")


def _cold_ss(rate: float) -> dict:
    return dc.steady_state_dps(
        {"cold": 100.0},
        100.0,
        1.0,
        {},
        "Grineer",
        rate,
        1.0,
        0.5,
        2.0,
        3.0,
        1.0,
        1.0,
        {"_armor": 0.0},
    )


_c = _cold_ss(2.0)  # 12 层量级 ⇒ 冻结循环：3s(+1.0) + 7/2s 爬升(3..9 层阶梯和 2.45)
check(
    "r=2/s：暴伤加成 = (3×1.0 + 2.45/2)/(3 + 3.5) = 0.65、冻结占 3/6.5（修前封顶 0.5）",
    close(_c["cm_bonus"], 0.65) and close(_c["frozen_share"], 3 / 6.5),
    str((_c["cm_bonus"], _c["frozen_share"])),
)
check("冻结占比随冰触发率升高趋近 +1.0", 0.95 < _cold_ss(100.0)["cm_bonus"] < 1.0)
_c = _cold_ss(1.5)  # 9 层：未满 10 层不冻结，阶梯 +0.5
check("9 层未冻结：仍按阶梯 +0.5", close(_c["cm_bonus"], 0.5) and _c["frozen_share"] == 0.0)
check("1 层：+0.1", close(_cold_ss(1 / 6)["cm_bonus"], 0.1))

print("#4e 冲击 6s（wiki Damage/Impact_Damage；wfsim stagger.yaml:19）——不参与数值")
check("STATUS_TABLE impact dur = 6", dc.STATUS_TABLE["impact"]["dur"] == 6.0)
_w, _r6 = calc("Kronen Prime")
dc.STATUS_TABLE["impact"]["dur"] = 1.0
try:
    _w, _r1 = calc("Kronen Prime")
finally:
    dc.STATUS_TABLE["impact"]["dur"] = 6.0
check("冲击时长 1 → 6 稳态 DPS 不变", close(_r6["steady"]["dps_total"], _r1["steady"]["dps_total"]))

print("#4f 伤害量化：各类型取整到 ModdedBase/32（wiki Damage/Calculation；wfsim damage.rs:280）")
_B = {"impact": 1.75, "puncture": 12.25, "slash": 21.0, "total": 35.0}  # Braton Prime 35


def _sum(dmg: dict, n_types: int = 0, co_mode: str = "additive_with_base_damage", **kw) -> float:
    _s = {**dc.parse_args([])[0], **kw}
    pt, _, _, _ = dc._hit_from_damage(
        dmg, dmg["total"], _s, {}, "Grineer", 0.0, 1.0, 1.0, n_types=n_types, co_mode=co_mode
    )
    return sum(pt.values())


check(
    "wiki 例 30/30/40 无 MOD ⇒ 103.125",
    close(_sum({"impact": 30.0, "puncture": 30.0, "slash": 40.0, "total": 100.0}), 103.125),
)
for _el, _meas, _exp in (
    ({"toxin": 90.0, "heat": 90.0}, 98, 98.4375),
    ({"corrosive": 200.0}, 105, 105.0),
    ({"gas": 200.0, "toxin": 90.0}, 137, 136.71875),
    ({"blast": 200.0, "cold": 90.0, "heat": 90.0}, 168, 168.4375),
):
    _v = _sum(_B, singles=_el)
    check(f"M57 Braton Prime {_el} ⇒ {_exp}（游戏读数 {_meas}）", close(_v, _exp), str(_v))
_BP = {"impact": 7.75, "slash": 62.0, "puncture": 85.25, "total": 155.0}  # Ballistica Prime 蓄力
_v = _sum(_BP, base_dmg=220.0, singles={"electricity": 165.0, "cold": 90.0})
check("M80 基伤220+电165+冰90 ⇒ 1782.5（游戏 1783；量化前 1760.8）", close(_v, 1782.5), str(_v))
_v = _sum(
    _BP,
    base_dmg=220.0,
    singles={"electricity": 165.0, "cold": 90.0},
    physical={"slash": 120.0, "puncture": 120.0},
)
check("M80 再加切割/穿刺+120（物理单项不进分母）⇒ 2340.5（游戏 2341）", close(_v, 2340.5), str(_v))
_v = _sum(
    _BP,
    base_dmg=220.0,
    singles={"electricity": 165.0, "cold": 90.0},
    physical={"slash": 120.0},
    dmg_per_status=100 * 32 / 155 * 3,
    n_types=2,
)
check("M80 加法型 CO 进分母（3–2 档）⇒ 2795（游戏 2795）", close(_v, 2795.0), str(_v))
_S = {"slash": 833.0, "total": 833.0}
_v = _sum(
    _S,
    base_dmg=220.0,
    singles={"electricity": 165.0, "cold": 90.0},
    physical={"slash": 120.0},
    dmg_per_status=120.0,
    n_types=1,
    co_mode="independent",
)
check("M81 独立型 CO 在量化之后乘（3–1 档）⇒ 27855.52（游戏 27856）", close(_v, 27855.52), str(_v))

print()
if FAILED:
    print(f"✗ {len(FAILED)} 项失败：" + "、".join(FAILED))
    sys.exit(1)
print("✓ 全部通过")
