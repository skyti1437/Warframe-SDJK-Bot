# -*- coding: utf-8 -*-
"""后台推送引擎（Daemon 协程）。

工作方式：
1. 定期（默认 45s）按平台拉取 WorldState 快照（经 TTL 缓存，不额外加压）；
2. 与上次快照做差量比对（新裂隙出现 / 夜灵入夜 / 奸商抵离 / 仲裁换节点…）；
3. 命中群内订阅规则（含裂隙筛选、免打扰时间窗、时长/次数）则主动广播。

支持的事件类型与数据源映射见 PUSH_EVENTS；未接线的类型在订阅时即被拒绝。
"""

from __future__ import annotations

import asyncio
import json
import math
import copy
import re
import time
import threading
from weakref import WeakValueDictionary
from pathlib import Path
from typing import Awaitable, Callable, Optional

from . import i18n, paths
from .api_client import WarframeClient
from .de_worldstate import DUVIRI_STATES
from .logging_compat import mask_in_text, mask_umo
from .messaging import LiteralProgress, literal_progress
from .formatters import (
    ORACLE_REGIONS,
    _clean_challenge_desc,
    _left_en,
    _shop_name,
    _synd_title,
    calendar_season_name,
    countdown,
    _RAILJACK_TYPE_CN,
    de_zh,
    mission_cn,
    palladino_shop,
    parse_iso,
    rotation_window,
    steel_rotation_index,
    steel_shop,
    tier_cn,
    utc_today,
    weekly_reset_info,
)
from .parser import FissureFilter, contains_fissure_tier, parse_fissure_filter
from . import arbi, display_time
from .store import Subscription, SubscriptionStore, finish_write, write_json_atomic

SendFunc = Callable[[str, str, Optional[list]], Awaitable[None]]

# 基线（重启补推）：落盘快照在此秒数内视为有效
BASELINE_MAX_AGE = 30 * 60
# 发送失败：待发队列最多重投轮数 / 事件过期秒数；会话连续失败多少轮 ⇒ 暂停推送
PENDING_MAX_TRIES = 3
PENDING_TTL = 15 * 60
FAIL_PURGE_THRESHOLD = 5
# ★ 2026-10-17 用户口径：连续失败达阈值不再直接清理订阅，改为「暂停」该会话推送 ——
#   暂停期间不发送；每 PAUSE_RETRY_SECS 到点试发一次，成功即恢复；
#   自暂停起满 PAUSE_PURGE_SECS 仍失败才清理订阅。
PAUSE_RETRY_SECS = 6 * 3600
PAUSE_PURGE_SECS = 7 * 86400


class _InvalidPause(dict):
    """只在读取副本中标记坏暂停；不写回存档，也不当成未暂停而试发。"""


class _SendBusy(Exception):
    """同一会话的上一笔发送尚未退出，本笔还没有调用适配器。"""


class _CheckpointFailed(Exception):
    """等待推送状态恢复可写后，再发送剩余正文。"""


