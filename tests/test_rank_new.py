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
    return {"slug": slug, "zh": zh, "en": slug, "tags": tags,
            "median48": m48, "min48": mn, "max48": mx,
            "median_prev": mprev, "vol48": 3, "vol90": 9}


# --------------------------------------------------------------- 裸排行
from core import formatters as F  # noqa: E402

rows = [
    _row("messa_set", "绯红五刃 一套", ["warframe", "prime", "set"], m48=90, mn=88, mx=95, mprev=99),
    _row("mesa_p_set", "Mesa Prime 一套", ["warframe", "prime", "set"], m48=120, mn=115, mx=130, mprev=133),
    _row("no_48h", "仅90天数据的甲", ["warframe", "prime", "set"], m48=0, mprev=15),  # 无当前价不入榜
    _row("no_sale", "无成交甲", ["warframe", "prime", "set"]),                      # 无成交不入榜
    _row("gram", "格拉姆", ["weapon", "melee"], m48=30, mn=28, mx=35, mprev=41),
    _row("laetum", "拉特姆", ["weapon", "secondary"], m48=55, mn=50, mx=60, mprev=66),
    _row("nataruk", "纳塔鲁克", ["weapon", "primary"], m48=45, mn=40, mx=50, mprev=58),
    _row("primed_cont", " Prime 连续", ["mod"], m48=210, mn=200, mx=220, mprev=230),
    _row("arcane_eng", "赋能·狂怒", ["arcane_enhancement"], m48=310, mn=300, mx=320, mprev=330),
]
title, lines = F.fmt_rank_overview(rows)
check("裸排行标题", title == "价格排行榜", title)
check("三组各前五", sum(1 for ln in lines if ln.startswith("◆ ")) == 3, str(lines[:12]))
check("Mesa Prime 榜首（120 > 90）",
      any("Mesa Prime" in ln and "120p" in ln for ln in lines), str(lines[:8]))
check("无当前价（仅90d）不入榜", not any("仅90天数据的甲" in ln for ln in lines))
check("无成交物品不入榜", not any("无成交甲" in ln for ln in lines))
check("赋能不进甲组", not any("赋能·狂怒" in ln for ln in lines))
check("引导语存在", any("完整 20 名榜单" in ln for ln in lines))
check("历史列=上期中位", any("上期中位" in ln for ln in lines))

# --------------------------------------------------------------- 分类榜
title2, lines2 = F.fmt_rank_table("主武", rows)
check("分类榜标题", title2 == "主武价格排行", title2)
check("主武榜不含近战/副武",
      all("格拉姆" not in ln and "拉特姆" not in ln for ln in lines2), str(lines2))
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
check("周报字段完整", entries[0]["itemType"] == "Melee Riven Mod"
      and entries[3]["compatibility"] is None and entries[1]["pop"] == 30)

snap = {"fetched": 1789200000.0, "platform": "PC", "entries": entries}
zhmap = {"gram": "格拉姆"}
tw, lw = F.fmt_riven_weekly(snap, zhmap)
check("周榜标题", tw == "紫卡热度排行榜", tw)
check("周榜组数（近战+空战，无数据组跳过）",
      sum(1 for ln in lw if ln.startswith("◆ ")) == 2, str(lw))
check("空战组入榜", any("Larchibra" in ln for ln in lw), str(lw))
check("未开不再出现在周榜数据行",
      not any("未开" in ln for ln in lw if not ln.startswith("※")))
check("格拉姆 0洗中位 30p", any("格拉姆" in ln and "30p" in ln for ln in lw), str(lw))
check("参考价 = 较低中位（30 < 45）",
      any("格拉姆" in ln and ln.rstrip().endswith("30p") for ln in lw), str(lw))
check("刷新提示存在", any("紫卡排行 刷新" in ln for ln in lw))

# 未开单类榜（veiled_wm 主路径）：WM 实时价
_veiled_wm = {"Rifle": {"median": 10, "min": 4, "max": 30, "vol": 46},
              "Melee": {"median": 6, "min": 5, "max": 9, "vol": 50},
              "Kitgun": {"median": 2, "min": 2, "max": 3, "vol": 4}}
