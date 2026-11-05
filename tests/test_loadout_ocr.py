# -*- coding: utf-8 -*-
"""配卡截图识别离线测试：python3 tests/test_loadout_ocr.py

钉住三件事（这三条一旦悄悄坏了，识别结果就会"看起来对、其实全错"）：

1. **等级反推**：`drain = base + rank`，槽位极性匹配时减半 —— 这是把截图里
   那张卡折算成正确数值的唯一依据（北风 rank1 冰 +30% 与满级 +90% 差 3 倍）；
2. **面板交叉校验**：识别到的面板值必须与本地推算值吻合，否则要报出来，
   不能默默给一组假数字；
3. **端到端**：用维护者 2026-09-16 提供的 Xoris（驱魔之刃）截图真实识别结果
   作为 fixture，9 张卡 + 四项面板校验逐条钉死。
"""

from __future__ import annotations

import sys
import copy
import json as _json_boxes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import damage_calc as dc  # noqa: E402
from core import loadout_ocr as lo  # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = ""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f"  -> {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


def close(a: float, b: float, tol: float = 1e-6) -> bool:
    return abs(a - b) <= tol


def _calc_fixture(observation, ranks):
    """显式计算夹具：已知等级的独立合成豆数/锚点，不测试图像识别。

    每次调用必须逐卡列出等级；不读取max_rank作默认，不改全局analyze。
    None保留等级未知。仅供下面明确列出的公式/灵化计算夹具使用。
    """
    import copy

    observed = copy.deepcopy(observation)
    assert len(ranks) == len(observed["mods"])
    slots, rows = [], []
    for index, (raw, rank) in enumerate(zip(observed["mods"], ranks)):
        raw["_geometry_source"] = "coordinate_ocr"
        raw["_geometry_view"] = "full_image"
        if rank is None:
            continue
        y = (index + 1) * 80
        raw["_geometry"] = {"x": 500, "y": y}
        box = [0.45, (y - 20) / 1000, 0.55, (y + 20) / 1000]
        line = [0.45, (y + 19) / 1000, 0.55, (y + 21) / 1000]
        slots.append(
            {
                "id": f"fixture-{index}",
                "box": box,
                "line_box": line,
                "kind": "regular_candidate",
                "zone": "equipment_candidate",
                "content_state": "rank_marks_observed",
            }
        )
        rows.append(
            {
                "counts": [rank],
                "grid": [500],
                "band": [y + 19, y + 21],
                "image_size": [1000, 1000],
                "card_w": 50,
                "is_inventory": False,
                "maxed": [False],
            }
        )
    observed["_pixel_layout"] = {
        "source": "pixel_geometry",
        "slots": slots,
        "coverage": {"status": "unknown", "equipment": "unknown", "special_slots": "unknown"},
    }
    return lo.analyze(observed, rows)


# ---------------------------------------------------------------------------
# 1) 文本清洗与容错（模型输出常带装饰符号）
# ---------------------------------------------------------------------------
check(
    "武器名清洗：'+ 升级 / 驱魔之刃 [30]' → 驱魔之刃",
    lo.clean_weapon_name("+ 升级 / 驱魔之刃 [30]") == "驱魔之刃",
)
check("武器名清洗：'升级/驱魔之刃[30]'", lo.clean_weapon_name("升级/驱魔之刃[30]") == "驱魔之刃")
check("武器名清洗：已是纯名（不带尾部等级）", lo.clean_weapon_name("绝路 Prime") == "绝路 Prime")
check(
    "to_int 容忍上下标/箭头：'^10' '7→' '9↘'",
    lo.to_int("^10") == 10 and lo.to_int("7→") == 7 and lo.to_int("9↘") == 9,
)
check("to_int 直接吃数字", lo.to_int(11) == 11)
check("to_int 无数字返回 None", lo.to_int("—") is None)
check(
    "to_float：'44%' '4.6倍' '40.8'",
    close(lo.to_float("44%"), 44)
    and close(lo.to_float("4.6倍"), 4.6)
    and close(lo.to_float("40.8"), 40.8),
)
check("to_pair：'36>144'", lo.to_pair("36>144") == (36.0, 144.0))
check(
    "to_pair：['总计','156>264'] 必须继续拆字符串（曾在这里取错值）",
    lo.to_pair(["总计", "156>264"]) == (156.0, 264.0),
)
check("to_pair：['36','144']", lo.to_pair(["36", "144"]) == (36.0, 144.0))
check("to_pair：单值", lo.to_pair("24") == (24.0, None))

# ---------------------------------------------------------------------------
# 2) 模型输出的 JSON 提取
# ---------------------------------------------------------------------------
check("剥 markdown 围栏", lo.parse_vision_json('```json\n{"a": 1}\n```') == {"a": 1})
check(
    "剥思考段（glm-4.1v-thinking-flash 会先吐 <think>）",
    lo.parse_vision_json('<think>这里可能有 {花括号}</think>\n{"b": 2}') == {"b": 2},
)
check("剥前后废话", lo.parse_vision_json('好的：\n{"c": 3}\n以上') == {"c": 3})
check("容忍尾随逗号", lo.parse_vision_json('{"d": [1, 2,],}') == {"d": [1, 2]})
check("完全不是 JSON → 空 dict", lo.parse_vision_json("抱歉我看不清") == {})

# ---------------------------------------------------------------------------
# 3) MOD 名称匹配
# ---------------------------------------------------------------------------
_r, _how = lo.match_mod("熔岩冲击")
check("MOD 精确匹配（中文名）", _r is not None and _r.get("name") == "Molten Impact", f"{_how}")
check("MOD 英文名直输", (lo.match_mod("Molten Impact")[0] or {}).get("zh") == "熔岩冲击")
check("MOD 带空格差异（'true steel'）", (lo.match_mod("true steel")[0] or {}).get("zh") == "斩铁")
check("库里没有的名字 → (None, '')", lo.match_mod("不存在的卡") == (None, ""))
check("空串 → (None, '')", lo.match_mod("") == (None, ""))

# ---------------------------------------------------------------------------
# 4) 容量数字反推等级（识别功能的核心）
# ---------------------------------------------------------------------------
_molten = lo.mod_pool()["molten impact"]
check("熔岩冲击：base=6 max=5", _molten["base_drain"] == 6 and _molten["max_rank"] == 5)
check("drain 11 = 6+5 → rank 5（未减半）", lo.infer_rank(_molten, 11, "白")[0] == 5)
check("drain 9 = 6+3 → rank 3", lo.infer_rank(_molten, 9, "白")[0] == 3)
check("drain 6 无色多解→unknown", lo.infer_rank(_molten, 6)[0] is None)
check("drain 12 超出 max_rank → None（不许瞎猜）", lo.infer_rank(_molten, 12)[0] is None)

_vol = lo.mod_pool()["volatile rebound"]
check("易爆反弹：base=10 max=3", _vol["base_drain"] == 10 and _vol["max_rank"] == 3)
_vr, _vw = lo.infer_rank(_vol, 7)
check(
    "drain 7 = ceil((10+3)/2) → rank 3（槽位极性匹配减半）",
    _vr == 3 and "减半" in _vw,
    f"{_vr} / {_vw}",
)

_gleam = lo.mod_pool()["gleaming talon"]
check(
    "姿态卡可定级且仍不参与伤害折算",
    lo.infer_rank(_gleam, 10)[0] == 3 and not _gleam.get("calculable"),
)

check(
    "等级未知时 effect_at 退回满级值",
    close(lo.effect_at(_molten, None).get("elements", {}).get("heat", 0), 90.0),
)
check(
    "按等级取真实数值：rank1 → 火+30%",
    close(lo.effect_at(_molten, 1).get("elements", {}).get("heat", 0), 30.0),
)
check(
    "按等级取真实数值：rank5 → 火+90%",
    close(lo.effect_at(_molten, 5).get("elements", {}).get("heat", 0), 90.0),
)

# ---------------------------------------------------------------------------
# 5) 历史Xoris文字fixture + 显式合成等级观察的计算回归（不测图像准确率）
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
        "attack_speed": "1.17",
        "crit_chance": "44%",
        "crit_damage": "4.6倍",
        "status_chance": "18%",
        "damage_rows": [
            ["暴击几率", "44%", None],
            ["暴击伤害", "4.6倍", None],
            ["触发几率", "18%", None],
            ["* 爆炸 (A + *)", "36>144", None],
            ["冲击", "24", None],
            ["穿刺", "40.8", None],
            ["切割", "55.2", None],
            ["总计", "156>264", None],
        ],
        "total": ["总计", "156>264"],
        "heavy": ["重击", "343.2>580.8"],
    },
}
_an = _calc_fixture(OCR, [None, 3, 5, 3, 5, 5, 5, 1, 5])

check("武器解析为 Xoris（官方简中名「驱魔之刃」）", (_an["weapon"] or {}).get("name") == "Xoris")
check("9 张卡全部匹配上库", len(_an["mods"]) == 9 and all(m["found"] for m in _an["mods"]))
check("没有未收录的名字", _an["unknown"] == [], str(_an["unknown"]))

_by_zh = {m.get("zh"): m for m in _an["mods"]}
_ranks = {zh: m.get("rank") for zh, m in _by_zh.items()}
check(
    "已知等级合成观察传入真实融合，八张计算卡保持等级",
    _ranks
    == {
        "微光利爪": 3,
        "易爆反弹": 3,
        "熔岩冲击": 5,
        "邪恶蓄力": 3,
        "肢解": 5,
        "一击必杀": 5,
        "奋力一掷": 5,
        "北风": 1,
        "斩铁": 5,
    },
    str(_ranks),
)
check(
    "★ 北风是 rank 1（非满级）—— 这条就是「MOD 等级差异」的活样本",
    close(lo.effect_at(lo.mod_pool()["north wind"], 1).get("elements", {}).get("cold", 0), 30.0)
    and close(
        lo.effect_at(lo.mod_pool()["north wind"], 5).get("elements", {}).get("cold", 0), 90.0
    ),
)

_t = _an["totals"]
check("折算加成：暴击率 +120%（斩铁满级）", close(_t["crit_chance"], 120.0))
check("折算加成：暴伤 +90%（肢解满级）", close(_t["crit_dmg"], 90.0))
check("折算加成：重击伤害 +120%（一击必杀）", close(_t["heavy_dmg"], 120.0))
check("折算加成：初始连击 +30（邪恶蓄力）", close(_t["initial_combo"], 30.0))
check(
    "折算加成：火 90% / 冰 30%",
    close(_t["elements"].get("heat", 0), 90.0) and close(_t["elements"].get("cold", 0), 30.0),
)
check(
    "合成元素：火+冰 → 爆炸 120%",
    list(_t["combined"]) == ["blast"] and close(_t["combined"]["blast"], 120.0),
)

check("推算面板：暴击率 44%", close(_an["calc_panel"]["crit_chance"], 0.44))
check("推算面板：暴伤 4.56 倍", close(_an["calc_panel"]["crit_damage"], 4.56))
check("推算面板：总伤害 264", close(_an["calc_panel"]["total"], 264.0))
check(
    "★ 库基础路径面板可比项目吻合（含行和自检，不代表图像真值）",
    len(_an["checks"]) >= 4 and all(c["ok"] for c in _an["checks"]),
    str([(c["label"], c["panel"], round(c["calc"], 3), c["ok"]) for c in _an["checks"]]),
)
check("分析整体 ok", _an["ok"] is True)

_lines = lo.card_lines(_an)
check("卡面非空且含武器行", any("驱魔之刃" in ln for ln in _lines))
check("卡面标注了非满级的卡", any("非满级" in ln and "北风" in ln for ln in _lines))
check("卡面写出了「按实际等级折算」", any("按实际等级折算" in ln for ln in _lines))


# 折算结果能直接喂给伤害计算器（不抛异常、不吃 KeyError）
def _manual_spec(an, *args, **kwargs):
    """显式手工字段夹具：仅测映射/伤害语义，不代表截图获准计算。"""
    return lo.to_damage_spec({k: v for k, v in an.items() if k != "quality"}, *args, **kwargs)


_spec = _manual_spec(_an)
check(
    "to_damage_spec 产出可用 spec",
    isinstance(_spec, dict)
    and close(_spec["crit_chance"], 120.0)
    and _spec["singles"].get("heat") == 90.0,
)
_res = dc.calculate(_spec, _an["weapon"])
check(
    "识别结果可直接跑伤害计算",
    _res.get("ok") is True and _res["health"] > 0,
    str(_res.get("health")),
)
check(
    "重击数据被带出来（配了一击必杀）",
    _res.get("heavy") is not None and close(_res["heavy"]["dmg_bonus"], 120.0),
)

# ---------------------------------------------------------------------------
# 6) 边界：认不出来时要"说出来"，不能静默给假数字
# ---------------------------------------------------------------------------
_an2 = lo.analyze({"weapon": "米老鼠", "mods": [{"name": "不存在卡", "drain": 3}]})
check(
    "武器认不出 → ok=False 且带错误文案",
    _an2["ok"] is False and any("米老鼠" in e for e in _an2["errors"]),
)
check("认不出的 MOD 记进 unknown", _an2["unknown"] == ["不存在卡"], str(_an2["unknown"]))
check("空输入不抛异常", lo.analyze({})["ok"] is False)
check("to_damage_spec 无武器时返回 None", _manual_spec({}) is None)

_an3 = lo.analyze({"weapon": "驱魔之刃", "mods": [{"name": "熔岩冲击", "drain": "999"}]})
check(
    "等级推不出来时按满级估并标记",
    _an3["totals"]["no_rank"] == ["熔岩冲击"],
    str(_an3["totals"]["no_rank"]),
)
check(
    "该情形下卡面给出警告",
    any("等级未定" in ln or "等级无法确定" in ln for ln in lo.card_lines(_an3)),
)


# ---------------------------------------------------------------------------
# 7) v1.8：斩铁「重击时 x2」与奋力一掷「连续投掷伤害」（维护者 2026-09-16 报漏算）
#    这两条**一直都在数据里**：前者在效果文本的括号里，后者在 stats 的第二行。
# ---------------------------------------------------------------------------
check(
    "斩铁效果含「重击时暴击几率 ×2」",
    (lo.effect_at(lo.mod_pool()["true steel"], 5)).get("heavy_crit_mult") == 2.0,
)
_pt5 = lo.effect_at(lo.mod_pool()["power throw"], 5)
check(
    "奋力一掷满级：投掷伤害 +100%/层、最多 3 层",
    close(_pt5.get("throw_dmg", 0), 100.0) and _pt5.get("throw_max_stacks") == 3,
    str(_pt5),
)
check(
    "奋力一掷 1 级 → +33.3%（与 wiki 表逐级核对）",
    close(lo.effect_at(lo.mod_pool()["power throw"], 1).get("throw_dmg", 0), 33.3),
)
check(
    "奋力一掷 3 级 → +66.7%",
    close(lo.effect_at(lo.mod_pool()["power throw"], 3).get("throw_dmg", 0), 66.7),
)
check(
    "折算：重击时暴击率再 +120%（= 斩铁 120% × (2−1)）",
    close(_t["crit_chance_heavy"], 120.0),
    str(_t["crit_chance_heavy"]),
)
check(
    "折算：投掷伤害 +100%/层、最多 3 层",
    close(_t["throw_dmg"], 100.0) and _t["throw_max_stacks"] == 3,
)
check("卡面写出「连续投掷伤害」", any("连续投掷伤害" in ln for ln in _lines))
check("卡面写出「重击时暴击几率 ×2」", any("重击时暴击几率" in ln for ln in _lines))
check("未知布局不展示重击参考数值", not any("重击暴击率" in ln for ln in _lines))
check("未知布局不展示投掷参考数值", not any("投掷（连续 3/3 层" in ln for ln in _lines))


# ---------------------------------------------------------------------------
# v1.9 补测：非满级的新字段折算 + 灵化（Incarnon）面板反推
# ---------------------------------------------------------------------------
# ① 急进猛突容量7多解；计算夹具显式提供独立10级豆数
_ocr_br = {
    "weapon": "升级 / 空刃 [30]",
    "capacity": "4/70",
    "mods": [{"name": "急进猛突", "drain": "7", "polarity": "?"}],
    "panel": {},
}
_an_br = _calc_fixture(_ocr_br, [10])
check(
    "急进猛突独立10级豆数 → +40% 暴击率/连击倍率",
    close(_an_br["totals"]["crit_per_combo"], 40.0, 1e-6),
    str(_an_br["totals"]["crit_per_combo"]),
)
_sp_br = _manual_spec(_an_br)
check("crit_per_combo 进 spec", close(_sp_br["crit_per_combo"], 40.0))
check("卡面写出随连击的暴击率", any("暴击率/连击倍率" in ln for ln in lo.card_lines(_an_br)))

