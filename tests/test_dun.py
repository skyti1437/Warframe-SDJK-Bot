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


def _txt(r) -> str:
    """Reply 的可读文本：raw_text 或 title + lines（报错卡片迁移后断言用）。"""
    if r is None:
        return ""
    return r.raw_text or (r.title + "\n" + "\n".join(r.lines))


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
        event_message_type = staticmethod(lambda spec: lambda fn: fn)

    event_mod.AstrMessageEvent = AstrMessageEvent
    event_mod.MessageChain = MessageChain
    event_mod.EventMessageType = _EventMessageType
    event_mod.event_message_type = lambda spec: lambda fn: fn  # 装饰器工厂占位
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
import core  # noqa: E402
from core.parser import parse  # noqa: E402
from core.store import GroupStore, SubscriptionStore  # noqa: E402


def _make_plugin(tmp: Path) -> "plugin.WarframeSDJK":
    """绕过 __init__ 构造实例，仅挂载 _h_dun 需要的存储。"""
    obj = plugin.WarframeSDJK.__new__(plugin.WarframeSDJK)
    obj.subs = SubscriptionStore(tmp / "subs.json")
    obj.groups = GroupStore(tmp / "groups.json", default_platform="pc")
    obj.push = None
    return obj


async def _run_one(obj, text: str, umo: str = "group://test", role: str = "admin"):
    parsed = parse(text)
    event = (
        plugin.AstrMessageEvent(umo=umo, sender="tester")
        if hasattr(plugin, "AstrMessageEvent")
        else None
    )
    # 用桩事件：直接构造，避免依赖 stub 名
    event = _StubEvent(umo, role=role)
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
    print(
        f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" -> {detail}" if detail and not cond else "")
    )
    if not cond:
        FAILED.append(name)


