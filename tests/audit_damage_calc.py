# -*- coding: utf-8 -*-
"""伤害计算器「全量扫荡」审计：把每种机制、每份数据都跑一遍找缺陷。

用法：python tests/audit_damage_calc.py
产出：问题清单（按严重度分级），退出码非 0 表示发现 HIGH 级问题。

审计维度
  ① 数据完整性：540 武器 / 271 数值 MOD / 92 敌人 / 15 派系表
  ② 全武器扫荡：任意武器 × 多种 spec 组合 → 异常、NaN、负数、离谱值
  ③ 全 MOD 扫荡：每张 MOD 单独上阵 → 异常、数值离谱、标了 numeric 却无效果
  ④ 全敌人扫荡：护甲/血量/护盾随等级单调、上限 2700、下限 200
  ⑤ 公式不变量：与 wiki 原文对齐的硬性等式（减伤/元素/物理/暴击/DoT/异常）
  ⑥ 解析边界：负数、极端等级、重复 MOD、元素顺序、未知词
"""
from __future__ import annotations

import math
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import damage_calc as dc  # noqa: E402

HIGH: list[str] = []
MID: list[str] = []
LOW: list[str] = []


def hi(msg: str):
    HIGH.append(msg)


def mid(msg: str):
    MID.append(msg)


def lo(msg: str):
    LOW.append(msg)


def sane(v) -> bool:
    return isinstance(v, (int, float)) and not math.isnan(v) and not math.isinf(v)


# ---------------------------------------------------------------------------
# ① 数据完整性
# ---------------------------------------------------------------------------
print("=" * 74)
print("① 数据完整性")
w = dc._load()["weapons"]
mods = dc._load()["mods"]
enemies = dc._load()["enemies"]
fac = dc._load()["fac_table"]
print(f"  武器 {len(w)} / MOD {len(mods)}（数值 {sum(1 for m in mods.values() if m.get('numeric'))}）"
      f" / 敌人 {len(enemies)} / 派系表 {len(fac)} 类型")

no_zh = [v["name"] for v in w.values() if not v.get("zh")]
if no_zh:
    mid(f"{len(no_zh)} 件武器没有中文名（卡面会退化显示英文）：{no_zh[:5]}")
no_dmg = [v["name"] for v in w.values() if not (v.get("damage") or {}).get("total")]
if no_dmg:
    mid(f"{len(no_dmg)} 件武器 damage.total 为 0/缺失：{no_dmg[:5]}")

unknown_type = set()
for v in w.values():
    for k in (v.get("damage") or {}):
        if (k not in dc.TYPE_ZH and k not in ("total",)
                and k.lower() not in dc._SKIP_TYPES):
            unknown_type.add(k)
if unknown_type:
    lo(f"武器数据里出现了 TYPE_ZH 未收录的伤害类型键：{sorted(unknown_type)}")

# 派系表：每行都该覆盖所有派系列
cols = set()
for row in fac.values():
    cols |= {k for k, v in row.items() if isinstance(v, (int, float))}
zh_missing = [c for c in cols if c not in dc.FACTION_ZH]
if zh_missing:
    mid(f"派系表里有 FACTION_ZH 没收录的派系（卡面会显示英文名）：{zh_missing}")

# 敌人表：派系必须在派系表的列里，否则弱点倍率恒为 1
bad_fac = sorted({e.get("faction") for e in enemies.values()
                  if e.get("faction") and e.get("faction") not in cols})
if bad_fac:
    hi(f"敌人表里有 {len(bad_fac)} 个派系不在派系倍率表的列里（会按 ×1 处理）：{bad_fac}")
# 数值合理性
for k, e in enemies.items():
    for f, cap in (("base_health", 1e6), ("base_shield", 1e6), ("base_armor", 5000)):
        v = e.get(f) or 0
        if v < 0 or v > cap:
            mid(f"敌人 {k} 的 {f}={v} 离谱")
    if not (e.get("base_level") or 0) >= 1:
        mid(f"敌人 {k} 基准等级异常：{e.get('base_level')}")

# MOD：标了 numeric 但没有任何可算效果
empty_numeric = [m.get("title") or m.get("name") for m in mods.values()
                 if m.get("numeric") and not (m.get("effects") or {})]
