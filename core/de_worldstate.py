# -*- coding: utf-8 -*-
"""DE 官方世界状态适配层。

直接解析 https://api.warframe.com/cdn/worldState.php 的原始 JSON，
产出与 warframestat.us 同构的归一化结构，供 formatters / push 消费。

优点：无第三方中转、无 Cloudflare 拦截、单次请求即得全量数据。
限制：该端点为 PC 世界状态（ps/xb/sw 平台数据与 PC 一致或近似）；
赤毒/仲裁（10o.io）与钢铁之路轮换不包含在内，相关指令自动降级。

翻译数据来自 wfcd/warframe-worldstate-data（core/data/de/）。
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

DATA_DIR = Path(__file__).resolve().parent / "data" / "de"

# ---------------------------------------------------------------------------
# 静态数据表（进程级缓存）
# ---------------------------------------------------------------------------


@lru_cache(maxsize=None)
def _load(name: str) -> Any:
    """按文件名缓存静态表。

    **必须 maxsize=None**：这里会被 14 个不同文件名轮流命中，
    lru_cache(maxsize=1) 意味着一进一出、每次都重新读盘 —— 最大的
    languages_zh.json 有 3.5MB，实测把 AstrBot 的事件循环堵到 30s+。
    """
    path = DATA_DIR / name
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def sol_node(key: str) -> dict:
    return (_load("solNodes.json") or {}).get(key) or {}


def nodes_zh() -> dict:
    """DE 官方中文节点表：``uniqueName -> {"name", "system"}``。"""
    return _load("nodes_zh.json") or {}


def node_key_of(mission: str, planet: str) -> str:
    """按「任务名 + 星球」反查节点 key，用于接外部静态表（如结合目标）。

    ``solNodes.json`` 的 value 形如 ``Tikal (Earth)``，据此与联合目标表里
    的 ``{"mission": "Tikal", "planet": "Earth"}`` 对齐。

    Args:
        mission: 节点英文名，如 ``Tikal``。
        planet: 星球英文名，如 ``Earth``。

    Returns:
        节点 key（如 ``ClanNode3``）；查不到时返回空串。
    """
    if not mission:
        return ""
    want_node = _PLANET_RE.sub("", mission).strip().lower()  # 去掉 "(Unconfirmed)" 之类注解
    want_node = want_node.replace("-", " ")                  # E-Prime == E Prime
    want_planet = (planet or "").strip().lower()
    table = _load("solNodes.json") or {}
    hits: list[str] = []
    for key, info in table.items():
        if not isinstance(info, dict):
            continue
        value = (info.get("value") or "").strip().lower()
        head, _, tail = value.partition("(")
        if head.strip() != want_node:
            continue
        hits.append(key)
        if tail.rstrip(")").strip() == want_planet:
            return key
    return hits[0] if hits else ""


def mission_type(key: str) -> str:
    """任务类型 code（``MT_SURVIVAL``）或英文名 -> 官方简中。

    优先用 ``mission_types_zh.json``（由 DE ExportMissionTypes + 官方简中词表生成），
    它才是游戏内显示的那一套；旧表 ``missionTypes.json`` 是英文原值，仅作兜底。
    """
    zh = (_load("mission_types_zh.json") or {}).get(key)
    if zh:
        return zh
    v = (_load("missionTypes.json") or {}).get(key)
    if isinstance(v, dict):
        return v.get("value", key)
    return v or key


@lru_cache(maxsize=1)
def _mission_type_by_name() -> dict[str, str]:
    """英文任务类型名（小写）-> 官方简中。

    由两张官方表拼出：``missionTypes.json``（``MT_*`` -> 英文名）反查代码，
    再取 ``mission_types_zh.json``（``MT_*`` -> 官方简中）。
    例：``assassination`` -> ``MT_ASSASSINATION`` -> **刺杀**。
    """
    en = _load("missionTypes.json") or {}
    zh = _load("mission_types_zh.json") or {}
    out: dict[str, str] = {}
    for code, rec in en.items():
        name = rec.get("value") if isinstance(rec, dict) else rec
        if isinstance(name, str) and name and code in zh:
            out.setdefault(name.strip().lower(), zh[code])
    return out


def mission_type_zh_by_name(name: str) -> str:
    """英文任务类型名 -> 官方简中（查不到返回空串，调用方自己兜底）。"""
    return _mission_type_by_name().get((name or "").strip().lower(), "")


def fissure_tier(modifier: str) -> tuple[str, int]:
    """VoidT1..T6 -> ("Lith", 1) 等；未知返回原文与 0。"""
    m = re.match(r"VoidT(\d)", modifier or "")
    table = {"1": ("Lith", 1), "2": ("Meso", 2), "3": ("Neo", 3),
             "4": ("Axi", 4), "5": ("Requiem", 5), "6": ("Omnia", 6)}
    if m and m.group(1) in table:
        return table[m.group(1)]
    return (modifier or "?", 0)


def language_text(key: str) -> tuple[str, str]:
    """languages.json 查询：完整路径或裸键精确匹配（不做模糊，避免错配多语言串）。"""
    if not key:
        return "", ""
    table = _load("languages.json") or {}
    hit = table.get(key)
    if hit is None:
        hit = table.get(key.lower())          # DE 给的键多为驼峰，表键是小写
    if hit is None and "/" in key:
        hit = table.get(key.rstrip("/").rsplit("/", 1)[-1])
    if isinstance(hit, dict):
        return hit.get("value", ""), hit.get("desc", "")
    if isinstance(hit, str):
        return hit, ""
    return "", ""


# ★ 2026-09-28 §一：`[PH]` = DE 的开发中占位符（PlaceHolder），**不是可用文案**。
#   实测官方全量包 140,264 键里 **1508** 条值以 `[PH]` 开头（严格大写；
#   大小写不敏感 **1509** —— 多出的那条 `/Lotus/Language/Upgrades/
#   AntiqueHeatStatusProcOnUltimateKillName` 写作 `[ph]`，所以判据必须大小写不敏感）。
#   其中落在**运行期卡面表**的有：`zh_ext.json` **87** 条（1999 / Items / Weapons /
#   CircleOfHell 四命名空间）、`languages_zh.json` **16** 条 ⇒ 旧实现会把
#   `[PH] Lunaro Sugatra`、`[PH] Avoid the Lava` 这种直上卡。
#   DE 自己在补丁说明里修过「PH 标签出现在游戏 HUD」的缺陷 ⇒ 这是真会漏的类型。
#   修法：**查询封装层一处收口** —— 取到的值以 `[PH]` 开头（大小写不敏感、容忍前导空白）
#   一律**视为无键**，继续走回退链，最终返回空串（调用方自然会落英文 + 注脚）。
# ---------------------------------------------------------------------------
# 官方值里的「占位符 / 模板标记」—— **单一真源**（★ 2026-09-28 §二 扩）
# ---------------------------------------------------------------------------
# 分两类语义，判据**集中在这里定义**（不要再散落到各调用点）：
#
# ① **不可用**：`_is_placeholder()` 命中 ⇒ **视同无键**走回落链（宁可落英文，
#    也不许把带标记的半成品印上卡）。
#      · `[PH] …`  —— DE 开发中占位符（大小写不敏感；官方全量包 **1508** 条）
#      · `|val|`   —— **变量槽未填**。例：`PersonalMod_ShieldDelay_Desc`
#        =「护盾充能延迟增加 |val|%。」（2026-09-28 审核线上实测，漏到深层科研卡上）
#        ⚠️ 判据是「**段内含小写字母**（`any(ch.islower())`）」，这是为了**不误伤**：
#          · `|COLOR_POS|` / `|COLOR_END|` / `|COUNT|` / `|STAT1|`（纯大写/数字族）**不算不可用** ——
#            见 ② 与下面的反例护栏；若按「忽略大小写」一律判不可用，
#            `CoHHarrowEximusDamageBoost_Desc`（含 `|COLOR_POS|100%|COLOR_END|`）
#            会被整条弃掉，正文「…增加 100%。」就丢了（违反 2026-09-28 批 §二.2 的保留要求）。
#          · `|1|`（`|OPEN_COLOR|1|CLOSE_COLOR|` 里的数字段）也**不算**（无字母）。
# ② **可清洗**：`_clean_lang_text()` 剥掉标记、**保留正文**（不许因为含标记就整条弃掉）。
#      · `<SHARD_ORANGE_SIMPLE>` 图标占位符 ⇒ 剥后「黄玉执刑官源力石」仍是好文案
#      · `|COLOR_POS|…|COLOR_END|` 富文本       ⇒ 剥后「…100%…」仍是好文案
#      · `|STAT1|` **数字槽**（★ 2026-09-28 二批裁定并入可清洗）—— 与 `|COUNT|` 同族：
#        调用方有可能填数（`|COUNT|` 由 `_cal_objective` 填），没填的把槽剥掉、**留正文**；
#        不升级为「不可用」（否则整条描述丢失，比「剥后残句」更糟）。
#        ⚠️ 顺序陷阱：`|COUNT|` 是**纯字母**段，`_DIGIT_SLOT_RE` 不含纯字母 ⇒ 天然不受影响
#        （测试里有「填数发生在清洗之后」的实测断言守这条）。
_PH_PREFIX = "[ph]"
# 任意 `|…|` 段（长度设上限，避免病态匹配）
_ANY_SLOT_RE = re.compile(r"\|[^|<>]{1,24}\|")
# 可清洗的标记（②）：尖括号图标占位符 + 纯大写富文本 `|COLOR_*|` + 大写数字槽 `|STAT1|`
_ANGLE_TAG_RE = re.compile(r"<[^<>]{1,40}>")
_COLOR_TAG_RE = re.compile(r"\|[A-Z_]{2,20}\|")
# 数字槽：大写+数字（`|STAT1|` / `|BONUS2|` / `|OPEN_COLOR1|` / `|1|`）。
# **不含纯字母段** ⇒ `|COUNT|` / `|DURATION|` 这类「调用方填数槽」不会被误剥（见上方 ⚠️）。
_DIGIT_SLOT_RE = re.compile(r"\|[A-Z_]*\d[A-Z_]*\|")


def _is_placeholder(value: str) -> bool:
    """值是否**不可用**（命中 ⇒ 视同无键，走回落链）。

    判据两条（见上方 ① 的说明；**判据按数据实测得出，不是拍脑袋的大小写规则**）：
      · `[PH]` 前缀（大小写不敏感）；
      · **含小写字母的 `|…|` 段** = 未填的变量槽 —— 覆盖 `|val|`（下划线小写族）
        与 `|NumAvatarsInside|`（驼峰族，`CircleOfHell/HUD_PlayersWaitingOnYou` 里实测有）。
    纯大写 / 数字段（`|COUNT|`、`|STAT1|`、`|COLOR_POS|`、`|1|`）是**可替换或可剥**
    的格式标记，**不算不可用** —— 详见下方两条反例护栏（`CoHHarrowEximusDamageBoost_Desc`
    的正文「…100%。」与 `|OPEN_COLOR|1|CLOSE_COLOR|` 里的数字段都必须留住）。
    """
    s = (value or "").lstrip()
    if s.lower().startswith(_PH_PREFIX):
        return True
    return any(any(ch.islower() for ch in m.group(0))
               for m in _ANY_SLOT_RE.finditer(s))


def language_text_zh(key: str) -> str:
    """DE 官方简中词表查询（``languages_zh.json``，来自官方导出 dict.zh）。

    国际服的简体中文就是这一套：Grineer / Corpus / Infested 保留英文，
    奥罗金 / 低语者 / 合一众 / 炽蛇军 / 科腐者 有官方中文译名。

    Args:
        key: 语言键，如 ``/Lotus/Language/1999Challenges/...``。

    Returns:
        中文串；查不到返回空串（调用方自己决定回落到英文还是美化路径）。
        `[PH] …` 占位符视为查不到（见 :func:`_is_placeholder`）。
    """
    if not key:
        return ""
    table = _load("languages_zh.json") or {}
    ext = _load("zh_ext.json") or {}          # 构建期提取的补充表（见 scripts/build_zh_ext.py）
    # 候选顺序：完整路径 → 小写 →（兼容位）裸尾段。命中即返回**非占位符**的值。
    cands = [key, key.lower()]
    if "/" in key:
        # ⚠️ 尾段这一级是**死代码**（2026-09-27 审核实测 + 我复核）：两张表
        #    （本地 36k + zh_ext 7,471）里**无斜杠键均为 0** ⇒ 传 tail 永远查不到。
        #    保留仅为「将来若有人把表改成短键」的兼容位；**调用方请一律传完整路径**
        #    （跨命名空间回退要**显式枚举候选全路径**，别指望这里兜）。
        tail = key.rstrip("/").rsplit("/", 1)[-1]
        cands.append(tail)
        if tail.endswith("Name"):
            cands.append(tail[:-4])           # `…NameNoIcon` → `…Name` → `…`
    for tb in (table, ext):                   # 本地 36k 优先，补充表兜底
        for cand in cands:
            hit = tb.get(cand)
            if isinstance(hit, str) and hit and not _is_placeholder(hit):
                return _clean_lang_text(hit)
    return ""


# 卡面清洗（★ 2026-09-27 审核 §一/§五.3；★ 2026-09-28 §二 与 `_is_placeholder` 并到同一处）：
#   · 图标占位符：`<SHARD_ORANGE_SIMPLE>黄玉执刑官源力石`
#   · 富文本着色标记：`|COLOR_POS|` … `|COLOR_END|`
#   · 多行描述里的 CRLF
# 提取表里**保留原文**（便于排错），清洗统一放在这里做。
# ⚠️ 这里用的三个 `_*_RE` 与不可用判据 `_is_placeholder()` 的 `_ANY_SLOT_RE`
#    **定义在同一处**（文件顶部「占位符/模板标记单一真源」块）——
#    两类语义（剥 / 弃）分得很清楚，别把某一类挪走。


def _clean_lang_text(s: str) -> str:
    """官方值 → 可直接上卡的一行文本（剥占位符 / 富文本标记，压平换行）。

    只剥**可清洗**类标记（见顶部 ②）：尖括号图标占位符、`|COLOR_*|` 富文本、
    **大写数字槽** `|STAT1|`；**变量槽**（`|val|`）不在这里剥 —— 那是「不可用」类，
    由 :func:`_is_placeholder` 在查询层拦掉（剥掉会留下「%」这种半成品）。

    ⚠️ 剥数字槽会留下「残句」（如 `…增加 % 技能强度`），这是**已裁定的可接受代价**
    （对面是「整条描述丢失」）；实测这些键当前都不上卡，若哪天接线显示需重新评估。
    """
    s = _ANGLE_TAG_RE.sub("", s or "")
    s = _COLOR_TAG_RE.sub("", s)
    s = _DIGIT_SLOT_RE.sub("", s)
    for _ch in (chr(13), chr(10)):
        s = s.replace(_ch, " ")
    return " ".join(s.split())


def text_cn(key: str, *, fallback: str = "") -> str:
    """官方简中优先、英文次之、给定兜底。"""
    return language_text_zh(key) or language_text(key)[0] or fallback


@lru_cache(maxsize=1)
def _challenge_table() -> dict:
    return _load("challenges_zh.json") or {}


def challenge_zh(path: str) -> dict:
    """挑战资产路径 -> ``{"name", "desc", "count"}``（官方简中，已解析）。"""
    return _challenge_table().get(path) or {}



_STRIP_STORE = re.compile(r"/StoreItems(?=/)", re.I)
# 纯现金资产：/Lotus/StoreItems/Types/PickUps/Credits/50000Credits -> 50000 现金
_CREDIT_PATH = re.compile(r"^(\d[\d,]*)\s*Credits?$", re.I)
# Prime 重生（Varzia）组合包：资产名 MPVBansheePrimeSinglePack -> 语言键
#   /Lotus/Language/PrimePacks/MPVBansheeSinglePackName
# （DE 在语言键里去掉了 "Prime" 中缀，必须显式还原，否则这些包会被整条跳过）
_PRIME_PACK_RE = re.compile(r"^(MPV)(.+?)(Prime)?(SinglePack|DualPack)$")


def _prime_pack_name(path: str) -> Optional[str]:
    tail = (path or "").rstrip("/").rsplit("/", 1)[-1]
    if not tail.startswith("MPV"):
        return None
    cands = [tail, tail.replace("Prime", "")]
    m = _PRIME_PACK_RE.match(tail)
    if m:
        cands.insert(0, f"{m.group(1)}{m.group(2)}{m.group(4)}")
    for c in cands:
        for suf in ("Name", ""):
            hit = language_text_zh(f"/Lotus/Language/PrimePacks/{c}{suf}")
            if hit:
                return hit
    return None


# ★ 手工译名补漏：DE 导出表里确实没有的字符串（2026-09-18 起逐条登记）。
#   按**资产路径尾段**（小写）匹配，对 /StoreItems 前缀变体免疫。
#   放在官方词库之后、英文回退之前 —— DE 将来在导出里补了官方译名，
#   官方值会自动优先，这里的条目自然让位（留着不碍事）。
MANUAL_ITEM_NAMES = {
    # Inaros 之沙任务蓝图：官方任务键 /lotus/types/keys/mummyquest/
    # mummyquestkeychain 译「Inaros 之沙」（name_zh.json），但蓝图本身
    # （MummyQuestKeyBlueprint）不在任何导出表 → 奸商卡曾显示英文原名。
    "mummyquestkeyblueprint": "Inaros 之沙蓝图",
}


def item_name_opt(path: str) -> Optional[str]:
    """DE 资产路径 -> 中文/英文名；词库无覆盖时返回 None（不编造）。"""
    return item_name_opt_ex(path)[0]


# 「蓝图」类资产：DE 官方词表**只有基名**、没有「…Blueprint」这一条
# （实测 `OrokinCatalystBlueprint` / `OrokinReactorBlueprint` 在本地 36k、
#  zh_ext 7,471、18MB 全量 140,264 三处全查无；`/Lotus/Language/Items/OrokinCatalyst`
#  = 奥罗金催化剂 则是有的）。按维护者 2026-09-27 裁定：官方基名 + 「蓝图」**合成**，
#  并在卡面注脚标明是合成译名 —— 「官方没有的写法 ≠ 不存在的东西」，
#  但也不许把合成结果冒充游戏内原文。
_ITEM_BLUEPRINT_MARK = "Blueprint"
_ITEM_BLUEPRINT_SUFFIX = "蓝图"
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def _blueprint_synth(path: str) -> Optional[str]:
    """``…Blueprint`` 资产 -> 官方基名 + 「蓝图」；基名拿不到官方中文时返回 None。

    基名查的是**官方语言表**（4 个卡面命名空间 × 尾段归一化），不是资产名表 ——
    资产树是 `/Lotus/Types/Recipes/Components/OrokinCatalyst`，语言键却在
    `/Lotus/Language/Items/OrokinCatalyst`，两边路径对不上，只能按尾段找。
    只接受**官方中文**基名：英文基名（如 `Orokin Catalyst`）拼上「蓝图」是硬编。
    """
    if not path.endswith(_ITEM_BLUEPRINT_MARK):
        return None
    base = path[:-len(_ITEM_BLUEPRINT_MARK)]
    tail = base.rstrip("/").rsplit("/", 1)[-1]
    if not tail:
        return None
    _key, zh = _card_lang_by_tail(tail)
    if zh:
        return zh + _ITEM_BLUEPRINT_SUFFIX
    # 语言表没覆盖时再看本地物品词表（同样要求是中文）
    name = _item_name_raw(base)
    if name and _CJK_RE.search(name):
        return name + _ITEM_BLUEPRINT_SUFFIX
    return None


def item_name_opt_ex(path: str) -> tuple[Optional[str], bool]:
    """同 :func:`item_name_opt`，另给「是否合成译名（非游戏内原文）」标记。

    调用方（卡面）应在 ``synthetic`` 为真时加注脚说明，别让用户以为是官方文案。
    """
    if not path:
        return (None, False)
    synth = _blueprint_synth(path)
    if synth:
        return (synth, True)
    return (_item_name_raw(path), False)


def _item_name_raw(path: str) -> Optional[str]:
    """官方词库 -> 英文名 -> 路径美化（不含「蓝图」合成）。"""
    if not path:
        return None
    key = path.strip().lower()
    zh = _load("name_zh.json")
    name = zh.get(key)
    # ``/Lotus/StoreItems/Types/...`` 与 ``/Lotus/Types/StoreItems/...`` 两种写法都存在，
    # 而中文词库只收录其中一种。逐个剥掉 ``/StoreItems`` 段（只剥一层 / 全剥）都试一遍，
    # 否则会出现该命中却不命中、退化成英文路径美化的假象。
    if not name and "/storeitems/" in key:
        cands = [key]
        parts = [p for p in key.split("/") if p]
        for i, seg in enumerate(parts):
            if seg.lower() == "storeitems":
                cands.append("/" + "/".join(parts[:i] + parts[i + 1:]))
        cands.append(_STRIP_STORE.sub("", key))
        for c in dict.fromkeys(cands):
            if zh.get(c):
                name = zh[c]
                break
    if name:
        return name
    pack = _prime_pack_name(path)
    if pack:
        return pack
    tail = path.rstrip("/").rsplit("/", 1)[-1]
    if tail.lower() in MANUAL_ITEM_NAMES:
        return MANUAL_ITEM_NAMES[tail.lower()]
    m = _CREDIT_PATH.match(tail)
    if m:
        return f"{int(m.group(1).replace(',', '')):,} 现金"
    # 内融核心捆包（UncommonFusionBundle 等）变体很多但官方统称「内融核心」
    if tail.lower().endswith("fusionbundle"):
        return language_text_zh("/Lotus/Language/Items/FusionBundle") or "内融核心"
    en, _ = language_text(path)
    if en:
        m2 = _CREDIT_PATH.match(en)
        if m2:
            return f"{int(m2.group(1).replace(',', '')):,} 现金"
        return en
    return None


def item_name(path: str) -> str:
    """DE 资产路径 -> 展示名。多级回退：中文词库 -> 英文名 -> 路径美化。

    worldState 里的路径多是 `/Lotus/StoreItems/...`，而中文词库（name_zh.json，
    取自官方导出）里多为 `/Lotus/Types/...`、`/Lotus/Weapons/...`，
    因此需要剥离 `/StoreItems` 段后重试。
    """
    return item_name_opt(path) or _prettify(path)


def node_cn(key: str) -> str:
    """SolNode/CrewBattleNode 等节点键 -> 中文节点名（带星球）。"""
    if not key:
        return "?"
    node = sol_node(key)
    if node:
        return _node_name(key)
    # 导出词库里的 Regions 也带节点名
    reg = _load("name_zh.json").get(key.lower())
    return reg or key


# 派系显示名。
#
# ⚠️ 直接用 **DE 官方导出的 Faction 表**（build_de_data.py 由 ExportFactions +
#    官方简中词表重建 factionsData.json），不再手工维护例外表：
#      Grineer / Corpus / Infestation / SENTIENT 官方保留英文；
#      奥罗金 / 合一众 / 低语者 / 炽蛇军 / 科腐者 / 血色面纱 官方有中文译名。
#    （旧实现硬编 FC_MITW -> 墙中人，与官方表的「低语者」不一致；
#      FC_SCALDRA / FC_TECHROT 也因缺少例外表条目而回落成英文。）
# 逐条对照数据与禁用词表见 tests/test_translation_locale.py。
_FACTION_CN: dict[str, str] = {}


def faction_name(code: str) -> str:
    """派系显示名。

    优先取官方简中表（``factionsData.json``）；表里没有的代码再回落到英文原值，
    与游戏内客户端显示一致（不会自己拼中文）。

    Args:
        code: 派系代码（如 ``FC_GRINEER``）或已是英文名（如 ``Grineer``）。

    Returns:
        显示用派系名。
    """
    if code in _FACTION_CN:
        return _FACTION_CN[code]
    v = (_load("factionsData.json") or {}).get(code)
    if isinstance(v, dict):
        return _FACTION_CN.get(v.get("value", ""), v.get("value", code))
    return code


def sortie_boss(code: str) -> str:
    v = (_load("sortieData.json") or {}).get("bosses", {}).get(code)
    if isinstance(v, dict):
        return v.get("name", code)
    return code


def sortie_modifier(code: str) -> str:
    return (_load("sortieData.json") or {}).get("modifierTypes", {}).get(code, code)


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------


def _ms(value: Any) -> Optional[int]:
    """{"$date": {"$numberLong": "..."}} / 秒级整数 -> 毫秒时间戳。"""
    if value is None:
        return None
    if isinstance(value, dict):
        value = (value.get("$date") or {}).get("$numberLong")
    if value is None:
        return None
    try:
        num = int(value)
    except (TypeError, ValueError):
        return None
    if num < 10 ** 12:  # 秒级
        num *= 1000
    return num


def _iso(ms: Optional[int]) -> str:
    if ms is None:
        return ""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def _iso_of(raw: dict, key: str) -> str:
    return _iso(_ms((raw or {}).get(key)))


def _oid(raw: Any) -> str:
    if isinstance(raw, dict):
        inner = raw.get("_id") or raw.get("$oid")
        if isinstance(inner, dict):
            inner = inner.get("$oid")
        return str(inner or "")
    return str(raw or "")


# 星球/区域中文名（点位名保留英文，仅汉化括号内的星球）
PLANET_CN = {
    "Earth": "地球", "Venus": "金星", "Mercury": "水星", "Mars": "火星",
    "Phobos": "火卫一", "Ceres": "谷神星", "Jupiter": "木星",
    "Europa": "欧罗巴", "Saturn": "土星", "Uranus": "天王星",
    "Neptune": "海王星", "Pluto": "冥王星", "Sedna": "赛德娜",
    "Eris": "阋神星", "Deimos": "火卫二", "Lua": "月球", "Void": "虚空",
    "Veil": "面纱", "Kuva Fortress": "赤毒要塞", "Zariman": "扎里曼",
    "Duviri": "双衍王境", "Ambulas": "安布拉斯",
}
_PLANET_RE = re.compile(r"\(([^)]+)\)\s*$")


def _node_name(node_key: str) -> str:
    """节点显示名，优先用**官方中文节点表**。

    ``nodes_zh.json`` 来自 DE 官方本地化导出（ExportRegions_zh），里面
    ``奥布山谷 / 夜灵平野 / 神王塔`` 这类才是官方译名；而 ``solNodes.json``
    的 value 是英文原值（``Orb Vallis (Venus)``），只能汉化括号里的星球。
    官方表里没有的 key 再回落到 solNodes 的老逻辑。

    Args:
        node_key: DE 节点 key，如 ``SolNode129``。

    Returns:
        ``节点名（星球）`` 形式的显示名。
    """
    zh = nodes_zh().get(node_key)
    if zh and zh.get("name"):
        sysname = zh.get("system") or ""
        return f"{zh['name']}（{sysname}）" if sysname else zh["name"]
    info = sol_node(node_key)
    name = info.get("value") or node_key
    if node_key.endswith("HUB"):  # 中继站/中枢不带敌派后缀
        return name
    suffix = ""
    m = _PLANET_RE.search(name)
    if m and m.group(1).strip() in PLANET_CN:
        planet = PLANET_CN[m.group(1).strip()]
        name = name[:m.start()].rstrip() + f"（{planet}）"
    return name + suffix


def _prettify(path: str) -> str:
    """/Lotus/... 路径 -> 人类可读兜底名（缩写词不拆分，如 PRIMETIME -> PRIMETIME）。"""
    if not path:
        return "?"
    tail = path.rstrip("/").rsplit("/", 1)[-1]
    tail = re.sub(r"^(StoreItems|Types|Items|Challenges|Game|Gameplay|Levels|Scripts)", "", tail)
    words = re.findall(r"[A-Z]+(?=[A-Z][a-z0-9]|\b)|[A-Z][a-z0-9]*|[a-z0-9]+", tail)
    return " ".join(words) if words else tail


# 官方文案里的富文本标记（<DT_FIRE_COLOR> 等）与换行，展示前一律剥掉
_DT_TAG_SUB = re.compile(r"<[^<>]{0,40}>")


# ---------------------------------------------------------------------------
# 周期算法（与 wfcd warframe-worldstate-parser 对齐）
# ---------------------------------------------------------------------------


def _fmt_left(ms_left: int) -> str:
    """毫秒差 → 「Xh Ym / Xm Ys / Xs」。

    ★ 2026-09-27（审核 §三）：**负值不再静默钳成 0**。旧写法 ``max(0, …)`` 把
    已过期条目显示成「剩 0s」—— 线上实测 6 条风暴显示 0s，实为 DE 保留下发的
    **上一批已结束**条目（审核拉 worldState 钉死：12 条 = 两批、各 6 条、存活
    90 分钟、相隔 60 分钟，前一批已过期 8 分钟）。负值一律返回「已结束」，
    由调用方决定过滤（`_parse_void_storms` 已过滤）或标注。
    """
    if ms_left <= 0:
        return "已结束"
    secs = ms_left // 1000
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def earth_cycle(cetus: dict) -> dict:
    """地球昼夜 —— 与夜灵平野**共用一个周期**，不是另算的 8 小时循环。

    依据：browse.wf/live 把这两者画在**同一行**（"夜灵平野 / 地球"，挂同一个
    expiry 徽章）；夜灵平野本就位于地球，自转相位天然一致。

    ⚠️ 旧实现自己走 ``(now_s % 28800)`` 的 8 小时循环（4h 白天 / 4h 夜晚），
       于是同一张卡上会同时出现「夜灵平野：白天 剩 1h8m」和
       「地球：夜晚 剩 3h39m」——两条自相矛盾的记录，实测与 browse.wf 对不上。
    """
    return {"state": cetus.get("state", "day"),
            "isDay": cetus.get("isDay", True),
            "expiry": cetus.get("expiry", ""),
            "timeLeft": cetus.get("timeLeft", "")}


def cetus_cycle(bounty_end_ms: Optional[int], now_ms: int) -> dict:
    """夜灵平野：希图斯赏金截止 = 当前期相结束；剩 >50 分钟为白天。

    与 browse.wf 的 ``cycleNightStart = bountyExpiry - 50min`` 一致。
    ⚠️ 不要对 expiry 做「截断到分钟」：DE 下发的是毫秒级（如 ``...:44.656``），
       截断会平白引入最多 59 秒误差，卡面倒计时与游戏内 / browse.wf 就对不上。
    """
    if not bounty_end_ms:
        return {"state": "day", "isDay": True, "expiry": "",
                "timeLeft": "?", "_degraded": True}
    secs_left = (bounty_end_ms - now_ms) // 1000
    if secs_left <= 0:
        secs_left = 3600
    day = secs_left > 3000                     # 50min 夜晚
    left = (secs_left - 3000) * 1000 if day else secs_left * 1000
    return {"state": "day" if day else "night", "isDay": day,
            "expiry": _iso(now_ms + left), "timeLeft": _fmt_left(left)}


def vallis_cycle(now_ms: int) -> dict:
    loop, cold = 1600, 1200                    # 26min40s 循环：冷 20min / 暖 6min40s
    epoch = int(datetime(2026, 2, 4, 19, 46, 48, tzinfo=timezone.utc)
                .timestamp() * 1000)
    since = (now_ms - epoch) % (loop * 1000)
    to_full = loop * 1000 - since
    warm = to_full > cold * 1000               # 暖期在循环尾段
    left = to_full - cold * 1000 if warm else to_full
    return {"state": "warm" if warm else "cold", "isWarm": warm,
            "expiry": _iso(now_ms + left), "timeLeft": _fmt_left(left)}


# 双衍王境情绪（螺旋）：官方简中译名取自 oracle 补充词表
#   /Lotus/Language/Duviri/{Sad,Scared,Happy,Angry,Jealous}MoodTitleShort
# 顺序与 DE 的 5 值循环一致（悲伤→恐惧→喜悦→愤怒→嫉妒），每相 2 小时。
DUVIRI_STATES = ["Sorrow", "Fear", "Joy", "Anger", "Envy"]
DUVIRI_STATES_CN = {"Sorrow": "悲伤", "Fear": "恐惧", "Joy": "喜悦",
                    "Anger": "愤怒", "Envy": "嫉妒"}


def duviri_mood_cn(mood: str) -> str:
    """Sorrow -> 悲伤（未知值原样返回）。"""
    return DUVIRI_STATES_CN.get(mood, mood)


# 1999 日历「增益覆写」条目名 —— **兜底表**（主路径查官方键）。
#
# 旧注释「DE 的简中导出里没有这一批字符串」是**错的**：`/Lotus/Language/1999/`
# 官方有 **716** 键，每条覆写都有 `<尾段>Name`（如
# `/Lotus/Language/1999/MeleeCritChanceName` = 熟能生巧）。以前落英文是因为本地
# 36k 表没收录这个命名空间，不是因为 DE 没给。
#
# 现在主路径 = `/Lotus/Upgrades/Calendar/<尾段>` -> `/Lotus/Language/1999/<尾段>Name`
# -> `<尾段>`（归一化、大小写/分隔符不敏感）；本表降级为**兜底**，
# 且**表内每条都必须与官方值逐字一致**（测试复算）。
#
# ★ 2026-09-27 按官方值订正 **15** 条（原手编与官方不符）：
#   Armor 皮糙肉厚→硬化装甲 ｜ AttackAndMovementSpeedOnCritMelee 一触即散→快刀斩乱麻
#   BlastEveryXShots 爆破狂欢→爆炸派对 ｜ CompanionDamage 坚实后盾→好兄弟
#   CompanionsRadiationChance 友方辐射→辐射支援 ｜ EnergyRestoration 浓缩能量→特浓咖啡
#   EnergyWavesOnCombo 连击冲击波→波动连段 ｜ FinisherChancePerComboMultiplier 连击杀手→杀意连段
#   GasChanceToPrimaryAndSecondary 剧毒射击→毒气弹头 ｜ MagnetStatusPull 引力牵引→吸引力
#   MagnitizeWithinRangeEveryXCasts 磁力威胁→磁场威胁 ｜ OvershieldCap 硬化护盾→硬质化
#   PowerStrengthAndEfficiencyPerEnergySpent 力量压制→能量过载 ｜ PunchToPrimary 穿孔卡→打孔纸带
#   RadiationProcOnTakeDamage 心灵反馈→精神反击
#
# ★ 2026-09-28 §四：**本轮起官方查表已覆盖现行覆写池全部 38 条**（可由
#   `_calendar_upgrade_zh()` 逐条复算，0 未解出）⇒ 本表**仅作离线/查表失败兜底**，
#   新增条目**一律先与官方对拍**再写进来。38 条全表见审核产物
#   `1999取证-20260928/1999覆写池-38条-code与官方简中.md`。
CAL_UPGRADE_CN = {
    "/Lotus/Upgrades/Calendar/MeleeCritChance": "熟能生巧",
    "/Lotus/Upgrades/Calendar/EnergyOrbToAbilityRange": "极限拓展",
    "/Lotus/Upgrades/Calendar/HealingEffects": "应急特效药",
    "/Lotus/Upgrades/Calendar/OrbsDuplicateOnPickup": "靶向治疗",
    "/Lotus/Upgrades/Calendar/RefundBulletOnStatusProc": "随意开火",
    "/Lotus/Upgrades/Calendar/SharedFreeAbilityEveryXCasts": "有福同享",
    "/Lotus/Upgrades/Calendar/MeleeAttackSpeed": "毫不留情",
    "/Lotus/Upgrades/Calendar/AbilityStrength": "力量飙升",
    "/Lotus/Upgrades/Calendar/MagazineCapacity": "重型弹匣",
    "/Lotus/Upgrades/Calendar/ElectricStatusDamageAndChance": "瓶装闪电",
    "/Lotus/Upgrades/Calendar/CompanionsBuffNearbyPlayer": "人多势众",
    "/Lotus/Upgrades/Calendar/GenerateOmniOrbsOnWeakKill": "强制输血",
    "/Lotus/Upgrades/Calendar/Armor": "硬化装甲",
    "/Lotus/Upgrades/Calendar/AttackAndMovementSpeedOnCritMelee": "快刀斩乱麻",
    "/Lotus/Upgrades/Calendar/BlastEveryXShots": "爆炸派对",
    "/Lotus/Upgrades/Calendar/CompanionDamage": "好兄弟",
    "/Lotus/Upgrades/Calendar/CompanionsRadiationChance": "辐射支援",
    "/Lotus/Upgrades/Calendar/EnergyRestoration": "特浓咖啡",
    "/Lotus/Upgrades/Calendar/EnergyWavesOnCombo": "波动连段",
    "/Lotus/Upgrades/Calendar/FinisherChancePerComboMultiplier": "杀意连段",
    "/Lotus/Upgrades/Calendar/GasChanceToPrimaryAndSecondary": "毒气弹头",
    "/Lotus/Upgrades/Calendar/MagnetStatusPull": "吸引力",
    "/Lotus/Upgrades/Calendar/MagnitizeWithinRangeEveryXCasts": "磁场威胁",
    "/Lotus/Upgrades/Calendar/OvershieldCap": "硬质化",
    "/Lotus/Upgrades/Calendar/PowerStrengthAndEfficiencyPerEnergySpent": "能量过载",
    "/Lotus/Upgrades/Calendar/PunchToPrimary": "打孔纸带",
    "/Lotus/Upgrades/Calendar/RadiationProcOnTakeDamage": "精神反击",
    # —— 手编表原先漏掉的 4 条（本期线上/常见轮换里出过，官方键都在）——
    "/Lotus/Upgrades/Calendar/RadialJavelinOnHeavy": "重型标枪",
    "/Lotus/Upgrades/Calendar/ElectricalDamageOnBulletJump": "电能飞跃",
    "/Lotus/Upgrades/Calendar/MeleeSlideFowardMomentumOnEnemyHit": "斩筋断骨",
    "/Lotus/Upgrades/Calendar/StatusChancePerAmmoSpent": "累积弹匣",
}

# 官方**确实没有**键的覆写条目 —— 逐条实测（本地 36k + zh_ext 7,471 + 18MB 全量包
# 140,264 键三处全查无，含大小写/分隔符归一化搜索）⇒ 卡面留英文 + 注脚，不硬编。
# 例：GuidingMissilesChance 的官方英文名是 `Tickshots`（languages.json 有），简中无。
#
# ★ 2026-09-28 §五：`GuidingMissilesChance` 已**不在现行覆写池**——
#   · 官方包搜 `guidingmissiles` = **0 键**；
#   · wiki Hex Override 全表（**38 条**，见 `1999取证-20260928/1999覆写池-38条-code与官方简中.md`）
#     里**也没有**这一条。
#   ⇒ 判定：**已废弃 / 非现行池**（DE 无对应文案，wiki 池亦无），保留清单只为
#     「万一 worldstate 又下发它」时不硬编。
#   卡面行为（已实测确认）：`_calendar_upgrade_zh()` 返回 `""`，且它**不在** `CAL_UPGRADE_CN`
#   ⇒ 落 `fmt._cal_name(_prettify(tail))` = 英文 `Guiding Missiles Chance`，并计入
#     日历卡的 `noKey` 注脚（`※ 官方无简中、保留原文：…`）——即「留英文 + 注脚」，可接受。
CAL_UPGRADE_NO_KEY = frozenset({
    "/Lotus/Upgrades/Calendar/GuidingMissilesChance",
})


def duviri_cycle(now_ms: int) -> dict:
    """双衍王境螺旋（情绪）周期。

    与 browse.wf 的算法一致：``idx = floor(now_ms / 2h) % 5``（无需额外锚点偏移）。
    旧实现用 ``(now_s - 52) % 36000 // 7200``，在每小时的头 52 秒内会错相，
    且状态名直接输出英文（Fear 等），与游戏内「恐惧」不一致。

    Returns:
        ``{"state", "spiral", "stateCn", "choices", "expiry", "timeLeft"}``；
        ``choices`` 为当前及后续共 3 相（与 warframestat 的 duviriCycle.choices 对齐）。
    """
    period = 7200 * 1000                       # 2 小时
    idx = (now_ms // period) % len(DUVIRI_STATES)
    left = period - (now_ms % period)
    state = DUVIRI_STATES[idx]
    choices = [DUVIRI_STATES[(idx + i) % 5] for i in range(3)]
    # spiral 字段只存英文状态名，中文名由 stateCn 提供；
    # 后缀由 fmt_cetus 统一加，避免「Sorrow Spiral 螺旋」重复拼。
    return {"state": state, "spiral": state,
            "stateCn": duviri_mood_cn(state),
            "choices": choices,
            "choicesCn": [duviri_mood_cn(c) for c in choices],
            "expiry": _iso(now_ms + left), "timeLeft": _fmt_left(left),
            "leftMs": left}


# 扎里曼号派系轮换（Grineer / Corpus）。
#   · 节拍 = 赏金周期 150 分钟 —— 实测 `ZarimanSyndicate.Expiry` 与
#     `CetusSyndicate.Expiry` **逐毫秒相同**（如 2026-09-12 06:19:44.656Z），
#     browse.wf 也是拿同一个 `bounty-cycle.expiry` 画它的倒计时。
#   · 派系没有独立字段，而是由 **`Seed` 的最低位**决定。这是 browse.wf oracle
#     的做法（`wf.browse.oracle/bounty-cycle.pluto`，取自游戏脚本
#     `SyndicateMissionGenerator.lua` 的 CheckFaction 判据）：
#         zarimanFaction = {"FC_GRINEER","FC_CORPUS"}[(Seed & 1) + 1]
#     实测校验：当前周期 Seed=34370（偶）-> oracle 报 FC_GRINEER ✔
#              2026-09-09 抓包 Seed=59621（奇）-> FC_CORPUS ✔
#   · ⚠️ 不要拿 CetusSyndicate 的 Seed 来算：同一周期内它可能与 Zariman 的**不同**
#     （实测 59620 vs 59621），用错会把派系整体反掉。
#   · ⚠️ 也不要改成「周期序号奇偶」的推算 —— Seed 由 DE 每周期直接下发，
#     自校准、永不漂移；推算版本一旦遇到维护/版本更新重置周期链就会错。
_ZARIMAN_STATES = ("grineer", "corpus")


def zariman_cycle(seed: Optional[int], expiry_ms: Optional[int],
                  now_ms: int) -> dict:
    """扎里曼号：Grineer / Corpus 派系轮换（周期同赏金，150 分钟一换）。

    Args:
        seed: ``ZarimanSyndicate.Seed``，最低位 0 = Grineer / 1 = Corpus。
        expiry_ms: 该周期的结束时间戳（毫秒）。
        now_ms: 当前时间戳（毫秒）。
    """
    if not expiry_ms:
        return {}
    state = _ZARIMAN_STATES[(int(seed or 0) & 1)]
    left = max(0, expiry_ms - now_ms)
    return {"state": state,
            "stateCn": "Grineer" if state == "grineer" else "Corpus",
            "expiry": _iso(expiry_ms), "timeLeft": _fmt_left(left)}


# ---------------------------------------------------------------------------
# 各结构解析
# ---------------------------------------------------------------------------


def _synth_table() -> list[dict]:
    import json as _json
    from pathlib import Path as _P
    try:
        return _json.loads((_P(__file__).parent / "data" / "synth_targets.json")
                           .read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


_ZH_SYNONYM = {}  # 简-繁/异体占位


def _parse_synth(raw: dict) -> list[dict]:
    """结合仪式目标（DE 源无此项，用 WFCD 静态表带出）。

    静态表的 mission/planet/faction 都是英文，这里补齐官方中文：
    节点名取 ``nodes_zh.json``（DE 官方导出），星球与派系走各自的中文表。
    """
    table = _synth_table()
    out = []
    for en, locs in table.items():
        first = (locs or [{}])[0]
        mission_en = first.get("mission", "")
        planet_en = first.get("planet", "")
        key = node_key_of(mission_en, planet_en)
        out.append({
            "name": en,
            "planet": PLANET_CN.get(planet_en, planet_en),
            "mission": mission_en,
            "node": _node_name(key) if key else mission_en,
            "type": first.get("type", ""),
            "faction_en": first.get("faction", ""),
            "faction": faction_name(first.get("faction", "")),
            "spawn": first.get("spawn", ""),
            "active": True,
        })
    return out


def _parse_fissures(raw: dict) -> list[dict]:
    out = []
    for src, is_storm in (("ActiveMissions", False), ("VoidStorms", True)):
        for m in raw.get(src) or []:
            tier, tier_num = fissure_tier(
                m.get("Modifier") or m.get("ActiveMissionTier") or "")
            out.append({
                "id": _oid(m.get("_id")),
                "node": _node_name(m.get("Node", "")),
                "nodeKey": m.get("Node", ""),
                "missionType": mission_type(m.get("MissionType", "")),
                "tier": tier,
                "tierNum": tier_num,
                "isHard": bool(m.get("Hard")),
                "isStorm": is_storm,
                "activation": _iso_of(m, "Activation"),
                "expiry": _iso_of(m, "Expiry"),
                "enemy": sol_node(m.get("Node", "")).get("enemy", ""),
            })
    return out


def node_level(key: str) -> str:
    """节点敌人等级区间，如 ``33-35级``；无数据返回空串。"""
    lv = (nodes_zh().get(key) or {}).get("level")
    if lv:
        return f"{lv}级"
    return ""


def _parse_sortie(entry: dict, *, archon: bool) -> dict:
    variants_src = entry.get("Variants") or entry.get("Missions") or []
    variants = [{
        "node": _node_name(v.get("node", "")),
        "nodeKey": v.get("node", ""),
        "level": node_level(v.get("node", "")),
        "missionType": mission_type(v.get("missionType", "")),
        "modifier": sortie_modifier(v.get("modifierType", "")),
    } for v in variants_src]
    boss = sortie_boss(entry.get("Boss", ""))
    return {
        "id": _oid(entry.get("_id")),
        "boss": ("执刑官 " + boss) if archon else boss,
        "variants": variants,
        "activation": _iso_of(entry, "Activation"),
        "expiry": _iso_of(entry, "Expiry"),
    }


def _parse_void_trader(entry: dict, now_ms: int) -> dict:
    act, exp = _ms(entry.get("Activation")), _ms(entry.get("Expiry"))
    active = bool(act and exp and act <= now_ms < exp)
    inventory = []
    for it in entry.get("Manifest") or []:
        # 商品名要走 item_name()（含官方简中物品表），只用 language_text() 会
        # 整列落成英文。
        #
        # ★★ 字段名以 **DE 官方 worldState.php 实测**为准（2026-09-18）：
        #    ``ItemType / PrimePrice / RegularPrice``。原来按 warframestat.us
        #    的字段名（StoreItem / ItemPrice / CreditPrice）读 —— DE 源里根本
        #    没有这些键，整卡落成「? 杜卡德 ? 现金」。这里保留旧键做兜底，
        #    万一哪天切回 warframestat 源也不至于再坏一次。
        store = it.get("ItemType") or it.get("StoreItem", "")
        inventory.append({
            "item": item_name(store),
            "ducats": it.get("PrimePrice", it.get("ItemPrice", "?")),
            "credits": it.get("RegularPrice", it.get("CreditPrice", "?")),
        })
    character = entry.get("Character") or ""
    if "Baro" in character or "Teel" in character:
        # DE 源给的是「Baro'Ki Teel」（官方拼写彩蛋），统一成常用名
        character = "Baro Ki'Teer"
    return {
        "id": _oid(entry.get("_id")),
        "character": character,
        "location": _node_name(entry.get("Node", "")),
        "activation": _iso(act), "expiry": _iso(exp),
        "active": active, "inventory": inventory,
    }


def _parse_daily_deals(raw: dict) -> list[dict]:
    out = []
    for d in raw.get("DailyDeals") or []:
        # ★ 用 item_name() 而不是 language_text()。
        #   language_text() 只查**英文**表（languages.json），而不少商店商品
        #   （聚焦晶体、增幅器等）的官方简中在**物品名表** name_zh.json 里，
        #   两者 key 段也不同（`/StoreItems/Upgrades/...` vs `/upgrades/...`）。
        #   维护者 2026-09-17 反馈的「Greater Zenurik Lens 没汉化」就是
        #   这条路径只走英文表导致的 —— item_name() 会按
        #   中文词库 → 英文名 → 路径美化 逐级回退，并自动剥离 /StoreItems。
        out.append({
            "item": item_name(d.get("StoreItem", "")),
            "originalPrice": d.get("OriginalPrice"),
            "salePrice": d.get("SalePrice"),
            "discount": d.get("Discount"),
            "total": d.get("AmountTotal"),
            "sold": d.get("AmountSold"),
            "expiry": _iso_of(d, "Expiry"),
        })
    return out


def _parse_nightwave(season: dict) -> dict:
    zh = (_load("nightwave_zh.json") or {}).get("challenges") or {}
    challenges = []
    for ch in season.get("ActiveChallenges") or []:
        path = ch.get("Challenge", "")
        meta = zh.get(path) or {}
        name, _ = language_text(path)
        title = meta.get("name") or name or _prettify(path)
        title = re.sub(r"^Season (Daily|Weekly) (Permanent )?", "", title)
        challenges.append({
            "id": _oid(ch.get("_id")),
            "title": title,
            "desc": meta.get("desc", ""),
            "standing": meta.get("standing"),
            "required": meta.get("required"),
            "path": path,
            "isDaily": bool(ch.get("Daily") or ch.get("IsDaily")),
            "isElite": bool(ch.get("Elite") or ch.get("IsElite")) or "Elite" in title,
            "activation": _iso_of(ch, "Activation"),
            "expiry": _iso_of(ch, "Expiry"),
        })
    return {
        "id": _oid(season.get("_id")),
        "season": season.get("Season"),
        "phase": season.get("Phase"),
        "activation": _iso_of(season, "Activation"),
        "expiry": _iso_of(season, "Expiry"),
        "activeChallenges": challenges,
    }


def _parse_alerts(raw: dict) -> list[dict]:
    out = []
    for a in raw.get("Alerts") or []:
        mission = a.get("Mission") or {}
        reward = mission.get("Reward") or {}
        items = reward.get("items") or reward.get("countedItems") or []
        out.append({
            "id": _oid(a.get("_id")),
            "active": True,
            "activation": _iso_of(a, "Activation"),
            "expiry": _iso_of(a, "Expiry"),
            "mission": {
                "node": _node_name(mission.get("node", "")),
                "type": mission_type(mission.get("missionType", "")),
                "reward": {"credits": reward.get("credits"),
                           "item": _prettify(items[0].get("ItemType", "")) if items else None},
            },
        })
    return out


def _parse_invasions(raw: dict) -> list[dict]:
    """入侵：按「双方占比」解析。

    DE 的 ``Count`` 是**攻击方**的净推进点数，可为负（被反推）。游戏内的两条进度条
    显示的其实是双方占比：``攻击方 = (Goal + Count) / (2·Goal)``、防御方为其补数。
    旧实现只输出 ``Count/Goal``（负数直接夹成 0%），把「48% vs 52%」显示成
    「攻击方 0%（被反推 4%）」，与游戏内完全对不上。
    """
    out = []
    for inv in raw.get("Invasions") or []:
        if inv.get("Completed"):
            continue
        count = inv.get("Count", 0) or 0
        goal = inv.get("Goal") or 1
        at_pct = max(0.0, min(100.0, (goal + count) / (2 * goal) * 100))
        vs_pct = 100.0 - at_pct
        synth: list[str] = []

        def _side(faction_key: str, reward_key: str) -> dict:
            reward = inv.get(reward_key) or {}
            if isinstance(reward, list):        # 无奖励时 DE 给 [] 而不是 {}
                reward = {}
            items = reward.get("countedItems") or []
            parts: list[str] = []
            for it in items:
                name, _synth = item_name_opt_ex(it.get("ItemType", ""))
                if name is None:
                    name = _prettify(it.get("ItemType", ""))
                elif _synth:
                    synth.append(name)
                cnt = it.get("ItemCount") or 1
                if not name:
                    continue
                parts.append(f"{name}" + (f" ×{cnt}" if cnt > 1 else ""))
            credits = reward.get("credits")
            if credits:
                parts.append(f"{credits:,} 现金")
            code = inv.get(faction_key, "")
            # 同时留一份原始代码：显示层要按平台（国际服/国服）换译名体系
            return {"faction": faction_name(code), "faction_code": code,
                    "items": parts}

        out.append({
            "id": _oid(inv.get("_id")),
            "node": _node_name(inv.get("Node", "")),
            "desc": text_cn(inv.get("LocTag", "")) or _prettify(inv.get("LocTag", "")),
            "attacker": _side("Faction", "AttackerReward"),
            "defender": _side("DefenderFaction", "DefenderReward"),
            "attacker_pct": at_pct,
            "defender_pct": vs_pct,
            "completion": at_pct,
            "count": count,
            "goal": goal,
            "activation": _iso_of(inv, "Activation"),
            "expiry": "",
            # 合成译名清单（如「奥罗金催化剂蓝图」）：卡面据此加注脚
            "synthetic": sorted(set(synth)),
        })
    # 与游戏内一致：按「争夺最激烈」排序（越接近 50/50 越靠前）
    out.sort(key=lambda x: abs(x["attacker_pct"] - 50))
    return out


def _news_message(msgs: dict) -> str:
    """从 Events.Messages 里挑出可读文案：官方简中 > 英文 > 语言键解析。

    ⚠️ 只接受 zh / zh-hans / en 三种语言：DE 的社区公告会同时下发十几种语言的
    全句（波兰语、法语、西语…），旧实现「取不到 en 就取任意一种」，
    于是界面上一半是看不懂的欧洲语言。宁可少一条，也不要乱码。
    """
    for code in ("zh", "zh-hans", "en"):
        raw = (msgs.get(code) or "").strip()
        if not raw:
            continue
        if not raw.startswith("/Lotus/"):
            return raw
        zh = language_text_zh(raw)
        if zh:
            return zh
        en = language_text(raw)[0]
        if en:
            return en
    return ""


def _parse_news(raw: dict) -> list[dict]:
    out = []
    for e in raw.get("Events") or []:
        msgs = {m.get("LanguageCode"): m.get("Message") for m in e.get("Messages") or []}
        text = _news_message(msgs)
        if not text:
            continue
        out.append({
            "id": _oid(e.get("_id")),
            "message": text,
            "link": e.get("Prop", ""),
            "date": _iso_of(e, "Date"),
            "priority": bool(e.get("Priority")),
            "community": bool(e.get("Community")),
        })
    out.sort(key=lambda n: (n["date"], n["priority"]), reverse=True)
    return out


# ProjectPct 三个分量：0=巴罗尔巨人战舰(Grineer) 1=利刃豺狼舰队(Corpus)
#                    2=DE 从未公开说明的第三项（恒为 0）
# 名称取自 DE 官方简中（「巴罗尔巨人战舰即将来袭」/「获取地点：利刃豺狼舰队活动期间木星和海王星」）。
# 机制：入侵任务的胜方会累积全服「建造进度」，Grimeer 计入巴罗尔巨人战舰、Corpus 计入利刃豺狼舰队；
#      满 100% 触发 3 天的战术警报，该阵营会攻击一座中继站；全服把舰体完整度打到 0% 即结束，
#      建造进度随之归零。
_PROJECT_KEYS = [
    ("巴罗尔巨人战舰", "Balor Fomorian", "Grineer", "战舰破坏"),
    ("利刃豺狼舰队", "Razorback Armada", "Corpus", "利刃豺狼舰队"),
    ("未知项目", "", "", ""),
]


def _parse_construction(raw: dict) -> dict:
    """舰队建造进度 + 进行中的袭击事件。

    Returns:
        ``{"projects": [{name, en, faction, event, pct}], "assaults": [...]}``；
        无数据时返回 ``{}``。
    """
    pct = raw.get("ProjectPct") or []
    if not pct:
        return {}
    projects = []
    for i, (name, en, faction, event) in enumerate(_PROJECT_KEYS):
        if i >= len(pct):
            break
        val = float(pct[i] or 0)
        if val <= 0:
            continue            # 进度为 0 的项没有展示价值
        projects.append({"name": name, "en": en, "faction": faction,
                         "event": event, "pct": val})

    # 进行中的袭击：DE 在这些事件里下发带 HealthPct/VictimNode 的 Goal
    assaults = []
    for g in raw.get("Goals") or []:
        if not isinstance(g, dict) or "VictimNode" not in g:
            continue
        tag = json.dumps(g, ensure_ascii=False)
        hit = next((p for p in _PROJECT_KEYS
                    if p[1] and p[1].lower().replace(" ", "") in tag.lower().replace(" ", "")),
                   None)
        if hit is None:
            continue
        assaults.append({
            "name": hit[0],
            "victim": _node_name(g.get("VictimNode") or ""),
            "health": float(g.get("HealthPct") or 0),
            "expiry": _iso(_ms(g.get("Expiry"))),
        })
    return {"projects": projects, "assaults": assaults}


# ---------------------------------------------------------------------------
# 九重天 / 活动 / 武形秘仪 / 阿耶兑换 / 氏族奖励 / 商城折扣
# ---------------------------------------------------------------------------

# 武形秘仪模式（DE 内部名 -> 游戏内官方中文）
PVP_MODE_CN = {
    "PVPMODE_SPEEDBALL": "月动球",
    "PVPMODE_LUNARO": "月动球",
    "PVPMODE_CAPTURETHEFLAG": "中枢捕获",
    "PVPMODE_TEAMDEATHMATCH": "团队歼灭",
    "PVPMODE_DEATHMATCH": "歼灭",
    "PVPMODE_ANNIHILATION": "歼灭",
    "PVPMODE_TEAMANNIHILATION": "团队歼灭",
    "PVPMODE_FACEOFF": "对峙",
    "PVPMODE_ALL": "全部模式",
    "PVPMODE_NONE": "不限",
}

# 九重天虚空风暴等级（VoidT1..T4 = 面纱/冥王/海王/火卫二 等，按 DE 内部层数）
VOIDSTORM_TIER_CN = {"VoidT1": "T1", "VoidT2": "T2", "VoidT3": "T3", "VoidT4": "T4"}


def conclave_text(en: str) -> str:
    """武形秘仪挑战英文描述 -> 中文（词库没有时回落英文）。"""
    if not en:
        return ""
    return (_load("conclave_zh.json") or {}).get(en, en)


def _parse_void_storms(raw: dict, now_ms: int) -> list[dict]:
    """九重天虚空风暴（已过期条目**过滤**，按真实剩余毫秒排序）。

    ★ 2026-09-27（审核 §三）：DE 会保留下发**上一批已结束**的 VoidStorms
    （线上实测 12 条 = 两批，各 6 条、存活 90 分钟、相隔 60 分钟）。旧实现
    ① 不过滤 ⇒ 过期条目显示「剩 0s」；② 按**格式化字符串**排序 ⇒ `"0s" < "52m 18s"`
    把过期条目排到每组最前（与用户截图逐项吻合）。现在：过期即丢弃，
    排序改按真实剩余毫秒升序。
    """
    out = []
    for s in raw.get("VoidStorms") or []:
        node = s.get("Node") or ""
        end = _ms(s.get("Expiry"))
        if end and end <= now_ms:
            continue                     # 已结束：用户看卡时已不可玩
        left_ms = (end or now_ms) - now_ms
        out.append({
            "node": node,
            "nodeCn": _node_name(node) if node else "?",
            "missionType": sol_node(node).get("type", ""),
            "tier": VOIDSTORM_TIER_CN.get(s.get("ActiveMissionTier", ""),
                                          s.get("ActiveMissionTier", "")),
            "expiry": _iso(end),
            "timeLeft": _fmt_left(left_ms),
            "_msLeft": left_ms,          # 排序用（真实剩余毫秒，别拿字符串排）
        })
    out.sort(key=lambda x: x["_msLeft"])
    return out


def _parse_goals(raw: dict, now_ms: int) -> list[dict]:
    """限时活动（Thermia Fractures / 三伏天 / 尸鬼净化 / 瘟疫之星 …）。

    DE 的 Goal 有 ``GracePeriod``（结算宽限期）：活动本体 ``Expiry`` 到期后，
    宽限期内仍会下发该条目。旧实现一律按 ``Expiry`` 过滤，于是刚结束的三伏天
    直接从列表里消失（玩家反馈「活动是不是漏了三伏天」）。现在保留宽限期内的
    条目并标记 ``ended``，由显示层写「已结束」。
    """
    out = []
    zh_override = _load("events_zh.json") or {}
    for g in raw.get("Goals") or []:
        end = _ms(g.get("Expiry"))
        grace = _ms(g.get("GracePeriod"))
        if end and end < now_ms and (not grace or grace < now_ms):
            continue
        desc_zh = text_cn(g.get("Desc") or "")
        tip_zh = text_cn(g.get("ToolTip") or "")
        raw_name = desc_zh or tip_zh or _prettify(g.get("Tag") or "") or "活动"
        name = zh_override.get(raw_name) or raw_name
        # 描述：优先 ToolTip（更完整），去掉 <DT_xxx> 标记与换行
        desc = _DT_TAG_SUB.sub("", tip_zh or "").strip()
        if desc.startswith(name):
            desc = desc[len(name):].strip(" ：:")
            desc = desc.split("获取地点")[0].strip()
        rw = g.get("Reward") or {}
        rewards = [item_name(i) for i in (rw.get("items") or [])]
        for ci in rw.get("countedItems") or []:
            rewards.append(f"{item_name(ci.get('ItemType', ''))} ×{ci.get('ItemCount', '')}")
        rewards = [r for r in rewards if r and r != "?"]
        out.append({
            "name": name,
            "tag": g.get("Tag") or "",
            "faction": faction_name(g.get("Faction") or "") if g.get("Faction") else "",
            "desc": desc,
            "node": _node_name(g.get("Node") or "") if g.get("Node") else "",
            "count": g.get("Count"),
            "goal": g.get("Goal"),
            "expiry": _iso(end),
            "grace": _iso(grace) if grace else "",
            "ended": bool(end and end < now_ms),
            "timeLeft": _fmt_left((end or now_ms) - now_ms),
            "rewards": rewards,
        })
    # 进行中的排前面，其余按剩余时间
    out.sort(key=lambda e: (e["ended"], e["timeLeft"]))
    return out


def _parse_conclave(raw: dict, now_ms: int) -> list[dict]:
    """武形秘仪每日/每周挑战。"""
    out = []
    for c in raw.get("PVPChallengeInstances") or []:
        ref = c.get("challengeTypeRefID") or ""
        en, _ = language_text(ref)
        val = None
        for p in c.get("params") or []:
            if p.get("n") == "ScriptParamValue":
                val = p.get("v")
                break
        cat = (c.get("Category") or "").upper()
        out.append({
            "mode": PVP_MODE_CN.get(c.get("PVPMode", ""), c.get("PVPMode", "")),
            "text": conclave_text(en) if en else _prettify(ref),
            "value": val,
            "cat": "每周" if "WEEKLY" in cat else "每日",
            "expiry": _iso_of(c, "endDate"),
        })
    out.sort(key=lambda x: (x["cat"], x["mode"]))
    return out


def _vault_kind(path: str) -> str:
    """Prime 出库条目的类别 —— 供「出库」指令分组用。

    依据 DE 资产路径段判断，比按显示名猜可靠：
      ``/Powersuits/``   战甲
      ``/Weapons/``      武器（主 / 副 / 近战）
      ``sentinel``       守护与守护武器
      ``/Projections/``  遗物
      ``/Packages/``     组合包 / 饰品套装
      其余               飞船装饰（摇头娃娃）、披饰等
    """
    p = (path or "").lower()
    if "/powersuits/" in p:
        return "frame"
    if "/weapons/" in p:
        return "weapon"
    if "sentinel" in p:
        return "sentinel"
    if "/projections/" in p:
        return "relic"
    if "/packages/" in p:
        return "pack"
    return "other"


def _parse_prime_vault(raw: dict, now_ms: int) -> dict:
    """御品阿耶精华 / Prime 重生（Varzia 商店）。

    ``Manifest`` = 本期在售，``EvergreenManifest`` = 常驻。
    条目价格字段有 ``PrimePrice``（御品阿耶）与 ``RegularPrice``（普通阿耶）两种，
    **都可能缺省**（遗物类条目就没有 PrimePrice）——旧实现 price 为空时只写
    「阿耶精华」不带数量，看起来像少了数据；这里统一兜底为 1。
    组合包（``MPV*SinglePack``）之前因为查不到名字被整条丢弃，
    现在经 ``_prime_pack_name`` 补齐（见该函数）。
    """
    pv = (raw.get("PrimeVaultTraders") or [{}])[0]
    if not pv:
        return {}

    def _rows(src: list) -> list[dict]:
        out = []
        for it in src or []:
            path = it.get("ItemType", "")
            nm = item_name_opt(path)
            if not nm:
                continue
            price = it.get("PrimePrice")
            prime = True
            if price is None:
                price = it.get("RegularPrice")
                prime = price is None          # 两种价格都没有 -> 默认 1 御品阿耶
            out.append({"name": nm, "path": path, "kind": _vault_kind(path),
                        "prime": int(price if price is not None else 1),
                        "prime_currency": prime})
        return out

    items = _rows(pv.get("Manifest"))
    evergreen = _rows(pv.get("EvergreenManifest"))
    # —— 下一期 ——
    # ★ 坑：``ScheduleInfo`` 是**历史 + 未来的完整排期表**（实测 55 条，
    #   最早可追到 2022-11），而**当前期自己也在里面**、且它的 Expiry 同样是
    #   未来时间。旧实现取「第一个 Expiry > now 的条目」，取到的正是当前期，
    #   于是卡片上「下一期」显示的就是本期数据（维护者 2026-09-17 反馈）。
    #   正解：把未来条目按 Expiry 升序排，**跳过与顶层 Expiry 相同的那条**
    #   （= 当前期），再取第一条才是真正的下一期；它的开启时间就是当前期结束。
    cur_exp = _ms(pv.get("Expiry"))
    upcoming = sorted(
        ((_ms(s.get("Expiry")), s) for s in pv.get("ScheduleInfo") or []
         if _ms(s.get("Expiry")) and _ms(s.get("Expiry")) > now_ms),
        key=lambda t: t[0])
    nxt, nxt_exp, placeholder = "", None, False
    for exp, s in upcoming:
        if cur_exp and abs(exp - cur_exp) < 60_000:      # 就是当前期，跳过
            continue
        name = item_name_opt(s.get("FeaturedItem") or "") or ""
        if not name:
            # 有排期条目、但 DE 没写内容（实测下一条的 FeaturedItem 是空串）
            placeholder = True
            continue
        nxt, nxt_exp = name, cur_exp or exp
        break
    end = _ms(pv.get("Expiry"))
    return {
        "expiry": _iso(end),
        "timeLeft": _fmt_left((end or now_ms) - now_ms),
        "items": items,
        "evergreen": evergreen,
        "next": nxt,
        "next_expiry": _iso(nxt_exp) if nxt_exp else "",
        "next_left": _fmt_left(nxt_exp - now_ms) if nxt_exp else "",
        # 「有下一轮排期但 DE 未公布内容」与「完全没有下一期数据」是两回事：
        # 前者还能告诉用户下一轮何时开启，后者只能说数据没下发。
        "next_unannounced": bool(not nxt and placeholder),
        "next_open": _iso(cur_exp) if cur_exp else "",
    }


def _parse_clan_rewards(raw: dict, now_ms: int) -> list[dict]:
    """每周氏族组队奖励加成（按区域）。"""
    out = []
    for w in raw.get("WeeklyVaultBonusRewards") or []:
        raw_region = (w.get("BonusRegion") or "").rsplit("/", 1)[-1]
        rws = []
        for r in w.get("Rewards") or []:
            rws.append({
                "points": r.get("PointThreshold"),
                "name": item_name(r.get("Reward", "")),
                "count": r.get("ItemCount"),
            })
        out.append({"week": w.get("WeekCount"),
                    "region": PLANET_CN.get(raw_region, raw_region) or "?",
                    "rewards": rws})
    return out


def _parse_flash_sales(raw: dict, now_ms: int) -> list[dict]:
    """游戏内商店在售礼包。"""
    out = []
    for s in raw.get("FlashSales") or []:
        if not s.get("ShowInMarket"):
            continue
        end = _ms(s.get("EndDate"))
        out.append({
            "name": item_name(s.get("TypeName", "")),
            "start": _iso(_ms(s.get("StartDate"))),
            "end": _iso(end),
            "timeLeft": _fmt_left((end or now_ms) - now_ms),
        })
    out.sort(key=lambda x: x["timeLeft"])
    return out


def _syndicate_display(tag: str) -> str:
    table = {"CetusSyndicate": "Ostrons",
             "SolarisSyndicate": "Solaris United",
             "EntratiSyndicate": "Entrati",
             "EntratiLabSyndicate": "EntratiLab",
             "HexSyndicate": "HexCity",
             "HoldfastSyndicate": "Holdfasts"}
    return table.get(tag, "")


_TIER_RE = re.compile(r"Tier([A-F])", re.I)


def _load_job_names() -> dict:
    # DATA_DIR 指向 core/data/de，任务名表在上层 core/data/ 下。
    # 早期写成 DATA_DIR.parent / "data" 会多拼一层 data（core/data/data/...），
    # 导致文件不存在、表恒为空，所有赏金阶段只剩等级没有任务名。
    p = Path(__file__).resolve().parent / "data" / "bounty_job_names.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
    return {}


def _load_job_meta() -> dict:
    """DE 官方导出重建的赏金表（``core/data/de/bounty_jobs_zh.json``）。

    键是 jobType 资产路径（小写），值是 ``{name, desc, stages, final}``：

      * ``name``  官方赏金名（旧表 ``bounty_job_names.json`` 里
        「Dirt Unit / Dog Boards / Served Cold」这类英文条目在这里都有中文名）
      * ``desc``  目标描述
      * ``final`` 末阶段遭遇战代码列表 —— 赏金的任务类型取它（DE 的 job
        只有资产路径、没有节点，拿不到节点级任务类型）
      * ``stages`` 阶段数
    """
    return _load("bounty_jobs_zh.json")


_JOB_NAMES = _load_job_names()
_JOB_META = _load_job_meta()
# ---------------------------------------------------------------------------
# 金星「深矿」钢铁之路档 —— **社区观测**登记（2026-09-26）
# ---------------------------------------------------------------------------
# 用户实测：金星共 8 档，而 DE worldState 的 SolarisSyndicate.Jobs **只下发 7 档**，
# 缺「深矿：企业重组（钢铁之路）130-140」。三方排查（都是实测）：
#   · DE worldState  ✗ 只有 7 个 job
#   · oracle `VenusJobManifest.json` ✗ 无 130 档
#   · DE 公开导出 `ExportBounties.json` / `ExportSyndicates.json` ✗ 无 Deepmine/Enterprise/130
#     （`ExportSyndicates` 里的 Nokko 命中是「夜帽追思」阵营，不是深矿 job）
# 所以按维护者要求**硬编登记**：口径与其余 7 档完全一致（等级区间 / 奖励池 / 任务名），
# 奖励取 **DE 公开导出的钢铁版表** `NokkoColonyRewardsSteel{,B,C}`
# （`bounty_pools.json` 的「深矿·企业重组·钢铁」，构建脚本
#  `scripts/build_nokko_sp_pool.py`；普通版 `NokkoColonyRewards*` 是另一套更低的数值，
#  两套的差别实测：现金匣 ×1/5,000×3 vs **×2/×3**、内融核心 1000/2000 vs **3500/4000**
#  —— 与用户提供的沃沃截图逐项一致的是**钢铁版**，2026-09-26 据此更正）。
# 中文名取自**官方简中表**（`/Lotus/Language/NokkoColony/LocationName`=深矿、`Job2Name`=企业重组）。
# ⚠️ 来源=社区观测，**不是 DE 下发的数据**：`source` 字段随卡面注明，便于区分与复核。
# ★ 2026-09-26 二次更正（用户实测 + 沃沃对拍）：**任务名是轮换的** —— 沃沃 10:51 显示
# 「深矿 解放小动物（钢铁之路）」，而用户 10:5x 的截图是「企业重组」；官方导出里
# `NokkoColonyRewards{A,B,C}` 三张表**就是三个任务**（实测部件对与资源一一对应）：
#   A = 解放小动物（FungusHeart 心肌菌孢子 + 机体/枪机）
#   B = 企业重组  （CoolantItem 热能软泥 + 系统/枪管）← 旧池取自这张
#   C = 花园除草  （VenusCoconut 葛嘉里菌孢子 + 头部神经光元/枪托）
# DE 不下发「当前是哪一档」⇒ 卡面**不写死任务名**（改「深矿（钢铁之路）」），
# 并在注脚说明轮换；奖励仍列钢铁版三表合并（对未知轮换是超集，见 build_nokko_sp_pool.py）。
SOLARIS_SUPPLEMENT_JOBS: tuple[dict, ...] = ({
    "jobType": "深矿 企业重组（钢铁之路）",
    "jobTypeKey": "NokkoColonyEnterpriseSP",
    "isNarmer": False,
    "enemyLevels": [130, 140],
    "standingStages": [],
    "masteryReq": None,
    "rewardTable": "NokkoColonyRewardsSteel",
    "_jobName": "深矿（钢铁之路）",
    "_jobDesc": "",
    "_jobFinal": [],
    "_jobStages": 0,
    "source": "community",
},)


def _parse_syndicate_missions(raw: dict) -> list[dict]:
    out = []

    for s in raw.get("SyndicateMissions") or []:
        display = _syndicate_display(s.get("Tag", ""))
        if not display:
            continue
        jobs = []
        for j in s.get("Jobs") or []:
            raw_jt = j.get("jobType", "") or ""
            jt_tail = raw_jt.rsplit("/", 1)[-1]
            # rewards 指向奖励池表（VenusTierETableARewards / NarmerTableARewards ...），
            # 它才是“这一档该用哪个池”的权威依据：Entrati 的 job 顺序与 Tier 并不一致，
            # 100-100 档的 Tier 也与 40-60 档相同，靠位置索引推断必然错位。
            reward_table = (j.get("rewards", "") or "").rsplit("/", 1)[-1]
            _meta = _JOB_META.get(raw_jt.lower()) or {}
            jobs.append({
                "jobType": _prettify(raw_jt),
                "jobTypeKey": jt_tail,
                "isNarmer": "/Narmer/" in raw_jt,
                "enemyLevels": [j.get("minEnemyLevel"), j.get("maxEnemyLevel")],
                "standingStages": j.get("xpAmounts") or [],
                "masteryReq": j.get("masteryReq"),
                "rewardTable": reward_table,
                # 任务名表以完整资产路径（小写）为键：Eidolon 的合一众分支与普通
                # 分支末段同名（都叫 AttritionBountyExt），只按末段查会把两档混为
                # 一谈。末段回退仅用于兼容旧的短键条目。
                # 优先用 DE 官方导出重建的 bounty_jobs_zh.json（含中文名 + 描述 +
                # 末阶段类型），旧表 bounty_job_names.json 只作兜底。
                "_jobName": (_meta.get("name")
                             or _JOB_NAMES.get(raw_jt.lower())
                             or _JOB_NAMES.get(jt_tail, "")),
                "_jobDesc": _meta.get("desc") or "",
                "_jobFinal": _meta.get("final") or [],
                "_jobStages": _meta.get("stages") or 0,
            })
        # ★ 金星：补上 DE 不下发的「深矿·企业重组（钢铁之路）」档（社区观测，见上方常量注释）
        if s.get("Tag", "") == "SolarisSyndicate":
            jobs.extend(dict(x) for x in SOLARIS_SUPPLEMENT_JOBS)
        out.append({
            "id": _oid(s.get("_id")),
            "syndicate": display,
            "syndicateKey": s.get("Tag", ""),
            # 世界种子：DE 每窗口下发，各 Syndicate 同值；小帐篷 A/B/C 的
            # 当前赏金由它 + JobManifest 确定性推算（见 core/tents.py）。
            "seed": s.get("Seed"),
            "activation": _iso_of(s, "Activation"),
            "expiry": _iso_of(s, "Expiry"),
            "jobs": jobs,
        })
    return out


# 沉沦之地（炼狱塔）任务类型代码 -> **wiki `The Descendia` 的本周层名英文**
# （供 `mission_cn()` 二次中文化 / 无键时的英文兜底）。
#
# ★ 2026-09-28 §二：**这张表的英文名原先大面积写错**，而它经 `mission_cn()` 兜底
#   就会变成**错的中文** ⇒ 属「静默错槽」（英文名错 → 中文也错），不是「查不到中文」。
#   实测被卡面暴露的 2 处：`DT_MIMICS` 写 `Exterminate` ⇒ 卡面「歼灭」（应为掠夺轮盘）、
#   `DT_INTERCEPTION` 写 `Interception` ⇒ 卡面「拦截」（应为移动拦截）。
#   依据 = wiki `The Descendia`（2026-09-23，含 Update 44.0）本周 21 层逐层对拍 +
#   `wiki资源-20260928/wiki-表7-Descents-Type.md`；**不是**第三方社区译文（灰机那份已判不可采信）。
#   后 6 条当前无卡面影响（官方键已先命中），一并订正防复发。
DESCENT_TYPES = {
    "DT_ALCHEMY": "Alchemy", "DT_BOSS": "Assassination",
    "DT_BREAK_TARGETS": "Destroy Hologlobes",       # 原 Assassination（错）
    "DT_CAPTURE": "Capture",
    "DT_COLLECTION": "Void Flood",                  # 原 Recovery（错）
    "DT_DEFENSE": "Defense of a protoframe",        # 原 Defense（错）
    "DT_DEFENSE_PROTECT": "Defense",                # 本轮补齐（手译表原有、代码表缺）
    "DT_EXCAVATION": "Excavation", "DT_EXTERMINATE": "Exterminate",
    "DT_INFESTED_SALVAGE": "Infested Salvage",
    "DT_INTERCEPTION": "Mobile Interception",       # 原 Interception（错 ⇒ 卡面「拦截」）
    "DT_LOOT": "Hijack", "DT_LOOT_CREATURES": "Gruzzling Plunder",   # 原 Hijack（错）
    "DT_MIMICS": "Plunder Roulette",                # 原 Exterminate（错 ⇒ 卡面「歼灭」）
    "DT_MOVING_INTERCEPTION": "Mobile Interception",  # 本轮补齐
    "DT_NETRACELLS": "Targeted Elimination",        # 原 Recovery（错）
    "DT_PRESURE_GAUGE": "Volatile",                 # 原 Excavation（错）
    "DT_PROTOFRAME": "Defense",
    "DT_RACE": "Pursuit", "DT_SABOTAGE_DEFENSE": "Defense",   # 原 Sabotage（错 ⇒ 卡面英文）
    "DT_SABOTAGE_HIVE": "Hive",                     # 原 Sabotage（错）
    "DT_SHRINE_DEFENSE": "Defense",
    "DT_TIME_TRIAL": "Time Tribulation",            # 本轮补齐
    "DT_UNIQUE": "Assassination",                   # 官方无键、wiki 未收录 ⇒ 保持英文
}


# DE 内部代码 -> 语言表键尾的别名（code 与语言键不同名的少数几个）。
# 用英文描述对上了：EMPBlackHole(Alluring Arcocanids) = Condition_MagneticHounds(迷人弧犬)。
_CONQUEST_CODE_ALIAS = {"EMPBlackHole": "MagneticHounds"}


def _conquest_text(raw: str, kind: str) -> tuple[str, str]:
    """科研条目名 -> 官方简中。

    三类条目各有独立前缀，且两条产品线（CT_LAB=深层科研 / CT_HEX=时光科研）
    的「偏差」前缀不同：
      · 偏差 -> ``MissionVariant_{Lab,Hex}Conquest_<X>``
      · 风险 -> ``Condition_<X>``
      · 可选个人减益 -> ``PersonalMod_<X>``
    逐一试前缀，命中即返回 (名称, 描述)。

    ⚠️ DE 的内部代码和语言表键**不是一一对应**：
    ``EMPBlackHole``（英文 Alluring Arcocanids）的语言键其实是
    ``Condition_MagneticHounds``（迷人弧犬）—— 按 code 直查会落空，
    卡面上就会出现「EMP Black Hole：」这种吊着冒号没有说明的行
    （玩家反馈「可选减益没有详细说明」）。所以先过一张代码别名表。
    """
    if not raw:
        return "", ""
    tail = raw.rstrip("/").rsplit("/", 1)[-1]
    tail = _CONQUEST_CODE_ALIAS.get(tail, tail)
    line = "LabConquest" if kind == "CT_LAB" else "HexConquest"
    for base in (f"MissionVariant_{line}_{tail}", f"Condition_{tail}",
                 f"PersonalMod_{tail}", f"MissionVariant_HexConquest_{tail}",
                 f"MissionVariant_LabConquest_{tail}", tail):
        key = f"/Lotus/Language/Conquest/{base}"
        name = language_text_zh(key)
        if name:
            return name, language_text_zh(key + "_Desc")
    # 回落：英文语言表 / 美化
    en_name, en_desc = language_text(
        f"/Lotus/Language/Conquest/MissionVariant_{line}_{tail}")
    if en_name:
        return en_name, en_desc
    en_name, en_desc = language_text(f"/Lotus/Language/Conquest/Condition_{tail}")
    if en_name:
        return en_name, en_desc
    return _prettify(raw), ""


def _parse_archimedea(entry: dict, kind: str) -> dict:
    """深层科研 / 时光科研（Conquests）。

    ⚠️ 数据源踩坑：DE 下发的 ``Conquests`` 里 **CT_LAB 才是深层科研**
    （解剖圣所，敌人 FC_MITW），**CT_HEX 是时光科研**（霍瓦尼亚）。
    旧实现把 ``deepArchimedea`` 接到已废弃的 ``Descents``、把
    ``temporalArchimedea`` 接到 ``Conquests[0]``（其实是 CT_LAB），
    于是两边的任务名、偏差、风险全部张冠李戴。
    """
    if not entry:
        return {}
    missions: list[dict] = []
    risks: list[dict] = []
    seen: set[str] = set()
    for m in entry.get("Missions") or []:
        fac_code = m.get("faction", "")
        diffs = []
        for d in m.get("difficulties") or []:
            tag = "硬化" if d.get("type") == "CD_HARD" else "普通"
            dv_name, dv_desc = _conquest_text(d.get("deviation", ""), kind)
            rk = []
            for r in d.get("risks") or []:
                rn, rd = _conquest_text(r, kind)
                rk.append(rn)
                key = f"{kind}|{r}"
                if key not in seen and (rn or rd):
                    seen.add(key)
                    risks.append({"name": rn, "description": rd})
            diffs.append({"tag": tag, "deviation": dv_name,
                          "deviation_desc": dv_desc, "risks": rk})
        missions.append({
            "missionType": mission_type(m.get("missionType", "")),
            "faction": faction_name(fac_code) if fac_code else "",
            "faction_code": fac_code,
            "difficulties": diffs,
            "expiry": _iso_of(entry, "Expiry"),
        })
    variables = []
    for v in entry.get("Variables") or []:
        vn, vd = _conquest_text(v, kind)
        variables.append({"name": vn, "description": vd})
    return {"id": _oid(entry.get("_id")), "kind": kind,
            "activation": _iso_of(entry, "Activation"),
            "expiry": _iso_of(entry, "Expiry"),
            "missions": missions, "risks": risks, "variables": variables}


def _parse_descents(entry: dict) -> dict:
    """深层科研兼容路径：Conquests 缺 CT_LAB 时回落到旧的 Descents 挑战池。"""
    if not entry:
        return {}
    missions, risks = [], []
    seen: set[str] = set()
    for ch in entry.get("Challenges") or []:
        code = ch.get("Type", "")
        # ★ 2026-09-28 §二：这里**先走官方解析**再回落英文名 —— 否则 `DESCENT_TYPES`
        #   英文名一改，`mission_cn()` 就会把本路径的 missionType 变成英文，
        #   把「深层科研」卡带崩（该卡只做 mission_cn()，中文会被原样透传）。
        label = (descent_type_zh(code)
                 or DESCENT_TYPES.get(code, mission_type(code)
                                      if code.startswith("MT_") else _prettify(code)))
        if label in seen:
            continue
        seen.add(label)
        raw = ch.get("Challenge", "")
        name, desc = _conquest_text(raw, "CT_LAB")
        missions.append({"missionType": label, "faction": "", "risks": [name],
                         "difficulties": [], "expiry": _iso_of(entry, "Expiry")})
        if name or desc:
            risks.append({"name": name, "description": desc})
    return {"id": _oid(entry.get("_id")), "kind": "CT_LAB",
            "activation": _iso_of(entry, "Activation"),
            "expiry": _iso_of(entry, "Expiry"),
            "missions": missions, "risks": risks, "variables": []}


# ---------------------------------------------------------------------------
# 沉沦之地（炼狱塔）：内部代码 -> 官方简中（★ 2026-09-27 §二）
# ---------------------------------------------------------------------------
# 旧结论「DE 没有给这些代码的官方中文名」是**错的**：`/Lotus/Language/CircleOfHell/`
# 官方有 **268** 键（含 68 个 `CoHChallenge*`），本轮 21 层里 20 层都能查到。
# 之所以以前落英文，是因为**本地 36k 表没收录这个命名空间**，而不是 DE 没给 ——
# 现在 `zh_ext.json` 已覆盖（见 scripts/build_zh_ext.py）。
#
# 解析优先级（对本轮实测的 8 级）：
#   ① 剥 `NC_` 前缀再试（NC_SecuritySpin -> SecuritySpin）
#   ② `CoHChallenge{code}`
#   ③ `CoHChallengeEximus{code}`      ← Eximus 家族（HardShell/PowerHouse/ToxicFire …）
#   ④ `CoHChallenge{code}Only`        ← Manics -> ManicsOnly
#   ⑤ `CoH{code}Desc`                 ← MineField / SecuritySpin / SlipAndSlide
#   ⑥ `CoHProtoframe{code}`           ← Devil / Harrow / Wisp
#   ⑦ 双向子串兜底（归一化：大小写不敏感、去 `-`/`_`/空格；并列歧义 ⇒ 判不可靠）
#   ⑧ 皆无 ⇒ 返回空串，调用方回落英文 + 卡面注脚（**不硬编译名**）
#
# ⚠️ 官方键与内部代码**不是处处一字对应**（`FireChain` 的键是 `CoHChallengeFirechain`、
#    `JumpSmash` 是 `CoHChallengeJumpsmash`、DE 把 `Arbitration` 拼成 `Arbitation`），
#    所以比对一律走归一化；命中后再核语义，**不套近似键**。
_COH_NS = "/Lotus/Language/CircleOfHell"

# 键尾的「族前缀」：归一化后先剥掉再比对，让 `fierytrail` 能对上
# `CoHChallengeFieryTrail`、`escapist` 能对上 `CoHEscapist`。
# 顺序=从长到短（先剥长前缀，避免 `cohchallenge` 被 `coh` 抢先剥掉）。
_COH_FAMILIES = ("cohchallengeeximus", "cohchallenge", "cohprotoframe",
                 "cohgamemode", "coh")


def _norm_key(s: str) -> str:
    """官方键尾 / 内部代码的归一化形：小写 + 去 `-`/`_`/空格。"""
    return re.sub(r"[-_ ]", "", (s or "").lower())


def _coh_stem(tail: str) -> str:
    """键尾去掉族前缀后的「词干」（用于子串兜底）。"""
    n = _norm_key(tail)
    for p in _COH_FAMILIES:
        if n.startswith(p):
            return n[len(p):]
    return n


@lru_cache(maxsize=1)
def _coh_index() -> dict:
    """CircleOfHell 命名空间索引：``归一化键尾 -> (完整路径, 中文)``。

    只覆盖 ``/Lotus/Language/CircleOfHell/`` 一个命名空间（268 键，约 40KB），
    不额外占用运行期内存；`zh_ext.json` 本身已由 :func:`language_text_zh` 加载。
    """
    table = _load("zh_ext.json") or {}
    out: dict = {}
    for path, val in table.items():
        if not (isinstance(path, str) and isinstance(val, str)
                and path.startswith(_COH_NS + "/") and val):
            continue
        out.setdefault(_norm_key(path[len(_COH_NS) + 1:]), (path, val))
    return out


def _coh_by_tail(tail: str) -> tuple[str, str]:
    """按归一化键尾取 ``(完整路径, 中文)``；未命中返回 ``("", "")``。"""
    return _coh_index().get(_norm_key(tail), ("", ""))


@lru_cache(maxsize=512)
def _coh_by_substring(code: str) -> tuple[str, str]:
    """⑦ 双向子串兜底：取**最长交集**且**取值唯一**的键，否则判不可靠返回空。

    例：``FieryTrailRollers`` -> 与 ``CoHChallengeFieryTrail`` 共享 10 个字符、
    与 ``CoHChallengeRollers`` 共享 7 个 ⇒ 取前者（防火道）。
    并列且取值不同时**不猜**（宁可留英文，也不给错名）。
    """
    needle = _norm_key(code)
    if len(needle) < 4:
        return ("", "")
    scored: list[tuple[int, str, str]] = []
    for tail, (path, val) in _coh_index().items():
        stem = _coh_stem(tail)
        if len(stem) < 4:
            continue
        if needle == stem:
            score = 10_000 + len(stem)
        elif needle in stem or stem in needle:
            score = min(len(needle), len(stem))
        else:
            continue
        scored.append((score, path, val))
    if not scored:
        return ("", "")
    scored.sort(key=lambda t: -t[0])
    top = [t for t in scored if t[0] == scored[0][0]]
    if len({t[2] for t in top}) > 1:          # 并列歧义 ⇒ 不猜
        return ("", "")
    return (top[0][1], top[0][2])


# 显式别名：官方键与内部代码**语义同、字面不同**的那几个 —— 算法推不出来，
# 只能人工登记。依据 = 参考卡实机核对过的中文与 CircleOfHell 官方值逐字相等
# ⇒ 由此反查出官方键。**测试会对着官方表复算这一步**（键必须存在、取值必须一致）。
_DESCENT_GOAL_KEY = {
    "BasicBreakTargets": "CoHGamemodeDestroyTargets",      # 摧毁全息球
    "BasicLootCreatures": "CoHCreaturesLoot",              # 贪囤断肢劫掠
    "BasicLoot": "CoHGamemodeCollectionBasic",             # 掠夺
    "BasicMimics": "CoHLootMimicDesc",                     # 打开容器
    "BasicRace": "CoHGamemodeRace",                        # 时间试炼
    "HeadShotsOnly": "CoHChallengeWeakpointsOnly",         # 只有弱点才会受到伤害
    # 词序被 DE 调换 ⇒ 双向子串够不着（hordeweakpoints vs weakpointhorde），只能登记
    "HordeWeakpoints": "CoHChallengeWeakpointHorde",       # 弱点敌群
    "Escapist": "CoHEscapist",                             # 秘密撤离
}

_DESCENT_TYPE_KEY = {
    "DT_INFESTED_SALVAGE": "CoHGamemodePurify",            # 净化
    "DT_CAPTURE": "CoHGamemode99Capture",                  # 传承种捕获
    "DT_PRESURE_GAUGE": "CoHGamemodeMeltdown",             # 压力锅
    "DT_SABOTAGE_HIVE": "CoHGamemodeHive",                 # 清巢
    "DT_LOOT_CREATURES": "CoHCreaturesLoot",               # 贪囤断肢劫掠
    "DT_SHRINE_DEFENSE": "CoHGamemodeShrineDefense",       # 祈运坛防御
    "DT_MOVING_INTERCEPTION": "CoHGamemodeMobileInterception",   # 移动拦截
    "DT_TIME_TRIAL": "CoHGamemodeRace",                    # 时间试炼
    "DT_BREAK_TARGETS": "CoHGamemodeDestroyTargets",       # 摧毁全息球
    "DT_LOOT": "CoHGamemodeLootRoom",                      # 掠夺
    "DT_RACE": "CoHGamemodeRace",                          # 时间试炼
    # ★ 2026-09-28 §二 本轮新登记的 4 条（算法拼键拼不出：模式名与代码不同字面）
    "DT_SABOTAGE_DEFENSE": "CoHGamemodeDefense",           # 防御（wiki 本周第 4 层 = Defense）
    "DT_INTERCEPTION": "CoHGamemodeMobileInterception",    # 移动拦截（wiki = Mobile Interception）
    "DT_NETRACELLS": "CoHGamemodeKeyTarget",               # 消灭目标（wiki = Targeted Elimination）
    # ⚠️ DT_MIMICS 的官方键**不带 `CoHGamemode` 前缀**（是 `CoHMimicsLoot`）——
    #    这是审计 2026-09-28 新找到的键，正是第 11 层「歼灭」错槽的正解。
    "DT_MIMICS": "CoHMimicsLoot",                          # 掠夺轮盘（wiki = Plunder Roulette）
}

# 少数目标的官方键**不在 CircleOfHell 命名空间**（按 code 推不出来）⇒ 登记完整路径。
# ★ `VoidAberration`（2026-09-28 §三）：键在 `Conquest/Condition_*`（科研「风险」族）
#   = **吸血界影**。两重独立来源对得上：字面 = 吸血(Vampyric) + 界影(Liminus)，
#   且 wiki `The Descendia` 本周第 10 层挑战名正是 **Vampyric Liminus**。
#   ⚠ 但仍存疑：官方 Desc 写「双衍王境**界影**」、键落在 `Condition_*`（科研风险）族，
#   不是沉沦之地自己的命名空间 ⇒ **采纳 + 卡面注脚标明来源，待实机确认**。
_DESCENT_GOAL_EXTERNAL = {
    "VoidAberration": "/Lotus/Language/Conquest/Condition_VoidAberration",
}

# 需要卡面注脚提示「来源存疑」的目标代码（见上面 ⚠）。剥 `NC_` 前/后都要覆盖。
DESCENT_GOAL_CAVEAT = frozenset({"VoidAberration", "NC_VoidAberration"})

# 官方**确实没有**键的（实测 CircleOfHell 268 键里查无、wiki 亦未收录）⇒ 卡面留英文 + 注脚。
# ★ 2026-09-28 §二：本轮把 `DT_SABOTAGE_DEFENSE` / `DT_INTERCEPTION` / `DT_NETRACELLS` /
#   `DT_MIMICS` 从下面移出（它们**有官方键**，只是原先没找到）；`DT_BOSS` 也移出
#   （改走官方任务类型表 `missionTypes.json` → `mission_types_zh.json`，`assassination` = 刺杀）。
#   命中数 4 → **1**（只剩 `DT_UNIQUE`）。
DESCENT_TYPE_NO_KEY = frozenset({"DT_UNIQUE"})
# ★ 2026-09-28 §三：目标族原本的 `{VoidAberration, NC_VoidAberration}` 已**清空**
#   （两个代码都接到了官方键 `Conquest/Condition_VoidAberration` = 吸血界影）。
DESCENT_GOAL_NO_KEY: frozenset[str] = frozenset()


def _descent_code_variants(code: str) -> list[str]:
    """① 候选代码：原样 + 剥 ``NC_`` 前缀（顺序即优先级，剥过的那份在后）。"""
    out = [code]
    if code.upper().startswith("NC_") and len(code) > 3:
        out.append(code[3:])
    return out


def _coh_goal(code: str) -> tuple[str, str]:
    """②~⑧：沉沦之地**目标列**（``Challenge`` 代码）-> ``(官方键, 中文)``。"""
    variants = _descent_code_variants(code)
    # ②~⑥ 按模式拼键（归一化精确匹配）
    for base in variants:
        for pat in ("CoHChallenge{0}", "CoHChallengeEximus{0}",
                    "CoHChallenge{0}Only", "CoH{0}Desc", "CoHProtoframe{0}"):
            hit = _coh_by_tail(pat.format(base))
            if hit[0]:
                return hit
    # 显式别名（推导不出来的语义映射；键名写死，测试断言「键在 + 值等于卡面值」）
    for base in variants:
        alias = _DESCENT_GOAL_KEY.get(base)
        if alias:
            hit = _coh_by_tail(alias)
            if hit[0]:
                return hit
    # 键不在 CircleOfHell 命名空间的少数几个（登记了完整路径，值从运行期词表取）
    for base in variants:
        ext = _DESCENT_GOAL_EXTERNAL.get(base)
        if ext:
            zh = language_text_zh(ext)
            if zh:
                return (ext, zh)
    # ⑦ 双向子串兜底
    for base in variants:
        hit = _coh_by_substring(base)
        if hit[0]:
            return hit
    return ("", "")


def _coh_type(code: str) -> tuple[str, str]:
    """沉沦之地**任务类型列**（``DT_*``）-> ``(官方键, 中文)``。

    ``DT_*`` 是 DE 的层模式代码，和 CircleOfHell 键同样不是一字对应
    （``DT_INFESTED_SALVAGE`` -> ``CoHGamemodePurify``、``DT_PRESURE_GAUGE``
     -> ``CoHGamemodeMeltdown``），所以**先试按词拼键、再走显式别名表**；
    别名表里的每一项都由测试对着官方表复算。
    """
    if code in DESCENT_TYPE_NO_KEY:
        return ("", "")
    body = code[3:] if code.startswith("DT_") else code
    words = [w for w in body.split("_") if w]
    if words:
        hit = _coh_by_tail("CoHGamemode" + "".join(w.capitalize() for w in words))
        if hit[0]:
            return hit
    alias = _DESCENT_TYPE_KEY.get(code)
    if alias:
        hit = _coh_by_tail(alias)
        if hit[0]:
            return hit
    return ("", "")


def descent_goal_official(code: str) -> tuple[str, str]:
    """沉沦之地目标列：``(官方键, 官方中文)``；官方无键时返回 ``("", "")``。"""
    if not code or code in DESCENT_GOAL_NO_KEY:
        return ("", "")
    return _coh_goal(code)


def descent_type_official(code: str) -> tuple[str, str]:
    """沉沦之地任务类型列：``(官方键, 官方中文)``；无键时返回 ``("", "")``。"""
    return _coh_type(code or "")


def descent_goal_zh(code: str) -> str:
    """目标列官方中文（无键返回空串，由调用方兜底）。"""
    return descent_goal_official(code)[1]


def descent_type_zh(code: str) -> str:
    """任务类型列官方中文（无键返回空串，由调用方兜底）。"""
    return descent_type_official(code)[1]


def _parse_descendia(entries: list[dict], now_ms: int) -> dict:
    """沉沦之地（炼狱塔）：DE ``Descents``，每周一轮、每轮 21 层（DevilTower）。

    每层给 ``Type``（任务类型，DT_*，见 :data:`DESCENT_TYPES`）与 ``Challenge``
    （该层的挑战/复杂化代码）。这两族代码**官方都有简中**（``/Lotus/Language/
    CircleOfHell/``，268 键），由 :func:`descent_type_zh` / :func:`descent_goal_zh`
    按 8 级优先级解析；只有 ``VoidAberration`` 一族与 DT_BOSS / DT_NETRACELLS /
    DT_SABOTAGE_DEFENSE / DT_UNIQUE 实测官方无键，按内部代码原样带出、卡面加注脚。
    """
    if not entries:
        return {}

    # ⚠️ 这里**不能**自己实现一层 `_ms`：DE 的 Descents 时间戳是
    #    `{"$date": {"$numberLong": "…"}}`（多一层嵌套），只认顶层 `$numberLong`
    #    会一律得 0 ⇒ 「取当前窗口」退化成永远取 `entries[0]`。线上实测 DE 恰好把
    #    当前周排在首位，所以一直没暴露；一旦顺序变化就会整卡错周。
    #    统一走模块级 :func:`_ms`（它已处理 `$date` 嵌套与秒级时间戳）。
    cur = next((e for e in entries
                if (_ms(e.get("Activation")) or 0) <= now_ms
                < (_ms(e.get("Expiry")) or 0)),
               entries[0])
    chs = []
    for c in cur.get("Challenges") or []:
        chs.append({"index": c.get("Index"),
                    "Type": (c.get("Type") or "").strip(),
                    "type": DESCENT_TYPES.get((c.get("Type") or "").strip(),
                                              (c.get("Type") or "").strip()),
                    "code": (c.get("Challenge") or "").strip()})
    return {"activation": _iso_of(cur, "Activation"),
            "expiry": _iso_of(cur, "Expiry"),
            "challenges": chs}


# 1999 日历奖励/升级的官方简中解析（★ 2026-09-27 §三）
#
# ⚠️ **运行期只能传完整路径**：`language_text_zh` 的 tail 回退是死代码（两张表里
#    无斜杠键均为 0，实测）。跨命名空间回退必须**显式枚举候选全路径** ——
#    同一个「尾段」在不同命名空间里可能是不同东西，猜错就是错名。
_CARD_LANG_NS = ("/Lotus/Language/1999", "/Lotus/Language/Items",
                  "/Lotus/Language/Weapons", "/Lotus/Language/Narmer")
# 后缀优先级：`…NameNoIcon` 优先（`…Name` 可能带 `<SHARD_ORANGE_SIMPLE>` 之类图标占位符）
_CARD_LANG_SUFFIX = ("NameNoIcon", "Name", "")

# 资产尾段 -> 语言键尾（DE 在键里插/改了词，靠算法推不出来，只能登记）。
_CAL_TAIL_ALIAS = {
    # ResourceDropChance3Day -> ResourceDropChance**Booster**ThreeDay（键里多了 Booster）
    "ResourceDropChance3Day": "ResourceDropChanceBoosterThreeDay",
}
# 可剥的资产后缀（剥掉再试，用于「蓝图」类：OrokinReactorBlueprint -> OrokinReactor）
_CAL_TAIL_STRIP = ("StoreItem", "Blueprint", "Bundle", "Pack")
# 「蓝图」类资产：官方只有基名（无「蓝图」键）时的**合成译名**后缀
_CAL_BLUEPRINT_SUFFIX = "蓝图"
_CAL_BLUEPRINT_MARK = "Blueprint"
# 日历奖励的**合成译名**登记（官方简中无键、但游戏语境有确定官方称谓）。
# · CalendarRivenPack：1999/Items/Bundles 等命名空间 × Name/空/Desc 后缀探测**全空**
#   （2026-09-29 实测）；EN 侧官方名就是「Riven」。U38 更新说明官方中文称其为
#   「裂罅 Mod」（原文「裂罅 Mod——选择后有机会获得以下类型之一」）⇒ 采纳该措辞，
#   走合成注脚自曝（与「蓝图」同机制；沃沃同类卡直接显示「裂罅」，无出处标注）。
_CAL_REWARD_SYNTH = {"CalendarRivenPack": "裂罅 Mod"}


@lru_cache(maxsize=1)
def _card_lang_index() -> dict[str, dict[str, tuple[str, str]]]:
    """``{命名空间: {归一化键尾: (完整路径, 中文)}}``（只建 4 个卡面命名空间）。

    归一化 = 小写 + 去 ``-``/``_``/空格 —— DE 的键名大小写与资产名并不总是一致
    （资产 `WeaponSecondaryArcaneUnlocker` vs 键 `WeaponsecondaryArcaneUnlocker`），
    所以比对必须归一化，**命中后再核语义**。
    """
    out: dict[str, dict[str, tuple[str, str]]] = {ns: {} for ns in _CARD_LANG_NS}
    # 先本地 36k 表、后补充表：与 language_text_zh 的优先级保持一致
    for name in ("languages_zh.json", "zh_ext.json"):
        table = _load(name) or {}
        if not isinstance(table, dict):
            continue
        for path, rec in table.items():
            if not isinstance(path, str):
                continue
            val = rec.get("value") if isinstance(rec, dict) else rec
            if not (isinstance(val, str) and val):
                continue
            # ★ §一 收口②：`[PH] …` 占位符**不进索引** —— 效果等价于「这个键不存在」，
            #   于是「后缀 NameNoIcon → Name → ""」与「命名空间 1999 → Items → …」两层
            #   回退会自然继续往下找（而不是拿占位符当结果返回）。
            if _is_placeholder(val):
                continue
            for ns in _CARD_LANG_NS:
                if path.startswith(ns + "/"):
                    out[ns].setdefault(_norm_key(path[len(ns) + 1:]), (path, val))
                    break
    return out


def _card_lang_by_tail(tail: str) -> tuple[str, str]:
    """按「尾段 + 后缀」在 4 个命名空间里查官方简中 -> ``(完整路径, 中文)``。

    `[PH] …` 占位符在建索引时已被剔除（§一 收口②）；这里再兜一层，
    防止将来有人把索引改成惰性构建而漏掉那道过滤。
    """
    idx = _card_lang_index()
    norm = _norm_key(tail)
    for ns in _CARD_LANG_NS:
        bucket = idx.get(ns) or {}
        for suf in _CARD_LANG_SUFFIX:
            hit = bucket.get(norm + _norm_key(suf))
            if hit and not _is_placeholder(hit[1]):
                return hit
    return ("", "")


def _cal_reward_tails(tail: str) -> list[tuple[str, bool]]:
    """资产尾段 -> 候选语言键尾 ``[(键尾, 是否「蓝图」剥壳)…]``（顺序即优先级）。

    三类变形可叠加（所以按深度递归展开）：
      · ``3Day`` -> ``ThreeDay``（DE 语言键里写全称）
      · 剥资产后缀 ``StoreItem`` / ``Blueprint`` / ``Bundle`` / ``Pack``
      · 词形别名（见 :data:`_CAL_TAIL_ALIAS`；例：剥掉 StoreItem 之后的
        ``ResourceDropChance3Day`` 才对得上键尾 ``ResourceDropChanceBoosterThreeDay``）
    """
    order: list[tuple[str, bool]] = []

    def _push(cand: str, is_bp: bool) -> None:
        if cand and all(c != cand for c, _ in order):
            order.append((cand, is_bp))

    def _expand(cand: str, is_bp: bool, depth: int = 0) -> None:
        _push(cand, is_bp)
        if depth >= 3 or not cand:
            return
        if re.search(r"3Day", cand, re.I):
            _expand(re.sub(r"3Day", "ThreeDay", cand, flags=re.I), is_bp, depth + 1)
        alias = _CAL_TAIL_ALIAS.get(cand)
        if alias:
            _expand(alias, is_bp, depth + 1)
        for suf in _CAL_TAIL_STRIP:
            if cand.endswith(suf) and len(cand) > len(suf):
                _expand(cand[:-len(suf)],
                        is_bp or suf == _CAL_BLUEPRINT_MARK, depth + 1)

    _expand(tail, False)
    return order


def _calendar_reward_name(path: str) -> tuple[str, bool]:
    """日历奖励资产路径 -> ``(中文, 是否合成译名)``。

    主路径：尾段（含 `3Day`→`ThreeDay`、剥 `StoreItem`/`Blueprint`/`Bundle`/`Pack`、
    别名表）× 4 个命名空间 × 3 个后缀，全部拼成**完整路径**查官方简中。

    两个**合成**分支（官方无键，但同族有官方格式范例，注脚会标明）：
      · `…Blueprint` 且只查到基名 ⇒ 基名 + 「蓝图」
        （例：`OrokinReactorBlueprint` 官方只有 `/Lotus/Language/Items/OrokinReactor`
          = 奥罗金反应堆 ⇒ 奥罗金反应堆蓝图）
      · `…FusionBundle` 的官方英文名形如 `6,000 Endo` ⇒ `6000 内融核心`
        （格式照同族官方值 `DuviriCircuitSteelPathSilverFusionBundle` = 4000 内融核心：
          数字裸写、无千分位、无空格）
    """
    if not path:
        return ("", False)
    tail = path.rstrip("/").rsplit("/", 1)[-1]
    if not tail:
        return ("", False)
    for cand, is_bp in _cal_reward_tails(tail):
        _key, zh = _card_lang_by_tail(cand)
        if zh:
            if is_bp:
                return (zh + _CAL_BLUEPRINT_SUFFIX, True)
            return (zh, False)
        synth = _CAL_REWARD_SYNTH.get(cand)
        if synth:
            return (synth, True)
    # 内融核心捆包：官方只给英文 `6,000 Endo`，按同族官方格式合成
    en, _desc = language_text(path)
    m = re.match(r"^\s*([\d,]+)\s+Endo\s*$", en or "", re.I)
    if m and tail.lower().endswith("fusionbundle"):
        return (f"{int(m.group(1).replace(',', ''))} 内融核心", True)
    return (en or "", False)


def _calendar_upgrade_zh(ref: str) -> str:
    """日历覆写（``/Lotus/Upgrades/Calendar/<尾段>``）-> 官方简中；无键返回空串。

    官方键：``/Lotus/Language/1999/<尾段>Name``（716 键里的覆写名），
    取不到再试无后缀；仍无 ⇒ 交调用方走手编兜底表 / 英文。
    """
    if not ref:
        return ""
    tail = ref.rstrip("/").rsplit("/", 1)[-1]
    if not tail:
        return ""
    for cand in (tail, tail[:-4] if tail.endswith("Name") else tail):
        _key, zh = _card_lang_by_tail(cand)
        if zh:
            return zh
    return ""


def _calendar_upgrade_desc(ref: str) -> str:
    """日历覆写**效果描述**（``<尾段>Desc``）-> 官方简中；无键返回空串。

    2026-09-29 沃沃式版式需要：覆写块带效果行（如
    「毫不留情　增加 25% 近战攻击速度。」）。键与名字同尾段，
    1999 命名空间里的 `*Desc` 覆盖全部覆写项。
    """
    if not ref:
        return ""
    tail = ref.rstrip("/").rsplit("/", 1)[-1]
    if not tail:
        return ""
    cand = tail[:-4] + "Desc" if tail.endswith("Name") else tail + "Desc"
    _key, zh = _card_lang_by_tail(cand)
    return _cal_desc_clean(zh) if zh else ""


# 覆写描述里的**纯字母变量槽**（`|CONDITION|` 等）：无官方可填值 ⇒ 按
# 2026-09-24「读顺优先」口径删除（与 `_clean_lang_text` 的 COLOR/数字槽同层清洗；
# 该函数刻意不含纯字母槽以保护 `|COUNT|` 填数链，故这里单独补一层）。
_SOFT_SLOT_RE = re.compile(r"\|[A-Za-z_]{1,24}\|")


def _cal_desc_clean(s: str) -> str:
    """覆写描述上卡清洗：剥标记 → 删纯字母变量槽 → 槽位剥掉后的空格/标点收尾。"""
    s = _clean_lang_text(s)
    s = _SOFT_SLOT_RE.sub("", s)
    s = re.sub(r"\s+([，。；、）%])", r"\1", s)
    return " ".join(s.split())


def _parse_calendar(entry: dict, now_ms: int) -> dict:
    """1999 日历：season 窗口 + 事件表（day N 对应 1999-01-01+(N-1)，与 wfcd 对齐）。

    三类事件的中文都走**官方简中**：
      · CET_CHALLENGE 资产路径 -> (ExportChallenges) -> /Lotus/Language/1999Challenges/*
      · CET_REWARD    资产路径 -> 4 个命名空间 × 尾段变形（见 :func:`_calendar_reward_name`）
      · CET_UPGRADE   资产路径 -> /Lotus/Language/1999/<尾段>Name
        （`/Lotus/Language/1999/` 官方 **716** 键，覆写名都在 —— 旧注释说
          「DE 未在简中导出这些升级名」是错的）
    三类都没有官方键时，卡面留英文 + 注脚（不硬编）。
    """
    days = []
    synthesized: list[str] = []
    no_key: list[str] = []
    for day in entry.get("Days") or []:
        num = day.get("day")
        if num is None:
            continue
        date1999 = datetime(1999, 1, 1, tzinfo=timezone.utc) + timedelta(days=num - 1)
        events = []
        for ev in day.get("events") or []:
            kind = ev.get("type", "")
            if kind == "CET_REWARD":
                ref = ev.get("reward", "")
                name, synth = _calendar_reward_name(ref)
                if not name:
                    name = _prettify(ref)
                    no_key.append(ref.rstrip("/").rsplit("/", 1)[-1])
                elif synth:
                    synthesized.append(name)
                events.append({"type": "REWARD", "name": name})
            elif kind == "CET_CHALLENGE":
                ref = ev.get("challenge", "")
                rec = challenge_zh(ref)
                name = rec.get("name") or ""
                desc = rec.get("desc") or ""
                cnt = rec.get("count")
                if name:
                    events.append({"type": "CHALLENGE", "name": name,
                                   "desc": desc, "count": cnt})
                else:
                    events.append({"type": "CHALLENGE",
                                   "name": language_text_zh(ref)
                                   or language_text(ref)[0]
                                   or _prettify(ref)})
            elif kind == "CET_UPGRADE":
                ref = ev.get("upgrade", "") or ""
                tail = ref.rstrip("/").rsplit("/", 1)[-1]
                up_cn = _calendar_upgrade_zh(ref)
                if not up_cn and ref not in CAL_UPGRADE_NO_KEY:
                    up_cn = CAL_UPGRADE_CN.get(ref, "")
                if not up_cn:
                    up_cn = _prettify(tail)
                    no_key.append(tail)
                events.append({"type": "UPGRADE", "name": up_cn,
                               "desc": _calendar_upgrade_desc(ref)})
        days.append({"day": num, "date": date1999.date().isoformat(), "events": events})
    return {"season": entry.get("Season"), "yearIteration": entry.get("YearIteration"),
            "start": _iso_of(entry, "Activation"), "expiry": _iso_of(entry, "Expiry"),
            "days": days,
            # 供卡面加注脚：合成译名（官方无键但照同族格式产出）与无键项
            "synthesized": sorted(set(synthesized)),
            "noKey": sorted(set(no_key))}


# ---------------------------------------------------------------------------
# 总入口
# ---------------------------------------------------------------------------


def parse_worldstate(raw: dict, now_ms: Optional[int] = None) -> dict:
    """DE 原始 worldstate -> 归一化 bundle（键与 warframestat.us 端点同名）。"""
    now_ms = now_ms or int(datetime.now(timezone.utc).timestamp() * 1000)

    syndicates = raw.get("SyndicateMissions") or []
    ostrons = next((s for s in syndicates if s.get("Tag") == "CetusSyndicate"), {})
    cetus = cetus_cycle(_ms(ostrons.get("Expiry")), now_ms)
    zariman = next((s for s in syndicates if s.get("Tag") == "ZarimanSyndicate"), {}) \
        or next((s for s in syndicates if s.get("Tag") == "HexSyndicate"), {})

    sorties = raw.get("Sorties") or []
    lite = raw.get("LiteSorties") or []
    descents = raw.get("Descents") or []
    conquests = raw.get("Conquests") or []

    def _conquest(kind: str) -> dict:
        hit = next((c for c in conquests if c.get("Type") == kind), None)
        return _parse_archimedea(hit, kind) if hit else {}

    deep = _conquest("CT_LAB") or (_parse_descents(descents[0]) if descents else {})
    temporal = _conquest("CT_HEX")

    bundle = {
        "timestamp": _iso_of(raw, "Time"),
        "cetusCycle": cetus,
        "earthCycle": earth_cycle(cetus),
        "zarimanCycle": zariman_cycle(zariman.get("Seed"),
                                      _ms(zariman.get("Expiry")), now_ms),
        "vallisCycle": vallis_cycle(now_ms),
        "cambionCycle": {"state": "fass" if cetus.get("isDay") else "vome",
                         "expiry": cetus.get("expiry", ""),
                         "timeLeft": cetus.get("timeLeft", "")},
        "duviriCycle": duviri_cycle(now_ms),
        "fissures": _parse_fissures(raw),
        "sortie": _parse_sortie(sorties[0], archon=False) if sorties else {},
        "archonHunt": _parse_sortie(lite[0], archon=True) if lite else {},
        "voidTrader": _parse_void_trader((raw.get("VoidTraders") or [{}])[0], now_ms),
        "dailyDeals": _parse_daily_deals(raw),
        "nightwave": _parse_nightwave(raw.get("SeasonInfo") or {}),
        "alerts": _parse_alerts(raw),
        "invasions": _parse_invasions(raw),
        "news": _parse_news(raw),
        "constructionProgress": _parse_construction(raw),
        "synthTargets": _parse_synth(raw),
        "syndicateMissions": _parse_syndicate_missions(raw),
        "deepArchimedea": deep,
        "temporalArchimedea": temporal,
        "descendia": _parse_descendia(raw.get("Descents") or [], now_ms),
        "calendar": _parse_calendar((raw.get("KnownCalendarSeasons") or [{}])[0], now_ms),
        "voidStorms": _parse_void_storms(raw, now_ms),
        "goals": _parse_goals(raw, now_ms),
        "conclaveChallenges": _parse_conclave(raw, now_ms),
        "primeVault": _parse_prime_vault(raw, now_ms),
        "clanRewards": _parse_clan_rewards(raw, now_ms),
        "flashSales": _parse_flash_sales(raw, now_ms),
        "kuva": None,        # DE 源不含赤毒虹吸（10o.io 独立源）
        "arbitration": None,  # 同上
        "steelPath": None,    # 钢铁之路轮换为外部数据
    }
    return bundle
