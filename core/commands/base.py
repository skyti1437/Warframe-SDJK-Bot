# -*- coding: utf-8 -*-
"""指令层公共件：Reply（handler 统一产出）。

自 main.py 迁出（结构优化 D1，2026-09-28）；main.py 顶部 re-export，
既有 ``plugin.Reply`` 等用法不变。子包纪律：不 import astrbot。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ..logging_compat import logger

# 插件根目录（= main.py 所在目录）。commands 域方法体逐字迁自 main.py，其中
# `PLUGIN_DIR / "core" / "data" / ...` 等引用名保持不变；本常量与
# main.py::PLUGIN_DIR 同值（core/commands/base.py 的上两级即插件根），
# 是 commands 子包内的**单一副本**（D3 起随域提供）。
PLUGIN_DIR = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Action:
    """平台无关的后续操作；发送层按平台保留执行、填入与复制的语义。"""

    label: str
    send: str
    mode: str = "run"
    row: int = 0
    owner_only: bool = False


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
    actions: list[Action] = field(default_factory=list)
    markdown: Optional[str] = None  # 显式 QQ Markdown 正文；仅文字分支使用，lines 保留降级正文
    riven_splice: Optional[dict] = None  # 紫卡融合预测契约；展示层按 schema_version 读取


def err_card(reason: str, fix: str, *kv, title: str = "提示") -> Reply:
    """统一报错 / 用法卡片：「◆ 原因」+ 可选 KV 行 +「※ ⚠ 怎么改」。

    kv 每项：``(标签, 内容)`` -> 「◆ 标签：内容」；或已成型的字符串（原样一行，
    如「· 示例 …」「　续行」）。文字降级（-w / render_mode=text）走 text_card，
    原因与怎么改都在。限流 / 并发 / 权限这类高频系统提示不要用它（保持纯文本）。
    """
    lines = [f"◆ 原因：{reason}"]
    for item in kv:
        if isinstance(item, (tuple, list)):
            lines.append(f"◆ {item[0]}：{item[1]}")
        elif item:
            lines.append(str(item))
    lines.append(f"※ ⚠ 怎么改：{fix}")
    return Reply(title, lines)


def validate_numeric_setting(key: str, value: float) -> None:
    """初始化读取和面板保存共用的数值边界；0 的含义按配置项分别保留。"""
    try:
        finite = not isinstance(value, bool) and math.isfinite(value)
    except (TypeError, OverflowError):
        finite = False
    if not finite:
        raise ValueError("应为有限数字")
    minimum = {"page_size": 1, "http_timeout": 1, "push_interval": 15}.get(key)
    if minimum is not None and value < minimum:
        raise ValueError(f"不能小于 {minimum}")
    if key == "text_chunk_limit" and value != 0 and value < 200:
        raise ValueError("应为 0（自动）或不小于 200")


def _num(cfg: dict, key: str, default: float, cast=float) -> float:
    """容错读取配置数值；非有限数或越界值回退默认，合法的 0 语义保留。"""
    raw = cfg.get(key, default)
    if isinstance(raw, str) and not raw.strip():
        return default
    try:
        if isinstance(raw, bool):
            raise ValueError("应为数字")
        value = cast(raw)
        validate_numeric_setting(key, value)
        return value
    except (TypeError, ValueError, OverflowError):
        logger.warning("[sdjk] 配置项 %s=%r 非法，回退默认值 %s", key, raw, default)
        return default
