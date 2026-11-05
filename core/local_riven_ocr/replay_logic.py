"""纯raw重新布局与原业务解析；前缀只生成来源可追溯的待确认解析副本。"""

import copy
import math

from . import base_worker as base
from .layout_candidate import analyze
from .prefix_field import propose_prefix_field
from .continuation import merge_parenthesized_continuations, recover_category_fragments


def validate_input(payload):
    if not isinstance(payload, dict) or set(payload) != {"raw", "raw_icons", "image_sha256"}:
        raise ValueError("重放输入只允许raw/检测几何/原图SHA")
    raw = payload["raw"]
    if not isinstance(raw, dict) or set(raw) != {"width", "height", "orientation", "rows"}:
        raise ValueError("raw字段不符合原始OCR白名单")
    if not isinstance(raw["rows"], list) or payload["raw_icons"] != []:
        raise ValueError("原始rows/icons必须为列表")
    return payload


def source_confirmed(raw, analysis, source, line):
    ids = source.get("ids", [])
    region = analysis.get("region")
    if (
        not analysis.get("accepted_region")
        or not isinstance(region, list)
        or len(region) != 4
        or not ids
        or len(ids) != len(set(ids))
    ):
        return False
    if any(type(index) is not int or not 0 <= index < len(raw["rows"]) for index in ids):
        return False
    rows = [raw["rows"][index] for index in ids]
    if (
        " ".join(str(row.get("text", "")).strip() for row in rows) != line
        or source.get("text") != line
    ):
        return False
    for row in rows:
        bounds = base.pixel_box(row)
        if (
            not bounds
            or not all(math.isfinite(value) for value in bounds)
            or not (
                region[0] <= bounds[0] <= bounds[2] <= region[2]
                and region[1] <= bounds[1] <= bounds[3] <= region[3]
            )
        ):
            return False
    return True


def recover_sources(raw, analysis, semantics, exact):
    derived = copy.deepcopy(analysis)
    proposals = []
    evidence = semantics.get("name_evidence", [])
    for index, line in enumerate(analysis.get("lines", [])):
        source = analysis["line_sources"][index]
        parsed = evidence[index].get("core_parse", {}) if index < len(evidence) else {}
        already = len(parsed.get("positive", [])) + len(parsed.get("negative", [])) == 1
        candidate = propose_prefix_field(
            line,
            source.get("ids", []),
            source_confirmed=source_confirmed(raw, analysis, source, line),
            confirmed_stat_row=analysis.get("accepted_region") is True,
            needs_recovery=not already,
            already_parsed=already,
            exact_stat=exact,
            absolute_units=("米", "秒", "m", "s"),
            faction_stat_ids=tuple(
                sid
                for sid in (
                    "damage_vs_corpus",
                    "damage_vs_grineer",
                    "damage_vs_infested",
                    "damage_vs_orokin",
                    "damage_vs_scaldra",
                    "damage_vs_techrot",
                )
            ),
        )
        proposals.append(candidate)
        if candidate["status"] == "partial":
            derived["lines"][index] = candidate["parse_copy"]
            derived["line_sources"][index].update(
                text=candidate["parse_copy"],
                raw_text=line,
                unparsed_prefix=candidate["prefix"],
                parse_copy_offset=candidate["offset"],
                text_kind="derived_parse_copy_not_OCR_raw",
            )
    return derived, proposals


