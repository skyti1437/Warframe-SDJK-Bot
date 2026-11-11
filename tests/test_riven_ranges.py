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


check("投射速度展示短名", RIVEN_STAT_ZH["projectile_speed"] == "投射")


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

# 2026-10-25 起按 DE 规则（不再用 wfsim 逐家族证据）：
#   弹道速度看 PROJECTILE 兼容标签；物理三项按占比 ≤25% 删，我方抽样见过的照列。
opos, oneg, _ = avail("pistol", "CrpSentExperimentPistol", ["Ocucor"])
check("★ Ocucor 能出弹道速度（DE 兼容标签含 PROJECTILE）", "projectile_speed" in opos)
# 已知反例（wiki：Ocucor 0% 切割也能出）：规则会删；2026-10-26 物理抽样（ZCode，n=100，三项都见过）
# 纠正 ⇒ 实证压过规则，三项照列
check(
    "Ocucor 物理三项占比 0%，规则会删，但我方抽样见过 ⇒ 照列（实证压过规则）",
    {"impact_damage", "puncture_damage", "slash_damage"} <= opos,
)

bpos, bneg, bunc = avail("shotgun", "FullAutoShotgun", ["Boar", "Boar Prime"])
check("★ Boar 不出弹道速度（DE 兼容标签无 PROJECTILE）", "projectile_speed" not in bpos)
check("★ Boar 能出变焦（44.0.3 起霰弹枪可出，DE 官方模板已含）", "zoom" in bpos and "zoom" in bneg)

ppos, _, _ = avail("rifle", "ZarimanSemiAutoRifle", ["Phenmor"])
check("★ Phenmor 不出冲击（占比 ≤25%）", "impact_damage" not in ppos)
check("Phenmor 能出切割（占比 80.9%）", "slash_damage" in ppos)

# 守护武器按 DE NotSentinel：守护近战不出连击类 / 滑行暴击 / 处决伤害
_spos, _, _ = avail("melee", "SentGlaiveWeapon", ["Deconstructor", "Deconstructor Prime"])
if _spos:
    check(
        "★ 守护近战（Deconstructor）不出连击时间 / 滑暴 / 处决伤（DE NotSentinel）",
        not ({"combo_duration", "slide_crit", "finisher_damage"} & set(_spos)),
        str(sorted(_spos)),
    )
check(
    "有伤害数据的武器：未确认集合为空（物理按规则直接删）",
    RP.unconfirmed_for("shotgun", "FullAutoShotgun", ["Boar", "Boar Prime"]) == set(),
)

