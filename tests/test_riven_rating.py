"""紫卡词条推荐、出售参考、固定配装和实卡重搜。"""

import asyncio
import json
import os
import sys
from copy import deepcopy
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import (
    damage_calc,
    damage_engine,
    formatters,
    i18n,
    paths,
    portable_riven,
    riven_analysis,
    riven_rating,
    riven_sale,
)  # noqa: E402
from core.commands.riven import RivenCommands, _card_parameters  # noqa: E402
from core.commands.vision import VisionCommands  # noqa: E402
from core.parser import _STAT_LOOKUP, parse  # noqa: E402
from core.webpanel import coerce_setting, load_schema  # noqa: E402
from scripts import build_portable_precomputed as precomputed  # noqa: E402


class Plugin(RivenCommands, VisionCommands):
    def __init__(self, url=""):
        self.cfg = {"damage_engine_mode": "simulation", "riven_sale_sheet_url": url}
        self.riven_sale = riven_sale.RivenSaleSource()

    async def query(self, message):
        parsed = parse(message)
        assert parsed.command == "riventraits"
        return await self._h_riven_traits(parsed, None, "pc")


def _without_bundled():
    return patch.object(
        portable_riven,
        "bundled_weapons",
        new=lambda query, choices, target, options: (choices, None, {}),
    )


def test_weapon_index():
    assert "slash_damage" in riven_rating.physical_evidence("crphammershield", positive=True)
    assert {
        row["engine_id"]
        for row in riven_rating.related_weapons(riven_rating.match_weapon("Burston Prime"))
    } == {"burston", "burston_prime"}
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


def test_related_weapon_snapshot():
    from core import matching

    choices = riven_rating.match_weapon("Braton")

    def members():
        with patch.object(
            matching, "_riven_families_stamp", wraps=matching._riven_families_stamp
        ) as stamp:
            result = [row["engine_id"] for row in riven_rating.related_weapons(choices)]
            stamp.assert_called_once_with()
            return result

    assert members() == ["braton", "braton_prime", "braton_vandal", "mk1_braton"]
    with TemporaryDirectory() as tmp, patch.object(paths, "_RUN", Path(tmp)):
        table = paths.write_path("de/riven_families.json")
        table.write_text(json.dumps({"by_name": {"Braton": "one", "Soma": "one"}}))
        assert "soma" in members()
        # 替换时同一份快照仍可安全读；下一次查询重查文件版本。
        earlier = matching.family_index_snapshot()
        paths.atomic_write_text(
            table, json.dumps({"by_name": {"Braton": "one", "Soma": "two", "Hek": "one"}})
        )
        after = members()
        assert "hek" in after and "soma" not in after and after == sorted(after)
        assert matching.family_key("Soma", earlier) == "one"
    assert members() == ["braton", "braton_prime", "braton_vandal", "mk1_braton"]


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


def test_sale_trait_localization():
    for short, zh in (
        ("AEFF", "弹效"),
        ("ELEMENT", "元素"),
        ("SLASH", "切割"),
        ("AMMO", "弹药"),
        ("DTG", "G伤"),
        ("DTC", "C伤"),
        ("DTI", "I伤"),
        ("DTO", "O伤"),
        ("DTS", "S伤"),
        ("DTT", "T伤"),
        ("WUP", "重击速度"),
        ("SLAMDAM", "震地伤"),
        ("RECOIL", "后坐"),
        ("REC", "后坐"),
    ):
        sale = riven_sale.SaleResult("", "ok", riven_sale.SaleRow("Lex", short, short, ""))
        negative_zh = "加后坐力" if short in {"REC", "RECOIL"} else zh
        with i18n.use_lang("zh"):
            assert riven_sale.lines(sale) == [
                "◆ 出售参考",
                f"· 正词条 {zh}",
                f"· 负词条 {negative_zh}",
            ], short
        with i18n.use_lang("en"):
            assert riven_sale.lines(sale) == [
                "◆ Selling reference",
                f"· Positive traits {short}",
                f"· Negative traits {short}",
            ], short


@_without_bundled()
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
        assert any("启用独立引擎后可查看模拟词条推荐" in line for line in first.lines)
        for name, text in (("second.json", "代号二"), ("unavailable.json", "暂时无法读取")):
            host.cfg["riven_sale_sheet_url"] = "https://example.org/" + name
            reply = await host.query("词条推荐 安培克斯")
            a, b = first.lines.index("◆ 出售参考"), reply.lines.index("◆ 出售参考")
            assert first.lines[:a] == reply.lines[:b]
            assert text in "\n".join(reply.lines[b:])
    engine.assert_not_awaited()


def _live_catalog_weapon(name, *fire_modes):
    return {
        "path": damage_calc.find_weapon_exact(name)["uniqueName"],
        "nameEn": name,
        "weaponType": "rifle",
        "available": True,
        "buildAvailable": True,
        "analysisAvailable": True,
        "fireModes": list(fire_modes),
        "rivenType": "rifle",
    }


def _live_catalog(*weapons):
    return {
        "schemaVersion": 3,
        "buildLabel": damage_engine.PORTABLE_DATA,
        "weapons": weapons,
        "enemies": [],
        "statsByType": {
            "rifle": [
                {"code": str(index), "baseValue": 100, "canPositive": True} for index in range(3)
            ]
        },
    }


def _live_reply(request, *, limited=0):
    target = request.get("benchmarkTarget") or damage_engine.portable_target_fields(
        damage_calc.DEFAULT_ENEMY_PATH, damage_calc.default_target()
    )
    mode = (request["weapon"], request["fireMode"], request["headshotChance"])
    row = _portable_score(mode, 100)
    row["evaluationTarget"].update(
        enemyPath=target["enemy"],
        **{
            key: target[key] for key in ("level", "steelPath", "windowSeconds", "enemyEnhancements")
        },
    )
    row["searchWork"]["limited"] = limited
    mod = {"slot": 0, "path": "/Mod", "nameEn": "Serration", "rank": 1}
    return {
        **row,
        "disposition": request["disposition"],
        "evaluationScenario": request.get("scenario", {}),
        **({"exilus": None} if request.get("exilus") == {"mode": "auto"} else {}),
        "arcaneStates": [{**arc, "applied": True} for arc in request.get("arcanes", ())],
        "actual": {
            "riven": {**request["rivenCard"], "drain": 18, "stats": request["stats"], "slot": 1},
            "mods": [mod],
            "forma": {
                "drain": 13,
                "limit": 60,
                "fits": True,
                "placement": [{"origin": 0, "drain": 4}, {"origin": 1, "drain": 9}] + [None] * 6,
            },
        },
        "baseline": {"includesRiven": False, "mods": [mod], "forma": {"fits": True}},
        "recommendedPositiveTiers": [],
        "recommendedNegative": ["Zoom"],
        "fatalNegative": [],
        "fatalNegativeAbove": {},
    }


def _matrix_reply(request):
    target = request["benchmarkTarget"]
    scenario = request.get("scenario", {})
    cycle = scenario.get("incarnonCycle", False)
    incarnon = request["fireMode"] != 0 and not cycle

    def trait(code, stat, pct, **flags):
        return {
            "code": code,
            "stat_id": stat,
            "unit": "%",
            "modeled": pct is not None,
            "shown_31": -30 if stat == "zoom" else 90,
            "pct_31": pct,
            "shown_21": -35 if stat == "zoom" else 105,
            "pct_21": pct,
            **flags,
        }

    return {
        "engineFingerprint": portable_riven.FINGERPRINT,
        "buildLabel": damage_engine.PORTABLE_DATA,
        "weapon": {"path": request["weapon"]},
        "fireMode": request["fireMode"],
        "evaluationScenario": scenario,
        "evaluationTarget": {
            **target,
            "enemyPath": target["enemy"],
            "nameEn": "Thrax Centurion",
            "armourStrip": request.get("armourStrip", 0.0),
            "elementalArmourStrip": request.get("elementalArmourStrip", 0.0),
        },
        "build": {"full_dps": 100, "kpm": 10, "capacity": {"valid": True}},
        "matrix": {
            "baseline_dps": 100,
            "baseline_kpm": 10,
            "low_ammo": False,
            "positive": [
                trait("F", "reload_speed", 0 if incarnon else 6.5),
                trait("Y", "weakpoint_crit_chance", 11.0, spliced=True, weakpoint=True),
                trait("HR", "reload_holstered", None, spliced=True),
            ],
            "negative": [trait("H", "zoom", 0, neutral=True)],
        },
    }


def _build_reply(body):
    return {
        "engineFingerprint": portable_riven.FINGERPRINT,
        "buildLabel": damage_engine.PORTABLE_DATA,
        "weapon": {"path": body["weapon"]},
        "fireMode": body["fireMode"],
        "build": {
            "includesRiven": False,
            "mods": [{"path": "/Mod", "nameEn": "Serration", "rank": 1, "slot": 0}],
            "forma": {"fits": True, "slots": {}},
        },
        "scoreMetric": "killsPerMinute",
        "evaluationScenario": body.get("scenario", {}),
        "evaluationTarget": {"path": body["enemy"], "nameEn": "Thrax Centurion"},
        "evaluationContext": {
            "level": body["level"],
            "steel_path": body["steelPath"],
            "enemy_enhancements": body["enemyEnhancements"],
            "headshotChance": body["headshotChance"],
            "windowSeconds": body["windowSeconds"],
            "armour_strip": 0,
            "elemental_armour_strip": 0,
            "hit_location": "body",
        },
    }


async def test_reference_build_replays_auto_equipment_and_locked_options():
    host = Plugin()
    weapon = _live_catalog_weapon("Burston Prime", {"index": 0})
    scope = weapon["path"], 0, 0.0
    route = {"tier2": "2-1", "tier3": "3-2", "tier4": "4-2"}
    arcane = {
        "path": "/Lotus/Upgrades/CosmeticEnhancers/Offensive/PrimaryDamageOnKill",
        "rank": 5,
        "stacks": 4,
        "uptime": 1.0,
    }
    arcane_label = {**arcane, "nameZh": "主要·无情", "nameEn": "Primary Merciless"}
    exilus = {
        "path": "/Exilus",
        "rank": 0,
        "nameZh": "私法 补给",
        "nameEn": "Vigilante Supplies",
        "slotPolarity": "AP_TACTIC",
        "drain": 2,
        "modSet": "VigilanteSetMod",
    }
    builds, simulations = [], []
    benchmark = damage_engine.portable_target_fields(
        damage_calc.DEFAULT_ENEMY_PATH, damage_calc.default_target()
    )

    async def engine(_, endpoint, body=None, **_kwargs):
        if endpoint == "/api/catalog":
            return _live_catalog(weapon)
        if endpoint == "/api/build":
            builds.append(body)
            return {
                **_build_reply(body),
                "incarnonChoices": body.get("incarnonChoices", route),
                "exilus": exilus,
                "autoLoadout": {"arcane": {"chosen": [{**arcane, "nameZh": "主要·无情"}]}}
                if body.get("autoArcane")
                else None,
            }
        assert endpoint == "/api/simulate"
        simulations.append(body)
        assert body["arcanes"] in ([arcane], [{**arcane, "stacks": 0}])
        assert body["incarnonChoices"] == route and body["windowSeconds"] == 30
        assert body["exilus"] == {"path": "/Exilus", "rank": 0}
        return {
            "engineFingerprint": portable_riven.FINGERPRINT,
            "buildLabel": damage_engine.PORTABLE_DATA,
            "damageModel": body["damageModel"],
            "selection": {"weapon": weapon, "fireMode": 0},
            "incarnonChoices": route,
            "enemyEnhancements": body["enemyEnhancements"],
            "arcanes": [{**arcane_label, **body["arcanes"][0], "applied": True}],
            "exilus": exilus,
            "loadoutValidation": {
                "valid": True,
                "capacity": 4 * len(body["mods"]) + 2,
                "capacityLimit": 60,
                "assignments": [{**mod, "drain": 4} for mod in body["mods"]],
            },
            "benchmarkTimeline": {
                "windowDps": 120.0 if body["mods"] else 60.0,
                "windowSeconds": body["windowSeconds"],
                "context": {
                    "enemyPath": body["enemy"],
                    "level": body["level"],
                    "steelPath": body["steelPath"],
                },
            },
        }

    with TemporaryDirectory() as root, patch.object(damage_engine, "portable_request", engine):
        host.damage_engine = damage_engine.EngineService(Path(root))
        host.damage_engine.ensure_portable = AsyncMock()
        options = {"benchmarkTarget": benchmark}
        result = await portable_riven._reference_build(host, None, 8765, scope, weapon, options)
        assert builds[0]["autoArcane"] is True and builds[0]["autoIncarnon"] is True
        assert result["arcanes"] == [arcane_label]
        lines = await portable_riven.build_view(host, 8765, result)
        assert len(simulations) == 3 and len(simulations[0]["mods"]) == 1
        assert simulations[0]["arcanes"] == simulations[2]["arcanes"] == [arcane]
        assert simulations[1]["arcanes"] == [{**arcane, "stacks": 0}]
        assert simulations[2]["mods"] == []
        assert "· 赋能　主要·无情（5级）" in lines
        assert any("30 秒平均 DPS 120" in line for line in lines)
        assert any(line.startswith("· 01　") and "　60　" in line for line in lines)
        failed = await engine(8765, "/api/simulate", simulations[0])
        failed["arcanes"] = []
        try:
            damage_engine.validate_fixed_replay(failed, simulations[0])
        except damage_engine.EngineError as exc:
            assert "赋能回放与配装不符" in str(exc)
        else:
            raise AssertionError("回放漏掉自动赋能时应拒绝结果")
        active = {**arcane, "applied": True, "stacks": 12}
        for requested, receipt in (
            ([], active),
            ([{**arcane, "stacks": 0}], active),
            ([{**arcane, "stacks": 0}], {k: v for k, v in active.items() if k != "stacks"}),
            ([arcane], {**arcane, "applied": "false"}),
            ([{**arcane, "rank": 1}], {**arcane, "rank": True, "applied": True}),
            ([arcane], {**arcane, "applied": True, "uptime": 0.5}),
            ([arcane], {k: v for k, v in {**arcane, "applied": True}.items() if k != "uptime"}),
            ([arcane], {**arcane, "applied": True, "uptime": True}),
            ([{**arcane, "uptime": True}], {**arcane, "applied": True, "uptime": 1}),
        ):
            try:
                damage_engine.validate_fixed_replay(
                    {**failed, "arcanes": [receipt]}, {**simulations[0], "arcanes": requested}
                )
            except damage_engine.EngineError as exc:
                assert "赋能回放与配装不符" in str(exc)
            else:
                raise AssertionError(f"赋能回放应拒绝不符的记录：{receipt}")
        damage_engine.validate_fixed_replay(
            {**failed, "arcanes": [active]},
            {**simulations[0], "arcanes": [{"path": arcane["path"], "rank": arcane["rank"]}]},
        )
        for field in ("mods", "exilus"):
            body = {**simulations[0], "arcanes": []}
            if field == "exilus":
                body[field] = {**body[field], "rank": 1}
                bad_rank = {**failed, field: {**exilus, "rank": True}}
            else:
                validation = failed["loadoutValidation"]
                (assignment,) = validation["assignments"]
                bad_rank = {
                    **failed,
                    "loadoutValidation": {
                        **validation,
                        "assignments": [{**assignment, "rank": True}],
                    },
                }
            try:
                damage_engine.validate_fixed_replay(bad_rank, body)
            except damage_engine.EngineError:
                pass
            else:
                raise AssertionError(f"{field} 的布尔值不能冒充等级 1")
        locked = {**options, "arcanes": [arcane], "incarnonChoices": route}
        await portable_riven._reference_build(host, None, 8765, scope, weapon, locked)
        assert builds[-1]["arcanes"] == [arcane] and "autoArcane" not in builds[-1]
        assert builds[-1]["incarnonChoices"] == route and "autoIncarnon" not in builds[-1]
        await host.damage_engine.close()


def test_rank_zero_set_exilus_description():
    exilus = {
        "nameEn": "Vigilante Supplies",
        "nameZh": "私法 补给",
        "rank": 0,
        "drain": 2,
        "slotPolarity": "AP_TACTIC",
    }
    for language, expected in (
        ("zh", "私法 补给（0级，容量 2；套装特效 0 级满收益、省极化）"),
        ("en", "Vigilante Supplies (rank 0, drain 2; full set bonus at rank 0, saves Forma)"),
    ):
        with i18n.use_lang(language):

            def line(**fields):
                return portable_riven._equipment_lines(
                    {"exilus": {**exilus, **fields}}, verified=True
                )[1]

            assert expected + " · ⟦pol:naramon⟧" in line(modSet="VigilanteSetMod")
            assert expected in line(setNameZh="私法 Mod 组合加成")
            for ordinary in (line(rank=5, modSet="VigilanteSetMod"), line(rank=5), line()):
                assert "满收益" not in ordinary and "full set bonus" not in ordinary


def test_unpolarized_special_slots():
    exilus = {"nameZh": "旋风", "nameEn": "Whirlwind", "rank": 5, "drain": 9}
    stance = {"nameZh": "星界微光", "nameEn": "Astral Twilight", "capacityGain": 10}
    for lang, labels, universal in (
        ("zh", ("· 扩展槽　旋风（5级，容量 9）", "· 架势　星界微光（容量 +10）"), "通用"),
        (
            "en",
            ("· Exilus　Whirlwind (rank 5, drain 9)", "· Stance　Astral Twilight (capacity +10)"),
            "Universal",
        ),
    ):
        with i18n.use_lang(lang):
            for polarity in (
                None,
                "",
                "AP_UNIVERSAL",
                "AP_NONE",
                "NONE",
                "NULL",
                "AP_UNKNOWN",
                "AP_ANY",
            ):
                equipment = {
                    "exilus": {**exilus, "slotPolarity": polarity},
                    "stance": {**stance, "slotPolarity": polarity},
                }
                lines = portable_riven._equipment_lines(equipment, verified=True)
                suffix = f" · ⟦pol:universal⟧{universal}" if polarity == "AP_ANY" else ""
                assert (lines[1], lines[3]) == tuple(label + suffix for label in labels)
                assert not any("AP_" in line for line in lines)


async def test_bundled_metadata_failure_falls_back_to_engine():
    host = Plugin()
    host.cfg["damage_engine_portable_port"] = 8765
    host.riven_sale.lookup = AsyncMock(return_value=riven_sale.SaleResult("", "unavailable"))
    weapon = _live_catalog_weapon(
        "Burston Prime", {"index": 0}, {"index": 1, "isIncarnonForm": True}
    )
    calls = []

    async def engine(_, endpoint, body=None, **_kwargs):
        calls.append(endpoint)
        if endpoint == "/api/catalog":
            return _live_catalog(weapon)
        if endpoint == "/api/analyze":
            assert "_searchBudgetSeconds" not in body
            assert [row["negative"] for row in body["stats"]] == [False, False, True]
            assert [round(row["value"], 1) for row in body["stats"]] == [148.5, 148.5, 59.4]
        return _build_reply(body) if endpoint == "/api/build" else _live_reply(body)

    with TemporaryDirectory() as root:
        root = Path(root)
        seed = root / "seed"
        seed.mkdir()
        host.damage_engine = damage_engine.EngineService(root)
        host.damage_engine.ensure_portable = AsyncMock()
        with (
            patch.object(portable_riven, "paths", SimpleNamespace(PKG_DATA=seed)),
            patch.object(damage_engine, "portable_request", engine),
            patch.object(portable_riven, "_trait_matrix_request", AsyncMock(return_value=None)),
            patch.object(portable_riven, "build_view", return_value=["实时配装"]),
        ):
            build = await host._h_loadout_recommend(parse("配卡推荐 伯斯顿p"), None, "pc")
            assert build.lines == ["实时配装", "※ 默认展示灵化循环，可加「主射」「灵化形态」切换。"]
            assert calls == ["/api/catalog", "/api/build"]
            document = {
                "schema": 1,
                "engineFingerprint": portable_riven.FINGERPRINT,
                "buildLabel": damage_engine.PORTABLE_DATA,
                "target": damage_engine.portable_target_fields(
                    damage_calc.DEFAULT_ENEMY_PATH, damage_calc.default_target()
                ),
                "weapons": {},
            }
            for field, bad in (
                ("schema", 2),
                ("engineFingerprint", "old"),
                ("buildLabel", "old"),
                ("target", {}),
            ):
                (seed / portable_riven.BUNDLED_BUILDS).write_text(
                    json.dumps({**document, field: bad})
                )
                assert portable_riven._load_bundled(portable_riven.BUNDLED_BUILDS) is None
            (seed / portable_riven.BUNDLED_BUILDS).write_text("{bad JSON")
            assert portable_riven._load_bundled(portable_riven.BUNDLED_BUILDS) is None
            traits = await host.query("词条推荐 伯斯顿p 2+1-")
            assert any("低影响负面" in line for line in traits.lines)
            assert any("凶魂百夫长" in line and "2+1-" in line for line in traits.lines)
            assert calls[-1] == "/api/analyze"
            assert len(list((host.damage_engine.root / "portable-live-cache").glob("*.json"))) == 1
        await host.damage_engine.close()


async def test_trait_matrix_cache_identity():
    weapon = _live_catalog_weapon("Burston Prime", {"index": 0})
    options = {
        "benchmarkTarget": damage_engine.portable_target_fields(
            damage_calc.DEFAULT_ENEMY_PATH, damage_calc.default_target()
        )
    }
    identity = {
        "engineFingerprint": portable_riven.FINGERPRINT,
        "buildLabel": damage_engine.PORTABLE_DATA,
    }
    fields = dict(identity)
    calls = []

    async def matrix(port, request):
        calls.append(port)
        result = {k: v for k, v in _matrix_reply(request).items() if k not in identity}
        return {**result, **fields}

    with (
        TemporaryDirectory() as root,
        patch.object(portable_riven, "_trait_matrix_request", matrix),
    ):

        async def query(health):
            service = damage_engine.EngineService(Path(root))
            service.ensure_portable = AsyncMock()
            service._portable_health = AsyncMock(return_value=health)
            try:
                row = await portable_riven._reference_traits(
                    SimpleNamespace(damage_engine=service),
                    None,
                    8843,
                    (weapon["path"], 0, 0),
                    weapon,
                    options,
                    "3+1-",
                    None,
                )
                return row, service._portable_health
            finally:
                await service.close()

        cache = Path(root) / "damage-engine" / "portable-live-cache"
        for key in identity:
            fields.update({**identity, key: "wrong"})
            try:
                await query(identity)
            except ValueError as exc:
                assert "版本" in str(exc)
            else:
                raise AssertionError("错误版本不能采信")
            assert not list(cache.glob("*.json"))
        fields.clear()
        result, probe = await query({"buildLabel": damage_engine.PORTABLE_DATA})
        probe.assert_awaited_once_with(8843, require_fingerprint=False)
        assert "engineFingerprint" not in result and not list(cache.glob("*.json"))
        result, probe = await query(identity)
        probe.assert_awaited_once_with(8843, require_fingerprint=False)
        assert all(result[k] == v for k, v in identity.items())
        (entry,) = cache.glob("*.json")
        legacy = json.loads(entry.read_bytes())
        legacy.pop("cacheFormat")
        entry.write_text(json.dumps(legacy))
        fields.update(identity)
        count = len(calls)
        await query({})
        assert len(calls) == count + 1
        await query({})
        assert len(calls) == count + 1


