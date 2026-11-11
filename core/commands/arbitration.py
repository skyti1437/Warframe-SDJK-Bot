# -*- coding: utf-8 -*-
"""仲裁指令（D3 自 main.py 迁入，方法体逐字未改）。

覆盖路由键 2 项：arbitration / arbtable；类属性 _ARB_TYPES / _ARB_RATING /
_ARB_TYPES_STR 随域迁出（经 Mixin MRO 供 self._ARB_* 寻址，取值不变）。
附：D2 区间尾界曾误切本域两行筛选关键词注释，本笔自 D1 提交原文恢复。
子包纪律：不 import astrbot（事件对象鸭子类型）。
"""

from __future__ import annotations


import re
from datetime import datetime, timezone

from .. import arbi as _arbi
from .. import de_worldstate as _dew
from .. import formatters as fmt
from ..display_time import local_datetime, offset_label
from ..i18n import L, is_en
from .base import Reply, err_card

# 仲裁的任务类型 / 派系 / 节点渲染：**唯一实现**在 core/arbi.py
# （「仲裁」查询指令与「蹲 仲裁」推送共用；2026-09-19 收敛，避免两套口径漂移）。
_arb_mission = _arbi.mission_of
_arb_faction = _arbi.faction_of
_ARB_FACTION_FIX = _arbi._FACTION_FIX  # 兼容旧引用


# 英文筛选写法 → 中文内部键（只在仲裁 / 仲裁表的参数解析处认，不进通用别名表）
_ARB_EN_PHRASES = {
    "mirror defense": "镜像防御",
    "void flood": "虚空洪流",
    "void cascade": "虚空覆涌",
    "void armageddon": "虚空决战",
    "conjunction survival": "联结生存",
}
_ARB_EN_WORDS = {
    "survival": "生存",
    "defense": "防御",
    "interception": "拦截",
    "excavation": "挖掘",
    "defection": "叛逃",
    "salvage": "资源回收",
    "disruption": "中断",
    "exterminate": "歼灭",
    "extermination": "歼灭",
    "capture": "捕获",
    "alchemy": "元素转换",
    "efficient": "高效",
    "legendary": "传奇",
    "today": "今天",
}
# 中文内部键 → 英文展示名（筛选说明用）
_ARB_ZH_EN = {
    **{v: k.title() for k, v in _ARB_EN_PHRASES.items()},
    **{v: k.title() for k, v in _ARB_EN_WORDS.items() if k not in ("extermination",)},
}
_ARB_TYPES_STR_EN = (
    "survival / defense / mirror defense / interception / excavation / defection / salvage / "
    "disruption / exterminate / capture / void flood / void cascade / void armageddon / "
    "conjunction survival / alchemy"
)
_TIER_NOTE_EN = (
    "※ Rating: the official arbi.wf.wiki tierlist comes first; nodes it leaves unrated use the "
    "community measured median; score ranges S≥800 / A+≥700 / A≥600 / A-≥450 / F＜450 (per hour)"
)


def _arb_en_toks(toks: list) -> list:
    """英文筛选词并入中文内部键（两词短语先合并）；中文词原样保留。"""
    out: list = []
    i = 0
    toks = [str(t) for t in toks]
    while i < len(toks):
        if i + 1 < len(toks):
            pair = f"{toks[i]} {toks[i + 1]}".lower()
            if pair in _ARB_EN_PHRASES:
                out.append(_ARB_EN_PHRASES[pair])
                i += 2
                continue
        out.append(_ARB_EN_WORDS.get(toks[i].lower(), toks[i]))
        i += 1
    return out


