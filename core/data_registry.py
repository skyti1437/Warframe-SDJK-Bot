# -*- coding: utf-8 -*-
"""随包数据登记表与统一健康判据（自维护性评估 §6 第 5 项）。

**用途**：把「每份 ``core/data`` 文件从哪来、由谁生成、什么时候该重建、谁在读」
登记成**一份可执行清单**，让面板、``scripts/run_checks.py health``、打包闸门
读**同一份判据**，把维护经验从口头约定变成代码。

本模块**只读**：不联网、不写盘、不 import astrbot（与 :mod:`core.paths` 同风格）。
单个文件坏 JSON 只影响那一行，不抛异常。

三类判据分开（**不给稳定表硬套日期**）：

* ``rotating``：按轮次/到期判定（效价窗口 ``valence``、货单 ``expiry``）；
* ``max_age``：按**内容时间**判定（``max_age_days`` + ``observed`` 点路径）；
* 其余（``on_upstream_change`` / ``manual_curated`` / ``static_asset`` /
  ``runtime_cache`` / ``unused``）：**不按时间判**，上游变了再重建
  （status = ``no_ttl``）——这正是「别对稳定别名表硬套 7 天 TTL」的落地。

``source`` / ``producer`` 查不到就写 ``None``，判定显示「来源未确认」
（status = ``unknown``，**永远不是 ok**）；不编造。

口径边界：``data_dir`` 默认 ``paths.PKG_DATA``（**只读包内种子**）。运行期覆盖件
（运行目录里的同名刷新件）不在本项范围 —— 那是 :func:`ledger`（**有效数据账本**：
逐份对照「运行目录副本 / 随包种子」、说明插件实际读到哪一份，同样只读）的事。
"""

from __future__ import annotations

import fnmatch
import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import paths

# ------------------------------------------------------------------ 枚举（测试与面板共用）
GROUPS = (
    "damage",
    "drops_relics",
    "names_zh",
    "rotation",
    "community",
    "market",
    "world",
    "misc",
    "asset",
    "runtime_cache",
)
POLICIES = (
    "rotating",
    "max_age",
    "on_upstream_change",
    "manual_curated",
    "static_asset",
    "runtime_cache",
    "unused",
)
OVERRIDES = ("seed_only", "community_snapshot", "runtime_refresh", "runtime_only")
RISKS = ("high", "medium", "low")
# 判定结果（evaluate 返回行的 status 取值）
STATUSES = ("ok", "stale", "no_ttl", "unknown", "missing", "unregistered", "not_bundled")
# 需要按时间/轮次判定的 policy
TIMED_POLICIES = ("rotating", "max_age")


def _e(
    group: str,
    producer,
    source,
    *,
    source_rev=None,
    observed=None,
    policy: str = "on_upstream_change",
    max_age_days=None,
    readers=(),
    override: str = "seed_only",
    risk: str = "medium",
    game_version=None,
    tracked: bool = True,
    oss: bool = True,
    check=None,
    note: str = "",
) -> dict:
    """构造一条登记项（补齐全部字段，避免各处漏键）。"""
    return {
        "group": group,
        "producer": producer,
        "source": source,
        "source_rev": source_rev,
        "observed": observed,
        "policy": policy,
        "max_age_days": max_age_days,
        "readers": list(readers),
        "override": override,
        "risk": risk,
        "game_version": game_version,
        "tracked": tracked,
        # oss=False：开源 / 市场包不随附（dist/package_release.py::OSS_EXCLUDE_FILES），运行期从社区快照 / 刷新获取。
        #   缺这个文件不是「缺文件」（2026-10-17 v1.2.2 公开仓 CI 红 + 开源用户面板会误报）
        "oss": oss,
        "check": check,
        "note": note,
    }


