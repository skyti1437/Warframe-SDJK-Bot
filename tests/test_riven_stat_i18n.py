# -*- coding: utf-8 -*-
"""紫卡词条译名全覆盖守卫：python3 tests/test_riven_stat_i18n.py

背景（2026-09-14 用户报障）：`wm` / `wr` 紫卡拍卖卡里**部分词条没翻译**，
直接漏出英文，例如

    ▲critical chance on slide attack9.2
    ▲channeling damage19.7   ▼channeling efficiency42.9

根因：``core/formatters._riven_stat_cn()`` 对认不出的 WM url_name 会**原样回落**
成 ``url_name.replace("_", " ")``。词条表当时只覆盖 27 个 slug，漏掉的 5 个
正好是 slug 与插件标准名**完全不同**的那几个（WM 沿用了远古 Channeling 时代的
slug 命名），所以永远走不到中文那一跳。

本测试把 WM ``/v2/riven/attributes`` 的全部 32 个 slug **冻死**在这里当哨兵：
将来漏译、或 WM 上新词条（slug 列表变了）都会立刻失败。

中文名口径：优先 DE 官方本地化 ``languages_zh.json``（如
``ComboInitialBonusModDesc`` = 「|val| 初始连击」），其次 WM v2 的 ``zh-hans``；
卡面用社区习惯的短名（滑暴 / 初始连击 / 重击效率 / 额外连击 / 连击获取）。
"""

from __future__ import annotations

import asyncio
import copy
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = ""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f"  -> {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


# ---------------------------------------------------------------------------
# WM v2 /riven/attributes 全量 slug（2026-09-14 快照，共 32 个）
# 顺序无关；这一行就是"契约" —— WM 加词条时这里要一起改，并补上中文名。
# ---------------------------------------------------------------------------
WM_RIVEN_SLUGS = [
    "ammo_maximum",
    "base_damage_/_melee_damage",
    "chance_to_gain_combo_count",
    "chance_to_gain_extra_combo_count",
    "channeling_damage",
    "channeling_efficiency",
    "cold_damage",
    "combo_duration",
    "critical_chance",
    "critical_chance_on_slide_attack",
    "critical_damage",
    "damage_vs_corpus",
    "damage_vs_grineer",
    "damage_vs_infested",
    "electric_damage",
    "finisher_damage",
    "fire_rate_/_attack_speed",
    "heat_damage",
    "impact_damage",
    "magazine_capacity",
    "multishot",
    "projectile_speed",
    "punch_through",
    "puncture_damage",
    "range",
    "recoil",
    "reload_speed",
    "slash_damage",
    "status_chance",
    "status_duration",
    "toxin_damage",
    "zoom",
]

# slug != 插件标准名的特例（必须显式挂 RIVEN_URL_COMPAT，否则回落英文）
_SLUG_DIFFERS_FROM_ID = {
    "ammo_maximum",
    "base_damage_/_melee_damage",
    "critical_chance",
    "critical_damage",
    "fire_rate_/_attack_speed",
    "chance_to_gain_combo_count",
    "chance_to_gain_extra_combo_count",
    "channeling_damage",
    "channeling_efficiency",
    "critical_chance_on_slide_attack",
}

from core import formatters as F  # noqa: E402
from core import parser as P  # noqa: E402
from core import riven_analysis as RA  # noqa: E402
from core.commands.vision import VisionCommands as V  # noqa: E402
from core.commands.market import MarketCommands as M  # noqa: E402

check("冻结的 slug 数量为 32", len(WM_RIVEN_SLUGS) == 32, str(len(WM_RIVEN_SLUGS)))
check("slug 无重复", len(set(WM_RIVEN_SLUGS)) == len(WM_RIVEN_SLUGS))

# ------------------------------------------------- ① 每个 slug 都能译成中文
for slug in WM_RIVEN_SLUGS:
    zh = F._riven_stat_cn(slug)
    fallback = slug.replace("_", " ")
    check(f"拍卖词条已中文化：{slug}", zh != fallback, f"回落成了 {zh!r}")

