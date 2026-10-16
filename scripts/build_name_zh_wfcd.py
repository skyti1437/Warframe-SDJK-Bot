# -*- coding: utf-8 -*-
"""构建期：物品路径 → 官方简中名**补充表** core/data/de/name_zh_wfcd.json。

背景（2026-10-16，警报奖励「RoadToTauEventResource（未收录）」）：
  运行期主名称表 `de/name_zh.json` 取自 DE 官方导出，而**新活动资源**常常还不在导出里
  （实例：《划时代行动：奔赴 Tau 星系》的「月华脯」Moonflesh，ExportResources.json 0 命中）
  ⇒ 警报 / 日历等卡面只能回落「路径末段（未收录）」。
  WFCD warframe-items 的 `data/json/i18n/zh.json` 是同一套官方语言数据的整理版，覆盖更全
  （2026-10-16 实测 19504 条，其中 507 条是 name_zh.json 没有的）。

口径：只收 **name_zh.json 里没有的键**（主表优先、绝不覆盖），值取 i18n 的 `name`（官方简中原文，
     不改写）；键一律小写，与 name_zh.json 同形。
输入：warframe-items 的 `i18n/zh.json`（KB 构建数据 `$WF_KB_DATA/items/i18n/zh.json`，或 --i18n 指定）
输出：core/data/de/name_zh_wfcd.json（随包分发）

用法：python scripts/build_name_zh_wfcd.py [--i18n <zh.json>] [--check]
  --check 只校验「重新生成 == 仓库现有文件」，不写盘。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAIN = ROOT / "core" / "data" / "de" / "name_zh.json"
OUT = ROOT / "core" / "data" / "de" / "name_zh_wfcd.json"


def build(i18n: dict, main: dict) -> dict:
    names = {}
    for path, v in i18n.items():
        nm = (v.get("name") if isinstance(v, dict) else "") or ""
        key = path.strip().lower()
        if nm.strip() and key.startswith("/lotus/") and key not in main:
            names[key] = nm.strip()
    return {
        "_note": (
            "物品路径 → 官方简中名补充表：只收 de/name_zh.json（DE 官方导出）没有的键，"
            "来源 WFCD warframe-items data/json/i18n/zh.json（官方语言数据整理版）。"
            "主表优先、本表不覆盖。重新生成：python scripts/build_name_zh_wfcd.py"
        ),
        "names": dict(sorted(names.items())),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    default = Path(os.environ.get("WF_KB_DATA", "")) / "items" / "i18n" / "zh.json"
    ap.add_argument("--i18n", type=Path, default=default, help="warframe-items 的 i18n/zh.json")
    ap.add_argument("--check", action="store_true", help="只校验不写盘")
    args = ap.parse_args()
    if not args.i18n.is_file():
        print(f"缺少 warframe-items i18n：{args.i18n}（构建机设 WF_KB_DATA 或用 --i18n 指定）")
        return 1
    i18n = json.loads(args.i18n.read_text(encoding="utf-8"))
    main_tbl = json.loads(MAIN.read_text(encoding="utf-8"))
    data = build(i18n, main_tbl)
    text = json.dumps(data, ensure_ascii=False, indent=1) + "\n"
    print(f"i18n {len(i18n)} 条；name_zh {len(main_tbl)} 条；补充 {len(data['names'])} 条")
    if args.check:
        same = OUT.is_file() and OUT.read_text(encoding="utf-8") == text
        print("✓ --check 通过（与仓库一致）" if same else "✗ 与仓库现有文件不一致，需重新生成")
        return 0 if same else 1
    OUT.write_text(text, encoding="utf-8")
    print(f"✓ 已写 {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
