# -*- coding: utf-8 -*-
"""言录使当期货单解析器（parse_acrichis_current）测试。

子页 https://wiki.warframe.com/w/Acrithis/Current_Offerings 是社区人工维护的
「本周 5 件」上报（vardefine 模板），2026-09-21 起作为自动刷新数据源。
样例取自 2026-09-21 实抓（FlareSolverr 转义形态 + 纯 wikitext 两种都要能吃）。
"""

import asyncio
import json
import locale
import subprocess
import tempfile
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from core.api_client import WarframeClient, community_acrithis_ok  # noqa: E402


ENGLISH_MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)

RAW_ESCAPED = (
    '<html><head><meta name="color-scheme" content="light dark"></head>'
    '<body><pre style="word-wrap: break-word; white-space: pre-wrap;">'
    "&lt;!-- Valid item names for AcrithisItem1\u20135 below (must match "
    "exactly, including capitalization):\n"
    "Orokin Reactor\nOrokin Catalyst\nExilus Warframe Adapter\n"
    "Exilus Weapon Adapter\nPrimary Arcane Adapter\nSecondary Arcane Adapter\n"
    "Forma\nMelee Riven Mod\nPistol Riven Mod\nRifle Riven Mod\n"
    "Kitgun Riven Mod\nZaw Riven Mod\nShotgun Riven Mod\n"
    "Companion Weapon Riven Mod\n10,000 Kuva\n"
    "--&gt;{{#vardefine:AcrithisObserved|September 21, 2026}}&lt;!--\n"
    "--&gt;{{#vardefine:AcrithisItem1|10,000 Kuva}}&lt;!--\n"
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
    "Companion Weapon Riven Mod\n10,000 Kuva\n"
    "-->{{#vardefine:AcrithisObserved|September 21, 2026}}<!--\n"
    "-->{{#vardefine:AcrithisItem1|10,000 Kuva}}<!--\n"
    "-->{{#vardefine:AcrithisItem2|Primary Arcane Adapter}}<!--\n"
    "-->{{#vardefine:AcrithisItem3|Orokin Catalyst}}<!--\n"
    "-->{{#vardefine:AcrithisItem4|Forma}}<!--\n"
    "-->{{#vardefine:AcrithisItem5|Orokin Reactor}}<!--\n"
    "-->"
)

# ★ 2026-10-05 实抓形态：wiki 编辑把 AcrithisObserved 从字面日期改成了
#   MediaWiki 魔法词（页面自注「Current UTC date (copy this into
#   AcrithisObserved)」）。旧解析按 [^}]* 只截到 "{{CURRENTMONTHNAME" ⇒
#   日期解析必失败、刷新恒 failed、新货单永远落不了盘。
RAW_MW_TEMPLATE = (
    "<!-- Valid item names for AcrithisItem1\u20135 below (must match exactly, "
    "including capitalization):\n"
    "Orokin Reactor\nOrokin Catalyst\nExilus Warframe Adapter\n"
    "Exilus Weapon Adapter\nPrimary Arcane Adapter\nSecondary Arcane Adapter\n"
    "Forma\nMelee Riven Mod\nPistol Riven Mod\nRifle Riven Mod\n"
    "Kitgun Riven Mod\nZaw Riven Mod\nShotgun Riven Mod\n"
    "Companion Weapon Riven Mod\n10,000 Kuva\n"
    "-->{{#vardefine:AcrithisObserved|{{CURRENTMONTHNAME}} {{CURRENTDAY}}, "
    "{{CURRENTYEAR}}}}<!--\n"
    "-->{{#vardefine:AcrithisItem1|10,000 Kuva}}<!--\n"
    "-->{{#vardefine:AcrithisItem2|Exilus Weapon Adapter}}<!--\n"
    "-->{{#vardefine:AcrithisItem3|Primary Arcane Adapter}}<!--\n"
    "-->{{#vardefine:AcrithisItem4|Orokin Reactor}}<!--\n"
    "-->{{#vardefine:AcrithisItem5|Orokin Catalyst}}<!--\n"
    "-->"
)


