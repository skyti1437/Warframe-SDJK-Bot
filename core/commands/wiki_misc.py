# -*- coding: utf-8 -*-
"""资料与计算器域（D8 自 main.py 迁入，方法体逐字未改）。

覆盖路由键 3 项：wiki / valence / damage；含 wiki 简介回退。
子包纪律：不 import astrbot（事件对象鸭子类型）。
"""

from __future__ import annotations

import asyncio
import functools
import json
import re
from pathlib import Path
from urllib.parse import quote as _quote

from .. import calculators as calc
from .. import damage_calc as dc
from .. import formatters as fmt
from .. import matching
from .. import search as search_engine
from .. import wiki_intro
from ..api_client import WarframeAPIError
from ..i18n import L, is_en
from ..logging_compat import logger
from .base import Action, Reply, err_card

# ---------------------------------------------------------------------------
# wiki 链接来源（2026-11-10 维护者定：灰机 wiki 权威性太低，默认改英文 wiki，可选灰机）：
#   · 配置 ``wiki_link_source``：``en``（默认）/ ``huiji``；单次查询末尾加「灰机」临时用灰机；
#   · 英文显示模式始终用英文 wiki（页面名也显示英文）；
#   · 中文显示 + 英文 wiki：卡片内容不变，只换链接；查不到英文页面名时退回灰机链接
#     （拿中文去英文 wiki 站内搜索搜不到东西）。
# 英文页面名优先用 DE 官方语言表（内部路径把 name_zh / name_en 连起来）——检索索引里的
# ``en`` 字段很多是内部类型名（川流不息 →「avatar power max mod」），不能当页面名。
# ---------------------------------------------------------------------------
_EN_WIKI = "https://wiki.warframe.com/w/"
_CJK_RE = re.compile(r"[\u3400-\u9fff]")
_HUIJI_WORDS = {"灰机", "灰机wiki", "huiji"}
_ZH2EN: dict | None = None


_LANG2EN: dict | None = None

# 语言表里对不上的常用概念 → 英文 wiki 页面名（人工核对；2026-11-10 用 wiki API 确认页面存在）。
# 更多缺口发现后往这里补（或补进 aliases.json ``wiki`` 段的 ``en`` 字段）。
_CONCEPT_EN = {
    "扎里曼": "Zariman Ten Zero",
    "虚空裂隙": "Void Fissure",
    "九重天": "Void Fissure",
    "钢铁之路": "The Steel Path",
    "氏族道场": "Clan Dojo",
    "集团": "Syndicate",
}


def _official_en(name: str) -> str:
    """官方简中名 → 官方英文名；查不到返回空串。

    ① 物品名表（name_zh / name_en，同一内部路径；一名多译不猜）；
    ② DE 完整语言表（languages_zh / languages_en，同一语言键；地名 / 玩法 / 系统名都在这里，
       一名多译取最短的英文——「午夜电波」→ Nightwave 而非 Nightwave Livery）。
    """
    global _ZH2EN, _LANG2EN
    from .. import de_worldstate

    if _ZH2EN is None:
        zh = de_worldstate._load_file("name_zh.json") or {}
        en = de_worldstate._load_file("name_en.json") or {}
        acc: dict[str, set] = {}
        for path, z in zh.items():
            e = en.get(path)
            if isinstance(z, str) and isinstance(e, str) and z and e:
                acc.setdefault(matching.normalize_name(z), set()).add(e)
        _ZH2EN = {k: next(iter(v)) for k, v in acc.items() if len(v) == 1}
    key = matching.normalize_name(name or "")
    if not key:
        return ""
    hit = _CONCEPT_EN.get(key) or _ZH2EN.get(key)
    if hit:
        return hit.title() if hit.isupper() else hit  # 界面大写串（EXTERMINATE）→ 页面名写法
    if _LANG2EN is None:
        zh = de_worldstate._load_file("languages_zh.json") or {}
        en = de_worldstate._load_file("languages_en.json") or {}
        acc2: dict[str, set] = {}
        for k, z in zh.items():
            e = en.get(k)
            # 只收短名（描述 / 对白类长句不当页面名）
            if (
                isinstance(z, str)
                and isinstance(e, str)
                and z
                and e
                and len(z) <= 20
                and len(e) <= 60
            ):
                acc2.setdefault(matching.normalize_name(z), set()).add(e.strip())
        # 一名多译：优先非全大写（界面大写串「SURVIVAL」不是页面名），再取最短
        _LANG2EN = {k: min(v, key=lambda x: (x.isupper(), len(x), x)) for k, v in acc2.items()}
    hit = _LANG2EN.get(key, "")
    return hit.title() if hit.isupper() else hit


_EN_OFFICIAL: dict | None = None


def _official_en_ascii(name: str) -> str:
    """英文查询 → 官方英文名的正确大小写（「frost」→ Frost）；不是官方名返回空串。"""
    global _EN_OFFICIAL
    if _EN_OFFICIAL is None:
        from .. import de_worldstate

        acc: dict[str, set] = {}
        for tbl in ("name_en.json", "languages_en.json"):
            for e in (de_worldstate._load_file(tbl) or {}).values():
                if isinstance(e, str) and 0 < len(e) <= 60 and not e.isupper():
                    acc.setdefault(e.strip().casefold(), set()).add(e.strip())
        _EN_OFFICIAL = {k: min(v) for k, v in acc.items()}
    return _EN_OFFICIAL.get(" ".join((name or "").split()).casefold(), "")


