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


class RecordingLogger(FakeLogger):
    """留下 info 文案，供「派发留痕」断言用。"""

    def __init__(self):
        self.lines: list[str] = []

    def info(self, fmt, *a, **k):
        self.lines.append(fmt % a if a else str(fmt))

    def warning(self, fmt, *a, **k):
        self.lines.append(fmt % a if a else str(fmt))


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

# —— 2026-09-24 自查回归：派发/落盘语义 ——
# ① 同群多条裂隙订阅只该派发给「自身筛选命中」的那条；
# ② 前一条被免打扰窗挡住时，同群全天候订阅要能接住；
# ③ 持续订阅的 hits_left / notified 必须原位落盘（旧 sync 写法是空转）。
import core.push as push_mod  # noqa: E402

_pushed: list[tuple[str, str]] = []


async def _send2(umo, text):
    _pushed.append((umo, text))


async def dispatch_scenarios():
    tmp2 = Path(tempfile.mkdtemp())

    # ① 生存订阅在前、捕获订阅在后；新裂隙是钢铁捕获 → 只命中后者。
    st1 = SubscriptionStore(tmp2 / "s1.json")
    d1 = PushDaemon(FakeClient(), st1, _send2, FakeLogger(), interval=15)
    subA = Subscription(umo="group://A", platform="pc", event="裂隙",
                        rule="生存", until=-1, once=True)
    subB = Subscription(umo="group://A", platform="pc", event="裂隙",
                        rule="捕获", until=-1, once=True)
    await st1.add(subA)
    await st1.add(subB)
    await d1.tick()          # 基线：f1
    n = len(_pushed)
    await d1.tick()          # f2 钢铁捕获：只命中 subB
    check("同群多筛选：派发给筛选命中的订阅", len(_pushed) == n + 1,
          str(_pushed[n:]))
    left = [s.rule for s in st1.all()]
    check("同群多筛选：未命中的订阅不被消费（生存留存）",
          left == ["生存"], str(left))

    # ② 免打扰窗挡住首条时，全天候订阅接住同一事件。
    st2 = SubscriptionStore(tmp2 / "s2.json")
    d2 = PushDaemon(FakeClient(), st2, _send2, FakeLogger(), interval=15)
    subC = Subscription(umo="group://B", platform="pc", event="裂隙",
                        rule="捕获", until=-1, once=True,
                        windows={"start": 0, "end": 1})   # 只在 0-1 点推
    subD = Subscription(umo="group://B", platform="pc", event="裂隙",
                        rule="捕获", until=-1, once=True)
    await st2.add(subC)
    await st2.add(subD)
    await d2.tick()
    n2 = len(_pushed)
    await d2.tick()
    check("免打扰窗挡住首条时，全天候订阅接住推送", len(_pushed) == n2 + 1,
          str(_pushed[n2:]))
    left2 = {s.sid for s in st2.all()}
    check("被窗口挡住的订阅留存，接住的那条消费",
          subC.sid in left2 and subD.sid not in left2, str(left2))

    # ③ 持续订阅（hits_left=2）命中后计数与去重键要落盘。
    st3 = SubscriptionStore(tmp2 / "s3.json")
    d3 = PushDaemon(FakeClient(), st3, _send2, FakeLogger(), interval=15)
    subE = Subscription(umo="group://C", platform="pc", event="裂隙",
                        rule="捕获", until=-1, once=False, hits_left=2)
    await st3.add(subE)
    await d3.tick()
    await d3.tick()
    saved = [s for s in st3.all() if s.sid == subE.sid]
    check("hits_left 计数落盘（2→1）",
          len(saved) == 1 and saved[0].hits_left == 1,
          str(saved and saved[0].hits_left))
    check("notified 去重键落盘",
          len(saved) == 1 and len(saved[0].notified) == 1,
          str(saved and saved[0].notified))

    # ④ ★ 成功派发留痕（2026-09-26）：带实例 id —— 跨实例重复推送会打印两个 id。
    st4 = SubscriptionStore(tmp2 / "s4.json")
    rec = RecordingLogger()
    d4 = PushDaemon(FakeClient(), st4, _send2, rec, interval=15)
    await st4.add(Subscription(umo="group://D", platform="pc", event="裂隙",
                               rule="捕获", until=-1, once=False, hits_left=5))
    await d4.tick()          # 基线，不推
    check("首轮只建基线：无派发日志", not [x for x in rec.lines if "已推送" in x],
          str(rec.lines))
    await d4.tick()          # 推一条 → 恰一行留痕，且实例 id 正确
    sent_lines = [x for x in rec.lines if "已推送" in x]
    check("★ 成功派发留痕恰一行且带实例 id",
          len(sent_lines) == 1 and f"实例 {id(d4)}" in sent_lines[0]
          and "事件=裂隙" in sent_lines[0],
          str(sent_lines))


_real_local_now = push_mod._local_now
push_mod._local_now = lambda: datetime(2026, 9, 24, 14, 0, 0)  # 周四 14:00
asyncio.run(dispatch_scenarios())
push_mod._local_now = _real_local_now

