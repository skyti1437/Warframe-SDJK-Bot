"""固定方案离线回归；合成协议兼容不代表真实模型识别通过。"""

from __future__ import annotations

import json
import sys
import unittest
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.commands.scan import ScanCommands  # noqa: E402
from core.commands.vision import original_vision_prompt  # noqa: E402
from core.loadout_ocr import parse_focus_text  # noqa: E402
from core.vision_prompt_presets import (  # noqa: E402
    preset_catalog,
    preset_detail,
    preset_prompt,
)
from core.vision_prompts import TASKS  # noqa: E402

# 内置提示词字节快照：紫卡仅保留原文数值、符号与单位，换算由代码负责。
# 配卡主读承担文字位置；装备路只抄文字，等级由像素和容量关系计算。
LEGACY_HASHES = {
    "scan_tiles": "ab3857dbeedce94ec1ac0e5fe34163a27c8a03d02142b03109aed06284253782",
    "riven_json": "7e4634cb9f443b354ca7c928859838f9f918baef82242019343357e52a6d3429",
    "riven_lines": "f18e2d5be188bcb4f32061eec56dffdc3de14b7b5d046a03ca474a0cc080ec5a",
    "scan_main": "f18e2d5be188bcb4f32061eec56dffdc3de14b7b5d046a03ca474a0cc080ec5a",
    "scan_title": "8474bd0b318fd33e9f56c677cc03f67e76a83ba3e21f933a567acf6a1ac44524",
    "scan_mods": "852a35cbc335f62e99ef3bddc227f0608a37d9f806651ce1eb3d49a492f68d6a",
    "scan_arcanes": "16aa32044f19a0da380dafa3804b3f50acc667165544dbb0fc8e18d454c4ced1",
    "scan_damage_rows": "259e3aef3df5c3fcfb19132015aeed63863d8c23e1fe8f08d00ef10e05131038",
    "scan_panel": "65ac0de44e5245c7134086112bc7c15cec82491bb665ed3ffe40203e702bd407",
}
SYNTHETIC_FIXTURES = [
    {
        "id": "json_full_raw_units_pairs_rows",
        "input": '{"panel": {"attack_speed": "2.25/秒 → 3.75/秒", "crit_chance": "19% → 29%", "crit_damage": "1.7倍 → 2.9倍", "status_chance": "13% → 23%", "damage_rows": [["合成行甲", "17 → 31"], ["合成行乙", "2,345.6 → 3,456.7"], ["合成行甲", "8 → 9"]], "total": "2,362.6 → 3,487.7", "heavy": "7,654.3"}}',
        "expected_exact": {
            "panel": {
                "attack_speed": "2.25/秒 → 3.75/秒",
                "crit_chance": "19% → 29%",
                "crit_damage": "1.7倍 → 2.9倍",
                "status_chance": "13% → 23%",
                "damage_rows": [
                    ["合成行甲", "17 → 31"],
                    ["合成行乙", "2,345.6 → 3,456.7"],
                    ["合成行甲", "8 → 9"],
                ],
                "total": "2,362.6 → 3,487.7",
                "heavy": "7,654.3",
            }
        },
        "expected_relation": "原样返回；左右列、单位、重复行和行序均保留",
    },
    {
        "id": "json_null_missing_no_fill",
        "input": '{"panel": {"attack_speed": null, "crit_chance": "12% → 34%", "crit_damage": null, "status_chance": null, "damage_rows": [["合成行甲", null]], "total": null, "heavy": null}}',
        "expected_exact": {
            "panel": {
                "attack_speed": None,
                "crit_chance": "12% → 34%",
                "crit_damage": None,
                "status_chance": None,
                "damage_rows": [["合成行甲", None]],
                "total": None,
                "heavy": None,
            }
        },
        "expected_relation": "null保持；没有额外补值或补行",
    },
    {
        "id": "json_heading_in_scalar_negative",
        "input": '{"panel": {"attack_speed": "攻击速度", "crit_chance": "暴击几率", "crit_damage": "暴击倍率", "status_chance": "触发几率", "damage_rows": [], "total": null, "heavy": null}}',
        "expected_exact": {
            "panel": {
                "attack_speed": "攻击速度",
                "crit_chance": "暴击几率",
                "crit_damage": "暴击倍率",
                "status_chance": "触发几率",
                "damage_rows": [],
                "total": None,
                "heavy": None,
            }
        },
        "expected_relation": "负例：现parser原样接收，标题填值未在此层拒绝；不能记为防护通过",
    },
    {
        "id": "plain_headings_only_negative",
        "input": "攻击速度\n暴击几率\n暴击倍率\n触发几率",
        "expected_exact": None,
        "expected_relation": "纯标题原文不形成panel",
    },
]


