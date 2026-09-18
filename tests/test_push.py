# -*- coding: utf-8 -*-
"""推送守护协程离线集成测试：python3 tests/test_push.py"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.parser import parse_fissure_filter  # noqa: E402
from core.push import PushDaemon, normalize_event  # noqa: E402
from core.store import Subscription, SubscriptionStore  # noqa: E402


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


class FakeLogger:
    def info(self, *a, **k): pass

    def warning(self, *a, **k): pass

    def error(self, *a, **k): pass


class FakeClient:
    """模拟 warframestat 返回：第一轮 2 条裂隙，第二轮新增 1 条钢铁捕获。"""

    def __init__(self):
        self.round = 0
        self._fissures = [
            {"id": "f1", "node": "Teshub (Eris)", "missionType": "Capture",
             "tier": "Lith", "tierNum": 1, "expiry": _iso(datetime.now(timezone.utc)
                                                          + timedelta(hours=1))},
        ]

    async def fissures(self, platform):
        if self.round == 0:
            self.round += 1
        else:
            self._fissures.append({
                "id": "f2", "node": "Ukko (Void)", "missionType": "Capture",
                "tier": "Meso", "tierNum": 2, "isHard": True,
                "expiry": _iso(datetime.now(timezone.utc) + timedelta(minutes=30))})
        return list(self._fissures)

    async def cycle(self, platform, name):
        return {"_name": name, "state": "day",
                "expiry": _iso(datetime.now(timezone.utc) + timedelta(minutes=50))}


FAILED = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" -> {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


async def main():
    tmp = Path(tempfile.mkdtemp())
    store = SubscriptionStore(tmp / "subs.json")
    sent: list[tuple[str, str]] = []

    async def send(umo, text):
        sent.append((umo, text))

    daemon = PushDaemon(FakeClient(), store, send, FakeLogger(), interval=15)

    # 订阅：只蹲 钢铁捕获，命中一次即取消
    sub = Subscription(umo="group://1", platform="pc", event="裂隙",
                       rule="钢铁捕获", until=-1, once=True, hits_left=None,
                       windows={})
    await store.add(sub)

    check("事件词归一化", normalize_event("钢铁裂隙") == "裂隙"
          and normalize_event("执刑官猎杀") == "执刑官")

    flt = parse_fissure_filter("钢铁捕获")
    check("守护协程筛选缓存", daemon.filter_for(sub).describe() == flt.describe(),
          daemon.filter_for(sub).describe())

    await daemon.tick()   # 第一轮：f1（普通）不命中
    check("第一轮无推送（普通捕获不命中钢铁规则）", sent == [], str(sent))

    await daemon.tick()   # 第二轮：f2 钢铁捕获 命中
    check("第二轮推送钢铁捕获", len(sent) == 1 and "Ukko" in sent[0][1] and "钢铁" in sent[0][1],
          str(sent))
    check("一次性订阅已消费移除", store.all() == [], str([s.to_dict() for s in store.all()]))

    await daemon.tick()   # 第三轮：无订阅，不再推送
    check("移除后不再推送", len(sent) == 1)

    # 时长订阅（永久+窗口）
    store2 = SubscriptionStore(tmp / "subs2.json")
    daemon2 = PushDaemon(FakeClient(), store2, send, FakeLogger(), interval=15)
    await store2.add(Subscription(umo="group://2", platform="pc", event="裂隙",
                                  rule="", until=-1, once=False, hits_left=None))
    before = len(sent)
    # 新守护协程首轮只建立基线，不推送
    await daemon2.tick()
    check("首轮建立基线不推送", len(sent) == before)
    check("持久化文件生成", (tmp / "subs2.json").exists())


check("时间窗全天放行",
      parse_fissure_filter.__globals__["TimeWindow"]().allows(datetime(2026, 9, 9, 3, 0)))

asyncio.run(main())

# —— 取消选择器（2026-09-14 修「蹲 取消 裂隙 捕获 把全群订阅删光」）——
from core.push import build_cancel_selector  # noqa: E402

UMO = "GroupMessage:12345"
_group = [
    Subscription(UMO, "pc", "山谷"),
    Subscription(UMO, "pc", "裂隙", rule="虚空捕获"),
    Subscription(UMO, "pc", "裂隙", rule="虚空歼灭"),
    Subscription(UMO, "pc", "裂隙", rule="捕获"),
    Subscription(UMO, "pc", "裂隙", rule="虚空捕获"),
    Subscription("GroupMessage:999", "pc", "裂隙", rule="捕获"),  # 别的群，不能碰
]


def _apply(pred):
    kept = [s for s in _group if not pred(s)]
    return len(_group) - len(kept)


_ev, _keys, _exact, _fuzzy, _label = build_cancel_selector(UMO, ["裂隙", "捕获"])
check("取消选择器：首词识别为事件类型 裂隙", _ev == "裂隙" and _keys == ["捕获"])
check("「蹲 取消 裂隙 捕获」精确匹配只删 1 条（不误杀虚空捕获/别的群）",
      _apply(_exact) == 1)

_ev2, _keys2, _exact2, _fuzzy2, _label2 = build_cancel_selector(UMO, ["虚空捕获"])
check("「蹲 取消 虚空捕获」精确删 2 条", _ev2 is None and _apply(_exact2) == 2)

_, _, _exact3, _fuzzy3, _ = build_cancel_selector(UMO, ["捕获"])
check("「蹲 取消 捕获」精确匹配只删 1 条普通捕获", _apply(_exact3) == 1)
check("选择器模糊兜底按包含可匹配 3 条（虚空捕获也算）", _apply(_fuzzy3) == 3)

_, _, _, _, _label4 = build_cancel_selector(UMO, [])
check("「蹲 取消」无词 = 全部", _label4 == "全部")

_ev5, _, _exact5, _, _ = build_cancel_selector(UMO, ["山谷"])
check("「蹲 山谷 取消」按事件删 1 条且不跨群", _ev5 == "山谷" and _apply(_exact5) == 1)
print()
if FAILED:
    print(f"共 {len(FAILED)} 项失败：{FAILED}")
    sys.exit(1)
print("全部通过 ✔")