def _arb_en_node(nodes: dict, key: str) -> tuple[str, str, str, str]:
    """英文模式的节点四元组 (节点, 星球, 任务类型, 派系)。

    arbi 的节点表只有中文；节点键与 DE solNodes 同一套（SolNodeNNN），英文模式直接取
    DE 官方英文表。表里查不到的键（ClanNode 之类）回落 arbi 原值。
    """
    n = nodes.get(key) or {}
    info = _dew.sol_node(key)
    if not info:
        return (
            n.get("nameZh") or "?",
            n.get("systemNameZh") or "",
            _arb_mission(n),
            _arb_faction(n),
        )
    full = _dew._node_name(key)
    m = re.match(r"^(.*?)\s*\((.*)\)$", full)
    name, system = (m.group(1), m.group(2)) if m else (full, "")
    enemy = (info.get("enemy") or "").strip()
    faction = "Infested" if enemy == "Infestation" else enemy
    return name, system, info.get("type") or _arb_mission(n), faction


def _arb_cells(nodes: dict, key: str, tier_of: dict, *, with_median: bool = False) -> list[str]:
    """节点分列单元格：中文沿用 arbi.node_cells，英文换成 DE 官方英文名。"""
    cells = _arbi.node_cells(nodes, key, tier_of, with_median=with_median)
    if not is_en():
        return cells
    name, system, mission, faction = _arb_en_node(nodes, key)
    return [f"{name} ({system})" if system else name, mission, faction, *cells[3:]]


def _arb_clock(t: datetime, show_offset: bool = False) -> str:
    stamp = L(f"{t.month}月{t.day}日 {t:%H:%M}", f"{t:%b} {t.day} {t:%H:%M}")
    return f"{stamp} {offset_label(t)}" if show_offset else stamp


