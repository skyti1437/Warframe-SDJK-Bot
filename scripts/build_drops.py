#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从公开 WFCD 固定快照重建掉落表，任务表以 DE 官方页覆盖校准。

命令：python scripts/build_drops.py [--src all.slim.json] [--official droptables.html]
      [--out core/data/drops.json] [--dry-run | --check]
地点/遗物/items 同趟编号；旧 items 键可保留供搜索，但值绝不续传。
任务译名直接复用 core.parser.MISSION_CN；活动与商人来源单独标注，不视作常驻掉落。
所有源打印 SHA256；--src/--official 可用保存的源文件离线复跑。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "core" / "data" / "drops.json"
WFCD_REF = "3263b2dff8bc1b434a191f0364395b156bd26561"
SRC_URL = f"https://raw.githubusercontent.com/WFCD/warframe-drop-data/{WFCD_REF}/data/all.slim.json"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts/kb"))
from core.parser import MISSION_CN  # noqa: E402
from build_relic_index import CHANCE_RE, OFFICIAL_URL, official_sections, read_source  # noqa: E402

# 英文 wiki 的 Baro Exclusive Relics 清单（轮换出售，不表示本期在售）：
# https://wiki.warframe.com/w/Void_Relic#Baro_Exclusive_Relics
BARO_RELICS = ("axi a2 relic", "axi a5 relic", "axi m5 relic", "axi v8 relic", "neo o1 relic")

PLACE_RE = re.compile(
    r"^(?P<planet>[^/]+)/(?P<node>.+?)\s*\((?P<mode>[^)]+)\)"
    r"(?:,\s*Rotation\s+(?P<rot>[ABC]))?\s*$"
)

PLANET_ZH = {
    "Mercury": "水星",
    "Venus": "金星",
    "Earth": "地球",
    "Mars": "火星",
    "Phobos": "火卫一",
    "Ceres": "谷神星",
    "Jupiter": "木星",
    "Saturn": "土星",
    "Uranus": "天王星",
    "Neptune": "海王星",
    "Pluto": "冥王星",
    "Sedna": "塞德娜",
    "Eris": "阋神星",
    "Europa": "木卫二",
    "Lua": "月球",
    "Deimos": "火卫二",
    "Void": "虚空",
    "Kuva Fortress": "女皇要塞",
    "Zariman": "扎利曼",
    "Höllvania": "霍瓦尼亚",
    "Earth Proxima": "地球比邻星域",
    "Venus Proxima": "金星比邻星域",
    "Saturn Proxima": "土星比邻星域",
    "Neptune Proxima": "海王星比邻星域",
    "Pluto Proxima": "冥王星比邻星域",
    "Veil Proxima": "面纱比邻星域",
    "Ceres Proxima": "谷神星比邻星域",
    "Jupiter Proxima": "木星比邻星域",
}

# DE 官方任务词表单源；仅补充该词表没有的奖励点类别。
MODE_ZH = {k.title(): v for k, v in MISSION_CN.items()}
MODE_ZH.update(
    {"Caches": "储藏库", "Vault": "宝藏", "Volatile": "爆发", "Conjunction Survival": "交汇生存"}
)
# 「Level a - b X」形态的模式：保留等级前缀 + 译尾（与旧数据既有标签同风格，
# 例：'Level 30 - 40 隔离库' —— 旧标签就是这么写的，这里把它**按模式**写对）
MODE_PATTERNS = (
    (re.compile(r"^Level\s+(\d+)\s*-\s*(\d+)\s+Arcana Isolation Vault$"), r"Level \1 - \2 隔离库"),
    (re.compile(r"^Level\s+(\d+)\s*-\s*(\d+)\s+Isolation Vault$"), r"Level \1 - \2 隔离库"),
    (re.compile(r"^Level\s+(\d+)\s*-\s*(\d+)\s+Cambion Drift Bounty$"), r"Level \1 - \2 赏金"),
    (re.compile(r"^Level\s+(\d+)\s*-\s*(\d+)\s+Entrati Lab Bounty$"), r"Level \1 - \2 实验室赏金"),
    (re.compile(r"^Level\s+(\d+)\s*-\s*(\d+)\s+Plague Star$"), r"Level \1 - \2 瘟疫之星"),
)


