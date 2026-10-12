# -*- coding: utf-8 -*-
"""蹲点推送：死链修复与筛选语义的回归守卫（2026-09-19）。

背景（玩家反馈「蹲 仲裁 高效 永久 没生效」）：
  ① `client.arbitration()` 在 10o.io 停摆后**恒抛**，push 侧 `except: pass`
     静默吞掉 → 「蹲 仲裁」永远不会推；
  ② 同为恒抛的还有 `client.steel_path()` → 「蹲 钢路侵袭」也不会推；
  ③ 仲裁/其它非裂隙类型的 `rule` 筛选被**整体忽略**（写了「高效」等于没写）。
本文件把「数据源接线正确 + 筛选真的生效」钉死，避免再退化。
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_fails: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  ✓ " if cond else "  ✗ ") + name + (f"　[{extra}]" if extra and not cond else ""))
    if not cond:
        _fails.append(name)


print("=== 一、死链检查（静态）===")
push_src = (ROOT / "core" / "push.py").read_text(encoding="utf-8")
main_src = (ROOT / "main.py").read_text(encoding="utf-8")
api_src = (ROOT / "core" / "api_client.py").read_text(encoding="utf-8")


def _code_lines(text: str) -> str:
    """去掉注释行后的代码（静态检查不能把注释里的历史说明当成调用）。"""
    return "\n".join(ln for ln in text.splitlines() if not ln.strip().startswith("#"))


push_code = _code_lines(push_src)
check("push 不再调用恒抛的 client.arbitration()", "client.arbitration(" not in push_code)
check(
    "push 不再调用恒抛的 client.steel_path()（注意别误伤 _incursions）",
    "client.steel_path(" not in push_code and "client.steel_path_incursions(" in push_code,
)
check(
    "push 的仲裁走 core.arbi（与查询指令同一实现）",
    "arbi.current(" in push_src and "arbi.match_rule(" in push_src,
)
check(
    "core/arbi.py 存在且数据源为 arbi.wf.wiki",
    (ROOT / "core" / "arbi.py").is_file()
    and "arbi.wf.wiki" in (ROOT / "core" / "arbi.py").read_text(encoding="utf-8"),
)
# 2026-09-28 D3：仲裁别名块随域迁 core/commands/arbitration.py——单一实现
# 委托行在现址核验，且 main.py 与现址都不得出现第二份 def 拷贝。
_arb_cmd_src = (ROOT / "core" / "commands" / "arbitration.py").read_text(encoding="utf-8")
check(
    "main 的仲裁辅助已委托 arbi（单一实现，无第二份拷贝）",
    main_src.count("def _arb_mission(") == 0
    and "def _arb_mission(" not in _arb_cmd_src
    and "_arb_mission = _arbi.mission_of" in _arb_cmd_src,
)
check(
    "api_client 仍在（历史事实留痕）：arbitration 恒抛、steelPath 恒抛",
    'raise WarframeAPIError(_EXTERNAL_ONLY["arbitration"])' in api_src,
)

print()
print("=== 二、PUSH_EVENTS 可订阅性 ===")
from core.push import PUSH_EVENTS  # noqa: E402

check(
    "「警报」已接线（C3：常规停用、活动型会下发）",
    PUSH_EVENTS["警报"][1] is True and "活动型" in PUSH_EVENTS["警报"][0],
    str(PUSH_EVENTS["警报"]),
)
check(
    "「仲裁」标为可订阅且描述不再写「外部源不可用」",
    PUSH_EVENTS["仲裁"][1] is True and "不可用" not in PUSH_EVENTS["仲裁"][0],
    str(PUSH_EVENTS["仲裁"]),
)
check("「钢路侵袭」标为可订阅", PUSH_EVENTS["钢路侵袭"][1] is True)

print()
print("=== 三、arbi 筛选语义（parse_rule / match_rule）===")
from core import arbi  # noqa: E402

SLOT = {"mission": "防御", "tier": "A+", "node": "X", "line": "X"}
check("空规则 = 全推", arbi.match_rule("", SLOT) is True)
check("高效(S/A+/A) 命中 A+", arbi.match_rule("高效", SLOT) is True)
check("传奇(S) 不命中 A+", arbi.match_rule("传奇", SLOT) is False)
check("任务类型命中", arbi.match_rule("防御", SLOT) is True)
check("任务类型不命中", arbi.match_rule("生存", SLOT) is False)
check(
    "类型+评级 同时满足才推（AND）",
    arbi.match_rule("防御,高效", SLOT) is True and arbi.match_rule("生存,高效", SLOT) is False,
)
check("未评级场次不会被「高效」误命中", arbi.match_rule("高效", {**SLOT, "tier": ""}) is False)
check(
    "未知词不阻塞（拼错也不至于永远收不到）",
    arbi.match_rule("高效mispell", {**SLOT, "tier": "S"}) is True,
)
check(
    "parse_rule 能报出未知词（供订阅时提示）",
    arbi.parse_rule("高效,乱写")[2] == ["乱写"],
    str(arbi.parse_rule("高效,乱写")),
)
check(
    "rule_supported：仅 裂隙 / 仲裁 支持筛选",
    arbi.rule_supported("仲裁") and arbi.rule_supported("裂隙") and not arbi.rule_supported("奸商"),
)

print()
print("=== 四、推送侧端到端（桩 client，不联网）===")
from core.logging_compat import logger as CORE_LOG  # noqa: E402
from core.push import PushDaemon  # noqa: E402
from core.store import Subscription, SubscriptionStore  # noqa: E402

# ★ startTs 用「当前整点」：idx 恒为 0 → 当前场次稳定落在 seq[0]=0 → nodeA（A+ 防御），
#   否则测试结果会随运行时刻漂移（第一版就踩到：断言「节点A」随机失败）。
SCHED = {
    "seq": [0, 0, 1, 1],
    "startTs": int(time.time() // 3600) * 3600,
    "stepSec": 3600,
    "nodes": ["nodeA", "nodeB"],
}
NODES = {
    "nodeA": {
        "nameZh": "节点A",
        "systemNameZh": "土星",
        "missionNameZh": "防御",
        "factionNameZh": "Grineer",
    },
    "nodeB": {
        "nameZh": "节点B",
        "systemNameZh": "火星",
        "missionNameZh": "INFESTED 资源回收",
        "factionNameZh": "Infestation",
    },
}
TIERS = {"tierBuckets": {"A+": ["nodeA"], "S": []}}


class StubClient:
    """只实现蹲点采集用到的方法（其余类型不在本测试的订阅里）。"""

    async def _fetch_json(self, url, ttl=0, **kw):
        if "schedule" in url:
            return SCHED
        if "nodes" in url:
            return {"nodes": NODES}
        if "tierlist" in url:
            return TIERS
        return {}

    async def steel_path_incursions(self, platform):
        return {"nodes": ["Ceres/Defense", "Jupiter/Survival"], "expiry": "x"}


async def run_push():
    store = SubscriptionStore(Path("/tmp") / "test_dun_push_subs.json")
    sent: list[tuple[str, str]] = []

    async def send(umo, text, at=None):
        sent.append((umo, text))

    p = PushDaemon(StubClient(), store, send, CORE_LOG, interval=999)

    async def scenario(rule: str, tamper: str):
        st = SubscriptionStore(Path("/tmp") / f"test_dun_{tamper}_{abs(hash(rule))}.json")
        await st.add(
            Subscription(
                umo=f"u:{rule}", platform="pc", event=tamper, rule=rule, once=False, until=-1
            )
        )
        subs = st.all()
        await p._snapshot_diff("pc", subs)  # 建基线
        key = "arbi_slot" if tamper == "仲裁" else "sp_incursions"
        p._last["pc"][key] = "tampered"  # 模拟轮换
        evs = await p._snapshot_diff("pc", subs)
        return [e for e in evs if e[0] == tamper]

    # 仲裁：rule 空 → 产出；rule 高效（当前 nodeA 是 A+）→ 产出；
    #       rule 生存（当前是防御）→ 不产出
    ev_all = await scenario("", "仲裁")
    check("★ 蹲 仲裁（无筛选）能产出推送", len(ev_all) == 1, str(ev_all))
    check(
        "推送文本含节点/类型/评级",
        bool(ev_all) and "⚖️ 仲裁已轮换" in ev_all[0][2] and "节点A" in ev_all[0][2],
    )

    ev_eff = await scenario("高效", "仲裁")
    check("★ 蹲 仲裁 高效：当前 A+ 场次命中 → 产出", len(ev_eff) == 1)
    ev_no = await scenario("生存", "仲裁")
    check("★ 蹲 仲裁 生存：当前防御场次不命中 → 不产出（筛选真的生效）", len(ev_no) == 0)

    ev_sp = await scenario("", "钢路侵袭")
    check("★ 蹲 钢路侵袭能产出推送（原为死链）", len(ev_sp) == 1, str(ev_sp))


asyncio.run(run_push())

# ---------------------------------------------------------------------------
# 五、蹲「空格陷阱」提示（2026-09-26 审核通过；只提示、不改语义）
#   实测触发：`蹲 裂隙 钢铁 防御 永久` 落库 `钢铁,防御` = 钢铁 **或** 普通防御
#   （`FissureFilter` 的 groups 之间是 any/OR），比用户预期宽得多。
# ---------------------------------------------------------------------------
print("=== 五、空格陷阱提示 ===")
from core.parser import (  # noqa: E402
    dun_rule_hint,
    parse,
    parse_fissure_filter,
)

# 5.1 提示函数：正反两侧
for parts in (
    ["钢铁", "防御"],
    ["虚空", "捕获"],
    ["后纪", "捕获"],
    ["钢铁", "虚空", "防御"],
    ["九重天", "拦截"],
):
    h = dun_rule_hint(parts)
    check(
        f"应提示：{parts} → 给出连写与引号两种改法",
        bool(h) and "".join(parts) in h and '"' in h and "\n" not in h,
        h,
    )
for parts in (["钢铁防御"], ["捕获", "生存"], ["高效"], [], ["防御"]):
    check(f"不应提示：{parts}", dun_rule_hint(parts) == "", dun_rule_hint(parts))
check("提示是**单行**文案（卡片高度只 +1 行）", "\n" not in dun_rule_hint(["钢铁", "防御"]))


# 5.2 指令入口：三种写法（与用户实测口径一致）
def _rule_of(text: str) -> tuple:
    toks = list(parse(text).content or [])
    parts = [x for x in toks if x != "永久"]
    return ",".join(parts), parts


_rule_a, _parts_a = _rule_of("裂隙 钢铁 防御 永久")
_rule_b, _parts_b = _rule_of("裂隙 钢铁防御 永久")
_rule_c, _parts_c = _rule_of('裂隙 "钢铁 防御" 永久')
check(
    "★ 分开写：落库 钢铁,防御 且**触发**提示",
    _rule_a == "钢铁,防御" and bool(dun_rule_hint(_parts_a)),
    _rule_a,
)
check(
    "★ 连写：落库 钢铁防御 且**不**提示",
    _rule_b == "钢铁防御" and dun_rule_hint(_parts_b) == "",
    _rule_b,
)
check(
    "★ 引号写法：落库 钢铁 防御（词元内空格合并）且**不**提示",
    _rule_c == "钢铁 防御" and dun_rule_hint(_parts_c) == "",
    _rule_c,
)
check(
    "★ 引号写法语义正确（= 钢铁之路的防御，不是取或）",
    parse_fissure_filter(_rule_c).describe() == "钢铁防御",
    parse_fissure_filter(_rule_c).describe(),
)
# ★ 2026-10-11 改断言：describe 不再给未写难度的组补「普通」（「防御」组实际不限难度，
#   旧描述「普通防御」与匹配语义不符）；语义变宽本身另用 match 实测锁住。
check(
    "分开写语义确实变宽（= 钢铁 **或** 防御，两组取或）",
    parse_fissure_filter(_rule_a).describe() == "钢铁，防御"
    and parse_fissure_filter(_rule_a).match({"isHard": True, "missionType": "生存"})
    and parse_fissure_filter(_rule_a).match({"isHard": False, "missionType": "防御"}),
    parse_fissure_filter(_rule_a).describe(),
)

# 5.2b 档位提示（2026-10-03 T1–T6）：越界/孤立 t 必提示；合法档位不提示
from core.parser import fissure_tier_hint  # noqa: E402

_t_hint_bad = fissure_tier_hint(["T0", "歼灭"])
check(
    "档位提示：越界编号非空且含 T1–T6 对照表",
    bool(_t_hint_bad) and "T1–T6" in _t_hint_bad and "T5安魂" in _t_hint_bad,
    _t_hint_bad,
)
check(
    "档位提示：合法写法（T5连写/安魂/T1 钢铁连写）均为空串",
    fissure_tier_hint(["T5歼灭"]) == ""
    and fissure_tier_hint(["安魂", "歼灭"]) == ""
    and fissure_tier_hint(["钢铁T1生存"]) == "",
)

# 5.3 静态守卫：提示只进回执，**不得参与 rule 构造**（防有人顺手改成自动合并）
# 2026-09-29 D11：_h_dun 随蹲订阅域迁 core/commands/dun.py，本段断言改读现址
dun_src = (ROOT / "core" / "commands" / "dun.py").read_text(encoding="utf-8")
_i_join = dun_src.index('rule=",".join(rule_parts)')
_i_hint = dun_src.index("dun_rule_hint(rule_parts)")
check(
    "★ 提示调用在 rule 拼接**之后**（不改语义）",
    _i_join < _i_hint,
    f"join@{_i_join} vs hint@{_i_hint}",
)
check(
    "★ 提示只在「裂隙」分支（非裂隙类型 rule 语义不同，本轮不做）",
    'if event_type == "裂隙":' in dun_src[max(0, _i_hint - 600) : _i_hint],
)
check(
    "★ 修饰词清单同源：解析与提示共用 FISSURE_MODIFIER_WORDS",
    "FISSURE_MODIFIER_WORDS" in (ROOT / "core" / "parser.py").read_text(encoding="utf-8")
    and "FISSURE_MODIFIER_WORDS"
    in (ROOT / "core" / "parser.py").read_text(encoding="utf-8").split("def dun_rule_hint")[1],
)

print()
if _fails:
    print(f"✗ {len(_fails)} 项失败: {_fails}")
    raise SystemExit(1)
print("✓ 蹲点推送死链与筛选回归全部通过")
