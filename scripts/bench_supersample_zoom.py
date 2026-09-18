# -*- coding: utf-8 -*-
"""把同一张卡在 SS=3 / SS=2 / SS=1 下的**文字边缘**裁切放大成图，供人眼定案。

用法：python scripts/bench_supersample_zoom.py
产物：runtime/ss_zoom_*.png
"""
from __future__ import annotations

import sys
import tempfile
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

out_dir = ROOT / "runtime"
out_dir.mkdir(exist_ok=True)


def render_at(ss: int) -> Path | None:
    for m in [m for m in sys.modules if m.startswith("core")]:
        del sys.modules[m]
    from core import render as R

    R.SS = ss
    r = R.ImageRenderer(Path(tempfile.mkdtemp(prefix=f"zoom{ss}_")))
    if not r.available:
        return None
    return r.render(TITLE, BODY, "")


base = render_at(3)
if not base:
    print("[SKIP] 无可用 CJK 字体")
    sys.exit(0)

ref = Image.open(base).convert("RGB")
W, H = ref.size
# 取标题下方前两行文字带（高对比：亮字 + 深底 + 描边）
crop = (40, 165, min(W - 40, 640), 265)
Z = 3  # 再放大 3 倍看边缘

tiles = []
for ss in (3, 2, 1):
    p = base if ss == 3 else render_at(ss)
    if not p:
        continue
    im = Image.open(p).convert("RGB").crop(crop)
    tiles.append((ss, im.resize((im.width * Z, im.height * Z), Image.NEAREST)))

gap = 24
label_h = 46
tw = max(t.width for _, t in tiles)
th = sum(t.height for _, t in tiles) + gap * (len(tiles) - 1) + label_h * len(tiles)
canvas = Image.new("RGB", (tw, th), (24, 22, 30))
y = 0
for ss, t in tiles:
    canvas.paste(t, (0, y + label_h))
    y += label_h + t.height + gap

dst = out_dir / "ss_zoom_compare.png"
canvas.save(dst)
print(f"已保存 {dst}  ({canvas.width}x{canvas.height})")
for ss in (3, 2, 1):
    print(f"  {ss} 倍 = 第 {[s for s, _ in tiles].index(ss) + 1} 条（自上而下）"
          if ss in [s for s, _ in tiles] else f"  {ss} 倍 = 未渲染")