async def test_build_and_simulate_cache_identity():
    from unittest import TestCase

    port = 8766
    identity = {
        "engineFingerprint": portable_riven.FINGERPRINT,
        "buildLabel": damage_engine.PORTABLE_DATA,
    }
    weapon = _live_catalog_weapon("Burston Prime", {"index": 0})
    target = damage_engine.portable_target_fields(
        damage_calc.DEFAULT_ENEMY_PATH, damage_calc.default_target()
    )
    request = {
        **target,
        "weapon": weapon["path"],
        "fireMode": 0,
        "slotPolarities": [None] * 8,
        "incarnonChoices": {},
        "mods": [{"slot": 0, "path": "/Mod", "rank": 1}],
    }

    for endpoint in ("/api/build", "/api/simulate"):

        async def query(fields, health, *, variant_fields=None):
            # 每次使用独立实例，跨次命中必须来自磁盘。
            service = damage_engine.EngineService(Path(root))
            service.ensure_portable = AsyncMock()
            service._portable_health = AsyncMock(return_value=health)

            async def compute(actual_port, route, body, **kw):
                assert (actual_port, route) == (port, endpoint)
                if endpoint == "/api/build":
                    reply = {k: v for k, v in _build_reply(body).items() if k not in identity}
                else:
                    reply = {
                        "damageModel": "target",
                        "selection": {"weapon": weapon, "fireMode": 0},
                        "enemyEnhancements": body["enemyEnhancements"],
                        "loadoutValidation": {"valid": True, "assignments": body["mods"]},
                        "benchmarkTimeline": {
                            "windowDps": 100 if body["mods"] else 50,
                            "windowSeconds": body["windowSeconds"],
                            "context": {
                                "enemyPath": body["enemy"],
                                "level": body["level"],
                                "steelPath": body["steelPath"],
                            },
                        },
                    }
                    if not body["mods"] and variant_fields is not None:
                        return {**reply, **variant_fields}
                return {**reply, **fields}

            try:
                with patch.object(damage_engine, "portable_request", side_effect=compute) as calls:
                    if endpoint == "/api/build":
                        result = await portable_riven._reference_build(
                            SimpleNamespace(damage_engine=service),
                            None,
                            port,
                            (weapon["path"], 0, 0),
                            weapon,
                            {"benchmarkTarget": target},
                        )
                    else:
                        result = await portable_riven._mod_gains(
                            service, port, request, persistent=True
                        )
                    return result, service._portable_health, calls.await_count
            finally:
                await service.close()

        for key in identity:
            with TemporaryDirectory() as root:
                cache = Path(root) / "damage-engine" / "portable-live-cache"
                missing = {k: v for k, v in identity.items() if k != key}
                wrong = {**identity, key: "wrong"}
                for invalid in ("wrong", "", None):
                    with TestCase().assertRaises(damage_engine.EngineVersionMismatch):
                        await query({**identity, key: invalid}, identity)
                with TestCase().assertRaises(damage_engine.EngineVersionMismatch):
                    await query(missing, wrong)
                assert not list(cache.glob("*.json"))
                result, probe, calls = await query(missing, missing)
                probe.assert_awaited_once_with(port, require_fingerprint=False)
                assert calls > 0 and not list(cache.glob("*.json"))
                if endpoint == "/api/build":
                    assert key not in result
                else:
                    assert result["complete"] and result["without"] == {0: 50}
                    with TestCase().assertRaises(damage_engine.EngineVersionMismatch):
                        await query(identity, identity, variant_fields=wrong)
                    _, probe, _ = await query(identity, missing, variant_fields=missing)
                    probe.assert_awaited_once_with(port, require_fingerprint=False)
                    assert not list(cache.glob("*.json"))
                verified, probe, _ = await query(missing, identity)
                probe.assert_awaited_once_with(port, require_fingerprint=False)
                (path,) = cache.glob("*.json")
                entry = json.loads(path.read_bytes())
                assert entry["cacheFormat"] == 3
                assert all(entry[k] == verified[k] == v for k, v in identity.items())
                path.write_text(json.dumps({**entry, "cacheFormat": 2}))
                _, probe, calls = await query(identity, {})
                assert (
                    calls > 0
                    and not probe.called
                    and json.loads(path.read_bytes())["cacheFormat"] == 3
                )
                _, probe, calls = await query(identity, {})
                assert calls == 0 and not probe.called


def test_slot_plan_matches_exhaustive_order():
    from unittest import TestCase
    from itertools import permutations
    from time import perf_counter

    def exhaustive(original, polarities, costs, riven, elements, extra, limit, unspecified):
        # 独立参考：按原排列逐项评估 256 个掩码，同分保留首次排列。
        best = None
        for order in dict.fromkeys(permutations(original)):
            if [i for i in order if i in elements] != elements:
                continue
            for mask in range(256):
                drain, changes, placement = extra, {}, []
                for slot, origin in enumerate(order):
                    changed = mask & (1 << (7 - slot))
                    native = polarities[slot]
                    if origin is None:
                        if changed:
                            break
                        placement.append(None)
                        continue
                    raw, mod = costs[origin]
                    half = (raw + 1) // 2
                    if changed:
                        if mod in portable_riven._NO_POLARITY:
                            break
                        native = changes[str(slot)] = mod
                        used = half
                    elif unspecified and origin == riven and native != "AP_ANY":
                        break
                    elif (
                        native in portable_riven._NO_POLARITY or mod in portable_riven._NO_POLARITY
                    ):
                        used = raw
                    else:
                        used = (
                            half
                            if native == mod or "AP_ANY" in (native, mod)
                            else (raw * 5 + 3) // 4
                        )
                    drain += used
                    placement.append({"origin": origin, "drain": used, "polarity": native})
                else:
                    score = (len(changes), mask, sum(a != b for a, b in zip(original, order)))
                    if drain <= limit and (best is None or score < best[0]):
                        best = score, (order, changes, placement, drain)
        return best[1] if best else None

    for order, polarities, costs, elements, limit, unspecified in (
        (
            [0, None, 2, None, 4, None, None, None],
            ["B", "A", None, "A", "B", None, None, None],
            {0: (10, "A"), 2: (11, "B"), 4: (8, "A")},
            [0, 4],
            16,
            False,
        ),
        (
            list(range(8)),
            ["B", "A"] * 4,
            {i: (6 + i, "A" if i % 2 == 0 else "B") for i in range(8)},
            [0, 1, 2, 3, 4, 5],
            45,
            True,
        ),
        (
            [None, 1, 2, None, None, None, None, None],
            ["AP_ANY"] + [None] * 7,
            {1: (11, "A"), 2: (8, None)},
            [],
            14,
            True,
        ),
        (list(range(8)), [None] * 8, {i: (12, "A") for i in range(8)}, list(range(8)), 47, False),
    ):
        args = (order, polarities, costs, next(iter(costs)), elements, 0, limit, unspecified)
        expected = exhaustive(*args)
        if expected is None:
            with TestCase().assertRaises(ValueError):
                portable_riven._fit_riven_slots(*args)
        else:
            assert portable_riven._fit_riven_slots(*args) == expected

    eight = list(range(8))
    start = perf_counter()
    fitted = portable_riven._fit_riven_slots(
        eight, [None] * 8, {i: (12, "A") for i in eight}, 7, [], 0, 60, False
    )
    elapsed = perf_counter() - start
    assert elapsed < 0.1, f"八槽耗时 {elapsed:.4f}s"
    assert fitted[0] == tuple(eight) and set(fitted[1]) == set("234567") and fitted[3] == 60
    original = list(range(8))
    costs = {slot: (raw, "A") for slot, raw in enumerate([12, 6, 6, 7, 7, 8, 9, 10])}
    order, changes, placement, drain = portable_riven._fit_riven_slots(
        original, [None] * 8, costs, 7, [0, 7], 0, 60, False
    )
    score = (
        len(changes),
        sum(1 << (7 - int(s)) for s in changes),
        sum(a != b for a, b in zip(original, order)),
    )
    assert score == (1, 1, 0) and changes == {"7": "A"}
    assert drain == sum(row["drain"] for row in placement) == 60


@_without_bundled()
async def test_live_recommendations_use_separate_routes_and_persistent_cache():
    burston = _live_catalog_weapon(
        "Burston Prime", {"index": 0}, {"index": 1, "isIncarnonForm": True}
    )
    toxocyst = _live_catalog_weapon(
        "Dual Toxocyst", {"index": 0}, {"index": 1, "isIncarnonForm": True}
    )
    toxocyst.update(weaponType="pistol", rivenType="pistol")
    nikana = _live_catalog_weapon("Nikana Prime", {"index": 0})
    nikana.update(weaponType="melee", analysisAvailable=False, rivenType=None)
    magistar = _live_catalog_weapon("Magistar", {"index": 0})
    magistar.update(weaponType="melee", rivenType="melee", compatTags=["HAMMERS_STANCE"])
    glaive = _live_catalog_weapon("Glaive Prime", {"index": 0})
    glaive.update(weaponType="melee", rivenType="melee", compatTags=["GLAIVES_STANCE"])
    host = Plugin()
    host.cfg["damage_engine_portable_port"] = 8765
    host.riven_sale.lookup = AsyncMock(return_value=riven_sale.SaleResult("", "unavailable"))
    calls = []
    notices = []
    event = SimpleNamespace(unified_msg_origin="test")

    async def notice(origin, text):
        assert origin is event
        notices.append((len(calls), text))

    host._send_progress_notice = notice

    async def engine(_, endpoint, body=None, *, timeout):
        if endpoint == "/api/catalog":
            return _live_catalog(burston, nikana, magistar, glaive, toxocyst)
        assert timeout == 600 and body["profile"] == "standard"
        assert host.damage_engine.simulations.active == 1
        calls.append((endpoint, body))
        if endpoint == "/api/build":
            assert "benchmarkTarget" not in body and "stats" not in body
            assert body["autoArcane"] is True and body["autoIncarnon"] is True
            assert body["metric"] == (
                "attackDps"
                if body["weapon"] in {nikana["path"], magistar["path"], glaive["path"]}
                else "sustainedDps"
            )
            return _build_reply(body)
        assert endpoint == "/api/analyze" and "_searchBudgetSeconds" not in body
        return _live_reply(body)

    async def matrix_http(request):
        assert request.method == "POST" and request.url.path == "/api/trait-matrix"
        assert request.extensions["timeout"]["read"] == 600
        assert host.damage_engine.simulations.active == 1
        body = json.loads(request.content)
        assert body["profile"] == "standard" and "_searchBudgetSeconds" not in body
        calls.append((request.url.path, body))
        return httpx.Response(200, json=_matrix_reply(body))

    client = httpx.AsyncClient

    def transport(**kwargs):
        return client(transport=httpx.MockTransport(matrix_http), **kwargs)

    async def build_view(_, __, row, **_options):
        return [portable_riven.scenario_line(row), "◆ 无紫卡参考配装"]

    async def build(text):
        return await host._h_loadout_recommend(parse(text), event, "pc")

    async def traits_query(text):
        return await host._h_riven_traits(parse(text), event, "pc")

    with (
        TemporaryDirectory() as root,
        patch.object(damage_engine, "portable_request", engine),
        patch.object(portable_riven, "build_view", side_effect=build_view),
        patch.object(httpx, "AsyncClient", side_effect=transport),
    ):

        async def new_service():
            if hasattr(host, "damage_engine"):
                await host.damage_engine.close()
            host.damage_engine = damage_engine.EngineService(Path(root))
            host.damage_engine.ensure_portable = AsyncMock()

        await new_service()
        incarnon = await build("配卡推荐 伯斯顿p 灵化形态")
        base = await build("配卡推荐 伯斯顿p 主要")
        cycle = await build("配卡推荐 伯斯顿p")
        assert (await build("配卡推荐 伯斯顿p 灵化")).lines == cycle.lines[:-1]
        assert cycle.lines[-1] == "※ 默认展示灵化循环，可加「主射」「灵化形态」切换。"
        traits = await traits_query("词条推荐 伯斯顿p 灵化形态")
        assert [body["fireMode"] for _, body in calls] == [1, 0, 1, 1]
        assert [route for route, _ in calls] == ["/api/build"] * 3 + ["/api/trait-matrix"]
        assert [index for index, _ in notices] == [0, 1, 2, 3]
        assert all("后台计算" in message for _, message in notices)
        charged_cycle = {"incarnonCycle": True, "combatState": {"gaugeChargingHeadshots": True}}
        assert calls[2][1]["scenario"] == charged_cycle
        assert "灵化形态" in incarnon.lines[0] and "主射" in base.lines[0]
        assert "灵化循环（攒条全弱点）" in cycle.lines[0]
        assert traits.lines[0] == "◆ 单词条模拟推荐 · 3+1-"
        card = "\n".join(traits.lines)
        for label in (
            "◆ 实时参考配装 · 全词条",
            "弱点边界",
            "（100%弱点）",
            "灵化无效",
            "未模拟",
            "正词条",
            "负词条",
            "融合词条",
            "◆ 出售参考",
        ):
            assert label in card
        assert "洗卡优先看" not in card and "搜索达到时限" not in card

        cache = host.damage_engine.root / "portable-live-cache"
        assert len(list(cache.glob("*.json"))) == 4
        for entry in cache.glob("*.json"):
            os.utime(entry, (1, 1))
        await new_service()
        assert (await build("配卡推荐 Burston Prime inc")).lines == incarnon.lines
        assert (await build("配卡推荐 Burston Prime pri")).lines == base.lines
        assert (await traits_query("词条推荐 伯斯顿p 灵化形态")).lines == traits.lines
        assert len(calls) == len(notices) == 4

        for entry in cache.glob("*.json"):
            payload = json.loads(entry.read_bytes())
            if payload.get("matrix"):
                entry.write_text(json.dumps({**payload, "engineFingerprint": "old"}))
        await new_service()
        await traits_query("词条推荐 伯斯顿p 灵化形态")
        assert len(calls) == len(notices) == 5 and calls[-1][0] == "/api/trait-matrix"

        custom = await build("配卡推荐 伯斯顿p 主射 百夫长9999 有盾 去超宏 普通")
        request = calls[-1][1]
        assert request["scenario"] == {"aiming": True}
        assert (
            request["level"],
            request["steelPath"],
            request["headshotChance"],
            request["windowSeconds"],
        ) == (9999, False, 1.0, 180.0)
        assert request["enemyEnhancements"]["shieldOverride"] == 3600
        assert "普通 凶魂百夫长 Lv9999" in custom.lines[0]
        with i18n.use_lang("en"):
            prior = len(calls)
            command = "loadoutrecommend Burston Prime primary lv812"
            custom_en = await build(command)
            assert (await build(command)).lines == custom_en.lines
            assert len(calls) == prior + 1 and calls[-1][0] == "/api/build"
            assert notices[-1][0] == prior and "Calculating in the background" in notices[-1][1]

        melee = await build("配卡推荐 侍刃 Prime")
        assert "平砍" in melee.lines[0] and calls[-1][0] == "/api/build"
        before = len(calls)
        unsupported = await build("配卡推荐 侍刃 Prime 灵化")
        assert any("可用模式" in line and "重击" in line for line in unsupported.lines)
        assert len(calls) == before
        for lang, command, note, progress in (
            ("zh", "词条推荐 伯斯顿p 灵化循环", "仅充能期", "后台计算"),
            (
                "en",
                "riventraits Burston Prime cycle 2+1-",
                "Charge only",
                "Calculating in the background",
            ),
        ):
            with i18n.use_lang(lang):
                prior = len(notices)
                reply = await traits_query(command)
                assert (await traits_query(command)).lines == reply.lines
                assert len(notices) == prior + 1 and progress in notices[-1][1]
            assert f"⟦v⟧{note}⟦/v⟧" in "\n".join(reply.lines), reply.lines
            assert any("⟦y⟧+6.5%⟦/y⟧" in line for line in reply.lines), reply.lines
        assert len(calls) == before + 2 and calls[-1][0] == "/api/trait-matrix"
        assert calls[-1][1]["scenario"] == charged_cycle
        for lang, command, progress in (
            ("zh", "配卡推荐 伯斯顿p 主射 暴率178.2 多重142.2 等级8 极性=naramon", "后台计算"),
            (
                "en",
                "loadoutrecommend Burston Prime base critchance178.2 multishot142.2 rank=8 polarity=naramon lv171",
                "Calculating in the background",
            ),
        ):
            with i18n.use_lang(lang):
                prior = len(calls)
                locked = await build(command)
                assert (await build(command)).lines == locked.lines
                assert len(calls) == prior + 1 and calls[-1][0] == "/api/analyze", locked.lines
                assert notices[-1][0] == prior and progress in notices[-1][1]

        with _without_bundled():
            melee_build = await build("配卡推荐 执法者 平砍")
            melee_traits = await traits_query("词条推荐 执法者 平砍")
            slam_build = await build("配卡推荐 执法者")
            slam_traits = await traits_query("词条推荐 执法者")
            assert (await traits_query("词条推荐 执法者 重击震地")).lines == slam_traits.lines
            distant_slam_traits = await traits_query("词条推荐 执法者 重击震地 距离12米")
            glaive_build = await build("配卡推荐 Glaive Prime")
            glaive_traits = await traits_query("词条推荐 Glaive Prime")
        combo = {"combatState": {"meleeRoute": "combo"}}
        slam = {"heavyAttack": True, "combatState": {"meleeRoute": "slamBullet"}}
        throw = {"heavyAttack": True, "combatState": {"meleeRoute": "glaive"}}
        assert [(route, body["scenario"]) for route, body in calls[-7:]] == [
            ("/api/build", combo),
            ("/api/trait-matrix", combo),
            ("/api/build", slam),
            ("/api/trait-matrix", slam),
            ("/api/trait-matrix", {**slam, "distance": 12.0}),
            ("/api/build", throw),
            ("/api/trait-matrix", throw),
        ]
        assert melee_build.lines[0].startswith("◆ 平砍 · ")
        assert any(line.startswith("· 平砍 · ") for line in melee_traits.lines)
        assert slam_build.lines[0].startswith("◆ 重击震地 · ")
        assert any(line.startswith("· 重击震地 · ") for line in slam_traits.lines)
        assert glaive_build.lines[0].startswith("◆ 重击投掷 · ")
        assert any(line.startswith("· 重击投掷 · ") for line in glaive_traits.lines)
        assert "· 重击" not in slam_traits.lines
        assert "· 距离 12 米" in distant_slam_traits.lines

        for suffix, chance, hit, scenario in (
            ("", 1.0, "弱点 100%（机制需爆头）", {"incarnonCycle": True}),
            ("标准靶", 1.0, "弱点 100%（机制需爆头）", {"incarnonCycle": True, "aiming": False}),
            ("躯干", 0.0, "躯干", charged_cycle),
            ("弱点 50%", 0.5, "弱点 50%", charged_cycle),
        ):
            live_build = await build(f"配卡推荐 毒囊双枪 灵化 {suffix}")
            live_traits = await traits_query(f"词条推荐 毒囊双枪 灵化 {suffix}")
            live_riven = await build(
                f"配卡推荐 毒囊双枪 灵化 暴率178.2 多重142.2 等级8 极性=naramon {suffix}"
            )
            for (endpoint, body), reply in zip(
                calls[-3:], (live_build, live_traits, live_riven), strict=True
            ):
                assert body["weapon"] == toxocyst["path"] and body["fireMode"] == 1
                assert body.get("benchmarkTarget", body)["headshotChance"] == chance
                assert body["scenario"] == scenario, (endpoint, suffix, body["scenario"])
                assert hit in "\n".join(reply.lines), (endpoint, reply.lines)
                assert all("gaugeChargingHeadshots" not in line for line in reply.lines)
            if chance == 1.0:
                assert all("（100%弱点）" not in line for line in live_traits.lines)

        event.message_obj = SimpleNamespace(message=[type("Image", (), {})()])
        fields = SimpleNamespace(
            weapon="Burston",
            positive=[("critical_chance", 178.2), ("multishot", 142.2)],
            negative=[],
        )
        observed = {"rank": 8, "polarity": "vazarin"}
        expected_notice = len(calls)
        previous_notices = len(notices)
        with patch.object(host, "_confirmed_riven_image", return_value=(fields, observed)):
            screenshot = await build("配卡推荐 伯斯顿p 主要")
            assert (await build("配卡推荐 伯斯顿p 主要")).lines == screenshot.lines
        assert screenshot.title.startswith("配卡推荐 ·") and screenshot.lines[0].startswith("◆ ")
        assert len(calls) == expected_notice + 1 and calls[-1][0] == "/api/analyze", (
            screenshot.lines
        )
        [(before_solve, text)] = notices[previous_notices:]
        assert before_solve == expected_notice and "算完后自动发送结果" in text
        await host.damage_engine.close()


def test_mode_aliases_keep_weapon_and_target():
    assert {name: riven_rating.MODES[name] for name in ("灵化", "灵化形态", "震地")} == {
        "灵化": "cycle",
        "灵化形态": "incarnon",
        "震地": "slam",
    }
    for synonym, mode in riven_rating.MODES.items():
        target = {}
        tokens = parse(f"配卡推荐 Burston Prime {synonym} normal").content
        name, parsed_mode, *_ = _card_parameters(tokens, target=target)
        assert (
            name == ["Burston", "Prime"] and parsed_mode == mode and target == {"steel_path": False}
        )
    for name, variant in (
        ("Catchmoon Primary", "catchmoon_primary"),
        ("Catchmoon Secondary", "catchmoon_secondary"),
    ):
        weapon, mode, *_ = _card_parameters(name.split(), target={})
        assert (
            mode is None and riven_rating.match_weapon(" ".join(weapon))[0]["engine_id"] == variant
        )


