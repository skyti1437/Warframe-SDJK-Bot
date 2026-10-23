# -*- coding: utf-8 -*-
"""wr 未开紫卡（2026-10-23 玩家报「wr 手枪未开 搜不到」）。

钉住三件事：
① 「类别 + 未开」四种写法（含连写/换序）都能识别；
② **原 wr 行为一字不改**（不带未开词的输入一律不认，别把「未开」误吞成武器名）；
③ `_VEILED_SLUGS` 用 WM v2 的**无括号** slug（旧 `..._(veiled)` 是 v1 形态）。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import formatters as F  # noqa: E402
from core import parser as P  # noqa: E402
from core.api_client import WarframeClient  # noqa: E402
from core.commands.market import MarketCommands  # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = ""):
    print(
        ("[PASS] " if cond else "[FAIL] ")
        + name
        + (f"  -> {detail}" if detail and not cond else "")
    )
    if not cond:
        FAILED.append(name)


M = MarketCommands()


def vq(s: str):
    return M._wr_veiled_query(P.parse(s).content)


print("=== ① 四种写法 ===")
for s in ("wr 手枪未开", "wr 手枪 未开", "wr 未开 手枪", "wr 未开手枪"):
    check(f"识别 {s}", vq(s) == "Pistol", repr(vq(s)))

print("\n=== ② 类别同义词 ===")
for s, want in (
    ("wr 副武未开", "Pistol"),
    ("wr 步枪未开", "Rifle"),
    ("wr 主武未开", "Rifle"),
    ("wr 霰弹未开", "Shotgun"),
    ("wr 霰弹枪未开", "Shotgun"),
    ("wr 近战未开", "Melee"),
    ("wr 曲翼未开", "Archgun"),
    ("wr 空战未开", "Archgun"),
    ("wr 组合枪未开", "Kitgun"),
    ("wr Zaw未开", "Zaw"),
    ("wr 守护未开", "Robotic"),
    ("wr 同伴未开", "Robotic"),
):
    check(f"{s} -> {want}", vq(s) == want, repr(vq(s)))

print("\n=== ③ 未开同义词 ===")
for s in ("wr 手枪未揭示", "wr 手枪未解", "wr 手枪 veiled"):
    check(f"{s} -> Pistol", vq(s) == "Pistol", repr(vq(s)))

print("\n=== ④ 不带类别 ===")
check("wr 未开 -> ''（列 8 类行情）", vq("wr 未开") == "", repr(vq("wr 未开")))
check("wr 未开 带别的词不认", vq("wr 绝路 未开") is None, repr(vq("wr 绝路 未开")))

print("\n=== ⑤ 原 wr 行为不受影响 ===")
for s in (
    "wr 绝路 暴击",
    "wr 绝路",
    "wr 赤毒 布拉玛",
    "wr 手枪",
    "wr 手枪 暴击 负变焦",
    "wr 未开手枪x",
):
    check(f"不认：{s}", vq(s) is None, repr(vq(s)))

print("\n=== ⑥ slug 用无括号 v2 形态 ===")
slugs = WarframeClient._VEILED_SLUGS
check("8 个类别齐", len(slugs) == 8, str(len(slugs)))
bad = {k: v for k, v in slugs.items() if "(" in v or ")" in v}
check("无括号 slug", not bad, str(bad))
check(
    "Pistol slug 正确",
    slugs.get("Pistol") == "pistol_riven_mod_veiled",
    str(slugs.get("Pistol")),
)

print("\n=== ⑦ 无类别总览渲染 ===")
stats = {
    "Pistol": {"median": 12.5, "min": 8, "max": 30, "vol": 100},
    "Rifle": {"median": 20.0, "min": 15, "max": 40, "vol": 50},
}
title, lines = F.fmt_wm_veiled_summary(stats)
check("标题含未开", "未开" in title, title)
check("含手枪行", any("手枪未开紫卡" in x for x in lines), str(lines))
check("含提示", any("wr 手枪未开" in x for x in lines), str(lines))
check("空统计有兜底", "未取到" in F.fmt_wm_veiled_summary({})[1][0], "")

print()
if FAILED:
    print(f"FAILED {len(FAILED)}: " + "、".join(FAILED))
    sys.exit(1)
print("全部通过")
