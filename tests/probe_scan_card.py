# -*- coding: utf-8 -*-
"""离线渲染「识卡」结果卡（用 venv 的 python 跑，managed python 没 PIL）。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import loadout_ocr as lo     # noqa: E402
from core import render as R           # noqa: E402

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

out = ROOT / "runtime/probe"
out.mkdir(parents=True, exist_ok=True)
renderer = R.ImageRenderer(out)

an = lo.analyze(OCR)
lines = lo.card_lines(an)
path = renderer.render("配卡识别", lines)
print("→", path)
for ln in lines:
    print("   ", ln)

# 第二张：认不全的情形（验证 warning 分支的排版）
an2 = lo.analyze({"weapon": "驱魔之刃",
                  "mods": [{"name": "熔岩冲击", "drain": "11"},
                           {"name": "宇宙无敌卡", "drain": "5"}]})
lines2 = lo.card_lines(an2)
path2 = renderer.render("配卡识别", lines2)
print("\n→", path2)
for ln in lines2:
    print("   ", ln)
