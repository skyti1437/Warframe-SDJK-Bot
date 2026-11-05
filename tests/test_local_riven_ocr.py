"""本地紫卡纯解析包的合成护栏；无OCR、网络、GT或本机模型依赖。"""

import copy
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.local_riven_ocr import analyze_raw
from core.local_riven_ocr.annotation_fields import split_tail_annotation
from core.local_riven_ocr.continuation import merge_parenthesized_continuations
from core.local_riven_ocr.layout_candidate import analyze as analyze_layout
from core.local_riven_ocr.name_evidence import _extract, _lookup
from core.local_riven_ocr.prefix_field import propose_prefix_field
from core.local_riven_ocr.weapon_identity import assess, build_catalog


def exact(name):
    return {"甲属性": "a", "乙属性": "b"}.get(name)


def fixture(first="+37.5%甲属性（说明", second="条件x3)"):
    texts = [first, second, "-12%乙属性", "段位8"]
    rows = []
    for i, text in enumerate(texts):
        x0, x1 = (100, 300) if i != 1 else (150, 240)
        y = 100 + 20 * i
        rows.append({"text": text, "quad": [[x0, y], [x1, y], [x1, y + 20], [x0, y + 20]]})
    raw = {"width": 600, "height": 500, "rows": rows}
    analysis = {
        "accepted_region": True,
        "region": [70, 50, 330, 190],
        "lines": texts[:3],
        "line_sources": [{"ids": [i], "text": text} for i, text in enumerate(texts[:3])],
        "title_ids": [],
        "excluded_ids": [3],
        "diagnostics": {"footer_y": 170, "unresolved_ids": []},
    }
    return analysis, raw


class ContinuationTests(unittest.TestCase):
    def reject(self, analysis, raw, reason=None):
        before = copy.deepcopy(analysis)
        new, proposals = merge_parenthesized_continuations(analysis, raw, exact)
        self.assertEqual(new, before)
        self.assertFalse(any(p["status"] == "merged" for p in proposals))
        if reason:
            self.assertEqual(proposals[0]["reason"], reason)

    def test_existing_parenthesis_numeric_annotation_merges_and_raw_is_immutable(self):
        analysis, raw = fixture()
        old_analysis, old_raw = copy.deepcopy(analysis), copy.deepcopy(raw)
        new, proposals = merge_parenthesized_continuations(analysis, raw, exact)
        self.assertEqual(len(new["lines"]), 2)
        self.assertEqual(
            new["line_sources"][0], {"ids": [0, 1], "text": "+37.5%甲属性（说明 条件x3)"}
        )
        self.assertEqual(proposals[0]["status"], "merged")
        self.assertEqual(analysis, old_analysis)
        self.assertEqual(raw, old_raw)

    def test_only_the_matching_name_ambiguity_is_resolved(self):
        analysis, raw = fixture()
        ownership = {
            "source": {"ids": [1], "text": raw["rows"][1]["text"]},
            "previous_ids": [0],
            "next_ids": [2],
            "target": None,
        }
        analysis["diagnostics"]["name_ownership"] = [ownership]
        analysis["diagnostics"]["ambiguities"] = ["ambiguous_name_continuation"]
        result, proposals = merge_parenthesized_continuations(analysis, raw, exact)
        self.assertEqual(proposals[0]["status"], "merged")
        self.assertEqual(result["diagnostics"]["ambiguities"], [])
        self.assertTrue(
            result["diagnostics"]["name_ownership"][0]["resolved_by_parenthesized_continuation"]
        )
        analysis["diagnostics"]["name_ownership"].append(
            {"source": {"ids": [99], "text": "另一个歧义"}, "target": None}
        )
        result, _ = merge_parenthesized_continuations(analysis, raw, exact)
        self.assertEqual(result["diagnostics"]["ambiguities"], ["ambiguous_name_continuation"])

    def test_mixed_brackets_are_not_numeric_values(self):
        analysis, raw = fixture("+37.5%甲属性(说明", "x3）")
        new, proposals = merge_parenthesized_continuations(analysis, raw, exact)
        self.assertEqual(proposals[0]["status"], "merged")
        self.assertEqual(new["line_sources"][0]["ids"], [0, 1])

    def test_icon_prefix_kept_verbatim(self):
        analysis, raw = fixture("θ+37.5%甲属性（说明")
        new, _ = merge_parenthesized_continuations(analysis, raw, exact)
        self.assertTrue(new["lines"][0].startswith("θ+"))

    def test_independent_positive_not_swallowed(self):
        self.reject(*fixture(second="+28%乙属性)"), "independent_signed_attribute")

    def test_independent_negative_not_swallowed(self):
        self.reject(*fixture(second="-28%乙属性)"), "independent_signed_attribute")

    def test_independent_multiplier_not_swallowed(self):
        self.reject(*fixture(second="x3乙属性)"), "independent_multiplier_attribute")

    def test_independent_trailing_multiplier_not_swallowed(self):
        self.reject(*fixture(second="乙属性x3)"), "independent_multiplier_attribute")

    def test_independent_trailing_sign_not_swallowed(self):
        self.reject(*fixture(second="乙属性+28%)"), "independent_signed_attribute")

    def test_closed_first_line_does_not_swallow_x(self):
        self.reject(*fixture(first="+37.5%甲属性（说明）", second="x3乙属性"))

    def test_cross_column_not_swallowed(self):
        analysis, raw = fixture()
        raw["rows"][1]["quad"] = [[400, 120], [490, 120], [490, 140], [400, 140]]
        self.reject(analysis, raw, "no_unique_adjacent_row")

    def test_cross_card_region_not_swallowed(self):
        analysis, raw = fixture()
        analysis["region"][3] = 125
        self.reject(analysis, raw, "outside_accepted_region")

    def test_far_row_not_swallowed(self):
        analysis, raw = fixture()
        raw["rows"][1]["quad"] = [[150, 230], [240, 230], [240, 250], [150, 250]]
        self.reject(analysis, raw, "no_unique_adjacent_row")

    def test_missing_closer_not_swallowed(self):
        self.reject(*fixture(second="条件x3"), "parenthesis_not_closed")

    def test_ambiguous_adjacent_rows_not_swallowed(self):
        analysis, raw = fixture()
        raw["rows"].append(
            {"text": "另一续行)", "quad": [[100, 120], [145, 120], [145, 140], [100, 140]]}
        )
        self.reject(analysis, raw, "ambiguous_adjacent_band")

    def test_title_not_swallowed(self):
        analysis, raw = fixture()
        analysis["title_ids"] = [1]
        self.reject(analysis, raw, "title_or_footer_boundary")

    def test_footer_not_swallowed(self):
        self.reject(*fixture(second="段位8)"), "title_or_footer_boundary")

    def test_missing_primary_sign_not_guessed(self):
        self.reject(*fixture(first="37.5%甲属性（说明"), "missing_or_multiple_primary_signed_value")

    def test_unknown_complete_base_name_not_trimmed(self):
        self.reject(*fixture(first="+37.5%多余甲属性（说明"), "complete_base_name_not_exact")

    def test_payload_after_closer_not_swallowed(self):
        self.reject(*fixture(second="条件x3)乙属性"), "content_after_parenthesis")


