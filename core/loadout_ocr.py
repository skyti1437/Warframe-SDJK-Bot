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

反推结果再用面板数值（暴击率 / 暴击伤害 / 触发率 / 总伤害）**交叉校验** ——
这两条路径互相独立，一致才敢下结论。

实测（2026-09-16，用户提供的 Xoris「驱魔之刃」配卡截图）：
9 张卡全部识别成功；drain 反推等级 8/8 命中；面板四项校验全部吻合。
"""
from __future__ import annotations

import difflib
import json
import math
import re
from pathlib import Path
from typing import Optional

from . import damage_calc as dc

_DATA = Path(__file__).resolve().parent / "data"
_payload: dict = {}

# 物理三类型的中文名（面板行里用的就是这三个词）
PHYS_ZH = {"impact": "冲击", "puncture": "穿刺", "slash": "切割"}
# 单元素 -> 中文
ELEM_ZH = {"heat": "火", "cold": "冰", "electricity": "电", "toxin": "毒"}
# 全部元素（含复合）的中文：卡面里统一用这一套，避免「火」和「火焰」混着显示
ELEM_ALL_ZH = dict(ELEM_ZH, **{"blast": "爆炸", "corrosive": "腐蚀", "viral": "病毒",
                              "magnetic": "磁力", "radiation": "辐射", "gas": "毒气"})
# 武器类别中文化
CATEGORY_ZH = {"Primary": "主武器", "Secondary": "副武器", "Melee": "近战"}
# 游戏里复合元素的合成顺序（火 > 冰 > 电 > 毒，两两配对）
ELEM_PAIRS = {frozenset({"heat", "cold"}): "blast",
              frozenset({"heat", "electricity"}): "radiation",
              frozenset({"heat", "toxin"}): "gas",
              frozenset({"cold", "electricity"}): "magnetic",
              frozenset({"cold", "toxin"}): "viral",
              frozenset({"electricity", "toxin"}): "corrosive"}


VISION_PROMPT = """这是 Warframe（星际战甲）游戏内武器升级界面的截图。
请**只读出图中确实可见的文字与数字**，不要推测、不要补全、不要翻译。

严格输出如下 JSON（不要 markdown 代码块、不要任何解释文字）：
{
  "weapon": "顶部武器名，原文照抄（含方括号里的等级数字，如 驱魔之刃 [30]）",
  "capacity": "容量那一行右侧的数字，形如 0/70",
    "mods": [
    {"name": "卡片上的 MOD 名称，原文照抄（图上中文就用中文）",
     "drain": 卡片右上角的数字（只要整数）,
     "color": "这个数字的颜色：绿=匹配减半 / 红=极性不合+25% / 白=无加成，看不清写 ?",
     "polarity": "卡片左上角的极性符号，只可能是 V / D / — / Y / = 之一，看不清写 ?"}
  ],
  "panel": {
    "attack_speed": "攻击速度行的数字",
    "crit_chance": "暴击几率行的数字（含百分号）",
    "crit_damage": "暴击伤害行的数字（含「倍」）",
    "status_chance": "触发几率行的数字（含百分号）",
    "damage_rows": [["伤害类型名", "数字", "第二个数字或 null"]],
    "total": ["总计左值", "总计右值"],
    "heavy": ["重击左值", "重击右值"]
  }
}

要求：
1. mods 必须列出**全部卡片**，包括最上方那张最大的姿态卡；
2. drain 只填整数，不要带箭头、百分号、上标等符号；
3. **damage_rows 是重点，一条都不能漏**：左侧「伤害」栏里每一行都要给 ——
   冲击 / 穿刺 / 切割 / 各元素行（如 毒素、爆炸）/ 总计。
   ⚠ **合成元素行**（形如「爆炸（火+冰）144」「腐蚀（电+毒）」「病毒（冰+毒）」
   「磁力」「辐射」「毒气」）**必须读出来**：括号里是合成它的两种基础元素，
   前面的数值（如 144）要照抄 —— 这一行最常被整行漏掉。
   数字里的千分位逗号
   （如 1,807.3）原样保留；若某行是「旧值>新值」形式就两个都给。
   行名必须与数值严格对齐（不要串行）；「总计」是独立的一行，它的数值
   约等于上面所有行之和 —— 不要把总计的数值写到别的行名下，也不要
   凭空新增图中没有的行（图里没有「爆炸」就不要输出爆炸行）。
   这些行用于灵化形态判定，缺失或错位会导致整张卡算错；
4. 看不清的内容填 null，不要猜。
5. 面板数值块在「伤害」栏**上方**，从上到下：暴击几率 / 暴击伤害 /
   触发几率 —— 每行都是「基础值 ▶ MOD后值」两个数（红色左值、绿色右值）。
   **一律取 ▶ 右边的 MOD 后值**（如「22% ▶ 138%」要填 138%，不是 22%）；
   暴击伤害的「倍」字保留。暴击几率常带小数（如 75.6%），触发几率是
   个位数十位数的小百分数（如 10%），两行别看串。
6. 容量数字的 color 只看**数字本身**的颜色（绿/红/白）—— 数字旁边
   的极性符号（V/—/D 等）自带的颜色不是数字颜色，别搞混。
