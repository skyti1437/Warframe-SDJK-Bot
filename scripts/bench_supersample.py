# -*- coding: utf-8 -*-
"""SS（超采样倍率）对拍：单张渲染耗时 + 画质（文字边缘彩色振铃）。

用法：
    python scripts/bench_supersample.py          # 3 vs 2
    python scripts/bench_supersample.py 2 1      # 自定义对比

背景：线上宿主机只有 2 核，SS=3 时画布 3360 宽、像素操作量是 SS=2 的 2.25 倍，
实测单张会被拖到 30 s+。降 SS 能提速，但会削弱「先放大再缩」的抗锯齿效果，
可能出现 LANCZOS 在高对比文字边缘的彩色振铃 —— 本脚本就是量化这件事的。
"""
from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TITLE = "仲裁时间表（第1/23页，共336条）"
BODY = [
    "12日12时　光理塔　虚空　拦截　奥罗金　A",
    "12日13时　Apollodorus　水星　生存　Infested",
    "12日14时　奥金工场　扎里曼号　虚空决战　Grineer",
    "12日15时　Stöfler　月球　防御　Grineer",
    "12日23时　Alator　火星　拦截　Grineer　S",
    "13日00时　Cinxia　谷神星　拦截　Grineer　A+",
] * 3


def bench(ss: int):
    """返回 (最短耗时秒, 出图路径)。"""
    for m in [m for m in sys.modules if m.startswith("core")]:
        del sys.modules[m]
    from core import render as R

    R.SS = ss
    d = Path(tempfile.mkdtemp(prefix=f"ss{ss}_"))
    r = R.ImageRenderer(d)
    if not r.available:
        return None, None
    r.render(TITLE, BODY, "")          # 预热（字体/渐变缓存）
    ts, p = [], None
    for _ in range(3):
        t0 = time.perf_counter()
        p = r.render(TITLE, BODY, "")
        ts.append(time.perf_counter() - t0)
    return min(ts), p


def ringing(path: str):
    """量化文字边缘彩色振铃：亮部像素的最大通道间差与超标像素数。"""
    im = Image.open(path).convert("RGB")
    W, H = im.size
    px = im.load()
    worst, cnt = 0, 0
    for x in range(1, W - 1):
        for y in range(160, min(H - 100, 900)):
            r, g, b = px[x, y]
            mx, mn = max(r, g, b), min(r, g, b)
            if mx > 90:                 # 只看文字笔画（亮部）
                d = mx - mn
                if d > 40:              # 明显着色 = 振铃
                    cnt += 1
                    worst = max(worst, d)
    return W, H, cnt, worst


def main():
    pair = [int(a) for a in sys.argv[1:3]] or [3, 2]
    out = {}
    print(f"对比 SS = {pair[0]} vs {pair[1]}\n")
    for ss in pair:
        t, p = bench(ss)
        if not t:
            print(f"SS={ss}  跳过（无可用 CJK 字体）")
            continue
        out[ss] = (t, p)
        print(f"SS={ss}   单张最短 {t * 1000:.0f} ms")

    if len(out) == len(pair):
        print()
        for ss in pair:
            W, H, cnt, worst = ringing(out[ss][1])
            print(f"SS={ss}   尺寸 {W}x{H}   振铃像素 {cnt}   最大通道差 {worst}")
        try:
            base, fast = pair
            speed = out[base][0] / out[fast][0]
            print(f"\n提速 {speed:.2f}x（{out[base][0] * 1000:.0f} ms → "
                  f"{out[fast][0] * 1000:.0f} ms）")
        except ZeroDivisionError:
            pass


if __name__ == "__main__":
    main()
