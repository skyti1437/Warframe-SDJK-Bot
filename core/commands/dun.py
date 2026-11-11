# -*- coding: utf-8 -*-
"""蹲点订阅域（D11 自 main.py 迁入，方法体逐字未改）。

覆盖路由键 1 项：dun（拆解表最后一笔）。推送回调 _push_send 依赖 astrbot
的 MessageChain，按「子包禁 import astrbot」纪律留守 main.py；本 Mixin 经
self._push_send 跨 Mixin 调用。
"""

from __future__ import annotations

import re
import time

from .. import arbi as _arbi
from .. import i18n
from ..i18n import L
from ..parser import (
    FISSURE_MODIFIER_WORDS,
    PLATFORM_DISPLAY,
    contains_fissure_tier,
    fissure_tier_hint,
    parse_duration,
    parse_hit_count,
    parse_fissure_filter,
    parse_time_window,
)
from ..push import parse_bounty_rule
from ..push import (
    PUSH_EVENTS,
    Subscription,
    alias_preset,
    build_cancel_selector,
    normalize_event,
)
from .arbitration import _ARB_TYPES_STR_EN, _arb_en_toks
from .base import Reply, err_card

# ---------------------------------------------------------------------------
# 英文参数别名：只在「蹲」的参数解析处认，不进通用输入别名表。
# 键一律小写；值是内部中文键 / 中文写法，之后走原有的解析路径。
# ---------------------------------------------------------------------------
# 事件类型：中文内部键 -> (英文指令词, 英文展示名, 英文说明)
_EV_EN: dict[str, tuple[str, str, str]] = {
    "裂隙": (
        "fissures",
        "Void Fissures",
        "New Void Fissure appears (supports filters like capture, steel survival)",
    ),
    "夜灵": ("plains", "Plains of Eidolon", "Plains of Eidolon turns to night (Eidolon hunts)"),
    "山谷": ("vallis", "Orb Vallis", "Orb Vallis temperature switches (Warm / Cold)"),
    "魔胎": ("cambion", "Cambion Drift", "Cambion Drift faction rotates (Fass / Vome)"),
    "地球": ("earth", "Earth Cycle", "Earth day / night cycle changes"),
    "双衍": ("duviri", "Duviri Spiral", "Duviri Spiral (mood) rotates"),
    "奸商": ("baro", "Baro Ki'Teer", "Void Trader Baro Ki'Teer arrives / leaves"),
    "突击": ("sortie", "Sortie", "Daily Sortie refreshes"),
    "执刑官": ("archon", "Archon Hunt", "Weekly Archon Hunt refreshes"),
    "仲裁": (
        "arbitration",
        "Arbitration",
        "Arbitration slot changes (filters: efficient / legendary / survival / defense ...)",
    ),
    "钢路侵袭": ("incursions", "Steel Path Incursions", "Daily Steel Path Incursions refresh"),
    "警报": (
        "alerts",
        "Alerts",
        "Event alerts appear (regular alerts are retired; e.g. Tenno Unity alerts)",
    ),
    "入侵": ("invasions", "Invasions", "New invasion appears"),
    "新闻": ("news", "News", "Official news / hotfix posted"),
    "每日特惠": ("dailydeal", "Daily Deal", "Darvo's Daily Deal refreshes"),
    "活动": ("events", "Events", "Limited-time event starts / ends"),
    "1999日历": (
        "calendar",
        "1999 Calendar",
        "1999 Calendar: season rotation / daily schedule refresh",
    ),
    "灵化武器": (
        "incarnon",
        "Incarnon Weapons",
        "Weekly Incarnon rotation (Steel Path Circuit, 9-week cycle, Monday 00:00 UTC)",
    ),
    "信条": (
        "tenet",
        "Tenet Weapons",
        "Ergo Tenet weapon stock and element bonuses (every 96 hours)",
    ),
    "终幕": ("coda", "Coda Weapons", "Coda weapon batch rotation (batches A/B every 96 hours)"),
    "钢精兑换": (
        "steelessence",
        "Steel Essence Shop",
        "Teshin's Steel Essence weekly rotation (Monday 00:00 UTC, 8-item cycle)",
    ),
    "碎银兑换": (
        "slivershop",
        "Riven Sliver Shop",
        "Palladino's Riven Sliver shop weekly purchase limits reset (Monday 00:00 UTC)",
    ),
    "赏金": (
        "bounties",
        "Bounties",
        "Bounty rotation (Zariman / Laboratories / 1999; filter by region + task, e.g. zariman efficient exterminate)",
    ),
    "阿耶兑换": ("varzia", "Prime Resurgence", "Prime Resurgence rotation (Regal Aya offers)"),
    "电波": ("nightwave", "Nightwave", "Nightwave daily / weekly challenges refresh"),
}
# 额外的英文写法（含常见别称）
_EV_EN_EXTRA = {
    "fissure": "裂隙",
    "rift": "裂隙",
    "rifts": "裂隙",
    "cetus": "夜灵",
    "eidolon": "夜灵",
    "poe": "夜灵",
    "orb": "山谷",
    "orbvallis": "山谷",
    "deimos": "魔胎",
    "drift": "魔胎",
    "spiral": "双衍",
    "trader": "奸商",
    "voidtrader": "奸商",
    "arbi": "仲裁",
    "arbitrations": "仲裁",
    "incursion": "钢路侵袭",
    "alert": "警报",
    "invasion": "入侵",
    "daily": "每日特惠",
    "darvo": "每日特惠",
    "dailydeals": "每日特惠",
    "event": "活动",
    "1999": "1999日历",
    "tenets": "信条",
    "teshin": "钢精兑换",
    "sliver": "碎银兑换",
    "palladino": "碎银兑换",
    "bounty": "赏金",
    "resurgence": "阿耶兑换",
    "aya": "阿耶兑换",
}
_EV_BY_EN = {v[0]: k for k, v in _EV_EN.items()}
_EV_BY_EN.update(_EV_EN_EXTRA)

