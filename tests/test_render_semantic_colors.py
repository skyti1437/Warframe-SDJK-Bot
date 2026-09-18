# -*- coding: utf-8 -*-
"""渲染层「语义配色」回归测试（python3 tests/test_render_semantic_colors.py）

2026-09-12 用户反馈：
  · 「任务类型单独标记加个颜色吧，现在像和后面文字是一块的」
  · 「信条/终幕这个元素和元素加成分别来个其他颜色吧，然后没对齐真的好丑」
  · 「这个等级做的有些不美观」

对应实现全在 `core/render.py`：把任务类型 / 挑战名 / 派系 / 元素 / 加成百分比
切成 token 再上色，等级徽章全卡统一宽度。这里守住最容易被后续改动破坏的三点：

1. **词的左右边界**：中文没有词边界，若不加 `(?:^|(?<=[\\s　｜（]))…(?=[\\s　｜）]|$)`
   这类守卫，「电磁力场装置」里的「磁力」、「致命冲击」里的「冲击」都会被染色，
   看起来像卡出错了。
2. **行首任务类型切分**：官方赏金名自带类型词的档位（刺杀指挥官 / 物资回收）
   不加前缀，靠 `_split_leading_type` 把开头的类型词染色。
3. **元素/加成两列对齐的识别式**（`_PAIR_RE`）：只有它能匹配的行才进列对齐。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import formatters as fmt  # noqa: E402
from core import render as R  # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = ""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f"  -> {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


def toks(text: str) -> list[str]:
    return [m.group(0) for m in R._TOKEN_RE.finditer(text)]


# --------------------------------------------------------------- 词表
_NODES = json.loads((ROOT / "core" / "data" / "de" / "nodes_zh.json")
                    .read_text(encoding="utf-8"))

check("任务类型词表非空", len(R.MISSION_TYPE_WORDS) >= 30,
      str(len(R.MISSION_TYPE_WORDS)))
# 官方 missionName 的全部中文类型都必须在词表里（否则那些档位不会上色）
_official = {v["type"] for v in _NODES.values() if v.get("type")}
_missing = sorted(_official - set(R.MISSION_TYPE_WORDS))
check("节点官方任务类型全部覆盖", not _missing, str(_missing))
# ExportBounties 末阶段映射出来的类型（含官方 MT 里没有的「伏击/资源回收」）也都在
_enc = set(fmt._BOUNTY_ENC_ZH.values())
check("末阶段映射的类型全部覆盖", not (_enc - set(R.MISSION_TYPE_WORDS)),
      str(sorted(_enc - set(R.MISSION_TYPE_WORDS))))
# 长的必须排在前面，否则「资源回收」会被「回收」抢先匹配
check("长词优先（资源回收 在 回收 之前）",
      R.MISSION_TYPE_WORDS.index("资源回收") < R.MISSION_TYPE_WORDS.index("回收"))
check("长词优先（移动防御 在 防御 之前）",
      R.MISSION_TYPE_WORDS.index("移动防御") < R.MISSION_TYPE_WORDS.index("防御"))
check("元素词表含 7 种 Progenitor 元素",
      {"冲击", "火焰", "冰冻", "电击", "毒素", "磁力", "辐射"}
      <= set(R.ELEMENT_WORDS), str(R.ELEMENT_WORDS))
check("每种语义色互不相同", len({R.MISSION_TYPE_COLOR, R.CHALLENGE_COLOR,
                              R.FACTION_COLOR, R.ELEMENT_COLOR,
                              R.BONUS_COLOR}) == 5)

# ------------------------------------------------- 词边界（防误染）
check("整行任务类型被切成 token",
      "歼灭" in toks("　· 歼灭 核心样本｜40-60级"),
      str(toks("　· 歼灭 核心样本｜40-60级")))
check("行首任务类型被切成 token（无左边界也算）",
      toks("歼灭 核心样本")[0] == "歼灭", str(toks("歼灭 核心样本")))
# ⚠️ 负例：这些词出现在**别的词中间**，绝不能被单独染色
check("「电磁力场装置」里的「磁力」不误染",
      "磁力" not in toks("2 × 电磁力场装置"), str(toks("2 × 电磁力场装置")))
check("「致命冲击」里的「冲击」不误染",
      "冲击" not in toks("致命冲击"), str(toks("致命冲击")))
check("「磁力王」里的「磁力」不误染（后面没有边界）",
      "磁力" not in toks("磁力王"), str(toks("磁力王")))
check("「生存物资」里的「生存」不误染",
      "生存" not in toks("生存物资"), str(toks("生存物资")))

# ------------------------------------------------- 元素 / 加成百分比
_elem_toks = toks("· 信条·集议（Tenet Agendus）　毒素 35.3%")
check("元素被切成 token", "毒素" in _elem_toks, str(_elem_toks))
check("加成百分比被切成 token", "35.3%" in _elem_toks, str(_elem_toks))
check("整数百分比也认", "25%" in toks("冲击 25%"), str(toks("冲击 25%")))
check("百分比 token 不吞普通数字",
      not any("%" in t for t in toks("1,500 现金匣、50 内融核心")),
      str(toks("1,500 现金匣、50 内融核心")))

# ------------------------------------------------- 派系
for _f in ("Grineer", "Corpus", "科腐者", "炽蛇军", "合一众", "奥罗金", "低语者"):
    check(f"派系 token：{_f}", _f in toks(f"· 歼灭 粉碎邪教｜50-70级｜{_f}")
          or _f in toks(f"· {_f} 的据点"), str(toks(f"· 歼灭 粉碎邪教｜50-70级｜{_f}")))
check("「合一众」在 ｜ 后仍被切成 token",
      "合一众" in toks("防御 带他们回家｜50-70级｜合一众"),
      str(toks("防御 带他们回家｜50-70级｜合一众")))

# ------------------------------------------------- 行首任务类型切分
check("_split_leading_type：类型 + 名称",
      R._split_leading_type("刺杀 指挥官") == ("刺杀", "指挥官"),
      str(R._split_leading_type("刺杀 指挥官")))
check("_split_leading_type：类型自带冒号时一起吃掉",
      R._split_leading_type("刺杀：H-09 坦克") == ("刺杀：", "H-09 坦克"),
      str(R._split_leading_type("刺杀：H-09 坦克")))
check("_split_leading_type：长词优先（资源回收）",
      R._split_leading_type("资源回收 取回被偷的器物")[0] == "资源回收",
      str(R._split_leading_type("资源回收 取回被偷的器物")))
check("_split_leading_type：无类型时原样返回",
      R._split_leading_type("核心样本｜40-60级") == ("", "核心样本｜40-60级"),
      str(R._split_leading_type("核心样本｜40-60级")))
check("_split_leading_type：节点名不带类型时不误切",
      R._split_leading_type("地狱净化：科腐者｜115-120级")[0] == "",
      str(R._split_leading_type("地狱净化：科腐者｜115-120级")))
check("_split_leading_type：3 级隔离库赏金 不误切",
      R._split_leading_type("3 级隔离库赏金｜50-60级")[0] == "",
      str(R._split_leading_type("3 级隔离库赏金｜50-60级")))

# ------------------------------------------------- 元素/加成两列对齐识别
for _row in ("· 信条·集议（Tenet Agendus）　毒素 35.3%",
             "· 终幕·低音爆囊（Coda Bassocyst）　磁力 42.9%",
             "· 终幕·啐沫者（Coda Tysis）　火焰 53.4%"):
    _cells = R._split_cells(R._strip_emoji(_row)[0])
    check(f"两格且第二格是「元素 百分比」：{_row[:12]}…",
          bool(_cells) and len(_cells) == 2 and bool(R._PAIR_RE.match(_cells[1])),
          str(_cells))
# 负例：注释行第二格不是「元素 百分比」，不该进列对齐
for _row in ("※ 元素加成每 4 天重生成　距下次 3天13小时（09-16 00:00 UTC）",
             "※ 快照 09-12 09:51（本轮快照）　以游戏内商店为准",
             "　· 歼灭 核心样本｜40-60级"):
    _cells = R._split_cells(R._strip_emoji(_row)[0]) or []
    check(f"非加成行不进列对齐：{_row[:14]}…",
          not (len(_cells) == 2 and R._PAIR_RE.match(_cells[1])), str(_cells))

print()
if FAILED:
    print(f"✗ {len(FAILED)} 项失败：" + "、".join(FAILED))
    sys.exit(1)
print("✓ 全部通过")
