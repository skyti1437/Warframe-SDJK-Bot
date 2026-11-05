"""完整 main 输出入口回归；替换宿主/SDK和图片渲染，不复制输出算法、不联网。"""

from __future__ import annotations

import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class Plain:
    def __init__(self, text):
        self.text = text


class At:
    def __init__(self, qq):
        self.qq = qq


class Image:
    def __init__(self, path):
        self.path = path

    @classmethod
    def fromFileSystem(cls, path):
        return cls(path)


class Result:
    def __init__(self, chain=()):
        self.chain = list(chain)
        self.markdown = True

    def message(self, text):
        self.chain.append(Plain(text))
        return self

    def use_markdown(self, enabled):
        self.markdown = enabled
        return self


def install_host():
    api = types.ModuleType("astrbot.api")
    api.AstrBotConfig = dict
    api.logger = types.SimpleNamespace(
        **{
            name: lambda *args, **kwargs: None
            for name in ("debug", "info", "warning", "error", "exception")
        }
    )
    event = types.ModuleType("astrbot.api.event")
    event.AstrMessageEvent = object
    event.MessageChain = Result
    event.filter = types.SimpleNamespace(
        EventMessageType=types.SimpleNamespace(ALL="all"),
        event_message_type=lambda *args: lambda fn: fn,
        on_decorating_result=lambda: lambda fn: fn,
    )
    components = types.ModuleType("astrbot.api.message_components")
    components.Image, components.Plain, components.At = Image, Plain, At
    star = types.ModuleType("astrbot.api.star")
    star.Context = star.Star = type("Host", (), {})
    star.register = lambda *args, **kwargs: lambda cls: cls
    for name, module in {
        "astrbot": types.ModuleType("astrbot"),
        "astrbot.api": api,
        "astrbot.api.event": event,
        "astrbot.api.message_components": components,
        "astrbot.api.star": star,
    }.items():
        sys.modules[name] = module


install_host()

import main as plugin  # noqa: E402
from core.commands.base import Action, Reply  # noqa: E402
from core.messaging import ImageDelivery, MessagingBridge  # noqa: E402
from core.parser import parse  # noqa: E402


class GroupMessage:
    group_openid = "fixture-group"


class C2CMessage:
    author = types.SimpleNamespace(user_openid="fixture-user")


class Event:
    def __init__(self, platform="qq_official", text="查询", private=False):
        self.platform, self.message_str = platform, text
        self.message_obj = types.SimpleNamespace(
            raw_message=C2CMessage() if private else GroupMessage(),
            group_id="" if private else "fixture-group",
            session_id="fixture-user" if private else "fixture-group",
            self_id="fixture-bot",
            message_id="fixture-message",
            message_str=text,
        )
        self.unified_msg_origin = "fixture:GroupMessage:fixture-group"
        self.stopped = False
        self.result = None
        self.deliveries = []
        self.button_error = None
        self.bot = types.SimpleNamespace(
            api=types.SimpleNamespace(post_group_message=self.post, post_c2c_message=self.post)
        )

    async def post(self, **payload):
        self.deliveries.append(("buttons", payload))
        if self.button_error:
            raise self.button_error
        return {"id": "fixture-delivery"}

    def get_platform_name(self):
        return self.platform

    def get_platform_id(self):
        return "fixture-instance"

    def get_sender_id(self):
        return "fixture-user"

    def get_messages(self):
        return [Plain(self.message_str)]

    def plain_result(self, text):
        return Result([Plain(text)])

    def chain_result(self, chain):
        return Result(chain)

    def get_result(self):
        return self.result

    def stop_event(self):
        self.stopped = True


def new_plugin():
    obj = object.__new__(plugin.WarframeSDJK)
    obj.cfg = {"strip_markdown": True}
    obj.render_mode = "image"
    obj.renderer = types.SimpleNamespace(available=True)
    obj.context = types.SimpleNamespace(get_platform_inst=lambda _: None)
    obj._messaging = MessagingBridge(obj.context)
    obj._messaging._sdk = lambda name: (
        types.SimpleNamespace(GroupMessage=GroupMessage, C2CMessage=C2CMessage)
        if name == "botpy.message"
        else None
    )
    obj._render_card = AsyncMock(
        side_effect=lambda title, lines, footer: "IMAGE:" + "|".join(lines)
    )
    obj.cleanups = []
    obj._schedule_card_cleanup = lambda path, delay=120.0: obj.cleanups.append((path, delay))
    return obj


async def consume(obj, event, generator):
    results = []
    async for result in generator:
        event.result = result
        await obj._on_decorating_result(event)
        event.deliveries.append(("body", result))
        results.append(result)
    return results


def body_parts(results):
    return [
        segment.path if isinstance(segment, Image) else segment.text
        for result in results
        for segment in result.chain
    ]


