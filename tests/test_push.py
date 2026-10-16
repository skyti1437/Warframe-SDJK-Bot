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
from core.api_client import WarframeAPIError  # noqa: E402
from core.push import PushDaemon, normalize_event  # noqa: E402
from core.store import Subscription, SubscriptionStore  # noqa: E402


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


class FakeLogger:
    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass


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
            {
                "id": "f1",
                "node": "Teshub (Eris)",
                "missionType": "Capture",
                "tier": "Lith",
                "tierNum": 1,
                "expiry": _iso(datetime.now(timezone.utc) + timedelta(hours=1)),
            },
        ]

    async def fissures(self, platform):
        if self.round == 0:
            self.round += 1
        else:
            self._fissures.append(
                {
                    "id": "f2",
                    "node": "Ukko (Void)",
                    "missionType": "Capture",
                    "tier": "Meso",
                    "tierNum": 2,
                    "isHard": True,
                    "expiry": _iso(datetime.now(timezone.utc) + timedelta(minutes=30)),
                }
            )
        return list(self._fissures)

    async def cycle(self, platform, name):
        return {
            "_name": name,
            "state": "day",
            "expiry": _iso(datetime.now(timezone.utc) + timedelta(minutes=50)),
        }

    async def bounty_cycle(self):
        return {
            "expiry": getattr(self, "bounty_exp", "1791047752866"),
            "rot": "A",
            "bounties": {
                "ZarimanSyndicate": [
                    {
                        "node": "SolNode230",
                        "challenge": "/Lotus/Types/Challenges/Zariman/"
                        "ZarimanExterminateFastCompleteChallenge",
                    },
                    {
                        "node": "SolNode231",
                        "challenge": "/Lotus/Types/Challenges/Zariman/"
                        "ZarimanSurvivalAbove50Challenge",
                    },
                ],
            },
        }


FAILED = []


def check(name, cond, detail=""):
    print(
        f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" -> {detail}" if detail and not cond else "")
    )
    if not cond:
        FAILED.append(name)


async def main():
    tmp = Path(tempfile.mkdtemp())
    store = SubscriptionStore(tmp / "subs.json")
    sent: list[tuple[str, str]] = []

    async def send(umo, text, at=None):
        sent.append((umo, text))

    daemon = PushDaemon(FakeClient(), store, send, FakeLogger(), interval=15)

    # 订阅：只蹲 钢铁捕获，命中一次即取消
    sub = Subscription(
        umo="group://1",
        platform="pc",
        event="裂隙",
        rule="钢铁捕获",
        until=-1,
        once=True,
        hits_left=None,
        windows={},
    )
    await store.add(sub)

    check(
        "事件词归一化",
        normalize_event("钢铁裂隙") == "裂隙" and normalize_event("执刑官猎杀") == "执刑官",
    )

    flt = parse_fissure_filter("钢铁捕获")
    check(
        "守护协程筛选缓存",
        daemon.filter_for(sub).describe() == flt.describe(),
        daemon.filter_for(sub).describe(),
    )

    await daemon.tick()  # 第一轮：f1（普通）不命中
    check("第一轮无推送（普通捕获不命中钢铁规则）", sent == [], str(sent))

    await daemon.tick()  # 第二轮：f2 钢铁捕获 命中
    check(
        "第二轮推送钢铁捕获",
        len(sent) == 1 and "Ukko" in sent[0][1] and "钢铁" in sent[0][1],
        str(sent),
    )
    check("一次性订阅已消费移除", store.all() == [], str([s.to_dict() for s in store.all()]))

    await daemon.tick()  # 第三轮：无订阅，不再推送
    check("移除后不再推送", len(sent) == 1)

    # 时长订阅（永久+窗口）
    store2 = SubscriptionStore(tmp / "subs2.json")
    daemon2 = PushDaemon(FakeClient(), store2, send, FakeLogger(), interval=15)
    await store2.add(
        Subscription(
            umo="group://2",
            platform="pc",
            event="裂隙",
            rule="",
            until=-1,
            once=False,
            hits_left=None,
        )
    )
    before = len(sent)
    # 新守护协程首轮只建立基线，不推送
    await daemon2.tick()
    check("首轮建立基线不推送", len(sent) == before)
    check("持久化文件生成", (tmp / "subs2.json").exists())


check(
    "时间窗全天放行",
    parse_fissure_filter.__globals__["TimeWindow"]().allows(datetime(2026, 9, 9, 3, 0)),
)

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
check("「蹲 取消 裂隙 捕获」精确匹配只删 1 条（不误杀虚空捕获/别的群）", _apply(_exact) == 1)

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


async def _send2(umo, text, at=None):
    _pushed.append((umo, text))


async def dispatch_scenarios():
    tmp2 = Path(tempfile.mkdtemp())

    # ① 生存订阅在前、捕获订阅在后；新裂隙是钢铁捕获 → 只命中后者。
    st1 = SubscriptionStore(tmp2 / "s1.json")
    d1 = PushDaemon(FakeClient(), st1, _send2, FakeLogger(), interval=15)
    subA = Subscription(
        umo="group://A", platform="pc", event="裂隙", rule="生存", until=-1, once=True
    )
    subB = Subscription(
        umo="group://A", platform="pc", event="裂隙", rule="捕获", until=-1, once=True
    )
    await st1.add(subA)
    await st1.add(subB)
    await d1.tick()  # 基线：f1
    n = len(_pushed)
    await d1.tick()  # f2 钢铁捕获：只命中 subB
    check("同群多筛选：派发给筛选命中的订阅", len(_pushed) == n + 1, str(_pushed[n:]))
    left = [s.rule for s in st1.all()]
    check("同群多筛选：未命中的订阅不被消费（生存留存）", left == ["生存"], str(left))

    # ② 免打扰窗挡住首条时，全天候订阅接住同一事件。
    st2 = SubscriptionStore(tmp2 / "s2.json")
    d2 = PushDaemon(FakeClient(), st2, _send2, FakeLogger(), interval=15)
    subC = Subscription(
        umo="group://B",
        platform="pc",
        event="裂隙",
        rule="捕获",
        until=-1,
        once=True,
        windows={"start": 0, "end": 1},
    )  # 只在 0-1 点推
    subD = Subscription(
        umo="group://B", platform="pc", event="裂隙", rule="捕获", until=-1, once=True
    )
    await st2.add(subC)
    await st2.add(subD)
    await d2.tick()
    n2 = len(_pushed)
    await d2.tick()
    check("免打扰窗挡住首条时，全天候订阅接住推送", len(_pushed) == n2 + 1, str(_pushed[n2:]))
    left2 = {s.sid for s in st2.all()}
    check(
        "被窗口挡住的订阅留存，接住的那条消费",
        subC.sid in left2 and subD.sid not in left2,
        str(left2),
    )

    # ③ 持续订阅（hits_left=2）命中后计数与去重键要落盘。
    st3 = SubscriptionStore(tmp2 / "s3.json")
    d3 = PushDaemon(FakeClient(), st3, _send2, FakeLogger(), interval=15)
    subE = Subscription(
        umo="group://C", platform="pc", event="裂隙", rule="捕获", until=-1, once=False, hits_left=2
    )
    await st3.add(subE)
    await d3.tick()
    await d3.tick()
    saved = [s for s in st3.all() if s.sid == subE.sid]
    check(
        "hits_left 计数落盘（2→1）",
        len(saved) == 1 and saved[0].hits_left == 1,
        str(saved and saved[0].hits_left),
    )
    check(
        "notified 去重键落盘",
        len(saved) == 1 and len(saved[0].notified) == 1,
        str(saved and saved[0].notified),
    )

    # ④ ★ 成功派发留痕（2026-09-26）：带实例 id —— 跨实例重复推送会打印两个 id。
    st4 = SubscriptionStore(tmp2 / "s4.json")
    rec = RecordingLogger()
    d4 = PushDaemon(FakeClient(), st4, _send2, rec, interval=15)
    await st4.add(
        Subscription(
            umo="group://D",
            platform="pc",
            event="裂隙",
            rule="捕获",
            until=-1,
            once=False,
            hits_left=5,
        )
    )
    await d4.tick()  # 基线，不推
    check("首轮只建基线：无派发日志", not [x for x in rec.lines if "已推送" in x], str(rec.lines))
    await d4.tick()  # 推一条 → 恰一行留痕，且实例 id 正确
    sent_lines = [x for x in rec.lines if "已推送" in x]
    check(
        "★ 成功派发留痕恰一行且带实例 id",
        len(sent_lines) == 1 and f"实例 {id(d4)}" in sent_lines[0] and "事件=裂隙" in sent_lines[0],
        str(sent_lines),
    )


_real_local_now = push_mod._local_now
push_mod._local_now = lambda: datetime(2026, 9, 24, 14, 0, 0)  # 周四 14:00
asyncio.run(dispatch_scenarios())
push_mod._local_now = _real_local_now


