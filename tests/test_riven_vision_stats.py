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


_log_json = {"weapon": "盗贼 Visi-toxican",
             "positive": [["基伤", 120.5], ["毒素伤害", 299.9], ["多重", 148],
                          ["触发", 97.2], ["持续", 97.2]],
             "negative": [["滑暴", 97.2], ["触发时间", 97.2]]}
_p0, _n0 = _norm(_log_json, _rev)
check("回归锚点：只信语义 JSON 确实数成 4 正 2 负（旧故障原样复现）",
      len(_p0) == 4 and len(_n0) == 2, f"{_p0} / {_n0}")

_card_lines = ["盗贼 Visi-toxican",        # 武器名 + 自命名：不是词条行
               "🔒 +120.5% 毒素伤害",      # 行首锁图标：不能因此丢词条
               "+299.9% 伤害",
               "+148% 多重射击",
               "-97.2% 触发时间",
               "点击 ⓘ 查看详情",          # 图例行：不是词条
               "内融值 1,234"]             # 右下角内融值：不是词条
_lp, _ln, _notes = RA.parse_riven_lines(_card_lines, _resolve)
check("卡面逐行 → 3 正 1 负（正是 2~3 正 / ≤1 负的合法卡面）",
      len(_lp) == 3 and len(_ln) == 1, f"{_lp} / {_ln} / 备注 {_notes}")
check("逐行：名称与数值取同一行（毒素伤害 120.5、伤害 299.9 —— 不再交叉配错）",
      _lp[0] == ("toxin_damage", 120.5) and _lp[1] == ("melee_damage", 299.9),
      str(_lp))
check("逐行：多重 148 保留", _lp[2] == ("multishot", 148.0), str(_lp))
check("逐行：负词条 = 触发时间 97.2（不再被拆成触发/持续）",
      _ln == [("status_duration", 97.2)], str(_ln))
check("逐行：武器名/图例/内融值行被跳过且留痕（可查日志）",
      sum(1 for n in _notes if n.startswith("无极性符号")) == 3, str(_notes))

# 行被拆开时的合并（用户口径：数值行 + 极性符号行并成一条）
_lp2, _ln2, _ = RA.parse_riven_lines(["+", "120.5% 毒素伤害"], _resolve)
check("合并：纯极性符号行 + 下一行", _lp2 == [("toxin_damage", 120.5)], str(_lp2))
_lp3, _, _ = RA.parse_riven_lines(["+毒素伤害", "120.5"], _resolve)
check("合并：带极性无数值行 + 下一行纯数值行",
      _lp3 == [("toxin_damage", 120.5)], str(_lp3))

# 乘数写法的负词条（卡面「x0.55 对 Corpus 的伤害」→ magnitude 45）
_lp4, _ln4, _ = RA.parse_riven_lines(["-0.55x 对Corpus的伤害"], _resolve)
check("乘数行 → damage_vs_corpus 45（负）",
      not _lp4 and _ln4 == [("damage_vs_corpus", 45.0)], f"{_lp4} / {_ln4}")
_lp5, _ln5, _ = RA.parse_riven_lines(["-0.55 对Corpus的伤害"], _resolve)
check("乘数漏写 x 也按乘数算（对派系基值 45，真 magnitude 不可能 <1）",
      not _lp5 and _ln5 == [("damage_vs_corpus", 45.0)], f"{_lp5} / {_ln5}")

# 重复行去重 + 同词条两侧都在时以负为准
_lp6, _ln6, _ = RA.parse_riven_lines(["+148% 多重射击", "+148% 多重射击"],
                                     _resolve)
check("同一行重复出现只算一条", _lp6 == [("multishot", 148.0)], str(_lp6))
_lp7, _ln7, _ = RA.parse_riven_lines(["+97.2% 触发时间", "-97.2% 触发时间"],
                                     _resolve)
check("同词条两侧都在 → 以负为准（卡面每行只出现一次）",
      not _lp7 and _ln7 == [("status_duration", 97.2)], f"{_lp7} / {_ln7}")

# 拿不到行（模型没给 lines / 给了空表）⇒ 行数为 0，主流程据此退回语义 JSON
_lp8, _ln8, _ = RA.parse_riven_lines([], _resolve)
check("没有 lines 时行解析返回空（主流程据此退回语义 JSON）",
      not _lp8 and not _ln8)
