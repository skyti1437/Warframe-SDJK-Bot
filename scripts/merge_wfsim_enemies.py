# -*- coding: utf-8 -*-
"""把 wfsim data/enemies 的敌人基准数据合并进 core/data/enemies.json。

数据源：magenie33/wfsim data/enemies/*.yaml（每条带 wiki Module:Enemies/data
来源）。字段：stats.{base_level,health,shield,armor}、body_parts 里 is_head
的 multiplier、faction_damage_override（派系弱点表用的键）。
中文名从 wfsim data/i18n/zh/names.yaml 的 enemies 段取（DE 官方客户端串）。

合并后再打「爆头口径」补丁（apply_head_patch，2026-10-10）：head_mul 只放有来源的
真实头部倍率，**找不到可靠来源的写 null（= 未分类，计算器按人形 ×3 计并在卡面注明）**，
不再拿数值 2 当「未分类」哨兵（riven-mirror 的 2 是旧「爆头 ×2」通用约定，并非逐敌值）；
每条带 head_src 说明出处；没有头部的写 head_none；暴击爆头资格与派系默认不同的写 head_crit。
只打补丁、不需要 wfsim/PyYAML：python scripts/merge_wfsim_enemies.py --head-only

最后按 KEEP_ENEMIES（常用敌人保留名单，2026-10-10 维护者定 19 条）删掉其余条目，并补官方
中文名（ZH_PATCH）与额外叫法（ENEMY_ALIASES）。两种模式都执行，重复跑结果不变。
"""

import glob
import json
import sys
from collections import Counter
from pathlib import Path

WFSIM = Path.home() / "tmp" / "wfsim" / "data"
OUT = Path(__file__).resolve().parent.parent / "core" / "data" / "enemies.json"

# wfsim faction_damage_override → 我们的派系键（damage_faction.json 的列名）
FAC_MAP = {
    "grineer": "Grineer",
    "kuva_grineer": "Kuva Grineer",
    "corpus": "Corpus",
    "corpus_amalgam": "Corpus Amalgam",
    "infested": "Infested",
    "infested_deimos": "Infested Deimos",
    "orokin": "Orokin",
    "sentient": "Sentient",
    "narmer": "Narmer",
    "the_murmur": "The Murmur",
    "zariman": "Zariman",
    "scaldra": "Scaldra",
    "techrot": "Techrot",
    "anarchs": "Anarchs",
    "unaffiliated": "Grineer",
}

