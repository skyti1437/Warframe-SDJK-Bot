# -*- coding: utf-8 -*-
"""DE 官方 worldstate 适配层离线测试（用真实抓包样例）。"""

import json
import re
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
_zexp = dw._ms(
    next(s for s in raw["SyndicateMissions"] if s["Tag"] == "ZarimanSyndicate")["Expiry"]
)
_cet = dw._ms(next(s for s in raw["SyndicateMissions"] if s["Tag"] == "CetusSyndicate")["Expiry"])
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

# ---------------------------------------------------------------------------
# 九重天「剩 0s」修复（2026-09-27 审核 §三）
# ---------------------------------------------------------------------------
# DE 会保留下发**上一批已结束**的 VoidStorms（线上实测 12 条 = 两批、各 6 条、
# 存活 90 分钟、相隔 60 分钟，前一批已过期 8 分钟）。旧实现 ① 不过滤 ⇒ 显示
# 「剩 0s」；② 按**格式化字符串**排序 ⇒ "0s" 排在 "52m 18s" 之前（与用户截图
# 逐项吻合）。现在：过期即过滤、按真实剩余毫秒升序、负值一律「已结束」。
_NOW = 1788964350000
# ⚠️ Expiry 用**线上真实形状**（`$date.$numberLong` 毫秒串）—— `_ms()` 只吃
#   数字/该对象，喂 ISO 串会解析成 None（首版夹具就是这么错的）。
_STORMS = {
    "VoidStorms": [
        {
            "Node": "SolNode10",
            "Expiry": {"$date": {"$numberLong": str(_NOW - 8 * 60 * 1000)}},
            "ActiveMissionTier": "VoidT1",
        },  # 上一批：已过期
        {
            "Node": "SolNode11",
            "Expiry": {"$date": {"$numberLong": str(_NOW + (52 * 60 + 18) * 1000)}},
            "ActiveMissionTier": "VoidT1",
        },  # 本批：剩 52m 18s
        {
            "Node": "SolNode12",
            "Expiry": {"$date": {"$numberLong": str(_NOW + 5 * 60 * 1000)}},
            "ActiveMissionTier": "VoidT2",
        },  # 本批：剩 5m（应最前）
    ]
}
_st = dw._parse_void_storms(_STORMS, _NOW)
assert len(_st) == 2, f"过期条目应被过滤，实得 {len(_st)}"
assert all(x["timeLeft"] != "0s" for x in _st), [x["timeLeft"] for x in _st]
assert all(not x["timeLeft"].startswith("0s") for x in _st)
assert [x["_msLeft"] for x in _st] == sorted(x["_msLeft"] for x in _st), (
    f"应按真实剩余毫秒升序：{[x['timeLeft'] for x in _st]}"
)
assert _st[0]["timeLeft"] == "5m 0s", _st[0]["timeLeft"]
# `_fmt_left` 加固：负值 / 零不再伪装成「0s」
assert dw._fmt_left(-1) == "已结束" and dw._fmt_left(0) == "已结束"
assert dw._fmt_left(1000) == "1s" and dw._fmt_left(65 * 1000) == "1m 5s"
assert dw._fmt_left(3 * 3600 * 1000 + 5 * 60 * 1000) == "3h 5m"
print("九重天 0s 修复：断言通过")

# ---------------------------------------------------------------------------
# §五 汉化链路（2026-09-27）：构建期提取表 + 卡面清洗
# ---------------------------------------------------------------------------
# 根因：18MB 全量简中包（140,264 键）此前**没有任何代码读它**，运行期只有 36,088 键
# 的本地表 ⇒ 沉沦之地复杂化名 / 1999 升级名 / 源力石 / 赋能槽连接器全落英文。
# 修法：构建期提取卡面命名空间 → core/data/de/zh_ext.json（运行期由 language_text_zh 第二顺位查）。
_ext = dw._load("zh_ext.json") or {}
assert len(_ext) >= 7000, f"zh_ext 键数异常：{len(_ext)}"
_coh = [k for k in _ext if "cohchallenge" in k.lower()]
assert len(_coh) >= 68, f"zh_ext 覆盖 CoHChallenge* 应 ≥68，实得 {len(_coh)}"
# 抽 6 条与官方值逐字对齐（键/值均经双方独立复算）
_CASES = {
    "/Lotus/Language/CircleOfHell/CoHChallengeNarmerPhobia": "合一众执事",
    "/Lotus/Language/CircleOfHell/CoHChallengeEximusBlitzLeech": "突袭吸血卓越者军团",
    "/Lotus/Language/Narmer/ArchonCrystalOrangeNameNoIcon": "黄玉执刑官源力石",
    "/Lotus/Language/1999/StatusChancePerAmmoSpentName": "累积弹匣",
    "/Lotus/Language/Weapons/WeaponsecondaryArcaneUnlockerName": "次要武器赋能槽连接器",
    "/Lotus/Language/Items/GenericLensOstronBlueprint": "夜灵晶体",
}
for _k, _want in _CASES.items():
    _got = dw.language_text_zh(_k)
    assert _got == _want, f"{_k}: {_got!r} != {_want!r}"
# 卡面清洗：带 `<...>` 占位符的键取出来必须已剥净（否则卡面印出 <SHARD_ORANGE_SIMPLE>）
_cleaned = dw.language_text_zh("/Lotus/Language/Narmer/ArchonCrystalOrangeName")
assert _cleaned == "黄玉执刑官源力石", _cleaned
assert "<" not in _cleaned and "|" not in _cleaned, _cleaned
# 反向：真没有的键仍返回空串（不硬编、不猜）
assert dw.language_text_zh("/Lotus/Language/Items/OrokinCatalystBlueprint") == ""
print("§五 汉化链路：断言通过")

# ---------------------------------------------------------------------------
# §二 沉沦之地：内部代码 -> 官方简中（2026-09-27）
# ---------------------------------------------------------------------------
# 根因同 §五：DE 在 `/Lotus/Language/CircleOfHell/` 有 **268** 键（含 68 个
# `CoHChallenge*`），旧结论「DE 没给这些代码官方中文名」是错的 —— 落英文是因为
# 本地 36k 表没收录这个命名空间。现在主路径走 8 级官方键解析（de_worldstate），
# `_DESCENT_ZH` / `_DESCENT_GOAL_ZH` 两表**降级为兜底**。
from core import formatters as fmt  # noqa: E402

_RE_CJK = dw._CJK_RE

_coh_ns = {
    k: v
    for k, v in (dw._load("zh_ext.json") or {}).items()
    if k.startswith("/Lotus/Language/CircleOfHell/")
}
assert len(_coh_ns) == 268, f"CircleOfHell 应 268 键，实得 {len(_coh_ns)}"

# 「运行期只能传完整路径」：两张表里**无斜杠键均为 0** ⇒ 传 tail 永远查不到
# （`language_text_zh` 里那段 tail 回退是死代码，跨命名空间回退必须显式枚举全路径）
for _tb in ("languages_zh.json", "zh_ext.json"):
    _bare = [k for k in (dw._load(_tb) or {}) if isinstance(k, str) and "/" not in k]
    assert not _bare, f"{_tb} 出现无斜杠键 {len(_bare)} 条，tail 回退不再是死代码"
assert dw.language_text_zh("Wisp") == "", "裸尾段不应命中任何键"

# 夹具里的全部代码：50 个 Challenge + 21 个 DT_* 类型 = 71
_codes, _types = set(), set()
for _e in raw.get("Descents") or []:
    for _c in _e.get("Challenges") or []:
        _codes.add((_c.get("Challenge") or "").strip())
        _types.add((_c.get("Type") or "").strip())
assert len(_codes) == 50 and len(_types) == 21, (len(_codes), len(_types))

# 审核方订正表（13 行 / 16 个代码）：卡面必须与官方值**逐字**一致
_DESCENT_GOAL_EXPECT = {
    "HardShell": "冰封卓越者军团",
    "PowerHouse": "力量贪婪卓越者军团",
    "JumpSmash": "跺头者",
    "SpicyKnife": "拆弹",
    "BasicLootCreatures": "贪囤断肢劫掠",
    "JadeGuardian": "翠玉卓越者军团",
    "GiantRealm": "巨人症",
    "Sentients": "Tau 的复仇",
    "HordeWeakpoints": "弱点敌群",
    "UnseenFoes": "潜在威胁",
    "FieryTrail": "防火道",
    "HorseCombatOnly": "绝灵骥战斗",
    "BasicLoot": "掠夺",
    "BasicRace": "时间试炼",
    "BasicMimics": "打开容器",
}
_DESCENT_TYPE_EXPECT = {"DT_LOOT_CREATURES": "贪囤断肢劫掠"}
# 本期线上实测落英文的 8 条（审核方给的官方值）
_DESCENT_LIVE8 = {
    "ToxicFire": "毒焰卓越者军团",
    "NarmerPhobia": "合一众执事",
    "BlitzLeech": "突袭吸血卓越者军团",
    "RocketsOnly": "易受火箭炮攻击的敌人",
    "PoisonGas": "化学战",
    "FireChain": "烈焰枷锁",
    "Wisp": "玛丽的圣所",
}
for _c, _want in _DESCENT_GOAL_EXPECT.items():
    _got, _ok = fmt.descent_goal_label(_c)
    assert _ok and _got == _want, f"{_c}: 卡面 {_got!r} != 官方 {_want!r}"
