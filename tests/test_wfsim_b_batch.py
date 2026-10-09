# -*- coding: utf-8 -*-
"""wfsim 对照 B 批回归（python3 tests/test_wfsim_b_batch.py）

出处：研究仓 docs/wfsim-伤害计算对照-20261009.md §4.3 / §3.2 / §4.2 / §3.3
（wfsim github.com/magenie33/wfsim @8f3e6a5，只读核对）。口径由用户 2026-10-09 拍板：

  B1 DoT 乘本元素 MOD 括号：火 / 毒 / 电 ×(1+该元素 MOD%)，切割与毒气固定 1.0
     （wfsim fight/procs.rs elem_bracket；build/loadout/resolve.rs:569-574 按 MOD 自身元素累加）。
  B2 爆头：人形头部 ×3（wiki Enemy_Body_Parts / wfsim MECHANICS.md:1001-1003），卡面注明口径；
     「加成放大整个部位倍率」「暴击爆头暴伤翻倍」暂缓（等敌人分类表）。
  B3 CO 按 co_behavior 三类：additive 并进基伤括号 / independent 独立乘区 / inert 无效；
     异常种类数计入 MOD 加成的元素。
  #6 元素合成顺序：只加卡面口径说明，不改算法。

全部走 dc.parse_args → dc.calculate 真实入口，不联网。
"""

from __future__ import annotations

import json
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


# ---------------------------------------------------------------------------
print("B1 DoT 乘本元素 MOD 括号")
_, _r0 = calc("Braton Prime", ["火90"])
_dots_ref = {"slash": 84.672, "heat": 26.492 * 1.9}
check(
    "Braton Prime 火90：火 DoT ×1.9（26.492 → 50.335），切割不变 84.672",
    close(_r0["dots"]["heat"], _dots_ref["heat"], 1e-4)
    and close(_r0["dots"]["slash"], 84.672, 1e-4),
    str(_r0["dots"]),
)
_, _rt = calc("Braton Prime", ["毒90"])
_, _re = calc("Braton Prime", ["电90"])
check(
    "毒90 / 电90 同样 ×1.9",
    close(_rt["dots"]["toxin"], _dots_ref["heat"], 1e-4)
    and close(_re["dots"]["electricity"], _dots_ref["heat"], 1e-4),
    f"{_rt['dots']} / {_re['dots']}",
)
_, _rg = calc("Braton Prime", ["火90", "毒90"])
check(
    "火90+毒90 合成毒气：毒气 DoT 固定 1.0（火 / 毒 MOD 不进毒气括号）",
    close(_rg["dots"]["gas"], 26.492, 1e-4),
    str(_rg["dots"]),
)
check(
    "_dot_elem_bracket：切割 / 毒气恒 1.0，火 +90% → 1.9",
    dc._dot_elem_bracket("slash", {"singles": {"slash": 90}}) == 1.0
    and dc._dot_elem_bracket("gas", {"singles": {"gas": 90}}) == 1.0
    and close(dc._dot_elem_bracket("heat", {"singles": {"heat": 90}}), 1.9),
)
_, _rs0 = calc("Braton Prime")
check(
    "稳态 DoT 同口径：火90 后稳态 DoT 上升",
    _r0["steady"]["dps_dot"] > _rs0["steady"]["dps_dot"],
    f"{_rs0['steady']['dps_dot']} → {_r0['steady']['dps_dot']}",
)

# ---------------------------------------------------------------------------
print("B2 爆头：人形 ×3")
check("HEADSHOT_BASE = 3.0", dc.HEADSHOT_BASE == 3.0)
_w, _r = calc("Braton Prime", ["爆头"])
check("不指定敌人：爆头倍率 3.0", close(_r["head_mult"], 3.0), str(_r["head_mult"]))
_lines = dc.card_lines(_w, dc.parse_args(["爆头"])[0], _r, [])
check(
    "卡面注明爆头口径来源",
    any("爆头口径" in ln and "人形头部 ×3" in ln for ln in _lines),
    str([ln for ln in _lines if "爆头" in ln]),
)
_, _r = calc("Braton Prime", ["对", "重型机枪手", "爆头"])
check(
    "重型机枪手（riven-mirror 通用值 2 = 未分类）按人形 3.0",
    close(_r["head_mult"], 3.0),
    str(_r["head_mult"]),
)
_, _r = calc("Braton Prime", ["对", "Legacyte", "爆头"])
check("Legacyte（wfsim 无头部）→ 1.0", close(_r["head_mult"], 1.0), str(_r["head_mult"]))
_, _r = calc("Braton Prime", ["对", "Eidolon Teralyst", "爆头"])
check("夜灵（head_mul 1.0）不吃爆头", close(_r["head_mult"], 1.0), str(_r["head_mult"]))
_, _r = calc("Braton Prime", ["爆头", "爆头加成60"])
check(
    "爆头加成仍只放大超出 1 的部分（暂缓项）：1 + 2×1.6 = 4.2",
    close(_r["head_mult"], 4.2),
    str(_r["head_mult"]),
)

