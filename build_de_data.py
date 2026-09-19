# -*- coding: utf-8 -*-
"""从 DE 官方导出重建 core/data/de/ 下的中文数据表。

数据源（全部为 DE PublicExport 镜像，非国服数据）：
  · https://browse.wf/warframe-public-export-plus/dict.zh.json
      DE 官方简中词表（35900+ 条 /Lotus/Language/... -> 中文）
  · https://oracle.browse.wf/dicts/zh.json
      browse.wf 维护的补充词表（DE 导出里缺失的 200 余条）
  · .../ExportRegions.json
      节点 key -> {name, systemName}（值是语言键，需经 dict 解析）
  · .../ExportMissionTypes.json
      任务类型 key -> 语言键
  · .../ExportChallenges.json
      挑战资产路径 -> {name, description, requiredCount}（值是语言键）

产物：
  core/data/de/languages_zh.json      合并后的官方简中词表（精简：仅 /Lotus/Language/*）
  core/data/de/nodes_zh.json          重建：节点 key -> {name, system}
  core/data/de/mission_types_zh.json  重建：solNodes 的 MT 键 / 中文名
  core/data/de/challenges_zh.json     重建：挑战资产路径 -> {name, desc, count}

用法：
  python build_de_data.py            # 全量重建
  python build_de_data.py --check    # 只做一致性自检，不写文件
"""
from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

BASE = "https://browse.wf/warframe-public-export-plus"
DICT_URL = f"{BASE}/dict.zh.json"
ORACLE_DICT = "https://oracle.browse.wf/dicts/zh.json"
REGIONS_URL = f"{BASE}/ExportRegions.json"
BOUNTIES_URL = f"{BASE}/ExportBounties.json"
VENDORS_URL = f"{BASE}/ExportVendors.json"
MISSIONS_URL = f"{BASE}/ExportMissionTypes.json"
CHALLENGES_URL = f"{BASE}/ExportChallenges.json"
UPGRADES_URL = f"{BASE}/ExportUpgrades.json"
RECIPES_URL = f"{BASE}/ExportRecipes.json"
FACTIONS_URL = f"{BASE}/ExportFactions.json"

DATA = Path(__file__).resolve().parent / "core" / "data" / "de"

# 本地已有的 solNodes（wfcd），用于给节点补充 MT 键
SOL_NODES = DATA / "solNodes.json"


def fetch(url: str) -> object:
    req = urllib.request.Request(url, headers={"User-Agent": "sdjk-build/1.0"})
    with urllib.request.urlopen(req, timeout=120) as resp:  # noqa: S310
        return json.loads(resp.read().decode("utf-8"))


def write_json(path: Path, data: object) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")),
                    encoding="utf-8")
    print(f"  -> {path.name}: {path.stat().st_size / 1024:.0f} KB")


def build_languages(dict_zh: dict, oracle: dict) -> dict:
    """合并官方词表 + browse.wf 补充词表；只保留 /Lotus/Language/* 前缀。"""
    merged: dict[str, str] = {}
    for k, v in dict_zh.items():
        if isinstance(v, str) and k.startswith("/Lotus/Language/"):
            merged[k] = v
    for k, v in oracle.items():
        if isinstance(v, str) and k.startswith("/Lotus/Language/") and k not in merged:
            merged[k] = v
    return merged


