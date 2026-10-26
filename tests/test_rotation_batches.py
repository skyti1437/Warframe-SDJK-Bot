# -*- coding: utf-8 -*-
"""信条 / 终幕 / 灵化轮换卡离线测试（python3 tests/test_rotation_batches.py）

覆盖本轮新接线的两个轮换模式（版式对齐既有「灵化回廊」）：

1. **信条（Tenet，``mode=refresh_only``）**：Ergo Glast 在任意中继站**常驻** 5 把
   （Agendus / Exec / Ferrox / Grigori / Livia），每把 40 个腐化全息密钥；只有
   **元素加成**每 4 天 00:00 UTC 重生成。所以卡面不该出现「A/B 批」这类
   不存在的分批描述。
2. **终幕（Coda，``mode=batch_cycle``）**：Eleanor 的 Coda 武器按 **A/B 两批**
   每 4 天换批，A ∪ B = 14 把（各 7 把），批次表来自 wiki.warframe.com/w/Reset。
   ⚠️ 终幕的换批边界**比信条的元素加成重生成晚 24 小时**（2026-09-14 实测：
   终幕 09-17 00:00 UTC / 信条 09-16 00:00 UTC），两者**不是**同一个锚点，
   别为了"看起来整齐"去对齐 epoch —— 对齐会让终幕整体错位一天。

断言对「今天是哪天」不敏感：批次下标按 epoch + anchor_idx 在测试里独立重算，
再和卡面文字对齐，避免把某一天的快照写死。

★ 2026-10-15：本脚本只做**逻辑回归**，全程走固定时钟（种子卡面按种子自身的快照时刻求值，
换批边界/跨周期/过期快照用临时 fixture 覆盖）。「随包快照此刻是否还新鲜」属于数据新鲜度，
已移到 ``python scripts/run_checks.py health``（判据同运行期 ``valence_is_stale``），
不再让本脚本随日期自然变红。
"""

from __future__ import annotations

import datetime as _dt
import json
import re
import shutil
import sys
import tempfile
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _install_astrbot_stub() -> None:
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

    class Image:
        pass

    class Plain:
        pass

    mc_mod.Image = Image
    mc_mod.Plain = Plain

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
    sys.modules.setdefault("astrbot", pkg)
    sys.modules["astrbot.api"] = api
    sys.modules["astrbot.api.event"] = event_mod
    sys.modules["astrbot.api.message_components"] = mc_mod
    sys.modules["astrbot.api.star"] = star_mod


_install_astrbot_stub()

import main as plugin  # noqa: E402
from core import calculators as calc  # noqa: E402
from core import formatters as fmt  # noqa: E402
from core import paths as core_paths  # noqa: E402

ROT = json.loads((ROOT / "core" / "data" / "rotations.json").read_text(encoding="utf-8"))

# —— 固定时钟（2026-10-15 加）——
# 运行期在函数内 ``from datetime import datetime`` 后取 ``datetime.now``，倒计时走
# ``formatters._now``；这里把两处都换成可设的时钟，运行期代码不动。
_REAL_DT = _dt.datetime
_CLOCK: list = [None]


class _FrozenDT(_REAL_DT):
    @classmethod
    def now(cls, tz=None):
        if _CLOCK[0] is None:
            return _REAL_DT.now(tz)
        return _CLOCK[0].astimezone(tz) if tz else _CLOCK[0]


_dt.datetime = _FrozenDT
fmt._now = lambda: _FrozenDT.now(timezone.utc)

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = ""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f"  -> {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


def _obj():
    obj = plugin.WarframeSDJK.__new__(plugin.WarframeSDJK)
    obj._dir = Path(tempfile.mkdtemp())
    return obj


OBJ = _obj()


# ------------------------------------------------------------------ 表结构
tenet = ROT.get("tenet") or {}
coda = ROT.get("coda") or {}
inc = ROT.get("incarnon") or {}

check(
    "rotations.json 有 tenet/coda/incarnon 三段",
    all(k in ROT for k in ("tenet", "coda", "incarnon")),
)
check("信条 mode = refresh_only", tenet.get("mode") == "refresh_only", str(tenet.get("mode")))
check("信条周期 96 小时（4 天）", int(tenet.get("period_hours", 0)) == 96)
check("信条 5 把常驻", len(tenet.get("items") or []) == 5, str(len(tenet.get("items") or [])))
check("信条无 batches 字段（不存在分批）", not tenet.get("batches"))
_TENET_EN = {i.get("en") for i in (tenet.get("items") or [])}
check(
    "信条武器清单与 wiki 一致",
    _TENET_EN == {"Tenet Agendus", "Tenet Exec", "Tenet Ferrox", "Tenet Grigori", "Tenet Livia"},
    str(sorted(_TENET_EN)),
)
check("信条每把都有官方简中名", all(i.get("cn") for i in (tenet.get("items") or [])))
check(
    "信条简中名都带「信条·」前缀",
    all((i.get("cn") or "").startswith("信条·") for i in (tenet.get("items") or [])),
    str([i.get("cn") for i in (tenet.get("items") or [])]),
)

