# -*- coding: utf-8 -*-
"""群管理指令（`.锚点 / .状态 / .默认平台`）回归测试。

直接 import main.py 需要 astrbot 运行环境；本测试在导入前注入轻量 astrbot 桩，
构造 WarframeSDJK 实例（绕过 __init__）后直接调用 `_handle_admin`，覆盖：
  - L-1：`.锚点` 缺失 `is_admin` 校验的回归（应被权限拦截）
  - L-3：`.锚点` 写入路径为运行时目录而非硬编码 `/AstrBot/data/config/...`
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
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
        def __init__(self):
            self.unified_msg_origin = "group://admin_test"

        def get_sender_name(self) -> str:
            return "stub_user"

        def get_sender_id(self) -> str:
            return "stub_id"

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
from core.store import GroupStore, Subscription  # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(
        f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" -> {detail}" if detail and not cond else "")
    )
    if not cond:
        FAILED.append(name)


class _MemberEvent:
    """普通群成员（role=member）事件。"""

    def __init__(self):
        self.unified_msg_origin = "group://admin_test"
        self.role = "member"

    def get_sender_id(self) -> str:
        return "ordinary_user"

    def get_sender_name(self) -> str:
        return "ordinary"


class _AdminEvent:
    """群管理员（role=admin）事件。"""

    def __init__(self):
        self.unified_msg_origin = "group://admin_test"
        self.role = "admin"

    def get_sender_id(self) -> str:
        return "admin_user"

    def get_sender_name(self) -> str:
        return "admin"


def _make_plugin(tmp: Path):
    obj = plugin.WarframeSDJK.__new__(plugin.WarframeSDJK)
    obj.cfg = {}
    obj.groups = GroupStore(tmp / "groups.json", default_platform="pc")
    return obj


async def main() -> None:
    tmp = Path(tempfile.mkdtemp())
    obj = _make_plugin(tmp)

    # 1) `.锚点` 已废弃（2026-09-18 安全审查）：它写入的锚点**无任何读取方**
    #    （仲裁表改用 arbi.wf.wiki 确定性排期），却会写全局 cfg —— 任何群的
    #    群管都能覆盖。按项目铁律「失效功能必须给出真实可用的替代」，
    #    现在一律返回废弃说明 + 替代指令。
    for ev, who in ((_MemberEvent(), "普通成员"), (_AdminEvent(), "管理员")):
        reply = await obj._handle_admin(ev, ".锚点 Palus")
        txt = reply.raw_text if reply else ""
        check(f"{who} .锚点 得到「已废弃」说明", "已废弃" in txt, repr(txt))
        check(
            f"{who} .锚点 的提示里给出真实替代指令（仲裁 / 仲裁表）",
            "「仲裁」" in txt and "仲裁表" in txt,
            repr(txt),
        )

    # 2) ★ 安全断言：`.锚点` 不再写任何全局状态
    #    （以前会写 cfg["arb_anchor"] 与 runtime/arb_anchor.json）
    #    2026-09-19：运行期数据目录已迁到 data/plugin_data/<插件名>，两个位置都查。
    legacy_path = plugin.PLUGIN_DIR / "runtime" / "arb_anchor.json"
    data_path = Path(plugin._resolve_data_dir()) / "arb_anchor.json"
    before = (
        legacy_path.stat().st_mtime if legacy_path.exists() else None,
        data_path.stat().st_mtime if data_path.exists() else None,
    )
    await obj._handle_admin(_AdminEvent(), ".锚点 Palus")
    after = (
        legacy_path.stat().st_mtime if legacy_path.exists() else None,
        data_path.stat().st_mtime if data_path.exists() else None,
    )
    check(
        "★ .锚点 不再写入 arb_anchor.json（跨租户写入已消除）",
        before == after,
        f"mtime {before} → {after}",
    )
    check(
        "★ .锚点 不再写 cfg['arb_anchor']",
        "arb_anchor" not in obj.cfg,
        str(obj.cfg.get("arb_anchor")),
    )

    # 3) `.状态` 会列出本群订阅明细 → 必须要求管理权限（2026-09-18 新增）
    reply = await obj._handle_admin(_MemberEvent(), ".状态")
    check(
        "普通成员 .状态 被权限拦截（含订阅明细）",
        reply is not None and reply.raw_text is not None and "需要群管理员权限" in reply.raw_text,
        repr(reply.raw_text if reply else None),
    )

    # 4) 管理员仍可用 `.状态`
    obj2 = _make_plugin(tmp)
    obj2.subs = plugin.SubscriptionStore(tmp / "subs.json")
    obj2.client = type(
        "C",
        (),
        {"cache": type("K", (), {"stats": staticmethod(lambda: {"size": 0, "hit_rate": 0.0})})()},
    )()
    reply = await obj2._handle_admin(_AdminEvent(), ".状态")
    check("管理员 .状态 正常返回", reply is not None and reply.raw_text is None, repr(reply))

    # 5) 普通成员也不能 .默认平台 / .开启 / .关闭
    for cmd in (".默认平台 ps", ".开启 推送", ".关闭 推送"):
        reply = await obj._handle_admin(_MemberEvent(), cmd)
        check(
            f"普通成员 {cmd} 被拦截",
            reply is not None
            and reply.raw_text is not None
            and "需要群管理员权限" in reply.raw_text,
            repr(reply.raw_text if reply else None),
        )

    # 6) 主指令「状态」直接委托 .状态（_h_status_cmd）→ 同一道闸门，普通成员
    #    也拿不到；帮助卡里这两处都标了「需群管理员」（tests/test_help_coverage.py
    #    第 4 条锁卡面标注）。2026-09-24 自查补：此前只有 .状态 的断言。
    reply = await obj._h_status_cmd(None, _MemberEvent(), "pc")
    check(
        "普通成员「状态」也被权限拦截（与 .状态 同源）",
        reply is not None and reply.raw_text is not None and "需要群管理员权限" in reply.raw_text,
        repr(reply.raw_text if reply else None),
    )
    reply = await obj2._h_status_cmd(None, _AdminEvent(), "pc")
    check("管理员「状态」正常返回卡片", reply is not None and reply.raw_text is None, repr(reply))

    # 7) 2026-10-11：.关闭 推送 = 该群已有订阅暂停（订阅保留）；重新开启后恢复
    obj3 = _make_plugin(tmp)
    obj3.subs = plugin.SubscriptionStore(tmp / "subs_pause.json")
    for ev in ("夜灵", "裂隙"):
        await obj3.subs.add(Subscription(umo="group://admin_test", platform="pc", event=ev))
    await obj3.subs.add(Subscription(umo="group://other", platform="pc", event="夜灵"))
    await obj3.groups.set_switch("group://admin_test", "push", True)
    reply = await obj3._handle_admin(_AdminEvent(), ".关闭 推送")
    txt = reply.raw_text if reply else ""
    check(
        "★ .关闭 推送 回复写明「已有 2 条订阅暂停」（只数本群）",
        "已有 2 条订阅暂停" in txt and "恢复" in txt,
        repr(txt),
    )
    check(
        "★ .关闭 推送 只改开关：订阅全部保留，群开关为关",
        len(obj3.subs.all()) == 3 and obj3.groups.get("group://admin_test")["push"] is False,
    )
    reply = await obj3._handle_admin(_AdminEvent(), ".开启 推送")
    txt = reply.raw_text if reply else ""
    check(
        "★ .开启 推送 回复写明已有 2 条订阅恢复",
        "已有 2 条订阅恢复推送" in txt and obj3.groups.get("group://admin_test")["push"] is True,
        repr(txt),
    )
    obj7 = _make_plugin(tmp)
    obj7.subs = plugin.SubscriptionStore(tmp / "subs_empty.json")
    reply = await obj7._handle_admin(_AdminEvent(), ".关闭 推送")
    check(
        "护栏：没有订阅时关闭回复不带「暂停」字样",
        reply is not None and "暂停" not in (reply.raw_text or "") and "已关闭" in reply.raw_text,
        repr(reply.raw_text if reply else None),
    )
    reply = await obj7._handle_admin(_AdminEvent(), ".关闭 聊天")
    check(
        "护栏：.关闭 聊天 不带订阅暂停说明",
        reply is not None and "暂停" not in (reply.raw_text or ""),
        repr(reply.raw_text if reply else None),
    )
    import inspect as _inspect

    _init_src = _inspect.getsource(plugin.WarframeSDJK.__init__)
    check(
        "接线：PushDaemon 构造注入 groups 与基线落盘路径；initialize 挂 done-callback",
        "groups=self.groups" in _init_src
        and "push_baseline.json" in _init_src
        and 'self._log_task_death("push_daemon")'
        in _inspect.getsource(plugin.WarframeSDJK.initialize),
    )


async def terminate_checks() -> None:
    """2026-10-17（v1.2.2 部署实测）：reload 后旧实例的首启预热还在已关闭的 client 上发请求。"""
    from core.api_client import WarframeAPIError, WarframeClient

    tmp = Path(tempfile.mkdtemp())
    obj = _make_plugin(tmp)
    obj.client = WarframeClient()
    events: list[str] = []

    class _Push:
        async def stop(self):
            events.append("push.stop")

    obj.push = _Push()
    obj._panel = None
    real_close = obj.client.close

    async def _close():
        events.append("client.close")
        await real_close()

    obj.client.close = _close

    async def _hanging_warm():
        try:
            await asyncio.sleep(30)  # 模拟预热还在等网络
        except asyncio.CancelledError:
            events.append("boot.cancelled")
            raise

    obj._boot_task = asyncio.create_task(_hanging_warm())
    obj._warm_task = asyncio.create_task(asyncio.sleep(30))  # 线程预热（P08）：纳入统一收尾
    await asyncio.sleep(0)
    await obj.terminate()
    check("terminate 取消首启预热任务（_boot_task）", obj._boot_task.cancelled(), str(events))
    check("terminate 也收尾线程预热任务（_warm_task）", obj._warm_task.cancelled())
    check(
        "预热任务先退出、再关 client（不在已关闭的连接上发请求）",
        "boot.cancelled" in events
        and "client.close" in events
        and events.index("boot.cancelled") < events.index("client.close"),
        str(events),
    )
    try:
        await obj.client._fetch_json("https://example.invalid/x.json", ttl=1, retries=0)
        why = ""
    except WarframeAPIError as exc:
        why = str(exc)
    check("client 关闭后请求直接报「插件已卸载」，不发出", "已关闭" in why, why)


async def dot_status_dispatch_checks() -> None:
    """★ 2026-10-25（Astra 真实宿主补测）：群管理员发 `.状态` 静默 —— 点号分支只发 raw_text，
    而 `.状态` 返回图卡（lines）。走完整 on_message 分发，断言**确有输出**且走出图流程。"""
    tmp = Path(tempfile.mkdtemp())
    obj = _make_plugin(tmp)
    obj.subs = types.SimpleNamespace(for_umo=lambda _u: [])
    obj.client = types.SimpleNamespace(
        cache=types.SimpleNamespace(stats=lambda: {"size": 0, "hit_rate": 0.0})
    )
    built: list = []

    async def _fake_build(event, reply, parsed):
        built.append(reply)
        yield event.plain_result(reply.raw_text) if reply.raw_text is not None else "IMG"

    obj._build_results = _fake_build

    class _Ev(_AdminEvent):
        message_str = ".状态"

        def plain_result(self, text):
            return ("TEXT", text)

        def stop_event(self):
            pass

    out = [r async for r in obj.on_message(_Ev())]
    check(
        "★ 管理员 .状态 有输出（图卡走出图流程，不再发空串）",
        out == ["IMG"] and built and built[0].lines and built[0].raw_text is None,
        str(out),
    )
    out2 = [r async for r in obj.on_message(type("E", (_Ev,), {"message_str": ".默认平台"})())]
    check(
        "纯文字管理回复经统一出口仍发文字",
        len(out2) == 1
        and out2[0][0] == "TEXT"
        and out2[0][1]
        and len(built) == 2
        and built[-1].raw_text is not None,
        str(out2),
    )


async def legacy_switch_precedence_checks() -> None:
    """真实管理入口：规范键优先且显式设置只迁移当前开关，不受旧键顺序影响。"""
    import copy
    import json

    from core.push import PushDaemon
    from core.store import SubscriptionStore

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        event = _AdminEvent()
        umo = event.unified_msg_origin
        for legacy, canonical in (("推送", "push"), ("聊天", "quiet")):
            for canonical_first in (False, True):
                for wanted in (False, True):
                    ordered = [(canonical, not wanted), (legacy, wanted)]
                    if not canonical_first:
                        ordered.reverse()
                    other_keys = (
                        {"quiet": False, "聊天": True}
                        if canonical == "push"
                        else {"push": False, "推送": True}
                    )
                    target = {
                        **dict(ordered),
                        **other_keys,
                        "platform": "ps",
                        "extra": {"kept": [1, 2]},
                        "push_pause": {"since": 1, "next_try": 2, "fails": 5},
                    }
                    original = {
                        umo: target,
                        "other": {"push": True, "推送": False, "extra": [3]},
                        "bad-record": ["preserve"],
                    }
                    path = tmp / "groups.json"
                    path.write_text(json.dumps(original), encoding="utf-8")
                    before_bytes = path.read_bytes()
                    obj = _make_plugin(tmp)
                    obj.subs = SubscriptionStore(tmp / "subs.json")
                    label = f"旧开关{legacy}/{'规范先' if canonical_first else '中文先'}/{wanted}"
                    check(
                        f"{label}：只读始终优先规范键",
                        obj.groups.get(umo)[canonical] is not wanted,
                    )
                    check(f"{label}：只读不改原存档", path.read_bytes() == before_bytes)
                    command = f".{'开启' if wanted else '关闭'} {legacy}"
                    reply = await obj._handle_admin(event, command)
                    check(
                        f"{label}：真实管理回复与读值一致",
                        ("已开启" if wanted else "已关闭") in reply.raw_text
                        and obj.groups.get(umo)[canonical] is wanted,
                    )
                    expected = copy.deepcopy(original)
                    expected[umo][canonical] = wanted
                    expected[umo].pop(legacy)
                    if canonical == "push" and wanted:
                        expected[umo].pop("push_pause")
                    check(
                        f"{label}：只迁移目标键且保留其它字段与会话",
                        json.loads(path.read_text()) == expected,
                    )
                    reread = GroupStore(path)
                    daemon = PushDaemon(None, obj.subs, None, None, groups=reread)
                    check(
                        f"{label}：重载后读值与守护开关保持一致",
                        reread.get(umo)[canonical] is wanted
                        and daemon._group_on(umo) is (wanted if canonical == "push" else False),
                    )

        for legacy, canonical in (("推送", "push"), ("聊天", "quiet")):
            for value in (False, True):
                path = tmp / "groups.json"
                path.write_text(json.dumps({umo: {legacy: value}}), encoding="utf-8")
                before_bytes = path.read_bytes()
                store = GroupStore(path)
                check(
                    f"纯中文旧开关 {legacy}/{value}：继续读取且不静默迁移",
                    store.get(umo)[canonical] is value and path.read_bytes() == before_bytes,
                )


class _QQOfficialGroupEvent(_MemberEvent):
    """QQ 官方群事件形状；只在 raw_data 保留平台原始 author.member_role。"""

    def __init__(
        self,
        *,
        platform="qq_official",
        group_id="test_group_a",
        sender_id="test_member_a",
        raw_data=...,
    ):
        super().__init__()
        self._platform = platform
        self._group_id = group_id
        self._sender_id = sender_id
        self._astrbot_admin = False
        self.unified_msg_origin = f"{platform}:GroupMessage:{group_id}"
        if raw_data is ...:
            raw_data = {
                "group_openid": group_id,
                "author": {"member_openid": sender_id, "member_role": "admin"},
            }
        self.message_obj = types.SimpleNamespace(
            raw_message=types.SimpleNamespace(raw_data=raw_data)
        )

    def get_platform_name(self):
        return self._platform

    def get_group_id(self):
        return self._group_id

    def get_sender_id(self):
        return self._sender_id

    def is_admin(self):
        return self._astrbot_admin


async def qq_official_group_admin_checks() -> None:
    """真实管理入口验证 QQ 原始角色的群、发送者绑定与撤权，不修改事件全局角色。"""
    import copy

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        case_index = 0

        async def run_case(label, event, allowed, *, config=None):
            nonlocal case_index
            case_index += 1
            case_dir = tmp / str(case_index)
            case_dir.mkdir()
            obj = _make_plugin(case_dir)
            obj.cfg = config or {}
            obj.subs = types.SimpleNamespace(for_umo=lambda _u: [])
            await obj.groups.set_switch(event.unified_msg_origin, "推送", False)
            before_bytes = (case_dir / "groups.json").read_bytes()
            before_raw = copy.deepcopy(event.message_obj.raw_message.raw_data)
            before_role = event.role
            reply = await obj._handle_admin(event, ".开启 推送")
            opened = reply is not None and "推送功能已开启" in (reply.raw_text or "")
            push = obj.groups.get(event.unified_msg_origin)["push"]
            unchanged = (case_dir / "groups.json").read_bytes() == before_bytes
            check(
                f"QQ 官方权限：{label}",
                (opened and push is True)
                if allowed
                else (reply is not None and not opened and push is False and unchanged),
                repr(reply),
            )
            check(
                f"QQ 官方权限：{label}不提升事件全局角色或改原始事件",
                event.role == before_role and event.message_obj.raw_message.raw_data == before_raw,
            )
            return obj

        for role in ("admin", "owner"):
            event = _QQOfficialGroupEvent()
            event.message_obj.raw_message.raw_data["author"]["member_role"] = role
            await run_case(f"当前群当前发送者 {role} 可开启推送", event, True)

        for role in ("member", None, "", "administrator", "ADMIN", " owner ", 1, True, ["owner"]):
            event = _QQOfficialGroupEvent()
            event.message_obj.raw_message.raw_data["author"]["member_role"] = role
            await run_case(f"角色 {role!r} 不授权", event, False)

        event = _QQOfficialGroupEvent()
        del event.message_obj.raw_message.raw_data["author"]["member_role"]
        await run_case("缺少角色不授权", event, False)

        for field in ("group_openid", "member_openid"):
            event = _QQOfficialGroupEvent()
            data = event.message_obj.raw_message.raw_data
            del (data if field == "group_openid" else data["author"])[field]
            await run_case(f"缺少 {field} 不授权", event, False)

        for field in ("group_openid", "member_openid"):
            event = _QQOfficialGroupEvent()
            data = event.message_obj.raw_message.raw_data
            (data if field == "group_openid" else data["author"])[field] = "other_scope"
            await run_case(f"{field} 不匹配当前事件不授权", event, False)

        for raw_data in (None, [], "invalid", {"group_openid": "test_group_a", "author": []}):
            await run_case(
                f"原始载荷 {raw_data!r} 非法不授权",
                _QQOfficialGroupEvent(raw_data=raw_data),
                False,
            )

        for platform in ("aiocqhttp", "discord", ""):
            await run_case(
                f"平台 {platform!r} 不借用 QQ 原始角色授权",
                _QQOfficialGroupEvent(platform=platform),
                False,
            )

        for kwargs in ({"group_id": ""}, {"sender_id": ""}):
            await run_case(
                f"事件身份 {kwargs!r} 为空不授权", _QQOfficialGroupEvent(**kwargs), False
            )

        original = _QQOfficialGroupEvent().message_obj.raw_message.raw_data
        for kwargs in ({"group_id": "test_group_b"}, {"sender_id": "test_member_b"}):
            await run_case(
                f"原事件跨群或跨用户重放 {kwargs!r} 不授权",
                _QQOfficialGroupEvent(raw_data=copy.deepcopy(original), **kwargs),
                False,
            )

        event = _QQOfficialGroupEvent()
        data = event.message_obj.raw_message.raw_data
        data["author"]["member_role"] = "member"
        data["message_reference"] = {"message": copy.deepcopy(original)}
        data["message_reference"]["message"]["author"]["member_role"] = "owner"
        await run_case("引用消息中的 owner 不能授权当前普通成员", event, False)

        event = _QQOfficialGroupEvent()
        obj = await run_case("撤权前 admin 可操作", event, True)
        event = _QQOfficialGroupEvent()
        event.message_obj.raw_message.raw_data["author"]["member_role"] = "member"
        reply = await obj._handle_admin(event, ".关闭 推送")
        check(
            "QQ 官方权限：角色撤销后的下一条管理指令立即拒绝，不缓存旧权限",
            reply is not None
            and "推送功能已关闭" not in (reply.raw_text or "")
            and obj.groups.get(event.unified_msg_origin)["push"] is True
            and event.role == "member",
            repr(reply),
        )

        for kwargs in ({"sender_id": "test_member_b"}, {"group_id": "test_group_b"}):
            event = _QQOfficialGroupEvent(**kwargs)
            event.message_obj.raw_message.raw_data["author"]["member_role"] = "member"
            before = copy.deepcopy(obj.groups.get(event.unified_msg_origin))
            reply = await obj._handle_admin(event, ".开启 推送")
            check(
                f"QQ 官方权限：同一插件实例不将旧管理员权限借给 {kwargs!r}",
                reply is not None
                and "推送功能已开启" not in (reply.raw_text or "")
                and obj.groups.get(event.unified_msg_origin) == before
                and event.role == "member",
                repr(reply),
            )

        event = _QQOfficialGroupEvent(raw_data=None)
        event._astrbot_admin = True
        await run_case("既有 AstrBot 超管在无群角色时仍可操作", event, True)
        event = _QQOfficialGroupEvent(raw_data=None)
        await run_case(
            "既有插件 super_admins 仍精确匹配 sender_id",
            event,
            True,
            config={"super_admins": [event.get_sender_id()]},
        )
        await run_case(
            "插件 super_admins 不匹配 sender_id 时仍拒绝",
            _QQOfficialGroupEvent(raw_data=None),
            False,
            config={"super_admins": ["test_other_member"]},
        )


if __name__ == "__main__":
    asyncio.run(main())
    asyncio.run(terminate_checks())
    asyncio.run(dot_status_dispatch_checks())
    asyncio.run(legacy_switch_precedence_checks())
    asyncio.run(qq_official_group_admin_checks())
    print()
    if FAILED:
        print(f"共 {len(FAILED)} 项失败：{FAILED}")
        sys.exit(1)
    print("管理指令回归测试全部通过 ✔")