# ★ 2026-11-10：伤害计算器武器表查不到的武器，物理占比改用生成器补的 phys（DE 导出 / wiki 组合武器模块）；
#   都没有才标「?」照列（原先直接三项全列、也不标 ?：猎犬武器 Akaten 列出了它根本没有的冲击）
_r, _m = RP.family_members("Akaten")
kpos, kneg, kunc = avail("melee", _r, _m)
check(
    "★ Akaten（猎犬武器，穿刺 150 + 切割 150）不出冲击",
    "impact_damage" not in kpos and "impact_damage" not in kneg,
)
check("Akaten 能出穿刺 / 切割", {"puncture_damage", "slash_damage"} <= kpos)
check(
    "★ Akaten 按守护武器排除连击 / 滑暴 / 处决 / 重击效率",
    not (
        {
            "combo_duration",
            "initial_combo",
            "slide_crit",
            "finisher_damage",
            "heavy_attack_efficiency",
        }
        & kpos
    ),
)
check("Akaten 有伤害数据 ⇒ 物理不标 ?", not (set(RP.PHYSICAL_STATS) & kunc))
# Zaw：组装后的伤害比例不能套 25% 规则（抽样 Sepfahn 冲击 10% / 穿刺 25%、Plague Kripath 冲击 14% / 切割 23%，
#   三种物理词条都见过）⇒ 没抽样的打击部件物理三项照列标 ?，抽样见过的不标 ?
_r, _m = RP.family_members("Balla")
zpos, _, zunc = avail("melee", _r, _m)
check(
    "★ Zaw 打击部件 Balla：物理三项照列并标 ?",
    set(RP.PHYSICAL_STATS) <= zpos and set(RP.PHYSICAL_STATS) <= zunc,
)
_r, _m = RP.family_members("Sepfahn")
spos, _, sunc = avail("melee", _r, _m)
check(
    "★ Zaw 打击部件 Sepfahn：抽样三种都见过 ⇒ 照列且不标 ?",
    set(RP.PHYSICAL_STATS) <= spos and not (set(RP.PHYSICAL_STATS) & sunc),
)
_r, _m = RP.family_members("Catchmoon")
cpos, _, cunc = avail("pistol", _r, _m)
check(
    "★ 组合枪枪膛 Catchmoon（伤害构成未知）物理三项照列并标 ?",
    set(RP.PHYSICAL_STATS) <= cpos and set(RP.PHYSICAL_STATS) <= cunc,
)

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
    _miss = [
        (v.get("survey") or {}).get("family")
        for v in _pf.values()
        if (v.get("survey") or {}).get("source") == "local"
        and (v.get("survey") or {}).get("family") not in _fams
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
        # 2026-10-25 改口径（Astra 评估）：挂单抽样「没见过」≠ 刷不出 ⇒ 列出并标「?」
        _dpos2, _dunc = RP.available(
            "melee", "TnGuandaoPolearmWeapon", ["Guandao", "Guandao Prime"], positive=True
        )
        # 2026-10-25 物理按规则删：Guandao 穿刺占比 ≤25%，抽样也没见过 ⇒ 规则与实证一致，不列
        check(
            "Guandao 穿刺：规则（≤25%）与抽样（没见过）一致 ⇒ 不列",
            "puncture_damage" not in _dpos2,
        )

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
    "词条池表：冰凇有家族属性（守护武器）",
    (pools.get("families") or {}).get("SentinelFreezeRayRifle", {}).get("sentinel") is True,
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


# ---------------------------------------------------------------------------
# 融合预览计算：仅消费输入两条的归一化随机数，保留卡面舍入不确定性。
# ---------------------------------------------------------------------------
def _sp_value(sid, roll=1.0, *, cls="rifle", counts=(3, 1), negative=False):
    pf, nf = RA.FACTOR[counts]
    return RA._base_value(sid, cls) * 1.2 * (nf if negative else pf) * roll


def _sp_preview(pos, neg=(), *, cls="rifle", counts=None, **kwargs):
    counts = counts or (len(pos), len(neg))
    return RA.splice_preview(
        [(sid, _sp_value(sid, roll, cls=cls, counts=counts)) for sid, roll in pos],
        [(sid, _sp_value(sid, roll, cls=cls, counts=counts, negative=True)) for sid, roll in neg],
        cls,
        counts=counts,
        pool=RP.class_pool(cls),
        **kwargs,
    )


def _sp_recipe(report, output, *, missing=False, inputs=None):
    return next(
        r
        for r in report["missing_one" if missing else "direct"]
        if r["output"] == output and (inputs is None or r["inputs"] == inputs)
    )


_sp = _sp_preview(
    [("melee_damage", 0.92), ("status_chance", 1.04), ("crit_damage", 1.1)],
    [("reload_speed", 1.1)],
)
_sp_p = _sp_recipe(_sp, "status_damage")["prediction"]
check(
    "融合预览：不同基值先归一化，只取消耗两条（无关高卷不参与）",
    _sp["status"] == "ready"
    and abs(_sp_p["value"] - 105.3) < 1e-9
    and abs(_sp_p["position_pct"] - 70.0) < 1e-9
    and _sp_p["value_min"] < _sp_p["value"] < _sp_p["value_max"],
    str(_sp_p),
)
_sp = _sp_preview(
    [("ammo_max", 0.92), ("crit_damage", 1.0), ("crit_chance", 1.0)],
    [("recoil", 1.08)],
)
_sp_r = _sp_recipe(_sp, "ammo_efficiency")
check(
    "融合预览：负面后坐力按负槽magnitude反推，不做1-U反转",
    abs(_sp_r["prediction"]["value"] - 54.675) < 1e-9
    and _sp_r["present"][0]["negative"]
    and abs(_sp_r["present"][0]["roll"] - 1.08) < 1e-9,
    str(_sp_r),
)
_sp_f = _sp_preview([("damage_vs_grineer", 0.96), ("damage_vs_corpus", 1.04), ("crit_damage", 1.0)])
check(
    "融合预览：派系保持内部百分数magnitude，容差复用派系0.5",
    abs(_sp_recipe(_sp_f, "damage_vs_orokin")["prediction"]["value"] - 42.12) < 1e-9
    and _sp_recipe(_sp_f, "damage_vs_orokin")["prediction"]["value_max"]
    - _sp_recipe(_sp_f, "damage_vs_orokin")["prediction"]["value_min"]
    > 0.9,
)
for _sp_counts in ((2, 0), (3, 0), (2, 1), (3, 1)):
    _sp_pos = [("toxin_damage", 0.98), ("heat_damage", 1.02)]
    if _sp_counts[0] == 3:
        _sp_pos.append(("crit_damage", 1.1))
    _sp_neg = [("reload_speed", 1.0)] if _sp_counts[1] else []
    _sp_card = _sp_preview(_sp_pos, _sp_neg)
    check(
        f"融合预览：原布局{_sp_counts}使用原正槽系数",
        _sp_card["counts"] == list(_sp_counts)
        and abs(
            _sp_recipe(_sp_card, "gas_damage")["prediction"]["value"]
            - _sp_value("gas_damage", 1.02, counts=_sp_counts)
        )
        < 1e-9,
    )
_sp_multi = _sp_preview([("crit_chance", 1.0), ("zoom", 1.0), ("multishot", 1.0)])
check(
    "融合预览：同结果不同输入配方不去重",
    sum(r["output"] == "weakpoint_crit_chance" for r in _sp_multi["direct"]) == 2,
)
_sp_cond = _sp_preview(
    [("melee_damage", 0.92), ("crit_damage", 1.1), ("crit_chance", 1.1)],
    [("reload_speed", 1.1)],
    allowed_missing={"status_chance": {"positive", "negative"}},
)
_sp_cr = _sp_recipe(_sp_cond, "status_damage", missing=True)
_sp_cp = _sp_cr["prediction"]
check(
    "融合预览：缺一项仅给条件区间，下界取保留输入而非整卡最高卷",
    _sp_cp["value"] is None
    and _sp_cp["position_pct"] is None
    and abs(_sp_cp["value_min"] - (101.25 * 0.92 - 0.05 * 90 / 165)) < 1e-9
    and abs(_sp_cp["value_max"] - 111.375) < 1e-9
    and _sp_cr["missing_polarities"] == ["positive", "negative"],
    str(_sp_cr),
)
_sp_negknown = _sp_preview(
    [("crit_chance", 1.0), ("crit_damage", 1.0), ("reload_speed", 1.0)],
    [("ammo_max", 1.08)],
)
check(
    "融合预览：保留输入本身占负槽时，缺项只能正面",
    _sp_recipe(_sp_negknown, "ammo_efficiency", missing=True, inputs=["recoil", "ammo_max"])[
        "missing_polarities"
    ]
    == ["positive"],
)
_sp_noneg = _sp_preview([("melee_damage", 1.0), ("crit_damage", 1.0)])
check(
    "融合预览：原布局无负槽时缺项不可为负；两缺项不列",
    _sp_recipe(_sp_noneg, "status_damage", missing=True)["missing_polarities"] == ["positive"]
    and all(len(r["missing"]) == 1 for r in _sp_noneg["missing_one"]),
)
_sp_melee = RA.splice_preview(
    [
        ("fire_rate", _sp_value("attack_speed", 0.98, cls="melee", counts=(2, 0))),
        ("range", _sp_value("range", 1.02, cls="melee", counts=(2, 0))),
    ],
    [],
    "melee",
    pool=RP.class_pool("melee"),
)
check(
    "融合预览：近战射速别名归一攻速，招架使用81基值",
    abs(_sp_recipe(_sp_melee, "melee_parry_angle")["prediction"]["value"] - 98.15256) < 1e-9,
)
_sp_valid = [("toxin_damage", 106.92), ("heat_damage", 106.92)]
for _sp_kwargs, _sp_reason in (
    ({"hidden": 1}, "hidden_stats"),
    ({"incomplete": True}, "incomplete_stats"),
    ({"blocked_reason": "legacy_values"}, "legacy_values"),
):
    _sp_block = RA.splice_preview(_sp_valid, [], "rifle", pool=RP.class_pool("rifle"), **_sp_kwargs)
    check(
        f"融合预览：阻断{_sp_reason}仍保留元数据",
        _sp_reason in _sp_block["reasons"]
        and not _sp_block["direct"]
        and not _sp_block["missing_one"]
        and _sp_block["counts"] == [2, 0]
        and _sp_block["disposition"] == 1.2,
    )
_sp_existing = RA.splice_preview([("ammo_efficiency", 1)], [], "melee", hidden=1)
check(
    "融合预览：全局融合结果扫描先于职业/隐藏验证",
    _sp_existing["status"] == "already_spliced"
    and _sp_existing["existing_spliced"] == ["ammo_efficiency"],
)
for _sp_rows, _sp_kwargs, _sp_reason in (
    ([("toxin_damage", 106.92)] * 2, {}, "duplicate_stat"),
    ([("unknown_stat", 1), ("heat_damage", 106.92)], {}, "unknown_stat"),
    (_sp_valid, {"counts": (3, 0)}, "counts_mismatch"),
    (_sp_valid, {"counts": (1, 0)}, "invalid_counts"),
    (_sp_valid, {"disposition": float("nan")}, "invalid_disposition"),
    ([("toxin_damage", 200), ("heat_damage", 106.92)], {}, "out_of_range"),
):
    _sp_bad = RA.splice_preview(_sp_rows, [], "rifle", pool=RP.class_pool("rifle"), **_sp_kwargs)
    check(f"融合预览：拦截{_sp_reason}", _sp_bad["reasons"] == [_sp_reason])
    json.dumps(_sp_bad, allow_nan=False)
for _sp_badvalue in (float("nan"), float("inf"), -float("inf"), 0, -1):
    _sp_bad = RA.splice_preview(
        [("toxin_damage", _sp_badvalue), ("heat_damage", 106.92)],
        [],
        "rifle",
        pool=RP.class_pool("rifle"),
    )
    check(f"融合预览：拦截非法magnitude {_sp_badvalue}", _sp_bad["reasons"] == ["invalid_value"])
    json.dumps(_sp_bad, allow_nan=False)
_sp_pool = RP.class_pool("rifle")
_sp_pool["toxin_damage"]["bonus"] = False
check(
    "融合预览：职业池正负许可闸门，缺少池不猜",
    RA.splice_preview(_sp_valid, [], "rifle", pool=_sp_pool)["reasons"] == ["stat_not_allowed"]
    and RA.splice_preview(_sp_valid, [], "rifle")["reasons"] == ["pool_unavailable"],
)
_sp_edge = RA.splice_preview(
    [("toxin_damage", 106.92 * 1.1 + 0.049), ("heat_damage", 106.92)],
    [],
    "rifle",
    pool=RP.class_pool("rifle"),
)
check(
    "融合预览：舍入带边界内接受并截断估计U，边界外拒绝",
    _sp_edge["status"] == "ready"
    and _sp_recipe(_sp_edge, "gas_damage")["prediction"]["value"] == 106.92 * 1.1
    and RA.splice_preview(
        [("toxin_damage", 106.92 * 1.1 + 0.051), ("heat_damage", 106.92)],
        [],
        "rifle",
        pool=RP.class_pool("rifle"),
    )["reasons"]
    == ["out_of_range"],
)
check(
    "融合预览：missing许可交集为空时不列候选，JSON可序列化",
    not _sp_preview([("melee_damage", 1.0), ("crit_damage", 1.0)], allowed_missing={})[
        "missing_one"
    ]
    and bool(json.dumps(_sp_cond, allow_nan=False)),
)

check(
    "融合预览：缺失职业基值和未知职业不预测",
    RA.splice_preview(
        [("range", 1), ("heat_damage", 106.92)], [], "rifle", pool=RP.class_pool("rifle")
    )["reasons"]
    == ["missing_base"]
    and RA.splice_preview(_sp_valid, [], "unknown", pool={})["reasons"] == ["unsupported_class"],
)
_sp_denied_negative = RA.splice_preview(
    [("crit_damage", 135), ("crit_chance", 168.73875), ("reload_speed", 56.25)],
    [("heat_damage", 81)],
    "rifle",
    pool=RP.class_pool("rifle"),
)
check(
    "融合预览：职业负面许可拒绝元素负槽",
    _sp_denied_negative["reasons"] == ["stat_not_allowed"],
)
_sp_input = list(_sp_valid)
_sp_pool_copy = RP.class_pool("rifle")
_sp_pool_before = json.dumps(_sp_pool_copy, sort_keys=True)
RA.splice_preview(_sp_input, [], "rifle", pool=_sp_pool_copy)
check(
    "融合预览：不修改输入词条与职业池快照",
    _sp_input == _sp_valid and json.dumps(_sp_pool_copy, sort_keys=True) == _sp_pool_before,
)

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
    and "· ⟦v⟧毒气伤害⟦/v⟧　⟦w⟧毒素伤害⟦/w⟧　⟦w⟧火焰伤害⟦/w⟧" in _l_sp,
    str(_l_sp[:4]),
)


