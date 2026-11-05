"""紫卡离线名称身份：同域表精确匹配，只确认紫卡家族，不选择武器模式。

只读随包四个既有快照，译名只与紫卡 entries/by_name 中已有身份 join。
显式主要/次要后缀可以投影为家族显示名，
同名跨家族仍拒绝。英文译名键沿用业务表的小写规范，不向物品域外扩展。
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path


SOURCE_FILES = (
    ("core/data/dispositions_rivenmirror.json", "紫卡运行期合并表的既有离线 entries，含中文名"),
    ("core/data/de/riven_families.json", "官方紫卡家族/模块名称身份"),
    ("core/data/de/name_bilingual.json", "DE 同键双语原名，仅与上述英文身份精确 join"),
    ("core/data/de/name_en_zh.json", "业务层同源官方译名；仅 join 已有紫卡身份"),
)


def load_catalog(source_root=None):
    """缺失、拒读或结构损坏均保留为不可用证据，不换域、不联网兜底。"""
    source_root = (
        Path(source_root) if source_root is not None else Path(__file__).resolve().parents[2]
    )
    documents, sources, errors = [], [], []
    for relative, purpose in SOURCE_FILES:
        path = Path(source_root) / relative
        evidence = {"path": relative, "purpose": purpose, "sha256": None}
        try:
            content = path.read_bytes()
            evidence["sha256"] = hashlib.sha256(content).hexdigest()
            document = json.loads(content)
            if not isinstance(document, dict):
                raise ValueError("表根必须为对象")
            documents.append(document)
        except (OSError, ValueError, TypeError) as exc:
            documents.append(None)
            errors.append(f"{relative}: {type(exc).__name__}")
        sources.append(evidence)
    if errors:
        return {"available": False, "sources": sources, "errors": errors}
    try:
        return build_catalog(*documents, sources=sources)
    except (TypeError, ValueError) as exc:
        return {
            "available": False,
            "sources": sources,
            "errors": [f"权威表结构不可用: {type(exc).__name__}: {exc}"],
        }


def build_catalog(dispositions, families, bilingual, official=None, *, sources=()):
    """为可重跑合成测试开放纯数据入口；生产只经 load_catalog 读既有快照。"""
    entries, by_name, pairs = (
        dispositions.get("entries"),
        families.get("by_name"),
        bilingual.get("pairs"),
    )
    if not isinstance(entries, dict) or not entries:
        raise ValueError("entries 缺失或为空")
    if not isinstance(by_name, dict) or not by_name:
        raise ValueError("by_name 缺失或为空")
    if not isinstance(pairs, list) or not pairs:
        raise ValueError("pairs 缺失或为空")
    if any(
        not isinstance(en, str) or not en or not isinstance(entry, dict)
        for en, entry in entries.items()
    ) or any(
        not isinstance(en, str) or not en or not isinstance(root, str) or not root
        for en, root in by_name.items()
    ):
        raise ValueError("紫卡条目/家族身份字段损坏")
    if any(
        not isinstance(pair, list)
        or len(pair) != 2
        or not all(isinstance(name, str) and name for name in pair)
        for pair in pairs
    ):
        raise ValueError("双语原名对损坏")
    records = {}
    for en in sorted(set(entries) | set(by_name)):
        entry = entries.get(en)
        records[en] = {
            "en": en,
            "family_key": by_name.get(en),
            "identity_scope": "riven_entry" if entry is not None else "family_or_module_only",
            "url_name": entry.get("url_name") if entry is not None else None,
            "riven_type": entry.get("riven_type") if entry is not None else None,
            "group": entry.get("group") if entry is not None else None,
            "unverified_entries_zh": entry.get("zh") if entry is not None else None,
        }
    index = {}

    def add(name, en, origin):
        if isinstance(name, str) and name:
            index.setdefault(name, {}).setdefault(en, []).append(origin)

    for en, record in records.items():
        add(en, en, "entries.en" if en in entries else "by_name.en")
        if en in entries:
            zh = entries[en].get("zh")
            if zh is not None and not isinstance(zh, str):
                raise ValueError("entries.zh 不是字符串")
            # 只绑定本条具体 entry；不把家族/模块名推成某一形态。
            # 来源与官方双语 join 分开记，冲突由 assess 保留全部候选。
            add(zh, en, "entries.zh_local_snapshot")
    for en, zh in pairs:
        if en in records:
            add(zh, en, "official_bilingual_exact_join")
    if official is not None:
        names = official.get("names")
        if not isinstance(names, dict) or not all(
            isinstance(en, str) and isinstance(zh, str) for en, zh in names.items()
        ):
            raise ValueError("官方译名表结构损坏")
        for en in records:
            add(names.get(en.casefold()), en, "official_name_exact_join")
    # 卡面不印模块的主要/次要模式；只补同一家族的显示名，不选择模式。
    for name, identities in list(index.items()):
        base = re.sub(r"\s*[（(](?:主要|次要|Primary|Secondary)[）)]\s*$", "", name)
        if base != name:
            for en, origins in identities.items():
                add(base, en, "explicit_mode_display_name")
                for origin in origins:
                    if origin in ("official_bilingual_exact_join", "official_name_exact_join"):
                        add(base, en, origin)
    return {
        "available": True,
        "sources": list(sources),
        "errors": [],
        "records": records,
        "index": index,
    }


def identity_key(record):
    """同一家族的目录记录共享紫卡身份；无家族表映射时只认本条完整记录。"""
    family = record.get("family_key")
    return ("family", family) if family else ("entry", record["en"])


def assess(name, catalog):
    """保留原始命中与家族条目证据；具体模式不参与 OCR 名称采信。"""
    result = {
        "input": name,
        "status": "partial",
        "exact_exists": False,
        "identity_usable": False,
        "sources": catalog.get("sources", []),
        "candidates": [],
        "family_keys": [],
        "riven_entries": [],
    }
    if not catalog.get("available"):
        result["reason"] = "紫卡身份权威快照不可用；保留 partial"
        result["source_errors"] = catalog.get("errors", [])
        return result
    if not isinstance(name, str) or not name:
        result["reason"] = "未提供完整原文武器名"
        return result
    hits = catalog["index"].get(name, {})
    if not hits:
        result["reason"] = "完整原名未在紫卡同域条目中文名/官方双语别名中精确命中"
        return result
    result["exact_exists"] = True
    records = catalog["records"]
    result["candidates"] = [
        {**records[en], "name_origins": sorted(set(origins))}
        for en, origins in sorted(hits.items())
    ]
    result["family_keys"] = sorted(
        {records[en]["family_key"] for en in hits if records[en]["family_key"]}
    )
    keys = {identity_key(records[en]) for en in hits}
    result["riven_entries"] = [
        dict(record)
        for record in records.values()
        if identity_key(record) in keys and record["identity_scope"] == "riven_entry"
    ]
    if len(keys) != 1:
        result["reason"] = "完整原名对应多个紫卡家族或未关联条目；名称仍有歧义"
        return result
    if not result["riven_entries"]:
        result["reason"] = "名称已在家族表中，但缺少关联紫卡条目证据"
        return result
    result.update(
        status="exact",
        identity_usable=True,
        reason="原文完整名对应唯一紫卡家族或完整条目；无需选择武器模式",
    )
    return result
