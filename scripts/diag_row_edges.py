# -*- coding: utf-8 -*-
"""量化渲染图里每一行的左边缘 x —— 用来判断「哪些行顶格、哪些行对齐」。

想法：列对齐的行左边缘应落在同一个 x（正文起点 + 圆点）；
被 `_wrap` 折断或走整行绘制的行，左边缘会明显更靠左/更靠右。
比肉眼看图可靠。

用法：python scripts/diag_row_edges.py runtime/help_preview.png [--y0 165]
"""
from __future__ import annotations

import sys
from collections import Counter

from PIL import Image


def lum(r, g, b):
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def main():
    path = sys.argv[1]
    y0 = 165
    if "--y0" in sys.argv:
        y0 = int(sys.argv[sys.argv.index("--y0") + 1])

    im = Image.open(path).convert("RGB")
    W, H = im.size
    px = im.load()
    x_lo, x_hi = 52, W - 52
    print(f"图 {W}x{H}  扫描区 x∈[{x_lo},{x_hi}]\n")

    # 每个 y 的最左亮像素；整行没有亮的 y 作为分段界
    per_y = []
    for y in range(y0, H - 80):
        left = None
        for x in range(x_lo, x_hi):
            if lum(*px[x, y]) > 115:
                left = x
                break
        per_y.append((y, left))

    bands = []
    cur = None
    for y, left in per_y:
        if left is None:
            if cur:
                bands.append(cur)
                cur = None
            continue
        if cur is None:
            cur = [y, y, left, left]
        else:
            cur[1] = y
            cur[2] = min(cur[2], left)
            cur[3] = max(cur[3], left)
    if cur:
        bands.append(cur)

    print(f"{'y 区间':>13}  {'最左 x':>7}  {'最右 x':>7}")
    print("-" * 34)
    for a, b, l, r in bands:
        if b - a < 6:          # 太薄的当作噪声/装饰
            continue
        print(f"{a:>5}-{b:<7}  {l:>7}  {r:>7}")

    xs = [b[2] for b in bands if b[1] - b[0] >= 6]
    if xs:
        print(f"\n最左 x 的分布（降序前三）：{Counter(xs).most_common(3)}")
        base = Counter(xs).most_common(1)[0][0]
        odd = sorted({x for x in xs if x < base - 10})
        print(f"基准左缘 {base}；明显偏左的行 x = {odd}")


if __name__ == "__main__":
    main()
