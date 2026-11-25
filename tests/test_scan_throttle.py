# -*- coding: utf-8 -*-
"""识卡限流回归（python3 tests/test_scan_throttle.py）

背景（2026-09-18 安全审查）：识卡每次都会调用**用户自配的多模态模型**，
多数渠道按量计费。原来只有「同会话串行」——防不住同一个人狂刷（账单）
和多群并发（2 核机器上 CPU 被打满，实测单张渲染 0.7s → 39s）。
现在有两道闸，都可在面板调整：
  · scan_cooldown：同一发送者的最小间隔（0=关）
  · scan_max_concurrent：全局同时识别数上限（0=关）
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

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(
        f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" -> {detail}" if detail and not cond else "")
    )
    if not cond:
        FAILED.append(name)


class _Ev:
    def __init__(self, umo="group://scan", sender="user_1"):
        self.unified_msg_origin = umo
        self.role = "member"
        self._sender = sender

    def get_sender_id(self) -> str:
        return self._sender

    def get_sender_name(self) -> str:
        return "tester"


class _Parsed:
    content_str = ""
    preset = ""
    page = 1


def _plugin(cfg: dict):
    obj = plugin.WarframeSDJK.__new__(plugin.WarframeSDJK)
    obj.cfg = dict(cfg)
    obj._ocr_busy = set()
    obj._ocr_last = {}
    # 让流程走到限流之后：假装消息里有图（真正下载会返回空，无所谓）
    obj._event_has_image = lambda e: True
    return obj


async def main() -> None:
    # ① 冷却：同一人第二次被拦
    obj = _plugin({"scan_cooldown": 60})
    from unittest.mock import AsyncMock
    from types import SimpleNamespace
    import base64
    import io
    from PIL import Image as PILImage

    class Image:
        def __init__(self, ref):
            self.file = ref

    def png(size):
        buffer = io.BytesIO()
        PILImage.new("1", size).save(buffer, format="PNG")
        return "base64://" + base64.b64encode(buffer.getvalue()).decode()

    def picture(ref):
        ev = _Ev(sender="u1")
        ev.message_obj = SimpleNamespace(message=[Image(ref)])
        return ev

    obj._extract_loadout_validated = AsyncMock(return_value={"weapon": "SOMA", "mods": []})
    for ref, hint in (
        ("base64://a?", "格式"),
        (png((320, 3000)), "像素"),
        ("file:///no/such/image.png", "下载失败"),
    ):
        rejected = await obj._h_scan(_Parsed(), picture(ref), "pc")
        check(
            "输入拒绝说明可操作的原因 " + hint,
            hint in rejected.raw_text and "u1" not in obj._ocr_last,
        )
    obj.MAX_SCAN_IMAGE_BYTES = 1
    rejected = await obj._h_scan(_Parsed(), picture(png((20, 10))), "pc")
    check("体积超限返还冷却", "体积" in rejected.raw_text and "u1" not in obj._ocr_last)
    obj._extract_loadout_validated.assert_not_called()
    del obj.MAX_SCAN_IMAGE_BYTES
    r1 = await obj._h_scan(_Parsed(), picture(png((20, 10))), "pc")
    obj._extract_loadout_validated.assert_awaited_once()
    check(
        "首次识卡放行（未触发冷却）",
        r1 is not None and "太频繁" not in (r1.raw_text or ""),
        repr(getattr(r1, "raw_text", None)),
    )
    r2 = await obj._h_scan(_Parsed(), _Ev(sender="u1"), "pc")
    check(
        "★ 同一人 60 秒内第二次识卡被冷却拦截",
        r2 is not None and "太频繁" in (r2.raw_text or ""),
        repr(getattr(r2, "raw_text", None)),
    )

    # ② 冷却只针对同一个人，不误伤别人
    r3 = await obj._h_scan(_Parsed(), _Ev(sender="u2"), "pc")
    check(
        "冷却不误伤其他用户",
        r3 is not None and "太频繁" not in (r3.raw_text or ""),
        repr(getattr(r3, "raw_text", None)),
    )

    # ③ scan_cooldown=0 → 关闭冷却
    obj0 = _plugin({"scan_cooldown": 0})
    a = await obj0._h_scan(_Parsed(), _Ev(sender="u1"), "pc")
    b = await obj0._h_scan(_Parsed(), _Ev(sender="u1"), "pc")
    check(
        "scan_cooldown=0 时冷却关闭（两次都放行）",
        "太频繁" not in (a.raw_text or "") and "太频繁" not in (b.raw_text or ""),
        "",
    )

    # ④ 全局并发上限
    obj2 = _plugin({"scan_cooldown": 0, "scan_max_concurrent": 2})
    obj2._ocr_busy = {"group://a", "group://b"}  # 假装已有 2 个在跑
    waiting = asyncio.create_task(obj2._h_scan(_Parsed(), _Ev(umo="group://c"), "pc"))
    await asyncio.sleep(0)
    check(
        "★ 全局并发到上限时新请求等待",
        not waiting.done() and obj2._scan_requests().is_waiting("group://c"),
    )
    obj2._ocr_busy.discard("group://b")
    obj2._scan_requests().wake()
    r5 = await asyncio.wait_for(waiting, 2)
    check(
        "并发未满时自动放行",
        r5 is not None and "图片下载失败" in (r5.raw_text or ""),
        repr(getattr(r5, "raw_text", None)),
    )

    # ⑤ 取不到 sender 时：跳过按人冷却，但并发闸仍生效
    obj3 = _plugin({"scan_cooldown": 60, "scan_max_concurrent": 0})
    ev_nosender = _Ev()
    ev_nosender.get_sender_id = lambda: (_ for _ in ()).throw(RuntimeError("x"))
    x1 = await obj3._h_scan(_Parsed(), ev_nosender, "pc")
    x2 = await obj3._h_scan(_Parsed(), ev_nosender, "pc")
    check(
        "取不到发送者时不按人冷却（不崩、不误杀）",
        "太频繁" not in (x1.raw_text or "") and "太频繁" not in (x2.raw_text or ""),
        "",
    )

    # ⑥ 限流状态是实例级（不是类级共享）
    check(
        "★ 限流状态挂在实例上（类属性不会串号）",
        obj._ocr_last is not obj0._ocr_last and obj._ocr_busy is not obj0._ocr_busy,
        "",
    )

    # ⑦ 冷却记录不会无限增长
    obj4 = _plugin({"scan_cooldown": 1})
    for i in range(600):
        obj4._ocr_last[f"u{i}"] = 1.0  # 全是过期记录
    await obj4._h_scan(_Parsed(), _Ev(sender="fresh"), "pc")
    check(
        "过期冷却记录会被清理（字典不无限增长）",
        len(obj4._ocr_last) < 600,
        str(len(obj4._ocr_last)),
    )

    # ⑧ 紫卡识图与识卡共用防刷闸（2026-10-10 安全审查：紫卡截图同样调用付费模型）
    obj5 = _plugin({"scan_cooldown": 60})
    g1, _ = obj5._vision_gate(_Ev(sender="u9"), "紫卡识图")
    g2, _ = obj5._vision_gate(_Ev(sender="u9"), "紫卡识图")
    check(
        "★ 紫卡识图：同一人冷却内第二次被拦",
        g1 is None and g2 is not None and "紫卡识图太频繁" in (g2.raw_text or ""),
        repr(getattr(g2, "raw_text", None)),
    )
    r6 = await obj5._h_scan(_Parsed(), _Ev(sender="u9"), "pc")
    check(
        "紫卡识图与识卡共用冷却",
        r6 is not None and "太频繁" in (r6.raw_text or ""),
        repr(getattr(r6, "raw_text", None)),
    )
    obj6 = _plugin({"scan_cooldown": 60, "scan_max_concurrent": 1})
    obj6._ocr_busy = {"riven:group://a:u1"}
    g3, _ = obj6._vision_gate(_Ev(sender="u10"), "紫卡识图")
    check(
        "紫卡识图计入全局并发上限",
        g3 is not None and "并发上限" in (g3.raw_text or ""),
        repr(getattr(g3, "raw_text", None)),
    )

    obj6._ocr_busy.clear()
    retry, _ = obj6._vision_gate(_Ev(sender="u10"), "紫卡识图")
    check("并发拒绝后同人可立即重发", retry is None)

    # ⑨ 主动迁移后只拆任务冷却，不扩大机器的总并发预算。
    isolated = _plugin({"vision_routes_isolated": True, "scan_cooldown": 60})
    first_riven, _ = isolated._vision_gate(_Ev(sender="task-user"), "紫卡识图", task="riven")
    first_scan, _ = isolated._vision_gate(_Ev(sender="task-user"), "识卡", task="scan")
    again_riven, _ = isolated._vision_gate(_Ev(sender="task-user"), "紫卡识图", task="riven")
    check(
        "隔离模式紫卡和识卡各自冷却",
        first_riven is None and first_scan is None and again_riven is not None,
        "",
    )
    check(
        "隔离冷却按实例惰性建立，不写旧共享表",
        not isolated._ocr_last
        and isolated._vision_last("scan") is not isolated._vision_last("riven"),
        "",
    )
    isolated.cfg.update(scan_cooldown=0, scan_max_concurrent=1)
    isolated._ocr_busy.add(object())
    budget, _ = isolated._vision_gate(_Ev(sender="other"), "识卡", task="scan")
    check(
        "隔离模式仍受机器共享预算限制",
        budget is not None and "机器共享资源预算" in budget.raw_text,
        "",
    )

    # 使用真正的线程和入口时钟：超时可以先回复，名额必须留到像素工作退出。
    import threading
    from unittest.mock import patch

    blocked = _plugin({"scan_cooldown": 60, "scan_max_concurrent": 1})
    blocked.RACE_WINDOW_S = 0.1
    loop = asyncio.get_running_loop()
    entered, release = asyncio.Event(), threading.Event()

    def slow_pips(image):
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(3)
        return []

    ev = picture(png((20, 10)))
    with patch("core.pips.detect_pips", slow_pips):
        first = asyncio.create_task(blocked._h_scan(_Parsed(), ev, "pc"))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            reply = await asyncio.wait_for(first, 1)
            assert "识卡超时" in reply.raw_text and "u1" not in blocked._ocr_last
            assert blocked._ocr_busy == blocked._scan_running == {ev.unified_msg_origin}
            gate, _ = blocked._vision_gate(_Ev(sender="other"), "紫卡", task="riven")
            assert "并发上限" in gate.raw_text
            blocked.RACE_WINDOW_S = 2
            retry = asyncio.create_task(blocked._h_scan(_Parsed(), _Ev(umo="other"), "pc"))
            await asyncio.sleep(0)
            assert blocked._scan_requests().is_waiting("other")
        finally:
            release.set()
        assert "下载失败" in (await asyncio.wait_for(retry, 1)).raw_text
        assert not blocked._ocr_busy and not blocked._scan_running

    print()
    if FAILED:
        print(f"✗ {len(FAILED)} 项失败：" + "、".join(FAILED))
        sys.exit(1)
    print("✓ 全部通过")


if __name__ == "__main__":
    asyncio.run(main())
