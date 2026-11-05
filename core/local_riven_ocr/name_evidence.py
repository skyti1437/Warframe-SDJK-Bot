"""逐行词名证据；仅接受锚定句法中的完整词名整表命中。

本层不改 OCR 行、不截词库子串。允许业务层全词表唯一的一次编辑匹配，
保留匹配证据；歧义与包含匹配不放行。回调使用插件现有词条解析入口。
"""

from __future__ import annotations

import re

from .annotation_fields import exact_annotated_name


_NUMBER = r"\d+(?:[.,]\d+)?"
_UNIT = r"[%\uff05米秒ms]?"
# 证据层不猜测任意前导符号是不是图标：解释不了的前缀交给 partial。
_DECORATION = r"\s*"
_POLARITY = r"[+＋\-−–—－]|负|[x×]"
_SIGN = r"[+＋\-−–—－]"
_LEADING = re.compile(
    rf"^(?P<decoration>{_DECORATION})(?P<polarity>{_POLARITY})\s*"
    rf"(?P<number>{_NUMBER})\s*(?P<unit>{_UNIT})\s*(?P<name>\S.*?)\s*$",
    re.I,
)
_TRAILING = re.compile(
    rf"^(?P<decoration>{_DECORATION})(?P<name>\S.*?)\s*"
    rf"(?P<polarity>{_SIGN})\s*(?P<number>{_NUMBER})\s*(?P<unit>{_UNIT})\s*$",
    re.I,
)
_TRAILING_MULTIPLIER = re.compile(
    rf"^(?P<decoration>{_DECORATION})(?P<name>\S.*?)\s*"
    rf"(?:[x×]\s*(?P<multiplier_a>{_NUMBER})|(?P<multiplier_b>{_NUMBER})\s*[x×])\s*$",
    re.I,
)


def _extract(line):
    """只取数值边界之外的完整原文片段；失败时保留 unknown。"""
    if not isinstance(line, str):
        return None, None, "unknown"
    for label, pattern in (
        ("leading_numeric", _LEADING),
        ("trailing_numeric", _TRAILING),
        ("trailing_multiplier", _TRAILING_MULTIPLIER),
    ):
        match = pattern.fullmatch(line)
        if not match:
            continue
        start, end = match.span("name")
        while start < end and line[start].isspace():
            start += 1
        while start < end and line[end - 1].isspace():
            end -= 1
        if start < end:
            return line[start:end], [start, end], label
    return None, None, "unknown"


def _source(source, line):
    if not isinstance(source, dict):
        return None, [], False
    text = source.get("text")
    ids = source.get("ids")
    valid_ids = (
        isinstance(ids, list)
        and bool(ids)
        and all(type(value) is int and value >= 0 for value in ids)
        and len(ids) == len(set(ids))
    )
    return text, list(ids) if isinstance(ids, list) else [], bool(text == line and valid_ids)


def _lookup(name, exact, fuzzy):
    if name is None:
        return "unknown", None, None, None
    annotation, annotated_sid, base_name = exact_annotated_name(name, exact)
    if annotation["present"]:
        if annotated_sid:
            return "exact", annotated_sid, base_name, "known_category_annotation"
        # 未知/未闭合/嵌套/错配说明不能通过泛删括号或模糊匹配变成exact。
        return "unknown", None, name, "rejected_category_annotation"
    # 仅空白归一；不删字符、数字、汉字、图标，也不从词库找子串。
    compact = re.sub(r"\s+", "", name)
    for candidate, normalization in ((name, "none"), (compact, "whitespace_only")):
        if candidate and (normalization == "none" or candidate != name):
            sid = exact(candidate)
            if sid:
                return "exact", sid, candidate, normalization
    sid = fuzzy(name)
    return ("fuzzy", sid, name, "none") if sid else ("unknown", None, name, "none")