def test_extended_recommendation_routes():
    for name, tags, default, heavy_route, slam_route in (
        ("Magistar", ["HAMMERS_STANCE"], "slam", "combo", "slamBullet"),
        ("Glaive Prime", ["GLAIVES_STANCE"], "heavy", "glaive", "slam"),
        ("Nikana Prime", [], "base", "combo", "slam"),
    ):
        (weapon,) = riven_rating.match_weapon(name)
        catalog = {**_live_catalog_weapon(name, {"index": 0}), "compatTags": tags}
        routes = {"base": "combo", "heavy": heavy_route, "slam": slam_route}
        for mode in (None, *routes):
            expected = mode or default
            selected, scope, scenario = portable_riven.recommendation_mode(weapon, catalog, mode)
            assert selected == expected and scope[1] == 0
            assert scenario == {
                "combatState": {"meleeRoute": routes[expected]},
                **({"heavyAttack": True} if expected != "base" else {}),
            }
    (weapon,) = riven_rating.match_weapon("Burston Prime")
    catalog = _live_catalog_weapon(
        "Burston Prime", {"index": 0}, {"index": 1, "isIncarnonForm": True}
    )
    for mode, scenario in (
        (None, {"incarnonCycle": True}),
        ("cycle", {"incarnonCycle": True}),
        ("incarnon", {}),
    ):
        label, scope, actual = portable_riven.recommendation_mode(weapon, catalog, mode)
        assert label == (mode or "cycle") and scope[1] == 1 and actual == scenario
    assert portable_riven.recommendation_mode(weapon, catalog, None, prefer_cycle=False) == (
        "incarnon",
        (catalog["path"], 1, 0.0),
        {},
    )
    for name, index in (("Angstrum", 1), ("Stug", 2)):
        (weapon,) = riven_rating.match_weapon(name)
        catalog = _live_catalog_weapon(name, {"index": 0}, {"index": index, "isIncarnonForm": True})
        assert portable_riven.recommendation_mode(
            weapon, catalog, None, require_analysis=False
        ) == (
            "incarnon",
            (catalog["path"], index, 0.0),
            {},
        )

    weapon = {"engine_id": "cast", "category": "Melee"}
    catalog = {"path": "/Cast", "fireModes": [{"index": 0}], "exalted": {"tier": "cast"}}
    assert portable_riven.recommendation_mode(weapon, catalog, "base", require_analysis=False) == (
        "base",
        ("/Cast", 0, 0.0),
        {},
    )


def test_precompute_melee_routes():
    def melee_weapon(name, **extra):
        return {
            "path": f"/{name}",
            "nameEn": name,
            "weaponType": "melee",
            "buildAvailable": True,
            "fireModes": [{"index": 0}],
            **extra,
        }

    magistar = melee_weapon("Magistar", compatTags=["HAMMERS_STANCE"])
    nikana = melee_weapon("Nikana Prime")
    clones = melee_weapon("Shadow Clones", exalted={"tier": "cast"})
    weapons = [magistar, nikana, clones]
    melee_sequence = SimpleNamespace(
        weapon_row=lambda _: {"slams": [{"event": "HeavySlam"}]},
        is_hammer=lambda rec: "HAMMERS_STANCE" in rec.get("compatTags", ()),
        build_schedule=lambda rec, route, **_: {"route": route},
    )
    melee_sequence.default_route = lambda rec: (
        "slamBullet" if melee_sequence.is_hammer(rec) else "combo"
    )
    app = SimpleNamespace(
        CATALOG={"weapons": weapons},
        EXPORTS={"weapons": {weapon["path"]: weapon for weapon in weapons}},
        optimizer=SimpleNamespace(resolve_record=lambda records, path, *, kind: records[path]),
    )
    with (
        patch.object(precomputed, "APP", app),
        patch.dict(sys.modules, {"wfarsenal": SimpleNamespace(melee_sequence=melee_sequence)}),
    ):
        scenes = {scene.name: scene for scene in precomputed.list_scenes(None)}

    base, heavy, slam = [scenes[f"Magistar {suffix}"] for suffix in ("fm0", "heavy", "slam")]
    assert base.mode_key == "0" and base.filename.endswith("__fm0.json")
    for scene, expected in (
        (base, {"combatState": {"meleeRoute": "combo"}}),
        (heavy, {"heavyAttack": True, "combatState": {"meleeRoute": "combo"}}),
        (slam, {"heavyAttack": True, "combatState": {"meleeRoute": "slamBullet"}}),
    ):
        assert precomputed.scene_payload(scene)["scenario"] == expected
        assert precomputed.identity(scene)["mode"]["scenario"] == expected
    assert "scenario" not in precomputed.identity(scenes["Nikana Prime fm0"])["mode"]
    assert "Shadow Clones fm0" in scenes
    assert {"Shadow Clones heavy", "Shadow Clones slam"}.isdisjoint(scenes)


def test_bundled_variant_from_command_parameters():
    for suffix, expected in (
        ("", "default"),
        ("标准靶", "default"),
        ("超宏", "overguard"),
        ("带超宏", "overguard"),
        ("有超宏", "overguard"),
        ("overguard", "overguard"),
        ("标准靶 超宏", "overguard"),
        ("护盾", "shield"),
        ("带盾", "shield"),
        ("有盾", "shield"),
        ("shield", "shield"),
        ("标准靶 护盾", "shield"),
        ("超宏50000", None),
        ("超宏 护盾", None),
        ("护盾 超宏", None),
        ("标准靶 超宏 护盾", None),
        ("护盾 超宏50000", None),
        ("百夫长9999", None),
        ("护盾 lv170", None),
        ("护盾 lv9999", None),
        ("护盾 爆头", "shield"),
        ("护盾 对 重机枪手", None),
        ("护盾 扩展槽 自动", None),
        ("超宏 爆头", "overguard"),
        ("超宏 对 重机枪手", None),
        ("超宏 扩展槽 自动", None),
    ):
        parsed = parse(f"词条推荐 伯斯顿p {suffix}")
        target = {}
        name, _, _, _, options = _card_parameters(parsed.content, target=target)
        assert name == ["伯斯顿p"], suffix
        assert portable_riven.bundled_variant(target, options) == expected, suffix
        assert portable_riven.standard_reference_query(target, options) == (expected is not None)


def _bundled_example(weapon, *, variant, mode=None):
    stat = {"code": "MA", "stat_id": "magnetic_damage", "nameEn": "Magnetic", "nameZh": "磁力"}
    dps, gain31, gain21 = {
        "default": (100, 3, 5),
        "overguard": (200, 21.7, 24.5),
        "shield": (300, 10, 11),
    }[variant]
    scene = precomputed.Scene(
        precomputed.fields(weapon, precomputed.WEAPON_FIELDS),
        {"index": 0, "kind": "base"} if mode is None else mode,
        None,
    )
    document = {
        "status": "ok",
        "build": {
            "full_dps": dps,
            "mods": [],
            "arcanes": [],
            "stance": None,
            "exilus": None,
            "without": {},
            "capacity": {"used": 0, "limit": 60, "forma": 0, "valid": True},
            "stats": dict.fromkeys(precomputed.PANEL_FIELDS),
        },
        "traits": {
            "baseline_dps": dps,
            "baseline_kpm": 10,
            "positive": [
                {
                    **stat,
                    "unit": "%",
                    "spliced": True,
                    "modeled": True,
                    "shown_31": 90,
                    "pct_31": gain31,
                    "shown_21": 105,
                    "pct_21": gain21,
                    **(
                        {"elem_build": "magnetic", "elem_build_roll": "31"}
                        if variant == "overguard"
                        else {}
                    ),
                }
            ],
            "negative": [],
        },
    }
    with patch.multiple(
        precomputed,
        TARGET=portable_riven.bundled_reference_target(variant=variant),
        STAT_DEFS={"MA": stat},
    ):
        return precomputed.compact_documents([scene], {scene.filename: document})


def test_bundled_headshot_rate_matches_each_mode():
    weapon = _live_catalog_weapon("Dual Toxocyst")
    weapon["weaponType"] = "pistol"
    path = weapon["path"]
    legacy_build, legacy_traits = _bundled_example(weapon, variant="default")
    builds = deepcopy(legacy_build)
    traits = deepcopy(legacy_traits)
    for kind, index in (("base", 0), ("incarnon", 1), ("cycle", 1)):
        mode = {"index": index, "kind": kind, "headshotChance": 1.0}
        if kind == "cycle":
            mode["scenario"] = {"incarnonCycle": True}
        key = kind if kind == "cycle" else str(index)
        new_build, new_traits = _bundled_example(weapon, variant="default", mode=mode)
        builds["weapons"][path].update(new_build["weapons"][path])
        traits["weapons"][path].update(new_traits["weapons"][path])
        assert (
            portable_riven._bundled_target(builds, builds["weapons"][path][key])["headshotChance"]
            == 1.0
        )
        for lang, expected in (
            ("zh", "弱点 100%（机制需爆头）"),
            ("en", "Weak points 100% (headshot-triggered)"),
        ):
            with i18n.use_lang(lang):
                lines = portable_riven.bundled_build_lines(builds, weapon, kind)
                assert expected in lines[0]
                lines = portable_riven.bundled_trait_lines(builds, traits, weapon, kind, "3+1-")
                assert any(expected in line for line in lines)
                for chance in (0.0, 0.5):
                    target = {"headshot": chance}
                    assert (
                        portable_riven.bundled_build_lines(builds, weapon, kind, target=target)
                        is None
                    )
                    assert (
                        portable_riven.bundled_trait_lines(
                            builds, traits, weapon, kind, "3+1-", target=target
                        )
                        is None
                    )

    assert portable_riven.bundled_build_lines(legacy_build, weapon, "base") is None
    assert portable_riven.bundled_trait_lines(builds, legacy_traits, weapon, "base", "3+1-") is None
    body = {"headshot": False}
    assert (
        "躯干" in portable_riven.bundled_build_lines(legacy_build, weapon, "base", target=body)[0]
    )
    assert any(
        "躯干" in line
        for line in portable_riven.bundled_trait_lines(
            legacy_build, legacy_traits, weapon, "base", "3+1-", target=body
        )
    )


def test_precomputed_headshot_weapons_and_cycle_profiles():
    from types import ModuleType

    names = ("Knell", "Knell Prime", "Dual Toxocyst", "Zymos", "Perigale", "Perigale Prime")
    base = {"index": 0}
    incarnon = {"index": 1, "isIncarnonForm": True}
    required = [
        _live_catalog_weapon(name, base, *((incarnon,) if name == "Dual Toxocyst" else ()))
        for name in names
    ]
    assert {weapon["path"] for weapon in required} == portable_riven.HEADSHOT_REQUIRED_WEAPONS
    torid = _live_catalog_weapon("Torid", base, incarnon)
    burston = _live_catalog_weapon("Burston Prime", base, incarnon)
    kill_charged = {**burston, "path": "/KillCharged", "nameEn": "Kill charged"}
    profiles = {
        required[2]["path"]: {"hits": 5},
        torid["path"]: {"chargeOn": "direct_hits"},
        burston["path"]: {"hits": 5},
        kill_charged["path"]: {"chargeOn": "kills"},
    }
    manual_combat = ModuleType("wfarsenal.manual_combat")
    manual_combat.reference = lambda: {"incarnon": profiles}
    with (
        patch.dict(sys.modules, {"wfarsenal.manual_combat": manual_combat}),
        patch.multiple(
            precomputed,
            APP=SimpleNamespace(CATALOG={"weapons": [*required, torid, burston, kill_charged]}),
            TARGET=portable_riven.bundled_reference_target(),
        ),
    ):
        scenes = {scene.name: scene for scene in precomputed.list_scenes(None)}
        for weapon in required:
            weapon_scenes = [scene for scene in scenes.values() if scene.key[0] == weapon["path"]]
            assert {scene.mode_key for scene in weapon_scenes} == (
                {"0", "1", "cycle"} if weapon["nameEn"] == "Dual Toxocyst" else {"0"}
            )
            for scene in weapon_scenes:
                assert precomputed.identity(scene)["mode"]["headshotChance"] == 1.0
                assert scene.target["headshotChance"] == 1.0
                assert precomputed.scene_payload(scene)["hitLocation"] == "head"
        for name in ("Dual Toxocyst", "Torid", "Kill charged"):
            assert scenes[f"{name} cycle"].scenario_options == {"scenario": {"incarnonCycle": True}}
        assert scenes["Burston Prime cycle"].scenario_options == {
            "scenario": {"incarnonCycle": True, "combatState": {"gaugeChargingHeadshots": True}}
        }


def test_precomputed_full_headshots_share_weakpoint_trait_baseline():
    from unittest.mock import Mock

    stats = {"W": {"code": "W"}, "Y": {"code": "Y"}}
    lines = {
        code: SimpleNamespace(
            unit="percent", value=100, negative=False, effect=SimpleNamespace(supported=True)
        )
        for code in stats
    }
    optimizer = SimpleNamespace(
        _evaluate=Mock(return_value=SimpleNamespace(score=10)),
        _set_shells=lambda _: {},
        _neutral_negative_codes=lambda _: (),
        _card_from_lines=lambda rows: rows,
        _percent_gain=lambda score, baseline: 100 * (score / baseline - 1),
        _FUSION_LINE_CODES=stats.keys(),
    )
    build = {"full_dps": 200, "exilus": None, "mods": []}
    ctx = {
        "metric": "killsPerMinute",
        "weapon": {},
        "scenario": {},
        "passives": (),
        "benchmarkContext": {"headshotChance": 1.0, "hit_location": "head"},
    }
    scene = precomputed.Scene({"path": "/Weapon"}, {"index": 0, "headshotChance": 1.0}, None)
    with (
        patch.multiple(
            precomputed,
            APP=SimpleNamespace(optimizer=optimizer, EXPORTS={"mods": {}}),
            STAT_DEFS=stats,
        ),
        patch.object(precomputed, "candidate_sets", return_value=((lines, {}), (lines, {}))),
        patch.object(precomputed, "reserve_ammo", return_value=None),
    ):
        matrix = precomputed.trait_matrix(scene, ctx, build)
    assert matrix["baseline_weakpoint_kpm"] is None
    assert len(optimizer._evaluate.call_args_list) == 5
    assert all(
        call.kwargs["target_context"] == ctx["benchmarkContext"]
        for call in optimizer._evaluate.call_args_list
    )
    assert all(
        not row["weakpoint"] and row["pct_31"] == row["pct_21"] == 0 for row in matrix["positive"]
    )


def test_gauge_charging_replay_defaults_and_compact_header():
    scenario = {"incarnonCycle": True, "combatState": {"victimStatusCount": 2}}
    state = {**scenario["combatState"], "gaugeChargingHeadshots": True}
    explicit_off = {"incarnonCycle": True, "combatState": {"gaugeChargingHeadshots": False}}
    result = {
        "fireMode": 1,
        "evaluationTarget": {
            **portable_riven._bundled_target({"target": portable_riven.bundled_reference_target()}),
            "armourStrip": 0,
            "elementalArmourStrip": 0,
        },
    }
    target = damage_calc.default_target()
    for context, chance, expected in (
        (scenario, 0.0, {**scenario, "combatState": state}),
        (scenario, 0.5, {**scenario, "combatState": state}),
        (scenario, 1.0, scenario),
        (explicit_off, 0.0, explicit_off),
    ):
        spec = {**target, "headshot": chance}
        options = {"scenario": context}
        fixed = damage_engine.portable_damage_request("/Gun", "/Enemy", [], spec, options=options)
        result["evaluationTarget"]["headshotChance"] = chance
        replay = portable_riven.build_replay_request(result, "/Gun", [], [], {}, options=options)
        assert fixed["scenario"] == replay["scenario"] == expected
        assert not damage_engine.combat_condition_lines(replay)
    assert scenario == {"incarnonCycle": True, "combatState": {"victimStatusCount": 2}}
    for invalid in (1, "true", None):
        try:
            damage_engine.with_portable_analysis_options(
                {}, {"scenario": {"combatState": {"gaugeChargingHeadshots": invalid}}}
            )
        except damage_engine.EngineError:
            pass
        else:
            raise AssertionError(f"非布尔攒条开关通过校验 {invalid!r}")

    charged = {**scenario, "combatState": state}
    cycle_scene = {
        "incarnon_cycle": True,
        "positive": [portable_riven._trait_result("critical_chance", 90, "%", 0, 10, None)],
        "negative": [],
    }
    torid = _live_catalog_weapon("Torid")
    for lang, long, short in (
        ("zh", "灵化循环（攒条全弱点）", "灵化循环"),
        ("en", "Incarnon Cycle (weakpoint charge)", "Incarnon cycle"),
    ):
        with i18n.use_lang(lang):
            assert riven_rating.incarnon_cycle_label({}, charged, 0.5) == long
            assert riven_rating.incarnon_cycle_label({}, charged, 1.0) == short
            assert riven_rating.incarnon_cycle_label(torid, charged, 0.0) == short
            assert riven_rating.incarnon_cycle_label({"chargeOn": "kills"}, charged, 0.0) == short
            lines = riven_rating.trait_matrix(
                {"category": "Rifle"},
                {
                    "base": ("主射", {**cycle_scene, "incarnon_cycle": False}),
                    "cycle": (long, cycle_scene),
                },
                bundled_roll="3+1-",
            )
            assert any(f"{short} KPM" in line for line in lines)
            assert all(long not in line for line in lines)


async def test_bundled_multimode_build_priority_and_silent_hits():
    host = Plugin()
    host.cfg["damage_engine_portable_port"] = 8765
    host._send_progress_notice = AsyncMock()
    host.riven_sale.lookup = AsyncMock(return_value=riven_sale.SaleResult("", "unavailable"))
    event = SimpleNamespace(unified_msg_origin="test")
    no_engine = AsyncMock(side_effect=AssertionError("随包路线不应请求实时目录"))
    with patch.object(portable_riven, "recommendation_weapons", no_engine):
        for name, default_key in (
            ("Magistar", "slam"),
            ("Sancti Magistar", "slam"),
            ("Glaive Prime", "heavy"),
            ("Nikana Prime", "0"),
        ):
            choices = riven_rating.match_weapon(name)
            _, builds, catalog = portable_riven.bundled_weapons(name, choices, {}, {})
            weapon = catalog[choices[0]["engine_id"]]
            modes = builds["weapons"][weapon["path"]]
            for lang, aliases, labels in (
                ("zh", ("平砍", "重击", "震地"), ("平砍", "重击", "重击震地")),
                ("en", ("base", "heavy", "slam"), ("Base", "Heavy attack", "Heavy slam")),
            ):
                with i18n.use_lang(lang):
                    auto_build = await host._h_loadout_recommend(
                        parse(f"配卡推荐 {name}"), event, "pc"
                    )
                    auto_traits = await host.query(f"词条推荐 {name}")
                    for key, alias, label in zip(
                        ("0", "heavy", "slam"), aliases, labels, strict=True
                    ):
                        if name == "Glaive Prime" and key == "heavy":
                            label = i18n.L("重击投掷", "Glaive throw")
                        build = await host._h_loadout_recommend(
                            parse(f"配卡推荐 {name} {alias}"), event, "pc"
                        )
                        traits = await host.query(f"词条推荐 {name} {alias}")
                        assert build.lines[0].startswith(f"◆ {label} · "), build.lines
                        assert traits.lines[1].startswith(f"· {label} · "), traits.lines
                        dps = f"{modes[key]['full_dps']:,.0f}"
                        assert "DPS" in build.lines[1] and build.lines[1].endswith(dps), build.lines
                        assert f"DPS {dps} ·" in traits.lines[2], traits.lines
                        assert (
                            sum(
                                line.startswith(("◆ 单词条模拟推荐", "◆ Single-trait simulation"))
                                for line in traits.lines
                            )
                            == 1
                        )
                        others = [item for item in aliases if item != alias]
                        hint = i18n.L(
                            f"※ 可加「{others[0]}」「{others[1]}」切换路线。",
                            f'※ Add "{others[0]}", "{others[1]}" to switch routes.',
                        )
                        assert hint in build.lines and hint in traits.lines
                        if key == default_key:
                            assert auto_build.lines == build.lines
                            assert auto_traits.lines == traits.lines
    no_engine.assert_not_awaited()
    host._send_progress_notice.assert_not_awaited()


def test_bundled_incarnon_build_priority():
    matrix = portable_riven._load_bundled(portable_riven.BUNDLED_TRAITS)
    for name in ("Boar Prime", "Burston Prime", "Torid", "Angstrum", "Stug"):
        choices = riven_rating.match_weapon(name)
        _, builds, catalog = portable_riven.bundled_weapons(name, choices, {}, {})
        weapon = catalog[choices[0]["engine_id"]]
        path = weapon["path"]
        modes = builds["weapons"][path]
        lines = portable_riven.bundled_build_lines(builds, weapon, None)
        if name in ("Angstrum", "Stug"):
            assert modes["cycle"]["buildIssue"] == precomputed.INCARNON_CYCLE_UNAVAILABLE
            assert lines[0].startswith("◆ 灵化形态 · ")
            assert all("默认展示灵化循环" not in line for line in lines)
            continue
        assert lines[0].startswith("◆ 灵化循环")
        assert lines[:-1] == portable_riven.bundled_build_lines(builds, weapon, "cycle")
        assert lines[-1] == "※ 默认展示灵化循环，可加「主射」「灵化形态」切换。"
        if name == "Burston Prime":
            assert modes["1"]["full_dps"] > modes["cycle"]["full_dps"]
        traits = portable_riven.bundled_trait_lines(builds, matrix, weapon, None, "3+1-")
        assert "词条　牌面中位值　主射 KPM　灵化形态 KPM　灵化循环 KPM" in traits

        with i18n.use_lang("en"):
            reduced = {**builds, "weapons": {path: {"1": modes["1"], "cycle": modes["cycle"]}}}
            lines = portable_riven.bundled_build_lines(reduced, weapon, None)
            assert (
                lines[-1] == '※ Showing Incarnon cycle by default. Add "incarnon" to switch modes.'
            )
        reduced = {**builds, "weapons": {path: {"0": modes["0"], "1": modes["1"]}}}
        assert portable_riven.bundled_build_lines(reduced, weapon, None)[0].startswith("◆ 灵化形态")
        reduced = {**builds, "weapons": {path: {"0": modes["0"]}}}
        assert portable_riven.bundled_build_lines(reduced, weapon, None)[0].startswith("◆ 主射")


