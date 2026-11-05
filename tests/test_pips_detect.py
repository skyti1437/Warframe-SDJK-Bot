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

from core.pips import detect_layout, detect_pips  # noqa: E402

FAILED = []


def check(name, cond, info=""):
    mark = "PASS" if cond else "FAIL"
    if not cond:
        FAILED.append(name)
    print(f"[{mark}] {name}" + (f"  ← {info}" if info and not cond else ""))


LIT = (143, 191, 229)  # 实测亮豆 RGB（蓝）
LIT_AMBER = (255, 222, 163)  # 执刑官那类边框的豆色（实测采样，琥珀橙）
BG = (18, 18, 22)
CARD = (40, 40, 48)
COLS = (712, 957, 1202, 1447)  # 装备区 4 列中心（1920 宽）
CARD_W = 152
CARD_H = 140
PITCH = 12


def make_shot(pips, top=300, W=1920, H=1080, cols=COLS, lines="all", lit=LIT, bg=BG, card=CARD):
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
            d.rectangle([x0, ly, x1, ly + 1], fill=lit)  # 装饰细线（与卡等宽）
        if n > 0:
            total = (n - 1) * PITCH
            sx = cx - total // 2
            for i2 in range(n):
                px = sx + i2 * PITCH
                d.polygon([(px, ly - 5), (px + 4, ly), (px, ly + 5), (px - 4, ly)], fill=lit)
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
    ok = (
        got2 is not None
        and len(got2) == 4
        and all(abs(a - b) <= 1 for a, b in zip(got2, [5, 5, 0, 3]))
    )
    check(f"1280 宽降采样后仍正确（±1 容差）→ {got2}", ok, str(got2))

    # ④ 部分截图：只截装备区（内容尺寸不变）→ 应完全正确
    crop = big.crop((560, 200, 1920, 560))
    got3 = counts_of(crop)
    check(f"只截装备区（部分截图）→ {got3}", got3 == [5, 5, 0, 3], str(got3))

    # ⑤ 仓库区识别：下半部分 + 一行 7 格 → 标记为 is_inventory
    inv = make_shot([5] * 7, top=800, cols=(212, 462, 712, 962, 1212, 1462, 1712))
    rows = detect_pips(inv)
    check(
        "仓库区（下半 + 7 格）被标为 is_inventory",
        bool(rows) and all(r["is_inventory"] for r in rows),
        str(rows),
    )

    # ⑥ 满级线（第三信号，维护者 2026-09-20 实测指出）：
    #    满级卡底部有一条**横贯全卡**的亮线，非满级卡没有。
    got = maxed_of(make_shot([5, 5, 5, 5], lines="all"))
    check(f"满级线：全都有线 → maxed 全 True  {got}", got == [True] * 4, str(got))

    got = maxed_of(make_shot([1, 5, 0, 3], lines=(0,)))
    check(
        f"满级线：只有第 1 格有线 → 只标第 1 格  {got}",
        got == [True, False, False, False],
        str(got),
    )

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
    im_art = make_shot([5, 0, 5, 1], lit=LIT)  # 一排蓝豆卡
    _d = ImageDraw.Draw(im_art)
    _cx = COLS[1]
    _x0, _x1 = _cx - CARD_W // 2, _cx + CARD_W // 2
    _ly = 300 + CARD_H - 24
    _d.rectangle([_x0 + 2, _ly - 3, _x1 - 2, _ly + 8], fill=(210, 175, 120))  # 卡面暖色美术
    _d.rectangle([_x0 + 1, _ly - 4, _x0 + 14, _ly + 9], fill=LIT_AMBER)  # 左缘高光
    got = counts_of(im_art)
    check(f"★ 金框卡的暖色卡面不被当豆（该格应为 0）→ {got}", got == [5, 0, 5, 1], str(got))

    # ⑪ 反过来：整排都没有蓝豆（纯执刑官排）时，**整图**暖色重扫仍要工作
    #    （★ 逐格暖色兜底已在 2026-09-20 删掉 —— 它会把金框卡的卡面美术当成豆）
    got = counts_of(make_shot([5, 3, 0, 5], lit=LIT_AMBER))
    check(f"纯琥珀排仍走（整图）暖色兜底 → {got}", got == [5, 3, 0, 5], str(got))

    # ⑫ 主题/背景鲁棒性（方案文档 TC-01「奥罗金浅白主题」）：
    #    实测结论 —— **不需要**「暗岛掩膜」。因为主掩码要求「蓝通道显著高于红通道」，
    #    白底(245,243,238)、灰卡这类低饱和背景**天然不满足**，不会被误当豆；
    #    而卡片内部（无论主题）仍是深色、豆子仍是蓝的。
    #    这里用 4 种背景/卡面组合把这条性质钉住。
    for _bg, _card, _why in (
        ((18, 18, 22), (40, 40, 48), "深色主题（基准）"),
        ((245, 243, 238), (40, 40, 48), "浅白主题 + 深色卡"),
        ((245, 243, 238), (245, 243, 238), "浅白主题 + 浅色卡（最坏）"),
        ((245, 243, 238), (220, 218, 214), "浅白主题 + 灰白卡"),
    ):
        _im = make_shot([5, 0, 5, 1], bg=_bg, card=_card)
        _got = counts_of(_im)
        check(f"主题鲁棒：[{_why}] → {_got}", _got == [5, 0, 5, 1], str(_got))
