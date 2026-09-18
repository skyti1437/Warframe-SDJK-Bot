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


def language_text_zh(key: str) -> str:
    """DE 官方简中词表查询（``languages_zh.json``，来自官方导出 dict.zh）。

    国际服的简体中文就是这一套：Grineer / Corpus / Infested 保留英文，
    奥罗金 / 低语者 / 合一众 / 炽蛇军 / 科腐者 有官方中文译名。

    Args:
        key: 语言键，如 ``/Lotus/Language/1999Challenges/...``。

    Returns:
        中文串；查不到返回空串（调用方自己决定回落到英文还是美化路径）。
    """
    if not key:
        return ""
    table = _load("languages_zh.json") or {}
    hit = table.get(key)
    if hit is None:
        hit = table.get(key.lower())
    if hit is None and "/" in key:
        tail = key.rstrip("/").rsplit("/", 1)[-1]
        hit = table.get(tail)
    return hit if isinstance(hit, str) else ""


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


def item_name_opt(path: str) -> Optional[str]:
    """DE 资产路径 -> 中文/英文名；词库无覆盖时返回 None（不编造）。"""
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
    secs = max(0, ms_left // 1000)
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


# 1999 日历「增益覆写」条目名。
# ⚠️ DE 的简中/繁中公开导出里**没有**这一批字符串（实测 dict.zh / dict.tc 均查不到，
#    2026-09-17 复核 languages_zh.json：/Lotus/Upgrades/Calendar 前缀 0 条）。
# 因此中文名分两级来源，**分组标注**，便于日后按实机逐条替换：
#   A. 实机核实：游戏内对照过，可信度最高
#   B. 意译：依据该条的官方英文名（languages.json 的 value）与效果说明
#      （同条的 desc 字段）译出，目的是让卡片可读。若与实机不符，以实机为准。
CAL_UPGRADE_CN = {
    # —— A. 实机核实 ——
    "/Lotus/Upgrades/Calendar/MeleeCritChance": "熟能生巧",
    "/Lotus/Upgrades/Calendar/EnergyOrbToAbilityRange": "极限拓展",
    "/Lotus/Upgrades/Calendar/HealingEffects": "应急特效药",
    "/Lotus/Upgrades/Calendar/OrbsDuplicateOnPickup": "靶向治疗",
    "/Lotus/Upgrades/Calendar/RefundBulletOnStatusProc": "随意开火",
    "/Lotus/Upgrades/Calendar/SharedFreeAbilityEveryXCasts": "有福同享",
    "/Lotus/Upgrades/Calendar/MeleeAttackSpeed": "毫不留情",
    "/Lotus/Upgrades/Calendar/AbilityStrength": "力量飙升",
    "/Lotus/Upgrades/Calendar/MagazineCapacity": "重型弹匣",
    # —— B. 依英文名与效果意译（2026-09-17 补，待实机核对）——
    "/Lotus/Upgrades/Calendar/Armor": "皮糙肉厚",                    # Thick Skin
    "/Lotus/Upgrades/Calendar/AttackAndMovementSpeedOnCritMelee":
        "一触即散",                                                  # Hit'N'Split
    "/Lotus/Upgrades/Calendar/BlastEveryXShots": "爆破狂欢",          # Have A Blast
    "/Lotus/Upgrades/Calendar/CompanionDamage": "坚实后盾",           # Got Your Back
    "/Lotus/Upgrades/Calendar/CompanionsBuffNearbyPlayer":
        "人多势众",                                                  # More the Merrier
    "/Lotus/Upgrades/Calendar/CompanionsRadiationChance":
        "友方辐射",                                                  # Friendly Fallout
    "/Lotus/Upgrades/Calendar/ElectricStatusDamageAndChance":
        "瓶装闪电",                                                  # Bottled Lightning
    "/Lotus/Upgrades/Calendar/EnergyRestoration": "浓缩能量",         # Espresso Shots
    "/Lotus/Upgrades/Calendar/EnergyWavesOnCombo": "连击冲击波",      # Combo Wave
    "/Lotus/Upgrades/Calendar/FinisherChancePerComboMultiplier":
        "连击杀手",                                                  # Combo Killer
    "/Lotus/Upgrades/Calendar/GasChanceToPrimaryAndSecondary":
        "剧毒射击",                                                  # Toxic Shot
    "/Lotus/Upgrades/Calendar/GenerateOmniOrbsOnWeakKill":
        "强制输血",                                                  # Involuntary Transfusion
    "/Lotus/Upgrades/Calendar/GuidingMissilesChance": "追踪射击",     # Tickshots
    "/Lotus/Upgrades/Calendar/MagnetStatusPull": "引力牵引",          # Force Of Attraction
    "/Lotus/Upgrades/Calendar/MagnitizeWithinRangeEveryXCasts":
        "磁力威胁",                                                  # Magnetic Menace
    "/Lotus/Upgrades/Calendar/OvershieldCap": "硬化护盾",            # Harden Up
    "/Lotus/Upgrades/Calendar/PowerStrengthAndEfficiencyPerEnergySpent":
        "力量压制",                                                  # Overpower
    "/Lotus/Upgrades/Calendar/PunchToPrimary": "穿孔卡",             # Punchcard
    "/Lotus/Upgrades/Calendar/RadiationProcOnTakeDamage":
        "心灵反馈",                                                  # Psionic Feedback
}


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
        # 同 _parse_daily_deals：商品名要走 item_name()（含官方简中物品表），
        # 只用 language_text() 会整列落成英文。
        inventory.append({
            "item": item_name(it.get("StoreItem", "")),
            "ducats": it.get("ItemPrice", "?"),
            "credits": it.get("CreditPrice", "?"),
        })
    return {
        "id": _oid(entry.get("_id")),
        "character": "Baro Ki'Teer" if "Baro" in (entry.get("Character") or "") \
            else entry.get("Character", "Baro Ki'Teer"),
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
        #   用户 2026-09-17 反馈的「Greater Zenurik Lens 没汉化」就是
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

        def _side(faction_key: str, reward_key: str) -> dict:
            reward = inv.get(reward_key) or {}
            if isinstance(reward, list):        # 无奖励时 DE 给 [] 而不是 {}
                reward = {}
            items = reward.get("countedItems") or []
            parts: list[str] = []
            for it in items:
                name = item_name_opt(it.get("ItemType", "")) \
                    or _prettify(it.get("ItemType", ""))
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
    out = []
    for s in raw.get("VoidStorms") or []:
        node = s.get("Node") or ""
        end = _ms(s.get("Expiry"))
        out.append({
            "node": node,
            "nodeCn": _node_name(node) if node else "?",
            "missionType": sol_node(node).get("type", ""),
            "tier": VOIDSTORM_TIER_CN.get(s.get("ActiveMissionTier", ""),
                                          s.get("ActiveMissionTier", "")),
            "expiry": _iso(end),
            "timeLeft": _fmt_left((end or now_ms) - now_ms),
        })
    out.sort(key=lambda x: x["timeLeft"])
    return out


def _parse_goals(raw: dict, now_ms: int) -> list[dict]:
    """限时活动（Thermia Fractures / 三伏天 / 尸鬼净化 / 瘟疫之星 …）。

    DE 的 Goal 有 ``GracePeriod``（结算宽限期）：活动本体 ``Expiry`` 到期后，
    宽限期内仍会下发该条目。旧实现一律按 ``Expiry`` 过滤，于是刚结束的三伏天
    直接从列表里消失（用户反馈「活动是不是漏了三伏天」）。现在保留宽限期内的
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
    #   于是卡片上「下一期」显示的就是本期数据（用户 2026-09-17 反馈）。
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
        out.append({
            "id": _oid(s.get("_id")),
            "syndicate": display,
            "syndicateKey": s.get("Tag", ""),
            "activation": _iso_of(s, "Activation"),
            "expiry": _iso_of(s, "Expiry"),
            "jobs": jobs,
        })
    return out


# 深层科研（Descents）任务类型 -> 英文名（供 mission_cn 二次中文化）
DESCENT_TYPES = {
    "DT_ALCHEMY": "Alchemy", "DT_BOSS": "Assassination",
    "DT_BREAK_TARGETS": "Assassination", "DT_CAPTURE": "Capture",
    "DT_COLLECTION": "Recovery", "DT_DEFENSE": "Defense",
    "DT_EXCAVATION": "Excavation", "DT_EXTERMINATE": "Exterminate",
    "DT_INFESTED_SALVAGE": "Infested Salvage", "DT_INTERCEPTION": "Interception",
    "DT_LOOT": "Hijack", "DT_LOOT_CREATURES": "Hijack",
    "DT_MIMICS": "Exterminate", "DT_NETRACELLS": "Recovery",
    "DT_PRESURE_GAUGE": "Excavation", "DT_PROTOFRAME": "Defense",
    "DT_RACE": "Pursuit", "DT_SABOTAGE_DEFENSE": "Sabotage",
    "DT_SABOTAGE_HIVE": "Sabotage", "DT_SHRINE_DEFENSE": "Defense",
    "DT_UNIQUE": "Assassination",
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
    （用户反馈「可选减益没有详细说明」）。所以先过一张代码别名表。
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
        label = DESCENT_TYPES.get(code, mission_type(code) if code.startswith("MT_")
                                  else _prettify(code))
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


def _parse_descendia(entries: list[dict], now_ms: int) -> dict:
    """沉沦之地（炼狱塔）：DE ``Descents``，每周一轮、每轮 21 层（DevilTower）。

    每层给 ``Type``（任务类型，DT_*，见 :data:`DESCENT_TYPES`）与 ``Challenge``
    （该层的挑战/复杂化代码）。⚠️ DE **没有**给这些代码的官方中文名
    （词表里 VeryToxic / GrenadesOnly / SlipAndSlide 全查不到，只有英文），
    所以这里原样带出代码，卡面上注明「目标为内部代码」。
    """
    if not entries:
        return {}

    def _ms(v) -> int:
        try:
            return int((v or {}).get("$numberLong", 0))
        except (TypeError, ValueError):
            return 0

    cur = next((e for e in entries
                if _ms(e.get("Activation")) <= now_ms < _ms(e.get("Expiry"))),
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


# 1999 日历里 StoreItems 奖励的官方语言键推导：
#   /Lotus/Types/StoreItems/Packages/Calendar/CalendarKuvaBundleLarge
#     -> /Lotus/Language/1999/CalendarKuvaBundleLargeName
# （DE 的 ExportChallenges 只覆盖挑战，奖励包没有对应导出，只能按命名规则推）
_CAL_REWARD_KEY = re.compile(r"/([A-Za-z0-9]+)$")


def _calendar_reward_name(path: str) -> str:
    if not path:
        return ""
    m = _CAL_REWARD_KEY.search(path.rstrip("/"))
    if not m:
        return ""
    tail = m.group(1)
    return (language_text_zh(f"/Lotus/Language/1999/{tail}Name")
            or language_text_zh(f"/Lotus/Language/1999/{tail}")
            or language_text(path)[0]
            or "")


def _parse_calendar(entry: dict, now_ms: int) -> dict:
    """1999 日历：season 窗口 + 事件表（day N 对应 1999-01-01+(N-1)，与 wfcd 对齐）。

    三类事件的中文来源各不相同：
      · CET_CHALLENGE 资产路径 -> (ExportChallenges) -> /Lotus/Language/1999Challenges/*
      · CET_REWARD    资产路径 -> /Lotus/Language/1999/<包名>Name
      · CET_UPGRADE   资产路径 -> languages.json（DE 未在简中导出这些升级名，
        保留英文原名而不是硬编一个非官方译名）
    """
    days = []
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
                name = _calendar_reward_name(ref)
                events.append({"type": "REWARD",
                               "name": name or _prettify(ref)})
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
                name, _desc = language_text(ref)
                if ref in CAL_UPGRADE_CN:
                    up_cn = CAL_UPGRADE_CN[ref]
                    ev_out = {"type": "UPGRADE", "name": up_cn}
                else:
                    ev_out = {"type": "UPGRADE",
                              "name": name or _prettify(ref)}
                events.append(ev_out)
        days.append({"day": num, "date": date1999.date().isoformat(), "events": events})
    return {"season": entry.get("Season"), "yearIteration": entry.get("YearIteration"),
            "start": _iso_of(entry, "Activation"), "expiry": _iso_of(entry, "Expiry"),
            "days": days}


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

# ---------------------------------------------------------------------------
# 仲裁排期推算器（锚点驱动）
# ---------------------------------------------------------------------------

def load_arb_slots() -> dict:
    import json as _json
    from pathlib import Path as _P
    return _json.loads((_P(__file__).parent / "data" / "arb_slots.json")
                       .read_text(encoding="utf-8"))


def arb_from_anchor(anchor_slot: int, anchor_node: str, hours_after: int) -> dict:
    """锚点推算：anchor 时刻的真实节点序号 -> 未来 N 小时的节点。"""
    db = load_arb_slots()
    slots = db["slots"]
    idx0 = next((i for i, s in enumerate(slots) if s["node"] == anchor_node), None)
    if idx0 is None:
        return {}
    n = len(slots)
    cur = (idx0 + hours_after - anchor_slot) % n
    s = slots[cur]
    return {
        "node": s["node"],
        "planet": db["planet_cn"].get(s["planet"], s["planet"]),
        "type": db["type_cn"].get(s["type"], s["type"]),
        "type_en": s["type"],
        "enemy": "",
    }
