"""仅供 raw 重放的括号续行候选，不调用 OCR，不改动 raw。

契约：merge_parenthesized_continuations(analysis, raw, exact=None)
返回 (analysis 深拷贝, proposals)。只处理已接受卡区内的既有 line_sources；
合并后的 text 是原 raw 行按来源顺序以空格连接，ids 仍是 raw.rows 数组下标。
exact 若给出，仅检验首个开括号前、原数值之后的完整基础词名，允许空白归一；
该证据不授权删改前缀或给语义层宣称整行 exact。
"""

from __future__ import annotations

import copy
import math
import re
import unicodedata
from statistics import median


def recover_category_fragments(analysis, raw, exact):
    """已知类别说明的断行残片，只产生待复读的核心字段副本。

    主体须完整精确词名，说明须整段命中既有类别说明或仅少一个汉字；
    数字、倍率和符号不得缺失/改写。两行须有相邻同列几何和完整来源，
    原文与未识别的说明字形保留，不宣称恢复了缺失文字。
    """
    from .annotation_fields import CATEGORY_ANNOTATIONS
    from .name_evidence import _extract

    result = copy.deepcopy(analysis)
    lines, sources = result.get("lines", []), result.get("line_sources", [])
    rows = {
        i: {"text": row.get("text", ""), "box": _box(row)}
        for i, row in enumerate(raw.get("rows", []))
        if _box(row)
    }
    if not rows or not result.get("accepted_region") or len(lines) != len(sources):
        return result, []
    height = median(row["box"][3] - row["box"][1] for row in rows.values())
    proposals, deleted = [], set()
    for index in range(1, len(lines)):
        before = index - 1
        if before in deleted or not all(
            _source_valid(sources[i], lines[i], rows) for i in (before, index)
        ):
            continue
        # 已有开括号的路径交给严格括注归并；不能借本分支吞未知说明。
        if any(c in lines[before] for c in "(（)）") or re.search(r"[+＋\-−%％]", lines[index]):
            continue
        name, span, extraction = _extract(lines[before])
        if not name or extraction != "leading_numeric":
            continue
        combined = re.sub(r"\s+", "", unicodedata.normalize("NFKC", name + lines[index])).casefold()
        combined = combined.removesuffix(")")
        matches = set()
        for annotation, sid in CATEGORY_ANNOTATIONS.items():
            variants = {annotation}
            variants.update(
                annotation[:i] + annotation[i + 1 :]
                for i, char in enumerate(annotation)
                if "\u3400" <= char <= "\u9fff"
            )
            for variant in variants:
                if combined.endswith(variant):
                    base = combined[: -len(variant)]
                    if base and exact(base) == sid:
                        matches.add((base, sid, annotation))
        if len(matches) != 1:
            continue
        base_name, sid, annotation = next(iter(matches))
        first = _union([rows[i]["box"] for i in sources[before]["ids"]])
        second = _union([rows[i]["box"] for i in sources[index]["ids"]])
        if not (
            0.45 * height < _cy(second) - _cy(first) <= 1.5 * height
            and _same_column(first, second, height)
        ):
            continue
        ids = sources[before]["ids"] + sources[index]["ids"]
        if len(ids) != len(set(ids)):
            continue
        observed = " ".join(rows[i]["text"].strip() for i in ids)
        parse_copy = lines[before][: span[0]] + base_name
        proposals.append(
            {
                "source_ids": ids,
                "raw_text": observed,
                "parse_copy": parse_copy,
                "stat_id": sid,
                "category_annotation": annotation,
                "requires_confirmation": True,
                "missing_text_restored": False,
            }
        )
        lines[before] = parse_copy
        sources[before] = {
            "ids": ids,
            "text": parse_copy,
            "raw_text": observed,
            "text_kind": "category_fragment_core_fields_only",
        }
        deleted.add(index)
    result["lines"] = [line for i, line in enumerate(lines) if i not in deleted]
    result["line_sources"] = [source for i, source in enumerate(sources) if i not in deleted]
    return result, proposals


_NUMBER = r"\d+(?:[.,]\d+)?"
_SIGNED = re.compile(rf"[+＋\-−–—－\ufe63负]\s*{_NUMBER}\s*[%％]?", re.I)
_VALUE = re.compile(rf"(?:[+＋\-−–—－\ufe63负xX×]\s*{_NUMBER}|{_NUMBER}\s*[xX×])")
_LEADING_MULTIPLIER = re.compile(rf"^\s*[xX×]\s*{_NUMBER}\s*(.*)$")
_TRAILING_MULTIPLIER = re.compile(rf"^(.*?)\s*[xX×]\s*{_NUMBER}\s*[)）]\s*$")
_FOOTER = re.compile(r"^(?:段位|内融|循环次数|容量|极性|确认|取消)")
_LETTER = re.compile(r"[A-Za-z\u3400-\u9fff]")