# 键 = 相对 ``core/data`` 的 posix 路径；字体/图标用通配（fonts/*、icons/**）。
REGISTRY: dict[str, dict] = {
    # ================================================== damage（伤害数值，高风险）
    "weapons_stats.json": _e(
        "damage",
        "build_damage_data.py",
        "WFCD warframe-items（Primary/Secondary/Melee）+ wfsim/wiki 修正",
        readers=["core/damage_calc.py", "core/api_client.py"],
        risk="high",
        note="武器基础面板；--repair-weapons 为幂等离线修正（物理对调 / 主段取值）。",
    ),
    "mods_stats.json": _e(
        "damage",
        "scripts/sync_mods_from_wfsim.py",
        "WFCD warframe-items Mods.json（wfsim 校准等级/数值）",
        observed="_meta.retrieved",
        readers=["core/damage_calc.py", "core/loadout_ocr.py"],
        risk="high",
        note="mods=能参与计算的满级值；names=可识别全量名。",
    ),
    "mods_wfsim_extra.json": _e(
        "damage",
        "scripts/enrich_mods_from_wfsim.py",
        "wfsim data/mods（家族互斥 + 条件堆叠 + 武器排除）",
        observed="_meta.updated",
        readers=["core/damage_calc.py"],
        risk="high",
        note="独立文件，不动 mods_stats.json 本体。",
    ),
    "arcanes_stats.json": _e(
        "damage",
        "manual",
        "WFCD warframe-items Arcanes.json",
        observed="_meta.retrieved",
        readers=["core/damage_calc.py"],
        risk="high",
        note="只收武器类赋能；数值为满级值。",
    ),
    "enemies.json": _e(
        "damage",
        "scripts/merge_wfsim_enemies.py",
        "riven-mirror + wfsim data/enemies（wiki Module:Enemies/data）",
        observed="_meta.retrieved",
        readers=["core/damage_calc.py"],
        risk="high",
        note="只保留常用敌人 19 个（KEEP_ENEMIES）；爆头口径见 _meta.head_note。",
    ),
    "damage_faction.json": _e(
        "damage",
        "build_damage_data.py",
        "wiki Damage/Overview_Table（U36 抗性重构后 ×1.5/×0.5/×1）",
        observed="_meta.retrieved",
        readers=["core/damage_calc.py"],
        risk="high",
        note="与 wfsim 全表 diff 过；U41 新增派系 Anarchs 已补。",
    ),
    "stances_stats.json": _e(
        "damage",
        "build_stances.py",
        "wiki Module:Stances/data",
        observed="_meta.retrieved",
        readers=["core/damage_calc.py"],
        risk="high",
        note="架势决定近战每段倍率与强制异常。",
    ),
    "evolutions.json": _e(
        "damage",
        "scripts/build_evolutions_and_valence.py",
        "wfsim data/evolutions",
        observed="_meta.generated",
        readers=["core/damage_calc.py", "core/loadout_ocr.py"],
        risk="high",
        note="灵化/进化选项；modellable 标记是否被折算。",
    ),
    "incarnon_forms.json": _e(
        "damage",
        "scripts/build_incarnon_forms.py",
        "wfsim data/weapons/*（form: incarnon）",
        source_rev="_meta.wfsim_commit",
        readers=["core/damage_calc.py", "core/loadout_ocr.py"],
        risk="high",
        note="枪械灵化形态基础值。",
    ),
    "lich_valence.json": _e(
        "damage",
        "scripts/build_evolutions_and_valence.py",
        "wfsim data/weapons/*.{kuva,tenet,coda}* valence 字段",
        observed="_meta.generated",
        readers=["core/damage_calc.py", "core/loadout_ocr.py"],
        risk="high",
        note="玄骸/信条额外元素加成的合法范围（非当期效价表）。",
    ),
    "co_behavior.json": _e(
        "damage",
        "scripts/build_co_behavior.py",
        "wfsim co_behavior（只收默认/灵化形态）",
        source_rev="_meta.wfsim_commit",
        readers=["core/damage_calc.py"],
        risk="high",
        note="创口溃烂（CO）桶别表。",
    ),
    "weapon_attacks.json": _e(
        "damage",
        "scripts/build_weapon_attacks.py",
        "wfsim data/weapons/*（attack 块）",
        source_rev="_meta.wfsim_commit",
        readers=["core/damage_calc.py"],
        risk="high",
        note="多段命中实例（segments/main/charge_seconds）。",
    ),
    "weapon_deployments.json": _e(
        "damage",
        "scripts/build_weapon_deployments.py",
        "wfsim data/weapons/*（deployments 字段）",
        observed="_meta.updated",
        readers=["core/damage_calc.py"],
        risk="high",
        note="Archgun 地面/空战两种部署面板。",
    ),
    # ================================================== drops_relics（掉落/遗物，高风险）
    "drops.json": _e(
        "drops_relics",
        "scripts/build_drops.py",
        "WFCD warframe-drop-data + DE 官方掉落页",
        observed="updated",
        readers=["core/drops.py", "core/wiki_intro.py", "core/search.py", "core/formatters.py"],
        risk="high",
        note="source 指向固定 WFCD commit 的 all.slim.json。",
    ),
    "relic_index.json": _e(
        "drops_relics",
        "scripts/kb/build_relic_index.py",
        "WFCD drop-data 遗物表（遗物 → 部件）",
        readers=["core/formatters.py", "core/wiki_intro.py", "core/commands/relic.py"],
        risk="high",
    ),
    "relic_inverse.json": _e(
        "drops_relics",
        "scripts/kb/build_relic_index.py",
        "WFCD drop-data 遗物表（部件 → 遗物反查）",
        readers=["core/formatters.py", "core/wiki_intro.py", "core/commands/relic.py"],
        risk="high",
    ),
    "bounty_pools.json": _e(
        "drops_relics",
        "manual",
        "DE ExportRewards（钢铁补池，只覆盖子池）",
        readers=["core/formatters.py", "core/de_worldstate.py"],
        risk="high",
        note="补池生成器为私有 skill 脚本（.zcode/…/build_nokko_sp_pool.py），只覆盖子池。",
    ),
    "de/sortie_rewards.json": _e(
        "drops_relics",
        "manual",
        "DE 官方导出 ExportSortieRewards_zh",
        readers=["core/formatters.py"],
        risk="high",
    ),
    # ================================================== names_zh（中文名/语言表，中风险）
    "de/bounty_jobs_zh.json": _e(
        "names_zh",
        "build_de_data.py",
        "DE ExportBounties + dict.zh",
        readers=["core/de_worldstate.py", "core/render.py", "core/tents.py"],
        risk="medium",
    ),
    "de/challenges_zh.json": _e(
        "names_zh",
        "build_de_data.py",
        "DE ExportChallenges + dict.zh",
        readers=["core/de_worldstate.py", "core/formatters.py", "core/push.py"],
        risk="medium",
    ),
    "de/conclave_zh.json": _e(
        "names_zh",
        "manual",
        "DE 官方语言表（dict.zh）",
        readers=["core/de_worldstate.py"],
        risk="medium",
    ),
    "de/de_items_zh.json": _e(
        "names_zh",
        "manual",
        "DE 官方本地化导出（EN↔CN）",
        readers=["core/api_client.py", "core/search.py"],
        risk="medium",
    ),
    "de/events_zh.json": _e(
        "names_zh",
        "manual",
        "DE 官方语言表（dict.zh）",
        readers=["core/de_worldstate.py"],
        risk="medium",
    ),
    "de/languages.json": _e(
        "names_zh",
        "manual",
        "WFCD worldstate 英文语言表",
        readers=["core/de_worldstate.py"],
        risk="medium",
    ),
    "de/languages_zh.json": _e(
        "names_zh",
        "build_de_data.py",
        "DE dict.zh + oracle 补充词表（精简 /Lotus/Language/*）",
        readers=["core/de_worldstate.py", "main.py"],
        risk="medium",
    ),
    "de/mission_types_zh.json": _e(
        "names_zh",
        "build_de_data.py",
        "DE ExportMissionTypes + dict.zh",
        readers=["core/de_worldstate.py", "core/formatters.py"],
        risk="medium",
    ),
    "de/mod_names_zh.json": _e(
        "names_zh",
        "build_de_data.py",
        "DE ExportUpgrades + dict.zh",
        readers=["core/formatters.py"],
        risk="medium",
        note="官方 MOD 名集合（列表），供奖励卡类型/名称判定。",
    ),
    "de/name_bilingual.json": _e(
        "names_zh",
        "scripts/build_name_bilingual.py",
        "DE dict.en/zh 同键对（命名空间 Mods/Weapons/Suits/Items）",
        readers=["core/matching.py", "core/commands/wiki_misc.py"],
        risk="medium",
    ),
    "de/name_en_zh.json": _e(
        "names_zh",
        "scripts/build_name_en_zh.py",
        "browse.wf public-export-plus dict.en.json + dict.zh.json",
        observed="_meta.generated",
        readers=["core/api_client.py", "core/search.py"],
        risk="medium",
    ),
    "de/name_zh.json": _e(
        "names_zh",
        "manual",
        "DE 官方导出（物品路径 → 官方简中）",
        readers=["core/de_worldstate.py", "core/formatters.py"],
        risk="medium",
        note="主表；name_zh_wfcd.json 只补其缺键，不覆盖。",
    ),
    "de/name_zh_wfcd.json": _e(
        "names_zh",
        "scripts/build_name_zh_wfcd.py",
        "WFCD warframe-items data/json/i18n/zh.json",
        readers=["core/de_worldstate.py"],
        risk="medium",
        note="只收 name_zh.json 没有的键；生成器支持 --check 可复现校验。",
    ),
    "de/nightwave_zh.json": _e(
        "names_zh",
        "manual",
        "NyxBot 官方导出（DE 语言表）",
        readers=["core/de_worldstate.py"],
        risk="medium",
    ),
    "de/nodes_zh.json": _e(
        "names_zh",
        "build_de_data.py",
        "DE ExportRegions + dict.zh",
        readers=["core/de_worldstate.py", "core/formatters.py", "core/push.py"],
        risk="medium",
    ),
    "de/recipe_names_zh.json": _e(
        "names_zh",
        "build_de_data.py",
        "DE ExportRecipes + dict.zh",
        readers=["core/formatters.py"],
        risk="medium",
        note="官方配方名集合（列表）。",
    ),
    "de/storeitems_zh.json": _e(
        "names_zh",
        "scripts/build_storeitems_zh.py",
        "warframe-public-export-plus ExportResources + dict.zh",
        observed="_meta.built",
        readers=["core/api_client.py"],
        risk="medium",
    ),
    "de/zh_ext.json": _e(
        "names_zh",
        "scripts/build_zh_ext.py",
        "DE 官方语言包全量抽取（卡面命名空间）",
        readers=["core/de_worldstate.py"],
        risk="medium",
    ),
    "baro_names_zh.json": _e(
        "names_zh",
        "scripts/build_baro_names.py",
        "DE dict.en/zh（奸商商品名）",
        readers=["core/baro.py", "core/formatters.py"],
        risk="medium",
    ),
    "wiki_effect_zh.json": _e(
        "names_zh",
        "scripts/merge_effect_zh.py",
        "本仓 agent 逐条翻译 + 官方增幅卡整句替换",
        observed="_meta.generated",
        readers=["core/wiki_intro.py", "core/commands/wiki_misc.py"],
        risk="medium",
        note="归一化英文效果文本 → 中文译文；含人工覆盖行。",
    ),
    # ================================================== rotation（轮换/到期，高风险）
    "rotations.json": _e(
        "rotation",
        "runtime",
        "手工校准 + 运行期刷新（效价快照）",
        observed="tenet.valence_snapshot",
        policy="rotating",
        check="valence",
        readers=[
            "core/api_client.py",
            "core/formatters.py",
            "core/loadout_ocr.py",
            "core/push.py",
            "core/commands/rotations.py",
            "main.py",
        ],
        override="runtime_refresh",
        risk="high",
        note="判据复用 WarframeClient.valence_is_stale（tenet / coda 两段窗口）。",
    ),
    "de/acrichis_week.json": _e(
        "rotation",
        "runtime",
        "游戏内言录使货单实拍 + wiki《Acrithis》",
        observed="observed",
        policy="rotating",
        check="expiry",
        readers=["core/api_client.py", "main.py"],
        override="runtime_refresh",
        risk="high",
        note="每周一 00:00 UTC 轮换；expiry <= now 即过期。",
    ),
    "de/acrichis_pool.json": _e(
        "rotation",
        "build_de_data.py",
        "DE ExportVendors（言录使货架池） + dict.zh",
        readers=["core/api_client.py"],
        risk="low",
    ),
    "de/palladino_shop.json": _e(
        "rotation",
        "manual",
        "wiki Riven_Sliver + DE 官方 language 表",
        policy="manual_curated",
        readers=["core/formatters.py", "core/commands/progress.py"],
        risk="medium",
        note="常驻、无轮换库存；每周限购，周一 00:00 UTC 重置（重置确定性推算）。",
    ),
    "de/steel_shop.json": _e(
        "rotation",
        "manual",
        "wiki Steel_Essence + DE 官方 language 表",
        policy="manual_curated",
        readers=["core/formatters.py"],
        risk="medium",
        note="商品/轮换顺序按 epoch 推算；规则变更需人工核对。",
    ),
    # ================================================== community（社区快照通道）
    "arb_ratings.json": _e(
        "community",
        "scripts/build_arb_ratings.py",
        "arbi.wf.wiki 排行榜中位数（社区实测，非官方）",
        observed="_updated",
        policy="max_age",
        max_age_days=14,
        readers=["core/arbi.py", "core/api_client.py"],
        override="community_snapshot",
        risk="medium",
        note="客户端每 3 小时检查社区快照。",
    ),
    "baro_history.json": _e(
        "community",
        "scripts/build_baro_history.py",
        "wiki.warframe.com · Module:Baro/data",
        observed="fetched",
        policy="max_age",
        max_age_days=14,
        check="baro_missed",
        readers=["core/baro.py", "core/formatters.py"],
        override="community_snapshot",
        risk="medium",
        note="判据走 core.baro.missed_visits（到访缺失即重建），不单看年龄。",
    ),
    "de/wiki_disp.json": _e(
        "community",
        "scripts/build_disposition.py",
        "wiki Riven_Mods 倾向表（CC BY-NC-SA）",
        observed="snapshot",
        policy="manual_curated",
        readers=["core/api_client.py"],
        override="runtime_refresh",
        risk="low",
        oss=False,
        note="2026-10 DE 取消紫卡倾向后冻结：不再重抓、不进社区快照，只作「倾向取消前的旧截图」判据。"
        "开源 / 市场包不随附（OSS_EXCLUDE_FILES），缺失时只少这条提示。",
    ),
    "dispositions_rivenmirror.json": _e(
        "community",
        "scripts/build_disposition.py",
        "wiki 主源 + riven-mirror 兜底 + WM 类别",
        readers=["core/api_client.py", "core/matching.py"],
        risk="low",
        note="运行期覆盖同名条目并追加缺失；与 wiki_disp 同一次 wiki 解析。2026-10 倾向取消后冻结为"
        "「取消前旧倾向」，只用于倾向卡的数值缩放提示与旧截图判据。",
    ),
    # ================================================== market（市场/紫卡）
    "junk.json": _e(
        "market",
        "manual",
        "人工策展（name 需与 WM url_name 对齐）",
        policy="manual_curated",
        readers=["core/commands/market.py"],
        risk="low",
        note="空池时回落动态采样计算性价比。",
    ),
    "lich_weapons.json": _e(
        "market",
        "scripts/build_lich_names.py",
        "weapons_stats + DE 官方中文 + WM",
        readers=["core/api_client.py", "core/commands/riven.py"],
        risk="medium",
    ),
    "de/riven_families.json": _e(
        "market",
        "scripts/build_riven_families.py",
        "DE ExportWeapons.parentName + wiki 双模式键",
        observed="_generated_at",
        readers=["core/api_client.py", "core/matching.py", "core/commands/market.py"],
        risk="medium",
        note="家族 = 沿 parentName 上溯；mtime 缓存护栏不等于新鲜度。",
    ),
    "de/riven_pools.json": _e(
        "market",
        "scripts/build_riven_pools.py",
        "wfsim data/rivens/{职业池,exceptions,physical,pools}.yaml + data/weapons（家族/弹药池）"
        " + 本地抽样覆盖层 de/riven_pools_survey.json",
        observed="_generated_at",
        readers=["core/riven_pools.py", "core/commands/riven.py"],
        risk="medium",
        note="词条池证据表（「词条范围」按武器过滤用）。静态快照：上游改动须重跑生成器，"
        "`--check` 可复现校验；`_wfsim_commit` 记上游版本。",
    ),
    "de/riven_pools_survey.json": _e(
        "market",
        "scripts/survey_riven_pools.py",
        "warframe.market /v1/auctions/search 逐词条带过滤计数（本地抽样）",
        observed="_surveyed",
        policy="manual_curated",
        readers=["scripts/build_riven_pools.py"],
        risk="medium",
        note="抽样覆盖层：只被生成器读（运行期不读），合并进 de/riven_pools.json 的 "
        "families[*].survey。WM 速率受限，补抽样要慢跑（脚本内置 2.5s/请求 + 退避）。",
    ),
    # ================================================== world（世界状态/节点）
    "de/solNodes.json": _e(
        "world",
        "manual",
        "WFCD worldstate 字典（节点 → 任务/星球）",
        policy="manual_curated",
        readers=["core/de_worldstate.py", "core/formatters.py"],
        risk="low",
    ),
    "de/missionTypes.json": _e(
        "world",
        "manual",
        "WFCD worldstate 字典（任务类型英文表）",
        policy="manual_curated",
        readers=["core/de_worldstate.py", "core/formatters.py"],
        risk="low",
    ),
    "de/sortieData.json": _e(
        "world",
        "manual",
        "WFCD worldstate 字典（突击首领/词缀）",
        policy="manual_curated",
        readers=["core/de_worldstate.py"],
        risk="low",
    ),
    "de/factionsData.json": _e(
        "world",
        "build_de_data.py",
        "DE ExportFactions + dict.zh",
        readers=["core/de_worldstate.py"],
        risk="low",
    ),
    "bounty_job_names.json": _e(
        "world",
        "manual",
        "旧赏金名称兜底表（DE 导出前的历史表）",
        policy="manual_curated",
        readers=["core/de_worldstate.py", "core/formatters.py"],
        risk="low",
        note="新版完整表见 de/bounty_jobs_zh.json（build_de_data.py 生成）。",
    ),
    "de/eidolon_job_manifest.json": _e(
        "world",
        "manual",
        "github.com/calamity-inc/wf.browse.oracle data/EidolonJobManifest.json（DE /Lotus/Types/Game/EidolonJobManifest 资产的社区镜像；PEP 未收录）",
        readers=["core/tents.py"],
        risk="medium",
        note="小帐篷按「世界种子 + JobManifest」推算点位当前赏金；2026-09-24 人工拷贝落地（3f58319），与 oracle.browse.wf/location-bounties 对拍一致。",
    ),
    "de/venus_job_manifest.json": _e(
        "world",
        ".zcode/skills/astrbot-warframe-plugin-dev/scripts/build_venus_points.py",
        "oracle VenusJobManifest（senpai 分支）",
        readers=["core/tents.py"],
        risk="medium",
        note="金星 DE 不下发 manifest，用 oracle 默认分支点位补；on_card=False。",
    ),
    "synth_targets.json": _e(
        "world",
        "manual",
        "WFCD warframe-worldstate-data synthTargets.json（西蒙斯扫描目标出现地点）",
        policy="on_upstream_change",
        readers=["core/de_worldstate.py"],
        risk="low",
        note="西蒙斯扫描目标出现地点表；09-11 接手基线自带的 WFCD 拷贝（格式与 warframe-worldstate-data 一致），上游变了再人工更新。",
    ),
    # ================================================== misc
    "aliases.json": _e(
        "misc",
        "manual",
        "人工轻量词库（wm_items / riven_items / wiki / clarify）",
        policy="manual_curated",
        readers=["core/api_client.py", "core/search.py", "core/commands/market.py"],
        risk="medium",
        note="可自由扩充；重启或重载后生效。",
    ),
    "wiki_intro.json": _e(
        "misc",
        "scripts/build_wiki_intro.py",
        "知识库 Markdown（warframe-kb-build 产物）构建期抽取",
        observed="_meta.generated",
        readers=["core/wiki_intro.py", "core/commands/wiki_misc.py", "main.py"],
        risk="medium",
        note="随开源 / 市场包分发（2026-09-25 维护者定）。",
    ),
    # ================================================== asset（字体 / 图标）
    "fonts/*": _e(
        "asset",
        "scripts/build_font_subset.py",
        "完整 TTC（scripts/fetch_font.py）+ 子集 OTF（scripts/build_font_subset.py）",
        policy="static_asset",
        readers=["core/render.py"],
        risk="low",
        note="按缺字事件/语料变化重建子集；完整字体仅版本/字形需求更新，无日历过期。",
    ),
    "icons/**": _e(
        "asset",
        "manual",
        "Warframe 官方 Fan Kit 极性图标",
        policy="static_asset",
        readers=["core/render.py"],
        risk="low",
        note="按新增极性/视觉资源变更更新，不安排联网刷新。",
    ),
    # ================================================== runtime_cache（运行期缓存，不随包）
    "wm_ranks.json": _e(
        "runtime_cache",
        "runtime",
        "WM warframemarket 价格排行（运行期爬取）",
        policy="runtime_cache",
        readers=["core/api_client.py"],
        override="runtime_only",
        risk="low",
        tracked=False,
        note="被 .gitignore 忽略，只在运行目录；不随包分发。",
    ),
    "riven_weekly.json": _e(
        "runtime_cache",
        "runtime",
        "DE 官方紫卡周报（运行期查询）",
        policy="runtime_cache",
        readers=["core/api_client.py"],
        override="runtime_only",
        risk="low",
        tracked=False,
        note="被 .gitignore 忽略，只在运行目录；不随包分发。",
    ),
    "riven_rt.json": _e(
        "runtime_cache",
        "runtime",
        "紫卡 WM 现价快照（WM 拍卖；2026-10-23 加，10-25 起只手动拉）",
        policy="runtime_cache",
        readers=["core/api_client.py"],
        override="runtime_only",
        risk="low",
        tracked=False,
        note="被 .gitignore 忽略，只在运行目录；不随包分发。游标续跑 + 覆盖率闸门见 crawl_riven_realtime。",
    ),
}


