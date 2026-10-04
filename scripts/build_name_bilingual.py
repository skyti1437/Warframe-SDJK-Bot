# -*- coding: utf-8 -*-
"""B1 构建期：dict.en/zh 同键对 → 精简双语名称表 core/data/de/name_bilingual.json。

背景（2026-10-03 插件批 B1「翻译」指令）：运行期不能带 3.7MB×2 的原始 dict
（在 EXCLUDE_DIRS 不进包），所以**构建期**筛出「名称类」短条目生成精简对照表
随包分发。筛选口径（可重跑）：

  · 命名空间白名单：Mods / Weapons / Suits / Items
    （覆盖 MOD / 武器 / 战甲 / 赋能 / 资源 / 常用道具；赋能与资源在 Items 下）；
  · 排除描述类键尾：Desc/Description/Tip/Hint/Text/Lore/Body/Objective/...；
  · 值须短（en ≤ 60 / zh ≤ 30）、无换行、zh 含 CJK 且与 en 不同（未译的跳过）。

校验：core/data/de/mod_names_zh.json（1369 条中文名 list，无英文）——
统计其中能被本表 zh 侧覆盖的比例，作为抽样哨兵（不要求 100%，它是另一来源）。

用法：python scripts/build_name_bilingual.py [--check]
  --check 只校验不写盘（用于 CI/回归）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "core" / "data" / "_cache"
OUT = ROOT / "core" / "data" / "de" / "name_bilingual.json"
MOD_ZH = ROOT / "core" / "data" / "de" / "mod_names_zh.json"

NS_OK = (
    "Mods",
    "Weapons",
    "Suits",
    "Items",
    "Primes",  # Prime 武器/战甲名与部件（Athodai Prime 等）
    "ClanTech",  # 氏族/特殊资源（突变原聚合物 等）
    "CraftingComponents",
)  # 部件名（X 枪管 / X 枪机 等）
BAD_SUFFIX = (
    "Desc",
    "Description",
    "Desc2",
    "Desc3",
    "Tip",
    "Hint",
    "Text",
    "Lore",
    "Body",
    "Subtitle",
    "Quote",
    "Objective",
    "Popup",
    "Info",
    "Letter",
    "Mail",
    "Message",
    "Story",
    "Transmission",
)


def _has_cjk(s: str) -> bool:
    return any("\u4e00" <= c <= "\u9fff" for c in s)


def keep(key: str, en: str, zh: str) -> bool:
    parts = key.split("/")
    ns = parts[3] if len(parts) > 3 else ""
    if ns not in NS_OK:
        return False
    leaf = key.rsplit("/", 1)[-1]
    if any(leaf.endswith(s) for s in BAD_SUFFIX):
        return False
    if not en or not zh or en == zh:
        return False
    if len(en) > 60 or len(zh) > 30:
        return False
    if "\n" in en or "\n" in zh:
        return False
    return _has_cjk(zh)


def build() -> dict:
    en = json.loads((CACHE / "dict.en.json").read_text(encoding="utf-8"))
    zh = json.loads((CACHE / "dict.zh.json").read_text(encoding="utf-8"))
    pairs = []
    seen = set()
    for k, v in en.items():
        z = zh.get(k, "")
        if not keep(k, str(v).strip(), str(z).strip()):
            continue
        pair = (str(v).strip(), str(z).strip())
        if pair in seen:
            continue
        seen.add(pair)
        pairs.append([pair[0], pair[1]])
    pairs.sort(key=lambda p: p[0].lower())
    return {
        "_note": (
            "双语名称对照表（B1「翻译」指令数据）。构建期由 "
            "core/data/_cache/dict.en/zh.json（DE 官方 language 表同键对）"
            "筛选生成：命名空间 Mods/Weapons/Suits/Items，排除描述类键尾，"
            "仅收短名（en≤60/zh≤30、zh 含 CJK 且与 en 不同）。"
            "重新生成：python scripts/build_name_bilingual.py"
        ),
        "pairs": pairs,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只校验不写盘")
    args = ap.parse_args()
    if not (CACHE / "dict.en.json").exists():
        print(f"缺少离线缓存 {CACHE / 'dict.en.json'}（构建机需先有 dict 对）")
        sys.exit(1)
    data = build()
    n = len(data["pairs"])
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    size = len(payload.encode("utf-8"))
    print(f"名称对: {n} 条 · 序列化 {size / 1024:.0f} KB（目标 ≤1536 KB）")
    # 抽样哨兵：mod_names_zh 覆盖率
    if MOD_ZH.exists():
        zh_list = json.loads(MOD_ZH.read_text(encoding="utf-8"))
        ours = {z for _e, z in data["pairs"]}
        hit = sum(1 for z in zh_list if z in ours)
        print(
            f"mod_names_zh 覆盖: {hit}/{len(zh_list)}"
            f"（{hit / max(1, len(zh_list)):.0%}；另一来源，不要求 100%）"
        )
    if size > 1536 * 1024:
        print("✗ 体积超 1.5MB 目标，需收紧筛选口径")
        sys.exit(1)
    if args.check:
        print("✓ --check 通过（未写盘）")
        return
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"✓ 已写 {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
