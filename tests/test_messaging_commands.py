# -*- coding: utf-8 -*-
"""业务按钮回归：真实 handler + HTTP MockTransport，不联网、不调用视觉。"""

from __future__ import annotations

import asyncio
import copy
import sys
import time
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import api_client, formatters as fmt, riven_analysis as RA  # noqa: E402
from core.commands.market import MarketCommands  # noqa: E402
from core.commands.riven import RivenCommands  # noqa: E402
from core.commands.vision import VisionCommands  # noqa: E402
from core.commands.wiki_misc import WikiMiscCommands  # noqa: E402
from core.parser import parse  # noqa: E402

FAILED = []


def check(name, condition, detail=""):
    print(("[PASS] " if condition else "[FAIL] ") + name + (f"：{detail}" if not condition else ""))
    if not condition:
        FAILED.append(name)


def _raw_order(name, status, price, *, kind="sell", visible=True):
    return {
        "id": name,
        "type": kind,
        "platinum": price,
        "quantity": 1,
        "visible": visible,
        "user": {"ingameName": name, "status": status, "reputation": 10},
    }


def _auction(name, price, status="ingame", *, extra=False, bid=False):
    attrs = [{"url_name": "critical_chance", "positive": True, "value": 150}]
    if extra:
        attrs.append({"url_name": "base_damage_/_melee_damage", "positive": True, "value": 200})
    return {
        "id": name,
        "buyout_price": None if bid else price,
        "starting_price": price,
        "owner": {"ingame_name": name, "status": status, "reputation": 20},
        "item": {"name": f"Rubico {name}", "attributes": attrs, "mod_rank": 8, "re_rolls": 0},
    }


class _Host(MarketCommands, RivenCommands, WikiMiscCommands, VisionCommands):
    def __init__(self, client, buttons):
        self.client = client
        self.buttons = buttons
        self.cfg = {}
        self.page_size = 4
        self.inverse = {"绝路 Prime 枪管": [{"relic": "后纪 Axi A1", "rarity": "常见"}]}

    def _can_send_actions(self, event):
        return self.buttons

    @staticmethod
    def _event_has_image(event):
        return False

    def _relic_db(self):
        return {}, self.inverse


class _APIVirtualClock:
    """只推进API出口的限速等待；事件循环与行情总预算仍使用真实时间。"""

    def __init__(self):
        self.now = time.monotonic()
        self.waits = []
        self.time = SimpleNamespace(monotonic=lambda: self.now, time=time.time)
        # 代理模块对象，不修改全局asyncio.sleep/time.monotonic：wait_for仍真实有界。
        self.asyncio = SimpleNamespace(
            **{name: getattr(asyncio, name) for name in dir(asyncio) if name != "sleep"},
            sleep=self.sleep,
        )

    async def sleep(self, delay):
        self.waits.append(delay)
        self.now += max(0.0, delay) + 1e-6
        await asyncio.sleep(0)


class _HTTP:
    def __init__(self, clock):
        self.clock = clock
        self.requests = []
        self.client = None
        self.orders = [
            _raw_order("On110", "ingame", 110),
            _raw_order("Off1", "offline", 1),
            _raw_order("On90_*#", "ingame", 90),
            _raw_order("Web10", "online", 10),
            _raw_order("On120", "ingame", 120),
            _raw_order("On100", "ingame", 100),
            _raw_order("Hidden", "ingame", 1, visible=False),
            _raw_order("Buy20", "ingame", 20, kind="buy"),
            _raw_order("Buy40", "ingame", 40, kind="buy"),
        ]
        self.auctions = [
            _auction("Off5", 5, "offline"),
            _auction("Bid1", 1, bid=True),
            _auction("Exact200", 200),
            _auction("Super100", 100, extra=True),
            _auction("Web20", 20, "online"),
            _auction("Exact150_*#", 150),
            _auction("Exact180", 180),
            _auction("Exact160", 160),
            _auction("Exact190", 190),
        ]
        self.items = [
            {
                "id": "barrel",
                "slug": "rubico_prime_barrel",
                "tags": ["component"],
                "i18n": {
                    "zh-hans": {"name": "绝路 Prime 枪管"},
                    "en": {"name": "Rubico Prime Barrel"},
                },
            },
            {
                "id": "serration",
                "slug": "serration",
                "tags": ["mod"],
                "i18n": {"zh-hans": {"name": "膛线"}, "en": {"name": "Serration"}},
            },
        ]
        self.weapons = [
            {
                "slug": "rubico",
                "gameRef": "/Lotus/Weapons/Rubico",
                "rivenType": "rifle",
                "group": "primary",
                "disposition": 0.9,
                "i18n": {"zh-hans": {"name": "绝路"}, "en": {"name": "Rubico"}},
            },
            {
                "slug": "rubico_prime",
                "gameRef": "/Lotus/Weapons/RubicoPrime",
                "rivenType": "rifle",
                "group": "primary",
                "disposition": 0.6,
                "i18n": {"zh-hans": {"name": "绝路 Prime"}, "en": {"name": "Rubico Prime"}},
            },
        ]

    def respond(self, request):
        # 内存传输模拟1ms往返，避免零RTT让补令牌等待恰好顶满15s出口预算。
        self.clock.now += 0.001
        self.requests.append(str(request.url))
        path = request.url.path
        if path == "/v2/items":
            body = {"data": self.items}
        elif path.startswith("/v2/items/"):
            body = {"data": {"slug": path.rsplit("/", 1)[-1], "setRoot": False}}
        elif path.startswith("/v2/orders/item/"):
            body = {"data": self.orders}
        elif path == "/v2/riven/weapons":
            body = {"data": self.weapons}
        elif path == "/v1/auctions/search":
            body = {"payload": {"auctions": self.auctions}}
        else:
            raise AssertionError(f"发生了计划外HTTP请求：{request.url}")
        return httpx.Response(200, json=copy.deepcopy(body))

    async def start(self):
        self.client = api_client.WarframeClient(
            wm_v2_base="https://wm.test/v2", wm_base="https://wm.test/v1", flare_enabled=False
        )
        await self.client._http.aclose()
        self.client._http = httpx.AsyncClient(transport=httpx.MockTransport(self.respond))
        self.client._aliases = {"wm_items": {"测试配件": "rubico_prime_barrel"}}
        return self.client


