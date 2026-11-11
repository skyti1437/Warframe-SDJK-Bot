# -*- coding: utf-8 -*-
"""紫卡域指令（D7 自 main.py 迁入）。

覆盖路由键 3 项：analysis / disposition / xh；模块级 _xh_element 随迁。
⚠ 已披露改写（本笔唯一非逐字点）：_h_riven_analysis 体内函数内相对导入
try 分支 from .core.* → from ..*（模块自插件根迁入 core 包，相对基准
随之调整，导入目标不变）；except 兜底与注释逐字保留。
子包纪律：不 import astrbot（事件对象鸭子类型）。
"""

from __future__ import annotations

import asyncio
import json
import math
import re
import unicodedata
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from .. import formatters as fmt
from .. import matching
from .. import riven_analysis as RA
from ..api_client import WarframeAPIError, fuzzy_hits, public_error_text
from ..i18n import L, is_en
from ..logging_compat import logger
from .base import Action, Reply, err_card
from .market import _disp_name

# 「词条范围」组合写法的归一：全角数字 / 加号走 NFKC，U+2212 减号另补一刀
_COMBO_TRANS = str.maketrans({"−": "-", "－": "-"})
# 「看着像组合」但不认识（如 4+ / 5+1-）：**不静默落进武器名**，给一句提示
_COMBO_LIKE_RE = re.compile(r"^\d+\+(?:\d+)?(?:-|负)?$")

# 行情是附加信息；家族解析与单次完整词条查询共用一个前台预算。
RIVEN_ANALYSIS_PRICE_BUDGET_S = 3.0
RIVEN_ANALYSIS_PRICE_REQUEST_TIMEOUT_S = 1.5


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


@dataclass
class _RivenInput:
    weapon: str
    positive: list
    negative: list
    hidden: int
    disposition: float
    unmatched: list


_LOCAL_STATUS_ABSENT = object()