"""


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


def clean_weapon_name(s) -> str:
    """把「+ 升级 / 驱魔之刃 [30]」清成「驱魔之刃」。"""
    t = str(s or "").strip()
    if "/" in t:                       # 「升级 / 武器名」的固定格式
        t = t.split("/")[-1]
    t = re.sub(r"^[+＋\-—=\s]*升级\s*[:：]?\s*", "", t)  # 「升级:武器名」
    t = re.sub(r"[（(\[]\s*\d+\s*[)）\]]", "", t)   # 去掉 [30] / (30)
    t = re.sub(r"等级\s*\d+\s*$", "", t)            # 「等级 30」后缀（新版 UI）
    t = re.sub(r"^[+＋\-—=\s]+", "", t)
    return t.strip(" +·|/").strip()


def parse_vision_json(text: str) -> dict:
    """从模型输出里抠出 JSON（容忍思考段、markdown 代码块与前后废话）。

    注意 thinking 类模型（glm-4.1v-thinking-flash）会先吐一段 ``<think>…</think>``，
    里面可能含花括号，必须先剥掉再取 JSON。
    """
    s = str(text or "").strip()
    s = re.sub(r"<think(?:ing)?>.*?</think(?:ing)?>", "", s, flags=re.S | re.I)
    s = re.sub(r"</?think(?:ing)?>", "", s, flags=re.I)
    m = re.search(r"```(?:json)?\s*(.+?)```", s, flags=re.S)
    if m:
        s = m.group(1).strip()
    i, j = s.find("{"), s.rfind("}")
    if i >= 0 and j > i:
        s = s[i:j + 1]
    for candidate in (s, re.sub(r",\s*([}\]])", r"\1", s)):
        try:
            d = json.loads(candidate)
            if isinstance(d, dict):
                return d
        except Exception:  # noqa: BLE001
            continue
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


def infer_rank(mod: dict, drain: Optional[int],
               color: Optional[str] = None) -> tuple[Optional[int], str]:
    """用卡片上的容量数字反推实际等级。

    三态（wiki /w/Polarity 原文规则）：
    * 槽位极性匹配：`drain = ceil((base_drain + rank)/2)` —— 数字显示**绿色**
    * 槽位极性不合：`drain = round((base_drain + rank) × 1.25)` —— 数字显示**红色**
    * 无加成（槽位无极性等）：`drain = base_drain + rank` —— 数字显示白色

    两支以上都解出合法等级时（真歧义），**取高者** —— 端局配卡普遍
    「极化 + 满级」，低等级假设几乎总是错的（2026-09-16 用户实测：
    整卡全满级却被推成 2~5 级）；万一真错，面板校验会亮 ⚠ 并由
    识别-校验闭环重试。模型读出的数字颜色（color）优先于该策略。
    """
    base = mod.get("base_drain")
    max_rank = mod.get("max_rank")
    if drain is None:
        return None, "没读到卡片容量数字"
    if base is None or max_rank is None:
        return None, "库中缺少该卡容量元数据"
    base = int(base)
    max_rank = int(max_rank)
    if base < 0:                      # 姿态卡在原始数据里是 -2，不参与折算
        return None, "姿态卡（不参与伤害折算）"
    hint = _color_hint(color)
    cands: list[tuple[int, str]] = []
    r_unmatched = drain - base        # ① 无加成
    if 0 <= r_unmatched <= max_rank:
        cands.append((r_unmatched, ""))
    lo, hi = 2 * drain - 1 - base, 2 * drain - base    # ② 匹配减半（含进位）
    if hi >= 0 and lo <= max_rank:
        # 区间 [lo, hi] 与 [0, max_rank] 交集内**取最高**（满级假设）
        r_matched = min(hi, max_rank)
        # 复核：取该等级时显示值必须等于 drain（ceil 语义下必然成立）
        if math.ceil((base + r_matched) / 2) == drain:
            cands.append((r_matched, "槽位极性匹配（容量减半）"))
    r0 = int(drain / 1.25 - base)     # ③ 极性不合 +25%（四舍五入）
    for r in (r0 - 1, r0, r0 + 1):
        if 0 <= r <= max_rank and int((base + r) * 1.25 + 0.5) == drain:
            cands.append((r, "极性不合（容量 +25%）"))
            break
    if not cands:
        return None, f"容量 {drain} 与库中基准 {base} 对不上，等级无法确定"
    # ⚠️ 模型读的颜色不可靠（2026-09-16 拉特昂实测：极性图标的红色被当成
    # 数字颜色，绿5满级被解成红0级；射速面板 4.17▶3.33 铁证满级）。
    # 颜色只做卡面标注，**选等级一律取所有分支的最高候选**（满级假设）。
    # 真歧义时低等级假设在端局配卡几乎总是错的；万一真错，射速/触发/
    # 总伤的面板校验会亮 ⚠ 并由识别-校验闭环重试。
    return max(cands, key=lambda t: t[0])


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

    数据源：wfsim（github.com/magenie33/wfsim）data/weapons/*_incarnon.yaml，
    只取事实数值重录（生成脚本 scripts/build_incarnon_forms.py）。
    近战灵化 wfsim 尚未导入 → 近战仍走面板反推。
    """
    global _incarnon_forms_cache
    if _incarnon_forms_cache is None:
        try:
            _incarnon_forms_cache = json.loads(
                (_DATA / "incarnon_forms.json").read_text(encoding="utf-8"))
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
            for week in ((r.get("incarnon") or {}).get("weeks") or []):
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
            d = json.loads(
                (_DATA / "lich_valence.json").read_text(encoding="utf-8"))
            _lich_keys_cache = {k for k in d if k != "_meta"}
        except Exception:  # noqa: BLE001
            _lich_keys_cache = set()
    if not _lich_keys_cache:
        return False
    key = re.sub(r"[\s'’]+", "_", str(weapon.get("name") or "").lower())
    return key in _lich_keys_cache


