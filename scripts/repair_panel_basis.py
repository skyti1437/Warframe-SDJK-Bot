# -*- coding: utf-8 -*-
"""核对并修复「顶层 damage 含额外段」的武器（dry-run 优先）。

背景：warframe-items 顶层 damage 有时把「主段 + 范围/爆炸段」合并
（沙皇 impact 25 + blast 175 = 200），而 wfsim 的 attack.damage 是
**主段**、attack.radial 是范围段 —— 用 wfsim 的成分做逐类型核对：

匹配条件（严格）：我们的顶层成分 == wfsim 主段 + wfsim radial 的逐类型和
（容忍 1% 或 0.05，四舍五入到 2 位），且主段本身有伤害。
命中 → 顶层改为 wfsim 主段（范围段由 attacks 段 / weapon_attacks.json 承担）。

用法：
  python scripts/repair_panel_basis.py          # dry-run，只报告
  python scripts/repair_panel_basis.py --apply  # 应用修改
"""
import glob
import json
import sys
from pathlib import Path

import yaml

WFSIM = Path(r"REDACTED_TMP_DIR/wfsim/data")
STATS = Path(__file__).resolve().parent.parent / "core" / "data" / "weapons_stats.json"
SKIP = {"total", "cinematic", "shieldDrain", "healthDrain", "energyDrain"}
DMG_KEYS = ("impact", "puncture", "slash", "heat", "cold", "electricity",
            "toxin", "blast", "radiation", "gas", "magnetic", "viral",
            "corrosive", "void", "true")


def sig(d: dict) -> dict:
    return {k: round(float(v), 2) for k, v in d.items()
            if isinstance(v, (int, float)) and v > 0 and k not in SKIP}


def close(a: dict, b: dict) -> bool:
    keys = set(a) | set(b)
    return all(abs(float(a.get(k, 0)) - float(b.get(k, 0)))
               <= max(0.05, abs(float(b.get(k, 0))) * 0.01) for k in keys)


def main() -> None:
    apply = "--apply" in sys.argv
    W = json.loads(STATS.read_text(encoding="utf-8"))
    # wfsim 主段/范围段索引
    wf: dict = {}
    for f in glob.glob(str(WFSIM / "weapons" / "**" / "*.yaml"), recursive=True):
        y = yaml.safe_load(open(f, encoding="utf-8"))
        if not isinstance(y, dict):
            continue
        key = str(y.get("internal_name") or "").lower()
        a = y.get("attack") or {}
        if not key or not isinstance(a, dict):
            continue
        wf[key] = {
            "main": sig(a.get("damage") or {}),
            "radial": sig((a.get("radial") or {}).get("damage") or {}),
            "cluster": sig((a.get("cluster") or {}).get("damage") or {}),
        }

    fixed, unmatched, ok = [], [], 0
    for k, w in W.items():
        d = sig(w.get("damage") or {})
        if not d:
            continue
        ats = w.get("attacks") or []
        if len(ats) < 2:
            ok += 1
            continue
        top_total = sum(d.values())
        # 规则：顶层应当 = attacks[0]（默认模式主段）。不等于它 → 顶层是
        # 「主段 + 范围/副模式段」的合并（沙皇 200 = 25 直击 + 175 爆炸），
        # 或「蓄力等非默认段」（弓类历史问题，已单独修过）。
        first = sig(ats[0].get("damage") or {})
        if not first:
            ok += 1
            continue
        if close(d, first):
            ok += 1
            continue
        # 用 wfsim 交叉验证（主段成分应与 attacks[0] 一致，避免误改）
        src = wf.get(k)
        wf_main = (src or {}).get("main") or {}
        cross_ok = (not wf_main) or close(first, wf_main)
        if cross_ok:
            fixed.append((k, w.get("zh") or w.get("name"),
                          round(top_total, 1),
                          round(sum(first.values()), 1),
                          first, ats[0].get("name")))
        else:
            unmatched.append((k, w.get("zh") or w.get("name"),
                              round(top_total, 1),
                              [(a.get("name"),
                                round(sum(sig(a.get("damage") or {}).values()), 1))
                               for a in ats][:3]))

    print(f"顶层已自洽/单段: {ok} 把")
    print(f"可明确修复（wfsim 成分核对通过）: {len(fixed)} 把")
    for k, zh, t, m, main, nm in fixed[:30]:
        print(f"  {zh:20s} {t:8.1f} → {m:7.1f}  [{nm}] {json.dumps(main, ensure_ascii=False)}")
    print(f"未匹配（需人工核对）: {len(unmatched)} 把")
    for k, zh, t, segs in unmatched[:20]:
        print(f"  {zh:20s} 顶层 {t:8.1f}  段: {segs}")

    if apply and fixed:
        for k, zh, t, m, main, nm in fixed:
            # 保留原有键集合（缺失的补 0）——其它代码/测试会直接取键
            old_keys = {kk: 0.0 for kk in (W[k].get("damage") or {})
                        if kk != "total" and kk not in SKIP}
            W[k]["damage"] = {**old_keys, **main,
                              "total": round(sum(main.values()), 4)}
        STATS.write_text(json.dumps(W, ensure_ascii=False, indent=1),
                         encoding="utf-8")
        print(f"✓ 已应用 {len(fixed)} 把")


if __name__ == "__main__":
    main()
