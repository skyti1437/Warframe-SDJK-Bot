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
        def info(self, *a, **k): pass
        def warning(self, *a, **k): pass
        def error(self, *a, **k): pass
        def exception(self, *a, **k): pass
        def debug(self, *a, **k): pass

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
        event_message_type = staticmethod(lambda spec: (lambda fn: fn))

    event_mod.AstrMessageEvent = AstrMessageEvent
    event_mod.MessageChain = MessageChain
    event_mod.EventMessageType = _EventMessageType
    event_mod.event_message_type = lambda spec: (lambda fn: fn)
    event_mod.filter = _Filter()
    mc_mod.Image = type("Image", (), {})
    mc_mod.Plain = type("Plain", (), {})
    star_mod.Context = type("Context", (), {})

    class Star:
        def __init__(self, *a, **k): pass

    star_mod.Star = Star
    star_mod.register = lambda *a, **k: (lambda cls: cls)
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
    WarframeClient, load_aliases, localize_variant_en,
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
_card = {"positive": [["暴击伤害", 60.8], ["暴击", 110.1], ["初始连击", 15.4],
                      ["滑暴", -63.4]], "negative": []}
_pos, _neg = _norm(_card, _rev)
check("4 条正词条里带负号的那条被移到负词条",
      len(_pos) == 3 and _neg == [("slide_crit", 63.4)],
      f"pos={_pos} neg={_neg}")
check("负词条 magnitude 取绝对值（不是 -63.4）",
      _neg and _neg[0][1] == 63.4, str(_neg))
_pos2, _neg2 = _norm({"positive": [["暴伤", 90.0]], "negative": [["切割伤害", 99.9]]}, _rev)
check("正常的 negative 仍按负词条收（未被误改）",
      _pos2 == [("crit_damage", 90.0)] and _neg2 == [("slash_damage", 99.9)],
      f"pos={_pos2} neg={_neg2}")

# --------------------------------------------------- ② 武器名兜底（含最长匹配）
print("\n=== ② 武器名兜底：卡面自命名连写/音译 ===")
_FAKE = [
    {"url_name": "okina", "zh": "翁", "en": "Okina", "disposition": 0.5,
     "riven_type": "melee", "group": "melee", "tags": []},
    {"url_name": "fake_longer", "zh": "翁洛", "en": "FakeLonger", "disposition": 1.0,
     "riven_type": "melee", "group": "melee", "tags": []},
]
_c = WarframeClient.__new__(WarframeClient)
_c._aliases = {}


async def _fake_weapons():
    return [dict(w) for w in _FAKE]


_c.wm_riven_weapons = _fake_weapons
_hit = asyncio.run(_c.resolve_riven_weapon("初始 翁"))
check("「初始 翁」→ okina（词条名混进武器名）",
      (_hit or {}).get("url_name") == "okina", str(_hit))
check("兜底命中会标 _fuzzy_from（卡面注明来源）",
      (_hit or {}).get("_fuzzy_from") == "初始 翁", str(_hit))
_hit2 = asyncio.run(_c.resolve_riven_weapon("翁洛奇亚鲁姆"))
check("★ 多个候选时取**最长**已知武器名（翁洛 优先于 翁）",
      (_hit2 or {}).get("url_name") == "fake_longer",
      str((_hit2 or {}).get("url_name")))
_hit3 = asyncio.run(_c.resolve_riven_weapon("翁"))
check("精确名仍走正常链路（不标 _fuzzy_from）",
      (_hit3 or {}).get("url_name") == "okina"
      and not (_hit3 or {}).get("_fuzzy_from"), str(_hit3))

# ------------------------------------------------------------ ③ 负词条幅度位
print("\n=== ③ 负词条「幅度位」方向 ===")
_title, _lines = F.fmt_riven_analysis(
    "视使之触", 1.2, "pistol",
    [("crit_damage", 108.2), ("multishot", 141.7), ("toxin_damage", 98.2)],
    [("slash_damage", 99.9)])
_neg_line = next((x for x in _lines if "切割" in x and x.startswith("·")), "")
check("★ -99.9 切割贴区间下限 → 幅度位 13%（不是反的 87%）",
      "幅度位 13%" in _neg_line, _neg_line)
