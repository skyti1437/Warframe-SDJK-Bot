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

from . import i18n
from . import tents as _tents
from .de_worldstate import (
    DESCENT_GOAL_CAVEAT as _DESCENT_GOAL_CAVEAT,
    DESCENT_TYPE_NO_KEY as _DT_NO_KEY,
    descent_goal_zh,
    descent_type_zh,
    faction_name,
    mission_type_zh_by_name,
)
from .display_time import format_time
from .i18n import L
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


def _dur_en(mins: int) -> str:
    """英文时长：``3h 12m`` / ``2d 5h`` / ``12m`` / ``<1m``（只用 d/h/m，数字紧贴单位）。"""
    if mins < 60:
        return f"{mins}m" if mins > 0 else "<1m"
    hours = mins // 60
    if hours < 24:
        return f"{hours}h {mins % 60}m"
    return f"{hours // 24}d {hours % 24}h"


def _ended() -> str:
    return L("已结束", "Ended")


def _left_en(cd: str) -> str:
    """英文「剩余」：``3h 12m left``；已结束写 ``Ended``。"""
    return cd if cd == "Ended" else f"{cd} left"


def _left(cd: str, zh_prefix: str = "剩余 ") -> str:
    """倒计时串 → 行文：中文 ``<前缀><时长>``，英文 ``3h 12m left`` / ``Ended``。"""
    return L(f"{zh_prefix}{cd}", _left_en(cd))


def _in_en(cd: str) -> str:
    """英文「N 后开始」：``in 5m``；已结束写 ``Ended``。"""
    return cd if cd == "Ended" else f"in {cd}"


