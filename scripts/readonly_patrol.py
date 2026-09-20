# -*- coding: utf-8 -*-
"""只读巡检：快照新鲜度 + 周期口径（python3 scripts/readonly_patrol.py）

★ 本脚本**只读**：不写任何文件、不 commit、不 push、不部署。
   它只负责"发现问题"，修复一律由人工确认后手工执行。

为什么需要它（与插件内置自动刷新的分工）：
  · 内置刷新（main.py::_valence_autoloop，每 6h）只写**运行期**数据
    （服务器 /AstrBot/data/plugin_data/...），仓库 / 开源包 / 市场包里那份快照
    是另一份，会随发版逐渐过期 —— 没装 FlareSolverr 的用户拿到的就是这份。
  · FlareSolverr 挂掉时内置刷新只记一条 warning，没人盯就会一直静默。

检查项：
  1. 信条 / 终幕效价快照是否落在当前换轮窗口内
  2. 言录使（Acrithis）本周货单是否过期（DE 不下发，只能人工更新）
  3. 各段周期口径是否与 PROJECTED 基准一致（wiki 改过口径而没人察觉的事发生过：
     2026-09-20 Acrithis 由「周日」变「周一」）

退出码：0 = 全部正常；1 = 有需要人工处理项。
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# 周期口径基准（以 wiki 实时页面为准；若 wiki 变了，**先人工确认**再改这里）
EXPECTED = {
    "tenet":    {"period_hours": 96,  "reset_weekday": None, "note": "wiki：每 4 天 00:00 UTC"},
    "coda":     {"period_hours": 96,  "reset_weekday": None, "note": "wiki：每 4 天 00:00 UTC"},
    "incarnon": {"period_hours": 168, "reset_weekday": None, "note": "周常，周一 00:00 UTC"},
    "acrichis": {"period_hours": 168, "reset_weekday": 0,    "note": "wiki《Acrithis》：Resets every Monday 0:00 UTC"},
}


def main() -> int:
    from core.api_client import WarframeClient

    now = datetime.now(timezone.utc)
    lines: list[str] = []
    todo: list[str] = []

    lines.append(f"只读巡检 · {now.isoformat()}  (北京时间 "
                 f"{now.astimezone(timezone.utc).astimezone().strftime('%H:%M')} 附近)")
    lines.append("")

    rot_path = ROOT / "core" / "data" / "rotations.json"
    rot = json.loads(rot_path.read_text(encoding="utf-8"))

    # ---- 1. 效价快照新鲜度 ----
    lines.append("【1】效价快照（仓库那份，会随包发出去）")
    for key in ("tenet", "coda"):
        sec = rot.get(key) or {}
        snap = sec.get("valence_snapshot")
        stale = WarframeClient.valence_is_stale(sec, now)
        flag = "⚠ 已过期" if stale else "✓ 新鲜"
        lines.append(f"    {key:8s} 快照={snap}  {flag}")
        if stale:
            todo.append(f"{key} 效价快照已过期 → 抓 wiki 更新 rotations.json（旧值→新值 需人工核对）")
    lines.append("")

    # ---- 2. 言录使本周货单 ----
    lines.append("【2】言录使（Acrithis）本周货单")
    wk_path = ROOT / "core" / "data" / "de" / "acrichis_week.json"
    try:
        wk = json.loads(wk_path.read_text(encoding="utf-8"))
        exp = datetime.fromisoformat(wk["expiry"])
        left_h = (exp - now).total_seconds() / 3600
        if left_h <= 0:
            lines.append(f"    ⚠ 已过期（expiry={wk['expiry']}，已过 {-left_h:.1f} 小时）")
            lines.append(f"      当前记录 {len(wk.get('items') or [])} 件；DE 不下发本周 5 件，"
                         f"需人工到游戏内/wiki 核对后更新")
            todo.append("言录使本周货单已过期 → 人工更新 core/data/de/acrichis_week.json")
        else:
            lines.append(f"    ✓ 未过期（expiry={wk['expiry']}，剩 {left_h:.1f} 小时）")
            lines.append(f"      下次轮换按规则算：{WarframeClient().acrithis_next_reset()}")
    except Exception as e:  # noqa: BLE001
        lines.append(f"    ⚠ 读取失败：{type(e).__name__}: {e}")
        todo.append("言录使货单文件读取异常 → 人工检查 core/data/de/acrichis_week.json")
    lines.append("")

    # ---- 3. 周期口径 ----
    lines.append("【3】周期口径（wiki 变过口径，这里只核对数据文件是否与基准一致）")
    for key, exp_conf in EXPECTED.items():
        sec = rot.get(key) or {}
        got_p = sec.get("period_hours")
        got_w = sec.get("reset_weekday")
        ok_p = got_p == exp_conf["period_hours"]
        ok_w = (exp_conf["reset_weekday"] is None) or (got_w == exp_conf["reset_weekday"])
        if ok_p and ok_w:
            lines.append(f"    ✓ {key:8s} period={got_p}h reset_weekday={got_w}"
                         f"（{exp_conf['note']}）")
        else:
            lines.append(f"    ⚠ {key:8s} period={got_p}h reset_weekday={got_w}"
                         f"，基准 period={exp_conf['period_hours']}h "
                         f"reset_weekday={exp_conf['reset_weekday']}（{exp_conf['note']}）")
            todo.append(f"{key} 周期口径与基准不一致 → 先抓 wiki 确认，再决定改数据还是改基准")

    lines.append("")
    lines.append("【结论】")
    if todo:
        for i, t in enumerate(todo, 1):
            lines.append(f"    {i}. {t}")
    else:
        lines.append("    全部正常，无需人工处理")

    print("\n".join(lines))
    return 1 if todo else 0


if __name__ == "__main__":
    sys.exit(main())
