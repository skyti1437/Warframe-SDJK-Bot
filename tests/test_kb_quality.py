"""P1：真实生成入口、资产候选保留与资料卡读取的离线回归。"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts" / "kb"))


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class QualityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.base = Path(cls.temp.name)
        for folder in ("items/i18n", "pep", "cfg", "out"):
            (cls.base / folder).mkdir(parents=True)
        for file, data in {
            "items/i18n/zh.json": {"/fixture/seed": {"name": "夹具"}},
            "items/Melee.json": [],
            "items/Primary.json": [],
            "items/Secondary.json": [],
            "items/Mods.json": [],
            "items/Arcanes.json": [],
            "pep/dict.zh.json": {},
            "pep/dict.en.json": {},
            "cfg/damageTypes.json": ["impact", "slash"],
            "cfg/polarities.json": [],
            "cfg/relicGrades.json": [],
        }.items():
            (cls.base / file).write_text(json.dumps(data), encoding="utf-8")
        os.environ["WF_KB_DATA"] = str(cls.base)
        os.environ["WF_KB_OUT"] = str(cls.base / "out")
        cls.builder = load_module("quality_builder", ROOT / "scripts/kb/build_kb.py")
        cls.lib = sys.modules["kb_lib"]
        cls.cards = load_module("quality_cards", ROOT / "scripts/build_wiki_intro.py")

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_component_panel_and_normal_weapon(self):
        b = self.builder
        original = b.I["Melee"]
        try:
            b.I["Melee"] = [
                {
                    "uniqueName": "/fixture/part",
                    "name": "Part",
                    "type": "Zaw Component",
                    "totalDamage": 0,
                    "damagePerShot": [-2.8e22, 0],
                    "criticalChance": 0,
                    "criticalMultiplier": 0,
                    "procChance": 0,
                    "fireRate": 0,
                },
                {
                    "uniqueName": "/fixture/weapon",
                    "name": "Weapon",
                    "type": "Melee",
                    "totalDamage": 100,
                    "damagePerShot": [40, 60],
                    "criticalChance": 0.2,
                },
            ]
            _, blocks = b.weapon_part("近战", "Melee")
            part, weapon = blocks
            self.assertIn("组合后", part)
            self.assertNotIn("总伤害", part)
            self.assertNotIn("暴击几率", part)
            self.assertIn("总伤害：100", weapon)
            self.assertIn("暴击几率 20%", weapon)
            self.assertIn('"asset_id": "de:/fixture/part"', part)
        finally:
            b.I["Melee"] = original

    def test_hotfix_exact_assets_and_real_builders(self):
        from unittest.mock import patch

        b = self.builder
        hotfix = b.S.zh_overrides["hotfix_44_1_3"]
        assets = hotfix["assets"]
        records = [
            {"uniqueName": key, "name": value["name"], "levelStats": [{"stats": ["OLD 102%"]}]}
            for key, value in assets.items()
        ]
        mods = [r for r in records if r["name"] in ("Brain Storm", "Skull Shots")]
        arcs = [r for r in records if r["name"] in ("Arcane Pistoleer", "Eternal Logistics")]
        ax = next(r for r in records if r["name"] == "AX-52")
        decoy = {"uniqueName": "/fixture/other", "name": "Brain Storm"}
        self.assertIsNone(b.hotfix_asset_effect(decoy))
        self.assertEqual(b.mod_effect(decoy), ([], "official"))
        # 真实生成器必须跳过赋能旧覆盖，不把新旧效果并列。
        stale = {r["uniqueName"]: {"effect": "STALE 102% / 72%"} for r in arcs}
        with patch.dict(b.I, {"Mods": mods, "Arcanes": arcs}), patch.object(b, "P", {}):
            with patch.object(b.S, "ov_arcane", stale):
                b.build_mods()
        text = (self.base / "out/03_MOD与赋能.md").read_text()
        self.assertNotIn("OLD", text)
        self.assertNotIn("STALE", text)
        self.assertIn("2** 张 MOD / **2** 个赋能 —— **44.1.3 英文补丁归纳", text)
        for r in mods + arcs:
            self.assertIn(assets[r["uniqueName"]]["effect"], text)
        self.assertIn("10%，持续时间同为 12 秒", text)
        self.assertIn("补丁未列出未升级数值", text)
        # 进化效果按源索引的武器段分配，机制说明不伪造资产 ID。
        index = {
            stem: {"asset_id": "/fixture/" + stem, "weapon_zh": stem} for stem in hotfix["incarnon"]
        }
        with patch.dict(b.I, {"Primary": [ax], "Secondary": [], "Melee": []}):
            with patch.object(b, "incarnon_index", return_value=index):
                b.build_weapons()
        text = (self.base / "out/02_武器.md").read_text()
        self.assertIn("腰射时弹药效率 +75%", text)
        self.assertEqual(text.count("Crimson Overture"), 2)
        self.assertEqual(text.count("Enough for Everyone"), 1)
        self.assertIn("Flowing Slayer", text)
        self.assertIn("其他时候弹药效率 -25%", text)
        self.assertIn("多个来源合计也受此上限限制", text)
        self.assertIn("效果不变，不能解释成普通弹药效率被封顶到 75%", text)
        self.assertNotIn("de:Flowing", text)
        self.assertNotIn("DE 官方简中）：腰射", text)

    def test_riven_unified_and_historical_real_builders(self):
        from unittest.mock import patch
        import ast

        b = self.builder
        rec = {
            "uniqueName": "/fixture/riven-weapon",
            "name": "Fixture Rifle",
            "type": "Rifle",
            "totalDamage": 100,
            "damagePerShot": [40, 60],
            "omegaAttenuation": 0.65,
        }
        with patch.dict(b.I, {"Primary": [rec], "Secondary": [], "Melee": []}):
            for mode in ("unified", "historical"):
                with patch.object(b, "RIVEN_DISPOSITION_MODE", mode):
                    b.build_weapons()
                    text = (self.base / "out/02_武器.md").read_text()
                    self.assertIn('"asset_id": "de:/fixture/riven-weapon"', text)
                    self.assertIn("总伤害：100", text)
                    self.assertIn("0.65", text)
                    if mode == "unified":
                        self.assertIn("2026-10 起统一 1.2（取消前 0.65）", text)
                        self.assertIn("差异化紫卡倾向已取消", text)
                        self.assertNotIn("倾向随 Prime 武器更替", text)
                    else:
                        self.assertIn("紫卡倾向（历史快照）：0.65", text)
                        self.assertIn("历史快照模式", text)
                        self.assertNotIn("2026-10 起统一 1.2（取消前 0.65）", text)
        for mode in ("unified", "historical"):
            with patch.dict(os.environ, {"WF_KB_RIVEN_DISPOSITION": mode}):
                self.assertEqual(b.riven_disposition_mode(), mode)
        with patch.dict(os.environ, {"WF_KB_RIVEN_DISPOSITION": "typo"}):
            with self.assertRaises(ValueError):
                b.riven_disposition_mode()
        plugin = ast.parse((ROOT / "core/riven_analysis.py").read_text())
        constant = next(
            n.value.value
            for n in plugin.body
            if isinstance(n, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "UNIFIED_DISPOSITION" for t in n.targets)
        )
        self.assertEqual(b.UNIFIED_DISPOSITION, constant)

    def test_native_identity_aggregates_and_manual_missing(self):
        from unittest.mock import patch

        b = self.builder
        marker = b.asset_marker("/fixture/a", related_asset_ids=["/fixture/b", "/fixture/a"])
        identity = json.loads(marker[len("<!-- wf-asset ") : -len(" -->")])
        self.assertEqual(identity["asset_ids"], ["de:/fixture/a", "de:/fixture/b"])
        with self.assertRaises(ValueError):
            b.asset_marker("kb:content-hash")
        with self.assertRaises(ValueError):
            b.asset_marker("/fixture/a", related_asset_ids=["guessed-name"])
        self.assertNotIn("wf-asset", b.entry("机制概览", "- 说明：无单一资产"))
        with patch.object(b, "MISSING_ASSET_IDS", []):
            self.assertNotIn("wf-asset", b.entry("成就", "- 说明：保留原文", "Own1Sentinel"))
            self.assertEqual(b.MISSING_ASSET_IDS[0]["source_key"], "Own1Sentinel")
        enemy = {"uniqueName": "/fixture/enemy", "name": "Enemy", "health": 100}
        with (
            patch.dict(b.I, {"Enemy": [enemy]}),
            patch.dict(b.P, {"Enemies": {}}),
            patch.object(b.S, "ov_boss", {"manual": {"name": "人工首领", "desc": "两源无ID"}}),
            patch.object(b, "MISSING_ASSET_IDS", []),
        ):
            b.build_enemies()
            text = (self.base / "out/05_敌人.md").read_text()
            self.assertIn('"asset_id": "de:/fixture/enemy"', text)
            self.assertEqual(b.MISSING_ASSET_IDS[0]["title"], "人工首领")
            self.assertNotIn("de:manual", text)

    def test_codex_rows_carry_source_key_through_sort_and_group(self):
        from unittest.mock import patch

        b = self.builder
        animals = {
            "/fixture/animal/a": {"name": "/fixture/name", "health": 100},
            "/fixture/animal/b": {"name": "/fixture/name", "health": 100},
        }
        codex = {"objects": {"/fixture/object": {"name": "/fixture/name"}}}
        with (
            patch.dict(b.P, {"Animals": animals, "Codex": codex}, clear=True),
            patch.dict(b.I, {"Relics": []}),
            patch.dict(b.S.dz, {"/fixture/name": "身份测试样本"}),
            patch.dict(b.S.de, {"/fixture/name": "Identity Test"}),
        ):
            b.build_codex_life()
            text = (self.base / "out/10_图鉴与生态.md").read_text()
            self.assertIn('"asset_id": "de:/fixture/object"', text)
            self.assertIn('"asset_ids": ["de:/fixture/animal/a", "de:/fixture/animal/b"]', text)
            self.assertIn("共 2 个变体资产", text)

    def test_damage_bounds_and_text_precision(self):
        lib = self.lib
        for v in (-1, 1e22, float("inf"), float("nan"), "bad"):
            self.assertFalse(lib.valid_damage(v))
        self.assertTrue(lib.valid_damage(0))
        self.assertTrue(lib.valid_damage(12345.5))
        self.assertEqual(lib.fmt_damage([100, -1], ["impact", "slash"]), "冲击 100")
        self.assertEqual(
            lib.clean("after 0.6000000238418579s / 0.10000000149011612s"), "after 0.6s / 0.1s"
        )
        self.assertEqual(lib.clean("rate 0.123456789"), "rate 0.123456789")

    def test_capacity_real_mod_generator(self):
        b = self.builder
        original = b.I["Mods"]
        try:
            b.I["Mods"] = [
                {
                    "uniqueName": f"/fixture/bad/{i}",
                    "name": f"Bad {i}",
                    "baseDrain": 2025586688,
                    "fusionLimit": 593,
                }
                for i in range(17)
            ] + [
                {"uniqueName": "/fixture/good", "name": "Good", "baseDrain": -4, "fusionLimit": 10}
            ]
            b.build_mods()
            text = (self.base / "out/03_MOD与赋能.md").read_text()
            self.assertNotIn("2025586688", text)
            self.assertNotIn("593", text)
            self.assertIn("基础容量 -4", text)
            self.assertIn("满级 10", text)
        finally:
            b.I["Mods"] = original
        for value in (float("nan"), float("inf"), "bad"):
            self.assertFalse(b.valid_mod_capacity(value, -30, 30))

    def test_same_title_never_discards_asset(self):
        b = self.builder
        body = "\n".join(
            [
                b.entry("Same", "- 说明：相同", "/fixture/a"),
                b.entry("Same", "- 说明：相同", "/fixture/b"),
                b.entry("Same", "- 说明：相同", "/fixture/a"),
            ]
        )
        rendered, dropped, _ = b.dedupe_body(body)
        self.assertEqual(dropped, 0)
        self.assertEqual(rendered.count("<!-- wf-asset "), 3)
        ids = []
        path = self.base / "out/02_武器.md"
        path.write_text("## 武器\n" + rendered, encoding="utf-8")
        entries, _ = self.cards.parse_kb(path, 7, ids)
        self.assertEqual(len(entries), 3)
        self.assertEqual(
            Counter(i["asset_id"] for i in ids), Counter({"de:/fixture/a": 2, "de:/fixture/b": 1})
        )

    def test_compact_identity_and_legacy_parser(self):
        b = self.builder
        p = self.base / "compact.md"
        p.write_text(
            "## 五、外观装饰与杂项\n"
            + b.asset_marker("/fixture/c", "Skins", "Skin")
            + "\n- 名称（Name）：说明\n",
            encoding="utf-8",
        )
        identities = []
        rows = self.cards.parse_line_items(p, identities)
        self.assertEqual(rows, [("名称（Name）", ["说明"])])
        self.assertEqual(identities[0]["asset_id"], "de:/fixture/c")
        p.write_text("### 旧条目\n- 说明：旧格式\n", encoding="utf-8")
        identities = []
        rows, _ = self.cards.parse_kb(p, 3, identities)
        self.assertEqual(rows, [("旧条目", ["说明：旧格式"])])
        self.assertEqual(identities, [{}])

    def test_legacy_and_malformed_identity_lookup(self):
        from core import wiki_intro

        original = wiki_intro._FILE
        try:
            path = self.base / "legacy.json"
            path.write_text(
                json.dumps(
                    {"entries": [["Old", ["说明：旧数据"]]], "keys": {"old": 0}, "uses": {}}
                ),
                encoding="utf-8",
            )
            wiki_intro._FILE = path
            wiki_intro._table.cache_clear()
            self.assertEqual(wiki_intro.intro("Old")[0], "Old")
            path.write_text(
                json.dumps(
                    {
                        "entries": [["Old", ["说明：旧数据"]]],
                        "keys": {"old": [{"bad": 1}]},
                        "assets": {},
                        "uses": {},
                    }
                ),
                encoding="utf-8",
            )
            wiki_intro._table.cache_clear()
            self.assertEqual(wiki_intro.candidates("Old"), [])
        finally:
            wiki_intro._FILE = original
            wiki_intro._table.cache_clear()

    def test_compact_projection_preserves_assets_views_and_priority(self):
        from core import wiki_intro

        full = {
            "_meta": {"schema": 2},
            "entries": [["变体", ["说明：PvP"]], ["普通", ["说明：普通"]], ["附加视图", ["说明"]]],
            "keys": {"同名": ["de:/fixture/PvPItem", "de:/fixture/Item"]},
            "id_entries": {"de:/fixture/PvPItem": [0], "de:/fixture/Item": [1, 2]},
            "uses": {},
        }
        compact = self.cards.compact_cards(full)
        self.assertEqual(compact["entries"], full["entries"])
        recovered = [full["entries"][i] for i in compact["keys"]["同名"]]
        self.assertEqual(recovered, full["entries"][:2])
        self.assertEqual(compact["views"], {"1": [2]})
        original = wiki_intro._FILE
        try:
            path = self.base / "compact.json"
            path.write_text(json.dumps(compact), encoding="utf-8")
            wiki_intro._FILE = path
            wiki_intro._table.cache_clear()
            rows = wiki_intro.candidates("同名")
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["variant"], "normal")
            self.assertEqual(wiki_intro.exact_title("同名"), "普通")
            self.assertEqual(wiki_intro.intro("同名")[0], "普通")
        finally:
            wiki_intro._FILE = original
            wiki_intro._table.cache_clear()

    def test_schema_two_reader_and_exact_title(self):
        from core import wiki_intro

        original = wiki_intro._FILE
        try:
            path = self.base / "schema2.json"
            path.write_text(
                json.dumps(
                    {
                        "_meta": {"schema": 2},
                        "entries": [["PvP", ["说明"]], ["普通", ["说明"]]],
                        "keys": {"同名": ["de:/pvp", "de:/normal"]},
                        "assets": [
                            {"asset_id": "de:/pvp", "variant": "pvp"},
                            {"asset_id": "de:/normal", "variant": "normal"},
                        ],
                        "id_entries": {"de:/pvp": [0], "de:/normal": [1]},
                        "uses": {},
                    }
                ),
                encoding="utf-8",
            )
            wiki_intro._FILE = path
            wiki_intro._table.cache_clear()
            self.assertEqual(wiki_intro.exact_title("同名"), "普通")
            self.assertEqual(len(wiki_intro.candidates("同名")), 2)
        finally:
            wiki_intro._FILE = original
            wiki_intro._table.cache_clear()

    def test_real_command_preserves_explicit_pvp(self):
        import asyncio

        from core import parser
        from core.api_client import WarframeClient
        from core.commands.wiki_misc import WikiMiscCommands

        client = object.__new__(WarframeClient)
        client._aliases = json.loads((ROOT / "core/data/aliases.json").read_text())

        async def forbidden(_):
            raise AssertionError("本地精确卡片不应发出远程查询")

        client.resolve_wm_item = forbidden
        harness = WikiMiscCommands()
        harness.cfg = {"wiki_intro": True}
        harness.client = client
        normal = asyncio.run(harness._h_wiki(parser.parse("wiki Balla"), None, "pc"))
        pvp = asyncio.run(harness._h_wiki(parser.parse("wiki Balla PvP"), None, "pc"))
        self.assertEqual(normal.title, "宝拉（Balla）")
        self.assertEqual(pvp.title, "宝拉（Balla）")
        self.assertIn("◆ 版本：PvP 变体", pvp.lines)
        self.assertNotIn("◆ 版本：PvP 变体", normal.lines)

    def test_real_card_lookup_and_all_candidates(self):
        from core import wiki_intro

        for query, title in [
            ("Balla", "宝拉（Balla）"),
            ("Balla PvP", "宝拉（Balla）"),
            ("宝拉", "宝拉（Balla）"),
            ("Wisp Prime", "Wisp Prime"),
        ]:
            self.assertEqual(wiki_intro.intro(query)[0], title)
        rows = wiki_intro.candidates("Balla")
        self.assertEqual(len(rows), 2)
        self.assertEqual({r["variant"] for r in rows}, {"normal", "pvp"})
        self.assertEqual(rows[0]["variant"], "normal")
        self.assertIn("PvP 变体", "\n".join(wiki_intro.intro("Balla PvP")[1]))
        self.assertEqual(len(wiki_intro.candidates("头盔或披饰")), 18)
        self.assertGreaterEqual(len(wiki_intro.candidates("未融合过的神器")), 43)


if __name__ == "__main__":
    unittest.main(verbosity=2)
