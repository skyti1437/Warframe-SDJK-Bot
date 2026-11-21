"""紫卡词条推荐、出售参考、固定配装和实卡重搜。"""

import asyncio
import json
import sys
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import (
    damage_calc,
    damage_engine,
    i18n,
    portable_riven,
    riven_rating,
    riven_sale,
)  # noqa: E402
from core.commands.riven import RivenCommands  # noqa: E402
from core.commands.vision import VisionCommands  # noqa: E402
from core.parser import parse  # noqa: E402
from core.webpanel import coerce_setting, load_schema  # noqa: E402


class Plugin(RivenCommands, VisionCommands):
    def __init__(self, url=""):
        self.cfg = {"damage_engine_mode": "simulation", "riven_sale_sheet_url": url}
        self.riven_sale = riven_sale.RivenSaleSource()

    async def query(self, message):
        parsed = parse(message)
        assert parsed.command == "riventraits"
        return await self._h_riven_traits(parsed, None, "pc")


def test_presets():
    presets = riven_rating.presets()
    assert len(presets) == 646
    assert Counter(mode for modes in presets.values() for mode in modes) == {
        "base": 646,
        "alternate": 84,
        "heavy": 237,
        "cycle": 79,
    }
    assert (
        sum(p["card_origin"] == "observed" for modes in presets.values() for p in modes.values())
        == 9
    )
    for wid, modes in presets.items():
        (weapon,) = riven_rating.match_weapon(wid)
        assert weapon["engine_id"] == wid
        for mode, preset in modes.items():
            fight = preset["fight"]
            assert (fight["weapon"], fight["mode"], preset["sample"]["weapon"]) == (wid, mode, wid)
            assert len(fight["mods"]) >= 8 and riven_rating.CARD not in fight["mods"]
            assert preset["rebuild"]["mods"][:8].count(riven_rating.CARD) == 1
    for query, wid in (
        ("太始弓", "nataruk_perfect"),
        ("斯特朗P", "strun_prime"),
        ("斯特朗", "strun"),
        ("Ack and Brunt", "ack_and_brunt"),
        ("捕月（主武器）", "catchmoon_primary"),
        ("Catchmoon Secondary", "catchmoon_secondary"),
        ("宝拉（匕首）", "balla_one_handed"),
    ):
        (weapon,) = riven_rating.match_weapon(query)
        assert weapon["engine_id"] == wid
    assert {w["engine_id"] for w in riven_rating.match_weapon("捕月")} == {
        "catchmoon_primary",
        "catchmoon_secondary",
    }
    lex = damage_calc.find_weapon_exact("雷克斯 Prime")
    (weapon,) = riven_rating.match_weapon(lex["uniqueName"].rsplit("/", 1)[-1])
    assert weapon["engine_id"] == "lex_prime"