# ---------------------------------------------------------------------------
# A1 @当事人聚合（2026-10-03 用户批准的特例）：
#   同一事件、同一会话、命中的订阅发起人收齐去重 ⇒ 一次性 @ 全部；
#   未命中的规则不得被 @；跨事件/跨会话绝不合并。
# ---------------------------------------------------------------------------
async def at_scenarios():
    tmp3 = Path(tempfile.mkdtemp())
    st = SubscriptionStore(tmp3 / "subs_at.json")
    sent_at: list[tuple[str, str, list]] = []

    async def send_at(umo, text, at=None):
        sent_at.append((umo, text, list(at or [])))

    daemon = PushDaemon(FakeClient(), st, send_at, FakeLogger(), interval=15)
    await st.add(
        Subscription(
            umo="group://AT",
            platform="pc",
            event="裂隙",
            rule="钢铁捕获",
            until=-1,
            once=False,
            created_by="1001",
        )
    )
    await st.add(
        Subscription(
            umo="group://AT",
            platform="pc",
            event="裂隙",
            rule="捕获",
            until=-1,
            once=False,
            created_by="1002",
        )
    )
    await st.add(
        Subscription(
            umo="group://AT",
            platform="pc",
            event="裂隙",
            rule="钢铁歼灭",
            until=-1,
            once=False,
            created_by="1003",
        )
    )
    await st.add(
        Subscription(
            umo="group://AT",
            platform="pc",
            event="裂隙",
            rule="钢铁捕获",
            until=-1,
            once=False,
            created_by="1004",
        )
    )  # 多人同规则
    await daemon.tick()  # 基线
    await daemon.tick()  # f2 = Ukko 虚空+钢铁 捕获
    check(
        "★ A1 一条推送一次性 @ 全部命中发起人（去重保序）",
        len(sent_at) == 1 and sent_at[0][2] == ["1001", "1002", "1004"],
        str(sent_at),
    )
    check(
        "★ A1 未命中的规则不得被 @（1003 排除）",
        sent_at and "1003" not in sent_at[0][2],
        str(sent_at),
    )

    # 不同会话独立 @：另一个群的同规则不得混入
    await st.add(
        Subscription(
            umo="group://AT2",
            platform="pc",
            event="裂隙",
            rule="钢铁捕获",
            until=-1,
            once=False,
            created_by="2001",
        )
    )
    await daemon.tick()
    await daemon.tick()
    check(
        "★ A1 跨会话不合并（各自 @ 自己的）",
        all(
            set(a) <= ({"1001", "1002", "1004"} if u == "group://AT" else {"2001"})
            for u, _t, a in sent_at
        ),
        str(sent_at),
    )


asyncio.run(at_scenarios())

# ---------------------------------------------------------------------------
# A2 钢精兑换换轮点检测（2026-10-03）：每周一 00:00 UTC，同源 steel_rotation_index
# ---------------------------------------------------------------------------
import core.formatters as fmt_mod  # noqa: E402
from core import formatters as F2  # noqa: E402
from core.formatters import steel_shop as _steel_shop  # noqa: E402
from core.formatters import steel_rotation_index as _sri  # noqa: E402


async def steel_scenario():
    tmp4 = Path(tempfile.mkdtemp())
    st = SubscriptionStore(tmp4 / "subs_steel.json")
    sent_s: list[tuple[str, str]] = []

    async def send_s(umo, text, at=None):
        sent_s.append((umo, text))

    d = PushDaemon(FakeClient(), st, send_s, FakeLogger(), interval=15)
    await st.add(
        Subscription(
            umo="group://S",
            platform="pc",
            event="钢精兑换",
            rule="",
            until=-1,
            once=False,
            hits_left=None,
        )
    )
    data = _steel_shop()
    rot = data["rotation"]
    boundary = datetime.fromisoformat(rot["epoch"]) + timedelta(hours=int(rot["period_hours"]) * 8)
    orig = fmt_mod._now
    fmt_mod._now = lambda: boundary - timedelta(minutes=30)
    await d.tick()
    check("A2 首轮只建基线不推", sent_s == [], str(sent_s))
    fmt_mod._now = lambda: boundary + timedelta(minutes=30)
    await d.tick()
    idx2, _ = _sri(data)
    fmt_mod._now = orig
    check(
        "A2 跨换轮点推送且带新轮换项名",
        len(sent_s) == 1
        and "钢精兑换已轮换" in sent_s[0][1]
        and data["weekly"][idx2]["name"] in sent_s[0][1],
        str(sent_s),
    )
    # 不跨点不推：同轮内再 tick 一次
    fmt_mod._now = lambda: boundary + timedelta(hours=1)
    await d.tick()
    fmt_mod._now = orig
    check("A2 同轮内不重复推送", len(sent_s) == 1, str(sent_s))


asyncio.run(steel_scenario())

# ---------------------------------------------------------------------------
# A3 碎银兑换周重置（Palladino；无轮换库存，事件 = 每周限购重置 周一 00:00 UTC）
# ---------------------------------------------------------------------------
from core.formatters import palladino_shop as _pshop  # noqa: E402


async def sliver_scenario():
    tmp5 = Path(tempfile.mkdtemp())
    st = SubscriptionStore(tmp5 / "subs_sliver.json")
    sent_l: list[tuple[str, str]] = []

    async def send_l(umo, text, at=None):
        sent_l.append((umo, text))

    d = PushDaemon(FakeClient(), st, send_l, FakeLogger(), interval=15)
    await st.add(
        Subscription(
            umo="group://L",
            platform="pc",
            event="碎银兑换",
            rule="",
            until=-1,
            once=False,
            hits_left=None,
        )
    )
    rot = _pshop()["rotation"]
    boundary = datetime.fromisoformat(rot["epoch"]) + timedelta(hours=int(rot["period_hours"]) * 8)
    orig = fmt_mod._now
    fmt_mod._now = lambda: boundary - timedelta(minutes=30)
    await d.tick()
    check("A3 首轮只建基线不推", sent_l == [], str(sent_l))
    fmt_mod._now = lambda: boundary + timedelta(minutes=30)
    await d.tick()
    fmt_mod._now = orig
    check(
        "A3 跨周重置点推送（含 Palladino）",
        len(sent_l) == 1 and "碎银兑换已重置" in sent_l[0][1] and "Palladino" in sent_l[0][1],
        str(sent_l),
    )


asyncio.run(sliver_scenario())


# ---------------------------------------------------------------------------
# C2 赏金订阅（2026-10-03）：地区 + 挑战名/任务类型（用户例：扎里曼 高效歼灭）
# ---------------------------------------------------------------------------
async def bounty_scenario():
    tmp7 = Path(tempfile.mkdtemp())
    st = SubscriptionStore(tmp7 / "subs_bounty.json")
    sent_b: list[tuple[str, str]] = []

    async def send_b(umo, text, at=None):
        sent_b.append((umo, text))

    d = PushDaemon(FakeClient(), st, send_b, FakeLogger(), interval=15)
    await st.add(
        Subscription(
            umo="group://B1",
            platform="pc",
            event="赏金",
            rule="扎里曼 高效歼灭",
            until=-1,
            once=False,
        )
    )
    await st.add(
        Subscription(
            umo="group://B2", platform="pc", event="赏金", rule="扎里曼 生存", until=-1, once=False
        )
    )
    await d.tick()
    check("C2 首轮只建基线不推", sent_b == [], str(sent_b))
    # 2026-10-11：赏金改为「换轮那一刻产出事件」（旧写法每轮重发当前全部赏金，窗口外的订阅
    # 进窗后会补推当前轮），所以这里先模拟换轮（expiry 变了）再断言推送。
    d.client.bounty_exp = "1791047999999"
    await d.tick()
    check(
        "C2 两条订阅各自命中对应赏金（歼灭→B1、生存→B2）",
        len(sent_b) == 2
        and any(u == "group://B1" and "高效歼灭" in t for u, t in sent_b)
        and any(u == "group://B2" and "减少虚空污染" in t for u, t in sent_b),
        str(sent_b),
    )
    check(
        "C2 不串台：B2 未收到歼灭赏金 / B1 未收到生存赏金",
        not any(u == "group://B2" and "高效歼灭" in t for u, t in sent_b)
        and not any(u == "group://B1" and "减少虚空污染" in t for u, t in sent_b),
        str(sent_b),
    )
    check(
        "C2 推送含 地区/类型/目标",
        any("羽化之穹" in t and "歼灭" in t and "6 分钟" in t for _u, t in sent_b),
        str(sent_b)[:150],
    )


asyncio.run(bounty_scenario())


# ---------------------------------------------------------------------------
# C3 警报接线（2026-10-03）：活动型警报（Tag=LotusGift）——推一次；
#   同一 _id 只推一次（alert_ids 基线去重）。
# ---------------------------------------------------------------------------
class AlertClient(FakeClient):
    """轮次控制：round0 空（建基线）→ round1 出现 a1（推一次）→ 之后保持。"""

    def __init__(self):
        super().__init__()
        self._round = 0

    async def alerts(self, platform):
        self._round += 1
        if self._round < 2:
            return []
        return [
            {
                "id": "a1",
                "active": True,
                "expiry": _iso(datetime.now(timezone.utc) + timedelta(days=12)),
                "mission": {
                    "node": "Ganymede（木星）",
                    "type": "中断",
                    "min_level": 20,
                    "max_level": 30,
                    "desc": "Tenno 联合警报",
                    "reward": {
                        "credits": 10000,
                        "item": "2021 年 QTCC 玩偶",
                        "item_names": ["2021 年 QTCC 玩偶"],
                    },
                },
            }
        ]


async def alert_scenario():
    tmp8 = Path(tempfile.mkdtemp())
    st = SubscriptionStore(tmp8 / "subs_alert.json")
    sent_a: list[tuple[str, str]] = []

    async def send_a(umo, text, at=None):
        sent_a.append((umo, text))

    c = AlertClient()
    d = PushDaemon(c, st, send_a, FakeLogger(), interval=15)
    await st.add(
        Subscription(umo="group://A1", platform="pc", event="警报", rule="", until=-1, once=False)
    )
    await d.tick()
    check("C3 首轮只建基线不推", sent_a == [], str(sent_a))
    await d.tick()
    check(
        "C3 活动型警报推送一次（含 desc/节点/奖励中文名）",
        len(sent_a) == 1
        and "Tenno 联合警报" in sent_a[0][1]
        and "Ganymede" in sent_a[0][1]
        and "2021 年 QTCC 玩偶" in sent_a[0][1],
        str(sent_a),
    )
    await d.tick()
    check("C3 同一 _id 只推一次（基线去重）", len(sent_a) == 1, str(sent_a))