def test_unmodeled_melee_incarnon_tier1_metadata():
    builds = portable_riven._load_bundled(portable_riven.BUNDLED_BUILDS)
    table = builds["catalog"]["weapons"]
    melee_incarnons = [
        weapon
        for row in table["rows"]
        if (weapon := dict(zip(table["columns"], row, strict=True)))["incarnon"]
        and weapon["weaponType"] in {"melee", "zaw"}
    ]
    has_gap = portable_riven._has_unmodeled_melee_incarnon_tier1
    assert len(melee_incarnons) == 28
    assert {weapon["nameEn"] for weapon in melee_incarnons if has_gap(weapon)} == {
        "Magistar",
        "Sancti Magistar",
        "Sibear",
        "Bo",
        "Bo Prime",
        "MK1-Bo",
        "Okina",
        "Okina Prime",
        "Ruvox",
    }
    complete = {"complete": True, "unsupported": []}
    gaps = ({"unsupported": ["an unmodeled effect"]}, {"complete": False})
    for identity in ({"weaponType": "melee"}, {"weaponType": "zaw"}, {"category": "Melee"}):
        for option in gaps:
            assert has_gap({**identity, "incarnon": [{"tier": 1, "options": [complete, option]}]})
            assert not has_gap({**identity, "incarnon": [{"tier": 2, "options": [option]}]})
        assert not has_gap({**identity, "incarnon": [{"tier": 1, "options": [complete]}]})
    assert not has_gap({"weaponType": "rifle", "incarnon": [{"tier": 1, "options": gaps}]})


async def test_melee_incarnon_tier1_note_on_recommendation_cards():
    cases = {
        "Magistar": True,
        "Sancti Magistar": True,
        "Hate": False,
        "Ceramic Dagger": False,
        "Nikana Prime": False,
    }
    host = Plugin()
    host.cfg["damage_engine_portable_port"] = 8765
    host.riven_sale.lookup = AsyncMock(return_value=riven_sale.SaleResult("", "unavailable"))
    weapons = {}
    for name in cases:
        (choice,) = choices = riven_rating.match_weapon(name)
        _, _, catalog = portable_riven.bundled_weapons(name, choices, {}, {})
        weapon = catalog[choice["engine_id"]]
        weapons[weapon["path"]] = {**weapon, "rivenType": "melee"}

    async def check_cards(*, legacy=False):
        for lang, note in (
            (
                "zh",
                "※ 灵化近战提示：已计入进化 2–4，进化 1 灵化变身期间的临时加成上游尚未建模，未计入当前数值。",
            ),
            (
                "en",
                "※ Incarnon Melee note: Tier 2–4 evolutions are modeled; "
                "temporary Tier 1 Incarnon form bonuses are not yet modeled upstream.",
            ),
        ):
            with i18n.use_lang(lang):
                for name, expected in cases.items():
                    traits = await host.query(f"词条推荐 {name}")
                    heading = (
                        i18n.L("◆ 洗卡优先看", "◆ Reroll priorities")
                        if legacy
                        else i18n.L(
                            "词条　牌面中位值　KPM 变化　备注",
                            "Trait　Median roll　KPM change　Notes",
                        )
                    )
                    assert heading in traits.lines, traits.lines
                    assert traits.lines.count(note) == expected, (name, lang, traits.lines)
                    if expected and not legacy:
                        assert traits.lines[traits.lines.index(note) - 1].startswith("※ ")
                    if legacy:
                        continue
                    build = await host._h_loadout_recommend(parse(f"配卡推荐 {name}"), None, "pc")
                    assert any(line.startswith("· 01　") for line in build.lines), build.lines
                    assert build.lines.count(note) == expected, (name, lang, build.lines)
                    if expected:
                        evolution = i18n.L("◆ 灵化进化", "◆ Incarnon evolutions")
                        assert build.lines[build.lines.index(evolution) + 1] == note

    no_engine = AsyncMock(side_effect=AssertionError("随包命中仍连接实时引擎"))
    with patch.object(portable_riven, "recommendation_weapons", no_engine):
        await check_cards()

    # 覆盖当前引擎的目录来源，以及回执单独提供元数据的情况。
    reply_only = {"Sancti Magistar", "Ceramic Dagger"}
    catalog = _live_catalog(
        *(
            {key: value for key, value in weapon.items() if key != "incarnon"}
            if weapon["nameEn"] in reply_only
            else weapon
            for weapon in weapons.values()
        )
    )
    catalog["statsByType"]["melee"] = catalog["statsByType"].pop("rifle")

    def with_weapon_metadata(result, body):
        weapon = weapons[body["weapon"]]
        if weapon["nameEn"] in reply_only:
            result["weapon"]["incarnon"] = weapon["incarnon"]
        return result

    async def engine(_, endpoint, body=None, **_kwargs):
        if endpoint == "/api/catalog":
            return catalog
        if endpoint == "/api/build":
            return with_weapon_metadata(_build_reply(body), body)
        assert endpoint == "/api/analyze"
        return with_weapon_metadata(_live_reply(body), body)

    for legacy in (False, True):
        matrix = AsyncMock(
            side_effect=lambda _, body: (
                None if legacy else with_weapon_metadata(_matrix_reply(body), body)
            )
        )
        with (
            TemporaryDirectory() as root,
            _without_bundled(),
            patch.object(damage_engine, "portable_request", engine),
            patch.object(portable_riven, "_trait_matrix_request", matrix),
            patch.object(portable_riven, "_mod_gains", AsyncMock(return_value={})),
        ):
            host.damage_engine = damage_engine.EngineService(Path(root))
            host.damage_engine.ensure_portable = AsyncMock()
            await check_cards(legacy=legacy)
            assert matrix.await_count == len(cases)
            await host.damage_engine.close()


async def test_bundled_target_recommendations_and_live_fallback():
    host = Plugin()
    host.cfg["damage_engine_portable_port"] = 8765
    host.riven_sale.lookup = AsyncMock(return_value=riven_sale.SaleResult("", "missing"))
    weapon = _live_catalog_weapon("Burston Prime", {"index": 0})
    default_builds, default_traits = _bundled_example(weapon, variant="default")
    overguard_builds, overguard_traits = _bundled_example(weapon, variant="overguard")
    shield_builds, shield_traits = _bundled_example(weapon, variant="shield")
    calls = []

    async def engine(_, endpoint, body=None, **_kwargs):
        if endpoint == "/api/catalog":
            return _live_catalog(weapon)
        target = body["benchmarkTarget"] if endpoint == "/api/analyze" else body
        enhancements = target["enemyEnhancements"]
        calls.append((endpoint, enhancements["noOverguard"], enhancements.get("shieldOverride")))
        return _build_reply(body) if endpoint == "/api/build" else _live_reply(body)

    async def loadout(suffix):
        return await host._h_loadout_recommend(parse(f"配卡推荐 伯斯顿p {suffix}"), None, "pc")

    with TemporaryDirectory() as root:
        seed = Path(root) / "seed"
        seed.mkdir()
        for name, document in (
            (portable_riven.BUNDLED_BUILDS, default_builds),
            (portable_riven.BUNDLED_TRAITS, default_traits),
            (portable_riven.BUNDLED_BUILDS_OVERGUARD, overguard_builds),
            (portable_riven.BUNDLED_TRAITS_OVERGUARD, overguard_traits),
            (portable_riven.BUNDLED_BUILDS_SHIELD, shield_builds),
            (portable_riven.BUNDLED_TRAITS_SHIELD, shield_traits),
        ):
            precomputed.write_json(seed / name, document)
        host.damage_engine = damage_engine.EngineService(Path(root))
        host.damage_engine.ensure_portable = AsyncMock()
        with (
            i18n.use_lang("zh"),
            patch.object(portable_riven, "paths", SimpleNamespace(PKG_DATA=seed)),
            patch.object(damage_engine, "portable_request", engine),
            patch.object(portable_riven, "_trait_matrix_request", AsyncMock(return_value=None)),
            patch.object(
                portable_riven,
                "build_view",
                side_effect=lambda _, __, row: [
                    portable_riven.scenario_line(row),
                    "◆ 实时配装",
                ],
            ),
        ):
            assert (await portable_riven.bundled_traits())["target"] == default_traits["target"]
            assert (await portable_riven.bundled_traits(damage_calc.target_option("护盾")))[
                "target"
            ] == shield_traits["target"]
            default_header = "钢铁之路 凶魂百夫长 Lv170 · 躯干 · 去超宏 · 无盾 · 30 秒"
            overguard_header = default_header.replace("去超宏", "带超宏")
            shield_header = default_header.replace("无盾", "模拟有盾（基准 3600）")
            for suffix, header, roll, gain, hint in (
                (
                    "",
                    default_header,
                    "3+1−",
                    "+3.0%",
                    "可加「2+1-」「超宏」「护盾」。",
                ),
                ("2+1-", default_header, "2+1−", "+5.0%", "默认看 3+1−，可加「超宏」「护盾」。"),
                (
                    "超宏",
                    overguard_header,
                    "3+1−",
                    "+21.7%",
                    "可加「2+1-」「护盾」，去「超宏」看默认。",
                ),
                (
                    "超宏 2+1-",
                    overguard_header,
                    "2+1−",
                    "+24.5%",
                    "默认看 3+1−，去「超宏」看默认。",
                ),
                (
                    "护盾",
                    shield_header,
                    "3+1−",
                    "+10.0%",
                    "可加「2+1-」「超宏」，去「护盾」看默认。",
                ),
                (
                    "护盾 2+1-",
                    shield_header,
                    "2+1−",
                    "+11.0%",
                    "默认看 3+1−，去「护盾」看默认。",
                ),
            ):
                reply = await host.query(f"词条推荐 伯斯顿p {suffix}")
                assert header in reply.lines[1], reply.lines
                notes = [line for line in reply.lines if line.startswith("※")]
                assert notes == [
                    f"※ 按满级 {roll} 中位值试算；「XX套」为对应元素配装同套对照。{hint}",
                    "※ KPM 为每分钟击杀数。",
                ], notes
                (magnetic,) = [line for line in reply.lines if line.startswith("· ⟦w⟧磁力伤害")]
                assert gain in magnetic and "带超宏" not in magnetic, magnetic
                assert ("磁力套" in magnetic) == (suffix == "超宏"), magnetic

            build = await loadout("超宏")
            assert overguard_header in build.lines[0]
            assert "随包参考 · 30 秒平均 DPS 200（含超宏防护）" in build.lines[1]
            build = await loadout("护盾")
            assert shield_header in build.lines[0]
            assert "随包参考 · 30 秒平均 DPS 300" in build.lines[1]
            assert default_header in (await loadout("")).lines[0]
            with i18n.use_lang("en"):
                for suffix, hint in (
                    ("", 'Add "2+1-", "overguard", or "shield".'),
                    ("2+1-", 'The default is 3+1−. Add "overguard" or "shield".'),
                    (
                        "overguard",
                        'Add "2+1-" or "shield". Omit "overguard" for the default target.',
                    ),
                    (
                        "overguard 2+1-",
                        'The default is 3+1−. Omit "overguard" for the default target.',
                    ),
                    ("shield", 'Add "2+1-" or "overguard". Omit "shield" for the default target.'),
                    ("shield 2+1-", 'The default is 3+1−. Omit "shield" for the default target.'),
                ):
                    english = await host.query(f"词条推荐 伯斯顿p {suffix}")
                    roll = "2+1−" if "2+1-" in suffix else "3+1−"
                    notes = [line for line in english.lines if line.startswith("※")]
                    assert notes == [
                        f"※ Median max-rank {roll} rolls; named element tags use a matching no-Riven build. "
                        f"{hint}",
                        "※ KPM means kills per minute.",
                    ], notes
            assert not calls

            combined = await host.query("词条推荐 伯斯顿p 超宏 护盾")
            assert "带超宏 · 模拟有盾（基准 3600）" in combined.lines[0]
            choices = riven_rating.match_weapon("伯斯顿p")
            for token, header, traits, build_name, trait_name in (
                (
                    "超宏",
                    overguard_header,
                    overguard_traits,
                    portable_riven.BUNDLED_BUILDS_OVERGUARD,
                    portable_riven.BUNDLED_TRAITS_OVERGUARD,
                ),
                (
                    "护盾",
                    shield_header,
                    shield_traits,
                    portable_riven.BUNDLED_BUILDS_SHIELD,
                    portable_riven.BUNDLED_TRAITS_SHIELD,
                ),
            ):
                targets = damage_calc.target_option(token)
                _, bundle, catalog = portable_riven.bundled_weapons("伯斯顿p", choices, targets, {})
                assert (
                    portable_riven.bundled_trait_lines(
                        bundle,
                        {**traits, "target": default_traits["target"]},
                        catalog["burston_prime"],
                        None,
                        "3+1-",
                    )
                    is None
                )
                precomputed.write_json(seed / trait_name, default_traits)
                assert await portable_riven.bundled_traits(targets) is None
                (seed / trait_name).unlink()
                live_traits = await host.query(f"词条推荐 伯斯顿p {token}")
                assert header in live_traits.lines[0]
                assert any("低影响负面" in line for line in live_traits.lines)
                assert "随包参考" in (await loadout(token)).lines[1]

                precomputed.write_json(seed / build_name, default_builds)
                assert portable_riven.bundled_weapons("伯斯顿p", choices, targets, {}) == (
                    choices,
                    None,
                    {},
                )
                (seed / build_name).unlink()
                live_build = await loadout(token)
                assert header in live_build.lines[0] and live_build.lines[1] == "◆ 实时配装"
            assert calls == [
                ("/api/analyze", False, 3600.0),
                ("/api/analyze", False, None),
                ("/api/build", False, None),
                ("/api/analyze", True, 3600.0),
                ("/api/build", True, 3600.0),
            ]
        await host.damage_engine.close()


def test_precompute_merge_targets_and_output_paths():
    weapon = precomputed.fields(
        {"path": "/MockWeapon", "nameEn": "Mock"}, precomputed.WEAPON_FIELDS
    )
    scene = precomputed.Scene(weapon, {"index": 0}, "mock")
    cast_weapon = {"path": "/CastWeapon", "weaponType": "melee", "exalted": {"tier": "cast"}}
    retired = precomputed.Scene(cast_weapon, {"index": 0, "kind": "heavy"}, None)
    with TemporaryDirectory() as root:
        root = Path(root)
        (root / retired.filename).touch()
        app_path = root / "app.py"
        app_path.touch()
        app = SimpleNamespace(
            __file__=str(app_path),
            ANALYSIS_STATICS_RUNTIME_INPUTS=precomputed.FINGERPRINT,
            EXPORTS={"buildLabel": precomputed.BUILD_LABEL},
            CATALOG={"weapons": [cast_weapon]},
            optimizer=SimpleNamespace(RIVEN_BASE_VALUES={}, RIVEN_RULES={}),
            _disable_analysis_statics_persistence=lambda: None,
        )
        scene_path = root / scene.filename
        cli = ["precompute", "--engine-dir", str(root), "--in-dir", str(root)]
        with (
            patch.dict(sys.modules, {"app": app}),
            patch.dict(os.environ, {"PYTHONHASHSEED": "0"}),
            patch.object(sys, "path", sys.path.copy()),
            patch.object(sys, "dont_write_bytecode", sys.dont_write_bytecode),
            patch.multiple(
                precomputed, APP=None, OUT_DIR=None, TARGET={}, STAT_DEFS={}, TEMPLATES={}
            ),
            patch.object(precomputed, "list_scenes", return_value=[scene]),
        ):
            precomputed.setup_engine(root)
            default_target = precomputed.TARGET
            precomputed.setup_engine(root, variant="overguard")
            overguard_target = precomputed.TARGET
            precomputed.setup_engine(root, variant="shield")
            shield_target = precomputed.TARGET
            assert default_target["enemyEnhancements"]["noOverguard"] is True
            assert "shieldOverride" not in default_target["enemyEnhancements"]
            for filename in (portable_riven.BUNDLED_BUILDS, portable_riven.BUNDLED_TRAITS):
                seed = paths.PKG_DATA / filename
                if seed.is_file():
                    assert json.loads(seed.read_bytes())["target"] == default_target
            assert overguard_target == {
                **default_target,
                "enemyEnhancements": {**default_target["enemyEnhancements"], "noOverguard": False},
            }
            assert shield_target == {
                **default_target,
                "enemyEnhancements": {
                    **default_target["enemyEnhancements"],
                    "shieldOverride": 3600.0,
                },
            }
            for target, flags, expected in (
                (
                    default_target,
                    ["--merge"],
                    ("portable_reference_builds.json", "portable_trait_matrix.json"),
                ),
                (
                    overguard_target,
                    ["--merge-only", "--overguard"],
                    (
                        "portable_reference_builds_overguard.json",
                        "portable_trait_matrix_overguard.json",
                    ),
                ),
                (
                    overguard_target,
                    ["--merge", "--overguard", "--builds-out", str(root / "custom-builds.json")],
                    ("custom-builds.json", "portable_trait_matrix_overguard.json"),
                ),
                (
                    overguard_target,
                    ["--merge", "--overguard", "--traits-out", str(root / "custom-traits.json")],
                    ("portable_reference_builds_overguard.json", "custom-traits.json"),
                ),
                (
                    shield_target,
                    ["--merge", "--shield"],
                    ("portable_reference_builds_shield.json", "portable_trait_matrix_shield.json"),
                ),
            ):
                document = {
                    **precomputed.identity(scene),
                    "target": target,
                    "status": "unsupported",
                    "build": None,
                    "traits": None,
                }
                precomputed.write_json(
                    scene_path, {**document, "sha256": precomputed.digest(document)}
                )
                with (
                    patch.object(sys, "argv", [*cli, *flags]),
                    patch.object(sys, "stdout", StringIO()),
                    patch.object(precomputed, "write_json", return_value=0) as write,
                ):
                    precomputed.main()
                writes = [call.args for call in write.call_args_list]
                assert tuple(path.name for path, _ in writes) == expected
                assert all(content["target"] == target for _, content in writes)
                assert set(writes[1][1]["flags"]) == {
                    "modeled",
                    "spliced",
                    "neutral",
                    "weakpoint",
                    "reoptimized",
                }

            unknown = root / "unknown__heavy.json"
            unknown.touch()
            with (
                patch.object(sys, "argv", [*cli, "--merge", "--shield"]),
                patch.object(sys, "stderr", StringIO()) as error,
                patch.object(precomputed, "write_json") as write,
            ):
                try:
                    precomputed.main()
                except SystemExit as exc:
                    assert exc.code == 1 and "输入包含未知场景" in error.getvalue()
                else:
                    raise AssertionError("合并应拒绝未知的重击场景")
                write.assert_not_called()
            unknown.unlink()

            with (
                patch.object(sys, "argv", [*cli, "--merge"]),
                patch.object(sys, "stderr", StringIO()) as error,
                patch.object(precomputed, "write_json") as write,
            ):
                try:
                    precomputed.main()
                except SystemExit as exc:
                    assert exc.code == 1 and "场景元数据不符" in error.getvalue()
                else:
                    raise AssertionError("默认档合并应拒绝带护盾场景")
                write.assert_not_called()

            with (
                patch.object(sys, "argv", [*cli, "--merge", "--overguard", "--shield"]),
                patch.object(sys, "stderr", StringIO()) as error,
                patch.object(precomputed, "prepare_engine") as prepare,
            ):
                try:
                    precomputed.main()
                except SystemExit as exc:
                    assert exc.code == 2 and "--overguard" in error.getvalue()
                    assert "--shield" in error.getvalue()
                else:
                    raise AssertionError("--overguard 与 --shield 应互斥")
                prepare.assert_not_called()

            for variant, target in (("overguard", overguard_target), ("shield", shield_target)):
                flags = [f"--{variant}", "--backfill-element-baselines", "--out-dir", str(root)]
                with (
                    patch.object(sys, "argv", ["precompute", "--engine-dir", str(root), *flags]),
                    patch.object(precomputed, "run_shard") as run_shard,
                ):
                    precomputed.main()
                run_shard.assert_called_once()
                args, selected = run_shard.call_args.args
                assert args.variant == variant and args.backfill_element_baselines
                assert selected == [scene] and precomputed.TARGET == target


def test_precompute_element_baselines_use_selected_target():
    element_types = {
        element for targets in precomputed.ELEMENT_TARGETS.values() for element in targets
    }
    scene = precomputed.Scene({"path": "/MockWeapon", "weaponType": "rifle"}, {"index": 0}, None)
    candidates = {code: code for code in ("4", "MA")}
    for variant in ("default", "overguard", "shield"):
        target = portable_riven.bundled_reference_target(variant=variant)
        benchmark = {"enemy_enhancements": target["enemyEnhancements"]}
        rows = [
            {"code": code, "stat_id": stat, "modeled": True, "pct_31": -5, "pct_21": -6}
            for code, stat in (("4", "heat_damage"), ("MA", "magnetic_damage"))
        ]
        document = {
            "status": "ok",
            "build": {"mods": [], "incarnonChoices": {}, "exilus": None},
            "traits": {"positive": rows},
        }

        def context(payload, **_kwargs):
            assert payload["enemyEnhancements"]["noOverguard"] is (variant != "overguard")
            assert payload["enemyEnhancements"].get("shieldOverride") == (
                3600.0 if variant == "shield" else None
            )
            return None, {
                "metric": "killsPerMinute",
                "weapon": {},
                "scenario": {},
                "passives": None,
                "benchmarkContext": benchmark,
                "capacityLimit": 60,
                "candidates": [],
            }

        def optimize(_weapon, _candidates, *, target_context, **_kwargs):
            assert target_context == benchmark
            return SimpleNamespace(score=10)

        optimizer = SimpleNamespace(
            _ELEMENT_TARGET_TYPES={element: element for element in element_types},
            _card_from_lines=list,
            optimize_for_element=optimize,
        )
        with (
            patch.multiple(
                precomputed,
                APP=SimpleNamespace(optimizer=optimizer, build_weapon_payload=context),
                TARGET=target,
            ),
            patch.object(precomputed, "candidate_sets", return_value=((candidates, {}),) * 2),
            patch.object(precomputed, "element_trial_gain", return_value=10) as trial,
        ):
            assert precomputed.element_baseline_rows(document) == rows
            precomputed.apply_element_baselines(scene, document, rows)
        trials = [call.args for call in trial.call_args_list]
        assert {riven[0] for _, _, _, riven, _, _ in trials} == candidates.keys()
        assert all(options["target_context"] == benchmark for *_, options in trials)
        assert precomputed.element_baseline_rows(document) == []
        assert rows[1]["elem_build"] == "magnetic"


