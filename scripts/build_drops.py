#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""重建 core/data/drops.json（遗物掉落出处表）。

数据源：WFCD warframe-drop-data ``data/all.slim.json``（DE 官方掉落表精简版，
list of {place, item, rarity, chance}，place 形如
``Mercury/Apollodorus (<b>Survival</b>), Rotation A``）。

产出（core/drops.py 消费 places/relic_drops；core/search.py 读 items 的**键**；
core/wiki_intro.py::_drops_card 读 items 键+值 + places + zh2en）：
  places       {pid: "水星 · Apollodorus · 生存 · A轮"}
  relic_drops  {基名小写: [[pid, rarity, chance], ...]}
  items        {英文物品名小写: [[pid, rarity, chance], ...]}   ← **与 places 同源同编号**
  zh2en        {中文名: 英文键}（值必须都是 items 的合法键；死映射剔除）
  names/source 等其余辅助键续传（names 无 drops 相关消费）；source/updated 刷新。

★ 两条不变量（2026-10-04 立规，违反即缺陷）：
  1. **items 的 pid 必须与 places 同源同编号，禁止从旧文件续传值**
     —— 旧实现 `payload = dict(old)` 续传 items，而 places 每次重建、pid 重排
     ⇒ `items[item]` 指向错误条目（实测：`items['athodai blueprint']` 6 条里 4 条
     错连 `Axi A16/A17/A19/A2 Relic`，而真值是 6 个金星 Caches 节点）。
     items 的**键**可取旧键 ∪ 原始表键（搜索索引只增不减），**值**一律本次重建。
  2. **旧标签复用必须按模式判定**：旧索引键 (星球, 节点, 轮次) 丢了 mode
     ⇒ 同节点同轮次的不同模式（金星 Railjack 的 Skirmish「前哨战」vs Caches「储藏库」）
     会共用同一 pid、后写覆盖前者（实测 4 个金星节点的 Caches 行全被吸进「空战」）。
     ⇒ 规则：**组内只有一种 mode 才复用旧标签**；多 mode 组按 mode 感知新组合。

筛选与映射规则（2026-09-24 固化；2026-10-04 补模式感知）：
- 遗物键：item 以 ``relic`` 结尾者才收；剥掉 `` (radiant)`` 辐射形态后缀
  合并到基名（小写）；排除 Syndicate Relic Pack 一类非遗物条目；
- 地点：按 (节点英文名, 轮次) 与旧 places 的中文串对齐复用翻译（**仅单 mode 组**）；
  多 mode 组与全新节点用静态 星球/任务类型 中文表兜底（`mode_zh`，含「Level a - b X」
  形态的模式化翻译；查不到的保留英文原文）；
- **上游缺表补丁**：`apply_patches()` 把官方页有、WFCD 缺的表补进原始行
  （当前仅 Cryotic Front (Excavation) 30 行，见 `PATCH_CRYOTIC_EXCAVATION`；
  上游补上后自动跳过）。

用法：
    python scripts/build_drops.py                    # 从 GitHub 拉最新源
    python scripts/build_drops.py --src all.slim.json  # 从本地文件构建
      ★ `--src` 必须是**当前 master 快照**——用旧快照构建会把过期值带回
      （2026-10-04 实测：09-23 快照带回 4 条 Gyre 名称 + 28 条扎里曼几率旧值）。
      运行日志会打印源 md5 与（--src 时）文件 mtime，便于核对。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "core" / "data" / "drops.json"
SRC_URL = "https://raw.githubusercontent.com/WFCD/warframe-drop-data/master/data/all.slim.json"

PLACE_RE = re.compile(
    r"^(?P<planet>[^/]+)/(?P<node>.+?)\s*\((?P<mode>[^)]+)\)"
    r"(?:,\s*Rotation\s+(?P<rot>[ABC]))?\s*$"
)