def _body(reply):
    data = asdict(reply)
    data.pop("actions")
    return data


def _copies(reply):
    return [a for a in reply.actions if a.mode == "copy"]


def _copy_names(reply):
    return [a.send.split(" ", 2)[1] for a in _copies(reply)]


def _shown_names(reply, names):
    return [
        name
        for line in reply.lines
        if line.startswith("· ")
        for name in names
        if name in line.split("　")
    ]


async def main(clock):
    http = _HTTP(clock)
    client = await http.start()
    host = _Host(client, True)
    event = SimpleNamespace(unified_msg_origin="qq_official:GroupMessage:test")

    async def call(command, *, buttons=True):
        # 不同业务样本视为相隔30s的独立消息；同一handler内部仍共享真实额度。
        clock.now += 30.0
        host.buttons = buttons
        parsed = parse(command)
        handlers = {
            "wm": host._h_wm,
            "wr": host._h_wr,
            "analysis": host._h_riven_analysis,
            "damage": host._h_damage,
        }
        return await handlers[parsed.command](parsed, event, "pc")

    try:
        print("① WM：已展示卖家与密语按钮对应，-r原样保留")
        reply = await call("wm 测试配件")
        names = [o["user"]["ingameName"] for o in http.orders]
        check(
            "WM按钮按展示顺序取前4位", _copy_names(reply) == ["On90_*#", "On100", "On110", "On120"]
        )
        check("WM按钮卖家与卡面实际行一致", _copy_names(reply) == _shown_names(reply, names)[:4])
        check("WM按钮编号从1起", [a.label for a in _copies(reply)] == ["1号", "2号", "3号", "4号"])
        check("不用-r也可复制密语", not reply.whisper and len(_copies(reply)) == 4)
        check(
            "密语特殊字符不改写",
            _copies(reply)[0].send
            == '/w On90_*# Hi! I want to buy: "Rubico Prime Barrel" for 90 platinum. (warframe.market)',
        )
        check(
            "趋势带真实解析名",
            any(a.label == "价格趋势" and a.send == "趋势 绝路 Prime 枪管" for a in reply.actions),
        )
        check(
            "Prime部件有可用遗物查询名",
            any(a.label == "所属遗物" and a.send == "遗物 绝路 Prime 枪管" for a in reply.actions),
        )
        requested = list(http.requests)
        client.cache = api_client.TTLCache(maxsize=1024)
        http.requests.clear()
        old = await call("wm 测试配件", buttons=False)
        check("无按钮平台正文不变", _body(reply) == _body(old) and not old.actions)
        check("相同冷缓存下挂按钮不增加HTTP抓取", http.requests == requested)
        second = await call("wm 测试配件 -2")
        check("第二页不复用第一页卖家", _copy_names(second) == ["Web10", "Off1"])
        last = await call("wm 测试配件 -99")
        check(
            "超出页码与展示同样钳制",
            _copy_names(last) == _shown_names(last, names) == ["Web10", "Off1"],
        )
        ranked = await call("wm 测试配件 -2 -r")
        legacy = await call("wm 测试配件 -2 -r", buttons=False)
        check("-r仍给原全池前5名", len(ranked.whisper) == 5 and ranked.whisper == legacy.whisper)
        check("-r开启仍不改变正文", _body(ranked) == _body(legacy))
        buying = await call("wm 测试配件 收购")
        check(
            "收购按钮按最高价并使用卖出密语",
            _copy_names(buying) == ["Buy40", "Buy20"]
            and all("want to sell" in a.send for a in _copies(buying)),
        )
        host.inverse = {}
        absent = await call("wm 测试配件")
        check("缺遗物反查记录不造所属遗物按钮", all(a.label != "所属遗物" for a in absent.actions))
        normal = await call("wm 膛线")
        check("非Prime物品不造所属遗物按钮", all(a.label != "所属遗物" for a in normal.actions))

        print("② WR：一口价/在线/恰好匹配/分页与真实卡面同序")
        host.page_size = 8
        wr_names = [a["owner"]["ingame_name"] for a in http.auctions]
        first = await call("wr 绝路 暴击")
        check(
            "恰好匹配优先于便宜超集",
            _copy_names(first) == ["Exact150_*#", "Exact160", "Exact180", "Exact190"],
        )
        for command in ("wr 绝路 暴击", "wr 绝路 暴击 -2", "wr 绝路 暴击 -99", "wr 绝路 多重"):
            result = await call(command)
            check(
                command + "：按钮对应展示卖家",
                _copy_names(result) == _shown_names(result, wr_names),
            )
            before = await call(command, buttons=False)
            check(
                command + "：无按钮行为不变", _body(result) == _body(before) and not before.actions
            )
        loose = await call("wr 绝路 多重")
        check("放宽档保留命中率排序不重排一口价", _copy_names(loose)[0] == "Bid1")
        second = await call("wr 绝路 暴击 -2 -r")
        check(
            "WR-r仍指全池首名", len(second.whisper) == 1 and "/w Exact150_*# " in second.whisper[0]
        )
        check("WR第二页复制指本页首名", _copy_names(second)[0] == "Exact200")
        check(
            "WR真实密语保留拍卖名与金额",
            _copies(second)[0].send
            == '/w Exact200 Hi! I want to buy: "Rubico Exact200" for 200 platinum. (warframe.market)',
        )
        variant = await call("wr 绝路Prime")
        followups = [a.send for a in variant.actions if a.mode == "run"]
        check("WR后续查询保留解析出的变体", followups == ["倾向 绝路 Prime", "词条范围 绝路 Prime"])
        check(
            "不生成不存在的裂罅走势入口",
            all(not a.send.startswith("裂罅走势") for a in variant.actions),
        )
        check(
            "拍卖排序helper不改传入列表",
            fmt.wr_sorted_auctions(http.auctions) is not http.auctions
            and http.auctions[0]["id"] == "Off5",
        )

        # 真实 WR handler → main 排布入口：配置页宽允许超过两行复制按钮。
        from test_message_output import plugin
        from core.commands.base import Action, Reply

        auctions_before = http.auctions
        http.auctions = [_auction(f"Nine{i}", 100 + i) for i in range(9)]
        host.page_size = 13
        wide = await call("wr 绝路 暴击 -r")
        original = copy.deepcopy(wide)
        laid_out = plugin.WarframeSDJK._reply_actions(wide, parse("wr 绝路 暴击 -r"))
        copy_actions = [a for a in laid_out if a.mode == "copy"]
        check("WR真实入口产生9条复制动作", len(copy_actions) == 9)
        check(
            "WR前8条按4×2，第9条明确降级", [a.row for a in copy_actions] == [0] * 4 + [1] * 4 + [-1]
        )
        check(
            "WR排布保留动作语义与密语原对象",
            [(a.label, a.send, a.mode, a.owner_only) for a in copy_actions]
            == [(a.label, a.send, a.mode, a.owner_only) for a in _copies(original)]
            and wide == original,
        )
        http.auctions = auctions_before
        host.page_size = 8
        # 行上限双向检查：有分页保留末行，无分页允许第五行；超额动作不丢弃。
        for paged, limit in ((True, 4), (False, 5)):
            source = [Action(f"动作{i}", f"查询{i}", row=i // 4) for i in range(24)]
            grouped = Reply("第2/3页" if paged else "普通卡", [], actions=source)
            result = plugin.WarframeSDJK._reply_actions(grouped, parse("wm 绝路"))
            normal = result[:24]
            check(
                f"分页{paged}：行界内保留、界外降级且语义不变",
                [a.row for a in normal] == [i // 4 if i // 4 < limit else -1 for i in range(24)]
                and [(a.label, a.send, a.mode, a.owner_only) for a in normal]
                == [(a.label, a.send, a.mode, a.owner_only) for a in source],
            )
            if paged:
                check("分页动作独占末行", [a.row for a in result[24:]] == [4, 4])

        print("③ 紫卡分析与伤害：只使用已解析武器，不从报错文本猜命令")
        analysis = await call("紫卡分析 Rubico 暴击178.2 暴伤142.6")
        check(
            "分析按钮使用实体中文名",
            [a.send for a in analysis.actions]
            == ["词条范围 绝路", "倾向 绝路", "wr 绝路 暴击 暴伤"],
        )
        plain_analysis = await call("紫卡分析 Rubico 暴击178.2 暴伤142.6", buttons=False)
        check(
            "分析无按钮时原卡不变",
            _body(analysis) == _body(plain_analysis) and not plain_analysis.actions,
        )
        # 同词条挂单：本地必须核对正负名称与条数完全一致
        check(
            "同词条挂单只统计完全相同正负名称与条数的挂单",
            "· ▲暴击 ▲暴伤　本次未找到完全同词条样本　—　—" in analysis.lines,
            str(analysis.lines),
        )

        def _riven_auction(name, price, status, urls, neg=()):
            attrs = [{"url_name": u, "positive": True, "value": 1} for u in urls]
            attrs += [{"url_name": u, "positive": False, "value": 1} for u in neg]
            return {
                "id": name,
                "buyout_price": price,
                "starting_price": price,
                "visible": True,
                "closed": False,
                "owner": {"ingame_name": name, "status": status, "reputation": 1},
                "item": {"name": name, "attributes": attrs, "mod_rank": 8, "re_rolls": 0},
            }

        three = ("base_damage_/_melee_damage", "status_chance", "critical_damage")
        saved_auctions = http.auctions
        http.auctions = saved_auctions + [
            _riven_auction("P380", 380, "offline", three, ("zoom",)),
            _riven_auction("P450", 450, "ingame", three, ("zoom",)),
            _riven_auction("P520", 520, "online", three, ("zoom",)),
            _riven_auction("P900", 900, "ingame", three),
            _riven_auction("Wrong1", 1, "ingame", three[:2], ("zoom",)),
        ]
        client.cache.drop_prefix("")
        priced = await call("紫卡分析 绝路 基伤185.6 触发96.2 暴伤148.5 负变焦54")
        check(
            "同词条挂单：正负都匹配时给最低/在线最低/中位与样本数",
            "· ▲基伤 ▲触发 ▲暴伤 ▼变焦　380p　450p　450p（3 条）" in priced.lines
            and priced.actions[-1].send == "wr 绝路 基伤 触发 暴伤 负变焦",
            str(priced.lines),
        )
        with patch.object(client, "wm_riven_auctions", wraps=client.wm_riven_auctions) as queried:
            exact_empty = await call("紫卡分析 绝路 基伤185.6 触发96.2 暴伤148.5 负多重81")
        check(
            "同词条挂单：无同负面样本也只查一次，完整条件保持不变",
            "· ▲基伤 ▲触发 ▲暴伤 ▼多重　本次未找到完全同词条样本　—　—" in exact_empty.lines
            and not any("只按正面词条匹配" in x for x in exact_empty.lines)
            and exact_empty.actions[-1].send == "wr 绝路 基伤 触发 暴伤 负多重"
            and queried.await_count == 1
            and queried.call_args.kwargs["negatives"] == ["multishot"],
            str(exact_empty.lines),
        )
        # 单次完整条件查询：本地严格核对正负名称、条数与显式bool极性。
        full_pos = [("crit_damage", 148.5), ("melee_damage", 185.6), ("status_chance", 96.2)]
        full_neg = [("zoom", 54.0)]
        full_attrs = _riven_auction("exact", 700, "ingame", three, ("zoom",))["item"]["attributes"]
        weapon = await client.resolve_riven_weapon("绝路")
        dirty_attrs = {
            "缺少正面": full_attrs[1:],
            "缺少负面": full_attrs[:-1],
            "多余正面": full_attrs + [{"url_name": "critical_chance", "positive": True}],
            "多余负面": full_attrs + [{"url_name": "multishot", "positive": False}],
            "重复正面": full_attrs + [full_attrs[0]],
            "重复负面": full_attrs + [full_attrs[-1]],
            "正面集合不同": [dict(full_attrs[0], url_name="critical_chance")] + full_attrs[1:],
            "负面集合不同": full_attrs[:-1] + [dict(full_attrs[-1], url_name="multishot")],
            "极性相反": full_attrs[:-1] + [dict(full_attrs[-1], positive=True)],
            "缺少极性flag": full_attrs[:-1] + [{"url_name": "zoom", "value": 54}],
            "数字伪极性flag": full_attrs[:-1] + [dict(full_attrs[-1], positive=0)],
            "空值极性flag": full_attrs[:-1] + [dict(full_attrs[-1], positive=None)],
            "字符串极性flag": full_attrs[:-1] + [dict(full_attrs[-1], positive="false")],
            "非对象属性": full_attrs[:-1] + ["zoom"],
        }
        for label, attrs, expected in [
            ("完全对齐", full_attrs, 1),
            ("词条顺序不同仍对齐", list(reversed(full_attrs)), 1),
        ] + [(label, attrs, 0) for label, attrs in dirty_attrs.items()]:
            candidate = _riven_auction("candidate", 700, "ingame", three, ("zoom",))
            candidate["item"].update(attributes=attrs, re_rolls=999)
            with patch.object(
                client, "wm_riven_auctions", new=AsyncMock(return_value=[candidate])
            ) as queried:
                price = await host._riven_analysis_price(weapon, full_pos, full_neg, "pc")
            check(
                "严格同词条：" + label,
                price.get("sample_count") == expected
                and queried.await_count == 1
                and price.get("pos") == [s for s, _ in full_pos]
                and price.get("neg") == ["zoom"]
                and price.get("match_mode") == "exact_stat_set"
                and price.get("relaxed") is False,
                str(price),
            )
            check(
                "严格单次请求参数：" + label,
                queried.call_args.kwargs
                == {
                    "positives": sorted(three),
                    "negatives": ["zoom"],
                    "sorts": ("price_asc",),
                    "retries": 0,
                    "allow_attribute_fallback": False,
                    "timeout": 1.5,
                },
                str(queried.call_args),
            )
        # 不带负面的输入也须恰好同条数，不能接受附加负面或正面。
        for label, urls, negatives, count in (
            ("无负面完全对齐", three, (), 1),
            ("无负面不能混入负面", three, ("zoom",), 0),
            ("无负面不能混入额外正面", three + ("critical_chance",), (), 0),
        ):
            candidate = _riven_auction("candidate", 700, "ingame", urls, negatives)
            with patch.object(
                client, "wm_riven_auctions", new=AsyncMock(return_value=[candidate])
            ) as queried:
                price = await host._riven_analysis_price(weapon, full_pos, [], "pc")
            check(
                label,
                price.get("sample_count") == count
                and price.get("neg") == []
                and queried.await_count == 1,
                str(price),
            )
        for failure in (api_client.WarframeAPIError("HTTP 400"), RuntimeError("fixture")):
            with patch.object(
                client, "wm_riven_auctions", new=AsyncMock(side_effect=failure)
            ) as queried:
                price = await host._riven_analysis_price(weapon, full_pos, full_neg, "pc")
            check(
                "严格行情错误隔离且不降级：" + type(failure).__name__,
                queried.await_count == 1
                and price.get("error") == type(failure).__name__
                and price.get("pos") == [s for s, _ in full_pos]
                and price.get("neg") == ["zoom"]
                and price.get("match_mode") == "exact_stat_set"
                and price.get("relaxed") is False,
                str(price),
            )
        client.cache.drop_prefix("")
        before_queries = len([u for u in http.requests if "/auctions/search?" in u])
        clock.now += 30.0  # 直接调用的共享缓存样本同样与前一业务样本隔离。
        price_one = await host._riven_analysis_price(weapon, full_pos, full_neg, "pc")
        price_two = await host._riven_analysis_price(
            weapon, list(reversed(full_pos)), full_neg, "pc"
        )
        after_queries = len([u for u in http.requests if "/auctions/search?" in u])
        check(
            "严格同词条顺序规范化共享缓存且保留原始输入顺序",
            after_queries == before_queries + 1
            and price_one.get("sample_count") == price_two.get("sample_count") == 3
            and price_one.get("pos") == [s for s, _ in full_pos]
            and price_two.get("pos") == [s for s, _ in reversed(full_pos)],
        )
        with patch.object(
            client, "wm_riven_auctions", new=AsyncMock(side_effect=RuntimeError("boom"))
        ):
            failed = await call("紫卡分析 绝路 基伤185.6 触发96.2 暴伤148.5 负变焦54")
        check(
            "同词条挂单查询失败只隐去数字，分析与融合建议照常",
            "◆ 同词条挂单：warframe.market 暂时查不到，稍后重试" in failed.lines
            and "◆ 融合建议（预计）" in failed.lines,
        )
        for command in (
            "紫卡分析 绝路 基伤185.6 暴伤135 负变焦54 ？？？",
            "紫卡分析 绝路 基伤185.6 触发96.2 负变焦54",
            "紫卡分析 绝路 基伤216.6 暴伤157.5 射速78.8 倾向1.4",
            "紫卡分析 绝路 基伤185.6 基伤185.6 触发96.2 负变焦54",
            "紫卡分析 绝路 基伤185.6 触发96.2 暴伤50 负变焦54",
        ):
            with patch.object(host, "_riven_analysis_price", new=AsyncMock()) as price_spy:
                uncertain = await call(command)
            check(
                "行情不使用未确认或推断输入：" + command,
                price_spy.await_count == 0
                and all(a.label != "同词条拍卖" for a in uncertain.actions)
                and not any("同词条挂单" in line for line in uncertain.lines),
            )
        with patch.object(
            host, "_riven_analysis_price", new=AsyncMock(return_value=None)
        ) as observed:
            await call("紫卡分析 绝路 基伤1856 触发962 暴伤1485 负变焦540")
        check(
            "行情只用修正后的实际识别词条",
            observed.call_args.args[1]
            == [("melee_damage", 185.6), ("status_chance", 96.2), ("crit_damage", 148.5)]
            and observed.call_args.args[2] == [("zoom", 54.0)],
        )
        with patch.object(
            client,
            "wm_riven_auctions",
            new=AsyncMock(side_effect=api_client.WarframeAPIError("限速")),
        ) as limited:
            unavailable = await call("紫卡分析 绝路 基伤185.6 触发96.2 暴伤148.5 负变焦54")
        check(
            "限速失败不尝试正面放宽，且禁用无条件回退",
            limited.await_count == 1
            and limited.call_args.kwargs["allow_attribute_fallback"] is False
            and limited.call_args.kwargs["retries"] == 0
            and limited.call_args.kwargs["timeout"] == 1.5
            and "◆ 融合建议（预计）" in unavailable.lines,
        )
        stopped = asyncio.Event()

        async def never_finishes(*args, **kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        with (
            patch("core.commands.riven.RIVEN_ANALYSIS_PRICE_BUDGET_S", 0.01),
            patch.object(client, "wm_riven_auctions", new=never_finishes),
        ):
            timed_out = await asyncio.wait_for(
                call("紫卡分析 绝路 基伤185.6 触发96.2 暴伤148.5 负变焦54"), timeout=0.5
            )
        check(
            "附加行情总预算超时后取消查询，原分析和融合继续",
            stopped.is_set()
            and "◆ 同词条挂单：warframe.market 暂时查不到，稍后重试" in timed_out.lines
            and "◆ 融合建议（预计）" in timed_out.lines,
        )
        http.auctions = saved_auctions
        client.cache.drop_prefix("")
        print("④ 融合预测：真实文本/图片 handler，展示契约独立于排版")
        direct_command = "紫卡分析 绝路 基伤185.6 触发96.2 暴伤148.5 负变焦54"
        preview = (await call(direct_command)).riven_splice
        check(
            "融合预览schema与布局", preview["schema_version"] == 1 and preview["counts"] == [3, 1]
        )
        direct = next(r for r in preview["direct"] if r["output"] == "status_damage")
        check(
            "实际handler只继承被消耗词条，不用无关的满暴伤",
            preview["status"] == "ready"
            and abs(direct["prediction"]["value"] - 185.6 * 90 / 165) < 1e-9
            and 49 < direct["prediction"]["position_pct"] < 51,
        )
        negative = (await call("紫卡分析 绝路 基伤185.6 暴伤135 射速67.5 负多重89.1")).riven_splice
        negative_pair = next(r for r in negative["direct"] if r["output"] == "weakpoint_damage")
        check(
            "负面幅度上限直接继承高随机值",
            abs(negative_pair["prediction"]["position_pct"] - 100) < 1e-9
            and abs(negative_pair["prediction"]["value"] - 278.4375) < 1e-9,
        )
        missing = (await call("紫卡分析 绝路 基伤185.6 暴伤135 射速67.5 负变焦54")).riven_splice
        missing_pair = next(r for r in missing["missing_one"] if r["output"] == "status_damage")
        check(
            "差一条给条件上下界，不能填写唯一预测值",
            missing_pair["missing"] == ["status_chance"]
            and missing_pair["prediction"]["value"] is None
            and missing_pair["prediction"]["position_pct"] is None
            and missing_pair["prediction"]["position_max_pct"] == 100
            and "retain_existing_inputs" in missing["assumptions"],
        )
        for command, status in (
            ("紫卡分析 绝路 毒气101.3 暴伤135 射速67.5 负变焦54", "already_spliced"),
            ("紫卡分析 绝路 基伤185.6 基伤185.6 触发96.2 负变焦54", "invalid"),
            ("紫卡分析 绝路 基伤185.6 暴伤135 负变焦54 ？？？", "incomplete"),
            ("紫卡分析 绝路 基伤185.6 触发96.2 负变焦54", "incomplete"),
            ("紫卡分析 绝路 神秘词条5 基伤185.6 触发96.2 暴伤148.5 负变焦54", "incomplete"),
            ("紫卡分析 绝路 基伤92.8 触发50.6 暴伤67.5 负变焦27 倾向0.6", "incomplete"),
        ):
            report = (await call(command)).riven_splice
            check(
                "融合预测阻断：" + command,
                report["status"] == status and not report["direct"] and not report["missing_one"],
                str(report),
            )
        # 融合建议超过首屏 6 条：图片 / 纯文字出续页，QQ Markdown 一条列全部
        long_card = await call("紫卡分析 绝路 基伤194.9 毒伤106.3 火伤106.3 负弹药45.0")
        # 去重后：每种融合结果一行（同一 output 只留最高方案）
        total = len(
            {
                r["output"]
                for r in long_card.riven_splice["direct"] + long_card.riven_splice["missing_one"]
            }
        )
        first_rows = (
            [x for x in long_card.pages[0][1] if x.startswith("· ⟦v⟧")] if long_card.pages else []
        )
        more_rows = (
            [x for x in long_card.pages[1][1] if x.startswith("· ⟦v⟧")] if long_card.pages else []
        )
        check(
            "真实handler：去重后超过6种时首页6条+续页其余，合计等于不同融合结果数",
            total > 6
            and len(long_card.pages) == 2
            and long_card.pages[0] == (long_card.title, long_card.lines)
            and len(first_rows) == 6
            and len(first_rows) + len(more_rows) == total
            and long_card.pages[1][0] == "紫卡分析 · 融合建议续",
            str(long_card.pages[1] if long_card.pages else long_card.lines),
        )
        check(
            "真实handler：QQ Markdown 正文列全部预测、区间用全角～",
            long_card.markdown is not None
            and long_card.markdown.count("    - 消耗 ") == total
            and "~" not in long_card.markdown
            and "*融合词条 · 理论区间 · 距中 · 区间位*" in long_card.markdown,
            str(long_card.markdown)[:300],
        )
        # 显式的分析 Markdown 也须遵守面板文字卡开关；关闭时保留完整两页。
        for md_enabled in (False, True):
            host.cfg["qq_markdown_cards"] = md_enabled
            switched = await call("紫卡分析 绝路 基伤194.9 毒伤106.3 火伤106.3 负弹药45.0")
            check(
                f"真实handler：Markdown开关={md_enabled}，关闭后仍保留完整续页",
                bool(switched.markdown) == md_enabled and switched.pages == long_card.pages,
            )
        host.cfg.pop("qq_markdown_cards")
        short_card = await call("紫卡分析 绝路 基伤185.6 触发96.2 暴伤148.5 负变焦54")
        spliced_card = await call("紫卡分析 绝路 毒气101.3 暴伤135 射速67.5 负变焦54")
        check(
            "真实handler：不超过6条不分页；已有融合词条停止预测且不出续页",
            not short_card.pages
            and short_card.markdown is not None
            and not spliced_card.pages
            and "◆ 融合建议：已有融合词条（毒气），不能再融合" in spliced_card.lines
            and not any(x.startswith("· ⟦v⟧") for x in spliced_card.lines),
        )
        with patch.object(RA, "splice_preview", wraps=RA.splice_preview) as captured:
            inferred = await call("紫卡分析 绝路 基伤216.6 暴伤157.5 射速78.8 倾向1.4")
            check(
                "虚构负面占位不传入融合预测",
                captured.call_args.args[1] == []
                and inferred.riven_splice["status"] == "incomplete"
                and "incomplete_stats" in inferred.riven_splice["reasons"],
                str(captured.call_args),
            )

        # 古董/例外不可仅按当前池断言非法，但未核实前也不作可直接融合保证。
        http.weapons.append(
            {
                "slug": "verglas",
                "rivenType": "rifle",
                "group": "primary",
                "disposition": 1.2,
                "i18n": {"zh-hans": {"name": "冰凇"}, "en": {"name": "Verglas"}},
            }
        )
        with patch.object(
            client,
            "resolve_riven_weapon",
            new=AsyncMock(
                return_value={
                    "url_name": "verglas",
                    "zh": "冰凇",
                    "en": "Verglas",
                    "riven_type": "rifle",
                    "group": "primary",
                    "disposition": 1.2,
                }
            ),
        ):
            vintage = (
                await call("紫卡分析 冰凇 基伤185.6 触发101.3 暴伤135 负变焦54")
            ).riven_splice
        http.weapons.pop()
        check(
            "当前非物理词条可用性未确认时不保证可融合",
            vintage["status"] == "incomplete"
            and "unverified_weapon_traits" in vintage["reasons"]
            and not vintage["direct"],
        )

        # 不调用视觉模型：以公开机制的合成卡面行验证真实图片入口。
        host._ocr_busy = set()
        host._ocr_last = {}
        image_data = {
            "weapon": "Rubico",
            "positive": [["伤害", 185.6], ["触发几率", 96.2], ["暴击伤害", 148.5]],
            "negative": [["变焦", 54]],
            "lines": ["+185.6% 伤害", "+96.2% 触发几率", "+148.5% 暴击伤害", "-54% 变焦"],
        }
        with (
            patch.object(host, "_event_has_image", return_value=True),
            patch.object(host, "_vision_gate", return_value=(None, "synthetic")),
            patch.object(host, "_image_data_urls", new=AsyncMock(return_value=["synthetic-image"])),
            patch.object(host, "_extract_riven_from_image", new=AsyncMock(return_value=image_data)),
        ):
            ocr = await call("紫卡分析")
        check(
            "逐行OCR与手输生成相同融合数据",
            ocr.riven_splice == preview and not host._ocr_busy,
            str(ocr.riven_splice),
        )
        sem_unknown = copy.deepcopy(image_data)
        sem_unknown.pop("lines")
        sem_unknown["positive"].append(["不存在的词条", 1])
        with (
            patch.object(host, "_event_has_image", return_value=True),
            patch.object(host, "_vision_gate", return_value=(None, "synthetic")),
            patch.object(host, "_image_data_urls", new=AsyncMock(return_value=["synthetic-image"])),
            patch.object(
                host, "_extract_riven_from_image", new=AsyncMock(return_value=sem_unknown)
            ),
            patch.object(host, "_riven_analysis_price", new=AsyncMock()) as unknown_price,
            patch.object(RA, "splice_preview", wraps=RA.splice_preview) as unknown_splice,
            patch.object(
                fmt, "fmt_riven_analysis", wraps=fmt.fmt_riven_analysis
            ) as unknown_analysis,
        ):
            unknown_ocr = await call("紫卡分析")
        check(
            "语义OCR未知词条保留原始结构待核对，不进入分析、融合或估价",
            "待核对" in (unknown_ocr.raw_text or "")
            and "不存在的词条" in unknown_ocr.raw_text
            and "原始识别结构" in unknown_ocr.raw_text
            and not unknown_ocr.lines
            and not unknown_ocr.pages
            and not unknown_ocr.actions
            and unknown_ocr.riven_splice is None
            and unknown_price.await_count == 0
            and unknown_splice.call_count == 0
            and unknown_analysis.call_count == 0,
            str(unknown_ocr),
        )
        mismatched = copy.deepcopy(image_data)
        mismatched["lines"] = ["+185.6% 伤害", "+96.3% 触发几率", "-54% 变焦"]
        with (
            patch.object(host, "_event_has_image", return_value=True),
            patch.object(host, "_vision_gate", return_value=(None, "synthetic")),
            patch.object(host, "_image_data_urls", new=AsyncMock(return_value=["synthetic-image"])),
            patch.object(host, "_extract_riven_from_image", new=AsyncMock(return_value=mismatched)),
            patch.object(host, "_riven_analysis_price", new=AsyncMock()) as disputed_price,
            patch.object(RA, "splice_preview", wraps=RA.splice_preview) as disputed_splice,
            patch.object(
                fmt, "fmt_riven_analysis", wraps=fmt.fmt_riven_analysis
            ) as disputed_analysis,
        ):
            disputed = await call("紫卡分析")
        check(
            "OCR两路同词条异值保留原文待核对，不进入分析、融合或估价",
            "待核对" in (disputed.raw_text or "")
            and "原始识别结构" in disputed.raw_text
            and "148.5" in disputed.raw_text
            and "逐行原文" in disputed.raw_text
            and all(line in disputed.raw_text for line in mismatched["lines"])
            and not disputed.lines
            and not disputed.pages
            and not disputed.actions
            and disputed.riven_splice is None
            and disputed_price.await_count == 0
            and disputed_splice.call_count == 0
            and disputed_analysis.call_count == 0,
            str(disputed),
        )
        decimal = (await call("紫卡分析 绝路 基伤1856 触发962 暴伤1485 负变焦540")).riven_splice
        check("修正小数点后使用最终实际词条做预测", decimal == preview)
        damage = await call("伤害 布拉玛 爆头")
        check(
            "伤害fill使用已解析武器名",
            len(damage.actions) == 1
            and damage.actions[0].send == "伤害 赤毒·布拉玛 对 "
            and damage.actions[0].mode == "fill",
        )
        changed = await call(damage.actions[0].send + "重机枪手 150级")
        check("伤害fill补全后可真实复算", any("150级" in line for line in changed.lines))
        plain_damage = await call("伤害 布拉玛 爆头", buttons=False)
        check(
            "伤害无按钮平台原样输出",
            _body(damage) == _body(plain_damage) and not plain_damage.actions,
        )
        for command in ("wm", "wr", "紫卡分析", "伤害"):
            usage = await call(command)
            check(
                command + "：明确用法示例仅填入",
                len(usage.actions) == 1
                and usage.actions[0].label == "照示例发"
                and usage.actions[0].mode == "fill"
                and parse(usage.actions[0].send).command is not None,
            )
            plain_usage = await call(command, buttons=False)
            check(
                command + "：无按钮用法卡不变",
                _body(usage) == _body(plain_usage) and not plain_usage.actions,
            )
        invalid = await call("伤害 ZZZ不存在武器")
        check("任意错误卡不猜按钮", not invalid.actions)
        host._can_send_actions = None
        no_bridge = await host._h_wm(parse("wm 测试配件"), event, "pc")
        check("能力方法不可调用时保持原输出", not no_bridge.actions)
    finally:
        await client.close()


if __name__ == "__main__":
    # 临时隔离可变API补全数据，只用内嵌HTTP桩；伤害公式仍读正式本地种子。
    with TemporaryDirectory(prefix="messaging-commands-") as data_dir:
        clock = _APIVirtualClock()
        with (
            patch.object(api_client, "DATA_DIR", Path(data_dir)),
            patch.object(api_client, "time", clock.time),
            patch.object(api_client, "asyncio", clock.asyncio),
        ):
            asyncio.run(main(clock))
        check("桩网络仍走真实限速等待并推进虚拟时间", any(delay > 0 for delay in clock.waits))
    if FAILED:
        raise SystemExit("失败：" + "、".join(FAILED))
    print("业务按钮真实入口全部通过")