async def main() -> None:
    tmp = Path(tempfile.mkdtemp())
    obj = _make_plugin(tmp)
    # 订阅类用例需要先开启本群推送（测试 5/7 单独验证“未开启”拒绝逻辑）
    await obj.groups.set_switch("group://test", "push", True)

    # 1) 正常订阅「蹲 裂隙」——此前会因 NameError 直接失效
    reply = await _run_one(obj, "蹲 裂隙")
    check(
        "「蹲 裂隙」成功返回订阅卡片",
        reply is not None and "订阅成功" in (reply.title or ""),
        repr(reply.title if reply else None),
    )
    check(
        "「蹲 裂隙」写入了订阅",
        len(obj.subs.all()) == 1,
        str([s.to_dict() for s in obj.subs.all()]),
    )
    if obj.subs.all():
        check("订阅事件类型为 裂隙", obj.subs.all()[0].event == "裂隙")

    # 2) 带筛选+时长+窗口
    reply = await _run_one(obj, "蹲 裂隙 钢铁虚空生存 永久 22到8")
    check(
        "「蹲 裂隙 钢铁... 永久 22到8」成功",
        reply is not None and "订阅成功" in (reply.title or ""),
        repr(reply.title if reply else None),
    )
    check("带筛选的订阅总数=2", len(obj.subs.all()) == 2)

    # 2b) 档位词自动判裂隙（2026-10-03 口径：T1–T6 本身就含裂隙语义）
    reply = await _run_one(obj, "蹲 钢铁t5歼灭")
    check(
        "「蹲 钢铁t5歼灭」漏写类型词也成功（档位词自动判裂隙）",
        reply is not None and "订阅成功" in (reply.title or ""),
        repr(reply.title if reply else None),
    )
    check(
        "自动判定的订阅 event=裂隙、rule 原样落库",
        len(obj.subs.all()) == 3
        and obj.subs.all()[-1].event == "裂隙"
        and obj.subs.all()[-1].rule == "钢铁t5歼灭",
        str([(s.event, s.rule) for s in obj.subs.all()]),
    )
    check(
        "回执 describe 含「安魂（T5）」编号",
        any("安魂（T5）" in ln for ln in (reply.lines or [])),
        str(reply.lines if reply else None),
    )
    reply = await _run_one(obj, "蹲 钢铁")
    check(
        "「蹲 钢铁」仍不静默降级（地点/修饰词不推断），且给正确写法提示",
        reply is not None
        and "未识别为可蹲类型" in _txt(reply)
        and "像是裂隙筛选词" in _txt(reply)
        and "怎么改" in _txt(reply),
        repr(_txt(reply)),
    )

    # 3) 帮助（2026-09-21 起卡片化：实现侧有意移除祖传 text_only=True，与其它
    #    指令输出一致，渲染失败由 _build_results 自动降级文字——测试跟进对齐）
    reply = await _run_one(obj, "蹲 帮助")
    check(
        "「蹲 帮助」返回类型卡片（2026-09-21 改卡片化）",
        reply is not None and not reply.text_only and "可蹲类型" in (reply.title or ""),
        repr(reply.title if reply else None),
    )

    # 4) 未识别类型（不静默降级）
    reply = await _run_one(obj, "蹲 火星")
    check(
        "「蹲 火星」报错而非静默降级",
        reply is not None and "未识别为可蹲类型" in _txt(reply),
        repr(_txt(reply)),
    )

    # 5) 推送未开启时拒绝
    obj2 = _make_plugin(Path(tempfile.mkdtemp()))
    # 确保 push=False（默认）
    reply = await _run_one(obj2, "蹲 突击")
    check(
        "推送未开启时拒绝订阅",
        reply is not None and "推送功能未开启" in _txt(reply) and ".开启 推送" in _txt(reply),
        repr(_txt(reply)),
    )
    check("未开启时不应写入订阅", len(obj2.subs.all()) == 0)

    # 6) 开启后正常订阅
    await obj2.groups.set_switch("group://test", "push", True)
    reply = await _run_one(obj2, "蹲 突击")
    check(
        "开启推送后「蹲 突击」成功",
        reply is not None and "订阅成功" in (reply.title or ""),
        repr(reply.title if reply else None),
    )

    # 7) 取消全部
    reply = await _run_one(obj2, "蹲 取消")
    check(
        "「蹲 取消」清空订阅",
        reply is not None and "已取消" in _txt(reply) and len(obj2.subs.all()) == 0,
        repr(_txt(reply)) + f" | left={len(obj2.subs.all())}",
    )

    # 8) 「蹲 类型 取消」
    await obj2.groups.set_switch("group://test", "push", True)
    await _run_one(obj2, "蹲 夜灵")
    await _run_one(obj2, "蹲 奸商")
    reply = await _run_one(obj2, "蹲 夜灵 取消")
    check(
        "「蹲 夜灵 取消」只取消夜灵",
        len(obj2.subs.all()) == 1 and obj2.subs.all()[0].event == "奸商",
        str([s.event for s in obj2.subs.all()]),
    )

    # 9) A1（2026-10-03 用户批准的特例，解除 L-2 自发脱敏）：仅「蹲」订阅
    #    记录发起人 id（用于命中时 @）；默认空、绝不硬编码；公开包此位恒空。
    # ★ 2026-09-19：警报在 DE 数据里恒为空（系统停用）已被标为不可订阅，
    #   本组断言改用「新闻」（可订阅、无筛选）。
    reply = await _run_one(obj2, "蹲 新闻")
    last = obj2.subs.all()[-1]
    check(
        "A1 created_by 记录发起人 id（特例：仅蹲订阅；stub 返回 tester_id）",
        last.created_by == "tester_id",
        repr(last.created_by),
    )

    # 10) L-4: 单 umo 蹲订阅上限（默认 30）
    obj3 = _make_plugin(Path(tempfile.mkdtemp()))
    await obj3.groups.set_switch("group://limit", "push", True)
    # 先塞 30 条不同 umo 的订阅绕开限额 = 把 obj3 的 groups 切换过来前先填到 29
    for i in range(29):
        await _run_one(obj3, "蹲 新闻", umo="group://limit")
    # 第 30 条允许通过
    await _run_one(obj3, "蹲 新闻", umo="group://limit")  # 第 30 条（消费订阅位）
    r31 = await _run_one(obj3, "蹲 新闻", umo="group://limit")
    n = len(obj3.subs.for_umo("group://limit"))
    # ★ C3（2026-10-03）：常规警报停用、但**活动型警报**（Tag=LotusGift）会
    #   下发（线上实测 3 条）⇒ 已接线，订阅应成功（旧断言「被拒」随之作废）。
    obj4 = _make_plugin(Path(tempfile.mkdtemp()))
    await obj4.groups.set_switch("group://x", "push", True)
    r_al = await _run_one(obj4, "蹲 警报", umo="group://x")
    check(
        "★ 蹲 警报 可订阅（C3：活动型警报已接线）",
        r_al is not None and "订阅成功" in (r_al.title or ""),
        repr(getattr(r_al, "title", None) or _txt(r_al)),
    )
    check(
        "L-4 第 30 条仍允许（>= 30 拒绝）",
        n == 30 and r31 is not None and "蹲订阅已达上限" in _txt(r31),
        f"n={n}, r31={_txt(r31)!r}",
    )

    # —— 状态卡回归（2026-09-14 修「.状态 platform 未定义」崩溃）——
    from core.store import Subscription as _Sub

    # 11) 取消权限（2026-10-10 安全审查）：普通成员只能删自己发起的订阅，
    #     管理员可删全群；created_by 为空的旧订阅只有管理员能删
    obj5 = _make_plugin(Path(tempfile.mkdtemp()))
    for ev, who in (("新闻", "tester_id"), ("奸商", "other_id"), ("夜灵", "")):
        await obj5.subs.add(_Sub("group://perm", "pc", ev, created_by=who))
    r_m = await _run_one(obj5, "蹲 取消", umo="group://perm", role="member")
    check(
        "★ 普通成员「蹲 取消」只删自己发起的",
        sorted(s.event for s in obj5.subs.all()) == ["夜灵", "奸商"]
        and "你在本群发起的" in _txt(r_m),
        f"{[s.event for s in obj5.subs.all()]} | {_txt(r_m)!r}",
    )
    r_m2 = await _run_one(obj5, "蹲 奸商 取消", umo="group://perm", role="member")
    check(
        "★ 普通成员不能按筛选词删他人订阅",
        len(obj5.subs.all()) == 2 and "没有" in _txt(r_m2),
        f"{[s.event for s in obj5.subs.all()]} | {_txt(r_m2)!r}",
    )
    await _run_one(obj5, "蹲 取消", umo="group://perm", role="admin")
    check("管理员「蹲 取消」可清空全群（含旧订阅）", len(obj5.subs.all()) == 0)

    class _CacheStub:
        def stats(self):
            return {"size": 3, "hit_rate": 0.5}

    obj4 = _make_plugin(tmp)
    obj4.client = type("C", (), {"cache": _CacheStub()})()
    await obj4.subs.add(_Sub("group://test", "pc", "裂隙", rule="虚空捕获"))
    rep = await obj4._handle_admin(_StubEvent("group://test"), ".状态")
    # 品牌名从 core.__brand__ 派生（不写死）：2026-09-19 改名时踩过 —— 写死品牌名
    # 的断言在换品牌时会连带崩，而「Warframe SDJK」还会被「Warframe SDJKBOT」前缀命中
    # 而假装通过。主次版本同样从 core.__version__ 派生（v1.1.0 抬版时这里写死 "1.0"
    # 连带红过一次，同病同治）。
    _ver2 = ".".join(str(core.__version__).split(".")[:2])
    check(
        "「.状态」出图形卡不崩（platform 未定义回归）",
        rep is not None
        and not rep.raw_text
        and rep.title.startswith(f"{plugin.BRAND} {_ver2}")
        and any("虚空捕获" in ln for ln in rep.lines),
        f"title={getattr(rep, 'title', None)}",
    )

    # —— 2026-10-11 时间段口径（北京时间；0到24=全天；22到8 的 8 点整结束；跨午夜按起始日）——
    obj6 = _make_plugin(tmp)
    await obj6.groups.set_switch("group://tw", "push", True)
    r_all = await _run_one(obj6, "蹲 夜灵 永久 0到24", umo="group://tw")
    sub_all = obj6.subs.all()[-1]
    check(
        "★ 蹲 夜灵 永久 0到24：回执当全天、不存窗口",
        "时间　全天" in _txt(r_all) and sub_all.windows == {},
        f"{_txt(r_all)!r} {sub_all.windows}",
    )
    r_night = await _run_one(obj6, "蹲 夜灵 永久 22到8", umo="group://tw")
    check(
        "★ 蹲 夜灵 永久 22到8：回执写明 22:00-次日8:00",
        "22:00-次日8:00" in _txt(r_night),
        _txt(r_night),
    )
    r_fri = await _run_one(obj6, "蹲 夜灵 永久 周5 22到8", umo="group://tw")
    sub_fri = obj6.subs.all()[-1]
    check(
        "蹲 … 周5 22到8：解析为周五起始的跨午夜窗口",
        sub_fri.windows.get("days") == [5]
        and sub_fri.windows.get("start") == 22
        and sub_fri.windows.get("end") == 8,
        f"{sub_fri.windows} {_txt(r_fri)!r}",
    )
    r_h = await _run_one(obj6, "蹲 夜灵 永久 每天19点", umo="group://tw")
    check(
        "护栏：每天19点 回执仍是整点小时口径",
        "每天19点" in _txt(r_h) and "19:59" in _txt(r_h),
        _txt(r_h),
    )

    # ★ 2026-10-16：带档位语义的连写别名（钢铁裂隙 / 裂缝 系）不得把筛选词吃掉。
    #   修前「蹲 钢铁裂隙」落库 rule=""（=订阅全部裂隙），九重天裂隙 / 钢铁裂缝 直接不认识。
    for _word, _rule in (
        ("钢铁裂隙", "钢铁"),
        ("虚空裂隙", "普通"),
        ("九重天裂隙", "九重天"),
        ("钢铁裂缝", "钢铁"),
        ("虚空裂缝", "普通"),
        ("九重天裂缝", "九重天"),
        ("裂缝", ""),
    ):
        _tmp7 = Path(tempfile.mkdtemp())
        obj7 = _make_plugin(_tmp7)
        await obj7.groups.set_switch("group://pre", "push", True)
        r7 = await _run_one(obj7, f"蹲 {_word}", umo="group://pre")
        _subs7 = obj7.subs.all()
        check(
            f"蹲 {_word} ⇒ 裂隙订阅、筛选「{_rule or '（全部）'}」",
            len(_subs7) == 1 and _subs7[0].event == "裂隙" and _subs7[0].rule == _rule,
            f"{[(x.event, x.rule) for x in _subs7]} {_txt(r7)!r}",
        )
    # 预设筛选词与用户自写筛选并存（不覆盖用户写的）
    _tmp8 = Path(tempfile.mkdtemp())
    obj8 = _make_plugin(_tmp8)
    await obj8.groups.set_switch("group://pre2", "push", True)
    await _run_one(obj8, "蹲 钢铁裂隙 生存", umo="group://pre2")
    _s8 = obj8.subs.all()
    check(
        "蹲 钢铁裂隙 生存 ⇒ 筛选同时含 钢铁 与 生存",
        len(_s8) == 1 and "钢铁" in _s8[0].rule and "生存" in _s8[0].rule,
        str([(x.event, x.rule) for x in _s8]),
    )

    # ★ 2026-10-16（ZCode 线上实例）：「蹲 钢铁裂隙 古纪」推来了安魂（T5）钢铁 ——
    #   旧实现用逗号拼 rule（逗号 = 或），连写「钢铁裂隙古纪」则残余「裂隙」当节点名 ⇒ 永不命中。
    #   六种写法都必须等价于「钢铁 且 古纪」；显式逗号仍是「或」。
    from core.parser import parse_fissure_filter as _pff

    _lith_steel = {"tier": "Lith", "isHard": True, "node": "某节点（土星）", "missionType": "生存"}
    _req_steel = {"tier": "Requiem", "isHard": True, "node": "Taveuni（赤毒要塞）"}
    _lith_norm = {"tier": "Lith", "isHard": False, "node": "某节点（火星）"}
    for _w in (
        "钢铁裂隙 古纪",
        "钢铁裂隙古纪",
        "裂隙 钢铁 古纪",
        "裂隙 古纪 钢铁",
        "裂隙 钢铁古纪",
        "钢铁古纪 裂隙",
        "钢铁裂缝 古纪",
    ):
        _tmp9 = Path(tempfile.mkdtemp())
        obj9 = _make_plugin(_tmp9)
        await obj9.groups.set_switch("group://and", "push", True)
        r9 = await _run_one(obj9, f"蹲 {_w}", umo="group://and")
        _s9 = obj9.subs.all()
        _f9 = _pff(_s9[0].rule) if _s9 else None
        check(
            f"蹲 {_w} ⇒ 钢铁且古纪：古纪钢铁命中、安魂钢铁 / 古纪普通都不命中",
            len(_s9) == 1
            and _s9[0].event == "裂隙"
            and _f9.match(_lith_steel)
            and not _f9.match(_req_steel)
            and not _f9.match(_lith_norm),
            f"{[(x.event, x.rule) for x in _s9]} {_txt(r9)!r}",
        )
        check(
            f"蹲 {_w} 确认卡：筛选描述为「钢铁古纪（T1）」且无节点名提示",
            "钢铁古纪（T1）" in _txt(r9) and "按节点" not in _txt(r9),
            _txt(r9),
        )
    # 显式逗号仍是或，且确认卡写明「或」
    _tmp10 = Path(tempfile.mkdtemp())
    obj10 = _make_plugin(_tmp10)
    await obj10.groups.set_switch("group://or", "push", True)
    r10 = await _run_one(obj10, "蹲 裂隙 钢铁,古纪", umo="group://or")
    _f10 = _pff(obj10.subs.all()[0].rule)
    check(
        "蹲 裂隙 钢铁,古纪 ⇒ 或：安魂钢铁与古纪普通都命中，确认卡写「或」",
        _f10.match(_req_steel) and _f10.match(_lith_norm) and "钢铁 或 古纪（T1）" in _txt(r10),
        _txt(r10),
    )
    # 识别不了的残余（按节点名匹配）当场提示
    _tmp11 = Path(tempfile.mkdtemp())
    obj11 = _make_plugin(_tmp11)
    await obj11.groups.set_switch("group://sub", "push", True)
    r11 = await _run_one(obj11, "蹲 裂隙 钢铁 赛德娜", umo="group://sub")
    check(
        "节点名残余：确认卡提示「按节点 / 星球名匹配」",
        "按节点 / 星球名匹配" in _txt(r11) and "赛德娜" in _txt(r11),
        _txt(r11),
    )


if __name__ == "__main__":
    asyncio.run(main())
    print()
    if FAILED:
        print(f"共 {len(FAILED)} 项失败：{FAILED}")
        sys.exit(1)
    print("蹲订阅回归测试全部通过 ✔")
