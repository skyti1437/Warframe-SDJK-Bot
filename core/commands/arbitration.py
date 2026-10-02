# -*- coding: utf-8 -*-
"""仲裁指令（D3 自 main.py 迁入，方法体逐字未改）。

覆盖路由键 2 项：arbitration / arbtable；类属性 _ARB_TYPES / _ARB_RATING /
_ARB_TYPES_STR 随域迁出（经 Mixin MRO 供 self._ARB_* 寻址，取值不变）。
附：D2 区间尾界曾误切本域两行筛选关键词注释，本笔自 D1 提交原文恢复。
子包纪律：不 import astrbot（事件对象鸭子类型）。
"""
from __future__ import annotations

import json

from .. import arbi as _arbi
from .. import formatters as fmt
from .base import PLUGIN_DIR, Reply

# 仲裁的任务类型 / 派系 / 节点渲染：**唯一实现**在 core/arbi.py
# （「仲裁」查询指令与「蹲 仲裁」推送共用；2026-09-19 收敛，避免两套口径漂移）。
_arb_mission = _arbi.mission_of
_arb_faction = _arbi.faction_of
_ARB_FACTION_FIX = _arbi._FACTION_FIX   # 兼容旧引用


class ArbitrationCommands:
    """Mixin：仲裁 handler（挂载于 main.WarframeSDJK）。"""

    # 仲裁筛选关键词 -> arbi.wf.wiki 的 missionNameZh 规范值
    # （该站中文表把 Infested Salvage 写作「INFESTED 资源回收」，比对前需去前缀）
    _ARB_TYPES = {
        "生存": "生存", "防御": "防御", "镜像防御": "镜像防御",
        "拦截": "拦截", "挖掘": "挖掘", "叛逃": "叛逃",
        "回收": "资源回收", "资源回收": "资源回收",
        "中断": "中断", "歼灭": "歼灭", "捕获": "捕获",
        "虚空洪流": "虚空洪流", "虚空覆涌": "虚空覆涌", "虚空决战": "虚空决战",
        "联结生存": "联结生存", "元素转换": "元素转换",
    }
    _ARB_RATING = {"高效": ("S", "A+", "A"), "传奇": ("S",)}
    _ARB_TYPES_STR = "生存 / 防御 / 镜像防御 / 拦截 / 挖掘 / 叛逃 / 回收 / 中断 /" \
                     " 歼灭 / 捕获 / 虚空洪流 / 虚空覆涌 / 虚空决战 / 联结生存 / 元素转换"

    async def _h_arbitration(self, parsed, event, platform) -> Reply:
        """当前仲裁 / 按类型筛选未来排期（arbi.wf.wiki 官方数据）。"""
        import time as _t
        from datetime import datetime, timezone, timedelta

        toks = list(parsed.content or [])
        if parsed.preset:
            toks.insert(0, parsed.preset)
        want_types = {self._ARB_TYPES[t] for t in toks if t in self._ARB_TYPES}
        want_ratings = next((self._ARB_RATING[t] for t in toks
                             if t in self._ARB_RATING), ())
        today_only = any(t in ("今天", "今日") for t in toks)
        filtered = bool(want_types or want_ratings or today_only)

        if not filtered:      # 默认：当前 + 下一小时
            return await self._arb_now(platform)

        sched, nodes, tier_of = await self._arb_fetch()
        seq, step, start = sched["seq"], sched.get("stepSec", 3600), sched["startTs"]
        idx0 = int((_t.time() - start) // step)
        bj = timezone(timedelta(hours=8))
        now_bj = datetime.now(bj)
        end_bj = now_bj.replace(hour=23, minute=59, second=59) if today_only \
            else now_bj + timedelta(days=14)
        rows = []
        for h in range(0, min(24 * 15, len(seq) - idx0)):
            key = sched["nodes"][seq[idx0 + h]]
            n = nodes.get(key, {})
            mt = _arb_mission(n)
            tv = tier_of.get(key, "")
            if tv == "未评级":
                tv = ""
            t = datetime.fromtimestamp(start + (idx0 + h) * step, tz=timezone.utc) \
                .astimezone(bj)
            if t > end_bj:
                break
            if want_types and mt not in want_types:
                continue
            if want_ratings and tv not in want_ratings:
                continue
            # ★ 2026-09-27 列对齐：排期卡改走渲染层的按列绘制 ⇒ 这里存**分列
            #   单元格**（[节点（星球）, 类型, 派系, [评级]]），不再拼整行字符串。
            rows.append((t, _arbi.node_cells(nodes, key, tier_of), tv))
        if not rows:
            return Reply(raw_text=f"该筛选条件下未来 14 天内没有仲裁场次\n"
                                  f"可筛选类型：{self._ARB_TYPES_STR}\n"
                                  f"评级筛选：高效（S/A+/A） / 传奇（S）")
        page_size = self.page_size
        total = len(rows)
        pages = max(1, (total + page_size - 1) // page_size)
        page = max(1, min(parsed.page or 1, pages))
        chunk = rows[(page - 1) * page_size: page * page_size]
        # 时间列 + 节点/类型/派系/评级 各占一列（全角空格分隔，渲染层按列对齐）
        lines = ["　".join([f"{t.month}月{t.day}日 {t.hour:02d}时", *cells])
                 for t, cells, _tv in chunk]
        lines.append("※ 每行格式：时间 · 节点（星球） · 任务类型 · 派系 · [站点评级]")
        lines.append("※ 评级来源 arbi.wf.wiki 社区评级；排期为确定性序列，非随机")
        fdesc = "、".join([*(t for t in toks if t in self._ARB_TYPES),
                           *(("高效" if want_ratings == self._ARB_RATING["高效"] else
                              "传奇") for _ in [0] if want_ratings),
                           *(["今天"] if today_only else [])]) or "全部"
        title = f"仲裁排期 · {fdesc}（第{page}/{pages}页，共{total}场）"
        return Reply(title, lines, footer=fmt.fmt_platform_footer(platform, "arbi.wf.wiki"))

    async def _arb_fetch(self) -> tuple[dict, dict, dict]:
        """拉 arbi.wf.wiki 三张表：排期序列 / 节点中文表 / 站点评级。

        数据源说明（用户可核对）：
          · arbys.schedule.v2.json —— 确定性的逐小时排期（官方数据推导，非随机）
          · arbys.nodes.zh.json    —— 节点/星球/任务类型/派系/等级的中文名
          · tierlist.default.json  —— 社区站点评级（S/A+/A/A-/B/C）
        """
        return await _arbi.fetch_tables(self.client)

    @staticmethod
    def _arb_node_line(nodes: dict, key: str, tier_of: dict) -> str:
        """把节点渲染成一行：节点（星球） · 类型 · 派系 · 评级。

        Args:
            nodes: 节点字典（key -> 节点数据）。
            key: 目标节点 key。
            tier_of: 节点 -> 评级。

        Notes:
            arbi 数据里的 ``minEnemyLevel/maxEnemyLevel`` 是**敌人等级区间**（不是
            「原始难度」之类的概念 —— 仲裁根本没有那种说法），但很多用户看着
            「Lv 6-11」会以为和之前那条「高效 / 传奇」筛选是一回事，反而起干扰，
            砍掉。
        """
        return _arbi.node_line(nodes, key, tier_of)
    async def _arb_now(self, platform) -> Reply:
        """当前仲裁 + 下一小时（含节点/星球/类型/派系/等级/站点评级 + 数据源说明）。"""
        import time as _t
        sched, nodes, tier_of = await self._arb_fetch()
        seq = sched["seq"]
        start, step = sched["startTs"], sched.get("stepSec", 3600)
        idx = int((_t.time() - start) // step) % len(seq)
        key_now = sched["nodes"][seq[idx]]
        key_nxt = sched["nodes"][seq[(idx + 1) % len(seq)]]
        n = nodes.get(key_now) or {}
        eff = ""

        # 生息效率数值：本地社区实测均值（非官方），明确标注来源
        ratings = {}
        try:
            ratings = json.loads((PLUGIN_DIR / "core" / "data" / "arb_ratings.json")
                                 .read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
        en_type = (n.get("missionType") or "").replace("MT_", "").title()
        rec = ratings.get(f"{n.get('nameZh', '')}|{en_type}")
        eff = f"　生息效率 {rec[0]}/小时（社区实测 {rec[1]} 档）" if rec else ""

        lines = [f"节点：{n.get('nameZh', '?')}"
                 + (f"（{n.get('systemNameZh')}）" if n.get("systemNameZh") else ""),
                 f"类型：{_arb_mission(n)} · 派系："
                 f"{_arb_faction(n) or '?'}",
                 "评级：" + (tier_of.get(key_now) or "未评级")
                 + "（arbi.wf.wiki 社区评级）" + eff,
                 "下一小时："
                 + self._arb_node_line(nodes, key_nxt, tier_of),
                 f"筛选：仲裁 {self._ARB_TYPES_STR} / 高效 / 传奇 / 今天"]
        lines.append("※ 排期·节点·派系·等级·评级：arbi.wf.wiki（社区维护的确定性序列）")
        lines.append("※ 生息效率：社区实测均值表 core/data/arb_ratings.json，非官方数据，仅供参考")
        return Reply("当前仲裁", lines, footer=fmt.fmt_platform_footer(platform))

    async def _h_arbtable(self, parsed, event, platform) -> Reply:
        """仲裁时间表：arbi.wf.wiki 官方数据（中文节点+派系+站点评级）。"""
        import time as _t
        from datetime import datetime, timezone, timedelta
        base = "https://arbi.wf.wiki/data/"
        sched = await self.client._fetch_json(base + "arbys.schedule.v2.json", ttl=3600)
        nodes = (await self.client._fetch_json(base + "arbys.nodes.zh.json", ttl=86400))["nodes"]
        tier = await self.client._fetch_json(base + "tierlist.default.json", ttl=86400)
        node_tier = {}
        for t_name, lst in tier.get("tierBuckets", {}).items():
            for nk in lst:
                node_tier[nk] = t_name
        start, step = sched["startTs"], sched.get("stepSec", 3600)
        seq = sched["seq"]
        idx0 = int((_t.time() - start) // step)
        rows = []
        for h in range(0, min(24 * 14, len(seq) - idx0)):  # 逐小时，14 天
            key = sched["nodes"][seq[idx0 + h]]
            n = nodes.get(key, {})
            name = n.get("nameZh", "?")
            system = n.get("systemNameZh", "")
            mtype = _arb_mission(n)
            fac = _arb_faction(n)
            tv = node_tier.get(key, "")
            if tv == "未评级":
                tv = ""
            t = datetime.fromtimestamp(start + (idx0 + h) * step,
                                       tz=timezone.utc) + timedelta(hours=8)
            # ★ 2026-10-02 裂列修复：存**结构化单元格**，输出时才按全角空格拼行。
            #   不变量：渲染层分列口径（render.py 的 _split_cells / 预扫描 /
            #   _table_mode 列对齐）**只认全角空格 `　`** ⇒ 单元格内允许 ASCII
            #   空格，但绝不可把 ASCII 空格当列分隔符。旧写法「ASCII 空格 join →
            #   split」会把含空格的节点名（V Prime / Tyana Pass / Outer Terminus，
            #   88 个仲裁节点中 3 个）劈成两列，其后各列整体右移、末列溢出。
            rows.append((t, name, system, mtype, fac, tv))
        size = 15
        total = len(rows)
        pages = max(1, (total + size - 1) // size)
        page = min(max(1, parsed.page or 1), pages)
        page_rows = rows[(page - 1) * size: page * size]
        # 时间 + 节点 + 星球 + 类型 + 派系 + [评级]：节点与星球**各占一列**
        # （不合并成「V Prime（金星）」——合并列是「仲裁排期」卡的写法）
        chunk = ["　".join(c for c in (f"{t.day}日{t.hour:02d}时",
                                       name, system, mtype, fac, tv) if c)
                 for t, name, system, mtype, fac, tv in page_rows]
        title = f"仲裁时间表（第{page}/{pages}页，共{total}条）"
        return Reply(title, chunk, footer=fmt.fmt_platform_footer(platform))