# ------------------------- ② slug → 标准 id → 展示名 两跳都要通（防中间落空）
# 注意 RIVEN_URL_COMPAT 是 {标准 id: slug}，查 slug 要先反转 —— 与
# formatters._riven_stat_cn() 内部做法一致，不要写反。
_SLUG_TO_CANON = {v: k for k, v in P.RIVEN_URL_COMPAT.items()}
for slug in WM_RIVEN_SLUGS:
    canon = _SLUG_TO_CANON.get(slug, slug)
    check(f"标准词条有中文展示名：{slug}", canon in P.RIVEN_STAT_ZH, canon)

# ------------------------- ③ 与标准名不同的 slug 必须显式写进 RIVEN_URL_COMPAT
for slug in _SLUG_DIFFERS_FROM_ID:
    check(f"特例 slug 已挂映射：{slug}", slug in P.RIVEN_URL_COMPAT.values(), slug)

# ------------------------- ④ 本次报障的 5 个词条逐一点名（回归锚点）
for slug, want in [
    ("critical_chance_on_slide_attack", "滑暴"),
    ("channeling_damage", "初始连击"),
    ("channeling_efficiency", "重击效率"),
    ("chance_to_gain_extra_combo_count", "额外连击"),
    ("chance_to_gain_combo_count", "连击获取"),
]:
    got = F._riven_stat_cn(slug)
    check(f"报障词条译名：{slug} -> {want}", got == want, got)

# ------------------------- ⑤ 搜索参数合法性：发给 WM 的 slug 必须真实存在
for sid in (
    "slide_crit",
    "initial_combo",
    "heavy_attack_efficiency",
    "extra_combo_count",
    "combo_gain_chance",
):
    mapped = P.RIVEN_URL_COMPAT.get(sid)
    check(f"紫卡搜索 slug 合法：{sid}", mapped in WM_RIVEN_SLUGS, str(mapped))

# ------------------------- ⑥ 展示名唯一（main.py 用 {展示名: id} 反查解析输入）
_vals = list(P.RIVEN_STAT_ZH.values())
check(
    "RIVEN_STAT_ZH 展示名无重复",
    len(set(_vals)) == len(_vals),
    str([v for v in set(_vals) if _vals.count(v) > 1]),
)

# ------------------------- ⑦ 单位：无基值表的连击词条仍要按百分比显示
check(
    "额外连击按百分比显示",
    RA.fmt_value("extra_combo_count", 19.7) == "19.7%",
    RA.fmt_value("extra_combo_count", 19.7),
)
check(
    "连击获取按百分比显示",
    RA.fmt_value("combo_gain_chance", 42.9) == "42.9%",
    RA.fmt_value("combo_gain_chance", 42.9),
)

# ------------------------------------------------- ⑧ 端到端：拍卖卡不再漏英文
# 复刻用户截图里那三条（第 1/3 条滑砍暴击、第 8 条两条 channeling）
_auc = [
    {
        "buyout_price": 40,
        "owner": {"status": "ingame", "ingame_name": "audgbfwo", "reputation": 4},
        "item": {
            "re_rolls": 0,
            "mod_rank": 0,
            "attributes": [
                {"url_name": "critical_chance_on_slide_attack", "value": 9.2, "positive": True},
                {"url_name": "channeling_damage", "value": 19.7, "positive": True},
                {"url_name": "channeling_efficiency", "value": 42.9, "positive": False},
            ],
        },
    }
]
_title, _lines, _best = F.fmt_wr_auctions("天薙刀", _auc)
_body = "\n".join(_lines)
check("拍卖卡标题正常", _title.startswith("天薙刀 紫卡拍卖"), _title)
check("拍卖卡不含英文词条", "critical chance" not in _body and "channeling" not in _body, _body)
check("拍卖卡显示中文滑暴", "▲滑暴9.2" in _body, _body)
check("拍卖卡显示中文初始连击", "▲初始连击19.7" in _body, _body)
check("拍卖卡显示中文重击效率（负向）", "▼重击效率42.9" in _body, _body)

# ⑨ 反向守卫：确认「回落」这条路径确实存在（否则上面 ① 是假阳性）
check(
    "未收录 slug 仍走英文回落（兜底路径在位）",
    F._riven_stat_cn("some_future_new_stat") == "some future new stat",
    F._riven_stat_cn("some_future_new_stat"),
)

