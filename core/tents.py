# -*- coding: utf-8 -*-
"""夜灵平野「小帐篷」赏金推算（TentA/B/C）。

背景：``CetusSyndicate`` 只下发 7 档赏金（Konzu 5 档 + 合一众 + 钢铁之路），
**不下发**平野三处小帐篷（TentA/B/C）当前挂的是哪几条链（全库实测无
camp/tent 字段，``Nodes`` 为空）。但该归属是**确定性**的：

  · 数据：``core/data/de/eidolon_job_manifest.json`` —— DE 的
    ``/Lotus/Types/Game/EidolonJobManifest`` 资产（每个帐篷一份 13 条链的
    候选表 + ``NumJobsToShow=3``）。PEP 未收录该文件；本副本取自社区镜像
    github.com/calamity-inc/wf.browse.oracle 的 ``data/EidolonJobManifest.json``。
  · 算法：同仓库 ``location-bounties.pluto`` 对游戏脚本 ``SRandomInt`` /
    ``SeededShuffleTable`` 的公开复刻（PCG 型 64 位 LCG）：以 worldstate 的
    ``CetusSyndicate.Seed`` 为种子，把「前 N 个 True」的布尔数组洗牌，再按
    清单原顺序取出命中的链。种子在清单各点位之间**连续推进**、不重置。

验证（2026-09-24 两路独立对拍）：
  · W4 窗口 seed=45267 与沃沃截图（17:10 CST）**9/9 格**逐格一致；
  · W5 窗口 seed=69703 与 oracle.browse.wf/location-bounties 的独立实现
    **15/15 点位**（地球 3 + 金星 7 + 火卫二 5）逐格一致。

金星 7 点位 / 火卫二 5 点位的算法同样验证通过；两者**都不上卡**（2026-09-26 定稿）：
金星点位名 7 个里只有 5 个有官方译名、且用户要求金星卡改列深矿钢铁档奖励，火卫二则
0/5 无译名。补名/要开时只需把 :data:`REGIONS` 里对应那段取消注释。
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Optional

DATA_DIR = Path(__file__).resolve().parent / "data" / "de"

# 地区（SyndicateMissions 的显示名）-> 点位清单配置。
# ``labels`` 把 DE 的内部 LocationTag 换成卡面显示名；缺省用原名。
REGIONS: dict[str, dict] = {
    "Ostrons": {
        "prefix": "/Lotus/Types/Gameplay/Eidolon/Jobs/",
        "manifest": "eidolon_job_manifest.json",
        "labels": {"TentA": "小帐篷 A", "TentB": "小帐篷 B", "TentC": "小帐篷 C"},
    },
    # ★ 金星：推算能力与点位名**保留**（测试仍覆盖），2026-09-26 起**不上卡**
    #   （用户口径：金星卡改列深矿钢铁档奖励）。`on_card=False` 只挡卡面出块，
    #   不影响 `region_locations()` 取值 —— 要重新上卡把这里改回 True 即可。
    #   点位与任务链取自 oracle 的 `data/VenusJobManifest.json`（构建期抽成随包文件
    #   `venus_job_manifest.json`，见 `scripts/build_venus_points.py`），7 个点位与
    #   种子推算口径已在 2026-09-24 与 oracle 独立实现对拍 **15/15 一致**；
    #   点位中文名取自**官方简中表** `/Lotus/Language/SolarisVenus/MapLabel_*`
    #   （7 个里 5 个有名字；Warehouse / DigSite 官方未译 → 显示「未定名」，**不硬编**）。
    #   链名走 `bounty_jobs_zh.json`（同表 27 条金星任务链，如 VenusArtifactJobAmbush=伏击信使）。
    "Solaris United": {
        "prefix": "/Lotus/Types/Gameplay/Venus/Jobs/",
        "manifest": "venus_job_manifest.json",
        "on_card": False,
        "labels": {
            "BountyNefsHead": "殿宇建造地",
            "BountyRepairBay": "中央维修点",
            "BountySunDial": "反应器控制点",
            "BountyPufferFarm": "生长地点",
            "BountyCoolantCoil": "珍珠",
            "BountyWarehouse": "未定名",
            "BountyDigSite": "未定名",
        },
    },
    # 火卫二：官方简中表里 **0/5** 点位名（ChamberA/B/C、TentA/B 都无 MapLabel）→
    # 按用户口径**暂不上卡**，等官方补译（补名后照上面加一项即可）。
}


def on_card(region: str) -> bool:
    """该地区是否上卡（配置项 ``on_card``，缺省 True）。

    与「有没有配置」分开：金星有完整配置（推算 + 点位名 + 测试）但当前不上卡。
    """
    return bool((REGIONS.get(region or "") or {}).get("on_card", True))

_MASK64 = (1 << 64) - 1
_MULT = 0x5851F42D4C957F2D      # SRandomInt 的 LCG 常量（PCG 同款）
_INCR = 0x14057B7EF767814F


@lru_cache(maxsize=None)
def _manifest(filename: str) -> dict:
    path = DATA_DIR / filename
    try:
        return (json.loads(path.read_text(encoding="utf-8")).get("data") or {})
    except Exception:  # noqa: BLE001
        return {}


@lru_cache(maxsize=1)
def _job_names() -> dict:
    """链资产路径（小写）-> 官方简中链名（与 de_worldstate 同一份表）。"""
    try:
        return json.loads((DATA_DIR / "bounty_jobs_zh.json")
                          .read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _chain_name(path: str) -> str:
    tail = path.rstrip("/").rsplit("/", 1)[-1]
    return _job_names().get(path.lower(), {}).get("name") or tail


def _sri(seed: int, lo: int, hi: int) -> tuple[int, int]:
    """SRandomInt：返回 (新种子, [lo, hi] 内的取样)。"""
    diff = hi - lo
    if diff:
        seed = (_MULT * seed + _INCR) & _MASK64
        lo += (((seed >> 32) & 0xFFFFFFFF) & 0x3FFFFFFF) % (diff + 1)
    return seed, lo


def _shuffle(seed: int, flags: list) -> tuple[int, list]:
    """SeededShuffleTable：就地洗牌（Fisher–Yates 走法，rand 取 [1, size]）。"""
    size = len(flags)
    while size >= 2:
        seed, rand = _sri(seed, 1, size)
        i, j = rand - 1, size - 1
        flags[i], flags[j] = flags[j], flags[i]
        size -= 1
    return seed, flags


def _pick(seed: int, jobs: list, num: int) -> tuple[int, list]:
    """从候选链里按种子选出 ``num`` 条（保持清单原顺序）。"""
    flags = [i < num for i in range(len(jobs))]
    seed, flags = _shuffle(seed, flags)
    return seed, [jobs[i] for i in range(len(jobs)) if flags[i]]


def region_locations(region: str, seed: Optional[int]) -> list[tuple[str, list[str]]]:
    """某地区各点位的当前赏金：``[(显示名, [链名, ...]), ...]``。

    无配置 / 无种子 / 清单缺失时返回空列表（调用方直接跳过，不占卡面）。
    """
    cfg = REGIONS.get(region or "")
    if not cfg or seed is None:
        return []
    try:
        s = int(seed)
    except (TypeError, ValueError):
        return []
    data = _manifest(cfg["manifest"])
    out: list[tuple[str, list[str]]] = []
    for loc in data.get("LocationSpecificJobs") or []:
        s, picked = _pick(s, loc.get("Jobs") or [], int(loc.get("NumJobsToShow") or 0))
        tag = loc.get("LocationTag") or ""
        label = (cfg.get("labels") or {}).get(tag, tag)
        out.append((label, [_chain_name(cfg["prefix"] + p) for p in picked]))
    return out


def seed_of(syndicates) -> Optional[int]:
    """从解析后的 SyndicateMissions 里取世界种子（各 Syndicate 同值）。"""
    for s in syndicates or []:
        if s.get("syndicateKey") == "CetusSyndicate" and s.get("seed") is not None:
            return int(s["seed"])
    return None