# ② 灵化执法者：面板是「含 MOD 的最终值」，要反推基础而不是直接当基础
_ocr_inc = {
    "weapon": "升级 / 执法者 [30]",
    "capacity": "4/70",
    "mods": [
        {"name": "压迫点", "drain": "9", "polarity": "?"},
        {"name": "镀层 斩铁", "drain": "6", "polarity": "?"},
        {"name": "急进猛突", "drain": "7", "polarity": "?"},
        {"name": "角斗士 威猛", "drain": "9", "polarity": "?"},
        # ★ 2026-10-05 订正：真实卡装了热病打击 Prime（旧 fixture 漏了它、
        #   还把穿刺/切割两行的值互换 —— 那套输入迫使反推编造「自带毒 511.5」，
        #   即本批证伪并修掉的幻影；详见 output/分析-灵化执法者配卡识别差异-*.md）
        {"name": "热病打击 Prime", "drain": "8", "polarity": "?"},
    ],
    "panel": {
        "attack_speed": "0.833",
        "crit_chance": "75.6%",
        "crit_damage": "7倍",
        "status_chance": "10%",
        "damage_rows": [
            ["毒素", "1,125.3"],
            ["冲击", "545.6"],
            ["穿刺", "102.3"],
            ["切割", "34.1"],
        ],
        "total": ["总计", "1,807.3"],
    },
}
_an_inc = _calc_fixture(_ocr_inc, [5, 10, 10, 5, 10])
check("识别出灵化形态", "灵化" in (_an_inc.get("panel_base") or ""), str(_an_inc.get("panel_base")))
_wi = _an_inc["weapon"]
check(
    "反推基础暴击 = 75.6% / 2.1 = 36%（斩铁满级 +110%）",
    close(_wi["criticalChance"], 0.36, 1e-6),
    str(_wi.get("criticalChance")),
)
check("反推基础暴伤 = 7 / 1.6 = 4.375", close(_wi["criticalMultiplier"], 4.375, 1e-6))
check("反推基础触发 = 10%", close(_wi["procChance"], 0.10, 1e-6))
# ★ 2026-10-05 订正：旧断言「自带毒 511.5、总伤 821.5」建立在被删掉的
#   毒素 MOD 与互换的行名上 —— 真值是基础 310（210 + 灵化 +100 按比例分摊）、
#   **无自带毒素**（毒 100% 来自热病打击 Prime：682×1.65 = 1125.3）；
#   面板四行校验因此全过（见下「反推路径面板校验」）。幻影由防线③拦截。
check(
    "反推基础 = 310（210 + 灵化 +100），无幻影自带毒素",
    _wi["damage"].get("impact") == 248
    and _wi["damage"].get("puncture") == 46.5
    and _wi["damage"].get("slash") == 15.5
    and close(_wi["damage"]["total"], 310.0, 0.05)
    and not _wi["damage"].get("toxin"),
    str(_wi["damage"]),
)
check(
    "  note 带灵化对账（310 ＝ 210 + 100）；毒素行被 MOD 完全解释、无需幻影",
    "灵化进化 +100" in (_an_inc.get("panel_base") or "") and not _wi["damage"].get("toxin"),
    str(_an_inc.get("panel_base")),
)
check(
    "反推路径面板校验开启且全部吻合（净基础+MOD一次）",
    bool(_an_inc.get("checks")) and all(c["ok"] for c in _an_inc["checks"]),
    str(_an_inc.get("checks")),
)
_sp_inc = _manual_spec(_an_inc)
check(
    "v1.11：MOD 正常叠算一次（基伤/元素/暴率均保留）",
    close(_sp_inc["base_dmg"], _an_inc["totals"]["base_dmg"])
    and close(_sp_inc["singles"].get("toxin", 0.0), _an_inc["totals"]["elements"].get("toxin", 0.0))
    and close(_sp_inc["crit_chance"], _an_inc["totals"]["crit_chance"]),
    f"{_sp_inc['base_dmg']} / {_sp_inc['singles']} / {_sp_inc['crit_chance']}",
)
check(
    "重击只额外吃 x2 的那份（heavy=MOD，不是 2×MOD）",
    close(_sp_inc["crit_chance_heavy"], _an_inc["totals"]["crit_chance"]),
    str(_sp_inc["crit_chance_heavy"]),
)
_ri = dc.calculate(_manual_spec(_an_inc), _an_inc["weapon"])
if _ri:
    check(
        "灵化参考伤害：暴击率 75.6%（MOD 后）、单发 >300",
        _ri["health"] > 300 and close(_ri["crit_cc"], 0.756, 1e-6),
        f"{_ri['health']} / {_ri['crit_cc']}",
    )
else:
    check("灵化参考伤害能算出来", False)

# ③ 非灵化、面板与库一致 → 不触发覆盖（原有行为不变）
_an_x = _an  # 上面 Xoris 主 fixture 的结果
check("普通形态不触发面板覆盖", not (_an_x.get("panel_base") or ""))


# ---------------------------------------------------------------------------
# v1.10b：wfsim 灵化库（incarnon_forms.json）三条路径
# ---------------------------------------------------------------------------
# ④ 布莱顿灵化（库命中）+ 膛线，面板与灵化基础吻合 → 直采灵化库
_ocr_g1 = {
    "weapon": "升级 / 布莱顿 [30]",
    "capacity": "4/70",
    "mods": [{"name": "膛线", "drain": "14", "polarity": "?"}],
    "panel": {
        "crit_chance": "30%",
        "crit_damage": "3倍",
        "status_chance": "12%",
        "damage_rows": [["冲击", "53"], ["穿刺", "5.3"], ["切割", "74.2"]],
        "total": ["总计", "132.5"],
    },
}
_an_g1 = _calc_fixture(_ocr_g1, [10])
check(
    "灵化库直采：note 标灵化库数据源",
    "灵化库" in (_an_g1.get("panel_base") or ""),
    str(_an_g1.get("panel_base")),
)
check(
    "灵化库直采：基础 = 灵化形态（cc 30%）",
    close(_an_g1["weapon"]["criticalChance"], 0.30, 1e-6),
    str(_an_g1["weapon"]["criticalChance"]),
)
check("灵化库直采：面板校验保持开启（非反推）", len(_an_g1.get("checks") or []) > 0)
_sp_g1 = _manual_spec(_an_g1)
check(
    "灵化库直采：MOD 正常叠算（膛线 +165%）",
    close(_sp_g1["base_dmg"], 165.0),
    str(_sp_g1["base_dmg"]),
)

# ⑤ 灵化进化平坦暴击（面板 cc 39% = 30% + 9% 平坦）→ 反推路径，note 标灵化
_ocr_g2 = {
    "weapon": "升级 / 布莱顿 [30]",
    "capacity": "4/70",
    "mods": [{"name": "膛线", "drain": "14", "polarity": "?"}],
    "panel": {
        "crit_chance": "39%",
        "crit_damage": "3倍",
        "status_chance": "12%",
        "damage_rows": [["冲击", "53"], ["穿刺", "5.3"], ["切割", "74.2"]],
        "total": ["总计", "132.5"],
    },
}
_an_g2 = _calc_fixture(_ocr_g2, [10])
check(
    "灵化进化平坦加成 → 反推路径 + 标灵化",
    "反推" in (_an_g2.get("panel_base") or "") and "灵化" in (_an_g2.get("panel_base") or ""),
    str(_an_g2.get("panel_base")),
)
check(
    "反推基础暴击吸收平坦加成 = 39%",
    close(_an_g2["weapon"]["criticalChance"], 0.39, 1e-6),
    str(_an_g2["weapon"]["criticalChance"]),
)

# ⑥ damage_rows 读空 + 总计有值 → 用灵化库向量兜底（IPS 构成取灵化形态）
_ocr_g3 = {
    "weapon": "升级 / 布莱顿 [30]",
    "capacity": "4/70",
    "mods": [{"name": "膛线", "drain": "14", "polarity": "?"}],
    "panel": {
        "crit_chance": "30%",
        "crit_damage": "3倍",
        "status_chance": "12%",
        "damage_rows": [],
        "total": ["总计", "132.5"],
    },
}
_an_g3 = _calc_fixture(_ocr_g3, [10])
_d = _an_g3["weapon"]["damage"]
check(
    "灵化库兜底：IPS 构成取灵化形态（20/2/28）",
    close(_d.get("impact", 0), 20.0, 0.05)
    and close(_d.get("puncture", 0), 2.0, 0.05)
    and close(_d.get("slash", 0), 28.0, 0.05),
    str(_d),
)

# ⑦ 基础形态截图（面板=库内基础）→ 不触发覆盖，哪怕灵化库命中
_ocr_g4 = {
    "weapon": "升级 / 布莱顿 [30]",
    "capacity": "4/70",
    "mods": [{"name": "膛线", "drain": "14", "polarity": "?"}],
    "panel": {
        "crit_chance": "12%",
        "crit_damage": "1.6倍",
        "status_chance": "6%",
        "damage_rows": [["冲击", "20.98"], ["穿刺", "20.99"], ["切割", "21.63"]],
        "total": ["总计", "63.6"],
    },
}
_an_g4 = _calc_fixture(_ocr_g4, [10])
check("基础形态截图不触发覆盖", not (_an_g4.get("panel_base") or ""), str(_an_g4.get("panel_base")))


# ---------------------------------------------------------------------------
# v1.11：家族互斥提示 + 镀层条件堆叠提示
# ---------------------------------------------------------------------------
_ocr_fam = {
    "weapon": "升级 / 空刃 [30]",
    "capacity": "4/70",
    "mods": [
        {"name": "膛线", "drain": "14", "polarity": "?"},
        {"name": "膛线", "drain": "14", "polarity": "?"},
    ],
    "panel": {},
}
_an_fam = lo.analyze(_ocr_fam)
check(
    "同族两张 → 互斥提示",
    any("互斥家族" in n for n in _an_fam.get("notes") or []),
    str(_an_fam.get("notes")),
)
_ocr_galv = {
    "weapon": "升级 / 布莱顿 [30]",
    "capacity": "4/70",
    "mods": [{"name": "镀层 分裂膛室", "drain": "16", "polarity": "?"}],
    "panel": {},
}
_an_galv = lo.analyze(_ocr_galv)
check(
    "镀层卡 → 条件堆叠可带入提示",
    any("满镀层" in n for n in _an_galv.get("notes") or []),
    str(_an_galv.get("notes")),
)


# ---------------------------------------------------------------------------
# v1.11c：聚焦读行拼接（splice_damage_rows）—— 读串场景的自动修复
# ---------------------------------------------------------------------------
_ocr_bad = {
    "weapon": "升级 / 执法者 [30]",
    "capacity": "4/70",
    "mods": [
        {"name": "压迫点", "drain": "9", "polarity": "?"},
        {"name": "镀层 斩铁", "drain": "6", "polarity": "?"},
        {"name": "急进猛突", "drain": "7", "polarity": "?"},
        {"name": "热病打击 Prime", "drain": "8", "polarity": "?"},
        {"name": "角斗士 威猛", "drain": "9", "polarity": "?"},
    ],
    "panel": {
        "crit_chance": "75.6%",
        "crit_damage": "7倍",
        "status_chance": "10%",
        "damage_rows": [
            ["冲击", "545.6"],
            ["穿刺", "1,807.3"],
            ["切割", "34.1"],
            ["毒素", "1,125.3"],
        ],
        "total": ["", ""],
    },
}
_an_bad = _calc_fixture(_ocr_bad, [5, 10, 10, 10, 5])
check("读串场景：总伤校验确实亮 ⚠", any(not c["ok"] for c in _an_bad.get("checks") or []))
_rows = [["冲击", 545.6], ["穿刺", 34.1], ["切割", 102.3], ["毒素", 1125.3], ["总计", 1807.3]]
_an_fix = _calc_fixture(lo.splice_damage_rows(_ocr_bad, _rows), [5, 10, 10, 10, 5])
check(
    "拼接后：面板校验全过",
    all(c["ok"] for c in _an_fix.get("checks") or []),
    str(_an_fix.get("checks")),
)
# ★ 2026-10-09 订正：本 fixture 的「穿刺 34.1 / 切割 102.3」本身是对调的读数 ——
#   同一张截图的另两份 fixture（_ocr_inc、遮挡用例的面板重建）都是穿刺 102.3 /
#   切割 34.1，库内构成 168/31.5/10.5 ×1.476×2.2 也给出 545.6/102.3/34.1。
#   旧断言「穿刺 15.5」把对调值钉成了预期；物理行串行纠正后应为 102.3/2.2 = 46.5
check(
    "拼接后：穿刺/切割对调被纠回（穿刺 102.3/2.2 ≈ 46.5、切割 34.1/2.2 ≈ 15.5）",
    close(_an_fix["weapon"]["damage"].get("puncture", 0), 46.5, 0.05)
    and close(_an_fix["weapon"]["damage"].get("slash", 0), 15.5, 0.05),
    str(_an_fix["weapon"]["damage"]),
)
check(
    "拼接后：满级热病 +165% 完全解释毒素行 → 无自带毒素",
    close(_an_fix["weapon"]["damage"].get("toxin", 0), 0.0, 1e-6),
    str(_an_fix["weapon"]["damage"].get("toxin")),
)
check("splice 不改原 dict", _ocr_bad["panel"]["damage_rows"][1][1] == "1,807.3")


# ---------------------------------------------------------------------------
# v1.11d：新版 UI 标题清洗（等级 N 后缀）+ 三段式伤害行归一
# ---------------------------------------------------------------------------
check(
    "清洗：'拉特昂 PRIME 等级 30' → 拉特昂 PRIME",
    lo.clean_weapon_name("拉特昂 PRIME 等级 30") == "拉特昂 PRIME",
    repr(lo.clean_weapon_name("拉特昂 PRIME 等级 30")),
)
check(
    "清洗：'升级:拉特昂 PRIME 等级 30' 冒号格式",
    lo.clean_weapon_name("升级:拉特昂 PRIME 等级 30") == "拉特昂 PRIME",
)
_ocr_tri = {
    "weapon": "拉特昂 PRIME 等级 30",
    "capacity": "4/60",
    "mods": [{"name": "膛线", "drain": "7", "polarity": "V"}],
    "panel": {
        "crit_chance": "138%",
        "crit_damage": "6.6倍",
        "status_chance": "41.6%",
        "damage_rows": [
            ["冲击", "9", "25.4"],
            ["穿刺", "72", "203.5"],
            ["切割", "9", "25.4"],
            ["病毒 (+)", None],
            ["总计", "90", "1,488.3"],
        ],
        "total": ["总计", "90", "1,488.3"],
    },
}
_an_tri = lo.analyze(_ocr_tri)
check(
    "三段式行 + 等级后缀：武器识别成功",
    (_an_tri["weapon"] or {}).get("zh") == "拉特昂 Prime",
    str((_an_tri["weapon"] or {}).get("zh")),
)
check(
    "三段式行归一：总伤取最终值 1488.3",
    close(lo._panel_total(_an_tri["panel"]) or 0, 1488.3),
    str(lo._panel_total(_an_tri["panel"])),
)
check("三段式行归一：不污染原 OCR", _ocr_tri["panel"]["damage_rows"][0][1] == "9")


# ---------------------------------------------------------------------------
# v1.11e：极性三态（wiki /w/Polarity：绿=减半 / 红=+25% / 白=原价）
# ---------------------------------------------------------------------------
_TRI = [
    ("压迫点", 9, None, None),
    ("镀层 斩铁", 6, "绿", None),
    ("镀层 斩铁", 15, "红", 10),
    ("急进猛突", 7, "绿", None),
    ("热病打击 Prime", 8, "绿", None),
    ("压迫点", 11, "红", 5),
    ("一击必杀", 6, "绿", 5),
    ("镀层 增幅线圈", 12, "白", 10),
]
for _zh, _d, _c, _want in _TRI:
    _rec, _ = lo.match_mod(_zh)
    _r, _how = lo.infer_rank(_rec, _d, _c)
    check(f"三态反推 {_zh} drain{_d}[{_c}] → rank{_want}", _r == _want, f"{_r} {_how}")
# +25% 数学锚点：压迫点满级红 = round((4+5)×1.25) = 11
_r11, _h11 = lo.infer_rank(lo.match_mod("压迫点")[0], 11, "红")
check("极性不合 +25% 数学锚点（(4+5)×1.25=11.25→11）", _r11 == 5, _h11)


# ---------------------------------------------------------------------------
# v1.11f：识卡第二页（伤害详情）与「识卡伤害」复算管线
# ---------------------------------------------------------------------------
def _detail_lines(an):
    spec = _manual_spec(an)
    assert spec is not None
    res = dc_mod.calculate(spec, an["weapon"])
    assert res.get("ok"), res
    return dc_mod.card_lines(an["weapon"], spec, res, [])


# 脚本式用例：这两个导入故意放在用到处（上方已跑完前置断言）
import core.damage_calc as dc_mod  # noqa: E402

_detail = _detail_lines(_an_inc)
check("第二页：详情行数 > 15（完整详情卡）", len(_detail) > 15, str(len(_detail)))
check(
    "第二页：含暴击期望与重击段",
    any("暴击期望" in ln for ln in _detail) and any("重击" in ln for ln in _detail),
)
# 第二页单发须与第一页「参考」单发同值（同一 calculate 管线自洽）。
# 2026-10-10 改：原写死 456 —— 那是本引擎算出来的值（不是游戏读数），加伤害量化后为 462
# 2026-10-11 卡片改版：第二页「单发对血：462」→ 单发表行「· 对血　462（无暴击）…」
_ref_h = f"· 对血　{_ri.get('health', -1):.0f}（无暴击）"
check(
    "手工字段详情与独立手工复算同值（不作为截图自动许可）",
    _ref_h.startswith("· 对血　462") and any(ln.startswith(_ref_h) for ln in _detail),
    _ref_h,
)

