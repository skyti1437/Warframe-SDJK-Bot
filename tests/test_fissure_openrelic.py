# -*- coding: utf-8 -*-
"""裂隙 / 开核桃的派系与「速刷」标记回归测试（python3 tests/test_fissure_openrelic.py）

覆盖三件在卡面上看得见、且本轮修过的事：

1. **派系必须显示**：``de_worldstate`` 的 fissure 带 ``enemy``（Grineer / Corpus /
   Infested / Sentient / Orokin / The Murmur），显示层要用它 —— 国际服官方简中
   不翻译派系名（Grineer / Corpus / Infested / Sentient 保留英文），只有
   奥罗金 / 低语者 官方有中文；Tenno / Crossfire / Duviri 不是敌对派系，不显示。
2. **「速刷」标记在行尾**：旧实现把「速刷」放在行首，紧贴纪元芯片看着像压字。
3. **移动防御不算速刷**：``_h_openrelic`` 的 QUICK 集合不得包含 移动防御 /
   防御 / 生存 / 拦截（用户明确反馈「移动防御算不上速刷」）。
"""
from __future__ import annotations

import asyncio
import json
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
        EventMessageType = _EventMessageType
        event_message_type = staticmethod(lambda spec: (lambda fn: fn))

    event_mod.AstrMessageEvent = AstrMessageEvent
    event_mod.MessageChain = MessageChain
    event_mod.EventMessageType = _EventMessageType
    event_mod.event_message_type = lambda spec: (lambda fn: fn)
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
        def __init__(self, *a, **k): pass

    def register(*a, **k):
        def deco(cls):
            return cls
        return deco

    star_mod.Context = Context
    star_mod.Star = Star
    star_mod.register = register

    api.AstrBotConfig = AstrBotConfig
    api.logger = _Logger()
    sys.modules.setdefault("astrbot", pkg)
    sys.modules["astrbot.api"] = api
    sys.modules["astrbot.api.event"] = event_mod
    sys.modules["astrbot.api.message_components"] = mc_mod
    sys.modules["astrbot.api.star"] = star_mod


_install_astrbot_stub()