finally:
    pass

# 几何观测只验证确定性信号，不宣称合成图能证明实图完整性。
_layout_image = make_shot([5, 0, 5, 1])
_layout = detect_layout(_layout_image)
check(
    "布局观测保留精确等级线和相对坐标",
    bool(_layout["rank_lines"])
    and all(0 <= v <= 1 for item in _layout["slots"] for v in item["box"]),
)
check("普通卡候选来自像素而非外部pips行列", _layout == detect_layout(_layout_image, None))
check(
    "图像几何不越权证明完整布局",
    not _layout["layout_complete"] and _layout["coverage"]["status"] == "unknown",
)
_empty = make_shot([0, 0, 0, 0], lines="none")
_empty_layout = detect_layout(_empty)
check(
    "无亮豆的封闭框仅是空框候选",
    any(item["content_state"] == "empty_frame_candidate" for item in _empty_layout["slots"])
    and not _empty_layout["layout_complete"],
)
_scaled = detect_layout(_layout_image.resize((1280, 720), Image.Resampling.LANCZOS))
check(
    "缩放后仍保留等级线几何",
    bool(_scaled["rank_lines"])
    and abs(_scaled["rank_lines"][0]["box"][0] - _layout["rank_lines"][0]["box"][0]) < 0.02,
)
_cropped = detect_layout(_layout_image.crop((560, 200, 1920, 560)))
check(
    "裁图不因能定位卡片就声称布局完整",
    not _cropped["layout_complete"] and _cropped["coverage"]["equipment"] == "unknown",
)
_inventory_layout = detect_layout(
    make_shot([5] * 7, top=800, cols=(212, 462, 712, 962, 1212, 1462, 1712))
)
check(
    "多列库存等级线不归入装备候选",
    bool(_inventory_layout["rank_lines"])
    and all(item["zone"] == "inventory_candidate" for item in _inventory_layout["rank_lines"]),
)
_meta = detect_pips(big4k)
check(
    "豆子坐标明确原图和内部工作图尺寸",
    bool(_meta)
    and _meta[0]["source_image_size"] == [3840, 2160]
    and _meta[0]["image_size"][0] < 3840,
)
check("黑图不凭空产生槽位", not detect_layout(Image.new("RGB", (1920, 1080)))["slots"])
check("过小图完整性保持未知", not detect_layout(Image.new("RGB", (30, 30)))["layout_complete"])

_striped = Image.new("RGB", (1280, 720))
_stripes = ImageDraw.Draw(_striped)
for _y in range(5, 715, 4):
    for _x in range(5, 1240, 90):
        _stripes.line((_x, _y, _x + 65, _y), fill="white", width=1)
_budget = detect_layout(_striped)
check(
    "高密度条纹达到预算时保守退出",
    not _budget["slots"] and "horizontal_edge_budget_exceeded" in _budget["coverage"]["reasons"],
)
check(
    "超长图几何扫描受缩放上限约束",
    not detect_layout(Image.new("RGB", (100, 10000)))["layout_complete"],
)


# 局部上方窗口：全图峰值会压掉稀疏线的第二行，局部峰值仍可保留。
def upper_roi_shot(*, outside=False, thin=False, inventory=False):
    image = Image.new("RGB", (1280, 720), BG)
    draw = ImageDraw.Draw(image)
    for center in (475, 638, 801, 964):
        draw.rectangle((center - 50, 300, center + 50, 302), fill=LIT)
    center = 100 if outside else 638
    draw.line((center - 50, 200, center + 50, 200), fill=LIT)
    if not thin:
        draw.line((center - 15, 201, center + 14, 201), fill=LIT)
    if inventory:
        for center in (100, 280, 460, 640, 820, 1000, 1180):
            draw.rectangle((center - 50, 600, center + 50, 602), fill=LIT)
    return image


def upper_roi_slots(image):
    return [
        slot
        for slot in detect_layout(image)["slots"]
        if "upper_roi_local_blue_band" in slot["evidence"]
    ]


