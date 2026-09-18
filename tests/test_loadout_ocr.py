# -*- coding: utf-8 -*-
"""配卡截图识别离线测试：python3 tests/test_loadout_ocr.py

钉住三件事（这三条一旦悄悄坏了，识别结果就会"看起来对、其实全错"）：

1. **等级反推**：`drain = base + rank`，槽位极性匹配时减半 —— 这是把截图里
   那张卡折算成正确数值的唯一依据（北风 rank1 冰 +30% 与满级 +90% 差 3 倍）；
2. **面板交叉校验**：识别到的面板值必须与本地推算值吻合，否则要报出来，
   不能默默给一组假数字；
3. **端到端**：用用户 2026-09-16 提供的 Xoris（驱魔之刃）截图真实识别结果
   作为 fixture，9 张卡 + 四项面板校验逐条钉死。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import damage_calc as dc      # noqa: E402
from core import loadout_ocr as lo      # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = ""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f"  -> {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


def close(a: float, b: float, tol: float = 1e-6) -> bool:
    return abs(a - b) <= tol


# ---------------------------------------------------------------------------
# 1) 文本清洗与容错（模型输出常带装饰符号）
# ---------------------------------------------------------------------------
check("武器名清洗：'+ 升级 / 驱魔之刃 [30]' → 驱魔之刃",
      lo.clean_weapon_name("+ 升级 / 驱魔之刃 [30]") == "驱魔之刃")
check("武器名清洗：'升级/驱魔之刃[30]'",
      lo.clean_weapon_name("升级/驱魔之刃[30]") == "驱魔之刃")
check("武器名清洗：已是纯名（不带尾部等级）",
      lo.clean_weapon_name("绝路 Prime") == "绝路 Prime")
check("to_int 容忍上下标/箭头：'^10' '7→' '9↘'",
      lo.to_int("^10") == 10 and lo.to_int("7→") == 7 and lo.to_int("9↘") == 9)
check("to_int 直接吃数字", lo.to_int(11) == 11)
check("to_int 无数字返回 None", lo.to_int("—") is None)
check("to_float：'44%' '4.6倍' '40.8'",
      close(lo.to_float("44%"), 44) and close(lo.to_float("4.6倍"), 4.6)
      and close(lo.to_float("40.8"), 40.8))
check("to_pair：'36>144'", lo.to_pair("36>144") == (36.0, 144.0))
check("to_pair：['总计','156>264'] 必须继续拆字符串（曾在这里取错值）",
      lo.to_pair(["总计", "156>264"]) == (156.0, 264.0))
check("to_pair：['36','144']", lo.to_pair(["36", "144"]) == (36.0, 144.0))
check("to_pair：单值", lo.to_pair("24") == (24.0, None))

# ---------------------------------------------------------------------------
# 2) 模型输出的 JSON 提取
# ---------------------------------------------------------------------------
check("剥 markdown 围栏",
      lo.parse_vision_json('```json\n{"a": 1}\n```') == {"a": 1})
check("剥思考段（glm-4.1v-thinking-flash 会先吐 <think>）",
      lo.parse_vision_json('<think>这里可能有 {花括号}</think>\n{"b": 2}') == {"b": 2})
check("剥前后废话", lo.parse_vision_json('好的：\n{"c": 3}\n以上') == {"c": 3})
check("容忍尾随逗号", lo.parse_vision_json('{"d": [1, 2,],}') == {"d": [1, 2]})
check("完全不是 JSON → 空 dict", lo.parse_vision_json("抱歉我看不清") == {})

# ---------------------------------------------------------------------------
# 3) MOD 名称匹配
# ---------------------------------------------------------------------------
_r, _how = lo.match_mod("熔岩冲击")
check("MOD 精确匹配（中文名）", _r is not None and _r.get("name") == "Molten Impact",
      f"{_how}")
check("MOD 英文名直输", (lo.match_mod("Molten Impact")[0] or {}).get("zh") == "熔岩冲击")
check("MOD 带空格差异（'true steel'）",
      (lo.match_mod("true steel")[0] or {}).get("zh") == "斩铁")
check("库里没有的名字 → (None, '')", lo.match_mod("不存在的卡") == (None, ""))
check("空串 → (None, '')", lo.match_mod("") == (None, ""))

# ---------------------------------------------------------------------------
# 4) 容量数字反推等级（识别功能的核心）
# ---------------------------------------------------------------------------
_molten = lo.mod_pool()["molten impact"]
check("熔岩冲击：base=6 max=5",
      _molten["base_drain"] == 6 and _molten["max_rank"] == 5)
check("drain 11 = 6+5 → rank 5（未减半）",
      lo.infer_rank(_molten, 11)[0] == 5)
check("drain 9 = 6+3 → rank 3", lo.infer_rank(_molten, 9)[0] == 3)
check("drain 6 双解（未匹配 r0 / 匹配 r5）→ 取高者 5（满级假设）",
      lo.infer_rank(_molten, 6)[0] == 5)
check("drain 12 超出 max_rank → None（不许瞎猜）",
      lo.infer_rank(_molten, 12)[0] is None)

_vol = lo.mod_pool()["volatile rebound"]
check("易爆反弹：base=10 max=3", _vol["base_drain"] == 10 and _vol["max_rank"] == 3)
_vr, _vw = lo.infer_rank(_vol, 7)
check("drain 7 = ceil((10+3)/2) → rank 3（槽位极性匹配减半）",
      _vr == 3 and "减半" in _vw, f"{_vr} / {_vw}")

_gleam = lo.mod_pool()["gleaming talon"]
check("姿态卡（原始数据 base=-2）不参与折算",
      lo.infer_rank(_gleam, 10)[0] is None
      and "姿态" in lo.infer_rank(_gleam, 10)[1])

check("等级未知时 effect_at 退回满级值",
      close(lo.effect_at(_molten, None).get("elements", {}).get("heat", 0), 90.0))
check("按等级取真实数值：rank1 → 火+30%",
      close(lo.effect_at(_molten, 1).get("elements", {}).get("heat", 0), 30.0))
check("按等级取真实数值：rank5 → 火+90%",
      close(lo.effect_at(_molten, 5).get("elements", {}).get("heat", 0), 90.0))

# ---------------------------------------------------------------------------
# 5) 端到端：用户 2026-09-16 的 Xoris 截图（视觉模型真实输出）
# ---------------------------------------------------------------------------
OCR = {
    "weapon": "+ 升级 / 驱魔之刃 [30]",
    "capacity": "0/70",
    "mods": [
        {"name": "微光利爪", "drain": "^10", "polarity": "?"},
        {"name": "易爆反弹", "drain": "7→", "polarity": "?"},
        {"name": "熔岩冲击", "drain": "11→", "polarity": "?"},
        {"name": "邪恶蓄力", "drain": "7↗", "polarity": "?"},
        {"name": "肢解", "drain": "9↘", "polarity": "?"},
        {"name": "一击必杀", "drain": "11↗", "polarity": "?"},
        {"name": "奋力一掷", "drain": "9↗", "polarity": "?"},
        {"name": "北风", "drain": "7↘", "polarity": "?"},
        {"name": "斩铁", "drain": "9↘", "polarity": "?"},
    ],
    "panel": {
        "attack_speed": "1.17", "crit_chance": "44%",
        "crit_damage": "4.6倍", "status_chance": "18%",
        "damage_rows": [["暴击几率", "44%", None], ["暴击伤害", "4.6倍", None],
                        ["触发几率", "18%", None], ["* 爆炸 (A + *)", "36>144", None],
                        ["冲击", "24", None], ["穿刺", "40.8", None],
                        ["切割", "55.2", None], ["总计", "156>264", None]],
        "total": ["总计", "156>264"], "heavy": ["重击", "343.2>580.8"],
    },
}
_an = lo.analyze(OCR)

check("武器解析为 Xoris（官方简中名「驱魔之刃」）",
      (_an["weapon"] or {}).get("name") == "Xoris")
check("9 张卡全部匹配上库", len(_an["mods"]) == 9
      and all(m["found"] for m in _an["mods"]))
check("没有未收录的名字", _an["unknown"] == [], str(_an["unknown"]))

_by_zh = {m.get("zh"): m for m in _an["mods"]}
_ranks = {zh: m.get("rank") for zh, m in _by_zh.items()}
check("等级反推 8/8 命中（姿态卡除外）",
      _ranks == {"微光利爪": None, "易爆反弹": 3, "熔岩冲击": 5, "邪恶蓄力": 3,
                 "肢解": 5, "一击必杀": 5, "奋力一掷": 5, "北风": 1, "斩铁": 5},
      str(_ranks))
check("★ 北风是 rank 1（非满级）—— 这条就是「MOD 等级差异」的活样本",
      close(lo.effect_at(lo.mod_pool()["north wind"], 1)
            .get("elements", {}).get("cold", 0), 30.0)
      and close(lo.effect_at(lo.mod_pool()["north wind"], 5)
                .get("elements", {}).get("cold", 0), 90.0))

_t = _an["totals"]
check("折算加成：暴击率 +120%（斩铁满级）", close(_t["crit_chance"], 120.0))
check("折算加成：暴伤 +90%（肢解满级）", close(_t["crit_dmg"], 90.0))
check("折算加成：重击伤害 +120%（一击必杀）", close(_t["heavy_dmg"], 120.0))
check("折算加成：初始连击 +30（邪恶蓄力）", close(_t["initial_combo"], 30.0))
check("折算加成：火 90% / 冰 30%",
      close(_t["elements"].get("heat", 0), 90.0)
      and close(_t["elements"].get("cold", 0), 30.0))
check("合成元素：火+冰 → 爆炸 120%",
      list(_t["combined"]) == ["blast"] and close(_t["combined"]["blast"], 120.0))

check("推算面板：暴击率 44%", close(_an["calc_panel"]["crit_chance"], 0.44))
check("推算面板：暴伤 4.56 倍", close(_an["calc_panel"]["crit_damage"], 4.56))
check("推算面板：总伤害 264", close(_an["calc_panel"]["total"], 264.0))
check("★ 面板校验全部通过（识别值与推算值互为独立证据，含行和自检）",
      len(_an["checks"]) >= 4 and all(c["ok"] for c in _an["checks"]),
      str([(c["label"], c["panel"], round(c["calc"], 3), c["ok"]) for c in _an["checks"]]))
check("分析整体 ok", _an["ok"] is True)

_lines = lo.card_lines(_an)
check("卡面非空且含武器行", any("驱魔之刃" in ln for ln in _lines))
check("卡面标注了非满级的卡",
      any("非满级" in ln and "北风" in ln for ln in _lines))
check("卡面写出了「按实际等级折算」", any("按实际等级折算" in ln for ln in _lines))

# 折算结果能直接喂给伤害计算器（不抛异常、不吃 KeyError）
_spec = lo.to_damage_spec(_an)
check("to_damage_spec 产出可用 spec", isinstance(_spec, dict)
      and close(_spec["crit_chance"], 120.0) and _spec["singles"].get("heat") == 90.0)
_res = dc.calculate(_spec, _an["weapon"])
check("识别结果可直接跑伤害计算", _res.get("ok") is True
      and _res["health"] > 0, str(_res.get("health")))
check("重击数据被带出来（配了一击必杀）",
      _res.get("heavy") is not None
      and close(_res["heavy"]["dmg_bonus"], 120.0))

# ---------------------------------------------------------------------------
# 6) 边界：认不出来时要"说出来"，不能静默给假数字
# ---------------------------------------------------------------------------
_an2 = lo.analyze({"weapon": "米老鼠", "mods": [{"name": "不存在卡", "drain": 3}]})
check("武器认不出 → ok=False 且带错误文案",
      _an2["ok"] is False and any("米老鼠" in e for e in _an2["errors"]))
check("认不出的 MOD 记进 unknown",
      _an2["unknown"] == ["不存在卡"], str(_an2["unknown"]))
check("空输入不抛异常", lo.analyze({})["ok"] is False)
check("to_damage_spec 无武器时返回 None", lo.to_damage_spec({}) is None)

_an3 = lo.analyze({"weapon": "驱魔之刃",
                   "mods": [{"name": "熔岩冲击", "drain": "999"}]})
check("等级推不出来时按满级估并标记",
      _an3["totals"]["no_rank"] == ["熔岩冲击"], str(_an3["totals"]["no_rank"]))
check("该情形下卡面给出警告", any("等级未定" in ln or "等级无法确定" in ln
                                 for ln in lo.card_lines(_an3)))


# ---------------------------------------------------------------------------
# 7) v1.8：斩铁「重击时 x2」与奋力一掷「连续投掷伤害」（用户 2026-09-16 报漏算）
#    这两条**一直都在数据里**：前者在效果文本的括号里，后者在 stats 的第二行。
# ---------------------------------------------------------------------------
check("斩铁效果含「重击时暴击几率 ×2」",
      (lo.effect_at(lo.mod_pool()["true steel"], 5)).get("heavy_crit_mult") == 2.0)
_pt5 = lo.effect_at(lo.mod_pool()["power throw"], 5)
check("奋力一掷满级：投掷伤害 +100%/层、最多 3 层",
      close(_pt5.get("throw_dmg", 0), 100.0) and _pt5.get("throw_max_stacks") == 3,
      str(_pt5))
check("奋力一掷 1 级 → +33.3%（与 wiki 表逐级核对）",
      close(lo.effect_at(lo.mod_pool()["power throw"], 1).get("throw_dmg", 0), 33.3))
check("奋力一掷 3 级 → +66.7%",
      close(lo.effect_at(lo.mod_pool()["power throw"], 3).get("throw_dmg", 0), 66.7))
check("折算：重击时暴击率再 +120%（= 斩铁 120% × (2−1)）",
      close(_t["crit_chance_heavy"], 120.0), str(_t["crit_chance_heavy"]))
check("折算：投掷伤害 +100%/层、最多 3 层",
      close(_t["throw_dmg"], 100.0) and _t["throw_max_stacks"] == 3)
check("卡面写出「连续投掷伤害」", any("连续投掷伤害" in ln for ln in _lines))
check("卡面写出「重击时暴击几率 ×2」", any("重击时暴击几率" in ln for ln in _lines))
check("参考区给出重击暴击率 68%（20%×(1+2.4)）",
      any("暴击 68%" in ln for ln in _lines), str(_lines[-6:]))
check("参考区给出投掷行", any("投掷（连续 3/3 层" in ln for ln in _lines))


# ---------------------------------------------------------------------------
# v1.9 补测：非满级的新字段折算 + 灵化（Incarnon）面板反推
# ---------------------------------------------------------------------------
# ① 急进猛突 容量7：双解（未匹配 r3 / 匹配 r10）→ 取高者 10（满级假设）
_ocr_br = {"weapon": "升级 / 空刃 [30]", "capacity": "4/70",
           "mods": [{"name": "急进猛突", "drain": "7", "polarity": "?"}],
           "panel": {}}
_an_br = lo.analyze(_ocr_br)
check("急进猛突 容量7 双解取高 → 满级 +40% 暴击率/连击倍率",
      close(_an_br["totals"]["crit_per_combo"], 40.0, 1e-6),
      str(_an_br["totals"]["crit_per_combo"]))
_sp_br = lo.to_damage_spec(_an_br)
check("crit_per_combo 进 spec", close(_sp_br["crit_per_combo"], 40.0))
check("卡面写出随连击的暴击率",
      any("暴击率/连击倍率" in ln for ln in lo.card_lines(_an_br)))

# ② 灵化执法者：面板是「含 MOD 的最终值」，要反推基础而不是直接当基础
_ocr_inc = {"weapon": "升级 / 执法者 [30]", "capacity": "4/70",
            "mods": [{"name": "压迫点", "drain": "9", "polarity": "?"},
                     {"name": "镀层 斩铁", "drain": "6", "polarity": "?"},
                     {"name": "急进猛突", "drain": "7", "polarity": "?"},
                     {"name": "角斗士 威猛", "drain": "9", "polarity": "?"}],
            "panel": {"attack_speed": "0.833", "crit_chance": "75.6%",
                      "crit_damage": "7倍", "status_chance": "10%",
                      "damage_rows": [["毒素", "1,125.3"], ["冲击", "545.6"],
                                      ["穿刺", "34.1"], ["切割", "102.3"]],
                      "total": ["总计", "1,807.3"]}}
_an_inc = lo.analyze(_ocr_inc)
check("识别出灵化形态", "灵化" in (_an_inc.get("panel_base") or ""),
      str(_an_inc.get("panel_base")))
_wi = _an_inc["weapon"]
check("反推基础暴击 = 75.6% / 2.1 = 36%（斩铁满级 +110%）",
      close(_wi["criticalChance"], 0.36, 1e-6), str(_wi.get("criticalChance")))
check("反推基础暴伤 = 7 / 1.6 = 4.375",
      close(_wi["criticalMultiplier"], 4.375, 1e-6))
check("反推基础触发 = 10%", close(_wi["procChance"], 0.10, 1e-6))
# 本 fixture 未装毒素 MOD → 毒素行全是自带元素：1125.3/2.2 = 511.5
# （压迫点满级 +120% → after_base = 2.2）
check("反推自带元素毒素 = 1125.3/2.2 ≈ 511.5（无 MOD 时整行即自带）",
      close(_wi["damage"].get("toxin", 0.0), 511.5, 0.05),
      str(_wi["damage"].get("toxin")))
check("反推基础总伤 = IPS 310.0 + 自带毒素 511.5 ≈ 821.5",
      close(_wi["damage"]["total"], 821.5, 0.05), str(_wi["damage"]["total"]))
check("反推路径面板校验开启且全部吻合（净基础+MOD一次）",
      bool(_an_inc.get("checks")) and all(c["ok"] for c in _an_inc["checks"]),
      str(_an_inc.get("checks")))
_sp_inc = lo.to_damage_spec(_an_inc)
check("v1.11：MOD 正常叠算一次（基伤/元素/暴率均保留）",
      close(_sp_inc["base_dmg"], _an_inc["totals"]["base_dmg"])
      and close(_sp_inc["singles"].get("toxin", 0.0),
                _an_inc["totals"]["elements"].get("toxin", 0.0))
      and close(_sp_inc["crit_chance"], _an_inc["totals"]["crit_chance"]),
      f"{_sp_inc['base_dmg']} / {_sp_inc['singles']} / {_sp_inc['crit_chance']}")
check("重击只额外吃 x2 的那份（heavy=MOD，不是 2×MOD）",
      close(_sp_inc["crit_chance_heavy"], _an_inc["totals"]["crit_chance"]),
      str(_sp_inc["crit_chance_heavy"]))
_ri = _an_inc.get("ref")
if _ri:
    check("灵化参考伤害：暴击率 75.6%（MOD 后）、单发 >300",
          _ri["health"] > 300 and close(_ri["crit_cc"], 0.756, 1e-6),
          f"{_ri['health']} / {_ri['crit_cc']}")
else:
    check("灵化参考伤害能算出来", False)

# ③ 非灵化、面板与库一致 → 不触发覆盖（原有行为不变）
_an_x = _an  # 上面 Xoris 主 fixture 的结果
check("普通形态不触发面板覆盖", not (_an_x.get("panel_base") or ""))


# ---------------------------------------------------------------------------
# v1.10b：wfsim 灵化库（incarnon_forms.json）三条路径
# ---------------------------------------------------------------------------
# ④ 布莱顿灵化（库命中）+ 膛线，面板与灵化基础吻合 → 直采灵化库
_ocr_g1 = {"weapon": "升级 / 布莱顿 [30]", "capacity": "4/70",
           "mods": [{"name": "膛线", "drain": "14", "polarity": "?"}],
           "panel": {"crit_chance": "30%", "crit_damage": "3倍",
                     "status_chance": "12%",
                     "damage_rows": [["冲击", "53"], ["穿刺", "5.3"],
                                     ["切割", "74.2"]],
                     "total": ["总计", "132.5"]}}
_an_g1 = lo.analyze(_ocr_g1)
check("灵化库直采：note 标 wfsim 数据源",
      "灵化库" in (_an_g1.get("panel_base") or ""),
      str(_an_g1.get("panel_base")))
check("灵化库直采：基础 = 灵化形态（cc 30%）",
      close(_an_g1["weapon"]["criticalChance"], 0.30, 1e-6),
      str(_an_g1["weapon"]["criticalChance"]))
check("灵化库直采：面板校验保持开启（非反推）",
      len(_an_g1.get("checks") or []) > 0)
_sp_g1 = lo.to_damage_spec(_an_g1)
check("灵化库直采：MOD 正常叠算（膛线 +165%）",
      close(_sp_g1["base_dmg"], 165.0), str(_sp_g1["base_dmg"]))

# ⑤ 灵化进化平坦暴击（面板 cc 39% = 30% + 9% 平坦）→ 反推路径，note 标灵化
_ocr_g2 = {"weapon": "升级 / 布莱顿 [30]", "capacity": "4/70",
           "mods": [{"name": "膛线", "drain": "14", "polarity": "?"}],
           "panel": {"crit_chance": "39%", "crit_damage": "3倍",
                     "status_chance": "12%",
                     "damage_rows": [["冲击", "53"], ["穿刺", "5.3"],
                                     ["切割", "74.2"]],
                     "total": ["总计", "132.5"]}}
_an_g2 = lo.analyze(_ocr_g2)
check("灵化进化平坦加成 → 反推路径 + 标灵化",
      "反推" in (_an_g2.get("panel_base") or "")
      and "灵化" in (_an_g2.get("panel_base") or ""),
      str(_an_g2.get("panel_base")))
check("反推基础暴击吸收平坦加成 = 39%",
      close(_an_g2["weapon"]["criticalChance"], 0.39, 1e-6),
      str(_an_g2["weapon"]["criticalChance"]))

# ⑥ damage_rows 读空 + 总计有值 → 用灵化库向量兜底（IPS 构成取灵化形态）
_ocr_g3 = {"weapon": "升级 / 布莱顿 [30]", "capacity": "4/70",
           "mods": [{"name": "膛线", "drain": "14", "polarity": "?"}],
           "panel": {"crit_chance": "30%", "crit_damage": "3倍",
                     "status_chance": "12%",
                     "damage_rows": [], "total": ["总计", "132.5"]}}
_an_g3 = lo.analyze(_ocr_g3)
_d = _an_g3["weapon"]["damage"]
check("灵化库兜底：IPS 构成取灵化形态（20/2/28）",
      close(_d.get("impact", 0), 20.0, 0.05) and close(_d.get("puncture", 0), 2.0, 0.05)
      and close(_d.get("slash", 0), 28.0, 0.05), str(_d))

# ⑦ 基础形态截图（面板=库内基础）→ 不触发覆盖，哪怕灵化库命中
_ocr_g4 = {"weapon": "升级 / 布莱顿 [30]", "capacity": "4/70",
           "mods": [{"name": "膛线", "drain": "14", "polarity": "?"}],
           "panel": {"crit_chance": "12%", "crit_damage": "1.6倍",
                     "status_chance": "6%",
                     "damage_rows": [["冲击", "20.98"], ["穿刺", "20.99"],
                                     ["切割", "21.63"]],
                     "total": ["总计", "63.6"]}}
_an_g4 = lo.analyze(_ocr_g4)
check("基础形态截图不触发覆盖", not (_an_g4.get("panel_base") or ""),
      str(_an_g4.get("panel_base")))


# ---------------------------------------------------------------------------
# v1.11：家族互斥提示 + 镀层条件堆叠提示
# ---------------------------------------------------------------------------
_ocr_fam = {"weapon": "升级 / 空刃 [30]", "capacity": "4/70",
            "mods": [{"name": "膛线", "drain": "14", "polarity": "?"},
                     {"name": "膛线", "drain": "14", "polarity": "?"}],
            "panel": {}}
_an_fam = lo.analyze(_ocr_fam)
check("同族两张 → 互斥提示",
      any("互斥家族" in n for n in _an_fam.get("notes") or []),
      str(_an_fam.get("notes")))
_ocr_galv = {"weapon": "升级 / 布莱顿 [30]", "capacity": "4/70",
             "mods": [{"name": "镀层 分裂膛室", "drain": "16", "polarity": "?"}],
             "panel": {}}
_an_galv = lo.analyze(_ocr_galv)
check("镀层卡 → 条件堆叠可带入提示",
      any("满镀层" in n for n in _an_galv.get("notes") or []),
      str(_an_galv.get("notes")))


# ---------------------------------------------------------------------------
# v1.11c：聚焦读行拼接（splice_damage_rows）—— 读串场景的自动修复
# ---------------------------------------------------------------------------
_ocr_bad = {"weapon": "升级 / 执法者 [30]", "capacity": "4/70",
            "mods": [{"name": "压迫点", "drain": "9", "polarity": "?"},
                     {"name": "镀层 斩铁", "drain": "6", "polarity": "?"},
                     {"name": "急进猛突", "drain": "7", "polarity": "?"},
                     {"name": "热病打击 Prime", "drain": "8", "polarity": "?"},
                     {"name": "角斗士 威猛", "drain": "9", "polarity": "?"}],
            "panel": {"crit_chance": "75.6%", "crit_damage": "7倍",
                      "status_chance": "10%",
                      "damage_rows": [["冲击", "545.6"], ["穿刺", "1,807.3"],
                                      ["切割", "34.1"], ["毒素", "1,125.3"]],
                      "total": ["", ""]}}
_an_bad = lo.analyze(_ocr_bad)
check("读串场景：总伤校验确实亮 ⚠",
      any(not c["ok"] for c in _an_bad.get("checks") or []))
_rows = [["冲击", 545.6], ["穿刺", 34.1], ["切割", 102.3],
         ["毒素", 1125.3], ["总计", 1807.3]]
_an_fix = lo.analyze(lo.splice_damage_rows(_ocr_bad, _rows))
check("拼接后：面板校验全过",
      all(c["ok"] for c in _an_fix.get("checks") or []),
      str(_an_fix.get("checks")))
check("拼接后：穿刺基础恢复正常（34.1/2.2 ≈ 15.5）",
      close(_an_fix["weapon"]["damage"].get("puncture", 0), 15.5, 0.05),
      str(_an_fix["weapon"]["damage"].get("puncture")))
check("拼接后：满级热病 +165% 完全解释毒素行 → 无自带毒素",
      close(_an_fix["weapon"]["damage"].get("toxin", 0), 0.0, 1e-6),
      str(_an_fix["weapon"]["damage"].get("toxin")))
check("splice 不改原 dict",
      _ocr_bad["panel"]["damage_rows"][1][1] == "1,807.3")


# ---------------------------------------------------------------------------
# v1.11d：新版 UI 标题清洗（等级 N 后缀）+ 三段式伤害行归一
# ---------------------------------------------------------------------------
check("清洗：'拉特昂 PRIME 等级 30' → 拉特昂 PRIME",
      lo.clean_weapon_name("拉特昂 PRIME 等级 30") == "拉特昂 PRIME",
      repr(lo.clean_weapon_name("拉特昂 PRIME 等级 30")))
check("清洗：'升级:拉特昂 PRIME 等级 30' 冒号格式",
      lo.clean_weapon_name("升级:拉特昂 PRIME 等级 30") == "拉特昂 PRIME")
_ocr_tri = {"weapon": "拉特昂 PRIME 等级 30", "capacity": "4/60",
            "mods": [{"name": "膛线", "drain": "7", "polarity": "V"}],
            "panel": {"crit_chance": "138%", "crit_damage": "6.6倍",
                      "status_chance": "41.6%",
                      "damage_rows": [["冲击", "9", "25.4"], ["穿刺", "72", "203.5"],
                                      ["切割", "9", "25.4"], ["病毒 (+)", None],
                                      ["总计", "90", "1,488.3"]],
                      "total": ["总计", "90", "1,488.3"]}}
_an_tri = lo.analyze(_ocr_tri)
check("三段式行 + 等级后缀：武器识别成功",
      (_an_tri["weapon"] or {}).get("zh") == "拉特昂 Prime",
      str((_an_tri["weapon"] or {}).get("zh")))
check("三段式行归一：总伤取最终值 1488.3",
      close(lo._panel_total(_an_tri["panel"]) or 0, 1488.3),
      str(lo._panel_total(_an_tri["panel"])))
check("三段式行归一：不污染原 OCR",
      _ocr_tri["panel"]["damage_rows"][0][1] == "9")


# ---------------------------------------------------------------------------
# v1.11e：极性三态（wiki /w/Polarity：绿=减半 / 红=+25% / 白=原价）
# ---------------------------------------------------------------------------
_TRI = [("压迫点", 9, None, 5), ("镀层 斩铁", 6, "绿", 10),
        ("镀层 斩铁", 15, "红", 10), ("急进猛突", 7, "绿", 10),
        ("热病打击 Prime", 8, "绿", 10), ("压迫点", 11, "红", 5),
        ("一击必杀", 6, "绿", 5), ("镀层 增幅线圈", 12, "白", 10)]
for _zh, _d, _c, _want in _TRI:
    _rec, _ = lo.match_mod(_zh)
    _r, _how = lo.infer_rank(_rec, _d, _c)
    check(f"三态反推 {_zh} drain{_d}[{_c}] → rank{_want}",
          _r == _want, f"{_r} {_how}")
# +25% 数学锚点：压迫点满级红 = round((4+5)×1.25) = 11
_r11, _h11 = lo.infer_rank(lo.match_mod("压迫点")[0], 11, "红")
check("极性不合 +25% 数学锚点（(4+5)×1.25=11.25→11）", _r11 == 5, _h11)


# ---------------------------------------------------------------------------
# v1.11f：识卡第二页（伤害详情）与「识卡伤害」复算管线
# ---------------------------------------------------------------------------
def _detail_lines(an):
    spec = lo.to_damage_spec(an)
    assert spec is not None
    res = dc_mod.calculate(spec, an["weapon"])
    assert res.get("ok"), res
    return dc_mod.card_lines(an["weapon"], spec, res, [])


import core.damage_calc as dc_mod
_detail = _detail_lines(_an_inc)
check("第二页：详情行数 > 15（完整详情卡）", len(_detail) > 15, str(len(_detail)))
check("第二页：含暴击期望与重击段",
      any("暴击期望" in ln for ln in _detail)
      and any("重击" in ln for ln in _detail))
check("第二页：单发与面板对拍一致（456）",
      any("456" in ln for ln in _detail))

from core.parser import parse as _parse
_p = _parse("识卡伤害 对 重机枪手 150级 爆头")
check("「识卡伤害」别名解析 → scandamage",
      _p.command == "scandamage" and "重机枪手" in (_p.content_str or ""),
      f"{_p.command} / {_p.content_str}")
_p2 = _parse("扫伤害 C系 120级")
check("「扫伤害」别名解析", _p2.command == "scandamage")


# ---------------------------------------------------------------------------
# v1.11g：拉特昂 Prime 实测 —— 歧义取高（颜色不可信）+ 幻影自带元素防线
# ---------------------------------------------------------------------------
_cd, _ = lo.match_mod("关键延迟")
_r, _h = lo.infer_rank(_cd, 5, "红")   # 模型把极性图标颜色误读成数字红
check("关键延迟 drain5[红] → 满级 5（红0 与 绿5 双解取高）",
      _r == 5, f"{_r} {_h}")
_vs, _ = lo.match_mod("秘法 补给")
_r2, _h2 = lo.infer_rank(_vs, 5, "红")
check("秘法补给 drain5[红] → 满级 5", _r2 == 5, f"{_r2} {_h2}")

_ocr_lat = {"weapon": "拉特昂 PRIME 等级 30", "capacity": "4/60",
            "mods": [{"name": "镀层 氩晶瞄具", "drain": "6", "color": "绿"},
                     {"name": "关键延迟", "drain": "5", "color": "红"},
                     {"name": "低温弹头 Prime", "drain": "8", "color": "绿"},
                     {"name": "猎人 战备", "drain": "5", "color": "绿"},
                     {"name": "私法 补给", "drain": "5", "color": "红"},
                     {"name": "镀层 分裂膛室", "drain": "8", "color": "绿"},
                     {"name": "膛线", "drain": "7", "color": "绿"},
                     {"name": "弱点感应", "drain": "5", "color": "绿"},
                     {"name": "致命火力", "drain": "7", "color": "绿"}],
            "panel": {"crit_chance": "138%", "crit_damage": "6.6倍",
                      "status_chance": "41.6%",
                      "damage_rows": [["冲击", "9", "25.4"], ["穿刺", "72", "203.5"],
                                      ["切割", "9", "25.4"], ["病毒 (+)", None, "572.4"],
                                      ["总计", "90", "1,488.3"]],
                      "total": ["总计", "90", "1,488.3"]}}
_an_lat = lo.analyze(_ocr_lat)
check("拉特昂：九卡全满级",
      all(m.get("rank") == m.get("max_rank")
          for m in _an_lat["mods"] if m.get("found") and m.get("max_rank")))
# 三段行右列反推 → 基础 = 含 EVO II +6 的有效基础 96（= 90+6，与游戏
# 面板 1488.3 = 96×2.65×3.25×1.8 精确吻合）；无幻影自带元素
check("拉特昂：基础 = 含进化的 96（90+EVO II 6，右列反推）",
      close(_an_lat["weapon"]["damage"]["total"], 96.0, 0.1)
      and "toxin" not in _an_lat["weapon"]["damage"]
      and "cold" not in _an_lat["weapon"]["damage"],
      str(_an_lat["weapon"]["damage"]))
check("拉特昂：面板校验全过（行和自检兜住漏读）",
      bool(_an_lat.get("checks")) and all(c["ok"] for c in _an_lat["checks"]),
      str([(c["label"], c["ok"]) for c in _an_lat.get("checks") or []]))
check("行和自检：唯一校验项 = 总计 = Σ行×(1+多重)",
      any(c["label"] == "行和×多重" for c in _an_lat.get("checks") or []))

if FAILED:
    print(f"\n失败 {len(FAILED)} 项：{FAILED}")
    sys.exit(1)
print("\n全部通过 ✔")