check("终幕 mode = batch_cycle", coda.get("mode") == "batch_cycle", str(coda.get("mode")))
check("终幕周期 96 小时（4 天）", int(coda.get("period_hours", 0)) == 96)
_batches = coda.get("batches") or []
check("终幕 2 批", len(_batches) == 2, str(len(_batches)))
check("终幕每批 7 把", all(len(b) == 7 for b in _batches), str([len(b) for b in _batches]))
_ALL_CODA = [x.get("en") for b in _batches for x in b]
check("终幕两批共 14 把且不重复", len(set(_ALL_CODA)) == 14, str(len(set(_ALL_CODA))))
check(
    "终幕批次标签为 A/B",
    list(coda.get("batch_label") or []) == ["A", "B"],
    str(coda.get("batch_label")),
)
check("终幕锚点下标在范围内", 0 <= int(coda.get("anchor_idx", -1)) < len(_batches))
check(
    "终幕每把都有官方简中名（终幕·XXX）",
    all((x.get("cn") or "").startswith("终幕·") for b in _batches for x in b),
    str([x.get("cn") for b in _batches for x in b if not (x.get("cn") or "").startswith("终幕·")]),
)

# 种子卡面按种子自身的快照时刻求值（确定性；快照缺失/畸形时退回终幕 epoch）
try:
    _SEED_AT = datetime.fromisoformat(coda["valence_snapshot"])
except (KeyError, TypeError, ValueError):
    _SEED_AT = datetime.fromisoformat(coda["epoch"])
_CLOCK[0] = _SEED_AT


# ------------------------------------------------------------- 卡面（信条）
title, lines = (
    OBJ._rotation_lines("tenet", "Ergo 信条武器轮换").title,
    OBJ._rotation_lines("tenet", "Ergo 信条武器轮换").lines,
)
check("信条卡标题", title == "Ergo 信条武器轮换", title)
check("信条卡说明「常驻」", any("常驻" in ln for ln in lines), str(lines))
check("信条卡列出 5 把", sum(1 for ln in lines if ln.startswith("· 信条·")) == 5, str(lines))
check("信条卡不含「批」字（常驻无分批）", not any("批" in ln for ln in lines), str(lines))
check(
    "信条卡标注元素加成刷新周期", any("元素加成" in ln and "4 天" in ln for ln in lines), str(lines)
)
check(
    "信条卡说明获取成本（40 个腐化全息密钥）", any("腐化全息密钥" in ln for ln in lines), str(lines)
)
check(
    "信条卡用 · 列项（次级层级）",
    all(ln.startswith("· ") or ln.startswith("◆") or ln.startswith("※") for ln in lines),
    str(lines),
)


# ------------------------------------------------------------- 卡面（终幕）
reply = OBJ._rotation_lines("coda", "Coda 终幕武器轮换")
lines = reply.lines
check("终幕卡标题", reply.title == "Coda 终幕武器轮换", reply.title)