# ------------------------------------------------------------------ 工具
def _dig(obj, path):
    """按 ``a.b.c`` 点路径取值；任一层缺失 / 非对象返回 ``None``。"""
    cur = obj
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


def _is_wildcard(key: str) -> bool:
    return "*" in key


def _registered(name: str) -> bool:
    """某个相对路径是否被 REGISTRY 覆盖（含通配键）。"""
    for key in REGISTRY:
        if name == key or (_is_wildcard(key) and fnmatch.fnmatch(name, key)):
            return True
    return False


def _discover_tracked(data_dir: Path):
    """``git ls-files`` 列出 data_dir 下被跟踪的文件（相对 data_dir）。

    不在 git 仓 / git 不可用 ⇒ 返回 ``None``，调用方退回 ``rglob`` 扫 ``*.json``。
    只读命令，不改工作树。
    """
    try:
        p = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=str(data_dir),
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if p.returncode != 0:
        return None
    return [n for n in p.stdout.decode("utf-8", "replace").split("\0") if n]


def _parse_time(value):
    """把登记到的内容时间解析为 aware datetime；解析不了返回 ``None``。"""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:  # 只写日期的（如 "2026-10-16"）按 UTC 计
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _iter_candidates(data_dir: Path, tracked_files):
    """待核对的相对路径清单（排除 ``_cache/`` 与 tracked=False 的运行期缓存）。"""
    if tracked_files is not None:
        names = [str(x) for x in tracked_files]
    else:
        names = _discover_tracked(data_dir)
        if names is None:
            try:
                names = [p.relative_to(data_dir).as_posix() for p in data_dir.rglob("*.json")]
            except OSError:
                names = []
    out = []
    for raw in names:
        name = raw.replace("\\", "/")
        if name.startswith("_cache/") or "/_cache/" in name:
            continue
        if name in REGISTRY and not REGISTRY[name].get("tracked", True):
            continue
        out.append(name)
    return out