def test_trait_recommendations():
    catalog = riven_rating._trait_catalog()
    assert catalog["protocol"]["denominator"] == "no_riven_reference_mean_kpm"
    assert catalog["protocol"]["runs"] == 128
    cases = catalog["weapons"]
    assert {wid: set(modes) for wid, modes in cases.items()} == {
        wid: set(modes) for wid, modes in riven_rating.presets().items()
    }
    counts = Counter()
    for modes in cases.values():
        for scene in modes.values():
            assert scene["baseline_kpm"] > 0
            for direction in ("positive", "negative"):
                rows = [riven_rating._TraitResult(*row) for row in scene[direction]]
                assert rows and len(rows) == len({row.stat for row in rows})
                for row in rows:
                    modeled = bool(row.flags & riven_rating._TRAIT_MODELED)
                    if modeled:
                        assert row.kpm >= 0 and row.low <= row.pct <= row.high
                    else:
                        assert (row.kpm, row.pct, row.low, row.high) == (None,) * 4
                    counts["modeled" if modeled else "unmodeled"] += 1
                    counts["entries"] += 1
                    counts["unconfirmed"] += int(bool(row.flags & riven_rating._TRAIT_UNCONFIRMED))
                    counts["ineligible"] += int(bool(row.flags & riven_rating._TRAIT_INELIGIBLE))
    for name, count in counts.items():
        assert catalog["counts"][name] == count

    audit = catalog["roll_pool_audit"]
    decisions = audit["decisions"]
    assert len({tuple(d[:4]) for d in decisions}) == len(decisions)
    assert len(decisions) + counts["unconfirmed"] == audit["reviewed"] == 1664
    samples = audit["market_samples"]
    assert samples["min_cards"] == 5
    assert samples["by_weapon"]["glaive"]["impact"] == 1
    assert samples["by_weapon"]["sepfahn_one_handed"]["puncture"] == 16
    assert any(
        w == "balla_one_handed" and s == "impact" and v == "rollable" for w, _, _, s, v in decisions
    )
    for wid, mode, direction, stat, verdict in decisions:
        (checked,) = (row for row in cases[wid][mode][direction] if row[0] == stat)
        flags = checked[3]
        assert stat in {"impact", "puncture", "slash"}
        assert flags & riven_rating._TRAIT_MODELED
        assert not flags & (riven_rating._TRAIT_UNCONFIRMED | riven_rating._TRAIT_SPLICED)
        assert bool(flags & riven_rating._TRAIT_INELIGIBLE) == (verdict == "no_riven")

    burston = cases["burston_prime"]
    normal, cycle = (
        next(
            riven_rating._TraitResult(*row)
            for row in burston[mode]["negative"]
            if row[0] == "magazine_capacity"
        )
        for mode in ("base", "cycle")
    )
    assert normal.high < -20 and cycle.low <= 0 <= cycle.high
    (weapon,) = riven_rating.match_weapon("伯斯顿 Prime")
    assert "基准" in "\n".join(riven_rating.trait_recommendation_lines(weapon, "base"))
    assert "含普通形态充能" in "\n".join(riven_rating.trait_recommendation_lines(weapon, "cycle"))
    assert not riven_rating.trait_recommendation_lines(weapon, "heavy")


def test_holstered_reload():
    catalog = riven_rating._trait_catalog()
    protocol = catalog["holster_reload_protocol"]
    assert protocol["seconds"] == 2
    assert protocol["metric"] == "seconds_after_return_until_full"
    assert protocol["reference_sha256"] == catalog["reference_sha256"]
    counts = Counter()
    for wid, modes in catalog["weapons"].items():
        for mode, scene in modes.items():
            splices = [row for row in scene["positive"] if row[0] == "reload_while_holstered"]
            result = scene.get("holster_reload")
            assert bool(result) == bool(splices)
            if result:
                (splice,) = splices
                assert splice[4:] == [None] * 4
                counts[result["kind"]] += 1
                if "saved" in result:
                    assert abs(result["without"] - result["with"] - result["saved"]) <= 0.01
                (weapon,) = riven_rating.match_weapon(wid)
                lines = riven_rating.trait_recommendation_lines(weapon, mode)
                assert lines and all("收枪装填" not in line for line in lines), (wid, mode, lines)
    assert (
        counts
        == protocol["counts"]
        == {
            "magazine": 442,
            "per_round": 16,
            "battery": 16,
            "sentinel": 19,
            "bow_nock": 35,
            "battery_cooldown": 1,
        }
    )


async def test_sources():
    source = riven_sale.RivenSaleSource()
    weapon = damage_calc.find_weapon_exact("雷克斯 Prime")
    urls = []

    async def get(url, proxy):
        urls.append((url, proxy))
        if url == riven_sale.DEFAULT_URL:
            raise httpx.ConnectError("主通道断开")
        if url == "http://localhost:8000/ref.csv":
            return riven_sale._parse("weapon,positive,negative\nlex,CD,ZOOM\n".encode())
        return riven_sale._parse(
            json.dumps(
                {"weapons": [{"weapon": "lex", "positive": "CC", "negative": "PUNC"}]}
            ).encode()
        )

    with patch.object(riven_sale, "_download", get):
        first = await source.lookup("", weapon, proxy="http://proxy.example:80")
        cached = await source.lookup("", weapon, proxy="http://proxy.example:80")
        assert (
            first == cached and first.row.positive == "CC" and first.url != riven_sale.DEFAULT_URL
        )
        assert len(urls) == 2 and all(proxy for _, proxy in urls)
        local = await source.lookup("http://localhost:8000/ref.csv", weapon, allow_private=True)
        assert local.row.positive == "CD" and len(urls) == 3
        with patch.object(riven_sale, "private_url_reason", return_value="内网"):
            denied = await source.lookup("http://localhost:8000/ref.csv", weapon)
            assert denied.status == "unavailable" and len(urls) == 3
    schema = load_schema()["riven_sale_sheet_url"]
    assert schema["default"] == riven_sale.DEFAULT_URL
    assert coerce_setting(schema, "", key="riven_sale_sheet_url") == riven_sale.DEFAULT_URL
    for bad in ("file:///tmp/ref.json", "http://user:secret@example.com/ref.csv"):
        try:
            coerce_setting(schema, bad, key="riven_sale_sheet_url")
        except ValueError:
            pass
        else:
            raise AssertionError("非法地址被接受")


