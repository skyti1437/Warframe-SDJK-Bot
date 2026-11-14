# -*- coding: utf-8 -*-
"""群级配置与蹲订阅的本地持久化（JSON，原子写）。"""

from __future__ import annotations

import asyncio
import copy
import json
import math
import os
import time
import uuid
from bisect import bisect_left
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from .logging_compat import logger
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
                # utf-8-sig：用户用记事本等手改存档会带 BOM，按 utf-8 读会被当坏文件隔离（GPT 评估 V03）
                self._data = json.loads(self.path.read_text(encoding="utf-8-sig"))
            except Exception as exc:  # noqa: BLE001 - 损坏：先留底再按空处理
                self._data = None
                self._quarantine(f"JSON 解析失败：{exc!r}")

    def _quarantine(self, reason: str) -> None:
        """把坏文件改名为 ``<原名>.bak-<时间戳>`` 并 WARNING，再由调用方按空处理。

        旧实现静默按空处理 ⇒ 下一次 save 直接覆盖原文件，全部订阅/群设置无声丢光、
        也没有任何可追溯的备份。
        """
        try:
            bak = self.path.with_name(f"{self.path.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
            n = 1
            while bak.exists():  # 同秒多次损坏不互相覆盖
                bak = self.path.with_name(
                    f"{self.path.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}-{n}"
                )
                n += 1
            self.path.rename(bak)
            logger.warning(
                "[warframe] 存储文件 %s 损坏（%s）→ 已备份为 %s，按空数据继续",
                self.path,
                reason,
                bak,
            )
        except OSError as exc:
            logger.warning(
                "[warframe] 存储文件 %s 损坏（%s），且备份失败：%s；按空数据继续",
                self.path,
                reason,
                exc,
            )

    async def save(self) -> None:
        async with self._lock:
            # 仅在事件循环内复制：后台序列化期间，调用方仍可修改内存中的嵌套记录。
            snapshot = copy.deepcopy(self._data)
            writer = asyncio.create_task(asyncio.to_thread(self._write_atomic, snapshot))
            cancelled = False
            while not writer.done():
                try:
                    await asyncio.shield(writer)
                except asyncio.CancelledError:
                    # to_thread 取消不了已运行的线程；包括重复取消在内，都等写盘收口再放锁。
                    cancelled = True
                except Exception:
                    break  # 下方统一提取线程异常，避免后台异常无人读取。
            if cancelled:
                # 取消优先向调用方传播；线程即使写失败，也已完成临时文件清理。
                try:
                    writer.result()
                except Exception:
                    pass
                raise asyncio.CancelledError
            writer.result()

    def _write_atomic(self, snapshot: Any) -> None:
        """在线程内完成序列化和原子写；同一实例由 save 的锁串行保护。"""
        tmp = self.path.with_suffix(".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(snapshot, ensure_ascii=False, indent=1)
            # 先写盘并 fsync，再原子替换：失败时旧存档仍可读取。
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            tmp.replace(self.path)
        except BaseException:
            # 写入、fsync 或替换失败时保留旧存档，清理本次未提交的临时件。
            try:
                tmp.unlink()
            except OSError:
                pass
            raise


class GroupStore(JsonStore):
    """每个会话（群/私聊，键为 unified_msg_origin）的设置。"""

    # lang=None 表示本会话没单独设置语言，跟随全局默认（运行时读配置，改默认不用重载）。
    DEFAULTS = {"platform": "pc", "push": False, "quiet": False, "lang": None}

    # 用户可见的开关名 -> 存储键。
    # ⚠️ 早期版本把「推送」这个**中文显示名**直接当键写进了存档，
    #    而读取端用的是 `push` —— 于是「.开启 推送」永远读不回来，
    #    「蹲」一直提示"本群推送功能未开启"。
    SWITCH_KEYS = {"推送": "push", "聊天": "quiet"}

    def __init__(self, path: Path, default_platform: str = "pc"):
        super().__init__(path)
        if not isinstance(self._data, dict):
            if self._data is not None:
                self._quarantine(f"顶层应为对象，实为 {type(self._data).__name__}")
            self._data = {}
        self._default_platform = default_platform
        self._option_lock = asyncio.Lock()

    def get(self, umo: str) -> dict:
        rec = dict(self.DEFAULTS)
        rec["platform"] = self._default_platform
        # 归一化历史脏键（「推送」-> push），否则老存档开了也读不到
        stored = self._data.get(umo)
        for k, v in (stored if isinstance(stored, dict) else {}).items():
            key = self.SWITCH_KEYS.get(k, k)
            if k in self.SWITCH_KEYS and key in stored:
                continue  # 规范键优先，与历史 JSON 的键顺序无关；只读不改用户存档。
            rec[key] = v
        return rec

    def platform(self, umo: str) -> str:
        return self.get(umo)["platform"]

    async def set_platform(self, umo: str, platform: str) -> None:
        await self.edit_option(umo, "platform", lambda old: platform)

    def lang(self, umo: str) -> Optional[str]:
        """本会话显式设置的显示语言；没设置或存档值非法返回 None（跟随全局默认）。"""
        value = self.get(umo).get("lang")
        return value if value in ("zh", "en") else None

    async def set_lang(self, umo: str, lang: Optional[str]) -> None:
        """设置本会话语言；传 None 清除设置，恢复跟随全局默认。"""
        await self.edit_option(umo, "lang", lambda old: lang)

    async def _update_record(self, umo: str, edit: Callable[[dict], Any]) -> Any:
        """串行修改当前会话，写盘失败时恢复原记录。"""
        async with self._option_lock:
            existed = umo in self._data
            previous = self._data.get(umo)
            rec = copy.deepcopy(previous) if isinstance(previous, dict) else {}
            result = edit(rec)
            if rec == previous:
                return result
            self._data[umo] = rec
            try:
                await self.save()
            except Exception:
                if existed:
                    self._data[umo] = previous
                else:
                    self._data.pop(umo, None)
                raise
            return result

    async def edit_option(self, umo: str, key: str, edit: Callable[[Any], Any]) -> Any:
        def change(rec):
            new = edit(rec.get(key))
            if new is None:
                rec.pop(key, None)
            else:
                rec[key] = new
            return new

        return await self._update_record(umo, change)

    def disabled_commands(self, umo: str) -> set[str]:
        value = self.get(umo).get("disabled")
        return (
            {command for command in value if isinstance(command, str)}
            if isinstance(value, list)
            else set()
        )

    def command_aliases(self, umo: str) -> dict[str, str]:
        value = self.get(umo).get("aliases")
        return (
            {key: target for key, target in value.items() if isinstance(target, str)}
            if isinstance(value, dict)
            else {}
        )

    async def set_switch(self, umo: str, key: str, value: bool) -> None:
        """写开关。key 必须是存储键（push/quiet），传入显示名会自动归一化。"""
        key = self.SWITCH_KEYS.get(key, key)

        def change(rec):
            rec[key] = value
            for legacy, canonical in self.SWITCH_KEYS.items():
                if canonical == key:
                    rec.pop(legacy, None)
            if key == "push" and value:
                # 手动重新开启推送，同时解除发送失败的暂停记录。
                rec.pop("push_pause", None)

        await self._update_record(umo, change)

    # ---- 「连续失败暂停」记录（2026-10-17）-----------------------------------
    # 会话连续 FAIL_PURGE_THRESHOLD 轮发送失败后不再清理订阅，而是写一条暂停记录；
    # 记录形如 {"since": ts, "next_try": ts, "reason": str, "fails": int}。
    # 与 push/quiet 同存一个 umo 记录里：`get()` 会把它一并带出来（调用方只读已知键，不受影响）。
    async def set_pause(self, umo: str, info: Optional[dict]) -> None:
        """写入 / 解除暂停记录（info=None 表示解除并 pop 掉该键）。"""
        await self.edit_option(
            umo, "push_pause", lambda old: dict(info) if info is not None else None
        )

    def pause_of(self, umo: str) -> Optional[dict]:
        """读该会话的暂停记录；没有 / 类型不对 ⇒ None。"""
        rec = self._data.get(umo)
        p = rec.get("push_pause") if isinstance(rec, dict) else None
        return dict(p) if isinstance(p, dict) else None

    def all(self) -> dict:
        return self._data


_STAT_OUTCOMES = ("success", "error", "api_error", "no_reply")
_STAT_LATENCY_MS = (
    1,
    5,
    10,
    20,
    35,
    50,
    75,
    100,
    150,
    250,
    350,
    500,
    750,
    1000,
    1500,
    2000,
    3000,
    5000,
    10000,
    30000,
    60000,
    120000,
    300000,
    600000,
)


class CommandStats(JsonStore):
    """UTC 日桶，仅保存指令键、结果计数与处理器耗时直方图。"""

    def __init__(self, path: Path, commands: Iterable[str]):
        super().__init__(path)
        self._commands = frozenset(commands)
        if not isinstance(self._data, dict):
            if self._data is not None:
                self._quarantine("指令统计顶层不是对象")
            self._data = {}
        clean = {}
        for day, rows in self._data.items():
            try:
                if datetime.strptime(day, "%Y-%m-%d").date().isoformat() != day:
                    continue
            except (TypeError, ValueError):
                continue
            if not isinstance(rows, dict):
                continue
            entries = {}
            for command, row in rows.items():
                if command not in self._commands or not isinstance(row, dict):
                    continue
                counts, hist = row.get("counts"), row.get("hist")
                if (
                    isinstance(counts, list)
                    and len(counts) == len(_STAT_OUTCOMES)
                    and isinstance(hist, list)
                    and len(hist) == len(_STAT_LATENCY_MS)
                    and all(type(n) is int and n >= 0 for n in counts + hist)
                    and sum(counts) == sum(hist)
                ):
                    entries[command] = {"counts": counts, "hist": hist}
            if entries:
                clean[day] = entries
        self._revision = int(clean != self._data)
        self._saved_revision = 0
        self._last_flush = time.monotonic()
        self._data = clean
        self._prune(time.time())

    @staticmethod
    def _day(now: float, offset: int = 0) -> str:
        return (
            datetime.fromtimestamp(now, timezone.utc).date() - timedelta(days=offset)
        ).isoformat()

    def _prune(self, now: float) -> None:
        limit = self._day(now, 29)
        for day in list(self._data):
            if day < limit:
                del self._data[day]
                self._revision += 1

    def record(self, command: str, outcome: str, elapsed_ms: float) -> None:
        if command not in self._commands or outcome not in _STAT_OUTCOMES:
            return
        now = time.time()
        day = self._day(now)
        if day not in self._data:
            self._prune(now)
        rows = self._data.setdefault(day, {})
        row = rows.get(command)
        if row is None:
            row = rows[command] = {
                "counts": [0] * len(_STAT_OUTCOMES),
                "hist": [0] * len(_STAT_LATENCY_MS),
            }
        row["counts"][_STAT_OUTCOMES.index(outcome)] += 1
        bucket = min(bisect_left(_STAT_LATENCY_MS, elapsed_ms), len(_STAT_LATENCY_MS) - 1)
        row["hist"][bucket] += 1
        self._revision += 1

    async def flush(self, *, force: bool = False) -> None:
        self._prune(time.time())
        now = time.monotonic()
        if self._revision == self._saved_revision or (not force and now - self._last_flush < 3600):
            return
        revision = self._revision
        try:
            await self.save()
        except OSError:
            self._last_flush = -math.inf
            logger.warning("[warframe] 指令统计落盘失败")
        else:
            self._last_flush = time.monotonic()
            self._saved_revision = revision

    @staticmethod
    def _percentile(hist: list[int], fraction: float) -> int:
        rank = math.ceil(sum(hist) * fraction)
        for upper, count in zip(_STAT_LATENCY_MS, hist):
            rank -= count
            if rank <= 0:
                return upper
        return _STAT_LATENCY_MS[-1]

    def summary(self, days: int, now: Optional[float] = None) -> list[dict]:
        now = time.time() if now is None else now
        first, last = self._day(now, min(max(days, 1), 30) - 1), self._day(now)
        totals: dict = {}
        for day, rows in self._data.items():
            if first <= day <= last:
                for command, row in rows.items():
                    if command not in totals:
                        totals[command] = (row["counts"].copy(), row["hist"].copy())
                    else:
                        counts, hist = totals[command]
                        totals[command] = (
                            [a + b for a, b in zip(counts, row["counts"])],
                            [a + b for a, b in zip(hist, row["hist"])],
                        )
        return sorted(
            (
                {
                    "command": command,
                    "count": sum(counts),
                    **dict(zip(_STAT_OUTCOMES, counts)),
                    "p50_ms": self._percentile(hist, 0.50),
                    "p95_ms": self._percentile(hist, 0.95),
                }
                for command, (counts, hist) in totals.items()
                if sum(counts)
            ),
            key=lambda row: (-row["count"], row["command"]),
        )


@dataclass
class Subscription:
    """一条“蹲”订阅。"""

    umo: str  # 会话标识（推送目标）
    platform: str  # 订阅平台（推送按平台差量比对）
    event: str  # 蹲类型：裂隙/夜灵/奸商/突击/执刑官/仲裁/...
    rule: str = ""  # 筛选规则原文（裂隙筛选等）
    windows: dict = field(default_factory=dict)  # TimeWindow 序列化
    until: float = -1.0  # 过期时间戳；-1 永久
    once: bool = True  # 未写时长 -> 命中一次后取消
    hits_left: Optional[int] = None  # None=不限次数
    created_by: str = ""
    created_at: float = field(default_factory=time.time)
    sid: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    notified: dict = field(default_factory=dict)  # 去重：事件key -> ts
    # v1.3.1：订阅创建时固化显示语言与区服。旧存档没有这两个字段，一律按 v1.3.0 的
    # 「国际服 + 中文」解释 —— 这里写死常量，不读当前配置，避免改默认值后旧订阅换语言。
    lang: str = "zh"
    region: str = "intl"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Subscription":
        flds = {f for f in cls.__dataclass_fields__}  # noqa: C416
        sub = cls(**{k: v for k, v in d.items() if k in flds})
        # dataclass 不校验类型；在进入推送循环前隔离会导致比较、集合或窗口运算崩溃的记录。
        # 仅归一化旧可选字段的空值；不修改调用方原始存档对象。
        for name in ("umo", "platform", "event", "sid"):
            if not isinstance(getattr(sub, name), str) or not getattr(sub, name):
                raise ValueError(f"{name} 应为非空字符串")
        if sub.lang not in ("zh", "en"):
            sub.lang = "zh"
        if sub.region != "intl":
            sub.region = "intl"
        for name in ("rule", "created_by"):
            if getattr(sub, name) is None:
                setattr(sub, name, "")
            if not isinstance(getattr(sub, name), str):
                raise ValueError(f"{name} 应为字符串")

        def number(value):
            return type(value) in (int, float) and math.isfinite(value)

        if not number(sub.until) or not number(sub.created_at):
            raise ValueError("until / created_at 应为有限数值")
        if not isinstance(sub.once, bool):
            raise ValueError("once 应为布尔值")
        if sub.hits_left is not None and type(sub.hits_left) is not int:
            raise ValueError("hits_left 应为整数或空值")
        if sub.windows is None:
            sub.windows = {}
        if not isinstance(sub.windows, dict):
            raise ValueError("windows 应为对象")
        for name in ("start", "end", "at_hour"):
            value = sub.windows.get(name)
            if value is not None and (
                type(value) is not int or not 0 <= value <= (24 if name == "end" else 23)
            ):
                raise ValueError("windows 小时字段不合法")
        days = sub.windows.get("days")
        if days is not None and (
            not isinstance(days, list)
            or any(type(day) is not int or not 1 <= day <= 7 for day in days)
        ):
            raise ValueError("windows days 应为星期列表")
        if not isinstance(sub.notified, dict) or any(
            not isinstance(key, str) or not number(value) for key, value in sub.notified.items()
        ):
            raise ValueError("notified 应为事件到有限时间数值的对象")
        return sub

    def time_window(self) -> TimeWindow:
        w = self.windows or {}
        days = w.get("days")
        if w.get("start") == 0 and w.get("end") == 0 and not days and w.get("at_hour") is None:
            # 旧版把「0到24」解析成 0/0（取模丢了 24），实际只放行 0 点一个小时；
            # 存档里的这种窗口一律按用户当初的本意（全天）处理。
            return TimeWindow()
        return TimeWindow(
            start=w.get("start"),
            end=w.get("end"),
            days=set(days) if days else None,
            at_hour=w.get("at_hour"),
        )

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
            if self._data is not None:
                self._quarantine(f"顶层应为数组，实为 {type(self._data).__name__}")
            self._data = []
        self._bad_warned: set[str] = set()

    def _load_one(self, d) -> Optional[Subscription]:
        """坏条目（缺必填字段/类型不对）返回 None 并 WARNING 一次，不拖垮整个推送。"""
        try:
            if not isinstance(d, dict):
                raise TypeError(f"条目不是对象：{type(d).__name__}")
            return Subscription.from_dict(d)
        except Exception as exc:  # noqa: BLE001
            tag = json.dumps(d, ensure_ascii=False, sort_keys=True, default=str)[:200]
            if tag not in self._bad_warned:
                self._bad_warned.add(tag)
                # ★ 2026-10-25（Astra 评估 S3）：日志只写条目里有哪些字段，不写内容（含发起者 QQ、
                #   会话号、筛选规则）；去重仍按完整内容
                keys = sorted(d) if isinstance(d, dict) else [type(d).__name__]
                logger.warning(
                    "[warframe] 订阅存档含坏条目，已跳过（%s）：字段 %s",
                    exc,
                    ",".join(map(str, keys)),
                )
            return None

    def all(self) -> list[Subscription]:
        return [s for s in (self._load_one(d) for d in self._data) if s is not None]

    def for_umo(self, umo: str) -> list[Subscription]:
        return [s for s in self.all() if s.umo == umo]

    def has(self, sid: str) -> bool:
        """订阅 sid 当前是否仍在存档里（派发前复查：并发取消 / 同 tick 已消费）。"""
        return any(isinstance(d, dict) and d.get("sid") == sid for d in self._data)

    async def add(self, sub: Subscription) -> None:
        self._data.append(sub.to_dict())
        await self.save()

    async def remove(self, predicate) -> int:
        keep = []
        removed = 0
        for d in self._data:
            sub = self._load_one(d)
            if sub is not None and predicate(sub):
                removed += 1
            else:
                keep.append(d)  # 坏条目原样保留（不替用户删数据）
        if removed:
            self._data = keep
            await self.save()
        return removed

    async def update(self, sub: Subscription) -> None:
        """按 sid 原位回写一条订阅（notified / hits_left 等运行期状态落盘）。"""
        for i, d in enumerate(self._data):
            if isinstance(d, dict) and d.get("sid") == sub.sid:
                self._data[i] = sub.to_dict()
                break
        else:
            return
        await self.save()

    async def sync(self, subs: list[Subscription]) -> None:
        self._data = [s.to_dict() for s in subs]
        await self.save()

    def expired_list(self, now: Optional[float] = None) -> list[Subscription]:
        """已过期（尚未 gc）的订阅，供到期提醒用。"""
        now = now or time.time()
        return [s for s in self.all() if s.expired(now)]

    def gc(self, now: Optional[float] = None) -> bool:
        """清理过期订阅，返回是否有变化。"""
        now = now or time.time()
        before = len(self._data)
        keep = []
        for d in self._data:
            sub = self._load_one(d)
            if sub is not None and sub.expired(now):
                continue
            keep.append(d)
        self._data = keep
        return len(self._data) != before
