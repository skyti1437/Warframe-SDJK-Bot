# -*- coding: utf-8 -*-
"""把 wfsim data/enemies 的敌人基准数据合并进 core/data/enemies.json。

数据源：magenie33/wfsim data/enemies/*.yaml（每条带 wiki Module:Enemies/data
来源）。字段：stats.{base_level,health,shield,armor}、body_parts 里 is_head
的 multiplier、faction_damage_override（派系弱点表用的键）。
中文名从 wfsim data/i18n/zh/names.yaml 的 enemies 段取（DE 官方客户端串）。
"""
import glob
import json
import re
from pathlib import Path

try:
    import yaml
except ImportError:  # noqa: BLE001 —— 插件运行本身不需要 yaml，只有这几个
    raise SystemExit(   # 数据构建脚本要；给可操作的提示而不是裸 ImportError
        "本脚本需要 PyYAML：pip install pyyaml"
        "（插件运行时并不依赖它，仅构建数据用）")

WFSIM = Path(r"REDACTED_TMP_DIR/wfsim/data")
OUT = Path(__file__).resolve().parent.parent / "core" / "data" / "enemies.json"

# wfsim faction_damage_override → 我们的派系键（damage_faction.json 的列名）
FAC_MAP = {
    "grineer": "Grineer", "kuva_grineer": "Kuva Grineer",
    "corpus": "Corpus", "corpus_amalgam": "Corpus Amalgam",
    "infested": "Infested", "infested_deimos": "Infested Deimos",
    "orokin": "Orokin", "sentient": "Sentient", "narmer": "Narmer",
    "the_murmur": "The Murmur", "zariman": "Zariman",
    "scaldra": "Scaldra", "techrot": "Techrot", "anarchs": "Anarchs",
    "unaffiliated": "Grineer",
}


def main() -> None:
    ours = json.loads(OUT.read_text(encoding="utf-8"))
    table = ours["enemies"]
    zh_names: dict = {}
    try:
        n = yaml.safe_load((WFSIM / "i18n" / "zh" / "names.yaml")
                           .read_text(encoding="utf-8"))
        zh_names = n.get("enemies") or {}
    except Exception:  # noqa: BLE001
        pass

    added, skipped = [], []
    for f in glob.glob(str(WFSIM / "enemies" / "**" / "*.yaml"), recursive=True):
        y = yaml.safe_load(open(f, encoding="utf-8"))
        if not isinstance(y, dict) or not y.get("stats"):
            continue
        name = y.get("name")
        if not name:
            continue
        if name in table:
            skipped.append(name)
            continue
        st = y["stats"]
        head = 2.0
        for bp in y.get("body_parts") or []:
            if bp.get("is_head"):
                head = float(bp.get("multiplier") or 2.0)
        fac = (y.get("faction_damage_override") or y.get("faction") or "").lower()
        zh = zh_names.get(y.get("id")) or zh_names.get(name)
        if isinstance(zh, dict):
            zh = zh.get("name") or zh.get("zh")
        table[name] = {
            "name": name,
            "zh": zh or None,
            "faction": FAC_MAP.get(fac, "Grineer"),
            "base_level": int(st.get("base_level") or 1),
            "base_health": float(st.get("health") or 0),
            "base_shield": float(st.get("shield") or 0),
            "base_armor": float(st.get("armor") or 0),
            "head_mul": head,
            "source": "wfsim data/enemies（wiki Module:Enemies/data）",
        }
        if st.get("overguard"):
            table[name]["base_overguard"] = float(st["overguard"])
        added.append(f"{name}({zh or '—'})")

    ours.setdefault("_meta", {})["note"] = (
        "2026-09-17 从 wfsim data/enemies（wiki Module:Enemies/data 来源）"
        "补充 Acolyte/凶魂百夫长等条目，带 zh 与 head_mul")
    OUT.write_text(json.dumps(ours, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"新增 {len(added)}：{', '.join(added)}")
    print(f"已存在跳过 {len(skipped)}；总数 {len(table)}")


if __name__ == "__main__":
    main()