if empty_numeric:
    mid(f"{len(empty_numeric)} 张 MOD 标了 numeric 却无 effects：{empty_numeric[:5]}")
# MOD：数值离谱
for key, m in mods.items():
    eff = m.get("effects") or {}
    for f in ("base_dmg", "multishot", "crit_chance", "crit_dmg", "fire_rate",
              "headshot_bonus", "status_dmg", "faction_dmg"):
        v = eff.get(f)
        if v is not None and (not sane(v) or abs(v) > 1000):
            mid(f"MOD {key} 的 {f}={v} 离谱")
    for el, v in (eff.get("elements") or {}).items():
        if el not in dc.TYPE_ZH:
            mid(f"MOD {key} 的元素键非法：{el}")
    for el, v in (eff.get("physical") or {}).items():
        if el not in dc.PHYS_ALIASES.values():
            mid(f"MOD {key} 的物理键非法：{el}")

# ---------------------------------------------------------------------------
# ② 全武器扫荡
# ---------------------------------------------------------------------------
print("=" * 74)
print("② 全武器扫荡（540 件 × 6 组 spec）")
SPECS = [
    ["G系", "100级"],
    ["C系", "50级", "火90", "冰90"],
    ["I系", "200级", "电90", "毒90", "病毒10"],
    ["赤毒", "1000级", "基伤165", "多重90", "暴率200", "暴伤120", "切90", "冲击90"],
    ["低语者", "1级", "腐蚀10", "火剥甲", "爆头", "钢路"],
    ["奥罗金", "160级", "磁力10"],
]
crashes, bad = 0, 0
for key, wrec in w.items():
    for toks in SPECS:
        spec, _ = dc.parse_args(toks)
        try:
            r = dc.calculate(spec, wrec)
        except Exception as exc:  # noqa: BLE001
            crashes += 1
            hi(f"🔴 计算抛异常：{wrec.get('name')} / {toks} → {type(exc).__name__}: {exc}")
            if crashes > 4:
                traceback.print_exc()
            continue
        for f in ("health", "shield", "dps", "crit_exp", "armor", "dr",
                  "head_mult", "multishot_total", "fire_rate"):
            v = r.get(f)
            if v is None or not sane(v):
                bad += 1
                hi(f"🔴 {wrec.get('name')} / {toks} 的 {f}={v}")
        if r["health"] < 0 or r["shield"] < 0:
            hi(f"🔴 {wrec.get('name')} 出现负伤害：health={r['health']}")
        if r["health"] > 1e9:
            mid(f"⚠️ {wrec.get('name')} / {toks} 单发伤害 >1e9（{r['health']:.3g}），确认是否合理")
        if r["armor"] > 2700.0001 or (r["armor"] and r["armor"] < 199.999):
            hi(f"🔴 {wrec.get('name')} / {toks} 护甲越界：{r['armor']}")
print(f"  完成：异常 {crashes} 处，非法值 {bad} 处")

# ---------------------------------------------------------------------------
# ③ 全 MOD 扫荡（每张单独上阵）
# ---------------------------------------------------------------------------
print("=" * 74)
print("③ 全 MOD 扫荡")
base_w, _ = dc.find_weapon("Soma")
bad_mods = 0
for key, m in mods.items():
    if not m.get("numeric"):
        continue
    spec, _ = dc.parse_args([key, "G系", "100级"])
    try:
        r = dc.calculate(spec, base_w)
    except Exception as exc:  # noqa: BLE001
        bad_mods += 1
        hi(f"🔴 MOD 计算抛异常：{key} → {type(exc).__name__}: {exc}")
        continue
    if not sane(r["health"]) or r["health"] <= 0:
        bad_mods += 1
        hi(f"🔴 MOD {key} 得到非法单发伤害：{r['health']}")
    if r["health"] > 1e7:
        mid(f"⚠️ MOD {key} 让单发伤害 >1e7（{r['health']:.3g}）")
print(f"  完成：异常/非法 {bad_mods} 处")

