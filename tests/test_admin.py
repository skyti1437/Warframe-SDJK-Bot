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
        def info(self, *a, **k): pass
        def warning(self, *a, **k): pass
        def error(self, *a, **k): pass
        def exception(self, *a, **k): pass
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
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" -> {detail}" if detail and not cond else ""))
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
        check(f"{who} .锚点 得到「已废弃」说明",
              "已废弃" in txt, repr(txt))
        check(f"{who} .锚点 的提示里给出真实替代指令（仲裁 / 仲裁表）",
              "「仲裁」" in txt and "仲裁表" in txt, repr(txt))

    # 2) ★ 安全断言：`.锚点` 不再写任何全局状态
    #    （以前会写 cfg["arb_anchor"] 与 runtime/arb_anchor.json）
    #    2026-09-19：运行期数据目录已迁到 data/plugin_data/<插件名>，两个位置都查。
    legacy_path = plugin.PLUGIN_DIR / "runtime" / "arb_anchor.json"
    data_path = Path(plugin._resolve_data_dir()) / "arb_anchor.json"
    before = (legacy_path.stat().st_mtime if legacy_path.exists() else None,
              data_path.stat().st_mtime if data_path.exists() else None)
    await obj._handle_admin(_AdminEvent(), ".锚点 Palus")
    after = (legacy_path.stat().st_mtime if legacy_path.exists() else None,
             data_path.stat().st_mtime if data_path.exists() else None)
    check("★ .锚点 不再写入 arb_anchor.json（跨租户写入已消除）",
          before == after, f"mtime {before} → {after}")
    check("★ .锚点 不再写 cfg['arb_anchor']",
          "arb_anchor" not in obj.cfg, str(obj.cfg.get("arb_anchor")))

    # 3) `.状态` 会列出本群订阅明细 → 必须要求管理权限（2026-09-18 新增）
    reply = await obj._handle_admin(_MemberEvent(), ".状态")
    check("普通成员 .状态 被权限拦截（含订阅明细）",
          reply is not None and reply.raw_text is not None
          and "需要群管理员权限" in reply.raw_text,
          repr(reply.raw_text if reply else None))

    # 4) 管理员仍可用 `.状态`
    obj2 = _make_plugin(tmp)
    obj2.subs = plugin.SubscriptionStore(tmp / "subs.json")
    obj2.client = type("C", (), {"cache": type("K", (), {
        "stats": staticmethod(lambda: {"size": 0, "hit_rate": 0.0})})()})()
    reply = await obj2._handle_admin(_AdminEvent(), ".状态")
    check("管理员 .状态 正常返回",
          reply is not None and reply.raw_text is None, repr(reply))


    # 5) 普通成员也不能 .默认平台 / .开启 / .关闭
    for cmd in (".默认平台 ps", ".开启 推送", ".关闭 推送"):
        reply = await obj._handle_admin(_MemberEvent(), cmd)
        check(f"普通成员 {cmd} 被拦截",
              reply is not None and reply.raw_text is not None
              and "需要群管理员权限" in reply.raw_text,
              repr(reply.raw_text if reply else None))

    # 6) 主指令「状态」直接委托 .状态（_h_status_cmd）→ 同一道闸门，普通成员
    #    也拿不到；帮助卡里这两处都标了「需群管理员」（tests/test_help_coverage.py
    #    第 4 条锁卡面标注）。2026-09-24 自查补：此前只有 .状态 的断言。
    reply = await obj._h_status_cmd(None, _MemberEvent(), "pc")
    check("普通成员「状态」也被权限拦截（与 .状态 同源）",
          reply is not None and reply.raw_text is not None
          and "需要群管理员权限" in reply.raw_text,
          repr(reply.raw_text if reply else None))
    reply = await obj2._h_status_cmd(None, _AdminEvent(), "pc")
    check("管理员「状态」正常返回卡片",
          reply is not None and reply.raw_text is None, repr(reply))


if __name__ == "__main__":
    asyncio.run(main())
    print()
    if FAILED:
        print(f"共 {len(FAILED)} 项失败：{FAILED}")
        sys.exit(1)
    print("管理指令回归测试全部通过 ✔")
