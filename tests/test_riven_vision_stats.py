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
_card = {
    "positive": [["暴击伤害", 108.2], ["多重射击", 141.7], ["毒素伤害", 98.2]],
    "negative": [["切割伤害", 99.9]],
}
_pos, _neg = _norm(_card, _rev)
check(
    "暴击伤害 → crit_damage（回归锚点：不得落到 crit_chance）",
    _pos and _pos[0] == ("crit_damage", 108.2),
    str(_pos),
)
check("多重射击 → multishot", len(_pos) > 1 and _pos[1] == ("multishot", 141.7), str(_pos))
check("毒素伤害 → toxin_damage", len(_pos) > 2 and _pos[2] == ("toxin_damage", 98.2), str(_pos))
check("切割伤害（负）→ slash_damage", _neg and _neg[0] == ("slash_damage", 99.9), str(_neg))

# ------------------------- ② 端到端：四条在 手枪/倾向1.2 下必须全部可行
check(
    "报障卡面 4/4 可行（Ocucor 倾向 1.2，不再误报「都不吻合」）",
    RA.disp_feasible(_pos, _neg, "pistol", 1.2) is True,
)
check("武器类别归一：pistol/secondary → pistol", RA.weapon_class("pistol", "secondary") == "pistol")

# ------------------------- ③ 全称与缩写的常见写法都要通
for name, want in [
    ("暴击伤害", "crit_damage"),
    ("爆击伤害", "crit_damage"),
    ("暴伤", "crit_damage"),
    ("暴击几率", "crit_chance"),
    ("暴击率", "crit_chance"),
    ("暴击", "crit_chance"),
    ("滑行暴击", "slide_crit"),
    ("攻击速度", "attack_speed"),
    ("触发几率", "status_chance"),
    ("火焰伤害", "heat_damage"),
    ("电击伤害", "electric_damage"),
    ("冰冻伤害", "cold_damage"),
    ("对Grineer伤害", "damage_vs_grineer"),
    ("处决伤害", "finisher_damage"),
    ("重击效率", "heavy_attack_efficiency"),
]:
    p, _n = _norm({"positive": [[name, 50.0]]}, _rev)
    check(f"归一化「{name}」→ {want}", bool(p) and p[0][0] == want, str(p))

# ------------------------- ④ 反向守卫：短名「暴击」仍须是暴击几率（未被误改）
check(
    "短名「暴击」= crit_chance（与暴伤区分）",
    _norm({"positive": [["暴击", 50.0]]}, _rev)[0][0][0] == "crit_chance",
)


# ---------------------------------------------------------------------------
# ⑤ 卡面「原文行」解析（2026-09-27 报障回归）
# ---------------------------------------------------------------------------
# 报障：卡面 4 行（3 正 1 负），机器人却回「当前解析到 4 正 2 负」。
# 服务器日志（01:11:52）里的原始 vision JSON 是 **7 条**：
#   positive [基伤/毒素伤害/多重/触发/持续] + negative [滑暴/触发时间]
# ⇒ 模型把**负词条那一行**拆成三份（触发 / 持续 / 触发时间），又凭空多一条
#   「滑暴」；计数校验据此把整张卡挡掉（数值还被交叉配错）。
# 现行分工：模型只照抄卡面文字行（JSON 的 "lines"），归条 / 极性 / 计数由
# `RA.parse_riven_lines()` 确定性决定 —— 只认**带极性符号**的行。
def _resolve(nm: str):
    return plugin.WarframeSDJK._stat_id_from_name(nm, _rev)


_log_json = {
    "weapon": "盗贼 Visi-toxican",
    "positive": [
        ["基伤", 120.5],
        ["毒素伤害", 299.9],
        ["多重", 148],
        ["触发", 97.2],
        ["持续", 97.2],
    ],
    "negative": [["滑暴", 97.2], ["触发时间", 97.2]],
}
_p0, _n0 = _norm(_log_json, _rev)
check(
    "回归锚点：只信语义 JSON 确实数成 4 正 2 负（旧故障原样复现）",
    len(_p0) == 4 and len(_n0) == 2,
    f"{_p0} / {_n0}",
)

_card_lines = [
    "盗贼 Visi-toxican",  # 武器名 + 自命名：不是词条行
    "🔒 +120.5% 毒素伤害",  # 行首锁图标：不能因此丢词条
    "+299.9% 伤害",
    "+148% 多重射击",
    "-97.2% 触发时间",
    "点击 ⓘ 查看详情",  # 图例行：不是词条
    "内融值 1,234",
]  # 右下角内融值：不是词条
_lp, _ln, _notes = RA.parse_riven_lines(_card_lines, _resolve)
check(
    "卡面逐行 → 3 正 1 负（正是 2~3 正 / ≤1 负的合法卡面）",
    len(_lp) == 3 and len(_ln) == 1,
    f"{_lp} / {_ln} / 备注 {_notes}",
)
check(
    "逐行：名称与数值取同一行（毒素伤害 120.5、伤害 299.9 —— 不再交叉配错）",
    _lp[0] == ("toxin_damage", 120.5) and _lp[1] == ("melee_damage", 299.9),
    str(_lp),
)
check("逐行：多重 148 保留", _lp[2] == ("multishot", 148.0), str(_lp))
check(
    "逐行：负词条 = 触发时间 97.2（不再被拆成触发/持续）",
    _ln == [("status_duration", 97.2)],
    str(_ln),
)
check(
    "逐行：武器名/图例/内融值行被跳过且留痕（可查日志）",
    sum(1 for n in _notes if n.startswith("无极性符号")) == 3,
    str(_notes),
)

# 行被拆开时的合并（用户口径：数值行 + 极性符号行并成一条）
_lp2, _ln2, _ = RA.parse_riven_lines(["+", "120.5% 毒素伤害"], _resolve)
check("合并：纯极性符号行 + 下一行", _lp2 == [("toxin_damage", 120.5)], str(_lp2))
_lp3, _, _ = RA.parse_riven_lines(["+毒素伤害", "120.5"], _resolve)
check("合并：带极性无数值行 + 下一行纯数值行", _lp3 == [("toxin_damage", 120.5)], str(_lp3))

# 乘数写法的负词条（卡面「x0.55 对 Corpus 的伤害」→ magnitude 45）
_lp4, _ln4, _ = RA.parse_riven_lines(["-0.55x 对Corpus的伤害"], _resolve)
check(
    "乘数行 → damage_vs_corpus 45（负）",
    not _lp4 and _ln4 == [("damage_vs_corpus", 45.0)],
    f"{_lp4} / {_ln4}",
)
_lp5, _ln5, _ = RA.parse_riven_lines(["-0.55 对Corpus的伤害"], _resolve)
check(
    "乘数漏写 x 也按乘数算（对派系基值 45，真 magnitude 不可能 <1）",
    not _lp5 and _ln5 == [("damage_vs_corpus", 45.0)],
    f"{_lp5} / {_ln5}",
)

