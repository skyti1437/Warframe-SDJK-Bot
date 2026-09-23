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
        def info(self, *a, **k): pass
        def warning(self, *a, **k): pass
        def error(self, *a, **k): pass
        def exception(self, *a, **k): pass
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
        event_message_type = staticmethod(lambda spec: (lambda fn: fn))

    event_mod.AstrMessageEvent = AstrMessageEvent
    event_mod.MessageChain = MessageChain
    event_mod.EventMessageType = _EventMessageType
    event_mod.event_message_type = lambda spec: (lambda fn: fn)
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


def mk(marker: str, *, weapon: str = "绝路 Prime [30]", ok: int = 2,
       bad: int = 0, nmods: int = 3) -> dict:
    """罐头 OCR：fake analyze 按 _checks/_mods 产出评分原料。"""
    checks = [{"ok": True, "label": f"ok{i}"} for i in range(ok)] + \
             [{"ok": False, "label": f"bad{i}"} for i in range(bad)]
    return {"_marker": marker, "weapon": weapon,
            "mods": [{"name": f"m{i}", "drain": 5} for i in range(nmods)],
            "_checks": checks}


def _install_fake_analyze():
    """可编程 analyze：结果完全由罐头 OCR 决定（weapon/_checks/mods）。"""
    def fake(ocr, pips_rows=None):
        return {"weapon": (ocr or {}).get("weapon") or "",
                "mods": (ocr or {}).get("mods") or [],
                "checks": (ocr or {}).get("_checks") or [],
                "pips": {}}
    lo.analyze = fake


def make_inst(pips_rows=None, damage_rows=None):
    """构造只含识卡竞速所需成员的实例 + 桩。"""
    inst = plugin.WarframeSDJK.__new__(plugin.WarframeSDJK)
    inst.RACE_WINDOW_S = 0.8

    async def _no_pips(url):
        return pips_rows
    inst._detect_pips = _no_pips
    inst._fit_scan_image = lambda u: u

    def _dump(url):
        pass
    inst._dump_scan_debug = _dump

    async def _rows(url):
        return damage_rows
    inst._read_damage_rows = _rows

    inst._vision_providers = lambda: [_Prov("fakeA"), _Prov("fakeB"),
                                      _Prov("fakeC")]
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
        return act[-1]
    inst._extract_loadout_from_image = _extract


def run(inst):
    return asyncio.run(inst._extract_loadout_validated("data:image/png;base64,x"))


_orig_analyze = lo.analyze
_install_fake_analyze()

GOOD = mk("good", ok=2, bad=0, nmods=3)                       # _score = 0
MID = mk("mid", ok=2, bad=1, nmods=4)                         # _score = 1
NOWEAPON = mk("noweapon", weapon="", ok=2, bad=0, nmods=3)    # 修复后 _score ≥ 6
UNDER2 = mk("under2", ok=2, bad=0, nmods=2)                   # 4 格豆 → 漏读罚

