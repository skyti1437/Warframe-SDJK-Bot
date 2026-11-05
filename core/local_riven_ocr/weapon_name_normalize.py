"""离线紫卡名称候选：保留原文，按家族去重，由外部数值回调判可行性。"""

from __future__ import annotations

import re

from .weapon_identity import identity_key


def _shape(name, matching):
    """只比较标识，不剥掉标识匹配；沿用现有 matching 的变体词表。"""
    low = name.casefold()
    tokens = set(matching.VARIANT_TOKENS) | set(matching._EN_TOKENS)
    markers = tuple(
        sorted(
            (token, edge)
            for token in tokens
            for edge, hit in (("prefix", low.startswith(token)), ("suffix", low.endswith(token)))
            if hit
        )
    )
    brackets = tuple(re.findall(r"[（(][^）)]*[）)]", name))
    return markers, brackets, matching.prime_base(name) is not None


def normalize_weapon_name(raw, catalog, numeric_gate, *, matching):
    """numeric_gate(record) 返回 bool/None 或含 feasible 的数值证据 dict。

    matching 为现有纯 core.matching 模块。精确命中优先；近名仅使用冻结
    catalog.index 中官方译名来源的中文名。同家族不同条目不制造歧义；
    不同名称写法、跨家族歧义或数值证据不足均保留原文，不推导武器模式。
    """
    result = {
        "weapon_raw": raw,
        "weapon_canonical": raw,
        "weapon_id": None,
        "match_method": "unresolved",
        "candidates": [],
        "reason": "未找到可唯一确认的名称",
    }
    if not catalog.get("available"):
        result["reason"] = "名称库不可用"
        return result
    if not isinstance(raw, str) or not raw:
        result["reason"] = "名称为空"
        return result
    index, records = catalog["index"], catalog["records"]
    if raw in index:
        hits = sorted(index[raw])
        result["candidates"] = [
            {
                "weapon_id": en,
                "names": [raw],
                "record": dict(records[en]),
                "numeric": {"feasible": None, "reason": "精确命中无需数值改名"},
            }
            for en in hits
        ]
        result["match_method"] = "exact"
        result["reason"] = "完整原名精确命中，保持原样"
        if len(hits) == 1 and records[hits[0]]["identity_scope"] == "riven_entry":
            result["weapon_id"] = hits[0]
        return result
    if len(raw) < 2:
        return result
    near = {}
    for name, identities in sorted(index.items()):
        if (
            not re.search(r"[\u3400-\u9fff]", name)
            or len(name) != len(raw)
            or sum(a != b for a, b in zip(raw, name)) != 1
        ):
            continue
        for en, origins in identities.items():
            if {"official_bilingual_exact_join", "official_name_exact_join"}.intersection(origins):
                near.setdefault(identity_key(records[en]), {}).setdefault(en, []).append(name)
    raw_shape = _shape(raw, matching)
    for key, members in sorted(near.items()):
        names = sorted({name for member_names in members.values() for name in member_names})
        compatible = [name for name in names if _shape(name, matching) == raw_shape]
        evidence = {"feasible": None, "reason": "变体或括号标识不同"}
        checks = []
        if compatible:
            for en, member_names in sorted(members.items()):
                if not set(member_names).intersection(compatible):
                    continue
                try:
                    verdict = numeric_gate(dict(records[en]))
                    check = dict(verdict) if isinstance(verdict, dict) else {"feasible": verdict}
                    if check.get("feasible") is not True and check.get("feasible") is not False:
                        check["feasible"] = None
                except Exception as exc:
                    check = {"feasible": None, "reason": f"数值校验异常: {type(exc).__name__}"}
                checks.append({"entry": en, **check})
            evidence = {
                "feasible": (
                    True
                    if any(c["feasible"] is True for c in checks)
                    else None
                    if not checks or any(c["feasible"] is None for c in checks)
                    else False
                ),
                "checks": checks,
            }
        unique_entry = next(iter(members)) if len(members) == 1 else None
        result["candidates"].append(
            {
                "weapon_id": unique_entry
                if unique_entry and records[unique_entry]["identity_scope"] == "riven_entry"
                else None,
                "family_key": key[1] if key[0] == "family" else None,
                "names": names,
                "compatible_names": compatible,
                "records": [dict(records[en]) for en in sorted(members)],
                "numeric": evidence,
            }
        )
    eligible = [c for c in result["candidates"] if c["compatible_names"]]
    passed = [c for c in eligible if c["numeric"]["feasible"] is True]
    unknown = [c for c in eligible if c["numeric"]["feasible"] is None]
    if len(passed) == 1 and not unknown and len(passed[0]["compatible_names"]) == 1:
        chosen = passed[0]
        result.update(
            weapon_canonical=chosen["compatible_names"][0],
            weapon_id=chosen["weapon_id"],
            match_method="unique_numeric_nearmiss_1",
            reason="完整一字近名候选中，家族及名称唯一、数值可行且没有未知候选",
        )
    elif result["candidates"]:
        result["reason"] = "候选名称或家族有歧义、数值不可行/未知或变体标识不兼容，保持原样"
    return result
