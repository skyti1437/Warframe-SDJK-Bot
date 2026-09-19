# -*- coding: utf-8 -*-
"""指令空间避让（与内置指令 / 其他插件抢答）回归测试。

背景（2026-09-19 线上事故）：
  AstrBot 的 WakingCheckStage 会把 wake_prefix（默认 "/"）从 message_str 上
  **剥掉**，所以到 handler 这一层 "/新闻" 已经变成 "新闻"。原实现用
  `text.startswith("/")` 挡前缀是**死代码**，导致本插件的 143 个触发词全都能
  被 "/触发词" 命中；而本插件 handler 注册得早、处理完又 stop_event()，
  把排在后面的处理器整体跳过。线上实证：
    /help  → 被本插件吞掉（AstrBot 内置帮助打不开）
    /新闻  → 被本插件吞掉（dailyhub 的资讯指令打不开）

本测试覆盖修复后的行为契约：
  * 带前缀 + 该词被别人占用 → 让路（无输出、**不** stop_event）
  * 带前缀 + 未被占用（/仲裁）  → 照常响应
  * 裸词（无前缀）              → 全部照常响应，不受避让影响
  * 前缀探测的判据与容错（拿不到原始文本时 fail-open，不误杀）
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
        def info(self, *a, **k): pass
        def warning(self, *a, **k): pass
        def error(self, *a, **k): pass
        def exception(self, *a, **k): pass

    logger = _Logger()

    class AstrMessageEvent:
        def __init__(self, umo: str = "group://conflict_test"):
            self.unified_msg_origin = umo

        def get_sender_id(self) -> str:
            return "stub_id"

        def get_sender_name(self) -> str:
            return "stub_user"

    class MessageChain:
        def message(self, text):  # noqa: ANN001
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

    class Image: pass
    class Plain: pass
    mc_mod.Image = Image
    mc_mod.Plain = Plain

    class Context: pass

    class Star:
        def __init__(self, *a, **k): pass

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
from core.store import GroupStore  # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"[{'PASS' if cond else 'FAIL'}] {name}"
          + (f" -> {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


class _SegText(plugin.Plain):
    """消息段桩（Plain 文本段）。"""

    def __init__(self, text: str):
        self.text = text


class _Ev:
    """最小事件桩。

    message_str  —— AstrBot 处理后的文本（wake_prefix 已被剥掉）
    raw          —— message_obj.message_str，**原始**文本（没被改写）；
                    传 None 表示该适配器不填它
    segs         —— 消息段（用于 webchat 这类不填 message_obj.message_str 的退路）
    """

    def __init__(self, message_str: str, raw: str | None = None, segs=None):
        self.unified_msg_origin = "group://conflict_test"
        self.message_str = message_str
        self.message_obj = types.SimpleNamespace(
            message_str=message_str if raw is None else raw)
        self._segs = list(segs) if segs is not None else None
        self.stopped = False
        self.sent: list[str] = []

    def get_messages(self):
        if self._segs is None:
            raise AttributeError("no messages")
        return self._segs

    def plain_result(self, text: str):
        self.sent.append(text)
        return ("plain", text)

    def stop_event(self) -> None:
        self.stopped = True

    def get_sender_id(self) -> str:
        return "stub_id"

    def get_sender_name(self) -> str:
        return "stub_user"


def _make_plugin(tmp: Path, occupied: frozenset | None = None):
    obj = plugin.WarframeSDJK.__new__(plugin.WarframeSDJK)
    obj.cfg = {}
    obj.groups = GroupStore(tmp / "groups.json", default_platform="pc")
    obj.render_mode = "text"
    obj._render_lock = None
    obj.called: list[str] = []

    async def _rec(parsed, event, platform):
        obj.called.append(parsed.command)
        return plugin.Reply(raw_text=f"OK:{parsed.command}")

    obj._routes = {"news": _rec, "help": _rec, "arbitration": _rec}
    obj._test_occupied = occupied
    return obj


async def _drive(obj, event) -> list:
    out = []
    async for item in plugin.WarframeSDJK.on_message(obj, event):
        out.append(item)
    return out


class _FakeFilter:
    """假 CommandFilter：只保留本插件读的两个字段。"""

    def __init__(self, command_name: str, alias=None):
        self.command_name = command_name
        self.alias = set(alias or ())


class _FakeHandler:
    def __init__(self, module_path: str, filters):
        self.handler_module_path = module_path
        self.event_filters = filters


def _install_fake_registry(handlers: list) -> "callable":
    """把假的 star_handlers_registry 塞进 sys.modules，返回还原函数。"""
    names = [
        "astrbot.core", "astrbot.core.star", "astrbot.core.star.filter",
        "astrbot.core.star.star_handler", "astrbot.core.star.filter.command",
    ]
    saved = {n: sys.modules.get(n) for n in names}

    core = types.ModuleType("astrbot.core")
    star = types.ModuleType("astrbot.core.star")
    filt = types.ModuleType("astrbot.core.star.filter")
    handler_mod = types.ModuleType("astrbot.core.star.star_handler")
    filter_mod = types.ModuleType("astrbot.core.star.filter.command")

    handler_mod.star_handlers_registry = handlers
    filter_mod.CommandFilter = _FakeFilter
    core.star = star
    star.filter = filt
    filt.command = filter_mod

    sys.modules.update({
        "astrbot.core": core,
        "astrbot.core.star": star,
        "astrbot.core.star.filter": filt,
        "astrbot.core.star.star_handler": handler_mod,
        "astrbot.core.star.filter.command": filter_mod,
    })

    def _restore() -> None:
        for name, mod in saved.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod

    return _restore


async def main() -> None:
    tmp = Path(tempfile.mkdtemp())
    # 占用集合显式给定：模拟「内置 help + dailyhub 新闻」已被别人注册
    obj = _make_plugin(tmp, occupied=frozenset({"help", "新闻", "news", "reset"}))
    real_occupied = plugin._occupied_command_words
    plugin._occupied_command_words = lambda: obj._test_occupied

    try:
        # ---------- 1) 带前缀且被别人占用 → 让路 ----------
        for raw_msg, stripped in (("/help", "help"), ("/新闻", "新闻"),
                                  ("/news", "news")):
            ev = _Ev(stripped, raw=raw_msg)
            out = await _drive(obj, ev)
            check(f"「{raw_msg}」让路：无任何输出", out == [], repr(out))
            check(f"「{raw_msg}」让路：handler 未被调用",
                  obj.called == [], str(obj.called))
            # ★ 关键：让路时**不能** stop_event，否则后面的处理器同样收不到，
            #   等于从「我们抢」变成「谁都收不到」，比不修还糟。
            check(f"★ 「{raw_msg}」让路时不中断事件（让别家接住）",
                  ev.stopped is False, f"stopped={ev.stopped}")

        # ---------- 2) 带前缀但没被占用 → 照常响应 ----------
        obj.called.clear()
        ev = _Ev("仲裁", raw="/仲裁")
        out = await _drive(obj, ev)
        check("「/仲裁」未被占用 → 仍由本插件响应",
              obj.called == ["arbitration"] and ev.sent == ["OK:arbitration"],
              f"called={obj.called} sent={ev.sent}")
        check("「/仲裁」响应后正常中断事件（维持原有语义）",
              ev.stopped is True, f"stopped={ev.stopped}")

        # 带修饰符的形式：首词判定必须只看第一个 token
        obj.called.clear()
        ev = _Ev("仲裁 -w", raw="/仲裁 -w")
        await _drive(obj, ev)
        check("「/仲裁 -w」同样能响应（首词判定不受参数影响）",
              obj.called == ["arbitration"], str(obj.called))

        # ---------- 3) 裸词（无前缀）→ 不受避让影响 ----------
        for word, cmd in (("help", "help"), ("新闻", "news"), ("仲裁", "arbitration")):
            obj.called.clear()
            ev = _Ev(word, raw=word)
            out = await _drive(obj, ev)
            check(f"裸词「{word}」照常响应（不因被占用而静默）",
                  obj.called == [cmd] and bool(out),
                  f"called={obj.called} out={out}")

        # @机器人 的形态：原始文本与处理后一致（无前缀）
        obj.called.clear()
        ev = _Ev("新闻", raw=" 新闻")
        await _drive(obj, ev)
        check("@机器人「新闻」不被误判为带前缀 → 照常响应",
              obj.called == ["news"], str(obj.called))

        # ---------- 4) 前缀探测的判据与容错 ----------
        check("判据：raw 比 message_str 多一截且以其结尾 → 判定带前缀",
              plugin._wake_prefix_stripped(_Ev("新闻", raw="/新闻")) is True)
        check("判据：两者相同 → 不判定带前缀",
              plugin._wake_prefix_stripped(_Ev("新闻", raw="新闻")) is False)
        check("判据：首尾空白不影响（waking_check 会 strip）",
              plugin._wake_prefix_stripped(_Ev("新闻", raw=" /新闻 ")) is True)
        check("容错：没有 message_obj → 按无前缀处理（fail-open，不误杀）",
              plugin._wake_prefix_stripped(types.SimpleNamespace(
                  message_str="新闻")) is False)
        check("容错：message_obj.message_str 非字符串 → 按无前缀处理",
              plugin._wake_prefix_stripped(types.SimpleNamespace(
                  message_str="新闻",
                  message_obj=types.SimpleNamespace(message_str=None))) is False)
        # 反向：raw 更短 / 不以 cur 结尾（理论上不会出现）→ 不判定
        check("判据：raw 不比处理后长 → 不判定带前缀",
              plugin._wake_prefix_stripped(_Ev("新闻 -w", raw="新闻")) is False)

        # ---------- 4b) 适配器不填 message_obj.message_str 时的消息段退路 ----------
        check("退路：靠消息段里的 Plain 文本也能认出前缀",
              plugin._wake_prefix_stripped(
                  _Ev("新闻", raw=None, segs=[_SegText("/新闻")])) is True)
        check("退路：@机器人 的 At 段不算文本，不误判",
              plugin._wake_prefix_stripped(
                  _Ev("新闻", raw=None, segs=[_SegText(" 新闻")])) is False)
        check("退路：没有消息段（get_messages 抛错）也不炸",
              plugin._wake_prefix_stripped(_Ev("新闻", raw=None)) is False)
        # 两条来源取「或」：任一能证明带前缀就让路
        check("两来源取或：message_obj 为空但消息段带前缀 → 判定带前缀",
              plugin._wake_prefix_stripped(
                  _Ev("仲裁", raw="", segs=[_SegText("/仲裁")])) is True)
    finally:
        plugin._occupied_command_words = real_occupied
        plugin._OCCUPIED_CACHE = None

    # ---------- 5) 占用集合的真源与兜底 ----------
    occupied = plugin._occupied_command_words()
    check("★ 拿不到 AstrBot 注册表时回退兜底集合（含内置 help）",
          "help" in occupied, str(sorted(occupied)))
    check("★ 兜底集合把已知第三方占用（新闻）一并算上，避免再次吞掉",
          "新闻" in occupied, str(sorted(occupied)))
    check("★ 本插件自己的指令（仲裁/赏金）不在占用集合里",
          "仲裁" not in occupied and "赏金" not in occupied, str(sorted(occupied)))
    # 缓存语义：注册表规模不变时返回同一对象
    check("占用集合有缓存（不每消息重扫注册表）",
          plugin._occupied_command_words() is occupied)
    plugin._OCCUPIED_CACHE = None
    plugin._OCCUPIED_EPOCH = -1

    # ---------- 6) 真源路径：从注册表里读出别人的指令（含 alias） ----------
    restore = _install_fake_registry([
        _FakeHandler("data.plugins.astrbot_plugin_dailyhub.main",
                     [_FakeFilter("新闻", {"60s", "news", "每日新闻"}),
                      _FakeFilter("订阅状态")]),
        _FakeHandler("astrbot.builtin_stars.builtin_commands.main",
                     [_FakeFilter("help")]),
        # 自己注册的必须被跳过（否则 /仲裁 会被自己让掉）
        _FakeHandler("data.plugins.astrbot_plugin_warframe.main",
                     [_FakeFilter("仲裁"), _FakeFilter("赏金")]),
        # 指令组是「父 子」两级，占用词应只取首词
        _FakeHandler("data.plugins.astrbot_plugin_get_px.main",
                     [_FakeFilter("签到 排行")]),
        # 非 CommandFilter 的 filter 必须被忽略
        _FakeHandler("data.plugins.astrbot_plugin_parser.main", [object()]),
    ])
    try:
        words = plugin._occupied_command_words()
        check("真源：读到第三方指令名「新闻」", "新闻" in words, str(sorted(words)))
        check("真源：读到第三方 alias「60s」/「news」",
              "60s" in words and "news" in words, str(sorted(words)))
        check("真源：读到内置 help", "help" in words, str(sorted(words)))
        check("真源：指令组只取首词（签到，而不是「签到 排行」）",
              "签到" in words and "签到 排行" not in words, str(sorted(words)))
        check("★ 真源：跳过本插件自己的指令（仲裁/赏金 不在集合里）",
              "仲裁" not in words and "赏金" not in words, str(sorted(words)))
        check("真源：非 CommandFilter 的 filter 不炸、也不贡献词",
              isinstance(words, frozenset), type(words).__name__)

        # ★ 缓存必须能感知「运行期新装插件」（今天的 dailyhub 就是新装的）
        before = plugin._occupied_command_words()
        registry = sys.modules["astrbot.core.star.star_handler"].star_handlers_registry
        registry.append(_FakeHandler(
            "data.plugins.astrbot_plugin_newone.main", [_FakeFilter("新指令")]))
        after = plugin._occupied_command_words()
        check("★ 注册表规模变化 → 缓存自动失效并重扫（新装插件的指令立即让路）",
              "新指令" in after and before is not after, str(sorted(after))[:80])

        # 让路链路：新装插件的指令带前缀时也确实被让掉
        obj2 = _make_plugin(tmp, occupied=after)
        plugin._occupied_command_words = lambda: obj2._test_occupied
        ev = _Ev("新指令", raw="/新指令")
        out = await _drive(obj2, ev)
        check("新装插件的「/新指令」立即让路（无需重启）",
              out == [] and ev.stopped is False, f"out={out} stopped={ev.stopped}")
    finally:
        restore()
        plugin._occupied_command_words = real_occupied
        plugin._OCCUPIED_CACHE = None
        plugin._OCCUPIED_EPOCH = -1

    print()
    if FAILED:
        print(f"FAILED ({len(FAILED)}): " + "; ".join(FAILED))
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    asyncio.run(main())