NAMES = {
    "合成暴击类": "crit_chance",
    "合成射速类": "fire_rate",
    "合成伤害类": "damage",
    "合成派系类": "damage_vs_synthetic",
}


def candidate(raw, **flags):
    options = dict(
        source_confirmed=True,
        confirmed_stat_row=True,
        needs_recovery=True,
        already_parsed=False,
        exact_stat=NAMES.get,
        absolute_units=("米", "秒"),
        faction_stat_ids=("damage_vs_synthetic",),
    )
    options.update(flags)
    return propose_prefix_field(raw, [2, 5], **options)


class AnnotationPrefixTests(unittest.TestCase):
    def test_valid_annotation_complete_exact_category(self):
        for name, sid in (
            ("合成暴击类（重击时 x2）", "crit_chance"),
            ("合成射速类(弓类武器效果加倍)", "fire_rate"),
            ("合成 暴击类（重击时 X2）", "crit_chance"),
        ):
            with self.subTest(name=name):
                result = _lookup(name, NAMES.get, lambda _: self.fail("不能调用fuzzy"))
                self.assertEqual(result[0:2], ("exact", sid))
                self.assertEqual(result[3], "known_category_annotation")

    def test_extract_preserves_full_name_and_original_span(self):
        line = "+37.25% 合成暴击类（重击时 x2）"
        name, span, mode = _extract(line)
        self.assertEqual(name, "合成暴击类（重击时 x2）")
        self.assertEqual(line[slice(*span)], name)
        self.assertEqual(mode, "leading_numeric")

    def test_invalid_annotation_neither_exact_nor_fuzzy(self):
        for name in (
            "合成射速类（重击时x2）",
            "合成暴击类（弓类武器效果加倍）",
            "合成暴击类（未知说明）",
            "合成暴击类（重击时x2",
            "合成暴击类重击时x2）",
            "合成暴击类（（重击时x2））",
            "合成暴击类（重击时x2）（重击时x2）",
            "合成暴击类（重击时x2）尾字",
            "未知类别（重击时x2）",
        ):
            with self.subTest(name=name):
                result = _lookup(name, NAMES.get, lambda _: self.fail("坏括注不得模糊补救"))
                self.assertEqual(result[0], "unknown")
                self.assertIsNone(result[1])

    def test_valid_prefix_recovery_is_only_partial_slice(self):
        for tail in ("+37.25% 合成暴击类（重击时 x2）", "-6.75％ 合成射速类（弓类武器效果加倍）"):
            raw = "未知符号 " + tail
            out = candidate(raw)
            self.assertEqual(out["status"], "partial")
            self.assertTrue(out["requires_confirmation"])
            self.assertEqual(out["raw"], raw)
            self.assertEqual(out["prefix"], "未知符号 ")
            self.assertEqual(out["parse_copy"], tail)
            self.assertEqual(out["offset"], len("未知符号 "))
            self.assertEqual(out["source_ids"], [2, 5])
            self.assertTrue(out["annotation_evidence"]["valid"])

    def test_invalid_prefix_annotations_and_external_anchors_rejected(self):
        for raw in (
            "? +8% 合成暴击类（未知说明）",
            "? +8% 合成暴击类（重击时x2",
            "? +8% 合成暴击类（（重击时x2））",
            "? +8% 合成射速类（重击时x2）",
            "? +8% 合成暴击类（重击时x3）",
            "? +8% 合成暴击类（重击时x2）尾字",
            "? -9% +8% 合成暴击类（重击时x2）",
            "? +8 合成暴击类（重击时x2）",
            "? ±8% 合成暴击类（重击时x2）",
            "? x1.1 合成暴击类（重击时x2）",
        ):
            with self.subTest(raw=raw):
                out = candidate(raw)
                self.assertEqual(out["status"], "unchanged")
                self.assertEqual(out["parse_copy"], raw)
                self.assertFalse(out["requires_confirmation"])

    def test_existing_unannotated_prefix_and_fuzzy_behavior_kept(self):
        for text in ("? +4.125% 合成伤害类", "? -3秒 合成伤害类", "? x1.13 合成派系类"):
            self.assertEqual(candidate(text)["status"], "partial")
        self.assertEqual(candidate("+4% 合成伤害类")["status"], "unchanged")
        self.assertEqual(_lookup("旧模糊名", NAMES.get, lambda _: "damage")[0], "fuzzy")
        self.assertEqual(_lookup("合成伤害类", NAMES.get, lambda _: None)[0], "exact")
        self.assertFalse(split_tail_annotation("合成伤害类")["present"])

    def test_confirmation_and_already_parsed_gates_kept(self):
        raw = "? +37.25% 合成暴击类（重击时x2）"
        for flags in (
            {"source_confirmed": False},
            {"confirmed_stat_row": False},
            {"already_parsed": True},
            {"needs_recovery": False},
        ):
            self.assertEqual(candidate(raw, **flags)["status"], "unchanged")