# 独立重算当前批次下标，再和卡面对齐（不写死某一天）
_epoch = datetime.fromisoformat(coda["epoch"])
_period = timedelta(hours=int(coda["period_hours"]))
_now = _SEED_AT
_passed = int((_now - _epoch) // _period)
_idx = (int(coda["anchor_idx"]) + _passed) % len(_batches)
_labels = coda["batch_label"]
check(
    f"终幕卡标注当前批次（{_labels[_idx]} 批）",
    # ★ 2026-10-11 第二步改版：「◆ 当前为 B 批（…）」改为 KV「◆ 当前批次：B 批｜共 2 批轮换…」，语义不变
    any(ln.startswith("◆ 当前批次：") and f"{_labels[_idx]} 批" in ln for ln in lines),
    str(lines[:1]),
)
check("终幕卡列出当前批 7 把", sum(1 for ln in lines if ln.startswith("· 终幕·")) == 7, str(lines))
_next = _labels[(_idx + 1) % len(_batches)]
check(
    f"终幕卡给出下一批（{_next}）",
    any(ln.startswith("◆ 下一批") and _next in ln for ln in lines),
    str(lines),
)
check("终幕卡标注换批周期（每 4 天）", any("每 4 天" in ln for ln in lines), str(lines))
check("终幕卡给出换批倒计时", any("距换批" in ln for ln in lines), str(lines))

# 下一批列出的 7 把 = 另一批
_next_line = [ln for ln in lines if ln.startswith("◆ 下一批")][0]
_other = _batches[(_idx + 1) % len(_batches)]
check(
    "终幕「下一批」列全另一批 7 把",
    all((x.get("cn") or "") in _next_line for x in _other),
    _next_line,
)

# 两次调用的当前批一致（幂等，无随机）
again = OBJ._rotation_lines("coda", "Coda 终幕武器轮换").lines
check("终幕卡幂等（同刻两次结果一致）", again == lines)


# ------------------------------------------------------------- 卡面（灵化）
reply = OBJ._rotation_lines("incarnon", "本周钢铁回廊灵化")
check(
    "灵化卡仍是周回环", any("第 " in ln and "/" in ln for ln in reply.lines), str(reply.lines[:1])
)
# 三种模式都遵守同一版式骨架：◆ 段标题 + · 列项
for key, ttl in (
    ("incarnon", "本周钢铁回廊灵化"),
    ("tenet", "Ergo 信条武器轮换"),
    ("coda", "Coda 终幕武器轮换"),
):
    ls = OBJ._rotation_lines(key, ttl).lines
    check(f"{key} 版式对齐（首行是 ◆ 段标题）", ls and ls[0].startswith("◆"), str(ls[:1]))
    check(f"{key} 有 ※ 数据来源/说明行", any(x.startswith("※") for x in ls), str(ls))


# --------------------------------------------------------- 元素加成（快照）
# 维护者 2026-09-12 反馈「信条和终幕缺失属性和加成数值」。加成值没有官方 API，
# 只有 wiki「Reset」页的玩家上报表（插件侧访问被 Cloudflare 拦 → 存快照）。
# 这里守住三件事：表里有值、值在合法区间、卡面真的印出来了。
_tenet_items = tenet.get("items") or []
check(
    "信条每把都有元素",
    all(i.get("element") for i in _tenet_items),
    str([i.get("en") for i in _tenet_items if not i.get("element")]),
)
check(
    "信条每把都有加成数值",
    all(isinstance(i.get("bonus"), (int, float)) for i in _tenet_items),
    str([i.get("en") for i in _tenet_items if not isinstance(i.get("bonus"), (int, float))]),
)
check(
    "信条元素都在展示表中",
    all(i["element"].lower() in calc.ELEM_ZH for i in _tenet_items if i.get("element")),
    str(
        [
            i["element"]
            for i in _tenet_items
            if i.get("element") and i["element"].lower() not in calc.ELEM_ZH
        ]
    ),
)
check(
    "信条加成在 25~60 区间",
    all(25 <= float(i["bonus"]) <= 60 for i in _tenet_items if i.get("bonus")),
    str([i.get("bonus") for i in _tenet_items]),
)

# ⚠️ 抽查/完整性都要对着**当前生效批**（_idx），不能对着 anchor_idx ——
# anchor_idx 是换批周期里的相位基准，换批后它不变、生效批会前进一格，
# 写死 anchor_idx 会在换批当天误报（2026-09-20 修正）。
_cur_batch = _batches[_idx]
check(
    "终幕当前批每把都有元素+加成",
    all(i.get("element") and isinstance(i.get("bonus"), (int, float)) for i in _cur_batch),
    str(
        [
            i.get("en")
            for i in _cur_batch
            if not (i.get("element") and isinstance(i.get("bonus"), (int, float)))
        ]
    ),
)
_other_batch = _batches[(int(coda.get("anchor_idx", 0)) + 1) % len(_batches)]
# ⚠️ 2026-09-17 更新：旧断言是「另一批不许填加成」，那是因为当时只抓得到
# 当前批。后来核对 wiki「Reset」页发现它**同时公布 Batch A / Batch B 两张表**，
# 两批都能拿到真实值（scripts/fetch_valence.py 会把两批都写回）。
# 旧断言已失效，但「不许瞎编」的本意要留住 —— 改为**整批完整性**约束：
#   一批要么全空（压根没抓到），要么 7 把全有；**半填 = 漏抓或瞎编**。
for _bi, _b in enumerate(_batches):
    _blabel = (coda.get("batch_label") or ["A", "B"])[_bi]
    _filled = [i for i in _b if i.get("element")]
    check(
        f"终幕 {_blabel} 批要么全空要么全有（禁止半填/瞎编）",
        len(_filled) in (0, len(_b)),
        f"filled={len(_filled)}/{len(_b)}",
    )
    _bad_vals = [
        (i.get("en"), i.get("element"), i.get("bonus"))
        for i in _filled
        if not (
            (i.get("element") or "").lower() in calc.ELEM_ZH
            and isinstance(i.get("bonus"), (int, float))
            and 25 <= float(i["bonus"]) <= 60
        )
    ]
    check(
        f"终幕 {_blabel} 批已填的值都合法（元素在展示表内、加成 25~60）",
        not _bad_vals,
        str(_bad_vals),
    )

for _k, _d in (("tenet", tenet), ("coda", coda)):
    _raw = _d.get("valence_snapshot") or ""
    check(f"{_k} 有加成快照时间", bool(_raw), str(_raw))
    try:
        _snap = datetime.fromisoformat(_raw)
        _ok = True
    except (TypeError, ValueError):
        _snap, _ok = None, False
    check(f"{_k} 快照时间可解析", _ok, str(_raw))
# 「快照落在此刻的轮次内（未过期）」是数据新鲜度，不是逻辑回归 ⇒ 见
# scripts/run_checks.py health；过期判定的边界逻辑由文末固定时钟 fixture 覆盖。

# —— 卡面：每条武器行都带「元素 百分比」，并有一行快照说明 ——
_reply_t = OBJ._rotation_lines("tenet", "Ergo 信条武器轮换")
_reply_c = OBJ._rotation_lines("coda", "Coda 终幕武器轮换")
for _name, _rep, _n in (("信条", _reply_t, 5), ("终幕", _reply_c, 7)):
    _item_rows = [ln for ln in _rep.lines if ln.startswith("· ")]
    check(f"{_name} 卡列出 {_n} 把", len(_item_rows) == _n, str(len(_item_rows)))
    check(
        f"{_name} 卡每把都带元素与百分比",
        all(re.search(r"　\S+ \d+(\.\d+)?%$", ln) for ln in _item_rows),
        str([ln for ln in _item_rows if not re.search(r"　\S+ \d+(\.\d+)?%$", ln)]),
    )
    check(
        f"{_name} 卡元素名是中文展示名",
        all(any(zh in ln for zh in calc.ELEM_ZH.values()) for ln in _item_rows),
        str(_item_rows[:2]),
    )
    check(f"{_name} 卡有快照说明行", any("快照" in ln for ln in _rep.lines), str(_rep.lines[-3:]))

# 具体值抽查（对照 wiki 快照，发版链第 0 步刷新后同步）
# ★ 2026-10-07 刷新（窗口 10-06/10-07 00:00 UTC 起，信条与终幕本轮都重掷）：
#   刷新源 = Tenet_Weapons / Coda_Weapons **源页 wikitext**（?action=raw，经服务器
#   FlareSolverr 抓取；口径见 scripts/fetch_valence.py 头注）。信条 5 把全部重掷 ——
#   Agendus Heat 47.2→Impact 39.2、Exec Toxin 38.8→Cold 39.8、Ferrox Radiation
#   25.7→Magnetic 36.8、Grigori Electricity 60.0→Heat 25.5、Livia Electricity
#   28.1→Toxin 25.1（上一轮 10-03 的值可在 git 历史里对）。
# ★ 2026-10-15 刷新（窗口 10-15 00:00 UTC 起，信条与终幕 A 批本轮都重掷）：刷新源 =
#   Coda_Weapons / Tenet_Weapons **源页 wikitext**（经服务器 FlareSolverr，见
#   scripts/fetch_valence.py）。本窗口抽查的两把：Ferrox Electricity 27.7→Cold 57.0、
#   Exec Toxin 25.1→Magnetic 29.8。
# ★ 2026-10-18 刷新（窗口 10-18 00:00 UTC 起，信条 5 把全重掷）：
#   上游 10-18T02:05:08Z 由 ~2026-ThinkingSingTogetherJiggy48724 修订该页
#   （"Updated the new valence percentages"）；运行期换轮后 17 分钟就抓到**尚未更新**
#   的旧表、把快照盖成新窗口时间，因而本轮不再重抓（详见
#   output/交接-给ZCode-信条种子探测-20261018-*.md），本段按上游源页实测值回写。
#   抽查两把：Ferrox Cold 57.0→26.5、Exec Magnetic 29.8→25.1。
# ★ 2026-10-18 元素列校正（用户 12:18 游戏内实拍 vs wiki 逐条比对）：
#   **wiki 这一轮只改了百分比、元素列原样留在 10-14 那轮** —— 编辑者备注自述
#   "Updated the new valence percentages"。百分比五个全对得上，元素四个不同：
#   Ferrox Cold→Electricity(电击)、Grigori Heat→Impact(冲击)、
#   Exec Magnetic→Toxin(毒素)、Agendus Magnetic→Radiation(辐射)；Livia Impact 未变。
#   故本段元素按**游戏内实拍**为准（wiki 修好后可回归 wiki 源）。
# ★ 2026-10-26 换轮（wiki Tenet_Weapons revid 2821771，2026-10-26T00:57:45Z，
#   经用户实拍核对）：五把全重掷 —— Ferrox Radiation 38.4、Exec Radiation 43.9；
#   种子由 fetch_valence 写回并同步 valence_values_since = 该次修订时刻。
#   （上一轮 10-22 的值是 Ferrox Electricity 30.8、Exec Magnetic 28.8，已随换轮作废。）
_tmap = {i["en"]: (i.get("element"), i.get("bonus")) for i in _tenet_items}
check(
    "信条·铁晶磁轨炮 = 辐射 38.4%（10-26 轮）",
    _tmap.get("Tenet Ferrox") == ("Radiation", 38.4),
    str(_tmap.get("Tenet Ferrox")),
)
check(
    "信条·枢密 = 辐射 43.9%（10-26 轮）",
    _tmap.get("Tenet Exec") == ("Radiation", 43.9),
    str(_tmap.get("Tenet Exec")),
)
check(
    "信条·莉薇娅 = 辐射 25.0%（10-26 轮，恰好 25 的边界）",
    _tmap.get("Tenet Livia") == ("Radiation", 25.0),
    str(_tmap.get("Tenet Livia")),
)
_cmap = {i["en"]: (i.get("element"), i.get("bonus")) for i in _cur_batch}
# 终幕按批次抽查：★ 每次终幕换批后，本表的当前批抽查值必须同步（A/B 两批的值都随
#   换批重掷，逐条取自刷新后的 core/data/rotations.json；刷新源 = 源页 wikitext）。
#   批次归属由 ``(anchor_idx + 换轮次数) % 2`` 计算 —— anchor_idx 是**相位基准**，
#   不等于当前批下标；epoch 2026-09-13、anchor_idx=0 ⇒ 该日起第 1 个 4 天窗口为 A 批。
#   终幕换批边界比信条晚 24h，**不要**为了整齐去对齐 epoch（对齐会整体错位一天）。
# ★ 2026-10-19 刷新（快照 10-19T00:17，用户游戏内实拍 —— FlareSolverr 当时 OOM 不可达）：
#   当期批 B 7 条全重掷 —— Bassocyst Radiation 38.3→Toxin 40.4、Synapse Heat 54.9→Toxin 39.4；
#   A 批本轮未取到新值（保持 10-15 的 Catabolyst Toxin 33.2 / Motovore Magnetic 25.7）。
# ★ 2026-10-23 刷新（wiki Coda_Weapons revid 2821059，2026-10-23T00:56:34Z）：A 批 7 条全重掷 ——
#   Catabolyst Toxin 29.7、Motovore Electricity 49.8；batch_values_since[A] 同步为该修订时刻。
_SPOT = {
    "A": {"Coda Catabolyst": ("Toxin", 29.7), "Coda Motovore": ("Electricity", 49.8)},
    "B": {"Coda Bassocyst": ("Toxin", 40.4), "Coda Synapse": ("Toxin", 39.4)},
}
_cur_label = (coda.get("batch_label") or ["A", "B"])[_idx]
for _en, _exp in _SPOT.get(_cur_label, {}).items():
    check(
        f"终幕当前批（{_cur_label}）·{_en} = {_exp[0]} {_exp[1]}%（wiki 快照）",
        _cmap.get(_en) == _exp,
        str(_cmap.get(_en)),
    )
check(
    "终幕当前批元素都在展示表中（电击/辐射不许是裸英文）",
    all((v[0] or "").lower() in calc.ELEM_ZH for v in _cmap.values()),
    str({k: v[0] for k, v in _cmap.items() if (v[0] or "").lower() not in calc.ELEM_ZH}),
)

# 2026-09-14 实测纠错：信条与终幕的换轮边界**相差 24 小时**（终幕晚一天），
# 别"顺手"把两者 epoch 对齐 —— 那会把终幕整体错位一天（当日线上事故就是这么来的）。
check(
    "终幕与信条的换轮锚点相差 24 小时（不是同一锚点）",
    abs(
        (
            datetime.fromisoformat(coda["epoch"]) - datetime.fromisoformat(tenet["epoch"])
        ).total_seconds()
    )
    == 86400,
    f"coda={coda['epoch']} tenet={tenet['epoch']}",
)
check(
    "终幕锚点对齐到 00:00 UTC（4 天网格整点）",
    (datetime.fromisoformat(coda["epoch"]).hour, datetime.fromisoformat(coda["epoch"]).minute)
    == (0, 0),
    coda["epoch"],
)
# ⚠️ 2026-09-17 更新：旧断言「周期内只有一批挂加成」已随 wiki 同时公布
# A/B 两表而失效（见上文整批完整性约束）。现在只要求一件事：
# **当前生效的那一批必须有值** —— 因为卡面印的就是它。
check(
    "终幕当前生效批必须有元素加成（卡面要印）",
    all(i.get("element") and isinstance(i.get("bonus"), (int, float)) for i in _batches[_idx]),
    str(
        [
            i.get("en")
            for i in _batches[_idx]
            if not (i.get("element") and isinstance(i.get("bonus"), (int, float)))
        ]
    ),
)

# ---------------------------------------------------------------------------
# ★ 2026-10-03 新增：刷新源解析器（scripts/fetch_valence.py::parse_wikitext）
#   用内联样例钉住行为（不联网）：Reset 渲染页受解析缓存影响会落后于源页，
#   换批后进行刷新必须走源页 wikitext（A/B 两批同页全量）。
# ---------------------------------------------------------------------------
import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "fetch_valence_under_test", ROOT / "scripts" / "fetch_valence.py"
)
_fv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fv)