class NumericConfigTests(unittest.TestCase):
    def initialize(self, config, data_dir):
        """调用真实插件构造入口，仅替换宿主和有外部副作用的依赖。"""
        with (
            patch.object(plugin.Star, "__init__", lambda self, context: None),
            patch.object(plugin, "_resolve_data_dir", return_value=data_dir),
            patch.object(plugin, "_migrate_legacy_user_data"),
            patch.object(plugin.core_paths, "set_run_dir"),
            patch.object(plugin, "migrate_legacy_user_fonts", return_value=[]),
            patch.object(plugin, "ImageRenderer"),
            patch.object(plugin, "GroupStore"),
            patch.object(plugin, "SubscriptionStore"),
            patch.object(plugin, "WebPanel"),
            patch.object(
                plugin,
                "WarframeClient",
                side_effect=lambda **kw: types.SimpleNamespace(timeout=kw["timeout"]),
            ),
            patch.object(
                plugin,
                "PushDaemon",
                side_effect=lambda *args, **kw: types.SimpleNamespace(interval=kw["interval"]),
            ),
        ):
            return plugin.WarframeSDJK(types.SimpleNamespace(), config)

    def test_invalid_numeric_config_falls_back_during_real_initialization(self):
        values = (
            0,
            -1,
            0.5,
            "0",
            float("nan"),
            float("inf"),
            float("-inf"),
            "NaN",
            "Inf",
            None,
            "bad",
            "",
            True,
        )
        with tempfile.TemporaryDirectory(prefix="wf-config-init-") as td:
            for value in values:
                with self.subTest(value=value):
                    config = json.loads(
                        json.dumps(
                            {
                                "page_size": value,
                                "http_timeout": value,
                                "push_interval": value,
                                "text_chunk_limit": value,
                            }
                        )
                    )
                    obj = self.initialize(config, Path(td))
                    self.assertEqual(obj.page_size, 12)
                    self.assertEqual(obj.client.timeout, 15)
                    self.assertEqual(obj.push.interval, 45)
                    self.assertEqual(obj.cfg["text_chunk_limit"], 0)
                    self.assertEqual(json.dumps(config["text_chunk_limit"]), json.dumps(value))

    def test_numeric_lower_bounds_and_zero_semantics_during_real_initialization(self):
        with tempfile.TemporaryDirectory(prefix="wf-config-init-") as td:
            for chunk in (0, 200):
                config = {
                    "page_size": 1,
                    "http_timeout": 1,
                    "push_interval": 15,
                    "scan_cooldown": 0,
                    "text_chunk_limit": chunk,
                }
                obj = self.initialize(config, Path(td))
                self.assertEqual((obj.page_size, obj.client.timeout, obj.push.interval), (1, 1, 15))
                self.assertEqual(plugin._num(obj.cfg, "scan_cooldown", 15), 0)
                self.assertEqual(obj._messaging._chunk_limit("telegram"), chunk or 4000)
            obj = self.initialize({"push_interval": 14, "text_chunk_limit": 199}, Path(td))
            self.assertEqual(obj.push.interval, 45)
            self.assertEqual(obj.cfg["text_chunk_limit"], 0)


