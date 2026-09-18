# -*- coding: utf-8 -*-
"""排查「世界状态」「警报」在冒烟里显示空/异常的原因。

区分三种可能：
  1. DE 直连源本身不含该表（数据源能力问题，非 bug）
  2. 本日确实无该内容（如无警报、无活动）
  3. 解析/取数代码有 bug（真问题）
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.api_client import WarframeClient  # noqa: E402


async def main():
    c = WarframeClient(timeout=25.0)
    raw = await c._de_raw()
    print("DE 原始 worldstate 类型:", type(raw).__name__)
    if isinstance(raw, dict):
        print("总键数:", len(raw))
        print("键名:", sorted(raw.keys()))
        for k in ("Alerts", "Events", "News", "Sorties", "Invasions",
                  "VoidFissures", "Goals", "ActiveMissions"):
            if k in raw:
                v = raw[k]
                print(f"  {k}: {len(v) if hasattr(v, '__len__') else v}")
            else:
                print(f"  {k}: <键不存在>")

    print()
    print("--- 解析后 bundle ---")
    bundle = await c._de_bundle()
    print("bundle 键数:", len(bundle))
    for k in ("alerts", "goals", "events", "news", "invasions", "fissures"):
        v = bundle.get(k)
        n = len(v) if hasattr(v, "__len__") else v
        print(f"  {k}: {type(v).__name__} n={n}")

    print()
    print("--- 插件层取数 ---")
    for name in ("alerts", "invasions", "news", "events"):
        fn = getattr(c, name, None)
        if not fn:
            continue
        try:
            r = await fn("pc")
            n = len(r) if hasattr(r, "__len__") else r
            print(f"  {name}: {type(r).__name__} n={n}")
        except Exception as e:
            print(f"  {name}: ERR {type(e).__name__}: {e}")


asyncio.run(main())
