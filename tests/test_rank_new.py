# -*- coding: utf-8 -*-
"""价格排行 / 紫卡热度排行 新版式离线测试：python3 tests/test_rank_new.py

覆盖：
1. 裸「排行」三组各前五（甲/武器/MOD卡），按当前成交中位价降序；
2. 分类榜取 20 名、标签过滤正确（主武不含近战等）；
3. DE 周报 JS 字面量解析（裸键 + 单引号 + null）；
4. 紫卡周榜五组（含未开）与单类榜排序（0洗热度降序、已洗热度次级）、参考价=较低中位；
5. wr 洗数档位：低洗 1-8、废洗 ≥10。
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = ""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f"  -> {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


def _row(slug, zh, tags, m48=0, mn=0, mx=0, mprev=0):
    return {
        "slug": slug,
        "zh": zh,
        "en": slug,
        "tags": tags,
        "median48": m48,
        "min48": mn,
        "max48": mx,
        "median_prev": mprev,
        "vol48": 3,
        "vol90": 9,
    }


# --------------------------------------------------------------- 裸排行
from core import formatters as F  # noqa: E402

rows = [
    _row(
        "messa_set", "绯红五刃 一套", ["warframe", "prime", "set"], m48=90, mn=88, mx=95, mprev=99
    ),
    _row(
        "mesa_p_set",
        "Mesa Prime 一套",
        ["warframe", "prime", "set"],
        m48=120,
        mn=115,
        mx=130,
        mprev=133,
    ),
    _row(
        "no_48h", "仅90天数据的甲", ["warframe", "prime", "set"], m48=0, mprev=15
    ),  # 无当前价不入榜
    _row("no_sale", "无成交甲", ["warframe", "prime", "set"]),  # 无成交不入榜
    _row("gram", "格拉姆", ["weapon", "melee"], m48=30, mn=28, mx=35, mprev=41),
    _row("laetum", "拉特姆", ["weapon", "secondary"], m48=55, mn=50, mx=60, mprev=66),
    _row("nataruk", "纳塔鲁克", ["weapon", "primary"], m48=45, mn=40, mx=50, mprev=58),
    _row("primed_cont", " Prime 连续", ["mod"], m48=210, mn=200, mx=220, mprev=230),
    _row("arcane_eng", "赋能·狂怒", ["arcane_enhancement"], m48=310, mn=300, mx=320, mprev=330),
]
title, lines = F.fmt_rank_overview(rows)
check("裸排行标题", title == "价格排行榜", title)
# 2026-10-11 改版：卡首多一条 KV「◆ 各榜榜首：…」，分区标题 = 不含「：」的 ◆ 行
check(
    "三组各前五",
    sum(1 for ln in lines if ln.startswith("◆ ") and "：" not in ln) == 3,
    str(lines[:12]),
)
check("卡首榜首 KV", lines[0].startswith("◆ 各榜榜首：甲 "), lines[0])
check(
    "Mesa Prime 榜首（120 > 90）",
    any("Mesa Prime" in ln and "120p" in ln for ln in lines),
    str(lines[:8]),
)
check("无当前价（仅90d）不入榜", not any("仅90天数据的甲" in ln for ln in lines))
check("无成交物品不入榜", not any("无成交甲" in ln for ln in lines))
check("赋能不进甲组", not any("赋能·狂怒" in ln for ln in lines))
check("引导语存在", any("完整 20 名榜单" in ln for ln in lines))
check("历史列=上期中位", any("上期中位" in ln for ln in lines))

# --------------------------------------------------------------- 分类榜
title2, lines2 = F.fmt_rank_table("主武", rows)
check("分类榜标题", title2 == "主武价格排行", title2)
check(
    "主武榜不含近战/副武",
    all("格拉姆" not in ln and "拉特姆" not in ln for ln in lines2),
    str(lines2),
)
check("主武榜取纳塔鲁克", any("纳塔鲁克" in ln for ln in lines2))
title3, lines3 = F.fmt_rank_table("卡", rows)
check("卡榜含 0 级 MOD", any("Prime 连续" in ln for ln in lines3))
check("未知分类提示", "未知分类" in F.fmt_rank_table("不存在", rows)[1][0])

# --------------------------------------------------------------- DE 周报解析
from core.api_client import parse_de_riven_weekly  # noqa: E402

sample = """[
  { itemType: 'Melee Riven Mod', compatibility: 'Gram', rerolled: false,
    avg: 30, stddev: 5, min: 20, max: 60, pop: 12, median: 30 },
  { itemType: 'Melee Riven Mod', compatibility: 'Gram', rerolled: true,
    avg: 40, stddev: 8, min: 25, max: 90, pop: 30, median: 45 },
  { itemType: 'Archgun Riven Mod', compatibility: 'Larchibra', rerolled: false,
    avg: 25, stddev: 4, min: 18, max: 40, pop: 9, median: 25 },
  { itemType: 'Melee Riven Mod', compatibility: null, rerolled: false,
    avg: 8, stddev: 2, min: 4, max: 10, pop: 1, median: 8 },
  { itemType: 'Rifle Riven Mod', compatibility: null, rerolled: false,
    avg: 30, stddev: 6, min: 12, max: 60, pop: 3, median: 28 }
]"""
entries = parse_de_riven_weekly(sample)
check("周报解析条数", len(entries) == 5, str(len(entries)))
check(
    "周报字段完整",
    entries[0]["itemType"] == "Melee Riven Mod"
    and entries[3]["compatibility"] is None
    and entries[1]["pop"] == 30,
)

snap = {"fetched": 1789200000.0, "platform": "PC", "entries": entries}
zhmap = {"gram": "格拉姆"}
tw, lw = F.fmt_riven_weekly(snap, zhmap)
check("周榜标题", tw == "紫卡热度排行榜", tw)
check(
    "周榜组数（近战+空战，无数据组跳过）", sum(1 for ln in lw if ln.startswith("◆ ")) == 2, str(lw)
)
check("空战组入榜", any("Larchibra" in ln for ln in lw), str(lw))
check("未开不再出现在周榜数据行", not any("未开" in ln for ln in lw if not ln.startswith("※")))
check("格拉姆 0洗中位 30p", any("格拉姆" in ln and "30p" in ln for ln in lw), str(lw))
check(
    "参考价 = 较低中位（30 < 45）",
    any("格拉姆" in ln and ln.rstrip().endswith("30p") for ln in lw),
    str(lw),
)
check("刷新提示存在", any("紫卡排行 刷新" in ln for ln in lw))

# 未开单类榜（veiled_wm 主路径）：WM 实时价
_veiled_wm = {
    "Rifle": {"median": 10, "min": 4, "max": 30, "vol": 46},
    "Melee": {"median": 6, "min": 5, "max": 9, "vol": 50},
    "Kitgun": {"median": 2, "min": 2, "max": 3, "vol": 4},
}
tv, lv = F.fmt_riven_type("未开", snap, zhmap, veiled_wm=_veiled_wm)
check(
    "未开榜用 WM 实时价（步枪 10p）",
    any("步枪未开紫卡" in ln and "10p" in ln for ln in lv),
    str(lv),
)
check("未开榜含最低/最高列", any("当前中位" in ln and "最低" in ln for ln in lv), str(lv[:3]))
check("未开榜来源标注 WM", any("warframe.market 实时成交" in ln for ln in lv))
check(
    "未开榜按中位降序（步枪 10 > 近战 6 > 组合枪 2）",
    lv.index([x for x in lv if "步枪未开" in x][0])
    < lv.index([x for x in lv if "近战未开" in x][0])
    < lv.index([x for x in lv if "组合枪未开" in x][0]),
    str(lv),
)
check(
    "未开榜不再出现 DE 周报 590/600p", not any("590p" in ln or "600p" in ln for ln in lv), str(lv)
)

# DE 周报回退路径（无 veiled_wm）
tu, lu = F.fmt_riven_type("未开", snap, zhmap)
check("未开榜标题", tu == "紫卡热度·未开", tu)
check(
    "未开榜按类型列出",
    any("近战未开紫卡" in ln and "8p" in ln for ln in lu)
    and any("步枪未开紫卡" in ln and "28p" in ln for ln in lu),
    str(lu),
)
check(
    "未开榜热度降序（步枪 3% > 近战 1%）",
    lu.index([x for x in lu if "步枪未开" in x][0])
    < lu.index([x for x in lu if "近战未开" in x][0]),
    str(lu),
)

tt, lt = F.fmt_riven_type("近战", snap, zhmap, page=1, page_size=10)
check("单类榜标题", tt == "紫卡热度·近战", tt)
check("单类榜格拉姆在列", any("格拉姆" in ln for ln in lt))
check("单类榜页码 1/1", any("页码:1/1" in ln for ln in lt))
t_unk, l_unk = F.fmt_riven_type("霰弹枪", snap, zhmap)
check("DE 无该分类数据时提示", "暂无成交记录" in l_unk[0], l_unk[0])

# --------------------------------------------------------------- wr 洗数档位
import core.parser as P  # noqa: E402

check("低洗 = 1..8", P._REROLL_WORDS["低洗"] == (1, 8), str(P._REROLL_WORDS["低洗"]))
check("废洗 = ≥10", P._REROLL_WORDS["废洗"] == (10, None), str(P._REROLL_WORDS["废洗"]))
q = P.parse_wr(["格拉姆", "低洗"])
check(
    "低洗解析 → max 8 / min 1",
    q.max_rerolls == 8 and q.rerolls_min == 1,
    f"{q.max_rerolls}/{q.rerolls_min}",
)
q2 = P.parse_wr(["格拉姆", "废洗"])
check(
    "废洗解析 → min 10 / 无上限",
    q2.rerolls_min == 10 and q2.max_rerolls is None,
    f"{q2.max_rerolls}/{q2.rerolls_min}",
)
q3 = P.parse_wr(["格拉姆", "零洗"])
check(
    "零洗不变 → 0/0",
    q3.rerolls_min == 0 and q3.max_rerolls == 0,
    f"{q3.max_rerolls}/{q3.rerolls_min}",
)

# --------------------------------------------------------------- 紫卡分析
from core import riven_analysis as RA  # noqa: E402

# 测试卡（用户实例）：棱晶·欧玛 3+1 倾向 0.95，暴伤 82.8 → 区间 72.14-88.17
lo, hi = RA.stat_range("crit_damage", "melee", 0.95, 3, 1)
check(
    "暴伤区间（与参考卡 72.1-88.2 一致）",
    abs(lo - 72.14) < 0.01 and abs(hi - 88.17) < 0.01,
    f"{lo}-{hi}",
)
lo2, hi2 = RA.stat_range("slide_crit", "melee", 0.95, 3, 1, negative=True)
check(
    "负词条系数（滑暴 76.95-94.05）",
    abs(lo2 - 76.95) < 0.01 and abs(hi2 - 94.05) < 0.01,
    f"{lo2}-{hi2}",
)
check("距中计算", RA.deviation_pct(82.8, lo, hi) == 3.3, str(RA.deviation_pct(82.8, lo, hi)))
check("基值缺失返回空区间", RA.stat_range("zoom", "melee", 1.0, 3, 1) == (None, None))

# ★ 2026-10-02：combo 家族两侧基值补录（官方 wiki「Riven Mods」基值表整页核对；
#   表后 Legend：¹ 仅正向 / ³ 仅负向）。反算自洽：报障卡 海波单剑 3+1
#   （近战伤害 231.2 / 暴伤 128.8 / 滑暴 130.2）反推 D≈1.39~1.43。
_lo_c, _hi_c = RA.stat_range("extra_combo_count", "melee", 1.41, 3, 1)
check(
    "★ 额外连击数几率（仅正向）：基值 58.77 → D=1.41/3+1 区间 69.92-85.46",
    _lo_c is not None and abs(_lo_c - 69.92) < 0.05 and abs(_hi_c - 85.46) < 0.05,
    f"{_lo_c}-{_hi_c}",
)
check(
    "★ 报障卡自洽：+71.1% 额外连击落在该区间内（不再「无官方基值」）",
    _lo_c is not None and _lo_c <= 71.1 <= _hi_c,
    f"{_lo_c} <= 71.1 <= {_hi_c}",
)
_lo_g, _hi_g = RA.stat_range("combo_gain_chance", "melee", 1.41, 3, 1, negative=True)
check(
    "★ 几率不获得连击数（仅负向）：基值 104.85 → 负档区间 99.79-121.97",
    _lo_g is not None and abs(_lo_g - 99.79) < 0.05 and abs(_hi_g - 121.97) < 0.05,
    f"{_lo_g}-{_hi_g}",
)
check(
    "两条 combo 词条只在近战列有基值（其余类别 None → 空区间）",
    RA.stat_range("extra_combo_count", "rifle", 1.0, 3, 1) == (None, None)
    and RA.stat_range("combo_gain_chance", "pistol", 1.0, 3, 1) == (None, None),
)
check(
    "_PCT_UNIT_ONLY 已清空（两条都进基值表）、且仍按百分比显示",
    RA._PCT_UNIT_ONLY == set()
    and RA.fmt_value("extra_combo_count", 71.1) == "71.1%"
    and RA.fmt_value("combo_gain_chance", 110.9) == "110.9%",
)
ta, la = F.fmt_riven_analysis(
    "棱晶·欧玛",
    0.95,
    "melee",
    [("crit_damage", 82.8), ("range", 1.6), ("attack_speed", 45.8)],
    [("slide_crit", 81.3)],
)
check("分析卡倾向行", any("倾向 0.95" in ln and "●●●○○" in ln for ln in la), str(la))
check("分析卡区间行（72.14~88.17%）", any("72.14~88.17%" in ln for ln in la), str(la))
check("分析卡负词条提示", any("幅度越大越友好" in ln for ln in la))

# ------------------------------------------------- wiki valence HTML 解析
from core.api_client import WarframeClient as _WC  # noqa: E402

_sample_html = """<table>
<tr><td>Weapon (Batch B)</td><td>Element</td><td>Bonus&nbsp;%</td></tr>
<tr><td>Tenet&nbsp;Ferrox</td><td>Magnetic</td><td>25.7%</td></tr>
<tr><td>Tenet Exec</td><td>Cold</td><td>53.3%</td></tr>
<tr><td>Coda Bubonico</td><td>Heat</td><td>31.2%</td></tr>
</table>
<p>Eleanor is selling <a>Batch A</a> weapons. Time left until Batch B.</p>"""
_v = _WC.parse_wiki_valence(_sample_html)
check(
    "valence 解析 tenet（含 &nbsp;）",
    _v["tenet"].get("Tenet Ferrox") == ("Magnetic", 25.7)
    and _v["tenet"].get("Tenet Exec") == ("Cold", 53.3),
    str(_v["tenet"]),
)
check(
    "valence 批次取表头标注（B）而非 is selling（A）",
    _v["coda_batch"] == "B",
    str(_v["coda_batch"]),
)
check("valence 解析 coda", _v["coda"].get("Coda Bubonico") == ("Heat", 31.2))
check("valence 残缺时不瞎判批", _WC.parse_wiki_valence("<p>xx</p>")["coda_batch"] == "")

# ------------------------------------------- 武器家族匹配（变体提示用）
check("家族匹配 棱晶·欧玛", _WC._family_match("欧玛", "Ohma", "棱晶·欧玛", "Prisma Ohma"))
check("家族匹配 英文后缀（Prisma Ohma）", _WC._family_match("", "Ohma", "", "Prisma Ohma"))
check("家族匹配 排除无关名", not _WC._family_match("欧玛", "Ohma", "牛津", "Oxium"))
check("家族匹配 空名不匹配", not _WC._family_match("", "", "棱晶·欧玛", "Prisma Ohma"))

# ------------------------------------------------- LLM 提取结果的规范化
# astrbot 桩（main 顶层 import 需要）
_pkg = types.ModuleType("astrbot")
_api = types.ModuleType("astrbot.api")
_ev = types.ModuleType("astrbot.api.event")
_mc = types.ModuleType("astrbot.api.message_components")
_st = types.ModuleType("astrbot.api.star")


class _Logger:
    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass

    def exception(self, *a, **k):
        pass


class _Filter:
    EventMessageType = type("EMT", (), {"ALL": "ALL"})

    @staticmethod
    def event_message_type(*a, **k):
        return lambda f: f

    permission_type = type("Perm", (), {"ADMIN": "ADMIN"})


class _E:
    pass


_ev.AstrMessageEvent = _E
_ev.MessageChain = _E
_ev.EventMessageType = _Filter.EventMessageType
_ev.event_message_type = _Filter.event_message_type
_ev.filter = _Filter()
_mc.Image = _E
_mc.Plain = _E
_st.Context = type("Ctx", (), {})
_st.Star = type("Star", (), {"__init__": lambda self, *a, **k: None})
_st.register = lambda *a, **k: lambda c: c
_api.AstrBotConfig = dict
_api.logger = _Logger()
sys.modules.setdefault("astrbot", _pkg)
sys.modules["astrbot.api"] = _api
sys.modules["astrbot.api.event"] = _ev
sys.modules["astrbot.api.message_components"] = _mc
sys.modules["astrbot.api.star"] = _st

import main as plugin  # noqa: E402

rev2 = {v: k for k, v in P.RIVEN_STAT_ZH.items()}
pos2, neg2 = plugin.WarframeSDJK._normalize_llm_stats(
    {
        "positive": [["暴伤", "82.8"], ["范围", "1.6m"], ["攻击速度", 45.8]],
        "negative": [["滑行暴击", "81.3%"]],
    },
    rev2,
)
check("LLM 词条规范化：暴伤 82.8", pos2[0] == ("crit_damage", 82.8), str(pos2))
check("LLM 词条规范化：范围 1.6", pos2[1] == ("range", 1.6), str(pos2))
check("LLM 词条规范化：攻击速度→攻速", pos2[2] == ("attack_speed", 45.8), str(pos2))
check("LLM 词条规范化：负词条滑暴 81.3", neg2 == [("slide_crit", 81.3)], str(neg2))
pos3, neg3 = plugin.WarframeSDJK._normalize_llm_stats({"positive": [["未知词条", 5]]}, rev2)
check("LLM 未知识别不了的词条被丢弃", not pos3, str(pos3))

# ------------------------------------------- 数值反推倾向（变体自动判定）
# 实测卡：欧玛卡面 +82.8 暴伤 / +1.6 范围 / +45.8 攻速 / -81.3 滑暴
# （玩家把它装在棱晶·欧玛上；卡面只写母武器名，变体信息不在图里）
_cp = [("crit_damage", 82.8), ("range", 1.6), ("attack_speed", 45.8)]
_cn = [("slide_crit", 81.3)]
_iv = RA.disposition_interval(_cp, _cn, "melee")
check("反推区间含 0.95（棱晶）", _iv[0] is not None and _iv[0] <= 0.95 <= _iv[1], str(_iv))
check(
    "反推区间排除 1.25（母武器）", _iv[0] is not None and not (_iv[0] <= 1.25 <= _iv[1]), str(_iv)
)
check(
    "倾向可行性 0.95 ✓ / 1.25 ✗",
    RA.disp_feasible(_cp, _cn, "melee", 0.95) and not RA.disp_feasible(_cp, _cn, "melee", 1.25),
)
# 2 正 0 负（系数 0.99）同样可反推
_iv2 = RA.disposition_interval([("crit_damage", 89.1), ("range", 1.92)], [], "melee")
check(
    "2 正 0 负系数下反推含 1.0",
    _iv2[0] is not None and _iv2[0] <= 1.0 <= _iv2[1] and not (_iv2[0] <= 1.25 <= _iv2[1]),
    str(_iv2),
)
# 显示舍入余量：1 位小数的词条必须有 ±0.05 容差，否则 +1.6m 会误杀
check(
    "显示容差 range=0.05 / initial_combo=0.5",
    RA.display_tol("range", 1.6) == 0.05 and RA.display_tol("initial_combo", 24) == 0.5,
)
# 数值互相矛盾（暴伤像 0.95、范围像 1.25）→ 无可行区间，不瞎判
check(
    "矛盾数值返回 None",
    RA.disposition_interval([("crit_damage", 82.8), ("range", 2.27)], [], "melee") == (None, None),
)
check(
    "无可用基值返回 None",
    RA.disposition_interval([("不存在的词条", 5)], [], "melee") == (None, None),
)

# ------------------------------------------- 事件循环阻塞修复（2026-09-13）
# 背景：AstrBot 是单事件循环，插件的同步重活会把整台机器人卡住 ——
# 线上 watchdog 记录到 13 次 stall（最长 89s），其中 4 次栈顶是我们的
# de_worldstate._load 读盘、1 次是 PIL 渲染。以下 4 条把修法钉住。
import inspect as _inspect  # noqa: E402
import ast as _ast  # noqa: E402

from core import de_worldstate as _DW  # noqa: E402

_DW._load.cache_clear()
_DW._load("solNodes.json")
_DW._load("nodes_zh.json")
_info = _DW._load.cache_info()
check(
    "静态表缓存必须全量（14 个文件轮流命中，maxsize=1 等于没缓存）",
    _info.maxsize is None and _info.currsize == 2,
    f"maxsize={_info.maxsize} currsize={_info.currsize}",
)
check(
    "世界状态解析走线程池",
    "asyncio.to_thread(de_worldstate.parse_worldstate"
    in (ROOT / "core" / "api_client.py").read_text(encoding="utf-8"),
)
check(
    "_build_results 是异步生成器（渲染改在线程池跑）",
    _inspect.isasyncgenfunction(plugin.WarframeSDJK._build_results),
)
_main_src = (ROOT / "main.py").read_text(encoding="utf-8")
# 检查语法结构，不依赖空白或线程池调用是否位于共用包装方法。
_main_tree = _ast.parse(_main_src)
_parents = {child: node for node in _ast.walk(_main_tree) for child in _ast.iter_child_nodes(node)}
_render_refs = [
    node
    for node in _ast.walk(_main_tree)
    if isinstance(node, _ast.Attribute) and _ast.unparse(node) == "self.renderer.render"
]


def _render_in_thread(node):
    call = _parents.get(node)
    return (
        isinstance(call, _ast.Call)
        and _ast.unparse(call.func) == "asyncio.to_thread"
        and bool(call.args)
        and call.args[0] is node
        and isinstance(_parents.get(call), _ast.Await)
    )


_build_tree = next(
    node
    for node in _ast.walk(_main_tree)
    if isinstance(node, _ast.AsyncFunctionDef) and node.name == "_build_content_results"
)
_wrapped_calls = [
    node
    for node in _ast.walk(_build_tree)
    if isinstance(node, _ast.Await)
    and isinstance(node.value, _ast.Call)
    and _ast.unparse(node.value.func) == "self._render_card"
]
check(
    "渲染无残留同步调用",
    bool(_render_refs)
    and all(_render_in_thread(node) for node in _render_refs)
    and len(_wrapped_calls) == 2,
    f"线程渲染引用={len(_render_refs)}，单页/多页包装入口={len(_wrapped_calls)}",
)

# ---------------------------------------------------------------------------
# ⑥ 价格排行自动刷新：三态分支 + 静默死亡修复（2026-10-03 交办）
#    事故：`RANKS_FILE = api_client.RANKS_FILE`（该常量不存在）且在 try 之外
#    ⇒ 协程创建即死于 AttributeError、零日志（榜单陈旧 8 天、48h 内日志 0 条）。
# ---------------------------------------------------------------------------
import asyncio as _aio  # noqa: E402
import datetime as _dt  # noqa: E402
import inspect as _ins  # noqa: E402

_real_dt_cls = _dt.datetime


class _FakeDT:
    """受控 datetime：now() 返回固定小时；fromisoformat 走真实现。"""

    hour = 3

    @classmethod
    def now(cls, tz=None):
        base = _real_dt_cls(2026, 10, 3, cls.hour, 30, 0)
        return base.replace(tzinfo=tz) if tz else base

    @classmethod
    def fromisoformat(cls, s):
        return _real_dt_cls.fromisoformat(s)


class _StopLoop(BaseException):
    """哨兵：让 autoloop 的第一次 sleep 立刻中断（BaseException 不被 except 吞）。"""


class _RecRank:
    def __init__(self):
        self.records = []

    def _rec(self, level):
        def _f(fmt, *a):
            self.records.append((level, fmt % a if a else fmt))

        return _f

    def __getattr__(self, name):
        if name in ("info", "warning", "debug", "error", "exception"):
            return self._rec(name)
        raise AttributeError(name)

    def has(self, level, needle):
        return any(lv == level and needle in m for lv, m in self.records)


class _RankLoopClient:
    """只服务 _rank_autoloop 的假客户端：句柄数据 + 抓取记录。"""

    def __init__(self, data):
        self.data = data
        self.crawls = []

    def _load_json_file(self, path):
        return self.data

    async def crawl_wm_ranks(self, limit=None):
        self.crawls.append(limit)
        return 2595


async def _run_loop_once(data, hour):
    obj = plugin.WarframeSDJK.__new__(plugin.WarframeSDJK)
    obj.client = _RankLoopClient(data)
    _FakeDT.hour = hour
    rec = _RecRank()
    old_logger, old_dt, old_sleep = plugin.logger, _dt.datetime, _aio.sleep
    plugin.logger, _dt.datetime = rec, _FakeDT

    async def _stop(_delay, *a, **k):
        raise _StopLoop()

    _aio.sleep = _stop
    try:
        try:
            await obj._rank_autoloop()
        except _StopLoop:
            pass
    finally:
        plugin.logger, _dt.datetime, _aio.sleep = old_logger, old_dt, old_sleep
    return obj.client, rec


_STALE = {"ts": "2026-09-24T01:53:48+00:00", "cursor": 0, "total": 3240, "rows": {}}
_FRESH = {
    "ts": _real_dt_cls.now(_dt.timezone.utc).isoformat(),
    "cursor": 0,
    "total": 3240,
    "rows": {},
}

_c1, _r1 = _aio.run(_run_loop_once(_STALE, 3))
check(
    "★ 自动刷新（过期 + 闲时 3 点）⇒ 确实开爬且带 limit=900", _c1.crawls == [900], str(_c1.crawls)
)
check(
    "…且日志含「开始自动重建」与「本轮抓取结束」",
    _r1.has("info", "开始自动重建") and _r1.has("info", "本轮抓取结束"),
    str(_r1.records),
)
_c2, _r2 = _aio.run(_run_loop_once(_FRESH, 3))
check("榜单新鲜（48h 内）⇒ 不抓取（不打扰 WM 限速）", _c2.crawls == [], str(_c2.crawls))
_c3, _r3 = _aio.run(_run_loop_once(_STALE, 18))
check(
    "★ 过期但非闲时（18 点）⇒ 不开爬，日志写明凌晨窗口",
    _c3.crawls == [] and _r3.has("info", "非闲时"),
    str(_c3.crawls) + " | " + str(_r3.records),
)

# ★ 2026-10-25（Astra 时区专项）：闲时按北京时间判断。伪造「UTC 宿主」：now() 无 tz 时给 UTC 本地小时，
#   带 tz 时按真实时区换算 ⇒ UTC 19 点 = 北京 3 点应开爬；UTC 3 点 = 北京 11 点不应开爬。
_orig_now = _FakeDT.now


def _utc_host_now(cls, tz=None):
    base = _real_dt_cls(2026, 10, 3, cls.hour, 30, 0, tzinfo=_dt.timezone.utc)
    return base.astimezone(tz) if tz else base.replace(tzinfo=None)


_FakeDT.now = classmethod(_utc_host_now)
try:
    _c4, _r4 = _aio.run(_run_loop_once(_STALE, 19))
    _c5, _r5 = _aio.run(_run_loop_once(_STALE, 3))
finally:
    _FakeDT.now = _orig_now
check("★ UTC 宿主 19 点（北京 3 点）⇒ 开爬", _c4.crawls == [900], str(_c4.crawls))
check(
    "★ UTC 宿主 3 点（北京 11 点）⇒ 不开爬（原先按服务器本地时间会在北京上午开爬）",
    _c5.crawls == [] and _r5.has("info", "北京时间 11 点"),
    str(_c5.crawls) + " | " + str(_r5.records)[:200],
)

_src_loop = _ins.getsource(plugin.WarframeSDJK._rank_autoloop)
# 只看**代码行**（注释里为说明事故会引用旧写法 api_client.RANKS_FILE）
_code_lines = [
    ln for ln in _src_loop.splitlines() if ln.strip() and not ln.lstrip().startswith("#")
]
check(
    "★ 源码接线：RANKS_FILE 经 core_paths.read_path 解析为绝对路径",
    any("core_paths.read_path(api_client.RANKS_NAME)" in ln for ln in _code_lines)
    and not any("api_client.RANKS_FILE" in ln for ln in _code_lines),
    str([ln for ln in _code_lines if "RANKS_FILE" in ln])[:200],
)
check(
    "★ 解析语句在 try 内（异常不再冒泡出协程）",
    _src_loop.index("while True:") < _src_loop.index("RANKS_FILE ="),
    "位置不对",
)
_src_init = _ins.getsource(plugin.WarframeSDJK.initialize)
check(
    "★ 加固：四个后台任务都挂了 done-callback",
    _src_init.count("_log_task_death(") >= 4,
    str(_src_init.count("_log_task_death(")),
)


async def _dead_task():
    raise RuntimeError("boom")


async def _check_death_cb():
    rec = _RecRank()
    old = plugin.logger
    plugin.logger = rec
    try:
        t = _aio.get_running_loop().create_task(_dead_task())
        t.add_done_callback(plugin.WarframeSDJK._log_task_death("_probe"))
        try:
            await t
        except RuntimeError:
            pass
        await _aio.sleep(0)  # 让 done-callback 跑完
        t2 = _aio.get_running_loop().create_task(_aio.sleep(3600))
        t2.add_done_callback(plugin.WarframeSDJK._log_task_death("_probe2"))
        t2.cancel()
        try:
            await t2
        except _aio.CancelledError:
            pass
        await _aio.sleep(0)
    finally:
        plugin.logger = old
    return rec


_rdeath = _aio.run(_check_death_cb())
check(
    "★ 后台任务异常退出 ⇒ WARNING 留痕（不再是静默死亡）",
    _rdeath.has("warning", "RuntimeError"),
    str(_rdeath.records),
)
check(
    "主动取消（CancelledError）⇒ 不告警（关插件时不该刷警告）",
    not any(lv == "warning" and "_probe2" in m for lv, m in _rdeath.records),
    str(_rdeath.records),
)

print()

# ★ 2026-10-25（维护者定：均衡点 Steam Pinnacle Pack 1600p 疑似 RMT）：48h 成交不足 3 笔不进榜
import core.formatters as _Fv  # noqa: E402

_rows_v = [
    {"zh": "均衡点 (Steam Pinnacle Pack)", "tags": ["mod"], "median48": 1600, "vol48": 1},
    {"zh": "膛室 Prime", "tags": ["mod"], "median48": 244.8, "vol48": 20},
]
_tv, _lv = _Fv.fmt_rank_table("卡", _rows_v)
check(
    "★ 成交不足 3 笔的天价单不进榜、注脚写明",
    not any("Steam Pinnacle" in x for x in _lv)
    and any("膛室 Prime" in x for x in _lv)
    and any("不足 3 笔" in x for x in _lv),
    str(_lv),
)


async def t_weekly_platform_isolation():
    """真实解析/handler/HTTP/落盘：平台别名、暖缓存、旧缓存和PC回落。"""
    import json
    import tempfile
    import time
    from unittest.mock import Mock

    import httpx

    from core import paths
    from core.api_client import WarframeClient
    from core.commands.market import MarketCommands
    from core.parser import parse

    prices = {"PC": 11, "PS4": 22, "XB1": 33, "SWITCH": 44}
    previous = paths._RUN
    calls, failing, revision = [], set(), [0]

    def respond(request):
        url = str(request.url)
        if "weeklyRivens" in url:
            plat = next(p for p in prices if f"weeklyRivens{p}.json" in url)
            calls.append(plat)
            if plat in failing:
                return httpx.Response(404)
            return httpx.Response(
                200,
                json=[
                    {
                        "itemType": "Rifle Riven Mod",
                        "compatibility": "Rubico",
                        "rerolled": False,
                        "pop": 10,
                        "median": prices[plat] + revision[0],
                    }
                ],
            )
        if "/riven/weapons" in url:
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "slug": "rubico",
                            "gameRef": "fixture",
                            "i18n": {"en": {"name": "Rubico"}, "zh-hans": {"name": "绝路"}},
                            "rivenType": "rifle",
                        }
                    ]
                },
            )
        if "/statistics" in url:
            return httpx.Response(
                200,
                json={
                    "payload": {
                        "statistics_closed": {
                            "48hours": [{"median": 44, "min_price": 40, "max_price": 48}],
                            "90days": [],
                        }
                    }
                },
            )
        return httpx.Response(200, json={"data": []})

    async def make():
        c = WarframeClient(flare_enabled=False)
        await c.http.aclose()
        c._http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        return c

    def snapshot(plat):
        return {
            "platform": plat,
            "fetched": time.time(),
            "entries": [{"compatibility": "Rubico", "median": prices[plat]}],
        }

    def seed(name, data):
        paths.write_path(name).write_text(json.dumps(data))

    try:
        with tempfile.TemporaryDirectory(prefix="wf-weekly-platform-") as directory:
            root = Path(directory)
            for code, plat in (("pc", "PC"), ("ps", "PS4"), ("xb", "XB1"), ("sw", "SWITCH")):
                paths.set_run_dir(root / code)
                calls.clear()
                c = await make()
                try:
                    h = MarketCommands()
                    h.client, h.page_size = c, 10
                    q = parse("紫卡排行 -" + code)
                    reply = await h._h_riven_rank(q, None, q.platform)
                    check(
                        f"周报平台 {code}：真实解析和handler请求对应官方文件",
                        calls == [plat] and any(f"{prices[plat]}p" in x for x in reply.lines),
                        str(calls),
                    )
                finally:
                    await c.close()

            paths.set_run_dir(root / "mixed")
            calls.clear()
            c = await make()
            try:
                order = ("pc", "ps4", "PS4", "ps", "xb1", "XB1", "xb", "sw", "SWITCH", "pc")
                results = [await c.de_weekly_rivens(code) for code in order]
                check(
                    "周报平台：别名与大小写等价、不同平台暖缓存不串",
                    [x["platform"] for x in results]
                    == ["PC", "PS4", "PS4", "PS4", "XB1", "XB1", "XB1", "SWITCH", "SWITCH", "PC"],
                    str([x["platform"] for x in results]),
                )
                check("周报平台：十次请求只为四个平台各回源一次", calls == list(prices), str(calls))
                pc_before = paths.write_path("riven_weekly.json").read_bytes()
                revision[0] = 5
                refreshed = await c.de_weekly_rivens("ps", force=True)
                check(
                    "周报平台：主机强刷不覆盖PC兼容文件",
                    refreshed["platform"] == "PS4"
                    and refreshed["entries"][0]["median"] == 27
                    and paths.write_path("riven_weekly.json").read_bytes() == pc_before,
                )
                revision[0] = 0
            finally:
                await c.close()

            for old_plat in ("PC", "PS4"):
                paths.set_run_dir(root / f"legacy-{old_plat}")
                calls.clear()
                seed("riven_weekly.json", snapshot(old_plat))
                c = await make()
                try:
                    result = await c.de_weekly_rivens("pc" if old_plat == "PC" else "ps4")
                    check(
                        f"周报平台：旧{old_plat}快照仅在平台匹配时复用",
                        result["platform"] == old_plat and not calls,
                    )
                    other = await c.de_weekly_rivens("ps" if old_plat == "PC" else "pc")
                    check(
                        f"周报平台：旧{old_plat}快照不冒充其它平台",
                        other["platform"] == ("PS4" if old_plat == "PC" else "PC")
                        and len(calls) == 1,
                    )
                finally:
                    await c.close()

            paths.set_run_dir(root / "wrong-platform")
            calls.clear()
            seed("_cache/riven_weekly_ps4.json", snapshot("PC"))
            seed("riven_weekly.json", snapshot("PC"))
            c = await make()
            try:
                result = await c.de_weekly_rivens("ps4")
                check(
                    "周报平台：缓存文件路径正确但payload平台错误也拒绝",
                    result["platform"] == "PS4" and calls == ["PS4"],
                )
            finally:
                await c.close()

            paths.set_run_dir(root / "fallback")
            calls.clear()
            failing.add("SWITCH")
            c = await make()
            try:
                result = await c.de_weekly_rivens("sw")
                check(
                    "周报平台：主机源失败显式回PC且不污染主机缓存",
                    result["platform"] == "PC"
                    and result.get("fallback_from") == "SWITCH"
                    and calls == ["SWITCH", "PC"]
                    and not paths.write_path("_cache/riven_weekly_switch.json").exists(),
                )
                pc = json.loads(paths.write_path("riven_weekly.json").read_text())
                check("周报平台：PC原始缓存不被回落元数据污染", "fallback_from" not in pc)
                failing.clear()
                restored = await c.de_weekly_rivens("sw")
                check(
                    "周报平台：主机源恢复后立即返回主机数据",
                    restored["platform"] == "SWITCH" and calls == ["SWITCH", "PC", "SWITCH"],
                )
            finally:
                failing.clear()
                await c.close()

            for plat in ("pc", "sw"):
                for fallback in (False, True) if plat == "sw" else (False,):
                    paths.set_run_dir(root / f"overlay-{plat}-{fallback}")
                    seed(
                        "riven_rt.json",
                        {"ts": "2026-10-30T00:00:00Z", "rows": {"rubico": {"median": 120}}},
                    )
                    failing.update(["SWITCH"] if fallback else [])
                    c = await make()
                    try:
                        h = MarketCommands()
                        h.client, h.page_size = c, 10
                        h._is_admin = lambda event: True
                        c.start_riven_rt_crawl = Mock(return_value=("started", 1, 2))
                        q = parse("紫卡排行 -" + plat)
                        reply = await h._h_riven_rank(q, None, q.platform)
                        compared = any(
                            "WM挂单" in x and "变化" in x
                            for x in reply.lines
                            if not x.startswith("※")
                        )
                        check(
                            f"周报平台 {plat}/{fallback}：只有PC周报叠加PC现价",
                            compared is (plat == "pc" or fallback),
                        )
                        if fallback:
                            check(
                                "周报平台：真实handler明示主机源回PC",
                                any(
                                    "回落" in x and "PC" in x for x in reply.lines + [reply.footer]
                                ),
                            )
                        if plat == "sw" and not fallback:
                            check(
                                "周报平台：主机视图的现价引导明确转PC",
                                any("紫卡排行 -pc" in x for x in reply.lines),
                            )
                        if fallback:
                            q = parse("紫卡排行 未开 -sw")
                            veiled_reply = await h._h_riven_rank(q, None, q.platform)
                            check(
                                "周报平台：未开实时成交仍用请求平台且不误标PC周报回落",
                                "SWITCH" in veiled_reply.footer
                                and "warframe.market" in veiled_reply.footer
                                and not any("回落 PC" in x for x in veiled_reply.lines),
                            )
                        q = parse("紫卡排行 刷新 -" + plat)
                        reply = await h._h_riven_rank(q, None, q.platform)
                        check(
                            f"周报平台 {plat}/{fallback}：管理员只在PC请求启动PC现价抓取",
                            c.start_riven_rt_crawl.call_count == int(plat == "pc"),
                        )
                    finally:
                        failing.clear()
                        await c.close()
    finally:
        paths.set_run_dir(previous)


_aio.run(t_weekly_platform_isolation())

if FAILED:
    print(f"✗ {len(FAILED)} 项失败：" + "、".join(FAILED))
    sys.exit(1)
print("✓ 全部通过")