# ---------------------------------------------------------------------------
# 2026-10-25 对照游戏数据包（riven-roll 导出）：近战融合池补 9 个、派系乘数显示舍入容差
# ---------------------------------------------------------------------------
_mp = RP.class_pool("melee")
check(
    "★ 近战职业池含 9 个融合词条（毒气/腐蚀/病毒/辐射/爆炸/磁力/O/S/T），且只出正面",
    all(
        (_mp.get(s) or {}).get("spliced") and (_mp.get(s) or {}).get("malus") is False
        for s in (
            "gas_damage",
            "viral_damage",
            "magnetic_damage",
            "damage_vs_orokin",
            "damage_vs_techrot",
        )
    ),
)
_pos_m, _ = RP.available("melee", "", [], positive=True)
_neg_m, _ = RP.available("melee", "", [], positive=False)
check(
    "近战可列正面含毒气 / O伤；负面不含",
    "gas_damage" in _pos_m and "damage_vs_orokin" in _pos_m and "gas_damage" not in _neg_m,
)
check(
    "★ 派系乘数卡面 x1.56（3正1负 上限 55.69%）不再误判不吻合；x1.58 仍不吻合",
    RA.disp_feasible([("damage_vs_grineer", 56.0)], [], "rifle", 1.2, counts=(3, 1))
    and not RA.disp_feasible([("damage_vs_grineer", 58.0)], [], "rifle", 1.2, counts=(3, 1)),
)