def mode_zh(mode: str) -> str:
    """任务模式 → 中文（MODE_ZH → 「Level a - b X」模式化翻译 → 原样英文）。"""
    if mode.lower() in MISSION_CN:
        return MISSION_CN[mode.lower()]
    if mode in MODE_ZH:
        return MODE_ZH[mode]
    for pat, repl in MODE_PATTERNS:
        if pat.match(mode or ""):
            return pat.sub(repl, mode)
    return mode


def strip_tags(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s or "").strip()


def parse_place(place: str):
    """'Mercury/Apollodorus (<b>Survival</b>), Rotation A' → (星球, 节点, 模式, 轮次|'')。"""
    m = PLACE_RE.match(strip_tags(place))
    if not m:
        return None
    return (
        m.group("planet").strip(),
        m.group("node").strip(),
        m.group("mode").strip(),
        m.group("rot") or "",
    )


def base_relic_key(item: str) -> "str | None":
    """'Lith Q3 Relic (Radiant)' → 'lith q3 relic'；非遗物返回 None。"""
    low = (item or "").strip().lower()
    low = re.sub(r"\s*\((?:intact|exceptional|flawless|radiant)\)\s*$", "", low)
    return (
        low
        if re.fullmatch(
            r"(?:lith|meso|neo|axi|requiem|vanguard|omnia) (?:[a-z]+\d+|[ivx]+|eterna) relic", low
        )
        else None
    )


def parse_any(place: str):
    """通用解析：常规地点走 parse_place；活动/剧情任务（无星球结构，
    如「Hot Mess, Rotation C」）走 raw 通道——(\":raw\", 任务名, \"\", 轮次)。"""
    pp = parse_place(place)
    if pp:
        return pp
    s = strip_tags(place)
    m = re.match(r"^(.*?),\s*Rotation\s+([ABC])\s*$", s)
    if m:
        return (":raw", m.group(1).strip(), "", m.group(2))
    return (":raw", s.strip(), "", "")


def official_mission_rows(html: str) -> list[dict]:
    rows, place, rotation = [], "", ""
    for cells in official_sections(html).get("missionRewards", []):
        if len(cells) == 1 and cells[0]:
            if cells[0].startswith("Rotation "):
                rotation = ", " + cells[0]
            else:
                place, rotation = cells[0], ""
        elif place and len(cells) == 2 and (m := CHANCE_RE.fullmatch(cells[1])):
            rows.append(
                {"place": place + rotation, "item": cells[0], "rarity": m[1], "chance": float(m[2])}
            )
    if not rows:
        raise ValueError("官方任务表为空，拒绝覆盖")
    return rows


def mission_identity(place: str) -> tuple:
    # Event 前缀是来源属性；只在官方/WFCD 表对拍时去除，展示时保留。
    planet, node, mode, _rot = parse_any(re.sub(r"^Event:\s*", "", strip_tags(place)))
    return planet.casefold(), node.casefold(), mode.casefold()


def apply_patches(slim: list, html: str) -> list:
    """官方任务表覆盖同名 WFCD 表；动态补缺表，避免硬编码奖励过期。"""
    official = official_mission_rows(html)
    identities = {mission_identity(r["place"]) for r in official}
    kept = [r for r in slim if mission_identity(r["place"]) not in identities]
    print(f"官方任务校准：WFCD {len(slim) - len(kept)} 行 → DE {len(official)} 行")
    return kept + official


def special_sources(html: str) -> dict:
    """官方敌人遗物表中的活动来源 + 已核对的奸商轮换来源。"""
    out = defaultdict(list)
    enemy = ""
    for cells in official_sections(html).get("relicByAvatar", []):
        if len(cells) == 2 and cells[1].startswith("Relic Drop Chance:"):
            enemy = cells[0]
        elif enemy and len(cells) == 3 and (key := base_relic_key(cells[1])):
            # 当前官方此节仅有 Hemocyte；未知敌人保留原名，不擅自归到活动。
            note = (
                "活动 · 瘟疫之星 · Hemocyte（活动开放时）"
                if enemy == "Hemocyte"
                else f"敌人 · {enemy}"
            )
            if note not in out[key]:
                out[key].append(note)
    for key in BARO_RELICS:
        out[key].append("奸商 · Baro Ki’Teer（轮换出售，以当期货单为准）")
    return dict(sorted(out.items()))


