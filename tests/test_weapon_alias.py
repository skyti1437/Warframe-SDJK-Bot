# -*- coding: utf-8 -*-
"""武器名变体别名解析的回归守卫（python3 tests/test_weapon_alias.py）。

背景（2026-09-23 用户真群报障 + 10 号交接 §五）：
1. ``倾向 绝路p`` 返回「未找到」——紫卡倾向的匹配只做原始子串 + 英文 slug 模糊，
   变体名（绝路 Prime / 赤毒 沙皇）带空格、用户输入无空格/缩写必然失配；
2. 维护者要求全变体覆盖（赤毒/信条/终幕/亡魂/…），且「紫卡倾向按变体分别计算」——
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
data_file = ROOT / "core" / "data" / "dispositions_rivenmirror.json"
check("倾向补全数据文件存在", data_file.exists())
if data_file.exists():
    entries = json.loads(data_file.read_text(encoding="utf-8")).get("entries") or {}
    rp = entries.get("Rubico Prime") or {}
    check("★ Rubico Prime=0.70（维护者指正：变体倾向 ≠ 本体 0.95）",
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

# ---------------------------------------------------------------------------
# 9. 家族展开漏列 Vandal/Wraith（2026-09-27 玩家报障：紫卡倾向 布莱顿）
# ---------------------------------------------------------------------------
# 报障：「紫卡倾向 布莱顿」少列「布莱顿·破坏者 1.30」。
# 实测根因**不是** group/riven_type 为空（family_of 根本不读这两个字段），而是
# strip_variant_norm 只剥**前缀**变体词，而中文变体在 WM 数据里是**后缀**写法
# （布莱顿·破坏者 / 蛇发女妖·亡魂）⇒ 这两条永远归不了族，全表 Vandal(11) +
# Wraith(11) 系统性漏列。用真实数据文件回归。
_disp = json.loads((ROOT / "core" / "data" / "dispositions_rivenmirror.json")
                   .read_text(encoding="utf-8"))["entries"]
D = [dict(v, en=k) for k, v in _disp.items()]

check("strip_variant_norm：中文/英文**后缀**变体也要剥",
      matching.strip_variant_norm("布莱顿破坏者") == "布莱顿"
      and matching.strip_variant_norm("蛇发女妖亡魂") == "蛇发女妖"
      and matching.strip_variant_norm("bratonvandal") == "braton")
check("反向守卫：纯变体词仍削成空串（search.py 靠它削掉输入里的纯变体词）",
      matching.strip_variant_norm("亡魂") == ""
      and matching.strip_variant_norm("破坏者") == "")
check("反向守卫：本体名与既有前缀写法原样（未改动旧行为）",
      matching.strip_variant_norm("布莱顿") == "布莱顿"
      and matching.strip_variant_norm("braton") == "braton"
      and matching.strip_variant_norm("mk1braton") == "braton"
      and matching.strip_variant_norm("棱晶欧玛") == "欧玛")

_braton = next(w for w in D if w["url_name"] == "braton")
_fam = matching.family_of(_braton, D)
_names = [f.get("zh") or f.get("en") for f in _fam]
check("★ 回归口径：布莱顿家族 = 4 个成员（含布莱顿·破坏者）",
      len(_fam) == 4 and "布莱顿·破坏者" in _names, str(_names))
check("★ 布莱顿·破坏者 = 倾向 1.30",
      any(f["url_name"] == "braton_vandal" and f["disposition"] == 1.3
          for f in _fam), str(_names))
check("同 url_name 的重复行只列一次（不再并排 MK1-Braton / MK1-布莱顿）",
      len({f["url_name"] for f in _fam}) == len(_fam), str(_names))
check("重复行里保留**有中文名**的那条",
      any((f.get("zh") or "") for f in _fam if f["url_name"] == "mk1-braton"),
      str(_names))

# 全表不变量：每个 *_vandal / *_wraith 变体都必须能归到自家本体家族
_by_url = {w["url_name"]: w for w in D}
_bad, _n = [], 0
for _u, _e in _disp.items():
    _url = _e.get("url_name") or ""
    for _suf in ("_vandal", "_wraith"):
        if not _url.endswith(_suf):
            continue
        _b = _by_url.get(_url[:-len(_suf)])
        if _b is None:
            continue
        _n += 1
        if _url not in {f["url_name"] for f in matching.family_of(_b, D)}:
            _bad.append(_url)
check(f"全表：{_n} 个 Vandal/Wraith 变体全部归到本体家族（修前全部漏列）",
      _n >= 20 and not _bad, f"n={_n} 漏 {_bad[:8]}")

# ---------------------------------------------------------------------------
# ★ 2026-10-03：中文变体前缀与**游戏内官方简中**对齐（用户《武器中文以及其变体》
#   清单 + 本地三表逐条反查：保障·勒克塔 / 枢议·咖玛腕甲枪 / 终极·螺钉步枪 /
#   勇气·海克 / 天仓·悲痛之刃 / 苦痛·德特昂 / 嫣红·潘塔）。
# ---------------------------------------------------------------------------
OFFICIAL_PREFIX = [("保障", "secura"), ("枢议", "synoid"), ("终极", "telos"),
                   ("勇气", "vaykor"), ("天仓", "ceti"), ("苦痛", "mara"),
                   ("嫣红", "carmine"), ("圣洁", "sancti")]
check("★ 前缀对齐：官方写法（保障/枢议/终极/勇气/天仓/苦痛/嫣红/圣洁）全部收录",
      all(matching.VARIANT_TOKENS.get(z) == e for z, e in OFFICIAL_PREFIX),
      str([(z, matching.VARIANT_TOKENS.get(z)) for z, _ in OFFICIAL_PREFIX]))
check("★ 修正：绯红 = Rakta（绯红·西诺斯 = Rakta Cernos）；血光 仍同义",
      matching.VARIANT_TOKENS.get("绯红") == "rakta"
      and matching.VARIANT_TOKENS.get("血光") == "rakta",
      str(matching.VARIANT_TOKENS.get("绯红")))
check("旧译保留为别名（安全/共生/终焉/瓦伊科/玛拉 仍可剥）",
      all(matching.VARIANT_TOKENS.get(z) == e for z, e in
          (("安全", "secura"), ("共生", "synoid"), ("终焉", "telos"),
           ("瓦伊科", "vaykor"), ("玛拉", "mara"))))
check("剥词实测：官方前缀都能剥到基名",
      all(matching.strip_variant_norm(matching.normalize(zh))
          == matching.normalize(base) for zh, base in
          (("保障勒克塔", "勒克塔"), ("枢议咖玛腕甲枪", "咖玛腕甲枪"),
           ("终极螺钉步枪", "螺钉步枪"), ("勇气海克", "海克"),
           ("天仓悲痛之刃", "悲痛之刃"), ("苦痛德特昂", "德特昂"),
           ("嫣红潘塔", "潘塔"), ("绯红西诺斯", "西诺斯"))))
_forms = set(matching.expand_variants("保障勒克塔"))
check("展开实测：「保障勒克塔」展开出 secura 形态（英中互换）",
      any(f.startswith("secura") or f.endswith("secura") for f in _forms),
      str(sorted(_forms)[:6]))
check("变体意图（中英通吃）：Ceti Lacera / Kuva Hek 都算变体、Lacera 不算",
      matching.variant_intent_any("Ceti Lacera") is True
      and matching.variant_intent_any("Kuva Hek") is True
      and matching.variant_intent_any("Lacera") is False)

if FAILED:
    print(f"\nFAILED {len(FAILED)}: {FAILED}")
    sys.exit(1)
print("\nALL PASS")
