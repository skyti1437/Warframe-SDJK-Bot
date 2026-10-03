# -*- coding: utf-8 -*-
"""构建期：StoreItems 路径 → 官方简中名反查表 core/data/de/storeitems_zh.json。

背景（2026-10-04，警报奖励翻译修复）：
  `core/api_client.py::_resolve_alert_items` 原来只用 WM `gameRef` 换中文，
  而 WM 物品表是**可交易品** —— 装饰品（ShipDecos）不在其中 ⇒ 警报奖励回落
  `_prettify` 出 "Plushy2021 QTCC" 这类怪名。官方中文其实有，在 DE 官方导出里：

    ExportResources.json：StoreItems 路径 → name（Language 键）
    dict.zh.json        ：Language 键 → 官方简中

  ★ 键名**不规则**（实测）：Plushy2021QTCC → Plush`2021`QTCCName（掉 y）；
    PlushyVirminkQTCC → PlushyVirminkQTCC`Deco`Name（多 Deco）
    ⇒ 绝不能用「路径末段 + Name」硬拼，必须用成对的权威导出建表。

筛选口径：`productCategory == 'ShipDecorations'`（装饰品；警报奖励的典型类）。
输入：core/data/_cache/ExportResources.json + core/data/_cache/dict.zh.json
     （两者均 gitignore + 不进包；ExportResources 可用 --pep 指向别处）
输出：core/data/de/storeitems_zh.json（随包分发）

用法：python scripts/build_storeitems_zh.py [--pep <dir>] [--check]
  --check 只校验不写盘（回归用）。
"""
from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "core" / "data" / "_cache"
OUT = ROOT / "core" / "data" / "de" / "storeitems_zh.json"
CATEGORY = "ShipDecorations"

# 哨兵：这三条是 2026-10-04 实测的线上警报奖励（键名不规则，最能防回归）
SENTINEL = {
    "/Lotus/Types/Items/ShipDecos/Plushies/Plushy2021QTCC": "征服库阿卡玩偶",
    "/Lotus/Types/Items/ShipDecos/Plushies/Plushy2022QTCC": "征服梭歌玩偶",
    "/Lotus/Types/Items/ShipDecos/Plushies/PlushyVirminkQTCC": "征服弗鸣克玩偶",
}


def build(pep_dir: Path) -> dict:
    res = json.loads((pep_dir / "ExportResources.json").read_text(encoding="utf-8"))
    dz = json.loads((CACHE / "dict.zh.json").read_text(encoding="utf-8"))
    items: dict[str, str] = {}
    unresolved: list[str] = []
    for path, rec in res.items():
        if not isinstance(rec, dict) or rec.get("productCategory") != CATEGORY:
            continue
        key = rec.get("name") or ""
        zh = dz.get(key) if key else None
        if zh:
            items[path] = zh
        else:
            unresolved.append(path)
    return {"items": dict(sorted(items.items())),
            "unresolved": unresolved}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pep", default=str(CACHE), help="ExportResources.json 所在目录")
    ap.add_argument("--check", action="store_true", help="只校验不写盘")
    args = ap.parse_args()
    data = build(Path(args.pep))
    items = data["items"]
    bad = {k: v for k, v in SENTINEL.items() if items.get(k) != v}
    print("ShipDecorations 条目：%d 条（未解析 %d）" % (len(items), len(data["unresolved"])))
    print("哨兵三条：%s" % ("全部命中 ✔" if not bad else "!! 未命中 %s" % bad))
    if bad:
        return 1
    payload = {
        "_meta": {
            "purpose": "StoreItems 路径 → 官方简中名（警报奖励等换名用；三级回落第一级）",
            "source": "warframe-public-export-plus：ExportResources(productCategory="
                      "ShipDecorations) + dict.zh（本地 _work/data/pep）",
            "built": datetime.date.today().isoformat(),
            "count": len(items),
            "unresolved": len(data["unresolved"]),
        },
        "items": items,
    }
    if args.check:
        cur = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {}
        same = cur.get("items") == items
        print("--check：与现表一致 =", same)
        return 0 if same else 1
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n",
                   encoding="utf-8", newline="\n")
    print("写出 %s（%d KB）" % (OUT.relative_to(ROOT), OUT.stat().st_size // 1024))
    return 0


if __name__ == "__main__":
    sys.exit(main())