# 真源响应的形状：<pre> 包裹 + 实体转义（实测 2026-10-03 抓取同形）
_WT = (
    '&lt;table class="wikitable"&gt;'
    "&lt;tr&gt;&lt;th&gt;Weapon (Batch A)&lt;/th&gt;&lt;th&gt;Element&lt;/th&gt;"
    "&lt;th&gt;Bonus %&lt;/th&gt;&lt;/tr&gt;"
    "&lt;tr&gt;&lt;td&gt;Coda Hema&lt;/td&gt;&lt;td&gt;{{D|Magnetic}}&lt;/td&gt;"
    "&lt;td&gt;{{ValenceBonusPercentageColor|34.2}}&lt;/td&gt;&lt;/tr&gt;"
    "&lt;tr&gt;&lt;td&gt;Coda Mire&lt;/td&gt;&lt;td&gt;{{D|Impact}}&lt;/td&gt;"
    "&lt;td&gt;{{ValenceBonusPercentageColor|28.3}}&lt;/td&gt;&lt;/tr&gt;"
    "&lt;/table&gt;"
    '&lt;table class="wikitable"&gt;'
    "&lt;tr&gt;&lt;th&gt;Weapon (Batch B)&lt;/th&gt;&lt;th&gt;Element&lt;/th&gt;"
    "&lt;th&gt;Bonus %&lt;/th&gt;&lt;/tr&gt;"
    "&lt;tr&gt;&lt;td&gt;Coda Bassocyst&lt;/td&gt;&lt;td&gt;{{D|Magnetic}}&lt;/td&gt;"
    "&lt;td&gt;{{ValenceBonusPercentageColor|31.6}}&lt;/td&gt;&lt;/tr&gt;"
    "&lt;/table&gt;"
    '&lt;table class="wikitable"&gt;'
    "&lt;tr&gt;&lt;th&gt;Weapon&lt;/th&gt;&lt;th&gt;Element&lt;/th&gt;"
    "&lt;th&gt;Bonus %&lt;/th&gt;&lt;/tr&gt;"
    "&lt;tr&gt;&lt;td&gt;{{Weapon|Tenet Ferrox}}&lt;/td&gt;&lt;td&gt;{{D|Radiation}}&lt;/td&gt;"
    "&lt;td&gt;{{ValenceBonusPercentageColor|25.7}}&lt;/td&gt;&lt;/tr&gt;"
    "&lt;/table&gt;"
)
_p = _fv.parse_wikitext(_WT)
check(
    "★ 源页解析器：A/B 两批 + Tenet 一次全解（不再依赖渲染页缓存）",
    sorted(_p["coda"]) == [1, 2] and len(_p["tenet"]) == 1,
    f"{_p}",
)
check(
    "★ 源页解析器：Batch A 两行、值正确（去 {{D|}} / {{Valence…}} 模板壳）",
    _p["coda"].get(1)
    == [
        {"en": "Coda Hema", "element": "Magnetic", "bonus": 34.2},
        {"en": "Coda Mire", "element": "Impact", "bonus": 28.3},
    ],
    str(_p["coda"].get(1)),
)
check(
    "★ 源页解析器：Batch B 同页解析（旧渲染路径正是漏这一批）",
    _p["coda"].get(2) == [{"en": "Coda Bassocyst", "element": "Magnetic", "bonus": 31.6}],
    str(_p["coda"].get(2)),
)
check(
    "★ 源页解析器：Tenet 名字带 {{Weapon|}} 壳也能剥出（且只收 Tenet 行）",
    _p["tenet"] == [{"en": "Tenet Ferrox", "element": "Radiation", "bonus": 25.7}],
    str(_p["tenet"]),
)
check(
    "★ 源页解析器：非效价表（无表头/无数据行）不误收",
    _fv.parse_wikitext("&lt;table&gt;&lt;tr&gt;&lt;td&gt;x&lt;/td&gt;&lt;/tr&gt;&lt;/table&gt;")
    == {"coda": {}, "tenet": []},
    "空表被误收",
)


