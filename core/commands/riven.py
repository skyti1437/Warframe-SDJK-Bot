# -*- coding: utf-8 -*-
"""紫卡域指令（D7 自 main.py 迁入）。

覆盖路由键 3 项：analysis / disposition / xh；模块级 _xh_element 随迁。
⚠ 已披露改写（本笔唯一非逐字点）：_h_riven_analysis 体内函数内相对导入
try 分支 from .core.* → from ..*（模块自插件根迁入 core 包，相对基准
随之调整，导入目标不变）；except 兜底与注释逐字保留。
子包纪律：不 import astrbot（事件对象鸭子类型）。
"""

from __future__ import annotations

import re
import unicodedata
from typing import Optional

from .. import formatters as fmt
from .. import matching
from .. import riven_analysis as RA
from ..api_client import WarframeAPIError, fuzzy_hits, public_error_text
from ..logging_compat import logger
from .base import Reply, err_card

# 「词条范围」组合写法的归一：全角数字 / 加号走 NFKC，U+2212 减号另补一刀
_COMBO_TRANS = str.maketrans({"−": "-", "－": "-"})
# 「看着像组合」但不认识（如 4+ / 5+1-）：**不静默落进武器名**，给一句提示
_COMBO_LIKE_RE = re.compile(r"^\d+\+(?:\d+)?(?:-|负)?$")


def _fold_combo(t: str) -> str:
    """组合写法归一：全角 → 半角（NFKC），减号统一成 ASCII ``-``。"""
    return unicodedata.normalize("NFKC", t or "").translate(_COMBO_TRANS)


def _xh_element(toks: list[str]) -> tuple[Optional[str], Optional[str]]:
    """从参数里认元素，返回 (中文名, WM 英文值)。

    认中英文全称与常见单字/简写（辐射 / radiation / 辐）；认不出返回 (None, None)。
    """
    for t in toks:
        s = (t or "").strip().lower()
        if s in fmt.LICH_ELEM_EN:  # 中文全称
            return s, fmt.LICH_ELEM_EN[s]
        if s in fmt.LICH_ELEM_CN:  # 英文
            return fmt.LICH_ELEM_CN[s], s
        if s in fmt.LICH_ELEM_ALT:  # 单字 / 简写
            cn = fmt.LICH_ELEM_ALT[s]
            return cn, fmt.LICH_ELEM_EN[cn]
    return None, None