class PresetTests(unittest.TestCase):
    def test_riven_weapon_is_official_name_and_latin_part_is_riven_suffix(self):
        prompt = original_vision_prompt("riven_json")
        self.assertIn("卡面武器名或部件名", prompt)
        self.assertIn("拉丁词缀", prompt)
        self.assertNotIn("自命名", prompt)
        self.assertNotIn("自定义", prompt)
        self.assertIn('"stat_lines"', prompt)
        self.assertIn("不加主要/次要后缀", prompt)

    def test_riven_reads_raw_units_and_code_owns_game_rules(self):
        prompt = original_vision_prompt("riven_json")
        self.assertIn("看不清的字符写问号，不猜数", prompt)
        self.assertIn("未揭示词条用问号保留位置", prompt)
        self.assertIn("正负号、小数、百分号", prompt)
        self.assertIn("x或×倍率", prompt)
        self.assertIn("不做正负面判断、数值换算", prompt)
        self.assertNotIn("(倍率 − 1) × 100", prompt)
        self.assertNotIn("数值只写数字", prompt)

    def test_original_eight_strings_frozen(self):
        for task, expected in LEGACY_HASHES.items():
            prompt = (
                ScanCommands._ROWS_PROMPT
                if task == "scan_damage_rows"
                else original_vision_prompt(task)
            )
            with self.subTest(task=task):
                self.assertEqual(sha256(prompt.encode("utf-8")).hexdigest(), expected)

    def test_candidate_exact_hash_and_scope(self):
        for task in TASKS:
            baseline = preset_detail("legacy_v1", task)
            candidate = preset_detail("scan_panel_json_v2_draft", task)
            self.assertEqual(baseline["prompt"], original_vision_prompt(task))
            self.assertTrue(baseline["inherited"])
            self.assertEqual(candidate["inherited"], task != "scan_panel")
            if task == "scan_panel":
                self.assertEqual(
                    candidate["sha256"],
                    "b4e6b645cb0a82b68131b2187a6fe57a6c29df0f484affb2b0c0fe5f9355d44a",
                )
                self.assertEqual(len(candidate["prompt"].encode("utf-8")), 1355)
            else:
                self.assertEqual(candidate["prompt"], baseline["prompt"])

    def test_inheritance_uses_current_original(self):
        with patch("core.vision_prompt_presets.original_vision_prompt", return_value="当前原文"):
            self.assertEqual(preset_prompt("legacy_v1", "scan_panel"), "当前原文")
            self.assertEqual(preset_prompt("scan_panel_json_v2_draft", "scan_main"), "当前原文")

    def test_catalog_summary_and_independent_containers(self):
        catalog = preset_catalog()
        self.assertEqual([x["id"] for x in catalog], ["legacy_v1", "scan_panel_json_v2_draft"])
        self.assertEqual(catalog[1]["overridden_tasks"], ["scan_panel"])
        for item in catalog:
            self.assertNotIn("prompt", item)
        self.assertEqual(catalog[0]["model_evaluation"], "existing_task_baseline")
        self.assertEqual(catalog[1]["model_evaluation"], "limited_scan_panel_samples")
        self.assertEqual(catalog[1]["samples_per_model"], 3)
        self.assertEqual(catalog[1]["repeats_per_image_and_scheme"], 1)
        self.assertEqual(catalog[1]["status"], "实验")
        self.assertIn("scan_panel", catalog[1]["protocol_boundary"])
        self.assertIn("标题", catalog[1]["protocol_boundary"])
        catalog[1]["overridden_tasks"].clear()
        self.assertEqual(preset_catalog()[1]["overridden_tasks"], ["scan_panel"])

    def test_unknown_identifiers_rejected(self):
        for function in (preset_prompt, preset_detail):
            for preset, task in [
                ("missing", "scan_panel"),
                ("legacy_v1", "missing"),
                ("legacy_v1", "scan_damage_rows"),
                ("legacy_v1", None),
            ]:
                with self.subTest(function=function.__name__, preset=preset, task=task):
                    with self.assertRaises(ValueError):
                        function(preset, task)

    def test_synthetic_parser_protocol_and_known_negative_limit(self):
        for fixture in SYNTHETIC_FIXTURES:
            with self.subTest(fixture=fixture["id"]):
                self.assertEqual(
                    parse_focus_text(fixture["input"], "panel"), fixture["expected_exact"]
                )
        # 负例确实仍被接收：此断言记录限制，绝不表示标题填值防护通过。
        negative = SYNTHETIC_FIXTURES[2]
        self.assertEqual(json.loads(negative["input"])["panel"]["attack_speed"], "攻击速度")
        self.assertEqual(
            parse_focus_text(negative["input"], "panel")["panel"]["attack_speed"], "攻击速度"
        )


if __name__ == "__main__":
    unittest.main()