# 重复行去重 + 同词条两侧都在时以负为准
_lp6, _ln6, _ = RA.parse_riven_lines(["+148% 多重射击", "+148% 多重射击"], _resolve)
check("同一行重复出现只算一条", _lp6 == [("multishot", 148.0)], str(_lp6))
_lp7, _ln7, _ = RA.parse_riven_lines(["+97.2% 触发时间", "-97.2% 触发时间"], _resolve)
check(
    "同词条两侧都在 → 以负为准（卡面每行只出现一次）",
    not _lp7 and _ln7 == [("status_duration", 97.2)],
    f"{_lp7} / {_ln7}",
)

# 拿不到行（模型没给 lines / 给了空表）⇒ 行数为 0，主流程据此退回语义 JSON
_lp8, _ln8, _ = RA.parse_riven_lines([], _resolve)
check("没有 lines 时行解析返回空（主流程据此退回语义 JSON）", not _lp8 and not _ln8)
_lp9, _ln9, _ = RA.parse_riven_lines(["120.5% 毒素伤害"], _resolve)
check("行首没有极性符号 ⇒ 不当词条（用户口径：只认 +/- 开头的行）", not _lp9 and not _ln9)


# ---------------------------------------------------------------------------
# ⑤b 乘数行（对派系伤害）—— 2026-10-01 用户报障：3+1 的卡被读成 2+1
# ---------------------------------------------------------------------------
# 报障卡面（空刃，近战）：+91.1% 暴击伤害 / x1.51 对 Infested 的伤害 /
# +29.6 初始连击 / -115.7% 处决伤害。服务器日志（01:42:44）显示**窄读一路抄全
# 了 4 行**，是我们的解析器把「x1.51 …」那行当「无极性符号」丢掉：
#   紫卡行解析：2 正 1 负（模型语义表 3 正 1 负）；跳过 无极性符号：x1.51 对 Infested 的伤害
# 窄读是词条的**唯一权威** ⇒ 词条数错成 2正1负，系数从 0.9375 变 1.2375，
# 四条数值区间整体偏小，卡面于是报「与家族已知倾向都不吻合」（误导为识别错武器）。
# 卡面这行写的是**净伤害倍率**：x1.51 = +51%（正词条）、x0.55 = −45%（负词条）。
_sa = ["+91.1% 暴击伤害", "x1.51 对 Infested 的伤害", "+29.6 初始连击", "-115.7% 处决伤害"]
_lpa, _lna, _na = RA.parse_riven_lines(_sa, _resolve)
check(
    "★ 乘数行不再被丢掉：报障卡面 → 3 正 1 负",
    len(_lpa) == 3 and len(_lna) == 1,
    f"{_lpa} / {_lna} / 备注 {_na}",
)
check(
    "★ 乘数 >1 是**正**词条：damage_vs_infested 51（不是 151、也不是负词条）",
    ("damage_vs_infested", 51.0) in _lpa,
    str(_lpa),
)
check(
    "乘数行与其余三行各自配对正确",
    ("crit_damage", 91.1) in _lpa
    and ("initial_combo", 29.6) in _lpa
    and _lna == [("finisher_damage", 115.7)],
    f"{_lpa} / {_lna}",
)
check("乘数行不再出现在「无极性符号」备注里", not any("x1.51" in n for n in _na), str(_na))

# 乘数 <1 仍是负词条（既有口径不回退）
_lpb, _lnb, _ = RA.parse_riven_lines(["x0.55 对 Corpus 的伤害"], _resolve)
check(
    "乘数 <1 仍是负词条（magnitude 45）",
    not _lpb and _lnb == [("damage_vs_corpus", 45.0)],
    f"{_lpb} / {_lnb}",
)
# 模型漏写 x / 漏写乘数算式的两种形态也要接住
_lpc, _lnc, _ = RA.parse_riven_lines(["+1.51 对 Infested 的伤害"], _resolve)
check(
    "写成 +1.51（漏 x）仍按乘数还原成正词条 51",
    _lpc == [("damage_vs_infested", 51.0)] and not _lnc,
    f"{_lpc} / {_lnc}",
)
_lpd, _lnd, _ = RA.parse_riven_lines(["-0.45 对 Infested 的伤害"], _resolve)
check(
    "写成 -0.45（漏 x）也按乘数还原：净倍率 0.45 ⇒ −55%",
    not _lpd and _lnd == [("damage_vs_infested", 55.0)],
    f"{_lpd} / {_lnd}",
)

# 语义表那一路：模型把乘数直接 ×100（x1.51 → 151）也要还原
_fix = plugin.WarframeSDJK._faction_val_fix
check(
    "语义表 151 → 正词条 51（不是 151）",
    _fix("damage_vs_infested", 151.0) == 51.0,
    str(_fix("damage_vs_infested", 151.0)),
)
check(
    "语义表 0.55 → −45（符号交给 _route 归 negative）",
    _fix("damage_vs_corpus", 0.55) == -45.0,
    str(_fix("damage_vs_corpus", 0.55)),
)
check(
    "正常 magnitude 原样不动（51 / 45）",
    _fix("damage_vs_infested", 51.0) == 51.0 and _fix("damage_vs_corpus", 45.0) == 45.0,
)
check("非「对派系」词条一律不动（避免误伤）", _fix("crit_damage", 0.55) == 0.55)

