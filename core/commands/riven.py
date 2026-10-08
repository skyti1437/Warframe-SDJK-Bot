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
from typing import Optional

from .. import formatters as fmt
from .. import matching
from ..api_client import WarframeAPIError, fuzzy_hits
from ..logging_compat import logger
from .base import Reply


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
            return Reply(
                raw_text=(
                    "用法：xh 武器名 [元素] [数值]\n"
                    "　例：xh 赤毒怒雷 ｜ xh 信条弧电离子枪 辐射 ｜ xh 赤毒海克 50\n"
                    "　元素：磁力/电击/毒素/火焰/冰冻/冲击/切割/辐射\n"
                    f"　已收录 {len(self.client._aliases.get('lich_items', {}))} 个中文写法"
                    "（赤毒/信条/科达三类）"
                )
            )
        first = toks[0]
        slug = self.client.resolve_lich_weapon(first) or await self._lich_slug_by_riven(first)
        if not slug:
            near = fuzzy_hits(first, list(self.client._aliases.get("lich_items", {})), n=3) or []
            hint = ("；你是不是想找：" + "、".join(near)) if near else ""
            return Reply(
                raw_text=f"未找到玄骸武器「{first}」{hint}\n"
                "支持赤毒/信条/科达三类，可只写后半段（如「怒雷」）"
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
            # 市场侧失败要说清原因（限速/网络），不能糊成「内部错误」
            return Reply(
                raw_text=f"warframe.market 查询失败：{exc}\n"
                "多为市场限速（3 请求/秒）或网络抖动，"
                "过几秒重试即可。"
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
                msg = "该武器当前没有挂单（冷门武器挂单少，可过段时间再看）"
            else:
                msg = "暂无符合条件的挂单" + (f"（筛选：{'·'.join(filters)}）" if filters else "")
            return Reply(
                title,
                msg.splitlines(),
                footer=fmt.fmt_platform_footer(platform, "warframe.market 玄骸"),
            )
        lines = [fmt.fmt_lich_row(i, a) for i, a in enumerate(pool[:12], 1)]
        lines.append("※ 在线优先排序；信用=卖家交易信誉等级（0~5），幻纹✦ 表示带幻纹")
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
                f"warframe.market 没有「{name}」的拍卖类目。",
                f"这**不是识别失败**：中文名已正常匹配到 {slug}，是市场侧没这个类目。",
                f"（已逐把核对全部 {total} 把玄骸武器：{has} 把有挂单、{total - has} 把没有）",
                f"· 换一把同系列：发「{example}」",
                f"· 网站自查：warframe.market/zh-hans/auctions/search"
                f"?type={kind}&weapon_url_name={slug}",
                f"· 这类武器（全部终幕 + 部分近战{sibling}）只能游戏内交易频道收",
            ],
            footer=fmt.fmt_platform_footer(platform, "warframe.market 玄骸"),
        )

    async def _lich_slug_by_riven(self, q: str) -> Optional[str]:
        """黑话兜底：尝试从 riven 别名表取基础武器 slug 前缀匹配玄骸。"""
        hit = self.client.alias_lookup(q.lower(), "riven_items")
        return f"kuva_{hit.split('_prime')[0]}" if hit and hit.split("_prime")[0] else None

    async def _h_disposition(self, parsed, event, platform) -> Reply:
        """紫卡倾向查询。"""
        query = parsed.content_str
        weapons = await self.client.wm_riven_weapons()
        if not query:
            top = sorted(
                [w for w in weapons if w.get("disposition")], key=lambda w: -w["disposition"]
            )[:8]
            lines = [
                f"· {(w.get('zh') or w.get('en') or w['url_name'])}　倾向 {w['disposition']:.2f}"
                for w in top
            ]
            return Reply(
                "紫卡倾向 Top8（越高越容易出好卡）", lines, footer=fmt.fmt_platform_footer(platform)
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
            close = matching.suggest_zh(query, weapons)
            return Reply(
                raw_text="未找到该武器" + (f"，你是不是想找：{'、'.join(close)}" if close else "")
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

        lines = [
            f"· {(w.get('zh') or w.get('en') or w['url_name'])}　"
            f"倾向 {w.get('disposition', 0):.2f}　"
            # ★ 2026-09-24：带上 group —— WM 把曲翼枪械的 rivenType 也标成
            #   rifle（翠雀显示成「步枪」），group 才是准的（曲翼枪械/守护武器）
            f"{_cls(w)}"
            for w in hits[:8]
        ]
        return Reply(f"紫卡倾向：{query}", lines, footer=fmt.fmt_platform_footer(platform))

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
        for tok in parsed.content or []:
            t = tok.strip()
            if not t:
                continue
            neg = t.startswith(("负", "-"))
            body = t[1:] if neg else t
            # ★ 2026-09-24：词条名放宽到 1~8 字（卡面原文「滑行攻击暴击几率」6 字，
            #   旧限 1~4 字会整条落到武器名里 → 负词条丢失、反推区间算错）
            # ★ 2026-10-02 上限 8→12 字：DE 卡面原文「的几率来获得连击数」是 9 字，
            #   旧限会让整条落进 weapon_name（静默丢词条 + 污染武器名）。
            m = _re.fullmatch(r"([\u4e00-\u9fa5]{1,12}?)(\d+(?:\.\d+)?)", body)
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
                return Reply(
                    raw_text="用法：紫卡分析 武器名 词条数值…（负词条加「负」前缀）\n"
                    "　例：紫卡分析 欧玛 暴伤82.8 范围1.6 攻速45.8 负滑暴81.3\n"
                    "　也可直接发「紫卡分析 + 紫卡截图」"
                )
            imgs = await self._image_data_urls(event)
            if not imgs:
                return Reply(raw_text="图片下载失败，请重发一次截图")
            data = await self._extract_riven_from_image(imgs[0])
            if not data:
                return Reply(
                    raw_text="图片识别失败（vision 渠道不可用或未配）——"
                    "请按文字格式发送：紫卡分析 武器名 暴伤82.8 范围1.6 "
                    "负滑暴81.3"
                )
            stats_pos, stats_neg = self._normalize_llm_stats(data, rev)
            # ★ 2026-10-07（用户报障「紫卡分析 冰凇 + 截图 ⇒ 未找到紫卡武器「冰松」」）：
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
                # ★ 2026-10-02 两路交叉校验（问题1）：行读**条数少于语义表**时也
                #   不许覆盖 —— 行读没报错也可能整行没抄（模型直接漏一行，不会产生
                #   notes）。但语义表自身必须**合法**才让位，否则会把 2026-09-27
                #   修掉的「语义表拆条/多条」老 bug 放回来（那次语义表 4 正 2 负）。
                _n_line = len(_lp) + len(_ln)
                _n_sem = len(stats_pos) + len(stats_neg)
                _sem_legal = 2 <= len(stats_pos) <= 3 and len(stats_neg) <= 1
                logger.info(
                    "[sdjk] 紫卡行解析：%d 正 %d 负（模型语义表 %d 正 %d 负）%s",
                    len(_lp),
                    len(_ln),
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
            if not stats_pos:
                return Reply(
                    raw_text="图片识别到了武器但没读出词条，请按文字格式重发："
                    "紫卡分析 武器名 暴伤82.8 范围1.6 负滑暴81.3"
                )
            if not weapon_name:
                # ★ 2026-09-25 ruff F821 修复：原写成未定义的 `abbr`，这条路一走
                #   就 NameError（用户拿到内部错误而不是下面这条提示）。
                hint = " ".join(f"{RIVEN_STAT_ZH.get(sid, sid)}{num:g}" for sid, num in stats_pos)
                return Reply(
                    raw_text="图片识别到了词条但没读出武器名，"
                    f"请按文字格式补一次：紫卡分析 武器名 {hint}"
                )

        if not 2 <= len(stats_pos) <= 3 or len(stats_neg) > 1:
            return Reply(
                raw_text="紫卡词条应为 2~3 条正面 + 0~1 条负面，"
                f"当前解析到 {len(stats_pos)} 正 {len(stats_neg)} 负"
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
        # 武器解析 + 变体倾向查询互不依赖 → 并行（原来串行，实测分析段 4 s）
        _t_res0 = asyncio.gather(
            self.client.resolve_riven_weapon(weapon_name.strip()),
            self.client.resolve_variant_disp(weapon_name.strip()),
            return_exceptions=True,
        )
        weapon, _t_variant = await _t_res0
        if isinstance(weapon, BaseException):
            weapon = None
        if isinstance(_t_variant, BaseException):
            _t_variant = (None, "")
        variant_disp_pre, variant_key_pre = _t_variant or (None, "")
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
                _cd = float(_c.get("disposition") or 0)
                if _cd > 0 and RA.disp_feasible(stats_pos, stats_neg, _ccls, _cd):
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
            tip = ("，你是不是想找：" + "、".join(tips)) if tips else ""
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
            return Reply(raw_text=f"未找到紫卡武器「{weapon_name.strip()}」{tip}")
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
        # 倾向来历：手输覆盖（棱晶等变体 WM 没数据，卡主最准）> WM/母武器值。
        # 游戏内紫卡不显示倾向数值，LLM 从卡面"读倾向"只会把内融值之类的
        # 数字当倾向（教训：49 → 区间爆表），所以永远不采信 LLM。
        wm_disp = float(weapon.get("disposition") or 0)
        # 倾向来历：手输覆盖 > wiki 变体表（棱晶等变体 WM 没数据）> WM 母武器。
        variant_disp, variant_key = None, ""
        if weapon_name.strip() != (weapon.get("zh") or weapon.get("en") or ""):
            variant_disp, variant_key = (variant_disp_pre, variant_key_pre)
        disp = disp_override or variant_disp or wm_disp
        if disp <= 0:
            return Reply(
                raw_text=f"WM 未返回「{weapon_name.strip()}」的倾向数值，无法计算区间；"
                "变体武器可手输：紫卡分析 武器名 倾向0.95 词条…"
            )
        name = weapon.get("zh") or weapon.get("en") or weapon["url_name"]
        mother_name = name
        # ── 小数点修正（2026-09-24 用户报障：115.7% 读成 1157%）────────────
        # vision 偶发把小数点读丢，整卡数值随之「都不吻合」。用**当前倾向的合法
        # 区间**做判据：原值明显超出区间、除以 10 落回区间内 → 修正并在卡面注明。
        decimal_fix: list[str] = []

        def _fix_decimal(pairs, negative: bool):
            out = []
            for sid, v in pairs:
                lo, hi = RA.stat_range(
                    sid, cls, disp, len(stats_pos), len(stats_neg), negative=negative
                )
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
        if not stats_neg and stats_pos and len(stats_pos) == 3:
            try:
                if not RA.disp_feasible(stats_pos, [], cls, disp) and RA.disp_feasible(
                    stats_pos, [("damage_vs_corpus", 45.0)], cls, disp
                ):
                    stats_neg = [("damage_vs_corpus", 45.0)]
                    neg_fix_note = (
                        "⚠ 卡面疑似有未被识别的负词条（常见写法"
                        "「x0.55 对 Corpus 的伤害」这类乘数形式），"
                        "已按 3正1负 的系数计算"
                    )
            except Exception:  # noqa: BLE001 —— 纠错失败不影响主流程
                pass
        if variant_disp or disp_override:
            name = weapon_name.strip() or name  # 保留用户输入的变体名
        # ── 数值反推倾向 ────────────────────────────────────────────────
        # 卡面只写母武器名（变体信息根本不在截图里），但数值 =
        # 基值 × 倾向 × 词条系数 × U(0.9~1.1) 可以反着解出倾向；家族内
        # （母武器 + 棱晶/Prime/亡魂…）通常只有一个候选能解释全部词条，
        # 据此自动判定该按谁的倾向算 —— 不用手输、也不用带变体名。
        infer_note = ""
        fam_all = []
        if not disp_override:
            fam_all = [
                (n, v) for n, v in await self.client.riven_family(weapon) if abs(v - wm_disp) > 1e-9
            ]
            if not variant_disp:
                # ★ 2026-10-01：母武器倾向可行就不做变体推断（卡面本来就写着
                #   武器名）——旧实现会让多值命中回「数值与多个倾向都吻合
                #   请带变体名重发」，而母武器自身就是合理解释。
                if RA.disp_feasible(stats_pos, stats_neg, cls, wm_disp):
                    pass  # 母武器可行：跳过变体推断（family_note 仍可展示参考）
                else:
                    # ★ 2026-10-03：家族候选按各自模式换基值列（kitgun 主要
                    #   形态逐腔体霰弹/步枪列），母行用母行类别（三元组）。
                    _fam_c = [
                        (n, v, RA.kitgun_mode_class(n, cls, _kitgun_type)) for n, v in fam_all
                    ]
                    fits = RA.match_disposition(
                        stats_pos, stats_neg, cls, [(mother_name, wm_disp, cls)] + _fam_c
                    )
                    # 多个变体**倾向同值**时不算歧义 —— 区间只由倾向数值决定，
                    # 名字（棱晶/Prime）不影响结果（棱晶·空刃与空刃 Prime 同为
                    # 1.2）。旧实现一律回「请带变体名重发」，卡面就仍按母武器的
                    # 1.3 计算（2026-10-01 用户报障的空刃 3+1 那张正是如此）。
                    fit_vals = sorted({round(float(v), 4) for _, v in fits})
                    if len(fits) == 1 and abs(fits[0][1] - wm_disp) > 1e-9:
                        name, disp = fits[0]  # 唯一吻合且不是母武器
                        infer_note = (
                            f"数值反推倾向 {disp:g}：唯一吻合 {name}"
                            f"（母武器 {mother_name} {wm_disp:g} 不吻合）"
                        )
                    elif len(fit_vals) == 1 and abs(fit_vals[0] - wm_disp) > 1e-9:
                        disp = fit_vals[0]
                        infer_note = (
                            f"数值反推倾向 {disp:g}：吻合 "
                            + "、".join(n for n, _ in fits)
                            + f"（母武器 {mother_name} {wm_disp:g} 不吻合）"
                        )
                    elif len(fits) > 1:
                        infer_note = (
                            "数值与多个倾向都吻合："
                            + "、".join(f"{n} {v:g}" for n, v in fits)
                            + "　请带变体名重发：紫卡分析 棱晶欧玛 [截图]"
                        )
                    elif not fits:
                        # ★ 2026-10-03 最近邻判据（取证 §六，修组合枪双模式报障）：
                        #   严格区间（lo ≤ v ≤ hi）是硬边界，组合枪「wiki 与实机
                        #   差 1%~5%」的腔体级残差会把只差 1% 的真候选判死（实例：
                        #   捕月主要 1.1 多重/切割各差 0.7%/1.1% ⇒ 误报「老卡」）。
                        #   先看最近邻：score = max|v/(基值×D×系数) − 1|，
                        #   ≤ 0.15 判该候选并**把残差印在卡面**（可审计）。
                        scored = RA.candidate_scores(
                            stats_pos, stats_neg, cls, [(mother_name, wm_disp, cls)] + _fam_c
                        )
                        near = [(n, d, s) for n, d, s in scored if s <= RA.NEAR_MISS_MAX]

                        def _fmt_pct(x: float) -> str:
                            """残差百分数：整数省小数（11.0%→11%），否则一位小数。"""
                            return f"{x * 100:.1f}".rstrip("0").rstrip(".") or "0"

                        _res_txt = ""
                        if scored:
                            _res_txt = "；候选残差：" + "、".join(
                                f"{n} {_fmt_pct(s)}%" for n, _d, s in scored[:4]
                            )

                        if near:
                            _bn, _bd, _bs = near[0]
                            _tied = [
                                n
                                for n, d, s in near
                                if abs(d - _bd) <= 1e-9 and abs(s - _bs) <= 1e-9
                            ]
                            name, disp = _bn, _bd
                            _tie_txt = ""
                            if len(_tied) > 1:
                                _tie_txt = "；同倾向候选：" + "、".join(
                                    n for n in _tied if n != _bn
                                )
                            if _bn == mother_name and abs(_bd - wm_disp) < 1e-9:
                                # 最近邻就是母武器自身（严格区间差一点）：
                                # 按「本体 + 残差」表述，别写「对不上」。
                                infer_note = (
                                    f"卡面数值与「{mother_name}」倾向 {wm_disp:g} "
                                    f"略有偏差（最近邻残差 {_fmt_pct(_bs)}%，"
                                    "未超阈值）—— 已按该倾向计算"
                                )
                            else:
                                infer_note = (
                                    f"数值最近邻倾向 {_bd:g}：{_bn}"
                                    f"（母武器 {mother_name} {wm_disp:g} 对不上）"
                                    f"⚠ 残差 {_fmt_pct(_bs)}%"
                                    "（接近该倾向，非严格命中）" + _tie_txt
                                )
                        else:
                            # ★ 2026-09-24 用户报障：老卡（洗出后该武器倾向被上调过，
                            #   游戏不回溯重算旧卡数值）会四条词条整体偏低、全落 0%，
                            #   旧实现只丢一句「武器名可能识别有误」（误导）。数值本身就
                            #   能反推倾向：区间够紧（≤25%）时直接按反推值算区间。
                            #   （2026-10-03 起排在最近邻判据之后：残差 >15% 才到这。）
                            iv = RA.disposition_interval(stats_pos, stats_neg, cls)
                            if iv[0] and iv[1] / iv[0] <= 1.25:
                                # ★ 2026-10-03（用户口径）：先看反推区间是否落在**家族
                                #   候选的已知倾向**里（本地 wiki 表，含玄骸变体 —— 家族
                                #   关系已改走官方 parentName 表，赤毒/信条/终幕列得出了）。
                                #   命中 ⇒ 判该变体，别一律推给「老卡」。
                                #   ⚠ 兜底性质：`disposition_interval` 与 `_fit_dev` 判据
                                #   数学等价（前者即后者的区间形式）⇒ 家族修好后「区间命中
                                #   但 fit 为空」本不可达（上方「唯一吻合」分支会先接住）；
                                #   此处保留，防两条判据将来分叉时退化成「老卡」误报。
                                _hit = [
                                    (n, float(v)) for n, v in fam_all if iv[0] <= float(v) <= iv[1]
                                ]
                                if _hit:
                                    _mid = (iv[0] + iv[1]) / 2
                                    _vals = sorted({round(v, 4) for _n, v in _hit})
                                    disp = min(_vals, key=lambda v: abs(v - _mid))
                                    _names = (
                                        "、".join(n for n, v in _hit if round(v, 4) == disp)
                                        or _hit[0][0]
                                    )
                                    infer_note = (
                                        f"数值反推倾向 {disp:g}：命中家族变体"
                                        f"「{_names}」（{mother_name} 当前值 {wm_disp:g} "
                                        "对不上）—— 疑似变体卡，请带前缀重发核对："
                                        f"紫卡分析 {_hit[0][0]} [截图]；"
                                        f"区间已按 {disp:g} 计算"
                                    )
                                else:
                                    disp = round((iv[0] + iv[1]) / 2, 2)
                                    infer_note = (
                                        f"卡面数值反推倾向 ≈{disp:g}（{mother_name} 当前值 "
                                        f"{wm_disp:g} 对不上，反推区间 {iv[0]:g}~{iv[1]:g}）"
                                        "—— 疑似倾向调整前洗出的老卡，区间已按反推值计算" + _res_txt
                                    )
                            else:
                                # ★ 2026-10-02：本体不吻合、家族变体也解释不了 ⇒ 明确
                                #   提示「疑似变体卡」并给候选（线上实证：赤毒努寇微波枪
                                #   被读成努寇微波枪，倾向 0.50 错按 1.45 算 —— 差 2.9 倍；
                                #   只写「武器名可能识别有误」不够，要用户带前缀重发）。
                                _cands = [n for n, _v in fam_all][:3]
                                if not _cands:
                                    try:  # 家族列不出时给名字候选（现成 suggest）
                                        _cands = list(
                                            await self.client.suggest_riven_weapons(
                                                weapon_name.strip()
                                            )
                                            or []
                                        )
                                    except Exception:  # noqa: BLE001 - 候选失败不阻断
                                        _cands = []
                                _cand_txt = ("；候选：" + "、".join(_cands)) if _cands else ""
                                # ★ 2026-10-03：示例不能硬编码「赤毒」——
                                #   ① mother_name 可能已含变体前缀（用户手输「赤毒·鳄神」⇒
                                #      旧文案会拼成「紫卡分析 赤毒赤毒·鳄神」）；
                                #   ② 本分支覆盖赤毒/信条/终幕三类 + 棱晶/圣洁/保障…等，
                                #      硬编码「赤毒」对非赤毒武器是错示例。
                                #   ★ 前缀判定**复用既有词表** matching.VARIANT_TOKENS
                                #     （dict 键即中文前缀），不在本文件新建第三份词表
                                #     （本仓已有词表漂移史，2026-09-27/10-02 各修过一次）。
                                #   优先级：已有候选 ⇒ 用候选（最准，往往就是真变体名）；
                                #   否则已含前缀 ⇒ 原样；否则保留旧「赤毒{本体名}」默认。
                                _example = (
                                    _cands[0]
                                    if _cands
                                    else mother_name
                                    if any(p in mother_name for p in matching.VARIANT_TOKENS)
                                    else f"赤毒{mother_name}"
                                )
                                infer_note = (
                                    f"⚠️ 卡面数值与「{mother_name}」本体倾向 {wm_disp:g} "
                                    "不吻合，疑似变体卡（赤毒 / 信条 / 终幕 等）—— 请带变体前缀重发"
                                    f"（例：紫卡分析 {_example} [截图]）{_cand_txt}{_res_txt}"
                                )
            elif not RA.disp_feasible(stats_pos, stats_neg, cls, disp):
                iv = RA.disposition_interval(stats_pos, stats_neg, cls)
                rng = f"（反推应在 {iv[0]:g}~{iv[1]:g}）" if iv[0] else ""
                infer_note = f"⚠️ 卡面数值与倾向 {disp:g} 不吻合{rng}，请核对武器"
        if disp_override:
            disp_note = (
                f"（已按手输倾向 {disp:g} 计算，母武器 WM 值 {wm_disp:g}）"
                if wm_disp
                else f"（已按手输倾向 {disp:g} 计算）"
            )
        elif variant_disp:
            disp_note = f"（倾向取自 wiki 变体表：{variant_key} {variant_disp:g}）"
        else:
            disp_note = ""
        # 家族提示：列出家族变体倾向供对照（手输倾向仍可用 disp_override）；
        # 倾向本身已由卡面数值反推/母武器可行确认，不再提示「带名字重发」
        family_note = ""
        if (
            not infer_note
            and not disp_override
            and not variant_disp
            and weapon_name.strip() == mother_name
        ):
            fam = [(n, v) for n, v in fam_all if abs(v - disp) > 1e-9]
            if fam:
                # ★ 2026-10-05：删掉「装在棱晶等变体上请发『紫卡分析
                #   棱晶欧玛 [截图]』」的重发提示 —— 倾向已由卡面数值反推
                #   （母武器可行才走到这里，数值落位本身就是变体判别），
                #   再让用户带名字重发没有意义；保留家族列表与
                #   「卡面不显示变体/模式」的说明即可。
                _tail = (
                    "　卡面不显示变体/模式"
                    if any("（主要）" in n or "（次要）" in n for n, _v in fam)
                    else "　卡面不显示变体"
                )
                family_note = (
                    "该武器家族有其它倾向：" + "、".join(f"{n} {v:g}" for n, v in fam[:4]) + _tail
                )
        title, lines = fmt.fmt_riven_analysis(name, disp, cls, stats_pos, stats_neg)
        if family_note:
            lines.insert(1, f"※ {family_note}")
        if disp_note:
            lines.insert(1, f"※ {disp_note}")
        if infer_note:
            lines.insert(1, f"※ {infer_note}")
        if neg_fix_note:
            lines.insert(1, f"※ {neg_fix_note}")
        if inverted_note:
            lines.insert(1, f"※ {inverted_note}")
        if decimal_fix:
            lines.insert(1, "※ 已修正小数点（截图未读出点号）：" + "、".join(decimal_fix))
        if source_note:
            lines.insert(1, f"※ 来源：{source_note.strip('（）')}")
        if nearmiss_note:
            lines.insert(1, f"※ {nearmiss_note}")
        if dup_note:
            lines.insert(1, f"※ {dup_note}")
        logger.info(
            "[sdjk] 紫卡分析耗时 %.0f ms（含识别/查询/计算）",
            (_tt.perf_counter() - _t_start) * 1000,
        )
        return Reply(
            title,
            lines,
            footer=fmt.fmt_platform_footer(platform, "DE 属性基值公式 · 倾向可由卡面数值反推"),
        )
