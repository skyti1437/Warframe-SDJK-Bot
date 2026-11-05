"""按钮桥真实入口契约：不联网，以 SDK 桩覆盖身份、会话、降级与生命周期。"""

from __future__ import annotations

import asyncio
import sys
import time
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.commands.base import Action
from core.messaging import MessagingBridge

NS = types.SimpleNamespace


def module(name, **attrs):
    value = types.ModuleType(name)
    value.__dict__.update(attrs)
    sys.modules[name] = value
    return value


class Message:
    def __init__(self):
        self.group_id = ""
        self.session_id = ""
        self.message_id = ""
        self.self_id = "bot"


class Event:
    def __init__(
        self, platform="telegram", instance="bot-a", target="42", user="owner", group=True, raw=None
    ):
        self.platform, self.instance, self.user = platform, instance, user
        self.message_obj = NS(
            group_id=target if group else "",
            session_id=target,
            self_id="bot",
            message_id="incoming-1",
            raw_message=raw,
            type="group" if group else "private",
        )

    def get_platform_name(self):
        return self.platform

    def get_platform_id(self):
        return self.instance

    def get_sender_id(self):
        return self.user


class Adapter:
    def __init__(self):
        self.events = []
        self.client = NS(username="bot")

    def create_event(self, message, **kwargs):
        return NS(
            message_obj=message,
            role="member",
            is_admin=False,
            web_client=getattr(self, "web_client", None),
            **kwargs,
        )

    def commit_event(self, event):
        self.events.append(event)


class App:
    def __init__(self):
        self.handlers = []

    def add_handler(self, handler, group):
        self.handlers.append((handler, group))

    def remove_handler(self, handler, group):
        self.handlers.remove((handler, group))


class Button:
    def __init__(self, text=None, **kwargs):
        self.text = text
        self.__dict__.update(kwargs)


class View:
    def __init__(self, timeout):
        self.timeout, self.children, self.stopped = timeout, [], False

    def add_item(self, item):
        self.children.append(item)

    def stop(self):
        self.stopped = True


class Handler:
    def __init__(self, callback, pattern):
        self.callback, self.pattern = callback, pattern


class Session:
    @staticmethod
    def from_str(umo):
        instance, kind, target = umo.split(":", 2)
        return NS(
            platform_name=instance, platform_id=instance, message_type=kind, session_id=target
        )


class GroupMessage:
    pass


class C2CMessage:
    pass


class PatchedGroupMessage(GroupMessage):
    pass


class PatchedC2CMessage(C2CMessage):
    pass


class MessagingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.saved_modules = dict(sys.modules)
        module(
            "astrbot.api.platform",
            AstrBotMessage=Message,
            MessageMember=lambda **kw: NS(**kw),
            MessageType=NS(GROUP_MESSAGE="group", FRIEND_MESSAGE="private"),
        )
        module("astrbot.api.message_components", Plain=lambda **kw: NS(**kw))
        module(
            "telegram",
            InlineKeyboardButton=Button,
            InlineKeyboardMarkup=lambda rows: NS(inline_keyboard=rows),
            CopyTextButton=lambda text: NS(text=text),
        )
        module("telegram.ext", CallbackQueryHandler=Handler)
        module("botpy.message", GroupMessage=GroupMessage, C2CMessage=C2CMessage)
        module(
            "discord",
            ui=NS(View=View, Button=Button),
            utils=NS(
                escape_markdown=lambda text, **_kwargs: "".join(
                    "\\" + char if char in "\\*_`|>" else char for char in text
                )
            ),
            AllowedMentions=NS(none=lambda: "no-mentions"),
        )
        module("astrbot.core.platform.message_session", MessageSesion=Session)
        module("astrbot.core.platform.sources.kook.kook_types", KookMessageType=NS(TEXT=1, CARD=10))
        self.adapter = Adapter()
        self.adapter.application = App()
        self.adapters = {"bot-a": self.adapter}
        self.bridge = MessagingBridge(NS(get_platform_inst=self.adapters.get))

    async def asyncTearDown(self):
        await self.bridge.close()
        for name in list(sys.modules):
            if name not in self.saved_modules:
                del sys.modules[name]
        sys.modules.update(self.saved_modules)

    def telegram(self, target="42", user="owner"):
        event = Event(target=target, user=user)
        event.client = NS(send_message=AsyncMock(return_value=NS(message_id=1)))
        return event

    async def click_tg(self, token, *, user="other", chat="42", thread=None, query_id="query-1"):
        query = NS(
            data=token,
            id=query_id,
            answer=AsyncMock(),
            from_user=NS(id=user, full_name=f"当前用户{user}"),
            message=NS(chat=NS(id=chat), message_thread_id=thread, from_user=NS(id="original-bot")),
        )
        handler = self.adapter.application.handlers[0][0]
        await handler.callback(NS(callback_query=query), NS())
        return query

    async def test_telegram_sender_instance_topic_and_duplicate(self):
        event = self.telegram(target="42#73")
        event.message_obj.group_id = "42"  # 适配器可只在 session_id 保留 topic。
        result = await self.bridge.send_actions(event, [Action("状态", ".状态")])
        self.assertTrue(result.sent)
        kwargs = event.client.send_message.call_args.kwargs
        self.assertEqual((kwargs["chat_id"], kwargs["message_thread_id"]), ("42", 73))
        token = kwargs["reply_markup"].inline_keyboard[0][0].callback_data
        self.assertLessEqual(len(token.encode()), 64)
        await self.click_tg(token, chat="elsewhere", thread=73)
        await self.click_tg(token, thread=74)
        self.assertEqual(self.adapter.events, [])
        await self.click_tg(token, thread=73)
        delivered = self.adapter.events[0]
        self.assertEqual(delivered.message_obj.sender.user_id, "other")
        self.assertEqual(delivered.message_obj.session_id, "42#73")
        self.assertEqual(delivered.message_obj.message_str, ".状态")
        self.assertEqual(delivered.role, "member")
        self.assertFalse(delivered.is_admin)
        self.assertTrue(delivered.is_at_or_wake_command)
        await self.click_tg(token, thread=73)
        self.assertEqual(len(self.adapter.events), 1)
        self.assertIsNone(self.bridge._lookup(token, "bot-b", "42", "73", "other", "new"))

    async def test_telegram_owner_copy_fill_and_registration_cleanup(self):
        event = self.telegram()
        actions = [
            Action("仅本人", ".状态", owner_only=True),
            Action("填入", "wm ", "fill"),
            Action("复制", "/w Player_Name", "copy"),
            Action("私有复制", "secret", "copy", owner_only=True),
            Action("长复制", "字" * 257, "copy"),
        ]
        result = await self.bridge.send_actions(event, actions)
        self.assertEqual(result.fallback, [actions[1], actions[3], actions[4]])
        markup = event.client.send_message.call_args.kwargs["reply_markup"]
        self.assertEqual(markup.inline_keyboard[0][1].copy_text.text, "/w Player_Name")
        token = markup.inline_keyboard[0][0].callback_data
        await self.click_tg(token)
        self.assertEqual(self.adapter.events, [])
        await self.click_tg(token, user="owner")
        self.assertEqual(len(self.adapter.events), 1)
        await self.bridge.send_actions(event, [actions[0]])
        self.assertEqual(len(self.adapter.application.handlers), 1)
        await self.bridge.close()
        self.assertEqual(self.adapter.application.handlers, [])
        self.assertFalse(self.bridge.can_send_actions(event))
        self.assertFalse(self.bridge._tickets)

    async def test_general_topic_and_callback_expiration_limit(self):
        event = self.telegram()
        self.bridge.MAX_TOKENS = 2
        await self.bridge.send_actions(
            event, [Action("一", "1"), Action("二", "2"), Action("三", "3")]
        )
        self.assertEqual(len(self.bridge._tickets), 2)
        token = next(iter(self.bridge._tickets))
        await self.click_tg(token, thread=1)
        self.assertEqual(len(self.adapter.events), 1)
        for ticket in self.bridge._tickets.values():
            ticket.expires = time.monotonic() - 1
        await self.click_tg(token, thread=1, query_id="later")
        self.assertEqual(len(self.adapter.events), 1)

    def qq(self, group=True):
        raw = PatchedGroupMessage() if group else PatchedC2CMessage()
        raw.group_openid = "qq-group" if group else None
        raw.author = NS(user_openid="owner")
        event = Event("qq_official", raw=raw, group=group)
        event.bot = NS(
            api=NS(
                post_group_message=AsyncMock(return_value={"id": "sent"}),
                post_c2c_message=AsyncMock(return_value={"id": "sent"}),
            )
        )
        return event

    def qq_image_store(self, event, **ref_values):
        event.bot.api._http = NS(_token=NS(app_id="real-app-id"))
        ref = NS(
            url="qqbot-media://image/v1/safe",
            width=640,
            height=360,
            image_id="image-1",
            sha256="a" * 64,
        )
        ref.__dict__.update(ref_values)
        lease = NS(ref=ref, release=AsyncMock())
        store = NS(get=AsyncMock(return_value=lease), invalidate=AsyncMock(), close=AsyncMock())
        self.bridge._qq_media = store
        return store, lease

    async def test_qq_image_card_combines_media_and_buttons_in_one_message(self):
        for group in (True, False):
            with self.subTest(group=group):
                event = self.qq(group)
                store, lease = self.qq_image_store(event)
                actions = [
                    Action("状态", ".状态", owner_only=True),
                    Action("填入", ".价格", "fill"),
                    Action("复制密语", "/w Name", "copy", owner_only=True),
                ]
                path = Path("**恶意标题**[跳转](https://evil.invalid).png")
                result = await self.bridge.send_image_card(
                    event, path, actions, remaining_messages=1
                )
                self.assertTrue(result.sent)
                self.assertFalse(result.fallback)
                self.assertFalse(result.uncertain)
                self.assertEqual(result.action_fallback, [])
                store.get.assert_awaited_once_with("real-app-id", event.bot.api, path)
                post = event.bot.api.post_group_message if group else event.bot.api.post_c2c_message
                post.assert_awaited_once()
                payload = post.call_args.kwargs
                content = payload["markdown"]["content"]
                self.assertIn("qqbot-media://image/v1/safe", content)
                self.assertIn("640", content)
                self.assertIn("360", content)
                self.assertNotIn("恶意标题", content)
                self.assertNotIn("evil.invalid", content)
                buttons = payload["keyboard"]["content"]["rows"][0]["buttons"]
                self.assertEqual([b["action"]["data"] for b in buttons], [a.send for a in actions])
                self.assertEqual([b["action"]["enter"] for b in buttons], [not group, False, False])
                self.assertEqual(
                    buttons[0]["action"]["permission"], {"type": 0, "specify_user_ids": ["owner"]}
                )
                self.assertEqual(payload["msg_id"], "incoming-1")
                lease.release.assert_awaited_once_with(confirmed=True)

    async def test_qq_image_card_preflight_does_not_upload(self):
        for scenario in ("account", "budget", "channel", "actions", "store"):
            with self.subTest(scenario=scenario):
                event = self.qq()
                store, _lease = self.qq_image_store(event)
                actions = [Action("状态", ".状态")]
                budget = 1
                if scenario == "account":
                    del event.bot.api._http
                elif scenario == "budget":
                    budget = 0
                elif scenario == "channel":
                    event.message_obj.raw_message = NS(group_openid="channel")
                elif scenario == "actions":
                    actions = [Action("不支持", ".状态", "unknown")]
                else:
                    self.bridge._qq_media = None
                result = await self.bridge.send_image_card(
                    event, Path("card.png"), actions, remaining_messages=budget
                )
                self.assertTrue(result.fallback)
                self.assertFalse(result.uncertain)
                store.get.assert_not_awaited()
                event.bot.api.post_group_message.assert_not_awaited()

    async def test_qq_image_card_send_outcomes_release_and_never_duplicate(self):
        cases = (
            (TimeoutError(), False),
            (type("ServerFailure", (Exception,), {"status": 500})(), False),
            (type("ForbiddenError", (Exception,), {})(), True),
        )
        for error, rejected in cases:
            with self.subTest(error=type(error).__name__):
                event = self.qq()
                store, lease = self.qq_image_store(event)
                event.bot.api.post_group_message.side_effect = error
                result = await self.bridge.send_image_card(
                    event, Path("card.png"), [Action("状态", ".状态")]
                )
                self.assertFalse(result.sent)
                self.assertEqual(result.fallback, rejected)
                self.assertEqual(result.uncertain, not rejected)
                event.bot.api.post_group_message.assert_awaited_once()
                lease.release.assert_awaited_once()
                if not rejected:
                    lease.release.assert_awaited_once_with(confirmed=False)
                    store.invalidate.assert_not_awaited()
                else:
                    store.invalidate.assert_awaited_once_with("real-app-id", lease.ref)

    async def test_qq_image_card_upload_failure_falls_back_without_post(self):
        event = self.qq()
        store, lease = self.qq_image_store(event)
        store.get.side_effect = RuntimeError("upload unavailable")
        result = await self.bridge.send_image_card(
            event, Path("card.png"), [Action("状态", ".状态")]
        )
        self.assertTrue(result.fallback)
        self.assertFalse(result.uncertain)
        event.bot.api.post_group_message.assert_not_awaited()
        lease.release.assert_not_awaited()

    async def test_qq_image_card_invalid_reference_releases_without_post(self):
        for values in (
            {"url": "https://evil.invalid/image"},
            {"url": "qqbot-media://image/v1/"},
            {"url": "qqbot-media://image/v1/safe)\n[注入](https://evil.invalid)"},
            {"width": 640.5},
            {"width": True},
            {"width": "640]恶意"},
            {"height": 0},
        ):
            with self.subTest(values=values):
                event = self.qq()
                _store, lease = self.qq_image_store(event, **values)
                result = await self.bridge.send_image_card(
                    event, Path("card.png"), [Action("状态", ".状态")]
                )
                self.assertTrue(result.fallback)
                self.assertFalse(result.uncertain)
                event.bot.api.post_group_message.assert_not_awaited()
                lease.release.assert_awaited_once()

    async def test_qq_image_card_capability_requires_live_qq_identity(self):
        event = self.qq()
        self.assertFalse(self.bridge.can_send_image_cards(event))
        store, _lease = self.qq_image_store(event)
        self.assertTrue(self.bridge.can_send_image_cards(event))
        self.assertFalse(self.bridge.can_send_image_cards(Event()))
        event.bot.api._http._token.app_id = ""
        self.assertFalse(self.bridge.can_send_image_cards(event))
        event.bot.api._http._token.app_id = "real-app-id"
        await self.bridge.close()
        self.assertFalse(self.bridge.can_send_image_cards(event))
        store.get.assert_not_awaited()

    async def test_qq_image_card_preserves_action_overflow_and_closes_store(self):
        event = self.qq()
        store, _lease = self.qq_image_store(event)
        actions = [Action(str(i), f".状态{i}") for i in range(8)]
        result = await self.bridge.send_image_card(event, Path("card.png"), actions)
        self.assertTrue(result.sent)
        rows = event.bot.api.post_group_message.call_args.kwargs["keyboard"]["content"]["rows"]
        sent = [button["action"]["data"] for row in rows for button in row["buttons"]]
        self.assertTrue(result.action_fallback)
        self.assertEqual(sent + [a.send for a in result.action_fallback], [a.send for a in actions])
        await self.bridge.close()
        store.close.assert_awaited_once()

    async def test_qq_expired_media_is_known_rejection_but_unknown_400_is_not(self):
        expired = type("MediaRejected", (Exception,), {"status": 400, "code": 40035009})()
        conflict = type("Conflict", (Exception,), {"status": 400, "code": 99999999})()
        self.assertTrue(self.bridge._known_rejection(expired))
        self.assertFalse(self.bridge._known_rejection(conflict))

    async def test_qq_unknown_image_retains_unsupported_action(self):
        from core.messaging import ImageDelivery

        event = self.qq()
        event.bot.api._http = NS(_token=NS(app_id="fixture-app"))
        lease = NS(
            ref=NS(
                url="qqbot-media://image/v1/safe",
                width=640,
                height=480,
                image_id="own",
                sha256="hash",
            ),
            release=AsyncMock(),
        )
        self.bridge._qq_media = NS(
            get=AsyncMock(return_value=lease), invalidate=AsyncMock(), close=AsyncMock()
        )
        event.bot.api.post_group_message.side_effect = TimeoutError("unknown")
        missing = Action("manual", "fill ", "fill", row=99)
        result = await self.bridge.send_image_card(
            event, "fixture.png", [Action("go", "arbitration"), missing]
        )
        self.assertIsInstance(result, ImageDelivery)
        self.assertTrue(result.uncertain)
        self.assertEqual(result.action_fallback, [missing])

    async def test_qq_semantics_permission_sequence_and_budget(self):
        event = self.qq()
        actions = [
            Action("状态", ".状态"),
            Action("复制密语", "/w Name_1", "copy", owner_only=True),
        ]
        result = await self.bridge.send_actions(event, actions, remaining_messages=1)
        self.assertTrue(result.sent)
        kwargs = event.bot.api.post_group_message.call_args.kwargs
        buttons = kwargs["keyboard"]["content"]["rows"][0]["buttons"]
        self.assertTrue(all(not b["action"]["enter"] for b in buttons))
        self.assertEqual(buttons[1]["action"]["data"], "/w Name_1")
        self.assertEqual(
            buttons[1]["action"]["permission"], {"type": 0, "specify_user_ids": ["owner"]}
        )
        self.assertIn("填入", buttons[1]["render_data"]["label"])
        self.assertEqual(kwargs["msg_id"], "incoming-1")
        self.assertGreater(kwargs["msg_seq"], 10000)
        await self.bridge.send_actions(event, actions)
        self.assertEqual(
            event.bot.api.post_group_message.call_args.kwargs["msg_seq"], kwargs["msg_seq"] + 1
        )
        count = event.bot.api.post_group_message.call_count
        exhausted = await self.bridge.send_actions(event, actions, remaining_messages=0)
        self.assertEqual(exhausted.fallback, actions)
        self.assertEqual(event.bot.api.post_group_message.call_count, count)
        private = self.qq(False)
        await self.bridge.send_actions(private, actions)
        buttons = private.bot.api.post_c2c_message.call_args.kwargs["keyboard"]["content"]["rows"][
            0
        ]["buttons"]
        self.assertTrue(buttons[0]["action"]["enter"])
        self.assertFalse(buttons[1]["action"]["enter"])
        event.message_obj.raw_message = type("Message", (), {"group_openid": "channel"})()
        self.assertFalse(self.bridge.can_send_actions(event), "频道不能借 QQ 群接口发送")

    async def test_markdown_sequence_and_only_known_rejection_fallback(self):
        for group in (True, False):
            with self.subTest(group=group):
                event = self.qq(group)
                post = event.bot.api.post_group_message if group else event.bot.api.post_c2c_message
                self.assertTrue((await self.bridge.send_markdown(event, "**正文**")).sent)
                first = post.call_args.kwargs
                self.assertEqual(first["markdown"], {"content": "**正文**"})
                self.assertNotIn("keyboard", first)
                await self.bridge.send_actions(event, [Action("按钮", "状态")])
                self.assertEqual(post.call_args.kwargs["msg_seq"], first["msg_seq"] + 1)
                for error, known in (
                    (TimeoutError(), False),
                    (type("ForbiddenError", (Exception,), {})(), True),
                ):
                    post.side_effect = error
                    count_before = post.call_count
                    result = await self.bridge.send_markdown(event, "**正文**")
                    self.assertEqual(result.uncertain, not known)
                    self.assertEqual(result.fallback, known)
                    self.assertEqual(post.call_count, count_before + 1)
                    self.assertEqual(post.call_args.kwargs["msg_id"], first["msg_id"])
                count = post.call_count
                self.assertTrue(
                    (await self.bridge.send_markdown(event, "正文", remaining_messages=0)).fallback
                )
                self.assertEqual(post.call_count, count)
        self.assertTrue((await self.bridge.send_markdown(Event(), "正文")).fallback)

    async def test_qq_timeout_never_retries_or_removes_message_id(self):
        event = self.qq()
        event.bot.api.post_group_message.side_effect = TimeoutError()
        actions = [Action("状态", ".状态")]
        result = await self.bridge.send_actions(event, actions)
        self.assertTrue(result.uncertain)
        self.assertFalse(result.sent)
        self.assertEqual(result.fallback, [])
        event.bot.api.post_group_message.assert_awaited_once()
        self.assertEqual(event.bot.api.post_group_message.call_args.kwargs["msg_id"], "incoming-1")
        event.bot.api.post_group_message.side_effect = type("ForbiddenError", (Exception,), {})()
        result = await self.bridge.send_actions(event, actions)
        self.assertEqual(result.fallback, actions)
        self.assertFalse(result.uncertain)
        sequence_conflict = type("SequenceNumberError", (Exception,), {"status": 400})()
        event.bot.api.post_group_message.side_effect = sequence_conflict
        result = await self.bridge.send_actions(event, actions)
        self.assertTrue(result.uncertain)
        self.assertEqual(result.fallback, [])

    async def test_discord_current_clicker_thread_and_close(self):
        event = Event("discord", target="773")
        channel = NS(send=AsyncMock())
        event.client = NS(get_channel=lambda target: channel)
        actions = [
            Action("状态", ".状态"),
            Action("填入", "wm ", "fill"),
            Action("复制", "text", "copy"),
        ]
        result = await self.bridge.send_actions(event, actions)
        self.assertEqual(result.fallback, actions[1:])
        view = channel.send.call_args.kwargs["view"]
        callback = view.children[0].callback
        interaction = NS(
            user=NS(id="clicker", display_name="点击者"),
            channel_id=773,
            id=17,
            response=NS(defer=AsyncMock(), send_message=AsyncMock()),
            followup=NS(),
        )
        await callback(interaction)
        delivered = self.adapter.events[0]
        self.assertEqual(delivered.message_obj.sender.user_id, "clicker")
        self.assertEqual(delivered.message_obj.session_id, "773")
        self.assertIs(
            delivered.interaction_followup_webhook
            if hasattr(delivered, "interaction_followup_webhook")
            else delivered.followup_webhook,
            interaction.followup,
        )
        interaction.channel_id = 774
        interaction.id = 18
        await callback(interaction)
        self.assertEqual(len(self.adapter.events), 1)
        await self.bridge.close()
        self.assertTrue(view.stopped)

    async def test_kook_bridge_preserves_unrelated_events_and_uses_body_user(self):
        previous = AsyncMock()
        client = NS(event_callback=previous, send_text=AsyncMock())
        event = Event("kook", target="channel")
        event.client = client
        await self.bridge.send_actions(event, [Action("状态", ".状态")])
        token = next(iter(self.bridge._tickets))
        unrelated = NS(type=255, extra=NS(type="joined_guild", body={}))
        await client.event_callback(unrelated)
        previous.assert_awaited_once_with(unrelated)
        data = NS(
            type=255,
            msg_id="event-id",
            author_id="system-user",
            extra=NS(
                type="message_btn_click",
                body={
                    "value": token,
                    "user_id": "clicker",
                    "target_id": "channel",
                    "user_info": {"username": "新用户"},
                },
            ),
        )
        await client.event_callback(data)
        delivered = self.adapter.events[0]
        self.assertEqual(delivered.message_obj.sender.user_id, "clicker")
        self.assertEqual(delivered.message_obj.raw_message["author_id"], "clicker")
        await self.bridge.close()
        self.assertIs(client.event_callback, previous)

    async def test_slack_socket_private_channel_thread_and_safe_unhook(self):
        previous = AsyncMock()
        socket = NS(event_handler=previous)
        self.adapter.socket_client = socket
        event = Event(
            "slack",
            target="UOWNER",
            user="UOWNER",
            group=False,
            raw={"channel": "DREAL", "thread_ts": "100.1"},
        )
        event.web_client = NS(chat_postMessage=AsyncMock())
        self.adapter.web_client = event.web_client
        await self.bridge.send_actions(event, [Action("状态", ".状态")])
        self.assertEqual(event.web_client.chat_postMessage.call_args.kwargs["channel"], "DREAL")
        token = next(iter(self.bridge._tickets))
        req = NS(
            type="interactive",
            envelope_id="envelope",
            payload={
                "type": "block_actions",
                "user": {"id": "UOWNER"},
                "channel": {"id": "DREAL"},
                "message": {"thread_ts": "100.1"},
                "actions": [{"action_id": token, "value": token}],
            },
        )
        await socket.event_handler(req)
        self.assertEqual(self.adapter.events[0].message_obj.session_id, "UOWNER")
        self.assertEqual(self.adapter.events[0].message_obj.raw_message["thread_ts"], "100.1")
        await self.adapter.events[0].web_client.chat_postMessage(channel="UOWNER", text="回复")
        self.assertEqual(event.web_client.chat_postMessage.call_args.kwargs["channel"], "DREAL")
        self.assertEqual(event.web_client.chat_postMessage.call_args.kwargs["thread_ts"], "100.1")
        wrapper_after_us = AsyncMock()
        socket.event_handler = wrapper_after_us
        await self.bridge.close()
        self.assertIs(socket.event_handler, wrapper_after_us)

    async def test_missing_handlers_and_unsupported_platform_fallback(self):
        actions = [Action("状态", ".状态")]
        for platform in ("lark", "aiocqhttp", "slack", "kook"):
            event = Event(platform)
            self.assertFalse(self.bridge.can_send_actions(event), platform)
            self.assertEqual((await self.bridge.send_actions(event, actions)).fallback, actions)
        self.adapters.clear()
        self.assertFalse(self.bridge.can_send_actions(self.telegram()))

    async def test_literal_preserves_bytes_splits_and_propagates_failure(self):
        event = self.telegram(target="42#73")
        text = "/w Player_Name*[]<>\\你😀" * 1000
        self.assertTrue(await self.bridge.send_literal(event, text))
        calls = event.client.send_message.call_args_list
        self.assertEqual("".join(c.kwargs["text"] for c in calls), text)
        self.assertTrue(all(c.kwargs["parse_mode"] is None for c in calls))
        self.assertTrue(all(c.kwargs["message_thread_id"] == 73 for c in calls))
        self.assertTrue(all(len(c.kwargs["text"].encode("utf-16-le")) // 2 <= 4000 for c in calls))
        event.client.send_message.side_effect = TimeoutError()
        with self.assertRaises(TimeoutError):
            await self.bridge.send_literal(event, "必须保留，不自动重试")
        slack = Event("slack", raw={"channel": "C42"})
        slack.web_client = NS(chat_postMessage=AsyncMock())
        await self.bridge.send_literal(slack, text)
        calls = slack.web_client.chat_postMessage.call_args_list
        self.assertEqual("".join(c.kwargs["blocks"][0]["text"]["text"] for c in calls), text)
        self.assertTrue(all(c.kwargs["blocks"][0]["text"]["type"] == "plain_text" for c in calls))
        kook = Event("kook")
        kook.client = NS(send_text=AsyncMock())
        await self.bridge.send_literal(kook, text)
        calls = kook.client.send_text.call_args_list
        self.assertEqual("".join(c.args[1] for c in calls), text)
        self.assertTrue(all(c.args[3] == 1 for c in calls))
        self.assertFalse(await self.bridge.send_literal(Event("unknown"), text))

    async def test_unknown_send_keeps_tokens_and_concurrent_failure_isolated(self):
        event = self.telegram()
        event.client.send_message.side_effect = TimeoutError()
        result = await self.bridge.send_actions(event, [Action("状态", ".状态")])
        self.assertTrue(result.uncertain)
        self.assertEqual(len(self.bridge._tickets), 1, "可能已送达的按钮仍须可用")
        original_token = next(iter(self.bridge._tickets))
        event.client.send_message.side_effect = type("ForbiddenError", (Exception,), {})()
        result = await self.bridge.send_actions(event, [Action("状态", ".状态")])
        self.assertFalse(result.uncertain)
        self.assertEqual(list(self.bridge._tickets), [original_token])

    async def test_empty_owner_and_close_failure_do_not_leak_other_hooks(self):
        event = self.qq()
        event.user = ""
        action = Action("仅本人", ".状态", owner_only=True)
        result = await self.bridge.send_actions(event, [action])
        self.assertEqual(result.fallback, [action])
        event.bot.api.post_group_message.assert_not_awaited()
        telegram = self.telegram()
        await self.bridge.send_actions(telegram, [Action("状态", ".状态")])
        previous = AsyncMock()
        kook = Event("kook")
        kook.client = NS(event_callback=previous, send_text=AsyncMock())
        await self.bridge.send_actions(kook, [Action("状态", ".状态")])

        def broken_remove(*_args):
            raise RuntimeError("正在卸载的应用")

        self.adapter.application.remove_handler = broken_remove
        await self.bridge.close()
        self.assertIs(kook.client.event_callback, previous)
        self.assertFalse(self.bridge._hooks)

    async def test_slack_prepare_keeps_literal_media_in_original_thread(self):
        event = Event("slack", raw={"channel": "C42", "thread_ts": "100.1"})
        client = NS(chat_postMessage=AsyncMock(), files_upload_v2=AsyncMock())
        event.web_client = client
        self.bridge.prepare_event(event)
        wrapper = event.web_client
        self.bridge.prepare_event(event)
        self.assertIs(event.web_client, wrapper)
        await wrapper.files_upload_v2(channel="wrong", file="/fake/image.png")
        self.assertEqual(client.files_upload_v2.call_args.kwargs["thread_ts"], "100.1")
        self.assertEqual(client.files_upload_v2.call_args.kwargs["channel"], "C42")

    async def test_concurrent_rejection_only_removes_its_own_tokens(self):
        first, second = self.telegram(), self.telegram(target="99")
        waiting, release = asyncio.Event(), asyncio.Event()

        async def reject_later(**_kwargs):
            waiting.set()
            await release.wait()
            raise type("ForbiddenError", (Exception,), {})()

        first.client.send_message.side_effect = reject_later
        first_task = asyncio.create_task(self.bridge.send_actions(first, [Action("一", "1")]))
        await waiting.wait()
        result = await self.bridge.send_actions(second, [Action("二", "2")])
        self.assertTrue(result.sent)
        release.set()
        await first_task
        self.assertEqual(len(self.bridge._tickets), 1)
        remaining = next(iter(self.bridge._tickets.values()))
        self.assertEqual((remaining.route.target, remaining.action.send), ("99", "2"))

    async def test_registration_failure_is_unattempted_fallback(self):
        event = self.telegram()

        def reject_registration(*_args):
            raise RuntimeError("应用正在关闭")

        self.adapter.application.add_handler = reject_registration
        action = Action("状态", ".状态")
        result = await self.bridge.send_actions(event, [action])
        self.assertEqual(result.fallback, [action])
        self.assertFalse(result.uncertain)
        event.client.send_message.assert_not_awaited()

    async def test_literal_chunks_prefer_line_boundaries(self):
        # 长货单分片：一行不被切到两条消息里；拼接仍与原文逐字一致
        text = "".join(
            f"· 商品{i:03d} Eskhatos Infinitas Ephemera　88杜　9.8万现金\n" for i in range(200)
        )
        event = self.telegram(target="42")
        self.assertTrue(await self.bridge.send_literal(event, text))
        parts = [c.kwargs["text"] for c in event.client.send_message.call_args_list]
        self.assertGreater(len(parts), 1)
        self.assertEqual("".join(parts), text)
        self.assertTrue(all(part.endswith("\n") for part in parts))
        self.assertTrue(all(len(part.encode("utf-16-le")) // 2 <= 4000 for part in parts))
        # 单行超过上限时才逐字硬切
        long_line = "甲" * 4500 + "\n乙"
        self.assertEqual("".join(MessagingBridge._text_chunks(long_line, 4000)), long_line)
        self.assertTrue(all(len(c) <= 4000 for c in MessagingBridge._text_chunks(long_line, 4000)))

    async def test_chunk_limit_auto_by_platform_and_panel_override(self):
        cfg = {}
        bridge = MessagingBridge(NS(get_platform_inst=self.adapters.get), settings=lambda: cfg)
        auto = {"telegram": 4000, "discord": 1900, "slack": 2900, "kook": 1800}
        self.assertEqual({p: bridge._chunk_limit(p) for p in auto}, auto)
        for value, expected in (
            (0, auto),
            (1000, dict.fromkeys(auto, 1000)),
            (50, dict.fromkeys(auto, 200)),
        ):
            cfg["text_chunk_limit"] = value
            self.assertEqual({p: bridge._chunk_limit(p) for p in auto}, expected, value)
        cfg["text_chunk_limit"] = 99999  # 手动值不能超过平台上限
        self.assertEqual({p: bridge._chunk_limit(p) for p in auto}, auto)
        cfg["text_chunk_limit"] = "abc"  # 非法值按自动
        self.assertEqual(bridge._chunk_limit("telegram"), 4000)
        self.assertEqual(MessagingBridge(None)._chunk_limit("kook"), 1800)  # 未接配置
        # 面板保存（原地更新配置）后下一次发送即按新值分段
        cfg["text_chunk_limit"] = 1000
        event = self.telegram(target="42")
        adapters = self.adapters
        bridge = MessagingBridge(NS(get_platform_inst=adapters.get), settings=lambda: cfg)
        text = "".join(f"第{i:04d}行 Eskhatos Infinitas Ephemera\n" for i in range(300))
        self.assertTrue(await bridge.send_literal(event, text))
        parts = [c.kwargs["text"] for c in event.client.send_message.call_args_list]
        self.assertEqual("".join(parts), text)
        self.assertTrue(all(len(part) <= 1000 and part.endswith("\n") for part in parts))

    async def test_discord_literal_escapes_without_splitting_escape_pairs(self):
        event = Event("discord", target="77")
        channel = NS(send=AsyncMock())
        event.client = NS(get_channel=lambda _id: channel)
        text = "/w Player_Name*`😀\\" * 400
        self.assertTrue(await self.bridge.send_literal(event, text))
        calls = channel.send.call_args_list
        self.assertTrue(all(c.kwargs["allowed_mentions"] == "no-mentions" for c in calls))
        self.assertTrue(
            all(len(c.kwargs["content"].encode("utf-16-le")) // 2 <= 1900 for c in calls)
        )
        escaped = "".join(c.kwargs["content"] for c in calls)
        sdk = sys.modules["discord"]
        self.assertEqual(escaped, sdk.utils.escape_markdown(text))
        channel.send.side_effect = TimeoutError()
        with self.assertRaises(TimeoutError):
            await self.bridge.send_literal(event, text)

    async def test_proactive_real_session_target_no_fake_user_or_inbound_event(self):
        for platform, target, expected, thread in (
            ("telegram", "42#73", "42", 73),
            ("slack", "USER_C42", "C42", None),
            ("kook", "42", "42", None),
            ("discord", "USER_42", "42", None),
        ):
            bot = NS(
                send_message=AsyncMock(),
                chat_postMessage=AsyncMock(),
                send_text=AsyncMock(),
                get_channel=lambda _id: NS(send=AsyncMock()),
            )
            self.adapter.meta = lambda platform=platform: NS(name=platform)
            created = []

            def create(message, platform=platform):
                event = Event(platform)
                event.message_obj = message
                event.user = message.sender.user_id
                event.client = bot
                event.web_client = bot
                created.append(event)
                return event

            self.adapter.create_event = create
            self.assertTrue(
                await self.bridge.send_proactive(f"bot-a:group:{target}", "/w Player_Name")
            )
            self.assertEqual(created[0].get_sender_id(), "")
            self.assertEqual(self.adapter.events, [], "主动推送不能伪造入站事件")
            route = self.bridge._route(created[0])
            self.assertEqual(route.target, expected)
            if thread:
                self.assertEqual(bot.send_message.call_args.kwargs["message_thread_id"], thread)
        self.adapter.meta = lambda: NS(name="qq_official")
        self.assertFalse(await self.bridge.send_proactive("bot-a:group:42", "不尝试QQ主动"))
        self.assertFalse(await self.bridge.send_proactive("unknown:group:42", "无此实例"))

    async def test_proactive_timeout_is_not_fallback_permission(self):
        event = self.telegram()
        event.client.send_message.side_effect = TimeoutError()
        self.adapter.meta = lambda: NS(name="telegram")
        self.adapter.create_event = lambda message: event
        with self.assertRaises(TimeoutError):
            await self.bridge.send_proactive("bot-a:group:42", "不自动重发")
        event.client.send_message.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