# ⑩ 基值表：霰弹枪 Zoom 格（2026-10-07 补；此前为 None）
#   依据 = wiki `Riven_Mods` 基值表原文 `| [[Zoom]] | Hera || Lis || 59.99% || 41.994%
#   || 80.1% || 59.99% || –`（列序 rifle|shotgun|pistol|archgun|melee）
#   + Hotfix 44.0.3「Shotgun Rivens can now roll Zoom stats」（为 Riven Splicer 铺路）。
check(
    "基值表 zoom 行 = (59.99, 41.994, 80.1, 59.99, None)",
    RA._BASE["zoom"] == (59.99, 41.994, 80.1, 59.99, None),
    str(RA._BASE["zoom"]),
)
check(
    "★ 霰弹枪 Zoom 不再是 None（44.0.3 起可 roll；区间按 41.994 起算）",
    RA._BASE["zoom"][1] is not None,
    str(RA._BASE["zoom"]),
)

# ⑪ ★ Riven Splicer（Update 44，2026-10-07 上线）18 个新词条：注册 + 不得错绑旧词条
#   实测（本批取证）：注册前 `_stat_id_from_name` 会把它们**静默错绑**——
#   病毒伤害/毒气伤害→毒素伤害、爆炸伤害→暴击伤害、弹药效率→弹药上限、弱点暴击→暴击几率；
#   其余 13 个则**静默丢弃**（⇒ 词条数少算、系数错档、区间偏高约 32%）。
_NEW_STATS = {
    "gas_damage": "毒气伤害",
    "corrosive_damage": "腐蚀伤害",
    "viral_damage": "病毒伤害",
    "radiation_damage": "辐射伤害",
    "blast_damage": "爆炸伤害",
    "magnetic_damage": "磁力伤害",
    "damage_vs_orokin": "对奥罗金的伤害",
    "damage_vs_scaldra": "对炽蛇军的伤害",
    "damage_vs_techrot": "对科腐者的伤害",
    "weakpoint_damage": "弱点伤害",
    "weakpoint_crit_chance": "弱点暴击几率",
    "status_damage": "状态伤害",
    "ammo_efficiency": "弹药效率",
    "reload_holstered": "收起武器时弹匣每秒自动装填",
    "melee_heavy_attack_damage": "在重击时的近战伤害",
    "melee_heavy_attack_charge": "重击准备速度",
    "melee_parry_angle": "招架角度",
    "melee_slam_damage": "震地伤害",
}
_rev = {v: k for k, v in P.RIVEN_STAT_ZH.items()}
_bad = [
    (n, V._stat_id_from_name(n, _rev))
    for n, k in ((n, k) for k, n in _NEW_STATS.items())
    if V._stat_id_from_name(n, _rev) != k
]
check(
    "⑪① 18 个 Splicer 新词条全部注册且按**新 id**解析（不再错绑旧词条）",
    not _bad,
    str(_bad),
)
check(
    "⑪② 每个新词条都有基值（否则区间算不出、只会显示「无官方基值」）",
    all(k in RA._BASE for k in _NEW_STATS),
    str([k for k in _NEW_STATS if k not in RA._BASE]),
)
check(
    "⑪④ 兼容别名仍在：对Orokin伤害/格挡角度/重击蓄力 等旧写法也能解析",
    all(
        V._stat_id_from_name(n, _rev) == k
        for n, k in (
            ("对Orokin伤害", "damage_vs_orokin"),
            ("对Scaldra伤害", "damage_vs_scaldra"),
            ("对Techrot伤害", "damage_vs_techrot"),
            ("格挡角度", "melee_parry_angle"),
            ("重击蓄力", "melee_heavy_attack_charge"),
        )
    ),
    str([(n, V._stat_id_from_name(n, _rev)) for n in ("对Orokin伤害", "格挡角度")]),
)
check(
    "⑪③ 反向守卫：旧元素词条解析不受影响（毒素伤害→toxin_damage）",
    V._stat_id_from_name("毒素伤害", _rev) == "toxin_damage",
    str(V._stat_id_from_name("毒素伤害", _rev)),
)

