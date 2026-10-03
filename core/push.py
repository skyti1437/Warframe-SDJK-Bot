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
import time
from typing import Awaitable, Callable, Optional

import json

from . import paths
from .api_client import WarframeAPIError, WarframeClient
from .formatters import (ORACLE_REGIONS, countdown, de_zh, mission_cn,
                         palladino_shop, parse_iso, rotation_window,
                         steel_rotation_index, steel_shop, tier_cn, utc_today,
                         weekly_reset_info)
from .parser import FissureFilter, parse_fissure_filter
from . import arbi
from .store import Subscription, SubscriptionStore

SendFunc = Callable[[str, str, Optional[list]], Awaitable[None]]


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
    ("Exterminate", "歼灭"), ("Survival", "生存"), ("MobDef", "移动防御"),
    ("Assassinate", "刺杀"), ("Cascade", "级联"), ("Flood", "洪流"),
    ("Corruption", "腐化"), ("DefeatVoidAngel", "天使"), ("Alchemy", "炼金"),
    ("Defense", "防御"), ("Defend", "防御"), ("Excavation", "挖掘"),
    ("Capture", "捕获"), ("Sabotage", "破坏"), ("Rescue", "救援"),
    ("Spy", "间谍"), ("Disruption", "中断"), ("Void", "虚空"),
)


def bounty_type_of(challenge_path: str) -> str:
    """challenge 路径 → 任务类型（无命中返回空串）。"""
    p = challenge_path or ""
    for token, zh in _BOUNTY_TYPE_HINTS:
        if token in p:
            return zh
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
        words.append(tok)          # 非地区词：按「挑战名/任务类型」词处理
    return {"tags": tags, "words": words}


def bounty_rule_hit(rule: str, tag: str, challenge_name: str,
                    challenge_path: str) -> bool:
    """规则是否命中一条赏金（地区 + 挑战名/类型词，全部条件须满足）。"""
    r = parse_bounty_rule(rule)
    if r["tags"] and tag not in r["tags"]:
        return False
    if not r["words"]:
        return True                       # 只写地区（或空规则）：该地区全收
    t = bounty_type_of(challenge_path)
    for w in r["words"]:
        if w in (challenge_name or "") or (t and (w == t or w in t)):
            continue
        return False
    return True


def at_targets(platform_name: Optional[str], ids) -> list[str]:
    """推送 @ 目标过滤（A1，2026-10-03 用户批准的特例）。

    仅 **aiocqhttp（QQ）** 支持 At；其余平台/解析不到平台名一律返回空表
    ⇒ 调用方回落纯文本（不得因 @ 不支持而丢推送）。**绝不含 @全体**
    （`all` 一律剔除）；去重保序。
    """
    if not ids or (platform_name or "").lower() != "aiocqhttp":
        return []
    return list(dict.fromkeys(
        s for s in (str(x).strip() for x in ids)
        if s and s.lower() != "all"))

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
    "1999日历": ("1999 日历：季轮换 / 当日日程刷新", True),
    "灵化武器": ("本周灵化轮换（钢铁回廊 9 周循环，周一 00:00 UTC）", True),
    "信条": ("Ergo 信条武器库存与元素加成（每 96 小时）", True),
    "终幕": ("Coda 终幕武器批次轮换（A/B 两批每 96 小时）", True),
    "钢精兑换": ("Teshin 钢精兑换周轮换（每周一 00:00 UTC，8 件循环）", True),
    "碎银兑换": ("Palladino 裂罅碎块商店每周限购重置（周一 00:00 UTC）", True),
    "赏金": ("赏金轮换（扎里曼/实验室/1999；可筛 地区+任务，如 扎里曼 高效歼灭）",
             True),
    "阿耶兑换": ("Prime 宝库轮换（Regal Aya 兑换包）", True),
    "电波": ("午夜电波每日/每周挑战刷新", True),
}

PUSH_ALIAS = {"钢铁裂隙": "裂隙", "虚空裂隙": "裂隙", "执刑官猎杀": "执刑官",
              "日历": "1999日历", "夜": "夜灵",
              "平原时间": "夜灵", "夜灵平野": "夜灵",
              "奥布山谷": "山谷", "金星": "山谷", "山谷温度": "山谷",
              "魔胎之境": "魔胎", "火卫二": "魔胎",
              "地球昼夜": "地球", "白天黑夜": "地球",
              "双衍王境": "双衍", "螺旋": "双衍", "情绪": "双衍"}