asyncio.run(alert_scenario())


# ---------------------------------------------------------------------------
# C4 灵化/信条/终幕接蹲（2026-10-03）：换轮点检测（同源 rotation_window），
#   跨点推「新一批内容」。此处以灵化（9 周循环）为例。
# ---------------------------------------------------------------------------
async def rotation_scenario():
    tmp9 = Path(tempfile.mkdtemp())
    st = SubscriptionStore(tmp9 / "subs_rot.json")
    sent_r: list[tuple[str, str]] = []

    async def send_r(umo, text, at=None):
        sent_r.append((umo, text))

    d = PushDaemon(FakeClient(), st, send_r, FakeLogger(), interval=15)
    await st.add(
        Subscription(
            umo="group://R1", platform="pc", event="灵化武器", rule="", until=-1, once=False
        )
    )
    import json as _json

    rot = _json.loads(
        (Path(__file__).resolve().parent.parent / "core" / "data" / "rotations.json").read_text(
            encoding="utf-8"
        )
    )
    inc = rot["incarnon"]
    epoch = datetime.fromisoformat(inc["epoch"])
    boundary = epoch + timedelta(hours=int(inc["period_hours"]) * 8)
    orig = fmt_mod._now
    fmt_mod._now = lambda: boundary - timedelta(minutes=30)
    await d.tick()
    check("C4 首轮只建基线不推", sent_r == [], str(sent_r))
    fmt_mod._now = lambda: boundary + timedelta(minutes=30)
    await d.tick()
    passed, _ = F2.rotation_window(inc)
    pos = (int(inc.get("anchor_week", 1)) - 1 + passed) % len(inc["weeks"])
    want = inc["weeks"][pos][0].get("cn") or ""
    fmt_mod._now = orig
    check(
        "C4 跨周换轮推送且含新一批武器名",
        len(sent_r) == 1 and "灵化轮换" in sent_r[0][1] and want and want in sent_r[0][1],
        f"{sent_r} want={want}",
    )
    fmt_mod._now = lambda: boundary + timedelta(hours=1)
    await d.tick()
    fmt_mod._now = orig
    check("C4 同窗口不重复推送", len(sent_r) == 1, str(sent_r))


asyncio.run(rotation_scenario())


# ---------------------------------------------------------------------------
# C5 阿耶兑换 / 1999 日历 / 电波 接蹲（2026-10-03）：stage 控制数据变化。
# ---------------------------------------------------------------------------
class C5Client(FakeClient):
    def __init__(self):
        super().__init__()
        self.stage = 0

    async def prime_vault(self, platform):
        if self.stage == 0:
            return {"expiry": "e0", "items": [{"name": "旧宝库包"}]}
        return {"expiry": "e1", "items": [{"name": "Banshee Prime 组合包"}]}

    async def calendar(self, platform):
        today = fmt_mod._now().astimezone(timezone.utc).date().isoformat()
        evs = [] if self.stage == 0 else [{"name": "敲山震虎"}]
        return {"season": "S1", "yearIteration": 1, "days": [{"date": today, "events": evs}]}

    async def nightwave(self, platform):
        chs = [{"id": "c1", "title": "旧挑战"}]
        if self.stage >= 1:
            chs.append({"id": "c2", "title": "新挑战", "isDaily": True})
        return {"activeChallenges": chs}


async def c5_scenario():
    tmpA = Path(tempfile.mkdtemp())
    st = SubscriptionStore(tmpA / "subs_c5.json")
    sent5: list[tuple[str, str]] = []

    async def send5(umo, text, at=None):
        sent5.append((umo, text))

    c = C5Client()
    d = PushDaemon(c, st, send5, FakeLogger(), interval=15)
    for ev in ("阿耶兑换", "1999日历", "电波"):
        await st.add(
            Subscription(umo="group://C5", platform="pc", event=ev, rule="", until=-1, once=False)
        )
    await d.tick()
    check("C5 首轮只建基线不推", sent5 == [], str(sent5))
    c.stage = 1
    await d.tick()
    check("C5 三事件各自推送一次", len(sent5) == 3, str(sent5))
    check(
        "C5 阿耶兑换：宝库轮换含新包名",
        any("Prime 宝库轮换" in t and "Banshee Prime 组合包" in t for _u, t in sent5),
        str(sent5),
    )
    check(
        "C5 1999 日历：今日日程含挑战名",
        any("今日日程" in t and "敲山震虎" in t for _u, t in sent5),
        str(sent5),
    )
    check(
        "C5 电波：新挑战（每日标注）",
        any("午夜电波新挑战" in t and "新挑战（每日）" in t for _u, t in sent5),
        str(sent5),
    )
    await d.tick()
    check("C5 无新变化不重复推送", len(sent5) == 3, str(sent5))


asyncio.run(c5_scenario())


# at_targets 单元：平台过滤 + 去重 + 剔除 @全体
from core.push import at_targets  # noqa: E402

check(
    "at_targets：仅 aiocqhttp 支持，去重保序、剔除 all",
    at_targets("aiocqhttp", ["1", "2", "1", "all", ""]) == ["1", "2"]
    and at_targets("telegram", ["1"]) == []
    and at_targets(None, ["1"]) == []
    and at_targets("aiocqhttp", []) == [],
)


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
    b.start()  # 跨实例：应取消 A
    await asyncio.sleep(0.05)
    live = [x for x in push_mod._LIVE_DAEMONS if not x.done()]
    check("★ 双实例：B 启动后 A 的守护被取消", ta.done(), f"a.done()={ta.done()}")
    check(
        "★ 双实例：任一时段只有 1 个活跃守护",
        len(live) == 1 and live[0] is b._task,
        f"活跃={len(live)}",
    )
    tb = b._task
    b.start()  # 重复 start 幂等
    check(
        "重复 start 幂等（不新建 task、活跃数仍 1）",
        b._task is tb and len([x for x in push_mod._LIVE_DAEMONS if not x.done()]) == 1,
    )
    await b.stop()
    check(
        "★ stop 后登记清空、task 置空",
        b._task is None and not [x for x in push_mod._LIVE_DAEMONS if not x.done()],
    )
    await a.stop()  # A 早被取消：stop 幂等不抛
    check(
        "stop 幂等（对已停止实例再调，登记仍空）",
        not [x for x in push_mod._LIVE_DAEMONS if not x.done()],
    )


asyncio.run(daemon_lifecycle())


async def leaked_daemon_detection():
    """★ 线上二次事故（2026-09-26 10:59：日志只记一条、群里收到两条）：
    **修复前泄漏的守护不在 `_LIVE_DAEMONS` 里**（当年创建时还没这张表），
    start() 必须靠**扫事件循环里的 `PushDaemon._run` 协程**把它揪出来取消。"""
    push_mod._LIVE_DAEMONS.clear()

    class PushDaemon:  # 与 core.push.PushDaemon 同名 → __qualname__ 命中扫描标记
        async def _run(self):
            await asyncio.sleep(3600)

    leaked = asyncio.create_task(PushDaemon()._run())
    await asyncio.sleep(0)  # 让它真正跑起来
    rec = RecordingLogger()
    d = push_mod.PushDaemon(client=None, store=None, send=None, logger=rec, interval=15)
    d.start()
    await asyncio.sleep(0.05)
    check(
        "★ 不在登记表里的旧版本守护也会被扫到并取消",
        leaked.done(),
        # Task.cancelling() 是 3.11+ 接口；3.10 上只拿 cancelled()（仅诊断文案，不影响判定）
        f"leaked.done()={leaked.done()} cancelled={leaked.cancelled()}"
        + (f" cancelling={leaked.cancelling()}" if hasattr(leaked, "cancelling") else ""),
    )
    check(
        "★ 取消旧守护时留 WARNING 日志（旧版本守护本身无日志，只能靠这条）",
        any("仍在运行的旧推送守护" in x for x in rec.lines),
        str(rec.lines),
    )
    check(
        "★ 扫描不会误伤自己：本实例守护仍活跃",
        d._task is not None and not d._task.done() and len(push_mod._live_daemon_tasks()) == 1,
        str(push_mod._live_daemon_tasks()),
    )
    await d.stop()
    push_mod._LIVE_DAEMONS.clear()


asyncio.run(leaked_daemon_detection())

# 基线守卫：空/骤降响应不得清空基线（防跨轮重推）；正常响应照常更新（正反两侧）
_d = _mk_daemon()
_last = {"fissure_ids": ["a", "b", "c", "d"]}
_d._set_baseline(_last, "fissure_ids", [], "ids")
check("★ 空响应 → 保留旧基线", _last["fissure_ids"] == ["a", "b", "c", "d"], str(_last))
_d._set_baseline(_last, "fissure_ids", ["a"], "ids")
check(
    "★ 骤降（不足旧值一半）→ 保留旧基线", _last["fissure_ids"] == ["a", "b", "c", "d"], str(_last)
)
_d._set_baseline(_last, "fissure_ids", ["a", "b"], "ids")
check("恰好一半 → 视为正常收缩，允许更新", _last["fissure_ids"] == ["a", "b"], str(_last))
_last["fissure_ids"] = ["a", "b", "c", "d"]
_d._set_baseline(_last, "fissure_ids", ["a", "b", "e"], "ids")
check("正常响应 → 更新基线", _last["fissure_ids"] == ["a", "b", "e"], str(_last))
_d._set_baseline(_last, "cetus_state", "")
check(
    "空状态串 → 不写入（保留旧值）", _last.get("cetus_state") is None, str(_last.get("cetus_state"))
)
_d._set_baseline(_last, "cetus_state", "night")
check("首次有效状态 → 写入", _last.get("cetus_state") == "night", str(_last.get("cetus_state")))

