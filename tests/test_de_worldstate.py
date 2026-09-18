# -*- coding: utf-8 -*-
"""DE 官方 worldstate 适配层离线测试（用真实抓包样例）。"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import de_worldstate as dw  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "de_worldstate.json"
raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
b = dw.parse_worldstate(raw, now_ms=1788964350000)

# 周期
assert b["cetusCycle"]["state"] in ("day", "night")
assert b["cetusCycle"]["timeLeft"]
# 地球昼夜与夜灵平野**共用同一周期**（browse.wf/live 也是同一行同一个 expiry）。
# ⚠️ 回归护栏：旧实现给地球单算了一个 8 小时循环，导致同卡上
#    「夜灵平野：白天 剩1h8m」和「地球：夜晚 剩3h39m」自相矛盾。
assert b["earthCycle"]["state"] == b["cetusCycle"]["state"]
assert b["earthCycle"]["expiry"] == b["cetusCycle"]["expiry"]
assert b["earthCycle"]["timeLeft"] == b["cetusCycle"]["timeLeft"]
assert b["vallisCycle"]["state"] in ("warm", "cold")
assert b["cambionCycle"]["state"] in ("fass", "vome")
# 魔胎之境与赏金（夜灵）周期同锚：白天 Fass / 夜晚 Vome
assert b["cambionCycle"]["expiry"] == b["cetusCycle"]["expiry"]
assert (b["cambionCycle"]["state"] == "fass") == bool(b["cetusCycle"]["isDay"])
assert b["duviriCycle"]["spiral"][0].isupper()  # 只存状态名（如 Sorrow）
assert b["duviriCycle"]["spiral"] in ("Sorrow", "Fear", "Joy", "Anger", "Envy")

# 扎里曼号派系：由 ZarimanSyndicate.Seed 的**最低位**决定（0 = Grineer / 1 = Corpus），
# 规则来自 browse.wf 的 oracle（{"FC_GRINEER","FC_CORPUS"}[(Seed & 1) + 1]）。
z = b["zarimanCycle"]
assert z["state"] in ("grineer", "corpus") and z["expiry"]
_zexp = dw._ms(next(s for s in raw["SyndicateMissions"]
                    if s["Tag"] == "ZarimanSyndicate")["Expiry"])
_cet = dw._ms(next(s for s in raw["SyndicateMissions"]
                   if s["Tag"] == "CetusSyndicate")["Expiry"])
assert z["expiry"] == dw._iso(_zexp), "扎里曼 expiry 应直接取 ZarimanSyndicate.Expiry"
# 注意：cetusCycle.expiry 是**期相**结束（白天→夜晚的切换点），不是周期结束，
# 两者差 50 分钟，不要拿来直接比。
assert _zexp == _cet, "实测 ZarimanSyndicate 与 CetusSyndicate 的 Expiry 逐毫秒相同"
# 本 fixture 抓包时 ZarimanSyndicate.Seed = 59621（奇数）-> 应为 Corpus。
# 这条是**真实抓包**校验，能挡住「用错 Seed（如 Cetus 的 59620 -> 会被判成 Grineer）」
# 与「改成周期序号推算（链一断就错）」两类回归。
assert z["state"] == "corpus", f"fixture Seed=59621 应为 corpus，实得 {z['state']}"
assert dw.zariman_cycle(0, 1789193984656, 1789193984656)["state"] == "grineer"
assert dw.zariman_cycle(1, 1789193984656, 1789193984656)["state"] == "corpus"
assert dw.zariman_cycle(34370, 1789193984656, 1789193984656)["state"] == "grineer"

# 裂隙：来自 ActiveMissions + VoidStorms
fiss = b["fissures"]
assert len(fiss) >= 20
storm = [f for f in fiss if f["isStorm"]]
hard = [f for f in fiss if f["isHard"]]
assert storm and hard
f0 = next(f for f in fiss if f["tierNum"] == 6)
assert f0["tier"] == "Omnia" and f0["missionType"]
assert all(f["node"] for f in fiss)

# 突击 / 执刑官
assert b["sortie"]["boss"] and len(b["sortie"]["variants"]) == 3
assert all(v["modifier"] for v in b["sortie"]["variants"])
assert "Amar" in b["archonHunt"]["boss"]

# 奸商（样例抓取时未抵达）
assert b["voidTrader"]["active"] is False
assert "Relay" in b["voidTrader"]["location"] or b["voidTrader"]["location"]
assert b["voidTrader"]["activation"]

# 每日特惠 / 电波 / 入侵 / 新闻 / 建造
assert b["dailyDeals"][0]["salePrice"] == 52
assert b["nightwave"]["season"] == 18
assert len(b["nightwave"]["activeChallenges"]) == 10
inv = b["invasions"][0]
assert inv["attacker"]["faction"] and 0 <= inv["completion"] <= 100
assert b["news"][0]["message"]
con = b["constructionProgress"]
assert isinstance(con.get("projects"), list) and isinstance(con.get("assaults"), list)
# 名称用 DE 官方简中（不是「巴洛巨舰 / 剃刀舰队」）
assert any(p["name"] == "巴罗尔巨人战舰" for p in con["projects"]), con["projects"]
assert all(0 < p["pct"] <= 100 for p in con["projects"])

# 赏金：四个赏金板、Ostrons 有任务
synd = {s["syndicate"]: s for s in b["syndicateMissions"]}
assert set(synd) >= {"Ostrons", "Solaris United", "Entrati"}
job = synd["Ostrons"]["jobs"][0]
assert len(job["enemyLevels"]) == 2 and job["standingStages"]

# 深层 / 时光科研
# v1.1 起 ``missionType`` 走 ``mission_types_zh.json``（DE 官方简中），
# 因此任务类型现在是中文；不再有 ``risk`` 字段，改读 ``difficulties[].tag``。
deep_types = {m["missionType"] for m in b["deepArchimedea"]["missions"]}
assert "中断" in deep_types and all("DT_" not in t for t in deep_types)
assert b["deepArchimedea"]["risks"][0]["name"]
tmp = b["temporalArchimedea"]["missions"][0]
assert tmp["missionType"] == "生存"
assert any(d["tag"] == "硬化" for d in tmp["difficulties"])

# 日历
cal = b["calendar"]
assert cal["days"][0]["date"].startswith("1999-")
assert any(e["name"] for d in cal["days"] for e in d["events"])

# DE 源不包含的外部数据
assert b["kuva"] is None and b["arbitration"] is None and b["steelPath"] is None

print("de_worldstate: 全部断言通过")
