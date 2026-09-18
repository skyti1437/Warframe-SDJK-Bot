# -*- coding: utf-8 -*-
"""玄骸（Kuva/Tenet/Coda）查询回归（python3 tests/test_lich.py）

覆盖 2026-09-18 用户反馈：
  1. 「xh 信条弧电离子枪」「玄骸 赤毒海克」等**查不到**（别名表只有 30 条赤毒，
     信条一把都没有）→ 改为从 weapons_stats + DE 译名自动生成 47 把三类武器
  2. 卡面「只有伤害加成」→ 元素原来显示英文（radiation），现在中文（辐射）；
     并补 **在线情况** 与 **信用等级**（WM 返回里本来就有，只是没显示）
  3. 新增分支筛选「xh 武器名 [元素] [数值]」
  4. WM 对部分武器（近战 Tenet / 全部 Coda）返回 400 → 要给出**准确提示**，
     不能糊成「内部错误」
"""
from __future__ import annotations

import asyncio
import json
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
        def debug(self, *a, **k): pass

    class AstrMessageEvent:
        def __init__(self):
            self.unified_msg_origin = "group://lich_test"
        def get_sender_name(self):
            return "tester"
        def get_sender_id(self):
            return "tester_id"

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
    star_mod.Context = type("Context", (), {})
    star_mod.Star = type("Star", (), {"__init__": lambda self, *a, **k: None})
    star_mod.register = lambda *a, **k: (lambda cls: cls)
    api.AstrBotConfig = AstrBotConfig
    api.logger = _Logger()
    sys.modules.update({"astrbot": pkg, "astrbot.api": api,
                        "astrbot.api.event": event_mod,
                        "astrbot.api.message_components": mc_mod,
                        "astrbot.api.star": star_mod})


_install_astrbot_stub()

