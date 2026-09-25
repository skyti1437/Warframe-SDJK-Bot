# -*- coding: utf-8 -*-
"""钢铁精华兑换表离线测试：python3 tests/test_steel_shop.py

覆盖三件事：
1. 兑换表结构完整（常驻 24 件、每周轮换 8 件、分组字段齐全）；
2. 轮换推算锚点正确（本周=霰弹枪紫卡，下周=Umbra Forma 蓝图，8 周后回环）；
3. 渲染文本包含轮换倒计时与分组标题。
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import formatters as F  # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = ""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f"  -> {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


def _at(iso: str):
    """把 formatters 的「现在」固定到指定时刻，便于断言轮换。"""
    dt = datetime.fromisoformat(iso)
    F._now = lambda: dt          # type: ignore[assignment]


def cur_week_name(iso: str) -> str:
    """在指定时刻推算本周轮换商品名。"""
    _at(iso)
    lines, _, _ = F._steel_rotation(F.steel_shop())
    # 第 2 行形如「· 霰弹枪紫卡　75 精华」
    return lines[1].split("　")[0].lstrip("· ")


# ---------------------------------------------------------------- 表结构
data = F.steel_shop()
ever = data.get("evergreen") or []
week = data.get("weekly") or []
check("兑换表可读取", bool(data))
check("常驻商品 24 件", len(ever) == 24, f"实际 {len(ever)}")
check("每周轮换 8 件", len(week) == 8, f"实际 {len(week)}")
check("常驻商品均有分组", all(i.get("group") for i in ever))
check("常驻商品均有名称与价格",
      all(i.get("name") and isinstance(i.get("cost"), int) for i in ever))
check("本周轮换含《老师》系列",
      sum(1 for i in ever if "《老师》" in i["name"]) == 5,
      str([i["name"] for i in ever if "《老师》" in i["name"]]))
check("《老师》四场景均为 50 精华",
      all(i["cost"] == 50 for i in ever if "《老师》" in i["name"] and "曲目" not in i["name"]))
check("错译已修正：三轨幻纹", any(i["name"] == "三轨幻纹" for i in ever))
check("错译已修正：架式 Forma 蓝图", any("架式 Forma" in i["name"] for i in ever))
check("错译已修正：奥罗金茶具", any(i["name"] == "奥罗金茶具" for i in ever))

rot = data.get("rotation") or {}
check("轮换锚点字段齐全",
      bool(rot.get("epoch")) and rot.get("index_at_epoch") is not None
      and rot.get("period_hours") == 168, str(rot))

# ---------------------------------------------------------------- 轮换推算
# 2026-09-11：实测（warframe.today）本周为霰弹枪紫卡、下周一换成 Umbra Forma 蓝图
check("2026-09-11 本周=霰弹枪紫卡", cur_week_name("2026-09-11T06:14:00+00:00") == "霰弹枪紫卡",
      cur_week_name("2026-09-11T06:14:00+00:00"))
check("2026-09-14 本周=Umbra Forma 蓝图",
      cur_week_name("2026-09-14T00:30:00+00:00") == "Umbra Forma 蓝图",
      cur_week_name("2026-09-14T00:30:00+00:00"))
# 8 周后应回到同一件
check("8 周回环", cur_week_name("2026-11-09T00:30:00+00:00") == "Umbra Forma 蓝图",
      cur_week_name("2026-11-09T00:30:00+00:00"))
# 边界：重置前一分钟仍是旧货，重置后一分钟换新
check("重置边界（前 1 分钟）",
      cur_week_name("2026-09-13T23:59:00+00:00") == "霰弹枪紫卡",
      cur_week_name("2026-09-13T23:59:00+00:00"))
check("重置边界（后 1 分钟）",
      cur_week_name("2026-09-14T00:01:00+00:00") == "Umbra Forma 蓝图",
      cur_week_name("2026-09-14T00:01:00+00:00"))

# ---------------------------------------------------------------- 渲染
_at("2026-09-11T06:14:00+00:00")
title, body = F.fmt_steel_essence_shop()
text = "\n".join(body)
check("标题含 Teshin 荣誉商店", "Teshin" in title)
check("正文含本周/下周轮换", "本周轮换" in text and "下周轮换" in text)
check("正文含轮换倒计时", "距轮换" in text)
check("正文含分组标题", all(f"【{g}】" in text for g in
                          {i["group"] for i in ever}))
check("正文列出全部常驻商品", all(i["name"] in text for i in ever))

if FAILED:
    print(f"\n失败 {len(FAILED)} 项：{FAILED}")
    sys.exit(1)
print("\n全部通过 ✔")