_upper = upper_roi_shot()
_upper_slots = upper_roi_slots(_upper)
check("普通网格上方局部峰值保留稀疏两行线", len(_upper_slots) == 1)
check(
    "局部上方线仍只是估计特殊候选",
    bool(_upper_slots)
    and _upper_slots[0]["kind"] == "special_candidate"
    and _upper_slots[0]["extent"] == "estimated_from_rank_line"
    and not detect_layout(_upper)["layout_complete"],
)
check("单像素蓝UI线不补成上方槽候选", not upper_roi_slots(upper_roi_shot(thin=True)))
check("普通网格横向范围外蓝线不补成上方槽候选", not upper_roi_slots(upper_roi_shot(outside=True)))
check(
    "库存高峰不压掉上方局部线且库存不当特殊槽",
    len(upper_roi_slots(upper_roi_shot(inventory=True))) == 1,
)
check("截断上方卡片范围不补成未截断候选", not upper_roi_slots(_upper.crop((0, 150, 1280, 720))))
_upper_scaled = upper_roi_slots(_upper.resize((1920, 1080), Image.Resampling.LANCZOS))
check(
    "缩放上方局部候选仍定位同一相对等级线",
    len(_upper_scaled) == 1
    and len(_upper_slots) == 1
    and max(abs(a - b) for a, b in zip(_upper_scaled[0]["line_box"], _upper_slots[0]["line_box"]))
    < 0.01,
)


# 同槽封闭边与等级线的相互唯一归并（不扩大布局覆盖许可）。
import copy  # noqa: E402
from core import pips as new  # noqa: E402
from core.loadout_ocr import analyze_layout  # noqa: E402


def shot():
    im = Image.new("RGB", (1920, 1080), (18, 18, 22))
    d = ImageDraw.Draw(im)
    for cx in (712, 957, 1202, 1447):
        d.rectangle(
            (cx - 76, 300, cx + 76, 440), fill=(40, 40, 48), outline=(220, 220, 220), width=2
        )
        d.rectangle((cx - 76, 416, cx + 76, 417), fill=(143, 191, 229))
        for px in range(cx - 24, cx + 25, 12):
            d.polygon([(px, 411), (px + 4, 416), (px, 421), (px - 4, 416)], fill=(143, 191, 229))
    return im


im = shot()
for w in (1920, 1280, 2560):
    pic = im.resize((w, round(w * 1080 / 1920)), Image.Resampling.LANCZOS)
    p = new.detect_layout(pic)
    mods = [
        {
            "name": "占位观察",
            "_geometry": {"x": cx / 1920 * 1000, "y": 380 / 1080 * 1000},
            "_geometry_source": "coordinate_ocr",
            "_geometry_view": "full_image",
        }
        for cx in (712, 957, 1202, 1447)
    ]
    an = analyze_layout({"mods": mods, "_pixel_layout": p})
    check(
        f"生产detect→analyze缩放{w}",
        len(p["slots"]) == 4
        and an["slot_binding_complete"]
        and not an["unread_slots"]
        and not an["layout_complete"],
    )
    check(
        f"保留两份源证据和等级锚点{w}",
        all(
            len(s.get("source_slots", [])) == 2
            and s.get("line_box")
            and s.get("extent") == "estimated_from_rank_line"
            for s in p["slots"]
        ),
    )
    if w == 1920:
        duplicate = analyze_layout({"mods": mods + mods[:1], "_pixel_layout": p})
        check(
            "同槽重复观察仍冲突",
            duplicate["bindings"][0]["reason"] == "multiple_mods_in_one_slot"
            and not duplicate["slot_binding_complete"],
        )
