#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""重建 core/data/drops.json（遗物掉落出处表）。

数据源：WFCD warframe-drop-data ``data/all.slim.json``（DE 官方掉落表精简版，
list of {place, item, rarity, chance}，place 形如
``Mercury/Apollodorus (<b>Survival</b>), Rotation A``）。

产出（沿用既有结构，core/drops.py 只消费 places/relic_drops）：
  places       {pid: "水星 · Apollodorus · 生存 · A轮"}
  relic_drops  {基名小写: [[pid, rarity, chance], ...]}
  items/names/zh2en/source 等辅助键从旧文件**原样续传**（运行期未消费，
  保留以维持完整谱系）；source/updated 刷新。

筛选与映射规则（2026-09-24 固化，此前构建器未入库）：
- 遗物键：item 以 ``relic`` 结尾者才收；剥掉 `` (radiant)`` 辐射形态后缀
  合并到基名（小写）；排除 Syndicate Relic Pack 一类非遗物条目；
- 地点：按 (节点英文名, 轮次) 与旧 places 的中文串对齐复用翻译；
  新节点用静态 星球/任务类型 中文表兜底（查不到的保留英文原文）。

用法：
    python scripts/build_drops.py                    # 从 GitHub 拉最新源
    python scripts/build_drops.py --src all.slim.json  # 从本地文件构建
"""
from __future__ import annotations

import argparse
import json
import os
import re
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "core" / "data" / "drops.json"
SRC_URL = ("https://raw.githubusercontent.com/WFCD/warframe-drop-data/"
           "master/data/all.slim.json")

PLACE_RE = re.compile(
    r"^(?P<planet>[^/]+)/(?P<node>.+?)\s*\((?P<mode>[^)]+)\)"
    r"(?:,\s*Rotation\s+(?P<rot>[ABC]))?\s*$")

PLANET_ZH = {
    "Mercury": "水星", "Venus": "金星", "Earth": "地球", "Mars": "火星",
    "Phobos": "火卫一", "Ceres": "谷神星", "Jupiter": "木星", "Saturn": "土星",
    "Uranus": "天王星", "Neptune": "海王星", "Pluto": "冥王星", "Sedna": "塞德娜",
    "Eris": "阋神星", "Europa": "木卫二", "Lua": "月球", "Deimos": "火卫二",
    "Void": "虚空", "Kuva Fortress": "女皇要塞", "Zariman": "扎利曼",
    "Earth Proxima": "地球比邻星域", "Venus Proxima": "金星比邻星域",
    "Saturn Proxima": "土星比邻星域", "Neptune Proxima": "海王星比邻星域",
    "Pluto Proxima": "冥王星比邻星域", "Veil Proxima": "面纱比邻星域",
    "Ceres Proxima": "谷神星比邻星域", "Jupiter Proxima": "木星比邻星域",
}

MODE_ZH = {
    "Survival": "生存", "Defense": "防御", "Interception": "拦截",
    "Capture": "捕获", "Rescue": "救援", "Spy": "间谍", "Sabotage": "破坏",
    "Excavation": "挖掘", "Defection": "防卫", "Disruption": "中断",
    "Assassination": "刺杀", "Mobile Defense": "移动防御", "Hijack": "劫持",
    "Skirmish": "小规模冲突", "Orphix": "奥菲克斯", "Vault": "宝藏",
    "Pursuit": "追击", "Rush": "冲刺", "Volatile": "易爆",
    "Void Flood": "虚空洪泛", "Void Cascade": "虚空瀑布", "Void Armageddon": "虚空末日",
    "Conjunction Survival": "交汇生存", "Alchemy": "炼金术", "Assault": "进攻",
}


def strip_tags(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s or "").strip()


def parse_place(place: str):
    """'Mercury/Apollodorus (<b>Survival</b>), Rotation A' → (星球, 节点, 模式, 轮次|'')。"""
    m = PLACE_RE.match(strip_tags(place))
    if not m:
        return None
    return (m.group("planet").strip(), m.group("node").strip(),
            m.group("mode").strip(), m.group("rot") or "")


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
    else:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler(
            {"http": "http://127.0.0.1:7897", "https": "http://127.0.0.1:7897"}))
        req = urllib.request.Request(SRC_URL,
                                     headers={"User-Agent": "sdjk-build/1.0"})
        raw = opener.open(req, timeout=300).read()
    slim = json.loads(raw)
    print(f"[1/4] 源条目 {len(slim)}")

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
    new_places: dict[str, str] = {}
    pid_n = 0
    reused = composed = 0
    for key in sorted(place_key_to):
        planet, node, mode, rot = key
        hit = old_idx.get((planet, node, rot))
        if hit:
            pid, zh = hit
            new_places[pid] = zh
            reused += 1
            continue
        pid_n += 1
        while str(pid_n) in new_places:      # 跳过与旧 pid 相撞的编号
            pid_n += 1
        pid = str(pid_n)
        if planet == ":raw":
            zh = f"{node}" + (f" · {rot}轮" if rot else "")
        else:
            pz = PLANET_ZH.get(planet, planet)
            mz = MODE_ZH.get(mode, mode)
            zh = f"{pz} · {node} · {mz}" + (f" · {rot}轮" if rot else "")
        new_places[pid] = zh
        composed += 1
    print(f"  地点 {len(new_places)}（复用旧翻译 {reused} / 新组合 {composed}）")
    if os.environ.get("BD_DEBUG"):
        miss = [k for k in place_key_to if k[:3] not in
                {(a, b, c) for (a, b, c) in old_idx}]
        print(f"  [debug] 未命中旧索引的地点键 {len(miss)} 个，样例:")
        for k in miss[:8]:
            print("    ", k)

    print("[3/4] 聚合遗物掉落 …")
    # 地点四元组 → pid（构建期一次建好，避免逐行反查）
    key_pid: dict[tuple, str] = {}
    for (planet, node, mode, rot), _zh in place_key_to.items():
        hit = old_idx.get((planet, node, rot))
        if hit:
            key_pid[(planet, node, mode, rot)] = hit[0]
            continue
        if planet == ":raw":
            zh = f"{node}" + (f" · {rot}轮" if rot else "")
        else:
            pz = PLANET_ZH.get(planet, planet)
            mz = MODE_ZH.get(mode, mode)
            zh = f"{pz} · {node} · {mz}" + (f" · {rot}轮" if rot else "")
        for pid, zh_old in new_places.items():
            if zh_old == zh:
                key_pid[(planet, node, mode, rot)] = pid
                break
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

    print("[4/4] 写出 …")
    payload = dict(old)  # 续传 items/names/zh2en 等辅助键
    payload["source"] = SRC_URL
    payload["updated"] = datetime.now(timezone.utc).astimezone().strftime(
        "%Y-%m-%d %H:%M")
    payload["places"] = dict(sorted(new_places.items(), key=lambda kv: int(kv[0])))
    payload["relic_drops"] = {k: sorted(v) for k, v in sorted(relic.items())}
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                        encoding="utf-8")

    old_keys = set((old.get("relic_drops") or {}))
    new_keys = set(relic)
    print(f"[OK] {out_path}（{out_path.stat().st_size / 1024:.0f} KB）")
    print(f"  遗物键 {len(old_keys)} → {len(new_keys)}"
          f"（新增 {len(new_keys - old_keys)}、移除 {len(old_keys - new_keys)}）")
    print(f"  新增键样例: {sorted(new_keys - old_keys)[:6]}")
    print(f"  移除键样例: {sorted(old_keys - new_keys)[:6]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
