# -*- coding: utf-8 -*-
"""指令处理器子包（结构优化 D1 起自 main.py 分域迁入，Mixin 挂载）。

纪律：本子包一律不 import astrbot（事件对象鸭子类型；日志统一
``from ..logging_compat import logger``）——由 tests/test_method_contract.py
机器断言防退化（2026-09-28 裁定 B）。
"""
from __future__ import annotations

from .base import Reply
from .dun import DunCommands
from .daily import DailyCommands
from .progress import ProgressCommands
from .arbitration import ArbitrationCommands
from .market import MarketCommands
from .relic import RelicCommands
from .riven import RivenCommands
from .scan import ScanCommands
from .vision import VisionCommands
from .wiki_misc import WikiMiscCommands
from .rotations import RotationCommands

__all__ = ["ArbitrationCommands", "DailyCommands", "MarketCommands", "ProgressCommands", "RelicCommands", "Reply", "RivenCommands", "RotationCommands", "DunCommands", "ScanCommands", "VisionCommands", "WikiMiscCommands"]
