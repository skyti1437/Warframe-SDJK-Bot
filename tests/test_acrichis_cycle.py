# -*- coding: utf-8 -*-
"""言录使（Acrithis）周常周期 / 时区 / 过期提示的守卫（python3 tests/test_acrichis_cycle.py）

背景（2026-09-20 用户指出 + wiki 核对）：
1. 每个系统的重置点**并不相同**，不能共用一个「周常 = 周一」：
   · 常规周常（Nightwave / Circuit / Netracells / Teshin / Yonta / Cavalero）= 周一 00:00 UTC
   · **Acrithis（言录使）= 周日 00:00 UTC**（DE Update 33.0 补丁说明 +
     wiki Reset 页「Acrithis weekly offerings reset on Sunday 0:00 UTC」）
   · Sortie = 每日 17:00 UTC
2. 轮换**判定用 UTC**，但**展示给国内玩家要换算北京时间**并标注，
   否则「周日 00:00」会被读成北京时间（差 8 小时）。
3. 本周实际 5 件 DE 不下发（只有候选池），过期时**必须明说**是候选池，
   不能静默回落到池让人误以为那就是本周在卖的。
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
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
# 1. 通用周重置：weekday 规则正确（言录使=周日，常规周常=周一）
# ---------------------------------------------------------------------------
sun_morning = datetime(2026, 9, 20, 2, 21, tzinfo=timezone.utc)     # 周日 02:21 UTC
mon_noon = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)        # 周一
sat_noon = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)        # 周六

r = iso(NR(sun_morning, weekday=6, hour_utc=0))
check("言录使规则（周日 00:00 UTC）：从周日凌晨算 → 下周日 00:00",
      r == datetime(2026, 9, 27, tzinfo=timezone.utc), r.isoformat())
check("  且时刻是 00:00 UTC", (r.hour, r.minute) == (0, 0))

r = iso(NR(mon_noon, weekday=6, hour_utc=0))
check("言录使：从周一算 → 本周日 00:00（9/27）",
      r == datetime(2026, 9, 27, tzinfo=timezone.utc), r.isoformat())

r = iso(NR(sat_noon, weekday=6, hour_utc=0))
check("言录使：从周六算 → 次日（周日）00:00（9/20）",
      r == datetime(2026, 9, 20, tzinfo=timezone.utc), r.isoformat())

# ★ 与常规周常（周一）必须不同 —— 这是原来会踩的坑。
#   基准日取**周四**（周日/周一都还没到，两边都是「下一次」，恰好差 1 天）
thu = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)            # 周四
r_mon = iso(NR(thu, weekday=0, hour_utc=0))     # 常规周常 = 周一 → 9/21
r_sun = iso(NR(thu, weekday=6, hour_utc=0))     # 言录使 = 周日 → 9/20
check("★ 言录使（周日）≠ 常规周常（周一），两者相差 1 天",
      (r_mon - r_sun) == timedelta(days=1),
      f"周一={r_mon.date()} 周日={r_sun.date()}")
check("  常规周常落在周一", r_mon.weekday() == 0, str(r_mon.date()))
check("  言录使落在周日", r_sun.weekday() == 6, str(r_sun.date()))

# 时区偏移：规则按时区判定后换回 UTC
check("带时区偏移时结果仍是合法 UTC 时刻", r.tzinfo is not None, str(r))

# ---------------------------------------------------------------------------
# 2. 展示口径：判定用 UTC，展示换算北京时间
# ---------------------------------------------------------------------------
check("★ UTC 00:00 → 北京时间 08:00",
      _to_bj("2026-09-27T00:00:00+00:00") == "09-27 08:00",
      _to_bj("2026-09-27T00:00:00+00:00"))
check("跨日换算正确（UTC 22:00 → 次日 06:00）",
      _to_bj("2026-09-26T22:00:00+00:00") == "09-27 06:00",
      _to_bj("2026-09-26T22:00:00+00:00"))
check("坏串返回空而不是抛异常", _to_bj("not-a-time") == "")

# ---------------------------------------------------------------------------
# 3. 数据：rotations.json 的 acrichis 段
# ---------------------------------------------------------------------------
rot = json.loads((ROOT / "core" / "data" / "rotations.json").read_text(encoding="utf-8"))
sec = rot.get("acrichis") or {}
check("acrichis.period_hours == 168（每周，不是 24）",
      sec.get("period_hours") == 168, str(sec.get("period_hours")))
check("acrichis.pick == 5（一次 5 件）", sec.get("pick") == 5, str(sec.get("pick")))
check("★ reset_weekday == 6（周日，DE 官方口径）",
      sec.get("reset_weekday") == 6, str(sec.get("reset_weekday")))
check("reset_hour_utc == 0（00:00 UTC）",
      sec.get("reset_hour_utc") == 0, str(sec.get("reset_hour_utc")))
check("★ 已简化为单一规则（不再有 intl/cn 双套 reset）",
      not isinstance(sec.get("reset"), dict), str(type(sec.get("reset"))))
check("记了官方来源", "warframe.com" in (sec.get("_source") or ""),
      str(sec.get("_source")))
check("epoch 锚点是周日 00:00 UTC",
      iso(sec["epoch"]).weekday() == 6 and iso(sec["epoch"]).hour == 0,
      str(sec.get("epoch")))

# ---------------------------------------------------------------------------
# 4. 过期判定与卡面提示
# ---------------------------------------------------------------------------
week_file = ROOT / "core" / "data" / "de" / "acrichis_week.json"
wk = json.loads(week_file.read_text(encoding="utf-8"))
check("本周货单 expiry 晚于现在（未过期）",
      iso(wk["expiry"]) > datetime.now(timezone.utc), wk["expiry"])
check("expiry 标注了 UTC", str(wk["expiry"]).endswith("+00:00"), wk["expiry"])

wk2 = dict(wk)
wk2["next_reset"] = NR(datetime.now(timezone.utc), weekday=6, hour_utc=0)
title, lines = fmt_acrichis_week(wk2)
check("本周货单卡面带下次刷新倒计时", any("距下次刷新" in ln for ln in lines), str(lines))
check("★ 卡面标注了北京时间（避免与 UTC 混淆）",
      any("北京时间" in ln for ln in lines), str(lines))
check("卡面说明轮换是周日而非周一",
      any("周日" in ln for ln in lines), str(lines))

# 过期 → 必须标注是候选池
title2, lines2 = fmt_acrichis({}, stale=True)
check("★ 过期时标题标注「货单待更新」", "货单待更新" in title2, title2)
check("过期时正文说明上面只是候选池",
      any("已过期未更新" in ln for ln in lines2), str(lines2))
title3, lines3 = fmt_acrichis({}, stale=False)
check("未过期时不加待更新字样", "货单待更新" not in title3, title3)

print()
if FAILED:
    print(f"✗ {len(FAILED)} 项失败：" + "、".join(FAILED))
    sys.exit(1)
print("✓ 言录使周期与时区守卫全部通过")
