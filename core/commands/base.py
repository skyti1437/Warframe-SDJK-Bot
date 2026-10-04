# -*- coding: utf-8 -*-
"""指令层公共件：Reply（handler 统一产出）。

自 main.py 迁出（结构优化 D1，2026-09-28）；main.py 顶部 re-export，
既有 ``plugin.Reply`` 等用法不变。子包纪律：不 import astrbot。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ..logging_compat import logger

# 插件根目录（= main.py 所在目录）。commands 域方法体逐字迁自 main.py，其中
# `PLUGIN_DIR / "core" / "data" / ...` 等引用名保持不变；本常量与
# main.py::PLUGIN_DIR 同值（core/commands/base.py 的上两级即插件根），
# 是 commands 子包内的**单一副本**（D3 起随域提供）。
PLUGIN_DIR = Path(__file__).resolve().parents[2]


@dataclass
class Reply:
    """handler 的统一产出。"""

    title: str = ""
    lines: list[str] = field(default_factory=list)
    footer: str = ""
    whisper: list[str] = field(default_factory=list)  # -r 生成的密语文本
    raw_text: Optional[str] = None  # 直接输出纯文本（wiki/管理类）
    text_only: bool = False  # 强制不适配图片
    extra_text: str = ""  # 卡片之外的补充文本（如 wiki 链接）
    pages: list[tuple[str, list[str]]] = field(  # 多页卡片：[(标题, 行), …]
        default_factory=list
    )  # 优先于 title/lines；渲染层自动加页码


def _num(cfg: dict, key: str, default: float, cast=float) -> float:
    """面板数值容错读取（BUG-2，2026-09-18 验收）。

    面板 schema 只在 UI 层约束类型，用户手改配置 JSON 可绕过 —— 原来直接
    float()/int() 会让插件 __init__ 抛 ValueError 加载失败。非法值回退默认
    并记 warning；空串 / None 同样视为未填写（回退默认）；0 是合法值
    （scan_cooldown=0 关闭限制等语义不受影响）。
    """
    raw = cfg.get(key, default)
    if isinstance(raw, str) and not raw.strip():
        return default
    try:
        return cast(raw)
    except (TypeError, ValueError):
        logger.warning("[sdjk] 配置项 %s=%r 非法，回退默认值 %s", key, raw, default)
        return default
