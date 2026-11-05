"""按平台实例发送按钮与字面文本；按钮回调重新进入 AstrBot 标准事件管线。

可选 SDK 延迟导入。没有接通回调的实例只提供文字降级，不发送失效按钮。
QQ 群的 run/copy 均明确呈现为填入；其余平台不能把 fill 偷换成 run。
"""

from __future__ import annotations

import asyncio
import importlib
import json
import secrets
import time
from collections import OrderedDict
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .logging_compat import logger

if TYPE_CHECKING:
    from .commands.base import Action


@dataclass(frozen=True)
class Capabilities:
    platform: str
    run: bool = False
    fill: bool = False
    copy: bool = False
    copy_as_fill: bool = False
    owner_only: bool = False
    callback_ready: bool = False
    max_rows: int = 5
    max_per_row: int = 5


@dataclass
class ActionDelivery:
    sent: bool = False
    fallback: list[Action] = field(default_factory=list)
    uncertain: bool = False


@dataclass
class MarkdownDelivery:
    sent: bool = False
    fallback: bool = False  # 仅在未尝试原生发送时允许重走普通文字正文。
    uncertain: bool = False


@dataclass
class ImageDelivery(MarkdownDelivery):
    action_fallback: list[Action] = field(default_factory=list)


@dataclass(frozen=True)
class _Route:
    instance: str
    platform: str
    target: str
    thread: str
    group: bool
    owner: str
    self_id: str

    @property
    def session(self):
        if self.platform == "telegram" and self.thread:
            return f"{self.target}#{self.thread}"
        if self.platform == "slack" and not self.group:
            return self.owner
        return self.target


@dataclass
class _Ticket:
    route: _Route
    action: Any
    expires: float
    batch: str = ""


class _SlackThreadClient:
    """仅当前事件的代理；不修改账号共用客户端或其他会话的线程。"""

    def __init__(self, client, channel, thread):
        self.client, self.channel, self.thread = client, channel, thread

    def __getattr__(self, name):
        return getattr(self.client, name)

    async def chat_postMessage(self, **kwargs):
        kwargs["channel"] = self.channel
        kwargs.setdefault("thread_ts", self.thread)
        return await self.client.chat_postMessage(**kwargs)

    async def files_upload_v2(self, **kwargs):
        kwargs["channel"] = self.channel
        kwargs.setdefault("thread_ts", self.thread)
        return await self.client.files_upload_v2(**kwargs)