# ------------------------------------------------------------------ 判定
def _judge_timed(meta: dict, data, now: datetime, row: dict) -> tuple[str, str]:
    """``rotating`` / ``max_age`` 的判定；返回 ``(status, detail)``。"""
    if not isinstance(data, dict):
        if meta["policy"] == "max_age":
            return "unknown", "没有内容时间，无法判新鲜（文件不可读或非对象）"
        return "stale", "文件不可读或非对象，无法判定轮次"
    check = meta.get("check")
    if check == "valence":
        from .api_client import WarframeClient

        bad = []
        for sec in ("tenet", "coda"):
            d = data.get(sec) or {}
            try:
                if WarframeClient.valence_is_stale(d, now):
                    bad.append(sec)
                elif WarframeClient.valence_values_stale(d, now):
                    bad.append(f"{sec}（数值疑似仍是上一轮，wiki 未完整更新）")
            except Exception:  # noqa: BLE001 - epoch 缺失等一律算过期
                bad.append(sec)
        if bad:
            return "stale", "效价快照不在本轮窗口（" + "、".join(bad) + " 段）"
        return "ok", "效价快照在本轮窗口内"
    if check == "expiry":
        exp = _parse_time(data.get("expiry"))
        if exp is None:
            return "stale", "expiry 缺失或畸形"
        if exp <= now:
            return "stale", f"已到期（expiry={data.get('expiry')!r}）"
        return "ok", f"未到期（expiry={data.get('expiry')!r}）"
    if check == "baro_missed":
        try:
            from . import baro

            missed = baro.missed_visits(now, grace_hours=baro.STALE_GRACE_HOURS)
            recent = baro.missed_visits(now) - missed
        except Exception as exc:  # noqa: BLE001
            return "stale", f"奸商到访判据起不来：{exc!r}"
        if missed > 0:
            return "stale", f"快照后已有 {missed} 次到访超过 3 天仍未收录"
        if recent > 0:
            return "ok", "本次到访进行中或刚结束，wiki 通常 1–3 天内收录"
        return "ok", "已含最近一次到访"
    obs_path = meta.get("observed")
    obs = _dig(data, obs_path) if obs_path else None
    dt = _parse_time(obs)
    if dt is None:
        return "unknown", "没有内容时间，无法判新鲜"
    days = meta.get("max_age_days")
    if days is not None and now - dt > timedelta(days=days):
        return "stale", f"内容时间 {obs} 已超过 {days} 天"
    return "ok", f"内容时间 {obs}（上限 {days} 天）"


