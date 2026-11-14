# -*- coding: utf-8 -*-
"""裂隙 / 开核桃的派系与「速刷」标记回归测试（python3 tests/test_fissure_openrelic.py）

覆盖三件在卡面上看得见、且本轮修过的事：

1. **派系必须显示**：``de_worldstate`` 的 fissure 带 ``enemy``（Grineer / Corpus /
   Infested / Sentient / Orokin / The Murmur），显示层要用它 —— 国际服官方简中
   不翻译派系名（Grineer / Corpus / Infested / Sentient 保留英文），只有
   奥罗金 / 低语者 官方有中文；Tenno / Crossfire / Duviri 不是敌对派系，不显示。
2. **「速刷」标记在行尾**：旧实现把「速刷」放在行首，紧贴纪元芯片看着像压字。
3. **移动防御不算速刷**：``_h_openrelic`` 的 QUICK 集合不得包含 移动防御 /
   防御 / 生存 / 拦截（维护者明确反馈「移动防御算不上速刷」）。
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
    api.logger = _Logger()
    sys.modules.setdefault("astrbot", pkg)
    sys.modules["astrbot.api"] = api
    sys.modules["astrbot.api.event"] = event_mod
    sys.modules["astrbot.api.message_components"] = mc_mod
    sys.modules["astrbot.api.star"] = star_mod


_install_astrbot_stub()

import main as plugin  # noqa: E402
from core import formatters as fmt  # noqa: E402
from core.parser import parse  # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(
        f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -> {detail}" if detail and not cond else "")
    )
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
check("The Murmur 用官方中文", fmt._fissure_faction({"enemy": "The Murmur"}) == "低语者")
check("Tenno 不显示", fmt._fissure_faction({"enemy": "Tenno"}) == "")
check("Crossfire 不显示", fmt._fissure_faction({"enemy": "Crossfire"}) == "")
check("空值安全", fmt._fissure_faction({}) == "" and fmt._fissure_faction({"enemy": ""}) == "")

# ---------------------------------------------------------------------------
# 2) fmt_fissures 每行都带派系
# ---------------------------------------------------------------------------
_FS = [
    {
        "node": "Pacific（地球）",
        "nodeKey": "SolNode26",
        "missionType": "捕获",
        "tier": "Lith",
        "tierNum": 1,
        "isHard": False,
        "isStorm": False,
        "enemy": "Grineer",
        "expiry": "2030-01-01T00:00:00+00:00",
    },
    {
        "node": "Ananke（木星）",
        "nodeKey": "SolNode115",
        "missionType": "捕获",
        "tier": "Meso",
        "tierNum": 2,
        "isHard": False,
        "isStorm": False,
        "enemy": "Corpus",
        "expiry": "2030-01-01T00:00:00+00:00",
    },
    {
        "node": "死灵塔（虚空）",
        "nodeKey": "SolNode309",
        "missionType": "生存",
        "tier": "Neo",
        "tierNum": 3,
        "isHard": True,
        "isStorm": False,
        "enemy": "Orokin",
        "expiry": "2030-01-01T00:00:00+00:00",
    },
]
_title, flines = fmt.fmt_fissures(_FS)
# ★ 2026-10-11 第二步改版：裂隙行统一为「· [纪元]　类型　节点　派系　标记　剩…」列表行（不再有序号）
_body = [ln for ln in flines if ln.startswith("· ")]
check("裂隙三行都在", len(_body) == 3, str(flines))
check("裂隙带 Grineer", any("Grineer" in ln for ln in _body), str(_body[:1]))
check("裂隙带 Corpus", any("Corpus" in ln for ln in _body), str(_body[1:2]))
check("裂隙 Orokin 显示为奥罗金", any("奥罗金" in ln for ln in _body), str(_body[2:]))
check(
    "裂隙派系在任务类型之后（列序 纪元 / 类型 / 节点 / 派系）",
    all(
        ln[2:].split("　")[3] in ("Grineer", "Corpus", "奥罗金")
        and ln[2:].split("　")[1] in set(fmt.MISSION_CN.values())
        for ln in _body
    ),
    str(_body),
)


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
    # 正文行 = 「· 」列表行（★ 2026-10-11 改版：「可掉落 N 种」是纪元级数字，已汇总进卡首
    #   「◆ 各纪元可掉落遗物」答案行，不再逐行重复）
    _body = [ln for ln in o_lines if ln.startswith("· ")]
    check("开核桃有三行", len(_body) == 3, str(o_lines))
    check("开核桃带派系 Grineer", any("Grineer" in ln for ln in _body), str(_body[:1]))
    check("开核桃带派系 Corpus", any("Corpus" in ln for ln in _body), str(_body[1:2]))
    check("开核桃 Orokin→奥罗金", any("奥罗金" in ln for ln in _body), str(_body[2:]))
    # 「速刷」只在快节奏任务上（捕获 ✓ / 生存 ✗），且必须落在行尾
    _quick = [ln for ln in _body if "速刷" in ln]
    check("速刷只标快节奏任务（2/3 条）", len(_quick) == 2, str(_body))
    check("生存不算速刷", not any("生存" in ln and "速刷" in ln for ln in _body), str(_body))
    check(
        "速刷在标记列（派系之后）而非行首",
        all(not ln.lstrip("· ").startswith("速刷") for ln in _body)
        and all(
            ln.find("Grineer") < ln.find("速刷") or ln.find("Corpus") < ln.find("速刷")
            for ln in _quick
        ),
        str(_quick),
    )
    check(
        "卡首汇总各纪元可掉落遗物数",
        any(ln.startswith("◆ 各纪元可掉落遗物：") and "种" in ln for ln in o_lines),
        str(o_lines[:2]),
    )
    # 行内不得出现「· ·」双点（任务类型为空时的拼接瑕疵）
    check("开核桃无 · ·  双点", not any("· ·" in ln for ln in o_lines), str(o_lines))

    # ★ 2026-09-27 用户口径：**任务类型前移到节点之前**（与裂隙卡同一列序）
    #   [纪元]　任务类型　节点　派系　标记　剩X（改版后按列分格）
    #   判据：纪元芯片后**第一格必须是任务类型**（若列序回退成「节点 任务」，
    #   节点名首词不会落在任务类型词表里 ⇒ 必红）。
    _MT = set(fmt.MISSION_CN.values())
    _bad_seq = [ln for ln in _body if ln[2:].split("　")[1] not in _MT]
    check(
        "★ 开核桃：任务类型前移（[纪元] 后第一个词是任务类型）",
        bool(_body) and not _bad_seq,
        str(_bad_seq or _body),
    )

    # ★ 2026-11-09 维护者：开遗物按无尽优先（同纪元 无尽 → 速刷 → 其它）
    _base = dict(_FS[0])
    _mix = [
        {**_base, "node": "Q（地球）", "missionType": "捕获"},
        {**_base, "node": "M（地球）", "missionType": "移动防御"},
        {**_base, "node": "E（地球）", "missionType": "生存"},
    ]
    _saved = list(_FS)
    _FS[:] = _mix
    try:
        _t2, _l2 = await _openrelic_lines("开核桃")
        _order = [ln[2:].split("　")[2] for ln in _l2 if ln.startswith("· ")]
        check(
            "同纪元：无尽 → 速刷 → 其它",
            _order == ["E（地球）", "Q（地球）", "M（地球）"],
            str(_order),
        )
        check("无尽带「无尽」标记", any("生存" in ln and "无尽" in ln for ln in _l2), str(_l2))
        check("卡首统计无尽场次", any("无尽 1 场" in ln for ln in _l2), str(_l2[:3]))
        _FS[:] = _mix + [
            {**_base, "node": "S（地球比邻星域）", "missionType": "生存", "isStorm": True}
        ]
        _t4, _l4 = await _openrelic_lines("开核桃")
        check(
            "九重天生存同样标无尽（wiki：Void Storm Survival 逐波结算、可开 2 把以上）",
            any("S（地球比邻星域）" in ln and "无尽" in ln for ln in _l4),
            str(_l4),
        )
        check("注脚说明九重天可在导航台接下一场", any("导航台" in ln for ln in _l4), str(_l4[-2:]))
        # 九重天速刷只认地球 / 土星前哨战；Corpus 星域（要登舰）不标速刷
        _FS[:] = [
            {
                **_base,
                "node": "奥加尔星团（地球比邻星域）",
                "missionType": "Skirmish",
                "isStorm": True,
            },
            {**_base, "node": "欧文—哈克（金星比邻星域）", "missionType": "Spy", "isStorm": True},
            {
                **_base,
                "node": "H-2 星云（面纱比邻星域）",
                "missionType": "Skirmish",
                "isStorm": True,
            },
        ]
        _t5, _l5 = await _openrelic_lines("开核桃")
        _r5 = {ln[2:].split("　")[2]: ln for ln in _l5 if ln.startswith("· ")}
        check(
            "地球前哨战九重天标速刷", "速刷" in _r5.get("奥加尔星团（地球比邻星域）", ""), str(_r5)
        )
        check(
            "Corpus 星域九重天间谍不标速刷",
            "速刷" not in _r5.get("欧文—哈克（金星比邻星域）", "x速刷"),
            str(_r5),
        )
        check(
            "面纱前哨战不标速刷（只认地球 / 土星）",
            "速刷" not in _r5.get("H-2 星云（面纱比邻星域）", "x速刷"),
            str(_r5),
        )
        check(
            "注脚写明首选地球 / 土星前哨战",
            any("地球 / 土星前哨战" in ln for ln in _l5),
            str(_l5[-2:]),
        )
        # 九重天单独一节：地面裂隙在前；九重天内 地球前哨战 → 面纱（格里尼尔）→ Corpus（含生存）
        _FS[:] = [
            {
                **_base,
                "node": "W（金星比邻星域）",
                "missionType": "Survival",
                "isStorm": True,
                "enemy": "Corpus",
            },
            {**_base, "node": "Q（地球）", "missionType": "捕获"},
            {
                **_base,
                "node": "H-2 星云（面纱比邻星域）",
                "missionType": "Skirmish",
                "isStorm": True,
                "enemy": "Grineer",
            },
            {
                **_base,
                "node": "奥加尔星团（地球比邻星域）",
                "missionType": "Skirmish",
                "isStorm": True,
                "enemy": "Grineer",
            },
        ]
        _t6, _l6 = await _openrelic_lines("开核桃")
        _o6 = [ln[2:].split("　")[2] for ln in _l6 if ln.startswith("· ")]
        check(
            "开核桃：九重天单独成节、按九重天规则排",
            _o6
            == [
                "Q（地球）",
                "奥加尔星团（地球比邻星域）",
                "H-2 星云（面纱比邻星域）",
                "W（金星比邻星域）",
            ],
            str(_o6),
        )
        check(
            "开核桃：有「九重天裂隙（航道星舰）」小节",
            any(ln.startswith("◆ 九重天裂隙（航道星舰）") for ln in _l6),
            str(_l6),
        )
        check("开核桃：卡首统计九重天场次", any("九重天 3 场" in ln for ln in _l6), str(_l6[:3]))
        _FS[:] = _mix
        _t3, _l3 = await _openrelic_lines("开核桃 无尽")
        _rows3 = [ln for ln in _l3 if ln.startswith("· ")]
        check("「开核桃 无尽」只留无尽", len(_rows3) == 1 and "生存" in _rows3[0], str(_rows3))
    finally:
        _FS[:] = _saved

    # 移动防御 / 站桩类不参与速刷（2026-09-29 D5：_h_openrelic 迁 relic.py）
    src = (ROOT / "core" / "commands" / "relic.py").read_text(encoding="utf-8")
    _q = src[src.find("QUICK = {") : src.find("}", src.find("QUICK = {")) + 1]
    for _bad in ("移动防御", "防御", "生存", "拦截", "挖掘", "劫持"):
        check(f"QUICK 不含 {_bad}", _bad not in _q, _q)
    for _good in ("捕获", "歼灭", "破坏"):
        check(f"QUICK 含 {_good}", _good in _q, _q)


asyncio.run(_main())

if FAILED:
    print(f"\n失败 {len(FAILED)} 项：{FAILED}")
    sys.exit(1)
print("\n全部通过 ✔")
