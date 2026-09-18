# -*- coding: utf-8 -*-
"""配置健壮性回归测试（BUG-2）：python3 tests/test_config_robust.py

背景：面板 schema 只在 UI 层约束类型，用户手改配置 JSON 可绕过。
修复前 main.py 对数值配置直接 float()/int()，填 "abc" 会让插件
__init__ 抛 ValueError 加载失败。修复后应回退默认值并记 warning。
"""
from __future__ import annotations

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

    import logging
    logger = logging.getLogger("astrbot_plugin_warframe")

    class AstrMessageEvent:
        pass

    class MessageChain(list):
        pass

    class _Filter:
        EventMessageType = type("EMT", (), {"ALL": 1})
        event_message_type = staticmethod(lambda spec: (lambda fn: fn))

    event_mod.AstrMessageEvent = AstrMessageEvent
    event_mod.MessageChain = MessageChain
    event_mod.filter = _Filter()

    class Image: pass
    class Plain: pass
    mc_mod.Image = Image
    mc_mod.Plain = Plain

    class Context: pass

    class Star:
        def __init__(self, *a, **k): pass

    star_mod.Context = Context
    star_mod.Star = Star
    star_mod.register = lambda *a, **k: (lambda cls: cls)

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
    print(f"[{'PASS' if cond else 'FAIL'}] {name}"
          + (f" -> {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


def try_init(cfg):
    """用真实 __init__ 构造插件；返回 (实例, 异常)。"""
    try:
        return plugin.WarframeSDJK(object(), cfg), None
    except Exception as e:  # noqa: BLE001
        return None, e


# ---------------------------------------------------------------- BUG-2：非法数值不崩
BAD_CFG = {
    "http_timeout": "abc",
    "page_size": "12页",
    "push_interval": "abc",
    "scan_cooldown": "xyz",
    "scan_max_concurrent": [],
}
obj, e = try_init(BAD_CFG)
check("全部数值配置非法时 init 不抛异常", e is None, repr(e))
if obj is not None:
    check("http_timeout 非法 → 回退默认 15", obj.client._http.timeout.read == 15,
          str(obj.client._http.timeout))
    check("page_size 非法 → 回退默认 12", obj.page_size == 12, str(obj.page_size))
    check("push_interval 非法 → 回退默认 45",
          obj.push.interval == 45, str(obj.push.interval))

# ---------------------------------------------------------------- 正常值不受影响
obj, e = try_init({"http_timeout": 30, "page_size": 20, "push_interval": 60})
check("合法配置 init 不抛", e is None, repr(e))
if obj is not None:
    check("http_timeout=30 生效", obj.client._http.timeout.read == 30)
    check("page_size=20 生效", obj.page_size == 20)
    check("push_interval=60 生效", obj.push.interval == 60)

# ---------------------------------------------------------------- config=None 兜底
obj, e = try_init(None)
check("config=None init 不抛", e is None, repr(e))

# ---------------------------------------------------------------- 数字字符串兼容
obj, e = try_init({"http_timeout": "20", "page_size": "8"})
check("数字字符串 init 不抛", e is None, repr(e))
if obj is not None:
    check("'20' 解析为 20", obj.client._http.timeout.read == 20,
          str(obj.client._http.timeout))
    check("'8' 解析为 8", obj.page_size == 8, str(obj.page_size))

# ---------------------------------------------------------------- _num 单元行为
if hasattr(plugin, "_num"):
    n = plugin._num
    check("_num 合法值", n({"k": 5}, "k", 15, int) == 5)
    check("_num 非法值回退", n({"k": "abc"}, "k", 15) == 15)
    check("_num None 回退", n({"k": None}, "k", 15) == 15)
    check("_num 空串回退", n({"k": ""}, "k", 15) == 15)
    check("_num 缺 key 用默认", n({}, "k", 15) == 15)
    check("_num 0 是合法值（不落入 or 0 陷阱）", n({"k": 0}, "k", 15) == 0)
    check("_num 浮点转 int", n({"k": 3.9}, "k", 3, int) == 3)
else:
    check("_num 辅助函数存在", False, "main 中未找到 _num")

print()
if FAILED:
    print(f"共 {len(FAILED)} 项失败：{FAILED}")
    sys.exit(1)
print("配置健壮性：全部断言通过")