def _panel_base_weapon(weapon: dict, panel: dict,
                       totals: dict) -> tuple[Optional[dict], str]:
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
    for row in panel.get("damage_rows") or []:
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            continue
        label = str(row[0]).lstrip("*").strip()
        key = next((k for zh, k in rev.items() if label.startswith(zh)), None)
        if not key:
            continue
        left, right = to_pair(row[1])
        if not left:
            continue
        if key in ("impact", "puncture", "slash"):
            _pm = totals.get("physical") or {}
            pm = (float(_pm.get(key) or 0.0)
                  if isinstance(_pm, dict) else 0.0)
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
        float(lib_dmg.get(k) or 0.0) for k in ("impact", "puncture", "slash"))
    if base_dmg:
        ips_total = sum(v for k, v in base_dmg.items()
                        if k in ("impact", "puncture", "slash"))
        # 总伤锚定：某行值≈面板总伤（可能是被模型挂错行名的「总计」）时，
        # 不许它进自带元素反推 —— 否则基础总伤直接翻倍（实测踩过）
        pan_total = _panel_total(panel)
        # 元素行 → 自带元素基础（灵化形态普遍改自带元素，如执法者灵化的毒素）。
        # 我们引擎的语义：元素 MOD 加成 = 元素%×MOD后总基础（含自带元素），即
        #   行值 = after_base×(E0 + pct×(IPS总+E0))
        # → E0 = (行值/after_base − pct×IPS总) / (1+pct)
        # 行值 ≤ 纯 MOD 贡献时视为没有自带元素（防 OCR 噪声算出负数）
        composed = dc.compose_elements(dict(totals.get("elements") or {}))
        for key, row_v in elem_rows:
            if pan_total and abs(row_v - pan_total) / pan_total < 0.02:
                continue          # 这是「总计」行（行名被模型挂错了），跳过
            pct = float(composed.get(key) or 0.0) / 100.0
            e0 = (row_v / bd_m - pct * ips_total) / (1 + pct)
            # 幻影自带元素防线：游戏面板行值与「纯 MOD 解释值」有未建模的
            # 小偏差（实测拉特昂 Prime 面板各行有 ~+6.7% 的显示系数），
            # 逆解后会得到占总量百分之几的幽灵自带元素 → 基础总伤虚高、
            # 总伤校验 ⚠。MOD 已能解释行值 90% 以上时视为无自带元素。
            # （真实自带元素远大于此：执法者灵化毒素占行值 ~24%）
            explained = bd_m * pct * ips_total          # 纯 MOD 贡献
            if pct > 0 and row_v - explained < 0.10 * row_v:
                continue
            # 疑似总计行兜底②：无该元素 MOD、库内基础也没有它、而反推出的
            # 自带值却 ≥ IPS 总量的 2 倍 —— 真实自带元素极少这么大（执法者
            # 灵化毒素才 1.65×IPS），大概率是「总计」被挂错了行名（实测踩过）
            if (pct == 0.0 and not float(lib_dmg.get(key) or 0.0)
                    and ips_total > 0 and e0 >= 2.0 * ips_total):
                continue
            if e0 > 0:
                base_dmg[key] = e0
    # wfsim 灵化形态数值（有就是权威判定 + 更准的兜底向量）
    inc = _incarnon_forms().get(
        str(weapon.get("uniqueName") or "").lower())
    inc_dmg = (inc or {}).get("damage") or {}
    inc_total = float(inc_dmg.get("total") or 0.0)

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
            if inc_total > 0 and (not lib_total
                                  or abs(est - inc_total) < abs(est - lib_total)):
                ref_dmg, ref_total = inc_dmg, inc_total
            base_dmg = {k: float(ref_dmg.get(k) or 0.0) * est / ref_total
                        for k in ("impact", "puncture", "slash")
                        if float(ref_dmg.get(k) or 0.0) > 0}
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
    diffs = [abs(base_total - lib_total) / max(lib_total, 1.0)]
    if cc_p is not None:
        # ⚠️ 面板是百分数、库里是小数：反推后先 /100 再比，否则必然误触发覆盖
        diffs.append(abs(cc_p / cc_m / 100.0
                         - float(weapon.get("criticalChance") or 0.0))
                     / max(float(weapon.get("criticalChance") or 0.0), 0.01))
    if cm_p is not None:
        diffs.append(abs(cm_p / cm_m - float(weapon.get("criticalMultiplier") or 1.0))
                     / max(float(weapon.get("criticalMultiplier") or 1.0), 0.01))
    if max(diffs, default=0.0) <= 0.20:
        return None, ""                # 与库内基础一致（未灵化），不需要覆盖

    # ---- 灵化库命中：面板反推值与灵化基础吻合 → 直接采用灵化基础 ----
    # （基础是真实库数据，MOD 正常叠算，面板校验也保持开启；
    #   反推值里吸收的灵化进化 perk 平坦加成会导致小偏差，20% 容差内即可）
    if inc_total > 0:
        diffs_inc = [abs(base_total - inc_total) / max(inc_total, 1.0)]
        if cc_p is not None:
            diffs_inc.append(abs(cc_p / cc_m / 100.0
                                 - float(inc.get("criticalChance") or 0.0))
                             / max(float(inc.get("criticalChance") or 0.0), 0.01))
        if cm_p is not None:
            diffs_inc.append(abs(cm_p / cm_m
                                 - float(inc.get("criticalMultiplier") or 1.0))
                             / max(float(inc.get("criticalMultiplier") or 1.0), 0.01))
        if max(diffs_inc, default=0.0) <= 0.20:
            w2 = dict(weapon)
            w2["damage"] = {**{k: v for k, v in inc_dmg.items() if k != "total"},
                            "total": inc_total}
            w2["criticalChance"] = float(inc.get("criticalChance") or 0.0)
            w2["criticalMultiplier"] = float(inc.get("criticalMultiplier") or 1.0)
            w2["procChance"] = float(inc.get("procChance") or 0.0)
            if float(inc.get("fireRate") or 0.0) > 0:
                w2["fireRate"] = float(inc["fireRate"])
            return w2, ("灵化形态：基础数值取自灵化库（wfsim 数据源），"
                        "MOD 正常叠算，面板校验保持开启")

    w2 = dict(weapon)
    w2["damage"] = {**base_dmg, "total": base_total}
    if cc_p is not None:
        w2["criticalChance"] = round(cc_p / cc_m / 100.0, 4)   # 面板百分数→库小数
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
    names = _incarnon_names()
    is_inc = (inc_total > 0
              or (weapon.get("name") or "").lower() in names
              or (weapon.get("zh") or "") in names)
    is_lich = _is_lich_weapon(weapon)
    if is_lich:
        # 赤毒/信条/终幕：回响加成 25-60% 连续随机、只能从面板反推，
        # 反推基础天然已含 —— 标注提醒实际值以游戏内为准
        return w2, ("灵化/回响武器：基础数据从面板反推（含回响加成与进化加成，"
                    "回响为 25-60% 随机值，实际以游戏内为准），"
                    "MOD 按实际等级正常叠算一次")
    return w2, ("灵化形态：基础数据从面板反推（含自带元素与灵化进化加成），"
                "MOD 按实际等级正常叠算一次"
                if is_inc else
                "特殊形态：基础数据从面板反推，MOD 按实际等级正常叠算一次")