def build_nodes(regions: dict, lang: dict) -> dict:
    """节点 key -> {name, system, level, mt, type}。

    ExportRegions 的值是语言键，需要经词表解析；解析不到中文时回落到英文键末段，
    但仍保留该条，保证「节点名带星球」的展示逻辑始终可用。
    ``level`` 是敌人等级区间（如 ``33-35``），供执刑官/突击/裂隙等显示用。

    ``mt`` / ``type`` 是该节点的**官方任务类型**（``MT_EXTERMINATION`` / ``歼灭``）：
    赏金卡上扎里曼 / 解剖圣所 / 1999 的档位只有 oracle 给的节点 key，
    任务类型只能从这里反查 —— 这是唯一权威来源（solNodes.json 的 ``type``
    是英文且个别节点与官方导出不一致，别用它）。
    """
    out: dict[str, dict] = {}
    for key, info in regions.items():
        if not isinstance(info, dict):
            continue
        name_key = info.get("name") or ""
        sys_key = info.get("systemName") or ""
        name = lang.get(name_key) or name_key.rsplit("/", 1)[-1]
        system = lang.get(sys_key) or sys_key.rsplit("/", 1)[-1]
        if not name:
            continue
        rec: dict = {"name": name, "system": system}
        lo, hi = info.get("minEnemyLevel"), info.get("maxEnemyLevel")
        if isinstance(lo, int) and isinstance(hi, int) and lo > 0:
            rec["level"] = f"{lo}-{hi}" if hi != lo else f"{lo}"
        mt = info.get("missionType") or ""
        if mt:
            rec["mt"] = mt
            mt_zh = lang.get(info.get("missionName") or "")
            if mt_zh:
                rec["type"] = mt_zh
        out[key] = rec
    return out


def build_bounty_jobs(bounties: dict, lang: dict) -> dict:
    """赏金 job 资产路径（小写） -> {name, desc, final, stages, endless}。

    数据源 ``ExportBounties.json``。这是 DE 侧赏金的**官方赏金名 + 目标描述 +
    阶段结构**，比旧的手工表 ``bounty_job_names.json`` 完整得多：

      * ``name``  —— 赏金官方名（「尘土部队」/「核心样本」…，旧表缺的条目会空着或留英文）
      * ``desc``  —— 目标描述（一句话说明这条赏金要干什么）
      * ``final`` —— **最后一阶段**的遭遇战类型（``DynamicExcavation`` 等）。
        赏金的任务类型取它：DE 的 job 只有 ``jobType`` 资产路径、**不带节点**，
        所以拿不到节点级任务类型，末阶段就是这条赏金的可识别任务类型
        （实测与官方名一致：核心样本→挖掘、粉碎邪教→歼灭、猎人杀手→歼灭）。
      * ``stages`` —— 阶段数（用于「N 阶段」提示）。

    名字里 Narmer 变体会带「（合一众）」后缀，卡面另有 ``｜合一众`` 标签，
    这里**去掉后缀**避免重复。
    """
    out: dict[str, dict] = {}
    for key, info in bounties.items():
        if not isinstance(info, dict):
            continue
        rec: dict = {}
        nm = lang.get(info.get("name") or "")
        if nm:
            rec["name"] = nm.replace("（合一众）", "").strip()
        ds = lang.get(info.get("description") or "")
        if ds:
            rec["desc"] = ds.strip()
        stages = info.get("stages") or []
        if stages:
            rec["stages"] = len(stages)
            last = stages[-1] or []
            codes = [x.rsplit("/", 1)[-1] for x in last]
            if codes:
                rec["final"] = codes
        if rec:
            out[key.lower()] = rec
    return out


def build_mission_types(mt: dict, lang: dict) -> dict:
    """MT_XXX -> 中文名（官方词表直出）。"""
    out = {}
    for key, info in mt.items():
        nk = (info or {}).get("name")
        if nk and lang.get(nk):
            out[key] = lang[nk]
    return out


def build_challenges(ch: dict, lang: dict) -> dict:
    """挑战资产路径 -> {name, desc, count}（已解析为中文）。"""
    out = {}
    for key, info in ch.items():
        if not isinstance(info, dict):
            continue
        rec: dict = {}
        nk = info.get("name") or ""
        dk = info.get("description") or ""
        if lang.get(nk):
            rec["name"] = lang[nk]
        if lang.get(dk):
            rec["desc"] = lang[dk]
        if info.get("requiredCount") is not None:
            rec["count"] = info["requiredCount"]
        if rec.get("name") or rec.get("desc"):
            out[key] = rec
    return out