# ---------------------------------------------------------------------------
# ★ 2026-10-15 新增：固定时钟 + 临时 fixture 的换批边界回归
#   不读真实 core/data 的时间字段（epoch/锚点/快照可能随刷新变），预期值按日历手算。
#   fixture 写进临时运行目录（core_paths.set_run_dir），read_path 运行目录优先。
# ---------------------------------------------------------------------------
def _fx_items(prefix: str, cn: str, n: int) -> list[dict]:
    return [
        {"en": f"{prefix} {i}", "cn": f"{cn}{i}", "element": "Heat", "bonus": 30.0}
        for i in range(n)
    ]


_FX_CODA = {
    "mode": "batch_cycle",
    "epoch": "2026-09-13T00:00:00+00:00",
    "period_hours": 96,
    "anchor_idx": 0,
    "batch_label": ["A", "B"],
    "batches": [_fx_items("Coda A", "终幕·甲", 7), _fx_items("Coda B", "终幕·乙", 7)],
    "valence_snapshot": "2026-09-21T06:00:00+00:00",
}
_FX_TENET = {
    "mode": "refresh_only",
    "epoch": "2026-09-12T00:00:00+00:00",
    "period_hours": 96,
    "items": _fx_items("Tenet X", "信条·丙", 5),
    "valence_snapshot": "2026-09-20T06:00:00+00:00",
}
_FX_INC = {
    "epoch": "2026-06-22T00:00:00+00:00",
    "anchor_week": 1,
    "period_hours": 168,
    "weeks": [
        [{"en": "W1", "cn": "周一号"}],
        [{"en": "W2", "cn": "周二号"}],
        [{"en": "W3", "cn": "周三号"}],
    ],
}