check("不再出现方向相反的字样", "幅度位 87%" not in _neg_line, _neg_line)
# 反向锚点：贴上限的负词条应显示高幅度位
_t2, _l2 = F.fmt_riven_analysis(
    "视使之触", 1.2, "pistol",
    [("crit_damage", 108.2), ("multishot", 141.7), ("toxin_damage", 98.2)],
    [("slash_damage", 118.0)])
_neg_line2 = next((x for x in _l2 if "切割" in x and x.startswith("·")), "")
check("贴上限的负词条 → 幅度位接近 100%（正负号都验过）",
      "幅度位 10" not in _neg_line2 and "幅度位 9" in _neg_line2, _neg_line2)

# ------------------------------------------------- ④ 变体中文名 + 曲翼枪械分类
print("\n=== ④ 变体中文名补全与曲翼枪械分类 ===")
_base = {"larkspur": "翠雀", "braton": "布莱顿", "grattler": "葛拉特勒",
         "war": "战争之剑", "hema": "血肢"}
for _en, _want in (("Larkspur Prime", "翠雀 Prime"),
                   ("MK1-Braton", "MK1-布莱顿"),
                   ("Kuva Grattler", "赤毒·葛拉特勒"),
                   ("Coda Hema", "终幕·血肢"),
                   ("War Prime", "战争之剑 Prime"),
                   ("Tombfinger (Secondary)", "墓指（次要）")):
    _got = localize_variant_en(_en, _base)
    check(f"变体规则 {_en} → {_want}", _got == _want, _got)
check("规则不认识/基名查不到 → 空串（保持英文，不猜）",
      localize_variant_en("Bogus Prime", {"bogus": ""}) == ""
      and localize_variant_en("Mystery Thing", {}) == "")

_names = __import__("json").loads(
    (ROOT / "core" / "data" / "de" / "name_en_zh.json").read_text(encoding="utf-8"))
_names = _names.get("names") or {}


def _name_lookup(en: str) -> str:
    from core.api_client import _norm_name_key
    for k, v in _names.items():
        if _norm_name_key(k) == _norm_name_key(en):
            return v
    return ""


check("★ DE 官方简中表能补出 Larkspur Prime（报障那条）",
      _name_lookup("Larkspur Prime") == "翠雀 Prime",
      _name_lookup("Larkspur Prime"))

check("曲翼枪械：riven_type=rifle + group=archgun → 曲翼枪械",
      F.riven_type_cn("rifle", "archgun") == "曲翼枪械",
      F.riven_type_cn("rifle", "archgun"))
check("守护武器：group=sentinel → 守护武器",
      F.riven_type_cn("rifle", "sentinel") == "守护武器",
      F.riven_type_cn("rifle", "sentinel"))
check("普通武器不受影响（group=melee/pistol → 原样）",
      F.riven_type_cn("melee", "melee") == "近战"
      and F.riven_type_cn("pistol", "secondary") == "手枪")
check("★ 基值列：曲翼枪械必须用 archgun 列（暴伤 80.1 而非步枪 120）",
      RA.weapon_class("rifle", "archgun") == "archgun",
      RA.weapon_class("rifle", "archgun"))
check("射击武器（group=secondary/pistol）仍按手枪列",
      RA.weapon_class("pistol", "secondary") == "pistol")
check("守护武器回落步枪列（wiki 无守护列，卡面已注明近似）",
      RA.weapon_class("rifle", "sentinel") == "rifle")

# ------------------------------------------------------- ⑤ wr 排序与放宽分档
print("\n=== ⑤ wr：放宽分档 + 排序保留 ===")
check("presorted=True 时渲染层不按 在线+价格 重排",
      [x for x in F.fmt_wr_auctions(
          "翁",
          [{"buyout_price": 300, "owner": {"status": "offline"},
            "item": {"attributes": []}},
           {"buyout_price": 230, "owner": {"status": "offline"},
            "item": {"attributes": []}}],
          presorted=True)[1] if x.startswith("1. ")][0].startswith("1. 300p"))
check("默认（严格档）仍按价格升序",
      [x for x in F.fmt_wr_auctions(
          "翁",
          [{"buyout_price": 300, "owner": {"status": "offline"},
            "item": {"attributes": []}},
           {"buyout_price": 230, "owner": {"status": "offline"},
            "item": {"attributes": []}}])[1] if x.startswith("1. ")][0].startswith("1. 230p"))