PLANET_ZH = {
    "Mercury": "水星",
    "Venus": "金星",
    "Earth": "地球",
    "Mars": "火星",
    "Phobos": "火卫一",
    "Ceres": "谷神星",
    "Jupiter": "木星",
    "Saturn": "土星",
    "Uranus": "天王星",
    "Neptune": "海王星",
    "Pluto": "冥王星",
    "Sedna": "塞德娜",
    "Eris": "阋神星",
    "Europa": "木卫二",
    "Lua": "月球",
    "Deimos": "火卫二",
    "Void": "虚空",
    "Kuva Fortress": "女皇要塞",
    "Zariman": "扎利曼",
    "Earth Proxima": "地球比邻星域",
    "Venus Proxima": "金星比邻星域",
    "Saturn Proxima": "土星比邻星域",
    "Neptune Proxima": "海王星比邻星域",
    "Pluto Proxima": "冥王星比邻星域",
    "Veil Proxima": "面纱比邻星域",
    "Ceres Proxima": "谷神星比邻星域",
    "Jupiter Proxima": "木星比邻星域",
}

MODE_ZH = {
    "Survival": "生存",
    "Defense": "防御",
    "Interception": "拦截",
    "Capture": "捕获",
    "Rescue": "救援",
    "Spy": "间谍",
    "Sabotage": "破坏",
    "Excavation": "挖掘",
    "Defection": "防卫",
    "Disruption": "中断",
    "Assassination": "刺杀",
    "Mobile Defense": "移动防御",
    "Hijack": "劫持",
    "Skirmish": "前哨战",
    "Orphix": "奥影母艇",
    "Vault": "宝藏",
    "Pursuit": "追击",
    "Rush": "冲刺",
    "Volatile": "爆发",
    "Void Flood": "虚空洪泛",
    "Void Cascade": "虚空瀑布",
    "Void Armageddon": "虚空末日",
    "Conjunction Survival": "交汇生存",
    "Alchemy": "炼金术",
    "Assault": "进攻",
}
# ★ 2026-10-04 补：原始表里出现、此前无译名的模式（保持既有中文风格）
MODE_ZH.update(
    {
        "Caches": "储藏库",  # 金星/火卫二等「储藏库」奖励点（1,579 行）
        "Exterminate": "歼灭",  # 歼灭（111 行；此前落到英文）
    }
)
# 「Level a - b X」形态的模式：保留等级前缀 + 译尾（与旧数据既有标签同风格，
# 例：'Level 30 - 40 隔离库' —— 旧标签就是这么写的，这里把它**按模式**写对）
MODE_PATTERNS = (
    (re.compile(r"^Level\s+(\d+)\s*-\s*(\d+)\s+Arcana Isolation Vault$"), r"Level \1 - \2 隔离库"),
    (re.compile(r"^Level\s+(\d+)\s*-\s*(\d+)\s+Isolation Vault$"), r"Level \1 - \2 隔离库"),
    (re.compile(r"^Level\s+(\d+)\s*-\s*(\d+)\s+Cambion Drift Bounty$"), r"Level \1 - \2 赏金"),
    (re.compile(r"^Level\s+(\d+)\s*-\s*(\d+)\s+Entrati Lab Bounty$"), r"Level \1 - \2 实验室赏金"),
    (re.compile(r"^Level\s+(\d+)\s*-\s*(\d+)\s+Plague Star$"), r"Level \1 - \2 瘟疫之星"),
)


def mode_zh(mode: str) -> str:
    """任务模式 → 中文（MODE_ZH → 「Level a - b X」模式化翻译 → 原样英文）。"""
    if mode in MODE_ZH:
        return MODE_ZH[mode]
    for pat, repl in MODE_PATTERNS:
        if pat.match(mode or ""):
            return pat.sub(repl, mode)
    return mode


