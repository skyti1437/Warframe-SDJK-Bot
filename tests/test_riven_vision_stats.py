# -*- coding: utf-8 -*-
"""紫卡截图（vision）词条归一化回归：卡面全称不能被短名抢走。

2026-09-24 用户报障：发「紫卡分析 + 截图」后分析卡报
「⚠️ 卡面数值与「视使之触」家族的已知倾向都不吻合，武器名可能识别有误」。

根因：LLM 从卡面读到的是**全称**「暴击伤害」，而 `_normalize_llm_stats` 的
包含匹配按 RIVEN_STAT_ZH 的字典顺序先撞上短名「暴击」（crit_chance），
于是暴伤按暴击率的基值算区间（手枪列 149.99 vs 90）——108.2% 落在
151.86%~185.61% 之外，被误判成「武器名识别有误」。

本测试用报障卡面冻死这条链（Ocucor/视使之触，倾向 1.2，3 正 1 负）：
  ① 全称必须整表命中（暴击伤害 → crit_damage，不得落到 crit_chance）
  ② 四条数值在 手枪/倾向 1.2 下必须全部可行（不再误报）
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

    class _Logger:
        def info(self, *a, **k): pass
        def warning(self, *a, **k): pass
        def error(self, *a, **k): pass
        def exception(self, *a, **k): pass
    logger = _Logger()

    class AstrMessageEvent:
        def __init__(self):
            self.unified_msg_origin = "group://riven_vision_test"
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
from core import riven_analysis as RA  # noqa: E402
from core.parser import RIVEN_STAT_ZH  # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = ""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f"  -> {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


_norm = plugin.WarframeSDJK._normalize_llm_stats
_rev = {v: k for k, v in RIVEN_STAT_ZH.items()}

# ------------------------- ① 报障卡面逐条归一化（全称必须整表命中）
_card = {"positive": [["暴击伤害", 108.2], ["多重射击", 141.7],
                      ["毒素伤害", 98.2]],
         "negative": [["切割伤害", 99.9]]}
_pos, _neg = _norm(_card, _rev)
check("暴击伤害 → crit_damage（回归锚点：不得落到 crit_chance）",
      _pos and _pos[0] == ("crit_damage", 108.2), str(_pos))
check("多重射击 → multishot", len(_pos) > 1 and _pos[1] == ("multishot", 141.7),
      str(_pos))
check("毒素伤害 → toxin_damage", len(_pos) > 2 and _pos[2] == ("toxin_damage", 98.2),
      str(_pos))
check("切割伤害（负）→ slash_damage",
      _neg and _neg[0] == ("slash_damage", 99.9), str(_neg))

# ------------------------- ② 端到端：四条在 手枪/倾向1.2 下必须全部可行
check("报障卡面 4/4 可行（Ocucor 倾向 1.2，不再误报「都不吻合」）",
      RA.disp_feasible(_pos, _neg, "pistol", 1.2) is True)
check("武器类别归一：pistol/secondary → pistol",
      RA.weapon_class("pistol", "secondary") == "pistol")

# ------------------------- ③ 全称与缩写的常见写法都要通
for name, want in [("暴击伤害", "crit_damage"), ("爆击伤害", "crit_damage"),
                   ("暴伤", "crit_damage"), ("暴击几率", "crit_chance"),
                   ("暴击率", "crit_chance"), ("暴击", "crit_chance"),
                   ("滑行暴击", "slide_crit"), ("攻击速度", "attack_speed"),
                   ("触发几率", "status_chance"), ("火焰伤害", "heat_damage"),
                   ("电击伤害", "electric_damage"), ("冰冻伤害", "cold_damage"),
                   ("对Grineer伤害", "damage_vs_grineer"),
                   ("处决伤害", "finisher_damage"), ("重击效率",
                                                "heavy_attack_efficiency")]:
    p, _n = _norm({"positive": [[name, 50.0]]}, _rev)
    check(f"归一化「{name}」→ {want}", bool(p) and p[0][0] == want, str(p))

# ------------------------- ④ 反向守卫：短名「暴击」仍须是暴击几率（未被误改）
check("短名「暴击」= crit_chance（与暴伤区分）",
      _norm({"positive": [["暴击", 50.0]]}, _rev)[0][0][0] == "crit_chance")

if FAILED:
    print(f"\n失败 {len(FAILED)} 项：{FAILED}")
    sys.exit(1)
print("\n全部通过 ✔")
