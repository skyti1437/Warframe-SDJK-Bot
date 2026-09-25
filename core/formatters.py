# -*- coding: utf-8 -*-
"""世界状态数据的中文文本格式化层。

输入为 api_client 返回的原始 JSON，输出 (标题, 行列表)，
供文本卡片与图片卡片两种渲染通道共用。
"""
from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

from . import tents as _tents
from .de_worldstate import faction_name
from .parser import MISSION_CN, PLATFORM_DISPLAY, TIER_CN

# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


# 是否含中日韩字符（用于判断官方简中是否真的存在）
_HAS_CJK = re.compile(r"[\u4e00-\u9fff\u3040-\u30ff]")


def parse_iso(value: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def countdown(expiry: str) -> str:
    """剩余时间：X天X小时 / X小时X分 / X分钟。"""
    end = parse_iso(expiry)
    if end is None:
        return "?"
    secs = (end - _now()).total_seconds()
    if secs <= 0:
        return "已结束"
    mins = int(secs // 60)
    if mins < 60:
        return f"{mins}分钟" if mins > 0 else "不到1分钟"
    hours = mins // 60
    if hours < 24:
        return f"{hours}小时{mins % 60}分"
    return f"{hours // 24}天{hours % 24}小时"


def mission_cn(mission_type: str) -> str:
    key = (mission_type or "").lower()
    return MISSION_CN.get(key, MISSION_CN.get(key.replace(" ", ""), mission_type))


def tier_cn(tier: str) -> str:
    return TIER_CN.get(tier, tier)


# 紫卡 riven_type slug → 中文武器类别（2026-09-23，倾向卡不再漏英文）
RIVEN_TYPE_CN = {
    "rifle": "步枪",
    "shotgun": "霰弹枪",
    "pistol": "手枪",
    "melee": "近战",
    "archgun": "曲翼枪械",
    "archmelee": "曲翼近战",
}

# WM 的 rivenType 只记「MOD 适用类别」——曲翼枪械（翠雀 Larkspur 等）在 WM
# 数据里 rivenType 也是 rifle，更具体的武器类别在 group 字段
# （archgun / sentinel / kitgun / zaw）。显示时 group 命中这些值优先。
RIVEN_GROUP_CN = {
    "archgun": "曲翼枪械",
    "archmelee": "曲翼近战",
    "sentinel": "守护武器",
    "kitgun": "组合枪",
    "zaw": "Zaw 近战",
}


def riven_type_cn(riven_type: str, group: str = "") -> str:
    """riven_type/group → 中文类别；未知值原样返回。

    ★ 2026-09-24 用户报障「翠雀应该是曲翼枪械」：WM 把曲翼枪械的 rivenType
    也标成 ``rifle``，只看 rivenType 会显示成「步枪」。改为 group 命中更具体
    类别时优先（archgun→曲翼枪械、sentinel→守护武器），否则回落 rivenType。
    """
    g = (group or "").strip().lower()
    if g in RIVEN_GROUP_CN:
        return RIVEN_GROUP_CN[g]
    key = (riven_type or "").strip().lower()
    if not key:
        return ""
    return RIVEN_TYPE_CN.get(key, riven_type)


def _tier_stars(tier_num) -> str:
    try:
        return "★" * int(tier_num)
    except (TypeError, ValueError):
        return ""


def _state_cn(state: str) -> str:
    """周期状态显示名。

    昼夜/温度这类通用词用中文；而 Fass / Vome / Sorrow / Fear / Joy /
    Anger / Envy 这些**派系与情绪状态词 DE 官方简中一律不翻译**，
    直接沿用英文原文，与游戏内客户端一致（「法斯 / 沃姆」是国服叫法，已弃用）。
    """
    return {"day": "白天", "night": "夜晚", "warm": "温暖", "cold": "寒冷",
            "fass": "Fass（白天）", "vome": "Vome（夜晚）"}.get(
                state, (state or "").capitalize())


# ---------------------------------------------------------------------------
# 周期类
# ---------------------------------------------------------------------------


def _left_pair(expiry: str) -> tuple[str, str]:
    """剩余时间 -> ``(中文倒计时, 紧凑倒计时)``，两者取自**同一个时刻**。

    中文那份与 :func:`countdown` 完全同构（X天X小时 / X小时X分 / X分钟 / 不到1分钟），
    紧凑那份与 DE 下发的 ``timeLeft`` 同构（1h 7m / 47m 42s / 58s）。

    ⚠️ 必须由**同一个** ``_now()`` 算出。旧实现中文走 ``countdown(expiry)``（格式化时算）、
       紧凑那份直接抄数据里的 ``timeLeft``（解析时算），两者相差几十秒就会拼出
       「剩余 16分钟（17m 26s）」这种自相矛盾的一行 —— 卡片渲染比数据解析晚一点点就复现。
    """
    end = parse_iso(expiry)
    if end is None:
        return "?", ""
    secs = int((end - _now()).total_seconds())
    if secs <= 0:
        return "已结束", ""
    mins = secs // 60
    if mins < 60:
        cn = f"{mins}分钟" if mins > 0 else "不到1分钟"
    elif mins < 1440:
        cn = f"{mins // 60}小时{mins % 60}分"
    else:
        cn = f"{mins // 1440}天{mins % 1440 // 60}小时"
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    compact = f"{h}h {m}m" if h else (f"{m}m {s}s" if m else f"{s}s")
    return cn, compact


# 地球平原的「日夜材料」轮换：同一株植物分日光/月光两个变体，随地球昼夜更替。
# 官方简中名取自 DE 本地化 /Lotus/Language/Game/{Day,Night}{Common,UnCommon,Rare}Plant。
EARTH_PLANTS = {
    "day": ("日光龙百合", "日光去壳毬果", "日光玉叶"),
    "night": ("月光龙百合", "月光去壳毬果", "月光玉叶"),
}

# 扎里曼号派系：官方简中**不翻译**派系名，与游戏内客户端一致。
ZARIMAN_CN = {"grineer": "Grineer", "corpus": "Corpus"}


def fmt_cetus(*cycles: Optional[dict]) -> tuple[str, list[str]]:
    """夜灵/平原时间：地球平原、金星山谷、魔胎之境、地球、双衍王境、扎里曼号全部周期。"""
    names = {"cetus": "夜灵平野", "vallis": "奥布山谷", "cambion": "魔胎之境",
             "earth": "地球", "duviri": "双衍王境", "zariman": "扎里曼号"}
    lines = []
    for c in cycles:
        if not c:
            continue
        key = c.get("_name", "")
        name = names.get(key, key or "周期")
        if key == "duviri":
            # 双衍王境：状态即「螺旋」（情绪），官方简中译名 + 英文原名。
            # ⚠️ 不要再单列「当前螺旋 / 后续螺旋」——那两行和本行是同一个螺旋，
            #    信息完全重复，还各带一个倒计时（旧版三行并列时尤其容易被当 bug）。
            state_cn = c.get("stateCn") or _state_cn(c.get("state", ""))
            state_en = c.get("state", "")
            state = f"{state_cn}（{state_en}）" if state_cn and state_en else state_cn
        elif key == "zariman":
            state = ZARIMAN_CN.get(c.get("state", ""), c.get("state", ""))
        else:
            state = _state_cn(c.get("state", ""))
        # 倒计时统一走 _left_pair：中文 + 紧凑两份同源同刻，行格式各周期完全一致
        # （双衍曾因只写中文秒级、缺「（Xh Ym）」而与其它行格式不一）。
        left, extra = _left_pair(c.get("expiry", ""))
        lines.append(f"{name}：{state} 剩余 {left}" + (f"（{extra}）" if extra else ""))
        if key == "earth":
            plants = EARTH_PLANTS.get("day" if c.get("isDay") else "night", ())
            if plants:
                lines.append("　当前材料：" + " · ".join(plants))
    if not lines:
        lines = ["暂无周期数据"]
    return ("平原时间", lines)


# 时效总览：每一项是什么（避免只给倒计时看不懂）
_TIMER_DESC = {
    "夜灵平野": "夜灵平野 · 昼夜交替",
    "奥布山谷": "奥布山谷 · 温度周期",
    "魔胎之境": "魔胎之境 · 派系轮换（Fass / Vome）",
    "双衍王境": "双衍王境 · 情绪轮换（Sorrow/Fear/Joy/Anger/Envy）",
    "扎里曼派系": "羽化之穹 · 派系轮换（先锋/合一众）",
    "每日突击": "每日突击 · 重置",
    "虚空奸商": "虚空商人 · 抵达/离开",
    "执刑官猎杀": "执刑官猎杀 · 周常重置",
    "仲裁": "仲裁 · 每小时轮换",
    "钢铁侵蚀": "钢铁之路 · 每日重置",
    "午夜电波": "午夜电波 · 赛季/每日挑战",
    "1999日历": "1999 日历 · 周期重置",
    "深层科研": "深层科研 · 周常重置",
    "时光科研": "时光科研 · 周常重置",
    "沉沦之地": "沉沦之地（炼狱塔）· 周常 21 层",
    "钢铁回廊灵化": "钢铁回廊灵化 · 周常轮换",
    "信条元素加成": "信条武器元素加成 · 每 4 天重生成",
    "终幕换批": "Coda 终幕武器 · 每 4 天换批",
    "周常重置": "每周重置（周一 00:00 UTC）",
}


def fmt_timers(timers: list[tuple[str, dict]]) -> tuple[str, list[str]]:
    """时效：所有周期/限时任务的最近截止时间汇总（每行注明是什么的时效）。

    ★ 铁律：**不允许静默吞行**（2026-09-25 立规）。取不到数据（源未下发、
    接口恒抛、字段缺失）时显式打印「暂无时效数据（源未下发）」——旧写法
    ``if not data: continue`` 让「仲裁」「钢铁侵蚀」两行自 10o.io 停摆后
    从卡面里静默消失，用户以为看全了其实没有。
    """
    lines = []
    for name, data in timers:
        desc = _TIMER_DESC.get(name, name)
        if data and "expiry" in data:
            lines.append(f"{desc}：剩余 {countdown(data['expiry'])}")
        elif data and "activation" in data:
            lines.append(f"{desc}：{countdown(data['activation'])} 后开始")
        else:
            lines.append(f"{desc}：暂无时效数据（源未下发）")
    lines.append("※ 以上为各周期内容的当前剩余时间，到点自动轮换/重置")
    return ("时效总览", lines or ["暂无数据"])


# ---------------------------------------------------------------------------
# 裂隙 / 突击 / 执刑官
# ---------------------------------------------------------------------------


# 遗物纪元的展示顺序（DE 的 VoidT1..T6），用于裂隙列表排序
_TIER_ORDER = {"Lith": 0, "Meso": 1, "Neo": 2, "Axi": 3, "Requiem": 4,
               "Omnia": 5, "Vanguard": 6}

# 单卡正文行数上限（渲染层 MAX_BODY_LINES = 90，留出标题/注脚余量）。
# 「不翻页」模式下超出只截断到该值并显式说明，绝不静默丢内容。
_ALL_ROWS_CAP = 86


# 裂隙/开核桃节点的敌对派系：取 DE ``solNodes`` 的 ``enemy``
# （Grineer / Corpus / Infested / Sentient / Orokin / The Murmur …）。
# 国际服官方简中**不翻译派系名**（Grineer / Corpus / Infested / Sentient /
# Corrupted 保留英文，与游戏内客户端一致）；有官方中文译名的是
# 奥罗金 / 低语者 / 合一众 / 炽蛇军 / 科腐者（均为 DE 游戏内官方文本）。
# Tenno / Crossfire / Duviri 这类不是敌对派系，不显示。
_FACTION_ZH = {"Orokin": "奥罗金", "The Murmur": "低语者",
               "Infestation": "Infested",
               "Narmer": "合一众", "Scaldra": "炽蛇军", "Techrot": "科腐者"}
_FACTION_HIDE = {"", "?", "Tenno", "Crossfire", "Duviri", "Anarch"}


def _fissure_faction(f: dict) -> str:
    """裂隙行的派系显示名（无数据/非敌对派系时返回空串）。"""
    raw = (f.get("enemy") or "").strip()
    if raw in _FACTION_HIDE:
        return ""
    return _FACTION_ZH.get(raw, raw)


def fmt_fissures(fissures: Iterable[dict], flt=None, page: int = 1,
                 page_size: int = 12,
                 all_rows: bool = False) -> tuple[str, list[str]]:
    """虚空裂隙列表：按「纪元 → 剩余时间」排序，标题行给出排序口径。

    旧实现有两个问题：① 每行尾部跟一串 ``★``（``tierNum`` 直接当星数），
    这个「星级」既非游戏内概念也无任何含义；② 只按 activation 排序，
    玩家看不出规律。现在改成显式的「古纪→前纪→中纪→后纪→安魂→全能」，
    同纪元内按剩余时间升序（快结束的排前面）。

    ``all_rows=True``：**不翻页**，一次把全部裂隙放进同一张卡（用户要求
    「别做翻页了，直接那几页内容都放一张截图上」）。实测同时在册约 34 条，
    远低于渲染层 90 行上限；万一超过，也只截到上限并显式说明，不静默丢内容。
    """
    flt = flt or (lambda f: True)
    act = [f for f in fissures if parse_iso(f.get("expiry", "")) and
           parse_iso(f["expiry"]) > _now() and flt(f)]
    act.sort(key=lambda f: (_TIER_ORDER.get(f.get("tier", ""), 99),
                            f.get("expiry", "")))
    total = len(act)
    extra = ""
    if all_rows:
        pages, page = 1, 1
        chunk = act[:_ALL_ROWS_CAP]
        if total > _ALL_ROWS_CAP:
            extra = f"※ 共 {total} 条，本卡只列前 {_ALL_ROWS_CAP} 条（按上面口径排序）"
        first_no = 1
    else:
        pages = max(1, (total + page_size - 1) // page_size)
        page = max(1, min(page, pages))
        chunk = act[(page - 1) * page_size: page * page_size]
        first_no = (page - 1) * page_size + 1
    lines = []
    for i, f in enumerate(chunk, first_no):
        parts = []
        tier = f.get("tier", "")
        if tier and tier != "?":
            parts.append(f"[{tier_cn(tier)}]")
        if f.get("node"):
            parts.append(f["node"])
        mtype = mission_cn(f.get("missionType", ""))
        if mtype and mtype != "?":
            parts.append(mtype)
        # 派系（Grineer / Corpus / Infested…）：数据在 de_worldstate 的 ``enemy`` 里，
        # 国际服官方简中不翻译派系名，原样显示（与游戏内客户端一致）。
        fac = _fissure_faction(f)
        if fac:
            parts.append(fac)
        if f.get("isHard"):
            parts.append("钢铁")
        if f.get("isStorm"):
            parts.append("九重天")
        head = " · ".join(p for p in parts if p)
        head = head.replace("] · ", "] ")   # 纪元芯片紧贴节点名
        lines.append(f"{i}. {head} · 剩{countdown(f['expiry'])}")
    head = (f"虚空裂隙（共{total}条）" if all_rows
            else f"虚空裂隙（第{page}/{pages}页，共{total}条）")
    lines.append("※ 按纪元（古纪→前纪→中纪→后纪→安魂→全能）排序，同纪元内剩余时间少的在前")
    if extra:
        lines.append(extra)
    return (head, lines or ["当前筛选条件下没有进行中的裂隙"])


def _t_num(f: dict) -> int:
    try:
        return int(f.get("tierNum", 0))
    except (TypeError, ValueError):
        return 0


_MODIFIER_CN: dict[str, str] = {}


def _sortie_modifier_cn(name: str) -> str:
    """突击/执刑官限制条件中文名（覆盖 DE sortieData 全部 30 种 modifierTypes）。"""
    if not _MODIFIER_CN:
        _MODIFIER_CN.update({
            # —— 敌人强化 ——
            "Energy Reduction": "能量上限降低",
            "Augmented Enemy Armor": "敌人护甲强化",
            "Enhanced Enemy Shields": "敌人护盾强化",
            "Enhanced Enemy Health": "敌人生命提升",
            "Enhanced Enemy Armor": "敌人护甲提升",
            "Enemy Shield Drain": "敌人护盾衰减",
            "Increased Enemy Damage": "敌人伤害提升",
            "Eximus Stronghold": "卓越者据点",
            "Eximus Strongholds": "卓越者据点",
            "Enemy Physical Enhancement: Impact": "敌人物理强化：冲击",
            "Enemy Physical Enhancement: Slash": "敌人物理强化：切割",
            "Enemy Physical Enhancement: Puncture": "敌人物理强化：穿刺",
            "Enemy Elemental Enhancement: Magnetic": "敌人元素强化：磁力",
            "Enemy Elemental Enhancement: Corrosive": "敌人元素强化：腐蚀",
            "Enemy Elemental Enhancement: Viral": "敌人元素强化：病毒",
            "Enemy Elemental Enhancement: Electricity": "敌人元素强化：电击",
            "Enemy Elemental Enhancement: Radiation": "敌人元素强化：辐射",
            "Enemy Elemental Enhancement: Gas": "敌人元素强化：毒气",
            "Enemy Elemental Enhancement: Heat": "敌人元素强化：火焰",
            "Enemy Elemental Enhancement: Blast": "敌人元素强化：爆炸",
            "Enemy Elemental Enhancement: Cold": "敌人元素强化：冰冻",
            "Enemy Elemental Enhancement: Toxin": "敌人元素强化：毒素",
            # —— 武器限制 ——
            "Weapon Restriction: Pistol Only": "武器限制：仅限手枪",
            "Weapon Restriction: Shotgun Only": "武器限制：仅限霰弹枪",
            "Weapon Restriction: Sniper Only": "武器限制：仅限狙击枪",
            "Weapon Restriction: Assault Rifle Only": "武器限制：仅限突击步枪",
            "Weapon Restriction: Melee Only": "武器限制：仅限近战",
            "Weapon Restriction: Bow Only": "武器限制：仅限弓",
            # —— 环境危害 ——
            "Environmental Hazard: Radiation Pockets": "环境危害：辐射区",
            "Environmental Hazard: Electromagnetic Anomalies": "环境危害：电磁异常区",
            "Environmental Hazard: Dense Fog": "环境危害：浓雾",
            "Environmental Hazard: Fire": "环境危害：火焰",
            "Environmental Effect: Cryogenic Leakage": "环境效应：低温泄漏",
            "Environmental Effect: Extreme Cold": "环境效应：极寒",
            # —— 旧版 / 别名 ——
            "Reduced Enemy Accuracy": "敌人命中率降低",
            "Void Electricity": "虚空电击", "Toxic Rain": "毒素之雨",
            "Magnetic Field": "磁场", "Corrosive Warfare": "腐蚀战",
            "Energy Overload": "能量过载", "Elemental Enhancement": "元素强化",
            "Weather Control": "天气控制", "Night Mode": "黑暗模式",
            "Kill Steal": "击杀窃取", "Machine Enemies": "机械敌人",
            "Bullet Attractor": "子弹吸引", "Viral Storms": "病毒风暴",
            "Overcharged Defenses": "防御过载", "Dense Atmosphere": "稠密大气",
            "Weapon Sharing": "武器共享", "Permanent Double Damage": "永久双倍伤害",
            "Low Gravity": "低重力", "High Voltage": "高压",
        })
    return _MODIFIER_CN.get(name, name)


_SORTIE_REWARD_CACHE: Optional[dict] = None


def sortie_rewards() -> dict:
    """突击奖励表（tier -> 奖励项列表），来源 core/data/de/sortie_rewards.json。"""
    global _SORTIE_REWARD_CACHE
    if _SORTIE_REWARD_CACHE is None:
        try:
            p = Path(__file__).resolve().parent / "data" / "de" / "sortie_rewards.json"
            _SORTIE_REWARD_CACHE = json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            _SORTIE_REWARD_CACHE = {}
    return _SORTIE_REWARD_CACHE


# DE 语言表里没给中文的通用奖励名（加成类 Store Item）
# DE Store Item 英文名 -> 官方简中名。
# 命名全部取自 DE 官方导出（ExportSortieRewards_zh / ExportResources_zh），
# 例如官方是「3 天经验值加成」而不是「经验值加成（3 天）」。
# 注意顺序：长串在前，避免短串先替换掉长串的前缀。
_REWARD_NAME_CN = {
    "Resource Drop Chance Booster Store Item": "3 天资源掉落几率加成",
    "Mod Drop Chance Booster Store Item": "3 天 Mod 掉落几率加成",
    "Resource Booster Store Item": "3 天资源数量加成",
    "Affinity Booster Store Item": "3 天经验值加成",
    "Credit Booster Store Item": "3 天现金加成",
    "Forma Blueprint": "Forma 蓝图",
    "Orokin Catalyst Blueprint": "奥罗金催化剂蓝图",
    "Orokin Reactor Blueprint": "奥罗金反应堆蓝图",
}

# 执刑官 -> (碎片颜色, 官方物品名)。
# 官方叫**源力石**（``archoncrystalamar`` → 深红执刑官源力石）。
_ARCHON_SHARD = {
    "Amar": ("红", "深红执刑官源力石"),
    "Nira": ("黄", "琥珀执刑官源力石"),
    "Boreal": ("蓝", "蔚蓝执刑官源力石"),
}


def _reward_cn(name: str) -> str:
    """把奖励名里的英文 Store Item 术语替换成中文。"""
    if not name:
        return name
    for en, cn in _REWARD_NAME_CN.items():
        if en in name:
            return name.replace(en, cn)
    # 「2000 x Kuva」这类 WM 风格的写法也归一化一下
    m = re.match(r"^([\d,]+)\s*x\s*(.+)$", name.strip())
    if m:
        return f"{m.group(2)} ×{m.group(1)}"
    return name


# 用于突击/执刑官奖励行的中文数字（「6000 x Kuva」-> 「赤毒 ×6000」）
_RES_ALIAS = {"Kuva": "赤毒", "Endo": "内融核心", "Credits": "现金"}


def _res_name_cn(s: str) -> str:
    out = _reward_cn(s)
    for en, cn in _RES_ALIAS.items():
        out = out.replace(en, cn)
    return out


def _sortie_reward_lines(limit: int = 8) -> list[str]:
    """突击奖励池（完成 3 个阶段后抽 1 件）。"""
    data = sortie_rewards()
    tiers = data.get("tiers") or {}
    rows = tiers.get("0") or next(iter(tiers.values()), [])
    if not rows:
        return []
    out = ["◆ 完成后奖励（从下列随机 1 件）"]
    for r in rows[:limit]:
        cnt = r.get("count") or 1
        name = _res_name_cn(r["name"]) + (f" ×{cnt}" if cnt and cnt > 1 else "")
        out.append(f"· {name}　{r.get('rarity', '')} {r.get('chance', '')}%")
    if len(rows) > limit:
        out.append(f"……另有 {len(rows) - limit} 项（其余为加成类与塑像）")
    return out


def fmt_sortie(sortie: Optional[dict]) -> tuple[str, list[str]]:
    if not sortie:
        return ("每日突击", ["数据暂不可用"])
    lines = [f"首领：{sortie.get('boss', '?')}　剩余 {countdown(sortie.get('expiry', ''))}"]
    for i, v in enumerate(sortie.get("variants", []) or [], 1):
        lim = _sortie_modifier_cn(v.get("modifier", ""))
        # 「武器限制：仅限霰弹枪」本身已带类别前缀，避免再套一层「限制：限制：」
        lim = re.sub(r"^(?:武器限制|敌人(?:物理|元素)?强化)：", "", lim)
        lines.append(f"{i}. {v.get('node', '?')} · {mission_cn(v.get('missionType', ''))}"
                     + (f"　限制：{lim}" if lim else ""))
    lines += _sortie_reward_lines()
    return ("每日突击", lines)


def fmt_archon(archon: Optional[dict]) -> tuple[str, list[str]]:
    """执刑官猎杀：3 阶段节点 + 敌人等级 + 限制条件 + 源力石奖励。"""
    if not archon:
        return ("执刑官猎杀", ["数据暂不可用"])
    boss = archon.get("boss", "?")
    lines = [f"本周执刑官：{boss}　剩余 {countdown(archon.get('expiry', ''))}"]
    lines.append("※ 执刑官猎杀无突击式负面限制；3 个阶段按顺序推进")
    for i, v in enumerate(archon.get("variants", []) or [], 1):
        lim = _sortie_modifier_cn(v.get("modifier", ""))
        lv = f"｜{v['level']}" if v.get("level") else ""
        lines.append(f"{i}. {v.get('node', '?')}{lv} · "
                     f"{mission_cn(v.get('missionType', ''))}"
                     + (f"　限制：{lim}" if lim else ""))
    color, shard = "?", "执刑官源力石"
    for name, (c, s) in _ARCHON_SHARD.items():
        if name.lower() in boss.lower():
            color, shard = c, s
            break
    lines.append(f"◆ 第 3 阶段奖励：{shard}（{color}）")
    lines.append("　（有几率升级为 Tau 强化版；源力石用于兑换执刑官 MOD 与赋能）")
    lines.append("※ 每周首次完成固定给 1 个对应颜色的执刑官源力石")
    return ("执刑官猎杀", lines)


# ---------------------------------------------------------------------------
# 商人与兑换
# ---------------------------------------------------------------------------


def _baro_cash(credits) -> str:
    """现金展示：≥1 万折叠成「N 万」，小数去尾零（250000 → 25万现金）。"""
    try:
        c = int(credits)
    except (TypeError, ValueError):
        return "?现金"
    if c >= 10000:
        s = f"{c / 10000:.1f}".rstrip("0").rstrip(".")
        return f"{s}万现金"
    return f"{c}现金"


def fmt_void_trader(trader: Optional[dict]) -> tuple[str, list[str]]:
    """虚空商人（当期在售）。

    排版（2026-09-18 用户要求「借鉴别人家的设计」）：**两列**。
    每行左右各一个商品，共 6 列（名称 / 杜卡德 / 现金 ×2），交给渲染层的
    表格列对齐（_table_mode）统一列位。实测 37 件全部塞进一页
    （表宽 1231 ≤ 安全阀 1322，A 全称格式 1365 会超限 —— 所以杜卡德用
    「N杜」缩写、现金用「N万现金」，单位不同正好配两种颜色）。

    颜色：名称暖白（默认）、杜卡德金色、现金青色（render 的 token 规则）。
    """
    if not trader:
        return ("虚空商人", ["数据暂不可用"])
    active = trader.get("active", False)
    lines = []
    if active:
        lines.append(f"{trader.get('character', 'Baro Ki\'Teer')} 已抵达 "
                     f"{trader.get('location', '?')}，离开剩余 {countdown(trader.get('expiry', ''))}")
        inv = trader.get("inventory") or []
        if not inv:
            lines.append("货单同步中，请稍后再查")
            return ("虚空商人", lines)

        def cell(it: dict) -> str:
            name = it.get("item") or "?"
            return f"{name}　{it.get('ducats', '?')}杜　{_baro_cash(it.get('credits'))}"

        half = (len(inv) + 1) // 2
        for i in range(half):
            row = cell(inv[i])
            if i + half < len(inv):
                row += "　" + cell(inv[i + half])
            lines.append(row)
        lines.append(f"※ 共 {len(inv)} 件｜金色＝杜卡德 · 青色＝现金")
    else:
        lines.append(f"奸商尚未抵达，将于 {trader.get('location', '?')} 出现，"
                     f"还有 {countdown(trader.get('activation', ''))}")
    return ("虚空商人", lines)


def fmt_baro_predict(rows: list[dict], next_est: str = "",
                     visits: int = 0, last: str = "",
                     names_zh: Optional[dict] = None
                     ) -> tuple[str, list[str]]:
    """奸商下期库存「预测」（**统计推测，非官方**）。

    用户 2026-09-18：「别人的奸商指令有预测功能；我们这张卡好像没汉化，
    排版可以借鉴、注意颜色运用」。本版做了三件事：

    1. **汉化**：物品名走 DE 官方双语词表（`core/data/baro_names_zh.json`，
       由 `scripts/build_baro_names.py` 生成，覆盖 466 件里的 460 件 = 98%），
       词表里确实没有的才回落英文原名；大类（MOD/武器/装饰…）全中文。
    2. **排版**：`序号 + [大类] + 中文名 + 杜卡德`，第二行缩进给依据
       （上次上架 / 已静默 / 均隔 / 回归度）。
    3. **颜色**：大类做成芯片并按类别配色（见 render.GROUP_CHIP_COLOR），
       杜卡德报价金色、序号暗金（render 的 rules 表）。

    口径（卡上也写）：`回归度 = 已静默次数 ÷ 该物品历史平均上架间隔`，
    越接近 1 越「该回来了」；静默超过历史最大间隔 2 倍的已剔除（那多半是停售）。
    """
    if not rows:
        return ("奸商下期预测", [
            "预测数据不可用（缺 core/data/baro_history.json）",
            "刷新方式：scripts/build_baro_history.py（需经 FlareSolverr 抓 wiki）",
        ])
    zh = names_zh or {}

    def nm(r: dict) -> str:
        return zh.get(r["name"]) or r.get("name_cn") or r["name"]

    lines: list[str] = []
    head = []
    if next_est:
        head.append(f"下次预计到访 {next_est}")
    if last:
        head.append(f"上次 {last}")
    if visits:
        head.append(f"样本 {visits} 次到访")
    if head:
        lines.append(" ｜ ".join(head))

    for i, r in enumerate(rows, 1):
        grp = r.get("group") or "其他"
        cost = f"{r['ducats']} 杜卡德" if r.get("ducats") else "—"
        lines.append(f"{i}. [{grp}] {nm(r)}　{cost}")
        lines.append(f"　 上次 {r['last']} ｜ 已静默 {r['silent']} 次"
                     f"（均隔 {r['gap']}）｜ 回归度 {r['score']}")

    lines.append("※ 统计推测，不是官方预测（DE 从不公布下期库存）")
    lines.append("　 口径：已静默次数 ÷ 该物品历史平均上架间隔，"
                 "越接近 1 越可能回归")
    return ("奸商下期预测（推测）", lines)


def fmt_daily_deals(deals: Iterable[dict]) -> tuple[str, list[str]]:
    lines = [f"{d.get('item', '?')}：{d.get('salePrice', '?')}白金（原价{d.get('originalPrice', '?')}）"
             f" 库存{d.get('total', '?')}　剩{countdown(d.get('expiry', ''))}"
             for d in deals or []]
    return ("每日特惠", lines or ["今日暂无特惠"])


def fmt_steel_path(sp: Optional[dict]) -> tuple[str, list[str]]:
    if not sp:
        return ("钢铁之路", ["钢铁之路轮换为外部数据源，当前数据模式下不可用。",
                          "可用「裂隙 钢铁」查看钢铁之路裂隙。"])
    lines = []
    if sp.get("currentReward"):
        lines.append(f"当前侵蚀奖励：{sp['currentReward']}　剩余 {sp.get('remaining', '?')}")
    inc = sp.get("incursions") or []
    for i, it in enumerate(inc[:8], 1):
        lines.append(f"{i}. {it.get('node', '?')} · {mission_cn(it.get('missionType', ''))}")
    return ("钢铁之路轮换", lines or ["暂无侵蚀数据"])


# ---------------------------------------------------------------------------
# 警报 / 入侵 / 仲裁 / 赤毒 / 新闻 / 电波
# ---------------------------------------------------------------------------


_STEEL_SHOP_CACHE: Optional[dict] = None


def steel_shop() -> dict:
    """钢铁精华兑换表（core/data/de/steel_shop.json）。"""
    global _STEEL_SHOP_CACHE
    if _STEEL_SHOP_CACHE is None:
        try:
            p = Path(__file__).resolve().parent / "data" / "de" / "steel_shop.json"
            _STEEL_SHOP_CACHE = json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            _STEEL_SHOP_CACHE = {}
    return _STEEL_SHOP_CACHE


def _steel_rotation(data: dict) -> tuple[list[str], Optional[str], int]:
    """推算本周/下周轮换商品。

    轮换锚点写在 data["rotation"]：epoch 时刻生效的是 index_at_epoch 号条目，
    之后每 period_hours 小时整体前移一格（官方「左移」顺序）。
    返回 (预览行列表, 下次轮换时间 ISO, 轮次序号 1..N)。

    Args:
        data: steel_shop.json 内容。

    Returns:
        (lines, next_reset_iso, cycle_no)；数据缺失时 lines 为空。
    """
    weekly = data.get("weekly") or []
    if not weekly:
        return [], None, 0
    rot = data.get("rotation") or {}
    try:
        epoch = datetime.fromisoformat(rot.get("epoch") or data.get("weekly_epoch", ""))
        hours = int(rot.get("period_hours") or data.get("weekly_period_hours") or 168)
        base = int(rot.get("index_at_epoch", 0))
        # 用 floor 除法保证 epoch 之前/之后都能得到非负的周期数
        n = int((_now() - epoch).total_seconds() // 3600 // max(1, hours))
        idx = (base + n) % len(weekly)
        nxt_reset = (epoch + timedelta(hours=(n + 1) * hours)).isoformat()
    except Exception:  # noqa: BLE001
        idx, nxt_reset = 0, None
    cur = weekly[idx]
    nxt = weekly[(idx + 1) % len(weekly)]
    lines = [
        f"◆ 本周轮换（第 {idx + 1}/{len(weekly)} 周 · 每周限购 1 次）",
        f"· {cur['name']}　{cur['cost']} 精华",
    ]
    if nxt_reset:
        lines.append(f"◆ 下周轮换：{nxt['name']}（{nxt['cost']} 精华）　"
                     f"距轮换 {countdown(nxt_reset)}")
    else:
        lines.append(f"◆ 下周轮换：{nxt['name']}（{nxt['cost']} 精华）")
    return lines, nxt_reset, idx + 1


def fmt_steel_essence_shop() -> tuple[str, list[str]]:
    """钢铁之路 → 钢铁精华兑换（Teshin 荣誉商店）。

    常驻商品按 json 里的 `group` 字段分组展示；每周轮换按官方 8 周循环推算，
    轮换时刻与执刑官猎杀同步（周一 00:00 UTC）。

    Returns:
        (标题, 正文行列表)。
    """
    data = steel_shop()
    if not data:
        return ("钢铁精华兑换", ["兑换表缺失：core/data/de/steel_shop.json"])
    weekly = data.get("weekly") or []
    evergreen = data.get("evergreen") or []

    lines: list[str] = []
    rot_lines, _, _ = _steel_rotation(data)
    lines.extend(rot_lines)

    lines.append(f"◆ 常驻商品（共 {len(evergreen)} 件，可随时购买）")
    last_group = None
    for it in evergreen:
        grp = it.get("group")
        # 分组标题只在切换分组时输出，避免同一分类反复出现
        if grp and grp != last_group:
            lines.append(f"【{grp}】")
            last_group = grp
        lines.append(f"· {it['name']}　{it['cost']} 精华")

    total_all = sum(int(i.get("cost", 0)) for i in evergreen) + \
        sum(int(w.get("cost", 0)) for w in weekly)
    lines.append("─" * 24)
    lines.append("※ 数据源：wiki.warframe.com/w/Steel_Essence（每周轮换 8 件循环）")
    lines.append("※ 精华获取：钢铁之路侵蚀任务 5/个 · Acolyte 2/个 · "
                 "夜灵 1/个 · 钢铁裂隙开核桃 1/个 · 清完星球 25")
    lines.append("※ 轮换按官方锚点推算；各商品精华总价 "
                 f"{total_all}（常驻全买 + 轮换各一次）")
    return ("钢铁精华兑换（Teshin 荣誉商店）", lines)


def fmt_alerts(alerts: Iterable[dict]) -> tuple[str, list[str]]:
    lines = []
    for a in alerts or []:
        if not a.get("active", True):
            continue
        mission = a.get("mission", {}) or {}
        rewards = mission.get("reward", {}) or {}
        items = "/".join(x for x in [rewards.get("item"), rewards.get("credits")
                                     and f"{rewards['credits']}现金"] if x)
        lines.append(f"{mission.get('node', '?')} · {mission_cn(mission.get('type', ''))}"
                     f"　奖励：{items or '?'}　剩{countdown(a.get('expiry', ''))}")
    return ("警报", lines or ["当前没有进行中的警报"])


def _fac(side: dict) -> str:
    """派系显示名：有原始代码时回落到官方英文名。"""
    code = side.get("faction_code") or ""
    if code:
        return faction_name(code)
    return side.get("faction", "?")


def fmt_invasions(invasions: Iterable[dict],
                  page: int = 1, page_size: int = 6) -> tuple[str, list[str]]:
    """入侵：节点 + 双方阵营/奖励 + 双方占比，支持翻页。

    占比与游戏内一致：``攻击方 = (Goal + Count) / (2·Goal)``，防御方为补数。
    旧实现只给 ``Count/Goal``，负数被夹成 0%，于是「48% vs 52%」显示成
    「攻击方 0%（被反推 4%）」，与客户端对不上。

    Args:
        invasions: 解析后的入侵列表。
        page: 页码，从 1 开始。
        page_size: 每页场数（一场占 3~4 行，默认 6 场）。
    """
    live = [i for i in (invasions or []) if not i.get("completed")]
    total = len(live)
    if not total:
        return ("入侵", ["当前没有进行中的入侵"])
    pages = max(1, (total + page_size - 1) // page_size)
    page = max(1, min(page, pages))
    chunk = live[(page - 1) * page_size: page * page_size]
    lines = []
    for inv in chunk:
        at = inv.get("attacker", {}) or {}
        vs = inv.get("defender", {}) or {}
        at_fac = _fac(at)
        vs_fac = _fac(vs)
        at_pct = inv.get("attacker_pct", inv.get("completion", 0)) or 0
        vs_pct = inv.get("defender_pct", 100 - at_pct) or 0
        lines.append(f"◆ {inv.get('node', '?')}　{at_fac}（攻） vs {vs_fac}（守）")
        lead = at_fac if at_pct >= vs_pct else vs_fac
        lines.append(f"　　进度：{at_fac} {at_pct:.0f}% vs {vs_pct:.0f}% {vs_fac}"
                     f"（{lead} 领先）")
        at_items = at.get("items") or []
        vs_items = vs.get("items") or []
        if at_items:
            lines.append(f"　　协助 {at_fac}：{'、'.join(at_items)}")
        if vs_items:
            lines.append(f"　　协助 {vs_fac}：{'、'.join(vs_items)}")
        if not at_items and not vs_items:
            lines.append("　　双方均无奖励物品")
    if pages > 1:
        lines.append(f"※ 第{page}/{pages}页，共{total}场；加 -2 / -3 翻页")
    lines.append("※ 占比为双方推进度（合计 100%）；按争夺最激烈的排前面")
    lines.append("※ 选边前请核对两侧奖励，选错在结算前仍可更换")
    return ("入侵", lines)


_DT_TAG = re.compile(r"<DT_[A-Z]+>")


def _nw_desc(desc: str, required) -> str:
    """电波任务描述：替换 |COUNT| 占位符 + 去掉 <DT_XXX> 标记。

    DE 语言表里描述是模板串，例：
      「使用<DT_FIRE>火焰伤害击杀 |COUNT| 名敌人」-> 「使用火焰伤害击杀 150 名敌人」
    """
    if not desc:
        return ""
    out = _DT_TAG.sub("", desc).strip()
    if "|COUNT|" in out:
        out = out.replace("|COUNT|", str(required) if required else "N")
    return out


def fmt_nightwave(nw: Optional[dict]) -> tuple[str, list[str]]:
    if not nw:
        return ("午夜电波", ["数据暂不可用"])
    season = nw.get("season")
    lines: list[str] = []
    daily = [c for c in (nw.get("activeChallenges") or []) if c.get("isDaily")]
    weekly = [c for c in (nw.get("activeChallenges") or []) if not c.get("isDaily")]
    for group, tag in ((daily, "每日"), (weekly, "每周")):
        if not group:
            continue
        lines.append(f"◆ {tag}挑战（{len(group)} 项）")
        for ch in group[:8]:
            title = ch.get("title", "") or ("每日挑战" if ch.get("isDaily") else "每周挑战")
            elite = "·精英" if ch.get("isElite") else ""
            standing = ch.get("standing")
            st = f"　{standing} 声望" if standing else ""
            lines.append(f"· [{tag}{elite}] {title}　剩 {countdown(ch.get('expiry', ''))}{st}")
            desc = _nw_desc(ch.get("desc") or "", ch.get("required"))
            if desc:
                lines.append(f"　　{desc}")
        if len(group) > 8:
            lines.append(f"　……另有 {len(group) - 8} 项")
    if season:
        lines.insert(0, f"第 {season} 季" + (f"　剩余 {countdown(nw.get('expiry', ''))}"
                                            if nw.get("expiry") else ""))
    lines.append("※ 完成挑战得电波声望；奖励在电波等级轨道上解锁（满级约 30 级）")
    return ("午夜电波", lines or ["当前没有激活的挑战"])


def fmt_kuva(missions: Iterable[dict]) -> tuple[str, list[str]]:
    lines = [f"{m.get('node', '?')} · {mission_cn(m.get('type', ''))} · {m.get('enemy', '?')}"
             for m in missions or []]
    return ("赤毒/血紊虹吸", lines or ["当前没有赤毒任务"])


def _clean_url(url: str) -> str:
    """去掉 utm 追踪参数，缩短新闻链接。"""
    return re.sub(r"[?&]utm_[a-z]+=[^&]*", "", url or "").rstrip("?&")


def fmt_news(news: Iterable[dict], limit: int = 6) -> tuple[str, list[str]]:
    """最近新闻。优先官方公告（community=False），社区贴只占补充位。

    中文来源：DE 在 Events.Messages 里直接下发 ``zh`` 全句，所以这里已经是官方简中，
    不再走英文兜底（旧实现取 ``en``，于是西语/波兰语的论坛贴也被当成新闻）。
    """
    items = [n for n in (news or []) if (n.get("message") or "").strip()]
    official = [n for n in items if not n.get("community")]
    community = [n for n in items if n.get("community")]
    picked = (official + community)[:limit]
    lines = []
    for n in picked:
        msg = (n.get("message") or "").strip()
        link = _clean_url(n.get("link", ""))
        tag = "" if not n.get("community") else "［社区］"
        lines.append(f"{tag}{msg}" + (f"　{link}" if link else ""))
    return ("最近新闻", lines or ["暂无新闻"])


# ---------------------------------------------------------------------------
# 赏金 / 结合目标 / 建造进度 / 深层科研
# ---------------------------------------------------------------------------

SYNDICATE_BOUNTY = {"Ostrons": "希图斯（地球）", "Solaris United": "奥布斯山谷（金星）",
                    "Entrati": "英择谛（魔胎之境）", "The Hex": "六人组（1999）",
                    "EntratiLab": "解剖圣所（实验室）", "HexCity": "霍瓦尼亚（1999）",
                    "Holdfasts": "羽化之穹（扎里曼）"}

_MOD_NAMES: Optional[set] = None
_RECIPE_NAMES: Optional[set] = None


def _de_names(filename: str) -> set:
    """读取 build_de_data.py 生成的官方名集合（MOD / 可制造物）。"""
    try:
        return set(json.loads((Path(__file__).parent / "data" / "de" / filename)
                              .read_text(encoding="utf-8")))
    except Exception:  # noqa: BLE001
        return set()


def _fmt_pool_items(items: list) -> str:
    """赏金奖励：合并同名（数量取最高，不累加），部件蓝图 / MOD 分别着色。

    着色标记交给 render.py 的 _draw_tokens 解析：
      ▣ 战甲武器部件、蓝图 -> 橙金
      ★ MOD                -> 亮金

    判定优先用 DE 官方名集合（``mod_names_zh.json`` / ``recipe_names_zh.json``，
    由 ExportUpgrades / ExportRecipes + 官方简中词表生成），关键词表只作兜底 ——
    旧实现纯靠关键词，「混沌蛇主之苦」「欺谋狼主之恨」这类 MOD 就不带标记。
    """
    global _MOD_NAMES, _RECIPE_NAMES
    if _MOD_NAMES is None:
        _MOD_NAMES = _de_names("mod_names_zh.json")
    if _RECIPE_NAMES is None:
        _RECIPE_NAMES = _de_names("recipe_names_zh.json")
    _num_re = re.compile(r"^([\d,]+)\s*[×x]\s*(.+)$")
    merged: dict[str, str] = {}
    order: list[str] = []
    for raw in items:
        raw = raw.strip().lstrip("★▣")   # 去掉可能已带的着色标记，保证幂等
        m = _num_re.match(raw)
        if m:
            qty, name = m.group(1), m.group(2).strip()
        else:
            qty, name = "", raw
        key = name.replace(" ", "").lower()
        if key in merged:
            old_q = merged[key].split("×")[0].strip().replace(",", "") \
                if "×" in merged[key] else "0"
            try:
                if int(qty.replace(",", "") or 0) > int(old_q or 0):
                    merged[key] = raw
            except ValueError:
                pass
        else:
            merged[key] = raw
            order.append(key)
    out = []
    mod_kw = ("预言", "角斗士", "私法", "技法", "合成", "机甲", "预见", "通灵",
              "电涌", "狂暴化", "致残突击", "得到救赎", "生长之力", "反抗军",
              "快枪手", "失效", "震惊", "蛇主", "狼主", "枭主")
    # 注意不含单字“刃”：否则“尖刃弹头”这类 MOD 会被误判成武器部件。
    part_kw = ("蓝图", "枪管", "枪机", "枪托", "机体", "头部", "系统", "握柄",
               "刀刃", "弓弦", "星体", "护手", "刀身", "剑柄")
    for key in order:
        raw = merged[key]
        base = re.sub(r"\s*蓝图$", "", raw).strip()
        is_mod = raw in _MOD_NAMES or any(k in raw for k in mod_kw)
        is_part = (base in _RECIPE_NAMES or raw in _RECIPE_NAMES
                   or any(k in raw for k in part_kw))
        if is_mod and not is_part:
            out.append(f"★{raw}")
        elif is_part:
            out.append(f"▣{raw}")
        else:
            out.append(raw)
    return "、".join(out)


try:
    from pathlib import Path as _P
    _BOUNTY_POOLS = json.loads((_P(__file__).parent / "data" / "bounty_pools.json")
                               .read_text(encoding="utf-8"))
except Exception:  # noqa: BLE001
    _BOUNTY_POOLS = {}


# DE 奖励池表名里的 Tier -> bounty_pools 的通用档位键（TierA..E 即 1..5 阶）
_TIER_POOL_KEY = {"TierA": "阶段1", "TierB": "阶段2", "TierC": "阶段3",
                  "TierD": "阶段4", "TierE": "阶段5"}

# 但部分 syndicate 的 Tier 字母并不按等级升序，必须按 syndicate 覆盖：
# 火卫二实测 TierA=5-15、TierC=15-25、TierB=25-30、TierD=30-40、TierE=40-60，
# 若沿用字母顺序会把「阶段2(蛮暴之力)」与「阶段3(露天开采)」整体互换。
_TIER_POOL_KEY_BY_SYNDICATE = {
    "Entrati": {"TierA": "阶段1", "TierC": "阶段2", "TierB": "阶段3",
                "TierD": "阶段4", "TierE": "阶段5"},
}

# 非 TierA-E 的特殊池：按 syndicate + rewards 表名特征定位
_SPECIAL_POOL_KEY = {
    # 合一众（Narmer）赏金：地球与金星共用同一张 NarmerTableXRewards，
    # 池内容也一致，故两边都指向唯一的「合一众」键。
    # （旧实现把地球的指到 `尸鬼净化`、金星的指到 `合一众·阶段6`，两处内容都是错的）
    "Ostrons": {"Narmer": "合一众"},
    "Solaris United": {"Narmer": "合一众"},
    # 隔离库三档在 DE 侧各有独立奖励表：VaultBountyTierA/B/C
    # 分别对应 30-40 / 40-50 / 50-60 级，旧实现把三者并成一个「隔离库」池，
    # 导致三档奖励完全一样。
    "Entrati": {"VaultBountyTierA": "隔离库1阶",
                "VaultBountyTierB": "隔离库2阶",
                "VaultBountyTierC": "隔离库3阶"},
}
_SPECIAL_TAG = {"Narmer": "合一众",
                "VaultBountyTierA": "隔离库1阶",
                "VaultBountyTierB": "隔离库2阶",
                "VaultBountyTierC": "隔离库3阶"}

_CONTINENT_TARGET = {
    "地球": "Ostrons", "夜灵": "Ostrons", "希图斯": "Ostrons", "尸鬼": "Ostrons",
    "金星": "Solaris United", "山谷": "Solaris United", "奥布": "Solaris United",
    "索拉里": "Solaris United", "福尔图娜": "Solaris United",
    "深矿": "Solaris United", "抢劫": "Solaris United",
    "火卫二": "Entrati", "魔胎": "Entrati", "英择谛": "Entrati",
    "隔离库": "Entrati",
    "六人组": "HexCity", "1999": "HexCity", "霍瓦尼亚": "HexCity",
    "实验室": "EntratiLab", "解剖": "EntratiLab", "圣所": "EntratiLab",
    "扎里曼": "Holdfasts", "羽化": "Holdfasts", "虚空天使": "Holdfasts",
}


def _resolve_bounty_pool(syndicate: str, job: dict) -> tuple[dict, str, str]:
    """按 DE 下发的 rewards 表名定位奖励池，返回 (池内容, 语义标签, 当前轮次)。

    为何不按“第几个 job -> 阶段N”推断：Entrati 的 job 顺序与 Tier 并不对应
    （job2 是 TierC、job3 是 TierB），按位置索引会整体错位；且 100-100 档的
    Tier 与 40-60 档相同，旧实现把它硬套到钢铁之路池，于是两档奖励完全重复。
    以 DE 的 rewards 表名（VenusTierETableARewards / NarmerTableARewards …）
    为准才可靠。

    表名末段的 TableA/B/C 即该赏金**当前生效的轮次**（如
    VaultBountyTierATableBRewards -> B 轮），直接取自 DE，无需自己推算。
    """
    pools = _BOUNTY_POOLS.get(syndicate or "", {})
    table = job.get("rewardTable") or ""
    tag, key = "", ""
    for mark, t in _SPECIAL_TAG.items():
        if mark in table:
            k = _SPECIAL_POOL_KEY.get(syndicate, {}).get(mark, "")
            if k:      # 该 syndicate 未配置此特殊池时不打标签，回落 Tier 匹配
                tag, key = t, k
            break
    if not key:
        m = re.search(r"Tier([A-E])", table)
        if m:
            tbl = _TIER_POOL_KEY_BY_SYNDICATE.get(syndicate, _TIER_POOL_KEY)
            key = tbl.get("Tier" + m.group(1), "")
    lv = job.get("enemyLevels") or []
    if not tag and len(lv) >= 2 and lv[0] == lv[1] == 100:
        tag = "钢铁之路"      # 100-100 的常规赏金即钢铁之路变体
    rm = re.search(r"Table([ABC])Rewards", table)
    return (pools.get(key) or {}), tag, (rm.group(1) if rm else "")


# ---------------------------------------------------------------------------
# 赏金：高价值奖励过滤 + 地区轮换汇总
# ---------------------------------------------------------------------------

# 用户关心的只有这几类：MOD（★）/ 部件·蓝图（▣）/ 债券 / 遗物 / 各地区特色资源。
# 现金匣、内融核心、聚魂晶体、赤毒、各种鱼和矿石每档都一样，列出来只会淹掉重点。
_HV_KEEP_KW = ("债券", "遗物", "绒翎", "音魂", "浆质", "阿耶檀识琥珀星",
               "赋能槽连接器")
# ▣ 是渲染层按关键词猜的，个别资源会被误标成「部件」——这里再摘掉。
_HV_DROP_KW = ("神经元", "奥罗金电池")


def _is_high_value(token: str) -> bool:
    """★MOD / ▣部件·蓝图 无条件保留，另保留债券、遗物与地区特色资源。"""
    t = token.strip()
    if any(k in t for k in _HV_DROP_KW):
        return False
    return t.startswith(("★", "▣")) or any(k in t for k in _HV_KEEP_KW)


def _fmt_high_value(items: Iterable[str]) -> str:
    """只保留高价值奖励：MOD（★）/ 部件·蓝图（▣）/ 债券 / 遗物 / 特色资源。

    先交给 :func:`_fmt_pool_items` 合并同名并打上 ★/▣ 标记，再按标记与关键词
    筛选 —— 顺序不能反，否则「Nokko机体蓝图」这种表里没带标记的部件
    无法与货币区分开。
    """
    merged = _fmt_pool_items(list(items))
    return "、".join(t for t in merged.split("、") if t and _is_high_value(t))


def _ordered_tier_keys(pool: dict) -> list[str]:
    """档位键排序：具名档（阶段N / 隔离库N阶）保持文件顺序，等级档按等级升序。"""
    def _lv_num(key: str) -> int:
        m = re.search(r"\d+", key)
        return int(m.group()) if m else 0

    named = [k for k in pool if not k.startswith("等级")]
    lv = [k for k in pool if k.startswith("等级")]
    lv.sort(key=_lv_num)
    return named + lv


def _region_tier_keys(pool: dict) -> list[str]:
    """地区级轮换只统计真正的赏金档位。

    抢劫 / 深矿 / 尸鬼净化是**另一套活动**的奖励池（抢劫有 27 条、深矿 3 板），
    混进来会把「陀螺磁抵系统、维加环形装置…」也算成本地区赏金该给的东西。
    """
    keep = [k for k in pool
            if k.startswith("阶段") or k.startswith("等级")
            or k == "合一众" or k.startswith("隔离库")]
    return _ordered_tier_keys({k: pool[k] for k in keep})


def _pool_at_rot(pool: dict, rot: str) -> list:
    """取某档在当前轮次下的奖励；该轮次缺内容时回落为 A/B/C 合并。"""
    if rot and pool.get(rot):
        return list(pool[rot])
    out: list = []
    for r in ("A", "B", "C"):
        out.extend(pool.get(r) or [])
    return out


# ---------------------------------------------------------------------------
# 赏金：任务名 / 任务目标 的数据来源
# ---------------------------------------------------------------------------
#   · 地球 / 金星 / 火卫二：DE 的 worldState 直接下发 Jobs（含 jobType）
#     → 任务名查 core/data/bounty_job_names.json
#   · 扎里曼 / 解剖圣所 / 1999：DE 侧 Jobs **恒为空**，只有 browse.wf 的 oracle
#     给出本轮节点（SolNode###）与挑战（/Lotus/Types/Challenges/…）
#     → 节点名查 nodes_zh.json、挑战名与目标查 challenges_zh.json
_ORACLE_REGIONS: tuple[tuple[str, str, str], ...] = (
    # (bounty_pools 键, oracle 的 SyndicateMissions Tag, 显示名)
    ("Holdfasts", "ZarimanSyndicate", "羽化之穹（扎里曼）"),
    ("EntratiLab", "EntratiLabSyndicate", "解剖圣所（实验室）"),
    ("HexCity", "HexSyndicate", "霍瓦尼亚（1999）"),
)

# pool 键 → oracle 的 SyndicateMissions Tag（DE 地区不进这张表）
_ORACLE_TAGS: dict[str, str] = {k: t for k, t, _ in _ORACLE_REGIONS}

# 赏金地区的**剧情推进顺序**（从最早的夜灵平野到最新的 1999）。
# DE 的 SyndicateMissions 数组顺序**不保证**（实测会把火卫二排在金星前面），
# 所以一览与详情都按这张显式表排列，不再依赖数据源顺序。
_BOUNTY_REGION_ORDER: tuple[str, ...] = (
    "Ostrons",         # 夜灵平野（地球，2017）
    "Solaris United",  # 奥布山谷（金星，2018）
    "Entrati",         # 魔胎之境（火卫二，2020）
    "Holdfasts",       # 羽化之穹（扎里曼，2022）
    "EntratiLab",      # 解剖圣所（实验室，2023）
    "HexCity",         # 霍瓦尼亚（1999，2024）
)

_DE_ZH_CACHE: dict[str, dict] = {}

# 隔离库三档在 DE 侧 jobType 为空，任务名只能按奖励池标签回填。
# 官方简中文案（languages_zh 的 NecraloidStanding*ItemDesc）里叫「等级 1 / 2 / 3 隔离库赏金」，
# 直接用池键「隔离库1阶」当任务名玩家对不上。
_VAULT_TIER_NAME = {"隔离库1阶": "1 级隔离库赏金",
                    "隔离库2阶": "2 级隔离库赏金",
                    "隔离库3阶": "3 级隔离库赏金"}

# 赏金末阶段遭遇战（ExportBounties.stages[-1]）-> 显示用任务类型。
#
# 为什么拿「末阶段」当任务类型：DE 下发的 job 只有 ``jobType`` 资产路径与
# 奖励表，**不带节点**，所以开放世界赏金没有节点级 missionType 可查；
# ExportBounties 的 stages 末项就是这条赏金的收官任务，实测与官方赏金名
# 一一吻合（核心样本→挖掘、粉碎邪教→歼灭、猎人杀手→歼灭、尘土部队→资源回收）。
# 名字尽量取官方 MT 译名（歼灭/刺杀/捕获/挖掘/防御/破坏/救援/间谍/劫持），
# 官方没有对应 MT 的（伏击 / 资源回收 / 物资回收 / 净化）沿用赏金里的叫法。
_BOUNTY_ENC_ZH = {
    "DynamicExterminate": "歼灭",
    "DynamicCaveExterminate": "歼灭",
    "DynamicExterminateDrones": "歼灭",
    "DynamicExterminateMoas": "歼灭",
    "DynamicAssassinate": "刺杀",
    "DynamicCapture": "捕获",
    "DynamicExcavation": "挖掘",
    "DynamicExcavationEndless": "挖掘",
    "DynamicDefend": "防御",
    "DynamicAreaDefense": "防御",
    "DynamicDroneDefense": "防御",
    "DynamicSabotage": "破坏",
    "DynamicRescue": "救援",
    "DynamicBaseSpy": "间谍",
    "DynamicCaches": "破坏",
    "DynamicCachesAirDrop": "破坏",
    "HiddenResourceCaches": "破坏",
    "HiddenResourceCachesCave": "破坏",
    "DynamicCorpusSurvivors": "生存",
    "DynamicGrineerSurvivors": "生存",
    "DynamicHijack": "劫持",
    "DynamicRecovery": "回收",
    "DynamicResourceCapture": "资源回收",
    "DynamicResourceTheft": "资源回收",
    "DynamicKeyPieces": "物资回收",
    "DynamicPurify": "净化",
    "DynamicAmbush": "伏击",
}


def _bounty_type(job: dict) -> str:
    """DE 侧赏金的任务类型（来自末阶段遭遇战；拿不到就返回空串）。

    ``NarmerDynamicXxx`` 先剥掉 ``Narmer`` 前缀再查表；末阶段若是多选
    （无尽赏金会有 4 种可能），取第一个——那个才是这一档实际的收官任务。
    """
    codes = job.get("_jobFinal") or []
    if isinstance(codes, str):
        codes = [codes]
    for code in codes:
        key = code[len("Narmer"):] if code.startswith("Narmer") else code
        zh = _BOUNTY_ENC_ZH.get(key) or _BOUNTY_ENC_ZH.get(code)
        if zh:
            return zh
    return ""


def _tier_of(job: dict) -> tuple[int, int]:
    """档位排序键：敌人等级区间下限（无数据排最后）。"""
    lv = job.get("enemyLevels") or []
    lo = lv[0] if len(lv) >= 1 and isinstance(lv[0], int) else -1
    hi = lv[1] if len(lv) >= 2 and isinstance(lv[1], int) else lo
    return (lo, hi)


def _top_tiers(jobs: list[dict], n: int) -> list[dict]:
    """一览/详情用的「等级最高的 N 档」，返回时按等级升序。

    ⚠️ 不能用 ``jobs[-N:]``：DE 下发的顺序里，火卫二的 3 个隔离库档是**追加在
    末尾**的（等级只有 30-60），直接取末尾会把高等级档挤掉；参考版式选的是
    「核心样本 40-60 + 蛮暴之力 100-100」这类高等级档。按等级取前 N 再升序排列，
    地球 / 金星 / 火卫二三处的结果都与参考版式一致。
    """
    if not jobs:
        return []
    picked = sorted(jobs, key=_tier_of, reverse=True)[:n]
    return sorted(picked, key=_tier_of)



def _de_zh(filename: str) -> dict:
    """惰性读取 core/data/de 下的官方简中表（nodes_zh / challenges_zh …）。"""
    if filename not in _DE_ZH_CACHE:
        try:
            _DE_ZH_CACHE[filename] = json.loads(
                (Path(__file__).parent / "data" / "de" / filename)
                .read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            _DE_ZH_CACHE[filename] = {}
    return _DE_ZH_CACHE[filename]


# DE 挑战文案里的富文本标记：|COUNT| 要换成实际数量，|OPEN_COLOR| 之类直接去掉。
def _clean_challenge_desc(desc: str, count) -> str:
    txt = (desc or "").replace(
        "|COUNT|", str(count) if count not in (None, "") else "N")
    txt = re.sub(r"\|[A-Z_]+\|", "", txt)
    txt = re.sub(r"^[^。\s　]*赏金任务[\s　]*", "", txt)   # 「…的赏金任务」前缀
    return re.sub(r"\s+", " ", txt).strip()


def _de_task_line(job: dict) -> str:
    """DE 地区档位的「任务：」行 —— 用 ``ExportBounties`` 的官方目标描述。

    用户反馈 DE 三地区（地球/金星/火卫二）「貌似也没任务详情」：其实 DE 有 ——
    ``ExportBounties`` 每条赏金都带 ``description``（这条赏金要干什么），
    之前只是没接。DE 侧没有挑战名/节点（job 只有资产路径），所以只有描述一段；
    任务类型已在档位行里上过色，这里不再重复。
    """
    desc = (job.get("_jobDesc") or "").strip()
    if not desc:
        return ""
    return f"　　任务：{desc}"


def _bounty_rows(jobs: list[dict], syndicate: str) -> list[str]:
    """DE 下发 jobs 的地区：每档两行「任务名｜N-M级｜标签」+ 该档完整奖励。

    等级留在行内、由渲染层 :data:`core.render._LV_RE` 抽成右对齐等级列；
    奖励**列全部**（含现金匣 / 内融核心），这是「赏金 地球」这类详情页的
    既定形态（v1.1 起就是这样，别过滤，过滤掉会让用户以为奖励丢了）。

    ``任务类型`` 前缀来自赏金末阶段（``_bounty_type``），隔离库三档 DE 侧
    jobType 为空、拿不到类型，此时不写前缀（宁可少写，别编）。
    """
    out: list[str] = []
    for j in jobs:
        lv = j.get("enemyLevels") or []
        lv_txt = f"{lv[0]}-{lv[1]}级" if len(lv) >= 2 else "?"
        pool, tag, rot = _resolve_bounty_pool(syndicate, j)
        name = j.get("_jobName") or ""
        if not name and tag:
            # 无任务名（如隔离库）时用池标签充当名称，避免等级顶到名称位
            name, tag = _VAULT_TIER_NAME.get(tag, tag), ""
        # 只列 DE 当前生效的那一轮；池里没有该轮次（或本就只有一轮）时合并去重
        filled = [r for r in ("A", "B", "C") if pool.get(r)]
        if rot and len(filled) > 1 and pool.get(rot):
            items = list(pool[rot])
        else:
            items = []
            for r in ("A", "B", "C"):
                items.extend(pool.get(r) or [])
        out.append(_bounty_head(name, _bounty_type(j), lv_txt, tag))
        _task = _de_task_line(j)
        if _task:
            out.append(_task)
        if items:
            out.append("　　" + _fmt_pool_items(items))
    return out


def _bounty_head(name: str, mtype: str, lv_txt: str, tag: str) -> str:
    """档位行：``　· 任务类型 任务名｜N-M级｜标签``（缺失的段落自动省略）。

    任务类型放在**行首**（与参考版式「挖掘 核心样本」「歼灭 带他们回家 合一众」
    一致）：玩家扫一眼就知道这档是什么任务，而后面的等级会被渲染层抽成右对齐列。

    赏金名里已经含类型词的（官方名「刺杀指挥官」「物资回收」「破坏 Grineer 的
    补给线」「伏击信使」）不再重复加前缀，否则会出现「物资回收 物资回收」。
    """
    if mtype and name and mtype in name:
        mtype = ""
    body = " ".join(x for x in (mtype, name) if x) or lv_txt
    if body == lv_txt:
        return f"　· {lv_txt}" + (f"｜{tag}" if tag else "")
    head = f"　· {body}｜{lv_txt}"
    if tag:
        head += f"｜{tag}"
    return head


def _region_block(syndicate: str, title: str, jobs: list[dict],
                  expiry: str) -> list[str]:
    """DE 下发 jobs 的地区（详情）：地区横幅 + 各档任务名与完整奖励。"""
    lines = [f"◆ {title}　剩{countdown(expiry)}"]
    lines.extend(_bounty_rows(jobs, syndicate))
    return lines


# 一览每地区列几个档位：取**等级最高的 3 档**（`_top_tiers`）。
# 不能用「DE 下发顺序的末尾 N 个」——火卫二的 3 个隔离库档（30-60 级）追加在
# 末尾，会把 100-100 的「蛮暴之力」挤掉；参考版式选的正是最高等级那几档
# （地球 = 40-60 刺杀指挥官 / 50-70 带他们回家 / 100-100 取回被偷的器物）。
_OVERVIEW_N = 3


def _region_rot_of(jobs: list[dict]) -> str:
    """从 DE 的 rewardTable 表名解析当前轮次（A/B/C）。"""
    for j in jobs:
        m = re.search(r"Table([ABC])Rewards", j.get("rewardTable") or "")
        if m:
            return m.group(1)
    return ""


def _rot_lines(region_pool: dict, rot: str, limit: int = 6) -> list[str]:
    """一览的「轮换」行：每地区**只出一行**、最多 ``limit`` 项、**不写省略号**。

    参考版式的地球「MOD 奖励轮换」、金星「债券轮换」、火卫二「隔离库轮换」
    就是这一行。旧实现把 A/B/C 与全部档位合并后逐类铺开（地球 16 项 MOD +
    10 项部件 + 「…等 N 项」），又长又杂（用户反馈「搞得太多了」）。

    现在：跨档位合并 → 按 债券 > MOD > 部件 选一类 → 只列前 ``limit`` 项。
    想看某个档位的完整奖励，用「赏金 <地区>」详情页。
    """
    items: list = []
    for key in _region_tier_keys(region_pool):
        items.extend(_pool_at_rot(region_pool.get(key) or {}, rot))
    merged = [t for t in _fmt_pool_items(list(items)).split("、")
              if t and _is_high_value(t)]
    if not merged:
        return []
    bonds = [t for t in merged if "债券" in t]
    mods = [t for t in merged if t.startswith("★")]
    parts = [t for t in merged if t.startswith("▣") and t not in bonds]
    name, lst = "本轮轮换", merged
    if bonds:
        name, lst = "债券轮换", bonds
    elif mods:
        name, lst = "MOD 轮换", mods
    elif parts:
        name, lst = "部件轮换", parts
    head = "、".join(lst[:limit])
    return [f"　{name}（{rot}）：{head}" if rot else f"　{name}：{head}"]


def _bounty_entry_line(syndicate: str, job: dict) -> str:
    """一览的档位行：``　· 任务类型 任务名｜N-M级｜标签``（不带奖励）。"""
    lv = job.get("enemyLevels") or []
    lv_txt = f"{lv[0]}-{lv[1]}级" if len(lv) >= 2 else "?"
    _pool, tag, _rot = _resolve_bounty_pool(syndicate, job)
    name = job.get("_jobName") or ""
    if not name and tag:
        name, tag = _VAULT_TIER_NAME.get(tag, tag), ""
    return _bounty_head(name, _bounty_type(job), lv_txt, tag)


def _region_summary(syndicate: str, title: str, jobs: list[dict],
                    expiry: str) -> list[str]:
    """一览（DE 地区）：地区横幅 + 轮换行 + 等级最高的若干档。"""
    region_pool = _BOUNTY_POOLS.get(syndicate) or {}
    lines = [f"◆ {title}　剩{countdown(expiry)}"]
    lines.extend(_rot_lines(region_pool, _region_rot_of(jobs)))
    for j in _top_tiers(jobs or [], _OVERVIEW_N):
        lines.append(_bounty_entry_line(syndicate, j))
        _task = _de_task_line(j)
        if _task:
            lines.append(_task)
    return lines


def _one_word(text: str) -> str:
    """把内部 ASCII 空格换成 NBSP（U+00A0）。

    渲染层靠空格把「任务：**类型** **挑战名** 目标」切成三段分别上色，
    所以前两段必须是**单个词**；挑战名里确实可能带空格（「任务完成 X」这类），
    换 NBSP 后仍是同一个词、视觉上仍是空格，不会串色。
    """
    return (text or "").replace(" ", "\u00a0")


def _oracle_task_lines(node_key: str, ch_path: str) -> list[str]:
    """oracle 地区的「任务：」行 —— ``任务：任务类型 挑战名 目标``。

    * **任务类型**取该节点的官方 ``missionName``（``nodes_zh[key]['type']``，
      如 哈拉科防线→歼灭、翠径→移动防御）。这是唯一权威来源：oracle 只给
      节点 key + 挑战路径，DE 又不给这三个地区的 jobs，所以没有别的路。
    * **挑战名**是这条赏金的挑战标题（能量超载 / 终结好戏 / 致命低语…）。
      2026-09-12 为了对齐参考版式曾把它去掉，用户随即反馈「这些任务名字怎么没了」，
      现已恢复：三段各自上色（类型=青、挑战名=紫、目标=正文色）。
    * **目标描述**取挑战的 desc（``|COUNT|`` 已替换）。整句**不再按句号拆行**
      —— 曾把第二句当「副目标」缩进一级，用户确认「第二个科腐者」其实是
      另一档赏金、不是副目标，那个分级已回退。
    """
    if not ch_path:
        return []
    node = _de_zh("nodes_zh.json").get(node_key or "") or {}
    mtype = _one_word((node.get("type") or "").strip())
    ch = _de_zh("challenges_zh.json").get(ch_path) or {}
    cname = _one_word((ch.get("name") or "").strip())
    goal = _clean_challenge_desc(ch.get("desc"), ch.get("count"))
    head = " ".join(x for x in (mtype, cname) if x)
    if not head and not goal:
        return []
    return [f"　　任务：{head + ' ' if head and goal else head}{goal}".rstrip()]


def _oracle_summary(pool_key: str, tag: str, title: str,
                    bounties: list[dict], rot: str = "") -> list[str]:
    """一览（oracle 地区）：地区横幅 + 轮换行 + 等级最高的若干档（含任务）。"""
    region_pool = _BOUNTY_POOLS.get(pool_key) or {}
    if not region_pool:
        return []
    tiers = _ordered_tier_keys(region_pool)

    def _lv_txt(key: str) -> str:
        return key.replace("等级", "") + "级" if key.startswith("等级") else key

    lines = [f"◆ {title}"]
    lines.extend(_rot_lines(region_pool, rot))
    node_tbl = _de_zh("nodes_zh.json")
    if not bounties:
        for lv in tiers[-_OVERVIEW_N:]:
            lines.append(f"　· {_lv_txt(lv)}")
        return lines
    # oracle 的节点列表本来就是等级升序，取末尾 N 个即「等级最高的 N 档」
    for i, b in list(enumerate(bounties))[-_OVERVIEW_N:]:
        lv = tiers[i] if i < len(tiers) else (tiers[-1] if tiers else "")
        node_key = b.get("node") or ""
        node = (node_tbl.get(node_key) or {}).get("name") or ""
        if not node:
            continue
        lines.append(f"　· {node}｜{_lv_txt(lv)}")
        lines.extend(_oracle_task_lines(node_key, b.get("challenge") or ""))
    return lines


def _oracle_region_block(pool_key: str, tag: str, title: str,
                         bounties: list[dict], rot: str = "") -> list[str]:
    """扎里曼 / 解剖圣所 / 1999（详情）：节点 + 挑战来自 browse.wf oracle。

    oracle 给出的节点顺序与奖励池的等级档升序一一对应（实测扎里曼
    SolNode233 奥金工场 = 90-95 档、SolNode231 哈拉科防线 = 110-115 档，
    与兔子卡面完全一致），所以按位置 zip 即可，不必另找等级映射表。
    奖励与 DE 地区同样**列全部**；oracle 不可达时退回「按等级档列奖励」。
    """
    region_pool = _BOUNTY_POOLS.get(pool_key) or {}
    if not region_pool:
        return []
    tiers = _ordered_tier_keys(region_pool)

    def _lv_txt(key: str) -> str:
        return key.replace("等级", "") + "级" if key.startswith("等级") else key

    if not bounties:
        # oracle 拿不到 → 按等级档列全部奖励（rot="" 时 _pool_at_rot 合并 A/B/C）
        lines = [f"◆ {title}"]
        for lv in tiers:
            items = _pool_at_rot(region_pool.get(lv) or {}, "")
            if items:
                lines.append(f"　· {_lv_txt(lv)}")
                lines.append("　　" + _fmt_pool_items(items))
        return lines if len(lines) > 1 else []

    node_tbl = _de_zh("nodes_zh.json")
    lines = [f"◆ {title}"]
    for i, b in enumerate(bounties):
        lv = tiers[i] if i < len(tiers) else (tiers[-1] if tiers else "")
        node_key = b.get("node") or ""
        if not (node_tbl.get(node_key) or {}).get("name"):
            continue
        lines.append(f"　· {node_tbl[node_key]['name']}｜{_lv_txt(lv)}")
        lines.extend(_oracle_task_lines(node_key, b.get("challenge") or ""))
        items = _pool_at_rot(region_pool.get(lv) or {}, rot)
        if items:
            lines.append("　　" + _fmt_pool_items(items))
    return lines


def _tent_lines(syndicates) -> list[str]:
    """小帐篷 A/B/C 当前赏金（详情卡专用；推算见 core/tents.py）。

    2026-09-24 双向对拍一致后才上卡：W4 与沃沃截图 9/9 格、W5 与
    oracle.browse.wf 独立实现 15/15 点位。种子缺失（源降级）时整块跳过。
    """
    rows = _tents.region_locations("Ostrons", _tents.seed_of(syndicates))
    if not rows:
        return []
    out = [f"　{label}：{'｜'.join(names)}" for label, names in rows]
    out.append("※ 小帐篷 = 平野三处营地的当前赏金"
               "（按 DE 世界种子推算，与游戏内一致）")
    return out


def fmt_bounties(syndicates: Iterable[dict], keyword: str = "",
                 cycle: Optional[dict] = None) -> tuple[str, list[str]]:
    """赏金：裸指令 = 一览（地区分组 + 轮换行 + 高等级档），带地区词 = 详情。

    **一览（无参）**：每地区「◆ 地区　剩X」+「MOD/债券/部件 轮换」行 +
    该地区末尾几个档（DE 下发顺序里钢铁之路 / 合一众 / 隔离库排在最后，
    正是玩家关心的）；扎里曼 / 实验室 / 1999 另给「任务：挑战 · 目标」。
    一览**不列每档奖励**，也**不联网取 oracle**（拿不到轮次就按 A/B/C 合并）。

    **详情（「赏金 地球」等）**：地区横幅 + 每档「任务名｜N-M级｜标签」
    + 该档**完整奖励**（含现金匣 / 内融核心，不过滤——过滤会让人以为漏了）。

    奖励着色标记：▣ 部件/蓝图（橙金）、★ MOD（亮金）。

    **关键词无法识别时明确报错**，而不是把全部板倒出来
    （旧行为：用户随便发个词就拿到一整屏所有地区的池子，看起来像乱码）。

    关键词表只收 DE 数据里**确实存在**的地区/活动名：「深坑」这类查不到来源的
    别名已于 2026-09-12 删除（用户要求），发了会落到「未识别地区」提示。
    """
    cycle = cycle or {}
    oracle = cycle.get("bounties") or {}

    target = None
    if keyword:
        for k, v in _CONTINENT_TARGET.items():
            if k in keyword:
                target = v
                break
        if target is None:
            return ("赏金任务", [
                f"未识别地区「{keyword}」。可用关键词："
                "地球 / 金星 / 火卫二（隔离库）/ 扎里曼 / 圣所（实验室）/ 1999",
                "例：赏金 地球｜赏金 隔离库｜赏金 扎里曼｜赏金 圣所｜赏金 1999"])

    lines: list[str] = []
    boards: set = set()
    detailed = target is not None
    rot = (cycle.get("rot") or "").strip()

    # DE 下发的地区先建索引，便于按剧情顺序取用
    de_by_synd: dict[str, dict] = {}
    for s in syndicates or []:
        synd = s.get("syndicate") or ""
        if synd in _BOUNTY_POOLS:
            boards.add(synd)
            de_by_synd[synd] = s

    for pool_key in _BOUNTY_REGION_ORDER:
        boards.add(pool_key)
        if target and pool_key != target:
            continue
        title = SYNDICATE_BOUNTY.get(pool_key, pool_key)
        tag = _ORACLE_TAGS.get(pool_key)
        if tag is not None:
            # ② DE 不下发 Jobs、只能靠 oracle 拿节点与挑战的地区
            bounty_list = oracle.get(tag) or []
            if detailed:
                lines.extend(_oracle_region_block(pool_key, tag, title,
                                                  bounty_list, rot))
            else:
                lines.extend(_oracle_summary(pool_key, tag, title,
                                             bounty_list, rot))
            continue
        # ① DE 直接下发 Jobs 的地区：地球 / 金星 / 火卫二
        s = de_by_synd.get(pool_key)
        jobs = (s or {}).get("jobs") or []
        if not jobs:
            continue
        if detailed:
            lines.extend(_region_block(pool_key, title, jobs,
                                       (s or {}).get("expiry", "")))
            if pool_key == "Ostrons":
                # 小帐篷 A/B/C：DE 不下发归属（全库无 camp 字段），由
                # 世界种子 + JobManifest 确定性推算，见 core/tents.py。
                lines.extend(_tent_lines(syndicates))
        else:
            lines.extend(_region_summary(pool_key, title, jobs,
                                         (s or {}).get("expiry", "")))

    if not lines:
        have = [SYNDICATE_BOUNTY.get(x, x) for x in sorted(boards)]
        return ("赏金任务", [f"未找到对应地区赏金。当前有赏金的板：{'、'.join(have) or '无'}",
                            "（扎里曼 / 实验室 / 1999 赏金为轮换开放，DE 不实时下发）"])
    if detailed:
        lines.append("※ ▣ 部件/蓝图　★ MOD　列每档全部奖励；轮次随刷新而变")
    else:
        lines.append("※ ▣ 部件/蓝图　★ MOD　轮换行只列高价值奖励")
        lines.append("※ 发「赏金 地球」「赏金 扎里曼」等看该地区各档完整奖励")
    return ("赏金任务", lines)


# 结合仪式目标的官方简中名（静态表，**39/39 全覆盖**）。
#
# 译名是 **DE 游戏内官方文本**（Simaris 商店 / 图鉴里显示的简中名词），
# 本表为人工转录的事实清单：远古干扰者=Ancient Disruptor、行刑者=Hellion、
# 恶徒=Trooper、禁卫军=Guardsman、痈裂者=Boiler…（英文侧来自 DE 公开导出
# 与结合仪式目标池；DE 导出里没有成套的单位名表，故手工整理）。
# 目标清单本身是静态的（39 种），不会过期。
_SYNTH_ZH = {
    "Ancient Disruptor": "远古干扰者",
    "Ancient Healer": "远古治愈者",
    "Boiler": "痈裂者",
    "Brood Mother": "病变虫母",
    "Charger": "疾冲者",
    "Crawler": "爬行者",
    "Leaper": "奔跳者",
    "Runner": "狂奔者",
    "Swarm-Mutalist MOA": "异融胞群恐鸟",
    "Anti MOA": "逆进恐鸟",
    "Crewman": "船员",
    "Elite Crewman": "精英船员",
    "Nullifier Crewman": "虚能船员",
    "MOA": "恐鸟",
    "Fusion MOA": "熔岩恐鸟",
    "Arid Eviscerator": "沙漠开膛者",
    "Ballista": "弩炮",
    "Bombard": "轰击者",
    "Butcher": "屠夫",
    "Commander": "指挥官",
    "Drahk Master": "爪喀驯兽师",
    "Eviscerator": "开膛者",
    "Guardsman": "禁卫军",
    "Heavy Gunner": "重型机枪手",
    "Hellion": "行刑者",
    "Lancer": "枪兵",
    "Napalm": "火焰轰击者",
    "Scorch": "怒焚者",
    "Scorpion": "天蝎",
    "Seeker / Frontier Seeker": "追踪者/前线追踪者",
    "Shield Lancer": "盾枪兵",
    "Trooper": "骑兵",
    "Corrupted Ancient": "远古堕落者",  # INTL
    "Corrupted Bombard": "堕落轰击者",  # INTL
    "Corrupted Butcher": "堕落屠夫",  # INTL
    "Corrupted Crewman": "堕落船员",  # INTL
    "Corrupted Heavy Gunner": "堕落重型机枪手",  # INTL
    "Corrupted Lancer": "堕落枪兵",  # INTL
    "Corrupted Nullifier": "堕落虚能者",  # INTL
}


def fmt_synth_targets(targets: Iterable[dict]) -> tuple[str, list[str]]:
    """结合仪式目标。

    Args:
        targets: ``_parse_synth`` 的产物。
    """
    lines = []
    for t in targets or []:
        if not t.get("active"):
            continue
        place = t.get("node") or t.get("mission", "")
        mtype = mission_cn(t.get("type") or t.get("mission", ""))
        fac = t.get("faction", "")
        name = (t.get("name") or "?").strip()
        zh = _SYNTH_ZH.get(name)
        if not zh:
            # 「XXX [Research]」是 Simaris 结合扫描器里的**研究变体**标记，
            # 静态库里存的是基底名（Anti MOA / Lancer / Crewman …），
            # 所以剥掉后缀再查一次、并把标记译作「研究版」。
            # 不这样处理时这 7 条会整条回落成英文（用户 2026-09-17 反馈
            # 「汉化缺失」的根因）。
            _m = re.match(r"^(?P<base>.+?)\s*\[Research\]$", name)
            if _m:
                _base_zh = _SYNTH_ZH.get(_m.group("base").strip())
                if _base_zh:
                    zh = f"{_base_zh}·研究版"
        # 中文优先，英文名留着便于对照（结合扫描器里看到的标记还是英文）
        shown = f"{zh}（{name}）" if zh else name
        lines.append(f"· {shown}　{place}·{mtype}（{fac}）")
    return ("结合仪式目标（39 种，静态库）", lines or ["结合目标表为空"])


def fmt_construction(data: Optional[dict]) -> tuple[str, list[str]]:
    """舰队建造进度 + 当前态势。

    只说百分比没有意义 —— 需要交代「这意味着什么、到 100% 会发生什么、现在有没有在打」。

    Args:
        data: ``_parse_construction`` 的产物。
    """
    if not data:
        return ("舰队建造进度", ["数据暂不可用"])
    projects = data.get("projects") or []
    assaults = data.get("assaults") or []
    lines: list[str] = []

    if assaults:
        for a in assaults:
            lines.append(f"⚠ 袭击进行中：{a['name']} → {a['victim'] or '一座中继站'}")
            extra = []
            if a.get("health") is not None:
                extra.append(f"舰体完整度 {a['health'] * 100:.0f}%")
            if a.get("expiry"):
                extra.append(f"剩余 {countdown(a['expiry'])}")
            if extra:
                lines.append("　　" + "　".join(extra))
    else:
        lines.append("◆ 当前无进行中的袭击事件")

    for p in projects:
        fac = faction_name({"Grineer": "FC_GRINEER", "Corpus": "FC_CORPUS"}
                           .get(p["faction"], p["faction"]))
        lines.append(f"◆ {p['name']}（{p['en']}）　建造进度 {p['pct']:.0f}%")
        lines.append(f"　　{fac} 阵营项目；满 100% 后开启 3 天的"
                     f"「{p['event']}」战术警报，该阵营将攻击一座中继站")

    if not projects and not assaults:
        lines.append("两个阵营的建造进度均为 0（刚结束一轮事件）")

    lines.append("※ 建造进度为全服累计：入侵任务中支持 Grineer 计入巴罗尔巨人战舰、"
                 "支持 Corpus 计入利刃豺狼舰队")
    lines.append("※ 警报期间把舰体完整度打到 0% 即结束，建造进度随之归零重新累积")
    lines.append("※ 数据源：DE worldState 的 ProjectPct 与 Goals")
    return ("舰队建造进度", lines)


# 言录使周常池的官方简中名（bin 2，13 件；与参考卡一致：
# 主要武器赋能槽连接器 / Forma 蓝图 / 赤毒 / 手枪裂罅 MOD…）
_ACRITHIS_ZH = {
    "Kuva": "赤毒",
    "FormaBlueprint": "Forma 蓝图",
    "RawMeleeRandomMod": "近战裂罅 MOD",
    "RawPistolRandomMod": "手枪裂罅 MOD",
    "RawRifleRandomMod": "步枪裂罅 MOD",
    "RawShotgunRandomMod": "霰弹枪裂罅 MOD",
    "RawSentinelWeaponRandomMod": "守护武器裂罅 MOD",
    "WeaponPrimaryArcaneUnlocker": "主要武器赋能槽连接器",
    "WeaponSecondaryArcaneUnlocker": "次要武器赋能槽连接器",
    "UtilityUnlocker": "战甲特殊功能槽连接器",
    "WeaponUtilityUnlocker": "武器特殊功能槽连接器",
    "OrokinCatalystBlueprint": "奥罗金催化剂蓝图",
    "OrokinReactorBlueprint": "奥罗金反应堆蓝图",
}


def _to_bj(iso: str) -> str:
    """UTC ISO 串 → 北京时间「09-27 08:00」。

    ★ 轮换**判定**一律用 UTC（DE 的服务器时间），但**展示**给国内玩家要换算成
    北京时间并标注 —— 否则「周日 00:00」会被当成北京时间，实际差 8 小时。
    """
    from datetime import datetime, timedelta, timezone
    try:
        dt = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone(timedelta(hours=8))).strftime("%m-%d %H:%M")


def fmt_acrichis_week(data: dict) -> tuple[str, list[str]]:
    """言录使**本周货单**（社区维护快照，含价格）。

    重置规则见 ``core/data/rotations.json`` 的 ``acrichis.reset_*``：
    wiki《Acrithis》**实时**页面写的是「Resets every **Monday** 0:00 UTC」，
    与其它周常同点（Update 32.3 起统一到周一）。判定用 UTC，卡面换算北京时间。
    """
    items = data.get("items") or []
    lines = []
    for it in items:
        nm = it.get("name") or "?"
        qty = f" ×{it['qty']}" if int(it.get("qty", 1) or 1) != 1 else ""
        # 结算货币是 Pathos Clamp，官方简中为**「苦栓」**
        # （/Lotus/Language/Duviri/DuviriDragonDropItemName），
        # 2026-09-17 用户指正；此前误写成「精华」。
        lines.append(f"· {nm}{qty}　{it.get('price', '?')} 苦栓")
    nxt = data.get("next_reset") or data.get("expiry")
    if nxt:
        tail = f"※ 距下次刷新 {countdown(nxt)}"
        bj = _to_bj(nxt)
        if bj:
            tail += f"（{bj} 北京时间）"
        lines.append(tail)
    lines.append("※ 每周一 00:00 UTC 轮换（与其它周常同点）；"
                 "本周货单为社区维护快照，DE 不下发")
    return ("言录使（Acrithis）本周货单", lines)


def fmt_acrichis(data: Optional[dict], stale: bool = False) -> tuple[str, list[str]]:
    """言录使（Acrithis）商品**池**。

    ⚠️ DE 不下发每周实际卖哪 5 件（``AcrithisVendorManifest`` 只有池子 + 权重，
    ``numRandomItemPrices`` 说明价格也是每周期随机 roll）。所以这里给的是
    「周常槽会出什么、权重多少」—— 参考机器人那张「本周 5 件」是它自己维护的
    快照，我们不做没有来源的数据。

    ``stale=True`` 表示本周货单快照已过期（此时展示的是候选池），
    必须**明确说出来**，不能让人误以为这就是本周实际在卖的 5 件。
    """
    bins = (data or {}).get("bins") or {}
    weekly = bins.get("2") or []
    if not weekly:
        lines = ["暂无商品池数据（ExportVendors 取不到）"]
        # ★ 过期提示不能因为「池子也没数据」就被吞掉 —— 那正是最容易让人
        #   以为「查到了」的场景
        if stale:
            lines.append("※ 本周 5 件货单**已过期未更新**，请以游戏内 Acrithis 为准")
        return ("言录使（Acrithis）" + ("（货单待更新）" if stale else ""), lines)
    lines = ["◆ 周常池（每个槽位每周期出 1 件）"]
    for it in weekly:
        nm = _ACRITHIS_ZH.get(it.get("en") or "", it.get("name") or it.get("en") or "?")
        qty = f" ×{it['qty']}" if int(it.get("qty", 1) or 1) != 1 else ""
        lines.append(f"· {nm}{qty}　权重 {it.get('pct', 0)}%")
    n_daily = sum(len(bins.get(b) or []) for b in ("0", "1", "3"))
    lines.append(f"◆ 另有每日槽 {n_daily} 件（船装装饰 / 拍照场景 / 小队增益，每日轮换）")
    if stale:
        lines.append("※ 本周 5 件货单**已过期未更新**，上面只是候选池 —— "
                     "本周实际在卖什么请以游戏内 Acrithis 为准")
    else:
        lines.append("※ 价格每周期随机 roll，DE 不下发本周实际货单；"
                     "上表是池子与权重，本周实际 5 件以游戏内 Acrithis 为准")
    return ("言录使（Acrithis）商品池" + ("（货单待更新）" if stale else ""), lines)


def fmt_incursions(data: Optional[dict]) -> tuple[str, list[str]]:
    """钢铁之路侵袭（每日 6 个钢路节点，重置后换一批）。

    节点等级 = 原节点等级 + 100（钢路规则）；任务类型用节点的官方简中
    ``nodes_zh[key]["type"]``（ExportRegions 直出）。行按「类型/派系/等级/地点」
    四格用全角空格分开，渲染层开列对齐（_table_mode）。
    """
    nodes = (data or {}).get("nodes") or []
    if not nodes:
        return ("钢铁之路侵袭", ["暂无侵袭排期（browse.wf 排期表取不到）"])
    nz = _de_zh("nodes_zh.json")
    sol = _de_zh("solNodes.json")
    lines = []
    for key in nodes:
        info = nz.get(key) or {}
        s = sol.get(key) or {}
        mtype = info.get("type") or mission_cn(s.get("type") or "")
        fac = _enemy_display(s.get("enemy") or "") or "—"
        lv = _steel_level(str(info.get("level") or ""))
        place = f"{info.get('system', '?')}-{info.get('name', key)}"
        lines.append(f"· {mtype}　{fac}　{lv}　{place}")
    lines.append(f"※ 每日重置（剩余 {countdown(data.get('expiry') or '')}）；"
                 f"钢路节点等级 = 原节点 +100，完成任一侵袭可得钢铁精华")
    return ("钢铁之路侵袭", lines)


def _steel_level(level: str) -> str:
    """「5-7」→「105-107」；单个数字也兼容。"""
    parts = level.replace("级", "").split("-")
    try:
        nums = [int(p) for p in parts if p.strip()]
    except ValueError:
        return level
    if not nums:
        return level
    return "-".join(str(n + 100) for n in nums) + "级"


def _enemy_display(enemy: str) -> str:
    """solNodes 的 enemy 字段 -> 展示名（与裂隙卡同一套规则）。"""
    e = (enemy or "").strip()
    if not e or e in ("Crossfire", "Tenno", "Duviri"):
        return ""
    return {"The Murmur": "低语者", "Orokin": "奥罗金"}.get(e, e)


# 沉沦之地（炼狱塔）任务类型的中文叫法。
# 来源：参考卡（沃沃「沉沦之地 炼狱塔」）逐层对出来的 DT_* 代码 → 中文名；
# DE 词表里这些模式名**不存在**（炼狱塔的层用的是独立模式名，不是普通任务类型），
# 没把握的代码回落到 mission_cn(英文)。
_DESCENT_ZH = {
    "DT_INFESTED_SALVAGE": "净化",
    "DT_CAPTURE": "传承种捕获",
    "DT_PRESURE_GAUGE": "压力锅",
    "DT_SABOTAGE_HIVE": "清巢",
    "DT_LOOT_CREATURES": "贪困断肢劫掠",
    "DT_ALCHEMY": "元素转换",
    "DT_PROTOFRAME": "战甲祈运",
    "DT_SHRINE_DEFENSE": "祈运坛防御",
    "DT_MOVING_INTERCEPTION": "移动拦截",
    "DT_TIME_TRIAL": "时间试炼",
    "DT_DEFENSE_PROTECT": "保护人物",
    "DT_COLLECTION": "收集",
    "DT_BREAK_TARGETS": "摧毁全息球",
    "DT_DEFENSE": "防御",
    "DT_EXTERMINATE": "歼灭",
    "DT_LOOT": "掠夺",
    "DT_RACE": "时间试炼",
}


# 沉沦之地（炼狱塔）「目标」列：DE 的 Challenge 代码 -> 中文。
# 这层的目标其实就是每层的复杂化（Penance），**DE 只给内部代码、不给显示文案**，
# 且每周换一批，所以这张表永远需要随周补。译名分两级来源：
#   A. 参考卡（沃沃「沉沦之地 炼狱塔[Demo]」）逐层对照 —— 实机可信
#   B. 依据英文 wiki「The Descendia → Penance」的官方英文名与效果说明译出
#      （warframe.wiki.gg / .com，2026-09-17 核对；Eximus Cabal 系沿用参考卡的
#       「XX卓越者军团」命名风格）
# 两级都查不到的代码，回落 _pretty_code() 拆成可读英文（不再是驼峰）。
_DESCENT_GOAL_ZH = {
    "VeryToxic": "毒蛭吸血卓越者军团",
    "GrenadesOnly": "易受元素瓶攻击的敌人",
    "SlipAndSlide": "无摩擦",
    "RangedArcadiaOnly": "泡泡枪",
    "BasicLootCreatures": "阻止贪困断肢",
    "Sunlight": "太阳神之怒",
    "HeadShotsOnly": "只有弱点才会受到伤害",
    "GlassMaker": "玻璃匠中枢人",
    "Manics": "躁狂症",
    "BasicRace": "穿过闸门赛跑",
    "FreezeInShoot": "冰封之光卓越者军团",
    "NC_SecuritySpin": "激光炼狱",
    "BasicBreakTargets": "摧毁全息球",
    "SpicyKnife": "折弹",
    "VoidAberration": "吸血异影",
    "JumpSmash": "跌头者",
    "HeavyWeaponsOnly": "易受曲翼枪械攻击的敌人",
    "BasicLoot": "搜寻资源",
    "Devil": "罗瑟的遗忘",
    "Harrow": "里昂的圣所",
    # —— 2026-09-17 补（该周 21 层里新出现、原表未覆盖的）——
    # ArchonBoreal 取**官方简中**：/Lotus/Language/Narmer/ArchonBoreal = 执刑官诡文枭主
    "ArchonBoreal": "执刑官诡文枭主",
    # 以下为依据 wiki 英文名 + 效果说明的译名
    "HorseCombatOnly": "只能骑乘战斗",        # Battle Kaithes：禁用下马/多数技能/主·近战
    "FireAndIce": "冰火卓越者军团",            # Fire & Ice（Arctic/Arson/Blitz）
    "ShockingLeech": "电击吸血卓越者军团",      # Shocking Leech（Leech/Shock/Venomous）
    "JadeGuardian": "翡翠守护者卓越者军团",      # Jade Guardian（Guardian/Jade Light/Shock）
    "GiantRealm": "巨大化",                   # Gigantism：敌人更大更慢
    "Sentients": "陶之复仇",                  # Tau's Revenge：全部敌人为 Sentient
    "HordeWeakpoints": "弱点群体",            # Weakpoint Horde：弱点外抗性 + 近战猛扑
    "UnseenFoes": "隐藏威胁",                 # Hidden Threats：首次攻击前隐形
    "FieryTrail": "火焰轨迹",                 # Fire Trails：敌人身后留下火焰
    "HardShell": "硬壳",
    "BasicMimics": "拟态",
    "PowerHouse": "强力增幅",
}


def _pretty_code(code: str) -> str:
    """把 DE 的驼峰内部代码拆成可读英文（``FireAndIce`` → ``Fire And Ice``）。

    复杂化每周换新，表不可能覆盖全部；回落时至少别把 ``HorseCombatOnly``
    这种一坨驼峰直接甩给用户。
    """
    if not code:
        return ""
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", code)


def fmt_descendia(data: Optional[dict]) -> tuple[str, list[str]]:
    """沉沦之地（炼狱塔）：每周 21 层，每层一个任务类型 + 一个目标（复杂化）。

    * **任务类型**：DE 的 ``Type`` 代码 → 参考卡整理的中文（``_DESCENT_ZH``）。
    * **目标**：DE 的 ``Challenge`` 代码 → 中文（``_DESCENT_GOAL_ZH``）；
      表里没有的代码回落 ``_pretty_code()`` 的可读英文（每周可能换新复杂化）。
      行用全角空格分格，渲染层按列对齐并给类型上色。
    """
    chs = (data or {}).get("challenges") or []
    if not chs:
        return ("沉沦之地 · 炼狱塔", ["暂无数据（DE Descents 取不到）"])
    lines = [f"◆ 本周　剩余 {countdown(data.get('expiry') or '')}"]
    for c in chs:
        code = c.get("Type") or ""
        label = _DESCENT_ZH.get(code) or mission_cn(c.get("type") or code)
        _goal_code = c.get("code") or ""
        goal = _DESCENT_GOAL_ZH.get(_goal_code) or _pretty_code(_goal_code)
        idx = c.get("index")
        lines.append(f"· 炼狱 [{idx}]　{label}　{goal}".rstrip())
    lines.append("※ 每周轮换 21 层；「目标」是参考卡整理的对照表，"
                 "进本前以游戏内显示为准")
    return ("沉沦之地 · 炼狱塔", lines)


def fmt_archimedea(data: Optional[dict], title: str) -> tuple[str, list[str]]:
    """深层科研 / 时光科研。

    结构（来自 DE 的 ``Conquests``）：3 个任务 × 「普通 / 硬化」两档，
    每档各有一个**偏差**（deviation）和若干**风险**（risks），
    另有 4 个**可选个人减益**（Variables）。
    """
    if not data:
        return (title, ["该数据源暂未开放（endpoint 未接入或返回为空）"])
    lines = []
    if data.get("expiry"):
        lines.append(f"◆ 本周　剩余 {countdown(data['expiry'])}")
    missions = data.get("missions") or []
    if not missions:
        return (title, lines + ["本周暂无数据"])
    for i, m in enumerate(missions, 1):
        fac = f"　{m['faction']}" if m.get("faction") else ""
        lines.append(f"{i}. {mission_cn(m.get('missionType', ''))}{fac}")
        diffs = m.get("difficulties") or []
        if not diffs:
            # 旧 Descents 结构：只有一串风险名
            for r in m.get("risks") or []:
                lines.append(f"　　[风险] {r}")
            continue
        shown_hard = any(d.get("tag") == "硬化" for d in diffs)
        for d in diffs:
            # 普通/硬化的偏差与普通风险相同，只列硬化的额外风险即可，避免整段重复
            if d.get("tag") == "硬化" and shown_hard:
                continue
            if d.get("deviation"):
                lines.append(f"　　[偏差] {d['deviation']}")
            for r in d.get("risks") or []:
                lines.append(f"　　[风险] {r}")
        hard = next((d for d in diffs if d.get("tag") == "硬化"), None)
        if hard and shown_hard:
            base = set(diffs[0].get("risks") or []) if diffs else set()
            extra = [r for r in (hard.get("risks") or []) if r not in base]
            for r in extra:
                lines.append(f"　　[硬化·额外风险] {r}")
    if data.get("variables"):
        # 可选个人减益**带说明**：玩家要据此决定这周选哪几个减益换奖励
        # （用户反馈「可选减益没有详细说明」—— 之前只列了名字）。
        lines.append("◆ 可选个人减益")
        for v in data["variables"]:
            nm = (v.get("name") or "").strip()
            if not nm:
                continue
            desc = (v.get("description") or "").strip().replace("\n", " ")
            desc = re.sub(r"<[^<>]{0,40}>", "", desc).strip()
            lines.append(f"　　{nm}：{desc[:64]}" if desc else f"　　{nm}")
    if data.get("risks"):
        lines.append("◆ 风险说明")
        for r in data["risks"][:12]:
            desc = (r.get("description") or "").strip().replace("\n", " ")
            desc = re.sub(r"<[^<>]{0,40}>", "", desc)
            lines.append(f"　　{r.get('name')}：{desc[:60]}")
    lines.append("※ 任务与偏差来自 DE 官方下发；实际进入时以游戏内为准")
    return (title, lines)


SEASON_CN = {"CST_SPRING": "春", "CST_SUMMER": "夏", "CST_FALL": "秋", "CST_WINTER": "冬"}


# 1999 日历奖励名：DE 语言表未收录的英文条目
_CAL_REWARD_CN = {
    "Arcane Enhancements: Double Pack": "赋能强化（双份）",
    "Arcane Enhancements": "赋能强化",
    # 官方：weaponprimaryarcaneunlocker -> 主要武器赋能槽连接器
    # （不要用社区/国服叫法，见 tests/test_translation_locale.py）
    "Weapon Primary Arcane Unlocker": "主要武器赋能槽连接器",
    "Weapon Secondary Arcane Unlocker": "次要武器赋能槽连接器",
    # 官方：Exilus -> 特殊功能槽
    "Exilus Weapon Adapter Blueprint": "武器特殊功能槽连接器蓝图",
    "Exilus Weapon Adapter": "武器特殊功能槽连接器",
    "Exilus Adapter Blueprint": "战甲特殊功能槽连接器蓝图",
    "Exilus Adapter": "战甲特殊功能槽连接器",
    # 官方：archoncrystal* -> 执刑官源力石
    "Emerald Archon Shard": "翡翠执刑官源力石",
    "Crimson Archon Shard": "深红执刑官源力石",
    "Azure Archon Shard": "蔚蓝执刑官源力石",
    "Amber Archon Shard": "琥珀执刑官源力石",
    "Violet Archon Shard": "紫罗兰执刑官源力石",
    "Topaz Archon Shard": "黄玉执刑官源力石",
    # 官方：orokincatalyst / orokinreactor -> 奥罗金催化剂 / 奥罗金反应堆
    "Orokin Catalyst Blueprint": "奥罗金催化剂蓝图",
    "Orokin Reactor Blueprint": "奥罗金反应堆蓝图",
    "Forma Blueprint": "Forma 蓝图",
    "Kuva": "赤毒",
    "Endo": "内融核心",
    "Riven Mod": "裂罅 Mod",      # 官方写法是「Mod」而非「MOD」（见 name_zh.json）
    "Riven Transmuter": "裂罅转换器",
    # 官方导出未收录此项，沿用社区通用名并与 sortie_rewards.json 保持一致
    "Legendary Core": "传说核心",
}


def _cal_name(name: str) -> str:
    """1999 日历条目名兜底汉化（官方词表未覆盖时用）。"""
    if not name:
        return name
    s = name.strip()
    for en, cn in _CAL_REWARD_CN.items():
        s = s.replace(en, cn)
    # 「2000 x Kuva」/「6000 x Kuva」/「6,000 Endo」统一成「赤毒 ×N」
    m = re.match(r"^([\d,]+)\s*x?\s*(.+)$", s)
    if m and m.group(2).strip():
        s = f"{m.group(2).strip()} ×{m.group(1)}"
    return s


def _cal_objective(ev: dict) -> str:
    """挑战目标文案：把 |COUNT| 换成实际数量并剥掉富文本标记。"""
    desc = ev.get("desc") or ""
    if not desc:
        return ""
    desc = re.sub(r"<[^<>]{0,40}>", "", desc).strip()
    cnt = ev.get("count")
    if "|COUNT|" in desc:
        desc = desc.replace("|COUNT|", f"{cnt:,}" if isinstance(cnt, int) else "N")
    return desc


def fmt_calendar(data: Optional[dict], mode: str = "") -> tuple[str, list[str]]:
    """1999 日历：季节窗口 + 各日期事件表。

    mode: "" 概览（前 10 天）｜"奖励" 只看奖励 ｜"清单" 全量 ｜"覆写" 升级项
    事件名的中文来自 DE 官方导出（挑战走 ExportChallenges + 官方简中词表，
    奖励走 /Lotus/Language/1999/<包名>Name），因此「任务名 + 任务目标」都是官方文案。
    """
    if not data or not data.get("days"):
        return ("1999 日历", ["日历数据暂不可用"])
    season = SEASON_CN.get(data.get("season", ""), str(data.get("season", "")))
    year = data.get("yearIteration")
    lines = [f"第 {year} 年 · {season}季　"
             + (f"季末 {countdown(data['expiry'])}" if data.get("expiry") else "")]
    kind_cn = {"REWARD": "🎁", "CHALLENGE": "⚔", "UPGRADE": "⬆"}
    want = {"奖励": "REWARD", "覆写": "UPGRADE"}.get(mode)
    full = mode in ("清单", "奖励", "覆写")

    shown = 0
    for day in data["days"]:
        events = day.get("events") or []
        if want:
            events = [e for e in events if e.get("type") == want]
        if not events:
            continue
        if not full and shown >= 10:
            break
        date = day.get("date", "?")
        head = (f"· 1999-{date[5:]}（第{day.get('day', '?')}天）"
                if not full else
                f"· 1999-{date[5:]}（第{day.get('day', '?')}天）")
        if full:
            lines.append(head)
            for e in events:
                lines.append(f"　　{kind_cn.get(e.get('type'), '·')}"
                             f"{_cal_name(e.get('name', '?'))}")
                obj = _cal_objective(e)
                if obj:
                    lines.append(f"　　　{obj}")
        else:
            segs = []
            for e in events[:3]:
                seg = f"{kind_cn.get(e.get('type'), '·')}{_cal_name(e.get('name', '?'))}"
                obj = _cal_objective(e)
                if obj:
                    seg += f"（{obj}）"
                segs.append(seg)
            lines.append(head + "　" + "、".join(segs))
        shown += 1
    if shown == 0:
        lines.append({"奖励": "本季暂无奖励事件",
                      "覆写": "本季暂无升级/覆写事件"}.get(mode, "本季暂无排定事件"))
    titles = {"奖励": "1999 日历 · 奖励", "清单": "1999 日历 · 全量清单",
              "覆写": "1999 日历 · 升级/覆写"}
    if mode == "覆写":
        lines.append("※ DE 未单独下发「覆写」字段，此处展示日历中的升级(UPGRADE)条目")
    return (titles.get(mode, "1999 日历"), lines)


def fmt_platform_footer(platform: str, extra: str = "") -> str:
    txt = f"平台：{PLATFORM_DISPLAY.get(platform, platform.upper())}"
    if extra:
        txt += f" · {extra}"
    return txt


# ---------------------------------------------------------------------------
# 市场输出
# ---------------------------------------------------------------------------


# 在线状态排序权重：游戏内在线 > 网页在线 > 离线
_ONLINE_RANK = {"ingame": 0, "online": 1, "offline": 2}
_REP_RANK = {"ingame": 0, "online": 0, "offline": 1}


def _rep_txt(rep) -> str:
    """WM 信誉值展示：``信46``。"""
    try:
        return f"信{int(rep or 0)}"
    except (TypeError, ValueError):
        return ""


def wm_best_price(orders: list[dict], kind: str) -> Optional[int]:
    """取与单件查询**展示口径完全一致**的最优价。

    排序规则同 ``fmt_wm_orders``：在线档位优先（ingame > online > offline），
    同档在售取最低 / 收购取最高——即单独搜部件时列表第一行的那个价格
    （2026-09-14 用户反馈套装部件参考价与单件查询对不上：之前取全量绝对
    最值，2p/10p 的离线陈年老单把数字拉低）。
    """
    pool = [o for o in orders
            if o.get("order_type") == kind and o.get("visible", True)]
    if not pool:
        return None
    if kind == "buy":
        pool.sort(key=lambda o: (_ONLINE_RANK.get(
            (o.get("user") or {}).get("status", ""), 3), -o["platinum"]))
    else:
        pool.sort(key=lambda o: (_ONLINE_RANK.get(
            (o.get("user") or {}).get("status", ""), 3), o["platinum"]))
    return pool[0]["platinum"]


def fmt_wm_set_parts(rows: list[dict]) -> list[str]:
    """套装查询附带的「部件参考价」行（2026-09-14 用户要求：查套装默认输出
    套装单的同时把分件价一起给；单独查「wm 席瓦蓝图」再走单件查询）。

    口径：每件部件取 ``wm_best_price``（在线档位优先、同档价格最优），
    **与单独搜该部件时列表第一行的价格完全一致**；不标注在线状态
    （同日用户反馈「不用特地标记在不在线」）。
    """
    if not rows:
        return []
    lines = ["◆ 部件参考价"]
    for r in rows:
        name = r.get("name") or "?"
        if r.get("sell") is None and r.get("buy") is None:
            lines.append(f"· {name}　暂无挂单")
            continue
        seg = []
        if r.get("sell") is not None:
            seg.append(f"在售 {r['sell']}p")
        if r.get("buy") is not None:
            seg.append(f"收购 {r['buy']}p")
        lines.append(f"· {name}　" + " ｜ ".join(seg))
    lines.append("※ 单查某个部件：wm 部件名（如 wm 席瓦蓝图）")
    return lines


def fmt_wm_orders(item_name: str, orders: list[dict], *, buy: bool = False,
                  page: int = 1, page_size: int = 10,
                  quantity: Optional[int] = None, rank: Optional[int] = None) -> tuple[str, list[str], Optional[dict]]:
    """整理 WM 订单：在线优先、价格其次，并标注卖家信誉与在线状态。

    排序口径（用户反馈「做了价格排序但没做在线排序」）：
      先按「在线状态」分档（游戏内在线 > 网页在线 > 离线），档内再按价格升序
      （收购单为降序）。这样最上面永远是能立刻交易的人。

    Returns:
        ``(标题, 行, 最低价订单, 订单池)``——第三项用于 ``-r`` 密语生成；
        第四项是排序/filter 后的完整订单池（在线优先同展示顺序），2026-09-14
        用户要求 ``-r`` 给前 5 个卖家各生成一条密语，不只第一个。
    """
    kind = "buy" if buy else "sell"
    pool = [o for o in orders if o.get("order_type") == kind and o.get("visible", True)]
    if rank is not None and rank >= 0:
        pool = [o for o in pool if o.get("mod_rank") == rank]
    if quantity:
        pool = [o for o in pool if int(o.get("quantity") or 0) >= quantity]
    pool.sort(key=lambda o: (_ONLINE_RANK.get(o.get("user", {}).get("status", ""), 3),
                             -o["platinum"] if buy else o["platinum"]))
    status_cn = {"ingame": "🟢在线", "online": "🔵网页在线", "offline": "⚫离线"}
    total = len(pool)
    pages = max(1, (total + page_size - 1) // page_size)
    page = max(1, min(page, pages))
    chunk = pool[(page - 1) * page_size: page * page_size]
    lines = []
    for i, o in enumerate(chunk, (page - 1) * page_size + 1):
        user = o.get("user", {}) or {}
        seg = [f"{o['platinum']}p ×{o.get('quantity', 1)}"]
        if o.get("mod_rank") is not None:
            seg.append(f"{o['mod_rank']}级")
        seg.append(status_cn.get(user.get("status", ""), ""))
        seg.append(str(user.get("ingame_name", "?")))
        rep = _rep_txt(user.get("reputation"))
        if rep:
            seg.append(rep)
        lines.append(f"{i}. " + " ".join(x for x in seg if x))
    title = f"{item_name} {'收购' if buy else '在售'}单（第{page}/{pages}页，共{total}条）"
    lines.append("※ 排序：在线优先（🟢>🔵>⚫），同档按价格"
                 + ("降序" if buy else "升序"))
    best = pool[0] if pool else None
    return (title, lines or ["没有符合条件的订单"], best, pool)


WM_WHISPER_BUY = '/w {name} Hi! I want to buy: "{item}" for {plat} platinum. (warframe.market)'
WM_WHISPER_SELL = '/w {name} Hi! I want to sell: "{item}" for {plat} platinum. (warframe.market)'


def build_whisper(order: dict, item_name: str, *, sell: bool = False) -> str:
    """生成快捷交易密语。

    尾部括号标注**数据来源站**：这些价格/在线状态来自 warframe.market，
    不是 warframe.com 官网（旧模板错写成 warframe.com，会被卖家当成骗子）。
    """
    tmpl = WM_WHISPER_SELL if sell else WM_WHISPER_BUY
    return tmpl.format(name=order.get("user", {}).get("ingame_name", "?"),
                       item=item_name, plat=order.get("platinum", "?"))


def _is_melee_weapon(riven_type: str = "", group: str = "") -> bool:
    """WM 的 rivenType/group 是否为近战（含 Zaw）。"""
    from . import riven_analysis as RA
    return RA.weapon_class(riven_type, group) == "melee"


def _riven_stat_cn(url_name: str, riven_type: str = "",
                   group: str = "") -> str:
    """拍卖词条 url_name -> 中文名（合并名/标准名都处理）。

    ⚠️ WM 把**射速**和**攻速**合成同一个 slug `fire_rate_/_attack_speed`
    （官网中文也写成「射速/攻击速度」），光看 slug 分不出近战还是枪械，
    必须用武器类别消歧：近战 → 攻速，其余 → 射速。
    2026-09-14 用户报障：「翁（Okina，匕首）紫卡里怎么会有射速」。
    """
    from .parser import RIVEN_STAT_ZH, RIVEN_URL_COMPAT
    to_canon = {v: k for k, v in RIVEN_URL_COMPAT.items()}
    canon = to_canon.get(url_name, url_name)
    if canon == "fire_rate" and _is_melee_weapon(riven_type, group):
        canon = "attack_speed"
    return RIVEN_STAT_ZH.get(canon, url_name.replace("_", " "))


def _riven_rolls(a: dict) -> int:
    """WM v1 拍卖的洗数字段是 re_rolls（不是 rerolls）——取错会导致全显示 0 洗。"""
    item = a.get("item", {}) or {}
    v = item.get("re_rolls")
    if v is None:
        v = item.get("rerolls")
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def fmt_wr_auctions(weapon: str, auctions: list[dict], page: int = 1,
                    page_size: int = 8, riven_type: str = "",
                    group: str = "", *, presorted: bool = False,
                    ) -> tuple[str, list[str], Optional[dict]]:
    """紫卡拍卖列表。

    展示与排序都按「先看能不能立刻交易、再看价格」：
      · 每行给出 卖家在线状态 / 信誉 / 紫卡等级 / 洗数
      · 词条用 ▲（正面，青绿）/ ▼（负面，红）区分，正面同一色、负面另一色
      · 排序：在线优先（游戏内 > 网页 > 离线），同档按买断价升序

    ``riven_type`` / ``group`` 用于词条消歧（近战的合并 slug 显示「攻速」）。
    ``presorted=True``：调用方已按**词条命中率**排好序（wr 的「无完全匹配，
    给最接近选项」分支），此时不再按「在线+价格」重排 —— 否则前排会变成
    便宜但词条不匹配的挂单（2026-09-24 用户报障「前排出现不匹配的项目」）。
    """
    status_cn = {"ingame": "🟢在线", "online": "🔵网页在线", "offline": "⚫离线"}

    def price_of(a: dict) -> float:
        return a.get("buyout_price") or a.get("starting_price") or 0

    def status_of(a: dict) -> str:
        return ((a.get("owner") or {}).get("status") or "offline")

    pool = list(auctions) if presorted else sorted(
        auctions, key=lambda a: (_ONLINE_RANK.get(status_of(a), 3),
                                 price_of(a)))
    rank_max = 8          # 紫卡满级 8 级
    total = len(pool)
    pages = max(1, (total + page_size - 1) // page_size)
    page = max(1, min(page, pages))
    chunk = pool[(page - 1) * page_size: page * page_size]
    lines = []
    for i, a in enumerate(chunk, (page - 1) * page_size + 1):
        item = a.get("item", {}) or {}
        owner = a.get("owner", {}) or {}
        attrs = []
        for at in item.get("attributes") or []:
            mark = "▲" if at.get("positive") else "▼"
            attrs.append(f"{mark}"
                         f"{_riven_stat_cn(at.get('url_name', ''), riven_type, group)}"
                         f"{abs(at.get('value', 0)):g}")
        rank = item.get("mod_rank")
        seg = [f"{int(price_of(a))}p",
               status_cn.get(owner.get("status"), "⚫离线"),
               str(owner.get("ingame_name", "?")),
               f"洗{_riven_rolls(a)}次"]
        if rank is not None:
            seg.append(f"{rank}/{rank_max}级")
        rep = _rep_txt(owner.get("reputation"))
        if rep:
            seg.append(rep)
        lines.append(f"{i}. " + " ".join(seg))
        if attrs:
            lines.append("　　" + "　".join(attrs[:6]))
    title = f"{weapon} 紫卡拍卖（第{page}/{pages}页，共{total}条）"
    lines.append("※ ▲正面词条（同色）· ▼负面词条（红色）；排序：在线优先，同档按价格升序")
    best = pool[0] if pool else None
    return (title, lines or ["没有符合条件的紫卡挂单"], best)


_DUCAT_TIER = {
    "金": {"name": "金垃圾（稀有部件 100 杜卡德）", "values": (100,)},
    "银": {"name": "银垃圾（非稀有部件 45-65 杜卡德）", "values": (45, 65)},
    "铜": {"name": "铜垃圾（常见部件 15-25 杜卡德）", "values": (15, 25)},
}


def fmt_ducat_junk(tier: str, rows: list[dict], page: int = 1,
                   pages: int = 1) -> tuple[str, list[str]]:
    """杜卡德垃圾榜：列出「杜卡德/白金」最高的部件。

    Args:
        tier: ``金`` / ``银`` / ``铜``。
        rows: ``[{"name", "ducats", "dpp", "dpp_wa", "plat", "volume"}]``。
        page: 当前页码。
        pages: 总页数。

    Returns:
        ``(标题, 行列表)``。
    """
    name = _DUCAT_TIER.get(tier, {}).get("name", f"{tier}垃圾")
    if not rows:
        return (name, ["当前取不到杜卡德榜单（WM tools/ducats 不可用）"])
    lines = [
        f"{i}. {r['name']}　{r['ducats']}杜 · {r['plat']:.0f}p"
        f" · {r['dpp']:.1f} 杜/p"
        + (f"（加权 {r['dpp_wa']:.1f}）" if r.get("dpp_wa") else "")
        for i, r in enumerate(rows, (page - 1) * len(rows) + 1 if pages else 1)
    ]
    lines.append("※ 杜/p = 每 1 白金能换到多少杜卡德，越高越值得买来换")
    lines.append("※ 数据源 warframe.market/tools/ducats（与官网杜卡德计算器同源）")
    return (name, lines)


# ---------------------------------------------------------------------------
# 九重天 / 活动 / 武形秘仪 / 阿耶兑换 / 氏族奖励 / 商城折扣
# ---------------------------------------------------------------------------

_RAILJACK_TYPE_CN = {
    "skirmish": "空战", "volatile": "易爆", "spy": "间谍", "survival": "生存",
    "extermination": "歼灭", "defense": "防御", "sabotage": "破坏",
    "assassinate": "刺杀", "orphix": "奥菲斯", "hijack": "劫持", "volatile": "易爆",
}


_SPACE_SUFFIX = re.compile(r"比邻星域$")


def fmt_void_storms(storms: Iterable[dict]) -> tuple[str, list[str]]:
    """九重天（航道星舰）虚空风暴。

    按纪元分组、每处一行并带「任务类型 + 剩余时间」，比原来一整列同构长短句
    更好扫读（旧版每行只有 ``节点｜类型｜T1｜剩0s``，12 行完全一样看不出重点）。
    """
    storms = list(storms or [])
    if not storms:
        return ("九重天虚空风暴", ["当前没有可用的虚空风暴"])
    groups: dict[str, list] = {}
    for s in storms:
        groups.setdefault(s.get("tier") or "?", []).append(s)
    lines = [f"共 {len(storms)} 处进行中　※ 九重天裂缝，可刷对应纪元遗物"]
    for tier in sorted(groups, key=lambda t: _TIER_ORDER.get(
            {"T1": "Lith", "T2": "Meso", "T3": "Neo", "T4": "Axi"}.get(t, t), 99)):
        items = groups[tier]
        cn = tier_cn({"T1": "Lith", "T2": "Meso", "T3": "Neo",
                      "T4": "Axi"}.get(tier, tier))
        lines.append(f"◆ {tier}（{cn}）　{len(items)} 处")
        for s in items:
            node = _SPACE_SUFFIX.sub("", s.get("nodeCn") or "?")
            mt = _RAILJACK_TYPE_CN.get((s.get("missionType") or "").lower(),
                                       s.get("missionType") or "?")
            left = s.get("timeLeft") or "?"
            lines.append(f"　　{node}｜{mt}｜剩{left}")
    return ("九重天虚空风暴", lines)


def fmt_events(events: Iterable[dict]) -> tuple[str, list[str]]:
    """限时活动（社区目标）。

    DE 的 Goal 带 ``GracePeriod``：活动本体到期后仍在宽限期内下发。这类条目
    会照常展示但标记「已结束」，避免用户以为漏了活动（如刚收官的三伏天）。
    """
    events = list(events or [])
    if not events:
        return ("活动", ["当前没有进行中的限时活动"])
    lines = []
    for e in events:
        head = f"◆ {e.get('name')}"
        if e.get("node"):
            head += f"｜{e['node']}"
        if e.get("ended"):
            head += "｜已结束（结算宽限期）"
        else:
            head += f"｜剩{e.get('timeLeft')}"
        lines.append(head)
        if e.get("count") is not None and e.get("goal"):
            try:
                pct = int(e["count"]) / max(1, int(e["goal"])) * 100
                lines.append(f"　　进度 {e['count']}/{e['goal']}（{pct:.0f}%）")
            except (TypeError, ValueError, ZeroDivisionError):
                lines.append(f"　　进度 {e.get('count')}/{e.get('goal')}")
        if e.get("desc") and _HAS_CJK.search(e["desc"]):
            # 官方简中缺失时 desc 会是英文长句（如 "Seal fractures across…"），
            # 中文卡片里塞英文只会更乱 —— 只在拿到中文说明时才展示。
            lines.append(f"　　{e['desc']}")
        if e.get("rewards"):
            lines.append("　　奖励：" + "、".join(e["rewards"][:8]))
    return ("进行中的活动", lines)


# 武形秘仪（Conclave）声望来源（wiki.warframe.com/w/Conclave）：
#   每周三项（赢得 6 场 / 完成 20 场 / 完成 10 项每日挑战）全部完成 -> 50,000 声望
#   + 10 个稀有资源 + 一个架式 MOD + 100,000 现金（Teshin 站内信）
#   每日挑战最多 8 项（每个模式 2 项），单项目 500 / 1,500 / 3,000 声望，
#   带「Focused」前缀的高级挑战为 6,000 声望
PVP_STANDING_NOTE = ("※ 声望：每周三项全完成得 50,000（另附站内信奖励）；"
                     "每日挑战 500/1,500/3,000 声望，高级(Focused) 6,000")


def fmt_conclave(challenges: Iterable[dict]) -> tuple[str, list[str]]:
    """武形秘仪（Conclave）每日/每周挑战 + 声望来源说明。"""
    challenges = list(challenges or [])
    if not challenges:
        return ("武形秘仪", ["当前没有可用的武形秘仪挑战"])
    lines = []
    cur = None
    for c in challenges:
        if c.get("cat") != cur:
            cur = c.get("cat")
            lines.append(f"【{cur}挑战】")
        val = f" ×{c['value']}" if c.get("value") else ""
        lines.append(f"· {c.get('mode')}｜{c.get('text')}{val}")
    lines.append(PVP_STANDING_NOTE)
    return ("武形秘仪挑战（Conclave）", lines)


def fmt_prime_vault(data: Optional[dict], page: int = 1,
                    page_size: int = 12) -> tuple[str, list[str]]:
    """御品阿耶精华 / Prime 重生（Varzia），支持翻页。

    Args:
        data: ``prime_vault`` 的产物。
        page: 页码，从 1 开始。
        page_size: 每页可兑换条目数（本期 + 常驻合并成一列翻页）。

    Returns:
        ``(标题含页码, 行列表)``。
    """
    if not data or not (data.get("items") or data.get("evergreen")):
        return ("御品阿耶精华", ["数据暂不可用"])
    lines = [f"⏳ 本期剩余 {data.get('timeLeft', '?')}"]
    if data.get("next"):
        lines.append(f"◆ 下一期：{data['next']}　"
                     f"（{countdown(data.get('next_expiry', ''))} 后开启）")
    elif data.get("next_unannounced"):
        # DE 给了下一轮的时间点、但没公布内容（FeaturedItem 为空串）
        lines.append(f"※ 下一期内容 DE 尚未公布"
                     f"（下一轮 {countdown(data.get('next_open', ''))} 后开启）")
    else:
        # 排期表里没有更多数据时明说，别留空标题让用户以为插件坏了
        lines.append("※ 下一期排期 DE 尚未下发（只公布到当前期）")

    def _line(tag: str, it: dict) -> str:
        cur = "御品阿耶" if it.get("prime_currency", True) else "阿耶精华"
        return f"· [{tag}] {it['name']}　{it.get('prime', 1)} {cur}"

    rows = ([_line("本期", it) for it in (data.get("items") or [])]
            + [_line("常驻", it) for it in (data.get("evergreen") or [])])
    if rows:
        total = len(rows)
        pages = max(1, (total + page_size - 1) // page_size)
        page = max(1, min(page, pages))
        lines.append(f"【可兑换】本期 {len(data.get('items') or [])} + "
                     f"常驻 {len(data.get('evergreen') or [])}，共 {total} 项")
        lines += rows[(page - 1) * page_size: page * page_size]
        if pages > 1:
            lines.append(f"※ 第{page}/{pages}页；加 -2 / -3 翻页")
    return ("御品阿耶精华 / Prime 重生", lines)


# 「出库」清单的分组顺序与标题（kind 值见 de_worldstate._vault_kind）
_VAULT_GROUPS: tuple[tuple[str, str], ...] = (
    ("frame", "Prime 战甲"),
    ("weapon", "Prime 武器"),
    ("sentinel", "Prime 守护与守护武器"),
)


def fmt_prime_vault_list(data: Optional[dict]) -> tuple[str, list[str]]:
    """出库清单：按「战甲 / 武器 / 守护」分组，**只列整套名**（不含部件）。

    与 :func:`fmt_prime_vault` 的分工（2026-09-17 用户要求拆开）：
      · ``阿耶`` = 御品阿耶兑换表（带价格，含装饰与组合包，可翻页）
      · ``出库`` = 本清单：只看**这一期能从宝库刷到哪些整套 Prime 道具**

    此前两者输出完全一样，用户反馈「出库指令和阿耶指令为什么是一样的」。
    """
    if not data:
        return ("Prime 出库清单", ["数据暂不可用"])
    items = list(data.get("items") or [])
    lines = [f"⏳ 本期剩余 {data.get('timeLeft', '?')}"]
    if data.get("next"):
        lines.append(f"◆ 下一期：{data['next']}　"
                     f"（{countdown(data.get('next_expiry', ''))} 后开启）")
    elif data.get("next_unannounced"):
        lines.append(f"※ 下一期内容 DE 尚未公布"
                     f"（下一轮 {countdown(data.get('next_open', ''))} 后开启）")

    known = {k for k, _ in _VAULT_GROUPS}
    for kind, label in _VAULT_GROUPS:
        group = [it for it in items if it.get("kind") == kind]
        if not group:
            continue
        lines.append(f"◆ {label}（{len(group)}）")
        lines += [f"· {it['name']}" for it in group]

    other = [it for it in items if it.get("kind") not in known]
    if other:
        names = "、".join(it["name"] for it in other[:3])
        more = f" 等 {len(other)} 件" if len(other) > 3 else ""
        lines.append(f"※ 另有装饰 / 组合包：{names}{more}"
                     f"（不能用遗物刷取；发「阿耶」看带价格的完整兑换表）")
    if not items:
        lines.append("本期没有出库的 Prime 道具")
    lines.append("※ 均为整套（不含部件）；部件与出处发「遗物 名称」查询")
    return ("Prime 出库清单（本期）", lines)


def fmt_clan_rewards(rewards: Iterable[dict]) -> tuple[str, list[str]]:
    """每周氏族组队奖励。"""
    rewards = list(rewards or [])
    if not rewards:
        return ("氏族组队奖励", ["本周暂无可用的氏族组队奖励"])
    lines = []
    for w in rewards:
        lines.append(f"◆ {w.get('region')}（第 {w.get('week')} 周）")
        for r in w.get("rewards") or []:
            cnt = f" ×{r['count']}" if r.get("count") else ""
            lines.append(f"　　{r.get('points')} 分：{r.get('name')}{cnt}")
    return ("每周氏族组队奖励", lines)


def fmt_flash_sales(sales: Iterable[dict],
                    page: int = 1, page_size: int = 15) -> tuple[str, list[str]]:
    """游戏内商店在售礼包，支持翻页。

    Args:
        sales: 礼包列表。
        page: 页码，从 1 开始。
        page_size: 每页条数。

    Returns:
        ``(标题含页码, 行列表)``。
    """
    sales = list(sales or [])
    if not sales:
        return ("商城在售礼包", ["当前没有在售的限时礼包"])
    total = len(sales)
    pages = max(1, (total + page_size - 1) // page_size)
    page = max(1, min(page, pages))
    chunk = sales[(page - 1) * page_size: page * page_size]
    lines = []
    for s in chunk:
        end = (s.get("end") or "")[:10]
        long_run = end >= "2029"       # DE 用 2030 表示「长期在售」
        tag = "　长期在售" if long_run else (f"　至 {end}" if end else "")
        lines.append(f"· {s.get('name')}{tag}")
    if pages > 1:
        lines.append(f"※ 第{page}/{pages}页，共{total}项；加 -2 / -3 翻页")
    lines.append("※ DE 未提供礼包中文名时显示原名")
    return ("游戏内商店在售礼包", lines)


# ---------------------------------------------------------------------------
# 市场排行 / 趋势
# ---------------------------------------------------------------------------


def _short_t(t: str, seq: list) -> str:
    """把统计时间戳压短：日线只留 MM-DD，小时线留 MM-DD HH时。"""
    s = (t or "").strip()
    if len(s) < 16:
        return s
    day, hhmm = s[:10], s[11:16]
    if len(seq) >= 30:          # 90 天日线
        return day[5:]
    hh = hhmm[:2]
    return f"{day[5:]} {hh}时"


def _downsample(values: list[float], target: int = 30) -> list[float]:
    """把长序列等比压缩到 target 个点（取每段均值），避免折行。"""
    v = list(values or [])
    if len(v) <= target:
        return v
    out, n = [], len(v)
    for i in range(target):
        lo = i * n // target
        hi = max(lo + 1, (i + 1) * n // target)
        seg = [x for x in v[lo:hi] if x]
        out.append(sum(seg) / len(seg) if seg else 0)
    return out


def _spark(values: list[float]) -> str:
    """把价格序列压成一行趋势条（8 档，自动降采样）。"""
    v = [x for x in values if x]
    if len(v) < 2:
        return ""
    v = _downsample(v)
    lo, hi = min(v), max(v)
    if hi <= lo:
        return "▁" * len(v)
    blocks = "▁▂▃▄▅▆▇█"
    return "".join(blocks[min(7, int((x - lo) / (hi - lo) * 7.999))] for x in v)


def _fmt_p(v) -> str:
    """价格显示：整数不带小数、小数留 1 位（118.667 → 118.7；空值 → —）。"""
    if not v:
        return "—"
    return f"{v:.1f}".rstrip("0").rstrip(".") + "p"


# 排行分类 → 标签判定（与 api_client.RANK_CATEGORIES 同口径，落盘行上直接过滤）
_RANK_TAG_PRED = {
    # 甲：WM 已下架整件 Prime 甲，实际交易物是套装
    "甲": lambda t: "warframe" in t and "set" in t and "mod" not in t,
    # 武器：整件（暮斩这类非 Prime 整件）+ 套装，排除蓝图/部件
    "武器": lambda t: ("weapon" in t and "mod" not in t
                      and "blueprint" not in t and "component" not in t),
    "卡": lambda t: "mod" in t,
    "部件": lambda t: "component" in t,
    "赋能": lambda t: "arcane_enhancement" in t,
    "主武": lambda t: "weapon" in t and "primary" in t and "mod" not in t,
    "副武": lambda t: "weapon" in t and "secondary" in t and "mod" not in t,
    "近战": lambda t: "weapon" in t and "melee" in t and "mod" not in t,
    "遗物": lambda t: "relic" in t,
}


def _rank_sort_key(r: dict) -> float:
    """排序价：只看当前价（48h 成交中位）—— 用户明确要求按当前价格排序。"""
    return r.get("median48") or 0


def _rank_has_price(r: dict) -> bool:
    """无当前价（48h）的行不进榜（落盘层已滤，这里兜底）。"""
    return bool(r.get("median48"))


def fmt_rank_overview(rows: list[dict]) -> tuple[str, list[str]]:
    """裸「排行」：甲 / 武器 / MOD卡 三组各前五（当前成交中位价降序）。

    卡组在落盘时已只保留 0 级成交（用户要求「全部可交易的 0 级卡片」）。
    """
    groups = [("甲", "甲价格排行"), ("武器", "武器价格排行"), ("卡", "MOD卡价格排行")]
    lines = ["· 主要物品在 warframe.market 的当前成交价格排行："]
    empty = True
    for cat, title in groups:
        pred = _RANK_TAG_PRED[cat]
        sel = sorted([r for r in rows if pred(set(r.get("tags") or []))
                      and _rank_has_price(r)],
                     key=_rank_sort_key, reverse=True)[:5]
        if not sel:
            continue
        empty = False
        # ◆ 行是 section（整行绘制、不参与列对齐），表头必须独立成行才能与
        # 数据列对齐
        lines.append(f"◆ {title}")
        lines.append("　名称　当前价　最低　最高　上期中位")
        for r in sel:
            name = r.get("zh") or r.get("en") or r["slug"]
            lines.append("· " + "　".join([
                name, _fmt_p(r.get("median48")), _fmt_p(r.get("min48")),
                _fmt_p(r.get("max48")), _fmt_p(r.get("median_prev"))]))
    if empty:
        return ("价格排行", ["暂无落盘数据，请先发「排行 刷新」建立全量榜单"])
    lines.append("※ 当前价=48h 成交中位（按此排序）　最低/最高=48h 区间")
    lines.append("※ 上期中位=前一个 48 小时的成交中位；无 48h 成交的物品不入榜")
    lines.append("※ 完整 20 名榜单：「排行 甲/武器/卡/赋能/部件/主武/副武/近战/遗物」")
    return ("价格排行榜", lines)


def fmt_rank_table(category: str, rows: list[dict]) -> tuple[str, list[str]]:
    """分类价格榜：取 20 种按当前成交中位价降序。"""
    pred = _RANK_TAG_PRED.get(category)
    if not pred:
        return (f"{category}价格排行", [f"未知分类「{category}」"])
    sel = sorted([r for r in rows if pred(set(r.get("tags") or []))
                  and _rank_has_price(r)],
                 key=_rank_sort_key, reverse=True)[:20]
    if not sel:
        return (f"{category}价格排行", ["该分类暂无成交数据（榜单未建立或物品无成交）"])
    lines = [f"◆ 前 {len(sel)} 名 · 按当前成交中位价降序"]
    lines.append("　名称　当前价　最低　最高　上期中位")
    for r in sel:
        name = r.get("zh") or r.get("en") or r["slug"]
        lines.append("· " + "　".join([
            name, _fmt_p(r.get("median48")), _fmt_p(r.get("min48")),
            _fmt_p(r.get("max48")), _fmt_p(r.get("median_prev"))]))
    lines.append("※ 当前价=48h 成交中位　上期中位=前一 48 小时成交中位；MOD 卡按 0 级成交计")
    return (f"{category}价格排行", lines)


# DE 周报 itemType → 中文分类（「主武」= 步枪+霰弹枪合并，「副武」= 手枪）
_RIVEN_ITEM_TYPE = {
    "近战": ("Melee Riven Mod",), "步枪": ("Rifle Riven Mod",),
    "霰弹枪": ("Shotgun Riven Mod",), "手枪": ("Pistol Riven Mod",),
    "空战": ("Archgun Riven Mod",), "Zaw": ("Zaw Riven Mod",),
    "组合枪": ("Kitgun Riven Mod",),
    "主武": ("Rifle Riven Mod", "Shotgun Riven Mod"),
    "副武": ("Pistol Riven Mod",),
}


def _riven_pair(entries: list[dict], itypes: tuple,
                veiled: bool = False) -> dict:
    """把周报条目按武器聚合：{compat: {"m0","p0","m1","p1","pop"}}。

    compatibility 为 null 的条目是**未开紫卡** —— 每个武器类别各有一条
    （步枪未开/手枪未开/…）。必须按 itemType 分键，否则会互相覆盖只剩一条
    （上一版就因为都挤进 None 键，五组里只显示出一行 590p）；
    且武器组聚合时要把它们排除（veiled=False），不然「未开·步枪」会混进
    步枪武器榜里。
    """
    itypes = set(itypes)
    out: dict[str, dict] = {}

    def add(e: dict):
        c = e.get("compatibility")
        if c is None:
            if not veiled:
                return
            c = "未开·" + e["itemType"]
        d = out.setdefault(c, {"m0": None, "p0": None, "m1": None, "p1": None})
        if e.get("rerolled"):
            d["m1"], d["p1"] = e.get("median"), e.get("pop")
        else:
            d["m0"], d["p0"] = e.get("median"), e.get("pop")

    for e in entries:
        if e.get("itemType") in itypes:
            add(e)
    return out


_VEILED_TYPE_ZH = {"Melee": "近战未开紫卡", "Rifle": "步枪未开紫卡",
                   "Shotgun": "霰弹枪未开紫卡", "Pistol": "手枪未开紫卡",
                   "Archgun": "空战未开紫卡", "Zaw": "Zaw 未开紫卡",
                   "Kitgun": "组合枪未开紫卡", "Robotic": "守护未开紫卡",
                   "Amalgam": "融合未开紫卡"}


def _riven_row(name: str, d: dict, veiled: bool = False) -> str:
    """紫卡榜一行：武器　0洗中位　0洗热度　已洗中位　已洗热度　参考价。"""
    if veiled:  # 未开卡只有一张「中位 + 热度」，放进 0 洗列
        d = {"m0": d.get("m0"), "p0": d.get("p0"), "m1": None, "p1": None}
    ref = min([x for x in (d.get("m0"), d.get("m1")) if x], default=None)
    cells = [name, _fmt_p(d.get("m0")),
             f"{d['p0']}%" if d.get("p0") is not None else "—",
             _fmt_p(d.get("m1")),
             f"{d['p1']}%" if d.get("p1") is not None else "—",
             _fmt_p(ref)]
    return "· " + "　".join(cells)


def fmt_riven_weekly(snap: dict, zhmap: dict) -> tuple[str, list[str]]:
    """裸「紫卡排行」：近战/手枪/步枪/霰弹枪/未开 五组各前五（按周销量热度）。"""
    entries = snap.get("entries") or []
    lines = ["· 0洗与已洗并列展示，按本周成交热度（≈7 天销量）降序；",
             "· 参考价为两者较低的中位价。"]
    for label, itype in (("近战", ("Melee Riven Mod",)),
                         ("手枪", ("Pistol Riven Mod",)),
                         ("步枪", ("Rifle Riven Mod",)),
                         ("霰弹枪", ("Shotgun Riven Mod",)),
                         ("空战", ("Archgun Riven Mod",))):
        pool = _riven_pair(entries, itype)
        ranked = sorted(pool, key=lambda c: -(pool[c]["p0"] or 0))[:5]
        if not ranked:
            continue
        lines.append(f"◆ {label}")
        lines.append("　武器　0洗中位　0洗热度　已洗中位　已洗热度　参考价")
        for c in ranked:
            name = (zhmap.get((c or "").lower()) or c or "未开紫卡")
            lines.append(_riven_row(name, pool[c]))
    import datetime as _dt
    when = _dt.datetime.fromtimestamp(snap.get("fetched") or 0,
                                      tz=_dt.timezone.utc)
    lines.append("※ 完整榜：「紫卡排行 近战/步枪/霰弹枪/手枪/主武/副武/空战/组合枪/Zaw/未开」"
                 "｜强制更新：「紫卡排行 刷新」")
    lines.append("※ 未开紫卡实时价：「紫卡排行 未开」（warframe.market 实时成交）")
    lines.append(f"※ DE 官方周报（每周更新一次），抓取于 {when:%m-%d %H:%M} UTC")
    return ("紫卡热度排行榜", lines)


def fmt_riven_type(cat: str, snap: dict, zhmap: dict,
                   page: int = 1, page_size: int = 10,
                   veiled_wm: Optional[dict] = None) -> tuple[str, list[str]]:
    """「紫卡排行 类型」：按首要排序 0洗热度降序、次要已洗热度降序；参考价=较低中位。

    ``veiled_wm``：未开紫卡的 WM 实时成交价（``wm_veiled_stats`` 产物）。
    DE 周报的未开条目是一周口径且明显偏高（组合枪/Zaw 报 590-600p，WM 实时
    个位数 P），有 WM 数据时未开视图一律用它。
    """
    if cat == "未开" and veiled_wm:
        rows = sorted(veiled_wm.items(),
                          key=lambda kv: -(kv[1].get("median") or 0))
    elif cat == "未开":
        # 未开紫卡 = compatibility 为 null 的条目，按武器类别各有一条
        #（键形如「未开·Melee Riven Mod」，翻译成中文类别名）。
        # ⚠️ 必须先把条目过滤到只剩未开，否则 itemType 过滤会把该类别
        # 的所有武器都带进来（上一版就因此列出 419 "种"）。
        veiled_entries = [e for e in (snap.get("entries") or [])
                          if not e.get("compatibility")]
        pool = _riven_pair(veiled_entries,
                           tuple(e["itemType"] for e in veiled_entries),
                           veiled=True)
        rows = sorted(pool.items(), key=lambda kv: -(kv[1]["p0"] or 0))
    else:
        itypes = _RIVEN_ITEM_TYPE.get(cat)
        if not itypes:
            return (f"紫卡热度·{cat}",
                    [f"未知分类「{cat}」。可用：近战/步枪/霰弹枪/手枪/主武/副武/"
                     f"空战/组合枪/Zaw/未开"])
        pool = _riven_pair(snap.get("entries") or [], itypes)
        # 首要排序：0洗热度；次要：已洗热度（均降序）
        rows = sorted(pool.items(),
                      key=lambda kv: (-(kv[1]["p0"] or 0), -(kv[1]["p1"] or 0)))
    if not rows:
        return (f"紫卡热度·{cat}", ["DE 本周数据里该分类暂无成交记录"])
    total = len(rows)
    pages = max(1, (total + page_size - 1) // page_size)
    page = max(1, min(page, pages))
    chunk = rows[(page - 1) * page_size: page * page_size]
    if cat == "未开" and veiled_wm:
        lines = ["◆ 未开紫卡 · warframe.market 实时成交（48h 中位）"]
        lines.append("　类别　当前中位　最低　最高")
        for tkey, d in chunk:
            name = _VEILED_TYPE_ZH.get(tkey, tkey + "未开紫卡")
            lines.append("· " + "　".join([
                name, _fmt_p(d.get("median")),
                _fmt_p(d.get("min")), _fmt_p(d.get("max"))]))
        lines.append("※ 来源：warframe.market 未开紫卡条目的实时成交统计"
                     "（DE 周报口径滞后且偏高，已弃用）")
        lines.append(f"※ 页码:{page}/{pages}　使用「紫卡排行 未开 -{page + 1}」查看下一页"
                     if page < pages else
                     f"※ 页码:{page}/{pages}　已是最后一页")
        return ("紫卡热度·未开", lines)
    lines = [f"◆ 共 {total} 种 · 按首要 0洗热度降序（次要：已洗热度）"]
    if cat == "未开":
        lines.append("　类别　中位　热度　参考价")
    else:
        lines.append("　武器　0洗中位　0洗热度　已洗中位　已洗热度　参考价")
    for c, d in chunk:
        if cat == "未开":
            base_t = (c or "").replace("未开·", "").replace(" Riven Mod", "")
            name = _VEILED_TYPE_ZH.get(base_t, base_t + "未开紫卡")
            ref = d.get("m0") or d.get("m1")
            lines.append("· " + "　".join([
                name, _fmt_p(d.get("m0")),
                f"{d['p0']}%" if d.get("p0") is not None else "—",
                _fmt_p(ref)]))
            continue
        name = (zhmap.get((c or "").lower()) or c or "未开紫卡")
        lines.append(_riven_row(name, d))
    lines.append(f"※ 页码:{page}/{pages}　使用「紫卡排行 {cat} -{page + 1}」查看下一页"
                 if page < pages else
                 f"※ 页码:{page}/{pages}　已是最后一页")
    return (f"紫卡热度·{cat}", lines)


def fmt_riven_analysis(name: str, disposition: float, cls: str,
                       stats_pos: list, stats_neg: list) -> tuple[str, list[str]]:
    """紫卡分析：逐词条给出 DE 机制下的取值区间与卷度。

    区间 = wiki「Riven Mods」页公式：基值 × 倾向 × 词条数系数 × 随机 ±10%。
    距中 = (数值-区间中值)/中值，正为高卷、负为低卷；负词条按幅度比较
    （幅度越接近上限=负得越多=对武器越友好）。
    """
    from . import riven_analysis as RA
    from .parser import RIVEN_STAT_ZH
    dots = {5: "●●●●●", 4: "●●●●○", 3: "●●●○○", 2: "●●○○○", 1: "●○○○○"}
    stars = next((v for lo, v in ((1.31, 5), (1.11, 4), (0.9, 3),
                                  (0.7, 2), (0.5, 1)) if disposition >= lo), 1)
    zh = RIVEN_STAT_ZH
    # 近战没有「射速」：合并词条 fire_rate 在近战上就是攻速（与拍卖卡同一套口径）
    _melee = cls == "melee"

    def _sid(s: str) -> str:
        return "attack_speed" if (_melee and s == "fire_rate") else s

    lines = [f"◆ 【{name}】倾向 {disposition:g}（{dots.get(stars, '?')}）"]
    for sid0, v in stats_pos:
        sid = _sid(sid0)
        lo, hi = RA.stat_range(sid, cls, disposition, len(stats_pos),
                               len(stats_neg))
        if lo is None:
            lines.append(f"· +{RA.fmt_value(sid, v)} {zh.get(sid, sid)}　暂无基值数据")
            continue
        dev = RA.deviation_pct(v, lo, hi)
        pos_pct = RA.range_position(v, lo, hi)
        lines.append("· " + "　".join([
            f"+{RA.fmt_value(sid, v)} {zh.get(sid, sid)}",
            f"{RA.fmt_value(sid, lo)}-{RA.fmt_value(sid, hi)}",
            f"距中{dev:+.1f}%", f"区间位 {pos_pct}%"]))
    for sid0, v in stats_neg:
        sid = _sid(sid0)
        lo, hi = RA.stat_range(sid, cls, disposition, len(stats_pos),
                               len(stats_neg), negative=True)
        if lo is None:
            lines.append(f"· -{RA.fmt_value(sid, v)} {zh.get(sid, sid)}　暂无基值数据")
            continue
        dev = RA.deviation_pct(v, lo, hi)
        pos_pct = RA.range_position(v, lo, hi)
        # ★ 2026-09-24 用户报障（红框）：负词条这里原写 100-pos_pct，方向反了——
        #   数值贴近下限（负得很浅）反而显示「幅度位 87%」（=负得很满），
        #   把浅负当深负卖。区间位本身就是「幅度接近上限的程度」，
        #   直接用 pos_pct：0%=最浅、100%=最满。
        lines.append("· " + "　".join([
            f"-{RA.fmt_value(sid, v)} {zh.get(sid, sid)}",
            f"{RA.fmt_value(sid, lo)}-{RA.fmt_value(sid, hi)}",
            f"幅度位 {pos_pct}%", "幅度越大越友好"]))
    lines.append("※ 区间 = DE 属性基值 × 倾向 × 词条数系数 × 随机 0.9~1.1"
                 "（相对中值 ±10%；上限=最满，下限=最弱）")
    lines.append("※ 「±11%」是把基准取成低值端（110÷90=+22.2% 再折半）的误传，"
                 "官方 wiki 为 90%~110%")
    lines.append("※ 正词条距中为正=高卷；负词条幅度位越高=负得越满（通常越好卖）")
    return ("紫卡分析", lines)


def fmt_trend(name: str, stats: dict, summary: dict) -> tuple[str, list[str]]:
    """单个物品的价格趋势（v1 统计 48h + 90d）。"""
    h48 = stats.get("h48") or []
    d90 = stats.get("d90") or []
    if not h48 and not d90:
        return (f"{name} 价格趋势", ["warframe.market 未返回该物品的成交统计"])
    lines = []
    s = summary
    lines.append(f"◆ 近 48 小时：{s['vol48']} 笔成交，中位 {s['median48']}p"
                 + (f"，均 {s['avg90']}p" if s.get("avg90") else ""))
    if s.get("change"):
        arrow = "▲" if s["change"] > 0 else "▼"
        lines.append(f"◆ 走势：{arrow}{abs(s['change'])}%"
                     f"（最新 {s['last']}p vs 区间初 {s['first']}p）")
    hourly = _spark([r["median"] for r in h48])
    if hourly:
        lines.append(f"· 48h 中位价走势：{hourly}")
    daily = _spark([r["median"] for r in d90])
    if daily:
        lines.append(f"· 90d 中位价走势：{daily}")
    best = [r for r in (d90 or h48) if r["volume"]]
    if best:
        peak = max(best, key=lambda r: r["volume"])
        lines.append(f"· 峰值成交：{_short_t(peak['t'], d90)}　{peak['volume']} 笔　{peak['median']}p")
    lo = min((r for r in h48 if r["min"]), key=lambda r: r["min"], default=None)
    if lo:
        lines.append(f"· 48h 最低成交：{lo['min']}p（{_short_t(lo['t'], h48)}）")
    lines.append("※ 数据源：warframe.market 官方成交统计（48 小时逐小时 / 90 天逐日）")
    return (f"{name} 价格趋势", lines)


def fmt_prime_relics(rows: list[dict], title: str = "遗物列表",
                     page: int = 1, page_size: int = 10) -> tuple[str, list[str]]:
    """遗物列表 / 出入库状态，带翻页与掉落位置。

    语义（沿用社区习惯，与游戏内「入库/出库」一致）：
      · **出库** = 已从金库放出，**当前可以掉落**（unvaulted）
      · **入库** = 已收回金库，**当前不能刷取**（vaulted）

    Args:
        rows: ``[{"cn", "tier", "unvaulted", "sources"}]``。
        title: 卡片标题。
        page: 页码，从 1 开始。
        page_size: 每页条数。

    Returns:
        ``(标题含页码, 行列表)``。
    """
    if not rows:
        return (title, ["没有可展示的遗物"])
    total = len(rows)
    pages = max(1, (total + page_size - 1) // page_size)
    page = max(1, min(page, pages))
    chunk = rows[(page - 1) * page_size: page * page_size]

    lines: list[str] = []
    for i, r in enumerate(chunk, (page - 1) * page_size + 1):
        drop = bool(r.get("unvaulted"))
        tag = "可掉落" if drop else "已入库"
        lines.append(f"{i}. [{tag}] {r.get('cn', '?')}　{r.get('tier', '')}".rstrip())
        src = r.get("sources") or []
        if src:
            more = r.get("source_total", len(src)) - len(src)
            tail = f"（共 {r['source_total']} 处）" if more > 0 else ""
            lines.append("　　掉落位置：" + "；".join(src) + tail)
        elif not drop:
            lines.append("　　掉落位置：已入库，当前不在任何掉落池中")
    lines.append(f"※ 第{page}/{pages}页，共{total}个；加 -2 / -3 翻页")
    lines.append("※ 出库=可掉落，入库=不可刷取；判定依据是 DE 官方掉落池")
    return (f"{title}（第{page}/{pages}页，共{total}个）", lines)


# 遗物纪元的固定展示顺序（DE 的 5 个常规纪元 + 特殊）。
# ⚠️ 命名务必与上面裂隙用的 `_TIER_ORDER`（英文纪元 → 序号字典）区分开：
#    2026-09-17 这里曾误用同名 `_TIER_ORDER`，把上面那个字典**覆盖**成元组，
#    导致 fmt_fissures 运行时 `'tuple' object has no attribute 'get'` 崩溃
#    （裂隙指令线上报错）。同模块内不要给不同结构复用同一个模块级名字。
_RELIC_TIER_ORDER = ("古纪", "前纪", "中纪", "后纪", "安魂", "全能")
# 每行并排几个遗物名。**实测得出，不要凭感觉改**：
# 正文 30px（NotoSansCJK）下「古纪 A12」实测 119px、最长「前纪 N11」123px，
# 列对齐模型的总宽 = Σ列宽 + 24×(列数-1)，9 列 = 1245px，刚好落在列对齐
# 安全阀的可用宽度（W - 178 = 1322，W 上限 1500）之内；10 列 = 1377px 会
# 触发安全阀整体放弃对齐。要改列数请先跑 scripts/bench_relic_grid.py 重测。
_TIER_PER_LINE = 9
# 遗物中文名：DE 官方简中写作「古纪 S1 遗物」（name_zh.json），卡面按
# 「古纪 S1」显示 —— 去掉「遗物」后缀但**保留纪元中文**：玩家口语就是
# 「古纪A1」，只留代号会看不出是哪个纪元（用户 2026-09-17 反馈
# 「遗物中文缺失」：列表里当时显示的是「Lith A12」这类英文）。
_RELIC_TIER_EN2CN = {"Lith": "古纪", "Meso": "前纪", "Neo": "中纪",
                     "Axi": "后纪", "Requiem": "安魂", "Omnia": "全能",
                     "Vanguard": "先锋"}
_RELIC_CN_TIERS = tuple(_RELIC_TIER_EN2CN.values())
_RELIC_TIER_CN2EN = {cn: en.lower() for en, cn in _RELIC_TIER_EN2CN.items()}
_RELIC_CODE_RE = re.compile(r"^([A-Za-z])(\d{1,2})$")
# 遗物名后缀：DE 官方给的是「古纪 S1 遗物」，英文 wiki/掉落表给的是
# 「Axi S20 Relic」—— 两种后缀都要能剥掉，否则代号里混进「Relic」
# 会匹配不上（relic_en_key 会直接返回空串）。
_REL_SUFFIX_RE = re.compile(r"\s*(?:遗物|relics?)\s*$", re.I)
_RELIC_TIGHT_RE = re.compile(
    r"^(古纪|前纪|中纪|后纪|安魂|全能|先锋)\s*([A-Za-z]\d{1,2})$")


def relic_cn(name: str) -> str:
    """把各种写法的遗物名统一成「古纪 A1」形式（官方简中口径）。

    必须吃得下这些历史写法（各处来源不一）：
      ``古纪 Lith A1``  —— ``relic_index.json`` / ``relic_inverse.json`` 的键
      ``Lith A1``       —— 掉落表键、外部来源
      ``古纪 S1 遗物``  —— DE ``name_zh.json`` 的官方写法
      ``后纪A2``        —— 用户输入（无空格）
    认不出来的原样返回，**不猜**。
    """
    raw = (name or "").strip()
    if not raw:
        return raw
    s = _REL_SUFFIX_RE.sub("", raw).strip()
    m = _RELIC_TIGHT_RE.match(s)          # 「后纪A2」这种没有空格的
    if m:
        return f"{m.group(1)} {m.group(2).upper()}"
    toks = s.split()
    tier = ""
    if toks and toks[0] in _RELIC_CN_TIERS:
        tier = toks.pop(0)
    if toks and toks[0] in _RELIC_TIER_EN2CN:
        tier = _RELIC_TIER_EN2CN[toks.pop(0)]
    if not tier:
        return raw
    code = " ".join(toks).strip().upper()
    return f"{tier} {code}".strip()


def _relic_sort_key(name: str):
    """遗物名的自然序：A1 < A2 < … < A10（直接按字符串排会得到 A1/A10/A11/A2）。"""
    m = _RELIC_CODE_RE.match(name.split()[-1]) if name else None
    if m:
        return (1, m.group(1), int(m.group(2)))
    return (0, name, 0)


def relic_en_key(name: str) -> str:
    """遗物名 → 掉落库/AH 口径的键前缀（``后纪 Axi A12`` / ``古纪 A12`` / ``Axi A12`` → ``axi a12``）。

    掉落表（``drops.json`` 的 ``relic_drops``）与阿耶商店接口给的键都是
    「英文纪元小写 + 代号小写」这种形式（``lith a12``）。卡面用的是中文
    「古纪 A12」，两边要能对上，否则状态判定全错 —— 这里统一转一次。

    认不出（代号不是「字母+1~2位数字」）返回空串，调用方按「入库」处理，
    **不猜**。
    """
    toks = _REL_SUFFIX_RE.sub("", name or "").split()
    if not toks:
        return ""
    tight = _RELIC_TIGHT_RE.match(" ".join(toks))    # 「后纪A2」没有空格的写法
    if tight:
        return f"{_RELIC_TIER_CN2EN[tight.group(1)]} {tight.group(2).lower()}"
    tier = ""
    if toks[0] in _RELIC_TIER_EN2CN:            # 英文纪元在前：Axi A20
        tier = _RELIC_TIER_EN2CN[toks.pop(0)]
    elif toks[0] in _RELIC_CN_TIERS:            # 中文纪元在前：古纪 A12
        tier = toks.pop(0)
        if toks and toks[0] in _RELIC_TIER_EN2CN:
            # 「古纪 Lith A12」（relic_inverse 存的写法）再吃掉一段英文纪元
            tier = _RELIC_TIER_EN2CN[toks.pop(0)]
    code = " ".join(toks).strip()
    if not tier or not _RELIC_CODE_RE.match(code):
        return ""
    return f"{_RELIC_TIER_CN2EN.get(tier, tier.lower())} {code.lower()}"


# 推荐刷取行能容纳的显示宽度（以「全角字符」为单位）。
# 实测：25px 注脚下可用宽度 1366px ≈ 55 个全角字符；19 个推荐点里最长的一行
# （后纪）是 53.5 个 —— 卡在临界值上会折行、尾行只剩几个字。
# 留 4 个字符余量，放不下就少放一个点（宁可少推荐也不让注释折行）。
_HINT_MAX_CELLS = 51


def _hint_cells(s: str) -> int:
    """字符串的显示宽度（全角记 1，半角记 0.5），用于估算是否放得下一行。"""
    import unicodedata
    return sum(2 if unicodedata.east_asian_width(c) in ("F", "W") else 1
               for c in s)


def _fit_hints(hints: list[str], prefix: str = "※ 推荐刷取：") -> list[str]:
    """按显示宽度裁剪推荐刷取点，保证注脚行不折行。

    Args:
        hints: 候选推荐点。
        prefix: ``※`` 行前缀 —— 宽度要算进去（部件反查卡的前缀带纪元名，
            比出库卡的长，用同一个常量会放多一个点然后折行）。
    """
    out: list[str] = []
    used = _hint_cells(prefix)
    for h in hints:
        w = _hint_cells(h) + _hint_cells(" ｜ ")
        if out and used + w > _HINT_MAX_CELLS * 2:
            break
        out.append(h)
        used += w
    return out or hints[:1]


def fmt_relic_by_tier(rows: list[dict], title: str = "遗物列表",
                      page: int = 1, page_size: int = 90,
                      farm_hints: Optional[dict[str, list[str]]] = None,
                      specials: Optional[dict[str, str]] = None
                      ) -> tuple[str, list[str]]:
    """遗物按**纪元**分组展示（2026-09-17 用户要求）。

    与 :func:`fmt_prime_relics` 的分工：
      · 后者逐条列「掉落位置」，适合单查某个遗物；
      · 本函数用于**批量列表**，不再输出掉落位置 —— 那会把同一批星球/节点
        重复贴几十遍（用户：「列出一大堆重复星系没啥用阿」），
        改为同一纪元的遗物名并排紧凑排布，一屏看清「这个纪元有哪些遗物」。

    Args:
        rows: ``[{"cn": "古纪 Lith A1", "tier_cn": "古纪"}]``（``tier_cn``
            缺省时从 ``cn`` 首个 token 推断）；``cn`` 会经 :func:`relic_cn`
            统一成「古纪 A1」。
        page: 页码，从 1 开始。
        page_size: 每页遗物数（默认 90 ≈ 10 行 9 列）。
        farm_hints: ``{纪元: ["虚空 · Taranis · 防御", …]}`` 推荐刷取点，
            来自 :func:`core.drops.relic_farm_hints`（数据驱动）。只在出库卡传。
        specials: ``{遗物名: "仅阿耶兑换"}`` 这类**特殊获取渠道**说明，会在该
            纪元下单独起一行标出（用户要求：「有一部分遗物只要指定位置能获取，
            那种单独去标记」）。
    """
    if not rows:
        return (title, ["没有可展示的遗物"])

    groups: dict[str, list[str]] = {}
    for r in rows:
        cn = relic_cn(r.get("cn") or "?")
        tier = (r.get("tier_cn") or "").strip() or \
            (cn.split()[0] if " " in cn else "其他")
        groups.setdefault(tier, []).append(cn)

    order = [t for t in _RELIC_TIER_ORDER if t in groups] + \
        [t for t in groups if t not in _RELIC_TIER_ORDER]

    # 平铺成 (纪元, 显示名) 后分页；显示名 = 完整中文名（含纪元），
    # 自然序排列（A1 < A2 < … < A10，直接字符串排会得到 A1/A10/A11/A2）。
    flat: list[tuple[str, str]] = []
    for t in order:
        for cn in sorted(groups[t], key=_relic_sort_key):
            flat.append((t, cn))

    total = len(flat)
    pages = max(1, (total + page_size - 1) // page_size)
    page = max(1, min(page, pages))
    chunk = flat[(page - 1) * page_size: page * page_size]

    lines: list[str] = []
    cur, buf = None, []

    def _flush_buf():
        if buf:
            lines.append("· " + "　".join(buf))
            buf.clear()

    # 该纪元的两个补充行（只在出库卡传参时出现）。
    # ★ 注意：这里刻意**不用全角空格**分隔 —— 全角空格会参与渲染层的
    #   列宽计算（col_max 是按「含全角空格的行」逐列取最大值的），
    #   注释行混进去会把表格宽度算爆、触发安全阀放弃列对齐。
    def _tier_notes(t: str):
        if specials:
            hits = [(n, specials[n]) for n in groups.get(t, []) if n in specials]
            if hits:
                # 按原因分组：同一原因（如「仅阿耶兑换」）只写一次，
                # 否则「后纪 H5 仅阿耶兑换；…；后纪 A12 仅阿耶兑换」会把
                # 这一行撑到两行（实测 1829px，可用只有 1352px）。
                by_reason: dict[str, list[str]] = {}
                for n, why in hits:
                    by_reason.setdefault(why, []).append(n)
                shown = "；".join(f"{'、'.join(ns)}（{why}）"
                                 for why, ns in by_reason.items())
                lines.append(f"※ 特殊渠道：{shown}")
        if farm_hints and farm_hints.get(t):
            lines.append("※ 推荐刷取：" + " ｜ ".join(_fit_hints(farm_hints[t])))

    # 每行放几个：整段都在本页时**按行数均分**，不要出现「9 个一行 + 1 个
    # 孤零零」（10 个条目排成 9+1 时右半边空一大片，用户说「浪费空间」）。
    def _per_line(count: int) -> int:
        if count <= _TIER_PER_LINE:
            return max(1, count)
        rows = (count + _TIER_PER_LINE - 1) // _TIER_PER_LINE
        return (count + rows - 1) // rows

    chunk_counts = Counter(t for t, _ in chunk)
    per_line = _TIER_PER_LINE
    for t, short in chunk:
        if t != cur:
            if cur is not None:
                _tier_notes(cur)
                _flush_buf()
            cur = t
            lines.append(f"◆ {t}（{len(groups[t])}）")
            per_line = _per_line(chunk_counts[t])
        buf.append(short)
        if len(buf) >= per_line:
            _flush_buf()
    _flush_buf()
    if cur is not None:
        _tier_notes(cur)

    summary = "　".join(f"{t} {len(groups[t])}" for t in order)
    lines.append(f"※ 共 {total} 个：{summary}")
    lines.append(f"※ 第{page}/{pages}页，加 -2 / -3 翻页；"
                 f"查某个遗物的奖励与出处用「遗物 名称」")
    return (f"{title}（第{page}/{pages}页，共{total}个）", lines)


# 部件反查卡最多列几把遗物（Forma 蓝图那种一张卡涉及 500+ 把，必须截断）
_PIECE_MAX_ROWS = 12
# 状态 → 卡面用词。★ 用词要和「遗物 出库/入库」两张卡的语义一致：
# 出库=现在能拿到，入库=收进金库、任务里刷不到。
_PIECE_STATE_TEXT = {"drop": "可掉落", "varzia": "仅阿耶兑换", "vaulted": "已入库"}
# 排序优先级：能拿到的排最前（用户查部件最先想知道的就是「现在能不能刷」）
_PIECE_STATE_ORDER = {"drop": 0, "varzia": 1, "vaulted": 2}


# ---------------------------------------------------------------------------
# 玄骸拍卖（Kuva / Tenet / Coda）
# ---------------------------------------------------------------------------
# 玄骸武器的元素只有这几种（WM 的 item.element 取值）。卡面显示中文，
# 用户输入中英文都认（用户 2026-09-18：「只有伤害加成，没有元素」——
# 其实是显示成了英文 radiation/toxin，中文玩家对不上）。
LICH_ELEM_CN = {
    "magnetic": "磁力", "electricity": "电击", "toxin": "毒素",
    "heat": "火焰", "cold": "冰冻", "impact": "冲击", "slash": "切割",
    "radiation": "辐射",
}
LICH_ELEM_EN = {v: k for k, v in LICH_ELEM_CN.items()}
# 玩家可能写单字或英文
LICH_ELEM_ALT = {
    "电": "电击", "毒": "毒素", "火": "火焰", "冰": "冰冻", "辐": "辐射",
    "磁": "磁力", "冲": "冲击", "切": "切割",
    "electric": "电击", "toxic": "毒素", "fire": "火焰", "ice": "冰冻",
    "rad": "辐射", "mag": "磁力",
}
LICH_OWNER_STATUS_CN = {"ingame": "游戏内", "online": "在线"}


def fmt_lich_row(i: int, auction: dict) -> str:
    """玄骸拍卖的一行：价格 / 元素 / 伤害 / 卖家状态与信用 / 幻纹。

    2026-09-18 用户要求补三样：**元素（中文）**、**在线情况**、**信用等级**，
    这些数据本来就在 WM 返回的 item/owner 里，原实现只取了价格与伤害。
    """
    it = auction.get("item") or {}
    ow = auction.get("owner") or {}
    price = auction.get("buyout_price") or auction.get("starting_price") or 0
    raw_elem = str(it.get("element") or "")
    elem = LICH_ELEM_CN.get(raw_elem, raw_elem or "?")
    dmg = it.get("damage")
    dmg_s = f"{int(dmg)}%" if isinstance(dmg, (int, float)) else "?"
    status = LICH_OWNER_STATUS_CN.get(str(ow.get("status") or ""), "离线")
    rep = ow.get("reputation")
    rep_s = f"·信用{int(rep)}" if isinstance(rep, (int, float)) else ""
    eph = "｜幻纹✦" if it.get("having_ephemera") else ""
    return f"{i}. {price}p {elem}｜伤害 {dmg_s}｜{status}{rep_s}{eph}"


def fmt_relic_piece(piece: str, origins: list[dict], *,
                    farm_hints: Optional[dict[str, list[str]]] = None
                    ) -> tuple[str, list[str]]:
    """部件反查卡：这个部件出自哪些遗物、**现在还能不能拿到**、去哪刷。

    2026-09-18 用户反馈：「没有写能不能获取，以及刚刚那种推荐位置也能加上」
    —— 旧版只列「遗物名 + 槽位」，用户看不出这把遗物是出库还是入库。
    实测 595 个部件里只有 128 个当前至少有一把遗物在掉落表里，
    「查了半天发现刷不到」是最需要提前说清楚的事，所以状态必须写在行内。

    Args:
        piece: 部件名（如「电幻步枪Prime蓝图」）。
        origins: ``[{"relic": "古纪 Lith A12", "rarity": "稀有",
            "state": "drop"}]``；``state`` 取
            ``drop``（在掉落表）/ ``varzia``（仅阿耶在售）/ ``vaulted``（已入库）。
        farm_hints: 纪元级推荐刷取点（``core.drops.farm_hints()``）。
            **只对「可掉落」的纪元给建议** —— 已入库的遗物刷不到，
            给了位置反而是误导；仅阿耶兑换的也不给任务点位。

    Returns:
        ``(标题, 行列表)``。
    """
    rows: list[dict] = []
    for o in origins:
        cn = relic_cn(o.get("relic") or "")
        state = (o.get("state") or "vaulted").strip()
        if state not in _PIECE_STATE_TEXT:
            state = "vaulted"
        rows.append({
            "cn": cn,
            "rarity": (o.get("rarity") or "").strip(),
            "state": state,
            "tier": cn.split()[0] if " " in cn else "其他",
        })
    if not rows:
        return (f"部件出处：{piece}", ["未找到该部件的遗物出处"])

    rows.sort(key=lambda r: (
        _PIECE_STATE_ORDER[r["state"]],
        _RELIC_TIER_ORDER.index(r["tier"])
        if r["tier"] in _RELIC_TIER_ORDER else len(_RELIC_TIER_ORDER),
        _relic_sort_key(r["cn"])))

    n_ok = sum(1 for r in rows if r["state"] != "vaulted")
    lines = [f"◆ 「{piece}」的遗物出处（可获取 {n_ok} / 已入库 {len(rows) - n_ok}）"]
    for r in rows[:_PIECE_MAX_ROWS]:
        lines.append(f"· {r['cn']}　{r['rarity'] or '?'}槽　"
                     f"{_PIECE_STATE_TEXT[r['state']]}")
    if len(rows) > _PIECE_MAX_ROWS:
        # ★ 「另有 N 把已入库」是当初按「可掉落排最前」想当然写的：Forma 蓝图
        #   有 30 把可掉落、545 把总数，截到 12 行后**隐藏的 533 把里还有 18 把
        #   是可掉落的** —— 这么说等于把能刷的藏起来了。这里按实际算。
        hidden = rows[_PIECE_MAX_ROWS:]
        hidden_ok = sum(1 for r in hidden if r["state"] != "vaulted")
        if hidden_ok:
            lines.append(f"※ 还有 {len(hidden)} 把未列出"
                         f"（其中 {hidden_ok} 把现在就能掉落）")
        else:
            lines.append(f"※ 另有 {len(hidden)} 把已入库遗物未展开")

    # ------------------------------------------------------------------
    # 推荐刷取：**永远只给「当前出库（可掉落）」的那几把所属纪元**。
    # 用户 2026-09-18 明确：「出库的时候另外两个多半入库了，到时候下面还是
    # 一个推荐，保持底下永远是出库的那个推荐刷新位置就行」。
    #   · 已入库的遗物现在刷不到，给点位是误导；
    #   · 仅阿耶兑换的靠买不靠刷，同样不给任务点位；
    #   · 所以「一个部件三把遗物、只有一把出库」时，底下就只会有**一条**
    #     推荐行，且它一定对应那把能刷的 —— 这正是要的效果，别去凑数。
    # ------------------------------------------------------------------
    tiers: list[str] = []
    for r in rows:
        if r["state"] == "drop" and r["tier"] not in tiers:
            tiers.append(r["tier"])
    hinted = 0
    for t in tiers:
        hints = (farm_hints or {}).get(t) or []
        if not hints:
            continue
        # 纪元名放在括号里：写成「古纪推荐刷取：」读着像机器话，
        # 且与出库卡那句「※ 推荐刷取：」保持同一个说法更好认。
        prefix = f"※ 推荐刷取（{t}）："
        picked = _fit_hints(hints, prefix=prefix)
        if picked:
            lines.append(prefix + " ｜ ".join(picked))
            hinted += 1

    n_drop = sum(1 for r in rows if r["state"] == "drop")
    if not n_ok:
        lines.append("※ 这些遗物都已入库，当前任务里刷不到；"
                     "可留意阿耶（Varzia）兑换或等 DE 重新出库")
    elif n_drop and not hinted:
        # 有可掉落遗物却没算出推荐点（该纪元不在 farm_hints 里）—— 理论上
        # 不会发生（当前 34 把可掉落遗物都在古/前/中/后四纪元），但真发生了
        # 也必须说清是哪把，不能静默把「能刷」的信息吞掉。
        names = "、".join(r["cn"] for r in rows if r["state"] == "drop")[:36]
        lines.append(f"※ 能掉落的是 {names}，用「遗物 名称」可看具体出处")
    elif not n_drop:
        lines.append("※ 可获取的那几把只在阿耶（Varzia）商店出售，任务里刷不到")
    return (f"部件出处：{piece}", lines)


def fmt_relic_rewards(relic: str, slots: list[dict],
                      prices: Optional[dict] = None) -> tuple[str, list[str]]:
    """遗物三槽位奖励 + 每件奖励的杜卡德 / 白金 / 杜・白比值。

    Args:
        relic: 遗物名（如 ``后纪 A2``）。
        slots: ``[{"rarity", "name", "chance"}]``。
        prices: 名称 -> ``{"ducats", "plat", "dpp"}``（来自 WM 杜卡德榜单）。

    Returns:
        ``(标题, 行列表)``。
    """
    relic = relic_cn(relic)          # 「古纪 Lith A1」→「古纪 A1」（标题也走中文）
    if not slots:
        return (f"遗物：{relic}", ["未找到该遗物的奖励数据"])
    prices = prices or {}
    lines = []
    for s in slots:
        name = s.get("name") or "?"
        p = prices.get(name) or prices.get(re.sub(r"\s+", "", name)) or {}
        extra = ""
        if p:
            bits = []
            if p.get("ducats"):
                bits.append(f"{int(p['ducats'])}杜")
            if p.get("plat"):
                bits.append(f"{p['plat']:.0f}p")
            if p.get("dpp"):
                bits.append(f"{p['dpp']:.1f}杜/p")
            if bits:
                extra = "　" + " ".join(bits)
        lines.append(f"· [{s.get('rarity', '?')}] {name}{extra}"
                     + (f"　{s['chance']}%" if s.get("chance") else ""))
    lines.append("※ 杜=杜卡德　p=白金　杜/p=每白金换到的杜卡德（越高越值）")
    lines.append("※ 价格来自 warframe.market 杜卡德计算器同源数据（近 1 小时）")
    return (f"遗物：{relic}", lines)
