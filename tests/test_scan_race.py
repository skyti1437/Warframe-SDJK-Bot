# -*- coding: utf-8 -*-
"""识卡竞速管线的回归网（python3 tests/test_scan_race.py）。

覆盖 ``_extract_loadout_validated`` 的纯 asyncio 逻辑——2026-09-17/20/21
三次识卡事故都发生在这里，而它是识卡管线唯一没有回归网的段落。

做法：astrbot 桩 + ``__new__`` 造实例；``lo.analyze`` 打桩为可编程评分器，
``_extract_loadout_from_image`` 按渠道桩返回罐头 OCR。钉住的行为：
  1. 面板校验全过 → 立即收工（等不到的渠道被取消）；
  2. 豆子漏读重罚能**翻转**选择（先到的漏读结果不赢）；
  3. ★ 武器没认出**不再立即接受**（2026-09-23 修订：幻觉编武器名的
     渠道按重罚继续竞速，窗口耗尽才兜底）；
  4. 聚焦二读改善才采纳、变差保持原结果；
  5. 窗口超时返回窗口内最优，不挂死。
"""

from __future__ import annotations

import asyncio
import gc
import sys
import types
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

    logger = _Logger()

    class AstrMessageEvent:
        unified_msg_origin = "group://scan_race"

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
    api.logger = logger
    sys.modules.setdefault("astrbot", pkg)
    sys.modules["astrbot.api"] = api
    sys.modules["astrbot.api.event"] = event_mod
    sys.modules["astrbot.api.message_components"] = mc_mod
    sys.modules["astrbot.api.star"] = star_mod


_install_astrbot_stub()

import main as plugin  # noqa: E402
from core import loadout_ocr as lo  # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = ""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f"  -> {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


class _Prov:
    """假 vision 渠道：只用于日志里的 id。"""

    def __init__(self, pid: str):
        self._pid = pid

    def meta(self):
        return types.SimpleNamespace(id=self._pid)


def mk(
    marker: str, *, weapon: str = "绝路 Prime [30]", ok: int = 2, bad: int = 0, nmods: int = 3
) -> dict:
    """罐头 OCR：fake analyze 按 _checks/_mods 产出评分原料。"""
    checks = [{"ok": True, "label": f"ok{i}"} for i in range(ok)] + [
        {"ok": False, "label": f"bad{i}"} for i in range(bad)
    ]
    return {
        "_marker": marker,
        "weapon": weapon,
        "mods": [{"name": f"m{i}", "drain": 5} for i in range(nmods)],
        "_checks": checks,
    }


def _fake_analysis(ocr, pips_rows=None, *, weapon=None, checks=None, mods=None, reasons=None):
    """可编程 analyze：保留竞速评分，并补齐分区补读依赖的分析结构。"""
    ocr = ocr or {}
    reasons = list(reasons or [])
    return {
        "weapon": weapon if weapon is not None else ocr.get("weapon") or "",
        "mods": mods if mods is not None else ocr.get("mods") or [],
        "checks": checks if checks is not None else ocr.get("_checks") or [],
        "panel": dict(ocr.get("panel") or {}),
        "quality": {"snapshot_safe": not reasons, "panel_safe": True, "reasons": reasons},
        "pips": {},
    }


def _install_fake_analyze():
    lo.analyze = _fake_analysis


def make_inst(pips_rows=None, damage_rows=None, title=None):
    """构造只含识卡竞速所需成员的实例 + 桩。"""
    inst = plugin.WarframeSDJK.__new__(plugin.WarframeSDJK)
    inst.cfg = {}
    inst.RACE_WINDOW_S = 0.8

    async def _no_pips(url):
        return pips_rows

    inst._detect_pips = _no_pips
    inst._fit_scan_image = lambda u: u

    def _dump(url):
        pass

    inst._dump_scan_debug = _dump

    inst._focus_ocr = None

    async def _focus(url, part):
        if part == "mods":
            # 脚本渠道按原卡清单确认；真实 Provider 清理用例沿用其单卡分析桩。
            ocr = inst._focus_ocr or {}
            return {"mods": ocr.get("mods") or [{"name": "test"}]}
        if part == "panel" and damage_rows:
            return lo.splice_damage_rows({}, damage_rows)
        return None

    inst._read_scan_focus = _focus
    inst._title_calls = 0

    async def _title(url):
        inst._title_calls += 1
        return title

    inst._read_weapon_title = _title

    inst._vision_providers = lambda: [_Prov("fakeA"), _Prov("fakeB"), _Prov("fakeC")]
    return inst