# ★ 上游缺表补丁（2026-10-04）：WFCD `all.slim.json` 缺官方页的
#   「Event: Europa/Cryotic Front (Excavation)」**整表**（Capture 表在、Excavation 表不在；
#   官方页 2026-09-24 build 实测有 30 行（A/B/C 轮），drop.wf.wiki 同源快照逐行一致）。
#   按 WFCD 的地点串风格（去 "Event: " 前缀）补注入；上游日后自行补上则**自动跳过**
#   （见 apply_patches 守卫，幂等）。行数据 = (轮次, 物品, 稀有度, 几率)。
PATCH_CRYOTIC_EXCAVATION = (
    ("A", "3,000 Credits Cache", "Uncommon", 25.0),
    ("A", "400 Endo", "Uncommon", 25.0),
    ("B", "Steel Fiber", "Rare", 6.67),
    ("B", "Stretch", "Rare", 6.67),
    ("B", "Serration", "Rare", 6.67),
    ("B", "Hell's Chamber", "Rare", 6.67),
    ("B", "Hornet Strike", "Rare", 6.67),
    ("B", "Flow", "Rare", 6.67),
    ("B", "Split Chamber", "Rare", 6.67),
    ("B", "Stabilizer", "Rare", 6.67),
    ("B", "Neo C7 Relic", "Rare", 6.67),
    ("B", "Neo A16 Relic", "Rare", 6.67),
    ("B", "Neo Y2 Relic", "Rare", 6.67),
    ("B", "Neo C11 Relic", "Rare", 6.67),
    ("B", "Neo K10 Relic", "Rare", 6.67),
    ("B", "Neo V13 Relic", "Rare", 6.67),
    ("B", "Neo C10 Relic", "Rare", 6.67),
    ("C", "Cleanse Infested", "Rare", 3.76),
    ("C", "Pistol Ammo Mutation", "Rare", 3.76),
    ("C", "Arrow Mutation", "Rare", 3.76),
    ("C", "Rifle Ammo Mutation", "Rare", 3.76),
    ("C", "Sniper Ammo Mutation", "Rare", 3.76),
    ("C", "Shotgun Ammo Mutation", "Rare", 3.76),
    ("C", "Axi S21 Relic", "Uncommon", 11.06),
    ("C", "Axi D6 Relic", "Uncommon", 11.06),
    ("C", "Axi V14 Relic", "Uncommon", 11.06),
    ("C", "Axi C12 Relic", "Uncommon", 11.06),
    ("C", "Axi A21 Relic", "Uncommon", 11.06),
    ("C", "Axi A22 Relic", "Uncommon", 11.06),
    ("C", "Axi P10 Relic", "Uncommon", 11.06),
)


def apply_patches(slim: list) -> list:
    """把上游缺表补进原始行（幂等：上游已含该表则跳过）。"""
    have = any("cryotic front (excavation)" in strip_tags(r.get("place", "")).lower() for r in slim)
    if have:
        print("  [补丁] Cryotic Front (Excavation)：上游已含该表，跳过注入")
        return slim
    for rot, item, rarity, chance in PATCH_CRYOTIC_EXCAVATION:
        slim.append(
            {
                "place": f"Europa/Cryotic Front (<b>Excavation</b>), Rotation {rot}",
                "item": item,
                "rarity": rarity,
                "chance": chance,
            }
        )
    print(
        f"  [补丁] 注入 Cryotic Front (Excavation) {len(PATCH_CRYOTIC_EXCAVATION)} 行"
        f"（官方页 2026-09-24 build；WFCD 缺该表，drop.wf.wiki 同源核对一致）"
    )
    return slim


def strip_tags(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s or "").strip()


def parse_place(place: str):
    """'Mercury/Apollodorus (<b>Survival</b>), Rotation A' → (星球, 节点, 模式, 轮次|'')。"""
    m = PLACE_RE.match(strip_tags(place))
    if not m:
        return None
    return (
        m.group("planet").strip(),
        m.group("node").strip(),
        m.group("mode").strip(),
        m.group("rot") or "",
    )


def base_relic_key(item: str) -> "str | None":
    """'Lith Q3 Relic (Radiant)' → 'lith q3 relic'；非遗物返回 None。"""
    low = (item or "").strip().lower()
    if not low.endswith("relic") or "pack" in low:
        return None
    return re.sub(r"\s*\(radiant\)\s*$", "", low)


