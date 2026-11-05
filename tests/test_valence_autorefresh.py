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
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
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
# 固定历史离线夹具；业务种子不携带效价值，数值回归独立于发布数据。
_FIXTURE_META = {
    "tenet": {
        "valence_snapshot": "2026-11-03T00:07:03+00:00",
        "valence_source": "wiki 固定修订源页 wikitext",
        "valence_values_since": "2026-11-03T00:07:03+00:00",
    },
    "coda": {
        "valence_snapshot": "2026-10-31T00:13:26+00:00",
        "valence_source": "wiki 源页 wikitext（Coda_Weapons / Tenet_Weapons，经 FlareSolverr）",
        "batch_values_since": ["2026-10-31T00:13:15+00:00", "2026-10-27T00:49:37+00:00"],
    },
}
_FIXTURE_VALUES = {
    "Tenet Agendus": ("Toxin", 41.9),
    "Tenet Exec": ("Impact", 42.4),
    "Tenet Ferrox": ("Heat", 26.3),
    "Tenet Grigori": ("Heat", 26.3),
    "Tenet Livia": ("Magnetic", 34.0),
    "Coda Hema": ("Toxin", 50.0),
    "Coda Sporothrix": ("Impact", 48.7),
    "Coda Catabolyst": ("Electricity", 25.0),
    "Coda Pox": ("Impact", 30.9),
    "Dual Coda Torxica": ("Heat", 28.8),
    "Coda Mire": ("Toxin", 41.4),
    "Coda Motovore": ("Toxin", 35.4),
    "Coda Bassocyst": ("Impact", 46.7),
    "Coda Bubonico": ("Heat", 25.1),
    "Coda Synapse": ("Radiation", 30.8),
    "Coda Tysis": ("Heat", 25.9),
    "Coda Caustacyst": ("Impact", 25.8),
    "Coda Hirudo": ("Toxin", 31.3),
    "Coda Pathocyst": ("Magnetic", 39.5),
}
for _key in ("tenet", "coda"):
    ROT[_key].update(_FIXTURE_META[_key])
    for _group in [ROT[_key]["items"]] if _key == "tenet" else ROT[_key]["batches"]:
        for _row in _group:
            _row["element"], _row["bonus"] = _FIXTURE_VALUES[_row["en"]]


T_EPOCH = datetime.fromisoformat(ROT["tenet"]["epoch"])
C_EPOCH = datetime.fromisoformat(ROT["coda"]["epoch"])
PERIOD = int(ROT["tenet"]["period_hours"])

# 日期夹具与业务入口共用固定 UTC 时钟，避免宿主换轮/跨周改变断言含义。
FIXED_NOW = datetime(2026, 10, 28, 12, tzinfo=timezone.utc)


class FixtureClock(datetime):
    @classmethod
    def now(cls, tz=None):
        return FIXED_NOW.astimezone(tz) if tz else FIXED_NOW.replace(tzinfo=None)


# 发布种子只提供静态商品骨架；缓存采用与失败回落走真实读路径。
def _empty_seed_checks():
    import asyncio
    import tempfile
    from unittest.mock import AsyncMock
    from core import paths
    from core.commands.progress import ProgressCommands
    from core.commands.rotations import _elem_txt

    seed = json.loads((ROOT / "core/data/rotations.json").read_text("utf-8"))
    absent = {"valence_snapshot", "valence_source", "valence_values_since", "batch_values_since"}
    for key in ("tenet", "coda"):
        groups = [seed[key]["items"]] if key == "tenet" else seed[key]["batches"]
        check(f"{key}种子无动态元数据", not (absent & seed[key].keys()))
        check(
            f"{key}种子无元素百分比",
            all(not ({"element", "bonus"} & row.keys()) for g in groups for row in g),
        )
        check(f"{key}种子缺快照持续过期", WC.valence_is_stale(seed[key], FIXED_NOW))
    message = "元素与加成尚未取得，后台自动获取，以游戏内为准"
    check(
        "无快照说明明确等待后台获取",
        message in ProgressCommands._valence_note(seed["tenet"], FIXED_NOW),
    )
    check("无值条目简短标记待获取", _elem_txt(seed["tenet"]["items"][0]) == "待获取")
    complete_coda = json.loads(json.dumps(ROT["coda"]))
    elapsed = int(
        (FIXED_NOW - datetime.fromisoformat(complete_coda["epoch"])) // timedelta(hours=96)
    )
    current = (complete_coda["anchor_idx"] + elapsed) % len(complete_coda["batches"])
    for row in complete_coda["batches"][1 - current]:
        row.pop("element")
        row.pop("bonus")
    check("完整性只要求当前终幕批", WC.valence_data_complete(complete_coda, FIXED_NOW))
    for invalid in (None, True, float("nan"), 24, 61):
        candidate = json.loads(json.dumps(ROT["tenet"]))
        candidate["items"][0]["bonus"] = invalid
        check(
            "完整性沿用解析合法数值规则/" + str(invalid),
            not WC.valence_data_complete(candidate, FIXED_NOW),
        )
    for key in ("tenet", "coda"):
        candidate = json.loads(json.dumps(ROT[key]))
        rows = candidate["items"] if key == "tenet" else candidate["batches"][current]
        rows.pop()
        check("缺行不能判完整/" + key, not WC.valence_data_complete(candidate, FIXED_NOW))
    for invalid_row in (None, "bad", {"en": ""}, {"en": None}):
        candidate = json.loads(json.dumps(ROT["tenet"]))
        candidate["items"][0] = invalid_row
        try:
            valid = WC.valence_data_complete(candidate, FIXED_NOW)
        except Exception:
            valid = True
        check("坏行返回False不抛异常/" + repr(invalid_row), not valid)
    for period in (-1, 0, None, True):
        candidate = json.loads(json.dumps(ROT["coda"]))
        candidate["period_hours"] = period
        check("坏周期返回False/" + repr(period), not WC.valence_data_complete(candidate, FIXED_NOW))
    broken = json.loads(json.dumps(ROT))
    broken["tenet"]["items"].pop()
    repaired = WC._restore_valence_skeleton(broken)
    check(
        "补缺行恢复5件且旧值与合法元数据保持",
        len(repaired["tenet"]["items"]) == 5
        and repaired["tenet"]["items"][0] == ROT["tenet"]["items"][0]
        and all(
            repaired["tenet"][name] == ROT["tenet"][name]
            for name in ("epoch", "period_hours", "valence_snapshot", "valence_values_since")
        ),
    )
    check(
        "补缺行不伪造缺失商品效值",
        "bonus" not in repaired["tenet"]["items"][-1]
        and "element" not in repaired["tenet"]["items"][-1],
    )
    broken = json.loads(json.dumps(ROT))
    broken["coda"] = complete_coda
    repaired = WC._restore_valence_skeleton(broken)
    check(
        "骨架修复不伪造非当前批效值",
        all(
            "element" not in row and "bonus" not in row
            for row in repaired["coda"]["batches"][1 - current]
        ),
    )
    check(
        "骨架修复保持合法相位元数据",
        all(
            repaired["coda"][name] == ROT["coda"][name]
            for name in ("epoch", "anchor_idx", "period_hours", "batch_label")
        ),
    )
    previous = paths._RUN
    seed_bytes = (ROOT / "core/data/rotations.json").read_bytes()
    with tempfile.TemporaryDirectory() as folder:
        paths.set_run_dir(folder)
        check(
            "新安装从静态骨架读取",
            paths.read_path("rotations.json") == paths.PKG_DATA / "rotations.json",
        )
        runtime = Path(folder) / "rotations.json"
        runtime.write_text("{bad", "utf-8")
        check(
            "损坏JSON回落静态骨架",
            paths.read_path("rotations.json") == paths.PKG_DATA / "rotations.json",
        )
        runtime.write_text(json.dumps(ROT), "utf-8")
        check(
            "既有缓存优先且未清理",
            paths.read_path("rotations.json") == runtime and json.loads(runtime.read_text()) == ROT,
        )
        check(
            "旧缓存显示过期警告",
            "可能已变"
            in ProgressCommands._valence_note(
                ROT["tenet"], datetime(2026, 12, 1, tzinfo=timezone.utc)
            ),
        )

    async def push_values(folder):
        from types import SimpleNamespace
        from core.push import PushDaemon
        from core.store import Subscription

        paths.set_run_dir(folder)
        runtime = Path(folder) / "rotations.json"
        old_cache = json.loads(json.dumps(ROT))
        for key in ("tenet", "coda"):
            old_cache[key]["valence_snapshot"] = "2020-01-01T00:00:00+00:00"
        for value, expected in ((seed, message), (old_cache, "可能已变")):
            runtime.write_text(json.dumps(value), "utf-8")
            host = SimpleNamespace(
                _last={"pc": {"tenet_window": "previous", "coda_window": "previous"}},
                _tick_ok=0,
                _tick_fail=0,
                log=mod.logger,
            )
            subs = [Subscription("test", "pc", event) for event in ("信条", "终幕")]
            events = await PushDaemon._snapshot_diff(host, "pc", subs)
            check(
                "订阅缺值与旧值如实说明/" + expected,
                len(events) == 2 and all(expected in event[2] for event in events),
                str(events),
            )

    with tempfile.TemporaryDirectory() as folder:
        asyncio.run(push_values(folder))

    async def no_runtime():
        paths.set_run_dir(None)
        c = WC()
        c.fetch_wiki_source = AsyncMock(side_effect=AssertionError("未注入运行目录不得抓取写种子"))
        try:
            check("未注入运行目录拒绝效价刷新", await c.refresh_valence(force=True) == "failed")
            check("拒绝刷新未发网络请求", c.fetch_wiki_source.await_count == 0)
        finally:
            await c.close()

    try:
        asyncio.run(no_runtime())
    finally:
        paths.set_run_dir(previous)
    check(
        "离线刷新不改包内种子字节", (ROOT / "core/data/rotations.json").read_bytes() == seed_bytes
    )