_q = parse_wr("爆率 爆伤 初始连击 负任意 翁".split())
check("解析：初始连击 → channeling_damage（WM 搜索词表口径）",
      WarframeClient.normalize_riven_stats(_q.stats, "melee")
      == ["critical_chance", "critical_damage", "channeling_damage"],
      str(WarframeClient.normalize_riven_stats(_q.stats, "melee")))

_match_auction = {"buyout_price": 500, "owner": {"status": "offline"},
                  "item": {"attributes": [
                      {"url_name": "critical_chance", "value": 100, "positive": True},
                      {"url_name": "critical_damage", "value": 80, "positive": True},
                      {"url_name": "channeling_damage", "value": 30, "positive": True},
                      {"url_name": "damage_vs_grineer", "value": 0.7, "positive": False}]}}
check("★ 完全匹配但卖家离线 → ignore_status 档收进来（不丢）",
      plugin.WarframeSDJK._auction_match(
          _match_auction, _q, set(), {"critical_chance", "critical_damage",
                                      "channeling_damage"}) is False
      and plugin.WarframeSDJK._auction_match(
          _match_auction, _q, set(), {"critical_chance", "critical_damage",
                                      "channeling_damage"},
          ignore_status=True) is True)


class _FakeClient2:
    """wr 端到端：2 条完全匹配（离线）+ 3 条不匹配（在线且更便宜）。"""

    _aliases = {}

    def __init__(self):
        self._weapon = {"url_name": "okina", "zh": "翁", "en": "Okina",
                        "riven_type": "melee", "group": "melee"}

    async def resolve_riven_weapon(self, q):
        return dict(self._weapon)

    def normalize_riven_stats(self, stats, rtype=""):
        return WarframeClient.normalize_riven_stats(stats, rtype)

    async def wm_riven_auctions(self, *a, **k):
        def _au(p, st, attrs):
            return {"buyout_price": p, "owner": {"status": st},
                    "item": {"attributes": attrs}}
        full = [{"url_name": "critical_chance", "value": 100, "positive": True},
                {"url_name": "critical_damage", "value": 80, "positive": True},
                {"url_name": "channeling_damage", "value": 30, "positive": True},
                {"url_name": "damage_vs_grineer", "value": 0.7, "positive": False}]
        junk = [{"url_name": "critical_chance", "value": 100, "positive": True},
                {"url_name": "cold_damage", "value": 57, "positive": True},
                {"url_name": "damage_vs_corpus", "value": 0.76, "positive": False}]
        return [_au(230, "ingame", junk), _au(240, "ingame", junk), _au(300, "ingame", junk),
                _au(750, "offline", full), _au(1000, "offline", full)]


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
check("★ 前排是完全匹配条（750p/1000p），不是更便宜的无关单",
      len(_rows) >= 2 and "750p" in _rows[0] and "1000p" in _rows[1],
      str(_rows[:3]))
check("给出「卖家都不在线」说明（而不是含糊的近似匹配）",
      any("都不在线" in x for x in _reply.lines), _body[:200])
check("近似匹配的兜底文案不再出现在本场景",
      not any("最接近选项" in x for x in _reply.lines))

# ------------------------------------------------- ⑥ 老卡：按反推倾向算区间
print("\n=== ⑥ 老卡（洗出后倾向被调整）按反推值算区间 ===")
_WEAPONS_FAKE = [{"url_name": "okina", "zh": "翁", "en": "Okina",
                  "disposition": 1.4, "riven_type": "melee", "group": "melee"}]


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
_r3 = asyncio.run(_obj3._h_riven_analysis(
    _Parsed("紫卡分析 翁 暴伤60.8 暴击110.1 初始连击15.4 负滑暴63.4"), None, "pc"))
_body3 = "\n".join(_r3.lines)
check("★ 老卡不再报「武器名可能识别有误」，而是反推倾向 ≈0.69",
      "反推倾向 ≈0.69" in _body3 and "识别有误" not in _body3,
      "\n".join(_r3.lines[:3]))
check("★ 区间按反推值计算（区间位不再是清一色 0%）",
      "区间位 0%" not in _body3 and "区间位 72%" in _body3,
      "\n".join(_r3.lines[:5]))
check("老卡提示里写明「倾向调整前洗出」与当前值对照",
      "疑似倾向调整前洗出的老卡" in _body3 and "当前值 1.4" in _body3,
      "\n".join(_r3.lines[:3]))
