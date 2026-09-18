# -*- coding: utf-8 -*-
"""异步安全的 TTL + LRU 内存缓存。

所有频繁请求的第三方接口（世界状态 / WM 市场 / 物品搜索）都经由本缓存，
以降低对他人服务器的压力，并让重复查询的响应稳定在毫秒级。
"""
from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from typing import Any, Callable, Coroutine, Optional


class TTLCache:
    """带过期时间的 LRU 缓存，读多写少场景下用一把 asyncio 锁保证协程安全。"""

    def __init__(self, maxsize: int = 512):
        self._maxsize = maxsize
        self._store: "OrderedDict[str, tuple[float, Any]]" = OrderedDict()
        self._lock = asyncio.Lock()
        # 同 key 并发回源时做单飞（singleflight），避免缓存击穿
        self._inflight: dict[str, asyncio.Future[Any]] = {}
        self._hits = 0
        self._misses = 0

    def _purge(self) -> None:
        now = time.monotonic()
        expired = [k for k, (exp, _) in self._store.items() if exp <= now]
        for k in expired:
            self._store.pop(k, None)

    async def get(self, key: str) -> Optional[Any]:
        async with self._lock:
            item = self._store.get(key)
            if item is None:
                self._misses += 1
                return None
            exp, value = item
            if exp <= time.monotonic():
                self._store.pop(key, None)
                self._misses += 1
                return None
            self._store.move_to_end(key)
            self._hits += 1
            return value

    async def set(self, key: str, value: Any, ttl: float) -> None:
        async with self._lock:
            self._store[key] = (time.monotonic() + max(0.0, ttl), value)
            self._store.move_to_end(key)
            while len(self._store) > self._maxsize:
                self._store.popitem(last=False)

    async def drop_prefix(self, prefix: str) -> None:
        """按前缀批量失效（例如强制刷新某平台全部世界状态缓存）。"""
        async with self._lock:
            for k in [k for k in self._store if k.startswith(prefix)]:
                self._store.pop(k, None)

    async def get_or_fetch(
        self,
        key: str,
        ttl: float,
        fetch: Callable[[], Coroutine[Any, Any, Any]],
    ) -> Any:
        value = await self.get(key)
        if value is not None:
            return value
        # 单飞：同一 key 的并发请求只回源一次
        async with self._lock:
            fut = self._inflight.get(key)
            if fut is None:
                fut = asyncio.get_running_loop().create_future()
                self._inflight[key] = fut
                creator = True
            else:
                creator = False
        if not creator:
            return await asyncio.shield(fut)
        try:
            value = await fetch()
            await self.set(key, value, ttl)
            if not fut.done():
                fut.set_result(value)
            return value
        except BaseException as exc:  # noqa: BLE001 - 需要唤醒所有等待者
            if not fut.done():
                fut.set_exception(exc)
            raise
        finally:
            async with self._lock:
                self._inflight.pop(key, None)

    def stats(self) -> dict:
        total = self._hits + self._misses
        return {
            "size": len(self._store),
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": round(self._hits / total, 3) if total else 0.0,
        }
