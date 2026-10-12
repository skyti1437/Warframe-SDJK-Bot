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
raw = json.loads((ROOT / "tests" / "fixtures" / "de_worldstate.json").read_text(encoding="utf-8"))
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


title, cyc_lines = fmt.fmt_cetus(
    *(_named(k) for k in ("cetus", "vallis", "cambion", "earth", "duviri", "zariman"))
)
check("周期卡标题", title == "平原时间", title)
# 周期名开头的行 = 6（夜灵/奥布/魔胎/地球/双衍/扎里曼），「当前材料」之类不数
# ★ 2026-10-11 第二步改版：周期行改为「· 名称　状态　剩余 …」列表项（卡首另有「◆ 最先换轮」答案行，
#   它不是周期行，不数、也不要求「中文（紧凑）」成对）。
_cycle_starts = ("· 夜灵平野", "· 奥布山谷", "· 魔胎之境", "· 地球　", "· 双衍王境", "· 扎里曼号")
check(
    "六个周期各一行",
    sum(any(ln.startswith(s) for s in _cycle_starts) for ln in cyc_lines) == 6,
    str(cyc_lines),
)
for _ln in cyc_lines:
    if "剩余" not in _ln or not _ln.startswith("· "):
        continue
    check(f"倒计时成对：{_ln[:14]}…", "（" in _ln and "）" in _ln and "剩余" in _ln, _ln)
# 双衍王境曾经只写中文秒级、缺「（Xh Ym）」，与其它行格式不一致
_duv = [ln for ln in cyc_lines if ln.startswith("· 双衍王境")][0]
check("双衍王境有紧凑倒计时括号", "（" in _duv and "m" in _duv and "）" in _duv, _duv)

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
for _tok in (
    "★简化的预测",
    "▣Gara机体蓝图",
    "2 × 培训债务债券",
    "古纪 Q3 遗物（光辉）",
    "虚空绒翎",
    "尖锐音魂",
):
    check(f"保留高价值：{_tok}", fmt._is_high_value(_tok) is True)
for _tok in ("1,500 现金匣", "50 内融核心", "阿耶精华", "300 × 赤毒", "▣神经元", "▣奥罗金电池"):
    check(f"剔除填充物：{_tok}", fmt._is_high_value(_tok) is False)

_hv = fmt._fmt_high_value(["★简化", "1,500 现金匣", "50 内融核心", "▣Gara机体蓝图", "阿耶精华"])
check("高价值串只剩 ★/▣", _hv == "★简化、▣Gara机体蓝图", _hv)

# 地区级轮换不能把「抢劫 / 深矿 / 尸鬼净化」也算进来（那是另一套活动池）
check(
    "轮换档位键排除抢劫/深矿",
    all(
        k not in fmt._region_tier_keys(fmt._BOUNTY_POOLS["Solaris United"])
        for k in ("抢劫", "深矿·解放小动物")
    ),
)
check(
    "轮换档位键含阶段与合一众",
    {"阶段1", "合一众"} <= set(fmt._region_tier_keys(fmt._BOUNTY_POOLS["Ostrons"])),
)

# ---------------------------------------------------------------------------
# 3) 赏金卡：一览（无参，简明）与详情（带地区词，完整）
# ---------------------------------------------------------------------------
ORACLE = {
    "bounties": {
        "ZarimanSyndicate": [
            {
                "node": "SolNode232",
                "challenge": "/Lotus/Types/Challenges/Zariman/ZarimanSurvivalAbove50EasyChallenge",
            },
            {
                "node": "SolNode233",
                "challenge": "/Lotus/Types/Challenges/Zariman/ZarimanFindMelicaCacheChallenge",
            },
        ],
        "EntratiLabSyndicate": [
            {
                "node": "SolNode721",
                "challenge": "/Lotus/Types/Challenges/EntratiLab/"
                "EntratiLabKillFlyingMurmurChallenge",
            },
        ],
        "HexSyndicate": [
            {
                "node": "SolNode851",
                "challenge": "/Lotus/Types/Challenges/Vania/VaniaDestroyBackpacksVeryHard",
            },
        ],
    },
}

# —— 无参「赏金」= 一览（参考版式）：地区分组 + 轮换行 + 高等级档 ——
_title, lines = fmt.fmt_bounties(bundle["syndicateMissions"], cycle=ORACLE)
check("赏金卡标题", _title == "赏金任务", _title)

# 六个地区都要在（DE 下发 jobs 的三个 + 只能靠 oracle 的三个），
# 且必须按**剧情推进顺序**排列——不能跟随 DE 的 SyndicateMissions 数组顺序。
_REGION_ORDER = [
    "希图斯（地球）",
    "奥布山谷（金星）",
    "英择谛（魔胎之境）",
    "羽化之穹（扎里曼）",
    "解剖圣所（实验室）",
    "霍瓦尼亚（1999）",
]
_banner = [ln for ln in lines if ln.startswith("◆ ")]
for _region in _REGION_ORDER:
    check(f"地区在卡上：{_region}", any(_region in ln for ln in _banner), _region)
check(
    "地区按剧情顺序排列",
    [next((r for r in _REGION_ORDER if r in ln), "") for ln in _banner] == _REGION_ORDER,
    str([ln[:14] for ln in _banner]),
)
# 剧情顺序常量与卡面一致（formatters 的单一来源）
check(
    "_BOUNTY_REGION_ORDER 与卡面一致",
    len(fmt._BOUNTY_REGION_ORDER) == 6,
    str(fmt._BOUNTY_REGION_ORDER),
)

# 一览有「轮换」行（MOD / 债券 / 部件），且标出当前轮次
check("一览有轮换行", any("轮换" in ln for ln in lines), str(lines[:6]))
check("一览轮换标轮次", any("轮换（" in ln for ln in lines), str(lines[:6]))
# 轮换行**不得有「…等 N 项」省略号**（维护者明确反馈「甚至还整了什么省略号」）
check(
    "一览轮换无省略号",
    not any("等 " in ln and "项" in ln for ln in lines),
    str([ln for ln in lines if "等 " in ln][:3]),
)
# 每地区只出一行轮换（参考版式就是一行）
_rot_rows = [ln for ln in lines if "轮换" in ln and not ln.startswith("※")]
_region_cnt = sum(1 for ln in lines if ln.startswith("◆ "))
check(
    "一览轮换行数 == 地区数",
    len(_rot_rows) == _region_cnt,
    f"轮换 {len(_rot_rows)} / 地区 {_region_cnt}",
)
# 每行轮换最多 6 项（避免再堆成长列表）
for _ln in _rot_rows:
    _items = _ln.split("：")[-1].split("、") if "：" in _ln else []
    check(f"轮换项 ≤6：{_ln[:12]}…", len(_items) <= 6, str(len(_items)))
