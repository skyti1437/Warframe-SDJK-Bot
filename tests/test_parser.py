# -*- coding: utf-8 -*-
"""解析器 / 计算器离线冒烟测试：python3 tests/test_parser.py"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import calculators as calc          # noqa: E402
from core.parser import (parse, parse_duration, parse_fissure_filter,  # noqa: E402
                         parse_time_window, parse_wm, parse_wr)

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = ""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f"  -> {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


# ---------------------------------------------------------------- 主指令与修饰符
p = parse("wr 基多暴负变焦 绝路 -2")
check("任意位置-主指令识别 wr", p.command == "wr" and p.command_raw == "wr")
check("翻页 -2", p.page == 2)
check("修饰符收集", p.modifiers == ["-2"])

p = parse("-pc 夜灵")
check("修饰符在前也能识别主指令", p.command == "cetus" and p.platform == "pc")

p = parse("夜灵 平原时间")
check("首个指令词生效，后续退化为内容", p.command == "cetus" and p.content == ["平原时间"])

p = parse("裂隙 -t -r -3")
check("修饰符叠加：t/r/页码",
      p.force_image and p.whisper and p.page == 3)

p = parse("wm 绝路 -w -ps")
check("文字模式+平台覆盖", p.force_text and p.platform == "ps")

p = parse("金垃圾")
check("预设指令 金垃圾->ducats", p.command == "ducats" and p.preset == "金")

p = parse("钢铁裂隙 -1")
check("钢铁裂隙预设 + 文字模式", p.command == "fissures" and p.preset == "钢铁" and p.force_text)

p = parse("今天天气不错")
check("无指令不误触发", p.command is None)

p = parse('wm "kuva bramma" 收购')
check("引号包裹英文物品名", p.content[0] == "kuva bramma" and p.command == "wm")

# ---------------------------------------------------------------- wm 二级解析
q = parse_wm(["充沛", "收购"])
check("wm 收购", q.buy and q.item == "充沛")
q = parse_wm(["古纪", "A1", "光辉"])
check("wm 遗物精炼等级", q.refinement == "radiant" and q.item == "古纪 A1")
q = parse_wm(["合购", "saryn*2,loki"])
check("wm 合购列表", q.group_buy == [("saryn", 2), ("loki", 1)])
q = parse_wm(["充沛", "3个", "满级"])
check("wm 数量+等级", q.quantity == 3 and q.rank == -1 and q.rank_word == "满级")

# ---------------------------------------------------------------- wr 二级解析
w = parse_wr(["基多暴负变焦", "绝路"])
check("词条连写：基多暴(3正)+负变焦",
      w.stats == ["melee_damage", "multishot", "crit_chance"]
      and w.negatives == ["zoom"] and w.weapon == "绝路",
      f"stats={w.stats} neg={w.negatives} weapon={w.weapon}")

w = parse_wr(["双暴", "绝路", "1000p", "零洗", "2+1"])
check("双暴/价格/洗数/词条数",
      set(w.stats) == {"crit_chance", "crit_damage"} and w.max_price == 1000
      and w.max_rerolls == 0 and w.positive_count == 2 and w.negative_count == 1,
      f"stats={w.stats}")

w = parse_wr(["r槽", "绝路"])
check("极性槽 r槽->madurai", w.polarity == "madurai" and w.weapon == "绝路")
w = parse_wr(["基伤", "多重", "负G伤", "绝路"])
check("负G伤", w.stats == ["melee_damage", "multishot"] and w.negatives == ["damage_vs_grineer"])
w = parse_wr(["带负", "绝路"])
check("带负=要求存在负面", w.require_negative and w.weapon == "绝路")
w = parse_wr(["kuva", "bramma"])
check("英文武器名不误吞", w.weapon == "kuva bramma" and not w.stats)

# ---------------------------------------------------------------- 裂隙筛选
f = parse_fissure_filter("普通捕获,钢铁虚空生存")
g0, g1 = f.groups
check("裂隙组1：普通捕获", g0["hard"] is False and g0["missions"] == {"capture"}, str(g0))
check("裂隙组2：钢铁生存", g1["hard"] is True and g1["missions"] == {"survival"}, str(g1))
sample = {"isHard": False, "isStorm": False, "missionType": "Capture", "tier": "Lith",
          "node": "Teshub (Eris)"}
check("裂隙匹配器", f.match(sample))
f2 = parse_fissure_filter("九重天 塞德娜")
check("九重天+星球筛选", f2.groups[0]["storm"] is True and f2.groups[0]["substr"] == "塞德娜")

# ---------------------------------------------------------------- 蹲：时长与时间窗
check("时长解析", parse_duration("永久") == -1 and parse_duration("两周") == 1209600
      and parse_duration("3小时") == 10800 and parse_duration("7天") == 604800)
check("时长缺省=命中一次", parse_duration("") is None and parse_duration("绝路") is None)
w = parse_time_window("22到8")
check("跨零点时间窗", w.start == 22 and w.end == 8 and w.allows(
    __import__("datetime").datetime(2026, 9, 9, 23, 10)))
w = parse_time_window("每天19点")
check("每天定点", w.at_hour == 19)
w = parse_time_window("周1/3/5 23点")
check("星期+小时", w.days == {1, 3, 5} and w.start == 23)

# ---------------------------------------------------------------- 计算器
# 效价融合：官方公式 result = min(1.1×较高值, 60)，≥58% 进位 60
check("融合 40+52 = 57.2", calc.valence_bonus(40, 52) == 57.2,
      str(calc.valence_bonus(40, 52)))
check("融合 28+25 = 30.8", calc.valence_bonus(28, 25) == 30.8,
      str(calc.valence_bonus(28, 25)))
check("融合 55+40 封顶 60", calc.valence_bonus(55, 40) == 60,
      str(calc.valence_bonus(55, 40)))
check("融合 52.8 一步满值", calc.valence_bonus(52.8, 40) == 60,
      str(calc.valence_bonus(52.8, 40)))
v = calc.valence_fusion("电", 60, "火", 58)
check("异元素给出两个可选项（不合成复合元素）",
      v["options"] == ["电", "火"] and v["percent"] == 60, str(v))
v = calc.valence_fusion("火", 44, "火", 50)
check("同元素只有一个选项", v["options"] == ["火"], str(v))
v = calc.valence_fusion("电", 30, "磁力", 40)
check("Tenet 元素可识别", v["options"] == ["电", "磁力"], str(v))
check("未知元素报错", "error" in calc.valence_fusion("电", 30, "zzz", 40))
trace = calc.fusion_to_cap(25)
check("25% 到满值需 9 次", trace[-1] == 60 and len(trace) == 9, str(trace))
check("52.8% 到满值需 1 次", len(calc.fusion_to_cap(52.8)) == 1)

# ---------------------------------------------------------------- P1 指令解析
p = parse("排行 甲")
check("排行分类", p.command == "rank" and p.content == ["甲"])

p = parse("战甲排行")
check("别名预设 甲排行",
      p.command == "rank" and p.preset == "甲" and p.content == [])

p = parse("MOD排行")
check("别名预设 MOD排行->卡", p.command == "rank" and p.preset == "卡")

p = parse("趋势 绝路")
check("趋势", p.command == "trend" and p.content == ["绝路"])

p = parse("wm趋势 绝路")
check("wm趋势别名", p.command == "trend" and p.content == ["绝路"])

p = parse("开核桃 速刷 Axi A1 未入库")
check("开核桃子参数",
      p.command == "openrelic"
      and "速刷" in p.content and "未入库" in p.content
      and "Axi" in p.content and "A1" in p.content)

p = parse("遗物入库")
check("遗物入库预设", p.command == "relic" and p.preset == "入库")

p = parse("遗物出库")
check("遗物出库预设", p.command == "relic" and p.preset == "出库")

p = parse("零件 绝路")
check("零件=部件", p.command == "parts" and p.content == ["绝路"])

p = parse("日历 奖励")
check("日历子参数", p.command == "calendar" and p.content == ["奖励"])

p = parse("仲裁 生存 高效")
check("仲裁子参数", p.command == "arbitration"
      and "生存" in p.content and "高效" in p.content)

print()
if FAILED:
    print(f"共 {len(FAILED)} 项失败：{FAILED}")
    sys.exit(1)
print("全部通过 ✔")