def place_label(key: tuple) -> str:
    planet, node, mode, rot = key
    prefix = ""
    if planet.startswith("Event: "):
        prefix, planet = "活动 · ", planet.removeprefix("Event: ")
    if planet == ":raw":
        if node.startswith("Event: "):
            prefix, node = "活动 · ", node.removeprefix("Event: ")
        label = node
    else:
        label = f"{PLANET_ZH.get(planet, planet)} · {node} · {mode_zh(mode)}"
    return prefix + label + (f" · {rot}轮" if rot else "")


def build(slim: list, html: str, old: dict) -> dict:
    slim = apply_patches(slim, html)
    keys = sorted({parse_any(r["place"]) for r in slim})
    # 只复用相同新标签的编号，不复用旧译名；先占旧编号再分配空号，避免碰撞。
    old_ids = {}
    for pid, label in old.get("places", {}).items():
        old_ids.setdefault(label, []).append(int(pid))
    key_pid, used = {}, set()
    for key in keys:
        ids = old_ids.get(place_label(key), [])
        pid = ids.pop(0) if ids else None
        if pid is not None and pid not in used:
            key_pid[key] = pid
            used.add(pid)
    next_pid = 0
    for key in keys:
        if key in key_pid:
            continue
        while next_pid in used:
            next_pid += 1
        key_pid[key] = next_pid
        used.add(next_pid)
    places = dict(
        sorted(
            ((str(pid), place_label(key)) for key, pid in key_pid.items()),
            key=lambda kv: int(kv[0]),
        )
    )
    items = {k: [] for k in old.get("items", {})}
    relic = defaultdict(list)
    for row in slim:
        item = row["item"].strip().lower()
        if not item:
            continue
        pid = key_pid[parse_any(row["place"])]
        record = [pid, row["rarity"], row["chance"]]
        bucket = items.setdefault(item, [])
        if record not in bucket:
            bucket.append(record)
        if key := base_relic_key(item):
            if record not in relic[key]:
                relic[key].append(record)
    zh2en = {zh: en for zh, en in old.get("zh2en", {}).items() if en in items}
    # 官方页落款固定构建日期，避免 --check 被每次运行的时钟差异打红。
    date = re.search(r"Last Update:</b>\s*([^<]+)", html)
    if not date:
        raise ValueError("官方页缺少更新时间")
    return {
        "source": SRC_URL,
        "official_source": OFFICIAL_URL,
        "updated": date[1].strip(),
        "places": places,
        "items": {k: sorted(v) for k, v in sorted(items.items())},
        "zh2en": zh2en,
        "relic_drops": {k: sorted(v) for k, v in sorted(relic.items())},
        "relic_special_sources": special_sources(html),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--src", type=Path, help="固定快照 all.slim.json")
    ap.add_argument("--official", type=Path, help="DE 官方掉落页 HTML")
    ap.add_argument("--out", type=Path, default=OUT)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--check", action="store_true")
    args = ap.parse_args()
    raw = read_source(args.src, SRC_URL)
    html = read_source(args.official, OFFICIAL_URL).decode("utf-8")
    old = json.loads(args.out.read_text(encoding="utf-8")) if args.out.exists() else {}
    slim = json.loads(raw)
    print(f"WFCD 源条目 {len(slim)}")
    payload = build(slim, html, old)
    for name in ("places", "items", "relic_drops"):
        print(f"{name} {len(old.get(name, {}))} → {len(payload[name])}")
    for name in ("items", "relic_drops"):
        print(
            f"{name} 行数 {sum(map(len, old.get(name, {}).values()))} → "
            f"{sum(map(len, payload[name].values()))}"
        )
    text = json.dumps(payload, ensure_ascii=False, indent=1)
    if args.check:
        same = args.out.is_file() and args.out.read_text(encoding="utf-8") == text
        print("✓ --check drops.json" if same else "✗ --check drops.json 不一致")
        return 0 if same else 1
    if not args.dry_run:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
