# -*- coding: utf-8 -*-
"""确定性轮换与赏金指令（D4 自 main.py 迁入，方法体逐字未改）。

覆盖路由键 5 项：bounty / incarnon / tenet / coda / acrichis；
核心 _rotation_lines：卡面数据源走 core_paths 的 read_path 读 rotations.json
（plugin_data 运行期副本优先 → 包内种子回退，调用逐字不变）。
随迁模块级函数 _elem_txt / _weapon_rows（_rotation_lines 专用行构造）。
子包纪律：不 import astrbot（事件对象鸭子类型）。
"""

from __future__ import annotations

import asyncio
import json
import re
import time

from .. import calculators as calc
from .. import formatters as fmt
from .. import paths as core_paths
from ..i18n import L, is_en
from ..logging_compat import logger
from .base import Reply

# 「信条 刷新」「终幕 重抓」「言录使 更新」：不等定时，立即从 wiki 源头重抓（2026-10-18 维护者要求）。
# 要经 FlareSolverr 抓页面，比较重，防滥用两道限（全局共享，不分群不分人）：
#   ① 同一项 5 分钟内只重抓一次（群管理 / 超管不受这条限制）；
#   ② 同一项 1 小时内最多重抓 6 次（**所有人都算**，管理员也不例外）。
_PULL_WORDS = frozenset({"刷新", "重抓", "更新"})
_PULL_WORDS_EN = frozenset({"refresh", "update", "pull"})  # 英文写法只在本命令的参数解析处认
_PULL_COOLDOWN = 300.0
_PULL_WINDOW = 3600.0
_PULL_MAX_PER_WINDOW = 6


def _nm(it: dict) -> str:
    """条目显示名：中文模式 cn 优先；英文模式 en 优先。"""
    if is_en():
        return it.get("en") or it.get("cn") or ""
    return it.get("cn") or it.get("en") or ""


def _note_line(note: str) -> str:
    """数据说明行：wiki 页名写成空格形式（Coda_Weapons → Coda Weapons，同一页面）。

    半角下划线会让 QQ Markdown 文字卡整卡退回纯文字；说明来自社区快照数据，只在显示时转换。
    """
    return "※ " + re.sub(r"(?<=[A-Za-z])_(?=[A-Za-z])", " ", str(note))


def _pull_say(status: str) -> str:
    """把 refresh_* 的返回值翻成人话（与网页面板「从源头重抓」同口径）。"""
    v = str(status or "")
    if v == "fresh":
        return L("本地已是最新", "Local data is already up to date")
    if v.startswith("refreshed"):
        via = (
            L("（wiki 抓不到，用的社区快照）", " (wiki unreachable, used the community snapshot)")
            if "(community)" in v
            else L("（wiki 抓不到，用的 WFHelper）", " (wiki unreachable, used WFHelper)")
            if "(wfhelper)" in v
            else ""
        )
        return L("已更新为 wiki 最新数据", "Updated to the latest wiki data") + via
    if v.startswith("not-updated"):
        return L(
            "wiki 还没人更新本轮数据，未覆盖（后台会继续重抓）",
            "The wiki has not been updated for this round yet, nothing overwritten (the background keeps retrying)",
        )
    if v == "no-community":
        return L(
            "wiki 与社区快照都取不到", "Neither the wiki nor the community snapshot is reachable"
        )
    if v == "failed":
        return L("重抓失败（详见日志）", "Re-fetch failed (see the log)")
    return v


