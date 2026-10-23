# -*- coding: utf-8 -*-
"""插件内置效价自动刷新的门限与锚点（python3 tests/test_valence_autorefresh.py）

背景（2026-09-20 实测事故）
--------------------------
服务器上 FlareSolverr 是通的（容器内 ``172.17.0.1:8191`` 返回 405），
``main.py::_valence_autoloop`` 每 6 小时也会调 ``refresh_valence()``，
但装着的 rotations.json 一直停在 2026-09-17 的旧值，plugin_data 下从没
写出过 rotations.json，日志里一条刷新记录都没有 —— **自动刷新在空转**。

两个根因，这里各钉一颗钉子：

1. **新鲜度门限**：旧写法是「遍历 tenet/coda，遇到第一个新鲜的就
   ``return "fresh"``」。但信条与终幕的换轮锚点**相差 24 小时**，任何时刻
   都必然有一段仍在本轮窗口内 → 永远返回 fresh，永远不抓。
2. **终幕锚点**：``anchor_idx`` 是**相位基准**（卡面算的是
   ``(anchor_idx + 换轮次数) % 批数``），旧代码却直接存「wiki 上观测到的
   当前批下标」，换轮次数一前进就整体错位一批。

真值来源：门限用 rotations.json 里真实的 epoch/period 复算；
锚点用 main.py 的显示公式**反解**（枚举所有 anchor，取能显示出观测批的那个），
不是照抄实现里的表达式。

2026-10-01 追加文末「boot warm」段：`_boot_warm` 把 `self.wm_items()` /
`self.wm_riven_weapons()` 挂错对象（方法在 `self.client` 上），AttributeError
被 except 静默吞掉 ⇒ 预热从未生效 —— 用假客户端 + 假 logger 的调用序列钉死。
"""

from __future__ import annotations

import importlib
import inspect
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = ""):
    print(
        f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -> {detail}" if detail and not cond else "")
    )
    if not cond:
        FAILED.append(name)


mod = importlib.import_module("core.api_client")
WC = mod.WarframeClient
ROT = json.loads((ROOT / "core" / "data" / "rotations.json").read_text(encoding="utf-8"))

T_EPOCH = datetime.fromisoformat(ROT["tenet"]["epoch"])
C_EPOCH = datetime.fromisoformat(ROT["coda"]["epoch"])
PERIOD = int(ROT["tenet"]["period_hours"])

