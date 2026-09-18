# -*- coding: utf-8 -*-
"""遗物卡片本地预览（不依赖 astrbot，直接调 formatters + render）。

用法：
    python scripts/dev_relic_preview.py                     # 出库 + 入库两张
    python scripts/dev_relic_preview.py --which 出库
    python scripts/dev_relic_preview.py --piece 电幻步枪Prime蓝图
    python scripts/dev_relic_preview.py --piece all          # 4 张代表性部件卡

产物：runtime/relic_<名称>.png
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import drops as drops_db          # noqa: E402
from core import formatters as fmt          # noqa: E402
from core import render as R                # noqa: E402

IDX = json.loads((ROOT / "core" / "data" / "relic_index.json")
                 .read_text(encoding="utf-8"))
INV = json.loads((ROOT / "core" / "data" / "relic_inverse.json")
                 .read_text(encoding="utf-8"))
TIER_EN = {"古纪": "lith", "前纪": "meso", "中纪": "neo",
           "后纪": "axi", "安魂": "requiem", "全能": "omnia"}

# 阿耶（Varzia）当前在售的遗物 —— 真实值由 main.py 从 DE 拉，
# 预览脚本用实测快照代替（2026-09-17 实测）。
VARZIA = ["古纪 K5", "古纪 M7", "前纪 E5", "中纪 B6", "后纪 H5", "后纪 A12"]


def _uv_and_keys():
    """返回 (可掉落集合, 阿耶集合)，键形如 ``lith a12``。"""
    vz = {fmt.relic_en_key(n) for n in VARZIA}
    return drops_db.droppable_keys(), {k for k in vz if k}


def build_rows(which: str):
    uv, vz = _uv_and_keys()
    alluv = uv | vz
    unv_rows, vaulted_rows = [], []
    for k in IDX:
        m = k.split()
        if len(m) < 3:
            continue
        key = f"{TIER_EN.get(m[0], m[0].lower())} {m[2].lower()}"
        row = {"cn": k, "tier_cn": m[0],
               "unvaulted": key in alluv, "varzia": key in vz}
        (unv_rows if row["unvaulted"] else vaulted_rows).append(row)
    return (unv_rows if which == "出库" else vaulted_rows), vz


def build_piece_rows(piece: str):
    """与 main._h_relic 的部件分支同口径：三态 + 排序。"""
    uv, vz = _uv_and_keys()
    rows = []
    for o in INV.get(piece) or []:
        key = fmt.relic_en_key(o.get("relic") or "")
        state = ("drop" if key and key in uv
                 else "varzia" if key and key in vz else "vaulted")
        rows.append({"relic": o.get("relic"), "rarity": o.get("rarity"),
                     "state": state})
    return rows


def _render(name: str, title: str, lines: list[str], out_dir: Path) -> Path:
    r = R.ImageRenderer(out_dir)
    if not r.available:
        print("[SKIP] 渲染器不可用（缺字体）")
        return Path()
    p = r.render(title, lines, "国际服")
    dst = out_dir / f"relic_{name}.png"
    Path(p).replace(dst)
    print(f"已渲染: {dst}  ({dst.stat().st_size / 1024:.0f} KB)")
    return dst


def preview_list(which: str, page: int, out_dir: Path) -> int:
    rows, _vz = build_rows(which)
    specials: dict[str, str] = {}
    for r in rows:
        if r.get("varzia"):
            specials[fmt.relic_cn(r["cn"])] = "仅阿耶兑换"
            continue
        m = r["cn"].split()
        if len(m) >= 3:
            key = f"{TIER_EN.get(m[0], m[0].lower())} {m[2].lower()} relic"
            why = drops_db.special_source(key)
            if why:
                specials[fmt.relic_cn(r["cn"])] = why

    title = {"出库": "遗物出库（当前可掉落）",
             "入库": "遗物入库（已入库、不可刷取）",
             "列表": "遗物列表"}[which]
    t, lines = fmt.fmt_relic_by_tier(
        rows, title, page=page, page_size=90,
        farm_hints=drops_db.farm_hints() if which == "出库" else None,
        specials=specials or None)
    print(f"=== {t} ===")
    for ln in lines:
        print("  " + ln)
    print()
    _render(which, t, lines, out_dir)
    return 0


def preview_piece(piece: str, out_dir: Path) -> int:
    rows = build_piece_rows(piece)
    if not rows:
        print(f"[SKIP] 部件反查表里没有「{piece}」")
        return 1
    t, lines = fmt.fmt_relic_piece(piece, rows, farm_hints=drops_db.farm_hints())
    print(f"=== {t} ===")
    for ln in lines:
        print("  " + ln)
    print()
    _render(f"部件_{piece}", t, lines, out_dir)
    return 0


# 四类代表性部件：全可掉落 / 混合 / 全入库 / 超大列表（545 把）
_PIECE_SAMPLES = ("Styanax Prime蓝图", "电幻步枪Prime蓝图",
                  "绝路Prime枪管", "Forma蓝图")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--which", default="出库", choices=["出库", "入库", "列表"])
    ap.add_argument("--page", type=int, default=1)
    ap.add_argument("--piece", help="部件名，或 all（4 张代表性部件卡）")
    args = ap.parse_args()

    out_dir = ROOT / "runtime"
    out_dir.mkdir(exist_ok=True)
    if args.piece:
        names = _PIECE_SAMPLES if args.piece == "all" else (args.piece,)
        rc = 0
        for n in names:
            rc |= preview_piece(n, out_dir)
        return rc
    return preview_list(args.which, args.page, out_dir)


if __name__ == "__main__":
    sys.exit(main())
