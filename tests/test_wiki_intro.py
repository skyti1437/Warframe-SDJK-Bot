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
                    ("MK1-布莱顿", "MK1-布莱顿"),
                    # 赋能两族规则（API 实测 16/16 存在）：战甲赋能交换、武器赋能保序去点
                    ("赋能·精确", "精确赋能"), ("赋能·充沛", "充沛赋能"),
                    ("近战·侵染", "近战侵染"), ("神威·勇武", "神威勇武"),
                    ("瀑流·耀炎", "瀑流耀炎"), ("双枪·滑射", "双枪滑射")]:
    _got = S.wiki_title(_src)
    check(f"★ 灰机标题归一「{_src}」→「{_want}」", _got == _want, _got)
# 赋能三种写法都要能在本地索引命中同一条（用户实测「精确赋能」未收录）
for _q in ("精确赋能", "赋能精确", "赋能·精确", "近战侵染", "神威勇武"):
    _h = S.search(_q, limit=1)
    check(f"★ 赋能写法兼容：{_q} 命中",
          bool(_h) and "·" in (_h[0].get("name") or ""), str(_h[:1]))

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
    # 知识库「半中半英」残留行的人工覆盖（2026-09-25，赋能卡实测可见）
    _lg = WI.intro("毒气铅弹")
    _lgt = "\n".join(_lg[1]) if _lg else ""
    check("★ 半中半英行已覆盖（毒气铅弹 → 毒气伤害与触发几率）",
          "毒气伤害与触发几率" in _lgt and " and " not in _lgt, _lgt[:120])
    _ap = WI.intro("赋能·精确")
    _apt = "\n".join(_ap[1]) if _ap else ""
    check("★ 赋能·精确效果行无 On/on 残留",
          "次要武器伤害 +300%" in _apt and " on " not in _apt and "On " not in _apt,
          _apt[:120])

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

# ------------------------------------------------ 战甲黑话清单（wiki 段 + 澄清）
# 来源 docs/reference/战甲黑话与招募简称-20260925.md；市场类已落 wm_items，
# 这里验「外号 → 国际服英文页面」与「易混淆澄清」两层（2026-09-25 用户口径：
# 页面名必须英文 —— 中文页面名正是最近修过的那批死链来源）。
from core.api_client import WarframeClient  # noqa: E402
import json  # noqa: E402
_c = WarframeClient(flare_enabled=False)

for _k, _want in [("花P", "Wisp Prime"), ("血P", "Garuda Prime"),
                  ("电P", "Volt Prime"), ("高P", "Gauss Prime"),
                  ("猴P", "Wukong Prime"), ("骨P", "Xaku Prime"),
                  ("船P", "Sevagoth Prime"), ("僧P", "Baruuk Prime")]:
    _hit = _c.wiki_lookup(_k) or {}
    check(f"★ wiki 段 {_k} → {_want}", _hit.get("title") == _want, str(_hit))
check("★ 小写 p 后缀等价（花p → Wisp Prime）",
      (_c.wiki_lookup("花p") or {}).get("title") == "Wisp Prime")
# 无 Prime 的新战甲只落基体（灰机 API 实测 Kullervo/Dagath/Qorvex/Koumei/Cyte-09
# 的 Prime 页不存在，故不给它们造 P 键）
for _k, _want in [("老九", "Cyte-09"), ("刀哥", "Kullervo"), ("暖气片", "Qorvex"),
                  ("扣妹", "Koumei"), ("赛马娘", "Dagath"), ("书甲", "Dante"),
                  ("黑咖喱", "Excalibur Umbra"), ("鸟哥", "Dante")]:
    _hit = _c.wiki_lookup(_k) or {}
    check(f"★ wiki 段外号 {_k} → {_want}", _hit.get("title") == _want, str(_hit))

_AL_WIKI = json.loads((ROOT / "core" / "data" / "aliases.json")
                      .read_text(encoding="utf-8"))["wiki"]
_CONCEPT = {"夜灵", "夜灵平野", "三傻", "平原", "紫卡", "裂罅", "玄骸", "信条",
            "杜卡德", "奸商", "新服", "新手"}          # 概念页：中文标题已核对存在
_bad_titles = [k for k, v in _AL_WIKI.items()
               if k not in _CONCEPT and isinstance(v, dict)
               and re.search(r"[一-鿿]", v.get("title", ""))]
check("★ 战甲外号页面名全为英文（无中文页面名）", not _bad_titles,
      str(_bad_titles[:5]))
check("wiki 段条目数 ≥ 60（原 12 条概念页 + 外号）", len(_AL_WIKI) >= 60,
      str(len(_AL_WIKI)))

for _k, _must in [("花甲", "Wisp"), ("血妈", "Garuda"), ("猫", "Khora"),
                  ("奶妈", "Trinity"), ("奶爸", "Oberon"), ("电", "Volt"),
                  ("高", "Gauss"), ("跑男", "Gauss"), ("明神", "Limbo"),
                  ("老猫甲", "Valkyr")]:
    _note = _c.wiki_clarify(_k)
    check(f"★ 易混淆澄清 {_k} 提到 {_must}", _must in _note, _note)
check("澄清：鸟姐=Zephyr（并把血妈/鸟妹分清）",
      "Zephyr" in _c.wiki_clarify("鸟姐"))
check("澄清未命中返回空串", _c.wiki_clarify("zzz 不存在") == "")
# 模糊阈值 0.8：短词不得被「X+P」这类新键劫持（「高」→ 高P / 「跑男」→ 老牌跑男
# 都是错的：应落回检索链给基体，2026-09-25 实测踩到）
check("★ 模糊不劫持短词（高 / 跑男 / 花 不落 P 页或长外号）",
      all((_c.wiki_lookup(_q) or {}).get("title") is None
          for _q in ("高", "跑男", "花", "花甲")))
check("概念页错字容忍仍在（杜卡德金 → 杜卡德金币）",
      (_c.wiki_lookup("杜卡德金") or {}).get("title") == "杜卡德金币")

if FAILED:
    print(f"\n失败 {len(FAILED)} 项：{FAILED}")
    sys.exit(1)
print("\n全部通过 ✔")