def countdown(expiry: str) -> str:
    """剩余时间：X天X小时 / X小时X分 / X分钟（英文：3h 12m / 2d 5h / 12m / <1m）。"""
    end = parse_iso(expiry)
    if end is None:
        return "?"
    secs = (end - _now()).total_seconds()
    if secs <= 0:
        return _ended()
    mins = int(secs // 60)
    if i18n.is_en():
        return _dur_en(mins)
    if mins < 60:
        return f"{mins}分钟" if mins > 0 else "不到1分钟"
    hours = mins // 60
    if hours < 24:
        return f"{hours}小时{mins % 60}分"
    return f"{hours // 24}天{hours % 24}小时"


def mission_cn(mission_type: str) -> str:
    if i18n.is_en():
        # 英文模式：世界状态里已是 DE 官方英文名，原样返回（全小写的才规整成 Title Case）
        mt = mission_type or ""
        return mt.title() if mt and mt.islower() else mt
    key = (mission_type or "").lower()
    return MISSION_CN.get(key, MISSION_CN.get(key.replace(" ", ""), mission_type))


def tier_cn(tier: str) -> str:
    if i18n.is_en():
        return tier
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


_RIVEN_TYPE_EN = {
    "rifle": "Rifle",
    "shotgun": "Shotgun",
    "pistol": "Pistol",
    "melee": "Melee",
    "archgun": "Archgun",
    "archmelee": "Arch-Melee",
}
_RIVEN_GROUP_EN = {
    "archgun": "Archgun",
    "archmelee": "Arch-Melee",
    "sentinel": "Companion Weapon",
    "kitgun": "Kitgun",
    "zaw": "Zaw",
}


def riven_type_cn(riven_type: str, group: str = "") -> str:
    """riven_type/group → 中文类别；未知值原样返回。

    ★ 2026-09-24 玩家报障「翠雀应该是曲翼枪械」：WM 把曲翼枪械的 rivenType
    也标成 ``rifle``，只看 rivenType 会显示成「步枪」。改为 group 命中更具体
    类别时优先（archgun→曲翼枪械、sentinel→守护武器），否则回落 rivenType。
    """
    g = (group or "").strip().lower()
    if i18n.is_en():
        if g in _RIVEN_GROUP_EN:
            return _RIVEN_GROUP_EN[g]
        key = (riven_type or "").strip().lower()
        if not key:
            return ""
        return _RIVEN_TYPE_EN.get(key, riven_type)
    if g in RIVEN_GROUP_CN:
        return RIVEN_GROUP_CN[g]
    key = (riven_type or "").strip().lower()
    if not key:
        return ""
    return RIVEN_TYPE_CN.get(key, riven_type)


def _state_cn(state: str) -> str:
    """周期状态显示名。

    昼夜/温度这类通用词用中文；而 Fass / Vome / Sorrow / Fear / Joy /
    Anger / Envy 这些**派系与情绪状态词 DE 官方简中一律不翻译**，
    直接沿用英文原文，与游戏内客户端一致（「法斯 / 沃姆」是国服叫法，已弃用）。
    """
    if i18n.is_en():
        return {
            "day": "Day",
            "night": "Night",
            "warm": "Warm",
            "cold": "Cold",
            "fass": "Fass (Day)",
            "vome": "Vome (Night)",
        }.get(state, (state or "").capitalize())
    return {
        "day": "白天",
        "night": "夜晚",
        "warm": "温暖",
        "cold": "寒冷",
        "fass": "Fass（白天）",
        "vome": "Vome（夜晚）",
    }.get(state, (state or "").capitalize())


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
        return _ended(), ""
    mins = secs // 60
    if i18n.is_en():
        # 英文只有一种写法（3h 12m），不再并列「紧凑」那份
        return _dur_en(mins), ""
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
# 英文名取自 DE languages_en（Day/Night{Common,UnCommon,Rare}Plant）
_EARTH_PLANTS_EN = {
    "day": ("Sunlight Dragonlily", "Sunlight Threshcone", "Sunlight Jadeleaf"),
    "night": ("Moonlight Dragonlily", "Moonlight Threshcone", "Moonlight Jadeleaf"),
}

# 扎里曼号派系：官方简中**不翻译**派系名，与游戏内客户端一致。
ZARIMAN_CN = {"grineer": "Grineer", "corpus": "Corpus", "unknown": "未知（DE 未下发周期种子）"}
_ZARIMAN_EN = {
    "grineer": "Grineer",
    "corpus": "Corpus",
    "unknown": "Unknown (no cycle seed from DE)",
}


def fmt_cetus(*cycles: Optional[dict]) -> tuple[str, list[str]]:
    """夜灵/平原时间：地球平原、金星山谷、魔胎之境、地球、双衍王境、扎里曼号全部周期。"""
    names = {
        "cetus": L("夜灵平野", "Plains of Eidolon"),
        "vallis": L("奥布山谷", "Orb Vallis"),
        "cambion": L("魔胎之境", "Cambion Drift"),
        "earth": L("地球", "Earth"),
        "duviri": L("双衍王境", "Duviri"),
        "zariman": L("扎里曼号", "Zariman"),
    }
    S = "　"
    rows: list[str] = []
    soon: Optional[tuple[datetime, str]] = None  # 最先换轮的一项：(到期, 卡首摘要)
    for c in cycles:
        if not c:
            continue
        key = c.get("_name", "")
        name = names.get(key, key or L("周期", "Cycle"))
        if key == "duviri":
            # 双衍王境：状态即「螺旋」（情绪），官方简中译名 + 英文原名。
            # ⚠️ 不要再单列「当前螺旋 / 后续螺旋」——那两行和本行是同一个螺旋，
            #    信息完全重复，还各带一个倒计时（旧版三行并列时尤其容易被当 bug）。
            state_cn = c.get("stateCn") or _state_cn(c.get("state", ""))
            state_en = c.get("state", "")
            if i18n.is_en():
                state = state_en or state_cn
            else:
                state = f"{state_cn}（{state_en}）" if state_cn and state_en else state_cn
        elif key == "zariman":
            state = (_ZARIMAN_EN if i18n.is_en() else ZARIMAN_CN).get(
                c.get("state", ""), c.get("state", "")
            )
        else:
            state = _state_cn(c.get("state", ""))
        # 倒计时统一走 _left_pair：中文 + 紧凑两份同源同刻，行格式各周期完全一致
        # （双衍曾因只写中文秒级、缺「（Xh Ym）」而与其它行格式不一）。
        left, extra = _left_pair(c.get("expiry", ""))
        end = parse_iso(c.get("expiry", ""))
        if end is not None and left != _ended() and (soon is None or end < soon[0]):
            soon = (
                end,
                L(f"{name}（{state}）{S}剩余 {left}", f"{name} ({state}){S}{_left_en(left)}"),
            )
        rows.append(
            L(
                f"· {name}{S}{state}{S}剩余 {left}" + (f"（{extra}）" if extra else ""),
                f"· {name}{S}{state}{S}{_left_en(left)}",
            )
        )
        if key == "earth":
            plants = (EARTH_PLANTS if not i18n.is_en() else _EARTH_PLANTS_EN).get(
                "day" if c.get("isDay") else "night", ()
            )
            if plants:
                # 从属行（缩进续行）：材料随地球昼夜更替
                rows.append(L("　当前材料：", "　Current materials: ") + " · ".join(plants))
    title = L("平原时间", "Open World Cycles")
    if not rows:
        return (title, [L("暂无周期数据", "No cycle data")])
    lines = []
    if soon is not None:
        lines.append(L("◆ 最先换轮：", "◆ Next change: ") + soon[1])
    lines.append(L("◆ 各周期", "◆ Cycles"))
    lines += rows
    return (title, lines)


# 时效总览：每一项是什么（避免只给倒计时看不懂）
#
# ★ 2026-10-11 第二步改版：按「开放世界 / 每日 / 每周 / 其它周期」分四段，
#   每行三列「名称　轮换内容　计时」（计时由渲染层右对齐成琥珀列）；
#   同一刻重置的周常合并成**一行 + 一个计时**，成员名列在下一条缩进行。
#   键 = 调用方传入的名称；值 = (分段, 显示名, 轮换内容, 周常短名)。
_TIMER_DESC = {
    "夜灵平野": ("世界", "夜灵平野", "昼夜交替", ""),
    "奥布山谷": ("世界", "奥布山谷", "温度周期", ""),
    "魔胎之境": ("世界", "魔胎之境", "Fass / Vome", ""),
    "双衍王境": ("世界", "双衍王境", "情绪轮换", ""),
    "扎里曼派系": ("世界", "扎里曼号", "Corpus / Grineer", ""),
    "每日突击": ("每日", "每日突击", "重置", ""),
    "钢铁侵蚀": ("每日", "钢铁之路", "侵袭 · 每日重置", ""),
    "仲裁": ("每日", "仲裁", "每小时轮换", ""),
    "执刑官猎杀": ("周", "执刑官猎杀", "周常重置", "执刑官"),
    "深层科研": ("周", "深层科研", "周常重置", "深层科研"),
    "时光科研": ("周", "时光科研", "周常重置", "时光科研"),
    "沉沦之地": ("周", "沉沦之地", "周常 21 层", "沉沦之地"),
    "钢铁回廊灵化": ("周", "钢铁回廊灵化", "周常轮换", "钢铁回廊灵化"),
    "1999日历": ("周", "1999 日历", "季末重置", "1999 日历"),
    "周常重置": ("周", "本周重置", "周一 00:00 UTC", ""),
    "信条元素加成": ("其它", "信条武器", "每 4 天", ""),
    "终幕换批": ("其它", "终幕武器", "每 4 天", ""),
    "虚空奸商": ("其它", "虚空商人", "抵达", ""),
    "午夜电波": ("其它", "午夜电波", "赛季结束", ""),
}
# 「后开始」的措辞例外：虚空商人是「抵达」
_TIMER_START_WORD = {"虚空奸商": "抵达"}
_TIMER_MISSING = "暂无数据"
_TIMER_SECTIONS = (
    ("世界", "◆ 开放世界"),
    ("每日", "◆ 每日"),
    ("周", "◆ 每周（周一 00:00 UTC 统一重置）"),
    ("其它", "◆ 其它周期"),
)
# 英文版：键与 _TIMER_DESC 一致，值同为 (分段, 显示名, 轮换内容, 周常短名)
_TIMER_DESC_EN = {
    "夜灵平野": ("世界", "Plains of Eidolon", "Day / Night", ""),
    "奥布山谷": ("世界", "Orb Vallis", "Warm / Cold", ""),
    "魔胎之境": ("世界", "Cambion Drift", "Fass / Vome", ""),
    "双衍王境": ("世界", "Duviri", "Mood rotation", ""),
    "扎里曼派系": ("世界", "Zariman", "Corpus / Grineer", ""),
    "每日突击": ("每日", "Sortie", "Reset", ""),
    "钢铁侵蚀": ("每日", "Steel Path", "Incursions · daily reset", ""),
    "仲裁": ("每日", "Arbitration", "Hourly rotation", ""),
    "执刑官猎杀": ("周", "Archon Hunt", "Weekly reset", "Archon Hunt"),
    "深层科研": ("周", "Deep Archimedea", "Weekly reset", "Deep Archimedea"),
    "时光科研": ("周", "Temporal Archimedea", "Weekly reset", "Temporal Archimedea"),
    "沉沦之地": ("周", "Descendia", "21 floors weekly", "Descendia"),
    "钢铁回廊灵化": ("周", "Circuit Incarnon", "Weekly rotation", "Circuit Incarnon"),
    "1999日历": ("周", "1999 Calendar", "Season-end reset", "1999 Calendar"),
    "周常重置": ("周", "Weekly Reset", "Mon 00:00 UTC", ""),
    "信条元素加成": ("其它", "Tenet Weapons", "Every 4 days", ""),
    "终幕换批": ("其它", "Coda Weapons", "Every 4 days", ""),
    "虚空奸商": ("其它", "Void Trader", "Arrival", ""),
    "午夜电波": ("其它", "Nightwave", "Season ends", ""),
}
_TIMER_SECTIONS_EN = (
    ("世界", "◆ Open World"),
    ("每日", "◆ Daily"),
    ("周", "◆ Weekly (all reset Mon 00:00 UTC)"),
    ("其它", "◆ Other Cycles"),
)


def _timer_desc() -> dict:
    return _TIMER_DESC_EN if i18n.is_en() else _TIMER_DESC


def _timer_missing() -> str:
    return L(_TIMER_MISSING, "No data")


def _timer_when(name: str, cd: str) -> str:
    """「X 后抵达/开始」：中文沿用旧写法，英文 ``in 5m``。"""
    return L(f"{cd} 后{_TIMER_START_WORD.get(name, '开始')}", _in_en(cd))


def _timer_text(name: str, data: Optional[dict]) -> tuple[str, str]:
    """一项时效的计时文案 → ``(文案, 倒计时串)``；取不到数据时文案为「暂无数据」、倒计时串为空。

    ★ 2026-10-11：``activation`` 在未来的条目（虚空商人未到）优先显示
    「X 后抵达/开始」—— 旧实现先看 ``expiry``，把**离开时间**当成剩余时间。
    """
    now = _now()
    act = parse_iso((data or {}).get("activation", "")) if data else None
    if act is not None and act > now:
        cd = countdown(data["activation"])
        return _timer_when(name, cd), cd
    if data and "expiry" in data:
        cd = countdown(data["expiry"])
        return _left(cd), cd
    if data and "activation" in data:
        cd = countdown(data["activation"])
        return _timer_when(name, cd), cd
    return _timer_missing(), ""


def fmt_timers(timers: list[tuple[str, dict]]) -> tuple[str, list[str]]:
    """时效：所有周期/限时任务的最近截止时间汇总（每行注明是什么的时效）。

    ★ 铁律：**不允许静默吞行**（2026-09-25 立规）。取不到数据（源未下发、
    接口恒抛、字段缺失）时显式打「暂无数据」（解释放卡尾注脚）——旧写法
    ``if not data: continue`` 让「仲裁」「钢铁侵蚀」两行自 10o.io 停摆后
    从卡面里静默消失，用户以为看全了其实没有。合并只发生在**同一倒计时**
    的周常成员之间，且成员名仍逐个列在从属行里，不丢项。
    """
    S = "　"
    desc_tbl = _timer_desc()
    sections = _TIMER_SECTIONS_EN if i18n.is_en() else _TIMER_SECTIONS
    rows: dict[str, list[tuple[str, str, str, str, str]]] = {k: [] for k, _ in sections}
    for name, data in timers:
        sec, head, desc, short = desc_tbl.get(name, ("其它", name, "", ""))
        txt, cd = _timer_text(name, data)
        rows[sec].append((name, head, desc, txt, cd))
    missing = any(r[3] == _timer_missing() for items in rows.values() for r in items)
    lines: list[str] = []
    for sec, title in sections:
        items = rows[sec]
        if not items:
            continue
        lines.append(title)
        skip: set[str] = set()
        if sec == "周":
            # 以「周常重置」的倒计时为基准，合并同一刻重置的成员（成员名列在从属行）
            base = next((cd for n, _h, _d, _t, cd in items if n == "周常重置" and cd), "")
            if base:
                members = [
                    desc_tbl[n][3]
                    for n, _h, _d, _t, cd in items
                    if n != "周常重置" and cd == base and desc_tbl.get(n, ("",) * 4)[3]
                ]
                skip = {n for n, _h, _d, _t, cd in items if n != "周常重置" and cd == base}
                deep, temporal = desc_tbl["深层科研"][3], desc_tbl["时光科研"][3]
                if deep in members and temporal in members:
                    members[members.index(deep)] = L("深层/时光科研", "Deep/Temporal Archimedea")
                    members.remove(temporal)
                lines.append(L(f"· 本周重置{S}剩余 {base}", f"· Weekly Reset{S}{_left_en(base)}"))
                if members:
                    lines.append(f"{S}" + " · ".join(members))
                skip.add("周常重置")
        for n, head, desc, txt, _cd in items:
            if n in skip:
                continue
            lines.append(f"· {head}{S}{desc}{S}{txt}" if desc else f"· {head}{S}{txt}")
    title = L("时效总览", "Timers Overview")
    if not lines:
        return (title, [L("暂无数据", "No data")])
    if missing:
        lines.append(
            L(
                "※ 「暂无数据」= 数据源未下发该项计时；到点自动轮换/重置",
                '※ "No data" = the data source did not provide this timer; cycles rotate/reset automatically',
            )
        )
    else:
        lines.append(
            L(
                "※ 以上为各周期内容的当前剩余时间，到点自动轮换/重置",
                "※ Time left on each cycle above; cycles rotate/reset automatically",
            )
        )
    return (title, lines)


# ---------------------------------------------------------------------------
# 裂隙 / 突击 / 执刑官
# ---------------------------------------------------------------------------


# 遗物纪元的展示顺序（DE 的 VoidT1..T6），用于裂隙列表排序
_TIER_ORDER = {"Lith": 0, "Meso": 1, "Neo": 2, "Axi": 3, "Requiem": 4, "Omnia": 5, "Vanguard": 6}

# 单卡正文行数上限（渲染层 MAX_BODY_LINES = 90，留出标题/注脚余量）。
# 「不翻页」模式下超出只截断到该值并显式说明，绝不静默丢内容。
_ALL_ROWS_CAP = 86


# 裂隙/开核桃节点的敌对派系：取 DE ``solNodes`` 的 ``enemy``
# （Grineer / Corpus / Infested / Sentient / Orokin / The Murmur …）。
# 国际服官方简中**不翻译派系名**（Grineer / Corpus / Infested / Sentient /
# Corrupted 保留英文，与游戏内客户端一致）；有官方中文译名的是
# 奥罗金 / 低语者 / 合一众 / 炽蛇军 / 科腐者（均为 DE 游戏内官方文本）。
# Tenno / Crossfire / Duviri 这类不是敌对派系，不显示。
_FACTION_ZH = {
    "Orokin": "奥罗金",
    "The Murmur": "低语者",
    "Infestation": "Infested",
    "Narmer": "合一众",
    "Scaldra": "炽蛇军",
    "Techrot": "科腐者",
}
_FACTION_HIDE = {"", "?", "Tenno", "Crossfire", "Duviri", "Anarch"}


def _fissure_faction(f: dict) -> str:
    """裂隙行的派系显示名（无数据/非敌对派系时返回空串）。"""
    raw = (f.get("enemy") or "").strip()
    if raw in _FACTION_HIDE:
        return ""
    if i18n.is_en():
        return "Infested" if raw == "Infestation" else raw
    return _FACTION_ZH.get(raw, raw)


def fmt_fissures(
    fissures: Iterable[dict], flt=None, page: int = 1, page_size: int = 12, all_rows: bool = False
) -> tuple[str, list[str]]:
    """虚空裂隙列表：按「纪元 → 剩余时间」排序，标题行给出排序口径。

    旧实现有两个问题：① 每行尾部跟一串 ``★``（``tierNum`` 直接当星数），
    这个「星级」既非游戏内概念也无任何含义；② 只按 activation 排序，
    玩家看不出规律。现在改成显式的「古纪→前纪→中纪→后纪→安魂→全能」，
    同纪元内按剩余时间升序（快结束的排前面）。

    ``all_rows=True``：**不翻页**，一次把全部裂隙放进同一张卡（维护者要求
    「别做翻页了，直接那几页内容都放一张截图上」）。实测同时在册约 34 条，
    远低于渲染层 90 行上限；万一超过，也只截到上限并显式说明，不静默丢内容。
    """
    flt = flt or (lambda f: True)
    act = [
        f
        for f in fissures
        if parse_iso(f.get("expiry", "")) and parse_iso(f["expiry"]) > _now() and flt(f)
    ]
    act.sort(key=lambda f: (_TIER_ORDER.get(f.get("tier", ""), 99), f.get("expiry", "")))
    total = len(act)
    extra = ""
    if all_rows:
        pages, page = 1, 1
        chunk = act[:_ALL_ROWS_CAP]
        if total > _ALL_ROWS_CAP:
            extra = L(
                f"※ 共 {total} 条，本卡只列前 {_ALL_ROWS_CAP} 条（按上面口径排序）",
                f"※ {total} total; this card lists the first {_ALL_ROWS_CAP} (sorted as above)",
            )
    else:
        pages = max(1, (total + page_size - 1) // page_size)
        page = max(1, min(page, pages))
        chunk = act[(page - 1) * page_size : page * page_size]
    S = "　"
    rows = []
    for f in chunk:
        # ★ 2026-10-11 第二步改版：单张对齐表「纪元　类型　节点　派系　标记　剩余」——
        #   纪元走芯片，派系灰字，「钢铁 / 九重天」由渲染层画成小号标记；
        #   不再用序号（序号列把折行孤字顶到序号下方），也不再用「 · 」串联。
        #   某列没有值就留空格，保证各行的列位一致。
        tier = f.get("tier", "")
        chip = f"[{tier_cn(tier)}]" if tier and tier != "?" else ""
        mt_raw = f.get("missionType", "")
        # 九重天风暴的类型来自节点表英文名（Skirmish / Volatile …），与「九重天」卡同一张译表
        mtype = (
            _RAILJACK_TYPE_CN.get((mt_raw or "").lower(), "")
            if f.get("isStorm") and not i18n.is_en()
            else ""
        ) or mission_cn(mt_raw)
        if mtype == "?":
            mtype = ""
        tags = " ".join(
            t
            for t in (
                L("钢铁", "Steel Path") if f.get("isHard") else "",
                L("九重天", "Void Storms") if f.get("isStorm") else "",
            )
            if t
        )
        rows.append(
            f"· {chip}{S}{mtype}{S}{f.get('node') or ''}{S}{_fissure_faction(f)}{S}{tags}{S}"
            + _left(countdown(f["expiry"]), "剩")
        )
    if all_rows:
        head = L(f"虚空裂隙（共{total}条）", f"Void Fissures ({total} total)")
    else:
        head = L(
            f"虚空裂隙（第{page}/{pages}页，共{total}条）",
            f"Void Fissures (Page {page}/{pages}, {total} total)",
        )
    if not total:
        # ★ 2026-10-11：0 结果时旧实现只剩一行「※ 按纪元…排序」（`lines or […]` 永远非空），
        #   「没有匹配」的提示从不出现。
        return (
            head,
            [
                L(
                    "◆ 结果：当前筛选条件下没有进行中的裂隙",
                    "◆ Result: No active fissures match the current filters",
                )
            ],
        )
    n_hard = sum(1 for f in act if f.get("isHard"))
    n_storm = sum(1 for f in act if f.get("isStorm"))
    summ = L(f"{total} 条", f"{total}")
    if n_hard or n_storm:
        _parts = " · ".join(
            x
            for x in (
                L(f"钢铁 {n_hard}", f"Steel Path {n_hard}") if n_hard else "",
                L(f"九重天 {n_storm}", f"Void Storms {n_storm}") if n_storm else "",
            )
            if x
        )
        summ += L("（" + _parts + "）", " (" + _parts + ")")
    nxt = act[0]
    for f in act:
        if f.get("expiry", "") < nxt.get("expiry", ""):
            nxt = f
    nxt_mt = (
        _RAILJACK_TYPE_CN.get((nxt.get("missionType") or "").lower(), "")
        if nxt.get("isStorm") and not i18n.is_en()
        else ""
    ) or mission_cn(nxt.get("missionType", ""))
    lines = [
        L(f"◆ 进行中：{summ}", f"◆ Active: {summ}"),
        L("◆ 最近到期：", "◆ Next to expire: ")
        + f"{nxt.get('node') or '?'} {'' if nxt_mt == '?' else nxt_mt}　"
        + _left(countdown(nxt["expiry"]), "剩"),
        L("◆ 裂隙列表（按纪元排序）", "◆ Fissure List (by tier)"),
        L(
            f"{S}纪元{S}类型{S}节点{S}派系{S}标记",
            f"{S}Tier{S}Type{S}Node{S}Faction{S}Tags",
        ),
        *rows,
        L(
            "※ 按纪元（古纪→前纪→中纪→后纪→安魂→全能）排序，同纪元内剩余时间少的在前",
            "※ Sorted by tier (Lith→Meso→Neo→Axi→Requiem→Omnia); within a tier, least time left first",
        ),
    ]
    if extra:
        lines.append(extra)
    return (head, lines)


_MODIFIER_CN: dict[str, str] = {}


def _sortie_modifier_cn(name: str) -> str:
    """突击/执刑官限制条件中文名（覆盖 DE sortieData 全部 30 种 modifierTypes）。

    英文模式原样返回 DE 官方英文名。
    """
    if i18n.is_en():
        return name
    if not _MODIFIER_CN:
        _MODIFIER_CN.update(
            {
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
                "Void Electricity": "虚空电击",
                "Toxic Rain": "毒素之雨",
                "Magnetic Field": "磁场",
                "Corrosive Warfare": "腐蚀战",
                "Energy Overload": "能量过载",
                "Elemental Enhancement": "元素强化",
                "Weather Control": "天气控制",
                "Night Mode": "黑暗模式",
                "Kill Steal": "击杀窃取",
                "Machine Enemies": "机械敌人",
                "Bullet Attractor": "子弹吸引",
                "Viral Storms": "病毒风暴",
                "Overcharged Defenses": "防御过载",
                "Dense Atmosphere": "稠密大气",
                "Weapon Sharing": "武器共享",
                "Permanent Double Damage": "永久双倍伤害",
                "Low Gravity": "低重力",
                "High Voltage": "高压",
            }
        )
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

# 执刑官 code -> (碎片颜色, 官方物品名)。
# 官方叫**源力石**（``archoncrystalamar`` → 深红执刑官源力石）。
# ★ 2026-10-11：键是 DE 的执刑官 **code**（``SORTIE_BOSS_NIRA`` -> ``Nira``，由
#   de_worldstate.archon_code 产出、存 ``bossCode``），按 code 精确匹配 ——
#   卡面首领名已改官方简中（执刑官混沌蛇主），不再含英文名可供子串匹配。
_ARCHON_SHARD = {
    "Amar": ("红", "深红执刑官源力石"),
    "Nira": ("黄", "琥珀执刑官源力石"),
    "Boreal": ("蓝", "蔚蓝执刑官源力石"),
}
_ARCHON_SHARD_EN = {
    "Amar": ("Red", "Crimson Archon Shard"),
    "Nira": ("Yellow", "Amber Archon Shard"),
    "Boreal": ("Blue", "Azure Archon Shard"),
}


# 英文模式：Store Item 术语 -> 游戏内英文名
_REWARD_NAME_EN = {
    "Resource Drop Chance Booster Store Item": "3 Day Resource Drop Chance Booster",
    "Mod Drop Chance Booster Store Item": "3 Day Mod Drop Chance Booster",
    "Resource Booster Store Item": "3 Day Resource Booster",
    "Affinity Booster Store Item": "3 Day Affinity Booster",
    "Credit Booster Store Item": "3 Day Credit Booster",
}


def _reward_cn(name: str) -> str:
    """把奖励名里的英文 Store Item 术语替换成中文（英文模式换成游戏内英文名）。"""
    if not name:
        return name
    for en, cn in (_REWARD_NAME_EN if i18n.is_en() else _REWARD_NAME_CN).items():
        if en in name:
            return name.replace(en, cn)
    # 「2000 x Kuva」这类 WM 风格的写法也归一化一下
    m = re.match(r"^([\d,]+)\s*x\s*(.+)$", name.strip())
    if m:
        return f"{m.group(2)} ×{m.group(1)}"
    return name


# 用于突击/执刑官奖励行的中文数字（「6000 x Kuva」-> 「赤毒 ×6000」）
_RES_ALIAS = {"Kuva": "赤毒", "Endo": "内融核心", "Credits": "现金"}


# 突击奖励池（core/data/de/sortie_rewards.json 只有简中名）的英文名，键为该表的中文名
_SORTIE_REWARD_EN = {
    "阿耶檀识 Anasa 塑像": "Ayatan Anasa Sculpture",
    "内融核心捆包": "Endo",
    "赤毒": "Kuva",
    "近战裂罅 Mod": "Melee Riven Mod",
    "步枪裂罅 Mod": "Rifle Riven Mod",
    "手枪裂罅 Mod": "Pistol Riven Mod",
    "3 天经验值加成": "3 Day Affinity Booster",
    "3 天资源掉落几率加成": "3 Day Resource Drop Chance Booster",
    "3 天 Mod 掉落几率加成": "3 Day Mod Drop Chance Booster",
    "Forma": "Forma",
    "战甲特殊功能槽连接器": "Exilus Warframe Adapter",
    "奥罗金反应堆蓝图": "Orokin Reactor Blueprint",
    "奥罗金催化剂蓝图": "Orokin Catalyst Blueprint",
    "霰弹枪裂罅 Mod": "Shotgun Riven Mod",
    "Zaw 裂罅 Mod": "Zaw Riven Mod",
    "组合枪裂罅 Mod": "Kitgun Riven Mod",
    "传说核心": "Legendary Core",
}
_SORTIE_RARITY_EN = {"普通": "Common", "罕见": "Uncommon", "稀有": "Rare", "传说": "Legendary"}


def _res_name_cn(s: str) -> str:
    if i18n.is_en():
        return _reward_cn(_SORTIE_REWARD_EN.get(s, s))
    out = _reward_cn(s)
    for en, cn in _RES_ALIAS.items():
        out = out.replace(en, cn)
    return out


def _sortie_reward_lines(limit: int = 8) -> tuple[list[str], list[str]]:
    """突击奖励池（完成 3 个阶段后抽 1 件）→ ``(表格行, 注脚行)``。"""
    data = sortie_rewards()
    tiers = data.get("tiers") or {}
    rows = tiers.get("0") or next(iter(tiers.values()), [])
    if not rows:
        return [], []
    S = "　"
    out = [
        L("◆ 完成后奖励（从下列随机 1 件）", "◆ Completion Reward (1 random item from the list)"),
        L(f"{S}奖励{S}稀有度{S}几率", f"{S}Reward{S}Rarity{S}Chance"),
    ]
    for r in rows[:limit]:
        cnt = r.get("count") or 1
        name = _res_name_cn(r["name"]) + (f" ×{cnt}" if cnt and cnt > 1 else "")
        if i18n.is_en() and r["name"] == "内融核心捆包":
            # 官方英文无此捆包名；wiki Sortie 奖励表写总量「4,000 Endo」（50 包 × 每包 80）
            name = f"{cnt * 80:,} Endo"
        rarity = r.get("rarity", "")
        if i18n.is_en():
            rarity = _SORTIE_RARITY_EN.get(rarity, rarity)
        out.append(f"· {name}{S}{rarity}{S}{r.get('chance', '')}%")
    notes = []
    if len(rows) > limit:
        notes.append(
            L(
                f"※ 另有 {len(rows) - limit} 项未列出（其余为加成类与塑像）",
                f"※ {len(rows) - limit} more not listed (the rest are boosters and statues)",
            )
        )
    return out, notes


def fmt_sortie(sortie: Optional[dict]) -> tuple[str, list[str]]:
    """每日突击：首领 / 三阶段任务表 / 奖励池（2026-10-11 第二步改版：答案先行 + 对齐表）。"""
    title = L("每日突击", "Sortie")
    if not sortie:
        return (title, [L("数据暂不可用", "Data unavailable")])
    S = "　"
    lines = [
        L("◆ 首领：", "◆ Boss: ")
        + f"{sortie.get('boss', '?')}{S}"
        + _left(countdown(sortie.get("expiry", "")))
    ]
    rows = []
    for i, v in enumerate(sortie.get("variants", []) or [], 1):
        lim = _sortie_modifier_cn(v.get("modifier", ""))
        # 「武器限制：仅限霰弹枪」本身已带类别前缀，避免再套一层「限制：限制：」
        if i18n.is_en():
            lim = re.sub(
                r"^(?:Weapon Restriction|Enemy (?:Physical |Elemental )?Enhancement): ", "", lim
            )
        else:
            lim = re.sub(r"^(?:武器限制|敌人(?:物理|元素)?强化)：", "", lim)
        rows.append(
            f"· {i}{S}{v.get('node', '?')}{S}{mission_cn(v.get('missionType', ''))}{S}"
            f"{lim or L('无', 'None')}"
        )
    if rows:
        lines += [
            L("◆ 三阶段任务", "◆ Three Stages"),
            L(f"{S}阶段{S}节点{S}类型{S}限制", f"{S}Stage{S}Node{S}Type{S}Modifier"),
            *rows,
        ]
    rw, notes = _sortie_reward_lines()
    return (title, lines + rw + notes)


def fmt_archon(archon: Optional[dict]) -> tuple[str, list[str]]:
    """执刑官猎杀：3 阶段节点 + 敌人等级 + 限制条件 + 源力石奖励。

    ★ 2026-10-11：阶段等级取 wiki `Archon Hunt` 固定档位（130-135 / 135-140 / 145-150，
    见 de_worldstate.ARCHON_STAGE_LEVELS），不再套用节点自身的星图等级。
    第二步改版：本周首领与奖励先行，三阶段做成「阶段 / 节点 / 类型 / 等级」表。
    """
    title = L("执刑官猎杀", "Archon Hunt")
    if not archon:
        return (title, [L("数据暂不可用", "Data unavailable")])
    S = "　"
    boss = archon.get("boss", "?")
    code = archon.get("bossCode") or ""
    if i18n.is_en():
        color, shard = _ARCHON_SHARD_EN.get(code, ("?", "Archon Shard"))
    else:
        color, shard = _ARCHON_SHARD.get(code, ("?", "执刑官源力石"))
    lines = [
        L("◆ 本周执刑官：", "◆ This Week's Archon: ")
        + f"{boss}{S}"
        + _left(countdown(archon.get("expiry", ""))),
        L(
            f"◆ 第 3 阶段奖励：{shard}（{color}）",
            f"◆ Stage 3 Reward: {shard} ({color})",
        ),
    ]
    rows = []
    has_lv = any(v.get("level") for v in archon.get("variants") or [])
    has_lim = False
    for i, v in enumerate(archon.get("variants", []) or [], 1):
        lim = _sortie_modifier_cn(v.get("modifier", ""))
        has_lim = has_lim or bool(lim)
        cells = [str(i), v.get("node", "?"), mission_cn(v.get("missionType", ""))]
        if has_lv:
            cells.append(v.get("level") or "—")
        if lim:
            cells.append(L(f"限制：{lim}", f"Modifier: {lim}"))
        rows.append("· " + S.join(cells))
    if rows:
        head = L(
            ["阶段", "节点", "类型"] + (["等级"] if has_lv else []),
            ["Stage", "Node", "Type"] + (["Level"] if has_lv else []),
        )
        lines += [L("◆ 三阶段任务", "◆ Three Stages"), S + S.join(head), *rows]
    lines.append(
        L(
            "※ 无突击式负面限制，3 个阶段按顺序推进；每周首次完成固定给 1 个对应颜色的源力石",
            "※ No Sortie-style modifiers; the 3 stages run in order; the first clear each week always gives 1 shard of the matching color",
        )
    )
    if has_lv:
        lines.append(
            L(
                "※ 敌人等级取自 wiki（普通难度）；钢铁之路档 wiki 未列，不作推算",
                "※ Enemy levels are from the wiki (normal difficulty); the wiki lists no Steel Path tier, so none is estimated",
            )
        )
    lines.append(
        L(
            "※ 源力石有几率升级为 Tau 强化版，用于兑换执刑官 MOD 与赋能",
            "※ Shards can be upgraded to Tau versions, used to buy Archon mods and Arcanes",
        )
    )
    return (title, lines)


# ---------------------------------------------------------------------------
# 商人与兑换
# ---------------------------------------------------------------------------


def _baro_cash(credits) -> str:
    """现金展示：≥1 万折叠成「N 万」，小数去尾零（250000 → 25万现金）。"""
    try:
        c = int(credits)
    except (TypeError, ValueError):
        return L("?现金", "? Credits")
    if i18n.is_en():
        if c >= 1000:
            return f"{f'{c / 1000:.1f}'.rstrip('0').rstrip('.')}k Credits"
        return f"{c} Credits"
    if c >= 10000:
        s = f"{c / 10000:.1f}".rstrip("0").rstrip(".")
        return f"{s}万现金"
    return f"{c}现金"


_BARO_GROUP_EN = {
    "武器": "Weapons",
    "MOD": "Mods",
    "外观": "Cosmetics",
    "装饰": "Decor",
    "遗物": "Relics",
    "消耗品": "Consumables",
    "礼包": "Bundles",
    "其他": "Other",
}


def _baro_group(g: str) -> str:
    """商品类别显示名（baro.py 的类别键是中文；英文模式换成英文）。"""
    return _BARO_GROUP_EN.get(g, g) if i18n.is_en() else g


def fmt_void_trader(trader: Optional[dict]) -> tuple[str, list[str]]:
    """虚空商人（当期在售）。

    排版（2026-09-18 维护者要求「借鉴别人家的设计」）：**两列**。
    每行左右各一个商品，共 6 列（名称 / 杜卡德 / 现金 ×2），交给渲染层的
    表格列对齐（_table_mode）统一列位。杜卡德用「N杜」缩写、现金用「N万现金」。

    ★ 2026-10-11 第二步改版：开头先给答案（状态 / 地点 / 离开或抵达倒计时 / 件数），
    货单加表头；颜色：名称暖白（默认）、杜卡德金色、现金青色（render 的 token 规则）。

    ★ 2026-10-16 第三版：按商品类别分组（武器 / MOD / 外观 / 装饰 / 遗物 / 消耗品 / 礼包 / 其他），
    每行两件同组商品、行首彩色类别芯片；类别由 DE 商品路径判定（core/baro.py::group_of_store）。
    """
    title = L("虚空商人", "Void Trader")
    if not trader:
        return (title, [L("数据暂不可用", "Data unavailable")])
    S = "　"
    active = trader.get("active", False)
    loc = trader.get("location", "?")
    lines = []
    if active:
        trader_name = trader.get("character", "Baro Ki'Teer")
        lines += [
            L(
                f"◆ 状态：{trader_name} 已抵达 {loc}",
                f"◆ Status: {trader_name} has arrived at {loc}",
            ),
            L("◆ 离开：", "◆ Departs: ") + _left(countdown(trader.get("expiry", ""))),
        ]
        inv = trader.get("inventory") or []
        if not inv:
            lines.append(
                L("◆ 货单：同步中，请稍后再查", "◆ Inventory: syncing, please check again later")
            )
            return (title, lines)
        # ★ 2026-10-16 维护者要求「同种类的商品放一块，用异色标记，像赏金那样」：
        #   按 DE 商品路径分组（baro.group_of_store），固定组序；每组先一行彩色类别芯片
        #   （render 的 GROUP_CHIP_COLOR），下面每行两件**同组**商品，奇数组末行单件，绝不跨组拼行。
        #   ⚠ 芯片**单独占一行**、不做成每行行首的一列：实测加一列后 55 件货单表宽超过渲染层安全阀
        #   （1322），列对齐被整体放弃、长名折行；单格的芯片行只占第一列，而第一列本来就被商品名撑宽。
        from .baro import STORE_GROUP_ORDER, group_of_store

        groups: dict[str, list] = {g: [] for g in STORE_GROUP_ORDER}
        for it in inv:
            groups.setdefault(group_of_store(it.get("store") or ""), []).append(it)
        counts = " · ".join(f"{_baro_group(g)} {len(v)}" for g, v in groups.items() if v)
        lines.append(
            L(f"◆ 货单：共 {len(inv)} 件（{counts}）", f"◆ Inventory: {len(inv)} items ({counts})")
        )

        def cell(it: dict) -> str:
            name = it.get("item") or "?"
            if i18n.is_en():
                return f"{name}{S}{it.get('ducats', '?')} Ducats{S}{_baro_cash(it.get('credits'))}"
            return f"{name}{S}{it.get('ducats', '?')}杜{S}{_baro_cash(it.get('credits'))}"

        lines.append(L("◆ 在售货单", "◆ Items for Sale"))
        _h = L(f"{S}商品{S}杜卡德{S}现金", f"{S}Item{S}Ducats{S}Credits")
        lines.append(_h + (_h if len(inv) > 1 else ""))
        for g, items in groups.items():
            if items:
                lines.append(f"· [{_baro_group(g)}]")
            for i in range(0, len(items), 2):
                row = cell(items[i])
                if i + 1 < len(items):
                    row += S + cell(items[i + 1])
                lines.append("· " + row)
        lines.append(
            L(
                "※ 金色＝杜卡德 · 青色＝现金 · 彩色标签＝商品类别",
                "※ Gold = Ducats · Cyan = Credits · Colored tag = item category",
            )
        )
    else:
        lines += [
            L("◆ 状态：尚未抵达", "◆ Status: Not arrived yet"),
            L(f"◆ 抵达地点：{loc}", f"◆ Arrival location: {loc}"),
            L("◆ 距抵达：", "◆ Arrives: ")
            + L(
                f"剩余 {countdown(trader.get('activation', ''))}",
                _in_en(countdown(trader.get("activation", ""))),
            ),
        ]
    return (title, lines)


def fmt_baro_predict(
    rows: list[dict],
    next_est: str = "",
    visits: int = 0,
    last: str = "",
    names_zh: Optional[dict] = None,
    snapshot: str = "",
    missed: int = 0,
) -> tuple[str, list[str]]:
    """奸商下期库存「预测」（**统计推测，非官方**）。

    ★ 2026-10-11：卡面注明**历史快照日期**与快照后未入库的到访次数（静默次数已同步加上）。
    ★ 2026-10-11 第二步改版：开头先给「下次 / 上次到访 + 样本」，候选做成
    「[大类] 名称　杜卡德」+ 缩进依据行（成对并入同一条斑马纹），注脚压到 2 条。

    汉化：物品名走 DE 官方双语词表（`core/data/baro_names_zh.json`），
    词表里确实没有的才回落英文原名；大类（MOD/武器/装饰…）全中文，渲染层做成配色芯片。

    口径（卡上也写）：`回归度 = 已静默次数 ÷ 该物品历史平均上架间隔`，
    越接近 1 越「该回来了」；静默超过历史最大间隔 2 倍的已剔除（那多半是停售）。
    """
    if not rows:
        return (
            L("奸商下期预测", "Void Trader Next Visit Forecast"),
            [
                L(
                    "预测数据不可用（缺 core/data/baro_history.json）",
                    "Forecast data unavailable (core/data/baro_history.json is missing)",
                ),
                L(
                    "刷新方式：scripts/build_baro_history.py（需经 FlareSolverr 抓 wiki）",
                    "Refresh with scripts/build_baro_history.py (scrapes the wiki via FlareSolverr)",
                ),
            ],
        )
    zh = names_zh or {}
    S = "　"

    def nm(r: dict) -> str:
        if i18n.is_en():
            return r["name"]
        return zh.get(r["name"]) or r.get("name_cn") or r["name"]

    lines: list[str] = []
    if next_est:
        lines.append(L(f"◆ 下次预计到访：{next_est}", f"◆ Next expected visit: {next_est}"))
    head = []
    if last:
        head.append(L(f"上次 {last}", f"Last {last}"))
    if visits:
        head.append(L(f"样本 {visits} 次到访", f"Sample: {visits} visits"))
    if head:
        lines.append(L("◆ 历史：", "◆ History: ") + " ｜ ".join(head))
    lines.append(
        L("◆ 候选库存（按回归度排序，推测）", "◆ Candidate Inventory (by return score, estimate)")
    )
    for r in rows:
        grp = _baro_group(r.get("group") or "其他")
        cost = L(f"{r['ducats']} 杜卡德", f"{r['ducats']} Ducats") if r.get("ducats") else "—"
        lines.append(f"· [{grp}]{S}{nm(r)}{S}{cost}")
        lines.append(
            L(
                f"{S}上次 {r['last']} ｜ 已静默 {r['silent']} 次"
                f"（均隔 {r['gap']}）｜ 回归度 {r['score']}",
                f"{S}Last {r['last']} ｜ Silent for {r['silent']} visits"
                f" (avg gap {r['gap']}) ｜ Return score {r['score']}",
            )
        )
    lines.append(
        L(
            "※ 统计推测，不是官方预测（DE 从不公布下期库存）；"
            "口径：已静默次数 ÷ 该物品历史平均上架间隔，越接近 1 越可能回归",
            "※ Statistical estimate, not an official forecast (DE never announces the next inventory); "
            "score = visits silent ÷ the item's average gap between listings; the closer to 1, the more likely it returns",
        )
    )
    if snapshot:
        tail = L(
            f"；其后 {missed} 次到访未入库，已按「未上架」计入静默次数" if missed else "",
            f"; {missed} later visits are not recorded and count as silent" if missed else "",
        )
        lines.append(
            L(
                f"※ 历史记录快照：{snapshot}（wiki Module:Baro/data）{tail}",
                f"※ History snapshot: {snapshot} (wiki Module:Baro/data){tail}",
            )
        )
    return (L("奸商下期预测（推测）", "Void Trader Next Visit Forecast (Estimate)"), lines)


def _deal_stock(d: dict) -> str:
    """每日特惠库存：剩余量 / 总量（DE 下发 AmountTotal 与 AmountSold）。

    ★ 2026-10-11：旧写法只印总量「库存200」，看不出还剩多少（已售字段一直有，没用上）。
    """
    total, sold = d.get("total"), d.get("sold")
    try:
        return L(
            f"余量{max(0, int(total) - int(sold or 0))}/{int(total)}",
            f"Stock {max(0, int(total) - int(sold or 0))}/{int(total)}",
        )
    except (TypeError, ValueError):
        return L(
            f"库存{total if total is not None else '?'}",
            f"Total {total if total is not None else '?'}",
        )


def fmt_daily_deals(deals: Iterable[dict]) -> tuple[str, list[str]]:
    """每日特惠：答案先行（件数）+ 「商品 / 折后 / 原价 / 余量」表，倒计时右对齐。"""
    S = "　"
    ds = list(deals or [])
    title = L("每日特惠", "Darvo's Daily Deal")
    if not ds:
        return (title, [L("◆ 今日特惠：暂无", "◆ Today's deals: None")])
    lines = [
        L(f"◆ 今日特惠：{len(ds)} 件", f"◆ Today's deals: {len(ds)}"),
        L("◆ 特惠商品", "◆ Deal Items"),
        L(f"{S}商品{S}折后{S}原价{S}余量", f"{S}Item{S}Sale{S}Original{S}Stock"),
    ]
    for d in ds:
        lines.append(
            L(
                f"· {d.get('item', '?')}{S}{d.get('salePrice', '?')}白金{S}"
                f"原价{d.get('originalPrice', '?')}{S}{_deal_stock(d)}{S}",
                f"· {d.get('item', '?')}{S}{d.get('salePrice', '?')} Platinum{S}"
                f"Was {d.get('originalPrice', '?')}{S}{_deal_stock(d)}{S}",
            )
            + _left(countdown(d.get("expiry", "")), "剩")
        )
    return (title, lines)


def fmt_steel_path(sp: Optional[dict]) -> tuple[str, list[str]]:
    if not sp:
        return (
            L("钢铁之路", "Steel Path"),
            [
                L(
                    "钢铁之路轮换为外部数据源，当前数据模式下不可用。",
                    "Steel Path rotation comes from an external data source and is unavailable in the current data mode.",
                ),
                L(
                    "可用「裂隙 钢铁」查看钢铁之路裂隙。",
                    'Use "fissure steel" to view Steel Path fissures.',
                ),
            ],
        )
    lines = []
    if sp.get("currentReward"):
        lines.append(
            L(
                f"当前侵袭奖励：{sp['currentReward']}　剩余 {sp.get('remaining', '?')}",
                f"Current incursion reward: {sp['currentReward']}　{sp.get('remaining', '?')} left",
            )
        )
    inc = sp.get("incursions") or []
    for i, it in enumerate(inc[:8], 1):
        lines.append(f"{i}. {it.get('node', '?')} · {mission_cn(it.get('missionType', ''))}")
    return (
        L("钢铁之路轮换", "Steel Path Rotation"),
        lines or [L("暂无侵袭数据", "No incursion data")],
    )


# ---------------------------------------------------------------------------
# 警报 / 入侵 / 仲裁 / 赤毒 / 新闻 / 电波
# ---------------------------------------------------------------------------


_STEEL_SHOP_CACHE: Optional[dict] = None
_PALLADINO_SHOP_CACHE: Optional[dict] = None


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


def palladino_shop() -> dict:
    """Palladino 裂罅碎块兑换表（core/data/de/palladino_shop.json）。"""
    global _PALLADINO_SHOP_CACHE
    if _PALLADINO_SHOP_CACHE is None:
        try:
            p = Path(__file__).resolve().parent / "data" / "de" / "palladino_shop.json"
            _PALLADINO_SHOP_CACHE = json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            _PALLADINO_SHOP_CACHE = {}
    return _PALLADINO_SHOP_CACHE


def utc_today() -> str:
    """今天的 UTC 日期（YYYY-MM-DD）——日历推送与卡面同钟（formatters._now）。"""
    return _now().astimezone(timezone.utc).date().isoformat()


def rotation_window(data: dict) -> tuple[int, Optional[str]]:
    """轮换数据（rotations.json 单项）的**窗口序号**与下次换轮 ISO。

    ★ C4（2026-10-03）：push 的换轮点检测与卡面（rotations.py::_rotation_lines）
    同一套锚点算法 —— epoch 起算 floor((now-epoch)/period)。返回
    (passed, next_iso)；数据缺失 (0, None)。内容映射（anchor_week/anchor_idx
    取模）由调用方按各自模式做。
    """
    try:
        epoch = datetime.fromisoformat(data.get("epoch") or "")
        hours = int(data.get("period_hours") or 168)
        passed = int((_now() - epoch).total_seconds() // 3600 // max(1, hours))
        nxt = (epoch + timedelta(hours=(passed + 1) * hours)).isoformat()
    except Exception:  # noqa: BLE001
        return 0, None
    return passed, nxt


def weekly_reset_info(data: dict) -> tuple[int, Optional[str]]:
    """通用「周一 00:00 UTC」周周期号与下次重置 ISO；数据缺失 (0, None)。

    ★ 2026-10-03（A3）：碎银兑换（Palladino）用 —— 其库存常驻、**无轮换**，
    只有每周限购在周一重置；卡面倒计时与「蹲 碎银兑换」换周检测**同源此处**。
    """
    rot = data.get("rotation") or {}
    try:
        epoch = datetime.fromisoformat(rot.get("epoch") or "")
        hours = int(rot.get("period_hours") or 168)
        n = int((_now() - epoch).total_seconds() // 3600 // max(1, hours))
        nxt = (epoch + timedelta(hours=(n + 1) * hours)).isoformat()
    except Exception:  # noqa: BLE001
        return 0, None
    return n, nxt


def steel_rotation_index(data: dict) -> tuple[int, Optional[str]]:
    """钢精兑换当前轮换序号（0 基）与下次轮换时间 ISO；数据缺失 (0, None)。

    ★ 2026-10-03（A2）：push 的「换轮点检测」与卡面推算**同源此处** ——
    别再写第二份推算。轮换锚点见 data["rotation"]（每周一 00:00 UTC）。
    """
    weekly = data.get("weekly") or []
    if not weekly:
        return 0, None
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
        return 0, None
    return idx, nxt_reset


# 钢精 / 碎银兑换表（core/data/de/*.json）只有简中名；英文模式按中文原名查此表，
# 查不到就原样显示（不猜）。英文名取自 DE languages_en 与 wiki。
_SHOP_NAME_EN = {
    "裂罅破解器（Veiled Riven Cipher，每周限购 1）": "Veiled Riven Cipher (1 per week)",
    "主武器赋能适配器": "Primary Arcane Adapter",
    "副武器赋能适配器": "Secondary Arcane Adapter",
    "架式 Forma 蓝图": "Stance Forma Blueprint",
    "10,000 赤毒（每周限购 25）": "10,000 Kuva (25 per week)",
    "遗物组合包 · 3 个随机遗物（每周限购 25）": "Relic Pack (3x Void Relics) (25 per week)",
    "比沙莫护肩套装（Bishamo Pauldrons）": "Bishamo Pauldrons Blueprint Set",
    "比沙莫胸甲套装（Bishamo Cuirass）": "Bishamo Cuirass Blueprint Set",
    "比沙莫头盔套装（Bishamo Helmet）": "Bishamo Helmet Blueprint Set",
    "比沙莫护胫套装（Bishamo Greaves）": "Bishamo Greaves Blueprint Set",
    "三轨幻纹": "Trio Orbit Ephemera",
    "颅骨幻纹（Crania Ephemera）": "Crania Ephemera",
    "制衡（Counterbalance，霰弹枪减后坐）": "Counterbalance (Shotgun Recoil)",
    "Teshin 摇头娃娃": "Noggle Statue - Teshin",
    "奥罗金茶具": "Orokin Tea Set",
    "Gauss 战斗姿态浮印": "Gauss in Action Glyph",
    "Grendel 战斗姿态浮印": "Grendel in Action Glyph",
    "Protea 战斗姿态浮印": "Protea In Action Glyph",
    "Xaku 战斗姿态浮印": "Xaku in Action Glyph",
    "《老师》地球场景": "The Teacher Earth Scene",
    "《老师》道场场景": "The Teacher Dojo Scene",
    "《老师》反应堆场景": "The Teacher Reactor Scene",
    "《老师》飞船场景": "The Teacher Ship Scene",
    "《老师》曲目": "The Teacher",  # /Lotus/Language/OldPeace/TheTeacherSongName
    "Umbra Forma 蓝图": "Umbra Forma Blueprint",
    "50,000 赤毒": "50,000 Kuva",
    "组合枪裂罅 Mod": "Kitgun Riven Mod",
    "3 × Forma": "3 × Forma",
    "Zaw 裂罅 Mod": "Zaw Riven Mod",
    "30,000 内融核心": "30,000 Endo",
    "步枪裂罅 Mod": "Rifle Riven Mod",
    "霰弹枪裂罅 Mod": "Shotgun Riven Mod",
    "150,000 现金": "150,000 Credits",
    "6,000 内融核心": "6,000 Endo",
    "35,000 赤毒": "35,000 Kuva",
    "安魂遗物 I": "Requiem I Relic",
    "安魂遗物 II": "Requiem II Relic",
    "安魂遗物 III": "Requiem III Relic",
    "安魂遗物 IV": "Requiem IV Relic",
    "安魂通牒": "Requiem Ultimatum",
    "裂罅 Mod": "Riven Mod",
    "裂罅转化器": "Riven Transmuter",
    "裂罅破解器": "Veiled Riven Cipher",
    "钢铁守望场景": "Iron Wake Scene",
    "Rell 的 Donda": "Rell's Donda",
    "Rell 的愤怒表情卡牌": "Rell's Anger Emotile",
    "Rell 的焦虑表情卡牌": "Rell's Anxiety Emotile",
    "Rell 的无聊表情卡牌": "Rell's Boredom Emotile",
    "Rell 的困惑表情卡牌": "Rell's Confusion Emotile",
    "Rell 的厌恶表情卡牌": "Rell's Disgust Emotile",
    "Rell 的尴尬表情卡牌": "Rell's Embarrassment Emotile",
    "Rell 的兴奋表情卡牌": "Rell's Excitement Emotile",
    "Rell 的疲惫表情卡牌": "Rell's Fatigue Emotile",
    "Rell 的恐惧表情卡牌": "Rell's Fear Emotile",
    "Rell 的快乐表情卡牌": "Rell's Happiness Emotile",
    "Rell 的饥饿表情卡牌": "Rell's Hunger Emotile",
    "Rell 的任性表情卡牌": "Rell's Petulance Emotile",
    "Rell 的自豪表情卡牌": "Rell's Pride Emotile",
    "Rell 的悲伤表情卡牌": "Rell's Sadness Emotile",
    "Rell 的害羞表情卡牌": "Rell's Shyness Emotile",
    "Rell 的惊讶表情卡牌": "Rell's Surprise Emotile",
}

_STEEL_GROUP_EN = {
    "强化与资源": "Upgrades & Resources",
    "护甲与幻纹": "Armor & Ephemera",
    "模组与装饰": "Mods & Decor",
    "战甲战斗姿态浮印": "Warframe Action Glyphs",
    "《老师》摄影棚系列": "The Teacher Photobooth Series",
}


def _shop_name(name: str) -> str:
    return _SHOP_NAME_EN.get(name, name) if i18n.is_en() else name


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
    idx, nxt_reset = steel_rotation_index(data)
    cur = weekly[idx]
    nxt = weekly[(idx + 1) % len(weekly)]
    lines = [
        L(
            f"◆ 本周轮换：{cur['name']}（{cur['cost']} 精华）｜第 {idx + 1}/{len(weekly)} 周 · 每周限购 1 次",
            f"◆ This week's rotation: {_shop_name(cur['name'])} ({cur['cost']} Essence)"
            f" ｜ Week {idx + 1}/{len(weekly)} · limit 1 per week",
        ),
    ]
    if nxt_reset:
        lines.append(
            L(
                f"◆ 下周轮换：{nxt['name']}（{nxt['cost']} 精华）｜距轮换 剩余 {countdown(nxt_reset)}",
                f"◆ Next week's rotation: {_shop_name(nxt['name'])} ({nxt['cost']} Essence)"
                f" ｜ Rotation {_in_en(countdown(nxt_reset))}",
            )
        )
    else:
        lines.append(
            L(
                f"◆ 下周轮换：{nxt['name']}（{nxt['cost']} 精华）",
                f"◆ Next week's rotation: {_shop_name(nxt['name'])} ({nxt['cost']} Essence)",
            )
        )
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
        return (
            L("钢铁精华兑换", "Steel Essence Shop"),
            [
                L(
                    "兑换表缺失：core/data/de/steel_shop.json",
                    "Shop table missing: core/data/de/steel_shop.json",
                )
            ],
        )
    weekly = data.get("weekly") or []
    evergreen = data.get("evergreen") or []

    lines: list[str] = []
    rot_lines, _, _ = _steel_rotation(data)
    lines.extend(rot_lines)

    lines.append(
        L(
            f"◆ 常驻商品：共 {len(evergreen)} 件，可随时购买",
            f"◆ Permanent stock: {len(evergreen)} items, always available",
        )
    )
    last_group = None
    for it in evergreen:
        grp = it.get("group")
        # 分组标题只在切换分组时输出，避免同一分类反复出现
        if grp and grp != last_group:
            lines.append(f"◆ {_STEEL_GROUP_EN.get(grp, grp) if i18n.is_en() else grp}")
            last_group = grp
        elif last_group is None and not any(ln.startswith("· ") for ln in lines[-1:]):
            lines.append(L("◆ 商品", "◆ Items"))
            last_group = ""
        lines.append(
            L(
                f"· {it['name']}　{it['cost']} 精华",
                f"· {_shop_name(it['name'])}　{it['cost']} Essence",
            )
        )

    total_all = sum(int(i.get("cost", 0)) for i in evergreen) + sum(
        int(w.get("cost", 0)) for w in weekly
    )
    lines.append(
        L(
            "※ 精华获取：钢铁之路侵袭 5/个 · Acolyte 2/个 · 夜灵 1/个 · 钢铁裂隙开核桃 1/个 · 清完星球 25",
            "※ Essence sources: Steel Path Incursions 5 each · Acolyte 2 each · "
            "Eidolon 1 each · Steel Path fissure Crack Relics 1 each · clearing a planet 25",
        )
    )
    lines.append(
        L(
            f"※ 轮换按官方锚点推算；各商品精华总价 {total_all}（常驻全买 + 轮换各一次）",
            f"※ Rotation is estimated from the official anchor; total Essence cost {total_all}"
            " (all permanent items + each rotation item once)",
        )
    )
    return (L("钢铁精华兑换（Teshin 荣誉商店）", "Steel Essence Shop (Teshin's Honors)"), lines)


def fmt_sliver_shop() -> tuple[str, list[str]]:
    """碎银兑换 = Palladino 裂罅碎块商店（地球「钢铁守望」）。

    ★ 2026-10-03 取证：全表**常驻、无轮换库存**；每项有每周限购，
    **周一 00:00 UTC 重置**（wiki 原文 Purchase limits reset on Monday
    00:00 UTC）。数据 core/data/de/palladino_shop.json，中文名 DE 官方。
    """
    data = palladino_shop()
    if not data:
        return (
            L("裂罅碎块兑换", "Riven Sliver Shop"),
            [
                L(
                    "兑换表缺失：core/data/de/palladino_shop.json",
                    "Shop table missing: core/data/de/palladino_shop.json",
                )
            ],
        )
    items = data.get("evergreen") or []
    _n, nxt = weekly_reset_info(data)
    S = "　"
    lines: list[str] = []
    if nxt:
        lines.append(
            L(
                f"◆ 限购重置：周一 00:00 UTC｜距重置 剩余 {countdown(nxt)}",
                f"◆ Limits reset: Mon 00:00 UTC ｜ Reset {_in_en(countdown(nxt))}",
            )
        )
    lines.append(
        L(
            f"◆ 常驻商品：共 {len(items)} 件（限购 = 每周可购次数）",
            f"◆ Permanent stock: {len(items)} items (limit = purchases per week)",
        )
    )
    lines += [
        L("◆ 兑换列表", "◆ Exchange List"),
        L(f"{S}商品{S}价格{S}每周限购", f"{S}Item{S}Price{S}Weekly limit"),
    ]
    for it in items:
        lim = it.get("limit")
        lines.append(
            L(
                f"· {it['name']}{S}{it['cost']} 碎块{S}{lim if lim else '—'}",
                f"· {_shop_name(it['name'])}{S}{it['cost']} Slivers{S}{lim if lim else '—'}",
            )
        )
    lines.append(
        L(
            "※ 碎块获取：开安魂遗物、航道星舰任务、钢铁之路卓越者；"
            "每周限购按 wiki 原文（未明示者按一次性解锁）",
            "※ Slivers come from cracking Requiem relics, Railjack missions and Steel Path Eximus; "
            "weekly limits follow the wiki (items without a stated limit count as one-time unlocks)",
        )
    )
    return (
        L("裂罅碎块兑换（Palladino · 钢铁守望）", "Riven Sliver Shop (Palladino · Iron Wake)"),
        lines,
    )


def fmt_alerts(alerts: Iterable[dict]) -> tuple[str, list[str]]:
    """警报卡（★ C3 2026-10-03：奖励不再「?」——解析补全 + 路径换中文名）。

    ★ 2026-10-11 第二步改版：开头给数量与最近到期；列表是「节点 / 类型 / 等级 / 奖励」表，
    倒计时右对齐；各条的警报名完全相同时（如全是「Tenno 联合警报」）不再每行重复，
    挪进摘要。
    """
    S = "　"
    recs = []
    for a in alerts or []:
        if not a.get("active", True):
            continue
        mission = a.get("mission", {}) or {}
        rewards = mission.get("reward", {}) or {}
        names = rewards.get("item_names") or ([rewards["item"]] if rewards.get("item") else [])
        items = L("、", ", ").join(
            [
                *names,
                *(
                    [L(f"{rewards['credits']}现金", f"{rewards['credits']} Credits")]
                    if rewards.get("credits")
                    else []
                ),
            ]
        )
        lv = (
            L(
                f"{mission['min_level']}-{mission['max_level']}级",
                f"Lv {mission['min_level']}-{mission['max_level']}",
            )
            if mission.get("min_level") and mission.get("max_level")
            else ""
        )
        recs.append(
            (
                mission.get("desc") or "",
                mission.get("node", "?"),
                mission_cn(mission.get("type", "")),
                lv,
                items or "?",
                a.get("expiry", ""),
            )
        )
    title = L("警报", "Alerts")
    if not recs:
        return (
            title,
            [L("◆ 进行中：当前没有进行中的警报", "◆ Active: no alerts at the moment")],
        )
    descs = {r[0] for r in recs}
    same_desc = len(descs) == 1 and next(iter(descs)) != ""
    summ = L(f"{len(recs)} 条", f"{len(recs)}") + (
        L(f"（{next(iter(descs))}）", f" ({next(iter(descs))})") if same_desc else ""
    )
    soon = min(recs, key=lambda r: r[5] or "9999")
    lines = [
        L(f"◆ 进行中：{summ}", f"◆ Active: {summ}"),
        L("◆ 最近到期：", "◆ Next to expire: ") + f"{soon[1]}{S}" + _left(countdown(soon[5]), "剩"),
        L("◆ 警报列表", "◆ Alert List"),
    ]
    head = L(["节点", "类型", "等级", "奖励"], ["Node", "Type", "Level", "Reward"])
    if not same_desc:
        head.insert(0, L("警报", "Alert"))
    lines.append(S + S.join(head))
    for desc, node, mt, lv, items, exp in recs:
        cells = ([desc] if not same_desc else []) + [node, mt, lv, items]
        lines.append("· " + S.join(cells) + S + _left(countdown(exp), "剩"))
    return (title, lines)


def _fac(side: dict) -> str:
    """派系显示名：有原始代码时回落到官方英文名。

    ★ 2026-10-11：Infestation 统一显示为 **Infested**（与裂隙 `_FACTION_ZH`、
    仲裁 `arbi._FACTION_FIX` 同一口径；官方 Faction 表的 Infestation 只在这里换显示词，
    `faction_name()` 的返回值不动）。
    """
    code = side.get("faction_code") or ""
    name = faction_name(code) if code else side.get("faction", "?")
    return "Infested" if name == "Infestation" else name


def fmt_invasions(
    invasions: Iterable[dict], page: int = 1, page_size: int = 6
) -> tuple[str, list[str]]:
    """入侵：节点 + 双方阵营/奖励 + 双方占比，支持翻页。

    占比与游戏内一致：``攻击方 = (Goal + Count) / (2·Goal)``，防御方为补数；
    Infested 入侵另算（起点 100%，见 de_worldstate._parse_invasions）。
    旧实现只给 ``Count/Goal``，负数被夹成 0%，于是「48% vs 52%」显示成
    「攻击方 0%（被反推 4%）」，与客户端对不上。

    Args:
        invasions: 解析后的入侵列表。
        page: 页码，从 1 开始。
        page_size: 每页场数（一场占 3~4 行，默认 6 场）。
    """
    live = [i for i in (invasions or []) if not i.get("completed")]
    total = len(live)
    title = L("入侵", "Invasions")
    if not total:
        return (title, [L("当前没有进行中的入侵", "No invasions in progress")])
    pages = max(1, (total + page_size - 1) // page_size)
    page = max(1, min(page, pages))
    chunk = live[(page - 1) * page_size : page * page_size]
    S = "　"
    lines = [
        L(f"◆ 进行中：{total} 场", f"◆ Active: {total}")
        + (
            L(
                f"（第 {page}/{pages} 页，本卡 {len(chunk)} 场；加 -2 / -3 翻页）",
                f" (Page {page}/{pages}, {len(chunk)} on this card; add -2 / -3 to turn pages)",
            )
            if pages > 1
            else ""
        )
    ]
    for inv in chunk:
        at = inv.get("attacker", {}) or {}
        vs = inv.get("defender", {}) or {}
        at_fac = _fac(at)
        vs_fac = _fac(vs)
        at_pct = inv.get("attacker_pct", inv.get("completion", 0)) or 0
        vs_pct = inv.get("defender_pct", 100 - at_pct) or 0
        lines.append(
            L(
                f"◆ {inv.get('node', '?')}{S}{at_fac}（攻） vs {vs_fac}（守）",
                f"◆ {inv.get('node', '?')}{S}{at_fac} (Att) vs {vs_fac} (Def)",
            )
        )
        lead = at_fac if at_pct >= vs_pct else vs_fac
        lines.append(
            L(
                f"· 进度{S}{at_fac} {at_pct:.0f}% vs {vs_pct:.0f}% {vs_fac}{S}{lead} 领先",
                f"· Progress{S}{at_fac} {at_pct:.0f}% vs {vs_pct:.0f}% {vs_fac}{S}{lead} leads",
            )
        )
        at_items = at.get("items") or []
        vs_items = vs.get("items") or []
        _sep = L("、", ", ")
        if at_items:
            lines.append(
                L(
                    f"· 协助 {at_fac}{S}{'、'.join(at_items)}",
                    f"· Assist {at_fac}{S}{_sep.join(at_items)}",
                )
            )
        if vs_items:
            lines.append(
                L(
                    f"· 协助 {vs_fac}{S}{'、'.join(vs_items)}",
                    f"· Assist {vs_fac}{S}{_sep.join(vs_items)}",
                )
            )
        if not at_items and not vs_items:
            lines.append(
                L(f"· 奖励{S}双方均无奖励物品", f"· Rewards{S}No reward items on either side")
            )
    lines.append(
        L(
            "※ 占比为双方推进度（合计 100%），按争夺最激烈的排前面；"
            "选边前请核对两侧奖励，选错在结算前仍可更换",
            "※ Percentages are each side's progress (100% in total), the most contested first; "
            "check both sides' rewards before choosing; you can switch sides before the invasion resolves",
        )
    )
    if any(i.get("vsInfestation") for i in chunk):
        lines.append(
            L(
                "※ Infested 入侵只能协助防御方：Infested 从 100% 起被逐步压回 0%",
                "※ Infested invasions can only be assisted on the defending side: Infested start at 100% and are pushed back toward 0%",
            )
        )
    synth = sorted({n for inv in chunk for n in (inv.get("synthetic") or [])})
    if synth:
        # 「官方没有的写法 ≠ 不存在的东西」：DE 只给基名（没有「…Blueprint」词条）时
        # 按官方基名 + 「蓝图」合成，但必须自曝，不能冒充游戏内原文。
        lines.append(
            L(
                "※ 合成译名（官方无对应文案，按官方基名合成）：" + "、".join(synth),
                "※ Synthesized names (no official text; built from the official base name): "
                + ", ".join(synth),
            )
        )
    return (title, lines)


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
    """午夜电波：本季摘要 + 每日 / 每周挑战（挑战行 + 缩进的任务描述并成一条）。

    ★ 2026-10-11 第二步改版：不再每行重复「[每日]」标签（分区标题已写明）；
    精英挑战用「精英」芯片；声望、倒计时分列。
    """
    title = L("午夜电波", "Nightwave")
    if not nw:
        return (title, [L("数据暂不可用", "Data unavailable")])
    S = "　"
    season = nw.get("season")
    lines: list[str] = []
    daily = [c for c in (nw.get("activeChallenges") or []) if c.get("isDaily")]
    weekly = [c for c in (nw.get("activeChallenges") or []) if not c.get("isDaily")]
    if season:
        lines.append(
            L(f"◆ 本季：第 {season} 季", f"◆ Season: {season}")
            + (S + _left(countdown(nw.get("expiry", ""))) if nw.get("expiry") else "")
        )
    notes = []
    for group, tag in ((daily, L("每日", "Daily")), (weekly, L("每周", "Weekly"))):
        if not group:
            continue
        lines.append(L(f"◆ {tag}挑战（{len(group)} 项）", f"◆ {tag} Challenges ({len(group)})"))
        for ch in group[:8]:
            ch_title = ch.get("title", "") or L(f"{tag}挑战", f"{tag} Challenge")
            standing = ch.get("standing")
            st = L(f"{standing} 声望", f"{standing} Standing") if standing else ""
            elite = L("[精英]", "[Elite]") if ch.get("isElite") else ""
            lines.append(
                f"· {ch_title}{S}{st}{S}{elite}{S}" + _left(countdown(ch.get("expiry", "")), "剩 ")
            )
            desc = _nw_desc(ch.get("desc") or "", ch.get("required"))
            if desc:
                lines.append(f"{S}{desc}")
        if len(group) > 8:
            notes.append(
                L(
                    f"※ {tag}挑战另有 {len(group) - 8} 项未列出",
                    f"※ {len(group) - 8} more {tag} challenges not listed",
                )
            )
    if not daily and not weekly:
        lines.append(L("◆ 挑战：当前没有激活的挑战", "◆ Challenges: none active at the moment"))
    lines += notes
    lines.append(
        L(
            "※ 完成挑战得电波声望；奖励在电波等级轨道上解锁（满级约 30 级）",
            "※ Completing challenges earns Nightwave Standing; rewards unlock along the rank track (about 30 ranks at max)",
        )
    )
    return (title, lines)


def fmt_kuva(missions: Iterable[dict]) -> tuple[str, list[str]]:
    lines = [
        f"{m.get('node', '?')} · {mission_cn(m.get('type', ''))} · {m.get('enemy', '?')}"
        for m in missions or []
    ]
    return (
        L("赤毒/血紊虹吸", "Kuva Siphons / Kuva Flood"),
        lines or [L("当前没有赤毒任务", "No Kuva missions at the moment")],
    )


def _clean_url(url: str) -> str:
    """去掉 utm 追踪参数，缩短新闻链接。"""
    return re.sub(r"[?&]utm_[a-z]+=[^&]*", "", url or "").rstrip("?&")


def fmt_news(news: Iterable[dict], limit: int = 6) -> tuple[str, list[str]]:
    """最近新闻。优先官方公告（community=False），社区贴只占补充位。

    中文来源：DE 在 Events.Messages 里直接下发 ``zh`` 全句，所以这里已经是官方简中，
    不再走英文兜底（旧实现取 ``en``，于是西语/波兰语的论坛贴也被当成新闻）。
    ★ 2026-10-11 第二步改版：标题一行、链接缩进成从属行（并入同一条斑马纹）。
    """
    items = [n for n in (news or []) if (n.get("message") or "").strip()]
    official = [n for n in items if not n.get("community")]
    community = [n for n in items if n.get("community")]
    picked = (official + community)[:limit]
    title = L("最近新闻", "Latest News")
    if not picked:
        return (title, [L("暂无新闻", "No news")])
    lines = [
        L(f"◆ 最近新闻：{len(picked)} 条", f"◆ Latest news: {len(picked)}"),
        L("◆ 新闻列表", "◆ News List"),
    ]
    for n in picked:
        msg = (n.get("message") or "").strip()
        link = _clean_url(n.get("link", ""))
        tag = "" if not n.get("community") else L("［社区］", "(Community) ")
        lines.append(f"· {tag}{msg}")
        if link:
            lines.append(f"　{re.sub(r'^https?://', '', link)}")
    return (title, lines)


# ---------------------------------------------------------------------------
# 赏金 / 结合目标 / 建造进度 / 深层科研
# ---------------------------------------------------------------------------

SYNDICATE_BOUNTY = {
    "Ostrons": "希图斯（地球）",
    "Solaris United": "奥布山谷（金星）",
    "Entrati": "英择谛（魔胎之境）",
    "The Hex": "六人组（1999）",
    "EntratiLab": "解剖圣所（实验室）",
    "HexCity": "霍瓦尼亚（1999）",
    "Holdfasts": "羽化之穹（扎里曼）",
}

_SYNDICATE_BOUNTY_EN = {
    "Ostrons": "Cetus (Earth)",
    "Solaris United": "Fortuna (Venus)",
    "Entrati": "Necralisk (Cambion Drift)",
    "The Hex": "The Hex (1999)",
    "EntratiLab": "Sanctum Anatomica (Lab)",
    "HexCity": "Höllvania (1999)",
    "Holdfasts": "Chrysalith (Zariman)",
}


def _synd_title(key: str) -> str:
    """赏金地区显示名（SYNDICATE_BOUNTY 的双语取值）。"""
    return (_SYNDICATE_BOUNTY_EN if i18n.is_en() else SYNDICATE_BOUNTY).get(key, key)


# 赏金里写死的中文标签 / 池键 → 英文（只在英文模式用；池键见 core/data/bounty_pools.json）
_BOUNTY_LABEL_EN = {
    "合一众": "Narmer",
    "钢铁之路": "Steel Path",
    "社区观测": "Community-observed",
    "隔离库1阶": "Isolation Vault Tier 1",
    "隔离库2阶": "Isolation Vault Tier 2",
    "隔离库3阶": "Isolation Vault Tier 3",
}


def _bl(text: str) -> str:
    """赏金标签双语：英文模式按 `` · `` 分段逐段换成英文，查不到的原样保留。"""
    if not i18n.is_en() or not text:
        return text
    return " · ".join(_BOUNTY_LABEL_EN.get(t, t) for t in text.split(" · "))


_POOL_EN_REV: Optional[dict] = None
# DE 英文掉落表原文（https://www.warframe.com/droptables）。旧奖励池使用的别名、
# 省略的蓝图部件字段不能仅凭语言键精确命中；这里保留官方掉落行的名称。
_POOL_FIXED_EN = {
    "内融核心": "Endo",
    "现金匣": "Credits Cache",
    "Orokin定向矩阵装置": "Orokin Orientation Matrix",
    "Orokin弹道矩阵装置": "Orokin Ballistics Matrix",
    "Orokin战意矩阵装置": "Orokin Animus Matrix",
    "侦察者蓝图": "Reconifex Blueprint",
    "加密的日记碎片": "Encrypted Journal Fragment",
    "史度巴蓝图": "Stubba Blueprint",
    "告达环形装置": "Calda Toroid",
    "夜灵晶体蓝图": "Eidolon Lens Blueprint",
    "夜语者77蓝图": "Vesper 77 Blueprint",
    "夸塔克蓝图": "Quartakk Blueprint",
    "威震武扇蓝图": "Quassus Blueprint",
    "孢丝感染枪蓝图": "Sporothrix Blueprint",
    "弗拉克斯亡魂蓝图": "Furax Wraith Blueprint",
    "提帕瘤": "Tepa Nodule",
    "暮斩蓝图": "Hespar Blueprint",
    "电幻步枪蓝图": "Alternox Blueprint",
    "疆南星刺蓝图": "Arum Spinosa Blueprint",
    "索拉环形装置": "Sola Toroid",
    "维加环形装置": "Vega Toroid",
    "蕈菇枪托蓝图": "Arbucep Stock Blueprint",
    "蕈菇枪机蓝图": "Arbucep Receiver Blueprint",
    "蕈菇枪管蓝图": "Arbucep Barrel Blueprint",
    "蕈菇蓝图": "Arbucep Blueprint",
    "蝰首骨妖蓝图": "Verdilac Blueprint",
    "赤炎流星蓝图": "Nepheri Blueprint",
    "辐光弩炮蓝图": "Mandonel Blueprint",
    "雷霆暴君蓝图": "Korumm Blueprint",
    "风鸣蓝图": "Aeolak Blueprint",
}
_POOL_RELIC_TIER_EN = {"古纪": "Lith", "前纪": "Meso", "中纪": "Neo", "后纪": "Axi"}
_POOL_PART_EN = {"机体": "Chassis", "系统": "Systems", "头部神经光元": "Neuroptics"}


def _pool_name_key(name: str) -> str:
    return re.sub(r"[\s·・]", "", name).casefold()


def _pool_en_rev() -> dict:
    """按同一官方键取英文名；兼容奖励池省略空格和间隔号的写法。"""
    global _POOL_EN_REV
    if _POOL_EN_REV is None:
        candidates: dict[str, set[str]] = {}
        for zh_file, en_file in (
            ("name_zh.json", "name_en.json"),
            ("languages_zh.json", "languages_en.json"),
        ):
            en_table = _de_zh(en_file)
            for key, zh_name in _de_zh(zh_file).items():
                en_name = en_table.get(key)
                if isinstance(zh_name, str) and isinstance(en_name, str) and en_name:
                    candidates.setdefault(_pool_name_key(zh_name), set()).add(en_name)
        _POOL_EN_REV = {
            key: next(iter(names)) for key, names in candidates.items() if len(names) == 1
        }
    return _POOL_EN_REV


def _pool_name_en(name: str) -> str:
    """赏金奖励池的单个中文名 → 英文；表里查不到的原样保留（不猜）。"""
    name = name.strip()
    if name in _POOL_FIXED_EN:
        return _POOL_FIXED_EN[name]
    m = re.match(r"^(古纪|前纪|中纪|后纪)\s+(\S+)\s+遗物(（光辉）)?$", name)
    if m:
        return f"{_POOL_RELIC_TIER_EN[m.group(1)]} {m.group(2)} Relic" + (
            " (Radiant)" if m.group(3) else ""
        )
    m = re.match(r"^([A-Za-z][A-Za-z0-9\- ]*?)(机体|系统|头部神经光元)?蓝图$", name)
    if m:
        part = _POOL_PART_EN.get(m.group(2) or "", "")
        return f"{m.group(1)} {part} Blueprint".replace("  ", " ")
    return _pool_en_rev().get(_pool_name_key(name), name)


def _pool_token_en(tok: str) -> str:
    mark = tok[0] if tok[:1] in ("★", "▣") else ""
    body = tok[len(mark) :].strip()
    m = re.match(r"^([\d,]+)\s*[×x]\s*(.+)$", body)
    if m:
        return f"{mark}{m.group(1)} × {_pool_name_en(m.group(2))}"
    m = re.match(r"^([\d,]+)\s+(\S+?)(\s*×\d+)?$", body)
    if m:
        return f"{mark}{m.group(1)} {_pool_name_en(m.group(2))}{m.group(3) or ''}"
    return mark + _pool_name_en(body)


def _disp_items(joined: str) -> str:
    """奖励串（内部用「、」连接，供后续按它切分）→ 显示：英文模式逐项换英文名并用 ``, `` 连接。"""
    if not i18n.is_en():
        return joined
    return ", ".join(_pool_token_en(t) for t in joined.split("、") if t)


def _pool_key_en(key: str) -> str:
    """奖励池档位键 → 英文显示（等级50-55 → Lv 50-55；阶段N → Tier N）。"""
    if key.startswith("等级"):
        return "Lv " + key.replace("等级", "")
    m = re.match(r"^阶段(\d+)$", key)
    if m:
        return f"Tier {m.group(1)}"
    return _BOUNTY_LABEL_EN.get(key, key)


_MOD_NAMES: Optional[set] = None
_RECIPE_NAMES: Optional[set] = None


def _de_names(filename: str) -> set:
    """读取 build_de_data.py 生成的官方名集合（MOD / 可制造物）。"""
    try:
        return set(
            json.loads(
                (Path(__file__).parent / "data" / "de" / filename).read_text(encoding="utf-8")
            )
        )
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
        raw = raw.strip().lstrip("★▣")  # 去掉可能已带的着色标记，保证幂等
        m = _num_re.match(raw)
        if m:
            qty, name = m.group(1), m.group(2).strip()
        else:
            qty, name = "", raw
        key = name.replace(" ", "").lower()
        if key in merged:
            old_q = (
                merged[key].split("×")[0].strip().replace(",", "") if "×" in merged[key] else "0"
            )
            try:
                if int(qty.replace(",", "") or 0) > int(old_q or 0):
                    merged[key] = raw
            except ValueError:
                pass
        else:
            merged[key] = raw
            order.append(key)
    out = []
    mod_kw = (
        "预言",
        "角斗士",
        "私法",
        "技法",
        "合成",
        "机甲",
        "预见",
        "通灵",
        "电涌",
        "狂暴化",
        "致残突击",
        "得到救赎",
        "生长之力",
        "反抗军",
        "快枪手",
        "失效",
        "震惊",
        "蛇主",
        "狼主",
        "枭主",
    )
    # 注意不含单字“刃”：否则“尖刃弹头”这类 MOD 会被误判成武器部件。
    part_kw = (
        "蓝图",
        "枪管",
        "枪机",
        "枪托",
        "机体",
        "头部",
        "系统",
        "握柄",
        "刀刃",
        "弓弦",
        "星体",
        "护手",
        "刀身",
        "剑柄",
    )
    for key in order:
        raw = merged[key]
        base = re.sub(r"\s*蓝图$", "", raw).strip()
        is_mod = raw in _MOD_NAMES or any(k in raw for k in mod_kw)
        is_part = base in _RECIPE_NAMES or raw in _RECIPE_NAMES or any(k in raw for k in part_kw)
        if is_mod and not is_part:
            out.append(f"★{raw}")
        elif is_part:
            out.append(f"▣{raw}")
        else:
            out.append(raw)
    return "、".join(out)


try:
    from pathlib import Path as _P

    _BOUNTY_POOLS = json.loads(
        (_P(__file__).parent / "data" / "bounty_pools.json").read_text(encoding="utf-8")
    )
except Exception:  # noqa: BLE001
    _BOUNTY_POOLS = {}


# DE 奖励池表名里的 Tier -> bounty_pools 的通用档位键（TierA..E 即 1..5 阶）
_TIER_POOL_KEY = {
    "TierA": "阶段1",
    "TierB": "阶段2",
    "TierC": "阶段3",
    "TierD": "阶段4",
    "TierE": "阶段5",
}

# 但部分 syndicate 的 Tier 字母并不按等级升序，必须按 syndicate 覆盖：
# 火卫二实测 TierA=5-15、TierC=15-25、TierB=25-30、TierD=30-40、TierE=40-60，
# 若沿用字母顺序会把「阶段2(蛮暴之力)」与「阶段3(露天开采)」整体互换。
_TIER_POOL_KEY_BY_SYNDICATE = {
    "Entrati": {
        "TierA": "阶段1",
        "TierC": "阶段2",
        "TierB": "阶段3",
        "TierD": "阶段4",
        "TierE": "阶段5",
    },
}

# 非 TierA-E 的特殊池：按 syndicate + rewards 表名特征定位
_SPECIAL_POOL_KEY = {
    # 合一众（Narmer）赏金：地球与金星共用同一张 NarmerTableXRewards，
    # 池内容也一致，故两边都指向唯一的「合一众」键。
    # （旧实现把地球的指到 `尸鬼净化`、金星的指到 `合一众·阶段6`，两处内容都是错的）
    "Ostrons": {"Narmer": "合一众"},
    "Solaris United": {
        "Narmer": "合一众",
        # 深矿钢铁档 → 钢铁版奖励池（`NokkoColonyRewardsSteel*` 三张表合并，
        # 构建见 `scripts/build_nokko_sp_pool.py`；普通版另有「深矿·企业重组」池）
        "NokkoColonyRewardsSteel": "深矿·企业重组·钢铁",
    },
    # 隔离库三档在 DE 侧各有独立奖励表：VaultBountyTierA/B/C
    # 分别对应 30-40 / 40-50 / 50-60 级，旧实现把三者并成一个「隔离库」池，
    # 导致三档奖励完全一样。
    "Entrati": {
        "VaultBountyTierA": "隔离库1阶",
        "VaultBountyTierB": "隔离库2阶",
        "VaultBountyTierC": "隔离库3阶",
    },
}
_SPECIAL_TAG = {
    "Narmer": "合一众",
    # 深矿（NokkoColony）钢铁之路档：**社区观测**登记（见 de_worldstate
    # SOLARIS_SUPPLEMENT_JOBS）—— 卡面标签同时标「钢铁之路」与「社区观测」，
    # 让读者知道这一档既不来自 DE 接口、也属于钢铁之路难度；
    # 奖励走**钢铁版**池（`NokkoColonyRewardsSteel*`，列 A/B/C 全部轮次）。
    "NokkoColonyRewardsSteel": "钢铁之路 · 社区观测",
    "VaultBountyTierA": "隔离库1阶",
    "VaultBountyTierB": "隔离库2阶",
    "VaultBountyTierC": "隔离库3阶",
}

_CONTINENT_TARGET = {
    # —— 英文写法（按小写子串匹配）——
    "earth": "Ostrons",
    "cetus": "Ostrons",
    "plains": "Ostrons",
    "venus": "Solaris United",
    "vallis": "Solaris United",
    "fortuna": "Solaris United",
    "nokko": "Solaris United",
    "deep mine": "Solaris United",
    "heist": "Solaris United",
    "deimos": "Entrati",
    "cambion": "Entrati",
    "necralisk": "Entrati",
    "vault": "Entrati",
    "zariman": "Holdfasts",
    "chrysalith": "Holdfasts",
    "sanctum": "EntratiLab",
    "anatomica": "EntratiLab",
    "hex": "HexCity",
    "hollvania": "HexCity",
    "höllvania": "HexCity",
    "地球": "Ostrons",
    "夜灵": "Ostrons",
    "希图斯": "Ostrons",
    "尸鬼": "Ostrons",
    "金星": "Solaris United",
    "山谷": "Solaris United",
    "奥布": "Solaris United",
    "索拉里": "Solaris United",
    "福尔图娜": "Solaris United",
    "深矿": "Solaris United",
    "抢劫": "Solaris United",
    "火卫二": "Entrati",
    "魔胎": "Entrati",
    "英择谛": "Entrati",
    "隔离库": "Entrati",
    "六人组": "HexCity",
    "1999": "HexCity",
    "霍瓦尼亚": "HexCity",
    "实验室": "EntratiLab",
    "解剖": "EntratiLab",
    "圣所": "EntratiLab",
    "扎里曼": "Holdfasts",
    "羽化": "Holdfasts",
    "虚空天使": "Holdfasts",
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
            if k:  # 该 syndicate 未配置此特殊池时不打标签，回落 Tier 匹配
                tag, key = t, k
            break
    if not key:
        m = re.search(r"Tier([A-E])", table)
        if m:
            tbl = _TIER_POOL_KEY_BY_SYNDICATE.get(syndicate, _TIER_POOL_KEY)
            key = tbl.get("Tier" + m.group(1), "")
    lv = job.get("enemyLevels") or []
    if not tag and len(lv) >= 2 and lv[0] == lv[1] == 100:
        tag = "钢铁之路"  # 100-100 的常规赏金即钢铁之路变体
    rm = re.search(r"Table([ABC])Rewards", table)
    return (pools.get(key) or {}), tag, (rm.group(1) if rm else "")


# ---------------------------------------------------------------------------
# 赏金：高价值奖励过滤 + 地区轮换汇总
# ---------------------------------------------------------------------------

# 用户关心的只有这几类：MOD（★）/ 部件·蓝图（▣）/ 债券 / 遗物 / 各地区特色资源。
# 现金匣、内融核心、聚魂晶体、赤毒、各种鱼和矿石每档都一样，列出来只会淹掉重点。
_HV_KEEP_KW = ("债券", "遗物", "绒翎", "音魂", "浆质", "阿耶檀识琥珀星", "赋能槽连接器")
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
    keep = [
        k
        for k in pool
        if k.startswith("阶段") or k.startswith("等级") or k == "合一众" or k.startswith("隔离库")
    ]
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
# ★ C2（2026-10-03）：公开别名供 push 的「蹲 赏金」复用（同源一处）。
ORACLE_REGIONS = _ORACLE_REGIONS


def de_zh(table: str) -> dict:
    """DE 简中表访问器（nodes_zh / challenges_zh …）——卡面与推送共用同源。"""
    return _de_zh(table)


# pool 键 → oracle 的 SyndicateMissions Tag（DE 地区不进这张表）
_ORACLE_TAGS: dict[str, str] = {k: t for k, t, _ in _ORACLE_REGIONS}

# 赏金地区的**剧情推进顺序**（从最早的夜灵平野到最新的 1999）。
# DE 的 SyndicateMissions 数组顺序**不保证**（实测会把火卫二排在金星前面），
# 所以一览与详情都按这张显式表排列，不再依赖数据源顺序。
_BOUNTY_REGION_ORDER: tuple[str, ...] = (
    "Ostrons",  # 夜灵平野（地球，2017）
    "Solaris United",  # 奥布山谷（金星，2018）
    "Entrati",  # 魔胎之境（火卫二，2020）
    "Holdfasts",  # 羽化之穹（扎里曼，2022）
    "EntratiLab",  # 解剖圣所（实验室，2023）
    "HexCity",  # 霍瓦尼亚（1999，2024）
)

_DE_ZH_CACHE: dict[str, dict] = {}

# 隔离库三档在 DE 侧 jobType 为空，任务名只能按奖励池标签回填。
# 官方简中文案（languages_zh 的 NecraloidStanding*ItemDesc）里叫「等级 1 / 2 / 3 隔离库赏金」，
# 直接用池键「隔离库1阶」当任务名玩家对不上。
_VAULT_TIER_NAME = {
    "隔离库1阶": "1 级隔离库赏金",
    "隔离库2阶": "2 级隔离库赏金",
    "隔离库3阶": "3 级隔离库赏金",
}


def _vault_tier_name(tag: str) -> str:
    return _BOUNTY_LABEL_EN.get(tag, tag) if i18n.is_en() else _VAULT_TIER_NAME.get(tag, tag)


# 赏金末阶段遭遇战（ExportBounties.stages[-1]）-> 显示用任务类型。
#
# 为什么拿「末阶段」当任务类型：DE 下发的 job 只有 ``jobType`` 资产路径与
# 奖励表，**不带节点**，所以开放世界赏金没有节点级 missionType 可查；
# ExportBounties 的 stages 末项就是这条赏金的收官任务，实测与官方赏金名
# 一一吻合（核心样本→挖掘、粉碎邪教→歼灭、猎人杀手→歼灭、尘土部队→资源回收）。
# 名字尽量取官方 MT 译名（歼灭/刺杀/捕获/挖掘/防御/破坏/救援/间谍/劫持），
# 官方没有对应 MT 的（伏击 / 资源回收 / 物资回收 / 净化）沿用赏金里的叫法。
_BOUNTY_ENC_EN = {
    "DynamicExterminate": "Exterminate",
    "DynamicCaveExterminate": "Exterminate",
    "DynamicExterminateDrones": "Exterminate",
    "DynamicExterminateMoas": "Exterminate",
    "DynamicAssassinate": "Assassinate",
    "DynamicCapture": "Capture",
    "DynamicExcavation": "Excavation",
    "DynamicExcavationEndless": "Excavation",
    "DynamicDefend": "Defense",
    "DynamicAreaDefense": "Defense",
    "DynamicDroneDefense": "Defense",
    "DynamicSabotage": "Sabotage",
    "DynamicRescue": "Rescue",
    "DynamicBaseSpy": "Spy",
    "DynamicCaches": "Sabotage",
    "DynamicCachesAirDrop": "Sabotage",
    "HiddenResourceCaches": "Sabotage",
    "HiddenResourceCachesCave": "Sabotage",
    "DynamicCorpusSurvivors": "Survival",
    "DynamicGrineerSurvivors": "Survival",
    "DynamicHijack": "Hijack",
    "DynamicRecovery": "Recovery",
    "DynamicResourceCapture": "Resource Recovery",
    "DynamicResourceTheft": "Resource Recovery",
    "DynamicKeyPieces": "Supply Recovery",
    "DynamicPurify": "Purify",
    "DynamicAmbush": "Ambush",
}
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
        key = code[len("Narmer") :] if code.startswith("Narmer") else code
        zh = (_BOUNTY_ENC_EN if i18n.is_en() else _BOUNTY_ENC_ZH).get(key) or (
            _BOUNTY_ENC_EN if i18n.is_en() else _BOUNTY_ENC_ZH
        ).get(code)
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


_DE_EN_FILE = {"nodes_zh.json": "nodes_en.json", "challenges_zh.json": "challenges_en.json"}


def _de_zh(filename: str) -> dict:
    """惰性读取 core/data/de 下的官方简中表（nodes_zh / challenges_zh …）；英文模式换成同名 _en 表。"""
    if i18n.is_en() and filename in _DE_EN_FILE:
        filename = _DE_EN_FILE[filename]
    if filename not in _DE_ZH_CACHE:
        try:
            _DE_ZH_CACHE[filename] = json.loads(
                (Path(__file__).parent / "data" / "de" / filename).read_text(encoding="utf-8")
            )
        except Exception:  # noqa: BLE001
            _DE_ZH_CACHE[filename] = {}
    return _DE_ZH_CACHE[filename]


# DE 挑战文案里的富文本标记：|COUNT| 要换成实际数量，|OPEN_COLOR| 之类直接去掉。
def _clean_challenge_desc(desc: str, count) -> str:
    txt = (desc or "").replace("|COUNT|", str(count) if count not in (None, "") else "N")
    txt = re.sub(r"\|[A-Z_]+\|", "", txt)
    txt = re.sub(r"^[^。\s　]*赏金任务[\s　]*", "", txt)  # 「…的赏金任务」前缀
    return re.sub(r"\s+", " ", txt).strip()


# 赏金「档位行」的行首块标记（★ 2026-09-27）：渲染层据此在**块与块之间**画分隔线。
# 为什么用**显式标记**而不是内容启发式：同一张卡上「档位行 / 奖励行（　　）/ 点位行
# （　点位：…）」混排，靠内容猜（如「含 ｜N-M级」）会漏掉 oracle 与退路两条路径；
# 显式标记让**新增地区自动生效**（这正是审核 §1.3 选方案 A 的理由）。
# ⚠️ 行首那个全角空格是原有缩进（渲染层按 `raw` 数它算 indent_lv），别去掉。
# 发射点共 5 处（跨 3 个函数）：`_bounty_head`（覆盖 DE 地区详情 + 一览）、
# `_oracle_summary` 两处、`_oracle_region_block` 两处 —— 改这里请**全部**同步。
_BOUNTY_HEAD_PREFIX = "　▸ "


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
        lv_txt = _bounty_lv_txt(lv)
        pool, tag, rot = _resolve_bounty_pool(syndicate, j)
        name = j.get("_jobName") or ""
        if not name and tag:
            # 无任务名（如隔离库）时用池标签充当名称，避免等级顶到名称位
            name, tag = _vault_tier_name(tag), ""
        # 只列 DE 当前生效的那一轮；池里没有该轮次（或本就只有一轮）时合并去重
        filled = [r for r in ("A", "B", "C") if pool.get(r)]
        if j.get("source") == "community":
            # 社区观测档（DE 完全不下发 → 当前轮次**无从得知**）：列 A/B/C 全部轮次，
            # 完全相同的条目只留一次（「10,000 现金匣」与「3 × 5,000 现金匣」这类
            # 数值不同的条目**都保留** —— 数量口径按池数据原样呈现，不合并数值）。
            items, seen = [], set()
            for r in ("A", "B", "C"):
                for t in pool.get(r) or []:
                    if t not in seen:
                        seen.add(t)
                        items.append(t)
        elif rot and len(filled) > 1 and pool.get(rot):
            items = list(pool[rot])
        else:
            items = []
            for r in ("A", "B", "C"):
                items.extend(pool.get(r) or [])
        out.append(_bounty_head(name, _bounty_type(j), lv_txt, _bl(tag)))
        if items:
            out.append("　　" + _disp_items(_fmt_pool_items(items)))
    return out


def _bounty_lv_txt(lv: list) -> str:
    """赏金等级文案：上下限相同写单值「100级」（渲染层 _LV_RE 认两种形态），否则「40-60级」。"""
    if len(lv) < 2:
        return "?"
    if i18n.is_en():
        return f"Lv {lv[0]}" if lv[0] == lv[1] else f"Lv {lv[0]}-{lv[1]}"
    return f"{lv[0]}级" if lv[0] == lv[1] else f"{lv[0]}-{lv[1]}级"


def _bounty_head(name: str, mtype: str, lv_txt: str, tag: str) -> str:
    """档位行：``　▸ 任务类型 任务名｜N-M级｜标签``（缺失的段落自动省略）。

    任务类型放在**行首**（与参考版式「挖掘 核心样本」「歼灭 带他们回家 合一众」
    一致）：玩家扫一眼就知道这档是什么任务，而后面的等级会被渲染层抽成右对齐列。

    赏金名里已经含类型词的（官方名「刺杀指挥官」「物资回收」「破坏 Grineer 的
    补给线」「伏击信使」）不再重复加前缀，否则会出现「物资回收 物资回收」。
    """
    if mtype and name and mtype in name:
        mtype = ""
    if tag and name:
        # ★ 2026-10-11：标签里已在名字中出现的段去掉（深矿档名「深矿（钢铁之路）」+
        #   标签「钢铁之路 · 社区观测」⇒ 卡面「钢铁之路」出现两次）
        tag = " · ".join(t for t in tag.split(" · ") if t and t not in name)
    body = " ".join(x for x in (mtype, name) if x) or lv_txt
    if body == lv_txt:
        return f"{_BOUNTY_HEAD_PREFIX}{lv_txt}" + (f"｜{tag}" if tag else "")
    head = f"{_BOUNTY_HEAD_PREFIX}{body}｜{lv_txt}"
    if tag:
        head += f"｜{tag}"
    return head


def _region_block(syndicate: str, title: str, jobs: list[dict], expiry: str) -> list[str]:
    """DE 下发 jobs 的地区（详情）：地区横幅 + 各档任务名与完整奖励。"""
    lines = [f"◆ {title}　" + _left(countdown(expiry), "剩")]
    lines.extend(_bounty_rows(jobs, syndicate))
    return lines


# 一览每地区列几个档位：取**等级最高的 3 档**（`_top_tiers`）。
# 不能用「DE 下发顺序的末尾 N 个」——火卫二的 3 个隔离库档（30-60 级）追加在
# 末尾，会把 100-100 的「蛮暴之力」挤掉；参考版式选的正是最高等级那几档
# （地球 = 40-60 刺杀指挥官 / 50-70 带他们回家 / 100-100 取回被偷的器物）。
_OVERVIEW_N = 3


def _region_rot_map(syndicate: str, jobs: list[dict]) -> dict[str, str]:
    """档位池键 -> 该档**自己** rewardTable 的轮次（A/B/C）。

    ★ 2026-10-11：旧实现只看第一个 job 的轮次套给全地区。火卫二 2026-10-11 实测
    ``TierATableB / TierCTableB / TierBTableA / TierDTableA / TierETableA / VaultBountyTier*TableA``
    —— 各档轮次不同，一览的「MOD 轮换（B）」把 A 轮档位的 MOD 也按 B 轮列了。
    """
    out: dict[str, str] = {}
    pool_all = _BOUNTY_POOLS.get(syndicate) or {}
    for j in jobs or []:
        if j.get("source") == "community":
            continue
        pool, _tag, rot = _resolve_bounty_pool(syndicate, j)
        if not rot:
            continue
        key = next((k for k, v in pool_all.items() if v is pool), "")
        if key and key not in out:
            out[key] = rot
    return out


def _rot_lines(region_pool: dict, rot, limit: int = 6) -> list[str]:
    """一览的「轮换」行：每地区**只出一行**、最多 ``limit`` 项、**不写省略号**。

    参考版式的地球「MOD 奖励轮换」、金星「债券轮换」、火卫二「隔离库轮换」
    就是这一行。旧实现把 A/B/C 与全部档位合并后逐类铺开（地球 16 项 MOD +
    10 项部件 + 「…等 N 项」），又长又杂（玩家反馈「搞得太多了」）。

    现在：跨档位合并 → 按 债券 > MOD > 部件 选一类 → 只列前 ``limit`` 项。
    想看某个档位的完整奖励，用「赏金 <地区>」详情页。

    ``rot`` 可以是单个轮次字母（oracle 地区：整区同轮次），也可以是
    ``{档位键: 轮次}``（DE 地区：按每档自己的 rewardTable，见 :func:`_region_rot_map`）；
    映射里没有的档位（本轮 DE 未下发）不计入。
    """
    items: list = []
    per_key = rot if isinstance(rot, dict) else None
    for key in _region_tier_keys(region_pool):
        if per_key is not None:
            if key not in per_key:
                continue
            items.extend(_pool_at_rot(region_pool.get(key) or {}, per_key[key]))
        else:
            items.extend(_pool_at_rot(region_pool.get(key) or {}, rot))
    if per_key is not None:
        rots = sorted(set(per_key.values()))
        rot = (
            rots[0]
            if len(rots) == 1
            else (L("各档轮次 ", "Rounds ") + "/".join(rots) if rots else "")
        )
    merged = [t for t in _fmt_pool_items(list(items)).split("、") if t and _is_high_value(t)]
    if not merged:
        return []
    bonds = [t for t in merged if "债券" in t]
    mods = [t for t in merged if t.startswith("★")]
    parts = [t for t in merged if t.startswith("▣") and t not in bonds]
    name, lst = L("本轮轮换", "Current Rotation"), merged
    if bonds:
        name, lst = L("债券轮换", "Bond Rotation"), bonds
    elif mods:
        name, lst = L("MOD 轮换", "Mod Rotation"), mods
    elif parts:
        name, lst = L("部件轮换", "Parts Rotation"), parts
    head = _disp_items("、".join(lst[:limit]))
    return [
        L(f"　{name}（{rot}）：{head}", f"　{name} ({rot}): {head}")
        if rot
        else L(f"　{name}：{head}", f"　{name}: {head}")
    ]


def _bounty_entry_line(syndicate: str, job: dict) -> str:
    """一览的档位行：``　▸ 任务类型 任务名｜N-M级｜标签``（不带奖励）。

    ★ 2026-09-27：行首标记由「·」改为 ``_BOUNTY_HEAD_PREFIX``（``▸``）——
    渲染层据此在**块与块之间**画分隔线（旧实现块间一条线都没有）。
    """
    lv = job.get("enemyLevels") or []
    lv_txt = _bounty_lv_txt(lv)
    _pool, tag, _rot = _resolve_bounty_pool(syndicate, job)
    name = job.get("_jobName") or ""
    if not name and tag:
        name, tag = _vault_tier_name(tag), ""
    return _bounty_head(name, _bounty_type(job), lv_txt, _bl(tag))


def _region_summary(syndicate: str, title: str, jobs: list[dict], expiry: str) -> list[str]:
    """一览（DE 地区）：地区横幅 + 轮换行 + 等级最高的若干档。"""
    region_pool = _BOUNTY_POOLS.get(syndicate) or {}
    lines = [f"◆ {title}　" + _left(countdown(expiry), "剩")]
    lines.extend(_rot_lines(region_pool, _region_rot_map(syndicate, jobs)))
    for j in _top_tiers(jobs or [], _OVERVIEW_N):
        lines.append(_bounty_entry_line(syndicate, j))
    return lines


def _one_word(text: str) -> str:
    """把内部 ASCII 空格换成 NBSP（U+00A0）。

    渲染层靠空格把「任务：**类型** **挑战名** 目标」切成三段分别上色，
    所以前两段必须是**单个词**；挑战名里确实可能带空格（「任务完成 X」这类），
    换 NBSP 后仍是同一个词、视觉上仍是空格，不会串色。
    """
    return (text or "").replace(" ", "\u00a0")


# oracle 三地区详情卡的档位行形态（2026-10-02 用户逐地区指定）：
#   · 圣所 / 扎里曼：地图（节点）名与「任务：」前缀都去掉，
#     档位行 = 「类型 挑战名」，等级挂行尾由渲染层抽成右对齐徽章
#   · 1999（HexCity）：节点名保留，挑战名并入档位行（⟦c⟧ 标记，渲染层染蓝）
# 一览卡仍走 `_oracle_task_lines` 的「任务：类型 挑战名」行（紧凑不变）。


def _oracle_task_lines(node_key: str, ch_path: str) -> list[str]:
    """**一览卡** oracle 地区的「任务：」行 —— ``任务：类型 挑战名``。

    * **任务类型**取该节点的官方 ``missionName``（``nodes_zh[key]['type']``，
      如 哈拉科防线→歼灭、翠径→移动防御）。这是唯一权威来源：oracle 只给
      节点 key + 挑战路径，DE 又不给这三个地区的 jobs，所以没有别的路。
    * **挑战名**是这条赏金的挑战标题（能量超载 / 终结好戏 / 致命低语…）。
      2026-09-12 为了对齐参考版式曾把它去掉，用户随即反馈「这些任务名字怎么没了」，
      现已恢复。
    * ★ **目标描述已整段移除**（2026-10-02 玩家反馈「任务描述没任何作用」）；
      DE 三地区的纯描述行随之整体下线。
    * ★ 详情卡 2026-10-02 起**不再出独立任务行**：类型+挑战名并入档位行
      （圣所/扎里曼无地图名；1999 挑战名 ⟦c⟧ 并入节点名后染蓝），见
      ``_oracle_region_block``。
    """
    node = _de_zh("nodes_zh.json").get(node_key or "") or {}
    mtype = _one_word((node.get("type") or "").strip())
    ch = _de_zh("challenges_zh.json").get(ch_path) or {}
    cname = _one_word((ch.get("name") or "").strip())
    head = " ".join(x for x in (mtype, cname) if x)
    if not head:
        return []
    return [L(f"　　任务：{head}", f"　　Task: {head}")]


def _oracle_summary(
    pool_key: str, tag: str, title: str, bounties: list[dict], rot: str = "", expiry: str = ""
) -> list[str]:
    """一览（oracle 地区）：地区横幅（含轮换倒计时）+ 轮换行 + 最高若干档。

    ★ C2（2026-10-03 复核修复）：横幅倒计时取 oracle bounty-cycle 的
    ``expiry``（与 DE 地区用 Syndicates[].Expiry 同口径）——此前 oracle
    三地区一览只有「◆ 地区」没有「剩X」（cycle 的 expiry 没被传进来）。
    """
    region_pool = _BOUNTY_POOLS.get(pool_key) or {}
    if not region_pool:
        return []
    tiers = _ordered_tier_keys(region_pool)

    def _lv_txt(key: str) -> str:
        if i18n.is_en():
            return _pool_key_en(key)
        return key.replace("等级", "") + "级" if key.startswith("等级") else key

    lines = [f"◆ {title}" + ("　" + _left(countdown(expiry), "剩") if expiry else "")]
    lines.extend(_rot_lines(region_pool, rot))
    node_tbl = _de_zh("nodes_zh.json")
    if not bounties:
        for lv in tiers[-_OVERVIEW_N:]:
            lines.append(f"{_BOUNTY_HEAD_PREFIX}{_lv_txt(lv)}")
        return lines
    # oracle 的节点列表本来就是等级升序，取末尾 N 个即「等级最高的 N 档」
    for i, b in list(enumerate(bounties))[-_OVERVIEW_N:]:
        lv = tiers[i] if i < len(tiers) else (tiers[-1] if tiers else "")
        node_key = b.get("node") or ""
        node = (node_tbl.get(node_key) or {}).get("name") or ""
        if not node:
            continue
        mtype = ((node_tbl.get(node_key) or {}).get("type") or "").strip()
        if mtype and node.startswith(mtype):
            # ★ 2026-10-11：1999 的节点名本身就是任务类型（「传承种收割」「歼灭：炽蛇军」），
            #   旧版再出一行「任务：传承种收割 噪音污染」⇒ 同一词连写两遍。改为把挑战名
            #   并进档位行（与详情卡同形），不再单出任务行。
            ch = _de_zh("challenges_zh.json").get(b.get("challenge") or "") or {}
            cname = _one_word((ch.get("name") or "").strip())
            head = f"{node} {cname}" if cname else node
            lines.append(f"{_BOUNTY_HEAD_PREFIX}{head}｜{_lv_txt(lv)}")
            continue
        lines.append(f"{_BOUNTY_HEAD_PREFIX}{node}｜{_lv_txt(lv)}")
        lines.extend(_oracle_task_lines(node_key, b.get("challenge") or ""))
    return lines


def _oracle_region_block(
    pool_key: str, tag: str, title: str, bounties: list[dict], rot: str = "", expiry: str = ""
) -> list[str]:
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
        if i18n.is_en():
            return _pool_key_en(key)
        return key.replace("等级", "") + "级" if key.startswith("等级") else key

    _head = f"◆ {title}" + ("　" + _left(countdown(expiry), "剩") if expiry else "")
    if not bounties:
        # oracle 拿不到 → 按等级档列全部奖励（rot="" 时 _pool_at_rot 合并 A/B/C）
        lines = [_head]
        for lv in tiers:
            items = _pool_at_rot(region_pool.get(lv) or {}, "")
            if items:
                lines.append(f"{_BOUNTY_HEAD_PREFIX}{_lv_txt(lv)}")
                lines.append("　　" + _disp_items(_fmt_pool_items(items)))
        return lines if len(lines) > 1 else []

    node_tbl = _de_zh("nodes_zh.json")
    lines = [_head]
    for i, b in enumerate(bounties):
        lv = tiers[i] if i < len(tiers) else (tiers[-1] if tiers else "")
        node_key = b.get("node") or ""
        node_name = (node_tbl.get(node_key) or {}).get("name") or ""
        if not node_name:
            continue
        lv_txt = _lv_txt(lv)
        ch = _de_zh("challenges_zh.json").get(b.get("challenge") or "") or {}
        cname = _one_word((ch.get("name") or "").strip())
        # 目标描述（额外目标要怎么完成）—— 2026-10-02 曾误删，用户随即反馈
        # 「之前有的白字不要去掉」：恢复
        goal = _clean_challenge_desc(ch.get("desc"), ch.get("count"))
        # 档位行三段染色（2026-10-02 维护者定色）：段1 = 类型（圣所/扎里曼，
        # 渲染层行首类型色青）/ 节点名（1999，⟦c⟧ 整段蓝 —— 节点名里的派系词
        # 「炽蛇军」必须跟整体同色，不能被派系词表单独染橙）；
        # 段2 = 挑战名（⟦v⟧ 紫，沿用早期三段切分的挑战名配色）；
        # 段3 = 目标（⟦w⟧ 白）。段2/段3 打标记由渲染层直绘，不走语义 token。
        segs: list[str] = []
        if pool_key == "HexCity":
            if node_name:
                segs.append(f"⟦c⟧{node_name}⟦/c⟧")
        else:
            mtype = _one_word((node_tbl[node_key].get("type") or "").strip())
            if mtype:
                segs.append(mtype)
        if cname:
            segs.append(f"⟦v⟧{cname}⟦/v⟧")
        if goal:
            segs.append(f"⟦w⟧{goal}⟦/w⟧")
        head = " ".join(segs)
        if head:
            lines.append(f"{_BOUNTY_HEAD_PREFIX}{head}｜{lv_txt}")
        else:
            lines.append(f"{_BOUNTY_HEAD_PREFIX}{lv_txt}")
        items = _pool_at_rot(region_pool.get(lv) or {}, rot)
        if items:
            lines.append("　　" + _disp_items(_fmt_pool_items(items)))
    return lines


# 各地区点位块的来源注脚（口径随地区不同，别用同一句）
_TENT_NOTE_EN = {
    "Ostrons": "※ Small tents = the current bounties at the three Plains camps (derived from the DE world seed, matches the game); blue text = fast tasks",
    "Solaris United": "※ Current bounties at the seven Orb Vallis locations (derived from the DE world seed, matches the game)",
}
_TENT_NOTE = {
    # 「蓝字」对应 render._TENT_BLUE_TASKS（攻略推荐的四个快速任务）；文字输出由 render 换成「（快速）」标注。
    "Ostrons": "※ 小帐篷 = 平野三处营地的当前赏金（按 DE 世界种子推算，与游戏内一致）；"
    "蓝字 = 快速任务",
    "Solaris United": "※ 以上为金星七处赏金点位的当前赏金（按 DE 世界种子推算，与游戏内一致）；"
    "「未定名」= 官方简中尚未翻译该点位",
}


def _tent_lines(syndicates, region: str = "Ostrons") -> list[str]:
    """某地区的**点位级**当前赏金（详情卡专用；推算见 core/tents.py）。

    2026-09-24 双向对拍一致后才上卡：地球 W4 与沃沃截图 9/9 格、W5 与
    oracle.browse.wf 独立实现 15/15 点位（含金星 7 / 火卫二 5）。**当前只出地球**：
    金星 2026-09-26 按用户口径撤下（卡面改列深矿钢铁档奖励）、火卫二缺官方译名。
    种子缺失（源降级）时整块跳过。

    ★ 地区由调用方传 **syndicate 键**（`tents.REGIONS` 的键），凡在 REGIONS 里登记
    的地区都会自动出块 —— 新增地区只需往 `tents.REGIONS` 加一项（见该文件注释）。
    """
    rows = _tents.region_locations(region, _tents.seed_of(syndicates))
    if not rows:
        return []
    # ★ 2026-10-02 改版（维护者要求）：纵列改**底部横向三块**——每点位
    # 「标题｜任务…」用 ⟦tents⟧ 分隔串成一行机器行，渲染层拆块画框 +
    # 蓝色高亮指定任务；文本模式由 render.text_card 展开回纵列。
    # ⚠️ 机器行内不要再出现全角空格 —— 渲染层 _split_cells 按它切列。
    out = ["⟦tents⟧" + "⟦tents⟧".join(f"{label}｜{'｜'.join(names)}" for label, names in rows)]
    out.append(
        (_TENT_NOTE_EN if i18n.is_en() else _TENT_NOTE).get(region)
        or L(
            "※ 以上为当前赏金点位（按 DE 世界种子推算，与游戏内一致）",
            "※ The above are the current bounty locations (derived from the DE world seed, matches the game)",
        )
    )
    return out


def _heist_block(title: str) -> list[str]:
    """「赏金 抢劫」：金星抢劫奖励池（DE 不下发抢劫轮换，列官方导出池）。"""
    pool = (_BOUNTY_POOLS.get("Solaris United") or {}).get("抢劫") or {}
    rots = [r for r in ("A", "B", "C") if pool.get(r)]
    if not rots:
        return [
            L(f"◆ {title} · 抢劫", f"◆ {title} · Heists"),
            L("　　暂无抢劫奖励池数据", "　　No heist reward pool data"),
        ]
    same = all(pool[r] == pool[rots[0]] for r in rots)
    items: list = []
    for r in rots[:1] if same else rots:
        for t in pool[r]:
            if t not in items:
                items.append(t)
    return [
        L(f"◆ {title} · 抢劫", f"◆ {title} · Heists"),
        "　　" + _disp_items(_fmt_pool_items(items)),
        L(
            "※ 抢劫不在 DE 赏金轮换下发里，上列为官方导出的抢劫奖励池"
            + ("（A/B/C 三轮相同）" if same else "（A/B/C 合并）"),
            "※ Heists are not part of DE's bounty rotation; the list above is the official exported heist reward pool"
            + (" (rounds A/B/C are identical)" if same else " (A/B/C combined)"),
        ),
    ]


def fmt_bounties(
    syndicates: Iterable[dict], keyword: str = "", cycle: Optional[dict] = None
) -> tuple[str, list[str]]:
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
    别名已于 2026-09-12 删除（维护者要求），发了会落到「未识别地区」提示。
    """
    cycle = cycle or {}
    oracle = cycle.get("bounties") or {}

    target = None
    btitle = L("赏金任务", "Syndicate Bounties")
    _kl = (keyword or "").lower()
    if keyword:
        for k, v in _CONTINENT_TARGET.items():
            if k in keyword or (k.isascii() and k in _kl):
                target = v
                break
        if target is None:
            return (
                btitle,
                [
                    L(
                        f"未识别地区「{keyword}」。可用关键词："
                        "地球 / 金星 / 火卫二（隔离库）/ 扎里曼 / 圣所（实验室）/ 1999",
                        f'Unrecognized region "{keyword}". Available keywords: '
                        "earth / venus / deimos (vault) / zariman / sanctum (lab) / 1999",
                    ),
                    L(
                        "例：赏金 地球｜赏金 隔离库｜赏金 扎里曼｜赏金 圣所｜赏金 1999",
                        "e.g. bounty earth | bounty vault | bounty zariman | bounty sanctum | bounty 1999",
                    ),
                ],
            )

    lines: list[str] = []
    boards: set = set()
    detailed = target is not None
    # ★ 2026-10-11：「深矿」「抢劫」聚焦到对应档位（旧行为：等同「赏金 金星」整板）
    focus = (
        next((w for w in ("深矿", "抢劫") if w in keyword), "")
        or ("深矿" if ("nokko" in _kl or "deep mine" in _kl) else "")
        or ("抢劫" if "heist" in _kl else "")
        if target == "Solaris United"
        else ""
    )
    rot = (cycle.get("rot") or "").strip()
    # ★ C2：oracle 地区的轮换倒计时（cycle.expiry 与 DE Syndicates[].Expiry 同口径）。
    #   注意 oracle 的 expiry 是 **epoch 毫秒整数**（实测 1791047752866），统一转
    #   ISO 再交给 countdown（countdown/parse_iso 只吃 ISO 串）。
    _raw_exp = cycle.get("expiry")
    if isinstance(_raw_exp, (int, float)) and _raw_exp > 0:
        cyc_expiry = datetime.fromtimestamp(_raw_exp / 1000, tz=timezone.utc).isoformat()
    else:
        cyc_expiry = str(_raw_exp or "").strip()
    community_shown = False  # 本卡是否真的画了社区观测档（注脚按此出现）

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
        title = _synd_title(pool_key)
        tag = _ORACLE_TAGS.get(pool_key)
        if tag is not None:
            # ② DE 不下发 Jobs、只能靠 oracle 拿节点与挑战的地区
            bounty_list = oracle.get(tag) or []
            if detailed:
                lines.extend(
                    _oracle_region_block(pool_key, tag, title, bounty_list, rot, cyc_expiry)
                )
            else:
                lines.extend(_oracle_summary(pool_key, tag, title, bounty_list, rot, cyc_expiry))
            continue
        # ① DE 直接下发 Jobs 的地区：地球 / 金星 / 火卫二
        s = de_by_synd.get(pool_key)
        jobs = (s or {}).get("jobs") or []
        if not jobs:
            continue
        if detailed and focus == "抢劫":
            lines.extend(_heist_block(title))
            continue
        if detailed and focus == "深矿":
            jobs = [j for j in jobs if "NokkoColony" in (j.get("rewardTable") or "")]
            if not jobs:
                lines.append(
                    L(f"◆ {title}　当前没有深矿档", f"◆ {title}　No Deep Mines tier right now")
                )
                continue
        if detailed:
            lines.extend(_region_block(pool_key, title, jobs, (s or {}).get("expiry", "")))
            if any(j.get("source") == "community" for j in jobs):
                community_shown = True
            if focus:
                continue
            if pool_key in _tents.REGIONS and _tents.on_card(pool_key):
                # 点位级赏金：DE 不下发归属（全库无 camp 字段），由世界种子 +
                # JobManifest 确定性推算，见 core/tents.py。凡登记在 REGIONS 且
                # on_card 的地区都出块（地球小帐篷 A/B/C；金星已登记但当前不上卡）。
                # 地区都出块（地球小帐篷 A/B/C、金星七处点位…）。
                lines.extend(_tent_lines(syndicates, pool_key))
        else:
            lines.extend(_region_summary(pool_key, title, jobs, (s or {}).get("expiry", "")))

    if not lines:
        have = [_synd_title(x) for x in sorted(boards)]
        return (
            btitle,
            [
                L(
                    f"未找到对应地区赏金。当前有赏金的板：{'、'.join(have) or '无'}",
                    f"No bounties found for that region. Boards with bounties: {', '.join(have) or 'None'}",
                ),
                L(
                    "（扎里曼 / 实验室 / 1999 赏金为轮换开放，DE 不实时下发）",
                    "(Zariman / Lab / 1999 bounties open on rotation; DE does not push them live)",
                ),
            ],
        )
    if detailed:
        lines.append(
            L(
                "※ ▣ 部件/蓝图　★ MOD　列每档全部奖励；轮次随刷新而变",
                "※ ▣ Parts/Blueprints　★ Mods　all rewards of each tier are listed; the round changes on refresh",
            )
        )
        if community_shown:
            # 社区观测档的轮次无从得知（DE 不下发该档）→ 卡面直接说明列的是全轮次；
            # 深矿档另有两件必须说清的事（都是 2026-09-26 实测/官方导出对拍结论）：
            #   ① 任务名在三个任务间轮换（沃沃 10:51 显示「解放小动物」，用户 10:5x 是
            #      「企业重组」）⇒ 档名不写死任务，这里说明轮换集合；
            #   ② 社区版（沃沃）该档另含金星资源 ×20，而**官方钢铁表不含资源项**
            #      （实测：普通 A/B/C 各带一种资源，Steel/SteelB/SteelC 一个都没有）。
            lines.append(
                L(
                    "※ 「社区观测」档 DE 不下发、轮次无从得知，上列为 A/B/C 全部轮次；"
                    "深矿任务在 解放小动物／企业重组／花园除草 间轮换（DE 不下发"
                    "当前是哪一档），社区版另含金星资源 ×20，官方钢铁表不含资源项",
                    '※ "Community-observed" tiers are not sent by DE and their round is unknown, so all A/B/C rounds are listed; '
                    "Deep Mines missions rotate between Free the Critters / Corporate Restructuring / Weeding the Garden "
                    "(DE does not say which one is current); the community version also includes Venus resources x20, "
                    "while the official Steel Path table has no resource entries",
                )
            )
    else:
        lines.append(
            L(
                "※ ▣ 部件/蓝图　★ MOD　轮换行只列高价值奖励",
                "※ ▣ Parts/Blueprints　★ Mods　the rotation line lists high-value rewards only",
            )
        )
        lines.append(
            L(
                "※ 发「赏金 地球」「赏金 扎里曼」等看该地区各档完整奖励",
                '※ Send "bounty earth", "bounty zariman" etc. for the full rewards of each tier',
            )
        )
    return (btitle, lines)


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
        zh = None if i18n.is_en() else _SYNTH_ZH.get(name)
        if not zh and not i18n.is_en():
            # 「XXX [Research]」是 Simaris 结合扫描器里的**研究变体**标记，
            # 静态库里存的是基底名（Anti MOA / Lancer / Crewman …），
            # 所以剥掉后缀再查一次、并把标记译作「研究版」。
            # 不这样处理时这 7 条会整条回落成英文（维护者 2026-09-17 反馈
            # 「汉化缺失」的根因）。
            _m = re.match(r"^(?P<base>.+?)\s*\[Research\]$", name)
            if _m:
                _base_zh = _SYNTH_ZH.get(_m.group("base").strip())
                if _base_zh:
                    zh = f"{_base_zh}·研究版"
        # 中文优先，英文名留着便于对照（结合扫描器里看到的标记还是英文）
        shown = f"{zh}（{name}）" if zh else name  # 英文模式 zh 恒为空，直接显示官方英文名
        # ★ 2026-09-27 列对齐：第二格原来把「节点·类型（派系）」整块当一列 ⇒
        #   节点列左边界随名称长短抖动（用户截图实证）。拆成
        #   「名称 / 节点 / 类型（派系）」三列，进渲染层的列对齐。
        tail = L(f"{mtype}（{fac}）" if fac else mtype, f"{mtype} ({fac})" if fac else mtype)
        lines.append(f"· {shown}　{place}　{tail}")
    title = L("结合仪式目标（39 种，静态库）", "Synthesis Targets (39, static)")
    if not lines:
        return (title, [L("结合目标表为空", "The synthesis target table is empty")])
    lines = [
        L(f"◆ 共收录：{len(lines)} 种（静态库）", f"◆ Total: {len(lines)} (static library)"),
        L("◆ 目标一览", "◆ Target List"),
        L("　名称　节点　类型（派系）", "　Name　Node　Type (Faction)"),
        *lines,
    ]
    return (title, lines)


def fmt_construction(data: Optional[dict]) -> tuple[str, list[str]]:
    """舰队建造进度 + 当前态势。

    只说百分比没有意义 —— 需要交代「这意味着什么、到 100% 会发生什么、现在有没有在打」。

    Args:
        data: ``_parse_construction`` 的产物。
    """
    title = L("舰队建造进度", "Fleet Construction")
    if not data:
        return (title, [L("数据暂不可用", "Data unavailable")])
    projects = data.get("projects") or []
    assaults = data.get("assaults") or []
    S = "　"
    lines: list[str] = []
    notes: list[str] = []

    if assaults:
        lines.append(
            L(
                f"◆ 袭击状态：进行中 {len(assaults)} 场",
                f"◆ Assault status: {len(assaults)} in progress",
            )
        )
        for a in assaults:
            extra = []
            if a.get("health") is not None:
                extra.append(
                    L(
                        f"舰体完整度 {a['health'] * 100:.0f}%",
                        f"Hull integrity {a['health'] * 100:.0f}%",
                    )
                )
            if a.get("expiry"):
                extra.append(_left(countdown(a["expiry"])))
            notes.append(
                L(
                    f"※ ⚠ 袭击进行中：{a['name']} → {a['victim'] or '一座中继站'}",
                    f"※ ⚠ Assault in progress: {a['name']} → {a['victim'] or 'a relay'}",
                )
                + ("｜" + "｜".join(extra) if extra else "")
            )
    elif data.get("assaults_unknown"):
        lines.append(
            L(
                "◆ 袭击状态：社区源未提供详情，当前状态未知",
                "◆ Assault status: the community source gave no details; current status unknown",
            )
        )
    else:
        lines.append(
            L(
                "◆ 袭击状态：当前无进行中的袭击事件",
                "◆ Assault status: no assault events in progress",
            )
        )

    if projects:
        lines += [
            L("◆ 建造项目", "◆ Construction Projects"),
            L(
                f"{S}项目{S}阵营{S}进度{S}满 100% 后开启（3 天）",
                f"{S}Project{S}Faction{S}Progress{S}Opens at 100% (3 days)",
            ),
        ]
    for p in projects:
        fac = faction_name(
            {"Grineer": "FC_GRINEER", "Corpus": "FC_CORPUS"}.get(p["faction"], p["faction"])
        )
        lines.append(
            L(
                f"· {p['name']}（{p['en']}）{S}{fac}{S}{p['pct']:.0f}%{S}「{p['event']}」战术警报",
                f'· {p["en"] or p["name"]}{S}{fac}{S}{p["pct"]:.0f}%{S}"{p["event"]}" Tactical Alert',
            )
        )

    if not projects and not assaults:
        lines.append(
            L(
                "◆ 建造进度：两个阵营均为 0（刚结束一轮事件）",
                "◆ Construction progress: both factions at 0 (a round of events just ended)",
            )
        )

    lines += notes
    lines.append(
        L(
            "※ 建造进度为全服累计：入侵任务中支持 Grineer 计入巴罗尔巨人战舰、"
            "支持 Corpus 计入利刃豺狼舰队；战术警报期间该阵营攻击一座中继站，把舰体完整度打到 0% "
            "即结束，进度归零重新累积",
            "※ Progress is server-wide: supporting Grineer in invasions builds the Balor Fomorian, "
            "supporting Corpus builds the Razorback Armada; during the Tactical Alert that faction "
            "attacks a relay and it ends when hull integrity reaches 0%, then progress resets and accumulates again",
        )
    )
    return (title, lines)


_ACRITHIS_EN = {
    "Kuva": "Kuva",
    "FormaBlueprint": "Forma Blueprint",
    "RawMeleeRandomMod": "Melee Riven Mod",
    "RawPistolRandomMod": "Pistol Riven Mod",
    "RawRifleRandomMod": "Rifle Riven Mod",
    "RawShotgunRandomMod": "Shotgun Riven Mod",
    "RawSentinelWeaponRandomMod": "Companion Weapon Riven Mod",
    "WeaponPrimaryArcaneUnlocker": "Primary Arcane Adapter",
    "WeaponSecondaryArcaneUnlocker": "Secondary Arcane Adapter",
    "UtilityUnlocker": "Exilus Warframe Adapter",
    "WeaponUtilityUnlocker": "Exilus Weapon Adapter",
    "OrokinCatalystBlueprint": "Orokin Catalyst Blueprint",
    "OrokinReactorBlueprint": "Orokin Reactor Blueprint",
}

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


def fmt_acrichis_week(data: dict) -> tuple[str, list[str]]:
    """言录使**本周货单**（社区维护快照，含价格）。

    重置规则见 ``core/data/rotations.json`` 的 ``acrichis.reset_*``：
    wiki《Acrithis》**实时**页面写的是「Resets every **Monday** 0:00 UTC」，
    与其它周常同点（Update 32.3 起统一到周一）。实际刷新时刻按显示时区格式化。
    """
    S = "　"
    items = data.get("items") or []
    rows = []
    for it in items:
        nm = it.get("name") or "?"
        qty = f" ×{it['qty']}" if int(it.get("qty", 1) or 1) != 1 else ""
        # 结算货币是 Pathos Clamp，官方简中为**「苦栓」**
        # （/Lotus/Language/Duviri/DuviriDragonDropItemName），
        # 2026-09-17 维护者指正；此前误写成「精华」。
        rows.append(
            L(
                f"· {nm}{qty}{S}{it.get('price', '?')} 苦栓",
                f"· {nm}{qty}{S}{it.get('price', '?')} Pathos Clamps",
            )
        )
    nxt = data.get("next_reset") or data.get("expiry")
    head = L(f"◆ 本周货单：{len(items)} 件", f"◆ This week's inventory: {len(items)} items")
    if nxt:
        head += L(
            f"｜距下次刷新 剩余 {countdown(nxt)}",
            f" ｜ Refresh: {_left_en(countdown(nxt))}",
        )
        when = format_time(nxt)
        if when:
            head += L(f"（{when}）", f" ({when})")
    lines = [head]
    if rows:
        lines += [
            L("◆ 货单", "◆ Inventory"),
            L(f"{S}商品{S}价格", f"{S}Item{S}Price"),
            *rows,
        ]
    if data.get("_price_note"):
        lines.append("※ " + data["_price_note"])
    lines.append(
        L(
            "※ 每周一 00:00 UTC 轮换（与其它周常同点）；本周货单为社区维护快照，DE 不下发",
            "※ Rotates every Monday 00:00 UTC (same as other weekly resets); this week's inventory is a community-maintained snapshot, not sent by DE",
        )
    )
    return (L("言录使（Acrithis）本周货单", "Acrithis Weekly Inventory"), lines)


def fmt_acrichis(
    data: Optional[dict], stale: bool = False, pending: bool = False
) -> tuple[str, list[str]]:
    """言录使（Acrithis）商品**池**。

    ⚠️ DE 不下发每周实际卖哪 5 件（``AcrithisVendorManifest`` 只有池子 + 权重，
    ``numRandomItemPrices`` 说明价格也是每周期随机 roll）。所以这里给的是
    「周常槽会出什么、权重多少」—— 参考机器人那张「本周 5 件」是它自己维护的
    快照，我们不做没有来源的数据。

    ``stale=True`` 表示本周货单快照已过期（此时展示的是候选池），
    必须**明确说出来**，不能让人误以为这就是本周实际在卖的 5 件。
    """
    if pending:
        title, lines = fmt_acrichis(data, stale=False)
        return title + L("（货单待获取）", " (Inventory Pending)"), [
            L(
                "◆ 本周货单待获取，下表仅为候选池",
                "◆ This week's inventory is pending; the table below is only the candidate pool",
            ),
            *lines,
        ]
    bins = (data or {}).get("bins") or {}
    weekly = bins.get("2") or []
    _stale_sfx = L("（货单待更新）", " (Inventory Outdated)")
    if not weekly:
        lines = [
            L(
                "◆ 商品池：暂无数据（ExportVendors 取不到）",
                "◆ Item pool: no data (ExportVendors unavailable)",
            )
        ]
        # ★ 过期提示不能因为「池子也没数据」就被吞掉 —— 那正是最容易让人
        #   以为「查到了」的场景
        if stale:
            lines.append(
                L(
                    "※ ⚠ 本周 5 件货单已过期未更新，请以游戏内 Acrithis 为准",
                    "※ ⚠ This week's 5-item inventory is outdated; check Acrithis in game",
                )
            )
        return (L("言录使（Acrithis）", "Acrithis") + (_stale_sfx if stale else ""), lines)
    S = "　"
    n_daily = sum(len(bins.get(b) or []) for b in ("0", "1", "3"))
    lines = [
        L("◆ 状态：", "◆ Status: ")
        + (
            L(
                "本周货单已过期未更新，下表仅为候选池",
                "This week's inventory is outdated; the table below is only the candidate pool",
            )
            if stale
            else L(
                "候选池与权重（DE 不下发本周实际货单）",
                "Candidate pool and weights (DE does not send this week's actual inventory)",
            )
        ),
        L(
            f"◆ 每日槽：另有 {n_daily} 件（船装装饰 / 拍照场景 / 小队增益，每日轮换）",
            f"◆ Daily slots: {n_daily} more (ship decor / photo scenes / squad boosts, rotating daily)",
        ),
        L("◆ 周常池（每个槽位每周期出 1 件）", "◆ Weekly pool (1 item per slot each cycle)"),
        L(f"{S}商品{S}权重", f"{S}Item{S}Weight"),
    ]
    for it in weekly:
        if i18n.is_en():
            nm = _ACRITHIS_EN.get(it.get("en") or "", it.get("en") or it.get("name") or "?")
        else:
            nm = _ACRITHIS_ZH.get(it.get("en") or "", it.get("name") or it.get("en") or "?")
        qty = f" ×{it['qty']}" if int(it.get("qty", 1) or 1) != 1 else ""
        lines.append(f"· {nm}{qty}{S}{it.get('pct', 0)}%")
    if stale:
        lines.append(
            L(
                "※ ⚠ 本周 5 件货单已过期未更新，上面只是候选池 —— "
                "本周实际在卖什么请以游戏内 Acrithis 为准",
                "※ ⚠ This week's 5-item inventory is outdated; the list above is only the candidate pool — "
                "check Acrithis in game for what is actually on sale",
            )
        )
    else:
        lines.append(
            L(
                "※ 价格每周期随机 roll，DE 不下发本周实际货单；"
                "上表是池子与权重，本周实际 5 件以游戏内 Acrithis 为准",
                "※ Prices are rolled randomly each cycle and DE does not send the actual inventory; "
                "the table is the pool and weights; check Acrithis in game for this week's 5 items",
            )
        )
    return (
        L("言录使（Acrithis）商品池", "Acrithis Item Pool") + (_stale_sfx if stale else ""),
        lines,
    )


def fmt_incursions(data: Optional[dict]) -> tuple[str, list[str]]:
    """钢铁之路侵袭（每日 6 个钢路节点，重置后换一批）。

    节点等级 = 原节点等级 + 100（钢路规则）；任务类型用节点的官方简中
    ``nodes_zh[key]["type"]``（ExportRegions 直出）。行按「类型/派系/等级/地点」
    四格用全角空格分开，渲染层开列对齐（_table_mode）。
    """
    nodes = (data or {}).get("nodes") or []
    title = L("钢铁之路侵袭", "Steel Path Incursions")
    if not nodes:
        return (
            title,
            [
                L(
                    "◆ 今日侵袭：暂无排期（browse.wf 排期表取不到）",
                    "◆ Today's incursions: no schedule (browse.wf schedule unavailable)",
                )
            ],
        )
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
    _cd = countdown(data.get("expiry") or "")
    lines = [
        L(
            f"◆ 今日侵袭：{len(nodes)} 个节点｜每日重置 剩余 {_cd}",
            f"◆ Today's incursions: {len(nodes)} nodes ｜ Daily reset: {_left_en(_cd)}",
        ),
        L("◆ 侵袭节点", "◆ Incursion Nodes"),
        L("　类型　派系　等级　地点", "　Type　Faction　Level　Location"),
        *lines,
        L(
            "※ 钢路节点等级 = 原节点 +100，完成任一侵袭可得钢铁精华",
            "※ Steel Path node level = original level +100; completing any incursion gives Steel Essence",
        ),
    ]
    return (title, lines)


def _steel_level(level: str) -> str:
    """「5-7」→「105-107」；单个数字也兼容。"""
    parts = level.replace("级", "").split("-")
    try:
        nums = [int(p) for p in parts if p.strip()]
    except ValueError:
        return level
    if not nums:
        return level
    if i18n.is_en():
        return "Lv " + "-".join(str(n + 100) for n in nums)
    return "-".join(str(n + 100) for n in nums) + "级"


def _enemy_display(enemy: str) -> str:
    """solNodes 的 enemy 字段 -> 展示名（与裂隙卡同一套规则）。"""
    e = (enemy or "").strip()
    if not e or e in ("Crossfire", "Tenno", "Duviri"):
        return ""
    if i18n.is_en():
        return e
    return {"The Murmur": "低语者", "Orokin": "奥罗金"}.get(e, e)


# 沉沦之地（炼狱塔）任务类型的中文叫法 —— **兜底表**（主路径查官方键）。
# 主路径：`core.de_worldstate.descent_type_zh()` 按 DT_* 代码推 CircleOfHell 官方键；
# 本表只在「语言表缺失 / 官方将来改了键名」时兜底，**表内每条都必须与官方值逐字一致**
# （`tests/test_de_worldstate.py` 会复算，不一致即红）。
# 唯一的例外是 `DT_PROTOFRAME` / `DT_DEFENSE_PROTECT`：实测官方无键、且参考卡逐层
# 对过，按老注释保留手译（见 `_DESCENT_TYPE_KEEP_CN`）。
_DESCENT_ZH = {
    "DT_INFESTED_SALVAGE": "净化",
    "DT_CAPTURE": "传承种捕获",
    "DT_PRESURE_GAUGE": "压力锅",
    "DT_SABOTAGE_HIVE": "清巢",
    "DT_LOOT_CREATURES": "贪囤断肢劫掠",  # 2026-09-27 订正：贪「困」-> 贪「囤」（官方 CoHCreaturesLoot）
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

# 官方无键、但**参考卡实机对过**因而保留的手译（其余手译条目必须与官方一致）。
# ★ 2026-09-29：`DT_PROTOFRAME` 的自编名「战甲祈运」**卡面已弃用**——
#   7/14/21 层走 `_PROTOFRAME_FLOOR` 特例（主标=官方圣所名 + 机制标注）；
#   本表条目仅当**未知 Protoframe 代码**出现时兜底（不再与官方
#   `CoHGamemodeShrineDefense`=「祈运坛防御」在卡面撞词）。
# ★ `DT_DEFENSE_PROTECT`（「保护人物」）为**自编**：2026-09-29 实测 payload
#   9 个候选文件零命中（属预埋代码）；待实际出现时按官方定名后再替换。
_DESCENT_TYPE_KEEP_CN = frozenset({"DT_PROTOFRAME", "DT_DEFENSE_PROTECT"})

# 沉沦 7/14/21 层（DT_PROTOFRAME，检查点）的机制标注：
#   Challenge 代码 -> 右侧机制文案（主标取官方圣所名，见 fmt_descendia）。
#   依据（2026-09-29 审核方实测）：EN wiki《The Descendia》Blessings 段——
#   「hosting Marie's Sanctuary and Lyon's Sanctuary respectively. They each offer
#    **one of two selectable buffs**, with Lyon's having **an additional debuff**」
#   ⇒ 祝福为玩家**二选一**（非随机）；21 层为终局，打败罗瑟。
_PROTOFRAME_FLOOR = {
    "Wisp": "祝福二选一",
    "Harrow": "祝福二选一·附代价",
    "Devil": "击败罗瑟",
}
_PROTOFRAME_FLOOR_EN = {
    "Wisp": "Blessing: pick 1 of 2",
    "Harrow": "Blessing: pick 1 of 2 · with a cost",
    "Devil": "Defeat the final boss",
}


# 沉沦之地（炼狱塔）「目标」列 —— **兜底表**（主路径查官方键）。
#
# 旧版说「DE 只给内部代码、不给显示文案」是**错的**：`/Lotus/Language/CircleOfHell/`
# 官方有 268 键（含 68 个 `CoHChallenge*`）。主路径改走
# `core.de_worldstate.descent_goal_zh()` 的 8 级官方键解析；本表降级为兜底，
# 且**表内每条都必须与官方值逐字一致**（测试复算）。
#
# 2026-09-27 按官方值订正 13 条（原手译与官方不符）：
#   HardShell 硬壳→冰封卓越者军团 ｜ PowerHouse 强力增幅→力量贪婪卓越者军团
#   JumpSmash 跌头者→跺头者 ｜ SpicyKnife 折弹→拆弹
#   BasicLootCreatures 阻止贪困断肢→贪囤断肢劫掠 ｜ JadeGuardian 翡翠守护者卓越者军团→翠玉卓越者军团
#   GiantRealm 巨大化→巨人症 ｜ Sentients 陶之复仇→Tau 的复仇
#   HordeWeakpoints 弱点群体→弱点敌群 ｜ UnseenFoes 隐藏威胁→潜在威胁
#   FieryTrail 火焰轨迹→防火道 ｜ HorseCombatOnly 只能骑乘战斗→绝灵骥战斗
#   BasicLoot/BasicRace/BasicMimics 搜寻资源/穿过闸门赛跑/拟态→掠夺/时间试炼/打开容器
# 另：`VoidAberration` 一族（含 `NC_` 变体）官方**确实无键**，已从本表删除 ——
#    照 9-27 裁定「官方没有的写法 ≠ 不存在的东西，但也不许硬编」，卡面留英文 + 注脚。
_DESCENT_GOAL_ZH = {
    "VeryToxic": "毒蛭吸血卓越者军团",
    "GrenadesOnly": "易受元素瓶攻击的敌人",
    "SlipAndSlide": "无摩擦",
    "RangedArcadiaOnly": "泡泡枪",
    "BasicLootCreatures": "贪囤断肢劫掠",
    "Sunlight": "太阳神之怒",
    "HeadShotsOnly": "只有弱点才会受到伤害",
    "GlassMaker": "玻璃匠中枢人",
    "Manics": "躁狂症",
    "BasicRace": "时间试炼",
    "FreezeInShoot": "冰封之光卓越者军团",
    "NC_SecuritySpin": "激光炼狱",
    "BasicBreakTargets": "摧毁全息球",
    "SpicyKnife": "拆弹",
    "JumpSmash": "跺头者",
    "HeavyWeaponsOnly": "易受曲翼枪械攻击的敌人",
    "BasicLoot": "掠夺",
    "Devil": "罗瑟的遗忘",
    "Harrow": "里昂的圣所",
    "ArchonBoreal": "执刑官诡文枭主",
    "HorseCombatOnly": "绝灵骥战斗",
    "FireAndIce": "冰火卓越者军团",
    "ShockingLeech": "电击吸血卓越者军团",
    "JadeGuardian": "翠玉卓越者军团",
    "GiantRealm": "巨人症",
    "Sentients": "Tau 的复仇",
    "HordeWeakpoints": "弱点敌群",
    "UnseenFoes": "潜在威胁",
    "FieryTrail": "防火道",
    "HardShell": "冰封卓越者军团",
    "BasicMimics": "打开容器",
    "PowerHouse": "力量贪婪卓越者军团",
}


def _pretty_code(code: str) -> str:
    """把 DE 的驼峰内部代码拆成可读英文（``FireAndIce`` → ``Fire And Ice``）。

    复杂化每周换新，表不可能覆盖全部；回落时至少别把 ``HorseCombatOnly``
    这种一坨驼峰直接甩给用户。
    """
    if not code:
        return ""
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", code)


def descent_type_label(code: str, eng: str = "") -> tuple[str, bool]:
    """任务类型列 -> ``(卡面文本, 是否官方中文)``。

    优先级：CircleOfHell 官方键 → **官方任务类型表**（`missionTypes.json` →
    `mission_types_zh.json`，如 `Assassination` = 刺杀）→ 手译兜底表 →
    `DT_UNIQUE`（官方无键、wiki 未收录）留英文原文。
    """
    zh = descent_type_zh(code)
    if zh:
        return zh, True
    if code in _DT_NO_KEY:
        return (eng or code), False
    return (
        (None if i18n.is_en() else _DESCENT_ZH.get(code))
        or mission_type_zh_by_name(eng)
        or mission_cn(eng or code)
        or code
    ), True


def descent_goal_label(code: str) -> tuple[str, bool]:
    """目标列 -> ``(卡面文本, 是否官方中文)``。

    官方键优先（含不在 CircleOfHell 命名空间的少数几个）→ 手译兜底表 →
    可读英文（``FireAndIce`` -> ``Fire And Ice``；显示时剥 DE 的内部变体标记 ``NC_``）。
    """
    zh = descent_goal_zh(code)
    if zh:
        return zh, True
    fallback = None if i18n.is_en() else _DESCENT_GOAL_ZH.get(code)
    if fallback:
        return fallback, True
    shown = code[3:] if code.upper().startswith("NC_") else code
    return _pretty_code(shown), False


def fmt_descendia(data: Optional[dict]) -> tuple[str, list[str]]:
    """沉沦之地（炼狱塔）：每周 21 层，每层一个任务类型 + 一个目标（复杂化）。

    * **任务类型**：DE 的 ``Type`` 代码（``DT_*``）→ CircleOfHell 官方简中
      （``CoHGamemode*``），无键者留英文（见 :func:`descent_type_label`）。
    * **目标**：DE 的 ``Challenge`` 代码 → 官方简中（8 级解析，见
      :func:`core.de_worldstate.descent_goal_zh`），无键者留可读英文 + 注脚。
    * **7/14/21 层特例（2026-09-29 口径）**：这三层是检查点、走
      ``DT_PROTOFRAME``；主标改用官方圣所名（``CoHProtoframe{Wisp,Harrow,Devil}``
      键值），右侧标机制（祝福二选一／附代价／击败罗瑟）——**弃用自编「战甲祈运」**
      （与官方 ``CoHGamemodeShrineDefense``=「祈运坛防御」撞词，属另一个模式）。
    * 行用全角空格分格，渲染层按列对齐并给类型上色。
    """
    chs = (data or {}).get("challenges") or []
    title = L("沉沦之地 · 炼狱塔", "Descendia")
    if not chs:
        return (title, [L("暂无数据（DE Descents 取不到）", "No data (DE Descents unavailable)")])
    lines = [
        L("◆ 本周：", "◆ This week: ") + _left(countdown(data.get("expiry") or "")),
        L(f"◆ 本周 {len(chs)} 层", f"◆ This week: {len(chs)} floors"),
    ]
    no_key: list[str] = []
    caveat: list[str] = []
    for c in chs:
        code = c.get("Type") or ""
        _goal_code = c.get("code") or ""
        label, type_ok = descent_type_label(code, c.get("type") or "")
        goal, goal_ok = descent_goal_label(_goal_code)
        mech = (_PROTOFRAME_FLOOR_EN if i18n.is_en() else _PROTOFRAME_FLOOR).get(_goal_code)
        if code == "DT_PROTOFRAME" and mech and goal_ok and goal:
            # 检查点层：主标 = 官方圣所名，右侧 = 机制（不再出类型列）
            lines.append(L("· 炼狱", "· Floor") + f" [{c.get('index')}]　{goal}　{mech}")
            continue
        if not type_ok:
            no_key.append(code)
        if not goal_ok:
            no_key.append(_goal_code)
        if _goal_code in _DESCENT_GOAL_CAVEAT:
            caveat.append(f"{_goal_code}={goal}")
        idx = c.get("index")
        lines.append((L("· 炼狱", "· Floor") + f" [{idx}]　{label}　{goal}").rstrip())
    lines.append(
        L(
            "※ 每周轮换 21 层（7/14/21 层为检查点）；中文名取自游戏内官方文案，进本前以实机为准",
            "※ 21 floors rotate weekly (floors 7/14/21 are checkpoints); names come from the in-game official text, verify in game before entering",
        )
    )
    if no_key and not i18n.is_en():
        # 「看不到的不描述成事实」：官方确实没有文案的层，明说保留内部代码
        # （英文模式下无官方键的层已把内部代码拆成可读英文，不再逐个列出）
        lines.append("※ 官方无简中、保留内部代码：" + "、".join(no_key))
    if caveat:
        # 「来源存疑就标明来源」：这些目标的官方键**不在沉沦之地命名空间**，
        # 是跨族借用的（见 de_worldstate._DESCENT_GOAL_EXTERNAL 的说明）。
        lines.append(
            L(
                "※ 以下译名取自官方「科研风险」键（非沉沦之地命名空间），"
                "与 wiki 该层挑战名对应，待实机确认：" + "、".join(caveat),
                "※ The names below come from official Archimedea risk keys (not the Descendia namespace); "
                "they match the wiki's floor challenge names, pending in-game confirmation: "
                + ", ".join(caveat),
            )
        )
    return (title, lines)


def fmt_archimedea(data: Optional[dict], title: str) -> tuple[str, list[str]]:
    """深层科研 / 时光科研。

    结构（来自 DE 的 ``Conquests``）：3 个任务 × 「普通 / 硬化」两档，
    每档各有一个**偏差**（deviation）和若干**风险**（risks），
    另有 4 个**可选个人减益**（Variables）。
    """
    if not data:
        return (
            title,
            [
                L(
                    "该数据源暂未开放（endpoint 未接入或返回为空）",
                    "This data source is not available yet (endpoint not connected or returned empty)",
                )
            ],
        )
    S = "　"
    lines = []
    _hard_tags = ("硬化", "Elite")
    _sep = L("、", ", ")
    if data.get("expiry"):
        lines.append(L("◆ 本周：", "◆ This week: ") + _left(countdown(data["expiry"])))
    missions = data.get("missions") or []
    if not missions:
        return (title, lines + [L("◆ 本周任务：暂无数据", "◆ This week's missions: no data")])
    # 三个任务 × 普通/硬化：一行一个任务，「偏差 / 风险 / 硬化额外风险」分列
    # （2026-10-11 第二步改版；旧版每条风险占一行、共 10 余行缩进文字）
    lines += [
        L("◆ 本周任务", "◆ This Week's Missions"),
        L(
            f"{S}任务{S}派系{S}偏差{S}风险{S}硬化额外风险",
            f"{S}Mission{S}Faction{S}Deviation{S}Risks{S}Elite extra risks",
        ),
    ]
    for m in missions:
        mt = mission_cn(m.get("missionType", ""))
        diffs = m.get("difficulties") or []
        if not diffs:
            # 旧 Descents 结构：只有一串风险名
            dev, risks, extra = "—", _sep.join(m.get("risks") or []) or "—", "—"
        else:
            shown_hard = any(d.get("tag") in _hard_tags for d in diffs)
            dev = next((d["deviation"] for d in diffs if d.get("deviation")), "—")
            # 普通/硬化的偏差与普通风险相同，只列硬化的额外风险即可，避免整段重复
            risks = _sep.join(diffs[0].get("risks") or []) or "—"
            hard = next((d for d in diffs if d.get("tag") in _hard_tags), None)
            base = set(diffs[0].get("risks") or [])
            extra_l = (
                [r for r in (hard.get("risks") or []) if r not in base]
                if hard and shown_hard
                else []
            )
            extra = _sep.join(extra_l) or "—"
        lines.append(f"· {mt}{S}{m.get('faction') or '—'}{S}{dev}{S}{risks}{S}{extra}")
    if data.get("variables"):
        # 可选个人减益**带说明**：玩家要据此决定这周选哪几个减益换奖励
        # （玩家反馈「可选减益没有详细说明」—— 之前只列了名字）。名称一行、说明缩进成从属行。
        lines.append(L("◆ 可选个人减益", "◆ Optional Personal Debuffs"))
        for v in data["variables"]:
            nm = (v.get("name") or "").strip()
            if not nm:
                continue
            desc = (v.get("description") or "").strip().replace("\n", " ")
            desc = re.sub(r"<[^<>]{0,40}>", "", desc).strip()
            lines.append(f"· {nm}")
            if desc:
                lines.append(f"{S}{desc[:64]}")
    if data.get("risks"):
        lines.append(L("◆ 风险说明", "◆ Risk Descriptions"))
        for r in data["risks"][:12]:
            desc = (r.get("description") or "").strip().replace("\n", " ")
            desc = re.sub(r"<[^<>]{0,40}>", "", desc)
            lines.append(f"· {r.get('name')}")
            if desc:
                lines.append(f"{S}{desc[:60]}")
    lines.append(
        L(
            "※ 任务与偏差来自 DE 官方下发；实际进入时以游戏内为准",
            "※ Missions and deviations are sent by DE; verify in game when you enter",
        )
    )
    return (title, lines)


SEASON_CN = {"CST_SPRING": "春", "CST_SUMMER": "夏", "CST_FALL": "秋", "CST_WINTER": "冬"}
_SEASON_EN = {
    "CST_SPRING": "Spring",
    "CST_SUMMER": "Summer",
    "CST_FALL": "Fall",
    "CST_WINTER": "Winter",
}
_MONTH_EN = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


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
    "Riven Mod": "裂罅 Mod",  # 官方写法是「Mod」而非「MOD」（见 name_zh.json）
    "Riven Transmuter": "裂罅转换器",
    # 官方导出未收录此项，沿用社区通用名并与 sortie_rewards.json 保持一致
    "Legendary Core": "传说核心",
}


def _cal_name(name: str) -> str:
    """1999 日历条目名兜底汉化（**仅对英文兜底值**生效）。

    ★ 官方简中已取到时**原样返回**：下面的英文替换与「N × 名称」重排只针对
    英文兜底值，否则会把官方的「3 天经验值加成」误拆成「天经验值加成 ×3」、
    把「6000 内融核心」拆成「内融核心 ×6000」。
    """
    if not name:
        return name
    s = name.strip()
    if _HAS_CJK.search(s):
        return s
    if i18n.is_en():
        # 英文模式：名称本来就是官方英文；只把「2000 x Kuva」「6,000 Endo」规整成「Kuva ×2000」
        m = re.match(r"^([\d,]+)\s*x\s+(.+)$", s) or re.match(
            r"^(\d{1,3}(?:,\d{3})+|\d{4,})\s+(.+)$", s
        )
        return f"{m.group(2).strip()} ×{m.group(1)}" if m else s
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


def fmt_calendar(data: Optional[dict]) -> tuple[str, list[str]]:
    """1999 日历：整季日程按日期分块（沃沃式版式，2026-09-29 用户口径）。

    每块 = `◆ N　MM月DD日　段标签`（待办清单 / 选择奖励 / 增益覆写），
    内容行 `· 名　描述`（挑战带目标、覆写带效果、奖励列物品；渲染层按列对齐）。
    子模式（奖励/清单/覆写）2026-09-29 已下线——裸「日历」即全量。

    事件名的中文都来自 DE 官方简中：挑战走 ExportChallenges + 官方词表，
    奖励走 4 个命名空间 × 尾段变形，覆写走 `/Lotus/Language/1999/<尾段>Name/Desc`。
    只有官方确实无键的条目才留英文（并在卡面加注脚说明）。
    """
    title = L("1999 日历", "1999 Calendar")
    if not data or not data.get("days"):
        return (title, [L("日历数据暂不可用", "Calendar data unavailable")])
    if i18n.is_en():
        season = _SEASON_EN.get(data.get("season", ""), str(data.get("season", "")))
    else:
        season = SEASON_CN.get(data.get("season", ""), str(data.get("season", "")))
    year = data.get("yearIteration")
    lines = [
        L(f"◆ 本季：第 {year} 年 · {season}季", f"◆ Season: Year {year} · {season}")
        + (
            L(
                f"｜季末重置 剩余 {countdown(data['expiry'])}",
                f" ｜ Season ends: {_left_en(countdown(data['expiry']))}",
            )
            if data.get("expiry")
            else ""
        )
    ]
    section_cn = L(
        {"CHALLENGE": "待办清单", "REWARD": "选择奖励", "UPGRADE": "增益覆写"},
        {"CHALLENGE": "To-Do List", "REWARD": "Reward Choice", "UPGRADE": "Buff Override"},
    )
    _cnt = Counter((d["events"][0].get("type") or "") for d in data["days"] if d.get("events"))
    _sum = " · ".join(f"{section_cn[k]} {_cnt[k]}" for k in section_cn if _cnt.get(k))
    if _sum:
        lines.append(
            L(
                f"◆ 排期：共 {sum(_cnt.values())} 天有事件（{_sum}）",
                f"◆ Schedule: {sum(_cnt.values())} days with events ({_sum})",
            )
        )
    shown = 0
    for day in data["days"]:
        events = day.get("events") or []
        if not events:
            continue
        shown += 1
        date = day.get("date", "?")  # "1999-mm-dd"
        label = section_cn.get(events[0].get("type"), "")
        if i18n.is_en():
            try:
                _d = f"{_MONTH_EN[int(date[5:7]) - 1]} {date[8:10]}"
            except (ValueError, IndexError):
                _d = date[5:]
            lines.append(f"◆ {shown}　{_d}　{label}")
        else:
            lines.append(f"◆ {shown}　{date[5:7]}月{date[8:10]}日　{label}")
        for e in events:
            name = _cal_name(e.get("name", "?"))
            kind = e.get("type")
            if kind == "CHALLENGE":
                obj = _cal_objective(e)
                lines.append(f"· {name}　{obj}" if obj else f"· {name}")
            elif kind == "UPGRADE":
                desc = (e.get("desc") or "").strip()
                lines.append(f"· {name}　{desc}" if desc else f"· {name}")
            else:
                lines.append(f"· {name}")
    if shown == 0:
        lines.append(L("本季暂无排定事件", "No events scheduled this season"))
    synth = data.get("synthesized") or []
    nokey = data.get("noKey") or []
    if synth:
        # 「官方没有的写法 ≠ 不存在的东西」：官方只给基名/英文数量时按同族格式合成，
        # 但必须自曝 —— 否则用户会以为是游戏内原文。
        lines.append(
            L(
                "※ 合成译名（官方无对应文案，按同族官方格式产出）：" + "、".join(synth),
                "※ Synthesized names (no official text; built in the same official format): "
                + ", ".join(synth),
            )
        )
    if nokey:
        lines.append(
            L(
                "※ 官方无简中、保留原文：" + "、".join(nokey),
                "※ No official text, original kept: " + ", ".join(nokey),
            )
        )
    return (title, lines)


def fmt_platform_footer(platform: str, extra: str = "") -> str:
    txt = L(
        f"平台：{PLATFORM_DISPLAY.get(platform, platform.upper())}",
        f"Platform: {'International' if platform in PLATFORM_DISPLAY else platform.upper()}",
    )
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
        return L(f"信{int(rep or 0)}", f"Rep {int(rep or 0)}")
    except (TypeError, ValueError):
        return ""


def wm_best_price(orders: list[dict], kind: str) -> Optional[int]:
    """取与单件查询**展示口径完全一致**的最优价。

    排序规则同 ``fmt_wm_orders``：在线档位优先（ingame > online > offline），
    同档在售取最低 / 收购取最高——即单独搜部件时列表第一行的那个价格
    （2026-09-14 玩家反馈套装部件参考价与单件查询对不上：之前取全量绝对
    最值，2p/10p 的离线陈年老单把数字拉低）。
    """
    pool = [o for o in orders if o.get("order_type") == kind and o.get("visible", True)]
    if not pool:
        return None
    if kind == "buy":
        pool.sort(
            key=lambda o: (
                _ONLINE_RANK.get((o.get("user") or {}).get("status", ""), 3),
                -o["platinum"],
            )
        )
    else:
        pool.sort(
            key=lambda o: (
                _ONLINE_RANK.get((o.get("user") or {}).get("status", ""), 3),
                o["platinum"],
            )
        )
    return pool[0]["platinum"]


def _strip_common_prefix(names: list[str]) -> list[str]:
    """去掉一组名称的公共前缀词（「圣英 Prime 枪管 / 圣英 Prime 刀刃」→「枪管 / 刀刃」）。

    按空格分词、逐词取最长公共前缀；至少保留每个名字的最后一个词；少于 2 个名字不剥。
    """
    toks = [n.split(" ") for n in names]
    if len(toks) < 2:
        return list(names)
    k = 0
    while all(len(t) > k + 1 for t in toks) and len({t[k] for t in toks}) == 1:
        k += 1
    return [" ".join(t[k:]) for t in toks]


def limit_notes(lines: list[str], limit: int = 3) -> list[str]:
    """把卡内所有「※ 注脚」收口到卡尾，且最多 ``limit`` 行（多出的并进最后一行，用「；」连接）。

    ``※ ⚠`` 警示行不参与合并（它是需要用户行动的判定，单独成行、排在注脚之后）。
    """
    body = [x for x in lines if not x.startswith("※")]
    notes = [x for x in lines if x.startswith("※") and not x.startswith("※ ⚠")]
    warns = [x for x in lines if x.startswith("※ ⚠")]
    if len(notes) > limit:
        tail = "；".join(x[1:].strip() for x in notes[limit - 1 :])
        notes = [*notes[: limit - 1], "※ " + tail]
    return [*body, *notes, *warns]


def fmt_wm_set_parts(rows: list[dict]) -> list[str]:
    """套装查询附带的「部件参考价」表（2026-09-14 维护者要求：查套装默认输出
    套装单的同时把分件价一起给；单独查「wm 席瓦蓝图」再走单件查询）。

    口径：每件部件取 ``wm_best_price``（在线档位优先、同档价格最优），
    **与单独搜该部件时列表第一行的价格完全一致**；不标注在线状态
    （同日玩家反馈「不用特地标记在不在线」）。

    排版（2026-10-11 卡片改版）：「部件｜在售｜收购」三列表，部件名去掉套装公共前缀
    （「圣英 Prime 枪管」→「枪管」）；没有的价格写「—」，两边都没有挂单的整行写「暂无挂单」。
    """
    if not rows:
        return []
    names = _strip_common_prefix([r.get("name") or "?" for r in rows])
    lines = [L("◆ 部件参考价", "◆ Part Prices"), L("　部件　在售　收购", "　Part　Selling　Buying")]
    for r, name in zip(rows, names):
        if r.get("sell") is None and r.get("buy") is None:
            lines.append(L(f"· {name}　暂无挂单", f"· {name}　No orders"))
            continue
        sell = f"{r['sell']}p" if r.get("sell") is not None else "—"
        buy = f"{r['buy']}p" if r.get("buy") is not None else "—"
        lines.append(f"· {name}　{sell}　{buy}")
    lines.append(
        L(
            "※ 单查某个部件：wm 部件名（如 wm 席瓦蓝图）",
            "※ Single part: wm <part name> (e.g. wm Ash Prime Chassis Blueprint)",
        )
    )
    return lines


# 遗物精炼档（WM 订单的 subtype）与墨染标签——2026-09-25
_RELIC_SUBTYPE_CN = {"intact": "完整", "exceptional": "优良", "flawless": "无瑕", "radiant": "光辉"}
_RELIC_SUBTYPE_EN = {
    "intact": "Intact",
    "exceptional": "Exceptional",
    "flawless": "Flawless",
    "radiant": "Radiant",
}
_MORAN_SUBTYPE = "atragraph"  # 墨染 Mod（Foil/Atragraph）


def _status_names() -> dict:
    """WM 卖家 / 挂单状态 → 展示词（ingame 游戏内 / online 网页 / offline）。"""
    if i18n.is_en():
        return {"ingame": "In Game", "online": "Web Online", "offline": "Offline"}
    return {"ingame": "在线", "online": "网页在线", "offline": "离线"}


def fmt_wm_orders(
    item_name: str,
    orders: list[dict],
    *,
    buy: bool = False,
    page: int = 1,
    page_size: int = 10,
    quantity: Optional[int] = None,
    rank: Optional[int] = None,
    refinement: Optional[str] = None,
    moran: bool = False,
    notes: Optional[list] = None,
) -> tuple[str, list[str], Optional[dict]]:
    """整理 WM 订单：在线优先、价格其次，并标注卖家信誉与在线状态。

    排序口径（玩家反馈「做了价格排序但没做在线排序」）：
      先按「在线状态」分档（游戏内在线 > 网页在线 > 离线），档内再按价格升序
      （收购单为降序）。这样最上面永远是能立刻交易的人。

    过滤口径：
      · ``rank``：MOD/赋能的品级（0~maxRank；赋能满级是 5 不是 10）
      · ``refinement``：遗物精炼档（WM subtype：intact/exceptional/flawless/radiant）
      · ``moran``：只列墨染 Mod（subtype=atragraph）
      · ``notes``：调用方给卡面的补充说明（如「该物品没有精炼等级，已忽略」）

    Returns:
        ``(标题, 行, 最低价订单, 订单池)``——第三项用于 ``-r`` 密语生成；
        第四项是排序/filter 后的完整订单池（在线优先同展示顺序），2026-09-14
        维护者要求 ``-r`` 给前 5 个卖家各生成一条密语，不只第一个。
    """
    kind = "buy" if buy else "sell"
    pool = [o for o in orders if o.get("order_type") == kind and o.get("visible", True)]
    if rank is not None and rank >= 0:
        pool = [o for o in pool if o.get("mod_rank") == rank]
    if quantity:
        pool = [o for o in pool if int(o.get("quantity") or 0) >= quantity]
    if refinement:
        pool = [o for o in pool if str(o.get("subtype") or "") == refinement]
    if moran:
        pool = [o for o in pool if str(o.get("subtype") or "") == _MORAN_SUBTYPE]
    pool.sort(
        key=lambda o: (
            _ONLINE_RANK.get(o.get("user", {}).get("status", ""), 3),
            -o["platinum"] if buy else o["platinum"],
        )
    )
    status_cn = _status_names()
    total = len(pool)
    pages = max(1, (total + page_size - 1) // page_size)
    page = max(1, min(page, pages))
    chunk = pool[(page - 1) * page_size : page * page_size]
    title = L(
        f"{item_name} {'收购' if buy else '在售'}单（第{page}/{pages}页，共{total}条）",
        f"{item_name} {'Buy' if buy else 'Sell'} Orders (Page {page}/{pages}, {total} total)",
    )
    sort_note = L(
        "※ 排序：在线优先，同档按价格" + ("降序" if buy else "升序"),
        "※ Sorted: online first, then by price " + ("descending" if buy else "ascending"),
    )
    side = L("买家", "Buyer") if buy else L("卖家", "Seller")
    if not pool:
        # 空结果：原因 + 怎么改（卡片化的「查不到」，见 AGENTS 行文法）
        lines = [
            L(
                f"◆ 原因：没有符合条件的{'收购' if buy else '在售'}单",
                f"◆ Reason: No matching {'buy' if buy else 'sell'} orders",
            )
        ]
        lines += [f"※ {n}" for n in (notes or [])]
        lines.append(
            L(
                "※ ⚠ 怎么改：去掉品级 / 数量 / 精炼等筛选词重发；或换一个名称写法",
                "※ ⚠ Fix: drop the rank / quantity / refinement filters and resend, "
                "or try another spelling of the name",
            )
        )
        return (title, lines, None, pool)

    # —— 卡首答案：最低（收购为最高）价 ｜ 在线中位 ｜ 在线单数 ——
    online = [o for o in pool if (o.get("user") or {}).get("status") in ("ingame", "online")]
    val = f"{pool[0]['platinum']}p" + ("" if online else L("（离线）", " (Offline)"))
    if online:
        ps = sorted(o["platinum"] for o in online)
        mid = ps[len(ps) // 2] if len(ps) % 2 else (ps[len(ps) // 2 - 1] + ps[len(ps) // 2]) / 2
        val += L(f"｜在线中位 {mid:g}p", f"｜Online median {mid:g}p")
    val += L(f"｜在线 {len(online)} 单（共 {total} 单）", f"｜{len(online)} online ({total} total)")
    lines = [
        L(
            f"◆ {'最高价' if buy else '最低价'}：{val}",
            f"◆ {'Highest price' if buy else 'Lowest price'}: {val}",
        )
    ]

    # —— 明细表：整页全相同的列不写（相同的值并进区块标题）——
    def _tags(o: dict) -> tuple:
        sub = str(o.get("subtype") or "")
        tag = (_RELIC_SUBTYPE_EN if i18n.is_en() else _RELIC_SUBTYPE_CN).get(sub) or (
            L("墨染", "Atragraph") if sub == _MORAN_SUBTYPE else ""
        )
        rk = (
            L(f"{o['mod_rank']}级", f"Rank {o['mod_rank']}")
            if o.get("mod_rank") is not None
            else ""
        )
        return (
            f"×{o['quantity']}" if int(o.get("quantity") or 1) > 1 else "",
            rk,
            tag,
            status_cn.get((o.get("user") or {}).get("status", ""), L("离线", "Offline")),
        )

    rows_t = [_tags(o) for o in chunk]
    shared: list[str] = []  # 整页相同的等级 / 档位：并进区块标题
    cols: list[int] = []  # 要写的可变列：0 数量 / 1 等级 / 2 档位 / 3 状态
    for ci in range(4):
        vals = {r[ci] for r in rows_t}
        if ci == 0:
            if vals != {""}:  # 数量：任一行 >1 才出列
                cols.append(0)
        elif len(vals) > 1:
            cols.append(ci)
        elif ci != 3 and next(iter(vals)):
            shared.append(next(iter(vals)))
    state = "" if 3 in cols else rows_t[0][3]  # 整页同一状态：写进标题（「在线卖单」）
    if i18n.is_en():
        sec = f"◆ {state + ' ' if state else ''}{'Buy Orders' if buy else 'Sell Orders'}"
        sec += "".join(f" · {x}" for x in shared)
        _dir = "descending" if buy else "ascending"
        sec += f" (price {_dir})" if state else f" (online first, price {_dir})"
    else:
        sec = f"◆ {state}{'收购单' if buy else '卖单'}" + "".join(f" · {x}" for x in shared)
        sec += (
            f"（{'价格降序' if buy else '价格升序'}）"
            if state
            else (f"（在线优先，价格{'降序' if buy else '升序'}）")
        )
    lines.append(sec)
    names = {
        0: L("数量", "Qty"),
        1: L("等级", "Rank"),
        2: L("档位", "Refine"),
        3: L("状态", "Status"),
    }
    lines.append(
        L("　价格", "　Price")
        + "".join(f"　{names[c]}" for c in cols)
        + L(f"　{side}　信誉", f"　{side}　Rep")
    )
    for o, r in zip(chunk, rows_t):
        user = o.get("user", {}) or {}
        cells = [f"{o['platinum']}p", *[r[c] for c in cols], str(user.get("ingame_name", "?"))]
        cells.append(_rep_txt(user.get("reputation")))
        lines.append("· " + "　".join(cells).rstrip("　"))
    lines.append(sort_note)
    for note in notes or []:
        lines.append(f"※ {note}")
    best = pool[0] if pool else None
    return (title, lines, best, pool)


WM_WHISPER_BUY = '/w {name} Hi! I want to buy: "{item}" for {plat} platinum. (warframe.market)'
WM_WHISPER_SELL = '/w {name} Hi! I want to sell: "{item}" for {plat} platinum. (warframe.market)'


def build_whisper(order: dict, item_name: str, *, sell: bool = False) -> str:
    """生成快捷交易密语。

    尾部括号标注**数据来源站**：这些价格/在线状态来自 warframe.market，
    不是 warframe.com 官网（旧模板错写成 warframe.com，会被卖家当成骗子）。
    """
    tmpl = WM_WHISPER_SELL if sell else WM_WHISPER_BUY
    return tmpl.format(
        name=order.get("user", {}).get("ingame_name", "?"),
        item=item_name,
        plat=order.get("platinum", "?"),
    )


def _is_melee_weapon(riven_type: str = "", group: str = "") -> bool:
    """WM 的 rivenType/group 是否为近战（含 Zaw）。"""
    from . import riven_analysis as RA

    return RA.weapon_class(riven_type, group) == "melee"


# 紫卡词条标准 id → 游戏内英文名（英文模式用；与 parser.RIVEN_STAT_ZH 同一套 id）。
# 写法取 DE 英文客户端卡面与 WM attributes 的英文名；4 个已在
# parser.RIVEN_STAT_EN_DISPLAY 登记的以那边为准。个别新词条的英文卡面写法待实卡复核。
_RIVEN_STAT_EN = {
    "gas_damage": "Gas",
    "corrosive_damage": "Corrosive",
    "viral_damage": "Viral",
    "radiation_damage": "Radiation",
    "blast_damage": "Blast",
    "magnetic_damage": "Magnetic",
    "damage_vs_orokin": "Damage to Orokin",
    "damage_vs_scaldra": "Damage to Scaldra",
    "damage_vs_techrot": "Damage to Techrot",
    "weakpoint_damage": "Weak Point Damage",
    "weakpoint_crit_chance": "Weak Point Critical Chance",
    "status_damage": "Status Damage",
    "ammo_efficiency": "Ammo Efficiency",
    "reload_holstered": "Magazine Reloaded/s when Holstered",
    "melee_heavy_attack_damage": "Melee Damage on Heavy Attack",
    "melee_heavy_attack_charge": "Heavy Attack Wind Up Speed",
    "melee_parry_angle": "Parry Angle",
    "melee_slam_damage": "Slam Attack Damage",
    "melee_damage": "Melee Damage",
    "damage": "Damage",
    "crit_chance": "Critical Chance",
    "crit_damage": "Critical Damage",
    "multishot": "Multishot",
    "attack_speed": "Attack Speed",
    "fire_rate": "Fire Rate",
    "status_chance": "Status Chance",
    "status_duration": "Status Duration",
    "range": "Range",
    "initial_combo": "Initial Combo",
    "combo_duration": "Combo Duration",
    "heavy_attack_efficiency": "Heavy Attack Efficiency",
    "finisher_damage": "Finisher Damage",
    "slide_crit": "Critical Chance for Slide Attack",
    "slash_damage": "Slash",
    "impact_damage": "Impact",
    "puncture_damage": "Puncture",
    "heat_damage": "Heat",
    "cold_damage": "Cold",
    "toxin_damage": "Toxin",
    "electric_damage": "Electricity",
    "damage_vs_grineer": "Damage to Grineer",
    "damage_vs_corpus": "Damage to Corpus",
    "damage_vs_infested": "Damage to Infested",
    "magazine_capacity": "Magazine Capacity",
    "ammo_max": "Ammo Maximum",
    "projectile_speed": "Projectile Speed",
    "punch_through": "Punch Through",
    "reload_speed": "Reload Speed",
    "recoil": "Weapon Recoil",
    "zoom": "Zoom",
    "extra_combo_count": "Additional Combo Count Chance",
    "combo_gain_chance": "Chance to Gain Combo Count",
}


def _riven_stat_sid(url_name: str, riven_type: str = "", group: str = "") -> str:
    """拍卖词条 url_name → 标准词条 id（合并名按武器类别消歧射速 / 攻速）。"""
    from .parser import RIVEN_URL_COMPAT

    to_canon = {v: k for k, v in RIVEN_URL_COMPAT.items()}
    canon = to_canon.get(url_name, url_name)
    if canon == "fire_rate" and _is_melee_weapon(riven_type, group):
        canon = "attack_speed"
    return canon


def _riven_stat_en(sid: str, fallback: str = "", riven_type: str = "", group: str = "") -> str:
    """标准词条 id → 游戏内英文名；查不到用 slug 的 Title Case。"""
    from .parser import RIVEN_STAT_EN_DISPLAY

    if sid == "melee_damage" and fallback == "base_damage_/_melee_damage":
        # WM 合并 slug：枪械是 Damage，近战是 Melee Damage
        return "Melee Damage" if _is_melee_weapon(riven_type, group) else "Damage"
    if sid in RIVEN_STAT_EN_DISPLAY:
        return RIVEN_STAT_EN_DISPLAY[sid][0]
    if sid in _RIVEN_STAT_EN:
        return _RIVEN_STAT_EN[sid]
    return (fallback or sid).replace("_", " ").title()


def _riven_stat_cn(url_name: str, riven_type: str = "", group: str = "") -> str:
    """拍卖词条 url_name -> 中文名（合并名/标准名都处理）。

    ⚠️ WM 把**射速**和**攻速**合成同一个 slug `fire_rate_/_attack_speed`
    （官网中文也写成「射速/攻击速度」），光看 slug 分不出近战还是枪械，
    必须用武器类别消歧：近战 → 攻速，其余 → 射速。
    2026-09-14 玩家报障：「翁（Okina，匕首）紫卡里怎么会有射速」。
    """
    from .parser import RIVEN_STAT_ZH

    canon = _riven_stat_sid(url_name, riven_type, group)
    if i18n.is_en():
        return _riven_stat_en(canon, url_name, riven_type, group)
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


def wr_sorted_auctions(
    auctions: list[dict], *, presorted: bool = False, exact_ids: Optional[set] = None
) -> list[dict]:
    """拍卖卡与卖家按钮共用排序：一口价、在线档、恰好命中、价格。"""
    if presorted:
        return list(auctions)
    return sorted(
        auctions,
        key=lambda a: (
            0 if a.get("buyout_price") else 1,
            _ONLINE_RANK.get((a.get("owner") or {}).get("status") or "offline", 3),
            0 if (exact_ids and a.get("id") in exact_ids) else 1,
            a.get("buyout_price") or a.get("starting_price") or 0,
        ),
    )


def fmt_wr_auctions(
    weapon: str,
    auctions: list[dict],
    page: int = 1,
    page_size: int = 8,
    riven_type: str = "",
    group: str = "",
    *,
    presorted: bool = False,
    exact_ids: Optional[set] = None,
) -> tuple[str, list[str], Optional[dict]]:
    """紫卡拍卖列表。

    展示与排序都按「先看能不能立刻交易、再看价格」：
      · 每行给出 卖家在线状态 / 信誉 / 紫卡等级 / 洗数
      · 词条用 ▲（正面，青绿）/ ▼（负面，红）区分，正面同一色、负面另一色
      · 排序：在线优先（游戏内 > 网页 > 离线），同档按买断价升序

    ``riven_type`` / ``group`` 用于词条消歧（近战的合并 slug 显示「攻速」）。
    ``presorted=True``：调用方已按**词条命中率**排好序（wr 的「无完全匹配，
    给最接近选项」分支），此时不再按「在线+价格」重排 —— 否则前排会变成
    便宜但词条不匹配的挂单（2026-09-24 玩家报障「前排出现不匹配的项目」）。
    ``exact_ids``（2026-10-01 B 口径）：**完全命中词条**的挂单 id 集合——
    只影响默认分支的排序键（在线档 → 档内恰好优先 → 价格升序），**不是硬过滤**
    （超集仍在结果里可翻页）；`None`（未指定词条）退化为现行为。
    """

    def status_of(a: dict) -> str:
        return (a.get("owner") or {}).get("status") or "offline"

    def age_of(a: dict) -> str:
        """挂单上架时长（WM `created`）→ 分/时/天；解析失败返回空串。"""
        try:
            t = datetime.fromisoformat((a.get("created") or "").replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return ""
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        secs = max(0.0, (datetime.now(timezone.utc) - t).total_seconds())
        # ★ 2026-10-08（用户口径）：用 s/m/h 分档、放行尾、可被渲染层右对齐；
        #   超过 100 小时不再往上加单位（显示 >100h，避免「3天12时」这种长串撑行）。
        if secs < 60:
            return f"{int(secs)}s"
        if secs < 3600:
            return f"{int(secs // 60)}m"
        hours = secs / 3600
        return f">{int(hours // 100) * 100}h" if hours >= 100 else f"{int(hours)}h"

    pool = wr_sorted_auctions(auctions, presorted=presorted, exact_ids=exact_ids)
    rank_max = 8  # 紫卡满级 8 级
    total = len(pool)
    pages = max(1, (total + page_size - 1) // page_size)
    page = max(1, min(page, pages))
    chunk = pool[(page - 1) * page_size : page * page_size]
    title = L(
        f"{weapon} 紫卡拍卖（第{page}/{pages}页，共{total}条）",
        f"{weapon} Riven Auctions (Page {page}/{pages}, {total} total)",
    )
    # ★ 2026-10-08：空池必须明说「没有挂单」；2026-10-11 卡片化为「原因 + 怎么改」。
    if not pool:
        return (
            title,
            [
                L("◆ 原因：没有符合条件的紫卡挂单", "◆ Reason: No matching riven listings"),
                L(
                    "※ ⚠ 怎么改：放宽词条 / 洗数 / 价格上限等筛选词重发；或去掉筛选只发「wr 武器名」",
                    "※ ⚠ Fix: loosen the stat / reroll / price-cap filters and resend, "
                    'or drop the filters and send just "wr <weapon>"',
                ),
            ],
            None,
        )
    status_name = _status_names()
    online_n = sum(1 for a in pool if status_of(a) in ("ingame", "online"))
    _bos = [a for a in pool if a.get("buyout_price") and status_of(a) in ("ingame", "online")]
    _lab = L("在线最低一口价", "Lowest online buyout")
    if not _bos:
        _bos = [a for a in pool if a.get("buyout_price")]
        _lab = L("最低一口价（均离线）", "Lowest buyout (all offline)")
    _kv = []
    if _bos:
        _kv.append(f"{int(min(a['buyout_price'] for a in _bos))}p")
    _kv.append(L(f"在线 {online_n} 条（共 {total} 条）", f"{online_n} online ({total} total)"))
    lines = [
        f"◆ {_lab if _bos else L('在线挂单', 'Online listings')}" + L("：", ": ") + "｜".join(_kv)
    ]

    # —— 明细行：整页全相同的列不写（相同的值并进区块标题）——
    recs = []
    for a in chunk:
        item = a.get("item", {}) or {}
        owner = a.get("owner", {}) or {}
        attrs = []
        for at in item.get("attributes") or []:
            mark = "▲" if at.get("positive") else "▼"
            if i18n.is_en():
                # 英文卡面写法：▲Critical Chance +120.5%（带符号与单位）
                from . import riven_analysis as _RA

                _sid = _riven_stat_sid(at.get("url_name", ""), riven_type, group)
                _val = at.get("value", 0) or 0
                # 符号按正负面标记定，不看 WM 数值本身的正负（负面词条的数值可能存成正数）；
                # 反转词条（如后坐力）正面是减少，所以写 "-"
                _plus = bool(at.get("positive")) != bool(_RA.is_inverted(_sid))
                attrs.append(
                    f"{mark}{_riven_stat_cn(at.get('url_name', ''), riven_type, group)} "
                    f"{'+' if _plus else '-'}{_RA.fmt_value(_sid, abs(_val))}"
                )
                continue
            attrs.append(
                f"{mark}"
                f"{_riven_stat_cn(at.get('url_name', ''), riven_type, group)}"
                f"{abs(at.get('value', 0)):g}"
            )
        rank = item.get("mod_rank")
        # ★ 价格列统一写「Np」；竞价型（无买断价）写「∞（起拍价 Np）」。
        # ⚠ 不能用「♾」(U+267E)：渲染层 `_EMOJI_RE` 会把它当 emoji 剥掉，且随包子集字体
        #   没有这个字形（实测画出来是 .notdef 方框）—— 2026-10-08「无限图标没显示」的真因；
        #   「∞」(U+221E) 两边都没问题（2026-10-11 出图实测）。
        _bo, _st = a.get("buyout_price"), a.get("starting_price") or 0
        recs.append(
            {
                "price": f"{int(_bo)}p"
                if _bo
                else (L(f"∞（起拍价 {int(_st)}p）", f"∞ (starting {int(_st)}p)") if _st else "∞"),
                "state": status_name.get(owner.get("status"), L("离线", "Offline")),
                "who": str(owner.get("ingame_name", "?")),
                "roll": L(
                    f"洗{_riven_rolls(a)}次",
                    f"{_riven_rolls(a)} Reroll" + ("" if _riven_rolls(a) == 1 else "s"),
                ),
                "lv": L(f"{rank}/{rank_max}级", f"Rank {rank}/{rank_max}")
                if rank is not None
                else "",
                "rep": _rep_txt(owner.get("reputation")),
                "age": age_of(a),
                "attrs": attrs,
            }
        )
    shared: list[str] = []
    cols: list[str] = []
    for key in ("state", "roll", "lv"):
        vals = {r[key] for r in recs}
        if len(vals) > 1:
            cols.append(key)
        elif key in ("roll", "lv") and next(iter(vals)):
            shared.append(next(iter(vals)))
    state = "" if "state" in cols else recs[0]["state"]
    if i18n.is_en():
        sec = f"◆ {state + ' ' if state else ''}Listings" + "".join(f" · {x}" for x in shared)
        _exact = " → exact stat matches first" if exact_ids else ""
        sec += (
            f" (buyout first{_exact}, price ascending)"
            if state
            else f" (buyout first, online first{_exact}, price ascending)"
        )
    else:
        sec = f"◆ {state}挂单" + "".join(f" · {x}" for x in shared)
        _exact = " → 完全命中词条优先" if exact_ids else ""
        sec += (
            f"（一口价在前{_exact}，价格升序）"
            if state
            else f"（一口价在前，在线优先{_exact}，价格升序）"
        )
    lines.append(sec)
    hdr = {
        "state": L("状态", "Status"),
        "roll": L("洗数", "Rerolls"),
        "lv": L("等级", "Rank"),
    }
    lines.append(
        L("　一口价", "　Buyout")
        + "".join(f"　{hdr[c]}" for c in cols)
        + L("　卖家　信誉", "　Seller　Rep")
    )
    for r in recs:
        cells = [r["price"], *[r[c] for c in cols], r["who"], r["rep"]]
        row = "· " + "　".join(cells).rstrip("　")
        if r["age"]:  # 行尾「上架 Nh」由渲染层抽成右对齐时间列
            row += L(f"　上架{r['age']}", f"　Listed {r['age']}")
        lines.append(row)
        if r["attrs"]:
            lines.append("　" + "　".join(r["attrs"][:6]))
    lines.append(
        L(
            "※ ▲正面词条（同色）· ▼负面词条（红色）",
            "※ ▲Positive stats (same color) · ▼Negative stats (red)",
        )
    )
    best = pool[0] if pool else None
    return (title, lines, best)


_DUCAT_TIER = {
    "金": {"name": "金垃圾（稀有部件 100 杜卡德）", "values": (100,)},
    "银": {"name": "银垃圾（非稀有部件 45-65 杜卡德）", "values": (45, 65)},
    "铜": {"name": "铜垃圾（常见部件 15-25 杜卡德）", "values": (15, 25)},
}


_DUCAT_TIER_NAME_EN = {
    "金": "Gold Parts (Rare parts, 100 Ducats)",
    "银": "Silver Parts (non-Rare parts, 45-65 Ducats)",
    "铜": "Bronze Parts (Common parts, 15-25 Ducats)",
}


def fmt_ducat_junk(
    tier: str, rows: list[dict], page: int = 1, pages: int = 1, total: int = 0
) -> tuple[str, list[str]]:
    """杜卡德垃圾榜：列出「杜卡德/白金」最高的部件。

    ★ 2026-10-11 第二步改版：开头一行给榜单规模与翻页，榜单做成
    「序 / 部件 / 杜卡德 / 白金 / 杜/p」表（杜/p 的加权值并入同一格），注脚压成 1 条。

    Args:
        tier: ``金`` / ``银`` / ``铜``。
        rows: ``[{"name", "ducats", "dpp", "dpp_wa", "plat", "volume"}]``。
        page: 当前页码。
        pages: 总页数。
        total: 榜单总件数（0 = 不显示）。

    Returns:
        ``(标题, 行列表)``。
    """
    S = "　"
    if i18n.is_en():
        name = _DUCAT_TIER_NAME_EN.get(tier, f"{tier} Parts (Ducats)")
    else:
        name = _DUCAT_TIER.get(tier, {}).get("name", f"{tier}垃圾")
    if not rows:
        return (
            name,
            [
                L(
                    "◆ 榜单：当前取不到杜卡德榜单（WM tools/ducats 不可用）",
                    "◆ Rankings: Ducat rankings unavailable right now (WM tools/ducats unreachable)",
                )
            ],
        )
    head = L(f"◆ 榜单：共 {total or len(rows)} 件", f"◆ Rankings: {total or len(rows)} parts")
    if pages > 1:
        head += L(
            f"（第 {page}/{pages} 页，加 -2 / -3 翻页）",
            f" (Page {page}/{pages}, add -2 / -3 to flip pages)",
        )
    lines = [
        head,
        L("◆ 杜/p 排行", "◆ Ducats per Platinum"),
        L(f"{S}序{S}部件{S}杜卡德{S}白金{S}杜/p", f"{S}#{S}Part{S}Ducats{S}Plat{S}D/p"),
    ]
    for i, r in enumerate(rows, (page - 1) * len(rows) + 1 if pages else 1):
        lines.append(
            L(
                f"· {i}{S}{r['name']}{S}{r['ducats']}杜{S}{r['plat']:.0f}p{S}{r['dpp']:.1f}",
                f"· {i}{S}{r['name']}{S}{r['ducats']} Ducats{S}{r['plat']:.0f}p{S}{r['dpp']:.1f}",
            )
            + (
                L(f"（加权 {r['dpp_wa']:.1f}）", f" (weighted {r['dpp_wa']:.1f})")
                if r.get("dpp_wa")
                else ""
            )
        )
    lines.append(
        L(
            "※ 杜/p = 每 1 白金能换到多少杜卡德，越高越值得买来换；"
            "数据源 warframe.market/tools/ducats（与官网杜卡德计算器同源）",
            "※ D/p = Ducats you get per 1 Platinum; higher means a better buy-to-convert deal. "
            "Source: warframe.market/tools/ducats (same as the official Ducat calculator)",
        )
    )
    return (name, lines)


# ---------------------------------------------------------------------------
# 九重天 / 活动 / 武形秘仪 / 阿耶兑换 / 氏族奖励 / 商城折扣
# ---------------------------------------------------------------------------

_RAILJACK_TYPE_CN = {
    "skirmish": "前哨战",
    "volatile": "爆发",
    "spy": "间谍",
    "survival": "生存",
    "extermination": "歼灭",
    "defense": "防御",
    "sabotage": "破坏",
    "assassinate": "刺杀",
    "orphix": "奥影母艇",
    "hijack": "劫持",
}


_RAILJACK_TYPE_EN = {
    "skirmish": "Skirmish",
    "volatile": "Volatile",
    "spy": "Spy",
    "survival": "Survival",
    "extermination": "Exterminate",  # 与 DE 官方任务类型表一致
    "defense": "Defense",
    "sabotage": "Sabotage",
    "assassinate": "Assassination",
    "orphix": "Orphix",
    "hijack": "Hijack",
}


_SPACE_SUFFIX = re.compile(r"比邻星域$")
_SPACE_SUFFIX_EN = re.compile(r"\s*\([A-Za-z ]*Proxima\)$")


def _space_node(name: str) -> str:
    """虚空风暴节点名去掉「比邻星域」括注（英文为「(Earth Proxima)」）。"""
    return (_SPACE_SUFFIX_EN if i18n.is_en() else _SPACE_SUFFIX).sub("", name)


def fmt_void_storms(storms: Iterable[dict]) -> tuple[str, list[str]]:
    """九重天（航道星舰）虚空风暴。

    按纪元分组、每处一行并带「任务类型 + 剩余时间」，比原来一整列同构长短句
    更好扫读（旧版每行只有 ``节点｜类型｜T1｜剩0s``，12 行完全一样看不出重点）。
    """
    storms = list(storms or [])
    title = L("九重天虚空风暴", "Void Storms")
    if not storms:
        return (title, [L("当前没有可用的虚空风暴", "No Void Storms available right now")])
    groups: dict[str, list] = {}
    for s in storms:
        groups.setdefault(s.get("tier") or "?", []).append(s)
    # ★ 2026-09-27 列对齐：这行原来把「※ 说明」用全角空格接在同一行 —— 渲染层的
    #   列宽是**按全角空格切列**算的（`_split_cells`），一条 normal 行挂一段长说明
    #   会把第二列整体撑宽。拆成两行规避（※ 行不参与切列）。
    S = "　"
    rows: list[tuple[str, str]] = []  # (expiry 排序键, 行)
    for tier in sorted(
        groups,
        key=lambda t: _TIER_ORDER.get(
            {"T1": "Lith", "T2": "Meso", "T3": "Neo", "T4": "Axi"}.get(t, t), 99
        ),
    ):
        cn = tier_cn({"T1": "Lith", "T2": "Meso", "T3": "Neo", "T4": "Axi"}.get(tier, tier))
        for s in groups[tier]:
            node = _space_node(s.get("nodeCn") or "?")
            if i18n.is_en():
                mt = _RAILJACK_TYPE_EN.get(
                    (s.get("missionType") or "").lower(),
                    (s.get("missionType") or "?").title(),
                )
            else:
                mt = _RAILJACK_TYPE_CN.get(
                    (s.get("missionType") or "").lower(), s.get("missionType") or "?"
                )
            # ★ 2026-10-11：渲染时按 expiry **现算**（旧写法用解析时算好的 timeLeft 字符串，
            #   缓存 N 分钟后卡面仍是解析那一刻的剩余；格式也与全卡「X小时X分」不统一）
            left = (
                countdown(s.get("expiry") or "") if s.get("expiry") else (s.get("timeLeft") or "?")
            )
            # 第二步改版：纪元走芯片列，不再按 T1..T4 分四个小区块（每块 2～4 行、各自一套列宽）
            rows.append((s.get("expiry") or "", f"· [{cn}]{S}{node}{S}{mt}{S}{_left(left, '剩')}"))
    soonest = min((r for r in storms if r.get("expiry")), key=lambda r: r["expiry"], default=None)
    lines = [L(f"◆ 进行中：{len(storms)} 处", f"◆ Active: {len(storms)}")]
    if soonest is not None:
        lines.append(
            L("◆ 最近到期：", "◆ Ends soonest: ")
            + f"{_space_node(soonest.get('nodeCn') or '?')}{S}"
            + _left(countdown(soonest["expiry"]), "剩")
        )
    lines += [
        L("◆ 风暴列表", "◆ Storm List"),
        L(f"{S}纪元{S}节点{S}类型", f"{S}Era{S}Node{S}Type"),
    ]
    lines += [ln for _k, ln in rows]
    lines.append(
        L(
            "※ 航道星舰虚空风暴裂缝，可刷对应纪元遗物",
            "※ Railjack Void Storm fissures drop relics of the matching era",
        )
    )
    return (title, lines)


def fmt_events(events: Iterable[dict]) -> tuple[str, list[str]]:
    """限时活动（社区目标）。

    DE 的 Goal 带 ``GracePeriod``：活动本体到期后仍在宽限期内下发。这类条目
    会照常展示但标记「已结束」，避免用户以为漏了活动（如刚收官的三伏天）。
    """
    events = list(events or [])
    if not events:
        return (
            L("活动", "Active Events"),
            [L("当前没有进行中的限时活动", "No limited-time events running right now")],
        )
    S = "　"
    lines = [L(f"◆ 进行中：{len(events)} 项", f"◆ Active: {len(events)}")]
    for e in events:
        lines.append(f"◆ {e.get('name')}")
        if e.get("node"):
            lines.append(L(f"· 地点{S}{e['node']}", f"· Location{S}{e['node']}"))
        if e.get("ended"):
            lines.append(
                L(f"· 时限{S}已结束（结算宽限期）", f"· Time Limit{S}Ended (grace period)")
            )
        else:
            # ★ 2026-10-11：按 expiry 现算（旧：解析时的 timeLeft「254h 56m」）
            left = countdown(e.get("expiry") or "") if e.get("expiry") else e.get("timeLeft")
            lines.append(L(f"· 时限{S}", f"· Time Limit{S}") + _left(str(left), "剩"))
        if e.get("count") is not None and e.get("goal"):
            try:
                pct = int(e["count"]) / max(1, int(e["goal"])) * 100
                lines.append(
                    L(
                        f"· 进度{S}{e['count']}/{e['goal']}（{pct:.0f}%）",
                        f"· Progress{S}{e['count']}/{e['goal']} ({pct:.0f}%)",
                    )
                )
            except (TypeError, ValueError, ZeroDivisionError):
                lines.append(
                    L(f"· 进度{S}", f"· Progress{S}") + f"{e.get('count')}/{e.get('goal')}"
                )
        if e.get("desc") and (i18n.is_en() or _HAS_CJK.search(e["desc"])):
            # 官方简中缺失时 desc 会是英文长句（如 "Seal fractures across…"），
            # 中文卡片里塞英文只会更乱 —— 只在拿到中文说明时才展示。英文模式照常展示。
            lines.append(L(f"· 说明{S}", f"· Info{S}") + f"{e['desc']}")
        if e.get("rewards"):
            lines.append(L(f"· 奖励{S}", f"· Rewards{S}") + L("、", ", ").join(e["rewards"][:8]))
    return (L("进行中的活动", "Active Events"), lines)


# 武形秘仪（Conclave）声望来源（wiki.warframe.com/w/Conclave）：
#   每周三项（赢得 6 场 / 完成 20 场 / 完成 10 项每日挑战）全部完成 -> 50,000 声望
#   + 10 个稀有资源 + 一个架式 MOD + 100,000 现金（Teshin 站内信）
#   每日挑战最多 8 项（每个模式 2 项），单项目 500 / 1,500 / 3,000 声望，
#   带「Focused」前缀的高级挑战为 6,000 声望
PVP_STANDING_NOTE = (
    "※ 声望：每周三项全完成得 50,000（另附站内信奖励）；"
    "每日挑战 500/1,500/3,000 声望，高级(Focused) 6,000"
)
_PVP_STANDING_NOTE_EN = (
    "※ Standing: completing all three weekly challenges gives 50,000 (plus inbox rewards); "
    "daily challenges give 500/1,500/3,000, Focused challenges 6,000"
)


def fmt_conclave(challenges: Iterable[dict]) -> tuple[str, list[str]]:
    """武形秘仪（Conclave）每日/每周挑战 + 声望来源说明。"""
    challenges = list(challenges or [])
    if not challenges:
        return (
            L("武形秘仪", "Conclave"),
            [L("◆ 状态：当前没有可用的武形秘仪挑战", "◆ Status: No Conclave challenges available")],
        )
    lines = []
    cur = None
    for c in challenges:
        if c.get("cat") != cur:
            cur = c.get("cat")
            lines.append(L(f"◆ {cur}挑战", f"◆ {cur} Challenges"))
            lines.append(L("　模式　挑战", "　Mode　Challenge"))
        val = f" ×{c['value']}" if c.get("value") else ""
        lines.append(f"· {str(c.get('mode')).replace('　', ' ')}　{c.get('text')}{val}")
    lines.append(L(PVP_STANDING_NOTE, _PVP_STANDING_NOTE_EN))
    return (L("武形秘仪挑战（Conclave）", "Conclave Challenges"), lines)


def _vault_left(data: dict) -> str:
    """Prime 重生本期剩余：按 expiry 现算（「X天X小时」），无 expiry 才用解析时的字符串。"""
    exp = data.get("expiry") or ""
    return countdown(exp) if exp else (data.get("timeLeft") or "?")


def _vault_unnamed_note(data: dict) -> list[str]:
    """词表未收录的条目（显示为路径美化名 +「（未收录）」）的计数注脚。"""
    n = len(data.get("unnamed") or [])
    if not n:
        return []
    return [
        L(
            f"※ {n} 件官方简中词表未收录，按资产路径显示（标「未收录」）",
            f'※ {n} item(s) missing from the official name list, shown by asset path (marked "unlisted")',
        )
    ]


def _vault_currency(it: dict) -> str:
    """阿耶商店的兑换货币名：御品阿耶 / 阿耶精华（英文 Regal Aya / Aya）。"""
    if it.get("prime_currency", True):
        return L("御品阿耶", "Regal Aya")
    return L("阿耶精华", "Aya")


def _vault_head(data: dict, with_empty_hint: bool = False) -> list[str]:
    """出库 / 阿耶两张卡共用的卡首：本期剩余 + 下一期（均为「◆ 标签：值」答案行）。"""
    _left_v = _vault_left(data)
    lines = [L(f"◆ 本期剩余：剩余 {_left_v}", f"◆ Current rotation: {_left_en(_left_v)}")]
    if data.get("next"):
        _cd = countdown(data.get("next_expiry", ""))
        lines.append(
            L(
                f"◆ 下一期：{data['next']}｜{_cd} 后开启",
                f"◆ Next rotation: {data['next']}｜opens {_in_en(_cd)}",
            )
        )
    elif data.get("next_unannounced"):
        # DE 给了下一轮的时间点、但没公布内容（FeaturedItem 为空串）
        _cd = countdown(data.get("next_open", ""))
        lines.append(
            L(
                f"◆ 下一期：内容 DE 尚未公布｜下一轮 {_cd} 后开启",
                f"◆ Next rotation: contents not yet announced by DE｜next round opens {_in_en(_cd)}",
            )
        )
    elif with_empty_hint:
        # 排期表里没有更多数据时明说，别留空标题让用户以为插件坏了
        lines.append(
            L(
                "◆ 下一期：排期 DE 尚未下发（只公布到当前期）",
                "◆ Next rotation: schedule not yet released by DE (only the current rotation is published)",
            )
        )
    return lines


def fmt_prime_vault(
    data: Optional[dict], page: int = 1, page_size: int = 12
) -> tuple[str, list[str]]:
    """御品阿耶精华 / Prime 重生（Varzia），支持翻页。

    Args:
        data: ``prime_vault`` 的产物。
        page: 页码，从 1 开始。
        page_size: 每页可兑换条目数（本期 + 常驻合并成一列翻页）。

    Returns:
        ``(标题含页码, 行列表)``。
    """
    if not data or not (data.get("items") or data.get("evergreen")):
        return (
            L("御品阿耶精华", "Prime Resurgence"),
            [L("数据暂不可用", "Data temporarily unavailable")],
        )
    lines = _vault_head(data, with_empty_hint=True)

    S = "　"

    def _line(tag: str, it: dict) -> str:
        cur = _vault_currency(it)
        return f"· [{tag}]{S}{it['name']}{S}{it.get('prime', 1)} {cur}"

    rows = [_line(L("本期", "Current"), it) for it in (data.get("items") or [])] + [
        _line(L("常驻", "Permanent"), it) for it in (data.get("evergreen") or [])
    ]
    if rows:
        total = len(rows)
        pages = max(1, (total + page_size - 1) // page_size)
        page = max(1, min(page, pages))
        lines.append(
            L(
                f"◆ 可兑换：本期 {len(data.get('items') or [])} + "
                f"常驻 {len(data.get('evergreen') or [])}，共 {total} 项"
                + (f"（第 {page}/{pages} 页，加 -2 / -3 翻页）" if pages > 1 else ""),
                f"◆ Redeemable: {len(data.get('items') or [])} current + "
                f"{len(data.get('evergreen') or [])} permanent, {total} total"
                + (f" (Page {page}/{pages}, add -2 / -3 to flip pages)" if pages > 1 else ""),
            )
        )
        lines += [
            L("◆ 兑换列表", "◆ Redemption List"),
            L(f"{S}类别{S}名称{S}价格", f"{S}Type{S}Name{S}Price"),
        ]
        lines += rows[(page - 1) * page_size : page * page_size]
    lines += _vault_unnamed_note(data)
    return (L("御品阿耶精华 / Prime 重生", "Prime Resurgence (Varzia)"), lines)


# ★ 2026-10-16 部件按**杜卡德价值**金 / 银 / 铜三色（渲染层 render._SPAN_COLORS 的 g / s / b）：
#   100 = 金、45 / 65 = 银、15 / 25 = 铜；查不到价值（Forma 蓝图等、或价格源不可用）不染色。
#   ⚠ 不按遗物槽位稀有度 —— 100 金币的部件也会出现在罕见 / 常见槽位（维护者 10-16 指出）。
#   价格与「遗物」卡同源（WM 杜卡德榜单，api_client.ducats_price_map）。


def ducats_of(prices: Optional[dict], name: str) -> int:
    """部件名 → 杜卡德价值（查不到返回 0）。键与 fmt_relic_rewards 同口径（原名或去空格名）。"""
    p = (prices or {}).get(name) or (prices or {}).get(re.sub(r"\s+", "", name or "")) or {}
    try:
        return int(p.get("ducats") or 0)
    except (TypeError, ValueError):
        return 0


def ducat_span(ducats: int, text: str) -> str:
    """按杜卡德价值给文本包染段标记：100 金 / 45·65 银 / 15·25 铜；0 原样返回。"""
    c = "g" if ducats >= 100 else "s" if ducats >= 45 else "b" if ducats > 0 else ""
    return f"⟦{c}⟧{text}⟦/{c}⟧" if c and text else text


def _vault_relic_order(name: str) -> tuple:
    """出库遗物排序：古纪 → 前纪 → 中纪 → 后纪（→ 安魂 / 全能），同档按代号自然序。"""
    cn = relic_cn(name)
    tier = cn.split(" ")[0] if " " in cn else ""
    if tier in _RELIC_TIER_EN2CN:  # 英文模式 relic_cn 给的是 Lith A1
        tier = _RELIC_TIER_EN2CN[tier]
    rank = _RELIC_CN_TIERS.index(tier) if tier in _RELIC_CN_TIERS else 99
    return (rank, _relic_sort_key(cn.split(" ")[-1]) if " " in cn else (9, cn, 0))


# 「出库」清单的分组顺序与标题（kind 值见 de_worldstate._vault_kind）
_VAULT_GROUPS: tuple[tuple[str, str], ...] = (
    ("frame", "Prime 战甲"),
    ("weapon", "Prime 武器"),
    ("sentinel", "Prime 守护与守护武器"),
)
_VAULT_GROUPS_EN = {
    "frame": "Prime Warframes",
    "weapon": "Prime Weapons",
    "sentinel": "Prime Companions & Companion Weapons",
}


def fmt_prime_vault_list(
    data: Optional[dict], relic_parts: Optional[dict] = None, prices: Optional[dict] = None
) -> tuple[str, list[str]]:
    """出库清单：按「战甲 / 武器 / 守护」分组，**只列整套名**（不含部件）。

    与 :func:`fmt_prime_vault` 的分工（2026-09-17 维护者要求拆开）：
      · ``阿耶`` = 御品阿耶兑换表（带价格，含装饰与组合包，可翻页）
      · ``出库`` = 本清单：只看**这一期能从宝库刷到哪些整套 Prime 道具**

    此前两者输出完全一样，玩家反馈「出库指令和阿耶指令为什么是一样的」。
    """
    if not data:
        return (
            L("Prime 出库清单", "Prime Vault List"),
            [L("数据暂不可用", "Data temporarily unavailable")],
        )
    items = list(data.get("items") or [])
    lines = _vault_head(data)

    known = {k for k, _ in _VAULT_GROUPS}
    for kind, label in _VAULT_GROUPS:
        group = [it for it in items if it.get("kind") == kind]
        if not group:
            continue
        lines.append(L(f"◆ {label}（{len(group)}）", f"◆ {_VAULT_GROUPS_EN[kind]} ({len(group)})"))
        lines += [f"· {it['name']}" for it in group]

    # ★ 2026-10-16：本期出库遗物 + 每个遗物的全部部件（稀有 → 罕见 → 常见，对应金 / 银 / 铜）。
    #   旧实现把遗物也算进下面「装饰 / 组合包（不能用遗物刷取）」注脚，等于把遗物说成了装饰品。
    relics = sorted(
        (it for it in items if it.get("kind") == "relic"),
        key=lambda it: _vault_relic_order(it["name"]),
    )
    if relics:
        lines.append(
            L(
                f"◆ 出库遗物（{len(relics)}，瓦奇娅处用阿耶精华兑换）",
                f"◆ Vaulted Relics ({len(relics)}, buy with Aya from Varzia)",
            )
        )
        for it in relics:
            cur = _vault_currency(it)
            lines.append(f"◆ {relic_cn(it['name'])}　{it.get('prime', 1)} {cur}")
            rw = (relic_parts or {}).get(it["name"]) or {}
            rows = [
                (tag, tag_en, rw.get(tag) or [])
                for tag, tag_en in (("稀有", "Rare"), ("罕见", "Uncommon"), ("常见", "Common"))
            ]
            rows = [(tag, tag_en, parts) for tag, tag_en, parts in rows if parts]
            if rows:
                lines += [
                    f"· {L(tag, tag_en)}　"
                    + L("、", ", ").join(
                        ducat_span(ducats_of(prices, str(x)), str(x)) for x in parts
                    )
                    for tag, tag_en, parts in rows
                ]
            else:
                lines.append(
                    L(
                        "· 部件　遗物表未收录（发「遗物 名称」查询）",
                        '· Parts　Not in the relic table (send "relic <name>" to look up)',
                    )
                )
    other = [it for it in items if it.get("kind") not in known and it.get("kind") != "relic"]
    if other:
        names = L("、", ", ").join(it["name"] for it in other[:3])
        more = (
            L(f" 等 {len(other)} 件", f" and {len(other)} items in total") if len(other) > 3 else ""
        )
        lines.append(
            L(
                f"※ 另有装饰 / 组合包：{names}{more}（不能用遗物刷取；发「阿耶」看带价格的完整兑换表）",
                f'※ Also decor / bundles: {names}{more} (not obtainable from relics; send "varzia" for the full priced list)',
            )
        )
    if not items:
        lines.append(
            L(
                "◆ 出库：本期没有出库的 Prime 道具",
                "◆ Vault: no Prime items leaving the vault this rotation",
            )
        )
    lines.append(
        L(
            "※ 战甲 / 武器 / 守护为整套名；遗物部件取自遗物掉落表快照，单件出处发「遗物 名称」查询",
            '※ Warframes / weapons / companions are full-set names; relic parts come from a drop-table snapshot, send "relic <name>" for a single part',
        )
        if relics
        else L(
            "※ 均为整套（不含部件）；部件与出处发「遗物 名称」查询",
            '※ Full sets only (no parts); send "relic <name>" for parts and sources',
        )
    )
    lines += _vault_unnamed_note(
        {
            "unnamed": [
                it["name"]
                for it in items
                if "（未收录）" in it["name"] or "(unlisted)" in it["name"]
            ]
        }
    )
    return (L("Prime 出库清单（本期）", "Prime Vault List (Current)"), lines)


def fmt_clan_rewards(rewards: Iterable[dict]) -> tuple[str, list[str]]:
    """每周氏族组队奖励。"""
    rewards = list(rewards or [])
    if not rewards:
        return (
            L("氏族组队奖励", "Clan Rewards"),
            [
                L(
                    "◆ 状态：本周暂无可用的氏族组队奖励",
                    "◆ Status: No clan team rewards available this week",
                )
            ],
        )
    lines = []
    for w in rewards:
        lines.append(
            L(
                f"◆ {w.get('region')}（第 {w.get('week')} 周）",
                f"◆ {w.get('region')} (Week {w.get('week')})",
            )
        )
        lines.append(L("　积分　奖励", "　Points　Reward"))
        for r in w.get("rewards") or []:
            cnt = f" ×{r['count']}" if r.get("count") else ""
            lines.append(
                L(
                    f"· {r.get('points')} 分　{str(r.get('name')).replace('　', ' ')}{cnt}",
                    f"· {r.get('points')} pts　{str(r.get('name')).replace('　', ' ')}{cnt}",
                )
            )
    return (L("每周氏族组队奖励", "Weekly Clan Rewards"), lines)


def fmt_flash_sales(
    sales: Iterable[dict], page: int = 1, page_size: int = 15
) -> tuple[str, list[str]]:
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
        return (
            L("商城在售礼包", "Flash Sales"),
            [L("◆ 状态：当前没有在售的限时礼包", "◆ Status: No limited-time bundles on sale")],
        )
    total = len(sales)
    pages = max(1, (total + page_size - 1) // page_size)
    page = max(1, min(page, pages))
    chunk = sales[(page - 1) * page_size : page * page_size]
    n_long = sum(1 for s in sales if (s.get("end") or "")[:10] >= "2029")
    lines = [
        L(
            f"◆ 在售礼包：共 {total} 项｜长期在售 {n_long} 项｜限时 {total - n_long} 项",
            f"◆ On sale: {total} total｜{n_long} long-running｜{total - n_long} limited-time",
        ),
        L("◆ 礼包", "◆ Bundles"),
        L("　礼包　期限", "　Bundle　Duration"),
    ]
    for s in chunk:
        end = (s.get("end") or "")[:10]
        long_run = end >= "2029"  # DE 用 2030 表示「长期在售」
        tag = (
            L("长期在售", "Long-running")
            if long_run
            else (L(f"至 {end}", f"Until {end}") if end else "—")
        )
        lines.append(f"· {str(s.get('name')).replace('　', ' ')}　{tag}")
    if pages > 1:
        lines.append(
            L(
                f"※ 第{page}/{pages}页，共{total}项；加 -2 / -3 翻页",
                f"※ (Page {page}/{pages}, {total} total) add -2 / -3 to flip pages",
            )
        )
    lines.append(L("※ DE 未提供礼包中文名时显示原名", "※ Bundle names are shown as provided by DE"))
    return (L("游戏内商店在售礼包", "In-Game Store Flash Sales"), lines)


# ---------------------------------------------------------------------------
# 市场排行 / 趋势
# ---------------------------------------------------------------------------


def _item_name(r: dict) -> str:
    """排行行的物品名：中文模式 zh 优先，英文模式 en 优先。"""
    if i18n.is_en():
        return r.get("en") or r.get("zh") or r.get("slug") or "?"
    return r.get("zh") or r.get("en") or r["slug"]


# 排行分类键（用户输入口径，别改）→ 英文分类名
_RANK_CAT_EN = {
    "甲": "Warframe",
    "武器": "Weapon",
    "卡": "MOD",
    "部件": "Parts",
    "赋能": "Arcane",
    "主武": "Primary",
    "副武": "Secondary",
    "近战": "Melee",
    "遗物": "Relic",
}


def _short_t(t: str, hourly: bool) -> str:
    """把统计时间戳压短：日线只留 MM-DD，小时线留 MM-DD HH时。

    ``hourly`` 由**调用方按序列口径**给（不能按序列长度猜：48 小时线有 48 个点，
    比 90 天日线的点数还多，长度启发式会把小时线判成日线 —— 2026-10-23 修）。
    """
    s = (t or "").strip()
    if len(s) < 16:
        return s
    day = s[:10][5:]
    return L(f"{day} {s[11:13]}时", f"{day} {s[11:13]}h") if hourly else day


# 图表行协议（见 core/render.py「图表行（§CHART）」一节）：格式层只产出**机器行**，
# 画图模式画真实折线图、文本模式展开回方块字 + 「最高/最低/最新」一行。
# JSON 一律 ASCII（ensure_ascii）—— 机器行不参与折行与列宽判定，含全角字符会撑爆卡宽。
def _chart_line(title: str, rows: list[dict], *, hourly: bool, unit: str = "p") -> str:
    """把统计行（``t`` / ``median``）编成一条 §CHART 机器行。

    ``median`` 为 0（该小时 / 该日无成交）记 ``null`` —— 渲染层据此**断开折线**，
    不把无成交时段连成一条假直线。
    """
    labels = [_short_t(r.get("t") or "", hourly) for r in rows]
    values = [(r.get("median") or None) for r in rows]
    return "§CHART" + json.dumps(
        {"title": title, "unit": unit, "labels": labels, "values": values},
        ensure_ascii=True,
        separators=(",", ":"),
    )


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
    "武器": lambda t: (
        "weapon" in t and "mod" not in t and "blueprint" not in t and "component" not in t
    ),
    "卡": lambda t: "mod" in t,
    "部件": lambda t: "component" in t,
    "赋能": lambda t: "arcane_enhancement" in t,
    "主武": lambda t: "weapon" in t and "primary" in t and "mod" not in t,
    "副武": lambda t: "weapon" in t and "secondary" in t and "mod" not in t,
    "近战": lambda t: "weapon" in t and "melee" in t and "mod" not in t,
    "遗物": lambda t: "relic" in t,
}


def _rank_sort_key(r: dict) -> float:
    """排序价：只看当前价（48h 成交中位）—— 维护者明确要求按当前价格排序。"""
    return r.get("median48") or 0


# 价格排行入榜的最低 48h 成交笔数（2026-10-25 维护者定：MOD 榜首「均衡点 (Steam Pinnacle Pack)」1600p
# 只有零星成交，疑似 RMT 过账价）—— 冷门物品一两笔离谱成交不代表行情，不进榜
RANK_MIN_VOL48 = 3
RANK_MIN_VOL_NOTE = (
    f"※ 48 小时成交不足 {RANK_MIN_VOL48} 笔的物品不进榜（防冷门物品的刷单 / RMT 过账价把榜单带偏）"
)
_RANK_MIN_VOL_NOTE_EN = (
    f"※ Items with fewer than {RANK_MIN_VOL48} trades in 48h are left out "
    "(keeps wash trades / RMT prices on obscure items from skewing the rankings)"
)


def _rank_has_price(r: dict) -> bool:
    """无当前价（48h）的行不进榜（落盘层已滤，这里兜底）；成交笔数不足的也不进榜。"""
    if not r.get("median48"):
        return False
    vol = r.get("vol48")
    return vol is None or vol >= RANK_MIN_VOL48


def fmt_rank_overview(rows: list[dict]) -> tuple[str, list[str]]:
    """裸「排行」：甲 / 武器 / MOD卡 三组各前五（当前成交中位价降序）。

    卡组在落盘时已只保留 0 级成交（维护者要求「全部可交易的 0 级卡片」）。
    """
    groups = [
        ("甲", L("甲价格排行", "Warframe Price Ranking")),
        ("武器", L("武器价格排行", "Weapon Price Ranking")),
        ("卡", L("MOD卡价格排行", "MOD Price Ranking")),
    ]
    lines: list[str] = []
    tops: list[str] = []  # 卡首答案：各榜榜首
    empty = True
    for cat, title in groups:
        pred = _RANK_TAG_PRED[cat]
        sel = sorted(
            [r for r in rows if pred(set(r.get("tags") or [])) and _rank_has_price(r)],
            key=_rank_sort_key,
            reverse=True,
        )[:5]
        if not sel:
            continue
        empty = False
        # ◆ 行是 section（整行绘制、不参与列对齐），表头必须独立成行才能与
        # 数据列对齐
        lines.append(f"◆ {title}")
        lines.append(
            L("　名称　当前价　最低　最高　上期中位", "　Name　Current　Low　High　Prev Median")
        )
        _tl = {
            "甲": L("甲", "Warframes"),
            "武器": L("武器", "Weapons"),
            "卡": L("MOD卡", "MODs"),
        }[cat]
        tops.append(f"{_tl} {_item_name(sel[0])} {_fmt_p(sel[0].get('median48'))}")
        for r in sel:
            name = _item_name(r)
            lines.append(
                "· "
                + "　".join(
                    [
                        name,
                        _fmt_p(r.get("median48")),
                        _fmt_p(r.get("min48")),
                        _fmt_p(r.get("max48")),
                        _fmt_p(r.get("median_prev")),
                    ]
                )
            )
    if empty:
        return (
            L("价格排行", "Price Rankings"),
            [
                L(
                    "暂无落盘数据，需管理员发「排行 刷新」建立全量榜单",
                    'No stored data yet; an admin must send "rank refresh" to build the full rankings',
                )
            ],
        )
    # ★ 2026-10-03 口径注脚（选 A）：数据源 = statistics_closed 真实成交，
    #   不是 statistics_live 挂单价（第三方「加权平均价」实为后者，实测高 13~15%）。
    lines.insert(0, L("◆ 各榜榜首：", "◆ Top of each list: ") + "｜".join(tops))
    lines.append(
        L(
            "※ 主要物品的当前成交价；当前价=48h 成交中位（真实成交·非挂单价；挂单请看「wm 物品名」）",
            "※ Current trade prices of major items; Current = 48h median of completed trades "
            '(real trades, not listing prices; see "wm <item>" for listings)',
        )
    )
    lines.append(
        L(
            "※ 按当前价降序；最低/最高=48h 成交区间；上期中位=前一 48 小时成交中位",
            "※ Sorted by current price, descending; Low/High = 48h trade range; "
            "Prev Median = median of the previous 48 hours",
        )
    )
    lines.append(
        L(
            "※ 完整 20 名榜单：「排行 甲/武器/卡/赋能/部件/主武/副武/近战/遗物」",
            '※ Full top-20 lists: "rank warframe/weapon/mod/arcane/parts/primary/secondary/melee/relic"',
        )
    )
    lines.append(L(RANK_MIN_VOL_NOTE, _RANK_MIN_VOL_NOTE_EN))
    return (L("价格排行榜", "Price Rankings"), lines)


def fmt_rank_table(category: str, rows: list[dict]) -> tuple[str, list[str]]:
    """分类价格榜：取 20 种按当前成交中位价降序。"""
    pred = _RANK_TAG_PRED.get(category)
    cat_title = L(f"{category}价格排行", f"{_RANK_CAT_EN.get(category, category)} Price Rankings")
    if not pred:
        return (cat_title, [L(f"未知分类「{category}」", f'Unknown category "{category}"')])
    sel = sorted(
        [r for r in rows if pred(set(r.get("tags") or [])) and _rank_has_price(r)],
        key=_rank_sort_key,
        reverse=True,
    )[:20]
    if not sel:
        return (
            cat_title,
            [
                L(
                    "该分类暂无成交数据（榜单未建立或物品无成交）",
                    "No trade data for this category (rankings not built yet, or no trades)",
                )
            ],
        )
    _top = sel[0]
    lines = [
        L(
            f"◆ 榜首：{_item_name(_top)}｜当前价 {_fmt_p(_top.get('median48'))}｜共 {len(sel)} 名",
            f"◆ Top: {_item_name(_top)}｜Current {_fmt_p(_top.get('median48'))}｜{len(sel)} listed",
        ),
        L(
            f"◆ 前 {len(sel)} 名 · 按当前成交中位价降序",
            f"◆ Top {len(sel)} · by current median trade price, descending",
        ),
        L("　名称　当前价　最低　最高　上期中位", "　Name　Current　Low　High　Prev Median"),
    ]
    for r in sel:
        name = _item_name(r)
        lines.append(
            "· "
            + "　".join(
                [
                    name,
                    _fmt_p(r.get("median48")),
                    _fmt_p(r.get("min48")),
                    _fmt_p(r.get("max48")),
                    _fmt_p(r.get("median_prev")),
                ]
            )
        )
    # ★ 2026-10-03 口径注脚（同上）：真实成交↔挂单价 必须写明，防与挂单站对比误判。
    lines.append(
        L(
            "※ 当前价=48h 成交中位（真实成交·非挂单价）　上期中位=前一 48 小时；MOD 卡按 0 级成交计",
            "※ Current = 48h median of completed trades (real trades, not listing prices)　"
            "Prev Median = previous 48 hours; MODs counted at rank 0",
        )
    )
    lines.append(L(RANK_MIN_VOL_NOTE, _RANK_MIN_VOL_NOTE_EN))
    return (cat_title, lines)


# DE 周报 itemType → 中文分类（「主武」= 步枪+霰弹枪合并，「副武」= 手枪）
_RIVEN_ITEM_TYPE = {
    "近战": ("Melee Riven Mod",),
    "步枪": ("Rifle Riven Mod",),
    "霰弹枪": ("Shotgun Riven Mod",),
    "手枪": ("Pistol Riven Mod",),
    "空战": ("Archgun Riven Mod",),
    "Zaw": ("Zaw Riven Mod",),
    "组合枪": ("Kitgun Riven Mod",),
    "主武": ("Rifle Riven Mod", "Shotgun Riven Mod"),
    "副武": ("Pistol Riven Mod",),
}


def _riven_pair(entries: list[dict], itypes: tuple, veiled: bool = False) -> dict:
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


# 官方物品名「<类别> Riven Mod」（DE 词表），未开状态按 WM 的写法加 (Veiled)
_VEILED_TYPE_EN = {
    "Melee": "Melee Riven Mod (Veiled)",
    "Rifle": "Rifle Riven Mod (Veiled)",
    "Shotgun": "Shotgun Riven Mod (Veiled)",
    "Pistol": "Pistol Riven Mod (Veiled)",
    "Archgun": "Archgun Riven Mod (Veiled)",
    "Zaw": "Zaw Riven Mod (Veiled)",
    "Kitgun": "Kitgun Riven Mod (Veiled)",
    "Robotic": "Companion Weapon Riven Mod (Veiled)",
    "Amalgam": "Amalgam Riven Mod (Veiled)",
}
_RIVEN_CAT_EN = {
    "近战": "Melee",
    "步枪": "Rifle",
    "霰弹枪": "Shotgun",
    "手枪": "Pistol",
    "空战": "Archgun",
    "Zaw": "Zaw",
    "组合枪": "Kitgun",
    "主武": "Primary",
    "副武": "Secondary",
    "未开": "Unrevealed",
}


def _veiled_name(key: str) -> str:
    """未开紫卡类别名（键为 DE 的 Melee / Rifle / … 英文类别）。"""
    if i18n.is_en():
        return _VEILED_TYPE_EN.get(key, f"{key} Riven Mod (Veiled)")
    return _VEILED_TYPE_ZH.get(key, key + "未开紫卡")


_VEILED_TYPE_ZH = {
    "Melee": "近战未开紫卡",
    "Rifle": "步枪未开紫卡",
    "Shotgun": "霰弹枪未开紫卡",
    "Pistol": "手枪未开紫卡",
    "Archgun": "空战未开紫卡",
    "Zaw": "Zaw 未开紫卡",
    "Kitgun": "组合枪未开紫卡",
    "Robotic": "守护未开紫卡",
    "Amalgam": "融合未开紫卡",
}

# 「wr 未开」不带类别时用的固定展示顺序（与 _VEILED_SLUGS 的键一致）
_VEILED_ORDER = ("Melee", "Rifle", "Shotgun", "Pistol", "Archgun", "Kitgun", "Zaw", "Robotic")


def fmt_wm_veiled_summary(stats: dict) -> tuple[str, list[str]]:
    """「wr 未开」不带类别：列 8 个类别当前中位/最低/最高（数据源 `wm_veiled_stats`）。

    ★ 2026-10-23 加：玩家报「wr 手枪未开 搜不到」，顺手给无类别时一个总览入口。
    """
    if not stats:
        return (
            L("未开紫卡 · warframe.market", "Unrevealed Rivens · warframe.market"),
            [
                L(
                    "※ 未取到未开紫卡的实时成交（稍后再试）",
                    "※ Could not fetch live trades for unrevealed rivens (try again later)",
                )
            ],
        )
    lines = [
        L(
            "◆ 未开紫卡 · warframe.market 实时成交（48h）",
            "◆ Unrevealed Rivens · live warframe.market trades (48h)",
        ),
        L("　类别　当前中位　最低　最高", "　Type　Median　Low　High"),
    ]
    for key in _VEILED_ORDER:
        d = stats.get(key)
        if not d:
            continue
        lines.append(
            "· "
            + "　".join(
                [
                    _veiled_name(key),
                    _fmt_p(d.get("median")),
                    _fmt_p(d.get("min")),
                    _fmt_p(d.get("max")),
                ]
            )
        )
    lines.append(
        L(
            "※ 加类别看具体卖家：wr 手枪未开（也认 近战/步枪/霰弹/曲翼/组合枪/Zaw/守护）",
            "※ Add a type to see sellers: wr pistol unrevealed "
            "(also melee/rifle/shotgun/archgun/kitgun/zaw/companion)",
        )
    )
    return (L("未开紫卡 · 各类别行情", "Unrevealed Rivens · Prices by Type"), lines)


# 「WM挂单 vs 周报参考价」的变化：相对偏离不足此值记「≈」（挂单价≠成交价，只看大幅偏离）
_RIVEN_NOW_FLAT = 0.15


def _riven_delta(ref, now) -> str:
    """周报参考价 → WM 挂单中位的变化：``▲133%`` / ``▼19%`` / ``≈`` / ``—``（缺数）。"""
    if not ref or not now:
        return "—"
    r = float(now) / float(ref) - 1
    if abs(r) < _RIVEN_NOW_FLAT:
        return "≈"
    return f"{'▲' if r > 0 else '▼'}{abs(r) * 100:.0f}%"


def _riven_row(
    name: str, d: dict, veiled: bool = False, *, now: Optional[dict] = None, with_now: bool = False
) -> str:
    """紫卡榜一行：武器　0洗中位　0洗热度　已洗中位　已洗热度　参考价［　WM挂单　变化］。

    ``with_now``（2026-10-25）：管理员手动拉过 WM 现价时追加两列；``now`` = 该武器的快照行
    （``median`` = 挂单最低 20 条中位），没抓到的武器两列写「—」。
    """
    if veiled:  # 未开卡只有一张「中位 + 热度」，放进 0 洗列
        d = {"m0": d.get("m0"), "p0": d.get("p0"), "m1": None, "p1": None}
    ref = min([x for x in (d.get("m0"), d.get("m1")) if x], default=None)
    cells = [
        name,
        _fmt_p(d.get("m0")),
        f"{d['p0']}%" if d.get("p0") is not None else "—",
        _fmt_p(d.get("m1")),
        f"{d['p1']}%" if d.get("p1") is not None else "—",
        _fmt_p(ref),
    ]
    if with_now:
        med = (now or {}).get("median")
        cells += [_fmt_p(med) if med else "—", _riven_delta(ref, med)]
    return "· " + "　".join(cells)


_RIVEN_HEAD = "　武器　0洗中位　0洗热度　已洗中位　已洗热度　参考价"
_RIVEN_HEAD_EN = "　Weapon　Unrolled Median　Unrolled Pop　Rerolled Median　Rerolled Pop　Ref Price"


def _riven_head(with_now: bool = False) -> str:
    """紫卡热度榜表头；with_now 时追加「WM挂单 / 变化」两列。"""
    return L(_RIVEN_HEAD, _RIVEN_HEAD_EN) + (
        L("　WM挂单　变化", "　WM Listing　Change") if with_now else ""
    )


def _riven_now_note(now_ts: str) -> str:
    """WM 现价两列的注脚；没拉过现价时提示管理员可拉。"""
    if not now_ts:
        return L(
            "※ 大更后价格波动大时，管理员可发「紫卡排行 刷新」拉一次 WM 现价对比（重抓保护 10 分钟，耗时看进度）",
            "※ When prices swing after a big update, an admin can send "
            '"rank riven refresh" to pull live WM prices for comparison '
            "(10-minute re-crawl guard; progress shown while running)",
        )
    when = format_time(now_ts, "%Y-%m-%d %H:%M")
    pulled = L(f" {when}", f" at {when}") if when else ""
    return L(
        f"※ WM挂单 = 管理员{pulled} 手动拉取的挂单最低 20 条中位；变化 = 对比周报参考价"
        f"（挂单价≠成交价，±{_RIVEN_NOW_FLAT * 100:.0f}% 内记 ≈，看大幅偏离即可）",
        f"※ WM Listing = median of the 20 lowest listings, pulled manually by an admin{pulled}; "
        f"Change = vs. the weekly-report reference price "
        f"(listing price != trade price; within ±{_RIVEN_NOW_FLAT * 100:.0f}% counts as ≈, "
        "only large deviations matter)",
    )


def _riven_weapon_name(c, zhmap: dict) -> str:
    """周报条目的武器名：中文模式取 zhmap 译名，英文模式直接用英文原名。"""
    if i18n.is_en():
        return c or "Unrevealed Riven"
    return zhmap.get((c or "").lower()) or c or "未开紫卡"


def fmt_riven_weekly(
    snap: dict, zhmap: dict, *, now: Optional[dict] = None, now_ts: str = ""
) -> tuple[str, list[str]]:
    """裸「紫卡排行」：近战/手枪/步枪/霰弹枪/未开 五组各前五（按周销量热度）。

    ``now`` / ``now_ts``（2026-10-25）：管理员手动拉的 WM 现价（键 = 武器英文名小写），
    有就每行追加「WM挂单 / 变化」两列。
    """
    entries = snap.get("entries") or []
    with_now = bool(now)
    lines: list[str] = []
    for label, itype in (
        (L("近战", "Melee"), ("Melee Riven Mod",)),
        (L("手枪", "Pistol"), ("Pistol Riven Mod",)),
        (L("步枪", "Rifle"), ("Rifle Riven Mod",)),
        (L("霰弹枪", "Shotgun"), ("Shotgun Riven Mod",)),
        (L("空战", "Archgun"), ("Archgun Riven Mod",)),
    ):
        pool = _riven_pair(entries, itype)
        ranked = sorted(pool, key=lambda c: -(pool[c]["p0"] or 0))[:5]
        if not ranked:
            continue
        lines.append(f"◆ {label}")
        lines.append(_riven_head(with_now))
        for c in ranked:
            name = _riven_weapon_name(c, zhmap)
            lines.append(
                _riven_row(name, pool[c], now=(now or {}).get((c or "").lower()), with_now=with_now)
            )
    when = format_time(snap.get("fetched"))
    lines.append(
        L(
            "※ 0洗与已洗并列展示，按本周成交热度（≈7 天销量）降序；参考价为两者较低的中位价",
            "※ Unrolled and rerolled are shown side by side, sorted by this week's trade popularity "
            "(~7-day sales) descending; Ref Price is the lower of the two medians",
        )
    )
    lines.append(
        L(
            "※ 完整榜：「紫卡排行 近战/步枪/霰弹枪/手枪/主武/副武/空战/组合枪/Zaw/未开」"
            "｜未开紫卡实时价（warframe.market 成交）：「紫卡排行 未开」",
            '※ Full lists: "rank riven melee/rifle/shotgun/pistol/primary/secondary/archgun/kitgun/zaw/unrevealed"'
            '｜Live unrevealed riven prices (warframe.market trades): "rank riven unrevealed"',
        )
    )
    lines.append(_riven_now_note(now_ts if with_now else ""))
    lines.append(
        L(
            f"※ DE 官方周报（每周一更新），抓取于 {when or '时间未知'}",
            f"※ DE official weekly report (updated every Monday), fetched {when or 'at an unknown time'}",
        )
    )
    return (L("紫卡热度排行榜", "Riven Trends"), lines)


def _riven_page_note(cat: str, page: int, pages: int, veiled: bool = False) -> str:
    """紫卡热度榜的翻页注脚（veiled=True 为「未开」视图，命令词固定为「未开」）。"""
    zh_cat = "未开" if veiled else cat
    en_cat = "unrevealed" if veiled else _RIVEN_CAT_EN.get(cat, cat).lower()
    if page < pages:
        return L(
            f"※ 页码:{page}/{pages}　使用「紫卡排行 {zh_cat} -{page + 1}」查看下一页",
            f'※ (Page {page}/{pages}) send "rank riven {en_cat} -{page + 1}" for the next page',
        )
    return L(f"※ 页码:{page}/{pages}　已是最后一页", f"※ (Page {page}/{pages}) last page")


def fmt_riven_type(
    cat: str,
    snap: dict,
    zhmap: dict,
    page: int = 1,
    page_size: int = 10,
    veiled_wm: Optional[dict] = None,
    now: Optional[dict] = None,
    now_ts: str = "",
) -> tuple[str, list[str]]:
    """「紫卡排行 类型」：按首要排序 0洗热度降序、次要已洗热度降序；参考价=较低中位。

    ``now`` / ``now_ts``：同 `fmt_riven_weekly`（未开视图不加）。

    ``veiled_wm``：未开紫卡的 WM 实时成交价（``wm_veiled_stats`` 产物）。
    DE 周报的未开条目是一周口径且明显偏高（组合枪/Zaw 报 590-600p，WM 实时
    个位数 P），有 WM 数据时未开视图一律用它。
    """
    if cat == "未开" and veiled_wm:
        rows = sorted(veiled_wm.items(), key=lambda kv: -(kv[1].get("median") or 0))
    elif cat == "未开":
        # 未开紫卡 = compatibility 为 null 的条目，按武器类别各有一条
        # （键形如「未开·Melee Riven Mod」，翻译成中文类别名）。
        # ⚠️ 必须先把条目过滤到只剩未开，否则 itemType 过滤会把该类别
        # 的所有武器都带进来（上一版就因此列出 419 "种"）。
        veiled_entries = [e for e in (snap.get("entries") or []) if not e.get("compatibility")]
        pool = _riven_pair(
            veiled_entries, tuple(e["itemType"] for e in veiled_entries), veiled=True
        )
        rows = sorted(pool.items(), key=lambda kv: -(kv[1]["p0"] or 0))
    else:
        itypes = _RIVEN_ITEM_TYPE.get(cat)
        if not itypes:
            return (
                L(f"紫卡热度·{cat}", f"Riven Trends: {cat}"),
                [
                    L(
                        f"未知分类「{cat}」。可用：近战/步枪/霰弹枪/手枪/主武/副武/空战/组合枪/Zaw/未开",
                        f'Unknown category "{cat}". Available: '
                        "melee/rifle/shotgun/pistol/primary/secondary/archgun/kitgun/zaw/unrevealed",
                    )
                ],
            )
        pool = _riven_pair(snap.get("entries") or [], itypes)
        # 首要排序：0洗热度；次要：已洗热度（均降序）
        rows = sorted(pool.items(), key=lambda kv: (-(kv[1]["p0"] or 0), -(kv[1]["p1"] or 0)))
    cat_title = L(f"紫卡热度·{cat}", f"Riven Trends: {_RIVEN_CAT_EN.get(cat, cat)}")
    if not rows:
        return (
            cat_title,
            [
                L(
                    "DE 本周数据里该分类暂无成交记录",
                    "No trade records for this category in this week's DE data",
                )
            ],
        )
    total = len(rows)
    pages = max(1, (total + page_size - 1) // page_size)
    page = max(1, min(page, pages))
    chunk = rows[(page - 1) * page_size : page * page_size]
    if cat == "未开" and veiled_wm:
        lines = [
            L(
                "◆ 未开紫卡 · warframe.market 实时成交（48h 中位）",
                "◆ Unrevealed Rivens · live warframe.market trades (48h median)",
            )
        ]
        lines.append(L("　类别　当前中位　最低　最高", "　Type　Median　Low　High"))
        for tkey, d in chunk:
            name = _veiled_name(tkey)
            lines.append(
                "· "
                + "　".join(
                    [name, _fmt_p(d.get("median")), _fmt_p(d.get("min")), _fmt_p(d.get("max"))]
                )
            )
        lines.append(
            L(
                "※ 来源：warframe.market 未开紫卡条目的实时成交统计（DE 周报口径滞后且偏高，已弃用）",
                "※ Source: live trade stats of unrevealed rivens on warframe.market "
                "(the DE weekly figures lag and run high, so they are no longer used)",
            )
        )
        lines.append(_riven_page_note(cat, page, pages, veiled=True))
        return (cat_title, lines)
    lines = [
        L(
            f"◆ 共 {total} 种 · 按首要 0洗热度降序（次要：已洗热度）",
            f"◆ {total} total · sorted by unrolled popularity, descending (then rerolled popularity)",
        )
    ]
    with_now = bool(now) and cat != "未开"
    if cat == "未开":
        lines.append(L("　类别　中位　热度　参考价", "　Type　Median　Pop　Ref Price"))
    else:
        lines.append(_riven_head(with_now))
    for c, d in chunk:
        if cat == "未开":
            base_t = (c or "").replace("未开·", "").replace(" Riven Mod", "")
            name = _veiled_name(base_t)
            ref = d.get("m0") or d.get("m1")
            lines.append(
                "· "
                + "　".join(
                    [
                        name,
                        _fmt_p(d.get("m0")),
                        f"{d['p0']}%" if d.get("p0") is not None else "—",
                        _fmt_p(ref),
                    ]
                )
            )
            continue
        name = _riven_weapon_name(c, zhmap)
        lines.append(_riven_row(name, d, now=(now or {}).get((c or "").lower()), with_now=with_now))
    if cat != "未开":
        lines.append(_riven_now_note(now_ts if with_now else ""))
    lines.append(_riven_page_note(cat, page, pages))
    return (cat_title, lines)


_DISP_DOTS = {5: "●●●●●", 4: "●●●●○", 3: "●●●○○", 2: "●●○○○", 1: "●○○○○"}


def _disp_dots(disposition: float) -> str:
    """倾向值 → 五档圆点（与 wiki「倾向」星级同口径）。"""
    stars = next(
        (v for lo, v in ((1.31, 5), (1.11, 4), (0.9, 3), (0.7, 2), (0.5, 1)) if disposition >= lo),
        1,
    )
    return _DISP_DOTS.get(stars, "?")


def _range_txt(RA, sid: str, lo: float, hi: float) -> str:
    """区间「96.2~117.6%」：单位只写一次（在上限后面）。"""
    a, b = RA.fmt_value(sid, lo), RA.fmt_value(sid, hi)
    unit = b[len(b.rstrip("%ms")) :]
    if unit and a.endswith(unit):
        a = a[: -len(unit)]
    return f"{a}~{b}"


def _riven_range_text(RA, sid: str, lo: float, hi: float, *, negative: bool) -> str:
    """一条词条的卡面区间文本（符号 + 单位只写一次）。

    · **派系伤害**（对 G/C/I/O/S/T）按**乘数**显示（基值 45% ⇒ ×1.45~1.56，用户口径）；
    · 反转词条（后坐力，`RA.INVERTED_STATS`）的卡面符号与收益方向相反 ——
      负面表里写「+」（+95% 后坐力才是负面）；
    · 其余按 `_range_txt` 的既有写法（单位只写在上限后面）。
    """
    # ★ 2026-10-24 Claude 审：区间统一保留 1 位小数（参考卡口径 +91.1%~+111.4%）——
    #   两位小数让四列并排时最宽的格（「−117.61~−96.23%」）断行；派系乘数仍两位。
    if not sid.startswith("damage_vs_"):
        lo, hi = round(lo + 1e-9, 1), round(hi + 1e-9, 1)
    if sid.startswith("damage_vs_"):
        # ★ 2026-10-23 Claude 审：负面派系是**减伤**，乘数 < 1（参考卡 0.55×~0.64×），旧写法给成 ×1.36~1.45
        if negative:
            return f"×{1 - hi / 100:.2f}~{1 - lo / 100:.2f}"
        return f"×{1 + lo / 100:.2f}~{1 + hi / 100:.2f}"
    # 卡面符号：正面「+」、负面「−」；反转词条（后坐力）反过来 —— 正面是「−后坐力」、负面是「+后坐力」
    plus = (not negative) != bool(RA.is_inverted(sid))
    if plus:
        return "+" + _range_txt(RA, sid, lo, hi)
    # 负号区间两头都带符号、绝对值大的在前（「−89.1~−72.9%」），避免读成「−89.1 到 +72.9」
    a, b = _range_txt(RA, sid, hi, lo).split("~", 1)
    return f"−{a}~−{b}"


# 「词条范围」的组合写法 → 中文标签（卡面用）
_RIVEN_COMBO_ZH = {(2, 0): "2正", (2, 1): "2正1负", (3, 0): "3正", (3, 1): "3正1负"}
# 卡上给出的组合切换提示（列出当前组合以外的三种）
_RIVEN_COMBO_EN = {
    (2, 0): "2 Pos",
    (2, 1): "2 Pos 1 Neg",
    (3, 0): "3 Pos",
    (3, 1): "3 Pos 1 Neg",
}


def _combo_label(c: tuple[int, int]) -> str:
    """组合标签：中文「3正1负」/ 英文「3 Pos 1 Neg」。"""
    return (_RIVEN_COMBO_EN if i18n.is_en() else _RIVEN_COMBO_ZH).get(c, "")


def _stat_name(sid: str, cls: str = "") -> str:
    """标准词条 id → 展示名（中文取 RIVEN_STAT_ZH，英文取游戏内英文名）。

    cls 为基值列名（rifle / melee …）：英文卡面上基伤枪械写 Damage、近战写 Melee Damage。
    """
    from .parser import RIVEN_STAT_ZH

    if i18n.is_en():
        if sid == "melee_damage" and cls and cls != "melee":
            return "Damage"
        return _riven_stat_en(sid, sid)
    return RIVEN_STAT_ZH.get(sid, sid)


_RIVEN_COMBO_HINT = {(2, 0): "2+", (2, 1): "2+1-", (3, 0): "3+", (3, 1): "3+1-"}


# 「词条范围」的四种组合（**列顺序 = 卡面默认顺序**）；用户指定哪个就提到首列并高亮
_RIVEN_COMBO_COLS = ((3, 1), (3, 0), (2, 1), (2, 0))

# 组合 → 染段字母（见 core/render.py 的 _SPAN_COLORS / _SPAN_DIM）。
# ★ 颜色跟**组合**走、不跟列位置走：指定组合提到首列后颜色不变；渲染层再按
#   「是否指定列」选全饱和（a/m/y/t）或与 INK_DIM 各半的次列变体（d/e/f/h）。
#   暖冷分组 = 有没有负词条槽：带负用暖色（AMBER / MAGENTA）、无负用冷色（CYAN / STEEL）。
_RIVEN_COMBO_SPAN = {(3, 1): "a", (2, 1): "m", (3, 0): "y", (2, 0): "t"}


def fmt_riven_ranges(
    weapon: str,
    cls: str,
    combo: tuple[int, int],
    pos: list[str],
    spliced: list[str],
    neg: list[str],
    unconfirmed: set,
    *,
    only: Optional[list[str]] = None,
    dropped: Optional[list[str]] = None,
    survey: Optional[dict] = None,
    stat_multipliers: Optional[dict] = None,
) -> tuple[str, list[str]]:
    """「词条范围」卡：按武器列出紫卡每条词条在**四种组合**下的卡面取值区间。

    只显示当前统一倾向（`RA.UNIFIED_DISPOSITION` = 1.2，2026-10 起 DE 取消旧倾向）。

    版面：三段（正面 / 融合词条·正面 / 负面），每段一张**五列表**——
    ``词条 | 3正1负 | 3正 | 2正1负 | 2正``。用户指定了组合就把那一列提到首列并**高亮**
    （渲染层用金色染段标记；纯文字模式取不到颜色，改成只出那一列 + 一行提示）。
    负面段只有 ``3正1负`` / ``2正1负`` 两列有值（``3+`` / ``2+`` 没有负词条槽）⇒ 只出这两列。

    数据经 `§RANGES{json}` 机器行交给渲染层（见 core/render.py 的「词条范围」一节）——
    与 `§CHART` / `⟦tents⟧` 同一套做法：格式层只产出机器行，文本模式展开回单列。

    Args:
        weapon: 展示用武器名。
        cls: 基值列名（rifle / shotgun / pistol / archgun / melee）。
        combo: 用户指定（或默认）的 ``(正词条数, 负词条数)``。
        pos / spliced / neg: 三段各自的词条 id（已按武器过滤）。
        unconfirmed: 物理「未确认」词条 id（卡上标「?」）。
        only: 用户显式点名的词条（非空时卡上注明已过滤）。
        dropped: 用户点名了、但该武器刷不出（或被过滤掉）的词条展示名。
    """
    from . import riven_analysis as RA

    disp = RA.UNIFIED_DISPOSITION
    # 列顺序：指定组合提到首列（仍出全部四列）
    cols = list(_RIVEN_COMBO_COLS)
    if combo in cols:
        cols.remove(combo)
        cols.insert(0, combo)

    def _cell(sid: str, c: tuple[int, int], *, negative: bool) -> str:
        lo, hi = RA.stat_range(
            sid, cls, disp, c[0], c[1], negative=negative, stat_multipliers=stat_multipliers
        )
        if lo is None:
            return "—"
        return _riven_range_text(RA, sid, lo, hi, negative=negative)

    lines: list[str] = []
    for label, sids, is_neg in (
        (L("正面", "Positive"), pos, False),
        (L("融合词条·正面", "Spliced Stats · Positive"), spliced, False),
        (L("负面", "Negative"), neg, True),
    ):
        # 负面段只出「带负词条槽」的组合（3+ / 2+ 整列都会是「—」，留着只是噪声）
        sec = [c for c in cols if c[1] > 0] if is_neg else list(cols)
        if not sec or not sids:
            continue
        rows = []
        for sid in sids:
            lo, _hi = RA.stat_range(
                sid,
                cls,
                disp,
                sec[0][0],
                sec[0][1],
                negative=is_neg,
                stat_multipliers=stat_multipliers,
            )
            if lo is None:
                continue
            # 未确认的物理词条：名字照普通色，只把「?」压暗（⟦n⟧ = INK_DIM）
            zh = _stat_name(sid, cls) + ("⟦n⟧?⟦/n⟧" if sid in unconfirmed else "")
            rows.append([zh] + [_cell(sid, c, negative=is_neg) for c in sec])
        if not rows:
            continue
        # 名字列配色：普通词条 INK（w）、融合词条 VIOLET（v）—— 渲染层据此包显式染段
        _name_span = "v" if sids is spliced else "w"
        payload = {
            "cols": [_combo_label(c) for c in sec],
            "spans": [_RIVEN_COMBO_SPAN.get(c, "") for c in sec],
            "name_span": _name_span,
            "primary": sec.index(combo) if combo in sec else 0,
            "rows": rows,
            "text_hint": (
                L(f"※ 其余组合：词条范围 {weapon} ", f"※ Other combos: rivenranges {weapon} ")
                + " / ".join(v for k, v in _RIVEN_COMBO_HINT.items() if k != combo)
            ),
        }
        _title_txt = f"⟦v⟧{label}⟦/v⟧" if _name_span == "v" else label
        lines.append(
            L(
                f"◆ {_title_txt}（{len(rows)} 条）",
                f"◆ {_title_txt} ({len(rows)} stat{'' if len(rows) == 1 else 's'})",
            )
        )
        lines.append("§RANGES" + json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    if not lines:
        lines.append(
            L(
                "◆ 原因：该武器在这个组合下列不出词条（职业池为空？）",
                "◆ Reason: no stats can be listed for this weapon under this combo (empty class pool?)",
            )
        )

    if only:
        lines.append(
            L(
                f"※ 已按你点名的 {len(only)} 条词条过滤",
                f"※ Filtered to the {len(only)} stat(s) you named",
            )
        )
    if dropped:
        lines.append(
            L(
                f"※ ⚠ 这些词条该武器刷不出，已略去：{'、'.join(dropped)}",
                f"※ ⚠ This weapon cannot roll these stats, omitted: {', '.join(dropped)}",
            )
        )
    if spliced:
        lines.append(
            L(
                "※ 融合词条同一张卡最多 1 项（Update 44 Riven Splicer）",
                "※ A riven can have at most 1 spliced stat (Update 44 Riven Splicer)",
            )
        )
    _shown = set(pos) | set(spliced) | set(neg)
    _unseen = set((survey or {}).get("unseen") or ()) & _shown
    if (unconfirmed - _unseen) & _shown:
        lines.append(
            L(
                "※ 带 ? 的物理伤害词条：这把武器（Zaw / 组合枪等）不能按「占比 ≤25% 洗不出」判断，按「未确认」照列",
                '※ Physical damage stats marked ?: the "25% share" rule does not apply to this weapon '
                '(Zaw / Kitgun etc.); listed as "unconfirmed"',
            )
        )
    if _unseen:
        # ★ 2026-10-25（Astra 评估）：抽样没见过 ≠ 刷不出 —— 标 ? 照列并写明样本时间
        _names = " / ".join(_stat_name(x, cls) for x in sorted(_unseen))
        lines.append(
            L(
                f"※ 带 ? 的 {_names}：warframe.market 挂单抽样（{survey.get('surveyed') or '时间未知'}，"
                f"{survey.get('n') or '?'} 条）里没见过，可能只是样本里没有或样本早于热修，按「未确认」照列",
                f"※ Stats marked ? ({_names}): not seen in the warframe.market listing sample "
                f"({survey.get('surveyed') or 'time unknown'}, {survey.get('n') or '?'} listings); "
                'the sample may simply lack them or predate a hotfix; listed as "unconfirmed"',
            )
        )
    # 图例（一行，替代原来的「切换组合」长句）：四种组合都在表里，不必再点名；
    # 每个组合名染各自组合色（与表头/数据格同一套字母）。
    lines.append(
        L("※ 列：", "※ Columns: ")
        + " / ".join(
            f"⟦{_RIVEN_COMBO_SPAN[c]}⟧{_combo_label(c)}⟦/{_RIVEN_COMBO_SPAN[c]}⟧" for c in cols
        )
        + L(
            f"（满级卡面，统一倾向 {disp}，随机系数 ±10%）",
            f" (max-rank card, unified disposition {disp}, random factor ±10%)",
        )
    )
    return (
        L(
            f"{weapon} 紫卡词条范围（{_RIVEN_COMBO_ZH.get(combo, '')}）",
            f"{weapon} Riven Stat Ranges ({_combo_label(combo)})",
        ),
        lines,
    )


def fmt_riven_analysis(
    name: str,
    disposition: float,
    cls: str,
    stats_pos: list,
    stats_neg: list,
    *,
    verdict: str = "",
    warns: Optional[list] = None,
    infos: Optional[list] = None,
    counts: Optional[tuple] = None,
    hidden: int = 0,
    splice: Optional[dict] = None,
    price: Optional[dict] = None,
    splice_limit: Optional[int] = None,
    stat_multipliers: Optional[dict] = None,
) -> tuple[str, list[str]]:
    """紫卡分析：逐词条给出 DE 机制下的取值区间与卷度。

    区间 = wiki「Riven Mods」页公式：基值 × 倾向 × 词条数系数 × 随机 ±10%。
    距中 = (数值-区间中值)/中值，正为高卷、负为低卷；负词条按幅度比较
    （幅度越接近上限=负得越多=对武器越友好）。

    排版（2026-10-11 卡片改版）：卡首 KV「武器 / 判定」→ ⚠ 警示（需要用户行动的判定）→
    「词条｜区间｜距中｜区间位」表（▲ 正词条青绿 / ▼ 负词条红）→ 注脚 ≤3 行。
    ``verdict`` 是短结论（空 = 数值与本体倾向无冲突，不写「判定」行）；``warns`` 进 ⚠ 警示；
    ``infos`` 合并成一条注脚。
    ``counts`` = (正, 负) 词条数（决定系数；None = 按 stats 条数）；``hidden`` = 融合未选定的
    「？？？」词条数（2026-10-24）：只计入词条数、单列一行不给区间。
    ``splice`` = 后端 ``Reply.riven_splice``（融合建议，前端只排版不重算）；``price`` = 同词条
    一口价参考（``None`` = 未查询，整段不出）。两段都排在词条表之后、注脚之前。
    ``splice_limit`` = 融合建议本页最多列几条（``None`` = 全列）；超出部分由
    ``fmt_riven_splice_more`` 出续页，本页注脚写明「其余 N 条见续页」。
    """
    from . import riven_analysis as RA

    class _Zh:
        """词条名查表（中文 RIVEN_STAT_ZH / 英文游戏内名），保持 zh.get(sid, sid) 的写法。"""

        @staticmethod
        def get(sid, _default=None):
            return _stat_name(sid, cls)

    zh = _Zh
    if stat_multipliers:
        infos = list(infos or []) + [
            L("武器机制：", "Weapon mechanics: ")
            + L("、", ", ").join(
                f"{zh.get(sid, sid)} ×{factor:g}" for sid, factor in stat_multipliers.items()
            )
            + L(
                "；区间按卡面倍率计算，原读数保留",
                "; ranges use the card multipliers, raw readings kept",
            )
        ]
    # 近战没有「射速」：合并词条 fire_rate 在近战上就是攻速（与拍卖卡同一套口径）
    _melee = cls == "melee"

    def _sid(s: str) -> str:
        return "attack_speed" if (_melee and s == "fire_rate") else s

    lines = [
        L(
            f"◆ 武器：{name}｜倾向 {disposition:g}（{_disp_dots(disposition)}）",
            f"◆ Weapon: {name}｜Disposition {disposition:g} ({_disp_dots(disposition)})",
        )
    ]
    if verdict:
        lines.append(L(f"◆ 判定：{verdict}", f"◆ Verdict: {verdict}"))
    for w in warns or []:
        lines.append("※ ⚠ " + str(w).lstrip("⚠\ufe0f "))
    n_pos, n_neg = counts or (len(stats_pos), len(stats_neg))
    if i18n.is_en():
        lines.append(
            (f"◆ Stats ({n_pos} pos {n_neg} neg" if n_neg else f"◆ Stats ({n_pos} pos")
            + (f", {hidden} unrevealed)" if hidden else ")")
        )
    else:
        lines.append(
            (f"◆ 词条（{n_pos} 正 {n_neg} 负" if n_neg else f"◆ 词条（{n_pos} 正")
            + (f"，{hidden} 条未揭示）" if hidden else "）")
        )
    lines.append(L("　词条　区间　距中　区间位", "　Stat　Range　Vs Mid　Position"))
    for sid0, v in stats_pos:
        sid = _sid(sid0)
        lo, hi = RA.stat_range(
            sid, cls, disposition, n_pos, n_neg, stat_multipliers=stat_multipliers
        )
        # ★ 2026-10-02：反转词条（后坐力）**保留卡面符号**显示 —— 卡面
        #   「-20% 后坐力」是正面效果，就写「-20% 后坐（卡面-号·正面）」；
        #   其余正词条照旧 +（用户口径：显示符号必须与卡面一致，极性用标注传达）。
        _inv = RA.is_inverted(sid)
        _head = (
            f"▲{zh.get(sid, sid)} -{RA.fmt_value(sid, v)}"
            + L("（卡面-号·正面）", " (card shows -; positive)")
            if _inv
            else f"▲{zh.get(sid, sid)} +{RA.fmt_value(sid, v)}"
        )
        if lo is None:
            lines.append(
                L(
                    f"· {_head}　该词条无官方基值（wiki 未收录），无法算区间",
                    f"· {_head}　No official base value (not on the wiki); range unavailable",
                )
            )
            continue
        dev = RA.deviation_pct(v, lo, hi)
        pos_pct = RA.range_position(v, lo, hi)
        lines.append(
            "· " + "　".join([_head, _range_txt(RA, sid, lo, hi), f"{dev:+.1f}%", f"{pos_pct}%"])
        )
    for sid0, v in stats_neg:
        sid = _sid(sid0)
        lo, hi = RA.stat_range(
            sid, cls, disposition, n_pos, n_neg, negative=True, stat_multipliers=stat_multipliers
        )
        # ★ 2026-10-02：反转词条在负词组里同样**保留卡面符号** —— 卡面
        #   「+95.4% 武器后坐力」是负面（增加后坐力），写「+95.4% 后坐
        #   （卡面+号·负面）」；区间仍按**负面档**系数计算（分组不变）。
        _inv = RA.is_inverted(sid)
        _head = (
            f"▼{zh.get(sid, sid)} +{RA.fmt_value(sid, v)}"
            + L("（卡面+号·负面）", " (card shows +; negative)")
            if _inv
            else f"▼{zh.get(sid, sid)} -{RA.fmt_value(sid, v)}"
        )
        if lo is None:
            lines.append(
                L(
                    f"· {_head}　该词条无官方基值（wiki 未收录），无法算区间",
                    f"· {_head}　No official base value (not on the wiki); range unavailable",
                )
            )
            continue
        pos_pct = RA.range_position(v, lo, hi)
        # ★ 2026-09-24 玩家报障（红框）：负词条这里原写 100-pos_pct，方向反了——
        #   数值贴近下限（负得很浅）反而显示「幅度位 87%」（=负得很满），
        #   把浅负当深负卖。区间位本身就是「幅度接近上限的程度」，
        #   直接用 pos_pct：0%=最浅、100%=最满。
        lines.append(
            "· "
            + "　".join(
                [_head, _range_txt(RA, sid, lo, hi), L("幅度位", "Magnitude"), f"{pos_pct}%"]
            )
        )
    for _ in range(hidden):
        lines.append(
            L(
                "· ？？？　未揭示（融合选定后再分析）　—　—",
                "· ???　Unrevealed (analyzed after splicing)　—　—",
            )
        )
    notes = []
    if splice:
        lines += _riven_splice_lines(RA, splice, cls, notes, limit=splice_limit)
    if price is not None:
        lines += _riven_price_lines(price, cls, notes)
    if infos:
        lines.append(
            "※ "
            + L("；", "; ").join(
                str(x)[1:-1] if str(x).startswith("（") and str(x).endswith("）") else str(x)
                for x in infos
            )
        )
    if hidden:
        lines.append(
            L(
                "※ 「？？？」词条正负未知，只计入词条数：词条数决定系数，已知词条的区间照常给出",
                '※ "???" stats have unknown polarity and only count toward the stat total: '
                "the count sets the multiplier; ranges for known stats are unchanged",
            )
        )
    # 注脚压到每类一行（手机上整段注释曾占 5–6 行）；详细机制在「紫卡融合」卡与帮助里
    lines.append(
        L(
            "※ 距中为正 = 高卷；负词条看幅度位，幅度越大越友好；"
            "区间 = DE 基值 × 倾向 × 词条数系数 × 0.9~1.1",
            "※ Positive Vs Mid = high roll; for negative stats look at Magnitude (bigger is friendlier); "
            "Range = DE base × disposition × stat-count factor × 0.9~1.1",
        )
    )
    lines += notes
    return (L("紫卡分析", "Riven Analysis"), lines)


# 融合建议阻断原因 → 卡面短句（键与 RA.splice_preview 的 reasons 一致）
_SPLICE_REASON_ZH = {
    "hidden_stats": "有未揭示的「？？？」词条",
    "incomplete_stats": "词条没读全或两路识别不一致",
    "legacy_values": "疑似倾向调整前的旧截图",
    "non_current_disposition": "手输倾向不是当前倾向",
    "unknown_weapon_class": "武器类别未确定",
    "unverified_values": "数值与当前区间对不上",
    "unverified_weapon_traits": "这把武器的词条可用性未确认",
    "duplicate_stat": "有重复词条",
    "unknown_stat": "有认不出的词条",
    "out_of_range": "数值超出区间",
    "invalid_value": "数值不合法",
    "stat_not_allowed": "词条与武器类别或正负不符",
    "counts_mismatch": "词条数与正负布局不符",
    "invalid_counts": "正负条数不合法",
    "pool_unavailable": "缺少词条池数据",
    "missing_base": "缺少词条基值",
    "unsupported_class": "该武器类别暂不支持",
    "no_matching_recipe": "现有词条凑不成任何配方",
    "invalid_stats": "词条结构不合法",
    "invalid_disposition": "倾向参数不合法",
}
_SPLICE_REASON_EN = {
    "hidden_stats": 'has unrevealed "???" stats',
    "incomplete_stats": "stats not fully read or the two readers disagree",
    "legacy_values": "looks like a screenshot from before the disposition change",
    "non_current_disposition": "the typed disposition is not the current one",
    "unknown_weapon_class": "weapon class not determined",
    "unverified_values": "values do not match the current ranges",
    "unverified_weapon_traits": "stat availability for this weapon is unconfirmed",
    "duplicate_stat": "has duplicate stats",
    "unknown_stat": "has unrecognized stats",
    "out_of_range": "value out of range",
    "invalid_value": "invalid value",
    "stat_not_allowed": "stat does not fit the weapon class or polarity",
    "counts_mismatch": "stat count does not fit the positive/negative layout",
    "invalid_counts": "invalid positive/negative counts",
    "pool_unavailable": "stat pool data missing",
    "missing_base": "stat base value missing",
    "unsupported_class": "this weapon class is not supported yet",
    "no_matching_recipe": "current stats form no recipe",
    "invalid_stats": "invalid stat structure",
    "invalid_disposition": "invalid disposition parameter",
}
# 融合建议首屏最多列几条（直接可融合在前）；其余进续页，不用通用配方表代替当前卡的预测
SPLICE_FIRST_ROWS = 6


def _riven_short(sid: str, cls: str) -> str:
    from .parser import RIVEN_STAT_ZH

    if cls == "melee" and sid == "fire_rate":
        sid = "attack_speed"
    if i18n.is_en():
        return _riven_stat_en(sid, sid)
    return RIVEN_STAT_ZH.get(sid, sid)


def _splice_value(RA, sid: str, v: float) -> str:
    """融合结果数值：派系是增伤百分数，按净倍率显示（与词条区间同一口径）。"""
    if sid.startswith("damage_vs_"):
        return f"×{1 + v / 100:.2f}"
    return "+" + RA.fmt_value(sid, round(v + 1e-9, 1))  # 单位 / 数字与语言无关


def _splice_num(v: float) -> str:
    """距中数字：1 位小数，整数不写「.0」（后端 5 ⇒「+5%」），−0 归 0。"""
    v = round(v, 1) + 0.0
    return f"{v:.1f}".rstrip("0").rstrip(".") if v % 1 else f"{v:.0f}"


def _splice_dev(v: float) -> str:
    return ("+" if round(v, 1) >= 0 else "") + _splice_num(v)


def _splice_rows(RA, splice: dict, cls: str) -> list[tuple[str, str]]:
    """当前卡融合预测 → [(表格行, 消耗续行)]；**每种融合结果只留数值最高的方案**。

    四列与上方词条表同构：融合词条 + 数值｜理论区间｜距中｜区间位。
    · 直接融合：「约 +101.2%」，距中 / 区间位读后端点估计 ``deviation_pct`` / ``position_pct``；
    · 差一条：「+253.1%起」，距中 / 区间位写后端给的条件上下界（点估计是 null，不当 0）；
    · 续行「消耗 ▲基伤、缺多重（正负皆可）」——单独一行，手机上不挤成第五列。
    距中 +5% 与区间位 75% 是两个量（同上方词条表），都只读后端字段，前端不重算。

    去重（同一 ``output``）：比后端原始数值——直接融合用 ``value``，差一条用「起」对应的
    ``value_min``（不用理论上限 ``value_max``）。最高值并列时优先直接融合；同类并列且四列显示
    完全相同则把消耗用「或」合并，否则只留第一条。所选行的数值 / 距中 / 区间位 / 缺项都取自
    同一个方案。顺序：直接融合在前，再按「起」值降序。
    """
    cands = []
    for i, r in enumerate(list(splice.get("direct") or []) + list(splice.get("missing_one") or [])):
        pred = r.get("prediction") or {}
        out = r.get("output", "")
        conditional = bool(r.get("missing")) or pred.get("value") is None
        score = pred.get("value_min") if conditional else pred.get("value")
        cands.append(
            dict(
                out=out,
                conditional=conditional,
                score=float("-inf") if score is None else score,
                cells=_splice_cells(RA, r, pred, out, cls, conditional),
                present=L("、", ", ").join(
                    ("▼" if p.get("negative") else "▲") + _riven_short(p.get("stat_id", ""), cls)
                    for p in r.get("present") or []
                ),
                lack=L("、", ", ").join(_riven_short(x, cls) for x in r.get("missing") or []),
                pol=_splice_pol(r.get("missing_polarities")) if r.get("missing") else "",
                order=i,
            )
        )
    best: dict[str, list[dict]] = {}
    for c in cands:
        group = best.setdefault(c["out"], [])
        if not group:
            group.append(c)
            continue
        top = group[0]
        rank = (c["score"], not c["conditional"])
        top_rank = (top["score"], not top["conditional"])
        if abs(c["score"] - top["score"]) < 1e-9:
            rank, top_rank = (0, rank[1]), (0, top_rank[1])
        if rank > top_rank:
            best[c["out"]] = [c]
        elif rank == top_rank and c["cells"] == top["cells"]:
            group.append(c)  # 同值同显示：消耗合并成「或」
    picked = sorted(
        best.values(), key=lambda g: (g[0]["conditional"], -g[0]["score"], g[0]["order"])
    )
    rows = []
    for group in picked:
        rows.append(
            ("· " + "　".join(group[0]["cells"]), L("　消耗 ", "　Uses ") + _splice_used(group))
        )
    return rows


def _splice_pol(pols) -> str:
    pols = set(pols or [])
    if pols >= {"positive", "negative"}:
        return L("正负皆可", "either polarity")
    return L("需负面", "needs negative") if "negative" in pols else L("需正面", "needs positive")


def _splice_used(group: list[dict]) -> str:
    """消耗续行；并列方案用「或」：保留同一条、缺项正负说明相同时只合并缺项（缺变焦或多重）。"""

    def one(c: dict, lack: str) -> str:
        if not lack:
            return c["present"]
        return (c["present"] + L("、", ", ") if c["present"] else "") + L(
            f"缺{lack}（{c['pol']}）", f"Missing {lack} ({c['pol']})"
        )

    head = group[0]
    if len(group) > 1 and all(
        c["lack"] and c["present"] == head["present"] and c["pol"] == head["pol"] for c in group
    ):
        return one(head, L("或", " or ").join(dict.fromkeys(c["lack"] for c in group)))
    return L(" 或 ", " or ").join(dict.fromkeys(one(c, c["lack"]) for c in group))


def _splice_cells(RA, r: dict, pred: dict, out: str, cls: str, conditional: bool) -> list[str]:
    """一条方案的四列显示（融合词条 + 数值｜理论区间｜距中｜区间位）。"""
    rng = _riven_range_text(
        RA, out, pred.get("range_min", 0), pred.get("range_max", 0), negative=False
    )
    # 融合词条用卡面全称（短名「异常伤」「弱点暴」不好认；消耗沿用上方词条表的短名）
    out_zh = (
        _splice_damage_name("melee" if cls == "melee" else "gun")
        if out == "melee_damage"
        else _splice_full(out) or _riven_short(out, cls)
    )
    head = f"⟦v⟧{out_zh}⟦/v⟧ "
    # 点估计（value / deviation_pct / position_pct）只有直接融合才有；缺字段写「—」，不当 0
    if conditional:
        v_lo = pred.get("value_min")
        head += (
            "—"
            if v_lo is None
            else L(_splice_value(RA, out, v_lo) + "起", "from " + _splice_value(RA, out, v_lo))
        )
        d_lo, d_hi = pred.get("deviation_min_pct"), pred.get("deviation_max_pct")
        p_lo, p_hi = pred.get("position_min_pct"), pred.get("position_max_pct")
        if d_lo is None or d_hi is None:
            dev = "—"
        elif round(d_lo, 1) >= 0:
            # 同号只在前面写一次（同理论区间「+91.1~111.4%」）；下界为负时两头都带号
            dev = f"+{_splice_num(d_lo)}~{_splice_num(d_hi)}%"
        else:
            dev = f"{_splice_dev(d_lo)}~{_splice_dev(d_hi)}%"
        pos = "—" if p_lo is None or p_hi is None else f"{round(p_lo)}~{round(p_hi)}%"
    else:
        head += L("约", "~") + _splice_value(RA, out, pred["value"])
        d, pp = pred.get("deviation_pct"), pred.get("position_pct")
        dev = "—" if d is None else _splice_dev(d) + "%"
        pos = "—" if pp is None else f"{round(pp)}%"  # 与上方词条表同为整数
    return [head, rng, dev, pos]


_SPLICE_HEADER = "　融合词条　理论区间　距中　区间位"
_SPLICE_HEADER_EN = "　Spliced Stat　Range　Vs Mid　Position"
_SPLICE_NOTE = (
    "※ 融合建议按英文 wiki 公式预计：继承被消耗两条中较高的随机值；"
    "「起」= 保留现有词条、洗出缺项后再融合"
)
_SPLICE_NOTE_EN = (
    "※ Splice suggestions are projected with the English wiki formula: the result inherits the "
    'higher random value of the two consumed stats; "from" = keep the current stat, reroll the '
    "missing one, then splice"
)


def _riven_splice_lines(
    RA, splice: dict, cls: str, notes: list, *, limit: Optional[int] = None
) -> list[str]:
    """「◆ 融合建议」段：消费 ``RA.splice_preview`` 的结果，不重算任何数值。

    ``limit`` = 本页最多列几条，其余由 ``fmt_riven_splice_more`` 出续页。
    阻断状态只给一句原因，不列配方。
    """
    status = splice.get("status")
    if status == "already_spliced":
        names = L("、", ", ").join(
            _riven_short(s, cls) for s in splice.get("existing_spliced") or []
        )
        return [
            L(
                f"◆ 融合建议：已有融合词条{f'（{names}）' if names else ''}，不能再融合",
                f"◆ Splice Suggestions: already has a spliced stat"
                f"{f' ({names})' if names else ''}; cannot splice again",
            )
        ]
    if status != "ready":
        _rmap = _SPLICE_REASON_EN if i18n.is_en() else _SPLICE_REASON_ZH
        why = [_rmap.get(r, "") for r in splice.get("reasons") or []]
        why = L("、", ", ").join(w for w in why if w) or L("条件不足", "insufficient conditions")
        return [L(f"◆ 融合建议：暂不判断（{why}）", f"◆ Splice Suggestions: not assessed ({why})")]
    rows = _splice_rows(RA, splice, cls)
    if not rows:
        return [
            L(
                "◆ 融合建议：现有词条凑不成任何配方",
                "◆ Splice Suggestions: current stats form no recipe",
            )
        ]
    shown = rows if limit is None else rows[:limit]
    title = (
        L("◆ 融合建议（预计）", "◆ Splice Suggestions (projected)")
        if len(shown) == len(rows)
        else L(
            f"◆ 融合建议（预计，共 {len(rows)} 条）",
            f"◆ Splice Suggestions (projected, {len(rows)} total)",
        )
    )
    out_lines = [title, L(_SPLICE_HEADER, _SPLICE_HEADER_EN)]
    for row, used in shown:
        out_lines += [row, used]
    if len(shown) < len(rows):
        notes.append(
            L(
                f"※ 融合建议其余 {len(rows) - len(shown)} 条见续页",
                f"※ {len(rows) - len(shown)} more splice suggestions on the next page",
            )
        )
    notes.append(L(_SPLICE_NOTE, _SPLICE_NOTE_EN))
    return out_lines


def fmt_riven_splice_more(
    name: str, cls: str, splice: Optional[dict], *, skip: int = SPLICE_FIRST_ROWS
) -> Optional[tuple[str, list[str]]]:
    """融合建议续页：首屏之后的**当前卡**剩余预测（同一组数值、缺项与条件上下界）。

    没有剩余（或不是 ready）时返回 ``None``。续页单独带武器行，转发 / 单看时也知道是哪张卡。
    """
    from . import riven_analysis as RA

    if not splice or splice.get("status") != "ready":
        return None
    rows = _splice_rows(RA, splice, cls)
    if len(rows) <= skip:
        return None
    lines = [
        L(f"◆ 武器：{name}", f"◆ Weapon: {name}"),
        L(
            f"◆ 融合建议（预计，第 {skip + 1}–{len(rows)} 条，共 {len(rows)} 条）",
            f"◆ Splice Suggestions (projected, {skip + 1}–{len(rows)} of {len(rows)})",
        ),
        L(_SPLICE_HEADER, _SPLICE_HEADER_EN),
    ]
    for row, used in rows[skip:]:
        lines += [row, used]
    lines.append(L(_SPLICE_NOTE, _SPLICE_NOTE_EN))
    return (L("紫卡分析 · 融合建议续", "Riven Analysis · Splice Suggestions (cont.)"), lines)


def _riven_price_lines(price: dict, cls: str, notes: list) -> list[str]:
    """「◆ 同词条挂单」段：warframe.market 当前一口价（非成交价）。

    ``price`` 键：low / online_low / median / sample_count（同 ``riven_buyout_summary``）、
    pos / neg（完整匹配词条 id）、match_mode（正负名称与条数全同）、error（查询失败）。
    """
    cond = " ".join(
        [f"▲{_riven_short(s, cls)}" for s in price.get("pos") or []]
        + [f"▼{_riven_short(s, cls)}" for s in price.get("neg") or []]
    )

    if price.get("error"):
        return [
            L(
                "◆ 同词条挂单：warframe.market 暂时查不到，稍后重试",
                "◆ Same-Stat Listings: warframe.market is unreachable right now, try again later",
            ),
            L(f"· 匹配词条：{cond}", f"· Matched stats: {cond}"),
        ]

    def _p(v):
        return f"{v:g}p" if v is not None else "—"

    out = [
        L("◆ 同词条挂单（一口价）", "◆ Same-Stat Listings (buyout)"),
        L("　匹配词条　最低　在线最低　中位", "　Matched Stats　Lowest　Lowest Online　Median"),
    ]
    n = price.get("sample_count") or 0
    if n:
        out.append(
            f"· {cond}　{_p(price.get('low'))}　{_p(price.get('online_low'))}"
            f"　{_p(price.get('median'))}" + L(f"（{n} 条）", f" ({n} listings)")
        )
    else:
        out.append(
            L(
                f"· {cond}　本次未找到完全同词条样本　—　—",
                f"· {cond}　No exact same-stat samples found this time　—　—",
            )
        )
    notes.append(
        L(
            "※ 同词条挂单：正负词条名称与条数全同（不比数值、洗数）；"
            "warframe.market 一口价，非成交价；中位取低价样本（最多 20 条）",
            "※ Same-stat listings: positive/negative stat names and counts match exactly "
            "(values and rerolls not compared); warframe.market buyout prices, not trade prices; "
            "the median uses the low-price samples (up to 20)",
        )
    )
    return out


_SPLICE_CLASS_ZH = {
    "rifle": "步枪",
    "shotgun": "霰弹枪",
    "pistol": "手枪",
    "archgun": "空战",
    "melee": "近战",
}


# 「紫卡融合」卡用**卡面全称**（官方国际服简中模板，取自游戏数据包 Upgrade 模板；2026-10-25
# 用户：短名「基伤」把枪的「伤害」与近战的「近战伤害」混成一个，看着像漏了「近战伤害」）。
_SPLICE_FULL_ZH = {
    "status_chance": "触发几率",
    "crit_chance": "暴击几率",
    "zoom": "变焦",
    "multishot": "多重射击",
    "magazine_capacity": "弹匣容量",
    "reload_speed": "装填速度",
    "recoil": "武器后坐力",
    "ammo_max": "弹药最大值",
    "toxin_damage": "毒素伤害",
    "heat_damage": "火焰伤害",
    "cold_damage": "冰冻伤害",
    "electric_damage": "电击伤害",
    "damage_vs_grineer": "对 Grineer 的伤害",
    "damage_vs_corpus": "对 Corpus 的伤害",
    "damage_vs_infested": "对 Infested 的伤害",
    "heavy_attack_efficiency": "重击效率",
    "extra_combo_count": "额外连击数几率",
    "combo_duration": "连击持续时间",
    "attack_speed": "攻击速度",
    "range": "攻击范围",
    "gas_damage": "毒气伤害",
    "corrosive_damage": "腐蚀伤害",
    "viral_damage": "病毒伤害",
    "radiation_damage": "辐射伤害",
    "blast_damage": "爆炸伤害",
    "magnetic_damage": "磁力伤害",
    "damage_vs_orokin": "对奥罗金的伤害",
    "damage_vs_scaldra": "对炽蛇军的伤害",
    "damage_vs_techrot": "对科腐者的伤害",
    "status_damage": "异常状态伤害",
    "ammo_efficiency": "弹药效率",
    "reload_holstered": "收起武器时弹匣每秒自动装填",
    "weakpoint_crit_chance": "弱点暴击几率",
    "weakpoint_damage": "弱点伤害",
    "melee_heavy_attack_damage": "在重击时的近战伤害",
    "melee_heavy_attack_charge": "重击准备速度",
    "melee_parry_angle": "招架角度",
    "melee_slam_damage": "震地攻击伤害",
}
# 「伤害」是同一个词条 id：枪械卡面写「伤害」、近战写「近战伤害」
_SPLICE_DAMAGE_ZH = {"gun": "伤害", "melee": "近战伤害", "any": "伤害 / 近战伤害"}
_SPLICE_DAMAGE_EN = {"gun": "Damage", "melee": "Melee Damage", "any": "Damage / Melee Damage"}


def _splice_damage_name(kind: str) -> str:
    return (_SPLICE_DAMAGE_EN if i18n.is_en() else _SPLICE_DAMAGE_ZH)[kind]


def _splice_full(sid: str) -> str:
    """融合词条的卡面全称：中文取 _SPLICE_FULL_ZH（没有返回空串），英文取游戏内英文名。"""
    if i18n.is_en():
        return _riven_stat_en(sid, sid)
    return _SPLICE_FULL_ZH.get(sid, "")


def fmt_riven_splice(
    recipes: list, *, scope: str = "", stat_zh: str = "", cls: Optional[str] = None
) -> tuple[str, list[str]]:
    """「紫卡融合 / 紫卡拼接」：Riven Splicer 每个融合词条由哪两条原词条拼成（2026-10-24）。

    三段：通用（所有武器）/ 枪械 / 近战；表三列「融合词条　原词条一　原词条二」。融合词条名
    染紫 ``⟦v⟧``（与「词条范围」融合段同色），原词条名包 ``⟦w⟧``（绕开元素语义词表单独染色）。
    ``scope`` = 已按武器 / 武器类过滤时的说明；``stat_zh`` = 已按词条过滤时的词条名；
    ``cls`` = 已按武器类过滤时的类别（决定通用段「伤害」写成「伤害」还是「近战伤害」）。
    """
    from .parser import RIVEN_STAT_ZH

    def zh(sid: str, kind: str) -> str:
        if sid == "melee_damage":
            if cls:
                kind = "melee" if cls == "melee" else "gun"
            return _splice_damage_name(kind)
        return _splice_full(sid) or RIVEN_STAT_ZH.get(sid, sid)

    groups = (
        (
            L("通用 · 所有武器", "Universal · All Weapons"),
            "any",
            lambda r: "melee" in r[3] and "rifle" in r[3],
        ),
        (
            L("枪械 · 步枪 / 霰弹枪 / 手枪 / 空战", "Guns · Rifle / Shotgun / Pistol / Archgun"),
            "gun",
            lambda r: "melee" not in r[3],
        ),
        (L("近战", "Melee"), "melee", lambda r: r[3] == ("melee",)),
    )
    lines: list[str] = []
    if scope:
        lines.append(L(f"◆ 适用：{scope}", f"◆ Applies to: {scope}"))
    for label, kind, pick in groups:
        rows = [r for r in recipes if pick(r)]
        if not rows:
            continue
        lines.append(
            L(
                f"◆ {label}（{len({r[2] for r in rows})} 个）",
                f"◆ {label} ({len({r[2] for r in rows})})",
            )
        )
        lines.append(L("　融合词条　原词条一　原词条二", "　Spliced Stat　Stat 1　Stat 2"))
        for a, b, out, _cls in rows:
            lines.append(f"· ⟦v⟧{zh(out, kind)}⟦/v⟧　⟦w⟧{zh(a, kind)}⟦/w⟧　⟦w⟧{zh(b, kind)}⟦/w⟧")
    if not lines or (scope and len(lines) == 1):
        lines.append(
            L(
                "◆ 原因：没有符合条件的融合配方" + (f"（{stat_zh}）" if stat_zh else ""),
                "◆ Reason: no matching splice recipes" + (f" ({stat_zh})" if stat_zh else ""),
            )
        )
    if any(r[2] == "reload_holstered" for r in recipes):
        lines.append(
            L(
                "※ 收起武器时弹匣每秒自动装填：空战武器不可融合",
                "※ Magazine Reloaded/s when Holstered: Archgun weapons cannot splice it",
            )
        )
    lines.append(
        L(
            "※ 两条原词条被消耗；融合词条总是正面，若消耗的原词条里有负面，补上的随机词条也是负面",
            "※ Both source stats are consumed; the spliced stat is always positive, and if a consumed "
            "stat was negative, the random stat added in its place is negative too",
        )
    )
    lines.append(
        L(
            "※ 融合词条继承两条原词条中较高的随机值；一张卡只能有 1 条融合词条，锁定、不随洗卡变",
            "※ The spliced stat inherits the higher random value of the two sources; a card can have "
            "only 1 spliced stat, and it is locked and does not change on reroll",
        )
    )
    lines.append(
        L(
            "※ 来源：英文 wiki「Riven Mods · Splicing」（融合器 Riven Splicer）",
            '※ Source: English wiki "Riven Mods · Splicing" (Riven Splicer)',
        )
    )
    title = L(
        "紫卡融合配方" + (f"：{stat_zh}" if stat_zh else ""),
        "Riven Splicing Recipes" + (f": {stat_zh}" if stat_zh else ""),
    )
    return (title, lines)


def fmt_trend(name: str, stats: dict, summary: dict) -> tuple[str, list[str]]:
    """单个物品的价格趋势（v1 统计 48h + 90d）。"""
    h48 = stats.get("h48") or []
    d90 = stats.get("d90") or []
    title = L(f"{name} 价格趋势", f"{name} Price Trend")
    if not h48 and not d90:
        return (
            title,
            [
                L(
                    "◆ 原因：warframe.market 未返回该物品的成交统计",
                    "◆ Reason: warframe.market returned no trade stats for this item",
                ),
                L(
                    "※ ⚠ 怎么改：该物品可能近期无成交；换个写法重发，或改查「wm 物品名」看挂单",
                    "※ ⚠ Fix: the item may have no recent trades; resend with another spelling, "
                    'or try "wm <item>" to see listings',
                ),
            ],
        )
    lines = []
    s = summary
    lines.append(
        L(
            f"◆ 近 48 小时：{s['vol48']} 笔成交｜中位 {s['median48']}p",
            f"◆ Last 48h: {s['vol48']} trades｜Median {s['median48']}p",
        )
        + (
            L(f"｜90 天均价 {s['avg90']}p", f"｜90d average {s['avg90']}p")
            if s.get("avg90")
            else ""
        )
    )
    if s.get("change"):
        arrow = "▲" if s["change"] > 0 else "▼"
        _span = s.get("base_span") or L("区间", "range")
        if i18n.is_en():
            _span = {"90 天": "90d", "48 小时": "48h", "区间": "range"}.get(_span, _span)
            lines.append(
                f"◆ Trend: {arrow}{abs(s['change'])}% (latest {s['last']}p vs "
                + (
                    f"early {_span} average {s['base']}p)"
                    if s.get("base")
                    else f"range start {s['first']}p)"
                )
            )
        else:
            lines.append(
                f"◆ 走势：{arrow}{abs(s['change'])}%（最新 {s['last']}p vs "
                + (
                    f"{_span}前段均价 {s['base']}p）"
                    if s.get("base")
                    else f"区间初 {s['first']}p）"
                )
            )
    # 走势图：交给渲染层画真实折线图（§CHART 机器行）；文本模式自动回退方块字 +
    # 「最高/最低/最新」一行。有效点 < 2 的序列不出图 —— 出一个点的「图」没有意义，
    # 而且会让「◆ 走势图」分区标题下面空着。
    charts = []
    if sum(1 for r in h48 if r["median"]) >= 2:
        charts.append(_chart_line(L("48h 中位走势", "48h Median Trend"), h48, hourly=True))
    if sum(1 for r in d90 if r["median"]) >= 2:
        charts.append(_chart_line(L("90d 中位走势", "90d Median Trend"), d90, hourly=False))
    if charts:
        lines.append(L("◆ 走势图", "◆ Trend Chart"))
        lines += charts
    rows = []
    best = [r for r in (d90 or h48) if r["volume"]]
    if best:
        peak = max(best, key=lambda r: r["volume"])
        rows.append(
            L(
                f"· 峰值成交　{_short_t(peak['t'], not d90)}｜{peak['volume']} 笔｜中位 {peak['median']}p",
                f"· Peak Volume　{_short_t(peak['t'], not d90)}｜{peak['volume']} trades｜"
                f"Median {peak['median']}p",
            )
        )
    lo = min((r for r in h48 if r["min"]), key=lambda r: r["min"], default=None)
    if lo:
        rows.append(
            L(
                f"· 48h 最低成交　{lo['min']}p（{_short_t(lo['t'], True)}）",
                f"· 48h Lowest Trade　{lo['min']}p ({_short_t(lo['t'], True)})",
            )
        )
    if rows:
        lines.append(L("◆ 成交要点", "◆ Highlights"))
        lines += rows
    lines.append(
        L(
            "※ 数据源：warframe.market 官方成交统计（48 小时逐小时 / 90 天逐日）",
            "※ Source: warframe.market official trade statistics (hourly for 48h / daily for 90 days)",
        )
    )
    return (title, lines)


_RELIC_TITLE_EN = {
    "遗物列表": "Relic List",
    "遗物入库": "Relics Entering the Vault",
    "遗物出库": "Relics Leaving the Vault",
}


def _relic_title(title: str) -> str:
    """遗物列表卡标题：英文模式把常用中文标题换成英文（「遗物列表：先锋（12 把）」带后缀的同理）。"""
    if not i18n.is_en():
        return title
    for zh_t, en_t in _RELIC_TITLE_EN.items():
        if title.startswith(zh_t):
            rest = title[len(zh_t) :]
            rest = re.sub(r"[：:]\s*", ": ", rest, count=1)
            rest = re.sub(
                r"\s*[（(](\d+) 把[)）]",
                lambda m: f" ({m.group(1)} relic{'' if m.group(1) == '1' else 's'})",
                rest,
            )
            return en_t + rest
    return title


# 遗物槽位稀有度 / 纪元的英文写法（数据层给的是中文键）
_RARITY_EN = {"稀有": "Rare", "罕见": "Uncommon", "常见": "Common"}


def _tier_key(t: str) -> str:
    """纪元归一成中文键（分组 / 排序用）：Lith → 古纪，其余原样。"""
    return _RELIC_TIER_EN2CN.get(t, t)


def _tier_disp(t: str) -> str:
    """纪元展示名：中文模式原样，英文模式 古纪 → Lith（「其他」→ Other）。"""
    if not i18n.is_en():
        return t
    if t == "其他":
        return "Other"
    return _RELIC_TIER_CN2EN.get(t, t).title() if t in _RELIC_TIER_CN2EN else t


def fmt_prime_relics(
    rows: list[dict], title: str = "遗物列表", page: int = 1, page_size: int = 10
) -> tuple[str, list[str]]:
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
    title = _relic_title(title)
    if not rows:
        return (title, [L("没有可展示的遗物", "No relics to show")])
    total = len(rows)
    pages = max(1, (total + page_size - 1) // page_size)
    page = max(1, min(page, pages))
    chunk = rows[(page - 1) * page_size : page * page_size]

    lines: list[str] = []
    for i, r in enumerate(chunk, (page - 1) * page_size + 1):
        drop = bool(r.get("unvaulted"))
        tag = L("可掉落", "Droppable") if drop else L("已入库", "Vaulted")
        lines.append(f"{i}. [{tag}] {r.get('cn', '?')}　{r.get('tier', '')}".rstrip())
        src = r.get("sources") or []
        if src:
            more = r.get("source_total", len(src)) - len(src)
            tail = (
                L(f"（共 {r['source_total']} 处）", f" ({r['source_total']} total)")
                if more > 0
                else ""
            )
            lines.append(L("　　掉落位置：", "　　Drops: ") + L("；", "; ").join(src) + tail)
        elif not drop:
            lines.append(
                L(
                    "　　掉落位置：已入库，当前不在任何掉落池中",
                    "　　Drops: Vaulted, not in any drop pool right now",
                )
            )
    lines.append(
        L(
            f"※ 第{page}/{pages}页，共{total}个；加 -2 / -3 翻页",
            f"※ (Page {page}/{pages}, {total} total) add -2 / -3 to flip pages",
        )
    )
    lines.append(
        L(
            "※ 出库=可掉落，入库=不可刷取；判定依据是 DE 官方掉落池",
            "※ Unvaulted = droppable, Vaulted = not farmable; based on DE's official drop pools",
        )
    )
    return (
        L(
            f"{title}（第{page}/{pages}页，共{total}个）",
            f"{title} (Page {page}/{pages}, {total} total)",
        ),
        lines,
    )


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
_TIER_PER_LINE = 12
# 遗物中文名：DE 官方简中写作「古纪 S1 遗物」（name_zh.json），卡面按
# 「古纪 S1」显示 —— 去掉「遗物」后缀但**保留纪元中文**：玩家口语就是
# 「古纪A1」，只留代号会看不出是哪个纪元（维护者 2026-09-17 反馈
# 「遗物中文缺失」：列表里当时显示的是「Lith A12」这类英文）。
_RELIC_TIER_EN2CN = {
    "Lith": "古纪",
    "Meso": "前纪",
    "Neo": "中纪",
    "Axi": "后纪",
    "Requiem": "安魂",
    "Omnia": "全能",
    "Vanguard": "先锋",
}
_RELIC_CN_TIERS = tuple(_RELIC_TIER_EN2CN.values())
_RELIC_TIER_CN2EN = {cn: en.lower() for en, cn in _RELIC_TIER_EN2CN.items()}
_RELIC_CODE_RE = re.compile(r"^([A-Za-z])(\d{1,2})$")
# 遗物名后缀：DE 官方给的是「古纪 S1 遗物」，英文 wiki/掉落表给的是
# 「Axi S20 Relic」—— 两种后缀都要能剥掉，否则代号里混进「Relic」
# 会匹配不上（relic_en_key 会直接返回空串）。
_REL_SUFFIX_RE = re.compile(r"\s*(?:遗物|relics?)\s*$", re.I)
_RELIC_TIGHT_RE = re.compile(r"^(古纪|前纪|中纪|后纪|安魂|全能|先锋)\s*([A-Za-z]\d{1,2})$")


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
    m = _RELIC_TIGHT_RE.match(s)  # 「后纪A2」这种没有空格的
    if m:
        return f"{_tier_disp(m.group(1))} {m.group(2).upper()}"
    toks = s.split()
    tier = ""
    if toks and toks[0] in _RELIC_CN_TIERS:
        tier = toks.pop(0)
    if toks and toks[0] in _RELIC_TIER_EN2CN:
        tier = _RELIC_TIER_EN2CN[toks.pop(0)]
    if not tier:
        return raw
    code = " ".join(toks).strip().upper()
    return f"{_tier_disp(tier)} {code}".strip()


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
    tight = _RELIC_TIGHT_RE.match(" ".join(toks))  # 「后纪A2」没有空格的写法
    if tight:
        return f"{_RELIC_TIER_CN2EN[tight.group(1)]} {tight.group(2).lower()}"
    tier = ""
    if toks[0] in _RELIC_TIER_EN2CN:  # 英文纪元在前：Axi A20
        tier = _RELIC_TIER_EN2CN[toks.pop(0)]
    elif toks[0] in _RELIC_CN_TIERS:  # 中文纪元在前：古纪 A12
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

    return sum(2 if unicodedata.east_asian_width(c) in ("F", "W") else 1 for c in s)


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


def fmt_relic_by_tier(
    rows: list[dict],
    title: str = "遗物列表",
    page: int = 1,
    page_size: int = 90,
    farm_hints: Optional[dict[str, list[str]]] = None,
    specials: Optional[dict[str, str]] = None,
) -> tuple[str, list[str]]:
    """遗物按**纪元**分组展示（2026-09-17 维护者要求）。

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
            纪元下单独起一行标出（维护者要求：「有一部分遗物只要指定位置能获取，
            那种单独去标记」）。
    """
    title = _relic_title(title)
    if not rows:
        return (title, [L("没有可展示的遗物", "No relics to show")])

    groups: dict[str, list[str]] = {}
    for r in rows:
        cn = relic_cn(r.get("cn") or "?")
        tier = _tier_key((r.get("tier_cn") or "").strip()) or (
            _tier_key(cn.split()[0]) if " " in cn else "其他"
        )
        groups.setdefault(tier, []).append(cn)

    order = [t for t in _RELIC_TIER_ORDER if t in groups] + [
        t for t in groups if t not in _RELIC_TIER_ORDER
    ]

    # 平铺成 (纪元, 显示名) 后分页；显示名 = 完整中文名（含纪元），
    # 自然序排列（A1 < A2 < … < A10，直接字符串排会得到 A1/A10/A11/A2）。
    flat: list[tuple[str, str]] = []
    for t in order:
        for cn in sorted(groups[t], key=_relic_sort_key):
            flat.append((t, cn))

    total = len(flat)
    pages = max(1, (total + page_size - 1) // page_size)
    page = max(1, min(page, pages))
    chunk = flat[(page - 1) * page_size : page * page_size]

    lines: list[str] = []
    cur, buf = None, []

    def _flush_buf():
        if buf:
            lines.append("· " + "　".join(buf))
            buf.clear()

    # 该纪元的两个补充行（只在出库卡传参时出现）。
    # ★ 2026-10-11 第二步改版：作为该纪元面板里的「从属行」（行首缩进、不带 ※）——
    #   旧版每个纪元都插两条 ※ 注脚，注脚散落在卡中间。
    # ★ 注意：这里刻意**不用全角空格**分隔 —— 全角空格会参与渲染层的
    #   列宽计算，混进去会把表格宽度算爆、触发安全阀放弃列对齐。
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
                shown = L("；", "; ").join(
                    L(f"{'、'.join(ns)}（{why}）", f"{', '.join(ns)} ({why})")
                    for why, ns in by_reason.items()
                )
                lines.append(L("　特殊渠道：", "　Special channels: ") + shown)
        _fh = (farm_hints or {}).get(t) or (farm_hints or {}).get(_tier_disp(t))
        if _fh:
            _pre = L("　推荐刷取：", "　Suggested farming: ")
            lines.append(_pre + " ｜ ".join(_fit_hints(_fh, prefix=_pre)))

    # 每行放几个：整段都在本页时**按行数均分**，不要出现「9 个一行 + 1 个
    # 孤零零」（10 个条目排成 9+1 时右半边空一大片，玩家说「浪费空间」）。
    def _per_line(count: int) -> int:
        if count <= _TIER_PER_LINE:
            return max(1, count)
        rows = (count + _TIER_PER_LINE - 1) // _TIER_PER_LINE
        return (count + rows - 1) // rows

    def _short(t: str, name: str) -> str:
        # 面板标题已写明纪元：格内只写代号（A1），不再每格重复「古纪」（全行相同的列不写）
        _td = _tier_disp(t)
        return name[len(_td) :].strip() if name.startswith(_td + " ") else name

    chunk_counts = Counter(t for t, _ in chunk)
    per_line = _TIER_PER_LINE
    for t, short in chunk:
        if t != cur:
            if cur is not None:
                _flush_buf()
                _tier_notes(cur)
            cur = t
            lines.append(L(f"◆ {t}（{len(groups[t])}）", f"◆ {_tier_disp(t)} ({len(groups[t])})"))
            per_line = _per_line(chunk_counts[t])
        buf.append(_short(t, short))
        if len(buf) >= per_line:
            _flush_buf()
    _flush_buf()
    if cur is not None:
        _tier_notes(cur)

    summary = " · ".join(f"{_tier_disp(t)} {len(groups[t])}" for t in order)
    lines.insert(
        0,
        L(f"◆ 共 {total} 个：{summary}", f"◆ {total} relic{'' if total == 1 else 's'}: {summary}"),
    )
    lines.append(
        L(
            f"※ 第{page}/{pages}页，加 -2 / -3 翻页；查某个遗物的奖励与出处用「遗物 名称」",
            f'※ (Page {page}/{pages}) add -2 / -3 to flip pages; send "relic <name>" for a relic\'s '
            "rewards and sources",
        )
    )
    return (
        L(
            f"{title}（第{page}/{pages}页，共{total}个）",
            f"{title} (Page {page}/{pages}, {total} total)",
        ),
        lines,
    )


# 部件反查卡最多列几把遗物（Forma 蓝图那种一张卡涉及 500+ 把，必须截断）
_PIECE_MAX_ROWS = 12
# 状态 → 卡面用词。★ 用词要和「遗物 出库/入库」两张卡的语义一致：
# 出库=现在能拿到，入库=收进金库、任务里刷不到。
_PIECE_STATE_TEXT = {"drop": "可掉落", "varzia": "仅阿耶兑换", "vaulted": "已入库"}
_PIECE_STATE_TEXT_EN = {"drop": "Droppable", "varzia": "Varzia only", "vaulted": "Vaulted"}
# 排序优先级：能拿到的排最前（用户查部件最先想知道的就是「现在能不能刷」）
_PIECE_STATE_ORDER = {"drop": 0, "varzia": 1, "vaulted": 2}


# ---------------------------------------------------------------------------
# 玄骸拍卖（Kuva / Tenet / Coda）
# ---------------------------------------------------------------------------
# 玄骸武器的元素只有这几种（WM 的 item.element 取值）。卡面显示中文，
# 用户输入中英文都认（维护者 2026-09-18：「只有伤害加成，没有元素」——
# 其实是显示成了英文 radiation/toxin，中文玩家对不上）。
LICH_ELEM_CN = {
    "magnetic": "磁力",
    "electricity": "电击",
    "toxin": "毒素",
    "heat": "火焰",
    "cold": "冰冻",
    "impact": "冲击",
    "slash": "切割",
    "radiation": "辐射",
}
LICH_ELEM_EN = {v: k for k, v in LICH_ELEM_CN.items()}
# 玩家可能写单字或英文
LICH_ELEM_ALT = {
    "电": "电击",
    "毒": "毒素",
    "火": "火焰",
    "冰": "冰冻",
    "辐": "辐射",
    "磁": "磁力",
    "冲": "冲击",
    "切": "切割",
    "electric": "电击",
    "toxic": "毒素",
    "fire": "火焰",
    "ice": "冰冻",
    "rad": "辐射",
    "mag": "磁力",
}
LICH_OWNER_STATUS_CN = {"ingame": "游戏内", "online": "在线"}
_LICH_OWNER_STATUS_EN = {"ingame": "In Game", "online": "Online"}
_LICH_ELEM_DISP_EN = {k: k.capitalize() for k in LICH_ELEM_CN}
_LICH_ONLINE_WORDS = ("游戏内", "在线", "In Game", "Online")


def fmt_lich_cells(auction: dict) -> list[str]:
    """玄骸拍卖的一行拆成列：[价格, 元素, 伤害, 状态, 信用, 幻纹]（缺字段用「?」/空串占位）。

    2026-09-18 维护者要求补三样：**元素（中文）**、**在线情况**、**信用等级**，
    这些数据本来就在 WM 返回的 item/owner 里，原实现只取了价格与伤害。
    """
    it = auction.get("item") or {}
    ow = auction.get("owner") or {}
    price = auction.get("buyout_price") or auction.get("starting_price") or 0
    raw_elem = str(it.get("element") or "")
    elem = (_LICH_ELEM_DISP_EN if i18n.is_en() else LICH_ELEM_CN).get(raw_elem, raw_elem or "?")
    dmg = it.get("damage")
    dmg_s = f"{int(dmg)}%" if isinstance(dmg, (int, float)) else "?"
    status = (_LICH_OWNER_STATUS_EN if i18n.is_en() else LICH_OWNER_STATUS_CN).get(
        str(ow.get("status") or ""), L("离线", "Offline")
    )
    rep = ow.get("reputation")
    rep_s = L(f"信用{int(rep)}", f"Rep {int(rep)}") if isinstance(rep, (int, float)) else ""
    eph = L("幻纹★", "Ephemera★") if it.get("having_ephemera") else ""
    return [f"{price}p", elem, dmg_s, status, rep_s, eph]


def fmt_lich_row(i: int, auction: dict) -> str:
    """玄骸拍卖的一行（单行写法，列用全角空格分隔；``i`` 仅为兼容旧签名）。"""
    return "· " + "　".join(c for c in fmt_lich_cells(auction) if c)


def fmt_lich_table(pool: list[dict], *, total: Optional[int] = None) -> list[str]:
    """玄骸拍卖卡正文：卡首答案 + 明细表（整页全相同的列不写）+ 注脚。

    列：价格｜元素｜伤害｜状态｜信用｜幻纹；状态整页同值时并进区块标题（「游戏内挂单」）。
    """
    rows = [fmt_lich_cells(a) for a in pool]
    n_online = sum(1 for c in rows if c[3] in _LICH_ONLINE_WORDS)
    prices = [int(c[0][:-1]) for c in rows if c[3] in _LICH_ONLINE_WORDS and c[0][:-1].isdigit()]
    if not prices:
        prices = [int(c[0][:-1]) for c in rows if c[0][:-1].isdigit()]
    kv = [
        f"{min(prices)}p" if prices else "—",
        L(
            f"在线 {n_online} 条（共 {total or len(rows)} 条）",
            f"{n_online} online ({total or len(rows)} total)",
        ),
    ]
    lines = [L("◆ 最低价：", "◆ Lowest price: ") + "｜".join(kv)]
    names = [
        L("价格", "Price"),
        L("元素", "Element"),
        L("伤害", "Damage"),
        L("状态", "Status"),
        L("信用", "Rep"),
        L("幻纹", "Ephemera"),
    ]
    keep = [0, 1, 2]  # 价格 / 元素 / 伤害 恒写
    state = ""
    vals3 = {c[3] for c in rows}
    if len(vals3) > 1:
        keep.append(3)
    else:
        state = next(iter(vals3)) if vals3 else ""
    if any(c[4] for c in rows):
        keep.append(4)
    if any(c[5] for c in rows):
        keep.append(5)
    lines.append(
        L(
            f"◆ {state}挂单（在线优先 → 伤害降序 → 价格升序）",
            f"◆ {state + ' ' if state else ''}Listings (online first → damage descending → price ascending)",
        )
    )
    lines.append("　" + "　".join(names[k] for k in keep))
    for c in rows:
        lines.append("· " + "　".join(c[k] for k in keep).rstrip("　"))
    lines.append(
        L(
            "※ 信用 = 卖家交易信誉等级（0~5）；幻纹★ = 带幻纹；在线优先排序",
            "※ Rep = seller trade reputation level (0-5); Ephemera★ = has an ephemera; online first",
        )
    )
    return lines


def fmt_relic_piece(
    piece: str, origins: list[dict], *, farm_hints: Optional[dict[str, list[str]]] = None
) -> tuple[str, list[str]]:
    """部件反查卡：这个部件出自哪些遗物、**现在还能不能拿到**、去哪刷。

    2026-09-18 玩家反馈：「没有写能不能获取，以及刚刚那种推荐位置也能加上」
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
        rows.append(
            {
                "cn": cn,
                "rarity": (o.get("rarity") or "").strip(),
                "state": state,
                "tier": _tier_key(cn.split()[0]) if " " in cn else "其他",
            }
        )
    ptitle = L(f"部件出处：{piece}", f"Part Sources: {piece}")
    if not rows:
        return (ptitle, [L("未找到该部件的遗物出处", "No relic sources found for this part")])

    rows.sort(
        key=lambda r: (
            _PIECE_STATE_ORDER[r["state"]],
            _RELIC_TIER_ORDER.index(r["tier"])
            if r["tier"] in _RELIC_TIER_ORDER
            else len(_RELIC_TIER_ORDER),
            _relic_sort_key(r["cn"]),
        )
    )

    n_ok = sum(1 for r in rows if r["state"] != "vaulted")
    S = "　"
    _state_txt = _PIECE_STATE_TEXT_EN if i18n.is_en() else _PIECE_STATE_TEXT
    lines = [
        L(
            f"◆ 可获取：{n_ok} 把｜已入库 {len(rows) - n_ok} 把",
            f"◆ Obtainable: {n_ok}｜Vaulted {len(rows) - n_ok}",
        ),
        L("◆ 遗物出处", "◆ Relic Sources"),
        L(f"{S}遗物{S}槽位{S}状态", f"{S}Relic{S}Slot{S}Status"),
    ]
    for r in rows[:_PIECE_MAX_ROWS]:
        _rar = r["rarity"] or "?"
        lines.append(
            L(
                f"· {r['cn']}{S}{_rar}槽{S}{_PIECE_STATE_TEXT[r['state']]}",
                f"· {r['cn']}{S}{_RARITY_EN.get(_rar, _rar)} slot{S}{_state_txt[r['state']]}",
            )
        )
    if len(rows) > _PIECE_MAX_ROWS:
        # ★ 「另有 N 把已入库」是当初按「可掉落排最前」想当然写的：Forma 蓝图
        #   有 30 把可掉落、545 把总数，截到 12 行后**隐藏的 533 把里还有 18 把
        #   是可掉落的** —— 这么说等于把能刷的藏起来了。这里按实际算。
        hidden = rows[_PIECE_MAX_ROWS:]
        hidden_ok = sum(1 for r in hidden if r["state"] != "vaulted")
        if hidden_ok:
            lines.append(
                L(
                    f"※ 还有 {len(hidden)} 把未列出（其中 {hidden_ok} 把现在就能掉落）",
                    f"※ {len(hidden)} more not shown ({hidden_ok} of them droppable right now)",
                )
            )
        else:
            lines.append(
                L(
                    f"※ 另有 {len(hidden)} 把已入库遗物未展开",
                    f"※ {len(hidden)} more vaulted relics not shown",
                )
            )

    # ------------------------------------------------------------------
    # 推荐刷取：**永远只给「当前出库（可掉落）」的那几把所属纪元**。
    # 维护者 2026-09-18 明确：「出库的时候另外两个多半入库了，到时候下面还是
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
        hints = (farm_hints or {}).get(t) or (farm_hints or {}).get(_tier_disp(t)) or []
        if not hints:
            continue
        # 纪元名放在括号里：写成「古纪推荐刷取：」读着像机器话，
        # 且与出库卡那句「※ 推荐刷取：」保持同一个说法更好认。
        prefix = L(f"※ 推荐刷取（{t}）：", f"※ Suggested farming ({_tier_disp(t)}): ")
        picked = _fit_hints(hints, prefix=prefix)
        if picked:
            lines.append(prefix + " ｜ ".join(picked))
            hinted += 1

    n_drop = sum(1 for r in rows if r["state"] == "drop")
    if not n_ok:
        lines.append(
            L(
                "※ 这些遗物都已入库，当前任务里刷不到；可留意阿耶（Varzia）兑换或等 DE 重新出库",
                "※ All of these relics are vaulted and cannot be farmed right now; "
                "watch for Varzia redemptions or wait for DE to unvault them",
            )
        )
    elif n_drop and not hinted:
        # 有可掉落遗物却没算出推荐点（该纪元不在 farm_hints 里）—— 理论上
        # 不会发生（当前 34 把可掉落遗物都在古/前/中/后四纪元），但真发生了
        # 也必须说清是哪把，不能静默把「能刷」的信息吞掉。
        names = L("、", ", ").join(r["cn"] for r in rows if r["state"] == "drop")[:36]
        lines.append(
            L(
                f"※ 能掉落的是 {names}，用「遗物 名称」可看具体出处",
                f'※ Droppable: {names}; use "relic <name>" for exact sources',
            )
        )
    elif not n_drop:
        lines.append(
            L(
                "※ 可获取的那几把只在阿耶（Varzia）商店出售，任务里刷不到",
                "※ The obtainable ones are only sold in Varzia's shop and cannot be farmed in missions",
            )
        )
    return (ptitle, lines)


def fmt_relic_rewards(
    relic: str, slots: list[dict], prices: Optional[dict] = None
) -> tuple[str, list[str]]:
    """遗物三槽位奖励 + 每件奖励的杜卡德 / 白金 / 杜・白比值。

    Args:
        relic: 遗物名（如 ``后纪 A2``）。
        slots: ``[{"rarity", "name", "chance"}]``。
        prices: 名称 -> ``{"ducats", "plat", "dpp"}``（来自 WM 杜卡德榜单）。

    Returns:
        ``(标题, 行列表)``。
    """
    relic = relic_cn(relic)  # 「古纪 Lith A1」→「古纪 A1」（英文模式 Lith A1）
    rtitle = L(f"遗物：{relic}", f"Relic: {relic}")
    if not slots:
        return (rtitle, [L("未找到该遗物的奖励数据", "No reward data found for this relic")])
    prices = prices or {}
    S = "　"
    lines = [
        L("◆ 三槽位奖励", "◆ Three Reward Slots"),
        L(f"{S}稀有度{S}奖励{S}杜卡德{S}白金{S}杜/p", f"{S}Rarity{S}Reward{S}Ducats{S}Plat{S}D/p"),
    ]
    for s in slots:
        name = s.get("name") or "?"
        p = prices.get(name) or prices.get(re.sub(r"\s+", "", name)) or {}
        _rar = s.get("rarity", "?")
        cells = [
            f"[{L(_rar, _RARITY_EN.get(_rar, _rar))}]",
            ducat_span(ducats_of(prices, name), name),
        ]
        if p:
            cells += [
                L(f"{int(p['ducats'])}杜", f"{int(p['ducats'])} Ducats") if p.get("ducats") else "",
                f"{p['plat']:.0f}p" if p.get("plat") else "",
                f"{p['dpp']:.1f}" if p.get("dpp") else "",
            ]
        if s.get("chance"):
            cells.append(f"{s['chance']}%")
        lines.append("· " + S.join(cells))
    lines.append(
        L(
            "※ 杜=杜卡德　p=白金　杜/p=每白金换到的杜卡德（越高越值）；"
            "价格来自 warframe.market 杜卡德计算器同源数据（近 1 小时）",
            "※ Ducats = Ducats　p = Platinum　D/p = Ducats per Platinum (higher is better); "
            "prices come from the same data as the warframe.market Ducat calculator (last hour)",
        )
    )
    return (rtitle, lines)
