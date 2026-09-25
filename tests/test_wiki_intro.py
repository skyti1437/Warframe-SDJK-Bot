# -*- coding: utf-8 -*-
"""wiki 国际服名解析 + 简介卡片的回归断言：python3 tests/test_wiki_intro.py

背景（2026-09-24 用户实测）：
- 「wiki 摸尸」给出「御魂主宰」（国服旧译），灰机 wiki 无此页 —— DE 官方简中
  **不翻译战甲名**（Nekros / Banshee 直接用英文），页面名必须按国际服口径；
- issue #1 需求②：wiki 要有可点击链接，另配一张简短介绍卡（战甲/武器/MOD/资源…）。

简介数据（core/data/wiki_intro.json）v1.0.7 起**随包分发**（此前不进包），本测试在
「有数据 / 无数据」两种环境下都必须过：无数据时只断言优雅回落。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import matching as M  # noqa: E402
from core import search as S  # noqa: E402
from core import wiki_intro as WI  # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = ""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}"
          + (f"  -> {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


def resolve(query: str) -> tuple[str, list[str]]:
    """复刻 _h_wiki 的解析链（want_variant → 检索（剥 p 回退）→ 页面名 + 变体）。"""
    want_variant = M.variant_intent_any(query)
    found = S.search(query, limit=3)
    if not found and want_variant:
        stripped = M.strip_variant_query(query)
        if stripped and stripped != query:
            found = S.search(stripped, limit=3)
    if not found:
        return "", []
    page = S.wiki_page_name(found[0], base=not want_variant)
    return page, ([] if want_variant else WI.variants(page))


# ------------------------------------------------ 变体意图识别（中英通吃）
for q, want in [("摸尸p", True), ("音妈p", True), ("绝路prime", True),
                ("Nekros Prime", True), ("Kuva Bramma", True),
                ("摸尸", False), ("托里德", False), ("布莱顿", False)]:
    check(f"变体意图 {q} → {want}", M.variant_intent_any(q) is want)
check("剥 p 后缀：摸尸p → 摸尸", M.strip_variant_query("摸尸p") == "摸尸",
      M.strip_variant_query("摸尸p"))
check("剥 prime：绝路prime → 绝路", M.strip_variant_query("绝路prime") == "绝路")

# ------------------------------------------------ 页面名：国际服口径
# 没指明变体 → 基体（DE 简中不翻译战甲名，直接用英文）
for slang, want in [("摸尸", "Nekros"), ("音妈", "Banshee"),
                    ("毒妈", "Saryn"), ("DJ", "Octavia"),
                    ("茶妹", "Protea"), ("玻璃甲", "Gara"),
                    ("咖喱棒", "Excalibur"), ("悟空", "Wukong")]:
    got, _ = resolve(slang)
    check(f"★ 黑话「{slang}」（无变体意图）→ 基体 {want}", got == want, got)

# 指明变体（p / prime）→ 直接给该变体
for q, want in [("摸尸p", "Nekros Prime"), ("音妈p", "Banshee Prime"),
                ("Nekros Prime", "Nekros Prime"), ("布莱顿p", "布莱顿 Prime")]:
    got, _ = resolve(q)
    check(f"★「{q}」→ 变体 {want}", got == want, got)

# 已翻译内容取官方简中（武器 / MOD / 赋能）
for slang, want in [("托里德", "托里德"), ("瞬时狡诈", "弹指瞬技"),
                    ("真空吸物", "吸取"), ("持久力Prime", "持久力 Prime"),
                    ("充沛", "赋能·充沛"), ("膛线", "膛线")]:
    h = S.search(slang, limit=1)
    got = S.wiki_page_name(h[0]) if h else ""
    check(f"★「{slang}」→ 官方简中 {want}", got == want, got)

# slug 直转 / 基体还原（WM 兜底路径用）
for slug, want in [("nekros_prime_set", "Nekros Prime"), ("torid", "托里德"),
                   ("banshee_prime_set", "Banshee Prime"),
                   ("primed_continuity", "持久力 Prime"),
                   ("braton_prime_blueprint", "布莱顿 Prime")]:
    got = S.page_name_from_slug(slug)
    check(f"slug {slug} → {want}", got == want, got)
for slug, want in [("nekros_prime_set", "nekros"), ("mk1_braton", "braton"),
                   ("kuva_bramma", "bramma"), ("arcane_energize", "arcane_energize"),
                   ("braton_prime_blueprint", "braton")]:
    got = S.base_slug(slug)
    check(f"base_slug {slug} → {want}", got == want, got)

# WM 展示名（带「一套」后缀）剥掉后归一到国际服名
check("WM 展示名「Banshee Prime 一套」→ Banshee Prime",
      S.wiki_page_name({"name": "Banshee Prime 一套"}) == "Banshee Prime",
      S.wiki_page_name({"name": "Banshee Prime 一套"}))
check("官方条目页面名保持原样（Forma）",
      S.wiki_page_name({"name": "Forma", "source": "官方"}) == "Forma")
check("未知词回落不返回空",
      bool(S.wiki_page_name({"name": "某不存在词", "en": "zzz nonexistent",
                             "source": "别名"})))
# 灰机标题归一（2026-09-25 浏览器 API 批量实测：含中文去空格/中点，纯拉丁保留）
for _src, _want in [("玻之武杖 Prime", "玻之武杖Prime"),
                    ("侍刃 Prime", "侍刃Prime"),
                    ("猎人 战备", "猎人战备"),
                    ("赤毒·布拉玛", "赤毒布拉玛"),
                    ("Mesa 的华尔兹", "Mesa的华尔兹"),
                    ("Nekros Prime", "Nekros Prime"),
                    ("Excalibur Umbra", "Excalibur Umbra"),
                    ("MK1-布莱顿", "MK1-布莱顿")]:
    _got = S.wiki_title(_src)
    check(f"★ 灰机标题归一「{_src}」→「{_want}」", _got == _want, _got)

# ------------------------------------------------ 简介卡片
# 战甲 / 武器 / MOD / 同伴 都有条目；数据不在（开源包）时优雅回落
if WI.available():
    for probe, want_title in [("托里德", "托里德（Torid）"),
                              ("Nekros", "Nekros"),
                              ("膛线", "膛线（Serration）"),
                              ("搬运者", "搬运者（Carrier）")]:
        card = WI.intro(probe)
        check(f"卡片「{probe}」→ {want_title}",
              bool(card) and card[0] == want_title,
              str(card[0] if card else None))
    card = WI.intro("托里德")
    text = "\n".join(card[1]) if card else ""
    check("武器卡片含段位与基础属性",
          "精通等级需求" in text and "总伤害" in text, text[:80])
    card = WI.intro("Nekros")
    text = "\n".join(card[1]) if card else ""
    check("战甲卡片含属性与技能",
          "属性：" in text and "技能：" in text, text[:80])
    check("卡片行数受控（≤8 行）",
          all(len(WI.intro(p)[1]) <= 8 for p in ("托里德", "Nekros", "膛线")))
    check("未收录条目回落 None（Forma 不在知识库）", WI.intro("Forma") is None)
    # 变体清单：不列自身，列同前缀条目
    check("★ 变体清单 摸尸 → 含 Nekros Prime",
          "Nekros Prime" in WI.variants("Nekros"), str(WI.variants("Nekros")))
    check("变体清单不列条目自身（托里德 → 空）", WI.variants("托里德") == [],
          str(WI.variants("托里德")))
    check("变体清单 搬运者 → Carrier Prime",
          any("Carrier Prime" in v for v in WI.variants("搬运者")),
          str(WI.variants("搬运者")))

    # -------------------------------------------- 占位符读顺 / 极性图标标记
    card = WI.intro("Rhino Prime")
    text = "\n".join(card[1]) if card else ""
    check("★ 占位符读顺：〈DAMAGE〉删除（能造成伤害的冲击波）",
          "能造成伤害的冲击波" in text and "〈" not in text, text[:150])
    check("★ 极性行附图标标记 ⟦pol:…⟧",
          "⟦pol:vazarin⟧" in text and "⟦pol:naramon⟧" in text, text[:150])
    check("非极性行不加标记（Excalibur Umbra 不误伤）",
          "⟦pol:" not in WI._localize("Excalibur Umbra 是暗影版战甲"))
    # 后随单位的占位符换 X（直接删会让单位悬空）
    _ash = WI.intro("Ash")
    _atext = "\n".join(_ash[1]) if _ash else ""
    check("★ 后随单位的占位符 → X（伤害增加 X%）",
          "X%" in _atext and "〈" not in _atext, _atext[:150])
    # MOD 效果行的真实数值不能被误伤
    _ser = WI.intro("膛线")
    _stext = "\n".join(_ser[1]) if _ser else ""
    check("★ MOD 效果数值完好（膛线 伤害 +40%）",
          "伤害 +40%" in _stext, _stext[:150])
    # 效果行汉化（自建翻译表；DE 导出没有这些整句的中文）
    _ww = WI.intro("创口溃烂")
    _wt = "\n".join(_ww[1]) if _ww else ""
    check("★ 英文效果整句已汉化（创口溃烂 → 每连击倍率触发几率 +40%）",
          "每连击倍率触发几率 +40%" in _wt, _wt[:150])
    _left = []
    for _m in ("创口溃烂", "适应", "膛线", "瞬时狡诈", "角斗士的壁垒",
               "猎人 战备", "菌株 热毒"):
        _c = WI.intro(_m)
        for _l in (_c[1] if _c else []):
            if _l.startswith("效果") and re.search(
                    r"(?i)\b(the|and|with|that|will|for)\b", _l):
                _left.append(_l[:40])
    check("★ 抽样 MOD 效果行无整句未翻译英文", not _left, str(_left))

    # -------------------------------------------- 兜底链：KB → 掉落表 → 配方用途
    card = WI.card_for("氩结晶")
    text = "\n".join(card[1]) if card else ""
    check("★ 材料卡（氩结晶：知识库杂项/资源条目优先）",
          bool(card) and ("获取" in text or "来源" in text), text[:120])
    card = WI.card_for("生物质")
    text = "\n".join(card[1]) if card else ""
    check("★ 材料卡（生物质）", bool(card) and bool(text), text[:120])
    # 三级兜底各自可用（不依赖知识库是否已收录）
    d = WI._drops_card("氩结晶")
    check("掉落表兜底卡可用（氩结晶 → 掉落来源）",
          bool(d) and "掉落来源" in "\n".join(d[1]), str(d)[:120])
    u = WI._uses_card("铁氧体")
    check("配方用途兜底卡可用（铁氧体 → 用于制造）",
          bool(u) and "用于制造" in "\n".join(u[1]), str(u)[:120])
    check("★ 前缀命中场景：原始查询词也作卡片候选（电路效果/电路）",
          bool(WI.card_for("电路效果", "电路")),
          str(WI.card_for("电路效果", "电路")))
    check("完全无数据的词也出兜底卡（不再回落 None）",
          bool(WI.card_for("zzz 不存在")))
    # 遗物卡（用户实测「wiki 前纪 V11」不出图）：知识库/掉落表都没有遗物条目
    _rc = WI.card_for("前纪 V11 遗物")
    _rt = "\n".join(_rc[1]) if _rc else ""
    check("★ 遗物卡（前纪 V11 → 三档奖励）",
          bool(_rc) and "常见：" in _rt and "稀有：" in _rt, _rt[:150])
    # 先锋档位（Vanguard，2026 新增）：档位中英对照必须走 parser.TIER_CN
    _vc = WI.card_for("先锋 C1 遗物")
    check("★ 先锋遗物卡（Vanguard 档位）",
          bool(_vc) and "常见：" in "\n".join(_vc[1]), str(_vc)[:150])
    _pc = WI._relic_part_card("Ash Prime 机体蓝图")
    check("★ 部件反查卡（Ash Prime 机体蓝图 → 所在遗物）",
          bool(_pc) and "所在遗物" in "\n".join(_pc[1]), str(_pc)[:150])
    # 兜底卡：任何已解析条目都出图（用户口径：无论什么内容都得绘制）
    _mc = WI.card_for("某不存在的冷门条目")
    check("★ 兜底卡（未知条目也出图，不再回落 None）",
          bool(_mc) and _mc[0] == "某不存在的冷门条目", str(_mc)[:120])
    check("兜底卡对空名字仍返回 None（不造空卡）", WI.card_for("", "  ") is None)
else:
    print("[SKIP] 简介数据不在（开源/市场包）——断言回落行为")
    check("无数据时 available()=False", WI.available() is False)
    check("无数据时 intro() 返回 None", WI.intro("托里德") is None)
    check("无数据时 variants() 返回空", WI.variants("Nekros") == [])

# ------------------------------------------------ 渲染层：标记处理
from core.render import text_card  # noqa: E402
check("text_card 剥离极性标记（纯文本模式不漏 ⟦pol:…⟧）",
      "⟦pol:" not in text_card("t", ["自带极性：⟦pol:vazarin⟧Vazarin（防御）"]))
try:
    import tempfile  # noqa: E402
    from core.render import ImageRenderer  # noqa: E402
    _r = ImageRenderer(tempfile.mkdtemp())
    if _r.available:                     # 开源包不带字体 → 跳过
        _p = _r.render("冒烟", ["自带极性：⟦pol:vazarin⟧Vazarin（防御）×2",
                                "普通行：无标记"])
        check("渲染器可画含极性标记的行", bool(_p), str(_p))
    else:
        print("[SKIP] 字体不在（开源包）——跳过渲染冒烟")
except Exception as _e:                  # noqa: BLE001
    check("渲染器可画含极性标记的行", False, repr(_e))

if FAILED:
    print(f"\n失败 {len(FAILED)} 项：{FAILED}")
    sys.exit(1)
print("\n全部通过 ✔")