_lp9, _ln9, _ = RA.parse_riven_lines(["120.5% 毒素伤害"], _resolve)
check("行首没有极性符号 ⇒ 不当词条（用户口径：只认 +/- 开头的行）",
      not _lp9 and not _ln9)


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
_sa = ["+91.1% 暴击伤害", "x1.51 对 Infested 的伤害", "+29.6 初始连击",
       "-115.7% 处决伤害"]
_lpa, _lna, _na = RA.parse_riven_lines(_sa, _resolve)
check("★ 乘数行不再被丢掉：报障卡面 → 3 正 1 负",
      len(_lpa) == 3 and len(_lna) == 1, f"{_lpa} / {_lna} / 备注 {_na}")
check("★ 乘数 >1 是**正**词条：damage_vs_infested 51（不是 151、也不是负词条）",
      ("damage_vs_infested", 51.0) in _lpa, str(_lpa))
check("乘数行与其余三行各自配对正确",
      ("crit_damage", 91.1) in _lpa and ("initial_combo", 29.6) in _lpa
      and _lna == [("finisher_damage", 115.7)], f"{_lpa} / {_lna}")
check("乘数行不再出现在「无极性符号」备注里",
      not any("x1.51" in n for n in _na), str(_na))

# 乘数 <1 仍是负词条（既有口径不回退）
_lpb, _lnb, _ = RA.parse_riven_lines(["x0.55 对 Corpus 的伤害"], _resolve)
check("乘数 <1 仍是负词条（magnitude 45）",
      not _lpb and _lnb == [("damage_vs_corpus", 45.0)], f"{_lpb} / {_lnb}")
# 模型漏写 x / 漏写乘数算式的两种形态也要接住
_lpc, _lnc, _ = RA.parse_riven_lines(["+1.51 对 Infested 的伤害"], _resolve)
check("写成 +1.51（漏 x）仍按乘数还原成正词条 51",
      _lpc == [("damage_vs_infested", 51.0)] and not _lnc, f"{_lpc} / {_lnc}")
_lpd, _lnd, _ = RA.parse_riven_lines(["-0.45 对 Infested 的伤害"], _resolve)
check("写成 -0.45（漏 x）也按乘数还原：净倍率 0.45 ⇒ −55%",
      not _lpd and _lnd == [("damage_vs_infested", 55.0)], f"{_lpd} / {_lnd}")

# 语义表那一路：模型把乘数直接 ×100（x1.51 → 151）也要还原
_fix = plugin.WarframeSDJK._faction_val_fix
check("语义表 151 → 正词条 51（不是 151）",
      _fix("damage_vs_infested", 151.0) == 51.0, str(_fix("damage_vs_infested", 151.0)))
check("语义表 0.55 → −45（符号交给 _route 归 negative）",
      _fix("damage_vs_corpus", 0.55) == -45.0, str(_fix("damage_vs_corpus", 0.55)))
check("正常 magnitude 原样不动（51 / 45）",
      _fix("damage_vs_infested", 51.0) == 51.0
      and _fix("damage_vs_corpus", 45.0) == 45.0)
check("非「对派系」词条一律不动（避免误伤）", _fix("crit_damage", 0.55) == 0.55)
check("回归：x1.51 经语义表 → 3 正 1 负（与行解析同口径）",
      (lambda pn: len(pn[0]) == 3 and len(pn[1]) == 1)(
          _norm({"positive": [["暴伤", 91.1], ["对Infested的伤害", 151],
                              ["初始连击", 29.6]],
                 "negative": [["处决伤害", 115.7]]}, _rev)),
      str(_norm({"positive": [["暴伤", 91.1], ["对Infested的伤害", 151],
                              ["初始连击", 29.6]],
                 "negative": [["处决伤害", 115.7]]}, _rev)))


# ---------------------------------------------------------------------------
# ⑥ 端到端：报障那张卡现在必须**走通**（不再是那条计数错误）
# ---------------------------------------------------------------------------
class _FakeRivenClient:
    """盗贼（手枪列，倾向 1.4）—— 只实现紫卡分析这条链用到的接口。"""

    _weapon = {"url_name": "bandit", "zh": "盗贼", "en": "Bandit",
               "disposition": 1.4, "riven_type": "pistol", "group": "secondary"}

    async def resolve_riven_weapon(self, q):
        return dict(self._weapon)

    async def resolve_variant_disp(self, name):
        return (None, "")

    async def riven_family(self, weapon):
        return []                      # 家族无变体：不干扰本条回归的数值判定

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
    "lines": ["盗贼 Visi-toxican", "🔒 +120.5% 毒素伤害", "+299.9% 伤害",
              "+148% 多重射击", "-97.2% 触发时间", "内融值 1,234"],
    "positive": [["基伤", 120.5], ["毒素伤害", 299.9], ["多重", 148],
                 ["触发", 97.2], ["持续", 97.2]],
    "negative": [["滑暴", 97.2], ["触发时间", 97.2]]}


