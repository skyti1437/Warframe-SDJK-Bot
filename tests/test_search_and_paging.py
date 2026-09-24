# -*- coding: utf-8 -*-
"""本地检索、遗物出处合并、分页工具的回归断言。

背景：
- 「物品 / wiki / 掉落」原先只查 warframe.market 的**可交易品**字典，
  Forma / 赤毒 / 内融核心 这类天然查不到（搜啥啥不出）。
- WM 兜底解析用纯子串匹配，``Forma`` 会命中 ``valence_formation``（效价炼成）。
- 遗物列表写死「以下仅列举前 10」，长列表没有翻页。

这些都属于「静默生效」的逻辑，必须靠断言锁住。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = ""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}"
          + (f"  -> {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


from core import drops as D  # noqa: E402
from core import formatters as F  # noqa: E402
from core import search as S  # noqa: E402

# ---------------------------------------------------------------- 统一检索
# 这些词原本一个都搜不到（WM 字典里没有）
for q, expect in [
    ("Forma", "Forma"),
    ("forma", "Forma"),
    ("赤毒", "赤毒"),
    ("氩结晶", "氩结晶"),
    ("膛线", "膛线"),
    # ★ 2026-09-20 修正：以前期望「压迫点」是把词典的错误映射固化成了断言。
    #   官方简中里 Serration = 「膛线」，Pressure Point = 「压迫点」，两者不能混。
    ("serration", "膛线"),
]:
    hit = S.search(q, limit=1)
    check(f"检索 {q} -> {expect}",
          bool(hit) and hit[0]["name"] == expect,
          str(hit[:1]))

check("检索条目数可观（官方导出已并入）", len(S._index()) > 10000,
      str(len(S._index())))

# 关键回归：forma 不得再命中 valence_formation
names = [h["name"] for h in S.search("Forma", limit=8)]
check("Forma 不再误命中 效价炼成", "效价炼成" not in names, str(names))
check("Forma 结果里带官方 Forma", "Forma" in names, str(names))

# 词边界：短查询不该被长串的长子串吃掉
check("短查询不匹配内部名", S._score("forma", "valence formation") == -1)
check("精确匹配优先级最高", S._score("forma", "forma") == 0)

# ------------------------------- wiki 页面名：黑话要落到官方简中名（2026-09-24）
# 用户报「wiki 音妈」给不出可用链接：别名条目的展示名就是别名键本身，
# 拼出的 /wiki/音妈 是死链 → wiki_page_name() 必须换成官方简中名。
for _slang, _want in [("音妈", "恸哭女妖"), ("毒妈", "剧毒之触"),
                      ("DJ", "吟游歌者"), ("茶妹", "泊时灵械"),
                      ("玻璃甲", "琉璃仕女")]:
    _h = S.search(_slang, limit=1)
    _pn = S.wiki_page_name(_h[0]) if _h else ""
    check(f"★ 黑话「{_slang}」页面名 → {_want}", _pn == _want, _pn)
check("官方条目页面名保持原样",
      S.wiki_page_name({"name": "Forma", "source": "官方"}) == "Forma")
check("WM 展示名后缀（一套/蓝图）剥掉后归一到官方名",
      S.wiki_page_name({"name": "Banshee Prime 一套"}) == "恸哭女妖",
      S.wiki_page_name({"name": "Banshee Prime 一套"}))
check("查不到官方名时回落别名键（不返回空）",
      S.wiki_page_name({"name": "某不存在词", "en": "zzz nonexistent",
                        "source": "别名"}) == "某不存在词")

# 2026-09-24 联网核对后修正的两条错映射（键是官方名的省略形式，却指向别的物品）
import json  # noqa: E402
_AL = json.loads((ROOT / "core" / "data" / "aliases.json").read_text(encoding="utf-8"))
check("★「充沛」= 赋能·充沛（Arcane Energize），不再指向川流不息 Prime",
      _AL["wm_items"].get("充沛") == "arcane_energize",
      str(_AL["wm_items"].get("充沛")))
check("★「蛇发女妖」不再指向 MOD gorgon_frenzy（官方名是武器的）",
      _AL["wm_items"].get("蛇发女妖") != "gorgon_frenzy",
      str(_AL["wm_items"].get("蛇发女妖")))
# 2026-09-24 自查新增：瞬时狡诈（Fleeting Expertise 旧译）曾错指持久力 Prime
# （primed_continuity，加持续），与「减持续换效率」的本身语义相反。
check("★「瞬时狡诈」= 弹指瞬技（fleeting_expertise），不再错指持久力 Prime",
      _AL["wm_items"].get("瞬时狡诈") == "fleeting_expertise",
      str(_AL["wm_items"].get("瞬时狡诈")))
_h = S.search("瞬时狡诈", limit=1)
check("★ 黑话「瞬时狡诈」页面名 → 弹指瞬技",
      bool(_h) and S.wiki_page_name(_h[0]) == "弹指瞬技",
      S.wiki_page_name(_h[0]) if _h else "")
# 2026-09-24 自查新增：页面名选择规则升级为「官方 MOD 名表命中 > 键最长」，
# 命中官方名的键直接采用官方原始写法（同时修掉旧译与 Prime 缺空格两类死链源）。
check("★ 官方名表命中优先于更长的旧译键（真空吸物 → 吸取）",
      S.wiki_page_name({"name": "真空吸物", "source": "别名"}) == "吸取",
      S.wiki_page_name({"name": "真空吸物", "source": "别名"}))
check("★ 官方写法带空格（持久力Prime → 持久力 Prime）",
      S.wiki_page_name({"name": "持久力Prime", "source": "别名"}) == "持久力 Prime",
      S.wiki_page_name({"name": "持久力Prime", "source": "别名"}))

# ---------------------------------------------------------------- 遗物出处
lines = D.relic_source_lines("neo c11 relic")
check("遗物出处可查且已合并轮次",
      bool(lines) and all("轮）" in x or "轮" not in x for x in lines),
      str(lines[:3]))
check("合并后按节点去重（少于原始条数）",
      len(lines) < len(D.relic_sources("neo c11 relic", limit=200)),
      f"{len(lines)} vs {len(D.relic_sources('neo t11 relic', limit=200))}")
check("limit 生效", len(D.relic_source_lines("neo c11 relic", limit=2)) == 2)
check("未知遗物返回空", D.relic_source_lines("zzz zzz relic") == [])

# 合并示例：同一节点的 A/B/C 轮应折成一条
merged = D.relic_source_lines("neo c11 relic")
check("同节点轮次合并成 A/B/C", any("B/C轮" in x or "A/B/C轮" in x for x in merged),
      str([x for x in merged if "轮" in x][:5]))

# ---------------------------------------------------------------- 分页
rows = [{"cn": f"遗物{i}", "tier": "", "unvaulted": i % 2 == 0,
         "sources": ["水星 · Lares · 防御（B/C轮）"], "source_total": 9}
        for i in range(1, 24)]
t1, l1 = F.fmt_prime_relics(rows, "遗物入库", page=1, page_size=10)
t2, l2 = F.fmt_prime_relics(rows, "遗物入库", page=2, page_size=10)
check("遗物列表标题带页码", "第1/3页" in t1 and "第2/3页" in t2, t1)
check("第 1 页含前 10 条", "1. [已入库] 遗物1" in l1[0], l1[0])
check("第 2 页从第 11 条开始", l2[0].startswith("11."), l2[0])
check("页码越界被夹回末页",
      "第3/3页" in F.fmt_prime_relics(rows, "x", page=99, page_size=10)[0])
check("掉落位置已写入",
      any("掉落位置：" in x for x in l1), str(l1[:4]))

# 杜卡德分档
check("杜卡德档位覆盖 15/25/45/65/100",
      all(v in F._DUCAT_TIER[t]["values"] for t, v in
          (("金", 100), ("银", 45), ("银", 65), ("铜", 15), ("铜", 25))))

# 入侵分页
inv = [{"node": f"节点{i}", "attacker": {"faction": "Grineer", "items": []},
        "defender": {"faction": "Corpus", "items": []}, "completion": 50,
        "count": 1, "goal": 2} for i in range(1, 15)]
ta, la = F.fmt_invasions(inv, page=1, page_size=6)
tb, lb = F.fmt_invasions(inv, page=3, page_size=6)
check("入侵分页：共 3 页", "第1/3页" in la[-3], str(la[-3:]))
check("入侵第 3 页只余 2 场", "第3/3页，共14场" in lb[-3], str(lb[-3:]))

if FAILED:
    print(f"\n失败 {len(FAILED)} 项：{FAILED}")
    sys.exit(1)
print("\n全部通过 ✔")