async def test_standalone_errors():
    for problem, error, text in (
        (httpx.ReadTimeout, damage_engine.EngineTimeout, "请求超时"),
        (httpx.ConnectError, damage_engine.EngineUnreachable, "独立引擎未运行或无法连接"),
    ):

        async def fail(_request):
            raise problem("test")

        client = httpx.AsyncClient(transport=httpx.MockTransport(fail))
        with patch.object(damage_engine.httpx, "AsyncClient", return_value=client):
            try:
                await damage_engine.portable_request(8765, "/api/analyze", {}, timeout=150)
            except error as exc:
                assert text in str(exc)
            else:
                raise AssertionError(f"expected {error}")


@_without_bundled()
async def test_legacy_view_does_not_override_live_fallback():
    braton = _live_catalog_weapon("Braton Prime", {"index": 0})
    host = Plugin()
    host.cfg["damage_engine_portable_port"] = 8765
    scope = portable_riven.MODES["braton_prime", "base"]
    braton["path"] = scope[0]
    prepared = {
        **_portable_score(scope, 100),
        "recommendedPositiveTiers": [],
        "recommendedNegative": ["Zoom"],
        "fatalNegative": [],
        "fatalNegativeAbove": {},
    }
    host.riven_sale.lookup = AsyncMock(
        return_value=riven_sale.SaleResult(url="", status="unavailable")
    )
    calls = []
    status = 404

    async def trait_endpoint(request):
        assert request.url.path == "/api/trait-matrix"
        calls.append(request.url.path)
        return httpx.Response(status, json={"error": {"message": "endpoint not found"}})

    client = httpx.AsyncClient

    async def engine(_, endpoint, body=None, **_kwargs):
        if endpoint == "/api/catalog":
            return _live_catalog(braton)
        calls.append(endpoint)
        if endpoint == "/api/analyze":
            assert "_searchBudgetSeconds" not in body
        return _build_reply(body) if endpoint == "/api/build" else _live_reply(body)

    with TemporaryDirectory() as root:
        host.damage_engine = damage_engine.EngineService(Path(root))
        host.damage_engine.root.mkdir()
        (host.damage_engine.root / "portable-views.json").write_text(
            json.dumps({"engineFingerprint": portable_riven.FINGERPRINT, "entries": [prepared]})
        )
        host.damage_engine.ensure_portable = AsyncMock()
        with (
            patch.object(damage_engine, "portable_request", engine),
            patch.object(portable_riven, "build_view", return_value=["◆ 无紫卡参考配装"]) as view,
            patch.object(
                httpx,
                "AsyncClient",
                side_effect=lambda **kwargs: client(
                    transport=httpx.MockTransport(trait_endpoint), **kwargs
                ),
            ),
        ):
            traits = await host.query("词条推荐 布莱顿p 主射 标准靶")
            assert any("低影响负面" in line for line in traits.lines)
            assert (await host.query("词条推荐 布莱顿p 主射 标准靶")).lines == traits.lines
            build = await host._h_loadout_recommend(
                parse("配卡推荐 布莱顿p 主射 标准靶"), None, "pc"
            )
            assert build.lines == ["◆ 无紫卡参考配装"]
            view.assert_awaited_once()
            assert calls == ["/api/trait-matrix", "/api/analyze", "/api/build"]
            status = 422
            invalid = await host.query("词条推荐 布莱顿p 主射 标准靶 2+1-")
            assert calls == ["/api/trait-matrix", "/api/analyze", "/api/build", "/api/trait-matrix"]
            assert any("endpoint not found" in line for line in invalid.lines)
            assert all("洗卡优先看" not in line for line in invalid.lines)
        await host.damage_engine.close()


async def test_live_traits_search_one_default_per_variant():
    weapons = [
        {
            **_live_catalog_weapon(
                "Lex Prime", {"index": 0}, {"index": 1, "isAlternateFire": True}
            ),
            "nameEn": "Catalog Rifle",
            "path": "/CatalogRifle/Base",
        },
        {
            **_live_catalog_weapon("Lex Prime", {"index": 0}, {"index": 2, "isIncarnonForm": True}),
            "nameEn": "Catalog Rifle",
            "path": "/CatalogRifle/Incarnon",
        },
    ]
    calls = []
    host = Plugin()
    host.cfg["damage_engine_portable_port"] = 8765
    event = SimpleNamespace(unified_msg_origin="test")
    host._send_progress_notice = AsyncMock()
    host.riven_sale.lookup = AsyncMock(
        return_value=riven_sale.SaleResult(url="", status="unavailable")
    )

    async def engine(_port, endpoint, body=None, **_options):
        assert endpoint == "/api/catalog"
        return _live_catalog(*weapons)

    async def matrix(_port, body):
        assert host.damage_engine.simulations.active == 1
        calls.append((body["weapon"], body["fireMode"]))
        return _matrix_reply(body)

    with (
        TemporaryDirectory() as root,
        patch.object(damage_engine, "portable_request", engine),
        patch.object(portable_riven, "_trait_matrix_request", matrix),
    ):
        host.damage_engine = damage_engine.EngineService(Path(root))
        host.damage_engine.ensure_portable = AsyncMock()
        parsed = parse("词条推荐 Catalog Rifle")
        reply = await host._h_riven_traits(parsed, event, "pc")
        assert calls == [(weapons[0]["path"], 0), (weapons[1]["path"], 2)]
        assert (await host._h_riven_traits(parsed, event, "pc")).lines == reply.lines
        host._send_progress_notice.assert_awaited_once()
        assert sum("凶魂百夫长 Lv170" in line for line in reply.lines) == 2
        assert all("旧版随包" not in line for line in reply.lines)
        await host.damage_engine.close()


@_without_bundled()
async def test_recommendation_requires_engine_and_preserves_sale():
    host = Plugin()
    engine = AsyncMock()
    commands = ("词条推荐 安培克斯", "配卡推荐 安培克斯")

    async def replies():
        return [
            await host.query(commands[0]),
            await host._h_loadout_recommend(parse(commands[1]), None, "pc"),
        ]

    with patch.object(damage_engine, "portable_request", engine):
        traits, build = await replies()
        assert any("启用独立引擎后可查看模拟词条推荐" in line for line in traits.lines)
        assert any("出售参考" in line for line in traits.lines)
        assert any("后台「伤害引擎」启用完整模拟" in line for line in build.lines)
        with i18n.use_lang("en"):
            traits, build = await replies()
            assert any("Enable the standalone engine" in line for line in traits.lines)
            assert any("Selling reference" in line for line in traits.lines)
            assert any("Full Simulation under Damage Engine" in line for line in build.lines)
        engine.assert_not_awaited()
        host.cfg["damage_engine_portable_port"] = 8765
        with TemporaryDirectory() as root:
            host.damage_engine = damage_engine.EngineService(Path(root))
            host.damage_engine.ensure_portable = AsyncMock()
            engine.return_value = _live_catalog()
            for reply in await replies():
                assert any("独立引擎暂不支持这把武器" in line for line in reply.lines)
            host.damage_engine.portable_catalog.drop_prefix("")
            engine.side_effect = damage_engine.EngineError("连接失败")
            for reply in await replies():
                assert any("独立引擎推荐暂不可用 · 连接失败" in line for line in reply.lines)
                assert all("旧版随包" not in line and "KPM" not in line for line in reply.lines)
            await host.damage_engine.close()


@_without_bundled()
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
    assert "启用独立引擎后可查看模拟词条推荐" in body
    assert "市场挂牌" not in body
    assert all(label in body for label in ("出售参考", "正词条 多重", "负词条 变焦"))
    assert "出售说明" not in body and "原表备注" not in body
    assert not reply.extra_text and sale.url not in body
    lookup.assert_awaited_once()
    host.client.wm_riven_auctions.assert_not_awaited()
    with i18n.use_lang("en"):
        stale = riven_sale.SaleResult(sale.url, "stale", sale.row)
        assert riven_sale.lines(stale) == [
            "◆ Selling reference",
            "· Positive traits 多重",
            "· Negative traits 变焦",
            "· Showing the last available reference",
        ]


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
        custom = await host._h_loadout_recommend(parse("配卡推荐 布莱顿p 百夫长9999"), None, "pc")
        assert any("伤害引擎" in line for line in custom.lines)
        assert (await host.query("词条推荐 布莱顿")).title == "词条推荐 · 布莱顿"
        assert (await host.query("词条推荐 安培克斯p")).title == "词条推荐"


async def test_bundled_missing_traits_show_on_recommendation_cards():
    host = Plugin()
    host.cfg["damage_engine_mode"] = "builtin"
    sale = riven_sale.SaleResult("", "missing")
    with i18n.use_lang("zh"), patch.object(host.riven_sale, "lookup", return_value=sale):
        for roll in ("3+1-", "2+1-"):
            for weapon, labels in (
                ("野猪 Prime", ("射速",)),
                ("伯斯顿 Prime", ("射速",)),
                ("侍刃 Prime", ("攻击速度", "切割", "滑行攻击暴击几率")),
            ):
                reply = await host.query(f"词条推荐 {weapon} {roll}")
                for label in labels:
                    rows = [line for line in reply.lines if line.startswith(f"· ⟦w⟧{label}⟦/w⟧　")]
                    assert len(rows) == 2, (weapon, roll, label, rows)
                    assert {line.split("　")[1][3] for line in rows} == {"+", "-"}, rows


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
        ("boar_prime", "base"): (portable_riven.MODES["boar_prime", "incarnon"][0], 0, 0.0),
        ("boar_prime", "incarnon"): portable_riven.MODES["boar_prime", "incarnon"],
        ("braton_prime", "base"): portable_riven.MODES["braton_prime", "base"],
    }
    replies = {
        scope: _portable_score(scope, score)
        for scope, score in zip(modes.values(), (700, 400, 900, 1000))
    }

    async def solve(_host, _event, port, mode, card, polarity, **kwargs):
        assert port == 8765 and polarity == "madurai" and card["rank"] == 8
        assert card["bonuses"][0] == {"id": "multishot", "value": 138.9}
        assert card["weapon"] in {"boar", "boar_prime"} and mode in modes.values()
        return replies[mode]

    async def query(command):
        parsed = parse(command)
        assert parsed.command == "loadoutrecommend"
        return await host._h_loadout_recommend(parsed, event, "pc")

    with (
        patch.object(host, "_confirmed_riven_image", return_value=(fields, observed)),
        patch.object(portable_riven, "MODES", modes),
        patch.object(
            portable_riven,
            "recommendation_weapons",
            side_effect=lambda _, __, ___, choices: (
                choices,
                {
                    wid: {
                        "path": path,
                        "fireModes": [
                            {"index": fire, "isIncarnonForm": label == "incarnon"}
                            for (variant, label), (_, fire, _) in modes.items()
                            if variant == wid
                        ],
                    }
                    for (wid, _), (path, _, _) in modes.items()
                },
            ),
        ),
        patch.object(portable_riven, "analyze", side_effect=solve) as search,
        patch.object(portable_riven, "build_view", return_value=["◆ 推荐配装"]) as view,
    ):
        best = await query("配卡推荐")
        assert best.title == "配卡推荐 · 野猪 Prime"
        assert "多组工况" in "\n".join(best.lines)
        assert search.await_count == 2
        assert [call.args[3] for call in search.await_args_list] == [
            modes["boar", "base"],
            modes["boar_prime", "incarnon"],
        ]
        assert search.await_args.kwargs["options"]["scenario"]["incarnonCycle"] is True
        view.assert_awaited_once_with(
            host, 8765, replies[modes["boar_prime", "incarnon"]], with_riven=True
        )
        search.reset_mock()
        picked = await query("配卡推荐 野猪 Prime 主射")
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
        replies[modes["boar", "base"]]["searchWork"]["limited"] = 1
        partial = await query("配卡推荐")
        assert partial.title == "配卡推荐 · 野猪 Prime"
        assert "部分候选未完成" in "\n".join(partial.lines)
    for entry in ("紫卡评级 野猪", "紫卡推荐 野猪", "rivenrating Boar", "rivenrecommend Boar"):
        parsed = parse(entry)
        assert parsed is None or parsed.command != "rivenrating"


def _portable_score(scope, score):
    path, index, headshot = scope
    return {
        "weapon": {"path": path},
        "fireMode": index,
        "engineFingerprint": portable_riven.FINGERPRINT,
        "scoreMetric": "windowedDps",
        "evaluationTarget": {
            "enemyPath": portable_riven.TARGET,
            "enemyName": "Thrax Centurion",
            "level": 170,
            "steelPath": True,
            "headshotChance": headshot,
            "windowSeconds": 30,
            "hitLocation": "body",
            "armourStrip": 0,
            "elementalArmourStrip": 0,
            "enemyEnhancements": {"eximus": False, "noOverguard": True},
        },
        "targetCalibration": {"hardnessNodes": [0, 1, 2]},
        "scoringValues": {"actual": score},
        "searchWork": {"limited": 0},
    }


async def test_screenshot_reference_selects_variant_and_falls_back():
    host = Plugin()
    host.cfg["damage_engine_portable_port"] = 8765
    event = SimpleNamespace(message_obj=SimpleNamespace(message=[type("Image", (), {})()]))
    fields = SimpleNamespace(
        weapon="Burston",
        positive=[("crit_chance", 180.6), ("corrosive_damage", 111.1), ("multishot", 105.9)],
        negative=[("magazine_capacity", 49.1)],
    )
    observed = {"rank": 8, "polarity": "vazarin"}
    catalog = {
        key: _live_catalog_weapon(name, {"index": 0}, {"index": 1, "isIncarnonForm": True})
        for key, name in (("burston", "Burston"), ("burston_prime", "Burston Prime"))
    }
    prime = catalog["burston_prime"]["path"]
    normal = catalog["burston"]["path"]
    arcane = {"path": "/Lotus/Upgrades/CosmeticEnhancers/Offensive/PrimaryDamageOnKill", "rank": 5}

    async def query(suffix=""):
        return await host._h_loadout_recommend(parse(f"配卡推荐 {suffix}"), event, "pc")

    async def solve(_host, _event, _port, scope, *_args, **kwargs):
        result = _portable_score(scope, 99 if scope[0] == normal else 80)
        if target := kwargs["options"].get("benchmarkTarget"):
            result["evaluationTarget"].update(
                {key: val for key, val in target.items() if key != "enemy"},
                enemyPath=target["enemy"],
            )
        return result

    with (
        patch.object(host, "_confirmed_riven_image", return_value=(fields, observed)),
        patch.object(portable_riven, "recommendation_weapons", return_value=((), catalog)),
        patch.object(portable_riven, "build_riven", side_effect=solve) as fast,
        patch.object(portable_riven, "analyze", side_effect=solve) as fallback,
        patch.object(
            portable_riven, "build_view", side_effect=lambda *_a, **_k: ["◆ 推荐配装"]
        ) as view,
    ):
        for text, variant in (("", "default"), ("超宏", "overguard"), ("有盾", "shield")):
            fast.reset_mock()
            reply = await query(text)
            assert reply.title == "配卡推荐 · 伯斯顿 Prime"
            assert "同族共 2 变体" in reply.lines[1]
            fast.assert_awaited_once()
            assert fast.await_args.args[3][:2] == (prime, 1)
            options = fast.await_args.kwargs["options"]
            assert options["exilus"] == {"mode": "auto"} and "arcanes" not in options
            assert options["scenario"] == {"incarnonCycle": True}
            assert "默认展示灵化循环" in reply.lines[-1]
            if variant != "default":
                assert (
                    options["benchmarkTarget"]["enemyEnhancements"]
                    == (
                        portable_riven.bundled_reference_target(variant=variant)[
                            "enemyEnhancements"
                        ]
                    )
                )
            assert view.await_args.args[2]["referenceDps"] > 0
        fallback.assert_not_awaited()

        fast.reset_mock(side_effect=True)
        fast.side_effect = damage_engine.EngineBuildError("build unsupported")
        reply = await query()
        assert reply.title == "配卡推荐 · 伯斯顿 Prime"
        fast.assert_awaited_once()
        fallback.assert_awaited_once()
        assert fallback.await_args.kwargs["options"]["arcanes"] == [arcane]
        assert fallback.await_args.kwargs["options"]["exilus"] == {"mode": "auto"}

        fallback.reset_mock()
        locked = {
            "arcanes": [{"path": "/Deadhead", "rank": 3}],
            "exilus": {"path": "/Exilus", "rank": 2},
        }
        with patch("core.commands.riven._card_equipment", return_value=locked):
            reply = await query("伯斯顿p 赋能 主要·死首 3级 扩展槽 私法补给 2级")
        assert reply.title == "配卡推荐 · 伯斯顿 Prime"
        assert not any("自动选" in line for line in reply.lines), reply.lines
        assert all(
            fallback.await_args.kwargs["options"][key] == value for key, value in locked.items()
        )

        fast.reset_mock()
        fallback.reset_mock()
        reply = await query("等级7")
        assert reply.title == "配卡推荐 · 伯斯顿 Prime"
        assert fallback.await_args.args[4]["rank"] == 7
        fast.assert_not_awaited()

        fallback.reset_mock()
        with patch.object(portable_riven, "loadout_references", return_value=None):
            reply = await query()
        assert reply.title == "配卡推荐 · 伯斯顿"
        assert "已比较 2 个变体" in reply.lines[1]
        assert [call.args[3][0] for call in fallback.await_args_list] == [normal, prime]
        fast.assert_not_awaited()


async def test_loadout_with_unspecified_riven_polarity():
    host = Plugin()
    host.cfg["damage_engine_portable_port"] = 8765
    weapon = _live_catalog_weapon(
        "Burston Prime", {"index": 0}, {"index": 1, "isIncarnonForm": True}
    )
    weapon["polarities"] = [None, "AP_TACTIC"] + [None] * 6
    catalog = _live_catalog(weapon)
    catalog["statsByType"]["rifle"] = [
        {"code": code, "baseValue": base, "canPositive": True}
        for code, base in (("0", 150), ("S", 90))
    ]
    analyses, builds, replays = [], [], []
    fields = SimpleNamespace(
        weapon="Burston", positive=[("crit_chance", 178.2), ("multishot", 106.9)], negative=[]
    )
    observed = {"rank": 8, "polarity": None}
    event = SimpleNamespace(message_obj=SimpleNamespace(message=[type("Image", (), {})()]))

    async def engine(_port, endpoint, body=None, **_kwargs):
        if endpoint == "/api/catalog":
            return catalog
        if endpoint == "/api/analyze":
            analyses.append(body)
            reply = {**_live_reply(body), "actualGain": 20}
            plan = reply["actual"]["forma"]
            polarity = body["rivenCard"]["polarity"]
            changes = {"1": polarity} if polarity != "AP_TACTIC" else {}
            plan.update(total=len(changes), slots=changes)
            plan["placement"][1]["polarity"] = polarity
            return reply
        if endpoint == "/api/build":
            builds.append(body)
            reply = _build_reply(body)
            reply["exilus"] = None
            reply["autoLoadout"] = {"arcane": {"chosen": []}}
            reply["build"]["mods"][0]["displayEffects"] = []
            reply["build"].update(
                includesRiven=True, objectiveScore=100, riven={**body["riven"], "slot": 1}
            )
            return reply
        assert endpoint == "/api/simulate"
        replays.append(body)
        card = body.get("riven")
        drain = 0
        if card:
            slot = body["slotPolarities"][card["slot"]]
            drain = 9 if slot == card["polarity"] else 18
        assignments = [
            {**mod, "drain": 4, "rawDrain": 4, "modPolarity": "AP_ATTACK"} for mod in body["mods"]
        ]
        return {
            "engineFingerprint": portable_riven.FINGERPRINT,
            "buildLabel": damage_engine.PORTABLE_DATA,
            "damageModel": "target",
            "selection": {"weapon": weapon, "fireMode": body["fireMode"]},
            "enemyEnhancements": body["enemyEnhancements"],
            "benchmarkTimeline": {
                "windowDps": (20 if card else 0) + 80 * len(assignments),
                "windowSeconds": body["windowSeconds"],
                "context": {
                    "enemyPath": body["enemy"],
                    "level": body["level"],
                    "steelPath": body["steelPath"],
                },
            },
            "loadoutValidation": {
                "valid": True,
                "capacityLimit": 60,
                "capacity": drain + 4 * len(assignments),
                "assignments": assignments,
            },
        }

    async def query(command, *, image=None):
        return await host._h_loadout_recommend(parse(command), image, "pc")

    def card_columns(reply, name):
        (row,) = [line for line in reply.lines if f"　{name}　" in line]
        return row.split("　")[2:5]

    with TemporaryDirectory() as root:
        host.damage_engine = damage_engine.EngineService(Path(root))
        host.damage_engine.ensure_portable = AsyncMock()
        with (
            patch.object(damage_engine, "portable_request", engine),
            patch.object(host, "_confirmed_riven_image", return_value=(fields, observed)),
        ):
            manual = "配卡推荐 伯斯顿p 主射 暴率178.2 多重106.9 等级8"
            unknown = await query(manual)
            assert card_columns(unknown, "你的紫卡") == ["8", "9", "紫卡极性"]
            assert any("容量 13/60 · 极化 1 次" in line for line in unknown.lines)
            assert len(analyses) == 1 and len(replays) == 3
            assert "riven" not in replays[-1]
            assert (await query(manual)).lines == unknown.lines
            assert len(analyses) == 1 and len(replays) == 3
            named = await query(manual + " 极性=naramon")
            assert card_columns(named, "你的紫卡") == ["8", "9", "⟦pol:naramon⟧Naramon"]
            assert any("容量 13/60 · 极化 0 次" in line for line in named.lines)
            assert len(analyses) == 2 and analyses[0] == analyses[1]
            assert len(replays) == 6 and replays[:3] == replays[3:]
            explicit = await query(manual + " 极性=madurai")
            assert card_columns(explicit, "你的紫卡") == ["8", "9", "⟦pol:madurai⟧Madurai"]

            with i18n.use_lang("en"):
                command = "loadoutrecommend Burston Prime"
                unknown = await query(command, image=event)
                assert card_columns(unknown, "Your Riven") == ["8", "9", "Riven polarity"]
                assert any("Capacity 13/60 · Forma ×1" in line for line in unknown.lines)
                assert len(builds) == 1 and builds[0]["fireMode"] == 1
                assert builds[0]["scenario"]["incarnonCycle"] is True
                assert "Showing Incarnon cycle by default" in unknown.lines[-1]
                observed["polarity"] = "naramon"
                named = await query(command, image=event)
                assert card_columns(named, "Your Riven") == ["8", "9", "⟦pol:naramon⟧Naramon"]
                assert any("Capacity 13/60 · Forma ×0" in line for line in named.lines)
                assert len(builds) == 2 and builds[0] == builds[1]
        await host.damage_engine.close()