def assess_lines(lines, line_sources, semantics, *, exact, fuzzy, parse, render, one_edit=None):
    """返回每个分析行的原文词名、来源、整名命中及逐行语义对拍。

    `parse(line)` 返回冻结核心的 (positive, negative, notes)；`render` 使用同一
    核心的展示投影。这里逐个位置比对，不把同值不同位置的行互相抵消。
    """
    lines = lines if isinstance(lines, list) else []
    sources = line_sources if isinstance(line_sources, list) else []
    pos = semantics.get("parser_positive", []) if isinstance(semantics, dict) else []
    neg = semantics.get("parser_negative", []) if isinstance(semantics, dict) else []
    shown_pos = semantics.get("structured_positive", []) if isinstance(semantics, dict) else []
    shown_neg = semantics.get("structured_negative", []) if isinstance(semantics, dict) else []
    expected = {
        "positive": pos if isinstance(pos, list) else [],
        "negative": neg if isinstance(neg, list) else [],
    }
    shown = {
        "positive": shown_pos if isinstance(shown_pos, list) else [],
        "negative": shown_neg if isinstance(shown_neg, list) else [],
    }
    cursor = {"positive": 0, "negative": 0}
    result = []
    for index, line in enumerate(lines):
        source_text, source_ids, source_valid = _source(
            sources[index] if index < len(sources) else None, line
        )
        name, span, extraction = _extract(line)
        status, sid, lookup_name, normalization = _lookup(name, exact, fuzzy)
        name_match = None
        if status == "fuzzy" and one_edit is not None:
            name_match = one_edit(name)
            if name_match.get("method") == "one_edit" and name_match.get("sid") == sid:
                status, normalization = "normalized", "unique_one_edit"
                lookup_name = name_match["matched_name"]
        parsed_positive, parsed_negative, notes = parse(line)
        parsed_positive = list(parsed_positive or [])
        parsed_negative = list(parsed_negative or [])
        notes = list(notes or [])
        pairs = [("positive", pair) for pair in parsed_positive] + [
            ("negative", pair) for pair in parsed_negative
        ]
        pair = None
        semantic_matched = False
        semantic_output_matched = False
        if len(pairs) == 1 and not notes:
            side, candidate = pairs[0]
            if isinstance(candidate, (list, tuple)) and len(candidate) == 2:
                parsed_sid, value = candidate
                position = cursor[side]
                pair = [side, parsed_sid, value]
                semantic_matched = (
                    sid == parsed_sid
                    and position < len(expected[side])
                    and list(expected[side][position]) == [parsed_sid, value]
                )
                if semantic_matched and position < len(shown[side]):
                    semantic_output_matched = shown[side][position] == render(
                        side, parsed_sid, value
                    )
                cursor[side] += 1
        result.append(
            {
                "index": index,
                "line": line,
                "source_text": source_text,
                "source_ids": source_ids,
                "source_valid": source_valid,
                "name": name,
                "name_span": span,
                "lookup_name": lookup_name,
                "normalization": normalization,
                "name_match": name_match,
                "extraction": extraction,
                "status": status,
                "stat_id": sid,
                "core_parse": {
                    "positive": parsed_positive,
                    "negative": parsed_negative,
                    "notes": notes,
                },
                "pair": pair,
                "semantic_matched": semantic_matched,
                "semantic_output_matched": semantic_output_matched,
            }
        )
    return result


def coverage_complete(evidence, semantics):
    """每行恰好一个 pair 且语义与展示列表均没有多出的尾项。"""
    if not isinstance(evidence, list) or not isinstance(semantics, dict):
        return False
    positive = semantics.get("parser_positive")
    negative = semantics.get("parser_negative")
    shown_positive = semantics.get("structured_positive")
    shown_negative = semantics.get("structured_negative")
    if not all(
        isinstance(value, list) for value in (positive, negative, shown_positive, shown_negative)
    ):
        return False
    return (
        len(evidence) == len(positive) + len(negative)
        and len(positive) == len(shown_positive)
        and len(negative) == len(shown_negative)
        and all(row["semantic_matched"] and row["semantic_output_matched"] for row in evidence)
    )