for _c, _want in _DESCENT_LIVE8.items():
    _got, _ok = fmt.descent_goal_label(_c)
    assert _ok and _got == _want, f"{_c}: 卡面 {_got!r} != 官方 {_want!r}"
    assert _c in _codes, f"{_c} 不在夹具代码集里，期望值可能过期"
for _t, _want in _DESCENT_TYPE_EXPECT.items():
    _got, _ok = fmt.descent_type_label(_t)
    assert _ok and _got == _want, f"{_t}: 卡面 {_got!r} != 官方 {_want!r}"

# ★ 正向举证「该有中文却落英文 ⇒ 必红」：Wisp 不在手译表里，旧路径必落英文
assert "Wisp" not in fmt._DESCENT_GOAL_ZH
assert not _RE_CJK.search(fmt._pretty_code("Wisp"))
assert fmt.descent_goal_label("Wisp") == ("玛丽的圣所", True)

# 正例：凡官方有键者，卡面必为**官方值本身**（不是手译、不是英文）
# 值一律回**原始词表**核对：CoH 键走 zh_ext，其他命名空间（如 `Conquest/Condition_*`）
# 走 languages_zh —— 所以按完整路径逐表查，而不是只认 CircleOfHell 一张表。
_RAW_ZH = {**(dw._load("languages_zh.json") or {}), **(dw._load("zh_ext.json") or {})}


def _raw_zh(path: str) -> str:
    v = _RAW_ZH.get(path)
    return v if isinstance(v, str) else ""


for _c in sorted(_codes):
    _k, _zh = dw.descent_goal_official(_c)
    if not _k:
        continue
    assert _zh and _raw_zh(_k) == _zh, f"{_c}: {_k} 的值与官方表不符"
    if _k.startswith(dw._COH_NS):
        assert _coh_ns.get(_k) == _zh, f"{_c}: {_k} 不在 CircleOfHell 官方表里"
    _txt, _ok = fmt.descent_goal_label(_c)
    assert _ok and _txt == _zh, f"{_c}: 卡面 {_txt!r} != 官方 {_zh!r}"
for _t in sorted(_types):
    _k, _zh = dw.descent_type_official(_t)
    if not _k:
        continue
    assert _zh and _raw_zh(_k) == _zh, f"{_t}: {_k} 的值与官方表不符"
    if _k.startswith(dw._COH_NS):
        assert _coh_ns.get(_k) == _zh, f"{_t}: {_k} 不在 CircleOfHell 官方表里"
    _txt, _ok = fmt.descent_type_label(_t, dw.DESCENT_TYPES.get(_t, ""))
    assert _ok and _txt == _zh, f"{_t}: 卡面 {_txt!r} != 官方 {_zh!r}"

# 反例：官方无键者 —— 目标列必须落**英文原文**（出现 CJK 即判失败）
# ★ 2026-09-28 §三：目标族的无键清单已**清空**（VoidAberration 一族接到官方键
#   `Conquest/Condition_VoidAberration`），所以这里改成空集断言 + 逐条仍须落英文。
_nokey = {_c for _c in _codes if not dw.descent_goal_official(_c)[0]}
assert _nokey == set(dw.DESCENT_GOAL_NO_KEY) == set(), f"无键目标代码集变化：{_nokey}"
assert "VoidAberration" not in fmt._DESCENT_GOAL_ZH, "无键代码不得留在手译兜底表"

# 类型列：官方确认无键的（2026-09-28 后只剩 DT_UNIQUE）-> 英文原文
assert dw.DESCENT_TYPE_NO_KEY == frozenset({"DT_UNIQUE"})
for _t in sorted(dw.DESCENT_TYPE_NO_KEY):
    _txt, _ok = fmt.descent_type_label(_t, dw.DESCENT_TYPES.get(_t, ""))
    assert not _ok and not _RE_CJK.search(_txt), f"{_t}: 无键却给了 {_txt!r}"
# 本轮新接入官方键的 4 条（原先 2 条吐英文、2 条错槽）—— 逐条钉住
for _t, _want in (
    ("DT_SABOTAGE_DEFENSE", "防御"),
    ("DT_NETRACELLS", "消灭目标"),
    ("DT_MIMICS", "掠夺轮盘"),
    ("DT_INTERCEPTION", "移动拦截"),
):
    assert fmt.descent_type_label(_t, dw.DESCENT_TYPES[_t]) == (_want, True), _t
# 保留手译的两条：必须**真的**没有官方键（否则就是该删的手编）
for _t in fmt._DESCENT_TYPE_KEEP_CN:
    assert not dw.descent_type_official(_t)[0], f"{_t} 其实有官方键，手译应删"
assert fmt._DESCENT_ZH["DT_PROTOFRAME"] == "战甲祈运"

# 手译表一致性：**每条要么与官方一致、要么显式标为无键**（只作兜底，不并列）
for _c, _cn in fmt._DESCENT_GOAL_ZH.items():
    _k, _zh = dw.descent_goal_official(_c)
    assert _k and _zh == _cn, f"兜底表 {_c}: {_cn!r} != 官方 {_zh!r}"
for _t, _cn in fmt._DESCENT_ZH.items():
    _k, _zh = dw.descent_type_official(_t)
    if _k:
        assert _zh == _cn, f"兜底表 {_t}: {_cn!r} != 官方 {_zh!r}"
    else:
        assert _t in fmt._DESCENT_TYPE_KEEP_CN, f"{_t} 无官方键却未登记为保留手译"

# 卡面：本期 21 层逐行核对 + 注脚
_dt_title, _dt_lines = fmt.fmt_descendia(b["descendia"])
assert _dt_title == "沉沦之地 · 炼狱塔"
_rows = [_ln for _ln in _dt_lines if _ln.startswith("· 炼狱 [")]
assert len(_rows) == 21, f"应 21 行，实得 {len(_rows)}"
for _row, _ch in zip(_rows, b["descendia"]["challenges"]):
    _cells = _row.split("　")
    _mech = fmt._PROTOFRAME_FLOOR.get(_ch["code"]) if _ch["Type"] == "DT_PROTOFRAME" else None
    if _mech:
        # ★ 7/14/21 检查点层（2026-09-29 拍板）：主标 = 官方圣所名，右侧 = 机制
        _main, _right = _cells[1], _cells[2]
        _want, _ok = fmt.descent_goal_label(_ch["code"])
        assert _main == _want and _ok, (_ch["code"], _main, _want)
        assert _right == _mech, (_ch["code"], _right, _mech)
        assert "<" not in _right and "|" not in _right
        continue
    _goal = _row.rsplit("　", 1)[-1]
    _want, _ = fmt.descent_goal_label(_ch["code"])
    assert _goal == _want, (_ch["code"], _goal, _want)
    assert "<" not in _goal and "|" not in _goal and "\r" not in _goal
# ★ 弃用自编「战甲祈运」：卡面文本里不得再出现（与官方「祈运坛防御」撞词的混淆源）
_dt_txt = "\n".join(_dt_lines)
assert "战甲祈运" not in _dt_txt, "检查点层应显示官方圣所名，不再出现自编类型名"
assert "玛丽的圣所　祝福二选一" in _dt_txt and "里昂的圣所　祝福二选一·附代价" in _dt_txt, _dt_txt
_nokey_live = {
    _ch["code"]
    for _ch in b["descendia"]["challenges"]
    if not dw.descent_goal_official(_ch["code"])[0]
}
_nokey_live |= {
    _ch["Type"] for _ch in b["descendia"]["challenges"] if _ch["Type"] in dw.DESCENT_TYPE_NO_KEY
}
if _nokey_live:
    _foot = [ln for ln in _dt_lines if "官方无简中" in ln]
    assert _foot and all(c in _foot[0] for c in _nokey_live), _foot
else:
    assert not [ln for ln in _dt_lines if "官方无简中" in ln]
print("§二 沉沦之地汉化链路：断言通过")

