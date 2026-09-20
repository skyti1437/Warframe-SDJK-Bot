#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""豆子（MOD 等级刻度）检测的命令行入口 —— 实现见 `core/pips.py`。

用法：
  python scripts/pips_detect.py <图片> [...] [--viz 目录]

输出每个装备区行的「每列亮豆数」（= 该卡等级，0 = 0 级）；
仓库区会被标出并建议忽略。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image  # noqa: E402

from core.pips import detect_pips  # noqa: E402


def _viz(img, rows, out_dir: Path, stem: str) -> None:
    """把检测到的豆位置标红存图（肉眼核对用）。"""
    v = img.convert("RGB").copy()
    vp = v.load()
    W, H = v.size
    for r in rows:
        y0, y1 = r["band"]
        for plist in r.get("pos") or []:
            for cx in plist:
                for dy in range(max(0, y0 - 10), min(H, y1 + 10)):
                    for dx in (-1, 0, 1):
                        if 0 <= cx + dx < W:
                            vp[cx + dx, dy] = (255, 0, 0)
    out_dir.mkdir(parents=True, exist_ok=True)
    v.save(str(out_dir / f"{stem}_pips.png"))


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    viz = None
    if "--viz" in sys.argv:
        viz = sys.argv[sys.argv.index("--viz") + 1]
        args = [a for a in args if a != viz]
    if not args:
        print(__doc__)
        return 1
    for p in args:
        im = Image.open(p)
        rows = detect_pips(im)
        print(f"===== {Path(p).name}  {im.size[0]}×{im.size[1]} =====")
        if not rows:
            print("  （未检出：图太小 / 不是配卡界面）")
        for r in rows:
            tag = "  【仓库区，忽略】" if r["is_inventory"] else ""
            print(f"  行{r['row']} y{r['band']} 豆数 {r['counts']}{tag}")
        if viz:
            _viz(im, rows, Path(viz), Path(p).stem)
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