async def test_full_rank_riven_build_validation_and_cache():
    host = Plugin()
    with TemporaryDirectory() as root:
        host.damage_engine = damage_engine.EngineService(Path(root))
        host.damage_engine.ensure_portable = AsyncMock()
        path = portable_riven.MODES["burston_prime", "base"][0]
        scope = path, 1, 0.0
        (weapon,) = riven_rating.match_weapon("Burston Prime")
        positive = [("crit_chance", 180.6), ("corrosive_damage", 111.1), ("multishot", 105.9)]
        negative = [("magazine_capacity", 49.1)]
        card = riven_rating.typed_card(weapon, positive, negative, 8, "vazarin")
        catalog = {
            "rivenStats": {
                code: {
                    "baseValue": value,
                    "canPositive": True,
                    "canNegative": True,
                    "isFusion": code in ("CO", "VI"),
                }
                for code, value in (("0", 150), ("CO", 90), ("VI", 90), ("S", 90), ("L", 50))
            }
        }
        calls = []

        async def engine(_port, endpoint, body, **kwargs):
            assert endpoint == "/api/build" and kwargs["timeout"] == 600
            calls.append(body)
            reply = _build_reply(body)
            fixed = body["exilus"]
            reply["exilus"] = fixed if fixed != {"mode": "auto"} else None
            if body["autoArcane"]:
                reply["autoLoadout"] = {"arcane": {"chosen": []}}
            reply["build"].update(
                includesRiven=True, objectiveScore=174, riven={**body["riven"], "slot": 1}
            )
            return reply

        options = {"exilus": {"mode": "auto"}, "scenario": {"incarnonCycle": True}}
        plan = {
            "fits": True,
            "limit": 60,
            "drain": 13,
            "slots": {"1": "AP_DEFENSE"},
            "total": 1,
            "placement": [{"origin": 0, "drain": 4}, {"origin": 1, "drain": 9}] + [None] * 6,
        }

        async def build(card_to_solve=card, *, source=None, **overrides):
            return await portable_riven.build_riven(
                host,
                None,
                8765,
                scope,
                card_to_solve,
                "vazarin",
                catalog,
                source_disposition=source,
                options={**options, **overrides},
                on_solve=AsyncMock(),
            )

        try:
            with (
                patch.object(damage_engine, "portable_request", engine),
                patch.object(portable_riven, "_riven_slot_plan", return_value=plan),
            ):
                assert (await build())["scoringValues"]["actual"] == 174
                await build()
                assert len(calls) == 1
                request = calls[0]
                assert request["autoArcane"] and request["autoIncarnon"]
                assert request["riven"]["polarity"] == "AP_DEFENSE"
                assert [row["code"] for row in request["riven"]["stats"]] == ["0", "CO", "S", "L"]
                assert request["scenario"]["combatState"]["gaugeChargingHeadshots"] is True
                assert "benchmarkTarget" not in request and "stats" not in request

                assert (await build(source=1.21))["cardSourceDisposition"] == 1.21
                assert calls[-1]["riven"]["stats"][0]["value"] == 180.6 * (1.2 / 1.21)
                updated = riven_rating.typed_card(
                    weapon, [("crit_chance", 180.5), *positive[1:]], negative, 8, "vazarin"
                )
                await build(updated)
                await build(arcanes=[])
                assert calls[-1]["arcanes"] == [] and calls[-1]["autoArcane"] is False
                await build(exilus={"path": "/Exilus", "rank": 2})
                assert calls[-1]["exilus"] == {"path": "/Exilus", "rank": 2}
                await build(scenario={"incarnonCycle": True, "distance": 12})
                await build(
                    benchmarkTarget=portable_riven.bundled_reference_target(variant="shield")
                )
                assert len(calls) == 7

                for changed in (
                    {"rank": 7},
                    {"bonuses": [{"id": "critical_chance", "value": 9999}, *card["bonuses"][1:]]},
                    {"bonuses": [*card["bonuses"][:2], {"id": "viral", "value": 111.1}]},
                ):
                    assert await build({**card, **changed}) is None
                assert len(calls) == 7
        finally:
            await host.damage_engine.close()


async def test_portable_melee_card_capacity_and_replay_fallback():
    (weapon,) = riven_rating.match_weapon("Okina Prime")
    source = damage_calc.find_weapon_exact("Okina Prime")["uniqueName"]
    mode = (source, 0, 0.0)
    labels = {
        "攻击速度": "J",
        "近战伤害": "K",
        "范围": "T",
        "处决伤害": "X",
        "初始连击": "B",
        "连击持续时间": "N",
        "额外连击数几率": "O",
        "连击数获取几率": "Q",
        "滑暴": "E",
        "重击效率": "U",
        "重击蓄力速度": "HC",
        "重击伤害": "HD",
        "震地伤害": "SL",
        "格挡角度": "PA",
        "电击伤害": "7",
        "病毒伤害": "VI",
    }
    reference_rows = []
    for label, code in labels.items():
        sid = _STAT_LOOKUP[label]
        negative = code == "Q"
        bonuses = (
            [("crit_chance", 90), ("crit_damage", 75)]
            if negative
            else [("crit_chance", 90), (sid, 8.2)]
        )
        card = riven_rating.typed_card(
            weapon, bonuses, [(sid, 8.2)] if negative else [], 8, "naramon"
        )
        request = portable_riven.analysis_request(mode, card, "naramon")
        assert request["stats"][-1] == {"code": code, "value": 8.2, "negative": negative}
        reference_rows.append(
            {
                **request["stats"][-1],
                "unit": "flat" if code in {"B", "N", "T", "PA"} else "percent",
                "score": 110,
                "fusion": code in {"HC", "HD", "SL", "PA", "VI"},
            }
        )

    card = riven_rating.typed_card(
        weapon, [("damage", 140), ("attack_speed", 60)], [("combo_gain_chance", 50)], 8, "naramon"
    )
    request = portable_riven.analysis_request(mode, card, "naramon")
    assert [row["code"] for row in request["stats"]] == ["K", "J", "Q"]
    (rifle,) = riven_rating.match_weapon("Braton Prime")
    gun_card = riven_rating.typed_card(
        rifle, [("melee_damage", 140), ("fire_rate", 60)], [], 8, "naramon"
    )
    gun_mode = portable_riven.MODES["braton_prime", "base"]
    assert [
        row["code"]
        for row in portable_riven.analysis_request(gun_mode, gun_card, "naramon")["stats"]
    ] == ["D", "R"]

    stance = {
        "path": "/Stance",
        "nameZh": "旋風切割",
        "nameEn": "Spinning Needle",
        "capacityGain": 10,
    }
    actual = {
        "riven": {**request["rivenCard"], "drain": 18, "stats": request["stats"], "slot": 7},
        "mods": [
            {"slot": i, "path": f"/mod{i}", "nameEn": f"Mod {i}", "rank": 1} for i in range(7)
        ],
        "forma": {
            "drain": 65,
            "limit": 70,
            "fits": True,
            "total": 0,
            "slots": {},
            "placement": [{"origin": i, "drain": 8} for i in range(7)]
            + [{"origin": 7, "drain": 9}],
        },
    }
    result = {
        **_portable_score(mode, 500),
        "contextHash": "okina",
        "disposition": request["disposition"],
        "actual": actual,
        "stance": stance,
        "exilus": {
            "path": "/Exilus",
            "nameZh": "近战扩展",
            "nameEn": "Melee Exilus",
            "rank": 3,
            "drain": 4,
        },
        "arcaneStates": [
            {
                "path": "/Arcane",
                "nameZh": "近战激励",
                "nameEn": "Melee Influence",
                "rank": 5,
                "applied": True,
            },
            {"path": "/Skipped", "nameEn": "Skipped Arcane", "rank": 5, "applied": False},
        ],
    }
    portable_riven.check_result(result, request)
    explicit_capacity = {
        **result,
        "actual": {**actual, "capacityLimit": 70},
        "stance": {"path": "/Stance"},
    }
    portable_riven.check_result(explicit_capacity, request)
    for invalid in (
        {**explicit_capacity, "stance": None},
        {**result, "stance": {**stance, "capacityGain": 0}},
        {**result, "actual": {**actual, "forma": {**actual["forma"], "drain": 71}}},
    ):
        try:
            portable_riven.check_result(invalid, request)
        except ValueError:
            pass
        else:
            raise AssertionError("Invalid melee capacity passed validation")

    key = (source, 0, result["contextHash"])
    with patch.object(
        portable_riven,
        "_comparison_catalog",
        return_value={key: {"fixedTraitReference": {"baselineScore": 100, "rows": reference_rows}}},
    ):
        _, traits = portable_riven.prepared_traits(result)
    assert len(traits["positive"]) == len(labels) - 1
    assert traits["positive"][1][:3] == ("melee_damage", 8.2, "percent")
    assert traits["negative"][0][:3] == ("chance_to_gain_combo_count", -8.2, "percent")
    heavy = next(row for row in traits["positive"] if row[0] == "melee_damage_on_heavy_attack")
    assert heavy[3] & riven_rating._TRAIT_SPLICED

    host = Plugin()
    host.damage_engine = damage_engine.EngineService(Path(__file__).parent)
    try:
        with (
            patch.object(host.damage_engine, "ensure_portable", new=AsyncMock()),
            patch.object(
                damage_engine, "portable_catalog", return_value=({source: result["weapon"]}, {})
            ),
            patch.object(
                damage_engine, "portable_request", side_effect=damage_engine.EngineError("timeout")
            ) as replay,
            i18n.use_lang("en"),
        ):
            lines = "\n".join(await portable_riven.build_view(host, 8765, result, with_riven=True))
            replay.assert_awaited_once()
            assert replay.await_args.args[2]["arcanes"] == [{"path": "/Arcane", "rank": 5}]
            empty = "\n".join(
                await portable_riven.build_view(
                    host, 8765, {**result, "arcanes": []}, with_riven=True
                )
            )
            assert replay.await_args.args[2]["arcanes"] == []
            assert "Melee Influence" not in empty and "Skipped Arcane" not in empty
            selected = [{"path": "/Selected Arcane", "rank": 2}]
            override = "\n".join(
                await portable_riven.build_view(
                    host, 8765, result, with_riven=True, options={"arcanes": selected}
                )
            )
            assert replay.await_args.args[2]["arcanes"] == selected
            assert "Selected Arcane (rank 2)" in override and "Melee Influence" not in override
        assert replay.await_args.args[1] == "/api/simulate"
        assert replay.await_args.args[2]["stancePath"] == stance["path"]
        assert "Capacity 65/70" in lines and "DPS unavailable" in lines
        assert (
            "Melee Exilus" in lines and "Melee Influence" in lines and "Skipped Arcane" not in lines
        )
        assert "Spinning Needle (capacity +10)" in lines
    finally:
        await host.damage_engine.close()


async def test_ballistica_dynamic_loadout():
    host = Plugin()
    host.cfg["damage_engine_portable_port"] = 8765
    host.damage_engine = SimpleNamespace(ensure_portable=AsyncMock())
    event = SimpleNamespace(message_obj=SimpleNamespace(message=[type("Image", (), {})()]))
    names = ("Ballistica", "Ballistica Prime", "Rakta Ballistica", "Grimoire")
    choices = tuple(riven_rating.match_weapon(name)[0] for name in names)
    paths = {name: damage_calc.find_weapon_exact(name)["uniqueName"] for name in names}
    paths["Grimoire"] = "/Catalog/Grimoire"
    catalog = {
        path: {"path": path, "nameEn": name, "fireModes": [{"index": 0}]}
        for name, path in paths.items()
    }
    catalog["/SameName"] = {
        "path": "/SameName",
        "nameEn": "Ballistica",
        "fireModes": [{"index": 0}],
    }
    prime = catalog[paths["Ballistica Prime"]]
    prime["fireModes"].extend(
        [
            {"index": 1, "available": False},
            {"index": 2, "name": "Charged shot", "isAlternateFire": True},
        ]
    )
    observed = {"drain": 9, "drain_color": "绿", "mastery_rank": 13, "polarity": "naramon"}
    fields = SimpleNamespace(
        weapon="Ballistica", positive=[("crit_chance", 158.4), ("multishot", 126.4)], negative=[]
    )

    different_target = {}

    async def solve(_host, _event, _port, scope, card, polarity, **kwargs):
        name = catalog[scope[0]]["nameEn"]
        assert card["rank"] == 7 and polarity == "naramon"
        assert scope[0] == paths[name]
        result = _portable_score(scope, 1000 if scope[1] else names.index(name) + 1)
        result["weapon"] = catalog[scope[0]]
        if name == "Rakta Ballistica":
            result["evaluationTarget"].update(different_target)
        return result

    async def query(command):
        return await host._h_loadout_recommend(parse(command), event, "pc")

    with (
        patch.object(
            damage_engine, "portable_catalog", new=AsyncMock(return_value=(catalog, {}))
        ) as fetch,
        patch.object(host, "_confirmed_riven_image", return_value=(fields, observed)),
        patch.object(portable_riven, "loadout_references", return_value=None),
        patch.object(portable_riven, "analyze", side_effect=solve) as search,
        patch.object(
            portable_riven,
            "build_view",
            side_effect=lambda _, __, result, **kw: [portable_riven.scenario_line(result)],
        ),
    ):
        found = await portable_riven.discover_weapons(host, 8765, choices)
        assert [found[row["engine_id"]]["path"] for row in choices] == [paths[n] for n in names]
        assert portable_riven.weapon_modes("grimoire", found["grimoire"], "base") == [
            ("base", (paths["Grimoire"], 0, 0.0))
        ]
        best = await query("配卡推荐")
        assert best.title == "配卡推荐 · 绯红·布里斯提卡"
        comparison = next(line for line in best.lines if "已比较 3 个变体" in line)
        assert all(name in comparison for name in (*names[:3], *(r["zh"] for r in choices[:3])))
        assert any("多组工况" in line for line in best.lines)
        assert search.await_count == 4
        search.reset_mock()

        different_target["headshotChance"] = 0.5
        best = await query("配卡推荐 主射")
        assert best.title == "配卡推荐 · 布里斯提卡 Prime"
        assert any("已比较 2 个变体" in line and "绯红" not in line for line in best.lines)
        assert any("多组工况" in line for line in best.lines)
        assert search.await_count == 3
        search.reset_mock()

        best = await query("配卡推荐 副射")
        assert best.title == "配卡推荐 · 布里斯提卡 Prime"
        assert "次要射击" in best.lines[0] and "Charged shot" in best.lines[0]
        assert search.await_count == 1
        search.reset_mock()

        fields.positive = [("crit_chance", 165), ("multishot", 132)]
        ambiguous = await query("配卡推荐")
        assert any("7 / 8" in line for line in ambiguous.lines)
        observed["_single_card_pips"] = 6
        conflict = await query("配卡推荐")
        assert any("容量 9" in line and "不一致" in line for line in conflict.lines)
        search.assert_not_awaited()

        observed.pop("_single_card_pips")
        fields.positive = [("crit_chance", 158.4), ("multishot", 126.4)]
        search.side_effect = portable_riven.CoverageUnavailable("weapon analysis is unavailable")
        fallback = await query("配卡推荐 布里斯提卡")
        assert any("暂无带紫卡" in line for line in fallback.lines)
        search.assert_awaited_once()
        search.reset_mock()

        fetch.side_effect = damage_engine.EngineError("catalog unavailable")
        fallback = await query("配卡推荐 布里斯提卡")
        assert any("catalog unavailable" in line for line in fallback.lines)
        search.assert_not_awaited()
        assert portable_riven.weapon_modes("braton_prime", None, "base") == [
            ("base", portable_riven.MODES["braton_prime", "base"])
        ]

    result = _portable_score((paths["Ballistica"], 0, 0.0), 50)
    context = portable_riven.comparison_context(result, None, "base")
    for key, value in (
        ("enemyPath", "/Other"),
        ("level", 160),
        ("steelPath", False),
        ("headshotChance", 0.2),
        ("windowSeconds", 40),
        ("hitLocation", "head"),
    ):
        different = {**result, "evaluationTarget": {**result["evaluationTarget"], key: value}}
        assert portable_riven.comparison_context(different, None, "base") != context
    assert (
        portable_riven.comparison_context({**result, "scoreMetric": "other"}, None, "base")
        != context
    )


def test_rank_inference():
    burston = riven_rating.rank_context(riven_rating.match_weapon("Burston Prime")[0])
    result = riven_analysis.infer_riven_rank(
        [("crit_chance", 178.2), ("multishot", 106.9)],
        drain=18,
        mastery_rank=13,
        **burston,
    )
    assert (result.status, result.rank, result.basis) == (
        "resolved",
        8,
        ("drain", "unified_disposition"),
    )
    ballistica = riven_rating.rank_context(riven_rating.match_weapon("Ballistica")[0])
    slot = {"drain": 9, "drain_color": "green", **ballistica}
    ambiguous = riven_analysis.infer_riven_rank(**slot)
    assert ambiguous.status == "ambiguous" and ambiguous.candidates == ((7, None), (8, None))
    assert "槽" in ambiguous.message[0] or "颜色" in ambiguous.message[0]
    assert "7 / 8" in ambiguous.message[1]
    assert (
        riven_analysis.infer_riven_rank([("crit_chance", 158.4), ("multishot", 126.4)], **slot).rank
        == 7
    )
    assert (
        riven_analysis.infer_riven_rank([("crit_chance", 178.2), ("multishot", 142.2)], **slot).rank
        == 8
    )
    history = riven_analysis.infer_riven_rank(
        [("crit_chance", 202), ("multishot", 159)],
        drain=18,
        **ballistica,
    )
    assert history.candidates == ((8, 1.25),) and "historical_disposition" in history.basis
    grid = riven_analysis.infer_riven_rank(
        [("crit_chance", 230.2), ("multishot", 183.7)],
        **slot,
    )
    assert grid.rank == 8 and "disposition_grid" in grid.basis and (8, 1.55) in grid.candidates
    assert (
        riven_analysis.infer_riven_rank(
            [("crit_chance", 230.2), ("multishot", 183.7)], pips_count=0, **ballistica
        ).status
        == "conflict"
    )
    assert riven_analysis.infer_riven_rank(observed_rank=7, pips_count=8).status == "conflict"
    assert riven_analysis.infer_riven_rank(drain=13, drain_color="red").rank == 0
    assert riven_analysis.infer_riven_rank(drain=22, drain_color="red").rank == 7
    assert riven_analysis.infer_riven_rank(drain=23, drain_color="red").rank == 8
    assert riven_analysis.infer_riven_rank(mastery_rank=13).status == "ambiguous"


def test_unspecified_riven_slot_halves_drain():
    for rank, half in ((7, 9), (8, 9)):
        raw = 10 + rank
        actual = {
            "riven": {"rank": rank, "drain": raw, "slot": 0, "polarity": "AP_TACTIC"},
            "mods": [],
            "forma": {
                "drain": raw,
                "limit": 60,
                "fits": True,
                "total": 0,
                "regular": 0,
                "slots": {},
                "placement": [{"origin": 0, "drain": raw, "polarity": None}, *[None] * 7],
            },
        }
        result = portable_riven._fit_unspecified_riven_polarity({"actual": actual})
        plan, _, placement, _ = portable_riven._placement(result["actual"])
        assert result["unspecifiedRivenPolarity"] is True
        assert plan["drain"] == half and plan["total"] == plan["regular"] == 1
        assert plan["slots"] == {"0": "AP_TACTIC"}
        assert placement[0] == {"origin": 0, "drain": half, "polarity": "AP_TACTIC"}
        assert actual["forma"]["drain"] == raw


async def test_portable_search_preserves_card_rank():
    mode = portable_riven.MODES["braton_prime", "base"]
    host = Plugin()
    host.damage_engine = damage_engine.EngineService(Path(__file__).parent)
    card = {
        "bonuses": [{"id": "critical_chance", "value": 21.4}, {"id": "multishot", "value": 12.9}],
        "malus": None,
    }
    requests = []

    async def engine(port, path, body, **kwargs):
        assert port == 8765 and path == "/api/analyze"
        requests.append(body)
        rank = body["rivenCard"]["rank"]
        raw = 10 + rank
        cost = (raw + 1) // 2
        return {
            **_portable_score(mode, 500),
            "disposition": body["disposition"],
            "actualGain": 0,
            "actual": {
                "riven": {
                    **body["rivenCard"],
                    "drain": raw,
                    "stats": deepcopy(body["stats"]),
                    "slot": 1,
                },
                "mods": [{"slot": 0, "path": "/mod", "nameEn": "Mod", "rank": 1}],
                "forma": {
                    "drain": 4 + cost,
                    "limit": 60,
                    "fits": True,
                    "total": 1,
                    "slots": {"1": "AP_TACTIC"},
                    "placement": [{"origin": 0, "drain": 4}, {"origin": 1, "drain": cost}]
                    + [None] * 6,
                },
            },
        }

    try:
        with (
            patch.object(host.damage_engine, "ensure_portable", new=AsyncMock()),
            patch.object(damage_engine, "portable_request", side_effect=engine),
            patch.object(
                damage_engine, "portable_catalog", side_effect=damage_engine.EngineError()
            ),
        ):
            for rank in (0, 7, 8):
                actual = {**card, "rank": rank}
                result = await portable_riven.analyze(host, None, 8765, mode, actual, "naramon")
                request = requests[-1]
                assert "sourceDisposition" not in request
                assert request["rivenCard"] == {"rank": rank, "polarity": "AP_TACTIC"}
                assert result["actual"]["riven"]["drain"] == 10 + rank
                assert [row["value"] for row in result["actual"]["riven"]["stats"]] == [21.4, 12.9]
                lines = await portable_riven.build_view(host, 8765, result, with_riven=True)
                assert any("原卡倾向待核对" in line for line in lines)
                assert any(f"你的紫卡　{rank}　{(11 + rank) // 2}" in line for line in lines)
                for field, value in (
                    ("rank", 2),
                    ("polarity", "AP_ATTACK"),
                    ("drain", 9),
                    ("stats", []),
                ):
                    invalid = deepcopy(result)
                    invalid["actual"]["riven"][field] = value
                    try:
                        portable_riven.check_result(invalid, request)
                    except damage_engine.EngineError:
                        pass
                    else:
                        raise AssertionError(f"Unchecked Riven field: {field}")
                invalid = deepcopy(result)
                invalid["actual"]["forma"]["placement"][1]["drain"] = 59
                invalid["actual"]["forma"]["drain"] = 63
                try:
                    portable_riven.check_result(invalid, request)
                except ValueError:
                    pass
                else:
                    raise AssertionError("Unchecked capacity")
        with (
            patch.object(host.damage_engine, "ensure_portable", new=AsyncMock()),
            patch.object(
                damage_engine,
                "portable_request",
                side_effect=damage_engine.EngineBuildError("Riven requires rank 8"),
            ),
        ):
            for language, expected in (("zh", "请更新引擎"), ("en", "Update it to search")):
                with i18n.use_lang(language):
                    try:
                        await portable_riven.analyze(
                            host, None, 8765, mode, {**card, "rank": 1}, "naramon"
                        )
                    except damage_engine.EngineBuildError as exc:
                        assert expected in str(exc)
                    else:
                        raise AssertionError("Missing upgrade hint")
    finally:
        await host.damage_engine.close()