# ---------------------------------------------------------------------------
# §三 1999 日历：奖励/覆写 -> 官方简中（2026-09-27）
# ---------------------------------------------------------------------------
# 本期线上 **17** 条路径（9 奖励 + 8 覆写，2026-09-27 抓 worldstate 实测）。
# 命名空间以**实测**为准（审核方给的命名空间列有两处需订正）：
#   · `AffinityBoosterThreeDayName` / `ResourceDropChanceBoosterThreeDayName`
#     在 **Items** 命名空间（不在 1999）
#   · `WeaponsecondaryArcaneUnlockerName` 在 **Weapons**（且键里 "secondary" 是小写 s）
#   · `ArchonCrystalOrangeNameNoIcon` 在 **Narmer**（优先 `…NameNoIcon`，无图标占位符）
_CAL_EXPECT = [
    ("/Lotus/StoreItems/Types/BoosterPacks/CalendarArtifactPack", "赋能助力", False),
    ("/Lotus/StoreItems/Types/BoosterPacks/CalendarMajorArtifactPack", "赋能助力：双重组合", False),
    (
        "/Lotus/StoreItems/Types/Gameplay/NarmerSorties/ArchonCrystalOrange",
        "黄玉执刑官源力石",
        False,
    ),
    (
        "/Lotus/StoreItems/Types/Items/MiscItems/WeaponSecondaryArcaneUnlocker",
        "次要武器赋能槽连接器",
        False,
    ),
    ("/Lotus/StoreItems/Types/Recipes/Components/OrokinReactorBlueprint", "奥罗金反应堆蓝图", True),
    (
        "/Lotus/StoreItems/Upgrades/Mods/FusionBundles/CircuitSilverSteelPathFusionBundle",
        "6000 内融核心",
        True,
    ),
    ("/Lotus/Types/StoreItems/Boosters/AffinityBooster3DayStoreItem", "3 天经验值加成", False),
    (
        "/Lotus/Types/StoreItems/Boosters/ResourceDropChance3DayStoreItem",
        "3 天资源掉落几率加成",
        False,
    ),
    ("/Lotus/Types/StoreItems/Packages/Calendar/CalendarVosforPack", "荧尘储藏箱", False),
    ("/Lotus/Upgrades/Calendar/StatusChancePerAmmoSpent", "累积弹匣", False),
    ("/Lotus/Upgrades/Calendar/AttackAndMovementSpeedOnCritMelee", "快刀斩乱麻", False),
    ("/Lotus/Upgrades/Calendar/EnergyRestoration", "特浓咖啡", False),
    ("/Lotus/Upgrades/Calendar/FinisherChancePerComboMultiplier", "杀意连段", False),
    ("/Lotus/Upgrades/Calendar/AbilityStrength", "力量飙升", False),
    ("/Lotus/Upgrades/Calendar/ElectricStatusDamageAndChance", "瓶装闪电", False),
    ("/Lotus/Upgrades/Calendar/MagazineCapacity", "重型弹匣", False),
    ("/Lotus/Upgrades/Calendar/MeleeCritChance", "熟能生巧", False),
]
assert len(_CAL_EXPECT) == 17, len(_CAL_EXPECT)
_UPG_PFX = "/Lotus/Upgrades/Calendar/"
for _spec, _want, _synth in _CAL_EXPECT:
    if _spec.startswith(_UPG_PFX):
        _got, _is_synth = dw._calendar_upgrade_zh(_spec), False
    else:
        _got, _is_synth = dw._calendar_reward_name(_spec)
    assert _got == _want, f"{_spec}: {_got!r} != {_want!r}"
    assert _is_synth == _synth, f"{_spec}: 合成标记 {_is_synth} != {_synth}"
    assert _RE_CJK.search(_got), f"{_spec}: 产出不含中文"
    for _bad in ("<", "|COLOR_", "\r", "\n"):
        assert _bad not in _got, f"{_spec}: 产出含未清洗标记 {_bad!r}"

# 覆写兜底表：每条都必须与官方查表结果**逐字一致**（15 条订正后的成果）
for _ref, _cn in dw.CAL_UPGRADE_CN.items():
    assert _ref.startswith(_UPG_PFX), _ref
    _zh = dw._calendar_upgrade_zh(_ref)
    assert _zh == _cn, f"兜底表 {_ref}: {_cn!r} != 官方 {_zh!r}"
# 补漏的 4 条必须在表内（旧手编表没有它们）
for _tail in (
    "RadialJavelinOnHeavy",
    "ElectricalDamageOnBulletJump",
    "MeleeSlideFowardMomentumOnEnemyHit",
    "StatusChancePerAmmoSpent",
):
    _ref = _UPG_PFX + _tail
    assert _ref in dw.CAL_UPGRADE_CN and dw._calendar_upgrade_zh(_ref), _tail
# 反向：官方确无键的覆写不得留在兜底表里，且查表返回空（不硬编）
assert dw.CAL_UPGRADE_NO_KEY == frozenset({_UPG_PFX + "GuidingMissilesChance"})
for _ref in dw.CAL_UPGRADE_NO_KEY:
    assert dw._calendar_upgrade_zh(_ref) == "", _ref
    assert _ref not in dw.CAL_UPGRADE_CN, _ref

# 夹具整季：产出名一律无未清洗标记；官方中文不得再被 `_cal_name` 重排
for _d in b["calendar"]["days"]:
    for _e in _d["events"]:
        _nm = _e.get("name") or ""
        assert _nm, (_d["day"], _e)
        for _bad in ("<", "|COLOR_", "\r", "\n"):
            assert _bad not in _nm, _nm
        _shown = fmt._cal_name(_nm)
        if _RE_CJK.search(_nm):
            assert _shown == _nm, f"官方中文被重排：{_nm!r} -> {_shown!r}"
# 反例：英文兜底值仍走老重排（否则「赤毒 ×6000」这类会退化）
assert fmt._cal_name("6,000 Endo") == "内融核心 ×6,000"
assert fmt._cal_name("2000 x Kuva") == "赤毒 ×2000"
# 正例：含前导数字的官方中文**不得**被拆成「… ×N」
assert fmt._cal_name("3 天经验值加成") == "3 天经验值加成"
assert fmt._cal_name("6000 内融核心") == "6000 内融核心"
print("§三 1999 日历汉化链路：断言通过")

# ---------------------------------------------------------------------------
# §四 入侵卡「蓝图」类奖励：官方只有基名 ⇒ 基名 + 「蓝图」（合成，卡面注脚）
# ---------------------------------------------------------------------------
_CAT = "/Lotus/Types/Recipes/Components/OrokinCatalystBlueprint"
assert dw.language_text_zh("/Lotus/Language/Items/OrokinCatalystBlueprint") == ""
assert dw.language_text_zh("/Lotus/Language/Items/OrokinCatalyst") == "奥罗金催化剂"
assert dw.item_name_opt_ex(_CAT) == ("奥罗金催化剂蓝图", True)
assert dw.item_name_opt_ex("/Lotus/StoreItems/Types/Recipes/Components/OrokinReactorBlueprint") == (
    "奥罗金反应堆蓝图",
    True,
)
# 反向：基名拿不到官方中文时**不许**硬拼「蓝图」（英文基名 + 蓝图 = 编）
assert dw.item_name_opt_ex("/Lotus/Types/Recipes/Components/NopeBlueprint") == (None, False)
assert dw.item_name_opt_ex("/Lotus/Types/Items/Research/ChemComponent") == ("爆燃喷射器", False)
# 卡面：合成译名必须自曝（注脚），且入侵卡本身仍能出
_inv_title, _inv_lines = fmt.fmt_invasions(b["invasions"])
assert _inv_title == "入侵"
print("§四 入侵卡蓝图合成：断言通过")

# ---------------------------------------------------------------------------
# §一 `[PH]` 占位符护栏（2026-09-28）
# ---------------------------------------------------------------------------
# DE 的开发中占位符 `[PH] …` **不是可用文案**。实测：官方全量包 140,264 键里
# **1508** 条值以 `[PH]` 开头（严格大写；大小写不敏感 **1509** —— 多出的那条写作
# `[ph]`，所以判据必须大小写不敏感）。其中落在**运行期卡面表**的有 zh_ext 与
# languages_zh 两张（下面按表实测，不写死数字）。
# ⚠️ 全量包在 `core/data/_cache/`（**未跟踪 / 只作构建期缓存**）⇒ 主断言必须只靠
#    仓内两张运行期表；全量包若在，再跑一遍加强版。
_rt_ph: dict[str, str] = {}
for _tbn in ("languages_zh.json", "zh_ext.json"):
    for _k, _v in (dw._load(_tbn) or {}).items():
        if isinstance(_v, str) and dw._is_placeholder(_v):
            _rt_ph[_k] = _v
# 实测基线（2026-09-28）：languages_zh 16 条 + zh_ext 87 条，交集 4 ⇒ 并集 **99**。
# 用下限而不是等号，避免将来官方刷新数据时把测试判成红。
assert len(_rt_ph) >= 90, f"运行期两表里的 [PH] 值应 ≥90 条，实得 {len(_rt_ph)}"
for _k in sorted(_rt_ph):
    assert dw.language_text_zh(_k) == "", f"{_k} 仍返回占位符：{dw.language_text_zh(_k)!r}"
print(f"§一 [PH] 运行期两表参数化：{len(_rt_ph)} 条键全部返回空串")