def build_named_set(export: dict, lang: dict, *, limit: int = 9000) -> list[str]:
    """导出表里每条的 ``name`` 语言键 -> 中文名集合（用于「这是 MOD / 蓝图吗」判定）。"""
    out: set[str] = set()
    for info in export.values():
        if not isinstance(info, dict):
            continue
        nk = info.get("name")
        if nk and lang.get(nk):
            out.add(lang[nk])
        if len(out) >= limit:
            break
    return sorted(out)


def build_recipe_names(recipes: dict, name_zh: dict, limit: int = 9000) -> list[str]:
    """可制造物品名集合（ExportRecipes.resultType -> 官方简中名）。

    ``ExportRecipes`` 没有 name 字段，只有 ``resultType`` 资产路径；
    用 ``name_zh.json``（小写路径 -> 中文）反查即可得到「XX 蓝图」去掉后缀的主体。
    """
    out: set[str] = set()
    for info in recipes.values():
        if not isinstance(info, dict):
            continue
        rt = (info.get("resultType") or "").strip().lower()
        if not rt:
            continue
        nm = name_zh.get(rt)
        if not nm and "/storeitems/" in rt:
            nm = name_zh.get(rt.replace("/storeitems/", "/types/"))
        if nm:
            out.add(nm)
        if len(out) >= limit:
            break
    return sorted(out)


def build_factions(factions: dict, lang: dict, fallback: dict) -> dict:
    """派系显示名（官方简中）。

    国际服简中的实际规则就是 DE 导出表的样子：
      Grineer / Corpus / Infestation / SENTIENT 保留英文，
      奥罗金 / 合一众 / 低语者 / 炽蛇军 / 科腐者 / 血色面纱 有中文译名。
    所以这里**直接采用官方表**，不再手工维护 _FACTION_CN 例外表。
    """
    out = {}
    for code, info in factions.items():
        nk = (info or {}).get("name")
        val = lang.get(nk) if nk else None
        if not val:
            val = (fallback.get(code) or {}).get("value")
        if val:
            out[code] = {"value": val}
    return out


def build_acrithis(vendors: dict, lang: dict,
                   name_zh: dict | None = None) -> dict:
    """言录使（Acrithis）的**商品池**。

    ⚠️ DE **不下发**每周实际卖哪 5 件：``AcrithisVendorManifest`` 是「池子 +
    权重」，``isOneBinPerCycle`` 表示每个槽位（bin）每周期只出一个，
    ``numRandomItemPrices`` 表示价格也是每周期随机roll的。
    所以这里只能给出「每个槽位会出什么、权重多少、价格区间」，
    本周实际货单请以游戏内为准（参考机器人那张卡是它自己维护的快照）。

    bin 2 = **周常池**（durationHours 168，Forma / 赤毒 / 裂罅 MOD / 赋能槽
    连接器 / 催化剂反应堆 这类），bin 0/1/3 = 每日池。
    """
    manifest = "/Lotus/Types/Game/VendorManifests/Duviri/AcrithisVendorManifest"
    m = vendors.get(manifest) or {}
    name_zh = name_zh or {}
    bins: dict[int, list] = {}
    for it in m.get("items") or []:
        si = (it.get("storeItem") or "").rstrip("/")
        tail = si.rsplit("/", 1)[-1]
        low = si.lower()
        nm = (name_zh.get(low) or name_zh.get(low.replace("/storeitems/",
                                                        "/types/")) or tail)
        rec = {"name": nm, "en": tail,
               "qty": it.get("quantity", 1),
               "pct": round(float(it.get("probability", 0)) * 100)}
        if it.get("itemPrices"):
            rec["price"] = "/".join(
                f"{p.get('ItemCount')}{'' }" for p in it["itemPrices"])
        bins.setdefault(int(it.get("bin", 0)), []).append(rec)
    prices = m.get("randomItemPricesPerBin") or []
    return {
        "manifest": manifest,
        "numItems": m.get("numItems"),
        "oneBinPerCycle": m.get("isOneBinPerCycle"),
        "priceRange": {str(k): v for k, v in enumerate(prices)},
        "bins": {str(k): v for k, v in sorted(bins.items())},
        "summary": f"{sum(len(v) for v in bins.values())} 件 / "
                   f"{len(bins)} 个槽位",
    }


