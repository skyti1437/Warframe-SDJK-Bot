"""本地紫卡纯解析入口：只处理武器名与词条，不执行OCR或探测锁图标。

raw 必须来自一次 OCR 的原始 rows/quad/text。本包保留原文和来源；名称纠正
须有唯一家族与完整数值证据，前缀恢复须复读确认，括注须通过逐行语义校验。
raw_icons 固定为空仅表示未探测。
"""

from __future__ import annotations

from .. import matching, riven_analysis
from . import base_worker, weapon_identity
from .name_application import apply_name_normalization
from .replay_logic import replay


def analyze_raw(raw: dict) -> dict:
    """返回 status/structured/trace/recognition_state/reasons/raw 等解析结果。

    本入口没有原图字节，因此不伪造 image_sha256；空元数据不参与解析。
    调用方负责将失败/partial 展示为需人工核对，不可转用云识别兜底。
    """
    catalog = weapon_identity.load_catalog()
    result = replay(
        {"raw": raw, "raw_icons": [], "image_sha256": ""},
        exact=base_worker.stat_name_exact(),
        weapon_catalog=catalog,
    )
    result = apply_name_normalization(result, catalog, ra=riven_analysis, matching=matching)
    result["reasons"] = list(result["recognition_reasons"])
    result["trace"]["lock_detection"] = "not_requested_weapon_and_stats_only"
    return result


__all__ = ["analyze_raw"]
