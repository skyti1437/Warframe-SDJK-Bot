# -*- coding: utf-8 -*-
"""豆子（MOD 等级刻度）像素检测的回归测试。

自包含：用程序**合成**一张游戏截图风格的图（卡片 + 装饰细线 + 菱形豆），
不依赖任何真实截图，因此可以进包。

背景（2026-09-20 实测确立）：
  装备区每张卡底部有一条与卡片等宽的**亮蓝装饰细线**（厚 1-2px），
  细线上串着**亮蓝色菱形豆**（厚 6-8px、间距 11-12px），
  **亮豆数 = 该卡当前等级**；豆子以卡片中心水平居中。
  检测器必须先按细线定出「列网格」，再只在格子内数豆（0 级卡报 0）。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image, ImageDraw  # noqa: E402

from core.pips import detect_pips  # noqa: E402

FAILED = []


def check(name, cond, info=""):
    mark = "PASS" if cond else "FAIL"
    if not cond:
        FAILED.append(name)
    print(f"[{mark}] {name}" + (f"  ← {info}" if info and not cond else ""))


LIT = (143, 191, 229)          # 实测亮豆 RGB（蓝）
LIT_AMBER = (255, 222, 163)    # 执刑官那类边框的豆色（实测采样，琥珀橙）
BG = (18, 18, 22)
CARD = (40, 40, 48)
COLS = (712, 957, 1202, 1447)  # 装备区 4 列中心（1920 宽）
CARD_W = 152
CARD_H = 140
PITCH = 12


def make_shot(pips, top=300, W=1920, H=1080, cols=COLS, lines="all", lit=LIT,
              bg=BG, card=CARD):
    """合成一张截图：每列给定豆数（0 = 0 级，只画细线不画豆）。

    `lines`：画「与卡片等宽的装饰细线」的列 ——
      "all"（默认，模拟满级卡多的界面）/ "none"（全非满级）/ 列下标集合。
    ★ 实测（2026-09-20）：那条线**只在满级卡上出现**（非满级卡只有菱形豆），
      它是判定满级的第三信号，所以生成器要能分别模拟这两种界面。
    `lit`：豆/线的颜色 —— 默认蓝色；执刑官那类边框是**琥珀橙**（见 core.pips.warm_mask）。
    `bg` / `card`：背景与卡面底色 —— 用来模拟**浅色 UI 主题**（奥罗金浅白）。
    """
    im = Image.new("RGB", (W, H), bg)
    d = ImageDraw.Draw(im)
    for i, (cx, n) in enumerate(zip(cols, pips)):
        x0, y0 = cx - CARD_W // 2, top
        x1, y1 = cx + CARD_W // 2, top + CARD_H
        d.rectangle([x0, y0, x1, y1], fill=card, outline=(120, 100, 60))
        ly = y1 - 24
        want = (lines == "all") or (lines != "none" and i in lines)
        if want:
            d.rectangle([x0, ly, x1, ly + 1], fill=lit)      # 装饰细线（与卡等宽）
        if n > 0:
            total = (n - 1) * PITCH
            sx = cx - total // 2
            for i2 in range(n):
                px = sx + i2 * PITCH
                d.polygon([(px, ly - 5), (px + 4, ly), (px, ly + 5), (px - 4, ly)],
                          fill=lit)
    return im


def counts_of(im):
    res = detect_pips(im)
    eq = [r for r in res if not r["is_inventory"]]
    if not eq:
        return None
    return eq[0]["counts"]


def maxed_of(im):
    res = detect_pips(im)
    eq = [r for r in res if not r["is_inventory"]]
    return eq[0]["maxed"] if eq else None


try:
    # ① 基本：混合等级都要数对
    for case in ([5, 5, 0, 3], [0, 0, 0, 0], [1, 10, 5, 2], [5, 5, 5, 5]):
        got = counts_of(make_shot(case))
        check(f"合成图豆数 {case} → {got}", got == case, f"期望 {case}")

    # ② 0 级卡也必须占位（细线在、豆全暗 → 报 0）——这是旧版最大的缺陷
    got = counts_of(make_shot([0, 0, 0, 0]))
    check("0 级卡也能占位（全暗 → 报 0，不消失）", got == [0, 0, 0, 0], str(got))

    # ③ 低分辨率：缩到 1280 宽仍应正确（±1 容差）
    big = make_shot([5, 5, 0, 3])
    small = big.resize((1280, 720), Image.LANCZOS)
    got2 = counts_of(small)
    ok = got2 is not None and len(got2) == 4 and \
        all(abs(a - b) <= 1 for a, b in zip(got2, [5, 5, 0, 3]))
    check(f"1280 宽降采样后仍正确（±1 容差）→ {got2}", ok, str(got2))

    # ④ 部分截图：只截装备区（内容尺寸不变）→ 应完全正确
    crop = big.crop((560, 200, 1920, 560))
    got3 = counts_of(crop)
    check(f"只截装备区（部分截图）→ {got3}", got3 == [5, 5, 0, 3], str(got3))

    # ⑤ 仓库区识别：下半部分 + 一行 7 格 → 标记为 is_inventory
    inv = make_shot([5] * 7, top=800, cols=(212, 462, 712, 962, 1212, 1462, 1712))
    rows = detect_pips(inv)
    check("仓库区（下半 + 7 格）被标为 is_inventory",
          bool(rows) and all(r["is_inventory"] for r in rows), str(rows))

    # ⑥ 满级线（第三信号，用户 2026-09-20 实测指出）：
    #    满级卡底部有一条**横贯全卡**的亮线，非满级卡没有。
    got = maxed_of(make_shot([5, 5, 5, 5], lines="all"))
    check(f"满级线：全都有线 → maxed 全 True  {got}", got == [True] * 4, str(got))

    got = maxed_of(make_shot([1, 5, 0, 3], lines=(0,)))
    check(f"满级线：只有第 1 格有线 → 只标第 1 格  {got}",
          got == [True, False, False, False], str(got))

    got = maxed_of(make_shot([1, 5, 0, 3], lines="none"))
    check(f"满级线：全都没线 → maxed 全 False  {got}", got == [False] * 4, str(got))

    # ⑦ 豆色随**边框类型**变化（用户给的 mod边框类型.zip 实测）：
    #    常见/罕见/稀有/传说/合并/怪奇/镀层/裂罅 偏蓝，**执刑官是琥珀橙**
    #    （采样 (255,222,163)，蓝通道太低 → 主掩码整格漏）→ 暖色掩码兜底。
    got = counts_of(make_shot([5, 3, 0, 5], lit=LIT_AMBER))
    check(f"执刑官风格（琥珀豆）也能数对 → {got}", got == [5, 3, 0, 5], str(got))

    # ⑧ 尺寸阶梯：4K 大图会被归一到基准尺度（用户 4K 报障）
    big4k = make_shot([5, 7, 3, 1]).resize((3840, 2160), Image.LANCZOS)
    got = counts_of(big4k)
    check(f"4K（3840 宽）归一后仍正确 → {got}", got == [5, 7, 3, 1], str(got))

    # ⑨ 容错：全黑 / 极小图不应抛异常
    try:
        detect_pips(Image.new("RGB", (1920, 1080), (0, 0, 0)))
        detect_pips(Image.new("RGB", (300, 200), BG))
        check("全黑图 / 极小图不抛异常", True)
    except Exception as e:  # noqa: BLE001
        check("全黑图 / 极小图不抛异常", False, f"{type(e).__name__}: {e}")

    # ⑩ ★ 暖色兜底不得凭空造豆（2026-09-20 4K 事故的回归）
    #    事故：金框卡「结霜侵蚀」的**卡面美术是暖色**，暖掩码把整片卡面点亮，
    #    在那一格数出 2 颗（真值 0）→ 该卡约束不满足 → 整图对齐无解 →
    #    7 张卡的豆子信号全部作废（用户看到「等级又不对」）。
    #    规则：**整排蓝豆都为 0** 时才允许暖色兜底。
    im_art = make_shot([5, 0, 5, 1], lit=LIT)          # 一排蓝豆卡
    _d = ImageDraw.Draw(im_art)
    _cx = COLS[1]
    _x0, _x1 = _cx - CARD_W // 2, _cx + CARD_W // 2
    _ly = 300 + CARD_H - 24
    _d.rectangle([_x0 + 2, _ly - 3, _x1 - 2, _ly + 8], fill=(210, 175, 120))   # 卡面暖色美术
    _d.rectangle([_x0 + 1, _ly - 4, _x0 + 14, _ly + 9], fill=LIT_AMBER)        # 左缘高光
    got = counts_of(im_art)
    check(f"★ 金框卡的暖色卡面不被当豆（该格应为 0）→ {got}",
          got == [5, 0, 5, 1], str(got))

    # ⑪ 反过来：整排都没有蓝豆（纯执刑官排）时，**整图**暖色重扫仍要工作
    #    （★ 逐格暖色兜底已在 2026-09-20 删掉 —— 它会把金框卡的卡面美术当成豆）
    got = counts_of(make_shot([5, 3, 0, 5], lit=LIT_AMBER))
    check(f"纯琥珀排仍走（整图）暖色兜底 → {got}", got == [5, 3, 0, 5], str(got))

    # ⑫ 主题/背景鲁棒性（用户方案文档 TC-01「奥罗金浅白主题」）：
    #    实测结论 —— **不需要**「暗岛掩膜」。因为主掩码要求「蓝通道显著高于红通道」，
    #    白底(245,243,238)、灰卡这类低饱和背景**天然不满足**，不会被误当豆；
    #    而卡片内部（无论主题）仍是深色、豆子仍是蓝的。
    #    这里用 4 种背景/卡面组合把这条性质钉住。
    for _bg, _card, _why in (((18, 18, 22), (40, 40, 48), "深色主题（基准）"),
                             ((245, 243, 238), (40, 40, 48), "浅白主题 + 深色卡"),
                             ((245, 243, 238), (245, 243, 238), "浅白主题 + 浅色卡（最坏）"),
                             ((245, 243, 238), (220, 218, 214), "浅白主题 + 灰白卡")):
        _im = make_shot([5, 0, 5, 1], bg=_bg, card=_card)
        _got = counts_of(_im)
        check(f"主题鲁棒：[{_why}] → {_got}", _got == [5, 0, 5, 1], str(_got))
finally:
    pass

print()
if FAILED:
    print(f"[FAIL] {len(FAILED)} 项失败: {FAILED}")
    sys.exit(1)
print("[OK] 豆子检测回归测试全部通过")