def _elem_txt(it: dict) -> str:
    """条目行尾的「磁力 25.7%」；没有快照数据时明确标记待获取。"""
    elem = calc.ELEM_ZH.get((it.get("element") or "").lower())
    pct = it.get("bonus")
    if not elem or pct in (None, ""):
        return L("待获取", "Pending")
    try:
        num = float(pct)
    except (TypeError, ValueError):
        return L("待获取", "Pending")
    if is_en():
        return f"{(it.get('element') or '').title()} {num:g}%"
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
        if is_en():
            label = en or name
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
            lines.append(
                L(
                    "※ 赏金不分页：一览已列全部地区，发「赏金 <地区>」看该地区详情",
                    '※ Bounties are not paged: the overview lists every region; send "bounty <region>" for one region',
                )
            )
        return Reply(
            title, lines, footer=fmt.fmt_platform_footer(platform, getattr(data, "source", ""))
        )

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
            _cd = fmt.countdown(nxt_reset.isoformat())
            lines = [
                L(
                    f"◆ 本周：第 {wk_no}/{len(weeks)} 周｜距换轮 剩余 {_cd}",
                    f"◆ This week: week {wk_no}/{len(weeks)}｜rotation ends: {fmt._left_en(_cd)}",
                ),
                L("◆ 下周：", "◆ Next week: ") + L("、", ", ").join(_nm(x) for x in nxt),
                L(
                    f"◆ 本周灵化武器（{len(cur)} 件）",
                    f"◆ This week's Incarnon weapons ({len(cur)})",
                ),
            ]
            for it in cur:
                name = _nm(it)
                var = it.get("variant") or ""
                same = var.lower().replace(" ", "") == (it.get("en") or "").lower().replace(" ", "")
                lines.append(
                    f"· {name}" + (L(f"（{var}）", f" ({var})") if var and not same else "")
                )
            lines.append(
                # ★ 2026-10-11：统一走 countdown（「X天X小时 / X小时X分」），
                #   不再出现「0天22小时」
                L(
                    f"※ 周一 00:00 UTC 换轮（本周期共 {len(weeks)} 周，循环往复）；"
                    "每周可在钢铁回廊 Tier 5 / Tier 10 各选 1 个灵化适配器",
                    f"※ Rotates Monday 00:00 UTC ({len(weeks)}-week cycle, repeating); "
                    "each week you can pick 1 Incarnon Adapter at Steel Path Circuit Tier 5 / Tier 10",
                )
            )
            return Reply(
                title,
                lines,
                footer=L(
                    f"来源 Update 43 官方轮换表 · 锚点 Week {anchor_week} 起于 "
                    f"{epoch.strftime('%Y-%m-%d')}（周一 UTC）",
                    f"Source: Update 43 official rotation table · anchor Week {anchor_week} "
                    f"starting {epoch.strftime('%Y-%m-%d')} (Monday UTC)",
                ),
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
            _cd = fmt.countdown(nxt_at.isoformat())
            _at = fmt.format_time(nxt_at)
            lines = [
                L(
                    f"◆ 当前批次：{labels[idx]} 批｜共 {len(batches)} 批轮换，每 {days} 天换一次",
                    f"◆ Current batch: {labels[idx]}｜{len(batches)} batches rotate, every {days} days",
                ),
                L(
                    f"◆ 距换批：剩余 {_cd}（{_at}）",
                    f"◆ Next batch: {fmt._left_en(_cd)} ({_at})",
                ),
                L(
                    f"◆ 下一批 {labels[(idx + 1) % len(batches)]}：",
                    f"◆ Next batch {labels[(idx + 1) % len(batches)]}: ",
                )
                + L("、", ", ").join(_nm(x) for x in nxt),
                L(f"◆ 本批武器（{len(cur)} 把）", f"◆ This batch's weapons ({len(cur)})"),
            ]
            lines.extend(_weapon_rows(cur))
            _vn = self._valence_note(data, epoch + period * passed)
            if _vn:
                lines.append(_vn)
            if data.get("_note"):
                lines.append(_note_line(data["_note"]))
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
            _cd = fmt.countdown(nxt_at.isoformat())
            _at = fmt.format_time(nxt_at)
            lines = [
                L(
                    f"◆ 距元素加成重生成：剩余 {_cd}（{_at}，每 {days} 天一次）",
                    f"◆ Element bonuses regenerate: {fmt._left_en(_cd)} ({_at}, every {days} days)",
                ),
                L(
                    f"◆ 常驻武器（{len(items)} 把，随到随买，各 40 个腐化全息密钥）",
                    f"◆ Permanent weapons ({len(items)}, buy any time, 40 Corrupted Holo-Keys each)",
                ),
            ]
            lines.extend(_weapon_rows(items))
            _vn = self._valence_note(data, epoch + period * passed)
            if _vn:
                lines.append(_vn)
            if data.get("_note"):
                lines.append(_note_line(data["_note"]))
            return Reply(title, lines)

        # —— 模式二：滑动窗口 ——
        items = data.get("items") or []
        if not items:
            return Reply(
                title,
                [
                    L(
                        "该轮换的数据表（core/data/rotations.json -> "
                        f"{key}）尚未接线，请按当前版本校准后填入",
                        f"The data table for this rotation (core/data/rotations.json -> {key}) "
                        "is not wired up yet; calibrate it for the current version and fill it in",
                    )
                ],
            )
        epoch = datetime.fromisoformat(data["epoch"])
        hours = max(1, int(data.get("period_hours", 168)))
        pick = max(1, int(data.get("pick", 1)))
        slot = int((datetime.now(timezone.utc) - epoch).total_seconds() // 3600 // hours)
        cur = [items[(slot * pick + i) % len(items)] for i in range(pick)]
        nxt = [items[((slot + 1) * pick + i) % len(items)] for i in range(pick)]
        _sep = L("、", ", ")
        lines = [
            L("当前：", "Current: ") + _sep.join(cur),
            L("下一轮：", "Next: ") + _sep.join(nxt),
        ]
        return Reply(title, lines)

    async def _h_rotation_incarnon(self, parsed, event, platform) -> Reply:
        return self._rotation_lines("incarnon", L("本周钢铁回廊灵化", "Weekly Incarnon Rotation"))

    @staticmethod
    def _wants_pull(parsed) -> bool:
        return any(
            t in _PULL_WORDS or str(t).lower() in _PULL_WORDS_EN for t in (parsed.content or [])
        )

    async def _pull_now(self, key: str, event) -> str:
        """立即从源头重抓（``key``：valence = 信条 + 终幕，acrichis = 言录使）；返回一行说明。"""
        hist = self.__dict__.setdefault("_pull_hist", {}).setdefault(key, [])
        lock = self.__dict__.setdefault("_pull_lock", asyncio.Lock())
        now = time.monotonic()
        hist[:] = [t for t in hist if now - t < _PULL_WINDOW]
        if len(hist) >= _PULL_MAX_PER_WINDOW:
            wait = _PULL_WINDOW - (now - hist[0])
            return L(
                f"※ 这一小时已重抓 {len(hist)} 次（上限 {_PULL_MAX_PER_WINDOW} 次），"
                f"{int(wait // 60) + 1} 分钟后可再试；下面是当前数据",
                f"※ Already re-fetched {len(hist)} times this hour (limit {_PULL_MAX_PER_WINDOW}); "
                f"try again in {int(wait // 60) + 1}m. Current data below",
            )
        wait = _PULL_COOLDOWN - (now - hist[-1]) if hist else 0
        is_admin = getattr(self, "_is_admin", None)
        if wait > 0 and not (callable(is_admin) and is_admin(event)):
            return L(
                f"※ 刚重抓过，{int(wait // 60) + 1} 分钟后可再试（群管理不受限）；下面是当前数据",
                f"※ Just re-fetched; try again in {int(wait // 60) + 1}m "
                "(group admins are exempt). Current data below",
            )
        if lock.locked():
            return L(
                "※ 正在重抓中，请稍后再查；下面是当前数据",
                "※ A re-fetch is in progress, check again shortly. Current data below",
            )
        async with lock:
            hist.append(time.monotonic())
            fn = (
                self.client.refresh_acrichis_week
                if key == "acrichis"
                else self.client.refresh_valence
            )
            try:
                status = str(await asyncio.wait_for(fn(force=True), 150))
            except Exception as exc:  # noqa: BLE001 - 重抓失败照样给出当前数据
                logger.warning("[sdjk] 指令重抓失败（%s）：%r", key, exc)
                return L(
                    f"※ 重抓失败：{type(exc).__name__}；下面是当前数据",
                    f"※ Re-fetch failed: {type(exc).__name__}. Current data below",
                )
        logger.info("[sdjk] 指令重抓（%s）：%s", key, status)
        return L("※ 已从源头重抓：", "※ Re-fetched from the source: ") + _pull_say(status)

    def _with_note(self, reply: Reply, note: str) -> Reply:
        if note:
            reply.lines.insert(0, note)
        return reply

    async def _h_rotation_tenet(self, parsed, event, platform) -> Reply:
        note = await self._pull_now("valence", event) if self._wants_pull(parsed) else ""
        return self._with_note(
            self._rotation_lines("tenet", L("Ergo 信条武器轮换", "Ergo Tenet Weapon Rotation")),
            note,
        )

    async def _h_rotation_coda(self, parsed, event, platform) -> Reply:
        note = await self._pull_now("valence", event) if self._wants_pull(parsed) else ""
        return self._with_note(
            self._rotation_lines("coda", L("Coda 终幕武器轮换", "Coda Weapon Rotation")), note
        )

    async def _h_acrichis(self, parsed, event, platform) -> Reply:
        note = await self._pull_now("acrichis", event) if self._wants_pull(parsed) else ""
        # 本周货单是社区维护快照（DE 不下发），过期后回落到静态商品池
        week = await self.client.acrithis_week()
        if week:
            title, lines = fmt.fmt_acrichis_week(week)
        else:
            # 过期时必须明说「这只是候选池」，否则会被当成本周实际在卖的 5 件
            snapshot = (
                self.client._acrichis_snapshot()
                if hasattr(self.client, "_acrichis_snapshot")
                else {}
            )
            title, lines = fmt.fmt_acrichis(
                await self.client.acrithis_pool(),
                stale=bool(snapshot.get("items")),
                pending=not bool(snapshot.get("items")),
            )
        return self._with_note(Reply(title, lines, footer=fmt.fmt_platform_footer(platform)), note)
