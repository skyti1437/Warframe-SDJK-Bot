# -*- coding: utf-8 -*-
"""配卡截图识别：视觉模型读图 → 本地库强校验 → 按**实际等级**折算加成。

职责边界
--------
视觉模型**只负责读字**（MOD 名、卡片右上角的容量数字、面板数值），
它给出的一切数值结论都不采信 —— 加成一律由本地 MOD 表按实际等级重算。
原因是同一张卡不同等级能差好几倍（北风 rank1 冰 +30% vs rank5 冰 +90%），
让模型看图猜等级极不可靠；而**卡片容量数字是可读且可反推的**：

    drain = base_drain + rank                    （槽位极性不匹配）
    drain = ceil((base_drain + rank) / 2)        （槽位极性匹配，消耗减半）

反推结果再与面板数值（暴击率 / 暴击伤害 / 触发率 / 总伤害）比较。
基础取自本地库时是数值对照；基础由同一截图反推时只证明自洽，不证明原图读准。

实测（2026-09-16，用户提供的 Xoris「驱魔之刃」配卡截图）：
9 张卡全部识别成功；drain 反推等级 8/8 命中；面板四项校验全部吻合。
"""

from __future__ import annotations

import copy
import difflib
import itertools
import json
import math
import re
from pathlib import Path
from typing import Optional

from . import damage_calc as dc
from . import pips as pips_engine

_DATA = Path(__file__).resolve().parent / "data"
_payload: dict = {}

# 物理三类型的中文名（面板行里用的就是这三个词）
PHYS_ZH = {"impact": "冲击", "puncture": "穿刺", "slash": "切割"}
# 单元素 -> 中文
ELEM_ZH = {"heat": "火", "cold": "冰", "electricity": "电", "toxin": "毒"}
# 全部元素（含复合）的中文：卡面里统一用这一套，避免「火」和「火焰」混着显示
ELEM_ALL_ZH = dict(
    ELEM_ZH,
    **{
        "blast": "爆炸",
        "corrosive": "腐蚀",
        "viral": "病毒",
        "magnetic": "磁力",
        "radiation": "辐射",
        "gas": "毒气",
    },
)
# 武器类别中文化
CATEGORY_ZH = {"Primary": "主武器", "Secondary": "副武器", "Melee": "近战"}
# 游戏里复合元素的合成顺序（火 > 冰 > 电 > 毒，两两配对）
ELEM_PAIRS = {
    frozenset({"heat", "cold"}): "blast",
    frozenset({"heat", "electricity"}): "radiation",
    frozenset({"heat", "toxin"}): "gas",
    frozenset({"cold", "electricity"}): "magnetic",
    frozenset({"cold", "toxin"}): "viral",
    frozenset({"electricity", "toxin"}): "corrosive",
}


# ★★ 2026-10-05 去掉字面示例值：阴性对照实测（喂**紫卡图**——卡面没有容量栏/
#   伤害栏）本提示词 8B 3/6、30B 4/5 次吐出 `"capacity": "0/70"`，即照抄旧例子。
#   旧例子里的 0/70 / 144 / 1,807.3 / 75.6 / 22▶138 / 驱魔之刃 [30] 全部来自
#   用户真实截图 ⇒ **本提示词不得再出现任何具体数值**，一律用占位符/描述表达。
VISION_PROMPT = "Read all the text in the image. Return each text line as cx,cy,width,height,angle,text. The first five values describe a rotated rectangle: center x, center y, side width, side height, and rotation angle in degrees. Normalize the center and side lengths to 0-1000; angle is in [-90,90]. Preserve the recognized text after the coordinates. Include visible words and Arabic numerals, even faint text. Do not transcribe icons, borders, rank pips or decorative marks as characters. Keep text from each separate region on separate lines. Do not calculate or infer missing text."


# ---------------------------------------------------------------------------
# 文本清洗
# ---------------------------------------------------------------------------
def _norm(s) -> str:
    """匹配用归一化：去空白 / 连字符 / 间隔号，转小写。"""
    return re.sub(r"[\s·・\-_'’]+", "", str(s or "")).lower()


def to_int(x) -> Optional[int]:
    """从 '^10' / '7→' / '9↘' / 10 里取出整数（模型的输出常带装饰符号）。"""
    if isinstance(x, bool):
        return None
    if isinstance(x, (int, float)):
        return int(x)
    m = re.search(r"\d+", str(x or ""))
    return int(m.group()) if m else None


def to_float(x) -> Optional[float]:
    """从 '44%' / '4.6倍' / '40.8' 里取出数值（先剥千分位逗号：1,807.3）。"""
    if isinstance(x, bool):
        return None
    if isinstance(x, (int, float)):
        return float(x)
    m = re.search(r"-?\d+(?:,\d{3})*(?:\.\d+)?", str(x or ""))
    return float(m.group().replace(",", "")) if m else None


def to_pair(x) -> tuple[Optional[float], Optional[float]]:
    """从 '36>144' / ['总计','156>264'] / ['36','144'] / '24' 里取 (左值, 右值)。

    注意列表形态：模型常把「总计」整行给成 `["总计", "156>264"]`，
    这时必须**继续往下拆字符串里的数字**，不能把 156 当成右值
    （实测就是这样误判了一处总伤害校验）。千分位逗号先剥掉，
    否则 "1,807.3" 会被拆成 1 和 807.3 两个数。
    """
    if isinstance(x, (list, tuple)):
        text = " ".join(str(v) for v in x)
    else:
        text = str(x or "")
    text = text.replace(",", "")
    nums = re.findall(r"\d+(?:\.\d+)?", text)
    if len(nums) >= 2:
        return float(nums[0]), float(nums[1])
    if len(nums) == 1:
        return float(nums[0]), None
    return None, None


_TOTAL_LABELS = {"总计", "总伤", "总伤害", "总"}
_PANEL_LABEL_FIELDS = {
    "攻击速度": "attack_speed",
    "射击速度": "attack_speed",
    "暴击几率": "crit_chance",
    "暴击伤害": "crit_damage",
    "暴击倍率": "crit_damage",
    "触发几率": "status_chance",
    **dict.fromkeys(_TOTAL_LABELS, "total"),
}


def _panel_field(label) -> str:
    label = re.sub(r"\s+", "", str(label or "").lstrip("*•- "))
    label = re.split(r"[（(]", label, 1)[0]
    if label in _PANEL_LABEL_FIELDS.values():
        return label
    return _PANEL_LABEL_FIELDS.get(label, label)


def _panel_columns(value) -> dict[str, float]:
    """单读数为终值；显式左右占位只比较有值的一侧。"""
    if isinstance(value, dict):
        if "left" in value or "right" in value:
            return {
                side: number
                for side in ("left", "right")
                if (number := to_float(value.get(side))) is not None
            }
        value = list(value.values())
    if isinstance(value, (list, tuple)) and len(value) > 1:
        if _panel_field(value[0]) in set(_PANEL_LABEL_FIELDS.values()) | set(_TOTAL_LABELS):
            value = value[1:]
    if isinstance(value, (list, tuple)) and len(value) == 2:
        if any(v is None or str(v).lower() == "null" for v in value):
            return {
                side: number
                for side, raw in zip(("left", "right"), value)
                if (number := to_float(raw)) is not None
            }
    left, right = to_pair(value)
    if right is not None:
        return {"left": left, "right": right}
    return {"right": left} if left is not None else {}


def _panel_values(panel: dict):
    """在解析/归一前保留的证据与最终字段都参与同指标核对。"""
    for key, value in panel.items():
        if key == "damage_rows":
            rows = value if isinstance(value, (list, tuple)) else []
        elif key == "_ocr_rows":
            rows = value if isinstance(value, (list, tuple)) else []
        elif key == "_ocr_duplicate_fields":
            for repeated, previous in value:
                yield from _panel_values({repeated: previous})
            continue
        elif key == "_ocr_prior_panels":
            for previous in value:
                if isinstance(previous, dict):
                    yield from _panel_values(previous)
            continue
        elif str(key).startswith("_"):
            continue
        else:
            yield _panel_field(key), value
            continue
        for row in rows:
            if isinstance(row, (list, tuple)) and len(row) >= 2:
                yield _panel_field(row[0]), row[1:]


def _panel_conflict_reasons(panel: dict) -> list[dict[str, str]]:
    seen = {}
    conflicts = set()
    for field, value in _panel_values(panel):
        for side, number in _panel_columns(value).items():
            previous = seen.setdefault((field, side), number)
            if not math.isclose(previous, number, rel_tol=1e-9, abs_tol=1e-9):
                conflicts.add(field)
    labels = {value: key for key, value in _PANEL_LABEL_FIELDS.items()}
    labels["total"] = "总伤害"
    return [
        {"code": "panel_conflict", "message": f"面板{labels.get(field, field)}同指标读数存在冲突"}
        for field in sorted(conflicts)
    ]


def _unique_damage_rows(rows: list) -> list:
    """同一伤害行的相同证据只计一次；优先保留含完整左右列的记录。"""
    result, seen = [], {}
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            result.append(row)
            continue
        field, columns = _panel_field(row[0]), _panel_columns(row[1:])
        if not columns or field not in seen:
            seen[field] = len(result)
            result.append(row)
            continue
        index = seen[field]
        previous = _panel_columns(result[index][1:])
        if any(
            side in previous
            and not math.isclose(previous[side], number, rel_tol=1e-9, abs_tol=1e-9)
            for side, number in columns.items()
        ):
            result.append(row)
        elif len(columns) > len(previous):
            result[index] = row
    return result


def clean_weapon_name(s) -> str:
    """把标题栏里的武器名清干净。

    实测样本（维护者 2026-09-20 报障「暮斩显示未能识别」）：

        "+ 升级／暮斩 [30] 3"  →  应当是「暮斩」

    两个坑：① 分隔符是**全角 ／**（U+FF0F），只切 ASCII "/" 会漏；
    ② 武器等级 [30] 右侧那个**极化次数**（这里是 3）会被读进来，
    残留在名字尾部 → 精确匹配失败（`find_weapon("暮斩 3")` 查不到，
    而 `find_weapon("暮斩")` 是好的 → 证明问题只在清洗环节）。
    """
    t = str(s or "").strip()
    t = t.replace("／", "/").replace("＼", "\\")  # 全角分隔符
    if "/" in t:  # 「升级 / 武器名」的固定格式
        t = t.split("/")[-1]
    t = re.sub(r"^[+＋\-—=\s]*升\s*级\s*[:：]?\s*", "", t)  # 「升级:武器名」
    # 段位/等级方括号：半角、全角、中文书名号式括号都要认
    t = re.sub(r"[（(\[【〔]\s*\d+\s*[)）\]】〕]", "", t)
    t = re.sub(r"等级\s*\d+\s*$", "", t)  # 「等级 30」后缀（新版 UI）
    t = re.sub(r"^[+＋\-—=\s]+", "", t)
    # ★ 尾部孤立的 1~2 位数字 = 标题栏的**极化次数**（不是武器名的一部分）。
    #   要求前面有空白或分隔符，避免切坏「MK1-布莱顿」这类本就带数字的名字。
    t = re.sub(r"[\s·|/]+\d{1,2}\s*$", "", t)
    return t.strip(" +·|/／").strip()


def _balance_brackets(s: str) -> str:
    """就地修复缺失/多余的收尾括号（模型少写 ``]`` 的常见笔误，字符串字面量内不算）。

    2026-10-05 识图测试实证：30B 对裁剪图（无卡框）吐出
    ``…"negative": [["装填速度", 23.3]}`` —— 少一个 ``]``，json.loads 直接失败，
    weapon 全丢（而窄读词条是全对的）。⇒ 扫描时遇到与栈顶不匹配的闭合符，
    **先把缺的收尾插在它前面**再收当前；多余闭合符直接丢弃；结尾按栈逆序补齐。
    """
    out: list = []
    stack: list = []
    pairs = {"]": "[", "}": "{"}
    closer = {"[": "]", "{": "}"}
    in_str = esc = False
    for ch in s:
        if in_str:
            out.append(ch)
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
            out.append(ch)
            continue
        if ch in "[{":
            stack.append(ch)
            out.append(ch)
            continue
        if ch in "]}":
            want = pairs[ch]
            if stack and stack[-1] == want:
                stack.pop()
                out.append(ch)
                continue
            if stack:
                while stack and stack[-1] != want:
                    out.append(closer[stack.pop()])
                if stack and stack[-1] == want:
                    stack.pop()
                    out.append(ch)
                # 栈空仍不匹配 ⇒ 多余闭合符，丢弃
            # 栈空 ⇒ 多余闭合符，丢弃
            continue
        out.append(ch)
    while stack:
        out.append(closer[stack.pop()])
    return "".join(out)


def _normalized_box(value) -> Optional[dict]:
    """共同协议的中心坐标框；只校验显式格式，不猜宽高顺序或像素单位。"""
    if isinstance(value, (list, tuple)) and len(value) == 4:
        value = dict(zip(("x", "y", "h", "w"), value))
    if not isinstance(value, dict):
        return None
    if any(
        not isinstance(value.get(k), (int, float))
        or isinstance(value[k], bool)
        or not math.isfinite(value[k])
        for k in ("x", "y", "h", "w")
    ):
        return None
    box = {k: float(value[k]) for k in ("x", "y", "h", "w")}
    if not (0 <= box["x"] <= 1000 and 0 <= box["y"] <= 1000):
        return None
    if not (0 < box["h"] <= 1000 and 0 < box["w"] <= 1000):
        return None
    return box


def coordinate_aabb(values) -> Optional[list]:
    """旋转框 cx,cy,width,height,angle → 图像轴对齐 x,y,h,w,angle。

    按显式角度做几何变换，不按文本、模型或宽高比例猜测横竖。
    """
    try:
        if any(isinstance(value, bool) for value in values):
            return None
        x, y, width, height, angle = map(float, values)
    except (ValueError, TypeError):
        return None
    if not all(math.isfinite(v) for v in (x, y, width, height, angle)):
        return None
    if not (0 <= x <= 1000 and 0 <= y <= 1000 and 0 < width <= 1000 and 0 < height <= 1000):
        return None
    if not -90 <= angle <= 90:
        return None
    cosine, sine = abs(math.cos(math.radians(angle))), abs(math.sin(math.radians(angle)))
    return [
        x,
        y,
        round(width * sine + height * cosine, 8),
        round(width * cosine + height * sine, 8),
        angle,
    ]


def coordinate_text_lines(text: str) -> list[str]:
    """显式坐标JSON和CSV共用协议；只解完整JSON，不修截断、不猜四元数组。"""
    raw = str(text or "").strip()
    payload = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", raw)
    try:
        items = json.loads(payload)
    except (ValueError, TypeError):
        lines = []
        for line in raw.splitlines():
            # 部分坐标文本在行首保留一个标记字符；仅在五个坐标与正文
            # 都完整时解包，不补正文、不修截断 JSON，也不剥词条中的符号。
            candidate = line.strip().removeprefix("<")
            parts = candidate.split(",", 5)
            if len(parts) == 6 and parts[5].strip() and coordinate_aabb(parts[:5]):
                lines.append(candidate)
            else:
                lines.append(line)
        return lines
    if not isinstance(items, list):
        return raw.splitlines()
    lines = []
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("text"), str):
            lines.append(json.dumps(item, ensure_ascii=False))
            continue
        rect = item.get("rotate_rect")
        corners = item.get("bbox_2d")
        if rect is None and isinstance(corners, list) and len(corners) == 4:
            if all(isinstance(v, (float, int)) and not isinstance(v, bool) for v in corners):
                x1, y1, x2, y2 = corners
                if 0 <= x1 < x2 <= 1000 and 0 <= y1 < y2 <= 1000:
                    rect = [(x1 + x2) / 2, (y1 + y2) / 2, x2 - x1, y2 - y1, 0]
        if isinstance(rect, list) and len(rect) == 5 and coordinate_aabb(rect):
            lines.append(",".join(map(str, rect)) + "," + item["text"].replace("\n", " "))
        else:
            # 疑似词条即使没有坐标，也保留正文进入后续完整性检查。
            lines.append(item["text"] or json.dumps(item, ensure_ascii=False))
    return lines


