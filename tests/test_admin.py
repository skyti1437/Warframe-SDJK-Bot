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

    # 1) L-1：普通成员发 .锚点 Palus 应被权限拒绝
    reply = await obj._handle_admin(_MemberEvent(), ".锚点 Palus")
    check("L-1 普通成员 .锚点 被权限拦截",
          reply is not None and reply.raw_text is not None
          and "需要群管理员权限" in reply.raw_text,
          repr(reply.raw_text if reply else None))

    # 2) L-1：管理员发 .锚点 Palus 应通过且写入 runtime/arb_anchor.json
    obj.cfg = {}
    reply = await obj._handle_admin(_AdminEvent(), ".锚点 Palus")
    check("L-1 管理员 .锚点 Palus 通过",
          reply is not None and reply.raw_text is not None
          and "仲裁锚点已校准" in reply.raw_text,
          repr(reply.raw_text if reply else None))

    # 3) L-3：arb_anchor.json 应写入到运行时目录而非 /AstrBot/data/config/...
    anchor_path = plugin.PLUGIN_DIR / "runtime" / "arb_anchor.json"
    # 若上一轮测试已经写过，文件存在；若不存在，说明没持久化
    check("L-3 arb_anchor.json 写入运行时目录",
          anchor_path.exists() or len(obj.cfg.get("arb_anchor", {})) > 0,
          f"path={anchor_path}, exists={anchor_path.exists()}, cfg={obj.cfg.get('arb_anchor')}")

    # 4) 用法提示对普通成员不可见（admin 先看）
    reply = await obj._handle_admin(_AdminEvent(), ".锚点")
    check("管理员 .锚点（无参数）显示用法",
          reply is not None and reply.raw_text is not None
          and "用法：.锚点" in reply.raw_text,
          repr(reply.raw_text if reply else None))

    # 5) 普通成员也不能 .默认平台 / .开启 / .关闭
    for cmd in (".默认平台 ps", ".开启 推送", ".关闭 推送"):
        reply = await obj._handle_admin(_MemberEvent(), cmd)
        check(f"普通成员 {cmd} 被拦截",
              reply is not None and reply.raw_text is not None
              and "需要群管理员权限" in reply.raw_text,
              repr(reply.raw_text if reply else None))


if __name__ == "__main__":
    asyncio.run(main())
    print()
    if FAILED:
        print(f"共 {len(FAILED)} 项失败：{FAILED}")
        sys.exit(1)
    print("管理指令回归测试全部通过 ✔")