# 审核方点名的 3 个键（注意真实全路径带 `Name` 后缀）+ 2 个我另找到的卡面键。
# ⚠️ 其中 `[ph]` 小写那条**只存在于 18MB 全量包**（`Upgrades` 命名空间不在运行期两表里）
#    ⇒ 原始值核对只在「该表里确实有这条」时进行；查询返回空串是**恒定要求**。
_RAW_TBL = {**(dw._load("languages_zh.json") or {}), **(dw._load("zh_ext.json") or {})}
for _k, _ph in (
    ("/Lotus/Language/1999/Wf99SurvivalInfestedCloudName", "[PH] TECHROT CLOUD"),
    (
        "/Lotus/Language/1999/LASxEntertechmentLootCrateUltraRareName",
        "[PH] Techrot Infested Entertechment Unit",
    ),
    (
        "/Lotus/Language/1999/LASxMilitaryLootCrateRareName",
        "[PH] Techrot Infested Scaldra Supplies",
    ),
    # 大小写变体（`[ph]` 小写）—— 判据必须大小写不敏感，否则漏这一条
    (
        "/Lotus/Language/Upgrades/AntiqueHeatStatusProcOnUltimateKillName",
        "[ph] Heat Status Effect On Tauron Strike Kill",
    ),
    # CircleOfHell 命名空间里也有一条（会直接上沉沦之地卡）
    ("/Lotus/Language/CircleOfHell/CohLavaSurviveTimer", "[PH] Avoid the Lava"),
):
    if _k in _RAW_TBL:
        assert _RAW_TBL.get(_k) == _ph, f"{_k} 的原始值应为 {_ph!r}，实得 {_RAW_TBL.get(_k)!r}"
    assert dw.language_text_zh(_k) == "", _k
    assert dw._is_placeholder(_ph)
print("§一 [PH] 点名键：5/5 查询返回空串（4 条在运行期表内，原始值已逐条核对）")

# 反例：值**中间/尾部**含 PH 的合法文案不受影响（大小写敏感地只认「开头」）
_RT_CASES = {
    "/Lotus/Language/Challenges/Challenge_KillPhorid_Description": "击败 Phorid 并赶到撤离点",
    "/Lotus/Language/Bundles/ZephyrDeluxeSkinBundleName": "Zephyr 鹞式组合包",
    "/Lotus/Language/Cosmetics/TenguDeluxeNobleAnims": "Zephyr 鹞式尊贵站姿",
}
for _k, _want in _RT_CASES.items():
    assert dw.language_text_zh(_k) == _want, f"{_k}: {dw.language_text_zh(_k)!r} != {_want!r}"
assert not dw._is_placeholder("击中 Phorid 后撤离")
assert not dw._is_placeholder("[PHX] 不是占位符前缀")  # `[PH]` 必须是完整方括号段
print("§一 反例：值中/尾部含 PH 的合法文案照常返回（3/3）")

# 卡面索引侧（收口②）：占位符不进索引 ⇒ 跨命名空间/后缀回退会继续找下一个候选
_ph_tail = "Wf99SurvivalInfestedCloudName"
assert dw._card_lang_by_tail(_ph_tail) == ("", ""), dw._card_lang_by_tail(_ph_tail)
assert dw._card_lang_by_tail("ArchonCrystalOrange")[1] == "黄玉执刑官源力石"  # 正常键不受影响

# 加强版（可选）：全量包里全部 `[PH]` 键 —— 存在才跑（该目录未跟踪）
_FULL = dw.DATA_DIR.parent / "_cache" / "languages_zh_full.json"
if _FULL.exists():
    _full = json.loads(_FULL.read_text(encoding="utf-8")) or {}
    _ph_all = {
        k: v
        for k, v in _full.items()
        if isinstance(k, str) and dw._is_placeholder(v.get("value") if isinstance(v, dict) else v)
    }
    for _k in _ph_all:
        assert dw.language_text_zh(_k) == "", f"{_k} 仍返回占位符"
    print(
        f"§一 占位符加强版（全量包在）：{len(_ph_all)} 条「不可用」值全部返回空串"
        f"（其中严格大写 [PH] 前缀 {sum(1 for v in _ph_all.values() if str(v).strip().startswith('[PH]'))} 条，"
        f"余为变量槽 |val| 一族 —— 本批 §二 新增判据）"
    )
else:
    print("§一 [PH] 加强版：跳过（core/data/_cache/ 未跟踪，本机无全量包）")

# ---------------------------------------------------------------------------
# §二 沉沦之地任务类型：DESCENT_TYPES 英文名订正 + 官方键接入（2026-09-28）
# ---------------------------------------------------------------------------
# ★ 端到端验收（最强的一条）：本周 21 层 × 逐层期望值（来源 = wiki `The Descendia`
#   2026-09-23 本周表 + 官方简中全量包；见 `审核结论-Descendia资源与逐层对拍-20260928.md` §三/§四）。
#   任何一层回退成英文或错槽 ⇒ 本断言必须变红。
_DESCENT_WEEK21 = [
    ("DT_SHRINE_DEFENSE", "FreezeInShoot", "祈运坛防御", "冰封之光卓越者军团"),
    ("DT_SABOTAGE_HIVE", "RangedArcadiaOnly", "清巢", "泡泡枪"),
    ("DT_INFESTED_SALVAGE", "ToxicFire", "净化", "毒焰卓越者军团"),
    ("DT_SABOTAGE_DEFENSE", "NarmerPhobia", "防御", "合一众执事"),
    ("DT_CAPTURE", "BlitzLeech", "传承种捕获", "突袭吸血卓越者军团"),
    ("DT_DEFENSE", "RocketsOnly", "防御", "易受火箭炮攻击的敌人"),
    ("DT_PROTOFRAME", "Wisp", "战甲祈运", "玛丽的圣所"),
    ("DT_PRESURE_GAUGE", "HardShell", "压力锅", "冰封卓越者军团"),
    ("DT_NETRACELLS", "PoisonGas", "消灭目标", "化学战"),
    ("DT_BREAK_TARGETS", "NC_VoidAberration", "摧毁全息球", "吸血界影"),
    ("DT_MIMICS", "BasicMimics", "掠夺轮盘", "打开容器"),
    ("DT_INFESTED_SALVAGE", "FireChain", "净化", "烈焰枷锁"),
    ("DT_INTERCEPTION", "FireAndIce", "移动拦截", "冰火卓越者军团"),
    ("DT_PROTOFRAME", "Harrow", "战甲祈运", "里昂的圣所"),
    ("DT_SHRINE_DEFENSE", "JumpSmash", "祈运坛防御", "跺头者"),
    ("DT_DEFENSE", "VeryToxic", "防御", "毒蛭吸血卓越者军团"),
    ("DT_RACE", "BasicRace", "时间试炼", "时间试炼"),
    ("DT_LOOT_CREATURES", "BasicLootCreatures", "贪囤断肢劫掠", "贪囤断肢劫掠"),
    ("DT_ALCHEMY", "JadeGuardian", "元素转换", "翠玉卓越者军团"),
    ("DT_COLLECTION", "NC_SecuritySpin", "收集", "激光炼狱"),
    ("DT_PROTOFRAME", "Devil", "战甲祈运", "罗瑟的遗忘"),
]
assert len(_DESCENT_WEEK21) == 21
for _i, (_tc, _gc, _want_t, _want_g) in enumerate(_DESCENT_WEEK21, 1):
    _got_t, _ok_t = fmt.descent_type_label(_tc, dw.DESCENT_TYPES.get(_tc, ""))
    _got_g, _ok_g = fmt.descent_goal_label(_gc)
    assert _ok_t and _got_t == _want_t, f"第{_i}层类型：{_got_t!r} != {_want_t!r}（{_tc}）"
    assert _ok_g and _got_g == _want_g, f"第{_i}层目标：{_got_g!r} != {_want_g!r}（{_gc}）"
print("§二 端到端：本周 21 层 × 类型+目标 = 42 项逐层等于期望值")

# 全部 DESCENT_TYPES（订正后 24 条）：产出**不得是裸 `DT_` 代码**
assert len(dw.DESCENT_TYPES) == 24, len(dw.DESCENT_TYPES)
for _c, _eng in dw.DESCENT_TYPES.items():
    _txt, _ = fmt.descent_type_label(_c, _eng)
    assert _txt and not _txt.startswith("DT_"), f"{_c} 产出裸代码或空：{_txt!r}"
# `DESCENT_TYPE_NO_KEY` 命中数 4 → ≤1（只允许 DT_UNIQUE）
assert dw.DESCENT_TYPE_NO_KEY == frozenset({"DT_UNIQUE"}), dw.DESCENT_TYPE_NO_KEY
assert len(dw.DESCENT_TYPE_NO_KEY) <= 1
# 官方键命中数：24 条里 **20** 条走 CircleOfHell 官方键，其余 4 条的来源各自钉住：
#   DT_BOSS → 官方任务类型表（刺杀）｜DT_PROTOFRAME / DT_DEFENSE_PROTECT → 手译保留（无键）
#   ｜DT_UNIQUE → 英文（无键）
_zofficial = [c for c in dw.DESCENT_TYPES if dw.descent_type_official(c)[0]]
assert len(_zofficial) == 20, len(_zofficial)
assert fmt.descent_type_label("DT_BOSS", "Assassination") == ("刺杀", True)
for _c in ("DT_PROTOFRAME", "DT_DEFENSE_PROTECT"):
    assert _c in fmt._DESCENT_TYPE_KEEP_CN
    assert fmt.descent_type_label(_c, dw.DESCENT_TYPES[_c]) == (fmt._DESCENT_ZH[_c], True), _c