def test_parse_mw_current_template_observed():
    """★ 2026-10-05：魔法词形态的 observed 必须展开为**当前 UTC 日期**。"""
    from datetime import datetime, timezone

    d = WarframeClient.parse_acrichis_current(RAW_MW_TEMPLATE)
    now = datetime.now(timezone.utc)
    assert d["observed"] == f"{ENGLISH_MONTHS[now.month - 1]} {now.day}, {now.year}", d["observed"]
    assert d["items"] == [
        "10,000 Kuva",
        "Exilus Weapon Adapter",
        "Primary Arcane Adapter",
        "Orokin Reactor",
        "Orokin Catalyst",
    ]
    assert len(d["valid"]) == 15


def test_expand_mw_current_passthrough():
    """不含魔法词的字面日期原样返回（老格式不回归）。"""
    assert WarframeClient._expand_mw_current("September 21, 2026") == "September 21, 2026"


def test_parse_escaped_html():
    """FlareSolverr 实抓形态（HTML 转义 + <pre> 包裹）。"""
    d = WarframeClient.parse_acrichis_current(RAW_ESCAPED)
    assert d["observed"] == "September 21, 2026"
    assert d["items"] == [
        "10,000 Kuva",
        "Primary Arcane Adapter",
        "Orokin Catalyst",
        "Forma",
        "Orokin Reactor",
    ]
    assert len(d["valid"]) == 15
    assert set(d["items"]) <= set(d["valid"])


def test_parse_plain_wikitext():
    """纯 wikitext（无 HTML 包裹）也要能解析。"""
    d = WarframeClient.parse_acrichis_current(RAW_PLAIN)
    assert d["items"] == [
        "10,000 Kuva",
        "Primary Arcane Adapter",
        "Orokin Catalyst",
        "Forma",
        "Orokin Reactor",
    ]


def test_parse_missing_item_rejected():
    """缺任一件 → 空 dict（宁可失败不写半成品）。"""
    bad = RAW_ESCAPED.replace("AcrithisItem5|Orokin Reactor", "AcrithisItem9|Orokin Reactor")
    assert WarframeClient.parse_acrichis_current(bad) == {}


def test_parse_empty_item_rejected():
    bad = RAW_ESCAPED.replace("AcrithisItem3|Orokin Catalyst", "AcrithisItem3|")
    assert WarframeClient.parse_acrichis_current(bad) == {}


def test_parse_empty_input():
    assert WarframeClient.parse_acrichis_current("") == {}
    assert WarframeClient.parse_acrichis_current(None) == {}  # type: ignore[arg-type]


def test_seed_catalog_matches_wiki_valid_list():
    """本地 _en_catalog 必须与 wiki 子页注释的 15 名完全一致 —— 不一致即
    「wiki 改池子」信号，自动刷新会拒绝落盘，此测试负责第一时间报警。"""
    seed = json.loads(pathlib.Path("core/data/de/acrichis_week.json").read_text(encoding="utf-8"))
    d = WarframeClient.parse_acrichis_current(RAW_ESCAPED)
    assert set(d["valid"]) == set(seed["_en_catalog"])


def test_seed_items_are_catalog_zh():
    """随包只留静态候选目录，不携带当周动态货单。"""
    seed = json.loads(pathlib.Path("core/data/de/acrichis_week.json").read_text(encoding="utf-8"))
    assert not set(seed) & {"items", "expiry", "observed", "source"}
    assert len(seed["_en_catalog"]) == 15
    assert all(it["name"] and it["price"] > 0 for it in seed["_en_catalog"].values())


class _FixedDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 10, 28, 12, tzinfo=timezone.utc).astimezone(tz)


