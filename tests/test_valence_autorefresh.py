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
    print(f"[{'PASS' if cond else 'FAIL'}] {name}"
          + (f"  -> {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


mod = importlib.import_module("core.api_client")
WC = mod.WarframeClient
ROT = json.loads((ROOT / "core" / "data" / "rotations.json")
                 .read_text(encoding="utf-8"))

T_EPOCH = datetime.fromisoformat(ROT["tenet"]["epoch"])
C_EPOCH = datetime.fromisoformat(ROT["coda"]["epoch"])
PERIOD = int(ROT["tenet"]["period_hours"])

# ---------------------------------------------------------------- 门限
# 取信条刚换轮、终幕仍在本轮的那一刻（2026-09-20 01:30 UTC）—— 正是旧代码
# 会误判成 fresh 的时刻。
NOW = T_EPOCH + timedelta(hours=PERIOD) * 2 + timedelta(minutes=90)
check("取样时刻：信条已进入新轮次", (NOW - T_EPOCH) // timedelta(hours=PERIOD) == 2)
check("取样时刻：终幕仍在旧轮次内（窗口起于 09-17）",
      (C_EPOCH + timedelta(hours=PERIOD) * 1).isoformat()
      == "2026-09-17T00:00:00+00:00")

_tenet_win = T_EPOCH + timedelta(hours=PERIOD) * 2
_coda_win = C_EPOCH + timedelta(hours=PERIOD) * 1

# 终幕新鲜（快照在 09-17 之后）、信条过期（快照停在 09-17）
_coda_fresh = dict(ROT["coda"])
_coda_fresh["valence_snapshot"] = (_coda_win + timedelta(hours=12)).isoformat()
_tenet_stale = dict(ROT["tenet"])
_tenet_stale["valence_snapshot"] = (_tenet_win - timedelta(days=3)).isoformat()

check("终幕那段判定为新鲜", WC.valence_is_stale(_coda_fresh, NOW) is False)
check("信条那段判定为过期", WC.valence_is_stale(_tenet_stale, NOW) is True)
check("快照缺失 = 过期（宁可重抓）",
      WC.valence_is_stale({"epoch": ROT["tenet"]["epoch"],
                           "period_hours": PERIOD}, NOW) is True)
check("快照时间串坏掉 = 过期",
      WC.valence_is_stale({"epoch": ROT["tenet"]["epoch"],
                           "period_hours": PERIOD,
                           "valence_snapshot": "not-a-time"}, NOW) is True)
# 两段都新鲜才该返回 fresh —— 用 any() 组合，不是遇到第一个就 return
check("★ 一段过期就整体不算 fresh（旧 bug：终幕新鲜会吞掉信条）",
      any(WC.valence_is_stale(s, NOW) for s in (_tenet_stale, _coda_fresh)))
_tenet_fresh = dict(ROT["tenet"])
_tenet_fresh["valence_snapshot"] = (_tenet_win + timedelta(hours=1)).isoformat()
check("两段都新鲜才算 fresh",
      not any(WC.valence_is_stale(s, NOW)
              for s in (_tenet_fresh, _coda_fresh)))

# 源码相位：refresh_valence 里不许再出现「遍历到第一个新鲜就 return」
_src = inspect.getsource(WC.refresh_valence)
check("refresh_valence 用 any(...) 汇总各段是否过期", "any(" in _src)
_loops = [i for i, ln in enumerate(_src.splitlines()) if "return \"fresh\"" in ln]
check("refresh_valence 不在逐段循环里 return fresh",
      len(_loops) == 1 and "any(" in _src.splitlines()[_loops[0] - 1],
      str(_src.splitlines()[max(0, (_loops or [0])[0] - 1):(_loops or [1])[0] + 1]))

# ---------------------------------------------------------------- 锚点
# 独立真值：按 main.py 的显示公式枚举，找出能显示出「观测批」的那个 anchor
def _true_anchor(observed: int, now: datetime) -> int:
    passed = int((now - C_EPOCH) // timedelta(hours=PERIOD))
    n = len(ROT["coda"]["batches"])
    cands = [a for a in range(n) if (a + passed) % n == observed]
    assert len(cands) == 1, cands
    return cands[0]


for _obs, _when in ((1, NOW),                                  # 现在：B 批
                    (0, NOW + timedelta(hours=PERIOD)),        # 下次换轮后：A 批
                    (1, NOW + timedelta(hours=PERIOD) * 3)):   # 再往后
    _got = WC.coda_anchor_for(_obs, C_EPOCH, PERIOD, _when,
                              len(ROT["coda"]["batches"]))
    check(f"观测 {ROT['coda']['batch_label'][_obs]} 批 @ {_when:%m-%d} "
          f"→ anchor={_got}（反解真值 {_true_anchor(_obs, _when)}）",
          _got == _true_anchor(_obs, _when), str(_got))
    # 回归：旧实现的错值就是 observed 本身
    check("  ≠ 旧实现的错值（直接存观测下标）", _got != _obs or _true_anchor(_obs, _when) == _obs)

# 现在这个具体时刻：观测 B 批 → anchor 必须是 0（文件里也是 0）
check("★ 2026-09-20 观测 B 批时 anchor 仍为 0（与 rotations.json 一致）",
      WC.coda_anchor_for(1, C_EPOCH, PERIOD, NOW, 2) == ROT["coda"]["anchor_idx"],
      str(WC.coda_anchor_for(1, C_EPOCH, PERIOD, NOW, 2)))

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
        def info(self, *a, **k): pass
        def warning(self, *a, **k): pass
        def error(self, *a, **k): pass
        def exception(self, *a, **k): pass
        def debug(self, *a, **k): pass

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
        event_message_type = staticmethod(lambda spec: (lambda fn: fn))

    event_mod.AstrMessageEvent = AstrMessageEvent
    event_mod.MessageChain = MessageChain
    event_mod.EventMessageType = _EventMessageType
    event_mod.event_message_type = lambda spec: (lambda fn: fn)
    event_mod.filter = _Filter()

    mc_mod.Image = type("Image", (), {})
    mc_mod.Plain = type("Plain", (), {})

    class Context:
        pass

    class Star:
        def __init__(self, *a, **k): pass

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
    def flare_enabled(self): return self._enabled

    async def flare_reachable(self, timeout: float = 6.0):
        self.calls.append("probe")
        if self._boom:
            raise RuntimeError("probe boom")
        return self._reachable

    async def recycle_flare_session(self): self.calls.append("recycle"); return True
    async def refresh_valence(self): self.calls.append("valence"); return "fresh"
    async def refresh_wiki_disp(self): self.calls.append("disp"); return "fresh"
    async def refresh_acrichis_week(self): self.calls.append("acrichis"); return "none"


def _flare_checks():
    import asyncio
    from types import SimpleNamespace

    async def phase_cases():
        for enabled, reachable, want in ((False, True, "off"),
                                         (True, True, "ready"),
                                         (True, False, "absent")):
            obj = SimpleNamespace(client=_StubFlareClient(enabled, reachable))
            got = await P.WarframeSDJK._flare_phase(obj)
            check(f"_flare_phase(enabled={enabled}, reachable={reachable}) == {want!r}",
                  got == want, got)
        obj = SimpleNamespace(client=_StubFlareClient(True, True, boom=True))
        check("_flare_phase 探测异常 → absent（保守：不误报真故障）",
              await P.WarframeSDJK._flare_phase(obj) == "absent")

    asyncio.run(phase_cases())

    # 降频：首次告警 + 24h 内不重复；超 24h 再提示
    rec: list[str] = []

    class _Rec:
        def info(self, *a, **k): pass
        def error(self, *a, **k): pass
        def exception(self, *a, **k): pass
        def debug(self, *a, **k): pass
        def warning(self, *a, **k): rec.append(str(a[0]) if a else "")

    old = P.logger
    P.logger = _Rec()
    try:
        obj = SimpleNamespace()
        P.WarframeSDJK._warn_flare_absent_once(obj)
        P.WarframeSDJK._warn_flare_absent_once(obj)          # 24h 内第二次
        check("不可达提示降频：首次告警、24h 内不重复", len(rec) == 1, str(len(rec)))
        obj._flare_absent_warned_at -= 24 * 3600 + 1
        P.WarframeSDJK._warn_flare_absent_once(obj)
        check("不可达提示：超过 24h 再提示一次", len(rec) == 2, str(len(rec)))
        check("提示文案含两条处置路径（关闭 / 部署地址）",
              "关闭" in rec[0] and "172.17.0.1" in rec[0], rec[0][:60])
    finally:
        P.logger = old

    # 源码接线：三态必须先判定，刷新只在 ready 分支里
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    i = src.index("async def _valence_autoloop")
    body = src[i:i + 5200]
    check("巡检循环按 _flare_phase 分流，且刷新在判定之后才发生",
          "phase = await self._flare_phase()" in body
          and 'if phase != "ready":' in body
          and body.index("phase = await self._flare_phase()") < body.index("refresh_valence"))
    check("不可达提示只在 absent 分支调用（off 不告警）",
          'if phase == "absent":' in body and "_warn_flare_absent_once()" in body)
    check("★ 真故障告警未被静默（可达时刷新失败仍 WARN）",
          'logger.warning("[sdjk] 元素加成快照刷新失败' in body
          and 'logger.warning("[sdjk] 变体倾向表刷新失败' in body)


_flare_checks()

print()
if FAILED:
    print(f"✗ {len(FAILED)} 项失败：" + "、".join(FAILED))
    sys.exit(1)
print("✓ 全部通过")
