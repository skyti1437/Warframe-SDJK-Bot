# -*- coding: utf-8 -*-
"""紫卡截图（vision）词条归一化回归：卡面全称不能被短名抢走。

2026-09-24 玩家报障：发「紫卡分析 + 截图」后分析卡报
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
import json
import sys
from hashlib import sha256
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

# ------------------------- ②c 「霰弹式副手」：食人鱼实卡（2026-10-19 报障）
# 食人鱼（Pyrana）在 WM 里是 group=secondary、rivenType=shotgun；那批「霰弹式副手」
# 共 15 把（德特昂/野马/布拉克/科马克…）都被标成 shotgun。此前 `_CLASS_KEYS` 没有
# secondary → group 那层对副手静默失效，落到 rivenType ⇒ 整类按**霰弹枪基值列**算。
#
# 实卡（3+1，倾向 0.95）：暴击 +124.4 / 多重 +103.9 / 射速 +71.7 / 装填 −32.1
#   · 按霰弹列：暴击 124.4 出界（72.14~88.17）、射速 71.7 出界（下限 72.14）⇒ 误判
#   · 按手枪列：四条全部落带
# 口径与 wfsim 一致：它按武器能吃哪些 MOD 定类（这些武器 `mod_pools: [pistol]`，
# `class: shotgun_sidearm` 只是伤害模拟用的武器类）。
_py_pos = [("crit_chance", 124.4), ("multishot", 103.9), ("fire_rate", 71.7)]
_py_neg = [("reload_speed", 32.1)]
check(
    "★ 霰弹式副手：group=secondary 必须压过被标错的 rivenType=shotgun",
    RA.weapon_class("shotgun", "secondary") == "pistol",
    RA.weapon_class("shotgun", "secondary"),
)
check(
    "★ 食人鱼实卡 4/4 可行（倾向 0.95，不再误报「疑似变体卡」）",
    RA.disp_feasible(_py_pos, _py_neg, RA.weapon_class("shotgun", "secondary"), 0.95) is True,
)
check(
    "  反证：同一张卡按霰弹列不可行（证明这条链真在区分列，不是恒真）",
    RA.disp_feasible(_py_pos, _py_neg, "shotgun", 0.95) is False,
)

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
# ⑤b 乘数行（对派系伤害）—— 2026-10-01 玩家报障：3+1 的卡被读成 2+1
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
    "七条非法语义结构由独立完整窄读恢复，不让乱序JSON否决卡面原文",
    "待核对" not in _txt(_reply)
    and "120.5" in _txt(_reply)
    and "299.9" in _txt(_reply)
    and bool(_reply.lines),
    _txt(_reply),
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
    "窄读残缺保留观察，但不标合法",
    _S._parse_riven_lines_text(_PART).get("_line_legal") is False,
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
    "★ 竞速不采信先到的残缺结果，等读全的那一路",
    _race.get("lines") == _FULL.splitlines() and _race.get("_line_legal"),
    str(_race),
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
check(
    "只有残缺结果时保留原文但不标合法",
    _race2.get("_line_legal") is False and _race2.get("lines") == _PART.splitlines(),
    str(_race2),
)


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
    all(k in _body3 for k in ("▲暴伤 +91.1%", "▲I伤 +51%", "▲初始连击 +29.6", "▼处决伤 -115.7%")),
    _body3[:400],
)
check("★ 不再误报「与家族已知倾向都不吻合」（旧故障串）", "都不吻合" not in _body3, _body3[:300])
check(
    "★ 倾向统一 1.2：不再走变体反推（卡面直接按 1.2，无反推注记）",
    "数值反推" not in _body3,
    _body3[:300],
)
check(
    "★ 卡面按 1.2 计算区间（不是不吻合的母武器 1.3）",
    "武器：空刃｜倾向 1.2" in _body3 and "倾向 1.3" not in _body3,
    _body3[:200],
)
check(
    "不再回「请带变体名重发」（同值变体无从也无须区分）",
    "请带变体名重发" not in _body3,
    _body3[:300],
)

# ---------------------------------------------------------------------------
# ⑥ 后坐力极性反转（2026-10-02 玩家报障：卡面四行全 `+`，其中
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
from core.commands.vision import original_vision_prompt  # noqa: E402

_line_prompt = plugin.WarframeSDJK._RIVEN_LINE_PROMPT
check(
    "窄读只输出文字和归一坐标，不包含某张卡的数值示例",
    "cx,cy,width,height,angle,text" in _line_prompt
    and "to 0-1000" in _line_prompt
    and "x1.51" not in _line_prompt
    and "x0.55" not in _line_prompt,
)
check(
    "窄读保留完整名称并排除图形转文字的要求",
    "each separate region on separate lines" in _line_prompt
    and "Do not transcribe icons" in _line_prompt,
)
_sem_prompt = original_vision_prompt("riven_json")
check(
    "语义提示词仅转录完整名称，未知行保留且不丢负号",
    '"stat_lines"' in _sem_prompt
    and "完整词条原文" in _sem_prompt
    and "未揭示词条用问号保留位置" in _sem_prompt
    and "正负号" in _sem_prompt,
)
check(
    "语义提示词只识图，游戏规则与倍率换算交给代码",
    "不做正负面判断、数值换算、武器分析或配装建议" in _sem_prompt and "x或×倍率" in _sem_prompt,
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


# ★ 2026-10 倾向统一 1.2：近形字兜底的可行性闸门按 1.2 判，报障卡（按倾向 1.4 洗出）
#   换成**按 1.2/1.4 缩放后**的同一张卡（倾向取消后游戏内显示的就是缩放值）。
_incident_vision_12 = {
    "weapon": "盗贼 Visi-toxican",
    "lines": [
        "盗贼 Visi-toxican",
        "🔒 +103.3% 毒素伤害",
        "+257.1% 伤害",
        "+126.9% 多重射击",
        "-83.3% 触发时间",
        "内融值 1,234",
    ],
    "positive": [
        ["基伤", 103.3],
        ["毒素伤害", 257.1],
        ["多重", 126.9],
        ["触发", 83.3],
        ["持续", 83.3],
    ],
    "negative": [["滑暴", 83.3], ["触发时间", 83.3]],
}
# 识别结果里的 weapon 就是线上那条「冰松 Visi-critapha」
_incident_vision_12.update(positive=[], negative=[])
_iceq_vision = dict(_incident_vision_12, weapon="冰松 Visi-critapha")
# 对照：同一张卡但 OCR 读对（纯图路径用）
_iceq_vision_ok = dict(_incident_vision_12, weapon="冰凇 Visi-critapha")


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

# 日志最小化：再次走真实分析入口，玩家说明保留，归一日志不含原名。
from unittest.mock import patch as _log_patch  # noqa: E402
from core.commands import riven as _riven_module  # noqa: E402


class _MinimalLog:
    def __init__(self):
        self.rows = []

    def info(self, fmt, *args, **kwargs):
        self.rows.append(fmt % args if args else fmt)

    warning = error = exception = info


_minlog = _MinimalLog()
with _log_patch.object(_riven_module, "logger", _minlog):
    _result_minlog = asyncio.run(
        _rec_obj(
            dict(
                _iceq_vision_ok,
                weapon_raw="RAW_NAME_PRIVATE",
                _local_ocr={"state": "accepted", "name_normalized": True},
            )
        )._h_riven_analysis(_FatParsed("紫卡分析"), _FakeImageEvent(), "pc")
    )
check(
    "日志最小化：归一说明仍在玩家卡面",
    "RAW_NAME_PRIVATE" in "\n".join(_result_minlog.lines)
    and "唯一数值可行候选" in "\n".join(_result_minlog.lines),
)
check("日志最小化：原始识别名不入日志", "RAW_NAME_PRIVATE" not in "\n".join(_minlog.rows))
check("日志最小化：归一状态已记录", any("类别=唯一数值可行候选" in row for row in _minlog.rows))


# 旧近形字分支同样保留玩家注记，日志只记录采纳状态。
_oldnearlog = _MinimalLog()
with _log_patch.object(_riven_module, "logger", _oldnearlog):
    _oldnearreply = asyncio.run(
        _rec_obj(_iceq_vision)._h_riven_analysis(_FatParsed("紫卡分析"), _FakeImageEvent(), "pc")
    )
check("日志最小化：旧近形分支保留注记", "冰松" in "\n".join(_oldnearreply.lines))
check("日志最小化：旧近形分支不记录原名", "冰松" not in "\n".join(_oldnearlog.rows))
check(
    "日志最小化：旧近形分支状态可排查",
    any("近形字兜底：状态=已采信" in row for row in _oldnearlog.rows),
)


# 行解析未知行仅计数，原行仍留在识别证据中。
_noteslog = _MinimalLog()
_notesvision = dict(_iceq_vision_ok, lines=[*_iceq_vision_ok["lines"], "PRIVATE_UNRECOGNIZED_LINE"])
with _log_patch.object(_riven_module, "logger", _noteslog):
    asyncio.run(
        _rec_obj(_notesvision)._h_riven_analysis(_FatParsed("紫卡分析"), _FakeImageEvent(), "pc")
    )
check("日志最小化：未知行不回显日志", "PRIVATE_UNRECOGNIZED_LINE" not in "\n".join(_noteslog.rows))
check("日志最小化：行解析保留跳过计数", any("跳过 " in row for row in _noteslog.rows))


async def _command_log_case():
    secret = "Authorization: Bearer PRIVATE_TOKEN https://fake.invalid/?signature=SECRET"
    for error in (plugin.WarframeAPIError("请求失败（" + secret + "）"), RuntimeError(secret)):
        obj = plugin.WarframeSDJK.__new__(plugin.WarframeSDJK)
        obj.groups = types.SimpleNamespace(platform=lambda origin: "pc")

        async def fail(*args):
            raise error

        fail.__code__ = fail.__code__.replace(
            co_filename="/private/credential-path/secret-handler.py"
        )

        async def results(event, reply, parsed):
            yield reply

        obj._routes = {"analysis": fail}
        obj._build_results = results
        event = types.SimpleNamespace(
            message_str="紫卡分析", unified_msg_origin="test", stop_event=lambda: None
        )
        log = _MinimalLog()
        with _log_patch.object(plugin, "logger", log):
            replies = [reply async for reply in obj.on_message(event)]
        text = "\n".join(log.rows)
        check(f"日志最小化：命令边界保留{type(error).__name__}", type(error).__name__ in text, text)
        check(
            "日志最小化：命令日志无凭据",
            all(
                x not in text
                for x in ("Authorization", "PRIVATE_TOKEN", "signature=", "SECRET", "fake.invalid")
            ),
            text,
        )
        check("日志最小化：命令仍回复错误", bool(replies and replies[0].raw_text))
        if isinstance(error, RuntimeError):
            check(
                "日志最小化：排障栈只含文件基名",
                "secret-handler.py:" in text and "/private/credential-path" not in text,
                text,
            )


async def _price_log_case():
    from core.commands import market as market_module

    for error in (
        plugin.WarframeAPIError("Authorization: Bearer SECRET"),
        RuntimeError("https://fake.invalid/?signature=SECRET"),
    ):

        async def fail_market(*args):
            raise error

        log = _MinimalLog()
        with (
            _log_patch.object(_riven_module, "logger", log),
            _log_patch.object(market_module, "riven_market_weapon", fail_market),
        ):
            result = await _rec_obj(_iceq_vision_ok)._riven_analysis_price(
                {}, [("damage", 1)], [], "pc"
            )
        text = "\n".join(log.rows)
        check("日志最小化：挂单失败保留UI类别", result["error"] == type(error).__name__)
        check(
            "日志最小化：挂单日志仅错误类型",
            type(error).__name__ in text
            and all(
                x not in text for x in ("Authorization", "SECRET", "signature=", "fake.invalid")
            ),
            text,
        )


asyncio.run(_price_log_case())

asyncio.run(_command_log_case())

# ---------------------------------------------------------------------------
# ⑭ 「多个近形候选都可行」⇒ 不猜：列进候选提示、不出卡
# ---------------------------------------------------------------------------
# 桩数据（名字为合成，只为触发分支）：OCR 读成「七星刃」，表里两个**等长 + 1 字差**
# 的候选（七星刀 / 七星剑）倾向都是 1.4 ⇒ 数值都能解释卡面 ⇒ 必须**不采纳**。
_vision_multi = dict(_incident_vision_12, weapon="七星刃 Seven-star")


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
    "未找到紫卡武器" in _txt(_rE),
    repr(_rE)[:200],
)
check(
    "⑭② 两个可行候选都列进提示",
    "七星刀" in _txt(_rE) and "七星剑" in _txt(_rE),
    _txt(_rE)[:200],
)

# ---------------------------------------------------------------------------
# ⑮ 紫卡识别**不竞速**：只取一个渠道（维护者 2026-10-07 裁示"宁慢不赌"）
# ---------------------------------------------------------------------------
# 2026-10-07 起因：同张「冰凇」卡 × 各 5 次 —— glm-4v-flash → 冰松 5/5（读错且最快
#   ⇒ 竞速里必赢）；30B 2/5；8B 5/5 ⇒ 紫卡路只发一个渠道。识卡那路保留原竞速。
# 2026-10-12 订正选渠道口径：**不再无条件挑 8B**，改为信任 `_vision_providers()`
#   的排序（它已把面板配置的 `vision_provider_id` 排在首位；没配置时才轮到内置
#   候选优先级）。旧实现把面板配置对整个紫卡路**静默失效**（用户实测：配置改 30B、
#   重载两次，紫卡仍打印"固定渠道 8B"）。换 30B 的依据见 vision.py 内注释
#   （bench3：65/70 vs 62/70、perfect 15 vs 12、9.1 s vs 18.3 s）。
#   ⚠ 本组用例只锚定「怎么取渠道」；"配置了 X 就发 X"由 `_vision_providers()` 自身
#   的单测（`test_vision_providers` 若存在）+ 线上日志共同覆盖。


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
    obj.cfg = {}
    stub = _OrderStub([_Prov(i) for i in prov_ids])
    obj._vision_providers = stub._vision_providers
    obj._vision_race_json = stub._vision_race_json
    asyncio.run(obj._extract_riven_from_image("data:image/png;base64,x"))
    return _seen.get("order", [])


check(
    "⑮① 紫卡路只传一个渠道（不竞速）：候选首个 = 配置渠道时，发它且只发它",
    _raced_with(
        [
            "siliconflow/Qwen/Qwen3-VL-30B-A3B-Instruct",
            "zhipu/glm-4v-flash",
            "siliconflow/Qwen/Qwen3-VL-8B-Instruct",
        ]
    )
    == ["siliconflow/Qwen/Qwen3-VL-30B-A3B-Instruct"],
    str(_seen),
)
check(
    "⑮② 候选序为 [glm, 8B] 时照发首位（不再按名字回落 8B——维护者 2026-10-12 裁示去定死）",
    _raced_with(
        [
            "zhipu/glm-4v-flash",
            "siliconflow/Qwen/Qwen3-VL-8B-Instruct",
        ]
    )
    == ["zhipu/glm-4v-flash"],
    str(_seen),
)
check(
    "⑮③ 候选为空：不猜渠道、不发请求（交调用方按无渠道报错）",
    _raced_with([]) == [],
    str(_seen),
)

# ---------------------------------------------------------------------------
# ⑯ ★ 2026-10-14 P0 回归：视觉模型 positive/negative 的**元素形状**漂移
#    —— 线上「紫卡分析 + 截图」连续报「⚠️ 内部错误」（`main.py` 兜底 except）。
#    根因：旧 `_normalize_llm_stats` 用 `to_stat(*item)`，假定每条都是二元序列。
#      · 整条扁平 `["变焦", "60.8"]`：迭代拿到裸字符串，`*item` 按字符拆成
#        4/5/6 个位置参数 ⇒ TypeError ⇒ 整条指令崩（线上 4 次：10-03 缺 val /
#        10-12 5 个 / 10-14 4 个、6 个）。
#      · 元素是字典 `{"name":..,"value":..}`（或中文键 `词条缩写/数值`）：
#        `*item` 解包的是**键** ⇒ to_stat 恒 None ⇒ 词条静默全丢
#        （日志实证：`模型语义表 0 正 0 负`，仅靠卡面逐行那路兜住才没翻车）。
#    修法：容器统一成 [(名, 值)]；未知形状保留占位，避免缩短整卡分母。
#    本组把线上四种形状各冻一条，并守住「正常嵌套不回归」。
# ---------------------------------------------------------------------------
check(
    "⑯⓪ 形状归一化：正常嵌套 [['名', 值], ...] 不受影响（不回归）",
    _S._iter_stat_pairs([["多重射击", "148"], ["毒素伤害", 299.9]])
    == [("多重射击", "148"), ("毒素伤害", 299.9)],
    str(_S._iter_stat_pairs([["多重射击", "148"], ["毒素伤害", 299.9]])),
)
check(
    "⑯① 形状归一化：整条扁平 ['名', 值] → 一条（不再按字符拆爆）",
    _S._iter_stat_pairs(["变焦", "60.8"]) == [("变焦", "60.8")],
    str(_S._iter_stat_pairs(["变焦", "60.8"])),
)
check(
    "⑯② 形状归一化：整条扁平多组 ['名', 值, '名', 值] → 两条",
    _S._iter_stat_pairs(["多重射击", "122.4%", "病毒伤害", "104.3%"])
    == [("多重射击", "122.4%"), ("病毒伤害", "104.3%")],
    str(_S._iter_stat_pairs(["多重射击", "122.4%", "病毒伤害", "104.3%"])),
)
check(
    "⑯③ 形状归一化：字典英文键 {name,value} → 一条",
    _S._iter_stat_pairs([{"name": "冰冻伤害", "value": "+40.7%"}]) == [("冰冻伤害", "+40.7%")],
    str(_S._iter_stat_pairs([{"name": "冰冻伤害", "value": "+40.7%"}])),
)
check(
    "⑯④ 形状归一化：字典中文键 {词条缩写,数值} → 一条",
    _S._iter_stat_pairs([{"词条缩写": "病毒伤害", "数值": "104.3%"}]) == [("病毒伤害", "104.3%")],
    str(_S._iter_stat_pairs([{"词条缩写": "病毒伤害", "数值": "104.3%"}])),
)
check(
    "⑯⑤ 形状归一化：坏行与奇数扁平项保留未知占位，绝不 *item 解包",
    _S._iter_stat_pairs(["火"]) == [(None, None)]
    and _S._iter_stat_pairs(["a", "b", "c"]) == [("a", "b"), (None, None)]
    and _S._iter_stat_pairs(None) == []
    and _S._iter_stat_pairs([["a", "b", "c"]]) == [(None, None)],
    f"{_S._iter_stat_pairs(['火'])} / {_S._iter_stat_pairs(['a', 'b', 'c'])}",
)
check(
    "⑯⑥ 形状归一化：混入裸字符串不崩、其余条目照收",
    _S._iter_stat_pairs([["多重射击", "148"], "火", ["伤害", "196.5"]])
    == [("多重射击", "148"), (None, None), ("伤害", "196.5")],
    str(_S._iter_stat_pairs([["多重射击", "148"], "火", ["伤害", "196.5"]])),
)

# 端到端：线上那两张崩掉的卡，现在必须走通（不再「内部错误」）
_flat_1645 = {
    "weapon": "托里德 Sati-croniata",
    "positive": [["射速", "73.9"], ["多重射击", "111.4"], ["伤害", "196.5"]],
    "negative": ["变焦", "60.8"],
}
_pf, _nf = _norm(_flat_1645, _rev)
check(
    "⑯⑦ 线上 10-14 16:45 卡（negative 扁平）：不崩，且负词条 变焦 60.8 收进 negative",
    _nf == [("zoom", 60.8)] and len(_pf) == 3,
    f"{_pf} / {_nf}",
)
_flat_1708 = {
    "weapon": "盗贼 Sati-igniata",
    "positive": [["火焰伤害", "124.2%"], ["多重射击", "166.4%"], ["变焦", "-80.5%"]],
    "negative": ["伤害", "297.3%"],
}
_pf2, _nf2 = _norm(_flat_1708, _rev)
check(
    "⑯⑧ 线上 10-14 17:08 卡（negative 扁平 name+值）：不崩，四条词条齐全",
    [s for s, _ in _pf2] == ["heat_damage", "multishot"]
    and sorted(s for s, _ in _nf2) == ["melee_damage", "zoom"],
    f"{_pf2} / {_nf2}",
)
_dict_1708 = {
    "weapon": "奏凯 Feva-gelidex",
    "positive": [
        {"name": "冰冻伤害", "value": "+40.7%"},
        {"name": "装填速度", "value": "+23.9%"},
        {"name": "触发几率", "value": "+39.8%"},
    ],
    "negative": [{"name": "暴击几率", "value": "-55.2%"}],
}
_pd, _nd = _norm(_dict_1708, _rev)
check(
    "⑯⑨ 线上 10-14 17:08 字典形态卡：不再「语义表 0 正 0 负」（词条全丢）",
    [s for s, _ in _pd] == ["cold_damage", "reload_speed", "status_chance"]
    and _nd == [("crit_chance", 55.2)],
    f"{_pd} / {_nd}",
)

# ---------------------------------------------------------------------------
# 用户实卡（2026-10-21，食人女魔 Parci-argiada）：长词条「收起武器时弹匣每秒自动装填」
#   数值写在名字后面、卡面还折了行。旧实现整条静默丢掉 ⇒ 按 2+1 系数算（实为 3+1）；
#   即便值在前也会因「每秒」的「秒」被当单位剥掉而认成弹匣容量。
# ---------------------------------------------------------------------------
_ogre_rev = {v: k for k, v in RIVEN_STAT_ZH.items()}
_ogre_cases = {
    "卡面折行": [
        "食人女魔 Parci-argiada",
        "x1.53 对 Grineer 的伤害",
        "🔒 收起武器时弹匣每秒自",
        "动装填 +112.9 %",
        "x1.5 对 Infested 的伤害",
        "-59.3% 射速（弓类武器效果加倍）",
    ],
    "值在后整行": ["收起武器时弹匣每秒自动装填 +112.9 %", "-59.3% 射速"],
    "值在前": ["+112.9% 收起武器时弹匣每秒自动装填", "-59.3% 射速"],
}
for _k, _L in _ogre_cases.items():
    _po, _ne, _no = RA.parse_riven_lines(
        _L, lambda nm: plugin.WarframeSDJK._stat_id_from_name(nm, _ogre_rev)
    )
    check(
        f"★ 实卡长词条（{_k}）：收枪装填 112.9 不丢、不认成弹匣容量",
        ("reload_holstered", 112.9) in _po
        and not any(sid == "magazine_capacity" for sid, _v in _po)
        and ("fire_rate", 59.3) in _ne,
        f"{_po} / {_ne} / {_no}",
    )
check(
    "★ 实卡整卡：3 正 1 负（派系×2 + 收枪装填 / 射速）",
    len(
        RA.parse_riven_lines(
            _ogre_cases["卡面折行"],
            lambda nm: plugin.WarframeSDJK._stat_id_from_name(nm, _ogre_rev),
        )[0]
    )
    == 3,
)
check(
    "折行拼接不误伤：武器名行（带拉丁自命名）不拼进词条",
    RA.merge_polarity_lines(["食人女魔 Parci-argiada", "+10% 射速"])
    == ["食人女魔 Parci-argiada", "+10% 射速"],
)
check(
    "「米/秒」单位仍照剥（两端）：「+12.5秒 连击持续时间」名字不带秒",
    RA._strip_name("12.5秒 连击持续时间") == "连击持续时间",
    RA._strip_name("12.5秒 连击持续时间"),
)

# ★ 2026-10-25（Astra 评估 S5）：多图消息第一张合格就停，不再把每张都下载转换
_image_reads = {"n": 0}


class Image:  # 名字必须是 Image（_image_data_urls 按类型名认图片组件）
    url = ""

    @property
    def file(self):
        _image_reads["n"] += 1
        return "base64://QUFB"

    async def convert_to_base64(self):
        raise AssertionError("禁止回退宿主无界转换")


_obj_img = plugin.WarframeSDJK.__new__(plugin.WarframeSDJK)
_obj_img._image_pixels_ok = lambda _b64: True
_ev_img = type(
    "E", (), {"message_obj": type("M", (), {"message": [Image() for _ in range(5)]})()}
)()
_urls = asyncio.run(_obj_img._image_data_urls(_ev_img))
check(
    "★ 5 张图只读取第一张来源、只返回 1 张",
    _image_reads["n"] == 1 and _urls == ["data:image/png;base64,QUFB"],
    f"{_image_reads} {len(_urls)}",
)


# 完整近名优先于短词包含；近距歧义按 SID 合并后再判，未知括号不裁头。
_lookup = plugin.WarframeSDJK._stat_id_from_name
for _name, _sid in (
    ("暴击", "crit_chance"),
    ("暴击伤害", "crit_damage"),
    ("滑行暴击几率", "slide_crit"),
    ("暴击伤售", "crit_damage"),
    ("触发几", "status_chance"),
    ("滑行暴击几", "slide_crit"),
    ("超长前缀暴击伤害说明后缀", "crit_damage"),
    ("暴击几率（重击时x2）", "crit_chance"),
    ("射速(弓类武器效果加倍)", "fire_rate"),
    ("暴击几率（重吉时x2）", "crit_chance"),
    ("射速(弓类武器效果加陪)", "fire_rate"),
):
    check(f"完整词名安全归一：{_name}", _lookup(_name, _rev) == _sid)
for _name in (
    "暴击伤率",
    "滑行暴击伤害",
    "武器伤害",
    "天地玄黄宇宙",
    "暴击伤害和多重射击",
    "暴击几率（未知说明）",
    "暴击几率（重击时x2",
    "暴击几率（重击时(x2)）",
    "暴击伤害（重击时x2）",
    "暴击伤售（重击时x2）",
    "暴击几率（重击时x3）",
    "暴击几率（重击时y2）",
    "暴击几率（重击时x）",
    "暴击几率（未知时x2）",
):
    check(f"歧义或未知结构不归成短词：{_name}", _lookup(_name, _rev) is None)

_saved_alias_full = plugin.WarframeSDJK._STAT_ALIAS_FULL
try:
    plugin.WarframeSDJK._STAT_ALIAS_FULL = {
        "甲乙丙丁": "sid_a",
        "甲乙丙戊": "sid_a",
        "甲乙丙庚": "sid_b",
    }
    check("同SID多个近名不能掩盖跨SID同距", _lookup("甲乙丙己", {}) is None)
    plugin.WarframeSDJK._STAT_ALIAS_FULL.pop("甲乙丙庚")
    check("多个近名仅同SID时可归一", _lookup("甲乙丙己", {}) == "sid_a")
finally:
    plugin.WarframeSDJK._STAT_ALIAS_FULL = _saved_alias_full

for _line in (
    "+88% 暴击几率（未知说明）",
    "+88% 暴击几率（重击时x2",
    "+88% 暴击伤害（重击时x2）",
    "+88% 暴击伤售（重击时x2）",
    "+暴击几率（重击时x2）",
):
    _bp, _bn, _bnotes = RA.parse_riven_lines([_line], _resolve)
    check(f"真实行解析拒绝未知括号或括号数字补值：{_line}", not _bp and not _bn and bool(_bnotes))


class _RecordedCardProvider:
    """只返回本地固定原文；走真实提取/解析/handler，不发任何模型请求。"""

    def __init__(self, payload, line_text=""):
        self.raw_json = "  \n" + json.dumps(payload, ensure_ascii=False) + "\n  "
        self.line_text = line_text

    def meta(self):
        return types.SimpleNamespace(id="offline/recorded-card")

    async def text_chat(self, prompt, **kwargs):
        text = self.line_text if prompt == plugin.WarframeSDJK._RIVEN_LINE_PROMPT else self.raw_json
        return types.SimpleNamespace(completion_text=text)


async def _cloud_handler(payload, line_text="", *, client=None, recheck_text=None):
    provider = _RecordedCardProvider(payload, line_text)
    obj = plugin.WarframeSDJK.__new__(plugin.WarframeSDJK)
    obj.cfg = {"vision_routes_isolated": True, "scan_cooldown": 0}
    obj.client = client or _FakeRivenClient()
    obj.page_size = 12
    obj._image_data_urls = _fake_imgs
    obj._riven_vision_providers = lambda: [provider]
    obj._line_vision_providers = lambda: [provider]
    if recheck_text is not None:
        recheck = _RecordedCardProvider({}, recheck_text)
        recheck.meta = lambda: types.SimpleNamespace(id="offline/recheck")
        obj.cfg["vision_provider_id_line_recheck"] = "offline/recheck"
        obj._try_get_provider = lambda pid: recheck if pid == "offline/recheck" else None
    captured = {"price_calls": 0}

    async def capture_image(url):
        result = await plugin.WarframeSDJK._extract_riven_from_image(obj, url)
        captured["parsed"] = result
        return result

    async def no_price(*args, **kwargs):
        captured["price_calls"] += 1
        return None

    obj._extract_riven_from_image = capture_image
    obj._riven_analysis_price = no_price
    reply = await obj._h_riven_analysis(_FatParsed("紫卡分析"), _FakeImageEvent(), "pc")
    return reply, captured, provider


_cloud_complete = {
    "weapon": "盗贼",
    "positive": [["暴击伤售", 108.2], ["多重射击", 141.7], ["毒素伤害", 98.2]],
    "negative": [["切割伤害", 99.9]],
}
_cloud_reply, _cloud_saved, _cloud_provider = asyncio.run(
    _cloud_handler(
        _cloud_complete, "+108.2% 暴击伤售\n+141.7% 多重射击\n+98.2% 毒素伤害\n-99.9% 切割伤害"
    )
)
check(
    "真实JSON整卡handler：暴伤近名不被短暴击抢走且原数值不变",
    bool(_cloud_reply.lines)
    and not _cloud_reply.raw_text
    and "▲暴伤 +108.2%" in "\n".join(_cloud_reply.lines)
    and "▲暴击 +108.2%" not in "\n".join(_cloud_reply.lines),
    _txt(_cloud_reply),
)
check(
    "成功分析披露代码近名归一的原词与使用词条",
    "词名按代码归一" in _txt(_cloud_reply) and "暴击伤售 → 暴伤" in _txt(_cloud_reply),
    _txt(_cloud_reply),
)
_recognition = _cloud_saved["parsed"]["_recognition"]
check(
    "主读追溯：完整原文、原解析结构、实际提示词hash与渠道保留",
    _recognition["raw_text"] == _cloud_provider.raw_json
    and _recognition["parsed"] == _cloud_complete
    and _recognition["provider_id"] == "offline/recorded-card"
    and _recognition["prompt_hash"] == sha256(_sem_prompt.encode()).hexdigest(),
)
check(
    "归一追溯：近名决策和数值原文可区分",
    _recognition["stat_normalization"][0]["raw_name"] == "暴击伤售"
    and _recognition["stat_normalization"][0]["raw_value"] == 108.2
    and _recognition["stat_normalization"][0]["name_match"]["method"] == "one_edit"
    and _recognition["stat_normalization"][0]["sid"] == "crit_damage",
)

for _label, _unknown in (
    ("裸null条目", None),
    ("单字段行", ["暴击伤害"]),
    ("多字段行", ["暴击伤害", 108.2, "extra"]),
    ("二元行null值", ["暴击伤害", None]),
    ("二元行null名", [None, 108.2]),
    ("字典行null值", {"name": "暴击伤害", "value": None}),
    ("字典行null名", {"name": None, "value": 108.2}),
    ("bool真值", ["暴击伤害", True]),
    ("bool假值", ["暴击伤害", False]),
    ("NaN", ["暴击伤害", float("nan")]),
    ("正无穷", ["暴击伤害", float("inf")]),
    ("负无穷", ["暴击伤害", float("-inf")]),
    ("跨SID歧义", ["暴击伤率", 108.2]),
    ("未知括号", ["暴击伤害（未知说明）", 108.2]),
):
    _payload = json.loads(json.dumps(_cloud_complete))
    _payload["positive"][0] = _unknown
    _pending, _saved, _ = asyncio.run(_cloud_handler(_payload))
    check(
        f"真实JSON整卡handler {_label}：保留原值待核对，不按剩余2+1分析/估价",
        bool(_pending.raw_text)
        and "待核对" in _pending.raw_text
        and "原始识别结构" in _pending.raw_text
        and _pending.riven_splice is None
        and _saved["price_calls"] == 0,
        _txt(_pending),
    )
    check(
        f"真实JSON整卡handler {_label}：原始分母仍为4行",
        sum(
            len(plugin.WarframeSDJK._iter_stat_pairs(_saved["parsed"].get(k)))
            for k in ("positive", "negative")
        )
        == 4,
    )

for _bucket in ("positive", "negative"):
    for _value in (None, False, "未确认的整桶原文"):
        _bucket_payload = json.loads(json.dumps(_cloud_complete))
        _bucket_payload[_bucket] = _value
        _bucket_reply, _bucket_saved, _ = asyncio.run(_cloud_handler(_bucket_payload))
        check(
            f"显式整桶{_bucket}={_value!r}：真实handler待核对，不按剩余合法条目分析",
            "待核对" in (_bucket_reply.raw_text or "")
            and json.dumps({_bucket: _value}, ensure_ascii=False)[1:-1] in _bucket_reply.raw_text
            and not _bucket_reply.lines
            and _bucket_reply.riven_splice is None
            and _bucket_saved["price_calls"] == 0,
            _txt(_bucket_reply),
        )

_empty_reply, _empty_saved, _ = asyncio.run(
    _cloud_handler({"weapon": "盗贼", "positive": [], "negative": []})
)
check(
    "空词条整卡明确待核对且不估价",
    "待核对" in (_empty_reply.raw_text or "") and _empty_saved["price_calls"] == 0,
)

_with_null = json.loads(json.dumps(_cloud_complete))
_with_null["positive"][0] = ["暴击伤害", None]
_full_line_text = "+108.2% 暴击伤害\n+141.7% 多重射击\n+98.2% 毒素伤害\n-99.9% 切割伤害"
_recovered, _recovered_saved, _ = asyncio.run(_cloud_handler(_with_null, _full_line_text))
check(
    "独立合法4行能补齐null语义行并正常分析",
    not _recovered.raw_text
    and "▲暴伤 +108.2%" in "\n".join(_recovered.lines)
    and "卡面逐行" in _recovered.footer,
    _txt(_recovered),
)
check(
    "窄读追溯：原始逐行响应与主读null结构同时保留",
    _recovered_saved["parsed"]["_recognition"]["line_read"]["raw_text"] == _full_line_text
    and _recovered_saved["parsed"]["_recognition"]["parsed"]["positive"][0][1] is None,
)
_near_line_reply, _near_line_saved, _ = asyncio.run(
    _cloud_handler(_with_null, _full_line_text.replace("暴击伤害", "暴击伤售"))
)
check(
    "云行读采用近名时也披露代码归一且保留逐行决策",
    not _near_line_reply.raw_text
    and "暴击伤售 → 暴伤" in _txt(_near_line_reply)
    and _near_line_saved["parsed"]["_recognition"]["selected_stat_source"] == "line_read"
    and _near_line_saved["parsed"]["_recognition"]["line_stat_normalization"][0]["method"]
    == "one_edit",
    _txt(_near_line_reply),
)
_short_lines = "+141.7% 多重射击\n+98.2% 毒素伤害\n-99.9% 切割伤害"
_short_reply, _short_saved, _ = asyncio.run(_cloud_handler(_with_null, _short_lines))
check(
    "独立合法3行不能覆盖原语义4行分母",
    "待核对" in (_short_reply.raw_text or "") and _short_saved["price_calls"] == 0,
    _txt(_short_reply),
)

_decimal_payload = json.loads(json.dumps(_cloud_complete))
_decimal_payload["positive"][0] = ["暴击伤害", 1082]
_decimal_reply, _decimal_saved, _ = asyncio.run(_cloud_handler(_decimal_payload))
check(
    "图片小数点推断仅给原值→候选待确认，不写回分析/估价",
    "待核对" in (_decimal_reply.raw_text or "")
    and "1082" in _decimal_reply.raw_text
    and "108.2" in _decimal_reply.raw_text
    and "代码推断候选" in _decimal_reply.raw_text
    and _decimal_saved["parsed"]["positive"][0][1] == 1082
    and _decimal_saved["price_calls"] == 0
    and _decimal_reply.riven_splice is None,
    _txt(_decimal_reply),
)

_text_obj = plugin.WarframeSDJK.__new__(plugin.WarframeSDJK)
_text_obj.client = _FakeRivenClient()
_text_obj.page_size = 12
_text_reply = asyncio.run(
    _text_obj._h_riven_analysis(
        _FatParsed("紫卡分析 盗贼 暴伤1082 多重141.7 毒伤98.2 负切割99.9"),
        _FakeImageEvent(),
        "pc",
    )
)
check(
    "纯文字小数点既有路径保留",
    not _text_reply.raw_text and "▲暴伤 +108.2%" in "\n".join(_text_reply.lines),
    _txt(_text_reply),
)


# 通用协议回归：格式变换不补字、不补数位；冲突与漏行经真实handler验证。
for _prefix in ("", "100,200,20,200,0,", "1. ", "第一行："):
    _wrapped = "\n".join(_prefix + ln for ln in _full_line_text.splitlines())
    _r, _c, _p = asyncio.run(_cloud_handler(_with_null, _wrapped))
    check(
        f"完整词条包装可恢复，原数值不变：{_prefix!r}", bool(_r.lines) and not _r.raw_text, _txt(_r)
    )
    check(
        f"包装回执原文独立留存：{_prefix!r}",
        _c["parsed"]["_recognition"]["line_read"]["raw_text"] == _wrapped,
    )

_annotation = plugin.WarframeSDJK._parse_riven_lines_text(
    "+88.4% 暴击几率（重击时\nx2）\n+44.2% 暴击伤害"
)
_ap, _an, _al, _notes = plugin.WarframeSDJK._riven_lines_legal(_annotation["lines"])
check("已知尾注明确折行可绑定，括注数字不替代主体", _al and ("crit_chance", 88.4) in _ap)
for _badline in (";53% 未确认伤害", "44.2% 暴击伤害", "暴击伤害", "+44.2% 暴击伤害（未知尾注）"):
    _r, _c, _ = asyncio.run(_cloud_handler(_cloud_complete, _full_line_text + "\n" + _badline))
    check(
        f"疑似漏行原文保留并阻分析：{_badline}",
        "待核对" in _txt(_r) and _badline in _txt(_r) and not _r.lines and _c["price_calls"] == 0,
        _txt(_r),
    )

_r, _c, _ = asyncio.run(_cloud_handler(_cloud_complete, _full_line_text.replace("108.2", "109.2")))
check("同SID异值不任意择路", "待核对" in _txt(_r) and not _r.lines and _c["price_calls"] == 0)
_r, _c, _ = asyncio.run(
    _cloud_handler(_cloud_complete, _full_line_text.replace("-99.9%", "+99.9%"))
)
check("同SID极性冲突不任意择路", "待核对" in _txt(_r) and not _r.lines)
# 各路单独都合法且只有一方多读一条，采用现成超集，不用子集清除完整性。
_r, _c, _ = asyncio.run(_cloud_handler(_cloud_complete, _short_lines))
check(
    "合法完整语义超集保留，短窄读不抹词条",
    bool(_r.lines) and "▲暴伤 +108.2%" in "\n".join(_r.lines),
    _txt(_r),
)
_other = dict(
    _cloud_complete, positive=[["火焰伤害", 108.2], ["多重射击", 141.7], ["毒素伤害", 98.2]]
)
_r, _c, _ = asyncio.run(_cloud_handler(_other, _full_line_text))
check("两路不相容词条不拼并集", "待核对" in _txt(_r) and not _r.lines)
_duplicate = _full_line_text + "\n+108.2% 暴击伤害"
_r, _c, _ = asyncio.run(_cloud_handler(_cloud_complete, _duplicate))
check("重复行不因去重而伪装完整", "待核对" in _txt(_r) and not _r.lines)

_precise, _captured, _ = asyncio.run(
    _cloud_handler(_cloud_complete, _full_line_text.replace("108.2", "108.2000001"))
)
check("不同十进制读数不被浮点舍入抹平冲突", "待核对" in _txt(_precise) and not _precise.lines)
_spurious = plugin.WarframeSDJK._parse_riven_lines_text("+12%\n34% 暴击伤害\n+44% 多重射击")
check("纯数值不借下一条有数字的词条名称", not _spurious["_line_legal"])
_bare, _captured, _ = asyncio.run(_cloud_handler(_cloud_complete, _full_line_text + "\n77.1"))
check("词条区内孤立数字不是结构底栏", "待核对" in _txt(_bare) and not _bare.lines)

_forensic, _c, _ = asyncio.run(
    _cloud_handler(_cloud_complete, _full_line_text + "\n```+77.1% 未确认词条")
)
check("带载荷的围栏不静默当结构行", "待核对" in _txt(_forensic) and "```+77.1%" in _txt(_forensic))
_longline, _c, _ = asyncio.run(
    _cloud_handler(_cloud_complete, _full_line_text.replace("108.2", "108.2000000000000000000001"))
)
check("浮点不能表示的原文末位保留未知", "待核对" in _txt(_longline) and not _longline.lines)
_longjson = dict(
    _cloud_complete,
    positive=[["暴击伤害", "108.2000000000000000000001"], ["多重射击", 141.7], ["毒素伤害", 98.2]],
)
_longreply, _c, _ = asyncio.run(_cloud_handler(_longjson, _full_line_text))
check(
    "JSON原数字串先检查保真再归一，不被完整行清除未知",
    "待核对" in _txt(_longreply) and not _longreply.lines,
)
from core import loadout_ocr as _lo_lexical  # noqa: E402

_lexical = _lo_lexical.parse_vision_json(
    '{"positive":[["伤害",108.2000000000000000000001]]}', preserve_numbers=True
)
check("JSON数值字面末位独立留存", _lexical["positive"][0][1] == "108.2000000000000000000001")

_unresolved_payload = dict(
    _cloud_complete, positive=[["未确认名称", 108.2], ["多重射击", 141.7], ["毒素伤害", 98.2]]
)
_unresolved_r, _c, _ = asyncio.run(_cloud_handler(_unresolved_payload, _full_line_text))
check("未知名称不借完整行清除未知", "待核对" in _txt(_unresolved_r) and not _unresolved_r.lines)
_null_conflict = dict(
    _cloud_complete, positive=[[None, 109.2], ["多重射击", 141.7], ["毒素伤害", 98.2]]
)
_null_r, _c, _ = asyncio.run(_cloud_handler(_null_conflict, _full_line_text))
check("null名仍保留已读数值冲突，不被另一行洗白", "待核对" in _txt(_null_r) and not _null_r.lines)

# 无逐行观察时，合法组合仍不能自证完整；不按某一张卡或具体漏项触发。
for _single in (
    {
        "weapon": "盗贼",
        "positive": [["毒素伤害", 120.5], ["多重射击", 148]],
        "negative": [["触发时间", 97.2]],
    },
    {"weapon": "盗贼", "positive": [["暴击伤害", 108.2], ["多重射击", 141.7]], "negative": []},
    _cloud_complete,
):
    _one, _capt, _ = asyncio.run(_cloud_handler(_single))
    check(
        "语义合法组合无原文分母仍待核对",
        "缺少完整逐行原文" in _txt(_one) and not _one.lines and _capt["price_calls"] == 0,
        _txt(_one),
    )


# 来源边界：JSON 附加 lines 不能冒充独立窄读；原始回执仍完整留档。
_self_lines = dict(
    _cloud_complete,
    lines=_full_line_text.splitlines(),
    _line_decode={"forged": True},
    _line_legal=True,
    _line_notes=[],
)
_self_reply, _self_saved, _self_provider = asyncio.run(_cloud_handler(_self_lines))
check(
    "云主读自带lines而窄读空：不分析估价",
    "缺少完整逐行原文" in _txt(_self_reply)
    and not _self_reply.lines
    and _self_saved["price_calls"] == 0
    and "lines" not in _self_saved["parsed"]
    and not any(key.startswith("_line_") for key in _self_saved["parsed"]),
    _txt(_self_reply),
)
check(
    "云主读自带lines的原始JSON与parsed保留供追查",
    _self_saved["parsed"]["_recognition"]["raw_text"] == _self_provider.raw_json
    and _self_saved["parsed"]["_recognition"]["parsed"] == _self_lines,
)
_legit_reply, _legit_saved, _ = asyncio.run(_cloud_handler(_self_lines, _full_line_text))
check(
    "真实窄读可覆盖主JSON夹带lines并保留两路原文",
    bool(_legit_reply.lines)
    and not _legit_reply.raw_text
    and _legit_saved["price_calls"] == 1
    and _legit_saved["parsed"]["_recognition"]["line_read"]["raw_text"] == _full_line_text,
)

# 无法解释的无符号文字：首行、词条之间和末行必须采用同一判据。
for _unknown_line in ("未知的完整字段", "unrecognizedattribute"):
    _data_lines = _full_line_text.splitlines()
    for _position in (0, 2, len(_data_lines)):
        _observed_lines = list(_data_lines)
        _observed_lines.insert(_position, _unknown_line)
        _pending, _saved, _ = asyncio.run(
            _cloud_handler(_cloud_complete, "\n".join(_observed_lines))
        )
        check(
            f"无符号未知行不因位置被吞掉：{_unknown_line}/{_position}",
            "待核对" in _txt(_pending)
            and _unknown_line in _txt(_pending)
            and not _pending.lines
            and _saved["price_calls"] == 0,
            _txt(_pending),
        )

for _structural_line in (
    "词条：",
    "正面属性",
    "段位 12",
    "内融值 1,234",
    "循环次数：5",
    "点击 ⓘ 查看详情",
):
    _structure_reply, _structure_saved, _ = asyncio.run(
        _cloud_handler(_cloud_complete, _full_line_text + "\n" + _structural_line)
    )
    check(
        f"明确栏目或数值元数据不误杀：{_structural_line}",
        bool(_structure_reply.lines)
        and not _structure_reply.raw_text
        and _structure_saved["price_calls"] == 1,
        _txt(_structure_reply),
    )
for _damaged_structure in ("段位 未读清", "内融值 未知字段", "循环次数：未确认"):
    _structure_reply, _structure_saved, _ = asyncio.run(
        _cloud_handler(_cloud_complete, _full_line_text + "\n" + _damaged_structure)
    )
    check(
        f"结构标签后的未知值仍待核对：{_damaged_structure}",
        "待核对" in _txt(_structure_reply) and _structure_saved["price_calls"] == 0,
        _txt(_structure_reply),
    )
for _title_position, _allow in ((0, True), (4, False)):
    _title_lines = _full_line_text.splitlines()
    _title_lines.insert(_title_position, "样例武器 Rava-testa")
    _title_reply, _title_saved, _ = asyncio.run(
        _cloud_handler(_cloud_complete, "\n".join(_title_lines))
    )
    check(
        f"标题需符合语法且处于数据前：{_title_position}",
        bool(_title_reply.lines) == _allow and _title_saved["price_calls"] == int(_allow),
        _txt(_title_reply),
    )


class _OfflineAcceptedWorker:
    """只提供既有 worker 协议结果；不启动进程、OCR 或模型。"""

    @staticmethod
    def status():
        return {"enabled": True, "ok": True}

    @staticmethod
    async def recognize(image_bytes):
        assert image_bytes == b"AAA"
        return {
            "state": "accepted",
            "structured": dict(_cloud_complete, lines=_full_line_text.splitlines()),
        }


async def _local_worker_handler():
    obj = plugin.WarframeSDJK.__new__(plugin.WarframeSDJK)
    obj.cfg = {"vision_routes_isolated": True, "scan_cooldown": 0}
    obj.client = _FakeRivenClient()
    obj.page_size = 12
    obj.local_ocr = _OfflineAcceptedWorker()
    captured = {"price_calls": 0}

    async def image_urls(event):
        return ["data:image/png;base64,QUFB"]

    async def no_price(*args, **kwargs):
        captured["price_calls"] += 1
        return None

    obj._image_data_urls = image_urls
    obj._riven_analysis_price = no_price
    reply = await obj._h_riven_analysis(_FatParsed("紫卡分析"), _FakeImageEvent(), "pc")
    return reply, captured


_local_reply, _local_saved = asyncio.run(_local_worker_handler())
check(
    "本地已接受worker的真实structured.lines保留分析路径",
    bool(_local_reply.lines) and not _local_reply.raw_text and _local_saved["price_calls"] == 1,
    _txt(_local_reply),
)


# 结构修订：页脚只借明确数值元数据锚点；不凭数值或截图位置猜字段。
for _anchor in ("段位 12", "内融值 1,234", "循环次数：5"):
    _footer_reply, _footer_saved, _ = asyncio.run(
        _cloud_handler(_cloud_complete, _full_line_text + "\n" + _anchor + "\n37\n8")
    )
    check(
        f"已有词条和明确元数据锚点后的纯整数页脚保留：{_anchor}",
        bool(_footer_reply.lines)
        and not _footer_reply.raw_text
        and _footer_saved["price_calls"] == 1,
        _txt(_footer_reply),
    )
for _unanchored in (
    "37\n" + _full_line_text,
    _full_line_text + "\n37",
    _full_line_text.replace("\n", "\n37\n", 1),
    _full_line_text.replace("\n", "\n段位 12\n37\n", 1),
    "段位 12\n37\n" + _full_line_text,
    _full_line_text + "\n段位 12\n37.5",
    _full_line_text + "\n段位 12\n37%",
    _full_line_text + "\n段位 12\n+37",
):
    _footer_reply, _footer_saved, _ = asyncio.run(_cloud_handler(_cloud_complete, _unanchored))
    check(
        "无锚/数据区/未读词条前/带量纲数值不能冒充整数页脚",
        "待核对" in _txt(_footer_reply)
        and not _footer_reply.lines
        and _footer_saved["price_calls"] == 0,
        _txt(_footer_reply),
    )
_reset_rows = _full_line_text.splitlines()
_reset_rows.insert(-1, "段位 12")
_reset_rows.append("37")
_reset_reply, _reset_saved, _ = asyncio.run(_cloud_handler(_cloud_complete, "\n".join(_reset_rows)))
check(
    "元数据后重新出现词条会清除页脚锚点",
    "待核对" in _txt(_reset_reply) and _reset_saved["price_calls"] == 0,
    _txt(_reset_reply),
)

# 标题只做语法解包与已有字符拼接，不加入任何武器/词条答案。
for _title in ("样例武器Rava-testa", "样例武器Rava-\ntesta", "样例武器 Rava-\ntesta"):
    _title_reply, _title_saved, _ = asyncio.run(
        _cloud_handler(_cloud_complete, _title + "\n" + _full_line_text)
    )
    check(
        f"数据前的粘连或显式连字符折行标题可机械恢复：{_title!r}",
        bool(_title_reply.lines) and not _title_reply.raw_text and _title_saved["price_calls"] == 1,
        _txt(_title_reply),
    )
_title_raw = ["样例武器Rava-", "testa", *_full_line_text.splitlines()]
_title_decoded = RA.decode_riven_lines(_title_raw)
check(
    "机械连接标题保留两条原文、行号和操作，不插入缺失字符",
    _title_decoded["rows"][0]["text"] == "样例武器Rava-testa"
    and _title_decoded["rows"][0]["source_lines"] == [1, 2]
    and _title_decoded["rows"][0]["raw_lines"] == _title_raw[:2]
    and "join_existing_hyphenated_title" in _title_decoded["rows"][0]["operations"],
    str(_title_decoded),
)
for _unknown_structure in (
    "未知首行",
    "unrecognizedattribute",
    "未知首行\nunrecognizedattribute",
    "样例武器Rava-",
    "100,200,20,200,0",
    "所有",
    "等级",
    "9R",
):
    for _before_data in (True, False):
        _observed = (
            _unknown_structure + "\n" + _full_line_text
            if _before_data
            else _full_line_text + "\n" + _unknown_structure
        )
        _struct_reply, _struct_saved, _ = asyncio.run(_cloud_handler(_cloud_complete, _observed))
        check(
            f"未知首尾/残缺封装不因结构修订放行：{_unknown_structure!r}/{_before_data}",
            "待核对" in _txt(_struct_reply)
            and not _struct_reply.lines
            and _struct_saved["price_calls"] == 0,
            _txt(_struct_reply),
        )
_late_title = _full_line_text + "\n样例武器Rava-\ntesta"
_late_reply, _late_saved, _ = asyncio.run(_cloud_handler(_cloud_complete, _late_title))
check(
    "数据后的连字符折行不能冒充标题",
    "待核对" in _txt(_late_reply) and _late_saved["price_calls"] == 0,
    _txt(_late_reply),
)


# 行末乘数能进入既有无行首符号的派系解析分支，也必须中断页脚状态。
for _faction_position in ("before_integer", "after_integer"):
    _rows = ["+88.4% 暴击几率", "段位 12"]
    _rows += (
        ["对 Corpus 的伤害 x1.51", "37"]
        if _faction_position == "before_integer"
        else ["37", "对 Corpus 的伤害 x1.51"]
    )
    _fp, _fn, _fnotes = RA.parse_riven_lines(_rows, _resolve)
    check(
        f"已有行末乘数词条不让整数落在伪页脚：{_faction_position}",
        any(sid == "damage_vs_corpus" for sid, value in _fp) and "疑似词条未解析：37" in _fnotes,
        f"{_fp} / {_fn} / {_fnotes}",
    )
_fp, _fn, _fnotes = RA.parse_riven_lines(
    ["+88.4% 暴击几率", "对 Corpus 的伤害 x1.51", "段位 12", "37"], _resolve
)
check(
    "行末乘数词条结束后新元数据锚点仍可确认整数页脚",
    any(sid == "damage_vs_corpus" for sid, value in _fp)
    and "无极性符号：37" in _fnotes
    and not any(note.startswith("疑似词条未解析") for note in _fnotes),
    f"{_fp} / {_fn} / {_fnotes}",
)

# 单卡字段角色只使用观察框架，不按某个容量/卡名/答案豁免。
for _header_value in ("7", "29", "62"):
    _framed = _header_value + "\n样例武器 Rava-testa\n" + _full_line_text + "\n段位 6\n103"
    _role_reply, _role_saved, _ = asyncio.run(_cloud_handler(_cloud_complete, _framed))
    check(
        "单卡标题+词条+明确底栏可解释头部整数：" + _header_value,
        bool(_role_reply.lines) and _role_saved["price_calls"] == 1,
        _txt(_role_reply),
    )
    _roles = _role_saved["parsed"]["_line_decode"]["rows"]
    check(
        "头部数值角色保留源行且不猜具体字段：" + _header_value,
        _roles[0].get("structural") == "card_header_integer"
        and _roles[0]["raw_lines"] == [_header_value]
        and _roles[0]["semantic_field"] is None,
    )
for _bad_prefix in ("7%", "+7", "7.5", "7\n29", "7\n未知字段"):
    _bad_frame = _bad_prefix + "\n样例武器 Rava-testa\n" + _full_line_text + "\n段位 6"
    _r, _c, _ = asyncio.run(_cloud_handler(_cloud_complete, _bad_frame))
    check(
        "损坏数据/多个数值/未知头部不借框架吞行：" + repr(_bad_prefix),
        "待核对" in _txt(_r) and _c["price_calls"] == 0,
        _txt(_r),
    )
for _broken_frame in (
    "7\n样例武器 Rava-testa\n" + _full_line_text,
    "样例武器 Rava-testa\n7\n" + _full_line_text + "\n段位 6",
    "7\n样例武器 Rava-testa\n另一武器 Tera-sida\n" + _full_line_text + "\n段位 6",
):
    _r, _c, _ = asyncio.run(_cloud_handler(_cloud_complete, _broken_frame))
    check(
        "无底栏/标题后数字/多卡框架不当头部元数据",
        "待核对" in _txt(_r) and _c["price_calls"] == 0,
        _txt(_r),
    )
_split_frame = "7\n样例武器\nRava-testa\n" + _full_line_text + "\n段位 6"
_r, _c, _ = asyncio.run(_cloud_handler(_cloud_complete, _split_frame))
check("有完整框架的中拉丁标题分行恢复", bool(_r.lines) and _c["price_calls"] == 1, _txt(_r))
# UI只按有坐标的卡外空间角色排除，卡内/无坐标未知字样继续拒绝。
_column_title = "600,300,24,180,90,样例武器 Rava-testa"
_column_stat = [
    f"600,{400 + i * 50},24,180,90,{line}" for i, line in enumerate(_full_line_text.splitlines())
]
_column_base = "\n".join([_column_title, *_column_stat, "550,650,24,80,90,段位 6"])
for _label in ("所有", "等级"):
    _r, _c, _ = asyncio.run(
        _cloud_handler(_cloud_complete, _column_base + f"\n250,700,24,50,90,{_label}")
    )
    check("卡外有坐标栏目不误杀：" + _label, bool(_r.lines) and _c["price_calls"] == 1, _txt(_r))
    for _row in (_label, f"600,700,24,50,90,{_label}", "600,500,24,50,90,未知字段"):
        _r, _c, _ = asyncio.run(_cloud_handler(_cloud_complete, _column_base + "\n" + _row))
        check(
            "卡内/无坐标/未知栏目不豁免：" + _row,
            "待核对" in _txt(_r) and _c["price_calls"] == 0,
            _txt(_r),
        )

for _state in ("null", "None", "unknown", "unreadable", "unclear", "NaN"):
    for _field in ("暴击机率", "未知词条"):
        _unsafe = _field + "\n" + _state + "\n" + _full_line_text + "\n段位 6"
        _r, _c, _ = asyncio.run(_cloud_handler(_cloud_complete, _unsafe))
        check(
            "未知值/近名词条不能伪装分行标题：" + _field + "/" + _state,
            "待核对" in _txt(_r) and _c["price_calls"] == 0,
            _txt(_r),
        )
_r, _c, _ = asyncio.run(
    _cloud_handler(_cloud_complete, "暴击机率\nRava-testa\n" + _full_line_text + "\n段位 6")
)
check(
    "有近名归一的词条不因拉丁尾巴变标题", "待核对" in _txt(_r) and _c["price_calls"] == 0, _txt(_r)
)

# 通用结构适配：仅修改保存观察的形状，不生成窄读来源。
for label, raw, want_pos, want_neg in [
    (
        "签名name与独立value一致",
        [{"name": "+53.2% 暴击伤害", "value": "53.2"}],
        [("crit_damage", 53.2)],
        [],
    ),
    ("倒置二元对", [["53.2%", "暴击伤害"]], [("crit_damage", 53.2)], []),
    (
        "明确text/modifier",
        [{"text": "+53.2% 暴击伤害", "modifier": {"name": "暴击伤害", "value": "53.2"}}],
        [("crit_damage", 53.2)],
        [],
    ),
    (
        "每项完整签名行",
        ["+53.2% 暴击伤害", "-26.4% 切割伤害"],
        [("crit_damage", 53.2)],
        [("slash_damage", 26.4)],
    ),
    ("显式派系倍率", [["对Corpus的伤害", "x0.73"]], [], [("damage_vs_corpus", 27.0)]),
    (
        "嵌入倍率和独立百分比等价",
        [{"name": "x1.42 对Corpus的伤害", "value": "42%"}],
        [("damage_vs_corpus", 42.0)],
        [],
    ),
    ("反转词条按符号", [{"name": "+53.2% 武器后坐力", "value": "+53.2%"}], [], [("recoil", 53.2)]),
]:
    sample = {"positive": raw}
    got = _norm(sample, _rev)
    check("结构适配正向：" + label, got == (want_pos, want_neg), str(got))
    check(
        "结构适配保持一项一分母：" + label,
        len(plugin.WarframeSDJK._iter_stat_pairs(raw)) == len(raw),
    )
    check("text不伪装窄读来源：" + label, "lines" not in sample)

for label, raw in [
    ("名称值不一致", [{"name": "+53.2% 暴击伤害", "value": "53.3"}]),
    ("极性不一致", [{"name": "-53.2% 暴击伤害", "value": "+53.2"}]),
    ("嵌入name的独立null", [{"name": "+53.2% 暴击伤害", "value": None}]),
    (
        "嵌套modifier名称冲突",
        [{"text": "+53.2% 暴击伤害", "modifier": {"name": "多重射击", "value": "53.2"}}],
    ),
    ("嵌套modifier缺值", [{"text": "+53.2% 暴击伤害", "modifier": {"name": "暴击伤害"}}]),
    ("假x非派系", [["暴击伤害", "x1.42"]]),
    ("假x百分比", [["对Corpus的伤害", "x1.42%"]]),
    ("假x非法倍率", [["对Corpus的伤害", "x99"]]),
    ("未知数值单位", [["暴击伤害", "53.2kg"]]),
    ("name内未知单位", [{"name": "+53.2kg 暴击伤害", "value": "53.2"}]),
    ("typed metadata不忽略", [["段位", "12"]]),
]:
    sample = {"positive": raw}
    got = _norm(sample, _rev)
    check("结构适配反向拒绝：" + label, not got[0] and not got[1], str(got))
    check("反向拒绝保留分母：" + label, len(plugin.WarframeSDJK._iter_stat_pairs(raw)) == len(raw))

_mixed = ["+53.2% 暴击伤害", "未读清楚"]
check("两项行列表坏行不缩分母", len(plugin.WarframeSDJK._iter_stat_pairs(_mixed)) == 2)
check(
    "原负桶不被正显式数值转为正桶",
    _norm({"negative": [["暴击伤害", "+53.2%"]]}, _rev) == ([], [("crit_damage", 53.2)]),
)

for label, value, precision, lexical in [
    ("精度损失", "53.200000000000000001", True, False),
    ("正常十进制", "53.2%", False, False),
    ("词法失败不是精度损失", "53.2kg", False, True),
]:
    payload = {
        "weapon": "盗贼",
        "positive": [["暴击伤害", value], ["多重射击", "63.4%"]],
        "negative": [],
    }
    _reply, captured, _provider = asyncio.run(
        _cloud_handler(payload, "+53.2% 暴击伤害\n+63.4% 多重射击")
    )
    parsed = captured["parsed"]
    check(
        "提取精度分型：" + label,
        parsed.get("_numeric_precision_unsafe") is precision
        and parsed.get("_numeric_lexical_invalid") is lexical,
        str(parsed),
    )
    if precision or lexical:
        check(
            "坏值真实入口fail closed：" + label,
            "待核对" in _txt(_reply) and captured["price_calls"] == 0,
            _txt(_reply),
        )

check(
    "负桶无符号幅度与嵌入负号一致",
    _norm({"negative": [{"name": "-53.2% 暴击伤害", "value": "53.2"}]}, _rev)
    == ([], [("crit_damage", 53.2)]),
)
check(
    "负桶显式正号仍与嵌入负号冲突",
    _norm({"negative": [{"name": "-53.2% 暴击伤害", "value": "+53.2"}]}, _rev) == ([], []),
)
check("已知单位不能移给错误SID", _norm({"positive": [["暴击伤害", "53.2m"]]}, _rev) == ([], []))
check(
    "嵌入值比较不经float掩盖微小矛盾",
    _norm({"positive": [{"name": "+53.2% 暴击伤害", "value": "53.200000000000000001"}]}, _rev)
    == ([], []),
)
# >28位倍率：真实提取到handler不能把不一致词法经context舍入洗白。
_long_faction_payload = {
    "weapon": "盗贼",
    "positive": [
        {"name": "x1.42 对Corpus的伤害", "value": "x1.420000000000000000000000000001"},
        ["暴击伤害", "53.2%"],
    ],
    "negative": [],
}
_long_reply, _long_saved, _ = asyncio.run(
    _cloud_handler(_long_faction_payload, "x1.42 对Corpus的伤害\n+53.2% 暴击伤害")
)
check(
    "超过Decimal默认精度的派系冲突真实入口拒绝",
    "待核对" in _txt(_long_reply) and _long_saved["price_calls"] == 0,
    _txt(_long_reply),
)
check(
    "超过Decimal默认精度的派系冲突保留失败分母",
    len(_long_saved["parsed"]["_recognition"]["stat_normalization"]) == 2
    and _long_saved["parsed"]["_recognition"]["stat_normalization"][0]["decision"] != "accepted",
    str(_long_saved["parsed"]["_recognition"].get("stat_normalization")),
)
# 嵌入签名与独立值的显式单位必须一致；单位不随数值适配被抹掉。
for _unit_name, _unit_value, _line in [
    ("+1.6m 近战范围", "1.6%", "+1.6m 近战范围"),
    ("+1.6% 近战范围", "1.6m", "+1.6m 近战范围"),
    ("+5.4秒 连击持续时间", "5.4%", "+5.4秒 连击持续时间"),
]:
    _payload = {
        "weapon": "盗贼",
        "positive": [{"name": _unit_name, "value": _unit_value}, ["暴击伤害", "53.2%"]],
        "negative": [],
    }
    _reply, _saved, _ = asyncio.run(_cloud_handler(_payload, _line + "\n+53.2% 暴击伤害"))
    check(
        "显式单位矛盾真实入口拒绝：" + _unit_name + "/" + _unit_value,
        "待核对" in _txt(_reply) and _saved["price_calls"] == 0,
        _txt(_reply),
    )
    check(
        "单位矛盾保留失败分母：" + _unit_name,
        len(_saved["parsed"]["_recognition"]["stat_normalization"]) == 2
        and _saved["parsed"]["_recognition"]["stat_normalization"][0]["decision"] != "accepted",
    )
check(
    "范围米/m等价",
    _norm({"positive": [{"name": "+1.6米 近战范围", "value": "1.6m"}]}, _rev)
    == ([("range", 1.6)], []),
)
check(
    "范围独立值无单位沿用幅度",
    _norm({"positive": [{"name": "+1.6m 近战范围", "value": "1.6"}]}, _rev)
    == ([("range", 1.6)], []),
)
check(
    "秒单位一致",
    _norm({"positive": [{"name": "+5.4秒 连击持续时间", "value": "5.4秒"}]}, _rev)
    == ([("combo_duration", 5.4)], []),
)
# 倒置显式倍率只由另一侧派系名称授权，不扩展非派系x或未知名称。
for _raw, _want in [
    (["x1.43", "对Grineer的伤害"], ([("damage_vs_grineer", 43.0)], [])),
    (["x0.72", "对Corpus的伤害"], ([], [("damage_vs_corpus", 28.0)])),
    (["×0.69", "对Infested的伤害"], ([], [("damage_vs_infested", 31.0)])),
]:
    check("倒置派系倍率：" + str(_raw), _norm({"positive": [_raw]}, _rev) == _want)
    check(
        "倒置派系倍率保留单行分母：" + str(_raw),
        len(plugin.WarframeSDJK._iter_stat_pairs([_raw])) == 1,
    )
for _raw in [
    ["x1.43", "未知词条"],
    ["x1.43", "暴击伤害"],
    ["x1.43", "1.43"],
    ["x1.43", "x0.72"],
    ["对Grineer的伤害", "对Corpus的伤害"],
    ["x99", "对Grineer的伤害"],
]:
    check("倒置倍率不扩权：" + str(_raw), _norm({"positive": [_raw]}, _rev) == ([], []))
    check(
        "倒置倍率拒绝保留分母：" + str(_raw), len(plugin.WarframeSDJK._iter_stat_pairs([_raw])) == 1
    )
# 字段别名、卡面元数据和页脚的通用恢复；不跳过卡内未知行。
check(
    "原文名字段保持可解析",
    plugin.WarframeSDJK._iter_stat_pairs([{"词条名原文": "暴击伤害", "数值": 108.2}])
    == [("暴击伤害", 108.2)],
)
_framed = (
    "18一\n盗贼 Visi toxican\n"
    + _full_line_text
    + "\n段位 12\n确认\n用 3,500 来循环\n剩余赤毒：99,999"
)
_frame_reply, _frame_saved, _ = asyncio.run(_cloud_handler(_cloud_complete, _framed))
check(
    "标题极性与页脚按钮不否决完整词条",
    not _frame_reply.raw_text and bool(_frame_reply.lines),
    _txt(_frame_reply),
)
_inside_reply, _inside_saved, _ = asyncio.run(
    _cloud_handler(_cloud_complete, _full_line_text + "\n剩余赤毒：99,999")
)
check(
    "未观测段位底栏不把未知正文当页脚",
    "待核对" in _txt(_inside_reply) and _inside_saved["price_calls"] == 0,
)


# 字符宽度归一只修排版；未知/嵌套/错类别尾注仍由上面的反例拒绝。
for _text in ("+73.6% 暴击几率（重击时x2)", "+73.6% 暴击几率(重击时x2）"):
    _np, _nn, _nt = RA.parse_riven_lines([_text], _resolve)
    check("混用全半角闭合括号保持主体数值", _np == [("crit_chance", 73.6)] and not _nn)
check("派系名中排版空白可归一", _lookup("对 Grineer 的伤害", _rev) == "damage_vs_grineer")


_outside = _column_base + "\n180,470,24,70,90,未知面板文字"
_outside_read = plugin.WarframeSDJK._parse_riven_lines_text(_outside)
check("唯一坐标卡框可排除横向外部文字", _outside_read["_line_legal"])
check(
    "框外未知文字保留原始证据",
    any(
        r.get("structural") == "outside_card_region" and r["text"] == "未知面板文字"
        for r in _outside_read["_line_decode"]["rows"]
    ),
)

# 两字段重复同一完整原文时只解析一次；不拿相似名称/数值放宽冲突。
for _line, _expected in [
    ("+73.6% 暴击几率（重击时x2）", ([("crit_chance", 73.6)], [])),
    ("+1.6m 近战范围", ([("range", 1.6)], [])),
    ("x0.72 对Corpus的伤害", ([], [("damage_vs_corpus", 28.0)])),
]:
    check(
        "重复整行观察只解码一次：" + _line, _norm({"positive": [[_line, _line]]}, _rev) == _expected
    )
for _pair in [
    ["+73.6% 暴击几率", "+73.6% 暴击伤害"],
    ["+73.6% 暴击几率", "+73.7% 暴击几率"],
    ["+1.6m 近战范围", "+1.6% 近战范围"],
    ["+73.6% 未知词条", "+73.6% 未知词条"],
]:
    check("非完全相同原文仍拒绝：" + str(_pair), _norm({"positive": [_pair]}, _rev) == ([], []))
    check("重复字段拒绝也保留词条分母", len(plugin.WarframeSDJK._iter_stat_pairs([_pair])) == 1)

# 数字与已知量纲间的空白属于排版，不得拼接被空格分开的数位或放过量纲冲突。
for _pair, _expected in [
    (["+6.3 秒连击持续时间", "+6.3 秒"], ([("combo_duration", 6.3)], [])),
    (["+1.6 m 近战范围", "+1.6m"], ([("range", 1.6)], [])),
    (["+73.6 % 暴击几率", "+73.6 %"], ([("crit_chance", 73.6)], [])),
    (["x 0.72 对Corpus的伤害", "x0.72"], ([], [("damage_vs_corpus", 28.0)])),
]:
    check("已知单位空白保持原数值：" + str(_pair), _norm({"positive": [_pair]}, _rev) == _expected)
for _pair in [
    ["连击持续时间", "+6 .3 秒"],
    ["连击持续时间", "+6 3秒"],
    ["+6.3 秒连击持续时间", "+6.4 秒"],
    ["+1.6 m 近战范围", "+1.6 秒"],
    ["+1.6 kg 近战范围", "+1.6"],
]:
    check(
        "空白兼容不掩盖数字或单位错误：" + str(_pair),
        _norm({"positive": [_pair]}, _rev) == ([], []),
    )

# 坐标标题折行后重建唯一卡面区域；保留原行，不将底栏后的疑似词条静默丢弃。
_spatial_split = _column_base.replace(
    _column_title, "600,280,24,180,90,样例武器 Rava-\n600,310,24,100,90,testa"
)
_spatial_out = _spatial_split + "\n100,450,24,70,90,卡外文字\n600,850,24,90,90,操作工具栏"
_sr = plugin.WarframeSDJK._parse_riven_lines_text(_spatial_out)
check("分行标题重建区域后排除框外文字", _sr["_line_legal"])
check(
    "被过滤的坐标观察仍保留原文",
    sum(r.get("structural") == "outside_card_region" for r in _sr["_line_decode"]["rows"]) == 2,
)
for _suspicious in ("+13% 未知词条", "？？？", "暴击伤害"):
    _sr = plugin.WarframeSDJK._parse_riven_lines_text(
        _spatial_split + "\n600,850,24,90,90," + _suspicious
    )
    check("底栏外疑似属性行不被删去：" + _suspicious, _suspicious in _sr["lines"])


class _ModeCatalogClient(_FakeRivenClient):
    def __init__(self):
        self.queries = []

    async def wm_riven_weapons(self):
        return [
            dict(
                self._weapon,
                zh="合成腔体（主要）",
                en="Synthetic (Primary)",
                url_name="synthetic_(primary)",
            ),
            dict(
                self._weapon,
                zh="合成腔体（次要）",
                en="Synthetic (Secondary)",
                url_name="synthetic_(secondary)",
            ),
            dict(self._weapon),
        ]

    async def resolve_riven_weapon(self, query):
        self.queries.append(query)
        return dict(self._weapon, zh="合成腔体")


_mode_client = _ModeCatalogClient()
_mr, _mc, _ = asyncio.run(
    _cloud_handler(
        dict(_cloud_complete, weapon=None),
        "合成腔体 Rava-testa\n" + _full_line_text + "\n段位 6",
        client=_mode_client,
    )
)
check(
    "模式后缀词表允许卡面仅写腔体名",
    _mc["parsed"]["_recognition"]["weapon_identity"]["selected"] == "合成腔体"
    and _mode_client.queries == ["合成腔体"],
)
_mr, _mc, _ = asyncio.run(
    _cloud_handler(
        _cloud_complete,
        "合成腔体 Rava-testa\n" + _full_line_text + "\n段位 6",
        client=_ModeCatalogClient(),
    )
)
check(
    "模块模式归一不掩盖两路不同武器名",
    "两路读到了不同的官方武器名" in _txt(_mr) and _mc["price_calls"] == 0,
)

_near_title = "600,100,24,200,90,预览武器 Tera-veta\n" + _column_base
_near_read = plugin.WarframeSDJK._parse_riven_lines_text(_near_title)
check("远处预览标题不能跨过最近卡名抢占词条", _near_read["_line_legal"])
_second_column = _column_base.replace("600,", "200,").replace("550,", "150,")
_two_cards = plugin.WarframeSDJK._parse_riven_lines_text(_column_base + "\n" + _second_column)
check("两张完整坐标卡仍拒绝拼接", not _two_cards["_line_legal"])

_wrapped_column = _column_base.replace(
    _column_title, "600,280,24,180,90,样例武器 Rava-\n600,310,24,100,90,testa"
)
for _x, _should_pass in ((470, True), (600, False)):
    _wrapped_read = plugin.WarframeSDJK._parse_riven_lines_text(
        _wrapped_column + f"\n{_x},295,24,20,90,陌"
    )
    check(
        "折行标题边距不吸入卡外字符且保留卡内未知观察：" + str(_x),
        _wrapped_read["_line_legal"] == _should_pass,
    )

_footer_unordered = _column_base.replace(
    "550,650,24,80,90,段位 6", "650,650,24,50,90,113\n550,650,24,80,90,段位 6"
)
_fu = plugin.WarframeSDJK._parse_riven_lines_text(_footer_unordered)
check(
    "底栏整数按坐标定位，不依赖OCR输出顺序",
    _fu["_line_legal"]
    and any(r.get("structural") == "card_footer_integer" for r in _fu["_line_decode"]["rows"]),
)
for _invalid_footer in ("113%", "+113", "113.5", "未知词条"):
    _fu = plugin.WarframeSDJK._parse_riven_lines_text(
        _footer_unordered.replace(",113\n", "," + _invalid_footer + "\n")
    )
    check("同基线非整数观察仍保留：" + _invalid_footer, _invalid_footer in _fu["lines"])


# 转录协议只转换已有行；不吞未知，不用模型分类正负面。
_transcript = {
    "weapon": "测试名",
    "stat_lines": ["+88.1%暴击伤害", "-7.4 秒连击持续时间", "？？？"],
}
_adapted = plugin.WarframeSDJK._adapt_riven_transcription(_transcript)
check(
    "原文协议保留未知行与负号",
    len(_adapted["positive"]) == 3 and _adapted["positive"][1]["value"].startswith("-"),
)
check("原文适配不改写输入", "positive" not in _transcript)
for _prefix in ("\U0001f512", "\u2694\ufe0f", "\u25c6"):
    check(
        "词条行首图标可剥而原文不变：" + repr(_prefix),
        plugin.WarframeSDJK._signed_stat_line(_prefix + " +88.1%暴击伤害")
        == ("暴击伤害", "+88.1%"),
    )
for _prefix in ("?", "9", "x", "未知"):
    check(
        "词条行首未知内容不能当图标剥掉：" + _prefix,
        plugin.WarframeSDJK._signed_stat_line(_prefix + " +88.1%暴击伤害") is None,
    )
check(
    "数字后置词条仅调换已有观察",
    plugin.WarframeSDJK._signed_stat_line("收起武器时弹匣每秒自动装填+88.1%")
    == ("收起武器时弹匣每秒自动装填", "+88.1%"),
)
_conflicting_schema = plugin.WarframeSDJK._adapt_riven_transcription(
    {**_transcript, "positive": [["基伤", 99]]}
)
check(
    "旧新字段冲突不静默覆盖",
    _conflicting_schema.get("_transcription_schema_conflict")
    and len(_conflicting_schema["positive"]) == 4,
)
for _category in ("霰弹枪", "步枪", "近战"):
    _category_payload = {
        "weapon": _category,
        "stat_lines": ["+93.5%多重射击", "+39.6%弹匣容量", "-13.4%变焦"],
    }
    _category_text = f"{_category} Armacan\n+93.5%多重射击\n+39.6%弹匣容量\n-13.4%变焦"
    _category_reply, _category_saved, _ = asyncio.run(
        _cloud_handler(_category_payload, _category_text)
    )
    check(
        "类别标题不猜武器或询价：" + _category,
        getattr(_category_reply, "riven_transcription", {}).get("category") == _category
        and _category_saved["price_calls"] == 0,
    )


# 可选复读仅裁决已有完整转录，保留第一条冲突证据；不得缩短观察分母。
_conflict_text = _full_line_text.replace("108.2", "108.3")
if _conflict_text == _full_line_text:
    _conflict_text = _full_line_text.replace("+", "-", 1)
_retry_reply, _retry_saved, _ = asyncio.run(
    _cloud_handler(_cloud_complete, _conflict_text, recheck_text=_full_line_text)
)
check(
    "完整主读与独立复读一致才裁决",
    bool(_retry_reply.lines) and _retry_saved["parsed"]["_recognition"]["line_recheck"]["adopted"],
)
check(
    "复读不抹掉最初冲突原文",
    _retry_saved["parsed"]["_recognition"]["line_recheck"]["original_line_read"]["_recognition"][
        "raw_text"
    ]
    == _conflict_text,
)
_retry_bad, _retry_bad_saved, _ = asyncio.run(
    _cloud_handler(
        _cloud_complete, _full_line_text + "\n+12.3%未知属性", recheck_text=_full_line_text
    )
)
check(
    "复读少一条不能压过原观察分母",
    not _retry_bad.lines
    and not _retry_bad_saved["parsed"]["_recognition"]["line_recheck"]["adopted"],
)
_retry_conflict, _retry_conflict_saved, _ = asyncio.run(
    _cloud_handler(_cloud_complete, _conflict_text, recheck_text=_conflict_text)
)
check(
    "复读仍分歧不选较顺眼的值",
    not _retry_conflict.lines
    and not _retry_conflict_saved["parsed"]["_recognition"]["line_recheck"]["adopted"],
)

if FAILED:
    print(f"\n失败 {len(FAILED)} 项：{FAILED}")
    sys.exit(1)
print("\n全部通过 ✔")
