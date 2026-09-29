# -*- coding: utf-8 -*-
"""资料与计算器域（D8 自 main.py 迁入，方法体逐字未改）。

覆盖路由键 3 项：wiki / valence / damage；含知识库引导与 wiki 简介回退。
子包纪律：不 import astrbot（事件对象鸭子类型）。
"""
from __future__ import annotations

from .. import calculators as calc
from .. import damage_calc as dc
from .. import formatters as fmt
from .. import matching
from .. import search as search_engine
from .. import wiki_intro
from ..api_client import WarframeAPIError
from ..logging_compat import logger
from .base import Reply


class WikiMiscCommands:
    """Mixin：wiki / 效价加成 / 伤害计算器 handler（挂载于 main.WarframeSDJK）。"""

    # ------------------------------------------------------------------
    # wiki（资料）
    # ------------------------------------------------------------------
    def _kb_hint(self) -> str:
        """知识库（AstrBot 平台侧 RAG）引导 —— 只在本地查不到时补一句。

        插件**不直连**知识库：检索由 AstrBot 平台提供，插件只负责告诉用户
        「有这份文档、放哪」。开源版用户没配知识库时可以关掉（面板
        「知识库引导」= 否），免得每次都多一句。
        """
        if not self.cfg.get("kb_enabled", True):
            return ""
        docs_dir = str(self.cfg.get("kb_docs_dir") or "kb").strip() or "kb"
        kb_id = str(self.cfg.get("kb_id") or "").strip()
        tail = f"（知识库 id：{kb_id}）" if kb_id else ""
        return (f"\n\n💡 推荐 / 攻略类问题可把 {docs_dir}/ 下的文档"
                f"传进 AstrBot 知识库{tail}")

    def _wiki_intro_on(self) -> bool:
        """wiki 卡片开关（默认开）。

        卡片是**分层出图**的：遗物卡 / 部件反查卡 / 最小兜底卡只依赖
        `relic_index.json`、`relic_inverse.json`；「简介卡」正文需要
        `core/data/wiki_intro.json`（v1.0.7 起**随包分发**，市场版也有），
        MOD 效果行的中文译文需要 `wiki_effect_zh.json`（同随包）。
        若这两个文件被删/缺失：简介类条目退「最小卡」（一行说明 + 可点链接），
        效果行回落英文原文 —— 都是防御性兜底，不是常态。
        """
        return bool(self.cfg.get("wiki_intro", True))

    def _wiki_reply(self, page_name: str, url: str, alts: list[str],
                    platform, *names: str, variants: list[str] | None = None,
                    note: str = "") -> Reply:
        """wiki 结果统一出口：有简介数据 → 卡片 + 链接；没有 → 纯文本链接。

        链接必须**可点击**（issue #1 需求②），所以卡片之外永远另发一段
        Plain 文本；只有链接时保持纯文本直发（渲成图片会把 URL 烧死在图里）。
        查询没指明变体时 ``variants`` 给出「变体：…」一行（用户口径：
        没指明就只介绍基础的，变体在下面说明）。
        ``note`` 是易混淆澄清（战甲黑话清单，见 ``wiki_clarify``），
        有就缀在卡片/文本末尾一行。
        """
        var_line = f"变体：{'、'.join(variants)}" if variants else ""
        note_line = f"⚠️ {note}" if note else ""
        link = f"🔗 wiki 链接：{url}"
        if alts:
            link += f"\n同类候选：{'、'.join(alts)}"
        if self._wiki_intro_on():
            card = wiki_intro.card_for(page_name, *names)
            if card:
                title, lines = card
                if var_line:
                    lines = [*lines, var_line]
                if note_line:
                    lines = [*lines, note_line]
                return Reply(title, lines, extra_text=link,
                             footer=fmt.fmt_platform_footer(platform))
        lines = [f"📖 {page_name}", url]
        if var_line:
            lines.append(var_line)
        if note_line:
            lines.append(note_line)
        if alts:
            lines.append(f"同类候选：{'、'.join(alts)}")
        return Reply("wiki 直达", lines, text_only=True,
                     footer=fmt.fmt_platform_footer(platform))

    async def _h_wiki(self, parsed, event, platform) -> Reply:
        """维基页面直达。

        先走本地概念页/战甲外号页（wiki 段），再走统一索引拼页面名；只有都落空
        才给搜索链接。页面名按**国际服**口径（DE 官方简中不翻译战甲名，见
        ``search_engine.wiki_page_name``）；查询没指明变体（p / prime / 亡魂…）
        时介绍基体并列一行变体，指明了就直接给该变体。配了简介数据时附卡片。
        易混淆黑话（花甲=Wisp 不是 Garuda 这类）在卡片尾给一行澄清（``note``）。
        """
        query = parsed.content_str
        if not query:
            return Reply(raw_text="用法：wiki 关键词（本地词库优先，未命中给搜索链接）")
        note = self.client.wiki_clarify(query)
        hit = self.client.wiki_lookup(query)
        if hit:
            # 概念页 / 战甲外号页（wiki 段）：同样出卡片（用户口径：都要绘制），
            # 卡片正文若知识库有该页条目就用，没有退最小卡
            return self._wiki_reply(hit["title"], hit["url"], [], platform,
                                    hit["title"], query, note=note)
        from urllib.parse import quote as _q

        want_variant = matching.variant_intent_any(query)
        found = search_engine.search(query, limit=3)
        if not found and want_variant:
            # 「摸尸p」这类带 p 后缀的查询词库里没有（别名表只登记无后缀形态）
            # → 剥掉 p/prime 再查，命中后按变体解析（want_variant 已为 True）
            stripped = matching.strip_variant_query(query)
            if stripped and stripped != query:
                found = search_engine.search(stripped, limit=3)
        if found:
            # 黑话命中（别名）时页面名取国际服官方名——别名键/国服旧译都不是
            # wiki 页面（「wiki 音妈」「wiki 摸尸」实测死链，2026-09-24）。
            best_name = search_engine.wiki_page_name(found[0],
                                                     base=not want_variant)
            # 灰机标题归一：含中文的名字要去掉空格与中点（「玻之武杖 Prime」→
            # 「玻之武杖Prime」），否则页面不存在（2026-09-25 浏览器 API 实测）
            page = _q(search_engine.wiki_title(best_name).replace(" ", "_"))
            alts = [f["name"] for f in found[1:] if f["name"] != best_name]
            return self._wiki_reply(
                best_name, f"https://warframe.huijiwiki.com/wiki/{page}",
                alts, platform, found[0].get("name") or "",
                found[0].get("en") or "",
                query,          # 前缀命中（「电路」→「电路效果」）时按原词找卡片
                variants=None if want_variant
                else wiki_intro.variants(best_name),
                note=note)
        # WM 中文名 → 灰机 wiki 对应页面（用官方名构造 URL）
        try:
            item = await self.client.resolve_wm_item(query)
        except WarframeAPIError:
            item = None
        if item and (item.get("zh") or item.get("en")):
            # WM 的展示名带「一套/蓝图」这类后缀；有 slug 就走 slug（国际服名）
            slug = str(item.get("url_name") or "").strip()
            if slug:
                page_name = search_engine.page_name_from_slug(
                    search_engine.base_slug(slug) if not want_variant else slug)
            else:
                page_name = search_engine.wiki_page_name(
                    {"name": item.get("zh") or "", "en": item.get("en") or ""},
                    base=not want_variant)
            page = _q(search_engine.wiki_title(page_name).replace(" ", "_"))
            return self._wiki_reply(
                page_name, f"https://warframe.huijiwiki.com/wiki/{page}",
                [], platform, item.get("zh") or "", item.get("en") or "",
                variants=None if want_variant
                else wiki_intro.variants(page_name),
                note=note)
        link = await self.client.wiki_search_link(query)
        # 未收录时也把澄清带上（如「跑男」这种自带歧义的写法）
        tip = f"\n\n⚠️ {note}" if note else ""
        return Reply(raw_text=f"本地词库未收录「{query}」，请前往维基搜索：\n{link}"
                              f"{tip}{self._kb_hint()}")

    async def _h_valence(self, parsed, event, platform) -> Reply:
        """玄骸 / 信条 / 科达武器的**效价融合**（Valence Fusion）。

        官方规则：两者必须是**同一种**武器；结果 = 较高值 × 1.1（≥58% 直接给 60%）；
        元素由玩家在两者间二选一，**不会**合成复合元素。
        """
        import re as _re
        toks = parsed.content or []
        pairs: list[tuple[str, float]] = []
        for tok in toks:
            m = _re.fullmatch(r"([^\d]*)(\d+(?:\.\d+)?)%?", tok)
            if m and m.group(2):
                pairs.append((m.group(1).strip(), float(m.group(2))))

        if len(pairs) >= 2:
            (e1, p1), (e2, p2) = pairs[0], pairs[1]
            has_elem = bool(e1 or e2)
            res = calc.valence_fusion(e1 or "火", p1, e2 or "火", p2)
            if "error" in res:
                return Reply(raw_text=res["error"])
            # 没给元素时不硬编一个「火」，也不输出问号
            if has_elem:
                labels = [f"{e or '—'}{p:g}%" for e, p in pairs[:2]]
            else:
                labels = [f"{p:g}%" for _, p in pairs[:2]]
            lines = [
                f"◆ 融合材料：{' ＋ '.join(labels)}",
                f"◆ 融合结果：加成 {res['percent']:g}%",
                f"　公式 结果 = 较高值 {max(p1, p2):g}% × 1.1"
                + (" → ≥58% 直接进位 60%" if res["percent"] >= 60 else ""),
            ]
            # 没给元素时别硬编一个「火」，也别输出「?」
            if has_elem:
                lines.insert(2, f"◆ 元素：{' 或 '.join(res['options'])}"
                                f"（{res['note']}）")
            if res["percent"] < calc.VALENCE_CAP:
                steps = calc.fusion_to_cap(res["percent"])
                lines.append(f"◆ 从 {res['percent']:g}% 到满值还需 {len(steps)} 次融合"
                             f"（用同数值材料）")
            lines.append("※ 必须是同款武器（同一把 Kuva/Tenet/Coda，同系列不同名不行）")
            lines.append("※ 只有受体会被保留：材料的催化剂 / Forma / 架式 Forma / 透镜"
                         "都不转移，务必用投资多的那把当受体")
            lines.append("※ 满值判定：≥58% 即进位到 60%，所以材料 ≥52.8% 可一步满值")
            return Reply("效价融合", lines)

        if len(pairs) == 1:
            _e1, p1 = pairs[0]
            trace = calc.fusion_to_cap(p1)
            lines = [f"◆ 从 {p1:g}% 用同数值材料融合到 60% 的轨迹：",
                     "　→ ".join(f"{x:g}%" for x in trace) or "已是满值",
                     f"◆ 共需 {len(trace)} 次融合（材料数值越高可少几次）"]
            lines.append("※ 材料 ≥58% 时受体无需再看自身数值，融合后直接 60%")
            return Reply("效价融合规划", lines)

        return Reply(raw_text="用法：\n"
                              "· 武器融合 电60 火58　两把融合的结果与可选元素\n"
                              "· 武器融合 44　　　　　从 44% 到 60% 需要融合几次\n"
                              "支持元素：电 / 火 / 冰 / 毒 / 冲击 / 磁力 / 辐射")

    async def _h_damage(self, parsed, event, platform) -> Reply:
        """伤害计算器 v1：武器 + 配卡加成 → 对指定派系/等级敌人的期望伤害。

        用法：伤害 <武器名> [G系/C系/I系/炽蛇军/科腐者…] [N级] [爆头]
                  [基伤N] [多重N] [暴率N] [暴伤N] [派系N] [电/火/冰/毒N] [基甲N]
        公式与机制基准见 core/damage_calc.py 模块注释（U36 抗性重构后）。
        """
        spec, name_tokens = dc.parse_args(parsed.content)
        query = " ".join(name_tokens).strip()
        if not query:
            return Reply("伤害计算", [
                "◆ 用法",
                "　伤害 <武器名> [对 敌人名] [派系] [N级] [爆头] [加成项…]",
                "　例：伤害 布拉玛 对 重机枪手 100级 膛线 分裂膛室 地狱火 电90",
                "　例：伤害 空刃 对 重机枪手 100级 钢铁凤凰 异况超量 急进猛突 连击120",
                "◆ 配卡（三种写法可混用）",
                "　· MOD 名（满级值，可整条配卡直接粘贴）",
                "　　膛线 / 分裂膛室 / 镀层分裂膛室 / 地狱火 / 关键延迟 / 瞄准目标 …",
                "　· 手写：基伤 / 多重 / 暴率 / 暴伤 / 爆头倍率 / 派系（%）",
                "　　电 火 冰 毒（自动合成复合）｜冲击 穿刺 切割（只加同类型）",
                "　　状态伤害N（只放大 DoT）｜基甲N（覆盖默认敌人基准甲）",
                "　· 敌人名：枪兵 / 重机枪手 / 轰击者 / 屠夫 / 船员 …（支持模糊）",
                "　　给了就用它的真实基准甲/血/盾，并算击杀发数",
                "◆ 异常（U36 后元素差异的主要来源）",
                "　病毒N / 磁力N（1-10 层，对血 / 对盾加伤）",
                "　腐蚀N（剥甲 26%+6%×每层，满层 −80%）｜火剥甲（−50%）",
                "◆ 近战",
                "　连击N 重击｜架势名（钢铁凤凰 / 猎鹰俯击 …：每段倍率+强制异常）",
                "　急进猛突 / 创口溃烂 / 异况超量 / 一击必杀 / 奋力一掷 按实际等级折算",
                "◆ 其它",
                "　派系：G系 / C系 / I系 / 合一众 / 奥罗金 / 低语者 / 扎里曼 / 炽蛇军 / 科腐者 …",
                "　赋能 <名>（条件触发只展示不折算）｜异常N（目标异常种类数）",
                "　镀层N / 满镀层（镀层类击杀堆叠按 N 层计入，如镀层 分裂膛室）",
                "　超宏[N]（不吃护甲/派系、免疫异常）｜适应N（Sentient 适应 1-4 层）",
                "　灵化 / 基础形态（有灵化数据的武器默认开灵化形态）",
                "　空战 / 地面（Archgun 双部署：默认地面・大气；空战用空战面板）",
                "　进化 基伤/暴击/爆头…（灵化进化选项，可多次；改基础面板）",
                "　赤毒辐射60（赤毒/信条/终幕回响加成 25-60%，元素要写）",
                "◆ 输出",
                "　单发对血/对盾（无暴击）→ 暴击与爆头期望（含暴击）",
                "　→ 每次扳机、DPS 爆发/持续（均给含暴击与无暴击）",
            ])

        try:
            weapon, alts = dc.find_weapon(query)
            if weapon is None:
                miss = "、".join(spec.get("unknown") or []) or "—"
                return Reply("伤害计算", [
                    f"武器库（warframe-items）里没找到「{query}」。",
                    f"　本串里没认出来的词：{miss}",
                    "　换英文名或完整中文名再试，例如「Kuva Bramma / 赤毒布拉玛」。",
                ])
            res = dc.calculate(spec, weapon)
            return Reply("伤害计算", dc.card_lines(weapon, spec, res, alts))
        except Exception as exc:  # noqa: BLE001 - 绝不静默：群聊里没输出最难查
            logger.warning(f"[sdjk] 伤害计算失败：{exc!r}")
            return Reply("伤害计算", [
                f"❗ 计算出错：{type(exc).__name__}: {exc}",
                f"　武器：{query}｜已识别 MOD："
                + ("、".join(m.get("zh") or m.get("name") or ""
                             for m in spec.get("mods") or []) or "无"),
            ])

    # ------------------------------------------------------------------
