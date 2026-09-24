# -*- coding: utf-8 -*-
"""生成「英文显示名 → DE 官方简中名」对照表：core/data/de/name_en_zh.json

为什么需要
----------
wiki 页面名必须落在**国际服**名字上：DE 官方简中对**战甲名不翻译**
（Nekros / Banshee / Volt 直接用英文），对武器 / MOD / 赋能才翻译
（Torid → 托里德、Fleeting Expertise → 弹指瞬技）。社区黑话（摸尸 / 音妈）
与国服旧译（御魂主宰 / 恸哭女妖）拼进 /wiki/<名> 都是死链（2026-09-24
用户实测反馈）。

DE PublicExportPlus 的 dict.en.json / dict.zh.json 是**同键**的官方双语表
（各 35,865 条 `/Lotus/Language/...`），取交集即得完整对照。这里只保留
**本地检索索引可能命中的名字**（别名 slug / 武器表 / MOD 表 / 赋能表 /
DE 物品表 / 掉落表 / 玄骸武器），并只收「已翻译」条目（zh != en）——
未翻译的内容运行时回落到英文原名即可，不必入库。

用法
----
    python3 scripts/build_name_en_zh.py            # 联网重建（词表缓存到 core/data/_cache/）
    python3 scripts/build_name_en_zh.py --offline  # 只用本地缓存
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
DATA = ROOT / "core" / "data"
DE = DATA / "de"
OUT = DE / "name_en_zh.json"
CACHE = DATA / "_cache"

BASE = "https://browse.wf/warframe-public-export-plus"
DICT_EN = f"{BASE}/dict.en.json"
DICT_ZH = f"{BASE}/dict.zh.json"
UA = {"User-Agent": "Mozilla/5.0 Chrome/124.0"}


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
    """与 core/search.py::_norm 同口径（小写、非词字符压成单空格）。"""
    return " ".join(re.sub(r"[^0-9a-z一-鿿]+", " ", (s or "").lower()).split())


def _jload(path: Path) -> dict:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def name_space() -> set[str]:
    """本地检索索引能命中的英文名集合（wiki 查询的实际可达面）。"""
    space: set[str] = set()

    def add(x: str) -> None:
        k = norm(x)
        if k:
            space.add(k)

    al = _jload(DATA / "aliases.json")
    for sec in ("wm_items", "riven_items"):
        for slug in (al.get(sec) or {}).values():
            if not slug:
                continue
            s = str(slug).replace("_", " ")
            add(s)
            add(s.replace(" set", ""))          # banshee prime set → banshee prime
            add(s.replace(" blueprint", ""))
    for v in _jload(DATA / "weapons_stats.json").values():
        if isinstance(v, dict):
            add(v.get("name") or "")
    ms = _jload(DATA / "mods_stats.json")
    for sec in ("mods", "names"):
        for v in (ms.get(sec) or {}).values():
            if isinstance(v, dict):
                add(v.get("name") or "")
    for v in (_jload(DATA / "arcanes_stats.json").get("arcanes") or {}).values():
        if isinstance(v, dict):
            add(v.get("name") or "")
    for it in (_jload(DE / "de_items_zh.json").get("items") or []):
        if isinstance(it, dict):
            add(it.get("en") or "")
    for k in (_jload(DATA / "drops.json").get("items") or {}):
        add(k)
    for v in (_jload(DATA / "lich_weapons.json") or {}).values():
        if isinstance(v, dict):
            add(v.get("en") or "")
    return space


def main() -> int:
    offline = "--offline" in sys.argv
    print("取 DE 官方双语词表 dict.en.json / dict.zh.json …")
    try:
        d_en = load_dict("dict.en", DICT_EN, offline)
        d_zh = load_dict("dict.zh", DICT_ZH, offline)
    except Exception as e:                       # noqa: BLE001
        print(f"  取词表失败（{type(e).__name__}: {e}）—— 保留现有 {OUT.name} 不动")
        return 1
    print(f"  en {len(d_en)} 条 / zh {len(d_zh)} 条 / 同键 {len(set(d_en) & set(d_zh))} 条")

    pair: dict[str, str] = {}
    for k, a in d_en.items():
        b = d_zh.get(k)
        if isinstance(a, str) and isinstance(b, str) and a.strip() and b.strip():
            pair.setdefault(norm(a), b.strip())
    print(f"  英文显示名 → 官方名：归一化 {len(pair)} 条")

    space = name_space()
    print(f"  本地检索名空间：{len(space)} 条")

    names = {k: v for k, v in pair.items() if k in space and norm(v) != k}
    names = dict(sorted(names.items()))

    OUT.write_text(json.dumps({
        "_meta": {
            "source": "browse.wf/warframe-public-export-plus dict.en.json + "
                      "dict.zh.json（同键交集，官方简中）",
            "note": "归一化英文显示名 → 官方简中名；只收已翻译条目（zh != en）——"
                    "未翻译内容（如战甲名 Nekros）国际服直接用英文，运行时回落英文原名。"
                    "名空间 = 本地检索索引可达的名字（别名 slug / 武器 / MOD / 赋能 / "
                    "DE 物品 / 掉落 / 玄骸武器）。",
            "generated": time.strftime("%Y-%m-%d"),
            "count": len(names),
        },
        "names": names,
    }, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")

    print(f"\n写入 {OUT.relative_to(ROOT)}：{len(names)} 条"
          f"（名空间内已翻译 {len([k for k in pair if k in space])} 条，"
          f"去掉 zh==en 后剩 {len(names)}）")
    for probe in ("nekros prime", "banshee prime", "torid", "fleeting expertise",
                  "primed continuity", "hunter munitions", "vacuum"):
        mark = "✓" if probe in names else ("· 未翻译/未收录（回落英文名）")
        print(f"   {probe:22s} -> {names.get(probe, mark)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
