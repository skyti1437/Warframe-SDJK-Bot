# -*- coding: utf-8 -*-
"""wiki 简介卡片：知识库条目的离线查询。

数据由 ``scripts/build_wiki_intro.py`` 在构建期从知识库 Markdown 抽取
（``core/data/wiki_intro.json``：战甲 / 武器 / MOD / 资源 / 同伴等）。
数据与读取代码随开源、市场包分发；文件缺失时返回 None，调用方回落纯链接。
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Optional

try:
    from . import matching  # core 包内正常导入（变体词表同源）
except ImportError:  # 离线脚本把 core/ 当顶层路径导入时
    import matching

try:
    from . import paths as _paths  # 共享只读 JSON 缓存（drops.json 三处读者同一份）
except ImportError:  # 离线脚本把 core/ 当顶层路径导入时
    import paths as _paths

_FILE = Path(__file__).resolve().parent / "data" / "wiki_intro.json"
_DROPS_FILE = Path(__file__).resolve().parent / "data" / "drops.json"
_EFFECT_ZH_FILE = Path(__file__).resolve().parent / "data" / "wiki_effect_zh.json"
_NAME_EN_ZH_FILE = Path(__file__).resolve().parent / "data" / "de" / "name_en_zh.json"
_RELIC_INDEX_FILE = Path(__file__).resolve().parent / "data" / "relic_index.json"
_RELIC_INVERSE_FILE = Path(__file__).resolve().parent / "data" / "relic_inverse.json"

try:
    from .parser import TIER_CN as _TIER_CN  # 档位中英对照（含先锋/Vanguard）
except ImportError:
    # ★ 包内导入失败 = 真错误 → 原样抛出（别再静默降级成空表：档位中英对照
    #   一旦为空，遗物档位会显示不出中文，且没有痕迹）。只有**非包上下文**
    #   （离线脚本把 core/ 当顶层路径）才走绝对导入兜底。
    if __package__:
        raise
    try:
        from parser import TIER_CN as _TIER_CN
    except ImportError:  # pragma: no cover
        _TIER_CN = {}

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
    """效果行的值查翻译表换中文；表里没有就原样保留。

    表里既有**纯英文整句**的译文，也有几行「半中半英」的人工覆盖
    （知识库术语转换残留，见 wiki_effect_zh.json 的 _meta）——所以这里
    不再按「值是否含中文」提前跳过，直接查表。
    """
    m = _EFFECT_LINE.match(text or "")
    if not m:
        return text
    zh = _effect_zh().get(_norm(m.group("val").strip()))
    if not zh:
        # ★ 2026-10-22（维护者 10-19 报）：**表里没有**这条效果 ⇒ 卡面直接显示英文原文，
        #   于是夹着英文 `and`、行末还留英文句点。这里只做**排版卫生**（不翻译、不猜词）。
        #   ⚠ 不能改数据里的效果行 —— 那是翻译表的**键**，改了查表就失效（见生成器注释）。
        return tidy_effect_text(text)
    return f"{m.group('head')}：{zh}"


def tidy_effect_text(text: str) -> str:
    """未命中翻译表的英文效果行：夹的 ` and ` → 「 和 」、去掉行末英文句点。"""
    s = re.sub(r"\s+and\s+", " 和 ", text or "")
    s = re.sub(r"\s*\.\s*$", "", s)
    return s


# DE 导出变量占位符（知识库正文保留了官方模板的 〈DAMAGE〉 这类 token）。
# 数值不在导出包内（pep 只有模板、WFCD 也未替换，wiki 被 CF 拦）——
# 按 2026-09-24 用户口径处理：能读顺的**直接删**，后随单位的换 X。
_UNIT_AFTER = set("%×") | {"秒", "米", "次", "层", "倍", "点", "个", "名", "发", "枚", "颗", "段"}
_PH_RE = re.compile(r"〈[^〉]+〉")
_COLOR_RE = re.compile(r"〈(?:OPEN_COLOR|CLOSE_COLOR)〉", re.I)

# —— 半角富文本标签（2026-09-29 v1.1.1 根治）——
# 官方整句里夹杂 <DT_FIRE_COLOR>（伤害类型着色）、<LOWER_IS_BETTER>（低更优标记）
# 这类**纯格式**标签，以及 <ENERGY>/<SHIELD>/<AFFINITY_SHARE>（词形已在后文）与
# <SECONDARY_FIRE>（无后随词，须映射中文）。旧实现只清全角〈〉，半角会原样上卡
# （de_worldstate._ANY_SLOT_RE 显式排除 <>，兜不到）。数据与渲染双清：
# 数据侧同规则已施于 wiki_effect_zh.json（92 条），此处为防线；tests 有哨兵断言。
_HW_TAG = re.compile(r"<([A-Z_][A-Z0-9_]*)>")
_HW_WORD_AFTER = {"ENERGY": "能量", "SHIELD": "护盾", "AFFINITY_SHARE": "经验共享"}
_HW_MAP_ALWAYS = {"SECONDARY_FIRE": "次要射击"}


def _clean_hw_tags(text: str) -> str:
    """半角富文本标签清洗：纯格式剥除；有后随中文词的删；无后随词的映射；
    收尾统一空格（双空格、中文间空格、标点前空格）。数字一律不动。"""

    def sub(m: "re.Match[str]") -> str:
        tag = m.group(1)
        if tag in _HW_MAP_ALWAYS:
            return _HW_MAP_ALWAYS[tag]
        word = _HW_WORD_AFTER.get(tag)
        if word:
            rest = (text or "")[m.end() :].lstrip()
            return "" if rest.startswith(word) else word
        return ""  # <DT_*_COLOR> / <LOWER_IS_BETTER> 等

    t = _HW_TAG.sub(sub, text or "")
    t = re.sub(r" {2,}", " ", t)
    t = _SPACE_BETWEEN_CJK.sub("", t)
    t = _SPACE_BEFORE_PUNCT.sub(r"\1", t)
    return t


# 删掉占位符后残留的空格（中文之间 / 标点前不留空格）
_CJK = "一-鿿，。；、（）"
_SPACE_BETWEEN_CJK = re.compile(rf"(?<=[{_CJK}])\s+(?=[{_CJK}])")
_SPACE_BEFORE_PUNCT = re.compile(r"\s+([，。；、）])")

# 极性名 → 图标键（core/data/icons/polarity/<key>.png，wiki 抓取；渲染层
# 画行内小图标，见 core/render.py::_draw_polarity_icon）。Aura 没有独立图标
# 文件（wiki 极性页实测），保留文字。
_POLARITY_KEY = {
    "madurai": "madurai",
    "vazarin": "vazarin",
    "naramon": "naramon",
    "zenurik": "zenurik",
    "penjaga": "penjaga",
    "unairu": "unairu",
    "umbra": "umbra",
}
_POL_NAME_RE = re.compile(r"\b(Madurai|Vazarin|Naramon|Zenurik|Penjaga|Unairu|Umbra)\b", re.I)


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
    t = _clean_hw_tags(t)  # 半角富文本（<DT_FIRE_COLOR> 等），同上口径

    def sub(m: "re.Match[str]") -> str:
        return "X" if t[m.end() : m.end() + 1] in _UNIT_AFTER else ""

    t = _PH_RE.sub(sub, t)
    t = _SPACE_BETWEEN_CJK.sub("", t)
    t = _SPACE_BEFORE_PUNCT.sub(r"\1", t)
    t = t.replace("：；", "：")  # 源自「On Kill:；…」的残留搭配
    t = re.sub(r" {2,}", " ", t).strip()
    return _mark_polarity(t)


def _norm(s: str) -> str:
    """与 core/search.py::_norm 同口径。"""
    return " ".join(re.sub(r"[^0-9a-z一-鿿]+", " ", (s or "").lower()).split())


class _EntryRows(list):
    """保留原 entries 接口，旁挂每条记录的来源与资产身份。"""

    def __init__(self, rows, assets, pvp=(), views=None):
        super().__init__(rows)
        self.assets = assets
        self.pvp = frozenset(pvp)
        self.views = views or {}


@lru_cache(maxsize=1)
def _table() -> tuple[list, dict, dict]:
    """兼容旧下标、完整身份格式与紧凑数字引用；保留同名资产。"""
    try:
        data = json.loads(_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return [], {}, {}
    if not isinstance(data, dict):
        return [], {}, {}
    entries, keys, uses = data.get("entries") or [], data.get("keys") or {}, data.get("uses") or {}
    if not (isinstance(entries, list) and isinstance(keys, dict) and isinstance(uses, dict)):
        return [], {}, {}
    if isinstance(data.get("_meta"), dict) and data["_meta"].get("schema") == 3:
        pvp = data.get("pvp") or []
        views = data.get("views") or {}
        if not isinstance(pvp, list) or not isinstance(views, dict):
            return [], {}, {}
        return _EntryRows(entries, [], (i for i in pvp if isinstance(i, int)), views), keys, uses
    ids = data.get("id_entries") or {}
    if not isinstance(ids, dict):
        return [], {}, {}
    resolved = {}
    for key, value in keys.items():
        if isinstance(value, int):
            resolved[key] = value
            continue
        indices = []
        for asset_id in value if isinstance(value, list) else []:
            if isinstance(asset_id, str):
                indices.extend(ids.get(asset_id, []))
            elif isinstance(asset_id, int):
                indices.append(asset_id)
        resolved[key] = list(dict.fromkeys(i for i in indices if isinstance(i, int)))
    assets = data.get("assets") or []
    return _EntryRows(entries, assets if isinstance(assets, list) else []), resolved, uses


def candidates(name: str) -> list[dict]:
    """精确中/英文名的全部同名资产；不以显示标题去重。"""
    entries, keys, _ = _table()
    value = keys.get(_norm(name), [])
    indices = [value] if isinstance(value, int) else value if isinstance(value, list) else []
    rows = []
    assets = getattr(entries, "assets", [])
    for i in indices:
        if not isinstance(i, int) or not 0 <= i < len(entries):
            continue
        e = entries[i]
        if not isinstance(e, list) or len(e) != 2:
            continue
        meta = assets[i] if i < len(assets) and isinstance(assets[i], dict) else {}
        rows.append(
            {
                "title": str(e[0]),
                "lines": e[1],
                "index": i,
                "asset_index": i,
                "variant": "pvp" if i in getattr(entries, "pvp", ()) else "normal",
                **meta,
            }
        )
    rows.sort(
        key=lambda x: (
            x.get("variant") == "pvp",
            x.get("source_file") == "08_其他与机制术语.md",
            x["index"],
        )
    )
    out, seen = [], set()
    for row in rows:
        asset_id = row.get("asset_id", row["asset_index"])
        if asset_id not in seen:
            seen.add(asset_id)
            out.append(row)
    return out


def available() -> bool:
    """简介数据是否存在（随开源/市场包分发）。"""
    return bool(_table()[1])


# 简介数据里的「标签：值」行 → 行文法的 KV 行（◆ 标签：值）；叙述性正文（无标签）原样保留。
_KV_LINE_RE = re.compile(
    r"^(?:(类型)(?:[：:]\s*|\s*)|(定位|获取|效果（[^）]{1,12}）|效果|建造材料|说明|简介|数据|特性|属性"
    r"|总伤害|可交易|商店|自带极性|用途|技能|被动|状态)(?:[：:]\s*|\s+))(\S.*)$"
)


def _kvify(line: str) -> str:
    """「类型 主武器 MOD｜…」「效果（满级 3）：…」→「◆ 类型：…」「◆ 效果（满级 3）：…」。"""
    m = _KV_LINE_RE.match(line)
    if not m:
        return line
    return f"◆ {m.group(1) or m.group(2)}：{m.group(3).strip()}"


# ---- 显示层兜底（2026-10-17 用户截图「宝拉」）：上游对 Zaw 部件给的伤害数组是巨型负数（±2.8e22）、
#   暴击 / 触发 / 射速全 0，知识库生成器原样写进了卡片数据。源头闸门由知识库重构 P1 做；这里保证
#   插件即使拿到旧数据也不把乱码画到卡上（不按物品名特判，只按数值规则）。
_DMG_ABS_MAX = 1e6  # 单个伤害分量的合理上限（游戏内最大面板远小于此）
_DMG_PART_RE = re.compile(r"^\s*(\S+?)\s+(-?\d+(?:\.\d+)?)\s*$")
_FLOAT_TAIL_RE = re.compile(r"(\d+\.\d{6,})")
_ZERO_PANEL_RE = re.compile(r"^数据[：:]\s*暴击几率 0%｜暴击倍率 0×｜触发几率 0%｜射速 0\s*$")
MODULAR_NOTE = (
    "说明：Zaw 由打击 + 握柄 + 环扣组合而成，单个部件没有独立面板；实际数值看组合后的武器"
)


def _round_float_tail(m: re.Match) -> str:
    """float32 残渣（0.6000000238418579）→ 最多 3 位小数（0.6）。"""
    try:
        return f"{float(m.group(1)):.3f}".rstrip("0").rstrip(".")
    except ValueError:
        return m.group(1)


def _clean_damage(line: str) -> Optional[str]:
    """「总伤害：T（a 1｜b 2…）」：总伤害 ≤ 0 ⇒ 整行不要；否则丢掉非有限 / 负数 / 超大的分量。"""
    m = re.match(r"^(总伤害[：:]\s*)(-?\d+(?:\.\d+)?)(?:（(.*?)）?)?(…)?$", line)
    if not m:
        return line
    head, total, body = m.group(1), m.group(2), m.group(3) or ""
    keep = []
    for part in [x for x in body.replace("…", "").split("｜") if x.strip()]:
        pm = _DMG_PART_RE.match(part)
        if not pm:
            continue
        try:
            v = float(pm.group(2))
        except ValueError:
            continue
        if v < 0 or v != v or abs(v) > _DMG_ABS_MAX:
            continue
        keep.append(part.strip())
    try:
        t = float(total)
    except ValueError:
        t = 0.0
    if t <= 0:  # 总伤害 ≤ 0 时分量必是垃圾（实测 Zaw 部件「总伤害 0（磁力 0.5｜腐蚀 -2.8e22…）」）
        return None
    if not body:
        return line
    return f"{head}{total}" + (f"（{'｜'.join(keep)}）" if keep else "")


def _sanitize(lines: list[str]) -> list[str]:
    out: list[str] = []
    dropped = False
    modular = any("Zaw 部件" in x for x in lines)
    for raw in lines:
        x = _FLOAT_TAIL_RE.sub(_round_float_tail, raw)
        if x.startswith("总伤害"):
            y = _clean_damage(x)
            if y is None:
                dropped = True
                continue
            x = y
        elif _ZERO_PANEL_RE.match(x):
            dropped = True
            continue
        out.append(x)
    if modular and dropped and not any(x.startswith("说明") for x in out):
        at = next((i + 1 for i, x in enumerate(out) if x.startswith("定位")), 0)
        out.insert(at, MODULAR_NOTE)
    return out


_MAX_RANK_RE = re.compile(r"满级\s*(\d+)")


def _prefer_full_rank(rows: list) -> dict:
    """同名多候选时挑「满级」最高的那条（2026-10-25，Astra 真实宿主补测）。

    「膛线（Serration）」有满级 3 / 5 / 10 三条同名条目（旧版 / 不同来源的数据），原先取第一条
    ⇒ 卡面写满级 3、+40%，与正常版满级 10、+165% 冲突。只在**标题相同、版本相同**（都不是 PvP，
    或都是 PvP）时按满级挑；首条之外有标题不同的候选就保持原顺序，不跨物品换条目。
    """
    first = rows[0]
    peers = [
        r
        for r in rows
        if r.get("title") == first.get("title") and r.get("variant") == first.get("variant")
    ]
    if len(peers) < 2:
        return first

    def rank(r: dict) -> int:
        for x in r.get("lines") or []:
            m = _MAX_RANK_RE.search(str(x))
            if m:
                return int(m.group(1))
        return -1

    return max(peers, key=rank)


def intro(*names: str) -> Optional[tuple[str, list[str]]]:
    """按候选名字（页面名 / 检索名 / 英文名）查简介条目。

    Returns:
        ``(标题, 行列表)``；都查不到返回 ``None``。
    """
    # 页面名与原查询会一起传入；显式 PvP 查询不能被排在前面的普通页面别名盖掉。
    ordered_names = sorted(names, key=lambda name: not _norm(name).endswith(" pvp"))
    for name in ordered_names:
        rows = candidates(name)
        if not rows:
            continue
        chosen = _prefer_full_rank(rows)
        lines = [
            _kvify(_localize(_localize_effect(x)))
            for x in _sanitize([str(x) for x in chosen["lines"]])
        ]
        if chosen.get("variant") == "pvp":
            lines.append("◆ 版本：PvP 变体")
        if len(rows) > 1:
            hint = (
                "（含 PvP 变体，可加 PvP 查询）"
                if any(r.get("variant") == "pvp" for r in rows)
                else ""
            )
            lines.append(f"◆ 同名条目：共 {len(rows)} 条{hint}")
        return chosen["title"], lines
    return None


def exact_title(name: str) -> Optional[str]:
    """名字与简介数据的某个键**归一化后完全相等**时返回该条目标题（如「Balla」→「宝拉（Balla）」）。

    给 wiki 指令用：本地检索索引只有内部英文名（Zaw 部件是「tip one」），精确未中时会退到前缀模糊，
    把「Balla」错配成「Ballas 的合一众权杖」（2026-10-17 知识库审计）。简介数据的键含玩家可见英文名。
    """
    rows = candidates(name)
    return rows[0]["title"] if rows else None


@lru_cache(maxsize=1)
def _drops() -> tuple[dict, dict, dict, dict]:
    """drops.json 的 (items, 归一化英文名 → 原键, places, zh2en)。

    原表经 ``paths.load_json`` 与 core/drops.py、core/search.py 共享（只读，不得就地修改）。
    """
    d = _paths.load_json(_DROPS_FILE)
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
        # 掉率可能缺失（drops.json 里少数条目 chance 为 None）：排最后，不让整张卡崩掉
        return (relic, -float(row[2]) if row[2] is not None else 1.0)

    rows = sorted(srcs, key=_rank)[:limit]
    title = (names[0] or key).strip() or key
    lines = [f"◆ 掉落来源（共 {len(srcs)} 处，按几率排序）", "　来源　几率"]
    for row in rows:
        pid, _rarity, chance = row[0], row[1], row[2]
        place = str(places.get(str(pid)) or places.get(pid) or "?").replace("　", " ")
        lines.append(f"· {place}　{chance}%" if chance is not None else f"· {place}　几率未知")
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
    lines = [f"◆ 用途 · 用于制造（共 {hit.get('n', len(prods))} 个配方）"]
    lines += [f"· {str(p).replace('　', ' ')}" for p in prods]
    return title, lines


@lru_cache(maxsize=1)
def _relics() -> tuple[dict, dict]:
    """遗物表：(遗物名 → {常见/罕见/稀有: [部件]}, 部件名 → [{relic, rarity}])。"""

    def _load(p: Path) -> dict:
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return d if isinstance(d, dict) else {}

    return _load(_RELIC_INDEX_FILE), _load(_RELIC_INVERSE_FILE)


def _relic_key(name: str) -> str:
    """遗物口语名 → relic_index 键（「前纪 V11 遗物」→「前纪 Meso V11」）。

    档位中英对照用 ``core.parser.TIER_CN``（含先锋 Vanguard / 全能 Omnia），
    别在本模块再硬编一份（遗物指令就栽在局部表缺档位上，见交接说明）。
    """
    s = re.sub(r"\s+", "", (name or "").replace("遗物", ""))
    m = re.match(r"^([一-鿿]{2,4})([A-Za-z]\d{1,2})$", s)
    if not m:
        return ""
    cn, code = m.group(1), m.group(2)
    en = _TIER_CN.get(cn) or ""
    if not en or (en, cn) not in _TIER_CN.items():
        return ""
    return f"{cn} {en} {code.upper()}"


def relic_rewards(name: str) -> Optional[dict]:
    """遗物名 → ``{"稀有"/"罕见"/"常见": [部件…]}``；查不到返回 None（不猜）。

    中英文写法都吃：「古纪 K5 遗物」（DE 简中）/「Lith B12 Relic」（新出库遗物简中词表常缺，
    回落英文名 —— 2026-10-16 实测本期 6 个出库遗物全是英文名）/「后纪A2」。
    """
    s = re.sub(r"(?i)\s*(?:遗物|relics?)\s*$", "", (name or "").strip())
    m = re.match(r"^([A-Za-z]+)\s*([A-Za-z]\d{1,2})$", s)
    if m:
        en2cn = {en.lower(): cn for cn, en in _TIER_CN.items()}
        cn = en2cn.get(m.group(1).lower())
        if not cn:
            return None
        s = f"{cn}{m.group(2)}"
    key = _relic_key(s)
    if not key:
        return None
    idx, _inv = _relics()
    return idx.get(key) or None


def _relic_card(*names: str, limit: int = 3) -> Optional[tuple[str, list[str]]]:
    """遗物卡：三档奖励（前纪 V11 遗物这类 —— 知识库/掉落表都没有条目）。"""
    idx, _inv = _relics()
    if not idx:
        return None
    flat = {k.replace(" ", ""): k for k in idx}
    hit = ""
    for n in names:
        for cand in (_relic_key(n), re.sub(r"\s+", "", n or "")):
            key = re.sub(r"\s+", "", cand or "")
            if key and key in flat:
                hit = flat[key]
                break
        if hit:
            break
    if not hit:
        return None
    rar = idx.get(hit) or {}
    lines = []
    for tag in ("常见", "罕见", "稀有"):
        items = [str(x) for x in (rar.get(tag) or [])]
        if items:
            lines.append(f"· {tag}　{'、'.join(items[:limit])}")
    if not lines:
        return None
    lines.insert(0, "◆ 奖励（三档）")
    title = next((n for n in names if (n or "").strip()), hit).strip() or hit
    return title, lines


def _relic_part_card(*names: str, limit: int = 8) -> Optional[tuple[str, list[str]]]:
    """部件卡：这个部件出自哪些遗物（relic_inverse 反查）。"""
    _idx, inv = _relics()
    if not inv:
        return None
    flat = {k.replace(" ", ""): k for k in inv}
    hit = ""
    for n in names:
        k = re.sub(r"\s+", "", n or "")
        if k and k in flat:
            hit = flat[k]
            break
    if not hit:
        return None
    rows = inv.get(hit) or []
    if not rows:
        return None
    title = next((n for n in names if (n or "").strip()), hit).strip() or hit
    lines = [f"◆ 所在遗物（共 {len(rows)} 个）", "　遗物　稀有度"]
    lines += [f"· {r.get('relic')}　{r.get('rarity')}" for r in rows[:limit]]
    return title, lines


def _minimal_card(*names: str) -> Optional[tuple[str, list[str]]]:
    """兜底卡：任何已解析的条目都出图（链接另发 Plain，见 main._wiki_reply）。"""
    title = next((n.strip() for n in names if (n or "").strip()), "")
    if not title:
        return None
    return title, ["◆ 状态：本地资料库暂无该条目的简介", "※ 详见下方 wiki 页面链接"]


_PATH_SEG_RE = re.compile(r"^(?:de:)?.*?/([^/]+)$")
_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_CODE_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]{11,}$")
_TITLE_WORDS: Optional[set] = None
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def _title_words() -> set:
    """英文显示名词表（`core/data/de/name_en_zh.json::names` 的键按空格拆）——切词用。"""
    global _TITLE_WORDS
    if _TITLE_WORDS is None:
        words: set = set()
        try:
            raw = json.loads((_NAME_EN_ZH_FILE).read_text(encoding="utf-8")).get("names") or {}
            for k in raw:
                words.update(str(k).split())
        except Exception:  # noqa: BLE001 - 词表缺失只影响切词质量，不阻断出卡
            words = set()
        _TITLE_WORDS = words
    return _TITLE_WORDS


def _greedy_words(s: str) -> str:
    """无空格代码名按词表**贪心切词**（`gladiatorsetmod` → `gladiator set mod`）；切不动原样返回。"""
    words = _title_words()
    if not words:
        return s
    out, i = [], 0
    while i < len(s):
        best = ""
        for w in words:
            if len(w) <= 22 and s.startswith(w, i) and len(w) > len(best):
                best = w
        if not best:
            return s
        out.append(best)
        i += len(best)
    return " ".join(out)


def display_title(title: str) -> str:
    """标题兜底：**内部资产名 / 路径不许原样上卡面**（维护者 2026-10-19 报）。

    资料卡数据抽自知识库，KB 给资产条目的 id 是 `de:<uniqueName>`；拿不到显示名时
    标题就成了 `de:/Lotus/Types/…/Forma`、`ChromaPrimeHelmetComponent` 这类内部名。
    兜底顺序：**路径取末段**（永不显示路径）→ 驼峰拆词 → 词表贪心切词 → 首字母大写。
    官方名能查到的一律不动（本函数只处理「看起来是代码名」的形态）。
    """
    t = (title or "").strip()
    # ★ 2026-10-22 Claude 审：只动「看起来就是代码名」的标题。旧版对全部 13065 条标题生效，
    #   改了 390 条 —— 「红/白/蓝（Red/White/Blue）」被当路径截成「Blue）」、
    #   「TennoCon 2016 披饰」被拆成「Tenno Con」。带中文 = 已有显示名，一律不动。
    if not t or _CJK_RE.search(t):
        return t
    if t.startswith(
        ("de:", "/")
    ):  # 只有整条是路径才取末段（标题里的「A/V」「Past/Future」不是路径）
        m = _PATH_SEG_RE.match(t)
        if m:
            t = m.group(1)
    head, sep, tail = t.partition("（")
    token = head.strip()
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", token):  # 多词英文名是正常显示名
        return t
    t2 = _CAMEL_RE.sub(" ", token)
    if t2 == token and _CODE_NAME_RE.match(token):
        t2 = _greedy_words(token.lower())
    return t2[:1].upper() + t2[1:] + sep + tail


def card_for(*names: str) -> Optional[tuple[str, list[str]]]:
    """卡片入口：知识库 → 掉落表 → 配方用途 → 遗物 → 兜底（**永远有卡**）。

    用户口径（2026-09-25）：「正常情况下无论什么内容都得绘制」——命中条目时
    哪怕本地没有简介数据，也要出一张只含名字的卡（链接另发 Plain）。
    """
    card = (
        intro(*names)
        or _drops_card(*names)
        or _uses_card(*names)
        or _relic_card(*names)
        or _relic_part_card(*names)
        or _minimal_card(*names)
    )
    # ★ 2026-10-22：出口统一兜底标题 —— 内部资产名/路径不许原样上卡面（维护者 10-19 报）。
    if card:
        _t, _lines = card
        _t2 = display_title(_t)
        if _t2 != _t:
            return _t2, _lines
    return card


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
    self_i = keys.get(base)  # 整名键（中文名 + 英文名）会命中条目自身
    out: list[str] = []
    self_indices = {self_i} if isinstance(self_i, int) else set(self_i or [])
    for k, value in keys.items():
        if not k.startswith(base + " "):
            continue
        for i in [value] if isinstance(value, int) else value:
            if i in self_indices or not isinstance(i, int) or not 0 <= i < len(entries):
                continue
            title = str(entries[i][0])
            if _is_variant_title(title, base_cmp) and title not in out:
                out.append(title)
    return out[:limit]
