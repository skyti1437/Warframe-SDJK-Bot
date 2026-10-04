# -*- coding: utf-8 -*-
"""仲裁指令（D3 自 main.py 迁入，方法体逐字未改）。

覆盖路由键 2 项：arbitration / arbtable；类属性 _ARB_TYPES / _ARB_RATING /
_ARB_TYPES_STR 随域迁出（经 Mixin MRO 供 self._ARB_* 寻址，取值不变）。
附：D2 区间尾界曾误切本域两行筛选关键词注释，本笔自 D1 提交原文恢复。
子包纪律：不 import astrbot（事件对象鸭子类型）。
"""

from __future__ import annotations


from .. import arbi as _arbi
from .. import formatters as fmt
from .base import Reply

# 仲裁的任务类型 / 派系 / 节点渲染：**唯一实现**在 core/arbi.py
# （「仲裁」查询指令与「蹲 仲裁」推送共用；2026-09-19 收敛，避免两套口径漂移）。
_arb_mission = _arbi.mission_of
_arb_faction = _arbi.faction_of
_ARB_FACTION_FIX = _arbi._FACTION_FIX  # 兼容旧引用


class ArbitrationCommands:
    """Mixin：仲裁 handler（挂载于 main.WarframeSDJK）。"""

    # 仲裁筛选关键词 -> arbi.wf.wiki 的 missionNameZh 规范值
    # （该站中文表把 Infested Salvage 写作「INFESTED 资源回收」，比对前需去前缀）
    _ARB_TYPES = {
        "生存": "生存",
        "防御": "防御",
        "镜像防御": "镜像防御",
        "拦截": "拦截",
        "挖掘": "挖掘",
        "叛逃": "叛逃",
        "回收": "资源回收",
        "资源回收": "资源回收",
        "中断": "中断",
        "歼灭": "歼灭",
        "捕获": "捕获",
        "虚空洪流": "虚空洪流",
        "虚空覆涌": "虚空覆涌",
        "虚空决战": "虚空决战",
        "联结生存": "联结生存",
        "元素转换": "元素转换",
    }
    _ARB_RATING = {"高效": ("S", "A+", "A"), "传奇": ("S",)}
    _ARB_TYPES_STR = (
        "生存 / 防御 / 镜像防御 / 拦截 / 挖掘 / 叛逃 / 回收 / 中断 /"
        " 歼灭 / 捕获 / 虚空洪流 / 虚空覆涌 / 虚空决战 / 联结生存 / 元素转换"
    )

    async def _h_arbitration(self, parsed, event, platform) -> Reply:
        """当前仲裁 / 按类型筛选未来排期（arbi.wf.wiki 官方数据）。"""
        import time as _t
        from datetime import datetime, timezone, timedelta

        toks = list(parsed.content or [])
        if parsed.preset:
            toks.insert(0, parsed.preset)
        want_types = {self._ARB_TYPES[t] for t in toks if t in self._ARB_TYPES}
        want_ratings = next((self._ARB_RATING[t] for t in toks if t in self._ARB_RATING), ())
        today_only = any(t in ("今天", "今日") for t in toks)
        filtered = bool(want_types or want_ratings or today_only)

        if not filtered:  # 默认：当前 + 下一小时
            return await self._arb_now(platform)

        sched, nodes, tier_of = await self._arb_fetch()
        seq, step, start = sched["seq"], sched.get("stepSec", 3600), sched["startTs"]
        idx0 = int((_t.time() - start) // step)
        bj = timezone(timedelta(hours=8))
        now_bj = datetime.now(bj)
        end_bj = (
            now_bj.replace(hour=23, minute=59, second=59)
            if today_only
            else now_bj + timedelta(days=14)
        )
        rows = []
        for h in range(0, min(24 * 15, len(seq) - idx0)):
            key = sched["nodes"][seq[idx0 + h]]
            n = nodes.get(key, {})
            mt = _arb_mission(n)
            tv = tier_of.get(key, "")
            if tv == "未评级":
                tv = ""
            t = datetime.fromtimestamp(start + (idx0 + h) * step, tz=timezone.utc).astimezone(bj)
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
            return Reply(
                raw_text=f"该筛选条件下未来 14 天内没有仲裁场次\n"
                f"可筛选类型：{self._ARB_TYPES_STR}\n"
                f"评级筛选：高效（S/A+/A） / 传奇（S）"
            )
        page_size = self.page_size
        total = len(rows)
        pages = max(1, (total + page_size - 1) // page_size)
        page = max(1, min(parsed.page or 1, pages))
        chunk = rows[(page - 1) * page_size : page * page_size]
        # 时间列 + 节点/类型/派系/评级 各占一列（全角空格分隔，渲染层按列对齐）
        lines = [
            "　".join([f"{t.month}月{t.day}日 {t.hour:02d}时", *cells]) for t, cells, _tv in chunk
        ]
        lines.append("※ 每行格式：时间 · 节点（星球） · 任务类型 · 派系 · [站点评级]")
        lines.append(
            "※ 评级：arbi.wf.wiki 官方 tierlist 优先；官方未评级的节点用"
            "「社区实测中位数」补（S≥800/A+≥700/A≥600/A-≥500/F<500 每小时生息）"
        )
        lines.append("※ ★ 实测值（尤其「生存 / 中断」）吃队伍熟练度 —— 普通队伍未必打得到该数值")
        fdesc = (
            "、".join(
                [
                    *(t for t in toks if t in self._ARB_TYPES),
                    *(
                        ("高效" if want_ratings == self._ARB_RATING["高效"] else "传奇")
                        for _ in [0]
                        if want_ratings
                    ),
                    *(["今天"] if today_only else []),
                ]
            )
            or "全部"
        )
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

        # ★ 2026-10-04 修 bug：实测表改按**节点 ID** 查。
        #   旧写法 `f"{nameZh}|{missionType.replace('MT_','').title()}"` 拼 key ——
        #   arbi 的 missionType 是内部代号（MT_TERRITORY/MT_PURIFY/MT_ARTIFACT…），
        #   与旧表的英文类型名（Interception/Infested Salvage/Disruption…）对不上
        #   ⇒ 17 个节点的「生息效率」行**永远不显示**（命中率仅 44/88）。
        #   实测表与 schedule/nodes/tierlist 三表同源同键（SolNodeXXX/ClanNodeXX）。
        tv = (tier_of.get(key_now) or "").strip()
        mrec = _arbi.measured_of(key_now)
        src = _arbi.tier_source(key_now, tier_of)
        if tv in ("", "未评级"):
            rate = "评级：未评级"
        elif src == "arbi":
            rate = f"评级：{tv}（arbi 社区评级）"
        else:
            rate = f"评级：{tv}（社区实测中位）"
        if mrec.get("median") is not None:
            rate += f"　生息效率 {mrec['median']}/小时（n={mrec.get('n', 0)}，社区实测中位数）"

        lines = [
            f"节点：{n.get('nameZh', '?')}"
            + (f"（{n.get('systemNameZh')}）" if n.get("systemNameZh") else ""),
            f"类型：{_arb_mission(n)} · 派系：{_arb_faction(n) or '?'}",
            rate,
            "下一小时：" + self._arb_node_line(nodes, key_nxt, tier_of),
            f"筛选：仲裁 {self._ARB_TYPES_STR} / 高效 / 传奇 / 今天",
        ]
        lines.append("※ 排期·节点·派系·等级：arbi.wf.wiki（社区维护的确定性序列）")
        lines.append(
            "※ 评级：arbi 官方 tierlist 优先；官方未评级的用「社区实测中位数」"
            "（arbi.wf.wiki 排行榜聚合，非官方，仅供参考）"
        )
        lines.append(
            "※ ★ 实测值（尤其「生存 / 中断」）吃队伍熟练度 —— 来自上传记录的高水平队伍，"
            "普通队伍未必打得到该数值"
        )
        return Reply("当前仲裁", lines, footer=fmt.fmt_platform_footer(platform))

    async def _h_arbtable(self, parsed, event, platform) -> Reply:
        """仲裁时间表：arbi.wf.wiki 数据（中文节点+派系+站点评级，含实测回落）。"""
        import time as _t
        from datetime import datetime, timezone, timedelta

        # ★ 2026-10-04：改走共用入口 `_arb_fetch()` —— 它返回**三级回落**后的
        #   tier_of（arbi 官方 → 社区实测中位 → 未评级）。此前这里自己拉三张表，
        #   实测档回落会漏掉这张卡（指令 §2.4 的"自动受益"假设在此处不成立）。
        sched, nodes, node_tier = await self._arb_fetch()
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
            t = datetime.fromtimestamp(start + (idx0 + h) * step, tz=timezone.utc) + timedelta(
                hours=8
            )
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
        page_rows = rows[(page - 1) * size : page * size]
        # 时间 + 节点 + 星球 + 类型 + 派系 + [评级]：节点与星球**各占一列**
        # （不合并成「V Prime（金星）」——合并列是「仲裁排期」卡的写法）
        chunk = [
            "　".join(c for c in (f"{t.day}日{t.hour:02d}时", name, system, mtype, fac, tv) if c)
            for t, name, system, mtype, fac, tv in page_rows
        ]
        title = f"仲裁时间表（第{page}/{pages}页，共{total}条）"
        return Reply(title, chunk, footer=fmt.fmt_platform_footer(platform))
