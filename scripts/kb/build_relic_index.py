#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从公开 WFCD 快照生成遗物奖励/反查表，并逐条对拍 DE 官方掉落页。

默认 WFCD URL 固定提交；官方页为实时页（打印 SHA256，支持 --official 离线复跑）。
译名复用仓库内官方简中表和已提取的 zh_overrides_items.json，不依赖本机解包。
完整档概率分槽：>20% 常见、>5% 罕见、其余稀有；这不是掉落页的原始 rarity 标签。
安魂沿用原分槽口径，仅报告三方差异，等待裁决（R7）。

用法：python scripts/kb/build_relic_index.py [--src Relics.json] [--official droptables.html]
      [--out-dir core/data] [--dry-run | --check] [--audit-json /tmp/relic-audit.json]
--check 重新生成并逐字节比较两张仓库表，差异时非零退出；不写入产物。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import urllib.request
from collections import defaultdict
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WFCD_REF = "d3afbf06ba7f249e8e14c3c1c7433cae81e3fc67"
SRC_URL = f"https://raw.githubusercontent.com/WFCD/warframe-items/{WFCD_REF}/data/json/Relics.json"
OFFICIAL_URL = "https://www.warframe.com/droptables"
ERA_ZH = {
    "Lith": "古纪",
    "Meso": "前纪",
    "Neo": "中纪",
    "Axi": "后纪",
    "Requiem": "安魂",
    "Vanguard": "先锋",
    "Omnia": "全能",
    "Void": "虚空",
}
_ERA_ORDER = list(ERA_ZH.values())
_RELIC_NAME_RE = re.compile(r"^(.+?)\s+(Intact|Exceptional|Flawless|Radiant)$")
CHANCE_RE = re.compile(r"^(.+?)\s+\(([\d.]+)%\)$")


class DropTableParser(HTMLParser):
    """按 h3 id 收集表格行；HTMLParser 负责实体解码和嵌套标签去除。"""

    def __init__(self):
        super().__init__()
        self.sections = defaultdict(list)
        self.section = ""
        self.row = None
        self.cell = None

    def handle_starttag(self, tag, attrs):
        if tag == "h3":
            self.section = dict(attrs).get("id", "")
        elif tag == "tr":
            self.row = []
        elif tag in ("td", "th"):
            self.cell = []
        elif tag == "br" and self.cell is not None:
            self.cell.append(" ")

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self.cell is not None:
            if self.row is not None:
                self.row.append(" ".join("".join(self.cell).split()))
            self.cell = None
        elif tag == "tr" and self.row is not None:
            if self.section:
                self.sections[self.section].append(self.row)
            self.row = None


def official_sections(html: str) -> dict:
    parser = DropTableParser()
    parser.feed(html)
    return dict(parser.sections)


def read_source(path: Path | None, url: str) -> bytes:
    if path:
        raw = path.read_bytes()
    else:
        req = urllib.request.Request(url, headers={"User-Agent": "warframe-relic-build/1.0"})
        with urllib.request.urlopen(req, timeout=90) as response:
            raw = response.read()
    print(f"来源 {path or url}\n  SHA256 {hashlib.sha256(raw).hexdigest()}")
    return raw


def relic_key(name: str) -> str:
    era, code = name.replace(" Relic", "").split()
    code = code.upper() if re.fullmatch(r"[IVX]+|[A-Za-z]+\d+", code, re.I) else code
    return f"{ERA_ZH[era]} {era} {code}"


def _relic_sort_key(key: str):
    parts = key.split()
    era = _ERA_ORDER.index(parts[0]) if parts[0] in _ERA_ORDER else 99
    code = parts[-1]
    m = re.fullmatch(r"([A-Za-z]+)(\d+)", code)
    return (era, m.group(1) if m else code, int(m.group(2)) if m else 0)


def rarity_of(chance: float) -> str:
    if chance > 20:
        return "常见"
    if chance > 5:
        return "罕见"
    return "稀有"


def parse_official_relics(html: str) -> dict:
    out, current = {}, None
    for row in official_sections(html).get("relicRewards", []):
        if len(row) == 1 and row[0]:
            m = re.fullmatch(r"(.+?) Relic \((Intact|Exceptional|Flawless|Radiant)\)", row[0])
            current = relic_key(m[1]) if m and m[2] == "Intact" and len(m[1].split()) == 2 else None
            if current:
                if current in out:
                    raise ValueError(f"官方完整档重复：{current}")
                out[current] = []
        elif current and len(row) == 2 and (m := CHANCE_RE.fullmatch(row[1])):
            out[current].append((row[0], float(m[2]), m[1]))
    if not out or any(not rows for rows in out.values()):
        raise ValueError("官方遗物表为空或包含空奖励，拒绝覆盖")
    return out


def wfcd_relics(items: list) -> tuple[dict, dict]:
    out, paths = {}, {}
    for item in items:
        for reward in item.get("rewards") or []:
            paths[reward["item"]["name"]] = reward["item"]["uniqueName"].lower()
        m = _RELIC_NAME_RE.fullmatch(item["name"])
        if m and m[2] == "Intact":
            key = relic_key(m[1])
        elif not m and item.get("rewards"):
            key = relic_key(item["name"])
        else:
            continue
        out[key] = [(r["item"]["name"], r["chance"], r["rarity"]) for r in item["rewards"]]
    if not out:
        raise ValueError("WFCD 完整档为空，拒绝覆盖")
    return out, paths


def load_names() -> dict:
    base = ROOT / "core/data/de"
    names = json.loads((base / "name_zh_wfcd.json").read_text(encoding="utf-8"))["names"]
    names.update(json.loads((base / "name_zh.json").read_text(encoding="utf-8")))
    overrides = json.loads(
        Path(__file__).with_name("zh_overrides_items.json").read_text(encoding="utf-8")
    )
    names.update({k.lower(): v for k, v in overrides["items"].items()})
    return names