check(
    "★ 近战「近战伤害」不出负面、枪械「基伤」仍可负（数据包 + WM 抽样 10-25）",
    "melee_damage" not in RP.available("melee", "", [], positive=False)[0]
    and "melee_damage" in RP.available("rifle", "", [], positive=False)[0],
)

# 2026-10-25 维护者定：「基础伤害有个『近战伤害』」「最好按卡面全称」—— 同一词条 id 按段写卡面全称
check(
    "★ 融合卡「伤害」分写：通用段「伤害 / 近战伤害」、枪械段「伤害」、近战段「近战伤害」",
    "· ⟦v⟧异常状态伤害⟦/v⟧　⟦w⟧伤害 / 近战伤害⟦/w⟧　⟦w⟧触发几率⟦/w⟧" in _l_sp
    and "· ⟦v⟧弱点伤害⟦/v⟧　⟦w⟧伤害⟦/w⟧　⟦w⟧变焦⟦/w⟧" in _l_sp
    and "· ⟦v⟧震地攻击伤害⟦/v⟧　⟦w⟧近战伤害⟦/w⟧　⟦w⟧攻击速度⟦/w⟧" in _l_sp,
    str(_l_sp),
)
_t_m, _l_m = F.fmt_riven_splice(RA.splice_recipes("melee"), scope="近战", cls="melee")
check(
    "按近战筛选：通用段的异常状态伤害也写「近战伤害」",
    "· ⟦v⟧异常状态伤害⟦/v⟧　⟦w⟧近战伤害⟦/w⟧　⟦w⟧触发几率⟦/w⟧" in _l_m,
    str(_l_m),
)

# 2026-10-25 招架角度定案 81（wiki 现行 Spliced Values 81% + 游戏数据包），卡面不带 %
_pa = RA.stat_range("melee_parry_angle", "melee", 1.2, 3, 1)
check(
    "★ 招架角度基值 81：3正1负 区间 82.01~100.24，显示不带 %",
    _pa == (82.01, 100.24) and RA.fmt_value("melee_parry_angle", 91.1) == "91.1",
    f"{_pa} {RA.fmt_value('melee_parry_angle', 91.1)}",
)

