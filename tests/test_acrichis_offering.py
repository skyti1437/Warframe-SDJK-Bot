# -*- coding: utf-8 -*-
"""言录使当期货单解析器（parse_acrichis_current）测试。

子页 https://wiki.warframe.com/w/Acrithis/Current_Offerings 是社区人工维护的
「本周 5 件」上报（vardefine 模板），2026-09-21 起作为自动刷新数据源。
样例取自 2026-09-21 实抓（FlareSolverr 转义形态 + 纯 wikitext 两种都要能吃）。
"""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from core.api_client import WarframeClient  # noqa: E402

RAW_ESCAPED = (
    "<html><head><meta name=\"color-scheme\" content=\"light dark\"></head>"
    "<body><pre style=\"word-wrap: break-word; white-space: pre-wrap;\">"
    "&lt;!-- Valid item names for AcrithisItem1\u20135 below (must match "
    "exactly, including capitalization):\n"
    "Orokin Reactor\nOrokin Catalyst\nExilus Warframe Adapter\n"
    "Exilus Weapon Adapter\nPrimary Arcane Adapter\nSecondary Arcane Adapter\n"
    "Forma\nMelee Riven Mod\nPistol Riven Mod\nRifle Riven Mod\n"
    "Kitgun Riven Mod\nZaw Riven Mod\nShotgun Riven Mod\n"
    "Companion Weapon Riven Mod\n5000 Kuva\n"
    "--&gt;{{#vardefine:AcrithisObserved|September 21, 2026}}&lt;!--\n"
    "--&gt;{{#vardefine:AcrithisItem1|5000 Kuva}}&lt;!--\n"
    "--&gt;{{#vardefine:AcrithisItem2|Primary Arcane Adapter}}&lt;!--\n"
    "--&gt;{{#vardefine:AcrithisItem3|Orokin Catalyst}}&lt;!--\n"
    "--&gt;{{#vardefine:AcrithisItem4|Forma}}&lt;!--\n"
    "--&gt;{{#vardefine:AcrithisItem5|Orokin Reactor}}&lt;!--\n"
    "--&gt;&lt;noinclude&gt;\nThis page holds Acrithis's currently reported "
    "Weekly Rotation items.\n&lt;/noinclude&gt;</pre></body></html>"
)

RAW_PLAIN = (
    "<!-- Valid item names for AcrithisItem1\u20135 below (must match exactly, "
    "including capitalization):\n"
    "Orokin Reactor\nOrokin Catalyst\nExilus Warframe Adapter\n"
    "Exilus Weapon Adapter\nPrimary Arcane Adapter\nSecondary Arcane Adapter\n"
    "Forma\nMelee Riven Mod\nPistol Riven Mod\nRifle Riven Mod\n"
    "Kitgun Riven Mod\nZaw Riven Mod\nShotgun Riven Mod\n"
    "Companion Weapon Riven Mod\n5000 Kuva\n"
    "-->{{#vardefine:AcrithisObserved|September 21, 2026}}<!--\n"
    "-->{{#vardefine:AcrithisItem1|5000 Kuva}}<!--\n"
    "-->{{#vardefine:AcrithisItem2|Primary Arcane Adapter}}<!--\n"
    "-->{{#vardefine:AcrithisItem3|Orokin Catalyst}}<!--\n"
    "-->{{#vardefine:AcrithisItem4|Forma}}<!--\n"
    "-->{{#vardefine:AcrithisItem5|Orokin Reactor}}<!--\n"
    "-->"
)


def test_parse_escaped_html():
    """FlareSolverr 实抓形态（HTML 转义 + <pre> 包裹）。"""
    d = WarframeClient.parse_acrichis_current(RAW_ESCAPED)
    assert d["observed"] == "September 21, 2026"
    assert d["items"] == ["5000 Kuva", "Primary Arcane Adapter",
                          "Orokin Catalyst", "Forma", "Orokin Reactor"]
    assert len(d["valid"]) == 15
    assert set(d["items"]) <= set(d["valid"])


def test_parse_plain_wikitext():
    """纯 wikitext（无 HTML 包裹）也要能解析。"""
    d = WarframeClient.parse_acrichis_current(RAW_PLAIN)
    assert d["items"] == ["5000 Kuva", "Primary Arcane Adapter",
                          "Orokin Catalyst", "Forma", "Orokin Reactor"]


def test_parse_missing_item_rejected():
    """缺任一件 → 空 dict（宁可失败不写半成品）。"""
    bad = RAW_ESCAPED.replace("AcrithisItem5|Orokin Reactor",
                              "AcrithisItem9|Orokin Reactor")
    assert WarframeClient.parse_acrichis_current(bad) == {}


def test_parse_empty_item_rejected():
    bad = RAW_ESCAPED.replace("AcrithisItem3|Orokin Catalyst",
                              "AcrithisItem3|")
    assert WarframeClient.parse_acrichis_current(bad) == {}


def test_parse_empty_input():
    assert WarframeClient.parse_acrichis_current("") == {}
    assert WarframeClient.parse_acrichis_current(None) == {}  # type: ignore[arg-type]


def test_seed_catalog_matches_wiki_valid_list():
    """本地 _en_catalog 必须与 wiki 子页注释的 15 名完全一致 —— 不一致即
    「wiki 改池子」信号，自动刷新会拒绝落盘，此测试负责第一时间报警。"""
    seed = json.loads(pathlib.Path("core/data/de/acrichis_week.json")
                      .read_text(encoding="utf-8"))
    d = WarframeClient.parse_acrichis_current(RAW_ESCAPED)
    assert set(d["valid"]) == set(seed["_en_catalog"])


def test_seed_items_are_catalog_zh():
    """种子快照里本周 5 件必须是 catalog 里英文名的中文映射（防手改漂移）。"""
    seed = json.loads(pathlib.Path("core/data/de/acrichis_week.json")
                      .read_text(encoding="utf-8"))
    cat = seed["_en_catalog"]
    for en in ("5000 Kuva", "Primary Arcane Adapter", "Orokin Catalyst",
               "Forma", "Orokin Reactor"):
        assert any(it["name"] == cat[en]["name"] for it in seed["items"]), en
