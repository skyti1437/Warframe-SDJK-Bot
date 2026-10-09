# -*- coding: utf-8 -*-
"""市场查询域（D6 自 main.py 迁入，方法体逐字未改）。

覆盖路由键 6 项：wm / rm / wr / rank / trend / ducats；类属性 _RANK_CN
随域迁出。模块级 JUNK_FILE 原文为 Path(__file__).resolve().parent 拼接，
随域迁移后改写为等价的 PLUGIN_DIR 拼接（同值：插件根/core/data/junk.json，
本笔唯一非逐字改写点，语义与目标文件逐字节不变）。
子包纪律：不 import astrbot（事件对象鸭子类型）。
"""

from __future__ import annotations

import collections
import difflib
import json
import re
from typing import Optional

from .. import api_client
from ..logging_compat import logger
from .. import formatters as fmt
from .. import matching
from ..parser import parse_wm, parse_wr
from .base import PLUGIN_DIR, Reply

JUNK_FILE = PLUGIN_DIR / "core" / "data" / "junk.json"

# 「蓝图=总图」分支的**排除词**（`_pick_wm_set_part` 用）：
#   「机体蓝图」这类**部件蓝图**不能被当成武器总图挑走。
# ★★ 必须与 `core/parser.py::_PART_SPECIFIC` **同步**（差集只允许「头部神经」——
#   它在 parser 侧已被 `_PART_STRENGTH` 归并成「头部」）。2026-10-07 提到模块级，
#   让 tests/test_wm_part.py 能做**结构断言**（此前只有注释提醒，靠人记）。
_SET_BLUEPRINT_SKIP = (
    "机体",
    "头部",
    "系统",
    "枪管",
    "枪机",
    "枪托",
    "头盔",
    "握柄",
    "刀刃",
    "连接器",
    "外壳",
    "弓弦",
    "弓身",
    "上弓臂",
    "下弓臂",
    "拳套",
    "武器舱",
    "镖袋",
    "护手",
    # ★ 2026-10-07 再同步 8 个新词（饰物/引擎/锤头/机舱/圆盘/机翼/外甲/握把）。
    #   全量 234 套装实测：不同步的话「蓝图」分支有 7 套给错部件
    #   （遂心=握把蓝图、Mantis/Scimitar/Xiphos/星察/午夜电波=引擎蓝图、
    #   刺影=外甲蓝图）；同步后 1 套修真 bug、5 套改给机身蓝图、1 套（刺影）
    #   变成「明说没有蓝图」——不静默给错。
    "饰物",
    "引擎",
    "锤头",
    "机舱",
    "圆盘",
    "机翼",
    "外甲",
    "握把",
)


# 「头」部件黑话消歧（2026-10-02 二修）：「水晶头」「水晶p头」「水晶 头」…
# = 头部部件。判据（用户口径）：**剥掉尾部「头」后的前缀必须精确存在于
# 物品名/黑话里**（水晶 → Citrine ✓）——白霜弹头的前缀「白霜弹」不是黑话
# → 不剥，整名按 MOD 解析。⚠️ 两个教训：
#   ① 消歧必须在整名解析**之前** —— 首版放在「解析失败→给建议」之后，
#      整名解析不到根本走不到（用户实测四种写法全挂）；
#   ② 判据只能用**精确**命中（resolve_wm_exact）—— 模糊链路会把「白霜弹」
#      沾到白霜（Frost）Prime 上，照样误剥。
async def _head_part_resolve(client, item: str):
    """整名以「头」结尾且剥头后的前缀精确命中 → 返回 (剥头物品名, 解析结果)。"""
    full = item.rstrip()
    if not full.endswith("头"):
        return None
    stripped = full[:-1].rstrip()
    if not stripped:
        return None
    if await client.resolve_wm_exact(full):
        return None  # 整名本身就是 MOD/物品（白霜弹头…）
    alt = await client.resolve_wm_exact(stripped)
    if alt:
        return stripped, alt
    return None


async def riven_market_weapon(client, query: str, weapon: dict) -> dict:
    """紫卡**市场**（wr）按母武器认卡 —— 2026-10-05 用户口径。

    「wr 绝路」→ 绝路家族（含 Prime）的紫卡都归它；市场路径**不解析倾向**，
    只要认出是哪把武器。WM 拍卖端点只认**自家列表**里的 slug（实测
    `weapon_url_name=rubico_prime` → **HTTP 400**，`rubico` → 200），而解析可能
    先命中本地补全的变体条目（`dispositions_rivenmirror.json` 的 rubico_prime 等）
    ⇒ 命中 slug 不在自家列表时，剥 Prime 后缀（p / p版 / Prime，含条目 zh 名）
    回退母武器再解析一次；回退不到就原样返回（交给下游报空，不静默换武器）。

    注：WM 自家列表里本就以 `_prime` 为名的独立武器（euphona_prime 等，其母武器
    不在表内）**不受影响** —— 它们本来就在自家列表里（第一道判断即放行）。
    """
    if not weapon:
        return weapon
    own = await client.wm_riven_weapon_slugs()
    if weapon.get("url_name") in own:
        return weapon
    base_q = matching.prime_base(query) or matching.prime_base(weapon.get("zh") or "")
    if not base_q:
        return weapon
    base_w = await client.resolve_riven_weapon(base_q)
    if base_w and base_w.get("url_name") in own:
        return base_w
    return weapon