# 一览有档位行（· ...｜N-M级），但不展开奖励行
check(
    "一览有档位行",
    any(ln.startswith(fmt._BOUNTY_HEAD_PREFIX) and "级" in ln for ln in lines),
    str([ln for ln in lines if ln.startswith(fmt._BOUNTY_HEAD_PREFIX)][:4]),
)
# 层级：地区行用 ◆（一级），任务档用 ·（二级）——玩家反馈「看着都是一级菜单」
check(
    "任务档不再用 ◆ 当一级标题",
    not any(ln.startswith("　◆") for ln in lines),
    str([ln for ln in lines if ln.startswith("　◆")][:3]),
)
check("地区行用 ◆ 一级", bool(_banner), str(len(_banner)))
check(
    "一览不展开每档奖励",
    not any(ln.startswith("　　") and "、" in ln for ln in lines),
    "一览不该有奖励行",
)
# 行数要克制（6 地区 × (标题+轮换1+档位2) + 图例/提示）
# DE 三地区现在也带任务描述行，一览自然变长（39 行是当前实测值）
check("一览行数紧凑", len(lines) <= 42, str(len(lines)))
# oracle 在线时，扎里曼 / 实验室 / 1999 也要有节点名 + 任务目标
check("一览·扎里曼节点名", any("奥金工场" in ln for ln in lines), "奥金工场")
check("一览·扎里曼任务目标", any("任务：" in ln for ln in lines), "任务：")

# —— 「赏金 地球」= 详情：每档任务名 + 等级 + **完整奖励**（v1.1 既定形态）——
_te, earth = fmt.fmt_bounties(bundle["syndicateMissions"], "地球", cycle=ORACLE)
check("详情·地球只一块", not any("奥布山谷" in ln for ln in earth))
# 每档后面必须跟一行奖励（◆ 档位行数量 == 奖励行数量）
_bars = [ln for ln in earth if ln.startswith(fmt._BOUNTY_HEAD_PREFIX) and "级" in ln]
_rewards = [ln for ln in earth if ln.startswith("　　") and "、" in ln]
check(
    "详情·每档都有奖励行",
    len(_bars) == len(_rewards) and len(_bars) >= 6,
    f"档位 {len(_bars)} / 奖励 {len(_rewards)}",
)
# 奖励**不过滤**：现金匣 / 内融核心必须还在（v1.2 曾误删，维护者要求回退）
check("详情·奖励含现金匣", any("现金匣" in ln for ln in earth), "现金匣")
check("详情·奖励含内融核心", any("内融核心" in ln for ln in earth), "内融核心")
check("详情·含钢铁之路档", any("钢铁之路" in ln for ln in earth), "钢铁之路")
check("详情·含合一众档", any("合一众" in ln for ln in earth), "合一众")

# —— 「赏金 扎里曼」= 详情：档位行 = 类型+挑战名｜等级（地图名/任务：前缀已去）——
_tz, zlines = fmt.fmt_bounties(bundle["syndicateMissions"], "扎里曼", cycle=ORACLE)
check(
    "详情·扎里曼节点名已移除（涂沃主厅/奥金工场不在卡上）",
    not any("奥金工场" in ln or "涂沃主厅" in ln for ln in zlines),
    str(zlines[:5]),
)
# 描述已移除（「耀金奖章」是 Melica 描述里的词，随之下线）；挑战名保留在档位行
check(
    "详情·扎里曼档位行带挑战名", any("给梅利卡加油打气" in ln for ln in zlines), "给梅利卡加油打气"
)
check(
    "详情·扎里曼有奖励行",
    any(ln.startswith("　　") and "、" in ln for ln in zlines),
    str(zlines[:4]),
)
_tn, nlines = fmt.fmt_bounties(bundle["syndicateMissions"], "实验室", cycle=ORACLE)
# 圣所：地图（节点）名已按维护者要求移除（2026-10-02），横幅与等级档仍在
check(
    "详情·解剖圣所横幅在、节点名已移除",
    any("解剖圣所" in ln for ln in nlines) and not any("卫城区" in ln for ln in nlines),
    str(nlines[:5]),
)
check(
    "详情·圣所档位行等级挂行尾（与其他地区一样抽右对齐徽章）",
    any(
        ln.startswith(fmt._BOUNTY_HEAD_PREFIX) and ln.endswith("级") and "｜" in ln for ln in nlines
    ),
    "▸ 类型 挑战名｜55-60级",
)

# ★ 别名防误删：「赏金 圣所」必须命中 EntratiLab（解剖圣所）—— 沃沃图上那行就是「圣所」
#   （别名表 formatters `"圣所": "EntratiLab"`；DE 侧该区 Jobs 恒为空，靠 oracle 补节点/挑战）
_ts, slines = fmt.fmt_bounties(bundle["syndicateMissions"], "圣所", cycle=ORACLE)
check(
    "★ 别名「圣所」命中解剖圣所（EntratiLab）",
    "解剖圣所" in (_ts or "") or any("解剖圣所" in ln for ln in slines),
    f"title={_ts!r} 行数={len(slines)}",
)

# 隔离库三档用 DE 官方叫法（jobType 为空，只能按池标签回填）
_td, dlines = fmt.fmt_bounties(bundle["syndicateMissions"], "火卫二", cycle=ORACLE)
check("隔离库官方叫法", any("级隔离库赏金" in ln for ln in dlines), "级隔离库赏金")

# 每一行详情赏金都要能被渲染层抽成右对齐等级列
_lv_rows = [ln for ln in dlines if ln.startswith(fmt._BOUNTY_HEAD_PREFIX) and "级" in ln]
check(
    "赏金行都能抽等级",
    _lv_rows and all(R._LV_RE.search(ln) for ln in _lv_rows),
    str([ln for ln in _lv_rows if not R._LV_RE.search(ln)][:3]),
)