def riven_read_selection(single_ocr, local_status=_LOCAL_STATUS_ABSENT) -> str:
    """给定同一次配置与本地状态，复用真实入口的逐值选路规则；不读取任何服务。"""
    if local_status is _LOCAL_STATUS_ABSENT:
        return "single" if single_ocr is True else "dual"
    if local_status.get("enabled") or local_status.get("ok") is not True:
        return "local"
    if single_ocr is True and local_status.get("enabled") is False:
        return "single"
    return "dual"


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
            _n_alias = len(self.client._aliases.get("lich_items", {}))
            return err_card(
                L("没有写武器名", "No weapon name given"),
                L(
                    "按 xh 武器名 [元素] [数值] 的格式重发，例如「xh 赤毒怒雷」",
                    'Resend as xh <weapon> [element] [min %], e.g. "xh Kuva Nukor"',
                ),
                (L("用法", "Usage"), L("xh 武器名 [元素] [数值]", "xh <weapon> [element] [min %]")),
                L(
                    "· 示例　xh 赤毒怒雷 ｜ xh 信条弧电离子枪 辐射 ｜ xh 赤毒海克 50",
                    "· Examples　xh Kuva Nukor ｜ xh Tenet Arca Plasmor radiation ｜ xh Kuva Hek 50",
                ),
                (
                    L("元素", "Elements"),
                    L(
                        "磁力/电击/毒素/火焰/冰冻/冲击/切割/辐射",
                        "magnetic / electricity / toxin / heat / cold / impact / slash / radiation",
                    ),
                ),
                (
                    L("已收录", "Known weapons"),
                    L(
                        f"{_n_alias} 个中文写法（赤毒/信条/终幕三类）",
                        f"{_n_alias} name entries (Kuva / Tenet / Coda)",
                    ),
                ),
                title=L("玄骸拍卖", "Lich Weapon Auctions"),
            )
        first = toks[0]
        slug = self.client.resolve_lich_weapon(first) or await self._lich_slug_by_riven(first)
        if not slug:
            near = fuzzy_hits(first, list(self.client._aliases.get("lich_items", {})), n=3) or []
            return err_card(
                L(f"未找到玄骸武器「{first}」", f'Lich weapon not found: "{first}"'),
                L(
                    "支持赤毒/信条/终幕三类，可只写后半段（如「怒雷」）；或照候选改名重发",
                    "Kuva, Tenet and Coda weapons are supported; "
                    'the second half alone also works (e.g. "Nukor"), or resend using a suggestion',
                ),
                (L("你是不是想找", "Did you mean"), L("、", ", ").join(near)) if near else "",
                title=L("玄骸拍卖", "Lich Weapon Auctions"),
            )

        info = self.client.lich_weapon_info(slug)
        name = (info.get("en") or slug) if is_en() else (info.get("zh") or slug)
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
                L(
                    f"warframe.market 查询失败：{public_error_text(exc)}",
                    f"warframe.market query failed: {public_error_text(exc)}",
                ),
                L(
                    "多为市场限速（429）或网络抖动，稍等一两分钟重试即可",
                    "Usually market rate limiting (429) or a network blip; retry in a minute or two",
                ),
                title=L("玄骸拍卖", "Lich Weapon Auctions"),
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
            filters.append(elem_cn if not is_en() else str(elem_en).capitalize())
        if min_dmg is not None:
            filters.append(L(f"伤害≥{min_dmg}%", f"Damage ≥{min_dmg}%"))
        if want_eph:
            filters.append(L("带幻纹", "With Ephemera"))
        title = L(
            f"{name} 玄骸拍卖（{len(pool)}条"
            + ("，" + "·".join(filters) if filters else "")
            + "）",
            f"{name} Lich Weapon Auctions ({len(pool)} listings"
            + (", " + " · ".join(filters) if filters else "")
            + ")",
        )
        if not pool:
            if self.client.lich_unsupported(slug):
                return self._xh_no_category(name, slug, kind, platform)
            elif not auctions:
                lines = [
                    L(
                        "◆ 原因：该武器当前没有挂单（冷门武器挂单少，可过段时间再看）",
                        "◆ Reason: this weapon has no listings right now (niche weapons have few; check back later)",
                    ),
                    L(
                        "※ ⚠ 怎么改：过段时间重发；或换一把同系列武器查",
                        "※ ⚠ How to fix: try again later, or check another weapon of the same line",
                    ),
                ]
            else:
                lines = [
                    L(
                        "◆ 原因：暂无符合条件的挂单"
                        + (f"（筛选：{'·'.join(filters)}）" if filters else ""),
                        "◆ Reason: no listings match"
                        + (f" (filter: {' · '.join(filters)})" if filters else ""),
                    ),
                    L(
                        "※ ⚠ 怎么改：去掉元素 / 数值 / 幻纹筛选词，或放宽数值下限后重发",
                        "※ ⚠ How to fix: drop the element / value / ephemera filters, or lower the minimum, then resend",
                    ),
                ]
            return Reply(
                title,
                lines,
                footer=fmt.fmt_platform_footer(
                    platform, L("warframe.market 玄骸", "warframe.market Lich Weapons")
                ),
            )
        lines = fmt.fmt_lich_table(pool[:12], total=len(pool))
        return Reply(
            title,
            lines,
            footer=fmt.fmt_platform_footer(
                platform, L("warframe.market 玄骸", "warframe.market Lich Weapons")
            ),
        )

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
        sibling_en = {"sister": "Tenet", "lich": "Kuva", "coda": "Coda"}.get(kind, "")
        example = {
            "sister": "xh 信条弧电离子枪",
            "lich": "xh 赤毒怒雷",
            "coda": "xh 信条弧电离子枪",
        }.get(kind, "xh 信条弧电离子枪")
        example_en = {
            "sister": "xh Tenet Arca Plasmor",
            "lich": "xh Kuva Nukor",
            "coda": "xh Tenet Arca Plasmor",
        }.get(kind, "xh Tenet Arca Plasmor")
        return Reply(
            L(
                f"{name} 玄骸拍卖（市场无此类目）",
                f"{name} Lich Weapon Auctions (No Market Category)",
            ),
            [
                L(
                    f"◆ 原因：warframe.market 没有「{name}」的拍卖类目",
                    f'◆ Reason: warframe.market has no auction category for "{name}"',
                ),
                L(
                    f"◆ 说明：这不是识别失败——中文名已正常匹配到 {slug}，是市场侧没这个类目"
                    f"（已逐把核对全部 {total} 把玄骸武器：{has} 把有挂单、{total - has} 把没有）",
                    f"◆ Note: this is not a recognition failure — the name matched {slug}; "
                    f"the market simply has no such category "
                    f"(all {total} lich weapons checked: {has} have listings, {total - has} do not)",
                ),
                L("◆ 替代路径", "◆ Alternatives"),
                L(
                    f"· 换一把同系列　发「{example}」",
                    f'· Try another weapon of the same line　send "{example_en}"',
                ),
                L(
                    f"· 网站自查　warframe.market/zh-hans/auctions/search"
                    f"?type={kind}&weapon_url_name={slug}",
                    f"· Check the site　warframe.market/auctions/search"
                    f"?type={kind}&weapon_url_name={slug}",
                ),
                L(
                    f"· 游戏内交易　这类武器（全部终幕 + 部分近战{sibling}）只能游戏内交易频道收",
                    f"· In-game trading　these weapons (all Coda + some melee {sibling_en}) "
                    "can only be bought in the trade chat",
                ),
            ],
            footer=fmt.fmt_platform_footer(
                platform, L("warframe.market 玄骸", "warframe.market Lich Weapons")
            ),
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
        head = L(
            f"◆ DE 已取消紫卡倾向：所有武器统一按 {unified:.2f}（{fmt._disp_dots(unified)}）计算",
            f"◆ DE has removed riven dispositions: all weapons now use {unified:.2f} "
            f"({fmt._disp_dots(unified)})",
        )
        note = L(
            "◆ 已有紫卡词条不变，数值按「1.2 / 原倾向」缩放；原倾向越低涨得越多",
            '◆ Existing riven stats are unchanged; values scale by "1.2 / old disposition" '
            "— the lower the old disposition, the bigger the increase",
        )
        if not query:
            old = sorted(
                [w for w in weapons if w.get("disposition")], key=lambda w: w["disposition"]
            )

            def _row(w: dict) -> str:
                return (
                    f"· {_disp_name(w)}　{w['disposition']:.2f}　×{unified / w['disposition']:.2f}"
                )

            lines = [
                head,
                note,
                L(
                    "◆ 涨幅最大 Top8（原倾向最低）",
                    "◆ Biggest Increases Top 8 (lowest old disposition)",
                ),
                L("　武器　原倾向　数值缩放", "　Weapon　Old Disp.　Value Scale"),
            ]
            lines += [_row(w) for w in old[:8]]
            lines += [
                L(
                    "◆ 降幅最大 Top5（原倾向最高）",
                    "◆ Biggest Decreases Top 5 (highest old disposition)",
                )
            ] + [_row(w) for w in old[::-1][:5]]
            lines.append(L("◆ 查单把武器：紫卡倾向 武器名", "◆ For one weapon: dispo <weapon>"))
            return Reply(
                L("紫卡倾向（已统一 1.2）", "Riven Dispositions (Unified 1.2)"),
                lines,
                footer=fmt.fmt_platform_footer(platform),
            )
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
            close = (
                matching.suggest_zh(query, weapons, zh="en", en="zh")
                if is_en()
                else matching.suggest_zh(query, weapons)
            )
            return err_card(
                L("未找到该武器", "Weapon not found"),
                L(
                    "照候选改名重发" if close else "换官方中文名或英文名重发",
                    "Resend using one of the suggestions"
                    if close
                    else "Check the spelling and resend with the weapon's official English name",
                ),
                (L("你是不是想找", "Did you mean"), L("、", ", ").join(close)) if close else "",
                title=L("紫卡倾向", "Riven Disposition"),
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
            L(
                f"◆ {_disp_name(_h0)}："
                f"倾向 {unified:.2f}（{fmt._disp_dots(unified)}）｜{_cls(_h0)}",
                f"◆ {_disp_name(_h0)}: "
                f"Disposition {unified:.2f} ({fmt._disp_dots(unified)})｜{_cls(_h0)}",
            ),
            L(
                f"◆ 原倾向 {_d0:.2f} → 已有紫卡数值 ×{unified / _d0:.2f}",
                f"◆ Old disposition {_d0:.2f} → existing riven values ×{unified / _d0:.2f}",
            )
            if _d0 > 0
            else L("◆ 原倾向：无数据", "◆ Old disposition: no data"),
        ]
        if len(hits) > 1:
            lines.append(L(f"◆ 同家族（共 {len(hits)} 种）", f"◆ Same family ({len(hits)} total)"))
            lines.append(
                L("　武器　原倾向　数值缩放　类别", "　Weapon　Old Disp.　Value Scale　Category")
            )
            lines += [
                f"· {_disp_name(w)}　{_scale(w)}　"
                # ★ 2026-09-24：带上 group —— WM 把曲翼枪械的 rivenType 也标成
                #   rifle（翠雀显示成「步枪」），group 才是准的（曲翼枪械/守护武器）
                f"{_cls(w)}"
                for w in hits[:8]
            ]
        lines.append(head)
        return Reply(
            L(f"紫卡倾向：{query}", f"Riven Disposition: {query}"),
            lines,
            footer=fmt.fmt_platform_footer(platform),
        )

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

    # 英文写法（只在「紫卡融合」参数解析处认，不进通用别名表）
    _SPLICE_CLASSES_EN = {
        "rifle": "rifle",
        "shotgun": "shotgun",
        "pistol": "pistol",
        "secondary": "pistol",
        "archgun": "archgun",
        "melee": "melee",
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
            if t.lower() in self._SPLICE_CLASSES_EN:
                cls = self._SPLICE_CLASSES_EN[t.lower()]
                scope = t.capitalize()
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
                    L(
                        f"未找到紫卡武器或词条「{query}」",
                        f'No riven weapon or stat found: "{query}"',
                    ),
                    L(
                        "照候选改名重发"
                        if tips
                        else "换官方中文名、武器类（步枪/霰弹枪/手枪/空战/近战）或词条名重发",
                        "Resend using one of the suggestions"
                        if tips
                        else "Resend with the weapon's English name, a weapon class "
                        "(rifle / shotgun / pistol / archgun / melee) or a stat name",
                    ),
                    (L("你是不是想找", "Did you mean"), L("、", ", ").join(tips)) if tips else "",
                    title=L("紫卡融合", "Riven Splicing"),
                )
            cls = RA.weapon_class(weapon.get("riven_type", ""), weapon.get("group", ""))
            cls = RA.kitgun_mode_class(
                weapon.get("en") or weapon.get("zh") or "", cls, weapon.get("riven_type") or ""
            )
            name = _disp_name(weapon) if is_en() else weapon.get("zh") or weapon.get("en") or query
            scope = L(
                f"{name}（{fmt._SPLICE_CLASS_ZH.get(cls, cls)}）",
                f"{name} ({cls.capitalize() if cls else cls})",
            )
        recipes = RA.splice_recipes(cls, stat)
        title, lines = fmt.fmt_riven_splice(
            recipes,
            scope=scope,
            stat_zh=(fmt._stat_name(stat) if is_en() else RIVEN_STAT_ZH.get(stat, ""))
            if stat
            else "",
            cls=cls,
        )
        return Reply(
            title,
            lines,
            footer=fmt.fmt_platform_footer(
                platform, L("Riven Splicer 配方", "Riven Splicer Recipes")
            ),
        )

    async def _h_riven_ranges(self, parsed, event, platform) -> Reply:
        """词条范围：按武器列出紫卡每条词条的卡面取值区间（只显示统一倾向 1.2）。

        用法：词条范围 武器名 [2+ | 2+1 | 2+1- | 3+ | 3+1 | 3+1-] [词条名…]
        不写组合默认 3正1负（`2+1` = `2+1-`，`3+1` = `3+1-`）；带上词条名就只列那几条。
        **按武器过滤**：该武器刷不出的词条不列（逐家族证据 > 武器自身推导 > 职业池）。
        """
        try:  # 服务器以包成员加载，相对导入才可靠（同 _h_riven_analysis）
            from .. import riven_pools as RP
            from .. import riven_analysis as RA
            from ..parser import _STAT_LOOKUP
        except ImportError:  # pragma: no cover - 本地直跑
            from core import riven_pools as RP
            from core import riven_analysis as RA
            from core.parser import _STAT_LOOKUP

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
                L(f"组合「{bad_combo}」不认识", f'Unknown combo "{bad_combo}"'),
                L(
                    "组合只有 3+1- / 3+ / 2+1- / 2+（也认 2+1 / 3+1 / 2+1负 / 3+1负）。"
                    "例如「词条范围 冰凇 2+1」",
                    'Only 3+1- / 3+ / 2+1- / 2+ are valid (2+1 / 3+1 also work). e.g. "rivenranges Glaive Prime 2+1"',
                ),
                (
                    L("用法", "Usage"),
                    L(
                        "词条范围 武器名 [2+ / 2+1 / 2+1- / 3+ / 3+1 / 3+1-] [词条名…]",
                        "rivenranges <weapon> [2+ / 2+1 / 2+1- / 3+ / 3+1 / 3+1-] [stat names…]",
                    ),
                ),
                title=L("紫卡词条范围", "Riven Stat Ranges"),
            )
        query = " ".join(weapon_toks).strip()
        if not query:
            return err_card(
                L("没有写武器名", "No weapon name given"),
                L(
                    "按 词条范围 武器名 [组合] [词条名…] 的格式重发，例如「词条范围 绝路Prime」"
                    "「词条范围 冰凇 3+」「词条范围 绝路 基伤 多重 毒气 3+1-」",
                    "Resend as rivenranges <weapon> [combo] [stat names…], "
                    'e.g. "rivenranges Rubico Prime", "rivenranges Glaive 3+", '
                    '"rivenranges Rubico Damage Multishot Toxin 3+1-"',
                ),
                (
                    L("用法", "Usage"),
                    L(
                        "词条范围 武器名 [2+ / 2+1 / 2+1- / 3+ / 3+1 / 3+1-] [词条名…]",
                        "rivenranges <weapon> [2+ / 2+1 / 2+1- / 3+ / 3+1 / 3+1-] [stat names…]",
                    ),
                ),
                title=L("紫卡词条范围", "Riven Stat Ranges"),
            )
        item = await self.client.resolve_riven_weapon(query)
        if not item:
            return await self._wm_suggest(query)
        display = (_disp_name(item) if is_en() else item.get("zh") or item.get("en")) or query
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
            dropped = [fmt._stat_name(s) for s in stats if s not in avail]
            pos = [s for s in pos if s in keep]
            spliced = [s for s in spliced if s in keep]
            neg = [s for s in neg if s in keep]
        _unseen, _sv = RP.survey_unseen(root)
        title, lines = fmt.fmt_riven_ranges(
            display,
            cls,
            combo,
            pos,
            spliced,
            neg,
            unc,
            only=only,
            dropped=dropped,
            survey={"unseen": _unseen, **_sv},
            stat_multipliers=RA.weapon_stat_multipliers(item),
        )
        if RA.class_estimated(item.get("riven_type") or "", item.get("group") or ""):
            lines.insert(
                0,
                L(
                    "※ ⚠ 武器类别未知，按步枪基值列估算（区间可能有偏差）",
                    "※ ⚠ Weapon class unknown; estimated with the rifle base column (ranges may be off)",
                ),
            )
        return Reply(
            title,
            lines,
            footer=fmt.fmt_platform_footer(platform, L("紫卡词条池", "Riven Stat Pool")),
        )

    def _riven_text_input(self, parsed) -> _RivenInput:
        """解析手输字段，使文字与图片共用后续的分析规则。"""
        from ..parser import RIVEN_STAT_ZH

        rev = {v: k for k, v in RIVEN_STAT_ZH.items()}
        disp_override = 0.0  # 手输倾向（倾向0.95 / @0.95 / d0.95）
        stats_pos: list[tuple[str, float]] = []
        stats_neg: list[tuple[str, float]] = []
        weapon_name = ""
        # 融合时还没选定的词条（卡面「？？？」）：正负未知，只计入词条数（2026-10-24）
        hidden = 0
        splice_unmatched_numeric: list[str] = []
        _stat_tok = r"([\u4e00-\u9fa5A-Za-z]{1,24}?)(\d+(?:\.\d+)?)"
        _en_neg = r"(?:negative|neg|curse)"
        _toks = [str(x).strip() for x in (parsed.content or [])]
        _pending_neg = False  # 英文独立的 neg / negative / curse 作用于下一个词条
        for _i, t in enumerate(_toks):
            if not t:
                continue
            if re.fullmatch(r"[?？]{2,}", t):
                hidden += 1
                continue
            neg = t.startswith(("负", "-"))
            body = t[1:] if neg else t
            if not neg:
                # 英文负词条前缀（与 wr 一致：neg / negative / curse）：
                # 紧贴 `negHeat45.8` / `neg-Heat45.8`，或独立 `neg Heat45.8`；
                # 去掉前缀后必须真能认出词条才算，否则原样当普通内容，不吞武器名
                m_en = re.fullmatch(_en_neg + r"[-_]?(?=[A-Za-z])(.+)", t, re.I)
                if m_en:
                    m_b = re.fullmatch(_stat_tok, m_en.group(1))
                    if m_b and self._stat_id_from_name(m_b.group(1), rev):
                        neg, body = True, m_en.group(1)
                if not neg and re.fullmatch(_en_neg, t, re.I) and _i + 1 < len(_toks):
                    m_b = re.fullmatch(_stat_tok, _toks[_i + 1])
                    if m_b and self._stat_id_from_name(m_b.group(1), rev):
                        _pending_neg = True
                        continue
                if _pending_neg:
                    neg, _pending_neg = True, False
            # ★ 2026-09-24：词条名放宽到 1~8 字（卡面原文「滑行攻击暴击几率」6 字，
            #   旧限 1~4 字会整条落到武器名里 → 负词条丢失、反推区间算错）
            # ★ 2026-10-02 上限 8→12 字：DE 卡面原文「的几率来获得连击数」是 9 字，
            #   旧限会让整条落进 weapon_name（静默丢词条 + 污染武器名）。
            # ★ 2026-10-23（ZCode 部署回执）：派系词条卡面原文夹英文「对Infested伤害」，
            #   只收汉字会整条落进武器名 ⇒ 放宽到汉字 + 英文字母（认不出的照旧归武器名）。
            m = re.fullmatch(r"([\u4e00-\u9fa5A-Za-z]{1,24}?)(\d+(?:\.\d+)?)", body)
            if m:
                sid = self._stat_id_from_name(m.group(1), rev)
                if sid:
                    (stats_neg if neg else stats_pos).append((sid, float(m.group(2))))
                    continue
                splice_unmatched_numeric.append(t)
            if re.fullmatch(r"\d\+(?:\d)?", t):
                continue  # 3+1 之类的词条数标注，P/N 直接按实际词条算
            m_d = re.fullmatch(r"(?:倾向|d|@)(\d+(?:\.\d+)?)", t, re.I)
            if m_d:
                disp_override = float(m_d.group(1))
                continue  # 手输倾向覆盖（棱晶等变体 WM 没有数据）
            weapon_name += t

        # ★ 2026-10-02 反转词条自洽改判（用户照抄卡面时不会给后坐力写「负」前缀）：
        #   4 正 0 负且其中**恰有 1 条**反转词条 ⇒ 该条实为负面（卡面 `+` 号但
        #   收益为负）。旧流程被下面的词条数闸门拒收，用户拿到「词条数不对」。
        #   （卡面注脚统一在下方的「反转词条注脚」块生成，两条路径同文案。）
        if len(stats_pos) > 3 and not stats_neg:
            _inv = [(i, s) for i, s in enumerate(stats_pos) if RA.is_inverted(s[0])]
            if len(_inv) == 1:
                _i, _s = _inv[0]
                stats_pos.pop(_i)
                stats_neg.append(_s)

        return _RivenInput(
            weapon_name, stats_pos, stats_neg, hidden, disp_override, splice_unmatched_numeric
        )

    def _use_single_riven_ocr(self) -> bool:
        if (getattr(self, "cfg", None) or {}).get("riven_single_ocr") is not True:
            return False
        local = getattr(self, "local_ocr", None)
        if local is None:
            return True
        status = local.status()
        return riven_read_selection(True, status) == "single"

    async def _h_riven_analysis(self, parsed, event, platform) -> Reply:
        """紫卡文字或截图的常规指令入口。"""
        started = time.perf_counter()
        capability = getattr(self, "_can_send_actions", None)
        can_actions = callable(capability) and capability(event)
        typed = self._riven_text_input(parsed)
        if typed.weapon and (typed.positive or typed.negative):
            return await self._finish_riven_analysis(typed, None, platform, can_actions, started)
        # —— 图片识别路径 ——
        if not self._event_has_image(event):
            reply = err_card(
                L(
                    "没有给出武器名和词条数值，也没有附紫卡截图",
                    "No weapon name or stat values given, and no riven screenshot attached",
                ),
                L(
                    "按用法补全重发；也可直接发「紫卡分析 + 紫卡截图」",
                    'Fill in the usage and resend, or send "rivenanalysis" with a riven screenshot',
                ),
                (
                    L("用法", "Usage"),
                    L(
                        "紫卡分析 武器名 词条数值…（负词条加「负」前缀）",
                        "rivenanalysis <weapon> <stat><value>… (prefix a negative stat with -; "
                        "write stat names without spaces)",
                    ),
                ),
                L(
                    "· 示例　紫卡分析 欧玛 暴伤82.8 范围1.6 攻速45.8 负滑暴81.3",
                    "· Example　rivenanalysis Ohma CriticalDamage82.8 Range1.6 -Heat45.8",
                ),
                title=L("紫卡分析", "Riven Analysis"),
            )
            if can_actions:
                reply.actions.append(
                    Action(
                        L("照示例发", "Send example"),
                        L(
                            "紫卡分析 欧玛 暴伤82.8 范围1.6 攻速45.8 负滑暴81.3",
                            "rivenanalysis Ohma CriticalDamage82.8 Range1.6 -Heat45.8",
                        ),
                        mode="fill",
                    )
                )
            return reply
        # 隔离模式按任务冷却；机器总并发预算仍共享，本地 OCR 另有单 worker 硬上限。
        _last = self._vision_last("riven")
        _gate, _sender = self._vision_gate(event, L("紫卡识图", "Riven Scan"), task="riven")
        if _gate is not None:
            return _gate
        _stamp = _last.get(_sender)
        # 每请求独立占位；同人同会话在冷却关闭时也不能折叠计数或提前释放别人席位。
        _busy_key = object()
        self._ocr_busy.add(_busy_key)
        try:
            imgs = await self._image_data_urls(event)
            if not imgs:
                return Reply(
                    raw_text=L(
                        "⚠️ 图片下载失败，请重发一次截图",
                        "⚠️ Image download failed, please resend the screenshot",
                    )
                )
            if self._use_single_riven_ocr():
                from .. import riven_ocr_reader as reader

                async def validate(observation):
                    return await self._finish_riven_analysis(
                        typed, observation, platform, can_actions, started, check_only=True
                    )

                # 单模型模式须显式配置紫卡主读，绝不自动枚举或借用其它任务渠道。
                provider_id = self._vision_route_value("vision_provider_id_riven")
                provider = self._try_get_provider(provider_id) if provider_id else None
                data, reason = await reader.read(
                    provider, imgs[0], validate, self._RIVEN_LINE_PROMPT
                )
                if reason:
                    self._vision_refund(_last, _sender, _stamp)
                    return Reply(raw_text=reason)
            else:
                data = await self._extract_riven_from_image(imgs[0])
        finally:
            self._ocr_busy.discard(_busy_key)
        if not data:
            if _sender:  # 识别失败返还冷却（同识卡口径）
                self._vision_refund(_last, _sender, _stamp)
            return err_card(
                L(
                    "图片识别失败（vision 渠道不可用或未配）",
                    "Image recognition failed (vision channel unavailable or not configured)",
                ),
                L(
                    "请按文字格式发送：紫卡分析 武器名 暴伤82.8 范围1.6 负滑暴81.3",
                    "Send it in text form instead: rivenanalysis <weapon> CriticalDamage82.8 Range1.6 -Heat45.8",
                ),
                title=L("紫卡分析", "Riven Analysis"),
            )
        local_result = data.get("_local_ocr")
        if isinstance(local_result, dict):
            state = local_result.get("state", local_result.get("recognition_state"))
            if state != "accepted":
                if _sender:
                    self._vision_refund(_last, _sender, _stamp)
                local_lines = data.get("lines") or []
                local_weapon = data.get("weapon") or L("未确认", "Unconfirmed")
                if state == "partial" and local_lines:
                    return Reply(
                        raw_text="\n".join(
                            [
                                L(
                                    "本地紫卡 OCR：请核对以下识别结果，尚未用于估价。",
                                    "Local riven OCR: please check the result below; it has not been used for pricing.",
                                ),
                                L(f"武器：{local_weapon}", f"Weapon: {local_weapon}"),
                                *[str(line) for line in local_lines[:8]],
                                L(
                                    "核对后可按文字格式发送「紫卡分析 武器名 词条数值…」。",
                                    'After checking, send it as text: "rivenanalysis <weapon> <stat><value>…".',
                                ),
                            ]
                        )
                    )
                return err_card(
                    L("本地紫卡 OCR 未能完成识别", "Local riven OCR could not finish"),
                    str(
                        local_result.get("message")
                        or L(
                            "请检查本地环境、模型与资源后重试",
                            "Check the local environment, models and resources, then retry",
                        )
                    ),
                    title=L("紫卡分析", "Riven Analysis"),
                )
        return await self._finish_riven_analysis(typed, data, platform, can_actions, started)

    async def _finish_riven_analysis(
        self, typed, data, platform, can_actions, started, *, check_only=False
    ):
        """校验和正式分析走同一入口。校验到完整性闸门为止，仅最终结果查询行情。"""
        from ..parser import RIVEN_STAT_ZH

        rev = {v: k for k, v in RIVEN_STAT_ZH.items()}
        weapon_name, disp_override, hidden = typed.weapon, typed.disposition, typed.hidden
        stats_pos, stats_neg = list(typed.positive), list(typed.negative)
        splice_unmatched_numeric = typed.unmatched
        splice_input_incomplete = False
        inverted_note = ""
        source_note = ""
        image_data = data
        stat_name_matches: list[dict] = []

        def pending_image(reason: str, candidates=()) -> Reply:
            original = {key: image_data.get(key) for key in ("weapon", "positive", "negative")}
            rows = [
                L(
                    "紫卡识别结果待核对，尚未用于分析或估价。",
                    "Riven recognition result needs checking; it has not been used for analysis or pricing.",
                ),
                reason,
                L("原始识别结构：", "Raw recognition structure: ")
                + json.dumps(original, ensure_ascii=False),
            ]
            original_lines = image_data.get("lines")
            _lines_head = L("逐行原文：", "Line-by-line text:")
            if isinstance(original_lines, str) and original_lines:
                rows.extend([_lines_head, *original_lines.splitlines()])
            elif isinstance(original_lines, (list, tuple)) and original_lines:
                rows.extend([_lines_head, *map(str, original_lines)])
            if candidates:
                rows.append(
                    L("代码推断候选（需确认）：", "Candidates inferred by code (please confirm):")
                )
                rows.extend(candidates)
            rows.append(
                L(
                    "请核对原图后按文字格式发送「紫卡分析 武器名 词条数值…」。",
                    'Please check the original image, then send it as text: "rivenanalysis <weapon> <stat><value>…".',
                )
            )
            return Reply(raw_text="\n".join(rows))

        # ★ 2026-10-07：图片路径里 OCR 读到的武器名（清洗后）单独留一份 —— 手输名
        #   认不出时用它兜底（见下面的解析链），纯文字路径保持空串。
        _ai_name = ""
        # 近形字兜底的卡面注记（解析链最后一跳命中时填，见下）
        nearmiss_note = ""
        if data is not None:
            if (data.get("_local_ocr") or {}).get("name_normalized"):
                raw_name = data.get("weapon_raw") or ""
                canonical_name = data.get("weapon") or ""
                nearmiss_note = L(
                    f"本地识别名「{raw_name}」→ 已按「{canonical_name}」计算（唯一数值可行候选）",
                    f'Locally recognized name "{raw_name}" → calculated as "{canonical_name}" '
                    "(the only numerically feasible candidate)",
                )
                logger.info("[sdjk] 紫卡原名归一：状态=已采信，类别=唯一数值可行候选")
            stats_pos, stats_neg = self._normalize_llm_stats(data, rev)
            # 语义表未识别项不能静默当作不存在。
            _splice_sem_count = sum(
                len(self._iter_stat_pairs(data.get(k))) for k in ("positive", "negative")
            )
            splice_input_incomplete = (
                bool(data.get("_numeric_precision_unsafe"))
                or _splice_sem_count > len(stats_pos) + len(stats_neg)
                or any(
                    key in data and not isinstance(data[key], (list, tuple, dict))
                    for key in ("positive", "negative")
                )
            )
            recognition = data.get("_recognition") or {}
            stat_name_matches = [
                row["name_match"]
                for row in recognition.get("stat_normalization", [])
                if row.get("decision") == "accepted" and row.get("name_match")
            ]
            recognition["selected_stat_source"] = "semantic_json"
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
            _ai_name = re.sub(r"[A-Za-z\-].*$", "", _ai_raw).strip(" ··") or _ai_raw
            # 融合未选定时自命名显示成「？？？」（视使之触？？？）：问号不是武器名
            _ai_name = re.sub(r"[?？\s]+$", "", _ai_name) or _ai_name
            weapon_name = _user_name or _ai_name
            # 独立窄读常保留了标题。用官方词表的精确/变体等价匹配恢复身份；
            # 拉丁自命名不能进入宽松子串搜索后误命中另一把武器。
            if not _user_name:
                from .. import matching as _identity_matching

                get_catalog = getattr(self.client, "wm_riven_weapons", None)
                if callable(get_catalog):
                    catalog = await get_catalog()

                    def exact_observed_name(query):
                        forms = set(_identity_matching.expand_variants(query))
                        forms.add(_identity_matching.normalize(query))

                        def catalog_forms(value):
                            # 模块部件卡面不印主要/次要后缀，词表可分两个模式；
                            # 保留模式候选交给后续代码，不替模型补武器后缀。
                            base = re.sub(
                                r"[_\s]*[（(](?:主要|次要|primary|secondary)[）)]\s*$",
                                "",
                                str(value or ""),
                                flags=re.I,
                            )
                            return {
                                _identity_matching.normalize(value or ""),
                                _identity_matching.normalize(base),
                            }

                        return [
                            w
                            for w in catalog
                            if any(
                                forms & catalog_forms(w.get(k)) for k in ("zh", "en", "url_name")
                            )
                        ]

                    main_hits = exact_observed_name(_ai_name) if _ai_name else []
                    title_names = []
                    for row in data.get("_line_decode", {}).get("rows", []):
                        title = row.get("text", "")
                        if not row.get("structural") and RA._is_riven_title(title):
                            name = re.split(r"[A-Za-z]", title, maxsplit=1)[0].strip()
                            if name not in title_names and exact_observed_name(name):
                                title_names.append(name)
                    recognition["weapon_identity"] = {
                        "json_name": _ai_name,
                        "line_titles": title_names,
                        "selected": weapon_name,
                    }
                    if len(title_names) == 1:
                        title_name = title_names[0]
                        title_hits = exact_observed_name(title_name)
                        if main_hits and not any(w in title_hits for w in main_hits):
                            return pending_image(
                                L(
                                    "两路读到了不同的官方武器名，请核对卡片标题。",
                                    "The two readers found different official weapon names; please check the card title.",
                                )
                            )
                        weapon_name = title_name
                        recognition["weapon_identity"].update(
                            selected=weapon_name, source="line_title_exact"
                        )
                    elif not main_hits and re.fullmatch(r"[A-Za-z\s-]+", _ai_name):
                        return pending_image(
                            L(
                                "只读到了拉丁词缀，尚未确认卡片武器名。",
                                "Only the Latin suffix was read; the card's weapon name is unconfirmed.",
                            )
                        )
            source_note = L("（图片识别）", "(image recognition)")
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
                line_name_matches = []
                _lp, _ln, _legal, _notes = self._riven_lines_legal(
                    _lines, normalization=line_name_matches
                )
                hidden = RA.hidden_line_count(_lines)
                # ★ 2026-10-02 两路交叉校验（问题1）：行读**条数少于语义表**时也
                #   不许覆盖 —— 行读没报错也可能整行没抄（模型直接漏一行，不会产生
                #   notes）。但语义表自身必须**合法**才让位，否则会把 2026-09-27
                #   修掉的「语义表拆条/多条」老 bug 放回来（那次语义表 4 正 2 负）。
                _n_line = len(_lp) + len(_ln)
                # null 名称或值仍是原卡的一行，完整行读须覆盖原始分母。
                _n_sem = _splice_sem_count
                _raw_count_legal = bool(
                    RA.count_candidates(
                        len(self._iter_stat_pairs(data.get("positive"))),
                        len(self._iter_stat_pairs(data.get("negative"))),
                        hidden,
                    )
                )
                _normalized_count_legal = bool(
                    RA.count_candidates(len(stats_pos), len(stats_neg), hidden)
                )
                _sem_legal = (
                    _raw_count_legal and _normalized_count_legal and not splice_input_incomplete
                )
                logger.info(
                    "[sdjk] 紫卡行解析：%d 正 %d 负 %d 未揭示（模型语义表 %d 正 %d 负），跳过 %d 条",
                    len(_lp),
                    len(_ln),
                    hidden,
                    len(stats_pos),
                    len(stats_neg),
                    len(_notes),
                )

                # 对齐两路已有词条，只选某一路完整集合；不把两路拼成新卡。
                def evidence_rows(pos, neg):
                    return {
                        (sid, bucket, format(Decimal(str(value)).normalize(), "f"))
                        for pairs, bucket in ((pos, "positive"), (neg, "negative"))
                        for sid, value in pairs
                    }

                semantic = evidence_rows(stats_pos, stats_neg)
                literal = evidence_rows(_lp, _ln)
                shared_ids = {r[0] for r in semantic} & {r[0] for r in literal}
                conflicts = sorted(
                    sid
                    for sid in shared_ids
                    if {r[1:] for r in semantic if r[0] == sid}
                    != {r[1:] for r in literal if r[0] == sid}
                )
                selection = {
                    "semantic": sorted(semantic),
                    "line": sorted(literal),
                    "conflicts": conflicts,
                    "line_notes": list(_notes),
                    "semantic_raw_count": _n_sem,
                    "line_count": _n_line,
                    "selected": None,
                    "reason": "unresolved_evidence",
                }
                recognition["stat_selection"] = selection
                selection["unknown_semantic_rows_retained"] = [
                    row
                    for row in recognition.get("stat_normalization", [])
                    if row.get("decision") != "accepted"
                ]
                unknown_rows = selection["unknown_semantic_rows_retained"]
                remaining = literal - semantic
                # null 只能由另一条实际观察唯一补足；未知名称、非法值不得借完整行洗白。
                reconciled, unknown_unresolved = [], False
                for row in unknown_rows:
                    matches = []
                    title_attached_value = (
                        row.get("decision") == "unknown_or_ambiguous_name"
                        and row.get("raw_name") == weapon_name
                        and row.get("raw_value") is not None
                    )
                    if row.get("decision") == "unknown_name_or_value" or title_attached_value:
                        for candidate in remaining:
                            sid, bucket, value = candidate
                            raw_name, raw_value = row.get("raw_name"), row.get("raw_value")
                            if (
                                raw_name is not None
                                and not title_attached_value
                                and self._stat_id_from_name(str(raw_name), rev) != sid
                            ):
                                continue
                            if raw_value is None:
                                if raw_name is None and bucket != row.get("input_bucket"):
                                    continue
                            else:
                                probe = {
                                    row["input_bucket"]: [[RIVEN_STAT_ZH.get(sid, sid), raw_value]]
                                }
                                pp, pn = self._normalize_llm_stats(probe, rev)
                                if candidate not in evidence_rows(pp, pn):
                                    continue
                            matches.append(candidate)
                    if len(matches) != 1:
                        unknown_unresolved = True
                        break
                    remaining.remove(matches[0])
                    reconciled.append({"observation": row, "line_candidate": matches[0]})
                selection["null_reconciliation"] = reconciled
                # 主JSON把标题/尾注错装进词条，或把四行都放到正面桶时，
                # 已失去合法卡面结构。它不能否决独立、完整且无重复的逐行观察。
                # 两路各自合法时仍保留数值冲突与短读检查。
                line_ids = [sid for sid, _value in _lp + _ln]
                # 未知词条仍保留；只有可证明的包装破损/桶极性非法，才走布局恢复。
                # null 或陌生名称本身不是丢弃整路证据的理由。
                malformed_pairs = any(
                    isinstance(row, (list, tuple)) and len(row) != 2
                    for key in ("positive", "negative")
                    for row in (data.get(key) if isinstance(data.get(key), list) else [])
                )
                embedded_conflicts = any(
                    isinstance(row.get("raw_value"), dict)
                    and "invalid_observation" in row["raw_value"]
                    for row in unknown_rows
                )
                metadata_rows = [
                    row
                    for row in unknown_rows
                    if any(
                        re.fullmatch(
                            r"(?:段位|精通段位|等级需求|循环次数|内融值)\s*[:：]?\s*\d+",
                            str(row.get(key) or ""),
                        )
                        for key in ("raw_name", "raw_value")
                    )
                ]
                layout_broken = (
                    not _raw_count_legal
                    or (not _normalized_count_legal and not unknown_rows)
                    or malformed_pairs
                    or bool(metadata_rows)
                )
                required_count = min(_n_sem - len(metadata_rows), 4)
                selection.update(
                    conflicting_embedded_values=embedded_conflicts,
                    semantic_count_legal=_normalized_count_legal,
                    semantic_layout_broken=layout_broken,
                    semantic_metadata_rows=metadata_rows,
                    required_line_count=required_count,
                )
                if (
                    _legal
                    and not _sem_legal
                    and layout_broken
                    and not data.get("_numeric_precision_unsafe")
                    and len(set(line_ids)) == len(line_ids)
                    and _n_line + hidden >= required_count
                ):
                    stats_pos, stats_neg = _lp, _ln
                    source_note = L("（图片识别·卡面逐行）", "(image recognition · line by line)")
                    stat_name_matches = line_name_matches
                    recognition["selected_stat_source"] = "line_read"
                    recognition["line_stat_normalization"] = line_name_matches
                    splice_input_incomplete = False
                    selection.update(selected="line_read", reason="recover_invalid_semantic_layout")
                elif conflicts or data.get("_numeric_precision_unsafe") or unknown_unresolved:
                    splice_input_incomplete = True
                    selection["reason"] = (
                        "numeric_precision_unknown"
                        if data.get("_numeric_precision_unsafe")
                        else "unknown_semantic_unresolved"
                        if unknown_unresolved
                        else "cross_route_conflict"
                    )
                elif _legal and semantic <= literal and _n_line >= _n_sem:
                    stats_pos, stats_neg = _lp, _ln
                    source_note = L("（图片识别·卡面逐行）", "(image recognition · line by line)")
                    stat_name_matches = line_name_matches
                    recognition["selected_stat_source"] = "line_read"
                    recognition["line_stat_normalization"] = line_name_matches
                    splice_input_incomplete = False
                    selection.update(selected="line_read", reason="complete_existing_superset")
                elif _legal and not _sem_legal and not splice_input_incomplete:
                    stats_pos, stats_neg = _lp, _ln
                    source_note = L("（图片识别·卡面逐行）", "(image recognition · line by line)")
                    stat_name_matches = line_name_matches
                    recognition["selected_stat_source"] = "line_read"
                    splice_input_incomplete = False
                    selection.update(selected="line_read", reason="recover_invalid_semantic_layout")
                elif _legal and literal <= semantic and _sem_legal and not splice_input_incomplete:
                    selection.update(selected="semantic_json", reason="complete_existing_superset")
                else:
                    splice_input_incomplete = True
                if any(not n.startswith("无极性符号") for n in _notes):
                    splice_input_incomplete = True
                    selection.update(selected=None, reason="unparsed_suspected_line")
            if splice_input_incomplete:
                return pending_image(
                    L(
                        "有词条名称、数值或原文行未确认，请核对后重发。",
                        "Some stat names, values or text lines are unconfirmed; please check and resend.",
                    )
                )
            if not (stats_pos or stats_neg):
                return pending_image(
                    L(
                        "没有读出可确认的词条，请核对后重发。",
                        "No confirmable stats were read; please check and resend.",
                    )
                )
            if not weapon_name:
                # ★ 2026-09-25 ruff F821 修复：原写成未定义的 `abbr`，这条路一走
                #   就 NameError（用户拿到内部错误而不是下面这条提示）。
                hint = " ".join(
                    f"{fmt._stat_name(sid).replace(' ', '') if is_en() else RIVEN_STAT_ZH.get(sid, sid)}{num:g}"
                    for sid, num in stats_pos
                )
                return err_card(
                    L(
                        "图片识别到了词条但没读出武器名",
                        "Stats were read from the image but the weapon name was not",
                    ),
                    L(
                        f"请按文字格式补一次：紫卡分析 武器名 {hint}",
                        f"Resend in text form: rivenanalysis <weapon> {hint}",
                    ),
                    title=L("紫卡分析", "Riven Analysis"),
                )

        count_cands = RA.count_candidates(len(stats_pos), len(stats_neg), hidden)
        if not count_cands or not (stats_pos or stats_neg):
            return err_card(
                L(
                    "紫卡词条应为 2~3 条正面 + 0~1 条负面，"
                    f"当前解析到 {len(stats_pos)} 正 {len(stats_neg)} 负"
                    + (f" + {hidden} 条未揭示" if hidden else ""),
                    "A riven has 2-3 positive + 0-1 negative stats; "
                    f"parsed {len(stats_pos)} positive, {len(stats_neg)} negative"
                    + (f" + {hidden} unrevealed" if hidden else ""),
                ),
                L(
                    "核对词条条数后重发（负词条加「负」前缀）；截图发错也请重发",
                    "Check the stat count and resend (prefix a negative stat with -); "
                    "resend as well if you sent the wrong screenshot",
                ),
                title=L("紫卡分析", "Riven Analysis"),
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
            dup_note = L(
                "⚠ 同一词条出现两次（"
                + "、".join(RIVEN_STAT_ZH.get(s, s) for s in _dup_ids)
                + "）—— 紫卡不会有重复词条，其中一条很可能是识别错误"
                "（常见：元素伤害被读成暴击伤害），请重发一次截图核对",
                "⚠ The same stat appears twice ("
                + ", ".join(fmt._stat_name(s) for s in _dup_ids)
                + ") — a riven never has duplicate stats, so one is probably misread "
                "(commonly an elemental damage read as Critical Damage); please resend the screenshot to check",
            )
            logger.warning("[sdjk] 紫卡识别到重复词条：%s（共 %d 条）", _dup_ids, len(_all_stats))
        # ★ 2026-10-02 反转词条注脚（两条路径都要解释）：图片路径由 vision 按
        #   符号自动归负、人工输入由上面的自洽改判处理。行内**保留卡面符号**并
        #   带「卡面+号·负面」标签（见 fmt_riven_analysis），这里再解释机理，
        #   避免「卡面 + 号」与「分析按负面算」看起来矛盾（用户口径：
        #   +后坐力 = 增加后坐力 = 负面；-后坐力 = 减少 = 正面）。
        if not inverted_note:
            if any(RA.is_inverted(s) for s, _ in stats_neg):
                inverted_note = L(
                    "「后坐力」是反转词条：+ 号 = 增加后坐力 = "
                    "负面，区间按负面档系数计算"
                    "（- 号 = 减少后坐力 = 正面）",
                    "Recoil is an inverted stat: a + sign = more recoil = negative, "
                    "so the range uses the negative-tier factor "
                    "(a - sign = less recoil = positive)",
                )
            elif any(RA.is_inverted(s) for s, _ in stats_pos):
                inverted_note = L(
                    "「后坐力」是反转词条：- 号 = 减少后坐力 = "
                    "正面，区间按正面档系数计算"
                    "（+ 号 = 增加后坐力 = 负面）",
                    "Recoil is an inverted stat: a - sign = less recoil = positive, "
                    "so the range uses the positive-tier factor "
                    "(a + sign = more recoil = negative)",
                )
        # 具体武器缺失的类别标题：保留已核对转录，不猜具体武器、不输出评级/行情。
        # 沿用类别字典，任何类别均适用；不按截图或拉丁词缀识别测试样本。
        if image_data is not None and weapon_name.strip() in RA.RIVEN_CATEGORY_LABELS:
            recognition["weapon_status"] = "category_only"
            if check_only:
                from ..riven_ocr_reader import Fields

                return Fields(stats_pos, stats_neg, hidden)
            transcription = {
                "category": weapon_name.strip(),
                "positive": list(stats_pos),
                "negative": list(stats_neg),
                "hidden": hidden,
            }
            reply = Reply(
                L("紫卡识别", "Riven Scan"),
                [
                    L(
                        f"◆ 卡面类别：{weapon_name.strip()}",
                        f"◆ Card category: {weapon_name.strip()}",
                    ),
                    L(
                        "◆ 具体武器未确认；已保留词条，不进行武器评级、融合或行情查询。",
                        "◆ The exact weapon is unconfirmed; stats are kept, with no weapon rating, splicing or price lookup.",
                    ),
                    *[f"· {row}" for row in image_data.get("lines", [])],
                ],
            )
            reply.riven_transcription = transcription
            return reply
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
                    RA.disp_feasible(
                        stats_pos,
                        stats_neg,
                        _ccls,
                        _cd,
                        counts=_counts,
                        stat_multipliers=RA.weapon_stat_multipliers(_c),
                    )
                    for _counts in count_cands
                ):
                    _feas.append((_c, _cd))
            if len(_feas) == 1:
                weapon, _near_disp = _feas[0]
                # 纪律（2026-09-02 形近容错立的规矩）：**禁静默改判** —— 采纳时
                # 既写卡面注记，也留状态日志；原始识别名不进入日志。
                logger.info(
                    "[sdjk] 近形字兜底：状态=已采信，类别=唯一数值可行候选，倾向 %g",
                    _near_disp,
                )
                nearmiss_note = L(
                    f"卡面识别名「{weapon_name.strip()}」→ 已按"
                    f"「{weapon.get('zh') or weapon.get('en')}」计算"
                    f"（数值与倾向 {_near_disp:g} 吻合）",
                    f'Card name "{weapon_name.strip()}" → calculated as '
                    f'"{weapon.get("en") or weapon.get("zh")}" '
                    f"(values match disposition {_near_disp:g})",
                )
            elif _feas:
                _near_names = [
                    (c.get("en") or c.get("zh") or "")
                    if is_en()
                    else (c.get("zh") or c.get("en") or "")
                    for c, _ in _feas
                ]
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
                L(
                    f"未找到紫卡武器「{weapon_name.strip()}」",
                    f'Riven weapon not found: "{weapon_name.strip()}"',
                ),
                L(
                    "照候选改名重发" if tips else "换官方中文名或英文名重发",
                    "Resend using one of the suggestions"
                    if tips
                    else "Check the spelling and resend with the weapon's official English name",
                ),
                (L("你是不是想找", "Did you mean"), L("、", ", ").join(tips)) if tips else "",
                title=L("紫卡分析", "Riven Analysis"),
            )
        cls = RA.weapon_class(weapon.get("riven_type", ""), weapon.get("group", ""))
        _kitgun_type = weapon.get("riven_type") or ""
        # ★ 2026-10-03：WM 拆分行（捕月（主要）等 (Primary)/(Secondary) 行）
        # riven_type/group 为空 ⇒ weapon_class 回落「rifle」会拿错基值。
        # 基名行在表里 ⇒ 继承基名行的类别。
        if not (weapon.get("riven_type") or weapon.get("group")):
            _mb = re.match(r"^(.+?)\s*\([^)]+\)\s*$", (weapon.get("en") or "").strip())
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
            L(
                "⚠ 武器类别未知，按步枪基值估算",
                "⚠ Weapon class unknown; estimated with the rifle base value",
            )
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
        name = (
            _disp_name(weapon)
            if is_en()
            else weapon.get("zh") or weapon.get("en") or weapon["url_name"]
        )
        mother_name = name
        stat_multipliers = RA.weapon_stat_multipliers(weapon)
        # ── 小数点修正（2026-09-24 玩家报障：115.7% 读成 1157%）────────────
        # 区间仅能给出代码推断候选，不能确认图片中的小数点；图片候选须先交用户核对。
        # 纯文字输入保留既有修正行为。
        decimal_fix: list[str] = []
        # 词条数（决定系数）：有「？？？」未揭示词条时按总行数定；3 行无负词条两可
        # （3+0 / 2+1）⇒ 用卡面数值挑能吻合的那个，都不吻合按先列的（未揭示当正）算
        counts = count_cands[0]
        if len(count_cands) > 1:
            counts = next(
                (
                    c
                    for c in count_cands
                    if RA.disp_feasible(
                        stats_pos, stats_neg, cls, disp, counts=c, stat_multipliers=stat_multipliers
                    )
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
                lo, hi = RA.stat_range(
                    sid, cls, disp, *counts, negative=negative, stat_multipliers=stat_multipliers
                )
                if not lo or not hi:
                    out.append((sid, v))
                    continue
                if not (lo * 0.7 <= v <= hi * 1.4):
                    v10 = v / 10.0
                    if lo * 0.85 <= v10 <= hi * 1.15:
                        if image_data is None:
                            decimal_fix.append(f"{RA.fmt_value(sid, v)} → {RA.fmt_value(sid, v10)}")
                            v = v10
                        else:
                            decimal_fix.append(
                                L(
                                    f"{RIVEN_STAT_ZH.get(sid, sid)}：原值 {RA.fmt_value(sid, v)}"
                                    f" → 推断候选 {RA.fmt_value(sid, v10)}",
                                    f"{'Damage' if sid == 'melee_damage' and cls != 'melee' else fmt._stat_name(sid)}: original {RA.fmt_value(sid, v)}"
                                    f" → inferred candidate {RA.fmt_value(sid, v10)}",
                                )
                            )
                out.append((sid, v))
            return out

        stats_pos = _fix_decimal(stats_pos, False)
        stats_neg = _fix_decimal(stats_neg, True)
        if image_data is not None and decimal_fix:
            recognition = image_data.setdefault("_recognition", {})
            if isinstance(recognition, dict):
                recognition["numeric_candidates"] = list(decimal_fix)
            return pending_image(
                L(
                    "区间提示小数点可能有误，尚无法从图片确认。",
                    "The range hints at a possible decimal-point error that the image cannot confirm.",
                ),
                decimal_fix,
            )
        if image_data is not None:
            # 语义JSON可合法地遗漏整行；本身没有截图覆盖的观测分母。
            # 不按武器/数值/卡ID猜漏项，也不因达到2~3正的合法计数而自证完整。
            observed_lines = image_data.get("lines")
            has_observed_lines = isinstance(observed_lines, (list, tuple, str)) and bool(
                observed_lines
            )
            recognition["image_integrity"] = {
                "status": "line_observation_available" if has_observed_lines else "unconfirmed",
                "semantic_rows": _splice_sem_count,
                "evidence": "recorded_line_observation" if has_observed_lines else None,
                "reason": "line_observation_retained"
                if has_observed_lines
                else "semantic_json_has_no_observation_denominator",
                "image_accuracy_verified": False,
            }
            if not has_observed_lines:
                return pending_image(
                    L(
                        "仅有语义词条表，缺少完整逐行原文，尚无法排除整行漏读。",
                        "Only the semantic stat table exists, without the full line-by-line text; "
                        "a missed line cannot be ruled out.",
                    )
                )
        # 下方用于判断条数的虚拟负面不能参与融合。
        splice_stats_pos = list(stats_pos)
        splice_stats_neg = list(stats_neg)
        # 负词条可能被漏识别：卡面负词条常写成「x0.55 对 Corpus 的伤害」这类
        # 乘数形式，vision 容易整行丢掉 —— 词条数系数会从 (n正,1)=0.9375
        # 错成 (n正,0)=0.75，区间整体偏小 20%，表现为「卡面数值与倾向都不吻合」。
        # 判据：0 负解释不了、1 负能解释 → 按 1 负算（区间只依赖正词条系数）。
        neg_fix_note = ""
        if not stats_neg and stats_pos and len(stats_pos) == 3 and not hidden:
            try:
                if not RA.disp_feasible(
                    stats_pos, [], cls, disp, stat_multipliers=stat_multipliers
                ) and RA.disp_feasible(
                    stats_pos,
                    [("damage_vs_corpus", 45.0)],
                    cls,
                    disp,
                    stat_multipliers=stat_multipliers,
                ):
                    stats_neg = [("damage_vs_corpus", 45.0)]
                    counts = (3, 1)
                    neg_fix_note = L(
                        "⚠ 卡面疑似有未被识别的负词条（常见写法"
                        "「x0.55 对 Corpus 的伤害」这类乘数形式），"
                        "已按 3正1负 的系数计算",
                        "⚠ The card may have an unrecognized negative stat (commonly a multiplier form "
                        'such as "x0.55 Damage to Corpus"); calculated with the 3 positive + 1 negative factor',
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
        legacy_verdict = False  # 命中「取消前旧倾向」的旧截图判定
        if not RA.disp_feasible(
            stats_pos, stats_neg, cls, disp, counts=counts, stat_multipliers=stat_multipliers
        ):
            iv = RA.disposition_interval(
                stats_pos, stats_neg, cls, counts=counts, stat_multipliers=stat_multipliers
            )
            verdict = L(
                f"卡面数值与倾向 {disp:g} 不吻合",
                f"card values do not match disposition {disp:g}",
            )
            infer_note = L(
                f"⚠️ 卡面数值与倾向 {disp:g} 不吻合，请核对武器与词条",
                f"⚠️ Card values do not match disposition {disp:g}; please check the weapon and stats",
            )
            if iv[0] and not disp_override:
                _old = [(mother_name, legacy_disp)] if legacy_disp > 0 else []
                _old += list(await self.client.riven_family(weapon))
                _hit = [n for n, v in _old if iv[0] <= float(v) <= iv[1]]
                if _hit:
                    legacy_verdict = True
                    verdict = L(
                        "疑似倾向取消前的旧截图",
                        "likely an old screenshot from before dispositions were removed",
                    )
                    infer_note = L(
                        f"⚠️ 卡面数值与取消前的旧倾向吻合（{'、'.join(_hit[:3])}）"
                        f"—— 疑似倾向取消前的截图，游戏内数值已按 {disp:g} 缩放，请重新截图后再发",
                        f"⚠️ Card values match the old pre-removal disposition ({', '.join(_hit[:3])}) "
                        f"— likely a screenshot from before the removal; in-game values are now scaled to "
                        f"{disp:g}, please take a new screenshot and resend",
                    )
            # 识别模型可能干脆不念「？？？」那行（2026-10-24）⇒ 条数少一条、区间整体偏移。
            # 不是旧截图、**多一条正词条**就吻合 ⇒ 按多一条算并明说（同上方负词条兜底的闸门思路）
            _more = (counts[0] + 1, counts[1])
            if (
                not legacy_verdict
                and not hidden
                and _more in RA.FACTOR
                and RA.disp_feasible(
                    stats_pos, stats_neg, cls, disp, counts=_more, stat_multipliers=stat_multipliers
                )
            ):
                counts = _more
                verdict = infer_note = ""
                hidden_fix_note = L(
                    "⚠ 卡面疑似还有一条没读出的词条（融合时未选定的「？？？」或漏识别），"
                    f"已按 {counts[0]}正{counts[1]}负 的系数计算",
                    '⚠ The card may have one more unread stat (an unselected "???" from splicing, or a '
                    f"missed read); calculated with the {counts[0]} positive + {counts[1]} negative factor",
                )
        disp_note = (
            L(
                f"（已按手输倾向 {disp:g} 计算；DE 已统一为 {RA.UNIFIED_DISPOSITION:g}）",
                f"(calculated with your manual disposition {disp:g}; DE has unified it to "
                f"{RA.UNIFIED_DISPOSITION:g})",
            )
            if disp_override
            else ""
        )

        if image_data is not None and (neg_fix_note or hidden_fix_note):
            recognition["completeness_candidates"] = {
                "source": "numeric_feasibility_inference",
                "counts_candidate": list(counts),
                "adopted": False,
                "notes": [n for n in (neg_fix_note, hidden_fix_note) if n],
            }
            return pending_image(
                L(
                    "数值关系提示可能还有漏读词条，不能据此确认条数，请核对原图。",
                    "The value relationships hint at more unread stats, so the count cannot be confirmed; "
                    "please check the original image.",
                )
            )

        if check_only:
            from ..riven_ocr_reader import Fields

            return Fields(stats_pos, stats_neg, hidden)

        # 融合预测只消费实际读到的最终词条；接口与展示样式独立。
        from .. import matching as _splice_matching
        from .. import riven_pools as RP

        _weapon_forms = {
            _splice_matching.normalize(weapon.get(k) or "") for k in ("zh", "en", "url_name")
        }
        if any(
            _splice_matching.normalize(t) not in _weapon_forms for t in splice_unmatched_numeric
        ):
            splice_input_incomplete = True
        _splice_pool = RP.class_pool(cls)
        _splice_root, _splice_members = RP.family_members(weapon.get("en") or "")
        _splice_allowed: dict[str, set[str]] = {}
        for positive, polarity in ((True, "positive"), (False, "negative")):
            available, _ = RP.available(cls, _splice_root, _splice_members, positive=positive)
            for sid in available:
                _splice_allowed.setdefault(sid, set()).add(polarity)
        # 当前词条池之外也可能有古董卡或例外；不能直接宣称可融合。
        # 物理占比属于有例外的推导，本功能不把它升级为已有词条的硬闸门。
        _splice_unverified = any(
            sid not in RP.PHYSICAL_STATS and polarity not in _splice_allowed.get(sid, ())
            for rows, polarity in ((splice_stats_pos, "positive"), (splice_stats_neg, "negative"))
            for sid, _ in rows
        )
        _splice_block = ""
        if class_est_note:
            _splice_block = "unknown_weapon_class"
        elif _splice_unverified:
            _splice_block = "unverified_weapon_traits"
        elif disp != RA.UNIFIED_DISPOSITION:
            _splice_block = "non_current_disposition"
        elif verdict:
            _splice_block = "legacy_values" if legacy_verdict else "unverified_values"
        splice_preview = RA.splice_preview(
            splice_stats_pos,
            splice_stats_neg,
            cls,
            disp,
            counts=counts,
            hidden=hidden,
            incomplete=bool(splice_input_incomplete or neg_fix_note or hidden_fix_note),
            pool=_splice_pool,
            allowed_missing=_splice_allowed,
            blocked_reason=_splice_block,
            stat_multipliers=stat_multipliers,
        )

        # 提示分流（2026-10-11 卡片改版）：需要用户行动的（重发 / 核对）进 ⚠ 警示；
        #   其余说明合并成一条注脚；来源（图片识别）进页脚。
        def _needs_action(t: str) -> bool:
            return "⚠" in t or "请带变体" in t or "请核对" in t or "请重发" in t

        _cands_n = [infer_note, neg_fix_note, hidden_fix_note, dup_note, class_est_note]
        warns = [t for t in _cands_n if t and _needs_action(t)]
        normalized_names = []
        selected_ids = {sid for sid, _ in stats_pos + stats_neg}
        for match in stat_name_matches:
            if match.get("sid") in selected_ids and match.get("method") in {
                "one_edit",
                "contained_alias",
            }:
                label = (
                    L("单字近名", "one-letter near match")
                    if match["method"] == "one_edit"
                    else L("原文含完整词名", "text contains the full stat name")
                )
                note = L(
                    f"{match['raw_name']} → {RIVEN_STAT_ZH.get(match['sid'], match['sid'])}"
                    f"（{label}）",
                    f"{match['raw_name']} → {fmt._stat_name(match['sid'])} ({label})",
                )
                if note not in normalized_names:
                    normalized_names.append(note)
        infos = [
            t
            for t in (
                L("词名按代码归一：", "Stat names normalized by code: ")
                + L("；", "; ").join(normalized_names)
                if normalized_names
                else "",
                nearmiss_note,
                infer_note if infer_note and not _needs_action(infer_note) else "",
                disp_note,
                inverted_note,
                (
                    L(
                        "已修正小数点（截图未读出点号）：",
                        "Decimal point corrected (not read from the screenshot): ",
                    )
                    + L("、", ", ").join(decimal_fix)
                )
                if decimal_fix
                else "",
            )
            if t
        ]
        price_ref = None
        # 只用实际读到且完整可信的输入，不把条数推断的占位值当作用户词条。
        _price_complete = (
            not hidden
            and not splice_input_incomplete
            and not (neg_fix_note or hidden_fix_note or dup_note or _splice_block)
            and counts == (len(splice_stats_pos), len(splice_stats_neg))
            and splice_preview.get("status") in {"ready", "already_spliced", "no_match"}
            and all(math.isfinite(v) and v > 0 for _, v in splice_stats_pos + splice_stats_neg)
        )
        if _price_complete:
            price_ref = await self._riven_analysis_price(
                weapon, splice_stats_pos, splice_stats_neg, platform
            )
        analysis_kw = dict(
            verdict=verdict,
            warns=warns,
            infos=infos,
            counts=counts,
            hidden=hidden,
            splice=splice_preview,
            price=price_ref,
            stat_multipliers=stat_multipliers,
        )
        title, lines = fmt.fmt_riven_analysis(
            name,
            disp,
            cls,
            stats_pos,
            stats_neg,
            splice_limit=fmt.SPLICE_FIRST_ROWS,
            **analysis_kw,
        )
        logger.info(
            "[sdjk] 紫卡分析耗时 %.0f ms（含识别/查询/计算）",
            (time.perf_counter() - started) * 1000,
        )
        reply = Reply(
            title,
            lines,
            footer=fmt.fmt_platform_footer(
                platform,
                (f"{source_note.strip('（）()')} · " if source_note else "")
                + L(
                    f"DE 属性基值公式 · 倾向统一 {RA.UNIFIED_DISPOSITION:g}",
                    f"DE stat base-value formula · Disposition unified at {RA.UNIFIED_DISPOSITION:g}",
                ),
            ),
        )
        reply.riven_splice = splice_preview
        # 融合建议超过首屏条数：图片 / 纯文字出第 2 页「续页」（当前卡的剩余预测）；
        # QQ Markdown 一条消息列全部（文字可滚动，不必拆页）。区间「~」在 Markdown 里会被当删除线，
        # Markdown 正文换成全角「～」，图片与纯文字不变。
        more = fmt.fmt_riven_splice_more(name, cls, splice_preview)
        if more:
            reply.pages = [(title, lines), more]
        try:  # 服务器以包成员加载，相对导入才可靠（同上）
            from ..render import card_markdown
        except ImportError:  # pragma: no cover - 本地直跑
            from core.render import card_markdown
        _, full = (
            fmt.fmt_riven_analysis(name, disp, cls, stats_pos, stats_neg, **analysis_kw)
            if more
            else (title, lines)
        )
        if getattr(self, "cfg", {}).get("qq_markdown_cards", True):
            reply.markdown = card_markdown(
                title, [x.replace("~", "～") for x in full], reply.footer.replace("~", "～")
            )
        query_name = (
            _disp_name(weapon)
            if is_en()
            else weapon.get("zh") or weapon.get("en") or weapon.get("url_name")
        )
        if can_actions and query_name:
            reply.actions.extend(
                [
                    Action(
                        L("词条范围", "Stat Ranges"),
                        L(f"词条范围 {query_name}", f"rivenranges {query_name}"),
                    ),
                    Action(
                        L("倾向", "Disposition"), L(f"倾向 {query_name}", f"dispo {query_name}")
                    ),
                ]
            )
            if price_ref is not None:
                # 与卡上「匹配条件」同一组词条；wr 认短名与「负」前缀
                words = [fmt._stat_name(s) for s in price_ref["pos"]] + [
                    L("负", "neg ") + fmt._stat_name(s) for s in price_ref["neg"]
                ]
                reply.actions.append(
                    Action(
                        L("同词条拍卖", "Same-Stat Auctions"),
                        f"wr {query_name} {' '.join(words)}",
                    )
                )
        return reply

    async def _riven_analysis_price(
        self, weapon: dict, stats_pos: list, stats_neg: list, platform: str
    ) -> Optional[dict]:
        """完整观测词条的一口价参考；正负名称与条数须完全对齐。

        只做一次完整条件查询，禁止去条件回退和自动重试；家族解析和查询
        共用独立时间预算。数值、洗卡次数不作为同词条匹配条件。
        """
        pos_ids = [s for s, _ in stats_pos]
        neg_ids = [s for s, _ in stats_neg]
        if not pos_ids:
            return None
        match = {"pos": pos_ids, "neg": neg_ids, "relaxed": False, "match_mode": "exact_stat_set"}

        async def query() -> dict:
            from .market import riven_market_weapon

            name = weapon.get("zh") or weapon.get("en") or weapon.get("url_name") or ""
            mw = await riven_market_weapon(self.client, name, weapon)
            rtype = mw.get("riven_type", "")
            # AND条件与顺序无关，规范化顺序使同组词条复用缓存。
            pos = sorted(set(self.client.normalize_riven_stats(pos_ids, rtype)))
            neg = sorted(set(self.client.normalize_riven_stats(neg_ids, rtype)))
            if len(pos) != len(pos_ids) or len(neg) != len(neg_ids):
                raise WarframeAPIError("词条无法完整转换为市场查询条件")
            auctions = await self.client.wm_riven_auctions(
                mw["url_name"],
                platform,
                positives=pos,
                negatives=neg,
                sorts=("price_asc",),
                retries=0,
                allow_attribute_fallback=False,
                timeout=RIVEN_ANALYSIS_PRICE_REQUEST_TIMEOUT_S,
            )
            pool = []
            pos_set, neg_set = set(pos), set(neg)
            for auction in auctions:
                if not isinstance(auction, dict) or not isinstance(auction.get("item"), dict):
                    continue
                attrs = auction["item"].get("attributes")
                if not isinstance(attrs, list) or len(attrs) != len(pos) + len(neg):
                    continue
                if any(
                    not isinstance(at, dict)
                    or not isinstance(at.get("url_name"), str)
                    or type(at.get("positive")) is not bool
                    for at in attrs
                ):
                    continue
                has_p = {at["url_name"] for at in attrs if at["positive"] is True}
                has_n = {at["url_name"] for at in attrs if at["positive"] is False}
                # 集合相等并核对总条数，排除多词条、重复词条和错误极性。
                if has_p == pos_set and has_n == neg_set:
                    pool.append(auction)
            summary = self.client.riven_buyout_summary(pool)
            summary.update(match)
            return summary

        try:
            return await asyncio.wait_for(query(), timeout=RIVEN_ANALYSIS_PRICE_BUDGET_S)
        except (WarframeAPIError, TimeoutError) as exc:
            logger.warning("[sdjk] 紫卡分析同词条挂单暂不可用：%s", type(exc).__name__)
            return {"error": type(exc).__name__, **match}
        except Exception as exc:  # 附加信息异常不打挂分析；日志只保留错误类别。
            logger.error("[sdjk] 紫卡分析同词条挂单内部异常：%s", type(exc).__name__)
            return {"error": type(exc).__name__, **match}
