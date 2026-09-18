# -*- coding: utf-8 -*-
"""全量指令冒烟：① 解析覆盖（54 个主指令 + 别名）② 数据链路（真实网络）。

用法：
    python scripts/smoke_all.py            # 全部
    python scripts/smoke_all.py --parse    # 只跑解析层（离线、快）
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import parser as P                                    # noqa: E402

# 参数型指令补样例（其余从别名表自动取）
EXTRA = {
    "xh": "玄骸",
    "wiki": "wiki 膛线",
    "valence": "武器融合 电60 火58",
    "damage": "伤害 绝路 对 重机枪手 100级 膛线",
    "scandamage": "识卡伤害 对 重机枪手 150级",
    "kim": "对话助手 赤毒",
    "wm": "wm 绝路",
    "wr": "wr 绝路",
    "rm": "rm 绝路",
    "rank": "排行 步枪",
    "trend": "趋势 绝路",
    "openrelic": "开核桃",
    "relic": "遗物 古纪 A1",
    "parts": "部件 绝路",
    "disposition": "倾向 绝路",
    "analysis": "紫卡分析 绝路 暴伤82.8",
}
FAIL_PARSE: list[tuple[str, str, str]] = []


def phase_parse() -> None:
    print("=" * 62)
    print("① 解析覆盖：54 个主指令 + 别名 + 预设命令")
    print("=" * 62)
    ok = 0
    for cmd in sorted(P.COMMAND_ALIASES):
        aliases = sorted(P.COMMAND_ALIASES[cmd])
        sample = EXTRA.get(cmd) or aliases[0]
        parsed = P.parse(sample)
        if parsed.command == cmd:
            ok += 1
        else:
            FAIL_PARSE.append((cmd, sample, str(parsed.command)))
            print(f"  ✗ {cmd:14s} 样例「{sample}」→ 解析成 {parsed.command}")
    print(f"  主指令：{ok}/{len(P.COMMAND_ALIASES)} 命中")

    # 别名全覆盖（118 条）
    bad_alias = 0
    for cmd, aliases in P.COMMAND_ALIASES.items():
        for a in aliases:
            if P.parse(a).command != cmd:
                bad_alias += 1
                print(f"  ✗ 别名「{a}」→ {P.parse(a).command}（应为 {cmd}）")
    print(f"  别名：{sum(len(v) for v in P.COMMAND_ALIASES.values()) - bad_alias}"
          f"/{sum(len(v) for v in P.COMMAND_ALIASES.values())} 命中")

    # 预设命令（金/银/铜垃圾、钢铁裂隙…）
    bad_pre = 0
    for alias, (cmd, preset) in P._PRESET_COMMANDS.items():
        r = P.parse(alias)
        if r.command != cmd or r.preset != preset:
            bad_pre += 1
            print(f"  ✗ 预设「{alias}」→ {r.command}/{r.preset}（应为 {cmd}/{preset}）")
    print(f"  预设命令：{len(P._PRESET_COMMANDS) - bad_pre}"
          f"/{len(P._PRESET_COMMANDS)} 命中")

    # 通用修饰符
    print("  修饰符：", end="")
    mods = [("-pc 夜灵", "platform", "pc"), ("夜灵 -w", "text_mode", True),
            ("夜灵 -t", "image_mode", True), ("wm 绝路 -r", "whisper", True),
            ("裂隙 -2", "page", 2)]
    bad_mod = 0
    for text, attr, want in mods:
        got = getattr(P.parse(text), attr)
        if got != want:
            bad_mod += 1
            print(f"\n  ✗ {text} → {attr}={got}（应为 {want}）", end="")
    print(f"{len(mods) - bad_mod}/{len(mods)} 命中")


async def phase_data() -> None:
    print()
    print("=" * 62)
    print("② 数据链路：真实网络（世界状态 / 市场 / 计算）")
    print("=" * 62)
    from core.api_client import WarframeClient
    client = WarframeClient(timeout=20.0)
    rows: list[tuple[str, bool, str, float]] = []

    async def probe(name, coro, check=lambda r: bool(r), optional=False):
        """optional=True：合法可为空的条目（见 OPTIONAL_EMPTY），空判「—」不算失败。"""
        t0 = time.perf_counter()
        try:
            r = await coro
            good = check(r)
            info = ""
            if isinstance(r, (list, dict)):
                info = f"{len(r)} 项" if isinstance(r, list) else "dict"
            if optional and not good:
                rows.append((name, None, "空（本就无此项）", time.perf_counter() - t0))
            else:
                rows.append((name, good, info if good else "空/异常",
                             time.perf_counter() - t0))
        except Exception as exc:  # noqa: BLE001
            rows.append((name, False, f"{type(exc).__name__}: {str(exc)[:60]}",
                         time.perf_counter() - t0))

    # 合法可为空的条目：DE 原始数据里该表本身为空，属正常而非故障。
    # 例：Warframe 现行版本已停用「警报（Alerts）」系统，DE 下发的
    #     worldstate 里 Alerts 恒为 []（2026-09-17 实测 44 个键，Alerts: 0），
    #     解析后 alerts 长度 0 是**正确结果**，若判为失败会掩盖真正的故障。
    OPTIONAL_EMPTY = {"警报"}

    # worldstate(platform, endpoint) 的 endpoint 是**必填**的：DE 直连模式下
    # 走 bundle.get(endpoint)，传空串只会拿到 None（曾被误报成「世界状态挂空」）。
    # 这里改为直接验证原始 bundle 的可用性 —— 那才是「世界状态整体是否拿到」。
    try:
        bundle = await client._de_bundle()
        await probe("世界状态全量", asyncio.sleep(0, bundle),
                    lambda r: isinstance(r, dict) and bool(r))
    except Exception as exc:  # noqa: BLE001
        rows.append(("世界状态全量", False, f"{type(exc).__name__}: {str(exc)[:60]}", 0.0))
    for name, coro in (
        ("周期(cetus)", client.cycle("pc", "cetus")),
        ("裂隙", client.fissures("pc")),
        ("突击", client.sortie("pc")),
        ("执刑官", client.archon_hunt("pc")),
        ("奸商", client.void_trader("pc")),
        ("每日特惠", client.daily_deals("pc")),
        ("电波", client.nightwave("pc")),
        ("仲裁", client.arbitration("pc")),
        ("警报", client.alerts("pc")),
        ("入侵", client.invasions("pc")),
        ("新闻", client.news("pc")),
        ("赤毒", client.kuva("pc")),
        ("钢铁之路", client.steel_path("pc")),
        ("深层科研", client.descendia("pc")),
        ("阿耶", client.acrithis_week()),
        ("侵袭", client.steel_path_incursions("pc")),
        ("九重天", client.void_storms("pc")),
        ("活动", client.events("pc")),
        ("武形秘仪", client.conclave("pc")),
        ("出库", client.prime_vault("pc")),
        ("氏族奖励", client.clan_rewards("pc")),
        ("商城折扣", client.flash_sales("pc")),
        ("舰队进度", client.construction("pc")),
    ):
        await probe(name, coro, optional=name in OPTIONAL_EMPTY)
    try:
        item = await client.resolve_wm_item("绝路")
        rows.append(("WM 物品解析", bool(item), (item or {}).get("zh", ""), 0.0))
    except Exception as exc:  # noqa: BLE001
        rows.append(("WM 物品解析", False, str(exc)[:50], 0.0))

    # rows[i][1]: True=通过 / None=合法为空 / False=失败
    ok = sum(1 for r in rows if r[1] is True)
    skip = sum(1 for r in rows if r[1] is None)
    bad = len(rows) - ok - skip
    for name, good, info, dt in rows:
        mark = "✓" if good is True else ("—" if good is None else "✗")
        print(f"  {mark} {name:14s} {info:22s} {dt * 1000:6.0f} ms")
    tail = f"，另有 {skip} 项本就无内容" if skip else ""
    print(f"  数据链路：{ok}/{len(rows) - skip} 通过（失败 {bad}{tail}）")

    # 纯本地计算
    print()
    from core import damage_calc as dc
    from core import riven_analysis as ra
    w, _ = dc.find_weapon("绝路")
    spec, _ = dc.parse_args(["膛线", "关键延迟"])
    res = dc.calculate(spec, w or {})
    print(f"  {'✓' if (w and res.get('ok')) else '✗'} 伤害计算："
          f"{(w or {}).get('zh')} 单发 {res.get('health', 0):.0f}")
    pos = [("multishot", 113.8), ("heat_damage", 102.6), ("melee_damage", 210.8)]
    iv = ra.disposition_interval(pos, [("damage_vs_corpus", 45.0)], "rifle")
    print(f"  {'✓' if iv[0] else '✗'} 紫卡分析：反推倾向区间 "
          f"{iv[0]:.3f}~{iv[1]:.3f}" if iv[0] else "  ✗ 紫卡分析失败")
    try:
        await client.close()
    except Exception:  # noqa: BLE001
        pass


def main() -> None:
    phase_parse()
    if "--parse" not in sys.argv:
        asyncio.run(phase_data())
    print()
    if FAIL_PARSE:
        print(f"⚠ 解析层有 {len(FAIL_PARSE)} 项失败")
        sys.exit(1)
    print("✓ 冒烟完成")


if __name__ == "__main__":
    main()
