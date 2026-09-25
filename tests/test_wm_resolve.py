# -*- coding: utf-8 -*-
"""wm 物品名解析的回归守卫（python3 tests/test_wm_resolve.py）

背景（2026-09-20 用户实测报障）：
1. `wm 压迫点 p` 返回「膛线」—— 官方简中「压迫点」是 Pressure Point，
   而词典把它错配到了 serration（膛线）。根因是别名词典（含双向包含的模糊匹配）
   排在官方名之前，官方名被黑话/错映射劫持。
2. `wm 毁灭Gp` / `wm 毁灭Gprime` 返回**非 Prime** 的「毁灭 Grineer」，订单数与价格
   全不对。根因是 `norm_wm_name()` 用 `.replace("prime","")` 做**子串**替换：
   「Primed」被切成「d」、用户输入的「Gprime」被切成「G」，Prime 语义凭空丢失；
   而归一化抹掉 Prime 后普通版与 Prime 版塌成同一字符串，谁在前谁赢。

这里把三条钉死：官方名优先、Prime 语义不丢、关键映射正确。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.api_client import (  # noqa: E402
    match_official_name, match_wm_normalized, norm_wm_name, load_aliases,
)

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = ""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f"  -> {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


# ---------------------------------------------------------------------------
# 1. 归一化：prime 只能作为「独立词/后缀」被抹，不能切进单词里
# ---------------------------------------------------------------------------
check("★ Primed 不再被切成 d（子串替换的 bug）",
      norm_wm_name("Primed Smite Grineer") == "primedsmitegrineer",
      norm_wm_name("Primed Smite Grineer"))
check("末尾/独立的 Prime 仍然被抹（保留「省略 Prime 也能查」的能力）",
      norm_wm_name("Saryn Prime 蓝图") == "saryn蓝图",
      norm_wm_name("Saryn Prime 蓝图"))
check("后接中文的 Prime 也被抹",
      norm_wm_name("毁灭Gprime") == "毁灭g", norm_wm_name("毁灭Gprime"))
check("去空白 + 小写", norm_wm_name("Smite  Grineer") == "smitegrineer")
check("普通词里的 prime 子串不被误伤（supreme）",
      norm_wm_name("Supreme") == "supreme", norm_wm_name("Supreme"))

# ---------------------------------------------------------------------------
# 2. 官方名精确（casefold）：物品表的官方简中是唯一权威
# ---------------------------------------------------------------------------
FAKE = [
    {"url_name": "pressure_point", "zh": "压迫点", "en": "Pressure Point", "tags": ["mod"]},
    {"url_name": "primed_pressure_point", "zh": "压迫点 Prime", "en": "Primed Pressure Point", "tags": ["mod"]},
    {"url_name": "serration", "zh": "膛线", "en": "Serration", "tags": ["mod"]},
]
check("官方简中精确命中", (match_official_name("压迫点", FAKE) or {}).get("url_name") == "pressure_point")
check("★ 官方简中优先于任何黑话/错映射（压迫点 ≠ 膛线）",
      (match_official_name("压迫点", FAKE) or {}).get("url_name") != "serration")
check("大小写不敏感（压迫点 prime 也能命中文献名）",
      (match_official_name("压迫点 prime", FAKE) or {}).get("url_name") == "primed_pressure_point")
check("官方英文名精确命中",
      (match_official_name("Serration", FAKE) or {}).get("url_name") == "serration")
check("slug 精确命中",
      (match_official_name("pressure_point", FAKE) or {}).get("url_name") == "pressure_point")
check("查无此物返回 None", match_official_name("不存在的东西", FAKE) is None)

# ---------------------------------------------------------------------------
# 3. Prime 语义：抹掉 Prime 后普通版与 Prime 版会重名，按输入语义选
# ---------------------------------------------------------------------------
PRIME_FAKE = [
    {"url_name": "smite_grineer", "zh": "毁灭 Grineer", "en": "Smite Grineer", "tags": ["mod"]},
    {"url_name": "primed_smite_grineer", "zh": "毁灭 Grineer Prime",
     "en": "Primed Smite Grineer", "tags": ["mod"]},
]
got = match_wm_normalized("毁灭Gprime", PRIME_FAKE)
check("★ 输入写了 prime → 命中 Prime 版",
      (got or {}).get("url_name") == "primed_smite_grineer",
      (got or {}).get("url_name"))
got = match_wm_normalized("毁灭G", PRIME_FAKE)
check("★ 输入没写 prime → 命中普通版",
      (got or {}).get("url_name") == "smite_grineer", (got or {}).get("url_name"))
got = match_wm_normalized("毁灭 Grineer Prime", PRIME_FAKE)
check("全名带 Prime → 命中 Prime 版",
      (got or {}).get("url_name") == "primed_smite_grineer", (got or {}).get("url_name"))

# ---------------------------------------------------------------------------
# 4. 别名词典的关键映射（官方名 -> 正确 slug）
# ---------------------------------------------------------------------------
WM = load_aliases().get("wm_items", {})
EXPECT = {
    "压迫点": "pressure_point",            # ★ 用户报：以前错成 serration（膛线）
    "压迫点Prime": "primed_pressure_point",
    "膛线": "serration",
    "空尖弹": "hollow_point",
    "精算蓄能": "calculated_redirection",
    "腐坏打击": "spoiled_strike",
    "金属纤维": "metal_fiber",
    "锯齿弹链": "sawtooth_clip",
    "鹰眼": "eagle_eye",
    "加速充能": "accelerated_deflection",
    "守望者": "vectis_prime_set",
    "圣装守望者": "vectis_prime_set",
    "绝路": "rubico_prime_set",
    "圣装绝路": "rubico_prime_set",
    "布莱顿": "braton_prime_set",
    "拉特昂": "latron_prime_set",
    "拉特昂w": "latron_wraith_set",
    "动物本能p": "primed_animal_instinct",
    "诺娃": "nova_prime_set",              # Nova 黑话（加速娃/诺娃），不与官方名「加速」冲突
    "加速娃": "nova_prime_set",
    # 2026-09-24 用户报「wm 音妈 出来的是 Octavia（DJ）」：音妈在社区指 Banshee
    # （B 站实测「Warframe 音妈BANSHEE加强」「Banshee音妈 技能翻新」），
    # Octavia 的黑话是 DJ/音乐甲。联网核对一并钉住的其余黑话：
    "音妈": "banshee_prime_set",
    "女妖": "banshee_prime_set",
    "毒妈": "saryn_prime_set",
    "茶妹": "protea_prime_set",
    "小丑": "mirage_prime_set",
    # 2026-09-24 自查：生机是「赋能·生机」(Arcane Pulse) 的社区简称（WM 官方简中
    # 赋能·生机、灰机维基页面名「生机赋能」），不是生命力 Vitality（官方名=生命力，
    # 本就能官方名直查）——旧映射 生机→vitality 无出处，已改指赋能本体。
    "生机": "arcane_pulse",
    # 2026-09-24 新增：Citrine（水晶甲）是新 P 甲，WM 官方简中名是拉丁文
    # 「Citrine Prime 一套」（战甲名不译），社区黑话 水晶甲/水晶，Prime 写 水晶p。
    "水晶甲": "citrine_prime_set",
    "水晶": "citrine_prime_set",
    "水晶p": "citrine_prime_set",
}
for k, want in EXPECT.items():
    check(f"映射 {k} → {want}", WM.get(k) == want, f"实际 {WM.get(k)}")

# 「加速」是官方简中 Quickening 的名，词典不该再占用它
check("★ 词典不再占用官方名「加速」（让官方名 Quickening 生效）",
      "加速" not in WM, f"实际指向 {WM.get('加速')}")

# 2026-09-24 自查：全表不许有「带首尾空格的键」——查询入口会 strip，
# 这类键永远打不中（曾出现 " Valkyr"/" Baruuk"/" DJ" 三条死键，已删）。
_dirty = {k: v for k, v in WM.items() if k != k.strip()}
check("★ 词库无带首尾空格的死键", not _dirty, str(_dirty))

# 「沙皇」是 Zarr 的官方简中名（DE 导出：cannon weapon → 沙皇；赤毒·沙皇 = kuva zarr），
# WM 普通物品表里没有 zarr 系条目（赤毒武器走 lich 拍卖）——
# 词典不能再把它占给 Inaros（2026-09-24 删），未命中时由 _wm_suggest 提示 xh。
check("★ 词典不再把「沙皇」占给 Inaros（官方名=Zarr，玄骸走 xh）",
      "沙皇" not in WM, f"实际指向 {WM.get('沙皇')}")
check("「沙皇」仍留在 lich_items 且指向 kuva_zarr",
      load_aliases().get("lich_items", {}).get("沙皇") == "kuva_zarr",
      str(load_aliases().get("lich_items", {}).get("沙皇")))
# 「AMP」原错映射到 Octavia（AMP=增幅器，与 Octavia 无关），2026-09-24 删
check("错映射「AMP」已删（原指向 Octavia）", "AMP" not in WM, f"实际指向 {WM.get('AMP')}")

# ---------------------------------------------------------------------------
# 5. 源码级：官方名必须排在别名词典之前
# ---------------------------------------------------------------------------
src = (ROOT / "core" / "api_client.py").read_text(encoding="utf-8")
i_official = src.index("match_official_name(query, items)")
i_alias = src.index("# 别名词典**精确**键")
check("★ 官方名匹配位于别名词典之前", i_official < i_alias,
      f"{i_official} vs {i_alias}")
check("别名词典模糊已降级（_alias_fuzzy 调用在归一化之后）",
      src.index("match_wm_normalized(query, items)") < src.index("self._alias_fuzzy(query)"))
check("_aliases 访问都有 getattr 兜底（测试用 __new__ 造实例时不炸）",
      src.count("getattr(self, \"_aliases\", None)") >= 2)

print()
if FAILED:
    print(f"✗ {len(FAILED)} 项失败：" + "、".join(FAILED))
    sys.exit(1)
print("✓ wm 解析回归守卫全部通过")