# ---------------------------------------------------------------------------
print("B3 CO 三类桶别 + 种类数计入 MOD 元素")
for _args, _n in (
    (["Braton", "Prime"], 3),
    (["Braton", "Prime", "火90"], 4),
    (["Braton", "Prime", "火90", "冰90"], 4),
):
    _w, _ = dc.find_weapon("Braton Prime")
    _rr = dc.calculate(dc.parse_args(_args)[0], _w)
    check(
        f"{' '.join(_args)}：异常种类数 {_n}", _rr["status_types"] == _n, str(_rr["status_types"])
    )

_co = json.loads((ROOT / "core" / "data" / "co_behavior.json").read_text(encoding="utf-8"))[
    "weapons"
]
_uids = {(w.get("uniqueName") or "").lower(): w for w in dc._load()["weapons"].values()}
_pick = {"additive_with_base_damage": dc.find_weapon("Braton Prime")[0]}
for _k, _v in _co.items():
    for _mode in ("independent", "inert"):
        if _k in _uids and _v.get("base") == _mode and _mode not in _pick:
            _pick[_mode] = _uids[_k]
_bd = 165.0  # 膛线满级
_want = {
    "additive_with_base_damage": (1 + 1.65 + 2.4) / (1 + 1.65),
    "independent": 1 + 2.4,
    "inert": 1.0,
}
for _mode, _w in _pick.items():
    _s0 = {**dc.parse_args(["膛线"])[0], "status_types": 3, "dmg_per_status": 0.0}
    _s8 = {**dc.parse_args(["膛线"])[0], "status_types": 3, "dmg_per_status": 80.0}
    _h0 = dc.calculate(_s0, _w)["health"]
    _r8 = dc.calculate(_s8, _w)
    check(
        f"{_mode}（{_w['name']}）：3 种异常 × CO 80%、膛线 +{_bd:g}% ⇒ ×{_want[_mode]:.4f}",
        close(_r8["health"] / _h0, _want[_mode]),
        f"{_r8['health'] / _h0}",
    )
    if _mode != "additive_with_base_damage":
        check(
            f"{_mode}：卡面附口径说明",
            any("异况超量类口径" in n for n in _r8["notes"]),
            str(_r8["notes"]),
        )

# 数据契约：只收默认 / 灵化形态（防副开火覆盖主形态，旧研究仓表 16 把错配）
check(
    "co_behavior.json 只有 base / incarnon 两种槽",
    all(set(v) <= {"base", "incarnon"} for v in _co.values()),
)
check(
    "co_behavior.json 值域只有 independent / inert（additive 不写）",
    {x for v in _co.values() for x in v.values()} <= {"independent", "inert"},
)
for _nm in ("Grimoire", "Mandonel", "Fulmin", "Larkspur", "Trumna"):
    _w, _ = dc.find_weapon(_nm)
    check(
        f"{_nm} 主开火为 additive（副开火的 independent / inert 不得覆盖）",
        bool(_w) and dc.co_behavior_of(_w) == "additive_with_base_damage",
        dc.co_behavior_of(_w) if _w else "weapon not found",
    )
check(
    "灵化形态单独取值：拉特昂 Prime 灵化 independent / 原型 additive",
    dc.co_behavior_of(dc.find_weapon("拉特昂 Prime")[0], incarnon=True) == "independent"
    and dc.co_behavior_of(dc.find_weapon("拉特昂 Prime")[0]) == "additive_with_base_damage",
)

# ---------------------------------------------------------------------------
print("#6 元素合成：只加口径说明")
_w, _r = calc("Braton Prime", ["火90", "冰90"])
_lines = dc.card_lines(_w, dc.parse_args(["火90", "冰90"])[0], _r, [])
check(
    "卡面有元素合成口径说明（槽位顺序 / 自带元素）",
    any("元素合成口径" in ln and "槽位顺序" in ln for ln in _lines),
)
check("算法未改：火+冰 仍合成爆炸", "blast" in _r["pools"], str(_r["pools"]))

print()
if FAILED:
    print(f"✗ {len(FAILED)} 项失败：" + "、".join(FAILED))
    sys.exit(1)
print("✓ 全部通过")