def _en_wiki_title(*cands: str, base: bool = False) -> str:
    """候选名 → 英文 wiki 页面标题；没有返回空串。

    先按 DE 官方语言表把候选名（中文 / 英文官方名）换成官方英文名；都换不到再取第一个不含汉字的
    候选（slug 式全小写名转标题）。``base`` = 查询没指明变体：slug 式的全小写名剥掉变体词，落到基体。
    """
    for c in cands:
        c = str(c or "")
        off = _official_en(c) if _CJK_RE.search(c) else _official_en_ascii(c)
        if off:
            return off
    for c in cands:
        c = " ".join(str(c or "").replace("_", " ").split())
        if not c or _CJK_RE.search(c):
            continue
        c = re.sub(r"(?i)\s+(blueprint|set)$", "", c)
        if c == c.lower():  # 全小写 = 转写 / slug 形态
            slug = c.replace(" ", "_")
            if base:
                slug = search_engine.base_slug(slug)
            c = search_engine._title_en(slug.replace("_", " "))
        return c
    return ""


def _en_wiki_url(title: str, query: str) -> str:
    """英文 wiki 链接：有页面名直达，否则站内搜索。"""
    if title:
        return _EN_WIKI + _quote(title.replace(" ", "_"), safe="/'(),")
    return _EN_WIKI + "Special:Search?go=Go&search=" + _quote(query)


def _wiki_localize(
    page_name: str,
    url: str,
    query: str,
    *cands: str,
    base: bool = False,
    source: str = "en",
    en: str = "",
):
    """按显示语言与链接来源给出 ``(展示页面名, 链接)``。

    ``en``：人工核对过的英文页面名（wiki 概念表 aliases.json ``wiki`` 段的 ``en`` 字段），优先用。
    英文模式：英文页面名 + 英文 wiki；中文模式 + ``source="en"``：页面名不变、链接换英文 wiki；
    ``source="huiji"``：原样返回灰机链接。
    ★ 2026-11-10 维护者：英文 wiki 基本页面齐全，**不自动切灰机**——查不到英文页面名时给英文 wiki
      站内搜索（有英文候选名就用英文名搜），缺口记下来补数据。
    """
    # 查询词本身就是官方名（「水星」「frost」）时以它为准：检索的前缀模糊 / 内部名会把它带偏到
    # 「水星棕」（库狛颜色）「低温眠者」（皮肤内部名 frost）上（2026-11-10 核对英文链接时发现）
    if is_en():
        title = en or _en_wiki_title(query, *cands, base=base)
        return (title or page_name), _en_wiki_url(title, _en_search_term(query, *cands))
    if source == "en":
        title = en or _en_wiki_title(query, page_name, *cands, base=base)
        if not title:
            logger.info("[sdjk] wiki：「%s」没有对上英文页面名，给英文 wiki 站内搜索", query)
        return page_name, _en_wiki_url(title, _en_search_term(query, *cands))
    return page_name, url


def _en_search_term(query: str, *cands: str) -> str:
    """英文 wiki 站内搜索词：优先第一个不含汉字的候选名，没有才用原查询。"""
    for c in cands:
        c = " ".join(str(c or "").split())
        if c and not _CJK_RE.search(c):
            return c
    return query


# 英文伤害参数写法 → 中文写法（只在「伤害」的参数解析处认，不进通用别名表）
_DMG_EN_WORDS = {
    "vs": "对",
    "versus": "对",
    "headshot": "爆头",
    "steelpath": "钢路",
    "strip": "火剥甲",
    "incarnon": "灵化",
    "charged": "蓄力",
    "uncharged": "未蓄力",
    "semi-auto": "半自动",
    "semiauto": "半自动",
    "auto": "全自动",
    "aerial": "空战",
    "ground": "地面",
}
_DMG_EN_PHRASES = {
    ("steel", "path"): "钢路",
    ("base", "form"): "基础形态",
    ("alt", "fire"): "副开火",
    ("semi", "auto"): "半自动",
    ("heavy", "attack"): "重击",
}
_DMG_EN_NUMBERED = {
    "viral": "病毒",
    "magnetic": "磁力",
    "corrosive": "腐蚀",
    "electricity": "电",
    "heat": "火",
    "cold": "冰",
    "toxin": "毒",
    "impact": "冲击",
    "puncture": "穿刺",
    "slash": "切割",
    "combo": "连击",
    "fire": "开火",
    "status": "异常",
    "basedmg": "基伤",
    "multishot": "多重",
    "critchance": "暴率",
    "critdmg": "暴伤",
    "hsmult": "爆头倍率",
    "faction": "派系",
    "basearmor": "基甲",
    "statusdmg": "状态伤害",
    "statuschance": "触发",
}
_DMG_EN_EVO = {
    "damage": "基伤",
    "crit": "暴击",
    "critdmg": "暴伤",
    "status": "触发",
    "multishot": "多重",
    "headshot": "爆头",
    "firerate": "射速",
    "reload": "装填",
    "combo": "连击",
    "punchthrough": "穿透",
    "magazine": "弹匣",
}
# 效价融合的英文元素写法 → 中文内部键（只在本命令的参数解析处认）
_VALENCE_EN = {
    "electricity": "电",
    "electric": "电",
    "heat": "火",
    "fire": "火",
    "cold": "冰",
    "ice": "冰",
    "toxin": "毒",
    "poison": "毒",
    "impact": "冲击",
    "magnetic": "磁力",
    "radiation": "辐射",
}
_VALENCE_NAME_EN = {
    "electricity": "Electricity",
    "heat": "Heat",
    "cold": "Cold",
    "toxin": "Toxin",
    "impact": "Impact",
    "magnetic": "Magnetic",
    "radiation": "Radiation",
}


def _valence_en(elem_zh: str) -> str:
    """中文元素键 → 英文元素名（认不出原样返回）。"""
    key = calc.VALENCE_ELEMENTS.get(elem_zh, "")
    return _VALENCE_NAME_EN.get(key, elem_zh)