def parse_coordinate_ocr(text: str) -> Optional[dict]:
    """把带归一化坐标的 OCR 文字归入配卡字段；不补读缺失行。

    每行 cx,cy,width,height,angle,text，旋转框先转为图像轴对齐范围。
    同行数字只按几何位置关联；卡片耗能只在本卡右上区域寻找。
    无库存分界时仍保留可见卡片候选；装备完整性由像素槽位和分区单独判定。
    """
    boxes = []
    for source_line, line in enumerate(coordinate_text_lines(text), 1):
        parts = line.strip().split(",", 5)
        if len(parts) != 6 or not parts[5].strip():
            continue
        geometry = coordinate_aabb(parts[:5])
        if geometry is None:
            continue
        x, y, h, w, angle = geometry
        boxes.append(dict(x=x, y=y, h=h, w=w, text=parts[5].strip(), source_line=source_line))
    if not boxes:
        # 一些通用视觉接口返回显式左上/右下框。沿用相同字段归类逻辑，
        # 不把 JSON 中的任意四元数组猜作坐标，也不修补截断的坐标列表。
        payload = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", str(text or "").strip())
        try:
            items = json.loads(payload)
        except (ValueError, TypeError):
            items = None
        if isinstance(items, list):
            for source_line, item in enumerate(items, 1):
                if not isinstance(item, dict) or not isinstance(item.get("text"), str):
                    continue
                corners = item.get("bbox_2d")
                if (
                    not isinstance(corners, list)
                    or len(corners) != 4
                    or any(
                        not isinstance(n, (int, float))
                        or isinstance(n, bool)
                        or not math.isfinite(n)
                        for n in corners
                    )
                    or not (0 <= corners[0] < corners[2] <= 1000)
                    or not (0 <= corners[1] < corners[3] <= 1000)
                ):
                    continue
                x1, y1, x2, y2 = corners
                if item["text"].strip():
                    boxes.append(
                        dict(
                            x=(x1 + x2) / 2,
                            y=(y1 + y2) / 2,
                            h=y2 - y1,
                            w=x2 - x1,
                            text=item["text"].strip(),
                            source_line=source_line,
                        )
                    )
        if not boxes:
            return None
    for box in boxes:
        normalized = re.sub(r"^(主要|次要|近战)\s*[:：·・.\uff0e]\s*", r"\1·", box["text"])
        if normalized != box["text"]:
            box["observed_text"] = box["text"]
            box["text"] = normalized
    # Prime 属于上一行卡名时合回已有字符，不能把单个后缀模糊成另一张卡。
    consumed = set()
    for suffix in boxes:
        if suffix["text"].casefold() != "prime":
            continue
        candidates = []
        for name in boxes:
            if (
                name is suffix
                or not 0 < suffix["y"] - name["y"] < max(name["h"], suffix["h"]) * 1.5
            ):
                continue
            if abs(name["x"] - suffix["x"]) > max(name["w"], suffix["w"]) * 0.3:
                continue
            combined = name["text"] + " Prime"
            rec, method = match_mod(combined)
            if rec and _norm(combined) in {_norm(rec.get("zh")), _norm(rec.get("name"))}:
                candidates.append((name, combined))
        if len(candidates) == 1:
            name, combined = candidates[0]
            name["text"] = combined
            name["joined_source_lines"] = [name["source_line"], suffix["source_line"]]
            consumed.add(id(suffix))
    boxes = [b for b in boxes if id(b) not in consumed and b["text"].casefold() != "prime"]

    def label(b):
        return re.sub(r"[\s\u2666\u25c7\u25c6]+", "", b["text"])

    def same_line(a, b):
        # 小于半个字高，不能借上下相邻面板行的数字。
        return abs(a["y"] - b["y"]) <= min(14, min(a["h"], b["h"]) * 0.34)

    number = re.compile(
        r"[+−-]?\d+(?:,\d{3})*(?:\.\d+)?(?:\s*(?:%|倍|米|秒))?"
        r"(?:(?:\s*(?:>|→|▶|▸)\s*|\s+)[+−-]?\d+(?:,\d{3})*(?:\.\d+)?"
        r"(?:\s*(?:%|倍|米|秒))?)*"
    )
    scalar_labels = {
        "攻击速度": "attack_speed",
        "射击速度": "attack_speed",
        "射速": "attack_speed",
        "暴击几率": "crit_chance",
        "暴击伤害": "crit_damage",
        "暴击倍率": "crit_damage",
        "触发几率": "status_chance",
        "范围": "range",
        "震地攻击": "slam_damage",
        "滑行攻击": "slide_damage",
        "连击持续时间": "combo_duration",
        "伤害穿透系数": "follow_through",
        "多重射击": "multishot",
        "弹匣": "magazine",
        "弹药拾取": "ammo_pickup",
        "弹药最大值": "ammo_max",
        "精准度": "accuracy",
        "装填": "reload",
    }
    damage_labels = {"冲击", "穿刺", "切割", "火焰", "冰冻", "电击", "毒素"} | _TOTAL_LABELS
    damage_labels.update(ELEM_ALL_ZH.values())
    result = {"weapon": None, "capacity": None, "mods": [], "panel": {}, "arcanes": []}
    sources = {"panel_rows": [], "mod_rows": [], "inventory_boundary": None}
    titles = [b for b in boxes if b["y"] < 200 and re.match(r"^[+＋\s]*升级\s*[/／:：]", b["text"])]
    if len(titles) == 1:
        result["weapon"] = clean_weapon_name(titles[0]["text"]) or None

    # 容量行既提供容量值，也限制左面板的横向范围。
    panel_end = None
    for b in boxes:
        if label(b) != "容量":
            continue
        caps = [
            v
            for v in boxes
            if v["x"] > b["x"] + b["w"] / 2
            and same_line(b, v)
            and re.fullmatch(r"\d+\s*/\s*\d+", v["text"])
        ]
        if len(caps) == 1:
            result["capacity"] = caps[0]["text"]
            panel_end = caps[0]["x"] + caps[0]["w"] / 2 + 15
    configs = [
        b["x"] - b["w"] / 2 for b in boxes if re.fullmatch(r"配置\s*[A-Z\uff21-\uff3a]", b["text"])
    ]
    has_panel_labels = any(label(b) in scalar_labels or label(b) in damage_labels for b in boxes)
    if panel_end is None and configs and has_panel_labels:
        panel_end = min(configs)

    for b in sorted(boxes, key=lambda v: (v["y"], v["x"])):
        raw_label = label(b)
        name = re.split(r"[（(]", raw_label, 1)[0]
        if name not in scalar_labels and name not in damage_labels:
            continue
        if panel_end is not None and b["x"] >= panel_end:
            continue
        values = sorted(
            [
                v
                for v in boxes
                if v["x"] > b["x"] + b["w"] / 2
                and same_line(b, v)
                and (panel_end is None or v["x"] < panel_end)
                and number.fullmatch(v["text"])
            ],
            key=lambda v: v["x"],
        )
        if not values:
            continue
        texts = [v["text"] for v in values]
        sources["panel_rows"].append(dict(label=b["text"], values=texts, y=b["y"]))
        result["panel"].setdefault("_ocr_rows", []).append([b["text"], ">".join(texts)])
        if name in scalar_labels:
            result["panel"][scalar_labels[name]] = ">".join(texts)
        else:
            row = [name, ">".join(texts)]
            result["panel"].setdefault("damage_rows", []).append(row)
            if name in _TOTAL_LABELS:
                result["panel"]["total"] = row[:]

    boundaries = [
        b for b in boxes if label(b) == "所有" or re.fullmatch(r"(?:搜寻|搜索)[.…。]*", label(b))
    ]
    boundary = min((b["y"] for b in boundaries), default=None)
    sources["inventory_boundary"] = boundary
    sources["equipment_boundary_confirmed"] = boundary is not None
    cards = []
    for b in boxes:
        if (boundary is not None and b["y"] >= boundary) or b["y"] < 200:
            continue
        if panel_end is not None and b["x"] <= panel_end:
            continue
        rec, how = match_mod(b["text"])
        # 完整名字可识别，不因截图裁掉仓库栏而丢弃整张已读卡；
        # 无底部边界时保留候选身份，不能据此宣称布局完整。
        if not rec or (
            how == "子串" and _norm(b["text"]) not in {_norm(rec.get("name")), _norm(rec.get("zh"))}
        ):
            continue
        cards.append((b, rec))
    known_boxes = {id(b) for b, _ in cards}
    for b in boxes:
        if id(b) in known_boxes or not (200 <= b["y"] < (boundary or 1000)):
            continue
        if panel_end is not None and b["x"] <= panel_end:
            continue
        if not re.search(r"[A-Za-z\u4e00-\u9fff]", b["text"]) or len(b["text"]) > 40:
            continue
        if re.fullmatch(r"(?:主要|次要|近战)[·・].+", b["text"]):
            continue
        if any(same_line(b, other) for other, _ in cards):
            cards.append((b, None))
    unique_cards = []
    for card in sorted(cards, key=lambda c: (-len(c[0]["text"]), c[0]["source_line"])):
        b, rec = card
        duplicate = None
        for other, other_rec in unique_cards:
            text, other_text = _norm(b["text"]), _norm(other["text"])
            same_identity = rec and other_rec and rec["name"] == other_rec["name"]
            contained_text = len(text) >= 2 and text in other_text
            if not (same_identity or contained_text):
                continue
            overlap_x = max(
                0,
                min(b["x"] + b["w"] / 2, other["x"] + other["w"] / 2)
                - max(b["x"] - b["w"] / 2, other["x"] - other["w"] / 2),
            )
            overlap_y = max(
                0,
                min(b["y"] + b["h"] / 2, other["y"] + other["h"] / 2)
                - max(b["y"] - b["h"] / 2, other["y"] - other["h"] / 2),
            )
            if overlap_x * overlap_y >= min(b["w"] * b["h"], other["w"] * other["h"]) * 0.7:
                duplicate = other
                break
        if duplicate is None:
            unique_cards.append(card)
        else:
            sources.setdefault("duplicate_card_observations", []).append(
                {
                    "observation": copy.deepcopy(b),
                    "retained_source_line": duplicate["source_line"],
                    "reason": "same_region_repeated_text",
                }
            )
    cards = unique_cards
    card_rows = []
    for card in sorted(cards, key=lambda c: (c[0]["y"], c[0]["x"])):
        if card_rows and same_line(card_rows[-1][0][0], card[0]):
            card_rows[-1].append(card)
        else:
            card_rows.append([card])
    ordered_cards = [card for row in card_rows for card in sorted(row, key=lambda c: c[0]["x"])]
    grid_rows = [row for row in card_rows if len(row) >= 3]
    auxiliary_boxes = set()
    if len(grid_rows) >= 2:
        grid_boxes = {id(b) for row in grid_rows for b, _ in row}
        gaps = sorted(
            right[0]["x"] - left[0]["x"]
            for row in grid_rows
            for left, right in zip(
                sorted(row, key=lambda c: c[0]["x"]),
                sorted(row, key=lambda c: c[0]["x"])[1:],
            )
            if right[0]["x"] > left[0]["x"]
        )
        if gaps:
            grid_pitch = gaps[len(gaps) // 2]
            grid_right = max(b["x"] for row in grid_rows for b, _ in row)
            auxiliary_boxes = {
                id(b)
                for b, _ in cards
                if id(b) not in grid_boxes and b["x"] > grid_right + grid_pitch / 2
            }
    for b, rec in ordered_cards:
        peers = [c for c, _ in cards if c is not b and same_line(c, b)]
        right_peer = min((p["x"] for p in peers if p["x"] > b["x"]), default=None)
        left_peer = max((p["x"] for p in peers if p["x"] < b["x"]), default=None)
        pitches = [abs(p["x"] - b["x"]) for p in peers]
        # 单独姿态/特殊槽借普通卡行的列距，而不是放宽到整张图。
        if not pitches:
            for a, _ in cards:
                pitches += [
                    abs(c["x"] - a["x"]) for c, _ in cards if c is not a and same_line(a, c)
                ]
        pitch = min(pitches) if pitches else max(b["w"] * 1.8, 80)
        right_edge = (b["x"] + right_peer) / 2 if right_peer else b["x"] + pitch * 0.5
        left_edge = (b["x"] + left_peer) / 2 if left_peer else b["x"] - pitch * 0.5
        drains = [
            v
            for v in boxes
            if re.fullmatch(r"\^?\d{1,2}", v["text"])
            and max(left_edge, b["x"] + pitch * 0.08) < v["x"] < right_edge
            and b["h"] * 0.35 < b["y"] - v["y"] < b["h"] * 2.1
        ]
        drain = to_int(drains[0]["text"]) if len(drains) == 1 else None
        item = dict(name=b["text"], drain=drain, color="?", polarity="?")
        item["_geometry"] = {key: b[key] for key in ("x", "y", "h", "w")}
        item["_geometry_source"] = "coordinate_ocr"
        item["_geometry_view"] = "full_image"
        item["_field_geometry"] = {"name": copy.deepcopy(b), "drain": copy.deepcopy(drains)}
        if id(b) in auxiliary_boxes:
            item["_slot"] = "auxiliary"
        result["mods"].append(item)
        sources["mod_rows"].append(dict(name=b["text"], x=b["x"], y=b["y"], drain=drain))
    for b in boxes:
        if re.fullmatch(r"(?:主要|次要|近战)[·・].+", b["text"]):
            if boundary is None or b["y"] < boundary:
                result["arcanes"].append(
                    {
                        "name": b["text"],
                        "_geometry": copy.deepcopy(b),
                        "_geometry_source": "coordinate_ocr",
                    }
                )
    # 坐标来源保留区域锚点和未定槽位；卡名网格不能证明空槽/特殊槽完整。
    sources["equipment_region"] = {"left": panel_end, "bottom": boundary}
    sources["coordinate_space"] = "normalized_1000"
    sources["boxes"] = copy.deepcopy(boxes)
    sources["inventory_rows"] = [
        copy.deepcopy(b)
        for b in boxes
        if boundary is not None and b["y"] >= boundary and match_mod(b["text"])[0]
    ]
    sources["slot_binding"] = "unknown"
    sources["coverage"] = "unknown"
    result["coordinate_ocr"] = sources
    return result


def observe_capacity_colors(ocr: dict, image_url: str) -> dict:
    """读取容量数字框内的实际像素颜色；只使用已定位数字，不按等级猜颜色。"""
    if not str(image_url).startswith("data:"):
        return ocr
    import base64
    import io

    from PIL import Image

    try:
        image = Image.open(io.BytesIO(base64.b64decode(image_url.split(",", 1)[1]))).convert("HSV")
    except (ValueError, OSError):
        return ocr
    width, height = image.size
    bindings = analyze_layout(ocr)["bindings"]
    for index, mod in enumerate(ocr.get("mods") or []):
        if mod.get("_geometry_view") != "full_image":
            continue
        if (
            mod.get("_geometry_source") == "structured_box"
            and bindings[index]["status"] != "bound_candidate"
        ):
            continue
        drains = mod.get("_field_geometry", {}).get("drain", [])
        tile_color = False
        if mod.get("_geometry_source") == "pixel_tile":
            tiles = [
                t for t in ocr.get("_tile_slots") or [] if t.get("id") == mod.get("_tile_slot_id")
            ]
            if len(tiles) == 1:
                left, top, right, bottom = tiles[0]["box"]
                # 固定卡框内右上耗能徽标，不按文字、容量大小或等级选区域。
                x1, x2 = left + (right - left) * 0.76, right
                y1, y2 = top + (bottom - top) * 0.06, top + (bottom - top) * 0.43
                drains = [
                    {
                        "x": (x1 + x2) * 500,
                        "y": (y1 + y2) * 500,
                        "w": (x2 - x1) * 1000,
                        "h": (y2 - y1) * 1000,
                    }
                ]
                tile_color = True
        if len(drains) != 1:
            continue
        box = drains[0]
        x, y, w, h = [float(box[k]) / 1000 for k in ("x", "y", "w", "h")]
        left, top = max(0, int((x - w / 2) * width)), max(0, int((y - h / 2) * height))
        right, bottom = min(width, int((x + w / 2) * width)), min(height, int((y + h / 2) * height))
        if right <= left or bottom <= top:
            continue
        counts = {"绿": 0, "红": 0, "白": 0}
        for hue, saturation, value in image.crop((left, top, right, bottom)).getdata():
            # 极性不匹配是高饱和红色；铜卡的橙铜色名称/数字并非该状态。
            # 红色徽标本身较暗，不能与绿色/白色共用亮度下限。
            if value >= 80 and (hue <= 8 or hue >= 248) and saturation >= 150:
                counts["红"] += 1
            elif value >= 150 and 40 <= hue <= 115 and saturation >= 90:
                counts["绿"] += 1
            elif saturation <= 55 and value >= 200:
                counts["白"] += 1
        color = None
        if counts["绿"] >= max(5, counts["红"] * 2, counts["白"] * 0.3):
            color = "绿"
        elif counts["红"] >= max(5, counts["绿"] * 2, counts["白"] * 0.3):
            color = "红"
        elif not tile_color and counts["白"] >= max(10, counts["绿"] * 2, counts["红"] * 2):
            # 较大的裁图徽标区含白色边框，不能拿边框颜色证明白色容量。
            color = "白"
        if color:
            mod["color"] = color
            mod["_color_evidence"] = {
                "source": "capacity_badge_pixels" if tile_color else "capacity_number_pixels",
                "counts": counts,
                "box": [left, top, right, bottom],
            }
    return ocr


def observe_arcane_ranks(ocr: dict, image_url: str) -> dict:
    """赋能等级只来自名称框下的独立像素，不采用模型直接猜的等级。"""
    import base64
    import io

    from PIL import Image

    try:
        image = Image.open(io.BytesIO(base64.b64decode(image_url.split(",", 1)[1])))
    except (ValueError, OSError, IndexError):
        return ocr
    for arcane in ocr.get("arcanes") or []:
        arcane["rank"] = None
        if arcane.get("_geometry_source") not in {"coordinate_ocr", "structured_box"}:
            continue
        if arcane.get("_geometry_view") == "unmapped_crop":
            continue
        if arcane.get("_geometry_source") == "structured_box" and (
            arcane.get("_geometry_view") != "full_image" or arcane.get("_geometry_field_errors")
        ):
            continue
        evidence = pips_engine.count_arcane_rank(image, arcane.get("_geometry") or {})
        arcane["_rank_evidence"] = evidence
        arcane["rank"] = evidence["rank"]
    return ocr


def panel_from_rows(rows: list) -> dict:
    """将明确标注的行归入面板字段；不猜缺失标签，也不借相邻行。"""
    scalar = {
        "攻击速度": "attack_speed",
        "射击速度": "attack_speed",
        "暴击几率": "crit_chance",
        "暴击伤害": "crit_damage",
        "暴击倍率": "crit_damage",
        "触发几率": "status_chance",
    }
    damage = (
        set(PHYS_ZH.values())
        | set(ELEM_ALL_ZH.values())
        | {
            "火焰",
            "冰冻",
            "电击",
            "毒素",
            "总计",
            "总伤",
            "总伤害",
            "总",
        }
    )
    panel = {}
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            continue
        label = str(row[0]).strip()
        name = re.split(r"[（(]", label, 1)[0]
        value = row[1] if len(row) == 2 else list(row[1:])
        if name in scalar or name in damage:
            panel.setdefault("_ocr_rows", []).append(list(row))
        if name in scalar:
            panel[scalar[name]] = value
        elif name in damage:
            panel.setdefault("damage_rows", []).append(list(row))
            if name in _TOTAL_LABELS:
                panel["total"] = list(row)
    return panel


def parse_focus_text(text: str, part: str) -> Optional[dict]:
    """分区补读的文字协议：坐标 CSV、JSON 与逐行原文共用确定性解析。"""
    coordinate = parse_coordinate_ocr(text)
    if coordinate and coordinate.get(part):
        return coordinate
    if part == "arcanes":
        arcanes = []
        for line in str(text or "").splitlines():
            line = re.sub(r"^\s*[-*•]\s*", "", line).strip()
            line = re.sub(r"^(主要|次要|近战)\s*[:：·・.\uff0e]\s*", r"\1·", line)
            if re.fullmatch(r"(?:主要|次要|近战)[·・][^\s:：|｜]+", line):
                arcanes.append({"name": line})
        return {"arcanes": arcanes} if arcanes else None
    parsed = parse_vision_json(text) or {}
    if part == "mods":
        if isinstance(parsed.get("mods"), list) and parsed["mods"]:
            return parsed
        mods, arcanes = [], []
        for line in str(text or "").splitlines():
            line = re.sub(r"^\s*[-*•]\s*", "", line).strip()
            if re.fullmatch(r"(?:主要|次要|近战)[·・].+", line):
                arcanes.append({"name": line})
                continue
            match = re.fullmatch(r"(.+?)\s*[|｜]\s*(\^?\d{1,2}|[?？]|null)", line)
            if match and not re.fullmatch(r"\d+", match[1].strip()):
                mods.append(
                    {
                        "name": match[1].strip(),
                        "drain": to_int(match[2]),
                        "color": "?",
                        "polarity": "?",
                    }
                )
        return {"mods": mods, "arcanes": arcanes} if mods else None
    if isinstance(parsed.get("panel"), dict):
        return {"panel": parsed["panel"]}
    rows = []
    for row in parsed.get("rows") or []:
        if isinstance(row, dict):
            row = [
                row.get("label", row.get("name", row.get("行名"))),
                row.get("value", row.get("数值")),
            ]
        if isinstance(row, (list, tuple)) and len(row) >= 2 and row[0]:
            rows.append(row)
    if not rows:
        for line in str(text or "").splitlines():
            line = re.sub(r"^\s*[-*•]\s*", "", line).strip()
            match = re.fullmatch(r"\s*([^:：|｜\d]+?)\s*(?:[:：|｜]|\s)\s*([+−-]?\d.*)", line)
            if match:
                rows.append([match[1].strip(), match[2].strip()])
    panel = panel_from_rows(rows)
    return {"panel": panel} if panel else None


def merge_focus_mods(original_mods: list, focused_mods: list) -> Optional[list]:
    """用共同卡确认补读来源；保留原卡顺序、版本与已有容量，不原地改输入。"""
    if not focused_mods or any(not isinstance(m, dict) for m in focused_mods):
        return None
    if not original_mods:
        return [dict(m) for m in focused_mods]
    original_records = [
        match_mod(str(m.get("name") or ""))[0] if isinstance(m, dict) else None
        for m in original_mods
    ]
    if any(rec is None for rec in original_records):
        return [dict(m) for m in focused_mods] if len(focused_mods) >= len(original_mods) else None

    matched = set()
    additions = []
    merged = [dict(m) for m in original_mods]
    for focused in focused_mods:
        rec, _ = match_mod(str(focused.get("name") or ""))
        canonical = _norm(rec.get("name")) if rec else ""
        candidates = [
            i
            for i, original in enumerate(original_records)
            if canonical and _norm(original.get("name")) == canonical
        ]
        if not candidates:
            short_names = {_norm(focused.get("name"))}
            if rec:
                short_names.update(_norm(rec.get(key)) for key in ("name", "zh"))
            candidates = [
                i
                for i, original in enumerate(original_records)
                if any(
                    short and long != short and long.endswith(short)
                    for short in short_names
                    for long in (_norm(original.get("name")), _norm(original.get("zh")))
                )
            ]
        if len(candidates) == 1:
            index = candidates[0]
            matched.add(index)
            for key, value in focused.items():
                if key in {
                    "_slot",
                    "_geometry",
                    "_geometry_view",
                    "_geometry_source",
                    "_field_geometry",
                    "_geometry_observed",
                    "_geometry_transform",
                } or merged[index].get(key) in (None, "", "?"):
                    if key != "name":
                        merged[index][key] = value
        else:
            additions.append((dict(focused), rec))
    if len(matched) < max(1, (len(original_mods) + 1) // 2):
        return None
    stances = [m for m, rec in additions if rec and rec.get("compat") == "Stance"]
    others = [m for m, rec in additions if not rec or rec.get("compat") != "Stance"]
    return stances + merged + others


def normalize_scan_structure(observed: dict) -> dict:
    """把共同JSON协议的面板行转为内部表示；模型等级/坐标保持原始观察。"""
    result = copy.deepcopy(observed)
    for field in ("mods", "arcanes"):
        raw_items = result.get(field)
        if raw_items is None:
            result[field] = []
            continue
        if isinstance(raw_items, (dict, str)):
            raw_items = [raw_items]
        if not isinstance(raw_items, list):
            raw_items = [raw_items]
        items = []
        for index, raw in enumerate(raw_items):
            if isinstance(raw, dict):
                items.append(raw)
            elif isinstance(raw, str) and raw.strip():
                items.append({"name": raw.strip(), "_structure_source": "name_string"})
            else:
                items.append({"name": None, "_invalid_observation": raw})
                result.setdefault("_structure_errors", []).append(
                    {"field": field, "index": index, "reason": "invalid_item_shape"}
                )
        result[field] = items
    for item in (result.get("mods") or []) + (result.get("arcanes") or []):
        if not isinstance(item, dict) or "name_box" not in item:
            continue
        if item.get("_geometry_source") in {"coordinate_ocr", "structured_box"}:
            continue
        # 结构化框属于模型观察，和坐标OCR保留不同来源；后续必须匹配像素槽位。
        item["_geometry_source"] = "structured_box"
        box = _normalized_box(item.get("name_box"))
        if not box:
            item["_geometry_field_errors"] = ["name_box"]
            continue
        item["_geometry"] = box
        fields = item["_field_geometry"] = {"name": copy.deepcopy(box), "drain": []}
        if item.get("drain_box") is not None:
            drain = _normalized_box(item["drain_box"])
            if drain:
                fields["drain"] = [drain]
            else:
                item["_geometry_field_errors"] = ["drain_box"]
    # 赋能类别是截图自身的字段标记；模型把它装进 MOD 数组也不改变类别。
    # 原始结构仍保留在 recognition.parsed，不按某张卡的名称或数值纠错。
    arcanes = result.setdefault("arcanes", [])
    mods = []
    for index, mod in enumerate(result.get("mods") or []):
        name = str(mod.get("name") or "").strip() if isinstance(mod, dict) else ""
        if (
            re.fullmatch(r"(?:主要|次要|近战)\s*[:：·・.\uff0e]\s*[^\s:：·・.\uff0e]+", name)
            and not match_mod(name)[0]
        ):
            canonical = re.sub(r"\s*[:：·・.\uff0e]\s*", "·", name)
            if not any(
                isinstance(a, dict)
                and re.sub(r"\s*[:：·・.\uff0e]\s*", "·", str(a.get("name") or "")) == canonical
                for a in arcanes
            ):
                arcane = {"name": canonical}
                for key, value in mod.items():
                    if key.startswith("_geometry") or key in {
                        "name_box",
                        "rank_box",
                        "_field_geometry",
                    }:
                        arcane[key] = copy.deepcopy(value)
                arcanes.append(arcane)
            result.setdefault("_field_reclassification", []).append(
                {
                    "source_field": "mods",
                    "source_index": index,
                    "target_field": "arcanes",
                    "raw": copy.deepcopy(mod),
                }
            )
        elif (
            name in {"连击", "容量", "所有", "消耗量", "配置A", "配置B", "配置C"}
            and not match_mod(name)[0]
        ):
            result.setdefault("_field_reclassification", []).append(
                {
                    "source_field": "mods",
                    "source_index": index,
                    "target_field": "interface_label",
                    "raw": copy.deepcopy(mod),
                }
            )
        else:
            if (
                isinstance(mod, dict)
                and _color_hint(mod.get("color"))
                and not mod.get("_color_evidence")
            ):
                mod["_color_source"] = "model_hint"
            mods.append(mod)
    if "mods" in result:
        result["mods"] = mods
    for arcane in result.get("arcanes") or []:
        if isinstance(arcane, dict):
            arcane["name"] = re.sub(
                r"^(主要|次要|近战)\s*[:：·・.\uff0e]\s*",
                r"\1·",
                str(arcane.get("name") or "").strip(),
            )
    panel = result.get("panel")
    raw_rows = (
        panel if isinstance(panel, list) else panel.get("rows") if isinstance(panel, dict) else None
    )
    if isinstance(raw_rows, list):
        rows = []
        for row in raw_rows:
            if not isinstance(row, dict) or not row.get("label"):
                continue
            values = [str(row[key]) for key in ("left", "right") if row.get(key) is not None]
            if values:
                rows.append([row["label"], ">".join(values)])
        result["panel"] = panel_from_rows(rows)
        result["_structured_panel_rows"] = copy.deepcopy(raw_rows)
    return result


def merge_equipment_read(primary: dict, focused: dict) -> dict:
    """完整补读保留其槽位顺序，共同卡沿用独立OCR几何；不信模型猜的坐标。"""
    focused = normalize_scan_structure(focused)
    old_mods, new_mods = primary.get("mods") or [], focused.get("mods") or []

    def key(mod):
        rec, _how = match_mod(str(mod.get("name") or "")) if isinstance(mod, dict) else (None, None)
        return (
            rec.get("name")
            if rec
            else (
                "raw:" + _norm(mod.get("name"))
                if isinstance(mod, dict) and mod.get("name")
                else None
            )
        )

    old = {key(m): m for m in old_mods if key(m)}
    keys = [key(m) for m in new_mods]
    complete = (
        1 <= len(new_mods) <= 10
        and all(keys)
        and len(set(k for k in keys if k)) == len([k for k in keys if k])
        and (
            set(old) <= set(keys)
            or (len(old_mods) > 10 and len(set(old) & set(keys)) >= (len(keys) + 1) // 2)
        )
    )
    if complete:
        mods = []
        for raw in new_mods:
            item = copy.deepcopy(raw)
            previous = (
                old.get(key(raw), {}) if sum(key(m) == key(raw) for m in old_mods) == 1 else {}
            )
            for field, value in previous.items():
                if field.startswith("_geometry") or field in {
                    "_slot",
                    "_field_geometry",
                    "_color_evidence",
                }:
                    item[field] = copy.deepcopy(value)
                elif field in {"drain", "color", "polarity"} and value not in (None, "", "?"):
                    item[field] = copy.deepcopy(value)
            mods.append(item)
    else:
        mods = merge_focus_mods(old_mods, new_mods)
    candidate = {**primary, "mods": mods if mods is not None else old_mods}
    candidate["_equipment_merge"] = {
        "complete_shared_identity": complete,
        "merged": mods is not None,
        "shared_identity_preserved": bool(old) and set(old) <= {key(m) for m in (mods or [])},
    }
    if focused.get("weapon"):
        candidate["weapon"] = focused["weapon"]
    if focused.get("arcanes"):
        arcanes = copy.deepcopy(focused["arcanes"])
        for arcane in arcanes:
            category = re.split(r"[·・]", str(arcane.get("name") or ""))[0]
            anchors = [
                a
                for a in primary.get("arcanes") or []
                if re.split(r"[·・]", str(a.get("name") or ""))[0] == category
                and a.get("_geometry_source") == "coordinate_ocr"
            ]
            if len(anchors) == 1:
                anchor = anchors[0]
                official_names = {
                    _norm(rec.get(field))
                    for rec in dc.arcanes_payload().values()
                    for field in ("name", "zh")
                    if rec.get(field)
                }
                if (
                    _norm(anchor.get("name")) in official_names
                    and _norm(arcane.get("name")) not in official_names
                ):
                    arcane["_name_selection"] = {
                        "reason": "keep_exact_catalog_observation",
                        "discarded_observation": arcane.get("name"),
                    }
                    arcane["name"] = anchor["name"]
                arcane["_geometry"] = copy.deepcopy(anchors[0]["_geometry"])
                arcane["_geometry_source"] = "coordinate_ocr"
        candidate["arcanes"] = arcanes
    return candidate


def merge_tile_read(primary: dict, observed: dict, tiles: list) -> dict:
    """隔离裁图给出名称/容量，代码赋予原图位置；模型的等级和几何字段不进入结果。"""
    rows = observed.get("mods")
    if not isinstance(rows, list):
        return primary
    by_label = {tile["label"]: tile for tile in tiles}
    labels = [row.get("tile") for row in rows if isinstance(row, dict)]
    if len(labels) != len(rows) or len(set(labels)) != len(labels) or set(labels) - set(by_label):
        return primary
    result = copy.deepcopy(primary)
    recovered = []
    for row in rows:
        tile = by_label[row["tile"]]
        name = str(row.get("name") or "").strip()
        if not name:
            continue
        if name in {"连击", "容量", "所有", "消耗量"} and not match_mod(name)[0]:
            continue
        rec, how = match_mod(name)
        # 裁图名称必须是完整词表身份；近形候选仍留在原始观察，不凭位置自证读对。
        if not rec or how == "子串":
            continue
        peers = [
            m
            for m in primary.get("mods") or []
            if (match_mod(str(m.get("name") or ""))[0] or {}).get("name") == rec["name"]
        ]
        if len(peers) > 1:
            continue
        item = copy.deepcopy(peers[0]) if peers else {}
        item.update(
            name=name,
            drain=row.get("drain"),
            color=row.get("color"),
            _color_source="model_hint",
            _geometry_source="pixel_tile",
            _geometry_view="full_image",
            _tile_slot_id=tile["id"],
        )
        item.pop("_field_geometry", None)
        item.pop("_color_evidence", None)
        left, top, right, bottom = tile["box"]
        item["_geometry"] = {
            "x": (left + right) * 500,
            "y": (top + bottom) * 500,
            "h": (bottom - top) * 1000,
            "w": (right - left) * 1000,
        }
        item["_tile_observation"] = copy.deepcopy(row)
        recovered.append(item)
    names = [(match_mod(m["name"])[0] or {}).get("name") for m in recovered]
    if len(set(names)) != len(names):
        return primary
    old_names = [
        (match_mod(str(m.get("name") or ""))[0] or {}).get("name")
        for m in primary.get("mods") or []
    ]
    if not recovered or len(set(names) & set(old_names)) < max(
        1, len(set(n for n in old_names if n)) // 2
    ):
        return primary
    # 复读没覆盖到的卡保留待核对，不根据固定槽数或满级假设补答案。
    result["mods"] = recovered + [
        copy.deepcopy(m)
        for m, name in zip(primary.get("mods") or [], old_names)
        if name not in names
    ]
    result["_tile_slots"] = copy.deepcopy(tiles)
    result["_tile_read"] = copy.deepcopy(observed)
    return result


def parse_vision_json(text: str, *, preserve_numbers: bool = False) -> dict:
    """从模型输出里抠出 JSON（容忍思考段、markdown 代码块、前后废话、缺收尾括号）。

    注意 thinking 类模型（glm-4.1v-thinking-flash）会先吐一段 ``<think>…</think>``，
    里面可能含花括号，必须先剥掉再取 JSON。
    """

    def keep_duplicates(pairs):
        result, repeated = {}, []
        for key, value in pairs:
            if key in result:
                repeated.append([key, result[key]])
            result[key] = value
        if repeated:
            result["_ocr_duplicate_fields"] = repeated
        return result

    s = str(text or "").strip()
    s = re.sub(r"<think(?:ing)?>.*?</think(?:ing)?>", "", s, flags=re.S | re.I)
    s = re.sub(r"</?think(?:ing)?>", "", s, flags=re.I)
    m = re.search(r"```(?:json)?\s*(.+?)```", s, flags=re.S)
    if m:
        s = m.group(1).strip()
    i, j = s.find("{"), s.rfind("}")
    if i >= 0 and j > i:
        s = s[i : j + 1]
    for candidate in (s, re.sub(r",\s*([}\]])", r"\1", s), _balance_brackets(s)):
        try:
            d = json.loads(
                candidate,
                object_pairs_hook=keep_duplicates,
                **({"parse_float": str, "parse_int": str} if preserve_numbers else {}),
            )
        except Exception:  # noqa: BLE001
            continue
        if isinstance(d, dict):
            if isinstance(d.get("panel"), dict):
                for key, previous in d.get("_ocr_duplicate_fields", []):
                    if key == "panel" and isinstance(previous, dict):
                        d["panel"].setdefault("_ocr_prior_panels", []).append(previous)
            return d
    return {}


# ---------------------------------------------------------------------------
# 本地库匹配
# ---------------------------------------------------------------------------
def mods_payload() -> dict:
    """MOD 表：可算（有每级数值）+ 仅识别（只有名字与容量元数据）。"""
    if not _payload:
        try:
            d = json.loads((_DATA / "mods_stats.json").read_text(encoding="utf-8"))
            _payload["mods"] = d.get("mods") or {}
            _payload["names"] = d.get("names") or {}
        except Exception:  # noqa: BLE001
            _payload["mods"], _payload["names"] = {}, {}
    return _payload


def mod_pool() -> dict[str, dict]:
    """合并出匹配池；可算条目覆盖仅识别条目（前者字段更全）。"""
    payload = mods_payload()
    pool: dict[str, dict] = {}
    for k, v in (payload.get("names") or {}).items():
        pool[k] = dict(v, calculable=False)
    for k, v in (payload.get("mods") or {}).items():
        pool[k] = dict(v, calculable=True)
    return pool


def match_mod(query: str) -> tuple[Optional[dict], str]:
    """把截图上的 MOD 名匹配到库记录，返回 (记录, 匹配方式)。

    阶梯：精确 → 唯一子串 → 编辑距离。**不做多义子串**：
    像「斩铁」会同时命中 True Steel / Galvanized Steel / Sacrificial Steel，
    这时宁可交给编辑距离去选最像的那个，也不要随便挑一个。
    """
    q = _norm(query)
    if not q:
        return None, ""
    pool = mod_pool()
    norm_map: dict[str, dict] = {}
    for rec in pool.values():
        for field in ("zh", "name"):
            n = _norm(rec.get(field))
            if n:
                norm_map.setdefault(n, rec)

    if q in norm_map:
        return norm_map[q], "精确"
    hits = {id(v): v for k, v in norm_map.items() if q in k}
    if len(hits) == 1:
        return next(iter(hits.values())), "子串"
    near = difflib.get_close_matches(q, list(norm_map), n=1, cutoff=0.72)
    if near:
        return norm_map[near[0]], "近似"
    # 同族兜底：「前缀 + 空格 + 词」的族名卡（镀层/赤毒/Prime…）OCR 错两字时
    # 全局相似度会掉到 0.72 以下（2026-10-09 实测「镀层 氚晶猫具」= 0.667）。
    # 只在**同前缀、同长度**的候选里放宽到 0.6，且第一名须领先第二名 0.15 ——
    # 族内其它卡与之相似度只剩前缀那点，不会被误吸
    raw = str(query or "").strip()
    if " " in raw:
        pre = _norm(raw.split()[0])
        fam = [k for k in norm_map if k.startswith(pre) and len(k) == len(q)]
        scored = sorted(
            ((difflib.SequenceMatcher(None, q, k).ratio(), k) for k in fam), reverse=True
        )
        if (
            scored
            and scored[0][0] >= 0.6
            and (len(scored) < 2 or scored[0][0] - scored[1][0] >= 0.15)
        ):
            return norm_map[scored[0][1]], "同族近似"
    return None, ""


def _color_hint(color) -> Optional[str]:
    """容量数字颜色 → 分支提示：matched / mismatch / normal / None。"""
    c = str(color or "").strip().lower()
    if c in ("绿", "green", "matched"):
        return "matched"
    if c in ("红", "red", "mismatch"):
        return "mismatch"
    if c in ("白", "white", "normal"):
        return "normal"
    return None


def infer_rank(
    mod: dict, drain: Optional[int], color: Optional[str] = None
) -> tuple[Optional[int], str]:
    """用卡片上的容量数字反推实际等级。

    三态（wiki /w/Polarity 原文规则）：
    * 槽位极性匹配：`drain = ceil((base_drain + rank)/2)` —— 数字显示**绿色**
    * 槽位极性不合：`drain = round((base_drain + rank) × 1.25)` —— 数字显示**红色**
    * 无加成（槽位无极性等）：`drain = base_drain + rank` —— 数字显示白色

    数字颜色约束兼容分支；候选唯一才由容量定级。
    绿色进位仍可能留下相邻两等级，不能凭端局/满级先验破平。
    """
    base = mod.get("base_drain")
    max_rank = mod.get("max_rank")
    if drain is None:
        return None, "没读到卡片容量数字"
    if base is None or max_rank is None:
        return None, "库中缺少该卡容量元数据"
    base = int(base)
    max_rank = int(max_rank)
    candidates = rank_candidates(mod, drain, color)
    if len(candidates) == 1:
        rank = candidates[0]
        return rank, "姿态/光环等级由提供容量反推" if base < 0 else drain_branch(mod, rank, drain)
    if not candidates:
        return None, "容量与数字颜色没有兼容等级"
    return None, f"容量仍有多个兼容等级 {candidates}，等待独立像素证据"


def rank_candidates(mod: dict, drain: Optional[int], color: Optional[str] = None) -> list[int]:
    """容量反推的**全部**可能等级（穷举 0..max_rank，不分极性三态）。

    用途：与「豆子」像素检测**交叉印证** —— 豆数必须落在候选集里才敢采信
    （`core/pips.py::pick_rank`）。`infer_rank` 是「选一个」（含满级假设与颜色
    提示），这里要的是「可能有哪些」，两者用途不同、别合并。
    姿态/光环（`base_drain < 0`）提供容量：同极性加倍，异极性80%向下取整。
    规则来源：https://www.warframe.com/en/news/mods-guide
    """
    base, max_rank = mod.get("base_drain"), mod.get("max_rank")
    if drain is None or base is None or max_rank is None:
        return []
    base, max_rank = int(base), int(max_rank)
    if base < 0:
        hint = _color_hint(color)
        capacity = abs(drain)
        return [
            r
            for r in range(max_rank + 1)
            if (
                (hint in (None, "normal") and -base + r == capacity)
                or (hint in (None, "matched") and (-base + r) * 2 == capacity)
                or (hint in (None, "mismatch") and math.floor((-base + r) * 0.8) == capacity)
            )
        ]
    out = set()
    hint = _color_hint(color)
    for r in range(max_rank + 1):
        if hint in (None, "normal") and base + r == drain:  # 白：无加成
            out.add(r)
        if (
            hint in (None, "matched") and math.ceil((base + r) / 2) == drain
        ):  # 绿：极性匹配，消耗减半
            out.add(r)
        if (
            hint in (None, "mismatch") and int((base + r) * 1.25 + 0.5) == drain
        ):  # 红：极性不合 +25%
            out.add(r)
    return sorted(out)


def effect_at(mod: dict, rank: Optional[int]) -> dict:
    """取该等级的效果；等级未知时退回满级值（调用方负责标注）。"""
    levels = mod.get("levels") or []
    if rank is not None and 0 <= rank < len(levels):
        return levels[rank] or {}
    return mod.get("effects") or {}


# ---------------------------------------------------------------------------
# 灵化 / 特殊形态：用面板值覆盖武器基础数据
# ---------------------------------------------------------------------------
_incarnon_cache: Optional[set] = None
_incarnon_forms_cache: Optional[dict] = None


def _incarnon_forms() -> dict:
    """灵化形态基础数值表（key=基础武器小写 uniqueName）。

    数据源：英文 wiki Module:Weapons/data 原型条目里的 Incarnon 攻击（生成脚本
    scripts/build_incarnon_forms.py，2026-10-26 wfsim 迁移 P2）。近战加成记录没有独立基础面板，
    在此排除，仍走原有面板反推，避免把部分能力当作另一套基础数值。
    """
    global _incarnon_forms_cache
    if _incarnon_forms_cache is None:
        try:
            loaded = json.loads((_DATA / "incarnon_forms.json").read_text(encoding="utf-8"))
            _incarnon_forms_cache = {
                key: value
                for key, value in loaded.items()
                if not isinstance(value, dict)
                or value.get("kind") not in {"melee_buff", "melee_native"}
            }
        except Exception:  # noqa: BLE001
            _incarnon_forms_cache = {}
    return _incarnon_forms_cache


def _incarnon_names() -> set:
    """有灵化形态的武器名集合（来自 rotations.json 的灵化周轮换表）。"""
    global _incarnon_cache
    if _incarnon_cache is None:
        names: set = set()
        try:
            r = json.loads((_DATA / "rotations.json").read_text(encoding="utf-8"))
            for week in (r.get("incarnon") or {}).get("weeks") or []:
                if not isinstance(week, list):
                    continue
                for it in week:
                    if isinstance(it, dict):
                        names.add(str(it.get("en") or "").lower())
                        names.add(str(it.get("cn") or ""))
        except Exception:  # noqa: BLE001
            pass
        _incarnon_cache = names
    return _incarnon_cache


_lich_keys_cache: Optional[set] = None


def _is_lich_weapon(weapon: dict) -> bool:
    """赤毒/信条/终幕武器（有 valence 回响加成的 40 把）。"""
    global _lich_keys_cache
    if _lich_keys_cache is None:
        try:
            d = json.loads((_DATA / "lich_valence.json").read_text(encoding="utf-8"))
            _lich_keys_cache = {k for k in d if k != "_meta"}
        except Exception:  # noqa: BLE001
            _lich_keys_cache = set()
    if not _lich_keys_cache:
        return False
    key = re.sub(r"[\s'’]+", "_", str(weapon.get("name") or "").lower())
    return key in _lich_keys_cache


def _panel_base_weapon(weapon: dict, panel: dict, totals: dict) -> tuple[Optional[dict], str]:
    """灵化（Incarnon）/特殊形态检测：从面板**反推**基础数据。

    升级界面的面板是「已含 MOD 的最终值」，不能直接当基础（会二次叠算），
    要把已知 MOD 加成除回去：
        基础暴击率 = 面板暴击率 / (1 + 暴率MOD)          （暴率MOD是相对加成，可逆）
        基础暴伤 / 基础触发 同理；
        基础各类型 = 面板类型值 / ((1+基伤MOD)×(1+该类型物理MOD))
    反推后与库内基础比对，差异显著（灵化形态就是这种）才采用。
    """
    if not weapon:
        return None, ""
    cc_m = 1 + float(totals.get("crit_chance") or 0.0) / 100.0
    cm_m = 1 + float(totals.get("crit_dmg") or 0.0) / 100.0
    sc_m = 1 + float(totals.get("status_chance") or 0.0) / 100.0
    bd_m = 1 + float(totals.get("base_dmg") or 0.0) / 100.0

    cc_p = to_float(panel.get("crit_chance"))
    cm_p = to_float(panel.get("crit_damage"))
    sc_p = to_float(panel.get("status_chance"))

    # 各类型基础值：行值是「a>b」时左边就是基础；单值则要除掉 MOD。
    # 两遍处理：先 IPS 三系，再元素行 —— 元素行的反推需要 IPS 基础总量
    rev = {}
    for _k, _zh in {**PHYS_ZH, **ELEM_ALL_ZH}.items():
        rev.setdefault(str(_zh), _k)
    base_dmg: dict[str, float] = {}
    elem_rows: list[tuple[str, float]] = []
    unmatched: list[tuple[str, float]] = []  # 行名认不出但有数值的行（★不许静默丢）
    notes: list[str] = []  # 反推过程的非致命事件，随 note 上卡面
    for row in panel.get("damage_rows") or []:
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            continue
        label = str(row[0]).lstrip("*").strip()
        key = next((k for zh, k in rev.items() if label.startswith(zh)), None)
        if not key:
            if label in _TOTAL_LABELS:
                continue  # 总计行由 _panel_total 专门读取，不是「认不出的行」
            _l0, _r0 = to_pair(row[1])
            _v0 = float(_r0 or _l0 or 0.0)
            if _v0 > 0:
                unmatched.append((label or "（空行名）", _v0))
            continue
        left, right = to_pair(row[1])
        if not left:
            continue
        if key in ("impact", "puncture", "slash"):
            _pm = totals.get("physical") or {}
            pm = float(_pm.get(key) or 0.0) if isinstance(_pm, dict) else 0.0
            if right:
                # 右列（MOD 后）反推基础 —— **必须用右列**：左列是 UI 的
                # 基础显示值（截断、且不含灵化进化对基础面板的平坦加成，
                # 实测拉特昂 Prime EVO II +6 基伤：左 9 实际 9.6）。
                # right/(1+基伤MOD)(1+物理MOD) = 含进化的有效基础
                base_dmg[key] = float(right) / (bd_m * (1 + pm / 100.0))
            else:
                base_dmg[key] = float(left) / (bd_m * (1 + pm / 100.0))
        elif key not in ("true", "void"):
            elem_rows.append((key, float(left)))
    lib_dmg = weapon.get("damage") or {}
    lib_total = float(lib_dmg.get("total") or 0.0) or sum(
        float(lib_dmg.get(k) or 0.0) for k in ("impact", "puncture", "slash")
    )
    # 灵化形态库提前加载（incarnon_forms.json）：元素反解的幻影防线也要看它有没有该元素
    inc = _incarnon_forms().get(str(weapon.get("uniqueName") or "").lower())
    inc_dmg = (inc or {}).get("damage") or {}
    inc_total = float(inc_dmg.get("total") or 0.0)

    # ★★ 2026-10-09 物理行串行纠正：模型偶发把相邻两行的行名与数值对错位
    #   （实测拉特昂 Prime：面板「冲击 9▸25.4 / 穿刺 72▸203.5」被读成
    #   冲击 203.5、穿刺 25.4 ⇒ 反推基础冲击 76.8，吃 G 系冲击弱点致单发虚高）。
    #   判据（与具体武器无关）：反推三系与库内/灵化库的分布**对不上**，而它的
    #   某个排列**对得上**（各行 ÷ 参照同行的比值彼此相差 ≤3%）⇒ 按该排列纠正。
    #   两者都对不上时不动（可能是真的改了构成的形态，交给后面的校验）。
    def _fits(cand: dict, ref: dict) -> bool:
        if not cand or any(float(ref.get(k) or 0.0) <= 0 for k in cand):
            return False
        _r = [cand[k] / float(ref[k]) for k in cand]
        return max(_r) - min(_r) <= 0.03 * max(_r)

    _phys_read = {k: base_dmg[k] for k in PHYS_ZH if k in base_dmg}
    _refs = [d for d in (lib_dmg, inc_dmg) if any(float(d.get(k) or 0.0) > 0 for k in PHYS_ZH)]
    if (
        len(_phys_read) >= 2
        and len(set(round(v, 3) for v in _phys_read.values())) >= 2
        and _refs
        and not any(_fits(_phys_read, r) for r in _refs)
    ):
        _ks = list(_phys_read)
        for _perm in itertools.permutations(_phys_read.values()):
            _cand = dict(zip(_ks, _perm))
            if _cand != _phys_read and any(_fits(_cand, r) for r in _refs):
                _moved = [k for k in _ks if _cand[k] != _phys_read[k]]
                base_dmg.update(_cand)
                notes.append(
                    "面板"
                    + "/".join(PHYS_ZH[k] for k in _moved)
                    + "行的数值疑似串行（按库内伤害构成对调后吻合），已纠正"
                )
                break

    # ★★ 2026-10-05 锚定补全（库内有基础时只反推一个缩放锚）：
    #   IPS 三系里「库内有值、反推却缺失」的类型 —— 典型：面板行名被遮挡
    #   （实测执法者穿刺行被滚动箭头盖住 → 整行静默丢 → 本地推算少 102.3、
    #   毒素行反解出幻影自带毒 29）。用「已反推行 ÷ 库内同行」的中位锚补全：
    #   锚 = 基伤 MOD × 灵化进化系数（全类型一致），各行锚应自洽。
    #   执法者实测两锚同为 310/210 ≈ 1.476，补出穿刺 46.5 → 面板 102.3 逐行吻合。
    if base_dmg:
        _lib_phys = {k: float(lib_dmg.get(k) or 0.0) for k in PHYS_ZH}
        _missing = [k for k in PHYS_ZH if k not in base_dmg and _lib_phys.get(k, 0.0) > 0]
        _anchors = sorted(
            base_dmg[k] / _lib_phys[k]
            for k in base_dmg
            if k in PHYS_ZH and _lib_phys.get(k, 0.0) > 0
        )
        if _missing and _anchors:
            _scale = _anchors[len(_anchors) // 2]
            _lo, _hi = _anchors[0], _anchors[-1]
            if _hi - _lo > 0.02 * max(_hi, _lo):
                notes.append(f"各行反推锚不一致（{_lo:.3f}~{_hi:.3f}），按中位锚 {_scale:.3f} 补全")
            for k in _missing:
                base_dmg[k] = _lib_phys[k] * _scale
            notes.append(
                "面板"
                + "、".join(PHYS_ZH[k] for k in _missing)
                + "行未读到（行名被遮挡或漏读），已按库内比例补全 "
                + "、".join(f"{PHYS_ZH[k]} {_lib_phys[k] * _scale:.1f}" for k in _missing)
            )
    if unmatched:
        notes.append(
            "面板行 "
            + "、".join(f"「{lb}」{v:g}" for lb, v in unmatched)
            + " 行名未能归属类型（图标遮挡？），已按库内分布对账"
        )
    if base_dmg:
        ips_total = sum(v for k, v in base_dmg.items() if k in ("impact", "puncture", "slash"))
        # 总伤锚定：某行值≈面板总伤（可能是被模型挂错行名的「总计」）时，
        # 不许它进自带元素反推 —— 否则基础总伤直接翻倍（实测踩过）
        pan_total = _panel_total(panel)
        # 元素行 → 自带元素基础（灵化形态可能改自带元素 —— ⚠ 2026-10-05 实证：
        #   执法者「自带毒 29」其实是穿刺行被遮挡造成的**幻影**，执法者并无
        #   自带毒素；判定与防线见下）。
        # 我们引擎的语义：元素 MOD 加成 = 元素%×MOD后总基础（含自带元素），即
        #   行值 = after_base×(E0 + pct×(IPS总+E0))
        # → E0 = (行值/after_base − pct×IPS总) / (1+pct)
        # 行值 ≤ 纯 MOD 贡献时视为没有自带元素（防 OCR 噪声算出负数）
        composed = dc.compose_elements(dict(totals.get("elements") or {}))
        for key, row_v in elem_rows:
            if pan_total and abs(row_v - pan_total) / pan_total < 0.02:
                # 这是「总计」行（行名被模型挂错了），跳过 —— 上卡面说明：
                # 该元素改按 MOD 推算，别让用户以为面板那一行被采信了
                notes.append(
                    f"面板「{ELEM_ALL_ZH.get(key, key)}」行读数 {row_v:g} ≈ 总计，"
                    "疑似误抄总计，该元素按 MOD 推算"
                )
                continue
            pct = float(composed.get(key) or 0.0) / 100.0
            e0 = (row_v / bd_m - pct * ips_total) / (1 + pct)
            # 幻影自带元素防线：游戏面板行值与「纯 MOD 解释值」有未建模的
            # 小偏差（实测拉特昂 Prime 面板各行有 ~+6.7% 的显示系数），
            # 逆解后会得到占总量百分之几的幽灵自带元素 → 基础总伤虚高、
            # 总伤校验 ⚠。MOD 已能解释行值 90% 以上时视为无自带元素。
            explained = bd_m * pct * ips_total  # 纯 MOD 贡献
            if pct > 0 and row_v - explained < 0.10 * row_v:
                continue
            # 疑似总计行兜底②：无该元素 MOD、库内基础也没有它、而反推出的
            # 自带值却 ≥ IPS 总量的 2 倍 —— 真实自带元素极少这么大，大概率是
            # 「总计」被挂错了行名（实测踩过）
            if (
                pct == 0.0
                and not float(lib_dmg.get(key) or 0.0)
                and ips_total > 0
                and e0 >= 2.0 * ips_total
            ):
                continue
            # ★ 幻影防线③（2026-10-05）：库内基础与灵化形态**都没有**该元素时，
            #   反解出的「自带元素」必是缺行/噪声的假象 —— 实测执法者：穿刺行
            #   被遮挡 → IPS 少 46.5 → 反解出幻影自带毒 29（真值 0，毒 100% 来自
            #   热病打击 Prime；上面的锚定补全 + 本防线双保险）。
            if e0 > 0 and not float(lib_dmg.get(key) or 0.0) and not float(inc_dmg.get(key) or 0.0):
                notes.append(
                    f"反解出幻影自带{ELEM_ALL_ZH.get(key, key)} {e0:g}"
                    "（库内/灵化库均无此自带元素）→ 判 0"
                )
                continue
            if e0 > 0:
                base_dmg[key] = e0
    # 灵化形态数值（有就是权威判定 + 更准的兜底向量）—— 已提前加载（见上）

    if not base_dmg:
        # 兜底：类型行没读到（模型偶发 damage_rows 为空）但总计有值
        # → 用总计除掉基伤 MOD 反推总量，按参照向量的比例摊回三系；
        #   参照向量在「库内基础 / 灵化形态基础」里选与估计总量更接近的那个
        #   （灵化形态可能改变 IPS 构成，如布莱顿 24→50 但构成完全不同）
        #   ⚠️ total 行常是单一数字（"总计 132.5"）→ to_pair 只返回左值，要接住
        tot_right = _panel_total(panel)
        if tot_right and (lib_total > 0 or inc_total > 0):
            est = tot_right / bd_m
            ref_dmg, ref_total = lib_dmg, lib_total
            if inc_total > 0 and (not lib_total or abs(est - inc_total) < abs(est - lib_total)):
                ref_dmg, ref_total = inc_dmg, inc_total
            base_dmg = {
                k: float(ref_dmg.get(k) or 0.0) * est / ref_total
                for k in ("impact", "puncture", "slash")
                if float(ref_dmg.get(k) or 0.0) > 0
            }
    if not base_dmg:
        return None, ""

    # ---- 反推值校验与取整 ----
    # 游戏面板的基础数值只有整数或一位小数（如 9.6/76.8/9.6）；反推除法
    # 会留下浮点残渣（9.58491…），按「先四舍五入到一位小数、接近整数则
    # 吸为整数」清洗，再把总计校正为 Σ成分 —— 卡面不再出现三位小数。
    def _snap(v: float) -> float:
        r1 = round(v, 1)
        return float(round(r1)) if abs(r1 - round(r1)) < 0.05 else r1

    base_dmg = {k: _snap(v) for k, v in base_dmg.items()}
    base_total = _snap(sum(base_dmg.values()))
    # ★ 2026-10-05 灵化对账（修复④）：反推总量高于库内的差额若与 evolutions.json
    #   的 flat_base_damage 吻合（实测执法者 310−210＝100），明说「灵化 +100 已计入」，
    #   不再让用户对着 47.6% 的差值猜。键 = 英文名小写下划线（Magistar → magistar）。
    if base_total > lib_total > 0:
        _evo_key = str(weapon.get("name") or "").strip().lower().replace(" ", "_")
        _evo_tiers = (dc._load_evo_data()[0].get(_evo_key) or {}).get("tiers") or {}
        _flat = 0.0
        for _opts in _evo_tiers.values():
            _vals = [
                float(_e.get("value") or 0.0)
                for _o in _opts
                for _e in (_o.get("effects") or [])
                if _e.get("kind") == "flat_base_damage"
            ]
            if _vals:
                _flat += max(_vals)  # choose_one：取该档最高（执法者两选项同为 +100）
        if _flat > 0 and abs((base_total - lib_total) - _flat) <= 0.05 * max(lib_total, 1.0):
            notes.append(
                f"反推基础 {base_total:g} ＝ 库内 {lib_total:g} ＋ 灵化进化 +{_flat:g}"
                "（与 evolutions 数据吻合，已计入）"
            )
    diffs = [abs(base_total - lib_total) / max(lib_total, 1.0)]
    if cc_p is not None:
        # ⚠️ 面板是百分数、库里是小数：反推后先 /100 再比，否则必然误触发覆盖
        diffs.append(
            abs(cc_p / cc_m / 100.0 - float(weapon.get("criticalChance") or 0.0))
            / max(float(weapon.get("criticalChance") or 0.0), 0.01)
        )
    if cm_p is not None:
        diffs.append(
            abs(cm_p / cm_m - float(weapon.get("criticalMultiplier") or 1.0))
            / max(float(weapon.get("criticalMultiplier") or 1.0), 0.01)
        )
    if max(diffs, default=0.0) <= 0.20:
        return None, ""  # 与库内基础一致（未灵化），不需要覆盖

    # ---- 灵化库命中：面板反推值与灵化基础吻合 → 直接采用灵化基础 ----
    # （基础是真实库数据，MOD 正常叠算，面板校验也保持开启；
    #   反推值里吸收的灵化进化 perk 平坦加成会导致小偏差，20% 容差内即可）
    _extra = "；".join(notes)

    if inc_total > 0:
        diffs_inc = [abs(base_total - inc_total) / max(inc_total, 1.0)]
        if cc_p is not None:
            diffs_inc.append(
                abs(cc_p / cc_m / 100.0 - float(inc.get("criticalChance") or 0.0))
                / max(float(inc.get("criticalChance") or 0.0), 0.01)
            )
        if cm_p is not None:
            diffs_inc.append(
                abs(cm_p / cm_m - float(inc.get("criticalMultiplier") or 1.0))
                / max(float(inc.get("criticalMultiplier") or 1.0), 0.01)
            )
        if max(diffs_inc, default=0.0) <= 0.20:
            w2 = dict(weapon)
            w2["damage"] = {
                **{k: v for k, v in inc_dmg.items() if k != "total"},
                "total": inc_total,
            }
            w2["criticalChance"] = float(inc.get("criticalChance") or 0.0)
            w2["criticalMultiplier"] = float(inc.get("criticalMultiplier") or 1.0)
            w2["procChance"] = float(inc.get("procChance") or 0.0)
            if float(inc.get("fireRate") or 0.0) > 0:
                w2["fireRate"] = float(inc["fireRate"])
            dc.set_form_cadence(w2, inc)  # 点射 / 蓄力随灵化形态（b6 批）
            return w2, (
                "灵化形态：基础数值取自灵化库（英文 wiki），MOD 正常叠算，面板校验保持开启"
                + ("；" + _extra if _extra else "")
            )

    w2 = dict(weapon)
    w2["damage"] = {**base_dmg, "total": base_total}
    # 基础取自截图面板（只是面板当前那一种攻击）→ 伤害卡不再按库内 attacks
    # 标「本表基础伤害含多段合并」（键名与 damage_calc.card_lines 约定）
    w2["_panel_derived"] = True
    if cc_p is not None:
        w2["criticalChance"] = round(cc_p / cc_m / 100.0, 4)  # 面板百分数→库小数
    if cm_p is not None:
        w2["criticalMultiplier"] = round(cm_p / cm_m, 3)
    if sc_p is not None:
        w2["procChance"] = round(sc_p / sc_m / 100.0, 4)
    else:
        # 面板没读到触发率时，用灵化库的值兜底（比基础形态准得多）
        if inc_total > 0 and (inc or {}).get("procChance") is not None:
            w2["procChance"] = float(inc["procChance"])
    fr = to_float(panel.get("attack_speed"))
    if fr is not None:
        w2["fireRate"] = fr
        # 标记「射速取自面板终值」→ to_damage_spec 据此**不再**叠射速 MOD（防二次加成）
        w2[FIRE_RATE_FROM_PANEL] = True
    names = _incarnon_names()
    is_inc = (
        inc_total > 0
        or (weapon.get("name") or "").lower() in names
        or (weapon.get("zh") or "") in names
    )
    is_lich = _is_lich_weapon(weapon)
    if is_lich:
        # 赤毒/信条/终幕：回响加成 25-60% 连续随机、只能从面板反推，
        # 反推基础天然已含 —— 标注提醒实际值以游戏内为准
        return w2, (
            "灵化/回响武器：基础数据从面板反推（含回响加成与进化加成，"
            "回响为 25-60% 随机值，实际以游戏内为准），"
            "MOD 按实际等级正常叠算一次" + ("；" + _extra if _extra else "")
        )
    # 物理构成与库内（普通形态）同比例、且没有反推出自带元素 ⇒ 这是普通形态
    # 带灵化进化的面板（进化只平坦抬基伤/暴击），不是灵化形态 —— 别标错
    _phys_now = {k: base_dmg[k] for k in PHYS_ZH if base_dmg.get(k)}
    _evo_only = (
        is_inc
        and _fits(_phys_now, lib_dmg)
        and not any(k not in PHYS_ZH and v > 0 for k, v in base_dmg.items())
    )
    if _evo_only:
        _base_note = "普通形态（含灵化进化加成）：基础数据从面板反推，MOD 按实际等级正常叠算一次"
    elif is_inc:
        _base_note = (
            "灵化形态：基础数据从面板反推（含自带元素与灵化进化加成），MOD 按实际等级正常叠算一次"
        )
    else:
        _base_note = "特殊形态：基础数据从面板反推，MOD 按实际等级正常叠算一次"
    if is_inc and not _evo_only and inc:
        dc.set_form_cadence(w2, inc)  # 面板是灵化形态 ⇒ 点射 / 蓄力随灵化（b6 批）
    return w2, _base_note + ("；" + _extra if _extra else "")


# 武器 dict 上的私有标记键：fireRate 已被面板「攻速」行（终值）覆盖
FIRE_RATE_FROM_PANEL = "_fire_rate_from_panel"
# 面板上「基础 ▸ MOD 后」两列的标量行（analyze 入口统一归一为右列）
_PANEL_SCALARS = ("attack_speed", "crit_chance", "crit_damage", "status_chance")


# ---------------------------------------------------------------------------
# 主分析
# ---------------------------------------------------------------------------
_ADD_FIELDS = (
    "base_dmg",
    "multishot",
    "crit_chance",
    "crit_dmg",
    "fire_rate",
    "status_chance",
    "status_dmg",
    "heavy_dmg",
    "faction_dmg",
    "headshot_bonus",
    "initial_combo",
    "punch_through",
    "crit_chance_heavy",
    "throw_dmg",
    # v1.9 新机制（漏了就会像「急进猛突 rank3」那样什么都不显示）
    "crit_per_combo",
    "status_per_combo",
    "dmg_per_status",
)


def drain_branch(rec: dict, rank: Optional[int], drain: Optional[int]) -> str:
    """由 (基础容量, 等级, 卡面容量) 反查**极性分支**，返回可读说明。

    规则（wiki /w/Polarity，与 ``infer_rank`` 同一套）：
      · 无加成     ``drain = base + rank``
      · 匹配减半   ``drain = ceil((base + rank) / 2)``   —— 卡面数字显示绿色
      · 极性不合   ``drain = round((base + rank) * 1.25)`` —— 卡面数字显示红色

    ★ 为什么由**算术反查**而不是采信模型读的颜色（2026-09-20 玩家报障）：
      实测模型颜色读数不可靠（同一张「北风」实际是**红色 9**，模型报「白」；
      「肢解」实际绿 5 模型报白；「牺牲斩铁」实际白 13 模型报绿）。
      而等级一旦由豆子确定，(base, rank, drain) 三个数就能**唯一**定出分支 ——
      用户要的「容量要求增加 25% 没有说明」正是这么补上的。
    返回空串表示无加成（无需说明）。
    """
    base, r = rec.get("base_drain"), rank
    if base is None or r is None or drain is None:
        return ""
    base, r = int(base), int(r)
    if base < 0:
        return ""
    plain = base + r
    if drain == plain:
        return ""
    if drain == math.ceil(plain / 2):
        return "槽位极性匹配（容量减半）"
    if drain == int(plain * 1.25 + 0.5):
        return "极性不合（容量 +25%）"
    return ""


def _zero_row_anchored(pos, rows, pixel):
    """全零行不等于空槽；只有独立卡底/闭合框几何能确认该映射。"""
    if pos is None:
        return True
    eq = [r for r in rows or [] if not r.get("is_inventory")]
    ri, ci = pos
    if ri >= len(eq) or any(eq[ri].get("counts") or []):
        return True
    row = eq[ri]
    size, grid, band = row.get("image_size"), row.get("grid"), row.get("band")
    if not size or not grid or not band or ci >= len(grid):
        return False
    width, height = size
    x, y = grid[ci] / width, sum(band) / 2 / height
    tolerance = max(3, row.get("card_w", 0) * 0.2) / height
    for slot in (pixel or {}).get("slots", []):
        if slot.get("zone") != "equipment_candidate":
            continue
        box = slot.get("line_box") or slot.get("box")
        if not box or not box[0] <= x <= box[2]:
            continue
        bottom = (box[1] + box[3]) / 2 if slot.get("line_box") else box[3]
        if abs(bottom - y) <= tolerance:
            return True
    return False


def _pips_rank(
    pos,
    pips_rows: Optional[list],
    candidates: Optional[list] = None,
    max_rank: Optional[int] = None,
) -> tuple[Optional[int], str]:
    """按**已对齐的位置**取豆数（`pos` 由 `align_rows` 给出）。

    ★ 刻意不从模型报的 `row`/`col` 直接取 —— 端到端实测（2026-09-20）
      完整提示词下模型的 row/col 很不可靠（暮斩那张：北风/长时苦难/肢解
      三张全报错，取到了别行的豆数）。位置改由「卡顺序 + 容量候选集约束」
      整体对齐得出（`core.pips.align_rows`），只有全部卡都能自洽匹配时才采用。
    """
    if not pips_rows or pos is None:
        return None, ""
    r_i, c_i = pos
    eq = [r for r in pips_rows if not r.get("is_inventory")]
    if not (0 <= r_i < len(eq)):
        return None, ""
    row = eq[r_i]
    counts = row.get("counts") or []
    if not (0 <= c_i < len(counts)):
        return None, ""
    # ★ 候选集校验（2026-09-20 4K 报障后加）：该格豆数**不在**容量反推出来的
    #   候选集里时，它就是**检测噪声**（实测 4K 下金框卡「结霜侵蚀」被暖色兜底
    #   误数成 2 颗，真值 0），照抄会把等级改错 —— 直接拒收，让上层退回
    #   容量反推 + 标 `?`，并把原因写出来（不再静默）。
    n_here = counts[c_i]
    full = row.get("maxed") or []
    if (
        max_rank is not None
        and c_i < len(full)
        and full[c_i]
        and (not candidates or int(max_rank) in candidates)
    ):
        return int(max_rank), "满级线"
    if max_rank is not None and not 0 <= n_here <= int(max_rank):
        return None, "豆数超过该卡等级上限"
    if not candidates:
        return n_here, "本列豆数（独立几何定位）"
    if candidates and n_here not in set(candidates):
        return None, f"豆子 {n_here} 颗与容量候选 {sorted(set(candidates))} 不符（已忽略该格）"
    return pips_engine.pick_rank(
        counts, c_i + 1, candidates or [], maxed=row.get("maxed") or [], max_rank=max_rank
    )


_RIVEN_SUFFIX_RE = re.compile(r"[A-Za-z]{3,}-[A-Za-z]{3,}")


def is_riven_name(name) -> bool:
    """是不是**裂罅紫卡**的名字。

    紫卡名字 = 「武器名 + 随机词缀」，例如「野猪 Visi-satidex」「奏凯 Acri-cronitis」
    —— 词缀永远长成 ``Xxxx-yyyyy``（两段 ASCII 字母 + 连字符），
    所以静态词典**永远匹配不上**，必须单独归类而不是报「库中未收录」。

    ★ 判据收紧到「**矛盾式**」级别，避免把普通外文 mod 名误判成紫卡：
      ① 名字里直接出现「裂罅 / 紫卡 / Riven」；或
      ② 至少两个空格分隔的词，且**最后一个词**整体形如 ``Abcd-efgh``。
      （普通 mod 名如 "Amalgam Argonak Metal Auger" 不含连字符矮词；
        而这些名字本来就该被 `match_mod` 命中，走不到这里。）
    """
    t = str(name or "").strip()
    if not t:
        return False
    if any(k in t for k in ("裂罅", "紫卡", "Riven", "riven")):
        return True
    parts = t.split()
    if len(parts) < 2:
        return False
    return bool(_RIVEN_SUFFIX_RE.fullmatch(parts[-1]))


def _pips_count_at(pos, pips_rows: Optional[list]) -> Optional[int]:
    """按对齐位置取该格的**原始豆数**（不经过候选集/通配推导）。

    给紫卡这类**库中无基准**的卡用：它们没有容量候选集，豆数是唯一的等级来源。
    """
    if not pips_rows or pos is None:
        return None
    r_i, c_i = pos
    eq = [r for r in pips_rows if not r.get("is_inventory")]
    if not (0 <= r_i < len(eq)):
        return None
    counts = eq[r_i].get("counts") or []
    if not (0 <= c_i < len(counts)):
        return None
    return counts[c_i]


def _panel_structure_reasons(panel) -> list[dict[str, str]]:
    """只检查原始 OCR 字段的结构与量纲，不用静态武器数值裁决灵化或 buff。"""
    reasons: list[dict[str, str]] = []
    if panel is None:
        return reasons
    if not isinstance(panel, dict):
        return [{"code": "panel_structure", "message": "面板不是字段对象"}]
    reasons.extend(_panel_conflict_reasons(panel))

    def inspect(value, label: str, forbidden: str) -> None:
        if isinstance(value, dict):
            parts = list(value.values())
        elif isinstance(value, (list, tuple)):
            parts = list(value)
        else:
            parts = [value]
        nested = any(isinstance(v, (dict, list, tuple)) for v in parts)
        text = " ".join(str(v) for v in parts if v is not None)
        # 千分位不能算成多个读数；字段可以包含标签和 null 占位。
        readings = re.findall(r"-?\d+(?:,\d{3})*(?:\.\d+)?", text)
        wrong_unit = bool(re.search(forbidden, text))
        if nested or len(readings) > 2 or wrong_unit:
            detail = "量纲不符" if wrong_unit else "包含多个属性或嵌套结构"
            reasons.append({"code": "panel_structure", "message": f"{label}字段{detail}"})

    multiplier = r"倍|(?:[xX×]\s*\d)|(?:\d\s*[xX×])"
    scalar_units = {
        "total": ("总伤害", rf"%|％|{multiplier}"),
        "crit_chance": ("暴击几率", multiplier),
        "status_chance": ("触发几率", multiplier),
        "crit_damage": ("暴击伤害", r"%|％"),
        "attack_speed": ("攻击速度", rf"%|％|{multiplier}"),
    }
    damage_labels = tuple({**PHYS_ZH, **ELEM_ALL_ZH}.values())
    for field, value in _panel_values(panel):
        if field in scalar_units:
            label, forbidden = scalar_units[field]
            inspect(value, label, forbidden)
        elif field.startswith(damage_labels):
            inspect(value, field, rf"%|％|{multiplier}")
    return reasons


def remap_mod_geometry(observed: dict, transform: Optional[dict]) -> dict:
    """把分区坐标逆映射回原图；原始坐标和变换另外保留，不猜未知裁剪。"""
    result = copy.deepcopy(observed)
    bounds = (transform or {}).get("bounds")
    valid = (
        isinstance(bounds, (list, tuple))
        and len(bounds) == 4
        and all(isinstance(n, (float, int)) and math.isfinite(n) for n in bounds)
        and 0 <= bounds[0] < bounds[2] <= 1
        and 0 <= bounds[1] < bounds[3] <= 1
    )

    def remap(g):
        return {
            **g,
            "x": 1000 * bounds[0] + g["x"] * (bounds[2] - bounds[0]),
            "y": 1000 * bounds[1] + g["y"] * (bounds[3] - bounds[1]),
            "w": g["w"] * (bounds[2] - bounds[0]),
            "h": g["h"] * (bounds[3] - bounds[1]),
        }

    for item in (result.get("mods") or []) + (result.get("arcanes") or []):
        if not isinstance(item, dict) or not isinstance(item.get("_geometry"), dict):
            continue
        item["_geometry_observed"] = copy.deepcopy(item["_geometry"])
        item["_geometry_transform"] = copy.deepcopy(transform)
        geo_valid = all(
            isinstance(item["_geometry"].get(key), (int, float))
            and not isinstance(item["_geometry"].get(key), bool)
            and math.isfinite(item["_geometry"][key])
            for key in ("x", "y", "w", "h")
        )
        if not valid or not geo_valid:
            item["_geometry_view"] = "unmapped_crop"
            continue
        item["_geometry"] = remap(item["_geometry"])
        item["_geometry_view"] = "full_image"
        fields = item.get("_field_geometry")
        if fields is None:
            continue
        item["_field_geometry_observed"] = copy.deepcopy(fields)
        if not isinstance(fields, dict):
            item["_geometry_field_errors"] = ["invalid_field_geometry"]
            continue
        errors = []

        def field_remap(value, label):
            if not isinstance(value, dict) or any(
                not isinstance(value.get(k), (int, float))
                or isinstance(value.get(k), bool)
                or not math.isfinite(value[k])
                for k in ("x", "y", "w", "h")
            ):
                errors.append(label)
                return value
            return remap(value)

        if fields.get("name"):
            fields["name"] = field_remap(fields["name"], "name")
        drains = fields.get("drain", [])
        if isinstance(drains, list):
            fields["drain"] = [field_remap(g, "drain") for g in drains]
        else:
            errors.append("drain")
        if errors:
            item["_geometry_field_errors"] = errors
    return result


def analyze_layout(ocr: dict, pips_rows: Optional[list] = None) -> dict:
    """坐标观察与像素候选一对一绑定；候选外框及全局覆盖未知不能升级为许可。"""
    pixel = ocr.get("_pixel_layout") or {}
    if not isinstance(pixel, dict) or pixel.get("source") != "pixel_geometry":
        pixel = {}

    def valid_box(box):
        return (
            isinstance(box, (list, tuple))
            and len(box) == 4
            and all(
                isinstance(n, (int, float)) and not isinstance(n, bool) and math.isfinite(n)
                for n in box
            )
            and 0 <= box[0] < box[2] <= 1
            and 0 <= box[1] < box[3] <= 1
        )

    slots = [
        copy.deepcopy(slot)
        for slot in list(pixel.get("slots", [])) + list(ocr.get("_tile_slots") or [])
        if isinstance(slot, dict)
        and slot.get("id")
        and (
            slot.get("kind") in {"regular_candidate", "special_candidate"}
            or (
                slot.get("zone") == "inventory_candidate"
                and slot.get("extent") == "estimated_from_rank_line"
            )
        )
        and valid_box(slot.get("box"))
    ]
    result = {
        "content_read": bool(ocr.get("mods") or ocr.get("panel") or ocr.get("weapon")),
        "layout_complete": False,
        "slot_binding_complete": False,
        "coverage": copy.deepcopy(pixel.get("coverage")) or {"status": "unknown"},
        "slots": slots,
        "bindings": [],
        "unread_slots": [],
        "reasons": [],
        "pixel_evidence": copy.deepcopy(pixel),
        "coordinate_evidence": copy.deepcopy(ocr.get("coordinate_ocr")) or {},
        "pixel_rank_positions": {},
    }
    for index, raw in enumerate(ocr.get("mods") or []):
        binding = {
            "mod_index": index,
            "raw_name": raw.get("name") if isinstance(raw, dict) else None,
            "status": "unresolved",
            "slot_id": None,
            "reason": "no_image_geometry",
        }
        result["bindings"].append(binding)
        if not isinstance(raw, dict):
            continue
        geometry = raw.get("_geometry")
        if not isinstance(geometry, dict) or raw.get("_geometry_source") not in {
            "coordinate_ocr",
            "structured_box",
            "pixel_tile",
        }:
            continue
        if raw.get("_geometry_view") != "full_image":
            binding["reason"] = "unmapped_crop"
            continue
        if raw.get("_geometry_field_errors"):
            binding["reason"] = "field_geometry_invalid"
            continue
        try:
            x, y = float(geometry["x"]) / 1000, float(geometry["y"]) / 1000
        except (KeyError, ValueError, TypeError):
            continue
        if not math.isfinite(x) or not math.isfinite(y) or not (0 <= x <= 1 and 0 <= y <= 1):
            continue
        binding["observed_geometry"] = copy.deepcopy(geometry)
        binding["field_geometry"] = copy.deepcopy(raw.get("_field_geometry")) or {}
        hits = [
            slot
            for slot in slots
            if slot.get("zone") in {"equipment_candidate", "inventory_candidate"}
            and slot["box"][0] <= x <= slot["box"][2]
            and slot["box"][1] <= y <= slot["box"][3]
        ]
        if len(hits) != 1:
            binding["reason"] = "overlapping_slot_candidates" if hits else "no_observed_slot"
            continue
        slot = hits[0]
        if raw.get("_geometry_source") == "structured_box":
            drains = raw.get("_field_geometry", {}).get("drain", [])
            if any(
                not (
                    slot["box"][0] <= d["x"] / 1000 <= slot["box"][2]
                    and slot["box"][1] <= d["y"] / 1000 <= slot["box"][3]
                )
                for d in drains
            ):
                binding["reason"] = "name_drain_slots_disagree"
                continue
        binding.update(
            slot_id=slot["id"],
            kind=slot["kind"],
            zone=slot["zone"],
            status="inventory" if slot["zone"] == "inventory_candidate" else "bound_candidate",
            reason="unique_geometric_candidate",
            extent=slot.get("extent", "observed_edges"),
        )
    for binding in result["bindings"]:
        if (
            binding["slot_id"]
            and sum(b["slot_id"] == binding["slot_id"] for b in result["bindings"]) > 1
        ):
            binding.update(status="conflict", reason="multiple_mods_in_one_slot")
    for binding in result["bindings"]:
        raw = (ocr.get("mods") or [])[binding["mod_index"]]
        tile_id = raw.get("_tile_slot_id") if isinstance(raw, dict) else None
        tiles = [s for s in ocr.get("_tile_slots") or [] if s.get("id") == tile_id]
        if (
            isinstance(raw, dict)
            and raw.get("_geometry_source") == "pixel_tile"
            and len(tiles) == 1
        ):
            tile = tiles[0]
            binding.update(
                slot_id=tile_id,
                kind=tile["kind"],
                zone=tile["zone"],
                status="bound_candidate",
                reason="isolated_pixel_tile",
                extent=tile["extent"],
            )
            pos = tile.get("pips_position")
            if isinstance(pos, list) and len(pos) == 2:
                result["pixel_rank_positions"][binding["mod_index"]] = tuple(pos)
    occupied = {b["slot_id"] for b in result["bindings"] if b["status"] == "bound_candidate"}
    result["unread_slots"] = [
        s["id"]
        for s in slots
        if s.get("zone") == "equipment_candidate"
        and s.get("content_state") == "rank_marks_observed"
        and s["id"] not in occupied
    ]
    # 两路共同卡身份完整、其它槽位已有独立定位时，剩下一卡/一槽可由排除法绑定。
    # 多个未知、重复身份、库存或冲突均不推断；这不会把估计边界升级为完整布局。
    unbound = [b for b in result["bindings"] if b["status"] != "bound_candidate"]
    records = [
        match_mod(str(m.get("name") or ""))[0] if isinstance(m, dict) else None
        for m in ocr.get("mods") or []
    ]
    if (
        (
            ocr.get("_equipment_merge", {}).get("complete_shared_identity")
            or ocr.get("_equipment_merge", {}).get("shared_identity_preserved")
        )
        and len(occupied) >= 3
        and len(unbound) == len(result["unread_slots"]) == 1
        and unbound[0]["reason"] == "no_image_geometry"
        and all(records)
        and len({r["name"] for r in records}) == len(records)
    ):
        slot = next(s for s in slots if s["id"] == result["unread_slots"][0])
        unbound[0].update(
            slot_id=slot["id"],
            kind=slot["kind"],
            zone=slot["zone"],
            status="bound_candidate",
            reason="unique_remaining_equipment_slot",
            extent=slot.get("extent", "observed_edges"),
        )
        result["unread_slots"] = []
    result["slot_binding_complete"] = (
        bool(result["bindings"])
        and all(b["status"] == "bound_candidate" for b in result["bindings"])
        and not result["unread_slots"]
    )
    if any(b["status"] == "inventory" for b in result["bindings"]):
        result["reasons"].append(
            {
                "code": "inventory_in_loadout",
                "message": "识别清单有卡片定位到仓库候选区，未作为完整配卡采信",
            }
        )
    if not result["slot_binding_complete"]:
        result["reasons"].append(
            {"code": "slot_binding_unknown", "message": "部分卡片未能与原图槽位唯一对应"}
        )
    if result["unread_slots"]:
        result["reasons"].append(
            {"code": "visible_slot_unread", "message": "像素观测到的装备候选槽仍有未对应卡片"}
        )
    coverage = result["coverage"]
    # 估计外框不是全局覆盖证明；当前像素检测器明确返回unknown。
    result["layout_complete"] = (
        result["slot_binding_complete"]
        and coverage.get("status") == "complete"
        and coverage.get("equipment") == "complete"
        and coverage.get("special_slots") == "complete"
        and all(s.get("extent", "observed_edges") == "observed_edges" for s in slots)
    )
    if not result["layout_complete"]:
        result["reasons"].append(
            {"code": "layout_coverage_unknown", "message": "装备区、空槽及特殊槽完整边界尚未核实"}
        )
    eq_rows = [r for r in pips_rows or [] if not r.get("is_inventory")]
    # 零级/低级卡没有横贯亮线；名称文字仍能与相邻卡共同确定的行列网格定位。
    for binding in result["bindings"]:
        geometry = binding.get("observed_geometry")
        if not geometry or binding.get("status") in {"inventory", "conflict"}:
            continue
        raw = (ocr.get("mods") or [])[binding["mod_index"]]
        if (
            raw.get("_geometry_source") == "structured_box"
            and binding["status"] != "bound_candidate"
        ):
            continue
        if raw.get("_geometry_source") == "pixel_tile":
            continue
        matches = []
        for ri, row in enumerate(eq_rows):
            size, band, grid = row.get("image_size"), row.get("band"), row.get("grid")
            if not size or not band or not grid:
                continue
            width, height = size
            dy = sum(band) / 2 / height - geometry["y"] / 1000
            if not 0 < dy < max(0.035, geometry.get("h", 0) / 1000 * 2.5):
                continue
            hits = [
                ci
                for ci, x in enumerate(grid)
                if abs(x / width - geometry["x"] / 1000) < row.get("card_w", 0) / width * 0.55
            ]
            if len(hits) == 1:
                matches.append((ri, hits[0]))
        if len(matches) == 1:
            result["pixel_rank_positions"][binding["mod_index"]] = matches[0]
            binding["rank_position_source"] = "text_and_pixel_grid"
    for binding in result["bindings"]:
        if binding["status"] != "bound_candidate":
            continue
        slot = next(s for s in slots if s["id"] == binding["slot_id"])
        anchor = slot.get("line_box")
        if not anchor:
            continue
        center, bottom = (anchor[0] + anchor[2]) / 2, (anchor[1] + anchor[3]) / 2
        candidates = []
        for ri, row in enumerate(eq_rows):
            size = row.get("image_size")
            if not size or len(size) != 2 or min(size) <= 0 or not row.get("band"):
                continue
            width, height = size
            cw = row.get("card_w", 0)
            if abs(sum(row["band"]) / 2 / height - bottom) > max(3 / height, cw * 0.2 / height):
                continue
            for ci, cx in enumerate(row.get("grid") or []):
                if abs(cx / width - center) <= cw * 0.35 / width:
                    candidates.append((ri, ci))
        if len(candidates) == 1:
            result["pixel_rank_positions"][binding["mod_index"]] = candidates[0]
    return result


def analyze(ocr: dict, pips_rows: Optional[list] = None) -> dict:
    """把视觉识别结果转成结构化分析（不依赖 AstrBot，可离线测试）。

    `pips_rows`（可选）= `core.pips.detect_pips()` 的结果，即**豆子**（卡片底部
    菱形等级刻度）的像素检测值。它作为**第二独立信号**参与定级：

        rank = 容量数字反推  ←→  亮豆数

    两条路径一致才敢改；豆数落在 `rank_candidates()` 里（即与容量解吻合）
    才采信。为什么需要它：容量反推常有多解（私法补给 容量 5 → 1白/5绿/0红），
    旧策略「取最高」会把 0 级卡判成满级；豆子能唯一定出答案，而且它与容量
    反推**互相独立**（一个读数字、一个数像素），一致性本身就是强证据。
    """
    # 先冻结入口证据；后续归一化、匹配及反推不能覆盖模型/OCR 所给的字段。
    provenance = {
        "input": copy.deepcopy(
            {key: ocr.get(key) for key in ("weapon", "capacity", "mods", "panel", "arcanes")}
        ),
        "recognition": copy.deepcopy(ocr.get("_recognition")) or {"source": "provided_ocr"},
        "weapon_match": {},
        "weapon_base": {"source": "weapon_database"},
        "checks": {"image_accuracy_verified": False},
    }
    out: dict = {
        "provenance": provenance,
        "ok": False,
        "weapon": None,
        "weapon_query": "",
        "alts": [],
        "raw_mods": ocr.get("mods") or [],
        "arcanes": [
            dict(a) for a in ocr.get("arcanes") or [] if isinstance(a, dict) and a.get("name")
        ],
        "mods": [],
        "unknown": [],
        "totals": {},
        "panel": dict(ocr["panel"]) if isinstance(ocr.get("panel"), dict) else {},
        "panel_base": "",
        "checks": [],
        "notes": [],
        "errors": [],
        "capacity": str(ocr.get("capacity") or "").strip(),
        # ★ 豆子（第二信号）统计：可用行数 / 实际采纳张数 / 与容量反推冲突张数
        "pips": {
            "rows": len([r for r in (pips_rows or []) if not r.get("is_inventory")]),
            "used": 0,
            "conflict": 0,
        },
    }

    # 必须先看原始字段；归一化会截掉多余读数和量纲，不能再用归一结果验结构。
    panel_reasons = _panel_structure_reasons(ocr.get("panel"))
    layout = analyze_layout(ocr, pips_rows)
    out["layout"] = layout
    out["quality"] = {
        "content_read": layout["content_read"],
        "layout_complete": layout["layout_complete"],
        "calculation_allowed": False,
        "layout_reasons": copy.deepcopy(layout["reasons"]),
        "layout_evidence": {
            **copy.deepcopy(ocr.get("coordinate_ocr") or {}),
            "pixel_geometry": copy.deepcopy(layout["pixel_evidence"]),
            "bindings": copy.deepcopy(layout["bindings"]),
        },
        "snapshot_safe": False,
        "panel_safe": not panel_reasons,
        "reasons": panel_reasons,
    }

    # ---- 伤害行归一：三段式 [标签, 基础, 最终] → [标签, "基础>最终"] ----
    # 模型常把「9 ▸ 25.4」读成 ["冲击", "9", "25.4"] 三个元素，旧解析只吃
    # row[1]（基础）会把最终值丢掉，总伤校验也随之失真
    _panel = out["panel"]
    _fixed = []
    for _r in _panel.get("damage_rows") or []:
        if (
            isinstance(_r, (list, tuple))
            and len(_r) >= 3
            and to_float(_r[2]) is not None
            and (to_float(_r[1]) is None or to_pair(_r[1])[1] is None)
        ):
            _fixed.append([str(_r[0]), f"{_r[1]}>{_r[2]}"])
        elif isinstance(_r, (list, tuple)):
            _fixed.append(list(_r[:2]) if len(_r) > 2 else list(_r))
        else:
            _fixed.append(_r)
    if _fixed:
        _panel["damage_rows"] = _unique_damage_rows(_fixed)
    # ---- 面板标量归一：两列都给时一律取右列（MOD 后终值）----
    # 升级界面每行是「基础 ▸ MOD 后」两列；提示词要求只给右列，但模型常把两列
    # 一起给（"22% ▸ 138%" / ["4.17", "3.33"]），而下游 to_float 取**第一个**数
    # ⇒ 把基础值当终值：反推基础被多除一次 MOD、射速还会被打上「取自面板」而跳过
    # 射速 MOD（2026-10-09 修，拉特昂 Prime 截图实测口径）。单值保持原样。
    for _k in _PANEL_SCALARS:
        _l, _r = to_pair(_panel.get(_k))
        if _l is not None and _r is not None:
            _panel[_k] = _r
    _tot = _panel.get("total")
    if isinstance(_tot, (list, tuple)) and len(_tot) >= 3 and to_float(_tot[2]) is not None:
        _panel["total"] = [str(_tot[0]), f"{_tot[1]}>{_tot[2]}"]

    # ---- 武器 ----
    out["weapon_query"] = clean_weapon_name(ocr.get("weapon"))
    if out["weapon_query"]:
        weapon, alts = dc.find_weapon(out["weapon_query"])
        out["weapon"], out["alts"] = weapon, alts
        method = "unmatched"
        if weapon:
            query = out["weapon_query"]
            if dc._norm(query) in dc._name_index():
                method = "exact"
            elif any(f in dc._name_index() for f in dc.matching.expand_variants(query)):
                method = "variant_equivalent"
            else:
                method = "substring"
        provenance["weapon_match"] = {
            "raw": copy.deepcopy(ocr.get("weapon")),
            "query": out["weapon_query"],
            "canonical": weapon.get("name") if weapon else None,
            "method": method,
        }
        if weapon is None:
            out["errors"].append(f"武器库里没找到「{out['weapon_query']}」")
    else:
        out["errors"].append("没读到武器名")

    # ---- 豆子（第二信号）整体对齐 ----
    # ★ 必须在逐卡循环**之前**做：对齐需要「全部卡的候选集」这个全局信息。
    #   位置不采信模型报的 row/col（端到端实测其不可靠），而由
    #   「卡顺序 + 候选集约束」反推（见 core.pips.align_rows 的说明）。
    pips_map: dict = {}
    if pips_rows:
        _cands: list = []
        _align_indices = []
        for _index, _r0 in enumerate(out["raw_mods"]):
            if isinstance(_r0, dict) and _r0.get("_slot") == "auxiliary":
                continue
            _align_indices.append(_index)
            if not isinstance(_r0, dict):
                _cands.append([])
                continue
            _rec0, _ = match_mod(str(_r0.get("name") or "").strip())
            _cands.append(rank_candidates(_rec0, to_int(_r0.get("drain"))) if _rec0 else [])
        _eq = [r for r in pips_rows if not r.get("is_inventory")]
        _al = pips_engine.align_rows(_cands, [r.get("counts") or [] for r in _eq])
        if _al:
            pips_map = dict(zip(_align_indices, _al))

    # 有原图坐标的条目只能使用几何唯一绑定，不借其它卡的序号/容量补位。
    for index, raw in enumerate(out["raw_mods"]):
        if isinstance(raw, dict) and raw.get("_geometry_source") in {
            "coordinate_ocr",
            "structured_box",
            "pixel_tile",
        }:
            pips_map.pop(index, None)
    pips_map.update(layout["pixel_rank_positions"])
    provenance["slot_bindings"] = copy.deepcopy(layout["bindings"])
    provenance["pixel_rank_positions"] = copy.deepcopy(layout["pixel_rank_positions"])
    unmatched_pips_rows = []
    if pips_map:
        used_rows = {position[0] for position in pips_map.values()}
        unmatched_pips_rows = [
            r
            for i, r in enumerate(_eq)
            if i not in used_rows and any(c > 0 for c in r.get("counts") or [])
        ]

    # ---- 逐卡识别 ----
    totals: dict = {k: 0.0 for k in _ADD_FIELDS}
    totals.update(
        {
            "elements": {},
            "physical": {},
            "uncalc": [],
            "no_rank": [],
            "throw_max_stacks": 0,
            # 派系卡（灭亡/歼灭类）按派系分桶：{派系名: 百分比}（2026-10-06 同
            # damage_calc 口径：不再折进通用 faction_dmg 打任意派系都生效）
            "faction_dmg_of": {},
        }
    )
    for _mi, raw in enumerate(out["raw_mods"]):
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "").strip()
        drain = to_int(raw.get("drain"))
        rec, how = match_mod(name)
        item: dict = {
            "raw": name,
            "drain": drain,
            "how": how,
            "found": rec is not None,
            "rank": None,
            "note": "",
            "effect": {},
            "calculable": False,
            "provenance": {
                "name": {
                    "raw": copy.deepcopy(raw.get("name")),
                    "canonical": rec.get("name") if rec else None,
                    "method": how or "unmatched",
                    "source": "code_match",
                },
                "rank": {"source": "unresolved"},
            },
        }
        item["slot_binding"] = copy.deepcopy(layout["bindings"][_mi])
        if rec:
            item.update(
                {
                    "name": rec.get("name"),
                    "zh": rec.get("zh"),
                    "compat": rec.get("compat"),
                    "max_rank": rec.get("max_rank"),
                    "base_drain": rec.get("base_drain"),
                    "polarity": rec.get("polarity"),
                    "calculable": bool(rec.get("calculable")),
                }
            )
            # 模型颜色只是观察，不能单独把容量多解缩成一个等级；
            # 只有实际数字像素颜色才约束容量分支，与豆子读数共同定级。
            _pixel_color = raw.get("color") or raw.get("drain_color")
            if raw.get("_color_source") == "model_hint" and not raw.get("_color_evidence"):
                _pixel_color = None
            rank, why = infer_rank(rec, drain, _pixel_color)
            # ★ 豆子（第二信号）：亮豆数 = 实际等级，与容量反推互相印证
            _position = pips_map.get(_mi)
            _color_candidates = rank_candidates(rec, drain, _pixel_color)
            _capacity_impossible = (
                rec.get("base_drain") is not None
                and rec["base_drain"] >= 0
                and drain is not None
                and not _color_candidates
            )
            _zero_unanchored = not _zero_row_anchored(
                _position, pips_rows, ocr.get("_pixel_layout")
            )
            _pixel_color_conflict = False
            _observed_count = None
            if _position is not None and not _zero_unanchored:
                _ri, _ci = _position
                _rows = [r for r in pips_rows or [] if not r.get("is_inventory")]
                if _ri < len(_rows) and _ci < len(_rows[_ri].get("counts") or []):
                    _observed_count = _rows[_ri]["counts"][_ci]
                    _full_marks = _rows[_ri].get("maxed") or []
                    _full_rank = (
                        rec.get("max_rank") if _ci < len(_full_marks) and _full_marks[_ci] else None
                    )
                    _max_line_supported = _full_rank is not None and (
                        _full_rank in _color_candidates
                        or drain is None
                        or (_capacity_impossible and _mi in layout["pixel_rank_positions"])
                    )
                    if _max_line_supported and _mi in layout["pixel_rank_positions"]:
                        _capacity_impossible = False
                    _pixel_color_conflict = (
                        not _max_line_supported
                        and rec.get("base_drain") is not None
                        and rec["base_drain"] >= 0
                        and drain is not None
                        and _observed_count not in _color_candidates
                    )
            _pr, _psrc = _pips_rank(
                None
                if _zero_unanchored or _capacity_impossible or _pixel_color_conflict
                else _position,
                pips_rows,
                _color_candidates,
                rec.get("max_rank"),
            )
            if (
                _pr is not None
                and _mi not in layout["pixel_rank_positions"]
                and raw.get("_color_source") == "model_hint"
                and not raw.get("_color_evidence")
                and _pr
                not in rank_candidates(rec, drain, raw.get("color") or raw.get("drain_color"))
            ):
                # 顺序对齐和模型颜色互相冲突时，没有独立定位能裁决；
                # 不能把其它列的零颗豆子升级为已验证等级。
                rank = _pr = None
                _psrc = "顺序对齐与容量颜色观察冲突，缺少独立定位，等级未确定"
            if _capacity_impossible:
                _psrc = "容量及数字颜色无兼容等级，未用空候选作像素通配"
            if _zero_unanchored:
                _psrc = "全零行缺少该列卡底几何，未采信像素零级"
            if _pixel_color_conflict:
                rank = None
                why = (
                    f"像素豆数 {_observed_count} 与容量/数字颜色候选 {_color_candidates} 冲突，"
                    "两路均未定级"
                )
                _psrc = "独立像素与容量颜色不一致"
                out["pips"]["conflict"] += 1
                item["rank_conflict"] = {
                    "pixel_count": _observed_count,
                    "capacity_color_candidates": list(_color_candidates),
                    "raw_color": raw.get("color") or raw.get("drain_color"),
                    "position": list(_position),
                }
            if _pr is not None:
                out["pips"]["used"] += 1
                if rank is None:
                    why = f"豆子计数 {_pr} 级（{_psrc}）；容量未唯一定级"
                elif _pr != rank:
                    out["pips"]["conflict"] += 1
                    why = (
                        f"豆子计数 {_pr} 级（{_psrc}）—— 容量反推本为 {rank} 级，"
                        f"已采信豆子（容量有多种解读时以豆子为准）"
                    )
                else:
                    why = f"容量与豆子一致（{_pr} 级，{_psrc}）"
                rank = _pr
            elif _psrc:
                # 豆子被拒收（该格豆数不在容量候选集里 = 检测噪声）→ 说明原因，
                # 别静默（维护者 2026-09-20 报「等级又对不上」就是静默回退导致的）。
                out["pips"]["rejected"] = out["pips"].get("rejected", 0) + 1
                why = f"{why}；{_psrc}" if why else _psrc
            if (
                raw.get("_slot") == "auxiliary"
                and _pr is None
                and len(rank_candidates(rec, drain, raw.get("color") or raw.get("drain_color"))) > 1
            ):
                rank = None
                why = "辅助槽容量存在多个等级解，未取得独立豆子证据，等级未确定"
            # ★ 极性分支说明（2026-09-20 玩家报障「容量要求增加 25% 没说明」）：
            #   等级定下来后 (基础容量, 等级, 卡面容量) 能**唯一**反查出分支，
            #   不必采信模型读的颜色（实测它常读错：北风实际红 9 报白、
            #   肢解实际绿 5 报白）。容量反推的文案里已含同样字串时跳过，避免重复。
            _br = drain_branch(rec, rank, drain)
            if _br and _br not in why:
                why = f"{why}；{_br}" if why else _br
            if how == "子串" and _norm(name) not in {_norm(rec.get("name")), _norm(rec.get("zh"))}:
                item["name_incomplete"] = True
                incomplete = f"名称未读全，候选「{rec.get('zh') or rec.get('name')}」尚未确认"
                why = f"{why}；{incomplete}" if why else incomplete
            if how in {"近似", "同族近似"}:
                correction = f"名称按代码{how}匹配：「{name}」→「{rec.get('zh') or rec.get('name')}」，请核对原图"
                why = f"{why}；{correction}" if why else correction
            item["provenance"]["rank"] = {
                "source": "pixel_pips"
                if _pr is not None
                else ("capacity_inference" if rank is not None else "unresolved"),
                "value": rank,
                "raw_drain": copy.deepcopy(raw.get("drain")),
                "candidates": rank_candidates(rec, drain, _pixel_color),
                "observed_color": raw.get("color") or raw.get("drain_color"),
                "color_source": raw.get("_color_source"),
                "pixel_rank": _pr,
            }
            item["rank"], item["note"] = rank, why
            eff = effect_at(rec, rank) if rec.get("calculable") else {}
            item["effect"] = eff
            if rank is None and rec.get("calculable"):
                totals["no_rank"].append(item.get("zh") or name)
            if not eff:
                totals["uncalc"].append(item.get("zh") or name)
            for key in _ADD_FIELDS:
                if key == "faction_dmg" and eff.get("faction_of"):
                    continue  # 派系卡走 faction_dmg_of 分桶，不进通用桶
                if eff.get(key):
                    totals[key] += float(eff[key])
            # 斩铁：「重击时 x2」→ 重击的暴击几率加成额外再吃一份
            _hm = float(eff.get("heavy_crit_mult") or 1.0)
            if _hm > 1.0:
                totals["crit_chance_heavy"] += float(eff.get("crit_chance") or 0.0) * (_hm - 1.0)
            if eff.get("throw_max_stacks"):
                totals["throw_max_stacks"] = max(
                    int(totals["throw_max_stacks"]), int(eff["throw_max_stacks"])
                )
            # 派系卡按派系分桶（倍率写法 x1.55 与百分比写法都折成百分点）
            _fac_of = str(eff.get("faction_of") or "")
            if _fac_of:
                _fac_pct = (
                    (float(eff["faction_mul"]) - 1) * 100
                    if eff.get("faction_mul")
                    else float(eff.get("faction_dmg") or 0.0)
                )
                if _fac_pct:
                    totals["faction_dmg_of"][_fac_of] = (
                        totals["faction_dmg_of"].get(_fac_of, 0.0) + _fac_pct
                    )
            for el, val in (eff.get("elements") or {}).items():
                if el in ("impact", "puncture", "slash"):
                    # 数据层历史双写：物理类型只走 physical 通道，防重复折算
                    continue
                totals["elements"][el] = totals["elements"].get(el, 0.0) + float(val)
            for el, val in (eff.get("physical") or {}).items():
                totals["physical"][el] = totals["physical"].get(el, 0.0) + float(val)
        else:
            # ★ 裂罅紫卡（2026-09-20 方案文档 TC-04）：名字由「武器名 + 随机词缀」
            #   组成（如「野猪 Visi-satidex」），**不可能**在静态词典里全字匹配。
            #   以前它会被当成「库中未收录」—— 那是误导（不是我们缺数据，是它本来随机）。
            #   这里单独识别出来：数值无法核算（词缀随机），但**等级仍能从豆子读**。
            if is_riven_name(name):
                item["riven"] = True
                item["zh"] = "裂罅紫卡"
                item["rank"] = _pips_count_at(pips_map.get(_mi), pips_rows)
                item["provenance"]["rank"] = {
                    "source": "pixel_pips" if item["rank"] is not None else "unresolved",
                    "value": item["rank"],
                }
                item["note"] = "裂罅紫卡：词缀随机生成，数值无法自动核算" + (
                    f"；豆子计数 {item['rank']} 级" if item["rank"] is not None else "；等级未读到"
                )
            else:
                out["unknown"].append(name)
        out["mods"].append(item)

    # ---- 家族互斥（mods_wfsim_extra.json family，wiki Incompatible）：同族两张游戏里只能装一张 ----
    fam_seen: dict[str, list] = {}
    for item in out["mods"]:
        if not item.get("found"):
            continue
        fam = dc.mod_extra(item).get("family")
        if fam:
            fam_seen.setdefault(fam, []).append(str(item.get("zh") or item.get("raw") or ""))
    for fam, zs in fam_seen.items():
        if len(zs) >= 2:
            out["notes"].append(
                f"⚠️ 互斥家族 {fam}：{'、'.join(zs)} —— 游戏里只能装一张，"
                "若截图确实如此请人工核对（加成可能重复计了）"
            )

    # ---- 镀层类条件堆叠：默认不计入，但告诉用户可以显式带入 ----
    _COND_ZH = {
        "condition_overload": "每异常种类直伤",
        "multishot": "多重",
        "crit_chance": "暴击率",
        "crit_damage": "暴伤",
        "damage": "直伤",
        "base_damage": "基伤",
        "fire_rate": "射速",
        "status_chance": "触发率",
        "status_damage": "状态伤害",
        "punch_through": "穿透",
    }
    hints: list[str] = []
    for item in out["mods"]:
        if not item.get("found") or not item.get("calculable"):
            continue
        for c in dc.mod_extra(item).get("conditionals") or []:
            if not c.get("mappable"):
                continue
            hints.append(
                f"{item.get('zh') or item.get('raw')}"
                f"（{_COND_ZH.get(c.get('grants'), c.get('grants'))}"
                f"+{float(c.get('value') or 0):g}%"
                # 层数 null = 来源原文没写（计算按 1 层）
                + (f"×{c['max_stacks']}层）" if c.get("max_stacks") is not None else "，层数未写）")
            )
    if hints:
        out["notes"].append(
            "💡 条件堆叠默认未计入，伤害指令加「满镀层」或「镀层N」带入："
            + "、".join(hints[:4])
            + ("…" if len(hints) > 4 else "")
        )

    # 不完整的配卡和结构矛盾的面板不能成为可信伤害快照。
    quality = out["quality"]
    reasons = quality["reasons"]
    for code, message, present in (
        ("weapon_missing", "武器未确认", not out["weapon"]),
        ("mods_missing", "未读到已装备 MOD", not out["mods"]),
        ("mods_overread", "卡片数量超过武器升级槽位上限，疑似混入仓库卡", len(out["mods"]) > 10),
        (
            "equipment_unconfirmed",
            "装备区补读未能确认，姿态或特殊槽可能漏读",
            bool(ocr.get("_equipment_unconfirmed")),
        ),
        ("pips_unmatched_row", "像素检测到已装备卡行尚未对应到识别清单", bool(unmatched_pips_rows)),
        ("unknown_mod", "有 MOD 名称未能识别", bool(out["unknown"])),
        ("rank_unknown", "有可计算 MOD 的等级未确定", bool(totals["no_rank"])),
        (
            "mod_name_incomplete",
            "有 MOD 名称只读到片段，候选尚未确认",
            any(m.get("name_incomplete") for m in out["mods"]),
        ),
        (
            "riven_uncalculated",
            "裂罅紫卡的随机词条数值未读取，无法完整折算配卡",
            any(m.get("riven") for m in out["mods"]),
        ),
        (
            "mods_underread",
            "识别卡数少于像素检测到的已装备卡数下界",
            pips_engine.expected_min_cards(pips_rows or []) > len(out["mods"]),
        ),
    ):
        if present:
            reasons.append({"code": code, "message": message})
    quality["snapshot_safe"] = not reasons
    # snapshot_safe 保留字段/数值自洽的既有含义；计算另需布局证据。
    # 当前坐标 OCR 只检测文字，像素豆子只检测等级刻度；两者均不观测
    # 空槽边框及完整特殊槽范围。不得把模型 row/col 或卡名数量当成补证。
    quality["calculation_allowed"] = quality["snapshot_safe"] and quality["layout_complete"]

    # ---- 灵化 / 特殊形态：只用结构完整且配卡可折算的面板反推 ----
    if quality["snapshot_safe"]:
        _lib_stats = {
            "cc": float(out["weapon"].get("criticalChance") or 0.0),
            "sc": float(out["weapon"].get("procChance") or 0.0),
        }
        _w2, _pnote = _panel_base_weapon(out["weapon"], out["panel"], totals)
        if _w2 is not None:
            out["weapon"], out["panel_base"] = _w2, _pnote
            provenance["weapon_base"] = {
                "source": "panel_reconstruction"
                if _w2.get("_panel_derived")
                else "incarnon_database",
                "selected_from_panel": True,
            }
            # 反推基础 vs 库内基础的合理性：面板的暴击/触发两行被模型
            # 读串（75.6%↔10%）时，反推值会偏离库内基础 3 倍以上 → 亮 ⚠
            _w = out["weapon"]
            for _k, _lab, _libv in (
                ("criticalChance", "暴击几率", _lib_stats["cc"]),
                ("procChance", "触发几率", _lib_stats["sc"]),
            ):
                _v = float(_w.get(_k) or 0.0)
                if _libv > 0.01 and (_v >= _libv * 3 or _v <= _libv / 3):
                    out["notes"].append(
                        f"⚠ 反推基础{_lab} {_v * 100:g}% 与库内基础 "
                        f"{_libv * 100:g}% 差 3 倍以上 —— 面板暴击/触发两行"
                        "疑似读串，建议重发截图"
                    )

    # ---- 复合元素合成（火>冰>电>毒 两两配对）----
    leftovers = dict(totals["elements"])
    combined: dict[str, float] = {}
    for pair, res in ELEM_PAIRS.items():
        names = sorted(pair, key=lambda e: ("heat", "cold", "electricity", "toxin").index(e))
        if all(n in leftovers and leftovers[n] > 0 for n in names):
            combined[res] = sum(leftovers.pop(n) for n in names)
    for el, val in leftovers.items():
        if val > 0:
            combined[el] = val
    totals["combined"] = combined
    out["totals"] = totals

    # ---- 面板推算与校验 ----
    # v1.11：反推路径也跑校验 —— 基础是净基础、MOD 叠算一次，推算值应当
    # 能与面板吻合（吻合=反推自洽；对不上=读漏行或进化平坦加成，如实标出）。
    # 总伤用引擎同款结算（不含派系/护甲），覆盖基伤乘区与自带元素
    _spec_p = _mapped_damage_spec(out)  # 仅派生面板诊断，不是截图可计算许可
    out["calc_panel"] = _calc_panel(out.get("weapon"), totals)
    if out.get("weapon"):
        _pt, _, _, _ = dc._hit_from_damage(
            out["weapon"]["damage"],
            float(out["weapon"]["damage"].get("total") or 0.0),
            _spec_p,
            dc._load().get("fac_table") or {},
            "__panel__",
            dr=0.0,
            viral_mult=1.0,
            mag_mult=1.0,
            # 军械库面板显示的是**量化前**的值（Xoris 截图「总计 156>264」= 未量化和），
            # 对拍面板就不能量化；实战伤害（calculate）照常量化
            quantize=False,
        )
        out["calc_panel"]["total"] = sum(_pt.values())
        # 分类型结算（含基伤乘区）：行和自检据此判断「识别行 ≈ 纯物理」并补合成元素
        out["calc_panel"]["by_type"] = dict(_pt)
    # 条件暴击卡（镀层瞄具族）：游戏面板暴击率显示似乎含条件加成 → 跳过该项校验
    _cond_crit = any(
        "critical" in json.dumps(m.get("effect") or {}, default=str).lower()
        for m in out.get("mods") or []
    )
    out["checks"] = _panel_checks(
        out.get("weapon"), totals, out["panel"], calc=out.get("calc_panel"), cond_crit=_cond_crit
    )
    basis = (
        "panel_reconstruction_consistency"
        if (out.get("weapon") or {}).get("_panel_derived")
        else "database_comparison"
    )
    provenance["checks"]["basis"] = basis
    provenance["panel_normalized"] = copy.deepcopy(out["panel"])
    provenance["weapon_base"]["values"] = copy.deepcopy(
        {
            key: (out.get("weapon") or {}).get(key)
            for key in ("damage", "criticalChance", "criticalMultiplier", "procChance", "fireRate")
        }
    )
    for check in out["checks"]:
        check["basis"] = basis
        check["image_accuracy_verified"] = False
    if out.get("weapon") and (out["panel"].get("damage_rows") and not _panel_total(out["panel"])):
        out["notes"].append("⚠ 面板总伤没读到，伤害行无法交叉验证 —— 数字可能有误，请自行核对")

    out["ok"] = bool(out.get("weapon")) and bool(out["mods"])

    # 识别完整时顺手跑一版参考伤害（默认 G系 100 级·身体），卡面直接给出，
    # 省得用户再去手打一遍「伤害」指令
    if out["ok"] and quality["calculation_allowed"]:
        try:
            _res = dc.calculate(to_damage_spec(out), out["weapon"])
            if _res.get("ok"):
                out["ref"] = _res
        except Exception:  # noqa: BLE001 - 参考值算不出来不影响识别结果
            pass
    return out


def _calc_panel(weapon: Optional[dict], totals: dict) -> dict:
    """按 MOD 折算出手枪/近战面板（不含派系倍率、不含护甲减免）。"""
    res: dict = {}
    if not weapon:
        return res
    cc_base = float(weapon.get("criticalChance") or 0.0)
    cm_base = float(weapon.get("criticalMultiplier") or 1.0)
    sc_base = float(weapon.get("procChance") or 0.0)
    res["crit_chance"] = cc_base * (1 + totals["crit_chance"] / 100.0)
    res["crit_damage"] = cm_base * (1 + totals["crit_dmg"] / 100.0)
    res["status_chance"] = sc_base * (1 + totals["status_chance"] / 100.0)

    dmg = weapon.get("damage") or {}
    base_total = float(dmg.get("total") or 0.0)
    phys = {k: float(dmg.get(k) or 0.0) for k in ("impact", "puncture", "slash")}
    for k, val in totals["physical"].items():
        if k in phys:
            phys[k] = phys[k] * (1 + val / 100.0)
    res["physical"] = phys
    elem_pct = sum(totals["elements"].values())
    res["element_damage"] = base_total * elem_pct / 100.0
    res["total"] = sum(phys.values()) + res["element_damage"]
    res["base_total"] = sum(float(dmg.get(k) or 0.0) for k in ("impact", "puncture", "slash"))
    return res


def splice_damage_rows(ocr: dict, rows: list) -> dict:
    """把聚焦读行得到的干净伤害行拼接回 OCR 结果（深拷贝，不动原 dict）。

    rows 形如 [["冲击", 545.6], ...]；若其中有「总计」行，同时替换
    panel.total —— 总伤校验依赖它。
    """
    import copy as _copy

    out = _copy.deepcopy(ocr or {})
    panel = out.setdefault("panel", {})
    panel["damage_rows"] = [list(r) for r in rows]
    for r in rows:
        if str(r[0]).lstrip("*").strip() in _TOTAL_LABELS:
            panel["total"] = [str(r[0]), str(r[1])]
            break
    return out


def _panel_total(panel: dict) -> Optional[float]:
    """面板总伤害：① 「总计」标签行 → ② total 字段 → ③ 值≈其余行之和的行。

    ③ 是防模型幻觉的兜底：实测它会把「总计 1807.3」挂到「爆炸」行名下
    （2026-09-16 执法者截图），这种行不该再被当成自带元素反推。
    """
    rows = [
        r for r in (panel.get("damage_rows") or []) if isinstance(r, (list, tuple)) and len(r) >= 2
    ]
    finals: list[float] = []
    for r in rows:
        if str(r[0]).lstrip("*").strip() in _TOTAL_LABELS:
            _, v = to_pair(r[1])
            if v or to_pair(r[1])[0]:
                return v or to_pair(r[1])[0]
        left, right = to_pair(r[1])
        v = right or left
        if v:
            finals.append(float(v))
    _, tot = to_pair(panel.get("total"))
    if tot or to_pair(panel.get("total"))[0]:
        return tot or to_pair(panel.get("total"))[0]
    if len(finals) >= 2:
        rest = sum(finals)
        for v in finals:
            if v < rest and abs(rest - v - v) / max(rest, 1.0) < 0.03:
                return v
    return None


def _panel_checks(
    weapon: Optional[dict],
    totals: dict,
    panel: dict,
    calc: Optional[dict] = None,
    cond_crit: bool = False,
) -> list[dict]:
    """识别到的面板值 vs 本地推算值；截图反推基础时属于自洽比较。

    calc：analyze 里用引擎同款结算算好的面板推算（含基伤乘区与自带元素）；
    不传则退回 _calc_panel 的近似（仅暴击/触发，总伤不参与）。

    cond_crit：配了条件暴击卡（镀层瞄具族）—— 游戏面板似乎会把条件
    加成也算进暴击率显示（拉特昂实测 138% vs 静态推算 66%），此时
    跳过暴击几率校验，只留触发/总伤/行和。
    """
    checks: list[dict] = []
    if not weapon:
        return checks
    if calc is None:
        calc = _calc_panel(weapon, totals)

    def add(
        label: str, got: Optional[float], want: Optional[float], unit: str = "", tol: float = 0.04
    ) -> None:
        if got is None or want in (None, 0):
            return
        ok = abs(got - want) <= max(abs(want) * tol, 1e-6)
        checks.append({"label": label, "panel": got, "calc": want, "unit": unit, "ok": ok})

    if not cond_crit:
        cc_panel = to_float(panel.get("crit_chance"))
        if cc_panel is not None:
            add("暴击几率", cc_panel, calc["crit_chance"] * 100, "%")
    cm_panel = to_float(panel.get("crit_damage"))
    if cm_panel is not None:
        # 面板暴伤与静态推算有 ~7% 的未知显示系数（拉特昂 6.6 vs 6.16）
        add("暴击伤害", cm_panel, calc["crit_damage"], "倍", tol=0.10)
    sc_panel = to_float(panel.get("status_chance"))
    if sc_panel is not None:
        add("触发几率", sc_panel, calc["status_chance"] * 100, "%")
    tot_v = _panel_total(panel)
    if tot_v:
        # 总伤由反推行自洽推出，偏差来自「行读漏/读串」与游戏侧未建模的
        # 显示系数（拉特昂 ~+6.7%）→ 容差 10%；漏行/读串由下面的
        # 行和一致性检查（纯 OCR 自检、容差 2%）负责兜住
        ms = 1.0 + float(totals.get("multishot") or 0.0) / 100.0
        add("总伤害", tot_v, calc["total"] * ms, "", tol=0.10)
        # 行和一致性（独立于本地推算的纯 OCR 自检）：Σ伤害行 ×(1+多重)
        # 应等于总计行 —— 漏一行/读串一行立刻暴露，不受游戏未知显示
        # 系数影响（拉特昂面板各行有 ~+6.7% 的未建模系数）
        rows = panel.get("damage_rows") or []
        if len(rows) >= 2 and tot_v:
            rev = {}
            for _k, _zh in {**PHYS_ZH, **ELEM_ALL_ZH}.items():
                rev.setdefault(str(_zh), _k)
            vals_rows: list[float] = []
            for row in rows:
                if not isinstance(row, (list, tuple)) or len(row) < 2:
                    continue
                label = str(row[0]).lstrip("*").strip()
                # 只统计真正的伤害类型行 —— 模型偶尔把暴击/触发等属性行
                # 也塞进 damage_rows，混进来会把行和撑爆
                if not any(label.startswith(zh) for zh in rev):
                    continue
                if label.startswith("总"):
                    continue
                vals = [x for x in row[1:] if x is not None and str(x).strip()]
                _l, _r = to_pair(vals[-1] if vals else None)
                v = _r if _r else _l
                if v:
                    vals_rows.append(float(v))
            # 值 ≈ 总计的伤害行 = 模型把「总计」挂到了别的行名下（与 _panel_base_weapon
            # 的反推同口径跳过）；只在其余行确有数值时剔除 —— 单一类型且多重 1 的
            # 武器，那一行本来就等于总计
            _rest = [v for v in vals_rows if abs(v - tot_v) / tot_v >= 0.02]
            if len(_rest) < len(vals_rows) and sum(_rest) >= 0.02 * tot_v:
                vals_rows = _rest
            s = sum(vals_rows)
            n_rows = len(vals_rows)
            if n_rows >= 2:
                # 行和自检 = 识别到的伤害行 + **本地推算的合成元素**（补齐）
                #
                # 为什么补：视觉模型常**整行漏读合成元素行**——实测 Xoris
                # （2026-09-17 用户截图）面板是「冲击 24/穿刺 40.8/切割 55.2
                # + 爆炸（火+冰）144 = 总计 264」，模型只读到 3 行物理（120），
                # 于是行和 120 对不上 264，被误报成「读串」。而这 144 正是
                # 熔岩冲击(+90%火)+北风(+30%冰) 合成的爆炸，本地推算里本来就有
                # （calc["element_damage"]）。
                #
                # 只在「识别行和 ≈ 纯物理和」时补（模型读全时 s 已含元素，
                # 再补会双计）。
                _bt = calc.get("by_type")
                if _bt:
                    # 引擎同款结算（含基伤 MOD）—— _calc_panel 的 physical/element
                    # 不乘基伤乘区，配了膛线类就永远判不成「≈纯物理」（2026-10-09
                    # 拉特昂 Prime 实测：推算物理 96 vs 识别 254.3，合成病毒补不进来）
                    _phys = sum(float(v) for k, v in _bt.items() if k in PHYS_ZH)
                    _elem = sum(float(v) for k, v in _bt.items() if k not in PHYS_ZH)
                else:
                    _phys = sum(float(v) for v in (calc.get("physical") or {}).values())
                    _elem = float(calc.get("element_damage") or 0.0)
                _full = s
                if _elem > 0.5 and abs(s - _phys) <= max(_phys * 0.05, 0.5):
                    _full = s + _elem
                add("行和×多重", tot_v, _full * ms, "", tol=0.02)
                if not any(c["label"] == "行和×多重" and c["ok"] for c in checks):
                    # 面板总计与行和不符 → 记一条明示，别让用户以为是漏卡
                    checks.append(
                        {
                            "label": "总伤害行",
                            "panel": tot_v,
                            "calc": s * ms,
                            "unit": "",
                            "ok": True,
                            "note": "伤害行未完整对应总计，可能漏行或串行；请核对原图",
                        }
                    )
    return checks


# ---------------------------------------------------------------------------
# 卡面
# ---------------------------------------------------------------------------
def _rank_text(item: dict) -> str:
    """MOD 条目右侧的「容量 → 等级」描述。

    ★ `容量0?` 的含义（2026-09-20 玩家反馈「出现了个容量 0」后加）：
    库里**确有** base_drain=0 的卡（survival instinct / transfusion），
    它们**能正常反推出等级** → 会显示成 `容量0→0/3`。所以
    「drain 读到 0 **且** rank 反推不出来」基本只可能是**误读**（把该位置读成了 0）。
    这时标成 `容量0?` 而不是 `容量0`，避免让人以为该卡容量真是 0；
    等级未知的 MOD 其效果按满级计入（见 effect_at），不会算崩。
    """
    drain = item.get("drain")
    rank, max_rank = item.get("rank"), item.get("max_rank")
    # 歧义标记（2026-09-20）：容量数字有多重解释、且颜色解与选中的最高解冲突时，
    # `infer_rank` 会把「⚠歧义」写进 note —— 卡面加个 `?` 提示「等级存疑，
    # 以游戏内为准」，不要让它看起来像已确定。
    amb = "⚠歧义" in str(item.get("note") or "")
    if drain == 0 and rank is None:
        head = "容量0?"
    else:
        head = f"容量{int(drain)}" if drain is not None else "容量?"
    if rank is None:
        return head
    if max_rank:
        # ⚠️ 别用 ✔：卡片字体没有这个字形，渲染出来会凭空消失（实测）
        full = "满级" if rank >= int(max_rank) else "★非满级"
        text = f"{head}→{rank}/{int(max_rank)}{full}"
    else:
        text = f"{head}→{rank}级"
    return f"{text}?" if amb else text


def _disp_width(text: str) -> int:
    """粗略显示宽度：CJK 算 2，其余算 1（卡面是等宽中文字体）。"""
    return sum(2 if ord(c) > 0x2E80 else 1 for c in str(text))


def _wrap_bits(bits: list[str], width: int = 44) -> list[str]:
    """把「甲+1、乙+2、…」按显示宽度折行，避免整行被硬折断。"""
    out: list[str] = []
    cur = ""
    for b in bits:
        piece = b if not cur else "、" + b
        if cur and _disp_width(cur) + _disp_width(piece) > width:
            out.append(cur)
            cur = b
        else:
            cur += piece
    if cur:
        out.append(cur)
    return out or [""]


def _effect_text(eff: dict) -> str:
    """把某一等级的效果压成一行短文本。"""
    if not eff:
        return ""
    bits: list[str] = []
    # 特殊格式的两项（倍率 / 带层数），不走下面的「+X%」模板
    if eff.get("heavy_crit_mult"):
        bits.append(f"重击时暴击几率 ×{eff['heavy_crit_mult']:g}")
    if eff.get("throw_dmg"):
        _st = eff.get("throw_max_stacks")
        bits.append(
            f"连续投掷伤害 +{eff['throw_dmg']:g}%/层" + (f"（最多 {_st} 层）" if _st else "")
        )
    for key, label in (
        ("base_dmg", "基伤"),
        ("heavy_dmg", "重击伤害"),
        ("multishot", "多重"),
        ("crit_chance", "暴击率"),
        ("crit_dmg", "暴伤"),
        ("status_chance", "触发率"),
        ("status_dmg", "状态伤害"),
        ("fire_rate", "射速"),
        ("faction_dmg", "派系"),
        ("headshot_bonus", "爆头倍率"),
        ("punch_through", "穿透"),
        ("initial_combo", "初始连击"),
        ("crit_per_combo", "暴击率/连击倍率"),
        ("status_per_combo", "触发率/连击倍率"),
        ("dmg_per_status", "基伤/异常种类"),
    ):
        val = eff.get(key)
        if val:
            unit = "" if key in ("punch_through", "initial_combo") else "%"
            bits.append(f"{label}{val:+g}{unit}")
    for el, val in (eff.get("elements") or {}).items():
        if el in ("impact", "puncture", "slash"):
            continue  # 物理类型在下一行 physical 里显示，别重复一条
        bits.append(f"{ELEM_ALL_ZH.get(el, el)}{val:+g}%")
    for el, val in (eff.get("physical") or {}).items():
        bits.append(f"{PHYS_ZH.get(el, el)}{val:+g}%")
    if eff.get("faction_mul"):
        bits.append(f"派系×{eff['faction_mul']:g}")
    return "、".join(bits)


def to_damage_spec(an: dict, level: int = 100, faction: str = "Grineer") -> Optional[dict]:
    """可信截图才能进入业务计算；无 quality 的手工输入沿用字段映射契约。"""
    quality = an.get("quality")
    if isinstance(quality, dict) and quality.get("calculation_allowed") is not True:
        return None
    return _mapped_damage_spec(an, level, faction)


def _mapped_damage_spec(an: dict, level: int = 100, faction: str = "Grineer") -> Optional[dict]:
    """纯字段映射，供派生面板诊断；不提供截图完整性证明。"""
    if not an.get("weapon"):
        return None
    spec, _ = dc.parse_args([])
    t = an.get("totals") or {}
    spec["form"] = "keep"  # 面板已从截图反推（可能就是灵化形态）→ 不再套形态数据
    spec.update(
        {
            "level": int(level),
            "faction": faction,
            "base_dmg": float(t.get("base_dmg") or 0.0),
            "multishot": float(t.get("multishot") or 0.0),
            "crit_chance": float(t.get("crit_chance") or 0.0),
            "crit_dmg": float(t.get("crit_dmg") or 0.0),
            "faction_dmg": float(t.get("faction_dmg") or 0.0),
            "status_chance": float(t.get("status_chance") or 0.0),
            "status_dmg": float(t.get("status_dmg") or 0.0),
            "headshot_bonus": float(t.get("headshot_bonus") or 0.0),
            "crit_chance_heavy": float(t.get("crit_chance_heavy") or 0.0),
            "throw_dmg": float(t.get("throw_dmg") or 0.0),
            "throw_max_stacks": int(t.get("throw_max_stacks") or 0),
            "heavy_dmg": float(t.get("heavy_dmg") or 0.0),
            "initial_combo": float(t.get("initial_combo") or 0.0),
            "punch_through": float(t.get("punch_through") or 0.0),
            "crit_per_combo": float(t.get("crit_per_combo") or 0.0),
            "status_per_combo": float(t.get("status_per_combo") or 0.0),
            "dmg_per_status": float(t.get("dmg_per_status") or 0.0),
            "singles": {k: float(v) for k, v in (t.get("elements") or {}).items()},
            "physical": {k: float(v) for k, v in (t.get("physical") or {}).items()},
            # 派系卡分桶 → calculate 按最终派系取用（与 parse_args 同构）
            "faction_dmg_of": [[f, float(p)] for f, p in (t.get("faction_dmg_of") or {}).items()],
        }
    )
    # 反推路径的基础是「把 MOD 除回去」的净基础（v1.11 修正：此前误把 MOD
    # 再零化一次，导致反推路径 MOD 一次都没算、参考伤害只有面板的 ~1/18）。
    # 正确语义 = 净基础 + MOD 正常叠算一次 —— 与灵化库直采路径完全一致，
    # 这里不再对 spec 做任何零化。
    if t.get("heavy_dmg") or t.get("initial_combo"):
        spec["heavy"] = True  # 配了重击类 MOD 就顺便给重击数据
    # ★ 射速 MOD（2026-10-09 修：此前 totals.fire_rate 从未进 spec ⇒ 识卡 DPS 漏算射速，
    #   带「+射速 −基伤」的卡反而让 DPS 变低）。只在 fireRate **不是**面板终值时叠加：
    #   · 依据【实测·2026-10-09】拉特昂 Prime 配关键延迟（−20% 射速）的升级界面截图：
    #     「射击速度 4.17 ▸ 3.33」，库基础 4.166667 × 0.8 = 3.3333 ⇒ 攻速行右列 = 已含 MOD
    #     的终值（与执法者截图 暴击 75.6% vs 基础 20%、冲击 545.6 vs 168 的结论一致）。
    #     两条路径取同一值：读到面板 3.33 → 直接用、不叠；没读到 → 基础 × (1 − 20%)。
    #     守卫：tests/test_loadout_ocr.py「拉特昂 Prime 截图：…两条路径同值」。
    #   · 判据按「射速来源」而非「是否走了面板反推」：反推分支没读到攻速行、或灵化库直采
    #     分支，fireRate 仍是基础值（实例：拉特昂 Prime 反推但无攻速行，关键延迟 −20% 须计入）。
    #   · 两列取值：模型若把「基础 ▸ 终值」两列一起给，analyze 入口已统一归一为右列
    #     （_PANEL_SCALARS），这里拿到的 attack_speed 一定是终值。
    w = an.get("weapon") or {}
    if not w.get(FIRE_RATE_FROM_PANEL):
        spec["fire_rate_pct"] = float(t.get("fire_rate") or 0.0)
        # totals.fire_rate 来自 MOD 库值（卡面的步枪值）⇒ 全部算「射速 MOD」部分，
        # 弓/弩由 calculate 翻倍（面板终值分支不进这里，不会重复翻倍）
        spec["fire_rate_mod_pct"] = spec["fire_rate_pct"]
    return spec


def card_lines(an: dict) -> list[str]:
    """识别结果卡面。

    行文法（2026-10-11 卡片改版）：卡首 KV（武器 / 识别 / 折算加成 / 参考）→ 武器面板 →
    卡片表（名称｜容量与等级｜效果）→ 面板校验表 → ⚠ 警示与注脚（≤3 行）。
    """
    from .formatters import limit_notes

    lines: list[str] = []
    w = an.get("weapon")
    panel_rows: list[str] = []
    if w:
        dmg = w.get("damage") or {}
        parts = "、".join(
            f"{PHYS_ZH[k]}{float(dmg.get(k) or 0):g}"
            for k in ("impact", "puncture", "slash")
            if float(dmg.get(k) or 0) > 0
        )
        cat = CATEGORY_ZH.get(w.get("category") or "", w.get("category") or "")
        lines.append(
            f"◆ 武器：{w.get('zh') or w['name']}（{w['name']}）｜{cat}｜MR{w.get('masteryReq', 0)}"
        )
        panel_rows.append(
            f"· 基础　{parts}（计 {float(dmg.get('total') or 0):g}）"
            f"｜暴击 {float(w.get('criticalChance') or 0) * 100:g}%"
            f"×{float(w.get('criticalMultiplier') or 1):g}"
            f"｜触发 {float(w.get('procChance') or 0) * 100:g}%"
        )
        if an.get("panel_base"):
            panel_rows.append(f"· 面板　{an['panel_base']}")
    else:
        lines.append(f"◆ 武器：未能识别（读到的是「{an.get('weapon_query') or '空'}」）")

    weapon_match = (an.get("provenance") or {}).get("weapon_match") or {}
    if weapon_match.get("method") == "substring":
        lines.append(
            f"· 武器名称经代码子串匹配：「{an.get('weapon_query')}」→「{(w or {}).get('zh') or (w or {}).get('name')}」，请核对原图"
        )

    # —— 面板校验结论（先算好，卡首「识别」行引用）——
    checks = an.get("checks") or []
    _tips = [c for c in checks if c.get("note")] if checks else []
    if checks:
        # 带 note 的项是「说明性提示」（如面板总计读串），不参与 ✓/⚠ 对比
        checks = [c for c in checks if not c.get("note")]
    bad = [c for c in checks if not c["ok"]]
    verdict = ""
    panel_derived = bool((an.get("weapon") or {}).get("_panel_derived"))
    check_label = "反推自洽" if panel_derived else "面板校验"
    if checks:
        verdict = f"｜{check_label} " + ("可比项目吻合" if not bad else f"{len(bad)} 项对不上")

    cap = f"｜容量 {an['capacity']}" if an.get("capacity") else ""
    lines.append(f"◆ 识别：{len(an.get('mods') or [])} 张卡{cap}{verdict}")
    quality = an.get("quality") or {}
    if quality and not quality.get("calculation_allowed", quality["snapshot_safe"]):
        lines.append("◆ 识卡状态：结果未确认，本次不生成识卡伤害记录")
        lines.extend(
            f"· 待核对：{reason['message']}"
            for reason in quality["reasons"] + quality.get("layout_reasons", [])
        )
    if checks:
        lines.append("· 面板校验仅比较可用数值，不代表图像识别完整无误")
        if panel_derived:
            lines.append("· 基础由同一截图面板反推，数值吻合仅证自洽，不是独立识别验证")

    for arcane in an.get("arcanes") or []:
        name = str(arcane.get("name") or "").replace("\n", " ")[:60]
        evidence = arcane.get("_rank_evidence") or {}
        rank = evidence.get("rank") if evidence.get("source") == "arcane_diamond_pixels" else None
        level = f"{rank}级" if isinstance(rank, int) and 0 <= rank <= 5 else "等级未确认"
        lines.append(f"· 赋能：{name}（{level}，条件加成未计入）")
    totals = an.get("totals") or {}
    bits: list[str] = []
    for key, label in (
        ("base_dmg", "基伤"),
        ("heavy_dmg", "重击伤害"),
        ("multishot", "多重"),
        ("crit_chance", "暴击率"),
        ("crit_dmg", "暴伤"),
        ("status_chance", "触发率"),
        ("status_dmg", "状态伤害"),
        ("fire_rate", "射速"),
    ):
        if totals.get(key):
            bits.append(f"{label}{totals[key]:+g}%")
    if totals.get("crit_chance_heavy"):
        bits.append(f"重击时暴击率再+{totals['crit_chance_heavy']:g}%")
    if totals.get("throw_dmg"):
        _st = totals.get("throw_max_stacks")
        bits.append(
            f"投掷伤害+{totals['throw_dmg']:g}%/层" + (f"（最多{int(_st)}层）" if _st else "")
        )
    for el, val in (totals.get("elements") or {}).items():
        bits.append(f"{ELEM_ALL_ZH.get(el, el)}{val:+g}%")
    for el, val in (totals.get("physical") or {}).items():
        bits.append(f"{PHYS_ZH.get(el, el)}{val:+g}%")
    if bits:
        lines.append("◆ 按实际等级折算的加成：" + "、".join(bits))
    combined = totals.get("combined") or {}
    if combined:
        lines.append(
            "◆ 合成后元素："
            + "、".join(f"{ELEM_ALL_ZH.get(k, k)}{v:+g}%" for k, v in combined.items())
        )

    ref = an.get("ref") if quality.get("snapshot_safe", True) else None
    if ref:
        # FACTION_ZH 的值形如「Grineer（G系）」，嵌进括号里会套娃，先去括号
        fac_zh = re.sub(r"（.*?）", "", dc.FACTION_ZH.get(ref["faction"], ref["faction"]))
        lines.append(
            f"◆ 参考（{fac_zh}"
            f" {ref['level']}级·身体，无剥甲/异常层数）：单发 {ref['health']:.0f}"
            f"｜暴击期望 ×{ref['crit_exp']:.2f}"
            f" → {ref['health'] * ref['crit_exp']:.0f}"
        )
        hv = ref.get("heavy")
        if hv:
            extra = ""
            if hv.get("crit_cc") is not None:
                extra = f"，暴击 {hv['crit_cc'] * 100:.0f}%"
            if hv.get("initial_combo"):
                # 初始连击是下限，不与连击数相加（wiki Melee；damage_calc 同口径）
                extra += f"，初始连击 {hv['initial_combo']} 为下限"
            lines.append(
                f"　重击（{hv['combo_hits']} 连击{extra}）："
                f"{hv['health']:.0f}｜含暴击 {hv['health_crit']:.0f}"
            )
        tw = ref.get("throw")
        if tw:
            lines.append(
                f"　投掷（连续 {tw['stacks']}/{tw['max_stacks']} 层"
                f"，×{tw['mult']:.1f}）：{tw['health']:.0f}"
                f"｜含暴击 {tw['health_crit']:.0f}"
            )
        _modes = ref.get("modes") or []
        if _modes:
            # 识卡卡面只给摘要：完整各段走「伤害」指令，免得这一页太长
            _top3 = "、".join(f"{m['name']}{m['total']:g}" for m in _modes[:3])
            lines.append(f"　另有 {len(_modes)} 段独立判定：{_top3}…（用「伤害」指令可看每段明细）")

    if panel_rows:
        lines.append("◆ 武器面板")
        lines += panel_rows

    # —— 卡片表：名称｜容量与等级｜效果 ——
    en_pairs: list[str] = []
    if an.get("mods"):
        lines.append(f"◆ 卡片（{len(an['mods'])} 张）")
        lines.append("　卡片　容量与等级　效果")
    for item in an.get("mods") or []:
        if not item.get("found"):
            if item.get("riven"):
                # 紫卡是**合法但无法核算**的卡（词缀随机）—— 单独一行说清楚，
                # 不要混进「库中未收录」里（那会让人以为是我们缺数据）。
                r = item.get("rank")
                lines.append(
                    f"· {item.get('raw')}　　裂罅紫卡（词缀随机，数值无法核算）"
                    + (f"，等级按豆子 {r} 级" if r is not None else "")
                )
            else:
                lines.append(f"· {item.get('raw')}　　⚠ 库中未收录，已忽略")
            continue
        name = (
            item.get("raw") if item.get("name_incomplete") else item.get("zh") or item.get("name")
        )
        en = item.get("name") or ""
        tail = _effect_text(item.get("effect") or {})
        if tail:
            eff = tail
        elif not item.get("calculable"):
            eff = "数值未折算"
        elif item.get("rank") is None:
            eff = "⚠ 等级未定，按满级估"
        else:
            eff = ""
        # 英文名不进表格（名称列过宽会让整张表放弃列对齐）：统一进卡尾「英文名对照」注脚
        if en and en != name:
            en_pairs.append(f"{name} {en}")
        lines.append(f"· {name}　{_rank_text(item)}　{eff}".rstrip("　"))
        if item.get("note"):  # 备注（姿态卡 / 槽位极性匹配…）作为从属行缩进在该卡之下
            lines.append(f"　{item['note']}")

    # —— 面板校验表 ——
    if checks:
        lines.append(f"◆ {check_label}（识别值 vs 本地推算）")
        lines.append("　项目　识别值　本地推算　结果")
        for c in checks:
            got, want = float(c["panel"]), float(c["calc"])
            # 相对差 <0.1% 视为完全吻合（游戏显示舍入造成的小数残差，
            # 如 总伤 1488.3 vs 1488.24 —— 差 0.004%，如实显示相等）
            if c["ok"] and abs(got - want) <= abs(want) * 0.001:
                lines.append(f"· {c['label']}　{got:g}{c['unit']}　{got:g}{c['unit']}　吻合")
            else:
                lines.append(
                    f"· {c['label']}　{got:g}{c['unit']}　{want:.2f}{c['unit']}"
                    + ("　吻合" if c["ok"] else "　⚠ 对不上")
                )
        for c in _tips:
            lines.append(
                f"· {c['label']}　{float(c['panel']):g}　{float(c['calc']):.0f}　{c['note']}"
            )
    if en_pairs:
        lines.append("※ 英文名对照：" + "、".join(en_pairs))
    if bad:
        lines.append("※ ⚠ 可能漏读了卡片，或某张卡的等级推断有误")
    if totals.get("no_rank"):
        lines.append(
            "※ ⚠ 等级无法确定的卡：" + "、".join(totals["no_rank"]) + "（已按满级估算，仅供参考）"
        )
    if totals.get("uncalc"):
        lines.append("※ 以下卡无数值或暂不支持，未计入：" + "、".join(totals["uncalc"]))
    if an.get("unknown"):
        lines.append("※ 库中未收录的名称：" + "、".join(an["unknown"]))
    if an.get("alts"):
        lines.append(
            "※ 近似武器：" + "、".join(v.get("zh") or v.get("name", "") for v in an["alts"])
        )
    for note in an.get("notes") or []:
        lines.append(f"※ {note}")
    for err in an.get("errors") or []:
        lines.append(f"※ ⚠ {err}")

    lines.append(
        "※ 加成一律按卡片实际等级折算（容量数字反推，再用面板数值校验）；"
        "复合元素按 火>冰>电>毒 两两配对"
    )
    return limit_notes(lines)