# 大表（weapons_stats 等几 MB）只为取一两个字段：按 (mtime_ns, size) 缓存取出的小值，
# 不缓存整份对象（面板每次刷新都会调 evaluate，2GB 机器上不能反复全量解析 / 常驻大对象）。
_FIELD_CACHE: dict[str, tuple[int, int, dict]] = {}


def _fields(path: Path, meta: dict) -> dict:
    """取 observed / source_rev / 顶层类型名（带缓存）；读不了返回 ``{"error": ...}``。

    ``type`` = ``type(data).__name__``（``dict`` / ``list`` / …）—— 有效数据账本用它
    比对「运行副本与随包种子是不是同一种东西」，省得为看类型再整份解析一遍大表。
    """
    try:
        st = path.stat()
    except OSError as exc:
        return {"error": repr(exc)}
    key = str(path)
    hit = _FIELD_CACHE.get(key)
    if hit is not None and hit[0] == st.st_mtime_ns and hit[1] == st.st_size:
        return hit[2]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"error": repr(exc)}
    out = {"type": type(data).__name__}
    if isinstance(data, dict):
        for f in ("observed", "source_rev"):
            if meta.get(f):
                out[f] = _dig(data, meta[f])
    _FIELD_CACHE[key] = (st.st_mtime_ns, st.st_size, out)
    return out


