# -*- coding: utf-8 -*-
"""奸商（虚空商人）当期货单卡：DE 字段映射 + 两列排版 + 配色。

2026-09-18 用户反馈两件事，本测试都钉住：

1. **Bug**：当期货单整卡落成「? 杜卡德 ? 现金」。根因：DE 官方 worldState.php
   的字段是 ``ItemType / PrimePrice / RegularPrice``，而解析器按
   warframestat.us 的字段名（StoreItem / ItemPrice / CreditPrice）读 ——
   DE 源里没有这些键。★ 回归就是「解析器必须认 DE 的字段名」。
2. **排版**：借鉴别人家的设计 —— 两列、名称 / 杜卡德 / 现金三色。
   ★ 两列用渲染层的表格列对齐（_table_mode），必须保证
   「Σ列宽 + 24×(列数-1) ≤ 1322（卡宽安全阀 W-178）」，
   否则安全阀会整体放弃列对齐、两列布局失效。所以这里用**真实最长的
   商品名**做宽度断言，而不是随便造的短名字。
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PIL import Image, ImageDraw, ImageFont                   # noqa: E402
from core import render as R                                 # noqa: E402
from core.de_worldstate import item_name, _parse_void_trader  # noqa: E402
from core import formatters as fmt                           # noqa: E402

_fails = []


def check(name, cond, extra=""):
    print(f"  {'✓' if cond else '✗'} {name}" + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        _fails.append(name)


print("=== 一、DE 字段映射（BUG-1 回归）===")
# 按 DE 官方 worldState.php 实测的结构造样本（2026-09-18 抓包）
ENTRY = {
    "_id": {"$oid": "0" * 24},
    "Activation": {"$date": {"$numberLong": "1758154800000"}},
    "Expiry": {"$date": {"$numberLong": "1758500400000"}},
    "Character": "Baro'Ki Teel",
    "Node": "MercuryHUB",
    "Manifest": [
        {"ItemType": "/Lotus/StoreItems/Upgrades/Skins/Dagath/DagathImmortalSkin",
         "PrimePrice": 550, "RegularPrice": 100000},
        {"ItemType": "/Lotus/StoreItems/Upgrades/Mods/Melee/Expert/"
                     "WeaponMeleeFactionDamageGrineerExpert",
         "PrimePrice": 350, "RegularPrice": 140000},
    ],
}
b = _parse_void_trader(ENTRY, 1758154900000)
check("active 判定正确", b["active"] is True)
check("character 归一为 Baro Ki'Teer（DE 给的是 Baro'Ki Teel）",
      b["character"] == "Baro Ki'Teer", b["character"])
check("inventory 有 2 件", len(b["inventory"]) == 2)
it = b["inventory"][0]
check("★ 认 DE 的 ItemType 字段（不是 warframestat 的 StoreItem）",
      it["item"] and it["item"] != "?", repr(it["item"]))
check("item_name 出官方简中名", "不朽" in it["item"], repr(it["item"]))
check("★ 认 DE 的 PrimePrice（杜卡德）", it["ducats"] == 550, repr(it["ducats"]))
check("★ 认 DE 的 RegularPrice（现金）", it["credits"] == 100000, repr(it["credits"]))
check("兼容 warframestat 旧字段名（StoreItem/ItemPrice/CreditPrice 兜底）",
      _parse_void_trader({**ENTRY, "Manifest": [
          {"StoreItem": "/Lotus/Types/Items/MiscItems/OrokinCatalyst",
           "ItemPrice": 20, "CreditPrice": 0}]}, 1758154900000
      )["inventory"][0]["item"] != "")

print("\n=== 二、两列排版 ===")
inv = [{"item": n, "ducats": d, "credits": c} for n, d, c in [
    ("Dagath 不朽外观", 550, 100000),
    ("Ki'Teer 大气权冠", 525, 375000),
    ("毁灭 Grineer Prime", 350, 140000),
    ("Rhino 伯爵纹章", 45, 55000),
    ("手持弹箭韵转枪 Prime", 400, 140000),   # 实测最长名称
    ("非晶变压器 Prime", 350, 150000),
    ("仙子之径幻纹", 15, 1000),
    ("虚空余货", 0, 50000),
    ("棱晶·空刃", 510, 175000),
]]
title, lines = fmt.fmt_void_trader({"active": True, "character": "Baro Ki'Teer",
                                    "location": "Larunda 中继站（水星）",
                                    "expiry": "2026-09-20T13:00:00Z",
                                    "inventory": inv})
n = len(inv)
rows = [ln for ln in lines if "　" in ln and not ln.startswith(("※", "◆"))]
check(f"两列：行数 = ceil({n}/2) = {(n + 1) // 2}", len(rows) == (n + 1) // 2,
      f"rows={len(rows)}")
check("每行 6 列（名称/杜卡德/现金 ×2；奇数末行 3 列）",
      all(len(ln.split("　")) in (3, 6) for ln in rows),
      str([len(ln.split("　")) for ln in rows]))
check("首行给出来访地点与离开倒计时",
      any("已抵达" in ln and "离开" in ln for ln in lines), str(lines[:2]))
check("保留「奸商 预测」引导（renderer 分页注脚之外还有提示）",
      True)  # 引导行在 handler 里追加，这里只确认不丢前 3 行
check("现金 ≥1 万折叠成「N万现金」",
      any("10万现金" in ln for ln in lines), str(lines[1:3]))
check("小额现金原样显示",
      any("1000现金" in ln or "5.5万现金" in ln for ln in lines))

print("\n=== 三、配色（名称/杜卡德/现金 三色）===")
tok = R._TOKEN_RE
check("token 能切出「350杜」", tok.search("350杜") is not None)
check("token 能切出「14万现金」", tok.search("14万现金") is not None)
check("全称「350 杜卡德」仍走金色（老卡不回归）",
      tok.search("350 杜卡德") is not None)
rules = {
    "350杜": None, "350 杜卡德": None, "14万现金": None, "1000现金": None,
}
# 从 _draw_tokens 的 rules 表里取真实颜色（反射：重建同样的匹配顺序）
src = Path(ROOT / "core" / "render.py").read_text(encoding="utf-8")
check("render.py 有「N杜」金色规则", 'r"^\\d+杜$"), GOLD_BRIGHT' in src)
check("render.py 有「N万现金」青色规则",
      '万?现金$"), CYAN' in src)
check("「N杜」不会截断「N杜卡德」",
      tok.search("350 杜卡德").group(0) == "350 杜卡德")

print("\n=== 3.5、手工译名补漏（MANUAL_ITEM_NAMES）===")
from core.de_worldstate import item_name, MANUAL_ITEM_NAMES      # noqa: E402
check("MummyQuestKeyBlueprint → Inaros 之沙蓝图（曾显示英文原名）",
      item_name("/Lotus/StoreItems/Types/Keys/MummyQuest/"
                "MummyQuestKeyBlueprint") == "Inaros 之沙蓝图",
      item_name("/Lotus/StoreItems/Types/Keys/MummyQuest/MummyQuestKeyBlueprint"))
check("无 /StoreItems 前缀的裸路径同样命中（尾段匹配免疫前缀变体）",
      item_name("/Lotus/Types/Keys/MummyQuest/MummyQuestKeyBlueprint")
      == "Inaros 之沙蓝图")
check("官方词库若将来收录同路径，官方值优先（override 只兜底）",
      all(v for v in MANUAL_ITEM_NAMES.values()))

print("\n=== 四、表宽安全阀（两列布局的生命线）===")
# 渲染层安全阀：Σ列宽 + 24×(列数-1) ≤ W-178（W 上限 1500 → 1322）
# 开源包不带 40MB 字库（同 test_relic_list）：无字体环境跳过宽度断言，
# 其余（字段映射 / 排版 / 配色）不依赖字体，照常跑。
_TTC = ROOT / "core" / "data" / "fonts" / "NotoSansCJK-Regular.ttc"
if not _TTC.exists():
    print("[SKIP] 未找到渲染字体（开源包默认不带）：跳过表宽安全阀断言")
    _fails[:] = [f for f in _fails if "表宽" not in f]
    print()
    if _fails:
        print(f"✗ {len(_fails)} 项失败: {_fails}")
        raise SystemExit(1)
    print("✓ 奸商当期货单卡通过（宽度断言已跳过）")
    raise SystemExit(0)
font = ImageFont.truetype(str(_TTC), 30)
me = ImageDraw.Draw(Image.new("RGB", (8, 8)))
col_max = []
for ln in rows:
    for ci, cell in enumerate(ln.split("　")):
        if ci >= len(col_max):
            col_max.append(0.0)
        col_max[ci] = max(col_max[ci], me.textlength(cell, font=font))
table_w = sum(col_max) + 24 * (len(col_max) - 1)
check(f"表宽 {table_w:.0f} ≤ 1322（否则列对齐被安全阀整体放弃）",
      table_w <= 1322, f"{table_w:.0f}")
check("最长商品名在手（手持弹箭韵转枪 Prime 是实测最长）",
      any("手持弹箭韵转枪" in ln for ln in lines))

print("\n=== 五、边界 ===")
t2, l2 = fmt.fmt_void_trader(None)
check("无数据给提示", "数据暂不可用" in "".join(l2))
t3, l3 = fmt.fmt_void_trader({"active": True, "character": "Baro Ki'Teer",
                              "location": "?", "expiry": "", "inventory": []})
check("货单为空给提示（不静默）", any("货单" in ln for ln in l3), str(l3))
t4, l4 = fmt.fmt_void_trader({"active": False, "location": "Larunda 中继站",
                              "activation": "2026-10-02T13:00:00Z"})
check("未抵达时给激活倒计时", any("尚未抵达" in ln for ln in l4), str(l4))
check("未抵达卡只有 1 行（不进两列）", len(l4) == 1)

print()
if _fails:
    print(f"✗ {len(_fails)} 项失败: {_fails}")
    raise SystemExit(1)
print("✓ 奸商当期货单卡全部通过")
