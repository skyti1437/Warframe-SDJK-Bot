# -*- coding: utf-8 -*-
"""蹲点订阅域（D11 自 main.py 迁入，方法体逐字未改）。

覆盖路由键 1 项：dun（拆解表最后一笔）。推送回调 _push_send 依赖 astrbot
的 MessageChain，按「子包禁 import astrbot」纪律留守 main.py；本 Mixin 经
self._push_send 跨 Mixin 调用。
"""
from __future__ import annotations

import time

from .. import arbi as _arbi
from ..parser import (PLATFORM_DISPLAY, dun_rule_hint, parse_duration,
                      parse_fissure_filter, parse_time_window)
from ..push import (PUSH_EVENTS, Subscription, build_cancel_selector,
                    normalize_event)
from .base import Reply


class DunCommands:
    """Mixin：蹲订阅 handler（挂载于 main.WarframeSDJK）。"""

    async def _h_dun(self, parsed, event, platform) -> Reply:
        umo = event.unified_msg_origin
        toks = list(parsed.content or [])

        if not toks or toks == ["帮助"]:
            lines = []
            for ev, (desc, wired) in PUSH_EVENTS.items():
                lines.append(f"· {ev}：{desc}" + ("" if wired else "（未接线）"))
            lines += ["", "时长：永久/7天/两周/N小时…（不写=命中一次后取消）",
                      "时间：22到8 / 每天19点 / 周1/3/5 23点",
                      "筛选：一个词 = 一个条件（如 钢铁防御）；多个条件用逗号/空格并列（取或）",
                      "取消：蹲 取消（全部）/ 蹲 取消 裂隙 捕获（只删匹配项）"]
            # 2026-09-21 修：裸「蹲」应出卡片图（与其它指令一致）。
            # 原 text_only=True 是 v0.5 接手时的祖传写法，全插件唯一一处强制纯文本；
            # 渲染失败时 _build_results 本就会自动降级文字，无需在此抢降级。
            return Reply("可蹲类型", lines)

        # 取消：「取消」位置无关——「蹲 取消」「蹲 裂隙 取消」「蹲 取消 裂隙 捕获」
        # 都合法；其余词构成筛选条件（2026-09-14 修：旧版见「取消」就删全群）。
        if "取消" in toks:
            heads = [t for t in toks if t != "取消"]
            if not heads:
                removed = await self.subs.remove(lambda s: s.umo == umo)
                return Reply(raw_text=f"已取消本群全部 {removed} 条蹲订阅"
                                      if removed else "本群没有蹲订阅")
            _ev, _keys, _exact, _fuzzy, _label = build_cancel_selector(umo, heads)
            removed = await self.subs.remove(_exact)
            if not removed and _keys:
                removed = await self.subs.remove(_fuzzy)
            return Reply(raw_text=f"已取消「{_label}」相关订阅 {removed} 条"
                                  if removed else f"本群没有「{_label}」相关订阅")

        event_type = None
        rest: list[str] = []
        for tok in toks:
            ev = normalize_event(tok)
            if ev and event_type is None:
                event_type = ev
            else:
                rest.append(tok)
        if len(toks) >= 2 and toks[1] == "帮助" and event_type:
            desc, _ = PUSH_EVENTS[event_type]
            return Reply(raw_text=f"【蹲 {event_type}】{desc}" +
                         ("；示例：蹲 " + event_type + " 普通捕获,钢铁虚空生存 永久"
                          if event_type == "裂隙" else ""))
        # 没有识别出事件类型，且第一项不是「帮助/取消」时，**不要**静默降级
        # 为「裂隙 + 筛选=...」——这正是玩家反馈「蹲功能不生效」的根因：地点词
        # 被错当成裂隙筛选加入订阅，而该地点根本不刷裂隙，所以永远不会触发。
        if event_type is None:
            # 可蹲清单从 PUSH_EVENTS 现算（只列已接线的），别手抄——
            # 手抄版把不可订阅的「警报」也列了进去，还漏了山谷/魔胎等类型。
            wired = " / ".join(ev for ev, (_, ok) in PUSH_EVENTS.items() if ok)
            return Reply(raw_text=(
                "未识别为可蹲类型「" + (toks[0] if toks else "") + "」。\n"
                f"可蹲类型：{wired}\n"
                "发送「蹲 帮助」查看完整说明"))
        # 「蹲 类型」正常订阅路径：此处 event_type 已确定，必须先取 desc/wired，
        # 否则下面 `if not wired` 会在未赋值分支触发 NameError（「蹲 类型」直接失效的根因）。
        desc, wired = PUSH_EVENTS[event_type]
        if not wired:
            return Reply(raw_text=f"「{event_type}」当前无法订阅。\n"
                                  f"原因：{desc}")
        if not self.groups.get(umo)["push"]:
            return Reply(raw_text="本群推送功能未开启，请管理员发送 .开启 推送 后再蹲")

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
            return Reply(raw_text=f"⛔ 本群蹲订阅已达上限（{MAX_SUBS_PER_UMO} 条），"
                                  f"请先「蹲 取消」或「蹲 {event_type} 取消」清理后再试")
        sub = Subscription(
            umo=umo, platform=platform, event=event_type,
            rule=",".join(rule_parts),
            windows={} if window is None or window.is_always else {
                "start": window.start, "end": window.end,
                "days": sorted(window.days) if window.days else None,
                "at_hour": window.at_hour},
            until=-1 if duration == -1 else (now + duration if duration else -1),
            once=duration is None,
            hits_left=None,
            created_by="",  # L-2: 不再落盘 QQ 昵称/可识别字符串
        )
        await self.subs.add(sub)
        dur_text = ("永久" if duration == -1 else
                    (f"{duration // 3600} 小时" if duration else "命中一次即取消"))
        win_text = window.describe() if window else "全天"
        rule_text = sub.rule or "无"
        if event_type == "裂隙" and sub.rule:
            rule_text += f"（{parse_fissure_filter(sub.rule).describe()}）"
        lines = [
            f"类型　{event_type}（{PLATFORM_DISPLAY.get(platform, platform)}）",
            f"筛选　{rule_text}",
            f"时长　{dur_text}",
            f"时间　{win_text}",
            "─" * 16,
            f"订阅 #{sub.sid} 已加入本群推送队列",
            f"取消方式：蹲 取消 · 蹲 {event_type} 取消",
        ]
        # ★ 筛选词「写错/用在不支持的类型上」必须当场说清（铁律 A：不能静默失效）。
        #   2026-09-19 玩家反馈「蹲 仲裁 高效 永久」没生效 —— 一半原因就是
        #   仲裁的筛选词当时被整体忽略，用户看不到任何提示。
        if sub.rule and not _arbi.rule_supported(event_type):
            lines.append(f"※ 注意：「{event_type}」不支持筛选，"
                         f"上面的「{sub.rule}」不会生效"
                         f"（目前只有 裂隙 / 仲裁 支持筛选）")
        elif event_type == "仲裁":
            _types, _ratings, _unknown = _arbi.parse_rule(sub.rule)
            if _unknown:
                lines.append(f"※ 未识别的筛选词：{'、'.join(_unknown)}"
                             f"（可用：高效 / 传奇 / {_arbi.TYPES_STR}）")
            if _ratings:
                lines.append(f"※ 只在评级 {'/'.join(_ratings)} 的场次推送"
                             f"（想全都收就别写「高效/传奇」）")
        if event_type == "裂隙":
            # ★ 2026-09-26（审核通过）：多词元 = 多条件取或，而「钢铁/虚空/后纪…」
            #   这类纯修饰词本意是与任务词合成一个条件 → 拆开写语义被放大。
            #   只提示、**不改语义**（订阅按用户原样落库）。
            #   非裂隙类型（仲裁 / 钢路侵袭）的 rule 由 `arbi.match_rule` 等各自解释，
            #   语义不同、也不共用逗号分组，**本轮不做提示**（免将来重复问）。
            _hint = dun_rule_hint(rule_parts)
            if _hint:
                lines.append(_hint)
        return Reply("◆ 蹲订阅成功", lines)