# 独立框线证据的正反性质，不借助名称、图ID或GT
frame = {
    "id": "f",
    "box": [0.3, 0.3, 0.5, 0.5],
    "kind": "regular_candidate",
    "zone": "equipment_candidate",
    "content_state": "rank_marks_observed",
    "evidence": ["closed_rectangle_edges"],
}
line = {
    "id": "l",
    "box": [0.25, 0.25, 0.55, 0.49],
    "line_box": [0.31, 0.46, 0.49, 0.462],
    "anchor_box": [0.31, 0.46, 0.49, 0.462],
    "kind": "regular_candidate",
    "zone": "equipment_candidate",
    "content_state": "rank_marks_observed",
    "extent": "estimated_from_rank_line",
    "evidence": ["horizontal_blue_rank_line_candidate"],
}
merge = new._merge_slot_evidence
check("相互唯一合并", len(merge([frame, line])) == 1)
check("纯框保留", merge([frame]) == [frame])
check("纯等级线保留", merge([line]) == [line])
l2 = copy.deepcopy(line)
l2["id"] = "l2"
l2["line_box"] = [0.31, 0.47, 0.49, 0.472]
check("一框不同两等级线拒绝归并", len(merge([frame, line, l2])) == 3)
f2 = copy.deepcopy(frame)
f2["id"] = "f2"
f2["box"] = [0.295, 0.3, 0.505, 0.5]
check("一线两框拒绝归并", len(merge([frame, f2, line])) == 3)
wrong = copy.deepcopy(line)
wrong["zone"] = "inventory_candidate"
check("库存装备不混并", len(merge([frame, wrong])) == 2)
wrong = copy.deepcopy(line)
wrong["kind"] = "special_candidate"
check("普通特殊不混并", len(merge([frame, wrong])) == 2)
wrong = copy.deepcopy(line)
wrong["line_box"] = [0.31, 0.31, 0.49, 0.312]
check("框上部蓝线不并", len(merge([frame, wrong])) == 2)
wrong = copy.deepcopy(line)
wrong["line_box"] = [0.28, 0.46, 0.49, 0.462]
check("跨框线不并", len(merge([frame, wrong])) == 2)
f2 = copy.deepcopy(frame)
f2["id"] = "f2"
f2["box"] = [0.55, 0.3, 0.75, 0.5]
l2 = copy.deepcopy(line)
l2["id"] = "l2"
l2["line_box"] = [0.56, 0.46, 0.74, 0.462]
l2["anchor_box"] = l2["line_box"]
check("相邻两卡各自归并", len(merge([frame, line, f2, l2])) == 2)
f2 = copy.deepcopy(frame)
f2["box"] = [0.001, 0.3, 0.201, 0.5]
l2 = copy.deepcopy(line)
l2["line_box"] = [0.011, 0.46, 0.191, 0.462]
check("截断边框保留未知", len(merge([f2, l2])) == 2)

print()
# 连成宽簇的弱豆及中心高光：不能把跨豆间距/徽饰最高点当作豆间距和豆厚。
from core.pips import _count_in_cell  # noqa: E402

_thick = [0] * 240
for _x in range(44, 197):
    _thick[_x] = 1
for _x in range(60, 182):
    _thick[_x] = 5
for _center in range(66, 175, 12):
    for _offset in range(-3, 4):
        _thick[_center + _offset] = 9 - abs(_offset) // 2
_thick[121] = 13
check("宽簇豆子不受中央单点高光抬高阈值", _count_in_cell(_thick, 120, 152, 240)[0] == 10)
check("只有装饰细线不计作亮豆", _count_in_cell([2] * 240, 120, 152, 240)[0] == 0)

_regular = make_shot([5, 7, 3, 1], lines={0, 2})
_auxiliary = make_shot([10], top=470, cols=(1692,))
_regular.paste(_auxiliary.crop((1600, 460, 1790, 630)), (1600, 460))
_aux_rows = [r for r in detect_pips(_regular) if not r["is_inventory"] and len(r["grid"]) > 4]
check("右侧独立辅助槽保留自己的像素列", len(_aux_rows) == 1 and _aux_rows[0]["counts"][-1] == 10)


# 赋能读菱形中心；同一轮廓数量分别覆盖零级、部分级和满级。
from core.pips import count_arcane_rank  # noqa: E402

for _rank in (0, 2, 5):
    _image = Image.new("RGB", (300, 180), (22, 22, 28))
    _draw = ImageDraw.Draw(_image)
    for _idx in range(5):
        _x, _y = 90 + _idx * 30, 89
        _draw.polygon(
            [(_x, _y - 11), (_x + 10, _y), (_x, _y + 11), (_x - 10, _y)],
            outline=(205, 195, 135),
            width=2,
        )
        if _idx < _rank:
            _draw.polygon(
                [(_x, _y - 5), (_x + 5, _y), (_x, _y + 5), (_x - 5, _y)], fill=(205, 195, 135)
            )
    _evidence = count_arcane_rank(_image, {"x": 500, "y": 320, "h": 120, "w": 500})
    # 名称框必须合理；过宽框拒绝，不能借全图找五个亮点。
    check("赋能拒绝无约束名称框", _evidence["rank"] is None)
    _evidence = count_arcane_rank(_image, {"x": 500, "y": 320, "h": 120, "w": 290})
    # 另用更大的画布，使固定名称宽度包含五个菱形而不占整屏。
    _wide = Image.new("RGB", (700, 180), (22, 22, 28))
    _wide.paste(_image, (200, 0))
    _evidence = count_arcane_rank(_wide, {"x": 500, "y": 320, "h": 120, "w": 290})
    check(f"五个菱形轮廓按实际中心读{_rank}级", _evidence["rank"] == _rank, str(_evidence))

if FAILED:
    print(f"[FAIL] {len(FAILED)} 项失败: {FAILED}")
    sys.exit(1)
print("[OK] 豆子检测回归测试全部通过")