async def _fake_imgs(event):
    return ["data:image/jpeg;base64,x"]


async def _fake_extract(url):
    return dict(_incident_vision)


_obj = plugin.WarframeSDJK.__new__(plugin.WarframeSDJK)
_obj.client = _FakeRivenClient()
_obj.page_size = 12
_obj._image_data_urls = _fake_imgs
_obj._extract_riven_from_image = _fake_extract
_reply = asyncio.run(_obj._h_riven_analysis(_FatParsed("紫卡分析"),
                                            _FakeImageEvent(), "pc"))
_body = "\n".join(_reply.lines)
check("端到端确实走到了分析卡（不是用法/错误提示 —— 防空过守卫）",
      bool(_reply.lines) and not _reply.raw_text, repr(_reply)[:200])
check("★ 报障那张卡不再被计数校验挡掉（没有「4 正 2 负」）",
      "4 正 2 负" not in _body and "词条应为" not in _body, _body[:220])
check("★ 四条词条按卡面行原文配对：120.5 毒伤 / 299.9 基伤 / 148 多重",
      all(k in _body for k in ("120.5% 毒伤", "299.9% 基伤", "148% 多重")),
      _body[:300])
check("★ 反向守卫：不再按语义表交叉配错（120.5 基伤 / 299.9 毒伤）",
      "120.5% 基伤" not in _body and "299.9% 毒伤" not in _body,
      _body[:300])
check("★ 负词条 = 触时 97.2（不再冒出「滑暴」）",
      "-97.2% 触时" in _body and "滑暴" not in _body, _body[:300])
check("卡面注明来源为「卡面逐行」而不是语义表",
      "卡面逐行" in _body, _body[:220])

# 拿不到 lines（渠道/模型没给这个字段）⇒ 安全退回语义 JSON，不崩
async def _fake_extract_nolines(url):
    return {k: v for k, v in _incident_vision.items() if k != "lines"}


_obj._extract_riven_from_image = _fake_extract_nolines
_r2 = asyncio.run(_obj._h_riven_analysis(_FatParsed("紫卡分析"),
                                         _FakeImageEvent(), "pc"))
check("没给 lines 时退回语义 JSON（不崩，仍按原判据提示词条数）",
      bool(_r2.raw_text) and "词条应为" in _r2.raw_text, repr(_r2)[:200])


# ---------------------------------------------------------------------------
# ⑦ 窄读一路：读不全就不采信（2026-09-27 实测结论）
# ---------------------------------------------------------------------------
# 实测（用户那张 321×450 卡 ×3 次）：glm-4v-flash 用完整 JSON 提示词**稳定只
# 照抄 2/4 行**（把图放大 4× 也一样漏），而换成「只要词条行原文」的窄提示词后
# 三个渠道 6/6 全对 ⇒ 词条行单独一路窄读，且判据是「卡面合法」（能解析 ≠ 读全）。
_S = plugin.WarframeSDJK
_FULL = "+120.5% 毒素伤害\n+299.9% 伤害\n+148% 多重射击\n-97.2% 触发时间"
_PART = "+120.5% 毒素伤害\n-97.2% 触发时间"
check("窄读答案 4 行 → 采信", _S._parse_riven_lines_text(_FULL)["lines"]
      == _FULL.splitlines(), str(_S._parse_riven_lines_text(_FULL)))
check("窄读答案只给 2 行（漏读）→ 不采信，让位下一个渠道",
      _S._parse_riven_lines_text(_PART) == {}, str(_S._parse_riven_lines_text(_PART)))
_prose = _S._parse_riven_lines_text("以下是词条行：\n" + _FULL)
check("窄读答案夹带寒暄行也不影响（无极性符号的行天然跳过）",
      _prose.get("lines", [])[:1] == ["以下是词条行："], str(_prose))