# ---------------------------------------------------------------------------
# 爆头口径补丁（2026-10-10）。来源均为 wiki.warframe.com：
#   M = Module:Enemies/data/<阵营>（结构化数据 Stats.Multis 的「Head: N.Nx」）
#   B = Enemy_Body_Parts（逐敌部位表；* = 无暴击爆头，† = 非爆头弱点）
# ---------------------------------------------------------------------------
_SRC_M3 = "wiki Module:Enemies/data（Head: 3.0x）"
_HEAD3_MODULE = (
    "Bailiff",
    "Butcher",
    "Flameblade",
    "Powerfist",
    "Scorpion",
    "Shield Lancer",
    "Ballista",
    "Eviscerator",
    "Hellion",
    "Lancer",
    "Elite Lancer",
    "Scorch",
    "Seeker",
    "Trooper",
    "Bombard",
    "Commander",
    "Drahk Master",
    "Heavy Gunner",
    "Hyekka Master",
    "Napalm",
    "Nox",
    "Ghoul Auger",
    "Ghoul Devourer",
    "Ghoul Expired",
    "Ghoul Rictus",
    "Grineer Warden",
    "Crewman",
    "Detron Crewman",
    "Elite Crewman",
    "Nullifier Crewman",
    "Prod Crewman",
    "Sniper Crewman",
    "Corpus Tech",
    "Leech Osprey",
    "Lynx Osprey",
    "Mine Osprey",
    "Oxium Osprey",
    "Sapping Osprey",
    "Shield Osprey",
    "Leaper",
    "Runner",
    "Volatile Runner",
    "Crawler",
    "Electric Crawler",
    "Lobber Crawler",
    "Nauseous Crawler",
    "Toxic Crawler",
    "Ancient Healer",
    "Boiler",
    "Brood Mother",
    "Toxic Ancient",
    "Corrupted Ancient",
    "Corrupted Butcher",
    "Corrupted Bombard",
    "Corrupted Heavy Gunner",
    "Corrupted Lancer",
    "Corrupted Crewman",
    "Corrupted Nullifier",
)
HEAD_PATCH: dict = {n: (3.0, _SRC_M3) for n in _HEAD3_MODULE}
HEAD_PATCH.update(
    {
        # Module 里查不到同名条目，按 Body_Parts 的对应变体
        "Manic": (3.0, "wiki Enemy_Body_Parts（Grineer Manic：Head 3x）"),
        "Comba": (3.0, "wiki Enemy_Body_Parts（Comba：Head 3x*）"),
        "Scrambus": (3.0, "wiki Enemy_Body_Parts（Scrambus：Head 3x*）"),
        "Ancient Disrupter": (3.0, "wiki Enemy_Body_Parts（Ancient Disruptor：Head 3x）"),
        # 头部 1x（不吃爆头基数；1x 部位也没有暴击爆头）
        "Charger": (1.0, "wiki Module:Enemies/data（Head: 1.0x）"),
        "MOA": (1.0, "wiki Module:Enemies/data（Head: 1.0x）；Body_Parts 屁股 3x（非爆头弱点）"),
        "Anti MOA": (1.0, "wiki Enemy_Body_Parts（Anti MOA：Head 1x*）"),
        "Fusion MOA": (1.0, "wiki Enemy_Body_Parts（Fusion MOA：Head 1x*）"),
        "Corrupted MOA": (1.0, "wiki Enemy_Body_Parts（Fusion MOA 变体：Head 1x*）"),
        "Railgun MOA": (1.0, "wiki Enemy_Body_Parts（MOA 及多数变体：Head 1x*）"),
        "Shockwave MOA": (1.0, "wiki Enemy_Body_Parts（MOA 及多数变体：Head 1x*）"),
        "Denial Bursa": (1.0, "wiki Enemy_Body_Parts（Bursa：Head 1x*，背部控制台 3x*）"),
        "Drover Bursa": (1.0, "wiki Enemy_Body_Parts（Bursa：Head 1x*，背部控制台 3x*）"),
        "Isolator Bursa": (1.0, "wiki Enemy_Body_Parts（Bursa：Head 1x*，背部控制台 3x*）"),
        "Mutalist Osprey": (1.0, "wiki Enemy_Body_Parts（Infested Osprey 没有头部）"),
        "Tusk Firbolg": (1.0, "wiki Enemy_Body_Parts（Chassis 1x，无弱点）"),
        "Tusk Bolkor": (1.0, "wiki Enemy_Body_Parts（Chassis 1x，无弱点）"),
        "Sensor Regulator": (1.0, "wiki Enemy_Body_Parts（Regulator：Chassis 1x，无弱点）"),
        # 未分类：找不到可靠对应，或两处 wiki 互相矛盾
        "Wolf of Saturn Six": (None, "未分类：Module 条目无 Multis，Body_Parts 无此条"),
        "Condor Dropship": (None, "未分类：Module 条目无 Multis，Body_Parts 无此条"),
        "Hemocyte": (None, "未分类：Module 条目无 Multis，Body_Parts 无此条"),
        "Fire Prosecutor": (None, "未分类：两处 wiki 均无此条"),
        "Human of Anyo Corp": (None, "未分类：两处 wiki 均无此条"),
        "Robotic of Anyo Corp": (None, "未分类：两处 wiki 均无此条"),
        "Drone": (None, "未分类：名称无法唯一对应（Attack/Shield/Scavenger Drone…）"),
        "Scavanger Osprey": (None, "未分类：两处 wiki 均无同名条目"),
        "Orokin Drone": (None, "未分类：Module 条目 Multis 为空，Body_Parts 无此条"),
        # 2026-10-10 维护者定（第四批）：按 Enemy_Body_Parts 取 4.5（不在 KEEP_ENEMIES 里，
        # 数据中不保留；写在这里是为了将来加回时头部值正确）
        "Swarm Mutalist MOA": (
            4.5,
            "wiki Enemy_Body_Parts（Swarm Mutalist MOA：Head² (4.5x)，无 * = 可暴击爆头；"
            "² Infested MOAs have their head weak point where the gun would otherwise be）；"
            "Module:Enemies/data 为 Tail 3.0x/Face 0.5x 无 Head，未采用",
        ),
        "Tar-Mutalist MOA": (
            4.5,
            "wiki Enemy_Body_Parts（Tar Mutalist MOA：Central Purple Orb² (4.5x)*，"
            "* = No critical headshot bonus；² 同上）；Module 无此条",
        ),
        # 2026-10-10 第三批：逐条核对
        # Legacyte —— Body_Parts 与本页一致，Module 的 Multis 是注释掉的待确认值
        "Legacyte": (
            3.0,
            "wiki Enemy_Body_Parts（Legacyte：Head 3x*，* = 无暴击爆头）+ Legacyte 页"
            "「Head (3x, but no headcrit bonus)」；Module:Enemies/data/techrot 的 Multis 被注释"
            "（「Head: 4.5x … -- confirm」）未采用；wfsim legacyte.yaml「NO WEAK POINT IS "
            "PUBLISHED」与现行 wiki 不符",
        ),
        # Techrot Babau —— 三处不一致，保持 Module 值并写明（见第三批报告）
        "Techrot Babau": (
            1.5,
            "wiki Module:Enemies/data/techrot（Head: 1.5x；wfsim techrot_babau.yaml 同）；"
            "⚠ Enemy_Body_Parts 写 Phone Head¹ (1x)（¹ = 命中升一档暴击），Babau 页 Notes 写"
            "头部与触须球「2x Critical Damage multiplier, +100% Critical Chance」—— 三处不一致；"
            "升档 / 2x 暴伤本计算器未建模，保持 Module 值待拍板",
        ),
    }
)
# 暴击爆头资格与派系默认不同的个体（派系默认见 core/damage_calc.py::HEADCRIT_FACTIONS）
HEAD_CRIT_OVERRIDE = {
    # Body_Parts「Airborne Corpus units like Ospreys ... headcrit applies to them」
    **{
        n: True
        for n in (
            "Leech Osprey",
            "Lynx Osprey",
            "Mine Osprey",
            "Oxium Osprey",
            "Sapping Osprey",
            "Shield Osprey",
        )
    },
    # Corpus 人形的堕落变体：Crewmen 表「No critical headshot bonus」，表头注明适用于各变体
    "Corrupted Crewman": False,
    "Corrupted Nullifier": False,
    # Zariman 派系无整体说明；Body_Parts 行「Thrax Centurion … Head (3x)」无 * 标记
    "Thrax Centurion": True,
    # Body_Parts「Legacyte … Head (3x)*」+ 本页「no headcrit bonus」（Techrot 派系默认亦不触发）
    "Legacyte": False,
    # Body_Parts「Tar Mutalist MOA … Central Purple Orb² (4.5x)*」：* = 无暴击爆头
    "Tar-Mutalist MOA": False,
}

