# -*- coding: utf-8 -*-
"""wm 解析链修复批回归：Prime 后缀（p/P/p版/P版）+ 二字硬配 + 部件词扩充。

python3 tests/test_wm_prime_suffix.py

背景（2026-10-05 玩家报障「wm 满级 手枪精通p 出来的是满级普通手枪精通」）：
  ① 中文错别字兜底（fuzzy_hits）把「手枪精通p」当「手枪精通」的错别字提前
     return ⇒ 63 个 Primed MOD 里 48 个后缀错落（p/P/p版/P版 四种写法全错）；
  ② 二字档阈值 0.50 恰等于「共用一个字」的 difflib ratio ⇒「圣剑」硬配「剑风」；
  ③ 部件词表只有枪械 ⇒「格拉姆p 刀刃」解析不到部件。

修复：Prime 意图前置（`matching.prime_base` 共享口径 + `_prime_upgrade` 四级后备，
  别名命中已是 Prime 直接返回）、二字档 0.75、`_PART_SPECIFIC` 扩充 12 词 +
  `_pick_wm_set_part` skip 同步 + 拆件失败原文重组兜底。

★ 本测试**全离线**：物品表与别名表都是内嵌桩（不读 WM 接口、不读 output/）。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import matching  # noqa: E402
from core.api_client import WarframeClient  # noqa: E402
from core.commands.market import MarketCommands, riven_market_weapon  # noqa: E402
from core.parser import parse_wm  # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = ""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f"  -> {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


# ---------------------------------------------------------------------------
# 内嵌桩 1：63 条 Primed MOD 对照表（(基名中文, 普通版 slug, Prime 版 slug)）
#   2026-10-05 取自 WM v2 /items（zh-hans），覆盖全部 63 条 primed_*。
# ---------------------------------------------------------------------------
PRIMED_PAIRS = (
    ("弹链", "ammo_chain", "primed_ammo_chain"),
    ("霰弹扩充", "ammo_stock", "primed_ammo_stock"),
    ("动物本能", "animal_instinct", "primed_animal_instinct"),
    ("灭亡 Corpus", "bane_of_corpus", "primed_bane_of_corpus"),
    ("灭亡 Grineer", "bane_of_grineer", "primed_bane_of_grineer"),
    ("灭亡 Infested", "bane_of_infested", "primed_bane_of_infested"),
    ("灭亡奥罗金", "bane_of_orokin", "primed_bane_of_orokin"),
    ("灭亡低语者", "bane_of_the_murmur", "primed_bane_of_the_murmur"),
    ("膛室", "chamber", "primed_chamber"),
    ("充电弹头", "charged_shell", "primed_charged_shell"),
    ("急冻控场", "chilling_grasp", "primed_chilling_grasp"),
    ("净化 Corpus", "cleanse_corpus", "primed_cleanse_corpus"),
    ("净化 Grineer", "cleanse_grineer", "primed_cleanse_grineer"),
    ("净化 Infested", "cleanse_infested", "primed_cleanse_infested"),
    ("净化奥罗金", "cleanse_orokin", "primed_cleanse_orokin"),
    ("净化低语者", "cleanse_the_murmur", "primed_cleanse_the_murmur"),
    ("燃烧弹头", "combustion_rounds", "primed_combustion_rounds"),
    ("持久力", "continuity", "primed_continuity"),
    ("痉挛", "convulsion", "primed_convulsion"),
    ("制衡", "counterbalance", "primed_counterbalance"),
    ("低温弹头", "cryo_rounds", "primed_cryo_rounds"),
    ("致命效率", "deadly_efficiency", "primed_deadly_efficiency"),
    ("双重弹头", "dual_rounds", "primed_dual_rounds"),
    ("驱逐 Corpus", "expel_corpus", "primed_expel_corpus"),
    ("驱逐 Grineer", "expel_grineer", "primed_expel_grineer"),
    ("驱逐 Infested", "expel_infested", "primed_expel_infested"),
    ("驱逐奥罗金", "expel_orokin", "primed_expel_orokin"),
    ("驱逐低语者", "expel_the_murmur", "primed_expel_the_murmur"),
    ("爆发装填", "fast_hands", "primed_fast_hands"),
    ("热病打击", "fever_strike", "primed_fever_strike"),
    ("烈焰风暴", "firestorm", "primed_firestorm"),
    ("川流不息", "flow", "primed_flow"),
    ("猛烈爆发", "fulmination", "primed_fulmination"),
    ("火焰装填", "heated_charge", "primed_heated_charge"),
    ("重创", "heavy_trauma", "primed_heavy_trauma"),
    ("弹匣增幅", "magazine_warp", "primed_magazine_warp"),
    ("非晶变压器", "morphic_transformer", "primed_morphic_transformer"),
    ("领袖", "pack_leader", "primed_pack_leader"),
    ("手枪弹药转换", "pistol_ammo_mutation", "primed_pistol_ammo_mutation"),
    ("手枪精通", "pistol_gambit", "primed_pistol_gambit"),
    ("抵近射击", "point_blank", "primed_point_blank"),
    ("极地弹仓", "polar_magazine", "primed_polar_magazine"),
    ("压迫点", "pressure_point", "primed_pressure_point"),
    ("持续火力", "quickdraw", "primed_quickdraw"),
    ("破灭", "ravage", "primed_ravage"),
    ("剑风", "reach", "primed_reach"),
    ("蓄能重划", "redirection", "primed_redirection"),
    ("重生", "regen", "primed_regen"),
    ("步枪弹药转换", "rifle_ammo_mutation", "primed_rifle_ammo_mutation"),
    ("红晶枪管", "rubedo_lined_barrel", "primed_rubedo_lined_barrel"),
    ("霰弹枪弹药转换", "shotgun_ammo_mutation", "primed_shotgun_ammo_mutation"),
    ("串联弹匣", "slip_magazine", "primed_slip_magazine"),
    ("毁灭 Corpus", "smite_corpus", "primed_smite_corpus"),
    ("毁灭 Grineer", "smite_grineer", "primed_smite_grineer"),
    ("毁灭 Infested", "smite_infested", "primed_smite_infested"),
    ("毁灭奥罗金", "smite_orokin", "primed_smite_orokin"),
    ("毁灭低语者", "smite_the_murmur", "primed_smite_the_murmur"),
    ("狙击枪弹药转换", "sniper_ammo_mutation", "primed_sniper_ammo_mutation"),
    ("稳定", "stabilizer", "primed_stabilizer"),
    ("稳定枪手", "steady_hands", "primed_steady_hands"),
    ("战术上膛", "tactical_pump", "primed_tactical_pump"),
    ("弱点专精", "target_cracker", "primed_target_cracker"),
    ("恶毒弹匣", "venomous_clip", "primed_venomous_clip"),
)


def _en_of(slug: str) -> str:
    return " ".join(w.capitalize() for w in slug.split("_"))


ITEMS: list[dict] = []
_seq = [0]


def _add(slug: str, zh: str, tags: tuple[str, ...], en: str | None = None) -> None:
    _seq[0] += 1
    ITEMS.append(
        {
            "id": str(_seq[0]),
            "url_name": slug,
            "game_ref": "",
            "zh": zh,
            "en": en or _en_of(slug),
            "tags": list(tags),
        }
    )


for _base, _normal, _primed in PRIMED_PAIRS:
    _add(_normal, _base, ("mod",))
    _add(_primed, _base + " Prime", ("mod",))

# 武器/战甲：表里只有 Prime 的（普通版不可交易）、普通版可交易的、以及旧实现的诱饵
_add("rubico_prime_set", "绝路 Prime 一套", ("set",))
for _slug, _zh in (
    ("rubico_prime_barrel", "绝路 Prime 枪管"),
    ("rubico_prime_blueprint", "绝路 Prime 蓝图"),
    ("rubico_prime_receiver", "绝路 Prime 枪机"),
    ("rubico_prime_stock", "绝路 Prime 枪托"),
):
    _add(_slug, _zh, ("component", "blueprint"))
_add("gram_prime_set", "格拉姆 Prime 一套", ("set",))
_add("gram_prime_blade", "格拉姆 Prime 刀刃", ("component", "blueprint"))
_add("gram_prime_blueprint", "格拉姆 Prime 蓝图", ("component", "blueprint"))
_add("scindo_prime_set", "分裂斩斧 Prime 一套", ("set",))
_add("akbronco_prime_set", "野马双枪 Prime 一套", ("set",))
_add("bronco_prime_set", "野马 Prime 一套", ("set",))  # 诱饵：旧实现在此处错落
_add("nautilus_set", "鹦鹉螺 一套", ("set",))
_add("nautilus_prime_set", "鹦鹉螺 Prime 一套", ("set",))
_add("ash_prime_set", "灰烬 Prime 一套", ("set",))
_add("gara_prime_set", "Gara Prime 一套", ("set",))
_add("akjagara_prime_set", "Akjagara Prime 一套", ("set",))  # 诱饵：Garap 中段子串

# 以 p 结尾但非 Prime 的英文 MOD（反例 2）；弹匣增幅/战术上膛已在 63 表里
_add("rifle_amp", "步枪振幅", ("mod",), en="Rifle Amp")
_add("cold_snap", "寒流来袭", ("mod",), en="Cold Snap")
_add("piercing_step", "穿刺步伐", ("mod",), en="Piercing Step")

# 二字真名（收紧阈值不得误伤）：膛线不在 63 表里，单独补
_add("serration", "膛线", ("mod",))

_BY_SLUG = {it["url_name"]: it for it in ITEMS}


def _client() -> WarframeClient:
    c = WarframeClient.__new__(WarframeClient)
    c._aliases = {"wm_items": {"gara": "gara_prime_set"}}
    c._prime_seen = set()

    async def _wm_items():
        return ITEMS

    c.wm_items = _wm_items
    return c


CLIENT = _client()


def resolve(q: str):
    """同步跑一次真解析链，返回 url_name（未找到返回 None）。"""
    r = asyncio.run(CLIENT.resolve_wm_item(q))
    return (r or {}).get("url_name")


# ---------------------------------------------------------------------------
# 1. 正例：63 × 四种后缀（修复前 48/63 错落）
# ---------------------------------------------------------------------------
_bad = []
for _b, _n, _p in PRIMED_PAIRS:
    for _suf in ("p", "P", "p版", "P版"):
        _got = resolve(_b + _suf)
        if _got != _p:
            _bad.append((_b + _suf, _got, _p))
check(
    "★ 正例：63 条 Primed MOD × p/P/p版/P版 全部落 Prime 版（252 例）",
    not _bad,
    str(_bad[:6]),
)

# ---------------------------------------------------------------------------
# 2. 反例 1：不带后缀 ⇒ 仍是普通版（MOD 普通版可交易）
# ---------------------------------------------------------------------------
_bad = [(b, resolve(b), n) for b, n, _p in PRIMED_PAIRS if resolve(b) != n]
check("★ 反例 1：不带后缀仍是普通版（63 条）", not _bad, str(_bad[:6]))

# ---------------------------------------------------------------------------
# 3. 反例 2：以 p 结尾但非 Prime（现网已正确，不得被劫持）
# ---------------------------------------------------------------------------
for _q, _want in (
    ("rifle amp", "rifle_amp"),
    ("Rifle Amp", "rifle_amp"),
    ("magazine warp", "magazine_warp"),
    ("Magazine Warp", "magazine_warp"),
    ("cold snap", "cold_snap"),
    ("tactical pump", "tactical_pump"),
    ("piercing step", "piercing_step"),
    ("弹匣增幅", "magazine_warp"),
    ("寒流来袭", "cold_snap"),
):
    _got = resolve(_q)
    check(f"反例 2：{_q} 不被 Prime 逻辑劫持", _got == _want, f"{_got} != {_want}")

# ---------------------------------------------------------------------------
# 4. 反例 3：武器/守护（表里只有 Prime 的照旧给 Prime；普通版可交易的照旧给普通）
# ---------------------------------------------------------------------------
for _q, _want in (
    ("绝路p", "rubico_prime_set"),
    ("灰烬p", "ash_prime_set"),
    ("格拉姆p", "gram_prime_set"),
    ("鹦鹉螺p", "nautilus_prime_set"),
    ("绝路", "rubico_prime_set"),
    ("鹦鹉螺", "nautilus_set"),
):
    _got = resolve(_q)
    check(f"反例 3：{_q} → {_want}", _got == _want, f"{_got} != {_want}")

# ---------------------------------------------------------------------------
# 5. 二字硬配修复：表外二字名不再硬配；表内二字名不受影响
# ---------------------------------------------------------------------------
check(
    "★ 表外二字名不再硬配（圣剑 / 呼风 → 未找到）",
    resolve("圣剑") is None and resolve("呼风") is None,
)
check("★ 圣剑p 也不再经后缀回退落到剑风 Prime", resolve("圣剑p") is None)
check(
    "表内二字名不受影响（剑风 → reach；膛线 → serration）",
    resolve("剑风") == "reach" and resolve("膛线") == "serration",
)

# ---------------------------------------------------------------------------
# 6. 别名/归一化劫持修复（Garap：中段子串 + 已是 Prime 不再被「升级」）
# ---------------------------------------------------------------------------
check(
    "★ Garap → gara_prime_set（不再被 akjagara 中段子串劫持）", resolve("Garap") == "gara_prime_set"
)
check(
    "分裂斩斧p → scindo_prime_set（别名模糊不再误配 split_chamber）",
    resolve("分裂斩斧p") == "scindo_prime_set",
)
check(
    "野马双枪p → akbronco_prime_set（不再误配 bronco_prime_set）",
    resolve("野马双枪p") == "akbronco_prime_set",
)

# ---------------------------------------------------------------------------
# 7. 部件词扩充：格拉姆p 刀刃（parse_wm 拆件 + _pick_wm_set_part 挑件）
# ---------------------------------------------------------------------------
_q = parse_wm(["格拉姆p", "刀刃"])
_item = asyncio.run(CLIENT.resolve_wm_item(_q.item)) if _q.item else None
_parts = [it for it in ITEMS if it["url_name"].startswith("gram_prime")]
_picked = MarketCommands._pick_wm_set_part(_parts, _q.part) if _q.part else None
check(
    "★ 部件词扩充：wm 格拉姆p 刀刃 → gram_prime_blade",
    _q.part == "刀刃" and (_picked or {}).get("url_name") == "gram_prime_blade",
    f"part={_q.part!r} item={(_item or {}).get('url_name')} picked={(_picked or {}).get('url_name')}",
)

# ---------------------------------------------------------------------------
# 8. prime_base 共享口径（后续改动必须走它，别再各写正则）
# ---------------------------------------------------------------------------
check(
    "prime_base：CJK+p / p版 / prime 词",
    matching.prime_base("手枪精通p") == "手枪精通"
    and matching.prime_base("压迫点p版") == "压迫点"
    and matching.prime_base("绝路 Prime") == "绝路",
)
check(
    "prime_base：混排「驱逐 Grineerp」与空格「Gara p」",
    matching.prime_base("驱逐 Grineerp") == "驱逐 Grineer"
    and matching.prime_base("Gara p") == "Gara",
)
check(
    "prime_base：纯拉丁连写 p 不误判（Garap / rifle ampp）",
    matching.prime_base("Garap") is None and matching.prime_base("rifle ampp") is None,
)

# ---------------------------------------------------------------------------
# 9. 紫卡链（2026-10-05 用户口径）
#    ① 紫卡**分析**：p/p版/Prime 落**变体条目**（原口径，本批未动）；
#    ② 紫卡**市场**（wr）：按**母武器**认卡（「wr 绝路」→ 绝路家族全归它），
#       不解析倾向 —— WM 拍卖端点只认自家 slug（`rubico_prime` 实测 HTTP 400）
#       ⇒ 解析若落本地补全的变体条目，市场路径剥 Prime 后缀回退母武器。
#
# ★ 实测背景（勿再踩）：**运行期真表 = WM v2 原始端点（420）+ 本地
#   `dispositions_rivenmirror.json` 670 条变体补全 = 677 条**（`wm_riven_weapons()`
#   自带合并）。拿**原始端点**验证会得出假象：「紫卡表只挂母武器」「绝路p 会落
#   未找到」——真表里有 rubico_prime，两者都不成立。
# ---------------------------------------------------------------------------
_RIVEN_STUB = [
    {"id": "1", "url_name": "rubico", "zh": "绝路", "en": "Rubico"},
    # 本地补全的变体条目（不在 WM 自家拍卖列表里）
    {"id": "2", "url_name": "rubico_prime", "zh": "绝路 Prime", "en": "Rubico Prime"},
    {"id": "3", "url_name": "scindo", "zh": "分裂斩斧", "en": "Scindo"},
    {"id": "3b", "url_name": "scindo_prime", "zh": "分裂斩斧 Prime", "en": "Scindo Prime"},
    # WM 自家列表里本就以 _prime 为名的**独立武器**（母武器不在表内）——不得被回退
    {"id": "4", "url_name": "euphona_prime", "zh": "悦音 Prime", "en": "Euphona Prime"},
]
_OWN_SLUGS = {"rubico", "scindo", "euphona_prime"}


async def _wm_riven_weapons():
    return _RIVEN_STUB


async def _wm_riven_weapon_slugs():
    return _OWN_SLUGS


CLIENT.wm_riven_weapons = _wm_riven_weapons
CLIENT.wm_riven_weapon_slugs = _wm_riven_weapon_slugs


def _riven(q: str):
    r = asyncio.run(CLIENT.resolve_riven_weapon(q))
    return (r or {}).get("url_name")


def _market(q: str):
    """复刻 _h_wr：解析 → riven_market_weapon（市场按母武器认卡）。"""
    w = asyncio.run(CLIENT.resolve_riven_weapon(q))
    return (asyncio.run(riven_market_weapon(CLIENT, q, w)) or {}).get("url_name") if w else None


check("紫卡分析侧（原口径）：绝路p 落变体条目 rubico_prime", _riven("绝路p") == "rubico_prime")
check(
    "★ 紫卡市场侧：绝路p / 绝路p版 / 绝路 Prime / Rubico Prime 一律认到母武器 rubico",
    all(
        _market(q) == "rubico" for q in ("绝路p", "绝路P", "绝路p版", "绝路 Prime", "Rubico Prime")
    ),
    str([(q, _market(q)) for q in ("绝路p", "绝路p版", "绝路 Prime", "Rubico Prime")]),
)
check("紫卡市场侧：分裂斩斧p → scindo", _market("分裂斩斧p") == "scindo")
check(
    "★ 紫卡市场侧：WM 自家独立条目不受回退影响（悦音 Prime → euphona_prime）",
    _market("悦音 Prime") == "euphona_prime",
)
check("紫卡市场侧：不带后缀原样（绝路 → rubico）", _market("绝路") == "rubico")

if FAILED:
    print(f"\n失败 {len(FAILED)} 项：{FAILED}")
    sys.exit(1)
print("\n全部通过 ✔")