# ---------------------------------------------------------------------------
# ★ 2026-10-05 P0 回归：括号后缀「（重击时 x2）」劫持词条数值。
#   近战紫卡常见「+211.4% 暴击几率（重击时 x2）」——括号里的 x2 命中 _MULT_RE，
#   k=2.0 落在 is_faction_mult 域内 ⇒ 被当派系乘数换算成 100.0，**211.4 静默
#   丢失**且条数判据仍合法（整卡区间/评级全错，最难发现的一类）。
#   修法：搜乘数前剥括号（`_strip_parens`）+ 乘数分支只认 damage_vs_*。
# ---------------------------------------------------------------------------
_p0a, _n0a, _nt0a = RA.parse_riven_lines(["+211.4% 暴击几率（重击时 x2）"], _resolve)
check(
    "P0 括号后缀：暴击几率（重击时 x2）取主体 211.4，不被 x2 换成 100",
    _p0a == [("crit_chance", 211.4)] and not _n0a,
    f"{_p0a} / {_n0a} / {_nt0a}",
)
_p0b, _n0b, _ = RA.parse_riven_lines(["+150.0% 连击持续时间 x2"], _resolve)
check(
    "P0 行尾乘数：连击持续时间 x2 取主体 150，不被换成 100",
    _p0b == [("combo_duration", 150.0)] and not _n0b,
    f"{_p0b} / {_n0b}",
)
_p0c, _n0c, _ = RA.parse_riven_lines(["+120.5% 暴击伤害 x3"], _resolve)
check(
    "P0 对照：x3 出界（>2.05）本就不命中 —— 修后仍 120.5",
    _p0c == [("crit_damage", 120.5)] and not _n0c,
    f"{_p0c} / {_n0c}",
)
_p0d, _n0d, _ = RA.parse_riven_lines(["x1.51 对 Infested 的伤害"], _resolve)
check(
    "P0 勿破坏：真派系乘数行 x1.51 仍是正词条 51",
    _p0d == [("damage_vs_infested", 51.0)] and not _n0d,
    f"{_p0d} / {_n0d}",
)
_p0e, _n0e, _ = RA.parse_riven_lines(["-0.55x 对Corpus的伤害"], _resolve)
check(
    "P0 勿破坏：真派系乘数行 x0.55 仍是负词条 45",
    not _p0e and _n0e == [("damage_vs_corpus", 45.0)],
    f"{_p0e} / {_n0e}",
)
_p0f, _n0f, _ = RA.parse_riven_lines(["+72.9% 射速（弓类武器效果加倍）"], _resolve)
check(
    "P0 括号文案：射速（弓类武器效果加倍）仍取主体 72.9",
    _p0f == [("fire_rate", 72.9)] and not _n0f,
    f"{_p0f} / {_n0f}",
)
_p0g, _n0g, _ = RA.parse_riven_lines(["×59% 多重射击"], _resolve)
check(
    "P0 勿回归：图标误抄 ×59% 出界 ⇒ 按普通数值 59（旧 2026-10-02 口径）",
    _p0g == [("multishot", 59.0)] and not _n0g,
    f"{_p0g} / {_n0g}",
)
check(
    "P0 单点：_strip_parens 只剥括号、保留主体文案",
    RA._strip_parens("+211.4% 暴击几率（重击时 x2）").strip() == "+211.4% 暴击几率"
    and RA._strip_parens("x1.51 对 Infested 的伤害") == "x1.51 对 Infested 的伤害",
    RA._strip_parens("+211.4% 暴击几率（重击时 x2）"),
)
check(
    "回归：x1.51 经语义表 → 3 正 1 负（与行解析同口径）",
    (lambda pn: len(pn[0]) == 3 and len(pn[1]) == 1)(
        _norm(
            {
                "positive": [["暴伤", 91.1], ["对Infested的伤害", 151], ["初始连击", 29.6]],
                "negative": [["处决伤害", 115.7]],
            },
            _rev,
        )
    ),
    str(
        _norm(
            {
                "positive": [["暴伤", 91.1], ["对Infested的伤害", 151], ["初始连击", 29.6]],
                "negative": [["处决伤害", 115.7]],
            },
            _rev,
        )
    ),
)


# ---------------------------------------------------------------------------
# ⑥ 端到端：报障那张卡现在必须**走通**（不再是那条计数错误）
# ---------------------------------------------------------------------------
class _FakeRivenClient:
    """盗贼（手枪列，倾向 1.4）—— 只实现紫卡分析这条链用到的接口。"""

    _weapon = {
        "url_name": "bandit",
        "zh": "盗贼",
        "en": "Bandit",
        "disposition": 1.4,
        "riven_type": "pistol",
        "group": "secondary",
    }

    async def resolve_riven_weapon(self, q):
        return dict(self._weapon)

    async def resolve_variant_disp(self, name):
        return (None, "")

    async def riven_family(self, weapon):
        return []  # 家族无变体：不干扰本条回归的数值判定

    async def wm_riven_weapons(self):
        return [dict(self._weapon)]


class _Img:
    """桩图片组件。`_event_has_image` 按**类型名**判「消息链里有图」，
    所以必须把 __name__ 改成 Image（类名写成 _Img 会在那里判成「没图」→
    直接走用法提示，端到端断言就会空过）。"""


_Img.__name__ = "Image"


class _FakeImageEvent:
    def __init__(self):
        class _MsgObj:
            message = [_Img()]

        self.message_obj = _MsgObj()
        self.unified_msg_origin = "group://riven_card_test"


class _FatParsed:
    def __init__(self, content=""):
        self.content = str(content).split()[1:]
        self.content_str = content
        self.preset, self.page, self.whisper = "", 1, False


# ★ 服务器日志（01:11:52）里的原始识别结果：模型给了 7 条语义词条 + 逐行原文。
#   注意语义表里 基伤/毒素伤害 的数值与行原文是**交叉**的 —— 这正是「名称与
#   数值取同一行」要挡住的另一类错；行原文按报障卡面抄。
_incident_vision = {
    "weapon": "盗贼 Visi-toxican",
    "lines": [
        "盗贼 Visi-toxican",
        "🔒 +120.5% 毒素伤害",
        "+299.9% 伤害",
        "+148% 多重射击",
        "-97.2% 触发时间",
        "内融值 1,234",
    ],
    "positive": [
        ["基伤", 120.5],
        ["毒素伤害", 299.9],
        ["多重", 148],
        ["触发", 97.2],
        ["持续", 97.2],
    ],
    "negative": [["滑暴", 97.2], ["触发时间", 97.2]],
}


async def _fake_imgs(event):
    return ["data:image/jpeg;base64,x"]


async def _fake_extract(url):
    return dict(_incident_vision)


_obj = plugin.WarframeSDJK.__new__(plugin.WarframeSDJK)
_obj.client = _FakeRivenClient()
_obj.page_size = 12
_obj._image_data_urls = _fake_imgs
_obj._extract_riven_from_image = _fake_extract
_reply = asyncio.run(_obj._h_riven_analysis(_FatParsed("紫卡分析"), _FakeImageEvent(), "pc"))
_body = "\n".join(_reply.lines)
check(
    "端到端确实走到了分析卡（不是用法/错误提示 —— 防空过守卫）",
    bool(_reply.lines) and not _reply.raw_text,
    repr(_reply)[:200],
)
check(
    "★ 报障那张卡不再被计数校验挡掉（没有「4 正 2 负」）",
    "4 正 2 负" not in _body and "词条应为" not in _body,
    _body[:220],
)
check(
    "★ 四条词条按卡面行原文配对：120.5 毒伤 / 299.9 基伤 / 148 多重",
    all(k in _body for k in ("120.5% 毒伤", "299.9% 基伤", "148% 多重")),
    _body[:300],
)
check(
    "★ 反向守卫：不再按语义表交叉配错（120.5 基伤 / 299.9 毒伤）",
    "120.5% 基伤" not in _body and "299.9% 毒伤" not in _body,
    _body[:300],
)
check(
    "★ 负词条 = 触时 97.2（不再冒出「滑暴」）",
    "-97.2% 触时" in _body and "滑暴" not in _body,
    _body[:300],
)
check("卡面注明来源为「卡面逐行」而不是语义表", "卡面逐行" in _body, _body[:220])


# 拿不到 lines（渠道/模型没给这个字段）⇒ 安全退回语义 JSON，不崩
async def _fake_extract_nolines(url):
    return {k: v for k, v in _incident_vision.items() if k != "lines"}


_obj._extract_riven_from_image = _fake_extract_nolines
_r2 = asyncio.run(_obj._h_riven_analysis(_FatParsed("紫卡分析"), _FakeImageEvent(), "pc"))
check(
    "没给 lines 时退回语义 JSON（不崩，仍按原判据提示词条数）",
    bool(_r2.raw_text) and "词条应为" in _r2.raw_text,
    repr(_r2)[:200],
)