# 没有头部（打不出爆头）：head_mul 记 1.0 并标 head_none，计算器对它不叠任何爆头加成。
# 前四个 Body_Parts 明写无头/无弱点（出处在 HEAD_PATCH）；其余 5 条 2026-10-10 第三批逐条核过，
# 出处写进 HEAD_NONE_SRC（Legacyte 已移出：Body_Parts 与本页均写 Head 3x）。
HEAD_NONE = (
    "Mutalist Osprey",
    "Tusk Firbolg",
    "Tusk Bolkor",
    "Sensor Regulator",
    "Eidolon Teralyst",
    "Eidolon Gantulyst",
    "Eidolon Hydrolyst",
    "Teralyst Synovia",
    "Profit-Taker Orb",
)
_EIDOLON_SRC = (
    "保持「无头部、不吃爆头」待拍板：wiki Enemy_Body_Parts「Eidolon：Body (1x) / Head (1x) / "
    "Synovia (1x)」与各夜灵页「body part multiplier of 2x … head possesses a 3x headshot "
    "multiplier」矛盾，Module:Enemies/data/sentient 该条 Multis 为「?」；夜灵本体无敌，"
    "伤害只落在 Synovia（Eidolon Teralyst 页）"
)
HEAD_NONE_SRC = {
    "Eidolon Teralyst": _EIDOLON_SRC,
    "Eidolon Gantulyst": _EIDOLON_SRC,
    "Eidolon Hydrolyst": _EIDOLON_SRC,
    "Teralyst Synovia": "wiki Enemy_Body_Parts：Synovia 列在 Other 列（1x），不是头部；"
    "Eidolon Teralyst 页「Synovia do not count as weak points for most effects」",
    "Profit-Taker Orb": "无来源：Module:Enemies/data/corpus 该条 Multis 为「?」、Enemy_Body_Parts "
    "无此条、本页无部位倍率；沿用旧值（无头部、不吃爆头）待核",
}