async def test_trait_sale_source_independent():
    async def download(url, proxy):
        if url.endswith("unavailable.json"):
            raise httpx.ConnectError("不可达")
        title = "代号一" if url.endswith("first.json") else "代号二"
        return (riven_sale.SaleRow("amprex", title, "ZOOM", ""),)

    host = Plugin("https://example.org/first.json")
    host.cfg["allow_private_urls"] = True
    engine = AsyncMock(side_effect=AssertionError("词条推荐不应调用在线求解"))
    with (
        patch.object(damage_engine, "_request", engine),
        patch.object(riven_sale, "_download", download),
    ):
        first = await host.query("词条推荐 安培克斯")
        assert "安培克斯" in first.title and "Amprex" not in first.title
        assert "◆ 单词条模拟推荐" in first.lines
        for name, text in (("second.json", "代号二"), ("unavailable.json", "暂时无法读取")):
            host.cfg["riven_sale_sheet_url"] = "https://example.org/" + name
            reply = await host.query("词条推荐 安培克斯")
            a, b = first.lines.index("◆ 出售参考"), reply.lines.index("◆ 出售参考")
            assert first.lines[:a] == reply.lines[:b]
            assert text in "\n".join(reply.lines[b:])
        low_sample = await host.query("词条推荐 战刃 平砍")
        assert "冲击 ?（1 张）" in "\n".join(low_sample.lines), low_sample.lines
        catalog = riven_rating._trait_catalog()
        audit = catalog["roll_pool_audit"]
        boundary = {"min_cards": 5, "by_weapon": {"glaive": {"impact": 5}}}
        with patch.object(
            riven_rating,
            "_trait_catalog",
            return_value={**catalog, "roll_pool_audit": {**audit, "market_samples": boundary}},
        ):
            enough = await host.query("词条推荐 战刃 平砍")
        assert "冲击 ?" not in "\n".join(enough.lines)
    engine.assert_not_awaited()


async def test_trait_recommendations_keep_unprepared_modes():
    host = Plugin()
    host.cfg["damage_engine_portable_port"] = 8765
    prepared = {
        "fireMode": 0,
        "evaluationTarget": {"enemyName": "Thrax Centurion", "level": 170, "headshotChance": 0.5},
        "recommendedPositiveTiers": [],
        "recommendedNegative": ["变焦"],
        "fatalNegative": [],
        "fatalNegativeAbove": {},
    }
    sale = riven_sale.SaleResult(url="", status="unavailable")

    async def query(message):
        parsed = parse(message)
        assert parsed.command == "riventraits"
        return "\n".join((await host._h_riven_traits(parsed, None, "pc")).lines)

    with (
        patch.object(portable_riven, "prepared_result", return_value=prepared) as load_prepared,
        patch.object(host.riven_sale, "lookup", return_value=sale) as lookup_sale,
    ):
        combined = await query("词条推荐 伯斯顿 Prime")
        assert "凶魂百夫长" in combined and "低影响负面　变焦" in combined
        assert "灵化循环（含普通形态充能）" in combined
        load_prepared.assert_awaited_once_with(host, portable_riven.MODES["burston_prime", "base"])
        lookup_sale.assert_awaited_once()

        cycle = await query("词条推荐 伯斯顿 Prime 灵化循环")
        assert "灵化循环（含普通形态充能）" in cycle and "凶魂百夫长" not in cycle
        assert load_prepared.await_count == 1