_empty_seed_checks()

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
# 精确覆盖窗口起点与未来容忍阈值，固定时钟不掩盖边界回归。
for label, snapshot, stale in (
    ("本轮起点前一微秒", _tenet_win - timedelta(microseconds=1), True),
    ("恰在本轮起点", _tenet_win, False),
    ("恰在未来一天阈值", NOW + timedelta(days=1), False),
    ("超过未来一天阈值一微秒", NOW + timedelta(days=1, microseconds=1), True),
):
    check(
        f"快照边界：{label}",
        WC.valence_is_stale(dict(ROT["tenet"], valence_snapshot=snapshot.isoformat()), NOW)
        is stale,
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

# 源码相位：refresh_valence 的持锁实现里不许再出现「遍历到第一个新鲜就 return」
_src = inspect.getsource(WC._refresh_valence_locked)
check("refresh_valence 用 any(...) 汇总各段是否过期", "any(" in _src)
_loops = [i for i, ln in enumerate(_src.splitlines()) if 'return "fresh"' in ln]
check(
    "refresh_valence 不在逐段循环里 return fresh",
    # 2026-10-17：any(...) 与 return 之间多了一行 _note_comm（社区快照采用记录）；
    # 完整性、时间过期、数值过期三个判据都位于 any 内；只检查首个短路区块。
    len(_loops) == 1 and "any(" in _src.split('return "fresh"', 1)[0],
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


def _aligned_tick_checks():
    import asyncio
    from datetime import timezone
    from unittest.mock import patch

    cases = (
        ("2026-10-28T00:01:00+00:00", 240, 1140),
        ("2026-10-28T06:01:00+00:00", 240, 1140),
        ("2026-10-28T00:05:00+00:00", 21600, 22500),
        ("2026-10-28T00:06:00+00:00", 21540, 22440),
        ("2026-10-28T23:59:00+00:00", 360, 1260),
        ("2026-10-28T00:04:59+00:00", 60, 901),
    )
    for stamp, low, high in cases:
        now = datetime.fromisoformat(stamp)

        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return now.astimezone(tz) if tz else now.replace(tzinfo=None)

        for jitter, expected in ((0, low), (15, high)):
            with (
                patch("datetime.datetime", Clock),
                patch.object(P.random, "randint", return_value=jitter),
            ):
                delay = P.WarframeSDJK._secs_to_aligned_tick()
            check(f"对齐刷新 {stamp} 抖动{jitter}分", delay == expected, str(delay))

    # 真实自动刷新入口最终采用同一检查点；取消可正常退出。
    fixed_now = datetime(2026, 10, 28, 0, 1, tzinfo=timezone.utc)

    class LoopClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now.astimezone(tz) if tz else fixed_now.replace(tzinfo=None)

    sleeps = []
    first_sleep_calls = []
    clients = []

    async def capture_sleep(seconds):
        first_sleep_calls.append(list(clients[-1].calls))
        sleeps.append(seconds)
        raise asyncio.CancelledError

    async def run_loop(enabled, reachable):
        sleeps.clear()
        obj = object.__new__(P.WarframeSDJK)
        obj.client = _StubFlareClient(enabled, reachable)
        clients.append(obj.client)
        try:
            await obj._valence_autoloop()
        except asyncio.CancelledError:
            pass
        check("自动刷新真实入口等待当前00:05检查点", sleeps == [240], str(sleeps))
        check(
            "真实main首轮先refresh_valence后sleep",
            first_sleep_calls[-1] == ["valence", "acrichis"],
            str(first_sleep_calls[-1]),
        )
        check(
            f"自动刷新FS={enabled}/{reachable}两项均执行，且不先探测/回收FS",
            obj.client.calls == ["valence", "acrichis"],
            str(obj.client.calls),
        )

    with (
        patch("datetime.datetime", LoopClock),
        patch.object(P.random, "randint", return_value=0),
        patch.object(P.asyncio, "sleep", capture_sleep),
    ):
        for enabled, reachable in ((False, False), (True, False), (True, True)):
            asyncio.run(run_loop(enabled, reachable))


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


_aligned_tick_checks()


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

    # 刷新是否受FS阻断由上面的真实循环入口检查；保留退役定时任务接线守卫。
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    i = src.index("async def _valence_autoloop")
    body = src[i : i + 5200]
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
            return [{"name": "fixture"}]

        async def wm_riven_weapons(self):
            self.calls.append("riven")
            return [{"name": "fixture"}]

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

    from core import paths
    from core.commands.progress import ProgressCommands

    now = FIXED_NOW

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

                        async def _fetch(url, ttl=0, _p=page, **kwargs):
                            return json.dumps(_p)

                        c.fetch_wiki_source = _fetch
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

                async def _fetch(url, ttl=0, **kwargs):
                    fetched.append(ttl)
                    return json.dumps(page)

                async def _revs(url, what):
                    return dict(revs_in)

                c.fetch_wiki_source = _fetch
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
    acr["items"] = list(acr["_en_catalog"].values())[:5]
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
        "与上周货单完全相同" in inspect.getsource(WC._adopt_acrichis_source),
    )


with patch("datetime.datetime", FixtureClock):
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


def _background_result_checks():
    import asyncio
    import tempfile
    from types import SimpleNamespace
    from unittest.mock import patch
    from core import wiki_intro as wi

    async def healthy():
        return {"effect": "fresh"}

    async def unavailable():
        return {"effect": "no-community"}

    async def items():
        return [{"name": "fixture"}]

    async def empty_items():
        return []

    async def cases():
        obj = object.__new__(P.WarframeSDJK)
        obj.client = SimpleNamespace(wm_items=items, wm_riven_weapons=items)
        obj._refresh_from_community = unavailable
        obj._refresh_static_community = healthy
        await obj._boot_warm()
        row = obj._background_status["boot"]
        check(
            "正常返回但社区不可用：预热降级，不刷新正常检查时间",
            row["result"] == "degraded"
            and "last_success" not in row
            and row["checks"]["社区快照（元素加成 / 言录使）"]["result"] == "degraded",
        )
        obj._refresh_from_community = healthy
        await obj._boot_warm()
        check(
            "源恢复后预热正常，成功时间才推进",
            obj._background_status["boot"]["result"] == "ok"
            and obj._background_status["boot"]["last_success"] > 0,
        )
        success = obj._background_status["boot"]["last_success"]
        obj.client.wm_items = empty_items
        await obj._boot_warm()
        check(
            "WM空表不冒充已就绪且保留上次成功时间",
            obj._background_status["boot"]["result"] == "degraded"
            and obj._background_status["boot"]["last_success"] == success,
        )
        entered = asyncio.Event()

        async def blocked():
            entered.set()
            await asyncio.Event().wait()

        obj._refresh_from_community = blocked
        task = asyncio.create_task(obj._boot_warm())
        await entered.wait()
        task.cancel()
        try:
            await task
            check("预热取消传播", False)
        except asyncio.CancelledError:
            check(
                "预热取消传播且记录取消状态", obj._background_status["boot"]["state"] == "cancelled"
            )
        # 用真实轮换循环确认失败退避仍为一小时；仅捕获sleep，不实际等待。
        obj = object.__new__(P.WarframeSDJK)

        async def failed_refresh():
            raise RuntimeError("fixture refresh")

        obj.client = type("FailingRefresh", (), {})()
        obj.client.refresh_valence = failed_refresh
        waits = []

        async def capture(seconds):
            waits.append((seconds, dict(obj._background_status["valence"])))
            raise asyncio.CancelledError

        with patch.object(P.asyncio, "sleep", capture):
            try:
                await obj._valence_autoloop()
            except asyncio.CancelledError:
                pass
        check(
            "轮换失败保留一小时退避与可观测失败状态",
            waits[0][0] == 3600
            and waits[0][1]["result"] == "error"
            and waits[0][1]["next_run"] > waits[0][1]["last_attempt"]
            and "last_success" not in waits[0][1],
        )

    asyncio.run(cases())
    with tempfile.TemporaryDirectory(prefix="wf-warm-status-") as folder:
        with patch.object(wi, "_FILE", Path(folder) / "missing.json"):
            wi._table.cache_clear()
            obj = object.__new__(P.WarframeSDJK)
            obj._warmup()
            row = obj._background_status["warmup"]
            check(
                "真实wiki空表预热判降级，分段计时仍可用",
                row["result"] == "degraded"
                and "last_success" not in row
                and next(s for s in row["segments"] if s["name"] == "wiki简介")["result"]
                == "ValueError"
                and all(s["wall_ms"] >= 0 and s["cpu_ms"] >= 0 for s in row["segments"]),
            )
        wi._table.cache_clear()
        obj._warmup()
        check("真实数据恢复后本地预热回到正常", obj._background_status["warmup"]["result"] == "ok")


_background_result_checks()


def _revision_client_checks():
    """真实客户端→HTTP MockTransport→直连/FS 协议→wiki JSON/正文→临时 plugin_data。"""
    import time
    import asyncio
    import copy
    import tempfile
    from urllib.parse import urlsplit, parse_qs
    from unittest.mock import patch, AsyncMock
    import httpx
    from core import paths

    now = FIXED_NOW.replace(microsecond=0)
    stamp = now - timedelta(minutes=1)
    seed_shop = json.loads((ROOT / "core/data/de/acrichis_week.json").read_text("utf-8"))

    def setup_rot(stale=False):
        rot = copy.deepcopy(ROT)
        for key in ("tenet", "coda"):
            ep = datetime.fromisoformat(rot[key]["epoch"])
            period = timedelta(hours=96)
            win = ep + (now - ep) // period * period
            since = (
                win - timedelta(hours=1) if stale else stamp - timedelta(minutes=5)
            ).isoformat()
            rot[key]["valence_snapshot"] = since
            if key == "tenet":
                rot[key]["valence_values_since"] = since
            else:
                rot[key]["batch_values_since"] = [since, since]
        return rot

    def table(items, header="Weapon"):
        rows = "".join(
            "<tr><td>{{Weapon|"
            + it["en"]
            + "}}</td><td>{{D|"
            + it["element"]
            + "}}</td><td>{{ValenceBonusPercentageColor|"
            + str(it["bonus"])
            + "}}</td></tr>"
            for it in items
        )
        return "<table><tr><th>" + header + "</th></tr>" + rows + "</table>"

    def sources(rot):
        names = list(seed_shop["_en_catalog"])[:5]
        shop = (
            "{{#vardefine:AcrithisObserved|{{CURRENTMONTHNAME}} {{CURRENTDAY}}, {{CURRENTYEAR}}}}\n"
        )
        shop += "\n".join(
            "{{#vardefine:AcrithisItem" + str(i) + "|" + name + "}}"
            for i, name in enumerate(names, 1)
        )
        return {
            "tenet": table(rot["tenet"]["items"]),
            "coda": "".join(
                table(items, f"Weapon (Batch {label})")
                for label, items in zip(("A", "B"), rot["coda"]["batches"])
            ),
            "acrithis": shop,
        }

    class Harness:
        def __init__(self, directory, stale=False):
            self.root = Path(directory)
            self.rot = setup_rot(stale)
            self.shop = copy.deepcopy(seed_shop)
            self.shop["expiry"] = (now - timedelta(days=1)).isoformat()
            self.shop["items"] = []
            self.text = sources(self.rot)
            self.ids = {"tenet": 101, "coda": 102, "acrithis": 103}
            self.stamps = dict.fromkeys(self.ids, stamp)
            self.bad = {}
            self.metadata_response = None
            self.direct = False
            self.http_override = None
            self.fs_invalid = False
            self.http_bad = {}
            self.http_calls = []
            self.calls = []
            self.gate = None
            self.enter = None
            self.put("rotations.json", self.rot)
            self.put(mod.ACRITHIS_WEEK_NAME, self.shop)

        def put(self, name, value):
            p = self.root / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(value, ensure_ascii=False), "utf-8")

        def get(self, name):
            return json.loads((self.root / name).read_text("utf-8"))

        async def handler(self, req):
            self.http_calls.append((req.method, str(req.url)))
            if req.method == "GET":
                if self.http_override:
                    response = self.http_override(req)
                    if response is not None:
                        return response
                if not self.direct:
                    return httpx.Response(
                        403, headers={"cf-mitigated": "challenge"}, text="challenge"
                    )
                url = str(req.url)
            else:
                payload = json.loads(req.content)
                assert payload["cmd"] == "request.get", payload
                url = payload["url"]
            self.calls.append(url)
            if "action=raw" in url:
                key = "acrithis" if "Acrithis" in url else "coda" if "Coda" in url else "tenet"
                raw = self.text[key]
                if req.method == "GET":
                    return httpx.Response(200, text=raw)
                if self.fs_invalid:
                    raw = "invalid fixture source"
                return httpx.Response(
                    200, json={"status": "ok", "solution": {"status": 200, "response": raw}}
                )
            query = parse_qs(urlsplit(url).query)
            selected = None
            if "revids" in query:
                selected = next(k for k, n in self.ids.items() if str(n) == query["revids"][0])
                if self.enter:
                    self.enter.set()
                    await self.gate.wait()
            page_rows = {}
            for key in self.ids:
                if selected is not None and selected != key:
                    continue
                revision = {"revid": self.ids[key], "timestamp": self.stamps[key].isoformat()}
                if selected:
                    revision["slots"] = {"main": {"*": self.text[key]}}
                    revision.update(self.bad.get(key, {}))
                    if req.method == "GET":
                        revision.update(self.http_bad.get(key, {}))
                page_rows[str(self.ids[key])] = {
                    "pageid": self.ids[key],
                    "title": mod.ROTATION_PAGES[key],
                    "revisions": [revision],
                }
            wiki = {"query": {"pages": page_rows}}
            # 按 MediaWiki revisions 真实约束，多页查询不得携带 rvlimit。
            if "rvlimit" in query and len(query.get("titles", [""])[0].split("|")) > 1:
                wiki = {
                    "error": {
                        "code": "rvmultpages",
                        "info": "rvlimit may only be used on a single page",
                    }
                }
            elif selected is None and self.metadata_response is not None:
                wiki = self.metadata_response
            if req.method == "GET":
                return httpx.Response(200, json=wiki)
            raw = "invalid fixture source" if self.fs_invalid else json.dumps(wiki)
            return httpx.Response(
                200, json={"status": "ok", "solution": {"status": 200, "response": raw}}
            )

        async def client(self):
            c = WC(flare_urls=["http://fixture.invalid/v1"])
            # 大型业务夹具不重复真实等待；请求礼仪另有专门的一秒间隔回归。
            c.WIKI_MIN_INTERVAL_S = 0
            await c._http.aclose()
            c._http = httpx.AsyncClient(transport=httpx.MockTransport(self.handler))
            return c

    async def in_case(case, stale=False):
        with tempfile.TemporaryDirectory(prefix="wf-revision-") as folder:
            paths.set_run_dir(folder)
            h = Harness(folder, stale)
            c = await h.client()
            try:
                await case(h, c)
            finally:
                await c.close()
                paths.set_run_dir(None)

    async def protocol_errors(h, c):
        await c.check_rotation_revisions()
        state_before = (h.root / mod.ROTATION_REVISIONS_NAME).read_bytes()
        data_before = (h.root / "rotations.json").read_bytes()
        logs = []

        def warn(message, *args):
            logs.append(message % args if args else message)

        with patch.object(mod.logger, "warning", side_effect=warn):
            with patch.object(
                mod, "ROTATION_REVISION_API", mod.ROTATION_REVISION_API + "&rvlimit=1"
            ):
                result = await c.check_rotation_revisions()
            check(
                "真实协议拒绝多titles加rvlimit并记录rvmultpages",
                set(result.values()) == {"failed"} and any("rvmultpages" in line for line in logs),
                str((result, logs)),
            )
            for payload, code in (
                ({"error": {"code": "ratelimited", "info": "fixture limit"}}, "ratelimited"),
                ({}, "invalid-response"),
                ({"query": {}}, "invalid-response"),
                ({"query": {"pages": []}}, "invalid-response"),
                ({"query": {"pages": {}}}, "invalid-response"),
            ):
                h.metadata_response = payload
                c.__dict__.get("_wiki_retry_until", {}).clear()
                logs.clear()
                h.calls.clear()
                result = await c.check_rotation_revisions()
                check(
                    "API错误或坏结构明确记录" + code,
                    set(result.values()) == {"failed"}
                    and len(h.calls) == 1
                    and any(code in line for line in logs),
                    str((payload, result, logs)),
                )
                check(
                    "API错误不推进已处理游标或业务文件",
                    (h.root / mod.ROTATION_REVISIONS_NAME).read_bytes() == state_before
                    and (h.root / "rotations.json").read_bytes() == data_before,
                )
            h.metadata_response = None
            c.__dict__.get("_wiki_retry_until", {}).clear()
            h.calls.clear()
            result = await c.check_rotation_revisions()
            check(
                "API恢复后无需重置游标即可继续正常检查",
                set(result.values()) == {"unchanged"} and len(h.calls) == 1,
                str(result),
            )

    async def initial(h, c):
        before = (h.root / "rotations.json").read_bytes()
        result = await c.check_rotation_revisions()
        check(
            "修订首见：同业务值不改rotations，新货单正常采用",
            result == {"tenet": "fresh", "coda": "fresh", "acrithis": "refreshed"}
            and (h.root / "rotations.json").read_bytes() == before,
            str(result),
        )
        check(
            "修订一次metadata+三个固定正文共4次请求",
            len(h.calls) == 4
            and "titles=" in h.calls[0]
            and all("revids=" in u for u in h.calls[1:]),
            str(h.calls),
        )
        h.calls.clear()
        result = await c.check_rotation_revisions()
        check(
            "同修订且本地有效只查metadata不抓正文",
            set(result.values()) == {"unchanged"} and len(h.calls) == 1,
            str(result),
        )
        h.ids["tenet"] += 100
        updated = copy.deepcopy(h.rot)
        updated["tenet"]["items"][0].update(element="Heat", bonus=55.5)
        h.text["tenet"] = sources(updated)["tenet"]
        h.bad["coda"] = {"revid": -1}
        h.ids["coda"] += 100
        # 仅信条变化，已处理终幕不发正文；终幕仍逐字保留。
        coda_before = h.get("rotations.json")["coda"]
        result = await c.check_rotation_revisions()
        out = h.get("rotations.json")
        check(
            "仅信条更新不依赖另一页且终幕逐字段保留",
            result["tenet"] == "refreshed"
            and result["coda"] == "failed"
            and out["tenet"]["items"][0]["bonus"] == 55.5
            and out["coda"] == coda_before,
            str(result),
        )
        h.bad.clear()
        old_stamp = out["tenet"]["valence_values_since"]
        h.ids["tenet"] += 100
        updated["tenet"]["items"][0]["bonus"] = 54.5
        h.text["tenet"] = sources(updated)["tenet"]
        result = await c.check_rotation_revisions()
        check(
            "同轮只改百分比更正可采用",
            result["tenet"] == "refreshed"
            and h.get("rotations.json")["tenet"]["items"][0]["bonus"] == 54.5,
            str(result),
        )
        current = h.get("rotations.json")
        h.ids["tenet"] -= 1
        h.calls.clear()
        result = await c.check_rotation_revisions()
        check(
            "较小revid拒收且不取正文",
            result["tenet"] == "not-updated"
            and len(h.calls) == 1
            and h.get("rotations.json") == current,
            str(result),
        )
        check("采用使用源修订时间而非抓取now", old_stamp == stamp.isoformat(), old_stamp)

    async def stale_same(h, c):
        before = (h.root / "rotations.json").read_bytes()
        a = await c.check_rotation_revisions()
        b = await c.check_rotation_revisions()
        records = h.get(mod.ROTATION_REVISIONS_NAME)["pages"]
        check(
            "首次无记录+无关编辑不让旧值续鲜，候选可重试",
            a["tenet"] == b["tenet"] == "not-updated"
            and "tenet" not in records
            and (h.root / "rotations.json").read_bytes() == before,
            str((a, b)),
        )
        h.ids["coda"] += 100
        newer = copy.deepcopy(h.rot)
        ep = datetime.fromisoformat(newer["coda"]["epoch"])
        passed = int((now - ep) // timedelta(hours=96))
        idx = (newer["coda"]["anchor_idx"] + passed) % 2
        other = 1 - idx
        for row in newer["coda"]["batches"][idx]:
            row.update(element="Toxin" if row["element"] != "Toxin" else "Heat", bonus=50)
        h.text["coda"] = sources(newer)["coda"]
        result = await c.check_rotation_revisions()
        out = h.get("rotations.json")
        check(
            "终幕只更新在售批：另一批since和业务值保留",
            result["coda"] == "refreshed"
            and out["coda"]["batch_values_since"][other]
            == h.rot["coda"]["batch_values_since"][other]
            and out["coda"]["batches"][other] == h.rot["coda"]["batches"][other],
            str(result),
        )
        check(
            "终幕单页可更新，信条旧值不拖累",
            result["tenet"] == "not-updated" and out["tenet"] == h.rot["tenet"],
            str(result),
        )

    async def invalid(h, c):
        original = (h.root / "rotations.json").read_bytes()
        for label, mutate in (
            ("缺行", lambda rows: rows.pop()),
            ("重复", lambda rows: rows.append(copy.deepcopy(rows[0]))),
            ("非法元素", lambda rows: rows[0].update(element="BAD")),
            ("超范围", lambda rows: rows[0].update(bonus=99)),
            ("低范围", lambda rows: rows[0].update(bonus=24)),
        ):
            changed = copy.deepcopy(h.rot)
            mutate(changed["tenet"]["items"])
            h.text["tenet"] = sources(changed)["tenet"]
            result = await c.check_rotation_revisions()
            records = h.get(mod.ROTATION_REVISIONS_NAME)["pages"]
            check(
                "固定正文" + label + "拒绝且不推进游标",
                result["tenet"] == "failed"
                and "tenet" not in records
                and (h.root / "rotations.json").read_bytes() == original,
                str(result),
            )
        for bad_number in ("-25", "30oops", "30e999"):
            source = sources(h.rot)["tenet"]
            first = str(h.rot["tenet"]["items"][0]["bonus"])
            h.text["tenet"] = source.replace(
                "ValenceBonusPercentageColor|" + first,
                "ValenceBonusPercentageColor|" + bad_number,
                1,
            )
            result = await c.check_rotation_revisions()
            check(
                "原文非法数字" + bad_number + "不得截取合法前缀",
                result["tenet"] == "failed"
                and (h.root / "rotations.json").read_bytes() == original,
                str(result),
            )
        h.text["tenet"] = sources(h.rot)["tenet"]
        h.bad["tenet"] = {"revid": 999}
        result = await c.check_rotation_revisions()
        check(
            "固定正文版本不匹配拒绝，其他页继续",
            result["tenet"] == "failed"
            and result["acrithis"] == "unchanged"
            and "tenet" not in h.get(mod.ROTATION_REVISIONS_NAME)["pages"]
            and (h.root / "rotations.json").read_bytes() == original,
            str(result),
        )
        h.bad.clear()
        result = await c.check_rotation_revisions()
        check("失败同版重试成功", result["tenet"] == "fresh", str(result))
        for value in (True, float("nan"), float("inf"), 61, 24):
            row = {"en": "Tenet X", "element": "Heat", "bonus": value}
            try:
                c._valence_table([row], ["Tenet X"])
                check(f"业务校验拒绝{value!r}", False)
            except mod.WarframeAPIError:
                check(f"业务校验拒绝{value!r}", True)

    async def offerings(h, c):
        await c.check_rotation_revisions()
        for field in ("qty", "price"):
            current = h.get(mod.ACRITHIS_WEEK_NAME)
            expected = 999
            name = list(seed_shop["_en_catalog"])[0]
            current["_en_catalog"][name][field] = expected
            h.put(mod.ACRITHIS_WEEK_NAME, current)
            h.ids["acrithis"] += 100
            result = await c.check_rotation_revisions()
            check(
                "言录使同周同名" + field + "更正可采用",
                result["acrithis"] == "refreshed"
                and h.get(mod.ACRITHIS_WEEK_NAME)["items"][0].get(field) == expected,
                str(result),
            )
        current = h.get(mod.ACRITHIS_WEEK_NAME)
        current["expiry"] = (now - timedelta(days=1)).isoformat()
        h.put(mod.ACRITHIS_WEEK_NAME, current)
        h.ids["acrithis"] += 100
        previous_id = h.get(mod.ROTATION_REVISIONS_NAME)["pages"]["acrithis"]["revid"]
        result = await c.check_rotation_revisions()
        check(
            "言录使魔法日期无关编辑不能为相同旧货单续周",
            result["acrithis"] == "not-updated"
            and h.get(mod.ACRITHIS_WEEK_NAME) == current
            and h.get(mod.ROTATION_REVISIONS_NAME)["pages"]["acrithis"]["revid"] == previous_id,
            str(result),
        )
        names = list(seed_shop["_en_catalog"])[:5]
        h.text["acrithis"] = h.text["acrithis"].replace(
            "AcrithisItem5|" + names[4], "AcrithisItem5|" + names[0]
        )
        result = await c.check_rotation_revisions()
        check(
            "言录使重复名单拒收且不写半成品",
            result["acrithis"] == "failed" and h.get(mod.ACRITHIS_WEEK_NAME) == current,
            str(result),
        )

    async def no_replay(h, c):
        await c.check_rotation_revisions()
        current = h.get(mod.ACRITHIS_WEEK_NAME)
        current["items"][0]["price"] = 999
        h.put(mod.ACRITHIS_WEEK_NAME, current)
        h.calls.clear()
        result = await c.check_rotation_revisions()
        check(
            "外部更正后同已处理revid不回取旧正文覆盖",
            result["acrithis"] == "unchanged"
            and len(h.calls) == 1
            and h.get(mod.ACRITHIS_WEEK_NAME) == current,
            str(result),
        )
        h.ids["acrithis"] += 100
        h.calls.clear()
        result = await c.check_rotation_revisions()
        check(
            "新revid无关编辑也不能覆外部更正",
            result["acrithis"] == "unchanged"
            and len(h.calls) == 2
            and h.get(mod.ACRITHIS_WEEK_NAME) == current,
            str(result),
        )
        current["expiry"] = (now - timedelta(days=1)).isoformat()
        h.put(mod.ACRITHIS_WEEK_NAME, current)
        h.calls.clear()
        result = await c.check_rotation_revisions()
        check(
            "已处理同revid本地到期只报未更新不重取",
            result["acrithis"] == "not-updated"
            and len(h.calls) == 1
            and h.get(mod.ACRITHIS_WEEK_NAME) == current,
            str(result),
        )

    async def monotonic(h, c):
        await c.check_rotation_revisions()
        current = h.get("rotations.json")
        data = current["coda"]
        ep = datetime.fromisoformat(data["epoch"])
        idx = (data["anchor_idx"] + int((now - ep) // timedelta(hours=96))) % 2
        other = 1 - idx
        newer_snapshot = (now - timedelta(seconds=10)).isoformat()
        data["valence_snapshot"] = newer_snapshot
        data["batch_values_since"][idx] = newer_snapshot
        h.put("rotations.json", current)
        changed = copy.deepcopy(current)
        for row in changed["coda"]["batches"][other]:
            row.update(element="Heat" if row["element"] != "Heat" else "Cold", bonus=50)
        h.text["coda"] = sources(changed)["coda"]
        h.ids["coda"] += 100
        result = await c.check_rotation_revisions()
        out = h.get("rotations.json")["coda"]
        check(
            "仅非在售批更新不能倒退整体快照或推进在售since",
            result["coda"] == "refreshed"
            and out["valence_snapshot"] == newer_snapshot
            and out["batch_values_since"][idx] == newer_snapshot
            and out["batches"][idx] == data["batches"][idx],
            str(result),
        )

    async def group_no_replay(h, c):
        await c.check_rotation_revisions()
        current = h.get("rotations.json")
        # 外部仅更正A；新wiki修订仅更正B，A源正文保持上次已处理值。
        current["coda"]["batches"][0][0]["bonus"] = 49.123
        corrected_since = (now - timedelta(seconds=20)).isoformat()
        current["coda"]["batch_values_since"][0] = corrected_since
        h.put("rotations.json", current)
        candidate = copy.deepcopy(h.rot)
        candidate["coda"]["batches"][1][0]["bonus"] = 48.321
        h.text["coda"] = sources(candidate)["coda"]
        h.ids["coda"] += 100
        result = await c.check_rotation_revisions()
        out = h.get("rotations.json")["coda"]
        check(
            "终幕仅B源改变：保留A的外部更正及since",
            result["coda"] == "refreshed"
            and out["batches"][0] == current["coda"]["batches"][0]
            and out["batch_values_since"][0] == corrected_since
            and out["batches"][1][0]["bonus"] == 48.321,
            str(result),
        )

    async def pinned_legacy(h, c):
        await c.check_rotation_revisions()
        before = h.get("rotations.json")
        h.calls.clear()
        c._wiki_revisions = AsyncMock(side_effect=AssertionError("不得走浮动metadata"))
        result = await c.refresh_valence(force=True)
        check(
            "已建观测的手动/定时刷新复用固定修订链，不再浮动raw配时间",
            result == "fresh"
            and len(h.calls) == 1
            and "titles=" in h.calls[0]
            and c._wiki_revisions.await_count == 0
            and h.get("rotations.json") == before,
            result,
        )

    async def partial_unprocessed(h, c):
        before = (h.root / "rotations.json").read_bytes()
        d = h.rot["coda"]
        idx = (
            d["anchor_idx"] + int((now - datetime.fromisoformat(d["epoch"])) // timedelta(hours=96))
        ) % 2
        candidate = copy.deepcopy(h.rot)
        for row in candidate["coda"]["batches"][1 - idx]:
            row.update(element="Heat" if row["element"] != "Heat" else "Cold", bonus=50)
        h.text["coda"] = sources(candidate)["coda"]
        h.text["acrithis"] = "invalid offerings"
        result = await c.check_rotation_revisions()
        check(
            "当前批仍旧且全页未处理时不写另一批部分值，阻止无游标旧路径回滚",
            result["coda"] == "not-updated"
            and (h.root / "rotations.json").read_bytes() == before
            and not (h.root / mod.ROTATION_REVISIONS_NAME).exists(),
            str(result),
        )

    async def timed_fallback(h, c):
        await c.check_rotation_revisions()
        before = h.get("rotations.json")
        fetch_calls = []

        async def raw(url, ttl=0, **kwargs):
            fetch_calls.append((url, ttl))
            return h.text["coda"] if "Coda" in url else h.text["tenet"]

        c.fetch_wiki_source = raw
        c._wiki_revisions = AsyncMock(return_value={})
        c._community_valence_parsed = AsyncMock(return_value=None)
        status = await c.refresh_valence(force=True)
        check(
            "定时原raw成功但metadata未知：尝试社区、无合格源保留旧值",
            status.startswith("not-updated")
            and c._community_valence_parsed.await_count == 1
            and all(ttl == 0 for _, ttl in fetch_calls)
            and h.get("rotations.json") == before,
            status,
        )
        payload = {
            "tenet": {it["en"]: (it["element"], it["bonus"]) for it in before["tenet"]["items"]},
            "coda_batch": "A",
            "coda": {},
            "snapshot": now.isoformat(),
            "snapshots": {"tenet": now.isoformat(), "coda": now.isoformat()},
        }
        d = before["coda"]
        idx = (
            d["anchor_idx"] + int((now - datetime.fromisoformat(d["epoch"])) // timedelta(hours=96))
        ) % 2
        payload["coda_batch"] = d["batch_label"][idx]
        payload["coda"] = {it["en"]: (it["element"], it["bonus"]) for it in d["batches"][idx]}
        c._community_valence_parsed = AsyncMock(return_value=payload)
        status = await c.refresh_valence(force=True)
        check(
            "metadata未知时合格社区兜底仍可采用",
            status.startswith("refreshed") and "community" in status,
            status,
        )
        c._acrichis_page_revision = AsyncMock(return_value=None)
        c._community_acrithis_week = AsyncMock(return_value="refreshed")
        status = await c.refresh_acrichis_week(force=True)
        check(
            "言录使metadata未知仍走社区兜底",
            status == "refreshed" and c._community_acrithis_week.await_count == 1,
            status,
        )

    async def writes(h, c):
        actual = paths.atomic_write_text

        def fail_observation(target, value, *args, **kwargs):
            if Path(target).name == mod.ROTATION_REVISIONS_NAME:
                raise OSError("fixture readonly")
            return actual(target, value, *args, **kwargs)

        with patch.object(paths, "atomic_write_text", fail_observation):
            a = await c.check_rotation_revisions()
        check(
            "业务先写、观测写失败不推进游标",
            set(a.values()) == {"failed"}
            and not (h.root / mod.ROTATION_REVISIONS_NAME).exists()
            and len(h.get(mod.ACRITHIS_WEEK_NAME)["items"]) == 5,
            str(a),
        )
        b = await c.check_rotation_revisions()
        check(
            "观测写失败后同版本幂等重试",
            set(b.values()) == {"fresh"}
            and set(h.get(mod.ROTATION_REVISIONS_NAME)["pages"]) == set(mod.ROTATION_PAGES),
            str(b),
        )

    async def lock_cancel(h, c):
        h.enter, h.gate = asyncio.Event(), asyncio.Event()
        task = asyncio.create_task(c.check_rotation_revisions())
        await h.enter.wait()
        legacy = AsyncMock(return_value="fresh")
        c._refresh_valence_locked = legacy
        waiting = asyncio.create_task(c.refresh_valence(force=True))
        await asyncio.sleep(0)
        check("旧强制刷新等待修订检查同一资源锁", not waiting.done() and legacy.await_count == 0)
        task.cancel()
        try:
            await task
            check("固定正文取消传播", False)
        except asyncio.CancelledError:
            check("固定正文取消传播且不写游标", not (h.root / mod.ROTATION_REVISIONS_NAME).exists())
        await waiting
        check("取消释放资源锁给原强制刷新", legacy.await_count == 1)

    async def closing(h, c):
        h.enter, h.gate = asyncio.Event(), asyncio.Event()
        task = asyncio.create_task(c.check_rotation_revisions())
        await h.enter.wait()
        c.begin_close()
        h.gate.set()
        try:
            await task
            check("关闭后正文返回禁止写入", False)
        except mod.WarframeAPIError:
            check(
                "关闭后正文返回禁止写入和推进游标",
                not (h.root / mod.ROTATION_REVISIONS_NAME).exists(),
            )

    def changed_sources(h):
        candidate = copy.deepcopy(h.rot)
        for rows in [candidate["tenet"]["items"], *candidate["coda"]["batches"]]:
            for row in rows:
                row.update(element="Cold" if row["element"] != "Cold" else "Heat", bonus=50.125)
        h.text = sources(candidate)
        return candidate

    async def direct_without_fs(h, c):
        h.direct = True
        c._flare_enabled = False
        changed_sources(h)
        # 合法wikitext以及JSON内嵌正文含pre时，不能误当FS响应外壳拆掉整份数据。
        h.text["tenet"] += "\n<pre>合法说明里的 <html> 字样</pre>"
        status = await c.refresh_valence(force=True)
        shop = await c.refresh_acrichis_week(force=True)
        revision = await c.check_rotation_revisions()
        check(
            "FS关闭仍从HTTP刷新效价、货单和固定revid，FS零POST",
            status.startswith("refreshed")
            and shop == "refreshed"
            and set(revision.values()) <= {"fresh", "refreshed"}
            and all(method == "GET" for method, _ in h.http_calls)
            and set(h.get(mod.ROTATION_REVISIONS_NAME)["pages"]) == set(mod.ROTATION_PAGES),
            str((status, shop, revision, h.http_calls)),
        )
        check(
            "HTTP来源不误记为FS",
            "FlareSolverr" not in h.get("rotations.json")["tenet"]["valence_source"],
        )

    async def direct_rejects_to_fs(h, c):
        h.direct = True
        changed_sources(h)
        for name, response in (
            (
                "challenge-header",
                httpx.Response(200, headers={"cf-mitigated": "challenge"}, text=h.text["coda"]),
            ),
            ("HTML", httpx.Response(200, text="<!doctype html><html>challenge</html>")),
            ("empty", httpx.Response(200, text="")),
            ("partial", httpx.Response(200, text="<table>unfinished source</table>")),
        ):
            h.http_calls.clear()
            h.http_override = lambda req, response=response: (
                response if str(req.url) == mod.WIKI_RAW_CODA else None
            )
            status = await c.refresh_valence(force=True)
            check(
                "HTTP非法正文经业务校验后FS接力/" + name,
                status.startswith("refreshed")
                and sum(method == "POST" for method, _ in h.http_calls) == 1,
                str((status, h.http_calls)),
            )
        h.http_override = None
        h.http_calls.clear()
        h.http_bad["coda"] = {"revid": -1}
        result = await c.check_rotation_revisions()
        check(
            "HTTP固定修订串版拒绝并取FS正确版，才推进游标",
            set(result.values()) <= {"fresh", "refreshed"}
            and sum(method == "POST" for method, _ in h.http_calls) == 1,
            str((result, h.http_calls)),
        )

    async def direct_failure_community(h, c):
        h.direct = True
        candidate = changed_sources(h)
        h.http_override = lambda req: httpx.Response(200, text="invalid source")
        h.fs_invalid = True
        data = candidate["coda"]
        batch = (
            data["anchor_idx"]
            + int((now - datetime.fromisoformat(data["epoch"])) // timedelta(hours=96))
        ) % 2
        payload = {
            "tenet": {r["en"]: (r["element"], r["bonus"]) for r in candidate["tenet"]["items"]},
            "coda": {r["en"]: (r["element"], r["bonus"]) for r in data["batches"][batch]},
            "coda_batch": data["batch_label"][batch],
            "snapshot": stamp.isoformat(),
            "snapshots": {"tenet": stamp.isoformat(), "coda": stamp.isoformat()},
        }
        c._valence_fallback = AsyncMock(return_value=("community", payload))
        for enabled in (True, False):
            c._flare_enabled = enabled
            h.http_calls.clear()
            h.put("rotations.json", h.rot)
            status = await c.refresh_valence(force=True)
            check(
                "HTTP失败后社区接力且FS遵守开关/" + str(enabled),
                status.startswith("refreshed")
                and c._valence_fallback.await_count >= 1
                and sum(method == "POST" for method, _ in h.http_calls) == int(enabled)
                and mod.WIKI_RAW_CODA not in c.__dict__.get("_wiki_source_cache", {})
                and mod.WIKI_RAW_CODA not in c._flare_cache,
                str((status, h.http_calls)),
            )
        h.http_calls.clear()
        h.put("rotations.json", h.rot)
        status = await c.refresh_valence(community_only=True)
        check("显式community_only跳过HTTP和FS", status.startswith("refreshed") and not h.http_calls)

    async def direct_api_errors_and_retry(h, c):
        h.direct = True
        for payload in ({"error": {"code": "badvalue"}}, {}, {"query": {"pages": []}}):
            h.metadata_response = payload
            h.http_calls.clear()
            result = await c.check_rotation_revisions()
            check(
                "HTTP/API坏结构FS仍校验且不推进游标/" + str(payload),
                set(result.values()) == {"failed"}
                and [method for method, _ in h.http_calls] == ["GET", "POST"]
                and not (h.root / mod.ROTATION_REVISIONS_NAME).exists(),
                str((result, h.http_calls)),
            )
        for name, response in (
            ("429", httpx.Response(429, headers={"Retry-After": "1200"})),
            (
                "maxlag",
                httpx.Response(
                    200, headers={"Retry-After": "1200"}, json={"error": {"code": "maxlag"}}
                ),
            ),
            ("ratelimited", httpx.Response(200, json={"error": {"code": "ratelimited"}})),
        ):
            c.__dict__.get("_wiki_retry_until", {}).clear()
            h.http_calls.clear()
            h.http_override = lambda req, response=response: response
            first = await c.check_rotation_revisions()
            second = await c.check_rotation_revisions()
            check(
                "上游退避期间不换FS、不再次HTTP、不推进游标/" + name,
                set(first.values()) == set(second.values()) == {"failed"}
                and [method for method, _ in h.http_calls] == ["GET"]
                and not (h.root / mod.ROTATION_REVISIONS_NAME).exists(),
                str((first, second, h.http_calls)),
            )

    async def direct_cache(h, c):
        h.direct = True
        c._flare_enabled = False

        def validate(raw):
            return c._validate_rotation_source("tenet", raw)

        first = await c.fetch_wiki_source(mod.WIKI_RAW_TENET, ttl=600, validator=validate)
        second = await c.fetch_wiki_source(mod.WIKI_RAW_TENET, ttl=600, validator=validate)
        check("有效HTTP正文服从TTL且不重复GET", first == second and len(h.http_calls) == 1)
        c._wiki_source_cache[mod.WIKI_RAW_TENET] = (time.time(), "invalid cached body")
        third = await c.fetch_wiki_source(mod.WIKI_RAW_TENET, ttl=600, validator=validate)
        check(
            "旧缓存正文不能绕过业务校验，拒绝后重新HTTP", third == first and len(h.http_calls) == 2
        )

    async def current_snapshot_missing_values(h, c):
        for key in ("tenet", "coda"):
            broken = setup_rot()
            groups = [broken[key]["items"]] if key == "tenet" else broken[key]["batches"]
            for rows in groups:
                rows[0].pop("bonus")
            h.put("rotations.json", broken)
            h.http_calls.clear()
            status = await c.refresh_valence()
            check(
                "本轮元数据缺值仍自动获取/" + key,
                status.startswith("refreshed") and bool(h.http_calls),
                str(status),
            )

    async def current_snapshot_missing_rows(h, c):
        for key in ("tenet", "coda"):
            broken = setup_rot()
            if key == "tenet":
                broken[key]["items"].pop()
            else:
                elapsed = int(
                    (now - datetime.fromisoformat(broken[key]["epoch"])) // timedelta(hours=96)
                )
                idx = (broken[key]["anchor_idx"] + elapsed) % 2
                broken[key]["batches"][idx].pop()
            h.put("rotations.json", broken)
            h.http_calls.clear()
            status = await c.refresh_valence()
            out = h.get("rotations.json")[key]
            count_ok = (
                len(out.get("items", [])) == 5
                if key == "tenet"
                else all(len(rows) == 7 for rows in out["batches"])
            )
            check(
                "本轮元数据缺行仍自动补抓/" + key,
                status.startswith("refreshed") and bool(h.http_calls) and count_ok,
                str(status),
            )

    async def seed_first_use(h, c):
        seed = json.loads((ROOT / "core/data/rotations.json").read_text("utf-8"))
        seed_bytes = (ROOT / "core/data/rotations.json").read_bytes()
        # 新安装与损坏副本均走 read_path 的包内骨架回退，再写临时运行目录。
        for corrupt in (False, True):
            (h.root / "rotations.json").unlink(missing_ok=True)
            (h.root / mod.ROTATION_REVISIONS_NAME).unlink(missing_ok=True)
            if corrupt:
                (h.root / "rotations.json").write_text("{bad", "utf-8")
            result = await c.check_rotation_revisions()
            out = h.get("rotations.json")
            check(
                "无值首次固定修订采用/损坏=" + str(corrupt),
                result["tenet"] == result["coda"] == "refreshed"
                and all(row.get("element") for rows in out["coda"]["batches"] for row in rows),
            )
            check(
                "首次采用只写runtime不改种子",
                (ROOT / "core/data/rotations.json").read_bytes() == seed_bytes,
            )
        for channel in ("community", "wfhelper"):
            h.put("rotations.json", seed)
            (h.root / mod.ROTATION_REVISIONS_NAME).unlink(missing_ok=True)
            data = h.rot["coda"]
            elapsed = int((now - datetime.fromisoformat(data["epoch"])) // timedelta(hours=96))
            bi = (data["anchor_idx"] + elapsed) % len(data["batches"])
            label = data["batch_label"][bi]
            current = {row["en"]: (row["element"], row["bonus"]) for row in data["batches"][bi]}
            payload = {
                "tenet": {
                    row["en"]: (row["element"], row["bonus"]) for row in h.rot["tenet"]["items"]
                },
                "coda": current if channel == "community" else {label: current},
                "coda_batch": label,
                "snapshot": stamp.isoformat(),
                "snapshots": {"tenet": stamp.isoformat(), "coda": stamp.isoformat()},
            }
            c._valence_fallback = AsyncMock(return_value=(channel, payload))
            result = await c.refresh_valence(community_only=True)
            out = h.get("rotations.json")
            check(
                "无值首次兜底采用/" + channel,
                result.startswith("refreshed")
                and all(row.get("element") for row in out["coda"]["batches"][bi]),
            )
            check(
                "兜底只有当前批时另一批不伪造",
                all(
                    "element" not in row and "bonus" not in row
                    for row in out["coda"]["batches"][1 - bi]
                ),
            )
        h.put("rotations.json", seed)
        before = (h.root / "rotations.json").read_bytes()
        c._valence_fallback = AsyncMock(return_value=("community", None))
        check(
            "首次离线失败保留无值骨架并继续过期",
            await c.refresh_valence(community_only=True) == "no-community"
            and (h.root / "rotations.json").read_bytes() == before
            and c.valence_is_stale(h.get("rotations.json")["tenet"], now),
        )

    async def all_cases():
        await in_case(current_snapshot_missing_rows)
        await in_case(current_snapshot_missing_values)
        await in_case(seed_first_use)
        await in_case(direct_without_fs, stale=True)
        await in_case(direct_rejects_to_fs)
        await in_case(direct_failure_community, stale=True)
        await in_case(direct_api_errors_and_retry)
        await in_case(direct_cache)
        await in_case(protocol_errors)
        await in_case(initial)
        await in_case(stale_same, stale=True)
        await in_case(invalid)
        await in_case(offerings)
        await in_case(no_replay)
        await in_case(monotonic)
        await in_case(group_no_replay)
        await in_case(pinned_legacy)
        await in_case(partial_unprocessed, stale=True)
        await in_case(timed_fallback)
        await in_case(writes)
        await in_case(lock_cancel)
        await in_case(closing)

    asyncio.run(all_cases())


with patch("datetime.datetime", FixtureClock):
    _revision_client_checks()


def _wiki_governance_checks():
    """只用 MockTransport 验证 Wiki 请求礼仪；不访问实际 Wiki/FS。"""
    import asyncio
    import time
    from email.utils import formatdate
    from urllib.parse import parse_qs, urlsplit
    import httpx

    host = "wiki.governance.invalid"
    raw_url = f"https://{host}/w/Test?action=raw"
    api_url = f"https://{host}/api.php?action=query&prop=revisions&revids=101&format=json"
    api_body = {
        "query": {
            "pages": {
                "1": {
                    "pageid": 1,
                    "title": "Tenet Weapons",
                    "revisions": [{"revid": 101, "timestamp": "2026-10-28T11:59:00+00:00"}],
                }
            }
        }
    }

    async def make(handler, *, timed=False):
        client = WC(flare_urls=["http://first.fs.invalid/v1", "http://second.fs.invalid/v1"])
        if not timed:
            client.WIKI_MIN_INTERVAL_S = 0
        await client._http.aclose()
        client._http = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), headers={"User-Agent": mod.USER_AGENT}
        )
        return client

    async def denied(client, url, *, via_fs=False):
        try:
            if via_fs:
                await client.fetch_via_flaresolver(url, ttl=0)
            else:
                await client.fetch_wiki_source(url, ttl=0)
        except mod.WarframeAPIError:
            return True
        return False

    async def maxlag_urls():
        for fs in (False, True):
            seen = []

            async def handler(req):
                if req.method == "GET":
                    seen.append(("GET", str(req.url)))
                    if fs:
                        return httpx.Response(403, text="fixture challenge")
                    return httpx.Response(200, json=api_body)
                payload = json.loads(req.content)
                seen.append((payload["cmd"], payload["url"]))
                return httpx.Response(
                    200,
                    json={
                        "status": "ok",
                        "solution": {"status": 200, "response": json.dumps(api_body)},
                    },
                )

            c = await make(handler)
            try:
                await c.fetch_wiki_source(api_url, ttl=0)
                check(
                    "固定revid的API在直连及FS入口统一带maxlag=5/" + str(fs),
                    bool(seen)
                    and all(
                        parse_qs(urlsplit(url).query).get("maxlag") == ["5"] for _, url in seen
                    ),
                    str(seen),
                )
                for suffix in ("&maxlag=", "&maxlag=1&maxlag=2", "&maxlag=5&maxlag=5"):
                    seen.clear()
                    await c.fetch_wiki_source(api_url + suffix, ttl=0)
                    check(
                        "API空值/重复maxlag归一为单个5/" + str(fs) + suffix,
                        bool(seen)
                        and all(
                            parse_qs(urlsplit(url).query).get("maxlag") == ["5"] for _, url in seen
                        ),
                        str(seen),
                    )
                seen.clear()
                await c.fetch_wiki_source(raw_url, ttl=0)
                check(
                    "raw源页不添加API专属maxlag/" + str(fs),
                    bool(seen)
                    and all("maxlag" not in parse_qs(urlsplit(url).query) for _, url in seen),
                    str(seen),
                )
            finally:
                await c.close()
        check(
            "三个内置修订API声明maxlag=5",
            all(
                parse_qs(urlsplit(url).query).get("maxlag") == ["5"]
                for url in (
                    mod.ROTATION_REVISION_API,
                    mod.VALENCE_REVISION_API,
                    mod.ACRITHIS_REVISION_API,
                )
            ),
        )
        check(
            "User-Agent含插件标识及公开仓联系方式",
            "warframe-sdjk/" in mod.USER_AGENT
            and "https://github.com/skyti1437/Warframe-SDJK-Bot" in mod.USER_AGENT,
            mod.USER_AGENT,
        )

    async def retry_headers():
        fixed = time.time()
        fixed_mono = time.monotonic()
        cases = (
            ("秒数", "1200", 1200.0),
            ("HTTP日期", formatdate(fixed + 3600, usegmt=True), 3599.0),
            ("长等待", "999999999999999999999999999999999999999999", 1e41),
            ("溢出长秒数", "9" * 400, float("inf")),
            ("无效值", "fixture-invalid", 60.0),
            ("负数", "-1", 60.0),
            ("非有限值", "NaN", 60.0),
        )
        for name, value, minimum in cases:
            calls = []

            async def handler(req):
                calls.append(req.method)
                return httpx.Response(503, headers={"Retry-After": value}, text="busy")

            c = await make(handler)
            try:
                with patch.object(mod.time, "time", return_value=fixed):
                    failed = await denied(c, raw_url)
                    again = await denied(c, raw_url + "&other=1")
                    until = c.__dict__.get("_wiki_retry_until", {}).get(host, 0)
                check(
                    "Retry-After不截短且同host跨URL冷却/" + name,
                    failed and again and calls == ["GET"] and until - fixed_mono >= minimum,
                    str((calls, until - fixed_mono, minimum)),
                )
            finally:
                await c.close()

    async def upstream_backoff():
        # 这些源站信号都应终止本轮；候选FS地址与会话重建不得绕开源站冷却。
        cases = (
            ("FS429", 429, {"Retry-After": "1200"}, "busy", 1200),
            ("FS顶层session错误429", 429, {"Retry-After": "5400"}, "busy", 5400),
            ("FS503", 503, {"retry-after": "7200"}, "busy", 7200),
            (
                "FS200maxlag",
                200,
                {"Retry-After": "3600"},
                json.dumps({"error": {"code": "maxlag"}}),
                3600,
            ),
            ("FS200ratelimited", 200, {}, json.dumps({"error": {"code": "ratelimited"}}), 60),
        )
        for name, status, headers, body, minimum in cases:
            calls = []

            async def handler(req):
                calls.append((req.method, str(req.url)))
                if req.method == "GET":
                    return httpx.Response(403, text="fixture challenge")
                payload = json.loads(req.content)
                assert payload["cmd"] == "request.get", payload
                return httpx.Response(
                    200,
                    json={
                        "status": "error" if "顶层" in name else "ok",
                        "solution": {"status": status, "headers": headers, "response": body},
                        "message": "Error solving session fixture",
                    },
                )

            c = await make(handler)
            fixed = time.time()
            fixed_mono = time.monotonic()
            try:
                with patch.object(mod.time, "time", return_value=fixed):
                    failed = await denied(c, api_url)
                    until = c.__dict__.get("_wiki_retry_until", {}).get(host, 0)
                    again = await denied(c, raw_url)
                    fs_again = await denied(c, raw_url, via_fs=True)
                check(
                    "源站退避不换候选/不重建/不缩短已有冷却/" + name,
                    failed
                    and again
                    and fs_again
                    and [method for method, _ in calls] == ["GET", "POST"]
                    and until - fixed_mono >= minimum
                    and not c._flare_cache
                    and not c.__dict__.get("_wiki_source_cache", {}),
                    str((calls, until - fixed_mono)),
                )
            finally:
                await c.close()
        # 直连API在HTTP200时仍须把maxlag当作退避，而非转FS尝试。
        calls = []

        async def handler(req):
            calls.append(req.method)
            return httpx.Response(
                200, headers={"Retry-After": "1800"}, json={"error": {"code": "maxlag"}}
            )

        c = await make(handler)
        try:
            fixed = time.time()
            fixed_mono = time.monotonic()
            with patch.object(mod.time, "time", return_value=fixed):
                failed = await denied(c, api_url)
                until = c.__dict__.get("_wiki_retry_until", {}).get(host, 0)
            check(
                "直连API200maxlag不走FS", failed and calls == ["GET"] and until - fixed_mono >= 1800
            )
        finally:
            await c.close()

    async def invalid_fs_http():
        calls = []

        async def handler(req):
            calls.append(req.method)
            return httpx.Response(
                200,
                json={"status": "ok", "solution": {"status": 500, "response": "invalid HTTP body"}},
            )

        c = await make(handler)
        c._flare_urls = c._flare_urls[:1]
        try:
            first = await denied(c, raw_url, via_fs=True)
            second = await denied(c, raw_url, via_fs=True)
            check(
                "FS源站HTTP错误正文不得缓存或当成功返回",
                first and second and not c._flare_cache and len(calls) == 1,
                str((first, second, calls, c._flare_cache)),
            )
        finally:
            await c.close()

    async def in_flight_retry_maximum():
        short_started = asyncio.Event()
        release_short = asyncio.Event()
        calls = []

        async def handler(req):
            calls.append(str(req.url))
            if "short=1" in str(req.url):
                short_started.set()
                await release_short.wait()
                wait = "60"
            else:
                wait = "7200"
            return httpx.Response(503, headers={"Retry-After": wait}, text="busy")

        c = await make(handler)
        try:
            short = asyncio.create_task(denied(c, raw_url + "&short=1"))
            await short_started.wait()
            long_failed = await denied(c, raw_url + "&long=1")
            long_until = c._wiki_retry_until[host]
            release_short.set()
            short_failed = await short
            final_until = c._wiki_retry_until[host]
            blocked = await denied(c, raw_url)
            check(
                "两条在途响应中后到短Retry-After不能缩短既有长冷却",
                long_failed
                and short_failed
                and blocked
                and len(calls) == 2
                and final_until >= long_until,
                str((calls, long_until, final_until)),
            )
        finally:
            release_short.set()
            await c.close()

    async def redirect_stops():
        calls = []

        async def handler(req):
            calls.append((req.method, str(req.url)))
            return httpx.Response(
                302, headers={"Location": "https://redirect.governance.invalid/final"}
            )

        c = await make(handler)
        c._http.follow_redirects = True
        try:
            failed = await denied(c, raw_url)
            check(
                "Wiki重定向终点零调用且不接力FS",
                failed and calls == [("GET", raw_url)] and not c._flare_cache,
                str(calls),
            )
        finally:
            await c.close()

    async def fs_protocol_boundaries():
        cases = (
            ("非字典顶层", []),
            ("非字典solution", {"status": "ok", "solution": []}),
            ("缺源站状态", {"status": "ok", "solution": {"response": "body"}}),
            ("布尔源站状态", {"status": "ok", "solution": {"status": True, "response": "body"}}),
            ("越界源站状态", {"status": "ok", "solution": {"status": 999, "response": "body"}}),
            ("空正文", {"status": "ok", "solution": {"status": 200, "response": " "}}),
            ("非文本正文", {"status": "ok", "solution": {"status": 200, "response": ["body"]}}),
            (
                "挑战头",
                {
                    "status": "ok",
                    "solution": {
                        "status": 200,
                        "headers": {"cf-mitigated": "challenge"},
                        "response": "body",
                    },
                },
            ),
        )
        for name, payload in cases:
            calls = []

            async def handler(req):
                calls.append((str(req.url), json.loads(req.content)["cmd"]))
                return httpx.Response(200, json=payload)

            c = await make(handler)
            try:
                failed = await denied(c, raw_url, via_fs=True)
                check(
                    "FS非法协议拒绝且不缓存不换候选不重建/" + name,
                    failed and len(calls) == 1 and not c._flare_cache,
                    str((calls, c._flare_cache)),
                )
            finally:
                await c.close()

    async def separate_client_caches():
        bodies = []

        async def handler(req):
            body = "client fixture " + str(len(bodies) + 1)
            bodies.append(body)
            return httpx.Response(
                200, json={"status": "ok", "solution": {"status": 200, "response": body}}
            )

        first, second = await make(handler), await make(handler)
        try:
            a = await first.fetch_via_flaresolver(raw_url, ttl=600)
            cached = await first.fetch_via_flaresolver(raw_url, ttl=600)
            b = await second.fetch_via_flaresolver(raw_url, ttl=600)
            check(
                "FS缓存仅同client复用且跨client隔离",
                a == cached
                and a != b
                and len(bodies) == 2
                and first._flare_cache is not second._flare_cache,
                str(bodies),
            )
        finally:
            await first.close()
            await second.close()

    async def fs_control_redirect_stops():
        calls = []

        async def handler(req):
            calls.append(str(req.url))
            return httpx.Response(
                307, headers={"Location": "https://redirect.governance.invalid/fs-final"}
            )

        c = await make(handler)
        c._http.follow_redirects = True
        c._flare_urls = c._flare_urls[:1]
        try:
            failed = await denied(c, raw_url, via_fs=True)
            check(
                "FS控制端307不自动转发请求到终点",
                failed
                and bool(calls)
                and set(calls) == {"http://first.fs.invalid/v1"}
                and not c._flare_cache,
                str(calls),
            )
        finally:
            await c.close()

    async def real_request_gate():
        starts = []

        async def handler(req):
            if req.method == "GET":
                url = str(req.url)
            else:
                payload = json.loads(req.content)
                assert payload["cmd"] == "request.get", payload
                url = payload["url"]
            starts.append((req.method, url, time.monotonic()))
            if req.method == "GET" and "fallback" in url:
                return httpx.Response(403, text="fixture challenge")
            if req.method == "GET":
                return httpx.Response(200, text="valid fixture source")
            return httpx.Response(
                200,
                json={
                    "status": "ok",
                    "solution": {"status": 200, "response": "valid fixture source"},
                },
            )

        c = await make(handler, timed=True)
        try:
            check("Wiki默认最小请求间隔为一秒", c.WIKI_MIN_INTERVAL_S == 1.0)
            await asyncio.gather(
                c.fetch_wiki_source(raw_url, ttl=600),
                c.fetch_wiki_source(raw_url + "&fallback=1", ttl=0),
            )
            gaps = [b[2] - a[2] for a, b in zip(starts, starts[1:])]
            print("[EVIDENCE] Wiki GET/FS起始间隔（秒）:", gaps)
            check(
                "并发GET及接力FS共用按host的一秒起始间隔",
                len(starts) == 3 and all(gap >= 0.98 for gap in gaps),
                str((starts, gaps)),
            )
            before = len(starts)
            # 有效缓存即使处于同host冷却也不消耗请求配额。
            c.__dict__.setdefault("_wiki_retry_until", {})[host] = time.monotonic() + 3600
            value = await c.fetch_wiki_source(raw_url, ttl=600)
            check(
                "缓存命中不触发网络且不受请求冷却阻挡",
                value == "valid fixture source" and len(starts) == before,
            )
            c._wiki_retry_until.clear()
            pending = asyncio.create_task(c.fetch_wiki_source(raw_url + "&cancel=1", ttl=0))
            await asyncio.sleep(0.02)
            pending.cancel()
            try:
                await pending
            except asyncio.CancelledError:
                pass
            check("取消间隔等待不多发GET或FS", len(starts) == before, str(starts))
            started = time.monotonic()
            await c.fetch_wiki_source("https://other.governance.invalid/w/Test?action=raw", ttl=0)
            check("不同host独立计时不被另一host阻挡", time.monotonic() - started < 0.5)
        finally:
            await c.close()

    async def cases():
        await maxlag_urls()
        await retry_headers()
        await upstream_backoff()
        await invalid_fs_http()
        await in_flight_retry_maximum()
        await redirect_stops()
        await fs_protocol_boundaries()
        await separate_client_caches()
        await fs_control_redirect_stops()
        await real_request_gate()

    asyncio.run(cases())


_wiki_governance_checks()


def _revision_loop_checks():
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch

    async def run_case(phase, values, expected_delays, states):
        obj = object.__new__(P.WarframeSDJK)
        client = SimpleNamespace(
            _closing=False, check_rotation_revisions=AsyncMock(side_effect=values)
        )
        obj.client = client
        obj._flare_phase = AsyncMock(return_value=phase)
        waits, outcomes = [], []

        async def sleep(key, delay):
            waits.append(delay)
            if len(waits) > len(values):
                raise asyncio.CancelledError

        obj._background_sleep = sleep
        obj._note_background = lambda key, state, detail="": outcomes.append(state)
        with patch.object(P.random, "uniform", return_value=0):
            try:
                await obj._revision_autoloop()
            except asyncio.CancelledError:
                pass
        check(
            f"修订调度{phase}延迟/状态",
            waits == expected_delays and outcomes == states,
            str((waits, outcomes)),
        )
        return client

    async def community_case():
        obj = object.__new__(P.WarframeSDJK)
        obj._flare_phase = AsyncMock(side_effect=AssertionError("社区调度不依赖FS探测"))
        obj._refresh_from_community = AsyncMock(return_value={"valence": "fresh"})
        obj._refresh_static_community = AsyncMock(return_value={"static": "fresh"})
        obj._note_background = lambda *args: None
        delays = []

        async def sleep(key, delay):
            delays.append(delay)
            if len(delays) > 6:
                raise asyncio.CancelledError

        obj._background_sleep = sleep
        try:
            await obj._community_autoloop()
        except asyncio.CancelledError:
            pass
        check(
            "社区独立每30分钟轮询、每6轮静态刷新，不受FS状态拦截",
            delays == [1800] * 7
            and obj._refresh_from_community.await_count == 6
            and obj._refresh_static_community.await_count == 1
            and obj._flare_phase.await_count == 0,
        )

    async def cases():
        await community_case()
        result = dict.fromkeys(mod.ROTATION_PAGES, "unchanged")
        for phase in ("off", "absent"):
            c = await run_case(phase, [result], [0, 900], ["running", "ok", "cancelled"])
            check(f"修订调度FS {phase}仍检查HTTP源", c.check_rotation_revisions.await_count == 1)
        await run_case("ready", [result], [0, 900], ["running", "ok", "cancelled"])
        changed = {**result, "tenet": "refreshed"}
        await run_case("ready", [changed], [0, 900], ["running", "ok", "cancelled"])
        failed = {**result, "coda": "failed"}
        await run_case(
            "ready",
            [failed, failed, result],
            [0, 1800, 3600, 900],
            ["running", "degraded", "running", "degraded", "running", "ok", "cancelled"],
        )
        await run_case(
            "ready",
            [{**result, "coda": "not-updated"}],
            [0, 900],
            ["running", "degraded", "cancelled"],
        )
        await run_case(
            "ready", [RuntimeError("fixture")], [0, 1800], ["running", "error", "cancelled"]
        )
        await run_case(
            "ready", [TimeoutError("fixture timeout")], [0, 1800], ["running", "error", "cancelled"]
        )

    asyncio.run(cases())


_revision_loop_checks()


print()
if FAILED:
    print(f"✗ {len(FAILED)} 项失败：" + "、".join(FAILED))
    sys.exit(1)
print("✓ 全部通过")