# ---------------------------------------------------------------------------
# 常用敌人保留名单（2026-10-10 维护者定，第四批：102 → 19 条）
#   enemies.json 只留这 19 个；其余（riven-mirror 全表 / wfsim 新增）在合并与补丁之后
#   一律删掉。build_damage_data.py 重建出全表后必须再跑本脚本，结果才可复现。
#   名单里的名字必须在表里（缺了直接报错，不静默少一条）。
# ---------------------------------------------------------------------------
KEEP_ENEMIES = (
    "Lancer",
    "Heavy Gunner",
    "Crewman",
    "Charger",
    "Runner",
    "Corrupted Heavy Gunner",
    "MOA",
    "Leech Osprey",
    "Isolator Bursa",
    "Nox",
    "Bombard",
    "Grineer Warden",
    "Ancient Healer",
    "Demolisher Devourer",
    "Thrax Centurion",
    "Eidolon Teralyst",
    "Profit-Taker Orb",
    "Legacyte",
    "Techrot Babau",
)
# 官方简中名（core/data/_cache/dict.zh.json = DE 客户端词表，键见行尾注释）。
# 覆盖 build_damage_data._ENEMY_ZH / wfsim names.yaml 的空值或非官方译名。
ZH_PATCH = {
    "Leech Osprey": "吸血鱼鹰",  # /Lotus/Language/Game/LeechDrone
    "Isolator Bursa": "隔离金流恐鸟",  # /Lotus/Language/Game/DispersionRiotMoaName
    "Legacyte": "传承种",  # /Lotus/Language/1999/LegacyteName
    "Techrot Babau": "科腐者疫胞",  # /Lotus/Language/1999/ProtoInfestedBabauName
    "Nox": "毒化者",  # /Lotus/Language/Game/ChemStrikeNox（旧表写的「诺克斯」是音译）
    # 不在保留名单，留给将来加回
    "Swarm Mutalist MOA": "异融胞群恐鸟",  # /Lotus/Language/Game/InfestedMoaNanite
    "Tar-Mutalist MOA": "异融焦油恐鸟",  # /Lotus/Language/Game/InfestedMoaGoo
}
# 额外叫法（find_enemy_exact 认）：只放改名后旧叫法会失配的
ENEMY_ALIASES = {
    "Nox": ["诺克斯"],
}


def apply_keep(table: dict) -> list:
    """只保留 KEEP_ENEMIES，并补官方中文名与额外叫法。返回被删的英文名（按原顺序）。幂等。"""
    missing = [n for n in KEEP_ENEMIES if n not in table]
    if missing:
        raise SystemExit(f"保留名单里的敌人不在表里：{missing}")
    dropped = [n for n in table if n not in KEEP_ENEMIES]
    for n in dropped:
        del table[n]
    for n, rec in table.items():
        if n in ZH_PATCH:
            rec["zh"] = ZH_PATCH[n]
        if n in ENEMY_ALIASES:
            rec["aliases"] = list(ENEMY_ALIASES[n])
    return dropped


def apply_head_patch(table: dict) -> Counter:
    """按 HEAD_PATCH / HEAD_NONE / HEAD_CRIT_OVERRIDE 改写 head_mul/head_src/head_none/head_crit。

    不在补丁表里的条目：保留已有数值并补 head_src（wfsim 合并来的 / riven-mirror 原值）；
    若仍是旧哨兵 2 则改为 null（未分类）。幂等，可重复跑。
    """
    stat: Counter = Counter()
    for name, rec in table.items():
        if name in HEAD_PATCH:
            val, src = HEAD_PATCH[name]
            rec["head_mul"] = val
            rec["head_src"] = src
            stat["null" if val is None else f"{val:g}"] += 1
        else:
            hm = rec.get("head_mul")
            if hm == 2 and not rec.get("source"):
                rec["head_mul"] = None
                rec["head_src"] = "未分类：riven-mirror 通用值 2，非逐敌数据"
                stat["null"] += 1
            elif "head_src" not in rec:
                rec["head_src"] = (
                    "wfsim data/enemies body_parts（wiki Module:Enemies/data）"
                    if rec.get("source")
                    else "riven-mirror codex/enemy.ts"
                )
                stat["kept"] += 1
            else:
                stat["kept"] += 1
        if name in HEAD_NONE:
            rec["head_mul"] = 1.0
            rec["head_none"] = True
            if name in HEAD_NONE_SRC:
                rec["head_src"] = HEAD_NONE_SRC[name]
            stat["head_none"] += 1
        elif rec.get("head_none"):
            # 移出 HEAD_NONE 的（Legacyte）：清掉旧标记，保证 --head-only 可重复执行
            del rec["head_none"]
            stat["head_none_cleared"] += 1
        if name in HEAD_CRIT_OVERRIDE:
            rec["head_crit"] = HEAD_CRIT_OVERRIDE[name]
            stat["head_crit"] += 1
    return stat


