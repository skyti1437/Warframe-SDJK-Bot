# -*- coding: utf-8 -*-
"""伤害计算对照复核 B 批回归（python3 tests/test_damage_review_b.py；2026-10-26 前名 test_wfsim_b_batch.py）

出处：研究仓 docs/wfsim-伤害计算对照-20261009.md §4.3 / §3.2 / §4.2 / §3.3
（wfsim github.com/magenie33/wfsim @8f3e6a5，只读核对）。口径由维护者 2026-10-09 拍板：

  B1 DoT 乘本元素 MOD 括号：火 / 毒 / 电 ×(1+该元素 MOD%)，切割与毒气固定 1.0
     （wfsim fight/procs.rs elem_bracket；build/loadout/resolve.rs:569-574 按 MOD 自身元素累加）。
  B2 爆头：人形头部 ×3（wiki Enemy_Body_Parts / wfsim MECHANICS.md:1001-1003），卡面注明口径；
     「加成放大整个部位倍率」「暴击爆头暴伤翻倍」于 2026-10-10 爆头口径批落地（见文末同名段）。
  B3 CO 按 co_behavior 三类：additive 并进基伤括号 / independent 独立乘区 / inert 无效；
     异常种类数计入 MOD 加成的元素。
  #6 元素合成顺序：只加卡面口径说明，不改算法。

全部走 dc.parse_args → dc.calculate 真实入口，不联网。
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import damage_calc as dc  # noqa: E402

FAILED: list[str] = []


def _panel_row(lines: list[str]) -> str:
    """卡面「· 面板　基伤…｜暴击…｜射速…」行（2026-10-11 改版前是 lines[1] 的「　基伤…」缩进行）。"""
    return next(x for x in lines if x.startswith("· 面板"))


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
    "重型机枪手（wiki Module Head 3.0x，2026-10-10 补真实值）→ 3.0",
    close(_r["head_mult"], 3.0),
    str(_r["head_mult"]),
)
_, _r = calc("Braton Prime", ["对", "Legacyte", "爆头"])
# 2026-10-10 第三批改断言：wfsim legacyte.yaml 写「NO WEAK POINT IS PUBLISHED」已过时，
# 现行 wiki Enemy_Body_Parts「Legacyte … Head (3x)*」与 Legacyte 页「Head (3x, but no headcrit
# bonus)」一致 ⇒ 头部 ×3（无暴击爆头）
check("Legacyte（wiki Head 3x*）→ 3.0", close(_r["head_mult"], 3.0), str(_r["head_mult"]))
_, _r = calc("Braton Prime", ["对", "Eidolon Teralyst", "爆头"])
check("夜灵（head_mul 1.0）不吃爆头", close(_r["head_mult"], 1.0), str(_r["head_mult"]))

# ---------------------------------------------------------------------------
print("爆头口径批（2026-10-10）：Σ加成放大整个部位 / 暴击爆头 / 敌人头部真实值")
# wiki Enemy_Body_Parts：(3 + 1.5×(3.5+0.75)) × (1 + 0.3 + 0.4) = 15.9375x
# wfsim fight/pellet.rs:561 part_factor = wp_mult × (1+head_bonus) × (1+head_innate)
_, _r = calc("Braton Prime", ["爆头", "爆头加成60"])
check(
    "爆头加成60：3 × (1+0.6) = 4.8（修前 4.2）", close(_r["head_mult"], 4.8), str(_r["head_mult"])
)
# 进化爆头（evolutions.json value 1.0 = +100%，小数）并入同一括号：3 × (1+1.0) = 6.0（修前 4.0）
_, _r = calc("Latron", ["灵化", "进化", "爆头", "爆头"])
check("拉特昂 灵化进化爆头：3 × (1+1.0) = 6.0", close(_r["head_mult"], 6.0), str(_r["head_mult"]))
# Cernos Prime 自带 +50% 独立乘算（wiki / wfsim spec.rs:228-235）：3 × 1.6 × 1.5 = 7.2
_, _r = calc("Cernos Prime", ["爆头", "爆头加成60"])
check("Cernos Prime：3 × 1.6 × 1.5 = 7.2", close(_r["head_mult"], 7.2), str(_r["head_mult"]))
# Kuva Chakkhurr 自带 +50% 进加法括号：3 × (1+0.5) = 4.5
_, _r = calc("Kuva Chakkhurr", ["爆头"])
check("Kuva Chakkhurr：3 × 1.5 = 4.5", close(_r["head_mult"], 4.5), str(_r["head_mult"]))

# 暴击爆头：头部暴击期望 = 1 + 暴率 × (2×总暴伤 − 1)（wfsim pellet.rs:569-575 cd = 2×cd_total）
_w, _r = calc("Braton Prime", ["对", "Lancer", "爆头"])
_ehc = dc.crit_expectation(_r["crit_cc"], 2 * _r["crit_cm"])
check(
    "Grineer 枪兵：可暴击爆头，头部 = 身体 × 3 × (1+cc(2cm−1))",
    _r["head_crit"] is True
    and close(_r["head_health"], _r["health"] * 3.0 * _ehc)
    and _ehc > _r["crit_exp"],
    f"{_r['head_crit']} {_r['head_health']} vs {_r['health'] * 3.0 * _ehc}",
)
_, _rb = calc("Braton Prime", ["对", "Lancer"])
check(
    "DoT 爆头时继承暴击爆头：DoT(爆头)/DoT(身体) = 3 × 头部暴击期望/身体暴击期望",
    all(
        close(_r["dots"][k] / _rb["dots"][k], 3.0 * _ehc / _r["crit_exp"], 1e-6)
        for k in _rb["dots"]
    )
    and bool(_rb["dots"]),
    f"{_r['dots']} / {_rb['dots']}",
)
_, _r = calc("Braton Prime", ["对", "Crewman", "爆头"])
check(
    "Corpus 船员：头部 3.0 但无暴击爆头（wiki Crewmen 表 Head 3x*）",
    _r["head_crit"] is False
    and close(_r["head_mult"], 3.0)
    and close(_r["head_health"], _r["health"] * 3.0 * _r["crit_exp"]),
    f"{_r['head_crit']} {_r['head_health']}",
)
_, _r = calc("Braton Prime", ["对", "Leech Osprey", "爆头"])
check("Corpus Osprey（飞行单位）逐条标注可暴击爆头", _r["head_crit"] is True, str(_r["head_crit"]))
_w, _r = calc("Braton Prime", ["Corpus", "爆头"])
_lines = dc.card_lines(_w, dc.parse_args(["Corpus", "爆头"])[0], _r, [])
check(
    "不指定敌人 + Corpus：不计暴击爆头，卡面写明",
    _r["head_crit"] is False and any("未计暴击爆头" in ln for ln in _lines),
    str([ln for ln in _lines if "爆头" in ln]),
)
_w, _r = calc("Braton Prime", ["爆头"])
_lines = dc.card_lines(_w, dc.parse_args(["爆头"])[0], _r, [])
check(
    "不指定敌人（默认 Grineer）：按可触发计，卡面加注「对 Corpus 人形无暴击爆头」",
    _r["head_crit"] is True and any("对 Corpus 人形无暴击爆头" in ln for ln in _lines),
    str([ln for ln in _lines if "爆头" in ln]),
)

# 敌人头部真实值（scripts/build_enemies.py 的 HEAD_* 表；2026-10-26 前为 merge_wfsim_enemies.py）
# 2026-10-10 第四批（敌人表精简到 19 个常用敌人）改断言：原用的 Wolf of Saturn Six /
# Anti MOA / Fusion MOA / Corrupted MOA / Manic 均已删除 —— 未分类（null）改用合成敌人记录
# 表达同一语义；头部 1x 留 MOA / Charger / Isolator Bursa；Manic（Body_Parts 3.0）改 Bombard
# （Module 3.0）。保留表里已没有 null 条目。
_en = json.loads((ROOT / "core" / "data" / "enemies.json").read_text(encoding="utf-8"))["enemies"]
check(
    "enemies.json 不再有数值哨兵 2（未分类 = null），每条带 head_src",
    not any(v.get("head_mul") == 2 for v in _en.values())
    and all("head_src" in v for v in _en.values()),
)
check("旧哨兵常量已删除", not hasattr(dc, "_HEAD_MUL_UNCLASSIFIED"))
for _nm in ("MOA", "Isolator Bursa"):
    _, _r = calc("Braton Prime", ["对", _nm, "爆头"])
    check(
        f"{_nm} 头部 1.0（Corpus / 部位表 * ⇒ 无暴击爆头）",
        close(_r["head_mult"], 1.0) and not _r["head_crit"],
    )
# 2026-10-26 维护者定：敌人头部 ×1 仍算爆头部位，暴击爆头按派系 / 逐敌标注（Charger：Infested，Mouth 1x 无 *）
_, _r = calc("Braton Prime", ["对", "Charger", "爆头"])
check(
    "Charger 头部 1.0 仍可暴击爆头（Infested 默认）",
    close(_r["head_mult"], 1.0) and _r["head_crit"],
)
# Babau：头部 1.5x、暴击爆头、头部另加 +100% 暴击率（Braton Prime 12% → 112%：0.88×4 + 0.12×7 = 4.36）
_, _r = calc("Braton Prime", ["对", "Techrot Babau", "爆头"])
check(
    "Techrot Babau 头部 1.5x、可暴击爆头、头部暴击率 +100%",
    close(_r["head_mult"], 1.5) and _r["head_crit"] and close(_r["head_crit_exp"], 4.36),
    f"{_r['head_mult']} {_r['head_crit']} {_r['head_crit_exp']}",
)
_, _r = calc("Braton Prime", ["对", "MOA", "爆头", "爆头加成60"])
check("MOA 头部 1x 仍是爆头部位：1 × 1.6 = 1.6", close(_r["head_mult"], 1.6), str(_r["head_mult"]))
_, _r = calc("Braton Prime", ["对", "Eidolon Teralyst", "爆头", "爆头加成60"])
check("无头部（head_none）不叠爆头加成：恒 1.0", close(_r["head_mult"], 1.0), str(_r["head_mult"]))
_unc = {**_en["Lancer"], "name": "未分类测试敌人", "head_mul": None, "head_src": "未分类：测试"}
_w, _ = dc.find_weapon("Braton Prime")
_s = dc.parse_args(["爆头"])[0]
_s["enemy"] = _unc
_r = dc.calculate(_s, _w)
_lines = dc.card_lines(_w, _s, _r, [])
check(
    "未分类敌人（head_mul null，合成记录）仍按人形 3.0，卡面注明未分类",
    close(_r["head_mult"], 3.0) and any("未分类，按人形计" in ln for ln in _lines),
    str(_r["head_mult"]),
)
_, _r = calc("Braton Prime", ["对", "Bombard", "爆头"])
check("Bombard 按 wiki Module 头部 3.0", close(_r["head_mult"], 3.0), str(_r["head_mult"]))

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

# ---------------------------------------------------------------------------
print("生成器批（2026-10-10；2026-10-26 改由 wiki 生成）：范围段 / 子炸弹 count / 多重 / 范围段 CO")
_wa = json.loads((ROOT / "core" / "data" / "weapon_attacks.json").read_text(encoding="utf-8"))
_if = json.loads((ROOT / "core" / "data" / "incarnon_forms.json").read_text(encoding="utf-8"))
_segs = [
    (v.get("wiki"), s) for k, v in _wa.items() if k != "_meta" for s in v.get("segments") or []
]
# wiki「Condition Overload (Mechanic)」Attack Catalog 里注明「AoE does not scale off multishot」/
# 「Not affected by multishot」的武器（默认主段）；其余范围段一律吃多重
_NO_MS_SOURCED = {
    "Ferrox",
    "Tenet Ferrox",
    "Opticor",
    "Opticor Vandal",
    "Trumna",
    "Trumna Prime",  # 维护者核查 2026-10-26：同构 Trumna（_wiki_weapons.USER_VERIFIED_CO_ROWS）
    "Mausolon (Atmosphere)",
}
check(
    "weapon_attacks：默认主段的 takes_multishot: false 都有出处（CO 页 / 维护者核查行）",
    {w for w, s in _segs if s.get("takes_multishot") is False} == _NO_MS_SOURCED,
    str(sorted({w for w, s in _segs if s.get("takes_multishot") is False})),
)
_snaps = {
    json.loads((ROOT / "core" / "data" / f).read_text(encoding="utf-8"))["_meta"].get(
        "wiki_snapshot"
    )
    for f in (
        "weapon_attacks.json",
        "incarnon_forms.json",
        "co_behavior.json",
        "weapon_deployments.json",
    )
}
check("P2 四表 _meta 记录同一个 wiki 快照、不再记 wfsim", len(_snaps) == 1 and None not in _snaps)
check(
    "weapon_attacks 只收计算器认识的武器（键都在 weapons_stats，无 #类别 冲突键）",
    all(
        k in json.loads((ROOT / "core" / "data" / "weapons_stats.json").read_text(encoding="utf-8"))
        for k in _wa
        if k != "_meta"
    ),
)
_kb = _wa["/lotus/weapons/grineer/bows/grnbow/grnbowweapon"]["segments"]
check(
    "Kuva Bramma：子炸弹接触 + 爆炸两段，count 3",
    [(s["id"], s.get("count")) for s in _kb[1:]] == [("cluster", 3.0), ("cluster_radial", 3.0)],
    str(_kb),
)
_inc_flags = {
    v["base_name"]: (
        v["radial"].get("takes_multishot"),
        v["radial"].get("takes_condition_overload"),
    )
    for k, v in _if.items()
    if k != "_meta" and v.get("radial")
}
check(
    "incarnon_forms：Burston / Zylok / Braton 灵化范围段 = 不吃多重 + 吃 CO（CO 页出处）",
    all(_inc_flags.get(f) == (False, True) for f in ("Burston", "Zylok Prime", "Braton Prime")),
    str(_inc_flags),
)


def _st(name: str, args=(), **over) -> float:
    return calc(name, args, **over)[1]["segments_total"]["total"]


_CO = {"status_types": 3, "dmg_per_status": 80.0}
check(
    "Kuva Bramma 默认含段合计 361.88 → 458.34（子炸弹 ×3 且爆炸计入）",
    close(_st("Kuva Bramma"), 458.3431, 1e-5),
    str(_st("Kuva Bramma")),
)
check(
    "Zarr 默认含段合计 64.08 → 167.97（6 颗子炸弹）",
    close(_st("Zarr"), 167.9718, 1e-5),
    str(_st("Zarr")),
)
check(
    "Trumna Prime 多重100：范围段不吃多重（维护者核查：同构 Trumna），含段合计 73.70",
    close(_st("Trumna Prime", ["多重100"]), 73.7001, 1e-5),
    str(_st("Trumna Prime", ["多重100"])),
)
_cb = json.loads((ROOT / "core" / "data" / "co_behavior.json").read_text(encoding="utf-8"))[
    "weapons"
]


def _cbw(nm):
    return _cb.get(str(dc.find_weapon(nm)[0]["uniqueName"]).lower())


check(
    "CO 同族口径只认逐把核实的行（维护者核查 2026-10-26）：Epitaph Prime / Kuva Grattler / Akarius 直击 = "
    "independent，Latron Wraith / Mk1-Paris / Ballistica 灵化 = independent；未核实的仍缺省（Glaxion）",
    _cbw("Epitaph Prime") == {"base": "independent"}
    and _cbw("Kuva Grattler") == {"base": "independent"}
    and _cbw("Akarius") == {"base": "independent"}
    and all(
        (_cbw(n) or {}).get("incarnon") == "independent"
        for n in ("Latron Wraith", "Mk1-Paris", "Ballistica")
    )
    and _cbw("Glaxion") is None,
)
_, _rv0 = calc("Vectis Prime", ["灵化"])
_, _rv1 = calc("Vectis Prime", ["灵化", "爆头"])
check(
    "爆头才触发的爆炸段：不写爆头不计（卡面提示），写「爆头」计入（Vectis Prime 灵化 65.16 → 121.01）",
    close(_rv0["segments_total"]["total"], 65.1573, 1e-4)
    and any("爆头时另有爆炸段" in n for n in _rv0["notes"])
    and close(_rv1["segments_total"]["total"], 121.0118, 1e-4),
    f"{_rv0['segments_total']['total']} {_rv1['segments_total']['total']}",
)
check(
    "Opticor CO80×3：范围段吃 CO 1130.34 → 1400.65（含伤害量化）",
    close(_st("Opticor", **_CO), 1400.6503, 1e-5),
    str(_st("Opticor", **_CO)),
)
_, _rp = calc("Pox", **_CO)
_pc = next(m for m in _rp["modes"] if m["name"] == "Poison Cloud")
check(
    "Pox 云团吃 CO：4.38 → 14.89，且仍不进含段合计",
    close(_pc["health"], 14.8931, 1e-4) and not _pc.get("in_total"),
    str(_pc["health"]),
)
_, _rtd = calc("Torid", **_CO)
_tc = next(m for m in _rtd["modes"] if m["name"] == "Poison Cloud")
check("Torid 云团吃 CO（independent）：8.76 → 29.79", close(_tc["health"], 29.7862, 1e-4))
check(
    "Braton Prime 灵化 多重100：范围段一次扳机一次（109.17 → 85.22，含伤害量化）",
    close(_st("Braton Prime", ["灵化", "多重100"]), 85.2216, 1e-5),
    str(_st("Braton Prime", ["灵化", "多重100"])),
)
check(
    "Zylok Prime 灵化：Radial Attack 计入（无 → 388.83，含伤害量化）",
    close(_st("Zylok Prime", ["灵化"]), 388.8265, 1e-5),
    str(_st("Zylok Prime", ["灵化"])),
)
_, _ro = calc("Onos", ["灵化"])
check(
    "Onos 灵化（持续光束）不带蓄力攻击的 Radial Attack（wiki 母攻击写错，已按名字改挂）",
    not _ro["segments_total"],
)
# 2026-10-10 第四批改断言（维护者定「Nataruk 口径错位」）：我方主段 = 快速射击（wfsim
# nataruk.yaml，co 缺省 additive），完美射击（wfsim default_form nataruk_perfect，independent）
# 改走「蓄力」形态 ⇒ 默认桶别 additive；independent 见文末第四批段的「蓄力」断言。
check(
    "Nataruk 默认 = 快速射击，CO 桶别 = additive_with_base_damage",
    dc.co_behavior_of(dc.find_weapon("Nataruk")[0]) == "additive_with_base_damage",
)
_w, _r = calc("Kuva Bramma")
_lines = dc.card_lines(_w, dc.parse_args([])[0], _r, [])
check(
    "卡面：含段合计行标注子炸弹颗数",
    any("含段合计" in ln and "×3 颗" in ln for ln in _lines),
    str([ln for ln in _lines if "含段合计" in ln]),
)


# ---------------------------------------------------------------------------
print("第三批（2026-10-10，wfsim @2e5a8d4）：武器自带头部倍率 / 敌人头部 / 非默认形态")
# ① 武器自带头部倍率覆盖（wfsim spec.rs:460-480；wiki Enemy_Body_Parts §Notes）——只在对应形态
_, _r = calc("Arca Plasmor", ["爆头"])
check(
    "Arca Plasmor（默认形态）爆头：头部 ×3 → ×1，无暴击爆头",
    _r["head_mult"] == 1.0 and not _r["head_crit"] and close(_r["head_health"], 148.7559, 1e-5),
    f"{_r['head_mult']} {_r['head_crit']} {_r['head_health']}",
)
_, _r = calc("Arca Plasmor", ["爆头", "爆头加成30"])
check("Arca Plasmor：Deadhead 类加成照乘（×1 × 1.3）", close(_r["head_mult"], 1.3))
_, _r0 = calc("Paris", ["爆头"])
_, _r1 = calc("Paris", ["灵化", "爆头"])
check(
    "Paris：默认形态爆头仍 ×3 且暴击爆头；灵化形态 ×1、无暴击爆头",
    _r0["head_mult"] == 3.0
    and _r0["head_crit"]
    and _r1["head_mult"] == 1.0
    and not _r1["head_crit"],
)
_, _r0 = calc("Athodai", ["爆头"])
_, _r1 = calc("Athodai", ["副开火", "爆头"])
check(
    "Athodai：主开火 ×3，副开火 ×1（只在该形态生效）",
    _r0["head_mult"] == 3.0 and _r1["head_mult"] == 1.0,
)
_, _r0 = calc("Fulmin", ["爆头"])
_, _r1 = calc("Fulmin", ["半自动", "爆头"])
check("Fulmin：全自动 ×3，半自动 ×1", _r0["head_mult"] == 3.0 and _r1["head_mult"] == 1.0)
_, _r1 = calc("Alternox", ["副开火", "爆头"])
check(
    "Alternox 副开火不覆盖（wfsim / Body_Parts 只写主开火；与 Alternox 页「All firing modes」矛盾，待拍板）",
    _r1["head_mult"] == 3.0,
)
_, _r = calc("Braton Prime", ["爆头"], enemy=dc.find_enemy_exact("MOA"))
check("不在表里的武器照旧按敌人头部（MOA ×1）", _r["head_mult"] == 1.0)
_, _r = calc("Arca Plasmor", ["爆头"], enemy=dc.find_enemy_exact("Eidolon Teralyst"))
check("头部覆盖不凭空造头：无头部的敌人仍按身体计", "没有头部" in _r["head_src"])

# ② 敌人头部（build_enemies.py 的 HEAD_* 表）
_leg = dc.find_enemy_exact("Legacyte")
check(
    "Legacyte：head_none → 头部 ×3、无暴击爆头（Body_Parts「Head (3x)*」+ 本页）",
    _leg["head_mul"] == 3.0 and not _leg.get("head_none") and _leg.get("head_crit") is False,
    str(_leg),
)
_bab = dc.find_enemy_exact("Techrot Babau")
check(
    "Techrot Babau：Module 1.5x，head_src 写明部位表 1x 的取舍与暴击来源",
    _bab["head_mul"] == 1.5
    and "Phone Head" in _bab["head_src"]
    and _bab.get("head_cc_bonus") == 1.0,
)
check(
    # 2026-10-10 第四批：Teralyst Synovia 已随敌人表精简删除，留夜灵兆力使与圆蛛
    "夜灵 / 利润收割者圆蛛：仍无头部，head_src 换成逐条核对的出处",
    all(
        dc.find_enemy_exact(n).get("head_none")
        and "riven-mirror" not in dc.find_enemy_exact(n)["head_src"]
        for n in ("Eidolon Teralyst", "Profit-Taker Orb")
    ),
)

# ③ 非默认形态（weapon_attacks.json forms）：选了才计入，不选不计入
check("weapon_attacks：_meta 记录 forms 口径", "forms" in _wa["_meta"]["scope"])
_n_forms = sum(len(v.get("forms") or {}) for k, v in _wa.items() if k != "_meta")
_n_seg_forms = sum(
    1
    for k, v in _wa.items()
    if k != "_meta"
    for f in (v.get("forms") or {}).values()
    if f.get("segments")
)
# 2026-10-26 wfsim 迁移 P2 改断言：133/69 → 137/68（改由 wiki Attacks 生成：Nataruk / Cinta 普通满蓄力与
# 完美射击并存、Ballistica 系补「未蓄力」点射、Kohm 系补单发等；灵化只收与 incarnon_forms 同一攻击的那条）；
# 同日新武器入库（build_weapons_supplement：Daikyu Prime / Afentis Prime / Coda Bubonico …）→ 145/71
check(
    "forms：145 个非默认形态，其中带额外段 71",
    _n_forms == 145 and _n_seg_forms == 71,
    f"{_n_forms} / {_n_seg_forms}",
)
check(
    "顶层 main 与 segments 成对出现（默认主段没有额外段时不写 main，默认口径读不到 forms）",
    all(("segments" in v) == ("main" in v) for k, v in _wa.items() if k != "_meta"),
)
_, _rd = calc("Tenet Tetra")
_, _ra = calc("Tenet Tetra", ["副开火"])
check(
    "Tenet Tetra：不选形态无含段合计；副开火（榴弹）主段 200 + 范围段 → 合计 380.39",
    not _rd["segments_total"] and close(_ra["segments_total"]["total"], 380.3875, 1e-5),
    str(_ra.get("segments_total")),
)
_, _rd = calc("Stahlta")
_, _rc = calc("Stahlta", ["蓄力"])
check(
    "Stahlta：蓄力形态主段 600 + 范围段计入（731.79），默认不计",
    not _rd["segments_total"] and close(_rc["segments_total"]["total"], 731.7882, 1e-5),
    str(_rc.get("segments_total")),
)
_, _rl = calc("Laetum", ["灵化"])
_, _rl0 = calc("Laetum")
check(
    "Laetum 灵化：wfsim 灵化范围段（attacks 无对应行）计入 124.58；默认形态不计",
    close(_rl["segments_total"]["total"], 124.5765, 1e-5) and not _rl0["segments_total"],
    str(_rl.get("segments_total")),
)
_, _rb = calc("Braton Prime", ["副开火"])
check(
    "没有该形态数据的武器：按默认算并注明",
    any("没有「副开火」形态数据" in n for n in _rb["notes"]),
)
_, _rk0 = calc("Kuva Bramma")
check(
    "默认口径不读 forms：Kuva Bramma 默认含段合计仍 458.34",
    close(_rk0["segments_total"]["total"], 458.3431, 1e-5),
)
_s = dc.parse_args(["副开火", "蓄力"])[0]
check("形态关键词后写的生效（副开火 蓄力 → charged）", _s["form"] == "charged")
for _kw, _fk in (
    ("副开火", "alt_fire"),
    ("未蓄力", "uncharged"),
    ("半自动", "semi_auto"),
    ("全自动", "auto"),
):
    check(f"关键词「{_kw}」→ form {_fk}", dc.parse_args([_kw])[0]["form"] == _fk)

# ---------------------------------------------------------------------------
print("第四批（2026-10-10 维护者定）：常用敌人表 / Mutalist MOA 头部 / Nataruk 口径 / 电毒气落点")
sys.path.insert(0, str(ROOT / "scripts"))
import build_enemies as _be  # noqa: E402

# ① 敌人表 = 保留名单 19 个，官方中文名补齐
check(
    "enemies.json = KEEP_ENEMIES（19 个，顺序无关）",
    set(_en) == set(_be.KEEP_ENEMIES) and len(_en) == 19,
    str(sorted(set(_en) ^ set(_be.KEEP_ENEMIES))),
)
check("19 个敌人都有中文名", all(v.get("zh") for v in _en.values()))
check(
    "官方简中补齐（dict.zh.json）：吸血鱼鹰 / 隔离金流恐鸟 / 传承种 / 科腐者疫胞 / 毒化者",
    [_en[n]["zh"] for n in ("Leech Osprey", "Isolator Bursa", "Legacyte", "Techrot Babau", "Nox")]
    == ["吸血鱼鹰", "隔离金流恐鸟", "传承种", "科腐者疫胞", "毒化者"],
)
# 2026-10-26 P4-C：敌人表改由 build_enemies.py 从 wiki 模块 + PEP 整表重建（不再从全表删减）
check(
    "每个敌人每个数值字段都带出处（src），护盾 / 护甲 wiki 未写的注明取自 PEP",
    all(
        all(
            k in v.get("src", {})
            for k in ("faction", "base_level", "base_health", "base_shield", "base_armor")
        )
        for v in _en.values()
    )
    and "PEP" in _en["Lancer"]["src"]["base_shield"],
)
# ★ 2026-10-27：构建输入目录改由环境变量提供（公开仓 gitleaks 的 local-user-path 规则
#   不允许出现本机绝对路径）；未设置时指向不存在的相对路径 ⇒ 下面的 isdir 闸门自动跳过。
_BI = os.environ.get("WF_BUILD_INPUTS", "")
_ABSENT = Path("__no_build_inputs__")
_snap_w = Path(_BI, "wiki-modules-20261026/enemies") if _BI else _ABSENT
_snap_p = Path(_BI, "pep-513105d") if _BI else _ABSENT
if os.path.isdir(_snap_w) and os.path.isdir(_snap_p):  # CI 非 root：/root 不可读 ⇒ False，不抛
    _rebuilt = _be.build(_snap_w, _snap_p, allow_conflict=False)
    check(
        "build_enemies 按固定快照可复现 enemies.json（wiki 与 PEP 无冲突）",
        _rebuilt["enemies"] == _en,
    )
else:
    print("[SKIP] 本机无敌人 wiki / PEP 快照：跳过 build_enemies 复现断言")

# ② 常用叫法命中（parse_args 真实入口）
for _q, _want in (
    ("重机枪手", "Heavy Gunner"),
    ("枪兵", "Lancer"),
    ("船员", "Crewman"),
    ("恐鸟", "MOA"),
    ("夜灵", "Eidolon Teralyst"),
    ("兆力使", "Eidolon Teralyst"),
    ("诺克斯", "Nox"),
    ("鱼鹰", "Leech Osprey"),
    ("堕落重机枪手", "Corrupted Heavy Gunner"),
):
    _sp = dc.parse_args(["Braton", "Prime", "对", _q])[0]
    check(f"「对 {_q}」→ {_want}", (_sp["enemy"] or {}).get("name") == _want, str(_sp["enemy"]))

# ③ 不在表里的敌人：按未指定敌人算 + 卡面列出可用敌人；不被当成 MOD
_w, _ = dc.find_weapon("Braton Prime")
_sp = dc.parse_args(["Braton", "Prime", "对", "屠夫"])[0]
_r = dc.calculate(_sp, _w)
_lines = dc.card_lines(_w, _sp, _r, [])
check(
    "「对 屠夫」（已删）：enemy 为空、卡面「不在常用敌人表…可用的有：…枪兵…」",
    _sp["enemy"] is None
    and _sp["enemy_miss"] == "屠夫"
    and any("不在常用敌人表" in ln and "可用的有" in ln and "枪兵" in ln for ln in _lines),
    str([ln for ln in _lines if "敌人" in ln]),
)
for _n in ("Butcher", "Scorch"):
    _sp = dc.parse_args(["Braton", "Prime", "对", _n])[0]
    check(f"「对 {_n}」不被当成 MOD", not _sp["mods"] and _sp["enemy_miss"] == _n, str(_sp["mods"]))
_sp = dc.parse_args(["Braton", "Prime", "对", "C系", "膛线"])[0]
check(
    "「对 C系 膛线」：对与词之间隔了派系词，MOD 全名仍按 MOD（不误判为敌人）",
    [m.get("name") for m in _sp["mods"]] == ["Serration"] and not _sp["enemy_miss"],
)
_sp = dc.parse_args(["对", "G系", "绝路p"])[0]
check("「对 G系 绝路p」：武器名不被吞成敌人", not _sp["enemy_miss"])

# ④ Swarm / Tar Mutalist MOA 头部 4.5（补丁表，数据不保留）
check(
    "补丁表：Swarm / Tar Mutalist MOA 头部 4.5（Enemy_Body_Parts），Tar 无暴击爆头",
    _be.HEAD_TABLE["Swarm Mutalist MOA"][0] == 4.5
    and _be.HEAD_TABLE["Tar-Mutalist MOA"][0] == 4.5
    and _be.HEAD_CRIT_OVERRIDE.get("Tar-Mutalist MOA") is False
    and "Swarm Mutalist MOA" not in _en,
)

# ⑤ Nataruk：默认 = 快速射击（additive），「蓄力」= 完美射击（independent，头部 1x）
_CO3 = {"status_types": 3, "dmg_per_status": 80.0}
_, _rn = calc("Nataruk", ["膛线"], **_CO3)
_, _rc = calc("Nataruk", ["蓄力", "膛线"], **_CO3)
check(
    "Nataruk 膛线 CO80×3：默认（快速射击 additive）888.00 → 497.71",
    close(_rn["health"], 497.71, 1e-4),
    str(_rn["health"]),
)
check(
    "Nataruk 蓄力（完美射击 900，independent）膛线 CO80×3 = 1776.00，头部 1x",
    close(_rc["health"], 1776.0, 1e-4)
    and close(_rc["head_mult"], 1.0)
    and any("独立乘区" in n for n in _rc["notes"]),
    f"{_rc['health']} {_rc['head_mult']}",
)
_, _ru = calc("Nataruk", ["未蓄力"])
check(
    "Nataruk「未蓄力」= 默认主段（不再报「没有该形态数据」）",
    any("即该武器的默认主段" in n for n in _ru["notes"]),
    str(_ru["notes"]),
)
_nat = _wa["/lotus/weapons/tenno/bows/omicrus/omicrusplayerwep"]
check(
    "weapon_attacks：Nataruk main_form=base，forms = 满蓄力 + 完美射击（都 independent，wiki CO 页）",
    _nat.get("main_form") == "base"
    and sorted(_nat["forms"]) == ["charged:charged_shot", "charged:perfect_shot"]
    and all(f.get("co_behavior") == "independent" for f in _nat["forms"].values())
    and "charge_seconds" not in _nat,
)
_, _rcs = calc("Nataruk", ["满蓄力"])
_, _rpf = calc("Nataruk", ["完美射击"])
check(
    "Nataruk「满蓄力」= 普通满蓄力（1.0 s / 50% / 2.2×），「完美射击」= 完美射击（0.7 s / 60% / 2.4×）",
    close(_rcs["crit_cc"], 0.5) and close(_rpf["crit_cc"], 0.6),
    f"{_rcs['crit_cc']} {_rpf['crit_cc']}",
)

# ⑥ 电 / 毒气 DoT 落点层（wfsim M100）：有爆头加成时多乘 (1+Σ)，无加成不变，毒素不变
_, _e0 = calc("Braton Prime", ["电90", "爆头"])
_, _e1 = calc("Braton Prime", ["电90", "爆头", "爆头加成60"])
_, _t1 = calc("Braton Prime", ["毒90", "爆头", "爆头加成60"])
_, _g1 = calc("Braton Prime", ["火90", "毒90", "爆头", "爆头加成60"])
check("电90 爆头（无加成）DoT 不变 183.364", close(_e0["dots"]["electricity"], 183.364, 1e-5))
check(
    "电90 爆头 爆头加成60：电 DoT 293.382 → 469.412（×1.6），毒素同条件 293.382 不变",
    close(_e1["dots"]["electricity"], 469.412, 1e-5) and close(_t1["dots"]["toxin"], 293.382, 1e-5),
    f"{_e1['dots']} {_t1['dots']}",
)
check(
    "火90 毒90（毒气）爆头 爆头加成60：毒气 DoT 154.412 → 247.059（×1.6）",
    close(_g1["dots"]["gas"], 247.059, 1e-5),
    str(_g1["dots"]),
)

# ---------------------------------------------------------------------------
print("b5 批（2026-10-10）：主段按攻击段取值 / 物理对调残留 / 默认形态 ≠ wiki 首个攻击")
sys.path.insert(0, str(ROOT))
import build_damage_data as _bd  # noqa: E402

_ws = json.loads((ROOT / "core" / "data" / "weapons_stats.json").read_text(encoding="utf-8"))
_NAT = "/lotus/weapons/tenno/bows/omicrus/omicrusplayerwep"

# ① 源头修正幂等：现行 weapons_stats 已是 repair_weapons 之后的状态（重放 0 处改动）
check(
    "build_damage_data.repair_weapons 对现行 weapons_stats 重放 0 处修正（源头与数据一致、幂等）",
    _bd.repair_weapons(json.loads(json.dumps(_ws))) == [],
)
_fix_bad = []
for _k, _fx in _bd.MAIN_ATTACK_FIX.items():
    for _f, _v in _fx["values"].items():
        if _f == "damage":
            _got = {t: float(x) for t, x in _ws[_k]["damage"].items() if t != "total" and x}
            # 只比 > 0 的分量（b7 批 Simulor 系主段 = 直击 冲击 0）
            if _got != {t: float(x) for t, x in _v.items() if x}:
                _fix_bad.append(f"{_fx['name']}.damage {_got}")
        elif abs(float(_ws[_k][_f]) - float(_v)) > 1e-9:
            _fix_bad.append(f"{_fx['name']}.{_f}={_ws[_k][_f]}")
check(
    f"MAIN_ATTACK_FIX {len(_bd.MAIN_ATTACK_FIX)} 把都落进了 weapons_stats（值 = wiki 该攻击段原值）",
    # 16 → 24：b6 批新增 Tysis / Coda Tysis / Ambassador / Pathocyst / Kohm / Kuva Kohm / Zylok /
    # Zylok Prime 八条（wiki 原值，出处见各条 source）；24 → 27：b7 批新增 Simulor / Synoid Simulor /
    # Talons（维护者定：主段 = wiki 面板口径）
    len(_bd.MAIN_ATTACK_FIX) == 27 and not _fix_bad,
    str(_fix_bad),
)

# ② Nataruk 主段触发 = 快速射击 0.3（wiki primary Nataruk AttackIndex 1；修前 0.5 = 蓄力/完美射击）
check("weapons_stats：Nataruk procChance 0.5 → 0.3", close(_ws[_NAT]["procChance"], 0.3))
_, _n0 = calc("Nataruk")
_, _n9 = calc("Nataruk", ["触发90"])
_, _nc = calc("Nataruk", ["蓄力"])
check(
    # b7 批：快速射击 = 弓未蓄力点按，节奏 = 1 / 上弦 0.6s（wfsim 口径；修前按面板 0.667 ⇒ 0.20 次/秒）
    "Nataruk 默认触发 30%（0.50 次/秒 = 0.3 / 上弦 0.6s），触发90 → 57%；「蓄力」= 完美射击 50%",
    close(_n0["steady"]["status_chance"], 0.3)
    and close(_n0["steady"]["procs_per_sec"], 0.3 / 0.6, 1e-3)
    and close(_n9["steady"]["status_chance"], 0.57)
    and close(_nc["steady"]["status_chance"], 0.5),
    f"{_n0['steady']['status_chance']} / {_n9['steady']['status_chance']} / "
    f"{_nc['steady']['status_chance']}",
)

# ③ 物理对调残留：段里只有穿刺、顶层多出同值切割 ⇒ 记两次（Tetra 57.6 vs wiki 32）
for _nm, _tot in (("Tetra", 32.0), ("Dorrclave", 222.0), ("Vesper 77", 180.0), ("Sydon", 225.0)):
    _w, _ = dc.find_weapon(_nm)
    check(f"{_nm} 基础伤害 = wiki AttackIndex 1 合计 {_tot:g}", close(_w["damage"]["total"], _tot))
_, _rt = calc("Tetra")
check(
    "Tetra 默认：单发 13.404 → 7.666，无切割 DoT（修前切割是残留键）",
    close(_rt["health"], 7.666, 1e-3) and "slash" not in (_rt["dots"] or {}),
    f"{_rt['health']} {_rt['dots']}",
)

# ④ 主段其余字段与伤害同段
_, _rf = calc("Fulmin")
check(
    "Fulmin 主段 = 全自动（33 / 28% / 2.4x / 10% / 9.33）：DPS 15.66 → 67.433",
    close(_rf["crit_cc"], 0.28)
    and close(_rf["fire_rate"], 9.33)
    and close(_rf["dps"], 67.433, 1e-4),
    f"{_rf['crit_cc']} {_rf['fire_rate']} {_rf['dps']}",
)
for _nm, _f, _v in (
    ("Lanka", "criticalChance", 0.25),
    ("Epitaph", "criticalChance", 0.48),
    ("Ballistica", "criticalChance", 0.15),
    ("Miter", "procChance", 0.2),
    ("Drakgoon", "procChance", 0.063),
    ("Cinta", "procChance", 0.3),
):
    _w, _ = dc.find_weapon(_nm)
    check(f"{_nm} {_f} = {_v:g}（与主段伤害同一攻击段）", close(float(_w[_f]), _v))

# ⑤ 默认主段 ≠ wiki 首个攻击 / 有多种开火方式的组：关键词落到正确的 forms 条目
_kw_cases = (
    ("Paris", "蓄力", "形态：蓄力（Paris 基础 320"),
    ("Paris", "未蓄力", "「未蓄力」即该武器的默认主段"),
    ("Miter", "蓄力", "形态：蓄力（Miter 基础 500"),
    ("Miter", "未蓄力", "「未蓄力」即该武器的默认主段"),
    ("Drakgoon", "蓄力", "形态：蓄力（Drakgoon 基础 70"),
    ("Zenith", "半自动", "形态：半自动（Zenith 基础 150"),
    ("Zenith", "全自动", "「全自动」即该武器的默认主段"),
    ("Kuva Quartakk", "副开火", "形态：副开火（Kuva Quartakk 基础 54"),
    ("Kuva Quartakk", "全自动", "「全自动」即该武器的默认主段"),
    ("Fulmin", "全自动", "「全自动」即该武器的默认主段"),
    ("Lanka", "蓄力", "「蓄力」即该武器的默认主段"),
    ("Epitaph", "蓄力", "「蓄力」即该武器的默认主段"),
)
for _nm, _kw, _want in _kw_cases:
    _, _r = calc(_nm, [_kw])
    check(f"{_nm}「{_kw}」→ {_want}", any(_want in n for n in _r["notes"]), str(_r["notes"]))
_, _rp = calc("Paris", ["蓄力"])
check(
    "Paris「蓄力」单发 36.138 → 74.466（修前报没有数据、按未蓄力算）",
    close(_rp["health"], 74.466, 1e-4),
)
# 2026-10-26：默认主段改按 weapons_stats 认 wiki 攻击（build_weapon_attacks.pick_main），不再有
# MAIN_FORM_OVERRIDE；弓 / 蓄力霰弹的满蓄力都在 forms，记录带 main_form、顶层不带蓄力秒数
_bow_ok = []
for _nm in ("Paris", "Cernos", "Dread", "Evensong", "Drakgoon", "Miter", "Zenith", "Kuva Quartakk"):
    _rec = _wa[str(dc.find_weapon(_nm)[0]["uniqueName"]).lower()]
    _bow_ok.append((_nm, bool(_rec.get("main_form")) and "charge_seconds" not in _rec))
check(
    "弓 / Drakgoon / Zenith / Kuva Quartakk / Miter：满蓄力等进 forms，记录带 main_form、顶层无 charge_seconds",
    all(ok for _, ok in _bow_ok),
    str(_bow_ok),
)
check(
    "Mutalist Cernos 主段 = 未蓄力 205（去掉毒云 5），CO 桶别 = 未蓄力那条的 independent",
    close(dc.find_weapon("Mutalist Cernos")[0]["damage"]["total"], 205.0)
    and dc.co_behavior_of(dc.find_weapon("Mutalist Cernos")[0]) == "independent",
)

# ---------------------------------------------------------------------------
print(
    "b6 批（2026-10-10）：点射 / 蓄力有效射速 + Tysis / Ambassador / Pathocyst / Kohm / Zylok 数据"
)
_ws = json.loads((ROOT / "core" / "data" / "weapons_stats.json").read_text(encoding="utf-8"))
_inc = json.loads((ROOT / "core" / "data" / "incarnon_forms.json").read_text(encoding="utf-8"))

# ① 生成器表全部落进 weapons_stats（repair 幂等已由上面的「重放 0 处」覆盖）
_bad = [
    v[0]
    for k, v in _bd.BURST_FIX.items()
    if (_ws[k]["fireRate"], _ws[k]["burstCount"], _ws[k]["burstDelay"]) != (v[1], v[2], v[3])
]
_bad += [v[0] for k, v in _bd.CHARGE_DRAW_ONLY.items() if _ws[k].get("chargeCadence") != "draw"]
check(
    f"BURST_FIX {len(_bd.BURST_FIX)} 把 / CHARGE_DRAW_ONLY {len(_bd.CHARGE_DRAW_ONLY)} 把落进 "
    "weapons_stats（面板射速 = wiki FireRate，点射数 / 发间延迟 = wiki BurstCount / BurstDelay）",
    # CHARGE_DRAW_ONLY 19 → 17：b7 批 Epitaph / Epitaph Prime 移出（改走 wfsim 一般式）
    len(_bd.BURST_FIX) == 30 and len(_bd.CHARGE_DRAW_ONLY) == 17 and not _bad,
    str(_bad),
)

# ② 点射：wiki Fire Rate §Burst「Effective = N / (1/FR + (N−1)·D)」，Burston 3/(0.2+2×0.061)
_, _rb = calc("Burston")
check(
    "Burston 面板射速 5（修前 WFCD 7.83），有效 9.3168 发/s，DPS 61.92 → 73.65",
    close(_rb["fire_rate"], 5.0)
    and close(_rb["fire_rate_eff"], 3 / (0.2 + 2 * 0.061))
    and close(_rb["dps"], 73.65, 1e-4),
    f"{_rb['fire_rate']} {_rb['fire_rate_eff']} {_rb['dps']}",
)
_, _rbp = calc("Burston", fire_rate_pct=60.0, fire_rate_mod_pct=60.0)
check(
    "Burston 射速 +60%：发间延迟同步缩短 ⇒ 有效射速线性 ×1.6（14.907）",
    close(_rbp["fire_rate_eff"], 3 / (1 / 8 + 2 * 0.061 / 1.6))
    and close(_rbp["fire_rate_eff"], _rb["fire_rate_eff"] * 1.6),
    str(_rbp["fire_rate_eff"]),
)
_, _rbn = calc("Burston", ["关键延迟"])
check(
    "Burston 关键延迟 −20%：发间延迟不随负射速拉长（wiki「not affected by net negative」）⇒ 8.0645",
    close(_rbn["fire_rate_eff"], 3 / (1 / 4 + 2 * 0.061)),
    str(_rbn["fire_rate_eff"]),
)
for _nm, _eff in (
    ("Burston Prime", 3 / (0.2 + 2 * 0.04)),
    ("Hind", 5 / (0.2 + 4 * 0.12)),
    ("Sicarus", 3 / (1 / 3.5 + 2 * 0.04)),
    ("Akjagara", 10.0),  # 发间延迟 0 ⇒ = N × FR，修前后同值
    ("Quartakk", 4 * 1.58),
):
    _, _r = calc(_nm)
    check(f"{_nm} 有效射速 {_eff:.4f}", close(_r["fire_rate_eff"], _eff), str(_r["fire_rate_eff"]))
_, _rbi = calc("Burston", ["灵化"])
_, _rsi = calc("Sicarus", ["灵化"])
check(
    "灵化形态节奏随形态：Burston 灵化全自动 20（不沿用原型 3 连发）；Sicarus 灵化点射 8.2031",
    close(_rbi["fire_rate_eff"], 20.0) and close(_rsi["fire_rate_eff"], 3 / (1 / 3.5 + 2 * 0.04)),
    f"{_rbi['fire_rate_eff']} {_rsi['fire_rate_eff']}",
)
check(
    "incarnon_forms：Vasto 灵化 6 连发 / 0.1s、Paris 灵化蓄力 0.8s（wfsim 生成器带出 burst / charge_seconds）",
    _inc["/lotus/weapons/tenno/pistol/revolverpistol"]["burst"]
    == {"count": 6, "delay_seconds": 0.1}
    and _inc["/lotus/weapons/tenno/bows/huntingbow"]["charge_seconds"] == 0.8,
)
_, _rn = calc("Nagantaka", ["副开火"])
check(
    "Nagantaka 副开火（9 连发 / 0.15s / 5）有效 6.4286",
    close(_rn["fire_rate_eff"], 9 / (0.2 + 8 * 0.15)),
    str(_rn["fire_rate_eff"]),
)
_hdr = _panel_row(dc.card_lines(dc.find_weapon("Burston")[0], dc.parse_args([])[0], _rb, []))
check("卡面头部：射速 5（有效 9.32 发/s）", "射速 5（有效 9.32 发/s）" in _hdr, _hdr)

# ③ 蓄力：wiki Fire Rate §Charge Time
_, _rl = calc("Lanka")
check(
    "Lanka DPS 不再为 0：有效射速 = 1/蓄力 1s（wiki「Lanka … does not have a delay」）⇒ DPS 114.98",
    close(_rl["fire_rate_eff"], 1.0) and close(_rl["dps"], 114.984, 1e-4),
    f"{_rl['fire_rate_eff']} {_rl['dps']}",
)
_hdl = _panel_row(dc.card_lines(dc.find_weapon("Lanka")[0], dc.parse_args([])[0], _rl, []))
check("Lanka 卡面：射速 —（有效 1 发/s）", "射速 —（有效 1 发/s）" in _hdl, _hdl)
_, _ro = calc("Opticor")
check(
    "Opticor 一般蓄力式 1/(2 + 1/1) = 0.3333，DPS 229.28 → 76.43",
    close(_ro["fire_rate_eff"], 1 / 3) and close(_ro["dps"], 76.428, 1e-4),
    f"{_ro['fire_rate_eff']} {_ro['dps']}",
)
_, _rpc = calc("Paris", ["蓄力"])
# b8 批改：wiki Fire Rate 弓式 1/(蓄力 + 装填) 直接作有效射速（修前爆发 1/0.5 = 2、上弦只进持续）
check(
    "Paris 蓄力（弓式）：有效射速 = 1/(0.5 + 上弦 0.65) = 0.8696，爆发 = 持续",
    close(_rpc["fire_rate_eff"], 1 / (0.5 + 0.65), 1e-6)
    and close(_rpc["dps_sustained"], _rpc["dps"], 1e-9)
    and close(_rpc["reload"], 0.65, 1e-6),
    f"{_rpc['fire_rate_eff']} {_rpc['dps_sustained']}",
)
_, _rpn = calc("Paris", ["蓄力", "关键延迟"])
check(
    "弓 + 关键延迟：MOD 部分 ×2 ⇒ 蓄力 0.5/0.6 = 0.833s，有效射速 1/(0.833 + 0.65)",
    close(_rpn["fire_rate_eff"], 1 / (0.5 / 0.6 + 0.65), 1e-6),
    str(_rpn["fire_rate_eff"]),
)
_, _rp0 = calc("Paris")
check(
    # b7 批：wiki 未单列未蓄力节奏 ⇒ 按 wfsim「只付上弦」（paris_uncharged.yaml charge_seconds 0.0 +
    # DrawOnly；M16 无实测结果）。修前 = 面板 1
    "Paris 默认（未蓄力点按）：有效射速 = 1 / 上弦 0.65s = 1.5385，持续 = 爆发",
    close(_rp0["fire_rate_eff"], 1 / 0.65, 1e-6)
    and close(_rp0["dps_sustained"], _rp0["dps"], 1e-9),
    f"{_rp0['fire_rate_eff']} {_rp0['dps_sustained']} {_rp0['dps']}",
)

# ④ 数据修正（wiki 原值）
for _nm, _tot in (
    ("Tysis", 49.0),
    ("Coda Tysis", 76.0),
    ("Ambassador", 29.0),
    ("Pathocyst", 262.0),
):
    _w, _ = dc.find_weapon(_nm)
    check(f"{_nm} 主段 = {_tot:g}", close(_w["damage"]["total"], _tot), str(_w["damage"]["total"]))
_w, _ = dc.find_weapon("Tysis")
check(
    "Tysis 腐蚀 DoT 单列一段（27 / 跳），主段不含腐蚀；Coda Tysis 段 59",
    [a["total"] for a in _w["attacks"]] == [49.0, 27.0]
    and not _w["damage"].get("corrosive")
    and [a["total"] for a in dc.find_weapon("Coda Tysis")[0]["attacks"]] == [76.0, 59.0],
)
_, _rt = calc("Tysis")
check(
    "Tysis DoT 段只展示不进合计",
    any(m["name"] == "Corrosive DoT" and not m.get("in_total") for m in _rt["modes"]),
)
_, _rk = calc("Kohm")
check(
    "Kohm 满转 12 弹片 × 6.25%（wiki），每发耗 4 弹：异常 2.75 次/s，持续弹匣 245/4 发",
    close(_rk["multishot_total"], 12.0)
    and close(_rk["steady"]["status_chance"], 0.0625)
    and close(_rk["steady"]["procs_per_sec"], 3.6666667 * 12 * 0.0625, 1e-6)
    and close(
        _rk["dps_sustained"], _rk["dps"] * (245 / 4 / 3.6666667) / (245 / 4 / 3.6666667 + 2), 1e-9
    ),
    f"{_rk['multishot_total']} {_rk['steady']['procs_per_sec']} {_rk['dps_sustained']}",
)
for _nm in ("Zylok", "Zylok Prime"):
    check(f"{_nm} 射速 1.5 → 1.8（wiki）", close(dc.find_weapon(_nm)[0]["fireRate"], 1.8))

# ---------------------------------------------------------------------------
print(
    "b7 批（2026-10-10）：Simulor 系 / Talons / 暗黑分合剑主段 + 每发耗弹 + Epitaph / 弓未蓄力节奏"
)
_ws = json.loads((ROOT / "core" / "data" / "weapons_stats.json").read_text(encoding="utf-8"))
_wa = json.loads((ROOT / "core" / "data" / "weapon_attacks.json").read_text(encoding="utf-8"))

# ① 维护者定：Simulor / Synoid Simulor 主段 = wiki Orb Launch 直击 0，爆炸 200 / 240 是范围段
for _nm, _seg_tot, _crit in (("Simulor", 200.0, 0.12), ("Synoid Simulor", 240.0, 0.0)):
    _w, _r = calc(_nm)
    check(
        f"{_nm} 主段 = 直击 0（暴击 {_crit:g}）；范围段 {_seg_tot:g} 进含段合计；DPS 按含段合计 > 0",
        _w["damage"]["total"] == 0.0
        and close(_w["criticalChance"], _crit)
        and _r["health"] == 0
        and any(
            m["name"] == "Orb Explosion" and m.get("in_total") and close(m["total"], _seg_tot)
            for m in _r["modes"]
        )
        and close(_r["dps_seg"]["crit"], _r["segments_total"]["total"] * _r["fire_rate_eff"])
        and _r["dps_seg"]["nocrit"] > 0,
        str(_r.get("dps_seg")),
    )
_, _rs = calc("Simulor")
_mt = 8 / _rs["fire_rate_eff"]
check(
    "Simulor 含段 DPS 147.18（含暴击）/ 131.41（无暴击），持续 = 弹匣 8 / 装填 3s 循环 69.26",
    close(_rs["dps_seg"]["crit"], 147.179, 1e-5)
    and close(_rs["dps_seg"]["nocrit"], 131.410, 1e-5)
    and close(_rs["dps_seg"]["sustained_crit"], _rs["dps_seg"]["crit"] * _mt / (_mt + 3), 1e-9),
    str(_rs["dps_seg"]),
)
_, _rsf = calc("Simulor", ["火90", "触发90"])
check(
    "直击 0 + 元素 MOD：主段仍为 0（修前「每类型至少 1 点」把 0 伤害类型抬成 1 ⇒ 1.12）",
    _rsf["health"] == 0 and _rsf["dps"] == 0,
    f"{_rsf['health']} {_rsf['dps']}",
)
_cl = dc.card_lines(dc.find_weapon("Simulor")[0], dc.parse_args([])[0], _rs, [])
check(
    "Simulor 卡面：DPS 行注明「直击 0，按含段合计」；面板口径 = 发射球体（直击）",
    any("爆发 DPS：147" in x and "直击 0，按含段合计" in x for x in _cl)
    # 2026-10-11 改版：认出主段且有分段表时，「面板口径」并进「分段」区块标题（主段 X）
    and any(x.startswith("◆ 分段") and "主段 发射球体（直击）" in x for x in _cl),
    "\n".join(x for x in _cl if "DPS" in x or "面板口径" in x),
)

# ② 维护者定：Talons 主段 = wiki 面板段 Embedded Detonation 250 / 10%；Sancti Castanas 不变
_w, _rt = calc("Talons")
check(
    "Talons 主段 = 黏附后引爆 250 / 22% / 2x / 10%（修前 Mid-Flight 120 / 26%）；Mid-Flight 为其它段",
    _w["damage"] == {"blast": 250.0, "total": 250.0}
    and close(_w["procChance"], 0.1)
    and close(_w["criticalChance"], 0.22)
    and [a["name"] for a in _w["attacks"]] == ["Mid-Flight Detonation", "Embedded Detonation"]
    and not any(m.get("in_total") for m in _rt["modes"]),
)
_w = dc.find_weapon("Sancti Castanas")[0]
check(
    "Sancti Castanas 保持 Mid-Flight 300 / 34%（wiki 面板即该段）",
    _w["damage"]["total"] == 300 and close(_w["procChance"], 0.34, 1e-6),
)

# ③ 维护者定：暗黑分合剑默认 = 重剑 230；双剑段只作其它段展示
_w, _rd = calc("Dark Split-Sword")
check(
    "暗黑分合剑主段 = 重剑 230 / 15% / 2x / 25% / 0.917，重击 1380；双剑 116 只展示不进合计",
    close(_w["damage"]["total"], 230.0)
    and (_w["criticalChance"], _w["criticalMultiplier"], _w["procChance"], _w["fireRate"])
    == (0.15, 2.0, 0.25, 0.917)
    and _w["heavyAttackDamage"] == 1380
    and any(
        m["name"] == "Dual Swords Normal Attack"
        and close(m["total"], 116.0)
        and not m.get("in_total")
        for m in _rd["modes"]
    ),
)

# ④ 布里斯提卡 Prime 蓄力：10-10 维护者定保留 152（wfsim M66 实测）；2026-10-26 维护者核查改为 wiki 76 / 弹片 ×4
#   （模块 Multishot 4 + 游戏数据 fireIterations 4 + 正文「4 bolts」三源一致，152 无法复现）。每发耗 4 弹
_, _rbc = calc("Ballistica Prime", ["蓄力"])
_mt = 32 / 4 / _rbc["fire_rate_eff"]  # b8 批：弹匣按 wiki 弹药口径 32（修前 WFCD 8 ⇒ 8/4 = 2 发）
check(
    "Ballistica Prime 蓄力基伤 76 / 弹片（×4 弹片）；每发耗 4 弹 ⇒ 持续弹匣 32/4 = 8 发（wiki「8-shot magazine」）",
    close(_rbc["weapon_eff"]["damage"]["total"], 76.0)
    and _rbc["ammo_cost"] == 4.0
    and close(_rbc["dps_sustained"], _rbc["dps"] * _mt / (_mt + 1.2), 1e-6),
    f"{_rbc['dps_sustained']}",
)

# ⑤ 每发耗弹：AMMO_COST_FIX 全部落进 weapons_stats；形态各自的 ammo_cost
_bad = [v[0] for k, v in _bd.AMMO_COST_FIX.items() if _ws[k].get("ammoCost") != float(v[1])]
check(
    f"AMMO_COST_FIX {len(_bd.AMMO_COST_FIX)} 把落进 weapons_stats（wiki 主段 AmmoCost ≠ 1）",
    # 38 → 39（2026-10-16 +Arbucep 6：wiki Codex 齐射 6 枚火箭 + wfsim M112）
    len(_bd.AMMO_COST_FIX) == 39 and not _bad,
    str(_bad),
)
check(
    "Kuva Nukor 不填每发耗弹（wiki secondary:5055 为 1；wfsim 0.5 与 wiki 不一致）",
    "ammoCost" not in _ws["/lotus/weapons/grineer/kuvalich/secondaries/nukor/kuvanukor"],
)
_, _rg = calc("Glaxion")
_mt = 80 / (_rg["fire_rate_eff"] * 0.5)  # 面板射速 12.000001
check(
    "Glaxion 持续射击每跳耗 0.5 弹：持续 DPS = 爆发 × (80/(12×0.5)) / (… + 2.2)，51.38 → 58.66",
    close(_rg["dps_sustained"], _rg["dps"] * _mt / (_mt + _rg["reload"]), 1e-9)
    and close(_rg["dps_sustained"], 58.655, 1e-4),
    str(_rg["dps_sustained"]),
)
for _nm, _args, _ac in (
    ("Zenith", ["半自动"], 3.0),
    ("Fulmin", ["半自动"], 10.0),
    ("Staticor", [], 5.0),
    ("Staticor", ["未蓄力"], 1.0),
    ("Ferrox", ["副开火"], 1.0),
    ("Cedo", ["副开火"], 0.0),
):
    _, _r = calc(_nm, _args)
    check(f"{_nm} {''.join(_args) or '默认'} 每发耗 {_ac:g} 弹", _r["ammo_cost"] == _ac)
_, _rc = calc("Cedo", ["副开火"])
check(
    "Cedo 副开火不耗弹（wiki AmmoCost 0）⇒ 无装填循环，持续 = 爆发",
    _rc["no_cycle"] and close(_rc["dps_sustained"], _rc["dps"]),
)
check(
    "weapon_attacks forms：Zenith 半自动 ammo_cost 3、Cortege 副开火 0（wiki AmmoCost）",
    _wa["/lotus/weapons/tenno/longguns/loginprimary/sundialrifle"]["forms"][
        "semi_auto:semi_auto_mode"
    ]["ammo_cost"]
    == 3.0
    and _wa["/lotus/weapons/tenno/archwing/primary/thanotecharchgun/thanotecharchgun"]["forms"][
        "alt_fire:grenade_impact"
    ]["ammo_cost"]
    == 0.0,
)

# ⑥ Epitaph：b10 批按视频逐帧实测（YouTube imMX8IsPVF0：面板射速 2.10 / 蓄力 0.26，满蓄力连发 27 个
#    间隔最小 0.700、中位 0.867）定案为一般式 1/(蓄力 0.36 + 1/射速)、且不装填；b8 批的
#    max(蓄力, 1/射速) 推断作废（预测 0.476 无一样本接近）
for _nm, _fr in (("Epitaph", 1.5000001), ("Epitaph Prime", 1.7500001)):
    _w, _r = calc(_nm)
    _want = 1.0 / (0.36 + 1.0 / _fr)
    check(
        f"{_nm} 满蓄力有效射速 = 1/(0.36 + 1/{_fr:.2f}) = {_want:.4f}（视频实测；不装填）",
        not _w.get("chargeCadence") and close(_r["fire_rate_eff"], _want, 1e-4),
        str(_r["fire_rate_eff"]),
    )
    _w, _ru = calc(_nm, ["未蓄力"])
    check(
        f"{_nm} 未蓄力有效射速 = 射速 {_fr:.2f}（快射不蓄力、不装填）",
        close(_ru["fire_rate_eff"], _fr, 1e-6),
        str(_ru["fire_rate_eff"]),
    )
# 视频关键样本复算：射速 2.10 / 蓄力 0.26 ⇒ 间隔 0.26 + 1/2.10 = 0.736s（实测下限 0.700、p10 0.800）
check("葬铭视频样本复算：0.26 + 1/2.10 ≈ 0.736s", close(0.26 + 1 / 2.10, 0.7362, 1e-3))

# ⑦ 弓未蓄力：只付上弦（wfsim），射速加成不加快；Nataruk 弹匣 1 / 上弦 0.6（wiki）
_, _rpf = calc("Paris", fire_rate_pct=60.0, fire_rate_mod_pct=60.0)
check(
    "Paris 未蓄力 + 射速 60%：仍为 1/0.65（点按不吃射速）", close(_rpf["fire_rate_eff"], 1 / 0.65)
)
_w, _rn = calc("Nataruk")
check(
    "Nataruk 弹匣 1 / 上弦 0.6s（wiki；WFCD 无弹匣、装填 0.05）⇒ 快速射击 1/0.6 发/s",
    _w["magazineSize"] == 1
    and _w["reloadTime"] == 0.6
    and close(_rn["fire_rate_eff"], 1 / 0.6)
    and close(_rn["dps_sustained"], _rn["dps"]),
)
_, _rpc2 = calc("Paris", ["蓄力"])
check(
    "Paris 蓄力（弓式，b8 批）：有效射速 1/(0.5 + 0.65)、上弦已含 ⇒ no_cycle",
    close(_rpc2["fire_rate_eff"], 1 / 1.15, 1e-6) and _rpc2["no_cycle"],
)

# ⑧ 不改项：Zylok 双发扳机未建模（wfsim zylok.yaml unmodeled）；Tysis 腐蚀 DoT 单列（wfsim 未建模）
_, _rz = calc("Zylok")
check("Zylok 有效射速 = 面板 1.8（双发扳机不建模）", close(_rz["fire_rate_eff"], 1.8))

# ---------------------------------------------------------------------------
print(
    "b8 批（2026-10-10）：弹匣弹药口径 / 葬铭不装填 / 拟成枪预合并 / 弓有效射速 / 震地 / 回响冲击"
)
_ws = json.loads((ROOT / "core" / "data" / "weapons_stats.json").read_text(encoding="utf-8"))

# ① 弹匣 = wiki Magazine（弹药口径），每发耗弹只除一次
_bad = [v[0] for k, v in _bd.MAGAZINE_FIX.items() if _ws[k].get("magazineSize") != v[1]]
check(
    f"MAGAZINE_FIX {len(_bd.MAGAZINE_FIX)} 把落进 weapons_stats（wiki Magazine）",
    len(_bd.MAGAZINE_FIX) == 8 and not _bad,
    str(_bad),
)
for _nm, _args, _mag, _ac, _ratio in (
    # 持续 / 爆发 = (弹匣/(射速×耗弹)) / (… + 装填)；wfsim /api/simulate 600s 同值（报告附命令）
    ("Panthera", [], 60, 2.0, 10 / 12),  # 修前 30 发弹匣 ⇒ 0.7143
    ("Twin Grakatas", [], 120, 2.0, 0.5),  # 修前 0.3333
    ("Fulmin", [], 60, 1.0, (60 / 9.33) / (60 / 9.33 + 3)),  # 修前 6 ⇒ 0.1765
    ("Fulmin", ["半自动"], 60, 10.0, (6 / 2.17) / (6 / 2.17 + 3)),  # 修前 0.0844
    ("Staticor", [], 48, 5.0, None),
    ("Ballistica Prime", [], 32, 4.0, (8 / 3.3333335) / (8 / 3.3333335 + 1.2)),
):
    _, _r = calc(_nm, _args)
    _want = _ratio
    if _want is None:
        _t = _mag / (_r["fire_rate_eff"] * _ac)
        _want = _t / (_t + _r["reload"])
    check(
        f"{_nm}{''.join(_args)} 弹匣 {_mag} ÷ 每发耗 {_ac:g}：持续/爆发 = {_want:.4f}",
        _r["magazine"] == _mag
        and _r["ammo_cost"] == _ac
        and close(_r["dps_sustained"] / _r["dps"], _want, 1e-4),
        f"{_r['magazine']} {_r['dps_sustained'] / _r['dps']}",
    )
_, _r = calc("Convectrix")
check("Convectrix 补弹匣 70（WFCD 缺）⇒ 有持续 DPS", _r["magazine"] == 70 and _r["dps_sustained"])
_w, _r = calc("Panthera")
_cl = dc.card_lines(_w, dc.parse_args([])[0], _r, [])
check(
    "卡面：弹匣 60 ÷ 每发耗 2 弹 = 30 发",
    any("弹匣 60 ÷ 每发耗 2 弹 = 30 发" in x for x in _cl),
    "\n".join(x for x in _cl if "持续" in x),
)

# ② 葬铭系不装填（wiki Epitaph Notes + 视频实测）：持续 = 爆发；满蓄力间隔 = 0.36 + 1/射速（b10）
for _nm, _args in (
    ("Epitaph", []),
    ("Epitaph", ["未蓄力"]),
    ("Epitaph Prime", []),
    ("Epitaph Prime", ["未蓄力"]),
):
    _w, _r = calc(_nm, _args)
    check(
        f"{_nm}{''.join(_args)} 不装填 ⇒ 持续 = 爆发（修前每发付 0.6s 装填）",
        _r["no_cycle"]
        and close(_r["dps_sustained"], _r["dps"])
        and _ws[str(_w["uniqueName"]).lower()].get("noReload"),
    )
_w, _r = calc("Epitaph")
_cl = dc.card_lines(_w, dc.parse_args([])[0], _r, [])
check(
    "葬铭卡面：持续行注明不装填；Uncharged AoE 暴击 0.02（wiki；修前 WFCD 0 被回落成主段 0.48）",
    any("不装填" in x and "持续 DPS" in x for x in _cl)
    and close(next(m for m in _r["modes"] if m["name"] == "Uncharged AoE")["crit_cc"], 0.02),
)

# ③ 拟成枪系：爆炸 × min(多重, 4)，触发不随多重；合并伤害 / 奇点不吃多重
_, _r1 = calc("Synoid Simulor")
_, _r4 = calc("Synoid Simulor", ["多重300"])
_, _r5 = calc("Synoid Simulor", ["多重400"])
_ex = lambda r: next(m for m in r["modes"] if m["name"] == "Orb Explosion")["per_trigger_health"]  # noqa: E731
_mg = lambda r: next(m for m in r["modes"] if m["name"] == "Orb Merging Damage")[  # noqa: E731
    "per_trigger_health"
]
check(
    "Synoid Simulor 多重 ×4 / ×5：爆炸 = 单发 ×4（240 → 960 封顶）；合并伤害不变",
    close(_ex(_r4), 4 * _ex(_r1)) and close(_ex(_r5), 4 * _ex(_r1)) and close(_mg(_r5), _mg(_r1)),
    f"{_ex(_r1)} {_ex(_r4)} {_ex(_r5)}",
)
_, _rs1 = calc("Simulor")
_, _rs2 = calc("Simulor", ["多重150"])
check(
    "Simulor 多重 ×2.5：爆炸 ×2.5（期望 = 基础 × min(多重, 4)）；触发次数不乘多重",
    close(_ex(_rs2), 2.5 * _ex(_rs1))
    and close(_rs2["steady"]["procs_per_sec"], _rs1["steady"]["procs_per_sec"]),
)

# ④ Staticor 卡面主段 = 满蓄力（默认形态）
_w, _r = calc("Staticor")
_cl = dc.card_lines(_w, dc.parse_args([])[0], _r, [])
check(
    "Staticor 面板口径：主段 = Fully Charged Projectile（修前写成 Uncharged Projectile）",
    any(
        x.startswith("◆ 分段") and "主段 Fully Charged Projectile" in x for x in _cl
    ),  # 改版：并进分段标题
)

# ⑤ 弓（弹匣 1）：有效射速 = 1/(蓄力 + 装填)，爆发 = 持续
_, _r = calc("Kuva Bramma")
check(
    "Kuva Bramma 有效射速 1/(0.4 + 0.6) = 1，爆发 = 持续（修前爆发 2.5 发/s）",
    close(_r["fire_rate_eff"], 1.0, 1e-6) and close(_r["dps_sustained"], _r["dps"]),
    str(_r["fire_rate_eff"]),
)
_, _r = calc("Lanka")
check("Lanka（弹匣 10）不受弓式影响：仍有装填循环", not _r["no_cycle"])

# ⑦ 震地估算按 wiki 2× / 3× 武器伤害，重击震地乘重击连击倍率
_hs = lambda r: next(m for m in r["modes"] if m["name"] == "重击震地（估算）")  # noqa: E731
_sl = lambda r: next(m for m in r["modes"] if m["name"] == "震地攻击（估算）")  # noqa: E731
_, _r0 = calc("Kronen Prime")
_, _r3 = calc("Kronen Prime", ["连击40"])
check(
    "Kronen Prime 震地 424 / 重击震地 636（wiki melee 模块；修前 WFCD 848），连击 40 ⇒ ×3",
    close(_sl(_r0)["total"], 424.0)
    and close(_hs(_r0)["total"], 636.0)
    and close(_hs(_r3)["total"], 1908.0)
    and "blast" in _hs(_r0)["damage"],
    f"{_sl(_r0)['total']} {_hs(_r0)['total']} {_hs(_r3)['total']}",
)
_, _ra = calc("Ankyros Prime", ["连击40"])
check(
    "attacks 自带的 Heavy Slam Attack 同样乘重击连击倍率（384 × 3）",
    close(next(m for m in _ra["modes"] if m["name"] == "Heavy Slam Attack")["total"], 1152.0),
)

# ⑧ 回响加成「赤毒冲击25」：冲击是合法回响元素，加到冲击上（修前解析成 None ⇒ 回落成火焰）
check(
    "parse_args 赤毒冲击25 / 赤毒辐射25 / 信条磁力60 ⇒ impact / radiation / magnetic",
    [
        dc.parse_args([a])[0]["lich_bonus"]["element"]
        for a in ("赤毒冲击25", "赤毒辐射25", "信条磁力60")
    ]
    == ["impact", "radiation", "magnetic"],
)
_, _r = calc("Kuva Bramma", ["赤毒冲击25"])
_d = _r["weapon_eff"]["damage"]
check(
    "Kuva Bramma 赤毒冲击25：冲击 187 + 46.75 = 233.75，无火焰",
    close(_d["impact"], 233.75) and not _d.get("heat"),
    str({k: v for k, v in _d.items() if v}),
)


# ★ 2026-10-25（Astra 评估）：Nataruk「满蓄力」（50% / 2.2×）与「完美射击」（60% / 2.4×）是两种形态。
#   2026-10-26 起两者都从 wiki 收录 ⇒ 满蓄力按普通满蓄力算、标「蓄力」；完美射击标「完美射击」
def _nat(word):
    spec, rest = dc.parse_args(["Nataruk", word])
    w, _ = dc.find_weapon(" ".join(rest))
    r = dc.calculate(spec, w)
    return r, dc.card_lines(w, spec, r)


_r_full, _l_full = _nat("满蓄力")
_r_perf, _l_perf = _nat("完美射击")
check(
    "★ Nataruk 满蓄力：按普通满蓄力（50% / 2.2×）算、标「蓄力」，不再注明「未收录」",
    any("形态：蓄力" in x for x in _l_full)
    and not any("普通满蓄力未收录" in x for x in _l_full)
    and (_r_full.get("crit_cc"), _r_full.get("crit_cm")) == (0.5, 2.2),
    str([x for x in _l_full if "形态" in x or "完美" in x]),
)
check(
    "Nataruk 完美射击：形态标「完美射击」、不加未收录注明，数值 60% / 2.4×",
    any("形态：完美射击" in x for x in _l_perf)
    and not any("普通满蓄力未收录" in x for x in _l_perf)
    and (_r_perf.get("crit_cc"), _r_perf.get("crit_cm")) == (0.6, 2.4),
    str((_r_perf.get("crit_cc"), _r_perf.get("crit_cm"))),
)


# ★ 2026-10-25（Astra 评估）：射速曲线武器的持续 DPS 注明（凤殁灵化降速 / 起转类）
def _card(words):
    spec, rest = dc.parse_args(words)
    w, _ = dc.find_weapon(" ".join(rest))
    return dc.card_lines(w, spec, dc.calculate(spec, w))


check(
    "★ 凤殁灵化：注明 51 发内由 100% 线性降到 60%、停火重置；基础形态不注",
    any("线性降到 60%" in x and "停火重置" in x for x in _card(["Phenmor", "灵化"]))
    and not any("降到 60%" in x for x in _card(["Phenmor"])),
)
# ★ 2026-10-26 补 wiki §Notes 1x 爆头名单：Ignis / Ignis Wraith / Cyanex 主开火；Cyanex 副开火不受影响
check(
    "Ignis / Ignis Wraith / Cyanex 主开火爆头 ×1；Cyanex 副开火仍 ×3",
    all(close(calc(n, ["爆头"])[1]["head_mult"], 1.0) for n in ("Ignis", "Ignis Wraith", "Cyanex"))
    and close(calc("Cyanex", ["爆头", "副开火"])[1]["head_mult"], 3.0),
)
# ★ 2026-10-26 凤殁灵化降速建模：持续 DPS 按住扳机打空弹池（逐发降速累加），爆发仍按起始射速
_t_sd = dc.spooldown_hold_seconds(408, 13.33, 51, 0.6)
check(
    "凤殁降速：408 发用时介于满速 30.6s 与全程 60% 51.0s 之间（前 51 发线性、其后 60%）",
    close(_t_sd, sum(1 / (13.33 * (1 - 0.4 * n / 51)) for n in range(51)) + 357 / (13.33 * 0.6)),
    f"{_t_sd:.4f}",
)
_, _r_ph = calc("Phenmor", ["灵化"])
_ratio_ph = (408 / 13.33) / (_t_sd + 3.8)
check(
    "凤殁灵化：持续 = 爆发 × 名义弹匣时间 / (降速后用时 + 装填)；原型不受影响",
    close(_r_ph["dps_sustained"], _r_ph["dps"] * _ratio_ph)
    and close(calc("Phenmor")[1]["dps_sustained"], calc("Phenmor")[1]["dps"] * 10 / (10 + 2.8)),
    f"{_r_ph['dps_sustained']:.3f} vs {_r_ph['dps'] * _ratio_ph:.3f}",
)
check(
    "凤殁灵化：实战 DPS 按打空一匣的平均射速（= 短点射一行 × 名义时间 / 降速后用时）；短点射单列，原型无此行",
    close(
        _r_ph["steady"]["dps_total"],
        _r_ph["steady_burst"]["dps_total"] * (408 / 13.33) / _t_sd,
        1e-6 * _r_ph["steady_burst"]["dps_total"],
    )
    and any("短点射重置降速" in x for x in _card(["Phenmor", "灵化"]))
    and not any("短点射" in x for x in _card(["Phenmor"]))
    and calc("Phenmor")[1].get("steady_burst") is None,
    f"{_r_ph['steady']['dps_total']:.3f}",
)
check(
    "月神 Prime：注明起转（wiki 起始 25%、每发 +30%、第 4 发满速）；普通武器（Braton）不注",
    any("起转" in x and "第 1 发 25%" in x and "第 4 发满速" in x for x in _card(["Soma", "Prime"]))
    and not any("起转" in x for x in _card(["Braton"])),
)
# ★ 2026-10-26 起转建模（wiki 各武器页起始比例 + 每发增量；Astra 查证、Claude 抽 3 把复核）
_rt = dc.spoolup_rate_at(20, 10.667)
check(
    "起转曲线：Gorgon 第 1 发 20%、第 8 发 94.67%、第 9 发封顶 100%",
    close(_rt(0), 0.20) and close(_rt(7), 0.94669) and _rt(8) == 1.0,
)
check(
    "起转表到满速发数与 wiki Spool 一致（Secura Dual Cestra 原文自相矛盾，按式 6 发）",
    all(
        next(k for k in range(1, 200) if st + inc * (k - 1) >= 100 - 0.05)
        == dc._FIRE_RATE_SPOOL_SHOTS[n]
        for n, (st, inc, _) in dc._FIRE_RATE_SPOOLUP.items()
        if n != "Secura Dual Cestra"
    ),
)
_, _rg = calc("Gorgon")
_g = dc.find_weapon("Gorgon")[0]
_gm = float(_g["magazineSize"]) / float(_g.get("ammoCost") or 1)
_g_nom = _gm / _rg["fire_rate_eff"]
_g_hold = dc.ramp_hold_seconds(_gm, _rg["fire_rate_eff"], _rt)
check(
    "Gorgon：持续 = 爆发 × 名义弹匣时间 / (起转后用时 + 装填)；爆发仍按满速",
    close(
        _rg["dps_sustained"],
        _rg["dps"] * _g_nom / (_g_hold + float(_g["reloadTime"])),
        1e-6 * _rg["dps"],
    )
    and _rg["fire_rate_curve"] == "spoolup"
    and _rg.get("steady_burst") is None,
)
check(
    "起转只建在默认形态：Tenora 副开火 / Gorgon 灵化不注明、不计入；Kohm（弹片也随起转变）仍只注明",
    calc("Tenora", ["副开火"])[1]["fire_rate_curve"] == ""
    and not any("起转" in x for x in _card(["Tenora", "副开火"]))
    and calc("Gorgon", ["灵化"])[1]["fire_rate_curve"] == ""
    and all(calc(_k)[1]["fire_rate_curve"] == "" for _k in ("Kohm", "Kohmak", "Twin Kohmak"))
    and any("约 5 发后到满速" in x for x in _card(["Kohm"])),
)
check(
    "Zylok 双发扳机注明第二发在松开扳机时打出",
    any("松开时打出第二发" in x for x in _card(["Zylok"])),
)
check(
    "灵化形态注明持续 DPS 为上限（枪械灵化无时限、打空后需重新充能）",
    any("为上限" in x for x in _card(["Braton", "Prime", "灵化"])),
)

# ★ 2026-10-26 新武器入库（scripts/build_weapons_supplement.py：英文 wiki + DE 官方导出）
_wsx = json.loads((ROOT / "core" / "data" / "weapons_stats.json").read_text(encoding="utf-8"))
check(
    "武器表不再有 wfsim 来源条目（Archgun / 守护武器 / Valkyr Talons 改由 wiki + DE 生成）",
    not any("wfsim" in str(v.get("source") or "") for v in _wsx.values()),
)
_found = {
    n: dc.find_weapon(n)[0] for n in ("Daikyu Prime", "Quassus Prime", "Kuva Ghoulsaw", "霜冥差")
}
check(
    "新武器可查（含 wiki 内部名过时、键按 DE 官方的 Quassus Prime / Kuva Ghoulsaw，及中文名回落 DE 导出的霜冥差）",
    all(_found.values())
    and _found["Quassus Prime"]["uniqueName"].endswith("PrimeQuassusWeapon")
    and _found["霜冥差"]["name"] == "Nunchasa",
    str({k: (v or {}).get("uniqueName") for k, v in _found.items()}),
)
check(
    "蓄力 Archgun 默认主段 = 满蓄力（wiki 展示攻击，与原表一致）；Arbucep 一次齐射 6 枚",
    close(dc.find_weapon("Velocitus")[0]["damage"]["total"], 3200.0)
    and dc.find_weapon("Arbucep")[0]["multishot"] == 6.0,
)


# ★ 2026-10-26 灵化进化按形态取值（wiki Boar Incarnon Genesis：普通形态每颗弹丸 +12%、灵化形态 +96%；
#   Stug「灵化形态效果翻倍」）与 Sicarus「Increase Base Status Chance to 12% / 9%」按 by 处理（维护者定）
def _evo_note(words):
    spec, rest = dc.parse_args(words)
    w, _ = dc.find_weapon(" ".join(rest))
    return next((n for n in dc.calculate(spec, w)["notes"] if n.startswith("EVO")), "")


check(
    "Boar 进化触发：普通形态 +12%（注明仅灵化形态的部分未计入）、灵化形态 +96%",
    "触发+12%" in _evo_note(["Boar", "进化", "触发"])
    and "仅灵化形态生效" in _evo_note(["Boar", "进化", "触发"])
    and "触发+96%" in _evo_note(["Boar", "灵化", "进化", "触发"])
    and "未计入" not in _evo_note(["Boar", "灵化", "进化", "触发"]),
)
check(
    "Sicarus / Sicarus Prime Survivor's Edge：触发 +12% / +9%（wiki 写 to，按 by）",
    "触发+12%" in _evo_note(["Sicarus", "进化", "触发"])
    and "触发+9%" in _evo_note(["Sicarus", "Prime", "进化", "触发"]),
)
_evx = json.loads((ROOT / "core" / "data" / "evolutions.json").read_text(encoding="utf-8"))
_stug = next(o for o in _evx["stug"]["tiers"]["4"] if o["name"] == "Elemental Dominance")
check(
    "Stug Elemental Dominance：普通 +20%、灵化形态翻倍 +40%（incarnon_value）",
    any(e.get("value") == 0.2 and e.get("incarnon_value") == 0.4 for e in _stug["effects"]),
)

# ---------------------------------------------------------------------------
print("P4-AB（2026-10-26）：DE / PEP / wiki 生成，缺证标注，专精移出")
_p4a = json.loads((ROOT / "core/data/mods_wfsim_extra.json").read_text(encoding="utf-8"))
_p4b = json.loads((ROOT / "core/data/mods_stats.json").read_text(encoding="utf-8"))
check("A 每条规则带逐字段 src", all(v.get("src") for k, v in _p4a.items() if k != "_meta"))
check("B 每张计算 MOD 带逐字段 src", all(v.get("src") for v in _p4b["mods"].values()))
_p4focus = {"guardian break", "squad regen", "lethal levitation"}
check(
    "三个专精节点已移出 mods 与 names（按键和 name 两面检查）",
    all(
        k.lower() not in _p4focus and str(v.get("name", "")).lower() not in _p4focus
        for section in ("mods", "names")
        for k, v in _p4b[section].items()
    ),
)
check(
    "MOD 计算表 442 → 439，识别名表仍 727", len(_p4b["mods"]) == 439 and len(_p4b["names"]) == 727
)
check(
    "A 规则 199 → 204，直接互斥关系保持 116 对",
    len(_p4a) - 1 == 204 and _p4a["_meta"]["family_direct_pairs"] == 116,
)
_p4conds = [c for k, v in _p4a.items() if k != "_meta" for c in v.get("conditionals", [])]
check(
    "未知层数与时长保留 null，不填工程默认值",
    any(c.get("max_stacks_status") == "unknown_not_declared" for c in _p4conds)
    and all(
        c.get("max_stacks") is None
        for c in _p4conds
        if c.get("max_stacks_status") == "unknown_not_declared"
    )
    and all(
        c.get("duration") is None
        for c in _p4conds
        if c.get("duration_status") == "unknown_not_declared"
    ),
)
_p4scope = _p4a["galvanized scope"]["conditionals"]
check(
    "镀层氩晶主效果层数未知、子效果5层，时长各12秒",
    [c["max_stacks"] for c in _p4scope] == [None, 5]
    and [c["duration"] for c in _p4scope] == [12, 12],
)
_p4ff = _p4b["mods"]["fortifying fire"]
check(
    "Fortifying Fire 仅合法6级，逐级回盾0.5%至3%，额外3档忽略",
    _p4ff["max_rank"] == 5
    and _p4ff["base_drain"] == 6
    and _p4ff.get("_ignored_level_stats") == 3
    and len(_p4ff["levels"]) == 6
    and [level["conditionals"][0]["value"] for level in _p4ff["levels"]] == [0.5, 1, 1.5, 2, 2.5, 3]
    and not _p4ff["effects"],
)
check(
    "Fortifying Fire A/B 条件协议一致",
    all(
        _p4ff["conditionals"][0][k] == _p4a["fortifying fire"]["conditionals"][0][k]
        for k in ("trigger", "grants", "field", "value", "max_stacks", "duration")
    ),
)
check(
    "DE采纳：并合弹头扩散满级多重109.5 → 110",
    _p4b["mods"]["amalgam barrel diffusion"]["effects"]["multishot"] == 110,
)
check("DE采纳：压迫点满级容量14 → 9", _p4b["mods"]["pressure point"]["drain_at_max"] == 9)
check(
    "DE采纳：惩戒最高等级5 → 3、满级容量11 → 9",
    _p4b["mods"]["retribution"]["max_rank"] == 3
    and _p4b["mods"]["retribution"]["drain_at_max"] == 9,
)
check(
    "DE采纳：埋伏射击/上升打击射速从常驻移为条件",
    all(
        "fire_rate" not in _p4b["mods"][k]["effects"] and _p4b["mods"][k].get("conditionals")
        for k in ("lie in wait", "soaring strike")
    ),
)
check(
    "未重建旧字段明确标注：步枪元素师旧base_dmg保留",
    "effects.base_dmg" in _p4b["mods"]["rifle elementalist"]["_legacy_fields"]
    and _p4b["mods"]["rifle elementalist"]["effects"]["base_dmg"] == 90,
)
check(
    "新生成器已替换旧脚本",
    all((ROOT / "scripts" / n).is_file() for n in ("build_mod_rules.py", "build_mod_levels.py"))
    and not any(
        (ROOT / "scripts" / n).exists()
        for n in ("enrich_mods_from_wfsim.py", "sync_mods_from_wfsim.py")
    ),
)
_p4official = Path(_BI, "de-export-20261025") if _BI else _ABSENT
_p4pep = Path(_BI, "pep-513105d") if _BI else _ABSENT
_p4wiki = Path(_BI, "wiki-modules-20261026/mods") if _BI else _ABSENT
if all(os.path.isdir(p) for p in (_p4official, _p4pep, _p4wiki)):  # 同上
    for _p4script, _p4file in (
        ("build_mod_rules", "mods_wfsim_extra.json"),
        ("build_mod_levels", "mods_stats.json"),
    ):
        _p4target = ROOT / "core/data" / _p4file
        _p4before = hashlib.sha256(_p4target.read_bytes()).hexdigest()
        _p4args = [
            "--official",
            str(_p4official),
            "--pep",
            str(_p4pep),
            "--wiki",
            str(_p4wiki),
            "--check",
        ]
        _p4proc = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / (_p4script + ".py")), *_p4args],
            capture_output=True,
            text=True,
            timeout=60,
        )
        check(
            f"{_p4script} --check 可复现且不写盘",
            _p4proc.returncode == 0
            and hashlib.sha256(_p4target.read_bytes()).hexdigest() == _p4before,
            _p4proc.stdout + _p4proc.stderr,
        )
        # 反向护栏：改动临时产物，--check 应返回1且不能偷偷修复文件。
        with tempfile.TemporaryDirectory() as _p4tmp:
            _p4copy = Path(_p4tmp) / _p4file
            _p4changed = json.loads(_p4target.read_text(encoding="utf-8"))
            _p4changed["_meta"]["p4_check_negative_probe"] = True
            _p4copy.write_text(
                json.dumps(_p4changed, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
            )
            _p4badsha = hashlib.sha256(_p4copy.read_bytes()).hexdigest()
            _p4code = "import sys; from pathlib import Path; sys.path.insert(0,sys.argv[1]); name=sys.argv[2]; target=sys.argv[3]; rest=sys.argv[4:]; m=__import__(name); m.OUT=Path(target); sys.argv=[name]+rest; r=m.main(); raise SystemExit(r)"
            _p4bad = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    _p4code,
                    str(ROOT / "scripts"),
                    _p4script,
                    str(_p4copy),
                    *_p4args,
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )
            check(
                f"{_p4script} --check 检出差异且不改临时产物",
                _p4bad.returncode == 1
                and hashlib.sha256(_p4copy.read_bytes()).hexdigest() == _p4badsha,
                _p4bad.stdout + _p4bad.stderr,
            )
else:
    print("[SKIP] 本机无 DE / PEP / MOD wiki 固定快照：跳过 P4-AB --check 正反向复现断言")

# ★ 2026-10-26 P4-AB 接入（Claude）：镀层显示 / 换行 / 派系百分数换算
_cl = _card(["Soma", "Prime", "镀层氩晶瞄具", "满镀层", "爆头"])
check(
    "满镀层：已按层数计入的镀层卡不再列为「条件触发未计入」，层数原文未写的注明按 1 层",
    any("镀层已计入" in x for x in _cl)
    and not any(x.startswith("· 条件触发") and "氩晶瞄具" in x for x in _cl)
    and any("层数原文未写，按 1 层" in x for x in _cl),
    str([x for x in _cl if "镀层" in x or "条件" in x]),
)
check(
    "不写镀层层数时仍列为「条件触发未计入」",
    any(
        x.startswith("· 条件触发") and "分裂膛室" in x for x in _card(["Acceltra", "镀层分裂膛室"])
    ),
)
_ms = json.loads((ROOT / "core" / "data" / "mods_stats.json").read_text(encoding="utf-8"))["mods"]
check(
    "mods_stats 条件原文不含回车（DE 原文 \\r\\n 已统一为 \\n）",
    not any(
        "\r" in str(c)
        for v in _ms.values()
        for c in (v.get("effects") or {}).get("conditional") or []
    ),
)
check(
    "派系卡 faction_dmg 由 DE 乘数换算（x1.3 → 30），不再标历史保留",
    _ms["bane of grineer"]["effects"]["faction_dmg"] == 30.0
    and not _ms["bane of grineer"].get("_legacy_fields")
    and _ms["primed bane of grineer"]["effects"]["faction_dmg"] == 55.0,
    str(_ms["primed bane of grineer"]["effects"]),
)

# ★ 2026-10-26：只剩变体词时不做子串模糊匹配（「布拉顿 Prime」此前被配成翁 Prime 照算）
check(
    "is_variant_only：只有变体词（Prime / 赤毒 / Vandal / p）拦截；正常名与变体前缀写法不受影响",
    all(dc.is_variant_only(q) for q in ("Prime", "赤毒", "Vandal", "p"))
    and not any(dc.is_variant_only(q) for q in ("布莱顿 Prime", "绝路p", "赤毒沙皇", "Soma")),
)


def _wname(cmd):
    _s, _nt = dc.parse_args(cmd.split())
    _q = " ".join(_nt).strip()
    _w = None if dc.is_variant_only(_q) else dc.find_weapon(_q)[0]
    return _w and _w["name"]


check(
    "伤害指令入口：「布拉顿 Prime」不再配成翁 Prime；Tenet / 棱晶 前缀写法仍认对变体",
    _wname("布拉顿 Prime 膛线") is None
    and _wname("Tenet Arca Plasmor") == "Tenet Arca Plasmor"
    and _wname("棱晶 Gorgon") == "Prisma Gorgon"
    and _wname("赤毒 布拉玛 分裂膛室") == "Kuva Bramma",
)


# ★ 2026-10-26 条件扫描扩展：源文案正反护栏，禁止跨武器效果降格。
sys.path.insert(0, str(ROOT / "scripts"))
_opt_rules = __import__("build_mod_rules")


def _opt_parse(text):
    return _opt_rules.parse_scanned_conditionals(
        {"fusionLimit": 0, "levelStats": [{"stats": [text]}]}, "DE fixture"
    )


_opt_new = {
    "double tap": ("base_dmg", 20, 2),
    "final tap": ("base_dmg", 100, 0.2),
    "gorgon frenzy": ("fire_rate", 30, 3),
    "sudden justice": ("fire_rate", 30, 2),
    "triple tap": ("base_dmg", 40, 0.2),
    "vigorous swap": ("base_dmg", 165, 3),
}
check("条件扫描范围来自439张计算卡", _p4a["_meta"]["conditional_scan_cards"] == len(_ms) == 439)
check(
    "新增6卡字段数值时长来自DE且保留条件原文",
    all(
        [(c["field"], c["value"], c["duration"]) for c in _p4a[n]["conditionals"]] == [want]
        and all(c["condition_text"] and c["mappable"] for c in _p4a[n]["conditionals"])
        for n, want in _opt_new.items()
    ),
)
_opt_biotic_text = "On Weak Point Kill:\n+150% Viral and Magnetic Damage and Status Chance for 15s"
_opt_biotic, _opt_biotic_audit = _opt_parse(_opt_biotic_text)
check(
    "复合元素与异常效果不能完整映射时整句跳过、原文审计保留",
    not _opt_biotic
    and any(
        row["raw"] == _opt_biotic_text and row["status"] == "skipped" for row in _opt_biotic_audit
    )
    and "conditionals" not in _p4a.get("biotic rounds", {})
    and any(
        row["name"] == "biotic rounds"
        and row["raw"] == _opt_biotic_text
        and row["status"] == "skipped"
        for row in _p4a["_meta"]["conditional_scan_audit"]
    ),
)
_opt_status, _opt_status_audit = _opt_parse("On Kill:\n+150% Status Chance for 15s")
check(
    "完整简单异常几率效果仍可映射",
    len(_opt_status) == 1
    and not _opt_status_audit
    and _opt_status[0]["field"] == "status_chance"
    and _opt_status[0]["value"] == 150
    and _opt_status[0]["duration"] == 15,
)
_opt_unknown, _ = _opt_parse("On Kill:\n+30% Fire Rate")
check(
    "未声明层数和时长均null并标unknown，不补1",
    len(_opt_unknown) == 1
    and _opt_unknown[0]["max_stacks"] is None
    and _opt_unknown[0]["duration"] is None
    and _opt_unknown[0]["max_stacks_status"] == "unknown_not_declared"
    and _opt_unknown[0]["duration_status"] == "unknown_not_declared",
)
_opt_window, _ = _opt_parse("On 2 Hits within 0.2s:\n+30% Fire Rate for 2s")
check(
    "连击计数窗口保留完整条件原文",
    len(_opt_window) == 1 and "2 Hits within 0.2s" in _opt_window[0]["condition_text"],
)
for _opt_text in (
    "On Hit:\n+20% Secondary Weapon Fire Rate for 3s",
    "On Melee Kill:\n+100% Reload Speed\n+100% Magazine Capacity for 12s on Secondary Weapon",
    "On 6 Melee Kills within 6s:\n+120% Status and Critical Chance for Secondary Weapon for 24s",
    "On Consecutive throw (Max stacks 3):\n+100% Throw Damage",
    "On Equip:\nSquad gains 60% Weapon Damage for 3s",
    "On Unknown Trigger:\n+30% Fire Rate for 2s",
    "On Hit:\n+30% Fire Rate for 2s only with unsupported target",
):
    _opt_effects, _opt_audit = _opt_parse(_opt_text)
    check(
        "未支持作用域明确跳过并留审计：" + _opt_text.split("\n")[0],
        not _opt_effects and bool(_opt_audit),
    )
check(
    "跨武器Plan B、兽王标记、连击狂怒不新增自增条件",
    all(
        "conditionals" not in _p4a.get(n, {}) for n in ("plan b", "mark of the beast", "combo fury")
    ),
)

# ★ 2026-10-26：武器名写错时提示按原文显示（「没找到『布拉顿 Prime』」，此前显示「没找到『Prime』」）
from core.commands.wiki_misc import WikiMiscCommands  # noqa: E402
from core.parser import Parsed  # noqa: E402

_dr = WikiMiscCommands.__new__(WikiMiscCommands)._damage_reply
check(
    "伤害指令：武器名写错时回复「没找到『布拉顿 Prime』」并列出没认出的词，不配到别的武器",
    _dr(Parsed(content="布拉顿 Prime 膛线".split())).lines[:2]
    == ["◆ 原因：武器库里没找到「布拉顿 Prime」", "◆ 没认出来的词：布拉顿"],
    str(_dr(Parsed(content="布拉顿 Prime 膛线".split())).lines[:2]),
)

print()
if FAILED:
    print(f"✗ {len(FAILED)} 项失败：" + "、".join(FAILED))
    sys.exit(1)
print("✓ 全部通过")