class OutputTests(unittest.IsolatedAsyncioTestCase):
    def merge_fixture(self, outcome=None):
        obj, event = new_plugin(), Event()
        event.bot.api._http = types.SimpleNamespace(
            _token=types.SimpleNamespace(app_id="fixture-app")
        )
        obj._messaging._qq_media = types.SimpleNamespace()

        async def send(event, path, actions, **kw):
            event.deliveries.append(("merged", (path, list(actions), kw)))
            return outcome or ImageDelivery(sent=True)

        obj._messaging.send_image_card = AsyncMock(side_effect=send)
        return obj, event

    async def test_slack_defaults_to_text_cards_unless_forced_image(self):
        for platform, text, image in (
            ("slack", "查询", False),
            ("slack", "查询 -t", True),
            ("telegram", "查询", True),
        ):
            with self.subTest(platform=platform, text=text):
                obj, event = new_plugin(), Event(platform, text)
                results = await consume(
                    obj, event, obj._build_results(event, Reply("标题", ["正文行"]), parse(text))
                )
                parts = body_parts(results)
                self.assertEqual(any(p.startswith("IMAGE:") for p in parts), image, parts)
                if not image:
                    self.assertIn("正文行", "".join(parts))

    async def test_merged_image_uses_one_delivery_and_keeps_original_reply(self):
        obj, event = self.merge_fixture()
        reply = Reply("title", ["BODY"], actions=[Action("go", "arbitration")])
        results = await consume(obj, event, obj._build_results(event, reply, parse("query -t")))
        self.assertEqual(results, [])
        self.assertEqual([x[0] for x in event.deliveries], ["merged"])
        self.assertEqual(obj._render_card.await_count, 1)
        self.assertEqual(len(reply.actions), 1)
        self.assertEqual(len(obj.cleanups), 1)

    async def test_merged_multi_page_only_last_page_has_buttons(self):
        for count in (2, 5):
            obj, event = self.merge_fixture()
            reply = Reply(
                pages=[(str(i), [f"BODY{i}"]) for i in range(count)],
                actions=[Action("go", "arbitration")],
            )
            results = await consume(obj, event, obj._build_results(event, reply, parse("query -t")))
            self.assertEqual([x[0] for x in event.deliveries], ["body"] * (count - 1) + ["merged"])
            self.assertEqual(len(results), count - 1)
            self.assertEqual(obj._render_card.await_count, count)
            self.assertTrue(obj._messaging.send_image_card.call_args.args[2])
            self.assertEqual(
                obj._messaging.send_image_card.call_args.kwargs["remaining_messages"], 6 - count
            )

    async def test_merged_upload_failure_falls_back_without_rerendering_pages(self):
        obj, event = self.merge_fixture(ImageDelivery(fallback=True))
        reply = Reply(pages=[("a", ["ONE"]), ("b", ["TWO"])], actions=[Action("go", "arbitration")])
        results = await consume(obj, event, obj._build_results(event, reply, parse("query -t")))
        self.assertEqual([x[0] for x in event.deliveries], ["body", "merged", "body", "buttons"])
        self.assertEqual(body_parts(results).count("IMAGE:ONE"), 1)
        self.assertEqual(body_parts(results).count("IMAGE:TWO"), 1)
        self.assertEqual(obj._render_card.await_count, 2)

    async def test_merged_unknown_delivery_does_not_resend_image_or_buttons(self):
        obj, event = self.merge_fixture(ImageDelivery(uncertain=True))
        results = await consume(
            obj,
            event,
            obj._build_results(
                event,
                Reply("x", ["BODY"], actions=[Action("go", "arbitration")]),
                parse("query -t"),
            ),
        )
        self.assertEqual(results, [])
        self.assertEqual([x[0] for x in event.deliveries], ["merged"])

    async def test_merged_unknown_keeps_unsupported_action_manual_fallback(self):
        missing = Action("MANUAL_ONLY", "fill_me ", "fill", row=99)
        obj, event = self.merge_fixture(ImageDelivery(uncertain=True, action_fallback=[missing]))
        reply = Reply("x", ["BODY"], actions=[Action("go", "arbitration"), missing])
        results = await consume(obj, event, obj._build_results(event, reply, parse("query -t")))
        self.assertIn("MANUAL_ONLY", "".join(body_parts(results)))
        self.assertNotIn("IMAGE:BODY", body_parts(results))
        self.assertNotIn("buttons", [x[0] for x in event.deliveries])

    async def test_merged_last_page_failure_at_budget_keeps_manual_actions_in_caption(self):
        obj, event = self.merge_fixture(ImageDelivery(fallback=True))
        reply = Reply(
            pages=[(str(i), [f"BODY{i}"]) for i in range(5)], actions=[Action("go", "arbitration")]
        )
        results = await consume(obj, event, obj._build_results(event, reply, parse("query -t")))
        self.assertEqual([x[0] for x in event.deliveries].count("body"), 5)
        self.assertNotIn("buttons", [x[0] for x in event.deliveries])
        self.assertIn("arbitration", body_parts(results)[-1])

    async def test_merged_switch_off_and_partial_render_preserve_old_order(self):
        obj, event = self.merge_fixture()
        obj.cfg["qq_merge_image_actions"] = False
        await consume(
            obj,
            event,
            obj._build_results(
                event,
                Reply("x", ["BODY"], actions=[Action("go", "arbitration")]),
                parse("query -t"),
            ),
        )
        obj._messaging.send_image_card.assert_not_awaited()
        obj, event = self.merge_fixture()
        obj._render_card = AsyncMock(side_effect=["ONE", None, "THREE"])
        reply = Reply(
            pages=[("a", ["BODY1"]), ("b", ["BODY2"]), ("c", ["BODY3"])],
            actions=[Action("go", "arbitration")],
        )
        results = await consume(obj, event, obj._build_results(event, reply, parse("query -t")))
        obj._messaging.send_image_card.assert_not_awaited()
        parts = body_parts(results)
        self.assertEqual(parts[0], "ONE")
        self.assertIn("BODY2", parts[1])
        self.assertEqual(parts[2], "THREE")

    async def test_merged_tail_and_unsupported_actions_are_preserved_once(self):
        missing = Action("manual", "complete ", "fill")
        obj, event = self.merge_fixture(ImageDelivery(sent=True, action_fallback=[missing]))
        reply = Reply(
            "x",
            ["BODY"],
            actions=[Action("go", "arbitration")],
            extra_text="https://example.invalid/a_b",
        )
        results = await consume(obj, event, obj._build_results(event, reply, parse("query -t")))
        tail = "".join(body_parts(results))
        self.assertEqual(tail.count("https://example.invalid/a_b"), 1)
        self.assertEqual(tail.count("complete "), 1)
        self.assertEqual([x[0] for x in event.deliveries], ["merged", "body"])

    async def test_proactive_native_success_or_error_never_resends(self):
        for error in (None, TimeoutError("unknown delivery")):
            obj = new_plugin()
            obj.context.send_message = AsyncMock(return_value=True)
            native = AsyncMock(return_value=True, side_effect=error)
            obj._messaging.send_proactive = native
            if error:
                with self.assertRaises(TimeoutError):
                    await obj._push_send("synthetic:GroupMessage:target", "Player_Name *A*")
            else:
                await obj._push_send("synthetic:GroupMessage:target", "Player_Name *A*")
            native.assert_awaited_once()
            obj.context.send_message.assert_not_awaited()

    async def test_push_subscribers_reach_host_with_platform_specific_markdown(self):
        for platform in ("qq_official", "aiocqhttp"):
            with self.subTest(platform=platform):
                obj = new_plugin()
                adapter = types.SimpleNamespace(meta=lambda: types.SimpleNamespace(name=platform))
                obj.context.get_platform_inst = lambda instance: (
                    adapter if instance == "support" else None
                )
                obj.context.send_message = AsyncMock(return_value=True)
                obj._messaging.send_proactive = AsyncMock(return_value=False)
                owners = ["A" * 32, "B" * 32]
                body = "订阅提醒：Player_Name *Prime* [奖励]，第1次。&<>"
                await obj._push_send(
                    "support:GroupMessage:group-fixture",
                    body,
                    at=[owners[0], "all", owners[1], owners[0], "ALL", " "],
                )
                obj.context.send_message.assert_awaited_once()
                umo, chain = obj.context.send_message.call_args.args
                self.assertEqual(umo, "support:GroupMessage:group-fixture")
                self.assertEqual([c.qq for c in chain.chain if isinstance(c, At)], owners)
                expected = "\n订阅提醒：Player&#95;Name &#42;Prime&#42; &#91;奖励&#93;，第1次。&#38;&#60;&#62;"
                self.assertEqual(
                    chain.chain[-1].text, expected if platform == "qq_official" else body
                )
                self.assertEqual(chain.markdown, platform == "qq_official")

    async def test_push_without_mention_or_with_missing_component_stays_plain(self):
        obj = new_plugin()
        adapter = types.SimpleNamespace(meta=lambda: types.SimpleNamespace(name="qq_official"))
        obj.context.get_platform_inst = lambda _: adapter
        obj._messaging.send_proactive = AsyncMock(return_value=False)
        for targets in ([], ["all", "ALL", ""]):
            obj.context.send_message = AsyncMock(return_value=True)
            await obj._push_send("support:GroupMessage:fixture", "Player_Name *Prime*", at=targets)
            chain = obj.context.send_message.call_args.args[1]
            self.assertFalse(chain.markdown)
            self.assertEqual(
                [(type(c), c.text) for c in chain.chain], [(Plain, "Player_Name *Prime*")]
            )
        obj.context.send_message = AsyncMock(return_value=True)
        with patch.object(
            sys.modules["astrbot.api.message_components"], "At", side_effect=RuntimeError
        ):
            await obj._push_send(
                "support:GroupMessage:fixture", "Player_Name *Prime*", at=["A" * 32]
            )
        chain = obj.context.send_message.call_args.args[1]
        self.assertFalse(chain.markdown)
        self.assertEqual(chain.chain[0].text, "Player_Name *Prime*")

    async def test_buttons_preserve_platform_and_page_query(self):
        obj, event = new_plugin(), Event()
        reply = Reply(
            title="查询（第2/3页）",
            lines=["BODY"],
            actions=[
                Action("趋势", "趋势 绝路"),
                Action("补充", "伤害 绝路 对 ", "fill"),
                Action("密语", "/w Player_Name *A*", "copy"),
            ],
        )
        parsed = parse('wm "Soma Prime" -w -ps -2')
        await consume(obj, event, obj._build_results(event, reply, parsed))
        rows = event.deliveries[-1][1]["keyboard"]["content"]["rows"]
        sends = [button["action"]["data"] for row in rows for button in row["buttons"]]
        self.assertIn("-ps 趋势 绝路", sends)
        self.assertIn("-ps 伤害 绝路 对 ", sends)
        self.assertIn("/w Player_Name *A*", sends)
        self.assertIn('wm "Soma Prime" -w -ps', sends)
        self.assertIn('wm "Soma Prime" -w -ps -3', sends)
        self.assertNotIn('wm "Soma Prime" -w -ps -1', sends)

    def test_button_rows_wrap_whispers_then_shortcuts_then_paging(self):
        reply = Reply(
            title="查询（第2/3页）",
            lines=["BODY"],
            whisper=[f"/w P{i} hi" for i in range(1, 7)],
        )
        acts = plugin.WarframeSDJK._reply_actions(reply, parse("wm 绝路 -2"))
        rows = {}
        for a in acts:
            rows.setdefault(a.row, []).append(a.label)
        self.assertEqual(rows[0], ["1号密语", "2号密语", "3号密语", "4号密语"])
        self.assertEqual(rows[1], ["5号密语", "6号密语"])
        self.assertEqual(rows[4], ["上一页", "下一页"])
        self.assertTrue(all(len(v) <= 4 for v in rows.values()))
        help_rows = {}
        for a in plugin.WarframeSDJK._reply_actions(Reply("帮助", []), parse("帮助")):
            help_rows.setdefault(a.row, []).append(a.send)
        self.assertEqual(help_rows[0], ["突击", "执刑官", "电波", "奸商"])
        self.assertEqual(help_rows[1], ["裂隙", "仲裁", "时效", "舰队进度"])
        # wm：指令自带「N号」复制按钮时不再补第二套「N号密语」；趋势/遗物排在复制行之后
        wm = Reply(
            "wm",
            [],
            whisper=[f"/w P{i} hi" for i in range(1, 6)],
            actions=[Action(f"{i}号", f"/w P{i} hi", "copy") for i in range(1, 5)]
            + [Action("价格趋势", "趋势 x", row=1), Action("所属遗物", "遗物 x", row=1)],
        )
        wm_rows = {}
        for a in plugin.WarframeSDJK._reply_actions(wm, parse("wm 绝路")):
            wm_rows.setdefault(a.row, []).append(a.label)
        self.assertEqual(wm_rows, {0: ["1号", "2号", "3号", "4号"], 1: ["价格趋势", "所属遗物"]})
        # wr：自带复制按钮按每行 5 个写的 row 一律改排成每行 4 个
        wr = Reply(
            "wr",
            [],
            actions=[Action(f"{i}号", f"/w W{i}", "copy", row=(i - 1) // 5) for i in range(1, 7)]
            + [Action("倾向", "倾向 x", row=2), Action("词条范围", "词条范围 x", row=2)],
        )
        wr_rows = {}
        for a in plugin.WarframeSDJK._reply_actions(wr, parse("wr 绝路")):
            wr_rows.setdefault(a.row, []).append(a.label)
        self.assertEqual(
            wr_rows,
            {0: ["1号", "2号", "3号", "4号"], 1: ["5号", "6号"], 2: ["倾向", "词条范围"]},
        )

    async def test_first_page_follows_default_display_and_pagination_replaces_minus_one(self):
        for command in ("仲裁", "裂隙", "赏金", "巴罗", "夜波", "wm 绝路"):
            for mode in ("image", "text", "auto"):
                obj, event = new_plugin(), Event()
                obj.render_mode = mode
                reply = Reply("测试", ["正文"])
                default = await consume(
                    obj, event, obj._build_results(event, reply, parse(command))
                )
                obj, event = new_plugin(), Event()
                obj.render_mode = mode
                first = await consume(
                    obj, event, obj._build_results(event, reply, parse(command + " -1"))
                )
                self.assertEqual(body_parts(first), body_parts(default), (command, mode))
        self.assertEqual(
            plugin.WarframeSDJK._page_command('wm "Soma Prime" -1 -w -ps', 2),
            'wm "Soma Prime" -w -ps -2',
        )
        self.assertEqual(
            plugin.WarframeSDJK._page_command('wm "Soma Prime" -1 -t -ps', 1),
            'wm "Soma Prime" -t -ps',
        )

    async def test_markdown_opt_in_text_only_and_literal_copy_tail(self):
        for enabled, image in ((False, False), (True, False), (True, True)):
            with self.subTest(enabled=enabled, image=image):
                obj, event = new_plugin(), Event()
                obj.cfg["qq_markdown_cards"] = False  # 本例只测显式 markdown 字段
                whisper = "/w Player_Name *Soma*"
                reply = Reply(
                    "标题",
                    ["普通正文"],
                    whisper=[whisper],
                    markdown="**正文**" if enabled else None,
                )
                await consume(
                    obj,
                    event,
                    obj._build_results(event, reply, parse("查询 -t" if image else "查询 -w")),
                )
                native = [payload for kind, payload in event.deliveries if kind == "buttons"]
                bodies = [payload for payload in native if "keyboard" not in payload]
                self.assertEqual(len(bodies), int(enabled and not image))
                if bodies:
                    self.assertEqual(bodies[0]["markdown"]["content"], "**正文**")
                    self.assertNotIn(whisper, bodies[0]["markdown"]["content"])
                    self.assertLess(bodies[0]["msg_seq"], native[-1]["msg_seq"])
                buttons = native[-1]["keyboard"]["content"]["rows"][0]["buttons"]
                self.assertEqual(buttons[0]["action"]["data"], whisper)

    async def test_pages_with_explicit_markdown_send_one_markdown_body(self):
        # 紫卡分析续页：文字模式下 QQ Markdown 一条发全部，不再拼两页纯文字
        pages = [("紫卡分析", ["◆ 武器：甲", "· 一　1"]), ("紫卡分析 · 融合建议续", ["· 二　2"])]
        for markdown in ("## 全部\n\n- 一\n- 二", None):
            with self.subTest(markdown=bool(markdown)):
                obj, event = new_plugin(), Event()
                reply = Reply("紫卡分析", pages[0][1], pages=pages, markdown=markdown)
                await consume(obj, event, obj._build_results(event, reply, parse("查询 -w")))
                md = [
                    p["markdown"]["content"]
                    for k, p in event.deliveries
                    if k == "buttons" and "markdown" in p
                ]
                body = [p for k, p in event.deliveries if k == "body"]
                if markdown:
                    self.assertEqual(md, [markdown])
                    self.assertFalse(body, "Markdown 已发全部，正文不得再发纯文字")
                else:
                    self.assertFalse(md)
                    text = "".join(getattr(seg, "text", "") for r in body for seg in r.chain)
                    self.assertIn("融合建议续", text)  # 纯文字降级：两页拼接，续页不丢
                    self.assertIn("二", text)

    def test_small_screen_footer_time_and_empty_cells(self):
        from core.render import card_markdown, display_footer, text_card

        footer = (
            "平台：国际服 · 数据源：社区 warframestat（DE 后备）｜快照 2026-10-30T13:17:53.000Z"
        )
        self.assertEqual(
            display_footer(footer),
            "平台：国际服 · 数据源：社区 warframestat（DE 后备）｜快照 2026-10-30 13:17 UTC",
        )
        self.assertEqual(
            display_footer("快照 2026-10-30T13:17:53+00:00"), "快照 2026-10-30 13:17 UTC"
        )
        self.assertEqual(display_footer("平台：国际服"), "平台：国际服")  # 无时间不动
        lines = [
            "◆ 每日挑战",
            "· 熔毁　1000 声望　　剩 10小时41分",
            "　使用腐蚀伤害击杀 150 名敌人。",
            "· [全能]　虚空覆涌　涂沃主厅（扎里曼号）　　　剩1小时0分",
        ]
        text = text_card("午夜电波", lines, footer)
        self.assertIn("· 熔毁　1000 声望　剩 10小时41分", text)
        self.assertIn("· [全能]　虚空覆涌　涂沃主厅（扎里曼号）　剩1小时0分", text)
        self.assertIn("　使用腐蚀伤害击杀 150 名敌人。", text)  # 续行缩进保留
        self.assertIn("快照 2026-10-30 13:17 UTC", text)
        md = card_markdown("午夜电波", lines, footer)
        self.assertIn("快照 2026-10-30 13:17 UTC", md)
        self.assertIn("- **熔毁** · 1000 声望 · 剩 10小时41分", md)

    def test_markdown_section_sign_only_blocks_machine_lines(self):
        from core.render import card_markdown

        self.assertIsNotNone(card_markdown("t", ["◆ 来源：wiki Enemy Body Parts §Notes"]))
        self.assertIsNone(card_markdown("t", ["§UNKNOWN 机器行"]))

    def test_markdown_arbitration_low_sample_and_wiki_titles(self):
        from core import arbi
        from core.commands.rotations import _note_line
        from core.render import card_markdown

        # 仲裁「样本少」标记与说明行：半角 * 会让整卡退回纯文字
        self.assertNotIn("*", arbi.LOW_MARK + arbi.LOW_NOTE)
        self.assertIsNotNone(
            card_markdown(
                "仲裁", [f"◆ 下一小时：异化区（火卫二）　[F{arbi.LOW_MARK} 442]", arbi.LOW_NOTE]
            )
        )
        # 终幕数据说明里的 wiki 页名：显示时下划线换空格，其余文字原样
        note = "批次表与加成取自 wiki Coda_Weapons 源页（2026-10-15 起）"
        self.assertEqual(
            _note_line(note), "※ 批次表与加成取自 wiki Coda Weapons 源页（2026-10-15 起）"
        )
        self.assertIsNotNone(card_markdown("终幕", [_note_line(note)]))
        self.assertEqual(_note_line("a_1 不动"), "※ a_1 不动")
        # 钢铁之路页脚来源不带下划线链接
        src = (ROOT / "core" / "commands" / "progress.py").read_text(encoding="utf-8")
        self.assertNotIn("Steel_Essence", src)

    async def test_text_card_auto_markdown_and_unsafe_fallback(self):
        lines = ["◆ 状态：已抵达", "◆ 货单", "　商品　价格", "· 甲　1杜", "※ 注释"]
        cases = (
            ("查询 -w", True, lines, True),
            ("查询 -t", True, lines, False),  # 图片卡不走 Markdown
            ("查询 -w", False, lines, False),  # 面板开关关闭
            ("查询 -w", True, [*lines, "· Player_Name　1杜"], False),  # 含下划线退回纯文字
        )
        for command, switch, rows, expect in cases:
            with self.subTest(command=command, switch=switch, rows=len(rows)):
                obj, event = new_plugin(), Event()
                obj.cfg["qq_markdown_cards"] = switch
                await consume(
                    obj, event, obj._build_results(event, Reply("货单", rows), parse(command))
                )
                md = [
                    p["markdown"]["content"]
                    for k, p in event.deliveries
                    if k == "buttons" and "markdown" in p
                ]
                self.assertEqual(bool(md), expect)
                if md:
                    self.assertTrue(md[0].startswith("## 货单"))
                    self.assertIn("**状态**：已抵达", md[0])
                    self.assertIn("- **甲** · 1杜", md[0])
                    self.assertIn("> 注释", md[0])
                    self.assertFalse(
                        [k for k, _ in event.deliveries if k == "body"], "正文不得再发纯文字"
                    )

    def test_text_output_drops_image_color_legend(self):
        from core.render import card_markdown, text_card

        legend = "※ 金色＝杜卡德 · 青色＝现金 · 彩色标签＝商品类别"
        lines = ["◆ 货单：共 1 件", "· 甲　1杜　2万现金", legend, "※ 数据来自官方"]
        md = card_markdown("虚空商人", lines)
        text = text_card("虚空商人", lines)
        for out in (md, text):
            self.assertNotIn("金色", out)
            self.assertIn("数据来自官方", out)

    def test_text_card_frameless_and_goods_one_per_line(self):
        from core.render import text_card

        text = text_card(
            "虚空商人",
            [
                "◆ 状态：已抵达",
                "◆ 在售货单",
                "　商品　杜卡德　现金　商品　杜卡德　现金",
                "· 甲　1杜　2万现金　乙　3杜　4万现金",
            ],
            "平台：国际服",
        )
        self.assertNotIn("║", text)
        self.assertIn("\n· 甲　1杜　2万现金\n· 乙　3杜　4万现金\n", text)
        self.assertIn("　商品　杜卡德　现金\n", text)
        self.assertTrue(text.endswith("—— 平台：国际服"))

    def test_card_markdown_layout(self):
        from core.render import card_markdown

        md = card_markdown(
            "标题",
            [
                "◆ 在售",
                "　商品　价格　商品　价格",
                "· [武器]",
                "· 甲　1杜　乙　2杜",
                "◆ 列表",
                "　纪元　节点",
                "· [古纪]　A",
                "· [古纪]　B",
                "· [前纪]　C",
                "· 挑战　1000 声望",
                "　说明续行",
            ],
            "页脚",
        )
        self.assertIn("*商品 · 价格*", md)  # 双栏表头只留一半
        self.assertIn("**武器**\n\n- **甲** · 1杜\n- **乙** · 2杜", md)
        self.assertEqual(md.count("**古纪**"), 1)
        self.assertIn("- **A**\n- **B**", md)
        self.assertIn("- **挑战** · 1000 声望\n    - 说明续行", md)
        self.assertTrue(md.endswith("***\n\n页脚"))
        for bad in ("a*b", "a_b", "`x`", "<b>", "[链](x)", "# 标题", "- 项"):
            self.assertIsNone(card_markdown("t", ["◆ 值：" + bad if bad[0] not in "#-" else bad]))

    async def test_auto_markdown_known_rejection_keeps_plain_body(self):
        forbidden = type("ForbiddenError", (Exception,), {})()
        too_long = type("PayloadTooLarge", (Exception,), {"status": 413})()
        server_error = type("ServerError", (Exception,), {"status": 500})()
        for error, fallback in ((forbidden, True), (too_long, True), (server_error, False)):
            with self.subTest(error=type(error).__name__):
                obj, event = new_plugin(), Event()
                event.button_error = error
                results = await consume(
                    obj,
                    event,
                    obj._build_results(event, Reply("标题", ["普通正文"]), parse("查询 -w")),
                )
                self.assertEqual(bool(results), fallback)
                if fallback:
                    self.assertIn("普通正文", "".join(body_parts(results)))
                    self.assertFalse(results[0].markdown)

    def test_auto_markdown_checks_embedded_newlines_from_nightwave(self):
        from core.formatters import fmt_nightwave
        from core.render import card_markdown

        for desc, safe in (
            ("正常说明\n下一行", True),
            ("正常说明\n# 意外标题", False),
            ("正常说明\n- 意外列表", False),
        ):
            with self.subTest(desc=desc):
                title, lines = fmt_nightwave(
                    {"activeChallenges": [{"title": "测试挑战", "standing": 1000, "desc": desc}]}
                )
                self.assertEqual(card_markdown(title, lines) is not None, safe)

    async def test_markdown_timeout_does_not_repeat_body_as_plain(self):
        obj, event = new_plugin(), Event()
        event.button_error = TimeoutError()
        reply = Reply("标题", ["普通正文"], markdown="**正文**")
        results = await consume(obj, event, obj._build_results(event, reply, parse("查询 -w")))
        self.assertEqual(results, [])
        self.assertEqual(len(event.deliveries), 1)

    async def test_qq_pages_before_buttons_and_literal_tail(self):
        obj, event = new_plugin(), Event()
        whisper = "/w Player_Name *Soma*"
        reply = Reply(
            pages=[("一", ["BODY_1"]), ("二", ["BODY_2"])],
            extra_text="原始链接 https://example.invalid/a_b",
            whisper=[whisper],
            actions=[Action("执行", "仲裁")],
        )
        results = await consume(obj, event, obj._build_results(event, reply, parse("查询 -t")))
        self.assertEqual([kind for kind, _ in event.deliveries], ["body", "buttons"])
        self.assertEqual(body_parts(results)[:2], ["IMAGE:BODY_1", "IMAGE:BODY_2"])
        self.assertIn("a_b", body_parts(results)[2])
        buttons = event.deliveries[-1][1]["keyboard"]["content"]["rows"]
        actions = [button["action"] for row in buttons for button in row["buttons"]]
        # 行布局：复制类（密语）在最上一行，指令自带按钮排在其后（main._reply_actions）
        self.assertEqual([a["data"] for a in actions], [whisper, "仲裁"])
        self.assertTrue(all(a["enter"] is False for a in actions))
        self.assertEqual(len(obj.cleanups), 2)
        self.assertEqual(reply.whisper, [whisper], "组装不得修改handler返回的原对象")

    async def test_button_failure_never_repeats_body(self):
        class BadRequest(Exception):
            pass

        for error in (BadRequest("explicit rejection"), TimeoutError("unknown delivery")):
            with self.subTest(error=type(error).__name__):
                obj, event = new_plugin(), Event()
                event.button_error = error
                reply = Reply(
                    pages=[("一", ["BODY_1"]), ("二", ["BODY_2"])], actions=[Action("执行", "仲裁")]
                )
                results = await consume(
                    obj, event, obj._build_results(event, reply, parse("查询 -t"))
                )
                self.assertEqual(
                    [kind for kind, _ in event.deliveries], ["body", "buttons", "body"]
                )
                self.assertEqual(body_parts(results).count("IMAGE:BODY_1"), 1)
                self.assertEqual(body_parts(results).count("IMAGE:BODY_2"), 1)
                self.assertIn("发送「仲裁」", body_parts(results)[-1])
                self.assertEqual(obj._render_card.await_count, 2)

    async def test_no_buttons_keeps_whisper_and_extra_once(self):
        obj, event = new_plugin(), Event(platform="aiocqhttp")
        reply = Reply("标题", ["正文"], whisper=["/w Player_Name *A*"], extra_text="a_b")
        results = await consume(obj, event, obj._build_results(event, reply, parse("查询 -w")))
        text = "".join(body_parts(results))
        self.assertEqual([kind for kind, _ in event.deliveries], ["body"])
        self.assertEqual(text.count("/w Player_Name *A*"), 1)
        self.assertEqual(text.count("a_b"), 1)
        obj._render_card.assert_not_awaited()

    async def test_unsupported_actions_keep_run_fill_copy_distinct(self):
        obj, event = new_plugin(), Event(platform="aiocqhttp")
        whisper = "/w Player_Name *A*"
        reply = Reply(
            "结果",
            ["正文"],
            whisper=[whisper],
            actions=[
                Action("执行", "仲裁"),
                Action("补充条件", "蹲 裂隙 ", "fill"),
                Action("复制密语", whisper, "copy"),
            ],
        )
        results = await consume(obj, event, obj._build_results(event, reply, parse("查询 -w")))
        text = "".join(body_parts(results))
        self.assertIn("发送「仲裁」", text)
        self.assertIn("复制「蹲 裂隙 …」补全后发送", text)
        self.assertIn("复制到游戏内", text)
        self.assertEqual(text.count(whisper), 1)
        self.assertTrue(all(kind == "body" for kind, _ in event.deliveries))

    async def test_raw_and_admin_use_same_literal_path(self):
        obj, event = new_plugin(), Event(text=".开启 推送")
        text = "已开启 Player_Name 的 *原始字段*"
        obj._handle_admin = AsyncMock(return_value=Reply(raw_text=text))
        # 管理raw不应依赖尚未初始化的图片渲染器。
        del obj.renderer
        del obj.render_mode
        results = await consume(obj, event, obj.on_message(event))
        self.assertEqual(body_parts(results), [text])
        self.assertTrue(event.stopped)
        self.assertFalse(results[0].markdown)
        obj._handle_admin.assert_awaited_once_with(event, ".开启 推送")

    async def test_cleanup_only_strips_unmarked_qq_output(self):
        obj, event = new_plugin(), Event()
        original = "**粗体** 与 Player_Name"
        event.result = Result([Plain(original)])
        await obj._on_decorating_result(event)
        self.assertNotEqual(event.result.chain[0].text, original)
        marked = obj._literal_result(Result([Plain(original)]))
        event.result = marked
        await obj._on_decorating_result(event)
        self.assertEqual(marked.chain[0].text, original)
        event.platform = "telegram"
        event.result = Result([Plain(original)])
        await obj._on_decorating_result(event)
        self.assertEqual(event.result.chain[0].text, original)

    async def test_qq_limit_counts_page_tail_and_manual_hint(self):
        for private, count in ((False, 5), (True, 4)):
            with self.subTest(private=private):
                obj, event = new_plugin(), Event(private=private)
                reply = Reply(
                    pages=[(str(i), [f"BODY_{i}"]) for i in range(count)], extra_text="尾文"
                )
                results = await consume(
                    obj, event, obj._build_results(event, reply, parse("查询 -t"))
                )
                # 首尔AstrBot4.29.3已核：第二媒体才分块，尾Plain合进末图payload。
                image_count = sum(
                    isinstance(segment, Image) for result in results for segment in result.chain
                )
                sends = sum(
                    max(1, sum(isinstance(segment, Image) for segment in result.chain))
                    for result in results
                )
                sends += sum(kind == "buttons" for kind, _ in event.deliveries)
                self.assertEqual(image_count, count, "额度内完整图页不应被误拒绝")
                self.assertLessEqual(sends, count)
                obj, event = new_plugin(), Event(private=private)
                reply.extra_text = ""
                reply.actions = [Action("执行", "仲裁")]
                results = await consume(
                    obj, event, obj._build_results(event, reply, parse("查询 -t"))
                )
                self.assertEqual(
                    sum(
                        isinstance(segment, Image) for result in results for segment in result.chain
                    ),
                    count,
                )
                self.assertTrue(all(kind == "body" for kind, _ in event.deliveries))
                self.assertIn("发送「仲裁」", "".join(body_parts(results)))

    async def test_push_false_is_failure_and_none_is_legacy_success(self):
        obj = new_plugin()
        obj.context.send_message = AsyncMock(return_value=False)
        with self.assertRaises(RuntimeError):
            await obj._push_send("fixture:GroupMessage:group", "literal_a*b")
        obj.context.send_message.return_value = None
        await obj._push_send("fixture:GroupMessage:group", "literal_a*b")
        chain = obj.context.send_message.call_args.args[1]
        self.assertEqual(chain.chain[0].text, "literal_a*b")
        self.assertFalse(chain.markdown)


if __name__ == "__main__":
    unittest.main(verbosity=2)