def _eval_one(key: str, meta: dict, data_dir: Path, now: datetime, resolve=None) -> dict:
    row = {
        "file": key,
        "group": meta["group"],
        "policy": meta["policy"],
        "risk": meta["risk"],
        "status": "unknown",
        "detail": "",
        "observed": None,
        "source_rev": None,
    }
    known = bool(meta.get("producer")) and bool(meta.get("source"))
    tracked = bool(meta.get("tracked", True))
    wildcard = _is_wildcard(key)
    if wildcard:
        try:
            present = bool(list(data_dir.glob(key)))
        except OSError:
            present = False
    else:
        fpath = data_dir / key
        if resolve is not None and meta.get("override") in (
            "community_snapshot",
            "runtime_refresh",
        ):
            fpath = Path(resolve(key))  # 面板：看实际生效的那份（运行期刷新 / 社区快照优先）
        present = fpath.exists()
    if tracked and not present:
        if not meta.get("oss", True):
            row["status"] = "not_bundled"
            row["detail"] = "开源 / 市场包不随附，运行期首次从社区快照或刷新获取"
            return row
        row["status"] = "missing"
        row["detail"] = f"缺文件：{key} 不在 {data_dir}"
        return row
    data = None
    if not wildcard and present:
        if meta["policy"] in TIMED_POLICIES:
            # 轮换 / 年龄类都是小文件，判据要整份对象：直接读，不缓存
            try:
                data = json.loads(fpath.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:  # 单个坏文件不拖垮整表
                row["detail"] = f"读取失败：{exc!r}"
            if isinstance(data, dict):
                for f in ("observed", "source_rev"):
                    if meta.get(f):
                        row[f] = _dig(data, meta[f])
        elif meta.get("observed") or meta.get("source_rev"):
            got = _fields(fpath, meta)
            if "error" in got:
                row["detail"] = f"读取失败：{got['error']}"
            row["observed"] = got.get("observed")
            row["source_rev"] = got.get("source_rev")
    if meta["policy"] in TIMED_POLICIES:
        status, detail = _judge_timed(meta, data, now, row)
        if status == "ok" and not known:
            status, detail = "unknown", "来源未确认：producer/source 缺一"
        row["status"], row["detail"] = status, detail
        return row
    if known:
        row["status"] = "no_ttl"
        row["detail"] = "不按时间判，上游变了再重建"
    else:
        row["status"] = "unknown"
        row["detail"] = "来源未确认：producer/source 缺一"
    return row


def evaluate(
    data_dir: Path | None = None,
    now: datetime | None = None,
    *,
    tracked_files: list[str] | None = None,
    resolve=None,
) -> list[dict]:
    """逐条判定登记项；返回行 ``{file, group, policy, risk, status, detail, observed, source_rev}``。

    * ``data_dir`` 默认 ``paths.PKG_DATA``（只读包内种子）；
    * ``tracked_files`` 给出「应有」的相对路径清单（测试用）；缺省先试 ``git ls-files``，
      不可用则 ``rglob`` 扫 ``*.json``（排除 ``_cache/`` 与 tracked=False 项）；
    * ``resolve``：可选 ``name -> Path``（面板传 ``paths.read_path``）——override 为
      ``community_snapshot`` / ``runtime_refresh`` 的文件改看实际生效的那份，免得把已刷新的
      运行期副本按旧种子报「过期」；其余文件仍读 ``data_dir``；
    * 只读、不联网、不写盘；单个坏 JSON 只让那一行记 ``unknown``/错误说明，不抛。
    """
    data_dir = Path(data_dir) if data_dir is not None else paths.PKG_DATA
    now = now or datetime.now(timezone.utc)
    rows = [_eval_one(key, meta, data_dir, now, resolve) for key, meta in REGISTRY.items()]
    for name in _iter_candidates(data_dir, tracked_files):
        if _registered(name):
            continue
        rows.append(
            {
                "file": name,
                "group": "unregistered",
                "policy": "unregistered",
                "risk": "high",
                "status": "unregistered",
                "detail": f"未登记：{name} 不在 core/data_registry.REGISTRY",
                "observed": None,
                "source_rev": None,
            }
        )
    return rows


def integrity_problems(rows) -> list[str]:
    """登记表完整性问题（未登记 + 缺文件）字符串清单；空 = 通过。

    供打包闸门（``dist/package_release.py``）与测试用。
    """
    out = []
    for r in rows:
        if r.get("status") == "unregistered":
            out.append(f"未登记：{r.get('file')}（{r.get('detail')}）")
        elif r.get("status") == "missing":
            out.append(f"缺文件：{r.get('file')}（{r.get('detail')}）")
    return out


# ------------------------------------------------------------------ 有效数据账本（只读）
# 口径：:func:`evaluate` 判的是**包内种子**该不该重建；本项判的是「插件此刻实际读到
# 哪一份」—— 运行目录（``data/plugin_data/<插件名>/``）里的同名副本会**遮蔽**随包种子
# （``core.paths.read_path`` 运行目录优先），而副本只按「存在」被选中：
#   · 坏 JSON 时 ``load_json`` 返回 {}，**不会**回落种子 ⇒ 坏副本长期遮蔽种子；
#   · 升级后新种子比旧副本新，也照样用旧的。
# 本账本把这两类隐患摊开成一行行结论（只读：不改名 / 不删除 / 不写任何数据文件）。
LEDGER_VERDICTS = (
    "seed_in_use",  # 运行目录没有副本，用随包种子（正常）
    "run_ok",  # 用运行目录副本，且不比种子旧（正常）
    "run_only",  # 运行期缓存（override=runtime_only），本就不随包（正常）
    "run_bad",  # 副本坏 / 校验不过 —— 坏 JSON 已回落种子（active=seed），校验不过仍在读它
    "run_unexpected",  # override=seed_only 却有副本（这个文件插件不会自己写）
    "run_older",  # 副本比随包种子旧（多半是升级前留下的）
    "none",  # 两边都没有（tracked=True ⇒ 随包种子缺失）
    "not_yet",  # 运行期缓存还没生成过（tracked=False）
)
# 需要处理的结论（面板「需要处理」/ CLI 退出码 / 测试用）
LEDGER_PROBLEMS = ("run_bad", "run_unexpected", "run_older")

_COMMUNITY_LOCAL: dict[str, str] | None = None


def _community_locals() -> dict[str, str]:
    """本地相对路径 → 社区快照名（反查 ``core/api_client.COMMUNITY_STATIC``）。

    反查失败（缺依赖 / 导入出错）⇒ 空表：这些文件退回通用结构校验，绝不因为
    反查失败而报错或误判。
    """
    global _COMMUNITY_LOCAL
    if _COMMUNITY_LOCAL is None:
        mapping: dict[str, str] = {}
        try:
            from .api_client import COMMUNITY_STATIC

            for name, spec in COMMUNITY_STATIC.items():
                local = spec.get("local") if isinstance(spec, dict) else None
                if local:
                    mapping[str(local)] = str(name)
        except Exception:  # noqa: BLE001 - 反查失败只影响校验精度，不该抛
            mapping = {}
        _COMMUNITY_LOCAL = mapping
    return _COMMUNITY_LOCAL


def _community_checker():
    """``community_static_ok`` 本体；拿不到返回 ``None``（退回通用判据）。"""
    try:
        from .api_client import community_static_ok

        return community_static_ok
    except Exception:  # noqa: BLE001 - 同上
        return None


def _valid(key: str, data, seed_type: str | None = None) -> tuple[bool, str]:
    """单份数据的结构校验（纯函数）：``(是否可用, 原因)``。

    * 社区静态快照（本地路径命中 ``COMMUNITY_STATIC[*]["local"]``，如
      ``arb_ratings.json``）走运行期同一套判据 ``community_static_ok``；
    * 其余：顶层必须是非空 dict/list；给了 ``seed_type`` 还要求类型与种子一致
      （防「副本是数组、种子是对象」这类张冠李戴）。
    """
    remote = _community_locals().get(key)
    if remote is not None:
        checker = _community_checker()
        if checker is not None:
            return checker(remote, data)
    if not isinstance(data, (dict, list)):
        return False, "顶层不是对象或数组"
    if not data:
        return False, "内容为空"
    if seed_type and type(data).__name__ != seed_type:
        return False, f"顶层类型与随包种子不同（{type(data).__name__} ≠ {seed_type}）"
    return True, ""


def _blank_side() -> dict:
    """账本里「一份文件」的字段（不存在时 ``valid=None``）。"""
    return {
        "exists": False,
        "valid": None,
        "why": "",
        "observed": None,
        "size": None,
        "mtime": None,
    }


def _seed_side(path: Path, meta: dict) -> tuple[dict, str | None]:
    """随包种子那份：**只走 ``_fields`` 缓存**（不整份解析大表），返回 ``(side, 类型名)``。

    因此种子的 ``valid`` 是轻校验：能解析 + 顶层是对象/数组即算可用（种子本身由测试与
    打包闸门把关，账本不该为了判它把几 MB 的表再解析一遍）。
    """
    side = _blank_side()
    try:
        st = path.stat()
    except OSError:
        return side, None
    side["exists"] = True
    side["size"] = st.st_size
    side["mtime"] = st.st_mtime
    got = _fields(path, meta)
    if "error" in got:
        side["valid"], side["why"] = False, f"读不了或解析失败：{got['error']}"
        return side, None
    side["observed"] = got.get("observed")
    kind = got.get("type")
    if kind in ("dict", "list"):
        side["valid"] = True
    else:
        side["valid"], side["why"] = False, f"顶层不是对象或数组（{kind}）"
    return side, kind


# 运行副本校验结果按 (mtime_ns, size, 种子类型) 缓存：面板每次刷新都会调 ledger，
# wm_ranks 之类的副本不小，内容没变就不重新解析。
_RUN_CACHE: dict[str, tuple[int, int, str | None, dict]] = {}


def _run_side(path: Path, meta: dict, key: str, seed_type: str | None) -> dict:
    """运行目录副本那份：整份解析后校验（结果带缓存）。"""
    try:
        st = path.stat()
    except OSError:
        return _blank_side()
    ck = str(path)
    hit = _RUN_CACHE.get(ck)
    if hit is not None and hit[:3] == (st.st_mtime_ns, st.st_size, seed_type):
        return dict(hit[3])
    side = _run_side_parse(path, meta, key, seed_type, st)
    _RUN_CACHE[ck] = (st.st_mtime_ns, st.st_size, seed_type, dict(side))
    return side


def _run_side_parse(path: Path, meta: dict, key: str, seed_type: str | None, st) -> dict:
    side = _blank_side()
    side["exists"] = True
    side["size"] = st.st_size
    side["mtime"] = st.st_mtime
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        side["valid"], side["why"] = False, f"读不了：{exc}"
        return side
    try:
        data = json.loads(raw)
    except ValueError as exc:
        side["valid"], side["why"] = False, f"JSON 解析失败：{exc}"
        return side
    side["valid"], side["why"] = _valid(key, data, seed_type)
    if meta.get("observed") and isinstance(data, dict):
        side["observed"] = _dig(data, meta["observed"])
    return side


def _verdict(meta: dict, seed: dict, run: dict, active: str) -> tuple[str, str]:
    """按顺序取第一个命中的结论（顺序即优先级，改顺序等于改口径）。"""
    if active == "none":
        if not meta.get("oss", True):
            return "not_yet", "开源 / 市场包不随附，还没从社区快照取到：插件会自己获取，不用管"
        if meta.get("tracked", True):
            return "none", "随包种子缺失：这个文件本该随包分发，重新安装或补齐随包数据"
        return "not_yet", "还没生成过（运行期缓存）：插件用到时会自己生成，不用管"
    if active == "seed":
        return "seed_in_use", ""
    # —— 以下都是「运行目录里有副本」——
    if not run["valid"]:
        return "run_bad", (
            f"运行目录副本损坏或校验不过（{run['why']}），插件正在读它、不会回落随包种子；"
            "建议移走该副本（插件会回落种子，下次刷新再生成）"
        )
    if meta.get("override") == "seed_only":
        return "run_unexpected", (
            "运行目录里有同名文件遮蔽了随包种子（这个文件插件不会自己写）；确认来历后移走"
        )
    run_obs, seed_obs = _parse_time(run["observed"]), _parse_time(seed["observed"])
    if run_obs is not None and seed_obs is not None and run_obs < seed_obs:
        return "run_older", (
            f"运行副本（{run['observed']}）比随包种子（{seed['observed']}）旧，"
            "多半是升级前留下的；等下次刷新或移走即用新种子"
        )
    if meta.get("override") == "runtime_only":
        return "run_only", ""
    return "run_ok", ""


def _ledger_one(key: str, meta: dict, run_root: Path, pkg_root: Path) -> dict:
    """单条账本行（只读；任何一步失败都只影响这一行）。"""
    seed, seed_type = _seed_side(pkg_root / key, meta)
    run = _run_side(run_root / key, meta, key, seed_type)
    active = "run" if run["exists"] else ("seed" if seed["exists"] else "none")
    verdict, advice = _verdict(meta, seed, run, active)
    if verdict == "run_bad" and seed["exists"] and str(run["why"]).startswith("JSON 解析失败"):
        # 与 paths.read_path 同口径（P07）：坏 JSON 副本插件已自动回落随包种子
        active = "seed"
        advice = (
            f"运行目录副本 JSON 损坏（{run['why']}），插件已自动回落随包种子；"
            "建议移走该副本（下次刷新再生成）"
        )
    if verdict == "run_ok" and _parse_time(run["observed"]) is None:
        run["why"] = "无内容时间，未比新旧"  # 比不了新旧，但副本结构是好的
    return {
        "file": key,
        "override": meta.get("override"),
        "tracked": bool(meta.get("tracked", True)),
        "seed": seed,
        "run": run,
        "active": active,
        "verdict": verdict,
        "advice": advice,
    }


def ledger(run_dir: Path | None = None, pkg_dir: Path | None = None, now=None) -> list[dict]:
    """**有效数据账本**：逐份对照「运行目录副本」与「随包种子」，说明插件实际读到哪一份。

    * 只读：不写盘、不改名、不删除任何数据文件（隔离恢复是运维动作，不在本模块）；
    * 只看 :data:`REGISTRY` 的**非通配键**（含 ``tracked=False`` 的运行期缓存）：运行目录里
      还有订阅 / 群配置等用户存档（``JsonStore``），账本**不得**列出或读取它们；
    * ``run_dir`` 默认 ``paths.run_dir()``、``pkg_dir`` 默认 ``paths.PKG_DATA``；
      两者相同（未注入运行目录的离线场景）⇒ 没有覆盖层可言，返回 ``[]``；
    * ``now`` 只为与 :func:`evaluate` 的签名对齐，账本不按时间判定；
    * 单个文件 stat/读失败只影响那一行，不抛。

    行：``{file, override, tracked, seed, run, active, verdict, advice}``，其中
    ``seed`` / ``run`` 各为 ``{exists, valid, why, observed, size, mtime}``
    （不存在时 ``valid=None``）；``active`` 与 ``paths.read_path`` 语义一致
    （``run`` 存在 ⇒ 用 ``run``；否则种子存在 ⇒ ``seed``；都没有 ⇒ ``none``）。
    """
    run_root = Path(run_dir) if run_dir is not None else paths.run_dir()
    pkg_root = Path(pkg_dir) if pkg_dir is not None else paths.PKG_DATA
    try:
        if run_root.resolve() == pkg_root.resolve():
            return []
    except OSError:  # pragma: no cover - 路径解析不了就按「不同目录」继续
        pass
    rows = []
    for key, meta in REGISTRY.items():
        if _is_wildcard(key):  # 字体 / 图标是通配资产，没有「一份文件」可言
            continue
        try:
            rows.append(_ledger_one(key, meta, run_root, pkg_root))
        except Exception:  # noqa: BLE001 - 单行出问题不拖垮整本账
            blank = _blank_side()
            rows.append(
                {
                    "file": key,
                    "override": meta.get("override"),
                    "tracked": bool(meta.get("tracked", True)),
                    "seed": dict(blank),
                    "run": dict(blank),
                    "active": "none",
                    "verdict": "none",
                    "advice": "这一行读取出错（不影响其它行）；详见日志",
                }
            )
    return rows


def ledger_problems(rows) -> list[dict]:
    """需要处理的账本行：``run_bad`` / ``run_unexpected`` / ``run_older`` / 缺随包种子。"""
    out = []
    for r in rows:
        v = r.get("verdict")
        if v in LEDGER_PROBLEMS or (
            v == "none" and r.get("tracked", True)
        ):  # oss=False 缺文件已归 not_yet
            out.append(r)
    return out


def _main(argv=None) -> int:
    """CLI：只读打印某运行目录的有效数据账本（给运维在服务器上对拍用）。"""
    import argparse

    ap = argparse.ArgumentParser(
        prog="python -m core.data_registry",
        description="随包数据登记表 / 有效数据账本（只读，不写任何文件）",
    )
    ap.add_argument("--ledger", metavar="RUN_DIR", help="打印该运行目录的有效数据账本")
    args = ap.parse_args(argv)
    if not args.ledger:
        ap.print_help()
        return 0
    rows = ledger(Path(args.ledger))
    for r in rows:
        print(f"{r['verdict']}\t{r['active']}\t{r['file']}\t{r['advice']}")
    problems = ledger_problems(rows)
    print(f"LEDGER|rows={len(rows)}|problems={len(problems)}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(_main())
