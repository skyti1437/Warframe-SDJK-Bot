# -*- coding: utf-8 -*-
"""帮助卡列对齐/配色链路离线测试：python3 tests/test_help_table.py

2026-09-14 玩家反馈「指令和介绍颜色太接近」「一行三个指令要三个颜色」。
根因：帮助卡说明列整体超宽，触发 render() 的列宽安全阀把 _table_mode
整个关掉 —— _draw_plain_cell（指令列金色 / 说明列压暗 / 「A / B / C」
多指令分色）整条链路从未执行，全卡退化成单色。

双列各自折行（_plan_help_columns），保住列对齐与配色。
本文件离线验证规划函数与相关纯逻辑（不依赖 Pillow）。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.render import _plan_help_columns, _split_cells  # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = ""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f"  -> {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


# 假宽度函数：CJK/全角按 2、其余按 1（测试只要单调可复现，不要像素精度）
def w(text: str) -> float:
    return float(sum(2 if ord(c) > 0x2E7F else 1 for c in text))


def wrap(text: str, budget: float, *, strict: bool = False) -> list[str]:
    out, cur, cur_w = [], "", 0.0
    for ch in text:
        cw = w(ch)
        if cur and cur_w + cw > budget:
            out.append(cur)
            cur, cur_w = ch, cw
        else:
            cur += ch
            cur_w += cw
    if cur:
        out.append(cur)
    return out


# ------------------------------------------------ 不超宽：原样返回，不折行
cells = [(0, "帮助", "指令总览"), (1, "赏金", "各地区轮换")]
wrap_map, widths = _plan_help_columns(cells, wrap, w, 80, gap=4)
check("不超宽不折行", wrap_map == {}, f"{wrap_map}")
check("未折行时保留原列宽", widths == [4.0, 10.0], str(widths))

# ------------------------------------------------ 超宽：长说明折行、短说明不动
cells = [
    (0, "帮助", "指令总览"),
    (1, "蹲 取消", "取消订阅：蹲 取消=全部；带筛选词=只取消匹配项（词序随意）"),
]
wrap_map, widths = _plan_help_columns(cells, wrap, w, 50, gap=4)
check("超宽触发折行", 1 in wrap_map, str(wrap_map.keys()))
check("短说明不在折行表", 0 not in wrap_map)
segs = [value for _, value in wrap_map.get(1, ())]
check("折行≥2段", len(segs) >= 2, str(segs))
budget = 50 - widths[0] - 4
check(
    "每段都不超预算",
    all(w(s) <= budget for s in segs),
    f"budget={budget} segs={[(s, w(s)) for s in segs]}",
)
check("折行后说明列符合预算", widths[1] <= budget, str(widths))
check("折行不丢字", "".join(segs) == cells[1][2])

# ------------------------------------------------ 超长指令留出说明宽度，两列分别延续
long_key = "loadoutrecommend <weapon> [mode] [target options]"
short_value = "Categories and subscriptions"
wrap_map, widths = _plan_help_columns([(0, long_key, short_value)], wrap, w, 80, gap=4)
parts = wrap_map.get(0, [])
check("超长指令折行并限制左列宽度", len(parts) > 1 and widths[0] <= 80 * 0.46, str(parts))
check("指令折行时说明仍从首行开始", bool(parts) and parts[0][1] == short_value, str(parts))
check("指令续行不丢字", "".join(key for key, _ in parts) == long_key, str(parts))

# ------------------------------------------------ 空输入
wrap_map, widths = _plan_help_columns([], wrap, w, 80, gap=4)
check("空输入安全", wrap_map == {} and widths == [0.0, 0.0], f"{wrap_map} {widths}")

# ------------------------------------------------ _split_cells 配合「·」剥离
cells_line = _split_cells("· 活动 / 武形秘仪 / 九重天　限时活动 / Conclave / 航道星舰")
check(
    "切列剥点+保住斜杠词条",
    cells_line == ["活动 / 武形秘仪 / 九重天", "限时活动 / Conclave / 航道星舰"],
    str(cells_line),
)
check("多指令词条可按「 / 」分成3段", len(cells_line[0].split(" / ")) == 3)

# ------------------------------------------------ 真实 HELP_TOPIC 全量演练：
# 用假宽度函数模拟 render 的规划，确保所有说明列都能被折进预算内

import tests.test_dun as _td  # noqa: E402  复用其 astrbot 桩

_td._install_astrbot_stub()
import main  # noqa: E402

pairs = []
for items in main.HELP_TOPIC.values():
    for cmd, desc in items:
        pairs.append((len(pairs), cmd, desc))
wrap_map, widths = _plan_help_columns(pairs, wrap, w, 220, gap=4)
check("真实帮助数据：折行后两列不超预算", sum(widths) + 4 <= 220, str(widths))
check("真实帮助数据：说明不含全角空格（切列安全）", all("　" not in d for _, _, d in pairs))
check("真实帮助数据：说明不含换行", all("\n" not in d for _, _, d in pairs))

print()
if FAILED:
    print(f"FAILED {len(FAILED)}: " + "; ".join(FAILED))
    sys.exit(1)
print("ALL PASS")