assert len(_zofficial) + 3 + 1 == len(dw.DESCENT_TYPES)
print(
    f"§二 DESCENT_TYPES：24 条无一裸代码；官方键命中 {len(_zofficial)}"
    f" + 官方任务表 1(刺杀) + 手译保留 2 + 英文 1(DT_UNIQUE)"
)

# 反例：专防本轮两个错槽 + 一个英文残留
assert fmt.descent_type_label("DT_MIMICS", dw.DESCENT_TYPES["DT_MIMICS"])[0] != "歼灭"
assert (
    fmt.descent_type_label("DT_SABOTAGE_DEFENSE", dw.DESCENT_TYPES["DT_SABOTAGE_DEFENSE"])[0]
    != "Sabotage"
)
assert fmt.descent_type_label("DT_INTERCEPTION", dw.DESCENT_TYPES["DT_INTERCEPTION"])[0] != "拦截"
assert fmt.descent_type_label("DT_NETRACELLS", "Targeted Elimination")[0] != "Recovery"
# DT_BOSS 走**官方任务类型表**（不是手编 MISSION_CN）：assassination = 刺杀
assert dw.mission_type_zh_by_name("Assassination") == "刺杀"
assert fmt.descent_type_label("DT_BOSS", "Assassination") == ("刺杀", True)
# DT_UNIQUE 官方无键 ⇒ 保持英文 + 注脚（唯一允许项）
assert fmt.descent_type_label("DT_UNIQUE", "Assassination") == ("Assassination", False)
print("§二 反例：歼灭/ Sabotage / 拦截 / Recovery 四种错槽均已被防住")

# 两表同步（§2.3.4）：手译表不得有 DESCENT_TYPES 里没有的代码（本轮不同步的根因）；
# 反方向：不在手译表里的代码必须**能自解析**，唯一例外 DT_UNIQUE（官方无键留英文）。
assert set(fmt._DESCENT_ZH) <= set(dw.DESCENT_TYPES), set(fmt._DESCENT_ZH) - set(dw.DESCENT_TYPES)
for _c in set(dw.DESCENT_TYPES) - set(fmt._DESCENT_ZH):
    assert _c == "DT_UNIQUE" or fmt.descent_type_label(_c, dw.DESCENT_TYPES[_c])[1], (
        f"{_c} 既不在手译表、又不能自解析"
    )
print("§二 两表同步：手译表 ⊆ 代码表；差集内除 DT_UNIQUE 外全部可自解析")

# ---------------------------------------------------------------------------
# §三 `VoidAberration` 采纳官方名（吸血界影，带来源注脚）
# ---------------------------------------------------------------------------
_VA = "/Lotus/Language/Conquest/Condition_VoidAberration"
assert dw.language_text_zh(_VA) == "吸血界影"
for _c in ("VoidAberration", "NC_VoidAberration"):
    _k, _zh = dw.descent_goal_official(_c)
    assert _k == _VA and _zh == "吸血界影", (_c, _k, _zh)
    assert fmt.descent_goal_label(_c) == ("吸血界影", True)
# 剥 `NC_` 后同值
assert fmt.descent_goal_label("VoidAberration") == fmt.descent_goal_label("NC_VoidAberration")
# 反例：目标族无键清单已清空（两个代码都接到官方键了）
assert dw.DESCENT_GOAL_NO_KEY == frozenset(), sorted(dw.DESCENT_GOAL_NO_KEY)
assert "VoidAberration" not in fmt._DESCENT_GOAL_ZH
# 注脚：不再把 NC_VoidAberration 列为「无简中」，改为**标明来源存疑**
_st3_title, _st3 = fmt.fmt_descendia(b["descendia"])
_nokey_foot = [ln for ln in _st3 if "官方无简中" in ln]
assert all("VoidAberration" not in ln for ln in _nokey_foot), _nokey_foot
print("§三 VoidAberration：两代码同值 吸血界影；GOAL_NO_KEY 清空；不再进无简中注脚")

# ---------------------------------------------------------------------------
# §四 1999 覆写池 38 条参数化夹具 + 手编表一致性（2026-09-28）
# ---------------------------------------------------------------------------
# 数据源：`1999取证-20260928/1999覆写池-38条-code与官方简中.md`
# （英文名来自 wiki 1999_Calendar，官方简中来自 DE `/Lotus/Language/1999/<code>Name`）。
_OVERRIDE_POOL38 = {
    "ElectricStatusDamageAndChance": "瓶装闪电",
    "EnergyOrbToAbilityRange": "极限拓展",
    "HealingEffects": "应急特效药",
    "EnergyRestoration": "特浓咖啡",
    "CompanionDamage": "好兄弟",
    "OvershieldCap": "硬质化",
    "MagazineCapacity": "重型弹匣",
    "MeleeAttackSpeed": "毫不留情",
    "AbilityStrength": "力量飙升",
    "MeleeCritChance": "熟能生巧",
    "RadiationProcOnTakeDamage": "精神反击",
    "PunchToPrimary": "打孔纸带",
    "Armor": "硬化装甲",
    "GasChanceToPrimaryAndSecondary": "毒气弹头",
    "ExplodingHealthOrbs": "劣质医疗",
    "FinisherChancePerComboMultiplier": "杀意连段",
    "EnergyWavesOnCombo": "波动连段",
    "CloneActiveCompanionForEnergySpent": "复制粘贴",
    "StatusChancePerAmmoSpent": "累积弹匣",
    "ElectricalDamageOnBulletJump": "电能飞跃",
    "RefundBulletOnStatusProc": "随意开火",
    "SpeedBuffsWhenAirborne": "航班常客",
    "CompanionsRadiationChance": "辐射支援",
    "MagnetStatusPull": "吸引力",
    "BlastEveryXShots": "爆炸派对",
    "RadialJavelinOnHeavy": "重型标枪",
    "AttackAndMovementSpeedOnCritMelee": "快刀斩乱麻",
    "GenerateOmniOrbsOnWeakKill": "强制输血",
    "MagnitizeWithinRangeEveryXCasts": "磁场威胁",
    "CompanionsBuffNearbyPlayer": "人多势众",
    "PowerStrengthAndEfficiencyPerEnergySpent": "能量过载",
    "EnergyOrbsGrantShield": "充能护盾",
    "ReviveEnemyAsSpectreOnKill": "救赎契机",
    "SharedFreeAbilityEveryXCasts": "有福同享",
    "MeleeSlideFowardMomentumOnEnemyHit": "斩筋断骨",
    "ElectricDamagePerDistance": "电势积累",
    "OrbsDuplicateOnPickup": "靶向治疗",
    "RicochetChance": "射击戏法",
}
assert len(_OVERRIDE_POOL38) == 38, len(_OVERRIDE_POOL38)
_unresolved = []
for _code, _want in _OVERRIDE_POOL38.items():
    _got = dw._calendar_upgrade_zh(f"{_UPG_PFX}{_code}")
    if _got != _want:
        _unresolved.append((_code, _got, _want))
assert not _unresolved, f"覆写池未解出/不符：{_unresolved}"
print("§四 覆写池 38/38 与官方查表逐条相等（0 未解出）")

# 手编表一致性：存在即必须等于官方查表值（不一致即红）——上一批已有，这里补逆向覆盖
assert len(dw.CAL_UPGRADE_CN) == 31, len(dw.CAL_UPGRADE_CN)
_in_pool = [c for c in dw.CAL_UPGRADE_CN if c.replace(_UPG_PFX, "") in _OVERRIDE_POOL38]
# 手编表 31 条里 31 条都属现行池（GuidingMissilesChance 已移出、4 条补漏也在池内）
assert len(_in_pool) == 31, len(_in_pool)
print("§四 手编表 31 条全部落在现行 38 条池内，且与官方查表逐字一致")

# ---------------------------------------------------------------------------
# §五 `GuidingMissilesChance` 已废弃 / 非现行池
# ---------------------------------------------------------------------------
_GMC = f"{_UPG_PFX}GuidingMissilesChance"
assert _GMC not in _OVERRIDE_POOL38, "它不该在现行 38 条池里"
assert dw._calendar_upgrade_zh(_GMC) == ""  # 官方包 0 键
assert _GMC not in dw.CAL_UPGRADE_CN  # 不在手编表（不硬编）
assert _GMC in dw.CAL_UPGRADE_NO_KEY
# 卡面行为确认：若 worldstate 又下发它 ⇒ 英文可读名 + 计入 noKey 注脚
_ev = dw._parse_calendar(
    {"Days": [{"day": 1, "events": [{"type": "CET_UPGRADE", "upgrade": _GMC}]}]}, 0
)
_nm = _ev["days"][0]["events"][0]["name"]
assert _RE_CJK.search(_nm) is None, _nm
assert _nm == "Guiding Missiles Chance", _nm
assert _GMC.replace(_UPG_PFX, "") in _ev["noKey"], _ev["noKey"]
print(f"§五 GuidingMissilesChance：官方 0 键 / 不在现行池 ⇒ 卡面 {_nm!r} + 注脚（可接受）")