# ⑫ ★ 2026-10-10 对照 wfsim ce78ffeafd（market.yaml riven_stats）+ WM v2 实测：
#   `/v2/riven/attributes` 2026-10-10 返回 50 个 slug = 上面 32 个 + 下面 18 个 Splicer 新词条
#   （与 wfsim 表的 slug 集逐一相等）。冻结在此：WM 再改词条时这里先失败。
WM_SPLICER_SLUGS = [
    "ammo_efficiency",
    "blast",
    "corrosive",
    "damage_to_orokin",
    "damage_to_scaldra",
    "damage_to_techrot",
    "gas",
    "heavy_attack_wind_up_speed",
    "magazine_reloaded_s_when_holstered",
    "magnetic",
    "melee_damage_on_heavy_attack",
    "parry_angle",
    "radiation",
    "slam_attack_damage",
    "status_damage",
    "viral",
    "weak_point_critical_chance",
    "weak_point_damage",
]
_ALL50 = set(WM_RIVEN_SLUGS) | set(WM_SPLICER_SLUGS)
check("⑫① 32 + 18 = 50 个 slug 无交叠", len(_ALL50) == 50, str(len(_ALL50)))
check(
    "⑫② 我方 wr 发出的 slug 集 == WM 50 个（无缺、无多、无错拼）",
    {P.RIVEN_URL_COMPAT.get(s, s) for s in P.RIVEN_STAT_ALIASES} == _ALL50,
    str({P.RIVEN_URL_COMPAT.get(s, s) for s in P.RIVEN_STAT_ALIASES} ^ _ALL50),
)
for slug in WM_SPLICER_SLUGS:
    check(f"⑫③ Splicer 拍卖词条已中文化：{slug}", F._riven_stat_cn(slug) != slug.replace("_", " "))
# 真实入口 parse_wr → normalize_riven_stats：玩家写法 → WM slug
from core.api_client import WarframeClient as _WC  # noqa: E402

for _text, _pos, _neg in (
    ("x 病毒 弱爆 负弹效", ["viral", "weak_point_critical_chance"], ["ammo_efficiency"]),
    ("x 毒气辐射 对炽蛇军", ["gas", "radiation", "damage_to_scaldra"], []),
    (
        "x 重击伤害 招架 负震地",
        ["melee_damage_on_heavy_attack", "parry_angle"],
        ["slam_attack_damage"],
    ),
    (
        "x 重击速度 收枪装填 负腐蚀",
        ["heavy_attack_wind_up_speed", "magazine_reloaded_s_when_holstered"],
        ["corrosive"],
    ),
    (
        "x 磁力爆炸 对奥罗金 负对科腐者",
        ["magnetic", "blast", "damage_to_orokin"],
        ["damage_to_techrot"],
    ),
    # 原有常用词条不回归
    ("x 暴击暴伤 多重 负变焦", ["critical_chance", "critical_damage", "multishot"], ["zoom"]),
    (
        "x 基伤 攻速 触发 负G",
        ["base_damage_/_melee_damage", "fire_rate_/_attack_speed", "status_chance"],
        ["damage_vs_grineer"],
    ),
    (
        "x 双暴 滑暴 负初始连击",
        ["critical_chance", "critical_damage", "critical_chance_on_slide_attack"],
        ["channeling_damage"],
    ),
):
    _q = P.parse_wr(_text.split())
    _gp, _gn = _WC.normalize_riven_stats(_q.stats), _WC.normalize_riven_stats(_q.negatives)
    check(
        f"⑫④ parse_wr「{_text}」→ slug",
        _q.weapon == "x" and _gp == _pos and _gn == _neg,
        f"weapon={_q.weapon!r} pos={_gp} neg={_gn}",
    )