# 上下限相同的档（100-100）写成单值「100级」，且同样进右对齐等级列（_LV_RE 放宽后）
_one_lv = [ln for ln in dlines if ln.startswith(fmt._BOUNTY_HEAD_PREFIX) and "100级" in ln]
check(
    "★ 赏金单值等级：火卫二 100 级档写「100级」且能抽等级",
    bool(_one_lv) and all(R._LV_RE.search(ln) for ln in _one_lv),
    str(_one_lv[:3]),
)
check(
    "★ 赏金卡不再出现「100-100级」",
    not any("100-100级" in ln for ln in dlines)
    and not any(
        "100-100级" in ln
        for kw in ("地球", "金星", "扎里曼", "圣所", "1999")
        for ln in fmt.fmt_bounties(bundle["syndicateMissions"], kw, ORACLE)[1]
    ),
)

# oracle 不可达时：详情与一览都降级（按等级档），不空白
_t2, degraded = fmt.fmt_bounties(bundle["syndicateMissions"], "扎里曼", cycle={})
check("oracle 不可达仍有扎里曼块", any("羽化之穹（扎里曼）" in ln for ln in degraded))
check(
    "oracle 不可达降级列等级档",
    any("羽化之穹（扎里曼）" in ln for ln in degraded) and any("级" in ln for ln in degraded),
)
_t2s, summary2 = fmt.fmt_bounties(bundle["syndicateMissions"], cycle={})
check("oracle 不可达一览照常", any("羽化之穹（扎里曼）" in ln for ln in summary2))

# 关键词筛选（维护者明确要保留「赏金 地球」这类用法）
_t3, only_earth = fmt.fmt_bounties(bundle["syndicateMissions"], "地球", cycle=ORACLE)
check(
    "筛选 地球 只剩一块",
    any("希图斯（地球）" in ln for ln in only_earth)
    and not any("奥布山谷" in ln for ln in only_earth),
)
_t4, bad = fmt.fmt_bounties(bundle["syndicateMissions"], "不存在的地区", cycle=ORACLE)
check("未识别地区明确报错", any("未识别地区" in ln for ln in bad))

# ===========================================================================
# 任务类型（2026-09-12 玩家反馈「赏金任务类型没有」）
#
# 数据来源：DE 官方导出。
#   · DE 三地区（地球/金星/火卫二）：job 只有 jobType 资产路径、**没有节点**，
#     任务类型取 ExportBounties 的**末阶段遭遇战**（``_bounty_type``）。
#   · oracle 三地区（扎里曼/实验室/1999）：取节点的官方 ``missionName``
#     （``nodes_zh[key]['type']``），因为 DE 侧 jobs 恒为空。
# ===========================================================================
_DE_TYPES = (
    "歼灭",
    "刺杀",
    "捕获",
    "挖掘",
    "防御",
    "破坏",
    "救援",
    "间谍",
    "生存",
    "劫持",
    "资源回收",
    "物资回收",
    "净化",
    "伏击",
)


def _block_of(ls, region):
    """截取某一地区横幅到下一地区横幅之间的行。"""
    start = next(i for i, x in enumerate(ls) if region in x)
    out = []
    for x in ls[start + 1 :]:
        if x.startswith("◆ "):
            break
        out.append(x)
    return out


# —— 一览：DE 地区档位行必须带任务类型前缀 ——
_tier_rows = [ln for ln in lines if ln.startswith(fmt._BOUNTY_HEAD_PREFIX) and "级" in ln]
check("一览有档位行", len(_tier_rows) >= 6, str(len(_tier_rows)))
check(
    "一览 DE 档位行带任务类型",
    any(
        any(ln.startswith(f"{fmt._BOUNTY_HEAD_PREFIX}{t} ") for t in _DE_TYPES) for ln in _tier_rows
    ),
    str(_tier_rows[:5]),
)
for _reg in ("希图斯（地球）", "奥布山谷（金星）", "英择谛（魔胎之境）"):
    _blk = [x for x in _block_of(lines, _reg) if x.startswith(fmt._BOUNTY_HEAD_PREFIX)]
    check(
        f"{_reg} 至少一档带任务类型",
        any(any(f"{fmt._BOUNTY_HEAD_PREFIX}{t} " in ln for t in _DE_TYPES) for ln in _blk),
        str(_blk),
    )
# 赏金名里已含类型词的不得出现重复前缀（「物资回收 物资回收」）
check(
    "档位行无重复类型前缀",
    not any(ln.count(t) >= 2 for ln in _tier_rows for t in _DE_TYPES),
    str([ln for ln in _tier_rows if any(ln.count(t) >= 2 for t in _DE_TYPES)][:3]),
)
# 顺序：类型在前、名称在中、等级在后（渲染层再把等级抽成右对齐列）
# ★ 2026-09-27：行首标记由「·」改为 `▸`（`_BOUNTY_HEAD_PREFIX`）—— 判据跟着改
check(
    "档位行类型在名称前（▸ 类型 名称｜等级）",
    all(ln.index("｜") > ln.index(fmt._BOUNTY_HEAD_PREFIX.strip()) for ln in _tier_rows),
    str(_tier_rows[:3]),
)

# —— 一览 & 详情：oracle 地区的「任务：」行 = 官方任务类型 + 目标 ——
# 不断言具体某节点（fixture 的 oracle 快照与线上不同），而是**数据驱动**校验：
# 每个「任务：」行的首段必须是 nodes_zh 里的官方任务类型之一。
_NODES = json.loads((ROOT / "core" / "data" / "de" / "nodes_zh.json").read_text(encoding="utf-8"))
_OFFICIAL_TYPES = {v.get("type") for v in _NODES.values() if v.get("type")}
check("nodes_zh 带官方任务类型", len(_OFFICIAL_TYPES) >= 20, str(len(_OFFICIAL_TYPES)))