tv, lv = F.fmt_riven_type("未开", snap, zhmap, veiled_wm=_veiled_wm)
check("未开榜用 WM 实时价（步枪 10p）",
      any("步枪未开紫卡" in ln and "10p" in ln for ln in lv), str(lv))
check("未开榜含最低/最高列",
      any("当前中位" in ln and "最低" in ln for ln in lv), str(lv[:3]))
check("未开榜来源标注 WM", any("warframe.market 实时成交" in ln for ln in lv))
check("未开榜按中位降序（步枪 10 > 近战 6 > 组合枪 2）",
      lv.index([x for x in lv if "步枪未开" in x][0])
      < lv.index([x for x in lv if "近战未开" in x][0])
      < lv.index([x for x in lv if "组合枪未开" in x][0]), str(lv))
check("未开榜不再出现 DE 周报 590/600p",
      not any("590p" in ln or "600p" in ln for ln in lv), str(lv))

# DE 周报回退路径（无 veiled_wm）
tu, lu = F.fmt_riven_type("未开", snap, zhmap)
check("未开榜标题", tu == "紫卡热度·未开", tu)
check("未开榜按类型列出", any("近战未开紫卡" in ln and "8p" in ln for ln in lu)
      and any("步枪未开紫卡" in ln and "28p" in ln for ln in lu), str(lu))
check("未开榜热度降序（步枪 3% > 近战 1%）",
      lu.index([x for x in lu if "步枪未开" in x][0])
      < lu.index([x for x in lu if "近战未开" in x][0]), str(lu))

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
check("低洗解析 → max 8 / min 1",
      q.max_rerolls == 8 and q.rerolls_min == 1,
      f"{q.max_rerolls}/{q.rerolls_min}")
q2 = P.parse_wr(["格拉姆", "废洗"])
check("废洗解析 → min 10 / 无上限",
      q2.rerolls_min == 10 and q2.max_rerolls is None,
      f"{q2.max_rerolls}/{q2.rerolls_min}")
q3 = P.parse_wr(["格拉姆", "零洗"])
check("零洗不变 → 0/0",
      q3.rerolls_min == 0 and q3.max_rerolls == 0,
      f"{q3.max_rerolls}/{q3.rerolls_min}")

# --------------------------------------------------------------- 紫卡分析
from core import riven_analysis as RA  # noqa: E402

# 测试卡（用户实例）：棱晶·欧玛 3+1 倾向 0.95，暴伤 82.8 → 区间 72.14-88.17
lo, hi = RA.stat_range("crit_damage", "melee", 0.95, 3, 1)
check("暴伤区间（与参考卡 72.1-88.2 一致）",
      abs(lo - 72.14) < 0.01 and abs(hi - 88.17) < 0.01, f"{lo}-{hi}")
lo2, hi2 = RA.stat_range("slide_crit", "melee", 0.95, 3, 1, negative=True)
check("负词条系数（滑暴 76.95-94.05）",
      abs(lo2 - 76.95) < 0.01 and abs(hi2 - 94.05) < 0.01, f"{lo2}-{hi2}")
check("距中计算", RA.deviation_pct(82.8, lo, hi) == 3.3,
      str(RA.deviation_pct(82.8, lo, hi)))
check("基值缺失返回空区间", RA.stat_range("zoom", "melee", 1.0, 3, 1) == (None, None))
ta, la = F.fmt_riven_analysis("棱晶·欧玛", 0.95, "melee",
                              [("crit_damage", 82.8), ("range", 1.6),
                               ("attack_speed", 45.8)],
                              [("slide_crit", 81.3)])
check("分析卡倾向行", any("倾向 0.95" in ln and "●●●○○" in ln for ln in la), str(la))
check("分析卡区间行", any("72.14%-88.17%" in ln for ln in la), str(la))
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
check("valence 解析 tenet（含 &nbsp;）",
      _v["tenet"].get("Tenet Ferrox") == ("Magnetic", 25.7)
      and _v["tenet"].get("Tenet Exec") == ("Cold", 53.3), str(_v["tenet"]))