class MessagingBridge:
    TTL = 600.0
    MAX_TOKENS = 2048
    PREFIX = "wf:"
    TG_GROUP = -734915

    # 各平台单条纯文字的分片上限（UTF-16 单位）：略低于官方上限留余量——
    # Telegram sendMessage 4096、Discord 2000（转义后计）、Slack section 文本 3000、KOOK 卡片外文本取保守值。
    CHUNK_LIMITS = {"telegram": 4000, "discord": 1900, "slack": 2900, "kook": 1800}
    CHUNK_MIN = 200

    def __init__(self, context, media_cache_dir: Path | None = None, settings=None):
        self.context = context
        # settings：返回插件配置 dict 的可调用对象；每次发送时读取，面板保存即生效
        self._settings = settings
        self._qq_media = None
        if media_cache_dir is not None:
            from .qq_media import QQMaterialStore

            self._qq_media = QQMaterialStore(media_cache_dir)
        self._tickets: OrderedDict[str, _Ticket] = OrderedDict()
        self._seen: OrderedDict[tuple[str, str], float] = OrderedDict()
        self._hooks: dict[tuple[str, str], tuple[Any, ...]] = {}
        self._views: list[tuple[Any, float]] = []
        self._batch = ContextVar("wf_button_batch", default="")
        self._attempted = ContextVar("wf_button_attempted", default=False)
        self._qq_seq: OrderedDict[tuple[str, str], int] = OrderedDict()
        self._closed = False

    @staticmethod
    def _sdk(name):
        try:
            return importlib.import_module(name)
        except ImportError:
            return None

    def _adapter(self, event):
        getter = getattr(self.context, "get_platform_inst", None)
        return getter(event.get_platform_id()) if callable(getter) else None

    @staticmethod
    def _raw(event):
        return getattr(event.message_obj, "raw_message", None)

    def _route(self, event):
        platform = event.get_platform_name()
        obj = event.message_obj
        raw = self._raw(event)
        group = bool(getattr(obj, "group_id", ""))
        target = str(
            getattr(obj, "group_id", "") or getattr(obj, "session_id", "") or event.get_sender_id()
        )
        thread = ""
        if platform == "telegram":
            target = str(getattr(obj, "session_id", "") or target)
            if "#" in target:
                target, thread = target.split("#", 1)
        elif platform == "slack" and isinstance(raw, dict):
            target = str(raw.get("channel") or target)
            thread = str(raw.get("thread_ts") or "")
        return _Route(
            str(event.get_platform_id()),
            platform,
            target,
            thread,
            group,
            str(event.get_sender_id()),
            str(getattr(obj, "self_id", "")),
        )

    def _qq_kind(self, event):
        raw = getattr(event.message_obj, "raw_message", None)
        # 不把频道 Message/DirectMessage 误发到 QQ 群/C2C 接口。
        sdk = self._sdk("botpy.message")
        if sdk is None:
            return ""
        if isinstance(raw, getattr(sdk, "GroupMessage", ())) and getattr(raw, "group_openid", None):
            return "group"
        if isinstance(raw, getattr(sdk, "C2CMessage", ())) and getattr(
            getattr(raw, "author", None), "user_openid", None
        ):
            return "private"
        return ""

    def capabilities(self, event):
        platform = event.get_platform_name()
        empty = Capabilities(platform)
        if self._closed:
            return empty
        if platform == "qq_official":
            kind = self._qq_kind(event)
            api = getattr(getattr(event, "bot", None), "api", None)
            method = "post_group_message" if kind == "group" else "post_c2c_message"
            if (
                kind
                and callable(getattr(api, method, None))
                and getattr(event.message_obj, "message_id", None)
            ):
                return Capabilities(
                    platform,
                    run=kind == "private",
                    fill=True,
                    copy_as_fill=True,
                    owner_only=True,
                    callback_ready=True,
                )
            return empty
        adapter = self._adapter(event)
        if (
            adapter is None
            or not callable(getattr(adapter, "commit_event", None))
            or not callable(getattr(adapter, "create_event", None))
        ):
            return empty
        if platform == "telegram":
            tg, ext = self._sdk("telegram"), self._sdk("telegram.ext")
            app = getattr(adapter, "application", None)
            if tg and ext and app and callable(getattr(app, "add_handler", None)):
                return Capabilities(
                    platform,
                    run=True,
                    copy=hasattr(tg, "CopyTextButton"),
                    owner_only=True,
                    callback_ready=True,
                    max_per_row=5,
                )
        elif platform == "discord":
            dc = self._sdk("discord")
            if dc and hasattr(dc, "ui") and callable(getattr(adapter, "create_event", None)):
                return Capabilities(platform, run=True, owner_only=True, callback_ready=True)
        elif platform == "kook":
            client = getattr(event, "client", None)
            if (
                client
                and callable(getattr(client, "event_callback", None))
                and callable(getattr(adapter, "create_event", None))
            ):
                return Capabilities(
                    platform,
                    run=True,
                    owner_only=True,
                    callback_ready=True,
                    max_per_row=4,
                )
        elif platform == "slack":
            sock = getattr(adapter, "socket_client", None)
            if (
                sock
                and callable(getattr(sock, "event_handler", None))
                and callable(getattr(adapter, "create_event", None))
            ):
                return Capabilities(platform, run=True, owner_only=True, callback_ready=True)
        return empty

    def can_send_actions(self, event):
        cap = self.capabilities(event)
        return cap.callback_ready and (cap.run or cap.fill or cap.copy)

    def prepare_event(self, event):
        """发送正文前调用，补齐当前宿主 Slack 线程回复的客户端参数。"""
        if self._closed or event.get_platform_name() != "slack":
            return
        route = self._route(event)
        client = getattr(event, "web_client", None)
        if route.thread and client and not isinstance(client, _SlackThreadClient):
            event.web_client = _SlackThreadClient(client, route.target, route.thread)

    def _prune(self):
        now = time.monotonic()
        for token, ticket in list(self._tickets.items()):
            if ticket.expires <= now:
                del self._tickets[token]
        for key, expires in list(self._seen.items()):
            if expires <= now:
                del self._seen[key]
        alive = []
        for view, expires in self._views:
            if expires <= now:
                view.stop()
            else:
                alive.append((view, expires))
        self._views = alive

    def _token(self, route, action):
        self._prune()
        while len(self._tickets) >= self.MAX_TOKENS:
            self._tickets.popitem(last=False)
        token = self.PREFIX + secrets.token_urlsafe(12)
        self._tickets[token] = _Ticket(
            route, action, time.monotonic() + self.TTL, self._batch.get()
        )
        return token

    def _lookup(self, token, instance, target, thread, user, callback_id):
        self._prune()
        if self._closed or not user or not callback_id:
            return None
        ticket = self._tickets.get(token)
        if ticket is None:
            return None
        route = ticket.route
        if (route.instance, route.target, route.thread) != (instance, str(target), str(thread)):
            return None
        if ticket.action.owner_only and route.owner != str(user):
            return None
        key = (instance, str(callback_id))
        if key in self._seen:
            return None
        # 事件交付前占位，拒绝同一平台回调的重投；不把每次合法点击永久禁掉。
        self._seen[key] = time.monotonic() + self.TTL
        while len(self._seen) > self.MAX_TOKENS:
            self._seen.popitem(last=False)
        return ticket

    def _message(self, ticket, user, nickname, callback_id, raw):
        from astrbot.api.message_components import Plain
        from astrbot.api.platform import AstrBotMessage, MessageMember, MessageType

        route = ticket.route
        message = AstrBotMessage()
        message.type = MessageType.GROUP_MESSAGE if route.group else MessageType.FRIEND_MESSAGE
        message.group_id = route.session if route.group else ""
        message.session_id = route.session
        message.message_id = str(callback_id)
        message.self_id = route.self_id
        message.sender = MessageMember(user_id=str(user), nickname=str(nickname or user))
        message.message_str = ticket.action.send
        message.message = [Plain(text=ticket.action.send)]
        message.raw_message = raw
        return message

    def _dispatch(self, adapter, ticket, user, nickname, callback_id, raw, followup=None):
        message = self._message(ticket, user, nickname, callback_id, raw)
        if ticket.route.platform == "discord":
            event = adapter.create_event(message, followup_webhook=followup)
        else:
            event = adapter.create_event(message)
        if ticket.route.platform == "slack" and ticket.route.thread:
            event.web_client = _SlackThreadClient(
                event.web_client,
                ticket.route.target,
                ticket.route.thread,
            )
        # 只声明点击已唤醒；角色由新事件与当前点击者重新判定，不复制旧事件权限。
        event.is_wake = True
        event.is_at_or_wake_command = True
        adapter.commit_event(event)

    def _install_telegram(self, adapter, route):
        key = (route.instance, route.platform)
        app = adapter.application
        old = self._hooks.get(key)
        if old and old[0] is app:
            return
        if old:
            old[0].remove_handler(old[1], self.TG_GROUP)
        ext = self._sdk("telegram.ext")

        async def callback(update, context):
            query = update.callback_query
            if query is None:
                return
            message, user = query.message, query.from_user
            if message is None or user is None:
                await query.answer("这条按钮没有可识别的会话。")
                return
            thread = str(getattr(message, "message_thread_id", None) or "")
            if thread == "1":
                thread = ""  # AstrBot General topic 的会话 ID 不带 #1。
            ticket = self._lookup(
                query.data,
                route.instance,
                message.chat.id,
                thread,
                user.id,
                query.id,
            )
            await query.answer(None if ticket else "按钮已过期、重复提交或不属于当前会话/用户。")
            if ticket:
                self._dispatch(adapter, ticket, user.id, user.full_name, query.id, update)

        handler = ext.CallbackQueryHandler(callback, pattern=r"^wf:[A-Za-z0-9_-]{16}$")
        app.add_handler(handler, self.TG_GROUP)
        self._hooks[key] = (app, handler)

    def _install_kook(self, adapter, route, client):
        key = (route.instance, route.platform)
        old_hook = self._hooks.get(key)
        if old_hook and old_hook[0] is client and client.event_callback is old_hook[2]:
            return
        if old_hook and old_hook[0] is not client and old_hook[0].event_callback is old_hook[2]:
            old_hook[0].event_callback = old_hook[1]
        previous = client.event_callback

        async def callback(data):
            extra = getattr(data, "extra", None)
            body = getattr(extra, "body", None)
            if (
                getattr(data, "type", None) != 255
                or getattr(extra, "type", None) != "message_btn_click"
                or not isinstance(body, dict)
            ):
                return await previous(data)
            token = body.get("value", "")
            if not isinstance(token, str) or not token.startswith(self.PREFIX):
                return await previous(data)
            stored = self._tickets.get(token)
            if stored is None:
                return
            user = str(body.get("user_id") or "")
            target = body.get("target_id") or (user if not stored.route.group else "")
            callback_id = str(getattr(data, "msg_id", ""))
            ticket = self._lookup(token, route.instance, target, "", user, callback_id)
            if ticket:
                info = body.get("user_info") or {}
                raw = data.to_dict() if callable(getattr(data, "to_dict", None)) else {}
                raw = {
                    **raw,
                    "author_id": user,
                    "content": ticket.action.send,
                    "extra": {**(raw.get("extra") or {}), "author": info},
                }
                self._dispatch(adapter, ticket, user, info.get("username", user), callback_id, raw)

        client.event_callback = callback
        self._hooks[key] = (client, previous, callback)

    def _install_slack(self, adapter, route):
        key = (route.instance, route.platform)
        sock = adapter.socket_client
        old_hook = self._hooks.get(key)
        if old_hook and old_hook[0] is sock and sock.event_handler is old_hook[2]:
            return
        if old_hook and old_hook[0] is not sock and old_hook[0].event_handler is old_hook[2]:
            old_hook[0].event_handler = old_hook[1]
        previous = sock.event_handler

        async def callback(req):
            payload = req.payload or {}
            actions = payload.get("actions") or []
            owned = [a for a in actions if str(a.get("action_id", "")).startswith(self.PREFIX)]
            if req.type != "interactive" or payload.get("type") != "block_actions" or not owned:
                return await previous(req)
            # SlackSocketClient 在调用本入口前已经 ACK；这里不重复响应 envelope。
            user = str((payload.get("user") or {}).get("id") or "")
            channel = str((payload.get("channel") or {}).get("id") or "")
            original = payload.get("message") or {}
            thread = str(original.get("thread_ts") or "")
            for action in owned:
                callback_id = str(getattr(req, "envelope_id", "") or action.get("action_ts") or "")
                ticket = self._lookup(
                    action.get("value"), route.instance, channel, thread, user, callback_id
                )
                if ticket:
                    raw = {
                        "user": user,
                        "channel": channel,
                        "text": ticket.action.send,
                        "ts": action.get("action_ts", ""),
                        "thread_ts": thread,
                    }
                    self._dispatch(adapter, ticket, user, user, callback_id, raw)

        sock.event_handler = callback
        self._hooks[key] = (sock, previous, callback)

    @staticmethod
    def _known_rejection(exc):
        # 超时/5xx/序号冲突可能已经送达。仅明确的请求拒绝允许文字降级。
        if type(exc).__name__ in {
            "ForbiddenError",
            "MethodNotAllowedError",
            "NotFoundError",
            "BadRequest",
        }:
            return True
        if getattr(exc, "code", None) in {40035009, 40035010}:
            return True  # QQ 明确拒绝失效/无效素材引用，不是序号冲突。
        status = getattr(exc, "status", None) or getattr(exc, "status_code", None)
        return status in {401, 403, 404, 405, 413, 422}

    def _select_actions(self, event, actions):
        cap = self.capabilities(event)
        accepted, fallback, rows = [], [], {}
        for action in actions:
            supported = (
                action.mode == "run"
                and (cap.run or cap.platform == "qq_official")
                or action.mode == "fill"
                and cap.fill
                or action.mode == "copy"
                and (cap.copy or cap.copy_as_fill)
            )
            # TG 原生复制没有用户许可字段，不能承诺 owner_only。
            if (
                action.mode == "copy"
                and cap.platform == "telegram"
                and (action.owner_only or not 1 <= len(action.send) <= 256)
            ):
                supported = False
            if (
                not supported
                or not action.label
                or not action.send
                or (action.owner_only and not event.get_sender_id())
                or action.row < 0
                or action.row >= cap.max_rows
                or rows.get(action.row, 0) >= cap.max_per_row
            ):
                fallback.append(action)
            else:
                accepted.append(action)
                rows[action.row] = rows.get(action.row, 0) + 1
        return accepted, fallback

    async def send_actions(self, event, actions, *, remaining_messages=None):
        actions = list(actions)
        cap = self.capabilities(event)
        if (
            not actions
            or not self.can_send_actions(event)
            or (remaining_messages is not None and remaining_messages <= 0)
        ):
            return ActionDelivery(fallback=actions)
        accepted, fallback = self._select_actions(event, actions)
        if not accepted:
            return ActionDelivery(fallback=fallback)
        batch = secrets.token_hex(12)
        batch_context = self._batch.set(batch)
        attempted_context = self._attempted.set(False)
        try:
            await getattr(self, f"_send_{cap.platform}")(event, accepted)
        except Exception as exc:
            known = not self._attempted.get() or self._known_rejection(exc)
            if known:
                for token, ticket in list(self._tickets.items()):
                    if ticket.batch == batch:
                        self._tickets.pop(token, None)
            logger.warning(
                "[warframe] %s 按钮发送%s（%s）",
                cap.platform,
                "被明确拒绝，降级文字" if known else "结果不确定，不自动重发",
                type(exc).__name__,
            )
            return ActionDelivery(fallback=actions if known else fallback, uncertain=not known)
        finally:
            self._batch.reset(batch_context)
            self._attempted.reset(attempted_context)
        return ActionDelivery(sent=True, fallback=fallback)

    def can_send_image_cards(self, event):
        api = getattr(getattr(event, "bot", None), "api", None)
        token = getattr(getattr(api, "_http", None), "_token", None)
        return bool(
            self._qq_media is not None
            and not self._closed
            and self._qq_kind(event)
            and event.get_platform_name() == "qq_official"
            and self.can_send_actions(event)
            and getattr(token, "app_id", None)
        )

    async def send_image_card(self, event, path, actions, *, remaining_messages=None):
        if not self.can_send_image_cards(event) or (
            remaining_messages is not None and remaining_messages <= 0
        ):
            return ImageDelivery(fallback=True)
        accepted, unsupported = self._select_actions(event, list(actions))
        if not accepted:
            return ImageDelivery(fallback=True)
        api = event.bot.api
        account = str(api._http._token.app_id)
        lease = None
        confirmed = False
        attempted = self._attempted.set(False)
        try:
            lease = await self._qq_media.get(account, api, path)
            ref = lease.ref
            if (
                not ref.url.startswith("qqbot-media://image/v1/")
                or not ref.url.removeprefix("qqbot-media://image/v1/")
                or any(c in ref.url for c in "\r\n\t ()[]<>\\")
                or type(ref.width) is not int
                or type(ref.height) is not int
                or not 0 < ref.width <= 16384
                or not 0 < ref.height <= 16384
            ):
                raise ValueError("QQ 图片引用或尺寸无效")
            payload = {
                "markdown": {
                    "content": (
                        f"![Warframe卡片 #{ref.width}px #{ref.height}px]({ref.url})\n\n"
                        + self._qq_button_hint(event)
                    )
                },
                "keyboard": self._qq_keyboard(event, accepted),
            }
            await self._post_qq(event, payload)
            confirmed = True
            return ImageDelivery(sent=True, action_fallback=unsupported)
        except Exception as exc:
            uncertain = self._attempted.get() and not self._known_rejection(exc)
            if lease is not None and not uncertain:
                try:
                    await self._qq_media.invalidate(account, lease.ref)
                except Exception as invalidation_error:
                    logger.warning(
                        "[warframe] QQ 素材撤销失败（%s）", type(invalidation_error).__name__
                    )
            logger.warning(
                "[warframe] QQ 图片合并%s（%s）",
                "结果不确定，不重发" if uncertain else "未发送或明确拒绝，使用原图片路径",
                type(exc).__name__,
            )
            return ImageDelivery(
                fallback=not uncertain, uncertain=uncertain, action_fallback=unsupported
            )
        finally:
            self._attempted.reset(attempted)
            if lease is not None:
                try:
                    await lease.release(confirmed=confirmed)
                except Exception as release_error:
                    logger.warning(
                        "[warframe] QQ 素材租约释放失败（%s）", type(release_error).__name__
                    )

    async def send_markdown(self, event, content, *, remaining_messages=None):
        """仅 QQ 原生正文；尚未发送或已被明确拒绝时，可安全回退原文字链。"""
        if (
            not content
            or self._closed
            or event.get_platform_name() != "qq_official"
            or not self.can_send_actions(event)
            or (remaining_messages is not None and remaining_messages <= 0)
        ):
            return MarkdownDelivery(fallback=True)
        attempted = self._attempted.set(False)
        try:
            await self._post_qq(event, {"markdown": {"content": content}})
        except Exception as exc:
            uncertain = self._attempted.get() and not self._known_rejection(exc)
            logger.warning(
                "[warframe] QQ Markdown 正文%s（%s）",
                "送达不确定，不自动重发" if uncertain else "未发送，降级文字",
                type(exc).__name__,
            )
            return MarkdownDelivery(uncertain=uncertain, fallback=not uncertain)
        finally:
            self._attempted.reset(attempted)
        return MarkdownDelivery(sent=True)

    async def _post_qq(self, event, payload):
        raw, api = self._raw(event), event.bot.api
        group = self._qq_kind(event) == "group"
        seq_key = (str(event.get_platform_id()), str(event.message_obj.message_id))
        sequence = self._qq_seq.get(seq_key, 10000) + 1
        self._qq_seq[seq_key] = sequence
        self._qq_seq.move_to_end(seq_key)
        while len(self._qq_seq) > self.MAX_TOKENS:
            self._qq_seq.popitem(last=False)
        payload = {
            **payload,
            "msg_id": str(event.message_obj.message_id),
            "msg_seq": sequence,
            "msg_type": 2,
        }
        self._attempted.set(True)
        if group:
            result = await api.post_group_message(group_openid=raw.group_openid, **payload)
        else:
            result = await api.post_c2c_message(openid=raw.author.user_openid, **payload)
        if not result:
            raise RuntimeError("QQ 接口未返回送达凭据")

    @staticmethod
    def _qq_button_hint(event):
        return (
            "指令按钮填入输入框后发送；密语填入后请复制到游戏聊天。"
            if getattr(event.message_obj, "group_id", "")
            else "执行按钮直接发送；密语填入后请复制到游戏聊天。"
        )

    def _qq_keyboard(self, event, actions):
        group = self._qq_kind(event) == "group"
        rows = {}
        for action in actions:
            permission = (
                {"type": 0, "specify_user_ids": [str(event.get_sender_id())]}
                if action.owner_only
                else {"type": 2}
            )
            label = action.label.replace("复制", "填入") if action.mode == "copy" else action.label
            button = {
                "id": secrets.token_hex(8),
                "render_data": {"label": label, "visited_label": label, "style": 1},
                "action": {
                    "type": 2,
                    "permission": permission,
                    "data": action.send,
                    "enter": not group and action.mode == "run",
                    "unsupport_tips": "请手动输入指令",
                },
            }
            rows.setdefault(action.row, []).append(button)
        return {"content": {"rows": [{"buttons": rows[row]} for row in sorted(rows)]}}

    async def _send_qq_official(self, event, actions):
        await self._post_qq(
            event,
            {
                "markdown": {"content": self._qq_button_hint(event)},
                "keyboard": self._qq_keyboard(event, actions),
            },
        )

    async def _send_telegram(self, event, actions):
        tg = self._sdk("telegram")
        adapter, route = self._adapter(event), self._route(event)
        self._install_telegram(adapter, route)
        rows = {}
        for action in actions:
            kwargs = (
                {"copy_text": tg.CopyTextButton(action.send)}
                if action.mode == "copy"
                else {
                    "callback_data": self._token(route, action),
                }
            )
            rows.setdefault(action.row, []).append(tg.InlineKeyboardButton(action.label, **kwargs))
        kwargs = {"message_thread_id": int(route.thread)} if route.thread else {}
        self._attempted.set(True)
        await event.client.send_message(
            chat_id=route.target,
            text="请选择操作：",
            parse_mode=None,
            reply_markup=tg.InlineKeyboardMarkup([rows[row] for row in sorted(rows)]),
            **kwargs,
        )

    async def _send_discord(self, event, actions):
        dc = self._sdk("discord")
        adapter, route = self._adapter(event), self._route(event)
        self._prune()
        # View 对象同样有上限，不能只限制 token 而无限保留过期前的闭包。
        while len(self._views) >= self.MAX_TOKENS:
            self._views.pop(0)[0].stop()
        view = dc.ui.View(timeout=self.TTL)
        for action in actions:
            token = self._token(route, action)
            button = dc.ui.Button(label=action.label[:80], custom_id=token, row=action.row)

            async def callback(interaction, token=token):
                user = interaction.user
                ticket = self._lookup(
                    token,
                    route.instance,
                    interaction.channel_id,
                    "",
                    user.id,
                    interaction.id,
                )
                if ticket is None:
                    await interaction.response.send_message(
                        "按钮已过期、重复提交或不属于当前会话/用户。", ephemeral=True
                    )
                    return
                await asyncio.wait_for(interaction.response.defer(), timeout=2.5)
                self._dispatch(
                    adapter,
                    ticket,
                    user.id,
                    user.display_name,
                    interaction.id,
                    interaction,
                    interaction.followup,
                )

            button.callback = callback
            view.add_item(button)
        self._views.append((view, time.monotonic() + self.TTL))
        # 使用原生发送拿到错误，避开宿主 event.send 记录并吞掉异常的路径。
        followup = getattr(event, "interaction_followup_webhook", None)
        if followup:
            self._attempted.set(True)
            await followup.send(content="请选择操作：", view=view)
        else:
            channel = event.client.get_channel(int(route.target))
            if channel is None:
                channel = await event.client.fetch_channel(int(route.target))
            self._attempted.set(True)
            await channel.send(content="请选择操作：", view=view)

    async def _send_kook(self, event, actions):
        from astrbot.core.platform.sources.kook.kook_types import KookMessageType

        adapter, route = self._adapter(event), self._route(event)
        self._install_kook(adapter, route, event.client)
        rows = {}
        for action in actions:
            rows.setdefault(action.row, []).append(
                {
                    "type": "button",
                    "theme": "primary",
                    "click": "return-val",
                    "value": self._token(route, action),
                    "text": {"type": "plain-text", "content": action.label},
                }
            )
        card = [
            {
                "type": "card",
                "theme": "secondary",
                "size": "lg",
                "modules": [
                    {"type": "action-group", "elements": rows[row]} for row in sorted(rows)
                ],
            }
        ]
        self._attempted.set(True)
        await event.client.send_text(
            route.target,
            json.dumps(card, ensure_ascii=False),
            event.message_obj.type,
            KookMessageType.CARD,
        )

    async def _send_slack(self, event, actions):
        adapter, route = self._adapter(event), self._route(event)
        self._install_slack(adapter, route)
        rows = {}
        for action in actions:
            token = self._token(route, action)
            rows.setdefault(action.row, []).append(
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": action.label[:75]},
                    "action_id": token,
                    "value": token,
                }
            )
        kwargs = {"thread_ts": route.thread} if route.thread else {}
        self._attempted.set(True)
        await event.web_client.chat_postMessage(
            channel=route.target,
            text="请选择操作：",
            mrkdwn=False,
            blocks=[{"type": "actions", "elements": rows[row]} for row in sorted(rows)],
            **kwargs,
        )

    @staticmethod
    def _pack(pieces, limit):
        """把 (片段, 长度) 序列切成不超过 ``limit`` 的分片，**优先在换行处切**。

        手机上一行被切到两条消息里像排版坏了；单行本身超长时才退回逐片段硬切。
        各分片按顺序拼接与原文逐字一致（换行留在前一片末尾）。
        """
        lines, line, size = [], [], 0
        for piece, units in pieces:
            line.append((piece, units))
            size += units
            if piece.endswith("\n"):
                lines.append((line, size))
                line, size = [], 0
        if line:
            lines.append((line, size))
        chunk, used = [], 0
        for line, size in lines:
            if used + size > limit and chunk:
                yield "".join(chunk)
                chunk, used = [], 0
            if size <= limit:
                chunk += [piece for piece, _ in line]
                used += size
                continue
            for piece, units in line:  # 单行超长：逐片段硬切
                if used + units > limit and chunk:
                    yield "".join(chunk)
                    chunk, used = [], 0
                chunk.append(piece)
                used += units
        if chunk:
            yield "".join(chunk)

    def _chunk_limit(self, platform):
        """分片上限：面板「text_chunk_limit」填 0 = 按平台自动；手动值封顶到平台上限、
        不低于 CHUNK_MIN（填大了不会因超长被平台拒收，填太小也不会刷屏）。"""
        auto = self.CHUNK_LIMITS[platform]
        try:
            cfg = self._settings() if callable(self._settings) else {}
            value = int((cfg or {}).get("text_chunk_limit") or 0)
        except (TypeError, ValueError):
            value = 0
        return auto if value <= 0 else max(self.CHUNK_MIN, min(value, auto))

    @classmethod
    def _text_chunks(cls, text, limit):
        """按 Unicode 字符切分，以 UTF-16 单位上限兼容含 emoji 的平台长度规则。"""
        yield from cls._pack(((char, 2 if ord(char) > 0xFFFF else 1) for char in text), limit)

    @classmethod
    def _discord_chunks(cls, text, sdk, limit=1900):
        # Discord 没有关闭 Markdown 的消息字段；按官方 SDK 转义保持显示字面内容。
        # 逐字符计算转义后长度，防止分片切断反斜杠转义或 emoji。
        def pieces():
            for char in text:
                literal = sdk.utils.escape_markdown(char, as_needed=False, ignore_links=False)
                yield literal, len(literal.encode("utf-16-le")) // 2

        yield from cls._pack(pieces(), limit)

    async def send_proactive(self, umo, text):
        """仅按 UMO 绑定发送目标；不构造用户权限，不提交入站事件。

        False 仅代表没有尝试发送。已经开始发送后的异常抛给调用方，禁止主链
        因返回 False 再用宿主重发同一条。QQ 官方主动消息继续交由宿主既有策略。
        """
        if self._closed:
            return False
        getter = getattr(self.context, "get_platform_inst", None)
        if not callable(getter):
            return False
        sessions = self._sdk("astrbot.core.platform.message_session")
        if sessions is None:
            return False
        from astrbot.api.platform import AstrBotMessage, MessageMember, MessageType

        session = sessions.MessageSesion.from_str(umo) if isinstance(umo, str) else umo
        adapter = getter(session.platform_name)
        if adapter is None or not callable(getattr(adapter, "create_event", None)):
            return False
        platform = adapter.meta().name
        if platform not in {"telegram", "slack", "kook", "discord"}:
            return False
        target = session.session_id
        # 与当前宿主 send_by_session 使用相同的历史会话兼容规则。
        if platform == "slack" and session.message_type == MessageType.GROUP_MESSAGE:
            target = target.split("_")[-1]
        elif platform == "discord" and "_" in target:
            target = target.split("_")[1]
        message = AstrBotMessage()
        message.type = session.message_type
        message.session_id = target
        message.group_id = target if session.message_type == MessageType.GROUP_MESSAGE else ""
        message.message_str = text
        message.sender = MessageMember(user_id="", nickname="")
        message.raw_message = {"channel": target} if platform == "slack" else None
        event = adapter.create_event(message)
        delivered = await self.send_literal(event, text)
        if delivered:
            # 与 Context.send_message 一致保留可选的群消息历史；历史记录故障不能
            # 把已发送消息变成失败并诱发推送重试。
            try:
                getter = getattr(self.context, "get_config", None)
                history = getattr(self.context, "message_history_manager", None)
                settings = (
                    getter(umo=str(session)).get("provider_ltm_settings", {}) if getter else {}
                )
                if (
                    session.message_type == MessageType.GROUP_MESSAGE
                    and history
                    and settings.get("group_message_history_enable", False)
                ):
                    from astrbot.api.event import MessageChain
                    from astrbot.api.message_components import Plain

                    await history.insert_message_chain(
                        platform_id=session.platform_id,
                        user_id=str(session),
                        message_chain=MessageChain([Plain(text=text)]),
                        role="bot",
                        sender_id="bot",
                        sender_name="bot",
                        max_messages=max(
                            1, int(settings.get("group_message_history_max_cnt", 700))
                        ),
                    )
            except Exception as exc:
                logger.warning(
                    "[warframe] 主动消息已送出，但历史记录失败（%s）", type(exc).__name__
                )
        return delivered

    async def send_literal(self, event, text):
        if self._closed:
            return False
        route = self._route(event)
        if route.platform == "discord" and getattr(event, "client", None):
            sdk = self._sdk("discord")
            if sdk is None or not hasattr(sdk, "utils"):
                return False
            followup = getattr(event, "interaction_followup_webhook", None)
            if followup is None:
                channel = event.client.get_channel(int(route.target))
                if channel is None:
                    channel = await event.client.fetch_channel(int(route.target))
                send = channel.send
            else:
                send = followup.send
            for part in self._discord_chunks(text, sdk, self._chunk_limit("discord")):
                await send(content=part, allowed_mentions=sdk.AllowedMentions.none())
            return True
        if route.platform == "telegram" and getattr(event, "client", None):
            # 先分片，已成功的前片不能因为后片失败而被主链重发。
            kwargs = {"message_thread_id": int(route.thread)} if route.thread else {}
            for part in self._text_chunks(text, self._chunk_limit("telegram")):
                await event.client.send_message(
                    chat_id=route.target,
                    text=part,
                    parse_mode=None,
                    **kwargs,
                )
            return True
        if route.platform == "slack" and getattr(event, "web_client", None):
            kwargs = {"thread_ts": route.thread} if route.thread else {}
            for part in self._text_chunks(text, self._chunk_limit("slack")):
                await event.web_client.chat_postMessage(
                    channel=route.target,
                    text=part,
                    mrkdwn=False,
                    parse="none",
                    blocks=[{"type": "section", "text": {"type": "plain_text", "text": part}}],
                    **kwargs,
                )
            return True
        if route.platform == "kook" and getattr(event, "client", None):
            from astrbot.core.platform.sources.kook.kook_types import KookMessageType

            for part in self._text_chunks(text, self._chunk_limit("kook")):
                await event.client.send_text(
                    route.target,
                    part,
                    event.message_obj.type,
                    KookMessageType.TEXT,
                )
            return True
        return False

    async def close(self):
        self._closed = True
        if self._qq_media is not None:
            try:
                await self._qq_media.close()
            except Exception as exc:
                logger.warning("[warframe] QQ 素材管理关闭失败（%s）", type(exc).__name__)
        self._tickets.clear()
        self._seen.clear()
        self._qq_seq.clear()
        for (_instance, platform), hook in self._hooks.items():
            try:
                if platform == "telegram":
                    hook[0].remove_handler(hook[1], self.TG_GROUP)
                elif platform == "kook" and hook[0].event_callback is hook[2]:
                    hook[0].event_callback = hook[1]
                elif platform == "slack" and hook[0].event_handler is hook[2]:
                    hook[0].event_handler = hook[1]
            except Exception as exc:
                logger.warning("[warframe] %s 按钮回调清理失败（%s）", platform, type(exc).__name__)
        self._hooks.clear()
        for view, _expires in self._views:
            try:
                view.stop()
            except Exception as exc:
                logger.warning("[warframe] Discord 按钮视图清理失败（%s）", type(exc).__name__)
        self._views.clear()
