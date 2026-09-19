# -*- coding: utf-8 -*-
"""蹲点推送：死链修复与筛选语义的回归守卫（2026-09-19）。

背景（用户反馈「蹲 仲裁 高效 永久 没生效」）：
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
    return "\n".join(ln for ln in text.splitlines()
                     if not ln.strip().startswith("#"))


push_code = _code_lines(push_src)
check("push 不再调用恒抛的 client.arbitration()",
      "client.arbitration(" not in push_code)
check("push 不再调用恒抛的 client.steel_path()（注意别误伤 _incursions）",
      "client.steel_path(" not in push_code
      and "client.steel_path_incursions(" in push_code)
check("push 的仲裁走 core.arbi（与查询指令同一实现）",
      "arbi.current(" in push_src and "arbi.match_rule(" in push_src)
check("core/arbi.py 存在且数据源为 arbi.wf.wiki",
      (ROOT / "core" / "arbi.py").is_file()
      and "arbi.wf.wiki" in (ROOT / "core" / "arbi.py").read_text(encoding="utf-8"))
check("main 的仲裁辅助已委托 arbi（单一实现，无第二份拷贝）",
      main_src.count("def _arb_mission(") == 0
      and "_arb_mission = _arbi.mission_of" in main_src)
check("api_client 仍在（历史事实留痕）：arbitration 恒抛、steelPath 恒抛",
      "raise WarframeAPIError(_EXTERNAL_ONLY[\"arbitration\"])" in api_src)

print()
print("=== 二、PUSH_EVENTS 可订阅性 ===")
from core.push import PUSH_EVENTS                                    # noqa: E402

check("「警报」标为不可订阅（DE 已停用，数据恒空）",
      PUSH_EVENTS["警报"][1] is False and "停用" in PUSH_EVENTS["警报"][0],
      str(PUSH_EVENTS["警报"]))
check("「仲裁」标为可订阅且描述不再写「外部源不可用」",
      PUSH_EVENTS["仲裁"][1] is True and "不可用" not in PUSH_EVENTS["仲裁"][0],
      str(PUSH_EVENTS["仲裁"]))
check("「钢路侵袭」标为可订阅", PUSH_EVENTS["钢路侵袭"][1] is True)

print()
print("=== 三、arbi 筛选语义（parse_rule / match_rule）===")
from core import arbi                                                # noqa: E402

SLOT = {"mission": "防御", "tier": "A+", "node": "X", "line": "X"}
check("空规则 = 全推", arbi.match_rule("", SLOT) is True)
check("高效(S/A+/A) 命中 A+", arbi.match_rule("高效", SLOT) is True)
check("传奇(S) 不命中 A+", arbi.match_rule("传奇", SLOT) is False)
check("任务类型命中", arbi.match_rule("防御", SLOT) is True)
check("任务类型不命中", arbi.match_rule("生存", SLOT) is False)
check("类型+评级 同时满足才推（AND）",
      arbi.match_rule("防御,高效", SLOT) is True
      and arbi.match_rule("生存,高效", SLOT) is False)
check("未评级场次不会被「高效」误命中",
      arbi.match_rule("高效", {**SLOT, "tier": ""}) is False)
check("未知词不阻塞（拼错也不至于永远收不到）",
      arbi.match_rule("高效mispell", {**SLOT, "tier": "S"}) is True)
check("parse_rule 能报出未知词（供订阅时提示）",
      arbi.parse_rule("高效,乱写")[2] == ["乱写"],
      str(arbi.parse_rule("高效,乱写")))
check("rule_supported：仅 裂隙 / 仲裁 支持筛选",
      arbi.rule_supported("仲裁") and arbi.rule_supported("裂隙")
      and not arbi.rule_supported("奸商"))

print()
print("=== 四、推送侧端到端（桩 client，不联网）===")
from core.logging_compat import logger as CORE_LOG                   # noqa: E402
from core.push import PushDaemon                                     # noqa: E402
from core.store import Subscription, SubscriptionStore               # noqa: E402

# ★ startTs 用「当前整点」：idx 恒为 0 → 当前场次稳定落在 seq[0]=0 → nodeA（A+ 防御），
#   否则测试结果会随运行时刻漂移（第一版就踩到：断言「节点A」随机失败）。
SCHED = {"seq": [0, 0, 1, 1], "startTs": int(time.time() // 3600) * 3600,
         "stepSec": 3600, "nodes": ["nodeA", "nodeB"]}
NODES = {
    "nodeA": {"nameZh": "节点A", "systemNameZh": "土星", "missionNameZh": "防御",
              "factionNameZh": "Grineer"},
    "nodeB": {"nameZh": "节点B", "systemNameZh": "火星",
              "missionNameZh": "INFESTED 资源回收", "factionNameZh": "Infestation"},
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

    async def send(umo, text):
        sent.append((umo, text))

    p = PushDaemon(StubClient(), store, send, CORE_LOG, interval=999)

    async def scenario(rule: str, tamper: str):
        st = SubscriptionStore(Path("/tmp") / f"test_dun_{tamper}_{abs(hash(rule))}.json")
        await st.add(Subscription(umo=f"u:{rule}", platform="pc",
                                  event=tamper, rule=rule, once=False, until=-1))
        subs = st.all()
        await p._snapshot_diff("pc", subs)          # 建基线
        key = "arbi_slot" if tamper == "仲裁" else "sp_incursions"
        p._last["pc"][key] = "tampered"             # 模拟轮换
        evs = await p._snapshot_diff("pc", subs)
        return [e for e in evs if e[0] == tamper]

    # 仲裁：rule 空 → 产出；rule 高效（当前 nodeA 是 A+）→ 产出；
    #       rule 生存（当前是防御）→ 不产出
    ev_all = await scenario("", "仲裁")
    check("★ 蹲 仲裁（无筛选）能产出推送", len(ev_all) == 1, str(ev_all))
    check("推送文本含节点/类型/评级",
          bool(ev_all) and "⚖️ 仲裁已轮换" in ev_all[0][2]
          and "节点A" in ev_all[0][2])

    ev_eff = await scenario("高效", "仲裁")
    check("★ 蹲 仲裁 高效：当前 A+ 场次命中 → 产出", len(ev_eff) == 1)
    ev_no = await scenario("生存", "仲裁")
    check("★ 蹲 仲裁 生存：当前防御场次不命中 → 不产出（筛选真的生效）",
          len(ev_no) == 0)

    ev_sp = await scenario("", "钢路侵袭")
    check("★ 蹲 钢路侵袭能产出推送（原为死链）", len(ev_sp) == 1, str(ev_sp))


asyncio.run(run_push())

print()
if _fails:
    print(f"✗ {len(_fails)} 项失败: {_fails}")
    raise SystemExit(1)
print("✓ 蹲点推送死链与筛选回归全部通过")
