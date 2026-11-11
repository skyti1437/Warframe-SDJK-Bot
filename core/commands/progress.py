# -*- coding: utf-8 -*-
"""进度 / 科研 / 日历类指令（D2 自 main.py 迁入，方法体逐字未改）。

覆盖路由键 6 项：calendar / deeparchimedea / temporalarchimedea /
steelpath / descendia / incursions；另迁 _valence_note（效价快照说明行，
消费者 _rotation_lines 仍留 main.py，经 self 跨 Mixin 调用）。
子包纪律：不 import astrbot（事件对象鸭子类型）。
"""

from __future__ import annotations

from .. import formatters as fmt
from ..i18n import L
from .base import Reply


class ProgressCommands:
    """Mixin：进度 / 科研 / 日历 handler（挂载于 main.WarframeSDJK）。"""

    async def _h_calendar(self, parsed, event, platform) -> Reply:
        # 子模式（奖励/清单/覆写）2026-09-29 下线：裸「日历」= 沃沃式全量卡
        title, lines = fmt.fmt_calendar(await self.client.calendar(platform))
        return Reply(title, lines, footer=fmt.fmt_platform_footer(platform))

    async def _h_deep(self, parsed, event, platform) -> Reply:
        title, lines = fmt.fmt_archimedea(
            await self.client.deep_archimedea(platform), L("深层科研", "Deep Archimedea")
        )
        return Reply(title, lines, footer=fmt.fmt_platform_footer(platform))

    async def _h_temporal(self, parsed, event, platform) -> Reply:
        title, lines = fmt.fmt_archimedea(
            await self.client.temporal_archimedea(platform), L("时光科研", "Temporal Archimedea")
        )
        return Reply(title, lines, footer=fmt.fmt_platform_footer(platform))

    async def _h_steelpath(self, parsed, event, platform) -> Reply:
        """钢铁之路 = 钢铁精华兑换（Teshin 荣誉商店）。

        数据源是 warframe wiki 的 Steel Essence 页（常驻 24 件 + 每周轮换 8 件），
        中文名取自 DE 官方 language 表。warframestat 的 steelPath 端点、
        完整版 worldState 均已不可用（403 / 404），改用本地表 + 官方轮换锚点推算。
        """
        title, lines = fmt.fmt_steel_essence_shop()
        return Reply(
            title,
            lines,
            footer=fmt.fmt_platform_footer(
                platform,
                L(
                    "来源 wiki Steel Essence 页（每周轮换 8 件循环）",
                    "Source: wiki Steel Essence page (weekly 8-item rotation)",
                ),
            ),
        )

    async def _h_slivershop(self, parsed, event, platform) -> Reply:
        """碎银兑换 = Palladino 裂罅碎块商店（地球「钢铁守望」）。

        2026-10-03 取证：全表常驻、无轮换库存；每周限购周一 00:00 UTC 重置。
        数据 core/data/de/palladino_shop.json，中文名 DE 官方 language 表。
        """
        title, lines = fmt.fmt_sliver_shop()
        return Reply(
            title,
            lines,
            footer=fmt.fmt_platform_footer(
                platform,
                L(
                    "来源 wiki.warframe.com/w/Riven_Sliver · 中文名取 DE 官方 language 表",
                    "Source: wiki.warframe.com/w/Riven_Sliver · names from the DE official language table",
                ),
            ),
        )

    # 仲裁筛选关键词 -> arbi.wf.wiki 的 missionNameZh 规范值
    # （该站中文表把 Infested Salvage 写作「INFESTED 资源回收」，比对前需去前缀）
    @staticmethod
    def _valence_note(data: dict, window_start) -> str:
        """元素加成快照的说明行（快照落在上一轮时明确标「可能已变」）。"""
        from datetime import datetime, timezone

        raw = data.get("valence_snapshot") or ""
        _missing = L(
            "※ 元素与加成尚未取得，后台自动获取，以游戏内为准",
            "※ Elements and bonuses are not available yet; they are fetched automatically in the background, in-game values prevail",
        )
        if not raw:
            return _missing
        try:
            snap = datetime.fromisoformat(raw)
        except (TypeError, ValueError):
            return _missing
        if snap.tzinfo is None:
            snap = snap.replace(tzinfo=timezone.utc)
        stale = snap < window_start
        when = fmt.format_time(snap)
        flag = (
            L("（上一轮快照，数值可能已变）", " (previous-round snapshot, values may have changed)")
            if stale
            else L("（本轮快照）", " (this round's snapshot)")
        )
        from ..api_client import WarframeClient

        try:
            same = WarframeClient.valence_values_stale(data)
        except Exception:  # noqa: BLE001 - epoch 缺失等：不加标注
            same = False
        if not stale and same:
            # 快照时刻在本轮、数值却与上一轮相同 ⇒ wiki 还没人更新（见 valence_values_stale）
            flag = L(
                "（数值疑似仍是上一轮：wiki 尚未完整更新，可能已变）",
                " (values look like the previous round: the wiki is not fully updated yet, they may have changed)",
            )
        return L(
            f"※ 元素与加成为 {when} 快照{flag}：wiki 武器页玩家上报值"
            f"（无官方 API，后台自动获取），以游戏内商店为准",
            f"※ Elements and bonuses are a {when} snapshot{flag}: player-reported values from "
            f"wiki weapon pages (no official API, fetched automatically), the in-game shop prevails",
        )

    async def _h_descendia(self, parsed, event, platform) -> Reply:
        data = await self.client.descendia(platform)
        title, lines = fmt.fmt_descendia(data)
        return Reply(title, lines, footer=fmt.fmt_platform_footer(platform))

    async def _h_incursions(self, parsed, event, platform) -> Reply:
        data = await self.client.steel_path_incursions(platform)
        title, lines = fmt.fmt_incursions(data)
        return Reply(title, lines, footer=fmt.fmt_platform_footer(platform))