def _card_at(key: str, fixture: dict, when: str) -> list[str]:
    d = Path(tempfile.mkdtemp())
    (d / "rotations.json").write_text(json.dumps({key: fixture}, ensure_ascii=False), "utf-8")
    prev = core_paths._RUN
    core_paths.set_run_dir(d)
    _CLOCK[0] = datetime.fromisoformat(when)
    try:
        return OBJ._rotation_lines(key, "T").lines
    finally:
        core_paths.set_run_dir(prev)
        _CLOCK[0] = _SEED_AT
        shutil.rmtree(d, ignore_errors=True)


# 终幕：(时刻, 当前批, 下一批, 换批倒计时行) —— epoch 09-13 起 A 批，每 4 天交替
for _when, _cur, _nxt, _cd in (
    ("2026-09-16T23:59:59+00:00", "A", "B", "◆ 距换批：剩余 不到1分钟（09-17 00:00 UTC）"),
    ("2026-09-17T00:00:00+00:00", "B", "A", "◆ 距换批：剩余 4天0小时（09-21 00:00 UTC）"),
    ("2026-09-17T00:00:01+00:00", "B", "A", "◆ 距换批：剩余 3天23小时（09-21 00:00 UTC）"),
    # epoch 之前：上一轮（09-09～09-13）是 B 批
    ("2026-09-12T12:00:00+00:00", "B", "A", "◆ 距换批：剩余 12小时0分（09-13 00:00 UTC）"),
    # 跨周期：09-13 → 2027-01-01 12:00 = 110.5 天 = 27 个整周期（奇数 ⇒ B），窗口 12-30 起
    ("2027-01-01T12:00:00+00:00", "B", "A", "◆ 距换批：剩余 1天12小时（01-03 00:00 UTC）"),
    ("2027-01-03T00:00:00+00:00", "A", "B", "◆ 距换批：剩余 4天0小时（01-07 00:00 UTC）"),
):
    _ls = _card_at("coda", _FX_CODA, _when)
    _cn = "终幕·甲" if _cur == "A" else "终幕·乙"
    check(
        f"[固定时钟] 终幕 {_when[:19]} ⇒ {_cur} 批、下一批 {_nxt}",
        any(ln.startswith("◆ 当前批次：") and f"{_cur} 批" in ln for ln in _ls)
        and any(ln.startswith(f"◆ 下一批 {_nxt}") for ln in _ls)
        and sum(1 for ln in _ls if ln.startswith(f"· {_cn}")) == 7,
        str(_ls[:3]),
    )
    check(f"[固定时钟] 终幕 {_when[:19]} 倒计时", _cd in _ls, str(_ls[:3]))