# ---------------------------------------------------------------------------
# §六 `MISSION_CN` 与官方任务类型表零冲突
# ---------------------------------------------------------------------------
from core.parser import MISSION_CN as _MCN  # noqa: E402

_mt_en = json.loads((dw.DATA_DIR / "missionTypes.json").read_text(encoding="utf-8"))
_mt_zh = json.loads((dw.DATA_DIR / "mission_types_zh.json").read_text(encoding="utf-8"))
_by_en = {}
for _code, _rec in _mt_en.items():
    _nmz = _rec.get("value") if isinstance(_rec, dict) else _rec
    if isinstance(_nmz, str) and _code in _mt_zh:
        _by_en.setdefault(_nmz.strip().lower(), _mt_zh[_code])
_matched = {k: v for k, v in _MCN.items() if k.strip().lower() in _by_en}
_conflicts = [
    (k, v, _by_en[k.strip().lower()]) for k, v in _matched.items() if v != _by_en[k.strip().lower()]
]
assert not _conflicts, f"MISSION_CN 与官方表仍有冲突：{_conflicts}"
assert len(_matched) == 25, f"能与官方表对上的应有 25 条，实得 {len(_matched)}"
assert _MCN["infested salvage"] == "INFESTED 资源回收"
# 反查：旧叫法仍可筛（`FissureFilter` 用 _CN_TO_MISSION）
from core.parser import _CN_TO_MISSION  # noqa: E402

assert _CN_TO_MISSION["INFESTED 资源回收"] == "infested salvage"
assert _CN_TO_MISSION["感染打捞"] == "infested salvage"
print(f"§六 MISSION_CN：与官方任务类型表对齐 {len(_matched)} 条 / 冲突 0（原 1：infested salvage）")

# ---------------------------------------------------------------------------
# §二 `|val|` / `<>` 模板标记清洗（2026-09-28）
# ---------------------------------------------------------------------------
# 缺陷（审核线上实测）：深层科研卡出现一行「嗜睡护盾：护盾充能延迟增加 |val|%。」
# 来源 `/Lotus/Language/Conquest/PersonalMod_ShieldDelay_Desc` —— 官方原文自带**变量槽** `|val|`。
# 修法：把两类语义**集中在同一处定义**（`de_worldstate` 顶部「占位符/模板标记单一真源」块）：
#   ① 不可用（`_is_placeholder`：`[PH]` 前缀 + **小写变量槽**）⇒ 视同无键走回落；
#   ② 可清洗（`_clean_lang_text`：`<>` 图标占位符 + `|COLOR_*|` 富文本）⇒ 剥标记、**留正文**。
_PH_K = "/Lotus/Language/Conquest/PersonalMod_ShieldDelay_Desc"
assert dw._load("languages_zh.json").get(_PH_K) == "护盾充能延迟增加 |val|%。", "原始值应自带 |val|"
assert dw.language_text_zh(_PH_K) == "", "含 |val| 的值必须视为无键（不许上卡半成品）"
assert dw._card_lang_by_tail("PersonalMod_ShieldDelay_Desc") == ("", "")

# 判据正例（两类「不可用」）
for _s in (
    "[PH] TECHROT CLOUD",
    "[ph] Heat Status Effect",
    "护盾充能延迟增加 |val|%。",
    "增加 |duplicate_num| 层额外异常状态层数。",
    "|val|% 电击伤害",
):
    assert dw._is_placeholder(_s), _s
assert not dw._is_placeholder("")
# 判据反例（**不得误伤**）—— 大写槽 / 富文本 / 数字段 / 含 PH 的普通词
for _s in (
    "|COLOR_POS|100%|COLOR_END|",
    "|COUNT| 名敌人",
    "|STAT1|% 技能强度",
    "|1| 项操作",
    "奖励：<CREDITS>105,000",
    "（英文：PHAEDRA）",
    "PHAGE 是一种感染体",
    "PHANTASMA 幻影",
    "击中 Phorid 后撤离",
    "[PHX] 不是占位符前缀",
):
    assert not dw._is_placeholder(_s), _s
print("§二 判据：5 条正例 + 10 条反例（COLOR/COUNT/STAT1/数字段/PHAGE/PHAEDRA）全部符合预期")

# ★ 反例护栏 2 条（本批指令点名）：**含标记但正文必须保留**
assert (
    dw.language_text_zh("/Lotus/Language/CircleOfHell/CoHHarrowEximusDamageBoost_Desc")
    == "卓越者单位不再掉落球体，但是受到的伤害增加 100%。"
)
assert dw.language_text_zh("/Lotus/Language/Narmer/ArchonCrystalOrangeName") == "黄玉执刑官源力石"
assert (
    dw.language_text_zh("/Lotus/Language/Narmer/ArchonCrystalOrangeNameNoIcon")
    == "黄玉执刑官源力石"
)
print("§二 反例护栏：|COLOR_POS|100%|COLOR_END| 剥标记后保留「…100%。」；<SHARD…> 剥后保留全名")

# ★ 最小夹具（§二.3）：走**真实泄露链路**（_conquest_text → fmt_archimedea），
#   断言产出**不含** `|val|`（回落成「纯名称」或英文名均可，但不许是带标记的半成品）。
_nm, _desc = dw._conquest_text("PersonalMod_ShieldDelay", "CT_LAB")
assert _nm, "名称应仍可取到（不能因为描述坏掉把整条丢掉）"
assert _desc == "", f"含 |val| 的描述应落空，实得 {_desc!r}"
# `fmt_archimedea` 在 missions 为空时会提前返回 ⇒ 夹具必须带一个最小任务，
# 否则变量段根本不渲染（第一版夹具就是踩了这个，断言「名称仍在」直接红）。
_arch = {
    "expiry": "",
    "risks": [],
    "missions": [
        {
            "missionType": "防御",
            "faction": "",
            "difficulties": [{"tag": "普通", "deviation": "密闭装甲", "risks": []}],
        }
    ],
    "variables": [{"name": _nm, "description": _desc}],
}
_arch_title, _arch_lines = fmt.fmt_archimedea(_arch, "深层科研")
_joined = "\n".join(_arch_lines)
for _bad in ("|val|", "<DT_", "[PH]"):
    assert _bad not in _joined, f"深层科研卡残留 {_bad}：{_joined!r}"
assert _nm in _joined, f"个人减益名称应仍在卡上：{_joined!r}"
print(f"§二 最小夹具：PersonalMod_ShieldDelay → 产出 {_arch_lines[-2:]!r}（无 |val|）")

# ★ 全局残留断言（§二.4，三类模式；上一批的 [PH] 版在这里扩成三类）
_RES_RE = (re.compile(r"\|[A-Za-z_]{1,24}\|"), re.compile(r"<[A-Z_]{3,40}>"), re.compile(r"\[PH\]"))
_CARD_NS = (
    "/Lotus/Language/1999/",
    "/Lotus/Language/Items/",
    "/Lotus/Language/Weapons/",
    "/Lotus/Language/Narmer/",
    "/Lotus/Language/CircleOfHell/",
    "/Lotus/Language/Conquest/",
)


def _residue(text: str) -> str:
    for _r in _RES_RE:
        _m = _r.search(text or "")
        if _m:
            return _m.group(0)
    return ""


# ① **数据面**（完整）：卡面命名空间里每个键的查询结果都不得带残留
_n_res = 0
for _tbn in ("languages_zh.json", "zh_ext.json"):
    for _k in dw._load(_tbn) or {}:
        if not (isinstance(_k, str) and _k.startswith(_CARD_NS)):
            continue
        _out = dw.language_text_zh(_k)
        assert not _residue(_out), f"{_k} 产出带残留：{_out!r}"
        _n_res += 1
print(f"§二 数据面残留扫描：{_n_res} 个卡面键，产出零残留（三类模式）")