# —— oracle 任务行按地区三种形态（2026-10-02 用户分别指定）——
# 扎里曼维持「任务：类型 挑战名」；圣所去「任务：」前缀；1999 只留挑战名（⟦c⟧ 染蓝）
# —— oracle 三地区详情卡：独立任务行取消，类型/挑战名并入档位行（2026-10-02）——
# 圣所/扎里曼 = 「类型 挑战名｜等级」（无地图名）；1999 = 节点名 + ⟦c⟧挑战名｜等级
for _kw in ("扎里曼", "实验室", "1999"):
    _t, _ls = fmt.fmt_bounties(bundle["syndicateMissions"], _kw, cycle=ORACLE)
    _heads = [ln for ln in _ls if ln.startswith(fmt._BOUNTY_HEAD_PREFIX) and "级" in ln]
    check(
        f"详情 {_kw} 无独立任务行（无「任务：」前缀行）",
        _heads and not any(ln.startswith("　　任务：") for ln in _ls),
        str(_ls[:5]),
    )
    if _kw == "1999":
        # 节点名整体在蓝段内（派系词「炽蛇军」不得单独变橙），挑战名紫、目标白
        check(
            f"详情 {_kw} 节点名整段蓝（⟦c⟧ 跨整个节点名）",
            all("⟦c⟧地狱净化：炽蛇军⟦/c⟧" in ln for ln in _heads),
            str(_heads[:2]),
        )
        check(
            f"详情 {_kw} 挑战名紫段 + 目标白段",
            all("⟦v⟧" in ln and "⟦/v⟧" in ln and "⟦w⟧" in ln for ln in _heads),
            str(_heads[:2]),
        )
    else:
        check(
            f"详情 {_kw} 档位行 = 类型+挑战名(紫)+目标(白)｜等级",
            all(
                ln[len(fmt._BOUNTY_HEAD_PREFIX) :].split(" ")[0] in _OFFICIAL_TYPES
                and "⟦v⟧" in ln
                and "⟦w⟧" in ln
                and "｜" in ln
                for ln in _heads
            ),
            str(_heads[:3]),
        )
check(
    "详情·圣所档位行无地图名（节点名已去掉）",
    not any(
        "卫城区" in ln
        for ln in fmt.fmt_bounties(bundle["syndicateMissions"], "实验室", cycle=ORACLE)[1]
    ),
    "卫城区",
)

# 一览里现在有两类任务行：DE 地区（只有描述，无类型段）与 oracle 地区（类型+挑战名+目标）
_ov_task = [ln for ln in lines if ln.startswith("　　任务：")]
_ov_oracle = [ln for ln in _ov_task if ln[len("　　任务：") :].split(" ")[0] in _OFFICIAL_TYPES]
_ov_de = [ln for ln in _ov_task if ln[len("　　任务：") :].split(" ")[0] not in _OFFICIAL_TYPES]
check(
    "一览 oracle 任务行带类型",
    len(_ov_oracle) >= 3
    and all(ln[len("　　任务：") :].split(" ")[0] in _OFFICIAL_TYPES for ln in _ov_oracle),
    str(_ov_oracle[:3]),
)
check(
    "一览 DE 任务行已移除（2026-10-02 维护者要求：描述没作用）",
    not any(
        ln.startswith("　　任务：") and ln[len("　　任务：") :].split(" ")[0] not in _OFFICIAL_TYPES
        for ln in _ov_task
    ),
    str(_ov_de[:3]),
)
check(
    "任务行不再拼挑战名（旧形态「任务：给梅利卡加油打气 · …」已移除）",
    not any(" · " in ln for ln in _ov_task),
    str(_ov_task[:3]),
)
# 已知节点→类型映射（来自 DE ExportRegions，与 fixture 无关）
for _n, _want in (
    ("SolNode231", "歼灭"),
    ("SolNode230", "虚空洪流"),
    ("SolNode233", "虚空决战"),
    ("SolNode235", "移动防御"),
    ("SolNode718", "元素转换"),
    ("SolNode717", "生存"),
    ("SolNode856", "刺杀"),
    ("SolNode852", "生存"),
    ("SolNode26", "防御"),
):
    check(
        f"节点官方类型：{_n}→{_want}",
        (_NODES.get(_n) or {}).get("type") == _want,
        str((_NODES.get(_n) or {}).get("type")),
    )

# —— 任务行 = 「类型 + 挑战名」两段单行（描述段 2026-10-02 应维护者要求移除）——
_sub = fmt._oracle_task_lines(
    "SolNode852", "/Lotus/Types/Challenges/Vania/VaniaAbilityKillVeryHard"
)
check("任务行单行", len(_sub) == 1, str(_sub))
check(
    "任务行 = 类型 + 挑战名（描述已移除）",
    bool(_sub) and _sub[0] == "　　任务：生存 能量超载",
    str(_sub),
)
check("描述段不再出现在行内", bool(_sub) and "使用战甲技能击杀" not in _sub[0], str(_sub))
check(
    "任务行不含缩进二级（无四个全角空格）",
    all(not ln.startswith("　　　　") for ln in _sub),
    str(_sub),
)

_single = fmt._oracle_task_lines(
    "SolNode231", "/Lotus/Types/Challenges/Zariman/ZarimanUseVoidRiftsEasyChallenge"
)
check("单句描述也只有一行", len(_single) == 1, str(_single))
check("任务行带类型", bool(_single) and _single[0].startswith("　　任务：歼灭 "), str(_single))
check(
    "挑战名已恢复（玩家反馈「这些任务名字怎么没了」）",
    bool(_single) and "窃取新力量" in _single[0],
    str(_single),
)
check(
    "拿不到类型时至少还有目标（不空白）",
    bool(
        fmt._oracle_task_lines(
            "", "/Lotus/Types/Challenges/Zariman/ZarimanUseVoidRiftsEasyChallenge"
        )
    ),
    str(
        fmt._oracle_task_lines(
            "", "/Lotus/Types/Challenges/Zariman/ZarimanUseVoidRiftsEasyChallenge"
        )
    ),
)

# —— 渲染层靠空格切「类型 / 挑战名 / 目标」三段，前两段必须是**单个词** ——
# 挑战名里确实可能带空格（「突袭 Grineer」/「任务完成 X」），formatter 换 NBSP。
check(
    "_one_word 把 ASCII 空格换成 NBSP",
    fmt._one_word("任务完成 X") == "任务完成\u00a0X",
    repr(fmt._one_word("任务完成 X")),
)
_ka = fmt._oracle_task_lines(
    "SolNode235", "/Lotus/Types/Challenges/Zariman/ZarimanKillGrineerChallenge"
)
check("带空格的挑战名已换成 NBSP", bool(_ka) and "\u00a0" in _ka[0], str(_ka))
_ka_seg = _ka[0][len("　　任务：") :].split(" ", 2) if _ka else []
check("任务行切成两段（类型 / 挑战名，描述已移除）", len(_ka_seg) == 2, str(_ka_seg))
check(
    "挑战名段内无 ASCII 空格（不会被误切）",
    len(_ka_seg) >= 2 and " " not in _ka_seg[1],
    str(_ka_seg[:2]),
)
check("类型段内无空格", bool(_ka_seg) and " " not in _ka_seg[0], str(_ka_seg[:1]))