class FooterCompanionTests(unittest.TestCase):
    @staticmethod
    def raw(companion="123", *, companion_y=160, companion_x=240):
        items = [
            ("欧玛 Test", 100, 70, 300),
            ("+10%甲属性", 100, 105, 300),
            ("-20%乙属性", 100, 130, 300),
            ("段位8", 100, 164, 170),
            (companion, companion_x, companion_y, companion_x + 40),
        ]
        rows = [
            {"text": text, "quad": [[x0, y], [x1, y], [x1, y + 20], [x0, y + 20]]}
            for text, x0, y, x1 in items
        ]
        return {"width": 500, "height": 300, "orientation": 1, "rows": rows}

    def test_same_band_rank_number_is_excluded_with_anchor_provenance(self):
        result = analyze_layout(self.raw(), exact_name=exact)
        self.assertEqual(
            result["diagnostics"]["footer_companion_exclusions"],
            [{"row_id": 4, "rank_anchor_id": 3}],
        )
        self.assertEqual(result["diagnostics"]["unresolved_ids"], [])

    def test_signed_or_unit_text_is_not_silently_excluded(self):
        for text in ("+123", "123米", "123属性"):
            with self.subTest(text=text):
                result = analyze_layout(self.raw(text), exact_name=exact)
                self.assertEqual(result["diagnostics"]["footer_companion_exclusions"], [])

    def test_number_before_last_attribute_or_across_column_is_not_excluded(self):
        for raw in (self.raw(companion_y=120), self.raw(companion_x=400)):
            result = analyze_layout(raw, exact_name=exact)
            self.assertEqual(result["diagnostics"]["footer_companion_exclusions"], [])

    def test_missing_ambiguous_or_nonadjacent_rank_anchor_keeps_number_unresolved(self):
        missing = self.raw()
        missing["rows"][3]["text"] = "备注"
        ambiguous = self.raw()
        ambiguous["rows"].append(copy.deepcopy(ambiguous["rows"][3]))
        ambiguous["rows"][-1]["text"] = "段位9"
        for raw in (missing, ambiguous, self.raw(companion_y=154)):
            with self.subTest(raw=raw):
                result = analyze_layout(raw, exact_name=exact)
                self.assertEqual(result["diagnostics"]["footer_companion_exclusions"], [])
                self.assertIn(4, result["diagnostics"]["unresolved_ids"])


class WeaponIdentityEntriesZhTests(unittest.TestCase):
    @staticmethod
    def catalog(entries, by_name, pairs=None):
        return build_catalog(
            {"entries": entries},
            {"by_name": by_name},
            {"pairs": pairs or [["Unrelated", "无关物品"]]},
        )

    def test_unique_concrete_entry_zh_is_exact_with_source(self):
        catalog = self.catalog(
            {"Specific": {"zh": "合成武器", "url_name": "specific", "riven_type": "rifle"}},
            {"Specific": "family-a"},
        )
        result = assess("合成武器", catalog)
        self.assertTrue(result["identity_usable"])
        self.assertEqual(result["candidates"][0]["en"], "Specific")
        self.assertEqual(result["candidates"][0]["name_origins"], ["entries.zh_local_snapshot"])
        self.assertEqual([row["en"] for row in result["riven_entries"]], ["Specific"])

    def test_shared_zh_keeps_all_candidates_and_partial(self):
        catalog = self.catalog(
            {"One": {"zh": "同名武器"}, "Two": {"zh": "同名武器"}},
            {"One": "family-a", "Two": "family-b"},
        )
        result = assess("同名武器", catalog)
        self.assertTrue(result["exact_exists"])
        self.assertFalse(result["identity_usable"])
        self.assertEqual([row["en"] for row in result["candidates"]], ["One", "Two"])

    def test_existing_bilingual_collision_does_not_casefold_english_id(self):
        catalog = self.catalog(
            {"Mk1-Bo": {"zh": "合成MK1武器"}},
            {"Mk1-Bo": "family-a", "MK1-Bo": "family-a"},
            [["MK1-Bo", "合成MK1武器"]],
        )
        result = assess("合成MK1武器", catalog)
        self.assertTrue(result["identity_usable"])
        self.assertEqual([row["en"] for row in result["candidates"]], ["MK1-Bo", "Mk1-Bo"])
        self.assertEqual(result["candidates"][0]["name_origins"], ["official_bilingual_exact_join"])
        self.assertEqual(result["candidates"][1]["name_origins"], ["entries.zh_local_snapshot"])

    def test_module_name_does_not_select_primary_or_secondary(self):
        catalog = self.catalog(
            {"Barrel (Primary)": {"zh": ""}, "Barrel (Secondary)": {"zh": ""}},
            {
                "Barrel": "family-a",
                "Barrel (Primary)": "family-a",
                "Barrel (Secondary)": "family-a",
            },
            [["Barrel", "合成部件"]],
        )
        result = assess("合成部件", catalog)
        self.assertTrue(result["exact_exists"])
        self.assertTrue(result["identity_usable"])
        self.assertEqual(result["candidates"][0]["identity_scope"], "family_or_module_only")
        self.assertEqual(
            {row["en"] for row in result["riven_entries"]},
            {"Barrel (Primary)", "Barrel (Secondary)"},
        )

    def test_foreign_bilingual_name_and_nonexact_query_do_not_expand_domain(self):
        catalog = self.catalog(
            {"Specific": {"zh": "合成武器"}},
            {"Specific": "family-a"},
            [["Foreign", "域外物品"]],
        )
        for name in ("域外物品", "合成武器额外字", " 合成武器"):
            self.assertFalse(assess(name, catalog)["identity_usable"])

    def test_invalid_local_chinese_name_fails_closed(self):
        with self.assertRaises(ValueError):
            self.catalog({"Specific": {"zh": ["损坏"]}}, {"Specific": "family-a"})


