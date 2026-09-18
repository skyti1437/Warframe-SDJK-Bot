# -*- coding: utf-8 -*-
"""裂隙筛选匹配 + 「不翻页单卡」守卫：python3 tests/test_fissure_filter.py

两件事，都是 2026-09-14 用户报障：

**①「蹲 裂隙」设置了永远不触发。** 根因是字段口径不一致：
`de_worldstate._parse_fissures()` 写进字典的 `missionType` 是 **DE 官方简中**
（「捕获」「救援」「元素转换」…），而 `parse_fissure_filter()` 产出的是
**英文标准键**（capture / rescue / alchemy），`FissureFilter._match_one()`
却拿中文名去比英文键 —— 且**静默失败**（不报错、不写日志），于是
「蹲 裂隙 捕获」「蹲 裂隙 虚空歼灭」这类订阅一条都命中不了。
修法：`_CN_TO_MISSION` 补齐 DE 官方简中，`_match_one` 用
`FissureFilter._mission_keys()` 双向解析。

本文件第 1 组把「官方简中 → 英文键」的覆盖率冻成哨兵 —— DE 一旦加新任务类型，
这里立刻失败，逼着补表。

**② 裂隙卡不再翻页。** 用户要求「别做翻页了，直接那几页内容都放一张截图上」，
`fmt_fissures(all_rows=True)` 一次输出全部；标题里不能再有「第 x/y 页」
（渲染层靠这个正则抽右上角页码芯片）。
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.formatters import _ALL_ROWS_CAP, fmt_fissures  # noqa: E402
from core.parser import (                                # noqa: E402
    _CN_TO_MISSION, parse_fissure_filter,
)

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = ""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}"
          + (f"  -> {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _fix(node: str, mt: str, tier: str = "Lith", *, hard: bool = False,
         storm: bool = False, mins: int = 40) -> dict:
    """生产口径的裂隙字典：missionType 是**中文**（DE 官方简中）。"""
    return {"id": f"{node}-{mt}", "node": node, "missionType": mt,
            "tier": tier, "tierNum": 1, "isHard": hard, "isStorm": storm,
            "expiry": _iso(datetime.now(timezone.utc) + timedelta(minutes=mins))}


# ---------------------------------------------------------------- ① 官方简中覆盖
_de = ROOT / "core" / "data" / "de"
_zh_tbl = json.loads((_de / "mission_types_zh.json").read_text(encoding="utf-8"))
_missing = sorted({cn for cn in _zh_tbl.values()
                   if isinstance(cn, str) and cn and cn not in _CN_TO_MISSION})
# ⚠️ 回归守卫（2026-09-17 线上事故）：core/formatters.py 里曾有**两个同名模块级
# 变量 `_TIER_ORDER`** —— 一个是本文件用的「英文纪元 → 序号」字典，另一个是
# 遗物按纪元分组用的中文元组；后者定义在后面，把前者**覆盖成元组**，于是
# fmt_fissures 在 `_TIER_ORDER.get(...)` 处崩 `'tuple' object has no attribute 'get'`，
# 线上「裂隙」指令直接报错。这里把两者类型焊死，并强制它们不同名。
from core import formatters as _F  # noqa: E402

check("_TIER_ORDER 仍是「英文纪元→序号」字典（未被同名变量覆盖）",
      isinstance(_F._TIER_ORDER, dict) and _F._TIER_ORDER.get("Lith") == 0,
      f"type={type(_F._TIER_ORDER).__name__} value={_F._TIER_ORDER!r}")
check("遗物分组顺序另立名字（元组），不再占用 _TIER_ORDER",
      isinstance(getattr(_F, "_RELIC_TIER_ORDER", None), tuple),
      f"={getattr(_F, '_RELIC_TIER_ORDER', None)!r}")
# 端到端冒烟：真实构造一条裂隙行，确认 fmt_fissures 能跑完（不只是导入不报错）
def _one_row() -> dict:
    return {"id": "smoke", "node": "Ananke（木星）", "missionType": "捕获",
            "tier": "Meso", "tierNum": 1, "isHard": False, "isStorm": False,
            "expiry": (datetime.now(timezone.utc)
                       + timedelta(minutes=40)).isoformat()}


try:
    _t_smoke, _l_smoke = fmt_fissures([_one_row()])
    _smoke_ok = "Ananke" in " ".join(_l_smoke)
except Exception as _exc:  # noqa: BLE001
    _t_smoke, _l_smoke, _smoke_ok = "", [], False
    check(f"fmt_fissures 端到端可跑（异常：{type(_exc).__name__}: {_exc}）", False)
else:
    check("fmt_fissures 端到端可跑且渲染出节点名", _smoke_ok, str(_l_smoke[:2]))


check("DE 官方简中任务名全部能反查回英文键", not _missing, str(_missing))


# 生产实测出现过的任务类型（抓自线上 33 条活跃裂隙）
_LIVE = ["捕获", "歼灭", "生存", "防御", "拦截", "破坏", "挖掘", "间谍",
         "救援", "移动防御", "中断", "虚空洪流", "虚空覆涌", "元素转换"]
_unknown = [cn for cn in _LIVE if cn not in _CN_TO_MISSION]
check("线上出现过的任务类型全部可反查", not _unknown, str(_unknown))

# ---------------------------------------------------------------- ② 匹配（核心）
_CAP = _fix("Ananke（木星）", "捕获", "Meso")
_EXT = _fix("Xini（阋神星）", "歼灭")
_SUR = _fix("Tiwaz（谷神星）", "生存")
_RES = _fix("Odin（水星）", "救援", "Neo")
_ALC = _fix("Yuvarium（扎里曼）", "元素转换", "Axi")
_VCA = _fix("Circulus（月球）", "虚空覆涌", "Omnia")
_HARD_CAP = _fix("V（火星）", "捕获", hard=True)
_STORM = _fix("虚天神殿（地球比邻星域）", "", "Neo", storm=True)

check("用户报障规则「捕获」能命中捕获裂隙",
      parse_fissure_filter("捕获").match(_CAP))
_VOID_CAP = _fix("Ukko（虚空）", "捕获", "Meso")
check("「虚空捕获」只收虚空星系（2026-09-14 二次报障后加地区限定）",
      parse_fissure_filter("虚空捕获").match(_VOID_CAP)
      and not parse_fissure_filter("虚空捕获").match(_CAP))
check("「虚空歼灭」只收虚空星系歼灭",
      parse_fissure_filter("虚空歼灭").match(_fix("Belenus（虚空）", "歼灭"))
      and not parse_fissure_filter("虚空歼灭").match(_EXT))
check("「虚空,捕获」两组任一命中（虚空或捕获均可）",
      parse_fissure_filter("虚空,捕获").match(_CAP)
      and parse_fissure_filter("虚空,捕获").match(_VOID_CAP))
check("「救援」能命中救援裂隙（DE 官方名，旧表写作营救）",
      parse_fissure_filter("救援").match(_RES))
check("旧叫法「营救」仍能命中救援裂隙",
      parse_fissure_filter("营救").match(_RES))
check("「元素转换」能命中（DE 官方名，旧表写作炼金）",
      parse_fissure_filter("元素转换").match(_ALC))
check("「虚空覆涌」能命中（DE 官方名，旧表写作虚空级联）",
      parse_fissure_filter("虚空覆涌").match(_VCA))
check("旧叫法「虚空级联」仍能命中虚空覆涌裂隙",
      parse_fissure_filter("虚空级联").match(_VCA))
check("带纪元的规则「古纪捕获」纪元也对得上",
      parse_fissure_filter("古纪捕获").match(_fix("Ananke（木星）", "捕获",
                                                  "Lith"))
      and not parse_fissure_filter("古纪捕获").match(_CAP))
check("「钢铁虚空生存」命中钢铁+虚空的生存（虚空是地区限定）",
      parse_fissure_filter("钢铁虚空生存").match(_fix("S（虚空）", "生存", hard=True))
      and not parse_fissure_filter("钢铁虚空生存").match(_SUR))
check("「虚空覆涌」是任务名，不误触发虚空地区限定",
      parse_fissure_filter("虚空覆涌").match(_VCA)
      and parse_fissure_filter("虚空洪流").match(_fix("D（火卫二）", "虚空洪流")))
check("「普通捕获」不命中钢铁捕获",
      not parse_fissure_filter("普通捕获").match(_HARD_CAP))
check("「九重天」只命中九重天裂隙",
      parse_fissure_filter("九重天").match(_STORM)
      and not parse_fissure_filter("九重天").match(_CAP))
# 反向
check("「捕获」不命中生存裂隙", not parse_fissure_filter("捕获").match(_SUR))
check("空规则命中全部", parse_fissure_filter("").match(_SUR))
# 英文口径（历史 fixture / 兜底路径）仍要能用
check("英文 missionType 仍可匹配",
      parse_fissure_filter("捕获").match(
          {"node": "Teshub", "missionType": "Capture", "tier": "Lith",
           "isHard": False, "isStorm": False}))
check("带 missionKey 的字典也认",
      parse_fissure_filter("捕获").match(
          {"node": "Teshub", "missionKey": "capture", "missionType": "捕获",
           "tier": "Lith", "isHard": False, "isStorm": False}))


# ---------------------------------------------------------------- ③ 不翻页单卡
def _many(n: int) -> list[dict]:
    tiers = ["Lith", "Meso", "Neo", "Axi", "Requiem", "Omnia"]
    mts = ["捕获", "歼灭", "生存", "防御", "拦截", "破坏"]
    return [_fix(f"节点{i}", mts[i % len(mts)], tiers[i % len(tiers)],
                 mins=10 + i) for i in range(n)]


_rows = _many(34)
_title, _lines = fmt_fissures(_rows, all_rows=True)
check("单卡模式标题不带页码", "第" not in _title and "/" not in _title, _title)
check("单卡模式标题给出总条数", "共34条" in _title, _title)
check("单卡模式 34 条全在一张卡（+1 条排序说明）",
      sum(1 for x in _lines if x.startswith(("1.", "34."))) == 2
      and len([x for x in _lines if x and x[0].isdigit()]) == 34,
      str(len(_lines)))
check("单卡模式不写「-2 翻页」类提示",
      all("翻页" not in x for x in _lines))

_t2, _l2 = fmt_fissures(_rows, all_rows=True,
                        flt=parse_fissure_filter("捕获").match, page=2)
check("单卡模式下 page 参数被忽略（筛选仍在）",
      all("捕获" in x for x in _l2 if x and x[0].isdigit()) and
      len([x for x in _l2 if x and x[0].isdigit()]) > 0, str(_l2[:3]))

# 超过行数上限时只截断且显式说明（渲染层 MAX_BODY_LINES=90 会静默截断）
_t3, _l3 = fmt_fissures(_many(_ALL_ROWS_CAP + 5), all_rows=True)
check("超上限时显式说明，不静默丢内容",
      any("本卡只列前" in x for x in _l3), str(_l3[-3:]))
check("超上限时行数受控（正文 ≤ 上限+2 条注脚）",
      len([x for x in _l3 if x and x[0].isdigit()]) == _ALL_ROWS_CAP)

# 分页模式（默认）不受影响：其他列表类指令仍走翻页
_t4, _l4 = fmt_fissures(_many(34), page=2, page_size=12)
check("默认分页模式仍是第2/3页", "第2/3页" in _t4, _t4)
check("默认分页模式只出 12 条",
      len([x for x in _l4 if x and x[0].isdigit()]) == 12)

print()
if FAILED:
    print(f"共 {len(FAILED)} 项失败：{FAILED}")
    sys.exit(1)
print("全部通过 ✔")
