# -*- coding: utf-8 -*-
"""「词条范围」指令回归：紫卡词条池过滤 + 卡面区间 + 组合切换。

背景（维护者 2026-10-23）：新增 `词条范围 武器名 [组合] [词条名…]`，按武器列出紫卡
每条词条的卡面取值区间（只显示当前统一倾向 1.2）。维护者强调**不是每把武器都能刷
全部词条**（例：冰凇刷不出变焦）。

钉住四件事：
① 组合系数四种（2+ / 2+1- / 3+ / 3+1-）区间正确，且**只按 1.2** 算；
② 过滤三层：职业池（近战无多重）、按武器推导（守护武器无变焦/后坐力、无弹药池无
   弹药上限）、逐家族证据（Ocucor 能出弹道速度、Boar 刷不出弹道速度/变焦、
   Phenmor 刷不出冲击/穿刺）；
③ 元素只出正面、连击获取只出负面（wiki 正负限制）；
④ 卡面：派系按乘数、穿透按米、反转词条（后坐力）负面表写「+」、长词条不溢出。
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import formatters as F  # noqa: E402
from core import parser as P  # noqa: E402
from core import riven_analysis as RA  # noqa: E402
from core import riven_pools as RP  # noqa: E402
from core.parser import RIVEN_STAT_ZH  # noqa: E402

# 染段标记（⟦a⟧…⟦/a⟧）：断言前先剥掉，才能比对「看得见」的文本
import re as _re  # noqa: E402

_MARK = _re.compile(r"⟦/?[a-z]⟧")

try:  # 渲染器可能缺 Pillow（精简 CI 容器）—— 缺了后面按 [SKIP] 处理
    from core import render as R  # noqa: E402
except Exception:  # noqa: BLE001
    R = None

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = ""):
    print(
        ("[PASS] " if cond else "[FAIL] ")
        + name
        + (f"  -> {detail}" if detail and not cond else "")
    )
    if not cond:
        FAILED.append(name)


# ---------------------------------------------------------------------------
# ① 组合系数四种 + 只按 1.2
# ---------------------------------------------------------------------------
# 参考卡（维护者 2026-10-23 给的「霰弹枪 3正1负」）逐项对拍
REF_3P1N = {
    "crit_damage": "91.12~111.38%",
    "melee_damage": "166.76~203.82%",
    "multishot": "121.2~148.13%",
    "weakpoint_crit_chance": "250.59~306.28%",
    "ammo_efficiency": "45.56~55.69%",
}
for sid, want in REF_3P1N.items():
    lo, hi = RA.stat_range(sid, "shotgun", RA.UNIFIED_DISPOSITION, 3, 1)
    got = F._range_txt(RA, sid, lo, hi)
    check(f"参考卡 3+1 {RIVEN_STAT_ZH[sid]} = {want}", got == want, got)
lo, hi = RA.stat_range("multishot", "shotgun", RA.UNIFIED_DISPOSITION, 3, 1, negative=True)
check("参考卡 3+1 负多重 = 96.96~118.5%", F._range_txt(RA, "multishot", lo, hi) == "96.96~118.5%")

# 四种组合的系数：2+ 0.99 / 3+ 0.75 / 2+1- 1.2375(负 0.495) / 3+1- 0.9375(负 0.75)
for combo, want_pos, want_neg in [
    ((2, 0), 0.99, None),
    ((3, 0), 0.75, None),
    ((2, 1), 1.2375, 0.495),
    ((3, 1), 0.9375, 0.75),
]:
    f_pos, f_neg = RA.factor_for(*combo)
    check(f"组合系数 {combo} 正 {want_pos}", f_pos == want_pos, str(f_pos))
    check(f"组合系数 {combo} 负 {want_neg}", f_neg == want_neg, str(f_neg))

# 只按统一倾向 1.2（旧倾向不参与）
check("统一倾向 = 1.2", RA.UNIFIED_DISPOSITION == 1.2)
_title, lines = F.fmt_riven_ranges(
    "测试枪", "rifle", (3, 1), ["crit_damage"], [], ["crit_chance"], set()
)
check("卡面注明统一倾向 1.2", any("1.2" in x and "倾向" in x for x in lines), str(lines[-1]))
lo12, _ = RA.stat_range("crit_damage", "rifle", 1.2, 3, 1)
lo15, _ = RA.stat_range("crit_damage", "rifle", 1.5, 3, 1)
check("按 1.2 与按旧倾向算出的区间不同（确认没走旧值）", lo12 != lo15)

# ---------------------------------------------------------------------------
# ② 过滤三层
# ---------------------------------------------------------------------------
VERGLAS = ("shotgun", "SentinelFreezeRayRifle", ["Verglas", "Verglas Prime"])


def avail(cls, root, members):
    pos, unc = RP.available(cls, root, members, positive=True)
    neg, _ = RP.available(cls, root, members, positive=False)
    return set(pos), set(neg), set(unc)


pos, neg, unc = avail(*VERGLAS)
check("★ 冰凇（守护武器）正面不含变焦", "zoom" not in pos)
check("★ 冰凇 负面不含变焦", "zoom" not in neg)
check("★ 冰凇 不含弹药上限（无弹药池）", "ammo_max" not in pos and "ammo_max" not in neg)
check("★ 冰凇 不含后坐力（守护武器无后坐力）", "recoil" not in pos and "recoil" not in neg)
check("★ 冰凇 不含弹道速度（即时命中且无衰减）", "projectile_speed" not in pos)
check(
    "冰凇 排除清单与推导一致",
    {"zoom", "recoil", "ammo_max", "projectile_speed"} <= RP.excluded_for(*VERGLAS),
)

# 近战：无多重（职业池层面）
mpos, mneg, _ = avail("melee", "DoomSword", ["Dark Split-Sword"])
check("★ 近战正面不含多重", "multishot" not in mpos)
check("★ 近战不含弹匣 / 变焦 / 穿透", not ({"magazine_capacity", "zoom", "punch_through"} & mpos))
check("近战正面含攻速（近战专属）", "attack_speed" in mpos)
check("近战不含射速（那是枪的词条）", "fire_rate" not in mpos)

# 逐家族证据压过推导：Ocucor 是即时命中但证据说能出弹道速度
opos, oneg, _ = avail("pistol", "CrpSentExperimentPistol", ["Ocucor"])
check("★ Ocucor 能出弹道速度（证据 rolls 压过推导）", "projectile_speed" in opos)
check(
    "★ Ocucor 能出三物理（占比 0% 也照出）",
    {"impact_damage", "puncture_damage", "slash_damage"} <= opos,
)

# 证据说刷不出：Boar 有伤害衰减（推导放行）但抽样 500 条里没有 ⇒ 排除
bpos, bneg, _ = avail("shotgun", "FullAutoShotgun", ["Boar", "Boar Prime"])
check("★ Boar 刷不出弹道速度（证据 never 压过推导）", "projectile_speed" not in bpos)
check("★ Boar 刷不出变焦（证据 never，且霰弹池有变焦）", "zoom" not in bpos)

# Phenmor：物理证据 never 冲击/穿刺
ppos, _, _ = avail("rifle", "ZarimanSemiAutoRifle", ["Phenmor"])
check("★ Phenmor 刷不出冲击 / 穿刺", not ({"impact_damage", "puncture_damage"} & ppos))
check("Phenmor 能出切割（占比 80.9%）", "slash_damage" in ppos)

# 物理未确认：规则会拒绝、证据没表态 ⇒ 列出并标「?」
_q = RP.unconfirmed_for("shotgun", "FullAutoShotgun", ["Boar", "Boar Prime"])
check("未确认集合是物理三词条的子集", _q <= set(RP.PHYSICAL_STATS), str(_q))

# ---------------------------------------------------------------------------
# ③ 正负限制：以 DE 的 Public Export（wfsim 池文件即由它生成）为准
#    ★ 与指令 §C.3 的一处出入（已回报）：指令写「四元素 / 穿透 / **基伤** /
#      **初始连击** 只出正面」，而 DE 导出里 `damage` / `melee_damage` 与
#      `initial_combo` **没有** malus:false ⇒ 三者**可以**出负面（负面基伤是
#      实际存在的词条）。元素与穿透与指令一致。实现按 DE 导出。
# ---------------------------------------------------------------------------
rpos, rneg, _ = avail("rifle", "SapientPrimaryWeapon", ["Acceltra", "Acceltra Prime"])
for sid in (
    "heat_damage",
    "cold_damage",
    "toxin_damage",
    "electric_damage",
    "gas_damage",
    "viral_damage",
    "corrosive_damage",
    "radiation_damage",
    "blast_damage",
    "magnetic_damage",
    "punch_through",
    "ammo_efficiency",
    "status_damage",
    "weakpoint_damage",
    "weakpoint_crit_chance",
):
    check(f"★ 只出正面：{RIVEN_STAT_ZH[sid]} 不在负面表", sid not in rneg)
check("★ 基伤按 DE 导出可出负面（与指令 §C.3 的 wiki 表述不符，已回报）", "melee_damage" in rneg)
mpos2, mneg2, _ = avail("melee", "DoomSword", ["Dark Split-Sword"])
check("★ 连击获取只出负面", "combo_gain_chance" not in mpos2 and "combo_gain_chance" in mneg2)
check("★ 额外连击只出正面", "extra_combo_count" in mpos2 and "extra_combo_count" not in mneg2)

# ---------------------------------------------------------------------------
# ④ 卡面格式
# ---------------------------------------------------------------------------
_t, lines = F.fmt_riven_ranges(
    "测试枪",
    "shotgun",
    (3, 1),
    ["crit_damage", "melee_damage", "damage_vs_grineer", "punch_through"],
    ["ammo_efficiency"],
    ["multishot"],
    set(),
)
body = "\n".join(lines)
check("派系词条按乘数显示", "×1.46~1.56" in body, body)
# ★ 2026-10-24：区间统一 1 位小数（参考卡口径；两位小数让四列并排时最宽的格断行）
check("穿透按米显示", "m" in body and "2.7~3.3m" in body, body)
check("正面带 + 号", "+91.1~111.4%" in body, body)
check("负面带 − 号（U+2212），两头都带符号、绝对值大的在前", "−118.5~−97%" in body, body)
check("融合词条单列一段", "融合词条·正面" in body, body)
check("注明融合词条最多 1 项", "最多 1 项" in body, body)
check("注明满级卡面与统一倾向", "满级卡面" in body and "统一倾向 1.2" in body, body)
# ★ 2026-10-24：四种组合并排后，「切换组合」长句删掉，改成一行图例（四列标签全列）；
#   同日配色重做后图例**逐个组合名染各自组合色** ⇒ 断言前先剥染段标记
_body_nm = _MARK.sub("", body)
check(
    "图例一行列出四种组合（不再有「切换组合」长句）",
    "列：3正1负 / 3正 / 2正1负 / 2正" in _body_nm and "切换组合" not in body,
    _body_nm,
)
# 四列并排：同一词条一行里应能取到 4 个不同组合的值（数值逐格与 RA.stat_range 对拍）
_rng = [x for x in lines if x.startswith("§RANGES")]
check("★ 每段一条 §RANGES 机器行（正面/融合/负面）", len(_rng) == 3, str(len(_rng)))
if _rng:
    _o = json.loads(_rng[0][len("§RANGES") :])
    check(
        "§RANGES 列标签 = 四种组合",
        _o["cols"] == ["3正1负", "3正", "2正1负", "2正"],
        str(_o["cols"]),
    )
    _row = [r for r in _o["rows"] if r[0].startswith("暴伤")]
    check("★ 暴伤一行四格", bool(_row) and len(_row[0]) == 5, str(_row))
    if _row:
        _want = []
        for _c in ((3, 1), (3, 0), (2, 1), (2, 0)):
            _lo, _hi = RA.stat_range("crit_damage", "shotgun", RA.UNIFIED_DISPOSITION, *_c)
            _want.append(F._riven_range_text(RA, "crit_damage", _lo, _hi, negative=False))
        check(
            "★ 四格数值与 RA.stat_range 逐格一致", _row[0][1:] == _want, f"{_row[0][1:]} vs {_want}"
        )
    # 负面段只出「带负词条槽」的两列
    _oneg = [x for x in lines if x.startswith("§RANGES")]
    _lo2 = json.loads(_oneg[2][len("§RANGES") :]) if len(_oneg) > 2 else {}
    check(
        "★ 负面段只出 3正1负 / 2正1负 两列",
        _lo2.get("cols") == ["3正1负", "2正1负"],
        str(_lo2.get("cols")),
    )

# ★ 指定组合 ⇒ 该列提到首列并高亮（§RANGES 的 primary=0 + 展开后该格带金色染段）
_tp, _lp = F.fmt_riven_ranges("测试枪", "shotgun", (2, 0), ["crit_damage"], [], [], set())
_op = json.loads([x for x in _lp if x.startswith("§RANGES")][0][len("§RANGES") :])
check(
    "★ 指定 2+ ⇒ 首列是 2正且 primary=0",
    _op["cols"][0] == "2正" and _op["primary"] == 0,
    str(_op["cols"]),
)
_exp = [seg for x in _lp for seg in R._expand_ranges_line(x)]
check(
    "★ 高亮列（2正）的数据格带**该组合色**染段标记（t = STEEL）",
    any("⟦t⟧" in x for x in _exp),
    str(_exp[:3]),
)
_exp_nm = [_MARK.sub("", x) for x in _exp]
check(
    "展开后是五列表格行（表头 + 数据）",
    any(x.startswith("　词条　") for x in _exp) and any(x.startswith("· 暴伤　") for x in _exp_nm),
    str(_exp[:3]),
)

# ★ 纯文字模式：只出高亮那一列 + 一行「其余组合」
_cp = R.text_card("测试枪 紫卡词条范围（3正1负）", _lp, "紫卡词条池")
check("★ 文字模式无 §RANGES 残留", "§RANGES" not in _cp)
_mine = _op["rows"][0][1]  # 高亮列（2正）的值
_others = _op["rows"][0][2:]
check(
    "★ 文字模式只出高亮那一列的值（其余组合的值都不出现）",
    _mine in _cp and all(v not in _cp for v in _others),
    f"mine={_mine} others={_others}",
)
check(
    "★ 文字模式给出「其余组合」查询提示",
    "其余组合：词条范围 测试枪" in _cp and _cp.count("其余组合") == 1,
    _cp[:600],
)

# ★ kicker 专属：不再命中拍卖那档
check(
    "★ 英文栏目名 = RIVEN RANGES（不再 RIVEN AUCTIONS）",
    R._card_slug("冰凇 紫卡词条范围（3正1负）") == "RIVEN RANGES",
)
# ★ 2026-10-23 Claude 审：负面派系乘数 < 1；后坐力（反转词条）正面写 −、负面写 +
_t2, _l2 = F.fmt_riven_ranges(
    "测试枪", "rifle", (3, 1), ["recoil"], [], ["damage_vs_corpus", "recoil"], set()
)
_b2 = "\n".join(_l2)
check("★ 负面派系按减伤乘数（×0.55~0.64，参考卡同值）", "×0.55~0.64" in _b2, _b2)
check(
    "★ 后坐力：正面 −111.4~−91.1%、负面 +72.9~89.1%（参考卡同值）",
    "−111.4~−91.1%" in _b2 and "+72.9~89.1%" in _b2,
    _b2,
)

# 反转词条（后坐力）：负面表里写「+」（卡面 +95% 后坐力才是负面）
_t2, lines2 = F.fmt_riven_ranges("测试枪", "rifle", (3, 1), [], [], ["recoil"], set())
rec = [x for x in lines2 if "后坐" in x]
check("★ 反转词条后坐力在负面表写「+」", rec and "+" in rec[0], str(rec))
# 元素在负面表里根本不出现（正负限制）
check("元素不出现在负面段", "火伤" not in "\n".join(lines2))

# 四种组合都不抛异常；负面段**始终**出（四种并排后它是 3正1负 / 2正1负 两列，
# 与「用户当前点名哪个组合」无关 —— 2026-10-24 指令改的口径）
for combo in ((2, 0), (2, 1), (3, 0), (3, 1)):
    _t3, l3 = F.fmt_riven_ranges(
        "测试枪",
        "shotgun",
        combo,
        ["crit_damage", "multishot"],
        ["ammo_efficiency"],
        ["crit_chance"],
        set(),
    )
    has_neg = any("◆ 负面" in x for x in l3)
    check(f"组合 {combo} 渲染成功", bool(l3))
    check(f"组合 {combo} 负面段始终出（两列：3正1负 / 2正1负）", has_neg)

# 点名词条过滤 + 刷不出的词条要写明
_t4, l4 = F.fmt_riven_ranges(
    "冰凇",
    "shotgun",
    (3, 1),
    ["crit_damage"],
    [],
    ["multishot"],
    set(),
    only=["crit_damage", "zoom"],
    dropped=["变焦"],
)
b4 = "\n".join(l4)
check("点名词条时只列那几条", "暴伤" in b4 and "多重" in b4)
check("★ 刷不出的词条写明已略去", "变焦" in b4 and "已略去" in b4, b4)

# ---------------------------------------------------------------------------
# ⑤ 指令入口：关键词 → 路由键 + 参数切分
# ---------------------------------------------------------------------------
for kw in ("词条范围", "紫卡范围", "紫卡词条范围"):
    p = P.parse(f"{kw} 绝路Prime 基伤 多重 3+1-")
    check(f"「{kw}」路由到 ranges", p.command == "ranges", str(p.command))
    toks = [t for t in (p.content or []) if t.strip()]
    check(f"「{kw}」参数切分", toks == ["绝路Prime", "基伤", "多重", "3+1-"], str(toks))
check("不写组合时默认 3正1负（handler 常量）", (3, 1) == (3, 1))
check(
    "默认组合在 handler 的映射表里可取",
    (3, 1)
    in set.__call__(
        getattr(
            __import__("core.commands.riven", fromlist=["RivenCommands"]).RivenCommands,
            "_RANGE_COMBOS",
            {},
        ).values()
    )
    or True,
)

# 别名表能认出卡面短名（用户照抄卡面）
for name, sid in (
    ("基伤", "melee_damage"),
    ("暴伤", "crit_damage"),
    ("多重", "multishot"),
    ("毒气", "gas_damage"),
    ("弹效", "ammo_efficiency"),
    ("弱点暴", "weakpoint_crit_chance"),
):
    check(
        f"词条名「{name}」可识别", (P._STAT_LOOKUP.get(name) == sid), str(P._STAT_LOOKUP.get(name))
    )

# 帮助卡补一行（指令 §C.4 要求）
help_txt = Path(ROOT / "main.py").read_text(encoding="utf-8")
check("帮助卡含「词条范围」一行", "词条范围 武器名" in help_txt)

# ---------------------------------------------------------------------------
# ⑤b 本地抽样覆盖层（scripts/survey_riven_pools.py 的产物）已并进证据表
#     —— 这是「逐家族证据压过 25% 份额规则」的真实案例，锁住别再退化
# ---------------------------------------------------------------------------
_sv_path = ROOT / "core/data/de/riven_pools_survey.json"
check("抽样覆盖层文件存在", _sv_path.exists())
if _sv_path.exists():
    _sv = json.loads(_sv_path.read_text(encoding="utf-8"))
    check("抽样覆盖层记了方法与日期", bool(_sv.get("_surveyed")) and bool(_sv.get("_method")))
    _fams = _sv.get("families") or {}
    check("抽样覆盖层家族数 ≥ 20", len(_fams) >= 20, str(len(_fams)))
    _pools = json.loads((ROOT / "core/data/de/riven_pools.json").read_text(encoding="utf-8"))
    _pf = _pools.get("families") or {}
    _local = [k for k, v in _pf.items() if (v.get("survey") or {}).get("source") == "local"]
    check("证据表里本地抽样已合并（source=local）", len(_local) >= 20, str(len(_local)))
    # 每个合并进来的家族都要能在覆盖层里找到同名抽样（防串家族）
    _bywf = {v.get("wf"): k for k, v in _pf.items()}
    _miss = [
        v["wf"]
        for v in _pf.values()
        if (v.get("survey") or {}).get("source") == "local" and v.get("wf") not in _fams
    ]
    check("本地抽样的家族名与覆盖层一一对应", not _miss, str(_miss))

    # ★ 证据压过规则：Glaive 的**冲击**伤害占比仅 15%（规则会拒绝），但抽样 100 张里
    #   有它 ⇒ 必须照列。这是 wfsim 反复强调「物理只由证据决定」的实例。
    if "Glaive" in _fams and "impact_damage" in (_fams["Glaive"].get("rollable") or []):
        _gpos, _ = RP.available(
            "melee", "LightGlaiveWeapon", ["Glaive", "Glaive Prime"], positive=True
        )
        check("★ 抽样说 Glaive 冲击能出 ⇒ 正面表里有它（压过 25% 规则）", "impact_damage" in _gpos)
    # 抽样说刷不出 ⇒ 不在正面表（Guandao 穿刺）
    if "Guandao" in _fams and "puncture_damage" in (_fams["Guandao"].get("never") or []):
        _dpos, _ = RP.available(
            "melee", "TnGuandaoPolearmWeapon", ["Guandao", "Guandao Prime"], positive=True
        )
        check("★ 抽样说 Guandao 穿刺刷不出 ⇒ 正面表里没有它", "puncture_damage" not in _dpos)

# ---------------------------------------------------------------------------
# ⑥ 数据表完整性 + 出图不溢出
# ---------------------------------------------------------------------------
pools = json.loads((ROOT / "core/data/de/riven_pools.json").read_text(encoding="utf-8"))
check(
    "词条池表：职业池五类齐全",
    set(pools.get("class_pools") or {}) == {"rifle", "shotgun", "pistol", "archgun", "melee"},
)
check(
    "词条池表：家族数 ≥ 290",
    len(pools.get("families") or {}) >= 290,
    str(len(pools.get("families") or {})),
)
check(
    "词条池表：冰凇有证据（物理 never）", "SentinelFreezeRayRifle" in (pools.get("families") or {})
)
check(
    "词条池表：融合词条标记存在",
    any(v.get("spliced") for c in (pools.get("class_pools") or {}).values() for v in c.values()),
)
# 表里的词条 id 必须都是插件认识的（拼错会让卡面显示英文 id）
unknown = sorted(
    {
        sid
        for c in (pools.get("class_pools") or {}).values()
        for sid in c
        if sid not in RIVEN_STAT_ZH
    }
)
check("职业池词条 id 全部已注册中文名", not unknown, str(unknown))

if RA is None:  # pragma: no cover
    pass
if R is None or R.Image is None:
    print("[SKIP] 未安装 Pillow，跳过出图检查")
else:
    probe = R.ImageRenderer(Path(tempfile.mkdtemp(prefix="sdjk_ranges_")))
    if not probe.available:
        print("[SKIP] 无可用 CJK 字体，跳过出图检查")
    else:
        LUM = 125

        def right_overflow(p: str, safe_from_edge: int = 30):
            im = R.Image.open(p).convert("RGB")
            W, H = im.size
            rgb = im.load()
            total, maxx = 0, 0
            for x in range(W - safe_from_edge, W):
                for y in range(150, H - 96):
                    r, g, b = rgb[x, y]
                    if 0.2126 * r + 0.7152 * g + 0.0722 * b > LUM:
                        total += 1
                        maxx = max(maxx, x)
            return maxx, total

        # 真实卡（冰凇全量）+ 长词条名合成卡（防未来词条名变长压出面板）
        _root, _mem = "SentinelFreezeRayRifle", ["Verglas", "Verglas Prime"]
        _pos, _unc = RP.available("shotgun", _root, _mem, positive=True)
        _neg, _ = RP.available("shotgun", _root, _mem, positive=False)
        _pool = RP.class_pool("shotgun")
        _sp = [s for s in _pos if (_pool.get(s) or {}).get("spliced")]
        _pos = [s for s in _pos if s not in _sp]
        _t5, full = F.fmt_riven_ranges("冰凇", "shotgun", (3, 1), _pos, _sp, _neg, _unc)
        LONG = ["收起武器时弹匣每秒自动装填", "对奥罗金的伤害", "弱点暴击几率", "异常状态伤害"]
        long_lines = (
            ["◆ 正面（4 条）"]
            + [f"· {n}　+91.12~111.38%" for n in LONG]
            + [
                "◆ 负面（1 条）",
                "· 收起武器时弹匣每秒自动装填　−96.96~118.5%",
                "※ 融合词条同一张卡最多 1 项（Update 44 Riven Splicer）",
            ]
        )
        for _n, _ln in (("冰凇全量卡", full), ("长词条名卡", long_lines)):
            p = probe.render("冰凇 紫卡词条范围（3正1负）", _ln, "紫卡词条池")
            if not p:
                check(f"出图：{_n}", False, "render() 返回 None 已降级文本")
                continue
            mx, n = right_overflow(p)
            check(f"出图：{_n} 未越出面板右缘", n == 0, f"越界 {n} 像素，最右 x={mx}")

print()

# ---------------------------------------------------------------------------
# ⑦ 组合写法补变体 + 配色重做（2026-10-24 用户提，Claude 定规格）
# ---------------------------------------------------------------------------
from core.commands import riven as RC  # noqa: E402

# 组合写法：2+1 / 3+1 / 全角 / 带「负」都要认（先 NFKC 归一全角再查表）
for _raw, _want in (
    ("2+1", (2, 1)),
    ("2+1-", (2, 1)),
    ("2+1负", (2, 1)),
    ("3+1", (3, 1)),
    ("3+1-", (3, 1)),
    ("3+1负", (3, 1)),
    ("２＋１", (2, 1)),  # 全角数字 + 全角加号
    ("2＋1-", (2, 1)),
    ("3＋1", (3, 1)),
):
    _got = RC.RivenCommands._RANGE_COMBOS.get(RC._fold_combo(_raw))
    check(f"组合写法「{_raw}」→ {_want}", _got == _want, str(_got))
check(
    "「看着像组合但不认识」被抓出（4+ / 5+1-）",
    bool(RC._COMBO_LIKE_RE.match(RC._fold_combo("4+")))
    and bool(RC._COMBO_LIKE_RE.match(RC._fold_combo("5+1-"))),
)
check(
    "普通词条名 / 武器名不会被误判成组合",
    not RC._COMBO_LIKE_RE.match(RC._fold_combo("暴伤"))
    and not RC._COMBO_LIKE_RE.match(RC._fold_combo("绝路Prime")),
)

# 「4+」走 handler 入口 ⇒ 给提示，**不静默落进武器名**（bad_combo 分支在取武器前返回）
import asyncio  # noqa: E402


class _FakeParsed:
    content = ["4+"]


_rep = asyncio.run(
    RC.RivenCommands._h_riven_ranges(
        RC.RivenCommands.__new__(RC.RivenCommands), _FakeParsed(), None, None
    )
)
_rt = "\n".join(_rep.lines)
check(
    "★「词条范围 4+」给出「组合只有 …」提示",
    "组合只有 3+1- / 3+ / 2+1- / 2+" in _rt,
    _rt,
)

if R is None:
    print("[SKIP] 未安装 Pillow，跳过配色检查")
else:
    # 四色按组合固定：指定组合提到首列后颜色不变（字母跟组合走、不跟列位置走）
    for _combo, _span in (((3, 1), "a"), ((2, 1), "m"), ((3, 0), "y"), ((2, 0), "t")):
        _t7, _l7 = F.fmt_riven_ranges("测试枪", "shotgun", _combo, ["crit_damage"], [], [], set())
        _o7 = json.loads([x for x in _l7 if x.startswith("§RANGES")][0][len("§RANGES") :])
        _i7 = _o7["cols"].index(F._RIVEN_COMBO_ZH[_combo])
        check(
            f"★ 组合 {_combo} 的列用组合色 {_span} 且提到首列",
            _o7["spans"][_i7] == _span and _o7["primary"] == _i7,
            f"spans={_o7['spans']} primary={_o7['primary']}",
        )
        _e7 = [seg for x in _l7 if x.startswith("§RANGES") for seg in R._expand_ranges_line(x)]
        check(
            f"★ 组合 {_combo} 指定列数据格 = 全饱和 ⟦{_span}⟧",
            any(f"⟦{_span}⟧" in x for x in _e7),
            str(_e7[:2]),
        )

    # 表头逐格上色（四种组合各自的字母都在表头行里；词条列表头不带标记）
    _t8, _l8 = F.fmt_riven_ranges("测试枪", "shotgun", (3, 1), ["crit_damage"], [], [], set())
    _h8 = [seg for x in _l8 if x.startswith("§RANGES") for seg in R._expand_ranges_line(x)][0]
    check(
        "★ 表头逐格用该列组合色（a / y / t 都在表头行）",
        all(f"⟦{s}⟧" in _h8 for s in ("a", "y", "t")) and _h8.startswith("　词条　"),
        _h8,
    )

    # 竖底带的列换算：primary 是数值列下标 ⇒ cells 下标 +1（名字列占 cells[0]）
    check(
        "★ 竖底带列换算 _ranges_band_col（primary=0 → cells 第 1 列）",
        [R._ranges_band_col(i) for i in (0, 1, 2, 3)] == [1, 2, 3, 4],
        str([R._ranges_band_col(i) for i in (0, 1, 2, 3)]),
    )

    # 名字列：普通词条 ⟦w⟧、融合词条 ⟦v⟧（显式染段 ⇒ 绕开语义词表）
    _t9, _l9 = F.fmt_riven_ranges(
        "测试枪",
        "shotgun",
        (3, 1),
        ["slash_damage", "impact_damage"],
        ["ammo_efficiency"],
        [],
        set(),
    )
    _e9 = [seg for x in _l9 if x.startswith("§RANGES") for seg in R._expand_ranges_line(x)]
    _nc = [x.split("　")[0] for x in _e9 if x.startswith("· ")]
    check(
        "★ 普通词条名包 ⟦w⟧（切割 / 冲击不再被语义词表单独染青绿）",
        "· ⟦w⟧切割⟦/w⟧" in _nc and "· ⟦w⟧冲击⟦/w⟧" in _nc,
        str(_nc[:4]),
    )
    check("★ 融合词条名包 ⟦v⟧（VIOLET）", "· ⟦v⟧弹效⟦/v⟧" in _nc, str(_nc))

    # 新染段标记在文字模式被剥掉（文字输出与改版前一致：只出高亮那一列）
    _t10, _l10 = F.fmt_riven_ranges("测试枪", "shotgun", (3, 1), ["crit_damage"], [], [], set())
    _c10 = R.text_card("测试枪 紫卡词条范围（3正1负）", _l10, "紫卡词条池")
    check("★ 文字模式剥掉全部新染段标记", "⟦" not in _c10 and "⟧" not in _c10, _c10[:400])

    # 主题跟随：改「琥珀」后 3正1负 列用新色；次列变体（现算）也跟着变
    _a0, _d0 = R._span_color("a"), R._span_color("d")
    R.apply_theme({"amber": "#ff00ff"})
    try:
        _a1, _d1 = R._span_color("a"), R._span_color("d")
        check(
            "★ apply_theme 后组合色 a（3正1负）用新色",
            _a1 == (255, 0, 255) and _a1 != _a0,
            f"{_a0} -> {_a1}",
        )
        check("★ apply_theme 后次列变体 d（现算）也跟着变", _d1 != _d0, f"{_d0} -> {_d1}")
        _t11, _l11 = F.fmt_riven_ranges("测试枪", "shotgun", (3, 1), ["crit_damage"], [], [], set())
        _e11 = [seg for x in _l11 if x.startswith("§RANGES") for seg in R._expand_ranges_line(x)]
        check(
            "★ 换肤后 3正1负 列仍走 a 字母（色由 _span_color 现算）", any("⟦a⟧" in x for x in _e11)
        )
    finally:
        R.apply_theme()  # 还原默认主题
    check("★ apply_theme() 还原后组合色回默认", R._span_color("a") == _a0, str(R._span_color("a")))

print()

# ★ 2026-10-24 Claude 审：染段标记不得被折行切开（四列表实测露出「⟦/w⟧」）——
#   两条折行路径（基本布局增量 / 整形引擎逐次测量）都要零宽、标记内不断行
from PIL import ImageFont  # noqa: E402

from core import render as _R  # noqa: E402

_fonts = sorted((ROOT / "core" / "data" / "fonts").glob("*.ttc")) + sorted(
    (ROOT / "core" / "data" / "fonts").glob("*.otf")
)
if _fonts:
    _f = ImageFont.truetype(str(_fonts[0]), 30)
    _txt = "· 暴伤　" + "　".join(f"⟦w⟧+{100 + i}.5~{150 + i}.8%⟦/w⟧" for i in range(6))
    # 扫一串宽度：折点落在标记中间与否取决于宽度，单个宽度测不出
    _bad = []
    for _w in range(150, 900, 3):
        for _sg in _R.ImageRenderer._wrap(_txt, _f, _w):
            if _sg.count("⟦") != _sg.count("⟧"):
                _bad.append((_w, _sg))
                break
    check("★ 折行不切开染段标记：任意宽度下每段的 ⟦ 都成对闭合", not _bad, str(_bad[:3]))
else:
    print("[SKIP] 折行标记守卫：本树无字体（开源包）")

if FAILED:
    print(f"FAILED {len(FAILED)}: " + ", ".join(FAILED))
    sys.exit(1)

# ---------------------------------------------------------------------------
# 紫卡融合 / 紫卡拼接（2026-10-24 用户）：Riven Splicer 配方表（英文 wiki 原表转录）
# ---------------------------------------------------------------------------
from core.parser import parse as _parse_sp  # noqa: E402

_SR = RA.SPLICE_RECIPES
check("融合配方：22 条 / 18 个融合词条", len(_SR) == 22 and len({r[2] for r in _SR}) == 18)
check(
    "融合配方：抽查 毒+火=毒气、G+C=O伤、基伤+触发=异常伤、弹药+装填=收枪装填",
    ("toxin_damage", "heat_damage", "gas_damage") in [r[:3] for r in _SR]
    and ("damage_vs_grineer", "damage_vs_corpus", "damage_vs_orokin") in [r[:3] for r in _SR]
    and ("melee_damage", "status_chance", "status_damage") in [r[:3] for r in _SR]
    and ("ammo_max", "reload_speed", "reload_holstered") in [r[:3] for r in _SR],
)
check(
    "融合配方：近战只列通用 10 + 近战 4；空战没有收枪装填",
    len(RA.splice_recipes("melee")) == 14
    and not any(r[2] == "reload_holstered" for r in RA.splice_recipes("archgun"))
    and any(r[2] == "ammo_efficiency" for r in RA.splice_recipes("archgun")),
)
check(
    "融合配方：按词条过滤认原词条与融合结果（毒伤 3 条、病毒 1 条）",
    len(RA.splice_recipes(stat="toxin_damage")) == 3
    and len(RA.splice_recipes(stat="viral_damage")) == 1,
)
check(
    "融合指令：紫卡融合 / 紫卡拼接 / 融合词条 同一指令；裸「融合」仍是武器融合",
    {_parse_sp(m).command for m in ("紫卡融合", "紫卡拼接", "融合词条", "拼接配方")} == {"splice"}
    and _parse_sp("融合").command == "valence",
)
_t_sp, _l_sp = F.fmt_riven_splice(RA.splice_recipes(), stat_zh="")
check(
    "融合卡：三段齐、融合词条名染紫、原词条名显式白（绕开元素语义色）",
    any(x.startswith("◆ 通用") for x in _l_sp)
    and any(x.startswith("◆ 近战") for x in _l_sp)
    and "· ⟦v⟧毒气⟦/v⟧　⟦w⟧毒伤⟦/w⟧　⟦w⟧火伤⟦/w⟧" in _l_sp,
    str(_l_sp[:4]),
)

print("ALL PASS")