# 反向守卫：反推区间不紧（词条互相矛盾）时，仍给「武器名可能识别有误」
_r4 = asyncio.run(_obj3._h_riven_analysis(
    _Parsed("紫卡分析 翁 暴伤60.8 暴击110.1 初始连击100"), None, "pc"))
check("反推区间不紧时仍提示核对武器名（不硬套反推值）",
      "识别有误" in "\n".join(_r4.lines), "\n".join(_r4.lines[:3]))

# --------------------------------- ⑦ p 后缀必须区分「基础版 / Prime」两件东西
print("\n=== ⑦ p 后缀：基础版与 Prime 是两件东西 ===")
# 真实 zh/en 抄自 WM（2026-09-24）：普通版也能交易时，Prime 是 _prime_set 兄弟条目
_ITEMS_FAKE = [
    {"url_name": "nautilus_set", "zh": "鹦鹉螺 一套", "en": "Nautilus Set",
     "tags": ["sentinel", "set"]},
    {"url_name": "nautilus_prime_set", "zh": "鹦鹉螺 Prime 一套",
     "en": "Nautilus Prime Set", "tags": ["prime", "sentinel", "set"]},
    {"url_name": "nautilus_blueprint", "zh": "鹦鹉螺 蓝图",
     "en": "Nautilus Blueprint", "tags": ["sentinel", "blueprint"]},
    {"url_name": "epitaph_set", "zh": "葬铭 一套", "en": "Epitaph Set",
     "tags": ["weapon", "set"]},
    {"url_name": "epitaph_prime_set", "zh": "葬铭 Prime 一套",
     "en": "Epitaph Prime Set", "tags": ["prime", "weapon", "set"]},
    {"url_name": "corvas_set", "zh": "黑鸦 一套", "en": "Corvas Set",
     "tags": ["weapon", "set"]},
    {"url_name": "corvas_prime_set", "zh": "黑鸦 Prime 一套",
     "en": "Corvas Prime Set", "tags": ["prime", "weapon", "set"]},
    {"url_name": "zylok", "zh": "席尔火枪", "en": "Zylok", "tags": ["weapon"]},
    {"url_name": "zylok_prime_set", "zh": "席尔火枪 Prime 一套",
     "en": "Zylok Prime Set", "tags": ["prime", "weapon", "set"]},
    {"url_name": "pressure_point", "zh": "压迫点", "en": "Pressure Point",
     "tags": ["mod"]},
    {"url_name": "primed_pressure_point", "zh": "压迫点 Prime",
     "en": "Primed Pressure Point", "tags": ["mod"]},
]
_c2 = WarframeClient.__new__(WarframeClient)
_c2._aliases = load_aliases()


async def _items_fake():
    return [dict(x) for x in _ITEMS_FAKE]


_c2.wm_items = _items_fake
for _q, _want in (("鹦鹉螺", "nautilus_set"),
                  ("鹦鹉螺p", "nautilus_prime_set"),
                  ("鹦鹉螺 prime", "nautilus_prime_set"),
                  ("葬铭", "epitaph_set"),
                  ("葬铭p", "epitaph_prime_set"),
                  ("黑鸦p", "corvas_prime_set"),
                  ("席尔火枪", "zylok"),
                  ("席尔火枪p", "zylok_prime_set"),
                  ("压迫点", "pressure_point"),
                  ("压迫点p", "primed_pressure_point")):
    _hit = asyncio.run(_c2.resolve_wm_item(_q))
    check(f"★ {_q} → {_want}（不落回另一件）",
          (_hit or {}).get("url_name") == _want, str((_hit or {}).get("url_name")))

from core.matching import prime_sibling  # noqa: E402
check("★ prime_sibling 能容忍「一套」后缀（鹦鹉螺 Prime 一套 → 找到）",
      (prime_sibling(_ITEMS_FAKE[0], _ITEMS_FAKE) or {}).get("url_name")
      == "nautilus_prime_set")
check("prime_sibling 对无 Prime 版返回 None（不瞎配）",
      prime_sibling({"url_name": "x", "zh": "不存在的武器", "en": "Nonexistent"},
                    _ITEMS_FAKE) is None)

# ------------------------------------------- ⑧ 小数点丢点修正 + 卡面长词条名
print("\n=== ⑧ 小数点修正与长词条名 ===")
_p, _n = _norm({"positive": [["电伤", 115.7], ["攻速", 67.7], ["基伤", 212.9]],
                "negative": [["滑行攻击暴击几率", 110.8]]}, _rev)