def main() -> None:
    check_only = "--check" in sys.argv
    # --only nodes,bounties：只从 ExportRegions / ExportBounties 重建这两张表，
    # 词表直接用本地 languages_zh.json（不重抓 3MB 词表，避免顺带动了别的产物）。
    only = ""
    for a in sys.argv[1:]:
        if a.startswith("--only"):
            only = a.split("=", 1)[1] if "=" in a else "nodes,bounties"
    if only:
        lang = json.loads((DATA / "languages_zh.json").read_text(encoding="utf-8"))
        wanted = {x.strip() for x in only.split(",") if x.strip()}
        print("局部重建：", "、".join(sorted(wanted)))
        if "nodes" in wanted:
            regions = fetch(REGIONS_URL)
            nodes = build_nodes(regions, lang)
            print(f"  节点 {len(nodes)} 条｜带任务类型 "
                  f"{sum(1 for v in nodes.values() if v.get('type'))}")
            if not check_only:
                write_json(DATA / "nodes_zh.json", nodes)
        if "bounties" in wanted:
            bounties = fetch(BOUNTIES_URL)
            jobs = build_bounty_jobs(bounties, lang)
            print(f"  赏金 {len(jobs)} 条｜带末阶段类型 "
                  f"{sum(1 for v in jobs.values() if v.get('final'))}")
            if not check_only:
                write_json(DATA / "bounty_jobs_zh.json", jobs)
        if "acrichis" in wanted:
            vendors = fetch(VENDORS_URL)
            pool = build_acrithis(vendors, lang)
            print(f"  言录使池：{pool.get('summary', '')}")
            if not check_only:
                write_json(DATA / "acrichis_pool.json", pool)
        print("完成 ✔")
        return

    print("抓取官方导出 ...")
    dict_zh = fetch(DICT_URL)
    oracle = fetch(ORACLE_DICT)
    regions = fetch(REGIONS_URL)
    bounties = fetch(BOUNTIES_URL)
    missions = fetch(MISSIONS_URL)
    challenges = fetch(CHALLENGES_URL)
    upgrades = fetch(UPGRADES_URL)
    recipes = fetch(RECIPES_URL)
    factions = fetch(FACTIONS_URL)

    lang = build_languages(dict_zh, oracle)
    nodes = build_nodes(regions, lang)
    bounty_jobs = build_bounty_jobs(bounties, lang)
    mts = build_mission_types(missions, lang)
    chs = build_challenges(challenges, lang)
    mods = build_named_set(upgrades, lang)
    name_zh = json.loads((DATA / "name_zh.json").read_text(encoding="utf-8"))
    recipe_names = build_recipe_names(recipes, name_zh)
    try:
        old_factions = json.loads((DATA / "factionsData.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        old_factions = {}
    fac = build_factions(factions, lang, old_factions)

    print(f"词表 {len(lang)} 条｜节点 {len(nodes)} 条（含等级 "
          f"{sum(1 for v in nodes.values() if v.get('level'))}）｜"
          f"任务类型 {len(mts)} 条｜挑战 {len(chs)} 条｜"
          f"MOD 名 {len(mods)} 条｜可制造物 {len(recipe_names)} 条｜"
          f"派系 {len(fac)} 条")

    if check_only:
        return

    write_json(DATA / "languages_zh.json", lang)
    write_json(DATA / "nodes_zh.json", nodes)
    write_json(DATA / "bounty_jobs_zh.json", bounty_jobs)
    write_json(DATA / "mission_types_zh.json", mts)
    write_json(DATA / "challenges_zh.json", chs)
    write_json(DATA / "mod_names_zh.json", mods)
    write_json(DATA / "recipe_names_zh.json", recipe_names)
    write_json(DATA / "factionsData.json", fac)
    print("完成 ✔")


if __name__ == "__main__":
    main()
