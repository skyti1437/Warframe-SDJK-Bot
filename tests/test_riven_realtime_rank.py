# -*- coding: utf-8 -*-
"""紫卡实时排行（WM 拍卖）回归：目标武器集 + 实时卡渲染。

背景（维护者 2026-10-23）：「紫卡排行」用的 DE 官方周报时效差 —— 该文件**每周一 00:00 UTC
才更新一次**，1.2 热修后不少武器紫卡暴涨却看不出。故新增 WM 拍卖实时快照，
`紫卡排行` 默认改看实时、周报作 `紫卡排行 周报` 子视图。

钉住三件事：
① 目标集 = 周报热度前 N + 原倾向 ≤0.7，去重、**家族折到母武器**；
   DE 周报的 `compatibility` 是**内部名**（不是 WM url_name），必须经 WM 表解析、解析不到就跳过；
② 实时卡按**挂单数**降序、含「更新于」时间；
③ 快照缺失时给出明确文案（不假装有数据）。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import formatters as F  # noqa: E402
from core.api_client import riven_realtime_targets  # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = ""):
    print(
        ("[PASS] " if cond else "[FAIL] ")
        + name
        + (f"  -> {detail}" if detail and not cond else "")
    )
    if not cond:
        FAILED.append(name)


# 构造：WM 表（含 en/zh/url_name）+ 倾向表（**以英文名为键**）+ 家族表
WM = [
    {"url_name": "acceltra", "en": "Acceltra", "zh": "迅发电浆炮"},
    {"url_name": "acceltra_prime", "en": "Acceltra Prime", "zh": "迅发电浆炮 Prime"},
    {"url_name": "torid", "en": "Torid", "zh": "托里德"},
    {"url_name": "rubico", "en": "Rubico", "zh": "绝路"},
]
DISP = {
    "Acceltra": {"url_name": "acceltra", "zh": "迅发电浆炮", "disposition": 0.65},
    "Acceltra Prime": {"url_name": "acceltra_prime", "zh": "迅发电浆炮 Prime", "disposition": 0.55},
    "Torid": {"url_name": "torid", "zh": "托里德", "disposition": 1.3},
    "Rubico": {"url_name": "rubico", "zh": "绝路", "disposition": 0.95},
}
FAM = {
    "by_name": {"Acceltra": "K", "Acceltra Prime": "K"},
    "families": {"K": ["Acceltra", "Acceltra Prime"]},
}

# ① 周报路：compatibility 用**英文名**（真实表里是内部名，解析不到就跳过 —— 见下一条）
WK = [
    {"compatibility": "Torid", "pop": 90},
    {"compatibility": "Rubico", "pop": 80},
    {"compatibility": "1999InfShotgunWeapon", "pop": 999},  # 内部名 ⇒ 必须跳过
]
t = riven_realtime_targets(WK, WM, DISP, FAM)
check("目标集：周报热度前 N 取到", "torid" in t and "rubico" in t, str(t))
check("目标集：内部名（1999InfShotgunWeapon）被跳过", "1999InfShotgunWeapon" not in t, str(t))
check("目标集：原倾向 ≤0.7 的进来", "acceltra" in t, str(t))
check("目标集：家族折到母武器（不留 acceltra_prime）", "acceltra_prime" not in t, str(t))
check("目标集：倾向 1.3 的托里德只因周报入榜", t.count("torid") == 1, str(t))

t2 = riven_realtime_targets([], WM, DISP, FAM)
check("目标集：只有倾向路 ⇒ 仅 ≤0.7 的 acceltra 入榜", t2 == ["acceltra"], str(t2))

t3 = riven_realtime_targets(WK, WM, DISP, FAM, top_n=1)
check("目标集：top_n=1 只留最热（torid）", t3 == ["acceltra", "torid"], str(t3))

# ② 实时卡渲染：按挂单数降序 + 含更新时间
ROWS = {
    "torid": {"online_low": 120, "low": 5, "median": 130.0, "listings": 40},
    "rubico": {"online_low": 50, "low": 1, "median": 55.0, "listings": 200},
}
title, lines = F.fmt_riven_realtime(
    ROWS, "2026-10-23T07:10:17+00:00", {"torid": "托里德", "rubico": "绝路"}
)
check("实时卡：标题含实时", "实时" in title, title)
check("实时卡：标了更新时间", any("2026-10-23 07:10" in ln for ln in lines), str(lines))
check(
    "实时卡：按挂单数降序（绝路 200 在前）",
    lines.index([x for x in lines if "绝路" in x][0])
    < lines.index([x for x in lines if "托里德" in x][0]),
    str(lines),
)
check("实时卡：给了周报入口提示", any("紫卡排行 周报" in ln for ln in lines), str(lines))

# ③ 快照缺失 ⇒ 明确文案
t4, l4 = F.fmt_riven_realtime({}, "", {})
check("缺快照：明确说明还没有", any("还没有实时快照" in x for x in l4), str(l4))

# ④ 极端：某行字段缺失不崩
t5, l5 = F.fmt_riven_realtime({"x": {}}, "", {})
check("字段缺失不崩", isinstance(l5, list) and len(l5) >= 2, str(l5))

print()
if FAILED:
    print(f"FAILED {len(FAILED)}: " + "、".join(FAILED))
    sys.exit(1)
print("全部通过")