def normalize_event(word: str) -> Optional[str]:
    w = word.strip()
    if w in PUSH_EVENTS:
        return w
    return PUSH_ALIAS.get(w)


def build_cancel_selector(umo: str, heads: list[str]):
    """解析「蹲 …取消…」中「取消」以外的词 → (event, keys, exact, fuzzy, label)。

    2026-09-14 修「蹲 取消 裂隙 捕获 把全群订阅全删了」：旧版只看首词是不是
    「取消」，后面的筛选词被无视。现在「取消」位置无关，其余词构成筛选条件：
    · 第一个能识别为事件类型的词限定 event（如 裂隙 / 山谷）；
    · 其余词按筛选词匹配 rule —— 先要求**精确等于**（「捕获」不会误杀
      「虚空捕获」），一条没中再退回**包含**匹配兜底。
    heads 为空 = 取消本群全部。
    """
    ev = None
    keys: list[str] = []
    for h in heads:
        e = normalize_event(h)
        if e and ev is None:
            ev = e
        else:
            keys.append(h)

    def exact(s):
        if s.umo != umo:
            return False
        if ev and s.event != ev:
            return False
        if keys:
            rule = s.rule or ""
            if not any(k == rule for k in keys):
                return False
        return True

    def fuzzy(s):
        if not keys:
            return False
        if s.umo != umo:
            return False
        if ev and s.event != ev:
            return False
        rule = s.rule or ""
        return any(k in rule for k in keys)

    label = " ".join(heads) if heads else "全部"
    return ev, keys, exact, fuzzy, label


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
    except RuntimeError:        # 没有运行中的事件循环（离线测试/CLI）：退回登记表
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


