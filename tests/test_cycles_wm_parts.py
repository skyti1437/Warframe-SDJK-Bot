# -*- coding: utf-8 -*-
"""蹲推送周期事件去重键 + WM 套装部件 离线测试：python3 tests/test_cycles_wm_parts.py

2026-09-14 用户反馈「蹲指令没消息了」：
  夜灵/山谷/魔胎/地球/双衍的去重键只取 expiry 日期（vallis-warm-2026-09-14），
  同一天内第二次同状态切换被 notified 当成重复吞掉 —— 山谷每 ~10 分钟切换，
  一天只推得出第一轮，之后全部静默。修复：键改用完整 expiry 时间戳。

2026-09-14 用户要求：wm 查套装默认输出套装单 + 附带部件参考价；
  「wm 席瓦蓝图」这类中文部件名要能剥出基名+部件词命中部件物品。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.api_client import (WarframeClient, match_wm_normalized,  # noqa: E402
                             norm_wm_name, split_component_query)
from core import formatters as fmt  # noqa: E402
from core.formatters import fmt_wm_set_parts  # noqa: E402
from core.push import PushDaemon  # noqa: E402
from core.store import Subscription, SubscriptionStore  # noqa: E402

FAILED = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}"
          + (f" -> {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


class FakeLogger:
    def info(self, *a, **k): pass

    def warning(self, *a, **k): pass

    def error(self, *a, **k): pass


# ================================================================ 1. 归一化/部件拆分
ITEMS = [
    {"id": "0", "url_name": "silva_aegis_prime_set", "zh": "席瓦 & 神盾 Prime 一套",
     "en": "Silva & Aegis Prime Set", "tags": ["set", "prime", "warframe"]},
    {"id": "1", "url_name": "silva_aegis_prime", "zh": "席瓦 & 神盾 Prime",
     "en": "Silva & Aegis Prime", "tags": ["prime", "warframe"]},
    {"id": "2", "url_name": "silva_aegis_prime_blueprint",
     "zh": "席瓦 & 神盾 Prime 蓝图", "en": "Silva & Aegis Prime Blueprint",
     "tags": ["blueprint", "component"]},
    {"id": "3", "url_name": "silva_aegis_prime_orbiter",
     "zh": "席瓦 & 神盾 Prime 星体", "en": "Silva & Aegis Prime Orbiter",
     "tags": ["component"]},
]

check("部件拆分：席瓦蓝图", split_component_query("席瓦蓝图") == ("席瓦", "蓝图", "blueprint"))
check("部件拆分：长词优先（头部神经光元）",
      split_component_query("Saryn头部神经光元")
      == ("Saryn", "头部神经光元", "neuroptics"))
check("部件拆分：非部件名返回 None", split_component_query("席瓦") is None)

hit = match_wm_normalized("Saryn蓝图", ITEMS[:0])  # 空列表安全
check("空物品列表安全", hit is None)
check("归一化函数基本性质",
      norm_wm_name("Silva Prime 蓝图") == "silva蓝图" and norm_wm_name("") == "")


def _bare_client(items, aliases=None):
    """免 __init__ 的 WarframeClient，网络/词表全部替换成测试桩。"""
    c = WarframeClient.__new__(WarframeClient)

    async def _items():
        return items

    c.wm_items = _items
    c.alias_lookup = lambda q, table="wm_items": (aliases or {}).get(q)
    return c


async def _resolve_tests():
    c = _bare_client(ITEMS)
    hit = await c.resolve_wm_item("席瓦蓝图")
    check("「wm 席瓦蓝图」命中蓝图部件",
          hit and hit["url_name"] == "silva_aegis_prime_blueprint",
          str(hit and hit["url_name"]))
    hit = await c.resolve_wm_item("席瓦星体")
    check("「wm 席瓦星体」命中星体部件",
          hit and hit["url_name"] == "silva_aegis_prime_orbiter",
          str(hit and hit["url_name"]))
    hit = await c.resolve_wm_item("Saryn蓝图") if False else None
    # 拉丁名归一化直配（zh 混拉丁名）
    items2 = [{"id": "1", "url_name": "saryn_prime_blueprint",
               "zh": "Saryn Prime 蓝图", "en": "Saryn Prime Blueprint",
               "tags": ["blueprint", "component"]}]
    c2 = _bare_client(items2)
    hit = await c2.resolve_wm_item("Saryn蓝图")
    check("拉丁名归一化直配「Saryn蓝图」",
          hit and hit["url_name"] == "saryn_prime_blueprint",
          str(hit and hit["url_name"]))
    hit = await c.resolve_wm_item("不存在的物品名")
    check("无关查询不误命中", hit is None, str(hit))


asyncio.run(_resolve_tests())


# ================================================================ 2. 套装拆件
async def _wm_v2_stub(path, *, ttl, headers=None, platform=None):
    return {
        "id": "set-id", "slug": "silva_aegis_prime_set", "tags": ["set"],
        "setRoot": True,
        "setParts": ["1", "2", "3", "set-id"],
        "i18n": {"zh-hans": {"name": "席瓦 & 神盾 Prime 一套"},
                 "en": {"name": "Silva & Aegis Prime Set"}},
    }


async def _no_set_stub(path, *, ttl, headers=None, platform=None):
    return {"setRoot": False, "setParts": [], "i18n": {}}


async def _wm_parts_test():
    c = WarframeClient.__new__(WarframeClient)
    c._wm_v2 = _wm_v2_stub          # 实例属性不绑定 self，签名须无 self
    detail = await c.wm_item_detail("silva_aegis_prime_set")
    check("wm_item_detail 解析 setRoot/setParts",
          detail.get("set_root") is True and len(detail.get("set_parts") or []) == 4,
          str(detail))

    async def _items2():
        return ITEMS + [{"id": "set-id",
                         "url_name": "silva_aegis_prime_set",
                         "zh": "席瓦 & 神盾 Prime 一套",
                         "en": "Silva & Aegis Prime Set",
                         "tags": ["set"]}]
    c.wm_items = _items2
    parts = await c.wm_set_parts("silva_aegis_prime_set")
    slugs = [p["url_name"] for p in parts]
    check("wm_set_parts 返回部件且排除套装自身",
          "silva_aegis_prime_blueprint" in slugs
          and "silva_aegis_prime_set" not in slugs and len(parts) == 3,
          str(slugs))

    c2 = WarframeClient.__new__(WarframeClient)
    c2._wm_v2 = _no_set_stub
    check("非套装返回空部件列表", await c2.wm_set_parts("saryn_prime") == [])


asyncio.run(_wm_parts_test())


# ================================================================ 3. 部件价排版
rows = [
    {"name": "席瓦 & 神盾 Prime 蓝图", "sell": 45, "buy": 38},
    {"name": "席瓦 & 神盾 Prime 机体", "sell": None, "buy": 20},
    {"name": "席瓦 & 神盾 Prime 系统", "sell": 2, "buy": None},
    {"name": "席瓦 & 神盾 Prime 头盔", "sell": None, "buy": None},
]
lines = fmt_wm_set_parts(rows)
check("部件价标题行", lines[0] == "◆ 部件参考价")
check("在售+收购格式（无单数计数）",
      any("蓝图" in l and "在售 45p" in l and "收购 38p" in l
          and "×" not in l.split("　")[1] for l in lines), str(lines))
check("仅收购格式", any("机体" in l and "收购 20p" in l for l in lines))
check("不标注在线状态", all("（离线）" not in l and "离线" not in l
                          for l in lines), str(lines))
check("无挂单格式", any("头盔　暂无挂单" in l for l in lines))
check("空输入返回空列表", fmt_wm_set_parts([]) == [])
check("附单查部件提示", any("wm 部件名" in l for l in lines))

# wm_best_price：口径与单件查询列表第一行完全一致
_best_orders = [
    {"order_type": "sell", "platinum": 10, "visible": True,
     "user": {"status": "offline"}},                       # 绝对最低但是离线
    {"order_type": "sell", "platinum": 20, "visible": True,
     "user": {"status": "online"}},                        # 网页在线
    {"order_type": "sell", "platinum": 24, "visible": True,
     "user": {"status": "ingame"}},                        # 游戏内在线
]
check("在售最优=游戏内在线 24p（对齐单件查询第一条）",
      fmt.wm_best_price(_best_orders, "sell") == 24,
      str(fmt.wm_best_price(_best_orders, "sell")))
_buy_orders = [
    {"order_type": "buy", "platinum": 9, "visible": True,
     "user": {"status": "offline"}},
    {"order_type": "buy", "platinum": 8, "visible": True,
     "user": {"status": "ingame"}},
]
check("收购最优=游戏内在线 8p（离线 9p 不抢占）",
      fmt.wm_best_price(_buy_orders, "buy") == 8,
      str(fmt.wm_best_price(_buy_orders, "buy")))
check("无可见订单返回 None",
      fmt.wm_best_price([{"order_type": "sell", "platinum": 1,
                          "visible": False, "user": {"status": "ingame"}}],
                        "sell") is None)


# ================================================================ 4. 蹲周期事件
class CycleClient:
    """山谷轮换：每次调用切到下一个状态，expiry 变化但日期同一天。"""

    def __init__(self):
        self.seq = [("warm", "2026-09-14T12:00:00Z"),
                    ("cold", "2026-09-14T12:06:40Z"),
                    ("warm", "2026-09-14T12:13:20Z"),
                    ("cold", "2026-09-14T12:20:00Z")]
        self.i = 0

    async def cycle(self, platform, name):
        st, exp = self.seq[min(self.i, len(self.seq) - 1)]
        self.i += 1
        return {"state": st, "expiry": exp}


async def cycles_test():
    tmp = Path(__file__).parent / "_tmp_cycles.json"
    # ⚠️ 必须先删旧状态：这个文件存的是**已推送去重键**，而下面假 client 的
    # 时间戳是写死的（vallis-cold/warm-2026-09-14T12:xx:00Z）。只要上一轮
    # 跑测试失败（或中途被打断）没走到末尾的 unlink，残留文件就会让下一轮
    # 判定「三条都已推过」→ sent=0 → 永远失败，**自我延续的假失败**。
    tmp.unlink(missing_ok=True)
    store = SubscriptionStore(tmp)
    sent = []

    async def send(umo, text):
        sent.append((umo, text))

    client = CycleClient()
    daemon = PushDaemon(client, store, send, FakeLogger(), interval=15)
    await store.add(Subscription(umo="group://1", platform="pc", event="山谷",
                                 rule="", until=-1, once=False, hits_left=None,
                                 windows={}))
    for _ in range(4):
        await daemon.tick()
    # 轮 1 建基线不推；之后 cold/warm/cold 三次切换都应推送
    check("山谷同一天三次状态切换推 3 条（旧键只推 1 条）", len(sent) == 3,
          f"sent={len(sent)} {sent}")
    keys = list(store.all()[0].notified)
    check("去重键含完整时间戳（同日多轮互不吞）",
          len(keys) == 3 and all("T12:" in k for k in keys), str(keys))
    tmp.unlink(missing_ok=True)


asyncio.run(cycles_test())

# ================================================================ 3b. wm -r 多密语
from core.formatters import build_whisper, fmt_wm_orders  # noqa: E402

_wm_orders_sample = [
    {"order_type": "sell", "platinum": 10, "quantity": 1, "visible": True,
     "user": {"status": "online", "ingame_name": "webSeller", "reputation": 5}},
    {"order_type": "sell", "platinum": 6, "quantity": 1, "visible": True,
     "user": {"status": "ingame", "ingame_name": "ingameSeller", "reputation": 7}},
    {"order_type": "sell", "platinum": 8, "quantity": 1, "visible": True,
     "user": {"status": "ingame", "ingame_name": "ingame2", "reputation": 0}},
    {"order_type": "buy", "platinum": 4, "quantity": 1, "visible": True,
     "user": {"status": "ingame", "ingame_name": "buyer1", "reputation": 0}},
    {"order_type": "sell", "platinum": 3, "quantity": 1, "visible": False,
     "user": {"status": "ingame", "ingame_name": "hidden", "reputation": 0}},
]
_title, _lines, _best, _pool = fmt_wm_orders("测试物品", _wm_orders_sample)
check("fmt_wm_orders 返回 4 元组且池内 3 条可见在售", len(_pool) == 3, str(len(_pool)))
check("池排序：在线优先再价格（ingame6p 第一）",
      _pool[0]["user"]["ingame_name"] == "ingameSeller"
      and _pool[0]["platinum"] == 6)
_best5 = _pool[:5]
_ws = [build_whisper(o, "Test Prime Blueprint", sell=False) for o in _best5]
check("-r 可生成 3 条卖家密语（不足 5 按实有）", len(_ws) == 3, str(len(_ws)))
check("密语模板含玩家名/物品/价格",
      '/w ingameSeller Hi! I want to buy: "Test Prime Blueprint" for 6 platinum.'
      in _ws[0], _ws[0])
_wsell = build_whisper({"user": {"ingame_name": "buyer1"}},
                       "Test Prime Blueprint", sell=True)
check("收购单密语用 I want to sell 模板",
      "I want to sell" in _wsell and "buyer1" in _wsell, _wsell)


print()
if FAILED:
    print(f"共 {len(FAILED)} 项失败：{FAILED}")
    sys.exit(1)
print("全部通过 ✔")