class PackageEntryTests(unittest.TestCase):
    @staticmethod
    def raw(lines):
        rows = []
        for index, text in enumerate(["欧玛 Synth-", "suffix", *lines, "段位8"]):
            y = 20 + index * 26
            rows.append(
                {
                    "quad": [[100, y], [340, y], [340, y + 20], [100, y + 20]],
                    "text": text,
                    "score": 0.99,
                }
            )
        return {"width": 500, "height": 400, "orientation": 1, "rows": rows}

    def test_public_entry_preserves_raw_and_core_fields_without_lock_detection(self):
        raw = self.raw(["+88%暴击伤害", "+66%攻击速度", "-27%重击效率"])
        old = copy.deepcopy(raw)
        with patch("socket.socket", side_effect=AssertionError("不得联网")):
            result = analyze_raw(raw)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(raw, old)
        self.assertEqual(result["raw"], old)
        self.assertEqual(result["structured"]["weapon"], "欧玛")
        self.assertEqual(len(result["trace"]["parser_positive"]), 2)
        self.assertEqual(len(result["trace"]["parser_negative"]), 1)
        self.assertEqual(result["raw_icons"], [])
        self.assertEqual(result["icons"], [])
        self.assertEqual(result["associations"], [])
        self.assertEqual(result["trace"]["lock_detection"], "not_requested_weapon_and_stats_only")
        self.assertEqual(result["reasons"], result["recognition_reasons"])
        self.assertEqual(result["OCR_calls"], 0)

    def test_annotation_continuation_keeps_partial_and_provenance(self):
        raw = self.raw(["θ+181.8%暴击几率(重", "击时x2)", "+97%近战伤害", "-43%重击效率"])
        result = analyze_raw(raw)
        self.assertEqual(result["recognition_state"], "partial")
        self.assertEqual(result["trace"]["parenthesized_continuations"][0]["status"], "merged")
        prefix = result["trace"]["prefix_recovery"][0]
        self.assertEqual(prefix["status"], "partial")
        self.assertTrue(prefix["requires_confirmation"])
        self.assertEqual(prefix["prefix"], "θ")
        self.assertEqual(prefix["source_ids"], [2, 3])
        self.assertEqual(result["trace"]["parser_positive"][0], ("crit_chance", 181.8))
        self.assertEqual(result["raw"], raw)

    def test_complete_known_annotation_continuation_can_be_accepted(self):
        raw = self.raw(["+72%射速（弓类武器", "效果加倍）", "+95%多重射击", "-29%装填速度"])
        raw["rows"][0]["text"] = "鳄神 Test"
        result = analyze_raw(raw)
        self.assertEqual(result["recognition_state"], "accepted")
        self.assertEqual(result["candidate"]["diagnostics"]["ambiguities"], [])
        self.assertEqual(result["candidate"]["line_sources"][0]["ids"], [2, 3])

    def test_unknown_annotation_continuation_stays_partial(self):
        raw = self.raw(["+72%射速（未知说明", "效果加倍)", "+95%多重射击", "-29%装填速度"])
        raw["rows"][0]["text"] = "鳄神 Test"
        result = analyze_raw(raw)
        self.assertEqual(result["recognition_state"], "partial")

    def test_unclosed_annotation_continuation_stays_partial(self):
        raw = self.raw(["+72%射速（弓类武器", "效果加倍", "+95%多重射击", "-29%装填速度"])
        raw["rows"][0]["text"] = "鳄神 Test"
        result = analyze_raw(raw)
        self.assertEqual(result["recognition_state"], "partial")
        self.assertEqual(
            result["trace"]["parenthesized_continuations"][0]["reason"],
            "parenthesis_not_closed",
        )

    def test_exact_critical_categories_do_not_cross_stat_identity(self):
        for name, sid in (
            ("暴击几率", "crit_chance"),
            ("暴击伤害", "crit_damage"),
            ("滑行暴击几率", "slide_crit"),
        ):
            with self.subTest(name=name):
                raw = self.raw([f"+88%{name}", "+66%攻击速度", "-27%重击效率"])
                result = analyze_raw(raw)
                self.assertEqual(result["recognition_state"], "accepted")
                self.assertEqual(result["trace"]["parser_positive"][0], (sid, 88.0))
                self.assertEqual(result["raw"], raw)

    def test_unique_typo_and_missing_character_recovery_has_unique_name_evidence(self):
        for name, sid in (
            ("暴击伤售", "crit_damage"),
            ("脆发几率", "status_chance"),
            ("滑行暴击几", "slide_crit"),
        ):
            with self.subTest(name=name):
                raw = self.raw([f"+88%{name}", "+66%攻击速度", "-27%重击效率"])
                result = analyze_raw(raw)
                self.assertEqual(result["recognition_state"], "accepted")
                self.assertEqual(result["trace"]["parser_positive"][0], (sid, 88.0))
                self.assertEqual(result["trace"]["name_evidence"][0]["status"], "normalized")
                self.assertEqual(
                    result["trace"]["name_evidence"][0]["name_match"]["method"], "one_edit"
                )
                self.assertEqual(result["structured"]["lines"][0], f"+88%{name}")
                self.assertEqual(result["raw"], raw)

    def test_cross_stat_equal_distance_is_not_resolved_by_short_critical_alias(self):
        raw = self.raw(["+88%暴击伤率", "+66%攻击速度", "-27%重击效率"])
        result = analyze_raw(raw)
        self.assertNotEqual(result["recognition_state"], "accepted")
        self.assertFalse(any(value == 88.0 for _, value in result["trace"]["parser_positive"]))
        self.assertIsNone(result["trace"]["name_evidence"][0]["stat_id"])
        self.assertEqual(result["raw"], raw)

    def test_long_prefix_suffix_keeps_unique_full_category_and_confirmation(self):
        raw = self.raw(["+88%前置暴击伤害后置", "+66%攻击速度", "-27%重击效率"])
        result = analyze_raw(raw)
        self.assertEqual(result["recognition_state"], "partial")
        self.assertEqual(result["trace"]["parser_positive"][0], ("crit_damage", 88.0))
        self.assertEqual(result["structured"]["lines"][0], "+88%前置暴击伤害后置")
        self.assertEqual(result["raw"], raw)
        ambiguous = self.raw(["+88%暴击几率暴击伤害", "+66%攻击速度", "-27%重击效率"])
        other = analyze_raw(ambiguous)
        self.assertNotEqual(other["recognition_state"], "accepted")
        self.assertFalse(any(value == 88.0 for _, value in other["trace"]["parser_positive"]))

    def test_unknown_unclosed_and_mismatched_annotations_cannot_be_accepted(self):
        for name in (
            "暴击几率（未知说明）",
            "暴击几率（重击时x2",
            "暴击几率重击时x2）",
            "暴击几率（（重击时x2））",
            "暴击伤害（重击时x2）",
            "射速（重击时x2）",
        ):
            with self.subTest(name=name):
                raw = self.raw([f"+88%{name}", "+66%攻击速度", "-27%重击效率"])
                result = analyze_raw(raw)
                self.assertNotEqual(result["recognition_state"], "accepted")
                self.assertEqual(result["raw"], raw)
                self.assertFalse(
                    any(value == 88.0 for _, value in result["trace"]["parser_positive"])
                )

    def test_mixed_width_known_annotation_continuation_can_be_accepted(self):
        raw = self.raw(["+72%射速（弓类武器", "效果加倍)", "+95%多重射击", "-29%装填速度"])
        raw["rows"][0]["text"] = "鳄神 Test"
        result = analyze_raw(raw)
        self.assertEqual(result["recognition_state"], "accepted")
        self.assertEqual(result["raw"], raw)

    def test_faction_x_and_multiplication_sign_remain_independent_values(self):
        for symbol, multiplier, side, value in (
            ("x", "1.48", "parser_positive", 48.0),
            ("×", "1.48", "parser_positive", 48.0),
            ("x", "0.61", "parser_negative", 39.0),
            ("×", "0.61", "parser_negative", 39.0),
        ):
            with self.subTest(symbol=symbol, multiplier=multiplier):
                line = f"{symbol}{multiplier}对Corpus的伤害"
                raw = self.raw([line, "+66%攻击速度", "+44%暴击几率"])
                result = analyze_raw(raw)
                self.assertIn(("damage_vs_corpus", value), result["trace"][side])
                self.assertEqual(result["raw"], raw)
                self.assertEqual(result["structured"]["lines"][0], line)
                self.assertTrue(
                    all(
                        row["parse_copy"] == row["raw"]
                        for row in result["trace"]["prefix_recovery"]
                    )
                )

    def test_unproven_icon_prefix_and_adjacent_numbers_never_gain_exact_acceptance(self):
        for line in (
            "×+88%穿刺伤害",
            "×+88穿刺伤害",
            "××+88%穿刺伤害",
            "x+88%穿刺伤害",
            "×+88%穿刺伤害x1.48",
            "x1.48+88%穿刺伤害",
            "×+88米穿刺伤害",
            "×+8.8.8%穿刺伤害",
        ):
            with self.subTest(line=line):
                raw = self.raw([line, "+66%攻击速度", "-27%重击效率"])
                result = analyze_raw(raw)
                self.assertNotEqual(result["recognition_state"], "accepted")
                self.assertEqual(result["raw"], raw)
                self.assertEqual(result["structured"]["lines"][0], line)
                self.assertTrue(
                    all(
                        row["parse_copy"] == row["raw"]
                        for row in result["trace"]["prefix_recovery"]
                    )
                )

    def test_translation_and_uniform_scale_preserve_semantics_and_source_geometry(self):
        original = self.raw(["+88%暴击伤害", "+66%攻击速度", "-27%重击效率"])
        baseline = analyze_raw(original)
        for scale, dx, dy in ((1.0, 47, 61), (0.7, 0, 0), (1.8, 29, 17)):
            with self.subTest(scale=scale, dx=dx, dy=dy):
                raw = copy.deepcopy(original)
                raw["width"] = int(original["width"] * scale + dx)
                raw["height"] = int(original["height"] * scale + dy)
                for row in raw["rows"]:
                    row["quad"] = [[x * scale + dx, y * scale + dy] for x, y in row["quad"]]
                result = analyze_raw(raw)
                self.assertEqual(result["recognition_state"], baseline["recognition_state"])
                self.assertEqual(result["structured"], baseline["structured"])
                for before, after in zip(
                    baseline["line_geometry"], result["line_geometry"], strict=True
                ):
                    self.assertEqual(after["ids"], before["ids"])
                    self.assertEqual(
                        after["box"],
                        [
                            before["box"][0] * scale + dx,
                            before["box"][1] * scale + dy,
                            before["box"][2] * scale + dx,
                            before["box"][3] * scale + dy,
                        ],
                    )
                self.assertEqual(result["raw"], raw)

    def test_non_target_column_does_not_supply_target_attributes(self):
        raw = self.raw(["+88%暴击伤害", "+66%攻击速度", "-27%重击效率"])
        baseline = analyze_raw(raw)
        for index, text in enumerate(("其他武器", "+13%暴击伤害", "+15%攻击速度")):
            y = 260 + index * 26
            raw["rows"].append(
                {
                    "text": text,
                    "score": 0.99,
                    "quad": [[390, y], [490, y], [490, y + 20], [390, y + 20]],
                }
            )
        result = analyze_raw(raw)
        self.assertEqual(result["recognition_state"], "accepted")
        self.assertEqual(result["structured"], baseline["structured"])
        self.assertEqual(result["raw"], raw)

    def test_peripheral_small_text_never_creates_wrong_acceptance(self):
        # 允许布局保守拒绝；不把当前行高中位数敏感性固化为正确行为。
        for count in (1, 15, 30):
            with self.subTest(count=count):
                raw = self.raw(["+88%暴击伤害", "+66%攻击速度", "-27%重击效率"])
                baseline = analyze_raw(raw)
                for index in range(count):
                    y = 210 + index * 4
                    raw["rows"].append(
                        {
                            "text": "外围说明",
                            "score": 0.99,
                            "quad": [[420, y], [490, y], [490, y + 2], [420, y + 2]],
                        }
                    )
                result = analyze_raw(raw)
                if result["recognition_state"] == "accepted":
                    self.assertEqual(result["structured"], baseline["structured"])
                self.assertTrue(
                    set(result["trace"]["parser_positive"])
                    <= set(baseline["trace"]["parser_positive"])
                )
                self.assertTrue(
                    set(result["trace"]["parser_negative"])
                    <= set(baseline["trace"]["parser_negative"])
                )
                self.assertEqual(result["raw"], raw)

    def test_empty_raw_is_rejected(self):
        result = analyze_raw({"width": 1, "height": 1, "orientation": 1, "rows": []})
        self.assertEqual(result["recognition_state"], "rejected")
        self.assertEqual(result["structured"]["positive"], [])

    def test_raw_schema_rejects_answer_fields(self):
        raw = self.raw(["+88%暴击伤害", "+66%攻击速度", "-27%重击效率"])
        raw["answer"] = {"weapon": "答案不可进解析"}
        with self.assertRaises(ValueError):
            analyze_raw(raw)

    def test_no_ocr_runtime_or_isolation_dependency(self):
        self.assertNotIn("rapidocr", sys.modules)
        self.assertNotIn("onnxruntime", sys.modules)
        package = Path(__file__).resolve().parents[1] / "core" / "local_riven_ocr"
        for path in package.glob("*.py"):
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("/Users/", text)
            self.assertNotIn("from isolation", text)
            self.assertNotIn("sys.path", text)
            self.assertNotIn("subprocess", text)