# —— 档位数量：一览取「等级最高的 3 档」（维护者选择「末尾 3 档」）——
check("_OVERVIEW_N == 3", fmt._OVERVIEW_N == 3, str(fmt._OVERVIEW_N))
_earth_jobs = next(s for s in bundle["syndicateMissions"] if s["syndicate"] == "Ostrons")["jobs"]
_picked = fmt._top_tiers(_earth_jobs, 3)
check("_top_tiers 取到 3 档", len(_picked) == 3, str(len(_picked)))
_picked_desc = sorted((fmt._tier_of(j) for j in _picked), reverse=True)
check(
    "_top_tiers 是等级最高的 3 档",
    _picked_desc == sorted((fmt._tier_of(j) for j in _earth_jobs), reverse=True)[:3],
    str(_picked_desc),
)
check(
    "_top_tiers 结果按等级升序",
    [fmt._tier_of(j) for j in _picked] == sorted(fmt._tier_of(j) for j in _picked),
)
# 隔离库（火卫二末尾追加、等级低）不该挤掉高等级档
_deimos = next(s for s in bundle["syndicateMissions"] if s["syndicate"] == "Entrati")["jobs"]
_deimos_pick = fmt._top_tiers(_deimos, 3)
check(
    "火卫二高等级档没被隔离库挤掉",
    any(fmt._tier_of(j) == (100, 100) for j in _deimos_pick),
    str([fmt._tier_of(j) for j in _deimos_pick]),
)

# —— 官方赏金名（ExportBounties 中文名，修掉旧表里的英文条目）——
_meta = json.loads(
    (ROOT / "core" / "data" / "de" / "bounty_jobs_zh.json").read_text(encoding="utf-8")
)
_names = [v.get("name") for v in _meta.values() if v.get("name")]
check("官方赏金名表非空", len(_names) >= 50, str(len(_names)))
check(
    "金星「尘土部队」用官方中文名（旧表是 Dirt Unit）",
    "尘土部队" in _names,
    str([n for n in _names if "尘" in n or "Dirt" in n]),
)
check("金星「貌似合法」在官方名表里", "貌似合法" in _names)
check("火卫二「核心样本」在官方名表里", "核心样本" in _names)
check(
    "官方名表里不再有 Dog Boards / Served Cold / Dirt Unit",
    not any(n in ("Dog Boards", "Served Cold", "Dirt Unit") for n in _names),
    str([n for n in _names if n in ("Dog Boards", "Served Cold", "Dirt Unit")]),
)
check(
    "合一众后缀已剥离（卡面另有｜合一众 标签）",
    not any("（合一众）" in n for n in _names),
    str([n for n in _names if "（合一众）" in n][:3]),
)
# 每条赏金都要有末阶段类型，否则任务类型会缺
_missing = [k for k, v in _meta.items() if not v.get("final")]
check("赏金表每条都有末阶段（任务类型来源）", not _missing, str(_missing[:3]))

# ---------------------------------------------------------------------------
# ⑨ 金星「深矿：企业重组」130-140 钢铁之路（社区观测登记，2026-09-26）
#    DE 只下发 7 档；这一档**不在任何接口里**（worldState / oracle VenusJobManifest /
#    DE 公开导出 ExportBounties·ExportSyndicates 三方实测都没有）→ 硬编登记，
#    来源显式标 community；中文名取自官方简中表（NokkoColony/LocationName=深矿、
#    Job2Name=企业重组）。
#    ★ 奖励池 2026-09-26 更正：导出里深矿有**两套表** —— 普通版 NokkoColonyRewards*
#      （现金匣 ×1 / 5,000×3、内融核心 1000 / 2000）与**钢铁版 NokkoColonyRewardsSteel***
#      （现金匣 **×2 / ×3**、内融核心 **3500 / 4000**）。用户用沃沃截图对拍时给出的
#      四个数字与**钢铁版逐项一致**，故改用钢铁版（构建脚本
#      `scripts/build_nokko_sp_pool.py`，可 --check 复算）。
# ---------------------------------------------------------------------------
_sup = dw.SOLARIS_SUPPLEMENT_JOBS
check(
    "★ 金星补齐档常量：130-140 / 官方简中名 / 来源=community",
    len(_sup) == 1
    and list(_sup[0]["enemyLevels"]) == [130, 140]
    and _sup[0]["_jobName"] == "深矿（钢铁之路）"
    and _sup[0].get("source") == "community",
    str(dict(_sup[0]))[:130],
)
_pool, _tag, _rot = fmt._resolve_bounty_pool("Solaris United", dict(_sup[0]))
check(
    "★ 该档奖励接到**钢铁版**池（NokkoColonyRewardsSteel → 深矿·企业重组·钢铁）",
    _tag == "钢铁之路 · 社区观测" and len(_pool) == 3,
    f"tag={_tag} 池轮次={list(_pool)}",
)
# ★ 数量口径逐项对拍（用户从沃沃截图给出的四个数字，钢铁版必须逐项命中；
#   普通版是 10,000×1 / 5,000×3 与 1000 / 2000 —— 若有人换回普通版，这四条会立刻红）
_flat = "、".join(x for r in ("A", "B", "C") for x in _pool[r])
check("★ 数量口径：10,000 现金匣 ×2（钢铁版 B 轮）", "10,000 现金匣 ×2" in _flat, _flat)
check("★ 数量口径：10,000 现金匣 ×3（钢铁版 C 轮）", "10,000 现金匣 ×3" in _flat, _flat)
check("★ 数量口径：3500 内融核心（70 包 × 50，钢铁版 B 轮）", "3500 内融核心" in _flat, _flat)
check("★ 数量口径：4000 内融核心（50 包 × 80，钢铁版 C 轮）", "4000 内融核心" in _flat, _flat)
check(
    "★ 普通版数值不得残留（1000/2000 内融核心、3 × 5,000 现金匣）",
    not any(x in _flat for x in ("1000 内融核心", "2000 内融核心", "3 × 5,000")),
    _flat,
)
check(
    "★ 三张钢铁表的部件对全在（机体+枪机 / 系统+枪管 / 头部神经光元+枪托）",
    all(
        n in _flat
        for n in (
            "Nokko机体蓝图",
            "蕈菇枪机蓝图",
            "Nokko系统蓝图",
            "蕈菇枪管蓝图",
            "Nokko头部神经光元蓝图",
            "蕈菇枪托蓝图",
        )
    ),
    _flat,
)
_line = fmt._bounty_entry_line("Solaris United", dict(_sup[0]))
check(
    "★ 卡面行：档名（**不写死任务名**）+ 等级 + 钢铁之路/社区观测双标签",
    "深矿（钢铁之路）" in _line
    and "130-140级" in _line
    and "钢铁之路" in _line
    and "社区观测" in _line
    and "解放小动物" not in _line
    and "企业重组" not in _line,
    _line,
)
_ven = [s for s in bundle["syndicateMissions"] if s.get("syndicateKey") == "SolarisSyndicate"]
check(
    "★ 集成：金星档位 7 → 8（fixture 解析后含 130-140 档）",
    bool(_ven)
    and len(_ven[0]["jobs"]) == 8
    and any(list(j["enemyLevels"]) == [130, 140] for j in _ven[0]["jobs"]),
    f"档数={len(_ven[0]['jobs']) if _ven else 0}",
)
# 详情卡：社区档列 A/B/C **全部轮次**（DE 不下发 → 当前轮次无从得知），并附注脚说明
_tv, _vlines = fmt.fmt_bounties(bundle["syndicateMissions"], "金星")
_idx = next(
    i
    for i, ln in enumerate(_vlines)
    if ln.startswith(fmt._BOUNTY_HEAD_PREFIX) and "深矿（钢铁之路）" in ln
)
_rew = _vlines[_idx + 1]  # 该档的奖励行（社区档无「任务：」行）
check(
    "★ 详情卡：社区档奖励行含两档现金匣（×2 与 ×3 都在，未被按名并掉）",
    "10,000 现金匣 ×2" in _rew and "10,000 现金匣 ×3" in _rew,
    _rew,
)
check(
    "★ 详情卡注脚说明「深矿任务轮换」+「社区版含资源、官方钢铁表不含」",
    any("轮换" in ln and "解放小动物" in ln for ln in _vlines)
    and any("官方钢铁表不含资源项" in ln for ln in _vlines),
    str([ln for ln in _vlines if ln.startswith("※")]),
)
check(
    "★ 详情卡注脚说明「社区观测档列全部轮次」",
    any(ln.startswith("※") and "社区观测" in ln and "全部轮次" in ln for ln in _vlines),
    str([ln for ln in _vlines if ln.startswith("※")]),
)