async def test_trait_matrix_keeps_configured_sale_reference_without_querying_listings():
    host = Plugin()
    host.client = SimpleNamespace(wm_riven_auctions=AsyncMock())
    sale = riven_sale.SaleResult(
        url="https://example.com/riven_sale.json",
        status="ok",
        row=riven_sale.SaleRow("Strun", "多重", "变焦", "出售说明"),
    )
    with patch.object(host.riven_sale, "lookup", return_value=sale) as lookup:
        parsed = parse("词条推荐 斯特朗")
        assert parsed.command == "riventraits"
        reply = await host._h_riven_traits(parsed, None, "pc")
    body = "\n".join(reply.lines)
    assert "◆ 随包参考配装 · 全词条" in body
    assert all(f"{name}⟦/" in body for name in ("正词条", "负词条", "融合词条"))
    assert "市场挂牌" not in body
    assert "出售参考" in body and "出售说明" in body
    assert sale.url in reply.extra_text
    lookup.assert_awaited_once()
    host.client.wm_riven_auctions.assert_not_awaited()


async def test_recommendation_weapon_aliases():
    host = Plugin()
    host.cfg["damage_engine_mode"] = "builtin"
    sale = riven_sale.SaleResult("", "missing")
    with patch.object(host.riven_sale, "lookup", return_value=sale):
        for label, aliases in (
            ("布莱顿 Prime", ("布莱顿p", "布莱顿 \uff30版", "Braton P")),
            ("野猪 Prime", ("野猪\uff30",)),
            ("MK1-布莱顿", ("布莱顿MK1", "MK1 布莱顿")),
            ("布莱顿·破坏者", ("布莱顿破坏者", "布莱顿Vandal")),
            ("赤毒·沙皇", ("沙皇赤毒", "沙皇Kuva")),
            ("棱晶·空刃", ("棱镜空刃", "空刃棱镜")),
            ("保障·勒克塔", ("安全勒克塔",)),
        ):
            for alias in aliases:
                traits = await host.query(f"词条推荐 {alias}")
                build = await host._h_loadout_recommend(parse(f"配卡推荐 {alias}"), None, "pc")
                assert traits.title == f"词条推荐 · {label}", (alias, traits.title)
                assert build.title == f"配卡推荐 · {label}", (alias, build.title)
                assert "◆ 无紫卡参考配装" in build.lines
        assert (await host.query("词条推荐 布莱顿")).title == "词条推荐 · 布莱顿"
        assert (await host.query("词条推荐 安培克斯p")).title == "词条推荐"


async def test_reference_loadout_entry():
    host = Plugin()
    host.cfg["damage_engine_mode"] = "estimate"
    engine = AsyncMock(side_effect=AssertionError("参考配装查询不应请求引擎"))

    async def query(message):
        parsed = parse(message)
        assert parsed.command == "loadoutrecommend"
        return await host._h_loadout_recommend(parsed, None, "pc")

    with patch.object(damage_engine, "_request", engine):
        for cmd, wid, mode, label in (
            ("配卡推荐 安培克斯", "amprex", "base", "常态"),
            ("配卡推荐 野猪 Prime 主射", "boar_prime", "base", "常态"),
            ("配卡推荐 野猪 Prime 灵化循环", "boar_prime", "cycle", "灵化循环"),
            ("配卡推荐 太始弓 副射", "nataruk_perfect", "alternate", "副射"),
            ("配卡推荐 信条·集议 重击", "tenet_agendus", "heavy", "重击"),
        ):
            reply = await query(cmd)
            fight = riven_rating.presets()[wid][mode]["fight"]
            assert label in reply.lines[0] and "◆ 无紫卡参考配装" in reply.lines
            assert reply.lines[2] == "· " + " / ".join(
                riven_rating._mod_name(mid) for mid in fight["mods"][:8]
            )
            assert any("KPM" in line for line in reply.lines)
            if fight["evolutions"]:
                assert any(line.startswith("· 进化 ") for line in reply.lines)
        assert "主要·准星（5级）" in "\n".join((await query("配卡推荐 安培克斯")).lines)
        assert "该武器可用模式" in "\n".join((await query("配卡推荐 太始弓 重击")).lines)
        kitgun = await query("配卡推荐 捕月")
        assert "捕月（主武器）" in "\n".join(kitgun.lines)
        assert "捕月（副武器）" in "\n".join(kitgun.lines)
        assert "请指定使用模式" in "\n".join(
            (await query("配卡推荐 安培克斯 暴击100 暴伤100")).lines
        )
    engine.assert_not_awaited()