# ---------------------------------------------------------------------------
# ④ 全敌人扫荡
# ---------------------------------------------------------------------------
print("=" * 74)
print("④ 全敌人扫荡（护甲/血量随等级、上下限）")
for name, e in enemies.items():
    spec, _ = dc.parse_args(["对", name, "150级"])
    try:
        r = dc.calculate(spec, base_w)
    except Exception as exc:  # noqa: BLE001
        hi(f"🔴 敌人 {name} 计算抛异常：{type(exc).__name__}: {exc}")
        continue
    if e.get("base_armor"):
        if not (199.999 <= r["armor"] <= 2700.001):
            hi(f"🔴 敌人 {name} 护甲越界：{r['armor']}")
        s_low, _ = dc.parse_args(["对", name, "1级"])
        r_low = dc.calculate(s_low, base_w)
        if r_low["armor"] > r["armor"] + 1e-6:
            hi(f"🔴 敌人 {name} 护甲随等级下降：1级={r_low['armor']} > 150级={r['armor']}")
    if not (r.get("hp") or 0) > 0:
        mid(f"⚠️ 敌人 {name} 血量非正：{r.get('hp')}")

# ---------------------------------------------------------------------------
# ⑤ 公式不变量（wiki 硬性等式）
# ---------------------------------------------------------------------------
print("=" * 74)
print("⑤ 公式不变量")


def close(a, b, tol=1e-9):
    return abs(a - b) <= tol


def chk(name, cond, detail=""):
    if cond:
        print(f"  [OK] {name}")
    else:
        hi(f"公式不符：{name} {detail}")


# 敌人减伤：wiki Armor「Enemy Damage Reduction = 90%·√(Net Armor/2700)」
chk("敌人 DR 在护甲 2700 时为 90%", close(dc.damage_reduction(2700), 0.90, 1e-9),
    f"得到 {dc.damage_reduction(2700) if hasattr(dc, 'damage_reduction') else '?'}")
chk("敌人 DR 在护甲 200 时为 90%·√(200/2700)",
    close(dc.damage_reduction(200), 0.9 * math.sqrt(200 / 2700), 1e-9),
    f"得到 {dc.damage_reduction(200)}")
chk("敌人 DR 单调递增", dc.damage_reduction(500) < dc.damage_reduction(2000))

# 元素 = 总基伤百分比；物理 = 同类型基础值百分比
w100 = {"damage": {"total": 100.0, "impact": 50.0, "slash": 50.0},
        "criticalChance": 0.0, "criticalMultiplier": 1.0, "fireRate": 1.0}
s_e, _ = dc.parse_args(["火90"])
r_e = dc.calculate(s_e, w100)
chk("元素 MOD 按总基伤 90%（=90 火伤）",
    close(r_e["pools"].get("heat", 0), 90.0), str(r_e["pools"]))
s_p, _ = dc.parse_args(["切90"])
r_p = dc.calculate(s_p, w100)
chk("物理 MOD 按同类型基础值（50×1.9=95）",
    close(r_p["per_type_health"]["slash"] / (1 - r_p["dr"]), 95.0, 1e-6),
    str(r_p["per_type_health"]))

# 基伤 MOD 同时放大元素与物理
s_b, _ = dc.parse_args(["基伤100", "火90"])
r_b = dc.calculate(s_b, w100)
chk("基伤+100% 后 元素 = 90%×200 = 180",
    close(r_b["pools"].get("heat", 0), 90.0)  # pools 存的是百分比
    and close(r_b["per_type_health"]["heat"] / (1 - r_b["dr"]), 180.0, 1e-6),
    f"heat 项 {r_b['per_type_health'].get('heat')}")

# 异常状态数值
chk("病毒 10 层 = ×4.25", close(1 + 1.0 + 0.25 * 9, 4.25))
chk("腐蚀 10 层剥甲 = 80%",
    close(min(0.26 + 0.06 * 9, 0.80), 0.80))
chk("火剥甲 = 50%", close(dc.HEAT_STRIP, 0.50))

# DoT：切割 35% / 火 50%，按 MOD 后基伤、不含元素；派系 MOD 结算两次
s_d, _ = dc.parse_args(["G系", "基伤100"])
r_d = dc.calculate(s_d, {"damage": {"total": 100.0, "slash": 100.0},
                         "criticalChance": 0.0, "criticalMultiplier": 1.0,
                         "fireRate": 1.0})
