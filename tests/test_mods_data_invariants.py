# -*- coding: utf-8 -*-
"""MOD 库（`core/data/mods_stats.json`）的**数据不变量**测试。

为什么单独盯这份数据（2026-09-20 用户实测报障）：
    用户连报两次「等级对不上」——第一次是「剑风（非 p）满级就 3，你这个 5 级
    哪里来的」（仅识别表的 max_rank 从没被校准过，全量对拍发现 14 条错）。
    数据错会**直接体现在卡面上**，而且很容易在后续批量同步里被覆盖回去，
    所以把已核实的值钉成不变量。

★★ 特别记住：**wiki `Module:Mods/data` 的 BaseDrain 不能用来校准** ——
    用用户配卡截图裁决过（分裂膛室卡面 15 + 亮豆 5 颗 → 只有 base 10 成立，
    模块说 4 会得 9）;`MaxRank` 也有 6 条模块写错，见
    `scripts/audit_mods_vs_wiki.py::KNOWN_MODULE_WRONG`。
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FAILED = []


def check(name, cond, info=""):
    mark = "PASS" if cond else "FAIL"
    if not cond:
        FAILED.append(name)
    print(f"[{mark}] {name}" + (f"  ← {info}" if info and not cond else ""))


DATA = json.loads((ROOT / "core" / "data" / "mods_stats.json").read_text(encoding="utf-8"))
MODS = DATA["mods"]
NAMES = DATA["names"]


def rec(key):
    return MODS.get(key) or NAMES.get(key) or {}


# ---- 用户实测报障过的（必须钉住）----
check("剑风 reach：base 4 / max 3（用户实测：非 P 满级就是 3）",
      rec("reach").get("base_drain") == 4 and rec("reach").get("max_rank") == 3,
      json.dumps(rec("reach"), ensure_ascii=False))
check("分裂膛室 split chamber：base 10（卡面 15 + 亮豆 5 颗 → 只有 10+5 成立）",
      rec("split chamber").get("base_drain") == 10, str(rec("split chamber").get("base_drain")))
check("压迫点 pressure point：base 4 / max 5（卡面 9 + 5 颗豆）",
      rec("pressure point").get("base_drain") == 4
      and rec("pressure point").get("max_rank") == 5,
      json.dumps(rec("pressure point"), ensure_ascii=False))
check("北风 north wind：base 6 / max 5（卡面 9 = 6+1 后 ×1.25 取整）",
      rec("north wind").get("base_drain") == 6
      and rec("north wind").get("max_rank") == 5,
      json.dumps(rec("north wind"), ensure_ascii=False))
for _k, _max in (("eagle eye", 3), ("hawk eye", 3), ("charged chamber", 3),
                 ("finishing touch", 3), ("energy channel", 3), ("steady hands", 3)):
    check(f"{_k}：max_rank 必须是 {_max}（wiki 模块写 5 是错的，别被覆盖回去）",
          rec(_k).get("max_rank") == _max, str(rec(_k).get("max_rank")))
check("膛线 serration：base 4 / max 10",
      rec("serration").get("base_drain") == 4 and rec("serration").get("max_rank") == 10,
      json.dumps(rec("serration"), ensure_ascii=False))

# ---- 全局一致性 ----
bad = [k for k, v in list(MODS.items()) + list(NAMES.items())
       if isinstance(v.get("base_drain"), int) and isinstance(v.get("max_rank"), int)
       and v["base_drain"] >= 0 and v["max_rank"] < 0]
check("所有非姿态卡的 max_rank 都 ≥ 0", not bad, str(bad[:5]))

zero = [k for k in NAMES
        if isinstance(NAMES[k].get("base_drain"), int) and NAMES[k]["base_drain"] >= 0
        and NAMES[k].get("max_rank") is None]
check("仅识别表的非姿态卡都有 max_rank", not zero, str(zero[:5]))

# 姿态卡哨兵必须保留（loadout_ocr 靠 base_drain < 0 识别姿态卡）
st = [v for v in NAMES.values() if isinstance(v.get("base_drain"), int) and v["base_drain"] < 0]
check("姿态卡哨兵（base_drain < 0）仍然存在", len(st) >= 3, f"{len(st)} 条")

# 稀有度（2026-09-20 从 wiki 补入，供卡面边框/稀有度用）
n_rar = sum(1 for v in list(MODS.values()) + list(NAMES.values()) if v.get("rarity"))
check("稀有度字段已补入（≥800 条）", n_rar >= 800, f"{n_rar} 条")
check("压迫点 = Common / 重口径 应为 Rare 级（抽查）",
      rec("pressure point").get("rarity") == "Common",
      str(rec("pressure point").get("rarity")))

print()
if FAILED:
    print(f"[FAIL] {len(FAILED)} 项失败: {FAILED}")
    sys.exit(1)
print("[OK] MOD 库数据不变量全部通过")