async def test_loadout_entry():
    fight = riven_rating.presets()["torid"]["cycle"]["fight"]
    regular = fight["mods"][:8]
    calls = []
    host = Plugin()
    host.cfg["scan_cooldown"] = 0
    host._ocr_last = {}
    host._ocr_busy = set()
    host._safe_sender = lambda event: "test"
    image = type("Image", (), {})()
    event = SimpleNamespace(message_obj=SimpleNamespace(message=[image]))
    lines = [
        "Torid Crita-caritis",
        "+185,5% Critical Chance",
        "+111,2% Viral",
        "+148,1% Critical Damage",
        "-41,4% Magazine Capacity",
    ]
    observed = {"weapon": "Torid Crita-caritis", "rank": 8, "polarity": "naramon", "lines": lines}

    async def request(cfg, path, body, timeout=12):
        calls.append((path, json.loads(json.dumps(body))))
        if path == "/api/riven":
            traits = body["bonuses"] + [body["malus"]]
            return {
                "ok": True,
                "stats": [
                    {
                        "id": stat["id"],
                        "shown": stat["value"],
                        "roll": stat["value"] / 200,
                        "modeled": True,
                        "decimals": 1,
                        "unit": "%",
                    }
                    for stat in traits
                ],
            }
        if path == damage_engine.SEARCH_META_PATH:
            return {
                "weapons": [
                    {
                        "id": "torid",
                        "evolutions": [
                            {"tier": tier, "options": [{"id": evo}]}
                            for tier, evo in enumerate(fight["evolutions"], 1)
                        ],
                    }
                ],
                "arcanes": [{"id": arc, "max_rank": 5} for arc in fight["arcane"]],
                "mod_pools": {},
            }
        if path == "/api/candidates":
            return {"candidates": [{"id": mod} for mod in regular]}
        if path == damage_engine.SEARCH_PATH:
            lock = body["rivens"][0]
            assert lock["spec"]["rank"] == 8 and lock["spec"]["polarity"] == "naramon"
            assert body["modes"] == {"cycle": "fixed"}
            assert body["mods"][riven_rating.CARD] == "fixed"
            assert all(body["mods"][mod] == "search" for mod in regular)
            replay = {
                **fight,
                "mods": [riven_rating.CARD, *regular[1:], *fight["mods"][8:]],
                "rivens": [lock],
            }
            return {"searched": 48, "results": [{"replay": replay}]}
        assert path == "/api/simulate"
        score = 10 if "rivens" not in body else 12 + body["rivens"][0]["spec"]["bonuses"][0]["roll"]
        return {
            "score": score,
            "score_runs": [score] * body["runs"],
            "duration": body["duration"],
            "forma": {"legal": True},
        }

    async def query(command, message=None):
        parsed = parse(command)
        assert parsed.command == "loadoutrecommend"
        return await host._h_loadout_recommend(parsed, message, "pc")

    with (
        patch.object(damage_engine, "_request", request),
        patch.object(host, "_image_data_urls", new=AsyncMock(return_value=["image"])),
        patch.object(host, "_extract_riven_from_image", new=AsyncMock(return_value=observed)),
    ):
        photo = await query("配卡推荐 灵化循环", event)
        assert "◆ 配装顺序" in photo.lines, photo
        first = tuple(calls)
        calls.clear()
        typed = (
            "配卡推荐 托里德 灵化循环 暴击185.5 病毒111.2 暴伤148.1 负弹匣41.4 等级8 极性=naramon"
        )
        text = await query(typed)
        assert text.lines == photo.lines and tuple(calls) == first
        calls.clear()
        changed = await query(typed.replace("185.5", "169.5"))
        assert "◆ 配装顺序" in changed.lines and changed.lines != text.lines
        assert [path for path, _ in calls] == [path for path, _ in first]
        old_search = dict(first)[damage_engine.SEARCH_PATH]
        new_search = dict(calls)[damage_engine.SEARCH_PATH]
        assert old_search["rivens"] != new_search["rivens"]
        assert old_search.keys() == new_search.keys()
        assert all(old_search[key] == new_search[key] for key in old_search if key != "rivens")
        calls.clear()
        bad = await query("配卡推荐 斯特朗 灵化循环", event)
        assert "家族不匹配" in "\n".join(bad.lines) and not calls


