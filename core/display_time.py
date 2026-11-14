"""统一用户看到的时刻和订阅窗口时区。"""

from __future__ import annotations

import os
import re
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT = "system"
_setting: ContextVar[timezone | None] = ContextVar("sdjk_display_timezone", default=None)
_OFFSET = re.compile(r"UTC([+-])(\d{2}):([0-5]\d)")


def _fixed_zone(value: object) -> timezone | None:
    match = _OFFSET.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        return None
    sign, hours, minutes = match.groups()
    total = (int(hours) * 60 + int(minutes)) * (1 if sign == "+" else -1)
    if -12 * 60 <= total <= 14 * 60:
        return timezone(timedelta(minutes=total))
    return None


@contextmanager
def use_zone(value: object):
    token = _setting.set(_fixed_zone(value))
    try:
        yield
    finally:
        _setting.reset(token)


def local_datetime(value: datetime | float) -> datetime:
    if not isinstance(value, datetime):
        value = datetime.fromtimestamp(value, tz=timezone.utc)
    elif value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    # 系统时区用该时刻的本地规则，不能把今天的偏移用于其他日期。
    return value.astimezone(_setting.get())


def offset_label(value: datetime) -> str:
    offset = value.strftime("%z")
    return "UTC" if offset == "+0000" else f"UTC{offset[:3]}:{offset[3:5]}"


def format_time(value: datetime | float | str | None, pattern: str = "%m-%d %H:%M") -> str:
    """ISO 时间或 Unix 秒转为带 UTC 偏移的可读时刻。旧数据无偏移时按 UTC 读。"""
    try:
        if isinstance(value, str):
            value = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
        if value is None:
            return ""
        local = local_datetime(value)
    except (ValueError, OverflowError, OSError, TypeError):
        return ""
    return f"{local:{pattern}} {offset_label(local)}"


def _system_zone_name(now: datetime) -> str:
    configured = os.environ.get("TZ")
    if configured is not None:
        name = configured.removeprefix(":") or "UTC"
        candidates = [name.partition("/zoneinfo/")[2] if name.startswith("/") else name]
    else:
        try:
            candidates = [str(Path("/etc/localtime").resolve()).partition("/zoneinfo/")[2]]
        except (OSError, RuntimeError):
            candidates = []
        try:
            candidates.append(Path("/etc/timezone").read_text(encoding="utf-8").strip())
        except (OSError, UnicodeError):
            pass
    for name in filter(None, candidates):
        try:
            zone = ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            continue
        samples = [now + timedelta(days=days) for days in (0, 90, 180, 270)]
        if all(d.astimezone(zone).utcoffset() == d.astimezone().utcoffset() for d in samples):
            return name
    return ""


def current_zone_label() -> str:
    """时间窗使用的固定偏移，或 AstrBot 运行环境的时区。"""
    now = datetime.now(timezone.utc)
    system = _system_zone_name(now) if _setting.get() is None else ""
    return system or offset_label(local_datetime(now))


def panel_timezone(value: object) -> dict[str, str | int]:
    """浏览器用宿主的 IANA 时区，缺少可识别名称时用宿主当前 UTC 偏移。"""
    now = datetime.now(timezone.utc)
    fixed = _fixed_zone(value)
    offset = now.astimezone(fixed).utcoffset()
    return {
        "name": _system_zone_name(now) if fixed is None else "",
        "offset_minutes": int(offset.total_seconds() // 60),
    }