# ===========================================================================
# 2026-10-11 推送子系统审计修复批（P1-1 … P3-5）：每项至少一正一反
# ===========================================================================
import json  # noqa: E402
import os  # noqa: E402
import time  # noqa: E402

import core.store as store_mod  # noqa: E402
from core.parser import TimeWindow, parse_time_window  # noqa: E402
from core.store import GroupStore  # noqa: E402


class LvLogger:
    """区分级别的记录日志器（断言 WARNING 留痕用）。"""

    def __init__(self):
        self.info_lines: list[str] = []
        self.warn_lines: list[str] = []

    def info(self, fmt, *a, **k):
        self.info_lines.append(fmt % a if a else str(fmt))

    def warning(self, fmt, *a, **k):
        self.warn_lines.append(fmt % a if a else str(fmt))

    def error(self, fmt, *a, **k):
        self.warn_lines.append("ERROR " + (fmt % a if a else str(fmt)))


class ScriptClient:
    """可编程假客户端：data[name] 可为值或异常；未设置的源返回空结构。"""

    def __init__(self):
        self.data: dict = {}

    def _g(self, name, default):
        v = self.data.get(name, default)
        if isinstance(v, Exception):
            raise v
        return v

    async def fissures(self, p):
        return self._g("fissures", [])

    async def cycle(self, p, n):
        return self._g("cycle_" + n, {})

    async def goals(self, p):
        return self._g("goals", [])

    async def invasions(self, p):
        return self._g("invasions", [])

    async def news(self, p):
        return self._g("news", [])

    async def alerts(self, p):
        return self._g("alerts", [])

    async def sortie(self, p):
        return self._g("sortie", {})

    async def archon_hunt(self, p):
        return self._g("archon", {})

    async def void_trader(self, p):
        return self._g("trader", {})

    async def daily_deals(self, p):
        return self._g("deals", [])

    async def nightwave(self, p):
        return self._g("nightwave", {})

    async def bounty_cycle(self):
        return self._g("bounty", {})


def _zfut(m=60):
    return _iso(datetime.now(timezone.utc) + timedelta(minutes=m))


def _zfis(i, tier="Lith", mt="捕获", hard=False, storm=False, node="X (Y)"):
    return {
        "id": i,
        "node": node,
        "missionType": mt,
        "tier": tier,
        "tierNum": 1,
        "isHard": hard,
        "isStorm": storm,
        "expiry": _zfut(),
    }


def _zinv(i):
    return {"id": i, "node": "N" + i, "attacker": {"faction": "A"}, "defender": {"faction": "B"}}


def _zsub(umo="g1", ev="裂隙", rule="", once=True, created_by="", windows=None, until=-1, **kw):
    return Subscription(
        umo=umo,
        platform="pc",
        event=ev,
        rule=rule,
        once=once,
        created_by=created_by,
        windows=windows or {},
        until=until,
        **kw,
    )


def _zmk(groups=None, state_path=None, interval=45):
    tmp = Path(tempfile.mkdtemp())
    st = SubscriptionStore(tmp / "subscriptions.json")
    cl = ScriptClient()
    box: list = []
    lg = LvLogger()

    async def send(umo, text, at=None):
        box.append((umo, text, list(at or [])))

    d = PushDaemon(cl, st, send, lg, interval, groups=groups, state_path=state_path)
    return d, st, cl, box, lg, tmp


_BJ = timezone(timedelta(hours=8))
_ALWAYS = lambda: datetime(2026, 10, 12, 14, 0, tzinfo=_BJ)  # noqa: E731  周一 14:00 北京时间
_Z_BOUNTY_EXT = "/Lotus/Types/Challenges/Zariman/ZarimanExterminateFastCompleteChallenge"
_Z_BOUNTY_SURV = "/Lotus/Types/Challenges/Zariman/ZarimanSurvivalAbove50Challenge"


async def z_baseline_scenarios():
    # —— P1-1 ——
    d, st, cl, box, lg, _ = _zmk()
    cl.data["cycle_cetus"] = {"state": "day", "expiry": _zfut()}
    cl.data["invasions"] = [_zinv(str(n)) for n in range(5)]
    cl.data["news"] = [{"id": f"n{n}", "message": f"新闻{n}"} for n in range(4)]
    cl.data["fissures"] = [_zfis(f"f{n}") for n in range(6)]
    await st.add(_zsub("g1", "夜灵", once=False))
    await d.tick()
    await st.add(_zsub("g2", "入侵", once=False, created_by="111"))
    await st.add(_zsub("g2", "新闻", once=False))
    await st.add(_zsub("g2", "裂隙", once=False))
    await d.tick()
    check("★ P1-1 夜灵先建基线后，新增 入侵/新闻/裂隙 订阅的那一轮不灌存量", box == [], str(box))
    cl.data["invasions"].append(_zinv("new"))
    cl.data["fissures"].append(_zfis("fnew"))
    await d.tick()
    texts = [t for _u, t, _a in box]
    check(
        "P1-1 护栏：之后真正的新入侵/新裂隙照常推（各 1 条）",
        len(box) == 2 and any("新入侵" in t for t in texts) and any("新裂隙" in t for t in texts),
        str(box),
    )

    # 所有订阅清空 ⇒ 基线丢弃；数小时后重订不灌存量（陈旧基线变体）
    d, st, cl, box, lg, _ = _zmk()
    cl.data["invasions"] = [_zinv("a")]
    await st.add(_zsub("g1", "入侵", once=False))
    await d.tick()
    await st.remove(lambda s: True)
    await d.tick()
    check("★ P1-1 订阅清空 ⇒ 丢弃全部基线", d._last == {}, str(d._last))
    cl.data["invasions"] = [_zinv("b")]
    await st.add(_zsub("g1", "入侵", once=False))
    await d.tick()
    check("★ P1-1 清空后重订那一轮只建基线不推", box == [], str(box))

    # 某类型订阅消失后再出现，同样只建基线
    d, st, cl, box, lg, _ = _zmk()
    cl.data["invasions"] = [_zinv("a")]
    cl.data["cycle_cetus"] = {"state": "day", "expiry": _zfut()}
    await st.add(_zsub("g1", "入侵", once=False))
    await st.add(_zsub("g1", "夜灵", once=False))
    await d.tick()
    await st.remove(lambda s: s.event == "入侵")
    await d.tick()
    check(
        "P1-1 类型订阅消失 ⇒ 只丢该类型基线",
        "invasion_ids" not in d._last["pc"] and "cetus_state" in d._last["pc"],
        str(d._last),
    )
    cl.data["invasions"] = [_zinv("a"), _zinv("b"), _zinv("c")]
    await st.add(_zsub("g1", "入侵", once=False))
    await d.tick()
    check("★ P1-1 类型消失后再出现的那一轮不灌存量", box == [], str(box))