try:
    # 1. 全过收工：先到的全过结果直接赢
    inst = make_inst()
    script_extract(inst, {"fakeA": ("now", GOOD), "fakeB": ("delay", 0.3, MID)})
    r = run(inst)
    check("全过收工：先到的 GOOD 直接赢", r and r.get("_marker") == "good")

    # 2. 漏读重罚翻转选择：4 格豆下只读 2 张的 GOOD 罚 6 分，输给慢一点的 MID
    inst = make_inst(pips_rows=[{"is_inventory": False, "counts": [1]},
                                {"is_inventory": False, "counts": [2]},
                                {"is_inventory": False, "counts": [1]},
                                {"is_inventory": False, "counts": [3]}])
    script_extract(inst, {"fakeA": ("now", UNDER2), "fakeB": ("delay", 0.25, MID)})
    r = run(inst)
    check("★ 漏读重罚翻转：像素硬下界 4 vs 模型只读 2 → 慢的全读结果赢",
          r and r.get("_marker") == "mid",
          f"got={r and r.get('_marker')}")

    # 3. ★（2026-09-23 修订）武器没认出不抢跑：幻觉渠道的即时结果不取消慢的全对渠道
    inst = make_inst()
    script_extract(inst, {"fakeA": ("now", NOWEAPON), "fakeB": ("delay", 0.25, GOOD)})
    r = run(inst)
    check("★ 武器未认出不抢跑：重罚继续竞速，最终取 GOOD",
          r and r.get("_marker") == "good",
          f"got={r and r.get('_marker')}")

    # 4. 聚焦二读改善才采纳：读行拼接后校验变好 → 采纳拼接结果
    ROWS = [["冲击", 545.6], ["穿刺", 34.1], ["切割", 102.3], ["总计", 682.0]]

    def _analyze_focus_good(ocr, pips_rows=None):
        dr = (ocr.get("panel") or {}).get("damage_rows")
        if dr == [list(r) for r in ROWS]:
            return {"weapon": "绝路 Prime", "mods": ocr.get("mods") or [],
                    "checks": [{"ok": True, "label": "fixed"}], "pips": {}}
        return {"weapon": "绝路 Prime", "mods": ocr.get("mods") or [],
                "checks": [{"ok": False, "label": "b1"}, {"ok": False, "label": "b2"}],
                "pips": {}}
    lo.analyze = _analyze_focus_good
    inst = make_inst(damage_rows=ROWS)
    script_extract(inst, {"fakeA": ("now", mk("mid", ok=0, bad=2, nmods=3))})
    r = run(inst)
    check("★ 聚焦二读改善才采纳：拼接后校验变好 → 采纳拼接结果",
          r and r.get("_marker") == "mid"
          and (r.get("panel") or {}).get("damage_rows") == [list(x) for x in ROWS])

    # 5. 聚焦二读变差保持原结果
    def _analyze_focus_worse(ocr, pips_rows=None):
        dr = (ocr.get("panel") or {}).get("damage_rows")
        n_bad = 3 if dr == [list(r) for r in ROWS] else 1
        return {"weapon": "绝路 Prime", "mods": ocr.get("mods") or [],
                "checks": [{"ok": False, "label": f"b{i}"} for i in range(n_bad)],
                "pips": {}}
    lo.analyze = _analyze_focus_worse
    inst = make_inst(damage_rows=ROWS)
    script_extract(inst, {"fakeA": ("now", mk("mid", ok=2, bad=1, nmods=3))})
    r = run(inst)
    check("聚焦二读变差 → 保持原结果（不采纳拼接）",
          r and r.get("_marker") == "mid"
          and not (r.get("panel") or {}).get("damage_rows"))

    # 6. 窗口超时：挂死渠道不拖垮，返回窗口内最优
    lo.analyze = lambda ocr, pips_rows=None: {
        "weapon": (ocr or {}).get("weapon") or "",
        "mods": (ocr or {}).get("mods") or [],
        "checks": (ocr or {}).get("_checks") or [], "pips": {}}
    inst = make_inst()
    inst.RACE_WINDOW_S = 0.6
    script_extract(inst, {"fakeA": ("now", mk("mid", ok=2, bad=1, nmods=3)),
                          "fakeB": ("hang",)})
    r = run(inst)
    check("窗口超时：挂死渠道被放弃，返回窗口内最优（MID）",
          r and r.get("_marker") == "mid")

    # 7. 全渠道挂 → None（上层出「识别失败」提示）
    inst = make_inst()
    script_extract(inst, {"fakeA": ("hang",), "fakeB": ("hang",)})
    r = run(inst)
    check("全渠道挂 → None", r is None)
finally:
    lo.analyze = _orig_analyze

print()
if FAILED:
    print(f"FAILED {len(FAILED)}: {FAILED}")
    sys.exit(1)
print("ALL PASS")