class PushDaemon:
    def __init__(
        self,
        client: WarframeClient,
        store: SubscriptionStore,
        send: SendFunc,
        logger,
        interval: int = 45,
    ):
        self.client = client
        self.store = store
        self.send = send
        self.log = logger
        self.interval = max(15, interval)
        self._task: Optional[asyncio.Task] = None
        self._last: dict[str, dict] = {}   # platform -> 上次快照摘要
        self._filters: dict[str, FissureFilter] = {}  # sub.sid -> 裂隙筛选缓存
        self._matched: dict[str, set[str]] = {}  # 事件key -> 本轮筛选命中的 sub.sid

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
            self.log.warning("[warframe] %s 响应为空/骤降（%r），保留旧基线不更新（防跨轮重推）",
                             key, value)

    def start(self) -> None:
        """启动守护；**进程级**幂等 —— 先清掉任何仍活跃的旧 daemon（跨实例泄漏）。"""
        for dead in [x for x in _LIVE_DAEMONS if x.done()]:
            _LIVE_DAEMONS.discard(dead)          # 已结束的登记顺手清掉，防集合膨胀
        # ★ 2026-09-26 二次修复（线上实测：只记一条日志、群里却收到两条）：
        #   `_LIVE_DAEMONS` 只装**本版本**注册过的守护 —— 修复前泄漏的守护当年创建时
        #   还没有这张表，所以既看不见、也取消不掉，会一直静默推下去（旧代码没有派发日志，
        #   日志里查不到它）。改为**直接扫事件循环**：凡协程名是 `PushDaemon._run` 的任务
        #   都是本类的守护（协程限定名与代码版本、模块对象无关），逐个取消。
        leaked = [x for x in _LIVE_DAEMONS if not x.done() and x is not self._task]
        leaked += [t for t in _live_daemon_tasks() if t is not self._task
                   and t not in leaked and t not in _LIVE_DAEMONS]
        for task in leaked:
            task.cancel()                        # ★ 旧实例/旧版本泄漏的守护：取消，防重复推送
            _LIVE_DAEMONS.discard(task)
        if leaked:
            self.log.warning("[warframe] 发现 %d 个仍在运行的旧推送守护 → 已取消（防重复推送）",
                             len(leaked))
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="warframe-push-daemon")
            _LIVE_DAEMONS.add(self._task)
            self.log.info("[warframe] 推送守护协程已启动（间隔 %ss，实例 %s，活跃 %d）",
                          self.interval, id(self), len(_LIVE_DAEMONS))
        else:
            self.log.info("[warframe] 推送守护已在运行（重复 start 幂等跳过，实例 %s，活跃 %d）",
                          id(self), len(_LIVE_DAEMONS))

    async def stop(self) -> None:
        """停止本实例的守护并**注销登记**（幂等；失败也记日志）。"""
        task = self._task
        if task is None:
            self.log.info("[warframe] 推送守护未在运行（stop 幂等跳过，实例 %s）", id(self))
            return
        self.log.info("[warframe] 正在停止推送守护（实例 %s，task.done()=%s）",
                      id(self), task.done())
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass                                  # 正常取消路径
        except Exception as exc:                  # noqa: BLE001 - 停止失败要留痕
            self.log.warning("[warframe] 推送守护停止时异常：%s", exc)
        finally:
            _LIVE_DAEMONS.discard(task)
            self._task = None
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
                await asyncio.sleep(self.interval)
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
        now = time.time()
        if self.store.gc(now):
            await self.store.save()
        subs = [s for s in self.store.all() if not s.expired(now)]
        if not subs:
            self._filters.clear()
            return
        # 订阅删掉后筛选缓存也要跟着走，长期运行才不会慢慢漏内存
        live_sids = {s.sid for s in subs}
        for sid in [k for k in self._filters if k not in live_sids]:
            self._filters.pop(sid, None)
        platforms = sorted({s.platform for s in subs})
        for platform in platforms:
            events = await self._snapshot_diff(platform, subs)
            if not events:
                continue
            for kind, key, text in events:
                sent: set[str] = set()      # 同一事件对同一会话只推一次
                cands = [s for s in subs
                         if s.platform == platform and s.event == kind]
                # 同群常把同义筛选叠好几条（「蹲 裂隙 捕获」+「蹲 裂隙 虚空捕获」…）；
                # 派发必须选「自身筛选命中」的那条，否则会把没命中的订阅
                # （一次性）消费掉、或错套它的免打扰时间窗。
                prefer = ([s for s in cands
                           if s.sid in self._matched.get(key, set())]
                          or cands)
                for sub in prefer:
                    if sub.umo in sent:
                        continue
                    # ★ A1（2026-10-03 用户批准的特例）：同一条推送把「本事件
                    #   命中、且在同一会话」的订阅发起人收齐去重 ⇒ 一次性 @
                    #   全部当事人。跨事件/跨会话**绝不合并**；规则未命中的
                    #   订阅（不在 prefer 里）不得被 @。
                    at_ids = list(dict.fromkeys(
                        s.created_by for s in prefer
                        if s.umo == sub.umo and s.created_by))
                    # 真推出去才占掉这个会话的名额：前面那条被自己的免打扰窗
                    # 挡住时，后面全天候的订阅还能接住同一事件。
                    if await self._dispatch(sub, key, text, now, at=at_ids):
                        sent.add(sub.umo)

    async def _dispatch(self, sub: Subscription, key: str, text: str,
                        now: float, at: Optional[list] = None) -> bool:
        """尝试派发一条订阅；返回是否真的推送成功。"""
        if key in sub.notified:
            return False
        if not sub.time_window().allows(_local_now()):
            return False
        sub.notified[key] = now
        if len(sub.notified) > 200:
            for k in sorted(sub.notified, key=lambda k: sub.notified[k])[:100]:
                sub.notified.pop(k, None)
        try:
            await self.send(sub.umo, text, at=at)
        except Exception as exc:  # noqa: BLE001
            self.log.warning("[warframe] 推送失败（%s）：%s", sub.umo, exc)
            sub.notified.pop(key, None)
            return False
        if not sub.consume():
            await self.store.remove(lambda s: s.sid == sub.sid)
        else:
            # 运行期状态（notified 去重键 / hits_left 计数）在内存副本上改的，
            # 必须按 sid 原位落盘；旧写法 sync(self.store.all()) 会从 _data
            # 重新拷贝一遍，等于白写一次盘、什么都没存下。
            await self.store.update(sub)
        # ★ 成功派发也留痕（2026-09-26）：此前只有失败才记日志，无法从日志判断
        #   「同一事件被推了几条」。带上实例 id —— 若出现跨实例的重复推送，
        #   同一事件键会打印出**两个不同的实例 id**，一眼可见。
        self.log.info("[warframe] 已推送 事件=%s 键=%s → %s（实例 %s）",
                      sub.event, key, sub.umo, id(self))
        return True

    # ------------------------------------------------------------------
    async def _snapshot_diff(self, platform: str,
                             subs: list[Subscription]) -> list[tuple[str, str, str]]:
        """拉取快照并产出 (事件类型, 去重键, 推送文本) 列表。"""
        wanted = {s.event for s in subs if s.platform == platform}
        out: list[tuple[str, str, str]] = []
        matched = self._matched = {}   # 事件key -> 命中的 sub.sid（仅筛选类事件）
        last = self._last.setdefault(platform, {})
        first = not last  # 首轮只建立基线，不把存量内容当作新事件推送
        try:
            if "裂隙" in wanted:
                fissures = await self.client.fissures(platform)
                live = {f["id"]: f for f in fissures if f.get("id") and f.get("expiry")
                        and parse_iso(f["expiry"]) is not None}
                prev_ids = set(last.get("fissure_ids", []))
                for fid, f in (live.items() if not first else []):
                    if fid in prev_ids:
                        continue
                    out.append(("裂隙", fid,
                                f"⚡ 新裂隙：[{tier_cn(f.get('tier', ''))}] "
                                f"{f.get('node', '?')} · {mission_cn(f.get('missionType', ''))}"
                                + (" · 钢铁" if f.get("isHard") else "")
                                + (" · 九重天" if f.get("isStorm") else "")
                                + f" · 剩{countdown(f['expiry'])}"))
                # 只保留订阅规则命中的裂隙事件避免刷屏；命中的订阅 sid 一并
                # 记下，供 tick 派发时选中（而不是按存储顺序碰运气取第一条）。
                fissure_subs = [s for s in subs if s.platform == platform and s.event == "裂隙"]
                kept = []
                for e in out:
                    if e[0] != "裂隙":
                        kept.append(e)
                        continue
                    hit = {s.sid for s in fissure_subs
                           if self.filter_for(s).match(live.get(e[1], {}))}
                    if hit:
                        kept.append(e)
                        matched[e[1]] = hit
                out = kept
                self._set_baseline(last, "fissure_ids", list(live.keys()), "ids")

            if "夜灵" in wanted:
                cetus = await self.client.cycle(platform, "cetus")
                state = cetus.get("state")
                if last.get("cetus_state") and last["cetus_state"] != state:
                    if state == "night":
                        out.append(("夜灵", f"night-{cetus.get('expiry', '')}",
                                    f"🌙 夜灵平野已入夜，剩余 {countdown(cetus.get('expiry', ''))}，三傻走起"))
                    elif state == "day":
                        out.append(("夜灵", f"day-{cetus.get('expiry', '')}",
                                    "☀️ 夜灵平野天亮了"))
                self._set_baseline(last, "cetus_state", state)

            if "山谷" in wanted:
                vallis = await self.client.cycle(platform, "vallis")
                st = vallis.get("state")
                if last.get("vallis_state") and last["vallis_state"] != st:
                    label = "温暖（可采矿）" if st == "warm" else "寒冷（热美亚）"
                    out.append(("山谷", f"vallis-{st}-{vallis.get('expiry', '')}",
                                f"🌡️ 奥布山谷转为{label}，"
                                f"剩余 {countdown(vallis.get('expiry', ''))}"))
                self._set_baseline(last, "vallis_state", st)

            if "魔胎" in wanted:
                cambion = await self.client.cycle(platform, "cambion")
                st = cambion.get("state")
                if last.get("cambion_state") and last["cambion_state"] != st:
                    label = "Fass" if st == "fass" else "Vome"
                    out.append(("魔胎", f"cambion-{st}-{cambion.get('expiry', '')}",
                                f"🦠 魔胎之境已切换到 {label}，"
                                f"剩余 {countdown(cambion.get('expiry', ''))}"))
                self._set_baseline(last, "cambion_state", st)

            if "地球" in wanted:
                earth = await self.client.cycle(platform, "earth")
                st = earth.get("state")
                if last.get("earth_state") and last["earth_state"] != st:
                    label = "白天" if st == "day" else "夜晚"
                    out.append(("地球", f"earth-{st}-{earth.get('expiry', '')}",
                                f"🌍 地球已进入{label}，"
                                f"剩余 {countdown(earth.get('expiry', ''))}"))
                self._set_baseline(last, "earth_state", st)

            if "双衍" in wanted:
                duv = await self.client.cycle(platform, "duviri")
                st = duv.get("state")
                if last.get("duviri_state") and last["duviri_state"] != st:
                    cn = duv.get("stateCn") or st
                    out.append(("双衍", f"duviri-{st}-{duv.get('expiry', '')}",
                                f"🌀 双衍王境螺旋切换为「{cn}」（{st}），"
                                f"剩余 {countdown(duv.get('expiry', ''))}"))
                self._set_baseline(last, "duviri_state", st)

            if "活动" in wanted:
                goals = await self.client.goals(platform)
                live_ids = {g.get("tag") or g.get("name") for g in goals if not g.get("ended")}
                prev = set(last.get("goal_ids", []))
                for gid in (live_ids - prev) if not first else set():
                    g = next((x for x in goals
                              if (x.get("tag") or x.get("name")) == gid), {})
                    out.append(("活动", f"goal-{gid}",
                                f"🎯 新活动：{g.get('name', gid)}"
                                + (f"｜剩{g.get('timeLeft')}" if g.get("timeLeft") else "")))
                self._set_baseline(last, "goal_ids", list(live_ids), "ids")

            if "奸商" in wanted:
                trader = await self.client.void_trader(platform)
                active = bool(trader.get("active"))
                if last.get("trader_active") is not None and last["trader_active"] != active:
                    if active:
                        out.append(("奸商", f"in-{trader.get('activation', '')[:10]}",
                                    f"🛒 奸商已抵达 {trader.get('location', '?')}，"
                                    f"{countdown(trader.get('expiry', ''))} 后离开"))
                    else:
                        out.append(("奸商", f"out-{trader.get('expiry', '')[:10]}",
                                    "🛒 奸商已离开，下次再见"))
                if trader:                       # 空响应（源失败）不写基线
                    self._set_baseline(last, "trader_active", active, "bool")
                else:
                    self.log.warning("[warframe] trader_active 响应为空，保留旧基线不更新")

            if "突击" in wanted:
                sortie = await self.client.sortie(platform)
                sid = sortie.get("id")
                if last.get("sortie_id") and last["sortie_id"] != sid:
                    out.append(("突击", f"sortie-{sid}",
                                "⚔️ 每日突击已刷新，发送「突击」查看详情"))
                last["sortie_id"] = sid

            if "执刑官" in wanted:
                archon = await self.client.archon_hunt(platform)
                aid = archon.get("id")
                if last.get("archon_id") and last["archon_id"] != aid:
                    out.append(("执刑官", f"archon-{aid}",
                                "👑 本周执刑官猎杀已刷新，发送「执刑官」查看"))
                last["archon_id"] = aid

            if "仲裁" in wanted:
                # ★ 2026-09-19 修：原先调 client.arbitration()（10o.io 停摆后**恒抛**），
                #   又被 except 静默吞掉 → 「蹲 仲裁」永远不推。现改用与「仲裁」指令
                #   同一套 arbi.wf.wiki 推算（core.arbi），并**真正应用订阅筛选**
                #   （高效 = S/A+/A、传奇 = S、任务类型），筛选词以前是被忽略的。
                try:
                    sl = await arbi.current(self.client)
                except Exception:  # noqa: BLE001 - 排期源不可用就跳过本轮
                    sl = None
                if sl:
                    prev_key = last.get("arbi_slot")
                    if prev_key and prev_key != sl["key"]:
                        subs_a = [s for s in subs
                                  if s.platform == platform and s.event == "仲裁"]
                        hit = {s.sid for s in subs_a
                               if arbi.match_rule(s.rule, sl)}
                        if not subs_a or hit:
                            tier = f" · 评级 {sl['tier']}" if sl["tier"] else ""
                            left_min = max(0, int((sl["end"] - time.time()) // 60))
                            akey = f"arbi-{sl['key']}-{int(sl['start'])}"
                            out.append(("仲裁", akey,
                                        f"⚖️ 仲裁已轮换：{sl['line']}{tier}"
                                        f" · 剩 {left_min} 分钟"))
                            matched[akey] = hit
                    self._set_baseline(last, "arbi_slot", sl.get("key") or "")

            if "钢路侵袭" in wanted:
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
                        names = "、".join(nodes_today[:6])
                        out.append(("钢路侵袭", f"sp-{sig[:60]}",
                                    f"🗡️ 钢铁之路侵袭已刷新（{len(nodes_today)} 个节点）："
                                    f"{names}"))
                    last["sp_incursions"] = sig

            if "钢精兑换" in wanted:
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
                    if last.get("steel_rotation") is not None \
                            and last["steel_rotation"] != key:
                        cur = weekly[idx]
                        nxt = weekly[(idx + 1) % len(weekly)]
                        out.append(("钢精兑换", key,
                                    f"🪙 钢精兑换已轮换：本周 {cur['name']}"
                                    f"（{cur['cost']} 精华）"
                                    f" · 下周 {nxt['name']}"))
                    last["steel_rotation"] = key

            if "碎银兑换" in wanted:
                # ★ A3（2026-10-03）：Palladino 商店**无轮换库存**，事件语义 =
                #   每周限购重置（周一 00:00 UTC）。推算同源 palladino_shop.json
                #   （formatters.weekly_reset_info）。
                try:
                    _n, _ = weekly_reset_info(palladino_shop() or {})
                except Exception:  # noqa: BLE001 - 数据缺失降级不报错
                    _n = 0
                key = f"sliver-wk-{_n}"
                if last.get("sliver_reset") is not None \
                        and last["sliver_reset"] != key:
                    out.append(("碎银兑换", key,
                                "🪙 碎银兑换已重置（Palladino · 钢铁守望）："
                                "本周限购恢复 —— 可再购安魂遗物 / 裂罅 Mod / "
                                "安魂通牒等"))
                last["sliver_reset"] = key

            if "赏金" in wanted:
                # ★ C2（2026-10-03）：oracle 三地区（扎里曼/实验室/1999）的赏金
                #   轮换。每条赏金一个事件（key 带本轮 expiry ⇒ 换轮才重推，
                #   notified 去重兜底）；首轮只建基线不推（与裂隙同口径）。
                try:
                    _cyc = await self.client.bounty_cycle()
                except Exception:  # noqa: BLE001 - oracle 不可达降级不报错
                    _cyc = {}
                _bs = (_cyc or {}).get("bounties") or {}
                _exp = str((_cyc or {}).get("expiry") or "")
                _bounty_subs = [s for s in subs
                                if s.platform == platform and s.event == "赏金"]
                # 基线：首轮只记 bounty_exp（不推）；此后每轮对当前赏金发事件
                #（key 含 expiry + notified 去重 ⇒ 同轮只推一次、换轮自然重推）。
                _prev_exp = last.get("bounty_exp")
                if _bounty_subs and _prev_exp is not None:
                    _node_tbl = de_zh("nodes_zh.json")
                    _ch_tbl = de_zh("challenges_zh.json")
                    for _pool, _tag, _title in ORACLE_REGIONS:
                        for _b in _bs.get(_tag) or []:
                            _nk = _b.get("node") or ""
                            _ck = _b.get("challenge") or ""
                            _node = (_node_tbl.get(_nk) or {}).get("name") or _nk
                            _ch = _ch_tbl.get(_ck) or {}
                            _cname = _ch.get("name") or ""
                            _ctype = bounty_type_of(_ck)
                            key = f"bounty-{_tag}-{_nk}-{_ck}-{_exp}"
                            hit = {s.sid for s in _bounty_subs
                                   if bounty_rule_hit(s.rule, _tag, _cname, _ck)}
                            if hit:
                                _desc = (_ch.get("desc") or "").strip()
                                _seg = " · ".join(
                                    x for x in (_title, _node, _ctype, _cname)
                                    if x)
                                out.append(("赏金", key,
                                            f"⚡ 新赏金：{_seg}"
                                            + (f"（{_desc}）" if _desc else "")))
                                matched[key] = hit
                if _exp:
                    last["bounty_exp"] = _exp

            # ★ C4（2026-10-03）：灵化 / 信条 / 终幕 换轮点检测。窗口序号
            #   同源 formatters.rotation_window（与卡面同一套锚点算法）；
            #   跨点才推，文案给出新一批内容。
            _rot_push = (
                ("灵化武器", "incarnon", "🔮 灵化轮换"),
                ("信条", "tenet", "⚔ 信条库存刷新"),
                ("终幕", "coda", "🪲 终幕轮换"),
            )
            if any(ev in wanted for ev, _k, _i in _rot_push):
                try:
                    _rot_all = json.loads(
                        paths.read_path("rotations.json")
                        .read_text(encoding="utf-8"))
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
                    if _weeks:                       # weekly_cycle（灵化）
                        _pos = (int(_rd.get("anchor_week", 1)) - 1
                                + _passed) % len(_weeks)
                        _items = _weeks[_pos]
                        _label = f"第 {_pos + 1}/{len(_weeks)} 周"
                    elif _batches:                   # batch_cycle（终幕）
                        _pos = (int(_rd.get("anchor_idx", 0))
                                + _passed) % len(_batches)
                        _items = _batches[_pos]
                        _labels = _rd.get("batch_label") or []
                        _label = (f"{_labels[_pos]} 批"
                                  if _pos < len(_labels) else f"第 {_pos + 1} 批")
                    else:                            # refresh_only（信条）
                        _items = _rd.get("items") or []
                        _label = "库存"
                    if not _items:
                        continue
                    _key = f"rot-{_rk}-{_passed}"
                    _prev = last.get(f"{_rk}_window")
                    if _prev is not None and _prev != _key:
                        _names = "、".join(
                            (it.get("cn") or it.get("en") or "")
                            + (f"（{it.get('element')} {it.get('bonus')}%）"
                               if it.get("element") else "")
                            for it in _items[:8])
                        out.append((_ev, _key,
                                    f"{_icon}（{_label}）：{_names}"))
                    last[f"{_rk}_window"] = _key

            # ★ C5（2026-10-03）：阿耶兑换 / 1999 日历 / 电波。
            if "阿耶兑换" in wanted:
                try:
                    _pv = await self.client.prime_vault(platform)
                except Exception:  # noqa: BLE001 - 降级不报错
                    _pv = {}
                if isinstance(_pv, dict) and _pv.get("expiry"):
                    _pk = f"pv-{_pv.get('expiry')}"
                    if last.get("pv_key") is not None and last["pv_key"] != _pk:
                        _names = "、".join(
                            (it.get("name") or "") for it in
                            (_pv.get("items") or [])[:6])
                        out.append(("阿耶兑换", _pk,
                                    f"💠 Prime 宝库轮换：{_names}"))
                    last["pv_key"] = _pk

            if "1999日历" in wanted:
                try:
                    _cal = await self.client.calendar(platform)
                except Exception:  # noqa: BLE001
                    _cal = {}
                if isinstance(_cal, dict) and _cal.get("days"):
                    _sk = (f"cal-season-{_cal.get('season')}"
                           f"-{_cal.get('yearIteration')}")
                    if last.get("cal_season") is not None                             and last["cal_season"] != _sk:
                        out.append(("1999日历", _sk,
                                    f"🗓 1999 日历轮换：{_cal.get('season')} 季"
                                    f"（第 {_cal.get('yearIteration')} 年）"))
                    last["cal_season"] = _sk
                    _today = utc_today()
                    _day = next((d for d in _cal["days"]
                                 if d.get("date") == _today), None)
                    if _day:
                        # 键含**日程签名**：同日日程被 DE 更新也推（日切自然推）
                        _sig_d = ",".join(sorted(
                            e.get("name") or ""
                            for e in (_day.get("events") or [])))[:40]
                        _dk = f"cal-{_today}-{_sig_d}"
                        if last.get("cal_day") is not None                                 and last["cal_day"] != _dk:
                            _evs = [e.get("name") for e in
                                    (_day.get("events") or [])
                                    if e.get("name")]
                            out.append(("1999日历", _dk,
                                        f"🗓 1999 今日日程（{_today}）："
                                        + ("、".join(_evs[:6]) or "无条目")))
                        last["cal_day"] = _dk

            if "电波" in wanted:
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
                        _names = "、".join(
                            (c.get("title") or "?")
                            + ("（每日）" if c.get("isDaily") else "")
                            for c in _new[:5])
                        _sig = ",".join(sorted(_fresh))[:60]
                        out.append(("电波", f"nw-{_sig}",
                                    f"📻 午夜电波新挑战（{len(_new)} 条）："
                                    f"{_names}"))
                    last["nw_ids"] = list(_ids)

            if "警报" in wanted:
                alerts = await self.client.alerts(platform)
                ids = {a.get("id") for a in alerts if a.get("id") and a.get("active", True)}
                prev = set(last.get("alert_ids", []))
                for aid in ((ids - prev) if not first else ()):
                    a = next((x for x in alerts if x.get("id") == aid), {})
                    mission = a.get("mission", {}) or {}
                    reward = (mission.get("reward", {}) or {})
                    # ★ C3：解析已补全（节点/类型/等级/奖励中文名），推送照实给
                    names = reward.get("item_names") or (
                        [reward["item"]] if reward.get("item") else [])
                    lv = (f"{mission['min_level']}-{mission['max_level']}级 "
                          if mission.get("min_level") else "")
                    rw = "、".join([*names, *([f"{reward['credits']}现金"]
                                            if reward.get("credits") else [])])
                    head = f"{mission.get('desc')}｜" if mission.get("desc") else ""
                    out.append(("警报", aid,
                                f"🔔 新警报：{head}{mission.get('node', '?')} · "
                                f"{mission_cn(mission.get('type', ''))} {lv}"
                                f"奖励：{rw or '?'}"))
                last["alert_ids"] = list(ids)

            if "入侵" in wanted:
                invs = await self.client.invasions(platform)
                ids = {i.get("id") for i in invs if i.get("id") and not i.get("completed")}
                prev = set(last.get("invasion_ids", []))
                for iid in ((ids - prev) if not first else ()):
                    inv = next((x for x in invs if x.get("id") == iid), {})
                    out.append(("入侵", iid,
                                f"⚔️ 新入侵：{inv.get('node', '?')}（"
                                f"{(inv.get('attacker', {}) or {}).get('faction', '?')} vs "
                                f"{(inv.get('defender', {}) or {}).get('faction', '?')}）"))
                last["invasion_ids"] = list(ids)

            if "新闻" in wanted:
                news = await self.client.news(platform)
                ids = {n.get("id") for n in news if n.get("id")}
                prev = set(last.get("news_ids", []))
                for nid in ((ids - prev) if not first else ()):
                    n = next((x for x in news if x.get("id") == nid), {})
                    msg = (n.get("message") or n.get("title") or "").strip()
                    if msg:
                        out.append(("新闻", nid, f"📰 {msg}" +
                                    (f"\n{n.get('link')}" if n.get("link") else "")))
                last["news_ids"] = list(ids)

            if "每日特惠" in wanted:
                deals = await self.client.daily_deals(platform)
                # 别叫 first：那是「首轮基线」标志，被这里覆盖成商品 dict 后，
                # 排在每日特惠之后的新事件段会把存量内容误当增量推送。
                # 每日特惠自身用 deal_key 判基线，与 first 无关。
                lead = deals[0] if deals else {}
                dkey = lead.get("id") or f"{lead.get('item', '')}"
                if last.get("deal_key") and last["deal_key"] != dkey:
                    lines = [f"{d.get('item', '?')}：{d.get('salePrice', '?')}p "
                             f"（库存{d.get('total', '?')}）" for d in deals]
                    out.append(("每日特惠", f"deal-{dkey}",
                                "🏷️ 达沃每日特惠：\n" + "\n".join(lines)))
                last["deal_key"] = dkey
        except WarframeAPIError as exc:
            self.log.warning("[warframe] %s 快照拉取失败：%s", platform, exc)
        return out


def _local_now():
    from datetime import datetime
    return datetime.now()