async def z_dispatch_scenarios():
    # —— P1-2：一次性订阅同 tick 只推一条 ——
    d, st, cl, box, lg, _ = _zmk()
    cl.data["fissures"] = [_zfis("f0")]
    await st.add(_zsub("g1", "裂隙", "钢铁", once=True, created_by="A"))
    await d.tick()
    cl.data["fissures"] = [_zfis("f0")] + [_zfis(f"n{i}", hard=True) for i in range(3)]
    await d.tick()
    check(
        "★ P1-2 一次性订阅同 tick 3 个新事件只推 1 条", len(box) == 1 and st.all() == [], str(box)
    )
    # 护栏：持续订阅同 tick 照常每个事件各推一条
    d, st, cl, box, lg, _ = _zmk()
    cl.data["fissures"] = [_zfis("f0")]
    await st.add(_zsub("g1", "裂隙", "钢铁", once=False))
    await d.tick()
    cl.data["fissures"] = [_zfis("f0")] + [_zfis(f"n{i}", hard=True) for i in range(3)]
    await d.tick()
    check("P1-2 护栏：持续订阅同 tick 3 个事件推 3 条", len(box) == 3, str(len(box)))
    # hits_left 不超额
    d, st, cl, box, lg, _ = _zmk()
    cl.data["fissures"] = [_zfis("f0")]
    await st.add(_zsub("g1", "裂隙", "钢铁", once=False, hits_left=2))
    await d.tick()
    cl.data["fissures"] = [_zfis("f0")] + [_zfis(f"n{i}", hard=True) for i in range(3)]
    await d.tick()
    check(
        "★ P1-2 hits_left=2 同 tick 3 个事件只推 2 条并移除",
        len(box) == 2 and st.all() == [],
        str(len(box)),
    )

    # —— P1-3：合并 @ 的消费与窗口过滤 ——
    d, st, cl, box, lg, _ = _zmk()
    cl.data["fissures"] = [_zfis("f0")]
    await st.add(_zsub("g1", "裂隙", "捕获", created_by="A"))
    await st.add(_zsub("g1", "裂隙", "捕获", created_by="B"))
    await d.tick()
    cl.data["fissures"] = [_zfis("f0"), _zfis("f1")]
    await d.tick()
    check(
        "★ P1-3 被合并 @ 的一次性订阅同样被消费",
        len(box) == 1 and box[0][2] == ["A", "B"] and st.all() == [],
        f"{box} {st.all()}",
    )
    cl.data["fissures"] = [_zfis("f0"), _zfis("f1"), _zfis("f2")]
    await d.tick()
    check("P1-3 下一个事件不再有人被 @/推送", len(box) == 1, str(box))

    # 窗口外的订阅不被 @、不被消费、不记 notified
    d, st, cl, box, lg, _ = _zmk()
    cl.data["fissures"] = [_zfis("f0")]
    off = {"start": 20, "end": 23, "days": None, "at_hour": None}  # 14:00 在窗外
    await st.add(_zsub("g1", "裂隙", "捕获", once=False, created_by="A"))
    await st.add(_zsub("g1", "裂隙", "捕获", once=False, created_by="B", windows=off))
    await d.tick()
    cl.data["fissures"] = [_zfis("f0"), _zfis("f1")]
    await d.tick()
    b_sub = next(s for s in st.all() if s.created_by == "B")
    check("★ P1-3 窗口外的订阅不被 @", box[0][2] == ["A"], str(box))
    check("P1-3 窗口外的订阅不记 notified", b_sub.notified == {}, str(b_sub.notified))

    # 已就该事件通知过的订阅不再进 @ 集合
    d, st, cl, box, lg, _ = _zmk()
    sa, sb = _zsub("g1", "裂隙", created_by="A"), _zsub("g1", "裂隙", created_by="B")
    await st.add(sa)
    await st.add(sb)
    sa.notified["k1"] = 1.0
    got = d._eligible([sa, sb], "k1")
    check("P1-3 已就此事件通知过的订阅不进 @ 集合", [s.sid for s in got] == [sb.sid], str(got))

    # —— P3-3：tick 取数期间用户取消 ——
    d, st, cl, box, lg, _ = _zmk()
    cl.data["invasions"] = [_zinv("a")]
    await st.add(_zsub("g1", "入侵", once=False))
    await d.tick()
    cl.data["invasions"] = [_zinv("a"), _zinv("b")]
    orig = cl.invasions

    async def slow(p):
        await asyncio.sleep(0.15)
        return await orig(p)

    cl.invasions = slow
    t = asyncio.create_task(d.tick())
    await asyncio.sleep(0.04)
    await st.remove(lambda s: True)
    await t
    check("★ P3-3 取数期间被取消的订阅不再推送", box == [], str(box))
    # 护栏：没取消就正常推（同样的慢取数）
    d, st, cl, box, lg, _ = _zmk()
    cl.data["invasions"] = [_zinv("a")]
    await st.add(_zsub("g1", "入侵", once=False))
    await d.tick()
    cl.data["invasions"] = [_zinv("a"), _zinv("b")]
    orig2 = cl.invasions

    async def slow2(p):
        await asyncio.sleep(0.05)
        return await orig2(p)

    cl.invasions = slow2
    await d.tick()
    check("P3-3 护栏：未取消照常推", len(box) == 1, str(box))


async def z_empty_response_scenarios():
    # —— P1-4 ——
    d, st, cl, box, lg, _ = _zmk()
    cl.data.update(
        sortie={"id": "S1"},
        archon={"id": "A1"},
        deals=[{"id": "D1", "item": "x", "salePrice": 1, "total": 2}],
        trader={"active": True, "location": "L", "expiry": _zfut(), "activation": "2026-10-01"},
    )
    for e in ("突击", "执刑官", "每日特惠", "奸商"):
        await st.add(_zsub("g1", e, once=False))
    await d.tick()
    cl.data.update(sortie={}, archon={}, deals=[], trader={})
    await d.tick()
    check("★ P1-4 突击/执刑官/每日特惠/奸商 空响应不推", box == [], str(box))
    check(
        "P1-4 空响应保留旧基线",
        d._last["pc"].get("sortie_id") == "S1"
        and d._last["pc"].get("archon_id") == "A1"
        and d._last["pc"].get("deal_key") == "D1"
        and d._last["pc"].get("trader_active") is True,
        str(d._last),
    )
    cl.data.update(sortie={"id": "S1"}, archon={"id": "A1"}, deals=[{"id": "D1", "item": "x"}])
    await d.tick()
    check("P1-4 源恢复且 id 未变 ⇒ 仍不推", box == [], str(box))
    cl.data.update(sortie={"id": "S2"}, archon={"id": "A2"}, deals=[{"id": "D2", "item": "y"}])
    await d.tick()
    check("P1-4 护栏：id 真变化照常推（3 条）", len(box) == 3, str([t[:8] for _u, t, _a in box]))


async def z_group_switch_scenarios():
    # —— P2-1 ——
    tmp = Path(tempfile.mkdtemp())
    groups = GroupStore(tmp / "groups.json", default_platform="pc")
    await groups.set_switch("g1", "push", True)
    d, st, cl, box, lg, _ = _zmk(groups=groups)
    cl.data["invasions"] = [_zinv("a")]
    await st.add(_zsub("g1", "入侵", once=True, created_by="A"))
    await d.tick()
    await groups.set_switch("g1", "push", False)
    cl.data["invasions"] = [_zinv("a"), _zinv("b")]
    await d.tick()
    left = st.all()
    check("★ P2-1 关闭推送 ⇒ 不派发", box == [], str(box))
    check(
        "P2-1 暂停期间订阅保留、不消费、不记 notified",
        len(left) == 1 and left[0].notified == {},
        str(left),
    )
    await groups.set_switch("g1", "push", True)
    await d.tick()
    check("P2-1 恢复后不补推暂停期间的事件", box == [], str(box))
    cl.data["invasions"] = [_zinv("a"), _zinv("b"), _zinv("c")]
    await d.tick()
    check("★ P2-1 重新开启后新事件照常推且订阅仍可用", len(box) == 1 and st.all() == [], str(box))
    # 护栏：另一个群未关闭，不受影响
    d, st, cl, box, lg, _ = _zmk(groups=groups)
    await groups.set_switch("g2", "push", True)
    cl.data["invasions"] = [_zinv("a")]
    await st.add(_zsub("g1", "入侵", once=False))
    await st.add(_zsub("g2", "入侵", once=False))
    await groups.set_switch("g1", "push", False)
    await d.tick()
    cl.data["invasions"] = [_zinv("a"), _zinv("b")]
    await d.tick()
    check("P2-1 护栏：只暂停被关闭的群，另一个群照推", [u for u, _t, _a in box] == ["g2"], str(box))


def z_time_window_cases():
    def at(day, h, mi=0):  # 2026-10-12 是周一
        return datetime(2026, 10, 12 + day, h, mi, tzinfo=_BJ)

    w = parse_time_window("0到24")
    check(
        "★ P2-2 「0到24」= 全天",
        w.is_always and all(w.allows(at(0, h, 10)) for h in range(24)) and w.describe() == "全天",
        str(w),
    )
    legacy = Subscription(
        umo="g",
        platform="pc",
        event="夜灵",
        windows={"start": 0, "end": 0, "days": None, "at_hour": None},
    )
    check(
        "P2-2 兼容：旧存档里 0/0（旧版「0到24」）按全天处理；周窗口 0 点不受影响",
        legacy.time_window().is_always
        and not Subscription(
            umo="g", platform="pc", event="夜灵", windows={"start": 0, "end": 0, "days": [1]}
        )
        .time_window()
        .allows(at(0, 5)),
    )
    w = parse_time_window("22到8")
    check(
        "★ P2-2 「22到8」22:00 起、7:59 仍在、8:00 整结束",
        w.allows(at(0, 22, 0))
        and w.allows(at(1, 7, 59))
        and not w.allows(at(1, 8, 0))
        and not w.allows(at(0, 21, 59)),
    )
    check("P2-2 describe：22:00-次日8:00", w.describe() == "22:00-次日8:00", w.describe())
    w = parse_time_window("8到22")
    check(
        "P2-2 「8到22」21:59 在、22:00 不在（结束小时开区间）",
        w.allows(at(0, 21, 59)) and not w.allows(at(0, 22, 0)),
    )
    w = TimeWindow(start=22, end=8, days={5})
    check(
        "★ P2-2 「周五22到8」含周六凌晨，按窗口起始那天算",
        w.allows(at(4, 23, 30))  # 周五 23:30
        and w.allows(at(5, 3, 10))  # 周六 03:10
        and not w.allows(at(5, 23, 30))  # 周六 23:30（起始日是周六）
        and not w.allows(at(6, 3, 10))  # 周日 03:10（起始日是周六）
        and not w.allows(at(4, 3, 10)),  # 周五 03:10（起始日是周四）
    )
    w2 = parse_time_window("周5 22到8")
    check(
        "P2-2 「周5 22到8」可解析且口径同上",
        w2 is not None
        and w2.days == {5}
        and w2.allows(at(5, 3, 10))
        and not w2.allows(at(6, 3, 10)),
        str(w2),
    )
    w = parse_time_window("每天19点")
    check(
        "★ P2-2 「每天19点」= 19:00–19:59 整个小时",
        w.allows(at(0, 19, 0))
        and w.allows(at(0, 19, 45))
        and not w.allows(at(0, 20, 0))
        and not w.allows(at(0, 18, 59)),
    )
    w = parse_time_window("周1/3/5 23点")
    check(
        "P2-2 「周1/3/5 23点」口径一致（整小时）",
        w.allows(at(2, 23, 50))
        and w.allows(at(0, 23, 0))
        and not w.allows(at(1, 23, 10))
        and not w.allows(at(2, 22, 59)),
    )
    check(
        "P2-2 护栏：越界写法不解析",
        parse_time_window("25到8") is None and parse_time_window("每天25点") is None,
    )
    # 北京时间与服务器时区无关
    old = os.environ.get("TZ")
    try:
        got = []
        for tz in ("America/Los_Angeles", "UTC", "Asia/Tokyo"):
            os.environ["TZ"] = tz
            time.tzset()
            got.append(push_mod._local_now().utcoffset())
        check(
            "★ P2-2 _local_now 恒为 UTC+8，不随服务器时区变",
            all(g == timedelta(hours=8) for g in got),
            str(got),
        )
        os.environ["TZ"] = "America/Los_Angeles"
        time.tzset()
        skew = abs((push_mod._local_now() - datetime.now(timezone.utc)).total_seconds())
        check("P2-2 _local_now 是「现在」（与 UTC 同一时刻，仅时区表示不同）", skew < 5, str(skew))
    finally:
        if old is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old
        time.tzset()


