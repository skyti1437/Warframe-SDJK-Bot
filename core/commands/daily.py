# -*- coding: utf-8 -*-
"""日常世界状态类指令（D1 自 main.py 迁入，方法体逐字未改）。

覆盖路由键 20 项：cetus / timers / fissures / sortie / archon / voidtrader /
dailydeals / alerts / invasions / nightwave / news / kuva / synthtargets /
construction / voidstorms / events / conclave / primevault / clanrewards /
flashsales。子包纪律：不 import astrbot（事件对象鸭子类型）。
"""
from __future__ import annotations

import json

from .. import baro
from .. import formatters as fmt
from .. import paths as core_paths
from ..parser import fissure_tier_hint, parse_fissure_filter
from .base import Reply


class DailyCommands:
    """Mixin：日常世界状态 handler（挂载于 main.WarframeSDJK）。"""

    async def _h_cetus(self, parsed, event, platform) -> Reply:
        res = await self._gather([self.client.cycle(platform, n)
                                  for n in ("cetus", "vallis", "cambion", "earth",
                                            "duviri", "zariman")])
        cycles = [r for r in res if isinstance(r, dict)]
        title, lines = fmt.fmt_cetus(*cycles)
        return Reply(title, lines, footer=fmt.fmt_platform_footer(platform))

    @staticmethod
    def _rotation_expiry(data: dict) -> str:
        """轮换表（信条 / 终幕等）的下一次重置时间（ISO）。"""
        from datetime import datetime, timedelta, timezone
        try:
            epoch = datetime.fromisoformat(data["epoch"])
            period = timedelta(hours=int(data.get("period_hours", 96)))
            now = datetime.now(timezone.utc)
            passed = int((now - epoch) // period)
            return (epoch + period * (passed + 1)).isoformat()
        except (KeyError, TypeError, ValueError):
            return ""

    @staticmethod
    def _weekly_reset() -> str:
        """每周重置锚点（周一 00:00 UTC）—— 灵化回廊 / 言录使等周常的到期点。"""
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        base = (now - timedelta(days=now.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0)
        if base <= now:
            base += timedelta(days=7)
        return base.isoformat()

    async def _h_timers(self, parsed, event, platform) -> Reply:
        # worldState 是整包缓存的，这里多取几个键几乎不加延迟
        res = await self._gather([
            self.client.cycle(platform, "cetus"), self.client.cycle(platform, "vallis"),
            self.client.cycle(platform, "cambion"), self.client.cycle(platform, "duviri"),
            self.client.cycle(platform, "zariman"),
            self.client.arbitration(platform), self.client.sortie(platform),
            self.client.void_trader(platform), self.client.archon_hunt(platform),
            self.client.steel_path(platform), self.client.nightwave(platform),
            self.client.calendar(platform), self.client.deep_archimedea(platform),
            self.client.temporal_archimedea(platform),
        ])
        names = ["夜灵平野", "奥布山谷", "魔胎之境", "双衍王境", "扎里曼派系",
                 "仲裁", "每日突击", "虚空奸商", "执刑官猎杀", "钢铁侵蚀",
                 "午夜电波", "1999日历", "深层科研", "时光科研"]
        # ★ 不许静默丢行（2026-09-25）：源恒抛/未下发（如 DE 源没有的
        #   仲裁、钢铁侵蚀）也要把 None 传给 formatter，由它显式打
        #   「暂无时效数据（源未下发）」；旧写法 isinstance 过滤会让这两行
        #   从卡面里静默消失（用户以为看全了）。
        timers = [(n, r if isinstance(r, dict) else None) for n, r in zip(names, res)]
        timers.append(("沉沦之地", await self.client.descendia(platform)))
        # 本地可推算的确定性轮换（不占网络请求）
        # ★ 数据源三层（2026-09-28 裁定）：① 运行期副本 plugin_data/<插件>/rotations.json
        #   （自动刷新回写的新数据）→ ② 包内种子 core/data/rotations.json（read_path 内置回退）
        #   → ③ 异常时退回空 dict（下面的 except）。旧实现直接读包内常量 ⇒
        #   运行期刷新**到不了卡面**、换批只能靠发版。
        rot = {}
        try:
            rot = json.loads(core_paths.read_path("rotations.json").read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            rot = {}
        for key, name in (("incarnon", "钢铁回廊灵化"), ("tenet", "信条元素加成"),
                          ("coda", "终幕换批")):
            exp = self._rotation_expiry(rot.get(key) or {})
            if exp:
                timers.append((name, {"expiry": exp}))
        timers.append(("周常重置", {"expiry": self._weekly_reset()}))
        title, lines = fmt.fmt_timers(timers)
        return Reply(title, lines, footer=fmt.fmt_platform_footer(platform))
    async def _h_fissures(self, parsed, event, platform) -> Reply:
        content = parsed.content_str
        if parsed.preset:
            content = (parsed.preset + " " + content).strip()
        flt = parse_fissure_filter(content) if content else None
        # ★ 2026-10-03：查询侧与「蹲」共用同一解析器（有意支持 T1–T6），
        #   档位写法越界同样当场提示（不静默）。
        _t_hint = fissure_tier_hint(content) if content else ""
        data = await self.client.fissures(platform)
        title, lines = fmt.fmt_fissures(
            data, flt=flt.match if flt else None,
            page=parsed.page, page_size=self.page_size, all_rows=True)
        if _t_hint:
            lines.append(_t_hint)
        extra = f"筛选：{flt.describe()}" if flt else ""
        return Reply(title, lines, footer=fmt.fmt_platform_footer(platform, extra))

    async def _h_sortie(self, parsed, event, platform) -> Reply:
        title, lines = fmt.fmt_sortie(await self.client.sortie(platform))
        return Reply(title, lines, footer=fmt.fmt_platform_footer(platform))

    async def _h_archon(self, parsed, event, platform) -> Reply:
        title, lines = fmt.fmt_archon(await self.client.archon_hunt(platform))
        return Reply(title, lines, footer=fmt.fmt_platform_footer(platform))

    async def _h_voidtrader(self, parsed, event, platform) -> Reply:
        """虚空商人（Baro Ki'Teer）：当前库存 / 下期预测。

        「奸商 预测」给出**基于 wiki 历史到访记录的候选排序**（统计推测，
        非官方 —— DE 不公布下期库存），口径写在卡面上。
        """
        toks = [str(t).lower() for t in (parsed.content or [])]
        if any(t in ("预测", "predict") for t in toks):
            rows = baro.predict(8)
            title, lines = fmt.fmt_baro_predict(
                rows, baro.next_visit_est() or "",
                len(baro.visits()), baro.last_visit() or "",
                names_zh=baro.names_zh())
            return Reply(title, lines,
                         footer=fmt.fmt_platform_footer(platform, "wiki 历史统计"))
        title, lines = fmt.fmt_void_trader(await self.client.void_trader(platform))
        if baro.visits():
            lines.append(f"※ 想看下期可能卖什么：发「奸商 预测」"
                         f"（基于 wiki 的 {len(baro.visits())} 次到访统计）")
        return Reply(title, lines, footer=fmt.fmt_platform_footer(platform))

    async def _h_dailydeals(self, parsed, event, platform) -> Reply:
        title, lines = fmt.fmt_daily_deals(await self.client.daily_deals(platform))
        return Reply(title, lines, footer=fmt.fmt_platform_footer(platform))
    async def _h_alerts(self, parsed, event, platform) -> Reply:
        title, lines = fmt.fmt_alerts(await self.client.alerts(platform))
        return Reply(title, lines, footer=fmt.fmt_platform_footer(platform))

    async def _h_invasions(self, parsed, event, platform) -> Reply:
        inv = await self.client.invasions(platform)
        title, lines = fmt.fmt_invasions(inv, page=parsed.page)
        return Reply(title, lines, footer=fmt.fmt_platform_footer(platform))

    async def _h_nightwave(self, parsed, event, platform) -> Reply:
        title, lines = fmt.fmt_nightwave(await self.client.nightwave(platform))
        return Reply(title, lines, footer=fmt.fmt_platform_footer(platform))

    async def _h_news(self, parsed, event, platform) -> Reply:
        title, lines = fmt.fmt_news(await self.client.news(platform))
        return Reply(title, lines, footer=fmt.fmt_platform_footer(platform))

    async def _h_kuva(self, parsed, event, platform) -> Reply:
        title, lines = fmt.fmt_kuva(await self.client.kuva(platform))
        return Reply(title, lines, footer=fmt.fmt_platform_footer(platform))

    async def _h_synth(self, parsed, event, platform) -> Reply:
        data = await self.client.synth_targets(platform)
        title, lines = fmt.fmt_synth_targets(data)
        return Reply(title, lines, footer=fmt.fmt_platform_footer(platform))

    async def _h_construction(self, parsed, event, platform) -> Reply:
        data = await self.client.construction(platform)
        title, lines = fmt.fmt_construction(data)
        return Reply(title, lines, footer=fmt.fmt_platform_footer(platform))

    # ------------------------------------------------------------------
    # 新增世界状态：九重天 / 活动 / 武形秘仪 / 阿耶兑换 / 氏族奖励 / 商城折扣
    # ------------------------------------------------------------------
    async def _h_voidstorms(self, parsed, event, platform) -> Reply:
        title, lines = fmt.fmt_void_storms(await self.client.void_storms(platform))
        return Reply(title, lines, footer=fmt.fmt_platform_footer(platform))

    async def _h_events(self, parsed, event, platform) -> Reply:
        title, lines = fmt.fmt_events(await self.client.events(platform))
        return Reply(title, lines, footer=fmt.fmt_platform_footer(platform))

    async def _h_conclave(self, parsed, event, platform) -> Reply:
        title, lines = fmt.fmt_conclave(await self.client.conclave(platform))
        return Reply(title, lines, footer=fmt.fmt_platform_footer(platform))

    async def _h_primevault(self, parsed, event, platform) -> Reply:
        """「出库」与「阿耶」共用本 handler，按**用户实际发出的词**分流。

        · 出库 / 御品            → 按 战甲 / 武器 / 守护 分组，只列整套名
        · 阿耶 / 御品阿耶 / 阿耶兑换 → 带价格的完整兑换表（可翻页）

        此前两者输出完全相同，维护者 2026-09-17 反馈「出库和阿耶为什么
        是一样的」，故按 ``command_raw`` 拆开。
        """
        vault = await self.client.prime_vault(platform)
        trigger = (parsed.command_raw or "").strip()
        if trigger in ("出库", "御品"):
            title, lines = fmt.fmt_prime_vault_list(vault)
        else:
            title, lines = fmt.fmt_prime_vault(vault, page=parsed.page)
        return Reply(title, lines, footer=fmt.fmt_platform_footer(platform))

    async def _h_clanrewards(self, parsed, event, platform) -> Reply:
        title, lines = fmt.fmt_clan_rewards(await self.client.clan_rewards(platform))
        return Reply(title, lines, footer=fmt.fmt_platform_footer(platform))

    async def _h_flashsales(self, parsed, event, platform) -> Reply:
        sales = await self.client.flash_sales(platform)
        title, lines = fmt.fmt_flash_sales(sales, page=parsed.page)
        return Reply(title, lines, footer=fmt.fmt_platform_footer(platform))