import main as plugin  # noqa: E402
from core import de_worldstate as dw  # noqa: E402
from core import formatters as fmt  # noqa: E402
from core.parser import parse  # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"[{'PASS' if cond else 'FAIL'}] {name}"
          + (f"  -> {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


# ---------------------------------------------------------------------------
# 1) 派系映射
# ---------------------------------------------------------------------------
check("Grineer 保留英文", fmt._fissure_faction({"enemy": "Grineer"}) == "Grineer")
check("Corpus 保留英文", fmt._fissure_faction({"enemy": "Corpus"}) == "Corpus")
check("Infested 保留英文", fmt._fissure_faction({"enemy": "Infested"}) == "Infested")
check("Sentient 保留英文", fmt._fissure_faction({"enemy": "Sentient"}) == "Sentient")
check("Orokin 用官方中文", fmt._fissure_faction({"enemy": "Orokin"}) == "奥罗金")
check("The Murmur 用官方中文",
      fmt._fissure_faction({"enemy": "The Murmur"}) == "低语者")
check("Tenno 不显示", fmt._fissure_faction({"enemy": "Tenno"}) == "")
check("Crossfire 不显示", fmt._fissure_faction({"enemy": "Crossfire"}) == "")
check("空值安全", fmt._fissure_faction({}) == "" and fmt._fissure_faction({"enemy": ""}) == "")

# ---------------------------------------------------------------------------
# 2) fmt_fissures 每行都带派系
# ---------------------------------------------------------------------------
_FS = [
    {"node": "Pacific（地球）", "nodeKey": "SolNode26", "missionType": "捕获",
     "tier": "Lith", "tierNum": 1, "isHard": False, "isStorm": False,
     "enemy": "Grineer", "expiry": "2030-01-01T00:00:00+00:00"},
    {"node": "Ananke（木星）", "nodeKey": "SolNode115", "missionType": "捕获",
     "tier": "Meso", "tierNum": 2, "isHard": False, "isStorm": False,
     "enemy": "Corpus", "expiry": "2030-01-01T00:00:00+00:00"},
    {"node": "死灵塔（虚空）", "nodeKey": "SolNode309", "missionType": "生存",
     "tier": "Neo", "tierNum": 3, "isHard": True, "isStorm": False,
     "enemy": "Orokin", "expiry": "2030-01-01T00:00:00+00:00"},
]
_title, flines = fmt.fmt_fissures(_FS)
_body = [ln for ln in flines if ln[:1].isdigit()]
check("裂隙三行都在", len(_body) == 3, str(flines))
check("裂隙带 Grineer", any("Grineer" in ln for ln in _body), str(_body[:1]))
check("裂隙带 Corpus", any("Corpus" in ln for ln in _body), str(_body[1:2]))
check("裂隙 Orokin 显示为奥罗金", any("奥罗金" in ln for ln in _body), str(_body[2:]))
check("裂隙派系在任务类型之后",
      all(ln.find("· ") < ln.find("Grineer") for ln in _body if "Grineer" in ln))


# ---------------------------------------------------------------------------
# 3) 开核桃：派系 / 速刷位置 / QUICK 集合
# ---------------------------------------------------------------------------
class _FakeClient:
    async def fissures(self, platform):  # noqa: ANN001
        return list(_FS)


class _StubEvent:
    def __init__(self, umo: str = "group://test"):
        self.unified_msg_origin = umo
    def get_sender_name(self) -> str:
        return "tester"


async def _openrelic_lines(text: str) -> tuple[str, list[str]]:
    obj = plugin.WarframeSDJK.__new__(plugin.WarframeSDJK)
    obj.client = _FakeClient()
    obj.cfg = {}
    obj._dir = Path(tempfile.mkdtemp())
    obj.page_size = 12
    reply = await obj._h_openrelic(parse(text), _StubEvent(), "pc")
    return reply.title, reply.lines


async def _main() -> None:
    _t, o_lines = await _openrelic_lines("开核桃")
    # 正文行 = 带「可掉落 N 种 · 剩…」的行（图例行也含「可掉落」，用「剩」排除）
    _body = [ln for ln in o_lines if "可掉落" in ln and "剩" in ln]
    check("开核桃有三行", len(_body) == 3, str(o_lines))
    check("开核桃带派系 Grineer", any("Grineer" in ln for ln in _body), str(_body[:1]))
    check("开核桃带派系 Corpus", any("Corpus" in ln for ln in _body), str(_body[1:2]))
    check("开核桃 Orokin→奥罗金", any("奥罗金" in ln for ln in _body), str(_body[2:]))
    # 「速刷」只在快节奏任务上（捕获 ✓ / 生存 ✗），且必须落在行尾
    _quick = [ln for ln in _body if "速刷" in ln]
    check("速刷只标快节奏任务（2/3 条）", len(_quick) == 2, str(_body))
    check("生存不算速刷",
          not any("生存" in ln and "速刷" in ln for ln in _body), str(_body))
    check("速刷在行尾而非行首",
          all(not ln.lstrip().startswith("速刷") for ln in _body)
          and all(ln.find("可掉落") < ln.find("速刷") for ln in _quick),
          str(_quick))
    # 行内不得出现「· ·」双点（任务类型为空时的拼接瑕疵）
    check("开核桃无 · ·  双点", not any("· ·" in ln for ln in o_lines), str(o_lines))

    # 移动防御 / 站桩类不参与速刷
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    _q = src[src.find('QUICK = {'):src.find('}', src.find('QUICK = {')) + 1]
    for _bad in ("移动防御", "防御", "生存", "拦截", "挖掘", "劫持"):
        check(f"QUICK 不含 {_bad}", _bad not in _q, _q)
    for _good in ("捕获", "歼灭", "破坏"):
        check(f"QUICK 含 {_good}", _good in _q, _q)


asyncio.run(_main())

if FAILED:
    print(f"\n失败 {len(FAILED)} 项：{FAILED}")
    sys.exit(1)
print("\n全部通过 ✔")