_EN_WORDS = {
    "cancel": "取消",
    "help": "帮助",
    "forever": "永久",
    "permanent": "永久",
    "permanently": "永久",
    "unlimited": "永久",
}
# 裂隙筛选的英文写法 -> 中文筛选词（空格 = 且、逗号 = 或的规则不变）
_FISSURE_EN = {
    "steel": "钢铁",
    "steelpath": "钢铁",
    "hard": "钢铁",
    "normal": "普通",
    "storm": "九重天",
    "storms": "九重天",
    "voidstorm": "九重天",
    "voidstorms": "九重天",
    "railjack": "九重天",
    "void": "虚空",
    "lith": "古纪",
    "meso": "前纪",
    "neo": "中纪",
    "axi": "后纪",
    "requiem": "安魂",
    "omnia": "全能",
    "capture": "捕获",
    "exterminate": "歼灭",
    "extermination": "歼灭",
    "survival": "生存",
    "defense": "防御",
    "interception": "拦截",
    "defection": "叛逃",
    "excavation": "挖掘",
    "disruption": "中断",
    "rescue": "救援",
    "sabotage": "破坏",
    "spy": "间谍",
    "alchemy": "元素转换",
    "skirmish": "前哨战",
}
_BOUNTY_EN = {
    "zariman": "扎里曼",
    "laboratories": "实验室",
    "laboratory": "实验室",
    "lab": "实验室",
    "sanctum": "圣所",
    "efficient": "高效",
    "exterminate": "歼灭",
    "survival": "生存",
    "defense": "防御",
    "capture": "捕获",
    "excavation": "挖掘",
    "disruption": "中断",
    "sabotage": "破坏",
    "rescue": "救援",
    "spy": "间谍",
}
_DAY_EN = {"mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6, "sun": 7}
_DAYS_RE = re.compile(r"(mon|tue|wed|thu|fri|sat|sun)(?:/(?:mon|tue|wed|thu|fri|sat|sun))*", re.I)
_EN_HITS_RE = re.compile(r"(\d{1,3})\s*(?:x|hits?|times)", re.I)
_EN_RANGE_RE = re.compile(r"(\d{1,2})\s*[-–]\s*(\d{1,2})")


def _en_arg_toks(toks: list) -> list:
    """英文参数词并入中文内部写法（事件类型 / 取消 / 帮助 / 永久 / 次数 / 时间窗）；
    中文词与不认识的词原样保留。"""
    out: list = []
    toks = [str(t) for t in toks]
    i = 0
    while i < len(toks):
        t = toks[i]
        low = t.lower()
        nxt = toks[i + 1].lower() if i + 1 < len(toks) else ""
        if low == "daily" and re.fullmatch(r"\d{1,2}", nxt):
            out.append(f"每天{nxt}点")
            i += 1
        elif low in _EN_WORDS:
            out.append(_EN_WORDS[low])
        elif low in _EV_BY_EN:
            out.append(_EV_BY_EN[low])
        elif re.fullmatch(r"\d{1,3}", low) and nxt in ("x", "hit", "hits", "time", "times"):
            out.append(f"{low}次")
            i += 1
        elif _EN_HITS_RE.fullmatch(low):
            out.append(_EN_HITS_RE.fullmatch(low).group(1) + "次")
        elif _DAYS_RE.fullmatch(low) and (
            re.fullmatch(r"\d{1,2}", nxt) or _EN_RANGE_RE.fullmatch(nxt or "-")
        ):
            days = "/".join(str(_DAY_EN[d]) for d in low.split("/"))
            m = _EN_RANGE_RE.fullmatch(nxt)
            out.append(f"周{days} " + (f"{m.group(1)}到{m.group(2)}" if m else f"{nxt}点"))
            i += 1
        elif _EN_RANGE_RE.fullmatch(low):
            m = _EN_RANGE_RE.fullmatch(low)
            out.append(f"{m.group(1)}到{m.group(2)}")
        else:
            out.append(t)
        i += 1
    return out


def _en_rule_toks(event_type: str, toks: list) -> list:
    """各类型筛选词的英文写法并入中文内部键。"""
    if event_type == "仲裁":
        return _arb_en_toks(toks)
    if event_type == "裂隙":
        merged: list = []
        for t in toks:
            prev = merged[-1].lower() if merged else ""
            if (prev, t.lower()) in (("steel", "path"), ("void", "storm"), ("void", "storms")):
                merged[-1] = prev + t.lower()  # steel path -> steelpath；void storm -> voidstorm
            elif (prev, t.lower()) == ("mobile", "defense"):
                merged[-1] = "移动防御"
            elif (prev, t.lower()) in (
                ("void", "flood"),
                ("void", "cascade"),
                ("void", "armageddon"),
            ):
                # 以 Void 开头的任务类型整体并入官方中文名，不拆成「虚空（星球）+ 残词」
                merged[-1] = {"flood": "虚空洪流", "cascade": "虚空覆涌", "armageddon": "虚空决战"}[
                    t.lower()
                ]
            else:
                merged.append(t)
        out = []
        for t in merged:
            parts = [
                " ".join(_FISSURE_EN.get(w.lower(), w) for w in p.split()) for p in t.split(",")
            ]
            out.append(",".join(parts))
        return out
    if event_type == "赏金":
        return [_BOUNTY_EN.get(t.lower(), t) for t in toks]
    return list(toks)


# 中文筛选词 -> 英文展示（订阅回执里回显筛选条件用）
_RULE_ZH_EN = {
    "钢铁": "Steel Path",
    "钢路": "Steel Path",
    "普通": "Normal",
    "九重天": "Void Storms",
    "虚空": "Void",
    "古纪": "Lith",
    "前纪": "Meso",
    "中纪": "Neo",
    "后纪": "Axi",
    "安魂": "Requiem",
    "全能": "Omnia",
    "扎里曼": "Zariman",
    "实验室": "Laboratories",
    "圣所": "Sanctum",
    "高效": "Efficient",
    "传奇": "Legendary",
}


def _rule_en(rule: str, event_type: str) -> str:
    """订阅筛选的英文回显：空格（且）写成 +，逗号（或）写成 or；认不出的词原样保留。"""
    from ..parser import _CN_TO_MISSION

    sep = r"[,，]"
    parts = []
    for part in re.split(sep, rule):
        words = []
        for w in part.split() if event_type == "裂隙" else [part.strip()]:
            en = _RULE_ZH_EN.get(w)
            if not en and w in _CN_TO_MISSION:
                en = _CN_TO_MISSION[w].title()
            if not en and w in _arb_zh_en():
                en = _arb_zh_en()[w]
            words.append(en or w)
        if any(words):
            parts.append(" + ".join(x for x in words if x))
    return {"赏金": " + ", "仲裁": ", "}.get(event_type, " or ").join(parts)


def _arb_zh_en() -> dict:
    from .arbitration import _ARB_ZH_EN

    return _ARB_ZH_EN


def _ev_name(ev: str) -> str:
    """事件类型的展示名：中文原键；英文模式给英文名。"""
    return L(ev, _EV_EN[ev][1] if ev in _EV_EN else ev)


def _ev_cmd(ev: str) -> str:
    """事件类型作为指令词的写法（卡片里的示例）。"""
    return L(ev, _EV_EN[ev][0] if ev in _EV_EN else ev)


def _window_text_en(window) -> str:
    bits = []
    if window.start is not None:
        end = window.end if window.end is not None else window.start
        if end == window.start:
            bits.append(f"{window.start}:00-{window.start + 1}:00")
        elif end < window.start:
            bits.append(f"{window.start}:00-{end}:00 next day")
        else:
            bits.append(f"{window.start}:00-{end}:00")
    if window.days:
        names = {v: k.title() for k, v in _DAY_EN.items()}
        bits.append("/".join(names[d] for d in sorted(window.days)))
    if window.at_hour is not None:
        bits.append(f"daily at {window.at_hour} ({window.at_hour}:00-{window.at_hour}:59)")
    return " ".join(bits) or "All day"


class DunCommands:
    """Mixin：蹲订阅 handler（挂载于 main.WarframeSDJK）。"""

    async def _h_dun(self, parsed, event, platform) -> Reply:
        umo = event.unified_msg_origin
        toks = _en_arg_toks(list(parsed.content or []))

        if not toks or toks == ["帮助"]:
            lines = [
                L(
                    f"◆ 可蹲类型（共 {len(PUSH_EVENTS)} 项，发「蹲 类型」订阅）",
                    f'◆ Watchable types ({len(PUSH_EVENTS)} total; send "subscribe <type>" to subscribe)',
                )
            ]
            for ev, (desc, wired) in PUSH_EVENTS.items():
                lines.append(
                    L(
                        f"· {ev}　{desc}" + ("" if wired else "（未接线）"),
                        f"· {_ev_cmd(ev)}　{_EV_EN[ev][2] if ev in _EV_EN else desc}"
                        + ("" if wired else " (not wired)"),
                    )
                )
            lines += L(
                [
                    "◆ 写法（蹲 类型 筛选 时长 次数 时间）",
                    "· 时长　永久/7天/两周/N小时…（时长和次数都不写=命中一次后取消）",
                    "· 次数　2次 / 两次…：命中 N 次后取消；和时长一起写时先到为准，如 蹲 裂隙 钢铁生存 7天 3次",
                    "· 时间　北京时间：22到8（22:00–次日8:00，8 点整结束）/ 0到24（全天）/ 每天19点（19:00–19:59）"
                    " / 周1/3/5 23点 / 周5 22到8（含周六凌晨）",
                    "· 筛选　空格分开 = 同时满足（钢铁 古纪 = 钢铁的古纪裂隙）；逗号分开 = 任一满足（钢铁,古纪）",
                    "· 档位　T1–T6 = 古纪/前纪/中纪/后纪/安魂/全能（档位词自动按裂隙订阅），"
                    "如 蹲 钢铁t5歼灭",
                    "· 赏金　可筛 地区+任务（如 蹲 赏金 扎里曼 高效歼灭）；覆盖 扎里曼/实验室/1999",
                    "· 取消　蹲 取消（自己发起的全部；群管理员可删全群）/ 蹲 取消 裂隙 捕获（只删匹配项）",
                    "※ 筛选：空格 = 且、逗号 = 或 ——「钢铁 古纪」只蹲钢铁古纪，「钢铁,古纪」钢铁或古纪都推",
                ],
                [
                    "◆ Syntax (subscribe <type> <filter> <duration> <hits> <time>)",
                    "· Duration　permanent / 7d / 2w / 12h ... (omit both duration and hits = cancel after the first hit)",
                    "· Hits　2x / 3 hits: cancel after N hits; with a duration, whichever comes first, e.g. subscribe fissures steel survival 7d 3x",
                    "· Time　Beijing time: 22-8 (22:00-08:00 next day, ends at 8:00 sharp) / 0-24 (all day) / daily 19 (19:00-19:59)"
                    " / mon/wed/fri 23 / fri 22-8 (includes early Saturday)",
                    "· Filter　space = AND (steel lith = Steel Path Lith fissures); comma = OR (steel,lith)",
                    "· Tier　T1-T6 = Lith/Meso/Neo/Axi/Requiem/Omnia (a tier word subscribes to fissures automatically), "
                    "e.g. subscribe steel t5 exterminate",
                    "· Bounties　filter by region + task (e.g. subscribe bounties zariman efficient exterminate); covers zariman/laboratories/1999",
                    "· Cancel　subscribe cancel (all of your own; group admins can remove the whole group) / subscribe cancel fissures capture (matching ones only)",
                    '※ Filter: space = AND, comma = OR. "steel lith" only watches Steel Path Lith, "steel,lith" pushes Steel Path or Lith',
                ],
            )
            # 2026-09-21 修：裸「蹲」应出卡片图（与其它指令一致）。
            # 原 text_only=True 是 v0.5 接手时的祖传写法，全插件唯一一处强制纯文本；
            # 渲染失败时 _build_results 本就会自动降级文字，无需在此抢降级。
            return Reply(L("可蹲类型", "Watch Types"), lines)

        # 取消：「取消」位置无关——「蹲 取消」「蹲 裂隙 取消」「蹲 取消 裂隙 捕获」
        # 都合法；其余词构成筛选条件（2026-09-14 修：旧版见「取消」就删全群）。
        # 权限（2026-10-10 安全审查）：管理员可删本群任意订阅；普通成员只能删
        # **自己发起的**（created_by == 自己）—— 旧版任何群成员一句「蹲 取消」
        # 就能清空全群订阅。created_by 为空的旧订阅只有管理员能删。
        if "取消" in toks:
            heads = [t for t in toks if t != "取消"]
            _admin = self._is_admin(event)
            _me = str(event.get_sender_id() or "").strip()

            def _mine(s) -> bool:
                return _admin or (bool(_me) and s.created_by == _me)

            _scope = L("本群" if _admin else "你在本群发起的", "this group's" if _admin else "your")
            if not heads:
                removed = await self.subs.remove(lambda s: s.umo == umo and _mine(s))
                if removed:
                    return Reply(
                        L("蹲 订阅", "Watch Subscription"),
                        [
                            L(
                                f"◆ 结果：已取消{_scope}全部 {removed} 条蹲订阅",
                                f"◆ Result: cancelled all {removed} of {_scope} watch subscriptions",
                            )
                        ],
                    )
                return err_card(
                    L(
                        f"{_scope}蹲订阅为空"
                        + ("" if _admin else "（他人发起的订阅需群管理员取消）"),
                        f"{_scope.capitalize()} watch subscriptions are empty"
                        + (
                            ""
                            if _admin
                            else " (subscriptions made by others need a group admin to cancel)"
                        ),
                    ),
                    L(
                        "无需取消；要订阅请发「蹲 类型」，发送「蹲 帮助」查看说明",
                        'Nothing to cancel; to subscribe send "subscribe <type>", or "subscribe help" for details',
                    ),
                    title=L("蹲 订阅", "Watch Subscription"),
                )
            _ev, _keys, _exact, _fuzzy, _label = build_cancel_selector(umo, heads)
            if i18n.is_en():  # 标签回显玩家写的英文原词，而不是并入后的中文内部键
                _label = " ".join(
                    str(t)
                    for t in (parsed.content or [])
                    if str(t).lower() not in ("cancel", "取消")
                )
            removed = await self.subs.remove(lambda s: _exact(s) and _mine(s))
            if not removed and _keys:
                removed = await self.subs.remove(lambda s: _fuzzy(s) and _mine(s))
            if removed:
                return Reply(
                    L("蹲 订阅", "Watch Subscription"),
                    [
                        L(
                            f"◆ 结果：已取消「{_label}」相关订阅 {removed} 条",
                            f'◆ Result: cancelled {removed} subscriptions matching "{_label}"',
                        )
                    ],
                )
            return err_card(
                L(
                    f"{_scope}没有「{_label}」相关订阅",
                    f'{_scope.capitalize()} subscriptions include none matching "{_label}"',
                ),
                L(
                    "检查类型 / 筛选词写法；「蹲 取消」可清空自己发起的全部订阅",
                    'Check the type / filter spelling; "subscribe cancel" clears all of your own',
                ),
                title=L("蹲 订阅", "Watch Subscription"),
            )

        event_type = None
        rest: list[str] = []
        for tok in toks:
            ev = normalize_event(tok)
            if ev and event_type is None:
                event_type = ev
                if alias_preset(tok):  # 钢铁裂隙 → 裂隙 + 筛选「钢铁」（别把档位吃掉）
                    rest.append(alias_preset(tok))
            else:
                rest.append(tok)
        # ★ 2026-10-03（口径）：「T1–T6 / 古纪…全能」这类**档位词本身
        #   就含裂隙语义** —— 没写类型词时自动判定为裂隙（「蹲 钢铁t5歼灭」
        #   直接生效，不必再写「裂隙」）。⚠ 只认档位词：钢铁/虚空/地点词
        #   仍走「未识别」提示，不重蹈 2026-09-14 静默降级的覆辙。
        if event_type is None and any(contains_fissure_tier(t) for t in toks):
            event_type = "裂隙"
        if event_type:  # 英文筛选词并入中文内部键（中文输入原样不动）
            rest = _en_rule_toks(event_type, rest)
        if len(toks) >= 2 and toks[1] == "帮助" and event_type:
            desc, _ = PUSH_EVENTS[event_type]
            return Reply(
                raw_text=L(
                    f"【蹲 {event_type}】{desc}"
                    + (
                        "；示例：蹲 " + event_type + " 普通捕获,钢铁虚空生存 永久"
                        if event_type == "裂隙"
                        else ""
                    ),
                    f"[Watch {_ev_name(event_type)}] "
                    + (_EV_EN[event_type][2] if event_type in _EV_EN else desc)
                    + (
                        "; example: subscribe fissures capture,steel void survival forever"
                        if event_type == "裂隙"
                        else ""
                    ),
                )
            )
        # 没有识别出事件类型，且第一项不是「帮助/取消」时，**不要**静默降级
        # 为「裂隙 + 筛选=...」——这正是玩家反馈「蹲功能不生效」的根因：地点词
        # 被错当成裂隙筛选加入订阅，而该地点根本不刷裂隙，所以永远不会触发。
        if event_type is None:
            # 可蹲清单从 PUSH_EVENTS 现算（只列已接线的），别手抄——
            # 手抄版把不可订阅的「警报」也列了进去，还漏了山谷/魔胎等类型。
            wired = " / ".join(ev for ev, (_, ok) in PUSH_EVENTS.items() if ok)
            # ★ 2026-10-03（线上实证）：用户写「蹲 钢铁t5歼灭」漏了类型词，
            #   收到「未识别」一头雾水。**只提示不改语义**（不做静默推断，
            #   2026-09-14 的教训）——像裂隙筛选词的补一句正确写法。
            _fis_like = next(
                (t for t in toks if any(w in t.lower() for w in FISSURE_MODIFIER_WORDS)), ""
            )
            _sug = (
                L(
                    f"※ 「{_fis_like}」像是裂隙筛选词——正确写法要先写类型：蹲 裂隙 {_fis_like}",
                    f'※ "{_fis_like}" looks like a fissure filter word - write the type first: subscribe fissures {_fis_like}',
                )
                if _fis_like
                else ""
            )
            _first = str(parsed.content[0]) if i18n.is_en() and parsed.content else ""
            return err_card(
                L(
                    "未识别为可蹲类型「" + (toks[0] if toks else "") + "」",
                    'Not recognised as a watchable type: "' + _first + '"',
                ),
                L(
                    "从可蹲类型里选一个重发；发送「蹲 帮助」查看完整说明",
                    'Pick one of the watchable types and resend; send "subscribe help" for the full guide',
                ),
                (
                    L("可蹲类型", "Watchable types"),
                    " / ".join(_ev_cmd(ev) for ev, (_, ok) in PUSH_EVENTS.items() if ok),
                ),
                _sug,
                title=L("蹲 订阅", "Watch Subscription"),
            )
        # 「蹲 类型」正常订阅路径：此处 event_type 已确定，必须先取 desc/wired，
        # 否则下面 `if not wired` 会在未赋值分支触发 NameError（「蹲 类型」直接失效的根因）。
        desc, wired = PUSH_EVENTS[event_type]
        if not wired:
            return err_card(
                L(
                    f"「{event_type}」当前无法订阅：{desc}",
                    f'"{_ev_name(event_type)}" cannot be subscribed to right now: '
                    + (_EV_EN[event_type][2] if event_type in _EV_EN else desc),
                ),
                L(
                    "换一个可蹲类型；发送「蹲 帮助」查看完整说明",
                    'Pick another watchable type; send "subscribe help" for the full guide',
                ),
                title=L("蹲 订阅", "Watch Subscription"),
            )
        if not self.groups.get(umo)["push"]:
            return err_card(
                L("本群推送功能未开启", "Push is not enabled in this group"),
                L("请管理员发送 .开启 推送 后再蹲", "Ask a group admin to send .enable push first"),
                title=L("蹲 订阅", "Watch Subscription"),
            )

        duration = None
        hits = None
        window = None
        rule_parts: list[str] = []
        i = 0
        while i < len(rest):
            tok = rest[i]
            # ★ 2026-10-17：「2次」= 命中次数（原先落进筛选词 ⇒ 订阅永远不命中且不提示）
            if hits is None and parse_hit_count(tok) is not None:
                hits = parse_hit_count(tok)
                i += 1
                continue
            if duration is None and parse_duration(tok) is not None:
                duration = parse_duration(tok)
                i += 1
                continue
            if window is None:
                w = parse_time_window(tok)
                if w is None and i + 1 < len(rest) and tok.startswith("周"):
                    w = parse_time_window(tok + " " + rest[i + 1])
                    if w is not None:
                        i += 1
                if w is not None:
                    window = w
                    i += 1
                    continue
            rule_parts.append(tok)
            i += 1

        now = time.time()
        # L-4: 单 umo 订阅上限（防滥用/防误循环订阅撑爆存储）
        MAX_SUBS_PER_UMO = 30
        existing = self.subs.for_umo(umo)
        if len(existing) >= MAX_SUBS_PER_UMO:
            return err_card(
                L(
                    f"本群蹲订阅已达上限（{MAX_SUBS_PER_UMO} 条）",
                    f"This group has reached the watch subscription limit ({MAX_SUBS_PER_UMO})",
                ),
                L(
                    f"请先「蹲 取消」或「蹲 {event_type} 取消」清理后再试",
                    f'Clean up first with "subscribe cancel" or "subscribe {_ev_cmd(event_type)} cancel", then retry',
                ),
                title=L("蹲 订阅", "Watch Subscription"),
            )
        sub = Subscription(
            umo=umo,
            platform=platform,
            event=event_type,
            lang=i18n.current_lang(),  # 创建时固化，之后改会话或全局语言不影响这条订阅
            # ★ 2026-10-16：裂隙筛选里**逗号 = 或、空格 = 且**（parse_fissure_filter，与「裂隙」查询同口径）。
            #   旧实现一律用逗号拼 ⇒「蹲 钢铁裂隙 古纪」落成「钢铁,古纪」= 钢铁 或 古纪（推来安魂钢铁）。
            #   用户自己敲的逗号仍在 token 内，「蹲 裂隙 生存,防御」照旧是或。其他事件的规则解析不变。
            rule=(" " if event_type == "裂隙" else ",").join(rule_parts),
            windows={}
            if window is None or window.is_always
            else {
                "start": window.start,
                "end": window.end,
                "days": sorted(window.days) if window.days else None,
                "at_hour": window.at_hour,
            },
            until=-1 if duration == -1 else (now + duration if duration else -1),
            # 次数与时长：只写次数 ⇒ 不限时、命中 N 次取消；两者都写 ⇒ 先到为准（Subscription.consume / expired）
            once=duration is None and hits is None,
            hits_left=hits,
            # ★ A1（2026-10-03 用户批准的特例，解除 L-2 自发脱敏）：**仅**
            #   「蹲」订阅记录发起人 id，用于命中时 @ 当事人。默认空串、
            #   绝不硬编码；公开仓/公开包内此位置必须恒为空（发版前 grep
            #   QQ 特征须 OK）；未来如需扩展须再确认。
            created_by=str(event.get_sender_id() or "").strip(),
        )
        await self.subs.add(sub)
        if hits is not None:
            # 规则（维护者 10-17）：只写时长 ⇒ 不限次数；只写次数 ⇒ 命中 N 次取消；两者一起写 ⇒ 都生效、先到为准
            if duration and duration != -1:
                dur_text = L(
                    f"{duration // 3600} 小时内命中 {hits} 次即取消（先到为准）",
                    f"cancels after {hits} hits within {duration // 3600} hours (whichever comes first)",
                )
            else:
                dur_text = (
                    L(
                        f"命中 {hits} 次后取消（不限时）",
                        f"cancels after {hits} hits (no time limit)",
                    )
                    if duration == -1
                    else L(f"命中 {hits} 次后取消", f"cancels after {hits} hits")
                )
        else:
            dur_text = (
                L("永久", "Permanent")
                if duration == -1
                else (
                    L(f"{duration // 3600} 小时", f"{duration // 3600} hours")
                    if duration
                    else L("命中一次即取消", "Cancels after the first hit")
                )
            )
        win_text = L(window.describe(), _window_text_en(window)) if window else L("全天", "All day")
        rule_text = sub.rule or L("无", "None")
        if event_type == "裂隙" and sub.rule:
            rule_text += f"（{parse_fissure_filter(sub.rule).describe()}）"
        _rule_show = sub.rule
        if i18n.is_en():
            _rule_show = _rule_en(sub.rule, event_type)
            rule_text = _rule_show or "None"
        _ev_show = _ev_name(event_type)
        _plat = L(
            PLATFORM_DISPLAY.get(platform, platform),
            "International" if platform in PLATFORM_DISPLAY else str(platform).upper(),
        )
        lines = [
            L(
                f"◆ 订阅成功：#{sub.sid}｜{event_type}｜{dur_text}",
                f"◆ Subscribed: #{sub.sid}｜{_ev_show}｜{dur_text}",
            ),
            L(
                f"◆ 取消方式：蹲 取消 · 蹲 {event_type} 取消",
                f"◆ To cancel: subscribe cancel · subscribe {_ev_cmd(event_type)} cancel",
            ),
            L("◆ 订阅内容", "◆ Subscription"),
            L(f"· 类型　{event_type}（{_plat}）", f"· Type　{_ev_show} ({_plat})"),
            L(f"· 筛选　{rule_text}", f"· Filter　{rule_text}"),
            L(f"· 时长　{dur_text}", f"· Duration　{dur_text}"),
            L(f"· 时间　{win_text}", f"· Time　{win_text}"),
            L(
                f"※ 订阅 #{sub.sid} 已加入本群推送队列",
                f"※ Subscription #{sub.sid} has been added to this group's push queue",
            ),
        ]
        # ★ 筛选词「写错/用在不支持的类型上」必须当场说清（铁律 A：不能静默失效）。
        #   2026-09-19 玩家反馈「蹲 仲裁 高效 永久」没生效 —— 一半原因就是
        #   仲裁的筛选词当时被整体忽略，用户看不到任何提示。
        if sub.rule and not _arbi.rule_supported(event_type):
            lines.append(
                L(
                    f"※ ⚠ 注意：「{event_type}」不支持筛选，"
                    f"上面的「{sub.rule}」不会生效"
                    f"（目前只有 裂隙 / 仲裁 支持筛选）",
                    f'※ ⚠ Note: "{_ev_show}" does not support filters, '
                    f'so "{_rule_show}" above will have no effect '
                    f"(only fissures / arbitration support filters)",
                )
            )
        elif event_type == "裂隙":
            # ★ 2026-10-16：识别不了的残余按节点 / 星球名子串匹配 —— 写错就永远不命中，当场说清。
            _sub_names = [
                g["substr"] for g in parse_fissure_filter(sub.rule).groups if g.get("substr")
            ]
            if _sub_names:
                lines.append(
                    L(
                        f"※ 「{'、'.join(_sub_names)}」按节点 / 星球名匹配，名字写错会一直收不到推送",
                        f'※ "{", ".join(_sub_names)}" is matched against node / planet names; '
                        "a misspelled name will never push",
                    )
                )
        elif event_type == "仲裁":
            _types, _ratings, _unknown = _arbi.parse_rule(sub.rule)
            if _unknown:
                lines.append(
                    L(
                        f"※ ⚠ 未识别的筛选词：{'、'.join(_unknown)}"
                        f"（可用：高效 / 传奇 / {_arbi.TYPES_STR}）",
                        f"※ ⚠ Unrecognised filter words: {', '.join(_unknown)} "
                        f"(available: efficient / legendary / {_ARB_TYPES_STR_EN})",
                    )
                )
            if _ratings:
                lines.append(
                    L(
                        f"※ 只在评级 {'/'.join(_ratings)} 的场次推送（想全都收就别写「高效/传奇」）",
                        f"※ Only slots rated {'/'.join(_ratings)} are pushed "
                        '(to receive all, leave out "efficient / legendary")',
                    )
                )
        if event_type == "裂隙":
            # 2026-09-26 的「空格陷阱」提示（多词 = 取或，请连写）已随 10-16 语义统一（空格 = 且）删除。
            # ★ 2026-10-03：档位写法越界（T0/T7/T9、孤立的 t、T5x…）当场提示，
            #   不静默（铁律 A）；合法 T1–T6 返回空串。
            _t_hint = fissure_tier_hint(rule_parts)
            if _t_hint:
                lines.append(
                    L(
                        "※ ⚠ " + _t_hint.removeprefix("※ "),
                        "※ ⚠ Fissure tiers only support T1-T6 "
                        "(T1=Lith, T2=Meso, T3=Neo, T4=Axi, T5=Requiem, T6=Omnia)",
                    )
                )
        if event_type == "赏金" and rule_parts:
            # ★ C2（2026-10-03）：赏金筛选建议带地区词（否则任何地区命中都推）。
            #   只提示、不改语义（rule 原样落库）。
            _tags = parse_bounty_rule(" ".join(rule_parts))["tags"]
            if not _tags:
                lines.append(
                    L(
                        "※ 提示：赏金筛选建议带地区（扎里曼/实验室/圣所/"
                        "1999），否则各地区命中都会推；例：蹲 赏金 "
                        "扎里曼 高效歼灭",
                        "※ Tip: add a region to bounty filters (zariman / laboratories / sanctum / "
                        "1999), otherwise hits in every region are pushed; "
                        "e.g. subscribe bounties zariman efficient exterminate",
                    )
                )
        return Reply(L("订阅成功（蹲）", "Subscribed (Watch)"), lines)