# ---------------------------------------------------------------------------
# 4) ★ 赏金卡「块圆角框」（2026-09-27 用户口径：淡线看不清 → 换圆角框）
# ---------------------------------------------------------------------------
# 背景：档位行是 normal 行，旧画线判据只在「下一行是 ◆ / 数字行」时才画 ⇒ 赏金卡
# **块与块之间一条线都没有**（实测改前 = 0 条）。修法：格式层给档位行加**显式块
# 标记** `▸`（`fmt._BOUNTY_HEAD_PREFIX`，5 处发射点共用），渲染层判成
# `bounty_head` 并在块首画线（◆ 区域行下方仍是主题色渐隐线，未动）。
# ★ 逐地区断言（防「只改了扎里曼」）：6 个地区 + 3 个 oracle 退路**各跑一遍**。
ROOT = Path(__file__).resolve().parent.parent
(ROOT / "runtime").mkdir(exist_ok=True)


def _sep_lines(title: str, lines: list):
    """渲染并数「块间细线」的**真实绘制次数**（打桩 ImageDraw.line，只数赏金签名）。

    不手抄渲染规则来复刻 —— 直接量实际画了几条，规则改了也不会假绿。
    同时数 ◆ 区域行的延伸细线（A 款绘制层：PANEL_LINE 色的 ImageDraw.line）以证明它没被替换掉。
    """
    from PIL import ImageDraw

    _hg: list = []
    nbox: list = []
    orig = ImageDraw.ImageDraw.line
    orig_rr = ImageDraw.ImageDraw.rounded_rectangle

    # 只数**赏金档位框**：卡上还有别的 rounded_rectangle（卡框/徽章），
    # 用描边色签名精确区分（外援版设计：TIER_FRAME_LINE = 主面板内框线色）。
    # 每个框画两次调用（底色 fill + 描边 outline），只数 outline 那次。
    _sig = R.TIER_FRAME_LINE

    def spy_rr(self, xy, *a, **k):
        if k.get("outline") == _sig:
            nbox.append(1)
        return orig_rr(self, xy, *a, **k)

    ImageDraw.ImageDraw.rounded_rectangle = spy_rr

    def spy(self, xy, fill=None, width=1, *a, **k):
        if fill == R.PANEL_LINE:
            _hg.append(1)
        return orig(self, xy, fill=fill, width=width, *a, **k)

    ImageDraw.ImageDraw.line = spy
    try:
        r = R.ImageRenderer(ROOT / "runtime")
        if not r.available:
            return None, None, None
        png = r.render(title, lines, "国际服")
        return len(nbox), len(_hg), (r, png)
    finally:
        ImageDraw.ImageDraw.line = orig
        ImageDraw.ImageDraw.rounded_rectangle = orig_rr


_REGION_CASES = [
    ("地球", "地球", ORACLE),
    ("金星", "金星", ORACLE),
    ("火卫二", "火卫二", ORACLE),
    ("扎里曼", "扎里曼", ORACLE),
    ("圣所", "圣所", ORACLE),
    ("1999", "1999", ORACLE),
    ("扎里曼退路", "扎里曼", {}),
    ("圣所退路", "圣所", {}),
    ("1999退路", "1999", {}),
]
for _label, _kw, _cyc in _REGION_CASES:
    _t, _ls = fmt.fmt_bounties(bundle["syndicateMissions"], _kw, _cyc)
    _blocks = sum(1 for x in _ls if x.lstrip("　").startswith("▸"))
    # 2026-10-02 起地球卡带点位三块（⟦tents⟧ 机器行 → 每行 3 个圆角框）
    _tents_n = sum(1 for x in _ls if x.startswith("⟦tents⟧"))
    _n, _nhg, _ctx = _sep_lines(_t, _ls)
    if _n is None:
        print("[SKIP] 渲染器不可用（缺字体），跳过块间分隔线用例")
        break
    check(
        f"★ 赏金块圆角框（{_label}）：{_blocks} 块 → {_n} 个框（应 = 块数 + 3×点位行）",
        _blocks >= 1 and _n == _blocks + 3 * _tents_n,
        f"块={_blocks} 框={_n} 点位行={_tents_n}",
    )
    check(f"赏金块圆角框（{_label}）：◆ 区域行的延伸细线保留", _nhg >= 1, str(_nhg))
    if _label == "地球":
        check(
            "★ 赏金卡未走 _plain_body（彩色 token 路径必须保留）",
            _ctx[0]._plain_body is False,
            str(_ctx[0]._plain_body),
        )
        from PIL import Image

        _im = Image.open(_ctx[1]).convert("RGB")
        _px, (_W, _H) = _im.load(), _im.size
        _cyan = sum(
            1
            for y in range(150, _H - 60, 2)
            for x in range(40, _W - 40, 2)
            if _px[x, y][1] > 120 and _px[x, y][2] > 120 and _px[x, y][0] < 120
        )
        check("★ 赏金卡彩色回归：任务类型色（青）像素仍存在", _cyan > 50, str(_cyan))