def _brackets(text):
    """中英文圆括号同型；错配/额外闭括号失败，不推测 OCR 漏字。"""
    depth = 0
    first = None
    closing = None
    for i, char in enumerate(text):
        if char in "(（":
            if first is None:
                first = i
            depth += 1
        elif char in ")）":
            depth -= 1
            if depth < 0:
                return None
            if depth == 0:
                closing = i
    return depth, first, closing


def _box(row):
    try:
        points = row["quad"]
        xs, ys = [float(p[0]) for p in points], [float(p[1]) for p in points]
        if not xs or not all(math.isfinite(v) for v in xs + ys):
            return None
        box = [min(xs), min(ys), max(xs), max(ys)]
        return box if box[2] > box[0] and box[3] > box[1] else None
    except (KeyError, TypeError, ValueError, IndexError):
        return None


def _union(boxes):
    return [
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    ]


def _cy(box):
    return (box[1] + box[3]) / 2


def _same_column(a, b, height):
    overlap = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    width_a, width_b = a[2] - a[0], b[2] - b[0]
    centers = abs((a[0] + a[2] - b[0] - b[2]) / 2)
    return overlap >= 0.5 * min(width_a, width_b) and centers <= max(
        2.2 * height, 0.55 * max(width_a, width_b)
    )


def _exact_name(name, exact):
    if exact is None:
        return None
    sid = exact(name)
    return sid or exact(re.sub(r"\s+", "", name))


def _source_valid(source, line, rows):
    if not isinstance(source, dict) or source.get("text") != line:
        return False
    ids = source.get("ids")
    return (
        isinstance(ids, list)
        and bool(ids)
        and all(type(i) is int and i in rows for i in ids)
        and len(set(ids)) == len(ids)
        and " ".join(rows[i]["text"].strip() for i in ids) == line
    )