# ---------------------------------------------------------------------------
# ⑦ 窄读一路：读不全就不采信（2026-09-27 实测结论）
# ---------------------------------------------------------------------------
# 实测（用户那张 321×450 卡 ×3 次）：glm-4v-flash 用完整 JSON 提示词**稳定只
# 照抄 2/4 行**（把图放大 4× 也一样漏），而换成「只要词条行原文」的窄提示词后
# 三个渠道 6/6 全对 ⇒ 词条行单独一路窄读，且判据是「卡面合法」（能解析 ≠ 读全）。
_S = plugin.WarframeSDJK
_FULL = "+120.5% 毒素伤害\n+299.9% 伤害\n+148% 多重射击\n-97.2% 触发时间"
_PART = "+120.5% 毒素伤害\n-97.2% 触发时间"
check(
    "窄读答案 4 行 → 采信",
    _S._parse_riven_lines_text(_FULL)["lines"] == _FULL.splitlines(),
    str(_S._parse_riven_lines_text(_FULL)),
)
check(
    "窄读答案只给 2 行（漏读）→ 不采信，让位下一个渠道",
    _S._parse_riven_lines_text(_PART) == {},
    str(_S._parse_riven_lines_text(_PART)),
)
_prose = _S._parse_riven_lines_text("以下是词条行：\n" + _FULL)
# ★ 2026-10-02 口径细化（见 §⑨）：寒暄/武器名/图例这类**结构性杂行**（无极性
#   符号）跳过不算致命 —— 词条行 4/4 全在 ⇒ 仍采信；只有「像是词条行却没解析
#   成功」（词条名认不出/数值读不出）才判不合法（16:17 海波单剑事故口径）。
check(
    "窄读答案夹带寒暄行不影响（杂行天然跳过；真丢词条才判不合法）",
    _prose.get("lines", [])[:1] == ["以下是词条行："],
    str(_prose),
)


class _FakeVision:
    """固定回答 + 延时，模拟「快而残缺」与「慢但读全」两家渠道。"""

    def __init__(self, text, delay):
        self.text, self.delay = text, delay

    async def text_chat(self, prompt, session_id=None, image_urls=None):
        await asyncio.sleep(self.delay)
        return types.SimpleNamespace(completion_text=self.text)


_race = asyncio.run(
    _S.__new__(_S)._vision_race_json(
        "p",
        "data:image/jpeg;base64,x",
        [_FakeVision(_PART, 0.01), _FakeVision(_FULL, 0.3)],
        k=2,
        tag="测试行读",
        window=5,
        parse=_S._parse_riven_lines_text,
    )
)
check(
    "★ 竞速不采信先到的残缺结果，等读全的那一路", _race == {"lines": _FULL.splitlines()}, str(_race)
)
_race2 = asyncio.run(
    _S.__new__(_S)._vision_race_json(
        "p",
        "data:image/jpeg;base64,x",
        [_FakeVision(_PART, 0.01)],
        k=2,
        tag="测试行读",
        window=0.6,
        parse=_S._parse_riven_lines_text,
    )
)
check("只有残缺结果时竞速返回 None（不硬用残缺卡面）", _race2 is None, str(_race2))


# ---------------------------------------------------------------------------
# ⑧ 端到端：空刃 3+1（2026-10-01 报障）—— 必须走通，且按变体倾向算区间
# ---------------------------------------------------------------------------
class _FakeSkanaClient(_FakeRivenClient):
    """空刃（近战，倾向 1.3）+ 家族变体棱晶·空刃 / 空刃 Prime（同为 1.2）。"""

    _weapon = {
        "url_name": "skana",
        "zh": "空刃",
        "en": "Skana",
        "disposition": 1.3,
        "riven_type": "melee",
        "group": "melee",
    }

    async def riven_family(self, weapon):
        return [("棱晶·空刃", 1.2), ("空刃 Prime", 1.2)]


# 服务器日志（01:42:44）里的原始两路结果：窄读抄全 4 行、语义表 3正1负（乘数写成 151）
_skana_vision = {
    "weapon": "空刃 Para-puratis",
    "lines": [
        "空刃 Para-puratis",
        "+91.1% 暴击伤害",
        "x1.51 对 Infested 的伤害",
        "+29.6 初始连击",
        "-115.7% 处决伤害",
    ],
    "positive": [["暴伤", 91.1], ["对Infested的伤害", 151], ["初始连击", 29.6]],
    "negative": [["处决伤害", 115.7]],
}


async def _fake_extract_skana(url):
    return dict(_skana_vision)


_obj2 = plugin.WarframeSDJK.__new__(plugin.WarframeSDJK)
_obj2.client = _FakeSkanaClient()
_obj2.page_size = 12
_obj2._image_data_urls = _fake_imgs
_obj2._extract_riven_from_image = _fake_extract_skana
_r3 = asyncio.run(_obj2._h_riven_analysis(_FatParsed("紫卡分析"), _FakeImageEvent(), "pc"))
_body3 = "\n".join(_r3.lines)
check(
    "★ 端到端：空刃那张 3+1 卡走到分析卡（不是用法/错误提示 —— 防空过守卫）",
    bool(_r3.lines) and not _r3.raw_text,
    repr(_r3)[:200],
)
check(
    "★ 四条词条全部进卡：暴伤 91.1 / I伤 51 / 初始连击 29.6 / 处决伤 115.7",
    all(k in _body3 for k in ("+91.1% 暴伤", "+51% I伤", "+29.6 初始连击", "-115.7% 处决伤")),
    _body3[:400],
)
check("★ 不再误报「与家族已知倾向都不吻合」（旧故障串）", "都不吻合" not in _body3, _body3[:300])
check(
    "★ 数值反推落到变体倾向 1.2（棱晶·空刃 / 空刃 Prime 同值 ⇒ 不算歧义）",
    "数值反推倾向 1.2" in _body3,
    _body3[:300],
)
check(
    "★ 卡面按 1.2 计算区间（不是不吻合的母武器 1.3）",
    "【空刃】倾向 1.2" in _body3 and "倾向 1.3" not in _body3,
    _body3[:200],
)
check(
    "不再回「请带变体名重发」（同值变体无从也无须区分）",
    "请带变体名重发" not in _body3,
    _body3[:300],
)

# ---------------------------------------------------------------------------
# ⑥ 后坐力极性反转（2026-10-02 用户报障：卡面四行全 `+`，其中
#    「+95.4% 武器后坐力」实为**负面**）—— WM 1500 条 / 32 字段实测只有 recoil
#    反转；其余词条（含 zoom）符号即极性，必须原样。
# ---------------------------------------------------------------------------
check(
    "数据守卫：INVERTED_STATS 恰为 {recoil}（新增条目必须带实测证据）",
    RA.INVERTED_STATS == {"recoil"},
    str(RA.INVERTED_STATS),
)
_SIGN_OK = [
    "multishot",
    "crit_chance",
    "crit_damage",
    "reload_speed",
    "zoom",
    "magazine_capacity",
    "status_chance",
    "status_duration",
    "range",
    "attack_speed",
    "damage_vs_grineer",
]
check(
    "数据守卫：实测「符号即极性」的词条与 INVERTED_STATS 互补（一个都不许进）",
    not (set(_SIGN_OK) & RA.INVERTED_STATS),
    str(set(_SIGN_OK) & RA.INVERTED_STATS),
)

