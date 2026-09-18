# -*- coding: utf-8 -*-
"""从 wfsim（github.com/magenie33/wfsim）数据抽取枪械灵化形态基础数值。

数据源：data/weapons/{primary,secondary,sentinel,archgun}/*.yaml 中 form: incarnon
的条目（attack 块 = 该形态的基础面板，未含灵化进化 perk 的加成，进化加成
各玩家选择不同，识卡侧仍从面板反推吸收）。

join 方式：incarnon 文件没有 internal_name，通过 transforms_from → 基础形态
文件 → internal_name（DE uniqueName）→ 我们 weapons_stats.json 的 key
（小写 uniqueName）。

许可说明：只取事实数值（伤害向量/暴击/触发/射速），按我们自己的 schema
重录；zh 名取自 wfsim i18n/zh（其注明来源为 DE 官方客户端字符串，与我们
「DE 官方简中」的译名约定一致）。不搬运其 YAML 表达。
"""
import json
import sys
from pathlib import Path

import yaml

WFSIM = Path(r"REDACTED_TMP_DIR/wfsim/data")
OUT = Path(r"REDACTED_PROJECT_DIR/core/data/incarnon_forms.json")

DAMAGE_KEYS = ("impact", "puncture", "slash", "heat", "cold", "electricity",
               "toxin", "blast", "radiation", "gas", "magnetic", "viral",
               "corrosive", "void", "true")


def load_yaml(p: Path):
    with p.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def main():
    names = load_yaml(WFSIM / "i18n" / "zh" / "names.yaml").get("weapons") or {}
    out = {"_meta": {
        "source": "github.com/magenie33/wfsim data/weapons/*（form: incarnon），"
                  "数值为其按 wiki/实测整理的事实数据；zh 名为 DE 官方简中",
        "updated": "2026-09-16",
        "scope": "枪械灵化（近战灵化 wfsim 尚未导入）；attack 块不含灵化进化 perk",
    }}
    n_ok = 0
    for cat in ("primary", "secondary", "sentinel", "archgun"):
        for p in sorted((WFSIM / "weapons" / cat).glob("*.yaml")):
            y = load_yaml(p)
            if not isinstance(y, dict) or y.get("form") != "incarnon":
                continue
            atk = y.get("attack") or {}
            dmg = {k: float(atk.get("damage", {}).get(k) or 0.0)
                   for k in DAMAGE_KEYS}
            dmg = {k: v for k, v in dmg.items() if v}
            total = sum(dmg.values())
            if total <= 0:
                continue
            dmg["total"] = round(total, 4)

            # join 基础形态 → internal_name → 我们 weapons_stats 的 key
            base_id = y.get("transforms_from") or (
                (y.get("transform_group") or "").split(".")[0])
            base_path = WFSIM / "weapons" / cat / f"{base_id}.yaml"
            internal = ""
            base_name = base_zh = ""
            if base_path.exists():
                b = load_yaml(base_path)
                internal = str(b.get("internal_name") or "").lower()
                base_name = str(b.get("name") or "")
                base_zh = str(names.get(base_id) or "")
            if not internal:
                print(f"  !! 无 internal_name，跳过: {p.name}")
                continue

            out[internal] = {
                "form_id": y.get("id"),
                "form_name": y.get("name"),
                "form_zh": names.get(y.get("id")) or "",
                "base_name": base_name,
                "base_zh": base_zh,
                "slot": y.get("slot"),
                "class": y.get("class"),
                "damage": dmg,
                "criticalChance": float(atk.get("crit_chance") or 0.0),
                "criticalMultiplier": float(atk.get("crit_multiplier") or 1.0),
                "procChance": float(atk.get("status_chance") or 0.0),
                "fireRate": float(atk.get("fire_rate") or 0.0),
                "multishot": float(atk.get("multishot") or 1.0),
            }
            # 范围（AoE）段：灵化形态的第二段伤害（与直击并列结算）
            rad = atk.get("radial")
            if isinstance(rad, dict) and rad.get("damage"):
                r_dmg = {k: float(rad["damage"].get(k) or 0.0)
                         for k in DAMAGE_KEYS}
                r_dmg = {k: v for k, v in r_dmg.items() if v}
                if r_dmg:
                    out[internal]["radial"] = {
                        "damage": {**r_dmg,
                                   "total": round(sum(r_dmg.values()), 4)},
                        "criticalChance": float(rad.get("crit_chance") or 0.0),
                        "criticalMultiplier":
                            float(rad.get("crit_multiplier") or 1.0),
                        "procChance": float(rad.get("status_chance") or 0.0),
                        "radius_m": float(rad.get("radius_m") or 0.0),
                        "falloff_reduction":
                            float(rad.get("falloff_reduction") or 0.0),
                    }
            if atk.get("forced_procs"):
                out[internal]["forced_procs"] = [str(x)
                                                 for x in atk["forced_procs"]]
            rc = atk.get("ricochet")
            if isinstance(rc, dict) and rc.get("bounces"):
                out[internal]["ricochet"] = {
                    "bounces": int(rc.get("bounces") or 0),
                    "headshot_chance": float(rc.get("headshot_chance") or 0.0),
                }
            n_ok += 1

    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    print(f"✓ 写入 {OUT.name}: {n_ok} 个灵化形态")
    # 抽查
    for k in ("/lotus/weapons/tenno/rifle/rifle",):
        if k in out:
            e = out[k]
            print("  抽查", k, "→", e["base_zh"], e["damage"],
                  "cc", e["criticalChance"], "cm", e["criticalMultiplier"])
    missing_zh = sum(1 for k, v in out.items() if k != "_meta" and not v["base_zh"])
    print(f"  缺 zh 名: {missing_zh}")


if __name__ == "__main__":
    sys.exit(main())
