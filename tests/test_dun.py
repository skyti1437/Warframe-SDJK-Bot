# -*- coding: utf-8 -*-
"""蹲（后台推送订阅）handler 回归测试。

直接 import main.py 需要 astrbot 运行环境；本测试在导入前注入轻量 astrbot 桩，
构造 WarframeSDJK 实例（绕过 __init__）后直接调用 `_h_dun`，覆盖：
  - 「蹲 类型」正常订阅（此前因 wired 变量作用域 NameError 直接失效）
  - 「蹲 帮助」类型列表
  - 未识别类型报错（不静默降级）
  - 「蹲 取消」/「蹲 类型 取消」取消逻辑
  - 推送未开启时拒绝订阅
依赖本地 JSON 存储，无网络。
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
    """注入最小 astrbot.api 桩，使 main.py 可在无运行环境下 import。"""
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
        def __init__(self, umo: str = "group://test", sender: str = "tester"):
            self.unified_msg_origin = umo
            self._sender = sender
        def get_sender_name(self) -> str:
            return self._sender

    class MessageChain:
        def message(self, text):  # noqa: ANN001
            return text

    class _EventMessageType:
        ALL = "ALL"

    class _Filter:
        # main.py: `from astrbot.api.event import ... filter`
        # 之后用 `filter.event_message_type(...)` / `filter.EventMessageType.ALL`
        EventMessageType = _EventMessageType
        event_message_type = staticmethod(lambda spec: (lambda fn: fn))

    event_mod.AstrMessageEvent = AstrMessageEvent
    event_mod.MessageChain = MessageChain
    event_mod.EventMessageType = _EventMessageType
    event_mod.event_message_type = lambda spec: (lambda fn: fn)  # 装饰器工厂占位
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
        def __init__(self, *a, **k):  # noqa: ANN002, ANN003
            pass

    def register(*a, **k):  # noqa: ANN002, ANN003
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
from core.parser import parse  # noqa: E402
from core.store import GroupStore, SubscriptionStore  # noqa: E402


def _make_plugin(tmp: Path) -> "plugin.WarframeSDJK":
    """绕过 __init__ 构造实例，仅挂载 _h_dun 需要的存储。"""
    obj = plugin.WarframeSDJK.__new__(plugin.WarframeSDJK)
    obj.subs = SubscriptionStore(tmp / "subs.json")
    obj.groups = GroupStore(tmp / "groups.json", default_platform="pc")
    obj.push = None
    return obj


async def _run_one(obj, text: str, umo: str = "group://test"):
    parsed = parse(text)
    event = plugin.AstrMessageEvent(umo=umo, sender="tester") \
        if hasattr(plugin, "AstrMessageEvent") else None
    # 用桩事件：直接构造，避免依赖 stub 名
    event = _StubEvent(umo)
    return await obj._h_dun(parsed, event, "pc")


class _StubEvent:
    """最小事件桩。

    role="admin"：`.状态` 自 2026-09-18 起要求管理权限（它会列出本群订阅
    明细），本测试要验证的是「出图形卡不崩」，所以按管理员身份调用。
    """

    def __init__(self, umo: str, role: str = "admin"):
        self.unified_msg_origin = umo
        self.role = role
    def get_sender_name(self) -> str:
        return "tester"
    def get_sender_id(self) -> str:
        return "tester_id"


FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" -> {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


async def main() -> None:
    tmp = Path(tempfile.mkdtemp())
    obj = _make_plugin(tmp)
    # 订阅类用例需要先开启本群推送（测试 5/7 单独验证“未开启”拒绝逻辑）
    await obj.groups.set_switch("group://test", "push", True)

    # 1) 正常订阅「蹲 裂隙」——此前会因 NameError 直接失效
    reply = await _run_one(obj, "蹲 裂隙")
    check("「蹲 裂隙」成功返回订阅卡片",
          reply is not None and "订阅成功" in (reply.title or ""),
          repr(reply.title if reply else None))
    check("「蹲 裂隙」写入了订阅", len(obj.subs.all()) == 1,
          str([s.to_dict() for s in obj.subs.all()]))
    if obj.subs.all():
        check("订阅事件类型为 裂隙", obj.subs.all()[0].event == "裂隙")

    # 2) 带筛选+时长+窗口
    reply = await _run_one(obj, "蹲 裂隙 钢铁虚空生存 永久 22到8")
    check("「蹲 裂隙 钢铁... 永久 22到8」成功",
          reply is not None and "订阅成功" in (reply.title or ""),
          repr(reply.title if reply else None))
    check("带筛选的订阅总数=2", len(obj.subs.all()) == 2)

    # 3) 帮助
    reply = await _run_one(obj, "蹲 帮助")
    check("「蹲 帮助」返回类型列表",
          reply is not None and reply.text_only and "可蹲类型" in (reply.title or ""),
          repr(reply.title if reply else None))

    # 4) 未识别类型（不静默降级）
    reply = await _run_one(obj, "蹲 火星")
    check("「蹲 火星」报错而非静默降级",
          reply is not None and reply.raw_text is not None
          and "未识别为可蹲类型" in reply.raw_text,
          repr(reply.raw_text if reply else None))

    # 5) 推送未开启时拒绝
    obj2 = _make_plugin(Path(tempfile.mkdtemp()))
    # 确保 push=False（默认）
    reply = await _run_one(obj2, "蹲 突击")
    check("推送未开启时拒绝订阅",
          reply is not None and reply.raw_text is not None
          and "推送功能未开启" in reply.raw_text,
          repr(reply.raw_text if reply else None))
    check("未开启时不应写入订阅", len(obj2.subs.all()) == 0)

    # 6) 开启后正常订阅
    await obj2.groups.set_switch("group://test", "push", True)
    reply = await _run_one(obj2, "蹲 突击")
    check("开启推送后「蹲 突击」成功",
          reply is not None and "订阅成功" in (reply.title or ""),
          repr(reply.title if reply else None))

    # 7) 取消全部
    reply = await _run_one(obj2, "蹲 取消")
    check("「蹲 取消」清空订阅",
          reply is not None and "已取消" in (reply.raw_text or "")
          and len(obj2.subs.all()) == 0,
          repr(reply.raw_text if reply else None) + f" | left={len(obj2.subs.all())}")

    # 8) 「蹲 类型 取消」
    await obj2.groups.set_switch("group://test", "push", True)
    await _run_one(obj2, "蹲 夜灵")
    await _run_one(obj2, "蹲 奸商")
    reply = await _run_one(obj2, "蹲 夜灵 取消")
    check("「蹲 夜灵 取消」只取消夜灵",
          len(obj2.subs.all()) == 1 and obj2.subs.all()[0].event == "奸商",
          str([s.event for s in obj2.subs.all()]))

    # 9) L-2: created_by 不再落盘 QQ 昵称（哪怕 stub 返回 "tester"）
    # ★ 2026-09-19：警报在 DE 数据里恒为空（系统停用）已被标为不可订阅，
    #   本组断言改用「新闻」（可订阅、无筛选）。
    reply = await _run_one(obj2, "蹲 新闻")
    last = obj2.subs.all()[-1]
    check("L-2 created_by 不落盘 QQ 昵称（应为空串）",
          last.created_by == "",
          repr(last.created_by))

    # 10) L-4: 单 umo 蹲订阅上限（默认 30）
    obj3 = _make_plugin(Path(tempfile.mkdtemp()))
    await obj3.groups.set_switch("group://limit", "push", True)
    # 先塞 30 条不同 umo 的订阅绕开限额 = 把 obj3 的 groups 切换过来前先填到 29
    for i in range(29):
        await _run_one(obj3, "蹲 新闻", umo="group://limit")
    # 第 30 条允许通过
    r30 = await _run_one(obj3, "蹲 新闻", umo="group://limit")
    r31 = await _run_one(obj3, "蹲 新闻", umo="group://limit")
    n = len(obj3.subs.for_umo("group://limit"))
    # ★ 2026-09-19 新增：DE 已停用警报系统 → 订阅时必须明确拒绝并说明原因
    obj4 = _make_plugin(Path(tempfile.mkdtemp()))
    await obj4.groups.set_switch("group://x", "push", True)
    r_al = await _run_one(obj4, "蹲 警报", umo="group://x")
    check("★ 蹲 警报 被拒且说明原因（DE 已停用，给替代）",
          r_al is not None and r_al.raw_text is not None
          and "无法订阅" in r_al.raw_text and "停用" in r_al.raw_text,
          repr(r_al.raw_text if r_al else None))
    check("L-4 第 30 条仍允许（>= 30 拒绝）",
          n == 30 and r31 is not None and r31.raw_text is not None
          and "蹲订阅已达上限" in r31.raw_text,
          f"n={n}, r31.raw={r31.raw_text if r31 else None}")

    # —— 状态卡回归（2026-09-14 修「.状态 platform 未定义」崩溃）——
    from core.store import Subscription as _Sub

    class _CacheStub:
        def stats(self):
            return {"size": 3, "hit_rate": 0.5}

    obj4 = _make_plugin(tmp)
    obj4.client = type("C", (), {"cache": _CacheStub()})()
    await obj4.subs.add(_Sub("group://test", "pc", "裂隙", rule="虚空捕获"))
    rep = await obj4._handle_admin(_StubEvent("group://test"), ".状态")
    check("「.状态」出图形卡不崩（platform 未定义回归）",
          rep is not None and not rep.raw_text
          and rep.title.startswith("Warframe SDJK")
          and any("虚空捕获" in ln for ln in rep.lines),
          f"title={getattr(rep, 'title', None)}")


if __name__ == "__main__":
    asyncio.run(main())
    print()
    if FAILED:
        print(f"共 {len(FAILED)} 项失败：{FAILED}")
        sys.exit(1)
    print("蹲订阅回归测试全部通过 ✔")