# ---------------------------------------------------------------------------
# ①-5 推送守护：**进程级**单例（2026-09-26 修「重复推送」）
#   事故：同一时刻连发两条（04:00 仲裁 / 05:02 裂隙 / 08:00 仲裁 + 08:02 裂隙同内容）。
#   真因 = 实例级幂等挡不住**跨实例泄漏**的旧 daemon（重载后新旧并存，各推一次）。
# ---------------------------------------------------------------------------
def _mk_daemon() -> PushDaemon:
    return PushDaemon(client=None, store=None, send=None, logger=FakeLogger(), interval=15)


async def daemon_lifecycle():
    push_mod._LIVE_DAEMONS.clear()
    a, b = _mk_daemon(), _mk_daemon()
    a.start()
    ta = a._task
    await asyncio.sleep(0.05)
    b.start()                                  # 跨实例：应取消 A
    await asyncio.sleep(0.05)
    live = [x for x in push_mod._LIVE_DAEMONS if not x.done()]
    check("★ 双实例：B 启动后 A 的守护被取消", ta.done(), f"a.done()={ta.done()}")
    check("★ 双实例：任一时段只有 1 个活跃守护",
          len(live) == 1 and live[0] is b._task, f"活跃={len(live)}")
    tb = b._task
    b.start()                                  # 重复 start 幂等
    check("重复 start 幂等（不新建 task、活跃数仍 1）",
          b._task is tb
          and len([x for x in push_mod._LIVE_DAEMONS if not x.done()]) == 1)
    await b.stop()
    check("★ stop 后登记清空、task 置空",
          b._task is None and not [x for x in push_mod._LIVE_DAEMONS if not x.done()])
    await a.stop()                             # A 早被取消：stop 幂等不抛
    check("stop 幂等（对已停止实例再调，登记仍空）",
          not [x for x in push_mod._LIVE_DAEMONS if not x.done()])


asyncio.run(daemon_lifecycle())


async def leaked_daemon_detection():
    """★ 线上二次事故（2026-09-26 10:59：日志只记一条、群里收到两条）：
    **修复前泄漏的守护不在 `_LIVE_DAEMONS` 里**（当年创建时还没这张表），
    start() 必须靠**扫事件循环里的 `PushDaemon._run` 协程**把它揪出来取消。"""
    push_mod._LIVE_DAEMONS.clear()

    class PushDaemon:              # 与 core.push.PushDaemon 同名 → __qualname__ 命中扫描标记
        async def _run(self):
            await asyncio.sleep(3600)

    leaked = asyncio.create_task(PushDaemon()._run())
    await asyncio.sleep(0)                     # 让它真正跑起来
    rec = RecordingLogger()
    d = push_mod.PushDaemon(client=None, store=None, send=None, logger=rec, interval=15)
    d.start()
    await asyncio.sleep(0.05)
    check("★ 不在登记表里的旧版本守护也会被扫到并取消",
          leaked.done(), f"leaked.done()={leaked.done()} cancelling={leaked.cancelling()}")
    check("★ 取消旧守护时留 WARNING 日志（旧版本守护本身无日志，只能靠这条）",
          any("仍在运行的旧推送守护" in x for x in rec.lines), str(rec.lines))
    check("★ 扫描不会误伤自己：本实例守护仍活跃",
          d._task is not None and not d._task.done()
          and len(push_mod._live_daemon_tasks()) == 1, str(push_mod._live_daemon_tasks()))
    await d.stop()
    push_mod._LIVE_DAEMONS.clear()


asyncio.run(leaked_daemon_detection())

# 基线守卫：空/骤降响应不得清空基线（防跨轮重推）；正常响应照常更新（正反两侧）
_d = _mk_daemon()
_last = {"fissure_ids": ["a", "b", "c", "d"]}
_d._set_baseline(_last, "fissure_ids", [], "ids")
check("★ 空响应 → 保留旧基线", _last["fissure_ids"] == ["a", "b", "c", "d"], str(_last))
_d._set_baseline(_last, "fissure_ids", ["a"], "ids")
check("★ 骤降（不足旧值一半）→ 保留旧基线", _last["fissure_ids"] == ["a", "b", "c", "d"], str(_last))
_d._set_baseline(_last, "fissure_ids", ["a", "b"], "ids")
check("恰好一半 → 视为正常收缩，允许更新", _last["fissure_ids"] == ["a", "b"], str(_last))
_last["fissure_ids"] = ["a", "b", "c", "d"]
_d._set_baseline(_last, "fissure_ids", ["a", "b", "e"], "ids")
check("正常响应 → 更新基线", _last["fissure_ids"] == ["a", "b", "e"], str(_last))
_d._set_baseline(_last, "cetus_state", "")
check("空状态串 → 不写入（保留旧值）", _last.get("cetus_state") is None,
      str(_last.get("cetus_state")))
_d._set_baseline(_last, "cetus_state", "night")
check("首次有效状态 → 写入", _last.get("cetus_state") == "night",
      str(_last.get("cetus_state")))

print()
if FAILED:
    print(f"共 {len(FAILED)} 项失败：{FAILED}")
    sys.exit(1)
print("全部通过 ✔")