# ---------------------------------------------------------------- 门限
# 取信条刚换轮、终幕仍在本轮的那一刻（2026-09-20 01:30 UTC）—— 正是旧代码
# 会误判成 fresh 的时刻。
NOW = T_EPOCH + timedelta(hours=PERIOD) * 2 + timedelta(minutes=90)
check("取样时刻：信条已进入新轮次", (NOW - T_EPOCH) // timedelta(hours=PERIOD) == 2)
check(
    "取样时刻：终幕仍在旧轮次内（窗口起于 09-17）",
    (C_EPOCH + timedelta(hours=PERIOD) * 1).isoformat() == "2026-09-17T00:00:00+00:00",
)

_tenet_win = T_EPOCH + timedelta(hours=PERIOD) * 2
_coda_win = C_EPOCH + timedelta(hours=PERIOD) * 1

# 终幕新鲜（快照在 09-17 之后）、信条过期（快照停在 09-17）
_coda_fresh = dict(ROT["coda"])
_coda_fresh["valence_snapshot"] = (_coda_win + timedelta(hours=12)).isoformat()
_tenet_stale = dict(ROT["tenet"])
_tenet_stale["valence_snapshot"] = (_tenet_win - timedelta(days=3)).isoformat()

check("终幕那段判定为新鲜", WC.valence_is_stale(_coda_fresh, NOW) is False)
check("信条那段判定为过期", WC.valence_is_stale(_tenet_stale, NOW) is True)
check(
    "快照缺失 = 过期（宁可重抓）",
    WC.valence_is_stale({"epoch": ROT["tenet"]["epoch"], "period_hours": PERIOD}, NOW) is True,
)
check(
    "快照时间串坏掉 = 过期",
    WC.valence_is_stale(
        {"epoch": ROT["tenet"]["epoch"], "period_hours": PERIOD, "valence_snapshot": "not-a-time"},
        NOW,
    )
    is True,
)
check(
    "V01：快照时间在未来（> now + 1 天）= 过期（不能永远算本轮新鲜）",
    WC.valence_is_stale(
        dict(ROT["tenet"], valence_snapshot=(NOW + timedelta(days=30)).isoformat()), NOW
    )
    is True
    and WC.valence_is_stale(
        dict(ROT["tenet"], valence_snapshot=(NOW + timedelta(hours=2)).isoformat()), NOW
    )
    is False,
)
# 两段都新鲜才该返回 fresh —— 用 any() 组合，不是遇到第一个就 return
check(
    "★ 一段过期就整体不算 fresh（旧 bug：终幕新鲜会吞掉信条）",
    any(WC.valence_is_stale(s, NOW) for s in (_tenet_stale, _coda_fresh)),
)
_tenet_fresh = dict(ROT["tenet"])
_tenet_fresh["valence_snapshot"] = (_tenet_win + timedelta(hours=1)).isoformat()
check(
    "两段都新鲜才算 fresh",
    not any(WC.valence_is_stale(s, NOW) for s in (_tenet_fresh, _coda_fresh)),
)

# 源码相位：refresh_valence 里不许再出现「遍历到第一个新鲜就 return」
_src = inspect.getsource(WC.refresh_valence)
check("refresh_valence 用 any(...) 汇总各段是否过期", "any(" in _src)
_loops = [i for i, ln in enumerate(_src.splitlines()) if 'return "fresh"' in ln]
check(
    "refresh_valence 不在逐段循环里 return fresh",
    # 2026-10-17：any(...) 与 return 之间多了一行 _note_comm（社区快照采用记录）；
    # 2026-10-18：any(...) 里加了「数值未更新」判据、拆成多行 ⇒ 往上看 8 行
    len(_loops) == 1 and "any(" in "\n".join(_src.splitlines()[_loops[0] - 8 : _loops[0]]),
    str(_src.splitlines()[max(0, (_loops or [0])[0] - 1) : (_loops or [1])[0] + 1]),
)


# ---------------------------------------------------------------- 锚点
# 独立真值：按 main.py 的显示公式枚举，找出能显示出「观测批」的那个 anchor
def _true_anchor(observed: int, now: datetime) -> int:
    passed = int((now - C_EPOCH) // timedelta(hours=PERIOD))
    n = len(ROT["coda"]["batches"])
    cands = [a for a in range(n) if (a + passed) % n == observed]
    assert len(cands) == 1, cands
    return cands[0]


for _obs, _when in (
    (1, NOW),  # 现在：B 批
    (0, NOW + timedelta(hours=PERIOD)),  # 下次换轮后：A 批
    (1, NOW + timedelta(hours=PERIOD) * 3),
):  # 再往后
    _got = WC.coda_anchor_for(_obs, C_EPOCH, PERIOD, _when, len(ROT["coda"]["batches"]))
    check(
        f"观测 {ROT['coda']['batch_label'][_obs]} 批 @ {_when:%m-%d} "
        f"→ anchor={_got}（反解真值 {_true_anchor(_obs, _when)}）",
        _got == _true_anchor(_obs, _when),
        str(_got),
    )
    # 回归：旧实现的错值就是 observed 本身
    check("  ≠ 旧实现的错值（直接存观测下标）", _got != _obs or _true_anchor(_obs, _when) == _obs)

# 现在这个具体时刻：观测 B 批 → anchor 必须是 0（文件里也是 0）
check(
    "★ 2026-09-20 观测 B 批时 anchor 仍为 0（与 rotations.json 一致）",
    WC.coda_anchor_for(1, C_EPOCH, PERIOD, NOW, 2) == ROT["coda"]["anchor_idx"],
    str(WC.coda_anchor_for(1, C_EPOCH, PERIOD, NOW, 2)),
)

# ★ 2026-10-07 加强：用**四组外部观测记录**钉住相位（日期无关，且**不是自指** ——
#   期望值来自观测记录，不是从文件里的 anchor 反算）。任何一组对不上都说明
#   anchor 被写错了（这正是 10-07 00:09 那次自动刷新犯的错）。观测来源：
#     09-14 / 09-20 换批实测；10-03 刷新件注释；10-07 本批 —— Reset 页可见文案
#     「Eleanor is selling Batch A weapons. Time left until Batch B:」+ 表头
#     「Weapon (Batch A)」+ 表内为 A 批新值，倒计时精确指向 2026-10-11 00:00 UTC。
#   ⚠ 别照抄服务器运行副本的 anchor：那次读渲染页时其**批次标签还滞后于换轮边界**
#     （页面仍写 B）⇒ 相位被写错一批（并连带 pop 掉了 A 批的值）。
_OBS = (
    ("09-14", 0, datetime(2026, 9, 14, 12, tzinfo=C_EPOCH.tzinfo)),
    ("09-20", 1, datetime(2026, 9, 20, 12, tzinfo=C_EPOCH.tzinfo)),
    ("10-03", 1, datetime(2026, 10, 3, 12, tzinfo=C_EPOCH.tzinfo)),
    ("10-07", 0, datetime(2026, 10, 7, 12, tzinfo=C_EPOCH.tzinfo)),
)
for _tag, _obs, _when in _OBS:
    _a = WC.coda_anchor_for(_obs, C_EPOCH, PERIOD, _when, 2)
    check(
        f"★ 相位钉值：{_tag} 观测 {ROT['coda']['batch_label'][_obs]} 批 → 反解 anchor={_a}"
        f" 应等于文件值 {ROT['coda']['anchor_idx']}",
        _a == ROT["coda"]["anchor_idx"],
        str(_a),
    )

# ---------------------------------------------------------------------------
# FS 告警分级（v1.0.8；issue #1 实测：不用 FS 的用户更新后连收刷新失败 WARN）
# 三态：配置关闭→不尝试不告警；开着但不可达→24h 一次提示且跳过；可达但求解失败→照旧 WARN
# ---------------------------------------------------------------------------
import types  # noqa: E402


def _install_astrbot_stub_min() -> None:
    """最小 AstrBot 桩（只为 import main；与 tests/test_relic_list.py 同一配方）。"""
    if "astrbot" in sys.modules:
        return
    pkg = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    event_mod = types.ModuleType("astrbot.api.event")
    mc_mod = types.ModuleType("astrbot.api.message_components")
    star_mod = types.ModuleType("astrbot.api.star")

    class AstrBotConfig(dict):
        pass

    class _Logger:
        def info(self, *a, **k):
            pass

        def warning(self, *a, **k):
            pass

        def error(self, *a, **k):
            pass

        def exception(self, *a, **k):
            pass

        def debug(self, *a, **k):
            pass

    class AstrMessageEvent:
        def __init__(self, umo: str = "group://test", sender: str = "tester"):
            self.unified_msg_origin = umo
            self._sender = sender

        def get_sender_name(self) -> str:
            return self._sender

    class MessageChain:
        def message(self, text):
            return text

    class _EventMessageType:
        ALL = "ALL"

    class _Filter:
        EventMessageType = _EventMessageType
        event_message_type = staticmethod(lambda spec: lambda fn: fn)

    event_mod.AstrMessageEvent = AstrMessageEvent
    event_mod.MessageChain = MessageChain
    event_mod.EventMessageType = _EventMessageType
    event_mod.event_message_type = lambda spec: lambda fn: fn
    event_mod.filter = _Filter()

    mc_mod.Image = type("Image", (), {})
    mc_mod.Plain = type("Plain", (), {})

    class Context:
        pass

    class Star:
        def __init__(self, *a, **k):
            pass

    def register(*a, **k):
        def deco(cls):
            return cls

        return deco

    star_mod.Context = Context
    star_mod.Star = Star
    star_mod.register = register
    api.AstrBotConfig = AstrBotConfig
    api.logger = _Logger()
    api.event = event_mod
    api.message_components = mc_mod
    api.star = star_mod
    sys.modules["astrbot"] = pkg
    sys.modules["astrbot.api"] = api
    sys.modules["astrbot.api.event"] = event_mod
    sys.modules["astrbot.api.message_components"] = mc_mod
    sys.modules["astrbot.api.star"] = star_mod


_install_astrbot_stub_min()
import importlib as _il  # noqa: E402

P = _il.import_module("main")


class _StubFlareClient:
    """只记调用序列的 FS 桩。"""

    def __init__(self, enabled: bool, reachable: bool, boom: bool = False):
        self._enabled, self._reachable, self._boom = enabled, reachable, boom
        self.calls: list[str] = []

    @property
    def flare_enabled(self):
        return self._enabled

    async def flare_reachable(self, timeout: float = 6.0):
        self.calls.append("probe")
        if self._boom:
            raise RuntimeError("probe boom")
        return self._reachable

    async def recycle_flare_session(self):
        self.calls.append("recycle")
        return True

    async def refresh_valence(self):
        self.calls.append("valence")
        return "fresh"

    async def refresh_acrichis_week(self):
        self.calls.append("acrichis")
        return "none"


def _flare_checks():
    import asyncio
    from types import SimpleNamespace

    async def phase_cases():
        for enabled, reachable, want in (
            (False, True, "off"),
            (True, True, "ready"),
            (True, False, "absent"),
        ):
            obj = SimpleNamespace(client=_StubFlareClient(enabled, reachable))
            got = await P.WarframeSDJK._flare_phase(obj)
            check(
                f"_flare_phase(enabled={enabled}, reachable={reachable}) == {want!r}",
                got == want,
                got,
            )
        obj = SimpleNamespace(client=_StubFlareClient(True, True, boom=True))
        check(
            "_flare_phase 探测异常 → absent（保守：不误报真故障）",
            await P.WarframeSDJK._flare_phase(obj) == "absent",
        )

    asyncio.run(phase_cases())

    # 降频：首次告警 + 24h 内不重复；超 24h 再提示
    rec: list[str] = []

    class _Rec:
        def info(self, *a, **k):
            pass

        def error(self, *a, **k):
            pass

        def exception(self, *a, **k):
            pass

        def debug(self, *a, **k):
            pass

        def warning(self, *a, **k):
            rec.append(str(a[0]) if a else "")

    old = P.logger
    P.logger = _Rec()
    try:
        obj = SimpleNamespace()
        P.WarframeSDJK._warn_flare_absent_once(obj)
        P.WarframeSDJK._warn_flare_absent_once(obj)  # 24h 内第二次
        check("不可达提示降频：首次告警、24h 内不重复", len(rec) == 1, str(len(rec)))
        obj._flare_absent_warned_at -= 24 * 3600 + 1
        P.WarframeSDJK._warn_flare_absent_once(obj)
        check("不可达提示：超过 24h 再提示一次", len(rec) == 2, str(len(rec)))
        check(
            "提示文案含两条处置路径（关闭 / 部署地址）",
            "关闭" in rec[0] and "172.17.0.1" in rec[0],
            rec[0][:60],
        )
    finally:
        P.logger = old

    # 源码接线：三态必须先判定，刷新只在 ready 分支里
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    i = src.index("async def _valence_autoloop")
    body = src[i : i + 5200]
    check(
        "巡检循环按 _flare_phase 分流，且刷新在判定之后才发生",
        "phase = await self._flare_phase()" in body
        and 'if phase != "ready":' in body
        and body.index("phase = await self._flare_phase()") < body.index("refresh_valence"),
    )
    check(
        "不可达提示只在 absent 分支调用（off 不告警）",
        'if phase == "absent":' in body and "_warn_flare_absent_once()" in body,
    )
    check(
        "★ 真故障告警未被静默（可达时刷新失败仍 WARN）；倾向表重抓已退役",
        'logger.warning("[sdjk] 元素加成快照刷新失败' in body
        and "refresh_wiki_disp" not in body,  # 2026-10 倾向取消：倾向表定时重抓已退役
    )


# ---------------------------------------------------------------------------
# ★ 2026-10-01 事故：`_boot_warm` 预热挂错对象 → 从未生效（reload 后首条
#   wr/wm 指令仍要现下 2.5MB 物品表 + swap-in 风暴，Event loop lag 45s）。
#   `wm_items()` / `wm_riven_weapons()` 挂在 WarframeClient（`self.client`）上，
#   插件类没有 __getattr__ ⇒ `self.wm_items()` 抛 AttributeError，被
#   `except Exception` 静默吞掉（DEBUG 级不可见）⇒「已就绪」日志永不出现。
#   修复前：本段第一条断言即失败（假客户端调用序列为空）。
# ---------------------------------------------------------------------------
def _boot_warm_checks():
    import asyncio
    from types import SimpleNamespace

    class _FakeClient:
        """只记调用序列的假 WarframeClient（覆盖 _boot_warm 用到的两个方法）。"""

        def __init__(self):
            self.calls: list[str] = []

        async def wm_items(self):
            self.calls.append("items")
            return []

        async def wm_riven_weapons(self):
            self.calls.append("riven")
            return []

    class _RecLogger:
        """记录 (level, message)；swap 进 P.logger 用。"""

        def __init__(self):
            self.records: list[tuple[str, str]] = []

        def _rec(self, level):
            def _f(fmt, *a):
                self.records.append((level, fmt % a if a else fmt))

            return _f

        def __getattr__(self, name):
            if name in ("info", "warning", "debug", "error", "exception"):
                return self._rec(name)
            raise AttributeError(name)

        def has(self, level: str, needle: str) -> bool:
            return any(lv == level and needle in msg for lv, msg in self.records)

    async def _ok():
        return None

    async def _boom():
        raise RuntimeError("snapshot down")

    static_calls: list[str] = []

    async def _static():
        static_calls.append("static")

    async def _warm(client, refresh) -> _RecLogger:
        obj = SimpleNamespace(
            client=client, _refresh_from_community=refresh, _refresh_static_community=_static
        )
        rec = _RecLogger()
        old = P.logger
        P.logger = rec
        try:
            await P.WarframeSDJK._boot_warm(obj)
        finally:
            P.logger = old
        return rec

    async def cases():
        # ① 正常：必须真的打到 client 上（修复前 calls == []，必失败）
        c1 = _FakeClient()
        rec1 = await _warm(c1, _ok)
        check(
            "★ boot warm 打到 client 上（items → riven）",
            c1.calls == ["items", "riven"],
            repr(c1.calls),
        )
        check(
            "成功记 INFO「已就绪」（与线上验收判据同源）",
            rec1.has("info", "首启预热：WM 物品/紫卡武器表已就绪"),
            repr(rec1.records),
        )

        # ② 两段各自独立：第一段抛异常，第二段仍要执行
        c2 = _FakeClient()
        rec2 = await _warm(c2, _boom)
        check(
            "★ 第一段异常不阻断第二段（calls 仍完整）",
            c2.calls == ["items", "riven"],
            repr(c2.calls),
        )
        check(
            "★ 第一段失败记 WARNING 且带异常类型（%r；%s 只有消息文本）",
            rec2.has("warning", "RuntimeError"),
            repr(rec2.records),
        )
        check(
            "★ 2026-10-16：动态社区快照失败不连累静态快照（逐项独立）",
            static_calls.count("static") == 2,
            repr(static_calls),
        )

        async def _hang():
            await asyncio.sleep(3600)

        old_wait = asyncio.wait_for

        async def _fast_wait(coro, timeout):  # 把 90 s 限时压到 0.05 s，验证超时也只跳过该项
            return await old_wait(coro, 0.05)

        P.asyncio.wait_for = _fast_wait
        try:
            c4 = _FakeClient()
            rec4 = await _warm(c4, _hang)
        finally:
            P.asyncio.wait_for = old_wait
        check(
            "★ 动态社区快照超时：静态快照与 WM 预热照常执行",
            static_calls.count("static") == 3 and c4.calls == ["items", "riven"],
            repr((static_calls, c4.calls)),
        )
        check(
            "超时记 WARNING 带 TimeoutError",
            rec4.has("warning", "TimeoutError"),
            repr(rec4.records),
        )

        # ③ WM 预热失败可见：WARNING 带类型，且不冒充「已就绪」
        class _BrokenItems(_FakeClient):
            async def wm_items(self):
                self.calls.append("items")
                raise AttributeError("'WarframeSDJK' object has no attribute 'wm_items'")

        c3 = _BrokenItems()
        rec3 = await _warm(c3, _ok)
        check(
            "★ WM 预热失败记 WARNING 且带异常类型",
            rec3.has("warning", "AttributeError"),
            repr(rec3.records),
        )
        check(
            "失败时不得出现「已就绪」INFO（日志不撒谎）",
            not rec3.has("info", "已就绪"),
            repr(rec3.records),
        )

        # 源码接线：预热必须走 self.client.*（不得再写裸 self.wm_items）
        # 注意：docstring 里为说明事故会引用旧写法，检查前先剥掉 docstring
        body = inspect.getsource(P.WarframeSDJK._boot_warm)
        code = body.split('"""', 2)[2] if body.count('"""') >= 2 else body
        check(
            "★ 源码接线：self.client.wm_items() / wm_riven_weapons()",
            # 2026-10-16 起逐项表驱动，传的是方法本身（不带括号）；行为见上面的调用序列用例
            "self.client.wm_items" in code
            and "self.client.wm_riven_weapons" in code
            and "self.wm_items" not in code,
            code[:120],
        )

    asyncio.run(cases())


_flare_checks()
_boot_warm_checks()


# ---------------------------------------------------------------------------
# ★ 2026-10-18：换轮后 wiki 还没人更新 ⇒ 抓到的是上一轮数值，却被标成「本轮快照」
#   （信条 10-18 00:17 / 10-02 00:11 两次实测）。判据：当前在售那组数值最早出现的
#   时刻早于本轮窗口起点 ⇒ 数值已存在超过一个周期 ⇒ 未更新；后台必须继续重抓。
#   言录使同理：与上周货单完全相同 ⇒ 不落盘。
# ---------------------------------------------------------------------------
def _stale_value_checks():
    import asyncio
    import copy
    import tempfile
    from datetime import timezone

    from core import paths
    from core.commands.progress import ProgressCommands

    now = datetime.now(timezone.utc)

    def _win(sect):
        ep = datetime.fromisoformat(sect["epoch"])
        per = timedelta(hours=int(sect["period_hours"]))
        passed = int((now - ep) // per)
        return ep + per * passed, passed

    t_win, _ = _win(ROT["tenet"])
    c_win, c_passed = _win(ROT["coda"])
    cur_bi = (int(ROT["coda"].get("anchor_idx", 0)) + c_passed) % len(ROT["coda"]["batches"])
    prev = (min(t_win, c_win) - timedelta(hours=1)).isoformat()

    _ELEMS = ["Heat", "Cold", "Electricity", "Toxin", "Impact", "Magnetic", "Radiation"]

    def _shift(elem, on):
        """真实换轮时元素也会重新随机：bump≠0 时把元素轮换到下一个（keep_elem 模拟半更新）。"""
        return _ELEMS[(_ELEMS.index(elem) + 1) % len(_ELEMS)] if on and elem in _ELEMS else elem

    def _page(rot, bump_tenet=0.0, bump_coda=0.0, keep_elem=False):
        """模拟 wiki 源页解析结果；bump=0 即「页面还是上一轮的值」。"""
        return {
            "tenet": [
                {
                    "en": it["en"],
                    "element": _shift(it["element"], bump_tenet and not keep_elem),
                    "bonus": it["bonus"] + bump_tenet,
                }
                for it in rot["tenet"]["items"]
            ],
            "coda": {
                i + 1: [
                    {
                        "en": it["en"],
                        "element": _shift(
                            it["element"], i == cur_bi and bump_coda and not keep_elem
                        ),
                        "bonus": it["bonus"] + (bump_coda if i == cur_bi else 0.0),
                    }
                    for it in b
                ]
                for i, b in enumerate(rot["coda"]["batches"])
            },
        }

    async def _run(rot0, pages, revs=None):
        out = []
        with tempfile.TemporaryDirectory() as tmp:
            paths.set_run_dir(tmp)
            try:
                (Path(tmp) / "rotations.json").write_text(json.dumps(rot0), encoding="utf-8")
                c = WC()
                try:
                    for page in pages:

                        async def _fetch(url, ttl=0, _p=page):
                            return json.dumps(_p)

                        c.fetch_via_flaresolver = _fetch
                        c.parse_wikitext = lambda src: {
                            k: ({int(i): v for i, v in x.items()} if k == "coda" else x)
                            for k, x in json.loads(src).items()
                        }

                        async def _revs(url, what, _r=revs):
                            return dict(_r or {})

                        c._wiki_revisions = _revs
                        st = await c.refresh_valence()
                        rot = json.loads((Path(tmp) / "rotations.json").read_text("utf-8"))
                        out.append((st, rot))
                finally:
                    await c.close()
            finally:
                paths.set_run_dir(None)
        return out

    rot0 = copy.deepcopy(ROT)
    for k in ("tenet", "coda"):
        rot0[k]["valence_snapshot"] = prev  # 上一轮的快照
        rot0[k].pop("valence_values_since", None)
        rot0[k].pop("batch_values_since", None)

    # ① 换轮后 wiki 仍是旧值 → not-updated；② 再跑一次不能当 fresh 跳过；③ wiki 更新后恢复
    res = asyncio.run(_run(rot0, [_page(ROT), _page(ROT), _page(ROT, 1.5, 2.5)]))
    (s1, r1), (s2, _r2), (s3, r3) = res
    check("★ 换轮后数值与上一轮相同 → 返回 not-updated", s1.startswith("not-updated"), s1)
    check(
        "数值没变时 valence_values_since 沿用上一轮快照时刻",
        r1["tenet"].get("valence_values_since") == prev,
        str(r1["tenet"].get("valence_values_since")),
    )
    check("信条段判为数值未更新", WC.valence_values_stale(r1["tenet"], now))
    check("终幕当前批判为数值未更新", WC.valence_values_stale(r1["coda"], now))
    check("快照时刻本身在本轮（旧判据看不出问题）", not WC.valence_is_stale(r1["tenet"], now))
    check("★ 未更新时下一轮不当 fresh 跳过，继续重抓", s2 != "fresh", s2)
    check("wiki 更新后 → refreshed", s3.startswith("refreshed"), s3)
    check(
        "wiki 更新后两段都不再判未更新",
        not WC.valence_values_stale(r3["tenet"], now)
        and not WC.valence_values_stale(r3["coda"], now),
    )

    # ④ 只有终幕当前批没更新（信条已更新）→ 仍是 not-updated，且只点名终幕
    res = asyncio.run(_run(rot0, [_page(ROT, bump_tenet=1.5)]))
    (s4, r4) = res[0]
    check(
        "只有终幕当前批没更新 → not-updated，信条不误报",
        s4.startswith("not-updated")
        and not WC.valence_values_stale(r4["tenet"], now)
        and WC.valence_values_stale(r4["coda"], now),
        s4,
    )

    # ④½ 用 wiki 页面修订时间判（2026-10-18 第二道判据）
    before = {"Tenet Weapons": t_win - timedelta(days=3), "Coda Weapons": c_win - timedelta(days=3)}
    after = {
        "Tenet Weapons": t_win + timedelta(minutes=5),
        "Coda Weapons": c_win + timedelta(minutes=5),
    }
    s5, r5 = asyncio.run(_run(rot0, [_page(ROT, 1.5, 2.5)], before))[0]
    check(
        "★ 数值变了但页面最后修订早于本轮起点 → 仍判未更新（页面上是我们没见过的上一轮值）",
        s5.startswith("not-updated")
        and WC.valence_values_stale(r5["tenet"], now)
        and WC.valence_values_stale(r5["coda"], now),
        s5,
    )
    s6, r6 = asyncio.run(_run(rot0, [_page(ROT, 1.5, 2.5)], after))[0]
    check(
        "数值变了且页面在本轮修订过 → refreshed，since 取修订时刻",
        s6.startswith("refreshed")
        and r6["tenet"]["valence_values_since"] == after["Tenet Weapons"].isoformat(),
        f"{s6} {r6['tenet'].get('valence_values_since')}",
    )
    s7, r7 = asyncio.run(_run(rot0, [_page(ROT)], after))[0]
    check(
        "页面在本轮有修订但数值没变（无关编辑）→ 仍判未更新",
        s7.startswith("not-updated") and WC.valence_values_stale(r7["tenet"], now),
        s7,
    )
    # ★ 线上 10-18 的真实情形：升级前的数据没有记录、快照时刻在本轮、数值与页面相同、页面停在上一轮
    legacy = copy.deepcopy(ROT)
    for k in ("tenet", "coda"):
        legacy[k]["valence_snapshot"] = (max(t_win, c_win) + timedelta(minutes=17)).isoformat()
        legacy[k].pop("valence_values_since", None)
        legacy[k].pop("batch_values_since", None)
    s8, r8 = asyncio.run(_run(legacy, [_page(ROT)], before))[0]
    check(
        "★ 升级前数据（无记录、快照在本轮）不当 fresh 跳过，补记录后认出未更新",
        s8.startswith("not-updated")
        and r8["tenet"]["valence_values_since"] == before["Tenet Weapons"].isoformat(),
        f"{s8} {r8['tenet'].get('valence_values_since')}",
    )
    s9, r9 = asyncio.run(_run(legacy, [_page(ROT)], {}))[0]
    check(
        "修订时间查不到时：升级前数据补记录沿用旧快照时刻（不误报）",
        s9.startswith("refreshed") and not WC.valence_values_stale(r9["tenet"], now),
        s9,
    )

    # ④¾ 面板「从源头重抓」：本地已在本轮且有记录（平时直接 fresh）→ force 照样重抓
    async def _forced(rot_in, page, revs_in):
        with tempfile.TemporaryDirectory() as tmp:
            paths.set_run_dir(tmp)
            try:
                (Path(tmp) / "rotations.json").write_text(json.dumps(rot_in), encoding="utf-8")
                c = WC()
                fetched = []

                async def _fetch(url, ttl=0):
                    fetched.append(ttl)
                    return json.dumps(page)

                async def _revs(url, what):
                    return dict(revs_in)

                c.fetch_via_flaresolver = _fetch
                c.parse_wikitext = lambda src: {
                    k: ({int(i): v for i, v in x.items()} if k == "coda" else x)
                    for k, x in json.loads(src).items()
                }
                c._wiki_revisions = _revs
                try:
                    plain = await c.refresh_valence()
                    forced = await c.refresh_valence(force=True)
                finally:
                    await c.close()
                return plain, forced, fetched
            finally:
                paths.set_run_dir(None)

    plain, forced, fetched = asyncio.run(_forced(r6, _page(ROT, 4.0, 5.0), after))
    check("本地已在本轮：平时返回 fresh、不抓", plain == "fresh", plain)
    check(
        "★ force=True：跳过新鲜门照样重抓，且不吃 10 分钟页面缓存",
        forced.startswith("refreshed") and fetched and all(t == 0 for t in fetched),
        f"{forced} ttl={fetched}",
    )

    # ④⅞ ★ 2026-10-18 半更新：wiki 只改了百分比、元素列原样是上一轮（页面修订在本轮内）→ 仍判未更新
    sh, rh = asyncio.run(_run(rot0, [_page(ROT, 1.5, 2.5, keep_elem=True)], after))[0]
    check(
        "★ 只改了百分比、元素与上一轮逐把相同 → not-updated（半更新）",
        sh.startswith("not-updated")
        and WC.valence_values_stale(rh["tenet"], now)
        and WC.valence_values_stale(rh["coda"], now),
        sh,
    )
    check(
        "半更新时百分比照样写入（卡面标注疑似上一轮，等元素也更新后自动恢复）",
        rh["tenet"]["items"][0]["bonus"] == ROT["tenet"]["items"][0]["bonus"] + 1.5,
    )
    s2, r2b = asyncio.run(
        _run(
            rh,
            [
                {
                    **_page(rh),
                    "tenet": [
                        {**it, "element": _shift(it["element"], True)} for it in _page(rh)["tenet"]
                    ],
                }
            ],
            after,
        )
    )[0]
    check(
        "元素列随后也改好 → 信条恢复为已更新",
        not WC.valence_values_stale(r2b["tenet"], now),
        s2,
    )

    # ⑤ 旧数据没有 since 字段、快照在本轮 → 不误报
    plain = copy.deepcopy(ROT)
    plain["tenet"]["valence_snapshot"] = now.isoformat()
    plain["tenet"].pop("valence_values_since", None)
    check("没有 since 记录时不判未更新（不误报）", not WC.valence_values_stale(plain["tenet"], now))

    # ⑥ 卡面说明行
    note = ProgressCommands._valence_note(r1["tenet"], t_win)
    check("卡面标注「数值疑似仍是上一轮：wiki 尚未完整更新」", "wiki 尚未完整更新" in note, note)
    note3 = ProgressCommands._valence_note(r3["tenet"], t_win)
    check("更新后卡面恢复「本轮快照」", "（本轮快照）" in note3, note3)

    # ⑦ 言录使社区快照：与上周货单完全相同 → not-updated，不落盘
    acr = json.loads((ROOT / "core/data/de/acrichis_week.json").read_text("utf-8"))
    monday = (now - timedelta(days=now.weekday())).date().isoformat()

    async def _acr(items):
        with tempfile.TemporaryDirectory() as tmp:
            paths.set_run_dir(tmp)
            try:
                c = WC()

                async def _cj(urls):
                    return {
                        "expiry": (now + timedelta(days=3)).isoformat(),
                        "observed": monday,
                        "items": items,
                    }

                c._community_json = _cj
                try:
                    st = await c._community_acrithis_week(acr)
                finally:
                    await c.close()
                return st, (Path(tmp) / mod.ACRITHIS_WEEK_NAME).exists()
            finally:
                paths.set_run_dir(None)

    st, wrote = asyncio.run(_acr(acr["items"]))
    check(
        "★ 言录使：与上周货单完全相同 → not-updated 且不落盘", st == "not-updated" and not wrote, st
    )

    async def _acr_fresh(items):
        with tempfile.TemporaryDirectory() as tmp:
            paths.set_run_dir(tmp)
            try:
                c = WC()

                async def _cj(urls):
                    return {
                        "expiry": (now + timedelta(days=3)).isoformat(),
                        "observed": monday,
                        "items": items,
                    }

                c._community_json = _cj
                try:
                    return await c._community_acrithis_week(acr, expired=False)
                finally:
                    await c.close()
            finally:
                paths.set_run_dir(None)

    st = asyncio.run(_acr_fresh(acr["items"]))
    check("言录使强制重抓、本地未到期且货单相同 → 本地已是最新（不报未更新）", st == "fresh", st)
    st, wrote = asyncio.run(_acr(acr["items"][1:] + [{"name": "新的一件", "price": 1}]))
    check("言录使：货单有变化 → 正常采用（对照组）", st == "refreshed" and wrote, st)
    check(
        "言录使 wiki 抓取路径同样比对上周货单",
        "与上周货单完全相同" in inspect.getsource(WC.refresh_acrichis_week),
    )


_stale_value_checks()


# ---------------------------------------------------------------------------
# ★ 2026-10-18：WFHelper 可选通道（后台开关，默认关；开了排在社区快照前面）
# ---------------------------------------------------------------------------
def _wfhelper_checks():
    import asyncio
    import copy
    import tempfile

    from core import paths

    t_items = ROT["tenet"]["items"]
    b0, b1 = ROT["coda"]["batches"]
    labels = ROT["coda"].get("batch_label") or ["A", "B"]

    def _vend(items, bump=0.0):
        return [{"name": it["en"], "element": "Heat", "bonus": 30 + bump} for it in items]

    payload = {
        "ok": True,
        "source": "wiki",
        "tenet": {"items": _vend(t_items, 1)},
        "coda": {"batch": labels[0], "items": _vend(b0, 2)},
        "codaNext": {"batch": labels[1], "items": _vend(b1, 3)},
    }
    got = mod.parse_wfhelper_valence(payload)
    check(
        "WFHelper 解析：信条 5 件 + 终幕两批都收",
        len(got["tenet"]) == len(t_items) and set(got["coda"]) == set(labels[:2]),
        str(sorted(got["coda"])),
    )
    for bad, why in (
        ({**payload, "ok": False}, "ok≠true"),
        ({**payload, "tenet": {"items": []}}, "信条空"),
        ({**payload, "codaNext": {"items": payload["codaNext"]["items"]}}, "缺 batch"),
        (
            {**payload, "tenet": {"items": [{"name": "x", "element": "Heat", "bonus": True}]}},
            "bonus 是布尔",
        ),
    ):
        try:
            mod.parse_wfhelper_valence(bad)
            ok = False
        except ValueError:
            ok = True
        check(f"WFHelper 解析拒绝畸形输入（{why}）", ok)

    rot0 = copy.deepcopy(ROT)
    for k in ("tenet", "coda"):
        rot0[k]["valence_snapshot"] = "2020-01-01T00:00:00+00:00"

    async def _run(enabled, wf_ok=True):
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            paths.set_run_dir(tmp)
            try:
                (Path(tmp) / "rotations.json").write_text(json.dumps(rot0), encoding="utf-8")
                c = WC(wfhelper_valence=enabled)

                async def _wf():
                    calls.append("wfhelper")
                    return mod.parse_wfhelper_valence(payload) if wf_ok else None

                async def _comm(rot):
                    calls.append("community")
                    return None

                c._wfhelper_valence_parsed = _wf
                c._community_valence_parsed = _comm
                try:
                    st = await c.refresh_valence(community_only=True)
                finally:
                    await c.close()
                rot = json.loads((Path(tmp) / "rotations.json").read_text("utf-8"))
            finally:
                paths.set_run_dir(None)
        return st, calls, rot

    st, calls, rot = asyncio.run(_run(False))
    check("开关关闭（默认）：不调 WFHelper，只走社区快照", calls == ["community"], str(calls))
    st, calls, rot = asyncio.run(_run(True))
    check(
        "★ 开关打开：先用 WFHelper，成功就不再查社区快照",
        calls == ["wfhelper"] and "(wfhelper)" in st,
        f"{calls} {st}",
    )
    check(
        "WFHelper 数值写进信条与终幕两批，来源标注 WFHelper",
        rot["tenet"]["items"][0]["bonus"] == 31.0
        and rot["coda"]["batches"][0][0]["bonus"] == 32.0
        and rot["coda"]["batches"][1][0]["bonus"] == 33.0
        and "WFHelper" in rot["tenet"]["valence_source"],
        str(rot["tenet"].get("valence_source")),
    )
    st, calls, rot = asyncio.run(_run(True, wf_ok=False))
    check("WFHelper 取不到 → 回落社区快照", calls == ["wfhelper", "community"], str(calls))
    sch = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    check(
        "配置项 wfhelper_valence 存在且默认关闭",
        sch.get("wfhelper_valence", {}).get("default") is False,
    )


_wfhelper_checks()


# ---------------------------------------------------------------------------
# ★ 2026-10-18：指令「信条 刷新」「终幕 重抓」「言录使 更新」—— 立即从源头重抓，5 分钟冷却（管理员不受限）
# ---------------------------------------------------------------------------
def _pull_cmd_checks():
    import asyncio

    from core.commands.progress import ProgressCommands
    from core.commands.rotations import RotationCommands
    from core.parser import parse

    class _Cli:
        def __init__(self):
            self.calls = []

        async def refresh_valence(self, community_only=False, force=False):
            self.calls.append(("valence", force))
            return "refreshed 2026-10-18T02:20:00+00:00 batch=A"

        async def refresh_acrichis_week(self, community_only=False, force=False):
            self.calls.append(("acrichis", force))
            return "not-updated"

        async def acrithis_week(self):
            return {}

        async def acrithis_pool(self):
            return []

    class _Host(RotationCommands, ProgressCommands):
        def __init__(self, admin=False):
            self.client = _Cli()
            self._admin = admin

        def _is_admin(self, event):
            return self._admin

    def _run(host, text):
        p = parse(text)
        h = {
            "tenet": host._h_rotation_tenet,
            "coda": host._h_rotation_coda,
            "acrichis": host._h_acrichis,
        }[p.command]
        return asyncio.run(h(p, None, "pc"))

    h = _Host()
    r = _run(h, "信条")
    check("只发「信条」不重抓", h.client.calls == [] and not r.lines[0].startswith("※ 已从源头"))
    r = _run(h, "信条 刷新")
    check(
        "★「信条 刷新」→ 强制重抓元素加成，卡片首行写结果",
        h.client.calls == [("valence", True)] and r.lines[0].startswith("※ 已从源头重抓：已更新"),
        str(r.lines[:1]),
    )
    r = _run(h, "终幕 重抓")
    check(
        "★ 5 分钟内再次重抓（非管理员）→ 不抓，提示冷却并照常出卡",
        h.client.calls == [("valence", True)] and "分钟后可再试" in r.lines[0] and len(r.lines) > 1,
        str(r.lines[:1]),
    )
    r = _run(h, "言录使 更新")
    check(
        "言录使与元素加成各算各的冷却；wiki 未更新时如实说明",
        h.client.calls[-1] == ("acrichis", True) and "wiki 还没人更新" in r.lines[0],
        str(r.lines[:1]),
    )
    ha = _Host(admin=True)
    _run(ha, "信条 刷新")
    _run(ha, "信条 刷新")
    check(
        "群管理不受冷却限制",
        ha.client.calls == [("valence", True), ("valence", True)],
        str(ha.client.calls),
    )
    for _ in range(6):
        r = _run(ha, "信条 刷新")
    check(
        "★ 1 小时内同一项最多重抓 6 次，管理员也不例外",
        ha.client.calls.count(("valence", True)) == 6 and "上限 6 次" in r.lines[0],
        f"{len(ha.client.calls)} {r.lines[:1]}",
    )
    ha._pull_hist["valence"] = [t - 3600 for t in ha._pull_hist["valence"]]
    _run(ha, "信条 刷新")
    check("一小时窗口过去后恢复", ha.client.calls.count(("valence", True)) == 7)


_pull_cmd_checks()


print()
if FAILED:
    print(f"✗ {len(FAILED)} 项失败：" + "、".join(FAILED))
    sys.exit(1)
print("✓ 全部通过")
