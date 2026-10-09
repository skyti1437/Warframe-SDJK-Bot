# -*- coding: utf-8 -*-
"""伤害计算崩溃回归（python3 tests/test_damage_crash_regress.py）

2026-10-09 修的两处崩溃，各带最小复现：

  ① 范围段衰减形状不一致：wfsim 范围段的 falloff_reduction 是**裸数字**（0–1），
     warframe-items 的 attacks 段是 {start, end, reduction}；calculate 把裸数字原样
     塞进 res["modes"][i]["falloff"]，card_lines 对它 .get → AttributeError。
     默认参数即崩 9 把：Komorex / Trumna Prime / Grattler / Kuva Ayanga /
     Kuva Grattler / Mausolon / Morgha / Multron / Vulcax。
  ② 「关键词+可选数字」正则：`镀层 / 异常 / 连击 / 连投`（含异常种类 / 异常数 /
     投掷层数）的数字写成可选，裸词也命中 → re.search(...).group 对 None 崩。
     「镀层 分裂膛室」（带空格的 MOD 名）首词恰是「镀层」，玩家照官方名打就崩。

入口走真实链路（core.parser.parse → dc.parse_args → calculate → card_lines），不联网。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import damage_calc as dc  # noqa: E402
from core.parser import parse  # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  ✓ " if cond else "  ✗ ") + name + ("" if cond else f"  —— {detail}"))
    if not cond:
        FAILED.append(name)


def _card(text: str) -> tuple[dict, list[str]]:
    """「伤害 …」整条指令 → (spec, 卡面行)；崩溃原样抛出。"""
    p = parse(text)
    assert p.command == "damage", p.command
    spec, wt = dc.parse_args(p.content)
    w, alts = dc.find_weapon(" ".join(wt))
    assert w, f"武器没找到：{wt}"
    return spec, dc.card_lines(w, spec, dc.calculate(spec, w), alts)


def _safe(fn, *a):
    try:
        return fn(*a), None
    except Exception as exc:  # noqa: BLE001 —— 回归用例要把崩溃变成断言失败
        return None, f"{type(exc).__name__}: {exc}"


# ---------------------------------------------------------------------------
# ① 范围段 falloff
# ---------------------------------------------------------------------------
print("① 范围段衰减（falloff）")
_r, _err = _safe(_card, "伤害 Kuva Ayanga")
check("最小复现：伤害 Kuva Ayanga 不崩", _err is None, str(_err))
if _r:
    check(
        "Kuva Ayanga 范围段显示「射程内衰减 50%」",
        # 2026-10-09：同签名去重后保留 attacks 段（段名 Area Attack），衰减由 wfsim 段补上
        any("射程内衰减 50%" in ln for ln in _r[1]),
        str([ln for ln in _r[1] if "范围" in ln]),
    )
_r, _err = _safe(_card, "伤害 Kuva Ayanga 空战")
check("伤害 Kuva Ayanga 空战 不崩", _err is None, str(_err))

# 形状契约：res["modes"][*]["falloff"] 只能是 None 或 dict（两份数据源归一）
_bad_shape, _crash = [], {}
for _mode in ([], ["空战"], ["基础形态"], ["灵化"]):
    for _w in dc._load()["weapons"].values():
        _s, _ = dc.parse_args(list(_mode))
        _res, _e = _safe(dc.calculate, _s, _w)
        if _e:
            _crash[(_w.get("name"), tuple(_mode))] = _e
            continue
        for _m in _res.get("modes") or []:
            if _m.get("falloff") is not None and not isinstance(_m["falloff"], dict):
                _bad_shape.append((_w.get("name"), _m.get("name"), _m["falloff"]))
        _, _e = _safe(dc.card_lines, _w, _s, _res, [])
        if _e:
            _crash[(_w.get("name"), tuple(_mode))] = _e
_n_w = len(dc._load()["weapons"])
check(
    f"全武器 {_n_w} 把 × 4 模式（默认/空战/基础形态/灵化）卡面零崩溃",
    not _crash,
    str(list(_crash.items())[:3]),
)
check("modes[*].falloff 形状统一为 None 或 dict", not _bad_shape, str(_bad_shape[:3]))

# card_lines 读取兜底：段结构里出现裸数字也按衰减比例读，不崩
_w_k, _ = dc.find_weapon("Kuva Ayanga")
_s_k, _ = dc.parse_args([])
_res_k = dc.calculate(_s_k, _w_k)
_res_k["modes"] = [dict(m, falloff=0.3) for m in _res_k["modes"]] or [
    {
        "name": "x",
        "damage": {"blast": 1.0},
        "total": 1.0,
        "health": 1.0,
        "health_crit": 1.0,
        "falloff": 0.3,
    }
]
_lines, _err = _safe(dc.card_lines, _w_k, _s_k, _res_k, [])
check(
    "card_lines 兜底：裸数字 falloff 不崩且按比例显示",
    _err is None and any("射程内衰减 30%" in ln for ln in _lines or []),
    str(_err),
)

# ---------------------------------------------------------------------------
# ② 「关键词」后缺数字
# ---------------------------------------------------------------------------
print("② 关键词后缺数字（镀层 / 异常 / 连击 / 连投）")
_r, _err = _safe(_card, "伤害 Soma 镀层 分裂膛室")
check("最小复现：伤害 Soma 镀层 分裂膛室 不崩", _err is None, str(_err))
if _r:
    check(
        "「镀层 分裂膛室」按 MOD 认出（多重 80，无未识别）",
        [m.get("name") for m in _r[0]["mods"]] == ["Galvanized Chamber"]
        and abs(_r[0]["multishot"] - 80.0) < 1e-9
        and not _r[0]["unknown"],
        f"{[m.get('name') for m in _r[0]['mods']]} / {_r[0]['multishot']} / {_r[0]['unknown']}",
    )
_r, _err = _safe(_card, "伤害 Soma 镀层 分裂膛室 满镀层")
check(
    "镀层 分裂膛室 + 满镀层 → 多重 230（80+30×5，与无空格写法同口径）",
    _err is None and abs(_r[0]["multishot"] - 230.0) < 1e-9,
    str(_err or _r[0]["multishot"]),
)
for _kw in ("镀层", "异常", "异常种类", "异常数", "连击", "连投", "投掷层数"):
    _r, _err = _safe(_card, f"伤害 Soma {_kw}")
    check(
        f"裸词「{_kw}」不崩、进「未识别」（不被 MOD 子串吞掉）",
        _err is None and _r[0]["unknown"] == [_kw] and not _r[0]["mods"],
        str(_err or (_r[0]["unknown"], [m.get("name") for m in _r[0]["mods"]])),
    )
# 带数字的写法不受影响
_s, _ = dc.parse_args(["镀层3", "异常4", "连击120", "连投2"])
check(
    "带数字照旧：镀层3 / 异常4 / 连击120 / 连投2",
    (_s["galv_stacks"], _s["status_types"], _s["combo_hits"], _s["throw_stacks"]) == (3, 4, 120, 2),
    str((_s["galv_stacks"], _s["status_types"], _s["combo_hits"], _s["throw_stacks"])),
)

print()
if FAILED:
    print(f"✗ {len(FAILED)} 项失败：" + "、".join(FAILED))
    sys.exit(1)
print("✓ 全部通过")
