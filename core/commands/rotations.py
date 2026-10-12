# -*- coding: utf-8 -*-
"""确定性轮换与赏金指令（D4 自 main.py 迁入，方法体逐字未改）。

覆盖路由键 5 项：bounty / incarnon / tenet / coda / acrichis；
核心 _rotation_lines：卡面数据源走 core_paths 的 read_path 读 rotations.json
（plugin_data 运行期副本优先 → 包内种子回退，调用逐字不变）。
随迁模块级函数 _elem_txt / _weapon_rows（_rotation_lines 专用行构造）。
子包纪律：不 import astrbot（事件对象鸭子类型）。
"""

from __future__ import annotations

import json

from .. import calculators as calc
from .. import formatters as fmt
from .. import paths as core_paths
from .base import Reply


def _elem_txt(it: dict) -> str:
    """条目行尾的「磁力 25.7%」；没有快照数据时返回空串。"""
    elem = calc.ELEM_ZH.get((it.get("element") or "").lower())
    pct = it.get("bonus")
    if not elem or pct in (None, ""):
        return ""
    try:
        num = float(pct)
    except (TypeError, ValueError):
        return ""
    return f"{elem} {num:g}%"


def _weapon_rows(items: list[dict]) -> list[str]:
    """信条 / 终幕的条目行：``· 中文名（英文名）　磁力 25.7%``。

    只用**一个全角空格**分隔两格，列对齐交给渲染层 —— 那里才有真实字形宽度
    （`render._PAIR_RE` / `pair_col`）。早先在 formatter 里按「CJK 记 2、
    ASCII 记 1」手算补空格，可 Noto CJK 的拉丁字母是**比例宽**，算出来仍差一个
    汉字宽，玩家反馈「没对齐真的好丑」。
    """
    out: list[str] = []
    for it in items:
        name = it.get("cn") or it.get("en") or ""
        en = it.get("en") or ""
        label = f"{name}（{en}）" if en and en != name else name
        elem = _elem_txt(it)
        out.append(f"· {label}　{elem}" if elem else f"· {label}")
    return out