class AcceptanceEvidenceTests(unittest.TestCase):
    def test_detached_category_fragment_needs_matching_local_reread(self):
        from core.local_riven_ocr.replay_logic import confirm_local_reads

        first = self.result(["+88%暴击几率 击", "时x2)", "+66%攻击速度", "-27%重击效率"])
        self.assertEqual(first["recognition_state"], "partial")
        fragment = first["trace"]["category_fragments"][0]
        self.assertFalse(fragment["missing_text_restored"])
        self.assertEqual(fragment["source_ids"], [2, 3])
        self.assertEqual(first["raw"]["rows"][3]["text"], "时x2)")
        second = self.result(["+88%暴击几率（重击时x2）", "+66%攻击速度", "-27%重击效率"])
        result = confirm_local_reads(first, second)
        self.assertEqual(result["recognition_state"], "accepted")
        self.assertEqual(result["structured"]["lines"][0], "+88%暴击几率")

    def test_category_fragment_cannot_swallow_new_stat_wrong_multiplier_or_unknown_annotation(self):
        from core.local_riven_ocr.replay_logic import confirm_local_reads

        for first, second in (
            ("+88%暴击几率 击", "时x3)"),
            ("+88%暴击几率", "时x2)"),
            ("+88%射速 击", "时x2)"),
            ("+88%暴击几率 击", "+5%时x2)"),
            ("+88%暴击几率（未知说明", "时x2)"),
            ("+88%暴击几率 击", "时x2)另外属性"),
        ):
            with self.subTest(first=first, second=second):
                result = self.result([first, second, "+66%攻击速度", "-27%重击效率"])
                self.assertEqual(result["trace"]["category_fragments"], [])
                self.assertNotEqual(
                    confirm_local_reads(result, copy.deepcopy(result))["recognition_state"],
                    "accepted",
                )

    def test_fragment_requires_adjacent_same_column_sources(self):
        from core.local_riven_ocr.continuation import recover_category_fragments
        from core.local_riven_ocr.base_worker import stat_name_exact

        raw = PackageEntryTests.raw(["+88%暴击几率 击", "时x2)", "+66%攻击速度", "-27%重击效率"])
        layout = analyze_layout(raw, exact_name=stat_name_exact())
        for dx, dy in ((400, 0), (0, 100)):
            altered = copy.deepcopy(raw)
            altered["rows"][3]["quad"] = [[x + dx, y + dy] for x, y in altered["rows"][3]["quad"]]
            _, proposals = recover_category_fragments(layout, altered, stat_name_exact())
            self.assertEqual(proposals, [])

    def result(self, lines=None):
        return analyze_raw(
            PackageEntryTests.raw(
                lines
                or [
                    "0+88%暴击伤害",
                    "+66%攻击速度",
                    "-27%重击效率",
                ]
            )
        )

    def test_matching_second_observation_confirms_prefix_without_changing_raw(self):
        from core.local_riven_ocr.replay_logic import confirm_local_reads

        first = self.result()
        second = self.result(["6+88%暴击伤害", "+66%攻击速度", "-27%重击效率"])
        original = copy.deepcopy(first)
        self.assertEqual(first["recognition_state"], "partial")
        result = confirm_local_reads(first, second)
        self.assertEqual(result["recognition_state"], "accepted")
        self.assertEqual(first, original)
        self.assertEqual(result["raw"], first["raw"])
        self.assertEqual(result["local_verification"]["raw"], second["raw"])

    def test_conflicting_missing_or_unparsed_fields_cannot_confirm_prefix(self):
        from core.local_riven_ocr.replay_logic import confirm_local_reads

        first = self.result()
        for lines in (
            ["+89%暴击伤害", "+66%攻击速度", "-27%重击效率"],
            ["+88%暴击伤害", "+66%攻击速度", "+27%重击效率"],
            ["+88%暴击伤害", "+66%攻击速度"],
            ["+88%暴击伤害", "+66%攻击速度", "-27%重击效率", "+9%未知属性"],
        ):
            with self.subTest(lines=lines):
                second = self.result(lines)
                self.assertEqual(confirm_local_reads(first, second)["recognition_state"], "partial")

    def test_unique_name_edit_has_provenance_but_contained_or_ambiguous_name_does_not_pass(self):
        for name, accepted in (
            ("暴击伤售", True),
            ("超级暴击伤害加成", False),
            ("未知属性", False),
        ):
            result = self.result([f"+88%{name}", "+66%攻击速度", "-27%重击效率"])
            with self.subTest(name=name):
                self.assertEqual(result["recognition_state"] == "accepted", accepted)
                if accepted:
                    evidence = result["trace"]["name_evidence"][0]
                    self.assertEqual(evidence["normalization"], "unique_one_edit")
                    self.assertEqual(evidence["name_match"]["raw_name"], name)
                    self.assertEqual(result["raw"]["rows"][2]["text"], f"+88%{name}")

    def test_prefix_does_not_hide_duplicate_slots_or_ambiguous_annotation(self):
        from core.local_riven_ocr.replay_logic import confirm_local_reads

        for lines in (
            ["0+88%暴击伤害", "+66%攻击速度", "+27%攻击速度"],
            ["0+88%暴击几率（未知说明）", "+66%攻击速度", "-27%重击效率"],
            ["0±88%暴击伤害", "+66%攻击速度", "-27%重击效率"],
        ):
            with self.subTest(lines=lines):
                first = self.result(lines)
                self.assertNotEqual(
                    confirm_local_reads(first, copy.deepcopy(first))["recognition_state"],
                    "accepted",
                )

    def test_resolved_name_only_clears_identity_error(self):
        from core.local_riven_ocr.name_application import apply_name_normalization
        from core import matching, riven_analysis

        catalog = WeaponIdentityEntriesZhTests.catalog(
            {"Synthetic": {"zh": "合成武器", "riven_type": "melee"}},
            {"Synthetic": "family-a"},
        )
        for remaining in ([], ["未解决的 OCR 行：[9]"]):
            result = self.result(["+88%暴击伤害", "+66%攻击速度", "-27%重击效率"])
            result["recognition_state"] = "partial"
            result["recognition_reasons"] = ["OCR 原文武器身份未确认：原字未命中", *remaining]
            normalized = {
                "weapon_canonical": "合成武器",
                "weapon_id": "Synthetic",
                "match_method": "unique_numeric_nearmiss_1",
            }
            with patch(
                "core.local_riven_ocr.name_application.normalize_weapon_name",
                return_value=normalized,
            ):
                result = apply_name_normalization(
                    result, catalog, ra=riven_analysis, matching=matching
                )
            self.assertEqual(result["recognition_reasons"], remaining)
            self.assertEqual(result["recognition_state"], "partial" if remaining else "accepted")

    def test_category_is_transcription_identity_not_a_concrete_weapon(self):
        from core import riven_analysis

        for label in riven_analysis.RIVEN_CATEGORY_LABELS:
            raw = PackageEntryTests.raw(["+88%暴击伤害", "+66%攻击速度", "-27%重击效率"])
            raw["rows"][0]["text"] = label + " Synth"
            result = analyze_raw(raw)
            self.assertEqual(result["recognition_state"], "accepted", label)
            self.assertEqual(result["trace"]["weapon_scope"], "category")
            self.assertFalse(result["trace"]["weapon_identity"]["identity_usable"])