# 信条：元素加成每 4 天（epoch 09-12）重生成
for _when, _cd in (
    ("2026-09-15T23:00:00+00:00", "剩余 1小时0分（09-16 00:00 UTC，每 4 天一次）"),
    ("2026-09-16T00:00:00+00:00", "剩余 4天0小时（09-20 00:00 UTC，每 4 天一次）"),
):
    _ls = _card_at("tenet", _FX_TENET, _when)
    check(
        f"[固定时钟] 信条 {_when[:19]} 重生成倒计时",
        any(ln.startswith("◆ 距元素加成重生成：") and _cd in ln for ln in _ls),
        str(_ls[:1]),
    )

# 灵化：3 周循环，周一 00:00 UTC 换轮（epoch 06-22 为第 1 周）
for _when, _wk in (
    ("2026-06-28T23:59:59+00:00", "第 1/3 周"),
    ("2026-06-29T00:00:00+00:00", "第 2/3 周"),
    ("2026-07-13T00:00:00+00:00", "第 1/3 周"),  # 三周后回绕
):
    _ls = _card_at("incarnon", _FX_INC, _when)
    check(f"[固定时钟] 灵化 {_when[:19]} ⇒ {_wk}", _ls[0].startswith(f"◆ 本周：{_wk}"), _ls[0])