def test_portable_card_language_and_removed_impact():
    scenario = {
        "fireMode": 1,
        "weapon": {"fireModes": [{"index": 1, "isIncarnonForm": True}]},
        "evaluationTarget": {"enemyName": "Thrax Centurion", "level": 170, "headshotChance": 0},
    }
    assert "凶魂百夫长" in portable_riven.scenario_line(scenario)
    assert formatters.dps_removal_columns(542387.19, 28128.14) == ("28,128", "⟦m⟧-94.8%⟦/m⟧")
    assert formatters.dps_removal_columns(100, 110) == ("110", "⟦y⟧+10.0%⟦/y⟧")
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


def test_portable_neutral_melee_scenario():
    scenario = {
        "fireMode": 0,
        "evaluationTarget": {"enemyName": "Thrax Centurion", "level": 170, "headshotChance": 0},
    }
    melee = {"category": "Melee", "fireModes": [{"index": 0}]}
    named = {**melee, "fireModes": [{"index": 0, "nameZh": "重击", "nameEn": "Heavy attack"}]}
    incarnon = {**melee, "fireModes": [{"index": 0, "isIncarnonForm": True}]}
    for language, neutral, primary, heavy, form in (
        ("zh", "平砍", "主射", "重击", "灵化形态"),
        ("en", "Base", "Primary fire", "Heavy attack", "Incarnon form"),
    ):
        with i18n.use_lang(language):
            assert portable_riven.scenario_line({**scenario, "weapon": melee}).startswith(
                f"◆ {neutral} ·"
            )
            assert portable_riven.scenario_line(
                {**scenario, "stance": {"path": "/Stance"}}
            ).startswith(f"◆ {neutral} ·")
            assert portable_riven.scenario_line(scenario).startswith(f"◆ {primary} ·")
            assert heavy in portable_riven.scenario_line({**scenario, "weapon": named})
            assert portable_riven.scenario_line({**scenario, "weapon": incarnon}).startswith(
                f"◆ {form} ·"
            )
            for route, is_heavy, label in (
                ("combo", False, neutral),
                ("combo", True, heavy),
                ("glaive", False, i18n.L("重击投掷", "Glaive throw")),
                ("slam", True, i18n.L("重击震地", "Heavy slam")),
                ("slamBullet", False, i18n.L("重击震地", "Heavy slam")),
                ("slamContinuous", False, i18n.L("重击震地", "Heavy slam")),
            ):
                result = {
                    **scenario,
                    "weapon": melee,
                    "evaluationScenario": {
                        "heavyAttack": is_heavy,
                        "combatState": {"meleeRoute": route},
                    },
                }
                assert portable_riven.scenario_line(result).startswith(f"◆ {label} ·")


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
            in portable_riven.evolution_lines(torid, {"tier3": "3-1"})[1]
        )
    with i18n.use_lang("en"):
        assert portable_riven.enemy_label(butcher, "Butcher") == "Butcher"
        assert portable_riven.enemy_label(ambulas, "Ambulas") == "Ambulas"
        assert portable_riven.evolution_lines(torid, {"tier3": "3-1"})[1] == (
            "· 3-1 Swift Deliverance · 50% Projectile Speed"
        )
        fallback = portable_riven.evolution_lines(
            {**torid, "path": "/unknown", "incarnon": []}, {"tier3": "3-1"}
        )
        assert fallback[1] == "· 3-1 Swift Deliverance · +50% Projectile Speed"


def test_portable_optional_equipment_and_context():
    spec = {"level": 100, "steel_path": False, "headshot": False, "fire_seconds": 12}
    options = {
        "arcanes": [{"path": "/Arcane", "rank": 2, "stacks": 0}],
        "exilus": {"path": "/Exilus", "rank": 3},
        "scenario": {"comboTier": 0, "distance": 10.0},
    }
    original = damage_engine.portable_damage_request("/weapon", "/enemy", [], spec)
    assert not options.keys() & original.keys()
    configured = damage_engine.portable_damage_request(
        "/weapon", "/enemy", [], spec, fire_mode=1, options=options
    )
    assert configured == {**original, "fireMode": 1, **options}
    assert damage_engine.portable_replay_fingerprint(configured) != (
        damage_engine.portable_replay_fingerprint({**configured, "arcanes": []})
    )
    for exilus in (options["exilus"], {"mode": "auto"}):
        assert damage_engine.with_portable_analysis_options({}, {"exilus": exilus}) == {
            "exilus": exilus
        }
    for merger, extra in (
        (damage_engine.with_portable_analysis_options, {"exilus": {"mode": "auto"}}),
        (damage_engine.with_portable_replay_options, {"mods": [{"path": "/replacement"}]}),
        (damage_engine.with_portable_replay_options, {"scenario": {"distance": 50.0}}),
    ):
        try:
            merger(configured, extra)
        except damage_engine.EngineError:
            continue
        raise AssertionError("可选参数越界或覆盖已确认工况")

    def build(extra=None, **fields):
        return damage_engine.portable_damage_request(
            "/weapon", "/enemy", [], {**spec, **fields}, options=extra
        )

    near = build({"scenario": {"distance": 5, "modelSpread": True}})
    far = build({"scenario": {"distance": 65, "modelSpread": True}})
    no_spread = build({"scenario": {"distance": 65, "modelSpread": False}})
    assert len({damage_engine.portable_replay_fingerprint(q) for q in (near, far, no_spread)}) == 3
    assert build(combat_options={"distance": 5, "modelSpread": True}) == near
    for key, values in (
        ("distance", (-1, True, "4m", float("inf"), float("nan"))),
        ("nearestEnemyDistance", (-1, "3", None)),
        ("comboTier", (-1, 12, 1.5, True)),
        ("heavyAttack", (1, "false")),
        ("modelSpread", ("true", 1, None)),
    ):
        for invalid in values:
            try:
                build({"scenario": {key: invalid}})
            except damage_engine.EngineError:
                pass
            else:
                raise AssertionError((key, invalid))
    for invalid in (
        {"armourStrip": 20},
        {"elementalArmourStrip": -0.3},
        {"armourStrip": True},
        {"innateElementValue": 60},
        {"innateElementValue": 0},
        {"innateElementValue": float("inf")},
        {"exilus": {"mode": "auto"}},
    ):
        try:
            build(invalid)
        except damage_engine.EngineError:
            pass
        else:
            raise AssertionError(invalid)
    for field, shown in (("base_armor", "基准护甲"), ("overguard", "超宏防护")):
        try:
            build(**{field: 0})
        except damage_engine.EngineError as exc:
            assert shown in str(exc)
        else:
            raise AssertionError(field)


def test_offline_riven_comparison_context():
    path = Path(portable_riven.__file__).parent / "data" / "riven_reroll_comparisons.json"
    legacy = json.loads(path.read_bytes())
    assert legacy["engineFingerprint"] != portable_riven.FINGERPRINT
    portable_riven._comparison_catalog.cache_clear()
    assert not portable_riven._comparison_catalog()
    try:
        with patch.object(portable_riven, "FINGERPRINT", legacy["engineFingerprint"]):
            portable_riven._comparison_catalog.cache_clear()
            _check_offline_riven_comparison_context()
    finally:
        portable_riven._comparison_catalog.cache_clear()


def _check_offline_riven_comparison_context():
    catalog = portable_riven._comparison_catalog()
    assert len(catalog) == len(portable_riven.BUNDLED_BUILD_MODES) == 7
    assert sum("fixedTraitReference" in item for item in catalog.values()) == 6
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
    assert "无紫卡 54,806 / 当前紫卡 45,407 / 参考紫卡 135,831 DPS" in lines[1]
    assert "模拟满值" in lines[2]
    assert not portable_riven.comparison_lines({**result, "contextHash": "another-target"})
    assert not portable_riven.comparison_lines({**result, "engineFingerprint": "old-engine"})
    assert not portable_riven.comparison_lines({**result, "searchWork": {"limited": 1}})
    view = result
    prepared = portable_riven.prepared_traits(view)
    assert prepared is not None
    assert len(prepared[1]["positive"]) + len(prepared[1]["negative"]) == 55
    request = portable_riven.prepared_replay_request(
        view, {"arcanes": [], "scenario": {"distance": 17, "modelSpread": True}}
    )
    assert request["weapon"] == weapon and request["fireMode"] == fire_mode
    assert request["mods"] and len(request["slotPolarities"]) == 8
    assert portable_riven.prepared_traits(view, replay_request=request) is None
    entry = catalog[weapon, fire_mode, context]
    base = {**request, "scenario": {"distance": 8, "aiming": True}}
    with patch.object(
        portable_riven,
        "_comparison_catalog",
        return_value={(weapon, fire_mode, context): {"noRivenBuild": {"request": base}}},
    ):
        for options in (
            {"weapon": weapon},
            {"fireMode": 1},
            {"enemy": "/Other"},
            {"level": 1},
            {"mods": []},
            {"riven": {}},
            {"slotPolarities": []},
            {"scenario": {"enemy": "/Other"}},
            {"scenario": {"modelSpread": "true"}},
            {"scenario": []},
        ):
            try:
                portable_riven.prepared_replay_request(view, options)
            except damage_engine.EngineError:
                pass
            else:
                raise AssertionError(f"Unvalidated prepared replay options: {options}")
        merged = portable_riven.prepared_replay_request(
            view, {"scenario": {"distance": 17, "modelSpread": True}}
        )
        assert merged["scenario"] == {"distance": 17, "aiming": True, "modelSpread": True}
        assert merged["mods"] == base["mods"] and base["scenario"] == {
            "distance": 8,
            "aiming": True,
        }
    fixed = {
        **entry["fixedTraitReference"],
        "replayContextHash": damage_engine.portable_replay_fingerprint(request),
    }
    with patch.object(
        portable_riven,
        "_comparison_catalog",
        return_value={(weapon, fire_mode, context): {**entry, "fixedTraitReference": fixed}},
    ):
        assert portable_riven.prepared_traits(view, replay_request=request) == prepared
        for changes in (
            {"exilus": {"path": "/Other", "rank": 0}},
            {"scenario": {"distance": 17, "modelSpread": False}},
            {"scenario": {"distance": 62, "modelSpread": True}},
            {"mods": request["mods"][:-1]},
        ):
            assert (
                portable_riven.prepared_traits(view, replay_request={**request, **changes}) is None
            )
    (entry,) = riven_rating.match_weapon("布莱顿 Prime")
    matrix = riven_rating.trait_matrix(entry, {"base": prepared})
    assert matrix[0] == "◆ 全词条对照"
    assert "词条　牌面中位值　DPS 变化　备注" in matrix
    assert not any("percent" in line or "flat" in line for line in matrix)
    assert any("穿透" in line and " 米" in line for line in matrix)
    assert any("⟦m⟧-" in line for line in matrix) and any("⟦y⟧+" in line for line in matrix)
    assert any("基础伤害" in line and "-148.5%" in line and "-54.5%" in line for line in matrix)
    assert not any("-101.7%" in line for line in matrix)
    assert portable_riven.prepared_traits({**result, "contextHash": "another-target"}) is None
    assert portable_riven.prepared_traits({**result, "engineFingerprint": "old-engine"}) is None

    key = (weapon, fire_mode, context)
    reference = catalog[key]["fixedTraitReference"]
    (negative_damage,) = [
        row for row in reference["rows"] if row["code"] == "D" and row["negative"]
    ]
    for score, expected in (
        (0.0, -100.0),
        (reference["baselineScore"] * 0.5, -50.0),
        (-1.0, None),
        (float("inf"), None),
        (float("nan"), None),
    ):
        replacement = {
            **catalog[key],
            "fixedTraitReference": {**reference, "rows": [{**negative_damage, "score": score}]},
        }
        with patch.object(portable_riven, "_comparison_catalog", return_value={key: replacement}):
            (_label, rows) = portable_riven.prepared_traits(result)
        (values,) = rows["negative"]
        trait = riven_rating._TraitResult(*values)
        assert trait.pct == expected
        assert bool(trait.flags & riven_rating._TRAIT_REPLAY_REQUIRED) is (expected is None)
        matrix = riven_rating.trait_matrix(entry, {"base": (_label, rows)})
        assert any(
            (f"{expected:+.1f}%" if expected is not None else "待复算") in line for line in matrix
        )
    for baseline in (-1, 0, float("nan"), float("inf")):
        row = {**catalog[key], "fixedTraitReference": {**reference, "baselineScore": baseline}}
        with patch.object(portable_riven, "_comparison_catalog", return_value={key: row}):
            assert portable_riven.prepared_traits(result) is None


def test_trait_matrix_contextual_stats_show_dashes_for_both_signs():
    cases = (
        ("recoil", "看手感", "Handling"),
        ("weapon_recoil", "看手感", "Handling"),
        ("projectile_speed", "看弹道/衰减", "Ballistics"),
        ("ammo_max", "看续航", "Reserve"),
        ("ammo_maximum", "看续航", "Reserve"),
    )
    for lang, low_ammo in (("zh", "影响续航"), ("en", "Low ammo")):
        with i18n.use_lang(lang):
            for stat, zh, en in cases:
                note = i18n.L(zh, en)
                for change in (None, 0, -0.049, 0.049):
                    bonus = riven_rating._TraitResult(stat, 100, "%", 0, None, change, None, None)
                    malus = bonus._replace(shown=-100)
                    rows = {"positive": [bonus], "negative": [malus]}
                    scenes = {
                        "base": ("Primary", rows),
                        "low": ("Low reserve", {**rows, "low_ammo": True}),
                    }
                    matrix = riven_rating.trait_matrix({"category": "Rifle"}, scenes)
                    cells = [line.split("　")[2:] for line in matrix if line.startswith("· ")]
                    normal = f"⟦n⟧—⟦/n⟧ ⟦v⟧{note}⟦/v⟧"
                    scarce = f"⟦n⟧—⟦/n⟧ ⟦m⟧{low_ammo}⟦/m⟧" if en == "Reserve" else normal
                    assert cells == [[normal, normal], [normal, scarce]], (stat, change, cells)


def test_trait_matrix_keeps_distance_effects_and_other_zeroes():
    projectile = riven_rating._TraitResult("projectile_speed", 100, "%", 0, None, 4.2, None, None)
    faction = projectile._replace(stat="damage_to_grineer", pct=0)
    physical = projectile._replace(stat="impact_damage", pct=0)
    scene = {
        "positive": [projectile, faction, physical],
        "negative": [projectile._replace(shown=-100, pct=-0.05)],
    }
    with i18n.use_lang("zh"):
        matrix = riven_rating.trait_matrix(
            {"category": "Rifle"}, {"base": ("主射", scene)}, bundled_roll="3+1-"
        )
    assert [line.split("　")[2:] for line in matrix if line.startswith("· ")] == [
        ["⟦y⟧+4.2%⟦/y⟧", "⟦n⟧—⟦/n⟧"],
        ["⟦n⟧0.0%⟦/n⟧", "⟦n⟧按派系⟦/n⟧"],
        ["⟦n⟧0.0%⟦/n⟧", "⟦n⟧—⟦/n⟧"],
        ["⟦m⟧-0.1%⟦/m⟧", "⟦n⟧低影响⟦/n⟧"],
    ]


def test_prepared_trait_matrix_marks_incarnon_ammo():
    weapon = {
        "category": "Rifle",
        "path": "/MockWeapon",
        "fireModes": [
            {"index": 0, "isIncarnonForm": False, "nameEn": "Incarnon form"},
            {"index": 1, "isIncarnonForm": True, "nameEn": "Mode B"},
        ],
    }
    scores = [
        ("L", False, 112.5),
        ("F", False, 108),
        ("M", False, 100),
        ("L", True, 75),
        ("F", True, 95),
        ("M", True, None),
        ("AE", False, 117),
    ]
    fixed = [
        {"code": code, "negative": negative, "value": 40, "unit": "%", "fusion": code == "AE"}
        for code, negative, _ in scores
    ]
    catalog = {
        (weapon["path"], mode, context): {
            "fixedTraitReference": {
                "baselineScore": 100,
                "rows": [
                    {**row, "score": 100 if context == "form" and score is not None else score}
                    for row, (_, _, score) in zip(fixed, scores, strict=True)
                ],
            }
        }
        for mode, context in ((0, "base"), (1, "form"), (1, "cycle"))
    }
    cases = (
        ("zh", "灵化无效", "仅充能期", "高风险", "影响续航"),
        ("en", "Incarnon N/A", "Charge only", "High risk", "Low ammo"),
    )
    for lang, invalid, charge, risk, low_ammo in cases:
        with (
            i18n.use_lang(lang),
            patch.object(portable_riven, "_comparison_catalog", return_value=catalog),
        ):
            scenes = {
                context: portable_riven.prepared_traits(
                    {
                        "weapon": weapon,
                        "fireMode": mode,
                        "contextHash": context,
                        "scoreMetric": "windowedDps",
                        "engineFingerprint": portable_riven.FINGERPRINT,
                        "evaluationScenario": {"incarnonCycle": context == "cycle"},
                    }
                )
                for mode, context in ((0, "base"), (1, "form"), (1, "cycle"))
            }
            assert [
                (meta["incarnon_form"], meta["incarnon_cycle"]) for _, meta in scenes.values()
            ] == [(False, False), (True, False), (False, True)]
            scenes["form"][1]["low_ammo"] = True
            matrix = riven_rating.trait_matrix(weapon, scenes)
            cells = [line.split("　")[3:] for line in matrix if line.startswith("· ")]
            assert cells == [
                [f"⟦n⟧—⟦/n⟧ ⟦n⟧{invalid}⟦/n⟧", effect]
                for effect in (
                    f"⟦y⟧+12.5%⟦/y⟧ ⟦v⟧{charge}⟦/v⟧",
                    f"⟦y⟧+8.0%⟦/y⟧ ⟦v⟧{charge}⟦/v⟧",
                    f"⟦n⟧—⟦/n⟧ ⟦v⟧{charge}⟦/v⟧",
                    f"⟦m⟧-25.0%⟦/m⟧ ⟦m⟧{risk}⟦/m⟧",
                    f"⟦m⟧-5.0%⟦/m⟧ ⟦v⟧{charge}⟦/v⟧",
                    f"⟦n⟧—⟦/n⟧ ⟦v⟧{charge}⟦/v⟧",
                    f"⟦y⟧+17.0%⟦/y⟧ ⟦v⟧{charge}⟦/v⟧",
                )
            ], cells
            scenes["cycle"][1]["low_ammo"] = True
            matrix = riven_rating.trait_matrix(weapon, {"cycle": scenes["cycle"]})
            assert any(line.endswith(f"⟦n⟧—⟦/n⟧　⟦m⟧{low_ammo}⟦/m⟧") for line in matrix)


def test_bundled_trait_matrix_explains_weakpoint_scope():
    body_trait = riven_rating._TraitResult("critical_chance", 100, "%", 0, None, 10, None, None)
    weakpoint_trait = body_trait._replace(
        stat="weak_point_damage", flags=riven_rating._TRAIT_WEAKPOINT
    )
    scenes = {
        "base": ("主射", {"positive": [body_trait], "negative": []}),
        "incarnon": ("灵化形态", {"positive": [weakpoint_trait], "negative": []}),
    }
    for lang, scope, kpm in (
        (
            "zh",
            "※ 弱点边界：仅「弱点伤害/弱点暴击几率」按同套配装 100% 弱点命中单独试算，"
            "代表满弱点上限，不与躯干词条直比；不含弱点专属换卡/赋能，无弱点攻击（如范围爆炸）不生效。",
            "※ KPM 为每分钟击杀数。",
        ),
        (
            "en",
            "※ Weakpoint scope: only Weak Point Damage/Critical Chance use 100% weakpoint hits "
            "on the same build (upper bound; not directly comparable to body-hit traits); "
            "excludes weakpoint mod/arcane swaps, and hits without weakpoints (e.g., AoE blasts) gain 0%.",
            "※ KPM means kills per minute.",
        ),
    ):
        with i18n.use_lang(lang):
            lines = riven_rating.trait_matrix({"category": "Rifle"}, scenes, bundled_roll="3+1-")
        assert lines[1].startswith("※") and lines[2:4] == [scope, kpm], lines


def test_bundled_trait_summary_marks_only_weakpoint_fusion_options():
    fusion = riven_rating._TRAIT_SPLICED
    weakpoint = fusion | riven_rating._TRAIT_WEAKPOINT
    corrosive = riven_rating._TraitResult(
        "corrosive_damage", 100, "%", fusion, None, 39.8, None, None
    )
    weak_damage = corrosive._replace(stat="weak_point_damage", flags=weakpoint, pct=39.1)
    ammo = corrosive._replace(stat="ammo_efficiency", pct=19.8)
    magnetic = corrosive._replace(stat="magnetic_damage", pct=30.2)
    weak_crit = corrosive._replace(stat="weak_point_critical_chance", flags=weakpoint, pct=27.4)
    scene = {"baseline_dps": 100, "baseline_kpm": 10}
    cases = (
        (
            "zh",
            "融合选项",
            "⟦n⟧（100%弱点）⟦/n⟧",
            ("腐蚀", "弱点伤", "弹效", "磁力", "弱点暴"),
        ),
        (
            "en",
            "Fusion options",
            " ⟦n⟧(100% weakpoint)⟦/n⟧",
            (
                "Corrosive",
                "Weak Point Damage",
                "Ammo Efficiency",
                "Magnetic",
                "Weak Point Critical Chance",
            ),
        ),
    )
    with patch.object(portable_riven, "scenario_line", return_value="◆ 目标"):
        for lang, label, note, names in cases:
            corrosive_name, damage_name, ammo_name, magnetic_name, crit_name = names
            damage_option = f"{damage_name} ⟦v⟧+39.1%⟦/v⟧{note}"
            with i18n.use_lang(lang):
                for positive, expected in (
                    (
                        [ammo, weak_damage, corrosive],
                        (
                            f"{corrosive_name} ⟦v⟧+39.8%⟦/v⟧",
                            damage_option,
                            f"{ammo_name} ⟦v⟧+19.8%⟦/v⟧",
                        ),
                    ),
                    (
                        [weak_crit, magnetic, weak_damage],
                        (
                            damage_option,
                            f"{magnetic_name} ⟦v⟧+30.2%⟦/v⟧",
                            f"{crit_name} ⟦v⟧+27.4%⟦/v⟧{note}",
                        ),
                    ),
                ):
                    summary = portable_riven._bundled_trait_summary(
                        {}, scene, {"positive": positive, "negative": []}, "2+1-", melee=False
                    )
                    assert summary[-1] == f"· ⟦w⟧{label}⟦/w⟧  " + " / ".join(expected), summary


