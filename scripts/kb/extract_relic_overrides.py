#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从官方简中语言表摘录遗物奖励物品的译名 → scripts/kb/zh_overrides_items.json

背景
----
遗物奖励里的新物品（新 Prime 部件/全套蓝图）在四个数据包快照里没有
官方简中名，KB 与插件卡面会回落成英文。本脚本用**本机游戏缓存**提取的
官方简中语言表（lang_zh_44.json，见 warframe-kb-build §④/§⑤ 的导出链）
为这些物品生成覆盖层，供 kb_lib.Sources 的 ov_name 最高优先级使用。

用法
----
    export WF_KB_DATA=<解包数据目录>
    python scripts/kb/extract_relic_overrides.py --lang <lang_zh_44.json> \
        [--dry-run]

匹配规则（按优先级，全部基于 uniqueName / 英文显示名 / 官方语言表）：
  ① CraftingComponent_<stem>[Name]（任意组，Name 后缀优先）→ 部件名
  ② Primes/<X>[Prime]SuitName|Name、Items/<X>Name、其余 *Name（排除 Changyou 国服组）
  ③ en2zh（items 表英文名→中文名）直查 + 「基础名 + Prime」回落
  ④ 手工补充表 MANUAL（当前仅 Nyx Prime 三部件：官方语言表确无该键，
     按 lang 中同构样本「<战甲名> 部件词」的拼接规则补）

只补「S.name() 解析为空或纯英文」的物品；已有中文的一律不动（最小改动）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from kb_lib import Sources                        # noqa: E402

OUT = HERE / "zh_overrides_items.json"

# 手工补充（官方语言表 key 缺失；依据：KB 与旧数据的既有形态 + lang 同构样本）
MANUAL = {
    "/Lotus/Types/Recipes/WarframeRecipes/NyxPrimeChassisBlueprint": "Nyx Prime 机体蓝图",
    "/Lotus/Types/Recipes/WarframeRecipes/NyxPrimeHelmetBlueprint": "Nyx Prime 头部神经光元蓝图",
    "/Lotus/Types/Recipes/WarframeRecipes/NyxPrimeSystemsBlueprint": "Nyx Prime 系统蓝图",
}

_STOP = {"warframe", "suit"}


def toks(s: str) -> frozenset:
    """CamelCase / 空格分词 → 小写词集合（忽略 warframe/suit 等插入词）。"""
    parts = re.findall(r"[A-Z]?[a-z0-9]+|[A-Z]+(?![a-z])", s or "")
    return frozenset(p.lower() for p in parts if p and p.lower() not in _STOP)


def compact(s: str) -> str:
    """全小写压缩串（吃掉 DE 命名的大小写分隔差异，如 AkJagara/Akjagara）。"""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def build_indexes(lang: dict) -> dict:
    cc, cc_c = {}, {}
    primes_name, primes_c = {}, {}
    items_name, items_c = {}, {}
    all_name, all_c = {}, {}
    for k, v in lang.items():
        if k.startswith("/Lotus/Language/Changyou/"):
            continue                      # 国服译名，禁用
        if "CraftingComponent_" in k:
            mid = k.split("CraftingComponent_", 1)[1]
            prio = 2 if mid.endswith("Name") else (0 if mid.endswith("Desc") else 1)
            mid = re.sub(r"(Desc|Name)$", "", mid)
            if not mid:
                continue
            t, c = toks(mid), compact(mid)
            if t not in cc or prio > cc[t][1]:
                cc[t] = (v, prio)
            if c not in cc_c or prio > cc_c[c][1]:
                cc_c[c] = (v, prio)
            continue
        if not k.endswith("Name"):
            continue
        mid = k.rsplit("/", 1)[-1][:-4]
        t, c = toks(mid), compact(mid)
        if k.startswith("/Lotus/Language/Primes/"):
            primes_name.setdefault(t, v)
            primes_c.setdefault(c, v)
        elif k.startswith("/Lotus/Language/Items/"):
            items_name.setdefault(t, v)
            items_c.setdefault(c, v)
        else:
            all_name.setdefault(t, v)
            all_c.setdefault(c, v)
    return {"cc": cc, "cc_c": cc_c, "primes": primes_name, "primes_c": primes_c,
            "items": items_name, "items_c": items_c, "all": all_name, "all_c": all_c}