# ---------------------------------------------------------------------------
# 主分析
# ---------------------------------------------------------------------------
_ADD_FIELDS = ("base_dmg", "multishot", "crit_chance", "crit_dmg", "fire_rate",
               "status_chance", "status_dmg", "heavy_dmg", "faction_dmg",
               "headshot_bonus", "initial_combo", "punch_through",
               "crit_chance_heavy", "throw_dmg",
               # v1.9 新机制（漏了就会像「急进猛突 rank3」那样什么都不显示）
               "crit_per_combo", "status_per_combo", "dmg_per_status")


def analyze(ocr: dict) -> dict:
    """把视觉识别结果转成结构化分析（不依赖 AstrBot，可离线测试）。"""
    out: dict = {
        "ok": False, "weapon": None, "weapon_query": "", "alts": [],
        "raw_mods": ocr.get("mods") or [], "mods": [], "unknown": [],
        "totals": {}, "panel": dict(ocr.get("panel") or {}),
        "panel_base": "",
        "checks": [], "notes": [], "errors": [],
        "capacity": str(ocr.get("capacity") or "").strip(),
    }

    # ---- 伤害行归一：三段式 [标签, 基础, 最终] → [标签, "基础>最终"] ----
    # 模型常把「9 ▸ 25.4」读成 ["冲击", "9", "25.4"] 三个元素，旧解析只吃
    # row[1]（基础）会把最终值丢掉，总伤校验也随之失真
    _panel = out["panel"]
    _fixed = []
    for _r in _panel.get("damage_rows") or []:
        if (isinstance(_r, (list, tuple)) and len(_r) >= 3
                and to_float(_r[2]) is not None
                and (to_float(_r[1]) is None
                     or to_pair(_r[1])[1] is None)):
            _fixed.append([str(_r[0]), f"{_r[1]}>{_r[2]}"])
        elif isinstance(_r, (list, tuple)):
            _fixed.append(list(_r[:2]) if len(_r) > 2 else list(_r))
        else:
            _fixed.append(_r)
    if _fixed:
        _panel["damage_rows"] = _fixed
    _tot = _panel.get("total")
    if isinstance(_tot, (list, tuple)) and len(_tot) >= 3 \
            and to_float(_tot[2]) is not None:
        _panel["total"] = [str(_tot[0]), f"{_tot[1]}>{_tot[2]}"]

    # ---- 武器 ----
    out["weapon_query"] = clean_weapon_name(ocr.get("weapon"))
    if out["weapon_query"]:
        weapon, alts = dc.find_weapon(out["weapon_query"])
        out["weapon"], out["alts"] = weapon, alts
        if weapon is None:
            out["errors"].append(f"武器库里没找到「{out['weapon_query']}」")
    else:
        out["errors"].append("没读到武器名")

    # ---- 逐卡识别 ----
    totals: dict = {k: 0.0 for k in _ADD_FIELDS}
    totals.update({"elements": {}, "physical": {}, "uncalc": [], "no_rank": [],
                   "throw_max_stacks": 0})
    for raw in out["raw_mods"]:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "").strip()
        drain = to_int(raw.get("drain"))
        rec, how = match_mod(name)
        item: dict = {"raw": name, "drain": drain, "how": how,
                      "found": rec is not None, "rank": None, "note": "",
                      "effect": {}, "calculable": False}
        if rec:
            item.update({
                "name": rec.get("name"), "zh": rec.get("zh"),
                "compat": rec.get("compat"), "max_rank": rec.get("max_rank"),
                "base_drain": rec.get("base_drain"),
                "polarity": rec.get("polarity"),
                "calculable": bool(rec.get("calculable")),
            })
            rank, why = infer_rank(rec, drain,
                                    raw.get("color") or raw.get("drain_color"))
            item["rank"], item["note"] = rank, why
            eff = effect_at(rec, rank) if rec.get("calculable") else {}
            item["effect"] = eff
            if rank is None and rec.get("calculable"):
                totals["no_rank"].append(item.get("zh") or name)
            if not eff:
                totals["uncalc"].append(item.get("zh") or name)
            for key in _ADD_FIELDS:
                if eff.get(key):
                    totals[key] += float(eff[key])
            # 斩铁：「重击时 x2」→ 重击的暴击几率加成额外再吃一份
            _hm = float(eff.get("heavy_crit_mult") or 1.0)
            if _hm > 1.0:
                totals["crit_chance_heavy"] += (float(eff.get("crit_chance") or 0.0)
                                                * (_hm - 1.0))
            if eff.get("throw_max_stacks"):
                totals["throw_max_stacks"] = max(int(totals["throw_max_stacks"]),
                                                 int(eff["throw_max_stacks"]))
            for el, val in (eff.get("elements") or {}).items():
                totals["elements"][el] = totals["elements"].get(el, 0.0) + float(val)
            for el, val in (eff.get("physical") or {}).items():
                totals["physical"][el] = totals["physical"].get(el, 0.0) + float(val)
        else:
            out["unknown"].append(name)
        out["mods"].append(item)

    # ---- 家族互斥（wfsim family）：同族两张游戏里只能装一张 ----
    fam_seen: dict[str, list] = {}
    for item in out["mods"]:
        if not item.get("found"):
            continue
        fam = dc.mod_extra(item).get("family")
        if fam:
            fam_seen.setdefault(fam, []).append(
                str(item.get("zh") or item.get("raw") or ""))
    for fam, zs in fam_seen.items():
        if len(zs) >= 2:
            out["notes"].append(
                f"⚠️ 互斥家族 {fam}：{'、'.join(zs)} —— 游戏里只能装一张，"
                "若截图确实如此请人工核对（加成可能重复计了）")

    # ---- 镀层类条件堆叠：默认不计入，但告诉用户可以显式带入 ----
    _COND_ZH = {"condition_overload": "每异常种类直伤", "multishot": "多重",
                "crit_chance": "暴击率", "crit_damage": "暴伤",
                "damage": "直伤", "base_damage": "基伤", "fire_rate": "射速",
                "status_chance": "触发率", "status_damage": "状态伤害",
                "punch_through": "穿透"}
    hints: list[str] = []
    for item in out["mods"]:
        if not item.get("found") or not item.get("calculable"):
            continue
        for c in dc.mod_extra(item).get("conditionals") or []:
            if not c.get("mappable"):
                continue
            hints.append(f"{item.get('zh') or item.get('raw')}"
                         f"（{_COND_ZH.get(c.get('grants'), c.get('grants'))}"
                         f"+{float(c.get('value') or 0):g}%×{c.get('max_stacks')}层）")
    if hints:
        out["notes"].append(
            "💡 条件堆叠默认未计入，伤害指令加「满镀层」或「镀层N」带入："
            + "、".join(hints[:4]) + ("…" if len(hints) > 4 else ""))

    # ---- 灵化 / 特殊形态：面板与库内基础差得多 → 从面板反推基础数据 ----
    if out["weapon"] is not None:
        _lib_stats = {
            "cc": float(out["weapon"].get("criticalChance") or 0.0),
            "sc": float(out["weapon"].get("procChance") or 0.0),
        }
        _w2, _pnote = _panel_base_weapon(out["weapon"], out["panel"], totals)
        if _w2 is not None:
            out["weapon"], out["panel_base"] = _w2, _pnote
            # 反推基础 vs 库内基础的合理性：面板的暴击/触发两行被模型
            # 读串（75.6%↔10%）时，反推值会偏离库内基础 3 倍以上 → 亮 ⚠
            _w = out["weapon"]
            for _k, _lab, _libv in (
                    ("criticalChance", "暴击几率", _lib_stats["cc"]),
                    ("procChance", "触发几率", _lib_stats["sc"])):
                _v = float(_w.get(_k) or 0.0)
                if _libv > 0.01 and (_v >= _libv * 3 or _v <= _libv / 3):
                    out["notes"].append(
                        f"⚠ 反推基础{_lab} {_v * 100:g}% 与库内基础 "
                        f"{_libv * 100:g}% 差 3 倍以上 —— 面板暴击/触发两行"
                        "疑似读串，建议重发截图")

    # ---- 复合元素合成（火>冰>电>毒 两两配对）----
    leftovers = dict(totals["elements"])
    combined: dict[str, float] = {}
    for pair, res in ELEM_PAIRS.items():
        names = sorted(pair, key=lambda e: ("heat", "cold", "electricity",
                                            "toxin").index(e))
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
    _spec_p = to_damage_spec(out)
    out["calc_panel"] = _calc_panel(out.get("weapon"), totals)
    if out.get("weapon"):
        _pt, _, _, _ = dc._hit_from_damage(
            out["weapon"]["damage"],
            float(out["weapon"]["damage"].get("total") or 0.0),
            _spec_p, dc._load().get("fac_table") or {}, "__panel__",
            dr=0.0, viral_mult=1.0, mag_mult=1.0)
        out["calc_panel"]["total"] = sum(_pt.values())
    # 条件暴击卡（镀层瞄具族）：游戏面板暴击率显示似乎含条件加成 → 跳过该项校验
    _cond_crit = any("critical" in json.dumps(m.get("effect") or {}, default=str).lower()
                     for m in out.get("mods") or [])
    out["checks"] = _panel_checks(out.get("weapon"), totals, out["panel"],
                                  calc=out.get("calc_panel"),
                                  cond_crit=_cond_crit)
    if (out.get("weapon") and (out["panel"].get("damage_rows")
                               and not _panel_total(out["panel"]))):
        out["notes"].append("⚠ 面板总伤没读到，伤害行无法交叉验证 —— "
                            "数字可能有误，请自行核对")

    out["ok"] = bool(out.get("weapon")) and bool(out["mods"])

    # 识别完整时顺手跑一版参考伤害（默认 G系 100 级·身体），卡面直接给出，
    # 省得用户再去手打一遍「伤害」指令
    if out["ok"]:
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
    res["base_total"] = sum(float(dmg.get(k) or 0.0)
                            for k in ("impact", "puncture", "slash"))
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
        if str(r[0]).lstrip("*").strip() in ("总计", "总伤害", "总"):
            panel["total"] = [str(r[0]), str(r[1])]
            break
    return out


