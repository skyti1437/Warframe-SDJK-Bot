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

print()
if FAILED:
    print(f"✗ {len(FAILED)} 项失败：" + "、".join(FAILED))
    sys.exit(1)
print("✓ 全部通过")
