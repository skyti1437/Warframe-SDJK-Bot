"""纯解析副本候选：不改原行/字形/数值，不执行OCR，不自动确认结果。"""

import re

from .annotation_fields import split_tail_annotation


# 只定位显式极性与数值；不解释逗号/小数点，也不读前缀的字符内容。
ANCHOR = re.compile(r"[+\-−负x×]\s*[0-9]+(?:[.,][0-9]+)*")


def propose_prefix_field(
    raw,
    source_ids,
    *,
    source_confirmed,
    confirmed_stat_row,
    needs_recovery,
    already_parsed,
    exact_stat,
    absolute_units=(),
    faction_stat_ids=(),
):
    """exact_stat(完整类别名) -> SID或None；不允许模糊/别名猜测回调。

    absolute_units由既有单位表提供，x/×本身是倍率单位，且仅准入派系SID。
    调用方必须保留原解析和该候选来源；partial不能作为已确认字段。
    """
    result = {
        "status": "unchanged",
        "reason": None,
        "raw": raw,
        "parse_copy": raw,
        "prefix": "",
        "offset": 0,
        "source_ids": list(source_ids),
        "stat_id": None,
        "requires_confirmation": False,
    }

    def stop(reason):
        result["reason"] = reason
        return result

    if already_parsed or not needs_recovery:
        return stop("recovery_not_needed")
    if (
        not source_confirmed
        or not confirmed_stat_row
        or not source_ids
        or any(type(i) is not int or i < 0 for i in source_ids)
    ):
        return stop("unconfirmed_stat_row_source")
    annotation = split_tail_annotation(raw)
    if annotation["present"] and not annotation["valid"]:
        return stop("invalid_or_unknown_annotation")
    # 仅已知完整尾部说明可以暂不参与anchor计数；随后仍核对完整类别SID。
    numeric_body = annotation["base"] if annotation["present"] else raw
    anchors = list(ANCHOR.finditer(numeric_body))
    # ±自身不能证明单一极性；保持原文，不把它拆成+/-。
    if "±" in raw or len(anchors) != 1:
        return stop("no_unique_explicit_polarity_number")
    anchor = anchors[0]
    if not raw[: anchor.start()].strip():
        return stop("no_prefix")
    suffix = numeric_body[anchor.end() :].strip()
    is_multiplier = raw[anchor.start()] in "x×"
    if is_multiplier:
        name = suffix
    else:
        units = sorted(set(("%", "％", *absolute_units)), key=len, reverse=True)
        unit = next((unit for unit in units if unit and suffix.startswith(unit)), None)
        if unit is None:
            return stop("no_explicit_unit")
        name = suffix[len(unit) :].strip()
    if not name:
        return stop("missing_complete_stat_name")
    stat_id = exact_stat(name)
    if not isinstance(stat_id, str) or not stat_id:
        return stop("complete_stat_name_not_exact")
    if annotation["present"] and stat_id != annotation["required_sid"]:
        return stop("annotation_category_mismatch")
    if is_multiplier and stat_id not in faction_stat_ids:
        return stop("multiplier_requires_faction_stat")
    offset = anchor.start()
    result.update(
        status="partial",
        reason="prefix_candidate_requires_confirmation",
        parse_copy=raw[offset:],
        prefix=raw[:offset],
        offset=offset,
        stat_id=stat_id,
        requires_confirmation=True,
    )
    if annotation["present"]:
        result["annotation_evidence"] = annotation
    return result