def _panel_total(panel: dict) -> Optional[float]:
    """面板总伤害：① 「总计」标签行 → ② total 字段 → ③ 值≈其余行之和的行。

    ③ 是防模型幻觉的兜底：实测它会把「总计 1807.3」挂到「爆炸」行名下
    （2026-09-16 执法者截图），这种行不该再被当成自带元素反推。
    """
    rows = [r for r in (panel.get("damage_rows") or [])
            if isinstance(r, (list, tuple)) and len(r) >= 2]
    finals: list[float] = []
    for r in rows:
        if str(r[0]).lstrip("*").strip() in ("总计", "总伤害", "总"):
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


def _panel_checks(weapon: Optional[dict], totals: dict, panel: dict,
                  calc: Optional[dict] = None,
                  cond_crit: bool = False) -> list[dict]:
    """识别到的面板值 vs 本地推算值（两条独立路径互验）。

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

    def add(label: str, got: Optional[float], want: Optional[float],
            unit: str = "", tol: float = 0.04) -> None:
        if got is None or want in (None, 0):
            return
        ok = abs(got - want) <= max(abs(want) * tol, 1e-6)
        checks.append({"label": label, "panel": got, "calc": want,
                       "unit": unit, "ok": ok})

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
            s = 0.0
            n_rows = 0
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
                vals = [x for x in row[1:]
                        if x is not None and str(x).strip()]
                _l, _r = to_pair(vals[-1] if vals else None)
                v = _r if _r else _l
                if v:
                    s += float(v)
                    n_rows += 1
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
                _phys = sum(float(v) for v in (calc.get("physical") or {}).values())
                _elem = float(calc.get("element_damage") or 0.0)
                _full = s
                if _elem > 0.5 and abs(s - _phys) <= max(_phys * 0.05, 0.5):
                    _full = s + _elem
                add("行和×多重", tot_v, _full * ms, "", tol=0.02)
                if not any(c["label"] == "行和×多重" and c["ok"] for c in checks):
                    # 面板总计与行和不符 → 记一条明示，别让用户以为是漏卡
                    checks.append({
                        "label": "总伤害行", "panel": tot_v, "calc": s * ms,
                        "unit": "", "ok": True,
                        "note": "面板总计疑似读串，数值以行和推导为准"})
    return checks


# ---------------------------------------------------------------------------
# 卡面
# ---------------------------------------------------------------------------
def _rank_text(item: dict) -> str:
    """MOD 条目右侧的「容量 → 等级」描述。

    ★ `容量0?` 的含义（2026-09-20 用户反馈「出现了个容量 0」后加）：
    库里**确有** base_drain=0 的卡（survival instinct / transfusion），
    它们**能正常反推出等级** → 会显示成 `容量0→0/3`。所以
    「drain 读到 0 **且** rank 反推不出来」基本只可能是**误读**（把该位置读成了 0）。
    这时标成 `容量0?` 而不是 `容量0`，避免让人以为该卡容量真是 0；
    等级未知的 MOD 其效果按满级计入（见 effect_at），不会算崩。
    """
    drain = item.get("drain")
    rank, max_rank = item.get("rank"), item.get("max_rank")
    if drain == 0 and rank is None:
        head = "容量0?"
    else:
        head = f"容量{int(drain)}" if drain is not None else "容量?"
    if rank is None:
        return head
    if max_rank:
        # ⚠️ 别用 ✔：卡片字体没有这个字形，渲染出来会凭空消失（实测）
        full = "满级" if rank >= int(max_rank) else "★非满级"
        return f"{head}→{rank}/{int(max_rank)}{full}"
    return f"{head}→{rank}级"


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
        bits.append(f"连续投掷伤害 +{eff['throw_dmg']:g}%/层"
                    + (f"（最多 {_st} 层）" if _st else ""))
    for key, label in (("base_dmg", "基伤"), ("heavy_dmg", "重击伤害"),
                       ("multishot", "多重"), ("crit_chance", "暴击率"),
                       ("crit_dmg", "暴伤"), ("status_chance", "触发率"),
                       ("status_dmg", "状态伤害"), ("fire_rate", "射速"),
                       ("faction_dmg", "派系"), ("headshot_bonus", "爆头倍率"),
                       ("punch_through", "穿透"), ("initial_combo", "初始连击"),
                       ("crit_per_combo", "暴击率/连击倍率"),
                       ("status_per_combo", "触发率/连击倍率"),
                       ("dmg_per_status", "基伤/异常种类")):
        val = eff.get(key)
        if val:
            unit = "" if key in ("punch_through", "initial_combo") else "%"
            bits.append(f"{label}+{val:g}{unit}")
    for el, val in (eff.get("elements") or {}).items():
        bits.append(f"{ELEM_ALL_ZH.get(el, el)}+{val:g}%")
    for el, val in (eff.get("physical") or {}).items():
        bits.append(f"{PHYS_ZH.get(el, el)}+{val:g}%")
    if eff.get("faction_mul"):
        bits.append(f"派系×{eff['faction_mul']:g}")
    return "、".join(bits)


def to_damage_spec(an: dict, level: int = 100,
                   faction: str = "Grineer") -> Optional[dict]:
    """把识别到的配卡折成伤害计算器的 spec（便于直接复算）。

    注意：**先用 `parse_args([])` 拿一份默认 spec 再覆盖**，不要手写字段 ——
    spec 有二十多个键（病毒层数、剥甲、超宏…），手写漏一个 calculate 就 KeyError。
    """
    if not an.get("weapon"):
        return None
    spec, _ = dc.parse_args([])
    t = an.get("totals") or {}
    spec["form"] = "keep"     # 面板已从截图反推（可能就是灵化形态）→ 不再套形态数据
    spec.update({
        "level": int(level), "faction": faction,
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
    })
    # 反推路径的基础是「把 MOD 除回去」的净基础（v1.11 修正：此前误把 MOD
    # 再零化一次，导致反推路径 MOD 一次都没算、参考伤害只有面板的 ~1/18）。
    # 正确语义 = 净基础 + MOD 正常叠算一次 —— 与灵化库直采路径完全一致，
    # 这里不再对 spec 做任何零化。
    if t.get("heavy_dmg") or t.get("initial_combo"):
        spec["heavy"] = True          # 配了重击类 MOD 就顺便给重击数据
    return spec


def card_lines(an: dict) -> list[str]:
    """识别结果卡面。"""
    lines: list[str] = []
    w = an.get("weapon")
    if w:
        dmg = w.get("damage") or {}
        parts = "、".join(f"{PHYS_ZH[k]}{float(dmg.get(k) or 0):g}"
                          for k in ("impact", "puncture", "slash")
                          if float(dmg.get(k) or 0) > 0)
        cat = CATEGORY_ZH.get(w.get("category") or "", w.get("category") or "")
        lines.append(f"◆ 武器：{w.get('zh') or w['name']}（{w['name']}）"
                     f"｜{cat}｜MR{w.get('masteryReq', 0)}")
        if an.get("panel_base"):
            lines.append(f"　{an['panel_base']}")
        lines.append(f"　基础 {parts}（计 {float(dmg.get('total') or 0):g}）"
                     f"｜暴击 {float(w.get('criticalChance') or 0) * 100:g}%"
                     f"×{float(w.get('criticalMultiplier') or 1):g}"
                     f"｜触发 {float(w.get('procChance') or 0) * 100:g}%")
    else:
        lines.append(f"◆ 武器：未能识别（读到的是「{an.get('weapon_query') or '空'}」）")

    cap = f"｜容量 {an['capacity']}" if an.get("capacity") else ""
    lines.append(f"◆ 识别到 {len(an.get('mods') or [])} 张卡{cap}")
    for item in an.get("mods") or []:
        if not item.get("found"):
            lines.append(f"　· {item.get('raw')}：⚠ 库中未收录，已忽略")
            continue
        name = item.get("zh") or item.get("name")
        en = item.get("name") or ""
        tail = _effect_text(item.get("effect") or {})
        line = f"　· {name}（{en}）{_rank_text(item)}"
        if tail:
            line += f" → {tail}"
        elif not item.get("calculable"):
            line += " → 无数值（不影响伤害）"
        elif item.get("rank") is None:
            line += " → ⚠ 等级未定，按满级估"
        if item.get("note"):
            line += f"；{item['note']}"
        lines.append(line)

    totals = an.get("totals") or {}
    bits: list[str] = []
    for key, label in (("base_dmg", "基伤"), ("heavy_dmg", "重击伤害"),
                       ("multishot", "多重"), ("crit_chance", "暴击率"),
                       ("crit_dmg", "暴伤"), ("status_chance", "触发率"),
                       ("status_dmg", "状态伤害"), ("fire_rate", "射速")):
        if totals.get(key):
            bits.append(f"{label}+{totals[key]:g}%")
    if totals.get("crit_chance_heavy"):
        bits.append(f"重击时暴击率再+{totals['crit_chance_heavy']:g}%")
    if totals.get("throw_dmg"):
        _st = totals.get("throw_max_stacks")
        bits.append(f"投掷伤害+{totals['throw_dmg']:g}%/层"
                    + (f"（最多{int(_st)}层）" if _st else ""))
    for el, val in (totals.get("elements") or {}).items():
        bits.append(f"{ELEM_ALL_ZH.get(el, el)}+{val:g}%")
    for el, val in (totals.get("physical") or {}).items():
        bits.append(f"{PHYS_ZH.get(el, el)}+{val:g}%")
    if bits:
        wrapped = _wrap_bits(bits)
        lines.append("◆ 按实际等级折算的加成：" + wrapped[0])
        for extra in wrapped[1:]:
            lines.append("　" + extra)
    combined = totals.get("combined") or {}
    if combined:
        lines.append("◆ 合成后元素：" + "、".join(
            f"{ELEM_ALL_ZH.get(k, k)}+{v:g}%" for k, v in combined.items()))

    checks = an.get("checks") or []
    _tips = [c for c in checks if c.get("note")] if checks else []
    if checks:
        # 带 note 的项是「说明性提示」（如面板总计读串），不参与 ✓/⚠ 对比
        checks = [c for c in checks if not c.get("note")]
    if checks:
        bad = [c for c in checks if not c["ok"]]
        head = "全部吻合" if not bad else f"{len(bad)} 项对不上"
        lines.append(f"◆ 面板校验（识别值 vs 本地推算）—— {head}")
        for i in range(0, len(checks), 2):
            parts = []
            for c in checks[i:i + 2]:
                got, want = float(c["panel"]), float(c["calc"])
                # 相对差 <0.1% 视为完全吻合（游戏显示舍入造成的小数残差，
                # 如 总伤 1488.3 vs 1488.24 —— 差 0.004%，如实显示相等）
                if c["ok"] and abs(got - want) <= abs(want) * 0.001:
                    parts.append(f"{c['label']} {got:g}{c['unit']} ✓")
                else:
                    parts.append(f"{c['label']} {got:g}{c['unit']} vs "
                                 f"{want:.2f}{c['unit']}"
                                 + ("" if c["ok"] else " ⚠"))
            lines.append("　" + "｜".join(parts))
        if bad:
            lines.append("　⚠ 可能漏读了卡片，或某张卡的等级推断有误")
        for c in _tips:
            lines.append(f"　※ {c['label']}：{c['note']}"
                         f"（识别 {float(c['panel']):g}，推导 {float(c['calc']):.0f}）")

    ref = an.get("ref")
    if ref:
        # FACTION_ZH 的值形如「Grineer（G系）」，嵌进括号里会套娃，先去括号
        fac_zh = re.sub(r"（.*?）", "", dc.FACTION_ZH.get(ref["faction"],
                                                         ref["faction"]))
        lines.append(
            f"◆ 参考（{fac_zh}"
            f" {ref['level']}级·身体，无剥甲/异常层数）：单发 {ref['health']:.0f}"
            f"｜暴击期望 ×{ref['crit_exp']:.2f}"
            f" → {ref['health'] * ref['crit_exp']:.0f}")
        hv = ref.get("heavy")
        if hv:
            extra = ""
            if hv.get("crit_cc") is not None:
                extra = f"，暴击 {hv['crit_cc'] * 100:.0f}%"
            lines.append(f"　重击（{hv['combo_hits']} 连击{extra}）："
                         f"{hv['health']:.0f}｜含暴击 {hv['health_crit']:.0f}")
        tw = ref.get("throw")
        if tw:
            lines.append(f"　投掷（连续 {tw['stacks']}/{tw['max_stacks']} 层"
                         f"，×{tw['mult']:.1f}）：{tw['health']:.0f}"
                         f"｜含暴击 {tw['health_crit']:.0f}")
        _modes = ref.get("modes") or []
        if _modes:
            # 识卡卡面只给摘要：完整各段走「伤害」指令，免得这一页太长
            _top3 = "、".join(f"{m['name']}{m['total']:g}" for m in _modes[:3])
            lines.append(f"　另有 {len(_modes)} 段独立判定：{_top3}…"
                         "（用「伤害」指令可看每段明细）")
    if totals.get("no_rank"):
        lines.append("　⚠ 等级无法确定的卡：" + "、".join(totals["no_rank"])
                     + "（已按满级估算，仅供参考）")
    if totals.get("uncalc"):
        lines.append("　注：以下卡无数值或暂不支持，未计入：" + "、".join(totals["uncalc"]))
    if an.get("unknown"):
        lines.append("　注：库中未收录的名称：" + "、".join(an["unknown"]))
    if an.get("alts"):
        lines.append("　近似武器：" + "、".join(
            v.get("zh") or v.get("name", "") for v in an["alts"]))
    for note in an.get("notes") or []:
        lines.append(f"　{note}")
    for err in an.get("errors") or []:
        lines.append(f"✘ {err}")

    lines.append("※ 加成一律按卡片实际等级折算（容量数字反推，再用面板数值校验）；"
                 "复合元素按 火>冰>电>毒 两两配对")
    return lines