def _pause_number(value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError("暂停数值类型不合法")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError("暂停数值应为非负有限数")
    return number


def _bounty_round(cycle) -> tuple[str, float]:
    """保留旧事件键的原始写法，把 ISO、Unix 秒及 oracle 毫秒换算为统一的截止时间。"""
    expiry = cycle.get("expiry") if isinstance(cycle, dict) else None
    key = str(expiry).strip()
    try:
        end = float(key)
    except ValueError:
        dt = parse_iso(key)
        if dt is None:
            raise ValueError("赏金轮换时间缺失或无效") from None
        end = dt.timestamp()
    else:
        if end >= 1e11:
            end /= 1000
    if not math.isfinite(end) or end <= 0:
        raise ValueError("赏金轮换时间缺失或无效")
    return key, end


def _bounty_parts_for(parts: list[dict], sids: set[str]) -> list[dict]:
    return [
        {"text": part["text"], "sids": [sid for sid in part["sids"] if sid in sids]}
        for part in parts
        if sids.intersection(part["sids"])
    ]


def _bounty_parts_text(parts: list[dict], sids: set[str]) -> str:
    mine = [part["text"] for part in parts if sids.intersection(part["sids"])]
    if not mine:
        return ""
    body = "\n".join(f"· {text}" for text in mine)
    if len(mine) == 1:
        return i18n.L(f"⚡ 新赏金：{mine[0]}", f"⚡ New bounty: {mine[0]}")
    return i18n.L(
        f"⚡ 新赏金（{len(mine)} 条）：\n" + body,
        f"⚡ New bounties ({len(mine)}):\n" + body,
    )


def _bounty_retry_chunks(
    progress: LiteralProgress, parts: list[dict], sids: set[str], body: str
) -> LiteralProgress:
    """收件人减少后，把旧分片边界投影到仍有订阅的赏金行（含行中断点）。"""
    original = "".join(progress.chunks)
    if body == original:
        return progress
    ranges = []
    old_end = new_end = 0
    for part in parts:
        text = part["text"]
        old_start = original.index(text, old_end)
        old_end = old_start + len(text)
        if sids.intersection(part["sids"]):
            new_start = body.index(text, new_end)
            new_end = new_start + len(text)
            ranges.append((old_start, old_end, new_start))

    result = LiteralProgress()
    offset = copied = 0
    for i, chunk in enumerate(progress.chunks):
        offset += len(chunk)
        end = next(
            (new + max(0, offset - start) for start, stop, new in ranges if offset < stop),
            len(body),
        )
        if end > copied:
            result.chunks.append(body[copied:end])
            if i < progress.confirmed:
                result.confirmed += 1
            copied = end
    return result


_ARBI_TIER_RANGE_EN = "S≥800 / A+≥700 / A≥600 / A-≥450 / F＜450 (per hour)"

# 能识别为「目标会话已不存在」的发送异常 ⇒ 立即清理该会话订阅
_FATAL_SEND_RE = re.compile(
    r"群.{0,6}(不存在|已解散|解散)|被踢|踢出|不在群|已退群|退出了群|"
    r"not in group|kicked|chat not found|group (not found|does not exist)|no such group",
    re.I,
)


def _brief_reason(err: BaseException, umo: str = "", limit: int = 120) -> str:
    """发送异常的简短文本（落盘进暂停记录用）。

    暂停原因会显示在运维面板上，仅取异常文本并遮盖其中的会话号。
    """
    text = mask_in_text(err, umo).strip().replace("\n", " ").replace("\r", " ")
    return (text or err.__class__.__name__)[:limit]


# 每种事件类型在 PushDaemon._last[platform] 里占用的基线键（按「平台 × 事件类型」独立管理）
BASELINE_KEYS: dict[str, tuple[str, ...]] = {
    "裂隙": ("fissure_ids",),
    "夜灵": ("cetus_state",),
    "山谷": ("vallis_state",),
    "魔胎": ("cambion_state",),
    "地球": ("earth_state",),
    "双衍": ("duviri_state",),
    "活动": ("goal_ids",),
    "奸商": ("trader_active",),
    "突击": ("sortie_id",),
    "执刑官": ("archon_id",),
    "仲裁": ("arbi_slot",),
    "钢路侵袭": ("sp_incursions",),
    "钢精兑换": ("steel_rotation",),
    "碎银兑换": ("sliver_reset",),
    "赏金": ("bounty_exp",),
    "灵化武器": ("incarnon_window",),
    "信条": ("tenet_window",),
    "终幕": ("coda_window",),
    "阿耶兑换": ("pv_key",),
    "1999日历": ("cal_season", "cal_day"),
    "电波": ("nw_ids",),
    "警报": ("alert_ids",),
    "入侵": ("invasion_ids",),
    "新闻": ("news_ids",),
    "每日特惠": ("deal_key",),
}


def _fissure_type_cn(f: dict) -> str:
    """裂隙任务类型中文名：九重天风暴用与「九重天」卡同一张译表（前哨战 / 爆发 / 奥影母艇…），
    其余走 mission_cn。"""
    mt = f.get("missionType", "")
    if f.get("isStorm") and not i18n.is_en():
        cn = _RAILJACK_TYPE_CN.get((mt or "").lower(), "")
        if cn:
            return cn
    return mission_cn(mt)


# ---------------------------------------------------------------------------
# ★ C2（2026-10-03）：赏金订阅的筛选 —— 地区词 + 挑战名/任务类型词。
#   地区词 → oracle tag：与 formatters.ORACLE_REGIONS 同源（扎里曼/实验室/1999，
#   DE 三地区的 Jobs 不带节点/挑战，本轮订阅只覆盖 oracle 三地区）。
#   任务类型映射来源：challenge 路径族命名（ZarimanExterminateFastComplete →
#   歼灭、ZarimanSurvivalAbove50 → 生存 …，DE 官方导出/挑战表实查，自建并注释）。
# ---------------------------------------------------------------------------
_BOUNTY_REGION_WORDS: tuple[tuple[str, str, str], ...] = (
    ("扎里曼", "ZarimanSyndicate", "羽化之穹（扎里曼）"),
    ("羽化之穹", "ZarimanSyndicate", "羽化之穹（扎里曼）"),
    ("实验室", "EntratiLabSyndicate", "解剖圣所（实验室）"),
    ("圣所", "EntratiLabSyndicate", "解剖圣所（实验室）"),
    ("1999", "HexSyndicate", "霍瓦尼亚（1999）"),
    ("霍瓦尼亚", "HexSyndicate", "霍瓦尼亚（1999）"),
)

# challenge 路径子串 → 任务类型（玩家口径）。
# 来源：core/data/de/challenges_zh.json 的 Zariman*/EntratiLab*/Hex* 挑战族命名
# （如 ZarimanExterminateFastComplete=高效歼灭、ZarimanSurvivalAbove50、
#  EntratiLabAlchemy…=炼金），逐族人工核对（2026-10-03）。
_BOUNTY_TYPE_HINTS: tuple[tuple[str, str], ...] = (
    ("Exterminate", "歼灭"),
    ("Survival", "生存"),
    ("MobDef", "移动防御"),
    ("Assassinate", "刺杀"),
    ("Cascade", "级联"),
    ("Flood", "洪流"),
    ("Corruption", "腐化"),
    ("DefeatVoidAngel", "天使"),
    ("Alchemy", "炼金"),
    ("Defense", "防御"),
    ("Defend", "防御"),
    ("Excavation", "挖掘"),
    ("Capture", "捕获"),
    ("Sabotage", "破坏"),
    ("Rescue", "救援"),
    ("Spy", "间谍"),
    ("Disruption", "中断"),
    ("Void", "虚空"),
)


def bounty_type_of(challenge_path: str) -> str:
    """challenge 路径 → 任务类型（无命中返回空串）。"""
    p = challenge_path or ""
    for token, zh in _BOUNTY_TYPE_HINTS:
        if token in p:
            return zh
    return ""


_BOUNTY_TYPE_EN = {
    "MobDef": "Mobile Defense",
    "DefeatVoidAngel": "Void Angel",
    "Defend": "Defense",
}


def _bounty_type_label(challenge_path: str) -> str:
    """赏金任务类型的展示名（匹配规则仍用 bounty_type_of 的中文内部写法）。"""
    if not i18n.is_en():
        return bounty_type_of(challenge_path)
    p = challenge_path or ""
    for token, _zh in _BOUNTY_TYPE_HINTS:
        if token in p:
            return _BOUNTY_TYPE_EN.get(token, token)
    return ""


def parse_bounty_rule(rule: str) -> dict:
    """赏金订阅规则 → {"tags": {tag, ...}, "words": [词, ...]}。

    地区词（扎里曼/实验室/圣所/1999/霍瓦尼亚）→ oracle tag；其余词作为
    「挑战名/任务类型」词（如「高效歼灭」匹配挑战名、「歼灭」匹配类型）。
    """
    tags: set = set()
    words: list = []
    for tok in [t for t in (rule or "").replace(",", " ").split() if t]:
        hit = next((tag for w, tag, _t in _BOUNTY_REGION_WORDS if w in tok), None)
        if hit:
            tags.add(hit)
            continue
        words.append(tok)  # 非地区词：按「挑战名/任务类型」词处理
    return {"tags": tags, "words": words}


def bounty_rule_hit(rule: str, tag: str, challenge_name: str, challenge_path: str) -> bool:
    """规则是否命中一条赏金（地区 + 挑战名/类型词，全部条件须满足）。"""
    r = parse_bounty_rule(rule)
    if r["tags"] and tag not in r["tags"]:
        return False
    if not r["words"]:
        return True  # 只写地区（或空规则）：该地区全收
    t = bounty_type_of(challenge_path)
    for w in r["words"]:
        if w in (challenge_name or "") or (t and (w == t or w in t)):
            continue
        return False
    return True


def at_targets(platform_name: Optional[str], ids) -> list[str]:
    """推送 @ 目标过滤（A1，2026-10-03 用户批准的特例）。

    **aiocqhttp / qq_official（QQ）** 支持 At；其余平台/解析不到平台名返回空表
    ⇒ 调用方回落纯文本（不得因 @ 不支持而丢推送）。**绝不含 @全体**
    （`all` 一律剔除）；去重保序。
    """
    if not ids or (platform_name or "").lower() not in {"aiocqhttp", "qq_official"}:
        return []
    return list(dict.fromkeys(s for s in (str(x).strip() for x in ids) if s and s.lower() != "all"))


# 蹲类型 -> (说明, 是否已接线)
PUSH_EVENTS: dict[str, tuple[str, bool]] = {
    "裂隙": ("新虚空裂隙出现（支持 普通捕获,钢铁虚空生存 等筛选）", True),
    "夜灵": ("夜灵平野入夜（夜灵狩猎）", True),
    "山谷": ("奥布山谷温度切换（温暖 / 寒冷）", True),
    "魔胎": ("魔胎之境派系轮换（Fass / Vome）", True),
    "地球": ("地球昼夜交替（白天 / 夜晚）", True),
    "双衍": ("双衍王境螺旋（情绪）轮换", True),
    "奸商": ("虚空商人巴罗抵达/离开", True),
    "突击": ("每日突击刷新", True),
    "执刑官": ("每周执刑官猎杀刷新", True),
    "仲裁": ("仲裁换场次（可筛选：高效 / 传奇 / 生存 / 防御 …）", True),
    "钢路侵袭": ("钢铁之路每日侵袭刷新", True),
    # ★ C3（2026-10-03）：常规警报确已停用，但**活动型警报**（Tag=LotusGift
    #   等）会下发（实测 3 条：SolNode87/MT_ARTIFACT 等）—— 接线；同一 _id
    #   只推一次（alert_ids 基线去重）。
    "警报": ("活动型警报出现（常规警报已停用；如 Tenno 联合警报）", True),
    "入侵": ("新入侵出现", True),
    "新闻": ("官方新闻/热修发布", True),
    "每日特惠": ("达沃每日特惠刷新", True),
    "活动": ("限时活动开始 / 结束", True),
    "1999日历": ("1999 日历换季时提醒", True),
    "灵化武器": ("本周灵化轮换（钢铁回廊 9 周循环，周一 00:00 UTC）", True),
    "信条": ("Ergo 信条武器库存与元素加成（每 96 小时）", True),
    "终幕": ("Coda 终幕武器批次轮换（A/B 两批每 96 小时）", True),
    "钢精兑换": ("Teshin 钢精兑换周轮换（每周一 00:00 UTC，8 件循环）", True),
    "碎银兑换": ("Palladino 裂罅碎块商店每周限购重置（周一 00:00 UTC）", True),
    "赏金": ("赏金轮换（扎里曼/实验室/1999；可筛 地区+任务，如 扎里曼 高效歼灭）", True),
    "阿耶兑换": ("Prime 宝库轮换（Regal Aya 兑换包）", True),
    "电波": ("午夜电波每日/每周挑战刷新", True),
}

PUSH_ALIAS = {
    "裂缝": "裂隙",  # 2026-10-16 用户口径：裂缝 = 裂隙
    "钢铁裂隙": "裂隙",
    "虚空裂隙": "裂隙",
    "九重天裂隙": "裂隙",
    "钢铁裂缝": "裂隙",
    "虚空裂缝": "裂隙",
    "九重天裂缝": "裂隙",
    "执刑官猎杀": "执刑官",
    "日历": "1999日历",
    "夜": "夜灵",
    "平原时间": "夜灵",
    "夜灵平野": "夜灵",
    "奥布山谷": "山谷",
    "金星": "山谷",
    "山谷温度": "山谷",
    "魔胎之境": "魔胎",
    "火卫二": "魔胎",
    "地球昼夜": "地球",
    "白天黑夜": "地球",
    "双衍王境": "双衍",
    "螺旋": "双衍",
    "情绪": "双衍",
}


# 别名自带的「预设筛选词」（与 core/parser.py::_PRESET_COMMANDS 同口径：虚空 = 普通）。
# 补回别名包含的筛选词，例如「蹲 钢铁裂隙」的「钢铁」，避免订阅范围扩大。
PUSH_ALIAS_PRESET = {
    "钢铁裂隙": "钢铁",
    "虚空裂隙": "普通",
    "九重天裂隙": "九重天",
    "钢铁裂缝": "钢铁",
    "虚空裂缝": "普通",
    "九重天裂缝": "九重天",
}


def normalize_event(word: str) -> Optional[str]:
    w = word.strip()
    if w in PUSH_EVENTS:
        return w
    return PUSH_ALIAS.get(w)


def alias_preset(word: str) -> Optional[str]:
    """别名自带的预设筛选词（如 钢铁裂隙 → 钢铁）；没有则 None。"""
    return PUSH_ALIAS_PRESET.get(word.strip())


def subscription_event(toks, normalize_rules=None):
    event_type = None
    rest: list[str] = []
    for tok in toks:
        ev = normalize_event(tok)
        if ev and event_type is None:
            event_type = ev
            if alias_preset(tok):  # 钢铁裂隙 → 裂隙 + 筛选「钢铁」（别把档位吃掉）
                rest.append(alias_preset(tok))
        else:
            rest.append(tok)
    # ★ 2026-10-03（口径）：「T1–T6 / 古纪…全能」这类**档位词本身
    #   就含裂隙语义** —— 没写类型词时自动判定为裂隙（「蹲 钢铁t5歼灭」
    #   直接生效，不必再写「裂隙」）。⚠ 只认档位词：钢铁/虚空/地点词
    #   仍走「未识别」提示，不重蹈 2026-09-14 静默降级的覆辙。
    if event_type is None and any(contains_fissure_tier(t) for t in toks):
        event_type = "裂隙"
    if event_type and normalize_rules:  # 英文筛选词并入中文内部键（中文输入原样不动）
        rest = normalize_rules(event_type, rest)
    return event_type, rest


def subscription_rule(event, words):
    return (" " if event == "裂隙" else ",").join(words)


def build_cancel_selector(umo, heads, *, normalize_rules=None):
    """精确匹配完整规则；部分匹配用于从权限范围内收集候选。"""
    event, words = subscription_event(heads, normalize_rules)

    def in_scope(sub):
        return sub.umo == umo and (not event or sub.event == event)

    def exact(sub):
        # 旧订阅可能在 OR 逗号两侧留有空格；英文候选命令统一输出半角紧凑写法。
        rule = re.sub(r"\s*[,，]\s*", ",", sub.rule) if sub.event == "裂隙" else sub.rule
        return in_scope(sub) and (not words or rule == subscription_rule(sub.event, words))

    def fuzzy(sub):
        return in_scope(sub) and bool(words) and any(word in sub.rule for word in words)

    return event, words, exact, fuzzy, " ".join(heads) or "全部"


# ---------------------------------------------------------------------------
# ★ 进程级活跃守护登记（2026-09-26 修「重复推送」）
# ---------------------------------------------------------------------------
# 事故：同一时刻连发两条（04:00 仲裁 / 05:02 裂隙 / 08:00 仲裁 + 08:02 裂隙同内容）。
# 真因 = **推送守护跨实例叠加**：原来 start() 的幂等只看**实例级** ``self._task``，
# 插件重载时新实例的 ``_task`` 是 None，**感知不到旧实例仍存活的 daemon** ——
# 若某次重载没走到 terminate()（或 stop 未生效），旧 daemon 存活 → 两个 daemon 并存
# → 同一事件各推一次。实测时序吻合：最后一次「守护已启动」= 02:41:08，重复全在其后。
#
# 修法：把登记提到**模块级**（进程内共享），start() 先取消任何仍活跃的旧 daemon 再启动新的。
_LIVE_DAEMONS: "set[asyncio.Task]" = set()

# 守护协程的限定名（扫描用；与代码版本/模块对象无关，见 `_live_daemon_tasks`）
_DAEMON_CORO_MARK = "PushDaemon._run"


def _live_daemon_tasks() -> list:
    """扫事件循环里所有**仍在跑**的推送守护协程（跨实例、跨代码版本）。

    为什么不能只靠 :data:`_LIVE_DAEMONS`：那张表是本版本引入的，**修复前泄漏的守护
    当年创建时还没登记**，于是既看不见也取消不掉 —— 线上实测就是这样：日志只有一条
    「已推送」，群里却收到两条（旧守护没有派发日志，静默推送）。
    协程的 ``__qualname__`` 只由类名与方法名决定，所以「修复前启动的守护」同样能被这里找到。
    """
    try:
        tasks = asyncio.all_tasks()
    except RuntimeError:  # 没有运行中的事件循环（离线测试/CLI）：退回登记表
        return []
    out = []
    for t in tasks:
        try:
            # Task.get_coro() -> 协程对象，其 __qualname__ 形如 "PushDaemon._run"
            name = getattr(t.get_coro(), "__qualname__", "") or ""
        except Exception:  # noqa: BLE001 - 取不到协程信息就当它不是守护
            continue
        if _DAEMON_CORO_MARK in name:
            out.append(t)
    return out


class _PushOwnership:
    def __init__(self):
        self.current = None
        self.sending = {}
        self.lock = threading.Lock()


_PUSH_PATHS = WeakValueDictionary()


def _push_ownership(path):
    if path is None:
        return _PushOwnership()
    # AstrBot 可以重新导入整个插件包。旧回执与新实例仍属同一宿主事件循环。
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        registry = _PUSH_PATHS
    else:
        registry = getattr(loop, "_sdjk_push_paths", None)
        if registry is None:
            loop._sdjk_push_paths = registry = _PUSH_PATHS
    return registry.setdefault(Path(path).resolve(), _PushOwnership())


class PushDaemon:
    SEND_TIMEOUT = 20.0

    def __init__(
        self,
        client: WarframeClient,
        store: SubscriptionStore,
        send: SendFunc,
        logger,
        interval: int = 45,
        groups=None,
        state_path=None,
        tick_hook: Optional[Callable[[float], Awaitable[None]]] = None,
        *,
        display_timezone: str = display_time.DEFAULT,
    ):
        """groups：GroupStore（或任何有 ``get(umo)->dict`` 的对象），用来在派发前查群推送开关；
        state_path：基线快照落盘路径（运行目录；不传则不落盘）。"""
        self.client = client
        self.store = store
        self.send = send
        self.log = logger
        self.interval = max(15, interval)
        self._task: Optional[asyncio.Task] = None
        self._last: dict[str, dict] = {}  # platform -> 上次快照摘要
        self._filters: dict[str, FissureFilter] = {}  # sub.sid -> 裂隙筛选缓存
        self._matched: dict[str, set[str]] = {}  # 事件key -> 本轮筛选命中的 sub.sid
        self._text_for: dict = {}  # 事件key -> 按会话命中订阅现算文本的回调（赏金合并）
        self._event_meta: dict = {}  # (事件类型, key) -> 重投所需的源到期时间/赏金分行
        self.groups = groups
        self.tick_hook = tick_hook
        self.display_timezone = display_timezone
        self.state_path = Path(state_path) if state_path else None
        self._state_lock = asyncio.Lock()
        self._state_warned = False
        self._subs_save_pending = False
        self._baseline_ts = 0.0  # 仅保存重投队列时沿用，避免延长旧基线的有效期
        self._tick_no = 0
        # 零订阅时 tick 不更新基线文件，面板从内存读取最近一次运行时间。
        self.last_tick_at: Optional[float] = None
        self.last_tick_completed_at: Optional[float] = None
        self._sending: dict[str, asyncio.Future] = {}
        self._receipt_tasks: set[asyncio.Task] = set()
        self._receipts: list[dict] = []
        self._saved_last: dict = {}  # 中途落盘游标时沿用整轮已提交的基线
        self.last_tick_idle = False
        self._tick_ok = 0
        self._tick_fail = 0
        self._fail_streak = 0  # 数据源整体失败的连续 tick 数（指数退避）
        self._dead: set[str] = set()  # 本 tick 内已消费 / 已移除的订阅 sid
        self._purged_umos: set[str] = set()
        self._pending: list[dict] = []  # 发送失败待重投的事件
        self._fail_count: dict[str, int] = {}  # umo -> 连续失败轮数
        self._fail_tick: dict[str, int] = {}  # umo -> 最近一次记失败的 tick 序号
        # ★ 2026-10-17：umo -> 暂停记录。只在拿不到 GroupStore 时使用；有 GroupStore 时它就是真源
        self._pause: dict[str, dict] = {}
        self._bad_pause_warned: set[str] = set()
        self._retired = False
        self._owner = _push_ownership(getattr(store, "path", self.state_path))
        with self._owner.lock:
            previous = self._owner.current
            self._owner.current = self
        if previous is not None:
            previous.retire()
        bind_writer = getattr(store, "bind_writer", None)
        if bind_writer is not None:
            bind_writer(
                self._replace_owned, reload=previous is not None and previous.store is not store
            )
        self._load_state()
        if previous is not None:
            # 分片回执可能已更新内存游标，旧代的检查点写入却尚未完成。
            for record in previous._pending:
                if record.get("confirmed", 0):
                    restored = self._pending_for(record)
                    if (
                        restored is not None
                        and record["created"] == restored["created"]
                        and record["confirmed"] > restored.get("confirmed", 0)
                    ):
                        self._discard_pending(restored)
                        self._restore_pending([record], time.time())
            # 平台已经确认、但旧代还在等待订阅锁或回执写盘。由本代幂等重放。
            for receipt in previous._receipts:
                if receipt not in self._receipts:
                    self._receipts.append(copy.deepcopy(receipt))
                pending = self._pending_for(receipt)
                if pending is not None:
                    self._discard_pending(pending)

    def retire(self):
        self._retired = True

    def _replace_owned(self, tmp, path):
        with self._owner.lock:
            if self._owner.current is not self:
                raise OSError("推送存档已由新实例接管")
            tmp.replace(path)

    def _pending_for(self, event):
        return next(
            (p for p in self._pending if all(p[k] == event[k] for k in ("umo", "kind", "key"))),
            None,
        )

    # ------------------------------------------------------------------
    def _set_baseline(self, last: dict, key: str, value, kind: str = "truthy") -> None:
        """只在**有效**响应时更新基线（2026-09-26 加固，非重复推送的根因）。

        源降级/限流/超时后常返回**空结构**；若把空值写进基线，下一轮拿到正常数据时
        这些事件会被**当成新的再推一遍**（跨轮重推）。所以空值一律**保留旧基线**；
        ``ids`` 类另加**骤降判据**：新值不足旧值一半 → 视为源降级，同样保留旧基线。
        两种拒绝都记日志，等源恢复后自然对齐（代价：真实骤减时基线会多留一轮，
        方向偏保守 —— 宁可少推，不可重推）。
        """
        if kind == "ids":
            prev = last.get(key) or []
            ok = bool(value) and (not prev or len(value) >= max(1, len(prev) // 2))
        elif kind == "bool":
            ok = value is not None
        else:
            ok = bool(value)
        if ok:
            last[key] = value
        else:
            self.log.warning(
                "[warframe] %s 响应为空/骤降（%r），保留旧基线不更新（防跨轮重推）", key, value
            )

    @property
    def task(self) -> Optional[asyncio.Task]:
        """当前守护任务（供宿主挂 done-callback）。"""
        return self._task

    def start(self) -> None:
        """启动守护；**进程级**幂等 —— 先清掉任何仍活跃的旧 daemon（跨实例泄漏）。"""
        if self._retired:
            return
        for dead in [x for x in _LIVE_DAEMONS if x.done()]:
            _LIVE_DAEMONS.discard(dead)  # 已结束的登记顺手清掉，防集合膨胀
        # ★ 2026-09-26 二次修复（线上实测：只记一条日志、群里却收到两条）：
        #   `_LIVE_DAEMONS` 只装**本版本**注册过的守护 —— 修复前泄漏的守护当年创建时
        #   还没有这张表，所以既看不见、也取消不掉，会一直静默推下去（旧代码没有派发日志，
        #   日志里查不到它）。改为**直接扫事件循环**：凡协程名是 `PushDaemon._run` 的任务
        #   都是本类的守护（协程限定名与代码版本、模块对象无关），逐个取消。
        leaked = [x for x in _LIVE_DAEMONS if not x.done() and x is not self._task]
        leaked += [
            t
            for t in _live_daemon_tasks()
            if t is not self._task and t not in leaked and t not in _LIVE_DAEMONS
        ]
        for task in leaked:
            task.cancel()  # ★ 旧实例/旧版本泄漏的守护：取消，防重复推送
            _LIVE_DAEMONS.discard(task)
        if leaked:
            self.log.warning(
                "[warframe] 发现 %d 个仍在运行的旧推送守护 → 已取消（防重复推送）", len(leaked)
            )
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="warframe-push-daemon")
            _LIVE_DAEMONS.add(self._task)
            self.log.info(
                "[warframe] 推送守护协程已启动（间隔 %ss，实例 %s，活跃 %d）",
                self.interval,
                id(self),
                len(_LIVE_DAEMONS),
            )
        else:
            self.log.info(
                "[warframe] 推送守护已在运行（重复 start 幂等跳过，实例 %s，活跃 %d）",
                id(self),
                len(_LIVE_DAEMONS),
            )

    async def stop(self) -> None:
        """停止本实例的守护并**注销登记**（幂等；失败也记日志）。"""
        task = self._task
        if task is None and not self._sending and not self._receipt_tasks:
            return
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # noqa: BLE001
                self.log.warning("[warframe] 推送守护停止时异常：%s", exc)
            finally:
                _LIVE_DAEMONS.discard(task)
                self._task = None
        # 适配器可能在取消后返回有效回执。先等发送退出，再等已经接手的游标写入和消费。
        if self._sending:
            _, still_sending = await asyncio.wait(
                set(self._sending.values()), timeout=self.SEND_TIMEOUT
            )
            # 已超时的 SDK 也可能卡在它自己的取消清理中。回执写入已经由独立任务接手。
            for sending in still_sending:
                sending.cancel()
            if still_sending:
                await asyncio.gather(*still_sending, return_exceptions=True)
        await self._wait_receipts()
        if self._pending or self._receipts:
            await self._save_state(None, checkpoint=True)
        self.log.info("[warframe] 推送守护已停止（仍活跃 %d）", len(_LIVE_DAEMONS))

    async def _run(self) -> None:
        try:
            while True:
                try:
                    await self.tick()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - 守护协程不允许退出
                    self.log.error("[warframe] 推送轮询异常：%s", exc)
                await asyncio.sleep(self.next_delay())
        except asyncio.CancelledError:
            self.log.info("[warframe] 推送守护协程被取消（实例 %s）", id(self))
            raise
        finally:
            # 退出（含被取消）时注销自己的登记，避免集合里留死引用
            cur = asyncio.current_task()
            if cur is not None:
                _LIVE_DAEMONS.discard(cur)

    # ------------------------------------------------------------------
    def filter_for(self, sub: Subscription) -> FissureFilter:
        if sub.sid not in self._filters:
            self._filters[sub.sid] = parse_fissure_filter(sub.rule)
        return self._filters[sub.sid]

    async def tick(self) -> None:
        if self._retired:
            return
        await self._poll()
        self.last_tick_completed_at = time.time()

    async def _poll(self) -> None:
        now = time.time()
        self._tick_no += 1
        self.last_tick_at = now
        self._tick_ok = 0
        self._tick_fail = 0
        self._dead = set()  # 本 tick 内已消费 / 已移除的订阅 sid（防同 tick 超额派发）
        self._purged_umos = set()
        await self._wait_receipts()
        for receipt in self._receipts.copy():
            sids = set(receipt["sids"])
            delivered = [
                sub
                for sub in self.store.for_umo(receipt["umo"])
                if sub.event == receipt["kind"] and sub.sid in sids
            ]
            await self._consume_receipt(receipt, delivered, replay=True)
        if self.tick_hook is not None:
            try:
                await self.tick_hook(now)
            except Exception as exc:  # noqa: BLE001 - 早报/统计失败不阻塞订阅
                self.log.warning("[warframe] 推送附加任务失败：%s", type(exc).__name__)
        await self._expire_sweep(now)
        subs = [s for s in self.store.all() if not s.expired(now)]
        self.last_tick_idle = not subs
        if not subs:
            self._filters.clear()
            if self._last or self._pending:
                self._last.clear()  # 所有订阅清空 ⇒ 丢弃全部基线（防日后重订时拿陈旧基线 diff）
                self._pending.clear()
                await self._save_state(now)
            return
        # 订阅删掉后筛选缓存也要跟着走，长期运行才不会慢慢漏内存
        live_sids = {s.sid for s in subs}
        for sid in [k for k in self._filters if k not in live_sids]:
            self._filters.pop(sid, None)
        # v1.3.1：基线按「平台 × 显示语言」分组。中文组沿用旧键（pc），英文组用 pc|en ——
        #   推送文本在差量比对时生成，两种语言的群各比对、各渲染一份；
        #   数据请求走客户端缓存，不会因为多一种语言而多打上游。
        groups = sorted({self._baseline_key(s) for s in subs})
        for gone in [p for p in self._last if p not in groups]:
            self._last.pop(gone, None)  # 该平台 / 语言已无订阅：基线作废
        had_pending = bool(self._pending)
        await self._flush_pending(now, subs)
        for group in groups:
            group_subs = [s for s in subs if self._baseline_key(s) == group]
            platform = group_subs[0].platform
            with i18n.use_lang(group_subs[0].lang), display_time.use_zone(self.display_timezone):
                await self._diff_and_deliver(platform, group, group_subs, now)
        # 数据源整体失败 ⇒ tick 级指数退避；有任何一类成功即复位
        if self._tick_ok:
            self._fail_streak = 0
        elif self._tick_fail:
            self._fail_streak += 1
        if self._tick_ok or had_pending or self._pending:
            await self._save_state(now if self._tick_ok else None)

    async def _arbi_line_en(self, sl: dict) -> str:
        """仲裁推送的英文节点行：节点（星球） · 类型 · 派系 + 评级；取不到英文表时回落中文原行。"""
        try:
            from .commands.arbitration import _arb_en_node

            _sched, nodes, tier_of = await arbi.fetch_tables(self.client)
            name, system, mission, faction = _arb_en_node(nodes, sl["key"])
        except Exception:  # noqa: BLE001 - 取不到就用 arbi 的原行，不阻断推送
            return sl["line"]
        head = " · ".join(
            p for p in (f"{name} ({system})" if system else name, mission, faction) if p
        )
        tv = arbi.tier_label(sl["key"], tier_of)
        return f"{head}　[{tv}]" if tv else head

    @staticmethod
    def _baseline_key(sub: Subscription) -> str:
        lang = getattr(sub, "lang", "zh")
        return sub.platform if lang == "zh" else f"{sub.platform}|{lang}"

    async def _diff_and_deliver(
        self, platform: str, group: str, subs: list[Subscription], now: float
    ) -> None:
        events = await self._snapshot_diff(platform, subs, baseline_key=group)
        if not events:
            return
        by_event: dict[str, list[Subscription]] = {}
        for sub in subs:
            if sub.platform == platform:
                by_event.setdefault(sub.event, []).append(sub)
        for kind, key, text in events:
            cands = by_event.get(kind, [])
            # 同群多条筛选只派发给自身规则命中的订阅，保留原有回退与存储顺序。
            matched = self._matched.get(key)
            prefer = cands
            if matched:
                prefer = [s for s in cands if s.sid in matched] or cands
            by_umo: dict[str, list[Subscription]] = {}
            for sub in prefer:
                by_umo.setdefault(sub.umo, []).append(sub)
            for umo, recipients in by_umo.items():
                await self._deliver(umo, kind, key, text, recipients, now, self._text_for.get(key))

    def next_delay(self) -> float:
        """下一次 tick 前的等待秒数：数据源持续失败时指数退避（上限 10 分钟），恢复即复位。"""
        if self._fail_streak <= 0:
            return float(self.interval)
        return float(min(self.interval * (2 ** min(self._fail_streak, 6)), max(self.interval, 600)))

    # ------------------------------------------------------------------
    # 基线落盘（重启补推）
    # ------------------------------------------------------------------
    def _load_state(self) -> None:
        """读运行目录里的基线快照：30 分钟内沿用（重启后正常 diff 补推停机期间的新事件），
        更旧 / 损坏则丢弃（只重新建基线、不推存量）。"""
        p = self.state_path
        if p is None:
            return
        try:
            if not p.exists():
                return
            data = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("推送快照应为对象")
        except Exception as exc:  # noqa: BLE001
            self.log.warning("[warframe] 推送快照不可用（%s），本次重新建基线", exc)
            return
        receipts = data.get("receipts", [])
        if isinstance(receipts, list):
            for receipt in receipts:
                self._restore_receipt(receipt)
        now = time.time()
        self._restore_pending(data.get("pending", []), now)
        try:
            ts = _pause_number(data.get("ts"))
            last = data.get("last")
            if not isinstance(last, dict) or not all(isinstance(v, dict) for v in last.values()):
                raise ValueError("last 结构不对")
        except (ValueError, OverflowError) as exc:
            self.log.warning("[warframe] 推送基线快照不可用（%s），本次重新建基线", exc)
            return
        age = now - ts
        if 0 <= age <= BASELINE_MAX_AGE:
            self._last = last
            self._saved_last = copy.deepcopy(last)
            self._baseline_ts = ts
            self.log.info(
                "[warframe] 已载入推送基线快照（%d 秒前），停机期间的新事件将补推", int(age)
            )
        else:
            self.log.info(
                "[warframe] 推送基线快照已过期（%d 秒前，>30 分钟），只重建基线不补推", int(age)
            )

    def _restore_receipt(self, receipt) -> None:
        if not isinstance(receipt, dict) or not all(
            isinstance(receipt.get(k), str) and receipt[k] for k in ("umo", "kind", "key")
        ):
            return
        sids, stamp = receipt.get("sids"), receipt.get("ts")
        if (
            not isinstance(sids, list)
            or not sids
            or not all(isinstance(s, str) and s for s in sids)
        ):
            return
        if type(stamp) not in (int, float):
            return
        try:
            stamp = _pause_number(stamp)
        except (ValueError, OverflowError):
            return
        self._receipts.append(
            {k: receipt[k] for k in ("umo", "kind", "key", "sids")} | {"ts": stamp}
        )

    def _restore_pending(self, items, now: float) -> None:
        """队列损坏不影响旧基线；仅恢复未过期、未耗尽且仍有对应订阅的待发项。"""
        if not isinstance(items, list):
            self.log.warning("[warframe] 待重投队列结构不对，已忽略（保留基线）")
            return
        live = {s.sid: s for s in self.store.all() if not s.expired(now)}
        for it in items:
            if not isinstance(it, dict):
                continue
            if not all(
                isinstance(it.get(k), str) and it[k] for k in ("umo", "kind", "key", "text")
            ):
                continue
            created, tries, sids = it.get("created"), it.get("tries"), it.get("sids")
            if (
                not isinstance(sids, list)
                or not sids
                or not all(isinstance(s, str) and s for s in sids)
            ):
                continue
            chunks, confirmed = it.get("chunks"), it.get("confirmed")
            if "chunks" in it or "confirmed" in it:
                if (
                    not isinstance(chunks, list)
                    or not chunks
                    or not all(isinstance(chunk, str) and chunk for chunk in chunks)
                    or "".join(chunks) != it["text"]
                    or type(confirmed) is not int
                    or not 0 <= confirmed <= len(chunks)
                ):
                    continue
                if confirmed == len(chunks):
                    # 旧版快照的末片可能迟到。已确认整条的消费不受重投 TTL、窗口和预算限制。
                    self._restore_receipt({**it, "ts": created})
                    continue
            if type(created) not in (int, float) or not now - PENDING_TTL <= created <= now:
                continue
            if type(tries) is not int or not 0 <= tries < PENDING_MAX_TRIES:
                continue
            eligible_sids = [
                sid
                for sid in sids
                if sid in live
                and live[sid].umo == it["umo"]
                and live[sid].event == it["kind"]
                and it["key"] not in live[sid].notified
            ]
            if eligible_sids:
                record = {
                    **{k: it[k] for k in ("umo", "kind", "key", "text", "created", "tries")},
                    "sids": eligible_sids,
                }
                if it.get("unattempted") is True:
                    record["unattempted"] = True
                if chunks:
                    record.update(chunks=chunks, confirmed=confirmed)
                if "expires" in it:
                    try:
                        if type(it["expires"]) not in (int, float):
                            continue
                        record["expires"] = _pause_number(it["expires"])
                    except (ValueError, OverflowError):
                        continue
                    if record["expires"] <= now:
                        continue
                if "parts" in it:
                    parts = it["parts"]
                    if it["kind"] != "赏金" or not isinstance(parts, list) or not parts:
                        continue
                    if any(
                        not isinstance(part, dict)
                        or not isinstance(part.get("text"), str)
                        or not part["text"]
                        or not isinstance(part.get("sids"), list)
                        or not part["sids"]
                        or any(not isinstance(sid, str) or sid not in sids for sid in part["sids"])
                        for part in parts
                    ):
                        continue
                    surviving = set(eligible_sids)
                    record["parts"] = _bounty_parts_for(parts, surviving)
                    covered = {sid for part in record["parts"] for sid in part["sids"]}
                    if covered != surviving:
                        # 分行缺失时不能把没有对应正文的订阅一起 @ 或消费。
                        continue
                    if record.get("confirmed") and surviving != set(sids):
                        with i18n.use_lang(live[eligible_sids[0]].lang):
                            body = _bounty_parts_text(record["parts"], surviving)
                        try:
                            progress = _bounty_retry_chunks(
                                LiteralProgress(record["chunks"], record["confirmed"]),
                                parts,
                                surviving,
                                body,
                            )
                        except ValueError:
                            continue
                        record.update(
                            text=body, chunks=progress.chunks, confirmed=progress.confirmed
                        )
                elif it["kind"] == "赏金" and set(eligible_sids) != set(sids):
                    # 老队列只有合并正文，收件集合改变后无法安全拆回各订阅，保留订阅而跳过旧条目。
                    continue
                self._pending.append(record)

    async def _save_state(self, now: float | None, *, checkpoint=False, required=False) -> None:
        """中途写入待发项或回执时，只带上已经完整派发过的那轮基线。"""
        if self.state_path is None or self._owner.current is not self:
            return

        async def write():
            state = {
                "last": self._saved_last if checkpoint else self._last,
                "pending": self._pending,
            }
            if self._receipts:
                state["receipts"] = self._receipts
            snapshot = copy.deepcopy(state)
            snapshot["ts"] = self._baseline_ts if now is None else now
            await asyncio.to_thread(
                write_json_atomic, self.state_path, snapshot, replace=self._replace_owned
            )
            self._baseline_ts = snapshot["ts"]
            self._saved_last = snapshot["last"]

        try:
            async with self._state_lock:
                await finish_write(write())
        except Exception as exc:  # noqa: BLE001 - 落盘失败不影响推送
            if not self._state_warned:
                self._state_warned = True
                self.log.warning("[warframe] 推送状态落盘失败：%s", exc)
            if required:
                raise _CheckpointFailed from exc

    # ------------------------------------------------------------------
    # 到期提醒
    # ------------------------------------------------------------------
    async def _expire_sweep(self, now: float) -> None:
        """gc 过期订阅；被清理的在该群发一条简短提醒（@ 订阅人，同群同 tick 合并）。"""
        try:
            gone = self.store.expired_list(now)
        except Exception as exc:  # noqa: BLE001
            self.log.warning("[warframe] 扫描过期订阅失败：%s", exc)
            gone = []
        try:
            removed = 0
            if gone:
                removed = await self.store.remove(lambda s: s.expired(now), retain_on_failure=True)
            if self._subs_save_pending and not removed:
                await self.store.save()
            self._subs_save_pending = False
        except Exception as exc:  # noqa: BLE001 - 写盘失败：内存已清，下次再落盘
            self._subs_save_pending = True
            self.log.warning("[warframe] 订阅状态补写失败：%s", exc)
        by_umo: dict[str, list[Subscription]] = {}
        for s in gone:
            by_umo.setdefault(s.umo, []).append(s)
        for umo, items in by_umo.items():
            if not self._group_on(umo):
                continue  # 暂停中的群不打扰
            parts = []
            for s in items:
                label = f"#{s.sid} {s.event}" + (f"（{s.rule}）" if s.rule else "")
                parts.append(label)
            with i18n.use_lang(getattr(items[0], "lang", "zh")):
                text = self._expire_text(items, parts)
            at_ids = list(dict.fromkeys(s.created_by for s in items if s.created_by))
            try:
                await self._send_once(umo, text, at_ids)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 提醒发不出去不重试（订阅本就结束了）
                self.log.warning(
                    "[warframe] 到期提醒发送失败（%s）：%s", mask_umo(umo), mask_in_text(exc, umo)
                )
            else:
                # 记录成功投递，保持与普通推送的日志口径一致。
                self.log.info(
                    "[warframe] 到期提醒已发出（%s）：%d 条 %s",
                    mask_umo(umo),
                    len(items),
                    " ".join(f"#{s.sid}" for s in items),
                )

    @staticmethod
    def _expire_text(items: list[Subscription], parts: list[str]) -> str:
        """到期提醒正文（当前语言由调用方用 use_lang 包住）。"""
        if not i18n.is_en():
            return (
                f"⏰ 蹲订阅已到期并结束（{len(items)} 条）：{'、'.join(parts)}。"
                "需要继续请重新发送「蹲 类型 筛选 时长」续订（如：蹲 裂隙 钢铁捕获 永久）。"
            )
        from .commands.dun import _ev_name, _rule_en  # 延迟导入：dun 反向依赖本模块

        en_parts = [
            f"#{s.sid} {_ev_name(s.event)}" + (f" ({_rule_en(s.rule, s.event)})" if s.rule else "")
            for s in items
        ]
        return (
            f"⏰ Watch subscriptions expired and ended ({len(items)}): {', '.join(en_parts)}. "
            'To keep watching, send "watch <type> <filter> <duration>" again '
            "(e.g. watch fissures steel capture forever)."
        )

    # ------------------------------------------------------------------
    # 派发
    # ------------------------------------------------------------------
    def _group_on(self, umo: str) -> bool:
        """群推送开关（`.关闭 推送` = 订阅暂停，不消费、不记 notified）。"""
        if self.groups is None:
            return True
        try:
            return bool(self.groups.get(umo).get("push"))
        except Exception:  # noqa: BLE001 - 查不到开关就按开处理（宁可多推，不丢推送）
            return True

    # ---- 连续失败「暂停推送」（2026-10-17）--------------------------------
    def _pause_of(self, umo: str) -> Optional[dict]:
        """该会话的暂停记录；没有 ⇒ None。

        有 GroupStore 时**只认它**（它本身常驻内存，``set_pause`` 先改内存再写盘，写盘失败也不丢）：
        若另存一份守护内存镜像，群内 ``.开启 推送`` 清了存档却清不到镜像，会话会继续被暂停到重载。
        拿不到 GroupStore（离线 / 测试桩）才退回守护内存表。
        """
        if self.groups is not None:
            try:
                info = self.groups.pause_of(umo)
            except Exception:  # noqa: BLE001 - 读不到按未暂停处理（宁可多推，不丢推送）
                return None
        else:
            info = self._pause.get(umo)
        if not isinstance(info, dict):
            self._bad_pause_warned.discard(umo)
            return None
        normalized = dict(info)
        try:
            for key in ("since", "next_try", "fails"):
                value = info.get(key)
                if value is None:
                    continue  # 历史缺省字段保留原有 now/0 回退语义。
                number = _pause_number(value)
                if key == "fails":
                    if not number.is_integer():
                        raise ValueError("暂停次数应为整数")
                    normalized[key] = int(number)
                else:
                    normalized[key] = number
        except (ValueError, OverflowError):
            if umo not in self._bad_pause_warned:
                self._bad_pause_warned.add(umo)
                self.log.warning(
                    "[warframe] %s：暂停记录字段不合法，仅跳过该会话；管理员可重新开启推送恢复",
                    mask_umo(umo),
                )
            return _InvalidPause()
        self._bad_pause_warned.discard(umo)
        return normalized

    async def _write_pause(self, umo: str, info: Optional[dict]) -> None:
        """写 / 解除暂停：有 GroupStore 写它（内存先变、语义立刻生效，落盘失败只记 WARNING），否则写守护内存表。"""
        if self.groups is None:
            if info is None:
                self._pause.pop(umo, None)
            else:
                self._pause[umo] = dict(info)
            return
        try:
            await self.groups.set_pause(umo, info)
        except Exception as exc:  # noqa: BLE001 - 写盘失败不影响内存语义（照 _purge_umo）
            self.log.warning(
                "[warframe] 暂停状态写盘失败（%s）：%s", mask_umo(umo), mask_in_text(exc, umo)
            )

    async def _pause_or_purge(self, umo: str, err: BaseException, n_fail: int, now: float) -> None:
        """连续失败达阈值：未暂停 ⇒ 改为暂停推送；已暂停（本次是试发）⇒ 满 7 天才清理订阅。"""
        prev = self._pause_of(umo)
        if isinstance(prev, _InvalidPause):
            return
        if prev is None:
            await self._write_pause(
                umo,
                {
                    "since": now,
                    "next_try": now + PAUSE_RETRY_SECS,
                    "reason": _brief_reason(err, umo),
                    "fails": n_fail,
                },
            )
            # 暂停期间不再重投 ⇒ 清掉该会话的待发队列（照 _purge_umo 的写法）
            self._pending[:] = [p for p in self._pending if p["umo"] != umo]
            self.log.warning(
                "[warframe] %s：连续 %d 轮发送失败 → 已暂停该会话推送"
                "（%d 小时后重试；满 %d 天仍失败才清理，面板可手动恢复）",
                mask_umo(umo),
                n_fail,
                PAUSE_RETRY_SECS // 3600,
                PAUSE_PURGE_SECS // 86400,
            )
            return
        since = float(prev.get("since") or now)
        if now - since >= PAUSE_PURGE_SECS:
            await self._purge_umo(umo, f"暂停 {PAUSE_PURGE_SECS // 86400} 天仍发送失败")
            await self._write_pause(umo, None)
            return
        await self._write_pause(
            umo,
            {
                "since": since,
                "next_try": now + PAUSE_RETRY_SECS,
                "reason": _brief_reason(err, umo),
                "fails": int(prev.get("fails") or 0) + 1,
            },
        )
        self.log.warning(
            "[warframe] %s：暂停期间试发仍失败（暂停已 %d 天）→ %d 小时后再试",
            mask_umo(umo),
            int((now - since) // 86400),
            PAUSE_RETRY_SECS // 3600,
        )

    def _subscription_time(self, now: float):
        with display_time.use_zone(self.display_timezone):
            return display_time.local_datetime(now)

    def _can_deliver(self, umo: str, now: float) -> bool:
        if self._retired or not self._group_on(umo):
            return False
        pause = self._pause_of(umo)
        return pause is None or (
            not isinstance(pause, _InvalidPause) and now >= float(pause.get("next_try") or 0)
        )

    def _eligible(self, subs_u: list[Subscription], key: str) -> list[Subscription]:
        """本事件对这批（同会话）订阅里：仍存活 / 当前时间窗允许 / 没就此事件通知过的那些。"""
        now = time.time()
        local = self._subscription_time(now)
        out = []
        for s in subs_u:
            if s.sid in self._dead or key in s.notified:
                continue
            if s.expired(now):
                # 本轮取数或前面的发送可能跨过到期点；留给下一轮正常 gc 和到期提醒。
                self._dead.add(s.sid)
                continue
            if not self.store.has(s.sid, unnotified=key):
                # 迟到回执可能更新了上轮持有的订阅对象，派发前以当前存档复查。
                continue
            if not s.time_window().allows(local):
                continue
            out.append(s)
        return out

    async def _deliver(
        self,
        umo: str,
        kind: str,
        key: str,
        text: str,
        subs_u: list[Subscription],
        now: float,
        text_for=None,
        retry: Optional[dict] = None,
    ) -> str:
        """向一个会话派发一条事件；返回 ok（已送达）、fail（发送失败）、defer（延后）或 skip（无需发）。

        · 只合并 @「本事件命中、时间窗当前允许、尚未就此事件通知过、仍存活」的订阅，
          被合并的订阅同样记 notified 并按各自时长语义消费（一次性的被 @ 即消费）；
        · 群开关关闭 ⇒ 整体跳过（不消费、不记 notified）；
        · 瞬时发送失败 ⇒ 进待发队列下一轮重投（有重试次数与过期上限）。
        """
        if not self._can_deliver(umo, now):
            return "skip"
        if retry is None and any(
            (p["umo"], p["kind"], p["key"]) == (umo, kind, key)
            for p in (*self._pending, *self._receipts)
        ):
            return "defer"  # 重启后旧基线可能再次检出已登记的事件。
        meta = retry if retry is not None else self._event_meta.get((kind, key), {})
        if meta.get("expires") is not None and time.time() >= meta["expires"]:
            return "skip"
        if retry is not None and umo in self._owner.sending:
            return "defer"
        elig = self._eligible(subs_u, key)
        if not elig:
            return "skip"
        elig_sids = {s.sid for s in elig}
        if kind == "赏金" and retry is not None:
            if "parts" in retry:

                def text_for(sids):
                    return _bounty_parts_text(retry["parts"], sids)
            elif elig_sids != set(retry["sids"]):
                return "skip"  # 旧的不透明合并正文不能在收件集合改变后原样转发。
        body = text_for(elig_sids) if text_for else text
        if not body:
            return "skip"
        progress = LiteralProgress(meta.get("chunks", []), meta.get("confirmed", 0))
        if progress.chunks and body != text:
            if progress.confirmed:
                try:
                    progress = _bounty_retry_chunks(progress, meta["parts"], elig_sids, body)
                except ValueError:
                    self.log.warning("[warframe] 待重投赏金分行与原文不符，键=%s", key)
                    return "skip"
            else:
                progress = LiteralProgress()
        # 宿主回退也只收到未确认后缀。原生桥沿用分片边界，不受重载或配置变化影响。
        remaining = "".join(progress.chunks[progress.confirmed :]) if progress.chunks else body
        at_ids = list(dict.fromkeys(s.created_by for s in elig if s.created_by))
        record = retry
        if record is None:
            record = {
                "umo": umo,
                "kind": kind,
                "key": key,
                "created": now,
                "tries": 0,
                "unattempted": True,
            }
            self._pending.append(record)
        record.update(text=body, sids=[s.sid for s in elig])
        if "expires" in meta:
            record["expires"] = meta["expires"]
        if "parts" in meta:
            record["parts"] = _bounty_parts_for(meta["parts"], elig_sids)
        if progress.chunks:
            record.update(chunks=progress.chunks, confirmed=progress.confirmed)
        else:
            record.pop("chunks", None)
            record.pop("confirmed", None)

        async def confirmed(cursor: LiteralProgress):
            # 在取得回执的协程内同步更新状态并把写入交给守护，覆盖等订阅锁的阶段。
            active = self._owner.current
            pending = record if active is self else active._pending_for(record)
            if cursor.complete:
                receipt = {k: record[k] for k in ("umo", "kind", "key", "sids")}
                receipt["ts"] = now
                active._receipts.append(receipt)
                if pending is not None:
                    active._discard_pending(pending)
                work = active._consume_receipt(receipt, elig)
            elif pending is not None:
                if "parts" in record and pending["sids"] != record["sids"]:
                    cursor = _bounty_retry_chunks(
                        cursor, record["parts"], set(pending["sids"]), pending["text"]
                    )
                pending.update(chunks=cursor.chunks, confirmed=cursor.confirmed)
                work = active._save_state(None, checkpoint=True, required=True)
            else:
                return
            task = asyncio.create_task(work)
            active._receipt_tasks.add(task)
            task.add_done_callback(active._receipt_finished)
            await asyncio.shield(task)

        progress.on_confirm = confirmed
        if progress.complete:
            await confirmed(progress)
            return "ok"
        try:
            await self._save_state(None, checkpoint=True, required=True)
        except _CheckpointFailed:
            return "defer"
        if record not in self._pending:
            return "defer"  # 等待检查点期间，迟到回执已接手此事件。
        if self.state_path is not None:
            # 排队写快照期间可能有取消、窗口截止或轮换到期。
            ready_at = time.time()
            if (
                not self._can_deliver(umo, ready_at)
                or ready_at - record["created"] > PENDING_TTL
                or record.get("expires") is not None
                and ready_at >= record["expires"]
            ):
                self._discard_pending(record)
                return "skip"
            live = self._eligible(elig, key)
            if len(live) != len(elig):
                if not live:
                    self._discard_pending(record)
                    return "skip"
                return await self._deliver(umo, kind, key, body, live, now, retry=record)
        result = await self._send_guarded(umo, remaining, at_ids, now, progress)
        if result == "fail":
            if not record.pop("unattempted", False):
                record["tries"] += 1
            if umo in self._purged_umos or self._pause_of(umo) is not None:
                self._discard_pending(record)
        return result

    def _discard_pending(self, record: dict) -> None:
        if record in self._pending:
            self._pending.remove(record)

    def _receipt_finished(self, task: asyncio.Task) -> None:
        self._receipt_tasks.discard(task)
        if not task.cancelled():
            task.exception()

    async def _wait_receipts(self) -> None:
        while self._receipt_tasks:
            await asyncio.wait(self._receipt_tasks.copy())

    async def _send_once(
        self, umo: str, body: str, at_ids: list, progress: LiteralProgress | None = None
    ) -> None:
        """每个会话最多一笔在途发送。适配器延迟响应取消也有界返回。"""
        if self._retired or umo in self._owner.sending:
            raise _SendBusy("上次发送仍未退出")

        async def transmit():
            token = literal_progress.set(progress)
            try:
                if await self.send(umo, body, at=at_ids) is False:
                    raise RuntimeError("发送回调明确返回未发送")
                if progress is not None:
                    await progress.confirm_message(body)
            finally:
                literal_progress.reset(token)

        task = asyncio.create_task(transmit())
        self._sending[umo] = self._owner.sending[umo] = task

        def finished(done):
            self._sending.pop(umo, None)
            self._owner.sending.pop(umo, None)
            if not done.cancelled():
                done.exception()

        task.add_done_callback(finished)
        try:
            done, _ = await asyncio.wait({task}, timeout=self.SEND_TIMEOUT)
        finally:
            if not task.done():
                if progress is not None:
                    progress.stopped = True  # 即使适配器吞取消，也不能在后台继续投递后片。
                task.cancel()
        if progress is not None and progress.complete:
            await self._wait_receipts()
            return
        if not done:
            raise TimeoutError("发送超时，送达未确认")
        if task.cancelled():
            raise RuntimeError("发送被取消（非守护停止）")
        task.result()

    async def _send_guarded(
        self,
        umo: str,
        body: str,
        at_ids: list,
        now: float | None = None,
        progress: LiteralProgress | None = None,
    ) -> str:
        """真正发送一次；失败记 WARNING 并累计连续失败。

        · 识别出「群不在了」⇒ 照旧立即清理订阅；
        · 连续失败达 FAIL_PURGE_THRESHOLD ⇒ 暂停该会话推送（满 PAUSE_PURGE_SECS 才清理）。
        """
        now = time.time() if now is None else now
        try:
            await self._send_once(umo, body, at_ids, progress)
            return "ok"
        except (_SendBusy, _CheckpointFailed):
            return "defer"
        except Exception as exc:  # noqa: BLE001
            err = exc
            self.log.warning("[warframe] 推送失败（%s）：%s", mask_umo(umo), mask_in_text(err, umo))
        if self._retired:
            return "defer"
        # —— 失败累计（每会话每 tick 最多记一次）——
        if self._fail_tick.get(umo) != self._tick_no:
            self._fail_tick[umo] = self._tick_no
            self._fail_count[umo] = self._fail_count.get(umo, 0) + 1
        fatal = bool(_FATAL_SEND_RE.search(str(err)))
        n_fail = self._fail_count.get(umo, 0)
        if fatal:
            await self._purge_umo(umo, "发送目标不可达（群不存在/被踢类错误）")
        elif n_fail >= FAIL_PURGE_THRESHOLD:
            await self._pause_or_purge(umo, err, n_fail, now)
        return "fail"

    async def _purge_umo(self, umo: str, reason: str) -> None:
        """清理一个不可达会话的全部订阅（留 WARNING，之后不再刷屏）。"""
        sids = {s.sid for s in self.store.all() if s.umo == umo}
        try:
            n = await self.store.remove(lambda s: s.umo == umo, retain_on_failure=True)
        except Exception as exc:  # noqa: BLE001
            self._subs_save_pending = True
            n = len(sids)
            self.log.warning(
                "[warframe] 清理 %s 的订阅时写盘失败：%s", mask_umo(umo), mask_in_text(exc, umo)
            )
        self._dead |= sids
        self._purged_umos.add(umo)
        self._pending[:] = [p for p in self._pending if p["umo"] != umo]
        self._fail_count.pop(umo, None)
        self.log.warning("[warframe] %s：%s → 已清理该会话 %d 条订阅", mask_umo(umo), reason, n)

    async def _consume_receipt(
        self, receipt: dict, delivered: list[Subscription], *, replay=False
    ) -> None:
        """先保存平台回执，再幂等消费订阅。订阅写入成功才撤销回执。"""
        if not replay:
            await self._save_state(None, checkpoint=True)
        if self._owner.current is not self:
            return
        try:
            self._dead.update(
                await self.store.consume_batch(
                    delivered,
                    receipt["key"],
                    receipt["ts"],
                    retain_on_failure=True,
                    persist_unchanged=replay,
                )
            )
        except Exception as exc:  # noqa: BLE001
            # 连内存消费都未能执行时，本轮也不能再向这些 SID 派发其他事件。
            self._dead.update(s.sid for s in delivered if receipt["key"] not in s.notified)
            self.log.warning("[warframe] 已送达的订阅状态写盘失败，下轮补写：%s", exc)
        else:
            self._receipts.remove(receipt)
            await self._save_state(None, checkpoint=True)
        if not replay and self._owner.current is self:
            umo = receipt["umo"]
            self._fail_count.pop(umo, None)
            if self._pause_of(umo) is not None:
                await self._write_pause(umo, None)
                self.log.info("[warframe] %s：推送已恢复（试发成功，解除暂停）", mask_umo(umo))
            self.log.info(
                "[warframe] 已推送 事件=%s 键=%s → %s（实例 %s）",
                receipt["kind"],
                receipt["key"],
                mask_umo(umo),
                id(self),
            )

    async def _flush_pending(self, now: float, subs: list[Subscription]) -> None:
        """重投上一轮发送失败的事件：最多重试 PENDING_MAX_TRIES 轮，超过 PENDING_TTL 秒即丢。"""
        if not self._pending:
            return
        await self._wait_receipts()
        legacy_fissures = {}  # 本轮每平台最多补查一次，客户端 HTTP 缓存仍可复用。
        bounty_round = None
        checked_bounty = False
        for it in self._pending.copy():
            if it not in self._pending:
                continue
            expired = time.time() - it["created"] > PENDING_TTL
            if expired or it["tries"] >= PENDING_MAX_TRIES:
                self.log.warning(
                    "[warframe] 待重投事件放弃（%s 键=%s，已试 %d 次，%s）",
                    mask_umo(it["umo"]),
                    mask_in_text(it["key"], it["umo"]),
                    it["tries"],
                    "超过有效期" if expired else "达到重试次数上限",
                )
                self._discard_pending(it)
                continue
            # 用本 tick 的订阅对象（与主派发共用同一份内存状态，避免互相覆盖 notified / hits_left）
            subs_u = [s for s in subs if s.sid in it["sids"]]
            if not subs_u:
                self._discard_pending(it)
                continue
            if it["kind"] == "赏金" and "expires" not in it:
                if not checked_bounty:
                    checked_bounty = True
                    try:
                        bounty_round = _bounty_round(await self.client.bounty_cycle())
                    except Exception:  # 源暂不可用时保留原 TTL 和次数
                        pass
                if bounty_round is None:
                    continue
                key, end = bounty_round
                if it["key"] != f"bounty-{key}":
                    self._discard_pending(it)
                    continue
                it["expires"] = end
            if it["kind"] == "裂隙" and "expires" not in it:
                platforms = {s.platform for s in subs_u}
                if len(platforms) != 1:
                    continue
                platform = next(iter(platforms))
                if platform not in legacy_fissures:
                    try:
                        snapshot = await self.client.fissures(platform)
                        if not isinstance(snapshot, list) or any(
                            not isinstance(f, dict)
                            or not isinstance(f.get("id"), str)
                            or not f["id"]
                            for f in snapshot
                        ):
                            raise ValueError("裂隙列表结构不合法")
                        legacy_fissures[platform] = snapshot
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        legacy_fissures[platform] = None
                snapshot = legacy_fissures[platform]
                if snapshot is None:
                    continue  # 源失败时保留原 created/tries/TTL。
                source = next((f for f in snapshot if f.get("id") == it["key"]), None)
                if source is None:
                    self._discard_pending(it)
                    continue
                expiry = source.get("expiry")
                end = parse_iso(expiry) if isinstance(expiry, str) else None
                if end is None:
                    continue
                it["expires"] = end.timestamp()
            with i18n.use_lang(subs_u[0].lang), display_time.use_zone(self.display_timezone):
                res = await self._deliver(
                    it["umo"], it["kind"], it["key"], it["text"], subs_u, now, None, retry=it
                )
            if res == "skip":
                self._discard_pending(it)

    # ------------------------------------------------------------------
    async def _snapshot_diff(
        self, platform: str, subs: list[Subscription], baseline_key: Optional[str] = None
    ) -> list[tuple[str, str, str]]:
        """拉取快照并产出 (事件类型, 去重键, 推送文本) 列表。

        每种事件类型的取数**各自 try/except**：一个源失败只丢该类型本轮，
        不拖累同一轮里排在它后面的其它类型（旧实现一个 try 包到底，
        裂隙源挂了夜灵 / 入侵 / 新闻全部饿死）。
        基线按「平台 × 事件类型」独立：某类型本轮无人订阅 ⇒ 丢弃其基线，
        之后再有人订阅的那一轮只建基线不推（不灌存量）。
        """
        wanted = {s.event for s in subs if s.platform == platform}
        out: list[tuple[str, str, str]] = []
        matched = self._matched = {}  # 事件key -> 命中的 sub.sid（仅筛选类事件）
        self._text_for = {}  # 事件key -> 按「该会话命中的订阅」现算文本（赏金合并用）
        self._event_meta = {}
        last = self._last.setdefault(baseline_key or platform, {})
        for _ev, _keys in BASELINE_KEYS.items():
            if _ev not in wanted:
                for _k in _keys:
                    last.pop(_k, None)

        def _cycle_state(data, allowed) -> str:
            # 先校验再产出事件；空/未知状态不能落进二选一文案的 else 分支误推并消费订阅。
            state = data.get("state") if isinstance(data, dict) else None
            if not isinstance(state, str) or state not in allowed:
                raise ValueError("周期状态缺失或无法识别")
            return state

        async def _ev_fissure() -> None:
            fissures = await self.client.fissures(platform)
            live = {
                f["id"]: f
                for f in fissures
                if f.get("id") and f.get("expiry") and parse_iso(f["expiry"]) is not None
            }
            prev_ids = set(last.get("fissure_ids", []))
            # 首次（该类型尚无基线）只建基线、不把存量当新事件
            for fid, f in live.items() if "fissure_ids" in last else []:
                if fid in prev_ids:
                    continue
                self._event_meta[("裂隙", fid)] = {"expires": parse_iso(f["expiry"]).timestamp()}
                out.append(
                    (
                        "裂隙",
                        fid,
                        i18n.L(
                            f"⚡ 新裂隙：[{tier_cn(f.get('tier', ''))}] "
                            f"{f.get('node', '?')} · {_fissure_type_cn(f)}"
                            + (" · 钢铁" if f.get("isHard") else "")
                            + (" · 九重天" if f.get("isStorm") else "")
                            + f" · 剩{countdown(f['expiry'])}",
                            f"⚡ New Fissure: [{tier_cn(f.get('tier', ''))}] "
                            f"{f.get('node', '?')} · {_fissure_type_cn(f)}"
                            + (" · Steel Path" if f.get("isHard") else "")
                            + (" · Void Storm" if f.get("isStorm") else "")
                            + f" · {_left_en(countdown(f['expiry']))}",
                        ),
                    )
                )
            # 只保留订阅规则命中的裂隙事件避免刷屏；命中的订阅 sid 一并
            # 记下，供 tick 派发时选中（而不是按存储顺序碰运气取第一条）。
            fissure_subs = [s for s in subs if s.platform == platform and s.event == "裂隙"]
            kept = []
            for e in out:
                if e[0] != "裂隙":
                    kept.append(e)
                    continue
                hit = {s.sid for s in fissure_subs if self.filter_for(s).match(live.get(e[1], {}))}
                if hit:
                    kept.append(e)
                    matched[e[1]] = hit
            out[:] = kept
            self._set_baseline(last, "fissure_ids", list(live.keys()), "ids")

        async def _ev_cetus() -> None:
            cetus = await self.client.cycle(platform, "cetus")
            state = _cycle_state(cetus, ("day", "night"))
            if last.get("cetus_state") and last["cetus_state"] != state:
                if state == "night":
                    out.append(
                        (
                            "夜灵",
                            f"night-{cetus.get('expiry', '')}",
                            i18n.L(
                                f"🌙 夜灵平野已入夜，剩余 {countdown(cetus.get('expiry', ''))}，三傻走起",
                                "🌙 Night has fallen on the Plains of Eidolon, "
                                f"{_left_en(countdown(cetus.get('expiry', '')))}. Time to hunt the Eidolons",
                            ),
                        )
                    )
                elif state == "day":
                    out.append(
                        (
                            "夜灵",
                            f"day-{cetus.get('expiry', '')}",
                            i18n.L("☀️ 夜灵平野天亮了", "☀️ Day has broken on the Plains of Eidolon"),
                        )
                    )
            self._set_baseline(last, "cetus_state", state)

        async def _ev_vallis() -> None:
            vallis = await self.client.cycle(platform, "vallis")
            st = _cycle_state(vallis, ("warm", "cold"))
            if last.get("vallis_state") and last["vallis_state"] != st:
                label = (
                    i18n.L("温暖（可采矿）", "Warm (good for mining)")
                    if st == "warm"
                    else i18n.L("寒冷（热美亚）", "Cold (Thermia Fractures)")
                )
                out.append(
                    (
                        "山谷",
                        f"vallis-{st}-{vallis.get('expiry', '')}",
                        i18n.L(
                            f"🌡️ 奥布山谷转为{label}，剩余 {countdown(vallis.get('expiry', ''))}",
                            f"🌡️ Orb Vallis turned {label}, "
                            f"{_left_en(countdown(vallis.get('expiry', '')))}",
                        ),
                    )
                )
            self._set_baseline(last, "vallis_state", st)

        async def _ev_cambion() -> None:
            cambion = await self.client.cycle(platform, "cambion")
            st = _cycle_state(cambion, ("fass", "vome"))
            if last.get("cambion_state") and last["cambion_state"] != st:
                label = "Fass" if st == "fass" else "Vome"
                out.append(
                    (
                        "魔胎",
                        f"cambion-{st}-{cambion.get('expiry', '')}",
                        i18n.L(
                            f"🦠 魔胎之境已切换到 {label}，剩余 {countdown(cambion.get('expiry', ''))}",
                            f"🦠 Cambion Drift switched to {label}, "
                            f"{_left_en(countdown(cambion.get('expiry', '')))}",
                        ),
                    )
                )
            self._set_baseline(last, "cambion_state", st)

        async def _ev_earth() -> None:
            earth = await self.client.cycle(platform, "earth")
            st = _cycle_state(earth, ("day", "night"))
            if last.get("earth_state") and last["earth_state"] != st:
                label = i18n.L("白天", "day") if st == "day" else i18n.L("夜晚", "night")
                out.append(
                    (
                        "地球",
                        f"earth-{st}-{earth.get('expiry', '')}",
                        i18n.L(
                            f"🌍 地球已进入{label}，剩余 {countdown(earth.get('expiry', ''))}",
                            f"🌍 Earth is now {label}, "
                            f"{_left_en(countdown(earth.get('expiry', '')))}",
                        ),
                    )
                )
            self._set_baseline(last, "earth_state", st)

        async def _ev_duviri() -> None:
            duv = await self.client.cycle(platform, "duviri")
            # DE 适配器使用首字母大写；外部世界状态源也兼容小写状态名。
            st = _cycle_state(duv, (*DUVIRI_STATES, *(s.lower() for s in DUVIRI_STATES)))
            if last.get("duviri_state") and last["duviri_state"] != st:
                cn = duv.get("stateCn") or st
                out.append(
                    (
                        "双衍",
                        f"duviri-{st}-{duv.get('expiry', '')}",
                        i18n.L(
                            f"🌀 双衍王境螺旋切换为「{cn}」（{st}），"
                            f"剩余 {countdown(duv.get('expiry', ''))}",
                            f"🌀 Duviri Spiral switched to {cn}, "
                            f"{_left_en(countdown(duv.get('expiry', '')))}",
                        ),
                    )
                )
            self._set_baseline(last, "duviri_state", st)

        async def _ev_goals() -> None:
            goals = await self.client.goals(platform)
            live_ids = {g.get("tag") or g.get("name") for g in goals if not g.get("ended")}
            prev = set(last.get("goal_ids", []))
            for gid in (live_ids - prev) if "goal_ids" in last else set():
                g = next((x for x in goals if (x.get("tag") or x.get("name")) == gid), {})
                out.append(
                    (
                        "活动",
                        f"goal-{gid}",
                        i18n.L(
                            f"🎯 新活动：{g.get('name', gid)}"
                            + (f"｜剩{g.get('timeLeft')}" if g.get("timeLeft") else ""),
                            f"🎯 New event: {g.get('name', gid)}"
                            + (f" · {g.get('timeLeft')} left" if g.get("timeLeft") else ""),
                        ),
                    )
                )
            self._set_baseline(last, "goal_ids", list(live_ids), "ids")

        async def _ev_trader() -> None:
            trader = await self.client.void_trader(platform)
            active = bool(trader.get("active"))
            # 空响应（源失败）既不比对也不写基线：否则 True→{} 会被当成「奸商已离开」误推
            if trader and last.get("trader_active") is not None and last["trader_active"] != active:
                if active:
                    out.append(
                        (
                            "奸商",
                            f"in-{trader.get('activation', '')[:10]}",
                            i18n.L(
                                f"🛒 奸商已抵达 {trader.get('location', '?')}，"
                                f"{countdown(trader.get('expiry', ''))} 后离开",
                                f"🛒 Baro Ki'Teer has arrived at {trader.get('location', '?')}, "
                                f"leaving in {countdown(trader.get('expiry', ''))}",
                            ),
                        )
                    )
                else:
                    out.append(
                        (
                            "奸商",
                            f"out-{trader.get('expiry', '')[:10]}",
                            i18n.L(
                                "🛒 奸商已离开，下次再见",
                                "🛒 Baro Ki'Teer has left, see you next time",
                            ),
                        )
                    )
            if trader:  # 空响应（源失败）不写基线
                self._set_baseline(last, "trader_active", active, "bool")
            else:
                self.log.warning("[warframe] trader_active 响应为空，保留旧基线不更新")

        async def _ev_sortie() -> None:
            sortie = await self.client.sortie(platform)
            sid = sortie.get("id")
            # 空响应（id 为空）不推、不写基线（_set_baseline 的有效性守卫）
            if sid and last.get("sortie_id") and last["sortie_id"] != sid:
                out.append(
                    (
                        "突击",
                        f"sortie-{sid}",
                        i18n.L(
                            "⚔️ 每日突击已刷新，发送「突击」查看详情",
                            '⚔️ Daily Sortie has refreshed, send "sortie" for details',
                        ),
                    )
                )
            self._set_baseline(last, "sortie_id", sid)

        async def _ev_archon() -> None:
            archon = await self.client.archon_hunt(platform)
            aid = archon.get("id")
            if aid and last.get("archon_id") and last["archon_id"] != aid:
                out.append(
                    (
                        "执刑官",
                        f"archon-{aid}",
                        i18n.L(
                            "👑 本周执刑官猎杀已刷新，发送「执刑官」查看",
                            '👑 This week\'s Archon Hunt has refreshed, send "archon" to view',
                        ),
                    )
                )
            self._set_baseline(last, "archon_id", aid)

        async def _ev_arbi() -> None:
            # ★ 2026-09-19 修：原先调 client.arbitration()（10o.io 停摆后**恒抛**），
            #   又被 except 静默吞掉 → 「蹲 仲裁」永远不推。现改用与「仲裁」指令
            #   同一套 arbi.wf.wiki 推算（core.arbi），并**真正应用订阅筛选**
            #   （高效 = S/A+/A、传奇 = S、任务类型），筛选词以前是被忽略的。
            try:
                sl = await arbi.current(self.client)
                self._arbi_expired_logged = False
            except arbi.ScheduleExpired as exc:
                # ★ 2026-10-16（第十五批 A3）：排期表到期此前被下面的 except 静默吞掉 ⇒「蹲 仲裁」悄悄停推、
                #   日志无痕。单独记一次 WARNING（同一次过期只记一次，不每 45 秒刷屏）。
                if not getattr(self, "_arbi_expired_logged", False):
                    self.log.warning("[warframe] 仲裁排期表已到期，「蹲 仲裁」暂停推送：%s", exc)
                    self._arbi_expired_logged = True
                sl = None
            except Exception:  # noqa: BLE001 - 排期源不可用就跳过本轮
                sl = None
            if sl:
                prev_key = last.get("arbi_slot")
                if prev_key and prev_key != sl["key"]:
                    subs_a = [s for s in subs if s.platform == platform and s.event == "仲裁"]
                    hit = {s.sid for s in subs_a if arbi.match_rule(s.rule, sl)}
                    if not subs_a or hit:
                        tier = i18n.L(
                            f" · 评级 {sl['tier']}" if sl["tier"] else "",
                            f" · Rating {sl['tier']}" if sl["tier"] else "",
                        )
                        if sl["tier"] and sl.get("low_sample"):
                            tier += i18n.L("（样本少）", " (low sample)")
                        left_min = max(0, int((sl["end"] - time.time()) // 60))
                        akey = f"arbi-{sl['key']}-{int(sl['start'])}"
                        line_en = await self._arbi_line_en(sl) if i18n.is_en() else ""
                        out.append(
                            (
                                "仲裁",
                                akey,
                                i18n.L(
                                    f"⚖️ 仲裁已轮换：{sl['line']}{tier} · 剩 {left_min} 分钟"
                                    # 有评级时附评分范围（2026-10-16 维护者要求：凡出现评分都标范围）
                                    + (f"\n※ 评分范围 {arbi.TIER_RANGE}" if sl["tier"] else ""),
                                    f"⚖️ Arbitration rotated: {line_en}{tier} · {left_min}m left"
                                    + (
                                        f"\n※ Score range {_ARBI_TIER_RANGE_EN}"
                                        if sl["tier"]
                                        else ""
                                    ),
                                ),
                            )
                        )
                        matched[akey] = hit
                self._set_baseline(last, "arbi_slot", sl.get("key") or "")

        async def _ev_sp() -> None:
            # ★ 2026-09-19 修：原用 client.steel_path()（DE 精简版 worldState
            #   不下发该表 → 恒抛 → except 静默吞掉 → 「蹲 钢路侵袭」永不推）。
            #   改用社区排期表（browse.wf/sp-incursions.txt，「侵袭」指令同源）。
            try:
                inc = await self.client.steel_path_incursions(platform)
            except Exception:  # noqa: BLE001
                inc = {}
            nodes_today = list(inc.get("nodes") or [])
            if nodes_today:
                sig = ",".join(sorted(nodes_today))
                if last.get("sp_incursions") and last["sp_incursions"] != sig:
                    names = i18n.L("、", ", ").join(nodes_today[:6])
                    out.append(
                        (
                            "钢路侵袭",
                            f"sp-{sig[:60]}",
                            i18n.L(
                                f"🗡️ 钢铁之路侵袭已刷新（{len(nodes_today)} 个节点）：{names}",
                                f"🗡️ Steel Path Incursions refreshed ({len(nodes_today)} nodes): {names}",
                            ),
                        )
                    )
                last["sp_incursions"] = sig

        async def _ev_steel() -> None:
            # ★ A2（2026-10-03）：每周一 00:00 UTC 轮换（与执刑官猎杀同步）。
            #   数据与推算**同源** core/data/de/steel_shop.json::rotation
            #   （formatters.steel_rotation_index，勿另写第二份推算）。
            try:
                _ss = steel_shop() or {}
                idx, _nxt = steel_rotation_index(_ss)
                weekly = _ss.get("weekly") or []
            except Exception:  # noqa: BLE001 - 数据缺失降级不报错
                idx, weekly = 0, []
            if weekly:
                key = f"steel-rot-{idx}"
                if last.get("steel_rotation") is not None and last["steel_rotation"] != key:
                    cur = weekly[idx]
                    nxt = weekly[(idx + 1) % len(weekly)]
                    out.append(
                        (
                            "钢精兑换",
                            key,
                            i18n.L(
                                f"🪙 钢精兑换已轮换：本周 {cur['name']}"
                                f"（{cur['cost']} 精华）"
                                f" · 下周 {nxt['name']}",
                                f"🪙 Steel Essence shop rotated: this week {_shop_name(cur['name'])}"
                                f" ({cur['cost']} Essence)"
                                f" · next week {_shop_name(nxt['name'])}",
                            ),
                        )
                    )
                last["steel_rotation"] = key

        async def _ev_sliver() -> None:
            # ★ A3（2026-10-03）：Palladino 商店**无轮换库存**，事件语义 =
            #   每周限购重置（周一 00:00 UTC）。推算同源 palladino_shop.json
            #   （formatters.weekly_reset_info）。
            try:
                _n, _ = weekly_reset_info(palladino_shop() or {})
            except Exception:  # noqa: BLE001 - 数据缺失降级不报错
                _n = 0
            key = f"sliver-wk-{_n}"
            if last.get("sliver_reset") is not None and last["sliver_reset"] != key:
                out.append(
                    (
                        "碎银兑换",
                        key,
                        i18n.L(
                            "🪙 碎银兑换已重置（Palladino · 钢铁守望）："
                            "本周限购恢复 —— 可再购安魂遗物 / 裂罅 Mod / "
                            "安魂通牒等",
                            "🪙 Riven Sliver shop reset (Palladino · Iron Wake): "
                            "weekly purchase limits restored - Requiem Relics / Riven Mods / "
                            "Requiem Ultimatums and more are available again",
                        ),
                    )
                )
            last["sliver_reset"] = key

        async def _ev_bounty() -> None:
            # ★ C2（2026-10-03）：oracle 三地区（扎里曼/实验室/1999）的赏金
            #   轮换。每条赏金一个事件（key 带本轮 expiry ⇒ 换轮才重推，
            #   notified 去重兜底）；首轮只建基线不推（与裂隙同口径）。
            _cyc = await self.client.bounty_cycle()
            _exp, end = _bounty_round(_cyc)
            _bs = _cyc.get("bounties")
            if not isinstance(_bs, dict) or any(
                not isinstance(group, list) or any(not isinstance(row, dict) for row in group)
                for group in _bs.values()
            ):
                raise ValueError("赏金轮换表结构异常")
            _bounty_subs = [s for s in subs if s.platform == platform and s.event == "赏金"]
            # 基线：首轮只记 bounty_exp（不推）；**换轮**（expiry 变了）那一轮才发事件，
            # 与其它类型「事件只在发生那一刻产出、窗口外丢弃」一致 —— 旧写法每轮都对当前
            # 全部赏金产出事件，窗口外的订阅进入窗口后会把「当前轮」补推一遍。
            _prev_exp = last.get("bounty_exp")
            if _prev_exp is not None and _prev_exp != _exp:
                _node_tbl = de_zh("nodes_zh.json")
                _ch_tbl = de_zh("challenges_zh.json")
                _lines: list[tuple[set, str]] = []  # (命中的 sub.sid 集合, 一条赏金文本)
                for _pool, _tag, _title in ORACLE_REGIONS:
                    for _b in _bs.get(_tag) or []:
                        _nk = _b.get("node") or ""
                        _ck = _b.get("challenge") or ""
                        _node = (_node_tbl.get(_nk) or {}).get("name") or _nk
                        _ch = _ch_tbl.get(_ck) or {}
                        _cname = _ch.get("name") or ""
                        _ctype = _bounty_type_label(_ck)
                        # 规则词存的是中文内部写法：匹配始终用中文挑战名，展示才跟当前语言
                        if i18n.is_en():
                            with i18n.use_lang("zh"):
                                _cname_m = (de_zh("challenges_zh.json").get(_ck) or {}).get(
                                    "name"
                                ) or ""
                        else:
                            _cname_m = _cname
                        hit = {
                            s.sid
                            for s in _bounty_subs
                            if bounty_rule_hit(s.rule, _tag, _cname_m, _ck)
                        }
                        if hit:
                            _desc = (_ch.get("desc") or "").strip()
                            if i18n.is_en():  # 英文描述带 |COUNT| 之类模板记号，清掉
                                _desc = _clean_challenge_desc(_desc, _ch.get("count"))
                            _seg = " · ".join(
                                x
                                for x in (
                                    _synd_title(_pool) if i18n.is_en() else _title,
                                    _node,
                                    _ctype,
                                    _cname,
                                )
                                if x
                            )
                            _lines.append(
                                (
                                    hit,
                                    _seg + (i18n.L(f"（{_desc}）", f" ({_desc})") if _desc else ""),
                                )
                            )
                if _lines:
                    key = f"bounty-{_exp}"
                    union = set().union(*(h for h, _t in _lines))
                    matched[key] = union
                    parts = [{"sids": sorted(hit), "text": text} for hit, text in _lines]
                    self._event_meta[("赏金", key)] = {"parts": parts, "expires": end}
                    self._text_for[key] = lambda sids, parts=parts: _bounty_parts_text(parts, sids)
                    out.append(("赏金", key, _bounty_parts_text(parts, union)))
            last["bounty_exp"] = _exp

        # ★ C4（2026-10-03）：灵化 / 信条 / 终幕 换轮点检测。窗口序号
        #   同源 formatters.rotation_window（与卡面同一套锚点算法）；
        #   跨点才推，文案给出新一批内容。
        _rot_push = (
            ("灵化武器", "incarnon", i18n.L("🔮 灵化轮换", "🔮 Incarnon Rotation")),
            ("信条", "tenet", i18n.L("⚔ 信条库存刷新", "⚔ Tenet Stock Refresh")),
            ("终幕", "coda", i18n.L("🪲 终幕轮换", "🪲 Coda Rotation")),
        )

        async def _ev_rotations() -> None:
            try:
                _rot_all = json.loads(paths.read_path("rotations.json").read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001 - 数据缺失降级不报错
                _rot_all = {}
            for _ev, _rk, _icon in _rot_push:
                if _ev not in wanted:
                    continue
                _rd = _rot_all.get(_rk) or {}
                if not _rd:
                    continue
                _passed, _ = rotation_window(_rd)
                _weeks = _rd.get("weeks") or []
                _batches = _rd.get("batches") or []
                if _weeks:  # weekly_cycle（灵化）
                    _pos = (int(_rd.get("anchor_week", 1)) - 1 + _passed) % len(_weeks)
                    _items = _weeks[_pos]
                    _label = i18n.L(
                        f"第 {_pos + 1}/{len(_weeks)} 周", f"Week {_pos + 1}/{len(_weeks)}"
                    )
                elif _batches:  # batch_cycle（终幕）
                    _pos = (int(_rd.get("anchor_idx", 0)) + _passed) % len(_batches)
                    _items = _batches[_pos]
                    _labels = _rd.get("batch_label") or []
                    _label = (
                        i18n.L(f"{_labels[_pos]} 批", f"Batch {_labels[_pos]}")
                        if _pos < len(_labels)
                        else i18n.L(f"第 {_pos + 1} 批", f"Batch {_pos + 1}")
                    )
                else:  # refresh_only（信条）
                    _items = _rd.get("items") or []
                    _label = i18n.L("库存", "Current")
                if not _items:
                    continue
                _key = f"rot-{_rk}-{_passed}"
                _prev = last.get(f"{_rk}_window")
                if _prev is not None and _prev != _key:
                    _names = i18n.L("、", ", ").join(
                        (
                            (it.get("en") or it.get("cn") or "")
                            if i18n.is_en()
                            else (it.get("cn") or it.get("en") or "")
                        )
                        + (
                            i18n.L(
                                f"（{it.get('element')} {it.get('bonus')}%）",
                                f" ({str(it.get('element')).title()} {it.get('bonus')}%)",
                            )
                            if it.get("element") and it.get("bonus") is not None
                            else i18n.L("（效价待获取）", " (valence pending)")
                            if _rk in ("tenet", "coda")
                            else ""
                        )
                        for it in _items[:8]
                    )
                    _note = ""
                    if _rk in ("tenet", "coda"):
                        from datetime import timedelta
                        from .commands.progress import ProgressCommands

                        _epoch = parse_iso(_rd.get("epoch"))
                        if _epoch is not None:
                            _start = _epoch + timedelta(
                                hours=int(_rd.get("period_hours", 96)) * _passed
                            )
                            _note = "\n" + ProgressCommands._valence_note(_rd, _start)
                    out.append(
                        (
                            _ev,
                            _key,
                            i18n.L(
                                f"{_icon}（{_label}）：{_names}{_note}",
                                f"{_icon} ({_label}): {_names}{_note}",
                            ),
                        )
                    )
                last[f"{_rk}_window"] = _key

        # ★ C5（2026-10-03）：阿耶兑换 / 1999 日历 / 电波。
        async def _ev_vault() -> None:
            try:
                _pv = await self.client.prime_vault(platform)
            except Exception:  # noqa: BLE001 - 降级不报错
                _pv = {}
            if isinstance(_pv, dict) and _pv.get("expiry"):
                _pk = f"pv-{_pv.get('expiry')}"
                if last.get("pv_key") is not None and last["pv_key"] != _pk:
                    _names = i18n.L("、", ", ").join(
                        (it.get("name") or "") for it in (_pv.get("items") or [])[:6]
                    )
                    out.append(
                        (
                            "阿耶兑换",
                            _pk,
                            i18n.L(
                                f"💠 Prime 宝库轮换：{_names}",
                                f"💠 Prime Resurgence rotation: {_names}",
                            ),
                        )
                    )
                last["pv_key"] = _pk

        async def _ev_calendar() -> None:
            try:
                _cal = await self.client.calendar(platform)
            except Exception:  # noqa: BLE001
                _cal = {}
            if isinstance(_cal, dict) and _cal.get("days"):
                _sk = f"cal-season-{_cal.get('season')}-{_cal.get('yearIteration')}"
                if last.get("cal_season") is not None and last["cal_season"] != _sk:
                    season = calendar_season_name(str(_cal.get("season") or ""))
                    out.append(
                        (
                            "1999日历",
                            _sk,
                            i18n.L(
                                f"🗓 1999 日历轮换：{season}季（第 {_cal.get('yearIteration')} 年）",
                                f"🗓 1999 Calendar rotated: {season}"
                                f" (Year {_cal.get('yearIteration')})",
                            ),
                        )
                    )
                last["cal_season"] = _sk
                # 兼容曾下发公历日期的自定义源。游戏内日程用 1999 日期，玩家按顺序完成。
                _today = utc_today()
                _day = next((d for d in _cal["days"] if d.get("date") == _today), None)
                if _day:
                    # 键含**日程签名**：同日日程被 DE 更新也推（日切自然推）
                    _sig_d = ",".join(
                        sorted(e.get("name") or "" for e in (_day.get("events") or []))
                    )[:40]
                    _dk = f"cal-{_today}-{_sig_d}"
                    if last.get("cal_day") is not None and last["cal_day"] != _dk:
                        _evs = [e.get("name") for e in (_day.get("events") or []) if e.get("name")]
                        out.append(
                            (
                                "1999日历",
                                _dk,
                                i18n.L(
                                    f"🗓 1999 今日日程（{_today}）："
                                    + ("、".join(_evs[:6]) or "无条目"),
                                    f"🗓 1999 schedule for today ({_today}): "
                                    + (", ".join(_evs[:6]) or "no entries"),
                                ),
                            )
                        )
                    last["cal_day"] = _dk

        async def _ev_nightwave() -> None:
            try:
                _nw = await self.client.nightwave(platform)
            except Exception:  # noqa: BLE001
                _nw = {}
            _chs = (_nw or {}).get("activeChallenges") or []
            if _chs:
                _ids = {c.get("id") for c in _chs if c.get("id")}
                _prev_ids = set(last.get("nw_ids", []))
                _fresh = _ids - _prev_ids
                if last.get("nw_ids") is not None and _fresh:
                    _new = [c for c in _chs if c.get("id") in _fresh]
                    _names = i18n.L("、", ", ").join(
                        (c.get("title") or "?")
                        + (
                            i18n.L("（每日）", " (Daily)")
                            if c.get("isDaily")
                            else i18n.L("（精英周常）", " (Elite Weekly)")
                            if c.get("isElite")
                            else i18n.L("（周常）", " (Weekly)")
                        )
                        for c in _new[:5]
                    )
                    _sig = ",".join(sorted(_fresh))[:60]
                    out.append(
                        (
                            "电波",
                            f"nw-{_sig}",
                            i18n.L(
                                f"📻 午夜电波新挑战（{len(_new)} 条）：{_names}",
                                f"📻 New Nightwave challenges ({len(_new)}): {_names}",
                            ),
                        )
                    )
                last["nw_ids"] = list(_ids)

        async def _ev_alerts() -> None:
            alerts = await self.client.alerts(platform)
            ids = {a.get("id") for a in alerts if a.get("id") and a.get("active", True)}
            prev = set(last.get("alert_ids", []))
            for aid in (ids - prev) if "alert_ids" in last else ():
                a = next((x for x in alerts if x.get("id") == aid), {})
                mission = a.get("mission", {}) or {}
                reward = mission.get("reward", {}) or {}
                # ★ C3：解析已补全（节点/类型/等级/奖励中文名），推送照实给
                names = reward.get("item_names") or ([reward["item"]] if reward.get("item") else [])
                lv = (
                    i18n.L(
                        f"{mission['min_level']}-{mission['max_level']}级 ",
                        f"Lv {mission['min_level']}-{mission['max_level']} ",
                    )
                    if mission.get("min_level")
                    else ""
                )
                rw = i18n.L("、", ", ").join(
                    [
                        *names,
                        *(
                            [i18n.L(f"{reward['credits']}现金", f"{reward['credits']} Credits")]
                            if reward.get("credits")
                            else []
                        ),
                    ]
                )
                head = (
                    i18n.L(f"{mission.get('desc')}｜", f"{mission.get('desc')} | ")
                    if mission.get("desc")
                    else ""
                )
                out.append(
                    (
                        "警报",
                        aid,
                        i18n.L(
                            f"🔔 新警报：{head}{mission.get('node', '?')} · "
                            f"{mission_cn(mission.get('type', ''))} {lv}"
                            f"奖励：{rw or '?'}",
                            f"🔔 New Alert: {head}{mission.get('node', '?')} · "
                            f"{mission_cn(mission.get('type', ''))} {lv}"
                            f"Reward: {rw or '?'}",
                        ),
                    )
                )
            last["alert_ids"] = list(ids)

        async def _ev_invasions() -> None:
            invs = await self.client.invasions(platform)
            ids = {i.get("id") for i in invs if i.get("id") and not i.get("completed")}
            prev = set(last.get("invasion_ids", []))
            for iid in (ids - prev) if "invasion_ids" in last else ():
                inv = next((x for x in invs if x.get("id") == iid), {})
                out.append(
                    (
                        "入侵",
                        iid,
                        i18n.L(
                            f"⚔️ 新入侵：{inv.get('node', '?')}（"
                            f"{(inv.get('attacker', {}) or {}).get('faction', '?')} vs "
                            f"{(inv.get('defender', {}) or {}).get('faction', '?')}）",
                            f"⚔️ New Invasion: {inv.get('node', '?')} ("
                            f"{(inv.get('attacker', {}) or {}).get('faction', '?')} vs "
                            f"{(inv.get('defender', {}) or {}).get('faction', '?')})",
                        ),
                    )
                )
            self._set_baseline(last, "invasion_ids", list(ids), "ids")

        async def _ev_news() -> None:
            news = await self.client.news(platform)
            ids = {n.get("id") for n in news if n.get("id")}
            prev = set(last.get("news_ids", []))
            for nid in (ids - prev) if "news_ids" in last else ():
                n = next((x for x in news if x.get("id") == nid), {})
                msg = (n.get("message") or n.get("title") or "").strip()
                if msg:
                    out.append(
                        (
                            "新闻",
                            nid,
                            f"📰 {msg}" + (f"\n{n.get('link')}" if n.get("link") else ""),
                        )
                    )
            self._set_baseline(last, "news_ids", list(ids), "ids")

        async def _ev_deals() -> None:
            deals = await self.client.daily_deals(platform)
            # 空列表 = 源失败 / 降级：不推、不写基线（旧实现会推出只有一个标题的空消息）
            if not deals:
                self.log.warning("[warframe] deal_key 响应为空，保留旧基线不更新")
                return
            lead = deals[0]
            dkey = lead.get("id") or f"{lead.get('item', '')}"
            if dkey and last.get("deal_key") and last["deal_key"] != dkey:
                lines = [
                    i18n.L(
                        f"{d.get('item', '?')}：{d.get('salePrice', '?')}p （库存{d.get('total', '?')}）",
                        f"{d.get('item', '?')}: {d.get('salePrice', '?')}p ({d.get('total', '?')} in stock)",
                    )
                    for d in deals
                ]
                out.append(
                    (
                        "每日特惠",
                        f"deal-{dkey}",
                        i18n.L("🏷️ 达沃每日特惠：\n", "🏷️ Darvo's Daily Deal:\n") + "\n".join(lines),
                    )
                )
            self._set_baseline(last, "deal_key", dkey)

        handlers = (
            (("裂隙",), _ev_fissure),
            (("夜灵",), _ev_cetus),
            (("山谷",), _ev_vallis),
            (("魔胎",), _ev_cambion),
            (("地球",), _ev_earth),
            (("双衍",), _ev_duviri),
            (("活动",), _ev_goals),
            (("奸商",), _ev_trader),
            (("突击",), _ev_sortie),
            (("执刑官",), _ev_archon),
            (("仲裁",), _ev_arbi),
            (("钢路侵袭",), _ev_sp),
            (("钢精兑换",), _ev_steel),
            (("碎银兑换",), _ev_sliver),
            (("赏金",), _ev_bounty),
            (("灵化武器", "信条", "终幕"), _ev_rotations),
            (("阿耶兑换",), _ev_vault),
            (("1999日历",), _ev_calendar),
            (("电波",), _ev_nightwave),
            (("警报",), _ev_alerts),
            (("入侵",), _ev_invasions),
            (("新闻",), _ev_news),
            (("每日特惠",), _ev_deals),
        )
        for _evs, _fn in handlers:
            if not any(e in wanted for e in _evs):
                continue
            n_before = len(out)
            try:
                await _fn()
                self._tick_ok += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 单类型失败不拖累其它类型
                del out[n_before:]  # 半截产物不要（基线也未推进到失败点之后）
                self._tick_fail += 1
                self.log.warning("[warframe] %s %s 快照拉取失败：%s", platform, "/".join(_evs), exc)
        return out