class RotationCommands:
    """Mixin：确定性轮换 / 赏金 handler（挂载于 main.WarframeSDJK）。"""

    async def _h_bounty(self, parsed, event, platform) -> Reply:
        keyword = " ".join([parsed.preset] if parsed.preset else []) + " " + parsed.content_str
        keyword = keyword.strip()
        data = await self.client.syndicate_missions(platform)
        # oracle 补「扎里曼 / 实验室 / 1999」的节点与挑战（DE 侧 Jobs 恒为空）；
        # 客户端内置 15 分钟缓存 + 3.5s 硬超时，拿不到就降级为只列等级，不拖主流程。
        cycle = await self.client.bounty_cycle()
        title, lines = fmt.fmt_bounties(data, keyword, cycle)
        if (parsed.page or 1) > 1:
            # ★ 2026-10-11：赏金卡不分页，-N 无效 —— 明说，不静默
            lines.append("※ 赏金不分页：一览已列全部地区，发「赏金 <地区>」看该地区详情")
        return Reply(title, lines, footer=fmt.fmt_platform_footer(platform))

    # ------------------------------------------------------------------
    # 确定性轮换（灵化/信条/终幕/言录使）：数据驱动，见 core/data/rotations.json
    # ------------------------------------------------------------------

    def _rotation_lines(self, key: str, title: str) -> Reply:
        """轮换表查询。支持三种模式：
        · weekly_cycle：N 周固定循环（灵化，锚点周次已知）
        · batch_cycle：A/B 两批交替（终幕）
        · refresh_only：常驻 + 定期重生成（信条，只刷新元素加成）
        · 默认：按 period_hours 从 epoch 起算的滑动窗口
        """
        data: dict = {}
        try:
            # ★ 数据源三层（2026-09-28 裁定）：运行期副本（自动刷新回写）→ 包内种子 → 空 dict。
            #   旧实现直接读包内常量 ⇒ 自动刷新的回写**到不了卡面**，换批只能靠发版。
            data = json.loads(
                core_paths.read_path("rotations.json").read_text(encoding="utf-8")
            ).get(key, {})
        except Exception:  # noqa: BLE001
            pass
        from datetime import datetime, timedelta, timezone

        # —— 模式一：固定周循环 ——
        weeks = data.get("weeks") or []
        if weeks:
            epoch = datetime.fromisoformat(data["epoch"])
            anchor_week = int(data.get("anchor_week", 1))
            period = timedelta(hours=int(data.get("period_hours", 168)))
            now = datetime.now(timezone.utc)
            passed = int((now - epoch) // period)
            wk_no = (anchor_week - 1 + passed) % len(weeks) + 1
            cur = weeks[wk_no - 1]
            nxt = weeks[wk_no % len(weeks)]
            # 本周重置点（周一 00:00 UTC）
            wd = now.weekday()
            reset = (now - timedelta(days=wd)).replace(hour=0, minute=0, second=0, microsecond=0)
            nxt_reset = reset + period
            lines = [
                f"◆ 本周：第 {wk_no}/{len(weeks)} 周｜距换轮 剩余 {fmt.countdown(nxt_reset.isoformat())}",
                "◆ 下周：" + "、".join(x.get("cn") or x.get("en") for x in nxt),
                f"◆ 本周灵化武器（{len(cur)} 件）",
            ]
            for it in cur:
                name = it.get("cn") or it.get("en")
                var = it.get("variant") or ""
                same = var.lower().replace(" ", "") == (it.get("en") or "").lower().replace(" ", "")
                lines.append(f"· {name}" + (f"（{var}）" if var and not same else ""))
            lines.append(
                # ★ 2026-10-11：统一走 countdown（「X天X小时 / X小时X分」），
                #   不再出现「0天22小时」
                f"※ 周一 00:00 UTC 换轮（本周期共 {len(weeks)} 周，循环往复）；"
                "每周可在钢铁回廊 Tier 5 / Tier 10 各选 1 个灵化适配器"
            )
            return Reply(
                title,
                lines,
                footer=f"来源 Update 43 官方轮换表 · 锚点 Week {anchor_week} 起于 "
                f"{epoch.strftime('%Y-%m-%d')}（周一 UTC）",
            )

        # —— 模式：批次轮换（Coda 终幕：A/B 两批每 4 天交替）——
        batches = data.get("batches") or []
        if batches:
            epoch = datetime.fromisoformat(data["epoch"])
            period = timedelta(hours=int(data.get("period_hours", 96)))
            now = datetime.now(timezone.utc)
            passed = int((now - epoch) // period)
            anchor = int(data.get("anchor_idx", 0))
            idx = (anchor + passed) % len(batches)
            labels = data.get("batch_label") or [str(i + 1) for i in range(len(batches))]
            cur, nxt = batches[idx], batches[(idx + 1) % len(batches)]
            nxt_at = epoch + period * (passed + 1)
            days = int(data.get("period_hours", 96)) // 24
            lines = [
                f"◆ 当前批次：{labels[idx]} 批｜共 {len(batches)} 批轮换，每 {days} 天换一次",
                f"◆ 距换批：剩余 {fmt.countdown(nxt_at.isoformat())}（{nxt_at.strftime('%m-%d %H:%M')} UTC）",
                f"◆ 下一批 {labels[(idx + 1) % len(batches)]}："
                + "、".join(x.get("cn") or x.get("en") for x in nxt),
                f"◆ 本批武器（{len(cur)} 把）",
            ]
            lines.extend(_weapon_rows(cur))
            _vn = self._valence_note(data, epoch + period * passed)
            if _vn:
                lines.append(_vn)
            if data.get("_note"):
                lines.append(f"※ {data['_note']}")
            return Reply(title, lines)

        # —— 模式：常驻 + 定期刷新（Tenet 信条：武器常驻，元素加成每 4 天重生成）——
        items = data.get("items") or []
        if items and isinstance(items[0], dict):
            epoch = datetime.fromisoformat(data["epoch"])
            period = timedelta(hours=int(data.get("period_hours", 96)))
            now = datetime.now(timezone.utc)
            passed = int((now - epoch) // period)
            nxt_at = epoch + period * (passed + 1)
            days = int(data.get("period_hours", 96)) // 24
            lines = [
                f"◆ 距元素加成重生成：剩余 {fmt.countdown(nxt_at.isoformat())}"
                f"（{nxt_at.strftime('%m-%d %H:%M')} UTC，每 {days} 天一次）",
                f"◆ 常驻武器（{len(items)} 把，随到随买，各 40 个腐化全息密钥）",
            ]
            lines.extend(_weapon_rows(items))
            _vn = self._valence_note(data, epoch + period * passed)
            if _vn:
                lines.append(_vn)
            if data.get("_note"):
                lines.append(f"※ {data['_note']}")
            return Reply(title, lines)

        # —— 模式二：滑动窗口 ——
        items = data.get("items") or []
        if not items:
            return Reply(
                title,
                [
                    "该轮换的数据表（core/data/rotations.json -> "
                    f"{key}）尚未接线，请按当前版本校准后填入"
                ],
            )
        epoch = datetime.fromisoformat(data["epoch"])
        hours = max(1, int(data.get("period_hours", 168)))
        pick = max(1, int(data.get("pick", 1)))
        slot = int((datetime.now(timezone.utc) - epoch).total_seconds() // 3600 // hours)
        cur = [items[(slot * pick + i) % len(items)] for i in range(pick)]
        nxt = [items[((slot + 1) * pick + i) % len(items)] for i in range(pick)]
        lines = ["当前：" + "、".join(cur), "下一轮：" + "、".join(nxt)]
        return Reply(title, lines)

    async def _h_rotation_incarnon(self, parsed, event, platform) -> Reply:
        return self._rotation_lines("incarnon", "本周钢铁回廊灵化")

    async def _h_rotation_tenet(self, parsed, event, platform) -> Reply:
        return self._rotation_lines("tenet", "Ergo 信条武器轮换")

    async def _h_rotation_coda(self, parsed, event, platform) -> Reply:
        return self._rotation_lines("coda", "Coda 终幕武器轮换")

    async def _h_acrichis(self, parsed, event, platform) -> Reply:
        # 本周货单是社区维护快照（DE 不下发），过期后回落到静态商品池
        week = await self.client.acrithis_week()
        if week:
            title, lines = fmt.fmt_acrichis_week(week)
        else:
            # 过期时必须明说「这只是候选池」，否则会被当成本周实际在卖的 5 件
            title, lines = fmt.fmt_acrichis(await self.client.acrithis_pool(), stale=True)
        return Reply(title, lines, footer=fmt.fmt_platform_footer(platform))