check("★ 卡面原文「滑行攻击暴击几率」→ slide_crit（不是 crit_chance）",
      _n and _n[0][0] == "slide_crit", str(_n))
check("正常数值不受影响（115.7 不被误改）",
      _p[0] == ("electric_damage", 115.7), str(_p))

_obj5 = plugin.WarframeSDJK.__new__(plugin.WarframeSDJK)


class _FakeClient5:
    _aliases = {}

    async def resolve_riven_weapon(self, q):
        return {"url_name": "bo", "zh": "玻之武杖", "en": "Bo",
                "disposition": 1.35, "riven_type": "melee", "group": "melee"}

    async def resolve_variant_disp(self, name):
        return (None, "")

    async def riven_family(self, weapon):
        return []


_obj5.client = _FakeClient5()
_obj5.page_size = 12
_r5 = asyncio.run(_obj5._h_riven_analysis(
    _Parsed("紫卡分析 玻之武杖 电伤1157 攻速67.7 基伤212.9 负冲击1108"), None, "pc"))
_b5 = "\n".join(_r5.lines)
check("★ 丢点数值 1157 → 115.7（按合法区间判据回捞）",
      "+115.7% 电伤" in _b5 and "+1157%" not in _b5, _b5[:300])
check("修正会在卡面注明（透明可复核）",
      "已修正小数点" in _b5 and "1157% → 115.7%" in _b5, _b5[:200])
check("负词条同样修正（1108 → 110.8）", "-110.8% 冲击" in _b5, _b5[:300])
_r6 = asyncio.run(_obj5._h_riven_analysis(
    _Parsed("紫卡分析 玻之武杖 电伤115.7 攻速67.7 基伤212.9 负冲击110.8"), None, "pc"))
check("正确带点输入不触发修正（无误伤）",
      not any("已修正小数点" in x for x in _r6.lines),
      str([x for x in _r6.lines if x.startswith("※")][:2]))

# ------------------------------- ⑨ 家族倾向：剥套装后缀 + 过滤部件行
print("\n=== ⑨ 家族倾向（翁 Prime 0.7）===")


class _FakeClient6:
    _aliases = {}

    async def wm_items(self):
        return [
            {"url_name": "okina_prime_set", "zh": "翁 Prime 一套",
             "en": "Okina Prime Set", "tags": ["weapon", "melee", "prime", "set"]},
            {"url_name": "okina_prime_handle", "zh": "翁 Prime 握柄",
             "en": "Okina Prime Handle", "tags": ["weapon", "component", "prime"]},
            {"url_name": "okina_prime_blade", "zh": "翁 Prime 刀刃",
             "en": "Okina Prime Blade", "tags": ["weapon", "component", "prime"]},
        ]


_c6 = WarframeClient.__new__(WarframeClient)
_c6._aliases = _FakeClient6._aliases
_c6.wm_items = _FakeClient6().wm_items
_fam = asyncio.run(_c6.riven_family({"url_name": "okina", "zh": "翁", "en": "Okina"}))
check("★ 翁 家族 = [翁 Prime 0.7]（套装后缀已剥、部件行已过滤）",
      _fam == [("翁 Prime", 0.7)], str(_fam))
_v, _k = asyncio.run(_c6.resolve_variant_disp("翁 Prime 一套"))
check("变体倾向查询容忍「一套」后缀（找到 wiki 表的 okina prime）",
      _v == 0.7 and _k == "okina prime", f"{_v} / {_k}")

# --------------------------------------- ⑩ 战甲单字黑话（猫p/电p/沙p/鸟p）
print("\n=== ⑩ 战甲单字黑话与「单字键不劫持」守卫 ===")
_FRAME_KEYS = {
    "猫": "khora_prime_set", "电": "volt_prime_set", "沙": "inaros_prime_set",
    "鸟": "zephyr_prime_set", "冰": "frost_prime_set", "毒": "saryn_prime_set",
    "血": "garuda_prime_set", "花": "wisp_prime_set", "蝶": "titania_prime_set",
    "茶": "protea_prime_set", "鬼": "sevagoth_prime_set",
    "音": "banshee_prime_set", "磁": "mag_prime_set", "火": "ember_prime_set",
    "玻璃": "gara_prime_set", "高斯": "gauss_prime_set",
}
_WM_TABLE = load_aliases().get("wm_items", {})
for _k, _want in _FRAME_KEYS.items():
    check(f"词库单字键 {_k} → {_want}", _WM_TABLE.get(_k) == _want,
          str(_WM_TABLE.get(_k)))