async def z_send_failure_scenarios():
    # —— P2-3：瞬时失败 → 下一轮重投 ——
    d, st, cl, box, lg, _ = _zmk()
    cl.data["invasions"] = [_zinv("a")]
    await st.add(_zsub("g1", "入侵", once=True))
    await d.tick()
    cl.data["invasions"] = [_zinv("a"), _zinv("b")]
    fail = [True]

    async def flaky(umo, text, at=None):
        if fail[0]:
            raise RuntimeError("timeout")
        box.append((umo, text, list(at or [])))

    d.send = flaky
    await d.tick()
    check(
        "P2-3 首次失败：订阅保留、未记 notified、进待发队列",
        len(st.all()) == 1 and st.all()[0].notified == {} and len(d._pending) == 1,
    )
    fail[0] = False
    await d.tick()
    check(
        "★ P2-3 恢复后下一轮重投成功且订阅被消费",
        len(box) == 1 and "Nb" in box[0][1] and st.all() == [] and d._pending == [],
        str(box),
    )
    await d.tick()
    check("P2-3 重投成功后不再重复推送", len(box) == 1)

    # 过期丢弃
    d, st, cl, box, lg, _ = _zmk()
    cl.data["invasions"] = [_zinv("a")]
    await st.add(_zsub("g1", "入侵", once=False))
    await d.tick()
    cl.data["invasions"] = [_zinv("a"), _zinv("b")]

    async def always_fail(umo, text, at=None):
        raise RuntimeError("timeout")

    d.send = always_fail
    await d.tick()
    d._pending[0]["created"] -= push_mod.PENDING_TTL + 1

    async def ok_send(umo, text, at=None):
        box.append((umo, text, list(at or [])))

    d.send = ok_send
    await d.tick()
    check("★ P2-3 超过有效期的待发事件被丢弃（不再重投）", box == [] and d._pending == [], str(box))

    # 重试上限：单个事件 1 次首投 + 最多 3 轮重投，之后丢弃；计数未到阈值，订阅保留
    d, st, cl, box, lg, _ = _zmk()
    cl.data["invasions"] = [_zinv("a")]
    await st.add(_zsub("g1", "入侵", once=False))
    await d.tick()
    cl.data["invasions"] = [_zinv("a"), _zinv("b")]
    calls = [0]

    async def count_fail(umo, text, at=None):
        calls[0] += 1
        raise RuntimeError("timeout")

    d.send = count_fail
    for _i in range(7):
        await d.tick()
    check(
        "★ P2-3 重试有上限：1 次首投 + 3 轮重投后丢弃，不无限重试",
        calls[0] == 1 + push_mod.PENDING_MAX_TRIES and d._pending == [] and len(st.all()) == 1,
        f"{calls} {d._pending}",
    )

    # 连续失败达阈值 ⇒ 清理该会话订阅（别的会话不受影响）
    d, st, cl, box, lg, _ = _zmk()
    cl.data["invasions"] = [_zinv("a")]
    await st.add(_zsub("g1", "入侵", once=False))
    await st.add(_zsub("g2", "入侵", once=False))
    await d.tick()

    async def g1_bad(umo, text, at=None):
        if umo == "g1":
            raise RuntimeError("timeout")
        box.append((umo, text, list(at or [])))

    d.send = g1_bad
    for i in range(push_mod.FAIL_PURGE_THRESHOLD):
        cl.data["invasions"] = [_zinv("a")] + [_zinv(f"x{k}") for k in range(i + 1)]
        await d.tick()
        if i < push_mod.FAIL_PURGE_THRESHOLD - 1:
            assert len(st.for_umo("g1")) == 1
    check("★ P2-3 连续失败达阈值 ⇒ 清理该会话订阅", st.for_umo("g1") == [], str(st.all()))
    check(
        "P2-3 清理留 WARNING",
        any("已清理该会话" in x and "g1" in x for x in lg.warn_lines),
        str(lg.warn_lines[-3:]),
    )
    check("P2-3 护栏：别的会话订阅不受影响且照常收到", len(st.for_umo("g2")) == 1 and len(box) >= 1)
    warn_n = len(lg.warn_lines)
    cl.data["invasions"].append(_zinv("after"))
    await d.tick()
    check("P2-3 清理后不再刷失败日志", len(lg.warn_lines) == warn_n, str(lg.warn_lines[warn_n:]))

    # 失败后成功 ⇒ 计数复位，订阅不被清
    d, st, cl, box, lg, _ = _zmk()
    cl.data["invasions"] = [_zinv("a")]
    await st.add(_zsub("g1", "入侵", once=False))
    await d.tick()
    mode = ["fail"]

    async def toggle(umo, text, at=None):
        if mode[0] == "fail":
            raise RuntimeError("timeout")
        box.append((umo, text, list(at or [])))

    d.send = toggle
    seq = 0

    async def new_event():
        nonlocal seq
        seq += 1
        cl.data["invasions"] = [_zinv("a")] + [_zinv(f"e{k}") for k in range(seq)]
        await d.tick()

    for _i in range(4):
        await new_event()
    mode[0] = "ok"
    await new_event()
    mode[0] = "fail"
    for _i in range(4):
        await new_event()
    check(
        "P2-3 护栏：中间成功过 ⇒ 失败计数复位，订阅仍保留",
        len(st.for_umo("g1")) == 1,
        str(d._fail_count),
    )

    # 识别「群不存在 / 被踢」⇒ 立即清理
    d, st, cl, box, lg, _ = _zmk()
    cl.data["invasions"] = [_zinv("a")]
    await st.add(_zsub("g1", "入侵", once=False))
    await d.tick()
    cl.data["invasions"] = [_zinv("a"), _zinv("b")]

    async def kicked(umo, text, at=None):
        raise RuntimeError("retcode=100 群不存在或已被踢出")

    d.send = kicked
    await d.tick()
    check(
        "★ P2-3 「群不存在/被踢」类错误一次即清理订阅并留 WARNING",
        st.all() == [] and any("已清理该会话" in x for x in lg.warn_lines),
        str(lg.warn_lines),
    )


async def z_state_persist_scenarios():
    # —— P2-4 ——
    tmp = Path(tempfile.mkdtemp())
    sp = tmp / "push_baseline.json"
    d, st, cl, box, lg, tmp2 = _zmk(state_path=sp)
    cl.data["cycle_cetus"] = {"state": "day", "expiry": _zfut()}
    await st.add(_zsub("g1", "夜灵", once=False))
    await d.tick()
    saved = json.loads(sp.read_text(encoding="utf-8"))
    check(
        "P2-4 基线落盘（带时间戳）",
        saved["last"]["pc"]["cetus_state"] == "day" and abs(saved["ts"] - time.time()) < 5,
        str(saved),
    )
    check("P2-4 原子写：不留 .tmp", not list(tmp.glob("*.tmp")))

    def restart(path):
        st2 = SubscriptionStore(tmp2 / "subscriptions.json")
        box2: list = []

        async def send2(umo, text, at=None):
            box2.append((umo, text))

        return PushDaemon(cl, st2, send2, LvLogger(), 45, state_path=path), box2

    cl.data["cycle_cetus"] = {"state": "night", "expiry": _zfut()}  # 停机期间入夜
    d2, box2 = restart(sp)
    await d2.tick()
    check(
        "★ P2-4 30 分钟内重启 ⇒ 补推停机期间的新事件",
        len(box2) == 1 and "入夜" in box2[0][1],
        str(box2),
    )

    saved["ts"] = time.time() - 31 * 60
    sp.write_text(json.dumps(saved), encoding="utf-8")
    d3, box3 = restart(sp)
    await d3.tick()
    check(
        "★ P2-4 基线超过 30 分钟 ⇒ 只建基线不补推",
        box3 == [] and d3._last["pc"]["cetus_state"] == "night",
        str(box3),
    )
    sp.write_text("{坏的", encoding="utf-8")
    d4, box4 = restart(sp)
    await d4.tick()
    check("P2-4 护栏：快照损坏 ⇒ 当作没有，不崩不推", box4 == [])
    d5, box5 = restart(None)
    await d5.tick()
    check("P2-4 护栏：不配 state_path 则不落盘、不推", box5 == [])