def _check_acrithis_locale(locale_name):
    """只在子进程选 locale；通过真实刷新入口读取夹具、向临时目录落盘。"""
    locale.setlocale(locale.LC_TIME, locale_name)
    before = locale.setlocale(locale.LC_TIME)
    now = _FixedDatetime.now(timezone.utc)
    cases = (
        ("October 26, 2026", True, "refreshed"),
        ("October 25, 2026", False, "not-updated"),
        ("2026-10-26", True, "refreshed"),
        ("2026-10-25", False, "not-updated"),
        ("October 32, 2026", True, "failed"),
        ("February 29, 2026", True, "failed"),
        ("not a date", True, "failed"),
        ("", True, "failed"),
    )
    seed_path = pathlib.Path(__file__).resolve().parents[1] / "core/data/de/acrichis_week.json"
    seed = json.loads(seed_path.read_text(encoding="utf-8"))
    seed.update(expiry="2026-10-19T00:00:00+00:00", items=[])
    with patch("datetime.datetime", _FixedDatetime), tempfile.TemporaryDirectory() as tmp:
        expanded = WarframeClient.parse_acrichis_current(RAW_MW_TEMPLATE)
        assert expanded["observed"] == "October 28, 2026", expanded
        for observed, community_ok, refresh_status in cases:
            payload = {
                "expiry": "2026-11-02T00:00:00+00:00",
                "observed": observed,
                "items": [{"name": "测试物品", "price": 10}],
            }
            ok, reason = community_acrithis_ok(payload, now=now)
            assert ok is community_ok, (locale_name, observed, ok, reason)
            payload["expiry"] = "2026-10-25T00:00:00+00:00"
            assert community_acrithis_ok(payload, now=now)[0] is False
            target = pathlib.Path(tmp) / "acrithis_week.json"
            original = json.dumps(seed, ensure_ascii=False)
            target.write_text(original, encoding="utf-8")
            client = WarframeClient.__new__(WarframeClient)
            raw = RAW_PLAIN.replace("September 21, 2026", observed)
            client.fetch_wiki_source = AsyncMock(return_value=raw)
            client._acrichis_page_revision = AsyncMock(return_value=now)
            with (
                patch("core.api_client.paths.run_dir", return_value=pathlib.Path(tmp)),
                patch("core.api_client.paths.read_path", return_value=target),
                patch("core.api_client.paths.write_path", return_value=target),
            ):
                status = asyncio.run(client.refresh_acrichis_week())
            assert status == refresh_status, (locale_name, observed, status)
            client.fetch_wiki_source.assert_awaited_once()
            if status == "refreshed":
                written = json.loads(target.read_text(encoding="utf-8"))
                assert written["observed"] == observed
                assert len(written["items"]) == 5
                assert "静态候选目录" in written["_price_note"]
                assert all(it["price"] > 0 for it in written["items"])
            else:
                assert target.read_text(encoding="utf-8") == original
            assert locale.setlocale(locale.LC_TIME) == before
            print(f"[LOCALE {locale_name}] {observed!r}: community={ok}, refresh={status}")


def test_acrithis_locale_independent_refresh():
    """C、中文、日文环境覆盖有效/陈旧/无效日期，保留社区 observed 解析失败降级。"""
    for name in ("C", "zh_CN.UTF-8", "ja_JP.UTF-8"):
        command = (
            "import locale, runpy, sys; "
            "ns = runpy.run_path(sys.argv[1]); "
            "ns['_check_acrithis_locale'](sys.argv[2])"
        )
        result = subprocess.run(
            [sys.executable, "-c", command, str(pathlib.Path(__file__).resolve()), name],
            capture_output=True,
            text=True,
            check=False,
        )
        if "unsupported locale setting" in result.stderr and name != "C":
            print(f"[SKIP] 系统未安装 LC_TIME={name}")
            continue
        assert result.returncode == 0, result.stdout + result.stderr
        print(result.stdout, end="")