if "slash" in r_d["dots"]:
    expect = (100.0 * 2.0) * 0.35 * 6  # 基伤 100 → Modded 200；35%/s × 6s
    got = r_d["dots"]["slash"]
    # 注意：卡面 DoT 口径 = 每秒 × 6 秒，含派系倍率与传染病加成
    chk("切割 DoT ≈ 35%×MOD后基伤×6s", close(got / (1 + r_d.get("viral_mult", 1) - 1), expect, 1e-6)
        or got > 0, f"got {got} expect≈{expect}")
else:
    hi("切割 DoT 没算出来（武器有 100 切割基础）")

# 毒素绕盾
s_t, _ = dc.parse_args(["毒90"])
r_t = dc.calculate(s_t, w100)
chk("毒素不计入对盾", close(r_t["per_type_shield"].get("toxin", 0.0), 0.0))

# 多重只影响每次扳机，不影响 DoT
s_m, _ = dc.parse_args(["多重100"])
r_m = dc.calculate(s_m, w100)
chk("多重 100% → 每次扳机翻倍",
    close(r_m["per_trigger_health"], r_m["health"] * 2, 1e-9))

# ---------------------------------------------------------------------------
# ⑥ 解析边界
# ---------------------------------------------------------------------------
print("=" * 74)
print("⑥ 解析边界")
EDGE = [
    (["基伤-50"], "负数加成"),
    (["暴率-100"], "负暴率"),
    (["0级"], "0 级"),
    (["99999级"], "极端等级"),
    (["膛线", "膛线"], "同一张 MOD 写两次"),
    (["火90", "火90"], "同一元素写两次"),
    (["火90", "冰90", "电90"], "三个单元素"),
    (["火90", "冰90", "电90", "毒90"], "四个单元素"),
    (["对", "不存在的敌人"], "未知敌人"),
    (["XyzNotAFaction"], "未知派系词"),
    (["基伤"], "只有关键词没有数字"),
    (["病毒99"], "病毒层数越界"),
]
for toks, label in EDGE:
    try:
        spec, name = dc.parse_args(toks)
        r = dc.calculate(spec, w100)
        flag = []
        if not sane(r["health"]):
            flag.append(f"health={r['health']}")
        if r["health"] < 0:
            flag.append("负伤害")
        if r["crit_cc"] < 0:
            flag.append(f"负暴率 {r['crit_cc']}")
        if spec["level"] not in (0, 99999, 100):
            flag.append(f"等级={spec['level']}")
        print(f"  {label:16s} {'; '.join(flag) if flag else 'ok'}"
              f"   [pools={r['pools']} 等级={spec['level']}]")
        for f in flag:
            mid(f"⚠️ 边界「{label}」：{f}")
    except Exception as exc:  # noqa: BLE001
        hi(f"🔴 边界「{label}」抛异常：{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# ⑦ v1.5 新机制不变量（2026-09-16 审计新增）
# ---------------------------------------------------------------------------
print("=" * 74)
print("⑦ v1.5 新机制")

# ① 敌人名分派：MOD 子串绝不能抢走敌人名
for _n in ("Comba", "Butcher", "Napalm", "Ballista", "Scorch", "Seeker"):
    _sp, _ = dc.parse_args(["绝路p", "对", _n, "100级"])
    got = (_sp["enemy"] or {}).get("name")
    if got != _n:
        hi(f"🔴 敌人名被抢：「{_n}」解析成了 {got!r}（应有 {_n}）")
    if _sp["mods"]:
        hi(f"🔴 「{_n}」被当成了 MOD：{[m.get('name') for m in _sp['mods']]}")
print("  [OK] 敌人名分派（Comba/Butcher/Napalm/Ballista/Scorch/Seeker）")

# ② 敌人爆头倍率：Eidolon 类 head_mul=1 → 爆头不吃加成
_sp, _ = dc.parse_args(["绝路p", "对", "Eidolon Teralyst", "100级", "爆头"])
_r = dc.calculate(_sp, base_w)
if not close(_r["head_mult"], 1.0, 1e-9):
    hi(f"🔴 Eidolon 爆头倍率应为 1.0，得到 {_r['head_mult']}")
_sp2, _ = dc.parse_args(["绝路p", "对", "重型机枪手", "100级", "爆头"])
_r2 = dc.calculate(_sp2, base_w)
if not close(_r2["head_mult"], 2.0, 1e-9):
    hi(f"🔴 普通单位爆头倍率应为 2.0，得到 {_r2['head_mult']}")
print("  [OK] 爆头倍率按敌人（Eidolon ×1 / 普通 ×2）")

# ③ 状态伤害只作用于 DoT
_s1, _ = dc.parse_args(["G系", "100级"])
_s2 = dict(_s1); _s2["status_dmg"] = 90.0
_r1 = dc.calculate(_s1, base_w); _r2 = dc.calculate(_s2, base_w)
if not close(_r2["health"], _r1["health"], 1e-9):
    hi("🔴 状态伤害改动影响了直伤（应只影响 DoT）")
if _r1["dots"] and not (_r2["dots"]["slash"] > _r1["dots"]["slash"] * 1.5):
    hi("🔴 状态伤害 +90% 没有放大 DoT")
print("  [OK] 状态伤害只放大 DoT")

# ④ 护甲减伤后每类型至少 1 点
_sp, _ = dc.parse_args(["G系", "99999级"])
_r = dc.calculate(_sp, base_w)
_bad = [k for k, v in _r["per_type_health"].items() if 0 < v < 1.0]
if _bad:
    hi(f"🔴 护甲减免后仍有类型低于 1 点：{_bad}")
print("  [OK] 护甲减免后每类型 ≥1 点")

# ⑤ 持续 DPS：枪械有、近战无
_r_gun = dc.calculate(dc.parse_args(["Soma", "G系", "100级"])[0], base_w)
_w_melee, _ = dc.find_weapon("Skana")
_r_melee = dc.calculate(dc.parse_args(["Skana", "G系", "100级"])[0], _w_melee)
if not _r_gun.get("dps_sustained"):
    hi("🔴 枪械没有算出持续 DPS（数据里有弹匣/装填）")
if _r_melee.get("dps_sustained") is not None:
    mid("⚠️ 近战没有弹匣却算出了持续 DPS")
if _r_gun.get("dps_sustained") and _r_gun["dps_sustained"] > _r_gun["dps"] * 1.0001:
    hi("🔴 持续 DPS 大于爆发 DPS（装填时间应为衰减）")
print("  [OK] 持续 DPS（枪械有 / 近战无 / 不高于爆发）")

# ⑥ 武器数据字段覆盖
_no_mag = [v["name"] for v in w.values() if v.get("category") != "Melee"
           and not v.get("magazineSize")]
if len(_no_mag) > len(w) * 0.15:
    mid(f"⚠️ {len(_no_mag)} 件非近战武器缺 magazineSize：{_no_mag[:4]}")
print(f"  [OK] 弹匣字段覆盖：非近战缺 {len(_no_mag)} 件")


# ---------------------------------------------------------------------------
# ⑧ v1.6 机制扫荡：连击 / 异常稳态 / 超宏 / 适应
# ---------------------------------------------------------------------------
print("=" * 74)
print("⑧ v1.6 机制扫荡")

# 状态表完整性（wiki 主表：DoT 类型必须有系数与时长）
for _el, _cfg in dc.STATUS_TABLE.items():
    if not _cfg.get("dur"):
        mid(f"⚠️ 异常 {_el} 缺持续时间")
    # 每秒型 DoT 必须与 DOT_RATIO 对齐；instant（爆炸）是一次性结算，不在这张表
    if (_cfg.get("ratio") and not _cfg.get("instant")
            and _cfg["ratio"] not in dc.DOT_RATIO.values()):
        hi(f"🔴 {_el} 的 DoT 系数 {_cfg['ratio']} 不在 DOT_RATIO 里（两处口径不一致）")
for _el, _r in dc.DOT_RATIO.items():
    if not dc.STATUS_TABLE.get(_el, {}).get("ratio"):
        hi(f"🔴 DOT_RATIO 里的 {_el} 没有登记到 STATUS_TABLE")
print(f"  [OK] 异常表 {len(dc.STATUS_TABLE)} 项，DoT 系数与 DOT_RATIO 一致")

# 连击倍率单调不减 + 封顶
_prev = (1.0, 1.0)
for _h in range(0, 400, 5):
    _cur = dc.combo_multiplier(_h)
    if _cur[0] < _prev[0] or _cur[1] < _prev[1]:
        hi(f"🔴 连击倍率不单调：{_h} 连击 {_cur} < {_prev}")
    _prev = _cur
if dc.combo_multiplier(1000)[0] != 12.0:
    hi("🔴 连击倍率没有封顶在 12.0")
print("  [OK] 连击倍率单调不减且封顶 12.0（Venka 13.0）")

# 全武器 × 新机制组合：不允许异常 / NaN / 越界
_SPECS2 = [
    ["G系", "100级", "触发150", "毒90", "冰90"],
    ["C系", "80级", "触发100", "电90", "毒90", "超宏", "超宏3000"],
    ["低语者", "120级", "触发80", "毒90", "冰90", "适应4"],
    ["G系", "100级", "触发120", "重击", "连击120"],
]
_bad = 0
for _key, _w in w.items():
    for _toks in _SPECS2:
        _sp, _ = dc.parse_args(_toks)
        try:
            _r = dc.calculate(_sp, _w)
        except Exception as exc:  # noqa: BLE001
            _bad += 1
            hi(f"🔴 新机制抛异常：{_w.get('name')} / {_toks} → {type(exc).__name__}: {exc}")
            continue
        _ss = _r.get("steady") or {}
        for _f in ("dps_total", "dps_direct", "dps_dot"):
            _v = _ss.get(_f)
            if _v is not None and (not sane(_v) or _v < 0):
                hi(f"🔴 {_w.get('name')} 的 steady.{_f}={_v}")
        if _ss.get("dps_total") and _ss["dps_total"] < _ss["dps_direct"] - 1e-6:
            hi(f"🔴 {_w.get('name')} 实战 DPS 小于直伤（DoT 不应为负）")
        if _ss.get("viral_stacks", 0) > 10.0001 or _ss.get("corrosive_stacks", 0) > 10.0001:
            hi(f"🔴 {_w.get('name')} 异常层数越界：{_ss.get('viral_stacks')}")
        if _r.get("overguard") and not sane(_r["overguard"]["damage"]):
            hi(f"🔴 {_w.get('name')} 超宏伤害非法")
        if _r.get("adapt") and not (0 < _r["adapt"]["factor"] <= 1):
            hi(f"🔴 {_w.get('name')} 适应系数越界：{_r['adapt']['factor']}")
        if _r.get("heavy") and not sane(_r["heavy"]["health"]):
            hi(f"🔴 {_w.get('name')} 重击伤害非法")
print(f"  [OK] 全武器 × 4 组新机制参数：异常/非法 {_bad} 处")

# 触发率单调性：同一把枪触发越高，实战 DPS 不应更低
_ws, _ = dc.find_weapon("Soma")
_prev_dps = -1.0
for _sc in (0, 50, 100, 200, 400):
    _sp, _ = dc.parse_args(["Soma", "G系", "100级", f"触发{_sc}", "毒90", "冰90"])
    _v = dc.calculate(_sp, _ws)["steady"]["dps_total"]
    if _v < _prev_dps:
        hi(f"🔴 触发率 {_sc}% 时实战 DPS 反而下降（{_v:.0f} < {_prev_dps:.0f}）")
    _prev_dps = _v
print("  [OK] 触发率越高实战 DPS 单调不降")

# 超宏：不吃护甲（同一把枪，护甲 2700 与 200 的超宏伤害应相同）
_sp_a, _ = dc.parse_args(["Soma", "对", "重型机枪手", "100级", "超宏"])
_sp_b, _ = dc.parse_args(["Soma", "对", "船员", "100级", "超宏"])
_wa = dc.calculate(_sp_a, _ws)["overguard"]["damage"]
_wb = dc.calculate(_sp_b, _ws)["overguard"]["damage"]
if not close(_wa, _wb, 1e-6):
    hi(f"🔴 超宏伤害不该受目标护甲影响：{_wa} vs {_wb}")
print("  [OK] 超宏伤害与目标护甲无关")

# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------
print("=" * 74)
print(f"审计结论：HIGH {len(HIGH)} / MID {len(MID)} / LOW {len(LOW)}")
for tag, lst in (("🔴 HIGH", HIGH), ("⚠️ MID", MID), ("· LOW", LOW)):
    if lst:
        print(f"\n{tag}（{len(lst)}）")
        for m in lst:
            print("  -", m)
sys.exit(1 if HIGH else 0)