_DMG_EN_LICH = {"kuva": "赤毒", "tenet": "信条", "coda": "终幕"}
_DMG_EN_LICH_EL = {
    "radiation": "辐射",
    "magnetic": "磁力",
    "viral": "病毒",
    "blast": "爆炸",
    "heat": "火",
    "cold": "冰",
    "electricity": "电",
    "toxin": "毒",
    "impact": "冲击",
    "puncture": "穿刺",
    "slash": "切割",
}


def _dmg_en_toks(toks: list) -> list:
    """英文伤害参数并入中文写法；中文词与不认识的词（武器 / MOD / 敌人名）原样保留。"""
    toks = [str(t) for t in toks]
    out: list = []
    i = 0
    while i < len(toks):
        t = toks[i]
        low = t.lower()
        nxt = toks[i + 1].lower() if i + 1 < len(toks) else ""
        pair = (low, nxt)
        m = re.fullmatch(
            r"(kuva|tenet|coda)-?(" + "|".join(_DMG_EN_LICH_EL) + r")(\d+(?:\.\d+)?)%?", low
        )
        m2 = re.fullmatch(r"(" + "|".join(_DMG_EN_NUMBERED) + r")(\d+(?:\.\d+)?)(%|s)?", low)
        if pair in _DMG_EN_PHRASES:
            out.append(_DMG_EN_PHRASES[pair])
            i += 2
            continue
        if low == "evolve" and nxt:
            out.append("进化")
            out.append(_DMG_EN_EVO.get(nxt.replace(" ", ""), toks[i + 1]))
            i += 2
            continue
        if m:
            out.append(_DMG_EN_LICH[m.group(1)] + _DMG_EN_LICH_EL[m.group(2)] + m.group(3))
        elif m2:
            out.append(_DMG_EN_NUMBERED[m2.group(1)] + m2.group(2))
        elif low in _DMG_EN_NUMBERED and re.fullmatch(r"\d+(?:\.\d+)?s?", nxt):
            out.append(_DMG_EN_NUMBERED[low] + nxt.rstrip("s"))
            i += 2
            continue
        elif low == "heavy" and not nxt:
            # 裸 heavy 只认句尾，保留 Heavy Gunner、Heavy Caliber 等完整名字。
            out.append("重击")
        elif low in _DMG_EN_WORDS:
            out.append(_DMG_EN_WORDS[low])
        elif re.fullmatch(r"\d{1,4}", low):
            out.append(f"{low}级")  # 单独的数字 = 敌人等级
        else:
            out.append(t)
        i += 1
    return out


# 玩法 / 任务类型词（wiki 指令里优先于前缀模糊命中，见 _h_wiki）。任务类型取官方简中任务类型表；
# 活动取机器人已有指令覆盖的玩法（和世界状态卡片同一套叫法）。
_PLAY_ACTIVITIES = (
    "仲裁",
    "虚空裂缝",
    "裂缝",
    "九重天",
    "钢铁之路",
    "执刑官猎杀",
    "执刑官",
    "突击",
    "午夜电波",
    "电波",
    "赏金",
    "入侵",
    "警报",
    "夜灵平野",
    "夜灵平原",
    "奥布山谷",
    "魔胎之境",
    "双衍王境",
    "虚空风暴",
    "沉沦之地",
    "无尽回廊",
    "扎里曼",
    "坚战",
)


# 常见玩法英文名 → 中文页面词（灰机 wiki 标题是中文；英文直接搜不一定能跳到页面）
_PLAY_EN = {
    "arbitration": "仲裁",
    "arbitrations": "仲裁",
    "void fissure": "虚空裂缝",
    "fissure": "虚空裂缝",
    "sortie": "突击",
    "archon hunt": "执刑官猎杀",
    "nightwave": "午夜电波",
    "steel path": "钢铁之路",
    "bounty": "赏金",
    "bounties": "赏金",
    "invasion": "入侵",
    "alert": "警报",
    "duviri": "双衍王境",
    "zariman": "扎里曼",
    "plains of eidolon": "夜灵平野",
    "orb vallis": "奥布山谷",
    "cambion drift": "魔胎之境",
    "void storm": "虚空风暴",
    "the circuit": "无尽回廊",
}


@functools.lru_cache(maxsize=1)
def _play_terms() -> frozenset:
    terms = set(_PLAY_ACTIVITIES)
    try:
        data = json.loads(
            (
                Path(__file__).resolve().parent.parent / "data" / "de" / "mission_types_zh.json"
            ).read_text(encoding="utf-8")
        )
        terms |= {str(v) for v in (data.values() if isinstance(data, dict) else []) if v}
    except (OSError, ValueError):
        pass
    return frozenset(matching.normalize_name(t) for t in terms if t)


def _play_term(query: str) -> str:
    """玩法 / 任务类型词 → 用于 wiki 的中文词；不是返回空串。"""
    q = query.strip()
    zh = _PLAY_EN.get(" ".join(q.lower().split()))
    if zh:
        return zh
    return q if matching.normalize_name(q) in _play_terms() else ""