class IdentityAndFooterAcceptanceTests(unittest.TestCase):
    def test_family_identity_is_sufficient_without_concrete_mode(self):
        catalog = build_catalog(
            {"entries": {"Barrel (Primary)": {"zh": ""}, "Barrel (Secondary)": {"zh": ""}}},
            {
                "by_name": {
                    "Barrel": "family-a",
                    "Barrel (Primary)": "family-a",
                    "Barrel (Secondary)": "family-a",
                }
            },
            {"pairs": [["Unrelated", "无关物品"]]},
            {"names": {"barrel": "合成腔体", "unrelated": "域外武器"}},
        )
        result = assess("合成腔体", catalog)
        self.assertTrue(result["identity_usable"])
        self.assertEqual(result["family_keys"], ["family-a"])
        self.assertNotIn("concrete_entry_resolved", result)
        self.assertFalse(assess("域外武器", catalog)["identity_usable"])

    def test_cross_family_collision_is_still_ambiguous(self):
        catalog = WeaponIdentityEntriesZhTests.catalog(
            {"One": {"zh": "同名武器"}, "Two": {"zh": "同名武器"}},
            {"One": "family-a", "Two": "family-b"},
        )
        self.assertFalse(assess("同名武器", catalog)["identity_usable"])

    def test_footer_alphanumeric_is_excluded_only_on_rank_band(self):
        result = analyze_layout(FooterCompanionTests.raw("A7B"), exact_name=exact)
        self.assertEqual(
            result["diagnostics"]["footer_companion_exclusions"],
            [{"row_id": 4, "rank_anchor_id": 3}],
        )
        for text in ("+7", "7m", "7ms", "7米", "7属性"):
            result = analyze_layout(FooterCompanionTests.raw(text), exact_name=exact)
            self.assertEqual(result["diagnostics"]["footer_companion_exclusions"], [], text)
        result = analyze_layout(FooterCompanionTests.raw("A7B", companion_y=120), exact_name=exact)
        self.assertEqual(result["diagnostics"]["footer_companion_exclusions"], [])


