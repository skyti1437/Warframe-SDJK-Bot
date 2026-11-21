"""聊天指令共用的有界先到先算队列。"""

from __future__ import annotations

import asyncio
from collections import deque
from contextlib import asynccontextmanager

from .i18n import L
from .logging_compat import logger


class QueueUnavailable(Exception):
    """队列已满或插件正在关闭。"""


class RequestQueue:
    def __init__(self, workers: int, max_waiting: int):
        self.workers = workers
        self.max_waiting = max_waiting
        self.active = 0
        self._waiting: deque[object] = deque()
        self._changed = asyncio.Event()
        self._closed = False

    def is_waiting(self, key: object) -> bool:
        return key in self._waiting

    @property
    def needs_slot(self) -> bool:
        return bool(self._waiting) and self.active < self.workers and not self._closed

    def wake(self) -> None:
        self._changed.set()

    def close(self) -> None:
        self._closed = True
        self.wake()

    def _check_open(self) -> None:
        if self._closed:
            raise QueueUnavailable(
                L("队列正在关闭，请稍后再试", "Queue is shutting down. Try again shortly.")
            )

    @asynccontextmanager
    async def slot(self, notify=None, *, room=None, key=None):
        """等到队首且有空位才放行。room 可检查和其他任务共享的额外并发预算。"""

        def can_start():
            return self.active < self.workers and (room is None or room())

        self._check_open()
        notice = None
        try:
            if self._waiting or not can_start():
                if len(self._waiting) >= self.max_waiting:
                    raise QueueUnavailable(
                        L("排队已满，请稍后再试", "Queue is full. Try again shortly.")
                    )
                ticket = key if key is not None else object()
                self._waiting.append(ticket)
                self.wake()
                try:
                    if notify is not None:
                        notice = asyncio.create_task(notify(len(self._waiting)))
                    while not self._closed and (self._waiting[0] is not ticket or not can_start()):
                        self._changed.clear()
                        await self._changed.wait()
                    self._check_open()
                finally:
                    self._waiting.remove(ticket)
                    self.wake()

            self.active += 1
            try:
                yield
            finally:
                self.active -= 1
                self.wake()
            if notice is not None:
                await notice
        finally:
            if notice is not None and not notice.done():
                notice.cancel()
                await asyncio.gather(notice, return_exceptions=True)


async def send_queue_position(event, bridge, label: str, position: int) -> None:
    """排位通知在后台发送；发送失败或超时不影响排队和计算。"""
    send = getattr(event, "send", None)
    plain_result = getattr(event, "plain_result", None)
    if not callable(send) or not callable(plain_result):
        return
    text = L(
        f"⏳ {label}已排队，入队时第 {position} 位，轮到后自动处理",
        f"⏳ {label} queued at position {position}. It will start automatically.",
    )

    async def deliver():
        literal = getattr(bridge, "send_literal", None)
        if callable(literal):
            bridge.prepare_event(event)
            if await literal(event, text):
                return
        message = plain_result(text)
        markdown = getattr(message, "use_markdown", None)
        if callable(markdown):
            markdown(False)
        await send(message)

    try:
        await asyncio.wait_for(deliver(), timeout=2)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[sdjk] 排位提示发送失败（%s）", type(exc).__name__)