def match(u: str, en: str, S: Sources, ix: dict):
    """uniqueName + 英文显示名 → (中文名, 规则标签) 或 (None, '')。

    蓝图后缀口径（与 i18n 链的既有形态一致）：
      · 组件命中（值是「X 机体/头部神经光元/系统」）→ `值+蓝图`（连写：X 机体蓝图）
      · 武器/物品名命中（值是「幻离子 Prime / Forma」）→ `值+ 蓝图`（空格）
    """
    seg = u.rsplit("/", 1)[-1]
    dup = seg.endswith("Blueprint")
    sfx_space = " 蓝图" if dup else ""
    sfx_glue = "蓝图" if dup else ""
    # ① 组件直配（uniqueName stem）
    if dup:
        stem = seg[:-len("Blueprint")]
        got = ix["cc"].get(toks(stem)) or ix["cc_c"].get(compact(stem))
        if got:
            return got[0] + sfx_glue, "CC+蓝图"
    got = ix["cc"].get(toks(seg)) or ix["cc_c"].get(compact(seg))
    if got:
        return got[0], "CC"
    # ② 英文显示名（去 Blueprint 的武器名）—— 覆盖类名 uniqueName 与 name/uniqueName 不一致
    base_en = re.sub(r"\s*Blueprint\s*$", "", en or "").strip()
    base_t, base_c = toks(base_en), compact(base_en)
    if base_t:
        got = ix["cc"].get(base_t) or ix["cc_c"].get(base_c)
        if got:
            return got[0] + sfx_glue, "CC-en"
        for tbl, tbl_c, tag in ((ix["primes"], ix["primes_c"], "PRIMES"),
                                (ix["items"], ix["items_c"], "ITEMS"),
                                (ix["all"], ix["all_c"], "ALL")):
            hit = tbl.get(base_t) or tbl_c.get(base_c)
            if hit:
                return hit + sfx_space, tag
    # ③ uniqueName stem 的 Primes/ 兜底
    if dup:
        stem = seg[:-len("Blueprint")]
        hit = ix["primes"].get(toks(stem)) or ix["primes_c"].get(compact(stem))
        if hit:
            return hit + " 蓝图", "PRIMES+蓝图"
    hit = ix["primes"].get(toks(seg)) or ix["primes_c"].get(compact(seg))
    if hit:
        return hit, "PRIMES"
    # ④ en2zh（items 表英文名→中文名，最完整）+ 基础名 + Prime 回落
    zh = S.en2zh.get(base_en)
    if not zh:
        m = re.match(r"^(.+?)\s*Prime$", base_en)
        if m:
            b = S.en2zh.get(m.group(1))
            if b:
                zh = b + " Prime"
    if zh:
        return zh + sfx_space, "EN2ZH"
    return None, ""


def relic_reward_items(data_dir: str) -> dict:
    """遗物奖励涉及的物品 {uniqueName: 英文显示名}。"""
    rel = json.load(open(os.path.join(data_dir, "items", "Relics.json"),
                         encoding="utf-8"))
    groups = {}
    for x in rel:
        m = re.match(r"^(.+?)\s+(Intact|Exceptional|Flawless|Radiant)$",
                     x.get("name") or "")
        if m:
            groups.setdefault(m.group(1), {})[m.group(2)] = x
    items = {}
    for g in groups.values():
        x = g.get("Intact") or next(iter(g.values()))
        for rw in x.get("rewards") or []:
            it = rw.get("item") or {}
            if it.get("uniqueName"):
                items[it["uniqueName"]] = it.get("name") or ""
    return items


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lang", required=True, help="lang_zh_44.json 路径")
    ap.add_argument("--dry-run", action="store_true", help="只打印不写文件")
    args = ap.parse_args()

    S = Sources()
    # ★ 幂等：先卸掉本脚本上一轮生成的覆盖——否则 S.name() 会把自己上轮的
    #   产物判成「已有中文」，本轮全量跳过并把文件写成空集（2026-09-24 踩过）。
    if OUT.exists():
        for _u in (json.load(open(OUT, encoding="utf-8")).get("items") or {}):
            S.ov_name.pop(_u, None)
    lang = json.load(open(args.lang, encoding="utf-8"))
    ix = build_indexes(lang)
    items = relic_reward_items(os.environ["WF_KB_DATA"])

    need, filled = [], {}
    for u, en in sorted(items.items(), key=lambda kv: kv[1]):
        zh = S.name(u)
        if zh and re.search(r"[\u4e00-\u9fff]", zh):
            continue                       # 已有中文，不动
        need.append((u, en))
        got, how = match(u, en, S, ix)
        if not got and u in MANUAL:
            got, how = MANUAL[u], "MANUAL"
        if got and got != en:              # 与英文原名相同 = 无增益（如安魂 MOD）
            filled[u] = {"name": got, "en": en, "how": how}
        else:
            print("  [MISS] %-52s | %s" % (en, u.rsplit("/", 1)[-1]))

    from collections import Counter
    how_cnt = Counter(v["how"] for v in filled.values())
    print("待补 %d → 补齐 %d（%s）" % (
        len(need), len(filled),
        "、".join("%s=%d" % kv for kv in sorted(how_cnt.items()))))
    print("遗留 MISS:", len(need) - len(filled))

    payload = {
        "_meta": {
            "purpose": "从本机游戏缓存语言表（lang_zh_44.json）摘录的遗物奖励物品官方简中名，"
                       "自动生成物——重跑 extract_relic_overrides.py 可整体重生成",
            "source": "玩家客户端 Cache.Windows → Languages.bin（简中）",
            "toolchain": "见 warframe-kb-build/references/01-data-sources.md §⑤",
            "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "count": len(filled),
            "rule_stats": dict(how_cnt),
        },
        "items": {u: v["name"] for u, v in sorted(filled.items())},
        "_detail": {u: {"en": v["en"], "how": v["how"]}
                    for u, v in sorted(filled.items())},
    }
    if args.dry_run:
        print(json.dumps(payload["items"], ensure_ascii=False, indent=1)[:1200])
        return 0
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    print("[OK]", OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