class RivenCommands:
    """Mixin：紫卡分析 / 倾向 / 玄骸拍卖 handler（挂载于 main.WarframeSDJK）。"""

    async def _h_xh(self, parsed, event, platform) -> Reply:
        """玄骸拍卖查询（WM lich auctions）。

        覆盖**三类**：赤毒 Kuva（type=lich）/ 信条 Tenet（type=sister）/
        科达 Coda（type=coda，WM 未开放则提示无挂单）。
        1986 年这个指令只查了 lich，信条武器必然「未找到」—— 2026-09-18 修。

        分支筛选：``xh 武器名 [元素] [数值]``
          · 元素：中文（辐射/毒素/火焰…）或英文（radiation/toxin…）
          · 数值：伤害加成下限（如 ``50`` 表示只要 ≥50%）
        """
        toks = parsed.content or []
        if not toks:
            return err_card(
                "没有写武器名",
                "按 xh 武器名 [元素] [数值] 的格式重发，例如「xh 赤毒怒雷」",
                ("用法", "xh 武器名 [元素] [数值]"),
                "· 示例　xh 赤毒怒雷 ｜ xh 信条弧电离子枪 辐射 ｜ xh 赤毒海克 50",
                ("元素", "磁力/电击/毒素/火焰/冰冻/冲击/切割/辐射"),
                (
                    "已收录",
                    f"{len(self.client._aliases.get('lich_items', {}))} 个中文写法"
                    "（赤毒/信条/科达三类）",
                ),
                title="玄骸拍卖",
            )
        first = toks[0]
        slug = self.client.resolve_lich_weapon(first) or await self._lich_slug_by_riven(first)
        if not slug:
            near = fuzzy_hits(first, list(self.client._aliases.get("lich_items", {})), n=3) or []
            return err_card(
                f"未找到玄骸武器「{first}」",
                "支持赤毒/信条/科达三类，可只写后半段（如「怒雷」）；或照候选改名重发",
                ("你是不是想找", "、".join(near)) if near else "",
                title="玄骸拍卖",
            )

        info = self.client.lich_weapon_info(slug)
        name = info.get("zh") or slug
        kind = info.get("type") or "lich"
        toks_tail = toks[1:]
        want_eph = any("幻纹" in t for t in toks_tail)
        elem_cn, elem_en = _xh_element(toks_tail)
        min_dmg = next(
            (
                int(t.rstrip("%"))
                for t in toks_tail
                if t.rstrip("%").isdigit() and 1 <= int(t.rstrip("%")) <= 100
            ),
            None,
        )

        # 表中已标注 wm=False 的（逐把核对过），直接走「无类目」分支，省一次注定 400 的请求
        if info.get("wm") is False:
            return self._xh_no_category(name, slug, kind, platform)
        try:
            auctions = await self.client.wm_lich_auctions(slug, platform, lich_type=kind)
        except WarframeAPIError as exc:
            # 市场侧失败要说清原因（限速/网络），不能糊成「内部错误」；原文只进日志
            logger.warning("[sdjk] 玄骸拍卖查询失败（%s）：%s", slug, exc)
            return err_card(
                f"warframe.market 查询失败：{public_error_text(exc)}",
                "多为市场限速（429）或网络抖动，稍等一两分钟重试即可",
                title="玄骸拍卖",
            )
        pool = auctions
        if want_eph:
            pool = [a for a in pool if (a.get("item") or {}).get("having_ephemera")]
        if elem_en:
            pool = [a for a in pool if (a.get("item") or {}).get("element") == elem_en]
        if min_dmg is not None:
            pool = [a for a in pool if int((a.get("item") or {}).get("damage") or 0) >= min_dmg]

        # 排序：**在线优先**（能立刻交易）→ 伤害降序 → 价格升序
        def _rank(a: dict):
            o = a.get("owner") or {}
            it = a.get("item") or {}
            on = {"ingame": 0, "online": 1}.get(str(o.get("status") or ""), 2)
            price = a.get("buyout_price") or a.get("starting_price") or 999999
            return (on, -int(it.get("damage") or 0), price)

        pool = sorted(pool, key=_rank)

        filters = []
        if elem_cn:
            filters.append(elem_cn)
        if min_dmg is not None:
            filters.append(f"伤害≥{min_dmg}%")
        if want_eph:
            filters.append("带幻纹")
        title = (
            f"{name} 玄骸拍卖（{len(pool)}条" + ("，" + "·".join(filters) if filters else "") + "）"
        )
        if not pool:
            if self.client.lich_unsupported(slug):
                return self._xh_no_category(name, slug, kind, platform)
            elif not auctions:
                lines = [
                    "◆ 原因：该武器当前没有挂单（冷门武器挂单少，可过段时间再看）",
                    "※ ⚠ 怎么改：过段时间重发；或换一把同系列武器查",
                ]
            else:
                lines = [
                    "◆ 原因：暂无符合条件的挂单"
                    + (f"（筛选：{'·'.join(filters)}）" if filters else ""),
                    "※ ⚠ 怎么改：去掉元素 / 数值 / 幻纹筛选词，或放宽数值下限后重发",
                ]
            return Reply(
                title,
                lines,
                footer=fmt.fmt_platform_footer(platform, "warframe.market 玄骸"),
            )
        lines = fmt.fmt_lich_table(pool[:12], total=len(pool))
        return Reply(title, lines, footer=fmt.fmt_platform_footer(platform, "warframe.market 玄骸"))

    def _xh_no_category(self, name: str, slug: str, kind: str, platform: str) -> Reply:
        """WM 没有该武器的拍卖类目 —— 给**替代路径**，而不是只说「查不到」。

        ★ 2026-09-18 逐把核对过全部 47 把（`lich_weapons.json` 的 ``wm`` 字段）：
          30 把有类目、17 把没有（全部终幕 Coda + 5 把近战/异形信条）。
          用户看到「0 条」时最容易以为插件坏了，所以要写清「是市场没有，
          不是我们没查到」，并给出可操作的替代。
        """
        db = self.client._lich_db()
        has = sum(1 for r in db.values() if r.get("wm") is True)
        total = len(db)
        sibling = {"sister": "信条", "lich": "赤毒", "coda": "终幕"}.get(kind, "")
        example = {
            "sister": "xh 信条弧电离子枪",
            "lich": "xh 赤毒怒雷",
            "coda": "xh 信条弧电离子枪",
        }.get(kind, "xh 信条弧电离子枪")
        return Reply(
            f"{name} 玄骸拍卖（市场无此类目）",
            [
                f"◆ 原因：warframe.market 没有「{name}」的拍卖类目",
                f"◆ 说明：这不是识别失败——中文名已正常匹配到 {slug}，是市场侧没这个类目"
                f"（已逐把核对全部 {total} 把玄骸武器：{has} 把有挂单、{total - has} 把没有）",
                "◆ 替代路径",
                f"· 换一把同系列　发「{example}」",
                f"· 网站自查　warframe.market/zh-hans/auctions/search"
                f"?type={kind}&weapon_url_name={slug}",
                f"· 游戏内交易　这类武器（全部终幕 + 部分近战{sibling}）只能游戏内交易频道收",
            ],
            footer=fmt.fmt_platform_footer(platform, "warframe.market 玄骸"),
        )

    async def _lich_slug_by_riven(self, q: str) -> Optional[str]:
        """黑话兜底：尝试从 riven 别名表取基础武器 slug 前缀匹配玄骸。"""
        hit = self.client.alias_lookup(q.lower(), "riven_items")
        return f"kuva_{hit.split('_prime')[0]}" if hit and hit.split("_prime")[0] else None

    async def _h_disposition(self, parsed, event, platform) -> Reply:
        """紫卡倾向查询。

        ★ 2026-10 DE 取消倾向：所有武器统一 ``RA.UNIFIED_DISPOSITION``（1.2）。表里的
        ``disposition`` 是取消前的旧值（wiki 静态表覆盖 WM，不再重抓），只用来告诉玩家
        已有紫卡数值缩放了多少（新值 = 旧值 × 1.2 / 原倾向）。
        """
        query = parsed.content_str
        weapons = await self.client.wm_riven_weapons()
        unified = RA.UNIFIED_DISPOSITION
        head = f"◆ DE 已取消紫卡倾向：所有武器统一按 {unified:.2f}（{fmt._disp_dots(unified)}）计算"
        note = "◆ 已有紫卡词条不变，数值按「1.2 / 原倾向」缩放；原倾向越低涨得越多"
        if not query:
            old = sorted(
                [w for w in weapons if w.get("disposition")], key=lambda w: w["disposition"]
            )

            def _row(w: dict) -> str:
                return (
                    f"· {(w.get('zh') or w.get('en') or w['url_name'])}　"
                    f"{w['disposition']:.2f}　×{unified / w['disposition']:.2f}"
                )

            lines = [head, note, "◆ 涨幅最大 Top8（原倾向最低）", "　武器　原倾向　数值缩放"]
            lines += [_row(w) for w in old[:8]]
            lines += ["◆ 降幅最大 Top5（原倾向最高）"] + [_row(w) for w in old[::-1][:5]]
            lines.append("◆ 查单把武器：紫卡倾向 武器名")
            return Reply("紫卡倾向（已统一 1.2）", lines, footer=fmt.fmt_platform_footer(platform))
        hits, stage = matching.resolve_weapon_name(
            query, weapons, zh="zh", en="en", slug="url_name"
        )
        if not hits:
            # 紫卡黑话别名兜底（riven_items 词库），命中优先级低于官方名各层
            aurl = self.client.alias_lookup(query.lower(), "riven_items")
            if aurl:
                hits = [w for w in weapons if w.get("url_name") == aurl]
                stage = "alias"
        if not hits:
            close = matching.suggest_zh(query, weapons)
            return err_card(
                "未找到该武器",
                "照候选改名重发" if close else "换官方中文名或英文名重发",
                ("你是不是想找", "、".join(close)) if close else "",
                title="紫卡倾向",
            )
        logger.info(
            "[sdjk] 倾向武器解析：%s → %s（%s）", query, "/".join(matching.zh_names(hits)), stage
        )
        # 只报本体名（无变体意图）→ 列出全部变体家族（本体在前），
        # 对齐 Warframe Rabbit 的家族卡；显式变体查询（绝路p/赤毒沙皇）不展开
        nq = matching.normalize(query)
        if (
            len(hits) == 1
            and not matching.variant_intent(query)
            and not any(t in nq for t in matching.VARIANT_TOKENS)
        ):
            fam = matching.family_of(hits[0], weapons)
            if len(fam) > 1:
                hits = fam
        # ★ 2026-09-27：Vandal/Wraith 这类变体在倾向数据里 group/riven_type 是
        #   空的（49 条）⇒ 类别列会空着。家族卡里用**家族内第一个有类别的成员**
        #   兜底（同一家族类别相同；不能取 hits[0] —— 家族排序把「MK1-布莱顿」
        #   这类 _is_base 认不出的变体排在了本体前面，取它只会拿到空值）。
        base_w = next((w for w in hits if (w.get("riven_type") or w.get("group"))), {})
        base_rt = base_w.get("riven_type", "")
        base_gp = base_w.get("group", "")

        def _cls(w: dict) -> str:
            rt, gp = w.get("riven_type", ""), w.get("group", "")
            if not rt and not gp:
                rt, gp = base_rt, base_gp
            return fmt.riven_type_cn(rt, gp)

        def _scale(w: dict) -> str:
            d = float(w.get("disposition") or 0)
            return f"{d:.2f}　×{unified / d:.2f}" if d > 0 else "—　—"

        # 卡首答案：查询词对应的第一个（本体）；家族多于 1 个再列表（各自原倾向不同，缩放也不同）
        _h0 = hits[0]
        _d0 = float(_h0.get("disposition") or 0)
        lines = [
            f"◆ {(_h0.get('zh') or _h0.get('en') or _h0['url_name'])}："
            f"倾向 {unified:.2f}（{fmt._disp_dots(unified)}）｜{_cls(_h0)}",
            f"◆ 原倾向 {_d0:.2f} → 已有紫卡数值 ×{unified / _d0:.2f}"
            if _d0 > 0
            else "◆ 原倾向：无数据",
        ]
        if len(hits) > 1:
            lines.append(f"◆ 同家族（共 {len(hits)} 种）")
            lines.append("　武器　原倾向　数值缩放　类别")
            lines += [
                f"· {(w.get('zh') or w.get('en') or w['url_name'])}　{_scale(w)}　"
                # ★ 2026-09-24：带上 group —— WM 把曲翼枪械的 rivenType 也标成
                #   rifle（翠雀显示成「步枪」），group 才是准的（曲翼枪械/守护武器）
                f"{_cls(w)}"
                for w in hits[:8]
            ]
        lines.append(head)
        return Reply(f"紫卡倾向：{query}", lines, footer=fmt.fmt_platform_footer(platform))

    # 「词条范围」的组合写法（用户口径：不写组合默认 3正1负）
    # 键 = **_fold_combo 归一后**的写法：`2+1` / `2+1-` / `2+1负` 三者同义（3+1 同）。
    _RANGE_COMBOS = {
        "2+": (2, 0),
        "2+1": (2, 1),
        "2+1-": (2, 1),
        "2+1负": (2, 1),
        "2正": (2, 0),
        "2正1负": (2, 1),
        "3+": (3, 0),
        "3+1": (3, 1),
        "3+1-": (3, 1),
        "3+1负": (3, 1),
        "3正": (3, 0),
        "3正1负": (3, 1),
    }

    # 「紫卡融合」的武器类写法 → weapon_class 列名
    _SPLICE_CLASSES = {
        "步枪": "rifle",
        "霰弹": "shotgun",
        "霰弹枪": "shotgun",
        "手枪": "pistol",
        "副武": "pistol",
        "空战": "archgun",
        "曲翼": "archgun",
        "近战": "melee",
    }

    async def _h_riven_splice(self, parsed, event, platform) -> Reply:
        """紫卡融合 / 紫卡拼接：Riven Splicer 每个融合词条由哪两条原词条拼成（2026-10-24 用户）。

        用法：紫卡融合 [武器名 | 步枪/霰弹枪/手枪/空战/近战] [词条名]
        不带参数 = 全部 22 条配方；带武器（或武器类）只列该类能融合的；带词条只列含该词条的
        （作为原词条或融合结果都算）。
        """
        try:  # 服务器以包成员加载，相对导入才可靠（同 _h_riven_analysis）
            from .. import riven_analysis as RA
            from ..parser import RIVEN_STAT_ZH, _STAT_LOOKUP
        except ImportError:  # pragma: no cover - 本地直跑
            from core import riven_analysis as RA
            from core.parser import RIVEN_STAT_ZH, _STAT_LOOKUP

        cls = None
        scope = ""
        stat = None
        weapon_toks: list[str] = []
        for tok in parsed.content or []:
            t = (tok or "").strip()
            if not t:
                continue
            if t in self._SPLICE_CLASSES:
                cls = self._SPLICE_CLASSES[t]
                scope = t
                continue
            sid = _STAT_LOOKUP.get(t) or _STAT_LOOKUP.get(t.casefold())
            if sid and stat is None:
                stat = sid
                continue
            weapon_toks.append(t)
        query = " ".join(weapon_toks).strip()
        if query:
            try:
                weapon = await self.client.resolve_riven_weapon(query)
            except Exception:  # noqa: BLE001 - 认不出走下面的提示
                weapon = None
            if not weapon:
                tips = await self.client.suggest_riven_weapons(query)
                return err_card(
                    f"未找到紫卡武器或词条「{query}」",
                    "照候选改名重发"
                    if tips
                    else "换官方中文名、武器类（步枪/霰弹枪/手枪/空战/近战）或词条名重发",
                    ("你是不是想找", "、".join(tips)) if tips else "",
                    title="紫卡融合",
                )
            cls = RA.weapon_class(weapon.get("riven_type", ""), weapon.get("group", ""))
            cls = RA.kitgun_mode_class(
                weapon.get("en") or weapon.get("zh") or "", cls, weapon.get("riven_type") or ""
            )
            name = weapon.get("zh") or weapon.get("en") or query
            scope = f"{name}（{fmt._SPLICE_CLASS_ZH.get(cls, cls)}）"
        recipes = RA.splice_recipes(cls, stat)
        title, lines = fmt.fmt_riven_splice(
            recipes, scope=scope, stat_zh=RIVEN_STAT_ZH.get(stat, "") if stat else ""
        )
        return Reply(title, lines, footer=fmt.fmt_platform_footer(platform, "Riven Splicer 配方"))

    async def _h_riven_ranges(self, parsed, event, platform) -> Reply:
        """词条范围：按武器列出紫卡每条词条的卡面取值区间（只显示统一倾向 1.2）。

        用法：词条范围 武器名 [2+ | 2+1 | 2+1- | 3+ | 3+1 | 3+1-] [词条名…]
        不写组合默认 3正1负（`2+1` = `2+1-`，`3+1` = `3+1-`）；带上词条名就只列那几条。
        **按武器过滤**：该武器刷不出的词条不列（逐家族证据 > 武器自身推导 > 职业池）。
        """
        try:  # 服务器以包成员加载，相对导入才可靠（同 _h_riven_analysis）
            from .. import riven_pools as RP
            from .. import riven_analysis as RA
            from ..parser import RIVEN_STAT_ZH, _STAT_LOOKUP
        except ImportError:  # pragma: no cover - 本地直跑
            from core import riven_pools as RP
            from core import riven_analysis as RA
            from core.parser import RIVEN_STAT_ZH, _STAT_LOOKUP

        combo = (3, 1)
        stats: list[str] = []
        weapon_toks: list[str] = []
        bad_combo = ""
        for tok in parsed.content or []:
            t = (tok or "").strip()
            if not t:
                continue
            nt = _fold_combo(t)
            if nt in self._RANGE_COMBOS:
                combo = self._RANGE_COMBOS[nt]
                continue
            if _COMBO_LIKE_RE.match(nt):
                # 认识不出但**长得像组合**：别再当武器名的一部分（维护者 2026-10-24）
                bad_combo = bad_combo or t
                continue
            sid = _STAT_LOOKUP.get(t) or _STAT_LOOKUP.get(t.casefold())
            if sid:
                stats.append(sid)
                continue
            weapon_toks.append(t)
        if bad_combo:
            return err_card(
                f"组合「{bad_combo}」不认识",
                "组合只有 3+1- / 3+ / 2+1- / 2+（也认 2+1 / 3+1 / 2+1负 / 3+1负）。"
                "例如「词条范围 冰凇 2+1」",
                ("用法", "词条范围 武器名 [2+ / 2+1 / 2+1- / 3+ / 3+1 / 3+1-] [词条名…]"),
                title="紫卡词条范围",
            )
        query = " ".join(weapon_toks).strip()
        if not query:
            return err_card(
                "没有写武器名",
                "按 词条范围 武器名 [组合] [词条名…] 的格式重发，例如「词条范围 绝路Prime」"
                "「词条范围 冰凇 3+」「词条范围 绝路 基伤 多重 毒气 3+1-」",
                ("用法", "词条范围 武器名 [2+ / 2+1 / 2+1- / 3+ / 3+1 / 3+1-] [词条名…]"),
                title="紫卡词条范围",
            )
        item = await self.client.resolve_riven_weapon(query)
        if not item:
            return await self._wm_suggest(query)
        display = item.get("zh") or item.get("en") or query
        cls = RA.weapon_class(item.get("riven_type") or "", item.get("group") or "")
        root, members = RP.family_members(item.get("en") or "")
        pos, unc = RP.available(cls, root, members, positive=True)
        neg, _ = RP.available(cls, root, members, positive=False)
        # 融合词条单列一段（同一张卡最多 1 项）
        pool = RP.class_pool(cls)
        spliced = [s for s in pos if (pool.get(s) or {}).get("spliced")]
        pos = [s for s in pos if s not in spliced]
        only = stats or None
        dropped: list[str] = []
        if only:
            keep = set(stats)
            avail = set(pos) | set(spliced) | set(neg)
            dropped = [RIVEN_STAT_ZH.get(s, s) for s in stats if s not in avail]
            pos = [s for s in pos if s in keep]
            spliced = [s for s in spliced if s in keep]
            neg = [s for s in neg if s in keep]
        title, lines = fmt.fmt_riven_ranges(
            display, cls, combo, pos, spliced, neg, unc, only=only, dropped=dropped
        )
        if RA.class_estimated(item.get("riven_type") or "", item.get("group") or ""):
            lines.insert(0, "※ ⚠ 武器类别未知，按步枪基值列估算（区间可能有偏差）")
        return Reply(title, lines, footer=fmt.fmt_platform_footer(platform, "紫卡词条池"))

    async def _h_riven_analysis(self, parsed, event, platform) -> Reply:
        import time as _tt

        _t_start = _tt.perf_counter()
        """紫卡分析：按 DE 属性基值 × 倾向 × 词条数系数算每条词条的取值区间，
        标出实际数值是高卷还是低卷。

        用法：紫卡分析 武器名 暴伤82.8 范围1.6 攻速45.8 负滑暴81.3
        也可直接发「紫卡分析 + 紫卡截图」（vision 渠道识别，如 glm-4v-flash）。
        负词条用「负」或「-」前缀标记；数值不写正负号。
        """
        import re as _re

        try:  # 服务器以包成员加载，相对导入才可靠（绝对导入会被 sys.path 清理坑掉）
            from ..parser import RIVEN_STAT_ZH
            from .. import riven_analysis as RA
        except ImportError:  # pragma: no cover - 本地直跑
            from core.parser import RIVEN_STAT_ZH
            from core import riven_analysis as RA
        rev = {v: k for k, v in RIVEN_STAT_ZH.items()}
        disp_override = 0.0  # 手输倾向（倾向0.95 / @0.95 / d0.95）
        stats_pos: list[tuple[str, float]] = []
        stats_neg: list[tuple[str, float]] = []
        weapon_name = ""
        # 融合时还没选定的词条（卡面「？？？」）：正负未知，只计入词条数（2026-10-24）
        hidden = 0
        for tok in parsed.content or []:
            t = tok.strip()
            if not t:
                continue
            if _re.fullmatch(r"[?？]{2,}", t):
                hidden += 1
                continue
            neg = t.startswith(("负", "-"))
            body = t[1:] if neg else t
            # ★ 2026-09-24：词条名放宽到 1~8 字（卡面原文「滑行攻击暴击几率」6 字，
            #   旧限 1~4 字会整条落到武器名里 → 负词条丢失、反推区间算错）
            # ★ 2026-10-02 上限 8→12 字：DE 卡面原文「的几率来获得连击数」是 9 字，
            #   旧限会让整条落进 weapon_name（静默丢词条 + 污染武器名）。
            # ★ 2026-10-23（ZCode 部署回执）：派系词条卡面原文夹英文「对Infested伤害」，
            #   只收汉字会整条落进武器名 ⇒ 放宽到汉字 + 英文字母（认不出的照旧归武器名）。
            m = _re.fullmatch(r"([\u4e00-\u9fa5A-Za-z]{1,24}?)(\d+(?:\.\d+)?)", body)
            if m:
                sid = self._stat_id_from_name(m.group(1), rev)
                if sid:
                    (stats_neg if neg else stats_pos).append((sid, float(m.group(2))))
                    continue
            if _re.fullmatch(r"\d\+(?:\d)?", t):
                continue  # 3+1 之类的词条数标注，P/N 直接按实际词条算
            m_d = _re.fullmatch(r"(?:倾向|d|@)(\d+(?:\.\d+)?)", t, _re.I)
            if m_d:
                disp_override = float(m_d.group(1))
                continue  # 手输倾向覆盖（棱晶等变体 WM 没有数据）
            weapon_name += t

        # ★ 2026-10-02 反转词条自洽改判（用户照抄卡面时不会给后坐力写「负」前缀）：
        #   4 正 0 负且其中**恰有 1 条**反转词条 ⇒ 该条实为负面（卡面 `+` 号但
        #   收益为负）。旧流程被下面的词条数闸门拒收，用户拿到「词条数不对」。
        #   （卡面注脚统一在下方的「反转词条注脚」块生成，两条路径同文案。）
        inverted_note = ""
        if len(stats_pos) > 3 and not stats_neg:
            _inv = [(i, s) for i, s in enumerate(stats_pos) if RA.is_inverted(s[0])]
            if len(_inv) == 1:
                _i, _s = _inv[0]
                stats_pos.pop(_i)
                stats_neg.append(_s)

        has_image = self._event_has_image(event)
        source_note = ""
        # ★ 2026-10-07：图片路径里 OCR 读到的武器名（清洗后）单独留一份 —— 手输名
        #   认不出时用它兜底（见下面的解析链），纯文字路径保持空串。
        _ai_name = ""
        # 近形字兜底的卡面注记（解析链最后一跳命中时填，见下）
        nearmiss_note = ""
        if not weapon_name or not (stats_pos or stats_neg):
            # —— 图片识别路径 ——
            if not has_image:
                return err_card(
                    "没有给出武器名和词条数值，也没有附紫卡截图",
                    "按用法补全重发；也可直接发「紫卡分析 + 紫卡截图」",
                    ("用法", "紫卡分析 武器名 词条数值…（负词条加「负」前缀）"),
                    "· 示例　紫卡分析 欧玛 暴伤82.8 范围1.6 攻速45.8 负滑暴81.3",
                    title="紫卡分析",
                )
            # 紫卡识图同样调用付费多模态模型（每次 2 路并行）⇒ 与识卡共用防刷闸
            _gate, _sender = self._vision_gate(event, "紫卡识图")
            if _gate is not None:
                return _gate
            _busy_key = f"riven:{getattr(event, 'unified_msg_origin', '') or ''}:{_sender}"
            self._ocr_busy.add(_busy_key)
            try:
                imgs = await self._image_data_urls(event)
                if not imgs:
                    return Reply(raw_text="图片下载失败，请重发一次截图")
                data = await self._extract_riven_from_image(imgs[0])
            finally:
                self._ocr_busy.discard(_busy_key)
            if not data:
                if _sender:  # 识别失败返还冷却（同识卡口径）
                    self._ocr_last.pop(_sender, None)
                return err_card(
                    "图片识别失败（vision 渠道不可用或未配）",
                    "请按文字格式发送：紫卡分析 武器名 暴伤82.8 范围1.6 负滑暴81.3",
                    title="紫卡分析",
                )
            stats_pos, stats_neg = self._normalize_llm_stats(data, rev)
            # ★ 2026-10-07（玩家报障「紫卡分析 冰凇 + 截图 ⇒ 未找到紫卡武器「冰松」」）：
            #   旧写法 `weapon_name = data.get("weapon") or weapon_name` = **OCR 名无条件
            #   覆盖手输名** —— OCR 吐一个近形字（冰凇→冰松）就把用户写对的名字丢掉，
            #   整卡解析失败。线上日志实证（2026-10-07 10:59 / 17:11 / 17:13 三次）：
            #   用户输入确是「紫卡分析 冰凇」，卡面读成「冰松 Visi-critapha」⇒ 回未找到。
            #   改为**手输名优先**，OCR 名只作兜底（解析链见下方 `_ai_name` 那一跳）。
            _user_name = weapon_name.strip()
            # 紫卡卡面是「武器名 + 自命名」（欧玛 Acri-loctida）：去掉拉丁
            # 尾巴只留简中母名（WM 紫卡表按母武器挂）
            _ai_raw = (data.get("weapon") or "").strip()
            _ai_name = _re.sub(r"[A-Za-z\-].*$", "", _ai_raw).strip(" ··") or _ai_raw
            # 融合未选定时自命名显示成「？？？」（视使之触？？？）：问号不是武器名
            _ai_name = _re.sub(r"[?？\s]+$", "", _ai_name) or _ai_name
            weapon_name = _user_name or _ai_name
            source_note = "（图片识别）"
            # ★ 2026-09-27：优先采信「卡面文字行」，而不是模型给的语义词条表。
            #   故障实证（服务器日志 01:11:52）：4 行卡面被吐成 7 条 —— 负词条
            #   那行拆成「触发」「持续」「触发时间」三份 + 凭空一条「滑暴」，
            #   词条数校验直接报「4 正 2 负」把整张卡挡掉。行由**窄读那一路**
            #   （`_RIVEN_LINE_PROMPT`）照抄，读全（2~3 正、≤1 负）才采信，
            #   否则退回语义 JSON 的词条表。
            _lines = data.get("lines")
            if isinstance(_lines, str):
                _lines = _lines.splitlines()
            if isinstance(_lines, (list, tuple)) and _lines:
                _lp, _ln, _legal, _notes = self._riven_lines_legal(_lines)
                hidden = RA.hidden_line_count(_lines)
                # ★ 2026-10-02 两路交叉校验（问题1）：行读**条数少于语义表**时也
                #   不许覆盖 —— 行读没报错也可能整行没抄（模型直接漏一行，不会产生
                #   notes）。但语义表自身必须**合法**才让位，否则会把 2026-09-27
                #   修掉的「语义表拆条/多条」老 bug 放回来（那次语义表 4 正 2 负）。
                _n_line = len(_lp) + len(_ln)
                _n_sem = len(stats_pos) + len(stats_neg)
                _sem_legal = bool(RA.count_candidates(len(stats_pos), len(stats_neg), hidden))
                logger.info(
                    "[sdjk] 紫卡行解析：%d 正 %d 负 %d 未揭示（模型语义表 %d 正 %d 负）%s",
                    len(_lp),
                    len(_ln),
                    hidden,
                    len(stats_pos),
                    len(stats_neg),
                    ("；跳过 " + " / ".join(_notes)) if _notes else "",
                )
                if _legal and (_n_line >= _n_sem or not _sem_legal):
                    stats_pos, stats_neg = _lp, _ln
                    source_note = "（图片识别·卡面逐行）"
                elif _legal:
                    logger.warning(
                        "[sdjk] 紫卡行读条数少于语义表（%d < %d），退回语义表防漏行",
                        _n_line,
                        _n_sem,
                    )
            if not (stats_pos or stats_neg):
                return err_card(
                    "图片识别到了武器但没读出词条",
                    "请按文字格式重发：紫卡分析 武器名 暴伤82.8 范围1.6 负滑暴81.3",
                    title="紫卡分析",
                )
            if not weapon_name:
                # ★ 2026-09-25 ruff F821 修复：原写成未定义的 `abbr`，这条路一走
                #   就 NameError（用户拿到内部错误而不是下面这条提示）。
                hint = " ".join(f"{RIVEN_STAT_ZH.get(sid, sid)}{num:g}" for sid, num in stats_pos)
                return err_card(
                    "图片识别到了词条但没读出武器名",
                    f"请按文字格式补一次：紫卡分析 武器名 {hint}",
                    title="紫卡分析",
                )

        count_cands = RA.count_candidates(len(stats_pos), len(stats_neg), hidden)
        if not count_cands or not (stats_pos or stats_neg):
            return err_card(
                "紫卡词条应为 2~3 条正面 + 0~1 条负面，"
                f"当前解析到 {len(stats_pos)} 正 {len(stats_neg)} 负"
                + (f" + {hidden} 条未揭示" if hidden else ""),
                "核对词条条数后重发（负词条加「负」前缀）；截图发错也请重发",
                title="紫卡分析",
            )
        # ★ 2026-10-02 重复词条检测：紫卡同一条词条**不会出现两次**；出现两次
        #   几乎必是模型读改字（线上实证：`+44.9% ⚡电击伤害` 被两个渠道都读成
        #   「暴击伤害」）。不静默：卡面明确标注并请重发核对 —— 错的那行会给出
        #   一段看似正常的区间，最容易误导配卡决策。
        _all_stats = stats_pos + stats_neg
        _dup_ids = sorted(
            {sid for sid, _ in _all_stats if sum(1 for s2, _v in _all_stats if s2 == sid) > 1}
        )
        dup_note = ""
        if _dup_ids:
            dup_note = (
                "⚠ 同一词条出现两次（"
                + "、".join(RIVEN_STAT_ZH.get(s, s) for s in _dup_ids)
                + "）—— 紫卡不会有重复词条，其中一条很可能是识别错误"
                "（常见：元素伤害被读成暴击伤害），请重发一次截图核对"
            )
            logger.warning("[sdjk] 紫卡识别到重复词条：%s（stats=%s）", _dup_ids, _all_stats)
        # ★ 2026-10-02 反转词条注脚（两条路径都要解释）：图片路径由 vision 按
        #   符号自动归负、人工输入由上面的自洽改判处理。行内**保留卡面符号**并
        #   带「卡面+号·负面」标签（见 fmt_riven_analysis），这里再解释机理，
        #   避免「卡面 + 号」与「分析按负面算」看起来矛盾（用户口径：
        #   +后坐力 = 增加后坐力 = 负面；-后坐力 = 减少 = 正面）。
        if not inverted_note:
            if any(RA.is_inverted(s) for s, _ in stats_neg):
                inverted_note = (
                    "「后坐力」是反转词条：+ 号 = 增加后坐力 = "
                    "负面，区间按负面档系数计算"
                    "（- 号 = 减少后坐力 = 正面）"
                )
            elif any(RA.is_inverted(s) for s, _ in stats_pos):
                inverted_note = (
                    "「后坐力」是反转词条：- 号 = 减少后坐力 = "
                    "正面，区间按正面档系数计算"
                    "（+ 号 = 增加后坐力 = 负面）"
                )
        try:
            weapon = await self.client.resolve_riven_weapon(weapon_name.strip())
        except Exception:  # noqa: BLE001 - 解析失败走下面的兜底链
            weapon = None
        if not weapon:
            # 变体兜底：棱晶·X / Prime X → 母武器（WM 紫卡表只挂母武器，
            # 变体倾向取母武器值）
            import re as _re2

            base = _re2.sub(r"^(棱晶|Prime|P)", "", weapon_name.strip())
            base = _re2.sub(r"\s*Prime\s*$", "", base, flags=_re2.I)
            if base != weapon_name.strip():
                weapon = await self.client.resolve_riven_weapon(base)
        # ★ 2026-10-07：反向兜底一跳 —— 手输名认不出时，用**卡面 OCR 名**再试一次
        #   （用户手输写错、卡面反倒读对的情形）。纯图路径下 weapon_name 已等于
        #   _ai_name，此跳自然不触发（行为与改动前逐字一致）。
        if not weapon and _ai_name and _ai_name != weapon_name.strip():
            weapon = await self.client.resolve_riven_weapon(_ai_name)
        # ★ 2026-10-07 近形字兜底（解析链最后一跳）：卡面名被 OCR 读成**近形字**时
        #   （线上实证：冰凇 → 冰松，10:59/17:11/17:13 三次），2 字名的 difflib
        #   ratio 恰 0.5 < 模糊档阈值 0.75 ⇒ 正常链路、模糊层、候选提示全都捞不回。
        #   取「等长 + 仅 1 字不同」的表内名，**用卡面数值可行性当闸门**：
        #     · 唯一可行 ⇒ 采纳，并在卡面注明「识别名 → 实际按哪个名算」；
        #     · 多个可行 ⇒ 不猜，把它们列进候选提示；
        #     · 一个都不行 ⇒ 维持原回落（与改动前一致）。
        #   闸门保证这不是纯字面替换 —— 数值不吻合的错配会被自己挡掉。
        _near_names: list[str] = []
        if not weapon:
            _feas: list[tuple[dict, float]] = []
            for _c in await self.client.nearmiss_riven_weapons(weapon_name.strip()):
                _ccls = RA.weapon_class(_c.get("riven_type", ""), _c.get("group", ""))
                _ccls = RA.kitgun_mode_class(
                    _c.get("en") or _c.get("zh") or "", _ccls, _c.get("riven_type") or ""
                )
                _cd = RA.UNIFIED_DISPOSITION
                if _cd > 0 and any(
                    RA.disp_feasible(stats_pos, stats_neg, _ccls, _cd, counts=_c)
                    for _c in count_cands
                ):
                    _feas.append((_c, _cd))
            if len(_feas) == 1:
                weapon, _near_disp = _feas[0]
                # 纪律（2026-09-02 形近容错立的规矩）：**禁静默改判** —— 采纳时
                # 既写卡面注记，也留一条日志（事后可回溯是哪个名字被换掉了）。
                logger.info(
                    "[sdjk] 近形字兜底：卡面名「%s」→ 按「%s」计算（倾向 %g，数值可行）",
                    weapon_name.strip(),
                    weapon.get("zh") or weapon.get("en"),
                    _near_disp,
                )
                nearmiss_note = (
                    f"卡面识别名「{weapon_name.strip()}」→ 已按"
                    f"「{weapon.get('zh') or weapon.get('en')}」计算"
                    f"（数值与倾向 {_near_disp:g} 吻合）"
                )
            elif _feas:
                _near_names = [(c.get("zh") or c.get("en") or "") for c, _ in _feas]
        if not weapon:
            tips = await self.client.suggest_riven_weapons(weapon_name.strip())
            if not tips and _near_names:
                tips = _near_names[:3]
            # ★ 2026-10-07 删除一条旧提示（用户口径 + 线上实证）：原文是一条「模块化
            #   武器请连腔体与模式一起重发」的补发指引（附示例）。删除理由：
            #   ① 增幅器（Amp）**没有紫卡**（`_VEILED_SLUGS` 八大类无 Amp、紫卡表
            #      670 条也没有 Amp 条目），列出来就是错的；
            #   ② 魔典/Zaw 没有主要/次要之分（Zaw=近战、魔典=副手），与该指引的
            #      「带模式」前提不符；
            #   ③ 组合枪已能按卡面数值反推主要/次要（家族候选按模式换基值列 +
            #      最近邻判据），再让人「带模式重发」是过时指引；
            #   ④ 线上日志实证（2026-10-07 10:59 / 17:11 / 17:13 三次）：卡面
            #      **有**武器名、只是被 OCR 读成近形字（冰凇→冰松）时它照样弹出来，
            #      答非所问（群里当场质疑「你不识字啊」）。
            return err_card(
                f"未找到紫卡武器「{weapon_name.strip()}」",
                "照候选改名重发" if tips else "换官方中文名或英文名重发",
                ("你是不是想找", "、".join(tips)) if tips else "",
                title="紫卡分析",
            )
        cls = RA.weapon_class(weapon.get("riven_type", ""), weapon.get("group", ""))
        _kitgun_type = weapon.get("riven_type") or ""
        # ★ 2026-10-03：WM 拆分行（捕月（主要）等 (Primary)/(Secondary) 行）
        # riven_type/group 为空 ⇒ weapon_class 回落「rifle」会拿错基值。
        # 基名行在表里 ⇒ 继承基名行的类别。
        if not (weapon.get("riven_type") or weapon.get("group")):
            _mb = _re.match(r"^(.+?)\s*\([^)]+\)\s*$", (weapon.get("en") or "").strip())
            if _mb:
                _base_w = await self.client.resolve_riven_weapon(_mb.group(1))
                if _base_w and (_base_w.get("riven_type") or _base_w.get("group")):
                    _kitgun_type = _base_w.get("riven_type") or ""
                    cls = RA.weapon_class(_base_w.get("riven_type", ""), _base_w.get("group", ""))
        # ★ 2026-10-03（用户实证 + wiki Kitgun 页分类表）：kitgun 腔体主要
        #   形态的 MOD 基值列**逐腔体不同**——捕月/孢射主要=霰弹列，
        #   墓指/响胆/凝视/虫置主要=步枪列，次要一律手枪列；非 kitgun 原样
        #   （Vinquibus (Primary) 仍是步枪列）。
        cls = RA.kitgun_mode_class(weapon.get("en") or weapon.get("zh") or "", cls, _kitgun_type)
        # ★ 2026-10-22：类别**推不出**时不再静默 —— 卡面明说按步枪估算（此前 13 把
        #   变体/形态条目被静默按步枪列算了很久）。判据：最终仍是步枪列**且**
        #   group/riven_type 都命不中表；kitgun 形态行虽也空字段，但上面那步已按
        #   腔体给了手枪列 ⇒ 不会被误报。
        class_est_note = (
            "⚠ 武器类别未知，按步枪基值估算"
            if cls == "rifle"
            and RA.class_estimated(weapon.get("riven_type", ""), weapon.get("group", ""))
            else ""
        )
        # 倾向：DE 已统一（RA.UNIFIED_DISPOSITION）；手输覆盖仍保留作逃生口。
        # 游戏内紫卡不显示倾向数值，LLM 从卡面"读倾向"只会把内融值之类的
        # 数字当倾向（教训：49 → 区间爆表），所以永远不采信 LLM。
        # 表里的 disposition 是取消前的旧值，只用于识别旧截图。
        legacy_disp = float(weapon.get("disposition") or 0)
        disp = disp_override or RA.UNIFIED_DISPOSITION
        name = weapon.get("zh") or weapon.get("en") or weapon["url_name"]
        mother_name = name
        # ── 小数点修正（2026-09-24 玩家报障：115.7% 读成 1157%）────────────
        # vision 偶发把小数点读丢，整卡数值随之「都不吻合」。用**当前倾向的合法
        # 区间**做判据：原值明显超出区间、除以 10 落回区间内 → 修正并在卡面注明。
        decimal_fix: list[str] = []
        # 词条数（决定系数）：有「？？？」未揭示词条时按总行数定；3 行无负词条两可
        # （3+0 / 2+1）⇒ 用卡面数值挑能吻合的那个，都不吻合按先列的（未揭示当正）算
        counts = count_cands[0]
        if len(count_cands) > 1:
            counts = next(
                (
                    c
                    for c in count_cands
                    if RA.disp_feasible(stats_pos, stats_neg, cls, disp, counts=c)
                ),
                counts,
            )
        # 问号行折行时候选按少算（见 count_candidates）⇒ 未揭示条数以选定的词条数为准
        if hidden:
            hidden = max(0, counts[0] + counts[1] - len(stats_pos) - len(stats_neg))
        hidden_fix_note = ""

        def _fix_decimal(pairs, negative: bool):
            out = []
            for sid, v in pairs:
                lo, hi = RA.stat_range(sid, cls, disp, *counts, negative=negative)
                if not lo or not hi:
                    out.append((sid, v))
                    continue
                if not (lo * 0.7 <= v <= hi * 1.4):
                    v10 = v / 10.0
                    if lo * 0.85 <= v10 <= hi * 1.15:
                        decimal_fix.append(f"{RA.fmt_value(sid, v)} → {RA.fmt_value(sid, v10)}")
                        v = v10
                out.append((sid, v))
            return out

        stats_pos = _fix_decimal(stats_pos, False)
        stats_neg = _fix_decimal(stats_neg, True)
        # 负词条可能被漏识别：卡面负词条常写成「x0.55 对 Corpus 的伤害」这类
        # 乘数形式，vision 容易整行丢掉 —— 词条数系数会从 (n正,1)=0.9375
        # 错成 (n正,0)=0.75，区间整体偏小 20%，表现为「卡面数值与倾向都不吻合」。
        # 判据：0 负解释不了、1 负能解释 → 按 1 负算（区间只依赖正词条系数）。
        neg_fix_note = ""
        if not stats_neg and stats_pos and len(stats_pos) == 3 and not hidden:
            try:
                if not RA.disp_feasible(stats_pos, [], cls, disp) and RA.disp_feasible(
                    stats_pos, [("damage_vs_corpus", 45.0)], cls, disp
                ):
                    stats_neg = [("damage_vs_corpus", 45.0)]
                    counts = (3, 1)
                    neg_fix_note = (
                        "⚠ 卡面疑似有未被识别的负词条（常见写法"
                        "「x0.55 对 Corpus 的伤害」这类乘数形式），"
                        "已按 3正1负 的系数计算"
                    )
            except Exception:  # noqa: BLE001 —— 纠错失败不影响主流程
                pass
        if disp_override:
            name = weapon_name.strip() or name  # 保留用户输入的名字
        # ── 倾向吻合检查 ────────────────────────────────────────────────
        # ★ 2026-10 DE 取消倾向后，家族变体（棱晶/Prime/赤毒…）与组合枪主/次模式都按同一个
        #   1.2 算，原先「按数值反推该按谁的倾向算」整段退役。数值对不上只剩三种原因：
        #   武器认错、词条读错，或是**取消前的旧截图**（数值还没按 1.2 缩放）——
        #   最后一种用取消前的旧倾向（本地静态表，不再更新）对一下，命中就明说。
        infer_note = ""
        verdict = ""  # 卡首「判定」一行（短结论；完整说明仍在下面的提示行里）
        if not RA.disp_feasible(stats_pos, stats_neg, cls, disp, counts=counts):
            iv = RA.disposition_interval(stats_pos, stats_neg, cls, counts=counts)
            verdict = f"卡面数值与倾向 {disp:g} 不吻合"
            infer_note = f"⚠️ 卡面数值与倾向 {disp:g} 不吻合，请核对武器与词条"
            if iv[0] and not disp_override:
                _old = [(mother_name, legacy_disp)] if legacy_disp > 0 else []
                _old += list(await self.client.riven_family(weapon))
                _hit = [n for n, v in _old if iv[0] <= float(v) <= iv[1]]
                if _hit:
                    verdict = "疑似倾向取消前的旧截图"
                    infer_note = (
                        f"⚠️ 卡面数值与取消前的旧倾向吻合（{'、'.join(_hit[:3])}）"
                        f"—— 疑似倾向取消前的截图，游戏内数值已按 {disp:g} 缩放，请重新截图后再发"
                    )
            # 识别模型可能干脆不念「？？？」那行（2026-10-24）⇒ 条数少一条、区间整体偏移。
            # 不是旧截图、**多一条正词条**就吻合 ⇒ 按多一条算并明说（同上方负词条兜底的闸门思路）
            _more = (counts[0] + 1, counts[1])
            if (
                not verdict.startswith("疑似倾向取消前")
                and not hidden
                and _more in RA.FACTOR
                and RA.disp_feasible(stats_pos, stats_neg, cls, disp, counts=_more)
            ):
                counts = _more
                verdict = infer_note = ""
                hidden_fix_note = (
                    "⚠ 卡面疑似还有一条没读出的词条（融合时未选定的「？？？」或漏识别），"
                    f"已按 {counts[0]}正{counts[1]}负 的系数计算"
                )
        disp_note = (
            f"（已按手输倾向 {disp:g} 计算；DE 已统一为 {RA.UNIFIED_DISPOSITION:g}）"
            if disp_override
            else ""
        )

        # 提示分流（2026-10-11 卡片改版）：需要用户行动的（重发 / 核对）进 ⚠ 警示；
        #   其余说明合并成一条注脚；来源（图片识别）进页脚。
        def _needs_action(t: str) -> bool:
            return "⚠" in t or "请带变体" in t or "请核对" in t or "请重发" in t

        _cands_n = [infer_note, neg_fix_note, hidden_fix_note, dup_note, class_est_note]
        warns = [t for t in _cands_n if t and _needs_action(t)]
        infos = [
            t
            for t in (
                nearmiss_note,
                infer_note if infer_note and not _needs_action(infer_note) else "",
                disp_note,
                inverted_note,
                ("已修正小数点（截图未读出点号）：" + "、".join(decimal_fix))
                if decimal_fix
                else "",
            )
            if t
        ]
        title, lines = fmt.fmt_riven_analysis(
            name,
            disp,
            cls,
            stats_pos,
            stats_neg,
            verdict=verdict,
            warns=warns,
            infos=infos,
            counts=counts,
            hidden=hidden,
        )
        logger.info(
            "[sdjk] 紫卡分析耗时 %.0f ms（含识别/查询/计算）",
            (_tt.perf_counter() - _t_start) * 1000,
        )
        return Reply(
            title,
            lines,
            footer=fmt.fmt_platform_footer(
                platform,
                (f"{source_note.strip('（）')} · " if source_note else "")
                + f"DE 属性基值公式 · 倾向统一 {RA.UNIFIED_DISPOSITION:g}",
            ),
        )
