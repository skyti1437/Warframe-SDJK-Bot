# -*- coding: utf-8 -*-
"""资料与计算器模块：武器融合 / 对话助手 / 杜卡德。

纯本地计算，不依赖网络。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

DATA_DIR = Path(__file__).resolve().parent / "data"

# ---------------------------------------------------------------------------
# 武器融合：玄骸/信条/科达武器的「效价融合」（Valence Fusion）
#
# 官方公式（wiki.warframe.com / Kuva Weapons · Valence Fusion）：
#     Final = min(floor(1.1 × max(A, B) × 10) / 10, 60)
#     且 Final >= 58% 时**直接进位到 60%**
# 注意不是「两者相加」也不是「高值 + 低值一半」——早期版本按后者算，
# 连同把元素自动合成成辐射的处理，都属于另一套机制（元素 MOD 合成），
# 与效价融合不是一回事。
#
# 元素：**玩家在两者之间二选一**（同元素则无需选），不会合成复合元素。
# ---------------------------------------------------------------------------
VALENCE_ELEMENTS = {
    "电": "electricity", "电击": "electricity",
    "火": "heat", "火焰": "heat",
    "冰": "cold", "冰冻": "cold",
    "毒": "toxin", "毒素": "toxin",
    # Tenet 系还有这三种源于 Progenitor 的元素
    "冲击": "impact", "磁力": "magnetic", "辐射": "radiation",
}
# 内部英文名 -> 展示用的**最短**别名（自动从 VALENCE_ELEMENTS 反推，
# 这样以后往上面加元素不用同步维护两张表）
_ELEM_CN: dict[str, str] = {}
for _alias, _key in VALENCE_ELEMENTS.items():
    if _key not in _ELEM_CN or len(_alias) < len(_ELEM_CN[_key]):
        _ELEM_CN[_key] = _alias
VALENCE_CAP = 60.0

# **展示用**的完整元素名（对应游戏里伤害类型/异常状态的叫法）。
# 效价融合的输入别名仍走上面那张表（短名 电/火/冰/毒 与全名都能输入），
# 这张只用于「元素：磁力　加成：25.7%」这类**只读展示**，
# 所以统一用全名，别把 火/冰/毒 这种短名印在卡面上。
ELEM_ZH: dict[str, str] = {
    "impact": "冲击", "heat": "火焰", "cold": "冰冻", "electricity": "电击",
    "toxin": "毒素", "magnetic": "磁力", "radiation": "辐射",
    "corrosive": "腐蚀", "gas": "毒气", "viral": "病毒", "blast": "爆炸",
    "slash": "切割", "puncture": "穿刺",
}


def valence_bonus(a_pct: float, b_pct: float) -> float:
    """效价融合后的加成百分比。

    Args:
        a_pct: 保留武器的当前加成（25~60）。
        b_pct: 作为材料被消耗的那把的加成。

    Returns:
        融合后的加成百分比。
    """
    top = max(float(a_pct), float(b_pct))
    val = int(top * 1.1 * 10) / 10          # 向下取一位小数
    if val >= 58:                            # 官方：>=58 直接进位到 60
        return VALENCE_CAP
    return min(val, VALENCE_CAP)


def valence_fusion(a_elem: str, a_pct: float, b_elem: str, b_pct: float) -> dict:
    """两把同种玄骸/信条武器融合的结果。

    Args:
        a_elem: A 的元素（支持 电/火/冰/毒/冲击/磁力/辐射）。
        a_pct: A 的加成百分比。
        b_elem: B 的元素。
        b_pct: B 的加成百分比。

    Returns:
        ``{"percent", "options", "note"}``；元素无法识别时返回 ``{"error"}``。
        ``options`` 是可选择的元素列表（元素相同则只有一项）。

    Raises:
        ValueError: 百分比不在 25~60 的合理区间时并未抛出，交由调用方提示。
    """
    a_key = VALENCE_ELEMENTS.get(a_elem, "")
    b_key = VALENCE_ELEMENTS.get(b_elem, "")
    if not a_key or not b_key:
        known = "、".join(sorted({k for k in VALENCE_ELEMENTS if len(k) == 1}))
        return {"error": f"无法识别元素：{a_elem}/{b_elem}（支持 {known}）"}
    pct = valence_bonus(a_pct, b_pct)
    if a_key == b_key:
        note = "同元素，无需选择"
        options = [_ELEM_CN.get(a_key, a_key)]
    else:
        options = [_ELEM_CN.get(a_key, a_key), _ELEM_CN.get(b_key, b_key)]
        note = "两者任选其一，不会合成复合元素"
    return {"percent": pct, "options": options, "note": note,
            "higher_is_a": float(a_pct) >= float(b_pct)}


def fusion_to_cap(start_pct: float) -> list[float]:
    """从 start% 出发，用同数值材料反复融合到 60% 的数值轨迹。

    Args:
        start_pct: 起始加成百分比。

    Returns:
        每一步融合后的百分比列表，最后一项为 60。
    """
    trace: list[float] = []
    cur = float(start_pct)
    while cur < VALENCE_CAP and len(trace) < 12:
        cur = valence_bonus(cur, cur)
        trace.append(cur)
    return trace


# ---------------------------------------------------------------------------
# 对话助手（1999 好感度）：读取数据文件，缺省内置小表
# ---------------------------------------------------------------------------
def load_kim_guide() -> dict:
    path = DATA_DIR / "kim.json"
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {}
    return {}


def kim_advice(character: str) -> Optional[dict]:
    data = load_kim_guide()
    if not data:
        return None
    for key, info in data.items():
        if not isinstance(info, dict):
            continue
        aliases = [key] + [str(a) for a in (info.get("alias") or [])]
        if character and any(character in a or a in character for a in aliases if a):
            return {"name": key, **info}
    return None


# ---------------------------------------------------------------------------
# 杜卡德性价比
# ---------------------------------------------------------------------------
def ducat_per_plat(ducats: int, plat: float) -> Optional[float]:
    if plat <= 0:
        return None
    return round(ducats / plat, 1)
