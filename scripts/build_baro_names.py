# -*- coding: utf-8 -*-
"""生成奸商（Baro Ki'Teer）物品的**官方简中名**映射：core/data/baro_names_zh.json

为什么需要单独做
----------------
`core/data/baro_history.json` 来自 wiki 的 `Module:Baro/data`，**只有英文名**；
而项目里原有的 DE 中文表都是按 **uniqueName 索引**的
（`de/name_zh.json` = uniqueName → 中文），没有「英文**显示名** → 中文」这一层。
靠 warframe.market 的 gameRef 逐个反查只有 27% 覆盖（大量装饰类 WM 里查不到
uniqueName，实测 47 把玄骸武器也是同一限制）。

正确做法
--------
DE PublicExportPlus 同时提供两份**键完全一致**的词表：

    https://browse.wf/warframe-public-export-plus/dict.en.json
    https://browse.wf/warframe-public-export-plus/dict.zh.json

同为 35,865 条 `/Lotus/Language/...` 键：`dict.en` 是英文显示名、
`dict.zh` 是官方简中名。取交集即得完整对照表 —— 实测对 Baro 的 466 件物品
覆盖 **97%**（454/466），远好于按 uniqueName 反查的 27%。

用法
----
    python3 scripts/build_baro_names.py            # 联网重建（词表会缓存到 core/data/_cache/）
    python3 scripts/build_baro_names.py --offline  # 只用本地缓存
"""
from __future__ import annotations

import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DE = ROOT / "core" / "data" / "de"
OUT = ROOT / "core" / "data" / "baro_names_zh.json"
BARO = ROOT / "core" / "data" / "baro_history.json"
CACHE = ROOT / "core" / "data" / "_cache"

BASE = "https://browse.wf/warframe-public-export-plus"
DICT_EN = f"{BASE}/dict.en.json"
DICT_ZH = f"{BASE}/dict.zh.json"
UA = {"User-Agent": "Mozilla/5.0 Chrome/124.0"}

# 遗物：Baro 会卖 `Axi A2 Relic`，词表里没有，但规则固定（与遗物指令同口径）
RELIC_TIER = {"lith": "古纪", "meso": "中纪", "neo": "前纪",
              "axi": "后纪", "requiem": "安魂"}
RELIC_RE = re.compile(r"^(Lith|Meso|Neo|Axi|Requiem)\s+(\S+)\s+Relic$", re.I)


def http_json(url: str, tries: int = 4):
    last = None
    for a in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            return json.loads(urllib.request.urlopen(req, timeout=150).read())
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (429, 500, 502, 503) and a < tries - 1:
                time.sleep(3 * (a + 1))
                continue
            raise
        except Exception as e:              # noqa: BLE001
            last = e
            if a < tries - 1:
                time.sleep(3)
                continue
            raise
    raise last                                   # pragma: no cover


def load_dict(name: str, url: str, offline: bool) -> dict:
    """优先读本地缓存（core/data/_cache/），没有再联网拉并写缓存。"""
    CACHE.mkdir(parents=True, exist_ok=True)
    local = CACHE / f"{name}.json"
    if local.exists() and (offline or local.stat().st_size > 100_000):
        try:
            return json.loads(local.read_text(encoding="utf-8"))
        except Exception:                        # noqa: BLE001
            pass
    if offline:
        raise SystemExit(f"离线模式但缺少缓存 {local}")
    data = http_json(url)
    local.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return data


def norm(s: str) -> str:
    """归一化：忽略空格/标点/大小写（`5 x Foo` 与 `5x Foo` 视为同一名）。"""
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", (s or "").lower())


def strip_qty(n: str) -> str:
    return re.sub(r"^\d+\s*x\s+", "", n, flags=re.I)


def main() -> int:
    offline = "--offline" in sys.argv
    if not BARO.exists():
        print("缺少 core/data/baro_history.json，先跑 scripts/build_baro_history.py")
        return 1
    names: list[str] = list(json.loads(BARO.read_text(encoding="utf-8")).get("items") or {})
    print(f"Baro 物品 {len(names)} 个")

    print("取 DE 官方双语词表 dict.en.json / dict.zh.json …")
    try:
        d_en = load_dict("dict.en", DICT_EN, offline)
        d_zh = load_dict("dict.zh", DICT_ZH, offline)
    except Exception as e:                       # noqa: BLE001
        print(f"  取词表失败（{type(e).__name__}: {e}）—— 保留现有 {OUT.name} 不动")
        return 1
    print(f"  en {len(d_en)} 条 / zh {len(d_zh)} 条 / 同键 {len(set(d_en) & set(d_zh))} 条")

    m: dict[str, str] = {}
    for k, a in d_en.items():
        b = d_zh.get(k)
        if isinstance(a, str) and isinstance(b, str) and a.strip() and b.strip():
            m.setdefault(a.strip().lower(), b.strip())
    mn: dict[str, str] = {}
    for k, v in m.items():
        mn.setdefault(norm(k), v)
    print(f"  英文显示名 → 中文：字面 {len(m)} 条 / 归一化 {len(mn)} 条")

    out: dict[str, str] = {}
    via = {"dict": 0, "norm": 0, "relic": 0, "none": 0}
    misses: list[str] = []
    for n in names:
        got, src = "", ""
        for v in (n, strip_qty(n), re.sub(r"^\d+\s*Day\s+", "", n, flags=re.I)):
            if v.strip().lower() in m:
                got, src = m[v.strip().lower()], "dict"
                break
            if norm(v) in mn:
                got, src = mn[norm(v)], "norm"
                break
        if not got:
            rl = RELIC_RE.match(strip_qty(n))
            if rl:
                got = f"{RELIC_TIER[rl.group(1).lower()]} {rl.group(2)} 遗物"
                src = "relic"
        if got:
            out[n] = got
            via[src] += 1
        else:
            via["none"] += 1
            misses.append(n)

    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
                   encoding="utf-8")
    total = len(names)
    print(f"\n写入 {OUT.relative_to(ROOT)}：{len(out)}/{total} = {len(out) * 100 // total}%")
    print(f"  词表字面 {via['dict']} ｜ 归一化 {via['norm']} ｜ 遗物规则 {via['relic']}"
          f" ｜ 未命中 {via['none']}")
    if misses:
        print("  未命中（卡片上显示英文原名）：")
        for x in misses[:12]:
            print("     ", x)

    # 预测候选（卡片真正显示的那批）覆盖率
    sys.path.insert(0, str(ROOT))
    try:
        from core import baro as B                       # noqa: E402
        cand = B.predict(60)
        ch = [r for r in cand if out.get(r["name"])]
        print(f"\n预测候选 {len(cand)} 个 → 中文覆盖 {len(ch)} "
              f"({len(ch) * 100 // max(len(cand), 1)}%)")
        for r in cand[:16]:
            z = out.get(r["name"]) or ""
            print(f"   {'✓' if z else '·'} {r['name'][:34]:36s} {z}")
    except Exception as e:                        # noqa: BLE001
        print("（跳过候选覆盖率统计：%s）" % e)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
