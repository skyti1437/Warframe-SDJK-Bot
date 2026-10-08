# -*- coding: utf-8 -*-
"""wr 紫卡拍卖卡：**离线挂单默认列出** + **显示上架时长**（2026-10-08 用户口径）。

背景：旧行为里，只有「完全匹配词条的在线单为 0」时才走 offldine 兜底 ⇒
「伯斯顿 + 弱点暴击几率」那种「唯一一张还离线」的场景直接空卡（用户实测）。
现口径：默认（recent/在线）= 全部状态、靠渲染层「在线优先」排序；只有显式「最新」才只留游戏中。
本测试钉住：离线单在列表里、在线档仍排在前面、行内含上架时长、best 取在线那张、空池有明确文案。
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import formatters as F  # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = ""):
    print(("[PASS] " if cond else "[FAIL] ") + name + (f"  -> {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


def _auc(status: str, price: int, age_hours: float) -> dict:
    return {
        "id": f"{status}-{price}",
        "buyout_price": price,
        "starting_price": price,
        "created": (datetime.now(timezone.utc) - timedelta(hours=age_hours)).isoformat(),
        "owner": {"status": status, "ingame_name": f"u_{status}", "reputation": 0},
        "item": {
            "type": "riven",
            "mod_rank": 8,
            "attributes": [{"url_name": "multishot", "value": 150, "positive": True}],
        },
    }


pool = [_auc("offline", 50, 30), _auc("ingame", 200, 5)]
title, lines, best = F.fmt_wr_auctions("伯斯顿", pool, page=1, page_size=8)
body = "\n".join(lines)
check("离线挂单也出现在列表里（不再被藏起来）", "⚫离线" in body, body[:300])
check("在线档仍排在离线之前（在线优先排序不变）", body.index("🟢在线") < body.index("⚫离线"), body[:300])
check("行内含上架时长（s/m/h 口径，放行尾）", ("上架30h" in body) or ("上架5h" in body), body[:300])
check("best = 排序后的第一条（在线那张）", (best or {}).get("owner", {}).get("status") == "ingame", str(best)[:140])
check("空池给明确文案", F.fmt_wr_auctions("伯斯顿", [])[1] == ["没有符合条件的紫卡挂单"])

# 竞价型（无 buyout）与一口价：价格前「起」标记（2026-10-08 用户口径「也打开吧」）
def _auc2(status: str, buyout, start):
    return {
        "id": f"b-{status}-{start}",
        "buyout_price": buyout,
        "starting_price": start,
        "created": (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),
        "owner": {"status": status, "ingame_name": "u", "reputation": 0},
        "item": {"type": "riven", "mod_rank": 8,
                 "attributes": [{"url_name": "viral", "value": 100, "positive": True}]},
    }


_t2, _l2, _ = F.fmt_wr_auctions("盗贼", [_auc2("offline", None, 1800), _auc2("ingame", 2200, 2200)])
_b2 = "\n".join(_l2)
check(
    "竞价型挂单也列出：一口价显示 ♾（不带变体选择符 U+FE0F）+ 起拍价单列",
    "一口价：\u267e" in _b2 and "\ufe0f" not in _b2 and "起拍价：1800p" in _b2,
    _b2[:320],
)
check("一口价挂单写「一口价：Np」且无起拍价段", "一口价：2200p" in _b2 and "起拍价" not in _b2.split("一口价：2200p")[1][:40], _b2[:320])

print()
if FAILED:
    print(f"✗ {len(FAILED)} 项失败：{FAILED}")
    raise SystemExit(1)
print("✓ wr 离线挂单与上架时长全部通过")
