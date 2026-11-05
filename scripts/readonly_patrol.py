# -*- coding: utf-8 -*-
"""只读仓库巡检：静态种子结构与周期基线。

仅检查随包效价轮换骨架、言录使静态候选目录以及 EXPECTED 周期口径。
不联网、不刷新、不写文件；不据此宣称运行期数据健康。
退出码：0 = 静态检查通过；1 = 静态结构或周期口径异常。
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))


# 周期口径基准（以 wiki 实时页面为准；若 wiki 变了，**先人工确认**再改这里）
EXPECTED = {
    "tenet": {"period_hours": 96, "reset_weekday": None, "note": "wiki：每 4 天 00:00 UTC"},
    "coda": {"period_hours": 96, "reset_weekday": None, "note": "wiki：每 4 天 00:00 UTC"},
    "incarnon": {"period_hours": 168, "reset_weekday": None, "note": "周常，周一 00:00 UTC"},
    "acrichis": {
        "period_hours": 168,
        "reset_weekday": 0,
        "note": "wiki《Acrithis》：Resets every Monday 0:00 UTC",
    },
}


def main() -> int:
    from core.data_registry import acrithis_seed_problems, valence_seed_problems

    now = datetime.now(timezone.utc)
    lines: list[str] = []
    todo: list[str] = []

    lines.append(
        f"只读巡检 · {now.isoformat()}  (北京时间 "
        f"{now.astimezone(timezone.utc).astimezone().strftime('%H:%M')} 附近)"
    )
    lines.append("")

    rot = {}
    for label, path, validate in (
        ("效价静态轮换骨架", ROOT / "core/data/rotations.json", valence_seed_problems),
        ("言录使静态候选目录", ROOT / "core/data/de/acrichis_week.json", acrithis_seed_problems),
    ):
        lines.append(f"【静态种子】{label}")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            problems = validate(data)
            if label == "效价静态轮换骨架" and isinstance(data, dict):
                rot = data
        except (OSError, ValueError) as exc:
            problems = [f"{label}不可读/损坏：{exc}"]
        if problems:
            lines.extend(f"    ⚠ {problem}" for problem in problems)
            todo.extend(problems)
        else:
            lines.append("    ✓ 静态结构合法；动态值由运行期获取，本检查不判断运行期健康")
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
            lines.append(
                f"    ✓ {key:8s} period={got_p}h reset_weekday={got_w}（{exp_conf['note']}）"
            )
        else:
            lines.append(
                f"    ⚠ {key:8s} period={got_p}h reset_weekday={got_w}"
                f"，基准 period={exp_conf['period_hours']}h "
                f"reset_weekday={exp_conf['reset_weekday']}（{exp_conf['note']}）"
            )
            todo.append(f"{key} 周期口径与基准不一致 → 先抓 wiki 确认，再决定改数据还是改基准")

    lines.append("")
    lines.append("【结论】")
    if todo:
        for i, t in enumerate(todo, 1):
            lines.append(f"    {i}. {t}")
    else:
        lines.append("    仓库静态种子与周期基线通过；运行期健康需另查")

    print("\n".join(lines))
    return 1 if todo else 0


if __name__ == "__main__":
    sys.exit(main())
