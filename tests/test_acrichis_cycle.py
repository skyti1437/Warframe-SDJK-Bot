# -*- coding: utf-8 -*-
"""言录使（Acrithis）周常周期 / 时区 / 过期提示的守卫（python3 tests/test_acrichis_cycle.py）

背景（2026-09-20 用户两次纠正 + wiki 实时核对）：
1. 重置点以 **wiki 实时页面**为准：wiki《Acrithis》现在写的是
   「Weekly Rotation (5 of below) **Resets every Monday 0:00 UTC**」，
   页面上的实时倒计时也对得上；《Reset》总表同，且 Update 32.3 起各商人周常
   统一到周一（Palladino / Chipper / Teshin 等）。
   ⚠️ **不要引用** DE《Update 33.0》那句「rotating at Sundays at 00:00 UTC」——
   那是 2023 年初版、已被后续调整覆盖（我一度按它写成周日，是错的）。
2. 轮换判定用 **UTC**，展示给国内玩家要换算**北京时间**并标注。
3. 本周实际 5 件 DE 不下发（只有候选池），过期时**必须明说**。
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.api_client import WarframeClient  # noqa: E402
from core.formatters import _to_bj, fmt_acrichis, fmt_acrichis_week  # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = ""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -> {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


NR = WarframeClient.next_weekly_reset


def iso(s: str) -> datetime:
    return datetime.fromisoformat(s)


# ---------------------------------------------------------------------------
# 1. 周重置：周一 00:00 UTC（与 wiki 口径一致）
# ---------------------------------------------------------------------------
sun_0221 = datetime(2026, 9, 20, 2, 21, tzinfo=timezone.utc)     # 周日 02:21 UTC
mon_noon = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)     # 周一
sat_noon = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)     # 周六

r = iso(NR(sun_0221, weekday=0, hour_utc=0))
check("★ 从周日算 → 次日（周一）00:00 UTC",
      r == datetime(2026, 9, 21, tzinfo=timezone.utc), r.isoformat())
check("  且时刻是 00:00 UTC", (r.hour, r.minute) == (0, 0))
check("  ★ 距该时刻约 21.7 小时（与 wiki 页面倒计时吻合）",
      abs((r - sun_0221).total_seconds() / 3600 - 21.65) < 1.0,
      f"{(r - sun_0221).total_seconds()/3600:.2f} 小时")

r_mon = iso(NR(mon_noon, weekday=0, hour_utc=0))
check("从周一中午算 → 下周一 00:00（9/28）",
      r_mon == datetime(2026, 9, 28, tzinfo=timezone.utc), r_mon.isoformat())

r_sat = iso(NR(sat_noon, weekday=0, hour_utc=0))
check("从周六算 → 下周一 00:00（9/21）",
      r_sat == datetime(2026, 9, 21, tzinfo=timezone.utc), r_sat.isoformat())
check("三种基准算出的结果都落在周一",
      all(x.weekday() == 0 for x in (r, r_mon, r_sat)),
      str([x.weekday() for x in (r, r_mon, r_sat)]))

# ---------------------------------------------------------------------------
# 2. 展示口径：UTC 判定 → 北京时间展示
# ---------------------------------------------------------------------------
check("★ UTC 周一 00:00 → 北京时间 周一 08:00",
      _to_bj("2026-09-21T00:00:00+00:00") == "09-21 08:00",
      _to_bj("2026-09-21T00:00:00+00:00"))
check("跨日换算正确（UTC 22:00 → 次日 06:00）",
      _to_bj("2026-09-20T22:00:00+00:00") == "09-21 06:00",
      _to_bj("2026-09-20T22:00:00+00:00"))
check("坏串返回空而不是抛异常", _to_bj("not-a-time") == "")

# ---------------------------------------------------------------------------
# 3. 数据：rotations.json 各段周期与 wiki 对照
# ---------------------------------------------------------------------------
rot = json.loads((ROOT / "core" / "data" / "rotations.json").read_text(encoding="utf-8"))
sec = rot.get("acrichis") or {}
check("acrichis.period_hours == 168（每周，不是 24）",
      sec.get("period_hours") == 168, str(sec.get("period_hours")))
check("acrichis.pick == 5（一次 5 件）", sec.get("pick") == 5, str(sec.get("pick")))
check("★ reset_weekday == 0（周一，wiki 实时口径）",
      sec.get("reset_weekday") == 0, str(sec.get("reset_weekday")))
check("reset_hour_utc == 0（00:00 UTC）",
      sec.get("reset_hour_utc") == 0, str(sec.get("reset_hour_utc")))
check("★ 已简化为单一规则（不再有 intl/cn 双套 reset）",
      not isinstance(sec.get("reset"), dict), str(type(sec.get("reset"))))
check("来源指向 wiki（不是过时的 33.0 补丁说明）",
      "wiki.warframe.com" in (sec.get("_source") or ""), str(sec.get("_source")))
check("注释点明了 33.0 的 Sunday 说法已过时",
      "33.0" in (sec.get("_note") or "") and "Sunday" in (sec.get("_note") or ""))

check("tenet.period_hours == 96（wiki：每 4 天 0:00 UTC）",
      (rot.get("tenet") or {}).get("period_hours") == 96,
      str((rot.get("tenet") or {}).get("period_hours")))
check("coda.period_hours == 96（同上）",
      (rot.get("coda") or {}).get("period_hours") == 96,
      str((rot.get("coda") or {}).get("period_hours")))
check("incarnon.period_hours == 168（周常）",
      (rot.get("incarnon") or {}).get("period_hours") == 168,
      str((rot.get("incarnon") or {}).get("period_hours")))

# ---------------------------------------------------------------------------
# 4. 本周货单：expiry 落在周一、未过期
# ---------------------------------------------------------------------------
wk = json.loads((ROOT / "core" / "data" / "de" / "acrichis_week.json").read_text(encoding="utf-8"))
exp = iso(wk["expiry"])
check("★ 货单 expiry 落在周一 00:00 UTC",
      exp.weekday() == 0 and (exp.hour, exp.minute) == (0, 0), exp.isoformat())
check("expiry 标注了 UTC", str(wk["expiry"]).endswith("+00:00"), wk["expiry"])
check("货单有 5 件", len(wk.get("items") or []) == 5, str(len(wk.get("items") or [])))

# ---------------------------------------------------------------------------
# 5. 卡面
# ---------------------------------------------------------------------------
wk2 = dict(wk)
wk2["next_reset"] = NR(datetime.now(timezone.utc), weekday=0, hour_utc=0)
title, lines = fmt_acrichis_week(wk2)
check("本周货单卡面带下次刷新倒计时", any("距下次刷新" in ln for ln in lines), str(lines))
check("★ 卡面标注了北京时间", any("北京时间" in ln for ln in lines), str(lines))
check("★ 卡面说明轮换是周一 00:00 UTC",
      any("周一" in ln and "UTC" in ln for ln in lines), str(lines))
check("卡面不再出现「周日」", not any("周日" in ln for ln in lines), str(lines))

title2, lines2 = fmt_acrichis({}, stale=True)
check("★ 过期时标题标注「货单待更新」", "货单待更新" in title2, title2)
check("过期时正文说明已过期未更新",
      any("已过期未更新" in ln for ln in lines2), str(lines2))
title3, _ = fmt_acrichis({}, stale=False)
check("未过期时不加待更新字样", "货单待更新" not in title3, title3)

print()
if FAILED:
    print(f"✗ {len(FAILED)} 项失败：" + "、".join(FAILED))
    sys.exit(1)
print("✓ 言录使周期与时区守卫全部通过")
