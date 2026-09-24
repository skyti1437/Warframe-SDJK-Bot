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

from .api_client import WarframeAPIError, WarframeClient
from .formatters import (countdown, mission_cn, parse_iso, tier_cn)
from .parser import FissureFilter, parse_fissure_filter
from . import arbi
from .store import Subscription, SubscriptionStore

SendFunc = Callable[[str, str], Awaitable[None]]

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
    # ★ DE 已停用警报系统：worldState 的 Alerts 恒为 []（实测 2026-09-19 条数 0），
    #   订阅了也永远不会触发 —— 按铁律 A 标为不可订阅并给出替代，别让用户白等。
    "警报": ("DE 官方已停用警报系统（数据恒为空），可改蹲「入侵」或「新闻」", False),
    "入侵": ("新入侵出现", True),
    "新闻": ("官方新闻/热修发布", True),
    "每日特惠": ("达沃每日特惠刷新", True),
    "活动": ("限时活动开始 / 结束", True),
    "1999日历": ("1999 日历轮换（需接线数据源）", False),
    "灵化武器": ("本周灵化轮换（需接线数据源）", False),
    "信条": ("Ergo 信条武器轮换（需接线数据源）", False),
    "终幕": ("Coda 终幕武器轮换（需接线数据源）", False),
    "钢精兑换": ("Varzia 钢精兑换（需接线数据源）", False),
    "碎银兑换": ("碎银兑换（需接线数据源）", False),
    "阿耶兑换": ("阿耶 Priess 兑换（需接线数据源）", False),
    "电波": ("午夜电波周挑战刷新（需接线数据源）", False),
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
    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="warframe-push-daemon")
            self.log.info("[warframe] 推送守护协程已启动（间隔 %ss）", self.interval)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None

    async def _run(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 守护协程不允许退出
                self.log.error("[warframe] 推送轮询异常：%s", exc)
            await asyncio.sleep(self.interval)

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
                    # 真推出去才占掉这个会话的名额：前面那条被自己的免打扰窗
                    # 挡住时，后面全天候的订阅还能接住同一事件。
                    if await self._dispatch(sub, key, text, now):
                        sent.add(sub.umo)

    async def _dispatch(self, sub: Subscription, key: str, text: str,
                        now: float) -> bool:
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
            await self.send(sub.umo, text)
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
                last["fissure_ids"] = list(live.keys())

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
                last["cetus_state"] = state

            if "山谷" in wanted:
                vallis = await self.client.cycle(platform, "vallis")
                st = vallis.get("state")
                if last.get("vallis_state") and last["vallis_state"] != st:
                    label = "温暖（可采矿）" if st == "warm" else "寒冷（热美亚）"
                    out.append(("山谷", f"vallis-{st}-{vallis.get('expiry', '')}",
                                f"🌡️ 奥布山谷转为{label}，"
                                f"剩余 {countdown(vallis.get('expiry', ''))}"))
                last["vallis_state"] = st

            if "魔胎" in wanted:
                cambion = await self.client.cycle(platform, "cambion")
                st = cambion.get("state")
                if last.get("cambion_state") and last["cambion_state"] != st:
                    label = "Fass" if st == "fass" else "Vome"
                    out.append(("魔胎", f"cambion-{st}-{cambion.get('expiry', '')}",
                                f"🦠 魔胎之境已切换到 {label}，"
                                f"剩余 {countdown(cambion.get('expiry', ''))}"))
                last["cambion_state"] = st

            if "地球" in wanted:
                earth = await self.client.cycle(platform, "earth")
                st = earth.get("state")
                if last.get("earth_state") and last["earth_state"] != st:
                    label = "白天" if st == "day" else "夜晚"
                    out.append(("地球", f"earth-{st}-{earth.get('expiry', '')}",
                                f"🌍 地球已进入{label}，"
                                f"剩余 {countdown(earth.get('expiry', ''))}"))
                last["earth_state"] = st

            if "双衍" in wanted:
                duv = await self.client.cycle(platform, "duviri")
                st = duv.get("state")
                if last.get("duviri_state") and last["duviri_state"] != st:
                    cn = duv.get("stateCn") or st
                    out.append(("双衍", f"duviri-{st}-{duv.get('expiry', '')}",
                                f"🌀 双衍王境螺旋切换为「{cn}」（{st}），"
                                f"剩余 {countdown(duv.get('expiry', ''))}"))
                last["duviri_state"] = st

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
                last["goal_ids"] = list(live_ids)

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
                last["trader_active"] = active

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
                    last["arbi_slot"] = sl["key"]

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

            if "警报" in wanted:
                alerts = await self.client.alerts(platform)
                ids = {a.get("id") for a in alerts if a.get("id") and a.get("active", True)}
                prev = set(last.get("alert_ids", []))
                for aid in ((ids - prev) if not first else ()):
                    a = next((x for x in alerts if x.get("id") == aid), {})
                    mission = a.get("mission", {}) or {}
                    reward = (mission.get("reward", {}) or {})
                    out.append(("警报", aid,
                                f"🔔 新警报：{mission.get('node', '?')} · "
                                f"{mission_cn(mission.get('type', ''))} 奖励："
                                f"{reward.get('item', '') or reward.get('credits', '')}"))
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
