# -*- coding: utf-8 -*-
"""蹲点订阅域（D11 自 main.py 迁入，方法体逐字未改）。

覆盖路由键 1 项：dun（拆解表最后一笔）。推送回调 _push_send 依赖 astrbot
的 MessageChain，按「子包禁 import astrbot」纪律留守 main.py；本 Mixin 经
self._push_send 跨 Mixin 调用。
"""

from __future__ import annotations

import time

from .. import arbi as _arbi
from ..parser import (
    FISSURE_MODIFIER_WORDS,
    PLATFORM_DISPLAY,
    contains_fissure_tier,
    dun_rule_hint,
    fissure_tier_hint,
    parse_duration,
    parse_fissure_filter,
    parse_time_window,
)
from ..push import parse_bounty_rule
from ..push import PUSH_EVENTS, Subscription, build_cancel_selector, normalize_event
from .base import Reply, err_card


class DunCommands:
    """Mixin：蹲订阅 handler（挂载于 main.WarframeSDJK）。"""

    async def _h_dun(self, parsed, event, platform) -> Reply:
        umo = event.unified_msg_origin
        toks = list(parsed.content or [])

        if not toks or toks == ["帮助"]:
            lines = [f"◆ 可蹲类型（共 {len(PUSH_EVENTS)} 项，发「蹲 类型」订阅）"]
            for ev, (desc, wired) in PUSH_EVENTS.items():
                lines.append(f"· {ev}　{desc}" + ("" if wired else "（未接线）"))
            lines += [
                "◆ 写法（蹲 类型 筛选 时长 时间）",
                "· 时长　永久/7天/两周/N小时…（不写=命中一次后取消）",
                "· 时间　22到8 / 每天19点 / 周1/3/5 23点",
                "· 筛选　一个词 = 一个条件（如 钢铁防御）；多个条件用逗号/空格并列（取或）",
                "· 档位　T1–T6 = 古纪/前纪/中纪/后纪/安魂/全能（档位词自动按裂隙订阅），"
                "如 蹲 钢铁t5歼灭",
                "· 赏金　可筛 地区+任务（如 蹲 赏金 扎里曼 高效歼灭）；覆盖 扎里曼/实验室/1999",
                "· 取消　蹲 取消（自己发起的全部；群管理员可删全群）/ 蹲 取消 裂隙 捕获（只删匹配项）",
                "※ ⚠ 筛选连写 = 一个条件，空格拆开 = 取或：「钢铁防御」≠「钢铁 防御」",
            ]
            # 2026-09-21 修：裸「蹲」应出卡片图（与其它指令一致）。
            # 原 text_only=True 是 v0.5 接手时的祖传写法，全插件唯一一处强制纯文本；
            # 渲染失败时 _build_results 本就会自动降级文字，无需在此抢降级。
            return Reply("可蹲类型", lines)

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

            _scope = "本群" if _admin else "你在本群发起的"
            if not heads:
                removed = await self.subs.remove(lambda s: s.umo == umo and _mine(s))
                if removed:
                    return Reply("蹲 订阅", [f"◆ 结果：已取消{_scope}全部 {removed} 条蹲订阅"])
                return err_card(
                    f"{_scope}蹲订阅为空" + ("" if _admin else "（他人发起的订阅需群管理员取消）"),
                    "无需取消；要订阅请发「蹲 类型」，发送「蹲 帮助」查看说明",
                    title="蹲 订阅",
                )
            _ev, _keys, _exact, _fuzzy, _label = build_cancel_selector(umo, heads)
            removed = await self.subs.remove(lambda s: _exact(s) and _mine(s))
            if not removed and _keys:
                removed = await self.subs.remove(lambda s: _fuzzy(s) and _mine(s))
            if removed:
                return Reply("蹲 订阅", [f"◆ 结果：已取消「{_label}」相关订阅 {removed} 条"])
            return err_card(
                f"{_scope}没有「{_label}」相关订阅",
                "检查类型 / 筛选词写法；「蹲 取消」可清空自己发起的全部订阅",
                title="蹲 订阅",
            )

        event_type = None
        rest: list[str] = []
        for tok in toks:
            ev = normalize_event(tok)
            if ev and event_type is None:
                event_type = ev
            else:
                rest.append(tok)
        # ★ 2026-10-03（口径）：「T1–T6 / 古纪…全能」这类**档位词本身
        #   就含裂隙语义** —— 没写类型词时自动判定为裂隙（「蹲 钢铁t5歼灭」
        #   直接生效，不必再写「裂隙」）。⚠ 只认档位词：钢铁/虚空/地点词
        #   仍走「未识别」提示，不重蹈 2026-09-14 静默降级的覆辙。
        if event_type is None and any(contains_fissure_tier(t) for t in toks):
            event_type = "裂隙"
        if len(toks) >= 2 and toks[1] == "帮助" and event_type:
            desc, _ = PUSH_EVENTS[event_type]
            return Reply(
                raw_text=f"【蹲 {event_type}】{desc}"
                + (
                    "；示例：蹲 " + event_type + " 普通捕获,钢铁虚空生存 永久"
                    if event_type == "裂隙"
                    else ""
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
                f"※ 「{_fis_like}」像是裂隙筛选词——正确写法要先写类型：蹲 裂隙 {_fis_like}"
                if _fis_like
                else ""
            )
            return err_card(
                "未识别为可蹲类型「" + (toks[0] if toks else "") + "」",
                "从可蹲类型里选一个重发；发送「蹲 帮助」查看完整说明",
                ("可蹲类型", wired),
                _sug,
                title="蹲 订阅",
            )
        # 「蹲 类型」正常订阅路径：此处 event_type 已确定，必须先取 desc/wired，
        # 否则下面 `if not wired` 会在未赋值分支触发 NameError（「蹲 类型」直接失效的根因）。
        desc, wired = PUSH_EVENTS[event_type]
        if not wired:
            return err_card(
                f"「{event_type}」当前无法订阅：{desc}",
                "换一个可蹲类型；发送「蹲 帮助」查看完整说明",
                title="蹲 订阅",
            )
        if not self.groups.get(umo)["push"]:
            return err_card(
                "本群推送功能未开启",
                "请管理员发送 .开启 推送 后再蹲",
                title="蹲 订阅",
            )

        duration = None
        window = None
        rule_parts: list[str] = []
        i = 0
        while i < len(rest):
            tok = rest[i]
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
                f"本群蹲订阅已达上限（{MAX_SUBS_PER_UMO} 条）",
                f"请先「蹲 取消」或「蹲 {event_type} 取消」清理后再试",
                title="蹲 订阅",
            )
        sub = Subscription(
            umo=umo,
            platform=platform,
            event=event_type,
            rule=",".join(rule_parts),
            windows={}
            if window is None or window.is_always
            else {
                "start": window.start,
                "end": window.end,
                "days": sorted(window.days) if window.days else None,
                "at_hour": window.at_hour,
            },
            until=-1 if duration == -1 else (now + duration if duration else -1),
            once=duration is None,
            hits_left=None,
            # ★ A1（2026-10-03 用户批准的特例，解除 L-2 自发脱敏）：**仅**
            #   「蹲」订阅记录发起人 id，用于命中时 @ 当事人。默认空串、
            #   绝不硬编码；公开仓/公开包内此位置必须恒为空（发版前 grep
            #   QQ 特征须 OK）；未来如需扩展须再确认。
            created_by=str(event.get_sender_id() or "").strip(),
        )
        await self.subs.add(sub)
        dur_text = (
            "永久"
            if duration == -1
            else (f"{duration // 3600} 小时" if duration else "命中一次即取消")
        )
        win_text = window.describe() if window else "全天"
        rule_text = sub.rule or "无"
        if event_type == "裂隙" and sub.rule:
            rule_text += f"（{parse_fissure_filter(sub.rule).describe()}）"
        lines = [
            f"◆ 订阅成功：#{sub.sid}｜{event_type}｜{dur_text}",
            f"◆ 取消方式：蹲 取消 · 蹲 {event_type} 取消",
            "◆ 订阅内容",
            f"· 类型　{event_type}（{PLATFORM_DISPLAY.get(platform, platform)}）",
            f"· 筛选　{rule_text}",
            f"· 时长　{dur_text}",
            f"· 时间　{win_text}",
            f"※ 订阅 #{sub.sid} 已加入本群推送队列",
        ]
        # ★ 筛选词「写错/用在不支持的类型上」必须当场说清（铁律 A：不能静默失效）。
        #   2026-09-19 玩家反馈「蹲 仲裁 高效 永久」没生效 —— 一半原因就是
        #   仲裁的筛选词当时被整体忽略，用户看不到任何提示。
        if sub.rule and not _arbi.rule_supported(event_type):
            lines.append(
                f"※ ⚠ 注意：「{event_type}」不支持筛选，"
                f"上面的「{sub.rule}」不会生效"
                f"（目前只有 裂隙 / 仲裁 支持筛选）"
            )
        elif event_type == "仲裁":
            _types, _ratings, _unknown = _arbi.parse_rule(sub.rule)
            if _unknown:
                lines.append(
                    f"※ ⚠ 未识别的筛选词：{'、'.join(_unknown)}"
                    f"（可用：高效 / 传奇 / {_arbi.TYPES_STR}）"
                )
            if _ratings:
                lines.append(
                    f"※ 只在评级 {'/'.join(_ratings)} 的场次推送（想全都收就别写「高效/传奇」）"
                )
        if event_type == "裂隙":
            # ★ 2026-09-26（审核通过）：多词元 = 多条件取或，而「钢铁/虚空/后纪…」
            #   这类纯修饰词本意是与任务词合成一个条件 → 拆开写语义被放大。
            #   只提示、**不改语义**（订阅按用户原样落库）。
            #   非裂隙类型（仲裁 / 钢路侵袭）的 rule 由 `arbi.match_rule` 等各自解释，
            #   语义不同、也不共用逗号分组，**本轮不做提示**（免将来重复问）。
            _hint = dun_rule_hint(rule_parts)
            if _hint:
                lines.append(_hint)
            # ★ 2026-10-03：档位写法越界（T0/T7/T9、孤立的 t、T5x…）当场提示，
            #   不静默（铁律 A）；合法 T1–T6 返回空串。
            _t_hint = fissure_tier_hint(rule_parts)
            if _t_hint:
                lines.append("※ ⚠ " + _t_hint.removeprefix("※ "))
        if event_type == "赏金" and rule_parts:
            # ★ C2（2026-10-03）：赏金筛选建议带地区词（否则任何地区命中都推）。
            #   只提示、不改语义（rule 原样落库）。
            _tags = parse_bounty_rule(" ".join(rule_parts))["tags"]
            if not _tags:
                lines.append(
                    "※ 提示：赏金筛选建议带地区（扎里曼/实验室/圣所/"
                    "1999），否则各地区命中都会推；例：蹲 赏金 "
                    "扎里曼 高效歼灭"
                )
        return Reply("订阅成功（蹲）", lines)