from core.parser import parse as _parse  # noqa: E402

_p = _parse("识卡伤害 对 重机枪手 150级 爆头")
check(
    "「识卡伤害」别名解析 → scandamage",
    _p.command == "scandamage" and "重机枪手" in (_p.content_str or ""),
    f"{_p.command} / {_p.content_str}",
)
_p2 = _parse("扫伤害 C系 120级")
check("「扫伤害」别名解析", _p2.command == "scandamage")


# ---------------------------------------------------------------------------
# v1.11g：拉特昂计算夹具独立等级观察 + 错色冲突负例 + 幻影自带元素防线
# ---------------------------------------------------------------------------
_cd, _ = lo.match_mod("关键延迟")
_r, _h = lo.infer_rank(_cd, 5, "红")  # 模型把极性图标颜色误读成数字红
check("关键延迟 drain5[红] → 0级", _r == 0, f"{_r} {_h}")
_vs, _ = lo.match_mod("秘法 补给")
_r2, _h2 = lo.infer_rank(_vs, 5, "红")
check("秘法补给 drain5[红] → 0级", _r2 == 0, f"{_r2} {_h2}")

_ocr_lat = {
    "weapon": "拉特昂 PRIME 等级 30",
    "capacity": "4/60",
    "mods": [
        {"name": "镀层 氩晶瞄具", "drain": "6", "color": "绿"},
        {"name": "关键延迟", "drain": "5", "color": "绿"},
        {"name": "低温弹头 Prime", "drain": "8", "color": "绿"},
        {"name": "猎人 战备", "drain": "5", "color": "绿"},
        {"name": "私法 补给", "drain": "5", "color": "绿"},
        {"name": "镀层 分裂膛室", "drain": "8", "color": "绿"},
        {"name": "膛线", "drain": "7", "color": "绿"},
        {"name": "弱点感应", "drain": "5", "color": "绿"},
        {"name": "致命火力", "drain": "7", "color": "白"},
    ],
    "panel": {
        "crit_chance": "138%",
        "crit_damage": "6.6倍",
        "status_chance": "41.6%",
        "damage_rows": [
            ["冲击", "9", "25.4"],
            ["穿刺", "72", "203.5"],
            ["切割", "9", "25.4"],
            ["病毒 (+)", None, "572.4"],
            ["总计", "90", "1,488.3"],
        ],
        "total": ["总计", "90", "1,488.3"],
    },
}
_lat_wrong_color = copy.deepcopy(_ocr_lat)
_lat_wrong_color["mods"][1]["color"] = "红"
_lat_wrong_analysis = _calc_fixture(_lat_wrong_color, [10, 5, 10, 5, 5, 10, 10, 5, 3])
check(
    "独立满级豆与误读红色冲突保留unknown",
    _lat_wrong_analysis["mods"][1]["rank"] is None and _lat_wrong_analysis["pips"]["conflict"] == 1,
)
_an_lat = _calc_fixture(_ocr_lat, [10, 5, 10, 5, 5, 10, 10, 5, 3])
check(
    "拉特昂合成计算夹具：九卡等级由显式豆数观察确认",
    all(
        m.get("rank") == m.get("max_rank")
        for m in _an_lat["mods"]
        if m.get("found") and m.get("max_rank")
    ),
)
# 三段行右列反推 → 基础 = 含 EVO II +6 的有效基础 96（= 90+6，与游戏
# 面板 1488.3 = 96×2.65×3.25×1.8 精确吻合）；无幻影自带元素
check(
    "拉特昂：基础 = 含进化的 96（90+EVO II 6，右列反推）",
    close(_an_lat["weapon"]["damage"]["total"], 96.0, 0.1)
    and "toxin" not in _an_lat["weapon"]["damage"]
    and "cold" not in _an_lat["weapon"]["damage"],
    str(_an_lat["weapon"]["damage"]),
)
check(
    "拉特昂：面板校验全过（行和自检兜住漏读）",
    bool(_an_lat.get("checks")) and all(c["ok"] for c in _an_lat["checks"]),
    str([(c["label"], c["ok"]) for c in _an_lat.get("checks") or []]),
)
check(
    "行和自检：唯一校验项 = 总计 = Σ行×(1+多重)",
    any(c["label"] == "行和×多重" for c in _an_lat.get("checks") or []),
)

# ---------------------------------------------------------------------------
# 卡面「容量 → 等级」文案（2026-09-20 玩家反馈「出现了个容量 0」后加）
#   库里确有 base_drain=0 的卡（survival instinct / transfusion），它们能正常
#   反推出等级；所以「读到 0 且等级反推不出」= 误读，应标 `容量0?` 而不是 `容量0`。
# ---------------------------------------------------------------------------
check(
    "★ 误读成 0 且等级反推失败 → 标 `容量0?`（不假装是合法 0）",
    lo._rank_text({"drain": 0, "rank": None, "max_rank": 5}) == "容量0?",
    lo._rank_text({"drain": 0, "rank": None, "max_rank": 5}),
)
check(
    "base_drain=0 的卡能反推出等级 → 正常显示 `容量0→…`",
    lo._rank_text({"drain": 0, "rank": 3, "max_rank": 3}) == "容量0→3/3满级",
    lo._rank_text({"drain": 0, "rank": 3, "max_rank": 3}),
)
check(
    "正常卡：容量 11 满级 5",
    lo._rank_text({"drain": 11, "rank": 5, "max_rank": 5}) == "容量11→5/5满级",
    lo._rank_text({"drain": 11, "rank": 5, "max_rank": 5}),
)
check(
    "非满级标注仍保留",
    lo._rank_text({"drain": 9, "rank": 3, "max_rank": 5}) == "容量9→3/5★非满级",
    lo._rank_text({"drain": 9, "rank": 3, "max_rank": 5}),
)
check(
    "完全没读到容量 → `容量?`",
    lo._rank_text({"drain": None, "rank": None}) == "容量?",
    lo._rank_text({"drain": None, "rank": None}),
)

# ---------------------------------------------------------------------------
# ★ 容量多重解释时用「颜色」消歧（2026-09-20 用户实测私法补给判成满级）
#   私法补给（Vigilante Supplies）base=4 / max=5，卡片读到「容量 5 红色」，
#   实际是 0 级。而容量 5 同时满足三种解释：
#     1 级白（4+1=5） / 5 级绿（ceil(9/2)=5） / 0 级红（round(4×1.25)=5）
#   旧实现「一律取最高」→ 选 5 级（满级），错。颜色能唯一定出 0 级。
# ---------------------------------------------------------------------------
VS = {"base_drain": 4, "max_rank": 5, "name": "vigilante supplies", "calculable": False}
_r, _w = lo.infer_rank(VS, 5, "红")
check(
    "私法补给红5唯一0级，不取满级",
    _r == 0 and "⚠歧义" not in _w,
    f"{_r} / {_w}",
)
check("红色唯一0级结果明确", _r == 0, _w)
_r, _w = lo.infer_rank(VS, 5, "绿")
check(
    "私法补给：绿色容量5唯一5级，不依赖满级假设",
    _r == 5 and "⚠歧义" not in _w,
    f"{_r} / {_w}",
)
_r, _w = lo.infer_rank(VS, 5, "白")
check(
    "私法补给：容量 5 + 白 → 白色唯一1级",
    _r == 1,
    f"{_r} / {_w}",
)
_r, _w = lo.infer_rank(VS, 5, None)
check(
    "★ 颜色缺失 → 多解保留unknown",
    _r is None,
    f"{_r} / {_w}",
)
_r, _w = lo.infer_rank(VS, 5, "?")
check("颜色读作 ? → 多解保留unknown", _r is None, f"{_r} / {_w}")
check(
    "颜色与任何数学分支都对不上 → 不标记歧义（没得比）",
    "⚠歧义" not in lo.infer_rank(VS, 3, "红")[1],
    lo.infer_rank(VS, 3, "红")[1],
)

# 卡面：歧义等级要带 `?`
check(
    "★ 卡面：有歧义 → `容量5→5/5满级?`",
    lo._rank_text(
        {"drain": 5, "rank": 5, "max_rank": 5, "note": "⚠歧义：按颜色（mismatch）应为 0 级"}
    )
    == "容量5→5/5满级?",
    lo._rank_text(
        {"drain": 5, "rank": 5, "max_rank": 5, "note": "⚠歧义：按颜色（mismatch）应为 0 级"}
    ),
)
check(
    "卡面：无歧义 → 不带 `?`",
    lo._rank_text({"drain": 5, "rank": 5, "max_rank": 5, "note": ""}) == "容量5→5/5满级",
)

# ---- 武器名清洗（2026-09-20 玩家报障「暮斩显示未能识别」）----
# 实测样本："+ 升级／暮斩 [30] 3" —— 全角 ／ 没被切开、尾部**极化次数 3**
# 残留 → 精确匹配失败（find_weapon("暮斩") 是好的，证明问题只在清洗环节）。
WEAPON_CASES = [
    ("+ 升级／暮斩 [30] 3", "暮斩", "★ 用户实测样本（全角 ／ + [30] + 极化次数）"),
    ("+ 升级 / 驱魔之刃 [30]", "驱魔之刃", "半角斜杠"),
    ("升级:拉特昂 [30]", "拉特昂", "冒号式"),
    ("暮斩 [30]", "暮斩", "只有方括号"),
    ("＋ 升级／暮斩【30】 3", "暮斩", "全角加号 + 中文方括号"),
    ("MK1-布莱顿", "MK1-布莱顿", "★ 名字里本来就有数字，不能被切"),
    ("MK1-布莱顿 [30] 2", "MK1-布莱顿", "带极化次数的 MK1"),
    ("", "", "空串"),
]
for _raw, _want, _why in WEAPON_CASES:
    _got = lo.clean_weapon_name(_raw)
    check(f"clean_weapon_name({_raw!r}) → {_got!r}  [{_why}]", _got == _want, f"期望 {_want!r}")

# ---- 极性分支说明（维护者要求「容量 +25% 要说清楚」）----
# 分支由 (基础容量, 等级, 卡面容量) 算术反查 —— 不采信模型读的颜色
# （实测模型常读错：北风实际红 9 报白、肢解实际绿 5 报白）。
BRANCH_CASES = [
    ({"base_drain": 4}, 5, 9, "", "无加成（4+5=9）"),
    ({"base_drain": 6}, 1, 9, "极性不合（容量 +25%）", "★ 北风实测：6+1=7 → ×1.25=8.75→9"),
    ({"base_drain": 6}, 10, 8, "槽位极性匹配（容量减半）", "★ 热病打击 Prime：16→8"),
    ({"base_drain": 4}, 3, 7, "", "剑风 4+3=7 无加成"),
    ({"base_drain": 6}, 5, 6, "槽位极性匹配（容量减半）", "一击必杀 11→6"),
    ({"base_drain": 6}, None, 9, "", "等级未知 → 不给结论"),
    ({"base_drain": -2}, 3, 10, "", "姿态卡（哨兵）→ 不给结论"),
]
for _mod, _rk, _dr, _want, _why in BRANCH_CASES:
    _got = lo.drain_branch(_mod, _rk, _dr)
    check(
        f"drain_branch(base={_mod['base_drain']}, rank={_rk}, drain={_dr}) → {_got!r}  [{_why}]",
        _got == _want,
        f"期望 {_want!r}",
    )

# ---------------------------------------------------------------------------
# 裂罅紫卡（Riven）：名字 =「武器名 + 随机词缀」，静态词典永远匹配不上。
# 方案文档 TC-04 指出这类必须单独归类 —— 否则会报成「库中未收录」，
# 让人误以为是我们缺数据。等级仍能从豆子读出来。
# ---------------------------------------------------------------------------
RIVEN_CASES = [
    ("野猪 Visi-satidex", True, "典型紫卡名（武器 + 词缀）"),
    ("奏凯 Acri-cronitis", True, "同上"),
    ("裂罅 步枪", True, "直接写了裂罅"),
    ("Riven Mod", True, "英文"),
    ("压迫点", False, "普通 mod 名"),
    ("Amalgam Argonak Metal Auger", False, "含空格的外文 mod 名，但不是紫卡"),
    ("Primed Fever Strike", False, "Prime mod"),
    ("", False, "空"),
]
for _nm, _want, _why in RIVEN_CASES:
    _got = lo.is_riven_name(_nm)
    check(f"is_riven_name({_nm!r}) → {_got}  [{_why}]", _got == _want, f"期望 {_want}")

# analyze 里紫卡要走「riven」分支，并**从豆子**取等级（库中无基准）
_riven_ocr = {
    "weapon": "野猪",
    "mods": [
        {"name": "野猪 Visi-satidex", "drain": 14},
        {"name": "分裂膛室", "drain": 15},
    ],
}
_riven_pips = [
    {
        "row": 1,
        "counts": [6, 5, 0, 0],
        "maxed": [True, True, False, False],
        "pos": [[], [], [], []],
        "is_inventory": False,
        "grid": [712, 957, 1202, 1447],
        "card_w": 152,
    }
]
_an_riven = lo.analyze(_riven_ocr, _riven_pips)
_rv = next((m for m in _an_riven["mods"] if m.get("riven")), None)
check(
    "紫卡被单独归类为 riven（不进 unknown）",
    _rv is not None and "野猪 Visi-satidex" not in (_an_riven.get("unknown") or []),
    str(_an_riven.get("unknown")),
)
check(
    "  紫卡等级由豆子给出（6 级）",
    _rv is not None and _rv.get("rank") == 6,
    str(_rv and _rv.get("rank")),
)
check(
    "  紫卡备注说明词缀随机、无法核算",
    _rv is not None and "词缀随机" in (_rv.get("note") or ""),
    str(_rv and _rv.get("note")),
)
_lines_riven = lo.card_lines(_an_riven)
check(
    "  卡面把紫卡写成「裂罅紫卡（词缀随机…）」而不是「库中未收录」",
    any("裂罅紫卡" in ln for ln in _lines_riven)
    and not any("库中未收录" in ln for ln in _lines_riven),
    str([ln for ln in _lines_riven if "紫卡" in ln or "未收录" in ln]),
)


# ---------------------------------------------------------------------------
# ★ 2026-10-05 灵化反推「锚定补全」回归（执法者实战卡）：
#   面板穿刺行行名被滚动箭头遮挡 → 旧行为整行静默丢 → 本地推算 1705 ≠ 面板
#   1807.3、且毒素行反解出幻影自带毒 29（真值 0）。修复后：
#   缺行按库内比例×中位锚补全、幻影防线收紧、灵化 +100 对账写进 note。
# ---------------------------------------------------------------------------
_weapon_magistar = {
    "name": "Magistar",
    "uniqueName": "/Lotus/Weapons/Tenno/Melee/Maces/PaladinMace/PaladinMaceWeapon",
    "damage": {"total": 209.99998, "impact": 168, "puncture": 31.5, "slash": 10.5},
    "criticalChance": 0.2,
    "criticalMultiplier": 2.0,
}
_totals_mag = {
    "base_dmg": 120.0,
    "crit_chance": 110.0,
    "crit_dmg": 140.0,
    "status_chance": 0.0,
    "elements": {"toxin": 165.0},
    "physical": {},
}
_panel_occluded = {
    "crit_chance": "75.6%",
    "crit_damage": "7 倍",
    "status_chance": "10%",
    "damage_rows": [["毒素", "1,125.3"], ["冲击", "545.6"], ["切割", "34.1"]],
    "total": ["", "1,807.3"],
}
_w_occ, _n_occ = lo._panel_base_weapon(_weapon_magistar, _panel_occluded, _totals_mag)
_d_occ = (_w_occ or {}).get("damage") or {}
check(
    "灵化锚定补全：穿刺行被遮挡 → 按库内比例补出 46.5（基础 248/46.5/15.5，计 310）",
    _d_occ.get("impact") == 248
    and _d_occ.get("puncture") == 46.5
    and _d_occ.get("slash") == 15.5
    and _d_occ.get("total") == 310,
    str(_d_occ),
)
check(
    "  不再出现幻影自带毒素（真值 0：毒 100% 来自热病打击 Prime）",
    not _d_occ.get("toxin"),
    str(_d_occ.get("toxin")),
)
check(
    "  note 说明「穿刺行未读到…已按库内比例补全」",
    "穿刺行未读到" in (_n_occ or "") and "46.5" in (_n_occ or ""),
    _n_occ,
)
check(
    "  note 对账「反推基础 310 ＝ 库内 210 ＋ 灵化进化 +100」",
    "灵化进化 +100" in (_n_occ or "") and "与 evolutions 数据吻合" in (_n_occ or ""),
    _n_occ,
)
check(
    "  面板重建逐行吻合（545.6/102.3/34.1/1125.3，总 1807.3）",
    abs(_d_occ["impact"] * 2.2 - 545.6) < 0.05
    and abs(_d_occ["puncture"] * 2.2 - 102.3) < 0.05
    and abs(_d_occ["slash"] * 2.2 - 34.1) < 0.05
    and abs(
        (_d_occ["impact"] + _d_occ["puncture"] + _d_occ["slash"] + _d_occ.get("toxin", 0))
        * 2.2
        * 1.65
        - 1125.3
    )
    < 0.3,
    str(
        [
            round(_d_occ["impact"] * 2.2, 1),
            round(_d_occ["puncture"] * 2.2, 1),
            round(_d_occ["slash"] * 2.2, 1),
        ]
    ),
)
check(
    "  行名乱猜（未归属行）同场景 → 同样补全且 note 提示未归属",
    (lambda w, n: (w or {}).get("damage", {}).get("puncture") == 46.5 and "未能归属" in (n or ""))(
        *lo._panel_base_weapon(
            _weapon_magistar,
            dict(_panel_occluded, damage_rows=_panel_occluded["damage_rows"] + [["", "102.3"]]),
            _totals_mag,
        )
    ),
)
check(
    "对照：库内真有自带元素的武器 → 反解 E0 被接受（防线不误伤）",
    lo._panel_base_weapon(
        {
            "name": "InnateToxin",
            "uniqueName": "/test/innate",
            "damage": {"total": 310, "impact": 168, "puncture": 31.5, "slash": 10.5, "toxin": 100},
            "criticalChance": 0.2,
            "criticalMultiplier": 2.0,
        },
        {"damage_rows": [["毒素", "220.0"], ["冲击", "369.6"], ["穿刺", "69.3"], ["切割", "23.1"]]},
        {
            "base_dmg": 120.0,
            "crit_chance": 0.0,
            "crit_dmg": 0.0,
            "status_chance": 0.0,
            "elements": {},
            "physical": {},
        },
    )[0]
    is None,
)

