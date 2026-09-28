# -*- coding: utf-8 -*-
"""构建期提取：从 18MB 全量简中包抽出**卡面会用到的命名空间** → `core/data/de/zh_ext.json`

为什么需要（2026-09-27 审核结论 §2.4 的根因）：
  · 运行期唯一简中表 `core/data/de/languages_zh.json` 只有 36,088 键；
  · 全量包 `core/data/_cache/languages_zh_full.json`（140,264 键 / 18MB）**没有任何代码读它**；
  · 差额 104,177 键，正是「沉沦之地复杂化名 / 1999 日历升级名 / 源力石 / 赋能槽连接器」这几类
    ⇒ 卡面才落英文。**但 18MB 绝不能进运行期**（服务器 2 核 / 1.7G，3.5MB 的表已实测堵事件循环 30s+）。

做法：**构建期**只提取卡面用得到的命名空间，产出精简补充表（运行期由 `language_text_zh()`
第二顺位查它）。**提取时保留原文**（`<...>` 占位符、`|COLOR_*|` 富文本都不动 —— 语义不丢、便于排错；
清洗放在查询函数里做，见 `de_worldstate.language_text_zh`）。

用法：
    python scripts/build_zh_ext.py                 # 提取（默认只读 _cache 全量包）
    python scripts/build_zh_ext.py --stats         # 只看各命名空间规模，不写文件
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FULL = ROOT / "core" / "data" / "_cache" / "languages_zh_full.json"
OUT = ROOT / "core" / "data" / "de" / "zh_ext.json"

# 卡面会用到的命名空间（前缀匹配，大小写不敏感）。
# · CircleOfHell：沉沦之地（炼狱）复杂化名 —— 官方 268 键，本地 36k 只有 6
# · 1999：霍瓦尼亚日历升级名 / 加成 / 珍宝 —— 官方 716，本地 211
# · Narmer：执刑官源力石 —— 官方 149，本地 113
# · Weapons：赋能槽连接器等 —— 官方 776，本地 624
# · Items：奖励类（催化剂/反应堆/晶体/遗物包/内融核心 bundle …）—— 被日历与入侵卡引用
PREFIXES = ("/lotus/language/circleofhell/", "/lotus/language/1999/",
            "/lotus/language/narmer/", "/lotus/language/weapons/",
            "/lotus/language/items/")


def main() -> int:
    if not FULL.exists():
        print(f"✗ 找不到全量包：{FULL}\n  先拉：curl -L -o {FULL} "
              "https://raw.githubusercontent.com/calamity-inc/"
              "warframe-languages-bin-data/senpai/zh.json")
        return 2
    full = json.loads(FULL.read_text(encoding="utf-8"))
    picked: dict[str, str] = {}
    per_ns: dict[str, int] = {}
    for k, v in full.items():
        if not isinstance(k, str):
            continue
        low = k.lower()
        for p in PREFIXES:
            if low.startswith(p):
                val = v.get("value") if isinstance(v, dict) else v
                if isinstance(val, str) and val:
                    picked[k] = val
                    per_ns[p.rstrip("/").rsplit("/", 1)[-1]] = \
                        per_ns.get(p.rstrip("/").rsplit("/", 1)[-1], 0) + 1
                break
    print(f"全量包 {len(full)} 键 → 提取 {len(picked)} 键")
    for ns, n in sorted(per_ns.items(), key=lambda x: -x[1]):
        print(f"   {ns:<14}{n:>6} 键")
    coh = sum(1 for k in picked if "cohchallenge" in k.lower())
    print(f"   其中 CoHChallenge* = {coh} 键（审核要求 ≥68）")

    blob = json.dumps(picked, ensure_ascii=False, sort_keys=True).encode("utf-8")
    print(f"产物大小 {len(blob)} bytes（{len(blob)/1024:.0f} KB）")
    if "--stats" in sys.argv:
        return 0
    OUT.write_bytes(blob)
    print(f"已写 {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