# 2026-10-25（Astra 评估）：抽样没见过的词条 ⇒ 卡面注脚写明抽样时间与样本量
_t_b, _l_b = F.fmt_riven_ranges(
    "测试",
    "shotgun",
    (3, 1),
    ["zoom", "multishot"],
    [],
    [],
    {"zoom"},
    survey={"unseen": {"zoom"}, "surveyed": "2026-10-23", "n": 100},
)
check(
    "★ 抽样没见过 ⇒ 注脚写明抽样日期与条数",
    any("变焦" in x and "2026-10-23" in x and "100 条" in x for x in _l_b),
    str([x for x in _l_b if x.startswith("※")]),
)

# 2026-10-26（P1 部署实测）：**家族表里还没有的新武器**也必须查得到抽样证据 ——
#   `matching.family_key` 对未知武器名返回**小写**（`aksondol`），而生成器按
#   `name:<原大小写>` 存键（`name:Aksondol`）⇒ 精确查键落空、抽样注脚静默消失。
#   这条钉住「大小写无关」的兜底（否则 Aksondol / Nunchasa 的实证纠偏整层失效）。
_entry = RP.family_entry("aksondol")
check(
    "★ 新武器抽样证据可查（小写根 → name:Aksondol）",
    bool(_entry.get("survey")),
    str(_entry)[:140],
)
_unseen_new, _sv_new = RP.survey_unseen("aksondol")
check(
    "★ 新武器抽样「没见过」集合非空（实证层真的接上了）",
    bool(_unseen_new) and bool(_sv_new.get("surveyed")),
    f"unseen={sorted(_unseen_new)} sv={_sv_new}",
)

# 2026-10-31：紫卡分析「融合建议」只排版后端 RA.splice_preview 的结果（数值不重算）
_sp_rows = [("melee_damage", 185.6), ("status_chance", 96.2), ("crit_damage", 148.5)]


def _sp_pred(**kw):
    base = dict(
        value=101.2,
        value_min=101.2,
        value_max=101.3,
        range_min=91.125,
        range_max=111.375,
        position_pct=74.9,
        position_min_pct=74.8,
        position_max_pct=75.1,
        deviation_pct=5.0,
        deviation_min_pct=4.96,
        deviation_max_pct=5.02,
        approximate=True,
    )
    base.update(kw)
    return base