def replay(payload, *, exact, weapon_catalog):
    validate_input(payload)
    raw = copy.deepcopy(payload["raw"])
    analysis = analyze(raw, exact_name=exact)
    analysis_before_continuation = copy.deepcopy(analysis)
    analysis, continuations = merge_parenthesized_continuations(analysis, raw, exact=exact)
    analysis, fragments = recover_category_fragments(analysis, raw, exact)
    original_structured, original_semantics = base.project_semantics(analysis, weapon_catalog)
    derived, proposals = recover_sources(raw, analysis, original_semantics, exact)
    recovered = sum(item["status"] == "partial" for item in proposals)
    structured, semantics = (
        base.project_semantics(derived, weapon_catalog)
        if recovered
        else (original_structured, original_semantics)
    )
    raw_icons = copy.deepcopy(payload["raw_icons"])
    # 运行期只识别武器名和词条；不探测或推断锁图标，空列表不代表“卡面无锁”。
    icons, decisions = [], []
    geometry = base.line_geometry(raw, derived.get("line_sources", []), structured["lines"])
    state, reasons = base.recognition_state(derived, structured, semantics, geometry, decisions)
    evidence = semantics.get("name_evidence", [])
    unconfirmed_continuation = any(proposal.get("status") != "merged" for proposal in continuations)
    for proposal in continuations:
        if proposal.get("status") != "merged":
            continue
        matched = [
            row
            for row in evidence
            if row.get("source_ids") == proposal.get("after", {}).get("ids")
            and any(
                source.get("ids") == row.get("source_ids")
                and source.get("text") == row.get("source_text")
                and source.get("raw_text", source.get("text"))
                == proposal.get("after", {}).get("text")
                for source in derived.get("line_sources", [])
            )
            and row.get("status") == "exact"
            and row.get("normalization") == "known_category_annotation"
            and row.get("stat_id") == proposal.get("base_stat_id")
            and row.get("source_valid") is True
            and row.get("semantic_matched") is True
            and row.get("semantic_output_matched") is True
        ]
        if len(matched) != 1:
            unconfirmed_continuation = True
    if unconfirmed_continuation:
        state = "partial" if analysis.get("accepted_region") else "rejected"
        reasons.append("括号说明归并缺少唯一已知类别、精确词名或逐行来源语义证据")
    if recovered:
        state = "partial"
        reasons.append("未知前缀仅为解析副本候选，原文/来源/offset均保留；需要确认")
    if fragments:
        state = "partial"
        reasons.append("类别说明残片已归类，核心字段仍需卡区复读一致后采信")
    parsed_count = len(semantics.get("parser_positive", [])) + len(
        semantics.get("parser_negative", [])
    )
    if parsed_count != len(structured["lines"]):
        state = "partial" if analysis.get("accepted_region") else "rejected"
        reasons.append("解析词条数量与来源词条行数量不一致，保留缺项/重复冲突")
    return {
        "status": "ok",
        "raw": raw,
        "structured": structured,
        "candidate": analysis,
        "trace": {
            **derived,
            **semantics,
            "prefix_recovery": proposals,
            "parenthesized_continuations": continuations,
            "category_fragments": fragments,
            "analysis_before_continuation": analysis_before_continuation,
            "original_semantics_before_recovery": original_semantics,
            "source_text_kind": "原始raw保留，解析副本及prefix/offset另列",
        },
        "raw_icons": raw_icons,
        "icons": icons,
        "associations": [],
        "line_geometry": geometry,
        "recognition_state": state,
        "recognition_reasons": reasons,
        "prefix_recovered_lines": recovered,
        "original_structured_before_recovery": original_structured,
        "action": None if state == "accepted" else "本地结果需核对，请补清晰原图或文字输入。",
        "OCR_calls": 0,
        "handler_executed": False,
    }


def _signature(result):
    """完整保留词条顺序、正负和数值；不拼接两次观测。"""
    trace = result.get("trace", {})
    return (
        result.get("structured", {}).get("weapon"),
        tuple(tuple(pair) for pair in trace.get("parser_positive", [])),
        tuple(tuple(pair) for pair in trace.get("parser_negative", [])),
    )


def _confirmable(result):
    """复读只能解除前缀这一类悬而未决的证据，不能跳过字段和布局检查。"""
    if result.get("status") != "ok":
        return False
    trace = result.get("trace", {})
    state, _ = base.recognition_state(
        trace, result.get("structured", {}), trace, result.get("line_geometry", []), []
    )
    if state != "accepted":
        return False
    return not any(
        reason.startswith(("括号说明归并缺少", "解析词条数量"))
        for reason in result.get("recognition_reasons", [])
    )


def confirm_local_reads(primary, secondary):
    """整图与同一卡区复读均有完整字段且一致，才解除未知图标前缀的人工确认。"""
    result = copy.deepcopy(primary)
    evidence = {
        "kind": "same_image_card_crop",
        "raw": secondary.get("raw"),
        "structured": secondary.get("structured"),
        "state": secondary.get("recognition_state"),
        "reasons": secondary.get("recognition_reasons"),
        "primary_complete": _confirmable(primary),
        "secondary_complete": _confirmable(secondary),
        "fields_agree": _signature(primary) == _signature(secondary),
    }
    result["local_verification"] = evidence
    if all(evidence[k] for k in ("primary_complete", "secondary_complete", "fields_agree")):
        result["recognition_state"] = "accepted"
        result["recognition_reasons"] = [
            "整图与卡区复读的武器、逐行类型、正负和数值一致；来源校验通过"
        ]
        result["reasons"] = list(result["recognition_reasons"])
        result["action"] = None
    elif result.get("recognition_state") == "accepted":
        result["recognition_state"] = "partial"
        result["recognition_reasons"] = ["本地复读存在未确认字段或两次字段不一致"]
        result["reasons"] = list(result["recognition_reasons"])
        result["action"] = "请核对原图或改用文字输入。"
    return result