def merge_wfsim(table: dict) -> None:
    try:
        import yaml
    except ImportError:  # noqa: BLE001 —— 插件运行本身不需要 yaml，只有这几个
        raise SystemExit(  # 数据构建脚本要；给可操作的提示而不是裸 ImportError
            "本脚本需要 PyYAML：pip install pyyaml（插件运行时并不依赖它，仅构建数据用）"
        )
    zh_names: dict = {}
    try:
        n = yaml.safe_load((WFSIM / "i18n" / "zh" / "names.yaml").read_text(encoding="utf-8"))
        zh_names = n.get("enemies") or {}
    except Exception:  # noqa: BLE001
        pass

    added, skipped = [], []
    for f in glob.glob(str(WFSIM / "enemies" / "**" / "*.yaml"), recursive=True):
        y = yaml.safe_load(open(f, encoding="utf-8"))
        if not isinstance(y, dict) or not y.get("stats"):
            continue
        name = y.get("name")
        if not name:
            continue
        if name in table:
            skipped.append(name)
            continue
        st = y["stats"]
        head = None  # 无 is_head 部位 ⇒ 未分类（不再用 2.0 占位）
        for bp in y.get("body_parts") or []:
            if bp.get("is_head") and bp.get("multiplier") is not None:
                head = float(bp["multiplier"])
        fac = (y.get("faction_damage_override") or y.get("faction") or "").lower()
        zh = zh_names.get(y.get("id")) or zh_names.get(name)
        if isinstance(zh, dict):
            zh = zh.get("name") or zh.get("zh")
        table[name] = {
            "name": name,
            "zh": zh or None,
            "faction": FAC_MAP.get(fac, "Grineer"),
            "base_level": int(st.get("base_level") or 1),
            "base_health": float(st.get("health") or 0),
            "base_shield": float(st.get("shield") or 0),
            "base_armor": float(st.get("armor") or 0),
            "head_mul": head,
            "source": "wfsim data/enemies（wiki Module:Enemies/data）",
        }
        if st.get("overguard"):
            table[name]["base_overguard"] = float(st["overguard"])
        added.append(f"{name}({zh or '—'})")
    print(f"新增 {len(added)}：{', '.join(added)}")
    print(f"已存在跳过 {len(skipped)}；总数 {len(table)}")


def main() -> None:
    ours = json.loads(OUT.read_text(encoding="utf-8"))
    table = ours["enemies"]
    if "--head-only" not in sys.argv[1:]:
        merge_wfsim(table)
        ours.setdefault("_meta", {})["note"] = (
            "2026-09-17 从 wfsim data/enemies（wiki Module:Enemies/data 来源）"
            "补充 Acolyte/凶魂百夫长等条目，带 zh 与 head_mul"
        )
    stat = apply_head_patch(table)
    dropped = apply_keep(table)
    ours.setdefault("_meta", {})["keep_note"] = (
        f"2026-10-10 维护者定：只保留常用敌人 {len(KEEP_ENEMIES)} 个"
        "（scripts/merge_wfsim_enemies.py::KEEP_ENEMIES）；zh 为官方简中（ZH_PATCH），"
        "aliases 为额外叫法"
    )
    ours.setdefault("_meta", {})["head_note"] = (
        "2026-10-10 爆头口径：head_mul 为 null = 未分类（按人形 ×3 计）；"
        "head_src 为出处；head_none 为无头部（爆头不成立）；head_crit 为与派系默认不同的暴击爆头资格"
    )
    OUT.write_text(json.dumps(ours, ensure_ascii=False, indent=1), encoding="utf-8")
    print("爆头补丁：" + "，".join(f"{k} {v}" for k, v in sorted(stat.items())))
    print(
        f"保留名单：留 {len(table)} 条，删 {len(dropped)} 条"
        + (f"：{', '.join(dropped)}" if dropped else "")
    )


if __name__ == "__main__":
    main()