_pR, _nR, _ = RA.parse_riven_lines(["+95.4% 武器后坐力"], _resolve)
check(
    "★ 行解析：`+95.4% 武器后坐力` → 负面 recoil 95.4（不再当正面）",
    not _pR and _nR == [("recoil", 95.4)],
    f"{_pR} / {_nR}",
)
_pR2, _nR2, _ = RA.parse_riven_lines(["-20% 武器后坐力"], _resolve)
check(
    "★ 行解析：`-20% 武器后坐力` → 正面 recoil 20（减后坐力是好事）",
    _pR2 == [("recoil", 20.0)] and not _nR2,
    f"{_pR2} / {_nR2}",
)
_pR3, _nR3, _ = RA.parse_riven_lines(["+95.4% 多重射击"], _resolve)
check(
    "不许误伤：`+95.4% 多重射击` 仍是正面",
    _pR3 == [("multishot", 95.4)] and not _nR3,
    f"{_pR3} / {_nR3}",
)

_posR, _negR = _norm(
    {"weapon": "盗贼", "positive": [["后坐力", 95.4], ["多重", 146.7]], "negative": []}, _rev
)
check(
    "★ vision 路由：后坐力 +95.4 → negative（符号 + 反转表判据）",
    _negR == [("recoil", 95.4)] and _posR == [("multishot", 146.7)],
    f"{_posR} / {_negR}",
)
_posR2, _negR2 = _norm({"weapon": "盗贼", "positive": [["后坐力", -20]], "negative": []}, _rev)
check(
    "★ vision 路由：后坐力 -20 → positive 20",
    _posR2 == [("recoil", 20.0)] and not _negR2,
    f"{_posR2} / {_negR2}",
)
_posR3, _negR3 = _norm({"weapon": "盗贼", "positive": [["变焦", 30.0]], "negative": []}, _rev)
check(
    "不许误伤：变焦（zoom）仍按符号定极性（实测不反转）",
    _posR3 == [("zoom", 30.0)] and not _negR3,
    f"{_posR3} / {_negR3}",
)

# ---------------------------------------------------------------------------
# ⑦ 派系行兼容：`x` 在**行尾**（`对 X 的伤害 x1.51`）—— 2026-10-02 线上实证：
#    窄读把 x1.51 抄到行尾 ⇒ 行首无极性符号 ⇒ 旧版整条丢弃（3 条派系行全丢）。
# ---------------------------------------------------------------------------
_lpF, _lnF, _ = RA.parse_riven_lines(["对 Grineer 的伤害 x1.51"], _resolve)
check(
    "★ 派系行：x 在行尾也认（+51% 对 Grineer，不再整条丢弃）",
    _lpF == [("damage_vs_grineer", 51.0)] and not _lnF,
    f"{_lpF} / {_lnF}",
)
_lpF2, _lnF2, _ = RA.parse_riven_lines(["对 Infested 的伤害 x0.55"], _resolve)
check(
    "★ 派系行：x 在行尾 + 减伤 ⇒ 负面 45",
    not _lpF2 and _lnF2 == [("damage_vs_infested", 45.0)],
    f"{_lpF2} / {_lnF2}",
)
_lpF3, _, _ = RA.parse_riven_lines(["x1.51 对 Infested 的伤害"], _resolve)
check("派系行：x 在行首的旧写法不受影响", _lpF3 == [("damage_vs_infested", 51.0)], f"{_lpF3}")

# ★ 2026-10-02 线上实证（努寇微波枪 16:27）：窄读把词条图标抄成 `×`
#   （「×59% 多重射击」），旧实现一律按乘数换算 ⇒ |1−59|×100 = **5800%**。
#   出界的 x/× 不是派系乘数（真实乘数域 ≈0.42~1.95）⇒ 按普通数值读。
_lpM, _lnM, _nM = RA.parse_riven_lines(["×59% 多重射击"], _resolve)
check(
    "★ 图标被抄成 ×：`×59% 多重射击` → 多重 59（不再是 5800%）",
    _lpM == [("multishot", 59.0)] and not _lnM,
    f"{_lpM} / {_lnM}",
)
_lpM2, _lnM2, _ = RA.parse_riven_lines(
    ["+39.5% 暴击伤害", "+44.9% 暴击伤害", "×59% 多重射击", "×0.83 对 Corpus 的伤害"], _resolve
)
check(
    "★ 报障卡原行：暴伤×2 + 多重 59 + C伤 17（不再出现 5800）",
    ("multishot", 59.0) in _lpM2
    and _lnM2 == [("damage_vs_corpus", 17.0)]
    and all(p[1] != 5800.0 for p in _lpM2),
    f"{_lpM2} / {_lnM2}",
)
_posN, _negN = _norm(
    {"weapon": "努寇微波枪", "positive": [["对Corpus伤害", 5800]], "negative": []}, _rev
)
check(
    "语义表同口径：出界的 5800 不再被当乘数换算成 5700",
    _posN == [("damage_vs_corpus", 5800.0)] and not _negN,
    f"{_posN} / {_negN}",
)
_lpK, _lnK, _ = RA.parse_riven_lines(["x0.55 对 Corpus 的伤害"], _resolve)
check(
    "带内乘数（0.55）仍照旧换算（守卫没有过度收紧）",
    _lnK == [("damage_vs_corpus", 45.0)],
    f"{_lpK} / {_lnK}",
)

# ---------------------------------------------------------------------------
# ⑧ 词条名形近容错（线上实证：OCR 把「触发几率」读成「脆发几率」，整行被跳过）
# ---------------------------------------------------------------------------
check(
    "★ 形近容错：脆发几率 → status_chance（触发几率；相似度 0.75）",
    plugin.WarframeSDJK._stat_id_from_name("脆发几率", _rev) == "status_chance",
)
check(
    "形近容错反例：武器伤害（最相近仅 0.5）不采纳",
    plugin.WarframeSDJK._stat_id_from_name("武器伤害", _rev) is None,
)
check(
    "形近容错反例：段位（与任何词条相似度 0）不采纳",
    plugin.WarframeSDJK._stat_id_from_name("段位", _rev) is None,
)