check("valence 批次取表头标注（B）而非 is selling（A）",
      _v["coda_batch"] == "B", str(_v["coda_batch"]))
check("valence 解析 coda", _v["coda"].get("Coda Bubonico") == ("Heat", 31.2))
check("valence 残缺时不瞎判批", _WC.parse_wiki_valence("<p>xx</p>")["coda_batch"] == "")

# ---------------------------------------------- wiki 倾向表 HTML 解析
_disp_html = """<table>
<tr><td><a>Kuva Chakkhurr</a></td><td>(0.95)</td></tr>
<tr><td>Prisma Ohma</td><td>(0.95)</td></tr>
<tr><td>Boltor</td><td>(1.3)</td></tr>
<tr><td>Some Weapon</td><td>(99)</td></tr>
</table>"""
_disp = _WC.parse_wiki_dispositions(_disp_html)
check("倾向表解析（含值域过滤）",
      _disp.get("prisma ohma") == 0.95 and _disp.get("kuva chakkhurr") == 0.95
      and "some weapon" not in _disp, str(_disp))
check("倾向表 Boltor 1.3", _disp.get("boltor") == 1.3, str(_disp))

# ------------------------------------------- 武器家族匹配（变体提示用）
check("家族匹配 棱晶·欧玛",
      _WC._family_match("欧玛", "Ohma", "棱晶·欧玛", "Prisma Ohma"))
check("家族匹配 英文后缀（Prisma Ohma）",
      _WC._family_match("", "Ohma", "", "Prisma Ohma"))
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
_st.register = lambda *a, **k: (lambda c: c)
_api.AstrBotConfig = dict
_api.logger = _Logger()
sys.modules.setdefault("astrbot", _pkg)
sys.modules["astrbot.api"] = _api
sys.modules["astrbot.api.event"] = _ev
sys.modules["astrbot.api.message_components"] = _mc
sys.modules["astrbot.api.star"] = _st

import main as plugin  # noqa: E402
rev2 = {v: k for k, v in P.RIVEN_STAT_ZH.items()}
pos2, neg2 = plugin.WarframeQuery._normalize_llm_stats(
    {"positive": [["暴伤", "82.8"], ["范围", "1.6m"], ["攻击速度", 45.8]],
     "negative": [["滑行暴击", "81.3%"]]}, rev2)
check("LLM 词条规范化：暴伤 82.8", pos2[0] == ("crit_damage", 82.8), str(pos2))
check("LLM 词条规范化：范围 1.6", pos2[1] == ("range", 1.6), str(pos2))
check("LLM 词条规范化：攻击速度→攻速", pos2[2] == ("attack_speed", 45.8), str(pos2))
check("LLM 词条规范化：负词条滑暴 81.3", neg2 == [("slide_crit", 81.3)], str(neg2))
pos3, neg3 = plugin.WarframeQuery._normalize_llm_stats(
    {"positive": [["未知词条", 5]]}, rev2)
check("LLM 未知识别不了的词条被丢弃", not pos3, str(pos3))

# ------------------------------------------- 数值反推倾向（变体自动判定）
# 实测卡：欧玛卡面 +82.8 暴伤 / +1.6 范围 / +45.8 攻速 / -81.3 滑暴
# （玩家把它装在棱晶·欧玛上；卡面只写母武器名，变体信息不在图里）
_cp = [("crit_damage", 82.8), ("range", 1.6), ("attack_speed", 45.8)]
_cn = [("slide_crit", 81.3)]
_iv = RA.disposition_interval(_cp, _cn, "melee")
check("反推区间含 0.95（棱晶）", _iv[0] is not None and _iv[0] <= 0.95 <= _iv[1],
      str(_iv))
check("反推区间排除 1.25（母武器）",
      _iv[0] is not None and not (_iv[0] <= 1.25 <= _iv[1]), str(_iv))
