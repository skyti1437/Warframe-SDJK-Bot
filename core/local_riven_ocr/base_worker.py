"""本地紫卡 raw 的同源语义、来源几何与采信状态；无OCR/进程/网络入口。"""

from __future__ import annotations

import json
import math

from . import name_evidence, weapon_identity


def pixel_box(row):
    points = row.get("quad", [])
    if not points or any(len(point) != 2 for point in points):
        return None
    xs, ys = zip(*points)
    if not all(isinstance(value, (int, float)) and math.isfinite(value) for value in (*xs, *ys)):
        return None
    box = [min(xs), min(ys), max(xs), max(ys)]
    return box if box[2] > box[0] and box[3] > box[1] else None


def line_geometry(raw, sources, lines):
    result = []
    rows = raw.get("rows", [])
    for index, text in enumerate(lines):
        source = sources[index] if index < len(sources) else {}
        ids = source.get("ids", []) if isinstance(source, dict) else source
        ids = ids if isinstance(ids, list) else []
        boxes = [
            pixel_box(rows[value])
            for value in ids
            if isinstance(value, int) and 0 <= value < len(rows)
        ]
        boxes = [box for box in boxes if box is not None]
        box = (
            [
                min(b[0] for b in boxes),
                min(b[1] for b in boxes),
                max(b[2] for b in boxes),
                max(b[3] for b in boxes),
            ]
            if boxes
            else None
        )
        result.append(
            {
                "index": index,
                "line_index": index,
                "text": text,
                "ids": ids,
                "box": box,
                "source_boxes": boxes,
            }
        )
    return result


def stat_name_exact():
    """布局与 V3 词名闸门共用既有完整词名表；不经过 fuzzy。"""
    from ..commands.vision import VisionCommands as VC
    from ..parser import RIVEN_STAT_ZH

    rev = {name: sid for sid, name in RIVEN_STAT_ZH.items()}
    return lambda name: VC._stat_id_exact(name, rev)


def project_semantics(analysis, weapon_catalog=None):
    # 仅调用既有解析纯函数；不创建宿主、渠道或请求。
    from .. import riven_analysis as ra
    from ..commands.vision import VisionCommands as VC
    from ..parser import RIVEN_STAT_ZH

    lines = list(analysis.get("lines") or [])
    positive, negative, legal, notes = VC._riven_lines_legal(lines)
    identity = weapon_identity.assess(
        analysis.get("weapon"),
        weapon_catalog if weapon_catalog is not None else weapon_identity.load_catalog(),
    )
    structure = {
        "weapon": analysis.get("weapon"),
        "lines": lines,
        "positive": [
            [RIVEN_STAT_ZH.get(sid, sid), -value if ra.is_inverted(sid) else value]
            for sid, value in positive
        ],
        "negative": [
            [RIVEN_STAT_ZH.get(sid, sid), value if ra.is_inverted(sid) else -value]
            for sid, value in negative
        ],
    }
    semantics = {
        "parser_positive": positive,
        "parser_negative": negative,
        "parser_legal": bool(legal),
        "notes": notes,
        "hidden": ra.hidden_line_count(lines),
        "semantic_projection": "同一次 OCR 原文行",
        "weapon_lookup_input": analysis.get("weapon"),
        # 这里只确认紫卡名称；词条数值分析留给业务层，无需识别武器模式。
        "weapon_exact_exists": (
            identity["identity_usable"] or analysis.get("weapon") in ra.RIVEN_CATEGORY_LABELS
        ),
        "weapon_scope": "category"
        if analysis.get("weapon") in ra.RIVEN_CATEGORY_LABELS
        else "weapon",
        "weapon_identity": identity,
        "weapon_validation": "既有紫卡 entries + 官方家族/译名同域匹配；未运行真实 handler",
    }
    rev = {name: sid for sid, name in RIVEN_STAT_ZH.items()}

    def exact(name):
        return VC._stat_id_exact(name, rev)

    def fuzzy(name):
        return VC._stat_id_from_name(name, rev)

    def one_edit(name):
        evidence = {}
        VC._stat_id_from_name(name, rev, evidence=evidence)
        return evidence

    evidence_input = {
        **semantics,
        "structured_positive": structure["positive"],
        "structured_negative": structure["negative"],
    }
    evidence = name_evidence.assess_lines(
        lines,
        analysis.get("line_sources"),
        evidence_input,
        exact=exact,
        fuzzy=fuzzy,
        one_edit=one_edit,
        parse=lambda line: ra.parse_riven_lines([line], fuzzy, exact),
        render=lambda side, sid, value: [
            RIVEN_STAT_ZH.get(sid, sid),
            (-value if ra.is_inverted(sid) else value)
            if side == "positive"
            else (value if ra.is_inverted(sid) else -value),
        ],
    )
    semantics["name_evidence"] = evidence
    semantics["semantic_coverage_complete"] = name_evidence.coverage_complete(
        evidence, evidence_input
    )
    return structure, semantics


