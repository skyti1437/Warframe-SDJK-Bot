# -*- coding: utf-8 -*-
"""从 wfsim 抽取武器的「多段攻击」结构（主段 + 范围/集束等额外段）。

数据源：github.com/magenie33/wfsim data/weapons/**/*.yaml 的 attack 块。
每把武器的 attack 可能含：
- damage/crit_*/status_chance/fire_rate   → 主段（直接命中）
- radial                                  → 范围（AoE）段，有自己的
  crit/status、半径、边缘衰减、是否吃多重/爆炸范围 MOD
- cluster                                 → 集束子炸弹（如 Kuva Bramma）：
  count 颗，每颗一次接触命中（cluster.damage）+ 一次爆炸（cluster.radial）
- lingering                               → 残留云团（Pox / Torid…）：只用来给
  同签名的 attacks 段带上 takes_condition_overload（merge_only，不新建段、不进合计）
- charge_seconds                          → 蓄力时间
- forced_procs                            → 强制异常

输出 core/data/weapon_attacks.json（key = 小写 uniqueName，与 weapons_stats /
incarnon_forms 对齐）。顶层只收有额外段或蓄力的**默认形态**（带 internal_name 的条目）；
非默认形态（副开火 / 蓄力 / 未蓄力 / 半自动 / 全自动，及带额外段的灵化）收在记录的
``forms``（键 ``<form>:<yaml_id>``，沿 transform_group 回到母武器，2026-10-10 第三批），
计算器只在用户显式选了该形态时用它整套替换主段与额外段。
「默认形态」一般 = wfsim 的 default_form；MAIN_FORM_OVERRIDE 里的形态组（Nataruk 第四批；弓 /
Drakgoon / Zenith / Kuva Quartakk / Miter 等 b5 批）以我方 weapons_stats 主段对应的条目为准，
wfsim default_form 降为普通形态。有非灵化形态的记录带 main_form（我方主段的形态种类）。

口径（2026-10-10 生成器批，逐条对 wfsim @HEAD）：
- 范围段 / 子炸弹的暴击、暴伤、触发**缺省继承直击段**
  （engine/src/data/weapons/panel.rs:268-271「Each stat falls back to the direct
  part's when unstated」；子炸弹两半走同一个 a_radial，panel.rs:316-346）。
  修前写死 0.0 / 1.0 / 0.0。
- takes_multishot **默认吃多重**（attack.rs:713-731：「MOST AoE TAKES MULTISHOT…
  Default YES」「73 ENTRIES STILL DECLARE false AND ONLY TWO HAVE A SOURCE」）。
  只信 ``SOURCED_NO_MULTISHOT`` 白名单里的 false（每条附 yaml 出处），其余 false
  视为未验证的批量导入，不写进输出（= 吃多重）。
- takes_condition_overload：范围段默认**不吃** CO（attack.rs:704-708），声明 true 的
  原样写出（运行期让该段吃 CO，桶别随武器 co_behavior，pellet.rs:420-428）。
- cluster.count 写进段的 ``count``；子炸弹随母弹（多重 = 更多母弹）——wfsim 的
  ClusterSpec 注释自认「Nothing published states this either way」，按「默认吃多重」处理。

用法：python scripts/build_weapon_attacks.py [wfsim 仓库根或其 data 目录] [输出路径]

许可：只取事实数值（伤害向量/暴击/触发/半径），按我们自己的 schema 重录。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # noqa: BLE001 —— 插件运行本身不需要 yaml，只有这几个
    raise SystemExit(  # 数据构建脚本要；给可操作的提示而不是裸 ImportError
        "本脚本需要 PyYAML：pip install pyyaml（插件运行时并不依赖它，仅构建数据用）"
    )

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "core" / "data" / "weapon_attacks.json"
CATEGORIES = (
    "primary",
    "secondary",
    "melee",
    "sentinel",
    "archgun",
    "archmelee",
    "kitgun",
    "exalted",
)
DAMAGE_KEYS = (
    "impact",
    "puncture",
    "slash",
    "heat",
    "cold",
    "electricity",
    "toxin",
    "blast",
    "radiation",
    "gas",
    "magnetic",
    "viral",
    "corrosive",
    "void",
    "true",
)

# 有出处的「范围段不吃多重」（yaml id → 出处）。只有这些 false 会写进输出；
# 生成时校验它们在 wfsim 里仍声明 false，否则报错（防上游改了而白名单过期）。
SOURCED_NO_MULTISHOT = {
    "burston_incarnon": "burston_incarnon.yaml:144-148 wiki「AoE does not scale off multishot」",
    "burston_prime_incarnon": "burston_prime_incarnon.yaml:141-145 同上",
    "zylok_prime_incarnon": "zylok_prime_incarnon.yaml:75-85 CO 目录行「AoE does not scale off multishot」",
    "zylok_incarnon": "zylok_incarnon.yaml:74-82 同一目录行同时覆盖 Zylok / Zylok Prime",
    "braton_incarnon": "notes.yaml:627-634 fires_once_per_pull（M41 实测：一次扳机一次爆炸）",
    "braton_prime_incarnon": "notes.yaml:627-634 fires_once_per_pull（M41 实测）",
    "braton_vandal_incarnon": "notes.yaml:627-634 fires_once_per_pull",
    "mk1_braton_incarnon": "notes.yaml:627-634 fires_once_per_pull",
}


def data_dir(arg: str | None) -> Path:
    p = Path(arg) if arg else Path.home() / "tmp" / "wfsim"
    return p if (p / "weapons").is_dir() else p / "data"


def wfsim_commit(data: Path) -> str:
    """wfsim 仓库当前 HEAD（取不到就返回空串，_meta 里如实留空）。"""
    try:
        return subprocess.run(
            ["git", "-C", str(data.parent), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except Exception:  # noqa: BLE001
        return ""


def clean_damage(d) -> dict:
    out = {k: float(d.get(k) or 0.0) for k in DAMAGE_KEYS if d.get(k)}
    if out:
        out["total"] = round(sum(out.values()), 4)
    return out


def part_stat(part: dict, direct: dict, key: str, default: float) -> float:
    """段属性缺省继承直击段（panel.rs:268-271）；显式写 0 的保持 0。"""
    if part.get(key) is not None:
        return float(part[key])
    if direct.get(key) is not None:
        return float(direct[key])
    return default


def sourced_no_multishot(rad: dict, yaml_id: str) -> bool:
    """该范围段「不吃多重」是否有出处（白名单 + wfsim 仍声明 false）。"""
    return rad.get("takes_multishot") is False and yaml_id in SOURCED_NO_MULTISHOT


def seg_of(rad: dict, sid: str, name: str, direct: dict, yaml_id: str) -> dict:
    seg = {
        "id": sid,
        "name": name,
        "damage": clean_damage(rad.get("damage") or {}),
        "criticalChance": part_stat(rad, direct, "crit_chance", 0.0),
        "criticalMultiplier": part_stat(rad, direct, "crit_multiplier", 1.0),
        "procChance": part_stat(rad, direct, "status_chance", 0.0),
    }
    for k in ("radius_m", "falloff_start_m", "falloff_reduction"):
        if rad.get(k) is not None:
            seg[k] = float(rad[k])
    if rad.get("takes_blast_radius_mods") is not None:
        seg["takes_blast_radius_mods"] = bool(rad["takes_blast_radius_mods"])
    if sid == "radial" and sourced_no_multishot(rad, yaml_id):
        seg["takes_multishot"] = False
    if rad.get("takes_condition_overload") is True:
        seg["takes_condition_overload"] = True
    return seg


def check_whitelist(ents: dict) -> None:
    for yid, why in SOURCED_NO_MULTISHOT.items():
        rad = ((ents.get(yid) or {}).get("attack") or {}).get("radial") or {}
        if rad.get("takes_multishot") is not False:
            raise SystemExit(f"白名单过期：{yid} 已不再声明 takes_multishot: false（{why}）")


# 我方主段（weapons_stats.json 顶层面板）与 wfsim default_form 不是同一攻击的形态组：
# transform_group → {"main": 与我方主段对应的 wfsim 条目 id（None = wfsim 没有这一条）,
#                    "main_kind": 我方主段的形态种类（缺省 = main 条目的 form）,
#                    "demote_kind": wfsim default_form 降级进 forms 时用的种类（缺省 = 其 form）}。
# main 条目当「默认形态」（CO 桶别 / 顶层 segments·charge_seconds 都取它）；wfsim 的
# default_form 条目降为普通非默认形态（收进 forms，带自己的 co_behavior）。
# 判据：我方主段的伤害 / 暴击 / 暴伤 / 触发与该条目 attack 块逐项一致（b5 批全量筛查脚本，
# 581 把对 wfsim default_form）；各条再对 wiki Module:Weapons/data 的 Attacks 原文。
#   2026-10-10 第四批（维护者定）：nataruk —— wfsim nataruk_perfect.yaml:9 default_form 是完美
#     射击（900 / 60% / 2.4x，co independent），我方顶层 = Quick Shot（wiki primary Nataruk
#     AttackIndex 1，= nataruk.yaml form: base）。
#   2026-10-10 b5 批：
#   · 弓 / 蓄力霰弹：wfsim default_form = 满蓄力（module _TooltipAttackDisplay = 2），我方顶层 =
#     wiki AttackIndex 1「Uncharged Shot」（Evensong 为「Quick Shot」，Ballistica Prime 为
#     「Normal Shot」）= wfsim *_uncharged / *_quick（form: base）。
#   · zenith：wfsim zenith.yaml default_form = 半自动（form 写成 base，150 / 35%），我方顶层 =
#     wiki AttackIndex 1「Auto Mode」（30 / 10%）= zenith_auto.yaml ⇒ 半自动降级时记 semi_auto。
#   · kuva_quartakk：wfsim default_form = 瞄准点射（form: alt_fire，31% / 2.3x），我方顶层 =
#     wiki AttackIndex 1「Full-Auto」（21% / 1.9x）= kuva_quartakk_auto.yaml（form 写成 base）⇒
#     主段种类记 auto（「全自动」= 默认主段，「未蓄力」不适用）。
#   · miter：wfsim 只有满蓄力一条（miter.yaml，500），我方顶层 = wiki AttackIndex 1「Uncharged
#     Shot」（100 / 5% / 20%）⇒ main=None：满蓄力降级为 forms「蓄力」，主段种类记 base（未蓄力）。
MAIN_FORM_OVERRIDE: dict[str, dict] = {
    "nataruk": {"main": "nataruk"},
    "paris": {"main": "paris_uncharged"},
    "paris_prime": {"main": "paris_prime_uncharged"},
    "mk1_paris": {"main": "mk1_paris_uncharged"},
    "cernos": {"main": "cernos_uncharged"},
    "cernos_prime": {"main": "cernos_prime_uncharged"},
    "rakta_cernos": {"main": "rakta_cernos_uncharged"},
    "mutalist_cernos": {"main": "mutalist_cernos_uncharged"},
    "dread": {"main": "dread_uncharged"},
    "evensong": {"main": "evensong_quick"},
    "drakgoon": {"main": "drakgoon_uncharged"},
    "kuva_drakgoon": {"main": "kuva_drakgoon_uncharged"},
    "ballistica_prime": {"main": "ballistica_prime_uncharged"},
    "zenith": {"main": "zenith_auto", "demote_kind": "semi_auto"},
    "kuva_quartakk": {"main": "kuva_quartakk_auto", "main_kind": "auto"},
    "miter": {"main": None, "main_kind": "base"},
}

# wfsim 的 form: base 语义不定（弓 = 未蓄力、Fulmin = 全自动、Hind = 点射 …），而计算器关键词
# 「未蓄力」映射到 base。主段种类（写进记录的 main_form，计算器据此把关键词认作「就是默认主段」）：
# MAIN_FORM_OVERRIDE.main_kind > MAIN_KIND > 主段条目的 form（base 只在同组有 charged 形态时
# 才认作「未蓄力」，否则不写）。
#   fulmin / fulmin_prime：wfsim default_form（form: base）= wiki AttackIndex 1「Full Auto Mode」。
MAIN_KIND = {
    "fulmin": "auto",
    "fulmin_prime": "auto",
}


def is_main_form(y: dict) -> bool:
    """该 wfsim 条目是不是我方的「默认形态」（= weapons_stats 顶层面板对应的攻击）。"""
    g = str(y.get("transform_group") or "")
    if g in MAIN_FORM_OVERRIDE:
        return str(y.get("id") or "") == MAIN_FORM_OVERRIDE[g]["main"]
    return bool(y.get("default_form"))


def entry_key(y: dict, ents: dict) -> str:
    """条目 → 小写 uniqueName：自带 internal_name 优先，没写的沿 inherits 回溯。"""
    seen: set[str] = set()
    cur = y
    while cur and not cur.get("internal_name"):
        nxt = str(cur.get("inherits") or "")
        if not nxt or nxt in seen:
            return ""
        seen.add(nxt)
        cur = ents.get(nxt) or {}
    return str((cur or {}).get("internal_name") or "").lower()


def default_form_key(y: dict, ents: dict) -> str:
    """默认形态 → 小写 uniqueName（``entry_key``）；非默认形态返回空串（不收）。
    「默认形态」按 is_main_form 判定（MAIN_FORM_OVERRIDE 里的组以我方主段为准）。"""
    return entry_key(y, ents) if is_main_form(y) else ""


def form_kind(y: dict) -> str:
    """非默认形态收进 forms 的种类：MAIN_FORM_OVERRIDE 组里降级的 wfsim default_form 可改记
    ``demote_kind``（Zenith 的半自动 wfsim 写成 base），其余 = wfsim form。"""
    ov = MAIN_FORM_OVERRIDE.get(str(y.get("transform_group") or "")) or {}
    if y.get("default_form") and ov.get("demote_kind"):
        return str(ov["demote_kind"])
    return str(y.get("form") or "")


# 非默认形态（2026-10-10 第三批）：用户显式选了该形态才用（「灵化 / 副开火 / 蓄力 / 未蓄力 /
# 半自动 / 全自动」），整套替换主段与额外段；默认口径不读它。近战的 heavy_slam / block…
# 等招式形态不收（计算器没有对应开关，重击震地走 attacks 段）。
# 灵化形态的主段面板仍由 incarnon_forms.json 承担（现有运行期路径），这里只收带额外段的。
FORM_KINDS = ("incarnon", "alt_fire", "charged", "base", "semi_auto", "auto")
GUN_CATEGORIES = ("primary", "secondary", "sentinel", "archgun", "kitgun")


def group_default(y: dict, ents: dict) -> str:
    """同 transform_group 里「母武器锚点」的 yaml id（不唯一则空串）：一般 = 默认形态
    （is_main_form）；MAIN_FORM_OVERRIDE main=None 的组（wfsim 没有我方主段那一条）= wfsim
    default_form 条目（只借它的 internal_name 定位母武器）。"""
    g = y.get("transform_group")
    if not g:
        return ""
    ov = MAIN_FORM_OVERRIDE.get(str(g))
    no_main = bool(ov) and ov["main"] is None
    defaults = [
        str(e["id"])
        for e in ents.values()
        if e.get("transform_group") == g
        and (bool(e.get("default_form")) if no_main else is_main_form(e))
    ]
    return defaults[0] if len(defaults) == 1 else ""


def form_parent_key(y: dict, ents: dict) -> str:
    """非默认形态 → 母武器（同 transform_group 里锚点那条，见 group_default）的小写 uniqueName。
    wfsim 每个枪械 transform_group 恰有一个 default_form 条目（不唯一则返回空串、不收）；
    MAIN_FORM_OVERRIDE 的组以覆盖指定的条目为准。"""
    d = group_default(y, ents)
    return entry_key(ents[d], ents) if d else ""


def main_kind(group: str, ents: dict) -> str:
    """该组我方主段的形态种类（写进 main_form；空串 = 不写），口径见 MAIN_KIND 注释。"""
    ov = MAIN_FORM_OVERRIDE.get(group) or {}
    if ov.get("main_kind"):
        return str(ov["main_kind"])
    if group in MAIN_KIND:
        return MAIN_KIND[group]
    members = [e for e in ents.values() if e.get("transform_group") == group]
    main = [e for e in members if is_main_form(e)]
    if len(main) != 1:
        return ""
    f = str(main[0].get("form") or "")
    if f == "base":
        return "base" if any(e.get("form") == "charged" for e in members) else ""
    return f if f in FORM_KINDS and f != "incarnon" else ""


def check_override(ents: dict) -> None:
    """MAIN_FORM_OVERRIDE 防漂移：main 条目须仍在 wfsim 里，且与我方 weapons_stats 主段的
    伤害合计 / 暴击 / 暴伤 / 触发一致（容差 1%）；不一致只打印「!!」提示人工复核。"""
    ws_path = ROOT / "core" / "data" / "weapons_stats.json"
    ws = json.loads(ws_path.read_text(encoding="utf-8")) if ws_path.exists() else {}
    for g, ov in MAIN_FORM_OVERRIDE.items():
        yid = ov["main"]
        if yid is None:
            continue
        if yid not in ents:
            raise SystemExit(f"MAIN_FORM_OVERRIDE 过期：{g} 的 main 条目 {yid} 不在 wfsim 里")
        y = ents[yid]
        w = ws.get(entry_key(y, ents)) or {}
        a = y.get("attack") or {}
        pairs = (
            (
                (w.get("damage") or {}).get("total"),
                clean_damage(a.get("damage") or {}).get("total"),
            ),
            (w.get("criticalChance"), a.get("crit_chance")),
            (w.get("criticalMultiplier"), a.get("crit_multiplier")),
            (w.get("procChance"), a.get("status_chance")),
        )
        bad = [
            (x, z)
            for x, z in pairs
            if x is None or z is None or abs(float(x) - float(z)) > 0.01 * max(abs(float(z)), 0.6)
        ]
        if bad:
            print(f"  !! MAIN_FORM_OVERRIDE {g}→{yid} 与 weapons_stats 主段不一致：{bad}")


def attack_segments(a: dict, yid: str) -> list:
    """attack 块里的额外段（范围 / 集束 / 云团），默认形态与非默认形态同一口径。"""
    segs = []
    if isinstance(a.get("radial"), dict):
        segs.append(seg_of(a["radial"], "radial", "范围伤害", a, yid))
    c = a.get("cluster")
    if isinstance(c, dict):
        n = float(c.get("count") or 1.0)
        contact = seg_of(c, "cluster", "集束子炸弹（接触）", a, yid)
        contact["count"] = n
        segs.append(contact)
        if isinstance(c.get("radial"), dict):
            blast = seg_of(c["radial"], "cluster_radial", "集束子炸弹（爆炸）", a, yid)
            blast["count"] = n
            segs.append(blast)
    lg = a.get("lingering")
    if isinstance(lg, dict) and lg.get("damage"):
        cloud = seg_of(lg, "lingering", "残留云团", a, yid)
        cloud["merge_only"] = True
        segs.append(cloud)
    return segs


# 形态每发耗弹与 wiki 不一致处（b7 批，以 wiki 为准）：yaml id → (AmmoCost, 出处)。其余形态取 wfsim
# attack.ammo_cost（逐条对 wiki 模块该段 AmmoCost 一致；wiki 无对应段的 Tenet Plinx / Haalvu / Enkaus
# 等沿用 wfsim）。
FORM_AMMO_COST_WIKI: dict[str, tuple] = {
    "ferrox_thrown": (
        1.0,
        "wiki Ferrox §Notes「Throwing the spear consumes 1 ammo, then reloads」"
        "（模块 primary:4354 Spear Throw 无 AmmoCost 字段）；wfsim 写 0",
    ),
    "tenet_ferrox_thrown": (1.0, "同 Ferrox（模块 primary:12395 Spear Throw）；wfsim 写 0"),
    "mausolon_charged": (
        0.0,
        "wiki archwing:2570 Charged Shot Laser AmmoCost 0、Mausolon 页「Does "
        "not use ammo」；wfsim 1（按击杀槽当弹匣）",
    ),
    "morgha_alt": (0.0, "wiki archwing:2797 Charged Shot AmmoCost 0；wfsim 1"),
    "cortege_alt": (
        0.0,
        "wiki archwing:658 Grenade Impact AmmoCost 0、Cortege 页「Consumes no "
        "ammo」；wfsim 1（按击杀槽当弹匣）",
    ),
}


def form_record(y: dict, yid: str) -> dict:
    """一个非默认形态的整套数据：主段面板 + 额外段 + 蓄力时间 + 每发耗弹 + CO 桶别。
    wfsim 的形态把 attack 块写全（catalog.rs:61-78 的 INHERITED 只继承顶层键；
    panel.rs:289「a form states its `attack:` whole」），所以面板直接取本条目；
    弹匣 / 装填属顶层继承键，计算器沿用母武器的。"""
    a = y["attack"]
    rec: dict = {
        "main": {
            "damage": clean_damage(a.get("damage") or {}),
            "criticalChance": float(a.get("crit_chance") or 0.0),
            "criticalMultiplier": float(a.get("crit_multiplier") or 1.0),
            "procChance": float(a.get("status_chance") or 0.0),
            "fireRate": float(a.get("fire_rate") or 0.0),
            "multishot": float(a.get("multishot") or 1.0),
        },
        "name": y.get("name"),
    }
    segs = attack_segments(a, yid)
    if segs:
        rec["segments"] = segs
    if a.get("charge_seconds"):
        rec["charge_seconds"] = float(a["charge_seconds"])
    if isinstance(a.get("burst"), dict):
        # 点射形态（Bubonico / Nagantaka 副开火、Kuva Quartakk 开镜…）：fire_rate 是每秒点射次数，
        # 计算器按 wiki Fire Rate §Burst 换算有效射速（b6 批，wfsim model/weapon.rs:490-500）
        rec["burst"] = {
            "count": int(a["burst"]["count"]),
            "delay_seconds": float(a["burst"].get("delay_seconds") or 0.0),
        }
    if a.get("forced_procs"):
        rec["forced_procs"] = [str(x) for x in a["forced_procs"]]
    # 每发耗弹（b7 批）：形态各自的 AmmoCost（Zenith 半自动 3、Fulmin 半自动 10、Staticor 蓄力 5…，
    # wfsim attack.ammo_cost，attack.rs:101-109「READ, not assumed」；逐条对 wiki 模块该段 AmmoCost）。
    # 只写 ≠ 1 的；0（Cedo 副开火 / Ferrox 投矛等不耗弹）也写，计算器按「不耗弹 ⇒ 无装填循环」处理
    ac = FORM_AMMO_COST_WIKI[yid][0] if yid in FORM_AMMO_COST_WIKI else a.get("ammo_cost")
    if ac is not None and float(ac) != 1.0:
        rec["ammo_cost"] = float(ac)
    cb = str(y.get("co_behavior") or "additive_with_base_damage")
    if cb != "additive_with_base_damage":
        rec["co_behavior"] = cb
    return rec


def load_entries(data: Path, cats=CATEGORIES) -> list[tuple[str, dict]]:
    out = []
    for cat in cats:
        base = data / "weapons" / cat
        if not base.exists():
            continue
        for p in sorted(base.glob("*.yaml")):
            y = yaml.safe_load(p.read_text(encoding="utf-8"))
            if isinstance(y, dict):
                y.setdefault("id", p.stem)
                out.append((cat, y))
    return out


def main(argv: list[str]) -> int:
    data = data_dir(argv[1] if len(argv) > 1 else None)
    out_path = Path(argv[2]) if len(argv) > 2 else OUT
    entries = load_entries(data)
    ents: dict[str, dict] = {str(y["id"]): y for _, y in entries}
    rows: list[tuple[str, str, str, dict]] = []  # (key, cat, yaml_id, rec)
    # 非默认形态：(母武器键, 类别) → {"<kind>:<yaml_id>": rec}
    forms: dict[tuple[str, str], dict] = {}
    form_names: dict[tuple[str, str], str] = {}
    group_of: dict[tuple[str, str], str] = {}  # (母武器键, 类别) → transform_group
    for cat, y in entries:
        if not isinstance(y.get("attack"), dict):
            continue
        a = y["attack"]
        yid = str(y["id"])
        segs = attack_segments(a, yid)
        if not is_main_form(y):
            kind = form_kind(y)
            if cat not in GUN_CATEGORIES or kind not in FORM_KINDS:
                continue
            # 灵化形态的主段面板走 incarnon_forms.json；这里只收它带额外段的
            if kind == "incarnon" and not segs:
                continue
            pkey = form_parent_key(y, ents)
            if not pkey:
                print(f"  !! 形态找不到母武器（transform_group 无唯一默认形态）：{yid}")
                continue
            forms.setdefault((pkey, cat), {})[f"{kind}:{yid}"] = form_record(y, yid)
            form_names.setdefault((pkey, cat), str(ents[group_default(y, ents)].get("name") or ""))
            group_of.setdefault((pkey, cat), str(y.get("transform_group") or ""))
            continue
        if not segs and not a.get("charge_seconds"):
            continue
        key = default_form_key(y, ents)
        if not key:
            continue
        rec: dict = {}
        if a.get("charge_seconds"):
            rec["charge_seconds"] = float(a["charge_seconds"])
        if a.get("forced_procs"):
            rec["forced_procs"] = [str(x) for x in a["forced_procs"]]
        if segs:
            rec["segments"] = segs
            rec["main"] = {"damage": clean_damage(a.get("damage") or {})}
        rec["name"] = y.get("name")
        rec["yaml_id"] = yid
        rows.append((key, cat, yid, rec))
    check_whitelist(ents)
    check_override(ents)
    _gone = sorted(set(FORM_AMMO_COST_WIKI) - set(ents))
    if _gone:
        raise SystemExit(f"FORM_AMMO_COST_WIKI 过期：wfsim 已无条目 {_gone}")

    # 只有非默认形态、默认形态无额外段的母武器：补一条只带 forms 的记录
    have = {(k, c) for k, c, _, _ in rows}
    for (pkey, cat), fm in sorted(forms.items()):
        if (pkey, cat) not in have:
            rows.append((pkey, cat, "", {"name": form_names[(pkey, cat)]}))
    # 有非灵化形态的母武器：记下我方默认主段的形态种类 main_form（计算器据此把「蓄力 / 未蓄力 /
    # 全自动」等关键词识别为「就是默认形态」，而不是报「没有该形态数据」；口径见 main_kind）
    for key, cat, _yid, rec in rows:
        fm = forms.get((key, cat)) or {}
        if any(not k.startswith("incarnon:") for k in fm):
            mk = main_kind(group_of.get((key, cat), ""), ents)
            if mk:
                rec["main_form"] = mk
    for key, cat, _, rec in rows:
        if (key, cat) in forms:
            rec["forms"] = dict(sorted(forms[(key, cat)].items()))

    # 键冲突（同一 internal_name 出现在多个类别，如 Sporelacer / Tombfinger 的
    # primary / secondary 两份）：都改成「key#类别」，不让后写的静默覆盖先写的
    cnt: dict[str, int] = {}
    for key, *_ in rows:
        cnt[key] = cnt.get(key, 0) + 1
    out: dict = {
        "_meta": {
            "source": "github.com/magenie33/wfsim data/weapons/*（attack 块），其数值按 wiki/实测整理",
            "wfsim_commit": wfsim_commit(data),
            "generator": "scripts/build_weapon_attacks.py",
            "scope": "顶层 segments / main / charge_seconds 只收默认形态（default_form，"
            "或 MAIN_FORM_OVERRIDE 指定的我方主段；有非灵化形态的记录 main_form 记我方主段的形态"
            "种类（base = 未蓄力）；自身无 "
            "internal_name 的沿 inherits 取）；非默认形态收在 forms（键 <形态>:<yaml_id>，"
            "沿 transform_group 回到母武器），只在用户显式选了该形态时整套替换主段与额外段；"
            "灵化形态只收带额外段的（主段面板在 incarnon_forms.json）；近战招式形态不收",
            "note": "段是独立命中实例，各吃同样的 MOD 乘区；暴击/触发缺省继承直击段；"
            "takes_multishot 只写有出处的 false（缺省 = 吃多重）；cluster 段带 count；"
            "lingering 段 merge_only（只给同签名 attacks 段带 CO 标记）",
        }
    }
    for key, cat, yid, rec in rows:
        k = key if cnt[key] == 1 else f"{key}#{cat}"
        if k != key:
            print(f"  !! internal_name 冲突：{yid or rec.get('name')} → 键 {k}")
        out[k] = rec
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    vals = [v for k, v in out.items() if k != "_meta"]
    n_seg = sum(len(v.get("segments") or []) for v in vals)
    n_form = sum(len(v.get("forms") or {}) for v in vals)
    n_form_seg = sum(1 for v in vals for f in (v.get("forms") or {}).values() if f.get("segments"))
    sha = out["_meta"]["wfsim_commit"][:7]
    print(
        f"✓ {out_path.name}: {len(out) - 1} 把武器 / {n_seg} 个额外段 / "
        f"{n_form} 个非默认形态（其中带额外段 {n_form_seg}）（wfsim {sha}）"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
