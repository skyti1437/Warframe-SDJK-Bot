# -*- coding: utf-8 -*-
"""从 wfsim（github.com/magenie33/wfsim）抽取 MOD 元数据补强我们的数据。

产出 core/data/mods_wfsim_extra.json（独立文件，不动 mods_stats.json 本体，
避免被我们的构建脚本覆盖）：
- family：家族互斥组（wfsim 92 组，同族只能装一张 —— 含 Prime/普通变体）
- conditionals：结构化的条件堆叠（镀层类 on_kill / on_hit buff），
  grants 映射到我们 spec 字段的才标 mappable

只取事实（家族归属、堆叠数值/层数/时长），不搬其 YAML 表达。
许可：wfsim 引擎 AGPL-3.0 —— 只借事实数据，代码一律不抄。
"""
import glob
import json
import os

try:
    import yaml
except ImportError:  # noqa: BLE001 —— 插件运行本身不需要 yaml，只有这几个
    raise SystemExit(   # 数据构建脚本要；给可操作的提示而不是裸 ImportError
        "本脚本需要 PyYAML：pip install pyyaml"
        "（插件运行时并不依赖它，仅构建数据用）")

WFSIM = str(Path.home() / "tmp" / "wfsim" / "data" / "mods")
OUT = os.path.join(os.path.dirname(__file__), os.pardir, "core", "data",
                   "mods_wfsim_extra.json")

# wfsim grants → 我们 spec 字段（不可映射的记录但 mappable=false）
GRANTS_MAP = {
    "condition_overload": "dmg_per_status",   # 每层 = 每异常种类 +N%（CO 同桶）
    "multishot": "multishot",
    "crit_chance": "crit_chance",
    "crit_damage": "crit_dmg",
    "damage": "base_dmg",
    "base_damage": "base_dmg",
    "fire_rate": "fire_rate",
    "status_chance": "status_chance",
    "status_damage": "status_dmg",
    "punch_through": "punch_through",
}


def main():
    ours = json.load(open("core/data/mods_stats.json", encoding="utf-8"))
    keys = {k.lower() for k in ours["mods"]}
    names = {str(v.get("name") or "").lower() for v in ours["mods"].values()}

    out = {"_meta": {
        "source": "github.com/magenie33/wfsim data/mods（家族互斥 + 条件堆叠）",
        "updated": "2026-09-16",
        "grants_map": GRANTS_MAP,
    }}
    n_fam = n_cond = 0
    for f in glob.glob(os.path.join(WFSIM, "**", "*.yaml"), recursive=True):
        with open(f, encoding="utf-8") as fh:
            y = yaml.safe_load(fh)
        if not isinstance(y, dict) or not y.get("name"):
            continue
        nm = str(y["name"]).lower()
        our_key = nm if nm in keys else (nm if nm in names else None)
        if not our_key:
            continue
        entry: dict = {}
        if y.get("family"):
            entry["family"] = y["family"]
            n_fam += 1
        conds = []
        effs = y.get("effects")
        items = effs if isinstance(effs, list) else ([] if effs is None else [effs])
        for e in items:
            if not isinstance(e, dict):
                continue
            if e.get("kind") not in ("buff", "stacking_buff"):
                continue
            grants = str(e.get("grants") or "")
            field = GRANTS_MAP.get(grants)
            cond = {
                "trigger": e.get("trigger") or "on_kill",
                "grants": grants,
                "field": field,
                "mappable": bool(field),
                "value": round(float(e.get("rankMax") or 0.0) * 100.0, 2),
                "max_stacks": int(e.get("max_stacks") or 1),
                "duration": e.get("duration"),
            }
            conds.append(cond)
        if conds:
            entry["conditionals"] = conds
            n_cond += 1
        if entry:
            out[our_key] = entry

    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1)
    print(f"✓ {OUT}: 家族 {n_fam} 张、条件堆叠 {n_cond} 张")


if __name__ == "__main__":
    main()