check(
    "非灵化武器部分行漏读（缩放锚=1）→ 反推与库一致 → 不覆盖（None）",
    lo._panel_base_weapon(
        {
            "name": "TestWeapon",
            "uniqueName": "/test/w",
            "damage": {"total": 210, "impact": 168, "puncture": 31.5, "slash": 10.5},
            "criticalChance": 0.2,
            "criticalMultiplier": 2.0,
        },
        {"damage_rows": [["冲击", "369.6"]], "total": ["", "460.2"]},
        _totals_mag,
    )[0]
    is None,
)
check(
    "幻影防线：库内+灵化库均无该元素 → 反解自带元素判 0（不进基础）",
    (lambda w, n: (w is None) or not (w.get("damage") or {}).get("toxin"))(
        *lo._panel_base_weapon(
            {
                "name": "NoInnate",
                "uniqueName": "/test/no_innate",
                "damage": {"total": 178.5, "impact": 168, "puncture": 0, "slash": 10.5},
                "criticalChance": 0.2,
                "criticalMultiplier": 2.0,
            },
            {"damage_rows": [["毒素", "1,125.3"], ["冲击", "369.6"], ["切割", "23.1"]]},
            _totals_mag,
        )
    ),
)

# ---------------------------------------------------------------------------
# 2026-10-09：识卡射速 MOD 进伤害 spec（修前 totals.fire_rate 从未传下去）
# 判据 = fireRate 是否取自面板「攻速」行（终值）：是 ⇒ 不叠；否 ⇒ 叠 totals.fire_rate
# ---------------------------------------------------------------------------
_lex0 = lo.analyze(
    {"weapon": "雷克斯", "mods": [{"name": "黄蜂螫刺", "drain": "4", "color": "白"}], "panel": {}}
)
_lex1 = lo.analyze(
    {
        "weapon": "雷克斯",
        "mods": [
            {"name": "乏能迅敏", "drain": "9", "color": "白"},
            {"name": "黄蜂螫刺", "drain": "4", "color": "白"},
        ],
        "panel": {},
    }
)
check("库基础路径：乏能迅敏 totals.fire_rate = 90", close(_lex1["totals"].get("fire_rate"), 90.0))
_lex0_manual = dc_mod.calculate(_manual_spec(_lex0), _lex0["weapon"])
_lex1_manual = dc_mod.calculate(_manual_spec(_lex1), _lex1["weapon"])
check(
    "库基础路径：参考射速 = 基础 ×1.9",
    close(_lex1_manual["fire_rate"], _lex0_manual["fire_rate"] * 1.9, 1e-9),
    f"{_lex0_manual['fire_rate']} → {_lex1_manual['fire_rate']}",
)
check(
    "库基础路径：+90% 射速 −15% 基伤 ⇒ DPS 上升（修前反降 32.4→27.5）",
    _lex1_manual["dps"] > _lex0_manual["dps"],
    f"{_lex0_manual['dps']:.1f} → {_lex1_manual['dps']:.1f}",
)
check(
    "面板攻速路径（执法者）：fireRate 取自面板并打标记",
    _an_inc["weapon"].get(lo.FIRE_RATE_FROM_PANEL) is True,
)
_inc_spec = _manual_spec({**_an_inc, "totals": {**_an_inc["totals"], "fire_rate": 30.0}})
check(
    "面板攻速路径：即使有射速 MOD 也不再叠加（防二次加成）", close(_inc_spec["fire_rate_pct"], 0.0)
)
check(
    "反推但无攻速行（拉特昂 Prime）：关键延迟 −20% 计入",
    _an_lat["weapon"].get(lo.FIRE_RATE_FROM_PANEL) is None
    and close(_manual_spec(_an_lat)["fire_rate_pct"], -20.0),
    str(_manual_spec(_an_lat)["fire_rate_pct"]),
)

# ---------------------------------------------------------------------------
# 2026-10-09 拉特昂 Prime 截图实测（关键延迟 −20%：「射击速度 4.17 ▸ 3.33」）
# B) 面板=终值：读到攻速行 / 没读到攻速行 两条路径必须取同一值
# C) 两列取值：模型把「基础 ▸ 终值」两列一起给时，一律取右列（修前取左列=基础值）
# ---------------------------------------------------------------------------
import copy  # noqa: E402


def _lat_with(**panel):
    o = copy.deepcopy(_ocr_lat)
    o["panel"].update(panel)
    an = _calc_fixture(o, [10, 5, 10, 5, 5, 10, 10, 5, 3])
    return an, dc_mod.calculate(_manual_spec(an), an["weapon"])


_lat_fr = {}
for _lab, _asp in (
    ("无攻速行", None),
    ("右列单值 3.33", "3.33"),
    ("两列字符串", "4.17 ▸ 3.33"),
    ("两列列表", ["4.17", "3.33"]),
):
    _a, _r = _lat_with(**({} if _asp is None else {"attack_speed": _asp}))
    _lat_fr[_lab] = _r["fire_rate"]
    check(
        f"拉特昂 Prime 截图：{_lab} ⇒ 射速 ≈ 3.33（基础 4.166667 × 0.8）",
        abs(_r["fire_rate"] - 3.3333) <= 0.005,
        f"{_r['fire_rate']} / weapon.fireRate={_a['weapon'].get('fireRate')}",
    )
check(
    "拉特昂 Prime 截图：读到面板 / 没读到面板 两条路径同值（差 ≤ 0.005）",
    max(_lat_fr.values()) - min(_lat_fr.values()) <= 0.005,
    str(_lat_fr),
)
_lat_ref = _lat_with()[0]["weapon"]
for _k, _pair, _wk in (
    ("crit_chance", "22% ▸ 138%", "criticalChance"),
    ("crit_damage", "2.8倍 ▸ 6.6倍", "criticalMultiplier"),
    ("status_chance", "26% ▸ 41.6%", "procChance"),
):
    _w = _lat_with(**{_k: _pair})[0]["weapon"]
    check(
        f"两列取右：{_k}={_pair!r} 反推基础与单值输入一致",
        close(_w[_wk], _lat_ref[_wk]),
        f"{_w[_wk]} vs {_lat_ref[_wk]}",
    )

# ---------------------------------------------------------------------------
# 2026-10-09 拉特昂 Prime 线上复报：模型读串 →「冲击 203.5 / 穿刺 25.4」对调 +
# 病毒行误抄总计 1488.3（线上卡面「行和×多重 1488.3 vs 3136.68」即
# (203.5+25.4+25.4+1488.3)×1.8 —— 由此反推的读数，原始 JSON 未留档）
# ---------------------------------------------------------------------------
_lat_bad, _lat_bad_r = _lat_with(
    damage_rows=[
        ["冲击", "72", "203.5"],
        ["穿刺", "9", "25.4"],
        ["切割", "9", "25.4"],
        ["病毒（冰+毒）", "-", "1,488.3"],
        ["总计", "90", "1,488.3"],
    ]
)
_lat_good_r = _lat_with()[1]
_bd = _lat_bad["weapon"]["damage"]
check(
    "读串纠正：冲击/穿刺对调 ⇒ 按库内构成纠回 冲击 9.6 / 穿刺 76.8",
    close(_bd["impact"], 9.6) and close(_bd["puncture"], 76.8) and close(_bd["slash"], 9.6),
    str(_bd),
)
check(
    "读串纠正：单发对血与正确读数一致（修前冲击吃 G 系弱点虚高 203 vs 184）",
    close(_lat_bad_r["health"], _lat_good_r["health"], 1e-6),
    f"{_lat_bad_r['health']} vs {_lat_good_r['health']}",
)
check(
    "读串纠正：卡面注明已纠正 + 病毒行误抄总计",
    "串行" in _lat_bad["panel_base"] and "误抄总计" in _lat_bad["panel_base"],
    _lat_bad["panel_base"],
)
check(
    "行和自检：误抄总计的元素行剔除后补本地合成元素 ⇒ 全部吻合（修前 3136.68 ⚠）",
    all(c["ok"] for c in _lat_bad["checks"]) and not any(c.get("note") for c in _lat_bad["checks"]),
    str([(c["label"], c["panel"], round(c["calc"], 2), c["ok"]) for c in _lat_bad["checks"]]),
)
check(
    "「总计」行不再记为「行名未能归属类型」",
    "未能归属" not in _lat_bad["panel_base"]
    and "未能归属" not in _calc_fixture(_ocr_lat, [10, 5, 10, 5, 5, 10, 10, 5, 3])["panel_base"],
)
check(
    "普通形态 + 灵化进化的面板不标成「灵化形态」",
    _lat_bad["panel_base"].startswith("普通形态（含灵化进化加成）"),
    _lat_bad["panel_base"][:30],
)
_lat_card2 = "\n".join(
    dc_mod.card_lines(_lat_bad["weapon"], _manual_spec(_lat_bad), _lat_bad_r, [])
)
check("面板反推的基础不再标「含多段合并」", "含多段合并" not in _lat_card2)
check(
    "不误纠：分布本来就吻合的正确读数不出「串行」",
    "串行" not in _calc_fixture(_ocr_lat, [10, 5, 10, 5, 5, 10, 10, 5, 3])["panel_base"],
)
_lat_text = "\n".join(lo.card_lines(_calc_fixture(_ocr_lat, [10, 5, 10, 5, 5, 10, 10, 5, 3])))
check("负值加成不再写成「+-」", "+-" not in _lat_text and "射速-20%" in _lat_text)
check(
    "同族近似：「镀层 氚晶猫具」（错两字）⇒ Galvanized Scope",
    (lo.match_mod("镀层 氚晶猫具")[0] or {}).get("name") == "Galvanized Scope",
)
check("同族近似：族内别的卡不被误吸", lo.match_mod("镀层 某某某某")[0] is None)


# 近战 Genesis 表只有 EVO I 能力，不能当军械库另一套基础面板直采。
_ocr_melee_weapon, _ = dc.find_weapon("Skana")
_ocr_melee_dmg = _ocr_melee_weapon["damage"]
_ocr_melee = {
    "weapon": "升级 / 空刃 [30]",
    "capacity": "4/70",
    "mods": [{"name": "压迫点", "drain": "9", "polarity": "?"}],
    "panel": {
        "crit_chance": "5%",
        "crit_damage": "1.5倍",
        "status_chance": "16%",
        # 带进化的面板与普通面板不同，应沿用已有反推路径。
        "damage_rows": [["冲击", "72.6"], ["穿刺", "72.6"], ["切割", "338.8"]],
        "total": ["总计", "484"],
    },
}
_melee_analysis = _calc_fixture(copy.deepcopy(_ocr_melee), [5])
_melee_spec = _manual_spec(_melee_analysis)
check(
    "近战灵化能力记录不进入 OCR 完整面板库（原 69 枪械保留）",
    len([k for k in lo._incarnon_forms() if k != "_meta"]) == 69
    and all(
        str(dc.find_weapon(name)[0]["uniqueName"]).lower() not in lo._incarnon_forms()
        for name in ("Skana", "Ruvox")
    ),
)
check(
    "Skana 进化面板继续反推，不把 +100% 近战能力当基础面板",
    "反推" in (_melee_analysis.get("panel_base") or "")
    and "灵化库" not in (_melee_analysis.get("panel_base") or "")
    and close(_melee_analysis["weapon"]["damage"]["total"], 220, 0.05)
    and _melee_spec["form"] == "keep"
    and close(_melee_spec["base_dmg"], 120),
    str(_melee_analysis.get("panel_base")),
)
_ocr_melee_result = dc.calculate(_melee_spec, _melee_analysis["weapon"])
_melee_plain = copy.deepcopy(_melee_spec)
_melee_plain["form"] = "base"
_melee_plain_result = dc.calculate(_melee_plain, _melee_analysis["weapon"])
check(
    "近战识卡真实复算不自动叠加灵化100%且保持原面板",
    _ocr_melee_result["ok"]
    and close(_ocr_melee_result["health"], _melee_plain_result["health"])
    and close(_ocr_melee_result["weapon_eff"]["damage"]["total"], 220, 0.05),
)

# Ruvox 原生灵化是有界转换能力，不是可供 OCR 直接套用的完整军械库面板。
_ocr_native = copy.deepcopy(_ocr_melee)
_ocr_native["weapon"] = "升级 / 古声 [30]"
_ocr_native["panel"] = {
    "crit_chance": "26%",
    "crit_damage": "2.2倍",
    "status_chance": "20%",
    "damage_rows": [["冲击", "374"]],
    "total": ["总计", "374"],
}
_native_analysis = _calc_fixture(_ocr_native, [5])
_native_ocr_spec = _manual_spec(_native_analysis)
_native_ocr_result = dc.calculate(_native_ocr_spec, _native_analysis["weapon"])
check(
    "Ruvox 识卡面板继续反推并keep，不自动激活原生灵化转换或降速",
    _native_ocr_spec["form"] == "keep"
    and "灵化库" not in (_native_analysis.get("panel_base") or "")
    and _native_ocr_result["ok"]
    and close(_native_ocr_result["weapon_eff"]["damage"].get("impact", 0), 170, 0.05)
    and close(_native_ocr_result["weapon_eff"]["damage"].get("puncture", 0), 0)
    and close(_native_ocr_result["fire_rate"], 1),
    str(_native_analysis.get("panel_base")),
)


# ---------------------------------------------------------------------------
# 识卡质量：保留可见清单，拒绝把不完整/结构矛盾的输入保存为可信伤害。
# ---------------------------------------------------------------------------
def _quality_case(panel=None, mods=None, pips=None):
    return lo.analyze(
        {
            "weapon": "驱魔之刃",
            "mods": mods if mods is not None else [{"name": "北风", "drain": 7, "color": "白"}],
            "panel": panel if panel is not None else {},
        },
        pips,
    )


for _label, _panel in (
    ("缺字段", {}),
    ("null占位", {"total": [None, "null"], "crit_damage": "null"}),
    ("null对象占位", {"total": {"value": "null", "left": None, "right": "null"}}),
    ("总计标签与左右值", {"total": ["总计", "100 > 150"]}),
    ("合法左右标量", {"crit_chance": "20% ▶ 40%", "crit_damage": "2倍 ▶ 3倍"}),
    ("千分位与伤害三列", {"damage_rows": [["冲击", "1,000", "2,000"]]}),
    ("非伤害类型备注行", {"damage_rows": [["重击", "500 x3"]]}),
):
    _q = _quality_case(_panel)
    check("质量正例：" + _label, _q["quality"]["panel_safe"] and _q["quality"]["snapshot_safe"])

for _label, _panel in (
    ("总计混入倍数", {"total": ["2倍", "3倍"]}),
    ("总计混入百分率", {"total": "30%"}),
    ("总计超过两读数", {"total": ["100", "200", "300"]}),
    ("单标量串入三数", {"status_chance": "10% > 20% > 30%"}),
    ("百分率混入倍数", {"crit_chance": "3倍"}),
    ("暴伤混入百分率", {"crit_damage": "40%"}),
    ("伤害类型混入倍数", {"damage_rows": [["冲击", "2倍"]]}),
    ("伤害类型混入百分率", {"damage_rows": [["腐蚀（电+毒）", "40%"]]}),
    ("嵌套标量", {"crit_damage": [["2", "3"]]}),
):
    _q = _quality_case(_panel)
    check(
        "质量反例：" + _label,
        not _q["quality"]["panel_safe"]
        and not _q["quality"]["snapshot_safe"]
        and not _q.get("ref")
        and not _q["panel_base"]
        and not _q["weapon"].get("_panel_derived"),
        str(_q["quality"]),
    )
    check("质量反例保留清单：" + _label, len(_q["mods"]) == 1 and _q["mods"][0]["found"])

