# -*- coding: utf-8 -*-
"""进度 / 科研 / 日历类指令（D2 自 main.py 迁入，方法体逐字未改）。

覆盖路由键 6 项：calendar / deeparchimedea / temporalarchimedea /
steelpath / descendia / incursions；另迁 _valence_note（效价快照说明行，
消费者 _rotation_lines 仍留 main.py，经 self 跨 Mixin 调用）。
子包纪律：不 import astrbot（事件对象鸭子类型）。
"""
from __future__ import annotations

from .. import formatters as fmt
from .base import Reply


class ProgressCommands:
    """Mixin：进度 / 科研 / 日历 handler（挂载于 main.WarframeSDJK）。"""

    async def _h_calendar(self, parsed, event, platform) -> Reply:
        # 子模式（奖励/清单/覆写）2026-09-29 下线：裸「日历」= 沃沃式全量卡
        title, lines = fmt.fmt_calendar(await self.client.calendar(platform))
        return Reply(title, lines, footer=fmt.fmt_platform_footer(platform))

    async def _h_deep(self, parsed, event, platform) -> Reply:
        title, lines = fmt.fmt_archimedea(
            await self.client.deep_archimedea(platform), "深层科研")
        return Reply(title, lines, footer=fmt.fmt_platform_footer(platform))

    async def _h_temporal(self, parsed, event, platform) -> Reply:
        title, lines = fmt.fmt_archimedea(
            await self.client.temporal_archimedea(platform), "时光科研")
        return Reply(title, lines, footer=fmt.fmt_platform_footer(platform))

    async def _h_steelpath(self, parsed, event, platform) -> Reply:
        """钢铁之路 = 钢铁精华兑换（Teshin 荣誉商店）。

        数据源是 warframe wiki 的 Steel Essence 页（常驻 24 件 + 每周轮换 8 件），
        中文名取自 DE 官方 language 表。warframestat 的 steelPath 端点、
        完整版 worldState 均已不可用（403 / 404），改用本地表 + 官方轮换锚点推算。
        """
        title, lines = fmt.fmt_steel_essence_shop()
        return Reply(title, lines, footer=fmt.fmt_platform_footer(platform, "钢铁精华兑换"))

    # 仲裁筛选关键词 -> arbi.wf.wiki 的 missionNameZh 规范值
    # （该站中文表把 Infested Salvage 写作「INFESTED 资源回收」，比对前需去前缀）
    @staticmethod
    def _valence_note(data: dict, window_start) -> str:
        """元素加成快照的说明行（快照落在上一轮时明确标「可能已变」）。"""
        from datetime import datetime, timezone
        raw = data.get("valence_snapshot") or ""
        if not raw:
            return ""
        try:
            snap = datetime.fromisoformat(raw)
        except (TypeError, ValueError):
            return ""
        if snap.tzinfo is None:
            snap = snap.replace(tzinfo=timezone.utc)
        stale = snap < window_start
        when = snap.astimezone(timezone.utc).strftime("%m-%d %H:%M")
        flag = "（上一轮快照，数值可能已变）" if stale else "（本轮快照）"
        return (f"※ 元素与加成为 {when} UTC 快照{flag}：wiki「Reset」页玩家上报值"
                f"（无官方 API，换轮后需人工刷新），以游戏内商店为准")
    async def _h_descendia(self, parsed, event, platform) -> Reply:
        data = await self.client.descendia(platform)
        title, lines = fmt.fmt_descendia(data)
        return Reply(title, lines, footer=fmt.fmt_platform_footer(platform))

    async def _h_incursions(self, parsed, event, platform) -> Reply:
        data = await self.client.steel_path_incursions(platform)
        title, lines = fmt.fmt_incursions(data)
        return Reply(title, lines, footer=fmt.fmt_platform_footer(platform))