_sp_ready = {
    "status": "ready",
    "direct": [
        {
            "inputs": ["melee_damage", "status_chance"],
            "output": "status_damage",
            "present": [
                {"stat_id": "melee_damage", "negative": False},
                {"stat_id": "status_chance", "negative": False},
            ],
            "missing": [],
            "prediction": _sp_pred(),
        }
    ],
    "missing_one": [
        {
            "inputs": ["melee_damage", "multishot"],
            "output": "weakpoint_damage",
            "present": [{"stat_id": "zoom", "negative": True}],
            "missing": ["multishot"],
            "missing_polarities": ["positive", "negative"],
            "prediction": _sp_pred(
                value=None,
                value_min=253.1,
                range_min=227.8,
                range_max=278.4,
                position_pct=None,
                position_min_pct=50.0,
                position_max_pct=100.0,
                deviation_pct=None,
                deviation_min_pct=0.0,
                deviation_max_pct=10.0,
            ),
        },
        {
            "inputs": ["crit_damage", "damage_vs_grineer"],
            "output": "damage_vs_orokin",
            "present": [{"stat_id": "crit_damage", "negative": False}],
            "missing": ["damage_vs_grineer"],
            "missing_polarities": ["negative"],
            "prediction": _sp_pred(
                value=None,
                value_min=50.0,
                range_min=40.5,
                range_max=49.5 * 1.1,
                position_pct=None,
                position_min_pct=80.0,
                position_max_pct=100.0,
                deviation_pct=None,
                deviation_min_pct=-0.6,
                deviation_max_pct=10.0,
            ),
        },
    ],
}
_t_s, _l_s = F.fmt_riven_analysis("绝路", 1.2, "rifle", _sp_rows, [("zoom", 54)], splice=_sp_ready)
check(
    "★ 融合建议：直接融合写「约」，距中读 deviation_pct（5 ⇒ +5%）、区间位读 position_pct，各自保留",
    "· ⟦v⟧异常状态伤害⟦/v⟧ 约+101.2%　+91.1~111.4%　+5%　75%" in _l_s
    and "　消耗 ▲基伤、▲触发" in _l_s,
    str(_l_s),
)
check(
    "★ 融合建议：差一条写「起」、距中与区间位给条件范围，缺项单独一行",
    "· ⟦v⟧弱点伤害⟦/v⟧ +253.1%起　+227.8~278.4%　+0~10%　50~100%" in _l_s
    and "　消耗 ▼变焦、缺多重（正负皆可）" in _l_s
    and not any("约+0" in x for x in _l_s),
    str(_l_s),
)
check(
    "★ 融合建议：派系结果按净倍率显示；距中下界为负时两头都带符号",
    "· ⟦v⟧对奥罗金的伤害⟦/v⟧ ×1.50起　×1.41~1.54　-0.6~+10%　80~100%" in _l_s,
    str(_l_s),
)
check(
    "★ 融合建议：表头四列与上方词条表同构（消耗进续行，手机不挤第五列）",
    "　融合词条　理论区间　距中　区间位" in _l_s,
)
check(
    "★ 融合建议：段落在注脚之前，公式说明进注脚",
    _l_s.index("◆ 融合建议（预计）") < min(i for i, x in enumerate(_l_s) if x.startswith("※"))
    and any(x.startswith("※ 融合建议按英文 wiki") for x in _l_s),
)
_null = {
    "status": "ready",
    "direct": [
        {
            "output": "status_damage",
            "present": [{"stat_id": "melee_damage", "negative": False}],
            "missing": [],
            "prediction": _sp_pred(deviation_pct=None, position_pct=None),
        }
    ],
    "missing_one": [
        {
            "output": "weakpoint_damage",
            "present": [{"stat_id": "zoom", "negative": True}],
            "missing": ["multishot"],
            "missing_polarities": ["positive"],
            "prediction": _sp_pred(
                value=None, deviation_min_pct=None, position_min_pct=None, position_pct=None
            ),
        }
    ],
}
_, _l_null = F.fmt_riven_analysis("绝路", 1.2, "rifle", _sp_rows, [], splice=_null)
check(
    "★ 融合建议：null 字段写「—」，不当成 0",
    "· ⟦v⟧异常状态伤害⟦/v⟧ 约+101.2%　+91.1~111.4%　—　—" in _l_null
    and "· ⟦v⟧弱点伤害⟦/v⟧ +101.2%起　+91.1~111.4%　—　—" in _l_null,
    str(_l_null),
)
_, _l_as = F.fmt_riven_analysis(
    "绝路",
    1.2,
    "rifle",
    _sp_rows,
    [],
    splice={"status": "already_spliced", "existing_spliced": ["gas_damage"]},
)
check(
    "★ 已有融合词条只提示一句，也不出续页",
    "◆ 融合建议：已有融合词条（毒气），不能再融合" in _l_as
    and F.fmt_riven_splice_more(
        "绝路", "rifle", {"status": "already_spliced", "existing_spliced": ["gas_damage"]}
    )
    is None,
)
_, _l_inc = F.fmt_riven_analysis(
    "绝路", 1.2, "rifle", _sp_rows, [], splice={"status": "incomplete", "reasons": ["hidden_stats"]}
)
check(
    "★ 阻断状态只给原因，不列配方",
    "◆ 融合建议：暂不判断（有未揭示的「？？？」词条）" in _l_inc
    and "◆ 融合建议（预计）" not in _l_inc,
)
# 9 种不同融合结果：首屏 6 条 + 续页 3 条（当前卡的数值与缺项，不是通用配方表）
_outs = [
    "status_damage",
    "weakpoint_damage",
    "damage_vs_orokin",
    "gas_damage",
    "corrosive_damage",
    "viral_damage",
    "radiation_damage",
    "blast_damage",
    "ammo_efficiency",
]
_many = dict(
    _sp_ready,
    missing_one=[
        dict(
            _sp_ready["missing_one"][0],
            output=o,
            prediction=_sp_pred(
                value=None, value_min=200.0 - k, position_pct=None, deviation_pct=None
            ),
        )
        for k, o in enumerate(_outs[1:])
    ],
)
_, _l_many = F.fmt_riven_analysis(
    "绝路", 1.2, "rifle", _sp_rows, [], splice=_many, splice_limit=F.SPLICE_FIRST_ROWS
)
_more = F.fmt_riven_splice_more("绝路", "rifle", _many)
_, _l_full = F.fmt_riven_analysis("绝路", 1.2, "rifle", _sp_rows, [], splice=_many)
_rows = lambda ls: [x for x in ls if x.startswith("· ⟦v⟧")]  # noqa: E731
check(
    "★ 融合建议首屏 6 条，注脚指向续页",
    len(_rows(_l_many)) == 6
    and "◆ 融合建议（预计，共 9 条）" in _l_many
    and any("其余 3 条见续页" in x for x in _l_many)
    and not any("紫卡融合 武器名" in x for x in _l_many),
    str(_l_many),
)
check(
    "★ 续页列出当前卡剩余预测（含数值、条件范围与缺项），首屏 + 续页 = 全部",
    _more is not None
    and _more[0] == "紫卡分析 · 融合建议续"
    and "◆ 融合建议（预计，第 7–9 条，共 9 条）" in _more[1]
    and _rows(_l_many) + _rows(_more[1]) == _rows(_l_full)
    and sum(1 for x in _more[1] if x.startswith("　消耗 ")) == 3
    and not any("页" in x for x in _l_many + _more[1] if "第" in x),
    str(_more),
)
check(
    "★ 不超过 6 条时不出续页",
    F.fmt_riven_splice_more("绝路", "rifle", _sp_ready) is None,
)


# 去重：同一 output 只留最高（直接融合比 value、差一条比「起」的 value_min，不比 value_max）
def _cand(out, value=None, vmin=None, vmax=300.0, missing=(), present="melee_damage", dev=None):
    return {
        "output": out,
        "present": [{"stat_id": present, "negative": False}],
        "missing": list(missing),
        "missing_polarities": ["positive"] if missing else [],
        "prediction": _sp_pred(
            value=value,
            value_min=vmin if vmin is not None else value,
            value_max=vmax,
            range_min=227.8,
            range_max=278.4,
            deviation_pct=None if value is None else 5.0,
            position_pct=None if value is None else 75.0,
            deviation_min_pct=0.0 if dev is None else dev,
            deviation_max_pct=10.0,
            position_min_pct=50.0,
            position_max_pct=100.0,
        ),
    }