def recognition_state(analysis, structured, semantics, geometry, decisions):
    reasons = []
    if not analysis.get("accepted_region"):
        reasons.append("卡区未确认：" + str(analysis.get("reason") or "没有可靠候选"))
        return "rejected", reasons
    if not structured.get("weapon"):
        reasons.append("确认卡区内未提取到武器标题")
    elif not semantics.get("weapon_exact_exists"):
        identity = semantics.get("weapon_identity") or {}
        reasons.append(
            "OCR 原文武器身份未确认：" + str(identity.get("reason") or "未精确命中唯一离线紫卡条目")
        )
    if not structured.get("lines"):
        reasons.append("确认卡区内未提取到词条行")
    if not semantics.get("parser_legal"):
        reasons.append("同源词条合法性校验未通过")
    if semantics.get("notes"):
        reasons.append("词条解析仍有备注：" + "；".join(map(str, semantics["notes"])))
    evidence = semantics.get("name_evidence")
    lines = structured.get("lines", [])
    if semantics.get("semantic_coverage_complete") is not True:
        reasons.append("词条证据未逐行覆盖冻结语义及展示输出")
    if not isinstance(evidence, list) or len(evidence) != len(lines):
        reasons.append("词条行缺少逐原文完整词名证据")
    else:
        used_source_ids = set()
        for index, (line, row) in enumerate(zip(lines, evidence, strict=True)):
            span = row.get("name_span") if isinstance(row, dict) else None
            source_ids = row.get("source_ids") if isinstance(row, dict) else None
            valid_span = (
                isinstance(span, list)
                and len(span) == 2
                and all(type(value) is int for value in span)
                and 0 <= span[0] < span[1] <= len(line)
                and line[span[0] : span[1]] == row.get("name")
            )
            valid_source = (
                isinstance(source_ids, list)
                and bool(source_ids)
                and all(type(value) is int and value >= 0 for value in source_ids)
                and len(set(source_ids)) == len(source_ids)
                and not used_source_ids.intersection(source_ids)
                and row.get("source_valid") is True
                and row.get("source_text") == line
                and index < len(geometry)
                and geometry[index].get("ids") == source_ids
                and geometry[index].get("text") == line
            )
            if source_ids and isinstance(source_ids, list):
                used_source_ids.update(value for value in source_ids if type(value) is int)
            if not (
                isinstance(row, dict)
                and type(row.get("index")) is int
                and row["index"] == index
                and row.get("line") == line
                and valid_span
                and valid_source
                and row.get("status") in {"exact", "normalized"}
                and isinstance(row.get("stat_id"), str)
                and bool(row["stat_id"])
                and isinstance(row.get("pair"), list)
                and len(row["pair"]) == 3
                and row["pair"][1] == row["stat_id"]
                and row.get("semantic_matched") is True
                and row.get("semantic_output_matched") is True
            ):
                reasons.append(f"第 {index + 1} 条词名或语义对应证据不满足整名唯一匹配")
    diagnostics = analysis.get("diagnostics", {})
    for field, label in (("unresolved_ids", "未解决的 OCR 行"), ("ambiguities", "布局歧义")):
        if field not in diagnostics:
            reasons.append("布局分析未提供" + label + "的显式状态")
        elif diagnostics[field]:
            reasons.append(label + "：" + json.dumps(diagnostics[field], ensure_ascii=False))
    if len(geometry) != len(structured.get("lines", [])) or any(not row["box"] for row in geometry):
        reasons.append("至少一条词条缺少可回溯的像素几何")
    if any(row["in_region"] and row["line_index"] is None for row in decisions):
        reasons.append("同卡区锁图标仍有词条归属歧义")
    if reasons:
        return "partial", reasons
    return "accepted", ["卡区、标题、词条合法性与来源几何均通过本地结构校验；未运行真实 handler"]