# ★ 2026-10-02 别名补录（官方 wiki 基值表两侧 combo 词的核对产物）：中文表里
#   「仅负向」那行（近战 104.85%）的卡面名 —— 缺它整行会落「词条名认不出」。
check(
    "★ 别名：几率不获得连击数 → combo_gain_chance（仅负向词条）",
    plugin.WarframeSDJK._stat_id_from_name("几率不获得连击数", _rev) == "combo_gain_chance",
)
check(
    "别名：额外连击数几率 → extra_combo_count（仅正向词条）",
    plugin.WarframeSDJK._stat_id_from_name("额外连击数几率", _rev) == "extra_combo_count",
)
check(
    "★ 别名：DE 卡面原文「的几率来获得连击数」→ combo_gain_chance",
    plugin.WarframeSDJK._stat_id_from_name("的几率来获得连击数", _rev) == "combo_gain_chance",
)
# ★ 2026-10-02 归并（口径统一）：幽灵 sid combo_efficiency（「连击效率」，
#   基值恰好等于额外连击数几率）并入 extra_combo_count；三处表都不再有它。
check(
    "★ 归并：连击效率 / 近战连击效率 → extra_combo_count",
    plugin.WarframeSDJK._stat_id_from_name("连击效率", _rev) == "extra_combo_count"
    and plugin.WarframeSDJK._stat_id_from_name("近战连击效率", _rev) == "extra_combo_count",
)
check(
    "★ 幽灵 sid 已清除（基值表 / 展示名表都不再有 combo_efficiency）",
    "combo_efficiency" not in RA._BASE and "combo_efficiency" not in RIVEN_STAT_ZH,
)
check("归并后基值不变：额外连击数几率仍为近战 58.77", RA._BASE["extra_combo_count"][4] == 58.77)

# ★ 2026-10-03 术语（用户口径：远程叫「射速」、近战叫「攻速」）：`_STAT_ALIAS`
#   此前把「射速」改写成「攻速」⇒ 远程卡显示成近战词条 attack_speed。
check(
    "★ 术语：射速 → fire_rate（不再被压成 attack_speed）",
    plugin.WarframeSDJK._stat_id_from_name("射速", _rev) == "fire_rate",
)
check(
    "术语：攻速 → attack_speed（近战词条不受影响）",
    plugin.WarframeSDJK._stat_id_from_name("攻速", _rev) == "attack_speed",
)
check(
    "术语：射击速度 / 射击速率 也走 fire_rate",
    plugin.WarframeSDJK._stat_id_from_name("射击速度", _rev) == "fire_rate"
    and plugin.WarframeSDJK._stat_id_from_name("射击速率", _rev) == "fire_rate",
)
check(
    "★ 语义相反的跨词条改写已清除（_STAT_ALIAS 不含 射速 / 元素伤害）",
    "射速" not in plugin.WarframeSDJK._STAT_ALIAS
    and "元素伤害" not in plugin.WarframeSDJK._STAT_ALIAS,
)

# ★ 2026-10-02 用户实卡（翁 Locti-acrium）：「-60.4% 的几率来获得连击数」
#   —— ① 卡面原文认得出；②「仅负向」锁定生效（读成正数也归负面，wiki Legend ³）；
#   ③ 基值 104.85 与该卡自洽（d=0.70：四行 U = 0.92/1.02/0.93/1.10 全在 ±10%）。
_lpO, _lnO, _ = RA.parse_riven_lines(
    ["+14.8 初始连击", "+1.3 攻击范围", "+54.8% 暴击伤害", "-60.4% 的几率来获得连击数"], _resolve
)
check(
    "★ 实卡四行 → 3 正 1 负（连击获取 60.4 归负、不再「认不出」）",
    [p[0] for p in _lpO] == ["initial_combo", "range", "crit_damage"]
    and _lnO == [("combo_gain_chance", 60.4)],
    f"{_lpO} / {_lnO}",
)
_lpO2, _lnO2, _ = RA.parse_riven_lines(["+60.4% 的几率来获得连击数"], _resolve)
check(
    "★ 仅负向锁定：即使读成正数也归负面（NEGATIVE_ONLY）",
    not _lpO2 and _lnO2 == [("combo_gain_chance", 60.4)],
    f"{_lpO2} / {_lnO2}",
)
_posO, _negO = _norm(
    {"weapon": "翁", "positive": [["的几率来获得连击数", 60.4]], "negative": []}, _rev
)
check(
    "★ vision 路由同口径：连击获取进 negative",
    _negO == [("combo_gain_chance", 60.4)] and not _posO,
    f"{_posO} / {_negO}",
)
_lo_o, _hi_o = RA.stat_range("combo_gain_chance", "melee", 0.70, 3, 1, negative=True)
check(
    "基值自洽：翁 d=0.70 时 60.4 落在 104.85 的负档区间（49.54-60.55）",
    _lo_o is not None and _lo_o <= 60.4 <= _hi_o,
    f"{_lo_o}-{_hi_o}",
)

# ---------------------------------------------------------------------------
# ⑨ 采信判据：**像是词条行却没解析成功** ⇒ 不许判「合法采信」（线上 16:17
#    海波单剑事故：条数恰好达标，但有一条词条行被跳过 —— 旧实现照样「采信」
#    并覆盖语义表）。结构性杂行（武器名/图例/内融值/段位）不算致命。
# ---------------------------------------------------------------------------
_lpL, _lnL, _legalL, _noteL = plugin.WarframeSDJK._riven_lines_legal(
    ["+231.2% 近战伤害", "+148% 多重射击", "+124.2% 喵喵词条", "-106.1% 触发时间"]
)
check(
    "★ 采信判据：词条行认不出（喵喵词条）⇒ 不合法（条数达标也不许采信）",
    (not _legalL) and any(n.startswith("词条名认不出") for n in _noteL),
    f"legal={_legalL} notes={_noteL}",
)
_lpL2, _lnL2, _legalL2, _noteL2 = plugin.WarframeSDJK._riven_lines_legal(
    ["+231.2% 近战伤害", "+148% 多重射击", "+2.5 攻击范围", "-106.1% 触发时间", "段位 13"]
)
check(
    "对照：结构性杂行（段位 13 无极性符号）被跳过 ⇒ 仍合法（不误杀真读全）",
    _legalL2 and _noteL2 and len(_lpL2) == 3 and len(_lnL2) == 1,
    f"legal={_legalL2} notes={_noteL2}",
)
_lpL3, _lnL3, _legalL3, _noteL3 = plugin.WarframeSDJK._riven_lines_legal(
    ["+231.2% 近战伤害", "+148% 多重射击", "-106.1% 触发时间"]
)
check(
    "对照：无跳过且条数达标 ⇒ 合法（判据没有过度收紧）",
    _legalL3 and not _noteL3 and len(_lpL3) == 2 and len(_lnL3) == 1,
    f"legal={_legalL3} notes={_noteL3}",
)

