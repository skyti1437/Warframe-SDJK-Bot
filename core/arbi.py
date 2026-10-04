# -*- coding: utf-8 -*-
"""仲裁（Arbitration）排期推算 —— 查询指令与「蹲 仲裁」推送共用一份实现。

数据源（`arbi.wf.wiki`，社区维护、非官方）：
  · ``arbys.schedule.v2.json``  逐小时**确定性**排期（``seq`` 索引表 + ``nodes`` 键表）
  · ``arbys.nodes.zh.json``     节点中文名 / 星球 / 任务类型 / 派系
  · ``tierlist.default.json``   站点评级（S / A+ / A / A- / B / C）
  · ``core/data/arb_ratings.json``  **本地静态种子**：社区实测中位数 → 档位
    （构建期由 ``scripts/build_arb_ratings.py`` 从该站**排行榜**聚合；口径见该脚本）

★ 评级的**三级回落**（2026-10-04 起）：arbi tierlist（有）→ 社区实测档（有）→ 未评级。
  arbi 优先 —— 官方 16 个评级（含 C 档）原样保留，实测只补空白（覆盖率 16 → 65/88）。
  ★ 两套**不是同一把尺子**（tierlist 偏「上限/潜力」，中位数是「典型水平」，
  2026-10-04 实测一致率仅 2/16）⇒ 卡片上标注来源，冲突不暴露。

★ 本模块存在的理由（2026-09-19 玩家反馈「蹲 仲裁 高效 永久」没生效）：
10o.io 停摆后 ``client.arbitration()`` **恒抛** ``WarframeAPIError``，
查询指令早已改用 arbi.wf.wiki 推算；但推送侧仍在调那个恒抛接口、并用
``except WarframeAPIError: pass`` 静默吞掉 —— 于是「蹲 仲裁」**永远不会推**。
现在两边共用本模块，避免再次漂移。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

BASE = "https://arbi.wf.wiki/data/"
MEASURED_FILE = "arb_ratings.json"   # core/data 下的实测种子（构建期生成，不联网）

# 评级别名 -> arbi 的 tier 取值（与仲裁查询指令同一套口径）
RATING = {"高效": ("S", "A+", "A"), "传奇": ("S",)}

_MEASURED: Optional[dict] = None


def load_measured() -> dict:
    """读「社区实测中位数」表（``core/data/arb_ratings.json``）。

    ★ 静态种子：缺失/损坏一律返回 ``{}``（评级退化为「未评级」，不影响排期）。
    """
    global _MEASURED
    if _MEASURED is None:
        try:
            p = Path(__file__).resolve().parent / "data" / MEASURED_FILE
            _MEASURED = json.loads(p.read_text(encoding="utf-8")).get("nodes") or {}
        except Exception:  # noqa: BLE001 - 种子缺失不致命
            _MEASURED = {}
    return _MEASURED


def measured_of(key: str) -> dict:
    """该节点的实测记录（``median`` / ``tier`` / ``n``；没有返回 ``{}``）。"""
    return load_measured().get(key) or {}


def merge_measured_tiers(tier_of: dict, measured: Optional[dict] = None) -> dict:
    """三级回落：arbi tierlist（有）→ 社区实测档（有）→ 原样（未评级）。

    ★ **arbi 优先**：官方有评级就原样保留（含 C 档），实测只补空白。
    ★ **不删键**：返回值保留传入的全部键（未评级节点的「未评级」语义由调用方处理）
      —— ``node_cells`` 的列对齐依赖「不丢空串」的不变量，别在这里删键。
    """
    m = load_measured() if measured is None else measured
    out = dict(tier_of)
    for k, v in m.items():
        if out.get(k, "") in ("", "未评级") and v.get("tier"):
            out[k] = v["tier"]
    return out


_ARBI_ONLY: dict = {}


def tier_source(key: str, tier_of: dict) -> str:
    """档位来源：``"arbi"``（官方 tierlist 覆盖）/ ``"measured"``（本地实测补的）/ ``""``。

    ⚠️ 依赖 ``fetch_tables()`` 已跑过（它会把**回落前**的 arbi 表记进模块态）——
    卡片在 ``_arb_fetch()`` 之后立刻调用即可；单独调用只保证"非 arbi 即实测"。
    """
    tv = (tier_of.get(key) or "").strip()
    if tv in ("", "未评级"):
        return ""
    return "arbi" if _ARBI_ONLY.get(key, "") not in ("", "未评级") else "measured"

# 可筛选的任务类型（中文，来源 arbi 的 missionNameZh；INFESTED 前缀会被归一化）
TYPES = (
    "生存", "防御", "镜像防御", "拦截", "挖掘", "叛逃", "资源回收", "回收",
    "中断", "歼灭", "捕获", "虚空洪流", "虚空覆涌", "虚空决战", "联结生存",
    "元素转换", "劫持", "追击", "破坏", "刺杀", "移动防御", "救援", "破坏任务",
)
TYPES_STR = ("生存 / 防御 / 镜像防御 / 拦截 / 挖掘 / 叛逃 / 回收 / 中断 / "
             "歼灭 / 捕获 / 虚空洪流 / 虚空覆涌 / 虚空决战 / 联结生存 / 元素转换")


def mission_of(node: dict) -> str:
    """归一化 arbi 的任务类型中文名。

    该站把 Infested Salvage 写作「INFESTED 资源回收」，筛选时统一成「资源回收」。
    """
    mt = (node.get("missionNameZh") or "").strip()
    if mt.upper().startswith("INFESTED "):
        mt = mt[9:].strip()
    return mt or "?"


# arbi 的 factionNameZh 只有中系派系写了中文（奥罗金 / 低语者），其余是英文
# （Infestation / Grineer / Corpus）。DE 官方简中**不翻译**派系名，所以这里只把
# 它的英文用词对齐到官方写法。
_FACTION_FIX = {"Infestation": "Infested"}


def faction_of(node: dict) -> str:
    """仲裁节点派系名（显示用，与国际服官方写法一致）。"""
    raw = (node.get("factionNameZh") or "").strip()
    return _FACTION_FIX.get(raw, raw) if raw else ""


def node_line(nodes: dict, key: str, tier_of: dict) -> str:
    """把节点渲染成一行：节点（星球） · 类型 · 派系 · 评级。

    ★ 刻意**不显示敌人等级区间**（``minEnemyLevel``）：很多用户看到「Lv 6-11」
    会以为和「高效 / 传奇」筛选是一回事，反而起干扰（见 main 里的同款说明）。
    """
    n = nodes.get(key) or {}
    name = n.get("nameZh") or "?"
    system = n.get("systemNameZh") or ""
    mtype = mission_of(n)
    fac = faction_of(n)
    tv = tier_of.get(key, "")
    if tv == "未评级":
        tv = ""
    parts = [f"{name}（{system}）" if system else name, mtype, fac]
    head = " · ".join(p for p in parts if p)
    return f"{head}　[{tv}]" if tv else head


def node_cells(nodes: dict, key: str, tier_of: dict) -> list[str]:
    """节点 → **分列单元格**：[节点（星球）, 类型, 派系, [评级]]（评级可缺省）。

    ★ 2026-09-27：仲裁排期卡改走渲染层的列对齐（按全角空格切列），需要「一格一
    字段」；`node_line()` 保留给「当前仲裁」卡的单行内联写法，两者同源。
    ⚠️ **不丢空串**：字段缺失时保留空单元格，否则它后面的列会整体串位。
    """
    n = nodes.get(key) or {}
    name = n.get("nameZh") or "?"
    system = n.get("systemNameZh") or ""
    tv = tier_of.get(key, "")
    cells = [f"{name}（{system}）" if system else name,
             mission_of(n), faction_of(n)]
    if tv and tv != "未评级":
        cells.append(f"[{tv}]")
    return cells


async def fetch_tables(client) -> tuple[dict, dict, dict]:
    """拉三张表：排期序列 / 节点中文表 / 站点评级。返回 (sched, nodes, tier_of)。

    ★ ``tier_of`` 已做**三级回落**（arbi tierlist → 本地社区实测档 → 未评级）：
    查询卡、排期卡与「蹲 仲裁」筛选共用这一份，避免各算各的。
    """
    sched = await client._fetch_json(BASE + "arbys.schedule.v2.json", ttl=3600)
    nodes = (await client._fetch_json(BASE + "arbys.nodes.zh.json",
                                      ttl=86400)).get("nodes") or {}
    try:
        tier = await client._fetch_json(BASE + "tierlist.default.json", ttl=86400)
    except Exception:  # noqa: BLE001 - 评级表缺了不影响排期
        tier = {}
    tier_of = {nk: tv for tv, lst in (tier.get("tierBuckets") or {}).items()
               for nk in lst}
    _ARBI_ONLY.clear()
    _ARBI_ONLY.update(tier_of)          # 供 tier_source() 区分「官方 / 实测」
    return sched, nodes, merge_measured_tiers(tier_of)


def key_at(sched: dict, ts: Optional[float] = None) -> tuple[str, int, float, float]:
    """给定时刻的仲裁节点 key。

    Returns:
        ``(key, idx, start_ts, step)`` —— ``idx`` 用于算「下一小时」。
    """
    seq = sched["seq"]
    start = sched["startTs"]
    step = sched.get("stepSec", 3600)
    idx = int(((time.time() if ts is None else ts) - start) // step) % len(seq)
    return sched["nodes"][seq[idx]], idx, start, step


def slot(sched: dict, nodes: dict, tier_of: dict, ts: Optional[float] = None) -> dict:
    """当前场次信息（推送与「仲裁」指令共用口径）。"""
    key, idx, start, step = key_at(sched, ts)
    n = nodes.get(key) or {}
    seq = sched["seq"]
    nxt_key = sched["nodes"][seq[(idx + 1) % len(seq)]]
    tier = tier_of.get(key, "") or ""
    if tier == "未评级":
        tier = ""
    return {
        "key": key,
        "node": n.get("nameZh") or "?",
        "system": n.get("systemNameZh") or "",
        "mission": mission_of(n),
        "faction": faction_of(n),
        "tier": tier,
        "line": node_line(nodes, key, tier_of),
        "next_line": node_line(nodes, nxt_key, tier_of),
        "start": start + idx * step,
        "end": start + (idx + 1) * step,
    }


async def current(client, ts: Optional[float] = None) -> dict:
    """便捷入口：拉表 + 算当前场次（推送侧每次轮询调用）。"""
    sched, nodes, tier_of = await fetch_tables(client)
    return slot(sched, nodes, tier_of, ts)


# ---------------------------------------------------------------------------
# 订阅规则（蹲 仲裁 <筛选>）
# ---------------------------------------------------------------------------
def parse_rule(rule: str) -> tuple[set[str], tuple[str, ...], list[str]]:
    """拆分订阅规则 → (类型词, 评级集合, 不认识的词)。

    ``"高效,生存"`` → ``({"生存"}, ("S","A+","A"), [])``
    """
    types: set[str] = set()
    ratings: tuple[str, ...] = ()
    unknown: list[str] = []
    for raw in (rule or "").replace("，", ",").split(","):
        w = raw.strip()
        if not w:
            continue
        if w in RATING:
            ratings = RATING[w]
        elif w in TYPES:
            types.add(w)
        else:
            unknown.append(w)
    return types, ratings, unknown


def match_rule(rule: str, sl: dict) -> bool:
    """当前场次是否命中订阅筛选（**空规则 = 全推**）。

    语义与仲裁查询指令一致：写了类型就必须类型匹配，写了评级就必须评级匹配，
    两个都写则同时满足（AND）。不认识的词**不阻塞**（否则用户拼错就永远收不到，
    比漏筛更糟）—— 订阅时另有提示（见 main._h_dun）。
    """
    types, ratings, _unknown = parse_rule(rule)
    if not types and not ratings:
        return True
    if types and sl.get("mission") not in types:
        return False
    if ratings and sl.get("tier") not in ratings:
        return False
    return True


def rule_supported(event: str) -> bool:
    """该蹲类型是否支持筛选规则（裂隙走 FissureFilter，仲裁走本模块）。"""
    return event in ("裂隙", "仲裁")
