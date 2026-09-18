# -*- coding: utf-8 -*-
"""按英文名反查 DE 官方简中译名（插件数据表专用）。

用途：卡片上出现英文没汉化时，先用它确认「官方表里到底有没有这个词条」，
再决定是用官方译名还是往兜底表里补人工译名 —— 而不是凭印象编一个中文名。

数据来源：core/data/de/ 下的表（均由 DE 官方导出构建）
  languages.json     /Lotus/Language/... 的**英文**原文
  languages_zh.json  同一批 key 的**简中**
  challenges_zh.json 挑战（沉沦之地「目标」）代码 → 中文
  name_zh.json       物品名 → 中文
  de_items_zh.json   物品 → 中文
  nodes_zh.json      节点 → 中文
  mission_types_zh.json / mod_names_zh.json / recipe_names_zh.json

用法：
    python scripts/lookup_zh.py "Bottled Lightning" "Psionic Feedback"
    python scripts/lookup_zh.py --like HorseCombat      # 模糊匹配
    python scripts/lookup_zh.py --tables                # 只看各表规模
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DE = ROOT / "core" / "data" / "de"

ZH_TABLES = [
    "languages_zh.json", "challenges_zh.json", "name_zh.json",
    "de_items_zh.json", "nodes_zh.json", "mission_types_zh.json",
    "mod_names_zh.json", "recipe_names_zh.json", "nightwave_zh.json",
    "bounty_jobs_zh.json", "wiki_disp.json", "events_zh.json",
]


def load(name: str) -> dict:
    p = DE / name
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        print(f"[警告] {name} 读取失败：{exc}", file=sys.stderr)
        return {}


def flat_zh(table) -> dict:
    """把 {k: {value: v}} 或 {k: v} 统一成 {k: v}；非 dict 的表返回空。"""
    if not isinstance(table, dict):
        return {}
    out = {}
    for k, v in table.items():
        if isinstance(v, dict):
            for lk in ("value", "Value", "zh", "cn"):
                if lk in v and isinstance(v[lk], str):
                    out[k] = v[lk]
                    break
        elif isinstance(v, str):
            out[k] = v
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("words", nargs="*")
    ap.add_argument("--like", action="store_true", help="子串匹配（默认精确）")
    ap.add_argument("--tables", action="store_true", help="只打印各表规模")
    args = ap.parse_args()

    langs_en = flat_zh(load("languages.json"))
    langs_zh = flat_zh(load("languages_zh.json"))

    if args.tables:
        print(f"languages.json    英文条目 {len(langs_en)}")
        print(f"languages_zh.json 中文条目 {len(langs_zh)}")
        for name in ZH_TABLES:
            if name == "languages_zh.json":
                continue
            t = flat_zh(load(name))
            if t:
                print(f"{name:22s} {len(t)}")
        return

    # 英文值 → key（同名多 key 时全部保留）
    en_index: dict[str, list[str]] = {}
    for k, v in langs_en.items():
        en_index.setdefault(v, []).append(k)

    if not args.words:
        print("用法：python scripts/lookup_zh.py \"英文名\" […]  （--like 模糊）")
        return

    for w in args.words:
        print(f"\n=== {w} ===")
        hits = 0
        if args.like:
            cands = [(k, v) for k, v in langs_en.items() if w.lower() in v.lower()]
        else:
            cands = [(k, langs_en[k]) for k in en_index.get(w, [])]
        for k, en in cands[:6]:
            zh = langs_zh.get(k)
            print(f"  官方表: {en!r} → {zh!r}    [{k}]")
            hits += 1
        # 其余中文表：按值里含英文名找（有些表直接是「英文: 中文」或「code: 中文」）
        for name in ZH_TABLES:
            t = flat_zh(load(name))
            for k, v in t.items():
                if args.like:
                    if w.lower() not in k.lower():
                        continue
                elif k != w:
                    continue
                print(f"  {name}: {k!r} → {v!r}")
                hits += 1
                if hits > 14:
                    break
        if not hits:
            print("  （所有官方表里都没有 —— 需要人工补，或该字段是 DE 内部代号）")


if __name__ == "__main__":
    main()