def _dedup(direct, missing):
    rows = F._splice_rows(
        RA, {"status": "ready", "direct": direct, "missing_one": missing}, "rifle"
    )
    return [(r.split("　")[0][2:], u.strip()) for r, u in rows]


check(
    "★ 去重：差一条「起」低于直接融合 ⇒ 留直接融合",
    _dedup(
        [_cand("weakpoint_damage", 250.0)],
        [_cand("weakpoint_damage", vmin=240.0, missing=["multishot"])],
    )
    == [("⟦v⟧弱点伤害⟦/v⟧ 约+250%", "消耗 ▲基伤")],
)
check(
    "★ 去重：差一条「起」高于直接融合 ⇒ 留差一条，四列与缺项都取自它",
    _dedup(
        [_cand("weakpoint_damage", 250.0)],
        [_cand("weakpoint_damage", vmin=260.0, missing=["multishot"])],
    )
    == [("⟦v⟧弱点伤害⟦/v⟧ +260%起", "消耗 ▲基伤、缺多重（需正面）")],
)
check(
    "★ 去重：比「起」的 value_min，不拿理论上限 value_max",
    _dedup(
        [],
        [
            _cand("weakpoint_damage", vmin=255.0, vmax=278.4, missing=["multishot"]),
            _cand("weakpoint_damage", vmin=256.0, vmax=260.0, missing=["zoom"]),
        ],
    )
    == [("⟦v⟧弱点伤害⟦/v⟧ +256%起", "消耗 ▲基伤、缺变焦（需正面）")],
)
check(
    "★ 去重：同值时优先可直接融合",
    _dedup(
        [_cand("weakpoint_damage", 250.0)],
        [_cand("weakpoint_damage", vmin=250.0, missing=["multishot"])],
    )
    == [("⟦v⟧弱点伤害⟦/v⟧ 约+250%", "消耗 ▲基伤")],
)
check(
    "★ 去重：同值同显示的差一条方案合并为「缺 A 或 B」",
    _dedup(
        [],
        [
            _cand("weakpoint_damage", vmin=250.0, missing=["multishot"]),
            _cand("weakpoint_damage", vmin=250.0, missing=["zoom"]),
        ],
    )
    == [("⟦v⟧弱点伤害⟦/v⟧ +250%起", "消耗 ▲基伤、缺多重或变焦（需正面）")],
)
check(
    "★ 去重：同值同显示的直接融合方案合并为「A 或 B」",
    _dedup(
        [_cand("weakpoint_damage", 250.0), _cand("weakpoint_damage", 250.0, present="crit_damage")],
        [],
    )
    == [("⟦v⟧弱点伤害⟦/v⟧ 约+250%", "消耗 ▲基伤 或 ▲暴伤")],
)
check(
    "★ 去重：同值但距中等显示不同 ⇒ 只留一条，不拼出对不上的行",
    _dedup(
        [],
        [
            _cand("weakpoint_damage", vmin=250.0, missing=["multishot"], dev=0.0),
            _cand("weakpoint_damage", vmin=250.0, missing=["zoom"], dev=-1.0),
        ],
    )
    == [("⟦v⟧弱点伤害⟦/v⟧ +250%起", "消耗 ▲基伤、缺多重（需正面）")],
)
check(
    "★ 去重：不同 output 互不影响；直接融合在前，差一条按「起」降序",
    [
        h
        for h, _ in _dedup(
            [_cand("gas_damage", 100.0)],
            [
                _cand("status_damage", vmin=90.0, missing=["zoom"]),
                _cand("weakpoint_damage", vmin=260.0, missing=["zoom"]),
            ],
        )
    ]
    == ["⟦v⟧毒气伤害⟦/v⟧ 约+100%", "⟦v⟧弱点伤害⟦/v⟧ +260%起", "⟦v⟧异常状态伤害⟦/v⟧ +90%起"],
)
_, _l_np = F.fmt_riven_analysis(
    "绝路",
    1.2,
    "rifle",
    _sp_rows,
    [],
    splice=_sp_ready,
    price={"pos": ["melee_damage"], "neg": [], "sample_count": 3, "low": 1, "median": 2},
)
check(
    "★ 注脚精简：融合 + 挂单齐全时只 3 行（各一行），挂单口径仍写明全同 / 一口价 / 中位样本",
    sum(1 for x in _l_np if x.startswith("※")) == 3
    and any("全同" in x and "一口价" in x and "20 条" in x for x in _l_np),
    str([x for x in _l_np if x.startswith("※")]),
)
_, _l_none = F.fmt_riven_analysis("绝路", 1.2, "rifle", _sp_rows, [])
check(
    "★ 没有融合/价格数据时原卡不变",
    not any("融合建议" in x or "同词条挂单" in x for x in _l_none),
)
_R = __import__("core.render", fromlist=["x"])
_md = _R.text_card(_t_s, _l_s)
check(
    "★ 纯文字降级：融合词条去掉染色标记，消耗续行保留",
    "异常状态伤害 约+101.2%" in _md and "⟦" not in _md and "　消耗 ▲基伤、▲触发" in _md,
    _md[:400],
)
_qmd = _R.card_markdown(_t_s, [x.replace("~", "～") for x in _l_full])
check(
    "★ QQ Markdown：区间换全角～后可转，去重后全部 9 种结果与消耗子项都在一条消息里",
    _qmd is not None
    and _qmd.count("    - 消耗 ") == 9
    and all(f"- **{F._SPLICE_FULL_ZH[o]} " in _qmd for o in _outs)
    and "*融合词条 · 理论区间 · 距中 · 区间位*" in _qmd,
    str(_qmd)[:600],
)
check(
    "★ QQ Markdown：原样「~」仍按保护规则退回纯文字（换字只在紫卡分析 handler 里做）",
    _R.card_markdown(_t_s, _l_full) is None,
)