async def z_store_failure_scenarios():
    # —— P2-5 ——
    d, st, cl, box, lg, _ = _zmk()
    cl.data["invasions"] = [_zinv("a")]
    cl.data["news"] = [{"id": "n0", "message": "m0"}]
    await st.add(_zsub("g1", "入侵", once=False))
    await st.add(_zsub("g1", "新闻", once=False))
    await d.tick()
    cl.data["invasions"] = [_zinv("a"), _zinv("b")]
    cl.data["news"] = [{"id": "n0", "message": "m0"}, {"id": "n1", "message": "m1"}]

    async def boom(sub_):
        raise OSError("disk full")

    st.update = boom
    try:
        await d.tick()
        raised = None
    except Exception as exc:  # noqa: BLE001
        raised = exc
    check(
        "★ P2-5 update 写盘失败不抛出、后续事件不丢",
        raised is None and len(box) == 2,
        f"{raised} {box}",
    )
    check(
        "P2-5 写盘失败留 WARNING", any("写盘失败" in x for x in lg.warn_lines), str(lg.warn_lines)
    )
    # 一次性订阅 remove 写盘失败：内存已移除，同 tick 不会再推第二条
    d, st, cl, box, lg, _ = _zmk()
    cl.data["fissures"] = [_zfis("f0")]
    await st.add(_zsub("g1", "裂隙", "钢铁", once=True))
    await d.tick()
    cl.data["fissures"] = [_zfis("f0")] + [_zfis(f"n{i}", hard=True) for i in range(3)]

    async def boom_save():
        raise OSError("disk full")

    st.save = boom_save
    await d.tick()
    check(
        "★ P2-5 remove 写盘失败：一次性订阅仍只推 1 条",
        len(box) == 1 and st.all() == [],
        str(len(box)),
    )

    # —— P3-1 坏条目 ——
    tmp = Path(tempfile.mkdtemp())
    p = tmp / "subscriptions.json"
    good = _zsub("g1", "夜灵", once=False).to_dict()
    p.write_text(json.dumps([good, {"event": "裂隙"}, "junk", 5]), encoding="utf-8")
    lg2 = LvLogger()
    real_logger = store_mod.logger
    store_mod.logger = lg2
    try:
        st3 = SubscriptionStore(p)
        n1 = len(st3.all())
        st3.all()
        st3.all()
        warns = [x for x in lg2.warn_lines if "坏条目" in x]
        check("★ P3-1 坏条目被跳过，好条目保留", n1 == 1, str(n1))
        check("P3-1 同一坏条目只 WARNING 一次（3 种坏条目各一条）", len(warns) == 3, str(warns))
        removed = await st3.remove(lambda s: s.event == "夜灵")
        check(
            "P3-1 护栏：remove 不误删坏条目原始数据",
            removed == 1 and len(st3._data) == 3,
            str(st3._data),
        )
    finally:
        store_mod.logger = real_logger
    d, st4, cl, box, lg, _ = _zmk()
    st4.path.write_text(json.dumps([good, {"event": "裂隙"}]), encoding="utf-8")
    st4.load()
    d.store = st4
    cl.data["cycle_cetus"] = {"state": "day", "expiry": _zfut()}
    try:
        await d.tick()
        ok = True
    except Exception:  # noqa: BLE001
        ok = False
    check(
        "★ P3-1 存档含坏条目时推送不瘫痪（tick 不抛）",
        ok and "cetus_state" in d._last.get("pc", {}),
    )

    # —— P3-2 JSON 损坏备份 + fsync ——
    tmp = Path(tempfile.mkdtemp())
    p = tmp / "subscriptions.json"
    p.write_text("{broken", encoding="utf-8")
    real_logger = store_mod.logger
    lg3 = LvLogger()
    store_mod.logger = lg3
    try:
        s5 = SubscriptionStore(p)
        baks = list(tmp.glob("subscriptions.json.bak-*"))
        check(
            "★ P3-2 损坏的订阅存档先改名备份再按空处理，并 WARNING",
            s5.all() == []
            and len(baks) == 1
            and baks[0].read_text(encoding="utf-8") == "{broken"
            and any("损坏" in x for x in lg3.warn_lines),
            f"{baks} {lg3.warn_lines}",
        )
        gp = tmp / "groups.json"
        gp.write_text("[1,2", encoding="utf-8")
        g = GroupStore(gp)
        check(
            "★ P3-2 GroupStore 同理",
            g.all() == {} and len(list(tmp.glob("groups.json.bak-*"))) == 1,
        )
        gp2 = tmp / "groups2.json"
        gp2.write_text("[]", encoding="utf-8")
        GroupStore(gp2)
        check("P3-2 顶层类型不对也备份", len(list(tmp.glob("groups2.json.bak-*"))) == 1)
        okp = tmp / "ok.json"
        okp.write_text("[]", encoding="utf-8")
        SubscriptionStore(okp)
        check("P3-2 护栏：正常文件不被改名", okp.exists() and not list(tmp.glob("ok.json.bak-*")))
    finally:
        store_mod.logger = real_logger
    calls = []
    real_fsync = store_mod.os.fsync
    store_mod.os.fsync = lambda fd: (calls.append(fd), real_fsync(fd))[1]
    try:
        await s5.add(_zsub("g1", "夜灵"))
    finally:
        store_mod.os.fsync = real_fsync
    check("P3-2 save 在 replace 前 fsync", len(calls) == 1 and p.exists())


async def z_cancel_scenarios():
    # 发送链路内部抛 CancelledError（守护任务本身没被取消）⇒ 当作一次失败，tick 不被带走
    d, st, cl, box, lg, _ = _zmk()
    cl.data["invasions"] = [_zinv("a")]
    await st.add(_zsub("g1", "入侵", once=False))
    await d.tick()
    cl.data["invasions"] = [_zinv("a"), _zinv("b")]

    async def inner_cancel(umo, text, at=None):
        raise asyncio.CancelledError()

    d.send = inner_cancel
    t = asyncio.create_task(d.tick())
    await asyncio.sleep(0.05)
    if hasattr(asyncio.Task, "cancelling"):
        check(
            "★ 发送内部抛取消：守护 tick 不被取消、记 WARNING",
            t.done() and not t.cancelled() and any("发送被取消" in x for x in lg.warn_lines),
            str(lg.warn_lines),
        )
    else:
        # Python 3.10 没有 Task.cancelling()，_send_guarded 无法区分「守护被停」与「链路内部取消」，
        # 按设计保守上抛（宁可这一轮 tick 结束，也不吞掉真正的停止信号）——这里钉住该降级口径。
        check(
            "★ 发送内部抛取消（3.10 降级口径）：无 cancelling() ⇒ 保守上抛，tick 以取消结束",
            t.done() and t.cancelled(),
            str(lg.warn_lines),
        )
    # 守护任务真的在「发送中」被取消（stop/重载）⇒ 继续上抛，任务以取消结束，且有日志
    d2, st2, cl2, box2, lg2, _ = _zmk(interval=15)
    cl2.data["invasions"] = [_zinv("a")]
    await st2.add(_zsub("g1", "入侵", once=False))
    await d2.tick()
    cl2.data["invasions"] = [_zinv("a"), _zinv("b")]
    started = asyncio.Event()

    async def slow_send(umo, text, at=None):
        started.set()
        await asyncio.sleep(30)

    d2.send = slow_send
    task = asyncio.create_task(d2._run())
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    check(
        "P3-3 发送中被取消 ⇒ 守护以取消结束并留日志（不静默）",
        task.cancelled() and any("被取消" in x for x in lg2.info_lines),
        str(lg2.info_lines),
    )
    check("护栏：被取消的那次发送没有被误记为失败", d2._fail_count == {} and d2._pending == [])
    d3, *_ = _zmk()
    check("main 接线：PushDaemon.task 暴露当前守护任务（供 done-callback）", d3.task is None)