_q_unknown = _quality_case(mods=[{"name": "无法识别的卡片", "drain": 7}])
_q_rank = _quality_case(mods=[{"name": "北风", "drain": 999}])
_q_riven = _quality_case(mods=[{"name": "裂罅 步枪", "drain": 7}])
_q_underread = _quality_case(pips=[{"counts": [1, 1], "is_inventory": False}])
for _label, _q, _code in (
    ("未知卡", _q_unknown, "unknown_mod"),
    ("等级未确定", _q_rank, "rank_unknown"),
    ("合法裂罅但词条未读取", _q_riven, "riven_uncalculated"),
    ("像素证据漏卡", _q_underread, "mods_underread"),
):
    check(
        "配卡质量拒绝：" + _label,
        not _q["quality"]["snapshot_safe"]
        and _q["quality"]["panel_safe"]
        and any(r["code"] == _code for r in _q["quality"]["reasons"])
        and not _q.get("ref"),
    )
    _lines = lo.card_lines(_q)
    check(
        "拒绝后仍展示卡片与明确状态：" + _label,
        bool(_q["mods"]) and any("本次不生成识卡伤害记录" in line for line in _lines),
    )
check("合法裂罅不误标未知", not _q_riven["unknown"] and _q_riven["mods"][0]["riven"])
check("缺等级仍明确原有满级估算", bool(_q_rank["totals"]["no_rank"]))
_q_conflict = _quality_case(
    mods=[{"name": "北风", "drain": 6, "color": "白"}],
    pips=[{"counts": [0], "maxed": [False], "is_inventory": False}],
)
check(
    "无锚定全零行不证明豆子修正，白色唯一0仍可用",
    _q_conflict["pips"]["conflict"] == 0
    and _q_conflict["mods"][0]["rank"] == 0
    and _q_conflict["quality"]["snapshot_safe"],
)
_q_inventory = _quality_case(pips=[{"counts": [1, 1], "is_inventory": True}])
check("库存豆子不算已装漏卡", _q_inventory["quality"]["snapshot_safe"])
_q_static_difference = _quality_case({"crit_damage": "2倍", "total": "100000"})
check(
    "一般静态公式差异不直接封禁",
    any(not c["ok"] for c in _q_static_difference["checks"])
    and _q_static_difference["quality"]["snapshot_safe"],
)
check(
    "合法灵化面板可作派生诊断，未知布局不生成参考",
    _an_inc["quality"]["snapshot_safe"]
    and bool(_an_inc["panel_base"])
    and not _an_inc.get("ref")
    and not _an_inc["quality"]["calculation_allowed"],
)
check(
    "条件加成说明不直接封禁",
    _an_lat["quality"]["snapshot_safe"] and any("条件堆叠" in n for n in _an_lat["notes"]),
)
check(
    "数值吻合不称图像全通过",
    not any("全部吻合" in line for line in lo.card_lines(_an))
    and any("不代表图像识别完整无误" in line for line in lo.card_lines(_an)),
)


# 坐标 OCR：真实返回、缺失字段、库存与相邻行反例。
_csv_2_mods = """71,74,83,64,90,30]
230,72,65,25,90,4
206,780,27,13,90,8
206,560,27,13,90,9
120,616,47,59,90,压迫点
120,838,45,133,90,热病打击 Prime
314,838,45,79,90,一击必杀
314,972,45,39,90,所有
314,616,47,81,90,镀层 钣铁
314,392,45,79,90,云暴山碎
380,338,27,25,90,10
314,184,47,59,90,配置A
408,184,47,59,90,配置B
500,184,47,59,90,配置C
748,330,35,29,90,连击
774,780,27,13,90,9
704,616,47,39,90,震波
704,838,45,101,90,角斗士 威猛
508,838,45,123,90,镀层 增幅线圈
508,616,47,79,90,急进猛突
508,780,27,13,90,12
582,780,27,25,90,12
582,560,27,13,90,7
386,560,27,13,90,6
386,780,27,13,90,6
774,560,27,13,90,9
894,420,35,73,90,近战·暴露
"""
_csv_2_panel = """578,74,97,667,90,升级/执法者
116,174,47,105,90,容量
844,176,47,113,90,4/70
108,254,37,79,90,主要
144,292,35,153,90,攻击速度
108,328,35,79,90,范围
144,364,35,153,90,震地攻击
144,400,35,153,90,滑行攻击
178,436,35,229,90,连击持续时间
180,472,35,231,90,伤害穿透系数
162,508,35,191,90,裂罅倾向性
108,580,37,79,90,伤害
144,616,35,153,90,暴击几率
144,652,35,153,90,暴击伤害
144,690,35,153,90,触发几率
144,726,35,79,90,毒素
144,762,35,79,90,冲击
144,798,35,79,90,切割
852,292,35,105,90,0.833
852,328,35,105,90,2.5米
834,364,35,145,90,9,094.8
834,400,35,145,90,3,031.6
872,436,35,65,90,5秒
742,472,35,85,90,40%
860,472,35,85,90,80%
844,616,35,119,90,75.6%
728,652,35,79,90,7倍
848,652,35,105,90,4.8倍
860,690,35,85,90,10%
834,726,35,145,90,1,125.3
852,762,35,105,90,545.6
860,798,35,85,90,34.1
852,834,35,105,90,102.3
834,872,35,145,90,1,807.3"""
_csv_3_panel = """122,146,37,101,90,容量
652,146,43,111,90,4/60
118,216,33,81,90,噪音
158,252,33,153,90,多重射击
158,286,33,153,90,射击速度
122,320,33,79,90,弹匣
160,354,33,153,90,弹药拾取
180,388,33,193,90,弹药最大值
124,422,33,81,90,扳机
144,456,33,115,90,精准度
182,490,33,191,90,裂罅倾向性
128,524,33,79,90,装填
640,218,33,153,90,引起警戒
688,252,27,59,90,1.8
618,286,33,193,90,4.17>3.33
652,320,33,111,90,15>30
696,354,27,51,90,80
686,388,27,71,90,540
662,422,33,115,90,半自动
680,456,27,85,90,28.6
646,524,33,153,90,2.4>1.8
934,428,45,105,90,镀层
130,582,33,79,90,伤害
168,616,33,153,90,暴击倍率
168,650,33,153,90,暴击几率
168,684,33,153,90,触发几率
180,718,33,79,90,冲击
180,752,33,79,90,穿刺
180,786,33,79,90,切割
182,819,32,79,90,病毒
138,852,33,79,90,总计
158,922,33,115,90,搜寻…
532,616,33,103,90,2.8倍
542,650,33,85,90,22%
532,684,33,85,90,26%
668,616,33,103,90,6.6倍
670,650,33,103,90,138%
668,684,33,103,90,41.6%
670,718,33,103,90,25.4
566,752,33,51,90,72
670,752,33,103,90,203.5
598,786,33,29,90,9
688,786,33,85,90,25.4
676,819,32,103,90,572.4
532,852,33,51,90,90
658,852,33,139,90,1,488.3
938,650,45,105,90,镀层"""
_csv_2_whole = """714,78,19,11,90
836,76,37,65,90
908,76,37,33,90
970,76,37,51,90
32,174,47,31,90
236,174,47,31,90
32,254,31,23,90
39,288,31,46,90
30,326,31,23,90
39,364,31,46,90
39,398,31,46,90
50,434,31,65,90
50,470,31,65,90
46,508,31,51,90
29,578,31,24,90
40,614,31,45,90
40,652,31,45,90
40,688,31,45,90
39,726,31,24,90
39,762,31,24,90
39,798,31,24,90
29,944,31,20,90
188,76,91,261,90
446,70,69,21,90
506,184,47,43,90
574,184,47,43,90
642,184,47,43,90
818,332,31,21,90
924,418,35,51,90
506,394,41,53,90
554,338,27,19,90
558,556,27,13,90
558,780,27,13,90
506,616,41,63,90
506,838,41,59,90
506,968,41,27,90
366,616,41,45,90
366,838,41,99,90
416,556,27,13,90
416,780,27,13,90
700,556,27,13,90
788,616,41,31,90
788,838,41,75,90
788,780,69,31,90
788,556,27,13,90
836,556,27,13,90
836,780,27,13,90
648,616,41,53,90
648,838,41,89,90
694,780,27,13,90
236,288,31,27,90
238,326,31,29,90
232,364,31,43,90
232,398,31,43,90
244,434,31,21,90
226,470,31,53,90
236,616,31,33,90
224,652,31,63,90
240,688,31,23,90
232,726,31,43,90
236,762,31,33,90
240,798,31,23,90
238,834,31,33,90
232,870,31,43,90"""

_coord_mods = lo.parse_coordinate_ocr(_csv_2_mods)
check("坐标OCR实际MOD裁剪含姿态九卡", len(_coord_mods["mods"]) == 9)
check(
    "坐标OCR耗能来自各自卡片",
    [m["drain"] for m in _coord_mods["mods"]] == [10, 9, 6, 7, 9, 8, 6, 12, 9],
)
check(
    "坐标OCR赋能保留名称与定位但不猜等级",
    _coord_mods["arcanes"][0]["name"] == "近战·暴露"
    and _coord_mods["arcanes"][0]["_geometry_source"] == "coordinate_ocr"
    and "rank" not in _coord_mods["arcanes"][0],
)
_coord_panel = lo.parse_coordinate_ocr(_csv_2_panel)
check("坐标OCR标题容量", _coord_panel["weapon"] == "执法者" and _coord_panel["capacity"] == "4/70")
check("坐标OCR左右列单位保留", _coord_panel["panel"]["crit_damage"] == "7倍>4.8倍")
check(
    "坐标OCR缺标签不猜穿刺总计",
    "total" not in _coord_panel["panel"]
    and not any(r[0] == "穿刺" for r in _coord_panel["panel"]["damage_rows"]),
)
_coord_lat = lo.parse_coordinate_ocr(_csv_3_panel)
check("坐标OCR暴击倍率别名", _coord_lat["panel"]["crit_damage"] == "2.8倍>6.6倍")
check("坐标OCR千分位仍在第六字段", _coord_lat["panel"]["total"] == ["总计", "90>1,488.3"])
check("坐标OCR镀层碎片不成为卡片", not _coord_lat["mods"])
check("坐标OCR实际whole缺文字字段拒绝", lo.parse_coordinate_ocr(_csv_2_whole) is None)
check("坐标OCR普通JSON不误识别", lo.parse_coordinate_ocr('{"weapon":"执法者"}') is None)
_csv_layout = """180,65,38,240,90,升级/测试武器 [30]
35,120,20,40,90,容量
235,120,20,45,90,3/60
40,220,18,60,90,暴击几率
220,220,18,40,90,25%
40,250,18,60,90,触发几率
220,280,18,40,90,35%
40,310,18,40,90,总计
220,310,18,60,90,1,234.5
500,120,20,55,90,配置A
400,360,24,70,90,压迫点
580,358,24,70,90,北风
460,322,18,15,90,9
640,320,18,15,90,6
400,480,24,70,90,一击必杀
720,435,24,90,90,私法 补给
780,397,18,15,90,5
880,260,20,90,90,主要·无情
500,620,22,50,90,所有
80,620,22,70,90,搜索…
400,720,24,70,90,压迫点
460,682,18,15,90,9"""
_coord_whole = lo.parse_coordinate_ocr(_csv_layout)
check(
    "坐标OCR整图布局同排按x并含特殊槽",
    [m["name"] for m in _coord_whole["mods"]] == ["压迫点", "北风", "私法 补给", "一击必杀"],
)
check("坐标OCR缺本卡耗能不借邻卡", [m["drain"] for m in _coord_whole["mods"]] == [9, 6, 5, None])
check("坐标OCR不借面板下一行数字", "status_chance" not in _coord_whole["panel"])
check(
    "坐标OCR裁图没有库存分界仍保留可见卡片候选",
    bool(
        lo.parse_coordinate_ocr(
            _csv_layout.replace("所有", "未知标题").replace("搜索…", "未知搜索")
        )["mods"]
    ),
)
check(
    "坐标OCR两个容量候选留空",
    lo.parse_coordinate_ocr(_csv_layout + "\n450,323,18,15,90,8")["mods"][0]["drain"] is None,
)
check(
    "坐标OCR越界非有限拒绝",
    lo.parse_coordinate_ocr(
        "nan,100,20,20,90,压迫点\n100,100,20,20,nan,压迫点\n1001,100,20,20,90,压迫点"
    )
    is None,
)

# 分区协议不能把模型照提示返回的逐行文字或字典标量静默丢掉。
_plain_mods = lo.parse_focus_text("北风 | 7\n未知的零级卡 | 4\n近战·暴露", "mods")
check("补读逐行卡名保留未知项", len(_plain_mods["mods"]) == 2)
check("补读逐行赋能保留名称", _plain_mods["arcanes"] == [{"name": "近战·暴露"}])
_plain_panel = lo.parse_focus_text(
    "暴击几率 20% ▶ 40%\n暴击伤害：2倍 ▶ 3倍\n总计 | 1,000 > 2,000", "panel"
)
check("补读逐行面板保留标量与单位", _plain_panel["panel"]["crit_damage"] == "2倍 ▶ 3倍")
_json_panel = lo.parse_focus_text(
    '{"rows":[{"行名":"暴击几率","数值":"20% > 40%"},{"行名":"总计","数值":"100 > 200"}]}', "panel"
)
check("字典补读标量归正确字段", _json_panel["panel"]["crit_chance"] == "20% > 40%")
_unconfirmed = lo.analyze(
    {
        "weapon": "驱魔之刃",
        "mods": [{"name": "北风", "drain": 7, "color": "白"}],
        "_equipment_unconfirmed": True,
    }
)
check("装备区未确认不保存可信记录", not _unconfirmed["quality"]["snapshot_safe"])
_arcane = lo.analyze(
    {
        "weapon": "驱魔之刃",
        "mods": [{"name": "北风", "drain": 7, "color": "白"}],
        "arcanes": [{"name": "近战·暴露"}],
    }
)
check(
    "赋能经过分析到展示且明确未折算",
    any("近战·暴露" in line and "未计入" in line for line in lo.card_lines(_arcane)),
)
_unknown_csv = _csv_2_mods.replace("压迫点", "未收录的卡")
_unknown_parsed = lo.parse_coordinate_ocr(_unknown_csv)
check(
    "坐标已装行未知卡不被词库过滤", any(m["name"] == "未收录的卡" for m in _unknown_parsed["mods"])
)

_overread = _quality_case(mods=[{"name": "北风", "drain": 7, "color": "白"}] * 11)
check(
    "超槽位卡数禁止混入仓库后生成伤害",
    not _overread["quality"]["snapshot_safe"]
    and any(r["code"] == "mods_overread" for r in _overread["quality"]["reasons"]),
)

# 合并补读保留原卡顺序、版本与耗能；只用共同卡证明来自同一装备区。
_merge_original = [
    {"name": "北风", "drain": 6, "color": "白"},
    {"name": "镀层 氩晶瞄具", "drain": 6},
    {"name": "压迫点", "drain": 9},
]
_merge_focused = [
    {"name": "氩晶瞄具", "drain": 9},
    {"name": "北风", "drain": 99},
    {"name": "云暴山碎", "drain": 10},
    {"name": "未收录的新卡", "drain": 4},
]
_merged = lo.merge_focus_mods(_merge_original, _merge_focused)
check(
    "合并保留原顺序与变体且新增姿态前置",
    [m["name"] for m in _merged] == ["云暴山碎", "北风", "镀层 氩晶瞄具", "压迫点", "未收录的新卡"],
)
check("合并不覆盖原有耗能", [m["drain"] for m in _merged] == [10, 6, 6, 9, 4])
check(
    "合并不原地修改原清单",
    _merge_original[0] == {"name": "北风", "drain": 6, "color": "白"} and len(_merge_original) == 3,
)
check(
    "合并新增未知卡继续受质量门约束",
    any(r["code"] == "unknown_mod" for r in _quality_case(mods=_merged)["quality"]["reasons"]),
)
check(
    "少于半数共同卡拒绝合并",
    lo.merge_focus_mods(_merge_original, [{"name": "北风", "drain": 6}]) is None,
)
check(
    "重复同一共同卡不能凑足半数",
    lo.merge_focus_mods(_merge_original, [{"name": "北风", "drain": 6}] * 3) is None,
)
check(
    "无共同卡拒绝覆盖",
    lo.merge_focus_mods(_merge_original, [{"name": "一击必杀", "drain": 6}]) is None,
)
_variant_original = [{"name": "镀层 氩晶瞄具", "drain": 6}, {"name": "氩晶瞄具", "drain": 9}]
_variant_merged = lo.merge_focus_mods(_variant_original, [{"name": "氩晶瞄具", "drain": 4}])
check(
    "同名普通版本优先精确匹配不替代另一变体",
    _variant_merged == _variant_original,
)
check(
    "原含未知卡允许足长完整替换",
    lo.merge_focus_mods([{"name": "未识别旧卡"}], [{"name": "北风", "drain": 6}])
    == [{"name": "北风", "drain": 6}],
)
check(
    "原含未知卡仍拒绝更短替换",
    lo.merge_focus_mods([{"name": "未识别旧卡"}, {"name": "北风"}], [{"name": "北风"}]) is None,
)
check("原清单为空允许补读恢复", lo.merge_focus_mods([], [{"name": "北风"}]) == [{"name": "北风"}])
check("空补读不抹除原清单", lo.merge_focus_mods(_merge_original, []) is None)
_metadata_merged = lo.merge_focus_mods(
    [{"name": "私法 补给", "drain": 5}],
    [{"name": "私法 补给", "drain": 9, "_slot": "auxiliary", "_geometry": {"x": 900}}],
)
check(
    "合并携带辅助槽几何且保留耗能",
    _metadata_merged[0]["_slot"] == "auxiliary"
    and _metadata_merged[0]["_geometry"] == {"x": 900}
    and _metadata_merged[0]["drain"] == 5,
)

