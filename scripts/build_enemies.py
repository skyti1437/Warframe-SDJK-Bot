#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ⚠ 离线构建/开发工具：**不在插件加载路径**（插件运行期不 import 本文件），需手动运行。
"""生成 core/data/enemies.json（常用敌人 19 个）：英文 wiki + PEP（2026-10-26，wfsim 迁移 P4-C）。

取代 scripts/merge_wfsim_enemies.py（wfsim data/enemies + riven-mirror）。

数据源（每条每字段的出处写进记录的 ``src``）：
1. **wiki Module:Enemies/data/<阵营>**（CC BY-NC-SA 3.0）：派系、BaseLevel、Health / Shield / Armor /
   Overguard、Multis 的「Head: N.Nx」。派系优先取 ``FactionDamageOverride``（弱点表按它算，如
   Thrax Centurion：Faction = Unknown、FactionDamageOverride = Zariman）。
2. **PEP ExportEnemies.json**（calamity-inc/warframe-public-export-plus，MIT）：wiki 模块**只写非零项**
   （Grineer 63 条里只有 6 条写 Shield），没写的护盾 / 护甲取 PEP 伤害控制器的显式值（含 0）。
   wiki 与 PEP 都有的字段逐项对拍，不一致即报错（--allow-conflict 时以 wiki 为准并打印）。
   中文名取 PEP dict.zh.json（avatar 的 name 键），ZH_PATCH 覆盖。
3. **wiki Enemy_Body_Parts** 与各敌人页：头部倍率 / 无头部 / 暴击爆头资格的逐敌裁定，写在 HEAD_* 表里，
   每条带出处（2026-10-10 第三、四批逐条核对，维护者定的项目注明）。

用法::

    python scripts/build_enemies.py --wiki <wiki-modules 快照>/enemies --pep <PEP 目录> [--check]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lua_table import loads  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "core" / "data" / "enemies.json"

# 常用敌人保留名单（2026-10-10 维护者定，102 → 19 条）
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
# wiki 条目 InternalName 为空 / 不是 Agent 的，直接指定 PEP avatar（按 dict.en 英文名 + 阵营核对过）
PEP_AVATAR = {
    "Profit-Taker Orb": "/Lotus/Types/Enemies/Corpus/Vip/Arachnoid/ArachnoidCamperAvatar",
    "Grineer Warden": "/Lotus/Types/Enemies/Grineer/Gamemodes/WardenGrineerHeavyAvatar",
    "Thrax Centurion": "/Lotus/Types/Enemies/Duviri/Avatars/DuviriMeleeAvatar",
}
# 官方简中名覆盖（PEP dict.zh 缺或与客户端串不同的）；键见行尾注释
ZH_PATCH = {
    "Leech Osprey": "吸血鱼鹰",  # /Lotus/Language/Game/LeechDrone
    "Isolator Bursa": "隔离金流恐鸟",  # /Lotus/Language/Game/DispersionRiotMoaName
    "Legacyte": "传承种",  # /Lotus/Language/1999/LegacyteName
    "Techrot Babau": "科腐者疫胞",  # /Lotus/Language/1999/ProtoInfestedBabauName
    "Nox": "毒化者",  # /Lotus/Language/Game/ChemStrikeNox（旧表「诺克斯」是音译）
}
# 额外叫法（find_enemy_exact 认）：只放改名后旧叫法会失配的
ENEMY_ALIASES = {"Nox": ["诺克斯"]}

# ---------------------------------------------------------------------------
# 头部：模块 Multis 有「Head: N.Nx」就用它；下面是模块没有 / 与部位表冲突时的逐敌裁定
# ---------------------------------------------------------------------------
_EIDOLON_SRC = (
    "不吃爆头：Eidolon Teralyst 页「Explosives … will hit the invulnerable body and deal no damage」、"
    "伤害落在 Synovia，而「Synovia do not count as weak points for most effects」；同页「head possesses a "
    "3x headshot multiplier」与 Enemy_Body_Parts「Eidolon：Head (1x)」矛盾且本体无敌，不采用"
    "（2026-10-26 联网核对，Fandom / 论坛同）"
)
HEAD_TABLE: dict[str, tuple] = {
    # 名字 → (head_mul, 出处)
    "Isolator Bursa": (1.0, "wiki Enemy_Body_Parts（Bursa：Head 1x*，背部控制台 3x*）"),
    "Legacyte": (
        3.0,
        "wiki Enemy_Body_Parts（Legacyte：Head 3x*，* = 无暴击爆头）+ Legacyte 页"
        "「Head (3x, but no headcrit bonus)」；Module:Enemies/data/techrot 的 Multis 被注释"
        "（「Head: 4.5x … -- confirm」）未采用",
    ),
    # 2026-10-10 维护者定（第四批）：不在 KEEP_ENEMIES 里、数据不保留；留着是为了将来加回时头部值正确
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
}
# 模块值与部位表不一致、暂按模块值的（注明待拍板）
HEAD_NOTE = {
    "Techrot Babau": "Babau 页「Head: 1.5x / Tendril Bulbs: 1.5x」同；Enemy_Body_Parts 行「Phone Head¹ (1x)」"
    "的 1x 与同页总述「Techrot … closer to 1.5x」不符，按模块；¹「Promotes hits to next crit tier」= Babau 页"
    "「flat +100% Critical Chance」（head_cc_bonus），「2x Critical Damage multiplier」= 暴击爆头（head_crit）"
    "（2026-10-26 联网核对后定）",
    "MOA": "Body_Parts 屁股 3x（非爆头弱点）",
}
# 没有头部（打不出爆头）：head_mul 记 1.0 并标 head_none
HEAD_NONE_SRC = {
    "Eidolon Teralyst": _EIDOLON_SRC,
    "Profit-Taker Orb": "不吃爆头：Module:Enemies/data/corpus 该条 Multis 为「?」、Enemy_Body_Parts 无此条；"
    "本页头部只用于范围伤害判定（「immune to the AoE damage … unless the head … is within the AoE」）与"
    "病毒伤害，输出部位是腿；社区攻略 / 论坛亦无头部倍率（2026-10-26 联网核对）",
}
# 暴击爆头资格与派系默认不同的个体（派系默认见 core/damage_calc.py::HEADCRIT_FACTIONS）
HEAD_CRIT_OVERRIDE = {
    # Body_Parts「Airborne Corpus units like Ospreys ... headcrit applies to them」
    "Leech Osprey": True,
    # Body_Parts 行「Thrax Centurion … Head (3x)」无 * 标记（Zariman 派系无整体说明）
    "Thrax Centurion": True,
    # Body_Parts「Legacyte … Head (3x)*」+ 本页「no headcrit bonus」
    "Legacyte": False,
    # Babau 页「head … 2x Critical Damage multiplier」；Body_Parts 行无 *（Techrot 派系默认不触发）
    "Techrot Babau": True,
    # Body_Parts「Tar Mutalist MOA … Central Purple Orb² (4.5x)*」：* = 无暴击爆头（不在保留名单）
    "Tar-Mutalist MOA": False,
}

# 头部额外暴击率（加在暴击率上，1.0 = +100%）：Babau 页「flat +100% Critical Chance」
HEAD_CC_BONUS = {"Techrot Babau": 1.0}

_HEAD_RE = re.compile(r"^\s*Head:\s*([\d.]+)x\s*$")


def load_wiki(wiki_dir: Path) -> dict[str, tuple[str, dict, dict]]:
    """英文名 → (模块页名, 条目, 修订 meta)。"""
    out: dict[str, tuple[str, dict, dict]] = {}
    for f in sorted(wiki_dir.glob("Module_Enemies_data_*.wiki")):
        text = f.read_text(encoding="utf-8")
        data = loads(text[text.find("return") :])
        meta_f = f.with_suffix(".meta.json")
        meta = json.loads(meta_f.read_text(encoding="utf-8")) if meta_f.exists() else {}
        page = "Module:Enemies/data/" + f.stem.split("_")[-1]
        for name, rec in data.items():
            if isinstance(rec, dict):
                if name in out:
                    raise SystemExit(f"wiki 模块里重名：{name}（{out[name][0]} / {page}）")
                out[str(name)] = (page, rec, meta)
    return out


def pep_avatar(name: str, general: dict, pep: dict) -> str:
    if name in PEP_AVATAR:
        return PEP_AVATAR[name]
    agent = pep["agents"].get(general.get("InternalName") or "")
    if not agent:
        raise SystemExit(
            f"{name}：wiki InternalName {general.get('InternalName')!r} 在 PEP agents 里找不到"
        )
    return agent["avatarTypes"]["STANDARD"]


def build(wiki_dir: Path, pep_dir: Path, allow_conflict: bool) -> dict:
    wiki = load_wiki(wiki_dir)
    pep = json.loads((pep_dir / "ExportEnemies.json").read_text(encoding="utf-8"))
    zh_dict = json.loads((pep_dir / "dict.zh.json").read_text(encoding="utf-8"))
    conflicts: list[str] = []
    enemies: dict[str, dict] = {}
    for name in KEEP_ENEMIES:
        if name not in wiki:
            raise SystemExit(f"保留名单里的敌人不在 wiki 模块里：{name}")
        page, rec, meta = wiki[name]
        rev = meta.get("revision") or meta.get("oldid") or "?"
        wsrc = f"wiki {page}（oldid {rev}）「{name}」"
        gen, stats = rec.get("General") or {}, rec.get("Stats") or {}
        av_key = pep_avatar(name, gen, pep)
        av = pep["avatars"][av_key]
        ctrl = pep["damageControllers"].get(av.get("damageController") or "", {})
        psrc = f"PEP ExportEnemies {av_key.rsplit('/', 1)[-1]} / {str(av.get('damageController')).rsplit('/', 1)[-1]}"
        src: dict[str, str] = {}
        out: dict = {"name": name}

        zh = ZH_PATCH.get(name) or zh_dict.get(av.get("name") or "", "")
        out["zh"] = zh
        src["zh"] = "ZH_PATCH" if name in ZH_PATCH else f"PEP dict.zh {av.get('name')}"

        fac = gen.get("FactionDamageOverride") or gen.get("Faction")
        out["faction"] = fac
        src["faction"] = wsrc + (
            " FactionDamageOverride" if gen.get("FactionDamageOverride") else " Faction"
        )

        out["base_level"] = int(stats.get("BaseLevel") or 1)
        src["base_level"] = wsrc + " Stats.BaseLevel"

        for key, wkey, pkey, pval in (
            ("base_health", "Health", "health", av.get("health")),
            ("base_shield", "Shield", "shield", ctrl.get("shield")),
            ("base_armor", "Armor", "armor", ctrl.get("armor")),
        ):
            wv = stats.get(wkey)
            if wv is not None:
                out[key] = float(wv)
                src[key] = f"{wsrc} Stats.{wkey}"
                if pval is not None and float(pval) != float(wv):
                    conflicts.append(f"{name} {key}: wiki {wv} ≠ PEP {pkey} {pval}")
            elif pval is not None:
                out[key] = float(pval)
                src[key] = f"{psrc} {pkey}（wiki 模块只写非零项，未写）"
            else:
                raise SystemExit(f"{name} {key}：wiki 与 PEP 都没有")
        if stats.get("Overguard"):
            out["base_overguard"] = float(stats["Overguard"])
            src["base_overguard"] = wsrc + " Stats.Overguard"

        heads = [m.group(1) for s in stats.get("Multis") or [] if (m := _HEAD_RE.match(str(s)))]
        if name in HEAD_NONE_SRC:
            out["head_mul"], out["head_src"], out["head_none"] = 1.0, HEAD_NONE_SRC[name], True
        elif name in HEAD_TABLE:
            out["head_mul"], out["head_src"] = HEAD_TABLE[name]
        elif heads:
            out["head_mul"] = float(heads[0])
            out["head_src"] = f"wiki {page}（Head: {heads[0]}x）" + (
                "；" + HEAD_NOTE[name] if name in HEAD_NOTE else ""
            )
        else:
            raise SystemExit(f"{name}：模块无 Head 倍率，HEAD_TABLE / HEAD_NONE_SRC 也没有裁定")
        if name in HEAD_CRIT_OVERRIDE:
            out["head_crit"] = HEAD_CRIT_OVERRIDE[name]
        if name in HEAD_CC_BONUS:
            out["head_cc_bonus"] = HEAD_CC_BONUS[name]
        if name in ENEMY_ALIASES:
            out["aliases"] = list(ENEMY_ALIASES[name])
        out["src"] = src
        enemies[name] = out
    if conflicts:
        print("wiki / PEP 不一致：\n  " + "\n  ".join(conflicts))
        if not allow_conflict:
            raise SystemExit("有不一致，核对后加 --allow-conflict（以 wiki 为准）")
    return {
        "_meta": {
            "source": "英文 wiki Module:Enemies/data（CC BY-NC-SA 3.0）+ PEP ExportEnemies（MIT，"
            "calamity-inc/warframe-public-export-plus）",
            "wiki_snapshot": "wiki-modules-20261026/enemies",
            "pep_commit": "513105d5c4e56d2a3238b7c7914e8ef4e87e829a",
            "generator": "scripts/build_enemies.py",
            "head_note": "head_mul 为 null = 未分类（按人形 ×3 计）；head_src 为出处；head_none 为无头部"
            "（爆头不成立）；head_crit 为与派系默认不同的暴击爆头资格",
            "keep_note": "只保留常用敌人 19 个（2026-10-10 维护者定，KEEP_ENEMIES）；zh 为官方简中，"
            "aliases 为额外叫法",
        },
        "enemies": enemies,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument(
        "--wiki", type=Path, required=True, help="敌人 wiki 快照目录（Module_Enemies_data_*.wiki）"
    )
    ap.add_argument(
        "--pep", type=Path, required=True, help="PEP 目录（ExportEnemies.json / dict.zh.json）"
    )
    ap.add_argument("--allow-conflict", action="store_true")
    ap.add_argument("--check", action="store_true", help="只比对，不写盘（不一致则退出码 1）")
    a = ap.parse_args()
    data = build(a.wiki, a.pep, a.allow_conflict)
    text = json.dumps(data, ensure_ascii=False, indent=1) + "\n"
    if a.check:
        same = OUT.read_text(encoding="utf-8") == text
        print("enemies.json 与生成结果" + ("一致" if same else "不一致"))
        raise SystemExit(0 if same else 1)
    OUT.write_text(text, encoding="utf-8")
    print(f"写入 {OUT.relative_to(ROOT)}：{len(data['enemies'])} 个敌人")


if __name__ == "__main__":
    main()