check("家族匹配唯一命中棱晶·欧玛",
      RA.match_disposition(_cp, _cn, "melee",
                           [("欧玛", 1.25), ("棱晶·欧玛", 0.95)])
      == [("棱晶·欧玛", 0.95)])
check("倾向可行性 0.95 ✓ / 1.25 ✗",
      RA.disp_feasible(_cp, _cn, "melee", 0.95)
      and not RA.disp_feasible(_cp, _cn, "melee", 1.25))
# 同族母武器档位（1.25）的数值 → 唯一命中母武器，不能反过来误判成变体
_p2 = [("crit_damage", 105.5), ("range", 2.27), ("attack_speed", 64.3)]
_n2 = [("slide_crit", 106.9)]
check("1.25 档数值唯一命中欧玛",
      RA.match_disposition(_p2, _n2, "melee",
                           [("欧玛", 1.25), ("棱晶·欧玛", 0.95)]) == [("欧玛", 1.25)])
# 2 正 0 负（系数 0.99）同样可反推
_iv2 = RA.disposition_interval([("crit_damage", 89.1), ("range", 1.92)], [], "melee")
check("2 正 0 负系数下反推含 1.0",
      _iv2[0] is not None and _iv2[0] <= 1.0 <= _iv2[1]
      and not (_iv2[0] <= 1.25 <= _iv2[1]), str(_iv2))
# 显示舍入余量：1 位小数的词条必须有 ±0.05 容差，否则 +1.6m 会误杀
check("显示容差 range=0.05 / initial_combo=0.5",
      RA.display_tol("range", 1.6) == 0.05
      and RA.display_tol("initial_combo", 24) == 0.5)
# 数值互相矛盾（暴伤像 0.95、范围像 1.25）→ 无可行区间，不瞎判
check("矛盾数值返回 None",
      RA.disposition_interval([("crit_damage", 82.8), ("range", 2.27)],
                              [], "melee") == (None, None))
check("无可用基值返回 None",
      RA.disposition_interval([("不存在的词条", 5)], [], "melee") == (None, None))

# ------------------------------------------- 事件循环阻塞修复（2026-09-13）
# 背景：AstrBot 是单事件循环，插件的同步重活会把整台机器人卡住 ——
# 线上 watchdog 记录到 13 次 stall（最长 89s），其中 4 次栈顶是我们的
# de_worldstate._load 读盘、1 次是 PIL 渲染。以下 4 条把修法钉住。
import inspect as _inspect                      # noqa: E402

from core import de_worldstate as _DW           # noqa: E402

_DW._load.cache_clear()
_DW._load("solNodes.json")
_DW._load("nodes_zh.json")
_info = _DW._load.cache_info()
check("静态表缓存必须全量（14 个文件轮流命中，maxsize=1 等于没缓存）",
      _info.maxsize is None and _info.currsize == 2,
      f'maxsize={_info.maxsize} currsize={_info.currsize}')
check("世界状态解析走线程池",
      'asyncio.to_thread(de_worldstate.parse_worldstate'
      in (ROOT / 'core' / 'api_client.py').read_text(encoding='utf-8'))
check("_build_results 是异步生成器（渲染改在线程池跑）",
      _inspect.isasyncgenfunction(plugin.WarframeQuery._build_results))
_main_src = (ROOT / 'main.py').read_text(encoding='utf-8')
# ⚠️ 别写死单行字符串：调用被格式化成多行后
#   `await asyncio.to_thread(\n    self.renderer.render, ...)`
# 会让 `'asyncio.to_thread(self.renderer.render' in src` 恒为假 → 假失败。
# 先把连续空白归一成单空格再匹配，保住「走线程池 + 无裸调用」的原意。
_src_norm = ' '.join(_main_src.split())
check("渲染无残留同步调用",
      'asyncio.to_thread( self.renderer.render' in _src_norm
      and '= self.renderer.render(' not in _main_src)

print()
if FAILED:
    print(f"✗ {len(FAILED)} 项失败：" + "、".join(FAILED))
    sys.exit(1)
print("✓ 全部通过")
