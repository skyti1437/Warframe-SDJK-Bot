# -*- coding: utf-8 -*-
"""渲染溢出回归测试：python3 tests/test_render_overflow.py

锁住两类「内容压出面板」的回归：

1. **多列表格**（仲裁时间表 6 列）：列对齐时每列按「该列最大宽度」绘制、
   列间固定 24px 间隙，整表宽度 = Σ列宽 + 24×(列数-1)。
   历史 bug：卡宽只按「首列 + 剩余整串」两列估算，第 3..N 列的对齐扩张
   没进卡宽，末尾的评级列直接压到面板边框外。

2. **长标题**：标题字号 46、不换行、无裁剪。历史 bug：遗物列表标题里的
   页码是「共768**个**」，而页码正则只认「共N**条**」→ 页码没被抽走 →
   标题过长压到右上角平台徽章上、再被面板边框裁掉。

判定方式与人工核对一致：渲染成图后按像素扫面板右缘，越界即失败。
缺 Pillow / 字体时（如精简 CI 容器）跳过，不误报。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import render as R  # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = ""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f"  -> {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


# ------------------------------------------------------------------ 页码正则
# 量词各 handler 不统一：条 / 场 / 个 / 件 / 项
PAGE_CASES = [
    ("仲裁时间表（第1/23页，共336条）", ("1", "23", "336"), "仲裁时间表"),
    ("开核桃建议（第1/4页，共32场）", ("1", "4", "32"), "开核桃建议"),
    ("遗物列表：当前可掉落 34，已入库 734（第1/77页，共768个）",
     ("1", "77", "768"), "遗物列表：当前可掉落 34，已入库 734"),
    ("遗物入库（已入库、不可刷取）（第1/74页，共734个）",
     ("1", "74", "734"), "遗物入库（已入库、不可刷取）"),
    ("膛线 在售单（第1/39页，共386条）", ("1", "39", "386"), "膛线 在售单"),
]
for title, groups, rest in PAGE_CASES:
    m = R._PAGE_RE.search(title)
    check(f"页码可识别：{title[:20]}…",
          bool(m) and m.groups() == groups,
          f"groups={m.groups() if m else None}")
    stripped = R._PAGE_RE.sub("", title).strip()
    check(f"页码已抽离：{stripped[:20]}…", stripped == rest, stripped)

# ------------------------------------------------------------------ 渲染成图
if R.Image is None:
    print("[SKIP] 未安装 Pillow，跳过渲染溢出检查")
else:
    probe = R.ImageRenderer(Path(tempfile.mkdtemp(prefix="sdjk_render_")))
    if not probe.available:
        print("[SKIP] 无可用 CJK 字体，跳过渲染溢出检查")
    else:
        LUM = 125     # 正文暖白/亮金/青均 > 125；面板底/暗金框约 17~102

        def right_overflow(img_path: str, safe_from_edge: int = 30):
            """返回（最右越界 x，越界像素数）；正文带不含四角装饰。"""
            im = R.Image.open(img_path).convert("RGB")
            W, H = im.size
            rgb = im.load()
            total, maxx = 0, 0
            for x in range(W - safe_from_edge, W):
                for y in range(150, H - 96):
                    r, g, b = rgb[x, y]
                    if 0.2126 * r + 0.7152 * g + 0.0722 * b > LUM:
                        total += 1
                        maxx = max(maxx, x)
            return maxx, total

        # 1) 仲裁时间表式 6 列（含长节点名 Apollodorus 与可选评级列）
        arb = [
            "12日12时　光理塔　虚空　拦截　奥罗金　A",
            "12日13时　Apollodorus　水星　生存　Infested",
            "12日14时　奥金工场　扎里曼号　虚空决战　Grineer",
            "12日15时　Stöfler　月球　防御　Grineer",
            "12日23时　Alator　火星　拦截　Grineer　S",
            "13日00时　Cinxia　谷神星　拦截　Grineer　A+",
        ]
        p = probe.render("仲裁时间表（第1/23页，共336条）", arb, "")
        if p:
            mx, n = right_overflow(p)
            W = R.Image.open(p).size[0]
            check("6 列表格：正文未越出面板右缘", n == 0,
                  f"W={W} 越界 {n} 像素，最右 x={mx}")
        else:
            check("6 列表格：渲染成功", False, "render() 返回 None 已降级文本")

        # 2) 长标题（页码须已被抽走，标题自适应不得压到徽章）
        long_title = ("遗物列表：当前可掉落 34，已入库 734"
                      "（第1/77页，共768个）")
        p = probe.render(long_title, ["[可掉落]中纪 Neo T11", "掉落位置：金星 · Romula · 防御（C轮）"], "")
        if p:
            im = R.Image.open(p).convert("RGB")
            W, H = im.size
            rgb = im.load()
            # 徽章右缘 = W-64；标题带里 x ∈ [W-56, W-4] 必须为空
            hot = sum(
                1 for x in range(W - 56, W - 4)
                for y in range(68, 118)
                if 0.2126 * rgb[x, y][0] + 0.7152 * rgb[x, y][1]
                + 0.0722 * rgb[x, y][2] > LUM)
            check("长标题：未压到右上角徽章", hot == 0, f"W={W} 越界像素={hot}")
        else:
            check("长标题：渲染成功", False, "render() 返回 None 已降级文本")

print()
if FAILED:
    print(f"FAILED {len(FAILED)}: " + ", ".join(FAILED))
    sys.exit(1)
print("ALL PASS")