def parse_any(place: str):
    """通用解析：常规地点走 parse_place；活动/剧情任务（无星球结构，
    如「Hot Mess, Rotation C」）走 raw 通道——(\":raw\", 任务名, \"\", 轮次)。"""
    pp = parse_place(place)
    if pp:
        return pp
    s = strip_tags(place)
    m = re.match(r"^(.*?),\s*Rotation\s+([ABC])\s*$", s)
    if m:
        return (":raw", m.group(1).strip(), "", m.group(2))
    return (":raw", s.strip(), "", "")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", help="本地 all.slim.json 路径（缺省从 GitHub 拉取）")
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args()

    if args.src:
        raw = Path(args.src).read_bytes()
        st = Path(args.src).stat()
        src_note = (
            f"本地 {args.src}（mtime "
            f"{datetime.fromtimestamp(st.st_mtime):%Y-%m-%d %H:%M}）"
            f" ★ --src 必须是**当前 master 快照**，旧快照会带回过期值"
        )
    else:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler(
                {"http": "http://127.0.0.1:7897", "https": "http://127.0.0.1:7897"}
            )
        )
        req = urllib.request.Request(SRC_URL, headers={"User-Agent": "sdjk-build/1.0"})
        raw = opener.open(req, timeout=300).read()
        src_note = "master 拉取"
    slim = json.loads(raw)
    print(f"[1/4] 源条目 {len(slim)}（md5 {hashlib.md5(raw).hexdigest()[:12]}，{src_note}）")
    slim = apply_patches(slim)

    old = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {}
    old_places = old.get("places") or {}
    planet_en = {zh: en for en, zh in PLANET_ZH.items()}

    # 旧 places 的反查索引：(星球en, 节点英文名, 轮次) → (pid, 原中文串)
    # ★ 必须带星球维度：不同星球可能存在同名节点。
    # ★ 旧数据里有 1456 条**未翻译的英文原串**（Conclave/The Index 等，
    #   2026-09-24 诊断实测）——单段条目用 parse_any 解析入索引，否则
    #   这些地点的遗物行会全部丢 pid。
    old_idx: dict[tuple[str, str, str], tuple[str, str]] = {}
    for pid, zh in old_places.items():
        parts = [p.strip() for p in zh.split("·")]
        if len(parts) >= 3:
            node = parts[1]
            rot = parts[-1][:-1] if re.fullmatch(r"[ABC]轮", parts[-1]) else ""
            planet = planet_en.get(parts[0], parts[0])
            key = (planet, node, rot)
        else:
            pp = parse_any(zh)
            if pp is None:
                continue
            planet, node, mode, rot = pp
            key = (planet, node, rot)
        old_idx.setdefault(key, (pid, zh))

    print("[2/4] 解析地点并复用旧翻译 …")
    place_key_to: dict[tuple, dict] = {}
    for row in slim:
        pp = parse_any(row.get("place", ""))
        if pp is None:
            continue
        planet, node, mode, rot = pp
        key = (planet, node, mode, rot)
        place_key_to.setdefault(key, {})
    # ★ 模式感知复用（不变量 2）：旧索引键丢了 mode ⇒ 先统计每组 (星球,节点,轮次)
    #   的 mode 集合，**只有单 mode 组才允许复用旧标签**；多 mode 组按 mode 新组合。
    group_modes: dict[tuple, set] = defaultdict(set)
    for planet, node, mode, rot in place_key_to:
        group_modes[(planet, node, rot)].add(mode)

    def _reuse_hit(planet: str, node: str, mode: str, rot: str):
        if len(group_modes.get((planet, node, rot), ())) != 1:
            return None
        return old_idx.get((planet, node, rot))

    new_places: dict[str, str] = {}
    key_pid: dict[tuple, str] = {}  # 地点四元组 → pid（本次一趟定死）
    pid_n = 0
    reused = composed = 0
    # ★ 两趟分配（2026-10-04 修）：**先**把单 mode 组的旧 pid 全部占住，**再**发新编号。
    #   一趟写法下，新组合拿到的编号可能与「后面才复用」的旧编号相撞
    #   （`while pid in new_places` 只查已占用、查不到未来占用）⇒ new_places[pid] 被
    #   覆盖、两个地点共用同一 pid（实测：4 个金星 Caches 里 2 个的 pid 被扎利曼地点
    #   顶掉，兜底卡于是显示「扎利曼 · The Greenway」）。
    for key in sorted(place_key_to):
        planet, node, mode, rot = key
        hit = _reuse_hit(planet, node, mode, rot)
        if not hit:
            continue
        pid, zh = hit
        new_places[pid] = zh
        key_pid[key] = pid
        reused += 1
    for key in sorted(place_key_to):
        if key in key_pid:
            continue
        planet, node, mode, rot = key
        pid_n += 1
        while str(pid_n) in new_places:  # 跳过与旧 pid 相撞的编号
            pid_n += 1
        pid = str(pid_n)
        if planet == ":raw":
            zh = f"{node}" + (f" · {rot}轮" if rot else "")
        else:
            pz = PLANET_ZH.get(planet, planet)
            zh = f"{pz} · {node} · {mode_zh(mode)}" + (f" · {rot}轮" if rot else "")
        new_places[pid] = zh
        key_pid[key] = pid
        composed += 1
    assert len(key_pid) == len(place_key_to), "地点键未全部拿到 pid"
    assert len(set(key_pid.values())) == len(key_pid), "pid 复用冲突（两个地点共号）"
    print(f"  地点 {len(new_places)}（复用旧翻译 {reused} / 新组合 {composed}）")
    if os.environ.get("BD_DEBUG"):
        miss = [k for k in place_key_to if k[:3] not in {(a, b, c) for (a, b, c) in old_idx}]
        print(f"  [debug] 未命中旧索引的地点键 {len(miss)} 个，样例:")
        for k in miss[:8]:
            print("    ", k)

    print("[3/4] 聚合遗物掉落 …")
    relic: dict[str, list] = defaultdict(list)
    skipped = 0
    for row in slim:
        key = base_relic_key(row.get("item", ""))
        if key is None:
            continue
        pp = parse_any(row.get("place", ""))
        if pp is None:
            skipped += 1
            continue
        pid_s = key_pid.get(pp)
        if pid_s is None:
            skipped += 1
            continue
        relic[key].append([int(pid_s), row.get("rarity", ""), row.get("chance")])
    print(f"  遗物 {len(relic)} 种（无法定位地点跳过 {skipped} 行）")

    print("[4/4] 重建 items（与 places 同源同编号）+ 写出 …")
    # ★ 不变量 1：items **不再续传值**。旧实现 `payload = dict(old)` 原样续传 items，
    #   而 places 每次重建、pid 重排 ⇒ 编号空间不同步（实测 items['athodai blueprint']
    #   的 4 条 pid 错连到 Axi A16/A17/A19/A2 Relic，真值是 6 个金星 Caches 节点）。
    #   这里：**键取并集**（旧键 ∪ 原始表键 ⇒ core/search.py 的搜索索引只增不减），
    #   **值一律从原始表按同一趟 key_pid 重建**；旧表独有的键给空表
    #   （_drops_card 见空即跳过 ⇒ 不会再输出错误来源）。
    items: dict[str, list] = {k: [] for k in (old.get("items") or {})}
    for row in slim:
        it = str(row.get("item") or "").strip().lower()
        if not it:
            continue
        pp = parse_any(row.get("place", ""))
        if pp is None:
            continue
        pid_s = key_pid.get(pp)
        if pid_s is None:
            continue
        rec = [int(pid_s), row.get("rarity", ""), row.get("chance")]
        lst = items.setdefault(it, [])
        if rec not in lst:
            lst.append(rec)
    over = sum(1 for lst in items.values() for r in lst if str(r[0]) not in new_places)
    print(f"  items {len(items)} 条（键取旧∪新并集）；pid 越界 {over}（应为 0）")
    # zh2en：值必须都是 items 的合法键（旧数据有 34 条死映射 ⇒ 指向不存在的键；
    # 重建后仍不成立的**剔除**，不再留半成品映射）
    zh2en = {zh: en for zh, en in (old.get("zh2en") or {}).items() if en in items}
    dropped = len(old.get("zh2en") or {}) - len(zh2en)
    if dropped:
        print(f"  zh2en 剔除死映射 {dropped} 条（指向不存在的 items 键）")

    payload = dict(old)  # names 等其余辅助键续传（names 无 drops 相关消费）
    payload["source"] = SRC_URL
    payload["updated"] = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")
    payload["places"] = dict(sorted(new_places.items(), key=lambda kv: int(kv[0])))
    payload["items"] = dict(sorted(items.items()))
    payload["zh2en"] = zh2en
    payload["relic_drops"] = {k: sorted(v) for k, v in sorted(relic.items())}
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    old_keys = set((old.get("relic_drops") or {}))
    new_keys = set(relic)
    print(f"[OK] {out_path}（{out_path.stat().st_size / 1024:.0f} KB）")
    print(
        f"  遗物键 {len(old_keys)} → {len(new_keys)}"
        f"（新增 {len(new_keys - old_keys)}、移除 {len(old_keys - new_keys)}）"
    )
    print(f"  新增键样例: {sorted(new_keys - old_keys)[:6]}")
    print(f"  移除键样例: {sorted(old_keys - new_keys)[:6]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
