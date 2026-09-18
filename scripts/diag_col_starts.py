# -*- coding: utf-8 -*-
"""量化渲染图里每一行「第二列」的起始 x —— 验证列对齐是否真的生效。

方法：在每一行文字带里横向扫描，找出**最宽的一段空白**（列间隙），
空白之后的第一个文字像素即第二列起点。列对齐生效时所有行的该值应一致。

用法：python scripts/diag_col_starts.py runtime/help_preview.png
"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

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
    print(f"图 {W}x{H}\n")

    # 先按“整行有无文字”切出行带
    bands, cur = [], None
    for y in range(y0, H - 80):
        has = False
        left = None
        for x in range(x_lo, x_hi):
            if lum(*px[x, y]) > 115:
                has = True
                left = x
                break
        if not has:
            if cur:
                bands.append(cur)
                cur = None
            continue
        if cur is None:
            cur = [y, y, left]
        else:
            cur[1] = y
            cur[2] = min(cur[2], left)
    if cur:
        bands.append(cur)

    col2 = []
    for a, b, left in bands:
        if b - a < 6:
            continue
        ym = (a + b) // 2
        # 该行中间高度上，找最长的连续空白段（>25px）
        runs, s = [], None
        for x in range(x_lo, x_hi):
            blank = all(lum(*px[x, yy]) <= 115
                        for yy in range(a, min(b + 1, H)))
            if blank and s is None:
                s = x
            elif not blank and s is not None:
                # 列间隙是 24px（render 里写死的 gap），阈值必须小于它，
                # 否则会把真正的列间隙滤掉、只测到左列内部的半角空格。
                if x - s > 16:
                    runs.append((s, x))
                s = None
        if not runs:
            continue
        s, e = max(runs, key=lambda t: t[1] - t[0])
        # 空白之后若还有文字，e 就是第二列起点
        tail = any(lum(*px[x, ym]) > 115 for x in range(e, min(e + 80, x_hi)))
        if tail:
            col2.append((a, left, e, e - left))

    print(f"{'y':>6}  {'行左缘':>7}  {'第二列起点':>9}  {'相对偏移':>8}")
    print("-" * 40)
    for a, left, c2, off in col2:
        print(f"{a:>6}  {left:>7}  {c2:>9}  {off:>8}")

    vals = [c[2] for c in col2]
    if vals:
        print(f"\n第二列起点的分布：{Counter(vals).most_common(5)}")
        common = Counter(vals).most_common(1)[0][0]
        odd = [v for v in vals if abs(v - common) > 10]
        print(f"基准 {common}；偏离 >10px 的行数 = {len(odd)}"
              + (f"  值：{sorted(set(odd))}" if odd else ""))


if __name__ == "__main__":
    main()