def test_trait_matrix_hides_negative_element_bonus_gains_and_shows_matching_sets():
    def trait(stat, pct, flags=0):
        return riven_rating._TraitResult(stat, 100, "percent", flags, None, pct, None, None)

    elements = ("heat", "cold", "toxin", "electricity")
    compounds = ("corrosive", "viral", "blast", "gas", "magnetic", "radiation")
    fusion = riven_rating._TRAIT_SPLICED
    scenes = {
        "base": (
            "主射",
            {
                "positive": [
                    *(trait(stat, -40) for stat in elements),
                    trait("critical_chance", -40),
                    *(trait(f"{stat}_damage", -40, fusion) for stat in compounds),
                ],
                "negative": [trait("heat", -40)._replace(shown=-100)],
            },
        ),
        "incarnon": (
            "灵化形态",
            {
                "positive": [
                    *(
                        trait(stat, pct)
                        for stat, pct in zip(elements, (21.7, -0.04, -0.05, 0), strict=True)
                    ),
                    *(
                        trait(f"{stat}_damage", 21.7, fusion)._replace(elem_build=stat)
                        for stat in compounds
                    ),
                ],
                "negative": [],
            },
        ),
    }
    with i18n.use_lang("zh"):
        matrix = riven_rating.trait_matrix({"category": "Rifle"}, scenes)
    rows = [line.split("　") for line in matrix if line.startswith("· ")]
    note = " ⟦v⟧看组合⟦/v⟧"
    expected = ("⟦y⟧+21.7%⟦/y⟧", "⟦n⟧0.0%⟦/n⟧", "⟦m⟧-0.1%⟦/m⟧", "⟦n⟧0.0%⟦/n⟧")
    assert [row[2:] for row in rows[:4]] == [
        ["⟦n⟧—⟦/n⟧" + note, effect + note] for effect in expected
    ]
    assert len(rows) == 6 + len(compounds)
    assert all(row[2].startswith("⟦m⟧-40.0%⟦/m⟧") for row in rows[4:6])
    assert [row[2:] for row in rows[6:]] == [
        ["⟦n⟧—⟦/n⟧" + note, f"⟦y⟧+21.7%⟦/y⟧ ⟦v⟧{label}⟦/v⟧"]
        for label in ("腐蚀套", "病毒套", "爆炸套", "毒气套", "磁力套", "辐射套")
    ]
    with i18n.use_lang("en"):
        matrix = riven_rating.trait_matrix({"category": "Rifle"}, scenes)
    for label in ("Corrosive", "Viral", "Blast", "Gas", "Magnetic", "Radiation"):
        assert any(
            line.endswith(f"⟦n⟧—⟦/n⟧ ⟦v⟧Build-specific⟦/v⟧　⟦y⟧+21.7%⟦/y⟧ ⟦v⟧{label}⟦/v⟧")
            for line in matrix
        )


def test_bundled_element_baselines_decode_render_and_stay_out_of_top_three():
    document = {
        "columns": ("stat", "unit", "flags", "shown31", "pct31", "shown21", "pct21"),
        "statColumns": ("stat_id",),
        "stats": [
            (stat,)
            for stat in (
                "electric_damage",
                "cold_damage",
                "toxin_damage",
                "multishot",
                "blast_damage",
                "gas_damage",
                "viral_damage",
                "radiation_damage",
                "magnetic_damage",
                "corrosive_damage",
            )
        ],
        "units": ["%"],
        "flags": {"spliced": 1, "modeled": 2, "neutral": 4},
        "element_builds": ["corrosive", "viral", "blast", "gas"],
    }
    scene = {
        "baseline_dps": 100,
        "baseline_kpm": 10,
        "positive": [
            [0, 0, 2, 90, 81.2, 105, 94.3],
            [1, 0, 2, 90, -0.05, 105, 29.4],
            [2, 0, 2, 90, -4.6, 105, -3.7],
            [3, 0, 2, 90, 18.3, 105, 19.4],
            [4, 0, 3, 90, 91.2, 105, 94.2],
            [5, 0, 3, 90, 95.4, 105, 33.1],
            [6, 0, 3, 90, 31.5, 105, 32.5],
            [7, 0, 3, 90, 21.6, 105, 25.3],
            [8, 0, 3, 90, -6.4, 105, -3.7],
            [9, 0, 3, 90, 41.4, 105, 41.8],
        ],
        "negative": [],
        "elem_builds": {"0": 0, "1": [1, 31], "4": 2, "5": [3, 31]},
    }
    for roll, lang, name, tag, fallback in (
        ("3+1-", "zh", "多重", "腐蚀套", "看组合"),
        ("2+1-", "en", "Multishot", "Corrosive", "Build-specific"),
    ):
        rows = {
            sign: portable_riven._bundled_trait_rows(document, scene, sign, roll)
            for sign in ("positive", "negative")
        }
        assert [row.elem_build for row in rows["positive"]] == [
            "corrosive",
            "viral" if lang == "zh" else None,
            None,
            None,
            "blast",
            "gas" if lang == "zh" else None,
            None,
            None,
            None,
            None,
        ]
        with (
            i18n.use_lang(lang),
            patch.object(portable_riven, "scenario_line", return_value="◆ 目标"),
        ):
            summary = portable_riven._bundled_trait_summary({}, scene, rows, roll, melee=False)
            lines = riven_rating.trait_matrix(
                {"category": "Rifle"}, {"base": ("主射", rows)}, bundled_roll=roll
            )
        bonuses, fusion_options = summary[-2:]
        assert name in bonuses and "81.2%" not in bonuses and "94.3%" not in bonuses
        assert ("29.4%" in bonuses) == (lang == "en")
        assert fusion_options.count(" / ") == 2
        expected = (
            ("+41.4%", "+31.5%", "+21.6%") if lang == "zh" else ("+41.8%", "+33.1%", "+32.5%")
        )
        assert [part.split("⟦v⟧")[-1] for part in fusion_options.split(" / ")] == [
            f"{gain}⟦/v⟧" for gain in expected
        ]
        assert any(f"⟦v⟧{tag}⟦/v⟧" in line and "⟦y⟧+" in line for line in lines)
        assert any(f"⟦n⟧—⟦/n⟧　⟦v⟧{fallback}⟦/v⟧" in line for line in lines)
        if lang == "zh":
            assert any("⟦n⟧0.0%⟦/n⟧　⟦v⟧病毒套⟦/v⟧" in line for line in lines)


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


async def test_reference_prefers_cheaper_equivalent_serration():
    heavy = {"path": portable_riven._HEAVY_CALIBER, "rank": 10, "slot": 7}
    other = {"path": "/OtherMOD", "rank": 5, "slot": 1}
    request = {"slotPolarities": [None] * 8, "mods": [other, heavy], "incarnonChoices": {}}
    serration = {**heavy, "path": portable_riven._SERRATION}
    assert portable_riven._serration_alternative({**request, "riven": {"slot": 2}}) is None
    assert portable_riven._serration_alternative({**request, "mods": [heavy, serration]}) is None
    assert (
        portable_riven._serration_alternative({**request, "mods": [{**heavy, "rank": 9}]}) is None
    )

    for dps, capacity, valid, use_serration in (
        (100.0, 59, True, True),
        (99.0, 59, True, False),
        (100.0, 60, True, False),
        (100.0, 59, False, False),
    ):
        service = damage_engine.EngineService(Path("."))
        service.ensure_portable = AsyncMock()
        calls = []

        async def replay(_port, _endpoint, body, **_kwargs):
            mods = body["mods"]
            calls.append(mods)
            alternate = serration in mods
            return {
                "engineFingerprint": portable_riven.FINGERPRINT,
                "buildLabel": damage_engine.PORTABLE_DATA,
                "window": {"windowDps": (dps if alternate else 100.0) if len(mods) == 2 else 40.0},
                "validation": {
                    "capacity": capacity if alternate else 60,
                    "valid": valid if alternate else True,
                },
            }

        with (
            patch.object(damage_engine, "portable_request", side_effect=replay),
            patch.object(
                damage_engine,
                "validate_fixed_replay",
                side_effect=lambda row, _, **kw: (row["window"], row["validation"]),
            ),
        ):
            result = await portable_riven._mod_gains(service, 1, request)
        assert result["serration_slot"] == (7 if use_serration else None)
        assert result["capacity_saved"] == int(use_serration)
        assert result["without"] == {1: 40.0, 7: 40.0}
        assert calls == [
            [other, heavy],
            [other, serration],
            [serration if use_serration else heavy],
            [other],
        ]
        assert request["mods"] == [other, heavy]
        await service.close()


async def test_spread_and_auxiliary_replay():
    service = damage_engine.EngineService(Path("."))
    service.ensure_portable = AsyncMock()
    heavy = {"path": portable_riven._HEAVY_CALIBER, "rank": 10, "slot": 7}
    arcane = {"path": "/Arcane", "rank": 3}
    exilus = {"path": "/Exilus", "rank": 2}
    calls = []

    async def engine(port, endpoint, body, **_kwargs):
        assert endpoint == "/api/simulate"
        calls.append(body)
        assert body["exilus"] == exilus and body["arcanes"] == [arcane]
        scenario = body["scenario"]
        heavy_caliber = heavy in body["mods"]
        penalty = scenario["modelSpread"] and scenario["distance"] >= 60 and heavy_caliber
        return {
            "engineFingerprint": portable_riven.FINGERPRINT,
            "buildLabel": damage_engine.PORTABLE_DATA,
            "incarnonChoices": {},
            "window": {"windowDps": 100 if body["mods"] and not penalty else 50},
            "validation": {"capacity": 20 if heavy_caliber else 19, "valid": True},
            "exilus": {**exilus, "nameEn": "Vigilante Supplies", "nameZh": "私法 补给", "drain": 5},
            "arcanes": [
                {**arcane, "nameEn": "Primary Deadhead", "nameZh": "主要·死首", "applied": True}
            ],
        }

    with (
        patch.object(damage_engine, "portable_request", engine),
        patch.object(
            damage_engine,
            "validate_fixed_replay",
            side_effect=lambda row, _, **kw: (row["window"], row["validation"]),
        ),
    ):
        for distance, spread, replaced in ((5, True, True), (65, True, False), (65, False, True)):
            request = {
                "mods": [heavy],
                "slotPolarities": [None] * 8,
                "incarnonChoices": {},
                "scenario": {"distance": distance, "modelSpread": spread},
                "exilus": exilus,
                "arcanes": [arcane],
            }
            start = len(calls)
            result = await portable_riven._mod_gains(service, 1, request)
            assert result["capacity_saved"] == int(replaced)
            assert result["serration_slot"] == (7 if replaced else None)
            assert result["full"] == (100 if replaced else 50)
            assert result["without"] == {7: 50}
            assert len(calls[start:]) == 3
            assert all(row["scenario"] == request["scenario"] for row in calls[start:])
            assert any(
                "私法 补给（2级" in line
                for line in portable_riven._equipment_lines(result["equipment"], verified=True)
            )
            assert await portable_riven._mod_gains(service, 1, request) == result
            assert len(calls[start:]) == 3
    await service.close()


async def test_riven_build_uses_same_auxiliary_loadout():
    host = Plugin()
    host.cfg["damage_engine_portable_port"] = 8765
    service = damage_engine.EngineService(Path("."))
    service.ensure_portable = AsyncMock()
    host.damage_engine = service
    mode = portable_riven.MODES["braton_prime", "base"]
    path = mode[0]
    weapon = {"path": path, "nameEn": "Braton Prime", "fireModes": [{"index": 0}]}
    mod = {"path": "/Mod", "rank": 4, "slot": 0, "nameEn": "Serration", "nameZh": "膛线"}
    selected_exilus = {
        "path": "/Exilus",
        "rank": 5,
        "drain": 5,
        "nameEn": "Vigilante Supplies",
        "nameZh": "私法 补给",
    }
    equipment = {
        "arcanes": {
            "Primary Deadhead": [{"path": "/Deadhead", "maxRank": 5}],
            "Primary Crux": [{"path": "/Crux", "maxRank": 5}],
        },
        "utilityMods": {"Vigilante Supplies": [{"path": "/Exilus", "maxRank": 5}]},
    }
    analyses, replays = [], []
    returning_exilus = True

    async def engine(_port, endpoint, body, **_kwargs):
        assert endpoint == "/api/analyze"
        analyses.append(body)
        actual_card = {**body["rivenCard"], "slot": 1, "drain": 18, "stats": body["stats"]}
        actual_plan = {
            "drain": 13 + (5 if returning_exilus else 0),
            "limit": 60,
            "fits": True,
            "total": 1,
            "slots": {"1": "AP_TACTIC"},
            "placement": [{"origin": 0, "drain": 4}, {"origin": 1, "drain": 9}] + [None] * 6,
        }
        extra = {"exilus": selected_exilus} if returning_exilus else {}
        score = _portable_score(mode, 500)
        if target := body.get("benchmarkTarget"):
            score["evaluationTarget"].update(
                {key: value for key, value in target.items() if key != "enemy"},
                enemyPath=target["enemy"],
            )
            # 护盾或超宏可能让自订工况无法使用标准三节点校准。
            score["targetCalibration"] = {"status": "pending"}
        return {
            **score,
            **extra,
            "weapon": weapon,
            "disposition": body["disposition"],
            "actualGain": 20,
            "evaluationScenario": {"distance": body["scenario"]["distance"], "aiming": True},
            "arcaneStates": [{**row, "applied": True} for row in body["arcanes"]],
            "actual": {"riven": actual_card, "mods": [mod], "forma": actual_plan, **extra},
            "baseline": {"includesRiven": False, "mods": [mod], "forma": {"fits": True}, **extra},
        }

    async def replay(_service, _port, body, *, baseline_request):
        replays.append(body)
        assert "riven" in body and "riven" not in baseline_request
        assert all(
            body[key] == baseline_request[key]
            for key in (
                "exilus",
                "arcanes",
                "scenario",
                "enemy",
                "enemyEnhancements",
                "windowSeconds",
            )
        )
        return {
            "full": 500,
            "baseline": 400,
            "has_overguard": False,
            "without": {0: 200, 1: 400},
            "equipment": {
                "exilus": selected_exilus,
                "arcanes": [
                    {
                        **item,
                        "nameEn": "Primary Deadhead"
                        if item["path"] == "/Deadhead"
                        else "Primary Crux",
                        "nameZh": "主要·死首" if item["path"] == "/Deadhead" else "主要·准星",
                    }
                    for item in body["arcanes"]
                ],
            },
        }

    with (
        patch.object(
            damage_engine, "_portable_catalog", return_value=({path: weapon}, {}, equipment)
        ),
        patch.object(damage_engine, "portable_request", engine),
        patch.object(portable_riven, "_mod_gains", replay),
    ):
        base = "配卡推荐 布莱顿p 主射 暴率178.2 多重142.2 等级8 极性=naramon"
        for arc, exilus, distance in (("主要·死首 5级", "私法补给", 6), ("主要·准星", "自动", 12)):
            reply = await host._h_loadout_recommend(
                parse(f"{base} 百夫长9999 有盾 去超宏 赋能 {arc} 扩展槽 {exilus} 距离{distance}米"),
                None,
                "pc",
            )
            body = analyses[-1]
            assert body["exilus"] == (
                {"mode": "auto"} if exilus == "自动" else {"path": "/Exilus", "rank": 5}
            )
            assert body["arcanes"][0]["rank"] == 5
            assert body["benchmarkTarget"] == {
                "enemy": damage_calc.DEFAULT_ENEMY_PATH,
                "level": 9999,
                "steelPath": True,
                "headshotChance": 1.0,
                "windowSeconds": 180.0,
                "enemyEnhancements": {
                    "eximus": False,
                    "noOverguard": True,
                    "shieldOverride": 3600.0,
                },
            }
            assert body["scenario"] == {"distance": distance, "aiming": True}
            assert replays[-1]["enemyEnhancements"] == body["benchmarkTarget"]["enemyEnhancements"]
            assert replays[-1]["windowSeconds"] == 180
            assert any("凶魂百夫长 Lv9999" in line for line in reply.lines)
            assert any("去超宏 · 模拟有盾" in line for line in reply.lines)
            assert replays[-1]["arcanes"] == body["arcanes"]
            assert replays[-1]["scenario"] == {"distance": distance, "aiming": True}
            assert replays[-1]["exilus"] == {"path": "/Exilus", "rank": 5}
            assert any("私法 补给" in line for line in reply.lines)
            assert any(arc.split()[0] in line and "5级" in line for line in reply.lines)
            assert any("独立引擎 · 180 秒平均 DPS" in line for line in reply.lines)
            assert any("同工况无紫卡配装 400 DPS · 带卡 +25.0%" in line for line in reply.lines)

        returning_exilus = False
        failed = await host._h_loadout_recommend(
            parse(f"{base} 赋能 主要·准星 扩展槽 自动 距离20米"), None, "pc"
        )
        assert any("未确认配卡扩展槽" in line for line in failed.lines)
        assert len(replays) == 2
    await service.close()


async def test_riven_analysis_keeps_target_for_reference():
    host = Plugin()
    with patch.object(host, "_finish_riven_analysis", AsyncMock()) as finish:
        await host._h_riven_analysis(
            parse("紫卡分析 布莱顿p 暴率178.2 多重142.2 百夫长9999 有盾"), None, "pc"
        )
    typed = finish.await_args.args[0]
    assert typed.weapon == "布莱顿p" and typed.positive and not typed.unmatched
    assert finish.await_args.kwargs["target_args"] == ["百夫长9999", "有盾"]


if __name__ == "__main__":
    test_weapon_index()
    test_related_weapon_snapshot()
    asyncio.run(test_sources())
    test_sale_trait_localization()
    asyncio.run(test_trait_sale_source_independent())
    test_mode_aliases_keep_weapon_and_target()
    test_extended_recommendation_routes()
    test_precompute_melee_routes()
    test_bundled_variant_from_command_parameters()
    test_bundled_headshot_rate_matches_each_mode()
    test_precomputed_headshot_weapons_and_cycle_profiles()
    test_precomputed_full_headshots_share_weakpoint_trait_baseline()
    test_gauge_charging_replay_defaults_and_compact_header()
    asyncio.run(test_bundled_multimode_build_priority_and_silent_hits())
    test_bundled_incarnon_build_priority()
    test_unmodeled_melee_incarnon_tier1_metadata()
    asyncio.run(test_melee_incarnon_tier1_note_on_recommendation_cards())
    asyncio.run(test_bundled_target_recommendations_and_live_fallback())
    test_precompute_merge_targets_and_output_paths()
    test_precompute_element_baselines_use_selected_target()
    asyncio.run(test_standalone_errors())
    asyncio.run(test_reference_build_replays_auto_equipment_and_locked_options())
    test_rank_zero_set_exilus_description()
    test_unpolarized_special_slots()
    asyncio.run(test_bundled_metadata_failure_falls_back_to_engine())
    asyncio.run(test_trait_matrix_cache_identity())
    asyncio.run(test_build_and_simulate_cache_identity())
    test_slot_plan_matches_exhaustive_order()
    asyncio.run(test_live_recommendations_use_separate_routes_and_persistent_cache())
    asyncio.run(test_legacy_view_does_not_override_live_fallback())
    asyncio.run(test_live_traits_search_one_default_per_variant())
    asyncio.run(test_recommendation_requires_engine_and_preserves_sale())
    asyncio.run(test_trait_matrix_keeps_configured_sale_reference_without_querying_listings())
    asyncio.run(test_recommendation_weapon_aliases())
    asyncio.run(test_bundled_missing_traits_show_on_recommendation_cards())
    asyncio.run(test_image_chooses_best_supported_variant())
    asyncio.run(test_screenshot_reference_selects_variant_and_falls_back())
    asyncio.run(test_loadout_with_unspecified_riven_polarity())
    asyncio.run(test_full_rank_riven_build_validation_and_cache())
    asyncio.run(test_portable_melee_card_capacity_and_replay_fallback())
    asyncio.run(test_ballistica_dynamic_loadout())
    test_rank_inference()
    test_unspecified_riven_slot_halves_drain()
    asyncio.run(test_portable_search_preserves_card_rank())
    test_portable_card_language_and_removed_impact()
    test_portable_neutral_melee_scenario()
    test_portable_enemy_labels_and_english_evolutions()
    test_portable_optional_equipment_and_context()
    test_offline_riven_comparison_context()
    test_trait_matrix_contextual_stats_show_dashes_for_both_signs()
    test_trait_matrix_keeps_distance_effects_and_other_zeroes()
    test_prepared_trait_matrix_marks_incarnon_ammo()
    test_bundled_trait_matrix_explains_weakpoint_scope()
    test_bundled_trait_summary_marks_only_weakpoint_fusion_options()
    test_trait_matrix_hides_negative_element_bonus_gains_and_shows_matching_sets()
    test_bundled_element_baselines_decode_render_and_stay_out_of_top_three()
    test_reference_overflow_fits_without_rearranging_mods()
    asyncio.run(test_reference_prefers_cheaper_equivalent_serration())
    asyncio.run(test_spread_and_auxiliary_replay())
    asyncio.run(test_riven_build_uses_same_auxiliary_loadout())
    asyncio.run(test_riven_analysis_keeps_target_for_reference())
    print("武器识别、模式别名、引擎推荐、持久缓存与实卡重搜通过")