class WikiMiscCommands:
    """Mixin：wiki / 效价加成 / 伤害计算器 handler（挂载于 main.WarframeSDJK）。"""

    # ------------------------------------------------------------------
    # wiki（资料）
    # ------------------------------------------------------------------
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

    def _wiki_reply(
        self,
        page_name: str,
        url: str,
        alts: list[str],
        platform,
        *names: str,
        variants: list[str] | None = None,
        note: str = "",
    ) -> Reply:
        """wiki 结果统一出口：有简介数据 → 卡片 + 链接；没有 → 纯文本链接。

        链接必须**可点击**（issue #1 需求②），所以卡片之外永远另发一段
        Plain 文本；只有链接时保持纯文本直发（渲成图片会把 URL 烧死在图里）。
        查询没指明变体时 ``variants`` 给出「变体：…」一行（用户口径：
        没指明就只介绍基础的，变体在下面说明）。
        ``note`` 是易混淆澄清（战甲黑话清单，见 ``wiki_clarify``），
        有就缀在卡片/文本末尾一行。
        """
        _sep = L("、", ", ")
        var_line = (
            L(f"变体：{'、'.join(variants)}", f"Variants: {', '.join(variants)}")
            if variants
            else ""
        )
        note_line = f"⚠️ {note}" if note else ""
        link = L(f"🔗 wiki 链接：{url}", f"🔗 Wiki link: {url}")
        if alts:
            link += L(f"\n同类候选：{'、'.join(alts)}", f"\nSimilar: {_sep.join(alts)}")
        if self._wiki_intro_on():
            card = wiki_intro.card_for(page_name, *names)
            if card:
                title, lines = card
                if var_line:
                    lines = [*lines, var_line]
                if note_line:
                    lines = [*lines, note_line]
                return Reply(
                    title, lines, extra_text=link, footer=fmt.fmt_platform_footer(platform)
                )
        lines = [f"📖 {page_name}", url]
        if var_line:
            lines.append(var_line)
        if note_line:
            lines.append(note_line)
        if alts:
            lines.append(L(f"同类候选：{'、'.join(alts)}", f"Similar: {_sep.join(alts)}"))
        return Reply(
            L("wiki 直达", "Wiki Link"),
            lines,
            text_only=True,
            footer=fmt.fmt_platform_footer(platform),
        )

    async def _h_wiki(self, parsed, event, platform) -> Reply:
        """维基页面直达。

        先走本地概念页/战甲外号页（wiki 段），再走统一索引拼页面名；只有都落空
        才给搜索链接。页面名按**国际服**口径（DE 官方简中不翻译战甲名，见
        ``search_engine.wiki_page_name``）；查询没指明变体（p / prime / 亡魂…）
        时介绍基体并列一行变体，指明了就直接给该变体。配了简介数据时附卡片。
        易混淆黑话（花甲=Wisp 不是 Garuda 这类）在卡片尾给一行澄清（``note``）。
        """
        query = parsed.content_str
        # 链接来源：配置 wiki_link_source（默认英文 wiki）；末尾加「灰机」临时用灰机 wiki
        src = str((getattr(self, "cfg", None) or {}).get("wiki_link_source", "en")).strip().lower()
        src = "huiji" if src == "huiji" else "en"
        _toks = query.split()
        if len(_toks) >= 2 and _toks[-1].lower() in _HUIJI_WORDS:
            src, query = "huiji", " ".join(_toks[:-1])
        if not query:
            return err_card(
                L("没有写关键词", "No keyword given"),
                L(
                    "按 wiki 关键词 的格式重发，例如「wiki 夜灵平原」",
                    'Resend as "wiki <keyword>", e.g. "wiki Plains of Eidolon"',
                ),
                (
                    L("用法", "Usage"),
                    L(
                        "wiki 关键词（本地词库优先，未命中给搜索链接）",
                        "wiki <keyword> (local lexicon first; a search link if nothing matches)",
                    ),
                ),
                title=L("wiki 直达", "Wiki Link"),
            )
        note = self.client.wiki_clarify(query)
        hit = self.client.wiki_lookup(query)
        if hit:
            # 概念页 / 战甲外号页（wiki 段）：同样出卡片（用户口径：都要绘制），
            # 卡片正文若知识库有该页条目就用，没有退最小卡
            _pn, _url = _wiki_localize(
                hit["title"], hit["url"], query, hit["title"], source=src, en=hit.get("en") or ""
            )
            return self._wiki_reply(_pn, _url, [], platform, hit["title"], query, note=note)
        from urllib.parse import quote as _q

        want_variant = matching.variant_intent_any(query)
        # 冷索引可能正由后台预热构建；查找与等待都留在线程里，避免阻塞消息循环。
        found = await asyncio.to_thread(search_engine.search, query, limit=3)
        exact = bool(found) and found[0].get("score") == 0
        # 英文原名里的 Kuva / Tenet / Coda / Prime 等词本身就是「变体意图」词：整串精确命中简介
        # 数据的英文名时仍直接用它（否则 Kuva Bramma 会被前缀模糊吸到「Kuva」资源页）；
        # 中文查询保持原有分支。
        _en_exact_ok = want_variant and not re.search(r"[\u4e00-\u9fff]", query)
        if not exact and (not want_variant or _en_exact_ok):
            # ★ 2026-10-17（知识库审计：「wiki Balla」→ Ballas 的合一众权杖、「wiki 仲裁」→ 仲裁雕像）：
            #   检索只有前缀模糊命中时，先看两类更可靠的精确来源，再退模糊——
            #   ① 简介数据的键精确相等（含玩家可见英文名；检索索引里 Zaw 部件只有内部名「tip one」）；
            #   ② 玩法 / 任务类型词（官方任务类型表 + 机器人已有玩法指令覆盖的活动）：链到 wiki 搜索直达
            #      （页面存在即跳转，不会死链），不落到同前缀的外观 / 装饰品上。
            title = wiki_intro.exact_title(query)
            if title:
                base = title.split("（", 1)[0].strip() or title
                page = _q(search_engine.wiki_title(base).replace(" ", "_"))
                _paren = re.search(r"[（(]([^）)]+)[）)]", title)
                _pn, _url = _wiki_localize(
                    base,
                    f"https://warframe.huijiwiki.com/wiki/{page}",
                    query,
                    _paren.group(1) if _paren else "",
                    source=src,
                )
                return self._wiki_reply(
                    _pn,
                    _url,
                    [],
                    platform,
                    title,
                    query,
                    variants=None if want_variant else wiki_intro.variants(base),
                    note=note,
                )
            play = None if want_variant else _play_term(query)
            if play:
                _pn = play
                if is_en():
                    _pn, _url = _wiki_localize(play, "", query, source=src)
                else:
                    _pn, _url = _wiki_localize(
                        play, await self.client.wiki_search_link(play), query, source=src
                    )
                return self._wiki_reply(
                    _pn,
                    _url,
                    [],
                    platform,
                    play,
                    query,
                    note=note,
                )
            # 中文官方术语（星球 / 地名 / 玩法：水星、火卫二、仲裁…）：检索没有精确命中时直接按术语出页，
            # 不落到前缀模糊命中的外观（水星棕 / 火卫二保育徽章）上
            if not want_variant and (
                _official_en(query) if _CJK_RE.search(query) else _official_en_ascii(query)
            ):
                if src == "huiji" and not is_en():
                    _purl = await self.client.wiki_search_link(query)
                else:
                    _purl = ""
                _pn, _url = _wiki_localize(query, _purl, query, source=src)
                return self._wiki_reply(_pn, _url, [], platform, query, note=note)
        if not found and want_variant:
            # 「摸尸p」这类带 p 后缀的查询词库里没有（别名表只登记无后缀形态）
            # → 剥掉 p/prime 再查，命中后按变体解析（want_variant 已为 True）
            stripped = matching.strip_variant_query(query)
            if stripped and stripped != query:
                found = await asyncio.to_thread(search_engine.search, stripped, limit=3)
        if found:
            # 黑话命中（别名）时页面名取国际服官方名——别名键/国服旧译都不是
            # wiki 页面（「wiki 音妈」「wiki 摸尸」实测死链，2026-09-24）。
            best_name = search_engine.wiki_page_name(found[0], base=not want_variant)
            # 灰机标题归一：含中文的名字要去掉空格与中点（「玻之武杖 Prime」→
            # 「玻之武杖Prime」），否则页面不存在（2026-09-25 浏览器 API 实测）
            page = _q(search_engine.wiki_title(best_name).replace(" ", "_"))
            alts = [f["name"] for f in found[1:] if f["name"] != best_name]
            _pn, _url = _wiki_localize(
                best_name,
                f"https://warframe.huijiwiki.com/wiki/{page}",
                query,
                best_name,
                found[0].get("en") or "",
                base=not want_variant,
                source=src,
            )
            if is_en():  # 候选名也给英文（取不到英文名的保持原样）
                alts = [
                    _en_wiki_title(f.get("en") or "", f["name"]) or f["name"] for f in found[1:]
                ]
                alts = [a for a in alts if a != _pn]
            return self._wiki_reply(
                _pn,
                _url,
                alts,
                platform,
                found[0].get("name") or "",
                found[0].get("en") or "",
                query,  # 前缀命中（「电路」→「电路效果」）时按原词找卡片
                variants=None if want_variant else wiki_intro.variants(best_name),
                note=note,
            )
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
                    search_engine.base_slug(slug) if not want_variant else slug
                )
            else:
                page_name = search_engine.wiki_page_name(
                    {"name": item.get("zh") or "", "en": item.get("en") or ""},
                    base=not want_variant,
                )
            page = _q(search_engine.wiki_title(page_name).replace(" ", "_"))
            _pn, _url = _wiki_localize(
                page_name,
                f"https://warframe.huijiwiki.com/wiki/{page}",
                query,
                page_name,
                item.get("en") or "",
                base=not want_variant,
                source=src,
            )
            return self._wiki_reply(
                _pn,
                _url,
                [],
                platform,
                item.get("zh") or "",
                item.get("en") or "",
                variants=None if want_variant else wiki_intro.variants(page_name),
                note=note,
            )
        link = (
            _en_wiki_url("", query)
            if is_en() or src == "en"
            else await self.client.wiki_search_link(query)
        )
        # 未收录时也把澄清带上（如「跑男」这种自带歧义的写法）
        r = err_card(
            L(f"本地词库未收录「{query}」", f'"{query}" is not in the local lexicon'),
            L(
                "点下方链接前往维基搜索；或换官方中文 / 英文名重发",
                "Open the link below to search the wiki, or resend with the official name",
            ),
            (L("提示", "Note"), note) if note else "",
            title=L("wiki 直达", "Wiki Link"),
        )
        r.extra_text = link  # 链接必须是可点的纯文本
        return r

    async def _h_translate(self, parsed, event, platform) -> Reply:
        """翻译：中英名称对照（纯文本，**不渲染图片**）。

        数据：构建期生成的 core/data/de/name_bilingual.json（DE 官方 language
        表筛选，含 MOD/武器/战甲/赋能/资源/部件）；查表走
        matching.normalize_name（与 部件/wiki 同一套归一化）——
        「阿索代prime」≡「阿索代 Prime」≡「athodai」都能查到。
        多义给少量候选（每行一条）；找不到明确提示，不静默、不猜。
        """
        q = (parsed.content_str or "").strip()
        if not q:
            return err_card(
                L("没有写要翻译的名称", "No name to translate"),
                L(
                    "按 翻译 名称 的格式重发，例如「翻译 腐蚀投射」",
                    'Resend as "translate <name>", e.g. "translate Corrosive Projection"',
                ),
                (
                    L("用法", "Usage"),
                    L(
                        "翻译 腐蚀投射 ／ 翻译 Corrosive Projection（中英双向；只查名称类词条）",
                        "translate 腐蚀投射 / translate Corrosive Projection (Chinese-English both ways; names only)",
                    ),
                ),
                title=L("名称翻译", "Name Translation"),
            )
        r = matching.bilingual_lookup(q)

        # 方向：zh→en 时「原词（中）=> 译名（英）」；en→zh 反之。
        # 表内 pair 一律 (en, zh)，展示时按 direction 排前后。
        def _fmt(pairs):
            if r["direction"] == "zh→en":
                return [f"[{zh}] => {en}" for en, zh in pairs]
            return [f"[{en}] => {zh}" for en, zh in pairs]

        if r["hits"]:
            return Reply(raw_text="\n".join(_fmt(r["hits"])))
        if r["candidates"]:
            lines = [
                L(
                    f"未找到精确匹配「{q}」，相近候选：",
                    f'No exact match for "{q}", close candidates:',
                )
            ] + _fmt(r["candidates"])
            return Reply(raw_text="\n".join(lines))
        return err_card(
            L(f"未找到「{q}」（未收录该名称）", f'"{q}" not found (name not indexed)'),
            L(
                "改用官方中文或英文全名再试",
                "Try again with the full official Chinese or English name",
            ),
            title=L("名称翻译", "Name Translation"),
        )

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
                _el = m.group(1).strip()
                pairs.append((_VALENCE_EN.get(_el.lower(), _el), float(m.group(2))))

        if len(pairs) >= 2:
            (e1, p1), (e2, p2) = pairs[0], pairs[1]
            has_elem = bool(e1 or e2)
            res = calc.valence_fusion(e1 or "火", p1, e2 or "火", p2)
            if "error" in res:
                return err_card(
                    L(
                        res["error"],
                        f"Unrecognised element: {e1}/{e2} (supported: electricity / heat / cold / toxin)",
                    ),
                    L(
                        "元素写成：电 / 火 / 冰 / 毒 / 冲击 / 磁力 / 辐射，例如「武器融合 电60 火58」",
                        "Write elements as electricity / heat / cold / toxin / impact / magnetic / radiation, "
                        'e.g. "valence electricity60 heat58"',
                    ),
                    title=L("效价融合", "Valence Fusion"),
                )
            # 没给元素时不硬编一个「火」，也不输出问号
            if has_elem:
                labels = [
                    f"{(L(e, _valence_en(e) + ' ') if e else '—')}{p:g}%" for e, p in pairs[:2]
                ]
            else:
                labels = [f"{p:g}%" for _, p in pairs[:2]]
            # 行文法：卡首答案「融合结果」→ 材料 / 元素 / 公式
            _res = L(f"加成 {res['percent']:g}%", f"Bonus {res['percent']:g}%")
            if res["percent"] < calc.VALENCE_CAP:
                steps = calc.fusion_to_cap(res["percent"])
                _res += L(
                    f"｜到满值还需 {len(steps)} 次融合（用同数值材料）",
                    f"｜{len(steps)} more fusions to the cap (using materials of equal value)",
                )
            lines = [
                L(f"◆ 融合结果：{_res}", f"◆ Fusion result: {_res}"),
                L(f"◆ 融合材料：{' ＋ '.join(labels)}", f"◆ Materials: {' + '.join(labels)}"),
            ]
            # 没给元素时别硬编一个「火」，也别输出「?」
            if has_elem:
                _same = len(res["options"]) == 1
                lines.append(
                    L(
                        f"◆ 元素：{' 或 '.join(res['options'])}（{res['note']}）",
                        f"◆ Element: {' or '.join(_valence_en(o) for o in res['options'])} "
                        + (
                            "(same element, no choice needed)"
                            if _same
                            else "(pick either; elements never combine into composites)"
                        ),
                    )
                )
            lines.append(
                L(
                    f"◆ 公式：结果 = 较高值 {max(p1, p2):g}% × 1.1"
                    + (" → ≥58% 直接进位 60%" if res["percent"] >= 60 else ""),
                    f"◆ Formula: result = higher value {max(p1, p2):g}% × 1.1"
                    + (" → ≥58% rounds up to 60%" if res["percent"] >= 60 else ""),
                )
            )
            lines.append(
                L(
                    "※ 必须是同款武器（同一把 Kuva/Tenet/Coda，同系列不同名不行）",
                    "※ Both must be the same weapon (the same Kuva/Tenet/Coda weapon; different weapons of one series do not work)",
                )
            )
            lines.append(
                L(
                    "※ 只有受体会被保留：材料的催化剂 / Forma / 架式 Forma / 透镜"
                    "都不转移，务必用投资多的那把当受体",
                    "※ Only the receiver is kept: the material's Reactor / Forma / Stance Forma / Lens "
                    "do not transfer, so always use the more invested weapon as the receiver",
                )
            )
            lines.append(
                L(
                    "※ 满值判定：≥58% 即进位到 60%，所以材料 ≥52.8% 可一步满值",
                    "※ Cap rule: ≥58% rounds up to 60%, so a material ≥52.8% reaches the cap in one step",
                )
            )
            return Reply(L("效价融合", "Valence Fusion"), lines)

        if len(pairs) == 1:
            _e1, p1 = pairs[0]
            trace = calc.fusion_to_cap(p1)
            lines = [
                L(
                    f"◆ 共需：{len(trace)} 次融合（用同数值材料；材料数值越高可少几次）",
                    f"◆ Total needed: {len(trace)} fusions (using materials of equal value; a higher material needs fewer)",
                ),
                L(
                    f"◆ 融合轨迹（从 {p1:g}% 到 60%）",
                    f"◆ Fusion path (from {p1:g}% to 60%)",
                ),
                "· "
                + (" → ".join(f"{x:g}%" for x in trace) or L("已是满值", "Already at the cap")),
            ]
            lines.append(
                L(
                    "※ 材料 ≥58% 时受体无需再看自身数值，融合后直接 60%",
                    "※ With a material ≥58% the receiver's own value no longer matters; the result is 60%",
                )
            )
            return Reply(L("效价融合规划", "Valence Fusion Plan"), lines)

        return err_card(
            L("没有给出可计算的数值", "No values to calculate"),
            L("按上面的示例重发", "Resend following the examples above"),
            L(
                "· 武器融合 电60 火58　两把融合的结果与可选元素",
                "· valence electricity60 heat58　result and element choice for two weapons",
            ),
            L(
                "· 武器融合 44　　　　　从 44% 到 60% 需要融合几次",
                "· valence 44　　　　　fusions needed from 44% to 60%",
            ),
            (
                L("支持元素", "Supported elements"),
                L(
                    "电 / 火 / 冰 / 毒 / 冲击 / 磁力 / 辐射",
                    "electricity / heat / cold / toxin / impact / magnetic / radiation",
                ),
            ),
            title=L("效价融合", "Valence Fusion"),
        )

    async def _h_damage(self, parsed, event, platform) -> Reply:
        """伤害计算器 v1：武器 + 配卡加成 → 对指定派系/等级敌人的期望伤害。

        用法：伤害 <武器名> [G系/C系/I系/炽蛇军/科腐者…] [N级] [爆头]
                  [基伤N] [多重N] [暴率N] [暴伤N] [派系N] [电/火/冰/毒N] [基甲N]
        公式与机制基准见 core/damage_calc.py 模块注释（U36 抗性重构后）。
        """
        # 解析 + 计算是纯 CPU 活（整条配卡粘贴要逐词跑模糊匹配），放线程里跑，
        # 避免长串参数在事件循环上卡住所有群
        capability = getattr(self, "_can_send_actions", None)
        can_actions = callable(capability) and capability(event)
        return await asyncio.to_thread(self._damage_reply, parsed, can_actions=can_actions)

    def _damage_reply(self, parsed, *, can_actions: bool = False) -> Reply:
        """「伤害」指令的同步主体（由 _h_damage 放进线程执行）。"""
        _toks = _dmg_en_toks(parsed.content) if is_en() else parsed.content
        spec, name_tokens = dc.parse_args(_toks)
        query = " ".join(name_tokens).strip()
        if not query:
            return Reply(
                L("伤害计算", "Damage Calculator"),
                L(
                    [
                        "◆ 用法：伤害 <武器名> [对 敌人名] [派系] [N级] [爆头] [加成项…]",
                        "◆ 示例",
                        "· 步枪　伤害 布拉玛 对 重机枪手 100级 膛线 分裂膛室 地狱火 电90",
                        "· 近战　伤害 空刃 对 重机枪手 100级 钢铁凤凰 异况超量 急进猛突 连击120",
                        "◆ 配卡（三种写法可混用）",
                        "· MOD 名　满级值，可整条配卡直接粘贴：膛线 / 分裂膛室 / 镀层分裂膛室 / 地狱火 / "
                        "关键延迟 / 瞄准目标 …",
                        "· 手写加成　基伤 / 多重 / 暴率 / 暴伤 / 爆头倍率 / 派系（%）",
                        "· 元素　电 火 冰 毒（自动合成复合）｜冲击 穿刺 切割（只加同类型）",
                        "· 其它手写　状态伤害N（只放大 DoT）｜基甲N（覆盖默认敌人基准甲）",
                        "· 敌人名　枪兵 / 重机枪手 / 轰击者 / 船员 / 恐鸟 / 夜灵 …（常用 19 个，支持模糊）；"
                        "给了就用它的真实基准甲/血/盾，并算击杀发数",
                        "◆ 异常（U36 后元素差异的主要来源）",
                        "· 病毒N / 磁力N　1-10 层，对血 / 对盾加伤",
                        "· 腐蚀N / 火剥甲　腐蚀N：剥甲 26%+6%×每层，满层 −80%｜火剥甲：−50%",
                        "◆ 近战",
                        "· 连击N / 重击 / 架势名　钢铁凤凰 / 猎鹰俯击 …：每段倍率+强制异常",
                        "· 急进猛突 / 创口溃烂 / 异况超量 / 一击必杀 / 奋力一掷　按实际等级折算",
                        "◆ 其它",
                        "· 派系　G系 / C系 / I系 / 合一众 / 奥罗金 / 低语者 / 扎里曼 / 炽蛇军 / 科腐者 …",
                        "· 赋能 <名> / 异常N　赋能：条件触发只展示不折算｜异常N：目标异常种类数",
                        "· 镀层N / 满镀层　镀层类击杀堆叠按 N 层计入，如镀层 分裂膛室",
                        "· 超宏[N] / 适应N　超宏：不吃护甲/派系、免疫异常｜适应：Sentient 适应 1-4 层",
                        "· 灵化 / 基础形态　默认按原型算；加「灵化」切灵化形态",
                        "· 副开火 / 蓄力 / 未蓄力 / 半自动 / 全自动　切到该形态的整套面板与范围段，选了才算",
                        "· 开火N秒　火焰单例累加：稳态火层数按开火 N 秒窗口平均，默认 12",
                        "· 空战 / 地面　Archgun 双部署：默认地面・大气；空战用空战面板",
                        "· 进化 基伤/暴击/爆头…　灵化进化选项，可多次；改基础面板",
                        "· 赤毒辐射60　赤毒/信条/终幕回响加成 25-60%，元素要写",
                        "◆ 输出",
                        "· 单发　对血/对盾（无暴击）→ 暴击与爆头期望（含暴击）",
                        "· 每次扳机 / DPS　爆发/持续（均给含暴击与无暴击）",
                    ],
                    [
                        "◆ Usage: damage <weapon> [vs <enemy>] [faction] [lvN] [headshot] [modifiers...]",
                        "◆ Examples",
                        "· Rifle　damage Bramma vs Heavy Gunner 100 Serration Split Chamber Hellfire electricity90",
                        "· Melee　damage Dark Dagger vs Heavy Gunner 100 combo120",
                        "◆ Build (the three styles can be mixed)",
                        "· MOD names　max-rank values; a whole build can be pasted: Serration / Split Chamber / Hellfire / ...",
                        "· Manual bonuses　basedmgN / multishotN / critchanceN / critdmgN / hsmultN / factionN (%), e.g. critchance150",
                        "· Elements　electricity heat cold toxin (composites form automatically) | impact puncture slash (add to the same type only), e.g. electricity90",
                        "· Enemy name　the enemy after vs (about 19 common enemies, fuzzy matching); when given, its real base armor / health / shield is used and shots-to-kill is shown",
                        "◆ Status effects",
                        "· viralN / magneticN　1-10 stacks, extra damage to health / shields",
                        "· corrosiveN / strip　corrosiveN: armor strip 26%+6% per stack, -80% at full stacks | strip: Heat armor strip -50%",
                        "◆ Melee",
                        "· comboN / heavy attack (or heavy at the end) / stance name　each hit multiplier + forced status",
                        "◆ Other",
                        "· Faction　grineer / corpus / infested / amalgam / orokin / sentient / narmer ...",
                        "· steelpath / lvN / bare number　Steel Path | enemy level (a bare number is the level)",
                        "· incarnon / base form　defaults to the base form; add incarnon to switch",
                        "· alt fire / charged / uncharged / semi-auto / auto　switch to that fire mode's full panel, only calculated when chosen",
                        "· fire Ns　Heat stacking: steady-state stacks averaged over an N-second window, default 12",
                        "· aerial / ground　Archgun deployment: defaults to ground / atmosphere",
                        "· evolve X　Incarnon evolution choice, repeatable",
                        "· kuva-radiation60　Kuva / Tenet / Coda bonus 25-60%, the element must be written",
                        "◆ Output",
                        "· Single hit　vs health / shield (no crit) → expected crit and headshot (with crit)",
                        "· Per trigger / DPS　burst / sustained (with and without crit)",
                    ],
                ),
                actions=[
                    Action(
                        L("照示例发", "Send the example"),
                        L(
                            "伤害 布拉玛 对 重机枪手 100级 膛线 分裂膛室 地狱火 电90",
                            "damage Bramma vs Heavy Gunner 100 Serration Split Chamber Hellfire electricity90",
                        ),
                        mode="fill",
                    )
                ]
                if can_actions
                else [],
            )

        try:
            weapon, alts = (None, []) if dc.is_variant_only(query) else dc.find_weapon(query)
            if weapon is None:
                miss = L("、", ", ").join(spec.get("unknown") or []) or "—"
                shown = query
                if dc.is_variant_only(query):
                    # 只剩「Prime」这类变体词：武器名本体被当未识别词丢了 ⇒ 按原文词序拼回来显示
                    #   （「布拉顿 Prime」此前显示成「没找到『Prime』」）
                    _keep = set(spec.get("unknown") or []) | set(name_tokens)
                    shown = " ".join(t for t in (parsed.content or []) if t in _keep) or query
                return Reply(
                    L("伤害计算", "Damage Calculator"),
                    [
                        L(
                            f"◆ 原因：武器库里没找到「{shown}」",
                            f'◆ Reason: "{shown}" was not found in the weapon library',
                        ),
                        L(f"◆ 没认出来的词：{miss}", f"◆ Unrecognised words: {miss}"),
                        L(
                            "※ ⚠ 怎么改：换英文名或完整中文名再试，例如「Kuva Bramma / 赤毒布拉玛」",
                            '※ ⚠ How to fix: retry with the full official name, e.g. "Kuva Bramma"',
                        ),
                    ],
                )
            res = dc.calculate(spec, weapon)
            query_name = weapon.get("zh") or weapon.get("name")
            if is_en():
                query_name = weapon.get("name") or query_name
            return Reply(
                L("伤害计算", "Damage Calculator"),
                dc.card_lines(weapon, spec, res, alts),
                footer=dc.card_footer(weapon, res),
                actions=[
                    Action(
                        L("换敌人复算", "Recalculate vs another enemy"),
                        L(f"伤害 {query_name} 对 ", f"damage {query_name} vs "),
                        mode="fill",
                    )
                ]
                if can_actions and query_name and res.get("ok")
                else [],
            )
        except Exception as exc:  # noqa: BLE001 - 绝不静默：群聊里没输出最难查
            logger.exception(f"[sdjk] 伤害计算失败：{exc!r}")
            _mods = L("、", ", ").join(
                (m.get("name") or m.get("zh") or "")
                if is_en()
                else (m.get("zh") or m.get("name") or "")
                for m in spec.get("mods") or []
            )
            return Reply(
                L("伤害计算", "Damage Calculator"),
                [
                    L(
                        "◆ 原因：计算出错（内部错误，详情已写入日志）",
                        "◆ Reason: calculation error (internal error, details are in the log)",
                    ),
                    L(
                        f"◆ 输入：武器 {query}｜已识别 MOD：{_mods or '无'}",
                        f"◆ Input: weapon {query}｜recognised MODs: {_mods or 'None'}",
                    ),
                    L(
                        "※ ⚠ 怎么改：换个写法重发；仍出错请把这条指令原文反馈给管理员",
                        "※ ⚠ How to fix: resend with different wording; if it still fails, report the exact command to an admin",
                    ),
                ],
            )

    # ------------------------------------------------------------------
