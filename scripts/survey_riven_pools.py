# -*- coding: utf-8 -*-
"""抽样：WM 在售紫卡里每个家族**实际出现过哪些词条**（「词条范围」的证据来源之一）。

用法：
    python scripts/survey_riven_pools.py --hot            # 只抽「热门且缺证据」的家族
    python scripts/survey_riven_pools.py --families Nikana,Glaive
    python scripts/survey_riven_pools.py --hot --limit 3  # 先小样试跑
    python scripts/survey_riven_pools.py --hot --write    # 写 core/data/de/riven_pools_survey.json

★ 方法学照搬 wfsim `scripts/survey_riven_pools.py`（它踩过的坑写在那里，别重犯）：
  ① **逐词条带过滤查**，不要「拉一页家族挂单再数」—— 端点有返回条数上限且**按排序截断**
     （实测单次上限 100 条），拉一页拿到的是「最便宜的 100 张」，不是抽样。带
     `positive_stats=` / `negative_stats=` 的过滤在**服务端对全量挂单**执行，才是计数。
  ② **家族 slug = 母武器**：`nikana_prime` 会被端点 400 拒掉，`nikana` 才对（一张紫卡
     全家族通用，市场按母武器索引）。
  ③ **判据用家族自身的中位数**：市场深度差二十倍（同一职业里多数词条都会出），
     拿固定阈值比会让浅市场的家族全判「never」。
       count > 中位 × 0.25  ⇒ rollable
       count ≤ 中位 × 0.05  ⇒ never
       其余                  ⇒ unclear（引擎保留自己的推导）
  ④ **速率**：WM ≤ 3 req/s，连发会被拒/返回空 ⇒ 每次请求间隔 `SLEEP` 秒 + 失败退避重试。

输出（`core/data/de/riven_pools_survey.json`，由 `build_riven_pools.py` 合并进证据表）::

    {"_surveyed": "…", "_method": "…",
     "families": {"<wf 家族名>": {"n": int, "counts": {slug: int},
                                  "rollable": [...], "never": [...], "unclear": [...]}}}
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DEFAULT = ROOT / "core" / "data" / "de" / "riven_pools_survey.json"
DISPOSITIONS = ROOT / "core" / "data" / "dispositions_rivenmirror.json"
POOLS = ROOT / "core" / "data" / "de" / "riven_pools.json"

API = "https://api.warframe.market/v1/auctions/search?type=riven&weapon_url_name=%s"
HEADERS = {
    "Language": "zh-hans",
    "Platform": "pc",
    "Crossplay": "true",
    "User-Agent": "wf-sdjk-riven-survey/1.0",
}

# 我方词条 id → WM 属性 slug（WM `/v2/riven/attributes` 的 slug 字段；50 个）。
# 只列本脚本会查的（近战池 + 枪池里近战也用得上的少数）。
SLUG = {
    "melee_damage": "base_damage_/_melee_damage",
    "attack_speed": "fire_rate_/_attack_speed",
    "fire_rate": "fire_rate_/_attack_speed",
    "crit_chance": "critical_chance",
    "crit_damage": "critical_damage",
    "status_chance": "status_chance",
    "status_duration": "status_duration",
    "status_damage": "status_damage",
    "range": "range",
    "initial_combo": "channeling_damage",
    "combo_duration": "combo_duration",
    "heavy_attack_efficiency": "channeling_efficiency",
    "extra_combo_count": "chance_to_gain_extra_combo_count",
    "combo_gain_chance": "chance_to_gain_combo_count",
    "slide_crit": "critical_chance_on_slide_attack",
    "finisher_damage": "finisher_damage",
    "melee_heavy_attack_charge": "heavy_attack_wind_up_speed",
    "melee_heavy_attack_damage": "melee_damage_on_heavy_attack",
    "melee_parry_angle": "parry_angle",
    "melee_slam_damage": "slam_attack_damage",
    "impact_damage": "impact_damage",
    "puncture_damage": "puncture_damage",
    "slash_damage": "slash_damage",
    "heat_damage": "heat_damage",
    "cold_damage": "cold_damage",
    "toxin_damage": "toxin_damage",
    "electric_damage": "electric_damage",
    "damage_vs_grineer": "damage_vs_grineer",
    "damage_vs_corpus": "damage_vs_corpus",
    "damage_vs_infested": "damage_vs_infested",
    "multishot": "multishot",
    "magazine_capacity": "magazine_capacity",
    "ammo_max": "ammo_maximum",
    "reload_speed": "reload_speed",
    "recoil": "recoil",
    "zoom": "zoom",
    "projectile_speed": "projectile_speed",
    "punch_through": "punch_through",
}

# WM 标了 `positiveOnly: true` 的 slug —— 拿它们当 negative 查必然空，白耗一次请求
# （`/v2/riven/attributes` 实测，2026-10-23）。
POSITIVE_ONLY = {
    "punch_through",
    "toxin_damage",
    "channeling_damage",
    "heat_damage",
    "electric_damage",
    "cold_damage",
    "chance_to_gain_extra_combo_count",
    "blast",
    "corrosive",
    "gas",
    "heavy_attack_wind_up_speed",
    "magnetic",
    "melee_damage_on_heavy_attack",
    "parry_angle",
    "radiation",
    "slam_attack_damage",
    "viral",
}

# 「**只出负面**」的 slug（DE 导出的 `bonus: false`）—— 拿它当 positive 查会被 400 拒掉
# （2026-10-23 实测：`positive_stats=chance_to_gain_combo_count` 恒 400），只能查负面。
NEGATIVE_ONLY = {"chance_to_gain_combo_count"}

ROLLS_ABOVE = 0.25
NEVER_BELOW = 0.05
PHYSICAL_STATS = ("impact_damage", "puncture_damage", "slash_damage")
PAGE_CAP = 100  # 端点单页返回上限（实测）：取满这一页说明家族挂单更多，页内不能当抽样
MIN_SAMPLE = 50  # 判「刷不出」的最低在售样本数（指令 2026-10-23 口径）
SLEEP = 6.0  # 秒/请求（≈0.17 req/s）—— 实测 2.5s 仍频繁 429，WM 的窗口比标称的严
TRIES = 5


def _count(slug: str, extra: str = "", tries: int | None = None) -> tuple[int | None, str]:
    """带过滤条件的挂单计数。``(None, 原因)`` = 服务端始终没答。"""
    err = "?"
    for attempt in range(tries or TRIES):
        try:
            url = (API % slug) + extra
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=30) as r:
                return len(json.load(r)["payload"]["auctions"]), ""
        except urllib.error.HTTPError as e:
            err = f"HTTP {e.code}"
            if e.code == 400:  # 参数不被接受（slug 写错 / 该家族无此词条）⇒ 重试无意义
                return None, err
        except Exception as e:  # noqa: BLE001 - 429/超时是常态
            err = str(e)[:60]
        time.sleep(12 + 10 * attempt)  # 429 是常态，退避要够长（最长 ~52s）
    return None, err


def _families_without_evidence() -> list[str]:
    """热门（原倾向 ≤0.7）且证据表里**没有任何家族证据**的 wfsim 家族名。"""
    sys.path.insert(0, str(ROOT))
    from core import matching

    pools = json.loads(POOLS.read_text(encoding="utf-8")).get("families") or {}
    disp = json.loads(DISPOSITIONS.read_text(encoding="utf-8")).get("entries") or {}
    out: dict[str, float] = {}
    for en, e in disp.items():
        d = e.get("disposition")
        if d is None or d > 0.7:
            continue
        root = matching.family_key(en)
        if not root:
            continue
        v = pools.get(root) or {}
        if v.get("never") or v.get("rolls") or v.get("survey"):
            continue
        wf = v.get("wf") or en
        out.setdefault(wf, d)
    return [k for k, _ in sorted(out.items(), key=lambda kv: kv[1])]


def _families_without_any_evidence() -> list[str]:
    """证据表里**既无 wfsim 证据也无抽样**的全部家族（不限「热门」）。

    2026-10-23 第二批（指令：剩余家族补证据）：`--hot` 只覆盖「原倾向 ≤0.7」，
    冷门 / 新武器被漏掉；这一档按证据表全量取。
    """
    pools = json.loads(POOLS.read_text(encoding="utf-8")).get("families") or {}
    out = []
    for v in pools.values():
        if v.get("never") or v.get("rolls") or v.get("survey"):
            continue
        wf = v.get("wf")
        if wf:
            out.append(wf)
    return sorted(out)


def _wf_of(family: str) -> str:
    """我方家族根/英文名 → wfsim 家族名（= 母武器英文名 = 市场 slug 的来源）。"""
    sys.path.insert(0, str(ROOT))
    from core import matching

    pools = json.loads(POOLS.read_text(encoding="utf-8")).get("families") or {}
    root = matching.family_key(family)
    v = pools.get(root) or pools.get(family) or {}
    return v.get("wf") or family


def _slug_of(wf: str) -> str:
    """wfsim 家族名 → 市场 url_name。

    ★ 不能只做「空格/连字符 → 下划线」的机械转换：`EFV-0 Somnus` 的市场 slug 是
    **`efv-0_somnus`**（连字符保留），机械转换会拼成 `efv_0_somnus` 被端点 400 拒掉
    （2026-10-23 实测）。先查倾向表的 `url_name`（WM 真值），查不到再回落机械转换。
    """
    try:
        disp = json.loads(DISPOSITIONS.read_text(encoding="utf-8")).get("entries") or {}
        e = disp.get(wf) or {}
        if e.get("url_name"):
            return e["url_name"]
    except Exception:  # noqa: BLE001
        pass
    return wf.lower().replace(" ", "_")


def _page(slug: str) -> tuple[list | None, str]:
    """取**不带过滤**的一页挂单（最多 ``PAGE_CAP`` 条）。``(None, 原因)`` = 服务端没答。"""
    err = "?"
    for attempt in range(TRIES):
        try:
            req = urllib.request.Request(API % slug, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)["payload"]["auctions"], ""
        except urllib.error.HTTPError as e:
            err = f"HTTP {e.code}"
            if e.code == 400:
                return None, err
        except Exception as e:  # noqa: BLE001
            err = str(e)[:60]
        time.sleep(12 + 10 * attempt)
    return None, err


def _inv_slug(stats: list[str]) -> dict[str, str]:
    """市场 slug → 我方词条 id（只在本轮要查的 ``stats`` 内反查，避免一 slug 对多 id）。"""
    return {SLUG[s]: s for s in stats}


def _census(rows: list, stats: list[str]) -> dict[str, int]:
    """**全量普查**：一页就是该家族的全部在售（``len(rows) < PAGE_CAP`` 时成立），
    直接数每个词条出现在多少张卡上（正负合计）。

    ★ 比逐词条过滤查**更准**（不是抽样而是清点），且只花 1 次请求。家族挂单
    ≥ ``PAGE_CAP`` 时这一页只是「最便宜的一百张」，必须退回逐词条过滤查
    （wfsim 的脚本头用 Boar 的 2 vs 31 条记录过这个坑）。
    """
    inv = _inv_slug(stats)
    counts = dict.fromkeys(stats, 0)
    for a in rows:
        for at in (a.get("item") or {}).get("attributes") or []:
            sid = inv.get(at.get("url_name") or "")
            if sid:
                counts[sid] += 1
    return counts


def _class_of(members: list[str]) -> str:
    """家族类别 → 基值列名（判「规则是否也排除该词条」用）。"""
    from core import riven_pools as RP

    cats = {(r.get("category") or "").lower() for r in RP._family_weapons(members)}
    for cat, cls in (
        ("melee", "melee"),
        ("archgun", "archgun"),
        ("sentinel", "rifle"),
        ("primary", "rifle"),
        ("secondary", "pistol"),
    ):
        if cat in cats:
            return cls
    return ""


def _stats_for(wf: str) -> list[str]:
    """该家族该查哪些词条：**按它自己的类别池**取（近战查近战池、副手查手枪池）。

    ★ 不能一律用近战池：`Acrid` 是副手，拿近战池查会漏掉它真正有的枪类词条
    （弹匣 / 弹药 / 变焦…）且多查它根本没有的近战词条（2026-10-23 实测）。
    """
    try:
        sys.path.insert(0, str(ROOT))
        from core import riven_pools as RP

        _root, members = RP.family_members(wf)
        cls = _class_of(members) or "melee"
        pool = RP.class_pool(cls)
        return sorted(sid for sid in SLUG if sid in pool)
    except Exception:  # noqa: BLE001
        return sorted(SLUG)


def _spliced_of(wf: str) -> set:
    """该家族类别池里的**融合词条**（`spliced: true`）—— 见 `survey()` 里的说明。"""
    try:
        sys.path.insert(0, str(ROOT))
        from core import riven_pools as RP

        _root, members = RP.family_members(wf)
        cls = _class_of(members) or "melee"
        pool = RP.class_pool(cls)
        return {sid for sid, v in pool.items() if (v or {}).get("spliced")}
    except Exception:  # noqa: BLE001
        return set()


def _rule_excluded(wf: str) -> set:
    """规则推导出的排除项（用于「只出现 1 次且与规则一致 ⇒ 也算刷不出」这一档）。"""
    try:
        sys.path.insert(0, str(ROOT))
        from core import riven_pools as RP

        root, members = RP.family_members(wf)
        cls = _class_of(members)
        return RP.excluded_for(cls, root, members) if cls else set()
    except Exception:  # noqa: BLE001
        return set()


def survey(
    fams: list[str],
    stats: list[str] | None = None,
    pause: float = 0.0,
    sink=None,
    sweep: bool = True,
) -> dict:
    """逐家族抽样。``sink`` 非空时**每抽完一个家族就落一次盘**（长跑可中断续跑）。

    两条路：
      · **普查**（挂单 < ``PAGE_CAP``）：一页 = 全部在售，直接数（1 次请求，精确）；
      · **逐词条过滤**（挂单 ≥ ``PAGE_CAP``）：`positive_stats=` / `negative_stats=`
        逐词条查，服务端对全量过滤（一次页会被排序截断，不能用来数）。
    判据（指令 2026-10-23 口径）：样本 ≥ ``MIN_SAMPLE`` 才判 `never`；样本不足只记
    ``insufficient_sample``，词条照列、卡上标「?」。`never` = 计数 0，或计数 1 且
    规则也排除它（对齐 Boar「500 张 1 张变焦」那条口径）。
    """
    out: dict = {}
    for wf in fams:
        fam_stats = stats or _stats_for(wf)  # 未显式给就按该家族类别池
        slug = _slug_of(wf)
        rows, err = _page(slug)
        time.sleep(SLEEP)
        if rows is None:
            print(f"  {wf:22s} 跳过（{err}）", flush=True)
            continue
        n = len(rows)
        method = "census"
        # 页内计数（普查）。**饱和市场**（取满一页）里这一页是「最便宜的一百张」，
        # 页内计数**不能用来判「刷不出」**，但**能用来判「能刷」**：一张真卡上出现过
        # 该词条，就证明它出得来 —— 页面有偏只会漏，不会凭空造。
        counts = _census(rows, fam_stats)
        valid_absence = set(counts) if n < PAGE_CAP else set()
        if n >= PAGE_CAP and sweep:
            # 只对**物理三项**补全量过滤查（判「刷不出」用）：物理正是 25% 份额规则
            # 两个方向都会错的地方，其余词条的可用性由职业池 + 规则兜住，不必逐个扫
            # （47 族里 8 族饱和，全扫要 400+ 请求且会被 429 打成筛子）。
            method = "census+sweep"
            for sid in [s for s in fam_stats if s in PHYSICAL_STATS]:
                ms = SLUG[sid]
                pos, e1 = _count(slug, f"&positive_stats={ms}")
                time.sleep(SLEEP)
                if pos is None:  # 查询失败绝不记 0（首版把 429 吞成 0 ⇒ 整族假 never）
                    print(f"    {sid:26s} 跳过（正例查询失败：{e1}）", flush=True)
                    continue
                neg, e2 = _count(slug, f"&negative_stats={ms}")
                time.sleep(SLEEP)
                if neg is None:
                    print(f"    {sid:26s} 跳过（负例查询失败：{e2}）", flush=True)
                    continue
                counts[sid] = pos + neg
                valid_absence.add(sid)
            # 物理三项若有漏（429），**等一轮再补一次**：这一档正是「25% 规则判不了」
            # 的地方，缺了它这张家族的证据就只剩规则推导。
            for sid in [s for s in fam_stats if s in PHYSICAL_STATS and s not in valid_absence]:
                time.sleep(SLEEP * 2)
                ms = SLUG[sid]
                pos, _e1 = _count(slug, f"&positive_stats={ms}", tries=2)
                time.sleep(SLEEP)
                if pos is None:
                    continue
                neg, _e2 = _count(slug, f"&negative_stats={ms}", tries=2)
                time.sleep(SLEEP)
                if neg is None:
                    continue
                counts[sid] = pos + neg
                valid_absence.add(sid)
        if not counts:
            print(f"  {wf:22s} 全部查询失败，跳过", flush=True)
            continue
        entry: dict = {
            "n": n,
            "method": method,
            "absence_checked": sorted(valid_absence),
            "counts": {k: counts[k] for k in sorted(counts)},
        }
        if n < MIN_SAMPLE:
            # 样本不足 ⇒ 不下任何判据（词条照列、卡上标「?」由运行期负责）
            entry["insufficient_sample"] = True
            entry["rollable"] = []
            entry["never"] = []
            entry["unclear"] = sorted(counts)
            print(f"  {wf:22s} 挂单={n:<4d} [{method}] 样本 <{MIN_SAMPLE} ⇒ 只记样本少", flush=True)
        else:
            rule = _rule_excluded(wf)
            # ★ 融合词条（Update 44 Riven Splicer，2026-10-07 上线）**不判 never**：
            #   在售挂单是「新旧混合」的存量，这批词条上线才半个月 ⇒ 缺席主要是
            #   **太新 / 太少**，不是刷不出（2026-10-23 实测：Ankyros 69 张里 5 个
            #   融合词条全 0；照判就会把 5 个能刷的词条错标成「刷不出」）。
            spliced = _spliced_of(wf)
            # ★ **never 只判物理三项**。非物理词条的可用性由职业池（DE 导出）+ 规则决定，
            #   抽样再判一层只会引入风险：样本里偶发缺席就会被读成「刷不出」，而
            #   wfsim 的告诫正是「拒绝玩家真实持有的词条」是最糟的失败方向
            #   （2026-10-23 实测：双短柄战镰 53 张里冰伤 0 次 —— 元素明明能刷，
            #   只是那张卡的样本里没出现）。
            never = sorted(
                k
                for k, v in counts.items()
                if k in PHYSICAL_STATS
                and k in valid_absence
                and k not in spliced
                and (v == 0 or (v == 1 and k in rule))
            )
            rollable = sorted(k for k, v in counts.items() if v >= 2)
            unclear = sorted(set(counts) - set(never) - set(rollable))
            entry["rollable"] = rollable
            entry["never"] = never
            entry["unclear"] = unclear
            print(
                f"  {wf:22s} 挂单={n:<4d} [{method}] never={','.join(never) or '-':<30s}"
                f" unclear={','.join(unclear) or '-'}",
                flush=True,
            )
        out[wf] = entry
        if sink is not None:  # 每抽完一个家族落一次盘（长跑中断不丢已抽部分）
            sink(out)
    return out


def _recompute(out_path: Path) -> int:
    """按已存的计数与 absence_checked **离线重算**判据（换判据后不必重新联网抽样）。"""
    doc = json.loads(out_path.read_text(encoding="utf-8"))
    fams = doc.get("families") or {}
    changed = 0
    for wf, v in fams.items():
        # ★ 只重算**本轮**条目（有 `method` 字段）：早期一批用「家族自身中位数的
        #   0.25/0.05 两档」判据、**没有 absence_checked**，套新判据会把它们的
        #   never 全部清空（2026-10-23 实测：一跑就把旧 24 族的 18 条 never 抹掉）。
        if "counts" not in v or "method" not in v:
            continue
        counts = v["counts"]
        valid_absence = set(
            v.get("absence_checked") or (counts if v.get("method") == "census" else [])
        )
        if v.get("insufficient_sample"):
            v["rollable"], v["never"], v["unclear"] = [], [], sorted(counts)
            continue
        spliced = _spliced_of(wf)
        rule = _rule_excluded(wf)
        never = sorted(
            k
            for k, c in counts.items()
            if k in PHYSICAL_STATS
            and k in valid_absence
            and k not in spliced
            and (c == 0 or (c == 1 and k in rule))
        )
        rollable = sorted(k for k, c in counts.items() if c >= 2)
        unclear = sorted(set(counts) - set(never) - set(rollable))
        if (never, rollable, unclear) != (v.get("never"), v.get("rollable"), v.get("unclear")):
            changed += 1
        v["never"], v["rollable"], v["unclear"] = never, rollable, unclear
    out_path.write_text(
        json.dumps(doc, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"重算完成：{len(fams)} 族，判据有变化 {changed} 族")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hot", action="store_true", help="自动选「热门且缺证据」的家族")
    ap.add_argument("--all-missing", action="store_true", help="证据表里所有无证据的家族")
    ap.add_argument("--families", default="", help="逗号分隔的家族名（英文名或我方家族根）")
    ap.add_argument("--stats", default="", help="逗号分隔的词条 id（默认按每个家族的类别池自动选）")
    ap.add_argument("--limit", type=int, default=0, help="最多抽几个家族（试跑用）")
    ap.add_argument("--pause", type=float, default=0.0, help="家族之间额外停顿（秒）")
    ap.add_argument(
        "--no-sweep",
        action="store_true",
        help="饱和家族不跑物理补查（限流严重时先拿普查结果；补查可稍后单独重跑）",
    )
    ap.add_argument("--write", action="store_true", help="写证据覆盖层（默认只打印）")
    ap.add_argument("--out", default=str(OUT_DEFAULT))
    ap.add_argument(
        "--recompute",
        action="store_true",
        help="不联网：按已存的 counts/absence_checked 用当前判据重算 never/rollable（改判据后补跑用）",
    )
    args = ap.parse_args()

    if args.recompute:
        return _recompute(Path(args.out))

    if args.families:
        fams = [_wf_of(x.strip()) for x in args.families.split(",") if x.strip()]
    elif args.all_missing:
        fams = _families_without_any_evidence()
    elif args.hot:
        fams = _families_without_evidence()
    else:
        print("请给 --hot 或 --families", file=sys.stderr)
        return 2
    if args.limit:
        fams = fams[: args.limit]
    if not fams:
        print("没有需要抽样的家族（都有证据了）")
        return 0

    # 词条集：显式给就用它，否则**按每个家族自己的类别池**在循环内取
    stats = [x.strip() for x in args.stats.split(",") if x.strip()] or None
    print(
        f"抽样 {len(fams)} 个家族（挂单 <{PAGE_CAP} 的走一次全量普查、否则逐词条过滤；"
        f"间隔 {SLEEP}s）"
    )
    print("家族：", ", ".join(fams))

    out_path = Path(args.out)
    merged: dict = {}
    if out_path.exists():  # 续跑：保留已抽到的家族
        try:
            merged = dict(json.loads(out_path.read_text(encoding="utf-8")).get("families") or {})
        except Exception:  # noqa: BLE001
            merged = {}

    def _write(part: dict) -> None:
        """增量落盘（每抽完一个家族落一次）—— 长跑中断可续，别等全部跑完才写。"""
        if not args.write:
            return
        merged.update(part)
        doc = {
            "_surveyed": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "_method": (
                "warframe.market /v1/auctions/search；家族 slug = **母武器**"
                "（如 nikana 而非 nikana_prime，EFV-0 Somnus 的 slug 是 efv-0_somnus）。"
                "两条路：① **普查** —— 家族在售 <100 张时一页就是全部，直接数每个词条"
                "出现在多少张卡上（精确、1 次请求）；② **逐词条带过滤查** —— 挂单 ≥100 时"
                "一页会被排序截断（拿到的是最便宜的一百张），改查 "
                "positive_stats= / negative_stats=，服务端对全量过滤。"
                f"速率 {SLEEP}s/请求 + 指数退避（实测 1.2s 仍会被 429）。"
                f"判据（2026-10-23 指令口径）：样本 ≥{MIN_SAMPLE} 才判；`never` = 计数 0"
                "（或计数 1 且规则也排除它）；`rollable` = 计数 ≥2；其余 `unclear`。"
                f"样本 <{MIN_SAMPLE} 的家族只记 insufficient_sample（词条照列、卡上标「?」）。"
                "**融合词条（Update 44）不判 never** —— 在售是存量，它们上线才半个月，"
                "缺席是「太新」而非「刷不出」。"
                "★ 早期一批（无 method 字段）用「家族自身中位数的 0.25/0.05 两档」判，"
                "同 wfsim scripts/survey_riven_pools.py；两套判据的家族不重叠。"
            ),
            "_counts": {"families": len(merged)},
            "families": dict(sorted(merged.items())),
        }
        out_path.write_text(
            json.dumps(doc, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8"
        )

    res = survey(
        fams, stats, pause=args.pause, sink=_write if args.write else None, sweep=not args.no_sweep
    )
    print(f"\n完成 {len(res)}/{len(fams)} 个家族")
    if not args.write:
        print("（加 --write 才写盘）")
        return 0
    _write(res)
    print(f"写出 {out_path}（共 {len(merged)} 个家族）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
