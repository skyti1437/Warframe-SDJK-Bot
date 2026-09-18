# -*- coding: utf-8 -*-
"""诊断帮助卡的列对齐为何失效（列内折行 vs 整行折行的口径不一致）。

背景：帮助卡在 2.0 改版后被评「变丑了」。肉眼可见：
  · 有些行的说明列对得齐，有些整行顶格、说明跑到最左边
  · 长说明被 `_wrap` 折断后，续行不缩进、直接贴左边框

机制怀疑：`_plan_desc_col_wrap` 判定「放得下、不用折」（预算 w_cap-reserve），
而逐行 `_wrap` 用的是更窄的 `wrap_w`，于是行被折断 → 第 779 行
`cells_split = ... if len(wrapped) == 1 else None` 丢掉分列信息 → 顶格绘制。

本脚本把两侧的真实预算与每行的折行结果都打出来，让结论落在数字上。
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import render as R  # noqa: E402


def load_help_topic() -> dict:
    tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
    for node in tree.body:
        tgt = getattr(node, "target", None)
        if isinstance(tgt, ast.Name) and tgt.id == "HELP_TOPIC":
            return ast.literal_eval(node.value)
    raise SystemExit("没找到 HELP_TOPIC")


topic = load_help_topic()
lines: list[str] = []
for t, items in topic.items():
    lines.append(f"◆ {t}")
    lines += [f"· {c}　{d}" for c, d in items]

# —— 记录关键中间量 ——
log: dict[str, object] = {"wrap": [], "cells": [], "plan": None}

_orig_wrap = R.ImageRenderer._wrap


def spy_wrap(text, font, max_w_px):
    out = _orig_wrap(text, font, max_w_px)
    log["wrap"].append((text, max_w_px, len(out), out))
    return out


R.ImageRenderer._wrap = staticmethod(spy_wrap)

_orig_split = R._split_cells


def spy_split(text):
    out = _orig_split(text)
    log["cells"].append((text, out))
    return out


R._split_cells = spy_split

_orig_plan = R._plan_desc_col_wrap


def spy_plan(cells, col0, wrap_fn, measure_fn, w_cap=1500, reserve=178, gap=24):
    res = _orig_plan(cells, col0, wrap_fn, measure_fn, w_cap, reserve, gap)
    log["plan"] = {
        "n_cells": len(cells), "col0": round(col0, 1),
        "w_cap_minus_reserve": w_cap - reserve, "gap": gap,
        "col1": round(res[1], 1),
        "预算内?": col0 + gap + max((measure_fn(d) for _, _, d in cells),
                                   default=0) <= w_cap - reserve,
        "wrap_map_keys": list(res[0].keys()),
    }
    return res


R._plan_desc_col_wrap = spy_plan

r = R.ImageRenderer(Path(ROOT / "runtime" / "cards"))
if not r.available:
    raise SystemExit("渲染器不可用")
p = r.render("Warframe 查询助手 指令一览", lines, "平台：国际服")

from PIL import Image  # noqa: E402
W = Image.open(p).size[0]
print(f"卡宽 W = {W} px")
print(f"逐行折行预算 wrap_w = W - pad*2 - 56（pad 见 render，约 54）")
print(f"列折行规划：{log['plan']}")
print()

print("=== 被折成多段的行（这些行会丢掉分列信息 → 顶格绘制）===")
multi = [x for x in log["wrap"] if x[2] > 1]  # type: ignore[union-attr]
if not multi:
    print("（无）")
for text, budget, n, out in multi:  # type: ignore[misc]
    print(f"  budget={budget:>5} 段数={n}  原始={text[:46]!r}")
    for seg in out:
        print(f"       续行: {seg!r}")

print()
print("=== 分列行里，第 0 列最宽的几条（决定说明列起点）===")
two = [c for c in log["cells"] if c[1] and len(c[1]) == 2]  # type: ignore[union-attr]
two_sorted = sorted(two, key=lambda x: -len(x[1][0]))[:5]  # type: ignore[index]
for text, cells in two_sorted:  # type: ignore[misc]
    print(f"  第0列 {len(cells[0]):>3} 字 | {cells[0][:44]!r}")

print()
print(f"双列行总数 {len(two)}；被折行的行数 {len(multi)}")