# ---------------------------------------------------------------------------
# ⑩ 窄读 prompt 不变量（2026-10-05 **二次精简**：213 字 → 60 字，提问式）
#    全过程：A(517)→…→F(213)→ S4(32)→**S6(60，现行)**，判分一律走
#    `_riven_lines_legal` + `parse_riven_lines`，16 张真实卡 × 2 模型。
#    ★ 关键实证（勿凭直觉改回长版）：
#      · 8B 在 213 字版下 **11 个版次全漏** `🔒+59% 多重射击`（05 卡）；
#        换提问式后 47/48 读到。中性探针「有几行词条？逐行念出来」→ 5/5 读到
#        ⇒ 不是看不见、不是分辨率，是长提示词的约束框把它筛掉了。
#      · **「卡面上没有的行绝对不要编造」是副作用源**：S5 只把该句加回 S4，
#        8B 立刻复现漏锁行（23/24）、30B 输出「第一行：」前缀 3/8 被拒。
#        它当年防的幻觉源自「x1.51」**字面示例**，示例删掉后该句只剩副作用。
#      · 隔壁方案「明说无视 emoji」实测无效（8B 仍漏 05）。
# ---------------------------------------------------------------------------
import inspect as _inspect  # noqa: E402

_line_prompt = plugin.WarframeSDJK._RIVEN_LINE_PROMPT
check(
    "窄读 prompt：提问式 + 只念词条行（S6 的两处承重件）",
    "有几行词条" in _line_prompt and "只念词条行" in _line_prompt,
    _line_prompt,
)
check(
    "窄读 prompt：纯格式约束在（不要编号/列表符号/说明文字 —— 收 markdown 噪声）",
    "不要编号" in _line_prompt
    and "不要列表符号" in _line_prompt
    and "不要任何说明文字" in _line_prompt,
    _line_prompt,
)
check(
    "窄读 prompt：不含任何示例数值（x1.51/x0.55 是 30B 照抄泄漏源）",
    "x1.51" not in _line_prompt and "x0.55" not in _line_prompt and "1.51" not in _line_prompt,
    _line_prompt,
)
check(
    "★ 窄读 prompt：**不得**出现「不要编造」类反声明（实测副作用源）",
    "不要编造" not in _line_prompt and "别编" not in _line_prompt and "不能编" not in _line_prompt,
    _line_prompt,
)
check(
    "★ 窄读 prompt：**不得**出现锁行/图标交代（实测 11 版次全无效）",
    "带锁形图标的行" not in _line_prompt and "无视" not in _line_prompt,
    _line_prompt,
)
check(
    "窄读 prompt：已精简到 60 字（517 → 213 → 60）",
    len(_line_prompt) <= 100,
    f"len={len(_line_prompt)}",
)
_sem_prompt = _inspect.getsource(plugin.WarframeSDJK._extract_riven_from_image)
check(
    "语义 prompt：含元素伤害负例与「同一条词条不会出现两次」",
    "不得写成「暴击伤害」" in _sem_prompt and "不会出现两次" in _sem_prompt,
)

# ⑪ prompt 词条枚举（2026-10-03 线上实证：枚举缺「弹匣容量/变焦」，
#    语义渠道猜成 暴伤/集束、窄读 OCR 成 坦克容星/集中，负词条被整条丢弃）
#    窄读侧：2026-10-05 精简版**删掉了这两条正字样**（八版对照实测：8B 侧
#    14/16→14/16、30B 侧 3/16→14/16，无回退；负例改由「逐字照抄、不要改写」
#    通用规则承接）。此处只守语义（JSON）渠道仍带枚举。
check(
    "⑪ 语义 prompt：枚举含 弹匣容量/变焦 + 「照卡面原样照抄」+ 负词条警示",
    "弹匣容量" in _sem_prompt
    and "变焦" in _sem_prompt
    and "照卡面原样照抄" in _sem_prompt
    and "负词条绝不能丢" in _sem_prompt,
)
check(
    "⑪ 窄读 prompt：改守「先数行数」的计数框架（S6 下负词条/图标行都不丢）",
    "有几行词条" in _line_prompt,
    _line_prompt,
)

# ---------------------------------------------------------------------------
# ⑫ 武器名解析优先级：手输名优先 + OCR 名兜底（2026-10-07 线上报障回归）
# ---------------------------------------------------------------------------
# 报障（线上日志 10-07 10:59 / 17:11 / 17:13 三次）：
#   `[图片] 紫卡分析 冰凇` → 回「未找到紫卡武器「冰松」」。
# 根因：卡面首行被 OCR 读成**近形字**「冰松」（实为冰凇 Verglas），而旧实现
#   `weapon_name = data.get("weapon") or weapon_name` 让 **OCR 名无条件覆盖手输名**
#   ⇒ 用户写对的名字被丢掉、整卡失败。现改为「手输名优先，认不出时再用 OCR 名兜底」。
# 数值沿用 ⑥ 号回归的夹具（该卡能走通出卡），本段的判据**只关心解析用的是哪个名字**。


class _RecClient:
    """记录每次解析传入的武器名；「冰凇」可解析、「冰松」不可（复刻线上形态）。"""

    _weapon = dict(_FakeRivenClient._weapon)
    _weapon.update({"url_name": "verglas", "zh": "冰凇", "en": "Verglas"})

    def __init__(self):
        self.queries = []

    async def resolve_riven_weapon(self, q):
        self.queries.append(q)
        return dict(self._weapon) if q == "冰凇" else None

    async def resolve_variant_disp(self, name):
        return (None, "")

    async def riven_family(self, weapon):
        return []

    async def suggest_riven_weapons(self, q, n=3):
        return []

    async def wm_riven_weapons(self):
        return [dict(self._weapon)]  # 近形字兜底的候选池（⑬ 段用）

    async def nearmiss_riven_weapons(self, q, n=4):
        out = []
        for w in await self.wm_riven_weapons():
            zh = (w.get("zh") or "").strip()
            if zh and len(zh) == len(q) and sum(1 for a, b in zip(zh, q) if a != b) == 1:
                out.append(w)
        return out[:n]


# 识别结果里的 weapon 就是线上那条「冰松 Visi-critapha」
_iceq_vision = dict(_incident_vision, weapon="冰松 Visi-critapha")
# 对照：同一张卡但 OCR 读对（纯图路径用）
_iceq_vision_ok = dict(_incident_vision, weapon="冰凇 Visi-critapha")


def _rec_obj(vision: dict):
    o = plugin.WarframeSDJK.__new__(plugin.WarframeSDJK)
    o.client = _RecClient()
    o.page_size = 12
    o._image_data_urls = _fake_imgs

    async def _ext(_u):
        return dict(vision)

    o._extract_riven_from_image = _ext
    return o


# A：手输名对（冰凇）+ OCR 读错（冰松）⇒ 必须用手输名解析
_objA = _rec_obj(_iceq_vision)
_rA = asyncio.run(_objA._h_riven_analysis(_FatParsed("紫卡分析 冰凇"), _FakeImageEvent(), "pc"))
check(
    "⑫① 手输名优先：解析用的是「冰凇」，不是 OCR 的「冰松」",
    _objA.client.queries[:1] == ["冰凇"],
    str(_objA.client.queries),
)
check(
    "⑫② 该卡出卡（不再是「未找到紫卡武器」）",
    bool(_rA.lines) and not _rA.raw_text,
    repr(_rA)[:200],
)

