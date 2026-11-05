"""名称候选的离线RA回调；直接使用本次解析槽位/magnitude，不读GT。"""

import math
from . import weapon_identity


def make_numeric_gate(trace, line_count, ra, *, catalog):
    positive = trace.get("parser_positive", [])
    negative = trace.get("parser_negative", [])
    hidden = trace.get("hidden", 0)
    values = list(positive) + list(negative)
    complete = (
        trace.get("parser_legal") is True
        and not trace.get("notes")
        and trace.get("semantic_coverage_complete") is True
        and len(values) + hidden == line_count
        and bool(values)
        and len({sid for sid, _ in values}) == len(values)
        and all(
            isinstance(sid, str)
            and sid in ra._BASE
            and not isinstance(value, bool)
            and isinstance(value, (int, float))
            and math.isfinite(value)
            and value > 0
            for sid, value in values
        )
    )
    counts = ra.count_candidates(len(positive), len(negative), hidden) if complete else []

    def numeric_gate(record):
        if not complete or not counts:
            return {"feasible": None, "reason": "完整卡面数值/槽位证据不足"}
        identity = weapon_identity.assess(record.get("en"), catalog)
        if not identity["identity_usable"]:
            return {"feasible": None, "reason": "紫卡名称身份尚未确认", "identity": identity}
        # 完整条目保留自己的类别/机制；家族名检查关联条目的可行性，不选择任何模式。
        entries = (
            [record] if record["identity_scope"] == "riven_entry" else identity["riven_entries"]
        )
        checks = []
        for entry in entries:
            riven_type, group = entry.get("riven_type") or "", entry.get("group") or ""
            if ra.class_estimated(riven_type, group):
                checks.append({"feasible": None, "reason": "类别未知，不能用步枪估算证明唯一"})
                continue
            # 既有纯函数只按 riven_type 确定组合枪紫卡基值；不传入武器名或推断模式。
            cls = ra.kitgun_mode_class("", ra.weapon_class(riven_type, group), riven_type)
            multipliers = ra.weapon_stat_multipliers(entry)
            feasible_counts = [
                list(count)
                for count in counts
                if all(ra._base_value(sid, cls) for sid, _ in values)
                and ra.disp_feasible(
                    positive,
                    negative,
                    cls,
                    ra.UNIFIED_DISPOSITION,
                    counts=count,
                    stat_multipliers=multipliers,
                )
            ]
            checks.append(
                {
                    "feasible": bool(feasible_counts),
                    "weapon_class": cls,
                    "stat_multipliers": multipliers,
                    "feasible_counts": feasible_counts,
                }
            )
        feasible = (
            True
            if any(c["feasible"] is True for c in checks)
            else None
            if not checks or any(c["feasible"] is None for c in checks)
            else False
        )
        return {
            "feasible": feasible,
            "disposition": ra.UNIFIED_DISPOSITION,
            "count_candidates": [list(count) for count in counts],
            "checks": checks,
            "source": "本次trace槽位与RA纯函数，非GT",
        }

    return numeric_gate