# 反例：只有一块的卡**一条块间线都不画**（卡首/卡尾不画、奖励行之间不加线）
_ost = next(s for s in bundle["syndicateMissions"] if s.get("syndicate") == "Ostrons")
_one = [dict(_ost, jobs=_ost["jobs"][:1])]
_t1, _l1 = fmt.fmt_bounties(_one, "地球", {})
_t1_tents = sum(1 for x in _l1 if x.startswith("⟦tents⟧"))
_n1, _, _ = _sep_lines(_t1, _l1)
check(
    "反例：单块卡 → 1 个档位框 + 3×点位框（不画卡首/卡尾多余框）",
    _n1 == 1 + 3 * _t1_tents and sum(1 for x in _l1 if x.lstrip("　").startswith("▸")) == 1,
    f"块=1 框={_n1} 点位行={_t1_tents}",
)

# ---------------------------------------------------------------------------
# 4) 点位三块（2026-10-02 改版）：机器行格式、解析、蓝色高亮像素
# ---------------------------------------------------------------------------
_tent_rows = [
    x
    for x in fmt.fmt_bounties(bundle["syndicateMissions"], "地球", ORACLE)[1]
    if x.startswith("⟦tents⟧")
]
check("★ 地球详情卡：点位机器行存在（1 行）", len(_tent_rows) == 1, str(len(_tent_rows)))
if _tent_rows:
    _blk = R._tents_of(_tent_rows[0])
    check(
        "★ 点位机器行解析：3 块、每块标题+任务",
        _blk is not None
        and len(_blk) == 3
        and all(len(b) >= 2 and b[0].startswith("小帐篷") for b in _blk),
        str(_blk)[:120],
    )
    _tc = R.text_card("t", _tent_rows)
    check(
        "★ text_card 展开机器行：3 个点位、无 ⟦tents⟧ 残留",
        "⟦tents⟧" not in _tc and _tc.count("小帐篷") == 3,
        _tc,
    )
# 蓝色高亮像素：地球卡上 4 条指定任务（捕获 Grineer 特工 等）染蓝（BLUE）
_sep3 = _sep_lines(*fmt.fmt_bounties(bundle["syndicateMissions"], "地球", ORACLE))
if _sep3[0] is not None:
    from PIL import Image

    _im3 = Image.open(_sep3[2][1]).convert("RGB")
    _px3, _W3H3 = _im3.load(), _im3.size
    _blue = sum(
        1
        for y in range(150, _W3H3[1] - 60, 2)
        for x in range(40, _W3H3[0] - 40, 2)
        if abs(_px3[x, y][0] - 111) < 30
        and abs(_px3[x, y][1] - 168) < 30
        and abs(_px3[x, y][2] - 220) < 30
    )
    check("★ 点位块蓝色高亮像素存在（4 条指定任务）", _blue > 30, str(_blue))

# —— 2026-10-02 追加：Grineer/Corpus 赏金卡不染色 + 1999 挑战名蓝色 ——
if _sep3[0] is not None:
    # 派系色（A 款 = 冷灰 R.FACTION_COLOR，与次文字灰接近，像素计数分不开）：
    # 改为打桩 ImageDraw.text，按「文本 + 填充色」精确数——赏金卡里
    # Grineer/Corpus 不得以派系色绘制；对照组（非赏金卡）必须能以派系色绘制。
    from PIL import ImageDraw as _ID

    def _fac_draws(title, lines):
        got: list = []
        _orig_text = _ID.ImageDraw.text

        def _spy_text(self, xy, text, fill=None, *a, **k):
            if fill == R.FACTION_COLOR and text in ("Grineer", "Corpus"):
                got.append(text)
            return _orig_text(self, xy, text, fill, *a, **k)

        _ID.ImageDraw.text = _spy_text
        try:
            R.ImageRenderer(ROOT / "runtime").render(title, lines, "国际服")
        finally:
            _ID.ImageDraw.text = _orig_text
        return got

    _fac = _fac_draws(*fmt.fmt_bounties(bundle["syndicateMissions"], "地球", ORACLE))
    check(
        "★ 地球卡 Grineer/Corpus 不再派系染色（按文本+填充色打桩计数 = 0）",
        len(_fac) == 0,
        str(_fac),
    )
    _fac_ctl = _fac_draws("每日突击", ["· Grineer 据点　Corpus 前哨"])
    check(
        "★ 派系染色对照组：非赏金卡的 Grineer/Corpus 仍以派系色绘制",
        len(_fac_ctl) >= 1,
        str(_fac_ctl),
    )
    _t99, _l99 = fmt.fmt_bounties(bundle["syndicateMissions"], "1999", cycle=ORACLE)
    _r99 = R.ImageRenderer(ROOT / "runtime")
    _png99 = _r99.render(_t99, _l99, "国际服") if _r99.available else None
    if _png99:
        _im99 = Image.open(_png99).convert("RGB")
        _px99, _WH99 = _im99.load(), _im99.size
        _blue99 = sum(
            1
            for y in range(150, _WH99[1] - 60, 2)
            for x in range(40, _WH99[0] - 40, 2)
            if abs(_px99[x, y][0] - 111) < 30
            and abs(_px99[x, y][1] - 168) < 30
            and abs(_px99[x, y][2] - 220) < 30
        )
        check("★ 1999 卡挑战名蓝色像素存在（⟦c⟧ 行染蓝）", _blue99 > 30, str(_blue99))
    else:
        print("[SKIP] 渲染器不可用，跳过 1999 蓝色像素用例")