def test_runtime_without_catalog_and_invalid_cache():
    """首装缺值主动获取；运行副本只回填静态目录，合法缓存不被删除。"""
    from core import paths
    from core.api_client import ACRITHIS_WEEK_NAME
    from datetime import timedelta

    async def run():
        with tempfile.TemporaryDirectory() as tmp:
            paths.set_run_dir(tmp)
            client = WarframeClient()
            target = pathlib.Path(tmp) / ACRITHIS_WEEK_NAME
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                assert client.acrithis_week_expired()
                assert not await client.acrithis_week()
                client._community_json = AsyncMock(
                    return_value={
                        "expiry": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
                        "items": [{"name": "测试物品", "price": 10}],
                    }
                )
                assert await client.refresh_acrichis_week(community_only=True) == "refreshed"
                saved = target.read_bytes()
                assert not client.acrithis_week_expired()
                assert await client.refresh_acrichis_week(community_only=True) == "fresh"
                assert target.read_bytes() == saved
                for bad in (
                    [],
                    [None],
                    [{"name": "物品"}],
                    [{"name": "物品", "price": 10, "qty": "坏值"}],
                    [{"name": "物品", "price": True}],
                    [{"name": "物品", "price": -1}],
                ):
                    target.write_text(
                        json.dumps(
                            {
                                "expiry": (
                                    datetime.now(timezone.utc) + timedelta(days=1)
                                ).isoformat(),
                                "items": bad,
                            }
                        )
                    )
                    assert client.acrithis_week_expired()
                    assert not await client.acrithis_week()
                    assert client._acrichis_snapshot()["_en_catalog"]
                target.write_text(
                    json.dumps({"expiry": "2020-01-01T00:00:00+00:00", "items": [{"name": "旧货"}]})
                )
                old = target.read_bytes()
                client._community_json = AsyncMock(return_value=None)
                assert await client.refresh_acrichis_week(community_only=True) == "failed"
                assert target.read_bytes() == old
            finally:
                await client.close()
                paths.set_run_dir(None)

    asyncio.run(run())


def test_offline_refresh_never_writes_seed():
    """真实仓库先只读原字节；未注入目录时各采用入口都拒绝，正常写由运行目录测试覆盖。"""
    from core import paths
    from core.api_client import ACRITHIS_WEEK_NAME

    async def run():
        paths.set_run_dir(None)
        seed_path = paths.PKG_DATA / ACRITHIS_WEEK_NAME
        original = seed_path.read_bytes()
        client = WarframeClient()
        client.fetch_wiki_source = AsyncMock(side_effect=AssertionError("离线护栏应先于联网"))
        client._community_json = AsyncMock(side_effect=AssertionError("离线护栏应先于联网"))
        try:
            with patch("core.api_client.paths.atomic_write_text") as write:
                assert await client.refresh_acrichis_week() == "failed"
                assert await client._community_acrithis_week({}) == "failed"
                assert client._adopt_acrichis_source({}, RAW_PLAIN, None, True) == "failed"
                result = await client._check_rotation_revisions_locked()
                assert result["acrithis"] == "failed"
                write.assert_not_called()
            client.fetch_wiki_source.assert_not_awaited()
            client._community_json.assert_not_awaited()
            assert seed_path.read_bytes() == original
        finally:
            await client.close()

    asyncio.run(run())


if __name__ == "__main__":
    # 本文件是 pytest 风格（test_* + assert）；补一个脚本入口，保证
    # 「python tests/test_*.py」全量跑法也能真正执行这些断言
    # （2026-10-05 立：此前脚本方式跑等于静默跳过）。
    import traceback

    _failed: list[str] = []
    for _name in sorted(list(globals())):
        if not _name.startswith("test_"):
            continue
        _fn = globals()[_name]
        if not callable(_fn):
            continue
        try:
            _fn()
            print(f"[PASS] {_name}")
        except Exception:  # noqa: BLE001
            _failed.append(_name)
            print(f"[FAIL] {_name}")
            traceback.print_exc()
    if _failed:
        print(f"\n失败 {len(_failed)} 项：{_failed}")
        sys.exit(1)
    print("\n全部通过 ✔")
