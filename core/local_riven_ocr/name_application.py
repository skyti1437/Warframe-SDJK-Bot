"""名称归一的唯一接入点：保留raw，canonical只供实际分析字段。"""

import copy

from . import weapon_identity
from .name_gate import make_numeric_gate
from .weapon_name_normalize import normalize_weapon_name


def apply_name_normalization(result, catalog, *, ra, matching):
    raw_name = result["structured"].get("weapon")
    result["structured_before_name_normalization"] = copy.deepcopy(result["structured"])
    result["trace_before_name_normalization"] = copy.deepcopy(result["trace"])
    gate = make_numeric_gate(
        result["trace"], len(result["structured"].get("lines", [])), ra, catalog=catalog
    )
    normalization = normalize_weapon_name(raw_name, catalog, gate, matching=matching)
    candidate = normalization["weapon_canonical"]
    identity = weapon_identity.assess(candidate, catalog)
    adopted = (
        normalization["match_method"] == "unique_numeric_nearmiss_1" and identity["identity_usable"]
    )
    confirmed_id = (
        normalization["weapon_id"]
        if identity["identity_usable"] and (adopted or normalization["match_method"] == "exact")
        else None
    )
    result["structured"].update(
        weapon_raw=raw_name,
        weapon_canonical=candidate if adopted else raw_name,
        weapon_id=confirmed_id,
    )
    result["trace"]["weapon_name_normalization"] = {
        **normalization,
        "canonical_adopted": adopted,
        "canonical_identity": identity,
    }
    result["name_normalized"] = adopted
    if adopted:
        result["structured"]["weapon"] = candidate
        result["trace"]["weapon_raw_identity"] = result["trace"].get("weapon_identity")
        result["trace"].update(
            weapon_identity=identity,
            weapon_lookup_input=candidate,
            weapon_raw_lookup_input=raw_name,
            weapon_exact_exists=identity["identity_usable"],
        )
        # 身份已用原有唯一数值候选规则解决，清除这一个过期错误；其余拦截保留。
        result["recognition_reasons"] = [
            reason
            for reason in result["recognition_reasons"]
            if not reason.startswith("OCR 原文武器身份未确认：")
        ]
        if not result["recognition_reasons"]:
            result["recognition_state"] = "accepted"
            result["action"] = None
    return result
