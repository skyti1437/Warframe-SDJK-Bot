# -*- coding: utf-8 -*-
"""紫卡市场类 5 项报障的回归守卫（python3 tests/test_riven_market_fixes.py）

2026-09-24 用户实测报障，逐条钉死：

① 紫卡分析「解析到 4 正 0 负」整卡作废 —— 卡面负词条（-63.4% 滑行攻击暴击
   几率）被 vision 归进 positive；现按「数值带负号 → 负词条」收。
② 「紫卡分析 + 截图」报未找到「翁洛奇亚鲁姆」/「初始 翁」—— 识别把紫卡自命名
   连写/音译进武器名；现在解析链末跳做「输入里含最长已知武器名」兜底。
③ 紫卡分析负词条行「幅度位」方向反了（-99.9 贴区间下限却显示 87%=负得很满）。
④ 倾向卡把曲翼枪械显示成「步枪」，且补全变体没中文名（Larkspur Prime → 现在
   显示「翠雀 Prime 曲翼枪械」）。
⑤ wr 严格匹配为空时，近似结果被渲染层按「在线+价格」重排，前排全是与词条无关
   的便宜挂单；现在 ①先放宽在线状态（词条仍严格）②再按命中率排且保留顺序。
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _install_astrbot_stub() -> None:
    if "astrbot" in sys.modules:
        return
    pkg = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    event_mod = types.ModuleType("astrbot.api.event")
    mc_mod = types.ModuleType("astrbot.api.message_components")
    star_mod = types.ModuleType("astrbot.api.star")

    class AstrBotConfig(dict):
        pass

    class _Logger:
        def info(self, *a, **k):
            pass

        def warning(self, *a, **k):
            pass

        def error(self, *a, **k):
            pass

        def exception(self, *a, **k):
            pass

        def debug(self, *a, **k):
            pass

    class AstrMessageEvent:
        def __init__(self):
            self.unified_msg_origin = "group://riven_market_fixes"

        def get_sender_name(self) -> str:
            return "stub"

    class MessageChain:
        def message(self, text):
            return text

    class _EventMessageType:
        ALL = "ALL"

    class _Filter:
        EventMessageType = _EventMessageType
        event_message_type = staticmethod(lambda spec: lambda fn: fn)

    event_mod.AstrMessageEvent = AstrMessageEvent
    event_mod.MessageChain = MessageChain
    event_mod.EventMessageType = _EventMessageType
    event_mod.event_message_type = lambda spec: lambda fn: fn
    event_mod.filter = _Filter()
    mc_mod.Image = type("Image", (), {})
    mc_mod.Plain = type("Plain", (), {})
    star_mod.Context = type("Context", (), {})

    class Star:
        def __init__(self, *a, **k):
            pass

    star_mod.Star = Star
    star_mod.register = lambda *a, **k: lambda cls: cls
    api.AstrBotConfig = AstrBotConfig
    api.logger = _Logger()
    sys.modules.setdefault("astrbot", pkg)
    sys.modules["astrbot.api"] = api
    sys.modules["astrbot.api.event"] = event_mod
    sys.modules["astrbot.api.message_components"] = mc_mod
    sys.modules["astrbot.api.star"] = star_mod


_install_astrbot_stub()

import main as plugin  # noqa: E402
from core import formatters as F  # noqa: E402
from core import riven_analysis as RA  # noqa: E402
from core.api_client import (  # noqa: E402
    WarframeClient,
    load_aliases,
    localize_variant_en,
)
from core.parser import RIVEN_STAT_ZH, parse_wr  # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = ""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f"  -> {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


_rev = {v: k for k, v in RIVEN_STAT_ZH.items()}
_norm = plugin.WarframeSDJK._normalize_llm_stats

# --------------------------------------------------------------- ① 负词条符号
print("=== ① 数值带负号 → 负词条（4 正 0 负 报障）===")
_card = {
    "positive": [["暴击伤害", 60.8], ["暴击", 110.1], ["初始连击", 15.4], ["滑暴", -63.4]],
    "negative": [],
}
_pos, _neg = _norm(_card, _rev)
check(
    "4 条正词条里带负号的那条被移到负词条",
    len(_pos) == 3 and _neg == [("slide_crit", 63.4)],
    f"pos={_pos} neg={_neg}",
)
check("负词条 magnitude 取绝对值（不是 -63.4）", _neg and _neg[0][1] == 63.4, str(_neg))
_pos2, _neg2 = _norm({"positive": [["暴伤", 90.0]], "negative": [["切割伤害", 99.9]]}, _rev)
check(
    "正常的 negative 仍按负词条收（未被误改）",
    _pos2 == [("crit_damage", 90.0)] and _neg2 == [("slash_damage", 99.9)],
    f"pos={_pos2} neg={_neg2}",
)

# --------------------------------------------------- ② 武器名兜底（含最长匹配）
print("\n=== ② 武器名兜底：卡面自命名连写/音译 ===")
_FAKE = [
    {
        "url_name": "okina",
        "zh": "翁",
        "en": "Okina",
        "disposition": 0.5,
        "riven_type": "melee",
        "group": "melee",
        "tags": [],
    },
    {
        "url_name": "fake_longer",
        "zh": "翁洛",
        "en": "FakeLonger",
        "disposition": 1.0,
        "riven_type": "melee",
        "group": "melee",
        "tags": [],
    },
]
_c = WarframeClient.__new__(WarframeClient)
_c._aliases = {}


async def _fake_weapons():
    return [dict(w) for w in _FAKE]


_c.wm_riven_weapons = _fake_weapons
_hit = asyncio.run(_c.resolve_riven_weapon("初始 翁"))
check("「初始 翁」→ okina（词条名混进武器名）", (_hit or {}).get("url_name") == "okina", str(_hit))
check(
    "兜底命中会标 _fuzzy_from（卡面注明来源）",
    (_hit or {}).get("_fuzzy_from") == "初始 翁",
    str(_hit),
)
_hit2 = asyncio.run(_c.resolve_riven_weapon("翁洛奇亚鲁姆"))
check(
    "★ 多个候选时取**最长**已知武器名（翁洛 优先于 翁）",
    (_hit2 or {}).get("url_name") == "fake_longer",
    str((_hit2 or {}).get("url_name")),
)
_hit3 = asyncio.run(_c.resolve_riven_weapon("翁"))
check(
    "精确名仍走正常链路（不标 _fuzzy_from）",
    (_hit3 or {}).get("url_name") == "okina" and not (_hit3 or {}).get("_fuzzy_from"),
    str(_hit3),
)

# ------------------------------------------------------------ ③ 负词条幅度位
print("\n=== ③ 负词条「幅度位」方向 ===")
_title, _lines = F.fmt_riven_analysis(
    "视使之触",
    1.2,
    "pistol",
    [("crit_damage", 108.2), ("multishot", 141.7), ("toxin_damage", 98.2)],
    [("slash_damage", 99.9)],
)
_neg_line = next((x for x in _lines if "切割" in x and x.startswith("·")), "")
check("★ -99.9 切割贴区间下限 → 幅度位 13%（不是反的 87%）", "幅度位 13%" in _neg_line, _neg_line)
check("不再出现方向相反的字样", "幅度位 87%" not in _neg_line, _neg_line)
# 反向锚点：贴上限的负词条应显示高幅度位
_t2, _l2 = F.fmt_riven_analysis(
    "视使之触",
    1.2,
    "pistol",
    [("crit_damage", 108.2), ("multishot", 141.7), ("toxin_damage", 98.2)],
    [("slash_damage", 118.0)],
)
_neg_line2 = next((x for x in _l2 if "切割" in x and x.startswith("·")), "")
check(
    "贴上限的负词条 → 幅度位接近 100%（正负号都验过）",
    "幅度位 10" not in _neg_line2 and "幅度位 9" in _neg_line2,
    _neg_line2,
)

# ------------------------------------------------- ④ 变体中文名 + 曲翼枪械分类
print("\n=== ④ 变体中文名补全与曲翼枪械分类 ===")
_base = {
    "larkspur": "翠雀",
    "braton": "布莱顿",
    "grattler": "葛拉特勒",
    "war": "战争之剑",
    "hema": "血肢",
}
for _en, _want in (
    ("Larkspur Prime", "翠雀 Prime"),
    ("MK1-Braton", "MK1-布莱顿"),
    ("Kuva Grattler", "赤毒·葛拉特勒"),
    ("Coda Hema", "终幕·血肢"),
    ("War Prime", "战争之剑 Prime"),
    ("Tombfinger (Secondary)", "墓指（次要）"),
):
    _got = localize_variant_en(_en, _base)
    check(f"变体规则 {_en} → {_want}", _got == _want, _got)
check(
    "规则不认识/基名查不到 → 空串（保持英文，不猜）",
    localize_variant_en("Bogus Prime", {"bogus": ""}) == ""
    and localize_variant_en("Mystery Thing", {}) == "",
)

_names = __import__("json").loads(
    (ROOT / "core" / "data" / "de" / "name_en_zh.json").read_text(encoding="utf-8")
)
_names = _names.get("names") or {}


def _name_lookup(en: str) -> str:
    from core.api_client import _norm_name_key

    for k, v in _names.items():
        if _norm_name_key(k) == _norm_name_key(en):
            return v
    return ""


check(
    "★ DE 官方简中表能补出 Larkspur Prime（报障那条）",
    _name_lookup("Larkspur Prime") == "翠雀 Prime",
    _name_lookup("Larkspur Prime"),
)

check(
    "曲翼枪械：riven_type=rifle + group=archgun → 曲翼枪械",
    F.riven_type_cn("rifle", "archgun") == "曲翼枪械",
    F.riven_type_cn("rifle", "archgun"),
)
check(
    "守护武器：group=sentinel → 守护武器",
    F.riven_type_cn("rifle", "sentinel") == "守护武器",
    F.riven_type_cn("rifle", "sentinel"),
)
check(
    "普通武器不受影响（group=melee/pistol → 原样）",
    F.riven_type_cn("melee", "melee") == "近战"
    and F.riven_type_cn("pistol", "secondary") == "手枪",
)
check(
    "★ 基值列：曲翼枪械必须用 archgun 列（暴伤 80.1 而非步枪 120）",
    RA.weapon_class("rifle", "archgun") == "archgun",
    RA.weapon_class("rifle", "archgun"),
)
check(
    "射击武器（group=secondary/pistol）仍按手枪列",
    RA.weapon_class("pistol", "secondary") == "pistol",
)
check(
    "守护武器回落步枪列（wiki 无守护列，卡面已注明近似）",
    RA.weapon_class("rifle", "sentinel") == "rifle",
)

# ------------------------------------------------------- ⑤ wr 排序与放宽分档
print("\n=== ⑤ wr：放宽分档 + 排序保留 ===")
check(
    "presorted=True 时渲染层不按 在线+价格 重排",
    [
        x
        for x in F.fmt_wr_auctions(
            "翁",
            [
                {"buyout_price": 300, "owner": {"status": "offline"}, "item": {"attributes": []}},
                {"buyout_price": 230, "owner": {"status": "offline"}, "item": {"attributes": []}},
            ],
            presorted=True,
        )[1]
        if x.startswith("1. ")
    ][0].startswith("1. 一口价：300p"),
)
check(
    "默认（严格档）仍按价格升序",
    [
        x
        for x in F.fmt_wr_auctions(
            "翁",
            [
                {"buyout_price": 300, "owner": {"status": "offline"}, "item": {"attributes": []}},
                {"buyout_price": 230, "owner": {"status": "offline"}, "item": {"attributes": []}},
            ],
        )[1]
        if x.startswith("1. ")
    ][0].startswith("1. 一口价：230p"),
)

_q = parse_wr("爆率 爆伤 初始连击 负任意 翁".split())
check(
    "解析：初始连击 → channeling_damage（WM 搜索词表口径）",
    WarframeClient.normalize_riven_stats(_q.stats, "melee")
    == ["critical_chance", "critical_damage", "channeling_damage"],
    str(WarframeClient.normalize_riven_stats(_q.stats, "melee")),
)

_match_auction = {
    "buyout_price": 500,
    "owner": {"status": "offline"},
    "item": {
        "attributes": [
            {"url_name": "critical_chance", "value": 100, "positive": True},
            {"url_name": "critical_damage", "value": 80, "positive": True},
            {"url_name": "channeling_damage", "value": 30, "positive": True},
            {"url_name": "damage_vs_grineer", "value": 0.7, "positive": False},
        ]
    },
}
# ★ 2026-10-08 口径变更（用户）：「即使某个词条的在线挂单只有一张，也要把离线的也列出来」
#   ⇒ 默认档不再按在线状态过滤（离线单直接进池，靠渲染层「在线优先」排到后面并标 ⚫离线）；
#   只有显式「最新」才只留游戏中。旧断言（默认档必须滤掉离线、靠 ignore_status 兜底）已按新口径改写。
check(
    "★ 完全匹配且卖家离线 → 默认档就收进来（不再靠 ignore_status 兜底）",
    plugin.WarframeSDJK._auction_match(
        _match_auction, _q, set(), {"critical_chance", "critical_damage", "channeling_damage"}
    )
    is True
    and plugin.WarframeSDJK._auction_match(
        _match_auction,
        _q,
        set(),
        {"critical_chance", "critical_damage", "channeling_damage"},
        ignore_status=True,
    )
    is True,
)


class _FakeClient2:
    """wr 端到端：2 条完全匹配（离线）+ 3 条不匹配（在线且更便宜）。"""

    _aliases = {}

    def __init__(self):
        self._weapon = {
            "url_name": "okina",
            "zh": "翁",
            "en": "Okina",
            "riven_type": "melee",
            "group": "melee",
        }

    async def resolve_riven_weapon(self, q):
        return dict(self._weapon)

    async def wm_riven_weapon_slugs(self):
        # 市场按母武器认卡（2026-10-05）：本桩条目即 WM 自家条目 ⇒ 不触发回退
        return {self._weapon["url_name"]}

    def normalize_riven_stats(self, stats, rtype=""):
        return WarframeClient.normalize_riven_stats(stats, rtype)

    async def wm_riven_auctions(self, *a, **k):
        def _au(p, st, attrs):
            return {"buyout_price": p, "owner": {"status": st}, "item": {"attributes": attrs}}

        full = [
            {"url_name": "critical_chance", "value": 100, "positive": True},
            {"url_name": "critical_damage", "value": 80, "positive": True},
            {"url_name": "channeling_damage", "value": 30, "positive": True},
            {"url_name": "damage_vs_grineer", "value": 0.7, "positive": False},
        ]
        junk = [
            {"url_name": "critical_chance", "value": 100, "positive": True},
            {"url_name": "cold_damage", "value": 57, "positive": True},
            {"url_name": "damage_vs_corpus", "value": 0.76, "positive": False},
        ]
        return [
            _au(230, "ingame", junk),
            _au(240, "ingame", junk),
            _au(300, "ingame", junk),
            _au(750, "offline", full),
            _au(1000, "offline", full),
        ]


class _Parsed:
    def __init__(self, content="", preset="", page=1, whisper=False):
        self.content = str(content).split()[1:]
        self.preset = preset
        self.page = page
        self.whisper = whisper


_obj = plugin.WarframeSDJK.__new__(plugin.WarframeSDJK)
_obj.client = _FakeClient2()
_obj.page_size = 12
_reply = asyncio.run(_obj._h_wr(_Parsed("wr 爆率 爆伤 初始连击 负任意 翁"), None, "pc"))
_body = "\n".join(_reply.lines)
_rows = [x for x in _reply.lines if x.startswith(("1. ", "2. ", "3. "))]
check(
    "★ 前排是完全匹配条（750p/1000p），不是更便宜的无关单",
    len(_rows) >= 2 and "750p" in _rows[0] and "1000p" in _rows[1],
    str(_rows[:3]),
)
check(
    "给出「含 N 条离线挂单」说明（而不是含糊的近似匹配）",
    any("离线挂单" in x for x in _reply.lines),
    _body[:200],
)
check("近似匹配的兜底文案不再出现在本场景", not any("最接近选项" in x for x in _reply.lines))

# ------------------------------------------------- ⑥ 老卡：按反推倾向算区间
print("\n=== ⑥ 老卡（洗出后倾向被调整）按反推值算区间 ===")
_WEAPONS_FAKE = [
    {
        "url_name": "okina",
        "zh": "翁",
        "en": "Okina",
        "disposition": 1.4,
        "riven_type": "melee",
        "group": "melee",
    }
]


class _FakeClient3:
    """翁（当前倾向 1.4）；卡面数值是倾向 0.7 时代洗出来的。"""

    _aliases = {}

    async def resolve_riven_weapon(self, q):
        return dict(_WEAPONS_FAKE[0])

    async def resolve_variant_disp(self, name):
        return (None, "")

    async def riven_family(self, weapon):
        return []

    async def wm_riven_weapons(self):
        return [dict(w) for w in _WEAPONS_FAKE]


_obj3 = plugin.WarframeSDJK.__new__(plugin.WarframeSDJK)
_obj3.client = _FakeClient3()
_obj3.page_size = 12
_r3 = asyncio.run(
    _obj3._h_riven_analysis(
        _Parsed("紫卡分析 翁 暴伤60.8 暴击110.1 初始连击15.4 负滑暴63.4"), None, "pc"
    )
)
_body3 = "\n".join(_r3.lines)
check(
    "★ 老卡不再报「武器名可能识别有误」，而是反推倾向 ≈0.69",
    "反推倾向 ≈0.69" in _body3 and "识别有误" not in _body3,
    "\n".join(_r3.lines[:3]),
)
check(
    "★ 区间按反推值计算（区间位不再是清一色 0%）",
    "区间位 0%" not in _body3 and "区间位 72%" in _body3,
    "\n".join(_r3.lines[:5]),
)
check(
    "老卡提示里写明「倾向调整前洗出」与当前值对照",
    "疑似倾向调整前洗出的老卡" in _body3 and "当前值 1.4" in _body3,
    "\n".join(_r3.lines[:3]),
)
# 反向守卫：反推区间不紧（词条互相矛盾）时，仍给「武器名可能识别有误」
_r4 = asyncio.run(
    _obj3._h_riven_analysis(_Parsed("紫卡分析 翁 暴伤60.8 暴击110.1 初始连击100"), None, "pc")
)
check(
    "反推区间不紧时明确提示「疑似变体卡·带前缀重发」（不硬套反推值）",
    "疑似变体卡（赤毒 / 信条 / 终幕 等）" in "\n".join(_r4.lines)
    and "请带变体前缀重发" in "\n".join(_r4.lines),
    "\n".join(_r4.lines[:3]),
)

# --------------------------------- ⑦ p 后缀必须区分「基础版 / Prime」两件东西
print("\n=== ⑦ p 后缀：基础版与 Prime 是两件东西 ===")
# 真实 zh/en 抄自 WM（2026-09-24）：普通版也能交易时，Prime 是 _prime_set 兄弟条目
_ITEMS_FAKE = [
    {
        "url_name": "nautilus_set",
        "zh": "鹦鹉螺 一套",
        "en": "Nautilus Set",
        "tags": ["sentinel", "set"],
    },
    {
        "url_name": "nautilus_prime_set",
        "zh": "鹦鹉螺 Prime 一套",
        "en": "Nautilus Prime Set",
        "tags": ["prime", "sentinel", "set"],
    },
    {
        "url_name": "nautilus_blueprint",
        "zh": "鹦鹉螺 蓝图",
        "en": "Nautilus Blueprint",
        "tags": ["sentinel", "blueprint"],
    },
    {"url_name": "epitaph_set", "zh": "葬铭 一套", "en": "Epitaph Set", "tags": ["weapon", "set"]},
    {
        "url_name": "epitaph_prime_set",
        "zh": "葬铭 Prime 一套",
        "en": "Epitaph Prime Set",
        "tags": ["prime", "weapon", "set"],
    },
    {"url_name": "corvas_set", "zh": "黑鸦 一套", "en": "Corvas Set", "tags": ["weapon", "set"]},
    {
        "url_name": "corvas_prime_set",
        "zh": "黑鸦 Prime 一套",
        "en": "Corvas Prime Set",
        "tags": ["prime", "weapon", "set"],
    },
    {"url_name": "zylok", "zh": "席尔火枪", "en": "Zylok", "tags": ["weapon"]},
    {
        "url_name": "zylok_prime_set",
        "zh": "席尔火枪 Prime 一套",
        "en": "Zylok Prime Set",
        "tags": ["prime", "weapon", "set"],
    },
    {"url_name": "pressure_point", "zh": "压迫点", "en": "Pressure Point", "tags": ["mod"]},
    {
        "url_name": "primed_pressure_point",
        "zh": "压迫点 Prime",
        "en": "Primed Pressure Point",
        "tags": ["mod"],
    },
]
_c2 = WarframeClient.__new__(WarframeClient)
_c2._aliases = load_aliases()


async def _items_fake():
    return [dict(x) for x in _ITEMS_FAKE]


_c2.wm_items = _items_fake
for _q, _want in (
    ("鹦鹉螺", "nautilus_set"),
    ("鹦鹉螺p", "nautilus_prime_set"),
    ("鹦鹉螺 prime", "nautilus_prime_set"),
    ("葬铭", "epitaph_set"),
    ("葬铭p", "epitaph_prime_set"),
    ("黑鸦p", "corvas_prime_set"),
    ("席尔火枪", "zylok"),
    ("席尔火枪p", "zylok_prime_set"),
    ("压迫点", "pressure_point"),
    ("压迫点p", "primed_pressure_point"),
):
    _hit = asyncio.run(_c2.resolve_wm_item(_q))
    check(
        f"★ {_q} → {_want}（不落回另一件）",
        (_hit or {}).get("url_name") == _want,
        str((_hit or {}).get("url_name")),
    )

from core.matching import prime_sibling  # noqa: E402

check(
    "★ prime_sibling 能容忍「一套」后缀（鹦鹉螺 Prime 一套 → 找到）",
    (prime_sibling(_ITEMS_FAKE[0], _ITEMS_FAKE) or {}).get("url_name") == "nautilus_prime_set",
)
check(
    "prime_sibling 对无 Prime 版返回 None（不瞎配）",
    prime_sibling({"url_name": "x", "zh": "不存在的武器", "en": "Nonexistent"}, _ITEMS_FAKE)
    is None,
)

# ------------------------------------------- ⑧ 小数点丢点修正 + 卡面长词条名
print("\n=== ⑧ 小数点修正与长词条名 ===")
_p, _n = _norm(
    {
        "positive": [["电伤", 115.7], ["攻速", 67.7], ["基伤", 212.9]],
        "negative": [["滑行攻击暴击几率", 110.8]],
    },
    _rev,
)
check(
    "★ 卡面原文「滑行攻击暴击几率」→ slide_crit（不是 crit_chance）",
    _n and _n[0][0] == "slide_crit",
    str(_n),
)
check("正常数值不受影响（115.7 不被误改）", _p[0] == ("electric_damage", 115.7), str(_p))

_obj5 = plugin.WarframeSDJK.__new__(plugin.WarframeSDJK)


class _FakeClient5:
    _aliases = {}

    async def resolve_riven_weapon(self, q):
        return {
            "url_name": "bo",
            "zh": "玻之武杖",
            "en": "Bo",
            "disposition": 1.35,
            "riven_type": "melee",
            "group": "melee",
        }

    async def resolve_variant_disp(self, name):
        return (None, "")

    async def riven_family(self, weapon):
        return []


_obj5.client = _FakeClient5()
_obj5.page_size = 12
_r5 = asyncio.run(
    _obj5._h_riven_analysis(
        _Parsed("紫卡分析 玻之武杖 电伤1157 攻速67.7 基伤212.9 负冲击1108"), None, "pc"
    )
)
_b5 = "\n".join(_r5.lines)
check(
    "★ 丢点数值 1157 → 115.7（按合法区间判据回捞）",
    "+115.7% 电伤" in _b5 and "+1157%" not in _b5,
    _b5[:300],
)
check(
    "修正会在卡面注明（透明可复核）", "已修正小数点" in _b5 and "1157% → 115.7%" in _b5, _b5[:200]
)
check("负词条同样修正（1108 → 110.8）", "-110.8% 冲击" in _b5, _b5[:300])
_r6 = asyncio.run(
    _obj5._h_riven_analysis(
        _Parsed("紫卡分析 玻之武杖 电伤115.7 攻速67.7 基伤212.9 负冲击110.8"), None, "pc"
    )
)
check(
    "正确带点输入不触发修正（无误伤）",
    not any("已修正小数点" in x for x in _r6.lines),
    str([x for x in _r6.lines if x.startswith("※")][:2]),
)

# ------------------------------- ⑨ 家族倾向：剥套装后缀 + 过滤部件行
print("\n=== ⑨ 家族倾向（翁 Prime 0.7）===")


class _FakeClient6:
    _aliases = {}

    async def wm_items(self):
        return [
            {
                "url_name": "okina_prime_set",
                "zh": "翁 Prime 一套",
                "en": "Okina Prime Set",
                "tags": ["weapon", "melee", "prime", "set"],
            },
            {
                "url_name": "okina_prime_handle",
                "zh": "翁 Prime 握柄",
                "en": "Okina Prime Handle",
                "tags": ["weapon", "component", "prime"],
            },
            {
                "url_name": "okina_prime_blade",
                "zh": "翁 Prime 刀刃",
                "en": "Okina Prime Blade",
                "tags": ["weapon", "component", "prime"],
            },
        ]


_c6 = WarframeClient.__new__(WarframeClient)
_c6._aliases = _FakeClient6._aliases
_c6.wm_items = _FakeClient6().wm_items
_fam = asyncio.run(_c6.riven_family({"url_name": "okina", "zh": "翁", "en": "Okina"}))
check(
    "★ 翁 家族 = [翁 Prime 0.7]（套装后缀已剥、部件行已过滤）",
    _fam == [("翁 Prime", 0.7)],
    str(_fam),
)
_v, _k = asyncio.run(_c6.resolve_variant_disp("翁 Prime 一套"))
check(
    "变体倾向查询容忍「一套」后缀（找到 wiki 表的 okina prime）",
    _v == 0.7 and _k == "okina prime",
    f"{_v} / {_k}",
)

# --------------------------------------- ⑩ 战甲单字黑话（猫p/电p/沙p/鸟p）
print("\n=== ⑩ 战甲单字黑话与「单字键不劫持」守卫 ===")
_FRAME_KEYS = {
    "猫": "khora_prime_set",
    "电": "volt_prime_set",
    "沙": "inaros_prime_set",
    "鸟": "zephyr_prime_set",
    "冰": "frost_prime_set",
    "毒": "saryn_prime_set",
    "血": "garuda_prime_set",
    "花": "wisp_prime_set",
    "蝶": "titania_prime_set",
    "茶": "protea_prime_set",
    "鬼": "sevagoth_prime_set",
    "音": "banshee_prime_set",
    "磁": "mag_prime_set",
    "火": "ember_prime_set",
    "玻璃": "gara_prime_set",
    "高斯": "gauss_prime_set",
}
_WM_TABLE = load_aliases().get("wm_items", {})
for _k, _want in _FRAME_KEYS.items():
    check(f"词库单字键 {_k} → {_want}", _WM_TABLE.get(_k) == _want, str(_WM_TABLE.get(_k)))
check(
    "★ 用户点名的四个单字都在（猫/电/沙/鸟）",
    all(_WM_TABLE.get(k) for k in ("猫", "电", "沙", "鸟")),
)
# 2026-09-25 互联网核实（百度「相关搜索」= 社区真实输入）：
#   证实 电男p/冰男p/冰p/毒妈p/血妈/磁妹/沙甲/花甲p/跑男甲/鬼甲 在用；
#   百度侧「胖」多指宠物（莲花大胖/胖狗），但用户清单（战甲黑话.md）明确
#   肥/胖 → Grendel（胖P），且单字键有「本字/+p/+prime」守卫，故按清单收 胖。
check(
    "★ 「胖」按清单口径 → Grendel（胖P），非宠物",
    _WM_TABLE.get("胖") == "grendel_prime_set",
    str(_WM_TABLE.get("胖")),
)
# 用户清单核出的错位映射：奶妈=Trinity、奶爸=Oberon（外部核实：百度页 Oberon+奶爸配卡）
check(
    "★ 奶妈 → Trinity（原错指 Wisp，清单+外部核实）",
    _WM_TABLE.get("奶妈") == "trinity_prime_set",
    str(_WM_TABLE.get("奶妈")),
)
check(
    "★ 奶爸 → Oberon（原错指 Trinity）",
    _WM_TABLE.get("奶爸") == "oberon_prime_set" and _WM_TABLE.get("奶爸p") == "oberon_prime_set",
    f"{_WM_TABLE.get('奶爸')} / {_WM_TABLE.get('奶爸p')}",
)
check(
    "清单单字补齐（奶/摸/水/肥/枪/基/明/僧/盾/丑/高/船/骨）",
    all(
        _WM_TABLE.get(k)
        for k in ("奶", "摸", "水", "肥", "枪", "基", "明", "僧", "盾", "丑", "高", "船", "骨")
    ),
)

_ITEMS_FRAMES = [
    {
        "url_name": v,
        "zh": f"{k} Prime 一套",
        "en": f"{k}Prime Set",
        "tags": ["warframe", "prime", "set"],
    }
    for k, v in _FRAME_KEYS.items()
]
_c7 = WarframeClient.__new__(WarframeClient)
_c7._aliases = load_aliases()  # 完整别名结构（含 wm_items 内层表）


async def _items_frames():
    return [dict(x) for x in _ITEMS_FRAMES]


_c7.wm_items = _items_frames
for _q, _want in (
    ("猫", "khora_prime_set"),
    ("猫p", "khora_prime_set"),
    ("电p", "volt_prime_set"),
    ("沙p", "inaros_prime_set"),
    ("鸟p", "zephyr_prime_set"),
    ("猫p".replace("p", " prime"), "khora_prime_set"),
    # 2026-09-25 用户口径：玻璃p（Gara Prime）、蜘蛛p（Khora Prime）
    ("玻璃p", "gara_prime_set"),
    ("蜘蛛p", "khora_prime_set"),
    ("蜘蛛甲", "khora_prime_set"),
    ("跑男p", "gauss_prime_set"),
):
    _hit = asyncio.run(_c7.resolve_wm_item(_q))
    check(
        f"★ {_q} → {_want}",
        (_hit or {}).get("url_name") == _want,
        str((_hit or {}).get("url_name")),
    )

# 守卫：单字键只认「本字 / +p / +prime」——长查询不得被劫持成战甲
for _q in ("电击伤害", "猫头鹰", "冰霜伤害"):
    _hit = asyncio.run(_c7.resolve_wm_item(_q))
    check(
        f"★ 守卫：{_q} 不被劫持成战甲",
        (_hit or {}).get("url_name") not in set(_FRAME_KEYS.values()),
        str((_hit or {}).get("url_name")),
    )
# 反向守卫：多字黑话（冰男/猫刀）不受守卫影响
_hit = asyncio.run(_c7.resolve_wm_item("冰男p"))
check(
    "多字黑话「冰男p」仍正常（守卫只约束单字键）",
    (_hit or {}).get("url_name") == "frost_prime_set",
    str((_hit or {}).get("url_name")),
)

# ----------------------- ⑪ wm 品级 / 遗物精炼 / 墨染（解析早有、过滤刚接）
print("\n=== ⑪ wm 品级 / 精炼 / 墨染过滤 ===")
from core.parser import parse_wm  # noqa: E402

_q1 = parse_wm(["后纪", "A1", "光辉"])
check(
    "解析：光辉 → subtype radiant + 回显原词",
    _q1.refinement == "radiant" and _q1.refinement_word == "光辉",
    f"{_q1.refinement} / {_q1.refinement_word}",
)
# ★ 2026-09-25 总任务会话复核报障：帮助卡/KB 用官方简中「无瑕」，词典原先只收「无暇」
#   → 照帮助卡输入时该词不被消费、残留在物品名里且精炼过滤静默失效。两种写法都要收。
for _w in ("无瑕", "无暇"):
    _qw = parse_wm(["axi_c12_relic", _w])
    check(
        f"★ 精炼档「{_w}」→ flawless 且不残留在物品名里",
        _qw.refinement == "flawless" and _qw.item == "axi_c12_relic" and _qw.refinement_word == _w,
        f"{_qw.refinement} / {_qw.item!r}",
    )
_q2 = parse_wm(["生命力", "墨染"])
check("解析：墨染 → q.moran", _q2.moran is True)
_q3 = parse_wm(["充沛", "满级"])
check(
    "解析：满级 → -1（由物品实际 maxRank 决定具体级数）",
    _q3.rank == -1 and _q3.rank_word == "满级",
    str(_q3.rank),
)

_RF = plugin.WarframeSDJK._resolve_rank_filter
_arc = {"url_name": "arcane_energize", "tags": ["legendary", "arcane_enhancement"], "max_rank": 5}
_mod5 = {"url_name": "flow", "tags": ["mod", "warframe"], "max_rank": 5}
_mod10 = {"url_name": "vitality", "tags": ["mod", "warframe"], "max_rank": 10}
_relic = {"url_name": "axi_c12_relic", "tags": ["relic"], "max_rank": None}
check(
    "★ 赋能满级 = 5（不是 10）",
    _RF(-1, "满级", _arc) == (5, "只列满级（5级）的单"),
    str(_RF(-1, "满级", _arc)),
)
check(
    "★ 5 级 MOD 满级 = 5（川流不息类）", _RF(-1, "满级", _mod5)[0] == 5, str(_RF(-1, "满级", _mod5))
)
check("10 级 MOD 满级 = 10（生命力类）", _RF(-1, "满级", _mod10)[0] == 10)
check(
    "maxRank 缺失的 MOD 回落 10（保守兜底）",
    _RF(-1, "满级", {"tags": ["mod"], "max_rank": None})[0] == 10,
)
check(
    "★ 遗物没有品级 → 忽略并说明（不静默返回空表）",
    _RF(-1, "满级", _relic)[0] is None and "已忽略" in _RF(-1, "满级", _relic)[1],
    str(_RF(-1, "满级", _relic)),
)
# ★ 2026-09-25 二修（总任务会话自检要求）：满级来源要数据驱动 —— WM 有 44 个 MOD
#   条目没给 maxRank（如 intruder，上限 3），而 intruder 这类挂单根本没有级数。
_no_rank_orders = [
    {
        "order_type": "sell",
        "platinum": 5,
        "quantity": 1,
        "mod_rank": None,
        "user": {"status": "ingame", "ingame_name": "X"},
    }
]
_ranked_orders = [
    {
        "order_type": "sell",
        "platinum": 5,
        "quantity": 1,
        "mod_rank": 3,
        "user": {"status": "ingame", "ingame_name": "Y"},
    },
    {
        "order_type": "sell",
        "platinum": 3,
        "quantity": 1,
        "mod_rank": 0,
        "user": {"status": "ingame", "ingame_name": "Z"},
    },
]
check(
    "★ maxRank 缺失但有挂单级数 → 取挂单最高级并注明来源",
    _RF(-1, "满级", {"tags": ["mod"], "max_rank": None}, _ranked_orders)
    == (3, "只列满级（按挂单最高 3 级）"),
    str(_RF(-1, "满级", {"tags": ["mod"], "max_rank": None}, _ranked_orders)),
)
check(
    "★ 挂单完全没有级数（intruder 类）→ 忽略并说明，不静默空表",
    _RF(-1, "满级", {"tags": ["mod"], "max_rank": None}, _no_rank_orders)[0] is None,
    str(_RF(-1, "满级", {"tags": ["mod"], "max_rank": None}, _no_rank_orders)),
)
check(
    "有 maxRank 时仍优先用物品满级（不受挂单最高级影响）",
    _RF(-1, "满级", _mod5, _ranked_orders)[0] == 5,
    str(_RF(-1, "满级", _mod5, _ranked_orders)),
)
check("显式 N 级照样过滤", _RF(3, "3级", _arc) == (3, "只列 3 级的单"), str(_RF(3, "3级", _arc)))
check("没有品级词时不加提示", _RF(None, "", _arc) == (None, ""))

_orders = [
    {
        "order_type": "sell",
        "platinum": 20,
        "quantity": 1,
        "mod_rank": None,
        "subtype": "radiant",
        "user": {"status": "ingame", "ingame_name": "A"},
    },
    {
        "order_type": "sell",
        "platinum": 15,
        "quantity": 1,
        "mod_rank": None,
        "subtype": "intact",
        "user": {"status": "ingame", "ingame_name": "B"},
    },
    {
        "order_type": "sell",
        "platinum": 60,
        "quantity": 1,
        "mod_rank": 10,
        "subtype": "atragraph",
        "user": {"status": "online", "ingame_name": "C"},
    },
    {
        "order_type": "sell",
        "platinum": 30,
        "quantity": 1,
        "mod_rank": 10,
        "subtype": "regular",
        "user": {"status": "ingame", "ingame_name": "D"},
    },
]
_t_ref, _l_ref, _b_ref, _p_ref = F.fmt_wm_orders(
    "后纪 C12 遗物", _orders, refinement="radiant", notes=["只看「光辉」档遗物"]
)
check(
    "★ 精炼过滤只留光辉档",
    len(_p_ref) == 1 and _p_ref[0]["subtype"] == "radiant",
    str([o["subtype"] for o in _p_ref]),
)
check("行内标注精炼档（光辉）", any("光辉" in x for x in _l_ref), str(_l_ref[:2]))
check(
    "注释行带上调用方说明", any(x.startswith("※ 只看「光辉」档遗物") for x in _l_ref), str(_l_ref)
)
_t_m, _l_m, _b_m, _p_m = F.fmt_wm_orders("生命力", _orders, moran=True, notes=["只看墨染 Mod"])
check(
    "★ 墨染过滤只留 atragraph",
    len(_p_m) == 1 and _p_m[0]["subtype"] == "atragraph",
    str([o["subtype"] for o in _p_m]),
)
check("行内标注墨染", any("墨染" in x for x in _l_m), str(_l_m[:2]))
_rank_orders = [
    {
        "order_type": "sell",
        "platinum": 140,
        "quantity": 1,
        "mod_rank": 5,
        "user": {"status": "ingame", "ingame_name": "E"},
    },
    {
        "order_type": "sell",
        "platinum": 8,
        "quantity": 1,
        "mod_rank": 0,
        "user": {"status": "ingame", "ingame_name": "F"},
    },
]
_t_r, _l_r, _b_r, _p_r = F.fmt_wm_orders("赋能·充沛", _rank_orders, rank=5)
check(
    "★ 品级过滤只留 5 级（赋能满级），0 级被排除",
    len(_p_r) == 1 and _p_r[0]["mod_rank"] == 5,
    str([o["mod_rank"] for o in _p_r]),
)
check("行内标出级数（5级）", any("5级" in x for x in _l_r), str(_l_r[:2]))

# --------------------------- ⑪ 倾向卡家族：Vandal/Wraith 后缀变体 + 类别兜底
print("\n=== ⑪ 紫卡倾向 布莱顿：家族 4 成员 + 类别列不空 ===")
import json  # noqa: E402

_DISP = json.loads(
    (ROOT / "core" / "data" / "dispositions_rivenmirror.json").read_text(encoding="utf-8")
)["entries"]


class _FakeClient7:
    _aliases = {}

    async def wm_riven_weapons(self):
        return [dict(v, en=k) for k, v in _DISP.items()]

    def alias_lookup(self, q, kind):
        return ""


class _ParsedDispo:
    def __init__(self, q):
        self.content = []
        self.content_str = q
        self.preset, self.page, self.whisper = "", 1, False


_o7 = plugin.WarframeSDJK.__new__(plugin.WarframeSDJK)
_o7.client = _FakeClient7()
_r7 = asyncio.run(_o7._h_disposition(_ParsedDispo("布莱顿"), None, "pc"))
_row_v = next((x for x in _r7.lines if "布莱顿·破坏者" in x), "")
check(
    "★ 布莱顿家族列出 4 行（含此前漏列的布莱顿·破坏者）",
    len(_r7.lines) == 4 and bool(_row_v),
    "\n".join(_r7.lines),
)
check("★ 布莱顿·破坏者 倾向 1.30", "1.30" in _row_v, repr(_row_v))
check(
    "★ 类别列不空：从本体继承「步枪」（数据里该条 group/riven_type 为空）",
    _row_v.endswith("步枪"),
    repr(_row_v),
)
check(
    "重复的 MK1 行只列一次（4 行里没有空名行）",
    all("　" not in x.split("　")[0][2:] for x in _r7.lines),
    "\n".join(_r7.lines),
)

# ---------------------------------------------------------------------------
# ⑬ 官方名优先于别名词典（2026-10-01 报障：盗贼被认成盗贼双枪）
# ---------------------------------------------------------------------------
# 卡面原文「盗贼 Visi-toxican」⇒ OCR 武器名 = 盗贼（Furis 1.35）。旧解析链把
# **别名词典的包含匹配**排在官方名精确匹配之前：riven_items 的键「盗贼双枪」
# 满足 ``q in k``（"盗贼" ⊂ "盗贼双枪"）且长度差 2 ≤ 4 ⇒ 直接返回 afuris
# ⇒ 卡面按 1.45 算，四条数值全部压到区间下沿（暴伤区间 98.72-120.66 对 91.1…）
# ⇒ 误报「倾向调整前洗出的老卡」。同一类问题还有「猛毒」(komorex) 会被别名键
# 「猛毒镖枪」(zakti) 捞走。
print("\n=== ⑬ 官方名优先于别名（盗贼 ≠ 盗贼双枪）===")
_FAM = [
    {
        "url_name": "afuris",
        "zh": "盗贼双枪",
        "en": "Afuris",
        "disposition": 1.45,
        "riven_type": "pistol",
        "group": "secondary",
        "tags": [],
    },
    {
        "url_name": "furis",
        "zh": "盗贼",
        "en": "Furis",
        "disposition": 1.35,
        "riven_type": "pistol",
        "group": "secondary",
        "tags": [],
    },
    {
        "url_name": "afuris_prime",
        "zh": "盗贼双枪 Prime",
        "en": "Afuris Prime",
        "disposition": 1.1,
        "riven_type": "pistol",
        "group": "secondary",
        "tags": [],
    },
    {
        "url_name": "zakti",
        "zh": "猛毒镖枪",
        "en": "Zakti",
        "disposition": 1.1,
        "riven_type": "pistol",
        "group": "secondary",
        "tags": [],
    },
    {
        "url_name": "komorex",
        "zh": "猛毒",
        "en": "Komorex",
        "disposition": 0.85,
        "riven_type": "rifle",
        "group": "primary",
        "tags": [],
    },
]
_c6 = WarframeClient.__new__(WarframeClient)
_c6._aliases = {"riven_items": {"盗贼双枪": "afuris", "猛毒镖枪": "zakti", "捕月": "catchmoon"}}


async def _fam_weapons():
    return [dict(w) for w in _FAM]


_c6.wm_riven_weapons = _fam_weapons
_h6 = asyncio.run(_c6.resolve_riven_weapon("盗贼")) or {}
check(
    "★「盗贼」→ furis 1.35（旧实现被别名键「盗贼双枪」捞成 afuris 1.45）",
    _h6.get("url_name") == "furis",
    str(_h6),
)
check(
    "★ 解析出的倾向是 1.35（区间才不会被 1.45 压到 0%）",
    abs(float(_h6.get("disposition") or 0) - 1.35) < 1e-9,
    str(_h6),
)
_h6b = asyncio.run(_c6.resolve_riven_weapon("盗贼双枪")) or {}
check(
    "反向：真·盗贼双枪仍按 1.45 解析（未被这次重排改坏）",
    _h6b.get("url_name") == "afuris" and abs(float(_h6b.get("disposition") or 0) - 1.45) < 1e-9,
    str(_h6b),
)
_h6c = asyncio.run(_c6.resolve_riven_weapon("猛毒")) or {}
check(
    "★ 同类问题一并修掉：猛毒 → komorex（不是别名键「猛毒镖枪」的 zakti）",
    _h6c.get("url_name") == "komorex",
    str(_h6c),
)
check(
    "别名**精确键**仍生效（黑话/无中点写法照旧可用）",
    _c6.alias_lookup("捕月", "riven_items", exact=True) == "catchmoon",
)
check(
    "别名的**模糊**匹配仍可用（只是降级到链尾，未被删掉）",
    _c6._alias_fuzzy("双枪", "riven_items") == "afuris",
)


# 端到端：报障那张卡现在必须按 1.35 出区间
async def _no_family(_w):
    return []


async def _no_variant(_n):
    return (None, "")


_c6.riven_family = _no_family
_c6.resolve_variant_disp = _no_variant
_obj6 = plugin.WarframeSDJK.__new__(plugin.WarframeSDJK)
_obj6.client = _c6
_obj6.page_size = 12


async def _imgs6(_e):
    return ["data:image/jpeg;base64,x"]


_obj6._image_data_urls = _imgs6


async def _ext6(_u):
    return {
        "weapon": "盗贼 Visi-toxican",
        "positive": [["毒素伤害", 120.5], ["伤害", 299.9], ["多重射击", 148]],
        "negative": [["触发时间", 97.2]],
    }


_obj6._extract_riven_from_image = _ext6


class _Img6:
    pass


_Img6.__name__ = "Image"


class _Ev6:
    message_obj = types.SimpleNamespace(message=[_Img6()])
    unified_msg_origin = "group://alias_priority_test"


class _P6:
    content = []
    content_str = "紫卡分析"
    preset, page, whisper = "", 1, False


_r8 = asyncio.run(_obj6._h_riven_analysis(_P6(), _Ev6(), "pc"))
_b8 = "\n".join(_r8.lines)
check(
    "★ 端到端：报障卡按【盗贼】1.35 出区间（不再是盗贼双枪 1.38）",
    "【盗贼】倾向 1.35" in _b8 and "盗贼双枪" not in _b8,
    _b8[:200],
)
check(
    "★ 端到端：不再误报「倾向调整前洗出的老卡」",
    "对不上" not in _b8 and "老卡" not in _b8,
    _b8[:200],
)
check(
    "端到端：四条词条都进卡，且不再全落 0%",
    all(k in _b8 for k in ("+120.5% 毒伤", "+299.9% 基伤", "+148% 多重", "-97.2% 触时")),
    _b8[:300],
)

print()
if FAILED:
    print(f"✗ {len(FAILED)} 项失败：" + "、".join(FAILED))
    sys.exit(1)
print("✓ 紫卡市场 5 项修复全部通过")

# ---------------------------------------------------------------------------
# ⑫ 双向排序合并 + 价格本地过滤（2026-10-01，任务书 output/交接-ZCode-紫卡拍卖双向排序合并）
# ---------------------------------------------------------------------------
from core.api_client import WarframeAPIError as _WAE  # noqa: E402


class _FakeFetch2:
    """模拟 _fetch_json：price_asc 500 条低价 + price_desc 500 条高价，中间 50 条 id 重叠。

    用于验证 wm_riven_auctions 的「双查 → 按挂单 id 合并去重 → desc 失败降级」三行为。
    """

    def __init__(self, asc_auctions=None, desc_auctions=None, desc_error=False, stat_error=False):
        self._asc = asc_auctions or []
        self._desc = desc_auctions or []
        self._desc_error = desc_error
        self._stat_error = stat_error
        self.calls: list[dict] = []

    async def __call__(self, url, ttl=0, params=None, wm_rate_limit=False):
        from core.api_client import WarframeAPIError as _Err

        self.calls.append(dict(params or {}))
        sort = (params or {}).get("sort_by", "")
        if self._stat_error and sort == "price_desc":
            raise _Err("400 app.form.invalid")
        if sort == "price_desc":
            if self._desc_error:
                raise _Err("timeout on desc")
            return {"payload": {"auctions": self._desc}}
        return {"payload": {"auctions": self._asc}}


async def _run_merge(fake):
    import core.api_client as _api

    client = _api.WarframeClient.__new__(_api.WarframeClient)
    client.wm_base = "https://api.warframe.market/v1"
    client._fetch_json = fake
    return await client.wm_riven_auctions(
        "ocucor", "pc", positives=["multishot"], negatives=["toxin_damage"]
    )


# ① 双查合并去重：asc 有 id 1-3、desc 有 id 3-5 → 合并后 5 条不重复
_asc = [{"id": f"a{i}", "buyout_price": i * 100} for i in range(1, 4)]
_desc = [{"id": f"a{i}", "buyout_price": i * 100} for i in range(3, 6)]
_fake = _FakeFetch2(asc_auctions=_asc, desc_auctions=_desc)
_merged = asyncio.run(_run_merge(_fake))
check(
    "★ 双查合并：asc(3) + desc(3) 重叠 id=a3 → 去重后 5 条",
    len(_merged) == 5 and len({a["id"] for a in _merged}) == 5,
    f"{len(_merged)} 条, ids={[a['id'] for a in _merged]}",
)
check(
    "★ 两次请求 sort_by 分别是 price_asc / price_desc（TTL 缓存分键）",
    [c.get("sort_by") for c in _fake.calls] == ["price_asc", "price_desc"],
    str([c.get("sort_by") for c in _fake.calls]),
)

# ② desc 失败 → 降级为单方向结果（asc 3 条原样返回，不抛异常）
_fake2 = _FakeFetch2(asc_auctions=_asc, desc_error=True)
_degraded = asyncio.run(_run_merge(_fake2))
check(
    "★ desc 失败降级：返回 asc 3 条（指令不打挂）",
    len(_degraded) == 3 and all(a["id"].startswith("a") for a in _degraded),
    f"{len(_degraded)} 条",
)
check(
    "★ desc 失败后走词条降级路径再试 1 次（asc 1 + desc 2 = 3 次请求）",
    len(_fake2.calls) == 3
    and _fake2.calls[1].get("sort_by") == "price_desc"
    and _fake2.calls[1].get("positive_stats") is not None
    and _fake2.calls[2].get("positive_stats") is None,
    str(
        [
            (c.get("sort_by"), "stats" if c.get("positive_stats") else "no-stats")
            for c in _fake2.calls
        ]
    ),
)

# ③ asc 失败 + 无词条 → 抛异常（第一次失败原样抛，与修前行为一致）


class _FakeFetch3(_FakeFetch2):
    async def __call__(self, url, ttl=0, params=None, wm_rate_limit=False):
        from core.api_client import WarframeAPIError as _Err

        self.calls.append(dict(params or {}))
        raise _Err("WM down")


try:
    asyncio.run(_run_merge(_FakeFetch3()))
    _asc_error_raised = False
except _WAE:
    _asc_error_raised = True
check("★ asc 失败 + 无词条 → 抛 WarframeAPIError（与修前一致）", _asc_error_raised)

# ④ 价格本地过滤：_auction_match 按 buyout_price 与 q.max_price 判断
_q_price = parse_wr("多重 1000p".split())
_q_price.forbid_negative = False
_q_price.require_negative = False
_cheap = {
    "buyout_price": 600,
    "owner": {"status": "ingame"},
    "item": {"attributes": [{"url_name": "multishot", "value": 120, "positive": True}]},
}
_expensive = {
    "buyout_price": 4500,
    "owner": {"status": "ingame"},
    "item": {"attributes": [{"url_name": "multishot", "value": 120, "positive": True}]},
}
check(
    "★ 价格本地过滤：600p ≤ 1000p 保留",
    plugin.WarframeSDJK._auction_match(_cheap, _q_price, set(), set()) is True,
)
check(
    "★ 价格本地过滤：4500p > 1000p 拒绝（服务端忽略 price_min/max，本地必须拦）",
    plugin.WarframeSDJK._auction_match(_expensive, _q_price, set(), set()) is False,
)
_q_noprice = parse_wr("多重".split())
check(
    "★ 价格本地过滤：无价格条件不误杀",
    plugin.WarframeSDJK._auction_match(_expensive, _q_noprice, set(), set()) is True,
)


# ---------------------------------------------------------------------------
# ⑬ 家族判定（2026-10-02 起 = DE 官方 parentName 谱系）+ 词条参数逗号 AND
#    + require_negative（2026-10-01 追加批）
#    ⚠ 判据已从「主干名相等」再换为「官方谱系」（Dex 盗贼双枪 报障）：见 §⑭。
# ---------------------------------------------------------------------------
from core.api_client import WarframeClient as _WC  # noqa: E402

# A: _family_match 家族判定（8 条验收；换判据后结果保持不变）
_fm = _WC._family_match
for _bz, _be, _z, _e, _want, _label in [
    ("盗贼", "furis", "盗贼双枪 Prime", "Afuris Prime", False, "同前缀不同武器"),
    ("盗贼", "furis", "盗贼双枪", "afuris", False, "盗贼双枪≠盗贼的变体"),
    ("空刃", "nikana", "棱晶·空刃", "prisma nikana", True, "棱晶变体"),
    ("空刃", "nikana", "空刃 Prime", "nikana prime", True, "Prime变体"),
    ("空刃", "nikana", "空刃双刀", "dragon nikana", False, "空刃双刀≠空刃的变体"),
    ("翁", "okina", "翁 Prime 一套", "okina prime set", True, "翁 Prime（带一套后缀）"),
    ("布莱顿", "braton", "MK1-布莱顿", "mk1-braton", True, "MK1 算变体"),
    ("欧玛", "ohma", "棱晶·欧玛", "prisma ohma", True, "棱晶·欧玛"),
]:
    check(
        f"★ 家族判定（官方谱系）：{_label}",
        _fm(_bz, _be, _z, _e) is _want,
        f"{_bz}/{_z} -> {_fm(_bz, _be, _z, _e)}（期望 {_want}）",
    )

# ---------------------------------------------------------------------------
# ⑭ 家族判定改用 DE 官方 parentName 谱系（2026-10-02 用户报障）
#    症状：`紫卡倾向 盗贼` 多列 `Dex 盗贼双枪`（官方 data 里 Dex Furis 的
#    parentName 指向 **Afuris**，旧「中文主干相等 或 英文主干相等」双分支
#    把它同时并进了 Furis 族）。数据件 core/data/de/riven_families.json。
# ---------------------------------------------------------------------------
import json as _json2  # noqa: E402
from core import matching as _M2  # noqa: E402


def _W(en: str, zh: str, disp: float = 1.0) -> dict:
    return {"en": en, "zh": zh, "url_name": en.lower().replace(" ", "_"), "disposition": disp}


_AFURIS_POOL = [
    _W("Afuris", "盗贼双枪", 1.45),
    _W("Afuris Prime", "盗贼双枪 Prime", 1.10),
    _W("Dex Furis", "Dex 盗贼双枪", 1.39),
    _W("Furis", "盗贼", 1.35),
    _W("MK1-Furis", "MK1-盗贼", 1.40),
]
_fam = _M2.family_of(_AFURIS_POOL[0], _AFURIS_POOL)
check(
    "★ Afuris 族 ≡ {盗贼双枪, 盗贼双枪 Prime, Dex 盗贼双枪}",
    {e["zh"] for e in _fam} == {"盗贼双枪", "盗贼双枪 Prime", "Dex 盗贼双枪"},
    str([e["zh"] for e in _fam]),
)
_fam = _M2.family_of(_AFURIS_POOL[3], _AFURIS_POOL)
check(
    "★ Furis 族 ≡ {盗贼, MK1-盗贼}（Dex 盗贼双枪 不再误入）",
    {e["zh"] for e in _fam} == {"盗贼", "MK1-盗贼"},
    str([e["zh"] for e in _fam]),
)
check(
    "★ _family_match：Dex Furis 属 Afuris 族、不属 Furis 族",
    _fm("盗贼双枪", "Afuris", "Dex 盗贼双枪", "Dex Furis") is True
    and _fm("盗贼", "Furis", "Dex 盗贼双枪", "Dex Furis") is False,
)

_HEK_POOL = [_W("Hek", "海克"), _W("Kuva Hek", "赤毒·海克"), _W("Vaykor Hek", "勇气·海克")]
check(
    "★ Hek 族含 Kuva/Vaykor Hek（人工补丁族：DE 未设 parentName）",
    {e["en"] for e in _M2.family_of(_HEK_POOL[0], _HEK_POOL)} == {"Hek", "Kuva Hek", "Vaykor Hek"},
)
_DD_POOL = [_W("Dark Dagger", "暗黑匕首"), _W("Rakta Dark Dagger", "绯红·暗黑匕首")]
check(
    "★ Dark Dagger 族含 Rakta Dark Dagger（人工补丁族）",
    {e["en"] for e in _M2.family_of(_DD_POOL[0], _DD_POOL)} == {"Dark Dagger", "Rakta Dark Dagger"},
)
_LAC_POOL = [_W("Lacera", "悲痛之刃"), _W("Ceti Lacera", "天仓·悲痛之刃")]
check(
    "★ Lacera 族含 Ceti Lacera（漏列修复：ceti 不是变体词）",
    {e["en"] for e in _M2.family_of(_LAC_POOL[0], _LAC_POOL)} == {"Lacera", "Ceti Lacera"},
)
check(
    "★ 边界：Dakra Prime ≠ Dex Dakra（官方各成根，不许并）",
    _M2.family_key("Dakra Prime") != _M2.family_key("Dex Dakra"),
)
check("★ 边界：Bronco ≠ Akbronco", _M2.family_key("Bronco") != _M2.family_key("Akbronco"))
check(
    "★ 英文为空 ⇒ 不入家族（宁可少列）",
    _M2.family_key("") == "" and _fm("", "", "棱晶·欧玛", "Prisma Ohma") is False,
)

_FAMS = _json2.loads(
    (ROOT / "core" / "data" / "de" / "riven_families.json").read_text(encoding="utf-8")
)
_multi = sum(1 for v in _FAMS["families"].values() if len(v) > 1)
check(
    "★ 数据守卫：多成员族 == 208（2026-10-03 双模式并入 +18；换版须复核）",
    _multi == 208,
    str(_multi),
)
check(
    "★ 数据守卫：AkimboAutoPistols 恰为 3 件（本次报障族）",
    sorted(_FAMS["families"].get("AkimboAutoPistols", []))
    == ["Afuris", "Afuris Prime", "Dex Furis"],
    str(_FAMS["families"].get("AkimboAutoPistols")),
)
check(
    "★ 补丁守卫：人工补丁只含 Hek / DarkDagger 两族（红线：不许再加别名）",
    sorted(set(_FAMS["_patches"]["entries"].values())) == ["DarkDagger", "Hek"],
    str(sorted(set(_FAMS["_patches"]["entries"].values()))),
)

# B: 词条参数必须是逗号（AND），不是 list（OR 触顶 500）


class _ParamCapture(_FakeFetch2):
    """捕获 wm_riven_auctions 发出的 params 中 positive_stats 的形态。"""

    def __init__(self):
        super().__init__(asc_auctions=[], desc_auctions=[])
        self.pos_stats_forms: list = []

    async def __call__(self, url, ttl=0, params=None, wm_rate_limit=False):
        self.calls.append(dict(params or {}))
        ps = (params or {}).get("positive_stats")
        if ps is not None:
            self.pos_stats_forms.append(ps)
        return {"payload": {"auctions": []}}


_capture = _ParamCapture()
asyncio.run(_run_merge(_capture))
check(
    "★ 词条参数 = 字符串（非 list ⇒ httpx 不拆成重复参数/OR 触顶 500）",
    _capture.pos_stats_forms and all(isinstance(p, str) for p in _capture.pos_stats_forms),
    f"实际形态: {_capture.pos_stats_forms}",
)


# 多词条：必须拼逗号（AND），不能是 list
async def _run_multi(fake):
    import core.api_client as _api

    client = _api.WarframeClient.__new__(_api.WarframeClient)
    client.wm_base = "https://api.warframe.market/v1"
    client._fetch_json = fake
    return await client.wm_riven_auctions(
        "burston", "pc", positives=["multishot", "critical_chance", "toxin_damage"]
    )


_multi_cap = _ParamCapture()
asyncio.run(_run_multi(_multi_cap))
check(
    "★ 多词条 = 逗号拼接（AND 语义）",
    _multi_cap.pos_stats_forms
    and all(p == "multishot,critical_chance,toxin_damage" for p in _multi_cap.pos_stats_forms),
    f"实际形态: {_multi_cap.pos_stats_forms}",
)

# C: _split_negatives 结尾「负」落地 → require_negative

for _tok, _want_req in [("任意负", True), ("双暴负", True), ("负", True), ("带负", True)]:
    _q_test = parse_wr(["伯斯顿", _tok])
    check(
        f"★ 任意负落地：{_tok} → require_negative={_want_req}",
        _q_test.require_negative is _want_req,
        f"实际 {_q_test.require_negative}",
    )

# 「负变焦」不受影响：具体负面词条（不是 require_negative）
_q_vj = parse_wr(["伯斯顿", "负变焦"])
check(
    "★ 负变焦仍正常：negatives=['zoom'] 而非 require_negative",
    _q_vj.negatives == ["zoom"] and not _q_vj.require_negative,
    f"neg={_q_vj.negatives} req={_q_vj.require_negative}",
)

# B+C 联合：require_negative → negative_stats=has


class _NegCapture(_FakeFetch2):
    def __init__(self):
        super().__init__(asc_auctions=[], desc_auctions=[])
        self.neg_forms: list = []

    async def __call__(self, url, ttl=0, params=None, wm_rate_limit=False):
        self.calls.append(dict(params or {}))
        ns = (params or {}).get("negative_stats")
        if ns is not None:
            self.neg_forms.append(ns)
        return {"payload": {"auctions": []}}


async def _run_with_require_neg(fake):
    import core.api_client as _api

    client = _api.WarframeClient.__new__(_api.WarframeClient)
    client.wm_base = "https://api.warframe.market/v1"
    client._fetch_json = fake
    return await client.wm_riven_auctions(
        "burston", "pc", positives=["multishot"], require_negative=True
    )


_negcap = _NegCapture()
asyncio.run(_run_with_require_neg(_negcap))
check(
    "★ require_negative → negative_stats=has（服务端筛「带负词条」）",
    all(v == "has" for v in _negcap.neg_forms) and _negcap.neg_forms,
    f"实际: {_negcap.neg_forms}",
)


# ---------------------------------------------------------------------------
# ⑭ wr 完全命中优先排序（2026-10-01 B 口径：在线档 → 档内恰好 → 价格；非硬过滤）
# ---------------------------------------------------------------------------
_ps2 = {"multishot", "critical_damage"}


def _au2(aid, price, status, pos_slugs, neg_slugs=()):
    attrs = [{"url_name": s, "value": 100, "positive": True} for s in pos_slugs]
    attrs += [{"url_name": s, "value": 50, "positive": False} for s in neg_slugs]
    return {
        "id": aid,
        "buyout_price": price,
        "owner": {"status": status},
        "item": {"attributes": attrs},
    }


_a_sup_ig = _au2("sup_ig", 100, "ingame", list(_ps2 | {"cold_damage"}))
_a_ex_ig = _au2("ex_ig", 900, "ingame", list(_ps2))
_a_sup_on = _au2("sup_on", 200, "online", list(_ps2 | {"cold_damage"}))
_a_ex_off = _au2("ex_off", 50, "offline", list(_ps2))

# ① 同档内：恰好(900p) 压 超集(100p)——同档内完全命中优先
_t, _l, _ = F.fmt_wr_auctions("翁", [_a_sup_ig, _a_ex_ig], exact_ids={"ex_ig"})
check("★ B① 同档内恰好优先：900p 恰好压 100p 超集", _l[0].startswith("1. 一口价：900p"), _l[0])

# ② ★ 跨档（B 口径核心）：恰好离线、超集在线 ⇒ 超集在前（在线档第一优先）
_t, _l, _ = F.fmt_wr_auctions("翁", [_a_ex_off, _a_sup_on], exact_ids={"ex_off"})
check(
    "★ B② 跨档：在线超集(200p) 压 离线恰好(50p)——在线档仍是第一优先",
    _l[0].startswith("1. 一口价：200p"),
    _l[0],
)

# ③ 在线档次序 ingame → online → offline 不变
_a_e_on = _au2("e_on", 500, "online", list(_ps2))
_t, _l, _ = F.fmt_wr_auctions("翁", [_a_ex_off, _a_e_on, _a_sup_ig], exact_ids={"e_on", "ex_off"})
check(
    "★ B③ 在线档次序不变：ingame(超集) → online(恰好) → offline(恰好)",
    _l[0].startswith("1. 一口价：100p")
    and _l[2].startswith("2. 一口价：500p")
    and _l[4].startswith("3. 一口价：50p"),
    str(_l[:5]),
)

# ④ 同档同命中内价格升序（任务书原例：两条都恰好，500p 在线 / 300p 离线
#    ⇒ 500p 在前——档位压价格；若同档才轮到价格升序）
_a_e1 = _au2("e1", 500, "online", list(_ps2))
_a_e2 = _au2("e2", 300, "offline", list(_ps2))
_t, _l, _ = F.fmt_wr_auctions("翁", [_a_e1, _a_e2], exact_ids={"e1", "e2"})
check(
    "★ B④ 恰好内档位压价格（500p 在线在 300p 离线前）",
    _l[0].startswith("1. 一口价：500p") and _l[2].startswith("2. 一口价：300p"),
    str(_l[:3]),
)

# ⑤ 全部是超集 → exact_ids 为空集（handler 实际产物）⇒ 顺序与改动前逐条一致
_a_s1 = _au2("s1", 100, "ingame", list(_ps2 | {"cold_damage"}))
_a_s2 = _au2("s2", 200, "online", list(_ps2 | {"cold_damage"}))
_t1, _l1, _ = F.fmt_wr_auctions("翁", [_a_s1, _a_s2], exact_ids=set())
_t2, _l2, _ = F.fmt_wr_auctions("翁", [_a_s1, _a_s2])
check("★ B⑤ 全超集（exact_ids 空集）→ 顺序与无 exact_ids 逐条一致", _l1 == _l2)

# ⑥ 无指定词条 → exact_ids=None 退化
_t1, _l1, _ = F.fmt_wr_auctions("翁", [_a_s1, _a_s2], exact_ids=None)
_t2, _l2, _ = F.fmt_wr_auctions("翁", [_a_s1, _a_s2])
check("★ B⑥ 无词条 exact_ids=None → 顺序不变", _l1 == _l2)

# ⑦ presorted=True 完全不受新参数影响
_t1, _l1, _ = F.fmt_wr_auctions("翁", [_a_s2, _a_s1], presorted=True, exact_ids={"s1"})
check("★ B⑦ presorted=True 不受新参数影响", _l1[0].startswith("1. 一口价：200p"), _l1[0])

# ⑧ 任意负：正恰好 + 1 条负 ⇒ 判为恰好（不得因负词条无名判超集）
_q_neg = parse_wr(["多重", "任意负"])
_a_neg = _au2("n1", 300, "ingame", ["multishot"], ["cold_damage"])
_a_negless = _au2("n2", 300, "ingame", ["multishot"])
check(
    "★ B⑧ 任意负：1 条负即恰好；无负 = 超集",
    plugin.WarframeSDJK._is_exact_match(_a_neg, _q_neg, set(), {"multishot"}) is True
    and plugin.WarframeSDJK._is_exact_match(_a_negless, _q_neg, set(), {"multishot"}) is False,
)

# ⑨ 注脚文案：有词条时含「完全命中」、无词条时不含
_t_w, _l_w, _ = F.fmt_wr_auctions("翁", [_a_ex_ig], exact_ids={"ex_ig"})
_t_wo, _l_wo, _ = F.fmt_wr_auctions("翁", [_a_ex_ig])
check(
    "★ B⑨ 注脚：有词条含「完全命中词条优先」、无词条不含",
    any("完全命中词条优先" in x for x in _l_w) and not any("完全命中词条优先" in x for x in _l_wo),
)

# ---------------------------------------------------------------------------
# ⑮ 后坐力极性反转 e2e（2026-10-02 用户报障卡：四行全 `+`，其中
#    「+95.4% 武器后坐力」实为负面）—— 人工输入路径：4 正含 1 条反转词条
#    ⇒ 自动改判为 3 正 1 负，不再被词条数闸门拒收。
# ---------------------------------------------------------------------------
_obj9 = plugin.WarframeSDJK.__new__(plugin.WarframeSDJK)
_obj9.client = _c6  # 复用 §⑬ 的假客户端（盗贼 = Furis 1.35）
_obj9.page_size = 12
_obj9._event_has_image = lambda _e: False


class _P9:
    content = ["盗贼", "装填速度61.5", "多重射击146.7", "伤害274.5", "后坐力95.4"]
    content_str = "紫卡分析 盗贼 装填速度61.5 多重射击146.7 伤害274.5 后坐力95.4"
    preset, page, whisper = "", 1, False


class _Ev9:
    unified_msg_origin = "group://recoil_e2e"

    def get_sender_id(self) -> str:
        return "recoil"

    def get_sender_name(self) -> str:
        return "recoil"


_r9 = asyncio.run(_obj9._h_riven_analysis(_P9(), _Ev9(), "pc"))
_b9 = "\n".join(_r9.lines)
check(
    "★ 后坐力反转 e2e：不再报「词条数不对」（4 正含 1 反转 ⇒ 3 正 1 负）",
    "当前解析到" not in _b9 and "词条应为" not in _b9 and "+95.4% 后坐" in _b9,
    _b9[:240],
)
check(
    "★ 后坐力**保留卡面符号** + 负面标注（+95.4% 后坐（卡面+号·负面）+ 注脚）",
    "+95.4% 后坐（卡面+号·负面）" in _b9 and "是反转词条" in _b9,
    _b9[:300],
)
check(
    "★ 其余三行仍按正面且都命中区间（未被这次改动带偏）",
    all(k in _b9 for k in ("+61.5% 装填", "+146.7% 多重", "+274.5% 基伤"))
    and "无法算区间" not in _b9,
    _b9[:400],
)

# ---------------------------------------------------------------------------
# ⑯ 重复词条标注（2026-10-02 线上实证：`+44.9% ⚡电击伤害` 被模型两渠道都读成
#    「暴击伤害」⇒ 卡面上同一词条出现两次）—— 不静默：卡面明确标注请重发核对。
# ---------------------------------------------------------------------------
_obj10 = plugin.WarframeSDJK.__new__(plugin.WarframeSDJK)
_obj10.client = _c6
_obj10.page_size = 12
_obj10._event_has_image = lambda _e: False


class _P10:
    content = ["盗贼", "暴伤60.8", "暴伤110.1", "多重146.7", "负后坐力95.4"]
    content_str = "紫卡分析 盗贼 暴伤60.8 暴伤110.1 多重146.7 负后坐力95.4"
    preset, page, whisper = "", 1, False


_r10 = asyncio.run(_obj10._h_riven_analysis(_P10(), _Ev9(), "pc"))
_b10 = "\n".join(_r10.lines)
check(
    "★ 重复词条：同一词条出现两次 ⇒ 卡面标注「请重发核对」（不静默）",
    "同一词条出现两次" in _b10 and "请重发一次截图核对" in _b10,
    _b10[:300],
)


class _P10b:
    content = ["盗贼", "暴伤60.8", "多重146.7", "攻速45.8", "负后坐力95.4"]
    content_str = "紫卡分析 盗贼 暴伤60.8 多重146.7 攻速45.8 负后坐力95.4"
    preset, page, whisper = "", 1, False


_r10b = asyncio.run(_obj10._h_riven_analysis(_P10b(), _Ev9(), "pc"))
_b10b = "\n".join(_r10b.lines)
check("对照：无重复词条时不误报标注", "同一词条出现两次" not in _b10b, _b10b[:200])

# ---------------------------------------------------------------------------
# ⑰ 反转词条走图片路径（用户实际那张卡：来源＝图片识别·卡面逐行）
#    —— 行内保留卡面符号 + 负面标注 + 机理注脚，三条都要在。
# ---------------------------------------------------------------------------
_obj11 = plugin.WarframeSDJK.__new__(plugin.WarframeSDJK)
_obj11.client = _c6
_obj11.page_size = 12
_obj11._event_has_image = lambda _e: True


async def _imgs11(_e):
    return ["data:image/jpeg;base64,x"]


_obj11._image_data_urls = _imgs11


async def _ext11(_u):
    # 与线上同款窄读行（卡面 +95.4% 武器后坐力，模型原样照抄）
    return {
        "weapon": "盗贼",
        "lines": ["+61.5% 装填速度", "+146.7% 多重射击", "+274.5% 伤害", "+95.4% 武器后坐力"],
        "positive": [
            ["装填速度", 61.5],
            ["多重射击", 146.7],
            ["伤害", 274.5],
            ["武器后坐力", 95.4],
        ],
        "negative": [],
    }


_obj11._extract_riven_from_image = _ext11
_r11 = asyncio.run(_obj11._h_riven_analysis(_P6(), _Ev6(), "pc"))
_b11 = "\n".join(_r11.lines)
check(
    "★ 图片路径：卡面 +95.4% 后坐力 ⇒ 行内符号与卡面一致、标注负面",
    "+95.4% 后坐（卡面+号·负面）" in _b11,
    _b11[:300],
)
check(
    "★ 图片路径：反转词条机理注脚必须在（此前只有人工路径有）",
    "是反转词条" in _b11 and "增加后坐力 = 负面" in _b11,
    _b11[:300],
)
check(
    "图片路径：区间仍按负面档算（幅度位列，说明按负面系数计算）",
    "幅度位" in _b11 and "幅度越大越友好" in _b11,
    _b11[:300],
)

# ---------------------------------------------------------------------------
# ⑱ 用户实卡（翁 Locti-acrium，2026-10-02）：手动输入路径也要认 DE 卡面原文
#    「-60.4% 的几率来获得连击数」（9 字 —— 旧限 8 字会整条落进武器名）。
# ---------------------------------------------------------------------------
_OKINA = [
    {
        "url_name": "okina",
        "zh": "翁",
        "en": "Okina",
        "disposition": 0.7,
        "riven_type": "melee",
        "group": "melee",
        "tags": [],
    }
]
_c18 = WarframeClient.__new__(WarframeClient)
_c18._aliases = {}


async def _w18():
    return [dict(w) for w in _OKINA]


async def _fam18(_w):
    return []


async def _var18(_n):
    return (None, "")


_c18.wm_riven_weapons = _w18
_c18.riven_family = _fam18
_c18.resolve_variant_disp = _var18
_obj18 = plugin.WarframeSDJK.__new__(plugin.WarframeSDJK)
_obj18.client = _c18
_obj18.page_size = 12
_obj18._event_has_image = lambda _e: False


class _P18:
    content = ["翁", "初始连击14.8", "攻击范围1.3", "暴击伤害54.8", "负的几率来获得连击数60.4"]
    content_str = "紫卡分析 翁 初始连击14.8 攻击范围1.3 暴击伤害54.8 负的几率来获得连击数60.4"
    preset, page, whisper = "", 1, False


_r18 = asyncio.run(_obj18._h_riven_analysis(_P18(), _Ev9(), "pc"))
_b18 = "\n".join(_r18.lines)
check(
    "★ 实卡手动输入：9 字 DE 原文词条不丢（不再落进武器名）",
    "未找到紫卡武器" not in _b18 and "词条应为" not in _b18,
    _b18[:240],
)
check(
    "★ 实卡四行进卡：初始连击 / 范围 / 暴伤 / 连击获取（负）",
    all(k in _b18 for k in ("+14.8 初始连击", "+1.3m 范围", "+54.8% 暴伤", "-60.4% 连击获取")),
    _b18[:400],
)
check(
    "★ 连击获取按负档算出区间（104.85 基值生效，不再「无官方基值」）",
    "49.54%-60.55%" in _b18 and "无官方基值" not in _b18,
    _b18[:400],
)

# ---------------------------------------------------------------------------
# ⑲ 变体倾向改走本地表（2026-10-03 交办）：家族候选必须含玄骸变体 + 名称陷阱
#   旧实现遍历 WM 物品表找变体名，而 WM 全量 3892 条里 **0 件玄骸武器**
#   （赤毒/信条/终幕本体不可交易）⇒ 玄骸变体永远列不出（「鳄神」卡由此误判）。
# ---------------------------------------------------------------------------
_c19 = WarframeClient.__new__(WarframeClient)
_c19._aliases = {}


def _fam19(en):
    return asyncio.run(_c19.riven_family({"en": en}))


_XUANHAI = [
    ("Sobek", 0.8),
    ("Nukor", 0.5),
    ("Ogris", 0.7),
    ("Zarr", 0.7),
    ("Tonkor", 0.9),
    ("Kohm", 0.9),
    ("Kraken", 1.1),
    ("Hind", 1.1),
    ("Drakgoon", 1.15),
    ("Brakk", 0.95),
    ("Seer", 1.1),
    ("Quartakk", 1.1),
    ("Ferrox", 1.15),
    ("Hema", 1.0),
    ("Tysis", 1.0),
    ("Pox", 1.05),
    ("Bubonico", 0.6),
    ("Caustacyst", 0.85),
    ("Hirudo", 0.75),
    ("Catabolyst", 0.7),
    ("Glaxion", 0.65),
    ("Quanta", 0.85),
    ("Sporothrix", 0.95),
    ("Plinx", 0.8),
    ("Grattler", 0.7),
    ("Pathocyst", 0.65),
    ("Ghoulsaw", 0.8),
    ("Mire", 0.75),
    ("Arca Plasmor", 0.6),
    ("Cycron", 0.7),
    ("Synapse", 0.9),
]
# ★ 2026-10-03 wiki 倾向页快照刷新（09-13 → 10-02），7 处玄骸倾向更新：
#   Hema 0.95→1.0 / Pox 1.0→1.05 / Bubonico 0.55→0.6 / Catabolyst 0.65→0.7 /
#   Quanta 0.75→0.85 / Ghoulsaw 0.7→0.8 / Arca Plasmor 0.55→0.6
_miss19 = [(b, w) for b, w in _XUANHAI if not any(abs(v - w) < 1e-9 for _n, v in _fam19(b))]
check(
    f"★ 玄骸族抽查 {len(_XUANHAI)} 族：家族候选都列得出（缺 {len(_miss19)}）",
    not _miss19,
    str(_miss19[:4]),
)
check(
    "★ 报障族原样：Sobek 族 = [('赤毒·鳄神', 0.8)]",
    _fam19("Sobek") == [("赤毒·鳄神", 0.8)],
    str(_fam19("Sobek")),
)
check(
    "玄骸单成员族（无本体可对照）：Kuva Bramma 家族为空但不报错",
    _fam19("Kuva Bramma") == [],
    str(_fam19("Kuva Bramma")),
)

# 名称陷阱（清单 §四）：riven_family() 输出层同样要守住
check(
    "★ 名称陷阱：Afuris 族（除自身）= {盗贼双枪 Prime, Dex 盗贼双枪}",
    {n for n, _ in _fam19("Afuris")} == {"盗贼双枪 Prime", "Dex 盗贼双枪"},
    str(_fam19("Afuris")),
)
check(
    "★ 名称陷阱：Furis 族（除自身）= {MK1-盗贼}（不含 Dex 盗贼双枪）",
    {n for n, _ in _fam19("Furis")} == {"MK1-盗贼"},
    str(_fam19("Furis")),
)
check(
    "★ 名称陷阱：Lacera 族含 天仓·悲痛之刃",
    "天仓·悲痛之刃" in {n for n, _ in _fam19("Lacera")},
    str(_fam19("Lacera")),
)
check(
    "★ 名称陷阱：Pangolin Sword 族含 鲮鲤剑 Prime",
    "鲮鲤剑 Prime" in {n for n, _ in _fam19("Pangolin Sword")},
    str(_fam19("Pangolin Sword")),
)
check(
    "★ 名称陷阱：Dual Decurion 族含 棱晶·什长双枪",
    "棱晶·什长双枪" in {n for n, _ in _fam19("Dual Decurion")},
    str(_fam19("Dual Decurion")),
)
check(
    "★ Dakra 边界：Dakra Prime 与 Dex Dakra 各自独立（家族互不含对方、倾向 1.10/1.35）",
    _fam19("Dakra Prime") == [] and _fam19("Dex Dakra") == [],
    f"{_fam19('Dakra Prime')} / {_fam19('Dex Dakra')}",
)

# ★ 2026-10-03：Coda Bubonico 中英对照 —— 原为运行时手工补丁（_LOCAL_NAME_PATCH），
#   同日构建脚本补收 DE 官方家族表（riven_families.json 819 件）后，桥上
#   de/name_en_zh.json 直接命中 ⇒ 补丁已删（单一真源：构建期表，运行期只读）。
from core import api_client as _AC  # noqa: E402

check(
    "★ 显示名单一真源：_LOCAL_NAME_PATCH 已删（桥接接管，不许回流为手工补丁）",
    not hasattr(_AC, "_LOCAL_NAME_PATCH"),
    "仍有运行时手工补丁",
)
check(
    "★ 桥接出中文：Coda Bubonico ↔ 终幕·横痃重炮（双向可查）",
    _AC._zh_name_of_en("Coda Bubonico") == "终幕·横痃重炮"
    and _AC._en_name_of_zh("终幕·横痃重炮") != "",
    f"{_AC._zh_name_of_en('Coda Bubonico')!r} / {_AC._en_name_of_zh('终幕·横痃重炮')!r}",
)
check(
    "★ 桥接出中文：原本缺口的玄骸/Zaw 件抽查（Kuva Ghoulsaw / Tenet Quanta / Balla / Cyath / Dehtat / Dokrahm）",
    [
        _AC._zh_name_of_en(e)
        for e in ("Kuva Ghoulsaw", "Tenet Quanta", "Balla", "Cyath", "Dehtat", "Dokrahm")
    ]
    == ["赤毒·尸鬼电锯", "信条·量子切割器", "宝拉", "西亚什", "德塔特", "多克拉姆"],
    str(
        [
            _AC._zh_name_of_en(e)
            for e in ("Kuva Ghoulsaw", "Tenet Quanta", "Balla", "Cyath", "Dehtat", "Dokrahm")
        ]
    ),
)
check(
    "家族显示名：横痃重炮 家族 → 终幕·横痃重炮（中文，不再是英文名；2026-10-03 wiki 刷新 0.55→0.6）",
    _fam19("Bubonico") == [("终幕·横痃重炮", 0.6)],
    str(_fam19("Bubonico")),
)

# ---------------------------------------------------------------------------
# ⑳ 「鳄神」报障卡 e2e（2026-10-03）：反推 0.8 = 赤毒·鳄神 ⇒ 报变体，不是「老卡」
# ---------------------------------------------------------------------------
_SOBEK = [
    {
        "url_name": "sobek",
        "zh": "鳄神",
        "en": "Sobek",
        "disposition": 1.33,
        "riven_type": "shotgun",
        "group": "primary",
        "tags": [],
    },
    {
        "url_name": "kuva_sobek",
        "zh": "赤毒·鳄神",
        "en": "Kuva Sobek",
        "disposition": 0.8,
        "riven_type": "shotgun",
        "group": "primary",
        "tags": [],
    },
]
_c20 = WarframeClient.__new__(WarframeClient)
_c20._aliases = {}


async def _w20():
    return [dict(w) for w in _SOBEK]


_c20.wm_riven_weapons = _w20


class _P20:
    preset, page, whisper = "", 1, False

    def __init__(self, tokens):
        self.content = tokens
        self.content_str = "紫卡分析 " + " ".join(tokens)


_P20_A = _P20(["鳄神", "射速72.9", "多重95.8", "触发71.9", "负装填29.3"])
_P20_B = _P20(["赤毒·鳄神", "射速72.9", "多重95.8", "触发71.9", "负装填29.3"])


def _run20(tokens):
    obj = plugin.WarframeSDJK.__new__(plugin.WarframeSDJK)
    obj.client = _c20
    obj.page_size = 12
    obj._event_has_image = lambda _e: False
    reply = asyncio.run(obj._h_riven_analysis(tokens, _Ev9(), "pc"))
    return "\n".join(getattr(reply, "lines", []) or []) or reply.raw_text or ""


_b20 = _run20(_P20_A)
check(
    "★ 鳄神卡：报告为**变体**（含「赤毒·鳄神」）且不含「老卡」",
    "赤毒·鳄神" in _b20 and "老卡" not in _b20,
    _b20[:300],
)
check(
    "★ 鳄神卡：按 0.8 计算，四条区间全中（射速/多重/触发/负装填）",
    all(k in _b20 for k in ("60.75%-74.25%", "80.8%-98.75%", "27%-33%")),
    _b20[:400],
)
check(
    "鳄神卡：术语用「射速」（远程）而非近战「攻速」",
    "射速" in _b20 and "攻速" not in _b20,
    _b20[:200],
)
_b20b = _run20(_P20_B)
check(
    "★ 手输「赤毒·鳄神」：直接按 0.8 出卡（【赤毒·鳄神】倾向 0.8）",
    "【赤毒·鳄神】倾向 0.8" in _b20b,
    _b20b[:200],
)


class _NoFamClient:
    """家族列不出（模拟旧状态）：仍须走「老卡」兜底，不误报变体。"""

    def __init__(self):
        self._aliases = {}

    async def wm_riven_weapons(self):
        return [dict(w) for w in _SOBEK]

    async def resolve_riven_weapon(self, q):
        for w in _SOBEK:
            if q.strip() in (w["zh"], w["en"]):
                return dict(w)
        return None

    async def riven_family(self, _w):
        return []

    async def resolve_variant_disp(self, _n):
        return (None, "")


obj21 = plugin.WarframeSDJK.__new__(plugin.WarframeSDJK)
obj21.client = _NoFamClient()
obj21.page_size = 12
obj21._event_has_image = lambda _e: False
_r21 = asyncio.run(obj21._h_riven_analysis(_P20_A, _Ev9(), "pc"))
_b21 = "\n".join(_r21.lines)
check("对照：家族列不出时仍回「老卡」兜底（兜底逻辑未被改坏）", "老卡" in _b21, _b21[:240])

# ---------------------------------------------------------------------------
# ㉑ 变体示例文案（2026-10-03）：不硬编码「赤毒」、前缀不得重复
#    场景数字刻意取「射速属 1.33 档、多重/触发属 0.8 档」的混合值（rifle 基值下
#    对谁都不吻合且反推区间宽）⇒ 恰落「本体不吻合、家族也解释不了」兜底分支。
#    fixture 用 rifle 型是校准过的：shotgun 基值下同数字会可行/唯一吻合，落不进该分支。
# ---------------------------------------------------------------------------
_RF21 = [
    {
        "url_name": "sobek",
        "zh": "鳄神",
        "en": "Sobek",
        "disposition": 1.33,
        "riven_type": "rifle",
        "group": "primary",
        "tags": [],
    },
    {
        "url_name": "kuva_sobek",
        "zh": "赤毒·鳄神",
        "en": "Kuva Sobek",
        "disposition": 0.8,
        "riven_type": "rifle",
        "group": "primary",
        "tags": [],
    },
    {
        "url_name": "tenet_tetra",
        "zh": "信条·四核巨枪",
        "en": "Tenet Tetra",
        "disposition": 1.15,
        "riven_type": "rifle",
        "group": "primary",
        "tags": [],
    },
    {
        "url_name": "coda_mire",
        "zh": "终幕·米尔",
        "en": "Coda Mire",
        "disposition": 1.35,
        "riven_type": "rifle",
        "group": "primary",
        "tags": [],
    },
]
_MIX21 = ["射速72.9", "多重95.8", "触发71.9", "负装填29.3"]


class _NoFamRClient:
    """无家族、无 suggest（模拟家族表拿不到）：_cands 必为空 ⇒ 走示例兜底链。"""

    def __init__(self):
        self._aliases = {}

    async def wm_riven_weapons(self):
        return [dict(r) for r in _RF21]

    async def resolve_riven_weapon(self, q):
        for r in _RF21:
            if q.strip() in (r["zh"], r["en"]):
                return dict(r)
        return None

    async def riven_family(self, _w):
        return []

    async def resolve_variant_disp(self, _n):
        return (None, "")


async def _w21():
    return [dict(r) for r in _RF21]


def _run21(client, tokens):
    obj = plugin.WarframeSDJK.__new__(plugin.WarframeSDJK)
    obj.client = client
    obj.page_size = 12
    obj._event_has_image = lambda _e: False
    reply = asyncio.run(obj._h_riven_analysis(_P20(tokens), _Ev9(), "pc"))
    return "\n".join(getattr(reply, "lines", []) or []) or reply.raw_text or ""


_nf21 = _NoFamRClient()
_b21a = _run21(_nf21, ["赤毒·鳄神"] + _MIX21)
check(
    "㉑① 手输「赤毒·鳄神」无候选：示例原样（紫卡分析 赤毒·鳄神），无「赤毒赤毒」",
    "（例：紫卡分析 赤毒·鳄神 [截图]）" in _b21a and "赤毒赤毒" not in _b21a,
    _b21a[:240],
)
_b21b = _run21(_nf21, ["鳄神"] + _MIX21)
check(
    "㉑② 手输「鳄神」无候选：保持旧行为（紫卡分析 赤毒鳄神）",
    "（例：紫卡分析 赤毒鳄神 [截图]）" in _b21b,
    _b21b[:240],
)
_rc21 = WarframeClient.__new__(WarframeClient)
_rc21._aliases = {}
_rc21.wm_riven_weapons = _w21
_b21c = _run21(_rc21, ["鳄神"] + _MIX21)
check(
    "㉑③ 有家族候选：示例用候选名（紫卡分析 赤毒·鳄神）并附候选",
    "（例：紫卡分析 赤毒·鳄神 [截图]）" in _b21c and "候选：赤毒·鳄神" in _b21c,
    _b21c[:240],
)
_b21d = _run21(_nf21, ["信条·四核巨枪"] + _MIX21)
check(
    "㉑④ 信条武器：不硬套「赤毒」（示例 = 本体名 信条·四核巨枪）",
    "（例：紫卡分析 信条·四核巨枪 [截图]）" in _b21d and "赤毒信条" not in _b21d,
    _b21d[:240],
)
_b21e = _run21(_nf21, ["终幕·米尔"] + _MIX21)
check(
    "㉑④b 终幕武器：示例 = 本体名 终幕·米尔，无「终幕终幕」",
    "（例：紫卡分析 终幕·米尔 [截图]）" in _b21e and "终幕终幕" not in _b21e,
    _b21e[:240],
)
check(
    "㉑⑤ 文案为「疑似变体卡（赤毒 / 信条 / 终幕 等）」且全场景无重复前缀",
    all("疑似变体卡（赤毒 / 信条 / 终幕 等）" in t for t in (_b21a, _b21b, _b21c, _b21d, _b21e))
    and not any(
        x in t
        for t in (_b21a, _b21b, _b21c, _b21d, _b21e)
        for x in ("赤毒赤毒", "信条信条", "终幕终幕")
    ),
    "",
)

# ---------------------------------------------------------------------------
# ㉒ 组合枪双模式 + 最近邻判据（2026-10-03 交办，取证见
#    output/取证-ZCode-组合枪倾向与判据定标-20261003.md）
#    ① 家族表：双模式括号键与基名同根（主武器腔体不在 DE 导出，来源 wiki 键）；
#    ② 判据：严格区间 → 最近邻（score=max|v/(基值×D×系数)−1|）→ 阈值 0.15，
#      残差必须印在卡面；>0.15 保留「老卡」并附候选残差。
# ---------------------------------------------------------------------------
_rows22 = [
    {
        "url_name": "catchmoon",
        "zh": "捕月",
        "en": "Catchmoon",
        "disposition": 0.75,
        "riven_type": "kitgun",
        "group": "kitgun",
        "tags": [],
    },
    {
        "url_name": "catchmoon_(primary)",
        "zh": "捕月（主要）",
        "en": "Catchmoon (Primary)",
        "disposition": 1.1,
        "riven_type": "",
        "group": "",
        "tags": [],
    },
    {
        "url_name": "catchmoon_(secondary)",
        "zh": "捕月（次要）",
        "en": "Catchmoon (Secondary)",
        "disposition": 0.75,
        "riven_type": "",
        "group": "",
        "tags": [],
    },
    {
        "url_name": "tombfinger",
        "zh": "墓指",
        "en": "Tombfinger",
        "disposition": 0.9,
        "riven_type": "kitgun",
        "group": "kitgun",
        "tags": [],
    },
    {
        "url_name": "tombfinger_(primary)",
        "zh": "墓指（主要）",
        "en": "Tombfinger (Primary)",
        "disposition": 1.0,
        "riven_type": "",
        "group": "",
        "tags": [],
    },
]


class _KitClient:
    """WM 紫卡表回放：捕月 三行（plain + 拆分行，倾向为 wiki 覆盖后的值）。"""

    def __init__(self):
        self._aliases = {}

    async def wm_riven_weapons(self):
        return [dict(r) for r in _rows22]

    async def resolve_riven_weapon(self, q):
        for r in _rows22:
            if q.strip() in (r["zh"], r["en"]):
                return dict(r)
        return None


async def _w22():
    return [dict(r) for r in _rows22]


_kc22 = _KitClient()
_c22 = WarframeClient.__new__(WarframeClient)
_c22._aliases = {}
_f22 = asyncio.run(_c22.riven_family({"en": "Catchmoon"}))
check(
    "㉒① 捕月家族候选 = [捕月（次要）0.75, 捕月（主要）1.1]（双模式成对列出）",
    _f22 == [("捕月（次要）", 0.75), ("捕月（主要）", 1.1)],
    str(_f22),
)
_f22b = asyncio.run(_c22.riven_family({"en": "Sporelacer"}))
check(
    "㉒① 孢射家族候选 = [（次要）0.6,（主要）1.15]",
    _f22b == [("孢射（次要）", 0.6), ("孢射（主要）", 1.15)],
    str(_f22b),
)
_f22c = asyncio.run(_c22.riven_family({"en": "Vinquibus"}))
check(
    "㉒① 制胜者家族候选 = [（近战）0.7,（主要）0.7]（导出 (Melee) 并入基名根）",
    _f22c == [("制胜者（近战）", 0.7), ("制胜者（主要）", 0.7)],
    str(_f22c),
)
check(
    "㉒① 无后缀基名仍可查（向后兼容）：family_key 不变",
    _M2.family_key("Catchmoon")
    == _M2.family_key("Catchmoon (Primary)")
    == _M2.family_key("Catchmoon (Secondary)")
    and _M2.family_key("Sobek") == "DoubleBarrelShotgun",
)

_pos22 = [("crit_damage", 89.2), ("multishot", 110.3), ("electric_damage", 87.6)]
_neg22 = [("slash_damage", 88.1)]
_sc22 = RA.candidate_scores(_pos22, _neg22, "pistol", [("捕月", 0.75), ("捕月（主要）", 1.1)])
check(
    "㉒② candidate_scores：捕月（主要）1.1 score≈0.110、（次要/母）0.75 score≈0.41",
    abs(_sc22[0][1] - 1.1) < 1e-9
    and abs(_sc22[0][2] - 0.1099) < 5e-3
    and abs(_sc22[-1][1] - 0.75) < 1e-9
    and abs(_sc22[-1][2] - 0.41) < 5e-3,
    str(_sc22),
)

_rc22 = WarframeClient.__new__(WarframeClient)
_rc22._aliases = {}
_rc22.wm_riven_weapons = _w22
_b22a = _run21(_rc22, ["捕月", "暴伤89.2", "多重110.3", "电击87.6", "负切割88.1"])
check(
    "㉒③ 用户报障卡：判【捕月（主要）】1.1、卡面含「残差 11%」、不再「老卡」",
    "【捕月（主要）】倾向 1.1" in _b22a and "残差 11%" in _b22a and "老卡" not in _b22a,
    _b22a[:240],
)
_b22b = _run21(_rc22, ["捕月", "暴伤60.1", "多重84.2", "电击62.8", "负切割65.4"])
check(
    "㉒④ 次要卡（0.75 档数值）：判【捕月】0.75、家族提示列变体且无重发示例",
    "【捕月】倾向 0.75" in _b22b
    and "该武器家族有其它倾向" in _b22b
    and "卡面不显示变体/模式" in _b22b
    # 2026-10-05：反推已定变体，不再提示「带名字重发」
    and "请发「紫卡分析" not in _b22b,
    _b22b[:240],
)
_b22c = _run21(_rc22, ["捕月（主要）", "暴伤89.2", "多重110.3", "电击87.6", "负切割88.1"])
check(
    "㉒⑤ 手输「捕月（主要）」（拆分行）：1.1 + 继承手枪基值（暴伤 83.53%-102.09%）",
    "【捕月（主要）】倾向 1.1" in _b22c and "83.53%-102.09%" in _b22c,
    _b22c[:240],
)
_b22d = _run21(_rc22, ["捕月", "暴伤45.1", "多重60.2", "电伤49.8", "负切割40.3"])
check(
    "㉒⑥ 残差 >15% ⇒ 不判，保留反推（老卡）并附「候选残差」",
    ("老卡" in _b22d or "疑似变体卡" in _b22d) and "候选残差" in _b22d,
    _b22d[:260],
)

# ---------------------------------------------------------------------------
# ㉓ 家族表 mtime 护栏（2026-10-03 线上事故：热重载偶发不清 matching 的
#     模块级缓存 ⇒ 旧表驻留 ⇒ 组合枪双模式家族候选为空）。双向验证：
#     表文件变了必须重载（该动）；没变不得重载（不该动，避免白读磁盘）。
# ---------------------------------------------------------------------------
from core import matching as _M23  # noqa: E402
from core import paths as _P23  # noqa: E402
import json as _json23  # noqa: E402

_orig_rp23 = _P23.read_path
_tbl23 = Path(tempfile.mkdtemp(prefix="fam23_")) / "riven_families.json"


def _rp23(name):
    return _tbl23 if name == "de/riven_families.json" else _orig_rp23(name)


_P23.read_path = _rp23
_M23._RIVEN_TABLE = None
_M23._RIVEN_INDEX = None
_M23._RIVEN_TABLE_STAMP = None
try:
    _tbl23.write_text(_json23.dumps({"by_name": {"GunA": "RootA"}}), encoding="utf-8")
    _t23a = _M23._load_riven_families()
    check("㉓① 首载：读到表内容", _t23a == {"GunA": "RootA"}, str(_t23a))
    _t23b = _M23._load_riven_families()
    check("㉓② 文件未变：不重载（同一对象，stat 不触发重读）", _t23b is _t23a, "意外重新加载了")
    _M23._riven_index()  # 索引也建起来（覆盖索引缓存路径）
    _tbl23.write_text(
        _json23.dumps(
            {
                "by_name": {
                    "GunA": "RootA",
                    "GunB": "RootB",
                    "Catchmoon (Primary)": "SUModularSecondaryBarrelAPart",
                }
            }
        ),
        encoding="utf-8",
    )
    _t23c = _M23._load_riven_families()
    check(
        "㉓③ 表文件变更（size/mtime 变）：自动重载新内容",
        _t23c is not _t23a and "Catchmoon (Primary)" in _t23c,
        str(_t23c),
    )
    check(
        "㉓④ 索引缓存随表一并失效重建（family_key 用上新键）",
        _M23.family_key("Catchmoon (Primary)") == "SUModularSecondaryBarrelAPart"
        and _M23._riven_index()[0].get("GunB") == "RootB",
        str(_M23._riven_index()[0]),
    )
finally:
    _P23.read_path = _orig_rp23
    _M23._RIVEN_TABLE = None  # 还原：后续用例重读真实表
    _M23._RIVEN_INDEX = None
    _M23._RIVEN_TABLE_STAMP = None

# ---------------------------------------------------------------------------
# ㉔ 组合枪紫卡基值列（2026-10-03 晚定稿，用户同卡双态截图实证）：**不分模式
#     一律手枪列** —— 墓指（主要）实卡（伤害194.7/多重103.5/弹匣50.2）按手枪
#     列×倾向1.0 反推 U=0.95/0.92/1.07 全部落带，按步枪列出带（U 1.26/1.23）。
#     主/次形态只差倾向值（omega/primeOmega）；「霰弹枪MOD」指普通 MOD 槽。
#     早间按霰弹/步枪列部署的映射由此回退（8721dfc 回归部分）。
# ---------------------------------------------------------------------------
check(
    "㉔① kitgun_mode_class：所有腔体、所有模式一律手枪列",
    RA.kitgun_mode_class("Catchmoon (Primary)", "pistol", "kitgun") == "pistol"
    and RA.kitgun_mode_class("Tombfinger (Primary)", "pistol", "kitgun") == "pistol"
    and RA.kitgun_mode_class("墓指（主要）", "pistol", "kitgun") == "pistol"
    and RA.kitgun_mode_class("捕月（主要）", "pistol", "kitgun") == "pistol"
    and RA.kitgun_mode_class("Gaze (Primary)", "pistol", "kitgun") == "pistol"
    and RA.kitgun_mode_class("Sporelacer (Primary)", "pistol", "kitgun") == "pistol"
    and RA.kitgun_mode_class("Vermisplicer (Primary)", "pistol", "kitgun") == "pistol"
    and RA.kitgun_mode_class("Rattleguts (Primary)", "pistol", "kitgun") == "pistol",
    "kitgun 类别错",
)
check(
    "㉔② kitgun_mode_class：非 kitgun 原样",
    RA.kitgun_mode_class("Vinquibus (Primary)", "rifle", "rifle") == "rifle"
    and RA.kitgun_mode_class("Lex (Primary)", "pistol", "pistol") == "pistol",
    "非kitgun 类别错",
)
_b24 = _run21(_rc22, ["墓指（主要）", "伤害194.7", "弹匣50.2", "多重103.5", "负变焦65.1"])
check(
    "㉔③ 实卡判别（墓指主要，用户双态截图装配态）：按手枪列 1.0 严格命中，"
    "多重区间上限 123.44%（手枪列），不再步枪列 92.81%/老卡",
    "【墓指（主要）】倾向 1" in _b24
    and "123.44%" in _b24
    and "92.81%" not in _b24
    and "老卡" not in _b24,
    _b24[:260],
)

# ---------------------------------------------------------------------------
# ★ 追加段（⑫-㉔）出口守卫：本文件原有守卫在第 802 行（①-⑫ 之前），
#   其后的追加段此前**没有守卫** —— 失败不会置退出码（等同静默放行）。
# ---------------------------------------------------------------------------
print()
if FAILED:
    print(f"✗ 追加段失败 {len(FAILED)} 项：" + "、".join(FAILED))
    sys.exit(1)
print("✓ 追加段（⑫-㉔）全部通过")