async def z_storm_and_misc_scenarios():
    # —— P3-4 九重天风暴文案 / 筛选 ——
    d, st, cl, box, lg, _ = _zmk()
    cl.data["fissures"] = [_zfis("f0")]
    await st.add(_zsub("g1", "裂隙", "九重天前哨战", once=False))
    await st.add(_zsub("g2", "裂隙", "前哨战", once=False))
    await st.add(_zsub("g3", "裂隙", "九重天爆发", once=False))
    await d.tick()
    cl.data["fissures"] = [
        _zfis("f0"),
        _zfis("s1", tier="Axi", mt="Skirmish", storm=True, node="Gian Point（面纱）"),
        _zfis("s2", tier="Axi", mt="Orphix", storm=True, node="Gian Point（面纱）"),
    ]
    await d.tick()
    by = {u: [t for uu, t, _a in box if uu == u] for u in ("g1", "g2", "g3")}
    check(
        "★ P3-4 「蹲 裂隙 九重天前哨战」命中风暴前哨战，文案用卡面译名",
        len(by["g1"]) == 1 and "前哨战" in by["g1"][0] and "九重天" in by["g1"][0],
        str(by),
    )
    check("★ P3-4 「蹲 裂隙 前哨战」命中", len(by["g2"]) == 1, str(by))
    check("P3-4 护栏：蹲爆发的不被前哨战/奥影母艇误触发", by["g3"] == [], str(by))
    d, st, cl, box, lg, _ = _zmk()
    cl.data["fissures"] = [_zfis("f0")]
    await st.add(_zsub("g1", "裂隙", "", once=False))
    await d.tick()
    cl.data["fissures"] = [
        _zfis("f0"),
        _zfis("s2", mt="Orphix", storm=True),
        _zfis("n1", mt="Orphix"),
    ]
    await d.tick()
    texts = [t for _u, t, _a in box]
    check(
        "P3-4 Orphix：风暴用卡面译名「奥影母艇」，非风暴仍走 mission_cn",
        any("奥影母艇" in t and "九重天" in t for t in texts)
        and any("奥菲克斯" in t for t in texts),
        str(texts),
    )

    # —— P3-5a 一个源失败不拖累其它 + (d) 指数退避 ——
    d, st, cl, box, lg, _ = _zmk(interval=45)
    cl.data["fissures"] = WarframeAPIError("DE 世界状态返回异常")
    cl.data["cycle_cetus"] = {"state": "day", "expiry": _zfut()}
    await st.add(_zsub("g1", "裂隙", once=False))
    await st.add(_zsub("g1", "夜灵", once=False))
    await d.tick()
    cl.data["cycle_cetus"] = {"state": "night", "expiry": _zfut()}
    await d.tick()
    check("★ P3-5a 裂隙源失败不饿死后面的夜灵", len(box) == 1 and "入夜" in box[0][1], str(box))
    check("P3-5d 部分源成功不退避", d.next_delay() == 45)
    cl.data["cycle_cetus"] = WarframeAPIError("down")
    delays = []
    for _i in range(8):
        await d.tick()
        delays.append(d.next_delay())
    check(
        "★ P3-5d 全源失败 ⇒ tick 级指数退避（翻倍，有上限）",
        delays[0] == 90 and delays[1] == 180 and max(delays) <= 600 and delays[-1] == 600,
        str(delays),
    )
    cl.data["cycle_cetus"] = {"state": "night", "expiry": _zfut()}
    await d.tick()
    check("P3-5d 恢复后复位", d.next_delay() == 45)

    # —— P3-5c 精英周常 ——
    d, st, cl, box, lg, _ = _zmk()
    cl.data["nightwave"] = {"activeChallenges": [{"id": "c1", "title": "旧"}]}
    await st.add(_zsub("g1", "电波", once=False))
    await d.tick()
    cl.data["nightwave"] = {
        "activeChallenges": [
            {"id": "c1", "title": "旧"},
            {"id": "c2", "title": "精英一", "isElite": True},
            {"id": "c3", "title": "每日一", "isDaily": True},
            {"id": "c4", "title": "周常一"},
        ]
    }
    await d.tick()
    t = box[0][1] if box else ""
    check(
        "P3-5c 电波推送区分 精英周常/每日/周常",
        "精英一（精英周常）" in t and "每日一（每日）" in t and "周常一（周常）" in t,
        t,
    )

    # —— P3-5e 到期提醒 ——
    d, st, cl, box, lg, _ = _zmk()
    past = time.time() - 5
    await st.add(_zsub("g1", "夜灵", once=False, until=past, created_by="A"))
    await st.add(_zsub("g1", "裂隙", "钢铁", once=False, until=past - 1, created_by="B"))
    await st.add(_zsub("g1", "入侵", once=False, until=-1, created_by="C"))
    await st.add(_zsub("g2", "夜灵", once=False, until=past, created_by="D"))
    cl.data["invasions"] = [_zinv("a")]
    await d.tick()
    r1 = [b for b in box if b[0] == "g1"]
    r2 = [b for b in box if b[0] == "g2"]
    check(
        "★ P3-5e 同群同时刻多条到期合并成一条提醒（@ 订阅人 + 续订指引）",
        len(r1) == 1
        and set(r1[0][2]) == {"A", "B"}
        and "夜灵" in r1[0][1]
        and "钢铁" in r1[0][1]
        and "续订" in r1[0][1],
        str(r1),
    )
    check("P3-5e 不同群各自一条且不串 @", len(r2) == 1 and r2[0][2] == ["D"], str(r2))
    check(
        "P3-5e 发送成功留 INFO 日志（含会话与订阅号，2026-10-16 补：此前成功路径完全静默）",
        sum(1 for x in lg.info_lines if "到期提醒已发出" in x) == 2
        and any("g1" in x and "2 条" in x for x in lg.info_lines if "到期提醒已发出" in x),
        str([x for x in lg.info_lines if "到期" in x]),
    )
    check(
        "P3-5e 护栏：未到期订阅不提醒也不被清",
        len(st.all()) == 1 and st.all()[0].event == "入侵",
    )
    n = len(box)
    await d.tick()
    check("P3-5e 提醒只发一次", len(box) == n)
    tmpg = Path(tempfile.mkdtemp())
    gg = GroupStore(tmpg / "g.json")
    await gg.set_switch("g1", "push", False)
    d, st, cl, box, lg, _ = _zmk(groups=gg)
    await st.add(_zsub("g1", "夜灵", once=False, until=time.time() - 5, created_by="A"))
    await d.tick()
    check("P3-5e 护栏：推送已关闭的群不发到期提醒（订阅照清）", box == [] and st.all() == [])

    # —— P3-5b 赏金：换轮才产出、同会话合并、窗口外不补推 ——
    d, st, cl, box, lg, _ = _zmk()
    cl.data["bounty"] = {
        "expiry": "100",
        "bounties": {
            "ZarimanSyndicate": [
                {"node": "SolNode230", "challenge": _Z_BOUNTY_EXT},
                {"node": "SolNode231", "challenge": _Z_BOUNTY_SURV},
            ]
        },
    }
    await st.add(_zsub("g1", "赏金", "扎里曼", once=False, created_by="A"))
    await st.add(_zsub("g1", "赏金", "扎里曼", once=False, created_by="B"))
    await st.add(_zsub("g2", "赏金", "扎里曼 歼灭", once=False))
    await d.tick()
    await d.tick()
    check("P3-5b 同一轮内不重复发赏金（旧写法第二轮会补推全部）", box == [], str(box))
    cl.data["bounty"]["expiry"] = "200"
    await d.tick()
    g1m = [b for b in box if b[0] == "g1"]
    g2m = [b for b in box if b[0] == "g2"]
    check(
        "★ P3-5b 换轮：同会话多条赏金合并为一条消息（@ 两人）",
        len(g1m) == 1 and "（2 条）" in g1m[0][1] and g1m[0][2] == ["A", "B"],
        str(g1m),
    )
    check(
        "P3-5b 护栏：规则只命中一条的会话仍是单条文案",
        len(g2m) == 1 and "（2 条）" not in g2m[0][1],
        str(g2m),
    )
    d, st, cl, box, lg, _ = _zmk()
    cl.data["bounty"] = {
        "expiry": "100",
        "bounties": {"ZarimanSyndicate": [{"node": "SolNode230", "challenge": _Z_BOUNTY_EXT}]},
    }
    await st.add(_zsub("g1", "赏金", "扎里曼", once=False, windows={"start": 20, "end": 23}))
    await d.tick()
    cl.data["bounty"]["expiry"] = "200"
    await d.tick()  # 14:00 在窗外：换轮事件被丢弃
    real = push_mod._local_now
    push_mod._local_now = lambda: datetime(2026, 10, 12, 21, 0, tzinfo=_BJ)
    try:
        await d.tick()
    finally:
        push_mod._local_now = real
    check("★ P3-5b 窗口外的订阅进入窗口后不补推当前轮", box == [], str(box))


_real_ln = push_mod._local_now
push_mod._local_now = _ALWAYS
try:
    asyncio.run(z_baseline_scenarios())
    asyncio.run(z_dispatch_scenarios())
    asyncio.run(z_empty_response_scenarios())
    asyncio.run(z_group_switch_scenarios())
    asyncio.run(z_send_failure_scenarios())
    asyncio.run(z_state_persist_scenarios())
    asyncio.run(z_store_failure_scenarios())
    asyncio.run(z_cancel_scenarios())
    asyncio.run(z_storm_and_misc_scenarios())
finally:
    push_mod._local_now = _real_ln
z_time_window_cases()


# ★ 2026-10-16（第十五批 A3）：仲裁排期表到期不再静默 —— 记一次 WARNING，连续多轮不刷屏
async def z_arbi_expired():
    d, st, cl, box, lg, _ = _zmk()
    await st.add(_zsub("g1", "仲裁", once=False))  # 有仲裁订阅时守护才跑这一段
    real_cur = push_mod.arbi.current

    async def _expired(client, ts=None):
        raise push_mod.arbi.ScheduleExpired(1.0)

    push_mod.arbi.current = _expired
    try:
        await d.tick()
        await d.tick()
    finally:
        push_mod.arbi.current = real_cur
    n = sum(1 for x in lg.warn_lines if "仲裁排期表已到期" in x)
    check("★ A3 仲裁排期表到期记 WARNING 且两轮只记一次", n == 1, str(lg.warn_lines))


# ★ 2026-10-16：仲裁轮换推送有评级时附评分范围（无评级不附）
async def z_arbi_tier_note():
    d, st, cl, box, lg, _ = _zmk()
    await st.add(_zsub("g1", "仲裁", once=False))
    real_cur = push_mod.arbi.current
    slots = iter(
        [
            {
                "key": "k1",
                "line": "甲",
                "tier": "A",
                "start": 0,
                "end": 4102444800,
                "mission": "防御",
            },
            {
                "key": "k2",
                "line": "乙",
                "tier": "A-",
                "start": 1,
                "end": 4102444800,
                "mission": "防御",
            },
        ]
    )

    async def _cur(client, ts=None):
        return next(slots)

    push_mod.arbi.current = _cur
    try:
        await d.tick()
        await d.tick()
    finally:
        push_mod.arbi.current = real_cur
    msgs = [b[1] for b in box if "仲裁已轮换" in b[1]]
    check(
        "★ 仲裁轮换推送附评分范围",
        len(msgs) == 1 and push_mod.arbi.TIER_RANGE in msgs[0] and "评级 A-" in msgs[0],
        str(msgs),
    )


_real_ln2 = push_mod._local_now
push_mod._local_now = _ALWAYS
try:
    asyncio.run(z_arbi_expired())
    asyncio.run(z_arbi_tier_note())
finally:
    push_mod._local_now = _real_ln2

print()
if FAILED:
    print(f"共 {len(FAILED)} 项失败：{FAILED}")
    sys.exit(1)
print("全部通过 ✔")
