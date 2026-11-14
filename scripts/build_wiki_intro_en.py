#!/usr/bin/env python3
"""离线接入经核对的英文简介。使用相同语言键、资产和材料数量校验来源。

原文核对表由本批交接提供。另需对应的 DE ExportUpgrades_en.json、固定提交的
WFCD all.slim.json 和 PEP ExportRecipes.json。生成时打印输入校验值与覆盖数量。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "core" / "data"
OUT = DATA / "wiki_intro_en.json"
sys.path.insert(0, str(ROOT))
from build_drops import parse_any, place_label, strip_tags, translate_label  # noqa: E402
from build_wiki_intro import build_source_names, translate_source  # noqa: E402
from core import i18n, wiki_intro as wiki  # noqa: E402

_CJK = re.compile(r"[\u3400-\u9fff]")
_TOKEN = re.compile(r"\|([A-Z][A-Z0-9_]*)\|")
_NUMBER = r"[+-]?\d+(?:\.\d+)?"
_PERCENT = re.compile(r"(.+?)[\s　]+(\d+(?:\.\d+)?)%")
_MOD_RANK = re.compile(r"Effect \(Max Rank (\d+)\)")
_MATERIAL = re.compile(r"(.+?)\s*×\s*(\d+)")
_COLOR_TOKEN = re.compile(r"\|(?:COLOR_[A-Z_]+|OPEN_COLOR|CLOSE_COLOR)\|")
_COMPONENTS = {
    "枪管",
    "枪机",
    "机匣",
    "枪托",
    "刀刃",
    "握柄",
    "刀柄",
    "连接器",
    "下机匣",
    "上机匣",
    "肢体",
    "弓弦",
    "系统",
    "机体",
    "头部神经光元",
}
_STATIC_PARTS = {
    "资源": "Resources",
    "材料": "Materials",
    "鱼类（开放世界垂钓）": "Fish (Open World)",
    "Plexus（九重天）MOD": "Plexus Mod",
    "Parazon（数据匕首）MOD": "Parazon Mod",
    "MOD 套装 MOD": "Set Mod",
    "战甲强化 MOD": "Warframe Augment Mod",
    "战甲 MOD": "Warframe Mod",
    "主武器 MOD": "Primary Mod",
    "副武器 MOD": "Secondary Mod",
    "近战 MOD": "Melee Mod",
    "步枪 MOD": "Rifle Mod",
    "手枪 MOD": "Pistol Mod",
    "霰弹枪 MOD": "Shotgun Mod",
    "守护 MOD": "Sentinel Mod",
    "空战 MOD": "Archwing Mod",
    "主武器": "Primary",
    "副武器": "Secondary",
    "战甲 · 普通版": "Warframe",
    "战甲 · Prime 版": "Prime Warframe",
    "战甲 · Umbra 版": "Umbra Warframe",
    "Zaw 部件": "Zaw Component",
    "特殊条目（非可操控战甲）": "Non-playable Warframe",
    "噪音警报": "Noise: Alarming",
    "噪音静音": "Noise: Silent",
    "扳机全自动": "Trigger: Auto",
    "扳机半自动": "Trigger: Semi",
    "扳机蓄力": "Trigger: Charge",
    "扳机持续": "Trigger: Held",
    "扳机点射": "Trigger: Burst",
    "已入库（Vaulted）": "Vaulted",
    "极性万能（⟦pol:umbra⟧Umbra 除外）": "Universal Polarity (except ⟦pol:umbra⟧Umbra)",
}
_RARITY = {
    "普通": "Common",
    "罕见": "Uncommon",
    "稀有": "Rare",
    "传说": "Legendary",
}
_PASSIVE_SOURCES = {
    "Banshee": "https://www.warframe.com/en/patch-notes/switch/44-0-0",
    "Baruuk": "https://wiki.warframe.com/w/Baruuk",
    "Frost": "https://wiki.warframe.com/w/Module:Warframes/data",
    "Ivara": "https://wiki.warframe.com/w/Module:Warframes/data",
    "Loki": "https://www.warframe.com/en/patch-notes/pc/18-13-0",
    "Valkyr": "https://www.warframe.com/en/patch-notes/pc/39-0-0",
}


def read(path: Path):
    raw = path.read_bytes()
    return json.loads(raw.decode("utf-8-sig")), hashlib.sha256(raw).hexdigest()


def norm(text: str) -> str:
    return " ".join(text.split())


def one(values: set[str] | None) -> str:
    return next(iter(values)) if values and len(values) == 1 else ""


def numbers(text: str) -> Counter:
    return Counter(Decimal(v) for v in re.findall(_NUMBER, text.replace("−", "-")))


def clean(text: str) -> str:
    text = re.sub(r"<CREDITS>(?!\s*Credits\b)", "Credits", text, flags=re.I)
    return norm(wiki._clean_hw_tags(_COLOR_TOKEN.sub("", text)))


def fill_template(original: str, english: str, actual: str) -> str:
    """仅从同一简中模板中定位仍可读的数值。缺数的占位符保留待核对。"""
    if not _TOKEN.search(english):
        return english
    segments = _TOKEN.split(original)
    parts = []
    for index, value in enumerate(segments):
        parts.append(f"({_NUMBER})" if index % 2 else re.escape(value).replace(r"\ ", r"\s*"))
    match = re.fullmatch("".join(parts), actual)
    if match is None:
        return ""
    slots = {}
    for key, value in zip(segments[1::2], match.groups()):
        if slots.setdefault(key, value) != value:
            return ""
    return _TOKEN.sub(lambda m: slots.get(m[1], m[0]), english)


def line_parts(line: str) -> tuple[str, str]:
    if line.startswith("◆ ") and ": " in line:
        label, value = line.split(": ", 1)
        return label + ": ", value
    if line.startswith("· "):
        return "· ", line[2:]
    return "", line


def intact(source: str, candidate: str) -> bool:
    return (
        bool(candidate)
        and not _CJK.search(candidate)
        and not _TOKEN.search(candidate)
        and not any(mark in candidate for mark in ("〈", "〉", "[PH]", "¦"))
        and numbers(source) == numbers(candidate)
    )


def format_line(prefix: str, value: str) -> str:
    if prefix == "◆ Effect (official zh-CN): ":
        prefix = "◆ Effect: "
    return prefix + value


def static_line(label: str, value: str) -> str:
    """插件自己的字段文案只替换已审订的术语，专名仍以同键原文为准。"""
    if label not in {"Type", "Role", "Traits", "Polarity", "Shop", "Status"}:
        return ""
    out = []
    for fragment in re.split(r"(｜| / )", value):
        if fragment in _STATIC_PARTS:
            fragment = _STATIC_PARTS[fragment]
        elif bilingual := re.fullmatch(r"[\u3400-\u9fff]+（([A-Za-z][A-Za-z -]+)）", fragment):
            fragment = bilingual[1]
        elif fragment.startswith("稀有度") and fragment[3:] in _RARITY:
            fragment = "Rarity: " + _RARITY[fragment[3:]]
        elif label in {"Type", "Polarity"}:
            fragment = re.sub(
                r"(\b(?:Madurai|Vazarin|Naramon|Penjaga|Unairu|Zenurik|Umbra))"
                r"（(?:攻击|防御|战术|预置|姿态|特殊|暗影)）",
                r"\1",
                fragment,
            )
            fragment = fragment.replace("、", ", ")
            fragment = re.sub(r"^极性\s+", "Polarity: ", fragment)
            fragment = re.sub(r"^适配\s+", "Compatible with: ", fragment)
            fragment = re.sub(r"^基础容量 (\d+)$", r"Base Drain \1", fragment)
            fragment = re.sub(r"^(?:满级 (\d+)|最高 (\d+) 级)$", r"Max Rank \1\2", fragment)
        elif label == "Shop":
            fragment = re.sub(r"^(\d+) 铂金$", r"\1 Platinum", fragment)
        elif label == "Traits":
            fragment = re.sub(
                r"^Riven Disposition: (\d{4}-\d{2}) 起统一 (\S+)（取消前 (\S+)）$",
                r"Riven Disposition: \2 since \1 (previously \3)",
                fragment,
            )
        out.append(fragment)
    return "".join(out)


def passive_line(title: str, value: str, key: str, en: dict) -> str:
    """少数随包导出缺值或版本滞后的被动，按该战甲的更新说明和资料核对。"""
    name = wiki._en_title(wiki.display_title(title)).removesuffix(" Prime")
    if (
        name == "Ivara"
        and key.endswith("IvaraPrimePassiveAbility")
        and value == "侦测周围米内的敌人。"
    ):
        return en[key].replace("|RADIUS|", "50")
    if (
        name == "Loki"
        and key.endswith("LokiPrimePassiveAbility")
        and value == "壁面攀附时间提升倍。"
    ):
        return en[key].replace("|MULT|", "10")
    if name == "Banshee" and "施放技能时" in value and "穿刺异常" in value:
        return "Casting abilities applies maximum Puncture Status within 10m. Banshee's weapons are silent."
    if name == "Baruuk" and "克制力" in value and "伤害" in value:
        return "Dodging projectiles, lulling or disarming enemies reduces Restraint, granting up to 50% damage reduction."
    if name == "Frost" and key == "/Lotus/Language/Suits/FrostPassiveAbility":
        return clean(en[key].replace("|DURATION|", "100").replace("|ARMOR|", "50"))
    if name == "Valkyr" and key.endswith("ValkyrPassiveAbility") and "狂怒值" in value:
        return (
            en[key]
            .replace("above |CONSUME|%", "at or above 150%")
            .replace("|PERCENT|", "300")
            .replace("|DURATION|", "5")
        )
    return ""


def inventory() -> dict[str, set[str]]:
    rows: dict[str, set[str]] = defaultdict(set)
    for title, raw_lines in wiki._table()[0]:
        for line in wiki._intro_lines(raw_lines):
            rendered = wiki._en_line(line)
            if _CJK.search(rendered):
                rows[rendered].add(title)
    return rows


def mod_line(row: dict, mod: dict, title: str, value: str) -> str:
    label = row["实际显示位置"].rsplit("· ", 1)[-1]
    m = _MOD_RANK.fullmatch(label)
    levels = mod.get("levelStats") or []
    if mod.get("name", "").casefold() != wiki._en_title(wiki.display_title(title)).casefold():
        return ""
    if m:
        rank = int(m[1])
        if rank != mod.get("fusionLimit") or rank >= len(levels):
            return ""
        levels = levels[rank : rank + 1]
    elif label == "Effect (0 级)":
        levels = levels[:1]
    original = norm(row["候选英文"])
    for level in levels:
        stats = "；".join(level["stats"])
        if norm(stats) == original:
            candidate = "；".join(clean(s) for s in level["stats"])
            return candidate if intact(value, candidate) else ""
    return ""


def drop_index(rows: list, names: dict) -> dict[tuple[str, Decimal], set[str]]:
    sources: dict[str, set[Decimal]] = defaultdict(set)
    for row in rows:
        if row.get("chance") is not None:
            sources[strip_tags(row["place"])].add(Decimal(str(row["chance"])))
    out: dict[tuple[str, Decimal], set[str]] = defaultdict(set)
    for raw, rates in sources.items():
        key = parse_any(raw)
        labels = {
            translate_source("获取：" + raw, names)[3:],
            place_label(key),
            translate_label(place_label(key), names),
        }
        for label in labels:
            if _CJK.search(label) and not _CJK.search(raw):
                for rate in rates:
                    out[(norm(label), rate)].add(raw)
    return out


def verified_drop(value: str, shown: str, key: str, en: dict, le: dict, zh: dict, lz: dict) -> str:
    chance = _PERCENT.fullmatch(value)
    if not chance or not key or "；" in key:
        return ""
    name, rate = chance.groups()
    actual = clean(en.get(key) or le.get(key) or "")
    before = clean(zh.get(key) or lz.get(key) or "")
    if norm(name) != before or norm(f"{actual} {rate}%") != norm(shown):
        return ""
    return f"{actual}　{rate}%" if "　" in value else f"{actual} {rate}%"


def place_line(value: str, sources: dict) -> str:
    parts = []
    for part in wiki._split_top(value):
        chance = _PERCENT.fullmatch(part)
        if not chance:
            return ""
        name, rate = chance.groups()
        english = one(sources.get((norm(name), Decimal(rate))))
        if not english:
            return ""
        sep = "　" if "　" in part else " "
        parts.append(f"{english}{sep}{rate}%")
    return "；".join(parts)


def recipes_by_name(recipes: dict, en: dict, zh: dict) -> dict[str, list]:
    out = defaultdict(list)
    for blueprint, recipe in recipes.items():
        result = recipe.get("resultType", "").lower()
        if not result or not recipe.get("ingredients"):
            continue
        for name in {en.get(result), zh.get(result)} - {None, ""}:
            out[name.casefold()].append((blueprint.lower(), result, recipe["ingredients"]))
    return out


def material_line(value: str, title: str, recipes: dict, en: dict, zh: dict) -> str:
    materials = []
    for part in value.split("｜"):
        match = _MATERIAL.fullmatch(part.strip())
        if not match:
            return ""
        materials.append((match[1], int(match[2])))
    candidates = set()
    english_title = wiki._en_title(wiki.display_title(title)).casefold()
    for blueprint, result, ingredients in recipes.get(english_title, []):
        out = []
        left = [(r["ItemType"].lower(), r["ItemCount"]) for r in ingredients]
        for name, count in materials:
            if count == 1 and name in {zh.get(blueprint), (zh.get(result) or "") + "蓝图"}:
                shown = en.get(blueprint) or (en[result] + " Blueprint" if result in en else "")
            else:
                matches = [
                    (asset, qty)
                    for asset, qty in left
                    if qty == count
                    and (
                        name in {zh.get(asset), en.get(asset)}
                        or (name in _COMPONENTS and (zh.get(asset) or "").endswith(name))
                    )
                ]
                if len(matches) != 1:
                    break
                asset, _qty = matches[0]
                shown = en.get(asset, "")
                left.remove(matches[0])
            if not shown:
                break
            out.append(f"{shown} ×{count}")
        if not left and len(out) == len(materials):
            candidates.add("｜".join(out))
    return one(candidates)


def ability_line(value: str, shown: str, keys: str, en: dict, le: dict, zh: dict, lz: dict) -> str:
    old, new = value.split(" / "), shown.split(" / ")
    if len(old) != len(new):
        return ""
    names = {
        (clean(zh.get(key) or lz.get(key) or ""), clean(en.get(key) or le.get(key) or ""))
        for key in keys.split("；")
        if key
    }
    if all(a == b or (clean(a), clean(b)) in names for a, b in zip(old, new)):
        return " / ".join(clean(b) for b in new)
    return ""


def build(
    audit: dict, upgrades: dict, drops: list, recipe_data: dict, tables: dict
) -> tuple[dict, Counter]:
    le, lz, en, zh = (tables[k] for k in ("languages_en", "languages_zh", "name_en", "name_zh"))
    mods = {m["uniqueName"]: m for m in upgrades["ExportUpgrades"]}
    locations = drop_index(drops, build_source_names({**en, **le}, {**zh, **lz}))
    recipes = recipes_by_name(recipe_data, en, zh)
    existing = inventory()
    result: dict = {
        "schema": 1,
        "lines": {},
        "titles": {},
        "cards": {},
        "passive_sources": _PASSIVE_SOURCES,
    }
    counts: Counter = Counter()
    for row in audit["wiki卡片"]:
        original, shown, key = (row[k] for k in ("原中文", "候选英文", "资产或语言键"))
        status = row["状态"]
        source = row["来源类型"]
        label = row["实际显示位置"].rsplit("· ", 1)[-1]
        prefix, value = line_parts(original)
        if label == "<标题>" and status == "已验证" and norm(le.get(key, "")) == norm(shown):
            if intact(value, clean(shown)):
                result["titles"][original] = clean(shown)
                counts["同一语言键标题"] += 1
            continue
        titles = existing.get(original)
        if not titles:
            continue
        if status == "已验证" and key in mods:
            for title in titles:
                english = mod_line(row, mods[key], title, value)
                if english:
                    result["cards"].setdefault(title, {})[original] = format_line(prefix, english)
                    counts["同资产同等级效果"] += 1
            continue
        if label == "Build Materials":
            for title in titles:
                english = material_line(value, title, recipes, en, zh)
                if intact(value, english):
                    result["cards"].setdefault(title, {})[original] = format_line(prefix, english)
                    counts["配方与数量对齐"] += 1
            continue
        if label == "Passive":
            for title in titles:
                english = passive_line(title, value, key, le)
                if english:
                    result["cards"].setdefault(title, {})[original] = format_line(prefix, english)
                    counts["核实的战甲被动"] += 1
        english, group = "", ""
        if "WFCD" in source:
            if status == "已验证":
                english = verified_drop(value, shown, key, en, le, zh, lz)
                group = "官方敌人或物品名"
            if not english:
                english = place_line(value, locations)
                group = "固定掉落原文"
        elif status == "已验证" and label == "Abilities":
            english, group = ability_line(value, shown, key, en, le, zh, lz), "同键技能名"
        elif status == "已验证" and key in lz and norm(le.get(key, "")) == norm(shown):
            english = fill_template(clean(lz[key]), clean(le[key]), value)
            group = "同一语言键正文"
        elif status == "插件自写":
            english, group = static_line(label, value), "插件字段术语"
        if intact(value, english):
            result["lines"][original] = format_line(prefix, english)
            counts[group] += 1
    return result, counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--upgrades", type=Path, required=True)
    parser.add_argument("--drops", type=Path, required=True)
    parser.add_argument("--recipes", type=Path, required=True)
    parser.add_argument(
        "--source-tables",
        type=Path,
        default=DATA / "de",
        help="原文核对表绑定的 4 份词表所在目录",
    )
    parser.add_argument("--out", type=Path, default=OUT)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    audit, audit_sha = read(args.audit)
    upgrades, upgrade_sha = read(args.upgrades)
    drops, drop_sha = read(args.drops)
    recipes, recipe_sha = read(args.recipes)
    hashes = {
        "wiki_intro.json": hashlib.sha256((DATA / "wiki_intro.json").read_bytes()).hexdigest(),
        "ExportUpgrades_en.json": upgrade_sha,
        "scripts/build_drops.py": hashlib.sha256(
            (ROOT / "scripts/build_drops.py").read_bytes()
        ).hexdigest(),
    }
    tables = {}
    for key in ("languages_en", "languages_zh", "name_en", "name_zh"):
        tables[key], hashes[key + ".json"] = read(args.source_tables / (key + ".json"))
    expected = audit["meta"]["输入文件SHA256"]
    if hashes != expected:
        raise SystemExit(
            "原文核对表与输入版本不一致："
            + "、".join(k for k in hashes if hashes[k] != expected.get(k))
        )
    with i18n.use_lang("en"):
        result, counts = build(audit, upgrades, drops, recipes, tables)
    result["source_sha256"] = {
        **hashes,
        "audit": audit_sha,
        "WFCD all.slim.json": drop_sha,
        "PEP ExportRecipes.json": recipe_sha,
    }
    result["coverage"] = dict(counts)
    payload = json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    print(
        json.dumps(
            {"覆盖": counts, "输入 SHA256": result["source_sha256"]}, ensure_ascii=False, indent=2
        )
    )
    print(f"输出 UTF-8 {len(payload.encode('utf-8'))} 字节")
    if args.check:
        return 0 if args.out.read_text(encoding="utf-8") == payload else 1
    args.out.write_text(payload, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