def _arb_wait_fix() -> str:
    return L(
        "稍后再查，等数据源更新排期表后恢复",
        "Check again later, once the data source updates the schedule",
    )


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

        toks = list(parsed.content or [])
        if parsed.preset:
            toks.insert(0, parsed.preset)
        toks = _arb_en_toks(toks)
        want_types = {self._ARB_TYPES[t] for t in toks if t in self._ARB_TYPES}
        want_ratings = next((self._ARB_RATING[t] for t in toks if t in self._ARB_RATING), ())
        today_only = any(t in ("今天", "今日") for t in toks)
        filtered = bool(want_types or want_ratings or today_only)

        if not filtered:  # 默认：当前 + 下一小时
            return await self._arb_now(platform)

        sched, nodes, tier_of = await self._arb_fetch()
        seq, step, start = sched["seq"], sched.get("stepSec", 3600), sched["startTs"]
        now = _t.time()
        idx0 = int((now - start) // step)
        if idx0 < 0 or idx0 >= len(seq):
            return err_card(
                _arbi.expired_hint(sched),
                _arb_wait_fix(),
                title=L("仲裁排期", "Arbitration Schedule"),
            )
        today = local_datetime(now).date()
        rows = []
        for h in range(0, min(24 * 15, len(seq) - idx0)):
            stamp = start + (idx0 + h) * step
            t = local_datetime(stamp)
            if (today_only and t.date() > today) or stamp > now + 14 * 86400:
                break
            if today_only and t.date() < today:
                continue
            key = sched["nodes"][seq[idx0 + h]]
            n = nodes.get(key, {})
            mt = _arb_mission(n)
            tv = tier_of.get(key, "")
            if tv == "未评级":
                tv = ""
            if want_types and mt not in want_types:
                continue
            if want_ratings and tv not in want_ratings:
                continue
            # ★ 2026-09-27 列对齐：排期卡改走渲染层的按列绘制 ⇒ 这里存**分列
            #   单元格**（[节点（星球）, 类型, 派系, [评级]]），不再拼整行字符串。
            rows.append((t, _arb_cells(nodes, key, tier_of), tv))
        if not rows:
            return err_card(
                L(
                    "该筛选条件下未来 14 天内没有仲裁场次",
                    "No arbitration slots match this filter in the next 14 days",
                ),
                L("放宽或更换筛选词重发", "Loosen or change the filter and resend"),
                (
                    L("可筛选类型", "Filterable types"),
                    L(self._ARB_TYPES_STR, _ARB_TYPES_STR_EN),
                ),
                (
                    L("评级筛选", "Rating filter"),
                    L("高效（S/A+/A） / 传奇（S）", "efficient (S/A+/A) / legendary (S)"),
                ),
                title=L("仲裁排期", "Arbitration Schedule"),
            )
        page_size = self.page_size
        total = len(rows)
        pages = max(1, (total + page_size - 1) // page_size)
        page = max(1, min(parsed.page or 1, pages))
        chunk = rows[(page - 1) * page_size : page * page_size]
        offsets = list(dict.fromkeys(offset_label(t) for t, _cells, _tv in chunk))
        zone = " / ".join(offsets)
        # 时间列 + 节点/类型/派系/评级 各占一列（全角空格分隔，渲染层按列对齐）
        _fparts = [
            *(t for t in toks if t in self._ARB_TYPES),
            *(
                ("高效" if want_ratings == self._ARB_RATING["高效"] else "传奇")
                for _ in [0]
                if want_ratings
            ),
            *(["今天"] if today_only else []),
        ]
        fdesc = L(
            "、".join(_fparts) or "全部",
            ", ".join(_ARB_ZH_EN.get(t, t) for t in _fparts) or "All",
        )
        S = "　"
        lines = [
            L(
                f"◆ 筛选：{fdesc}｜共 {total} 场（{zone}）",
                f"◆ Filter: {fdesc}｜{total} slots ({zone})",
            ),
            L("◆ 仲裁排期", "◆ Arbitration Schedule"),
            L(
                f"{S}时间{S}节点（星球）{S}类型{S}派系{S}评级",
                f"{S}Time{S}Node (Planet){S}Type{S}Faction{S}Rating",
            ),
            *["· " + S.join([_arb_clock(t, len(offsets) > 1), *cells]) for t, cells, _tv in chunk],
            L(_arbi.TIER_NOTE, _TIER_NOTE_EN),
            *(
                [
                    L(
                        _arbi.LOW_NOTE,
                        f'※ A rating marked "{_arbi.LOW_MARK}" = small sample: fewer than '
                        f"{_arbi.LOW_N} community records for this node, tier for reference only",
                    )
                ]
                if any(_arbi.LOW_MARK in "".join(c) for _t, c, _tv in chunk)
                else []
            ),
            L(
                "★ 实测值（尤其「生存 / 中断」）吃队伍熟练度，普通队伍未必打得到",
                "★ Measured values (especially Survival / Disruption) depend on squad skill; "
                "an average squad may not reach them",
            ),
        ]
        title = L(
            f"仲裁排期 · {fdesc}（第{page}/{pages}页，共{total}场）",
            f"Arbitration Schedule · {fdesc} (Page {page}/{pages}, {total} total)",
        )
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
        # ★ 2026-10-11：走 arbi.key_at —— 排期表到期后给提示，不再 `% len(seq)` 回绕到表头
        try:
            key_now, idx, start, step = _arbi.key_at(sched, _t.time())
        except _arbi.ScheduleExpired:
            return err_card(
                _arbi.expired_hint(sched), _arb_wait_fix(), title=L("仲裁", "Arbitration")
            )
        key_nxt = sched["nodes"][seq[idx + 1]] if idx + 1 < len(seq) else ""
        n = nodes.get(key_now) or {}
        t0 = local_datetime(start + idx * step)
        t1 = local_datetime(start + (idx + 1) * step)
        slot = f"{t0:%H:%M}–{t1:%H:%M} ({offset_label(t1)})"
        if offset_label(t0) != offset_label(t1):
            slot = f"{t0:%H:%M} {offset_label(t0)} – {t1:%H:%M} {offset_label(t1)}"

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
            rate = L("评级：未评级", "Rating: Unrated")
        elif src == "arbi":
            rate = L(f"评级：{tv}（arbi 社区评级）", f"Rating: {tv} (arbi community rating)")
        elif _arbi.low_sample(key_now, tier_of):
            rate = L(
                f"评级：{tv}（社区实测中位 · 样本少）",
                f"Rating: {tv} (community measured median · small sample)",
            )
        else:
            rate = L(f"评级：{tv}（社区实测中位）", f"Rating: {tv} (community measured median)")
        if mrec.get("median") is not None:
            rate += L(
                f"　生息效率 {mrec['median']}/小时（n={mrec.get('n', 0)}，社区实测中位数）",
                f"　Vitus Essence rate {mrec['median']}/h "
                f"(n={mrec.get('n', 0)}, community measured median)",
            )

        S = "　"
        # ★ 2026-10-22：下一小时的评级**带社区实测中位数**（与时间表同口径）。
        nxt_cells = _arb_cells(nodes, key_nxt, tier_of, with_median=True) if key_nxt else []
        left = fmt.countdown(t1.astimezone(timezone.utc).isoformat())
        # ★ 2026-10-22：新增「下次高效」—— 从**下一场起**往后找第一个 S/A+/A（与「仲裁 高效」
        #   同一口径）。下一场本身就是高效时就直接是它（与上一行重复，属预期）。
        _hit = _arbi.next_efficient(sched, tier_of, idx + 1)
        if not _hit:
            eff_line = L(
                "◆ 下次高效：排期内暂无（未来排期里没有 S / A+ / A 的场次）",
                "◆ Next Efficient: none in the schedule (no S / A+ / A slots ahead)",
            )
        else:
            _ei, _ek = _hit
            _et = local_datetime(start + _ei * step)
            _ecells = _arb_cells(nodes, _ek, tier_of, with_median=True)
            _eleft = fmt.countdown(
                datetime.fromtimestamp(start + _ei * step, tz=timezone.utc).isoformat()
            )
            eff_line = (
                L(
                    f"◆ 下次高效：{_arb_clock(_et, True)}",
                    f"◆ Next Efficient: {_arb_clock(_et, True)}",
                )
                + S.join(["", *_ecells])
                + L(f"｜还有 {_eleft}", "｜" + fmt._in_en(_eleft))
            )
        _sys = n.get("systemNameZh")
        _en_name, _en_sys, _en_mission, _en_faction = _arb_en_node(nodes, key_now)
        lines = [
            L(
                f"◆ 当前仲裁：{n.get('nameZh', '?')}"
                + (f"（{_sys}）" if _sys else "")
                + f"{S}{_arb_mission(n)}{S}{_arb_faction(n) or '?'}",
                f"◆ Current Arbitration: {_en_name}"
                + (f" ({_en_sys})" if _en_sys else "")
                + f"{S}{_en_mission}{S}{_en_faction or '?'}",
            ),
            f"◆ {rate}",
            L(
                f"◆ 本场时段：{slot}｜剩余 {left}",
                f"◆ This Slot: {slot}｜{fmt._left_en(left)}",
            ),
            L("◆ 下一小时：", "◆ Next Hour: ")
            + (
                S.join(nxt_cells[:4]) if nxt_cells else L("（排期表已到末尾）", "(end of schedule)")
            ),
            eff_line,
            L(
                "※ 筛选：发「仲裁 类型 / 高效 / 传奇 / 今天」，类型 " + self._ARB_TYPES_STR,
                '※ Filter: send "arbitration <type> / efficient / legendary / today"; types: '
                + _ARB_TYPES_STR_EN,
            ),
            L(
                _arbi.TIER_NOTE + "（实测非官方，仅供参考）",
                _TIER_NOTE_EN + " (measured, unofficial; for reference only)",
            ),
            *(
                [
                    L(
                        _arbi.LOW_NOTE,
                        f'※ A rating marked "{_arbi.LOW_MARK}" = small sample: fewer than '
                        f"{_arbi.LOW_N} community records for this node, tier for reference only",
                    )
                ]
                if nxt_cells and _arbi.LOW_MARK in "".join(nxt_cells)
                else []
            ),
            L(
                "★ 实测值（尤其「生存 / 中断」）来自上传记录的高水平队伍，普通队伍未必打得到",
                "★ Measured values (especially Survival / Disruption) come from high-level squads' "
                "uploaded runs; an average squad may not reach them",
            ),
        ]
        return Reply(
            L("当前仲裁", "Current Arbitration"),
            lines,
            footer=fmt.fmt_platform_footer(
                platform,
                L("排期·节点·等级 arbi.wf.wiki", "Schedule · Nodes · Tiers arbi.wf.wiki"),
            ),
        )

    async def _h_arbtable(self, parsed, event, platform) -> Reply:
        """仲裁时间表：arbi.wf.wiki 数据（中文节点+派系+站点评级，含实测回落）。"""
        import time as _t

        # ★ 2026-10-04：改走共用入口 `_arb_fetch()` —— 它返回**三级回落**后的
        #   tier_of（arbi 官方 → 社区实测中位 → 未评级）。此前这里自己拉三张表，
        #   实测档回落会漏掉这张卡（指令 §2.4 的"自动受益"假设在此处不成立）。
        sched, nodes, node_tier = await self._arb_fetch()
        start, step = sched["startTs"], sched.get("stepSec", 3600)
        seq = sched["seq"]
        idx0 = int((_t.time() - start) // step)
        if idx0 < 0 or idx0 >= len(seq):
            return err_card(
                _arbi.expired_hint(sched),
                _arb_wait_fix(),
                title=L("仲裁时间表", "Arbitration Schedule"),
            )
        # ★ 2026-10-11：「仲裁表 生存」按任务类型筛选（帮助卡写了「可筛类型」，旧实现
        #   完全忽略参数 ⇒ 照发得到的是全表）。类型词与「仲裁」指令同一张表。
        toks = list(getattr(parsed, "content", None) or [])
        if getattr(parsed, "preset", None):
            toks.insert(0, parsed.preset)
        toks = _arb_en_toks(toks)
        want_types = {self._ARB_TYPES[t] for t in toks if t in self._ARB_TYPES}
        bad = [t for t in toks if t not in self._ARB_TYPES]
        rows = []
        for h in range(0, min(24 * 14, len(seq) - idx0)):  # 逐小时，14 天
            key = sched["nodes"][seq[idx0 + h]]
            n = nodes.get(key, {})
            name = n.get("nameZh", "?")
            system = n.get("systemNameZh", "")
            mtype = _arb_mission(n)
            fac = _arb_faction(n)
            # ★ 2026-10-22：评级后带该节点**社区实测每小时生息中位数**（如「A 612」）；
            #   无实测样本的节点只给档位（见 arbi.tier_label_with_median 的说明）。
            tv = _arbi.tier_label_with_median(key, node_tier)
            t = local_datetime(start + (idx0 + h) * step)
            # ★ 2026-10-02 裂列修复：存**结构化单元格**，输出时才按全角空格拼行。
            #   不变量：渲染层分列口径（render.py 的 _split_cells / 预扫描 /
            #   _table_mode 列对齐）**只认全角空格 `　`** ⇒ 单元格内允许 ASCII
            #   空格，但绝不可把 ASCII 空格当列分隔符。旧写法「ASCII 空格 join →
            #   split」会把含空格的节点名（V Prime / Tyana Pass / Outer Terminus，
            #   88 个仲裁节点中 3 个）劈成两列，其后各列整体右移、末列溢出。
            if want_types and mtype not in want_types:
                continue
            # ★ 2026-10-22：行元组末位带上节点 key（图例判「本页有没有带中位数的行」用；
            #   `r[5]` 仍是评级列，不动既有下标口径）。
            if is_en():
                name, system, mtype, fac = _arb_en_node(nodes, key)
            rows.append((t, name, system, mtype, fac, tv, key))
        if bad and not want_types:
            return err_card(
                L(
                    f"仲裁表：不认识的筛选词「{'、'.join(bad)}」",
                    f'Arbitration table: unknown filter "{", ".join(bad)}"',
                ),
                L(
                    "从可筛选类型里选词重发，例如「仲裁表 生存」",
                    'Pick a type from the list and resend, e.g. "arbtable survival"',
                ),
                (L("可筛选类型", "Filterable types"), L(self._ARB_TYPES_STR, _ARB_TYPES_STR_EN)),
                title=L("仲裁时间表", "Arbitration Schedule"),
            )
        if not rows:
            return err_card(
                L(
                    "该类型未来 14 天内没有仲裁场次",
                    "No arbitration slots of this type in the next 14 days",
                ),
                L("换一个类型重发", "Try another type"),
                (L("可筛选类型", "Filterable types"), L(self._ARB_TYPES_STR, _ARB_TYPES_STR_EN)),
                title=L("仲裁时间表", "Arbitration Schedule"),
            )
        size = 15
        total = len(rows)
        pages = max(1, (total + size - 1) // size)
        page = min(max(1, parsed.page or 1), pages)
        page_rows = rows[(page - 1) * size : page * size]
        offsets = list(dict.fromkeys(offset_label(r[0]) for r in page_rows))
        zone = " / ".join(offsets)
        over = (parsed.page or 1) > pages
        # 时间 + 节点 + 星球 + 类型 + 派系 + [评级]：节点与星球**各占一列**
        # （不合并成「V Prime（金星）」——合并列是「仲裁排期」卡的写法）
        S = "　"
        body = [
            "· "
            + S.join(
                c
                for c in (
                    _arb_clock(t, len(offsets) > 1),
                    name,
                    system,
                    mtype,
                    fac,
                    tv,
                )
                if c
            )
            for t, name, system, mtype, fac, tv, _k in page_rows
        ]
        chunk = [
            L(
                f"◆ 范围：未来 14 天共 {total} 场（{zone}）",
                f"◆ Range: {total} slots in the next 14 days ({zone})",
            ),
            L("◆ 仲裁时间表", "◆ Arbitration Schedule"),
            L(
                f"{S}时间{S}节点{S}星球{S}类型{S}派系{S}评级",
                f"{S}Time{S}Node{S}Planet{S}Type{S}Faction{S}Rating",
            ),
            *body,
        ]
        if over:
            chunk.append(
                L(
                    f"※ 只有 {pages} 页，已显示最后一页",
                    f"※ Only {pages} page{'' if pages == 1 else 's'}; showing the last one",
                )
            )
        chunk.append(
            L(_arbi.TIER_NOTE, _TIER_NOTE_EN)
        )  # 评级列下方标评分范围（2026-10-16 维护者要求）
        # ★ 2026-10-22：本卡评级列带了实测中位数 ⇒ 图例补一句说明那个数字是什么。
        #   只在**本页真有带中位数的行**时加，免得纯官方档位页多一句废话。
        if any(_arbi.measured_of(_r[6]).get("median") is not None for _r in page_rows):
            chunk.append(
                L(
                    _arbi.TIER_MEDIAN_NOTE,
                    "※ The number after the rating = community measured median per hour (rounded)",
                )
            )
        if any(r[5].endswith(_arbi.LOW_MARK) for r in page_rows):
            chunk.append(
                L(
                    _arbi.LOW_NOTE,
                    f'※ A rating marked "{_arbi.LOW_MARK}" = small sample: fewer than '
                    f"{_arbi.LOW_N} community records for this node, tier for reference only",
                )
            )
        fdesc = "、".join(t for t in toks if t in self._ARB_TYPES)
        fdesc_en = ", ".join(_ARB_ZH_EN.get(t, t) for t in toks if t in self._ARB_TYPES)
        title = L(
            f"仲裁时间表 · {fdesc}（第{page}/{pages}页，共{total}条）"
            if fdesc
            else f"仲裁时间表（第{page}/{pages}页，共{total}条）",
            f"Arbitration Schedule · {fdesc_en} (Page {page}/{pages}, {total} total)"
            if fdesc_en
            else f"Arbitration Schedule (Page {page}/{pages}, {total} total)",
        )
        return Reply(title, chunk, footer=fmt.fmt_platform_footer(platform))