def translated(name: str, paths: dict, names: dict, *, quantity: bool = True) -> str:
    # 上游数量前缀不属于物品路径；同一路径的 1/2 张 Forma 蓝图必须分键。
    m = re.fullmatch(r"(\d+)X\s+(.+)", name)
    path = paths.get(name) or paths.get(m[2] if m else name)
    zh = names.get(path)
    if path and path.endswith("blueprint"):
        component = names.get(path.removesuffix("blueprint") + "component")
        if component and " Prime " in component:
            zh = component + "蓝图"
    if zh and name.endswith("Neuroptics Blueprint"):
        zh = re.sub(r"头盔\s*蓝图$", "头部神经光元蓝图", zh)
    if not zh:
        raise ValueError(f"缺少官方简中译名：{name}（{path}）")
    return f"{m[1]}X {zh}" if m and quantity else zh


def slots_of(rows: list, paths: dict, names: dict, key: str) -> dict:
    slots = defaultdict(list)
    for name, chance, _label in rows:
        nm = translated(name, paths, names, quantity=not key.startswith("安魂 "))
        slots[rarity_of(chance)].append((-chance, nm))
    return {r: [nm for _c, nm in sorted(slots[r])] for r in ("常见", "罕见", "稀有") if slots[r]}


def build(items: list, html: str, names: dict) -> tuple[dict, dict, dict]:
    wfcd, paths = wfcd_relics(items)
    official = parse_official_relics(html)
    conflicts = {
        k: {"wfcd": wfcd.get(k), "official": official.get(k)}
        for k in sorted(wfcd.keys() | official.keys(), key=_relic_sort_key)
        if sorted(wfcd.get(k, [])) != sorted(official.get(k, []))
    }
    index = {}
    for key in sorted(wfcd.keys() | official.keys(), key=_relic_sort_key):
        # 安魂维持既有概率分槽，R7 的原始标签分歧仅进审计文件。
        rows = wfcd.get(key) if key.startswith("安魂 ") else official.get(key, wfcd.get(key))
        if not rows:
            raise ValueError(f"缺少奖励：{key}")
        if not key.startswith("安魂 ") and len(rows) != 6:
            raise ValueError(f"普通遗物必须有 6 个奖励：{key} = {len(rows)}")
        index[key] = slots_of(rows, paths, names, key)
    inverse = defaultdict(list)
    for key, slots in index.items():
        for rarity, rewards in slots.items():
            for name in rewards:
                inverse[name].append({"relic": key, "rarity": rarity})
    inverse = dict(sorted(inverse.items()))
    comparable = {k: slots_of(v, paths, names, k) for k, v in official.items()}
    audit = {
        "official_total": len(official),
        "wfcd_total": len(wfcd),
        "conflicts": conflicts,
        "official_missing": sorted(wfcd.keys() - official.keys(), key=_relic_sort_key),
        "official_slots": comparable,
        "requiem": {
            k: {"official": official.get(k), "wfcd": v, "generated": index[k]}
            for k, v in wfcd.items()
            if k.startswith("安魂 ")
        },
    }
    return index, inverse, audit


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--src", type=Path, help="WFCD Relics.json（默认固定提交 URL）")
    ap.add_argument("--official", type=Path, help="DE 掉落页 HTML（默认实时 URL）")
    ap.add_argument("--out-dir", type=Path, default=ROOT / "core/data")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--check", action="store_true")
    ap.add_argument("--audit-json", type=Path, help="另存完整对拍/冲突清单（不属于运行期表）")
    args = ap.parse_args()
    raw = read_source(args.src, SRC_URL)
    page = read_source(args.official, OFFICIAL_URL)
    index, inverse, audit = build(json.loads(raw), page.decode("utf-8"), load_names())
    audit["sources"] = {
        "wfcd": SRC_URL,
        "official": OFFICIAL_URL,
        "wfcd_sha256": hashlib.sha256(raw).hexdigest(),
        "official_sha256": hashlib.sha256(page).hexdigest(),
    }
    old_path = args.out_dir / "relic_index.json"
    old = json.loads(old_path.read_text(encoding="utf-8")) if old_path.exists() else {}
    changed = {k: {"before": old.get(k), "after": v} for k, v in index.items() if old.get(k) != v}
    audit["changed"] = changed
    audit["official_before_equal"] = sum(
        old.get(k) == v for k, v in audit["official_slots"].items()
    )
    audit["official_after_equal"] = sum(
        index.get(k) == v for k, v in audit["official_slots"].items()
    )
    print(f"遗物 {len(old)} → {len(index)}；奖励变更 {len(changed)}；部件 {len(inverse)}")
    print(
        f"官方对拍 {audit['official_total']}：原表一致 {audit['official_before_equal']}；"
        f"生成后一致 {audit['official_after_equal']}；两源冲突 {len(audit['conflicts'])}"
    )
    for key, conflict in audit["conflicts"].items():
        print(f"  两源冲突 {key}：{conflict}")
    print("  修正清单：" + "、".join(changed))
    if args.audit_json:
        args.audit_json.write_text(
            json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    same = True
    for filename, data in (("relic_index.json", index), ("relic_inverse.json", inverse)):
        path = args.out_dir / filename
        text = json.dumps(data, ensure_ascii=False, indent=1)
        equal = path.is_file() and path.read_text(encoding="utf-8") == text
        same &= equal
        if args.check:
            print(f"{'✓' if equal else '✗'} --check {filename}")
        elif not args.dry_run:
            args.out_dir.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
    return 0 if not args.check or same else 1


if __name__ == "__main__":
    raise SystemExit(main())
