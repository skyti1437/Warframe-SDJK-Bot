# -*- coding: utf-8 -*-
"""赏金卡与周期卡的行格式离线测试：python3 tests/test_bounty_card.py

覆盖三件在卡面上看得见、但很容易在重构里退化的事：

1. **赏金卡只列高价值奖励**：MOD(★) / 部件·蓝图(▣) / 债券 / 遗物 / 地区特色资源。
   现金匣、内融核心、阿耶精华这类每档都一样的填充物一旦漏回来，整张卡会被货币淹掉。
2. **扎里曼 / 解剖圣所 / 1999 必须有任务名与任务目标**：这三块 DE 侧 Jobs 恒为空，
   名字来自 browse.wf oracle 的 node + challenge（节点名查 nodes_zh.json、
   目标查 challenges_zh.json），oracle 挂了就退回按等级档列奖励。
3. **每行周期卡的倒计时格式一致**：中文倒计时与「（Xh Ym）」紧凑倒计时必须由
   **同一个时刻**算出，双衍王境那一行尤其容易漏掉括号部分。
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import de_worldstate as dw  # noqa: E402
from core import formatters as fmt  # noqa: E402
from core import render as R  # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = ""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f"  -> {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


ROOT = Path(__file__).resolve().parent.parent
raw = json.loads((ROOT / "tests" / "fixtures" / "de_worldstate.json")
                 .read_text(encoding="utf-8"))
bundle = dw.parse_worldstate(raw, now_ms=1788964350000)

# ---------------------------------------------------------------------------
# 1) 周期卡：每行的倒计时都要「中文（紧凑）」成对出现
# ---------------------------------------------------------------------------
# 跟 parse_worldstate 的 now_ms=1788964350000 (= 2026-09-09 14:32:30Z) 对齐，
# 这样 fixture 里 cetus/vallis/earth 的 expiry 仍落在未来若干分钟，倒计时
# 才不会是「已结束」。mock 时间要接近 parser 给的 now，但不能晚于任何 expiry，
# 否则测试只能验证错误分支。
_fixed = datetime(2026, 9, 9, 14, 32, tzinfo=timezone.utc)
_orig_now = fmt._now
fmt._now = lambda: _fixed


def _named(key: str) -> dict:
    d = dict(bundle[f"{key}Cycle"])
    d["_name"] = key
    return d


title, cyc_lines = fmt.fmt_cetus(*(_named(k) for k in
                                   ("cetus", "vallis", "cambion", "earth",
                                    "duviri", "zariman")))
check("周期卡标题", title == "平原时间", title)
# 周期名开头的行 = 6（夜灵/奥布/魔胎/地球/双衍/扎里曼），「当前材料」之类不数
_cycle_starts = ("夜灵平野", "奥布山谷", "魔胎之境", "地球：", "双衍王境", "扎里曼号")
check("六个周期各一行",
      sum(any(ln.startswith(s) for s in _cycle_starts) for ln in cyc_lines) == 6,
      str(cyc_lines))
for _ln in cyc_lines:
    if "剩余" not in _ln:
        continue
    check(f"倒计时成对：{_ln[:14]}…",
          "（" in _ln and "）" in _ln and "剩余" in _ln, _ln)
# 双衍王境曾经只写中文秒级、缺「（Xh Ym）」，与其它行格式不一致
_duv = [ln for ln in cyc_lines if ln.startswith("双衍王境")][0]
check("双衍王境有紧凑倒计时括号",
      "（" in _duv and "m" in _duv and "）" in _duv, _duv)

# 中文与紧凑两份必须同源同刻（旧实现一份来自 countdown()、一份抄数据的 timeLeft，
# 相差几十秒就会拼出「剩余 16分钟（17m 26s）」这种自相矛盾的行）
_pairs = fmt._left_pair("2030-01-01T00:00:00+00:00")
check("_left_pair 坏值返回占位", fmt._left_pair("") == ("?", ""))
check("_left_pair 已过期", fmt._left_pair("2000-01-01T00:00:00+00:00")[0] == "已结束")
fmt._now = _orig_now

# ---------------------------------------------------------------------------
# 2) 赏金卡：高价值奖励过滤
# ---------------------------------------------------------------------------
_FILLER = ("现金匣", "内融核心", "阿耶精华", "赤毒")
for _tok in ("★简化的预测", "▣Gara机体蓝图", "2 × 培训债务债券",
             "古纪 Q3 遗物（光辉）", "虚空绒翎", "尖锐音魂"):
    check(f"保留高价值：{_tok}", fmt._is_high_value(_tok) is True)
for _tok in ("1,500 现金匣", "50 内融核心", "阿耶精华", "300 × 赤毒",
             "▣神经元", "▣奥罗金电池"):
    check(f"剔除填充物：{_tok}", fmt._is_high_value(_tok) is False)

_hv = fmt._fmt_high_value(
    ["★简化", "1,500 现金匣", "50 内融核心", "▣Gara机体蓝图", "阿耶精华"])
check("高价值串只剩 ★/▣", _hv == "★简化、▣Gara机体蓝图", _hv)

# 地区级轮换不能把「抢劫 / 深矿 / 尸鬼净化」也算进来（那是另一套活动池）
check("轮换档位键排除抢劫/深矿",
      all(k not in fmt._region_tier_keys(fmt._BOUNTY_POOLS["Solaris United"])
          for k in ("抢劫", "深矿·解放小动物")))
check("轮换档位键含阶段与合一众",
      {"阶段1", "合一众"} <= set(fmt._region_tier_keys(fmt._BOUNTY_POOLS["Ostrons"])))

# ---------------------------------------------------------------------------
# 3) 赏金卡：一览（无参，简明）与详情（带地区词，完整）
# ---------------------------------------------------------------------------
ORACLE = {
    "bounties": {
        "ZarimanSyndicate": [
            {"node": "SolNode232",
             "challenge": "/Lotus/Types/Challenges/Zariman/"
                          "ZarimanSurvivalAbove50EasyChallenge"},
            {"node": "SolNode233",
             "challenge": "/Lotus/Types/Challenges/Zariman/"
                          "ZarimanFindMelicaCacheChallenge"},
        ],
        "EntratiLabSyndicate": [
            {"node": "SolNode721",
             "challenge": "/Lotus/Types/Challenges/EntratiLab/"
                          "EntratiLabKillFlyingMurmurChallenge"},
        ],
        "HexSyndicate": [
            {"node": "SolNode851",
             "challenge": "/Lotus/Types/Challenges/Vania/"
                          "VaniaDestroyBackpacksVeryHard"},
        ],
    },
}

# —— 无参「赏金」= 一览（参考版式）：地区分组 + 轮换行 + 高等级档 ——
_title, lines = fmt.fmt_bounties(bundle["syndicateMissions"], cycle=ORACLE)
check("赏金卡标题", _title == "赏金任务", _title)

# 六个地区都要在（DE 下发 jobs 的三个 + 只能靠 oracle 的三个），
# 且必须按**剧情推进顺序**排列——不能跟随 DE 的 SyndicateMissions 数组顺序。
_REGION_ORDER = ["希图斯（地球）", "奥布斯山谷（金星）", "英择谛（魔胎之境）",
                 "羽化之穹（扎里曼）", "解剖圣所（实验室）", "霍瓦尼亚（1999）"]
_banner = [ln for ln in lines if ln.startswith("◆ ")]
for _region in _REGION_ORDER:
    check(f"地区在卡上：{_region}", any(_region in ln for ln in _banner), _region)
check("地区按剧情顺序排列",
      [next((r for r in _REGION_ORDER if r in ln), "") for ln in _banner]
      == _REGION_ORDER,
      str([ln[:14] for ln in _banner]))
# 剧情顺序常量与卡面一致（formatters 的单一来源）
check("_BOUNTY_REGION_ORDER 与卡面一致",
      len(fmt._BOUNTY_REGION_ORDER) == 6, str(fmt._BOUNTY_REGION_ORDER))

# 一览有「轮换」行（MOD / 债券 / 部件），且标出当前轮次
check("一览有轮换行", any("轮换" in ln for ln in lines), str(lines[:6]))
check("一览轮换标轮次", any("轮换（" in ln for ln in lines), str(lines[:6]))
# 轮换行**不得有「…等 N 项」省略号**（用户明确反馈「甚至还整了什么省略号」）
check("一览轮换无省略号",
      not any("等 " in ln and "项" in ln for ln in lines),
      str([ln for ln in lines if "等 " in ln][:3]))
# 每地区只出一行轮换（参考版式就是一行）
_rot_rows = [ln for ln in lines if "轮换" in ln and not ln.startswith("※")]
_region_cnt = sum(1 for ln in lines if ln.startswith("◆ "))
check("一览轮换行数 == 地区数", len(_rot_rows) == _region_cnt,
      f"轮换 {len(_rot_rows)} / 地区 {_region_cnt}")
# 每行轮换最多 6 项（避免再堆成长列表）
for _ln in _rot_rows:
    _items = _ln.split("：")[-1].split("、") if "：" in _ln else []
    check(f"轮换项 ≤6：{_ln[:12]}…", len(_items) <= 6, str(len(_items)))
# 一览有档位行（· ...｜N-M级），但不展开奖励行
check("一览有档位行", any(ln.startswith("　· ") and "级" in ln for ln in lines),
      str([ln for ln in lines if ln.startswith("　· ")][:4]))
# 层级：地区行用 ◆（一级），任务档用 ·（二级）——用户反馈「看着都是一级菜单」
check("任务档不再用 ◆ 当一级标题",
      not any(ln.startswith("　◆") for ln in lines),
      str([ln for ln in lines if ln.startswith("　◆")][:3]))
check("地区行用 ◆ 一级", bool(_banner), str(len(_banner)))
check("一览不展开每档奖励",
      not any(ln.startswith("　　") and "、" in ln for ln in lines), "一览不该有奖励行")
# 行数要克制（6 地区 × (标题+轮换1+档位2) + 图例/提示）
# DE 三地区现在也带任务描述行，一览自然变长（39 行是当前实测值）
check("一览行数紧凑", len(lines) <= 42, str(len(lines)))
# oracle 在线时，扎里曼 / 实验室 / 1999 也要有节点名 + 任务目标
check("一览·扎里曼节点名", any("奥金工场" in ln for ln in lines), "奥金工场")
check("一览·扎里曼任务目标", any("任务：" in ln for ln in lines), "任务：")

# —— 「赏金 地球」= 详情：每档任务名 + 等级 + **完整奖励**（v1.1 既定形态）——
_te, earth = fmt.fmt_bounties(bundle["syndicateMissions"], "地球", cycle=ORACLE)
check("详情·地球只一块", not any("奥布斯山谷" in ln for ln in earth))
# 每档后面必须跟一行奖励（◆ 档位行数量 == 奖励行数量）
_bars = [ln for ln in earth if ln.startswith("　· ") and "级" in ln]
_rewards = [ln for ln in earth if ln.startswith("　　") and "、" in ln]
check("详情·每档都有奖励行", len(_bars) == len(_rewards) and len(_bars) >= 6,
      f"档位 {len(_bars)} / 奖励 {len(_rewards)}")
# 奖励**不过滤**：现金匣 / 内融核心必须还在（v1.2 曾误删，用户要求回退）
check("详情·奖励含现金匣", any("现金匣" in ln for ln in earth), "现金匣")
check("详情·奖励含内融核心", any("内融核心" in ln for ln in earth), "内融核心")
check("详情·含钢铁之路档", any("钢铁之路" in ln for ln in earth), "钢铁之路")
check("详情·含合一众档", any("合一众" in ln for ln in earth), "合一众")

# —— 「赏金 扎里曼」= 详情：节点名 + 任务目标 + 该档奖励 ——
_tz, zlines = fmt.fmt_bounties(bundle["syndicateMissions"], "扎里曼", cycle=ORACLE)
check("详情·扎里曼节点名", any("奥金工场" in ln for ln in zlines), "奥金工场")
check("详情·扎里曼任务目标",
      any("任务：" in ln and "耀金奖章" in ln for ln in zlines), "耀金奖章")
check("详情·扎里曼有奖励行",
      any(ln.startswith("　　") and "、" in ln for ln in zlines), str(zlines[:4]))
_tn, nlines = fmt.fmt_bounties(bundle["syndicateMissions"], "实验室", cycle=ORACLE)
check("详情·解剖圣所节点名", any("卫城区" in ln for ln in nlines), "卫城区")

# 隔离库三档用 DE 官方叫法（jobType 为空，只能按池标签回填）
_td, dlines = fmt.fmt_bounties(bundle["syndicateMissions"], "火卫二", cycle=ORACLE)
check("隔离库官方叫法", any("级隔离库赏金" in ln for ln in dlines), "级隔离库赏金")

# 每一行详情赏金都要能被渲染层抽成右对齐等级列
_lv_rows = [ln for ln in dlines if ln.startswith("　· ") and "级" in ln]
check("赏金行都能抽等级", _lv_rows and all(R._LV_RE.search(ln) for ln in _lv_rows),
      str([ln for ln in _lv_rows if not R._LV_RE.search(ln)][:3]))

# oracle 不可达时：详情与一览都降级（按等级档），不空白
_t2, degraded = fmt.fmt_bounties(bundle["syndicateMissions"], "扎里曼", cycle={})
check("oracle 不可达仍有扎里曼块", any("羽化之穹（扎里曼）" in ln for ln in degraded))
check("oracle 不可达降级列等级档",
      any("羽化之穹（扎里曼）" in ln for ln in degraded)
      and any("级" in ln for ln in degraded))
_t2s, summary2 = fmt.fmt_bounties(bundle["syndicateMissions"], cycle={})
check("oracle 不可达一览照常", any("羽化之穹（扎里曼）" in ln for ln in summary2))

# 关键词筛选（用户明确要保留「赏金 地球」这类用法）
_t3, only_earth = fmt.fmt_bounties(bundle["syndicateMissions"], "地球", cycle=ORACLE)
check("筛选 地球 只剩一块",
      any("希图斯（地球）" in ln for ln in only_earth)
      and not any("奥布斯山谷" in ln for ln in only_earth))
_t4, bad = fmt.fmt_bounties(bundle["syndicateMissions"], "不存在的地区", cycle=ORACLE)
check("未识别地区明确报错", any("未识别地区" in ln for ln in bad))

# ===========================================================================
# 任务类型（2026-09-12 用户反馈「赏金任务类型没有」）
#
# 数据来源：DE 官方导出。
#   · DE 三地区（地球/金星/火卫二）：job 只有 jobType 资产路径、**没有节点**，
#     任务类型取 ExportBounties 的**末阶段遭遇战**（``_bounty_type``）。
#   · oracle 三地区（扎里曼/实验室/1999）：取节点的官方 ``missionName``
#     （``nodes_zh[key]['type']``），因为 DE 侧 jobs 恒为空。
# ===========================================================================
_DE_TYPES = ("歼灭", "刺杀", "捕获", "挖掘", "防御", "破坏", "救援",
             "间谍", "生存", "劫持", "资源回收", "物资回收", "净化", "伏击")


def _block_of(ls, region):
    """截取某一地区横幅到下一地区横幅之间的行。"""
    start = next(i for i, x in enumerate(ls) if region in x)
    out = []
    for x in ls[start + 1:]:
        if x.startswith("◆ "):
            break
        out.append(x)
    return out


# —— 一览：DE 地区档位行必须带任务类型前缀 ——
_tier_rows = [ln for ln in lines if ln.startswith("　· ") and "级" in ln]
check("一览有档位行", len(_tier_rows) >= 6, str(len(_tier_rows)))
check("一览 DE 档位行带任务类型",
      any(any(ln.startswith(f"　· {t} ") for t in _DE_TYPES) for ln in _tier_rows),
      str(_tier_rows[:5]))
for _reg in ("希图斯（地球）", "奥布斯山谷（金星）", "英择谛（魔胎之境）"):
    _blk = [x for x in _block_of(lines, _reg) if x.startswith("　· ")]
    check(f"{_reg} 至少一档带任务类型",
          any(any(f"　· {t} " in ln for t in _DE_TYPES) for ln in _blk),
          str(_blk))
# 赏金名里已含类型词的不得出现重复前缀（「物资回收 物资回收」）
check("档位行无重复类型前缀",
      not any(ln.count(t) >= 2 for ln in _tier_rows for t in _DE_TYPES),
      str([ln for ln in _tier_rows if any(ln.count(t) >= 2 for t in _DE_TYPES)][:3]))
# 顺序：类型在前、名称在中、等级在后（渲染层再把等级抽成右对齐列）
check("档位行类型在名称前（· 类型 名称｜等级）",
      all(ln.index("｜") > ln.index("·") for ln in _tier_rows), str(_tier_rows[:3]))

# —— 一览 & 详情：oracle 地区的「任务：」行 = 官方任务类型 + 目标 ——
# 不断言具体某节点（fixture 的 oracle 快照与线上不同），而是**数据驱动**校验：
# 每个「任务：」行的首段必须是 nodes_zh 里的官方任务类型之一。
_NODES = json.loads((ROOT / "core" / "data" / "de" / "nodes_zh.json")
                    .read_text(encoding="utf-8"))
_OFFICIAL_TYPES = {v.get("type") for v in _NODES.values() if v.get("type")}
check("nodes_zh 带官方任务类型", len(_OFFICIAL_TYPES) >= 20, str(len(_OFFICIAL_TYPES)))

for _kw in ("扎里曼", "实验室", "1999"):
    _t, _ls = fmt.fmt_bounties(bundle["syndicateMissions"], _kw, cycle=ORACLE)
    _task = [ln for ln in _ls if ln.startswith("　　任务：")]
    check(f"详情 {_kw} 有任务行", bool(_task), str(_ls[:6]))
    _heads = [ln[len("　　任务："):].split(" ")[0] for ln in _task]
    check(f"详情 {_kw} 任务行首段是官方任务类型",
          all(h in _OFFICIAL_TYPES for h in _heads), str(_heads))

# 一览里现在有两类任务行：DE 地区（只有描述，无类型段）与 oracle 地区（类型+挑战名+目标）
_ov_task = [ln for ln in lines if ln.startswith("　　任务：")]
_ov_oracle = [ln for ln in _ov_task
              if ln[len("　　任务："):].split(" ")[0] in _OFFICIAL_TYPES]
_ov_de = [ln for ln in _ov_task
          if ln[len("　　任务："):].split(" ")[0] not in _OFFICIAL_TYPES]
check("一览 oracle 任务行带类型",
      len(_ov_oracle) >= 3
      and all(ln[len("　　任务："):].split(" ")[0] in _OFFICIAL_TYPES
              for ln in _ov_oracle),
      str(_ov_oracle[:3]))
check("一览 DE 任务行带描述（用户确认 DE 有 detail，已接上）",
      len(_ov_de) >= 3, str(_ov_de[:3]))
check("任务行不再拼挑战名（旧形态「任务：给梅利卡加油打气 · …」已移除）",
      not any(" · " in ln for ln in _ov_task), str(_ov_task[:3]))
# 已知节点→类型映射（来自 DE ExportRegions，与 fixture 无关）
for _n, _want in (("SolNode231", "歼灭"), ("SolNode230", "虚空洪流"),
                  ("SolNode233", "虚空决战"), ("SolNode235", "移动防御"),
                  ("SolNode718", "元素转换"), ("SolNode717", "生存"),
                  ("SolNode856", "刺杀"), ("SolNode852", "生存"),
                  ("SolNode26", "防御")):
    check(f"节点官方类型：{_n}→{_want}",
          (_NODES.get(_n) or {}).get("type") == _want,
          str((_NODES.get(_n) or {}).get("type")))

# —— 任务行 = 「类型 + 挑战名 + 目标」，三段单行（副目标分级已回退）——
# 用户先要求「副目标放主目标下一级」，后发现「第二个科腐者」其实是另一档赏金，
# 于是回退：整句描述不再按句号拆行。
_sub = fmt._oracle_task_lines(
    "SolNode852",
    "/Lotus/Types/Challenges/Vania/VaniaAbilityKillVeryHard")
check("任务行单行（不再拆副目标）", len(_sub) == 1, str(_sub))
check("任务行 = 类型 + 挑战名 + 目标",
      bool(_sub) and _sub[0].startswith("　　任务：生存 能量超载 使用战甲技能击杀 40 名敌人"),
      str(_sub))
check("第二句保留在同一行",
      bool(_sub) and "增加能量恢复速率" in _sub[0], str(_sub))
check("任务行不含缩进二级（无四个全角空格）",
      all(not ln.startswith("　　　　") for ln in _sub), str(_sub))

_single = fmt._oracle_task_lines(
    "SolNode231",
    "/Lotus/Types/Challenges/Zariman/ZarimanUseVoidRiftsEasyChallenge")
check("单句描述也只有一行", len(_single) == 1, str(_single))
check("任务行带类型",
      bool(_single) and _single[0].startswith("　　任务：歼灭 "), str(_single))
check("挑战名已恢复（用户反馈「这些任务名字怎么没了」）",
      bool(_single) and "窃取新力量" in _single[0], str(_single))
check("拿不到类型时至少还有目标（不空白）",
      bool(fmt._oracle_task_lines("", "/Lotus/Types/Challenges/Zariman/"
                                      "ZarimanUseVoidRiftsEasyChallenge")),
      str(fmt._oracle_task_lines("", "/Lotus/Types/Challenges/Zariman/"
                                     "ZarimanUseVoidRiftsEasyChallenge")))

# —— 渲染层靠空格切「类型 / 挑战名 / 目标」三段，前两段必须是**单个词** ——
# 挑战名里确实可能带空格（「突袭 Grineer」/「任务完成 X」），formatter 换 NBSP。
check("_one_word 把 ASCII 空格换成 NBSP",
      fmt._one_word("任务完成 X") == "任务完成\u00a0X",
      repr(fmt._one_word("任务完成 X")))
_ka = fmt._oracle_task_lines(
    "SolNode235",
    "/Lotus/Types/Challenges/Zariman/ZarimanKillGrineerChallenge")
check("带空格的挑战名已换成 NBSP",
      bool(_ka) and "\u00a0" in _ka[0], str(_ka))
_ka_seg = _ka[0][len("　　任务："):].split(" ", 2) if _ka else []
check("任务行切成三段（类型 / 挑战名 / 目标）", len(_ka_seg) == 3, str(_ka_seg))
check("挑战名段内无 ASCII 空格（不会被误切）",
      len(_ka_seg) >= 2 and " " not in _ka_seg[1], str(_ka_seg[:2]))
check("类型段内无空格", bool(_ka_seg) and " " not in _ka_seg[0], str(_ka_seg[:1]))

# —— 档位数量：一览取「等级最高的 3 档」（用户选择「末尾 3 档」）——
check("_OVERVIEW_N == 3", fmt._OVERVIEW_N == 3, str(fmt._OVERVIEW_N))
_earth_jobs = next(s for s in bundle["syndicateMissions"]
                   if s["syndicate"] == "Ostrons")["jobs"]
_picked = fmt._top_tiers(_earth_jobs, 3)
check("_top_tiers 取到 3 档", len(_picked) == 3, str(len(_picked)))
_picked_desc = sorted((fmt._tier_of(j) for j in _picked), reverse=True)
check("_top_tiers 是等级最高的 3 档",
      _picked_desc == sorted((fmt._tier_of(j) for j in _earth_jobs),
                             reverse=True)[:3], str(_picked_desc))
check("_top_tiers 结果按等级升序",
      [fmt._tier_of(j) for j in _picked] == sorted(fmt._tier_of(j) for j in _picked))
# 隔离库（火卫二末尾追加、等级低）不该挤掉高等级档
_deimos = next(s for s in bundle["syndicateMissions"]
               if s["syndicate"] == "Entrati")["jobs"]
_deimos_pick = fmt._top_tiers(_deimos, 3)
check("火卫二高等级档没被隔离库挤掉",
      any(fmt._tier_of(j) == (100, 100) for j in _deimos_pick),
      str([fmt._tier_of(j) for j in _deimos_pick]))

# —— 官方赏金名（ExportBounties 中文名，修掉旧表里的英文条目）——
_meta = json.loads((ROOT / "core" / "data" / "de" / "bounty_jobs_zh.json")
                   .read_text(encoding="utf-8"))
_names = [v.get("name") for v in _meta.values() if v.get("name")]
check("官方赏金名表非空", len(_names) >= 50, str(len(_names)))
check("金星「尘土部队」用官方中文名（旧表是 Dirt Unit）",
      "尘土部队" in _names, str([n for n in _names if "尘" in n or "Dirt" in n]))
check("金星「貌似合法」在官方名表里", "貌似合法" in _names)
check("火卫二「核心样本」在官方名表里", "核心样本" in _names)
check("官方名表里不再有 Dog Boards / Served Cold / Dirt Unit",
      not any(n in ("Dog Boards", "Served Cold", "Dirt Unit") for n in _names),
      str([n for n in _names if n in ("Dog Boards", "Served Cold", "Dirt Unit")]))
check("合一众后缀已剥离（卡面另有｜合一众 标签）",
      not any("（合一众）" in n for n in _names),
      str([n for n in _names if "（合一众）" in n][:3]))
# 每条赏金都要有末阶段类型，否则任务类型会缺
_missing = [k for k, v in _meta.items() if not v.get("final")]
check("赏金表每条都有末阶段（任务类型来源）", not _missing, str(_missing[:3]))

if FAILED:
    print(f"\n失败 {len(FAILED)} 项：{FAILED}")
    sys.exit(1)
print("\n全部通过 ✔")