# ② **卡面面**：把能用冻结夹具驱动的 fmt_* 全渲染一遍，任何产出行都不得带残留
_B = b  # 顶部已解析的夹具 bundle
_SWEEP = [
    (
        "fmt_timers",
        (
            [
                ("夜灵平野", _B["cetusCycle"]),
                ("魔胎之境", _B["cambionCycle"]),
                ("地球", _B["earthCycle"]),
                ("金星", _B["vallisCycle"]),
                ("双衍王境", _B["duviriCycle"]),
                ("扎里曼号", _B["zarimanCycle"]),
            ],
        ),
    ),
    ("fmt_cetus", (_B["cetusCycle"], _B["vallisCycle"], _B["cambionCycle"], _B["earthCycle"])),
    ("fmt_sortie", (_B["sortie"],)),
    ("fmt_archon", (_B["archonHunt"],)),
    ("fmt_invasions", (_B["invasions"],)),
    ("fmt_fissures", (_B["fissures"],)),
    ("fmt_duviri_dummy", None),  # 占位：下面按需跳过
    ("fmt_nightwave", (_B["nightwave"],)),
    ("fmt_void_storms", (_B["voidStorms"],)),
    ("fmt_void_trader", (_B["voidTrader"],)),
    ("fmt_synth_targets", (_B["synthTargets"],)),
    ("fmt_daily_deals", (_B["dailyDeals"],)),
    ("fmt_flash_sales", (_B["flashSales"],)),
    ("fmt_news", (_B["news"],)),
    ("fmt_conclave", (_B["conclaveChallenges"],)),
    ("fmt_clan_rewards", (_B["clanRewards"],)),
    ("fmt_construction", (_B["constructionProgress"],)),
    ("fmt_events", (raw.get("Events") if isinstance(raw.get("Events"), list) else [],)),
    ("fmt_prime_vault", (_B["primeVault"],)),
    ("fmt_descendia", (_B["descendia"],)),
    ("fmt_calendar", (_B["calendar"],)),
    ("fmt_archimedea", (_B["deepArchimedea"], "深层科研")),
    ("fmt_archimedea", (_B["temporalArchimedea"], "时光科研")),
    ("fmt_steel_essence_shop", ()),
    ("fmt_bounties", (_B["syndicateMissions"],)),
    ("fmt_alerts", (_B["alerts"],)),
    ("fmt_kuva", ([],)),
    ("fmt_steel_path", (None,)),
    ("fmt_incursions", (None,)),
]
_n_lines = 0
for _name, _args in _SWEEP:
    _fn = getattr(fmt, _name, None)
    if _fn is None or _args is None:
        continue
    _r = _fn(*_args)
    _out_lines = _r[1] if isinstance(_r, tuple) else _r
    if isinstance(_out_lines, str):
        _out_lines = [_out_lines]
    for _ln in _out_lines or []:
        _n_lines += 1
        assert not _residue(str(_ln)), f"{_name} 产出带残留：{_ln!r}"
print(
    f"§二 卡面面残留扫描：{len([x for x in _SWEEP if x[1] is not None])} 个 fmt_* 调用，"
    f"共 {_n_lines} 行产出，零残留"
)
print("§二 模板标记清洗：断言通过")

# ---------------------------------------------------------------------------
# §一 卡面数据源改 `read_path()`（2026-09-28 裁定）
# ---------------------------------------------------------------------------
# 背景：卡面原读**包内**常量 ⇒ 自动刷新回写到不了卡面、换批只能靠发版。
# 现在两个读点（时效汇总 / 轮换卡）都走 `core_paths.read_path("rotations.json")`：
# **运行期副本（plugin_data）优先 → 包内种子回退**。
# 断言分两级：源码级（防回归）+ 行为级（注入临时运行目录，验证「优先」与「回退」两侧）。
# 结构优化 D1 起读点随 handler 分域：时效汇总在 core/commands/daily.py、
# 轮换卡（_rotation_lines）暂在 main.py —— 扫描集 = main.py + core/commands/*.py。
_SRC_ROOT = Path(__file__).resolve().parent.parent
_SRC_FILES = [_SRC_ROOT / "main.py"] + sorted((_SRC_ROOT / "core" / "commands").glob("*.py"))
_MAIN_SRC = "\n".join(p.read_text(encoding="utf-8") for p in _SRC_FILES)
assert "ROTATION_FILE" not in _MAIN_SRC, "源码仍引用已删除的包内常量（应只留注释说明）"
_code_hits = [
    ln
    for ln in _MAIN_SRC.splitlines()
    if 'core_paths.read_path("rotations.json")' in ln and not ln.lstrip().startswith("#")
]  # 排除注释里提到的那一处
assert len(_code_hits) == 2, f"两个读点都应改走 read_path（非注释行），实得 {len(_code_hits)} 处"


def _install_astrbot_stub() -> None:
    """最小 astrbot.api 桩（与 tests/test_dun.py 同款），使 main.py 可在离线环境 import。"""
    import types

    if "astrbot" in sys.modules:
        return
    pkg = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    ev = types.ModuleType("astrbot.api.event")
    mc = types.ModuleType("astrbot.api.message_components")
    st = types.ModuleType("astrbot.api.star")

    class _F:
        EventMessageType = type("X", (), {"ALL": 1, "GROUP_MESSAGE": 2, "PRIVATE_MESSAGE": 3})

        @staticmethod
        def event_message_type(*a, **k):
            return lambda f: f

        @staticmethod
        def command(*a, **k):
            return lambda f: f

        @staticmethod
        def platform(*a, **k):
            return lambda f: f

    ev.AstrMessageEvent = object
    ev.MessageChain = list
    ev.filter = _F
    mc.Image = object
    mc.Plain = object
    st.Context = object
    st.Star = type("Star", (), {"__init__": lambda self, *a, **k: None})
    st.register = lambda *a, **k: lambda cls: cls
    api.AstrBotConfig = dict
    api.logger = type(
        "L", (), {n: (lambda *a, **k: None) for n in ("info", "warning", "error", "debug")}
    )()
    api.event, api.message_components, api.star = ev, mc, st
    pkg.api = api
    sys.modules.update(
        {
            "astrbot": pkg,
            "astrbot.api": api,
            "astrbot.api.event": ev,
            "astrbot.api.message_components": mc,
            "astrbot.api.star": st,
        }
    )


import tempfile  # noqa: E402

_install_astrbot_stub()  # ★ 必须在 import main 之前
import main as _main  # noqa: E402
from core import paths as _core_paths  # noqa: E402

_MARK_BONUS = 77.7  # 运行期副本里独有的哨兵值（包内种子不可能有）
_RUNTIME_ROT = {
    "tenet": {
        "mode": "refresh_only",
        "epoch": "2026-09-12T00:00:00+00:00",
        "period_hours": 96,
        "valence_snapshot": "2026-09-28T01:06:28+00:00",
        "items": [
            {"en": "Tenet Probe", "cn": "信条·探针", "element": "Heat", "bonus": _MARK_BONUS}
        ],
    },
}
_orig_run = _core_paths._RUN  # noqa: SLF001  —— 复原用（离线未注入时为 None）
_tmp_runtime = tempfile.mkdtemp(prefix="wf_rot_runtime_")
(Path(_tmp_runtime) / "rotations.json").write_text(
    json.dumps(_RUNTIME_ROT, ensure_ascii=False), encoding="utf-8"
)
_tmp_empty = tempfile.mkdtemp(prefix="wf_rot_empty_")

_m_inst = _main.WarframeSDJK.__new__(_main.WarframeSDJK)
try:
    _core_paths.set_run_dir(_tmp_runtime)
    _txt = "\n".join(_m_inst._rotation_lines("tenet", "Ergo 信条武器轮换").lines)
    assert "信条·探针" in _txt and f"{_MARK_BONUS}%" in _txt, (
        f"卡面应读到**运行期副本**的值，实得：{_txt!r}"
    )
    assert "信条·集议" not in _txt, "不该回落到包内种子（运行期副本优先）"
    # 回退侧：运行期副本不存在 ⇒ 必须回落到**包内种子**（防把回退改丢）
    _core_paths.set_run_dir(_tmp_empty)
    _txt2 = "\n".join(_m_inst._rotation_lines("tenet", "Ergo 信条武器轮换").lines)
    assert "信条·集议" in _txt2, f"缺运行期副本时应回退包内种子，实得：{_txt2!r}"
finally:
    _core_paths.set_run_dir(_orig_run)
assert "信条·探针" not in "\n".join(_m_inst._rotation_lines("tenet", "Ergo 信条武器轮换").lines), (
    "临时目录泄漏到后续用例"
)
print(
    "§一 数据源：源码级（常量零引用 / 两读点走 read_path）+ 行为级"
    "（运行期副本优先 + 缺文件回退种子）通过"
)

# ---------------------------------------------------------------------------
# §二 数字槽（`|STAT1|` 类）并入「可清洗」+ `|COUNT|` 顺序陷阱（2026-09-28 二批）
# ---------------------------------------------------------------------------
# 裁定：数字槽与 `|COUNT|` 同族（调用方可能填数）⇒ **剥掉留正文**，**不**升级为「不可用」。
_DIGIT_SLOT = re.compile(r"\|[A-Z_]*\d[A-Z_]*\|")
_SLOT_KEYS = {
    "/Lotus/Language/1999/AbilityStatusProcsGiveAbilityStrengthAndEfficiencyDesc": "每使用技能造成一种异常状态可增加 % 技能强度和 % 技能效率，持续 秒。",
    "/Lotus/Language/1999/MessengerStoreBuyConfirm": "您确定要将 x 寄送给吗？",
    "/Lotus/Language/Items/CatbrowReflectPreceptDesc": "镜像型库娃有 % 几率将伤害倍化 % 后反射给一名敌人。",
    "/Lotus/Language/Items/WarframeModFallingImpactDesc": "从高处着地将会产生 米的范围震波，造成 伤害并震倒敌人。",
    "/Lotus/Language/Weapons/LasGooSicklesUpgradeDesc": "：获得 和 ，持续 秒。最多叠加到 层。",
}
for _k, _want in _SLOT_KEYS.items():
    _out = dw.language_text_zh(_k)
    assert _out == _want, f"{_k}\n  期望 {_want!r}\n  实得 {_out!r}"
    assert not _DIGIT_SLOT.search(_out), f"{_k} 仍有数字槽残留：{_out!r}"
    assert not dw._is_placeholder(_want), "数字槽**不**升级为不可用（剥掉留正文）"