# 距中百分比与区间位置是不同量：+5% 对应区间位 75%。
_sp_dev = _sp_recipe(
    _sp_preview([("melee_damage", 1.0), ("status_chance", 1.05), ("crit_damage", 1.1)]),
    "status_damage",
)["prediction"]
check(
    "融合距中：直接融合+5%与区间位75%区分",
    abs(_sp_dev["deviation_pct"] - 5.0) < 1e-9
    and abs(_sp_dev["position_pct"] - 75.0) < 1e-9
    and _sp_dev["deviation_min_pct"] < 5 < _sp_dev["deviation_max_pct"],
    str(_sp_dev),
)
check(
    "融合距中：负面继承+8%",
    abs(_sp_r["prediction"]["deviation_pct"] - 8.0) < 1e-9,
    str(_sp_r),
)
check(
    "融合距中：缺一条不伪造点估计，保留上下界",
    _sp_cp["deviation_pct"] is None
    and _sp_cp["deviation_min_pct"] < -8
    and abs(_sp_cp["deviation_max_pct"] - 10) < 1e-9,
    str(_sp_cp),
)
check(
    "融合距中：已有融合词条仍停止预测",
    _sp_existing["status"] == "already_spliced"
    and not _sp_existing["direct"]
    and not _sp_existing["missing_one"],
)
check(
    "紫卡分析不再输出旧±11%误传纠错说明",
    not any("±11%" in line and "误传" in line for line in _l_none),
    str(_l_none),
)


# 失败/零样本也必须保留完整正负匹配条件。
for _price_state in ({"sample_count": 0}, {"error": "TimeoutError"}):
    _, _price_lines = F.fmt_riven_analysis(
        "绝路",
        1.2,
        "rifle",
        _sp_rows,
        [("zoom", 54)],
        price={"pos": ["melee_damage", "crit_damage"], "neg": ["zoom"], **_price_state},
    )
    check(
        "严格行情无样本或出错仍显示负面条件：" + str(_price_state),
        any("▲基伤" in line and "▲暴伤" in line and "▼变焦" in line for line in _price_lines)
        and not any("只按正面" in line for line in _price_lines),
        str(_price_lines),
    )


# 终幕·噬轮的武器机制只放大三项物理，不改OCR原读数、不扩展到同系列。
_mechanism = RA.weapon_stat_multipliers({"en": "Coda Motovore", "url_name": "coda_motovore"})
check(
    "机制仅三项物理×6",
    _mechanism == {"impact_damage": 6.0, "puncture_damage": 6.0, "slash_damage": 6.0},
)
for _weapon in ("Motovore", "Coda Pathocyst", "Coda Mire", "Coda Motovore Prime", "Unknown"):
    check("其它武器不继承倍率：" + _weapon, not RA.weapon_stat_multipliers(_weapon))
for _sid in ("impact_damage", "puncture_damage", "slash_damage", "crit_damage", "combo_duration"):
    for _neg in (False, True):
        _base = RA.stat_range(_sid, "melee", 1.2, 3, 1, negative=_neg)
        _scaled = RA.stat_range(
            _sid, "melee", 1.2, 3, 1, negative=_neg, stat_multipliers=_mechanism
        )
        _factor = _mechanism.get(_sid, 1)
        check(
            f"机制区间正负一致：{_sid}/{_neg}",
            all(abs(a * _factor - b) < 0.04 for a, b in zip(_base, _scaled)),
        )
_mpos = [("impact_damage", 830.9), ("puncture_damage", 749.5), ("heavy_attack_efficiency", 82.8)]
_mneg = [("combo_duration", 7.4)]
check(
    "武器机制数据可解释原卡物理数值",
    RA.disp_feasible(_mpos, _mneg, "melee", 1.2, stat_multipliers=_mechanism),
)
check("同一超大物理数值不能套到其他武器", not RA.disp_feasible(_mpos, _mneg, "melee", 1.2))
_iv = RA.disposition_interval(_mpos, _mneg, "melee", stat_multipliers=_mechanism)
check("反解倾向同步使用机制", _iv[0] <= 1.2 <= _iv[1])
_, _mlines = F.fmt_riven_analysis(
    "终幕·噬轮", 1.2, "melee", _mpos, _mneg, stat_multipliers=_mechanism
)
check(
    "展示保留830.9和机制说明",
    any("830.9" in line for line in _mlines) and any("×6" in line for line in _mlines),
)

if FAILED:
    print(f"FAILED {len(FAILED)}: " + ", ".join(FAILED))
    sys.exit(1)

print("ALL PASS")
