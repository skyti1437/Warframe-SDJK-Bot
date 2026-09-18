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
    "ammo_maximum", "base_damage_/_melee_damage", "critical_chance",
    "critical_damage", "fire_rate_/_attack_speed",
    "chance_to_gain_combo_count", "chance_to_gain_extra_combo_count",
    "channeling_damage", "channeling_efficiency",
    "critical_chance_on_slide_attack",
}

from core import formatters as F  # noqa: E402
from core import parser as P  # noqa: E402
from core import riven_analysis as RA  # noqa: E402

check("冻结的 slug 数量为 32", len(WM_RIVEN_SLUGS) == 32,
      str(len(WM_RIVEN_SLUGS)))
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
for sid in ("slide_crit", "initial_combo", "heavy_attack_efficiency",
            "extra_combo_count", "combo_gain_chance"):
    mapped = P.RIVEN_URL_COMPAT.get(sid)
    check(f"紫卡搜索 slug 合法：{sid}", mapped in WM_RIVEN_SLUGS, str(mapped))

# ------------------------- ⑥ 展示名唯一（main.py 用 {展示名: id} 反查解析输入）
_vals = list(P.RIVEN_STAT_ZH.values())
check("RIVEN_STAT_ZH 展示名无重复", len(set(_vals)) == len(_vals),
      str([v for v in set(_vals) if _vals.count(v) > 1]))

# ------------------------- ⑦ 单位：无基值表的连击词条仍要按百分比显示
check("额外连击按百分比显示",
      RA.fmt_value("extra_combo_count", 19.7) == "19.7%",
      RA.fmt_value("extra_combo_count", 19.7))
check("连击获取按百分比显示",
      RA.fmt_value("combo_gain_chance", 42.9) == "42.9%",
      RA.fmt_value("combo_gain_chance", 42.9))

# ------------------------------------------------- ⑧ 端到端：拍卖卡不再漏英文
# 复刻用户截图里那三条（第 1/3 条滑砍暴击、第 8 条两条 channeling）
_auc = [{
    "buyout_price": 40,
    "owner": {"status": "ingame", "ingame_name": "audgbfwo", "reputation": 4},
    "item": {"re_rolls": 0, "mod_rank": 0, "attributes": [
        {"url_name": "critical_chance_on_slide_attack",
         "value": 9.2, "positive": True},
        {"url_name": "channeling_damage", "value": 19.7, "positive": True},
        {"url_name": "channeling_efficiency", "value": 42.9, "positive": False},
    ]},
}]
_title, _lines, _best = F.fmt_wr_auctions("天薙刀", _auc)
_body = "\n".join(_lines)
check("拍卖卡标题正常", _title.startswith("天薙刀 紫卡拍卖"), _title)
check("拍卖卡不含英文词条",
      "critical chance" not in _body and "channeling" not in _body, _body)
check("拍卖卡显示中文滑暴", "▲滑暴9.2" in _body, _body)
check("拍卖卡显示中文初始连击", "▲初始连击19.7" in _body, _body)
check("拍卖卡显示中文重击效率（负向）", "▼重击效率42.9" in _body, _body)

# ⑨ 反向守卫：确认「回落」这条路径确实存在（否则上面 ① 是假阳性）
check("未收录 slug 仍走英文回落（兜底路径在位）",
      F._riven_stat_cn("some_future_new_stat") == "some future new stat",
      F._riven_stat_cn("some_future_new_stat"))

if FAILED:
    print(f"\n失败 {len(FAILED)} 项：{FAILED}")
    sys.exit(1)
print("\n全部通过 ✔")
