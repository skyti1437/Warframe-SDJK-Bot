# -*- coding: utf-8 -*-
"""伤害计算卡：离线渲染探针（不走 AstrBot）。

用法：python tests/probe_damage_card.py
产出：runtime/probe/damage_*.png
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import damage_calc as dc            # noqa: E402
from core import render as R                  # noqa: E402
from core.damage_calc import card_lines       # noqa: E402

OUT = ROOT / "runtime" / "probe"
OUT.mkdir(parents=True, exist_ok=True)

CASES = [
    ("近战：架势 + 异况超量 + 急进猛突/创口溃烂（连击120）",
     ["Skana", "对", "重机枪手", "100级", "钢铁凤凰", "异况超量", "急进猛突",
      "创口溃烂", "连击120"]),
    ("近战：加一张条件触发卡（狂暴，On Kill）",
     ["Skana", "对", "重机枪手", "100级", "钢铁凤凰", "嗜血", "狂暴"]),
    ("① 无关键延迟（暴率 38%）",
     "绝路p 半自动步枪炮轰 镀层分裂膛室 暴风使者 地狱火 弱点感应 锁定目标 膛线".split()),
    ("② +关键延迟（暴率 238%）—— 比 ① 只多这张卡",
     "绝路p 半自动步枪炮轰 镀层分裂膛室 暴风使者 地狱火 关键延迟 弱点感应 锁定目标 膛线".split()),
    ("整串配卡 + 指定敌人（重机枪手）",
     "绝路p 对 重机枪手 100级 半自动步枪炮轰 镀层分裂膛室 暴风使者 地狱火 关键延迟 弱点感应 锁定目标 膛线".split()),
    ("异常稳态：高触发 + 病毒 10 层",
     ["Soma", "对", "重机枪手", "100级", "触发150", "毒90", "冰90"]),
    ("近战重击：连击 120（×7.0）",
     ["Skana", "对", "重机枪手", "100级", "触发100", "重击", "连击120"]),
    ("对超宏（磁力拉满）+ 超宏池",
     ["Soma", "对", "重机枪手", "100级", "毒90", "冰90", "磁力10", "超宏", "超宏5000"]),
    ("Sentient 适应 2 层",
     ["Soma", "对", "重机枪手", "100级", "毒90", "冰90", "适应2"]),
    ("物理MOD（切割90）+ 未知词兜底",
     ["绝路p", "G系", "100级", "切90", "这张卡不存在"]),
    ("船员（无护甲、有护盾）+ MOD 名",
     ["Soma", "对", "船员", "60级", "膛线", "致命一击", "弱点感应"]),
]

renderer = R.ImageRenderer(OUT)
for idx, (label, toks) in enumerate(CASES, 1):
    toks = [t for t in toks if t and "?" not in t]
    spec, name_tokens = dc.parse_args(toks)
    weapon, alts = dc.find_weapon(" ".join(name_tokens))
    if weapon is None:
        res = dc.calculate(spec, None)      # 走「武器没找到」的错误卡路径
        lines = card_lines(None, spec, res, None)
    else:
        res = dc.calculate(spec, weapon)
        lines = card_lines(weapon, spec, res, alts)
    path = renderer.render("伤害计算", lines)
    # renderer 的 cache 只保留最近 2 张（_cleanup(keep=2)），探针自己留一份稳定命名的
    if path:
        stable = OUT / f"damage_{idx:02d}.png"
        stable.write_bytes(Path(path).read_bytes())
        path = str(stable)
    print(f"[{label}] → {path}")
    for ln in lines:
        print("   ", ln)
    print()