# 两排普通网格以外、最右列半个列距之外的卡才标辅助槽。
_aux_grid_lines = ["500,950,20,40,90,所有"]
_aux_names = [
    "压迫点",
    "北风",
    "一击必杀",
    "震波",
    "热病打击 Prime",
    "急进猛突",
    "角斗士 威猛",
    "镀层 增幅线圈",
]
for _i, _name in enumerate(_aux_names):
    _x, _y = 250 + (_i % 4) * 150, 450 + (_i // 4) * 250
    _aux_grid_lines.append(f"{_x},{_y},24,70,90,{_name}")
_aux_grid_csv = "\n".join(_aux_grid_lines)
_aux_geometry = lo.parse_coordinate_ocr(_aux_grid_csv + "\n900,300,24,70,90,私法 补给")
_aux_item = next(m for m in _aux_geometry["mods"] if m["name"] == "私法 补给")
check(
    "坐标辅助槽只照抄几何",
    _aux_item.get("_slot") == "auxiliary"
    and _aux_item.get("_geometry") == {"x": 900.0, "y": 300.0, "h": 24.0, "w": 70.0},
)
check(
    "普通网格卡不标辅助槽",
    all(not m.get("_slot") for m in _aux_geometry["mods"] if m["name"] != "私法 补给"),
)
for _label, _extra in (
    ("最右列半距以内", "730,300,24,70,90,私法 补给"),
    ("与普通网格同排", "900,450,24,70,90,私法 补给"),
):
    _aux_negative = lo.parse_coordinate_ocr(_aux_grid_csv + "\n" + _extra)
    check("坐标不误标辅助槽：" + _label, not any(m.get("_slot") for m in _aux_negative["mods"]))
_one_grid = "\n".join(_aux_grid_lines[:5]) + "\n900,300,24,70,90,私法 补给"
check(
    "只有一排网格不推断辅助槽",
    not any(m.get("_slot") for m in lo.parse_coordinate_ocr(_one_grid)["mods"]),
)

_eight_mods = [{"name": "北风", "drain": 6, "color": "白"} for _ in range(8)]
_eight_pips = [{"counts": [0, 0, 0, 0], "is_inventory": False} for _ in range(2)]
_eight_unanchored = _quality_case(mods=_eight_mods, pips=_eight_pips)
check("无锚点整排零不虚称像素证据", _eight_unanchored["pips"]["used"] == 0)
_eight_an = _calc_fixture({"weapon": "驱魔之刃", "mods": _eight_mods, "panel": {}}, [0] * 8)
_with_aux = _calc_fixture(
    {
        "weapon": "驱魔之刃",
        "mods": _eight_mods[:3]
        + [{"name": "私法 补给", "drain": 5, "_slot": "auxiliary"}]
        + _eight_mods[3:],
        "panel": {},
    },
    [0] * 3 + [None] + [0] * 5,
)
check(
    "辅助槽插入中途不挤坏八张普通卡豆子映射",
    _eight_an["pips"]["used"] == _with_aux["pips"]["used"] == 8
    and [m["rank"] for i, m in enumerate(_with_aux["mods"]) if i != 3]
    == [m["rank"] for m in _eight_an["mods"]],
)
check(
    "辅助功能卡多解等级不虚称满级但不一律禁伤害",
    _with_aux["mods"][3]["rank"] is None
    and "等级未确定" in _with_aux["mods"][3]["note"]
    and _with_aux["quality"]["snapshot_safe"],
)
check(
    "未折算功能卡不宣称不影响伤害",
    any("数值未折算" in line for line in lo.card_lines(_with_aux))
    and not any("不影响伤害" in line for line in lo.card_lines(_with_aux)),
)
_calc_aux = _quality_case(mods=[{"name": "北风", "drain": 6, "_slot": "auxiliary"}])
check(
    "辅助可计算卡多解仍禁止可信记录",
    _calc_aux["mods"][0]["rank"] is None and not _calc_aux["quality"]["snapshot_safe"],
)
_unique_aux = _quality_case(mods=[{"name": "北风", "drain": 3, "_slot": "auxiliary"}])
check("辅助槽容量唯一解仍保留等级", _unique_aux["mods"][0]["rank"] == 0)

check(
    "独立赋能逐行补读仅保留完整名称",
    lo.parse_focus_text("赋能如下\n近战·暴露\n等级未知\n主要・无情", "arcanes")
    == {"arcanes": [{"name": "近战·暴露"}, {"name": "主要·无情"}]},
)
check(
    "独立赋能补读拒绝说明与等级",
    lo.parse_focus_text("未能确认赋能\n近战·\n主要·无情 等级5", "arcanes") is None,
)
check(
    "独立赋能接受坐标协议",
    {
        k: lo.parse_focus_text("900,300,24,70,90,近战·暴露", "arcanes")["arcanes"][0]["_geometry"][
            k
        ]
        for k in ("x", "y", "h", "w")
    }
    == {"x": 900.0, "y": 300.0, "h": 24.0, "w": 70.0},
)

_full_gladiator = _quality_case(mods=[{"name": "角斗士 威猛", "drain": 9, "color": "白"}])
_short_gladiator = _quality_case(mods=[{"name": "角斗士 猛", "drain": 9}])
check("完整角斗士威猛仍可生成可信记录", _full_gladiator["quality"]["snapshot_safe"])
check(
    "截断角斗士名称保留匹配兼容但禁止可信记录",
    lo.match_mod("角斗士 猛")[1] == "子串"
    and _short_gladiator["mods"][0]["found"]
    and not _short_gladiator["quality"]["snapshot_safe"]
    and any(r["code"] == "mod_name_incomplete" for r in _short_gladiator["quality"]["reasons"])
    and not _short_gladiator.get("ref"),
)
check(
    "截断名称展示原文并说明候选未确认",
    any(line.startswith("· 角斗士 猛　") for line in lo.card_lines(_short_gladiator))
    and "候选" in _short_gladiator["mods"][0]["note"]
    and "尚未确认" in _short_gladiator["mods"][0]["note"],
)
for _raw_name, _drain, _canonical in (
    ("牺牲钿铁", 13, "Sacrificial Steel"),
    ("镀层钣铁", 6, "Galvanized Steel"),
):
    _near_name = _quality_case(
        mods=[{"name": _raw_name, "drain": _drain, "color": "白" if _drain == 13 else "红"}]
    )
    check(
        "既有错字近似不被截断门误封：" + _raw_name,
        _near_name["mods"][0].get("name") == _canonical
        and _near_name["quality"]["snapshot_safe"]
        and not any(r["code"] == "mod_name_incomplete" for r in _near_name["quality"]["reasons"]),
    )

# 原生OCR协议要求文字与位置同时返回；不在提示词中塞入卡名或卡面数值。
check(
    "主提示词要求原文与归一坐标同时返回",
    "cx,cy,width,height,angle,text" in lo.VISION_PROMPT
    and "0-1000" in lo.VISION_PROMPT
    and "recognized text after the coordinates" in lo.VISION_PROMPT,
)
_no_inventory = lo.parse_coordinate_ocr(
    _csv_layout.replace("所有", "未知标题").replace("搜索…", "未知搜索")
)
check(
    "可见卡片候选不等于装备布局已确认",
    not _no_inventory["coordinate_ocr"]["equipment_boundary_confirmed"]
    and not lo.analyze(_no_inventory)["layout"]["layout_complete"],
)

# 这些用例走真实解析器→analyze，不把“同一行左右不同”误当重复指标冲突。
_conflict_coord = """100,250,20,60,90,暴击伤害
250,250,20,60,90,2倍
100,300,20,60,90,暴击倍率
250,300,20,60,90,3倍"""
_conflict_inputs = (
    ("JSON重复标量键", lo.parse_vision_json('{"panel":{"crit_damage":"2倍","crit_damage":"3倍"}}')),
    ("JSON重复面板对象", lo.parse_vision_json('{"panel":{"total":"200"},"panel":{"total":"300"}}')),
    (
        "JSON总计字段与伤害行",
        lo.parse_vision_json(
            '{"panel":{"total":"300","damage_rows":[["总伤","200"],["总计","300"]]}}'
        ),
    ),
    (
        "JSON重复伤害行键",
        lo.parse_vision_json(
            '{"panel":{"damage_rows":[["冲击","200"]],"damage_rows":[["冲击","300"]]}}'
        ),
    ),
    ("坐标重复别名", lo.parse_focus_text(_conflict_coord, "panel")),
    ("逐行重复别名", lo.parse_focus_text("暴击伤害 2倍\n暴击倍率 3倍", "panel")),
    (
        "JSON行表重复别名",
        lo.parse_focus_text('{"rows":[["暴击伤害","2倍"],["暴击倍率","3倍"]]}', "panel"),
    ),
    ("同终值但左列冲突", lo.parse_focus_text("暴击伤害 2倍 → 4倍\n暴击倍率 3倍 → 4倍", "panel")),
    # 原始短档 short-85ba1432a3de-2.json text 35/36行的最小摘录；不依赖私有输出目录。
    ("实际自然文总伤总计同层冲突", lo.parse_focus_text("- 总伤：102.3\n- 总计：1,807.3", "panel")),
)
for _label, _parsed in _conflict_inputs:
    _conflict_an = _quality_case(panel=_parsed["panel"])
    check(
        "同指标冲突禁止可信记录：" + _label,
        not _conflict_an["quality"]["panel_safe"]
        and not _conflict_an["quality"]["snapshot_safe"]
        and any(r["code"] == "panel_conflict" for r in _conflict_an["quality"]["reasons"])
        and not _conflict_an.get("ref")
        and not _conflict_an["panel_base"]
        and len(_conflict_an["mods"]) == 1,
        str(_conflict_an["quality"]),
    )

_same_inputs = (
    (
        "相同JSON重复键",
        lo.parse_vision_json('{"panel":{"crit_damage":"2倍","crit_damage":"2.0倍"}}'),
    ),
    (
        "相同JSON重复面板",
        lo.parse_vision_json('{"panel":{"total":"200"},"panel":{"total":"200.0"}}'),
    ),
    ("合法箭头与单终值", lo.parse_focus_text("暴击伤害 2倍 → 4倍\n暴击倍率 4倍", "panel")),
    ("相同左右值重复", lo.parse_focus_text("暴击伤害 2倍 → 4倍\n暴击倍率 2倍 → 4倍", "panel")),
    (
        "总计双存与千分位",
        lo.parse_vision_json(
            '{"panel":{"total":["总计","500 > 1,000"],"damage_rows":[["总伤害","1000.0"]]}}'
        ),
    ),
    ("坐标相同别名", lo.parse_focus_text(_conflict_coord.replace("3倍", "2.0倍"), "panel")),
    ("不同指标同值不合并", lo.parse_focus_text("暴击伤害 2倍\n暴击几率 2%", "panel")),
    (
        "左右空占位不交叉比较",
        lo.parse_vision_json(
            '{"panel":{"total":["200",null],"damage_rows":[["总计",null,"300"]]}}'
        ),
    ),
    (
        "JSON行表左右空占位仍保留列",
        lo.parse_focus_text('{"rows":[["总伤","200",null],["总计",null,"300"]]}', "panel"),
    ),
)
for _label, _parsed in _same_inputs:
    _same_an = _quality_case(panel=_parsed["panel"])
    check(
        "同指标合法证据仍可用：" + _label,
        _same_an["quality"]["panel_safe"] and _same_an["quality"]["snapshot_safe"],
        str(_same_an["quality"]),
    )

_overwritten_unit = lo.parse_focus_text("暴击伤害 2%\n暴击倍率 2倍", "panel")
check(
    "被覆盖旧行的量纲错误不会消失",
    not _quality_case(panel=_overwritten_unit["panel"])["quality"]["panel_safe"],
)
_single_rows = {"damage_rows": [["冲击", "100"], ["穿刺", "200"], ["火焰", "50"], ["总计", "350"]]}
_repeat_rows = {
    "damage_rows": _single_rows["damage_rows"] + [["冲击", "100.0"], ["火焰", "50.0"]],
}
_single_result = _quality_case(panel=_single_rows)
_repeat_result = _quality_case(panel=_repeat_rows)
check(
    "相同伤害行不重复叠算且保留不同类型",
    _repeat_result["quality"]["snapshot_safe"]
    and len(_repeat_result["panel"]["damage_rows"]) == len(_single_rows["damage_rows"])
    and _repeat_result["calc_panel"] == _single_result["calc_panel"]
    and _repeat_result.get("ref") == _single_result.get("ref"),
)


# 来源分层：保留读图入口，不把代码纠正和同面板反推归作模型独立读准。
_prov_input = copy.deepcopy(OCR)
_prov_input["panel"]["crit_chance"] = "20% > 44%"
_prov_input["_recognition"] = {
    "source": "model",
    "raw_text": "原始模型响应",
    "parsed": copy.deepcopy(OCR),
}
_prov = lo.analyze(_prov_input)
check(
    "来源保留归一前面板与模型原文",
    _prov["provenance"]["input"]["panel"]["crit_chance"] == "20% > 44%"
    and _prov["panel"]["crit_chance"] == 44
    and _prov["provenance"]["recognition"]["raw_text"] == "原始模型响应",
)
_prov_input["panel"]["crit_chance"] = "999%"
_prov_input["_recognition"]["raw_text"] = "外部改动"
check(
    "来源证据不随调用方字典后改而变化",
    _prov["provenance"]["input"]["panel"]["crit_chance"] == "20% > 44%"
    and _prov["provenance"]["recognition"]["raw_text"] == "原始模型响应",
)
for _query, _method in (
    ("驱魔之刃", "exact"),
    ("绝路p", "variant_equivalent"),
    ("驱魔", "substring"),
):
    _match_an = lo.analyze({"weapon": _query, "mods": [{"name": "北风", "drain": 7}]})
    check("武器来源方法：" + _method, _match_an["provenance"]["weapon_match"]["method"] == _method)
    if _method == "substring":
        check(
            "武器子串匹配在卡面提示核对",
            any("代码子串匹配" in line for line in lo.card_lines(_match_an)),
        )

_near_an = _quality_case(mods=[{"name": "牺牲钿铁", "drain": 13, "color": "白"}])
_near_item = _near_an["mods"][0]
check(
    "近似MOD保留原文与代码候选且沿用快照兼容",
    _near_item["provenance"]["name"]
    == {
        "raw": "牺牲钿铁",
        "canonical": "Sacrificial Steel",
        "method": "近似",
        "source": "code_match",
    }
    and _near_an["quality"]["snapshot_safe"]
    and "牺牲钿铁" in _near_item["note"]
    and "请核对原图" in _near_item["note"],
)
check(
    "MOD等级来源标为容量推断而非模型等级",
    _near_item["provenance"]["rank"]["source"] == "capacity_inference"
    and _near_item["provenance"]["rank"]["value"] == _near_item["rank"],
)
check(
    "普通库对照保留既有数值但不认证图像准确率",
    _an["provenance"]["checks"]["basis"] == "database_comparison"
    and all(
        c["basis"] == "database_comparison" and not c["image_accuracy_verified"]
        for c in _an["checks"]
    ),
)
check(
    "反推后checks只标自洽并保留来源",
    _an_inc["provenance"]["weapon_base"]["source"] == "panel_reconstruction"
    and all(
        c["basis"] == "panel_reconstruction_consistency" and not c["image_accuracy_verified"]
        for c in _an_inc["checks"]
    )
    and any("仅证自洽" in line for line in lo.card_lines(_an_inc)),
)
check(
    "灵化库直采与面板反推分开标注",
    _an_g1["provenance"]["weapon_base"]["source"] == "incarnon_database"
    and _an_g1["provenance"]["weapon_base"]["selected_from_panel"]
    and _an_g1["provenance"]["checks"]["basis"] == "database_comparison",
)

# 真实识卡分析入口，Provider 仅返回固定文本，不连接云渠道。
import asyncio  # noqa: E402
import hashlib  # noqa: E402
import json  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from core.commands.scan import ScanCommands  # noqa: E402
from core.commands.vision import VisionCommands  # noqa: E402


class _ScanTestHost(ScanCommands, VisionCommands):
    """与真实宿主相同地组合识卡入口和渠道配置设施。"""

    pass


async def _provenance_scan_entry():
    main_parsed = {"weapon": "驱魔之刃", "mods": [{"name": "北风", "drain": 7}], "panel": {}}
    main_raw = " \n" + json.dumps(main_parsed, ensure_ascii=False) + "\n "

    class Provider:
        def meta(self):
            return SimpleNamespace(id="offline-provenance")

        async def text_chat(self, *, prompt, session_id, image_urls):
            if session_id.startswith("sdjk-loadout-"):
                text = main_raw
            elif prompt == ScanCommands._FOCUS_PROMPTS["mods"]:
                text = "北风 | 7"
            elif prompt == ScanCommands._FOCUS_PROMPTS["panel"]:
                text = "暴击几率 20%\n暴击伤害 2倍\n触发几率 18%"
            else:
                text = "近战·暴露"
            return SimpleNamespace(completion_text=text)

    host = _ScanTestHost()
    host.cfg = {}
    host.RACE_WINDOW_S = 0.5
    host._scan_vision_providers = lambda: [Provider()]
    host._fit_scan_image = lambda image: image
    host._scan_focus_image = lambda image, part: image

    async def no_pips(image):
        return []

    host._detect_pips = no_pips
    final = await host._extract_loadout_validated("offline-image")
    record = final["_recognition"]
    check(
        "主读入口保存未strip原文、解析快照及真实提示词hash",
        record["raw_text"] == main_raw
        and record["parsed"] == main_parsed
        and record["provider_id"] == "offline-provenance"
        and record["prompt_hash"] == hashlib.sha256(lo.VISION_PROMPT.encode()).hexdigest(),
    )
    check(
        "分区采纳保留主读并记录每个补读原文与字段",
        {r["task"] for r in record["focused_reads"]} == {"scan_mods", "scan_panel", "scan_arcanes"}
        and all(r["raw_text"] and r["prompt_hash"] for r in record["focused_reads"])
        and {field for r in record["revisions"] for field in r["fields"]}
        == {"mods", "panel", "arcanes"},
        repr(record),
    )
    analysis = lo.analyze(final)
    check(
        "真实分析入口沿用补读证据且不改ok和快照门槛",
        analysis["ok"]
        and analysis["quality"]["snapshot_safe"]
        and analysis["provenance"]["recognition"] == record
        and analysis["provenance"]["input"]["panel"]["crit_chance"] == "20%",
    )


asyncio.run(_provenance_scan_entry())


# 布局闸门：字段自洽不代表完整截图；模型位置不能充当独立图像证据。
_layout_model = lo.analyze(
    {
        "weapon": "驱魔之刃",
        "mods": [{"name": "北风", "drain": 7, "color": "白", "row": 1, "col": 1}],
    }
)
check("模型行列不证明布局完整", not _layout_model["quality"]["layout_complete"])
check(
    "读到内容与允许计算分开",
    _layout_model["quality"]["content_read"]
    and not _layout_model["quality"]["calculation_allowed"],
)
check(
    "未知布局不产参考伤害和计算spec",
    not _layout_model.get("ref") and lo.to_damage_spec(_layout_model) is None,
)
_layout_manual = {k: v for k, v in _layout_model.items() if k != "quality"}
check("无quality的手工分析保持spec兼容", lo.to_damage_spec(_layout_manual) is not None)
_layout_coordinate = lo.analyze(_coord_whole)
check(
    "真实坐标parser保留装备区候选几何",
    _layout_coordinate["quality"]["layout_evidence"]["equipment_region"]["bottom"] == 620
    and bool(_layout_coordinate["quality"]["layout_evidence"]["mod_rows"]),
)
check(
    "坐标文字候选不证明空槽及特殊槽完整",
    not _layout_coordinate["quality"]["layout_complete"]
    and not _layout_coordinate["quality"]["calculation_allowed"]
    and lo.to_damage_spec(_layout_coordinate) is None,
)

# 原图像素与OCR坐标联合验证，不把合成布局当实际游戏图准确率。
from PIL import Image as _LayoutImage, ImageDraw as _LayoutDraw  # noqa: E402
from core import pips as _layout_pixels  # noqa: E402

_layout_image = _LayoutImage.new("RGB", (1000, 800), (18, 18, 22))
_draw_layout = _LayoutDraw.Draw(_layout_image)
_names_layout = ["北风", "压迫点", "一击必杀", "肢解", "北风", "压迫点", "一击必杀", "肢解"]
_geometry_lines = [
    "20,100,20,40,90,容量",
    "200,100,20,50,90,4/70",
    "500,150,20,60,90,配置A",
    "500,800,20,60,90,所有",
]
for _index, _n in enumerate(_names_layout):
    _cx = 380 + (_index % 4) * 160
    _line_y = 300 + (_index // 4) * 180
    _draw_layout.rectangle((_cx - 60, _line_y, _cx + 60, _line_y + 2), fill=(143, 191, 229))
    _geometry_lines.append(f"{_cx},{(_line_y - 40) / 800 * 1000},24,90,90,{_n}")
_geometry_observation = lo.parse_coordinate_ocr("\n".join(_geometry_lines))
_geometry_observation["_pixel_layout"] = _layout_pixels.detect_layout(_layout_image)
_geometry_analysis = lo.analyze_layout(_geometry_observation)
check(
    "原图坐标与像素槽候选逐张唯一绑定",
    len(_geometry_analysis["bindings"]) == 8
    and all(b["status"] == "bound_candidate" for b in _geometry_analysis["bindings"]),
    str(_geometry_analysis["bindings"]),
)
check(
    "候选槽即使全对应也不自证全局完整",
    _geometry_analysis["slot_binding_complete"] and not _geometry_analysis["layout_complete"],
)
_missing_geometry = copy.deepcopy(_geometry_observation)
_missing_geometry["mods"].pop()
_missing_analysis = lo.analyze_layout(_missing_geometry)
check(
    "漏掉已观测槽保留显式缺口",
    len(_missing_analysis["unread_slots"]) == 1
    and any(r["code"] == "visible_slot_unread" for r in _missing_analysis["reasons"]),
)
_duplicate_geometry = copy.deepcopy(_geometry_observation)
_duplicate_geometry["mods"][1] = copy.deepcopy(_duplicate_geometry["mods"][0])
_duplicate_analysis = lo.analyze_layout(_duplicate_geometry)
check(
    "一槽两卡不借序号消冲突",
    sum(b["status"] == "conflict" for b in _duplicate_analysis["bindings"]) == 2,
)
_no_geometry = copy.deepcopy(_geometry_observation)
for _mod in _no_geometry["mods"]:
    _mod.pop("_geometry", None)
    _mod.update(row=1, col=1)
check("模型row/col不能代替原图坐标", not lo.analyze_layout(_no_geometry)["slot_binding_complete"])
# 同一slot排序改变不改变物理对应；使用不同格豆数证明不是按列表顺序借数。
_rank_grid = [
    {
        "is_inventory": False,
        "image_size": [1000, 800],
        "band": [300, 302],
        "grid": [380, 540, 700, 860],
        "card_w": 120,
        "counts": [1, 2, 3, 4],
    },
    {
        "is_inventory": False,
        "image_size": [1000, 800],
        "band": [480, 482],
        "grid": [380, 540, 700, 860],
        "card_w": 120,
        "counts": [4, 3, 2, 1],
    },
]
_reordered_geometry = copy.deepcopy(_geometry_observation)
_reordered_geometry["mods"] = list(reversed(_reordered_geometry["mods"]))
_reordered = lo.analyze_layout(_reordered_geometry, _rank_grid)
check(
    "打乱观察顺序仍按坐标定位豆数",
    _reordered["pixel_rank_positions"][0] == (1, 3)
    and _reordered["pixel_rank_positions"][7] == (0, 0),
    str(_reordered["pixel_rank_positions"]),
)
# 实际裁剪入口给出的变换用于反投影；原裁图观察逐字保留。
import base64 as _lb64  # noqa: E402
import io as _lio  # noqa: E402

_buffer_layout = _lio.BytesIO()
_layout_image.save(_buffer_layout, "PNG")
_data_layout = "data:image/png;base64," + _lb64.b64encode(_buffer_layout.getvalue()).decode()
_crop_url, _transform_layout = ScanCommands._scan_focus_image(
    _data_layout, "mods", with_geometry=True
)
_full_mod = _geometry_observation["mods"][0]
_g = _full_mod["_geometry"]
_bd = _transform_layout["bounds"]
_crop_mod = copy.deepcopy(_full_mod)
_crop_mod["_geometry"] = {
    "x": (_g["x"] - 1000 * _bd[0]) / (_bd[2] - _bd[0]),
    "y": (_g["y"] - 1000 * _bd[1]) / (_bd[3] - _bd[1]),
    "w": _g["w"] / (_bd[2] - _bd[0]),
    "h": _g["h"] / (_bd[3] - _bd[1]),
}
_reprojected = lo.remap_mod_geometry({"mods": [_crop_mod]}, _transform_layout)
check(
    "裁图坐标逆变换对应原图同槽且留原始坐标",
    close(_reprojected["mods"][0]["_geometry"]["x"], _g["x"])
    and _reprojected["mods"][0]["_geometry_observed"] == _crop_mod["_geometry"],
)
_unmapped = lo.remap_mod_geometry({"mods": [_crop_mod]}, None)
_unmapped["_pixel_layout"] = _geometry_observation["_pixel_layout"]
check(
    "无变换裁图不误当全图配槽",
    lo.analyze_layout(_unmapped)["bindings"][0]["reason"] == "unmapped_crop",
)
# 库存观察保留，与装备原始字段分开，不因同名混用。
_inv_csv = "\n".join(_geometry_lines) + "\n200,900,24,90,90,北风"
_inv_observed = lo.parse_coordinate_ocr(_inv_csv)
check(
    "仓库文字独立留证且不混入装备清单",
    len(_inv_observed["mods"]) == 8
    and any(r["text"] == "北风" for r in _inv_observed["coordinate_ocr"]["inventory_rows"]),
)
_malformed = {
    "mods": [
        {
            "name": "北风",
            "_geometry_source": "coordinate_ocr",
            "_geometry_view": "full_image",
            "_geometry": {"x": float("nan"), "y": 20},
        }
    ],
    "_pixel_layout": {
        "source": "pixel_geometry",
        "slots": [{"id": "bad", "kind": "regular_candidate", "box": None}],
    },
}
check("损坏几何保留未知不崩溃", not lo.analyze_layout(_malformed)["slot_binding_complete"])

_inventory_pixels = _layout_image.copy()
_inventory_draw = _LayoutDraw.Draw(_inventory_pixels)
for _cx in (110, 265, 420, 575, 730, 885):
    _inventory_draw.rectangle((_cx - 60, 700, _cx + 60, 702), fill=(143, 191, 229))
_inventory_payload = lo.parse_coordinate_ocr("\n".join(_geometry_lines) + "\n110,850,24,90,90,北风")
_inv_box = _inventory_payload["coordinate_ocr"]["inventory_rows"][0]
_inventory_payload["mods"].append(
    {
        "name": _inv_box["text"],
        "_geometry_source": "coordinate_ocr",
        "_geometry_view": "full_image",
        "_geometry": {k: _inv_box[k] for k in ("x", "y", "w", "h")},
    }
)
_inventory_payload["_pixel_layout"] = _layout_pixels.detect_layout(_inventory_pixels)
_inventory_binding = lo.analyze_layout(_inventory_payload)
check(
    "实际库存等级线候选能绑定并阻止当完整配卡",
    _inventory_binding["bindings"][-1]["status"] == "inventory"
    and any(r["code"] == "inventory_in_loadout" for r in _inventory_binding["reasons"]),
    str(_inventory_binding["bindings"][-1]),
)

_reordered_an = lo.analyze(_reordered_geometry, _rank_grid)
check(
    "几何定位经真实取豆入口使用本列，首末列不偏移",
    _reordered_an["mods"][0]["rank"] == 1 and _reordered_an["mods"][7]["rank"] == 1,
    str([(m["raw"], m["rank"]) for m in _reordered_an["mods"]]),
)
_distinct_rank = [{"is_inventory": False, "counts": [2, 7]}]
check("第一列不借第二列豆数", lo._pips_rank((0, 0), _distinct_rank, list(range(11)), 10)[0] == 2)
_field_bad = copy.deepcopy(_crop_mod)
_field_bad["_field_geometry"] = {"name": {"x": 3}, "drain": []}
_field_bad_mapped = lo.remap_mod_geometry({"mods": [_field_bad]}, _transform_layout)
_field_bad_mapped["_pixel_layout"] = _geometry_observation["_pixel_layout"]
check(
    "损坏字段几何不崩溃、不用未变换字段配槽",
    lo.analyze_layout(_field_bad_mapped)["bindings"][0]["reason"] == "field_geometry_invalid",
)


async def _layout_real_entry_contract():
    from types import SimpleNamespace
    import hashlib as _lhash

    class SavedGeometryProvider:
        async def text_chat(self, **kwargs):
            # 只验证保存观察与裁图逆变换协议，不声称文字由图像独立读对。
            g = _g
            text = (
                f"20,100,20,30,90,容量\n60,100,20,30,90,4/70\n500,990,20,60,90,所有\n"
                f"{g['x']},{g['y']},{g['h']},{g['w']},90,北风"
            )
            return SimpleNamespace(completion_text=text)

        def meta(self):
            return SimpleNamespace(id="offline-geometry-receipt")

    host = _ScanTestHost()
    host.cfg = {}
    host.RACE_WINDOW_S = 0.5
    host._scan_vision_providers = lambda: [SavedGeometryProvider()]
    focused = await host._read_scan_focus(_data_layout, "mods")
    check(
        "完整装备补读保留原图坐标",
        focused is not None
        and close(focused["mods"][0]["_geometry"]["x"], _g["x"])
        and focused["mods"][0]["_geometry_view"] == "full_image",
        str(focused),
    )
    check(
        "真实入口原始解析快照与变换分别保存",
        focused is not None
        and focused["_recognition"]["parsed"]["mods"][0]["_geometry"]
        == focused["mods"][0]["_geometry"]
        and focused["_recognition"]["geometry_transform"] == {"bounds": [0, 0, 1, 1]},
    )
    host._scan_focus_image = lambda image, part: image
    unmapped_read = await host._read_scan_focus(_data_layout, "mods")
    check(
        "完整装备补读不调用旧裁图适配器",
        unmapped_read is not None and unmapped_read["mods"][0]["_geometry_view"] == "full_image",
    )

    async def saved_extract(*args):
        return {**copy.deepcopy(_geometry_observation), "weapon": "驱魔之刃"}

    async def no_read(*args):
        return None

    async def no_pips(*args):
        return []

    host._extract_loadout_from_image = saved_extract
    host._read_scan_focus = no_read
    host._read_weapon_title = no_read
    host._read_damage_rows = no_read
    host._detect_pips = no_pips
    host._fit_scan_image = lambda image: image
    observed = await host._extract_loadout_validated(_data_layout)
    check(
        "真实识卡核验入口挂接原图像素观测且绑定图像字节",
        bool(observed and observed.get("_pixel_layout", {}).get("slots"))
        and observed["_pixel_layout"]["image_sha256"]
        == _lhash.sha256(_buffer_layout.getvalue()).hexdigest(),
    )
    check(
        "原图候选几何不会自动开计算闸",
        observed is not None and not lo.analyze(observed)["quality"]["calculation_allowed"],
    )


asyncio.run(_layout_real_entry_contract())

# Prime 的独立续行只和同列紧邻且词表精确存在的名称合并。
_split_prime = lo.parse_coordinate_ocr(
    "500,360,25,100,0,净化 Grineer\n500,387,25,70,0,Prime\n700,360,25,70,0,压迫点"
)
check(
    "同列Prime续行恢复完整词表名称",
    [m["name"] for m in _split_prime["mods"]] == ["净化 Grineer Prime", "压迫点"],
)
_far_prime = lo.parse_coordinate_ocr("500,360,25,100,0,净化 Grineer\n500,520,25,70,0,Prime")
check("远处Prime不改写卡名", [m["name"] for m in _far_prime["mods"]] == ["净化 Grineer"])

import base64  # noqa: E402
import io  # noqa: E402
from PIL import Image  # noqa: E402

for _expected_color, _rgb in (
    ("绿", (140, 240, 60)),
    ("红", (245, 40, 40)),
    ("白", (240, 240, 240)),
):
    _image = Image.new("RGB", (100, 100), (20, 20, 20))
    _image.paste(_rgb, (70, 10, 80, 20))
    _buffer = io.BytesIO()
    _image.save(_buffer, format="PNG")
    _color_input = {
        "mods": [
            {
                "name": "压迫点",
                "drain": 5,
                "color": "?",
                "_geometry_view": "full_image",
                "_field_geometry": {"drain": [{"x": 750, "y": 150, "w": 100, "h": 100}]},
            }
        ]
    }
    lo.observe_capacity_colors(
        _color_input, "data:image/png;base64," + base64.b64encode(_buffer.getvalue()).decode()
    )
    check(
        "只采样已定位容量数字颜色：" + _expected_color,
        _color_input["mods"][0]["color"] == _expected_color,
    )
check("裁图候选不获得计算许可", not lo.analyze(_no_inventory)["quality"]["calculation_allowed"])


# 双读合并：用不同卡数和重排覆盖插槽对齐；模型坐标不能升格为独立OCR证据。
_merge_primary = {
    "weapon": "未知标题",
    "mods": [
        {
            "name": "北风",
            "drain": 6,
            "color": "绿",
            "_geometry": {"x": 700, "y": 600},
            "_geometry_source": "coordinate_ocr",
        },
        {
            "name": "压迫点",
            "drain": 9,
            "color": "白",
            "_geometry": {"x": 400, "y": 600},
            "_geometry_source": "coordinate_ocr",
        },
    ],
    "arcanes": [
        {
            "name": "近战·模糊文字",
            "_geometry_source": "coordinate_ocr",
            "_geometry": {"x": 900, "y": 300, "w": 70, "h": 24},
        }
    ],
}
_merge_focus = {
    "weapon": "驱魔之刃",
    "mods": [
        {"name": "肢解", "drain": 9, "color": "白", "name_box": [1, 2, 3, 4], "rank_lit": 999},
        {"name": "压迫点", "drain": 1, "color": "红"},
        {"name": "北风", "drain": 1, "color": "红"},
    ],
    "arcanes": [{"name": "近战·暴露", "rank_lit": 5, "name_box": [1, 2, 3, 4]}],
}
_merge_result = lo.merge_equipment_read(_merge_primary, _merge_focus)
check(
    "完整双读按补读顺序插入漏卡",
    [m["name"] for m in _merge_result["mods"]] == ["肢解", "压迫点", "北风"],
)
check(
    "共同卡保留独立几何与容量",
    _merge_result["mods"][2]["_geometry"]["x"] == 700 and _merge_result["mods"][2]["drain"] == 6,
)
check(
    "新增卡的模型坐标不冒充独立OCR",
    _merge_result["mods"][0].get("_geometry_source") != "coordinate_ocr",
)
check(
    "赋能补读用同类唯一图标定位而非模型框",
    _merge_result["arcanes"][0]["_geometry"] == _merge_primary["arcanes"][0]["_geometry"],
)
check(
    "双读合并不改写输入",
    _merge_primary["weapon"] == "未知标题" and _merge_focus["mods"][2]["drain"] == 1,
)
_ambiguous_arcane = copy.deepcopy(_merge_primary)
_ambiguous_arcane["arcanes"] *= 2
check(
    "多个同类赋能位置不猜绑定",
    lo.merge_equipment_read(_ambiguous_arcane, _merge_focus)["arcanes"][0].get("_geometry_source")
    != "coordinate_ocr",
)
_normalized_panel = lo.normalize_scan_structure(
    {"panel": {"rows": [{"label": "暴击几率", "left": 20, "right": 30, "unit": "%"}]}}
)
check("共同面板协议保留比较两侧", _normalized_panel["panel"]["crit_chance"] == "20>30")


_arcane_punctuation = lo.normalize_scan_structure(
    {"arcanes": [{"name": "次要：测试赋能"}, {"name": "主要:测试赋能"}]}
)
for _category in ("主要", "次要", "近战"):
    for _separator in (".", "\uff0e", ":", "：", "・"):
        _name = _category + _separator + "测试赋能"
        check(
            "类别分隔符只作排版归一：" + _name,
            lo.parse_focus_text("500,500,100,20,0," + _name, "arcanes")["arcanes"][0]["name"]
            == _category + "·测试赋能",
        )
check("没有类别前缀的句点不是赋能", lo.parse_focus_text("12.5% 伤害", "arcanes") is None)
check(
    "赋能类别分隔符可归一",
    [a["name"] for a in _arcane_punctuation["arcanes"]] == ["次要·测试赋能", "主要·测试赋能"],
)


_misplaced_arcane = {
    "mods": [
        {"name": "次要:测试赋能", "drain": 99, "rank_lit": 10},
        {"name": "主要伤害"},
        {"name": "北风", "drain": 8},
    ],
    "arcanes": [],
}
_reclassified = lo.normalize_scan_structure(_misplaced_arcane)
check(
    "赋能类别从MOD列表归位且不继承猜测等级", _reclassified["arcanes"] == [{"name": "次要·测试赋能"}]
)
check(
    "无类别分隔符的未知名称保留原处",
    [m["name"] for m in _reclassified["mods"]] == ["主要伤害", "北风"],
)
check(
    "字段归位保留原始观察且不原地改写",
    _reclassified["_field_reclassification"][0]["raw"]["rank_lit"] == 10
    and len(_misplaced_arcane["mods"]) == 3,
)
_misplaced_arcane["arcanes"] = [{"name": "次要·测试赋能"}]
check(
    "两个字段同名赋能不重复输出",
    len(lo.normalize_scan_structure(_misplaced_arcane)["arcanes"]) == 1,
)


_synthetic_capacity_giver = {"base_drain": -3, "max_rank": 4}
check(
    "提供容量的同极性双倍可反推非满级",
    lo.rank_candidates(_synthetic_capacity_giver, 10, "绿") == [2],
)
check("提供容量的无极性保留原数", lo.infer_rank(_synthetic_capacity_giver, 6, "白")[0] == 3)
check(
    "提供容量的异极性按80%向下取整", lo.rank_candidates(_synthetic_capacity_giver, 5, "红") == [4]
)
check(
    "提供容量无法确定时保留多解", lo.rank_candidates(_synthetic_capacity_giver, 4, "红") == [2, 3]
)
check("提供容量超范围不补满", lo.infer_rank(_synthetic_capacity_giver, 99, "绿")[0] is None)

_singleton_layout = copy.deepcopy(_geometry_observation)
_unique_names = [
    "Serration",
    "Hornet Strike",
    "Vital Sense",
    "Organ Shatter",
    "North Wind",
    "Pressure Point",
    "Shocking Touch",
    "Fever Strike",
]
for _item, _name in zip(_singleton_layout["mods"], _unique_names):
    _item["name"] = _name
_singleton_layout["_equipment_merge"] = {"complete_shared_identity": True}
_singleton_layout["mods"][-1].pop("_geometry", None)
_singleton_result = lo.analyze_layout(_singleton_layout, _rank_grid)
check(
    "独立定位只剩一卡一槽可唯一对齐",
    _singleton_result["bindings"][-1]["reason"] == "unique_remaining_equipment_slot"
    and _singleton_result["pixel_rank_positions"].get(7) == (1, 3),
)
check("排除法不升级完整布局证据", not _singleton_result["layout_complete"])
_singleton_layout["mods"][-2].pop("_geometry", None)
check(
    "剩余两卡两槽不猜绑定",
    all(b["status"] == "unresolved" for b in lo.analyze_layout(_singleton_layout)["bindings"][-2:]),
)

_hint_only = lo.normalize_scan_structure({"mods": [{"name": "北风", "drain": 8, "color": "红"}]})
check("模型颜色标明提示来源", _hint_only["mods"][0]["_color_source"] == "model_hint")


_arcane_primary_exact = copy.deepcopy(_merge_primary)
_arcane_primary_exact["arcanes"][0]["name"] = "近战·暴露"
_arcane_focus_unknown = copy.deepcopy(_merge_focus)
_arcane_focus_unknown["arcanes"][0]["name"] = "近战·非词表名称"
_arcane_preserved = lo.merge_equipment_read(_arcane_primary_exact, _arcane_focus_unknown)
check(
    "词表外补读名称不覆盖独立精确赋能名",
    _arcane_preserved["arcanes"][0]["name"] == "近战·暴露"
    and _arcane_preserved["arcanes"][0]["_name_selection"]["discarded_observation"]
    == "近战·非词表名称",
)

# 共同坐标协议的两种载体，以及结构化框与独立像素的交叉验证。

_csv_boxes = "500,120,180,24,0,升级/驱魔之刃 [30]\n500,450,90,24,0,北风\n570,390,30,20,0,6"
_json_boxes_text = _json_boxes.dumps(
    [
        {"text": "升级/驱魔之刃 [30]", "bbox_2d": [410, 108, 590, 132]},
        {"text": "北风", "bbox_2d": [455, 438, 545, 462]},
        {"text": "6", "bbox_2d": [555, 380, 585, 400]},
    ],
    ensure_ascii=False,
)
_csv_parsed, _json_parsed = (
    lo.parse_coordinate_ocr(_csv_boxes),
    lo.parse_coordinate_ocr(_json_boxes_text),
)
check(
    "显式JSON角点与CSV中心框走同一归类",
    _csv_parsed["mods"] == _json_parsed["mods"] and _csv_parsed["weapon"] == _json_parsed["weapon"],
)
for _bad_box in ([10, 10, 9, 20], [0, 0, 1001, 20], [True, 0, 20, 20], [0, 0, float("nan"), 20]):
    check(
        "非法角点不生成坐标观察：" + str(_bad_box),
        lo.parse_coordinate_ocr(_json_boxes.dumps([{"text": "北风", "bbox_2d": _bad_box}])) is None,
    )
check(
    "未标坐标类型的四元组不猜格式",
    lo.parse_coordinate_ocr('[{"text":"北风","box":[1,2,3,4]}]') is None,
)
check("截断坐标列表不补造尾项", lo.parse_coordinate_ocr(_json_boxes_text[:-1]) is None)

_structured_raw = {
    "weapon": "驱魔之刃",
    "mods": [
        {
            "name": "北风",
            "drain": 11,
            "color": "白",
            "rank_lit": 999,
            "name_box": [500, 500, 24, 80],
            "drain_box": [550, 440, 20, 15],
        }
    ],
}
_structured = lo.remap_mod_geometry(
    lo.normalize_scan_structure(_structured_raw), {"bounds": [0, 0, 1, 1]}
)
_structured["_pixel_layout"] = {
    "source": "pixel_geometry",
    "slots": [
        {
            "id": "only-slot",
            "box": [0.4, 0.4, 0.6, 0.62],
            "line_box": [0.4, 0.59, 0.6, 0.61],
            "kind": "regular_candidate",
            "zone": "equipment_candidate",
            "content_state": "rank_marks_observed",
        }
    ],
    "coverage": {"status": "unknown"},
}
_structured_rows = [
    {
        "counts": [5],
        "grid": [500],
        "band": [590, 610],
        "image_size": [1000, 1000],
        "card_w": 200,
        "is_inventory": False,
        "maxed": [False],
    }
]
_structured_an = lo.analyze(_structured, _structured_rows)
check("结构化名称框保留单独来源", _structured["mods"][0]["_geometry_source"] == "structured_box")
check(
    "结构化框经像素槽位唯一绑定才取豆数",
    _structured_an["mods"][0]["rank"] == 5
    and _structured_an["mods"][0]["provenance"]["rank"]["source"] == "pixel_pips",
)
check(
    "模型等级999不进入实际等级",
    _structured_an["mods"][0]["rank"] != _structured_raw["mods"][0]["rank_lit"],
)
_crossed_fields = copy.deepcopy(_structured)
_crossed_fields["mods"][0]["_field_geometry"]["drain"][0]["x"] = 800
_crossed_result = lo.analyze_layout(_crossed_fields, _structured_rows)
check(
    "名称与容量落入不同槽位拒绝像素绑定",
    _crossed_result["bindings"][0]["reason"] == "name_drain_slots_disagree"
    and not _crossed_result["pixel_rank_positions"],
)
_without_slots = copy.deepcopy(_structured)
_without_slots["_pixel_layout"]["slots"] = []
check(
    "结构化框无像素槽位不走行列补位",
    not lo.analyze_layout(_without_slots, _structured_rows)["pixel_rank_positions"],
)
_invalid_structured = lo.normalize_scan_structure(
    {"mods": [{"name": "北风", "name_box": [float("nan"), 20, 20, 50]}]}
)
check(
    "非法结构化框留痕且不生成坐标",
    _invalid_structured["mods"][0]["_geometry_field_errors"] == ["name_box"]
    and "_geometry" not in _invalid_structured["mods"][0],
)
_structured_crop = lo.remap_mod_geometry(
    lo.normalize_scan_structure(_structured_raw), {"bounds": [0.2, 0.1, 0.8, 0.9]}
)
check(
    "结构化框裁剪回投只执行一次",
    lo.normalize_scan_structure(_structured_crop)["mods"][0]["_geometry"]
    == _structured_crop["mods"][0]["_geometry"],
)
check("坐标归一不改原始结构", "_geometry_source" not in _structured_raw["mods"][0])

_color_guess = lo.normalize_scan_structure({"mods": [{"name": "北风", "drain": 8, "color": "白"}]})
_color_guess_an = lo.analyze(_color_guess)
check(
    "模型颜色不能把容量多解变成确定等级",
    _color_guess_an["mods"][0]["rank"] is None
    and len(_color_guess_an["mods"][0]["provenance"]["rank"]["candidates"]) > 1,
)
_color_guess["mods"][0]["_color_evidence"] = {"source": "capacity_number_pixels"}
_color_pixel_an = lo.analyze(_color_guess)
check("独立数字像素颜色仍可唯一反推等级", _color_pixel_an["mods"][0]["rank"] == 2)

# 列表元素在字典/名称字符串间漂移时保留文字，坏形状保留占位与原观察。
_shape_raw = {"mods": ["膛线", None], "arcanes": ["主要·无情", {"name": "次要·妨害"}]}
_shape = lo.normalize_scan_structure(_shape_raw)
check(
    "名称字符串恢复成观察字段",
    _shape["mods"][0]["name"] == "膛线" and _shape["arcanes"][0]["name"] == "主要·无情",
)
check("坏形状不缩短MOD分母", len(_shape["mods"]) == 2 and bool(_shape.get("_structure_errors")))
check(
    "结构归一不改原始回执",
    _shape_raw["mods"] == ["膛线", None] and isinstance(_shape_raw["arcanes"][0], str),
)
check("结构归一重复执行不增条目", lo.normalize_scan_structure(_shape) == _shape)


# 旋转协议正反验证，横向/竖向相同图像框，非法框不生成证据。
check(
    "旋转框0/90度等价",
    lo.coordinate_aabb([500, 450, 90, 24, 0])[:4] == lo.coordinate_aabb([500, 450, 24, 90, 90])[:4],
)
for _bad_rect in (
    [True, 1, 2, 3, 0],
    [500, 500, -1, 30, 0],
    [500, 500, 30, 30, 91],
    [500, 500, float("inf"), 2, 0],
):
    check("非法旋转框不获几何：" + str(_bad_rect), lo.coordinate_aabb(_bad_rect) is None)
_rotated_json = _json_boxes.dumps([{"rotate_rect": [500, 450, 90, 24, 0], "text": "北风"}])
check(
    "显式旋转JSON保留文字与框",
    lo.parse_coordinate_ocr(_rotated_json)["mods"][0]["_geometry"]
    == {"x": 500, "y": 450, "h": 24, "w": 90},
)
_dup_csv = _csv_boxes + "\n500,450,90,24,0,北风"
check("同区域重复文字只消费一次", len(lo.parse_coordinate_ocr(_dup_csv)["mods"]) == 1)
_two_csv = _csv_boxes + "\n700,450,90,24,0,北风"
check("不同槽位同名卡不能按名称去重", len(lo.parse_coordinate_ocr(_two_csv)["mods"]) == 2)
check(
    "完整坐标行前标记仅解包不补字",
    lo.parse_coordinate_ocr("<" + _csv_boxes) == lo.parse_coordinate_ocr(_csv_boxes),
)
for _partial in ("<500,450,90,24,0", "<500,450,90,24,0,", "<500,450,90,24,91,未知"):
    check("不完整坐标或非法角度保留原行", lo.coordinate_text_lines(_partial) == [_partial])
_unknown_ui = lo.normalize_scan_structure({"mods": [{"name": "连击"}, {"name": "未知卡片"}]})
check(
    "界面角色分流不吞未知MOD",
    [m["name"] for m in _unknown_ui["mods"]] == ["未知卡片"]
    and _unknown_ui["_field_reclassification"][0]["raw"]["name"] == "连击",
)


async def _arcane_crop_entry():
    class Provider:
        def meta(self):
            return SimpleNamespace(id="offline-arcane-geometry")

        async def text_chat(self, **kwargs):
            return SimpleNamespace(completion_text="500,500,100,20,0,主要·无情")

    host = _ScanTestHost()
    host.cfg = {"vision_provider_id_scan": "offline-arcane-geometry"}
    host.context = SimpleNamespace(get_provider_by_id=lambda _id: Provider())
    buffer = io.BytesIO()
    Image.new("RGB", (1000, 500)).save(buffer, format="PNG")
    url = "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()
    return await host._read_scan_focus(url, "arcanes")


_arcane_crop = asyncio.run(_arcane_crop_entry())
_arcane_box = _arcane_crop["arcanes"][0]
check(
    "真实赋能补读将裁图坐标映射回原图",
    _arcane_box["_geometry"]["x"] == 900
    and _arcane_box["_geometry"]["y"] == 260
    and abs(_arcane_box["_geometry"]["w"] - 20) < 1e-9
    and abs(_arcane_box["_geometry"]["h"] - 10.4) < 1e-9
    and _arcane_box["_geometry_view"] == "full_image",
)
_unmapped_arcane = lo.remap_mod_geometry(_arcane_crop, None)
_unmapped_arcane["arcanes"][0]["rank"] = 5
lo.observe_arcane_ranks(_unmapped_arcane, _crop_url)
check(
    "未知裁剪变换不据错误位置给赋能等级",
    _unmapped_arcane["arcanes"][0]["rank"] is None,
)

if FAILED:
    print(f"\n失败 {len(FAILED)} 项：{FAILED}")
    sys.exit(1)
print("\n全部通过 ✔")