import main as plugin                       # noqa: E402
from core import formatters as fmt          # noqa: E402
from core.api_client import WarframeClient  # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"[{'PASS' if cond else 'FAIL'}] {name}"
          + (f" -> {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


class _P:
    def __init__(self, text: str):
        self.content = text.split()
        self.content_str = text
        self.preset = ""
        self.page = 1


def _client() -> WarframeClient:
    return WarframeClient(timeout=25)


# ---------------------------------------------------------------- 名称解析
c = _client()
for q, want in (("信条弧电离子枪", "tenet_arca_plasmor"),
                ("赤毒海克", "kuva_hek"),
                ("赤毒怒雷", "kuva_bramma"),
                ("怒雷", "kuva_bramma"),
                ("海克", "kuva_hek"),
                ("赤毒 海克", "kuva_hek"),          # 中间带空格
                ("赤毒·海克", "kuva_hek"),          # 带官方分隔符
                ("科达血肢", "coda_hema")):
    check(f"名称解析：{q} → {want}", c.resolve_lich_weapon(q) == want,
          str(c.resolve_lich_weapon(q)))

check("形近字兜底（赤毒弧电离子枪 → 弧电离子枪）",
      c.resolve_lich_weapon("赤毒弧电离子枪") is not None,
      str(c.resolve_lich_weapon("赤毒弧电离子枪")))

# ---------------------------------------------------------------- 武器表
db = json.loads((ROOT / "core" / "data" / "lich_weapons.json")
                .read_text(encoding="utf-8"))
kinds = {}
for r in db.values():
    kinds[r["type"]] = kinds.get(r["type"], 0) + 1
check("三类武器都有（lich/sister/coda）",
      all(kinds.get(k, 0) > 0 for k in ("lich", "sister", "coda")), str(kinds))
check(f"武器表规模合理（{len(db)} 把 ≥ 40）", len(db) >= 40, str(len(db)))
check("每把都有中文名", all(r.get("zh") for r in db.values()),
      str([k for k, r in db.items() if not r.get("zh")][:3]))
check("类型由 WM 拍卖 type 决定（kuva→lich / tenet→sister）",
      c.lich_weapon_info("kuva_bramma")["type"] == "lich"
      and c.lich_weapon_info("tenet_arca_plasmor")["type"] == "sister")

# ---------------------------------------------------------------- 卡面显示
row = fmt.fmt_lich_row(1, {
    "buyout_price": 300,
    "item": {"element": "radiation", "damage": 50, "having_ephemera": False},
    "owner": {"status": "ingame", "reputation": 45}})
check("★ 元素显示中文（radiation → 辐射）", "辐射" in row and "radiation" not in row, row)
check("★ 显示在线情况（ingame → 游戏内）", "游戏内" in row, row)
check("★ 显示信用等级", "信用45" in row, row)
row2 = fmt.fmt_lich_row(2, {
    "starting_price": 100,
    "item": {"element": "electricity", "damage": 47, "having_ephemera": True},
    "owner": {"status": "online", "reputation": 0}})
check("幻纹标记与在线状态", "幻纹✦" in row2 and "在线" in row2, row2)
row3 = fmt.fmt_lich_row(3, {"item": {}, "owner": {}})
check("字段缺失不崩（占位 ?）", "?" in row3, row3)

# ---------------------------------------------------------------- 元素识别
for tok, want in (("辐射", "radiation"), ("radiation", "radiation"),
                  ("辐", "radiation"), ("毒素", "toxin"), ("toxin", "toxin"),
                  ("火", "heat")):
    cn, en = plugin._xh_element([tok])
    check(f"元素识别 {tok} → {want}", en == want, f"{cn}/{en}")
check("非元素词不误判", plugin._xh_element(["50", "幻纹"]) == (None, None),
      str(plugin._xh_element(["50", "幻纹"])))


# ---------------------------------------------------------------- 端到端（离线桩）
class _FakeClient:
    """离线桩：不联网，只验证 handler 的筛选/排序/提示逻辑。"""

    def __init__(self):
        self._aliases = {"lich_items": {"赤毒怒雷": "kuva_bramma",
                                        "信条弧电离子枪": "tenet_arca_plasmor",
                                        "科达血肢": "coda_hema"}}
        self._unsupported = set()
        self.last_type = None

    def resolve_lich_weapon(self, q):
        return self._aliases["lich_items"].get(q)

    def lich_weapon_info(self, slug):
        return {"type": {"kuva": "lich", "tenet": "sister",
                         "coda": "coda"}[slug.split("_")[0]],
                "zh": slug, "en": slug}

    def lich_unsupported(self, slug):
        return slug in self._unsupported

    def alias_lookup(self, q, table="wm_items"):
        return None                      # 紫卡兜底路径（本测试不需要）

    async def wm_lich_auctions(self, slug, platform="pc", *, lich_type="lich", **kw):
        self.last_type = lich_type
        if slug == "coda_hema":
            self._unsupported.add(slug)
            return []
        return [
            {"buyout_price": 100, "item": {"element": "toxin", "damage": 30,
                                           "having_ephemera": False},
             "owner": {"status": "offline", "reputation": 1}},
            {"buyout_price": 200, "item": {"element": "radiation", "damage": 55,
                                           "having_ephemera": True},
             "owner": {"status": "ingame", "reputation": 9}},
            {"buyout_price": 150, "item": {"element": "toxin", "damage": 45,
                                           "having_ephemera": False},
             "owner": {"status": "online", "reputation": 3}},
        ]


def _plugin_with(fake):
    o = plugin.WarframeQuery.__new__(plugin.WarframeQuery)
    o.cfg = {}
    o.client = fake
    return o


fake = _FakeClient()
o = _plugin_with(fake)
r = asyncio.run(o._h_xh(_P("信条弧电离子枪"), None, "pc"))
check("★ 信条武器走 type=sister", fake.last_type == "sister", str(fake.last_type))
check("信条武器能出结果卡", not r.raw_text and r.lines, str(r)[:80])
check("★ 排序：游戏内优先（第 1 行）", "游戏内" in r.lines[0], r.lines[0])
check("卡面含在线/信用说明", any("信用" in ln for ln in r.lines), str(r.lines[-1]))

o2 = _plugin_with(_FakeClient())
r2 = asyncio.run(o2._h_xh(_P("赤毒怒雷 辐射"), None, "pc"))
check("元素筛选生效（只剩 1 条辐射）", "1条" in r2.title, r2.title)

o3 = _plugin_with(_FakeClient())
r3 = asyncio.run(o3._h_xh(_P("赤毒怒雷 40"), None, "pc"))
check("数值筛选生效（伤害≥40 剩 2 条）", "2条" in r3.title, r3.title)

o4 = _plugin_with(_FakeClient())
r4 = asyncio.run(o4._h_xh(_P("科达血肢"), None, "pc"))
check("★ WM 无该类目时给准确提示（不是内部错误）",
      r4.raw_text is None and any("挂单类目" in ln for ln in r4.lines),
      str(r4.lines[:2]))

o5 = _plugin_with(_FakeClient())
r5 = asyncio.run(o5._h_xh(_P("不存在的武器xyz"), None, "pc"))
check("未知武器给出提示而非崩溃", bool(r5.raw_text), str(r5)[:60])

print()
if FAILED:
    print(f"✗ {len(FAILED)} 项失败：" + "、".join(FAILED))
    sys.exit(1)
print("✓ 全部通过")