# B：手输名写错（冰松）+ 卡面读对（冰凇）⇒ OCR 名兜底那一跳要接住
#   （用读音对照件：反过来才是「手输对、卡面读错」，见情形 A）
_objB = _rec_obj(_iceq_vision_ok)
_rB = asyncio.run(_objB._h_riven_analysis(_FatParsed("紫卡分析 冰松"), _FakeImageEvent(), "pc"))
check(
    "⑫③ OCR 名兜底：手输名认不出时，用卡面名「冰凇」再试一次",
    _objB.client.queries == ["冰松", "冰凇"],
    str(_objB.client.queries),
)
check(
    "⑫④ 兜底后同样出卡",
    bool(_rB.lines) and not _rB.raw_text,
    repr(_rB)[:200],
)

# C：纯图路径（不输名）行为不变 —— 仍按 OCR 名解析（此例 OCR 读对）
_objC = _rec_obj(_iceq_vision_ok)
_rC = asyncio.run(_objC._h_riven_analysis(_FatParsed("紫卡分析"), _FakeImageEvent(), "pc"))
check(
    "⑫⑤ 纯图路径不变：仍用卡面识别名解析、且出卡",
    _objC.client.queries[:1] == ["冰凇"] and bool(_rC.lines),
    f"{_objC.client.queries} / {repr(_rC)[:120]}",
)

# ---------------------------------------------------------------------------
# ⑬ 近形字兜底（卡面数值可行性当闸门）—— 纯图路径被 OCR 读错时的自愈
# ---------------------------------------------------------------------------
# ⑫ 修好的是「用户手输了名字」的情形；**纯图路径（不输名）没有手输名可用**，
# 只能靠这一跳自愈（线上 10-07 三次报障正是这种卡：冰凇 被读成 冰松）。
_objD = _rec_obj(_iceq_vision)  # weapon = 冰松 Visi-critapha，表内只有「冰凇」
_rD = asyncio.run(_objD._h_riven_analysis(_FatParsed("紫卡分析"), _FakeImageEvent(), "pc"))
_bodyD = "\n".join(_rD.lines)
check(
    "⑬① 纯图路径：OCR 读成近形字也能出卡（不再是「未找到」）",
    bool(_rD.lines) and not _rD.raw_text,
    repr(_rD)[:200],
)
check(
    "⑬② 卡面注明识别名与实际计算名（数值可行性闸门通过才采纳）",
    "已按「冰凇」计算" in _bodyD,
    _bodyD[:300],
)

# ---------------------------------------------------------------------------
# ⑭ 「多个近形候选都可行」⇒ 不猜：列进候选提示、不出卡
# ---------------------------------------------------------------------------
# 桩数据（名字为合成，只为触发分支）：OCR 读成「七星刃」，表里两个**等长 + 1 字差**
# 的候选（七星刀 / 七星剑）倾向都是 1.4 ⇒ 数值都能解释卡面 ⇒ 必须**不采纳**。
_vision_multi = dict(_incident_vision, weapon="七星刃 Seven-star")


class _MultiNearClient(_RecClient):
    _w1 = dict(_RecClient._weapon, url_name="nr1", zh="七星刀", en="Near One")
    _w2 = dict(_RecClient._weapon, url_name="nr2", zh="七星剑", en="Near Two")

    async def wm_riven_weapons(self):
        return [dict(self._w1), dict(self._w2)]


_objE = plugin.WarframeSDJK.__new__(plugin.WarframeSDJK)
_objE.client = _MultiNearClient()
_objE.page_size = 12
_objE._image_data_urls = _fake_imgs


async def _extE(_u):
    return dict(_vision_multi)


_objE._extract_riven_from_image = _extE
_rE = asyncio.run(_objE._h_riven_analysis(_FatParsed("紫卡分析"), _FakeImageEvent(), "pc"))
check(
    "⑭① 多个近形候选都可行 ⇒ 不猜（仍回未找到、不出卡）",
    bool(_rE.raw_text) and "未找到紫卡武器" in _rE.raw_text,
    repr(_rE)[:200],
)
check(
    "⑭② 两个可行候选都列进提示",
    "七星刀" in (_rE.raw_text or "") and "七星剑" in (_rE.raw_text or ""),
    (_rE.raw_text or "")[:200],
)

# ---------------------------------------------------------------------------
# ⑮ 紫卡识别**不竞速**：只取一个渠道，且优先 8B（用户 2026-10-07 裁示）
# ---------------------------------------------------------------------------
# 实测（2026-10-07，同一张「冰凇」卡 × 各 5 次）：
#   glm-4v-flash → 冰松 **5/5**（确定性读错，且最快 ~3.1 s ⇒ k=2 竞速里必赢）；
#   30B → 冰凇 2/5 ｜ 8B → 冰凇 5/5。线上三次「冰松」的 3.1 s 与 glm 吻合。
# ⇒ 紫卡这一路只发一个渠道（名字是承重件，宁慢不赌）；识卡那路保留原竞速。


class _Prov:
    def __init__(self, pid):
        self._pid = pid

    def meta(self):
        return types.SimpleNamespace(id=self._pid)


_seen = {}


class _OrderStub:
    def __init__(self, provs):
        self._provs = provs

    def _vision_providers(self):
        return list(self._provs)

    async def _vision_race_json(self, prompt, image_url, provs, **kw):
        _seen["order"] = [_p.meta().id for _p in provs]
        return None


def _raced_with(prov_ids):
    _seen.clear()
    obj = plugin.WarframeSDJK.__new__(plugin.WarframeSDJK)
    stub = _OrderStub([_Prov(i) for i in prov_ids])
    obj._vision_providers = stub._vision_providers
    obj._vision_race_json = stub._vision_race_json
    asyncio.run(obj._extract_riven_from_image("data:image/png;base64,x"))
    return _seen.get("order", [])


check(
    "⑮① 紫卡路：候选里挑 8B、且只传一个渠道（不竞速）",
    _raced_with(
        [
            "siliconflow/Qwen/Qwen3-VL-8B-Instruct",
            "zhipu/glm-4v-flash",
            "siliconflow/Qwen/Qwen3-VL-30B-A3B-Instruct",
        ]
    )
    == ["siliconflow/Qwen/Qwen3-VL-8B-Instruct"],
    str(_seen),
)
check(
    "⑮② 配置/候选顺序里 8B 不在首位时，仍优先挑 8B（避免被 glm 顶上）",
    _raced_with(
        [
            "zhipu/glm-4v-flash",
            "siliconflow/Qwen/Qwen3-VL-8B-Instruct",
        ]
    )
    == ["siliconflow/Qwen/Qwen3-VL-8B-Instruct"],
    str(_seen),
)
check(
    "⑮③ 没有 8B 时退化为「第一个可用渠道」（单发，不竞速）",
    _raced_with(["zhipu/glm-4v-flash", "siliconflow/Qwen/Qwen3-VL-30B-A3B-Instruct"])
    == ["zhipu/glm-4v-flash"],
    str(_seen),
)

if FAILED:
    print(f"\n失败 {len(FAILED)} 项：{FAILED}")
    sys.exit(1)
print("\n全部通过 ✔")