def merge_parenthesized_continuations(analysis, raw, exact=None):
    """只在唯一近邻、同列、原括号闭合、无新词条证据时合并。

    首行必须已有唯一带正负符号的数值；没有符号不凭词名猜数值。
    后续行若以独立正负数值开头一律拒绝；倍数只允许留在原括号内，
    且若其后余文可独立 exact 成词条也拒绝。出现标题/页脚、跨列/卡、
    多个近邻同带候选、间距过大或括号仍不闭合时保留原 analysis。
    """
    result = copy.deepcopy(analysis)
    proposals = []
    if not isinstance(analysis, dict) or not analysis.get("accepted_region"):
        return result, proposals
    region = analysis.get("region")
    if not isinstance(region, list) or len(region) != 4:
        return result, proposals
    rows = {}
    for i, row in enumerate(raw.get("rows", [])):
        box = _box(row)
        if box is not None and isinstance(row.get("text"), str) and row["text"].strip():
            rows[i] = {**row, "box": box}
    if not rows:
        return result, proposals
    height = median(row["box"][3] - row["box"][1] for row in rows.values())
    lines = result.get("lines", [])
    sources = result.get("line_sources", [])
    if not isinstance(lines, list) or not isinstance(sources, list) or len(lines) != len(sources):
        return result, proposals
    owners = {}
    for index, source in enumerate(sources):
        if _source_valid(source, lines[index], rows):
            for row_id in source["ids"]:
                owners.setdefault(row_id, []).append(index)
    title_ids = set(analysis.get("title_ids", []))
    deleted = set()
    for index, (line, source) in enumerate(zip(lines, sources)):
        if index in deleted or not _source_valid(source, line, rows):
            continue
        bracket = _brackets(line)
        if bracket is None or bracket[0] != 1 or bracket[1] is None:
            continue
        first = bracket[1]
        outside = line[:first]
        numbers = list(_SIGNED.finditer(outside))
        proposal = {
            "kind": "parenthesized_continuation",
            "line_index": index,
            "before": copy.deepcopy(source),
            "status": "rejected",
        }
        proposals.append(proposal)
        if len(numbers) != 1 or len(list(_VALUE.finditer(outside))) != 1:
            proposal["reason"] = "missing_or_multiple_primary_signed_value"
            continue
        base_name = outside[numbers[0].end() :].strip()
        if not base_name or not _LETTER.search(base_name):
            proposal["reason"] = "missing_base_name"
            continue
        sid = _exact_name(base_name, exact)
        proposal.update(base_name=base_name, base_stat_id=sid)
        if exact is not None and not sid:
            proposal["reason"] = "complete_base_name_not_exact"
            continue
        boxes = [rows[i]["box"] for i in source["ids"]]
        last_y = max(_cy(b) for b in boxes)
        last_band = [b for b in boxes if last_y - _cy(b) <= 0.45 * height]
        last_box = _union(last_band)
        own = set(source["ids"])
        # 先取最近的物理行带再验证内容，不跳过中间的新词条去寻找闭括号。
        nearby = [
            i
            for i, row in rows.items()
            if i not in own
            and 0.45 * height < _cy(row["box"]) - last_y <= 1.5 * height
            and _same_column(last_box, row["box"], height)
        ]
        if not nearby:
            proposal["reason"] = "no_unique_adjacent_row"
            continue
        nearest = min(_cy(rows[i]["box"]) for i in nearby)
        band = [i for i in nearby if abs(_cy(rows[i]["box"]) - nearest) <= 0.45 * height]
        proposal["candidate_ids"] = band
        if len(band) != 1:
            proposal["reason"] = "ambiguous_adjacent_band"
            continue
        row_id = band[0]
        row, box = rows[row_id], rows[row_id]["box"]
        proposal["gap_line_heights"] = (_cy(box) - last_y) / height
        if not (
            region[0] <= box[0] <= box[2] <= region[2]
            and region[1] <= box[1] <= box[3] <= region[3]
        ):
            proposal["reason"] = "outside_accepted_region"
            continue
        footer_y = analysis.get("diagnostics", {}).get("footer_y")
        if (
            row_id in title_ids
            or _FOOTER.search(row["text"].strip())
            or (footer_y is not None and _cy(box) >= footer_y)
        ):
            proposal["reason"] = "title_or_footer_boundary"
            continue
        continuation = row["text"].strip()
        if _SIGNED.search(continuation):
            proposal["reason"] = "independent_signed_attribute"
            continue
        # 完整独立倍数词条不能因为上一行缺右括号而被吞。
        multiplier = _LEADING_MULTIPLIER.fullmatch(continuation)
        trailing_multiplier = _TRAILING_MULTIPLIER.fullmatch(continuation)
        if (multiplier and _LETTER.search(multiplier[1].rstrip(")） ").strip())) or (
            trailing_multiplier and _exact_name(trailing_multiplier[1].strip(), exact)
        ):
            proposal["reason"] = "independent_multiplier_attribute"
            continue
        merged_text = line + " " + continuation
        closed = _brackets(merged_text)
        if closed is None or closed[0] != 0 or closed[2] is None:
            proposal["reason"] = "parenthesis_not_closed"
            continue
        if merged_text[closed[2] + 1 :].strip(" \t,，.。;；"):
            proposal["reason"] = "content_after_parenthesis"
            continue
        row_owners = owners.get(row_id, [])
        if row_owners and (len(row_owners) != 1 or row_owners[0] <= index):
            proposal["reason"] = "ambiguous_source_owner"
            continue
        if row_owners:
            other = row_owners[0]
            if sources[other]["ids"] != [row_id]:
                proposal["reason"] = "continuation_has_other_rows"
                continue
            deleted.add(other)
        merged = {"ids": source["ids"] + [row_id], "text": merged_text}
        lines[index], sources[index] = merged_text, merged
        proposal.update(
            status="merged",
            reason="unique_adjacent_parenthesis_closer",
            continuation={"ids": [row_id], "text": continuation},
            after=merged,
        )
        if row_id in result.get("excluded_ids", []):
            result["excluded_ids"].remove(row_id)
        unresolved = result.get("diagnostics", {}).get("unresolved_ids", [])
        if row_id in unresolved:
            unresolved.remove(row_id)
        ownership = result.get("diagnostics", {}).get("name_ownership", [])
        matching = [
            item
            for item in ownership
            if item.get("target") is None
            and item.get("source", {}).get("ids") == [row_id]
            and item.get("previous_ids") == source["ids"]
        ]
        if len(matching) == 1:
            matching[0]["resolved_by_parenthesized_continuation"] = True
            ambiguities = result.get("diagnostics", {}).get("ambiguities", [])
            if "ambiguous_name_continuation" in ambiguities and all(
                item.get("target") is not None
                or item.get("resolved_by_parenthesized_continuation") is True
                for item in ownership
            ):
                ambiguities.remove("ambiguous_name_continuation")
    result["lines"] = [line for i, line in enumerate(lines) if i not in deleted]
    result["line_sources"] = [source for i, source in enumerate(sources) if i not in deleted]
    return result, proposals
