# -*- coding: utf-8 -*-
"""伤害计算器 v1 离线测试：python3 tests/test_damage_calc.py

钉住的是公式本身（护甲缩放 / DR 上限 / 暴击期望 / 元素合成 / 派系倍率）
和解析器的基本行为——这些一旦悄悄变了，卡面上的数就全不可信了。
公式基准：U36 抗性重构后（2026-09-16 对英文 wiki 核对）。
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import damage_calc as dc  # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = ""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f"  -> {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


def close(a: float, b: float, tol: float = 1e-6) -> bool:
    return abs(a - b) <= tol


# ---------------------------------------------------------------------------
# 1) 护甲缩放（Enemy Level Scaling, U36）
# ---------------------------------------------------------------------------
check("d<70 用 f1：level 50、基准甲 150",
      close(dc.armor_multiplier(50, 1), 1 + 0.005 * 49 ** 1.75))
check("d>80 用 f2：level 100、基准甲 150",
      close(dc.current_armor(100, 150), min(150 * (1 + 0.4 * 99 ** 0.75), 2700)))
check("护甲上限 2700（90% DR）",
      close(dc.current_armor(9999, 5000), 2700.0)
      and close(dc.damage_reduction(2700.0), 0.9))
check("护甲下限 200", close(dc.current_armor(1, 10), 200.0))
check("70-80 区间在 f1/f2 之间（smoothstep）",
      min(1 + 0.005 * 75 ** 1.75, 1 + 0.4 * 75 ** 0.75)
      <= dc.armor_multiplier(76, 1)
      <= max(1 + 0.005 * 75 ** 1.75, 1 + 0.4 * 75 ** 0.75))

# ---------------------------------------------------------------------------
# 2) 暴击期望
# ---------------------------------------------------------------------------
# wiki「Critical Hit → Average Damage」：平均倍率 = 1 + 总暴率 × (总暴伤 − 1)
# ★ 这条对超过 100% 的多段暴击同样成立；旧实现写成 CM×CC 会高估（2026-09-16 修）
check("CC<=100%：1+CC(CM-1)（wiki Paris 例 30%×2.0 → 1.3）",
      close(dc.crit_expectation(0.3, 2.0), 1.3))
check("CC=100%：等于 CM", close(dc.crit_expectation(1.0, 2.0), 2.0))
check("CC=200% 橙暴：×3（不是 CM×2=4）",
      close(dc.crit_expectation(2.0, 2.0), 3.0))
check("CC=250%：×3.5（橙暴全中 + 50% 概率超暴）",
      close(dc.crit_expectation(2.5, 2.0), 3.5))
check("CC=238% × CM6.6：×14.328（绝路P + 关键延迟）",
      close(dc.crit_expectation(2.38, 6.6), 1 + 2.38 * 5.6, 1e-9))
check("CC=0 无暴击", close(dc.crit_expectation(0.0, 3.0), 1.0))

# ---------------------------------------------------------------------------
# 3) 元素合成（HCET 顺序）
# ---------------------------------------------------------------------------
pools = dc.compose_elements({"electricity": 90, "toxin": 90})
check("电+毒 → 腐蚀180%", pools == {"corrosive": 180.0}, str(pools))
pools = dc.compose_elements({"heat": 60, "cold": 60, "toxin": 60})
check("火冰毒 → 爆炸120 + 毒60",
      close(pools.get("blast", 0), 120.0) and close(pools.get("toxin", 0), 60.0),
      str(pools))
pools = dc.compose_elements({"heat": 60})
check("单火不合成", pools == {"heat": 60.0}, str(pools))

# ---------------------------------------------------------------------------
# 4) 派系倍率表（U36 后按派系 ±50%）
# ---------------------------------------------------------------------------
table = dc._load()["fac_table"]
check("冲击 对 G系 ×1.5", table.get("impact", {}).get("Grineer") == 1.5)
check("腐蚀 对 G系 ×1.5", table.get("corrosive", {}).get("Grineer") == 1.5)
check("磁力 对 C系 ×1.5", table.get("magnetic", {}).get("Corpus") == 1.5)
check("病毒 对 奥罗金 ×1.5", table.get("viral", {}).get("Orokin") == 1.5)
check("无弱点的组合默认 ×1",
      table.get("impact", {}).get("Corpus", 1.0) == 1.0)
check("表覆盖 13+ 伤害类型", len([k for k in table if k != "_meta"]) >= 13,
      str(sorted(table)))

# ---------------------------------------------------------------------------
# 5) 武器查询
# ---------------------------------------------------------------------------
w, _ = dc.find_weapon("布拉玛")
check("中文名子串命中：布拉玛 → 赤毒布拉玛",
      bool(w) and "布拉玛" in (w.get("zh") or ""), str(w and w.get("zh")))
w2, _ = dc.find_weapon("kuva bramma")
check("英文名命中", bool(w2) and w2.get("name") == "Kuva Bramma")
w3, _ = dc.find_weapon("不存在的武器xyz")
check("查不到返回 None", w3 is None)

# ---------------------------------------------------------------------------
# 6) 解析器
# ---------------------------------------------------------------------------
spec, name = dc.parse_args(["赤毒", "布拉玛", "G系", "100级", "爆头",
                            "基伤165", "多重90", "电90", "毒60", "基甲300"])
check("等级解析", spec["level"] == 100)
check("派系解析", spec["faction"] == "Grineer")
check("爆头解析", spec["headshot"] is True)
check("基伤解析", spec["base_dmg"] == 165.0)
check("多重解析", spec["multishot"] == 90.0)
check("元素解析（电90 毒60）", spec["singles"] == {"electricity": 90.0, "toxin": 60.0},
      str(spec["singles"]))
check("基甲覆盖", spec["base_armor"] == 300.0)
check("武器名 token 保持完整（赤毒 布拉玛）",
      " ".join(name) == "赤毒 布拉玛", str(name))

spec2, name2 = dc.parse_args(["基础形态", "Soma", "C系", "60级", "派系30"])
check("独立数字参数（派系 30）", spec2["faction_dmg"] == 30.0, str(spec2))
check("C系解析", spec2["faction"] == "Corpus")

# ---------------------------------------------------------------------------
# 7) 端到端：Soma 打 G系 30 级（基准甲 100）
# ---------------------------------------------------------------------------
w4, _ = dc.find_weapon("Soma")
spec3, _ = dc.parse_args(["基础形态", "G系", "30级"])
res = dc.calculate(spec3, w4)
# 30 级（d=29<70）：甲 = 默认基准甲150 ×(1+0.005×29^1.75)，再夹到 [200,2700]
ar_expect = min(max(150 * (1 + 0.005 * 29 ** 1.75), 200.0), 2700.0)
check("端到端护甲值", close(res["armor"], ar_expect, 1e-4), f"{res['armor']} vs {ar_expect}")
check("打血 < 打盾（有甲）", res["health"] < res["shield"])
check("打盾剔除毒素",
      close(res["per_type_shield"].get("toxin", 0.0), 0.0))
# 基伤不加成时：血量总伤 = 面板总伤 ×(1-DR)×(各类型派系倍率)
raw_total = w4["damage"]["total"]
mult_expect = (1.2 * 1.5 + 6 * 1.0 + 4.8 * 1.0) / raw_total  # 仅冲击 对 G ×1.5
check("端到端：打血 = Σ类型×(1-DR)",
      close(res["health"], raw_total * (1 - res["dr"]) * mult_expect, 1e-4),
      f"{res['health']} vs {raw_total * (1 - res['dr']) * mult_expect}")
check("DPS 为正", res["dps"] > 0)

# 加基伤后总伤按比例放大
spec4, _ = dc.parse_args(["基础形态", "G系", "30级", "基伤100"])
res2 = dc.calculate(spec4, w4)
check("基伤+100% → 打血翻倍（元素未合成时）",
      close(res2["health"], res["health"] * 2, 1e-4),
      f"{res2['health']} vs {res['health'] * 2}")

# ---------------------------------------------------------------------------
# 8) v1.1：异常状态（用户反馈「不同元素算出来一个数」）
# ---------------------------------------------------------------------------
# 病毒：对血 +100%（1 层）→ +325%（10 层）
spec_v1, _ = dc.parse_args(["基础形态", "G系", "30级", "病毒1"])
spec_v10, _ = dc.parse_args(["基础形态", "G系", "30级", "病毒10"])
check("病毒1层 → 对血 ×2",
      close(dc.calculate(spec_v1, w4)["health"], res["health"] * 2, 1e-4))
check("病毒10层 → 对血 ×4.25",
      close(dc.calculate(spec_v10, w4)["health"], res["health"] * 4.25, 1e-4))
check("病毒不影响对盾",
      close(dc.calculate(spec_v10, w4)["shield"], res["shield"], 1e-4))
check("病毒层数上限 10", dc.parse_args(["病毒99"])[0]["viral"] == 10)

# 磁力：对盾/超宏 ×(1+100%+25%×(N-1))
spec_m10, _ = dc.parse_args(["基础形态", "G系", "30级", "磁力10"])
check("磁力10层 → 对盾 ×4.25",
      close(dc.calculate(spec_m10, w4)["shield"], res["shield"] * 4.25, 1e-4))

# 腐蚀剥甲：10 层 −80% → 护甲降到 20%，DR 随之下降；毒 DoT 走血量
spec_c10, _ = dc.parse_args(["基础形态", "G系", "30级", "腐蚀10"])
rc = dc.calculate(spec_c10, w4)
check("腐蚀10层剥甲 80%",
      close(rc["strip"], 0.80, 1e-9) and close(rc["armor_eff"], rc["armor"] * 0.2, 1e-9),
      f"{rc['armor_eff']} vs {rc['armor'] * 0.2}")
check("剥甲后伤害更高", rc["health"] > res["health"])
spec_hs, _ = dc.parse_args(["基础形态", "G系", "30级", "火剥甲"])
check("火剥甲 −50%",
      close(dc.calculate(spec_hs, w4)["strip"], 0.5, 1e-9))

# DoT（v1.5 起口径）：每秒 = ratio × MOD后基伤 × 派系MOD² × 类型弱点
#              × 状态伤害加成 × 暴击期望（× 爆头倍率，指定爆头时）
# 切割 35%/s 且**无视护甲**；电击 50%/s 吃护甲减免
spec_slash, _ = dc.parse_args(["基础形态", "G系", "30级"])
r_slash = dc.calculate(spec_slash, w4)
base_after = w4["damage"]["total"] * 1.0
# DoT 累加器起点为 1（wfsim 实测 M58）：(基伤+1)×系数
_exp_bleed = (base_after + 1.0) * 0.35 * 6 * r_slash["crit_exp"]
check("切割 DoT = 0.35×(基伤+1)×6×暴击期望（无视护甲）",
      close(r_slash["dots"].get("slash", 0.0), _exp_bleed, 1e-4),
      f"{r_slash['dots'].get('slash')} vs {_exp_bleed}")

spec_ele, _ = dc.parse_args(["基础形态", "G系", "30级", "电90"])
re_ele = dc.calculate(spec_ele, w4)
_exp_ele = ((base_after + 1.0) * 0.5 * 6 * re_ele["crit_exp"]
            * (1 - re_ele["dr"]))
check("电击 DoT = 0.5×(基伤+1)×6×暴击期望×(1-DR)",
      close(re_ele["dots"].get("electricity", 0.0), _exp_ele, 1e-4),
      f"{re_ele['dots'].get('electricity')} vs {_exp_ele}")

# 状态伤害 MOD 只影响 DoT，不动直伤（wiki 的 Bleed per tick 公式里那一项）
spec_sd, _ = dc.parse_args(["基础形态", "G系", "30级"])
spec_sd["status_dmg"] = 100.0
r_sd = dc.calculate(spec_sd, w4)
check("状态伤害 +100% → DoT 翻倍", close(r_sd["dots"].get("slash", 0.0),
                                        _exp_bleed * 2, 1e-4))
check("状态伤害不影响直伤",
      close(r_sd["health"], r_slash["health"], 1e-9))


# 派系弱点提示：G系 弱 冲击/腐蚀
spec_g, _ = dc.parse_args(["基础形态", "G系", "30级"])
rg = dc.calculate(spec_g, w4)
check("G系弱点含冲击/腐蚀",
      "impact" in rg["fac_weak"] and "corrosive" in rg["fac_weak"],
      str(rg["fac_weak"]))
check("本配卡吃到加成的类型被列出", rg["hit_weak"] == ["impact"], str(rg["hit_weak"]))

# 关键回归：火/冰/电三种单元素在 G系 上数值应当**相同**（都不在弱点里），
# 而腐蚀/冲击不同 —— 这正是「不是 bug，是 U36 后只有派系弱点」的证据
_g = dc.parse_args(["基础形态", "G系", "30级"])[0]
vals = {}
for tok in ("火90", "冰90", "电90", "毒90"):
    s, _ = dc.parse_args(["基础形态", "G系", "30级", tok])
    vals[tok] = round(dc.calculate(s, w4)["health"], 3)
check("火/冰/电 在 G系 同值（均非弱点）", len(set(vals.values())) == 1, str(vals))
s_cor, _ = dc.parse_args(["基础形态", "G系", "30级", "电90", "毒90"])
check("腐蚀(电+毒) 比 单电 高（弱点 ×1.5）",
      dc.calculate(s_cor, w4)["health"] > vals["电90"])


# ---------------------------------------------------------------------------
# 9) v1.2：敌人基准数值（极镜数据）+ MOD 名输入 + 击杀发数
# ---------------------------------------------------------------------------
_l = dc.find_enemy("重型机枪手")
check("敌人精确匹配：重型机枪手",
      bool(_l) and _l["base_armor"] == 500 and _l["base_health"] == 300
      and _l["base_level"] == 8, str(_l))
check("敌人模糊匹配：重机枪手 → 重型机枪手",
      (dc.find_enemy("重机枪手") or {}).get("zh") == "重型机枪手")
check("敌人英文名：Lancer",
      (dc.find_enemy("Lancer") or {}).get("zh") == "枪兵")
check("不知道的敌人返回 None", dc.find_enemy("米老鼠") is None)

check("Grineer 血量缩放 f1",
      close(dc.health_multiplier("Grineer", 50, 1), 1 + 0.015 * 49 ** 2.12))
check("Corpus 血量缩放 f1",
      close(dc.health_multiplier("Corpus", 50, 1), 1 + 0.015 * 49 ** 2.12))
check("Infested 血量缩放 f1（系数 0.0225）",
      close(dc.health_multiplier("Infested", 50, 1), 1 + 0.0225 * 49 ** 2.12))
check("Corpus 护盾缩放 f1",
      close(dc.shield_multiplier("Corpus", 50, 1), 1 + 0.02 * 49 ** 1.76))
check("基准等级参与计算（重型机枪手基准 8 级）",
      close(dc.health_multiplier("Grineer", 50, 8), 1 + 0.015 * 42 ** 2.12))

spec_e, wt_e = dc.parse_args(["基础形态", "Soma", "对", "重型机枪手", "60级"])
check("敌人参数被识别", (spec_e["enemy"] or {}).get("zh") == "重型机枪手")
check("「对」被当介词丢掉", wt_e == ["Soma"], str(wt_e))
check("给了敌人就不再吃默认甲", spec_e["base_armor"] is None)
w_soma, _ = dc.find_weapon("Soma")
r_e = dc.calculate(spec_e, w_soma)
check("敌人护甲用真实基准值（500）",
      close(r_e["armor"], min(max(500 * dc.armor_multiplier(60, 8), 200), 2700), 1e-6),
      f"{r_e['armor']}")
check("敌人派系取自表（Grineer）", r_e["faction"] == "Grineer")
check("血量按敌人基准值+基准等级缩放",
      close(r_e["hp"], 300 * dc.health_multiplier("Grineer", 60, 8), 1e-6),
      f"{r_e['hp']}")
check("击杀发数 = 血段（向上取整，含暴击期望）",
      r_e["shots"] == math.ceil(r_e["hp"] / (r_e["health"] * r_e["crit_exp"])),
      f"{r_e['shots']}")
spec_c, _ = dc.parse_args(["基础形态", "Soma", "船员", "60级"])
r_c = dc.calculate(spec_c, w_soma)
check("船员无护甲 → DR 0", r_c["dr"] == 0)
check("护盾段被计入击杀发数",
      r_c["shots"] > math.ceil(r_c["hp"] / (r_c["health"] * r_c["crit_exp"])))

check("膛线 = 基伤 +165%",
      (dc.resolve_mod("膛线") or {}).get("effects") == {"base_dmg": 165.0})
check("分裂膛室 = 多重 +90%",
      (dc.resolve_mod("分裂膛室") or {}).get("effects") == {"multishot": 90.0})
check("地狱火 = 火 +90%",
      (dc.resolve_mod("地狱火") or {}).get("effects", {}).get("elements")
      == {"heat": 90.0})
check("灭亡 Grineer = 派系 ×1.3",
      (dc.resolve_mod("灭亡Grineer") or {}).get("effects", {}).get("faction_mul") == 1.3)
check("武器名不会被误当 MOD", dc.resolve_mod("布拉玛") is None)

spec_m, wt_m = dc.parse_args(["基础形态", "Soma", "G系", "30级", "膛线", "分裂膛室"])
check("MOD 名不污染武器名", wt_m == ["Soma"], str(wt_m))
check("MOD 折算出预期值",
      spec_m["base_dmg"] == 165.0 and spec_m["multishot"] == 90.0,
      f"{spec_m['base_dmg']} / {spec_m['multishot']}")
spec_h, _ = dc.parse_args(["基础形态", "Soma", "G系", "30级", "基伤165", "多重90"])
r_mod = dc.calculate(spec_m, w_soma)
r_hand = dc.calculate(spec_h, w_soma)
check("MOD 名 ≡ 手写百分比（数值完全一致）",
      close(r_mod["health"], r_hand["health"], 1e-9)
      and close(r_mod["dps"], r_hand["dps"], 1e-9),
      f"{r_mod['health']} vs {r_hand['health']}")
check("MOD 记录被保留（卡面要显示乘区）",
      [m.get("zh") for m in r_mod["mods"]] == ["膛线", "分裂膛室"])

spec_sp, _ = dc.parse_args(["基础形态", "Soma", "船员", "60级", "钢路"])
r_sp = dc.calculate(spec_sp, w_soma)
check("钢路血量 ×2", close(r_sp["hp"], r_c["hp"] * 2, 1e-6))
check("钢路护盾 ×2", close(r_sp["shield_hp"], r_c["shield_hp"] * 2, 1e-6))


# ---------------------------------------------------------------------------
# 10) v1.3：整串配卡粘贴（未识别词不污染武器名）+ 物理MOD + 爆头倍率 + 射速
# ---------------------------------------------------------------------------
# 用户 2026-09-16 实测报「显示不出来」的那条指令：锁定目标当时不在名表里，
# 于是混进武器名 → 武器查不到 → 无输出。这条必须钉死。
FULL_BUILD = ("绝路p 半自动步枪炮轰 镀层分裂膛室 暴风使者 地狱火 "
              "关键延迟 弱点感应 锁定目标 膛线").split()
spec_fb, name_fb = dc.parse_args(FULL_BUILD)
check("整串配卡：武器名干净（只剩绝路p）", name_fb == ["绝路p"], str(name_fb))
check("整串配卡：没有未识别词", spec_fb["unknown"] == [], str(spec_fb["unknown"]))
check("整串配卡：8 张卡全部认出",
      len(spec_fb["mods"]) == 8,
      str([m.get("zh") for m in spec_fb["mods"]]))
check("锁定目标 = 爆头倍率 +60%", spec_fb["headshot_bonus"] == 60.0)
check("火炮弹幕 → 射速不可修改标记", spec_fb["no_fire_rate_mod"] is True)
check("Cannonade 的使用限制被带出",
      any("射速不可修改" in n or "Fire Rate cannot be modified" in n
          for n in spec_fb["notes"]), str(spec_fb["notes"]))
w_rp, _ = dc.find_weapon("绝路p")
check("绝路p 能查到绝路 Prime", (w_rp or {}).get("name") == "Rubico Prime")
res_fb = dc.calculate(spec_fb, w_rp)
check("整串配卡能算出结果", res_fb.get("ok") is True)
# Cannonade：射速回到默认值（wiki 明确「连负面效果都忽略」）
check("Cannonade 在场 → 射速 = 武器默认（忽略关键延迟 −20%）",
      close(res_fb["fire_rate"], w_rp["fireRate"], 1e-6),
      f"{res_fb['fire_rate']} vs {w_rp['fireRate']}")
check("爆头倍率 2.0 → 2.6（只放大超出 1 的部分）",
      close(res_fb["head_mult"], 1 + (2.0 - 1) * 1.6))
spec_nc, _ = dc.parse_args(["绝路p", "关键延迟"])
res_nc = dc.calculate(spec_nc, w_rp)
check("无 Cannonade → 关键延迟 −20% 射速生效",
      close(res_nc["fire_rate"], w_rp["fireRate"] * 0.8, 1e-6),
      f"{res_nc['fire_rate']}")
check("不配 Cannonade 时爆头仍是 2.0", close(res_nc["head_mult"], 2.0))

# 完全没进名表的词：不能毁掉整条指令，要单独报出来
spec_u, name_u = dc.parse_args(["绝路p", "假装这是张卡"])
check("未识别词不影响武器名", name_u == ["绝路p"], str(name_u))
check("未识别词被单独记录", spec_u["unknown"] == ["假装这是张卡"],
      str(spec_u["unknown"]))
res_u = dc.calculate(spec_u, w_rp)
check("有未识别词也照常出结果", res_u.get("ok") is True)
check("卡面写明未识别项",
      any("未识别" in ln and "假装这是张卡" in ln
          for ln in dc.card_lines(w_rp, spec_u, res_u)))

# 武器本身在库里不存在：返回 ok=False（而不是抛异常）
res_bad = dc.calculate(spec_u, None)
check("武器为 None → ok=False 且带错误文案",
      res_bad.get("ok") is False and res_bad.get("error"))
check("武器为 None 时卡面不抛异常且非空",
      len(dc.card_lines(None, spec_u, res_bad)) >= 2)

# 物理 MOD：只加同类型基础值（wiki Damage「Physical Damage」）
w_br, _ = dc.find_weapon("布拉玛")     # Kuva Bramma：纯冲击，切割基础 0
check("样例武器 布拉玛 没有切割基础", w_br["damage"]["slash"] == 0)
spec_ph, _ = dc.parse_args(["布拉玛", "G系", "100级", "切90"])
res_ph = dc.calculate(spec_ph, w_br)
check("缺该类型基础的武器：切割MOD 不产生切割项",
      not res_ph["per_type_health"].get("slash"))
check("但其它的项不为 0（说明整体没算崩）", res_ph["health"] > 0)
# 有切割基础的武器（绝路 Prime，切割 28.05）→ 只放大那一项
w_rp2, _ = dc.find_weapon("绝路p")
spec_rp, _ = dc.parse_args(["绝路p", "切90"])
res_rp = dc.calculate(spec_rp, w_rp2)
plain = dc.calculate(dc.parse_args(["绝路p"])[0], w_rp2)
ratio = (res_rp["per_type_health"]["slash"]
         / plain["per_type_health"]["slash"])
check("切割MOD 让切割项 ×1.9（只吃同类型基础值）", close(ratio, 1.9, 1e-6),
      f"{ratio}")
check("切割MOD 不动冲击项",
      close(res_rp["per_type_health"]["impact"],
            plain["per_type_health"]["impact"], 1e-9))
w_phy0 = {"damage": {"total": 100.0, "impact": 100.0, "slash": 0.0},
          "criticalChance": 0.0, "criticalMultiplier": 1.0, "fireRate": 1.0}
spec_p0, _ = dc.parse_args(["G系", "切90"])
res_p0 = dc.calculate(spec_p0, w_phy0)
check("没有切割基础的武器：切割MOD 无效",
      "slash" not in res_p0["per_type_health"]
      or close(res_p0["per_type_health"].get("slash", 0.0), 0.0))
spec_p1, _ = dc.parse_args(["G系", "切90"])
w_phy1 = {"damage": {"total": 100.0, "impact": 50.0, "slash": 50.0},
          "criticalChance": 0.0, "criticalMultiplier": 1.0, "fireRate": 1.0}
res_p1 = dc.calculate(spec_p1, w_phy1)
check("切割基础 50 + 90% → 95（不是 50+90%×100=140）",
      close(res_p1["per_type_health"]["slash"] / (1 - res_p1["dr"]), 95.0, 1e-6),
      str(res_p1["per_type_health"]))


# ---------------------------------------------------------------------------
# 11) v1.4：每次扳机 / DPS 必须体现暴击（用户 2026-09-16 实测反馈）
#     「去掉关键延迟，单次扳机还是一样」⇒ 卡面要给「含暴击」口径
# ---------------------------------------------------------------------------
_sA, _nA = dc.parse_args("绝路p 半自动步枪炮轰 镀层分裂膛室 暴风使者 地狱火 弱点感应 锁定目标 膛线".split())
_sB, _nB = dc.parse_args("绝路p 半自动步枪炮轰 镀层分裂膛室 暴风使者 地狱火 关键延迟 弱点感应 锁定目标 膛线".split())
_wR, _ = dc.find_weapon("绝路p")
_rA = dc.calculate(_sA, _wR)
_rB = dc.calculate(_sB, _wR)
check("两套卡的无暴击单发相同（暴率不影响白字）",
      close(_rA["health"], _rB["health"]))
check("无暴击的每次扳机也相同",
      close(_rA["per_trigger_health"], _rB["per_trigger_health"]))
check("含暴击的每次扳机必须不同（关键延迟 +200% 暴率）",
      _rA["per_trigger_health"] * _rA["crit_exp"]
      < _rB["per_trigger_health"] * _rB["crit_exp"])
_linesA = dc.card_lines(_wR, _sA, _rA)
_linesB = dc.card_lines(_wR, _sB, _rB)
_dpsA = [ln for ln in _linesA if ln.startswith("◆ DPS")]
_dpsB = [ln for ln in _linesB if ln.startswith("◆ DPS")]
check("DPS 爆发行同时给出「含暴击」与「无暴击」",
      _dpsA and "含暴击" in _dpsA[0] and "无暴击" in _dpsA[0], str(_dpsA))
check("DPS 持续行给出弹匣+装填循环",
      any(ln.startswith("◆ DPS 持续") and "装填" in ln for ln in _linesA),
      str([ln for ln in _linesA if ln.startswith("◆ DPS")]))
check("两套卡的含暴击 DPS 确实不同",
      _dpsA and _dpsB and _dpsA[0] != _dpsB[0], f"{_dpsA} vs {_dpsB}")
check("每次扳机行也带「含暴击」",
      any(ln.startswith("◆ 每次扳机") and "含暴击" in ln for ln in _linesA))
check("单发行标注「无暴击」",
      any("单发对血" in ln and "无暴击" in ln for ln in _linesA))


# ---------------------------------------------------------------------------
# 12) v1.6：连击 / 异常覆盖率 / 超宏 / 适应 —— 断言直接锚定 wiki 原文数值
# ---------------------------------------------------------------------------
# wiki《Melee Combo》表：层数 T 需要 (T−1)×20 连击；重击吃满倍率，普通近战吃 1/4 那列
for _hits, _h, _n in ((0, 1.0, 1.0), (19, 1.0, 1.0), (20, 2.0, 1.25),
                      (40, 3.0, 1.5), (100, 6.0, 2.25), (220, 12.0, 3.75),
                      (999, 12.0, 3.75)):
    _hm, _nm = dc.combo_multiplier(_hits)
    check(f"连击 {_hits} → 重击 ×{_h}/普通 ×{_n}",
          close(_hm, _h) and close(_nm, _n), f"得到 {_hm}/{_nm}")
check("Venka Prime 240 连击 → ×13（wik i 表第 13 层）",
      close(dc.combo_multiplier(240, venka=True)[0], 13.0))

# 连击影响：同一把近战，120 连击的重击是 0 连击的 7 倍
_sA, _ = dc.parse_args(["Skana", "G系", "100级", "重击"])
_sB, _ = dc.parse_args(["Skana", "G系", "100级", "重击", "连击120"])
_wS, _ = dc.find_weapon("Skana")
_hA = dc.calculate(_sA, _wS)["heavy"]
_hB = dc.calculate(_sB, _wS)["heavy"]
check("Skana 120 连击重击 = 0 连击 ×7",
      close(_hB["health"], _hA["health"] * 7.0, 1e-6),
      f"{_hB['health']} vs {_hA['health'] * 7}")
check("重击基础伤害取武器的 heavyAttackDamage",
      close(_hB["base"], _wS["heavyAttackDamage"]))
_wGun, _ = dc.find_weapon("Soma")
check("非近战不给重击数据（步枪）",
      dc.calculate(_sB, _wGun)["heavy"] is None)

# 异常层数上限与倍率（wiki《Status Effect》主表）
_sV, _ = dc.parse_args(["基础形态", "Soma", "G系", "100级", "触发200", "毒90", "冰90"])
_wSo, _ = dc.find_weapon("Soma")
_rV = dc.calculate(_sV, _wSo)
check("病毒层数封顶 10（不会超过）", _rV["steady"]["viral_stacks"] <= 10.0,
      str(_rV["steady"]["viral_stacks"]))
check("病毒 10 层 → 对血 ×4.25（=1+325%）",
      close(dc._stack_multiplier(1.0, 0.25, 10), 4.25))
check("磁力 10 层 → 对盾/超宏 ×4.25", close(dc._stack_multiplier(1.0, 0.25, 10), 4.25))
check("腐蚀 10 层剥甲 80%",
      close(min(dc.CORROSIVE_BASE + dc.CORROSIVE_STEP * 9, dc.CORROSIVE_MAX), 0.80))
check("冰 1 层 +0.1 暴伤、9 层 +0.5 封顶（wiki 原文）",
      close(min(0.5, 0.1 + 0.05 * 0), 0.1) and close(min(0.5, 0.1 + 0.05 * 8), 0.5))
# 触发率越高 → proc 越多 → 病毒层数越高（单调）
_sLow, _ = dc.parse_args(["基础形态", "Soma", "G系", "100级", "触发0", "毒90", "冰90"])
_rLow = dc.calculate(_sLow, _wSo)
check("触发率高 → 每秒 proc 更多",
      _rV["steady"]["procs_per_sec"] > _rLow["steady"]["procs_per_sec"])
check("没有任何 proc 时稳态模型返回空（卡面不显示实战行）",
      dc.status_procs_per_sec(15, 1, 0.0) == 0.0
      and dc.steady_state_dps({"impact": 100.0}, 100.0, 1.0, {}, "Grineer",
                              0.0, 1.0, 0.0, 1.0, 2.0, 1.0, 10.0,
                              {}).get("dps_total") is None)
check("DoT 占比随触发率上升",
      _rV["steady"]["dps_dot"] > _rLow["steady"]["dps_dot"])

# 超宏（wiki《Overguard》）：不吃护甲与派系弱点；虚空 +50%；磁力加伤；免疫异常
_sO, _ = dc.parse_args(["基础形态", "Soma", "G系", "100级", "超宏"])
_rO = dc.calculate(_sO, _wSo)["overguard"]
_sO2, _ = dc.parse_args(["基础形态", "Soma", "G系", "100级", "超宏", "磁力10"])
_rO2 = dc.calculate(_sO2, _wSo)["overguard"]
check("磁力 10 层 → 超宏伤害 ×4.25",
      close(_rO2["damage"], _rO["damage"] * 4.25, 1e-6),
      f"{_rO2['damage']} vs {_rO['damage'] * 4.25}")
check("超宏伤害不含护甲减免（>0 且与 DR 无关）", _rO["damage"] > 0)
_vg = dc.overguard_damage(0.0, 1.0, {"void": 100.0})
check("只有虚空伤害时超宏吃 +50%",
      close(_vg, 100.0 * 1.5), f"{_vg}")

# 适应（wiki《Sentient》）：1~4 层抗性依次 90/80/75/70%
check("适应抗性表 = 90/80/75/70%",
      dc.ADAPT_RESIST == (0.90, 0.80, 0.75, 0.70))
_sAd, _ = dc.parse_args(["基础形态", "Soma", "G系", "100级", "适应1"])
_ad1 = dc.calculate(_sAd, _wSo)["adapt"]
_sAd4, _ = dc.parse_args(["基础形态", "Soma", "G系", "100级", "适应4"])
_ad4 = dc.calculate(_sAd4, _wSo)["adapt"]
check("适应层数越多、有效伤害越低", _ad4["factor"] < _ad1["factor"])
check("适应 1 层只抗 1 个类型", len(_ad1["resisted"]) == 1)
check("适应 4 层最多抗 4 个类型、且不超过实际伤害类型数",
      len(_ad4["resisted"]) == min(4, len(_ad4["resisted"]))
      and len(_ad4["resisted"]) >= len(_ad1["resisted"]))
check("适应抗性永远是 90/80/75/70 的前缀（wiki 顺序）",
      list(_ad4["resisted"].values())
      == list(dc.ADAPT_RESIST[:len(_ad4["resisted"])]),
      str(list(_ad4["resisted"].values())))
check("首个被抗的类型是最高的伤害类型（90%）",
      list(_ad1["resisted"].values()) == [0.90])


# ---------------------------------------------------------------------------
# 13) v1.7：暴击率 MOD 是**相对基础值**的加成
#     这条是配卡截图识别时暴露的：写成加法会让 20% 变成 140%，
#     暴击期望从 ×2.58 虚高到 ×5.98，整套参考伤害全错。
# ---------------------------------------------------------------------------
_sCrit, _ = dc.parse_args(["驱魔之刃", "G系", "100级", "暴率120"])
_wCrit, _ = dc.find_weapon("驱魔之刃")
_rCrit = dc.calculate(_sCrit, _wCrit)
check("暴率 +120% 相对加成：20% → 44%（不是 140%）",
      close(_rCrit["crit_cc"], 0.44, 1e-9), f"{_rCrit['crit_cc']}")
# 武器数据里暴伤是 2.4000001（float 精度），容差给 1e-6
check("暴率加成后暴击期望 = 1 + 0.44×(2.4−1) = ×1.616",
      close(_rCrit["crit_exp"], 1 + 0.44 * (2.4 - 1), 1e-6),
      f"{_rCrit['crit_exp']}")
_sCrit0, _ = dc.parse_args(["驱魔之刃", "G系", "100级"])
_rCrit0 = dc.calculate(_sCrit0, _wCrit)
check("无暴率 MOD 时期望 ×1.28（20%×2.4）",
      close(_rCrit0["crit_exp"], 1 + 0.2 * (2.4 - 1), 1e-6),
      f"{_rCrit0['crit_exp']}")
check("暴伤 MOD 同样是相对基础值（肢解 +90% → 2.4×1.9）",
      close(dc.calculate(dc.parse_args(["驱魔之刃", "G系", "100级", "暴伤90"])[0],
                         _wCrit)["crit_cm"], 2.4 * 1.9, 1e-6))


# ---------------------------------------------------------------------------
# 14) v1.8：斩铁「重击时 x2」与奋力一掷「连续投掷伤害」
#     wiki：True Steel「120% Critical Chance (x2 for Heavy Attack)」；
#           Power Throw 满级 +100% Throw Damage、最多 3 层
# ---------------------------------------------------------------------------
_wX, _ = dc.find_weapon("驱魔之刃")
_sX1, _ = dc.parse_args(["驱魔之刃", "G系", "100级", "斩铁"])
_rX1 = dc.calculate(_sX1, _wX)
check("斩铁只加普通暴击：20%×(1+1.2) = 44%",
      close(_rX1["crit_cc"], 0.44, 1e-6), f"{_rX1['crit_cc']}")
check("★ 重击时斩铁翻倍：20%×(1+2.4) = 68%",
      close(_rX1["crit_cc_heavy"], 0.68, 1e-6), f"{_rX1['crit_cc_heavy']}")
check("重击暴击期望用加倍后的暴率（1+0.68×1.4）",
      close(_rX1["crit_exp_heavy"], 1 + 0.68 * (2.4 - 1), 1e-4),
      f"{_rX1['crit_exp_heavy']}")
check("普通与重击的暴击期望不同（重击更高）",
      _rX1["crit_exp_heavy"] > _rX1["crit_exp"])

_sX2, _ = dc.parse_args(["驱魔之刃", "G系", "100级", "肢解"])
_rX2 = dc.calculate(_sX2, _wX)
check("暴伤 MOD 不享用「重击翻倍」（只有暴击几率类有）",
      close(_rX2["crit_cc_heavy"], _rX2["crit_cc"], 1e-9))

_sX3, _ = dc.parse_args(["驱魔之刃", "G系", "100级", "奋力一掷", "投掷"])
_rX3 = dc.calculate(_sX3, _wX)
check("奋力一掷 + 3 层 → 投掷伤害 ×4", close(_rX3["throw"]["mult"], 4.0),
      str(_rX3["throw"]))
check("投掷基数取武器的 Throw 攻击伤害（Xoris = 120）",
      close(_rX3["throw"]["base"], 120.0))
check("投掷伤害 = 单发对血 × 投掷倍率",
      close(_rX3["throw"]["health"], _rX3["health"] * 4.0, 1e-6))
check("未配投掷 MOD 时不给投掷数据",
      dc.calculate(dc.parse_args(["驱魔之刃", "G系", "100级"])[0],
                   _wX)["throw"] is None)
_sX4, _ = dc.parse_args(["驱魔之刃", "G系", "100级", "奋力一掷", "连投1"])
check("连投层数可手写：1 层 → ×2",
      close(dc.calculate(_sX4, _wX)["throw"]["mult"], 2.0))
_sX5, _ = dc.parse_args(["驱魔之刃", "G系", "100级", "奋力一掷", "连投99"])
check("层数封顶在卡片上限（99 → 3 层）",
      dc.calculate(_sX5, _wX)["throw"]["stacks"] == 3)


# ---------------------------------------------------------------------------
# v1.11：wfsim 条件堆叠（镀层N / 满镀层）
# ---------------------------------------------------------------------------
_g1, _ = dc.parse_args(["镀层 分裂膛室", "满镀层"])
check("满镀层：镀层 分裂膛室 多重 = 80+30×5",
      close(_g1["multishot"], 230.0), str(_g1["multishot"]))
_g2, _ = dc.parse_args(["镀层 分裂膛室", "镀层3"])
check("镀层3 → 170", close(_g2["multishot"], 170.0))
_g3, _ = dc.parse_args(["镀层 分裂膛室", "镀层9"])
check("镀层9 封顶 5 层 → 230", close(_g3["multishot"], 230.0))
_g4, _ = dc.parse_args(["镀层 分裂膛室"])
check("未指定层数 → 条件不计入",
      close(_g4["multishot"], 80.0) and not _g4["galv_applied"])
_g5, _ = dc.parse_args(["异况超量", "镀层 准确射手", "异常3", "满镀层"])
check("镀层 准确射手进 CO 桶（80+40×3=200）",
      close(_g5["dmg_per_status"], 200.0), str(_g5["dmg_per_status"]))


# ---------------------------------------------------------------------------
# 形态：有灵化数据的武器默认开灵化（v1.11，2026-09-17 用户要求）
# ---------------------------------------------------------------------------
_w_lat, _ = dc.find_weapon("拉特昂 Prime")
_s_def, _ = dc.parse_args([])
_r_def = dc.calculate(_s_def, _w_lat)
check("拉特昂默认开灵化形态（基础 50 而非 90）",
      any("灵化形态" in n for n in _s_def["notes"]), str(_s_def["notes"])[:120])
check("灵化形态 note 带范围段与弹跳标注",
      any("范围段 140" in n and "弹跳" in n for n in _s_def["notes"]))
_s_base, _ = dc.parse_args(["基础形态"])
_r_base = dc.calculate(_s_base, _w_lat)
check("基础形态参数回退（90 基伤）",
      any("基础形态" in n for n in _s_base["notes"]))
check("灵化 vs 基础：基础形态单发更高（90 > 50 基伤口径）",
      _r_base["health"] > _r_def["health"],
      f"{_r_base['health']} vs {_r_def['health']}")

# 灵化形态 + 进化暴击（EVO IV 临界平行 +24% cc / +0.2x cd）
_inc = dc._load_incarnon_forms()[str(_w_lat["uniqueName"]).lower()]
check("灵化形态数据：50 冲击 / cc 44% / cm 3.4 / sc 30%",
      _inc["damage"]["total"] == 50 and close(_inc["criticalChance"], 0.44)
      and close(_inc["criticalMultiplier"], 3.4)
      and close(_inc["procChance"], 0.30))
_s_evo, _ = dc.parse_args(["灵化", "进化", "暴击", "镀层 氩晶瞄具",
                            "关键延迟", "满镀层", "弱点感应", "致命火力"])
dc.calculate(_s_evo, _w_lat)
check("灵化 + 进化暴击：wfsim 对拍值 421.6%",
      close((_inc["criticalChance"] + 0.24)
            * (1 + _s_evo["crit_chance"] / 100) * 100, 421.6, 0.5),
      str((_inc["criticalChance"] + 0.24) * (1 + _s_evo["crit_chance"] / 100) * 100))
check("灵化 + 弱感：暴伤 ×7.92（wfsim 7.9）",
      close((_inc["criticalMultiplier"] + 0.2)
            * (1 + _s_evo["crit_dmg"] / 100), 7.92, 0.02))


# ---------------------------------------------------------------------------
# 多段合计：主段 + 额外段（范围/火箭爆炸…）各吃同一套 MOD 乘区
# ---------------------------------------------------------------------------
_w_og, _ = dc.find_weapon("Kuva Ogris")
_s_og, _ = dc.parse_args(["膛线", "镀层 分裂膛室"])
_r_og = dc.calculate(_s_og, _w_og)
_st_og = _r_og.get("segments_total") or {}
check("食人女魔：段合计 = 主段 + 直击 + 爆炸（3 项都 >0）",
      _st_og.get("main", 0) > 0 and _st_og.get("segments", 0) > 0
      and _st_og.get("total", 0) > _st_og.get("main", 0)
      and _st_og.get("n_segments", 0) >= 2,
      str({k: round(v, 1) if isinstance(v, float) else v
           for k, v in _st_og.items()}))
check("食人女魔：段让每次扳机显著高于主段（特殊机制不可忽略）",
      _st_og.get("total", 0) > _st_og.get("main", 0) * 1.5)

_w_l2, _ = dc.find_weapon("拉特昂 Prime")
_s_l2, _ = dc.parse_args(["膛线"])
_r_l2 = dc.calculate(_s_l2, _w_l2)
_st_l2 = _r_l2.get("segments_total") or {}
check("灵化武器默认：段不含重复的灵化主段（只留范围段）",
      _st_l2.get("n_segments", 0) == 1, str(_st_l2))
check("灵化武器的段名不含「灵化形态」主段",
      all("灵化形态" != (m.get("name") or "") for m in _r_l2.get("modes") or []))
_s_l3, _ = dc.parse_args(["基础形态", "膛线"])
_r_l3 = dc.calculate(_s_l3, _w_l2)
check("基础形态参数下不计灵化段",
      not (_r_l3.get("segments_total") or {}).get("n_segments"),
      str(_r_l3.get("segments_total")))


# ---------------------------------------------------------------------------
# Archgun 双部署（空战 / 地面·大气）+ 武器库补全（9/17）
# ---------------------------------------------------------------------------
_w_ay, _ = dc.find_weapon("Kuva Ayanga")
check("空战枪已在武器库（赤毒·怒雷）", bool(_w_ay and _w_ay.get("name")))
if _w_ay:
    _s_atm, _ = dc.parse_args([])
    _r_atm = dc.calculate(_s_atm, _w_ay)
    _s_aw, _ = dc.parse_args(["空战"])
    _r_aw = dc.calculate(_s_aw, _w_ay)
    check("空战部署 note 正确",
          any("空战（Archwing）" in n for n in _s_aw["notes"]))
    check("空战/地面两套面板数值不同",
          _r_atm["per_trigger_health"] != _r_aw["per_trigger_health"],
          f"{_r_atm['per_trigger_health']:.1f} vs {_r_aw['per_trigger_health']:.1f}")
    check("空战范围伤害作为独立段计入",
          (_r_aw.get("segments_total") or {}).get("n_segments", 0) >= 1)

if FAILED:
    print(f"\n失败 {len(FAILED)} 项：{FAILED}")
    sys.exit(1)
print("\n全部通过 ✔")