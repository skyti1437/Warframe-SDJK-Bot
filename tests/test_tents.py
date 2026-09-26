# -*- coding: utf-8 -*-
"""小帐篷赏金推算的回归测试：python3 tests/test_tents.py

两条**独立来源**的验证向量（2026-09-24 实测，缺一不可）：

1. seed=45267（W4 窗口 16:17–18:47 CST）——与沃沃智能体截图（17:10 CST）
   的「小帐篷 A/B/C」9/9 格逐格一致；
2. seed=69703（W5 窗口 18:47–21:17 CST）——与 oracle.browse.wf/location-bounties
   的独立 Pluto 实现 15/15 点位（地球 3 + 金星 7 + 火卫二 5）逐格一致，
   本测试只固化其中的地球 3 帐。

再叠加：降级行为（无种子/未知地区/坏种子）与卡片接线（详情卡带、一览卡不带）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import de_worldstate as dw  # noqa: E402
from core import formatters as fmt  # noqa: E402
from core import tents  # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = ""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f"  -> {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# 1) 双向量：算法输出逐格一致
# ---------------------------------------------------------------------------
VECTORS = {
    45267: {   # W4 —— 沃沃截图（17:10 CST）
        "小帐篷 A": ["破坏 Grineer 的补给线", "削弱 Grineer 的据点", "宰杀敌人"],
        "小帐篷 B": ["宰杀敌人", "捕获 Grineer 特工", "找出遗失的器物"],
        "小帐篷 C": ["刺杀指挥官", "破坏 Grineer 的补给线", "破坏原型机"],
    },
    69703: {   # W5 —— oracle.browse.wf 独立实现
        "小帐篷 A": ["捕获新任 Grineer 指挥官", "取回被偷的器物", "间谍捕手"],
        "小帐篷 B": ["削弱 Grineer 的据点", "宰杀敌人", "捕获 Grineer 特工"],
        "小帐篷 C": ["破坏 Grineer 的补给线", "找出遗失的器物", "破坏原型机"],
    },
}
for _seed, _want in VECTORS.items():
    got = dict(tents.region_locations("Ostrons", _seed))
    check(f"seed={_seed} 三个帐篷且顺序稳定",
          list(got) == ["小帐篷 A", "小帐篷 B", "小帐篷 C"], str(list(got)))
    for _label, _names in _want.items():
        check(f"seed={_seed} {_label} 逐格一致",
              got.get(_label) == _names, str(got.get(_label)))

# ---------------------------------------------------------------------------
# 2) 降级：无种子 / 未知地区 / 坏种子 / 每帐篷恒 3 条
# ---------------------------------------------------------------------------
check("无种子返回空", tents.region_locations("Ostrons", None) == [])
check("未知地区返回空", tents.region_locations("Nope", 45267) == [])
check("坏种子安全返回空", tents.region_locations("Ostrons", "abc") == [])
check("seed_of 取 CetusSyndicate 的种子",
      tents.seed_of([{"syndicateKey": "SolarisSyndicate", "seed": 1},
                     {"syndicateKey": "CetusSyndicate", "seed": 45267}]) == 45267)
check("seed_of 无 Cetus 条目返回 None",
      tents.seed_of([{"syndicateKey": "SolarisSyndicate", "seed": 1}]) is None)
_rand = tents.region_locations("Ostrons", 43210)
check("任意种子下每帐篷恒 3 条", all(len(n) == 3 for _, n in _rand), str(_rand))
check("链名全部解析成功（无裸路径/空名）",
      all(n and not n.startswith("/") for _, names in _rand for n in names),
      str(_rand))

# ---------------------------------------------------------------------------
# 3) 卡片接线：详情卡「赏金 地球」带小帐篷，一览卡保持紧凑不带
# ---------------------------------------------------------------------------
raw = json.loads((ROOT / "tests" / "fixtures" / "de_worldstate.json")
                 .read_text(encoding="utf-8"))
bundle = dw.parse_worldstate(raw, now_ms=1788964350000)
_cet = next(s for s in bundle["syndicateMissions"]
            if s["syndicateKey"] == "CetusSyndicate")
check("解析层带 seed（fixture 实测 59620）", _cet.get("seed") == 59620, str(_cet.get("seed")))

_te, earth = fmt.fmt_bounties(bundle["syndicateMissions"], "地球")
_tent_rows = [ln for ln in earth if "小帐篷" in ln and "※" not in ln]
check("详情卡有小帐篷 A/B/C 三行", len(_tent_rows) == 3, str(_tent_rows))
check("详情卡小帐篷行带来源注脚",
      any(ln.startswith("※") and "小帐篷" in ln for ln in earth))
_tov, ov = fmt.fmt_bounties(bundle["syndicateMissions"])
check("一览卡不带小帐篷（保持紧凑）",
      not any("小帐篷" in ln for ln in ov), str([ln for ln in ov if "小帐篷" in ln]))

# 无种子时不出现半截块（标题在、内容空这种）
_no_seed = [dict(s, seed=None) for s in bundle["syndicateMissions"]]
_tn, earth_ns = fmt.fmt_bounties(_no_seed, "地球")
check("种子缺失时整块跳过",
      not any("小帐篷" in ln for ln in earth_ns), str([ln for ln in earth_ns if "小帐篷" in ln]))

# ---------------------------------------------------------------------------
# ★ 金星：**推算能力保留、卡面不上**（2026-09-26 用户决定 —— 金星卡改列深矿钢铁档奖励）
#   数据来源：oracle VenusJobManifest（构建期抽成随包文件）+ 官方简中表
#   /Lotus/Language/SolarisVenus/MapLabel_*；7 点位算法已于 09-24 与 oracle 对拍 15/15。
#   重新上卡 = 把 `tents.REGIONS` 里金星那段取消注释，渲染侧 `_tent_lines` 自动出块
#   （注脚 `_TENT_NOTE["Solaris United"]` 也还在），**不需要**改其它文件。
# ---------------------------------------------------------------------------
_ven = tents.region_locations("Solaris United", 69703)
check("★ 金星推算能力保留：7 个点位（算法未动，随时可重开）",
      len(_ven) == 7, str(len(_ven)))
_names = [n for n, _ in _ven]
check("★ 点位名 = 5 个官方简中 + 2 个「未定名」（不硬编）",
      sorted(_names) == sorted(["殿宇建造地", "中央维修点", "反应器控制点", "生长地点",
                                "珍珠", "未定名", "未定名"]), str(_names))
check("★ 回归向量（seed=69703，与 oracle 对拍过的种子）：首点位链名",
      _ven[0] == ("殿宇建造地", ["伏击信使", "财政解放", "尘土部队"]), str(_ven[0]))
check("每个点位都给出 3 条链且全为中文（官方简中表命中）",
      all(len(chains) == 3 and all(not c.isascii() for c in chains)
          for _, chains in _ven), str(_ven[:2]))
check("★ 金星登记但 on_card=False（推算保留、卡面不出块）",
      "Solaris United" in tents.REGIONS
      and not tents.on_card("Solaris United") and tents.on_card("Ostrons"),
      str(sorted(tents.REGIONS)))
check("火卫二仍不上卡（官方 0/5 点位名，等补译）",
      "Entrati" not in tents.REGIONS and not tents.region_locations("Entrati", 69703))

# 口径一致：DE 下发的档位 job 应能在 manifest 任务池里找到（两个已知例外见注释）
_man = json.loads((ROOT / "core" / "data" / "de" / "venus_job_manifest.json")
                  .read_text(encoding="utf-8"))["data"]
_locs = _man.get("LocationSpecificJobs") or []
check("随包金星 manifest：7 个 LocationSpecificJobs（构建期抽取）",
      len(_locs) == 7 and {x.get("LocationTag") for x in _locs} >= {
          "BountyNefsHead", "BountyWarehouse", "BountyRepairBay", "BountySunDial",
          "BountyPufferFarm", "BountyCoolantCoil", "BountyDigSite"}, str(len(_locs)))
_pool = set(_man.get("Jobs") or [])
_sect = [s for s in bundle["syndicateMissions"] if s.get("syndicateKey") == "SolarisSyndicate"]
_tiers = {j["jobTypeKey"] for j in (_sect[0]["jobs"] if _sect else [])}
# 例外两条：① 合一众分支变体（DE 档位与 manifest 的 Narmer ExtraJobs 不同款，属正常）
#           ② NokkoColonyEnterpriseSP = 我们硬编的**社区观测**档，本就不是金星点位任务
_known = {"NarmerVenusCullJobExterminate", "NokkoColonyEnterpriseSP"}
_missing = sorted(t for t in _tiers if t and t not in _pool and t not in _known)
check("★ 口径一致：金星档位 job 都在 manifest 任务池内（除 2 个已知例外）",
      not _missing, str(_missing))
check("★ 社区观测档不影响点位推算（它不在点位任务池里）",
      "NokkoColonyEnterpriseSP" not in _pool)

# 详情卡：地球出点位块、金星**不出**（同一套 `_tent_lines`，按 REGIONS 决定）
_tv, vlines = fmt.fmt_bounties(bundle["syndicateMissions"], "金星")
check("★ 金星详情卡**不再**出现点位块（2026-09-26 撤下）",
      not any(ln.startswith("　") and "｜" in ln and "级" not in ln for ln in vlines),
      str([ln for ln in vlines if ln.startswith("　")][:3]))
check("★ 地球详情卡仍有点位块（泛化能力未被撤金星带坏）",
      sum(1 for ln in earth if ln.startswith("　") and "｜" in ln) >= 3,
      str([ln for ln in earth if ln.startswith("　")][:4]))
check("金星注脚（「未定名」说明）随块一起收起，不残留",
      not any(ln.startswith("※") and "未定名" in ln for ln in vlines))
check("地球注脚仍是小帐篷口径（未被金星文案串台）",
      any(ln.startswith("※") and "小帐篷" in ln for ln in earth)
      and not any("小帐篷" in ln for ln in vlines))

# ---------------------------------------------------------------------------
if FAILED:
    print(f"\n{len(FAILED)} FAILED: {FAILED}")
    sys.exit(1)
print("\nALL PASS")