# ⑬ ★ 卡面短名回读：wr 卡显示的每个短名（RIVEN_STAT_ZH）都能被 parse_wr 认回同一词条
#   （修复前 O伤/S伤/T伤/弱点伤/处决伤 5 个认不出，「wr 空刃 异常伤 弱点伤」的武器名
#   变成「空刃 弱点伤」）。
_unread = [(k, v) for k, v in P.RIVEN_STAT_ZH.items() if P._STAT_LOOKUP.get(v) != k]
check("⑬① 卡面短名全部可回读为同一词条", not _unread, str(_unread))
_q = P.parse_wr("空刃 异常伤 弱点伤 负O伤".split())
check(
    "⑬② wr 空刃 异常伤 弱点伤 负O伤 → 武器「空刃」+ 3 词条",
    _q.weapon == "空刃"
    and _WC.normalize_riven_stats(_q.stats) == ["status_damage", "weak_point_damage"]
    and _WC.normalize_riven_stats(_q.negatives) == ["damage_to_orokin"],
    f"weapon={_q.weapon!r} pos={_q.stats} neg={_q.negatives}",
)

# ⑭ wr 别名等价 + handler 筛选：只替换网络边界，武器解析、请求构造和本地筛选走真代码。
_WR_EQUIVALENT = (
    "wr 冰凇 基础 多重 异常伤害 负i伤",
    "wr 冰凇 基多异常 负i",
    "wr 冰凇 基多异 负i",
)
_WANT_POS = ["base_damage_/_melee_damage", "multishot", "status_damage"]
_WANT_NEG = ["damage_vs_infested"]
for _text in _WR_EQUIVALENT:
    _q = P.parse_wr(P.parse(_text).content)
    check(
        f"⑭① 三种写法等价：{_text}",
        _q.weapon == "冰凇"
        and _WC.normalize_riven_stats(_q.stats) == _WANT_POS
        and _WC.normalize_riven_stats(_q.negatives) == _WANT_NEG
        and not _q.unrecognized_stats,
        repr(_q),
    )

# 英文别名逐个验证原文/小写/大写/混合大小写，含负面及与中文词条连写。
for _sid, _aliases in P.RIVEN_STAT_ALIASES.items():
    for _alias in sorted(_aliases):
        if not any(c.isascii() and c.isalpha() for c in _alias):
            continue
        for _variant in dict.fromkeys((_alias, _alias.lower(), _alias.upper(), _alias.swapcase())):
            for _neg in (False, True):
                _q = P.parse_wr(["冰凇", ("负" if _neg else "") + _variant])
                check(
                    f"⑭② 英文词条大小写一致：{'负' if _neg else ''}{_variant}",
                    _q.weapon == "冰凇"
                    and (_q.negatives if _neg else _q.stats) == [_sid]
                    and not (_q.stats if _neg else _q.negatives),
                    repr(_q),
                )
_q = P.parse_wr(P.parse("wr 冰凇 基多异负i伤").content)
check(
    "⑭③ 正负连写与分写等价",
    _q.weapon == "冰凇"
    and _WC.normalize_riven_stats(_q.stats) == _WANT_POS
    and _WC.normalize_riven_stats(_q.negatives) == _WANT_NEG,
    repr(_q),
)
for _text in ("wr 冰凇 触发", "wr 冰凇 触时"):
    _q = P.parse_wr(P.parse(_text).content)
    check(
        f"⑭④ 触发几率/触发时间不与异常伤害混淆：{_text}",
        _q.stats == (["status_chance"] if _text.endswith("触发") else ["status_duration"]),
        repr(_q),
    )
for _blob in ("基多负未知", "基多负任意未知"):
    _q = P.parse_wr(["冰凇", _blob])
    check(
        f"⑭⑤ 词条串失败不残留半截条件：{_blob}",
        not _q.stats and not _q.negatives and not _q.require_negative,
        repr(_q),
    )


def _wr_auction(aid, positives, negatives, *, price=300, status="ingame", rolls=0):
    return {
        "id": aid,
        "buyout_price": price,
        "owner": {"status": status, "ingame_name": aid},
        "item": {
            "re_rolls": rolls,
            "attributes": [
                {"url_name": s, "positive": positive, "value": 60}
                for positive, stats in ((True, positives), (False, negatives))
                for s in stats
            ],
        },
    }


