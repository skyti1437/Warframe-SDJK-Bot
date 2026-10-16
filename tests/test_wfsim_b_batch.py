# -*- coding: utf-8 -*-
"""wfsim 对照 B 批回归（python3 tests/test_wfsim_b_batch.py）

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

import json
import sys
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

# 敌人头部真实值（scripts/merge_wfsim_enemies.py --head-only）
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
for _nm in ("MOA", "Charger", "Isolator Bursa"):
    _, _r = calc("Braton Prime", ["对", _nm, "爆头"])
    check(f"{_nm} 头部 1.0（无暴击爆头）", close(_r["head_mult"], 1.0) and not _r["head_crit"])
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
print("生成器批（2026-10-10，wfsim @8267f09）：范围段继承 / 子炸弹 count / 多重 / 范围段 CO")
_wa = json.loads((ROOT / "core" / "data" / "weapon_attacks.json").read_text(encoding="utf-8"))
_if = json.loads((ROOT / "core" / "data" / "incarnon_forms.json").read_text(encoding="utf-8"))
_segs = [s for k, v in _wa.items() if k != "_meta" for s in v.get("segments") or []]
check(
    "weapon_attacks：默认形态里没有未经出处的 takes_multishot: false（白名单全是灵化形态）",
    not any(s.get("takes_multishot") is False for s in _segs),
)
check(
    "weapon_attacks：_meta 记录 wfsim 来源 commit",
    len(str(_wa["_meta"].get("wfsim_commit") or "")) == 40
    and _wa["_meta"]["wfsim_commit"] == _if["_meta"].get("wfsim_commit"),
)
check(
    "Sporelacer / Tombfinger 键冲突拆成 #primary / #secondary（不再后写覆盖）",
    sum(1 for k in _wa if k.endswith(("#primary", "#secondary"))) == 4,
)
_kb = _wa["/lotus/weapons/grineer/bows/grnbow/grnbowweapon"]["segments"]
check(
    "Kuva Bramma：子炸弹接触 + 爆炸两段，count 3",
    [(s["id"], s.get("count")) for s in _kb[1:]] == [("cluster", 3.0), ("cluster_radial", 3.0)],
    str(_kb),
)
_inc_flags = {
    v["form_id"]: (v["radial"].get("takes_multishot"), v["radial"].get("takes_condition_overload"))
    for v in _if.values()
    if isinstance(v, dict) and v.get("radial") and v.get("form_id")
}
check(
    "incarnon_forms：Burston / Zylok / Braton 灵化范围段 = 不吃多重 + 吃 CO（有出处）",
    all(
        _inc_flags.get(f) == (False, True)
        for f in ("burston_incarnon", "zylok_prime_incarnon", "braton_prime_incarnon")
    ),
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
    "Trumna Prime 多重100：范围段吃多重 73.74 → 88.33（含伤害量化）",
    close(_st("Trumna Prime", ["多重100"]), 88.3304, 1e-5),
    str(_st("Trumna Prime", ["多重100"])),
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
    "Onos 灵化蓄力模式的 Radial Attack 不计入（wfsim 灵化无此 radial）", not _ro["segments_total"]
)
# 2026-10-10 第四批改断言（维护者定「Nataruk 口径错位」）：我方主段 = 快速射击（wfsim
# nataruk.yaml，co 缺省 additive），完美射击（wfsim default_form nataruk_perfect，independent）
# 改走「蓄力」形态 ⇒ 默认桶别 additive；independent 见文末第四批段的「蓄力」断言。
check(
    "Nataruk 默认 = 快速射击（wfsim nataruk.yaml）CO 桶别 = additive_with_base_damage",
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

# ② 敌人头部（merge_wfsim_enemies.py 补丁表，--head-only 可重复执行）
_leg = dc.find_enemy_exact("Legacyte")
check(
    "Legacyte：head_none → 头部 ×3、无暴击爆头（Body_Parts「Head (3x)*」+ 本页）",
    _leg["head_mul"] == 3.0 and not _leg.get("head_none") and _leg.get("head_crit") is False,
    str(_leg),
)
_bab = dc.find_enemy_exact("Techrot Babau")
check(
    "Techrot Babau：保持 Module 1.5x，head_src 写明三处不一致",
    _bab["head_mul"] == 1.5 and "Phone Head" in _bab["head_src"],
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
# 2026-10-10 b5 批改断言：132/67 → 133/69。MAIN_FORM_OVERRIDE 扩到弓 / Drakgoon / Zenith /
# Kuva Quartakk / Miter：Miter 的满蓄力（wfsim 只有这一条）降级进 forms（+1 形态）；Evensong
# 满蓄力（带 Charged Radial 范围段）与 Mutalist Cernos 满蓄力（带毒云段）由顶层降级进 forms
# （+2 带额外段）。其余组是「默认 ↔ 形态」对调，总数不变。
check(
    "forms：133 个非默认形态，其中带额外段 69（灵化 26 + 副开火/蓄力/未蓄力 43）",
    _n_forms == 133 and _n_seg_forms == 69,
    f"{_n_forms} / {_n_seg_forms}",
)
check(
    "只有非默认形态的母武器记录不带顶层 segments / main（默认口径读不到 forms）",
    all(
        "segments" not in v and "main" not in v
        for k, v in _wa.items()
        if k != "_meta" and v.get("forms") and not v.get("yaml_id")
    ),
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
import merge_wfsim_enemies as _me  # noqa: E402

# ① 敌人表 = 保留名单 19 个，官方中文名补齐
check(
    "enemies.json = KEEP_ENEMIES（19 个，顺序无关）",
    set(_en) == set(_me.KEEP_ENEMIES) and len(_en) == 19,
    str(sorted(set(_en) ^ set(_me.KEEP_ENEMIES))),
)
check("19 个敌人都有中文名", all(v.get("zh") for v in _en.values()))
check(
    "官方简中补齐（dict.zh.json）：吸血鱼鹰 / 隔离金流恐鸟 / 传承种 / 科腐者疫胞 / 毒化者",
    [_en[n]["zh"] for n in ("Leech Osprey", "Isolator Bursa", "Legacyte", "Techrot Babau", "Nox")]
    == ["吸血鱼鹰", "隔离金流恐鸟", "传承种", "科腐者疫胞", "毒化者"],
)
# 删除护栏双向（AGENTS 硬纪律 9）：该删的删、该留的留；名单缺条目直接报错
_tbl = {n: {"name": n} for n in (*_me.KEEP_ENEMIES, "Butcher", "Wolf of Saturn Six")}
_drop = _me.apply_keep(_tbl)
check(
    "apply_keep：删非名单（Butcher / Wolf of Saturn Six），留名单 19 个，再跑不删",
    _drop == ["Butcher", "Wolf of Saturn Six"]
    and set(_tbl) == set(_me.KEEP_ENEMIES)
    and _me.apply_keep(_tbl) == [],
    str(_drop),
)
try:
    _me.apply_keep({"Lancer": {"name": "Lancer"}})
    _raised = False
except SystemExit:
    _raised = True
check("apply_keep：名单里的敌人缺失时报错（不静默少一条）", _raised)

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
    _me.HEAD_PATCH["Swarm Mutalist MOA"][0] == 4.5
    and _me.HEAD_PATCH["Tar-Mutalist MOA"][0] == 4.5
    and _me.HEAD_CRIT_OVERRIDE.get("Tar-Mutalist MOA") is False
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
    "weapon_attacks：Nataruk main_form=base，forms 只有 charged:nataruk_perfect（independent）",
    _nat.get("main_form") == "base"
    and list(_nat["forms"]) == ["charged:nataruk_perfect"]
    and _nat["forms"]["charged:nataruk_perfect"].get("co_behavior") == "independent"
    and "charge_seconds" not in _nat,
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
print("b5 批（2026-10-10）：主段按攻击段取值 / 物理对调残留 / 默认形态 ≠ wfsim default_form")
sys.path.insert(0, str(ROOT))
import build_damage_data as _bd  # noqa: E402
import build_weapon_attacks as _bwa  # noqa: E402

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

# ⑤ 默认形态 ≠ wfsim default_form 的组：关键词落到正确的 forms 条目
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
# wfsim default_form 的 yaml id 一般 = 组名；Nataruk 的是 nataruk_perfect（第四批）
_DEMOTED = {"nataruk": "nataruk_perfect"}
_bow_ok = []
for _g in _bwa.MAIN_FORM_OVERRIDE:
    _did = _DEMOTED.get(_g, _g)
    _rec = next(
        (
            v
            for k, v in _wa.items()
            if k != "_meta" and any(f.split(":", 1)[1] == _did for f in (v.get("forms") or {}))
        ),
        None,
    )
    _bow_ok.append(bool(_rec) and bool(_rec.get("main_form")) and "charge_seconds" not in _rec)
check(
    f"MAIN_FORM_OVERRIDE {len(_bwa.MAIN_FORM_OVERRIDE)} 组：wfsim default_form 都降级进 forms，"
    "记录带 main_form、顶层不再带默认形态的 charge_seconds",
    all(_bow_ok),
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

# ④ 维护者定：布里斯提卡 Prime 蓄力保留 152（wfsim M66 实测），每发耗 4 弹（wiki secondary:1949）
_, _rbc = calc("Ballistica Prime", ["蓄力"])
_mt = 32 / 4 / _rbc["fire_rate_eff"]  # b8 批：弹匣按 wiki 弹药口径 32（修前 WFCD 8 ⇒ 8/4 = 2 发）
check(
    "Ballistica Prime 蓄力基伤 152 不变；每发耗 4 弹 ⇒ 持续弹匣 32/4 = 8 发（wiki「8-shot magazine」）",
    close(_rbc["weapon_eff"]["damage"]["total"], 152.0)
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
    "weapon_attacks forms：Zenith 半自动 ammo_cost 3、Cortege 副开火 0（wiki，覆盖 wfsim 1）",
    _wa["/lotus/weapons/tenno/longguns/loginprimary/sundialrifle"]["forms"]["semi_auto:zenith"][
        "ammo_cost"
    ]
    == 3.0
    and _wa["/lotus/weapons/tenno/archwing/primary/thanotecharchgun/thanotecharchgun"]["forms"][
        "alt_fire:cortege_alt"
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

print()
if FAILED:
    print(f"✗ {len(FAILED)} 项失败：" + "、".join(FAILED))
    sys.exit(1)
print("✓ 全部通过")