async def test_image_chooses_best_supported_variant():
    host = Plugin()
    host.cfg["damage_engine_portable_port"] = 8765
    image = type("Image", (), {})()
    event = SimpleNamespace(message_obj=SimpleNamespace(message=[image]))
    fields = SimpleNamespace(
        weapon="Boar", positive=[("multishot", 138.9), ("gas_damage", 104.3)], negative=[]
    )
    observed = {"rank": 8, "polarity": "madurai"}
    modes = {
        ("boar", "base"): ("Boar", 0, 0.0),
        ("boar_prime", "incarnon"): portable_riven.MODES["boar_prime", "incarnon"],
        ("braton_prime", "base"): portable_riven.MODES["braton_prime", "base"],
    }
    replies = {
        "boar": {"scoringValues": {"actual": 700}, "searchWork": {"limited": 0}},
        "boar_prime": {"scoringValues": {"actual": 400}, "searchWork": {"limited": 0}},
    }

    async def solve(_host, _event, port, mode, card, polarity, **kwargs):
        assert port == 8765 and polarity == "madurai" and card["rank"] == 8
        assert card["bonuses"][0] == {"id": "multishot", "value": 138.9}
        assert card["weapon"] in replies and mode in modes.values()
        return replies[card["weapon"]]

    async def query(command):
        parsed = parse(command)
        assert parsed.command == "loadoutrecommend"
        return await host._h_loadout_recommend(parsed, event, "pc")

    with (
        patch.object(host, "_confirmed_riven_image", return_value=(fields, observed)),
        patch.object(portable_riven, "MODES", modes),
        patch.object(portable_riven, "analyze", side_effect=solve) as search,
        patch.object(portable_riven, "build_view", return_value=["◆ 推荐配装"]) as view,
    ):
        best = await query("配卡推荐")
        assert best.title == "配卡推荐 · 野猪"
        assert "已比较 2 个变体" in "\n".join(best.lines)
        assert search.await_count == 2
        view.assert_awaited_once_with(host, 8765, replies["boar"], with_riven=True)
        search.reset_mock()
        picked = await query("配卡推荐 野猪 Prime")
        assert picked.title == "配卡推荐 · 野猪 Prime"
        assert search.await_count == 1
        search.reset_mock()
        incarnon = await query("配卡推荐 灵化形态")
        assert incarnon.title == "配卡推荐 · 野猪 Prime"
        assert search.await_count == 1
        search.reset_mock()
        bad = await query("配卡推荐 布莱顿 Prime")
        assert "家族不匹配" in "\n".join(bad.lines)
        search.assert_not_awaited()
        replies["boar"]["searchWork"]["limited"] = 1
        partial = await query("配卡推荐")
        assert partial.title == "配卡推荐 · 野猪 Prime"
        assert "部分候选未完成" in "\n".join(partial.lines)
    for entry in ("紫卡评级 野猪", "紫卡推荐 野猪", "rivenrating Boar", "rivenrecommend Boar"):
        parsed = parse(entry)
        assert parsed is None or parsed.command != "rivenrating"


def test_loadout_roll_pool():
    data = riven_rating._trait_catalog()
    accepted = {
        (weapon, mode, direction, stat)
        for weapon, mode, direction, stat, verdict in data["roll_pool_audit"]["decisions"]
        if verdict == "rollable"
    }
    counts = Counter()
    for weapon, modes in data["weapons"].items():
        for mode, scene in modes.items():
            fight = {"weapon": weapon, "mode": mode}
            for direction in ("positive", "negative"):
                for stat, _, _, flags, *_ in scene[direction]:
                    allowed = (weapon, mode, direction, stat) in accepted
                    if not (
                        allowed
                        or flags
                        & (riven_rating._TRAIT_UNCONFIRMED | riven_rating._TRAIT_INELIGIBLE)
                    ):
                        continue
                    positive = direction == "positive"
                    query = {
                        "weapon": weapon,
                        "bonuses": [{"id": stat}] if positive else [],
                        "malus": None if positive else {"id": stat},
                    }
                    eligible = riven_rating._audited_unconfirmed(query, fight)
                    assert eligible == (frozenset({stat}) if allowed else frozenset())
                    reply = {"stats": [{"modeled": True}], "unconfirmed": [stat]}
                    usable = riven_rating._usable_card
                    assert usable(reply, 1, allowed_unconfirmed=eligible) == allowed
                    assert not usable({**reply, "illegal": [stat]}, 1, allowed_unconfirmed=eligible)
                    assert not usable(
                        {**reply, "stats": [{"modeled": False}]}, 1, allowed_unconfirmed=eligible
                    )
                    counts[allowed] += 1
    assert counts[True] == len(accepted) == 224
    assert {
        (wid, mode, "negative", stat)
        for wid, stat in (
            ("tatsu", "impact"),
            ("tatsu_prime", "impact"),
            ("mire", "puncture"),
            ("coda_mire", "puncture"),
            ("nunchasa", "slash"),
            ("tenet_agendus", "slash"),
        )
        for mode in data["weapons"][wid]
    } <= accepted
    assert counts[False] == data["counts"]["unconfirmed"] + data["counts"]["ineligible"]
    query = {"weapon": "balla_one_handed", "bonuses": [{"id": "impact"}], "malus": None}
    assert not riven_rating._audited_unconfirmed(
        query, {"weapon": "balla_one_handed", "mode": "cycle"}
    )
    assert not riven_rating._audited_unconfirmed(
        query, {"weapon": "balla_two_handed", "mode": "base"}
    )