class _WrReplayClient(_WC):
    def __init__(self, auctions):
        # 不初始化 HTTP 客户端；网络边界全部回放，避免测试联网。
        self._aliases = {"riven_items": {}}
        self.wm_base = "https://api.warframe.market/v1"
        self.auctions = auctions
        self.requests = []
        self.resolved = []

    async def resolve_riven_weapon(self, query):
        self.resolved.append(query)
        return await super().resolve_riven_weapon(query)

    async def wm_riven_weapons(self):
        return [
            {
                "url_name": "glaxion",
                "en": "Glaxion",
                "zh": "冰凇",
                "riven_type": "rifle",
                "group": "primary",
            }
        ]

    async def wm_riven_weapon_slugs(self):
        return {"glaxion"}

    async def _fetch_json(self, url, **kwargs):
        self.requests.append(copy.deepcopy(kwargs.get("params")))
        return {"payload": {"auctions": copy.deepcopy(self.auctions)}}


def _wr_replay(text, auctions):
    handler = M()
    handler.client = _WrReplayClient(auctions)
    handler.page_size = 12
    parsed = P.parse(text)
    entry = handler._h_rm if parsed.command == "rm" else handler._h_wr
    reply = asyncio.run(entry(parsed, None, "pc"))
    return reply, handler.client


_full = _wr_auction("full_match", _WANT_POS, _WANT_NEG, price=500)
_wrong_neg = _wr_auction("wrong_negative", _WANT_POS, ["damage_vs_corpus"], price=100)
_wrong_pos = _wr_auction(
    "wrong_positive", ["base_damage_/_melee_damage", "multishot", "status_chance"], _WANT_NEG
)
_no_neg = _wr_auction("missing_negative", _WANT_POS, [])
_wrong_sign = _wr_auction(
    "wrong_sign",
    ["multishot", "status_damage", "damage_vs_infested"],
    ["base_damage_/_melee_damage"],
)
_replies, _requests = [], []
for _text in _WR_EQUIVALENT:
    _reply, _client = _wr_replay(_text, [_wrong_neg, _wrong_pos, _no_neg, _wrong_sign, _full])
    _body = "\n".join(_reply.lines)
    _replies.append((_reply.title, _reply.lines, _reply.footer))
    _requests.append(_client.requests)
    check(
        f"⑭⑥ 完整 handler 只显示符合三个正词条和负I伤的挂单：{_text}",
        "full_match" in _body
        and not any(a["id"] in _body for a in (_wrong_neg, _wrong_pos, _no_neg, _wrong_sign))
        and _client.resolved == ["冰凇"],
        _body,
    )
check(
    "⑭⑦ 三种输入返回相同卡片与 API 参数",
    _replies.count(_replies[0]) == 3 and _requests.count(_requests[0]) == 3,
)
check(
    "⑭⑧ 请求携带完整正负词条（逗号 AND），升/降序各查一次",
    len(_requests[0]) == 2
    and all(
        r.get("positive_stats") == ",".join(_WANT_POS)
        and r.get("negative_stats") == "damage_vs_infested"
        for r in _requests[0]
    ),
    repr(_requests[0]),
)
for _text, _pool in (
    *((text, [_wrong_neg, _no_neg]) for text in _WR_EQUIVALENT),
    ("wr 冰凇 基多 3+1", [_no_neg]),
    ("wr 冰凇 基多异 带负", [_wrong_pos, _no_neg]),
    ("rm 冰凇 基多异 负i", [_wrong_neg, _no_neg]),
):
    _reply, _ = _wr_replay(_text, _pool)
    _body = "\n".join(_reply.lines)
    check(
        f"⑭⑨ 显式/隐式 3+1 无匹配不自动放宽：{_text}",
        "没有符合条件" in _body
        and not any(a["id"] in _body for a in _pool)
        and "最接近选项" not in _body,
        _body,
    )