# ---------------------------------------------------------------------------
# C2 倒计时复核（2026-10-03）：oracle 三地区的横幅倒计时取 cycle["expiry"]
# （与 DE 地区 SyndicateMissions[].Expiry 同口径）——此前一览缺倒计时。
# ---------------------------------------------------------------------------
import copy as _copy  # noqa: E402

_ORACLE_EXP = _copy.deepcopy(ORACLE)
_ORACLE_EXP["expiry"] = "2030-01-01T00:00:00+00:00"
_tc, clines = fmt.fmt_bounties(bundle["syndicateMissions"], cycle=_ORACLE_EXP)
_cb = [ln for ln in clines if ln.startswith("◆ ")]
check(
    "C2 一览：oracle 三地区有倒计时（扎里曼/实验室/1999）",
    all(
        any(r in ln and "剩" in ln for ln in _cb)
        for r in ("羽化之穹（扎里曼）", "解剖圣所（实验室）", "霍瓦尼亚（1999）")
    ),
    str(_cb),
)
check(
    "C2 一览：DE 三地区仍按 Syndicates[].Expiry 画倒计时",
    all(
        any(r in ln and "剩" in ln for ln in _cb)
        for r in ("希图斯（地球）", "奥布山谷（金星）", "英择谛（魔胎之境）")
    ),
    str(_cb),
)
_tc2, dlines2 = fmt.fmt_bounties(bundle["syndicateMissions"], "扎里曼", cycle=_ORACLE_EXP)
check(
    "C2 详情：扎里曼横幅也有倒计时",
    any(ln.startswith("◆ ") and "剩" in ln for ln in dlines2),
    str(dlines2[:2]),
)
check(
    "C2 无 expiry 时横幅保持原样（不画空倒计时）",
    all("剩" not in ln for ln in _banner if "羽化之穹" in ln),
    str(_banner),
)

# ---------------------------------------------------------------------------
# B11（2026-10-11）赏金卡审计修复
# ---------------------------------------------------------------------------
# ④ 火卫二各档轮次不同：按每档自己的 rewardTable 汇总，不再拿第一个 job 套全地区
_deimos_jobs = [
    {"rewardTable": "TierATableBRewards", "enemyLevels": [5, 15]},
    {"rewardTable": "TierCTableBRewards", "enemyLevels": [15, 25]},
    {"rewardTable": "TierBTableARewards", "enemyLevels": [25, 30]},
    {"rewardTable": "TierDTableARewards", "enemyLevels": [30, 40]},
    {"rewardTable": "TierETableARewards", "enemyLevels": [40, 60]},
]
_rmap = fmt._region_rot_map("Entrati", _deimos_jobs)
check(
    "④ 火卫二按档取轮次（阶段1/2=B、阶段3/4/5=A）",
    _rmap == {"阶段1": "B", "阶段2": "B", "阶段3": "A", "阶段4": "A", "阶段5": "A"},
    str(_rmap),
)
_dpool = fmt._BOUNTY_POOLS["Entrati"]
_exp_items: list = []
for _k, _r in _rmap.items():
    _exp_items.extend(fmt._pool_at_rot(_dpool[_k], _r))
_exp_mods = [
    t
    for t in fmt._fmt_pool_items(_exp_items).split("、")
    if t.startswith("★") and fmt._is_high_value(t)
]
_rl = fmt._rot_lines(_dpool, _rmap)
check(
    "④ 轮换行 = 各档自身轮次的并集（标注各档轮次）",
    _rl and "各档轮次 A/B" in _rl[0] and _rl[0].endswith("、".join(_exp_mods[:6])),
    str(_rl),
)
check(
    "④ 整区同轮次时仍标单字母",
    "（B）" in fmt._rot_lines(_dpool, {"阶段1": "B", "阶段2": "B"})[0],
)

# ⑫ 深矿档：名字已含「钢铁之路」⇒ 标签不再重复
_venus = next(s for s in bundle["syndicateMissions"] if s["syndicate"] == "Solaris United")
_tv, _vl = fmt.fmt_bounties([_venus], "金星", cycle={})
_deep = [ln for ln in _vl if "深矿" in ln and ln.startswith(fmt._BOUNTY_HEAD_PREFIX)]
check(
    "⑫ 深矿行「钢铁之路」只出现一次",
    _deep and all(ln.count("钢铁之路") == 1 for ln in _deep) and "社区观测" in _deep[0],
    str(_deep),
)
check("⑫ 金星地名统一官方「奥布山谷」", any("◆ 奥布山谷（金星）" in ln for ln in _vl), _vl[0])

# ⑰ 「深矿」「抢劫」聚焦对应档位
_t1, _dm = fmt.fmt_bounties([_venus], "深矿", cycle={})
_heads = [ln for ln in _dm if ln.startswith(fmt._BOUNTY_HEAD_PREFIX)]
check("⑰ 赏金 深矿 只列深矿档", _heads and all("深矿" in ln for ln in _heads), str(_heads))
from core import parser as _P  # noqa: E402

check(
    "⑰ 裸发「抢劫」preset=抢劫（不再落到深矿）",
    _P.parse("抢劫").preset == "抢劫" and _P.parse("深矿").preset == "深矿",
    str(_P.parse("抢劫").preset),
)
_t2, _hs = fmt.fmt_bounties([_venus], "抢劫", cycle={})
check(
    "⑰ 赏金 抢劫 列抢劫奖励池（不再整板金星）",
    any("· 抢劫" in ln for ln in _hs)
    and not any(ln.startswith(fmt._BOUNTY_HEAD_PREFIX) for ln in _hs),
    str(_hs[:3]),
)

# ⑫ 1999 一览：节点名即任务类型时不再重复出「任务：传承种收割 …」
_cyc99 = {
    "bounties": {
        "HexSyndicate": [
            {
                "node": "SolNode850",
                "challenge": "/Lotus/Types/Challenges/Vania/Lich/LichVaniaDestroySpeakers",
            }
        ]
    }
}
_ov = fmt._oracle_summary(
    "HexCity", "HexSyndicate", "霍瓦尼亚（1999）", _cyc99["bounties"]["HexSyndicate"]
)
_hx = [ln for ln in _ov if "传承种收割" in ln]
check(
    "⑫ 1999 一览「传承种收割」只出现一次（挑战名并入档位行）",
    len(_hx) == 1 and "噪音污染" in _hx[0] and _hx[0].count("传承种收割") == 1,
    str(_ov),
)

if FAILED:
    print(f"\n失败 {len(FAILED)} 项：{FAILED}")
    sys.exit(1)
print("\n全部通过 ✔")
