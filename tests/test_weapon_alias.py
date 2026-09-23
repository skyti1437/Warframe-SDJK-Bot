# -*- coding: utf-8 -*-
"""武器名变体别名解析的回归守卫（python3 tests/test_weapon_alias.py）。

背景（2026-09-23 用户真群报障 + 10 号交接 §五）：
1. ``倾向 绝路p`` 返回「未找到」——紫卡倾向的匹配只做原始子串 + 英文 slug 模糊，
   变体名（绝路 Prime / 赤毒 沙皇）带空格、用户输入无空格/缩写必然失配；
2. 用户要求全变体覆盖（赤毒/信条/终幕/亡魂/…），且「紫卡倾向按变体分别计算」——
   **变体解析绝不能把「赤毒沙皇」落到「沙皇」、把「绝路p」落回 base**。

这里钉死：归一化、后缀缩写、语序互换、zh↔en token、五级优先级、
多命中给中文候选、反向用例不被模糊吞掉。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import matching  # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = ""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f"  -> {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


# ---------------------------------------------------------------------------
# 罐头数据：形态对齐 WM /riven/weapons（zh 来自 i18n.zh-hans，变体名带空格）
# ---------------------------------------------------------------------------
W = [
    {"zh": "绝路", "en": "Rubico", "url_name": "rubico", "disposition": 0.70},
    {"zh": "绝路 Prime", "en": "Rubico Prime", "url_name": "rubico_prime",
     "disposition": 1.15},
    {"zh": "沙皇", "en": "Bramma", "url_name": "bramma", "disposition": 1.30},
    {"zh": "赤毒 沙皇", "en": "Kuva Bramma", "url_name": "kuva_bramma",
     "disposition": 0.55},
    {"zh": "信条 弧电离子枪", "en": "Tenet Arca Plasmor",
     "url_name": "tenet_arca_plasmor", "disposition": 0.65},
    {"zh": "终幕 卡托", "en": "Coda Kato", "url_name": "coda_kato",
     "disposition": 1.05},
    {"zh": "亡魂 斯特拉迪瓦", "en": "Stradavar Wraith",
     "url_name": "stradavar_wraith", "disposition": 0.90},
    {"zh": "棱镜 狂野犀角", "en": "Prisma Obex", "url_name": "prisma_obex",
     "disposition": 1.20},
    {"zh": "电男", "en": "Volt", "url_name": "volt", "disposition": 1.00},
    {"zh": "守望者", "en": "Vectis", "url_name": "vectis", "disposition": 1.05},
    {"zh": "守望者 Prime", "en": "Vectis Prime", "url_name": "vectis_prime",
     "disposition": 1.00},
    {"zh": "欧玛", "en": "Ohma", "url_name": "ohma", "disposition": 1.25},
    {"zh": "棱晶 欧玛", "en": "Prisma Ohma", "url_name": "prisma_ohma",
     "disposition": 0.95},
]

# ---------------------------------------------------------------------------
# 1. 归一化
# ---------------------------------------------------------------------------
check("归一化：全角空格/中点/连字符/大小写",
      matching.normalize("赤毒　沙皇") == matching.normalize("赤毒·沙皇")
      == matching.normalize("赤毒-沙皇") == "赤毒沙皇")
check("归一化不抹 prime（语义必须保留）",
      "prime" in matching.normalize("绝路 Prime"))
check("归一化：英文侧", matching.normalize("Kuva  BRAMMA") == "kuvabramma")

# ---------------------------------------------------------------------------
# 2. 后缀缩写（仅 CJK 后）
# ---------------------------------------------------------------------------
check("绝路p → 绝路prime 形态",
      "绝路prime" in matching.expand_variants("绝路p"))
check("绝路P版 → 绝路prime 形态",
      "绝路prime" in matching.expand_variants("绝路P版"))
check("英文词尾 p 不当缩写（Grineer 类）",
      "grineerprime" not in matching.expand_variants("grineerp")
      and matching.expand_variants("grineerp")[0] == "grineerp")

# ---------------------------------------------------------------------------
# 3. 语序互换与 zh↔en token
# ---------------------------------------------------------------------------
check("沙皇赤毒 → 赤毒沙皇 形态（前缀语序互换）",
      "赤毒沙皇" in matching.expand_variants("沙皇赤毒"))
check("kuva沙皇 → 赤毒沙皇 形态（en→zh token）",
      "赤毒沙皇" in matching.expand_variants("kuva沙皇"))
check("赤毒沙皇 展开含 沙皇kuva（zh→en token）",
      "沙皇kuva" in matching.expand_variants("赤毒沙皇"))
check("★ 前缀变体绝不剥 token（防倾向误配 base）",
      "沙皇" not in matching.expand_variants("赤毒沙皇"))
forms = matching.expand_variants("绝路p")
check("★ 2026-09-23 撤销回落：展开绝不含「去掉 prime 的 base 形态」",
      bool(forms) and all("prime" in f for f in forms[1:]), str(forms))

# ---------------------------------------------------------------------------
# 4. resolve_weapon_name 五级优先
# ---------------------------------------------------------------------------
hits, stage = matching.resolve_weapon_name("绝路", W)
check("exact 层：绝路 → zh 完全（精确名单查询，不混列变体）",
      stage == "exact" and hits[0]["url_name"] == "rubico")

hits, stage = matching.resolve_weapon_name("绝路p", W)
check("★ variant 层：绝路p → 绝路 Prime（表里有 Prime 时 prime 优先，绝不落 base）",
      stage == "variant" and len(hits) == 1
      and hits[0]["url_name"] == "rubico_prime")

hits, stage = matching.resolve_weapon_name("绝路 Prime", W)
check("★ 明写「绝路 Prime」→ Prime 本体（罐头里与 zh 全等，exact 直命中）",
      stage == "exact" and hits[0]["url_name"] == "rubico_prime")

W_NO_PRIME = [w for w in W if w["url_name"] != "rubico_prime"]
hits, stage = matching.resolve_weapon_name("绝路p", W_NO_PRIME)
check("★ 表里没有 Prime 条目时：未找到（2026-09-23 撤销回落 base——"
      "变体倾向与本体不同，绝不冒充本体值）",
      not hits and stage == "")

hits, stage = matching.resolve_weapon_name("赤毒沙皇", W)
check("★ normalized 层：赤毒沙皇 → 赤毒 沙皇（不是 base 沙皇）",
      stage == "normalized" and len(hits) == 1
      and hits[0]["url_name"] == "kuva_bramma")

hits, stage = matching.resolve_weapon_name("沙皇赤毒", W)
check("variant 层：沙皇赤毒（后缀语序）→ 赤毒 沙皇",
      stage == "variant" and hits[0]["url_name"] == "kuva_bramma")

hits, stage = matching.resolve_weapon_name("kuva沙皇", W)
check("variant 层：kuva沙皇 → 赤毒 沙皇", 
      stage == "variant" and hits[0]["url_name"] == "kuva_bramma")

hits, stage = matching.resolve_weapon_name("Kuva Bramma", W)
check("normalized 层：英文全名（含空格）直接等价",
      stage == "normalized" and hits[0]["url_name"] == "kuva_bramma")

hits, stage = matching.resolve_weapon_name("信条弧电离子枪", W)
check("normalized 层：信条弧电离子枪 → 信条 弧电离子枪",
      stage == "normalized" and hits[0]["url_name"] == "tenet_arca_plasmor")

hits, stage = matching.resolve_weapon_name("终幕卡托", W)
check("normalized 层：终幕卡托 → 终幕 卡托",
      stage == "normalized" and hits[0]["url_name"] == "coda_kato")

hits, stage = matching.resolve_weapon_name("亡魂斯特拉迪瓦", W)
check("normalized 层：亡魂斯特拉迪瓦 → 亡魂 斯特拉迪瓦",
      stage == "normalized" and hits[0]["url_name"] == "stradavar_wraith")

hits, stage = matching.resolve_weapon_name("棱镜狂野犀角", W)
check("normalized 层：棱镜狂野犀角 → 棱镜 狂野犀角",
      stage == "normalized" and hits[0]["url_name"] == "prisma_obex")

hits, stage = matching.resolve_weapon_name("沙皇", W)
check("★ 精确名单查询：沙皇 → exact 精确命中 base 本体（不混列变体——"
      "倾向按变体分别计算，查变体须显式写变体名）",
      stage == "exact" and len(hits) == 1 and hits[0]["url_name"] == "bramma")

W_MULTI = W + [{"zh": "测试枪", "en": "Test Gun", "url_name": "test_gun"},
               {"zh": "赤毒 测试枪", "en": "Kuva Test Gun",
                "url_name": "kuva_test_gun"}]
hits, stage = matching.resolve_weapon_name("试枪", W_MULTI)
check("substring 层：非精确片段 → 多条全列",
      stage == "substring"
      and {h["url_name"] for h in hits} == {"test_gun", "kuva_test_gun"})

check("多命中候选给中文名",
      matching.zh_names(matching.resolve_weapon_name("试枪", W_MULTI)[0]) ==
      ["测试枪", "赤毒 测试枪"])

# ---------------------------------------------------------------------------
# 5. 反向用例：不存在的名字不得被模糊吞掉
# ---------------------------------------------------------------------------
hits, stage = matching.resolve_weapon_name("守望p", W)
check("★ 截短缩写：守望p → 守望者 Prime（prime_prefix 层，2026-09-23 群实测）",
      stage == "prime_prefix" and len(hits) == 1
      and hits[0]["url_name"] == "vectis_prime")

hits, stage = matching.resolve_weapon_name("守望者p", W)
check("完整名+缩写：守望者p → 守望者 Prime（variant 层）",
      stage == "variant" and hits[0]["url_name"] == "vectis_prime")

hits, stage = matching.resolve_weapon_name("不存在xyz", W)
check("不存在的名字：五级全空", not hits and stage == "")
hits, stage = matching.resolve_weapon_name("赤毒不存在的枪", W)
check("★ 变体前缀+不存在的武器：禁用模糊层，宁「未找到」不吞成 base 卡",
      not hits and stage == "")
check("该场景 suggest_zh 仍给中文候选（只建议、不命中）",
      isinstance(matching.suggest_zh("赤毒不存在的枪", W), list))
hits, stage = matching.resolve_weapon_name("绝路普赖姆", W)
suggest = matching.suggest_zh("绝路普赖姆", W)
check("长错词不硬拗（宁空勿错）", not hits)
check("suggest_zh 只作候选不作命中，返回中文名列表",
      isinstance(suggest, list)
      and all(isinstance(s, str) and s for s in suggest))

# ---------------------------------------------------------------------------
# 6. prime_sibling（resolve_riven_weapon 的 p 分支用）
# ---------------------------------------------------------------------------
base = next(w for w in W if w["url_name"] == "rubico")
check("prime_sibling：绝路 → 绝路 Prime",
      (matching.prime_sibling(base, W) or {}).get("url_name") == "rubico_prime")
kuva = next(w for w in W if w["url_name"] == "kuva_bramma")
check("prime_sibling：无 Prime 的武器返回 None（调用方按未找到处理，不回落 base）",
      matching.prime_sibling(kuva, W) is None)

# ---------------------------------------------------------------------------
# 8. 倾向补全数据文件（scripts/build_disposition.py 产物）
# ---------------------------------------------------------------------------
import json  # noqa: E402

# ---------------------------------------------------------------------------
# 7.5 家族展开与类型翻译（2026-09-23 用户需求：只报本体名列出全部变体）
# ---------------------------------------------------------------------------
check("strip_variant_norm：棱晶欧玛→欧玛 / 绝路prime→绝路 / 赤毒沙皇→沙皇",
      matching.strip_variant_norm("棱晶欧玛") == "欧玛"
      and matching.strip_variant_norm("棱镜欧玛") == "欧玛"
      and matching.strip_variant_norm("绝路prime") == "绝路"
      and matching.strip_variant_norm("赤毒沙皇") == "沙皇")
ohma = next(w for w in W if w["url_name"] == "ohma")
fam = matching.family_of(ohma, W)
check("family_of：本体查询 → [欧玛, 棱晶 欧玛]（本体在前，官方名棱晶）",
      [f["url_name"] for f in fam] == ["ohma", "prisma_ohma"])
check("官方名棱晶 直查命中（DE 简中为准）",
      matching.resolve_weapon_name("棱晶欧玛", W)[1] == "normalized"
      and matching.resolve_weapon_name("棱晶 欧玛", W)[0][0]["url_name"]
      == "prisma_ohma")
rub = next(w for w in W if w["url_name"] == "rubico")
fam = matching.family_of(rub, W)
check("family_of：绝路家族 = [绝路, 绝路 Prime]",
      [f["url_name"] for f in fam] == ["rubico", "rubico_prime"])
check("variant_intent：绝路p=True / 绝路=False",
      matching.variant_intent("绝路p") and not matching.variant_intent("绝路"))

from core import formatters as fmt  # noqa: E402

check("riven_type_cn：rifle→步枪 / shotgun→霰弹枪 / melee→近战",
      fmt.riven_type_cn("rifle") == "步枪"
      and fmt.riven_type_cn("shotgun") == "霰弹枪"
      and fmt.riven_type_cn("melee") == "近战")
check("riven_type_cn：未知值原样、空值返回空",
      fmt.riven_type_cn("mystery") == "mystery" and fmt.riven_type_cn("") == "")

# ---------------------------------------------------------------------------
# 8. 倾向补全数据文件（scripts/build_disposition.py 产物）
# ---------------------------------------------------------------------------
import json  # noqa: E402
data_file = ROOT / "core" / "data" / "dispositions_rivenmirror.json"
check("倾向补全数据文件存在", data_file.exists())
if data_file.exists():
    entries = json.loads(data_file.read_text(encoding="utf-8")).get("entries") or {}
    rp = entries.get("Rubico Prime") or {}
    check("★ Rubico Prime=0.70（用户指正：变体倾向 ≠ 本体 0.95）",
          rp.get("disposition") == 0.7, str(rp))
    check("★ 补全条目带 zh 与 url_name（绝路 Prime / rubico_prime）",
          rp.get("zh") == "绝路 Prime" and rp.get("url_name") == "rubico_prime",
          str(rp))
    kz = entries.get("Kuva Zarr") or {}
    check("Kuva Zarr=0.7（wiki 新值，WM/极镜 0.6 已过时）",
          kz.get("disposition") == 0.7, str(kz))
    vp = entries.get("Vectis Prime") or {}
    check("★ Vectis Prime=1.0（守望者 Prime，wiki 新值；旧值 0.9 即用户截图的错值）",
          vp.get("disposition") == 1.0 and vp.get("zh") == "守望者 Prime",
          str(vp))
    tap = entries.get("Tenet Arca Plasmor") or {}
    check("Tenet Arca Plasmor riven_type=shotgun（基类继承）",
          tap.get("riven_type") == "shotgun", str(tap))

# ---------------------------------------------------------------------------
# 7. 与旧实现的优先级兼容（官方名优先）
# ---------------------------------------------------------------------------
hits, stage = matching.resolve_weapon_name("Rubico Prime", W)
check("英文精确（归一化）命中 Prime 本体", 
      stage == "normalized" and hits[0]["url_name"] == "rubico_prime")

if FAILED:
    print(f"\nFAILED {len(FAILED)}: {FAILED}")
    sys.exit(1)
print("\nALL PASS")
