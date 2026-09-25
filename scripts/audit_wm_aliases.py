# -*- coding: utf-8 -*-
"""审计 core/data/aliases.json 的 wm_items 表（python3 scripts/audit_wm_aliases.py）

查两类**硬错误**：

A. 词典键本身就是某物品的「官方简中名 / 官方英文名」，却指向了**另一个**物品。
   例：`'压迫点' -> serration` —— 官方简中「压迫点」是 Pressure Point，
   而 serration 是「膛线」（2026-09-20 用户实测报障）。这类错误会让用户
   「照官方名输入却拿到完全不相关的东西」，危害最大。

B. 词典指向的 slug 在 WM 物品表里不存在（物品已更名 / 绝版 / 从未上线）。
   注意：B 里有一部分是**正常的**（如 Excalibur Prime 绝版，WM 本就没有），
   需要人工判断，脚本只列出来。

黑话（DJ甲 / 一拳超人 / 主教 …）不会被 A 命中 —— 它们不是官方名，
其正确性只能靠社区来源逐条核实，脚本不判断。

只用标准库（不依赖 httpx，方便在任何环境跑）。
"""
from __future__ import annotations

import json
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

WM_ITEMS_URL = "https://api.warframe.market/v2/items"

# ★ 已知「WM 上本来就没有这个类目」—— 映射本身是对的，查不到属预期，不算错误。
#   （2026-09-20 用户确认：但丁=Dante、咖喱=Excalibur 对应正确，
#    只是 Dante/Jade 的 Prime 未出、Excalibur Prime 已绝版，WM 无从挂单。）
KNOWN_ABSENT = {
    "excalibur_prime_set": "Excalibur Prime 已绝版，WM 无此类目（咖喱/咖喱棒 = Excalibur ✅）",
    "dante_prime_set": "Dante Prime 尚未推出，WM 无此类目（但丁 ✅）",
    "jade_prime_set": "Jade Prime 尚未推出，WM 无此类目（国服译名「捷德」✅）",
}


def norm(s: str) -> str:
    return re.sub(r"\s+", "", (s or "").lower())


def fetch_items() -> list[dict]:
    req = urllib.request.Request(WM_ITEMS_URL, headers={
        "User-Agent": "wfq-alias-audit", "Accept": "application/json",
        "Language": "zh-hans"})
    raw = json.load(urllib.request.urlopen(req, timeout=90))
    return raw.get("data") or []


def main() -> int:
    table = json.loads((ROOT / "core" / "data" / "aliases.json").read_text(
        encoding="utf-8")).get("wm_items", {})
    print("拉取 WM 物品表 …", file=sys.stderr)
    items_raw = fetch_items()

    by_url, by_zh, by_en = {}, {}, {}
    for it in items_raw:
        i18n = it.get("i18n") or {}
        zh = (i18n.get("zh-hans") or {}).get("name", "")
        en = (i18n.get("en") or {}).get("name", "")
        rec = {"url_name": it.get("slug", ""), "zh": zh, "en": en}
        by_url[rec["url_name"]] = rec
        if zh:
            by_zh.setdefault(norm(zh), []).append(rec)
        if en:
            by_en.setdefault(en.lower(), []).append(rec)

    bad, dangling = [], []
    for key, url in sorted(table.items()):
        if url not in by_url:
            dangling.append((key, url))
            continue
        hit = None
        for rec in by_zh.get(norm(key), []):
            if rec["url_name"] != url:
                hit = (rec, "官方简中名")
                break
        if hit is None:
            for rec in by_en.get(key.strip().lower(), []):
                if rec["url_name"] != url:
                    hit = (rec, "官方英文名")
                    break
        if hit:
            bad.append((key, url, hit[0], hit[1]))

    print("=" * 72)
    print(f"A. 官方名被指向了别的物品（硬错误）: {len(bad)} 条")
    print("=" * 72)
    for key, url, rec, kind in bad:
        print(f"  '{key}'  ({kind})")
        print(f"      词典指向 → {url} = {by_url[url]['zh']} / {by_url[url]['en']}")
        print(f"      应当是   → {rec['url_name']} = {rec['zh']} / {rec['en']}")

    print()
    print("=" * 72)
    unknown_dangling = [(k, u) for k, u in dangling if u not in KNOWN_ABSENT]
    print(f"B. 指向 WM 表里不存在的 slug: {len(dangling)} 条"
          f"（其中 {len(dangling) - len(unknown_dangling)} 条属已知正常）")
    print("=" * 72)
    for key, url in dangling:
        if url in KNOWN_ABSENT:
            print(f"  ○ '{key}' → {url}")
            print(f"      已知正常：{KNOWN_ABSENT[url]}")
        else:
            hint = ""
            for rec in by_zh.get(norm(key), []):
                hint = f" → 疑似应为 {rec['url_name']}（{rec['zh']}）"
            print(f"  ⚠ '{key}' → {url}{hint}")

    print()
    if unknown_dangling:
        print(f"需要处理的断链 {len(unknown_dangling)} 条（上面标 ⚠ 的）。")
    else:
        print("断链已全部归类为「已知正常」，无待处理项。")
    return 1 if (bad or unknown_dangling) else 0


if __name__ == "__main__":
    sys.exit(main())
