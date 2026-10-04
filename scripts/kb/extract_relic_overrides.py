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
from kb_lib import Sources  # noqa: E402

OUT = HERE / "zh_overrides_items.json"

# 手工补充（官方语言表 key 缺失；依据：KB 与旧数据的既有形态 + lang 同构样本）
MANUAL = {
    "/Lotus/Types/Recipes/WarframeRecipes/NyxPrimeChassisBlueprint": "Nyx Prime 机体蓝图",
    "/Lotus/Types/Recipes/WarframeRecipes/NyxPrimeHelmetBlueprint": "Nyx Prime 头部神经光元蓝图",
    "/Lotus/Types/Recipes/WarframeRecipes/NyxPrimeSystemsBlueprint": "Nyx Prime 系统蓝图",
    # 挂在「全能 Forma（Omni Forma）」components 下的合成材料，un 虽带 Blueprint
    # 后缀但语义是「光环 Forma」本体（DE 内部命名混乱；SYNTH 会错合成「全能 Forma 蓝图」）
    "/Lotus/Types/Recipes/Components/FormaAuraBlueprint": "光环 Forma",
}

# 直键精确命中后的弃用名单（2026-09-30 部件批次人工复核：错配/内部占位/场景名）
DROP_SEGS = {
    "BardQuestSequencerBlueprint",  # 直键值「曼达和弦琴」与该蓝图无关
    "InfestedFoundryBlueprint",  # 值是系统名 HELMINTH，非蓝图名
    "FormaOmegaBlueprint",  # 值带 TEST（内部占位）
    "MummyBlueprint",  # 值「捍卫者蓝图」语义可疑
    "NoraShipBlueprint",  # 场景装饰，值是场景名
    "ZarimanShipBlueprint",  # 同上
    "SentientBlueprint",  # 值「震荡使齐诺斯库」可疑
    "SiriusOrionBlueprint",  # Movember 装饰
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
    seg_exact = {}
    for k, v in lang.items():
        if k.startswith("/Lotus/Language/Changyou/"):
            continue  # 国服译名，禁用
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
        mid = k.rsplit("/", 1)[-1]
        if mid.endswith("Desc") or mid.startswith("Desc"):
            continue
        # ⓪ 直键：末段与 uniqueName stem 完全一致（新内容部件键常无 Name 后缀，
        #    如 Iceblade/DuelistBowGrip →「霜冥差 握把」）；Name 后缀键优先级更高
        if not mid.endswith("Name"):
            seg_exact.setdefault(mid, v)
        else:
            seg_exact.setdefault(mid[:-4], v)
            mid = mid[:-4]
        if not mid:
            continue
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
    return {
        "cc": cc,
        "cc_c": cc_c,
        "primes": primes_name,
        "primes_c": primes_c,
        "items": items_name,
        "items_c": items_c,
        "all": all_name,
        "all_c": all_c,
        "seg": seg_exact,
    }


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
    # ⓪ 直键精确命中（uniqueName stem == 语言键末段；新内容部件键常无 Name 后缀。
    #    候选须含「剥 Blueprint 后的武器名」形态——如 KuvaOgrisBlueprint → 键 KuvaOgris）
    if seg not in DROP_SEGS:
        stem_bp = seg[: -len("Blueprint")] if dup else seg
        base_seg = re.sub(r"(Component|Item)$", "", stem_bp)
        for cand in (seg, stem_bp, base_seg, seg + "Name", stem_bp + "Name", base_seg + "Name"):
            if not cand:
                continue
            got = ix["seg"].get(cand)
            if (
                got
                and isinstance(got, str)
                and got.strip()
                and "TEST" not in got
                and "[PH" not in got
                and "<" not in got
                and len(got) <= 40
            ):
                v = got.strip()
                if dup and "蓝图" not in v:
                    v += sfx_space
                return v, "SEG"
    # ① 组件直配（uniqueName stem）
    if dup:
        stem = seg[: -len("Blueprint")]
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
        for tbl, tbl_c, tag in (
            (ix["primes"], ix["primes_c"], "PRIMES"),
            (ix["items"], ix["items_c"], "ITEMS"),
            (ix["all"], ix["all_c"], "ALL"),
        ):
            hit = tbl.get(base_t) or tbl_c.get(base_c)
            if hit:
                return hit + sfx_space, tag
    # ③ uniqueName stem 的 Primes/ 兜底
    if dup:
        stem = seg[: -len("Blueprint")]
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
    rel = json.load(open(os.path.join(data_dir, "items", "Relics.json"), encoding="utf-8"))
    groups = {}
    for x in rel:
        m = re.match(r"^(.+?)\s+(Intact|Exceptional|Flawless|Radiant)$", x.get("name") or "")
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


# 部件词合成映射（官方模式「武器名 + 部件词」，如 PaxDuviricus 枪械部件「锐铁 枪管」；
# 用于语言包无独立键的部件 —— i18n/dict.zh 对 TnHopliteSpearGunWeaponBlueprint 等零收录）
PART_WORDS = {
    "Barrel": "枪管",
    "Receiver": "枪机",
    "Stock": "枪托",
    "Link": "连接器",
    "Blade": "刀刃",
    "Handle": "握柄",
}
# 类别插入词：父条目末段与部件 stem 比对时两侧都剥掉（同词异序容忍）
CLASS_WORDS = {"weapon", "sentinel", "guard", "gun", "suit", "warframe"}


def component_items(data_dir: str) -> tuple:
    """条目 components 里的部件 {uniqueName: 英文显示名} 与 {部件: 父条目 uniqueName}
    （2026-09-30 新增源：消除材料行「｜ ×1」的空部件名——如 Prime 战甲/信条武器的部件蓝图）。"""
    items = {}
    parent = {}
    idir = os.path.join(data_dir, "items")
    for t in (
        "Warframes",
        "Primary",
        "Secondary",
        "Melee",
        "Sentinels",
        "Arch-Gun",
        "Arch-Melee",
        "Archwing",
        "Misc",
        "Pets",
    ):
        fp = os.path.join(idir, t + ".json")
        if not os.path.exists(fp):
            continue
        for r in json.load(open(fp, encoding="utf-8")):
            for c in r.get("components") or []:
                if c.get("uniqueName"):
                    items[c["uniqueName"]] = c.get("name") or ""
                    parent.setdefault(c["uniqueName"], r.get("uniqueName") or "")
    return items, parent


def synth_part_name(u: str, parent_un: str, S: Sources) -> str | None:
    """父条目官方中文名 + 部件词 → 合成部件名。

    两种形态（父末段与部件 stem 是同词异序，按剥类别词后的词集合比对）：
      · XxxBlueprint → 「父名 蓝图」（TnHopliteSpearGunWeaponBlueprint → 圣英 蓝图）
      · Xxx<部件词>   → 「父名 部件词」（AfentisPrimeBarrel → 圣英 Prime 枪管）
    """
    seg = u.rsplit("/", 1)[-1]
    stem = seg[: -len("Blueprint")] if seg.endswith("Blueprint") else seg
    zh_word = "蓝图" if seg.endswith("Blueprint") else None
    if zh_word is None:
        for suf, zh in PART_WORDS.items():
            if stem.endswith(suf):
                stem, zh_word = stem[: -len(suf)], zh
                break
    if zh_word is None or len(stem) < 6 or not parent_un:
        return None
    pw = toks(parent_un.rsplit("/", 1)[-1]) - CLASS_WORDS
    sw = toks(stem) - CLASS_WORDS
    if pw != sw or not pw:
        return None
    base = S.name(parent_un) if parent_un else None
    if base and re.search(r"[\u4e00-\u9fff]", base) and len(base) <= 30:
        return "%s %s" % (base, zh_word)
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lang", required=True, help="lang_zh_44.json 路径")
    ap.add_argument("--dry-run", action="store_true", help="只打印不写文件")
    args = ap.parse_args()

    S = Sources()
    # ★ 幂等：先卸掉本脚本上一轮生成的覆盖——否则 S.name() 会把自己上轮的
    #   产物判成「已有中文」，本轮全量跳过并把文件写成空集（2026-09-24 踩过）。
    if OUT.exists():
        for _u in json.load(open(OUT, encoding="utf-8")).get("items") or {}:
            S.ov_name.pop(_u, None)
    lang = json.load(open(args.lang, encoding="utf-8"))
    ix = build_indexes(lang)
    items = relic_reward_items(os.environ["WF_KB_DATA"])
    comp_items, comp_parent = component_items(os.environ["WF_KB_DATA"])
    for u, en in comp_items.items():
        items.setdefault(u, en)

    need, filled = [], {}
    for u, en in sorted(items.items(), key=lambda kv: kv[1]):
        zh = S.name(u)
        if zh and re.search(r"[\u4e00-\u9fff]", zh):
            continue  # 已有中文，不动
        need.append((u, en))
        got, how = match(u, en, S, ix)
        if not got and u in MANUAL:
            got, how = MANUAL[u], "MANUAL"
        if not got and u in comp_parent and u.rsplit("/", 1)[-1] not in DROP_SEGS:
            got = synth_part_name(u, comp_parent[u], S)
            if got:
                how = "SYNTH"
        if got and got != en:  # 与英文原名相同 = 无增益（如安魂 MOD）
            filled[u] = {"name": got, "en": en, "how": how}
        else:
            print("  [MISS] %-52s | %s" % (en, u.rsplit("/", 1)[-1]))

    from collections import Counter

    how_cnt = Counter(v["how"] for v in filled.values())
    print(
        "待补 %d → 补齐 %d（%s）"
        % (len(need), len(filled), "、".join("%s=%d" % kv for kv in sorted(how_cnt.items())))
    )
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
        "_detail": {u: {"en": v["en"], "how": v["how"]} for u, v in sorted(filled.items())},
    }
    if args.dry_run:
        print(json.dumps(payload["items"], ensure_ascii=False, indent=1)[:1200])
        return 0
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print("[OK]", OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