class MarketCommands:
    """Mixin：warframe.market / 排行 / 趋势 handler（挂载于 main.WarframeSDJK）。"""

    # ------------------------------------------------------------------
    # 市场与查价
    # ------------------------------------------------------------------
    @staticmethod
    def _pick_wm_set_part(parts: list[dict], part: str) -> Optional[dict]:
        """从套装部件里挑出部件词对应的那个；没有返回 None。

        「蓝图/总图」= 战甲/武器**总图**：zh 以「蓝图」结尾且不含其他部件词
        （「机体蓝图」也以蓝图结尾，得排除）。其余部件词按 zh 包含匹配
        （「头部」命中「XX Prime 头部神经光元蓝图」）。
        """
        if part in ("蓝图", "总图"):
            # ★ skip 与 _PART_SPECIFIC 必须同步（结构断言见 tests/test_wm_part.py）；
            #   常量已提到模块级 `_SET_BLUEPRINT_SKIP`（2026-10-07）。
            skip = _SET_BLUEPRINT_SKIP
            cands = [
                p
                for p in parts
                if (p.get("zh") or "").endswith("蓝图")
                and not any(w in (p.get("zh") or "") for w in skip)
            ]
            return cands[0] if cands else None
        for p in parts:
            if part in (p.get("zh") or ""):
                return p
        return None

    async def _h_wm(self, parsed, event, platform) -> Reply:
        pass

        q = parse_wm(parsed.content, parsed.preset)
        if q.group_buy:
            return await self._wm_group_buy(q, platform)
        if not q.item:
            return Reply(
                raw_text="用法：wm 物品名 [部件] [收购|合购a*2,b] [N个] [零级/满级/N级] "
                "[完整/优良/无瑕/光辉] [墨染] [-r]\n"
                "部件：蓝图（总图）/ 机体 / 系统 / 头部 / 配件（全部部件比价），"
                "如 wm 母牛 蓝图；头部可连写「头」（wm 水晶头 = wm 水晶 头部）\n"
                "品级：满级按物品实际满级（赋能 5 级 / 川流不息 5 级 / "
                "生命力 10 级）；精炼档只对遗物，墨染只看墨染 Mod"
            )
        # 「头」部件黑话：消歧在整名解析之前（教训见 _head_part_resolve 注释）
        _head = await _head_part_resolve(self.client, q.item)
        if _head:
            q.item, item = _head
            q.part = "头部"
        else:
            item = await self.client.resolve_wm_item(q.item)
        if not item and q.part:
            # ★ 2026-10-05：拆件后解析失败 → 用「原文重组」再试一次。部件词表
            #   扩充后（刀刃/外壳/头盔/枪管…）会与少数 MOD 名相撞（簧压刀刃、
            #   爆裂刀刃、锐利刀刃、燃烧外壳、震击 秘奥头盔、红晶枪管…），
            #   这些条目的正解是整名解析。只在主解析失败时触发 ⇒ 只会改善，
            #   不改变任何现有成功路径。
            item = await self.client.resolve_wm_item(f"{q.item}{q.part}")
        if not item:
            return await self._wm_suggest(q.item)
        # ★ 部件关键词（2026-09-19 用户反馈「wm 母牛 蓝图」出的是整套）：
        #   命中具体部件词时切到**该部件**的订单；「配件/部件」泛指时保留
        #   整套 + 部件参考价（见尾部提示）。只在命中套装时生效——
        #   「wm 母牛机体」直接解析成部件的走原路。
        set_parts: list[dict] = []
        if q.part and "set" in set(item.get("tags") or []):
            try:
                set_parts = await self.client.wm_set_parts(item["url_name"])
            except Exception:  # noqa: BLE001 - 部件拆价是增强项，失败不影响主输出
                set_parts = []
            if q.part != "配件":
                picked = self._pick_wm_set_part(set_parts, q.part)
                if picked is None:
                    names = [p.get("zh") or p.get("en") or p.get("url_name", "") for p in set_parts]
                    return Reply(
                        raw_text=(
                            f"「{item.get('zh') or item.get('en') or item['url_name']}」"
                            f"没有「{q.part}」这个部件。\n"
                            f"可用部件：{'、'.join(names) or '（未同步到部件表）'}\n"
                            f"也可以发整套看全部：wm {q.item}"
                        )
                    )
                item = picked
        # ── 品级 / 遗物精炼 / 墨染 过滤（2026-09-25 补：解析早就有，一直没接）──
        # 先取订单再判品级：满级要参考「挂单里出现的最高级」（WM 有 44 个 MOD
        # 条目没给 maxRank），无级挂单则忽略该词并说明。
        orders, _info = await self.client.wm_orders(item["url_name"], platform)
        rank, rank_note = self._resolve_rank_filter(q.rank, q.rank_word, item, orders)
        notes = [rank_note] if rank_note else []
        subtypes = {str(o.get("subtype") or "") for o in orders}
        refinement = q.refinement
        if refinement and not (subtypes & {"intact", "exceptional", "flawless", "radiant"}):
            notes.append(f"该物品没有遗物精炼档，「{q.refinement_word or refinement}」已忽略")
            refinement = None
        elif refinement:
            notes.append(f"只看「{q.refinement_word or refinement}」档遗物")
        moran = q.moran
        if moran and "atragraph" not in subtypes:
            notes.append("该物品没有墨染 Mod 变体，「墨染」已忽略")
            moran = False
        elif moran:
            notes.append("只看墨染 Mod")
        display = item.get("zh") or item.get("en") or item["url_name"]
        title, lines, best, pool = fmt.fmt_wm_orders(
            display,
            orders,
            buy=q.buy,
            page=parsed.page,
            page_size=self.page_size,
            quantity=q.quantity,
            rank=rank,
            refinement=refinement,
            moran=moran,
            notes=notes,
        )
        hint = "" if parsed.whisper or not best else " · 加 -r 生成游戏密语"
        # 套装附带部件参考价（2026-09-14 用户要求）：单查部件走上面的
        # 归一化匹配（wm 席瓦蓝图），这里只在命中套装时多拉几个部件订单。
        # 部件切换路径（q.part 具体词）已在上面拉过 set_parts 且 item 已是
        # 部件（非 set root），这里不会再命中。
        parts = set_parts
        if not q.part:
            try:
                parts = await self.client.wm_set_parts(item["url_name"])
            except Exception:  # noqa: BLE001
                parts = []
        if parts:
            rows = []
            for p_ in parts:
                try:
                    po, _ = await self.client.wm_orders(p_["url_name"], platform)
                except Exception:  # noqa: BLE001
                    po = []
                rows.append(
                    {
                        "name": p_.get("zh") or p_.get("en") or p_.get("url_name", ""),
                        "sell": fmt.wm_best_price(po, "sell"),
                        "buy": fmt.wm_best_price(po, "buy"),
                    }
                )
            lines.extend(fmt.fmt_wm_set_parts(rows))
            if q.part == "配件":
                lines.append(
                    f"※ 想看某个部件的在售/收购单：wm {q.item} 蓝图（或 机体 / 系统 / 头部）"
                )
        reply = Reply(
            title, lines, footer=fmt.fmt_platform_footer(platform, "warframe.market" + hint)
        )
        if parsed.whisper and pool:
            # -r 给前 5 个卖家各生成一条密语（在线优先同展示顺序，
            # 2026-09-14 用户要求：原来只有第一个）
            whisper_item = item.get("en") or item["url_name"]
            for o in pool[:5]:
                reply.whisper.append(fmt.build_whisper(o, whisper_item, sell=q.buy))
        return reply

    @staticmethod
    def _resolve_rank_filter(q_rank, q_rank_word, item, orders=None) -> tuple:
        """品级过滤解析 → ``(rank, note)``（2026-09-25，同日二修：数据驱动）。

        满级（q_rank == -1）取物品**实际**满级，来源逐级降级并在卡面标明：
          ① WM items 的 maxRank（赋能 5 / 川流不息 5 / 生命力 10）
          ② 挂单里出现的最高级（WM 有 44 个 MOD 条目没给 maxRank，如 intruder）
          ③ 类别保守兜底（赋能 5，其余 10）

        挂单完全没有品级信息时（遗物、intruder 这类无级挂单）忽略该词并说明，
        避免静默返回空表。
        """
        if q_rank is None:
            return None, ""
        ranks = {o.get("mod_rank") for o in (orders or []) if o.get("mod_rank") is not None}
        tags = set(item.get("tags") or [])
        has_rank_meta = bool(item.get("max_rank")) or bool({"mod", "arcane_enhancement"} & tags)
        if orders is None:
            # 单测/旧调用：只看元数据
            if not has_rank_meta:
                return None, f"该物品没有品级，「{q_rank_word or q_rank}」已忽略"
        elif not ranks:
            return None, f"该物品的挂单没有品级信息，「{q_rank_word or q_rank}」已忽略"
        if q_rank == -1:
            rank, src = item.get("max_rank"), "item"
            if not rank and ranks:
                rank, src = max(ranks), "orders"
            if not rank:
                rank, src = (5 if "arcane_enhancement" in tags else 10), "fallback"
            rank = int(rank)
            note = (
                f"只列满级（按挂单最高 {rank} 级）"
                if src == "orders"
                else f"只列满级（{rank}级）的单"
            )
            return rank, note
        return q_rank, f"只列 {q_rank} 级的单"

    async def _wm_suggest(self, query: str) -> Reply:
        """未命中时给中英文候选（含错别字容忍，如 波斯顿→伯斯顿）。"""
        # 玄骸武器不在 WM 普通物品表（价格走 xh 拍卖）——先给正确入口，
        # 否则「wm 沙皇」这类查询只能拿到一串无关候选（沙皇=赤毒·沙皇，
        # 2026-09-24 实测：旧词典把「沙皇」错映射到 Inaros，已删）。
        # 这里只做**归一化精确**匹配，不借 resolve_lich_weapon 的模糊兜底，
        # 避免未命中路径被形近字劫持。
        _nq = re.sub(r"[\s·・]+", "", query).lower()
        for _k, _slug in (self.client._aliases.get("lich_items") or {}).items():
            if re.sub(r"[\s·・]+", "", _k).lower() == _nq:
                _info = self.client.lich_weapon_info(_slug)
                _zh = _info.get("zh") or _slug
                return Reply(
                    raw_text=(
                        f"「{query}」是玄骸武器（{_zh}），不在集市物品表里。\n"
                        f"价格用：xh {_zh}　（支持元素/数值筛选，如 xh {_zh} 辐射 50）"
                    )
                )
        items = await self.client.wm_items()
        slugs = [it.get("url_name", "") for it in items]
        names = [(it.get("zh") or it.get("en") or it.get("url_name", "")) for it in items]
        close = difflib.get_close_matches(query.lower().replace(" ", "_"), slugs, n=3, cutoff=0.5)
        hits = list(dict.fromkeys(close))
        if len(hits) < 3:
            for h in api_client.fuzzy_hits(query, names, n=3):
                if h not in hits:
                    hits.append(h)
        tip = f"没有找到「{query}」" + (
            f"，你是不是想找：{'、'.join(hits[:3])}"
            if hits
            else "（可用英文名或在 core/data/aliases.json 中补词条）"
        )
        return Reply(raw_text=tip)

    async def _wm_group_buy(self, q, platform) -> Reply:
        sellers: dict[str, dict] = {}
        for name, qty in q.group_buy:
            item = await self.client.resolve_wm_item(name)
            if not item:
                return Reply(raw_text=f"合购中有物品未找到：{name}")
            orders, _ = await self.client.wm_orders(item["url_name"], platform)
            sells = [
                o
                for o in orders
                if o.get("order_type") == "sell"
                and o.get("platform", platform) == platform
                and (o.get("user", {}).get("status") in ("ingame", "online"))
            ]
            sells.sort(key=lambda o: o["platinum"])
            for o in sells[:5]:
                seller = o["user"]["ingame_name"]
                sellers.setdefault(seller, {"items": {}, "total": 0})
                sellers[seller]["items"][item["url_name"]] = (o["platinum"], qty)
        ranked = sorted(
            sellers.items(),
            key=lambda kv: (-len(kv[1]["items"]), sum(p * c for p, c in kv[1]["items"].values())),
        )
        lines = []
        for seller, info in ranked[:3]:
            total = sum(p * c for p, c in info["items"].values())
            covered = len(info["items"])
            lines.append(f"🟢 {seller}｜覆盖 {covered}/{len(q.group_buy)} 件｜合计约 {total}p")
            for url, (p, c) in info["items"].items():
                lines.append(f"　· {url} × {c} = {p * c}p")
        if not lines:
            return Reply(raw_text="暂时没有在线卖家能凑齐合购单")
        return Reply(
            "合购最优卖家（同卖家买齐更省事）", lines, footer=fmt.fmt_platform_footer(platform)
        )

    @staticmethod
    def _is_exact_match(a: dict, q, neg_set: set, pos_set: set) -> bool:
        """「完全命中词条」判定（2026-10-01 B 口径，**仅排序用**，非硬过滤）。

        与 ``_auction_match``（子集判定 + 在线状态/价格过滤）不同：这里只看
        词条集合**相等**，不看在线状态与价格：
          · 指定了正词条 → 实际正词条集合 == 指定集合（多一个即超集）；
          · 指定了具体负词条 → 实际负词条集合 == 指定集合；
          · 只是「任意负」（require_negative、无具体负词条）→ 实际负词条 ≥1
            即算命中（紫卡最多 1 条负，等价于 ==1；不得因「没指定名字」判超集）；
          · 完全没指定词条 → 全部算命中（调用方传 exact_ids=None 退化）。
        """
        item = a.get("item", {}) or {}
        attrs = item.get("attributes") or []
        urls_p = {at.get("url_name") for at in attrs if at.get("positive")}
        urls_n = {at.get("url_name") for at in attrs if not at.get("positive")}
        if pos_set and urls_p != pos_set:
            return False
        if neg_set and urls_n != neg_set:
            return False
        if q.require_negative and not neg_set and not urls_n:
            return False
        return True

    async def _h_wr(self, parsed, event, platform) -> Reply:
        pass

        q = parse_wr(parsed.content)
        if not q.weapon:
            return Reply(
                raw_text="用法：wr 武器名 [最新|离线] [r槽/-槽/角槽] [1000p] "
                "[零洗/低洗/废洗/N洗] [词条连写如:基多暴负变焦] [2+|3+1] [-r]"
            )
        weapon = await self.client.resolve_riven_weapon(q.weapon)
        if not weapon:
            tips = await self.client.suggest_riven_weapons(q.weapon)
            tip = ("，你是不是想找：" + "、".join(tips)) if tips else ("，请使用英文名或补充别名表")
            return Reply(raw_text=f"未找到紫卡武器「{q.weapon}」{tip}")
        weapon = await riven_market_weapon(self.client, q.weapon, weapon)
        url_name = weapon["url_name"]
        rtype = weapon.get("riven_type", "")
        positives = self.client.normalize_riven_stats(q.stats, rtype)
        negatives = self.client.normalize_riven_stats(q.negatives, rtype)
        auctions = await self.client.wm_riven_auctions(
            url_name,
            platform,
            positives=positives,
            negatives=negatives,
            polarity=q.polarity,
            max_price=q.max_price,
            max_rerolls=q.max_rerolls,
            min_rerolls=q.rerolls_min,
            require_negative=q.require_negative,
        )
        neg_set = set(negatives)
        pos_set = set(positives)
        pool = [a for a in auctions if self._auction_match(a, q, neg_set, pos_set)]
        # ★ 2026-10-08：可追溯日志 —— 「卡上少一条」类问题直接从日志对：
        #   API 回来多少 / 本地筛完多少 / 各状态几条（用户报障时按这个对 WM 网页）。
        _st = collections.Counter(
            ((a.get("owner") or {}).get("status") or "offline") for a in auctions
        )
        _st2 = collections.Counter(
            ((a.get("owner") or {}).get("status") or "offline") for a in pool
        )
        logger.info(
            "[sdjk] wr 挂单池：API %d 条%s → 本地筛后 %d 条%s（武器 %s，词条 %s）",
            len(auctions),
            dict(_st),
            len(pool),
            dict(_st2),
            url_name,
            sorted(pos_set),
        )
        relaxed = ""
        if not pool and auctions and (q.stats or q.negatives):
            # 严格匹配为空时分两档放宽（2026-09-24 用户报障「前排出现不匹配的
            # 项目」——旧实现直接跳到「近似匹配」，而近似排序又被渲染层按
            # 在线+价格重排，于是前排全是便宜但与词条无关的挂单）：
            #   ① 先只放宽**在线状态**：词条完全匹配但卖家离线 → 仍排前面
            #   ② 真没有完全匹配的，才按词条命中率给最接近的选项
            strict_offline = [
                a
                for a in auctions
                if self._auction_match(a, q, neg_set, pos_set, ignore_status=True)
            ]
            if strict_offline:
                pool = sorted(
                    strict_offline,
                    key=lambda a: (
                        fmt._ONLINE_RANK.get((a.get("owner") or {}).get("status") or "offline", 3),
                        a.get("buyout_price") or a.get("starting_price") or 0,
                    ),
                )
                relaxed = "offline"
        if not pool and auctions and (q.stats or q.negatives):
            # 服务端词条过滤为 OR 语义，精确匹配仍需本地二次筛；严格匹配为空时
            # 按 词条命中率+价格 给出最接近的选项，而不是一句「没有」
            def _rank(a: dict):
                item = a.get("item", {}) or {}
                attrs = item.get("attributes") or []
                urls_p = {at.get("url_name") for at in attrs if at.get("positive")}
                urls_n = {at.get("url_name") for at in attrs if not at.get("positive")}
                pos_hit = len(pos_set & urls_p)
                neg_hit = len(neg_set & urls_n)
                want_neg = 1 if (q.require_negative or q.negatives) else 0
                score = pos_hit * 4 + neg_hit * 2 + (want_neg & (1 if urls_n else 0))
                price = a.get("buyout_price") or a.get("starting_price") or 0
                # 同档内再按「在线优先 → 价格升序」（与渲染层展示口径一致）
                online = fmt._ONLINE_RANK.get((a.get("owner") or {}).get("status") or "offline", 3)
                return (-score, online, price)

            # 洗数过滤是硬条件，放宽词条时不能把它一起放开
            cand = (
                [a for a in auctions if self._rolls_ok(a, q)]
                if (q.max_rerolls is not None or q.rerolls_min is not None)
                else auctions
            )
            pool = sorted(cand, key=_rank)[: max(4, self.page_size - 4)]
            relaxed = "loose"
        wname = weapon.get("zh") or weapon.get("en") or url_name
        # 完全命中词条的 id 集合（2026-10-01 B 口径，仅排序用）：在线档优先、
        # 档内恰好在前、超集仍可翻页。未指定词条 → None（退化为现行为）。
        # 放宽档（offline/loose）走 presorted，此集合不参与。
        exact_ids = (
            {a.get("id") for a in pool if self._is_exact_match(a, q, neg_set, pos_set)}
            if (q.stats or q.negatives or q.require_negative)
            else None
        )
        title, lines, best = fmt.fmt_wr_auctions(
            wname,
            pool,
            page=parsed.page,
            page_size=max(4, self.page_size - 4),
            riven_type=rtype,
            group=weapon.get("group", ""),
            # 放宽档的排序是「词条命中率优先」，必须原样保留 —— 渲染层默认
            # 按 在线+价格 重排会把命中的挂单冲散
            presorted=bool(relaxed),
            exact_ids=exact_ids,
        )
        if weapon.get("_fuzzy_from"):
            lines.insert(0, f"※ 「{weapon['_fuzzy_from']}」按「{weapon.get('zh') or wname}」查询")
        _off = [a for a in pool if ((a.get("owner") or {}).get("status") or "offline") == "offline"]
        if _off:
            lines.append(f"※ 含 {len(_off)} 条离线挂单（已排在在线之后）")
        if relaxed == "offline":
            lines.append("※ 完全符合词条的挂单卖家目前都不在线，已按 在线优先 → 价格升序 列出")
        elif relaxed == "loose":
            lines.append("※ 没有完全符合条件的挂单，以上按词条命中率与价格给出最接近选项")
        reply = Reply(
            title, lines, footer=fmt.fmt_platform_footer(platform, "warframe.market 紫卡")
        )
        if not parsed.whisper:
            reply.lines.append("※ 加 -r 生成游戏内私聊密语（与卖家的 /w 消息）")
        if parsed.whisper and best:
            item = best.get("item", {}) or {}
            price = best.get("buyout_price") or best.get("starting_price")
            reply.whisper.append(
                f"/w {best.get('owner', {}).get('ingame_name', '?')} Hi! I want to buy: "
                f'"{item.get("name", weapon.get("url_name"))}" for {price} platinum. (warframe.market)'
            )
        return reply

    # ------------------------------------------------------------------
    # 排行 / 趋势 / 开核桃（P1 批次）
    # ------------------------------------------------------------------
    _RANK_CN = ("甲", "卡", "部件", "赋能", "主武", "副武", "近战", "遗物")

    @staticmethod
    def _rank_guide(platform) -> Reply:
        """榜单未建立时的引导卡。"""
        return Reply(
            "价格排行",
            [
                "◆ 全量价格榜单还未建立",
                "　发「排行 刷新」启动后台抓取",
                "　（WM 全量约 3800 项、限速约 20 分钟，期间排行照常可查）",
                "　完成后：「排行」看 甲/武器/卡 三榜，「排行 分类」看 20 名完整榜",
            ],
            footer=fmt.fmt_platform_footer(platform, "warframe.market"),
        )

    async def _h_rank(self, parsed, event, platform) -> Reply:
        """价格排行：落盘全量榜，按当前成交中位价降序。"""
        content = parsed.content_str or ""
        if "刷新" in content and (parsed.preset or "") != "紫卡":
            started, done, total = self.client.start_rank_crawl()
            state = "已启动新一轮全量抓取" if started else "抓取已在进行中"
            return Reply(
                "排行刷新",
                [
                    f"◆ {state}（WM 全量 {total or '约 3800'} 项，限速约 20 分钟）",
                    f"　当前进度：{done}/{total or '?'}",
                    "※ 抓取期间排行照常可查（显示已完成部分）；可稍后重发本指令看进度",
                ],
                footer=fmt.fmt_platform_footer(platform, "warframe.market"),
            )
        cat = parsed.preset or ""
        if not cat:
            for t in parsed.content or []:
                c = t.replace("排行", "").strip()
                if c in self._RANK_CN or c == "紫卡":
                    cat = c
                    break
            else:
                cat = content.strip()
        if cat == "紫卡":
            return await self._h_riven_rank(parsed, platform)
        if not cat:
            rows = self.client.rank_rows()
            if not rows:
                return self._rank_guide(platform)
            title, lines = fmt.fmt_rank_overview(rows)
            return Reply(
                title, lines, footer=fmt.fmt_platform_footer(platform, "warframe.market 成交")
            )
        if cat not in self.client.RANK_CATEGORIES:
            return Reply(
                raw_text=f"未知分类「{cat}」。可选："
                + " / ".join(self._RANK_CN)
                + "；紫卡排行请发「紫卡排行」"
            )
        rows = self.client.rank_rows()
        if not rows:
            return self._rank_guide(platform)
        title, lines = fmt.fmt_rank_table(cat, rows)
        return Reply(title, lines, footer=fmt.fmt_platform_footer(platform, "warframe.market 成交"))

    async def _h_riven_rank(self, parsed, platform) -> Reply:
        """紫卡热度排行：DE 官方周报（0洗/已洗 中位价与热度，周更）。"""
        content = (parsed.content_str or "").strip()
        try:
            snap = await self.client.de_weekly_rivens(platform, force="刷新" in content)
        except Exception as e:  # noqa: BLE001
            return Reply(raw_text=f"紫卡周报暂时拉取失败：{e}")
        zhm = {
            w.get("en", "").lower(): (w.get("zh") or "")
            for w in await self.client.wm_riven_weapons()
        }
        cat = ""
        for t in parsed.content or []:
            k = t.replace("排行", "").strip()
            if k in fmt._RIVEN_ITEM_TYPE or k in ("主武", "副武", "未开"):
                cat = k
                break
        if not cat:
            title, lines = fmt.fmt_riven_weekly(snap, zhm)
            return Reply(title, lines, footer=fmt.fmt_platform_footer(platform, "DE 官方周报"))
        veiled = await self.client.wm_veiled_stats(platform) if cat == "未开" else None
        title, lines = fmt.fmt_riven_type(
            cat, snap, zhm, page=parsed.page, page_size=self.page_size, veiled_wm=veiled
        )
        return Reply(title, lines, footer=fmt.fmt_platform_footer(platform, "DE 官方周报"))

    async def _h_trend(self, parsed, event, platform) -> Reply:
        """物品价格趋势（48h 逐小时 + 90d 逐日）。"""
        name = parsed.content_str.strip()
        if not name:
            return Reply(
                raw_text="用法：趋势 物品名（也支持「wm趋势 物品名」）\n"
                "　例：趋势 膛线　｜　趋势 绝路Prime"
            )
        item = await self.client.resolve_wm_item(name)
        if not item:
            return await self._wm_suggest(name)
        stats = await self.client.wm_statistics(item["url_name"], platform)
        summary = self.client.summarize_stats(stats)
        display = item.get("zh") or item.get("en") or item["url_name"]
        title, lines = fmt.fmt_trend(display, stats, summary)
        return Reply(title, lines, footer=fmt.fmt_platform_footer(platform, "warframe.market 统计"))

    async def _h_rm(self, parsed, event, platform) -> Reply:
        reply = await self._h_wr(parsed, event, platform)
        # 2026-09-14 实测：riven.market 后端（Firebase riven-market）已停用
        # （423 "database has been deactivated"，拍卖页 404），站点只剩静态页，
        # 没有可用 API。紫卡数据统一走 warframe.market 的紫卡拍卖接口，
        # 这行提示同步改掉，免得用户以为只是"没接线"。
        if reply.title:
            reply.footer = "riven.market 已停服（后端数据库停用），紫卡数据走 warframe.market 拍卖"
        return reply

    @staticmethod
    def _rolls_ok(a: dict, q) -> bool:
        """洗数硬条件（放宽词条匹配时单独保留）。"""
        r = fmt._riven_rolls(a)
        if q.rerolls_min is not None and r < q.rerolls_min:
            return False
        if q.max_rerolls is not None and r > q.max_rerolls:
            return False
        return True

    @staticmethod
    def _auction_match(
        a: dict, q, neg_set: set, pos_set: Optional[set] = None, *, ignore_status: bool = False
    ) -> bool:
        item = a.get("item", {}) or {}
        attrs = item.get("attributes") or []
        pos = [at for at in attrs if at.get("positive")]
        neg = [at for at in attrs if not at.get("positive")]
        # 价格本地过滤（2026-10-01）：WM auctions/search 实测忽略 price_min/price_max
        # 参数（服务端返回与基线逐字节同构）⇒ 此前「1000p 以内」这类条件静默不生效。
        # 拍卖一口价优先（buyout_policy=direct 恒有 buyout），无 buyout 用起始价兜底。
        price = a.get("buyout_price")
        if price is None:
            price = a.get("starting_price") or 0
        if q.max_price is not None and price > q.max_price:
            return False
        if q.min_price is not None and price < q.min_price:
            return False
        if q.forbid_negative and neg:
            return False
        if q.require_negative and not neg:
            return False
        if q.positive_count is not None and len(pos) != q.positive_count:
            return False
        if q.negative_count is not None and len(neg) != q.negative_count:
            return False
        if pos_set:
            urls = {at.get("url_name") for at in pos}
            if not pos_set <= urls:
                return False
        if neg_set:
            nurls = {at.get("url_name") for at in neg}
            if not neg_set <= nurls:
                return False
        rerolls = fmt._riven_rolls(a)  # WM 字段名是 re_rolls，取错会恒为 0
        if q.rerolls_min is not None and rerolls < q.rerolls_min:
            return False
        if q.max_rerolls is not None and rerolls > q.max_rerolls:
            return False
        if ignore_status:
            # 放宽「在线状态」单独一档（词条条件仍然全保留）——完全匹配但卖家
            # 离线，也比「词条不匹配的在线单」值得排在前面（2026-09-24）。
            return True
        status = (a.get("owner", {}) or {}).get("status")
        # ★ 2026-10-08（用户口径）：**默认不再把离线挂单藏起来** ——
        #   「即使某个词条的在线挂单只有一张，也要把离线的也列出来」。旧行为只在
        #   「完全匹配的在线单为 0」时才走 relaxed=offline 兜底 ⇒ 像「伯斯顿 + 弱点暴击几率」
        #   那种「唯一一张还离线」的场景直接空卡。现在默认（recent/在线）= 全部状态，
        #   靠渲染层「在线优先」把离线排到后面并标 ⚫离线；只有显式「最新」才只留游戏中。
        if q.status == "latest" and status != "ingame":
            return False
        return True

    async def _h_ducats(self, parsed, event, platform) -> Reply:
        """杜卡德垃圾榜：按「杜卡德/白金」排序，数据取自 WM 官方计算器。

        以前是逐项查订单自己算，只能抽样十几个，排名和官网对不上；
        现在直接接 WM 的 tools/ducats，与网页版同一份数据。
        """
        pass

        tier = (
            parsed.preset
            or next((t for t in parsed.content if t in ("金", "银", "铜")), None)
            or (
                "金"
                if parsed.command_raw in ("金垃圾",)
                else "银"
                if parsed.command_raw in ("银垃圾",)
                else "铜"
                if parsed.command_raw in ("铜垃圾",)
                else "金"
            )
        )
        want = getattr(fmt, "_DUCAT_TIER", {}).get(tier, {}).get("values") or (100,)

        board = await self.client.ducats_board()
        if board:
            pool = [r for r in board if r["ducats"] in want]
        else:
            pool = await self._ducats_fallback(tier, want, platform)
        total = len(pool)
        page_size = self.page_size
        pages = max(1, (total + page_size - 1) // page_size)
        page = max(1, min(parsed.page, pages))
        chunk = pool[(page - 1) * page_size : page * page_size]
        title, lines = fmt.fmt_ducat_junk(tier, chunk, page=page, pages=pages)
        if pages > 1:
            lines.append(f"※ 第{page}/{pages}页，共{total}件；加 -2 / -3 翻页")
        return Reply(
            title, lines, footer=fmt.fmt_platform_footer(platform, "杜卡德/白金 越高越值得换")
        )

    async def _ducats_fallback(self, tier: str, want: tuple, platform: str) -> list[dict]:
        """tools/ducats 不可用时的兜底：自己查订单算，样本有限。

        Args:
            tier: 档位，仅用于日志与提示。
            want: 该档位对应的杜卡德值集合。
            platform: 平台码。

        Returns:
            与 ``ducats_board`` 同结构的榜单。
        """
        wm_items = await self.client.wm_items()
        cands = [x for x in wm_items if (x.get("ducats") or 0) in want]
        cands.sort(key=lambda x: x.get("url_name", ""))
        if not cands:
            return []
        step = max(1, len(cands) // 40)  # 兜底才抽样，正路走全量榜单
        pool = cands[::step][:40]
        results = await self._gather([self.client.wm_orders(x["url_name"], platform) for x in pool])
        rows = []
        for it, res in zip(pool, results):
            if isinstance(res, Exception) or not isinstance(res, tuple):
                continue
            sells = [
                o
                for o in res[0]
                if o.get("order_type") == "sell"
                and o.get("platform", platform) == platform
                and (o.get("platinum") or 0) > 0
            ]
            if not sells:
                continue
            cheapest = min(o["platinum"] for o in sells)
            ducats = it.get("ducats") or 0
            rows.append(
                {
                    "name": it.get("zh") or it.get("en") or it["url_name"],
                    "ducats": ducats,
                    "dpp": ducats / cheapest,
                    "dpp_wa": 0.0,
                    "plat": float(cheapest),
                    "volume": 0,
                }
            )
        rows.sort(key=lambda r: -r["dpp"])
        return rows

    @staticmethod
    def _junk_list(tier: str) -> list:
        try:
            data = json.loads(JUNK_FILE.read_text(encoding="utf-8"))
            return data.get(tier) or []
        except Exception:  # noqa: BLE001
            return []