check("★ 用户点名的四个单字都在（猫/电/沙/鸟）",
      all(_WM_TABLE.get(k) for k in ("猫", "电", "沙", "鸟")))
# 2026-09-25 互联网核实（百度「相关搜索」= 社区真实输入）：
#   证实 电男p/冰男p/冰p/毒妈p/血妈/磁妹/沙甲/花甲p/跑男甲/鬼甲 在用；
#   百度侧「胖」多指宠物（莲花大胖/胖狗），但用户清单（战甲黑话.md）明确
#   肥/胖 → Grendel（胖P），且单字键有「本字/+p/+prime」守卫，故按清单收 胖。
check("★ 「胖」按清单口径 → Grendel（胖P），非宠物",
      _WM_TABLE.get("胖") == "grendel_prime_set", str(_WM_TABLE.get("胖")))
# 用户清单核出的错位映射：奶妈=Trinity、奶爸=Oberon（外部核实：百度页 Oberon+奶爸配卡）
check("★ 奶妈 → Trinity（原错指 Wisp，清单+外部核实）",
      _WM_TABLE.get("奶妈") == "trinity_prime_set", str(_WM_TABLE.get("奶妈")))
check("★ 奶爸 → Oberon（原错指 Trinity）",
      _WM_TABLE.get("奶爸") == "oberon_prime_set"
      and _WM_TABLE.get("奶爸p") == "oberon_prime_set",
      f"{_WM_TABLE.get('奶爸')} / {_WM_TABLE.get('奶爸p')}")
check("清单单字补齐（奶/摸/水/肥/枪/基/明/僧/盾/丑/高/船/骨）",
      all(_WM_TABLE.get(k) for k in
          ("奶", "摸", "水", "肥", "枪", "基", "明", "僧", "盾", "丑", "高",
           "船", "骨")))

_ITEMS_FRAMES = [{"url_name": v, "zh": f"{k} Prime 一套", "en": f"{k}Prime Set",
                  "tags": ["warframe", "prime", "set"]}
                 for k, v in _FRAME_KEYS.items()]
_c7 = WarframeClient.__new__(WarframeClient)
_c7._aliases = load_aliases()          # 完整别名结构（含 wm_items 内层表）


async def _items_frames():
    return [dict(x) for x in _ITEMS_FRAMES]


_c7.wm_items = _items_frames
for _q, _want in (("猫", "khora_prime_set"), ("猫p", "khora_prime_set"),
                  ("电p", "volt_prime_set"), ("沙p", "inaros_prime_set"),
                  ("鸟p", "zephyr_prime_set"), ("猫p".replace("p", " prime"),
                                                "khora_prime_set"),
                  # 2026-09-25 用户口径：玻璃p（Gara Prime）、蜘蛛p（Khora Prime）
                  ("玻璃p", "gara_prime_set"), ("蜘蛛p", "khora_prime_set"),
                  ("蜘蛛甲", "khora_prime_set"), ("跑男p", "gauss_prime_set")):
    _hit = asyncio.run(_c7.resolve_wm_item(_q))
    check(f"★ {_q} → {_want}", (_hit or {}).get("url_name") == _want,
          str((_hit or {}).get("url_name")))

# 守卫：单字键只认「本字 / +p / +prime」——长查询不得被劫持成战甲
for _q in ("电击伤害", "猫头鹰", "冰霜伤害"):
    _hit = asyncio.run(_c7.resolve_wm_item(_q))
    check(f"★ 守卫：{_q} 不被劫持成战甲",
          (_hit or {}).get("url_name") not in set(_FRAME_KEYS.values()),
          str((_hit or {}).get("url_name")))
# 反向守卫：多字黑话（冰男/猫刀）不受守卫影响
_hit = asyncio.run(_c7.resolve_wm_item("冰男p"))
check("多字黑话「冰男p」仍正常（守卫只约束单字键）",
      (_hit or {}).get("url_name") == "frost_prime_set",
      str((_hit or {}).get("url_name")))

print()
if FAILED:
    print(f"✗ {len(FAILED)} 项失败：" + "、".join(FAILED))
    sys.exit(1)
print("✓ 紫卡市场 5 项修复全部通过")