class _FakeVision:
    """固定回答 + 延时，模拟「快而残缺」与「慢但读全」两家渠道。"""

    def __init__(self, text, delay):
        self.text, self.delay = text, delay

    async def text_chat(self, prompt, session_id=None, image_urls=None):
        await asyncio.sleep(self.delay)
        return types.SimpleNamespace(completion_text=self.text)


_race = asyncio.run(_S.__new__(_S)._vision_race_json(
    "p", "data:image/jpeg;base64,x",
    [_FakeVision(_PART, 0.01), _FakeVision(_FULL, 0.3)],
    k=2, tag="测试行读", window=5,
    parse=_S._parse_riven_lines_text))
check("★ 竞速不采信先到的残缺结果，等读全的那一路",
      _race == {"lines": _FULL.splitlines()}, str(_race))
_race2 = asyncio.run(_S.__new__(_S)._vision_race_json(
    "p", "data:image/jpeg;base64,x", [_FakeVision(_PART, 0.01)],
    k=2, tag="测试行读", window=0.6,
    parse=_S._parse_riven_lines_text))
check("只有残缺结果时竞速返回 None（不硬用残缺卡面）",
      _race2 is None, str(_race2))

# ---------------------------------------------------------------------------
# ⑧ 端到端：空刃 3+1（2026-10-01 报障）—— 必须走通，且按变体倾向算区间
# ---------------------------------------------------------------------------
class _FakeSkanaClient(_FakeRivenClient):
    """空刃（近战，倾向 1.3）+ 家族变体棱晶·空刃 / 空刃 Prime（同为 1.2）。"""

    _weapon = {"url_name": "skana", "zh": "空刃", "en": "Skana",
               "disposition": 1.3, "riven_type": "melee", "group": "melee"}

    async def riven_family(self, weapon):
        return [("棱晶·空刃", 1.2), ("空刃 Prime", 1.2)]


# 服务器日志（01:42:44）里的原始两路结果：窄读抄全 4 行、语义表 3正1负（乘数写成 151）
_skana_vision = {
    "weapon": "空刃 Para-puratis",
    "lines": ["空刃 Para-puratis", "+91.1% 暴击伤害",
              "x1.51 对 Infested 的伤害", "+29.6 初始连击", "-115.7% 处决伤害"],
    "positive": [["暴伤", 91.1], ["对Infested的伤害", 151], ["初始连击", 29.6]],
    "negative": [["处决伤害", 115.7]]}


async def _fake_extract_skana(url):
    return dict(_skana_vision)


_obj2 = plugin.WarframeSDJK.__new__(plugin.WarframeSDJK)
_obj2.client = _FakeSkanaClient()
_obj2.page_size = 12
_obj2._image_data_urls = _fake_imgs
_obj2._extract_riven_from_image = _fake_extract_skana
_r3 = asyncio.run(_obj2._h_riven_analysis(_FatParsed("紫卡分析"),
                                          _FakeImageEvent(), "pc"))
_body3 = "\n".join(_r3.lines)
check("★ 端到端：空刃那张 3+1 卡走到分析卡（不是用法/错误提示 —— 防空过守卫）",
      bool(_r3.lines) and not _r3.raw_text, repr(_r3)[:200])
check("★ 四条词条全部进卡：暴伤 91.1 / I伤 51 / 初始连击 29.6 / 处决伤 115.7",
      all(k in _body3 for k in ("+91.1% 暴伤", "+51% I伤", "+29.6 初始连击",
                               "-115.7% 处决伤")), _body3[:400])
check("★ 不再误报「与家族已知倾向都不吻合」（旧故障串）",
      "都不吻合" not in _body3, _body3[:300])
check("★ 数值反推落到变体倾向 1.2（棱晶·空刃 / 空刃 Prime 同值 ⇒ 不算歧义）",
      "数值反推倾向 1.2" in _body3, _body3[:300])
check("★ 卡面按 1.2 计算区间（不是不吻合的母武器 1.3）",
      "【空刃】倾向 1.2" in _body3 and "倾向 1.3" not in _body3, _body3[:200])
check("不再回「请带变体名重发」（同值变体无从也无须区分）",
      "请带变体名重发" not in _body3, _body3[:300])

if FAILED:
    print(f"\n失败 {len(FAILED)} 项：{FAILED}")
    sys.exit(1)
print("\n全部通过 ✔")