def script_extract(inst, plan: dict):
    """按渠道 id 设定 _extract_loadout_from_image 的行为。

    plan: {prov_id: ("now", ocr) | ("delay", 秒, ocr) | ("hang",)}
    """

    async def _extract(image_url, prov=None):
        pid = getattr(getattr(prov, "meta", lambda: None)(), "id", "?")
        act = plan.get(pid)
        if not act:
            return None
        if act[0] == "hang":
            await asyncio.sleep(30)
            return None
        if act[0] == "delay":
            await asyncio.sleep(act[1])
        inst._focus_ocr = act[-1]
        return act[-1]

    inst._extract_loadout_from_image = _extract


def run(inst):
    return asyncio.run(inst._extract_loadout_validated("data:image/png;base64,x"))


_orig_analyze = lo.analyze
_install_fake_analyze()

GOOD = mk("good", ok=2, bad=0, nmods=3)  # _score = 0
MID = mk("mid", ok=2, bad=1, nmods=4)  # _score = 1
NOWEAPON = mk("noweapon", weapon="", ok=2, bad=0, nmods=3)  # 修复后 _score ≥ 6
UNDER2 = mk("under2", ok=2, bad=0, nmods=2)  # 4 格豆 → 漏读罚

try:
    # 1. 全过收工：先到的全过结果直接赢
    inst = make_inst()
    script_extract(inst, {"fakeA": ("now", GOOD), "fakeB": ("delay", 0.3, MID)})
    r = run(inst)
    check("全过收工：先到的 GOOD 直接赢", r and r.get("_marker") == "good")

    # 2. 漏读重罚翻转选择：4 格豆下只读 2 张的 GOOD 罚 6 分，输给慢一点的 MID
    inst = make_inst(
        pips_rows=[
            {"is_inventory": False, "counts": [1]},
            {"is_inventory": False, "counts": [2]},
            {"is_inventory": False, "counts": [1]},
            {"is_inventory": False, "counts": [3]},
        ]
    )
    script_extract(inst, {"fakeA": ("now", UNDER2), "fakeB": ("delay", 0.25, MID)})
    r = run(inst)
    check(
        "★ 漏读重罚翻转：像素硬下界 4 vs 模型只读 2 → 慢的全读结果赢",
        r and r.get("_marker") == "mid",
        f"got={r and r.get('_marker')}",
    )

    # 3. ★（2026-09-23 修订）武器没认出不抢跑：幻觉渠道的即时结果不取消慢的全对渠道
    inst = make_inst()
    script_extract(inst, {"fakeA": ("now", NOWEAPON), "fakeB": ("delay", 0.25, GOOD)})
    r = run(inst)
    check(
        "★ 武器未认出不抢跑：重罚继续竞速，最终取 GOOD",
        r and r.get("_marker") == "good",
        f"got={r and r.get('_marker')}",
    )

    # 4. 聚焦二读改善才采纳：读行拼接后校验变好 → 采纳拼接结果
    ROWS = [["冲击", 545.6], ["穿刺", 34.1], ["切割", 102.3], ["总计", 682.0]]

    def _analyze_focus_good(ocr, pips_rows=None):
        dr = (ocr.get("panel") or {}).get("damage_rows")
        if dr == [list(r) for r in ROWS]:
            return _fake_analysis(ocr, weapon="绝路 Prime", checks=[{"ok": True, "label": "fixed"}])
        return _fake_analysis(
            ocr,
            weapon="绝路 Prime",
            checks=[{"ok": False, "label": "b1"}, {"ok": False, "label": "b2"}],
        )

    lo.analyze = _analyze_focus_good
    inst = make_inst(damage_rows=ROWS)
    script_extract(inst, {"fakeA": ("now", mk("mid", ok=0, bad=2, nmods=3))})
    r = run(inst)
    check(
        "★ 聚焦二读改善才采纳：拼接后校验变好 → 采纳拼接结果",
        r
        and r.get("_marker") == "mid"
        and (r.get("panel") or {}).get("damage_rows") == [list(x) for x in ROWS],
    )

    # 5. 聚焦二读变差保持原结果
    def _analyze_focus_worse(ocr, pips_rows=None):
        dr = (ocr.get("panel") or {}).get("damage_rows")
        n_bad = 3 if dr == [list(r) for r in ROWS] else 1
        return _fake_analysis(
            ocr,
            weapon="绝路 Prime",
            checks=[{"ok": False, "label": f"b{i}"} for i in range(n_bad)],
        )

    lo.analyze = _analyze_focus_worse
    inst = make_inst(damage_rows=ROWS)
    script_extract(inst, {"fakeA": ("now", mk("mid", ok=2, bad=1, nmods=3))})
    r = run(inst)
    check(
        "聚焦二读变差 → 保持原结果（不采纳拼接）",
        r and r.get("_marker") == "mid" and not (r.get("panel") or {}).get("damage_rows"),
    )

    # 5b. 整图过读不能阻止更短的正确补读：11 张修正到 9 张，质量分下降。
    corrected_mods = mk("corrected", nmods=9)["mods"]

    def _analyze_overread(ocr, pips_rows=None):
        reasons = (
            [{"code": "mods_overread", "message": "读到的卡数超过已装备槽位上限"}]
            if len(ocr.get("mods") or []) > 10
            else []
        )
        return _fake_analysis(ocr, pips_rows, reasons=reasons)

    async def _correct_overread(url, part):
        return {"mods": corrected_mods} if part == "mods" else None

    lo.analyze = _analyze_overread
    inst = make_inst()
    inst._read_scan_focus = _correct_overread
    script_extract(inst, {"fakeA": ("now", mk("overread", nmods=11))})
    r = run(inst)
    check(
        "★ 过读修正：11 张缩为正确 9 张且质量改善 → 采纳并确认装备",
        r
        and r.get("_marker") == "overread"
        and r.get("mods") == corrected_mods
        and not r.get("_equipment_unconfirmed"),
        f"mods={len((r or {}).get('mods') or [])} unconfirmed={(r or {}).get('_equipment_unconfirmed')}",
    )

    # 6. 窗口超时：挂死渠道不拖垮，返回窗口内最优
    lo.analyze = _fake_analysis
    inst = make_inst()
    inst.RACE_WINDOW_S = 0.6
    script_extract(inst, {"fakeA": ("now", mk("mid", ok=2, bad=1, nmods=3)), "fakeB": ("hang",)})
    r = run(inst)
    check("窗口超时：挂死渠道被放弃，返回窗口内最优（MID）", r and r.get("_marker") == "mid")

    # 6b. ★（2026-10-27）武器名单读：主读把姿态卡名当武器名（认不出）→ 单读标题栏读到武器 → 采纳
    def _analyze_title(ocr, pips_rows=None):
        ok = "暮斩" in str((ocr or {}).get("weapon") or "")
        return _fake_analysis(ocr, weapon="暮斩" if ok else "", checks=[{"ok": True, "label": "c"}])

    lo.analyze = _analyze_title
    inst = make_inst(title="升级/暮斩 [30] 3")
    script_extract(inst, {"fakeA": ("now", mk("stance", weapon="狂风压境"))})
    r = run(inst)
    check(
        "★ 武器名单读：主读读成姿态卡名 → 单读标题栏「升级/暮斩」被采纳",
        r and r.get("weapon") == "升级/暮斩 [30] 3" and inst._title_calls == 1,
        f"got={r and r.get('weapon')} calls={inst._title_calls}",
    )
    inst = make_inst(title="升级/暮斩 [30] 3")
    script_extract(inst, {"fakeA": ("now", mk("ok", weapon="暮斩 [30]", ok=1, bad=1))})
    r = run(inst)
    check(
        "主读已认出武器（即使有别的校验不过）→ 不触发武器名单读",
        r and r.get("weapon") == "暮斩 [30]" and inst._title_calls == 0,
        f"calls={inst._title_calls}",
    )
    lo.analyze = _fake_analysis

    # 7. 全渠道挂 → None（上层出「识别失败」提示）
    inst = make_inst()
    script_extract(inst, {"fakeA": ("hang",), "fakeB": ("hang",)})
    r = run(inst)
    check("全渠道挂 → None", r is None)

    # 8. 走真实 handler 和两层提取入口：取消时必须先等渠道 finally，再归还并发名额。
    async def cancel_from_handler():
        started = asyncio.Event()
        cleaning = asyncio.Event()
        release = asyncio.Event()
        cleaned = asyncio.Event()
        interrupted = []

        class Provider(_Prov):
            async def text_chat(self, **kwargs):
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cleaning.set()
                    try:
                        await release.wait()
                    except asyncio.CancelledError:
                        interrupted.append(True)
                        raise
                    cleaned.set()

        inst = make_inst()
        inst.cfg = {"scan_cooldown": 0, "scan_max_concurrent": 1}
        inst._ocr_busy, inst._ocr_last = set(), {}
        inst._safe_sender = lambda event: event.sender
        inst._event_has_image = lambda event: True

        async def images(event):
            return ["data:image/png;base64,stub"]

        inst._image_data_urls = images
        inst._vision_providers = lambda: [Provider("local-probe")]
        event = types.SimpleNamespace(unified_msg_origin="group://first", sender="u1")
        task = asyncio.create_task(inst._h_scan(None, event, None))
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        await asyncio.wait_for(cleaning.wait(), 2)
        for _ in range(5):
            await asyncio.sleep(0)
        check(
            "★ 真实识卡入口取消：渠道清理未完时保留并发名额",
            not task.done() and event.unified_msg_origin in inst._ocr_busy,
        )
        retry = await inst._h_scan(
            None, types.SimpleNamespace(unified_msg_origin="group://second", sender="u2"), None
        )
        check("★ 渠道仍清理时新请求受并发上限约束", "并发上限 1" in (retry.raw_text or ""))
        task.cancel()
        for _ in range(5):
            await asyncio.sleep(0)
        check(
            "★ 重复取消不打断渠道 finally",
            not interrupted and not task.done() and bool(inst._ocr_busy),
        )
        release.set()
        try:
            await asyncio.wait_for(task, 2)
        except asyncio.CancelledError:
            pass
        check(
            "★ 清理完成后传播取消并归还名额",
            task.cancelled() and cleaned.is_set() and not inst._ocr_busy,
        )

    asyncio.run(cancel_from_handler())

    # 9. 两种竞速都必须回收败者，并消费败者清理中抛出的异常，不改变胜者。
    async def winner_cleanup(kind):
        ready = asyncio.Event()
        cleaned = asyncio.Event()
        loop_errors = []
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(lambda loop, context: loop_errors.append(context))

        class Fast(_Prov):
            async def text_chat(self, **kwargs):
                await ready.wait()
                return types.SimpleNamespace(completion_text='{"weapon":"winner"}')

        class Slow(_Prov):
            async def text_chat(self, **kwargs):
                ready.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    await asyncio.sleep(0)
                    cleaned.set()
                    raise RuntimeError("败者清理异常")

        provs = [Fast("fast"), Slow("slow")]
        inst = make_inst()
        inst._vision_providers = lambda: provs
        if kind == "识卡":
            out = await inst._extract_loadout_validated("data:image/png;base64,stub")
        else:
            out = await inst._vision_race_json("prompt", "image", provs, window=1)
        gc.collect()
        await asyncio.sleep(0)
        check(
            f"★ {kind}竞速：回收败者异常并保留胜者",
            out.get("weapon") == "winner" and cleaned.is_set() and not loop_errors,
        )

    lo.analyze = lambda ocr, pips_rows=None: _fake_analysis(
        ocr, mods=[{"name": "test"}], checks=[{"ok": True}]
    )
    asyncio.run(winner_cleanup("识卡"))
    asyncio.run(winner_cleanup("紫卡"))

    # 渠道自身退出不是用户取消：失败渠道不能取消另一份仍可用的结果。
    async def self_cancelled_channel(kind, mode):
        gate = asyncio.Event()
        child_tasks = []
        cleaned = []

        class Channel(_Prov):
            async def text_chat(self, **kwargs):
                child_tasks.append(asyncio.current_task())
                try:
                    await gate.wait()
                    if self._pid == "cancel" or mode == "all-cancel":
                        raise asyncio.CancelledError("渠道自行停止")
                    if mode == "cancel-first":
                        await asyncio.sleep(0.01)
                    return types.SimpleNamespace(completion_text='{"weapon":"winner"}')
                finally:
                    cleaned.append(self._pid)

        inst = make_inst()
        provs = [Channel("cancel"), Channel("good")]
        inst._vision_providers = lambda: provs

        async def race():
            if kind == "识卡":
                return await inst._extract_loadout_validated("data:image/png;base64,stub")
            return await inst._vision_race_json("prompt", "image", provs, window=1)

        task = asyncio.create_task(race())
        await asyncio.sleep(0)
        gate.set()
        out = await asyncio.wait_for(task, timeout=1)
        check(
            f"★ {kind}竞速 {mode}：渠道自行取消不冒充调用方取消",
            not task.cancelled()
            and (out is None if mode == "all-cancel" else out.get("weapon") == "winner"),
        )
        check(
            f"★ {kind}竞速 {mode}：所有渠道任务完成且执行各自清理",
            len(child_tasks) == 2
            and all(t.done() for t in child_tasks)
            and sorted(cleaned) == ["cancel", "good"],
        )

    for _kind in ("识卡", "紫卡"):
        for _mode in ("cancel-first", "same-tick", "all-cancel"):
            asyncio.run(self_cancelled_channel(_kind, _mode))

    # 10. 走真实识卡与紫卡路由，渠道只用本地 text_chat 桩，核对实际收图方。
    async def scan_provider_isolation(scan_config, *, unavailable=False):
        seen = []

        class Provider(_Prov):
            async def text_chat(self, *, prompt, session_id, image_urls):
                kind = session_id.rsplit("-", 1)[0]
                seen.append((self._pid, kind, list(image_urls)))
                if kind == "sdjk-紫卡行读":
                    response = "+100% 暴击几率\n+80% 暴击伤害\n-30% 变焦"
                elif kind == "sdjk-紫卡识别":
                    response = '{"weapon":"伯斯顿","positive":[["暴击",100],["暴伤",80]],"negative":[["变焦",30]]}'
                elif kind == "sdjk-title":
                    response = "路由测试"
                elif kind == "sdjk-rows":
                    response = '{"rows":[["冲击",1],["穿刺",2],["总计",3]]}'
                elif kind == "sdjk-focus":
                    if prompt == plugin.WarframeSDJK._FOCUS_PROMPTS["mods"]:
                        response = '{"mods":[{"name":"test","drain":5}]}'
                    elif prompt == plugin.WarframeSDJK._FOCUS_PROMPTS["arcanes"]:
                        response = "主要·无情"
                    else:
                        response = '{"panel":{"crit_chance":50,"crit_damage":2,"status_chance":20}}'
                else:
                    response = '{"weapon":"路由测试","mods":[{"name":"test","drain":5}],"_checks":[{"ok":true}],"arcanes":["测试赋能"]}'
                return types.SimpleNamespace(completion_text=response)

        main, line, scan = Provider("main/only"), Provider("line/only"), Provider("scan/only")

        class Context:
            def get_provider_by_id(self, pid):
                if unavailable and pid == "scan/only":
                    raise RuntimeError("渠道暂不可用")
                return {p.meta().id: p for p in (main, line, scan)}.get(pid)

            def get_all_providers(self):
                return [main, line, scan]

        inst = plugin.WarframeSDJK.__new__(plugin.WarframeSDJK)
        inst.cfg = {"vision_provider_id": main.meta().id, "vision_provider_id_line": line.meta().id}
        inst.cfg.update(scan_config)
        inst.context = Context()
        inst.RACE_WINDOW_S = 0.8
        inst._fit_scan_image = lambda image: image
        inst._scan_focus_image = lambda image, part: image

        async def no_pips(image):
            return []

        inst._detect_pips = no_pips
        image = "data:image/png;base64,local-routing-probe"
        expected = (
            []
            if unavailable or scan_config.get("vision_provider_id_scan") == "missing/only"
            else [scan if str(scan_config.get("vision_provider_id_scan") or "").strip() else main]
        )
        case = str(scan_config.get("vision_provider_id_scan", "未配置")).strip() or "空配置"
        if unavailable:
            case += "（取渠道抛错）"
        check(f"★ 识卡独立渠道 {case}：候选符合配置", inst._scan_vision_providers() == expected)

        # 不替换 _vision_providers / _vision_race_json / 任一视觉读图方法。
        outputs = [
            await inst._extract_loadout_validated(image),
            await inst._extract_loadout_from_image(image),
            await inst._read_weapon_title(image),
            await inst._read_scan_focus(image, "mods"),
            await inst._read_scan_focus(image, "panel"),
            await inst._read_scan_focus(image, "arcanes"),
            await inst._read_damage_rows(image),
        ]
        check(
            f"★ 识卡独立渠道 {case}：主读、标题、三区补读与旧伤害行返回",
            all(outputs) if expected else all(value is None for value in outputs),
            repr(outputs),
        )
        scan_seen = list(seen)
        check(
            f"★ 识卡独立渠道 {case}：实际收图方独占且无意外回落",
            bool(scan_seen)
            and {pid for pid, _, _ in scan_seen} == {expected[0].meta().id}
            and {kind for _, kind, _ in scan_seen}
            == {"sdjk-loadout", "sdjk-title", "sdjk-focus", "sdjk-rows"}
            if expected
            else not scan_seen,
            repr(scan_seen),
        )
        out = await inst._extract_riven_from_image(image)
        riven_seen = seen[len(scan_seen) :]
        check(
            f"★ 识卡独立渠道 {case}：紫卡主读与窄读实际收图方保持不变",
            {(pid, kind) for pid, kind, _ in riven_seen}
            == {(main.meta().id, "sdjk-紫卡识别"), (line.meta().id, "sdjk-紫卡行读")}
            and len(riven_seen) == 2
            and out
            and out.get("weapon") == "伯斯顿"
            and len(out.get("lines", [])) == 3,
            repr(riven_seen),
        )

    lo.analyze = _fake_analysis
    for _scan_config in (
        {},
        {"vision_provider_id_scan": "  "},
        {"vision_provider_id_scan": "scan/only"},
        {"vision_provider_id_scan": "missing/only"},
    ):
        asyncio.run(scan_provider_isolation(_scan_config))
    asyncio.run(scan_provider_isolation({"vision_provider_id_scan": "scan/only"}, unavailable=True))
finally:
    lo.analyze = _orig_analyze

print()
if FAILED:
    print(f"FAILED {len(FAILED)}: {FAILED}")
    sys.exit(1)
print("ALL PASS")