class FamilyNameAcceptanceTests(unittest.TestCase):
    @staticmethod
    def catalog(*, direct=False, official=False):
        entries = {
            "Chamber (Primary)": {"zh": "", "riven_type": "kitgun", "group": "primary"},
            "Chamber (Secondary)": {"zh": "", "riven_type": "kitgun", "group": "secondary"},
        }
        families = {en: "family-a" for en in ["Chamber", *entries]}
        names = list(entries) if direct else ["Chamber"]
        return build_catalog(
            {"entries": entries},
            {"by_name": families},
            {
                "pairs": [["Foreign", "域外物品"]]
                if official
                else [[en, "合成腔体"] for en in names]
            },
            {"names": {en.casefold(): "合成腔体" for en in names}} if official else None,
        )

    @staticmethod
    def read(catalog, name="合咸腔体", lines=None):
        raw = PackageEntryTests.raw(
            lines if lines is not None else ["+133.7%暴击伤害", "+177.8%多重射击", "-29.7%装填速度"]
        )
        raw["rows"][0]["text"] = name + " Synth-"
        with patch("core.local_riven_ocr.weapon_identity.load_catalog", return_value=catalog):
            result = analyze_raw(raw)
        assert result["raw"] == raw
        return result

    def test_exact_family_does_not_require_weapon_mode(self):
        result = self.read(self.catalog(), "合成腔体")
        self.assertEqual(result["recognition_state"], "accepted")
        self.assertTrue(result["trace"]["weapon_identity"]["identity_usable"])

    def test_one_edit_family_name_reaches_same_acceptance_as_exact(self):
        result = self.read(self.catalog())
        self.assertEqual(result["recognition_state"], "accepted")
        self.assertEqual(result["structured"]["weapon"], "合成腔体")
        self.assertEqual(result["structured"]["weapon_raw"], "合咸腔体")
        self.assertIsNone(result["structured"]["weapon_id"])
        self.assertEqual(result["trace"]["weapon_identity"]["family_keys"], ["family-a"])

    def test_same_family_entries_are_one_candidate_without_selecting_a_mode(self):
        result = self.read(self.catalog(direct=True))
        self.assertEqual(result["recognition_state"], "accepted")
        self.assertEqual(result["structured"]["weapon"], "合成腔体")
        self.assertIsNone(result["structured"]["weapon_id"])
        self.assertEqual(len(result["trace"]["weapon_name_normalization"]["candidates"]), 1)

    def test_official_aliases_share_the_same_domain_and_correction_rules(self):
        result = self.read(self.catalog(official=True))
        self.assertEqual(result["recognition_state"], "accepted")
        self.assertEqual(result["structured"]["weapon"], "合成腔体")
        self.assertFalse(assess("域外物品", self.catalog(official=True))["identity_usable"])

    def test_cross_family_feasible_or_unknown_candidate_keeps_original(self):
        for riven_type in ("kitgun", ""):
            catalog = self.catalog()
            other = {
                "en": "Other",
                "family_key": "family-b",
                "identity_scope": "riven_entry",
                "riven_type": riven_type,
                "group": "",
                "url_name": "other",
            }
            catalog["records"]["Other"] = other
            catalog["index"]["Other"] = {"Other": ["entries.en"]}
            catalog["index"]["合盛腔体"] = {"Other": ["official_bilingual_exact_join"]}
            result = self.read(catalog)
            self.assertEqual(result["recognition_state"], "partial")
            self.assertEqual(result["structured"]["weapon"], "合咸腔体")

    def test_two_near_spellings_in_one_family_still_need_name_confirmation(self):
        catalog = self.catalog()
        catalog["index"]["合盛腔体"] = {"Chamber": ["official_bilingual_exact_join"]}
        result = self.read(catalog)
        self.assertEqual(result["recognition_state"], "partial")
        self.assertEqual(result["structured"]["weapon"], "合咸腔体")

    def test_impossible_numbers_do_not_prove_a_name(self):
        result = self.read(
            self.catalog(), lines=["+1337%暴击伤害", "+1778%多重射击", "-297%装填速度"]
        )
        self.assertEqual(result["recognition_state"], "partial")
        self.assertEqual(result["structured"]["weapon"], "合咸腔体")

    def test_incomplete_stat_evidence_does_not_prove_a_name(self):
        result = self.read(
            self.catalog(), lines=["+133.7%暴击伤害", "+177.8%未知词条", "-29.7%装填速度"]
        )
        self.assertEqual(result["recognition_state"], "partial")
        self.assertEqual(result["structured"]["weapon"], "合咸腔体")

    def test_family_without_riven_entries_is_unconfirmed(self):
        catalog = self.catalog()
        for en in ("Chamber (Primary)", "Chamber (Secondary)"):
            catalog["records"][en]["identity_scope"] = "family_or_module_only"
        self.assertFalse(assess("合成腔体", catalog)["identity_usable"])
        self.assertEqual(self.read(catalog)["recognition_state"], "partial")

    def test_data_driven_stat_multiplier_is_used_without_name_or_mode_rules(self):
        with patch(
            "core.riven_analysis.weapon_stat_multipliers",
            return_value={"crit_damage": 2.0, "multishot": 2.0},
        ):
            result = self.read(
                self.catalog(), lines=["+267.4%暴击伤害", "+355.6%多重射击", "-29.7%装填速度"]
            )
        self.assertEqual(result["recognition_state"], "accepted")
        self.assertEqual(result["structured"]["weapon"], "合成腔体")


if __name__ == "__main__":
    unittest.main(verbosity=2)
