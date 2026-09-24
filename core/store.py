# -*- coding: utf-8 -*-
"""群级配置与蹲订阅的本地持久化（JSON，原子写）。"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from .parser import TimeWindow


class JsonStore:
    """极简 JSON 存储：加载到内存，保存时先写临时文件再原子替换。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = asyncio.Lock()
        self._data: Any = None
        self.load()

    def load(self) -> None:
        if self.path.exists():
            try:
                self._data = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001 - 损坏则重置
                self._data = None

    async def save(self) -> None:
        async with self._lock:
            tmp = self.path.with_suffix(".tmp")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=1),
                           encoding="utf-8")
            tmp.replace(self.path)


class GroupStore(JsonStore):
    """每个会话（群/私聊，键为 unified_msg_origin）的设置。"""

    DEFAULTS = {"platform": "pc", "push": False, "quiet": False}

    # 用户可见的开关名 -> 存储键。
    # ⚠️ 早期版本把「推送」这个**中文显示名**直接当键写进了存档，
    #    而读取端用的是 `push` —— 于是「.开启 推送」永远读不回来，
    #    「蹲」一直提示"本群推送功能未开启"。
    SWITCH_KEYS = {"推送": "push", "聊天": "quiet"}

    def __init__(self, path: Path, default_platform: str = "pc"):
        super().__init__(path)
        if not isinstance(self._data, dict):
            self._data = {}
        self._default_platform = default_platform

    def get(self, umo: str) -> dict:
        rec = dict(self.DEFAULTS)
        rec["platform"] = self._default_platform
        # 归一化历史脏键（「推送」-> push），否则老存档开了也读不到
        for k, v in (self._data.get(umo) or {}).items():
            rec[self.SWITCH_KEYS.get(k, k)] = v
        return rec

    def platform(self, umo: str) -> str:
        return self.get(umo)["platform"]

    async def set_platform(self, umo: str, platform: str) -> None:
        self._data.setdefault(umo, {})["platform"] = platform
        await self.save()

    async def set_switch(self, umo: str, key: str, value: bool) -> None:
        """写开关。key 必须是存储键（push/quiet），传入显示名会自动归一化。"""
        self._data.setdefault(umo, {})[self.SWITCH_KEYS.get(key, key)] = value
        await self.save()

    def all(self) -> dict:
        return self._data


@dataclass
class Subscription:
    """一条“蹲”订阅。"""

    umo: str                       # 会话标识（推送目标）
    platform: str                  # 订阅平台（推送按平台差量比对）
    event: str                     # 蹲类型：裂隙/夜灵/奸商/突击/执刑官/仲裁/...
    rule: str = ""                 # 筛选规则原文（裂隙筛选等）
    windows: dict = field(default_factory=dict)  # TimeWindow 序列化
    until: float = -1.0            # 过期时间戳；-1 永久
    once: bool = True              # 未写时长 -> 命中一次后取消
    hits_left: Optional[int] = None  # None=不限次数
    created_by: str = ""
    created_at: float = field(default_factory=time.time)
    sid: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    notified: dict = field(default_factory=dict)  # 去重：事件key -> ts

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Subscription":
        flds = {f for f in cls.__dataclass_fields__}  # noqa: C416
        return cls(**{k: v for k, v in d.items() if k in flds})

    def time_window(self) -> TimeWindow:
        w = self.windows or {}
        days = w.get("days")
        return TimeWindow(start=w.get("start"), end=w.get("end"),
                          days=set(days) if days else None,
                          at_hour=w.get("at_hour"))

    def expired(self, now: Optional[float] = None) -> bool:
        if self.until < 0:
            return False
        return (now or time.time()) >= self.until

    def consume(self) -> bool:
        """命中一次；返回是否仍需保留。"""
        if self.hits_left is not None:
            self.hits_left -= 1
            return self.hits_left > 0
        return not self.once


class SubscriptionStore(JsonStore):
    def __init__(self, path: Path):
        super().__init__(path)
        if not isinstance(self._data, list):
            self._data = []

    def all(self) -> list[Subscription]:
        return [Subscription.from_dict(d) for d in self._data]

    def for_umo(self, umo: str) -> list[Subscription]:
        return [s for s in self.all() if s.umo == umo]

    async def add(self, sub: Subscription) -> None:
        self._data.append(sub.to_dict())
        await self.save()

    async def remove(self, predicate) -> int:
        keep = []
        removed = 0
        for d in self._data:
            if predicate(Subscription.from_dict(d)):
                removed += 1
            else:
                keep.append(d)
        if removed:
            self._data = keep
            await self.save()
        return removed

    async def update(self, sub: Subscription) -> None:
        """按 sid 原位回写一条订阅（notified / hits_left 等运行期状态落盘）。"""
        for i, d in enumerate(self._data):
            if d.get("sid") == sub.sid:
                self._data[i] = sub.to_dict()
                break
        else:
            return
        await self.save()

    async def sync(self, subs: list[Subscription]) -> None:
        self._data = [s.to_dict() for s in subs]
        await self.save()

    def gc(self, now: Optional[float] = None) -> bool:
        """清理过期订阅，返回是否有变化。"""
        now = now or time.time()
        before = len(self._data)
        self._data = [d for d in self._data if not Subscription.from_dict(d).expired(now)]
        return len(self._data) != before
