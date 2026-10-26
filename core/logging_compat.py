# -*- coding: utf-8 -*-
"""日志出口（AstrBot 插件规范：日志器只能从 ``astrbot.api.logger`` 获取）。

规范要求插件**不得**自行创建日志器（标准库 logging 模块的 getLogger），
日志统一走 AstrBot 提供的日志器（``main.py`` 同样如此）。本模块把这件事
收敛到一处，core 各模块从这里导入，避免每处各写一遍。

脱离 AstrBot 运行时（离线单元测试、独立脚本）退化为静默日志 —— 既不引入
``logging`` 依赖，也不影响模块的可导入性（本仓库的离线测试大量直接 import core）。
"""

from __future__ import annotations

try:
    from astrbot.api import logger
except ImportError:  # pragma: no cover - 仅在无 astrbot 环境（离线测试/脚本）触发

    class _SilentLogger:
        """与 astrbot.api.logger 同接口的最小子集：丢弃日志。"""

        def debug(self, *args, **kwargs) -> None: ...

        def info(self, *args, **kwargs) -> None: ...

        def warning(self, *args, **kwargs) -> None: ...

        def warn(self, *args, **kwargs) -> None: ...

        def error(self, *args, **kwargs) -> None: ...

        def critical(self, *args, **kwargs) -> None: ...

        def exception(self, *args, **kwargs) -> None: ...

        def log(self, *args, **kwargs) -> None: ...

    logger = _SilentLogger()


def mask_id(s: str) -> str:
    """号码打码：保留前 2 位与后 2 位（``123456789`` → ``12*****89``）。"""
    s = str(s or "")
    if len(s) <= 4:
        return "*" * len(s)
    return s[:2] + "*" * (len(s) - 4) + s[-2:]


def mask_umo(umo: str) -> str:
    """``平台:消息类型:会话号`` 只给会话号打码（日志 / 面板共用；2026-10-25 Astra 评估 S3）。"""
    parts = str(umo or "").split(":")
    if len(parts) >= 3:
        return ":".join(parts[:-1] + [mask_id(parts[-1])])
    return mask_id(umo)


__all__ = ["logger", "mask_id", "mask_umo"]
