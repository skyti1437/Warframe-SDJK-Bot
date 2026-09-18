# -*- coding: utf-8 -*-
"""v1.9 准确性自测：多武器 × 多配卡，逐条对照 wiki 数值。

不是为了「跑通」，是为了**抓算错**。每条断言都写着 wiki 出处与手算期望值，
改公式后必须全绿。跑法：

    python tests/test_damage_accuracy.py
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import damage_calc as dc  # noqa: E402

FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("  [PASS] " if ok else "  [FAIL] ") + name + (f"  {detail}" if detail and not ok else ""))
    if not ok:
        FAILED.append(name)


def close(a: float, b: float, tol: float = 1e-6) -> bool:
    return abs(a - b) <= tol * max(1.0, abs(b))


def run(toks: str):
    spec, name = dc.parse_args(toks.split())
    w, alts = dc.find_weapon(" ".join(name))
    assert w is not None, f"武器没找到：{toks}"
    return w, spec, dc.calculate(spec, w), alts


print("=" * 76)
print("① 狂怒（Blood Rush）—— wiki：CC = 基础 ×[1 + MOD加成 + 狂怒×(连击倍率−1)]")
# wiki 例子口径：20% 基础 + 斩铁(1.2) + 狂怒(0.4) 在连击倍率 4.0 时 → 68%
# 这里用同一式子反推：连击 60 → 倍率 4.0
_w, _s, _r, _ = run("Skana G系 100级 斩铁 急进猛突 连击60")
_exp = 0.05 * (1 + 1.2 + 0.4 * (4.0 - 1.0))
check("Skana 5%×(1+1.2+0.4×3) = 23%", close(_r["crit_cc"], _exp),
      f"{_r['crit_cc']} vs {_exp}")
_w0, _s0, _r0, _ = run("Skana G系 100级 急进猛突 连击0")
check("0 连击时狂怒不加成（倍率 1.0 → ×0）", close(_r0["crit_cc"], 0.05))

print()
print("② 创口溃烂（Weeping Wounds）—— wiki：SC = 基础×(1+MOD)×(1+创口×连击倍率)")
_w, _s, _r, _ = run("Skana G系 100级 创口溃烂 连击60")
_base_sc = _w.get("procChance") or 0.0
check(f"Skana 触发 {_base_sc:.4f}×(1+0.4×4) = {_base_sc * 2.6:.4f}",
      close(_r["steady"]["status_chance"], _base_sc * 2.6),
      f"{_r['steady']['status_chance']} vs {_base_sc * 2.6}")

print()
print("③ 异况超量（Condition Overload）—— wiki：Total = 基础×[1+伤害MOD+(CO×n)]×(1+元素)")
_wA, _, _rA, _ = run("Skana G系 100级")
_wB, _, _rB, _ = run("Skana G系 100级 异况超量 异常1")
_wC, _, _rC, _ = run("Skana G系 100级 异况超量 异常3")
check("1 种异常 → ×(1+0.8)", close(_rB["health"], _rA["health"] * 1.8),
      f"{_rB['health']} vs {_rA['health'] * 1.8}")
check("3 种异常 → ×(1+2.4)（线性，不是相乘）",
      close(_rC["health"], _rA["health"] * 3.4),
      f"{_rC['health']} vs {_rA['health'] * 3.4}")

print()
print("④ 架势（Stance）—— wiki Module:Stances/data，取站立连招每段平均倍率")
_w, _, _r, _ = run("Skana G系 100级 钢铁凤凰")
check("钢铁凤凰（Iron Phoenix）Neutral 每段 ×2.3333",
      close(_r["stance"]["mult"], 7 / 3, 1e-4), str(_r["stance"]))
check("架势倍率已折进伤害（裸武器 ×2.3333）",
      close(_r["health"], _rA["health"] * 7 / 3, 1e-4),
      f"{_r['health']} vs {_rA['health'] * 7 / 3}")
_w2, _, _r2, _ = run("Skana G系 100级 猎鹰俯击")
check("猎鹰俯击（Swooping Falcon）Neutral 平均 240% + 强制切割",
      close(_r2["stance"]["mult"], 2.4, 1e-4)
      and "slash" in _r2["stance"]["procs"], str(_r2["stance"]))

print()
print("⑤ 物理转换 —— wiki：only converts physical damage，总量不变")
def raw_types(toks: str) -> dict[str, float]:
    """MOD 后的原始伤害构成（不含派系倍率/护甲减免）—— 用来验「转换保总量」。

    ⚠️ 不能用 per_type_health 验：转换会改变各类型的占比，而各类型的派系
    倍率不同（G系弱冲击），总值本来就会变 —— 那是正确行为，不是 bug。
    """
    spec, name = dc.parse_args(toks.split())
    w, _ = dc.find_weapon(" ".join(name))
    d = w["damage"]
    pt, _, _, _ = dc._hit_from_damage(d, float(d.get("total") or 0.0), spec,
                                      dc._load()["fac_table"], "Grineer",
                                      0.0, 1.0, 1.0)
    return pt


_a = raw_types("Soma G系 100级")
_b = raw_types("Soma G系 100级 穿刺弹")
check("转换后 MOD 后总伤不变（20% 物理搬去穿刺）",
      close(sum(_a.values()), sum(_b.values()), 1e-6),
      f"{sum(_a.values())} vs {sum(_b.values())}")
check("穿刺那一档确实变高了", _b.get("puncture", 0) > _a.get("puncture", 0),
      f"{_a.get('puncture')} → {_b.get('puncture')}")
check("冲击/切割被按比例扣掉",
      _b.get("impact", 0) < _a.get("impact", 0)
      and _b.get("slash", 0) < _a.get("slash", 0))

print()
print("⑥ 多段判定（同一把武器各段独立算）")
_wx, _, _rx, _ = run("驱魔之刃 对 重机枪手 100级 熔岩冲击 北风 斩铁")
_names = [m["name"] for m in _rx["modes"]]
check("驱魔之刃有多段判定（投掷/爆炸/震地…）", len(_rx["modes"]) >= 5, str(_names))
check("每段都有独立数值且非负",
      all(m["health"] >= 0 and not math.isnan(m["health"]) for m in _rx["modes"]))
check("投掷爆炸（电击250）比普通攻击高",
      any("Bounce" in m["name"] and m["health"] > _rx["health"] for m in _rx["modes"]))

print()
print("⑦ 跨类别冒烟：不同武器 × 不同配卡都不能崩、不能出非法值")
CASES = [
    "绝路p 对 重机枪手 100级 膛线 分裂膛室 地狱火 关键延迟 弱点感应 锁定目标",
    "赤毒布拉玛 G系 100级 基伤165 多重90 电90 毒90 病毒10",
    "Soma 对 船员 60级 膛线 致命一击 弱点感应 触发150",
    "驱魔之刃 对 重机枪手 100级 熔岩冲击 北风 斩铁 奋力一掷 投掷",
    "Skana 对 重机枪手 100级 钢铁凤凰 异况超量 急进猛突 创口溃烂 连击120",
    "瓦斯托 C系 80级 电90 毒90 磁力10 超宏 超宏5000",
    "布拉玛 低语者 120级 毒90 冰90 适应4 钢路",
    "绝路p 对 Eidolon Teralyst 100级 爆头",
]
for toks in CASES:
    try:
        w, spec, r, _ = run(toks)
    except Exception as exc:  # noqa: BLE001
        check(f"冒烟 {toks[:30]}", False, f"{type(exc).__name__}: {exc}")
        continue
    bad = []
    for k in ("health", "shield", "dps", "armor", "dr", "crit_exp"):
        v = r.get(k)
        if v is None:
            continue
        if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
            bad.append(f"{k}={v}")
    if r.get("health", 0) < 0 or r.get("shield", 0) < 0:
        bad.append("负伤害")
    if r.get("dr") is not None and not (0 <= r["dr"] <= 0.9 + 1e-9):
        bad.append(f"DR越界={r['dr']}")
    if r.get("armor") is not None and not (0 <= r["armor"] <= 2700 + 1e-6):
        bad.append(f"护甲越界={r['armor']}")
    ss = r.get("steady") or {}
    if ss.get("dps_total") is not None and ss["dps_total"] < ss["dps_direct"] - 1e-6:
        bad.append("实战DPS < 直伤")
    check(f"冒烟 {toks[:34]}", not bad, "；".join(bad))

print()
print("⑧ 单调性：加成往上加，伤害不应下降")
_wS, _, _base, _ = run("Soma G系 100级")
prev = _base["health"]
for extra in ("膛线", "膛线 分裂膛室", "膛线 分裂膛室 地狱火"):
    _, _, rr, _ = run(f"Soma G系 100级 {extra}")
    check(f"配卡 {extra} 不低于裸武器", rr["health"] >= prev - 1e-9,
          f"{rr['health']} vs {prev}")
    prev = max(prev, rr["health"])
_, _, _rlow, _ = run("Soma G系 100级")
_, _, _rhigh, _ = run("Soma G系 200级")
check("同配卡打更高级敌人：护甲更高 → 对血更低（或都顶上限时相等）",
      _rhigh["health"] <= _rlow["health"] + 1e-9,
      f"{_rhigh['health']} vs {_rlow['health']}")

print()
if FAILED:
    print(f"失败 {len(FAILED)} 项：{FAILED}")
    sys.exit(1)
print("全部通过 ✔")