_reply, _ = _wr_replay(
    "wr 冰凇 基多异 负i 最新",
    [dict(_full, owner={"status": "offline", "ingame_name": "offline_match"})],
)
check(
    "⑭⑩ 3+1 也不自动放宽显式在线条件",
    "没有符合条件" in "\n".join(_reply.lines),
    repr(_reply.lines),
)
_reply, _ = _wr_replay("wr 冰凇 基多 负i", [_wrong_neg])
check(
    "⑭⑪ 其余查询保留近似推荐并明确提示",
    "wrong_negative" in "\n".join(_reply.lines)
    and any("最接近选项" in line for line in _reply.lines),
    repr(_reply.lines),
)
_reply, _client = _wr_replay("wr 冰凇 基多负未知", [_full])
check(
    "⑭⑫ 无法识别的负词条在解析武器/请求挂单前报错",
    "无法识别" in "\n".join(_reply.lines) and not _client.resolved and not _client.requests,
    repr(_reply.lines),
)
for _text, _auction in (
    ("wr 冰凇 基多 负i 50p", _wrong_neg),
    ("wr 冰凇 基多 负i 零洗", _wr_auction("rerolled", _WANT_POS, [], rolls=10)),
    ("wr 冰凇 基多 负i 2+1", _wrong_neg),
    ("wr 冰凇 基多 带负", _no_neg),
    ("wr 冰凇 基多 无", _wrong_neg),
):
    _reply, _ = _wr_replay(_text, [_auction])
    check(
        f"⑭⑬ 近似推荐保留价格/洗数/词条数/有无负面硬条件：{_text}",
        "没有符合条件" in "\n".join(_reply.lines),
        repr(_reply.lines),
    )

# ⑮ 扩充别名的边界：整张词表正负回读 + 名称冲突 + 真实武器名不被拆成词条。
_alias_targets: dict[str, set[str]] = {}
for _sid, _aliases in P.RIVEN_STAT_ALIASES.items():
    for _alias in sorted(_aliases):
        _alias_targets.setdefault(_alias.casefold(), set()).add(P.RIVEN_URL_COMPAT.get(_sid, _sid))
        for _is_negative in (False, True):
            # 走 parse，顺带覆盖无需引号的英文多词全称。
            _q = P.parse_wr(P.parse("wr 冰凇 " + ("负" if _is_negative else "") + _alias).content)
            check(
                f"⑮① 别名正负回读：{'负' if _is_negative else ''}{_alias}",
                _q.weapon == "冰凇"
                and (_q.negatives if _is_negative else _q.stats) == [_sid]
                and not (_q.stats if _is_negative else _q.negatives)
                and not _q.unrecognized_stats,
                repr(_q),
            )
_conflicts = {alias: targets for alias, targets in _alias_targets.items() if len(targets) > 1}
check("⑮② 别名大小写归一后不跨词条冲突（WM 合并词条视为同项）", not _conflicts, repr(_conflicts))
for _text in (
    "wr 冰凇 base damage multishot status damage 负damage to infested",
    "wr 冰凇 基多异 负 i伤",
    "wr 冰凇 基多异 负 Damage to Infested",
    "紫卡 冰凇 基多异 负i",
):
    _reply, _client = _wr_replay(_text, [_wrong_neg, _full])
    _body = "\n".join(_reply.lines)
    check(
        f"⑮③ 完整 handler 支持英文全称/负面分写/紫卡别名：{_text}",
        "full_match" in _body
        and "wrong_negative" not in _body
        and _client.requests == _requests[0],
        _body,
    )
_names = set(json.loads((ROOT / "core/data/aliases.json").read_text())["riven_items"])
for _en, _weapon in json.loads((ROOT / "core/data/dispositions_rivenmirror.json").read_text())[
    "entries"
].items():
    _names.update((_en, _weapon.get("zh", ""), _weapon.get("url_name", "")))
_broken_names = []
for _name in sorted(_names - {""}):
    _q = P.parse_wr(P.parse("wr " + _name).content)
    if _q.weapon != _name or _q.stats or _q.negatives or _q.unrecognized_stats:
        _broken_names.append((_name, _q))
check(
    f"⑮④ 本地 {len(_names - {''})} 个紫卡武器名称/别名不被误拆成词条",
    not _broken_names,
    repr(_broken_names),
)
check(
    "⑮⑤ 易歧义缩写不注册：SD / 穿 / 连 / 效",
    all(P.segment_stats(alias) is None for alias in ("SD", "穿", "连", "效")),
)

if FAILED:
    print(f"\n失败 {len(FAILED)} 项：{FAILED}")
    sys.exit(1)
print("\n全部通过 ✔")