def test_portable_card_language_and_removed_impact():
    scenario = {
        "fireMode": 1,
        "evaluationTarget": {"enemyName": "Thrax Centurion", "level": 170, "headshotChance": 0},
    }
    assert "凶魂百夫长" in portable_riven.scenario_line(scenario)
    assert portable_riven._impact_label(542387.19, 28128.14) == "28,128 · ⟦m⟧-94.8%⟦/m⟧"
    assert portable_riven._impact_label(100, 110) == "110 · ⟦y⟧+10.0%⟦/y⟧"
    stats = dict(zip(damage_engine.PANEL_FIELDS, (30, 1.2, 5.68, 0.432, 8, 1.5)))
    panel = damage_engine.weapon_panel_lines(stats)
    assert len(panel) == 3 and "120.0%" in panel[1] and "射速 8.00/秒" in panel[2]
    stats["damageTypes"] = {"DT_IMPACT": 5, "DT_SLASH": 5, "DT_VIRAL": 10, "DT_FIRE": 0}
    panel = damage_engine.weapon_panel_lines(stats)
    assert panel[3:] == [
        "· 物理 50.0% · 冲击 7.5 (25.0%) / 切割 7.5 (25.0%)",
        "· 元素 50.0% · 病毒 15.0 (50.0%)",
    ]
    assert len(damage_engine.weapon_panel_lines({**stats, "damageTypes": {"DT_IMPACT": 5}})) == 3
    token = i18n.set_lang("en")
    try:
        assert "Incarnon form" in portable_riven.scenario_line(scenario)
        assert "Thrax Centurion" in portable_riven.scenario_line(scenario)
        assert damage_engine.weapon_panel_lines(stats)[0] == "◆ Weapon stats"
        assert damage_engine.weapon_panel_lines(stats)[3:] == [
            "· Physical 50.0% · Impact 7.5 (25.0%) / Slash 7.5 (25.0%)",
            "· Elemental 50.0% · Viral 15.0 (50.0%)",
        ]
    finally:
        i18n.reset_lang(token)


def test_portable_enemy_labels_and_english_evolutions():
    butcher = "/Lotus/Types/Enemies/Grineer/AIWeek/Avatars/BladeSawmanAvatar"
    ambulas = "/Lotus/Types/Enemies/Corpus/Vip/Ambulas/BossAmbulasRangedAvatar"
    torid = {
        "path": "/Lotus/Weapons/ClanTech/Bio/BioWeapon",
        "nameEn": "Torid",
        "incarnon": [
            {"options": [{"id": "3-1", "label": "迅速判决", "description": "投射物速度 +50%"}]}
        ],
    }
    with i18n.use_lang("zh"):
        assert portable_riven.enemy_label(butcher, "Butcher") == "屠夫"
        assert portable_riven.enemy_label(ambulas, "Ambulas", "Ambulas") == "智踱恐鸟"
        assert portable_riven.enemy_label("/unknown", "Unknown") == "Unknown"
        assert (
            "迅速判决 · 投射物速度 +50%"
            in portable_riven.evolution_lines(torid, {"tier3": "3-1"}, 0)[1]
        )
    with i18n.use_lang("en"):
        assert portable_riven.enemy_label(butcher, "Butcher") == "Butcher"
        assert portable_riven.enemy_label(ambulas, "Ambulas") == "Ambulas"
        assert portable_riven.evolution_lines(torid, {"tier3": "3-1"}, 0)[1] == (
            "· 3-1 Swift Deliverance · 50% Projectile Speed"
        )
        fallback = portable_riven.evolution_lines(
            {**torid, "path": "/unknown"}, {"tier3": "3-1"}, 0
        )
        assert fallback[1] == "· 3-1 · Effect description unavailable"


