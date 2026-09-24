# -*- coding: utf-8 -*-
"""wiki 简介卡片：知识库条目的离线查询。

数据由 ``scripts/build_wiki_intro.py`` 在构建期从知识库 Markdown 抽取
（``core/data/wiki_intro.json``：战甲 / 武器 / MOD / 资源 / 同伴等）。
本文件**不进开源与市场包**（dist/package_release.py::EXCLUDE_FILES）——
公开版 wiki 只给链接；文件缺失时这里返回 None，调用方自然回落纯链接。
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Optional

try:
    from . import matching        # core 包内正常导入（变体词表同源）
except ImportError:               # 离线脚本把 core/ 当顶层路径导入时
    import matching

_FILE = Path(__file__).resolve().parent / "data" / "wiki_intro.json"
_DROPS_FILE = Path(__file__).resolve().parent / "data" / "drops.json"
_EFFECT_ZH_FILE = Path(__file__).resolve().parent / "data" / "wiki_effect_zh.json"

# 效果行：「效果（满级 5）：+40% Status Chance per Combo Multiplier」
_EFFECT_LINE = re.compile(r"^(?P<head>效果(?:（[^）]*）)?)：(?P<val>.+)$")


@lru_cache(maxsize=1)
def _effect_zh() -> dict:
    """英文效果文本（归一化）→ 中文译文。

    自建翻译表：DE 导出里**没有**这些整句效果的中文（实测 dict.zh 0 命中，
    它们来自 WFCD 的英文文本），术语表式转换也做不了整句 —— 见文件 _meta。
    """
    try:
        d = json.loads(_EFFECT_ZH_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    table = d.get("effects") if isinstance(d, dict) else None
    return table if isinstance(table, dict) else {}


def _localize_effect(text: str) -> str:
    """效果行的英文值 → 查翻译表换中文；表里没有就保留英文原文。"""
    m = _EFFECT_LINE.match(text or "")
    if not m:
        return text
    val = m.group("val").strip()
    if not val or re.search(r"[一-鿿]", val):
        return text
    zh = _effect_zh().get(_norm(val))
    return f"{m.group('head')}：{zh}" if zh else text

# DE 导出变量占位符（知识库正文保留了官方模板的 〈DAMAGE〉 这类 token）。
# 数值不在导出包内（pep 只有模板、WFCD 也未替换，wiki 被 CF 拦）——
# 按 2026-09-24 用户口径处理：能读顺的**直接删**，后随单位的换 X。
_UNIT_AFTER = set("%×") | {"秒", "米", "次", "层", "倍", "点", "个", "名",
                           "发", "枚", "颗", "段"}
_PH_RE = re.compile(r"〈[^〉]+〉")
_COLOR_RE = re.compile(r"〈(?:OPEN_COLOR|CLOSE_COLOR)〉", re.I)
# 删掉占位符后残留的空格（中文之间 / 标点前不留空格）
_CJK = "一-鿿，。；、（）"
_SPACE_BETWEEN_CJK = re.compile(rf"(?<=[{_CJK}])\s+(?=[{_CJK}])")
_SPACE_BEFORE_PUNCT = re.compile(r"\s+([，。；、）])")

# 极性名 → 图标键（core/data/icons/polarity/<key>.png，wiki 抓取；渲染层
# 画行内小图标，见 core/render.py::_draw_polarity_icon）。Aura 没有独立图标
# 文件（wiki 极性页实测），保留文字。
_POLARITY_KEY = {"madurai": "madurai", "vazarin": "vazarin",
                 "naramon": "naramon", "zenurik": "zenurik",
                 "penjaga": "penjaga", "unairu": "unairu", "umbra": "umbra"}
_POL_NAME_RE = re.compile(
    r"\b(Madurai|Vazarin|Naramon|Zenurik|Penjaga|Unairu|Umbra)\b", re.I)


def _mark_polarity(text: str) -> str:
    """极性行里给极性名前加图标标记（只处理含「极性」的行，避免误伤
    「Excalibur Umbra」这类同名词）。"""
    if "极性" not in text:
        return text

    def sub(m: "re.Match[str]") -> str:
        name = m.group(1)
        key = _POLARITY_KEY.get(name.lower())
        return f"⟦pol:{key}⟧{name}" if key else name
    return _POL_NAME_RE.sub(sub, text)


def _localize(text: str) -> str:
    """占位符读顺化 + 极性行附图标标记。

    数值拿不到（导出包只有模板），所以：
    * 后随单位（``%`` ``×`` 秒 米 次 层…）→ 换 ``X``（「伤害增加 X%」「X 秒」）；
    * 其余位置**连同空格删掉**（「能造成 〈DAMAGE〉 伤害的冲击波」→
      「能造成伤害的冲击波」，2026-09-24 用户口径：读顺优先）；
    * 〈OPEN_COLOR〉/〈CLOSE_COLOR〉 是富文本记号，删掉。
    """
    t = _COLOR_RE.sub("", text or "")

    def sub(m: "re.Match[str]") -> str:
        return "X" if t[m.end():m.end() + 1] in _UNIT_AFTER else ""

    t = _PH_RE.sub(sub, t)
    t = _SPACE_BETWEEN_CJK.sub("", t)
    t = _SPACE_BEFORE_PUNCT.sub(r"\1", t)
    t = t.replace("：；", "：")          # 源自「On Kill:；…」的残留搭配
    t = re.sub(r" {2,}", " ", t).strip()
    return _mark_polarity(t)


def _norm(s: str) -> str:
    """与 core/search.py::_norm 同口径。"""
    return " ".join(re.sub(r"[^0-9a-z一-鿿]+", " ", (s or "").lower()).split())


@lru_cache(maxsize=1)
def _table() -> tuple[list, dict, dict]:
    """读简介数据；文件缺失/损坏时返回空表（可选项，不是硬依赖）。"""
    try:
        data = json.loads(_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return [], {}, {}
    if not isinstance(data, dict):
        return [], {}, {}
    entries = data.get("entries") or []
    keys = data.get("keys") or {}
    uses = data.get("uses") or {}
    if not (isinstance(entries, list) and isinstance(keys, dict)
            and isinstance(uses, dict)):
        return [], {}, {}
    return entries, keys, uses


def available() -> bool:
    """简介数据是否存在（开源/市场包不带该文件）。"""
    return bool(_table()[1])


def intro(*names: str) -> Optional[tuple[str, list[str]]]:
    """按候选名字（页面名 / 检索名 / 英文名）查简介条目。

    Returns:
        ``(标题, 行列表)``；都查不到返回 ``None``。
    """
    entries, keys, _ = _table()
    for n in names:
        k = _norm(n)
        if not k:
            continue
        i = keys.get(k)
        if isinstance(i, int) and 0 <= i < len(entries):
            e = entries[i]
            if isinstance(e, list) and len(e) == 2:
                return str(e[0]), [_localize(_localize_effect(str(x)))
                                   for x in e[1]]
    return None


@lru_cache(maxsize=1)
def _drops() -> tuple[dict, dict, dict, dict]:
    """drops.json 的 (items, 归一化英文名 → 原键, places, zh2en)。"""
    try:
        d = json.loads(_DROPS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}, {}, {}, {}
    if not isinstance(d, dict):
        return {}, {}, {}, {}
    items = d.get("items") or {}
    idx: dict[str, str] = {}
    for k in items:
        idx.setdefault(_norm(k), k)
    return items, idx, (d.get("places") or {}), (d.get("zh2en") or {})


def _drops_card(*names: str, limit: int = 5) -> Optional[tuple[str, list[str]]]:
    """知识库没收录时的兜底卡：掉落表里有来源就给「掉落来源」。

    材料 / MOD / 遗物部件这类「在哪刷」是刚需（知识库 06 只收特殊资源，
    基础材料没有条目，2026-09-24 用户实测）。掉落表也没有的（铁氧体 /
    合金板这类全图掉落的基础资源）返回 None，调用方回落纯链接。
    """
    items, idx, places, zh2en = _drops()
    if not items:
        return None
    key = ""
    for n in names:
        n = (n or "").strip()
        if not n:
            continue
        key = zh2en.get(n) or idx.get(_norm(n)) or ""
        if key:
            break
    if not key:
        return None
    srcs = items.get(key) or []
    if not srcs:
        return None

    def _rank(row) -> tuple:
        place = places.get(str(row[0])) or places.get(row[0]) or ""
        # 遗物来源（「Lith T1 Relic (Exceptional)」这类）对「在哪刷」没用，
        # 排到任务节点后面，再按几率降序
        relic = ("relic" in place.lower()) or ("遗物" in place)
        return (relic, -float(row[2]))

    rows = sorted(srcs, key=_rank)[:limit]
    title = (names[0] or key).strip() or key
    lines = [f"掉落来源（共 {len(srcs)} 处，按几率排序）："]
    for row in rows:
        pid, _rarity, chance = row[0], row[1], row[2]
        place = places.get(str(pid)) or places.get(pid) or "?"
        lines.append(f"· {place}（{chance}%）")
    return title, lines


def _uses_card(*names: str, limit: int = 5) -> Optional[tuple[str, list[str]]]:
    """兜底卡之三：用制造配方反查「用途」。

    生物质 / 铁氧体 / 合金板这类**全图掉落的基础材料**既不在知识库里
    （06 只收特殊资源）、也不在 DE 掉落表里 —— 但每条配方/建造材料行都
    记着它们（2026-09-24 用户实测「wiki 生物质」不出图）。给出配方数与
    前几个产物即可让卡片有内容。
    """
    _, _, uses = _table()
    hit = None
    for n in names:
        k = _norm(n)
        if k and isinstance(uses.get(k), dict):
            hit = uses[k]
            break
    if not hit:
        return None
    prods = [str(p) for p in (hit.get("p") or [])][:limit]
    if not prods:
        return None
    title = (names[0] or prods[0]).strip() or prods[0]
    lines = [f"用途：用于制造（共 {hit.get('n', len(prods))} 个配方）："]
    lines += [f"· {p}" for p in prods]
    return title, lines


def card_for(*names: str) -> Optional[tuple[str, list[str]]]:
    """卡片入口：知识库 → 掉落表 → 配方用途 三级兜底。"""
    return intro(*names) or _drops_card(*names) or _uses_card(*names)


def _is_variant_title(title: str, base_cmp: str) -> bool:
    """标题是否为基体的**变体**（「Nekros Prime」；排除「Nekros Prime 摇头娃娃」）。

    中文段 / 英文段分开判：任一段剥掉变体 token 后**恰好等于**基体名即算变体。
    """
    zh = re.split(r"[（(]", title, maxsplit=1)[0]
    for part in [zh, *re.findall(r"[（(]([^）)]+)[）)]", title)]:
        n = matching.normalize(part)
        if n and n != base_cmp and matching.strip_variant_norm(n) == base_cmp:
            return True
    return False


def variants(base_name: str, limit: int = 6) -> list[str]:
    """基体名 → 变体条目名列表（知识库里同前缀的**变体**条目）。

    ``Nekros`` → ``['Nekros Prime']``；``布莱顿`` → ``['布莱顿 Prime', …]``。
    查询没指明变体时，卡片在本体介绍后补一行「变体：…」（2026-09-24 用户口径）。
    只收真正的变体（剥掉变体 token 后与基体同名前缀，词表见 core.matching）——
    知识库还有大量同前缀的外观/装饰条目（「Nekros 迅捷站姿」这类），
    不过滤会把变体行冲掉（2026-09-24 实测）。
    """
    entries, keys, _ = _table()
    base = _norm(base_name)
    if not base:
        return []
    base_cmp = matching.normalize(base_name)
    self_i = keys.get(base)          # 整名键（中文名 + 英文名）会命中条目自身
    out: list[str] = []
    for k, i in keys.items():
        if not k.startswith(base + " ") or not isinstance(i, int):
            continue
        if i == self_i or not (0 <= i < len(entries)):
            continue
        title = str(entries[i][0])
        if not _is_variant_title(title, base_cmp):
            continue
        if title not in out:
            out.append(title)
    return out[:limit]