# 快照说明行：本轮 / 上一轮（过期）/ 缺失三态（快照 09-21 06:00，窗口 09-21～09-25）
for _key, _fx, _when, _want in (
    ("coda", _FX_CODA, "2026-09-24T23:59:59+00:00", "（本轮快照）"),
    ("coda", _FX_CODA, "2026-09-25T00:00:00+00:00", "（上一轮快照，数值可能已变）"),
    ("tenet", _FX_TENET, "2026-09-21T00:00:00+00:00", "（本轮快照）"),
    ("tenet", _FX_TENET, "2026-09-24T00:00:00+00:00", "（上一轮快照，数值可能已变）"),
):
    _ls = _card_at(_key, _fx, _when)
    check(
        f"[固定时钟] {_key} {_when[:19]} 快照标注 {_want}",
        any(ln.startswith("※ 元素与加成为") and _want in ln for ln in _ls),
        str([ln for ln in _ls if "快照" in ln]),
    )
_ls = _card_at(
    "coda",
    {k: v for k, v in _FX_CODA.items() if k != "valence_snapshot"},
    "2026-09-22T00:00:00+00:00",
)
check("[固定时钟] 缺快照 ⇒ 不印快照说明行", not any("快照" in ln for ln in _ls), str(_ls))

_dt.datetime = _REAL_DT


# ★ 2026-10-23：fetch_valence.apply 改数值必须同步写「数值最早出现时刻」（不变量，源于信条误报）
def _rot_fixture():
    return {
        "tenet": {"items": [{"en": "Tenet Arca Plasmor", "element": "Heat", "bonus": 30.0}]},
        "coda": {
            "batch_label": ["A", "B"],
            "batches": [
                [{"en": "Coda Bubonico", "element": "Cold", "bonus": 20.0}],
                [{"en": "Coda Mire", "element": "Toxin", "bonus": 25.0}],
            ],
            "batch_values_since": ["2026-10-01T00:00:00+00:00", "2026-10-01T00:00:00+00:00"],
        },
    }


_pv = {
    "tenet": [{"en": "Tenet Arca Plasmor", "element": "Electricity", "bonus": 40.0}],
    "coda": {1: [{"en": "Coda Bubonico", "element": "Heat", "bonus": 33.0}]},
}
_d = _rot_fixture()
_fv.apply(_d, _pv, revs={"tenet": "2026-10-22T00:40:26+00:00", "coda": "2026-10-23T00:56:34+00:00"})
check(
    "★ apply：数值有变化 ⇒ 同步写 valence_values_since / 该批 batch_values_since（= 源页修订时刻）",
    _d["tenet"].get("valence_values_since") == "2026-10-22T00:40:26+00:00"
    and _d["coda"]["batch_values_since"]
    == ["2026-10-23T00:56:34+00:00", "2026-10-01T00:00:00+00:00"],
    str((_d["tenet"].get("valence_values_since"), _d["coda"]["batch_values_since"])),
)
_d2 = _rot_fixture()
try:
    _fv.apply(_d2, _pv, revs={})
    check("★ apply：数值变了却没有修订时刻 ⇒ 拒绝写入", False, "未抛 SystemExit")
except SystemExit:
    # main() 在 apply 之后才落盘 ⇒ 抛 SystemExit 时文件不会被写（内存里的 dict 不重要）
    check("★ apply：数值变了却没有修订时刻 ⇒ 抛 SystemExit（main 不落盘）", True)
_d3 = _rot_fixture()
_same = {"tenet": [{"en": "Tenet Arca Plasmor", "element": "Heat", "bonus": 30.0}], "coda": {}}
_fv.apply(_d3, _same, revs={})
check(
    "apply：数值没变化 ⇒ 不需要修订时刻、不改 since",
    "valence_values_since" not in _d3["tenet"],
)

print()
if FAILED:
    print(f"✗ {len(FAILED)} 项失败：" + "、".join(FAILED))
    sys.exit(1)
print("✓ 全部通过")