def test_offline_riven_comparison_context():
    catalog = portable_riven._comparison_catalog()
    assert len(catalog) == len(portable_riven.MODES) == 6
    weapon, fire_mode, _ = portable_riven.MODES["braton_prime", "base"]
    (context,) = [key[2] for key in catalog if key[:2] == (weapon, fire_mode)]
    assert catalog[weapon, fire_mode, context]["replayed"] >= 2
    result = {
        "engineFingerprint": portable_riven.FINGERPRINT,
        "scoreMetric": "windowedDps",
        "weapon": {"path": weapon},
        "fireMode": fire_mode,
        "contextHash": context,
        "searchWork": {"limited": 0},
        "scoringValues": {"baseline": 54806, "actual": 45407},
    }
    lines = portable_riven.comparison_lines(result)
    assert "无紫卡 54,806 / 当前紫卡 45,407 / 参考紫卡 136,046 DPS" in lines[1]
    assert "模拟满值" in lines[2]
    assert not portable_riven.comparison_lines({**result, "contextHash": "another-target"})
    assert not portable_riven.comparison_lines({**result, "engineFingerprint": "old-engine"})
    assert not portable_riven.comparison_lines({**result, "searchWork": {"limited": 1}})
    prepared = portable_riven.prepared_traits(result)
    assert prepared is not None
    assert sum(map(len, prepared[1].values())) == 55
    (entry,) = riven_rating.match_weapon("布莱顿 Prime")
    matrix = riven_rating.trait_matrix(entry, ["base", "cycle"], {"base": prepared})
    assert matrix[0] == "◆ 全词条对照"
    assert "词条　牌面中位值　主射 DPS　灵化循环 KPM" in matrix
    assert not any("percent" in line or "flat" in line for line in matrix)
    assert any("穿透" in line and " 米" in line for line in matrix)
    assert any("⟦m⟧-" in line for line in matrix) and any("⟦y⟧+" in line for line in matrix)
    assert portable_riven.prepared_traits({**result, "contextHash": "another-target"}) is None


def test_reference_overflow_fits_without_rearranging_mods():
    request = {
        "slotPolarities": [None, "AP_TACTIC"],
        "mods": [{"path": "/MockMod", "slot": 1, "rank": 10}],
    }
    validation = {
        "capacity": 65,
        "capacityLimit": 60,
        "assignments": [{"slot": 1, "drain": 20, "rawDrain": 16, "modPolarity": "AP_ATTACK"}],
    }
    fitted = portable_riven._fit_reference_slots(request, validation)
    assert fitted["slotPolarities"] == [None, "AP_ATTACK"]
    assert request["slotPolarities"] == [None, "AP_TACTIC"]
    assert fitted["mods"] == request["mods"]


if __name__ == "__main__":
    test_presets()
    test_trait_recommendations()
    test_holstered_reload()
    asyncio.run(test_sources())
    asyncio.run(test_trait_sale_source_independent())
    asyncio.run(test_trait_recommendations_keep_unprepared_modes())
    asyncio.run(test_trait_matrix_keeps_configured_sale_reference_without_querying_listings())
    asyncio.run(test_recommendation_weapon_aliases())
    asyncio.run(test_reference_loadout_entry())
    asyncio.run(test_loadout_entry())
    asyncio.run(test_image_chooses_best_supported_variant())
    test_loadout_roll_pool()
    test_portable_card_language_and_removed_impact()
    test_portable_enemy_labels_and_english_evolutions()
    test_offline_riven_comparison_context()
    test_reference_overflow_fits_without_rearranging_mods()
    scenes = sum(map(len, riven_rating.presets().values()))
    print(f"{scenes:,} 项预设与单词条数据、图片锁卡与实值变更重搜通过")