print(f"§二 数字槽：{len(_SLOT_KEYS)} 个卡面键剥净（残留 0），且未被判「不可用」")

# ★ 顺序陷阱实测：`|COUNT|` 是**调用方填数槽**，两条填数链路各断言一次
_ch = dw._challenge_table()
_cnt_keys = [
    k for k, v in _ch.items() if isinstance(v, dict) and "|COUNT|" in (v.get("desc") or "")
]
assert len(_cnt_keys) >= 100, f"含 |COUNT| 的挑战应很多，实得 {len(_cnt_keys)}"
_cal_probe = {
    "season": "CST_WINTER",
    "yearIteration": 23,
    "expiry": "",
    "days": [
        {
            "day": 1,
            "date": "1999-01-01",
            "events": [
                {
                    "type": "CHALLENGE",
                    "name": "探针",
                    "count": 42,
                    "desc": "摧毁 |COUNT| 个储存容器",
                }
            ],
        }
    ],
}
_cal_txt = "\n".join(fmt.fmt_calendar(_cal_probe)[1])
assert "42" in _cal_txt and "|COUNT|" not in _cal_txt, f"|COUNT| 未被填数：{_cal_txt!r}"
assert (
    fmt._nw_desc("使用<DT_FIRE>火焰伤害击杀 |COUNT| 名敌人", 150) == "使用火焰伤害击杀 150 名敌人"
)
print("§二 顺序陷阱：`|COUNT|` 两条填数链路都产出数字（1999 日历 = 42；电波 = 150），清洗层未抢剥")

# ★ 残留断言扩成**四模式**，两层都重扫一遍（数据面 + 卡面面；与上一批同源，不另起炉灶）
_RES_RE = (
    re.compile(r"\|[A-Za-z_]{1,24}\|"),
    re.compile(r"\|[A-Z_]*\d[A-Z_]*\|"),
    re.compile(r"<[A-Z_]{3,40}>"),
    re.compile(r"\[PH\]"),
)
_res_n = 0
for _tbn in ("languages_zh.json", "zh_ext.json"):
    for _k in dw._load(_tbn) or {}:
        if not (isinstance(_k, str) and _k.startswith(_CARD_NS)):
            continue
        assert not _residue(dw.language_text_zh(_k)), f"{_k} 产出带四模式残留"
        _res_n += 1
_card_n = 0
for _name, _args in _SWEEP:
    _fn = getattr(fmt, _name, None)
    if _fn is None or _args is None:
        continue
    _r = _fn(*_args)
    _out_lines = _r[1] if isinstance(_r, tuple) else _r
    if isinstance(_out_lines, str):
        _out_lines = [_out_lines]
    for _ln in _out_lines or []:
        _card_n += 1
        assert not _residue(str(_ln)), f"{_name} 产出带四模式残留：{_ln!r}"
print(f"§二 四模式残留：数据面 {_res_n} 个卡面键 + 卡面面 {_card_n} 行，零残留")
print("§二 数字槽清洗：断言通过")

# ---------------------------------------------------------------------------
# §三 C3 警报解析（2026-10-03）：DE 字段是 MissionInfo（旧实现读 Mission ⇒ 全空
#   ⇒ 卡面「奖励：?」）。本段用**线上实抓的原始形态**做夹具对拍。
# ---------------------------------------------------------------------------
_ALERT_RAW = {
    "Alerts": [
        {
            "_id": {"$oid": "6abdec7a31ca47b57807bccd"},
            "Activation": {"$date": {"$numberLong": "1790863200000"}},
            "Expiry": {"$date": {"$numberLong": "1792087200000"}},
            "MissionInfo": {
                "location": "SolNode87",
                "missionType": "MT_ARTIFACT",
                "faction": "FC_CORPUS",
                "missionReward": {
                    "credits": 10000,
                    "items": ["/Lotus/StoreItems/Types/Items/ShipDecos/Plushies/Plushy2021QTCC"],
                },
                "minEnemyLevel": 20,
                "maxEnemyLevel": 30,
                "descText": "/Lotus/Language/Alerts/TennoUnitedAlert",
            },
            "Tag": "LotusGift",
        }
    ]
}
_al = dw._parse_alerts(_ALERT_RAW)
_m = (_al[0].get("mission") or {}) if _al else {}
assert (
    _al
    and _m.get("node") == "Ganymede（木星）"
    and _m.get("type") == "中断"
    and _m.get("faction") == "Corpus"
    and _m.get("min_level") == 20
    and _m.get("max_level") == 30
    and _m.get("desc") == "Tenno 联合警报"
), str(_m)
assert (_m.get("reward") or {}).get("credits") == 10000 and (_m.get("reward") or {}).get("items"), (
    str(_m.get("reward"))
)
_t3, _l3 = fmt.fmt_alerts(_al)
assert (
    _l3
    and "?" not in _l3[0]
    and "Ganymede" in _l3[0]
    and "中断" in _l3[0]
    and "20-30级" in _l3[0]
    and "10000现金" in _l3[0]
), str(_l3)
_l4 = fmt.fmt_alerts(
    [{**_al[0], "mission": {**_m, "reward": {**_m["reward"], "item_names": ["2021 年 QTCC 玩偶"]}}}]
)[1]
assert _l4 and "2021 年 QTCC 玩偶" in _l4[0], str(_l4)
print("§三 C3 警报解析：MissionInfo 补全 / 奖励不再「?」/ 中文名优先 —— 断言通过")

# ============================================================================
# §四 警报奖励中文名（2026-10-04）：StoreItems 反查表 + 路径归一 + 三级回落
# ============================================================================
import asyncio  # noqa: E402

from core.api_client import (  # noqa: E402
    WarframeClient,
    store_item_zh,
    store_items_path_variants,
)

# ① 反查表：线上实测三条（QTCC 玩偶；键名不规则 —— Plush2021QTCCName 掉 y、
#    PlushyVirminkQTCCDecoName 多 Deco，绝不能用「末段 + Name」硬拼）
for _p, _zh in (
    ("/Lotus/StoreItems/Types/Items/ShipDecos/Plushies/Plushy2021QTCC", "征服库阿卡玩偶"),
    ("/Lotus/StoreItems/Types/Items/ShipDecos/Plushies/Plushy2022QTCC", "征服梭歌玩偶"),
    ("/Lotus/StoreItems/Types/Items/ShipDecos/Plushies/PlushyVirminkQTCC", "征服弗鸣克玩偶"),
):
    assert store_item_zh(_p) == _zh, (_p, store_item_zh(_p))
# ② 导出键形态（无 StoreItems 段）直接命中；未知路径返回空（绝不造名）
assert store_item_zh("/Lotus/Types/Items/ShipDecos/Plushies/Plushy2021QTCC") == "征服库阿卡玩偶"
assert store_item_zh("/Lotus/StoreItems/Types/Items/Nope/Unknown") == ""
# ③ 路径归一三态
_vs = store_items_path_variants("/Lotus/StoreItems/Types/Items/ShipDecos/Plushies/Plushy2021QTCC")
assert "/Lotus/Types/Items/ShipDecos/Plushies/Plushy2021QTCC" in _vs, _vs


# ④ 三级回落整链：新表 → WM gameRef → 未收录（WM 装饰品为空，模拟其返回 []）
async def _resolve_probe():
    c = WarframeClient()

    async def _wm():
        return []

    c.wm_items = _wm  # type: ignore[method-assign]
    data = [
        {
            "mission": {
                "reward": {
                    "item": "Plushy2021 QTCC",
                    "items": [
                        "/Lotus/StoreItems/Types/Items/ShipDecos/Plushies/Plushy2021QTCC",
                        "/Lotus/StoreItems/Types/Items/Nope/Unknown",
                    ],
                }
            }
        }
    ]
    rw = (await c._resolve_alert_items(data))[0]["mission"]["reward"]
    assert rw["item_names"] == ["征服库阿卡玩偶"], rw
    assert rw["item"] == "征服库阿卡玩偶" and rw["item_unknown"] == ["Unknown"], rw
    d2 = [
        {
            "mission": {
                "reward": {
                    "item": "Unknown",
                    "items": ["/Lotus/StoreItems/Types/Items/Nope/Unknown"],
                }
            }
        }
    ]
    rw2 = (await c._resolve_alert_items(d2))[0]["mission"]["reward"]
    assert rw2["item"] == "Unknown（未收录）" and rw2["item_names"] == [], rw2
    return rw


rw_ok = asyncio.run(_resolve_probe())
print("§四 警报奖励中文名：反查表（三条玩偶）/ 路径归一 / 三级回落不造名 —— 断言通过")
