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

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import matching as M  # noqa: E402
from core import search as S  # noqa: E402
from core import wiki_intro as WI  # noqa: E402
import importlib.util as _ilu  # noqa: E402
from pathlib import Path as _P  # noqa: E402

_ROOT = _P(__file__).resolve().parents[1]
_bs = _ilu.spec_from_file_location("_bw2", str(_ROOT / "scripts" / "build_wiki_intro.py"))
_BIZ = _ilu.module_from_spec(_bs)
_bs.loader.exec_module(_BIZ)
_ENTRIES = __import__("json").loads(
    (_ROOT / "core" / "data" / "wiki_intro.json").read_text(encoding="utf-8")
)["entries"]

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = ""):
    print(
        f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -> {detail}" if detail and not cond else "")
    )
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
for q, want in [
    ("摸尸p", True),
    ("音妈p", True),
    ("绝路prime", True),
    ("Nekros Prime", True),
    ("Kuva Bramma", True),
    ("摸尸", False),
    ("托里德", False),
    ("布莱顿", False),
]:
    check(f"变体意图 {q} → {want}", M.variant_intent_any(q) is want)
check(
    "剥 p 后缀：摸尸p → 摸尸",
    M.strip_variant_query("摸尸p") == "摸尸",
    M.strip_variant_query("摸尸p"),
)
check("剥 prime：绝路prime → 绝路", M.strip_variant_query("绝路prime") == "绝路")

# ------------------------------------------------ 页面名：国际服口径
# 没指明变体 → 基体（DE 简中不翻译战甲名，直接用英文）
for slang, want in [
    ("摸尸", "Nekros"),
    ("音妈", "Banshee"),
    ("毒妈", "Saryn"),
    ("DJ", "Octavia"),
    ("茶妹", "Protea"),
    ("玻璃甲", "Gara"),
    ("咖喱棒", "Excalibur"),
    ("悟空", "Wukong"),
]:
    got, _ = resolve(slang)
    check(f"★ 黑话「{slang}」（无变体意图）→ 基体 {want}", got == want, got)

# 指明变体（p / prime）→ 直接给该变体
for q, want in [
    ("摸尸p", "Nekros Prime"),
    ("音妈p", "Banshee Prime"),
    ("Nekros Prime", "Nekros Prime"),
    ("布莱顿p", "布莱顿 Prime"),
]:
    got, _ = resolve(q)
    check(f"★「{q}」→ 变体 {want}", got == want, got)

# 已翻译内容取官方简中（武器 / MOD / 赋能）
for slang, want in [
    ("托里德", "托里德"),
    ("瞬时狡诈", "弹指瞬技"),
    ("真空吸物", "吸取"),
    ("持久力Prime", "持久力 Prime"),
    ("充沛", "赋能·充沛"),
    ("膛线", "膛线"),
]:
    h = S.search(slang, limit=1)
    got = S.wiki_page_name(h[0]) if h else ""
    check(f"★「{slang}」→ 官方简中 {want}", got == want, got)

# slug 直转 / 基体还原（WM 兜底路径用）
for slug, want in [
    ("nekros_prime_set", "Nekros Prime"),
    ("torid", "托里德"),
    ("banshee_prime_set", "Banshee Prime"),
    ("primed_continuity", "持久力 Prime"),
    ("braton_prime_blueprint", "布莱顿 Prime"),
]:
    got = S.page_name_from_slug(slug)
    check(f"slug {slug} → {want}", got == want, got)
for slug, want in [
    ("nekros_prime_set", "nekros"),
    ("mk1_braton", "braton"),
    ("kuva_bramma", "bramma"),
    ("arcane_energize", "arcane_energize"),
    ("braton_prime_blueprint", "braton"),
]:
    got = S.base_slug(slug)
    check(f"base_slug {slug} → {want}", got == want, got)

# WM 展示名（带「一套」后缀）剥掉后归一到国际服名
check(
    "WM 展示名「Banshee Prime 一套」→ Banshee Prime",
    S.wiki_page_name({"name": "Banshee Prime 一套"}) == "Banshee Prime",
    S.wiki_page_name({"name": "Banshee Prime 一套"}),
)
check(
    "官方条目页面名保持原样（Forma）",
    S.wiki_page_name({"name": "Forma", "source": "官方"}) == "Forma",
)
check(
    "未知词回落不返回空",
    bool(S.wiki_page_name({"name": "某不存在词", "en": "zzz nonexistent", "source": "别名"})),
)
# 灰机标题归一（2026-09-25 浏览器 API 批量实测：含中文去空格/中点，纯拉丁保留）
for _src, _want in [
    ("玻之武杖 Prime", "玻之武杖Prime"),
    ("侍刃 Prime", "侍刃Prime"),
    ("猎人 战备", "猎人战备"),
    ("赤毒·布拉玛", "赤毒布拉玛"),
    ("Mesa 的华尔兹", "Mesa的华尔兹"),
    ("Nekros Prime", "Nekros Prime"),
    ("Excalibur Umbra", "Excalibur Umbra"),
    ("MK1-布莱顿", "MK1-布莱顿"),
    # 赋能两族规则（API 实测 16/16 存在）：战甲赋能交换、武器赋能保序去点
    ("赋能·精确", "精确赋能"),
    ("赋能·充沛", "充沛赋能"),
    ("近战·侵染", "近战侵染"),
    ("神威·勇武", "神威勇武"),
    ("瀑流·耀炎", "瀑流耀炎"),
    ("双枪·滑射", "双枪滑射"),
]:
    _got = S.wiki_title(_src)
    check(f"★ 灰机标题归一「{_src}」→「{_want}」", _got == _want, _got)
# 赋能三种写法都要能在本地索引命中同一条（用户实测「精确赋能」未收录）
for _q in ("精确赋能", "赋能精确", "赋能·精确", "近战侵染", "神威勇武"):
    _h = S.search(_q, limit=1)
    check(f"★ 赋能写法兼容：{_q} 命中", bool(_h) and "·" in (_h[0].get("name") or ""), str(_h[:1]))

# ------------------------------------------------ 简介卡片
# 战甲 / 武器 / MOD / 同伴 都有条目；数据不在（开源包）时优雅回落
if WI.available():
    for probe, want_title in [
        ("托里德", "托里德（Torid）"),
        ("Nekros", "Nekros"),
        ("膛线", "膛线（Serration）"),
        ("搬运者", "搬运者（Carrier）"),
    ]:
        card = WI.intro(probe)
        check(
            f"卡片「{probe}」→ {want_title}",
            bool(card) and card[0] == want_title,
            str(card[0] if card else None),
        )
    card = WI.intro("托里德")
    text = "\n".join(card[1]) if card else ""
    check(
        "武器卡片按攻击段保留基础属性",
        "精通等级需求" in text
        and "Grenade Impact：该段伤害 100" in text
        and "Poison Cloud：该段伤害 40" in text
        and "总伤害" not in text,
        text[:160],
    )
    card = WI.intro("Nekros")
    text = "\n".join(card[1]) if card else ""
    check("战甲卡片含属性与技能", "属性：" in text and "技能：" in text, text[:80])
    # 来源完整后，既有获取表可能多显示一行；正文预算与来源表各自受限。
    check(
        "普通卡片正文行数受控（不计获取表）",
        all(
            len([x for x in WI.intro(p)[1] if not x.startswith(("◆ 获取", "　来源　几率", "· "))])
            <= 8
            for p in ("布莱顿", "Nekros", "膛线")
        ),
    )
    # 三个既有抽查样本的来源数来自种子，不把样本≤3外推成runtime硬上限。
    for _query in ("布莱顿", "Nekros", "膛线"):
        _seed_row = WI._prefer_full_rank(WI.candidates(_query))
        _seed_source = next((x for x in _seed_row["lines"] if x.startswith("获取：")), "")
        _expected_places = sum(
            bool(re.search(r"\d+(?:\.\d+)?%", part)) for part in re.split(r"[；;]", _seed_source)
        )
        _runtime_lines = WI.intro(_query)[1]
        _actual_places = sum(x.startswith("· ") for x in _runtime_lines)
        _table_head_rows = sum(
            x.startswith("◆ 获取（") or x == "　来源　几率" for x in _runtime_lines
        )
        check(
            "普通样本来源项与seed完整项一致：" + _query,
            _actual_places == _expected_places <= 3,
            str((_actual_places, _expected_places)),
        )
        check(
            "普通样本来源标题表头与展开预算：" + _query,
            _table_head_rows == (2 if _expected_places >= 2 else 0)
            and len(_runtime_lines) <= 8 + _expected_places + _table_head_rows,
            str((_table_head_rows, len(_runtime_lines))),
        )
    check(
        "多攻击卡片保留各段，普通信息仍受行数限制",
        sum(line.startswith("攻击·") for line in WI.intro("托里德")[1]) == 6
        and sum(not line.startswith("攻击·") for line in WI.intro("托里德")[1]) <= 8,
    )
    check("未收录条目回落 None（Forma 不在知识库）", WI.intro("Forma") is None)
    # 变体清单：不列自身，列同前缀条目
    check(
        "★ 变体清单 摸尸 → 含 Nekros Prime",
        "Nekros Prime" in WI.variants("Nekros"),
        str(WI.variants("Nekros")),
    )
    check(
        "变体清单不列条目自身（托里德 → 空）",
        WI.variants("托里德") == [],
        str(WI.variants("托里德")),
    )
    check(
        "变体清单 搬运者 → Carrier Prime",
        any("Carrier Prime" in v for v in WI.variants("搬运者")),
        str(WI.variants("搬运者")),
    )

    # -------------------------------------------- 占位符读顺 / 极性图标标记
    card = WI.intro("Rhino Prime")
    text = "\n".join(card[1]) if card else ""
    check(
        "★ 占位符读顺：〈DAMAGE〉删除（能造成伤害的冲击波）",
        "能造成伤害的冲击波" in text and "〈" not in text,
        text[:150],
    )
    check(
        "★ 极性行附图标标记 ⟦pol:…⟧",
        "⟦pol:vazarin⟧" in text and "⟦pol:naramon⟧" in text,
        text[:150],
    )
    check(
        "非极性行不加标记（Excalibur Umbra 不误伤）",
        "⟦pol:" not in WI._localize("Excalibur Umbra 是暗影版战甲"),
    )
    # 后随单位的占位符换 X（直接删会让单位悬空）
    _ash = WI.intro("Ash")
    _atext = "\n".join(_ash[1]) if _ash else ""
    check(
        "★ 后随单位的占位符 → X（伤害增加 X%）", "X%" in _atext and "〈" not in _atext, _atext[:150]
    )
    # MOD 效果行的真实数值不能被误伤
    _ser = WI.intro("膛线")
    _stext = "\n".join(_ser[1]) if _ser else ""
    check(
        "★ MOD 效果数值完好（膛线 满级 伤害 +165%；同名取满级最高，10-25）",
        "伤害 +165%" in _stext,
        _stext[:150],
    )
    # 效果行汉化（自建翻译表；DE 导出没有这些整句的中文）
    _ww = WI.intro("创口溃烂")
    _wt = "\n".join(_ww[1]) if _ww else ""
    check(
        "★ 英文效果整句已汉化（创口溃烂 → 每连击倍率触发几率 +40%）",
        "每连击倍率触发几率 +40%" in _wt,
        _wt[:150],
    )
    _left = []
    for _m in ("创口溃烂", "适应", "膛线", "瞬时狡诈", "角斗士的壁垒", "猎人 战备", "菌株 热毒"):
        _c = WI.intro(_m)
        for _l in _c[1] if _c else []:
            if _l.removeprefix("◆ ").startswith("效果") and re.search(
                r"(?i)\b(the|and|with|that|will|for)\b", _l
            ):
                _left.append(_l[:40])
    check("★ 抽样 MOD 效果行无整句未翻译英文", not _left, str(_left))
    # 知识库「半中半英」残留行的人工覆盖（2026-09-25，赋能卡实测可见）
    _lg = WI.intro("毒气铅弹")
    _lgt = "\n".join(_lg[1]) if _lg else ""
    check(
        "★ 半中半英行已覆盖（毒气铅弹 → 毒气伤害与触发几率）",
        "毒气伤害与触发几率" in _lgt and " and " not in _lgt,
        _lgt[:120],
    )
    _ap = WI.intro("赋能·精确")
    _apt = "\n".join(_ap[1]) if _ap else ""
    check(
        "★ 赋能·精确效果行无 On/on 残留",
        "次要武器伤害 +300%" in _apt and " on " not in _apt and "On " not in _apt,
        _apt[:120],
    )

    # -------------------------------------------- 兜底链：KB → 掉落表 → 配方用途
    card = WI.card_for("氩结晶")
    text = "\n".join(card[1]) if card else ""
    check(
        "★ 材料卡（氩结晶：知识库杂项/资源条目优先）",
        bool(card) and ("获取" in text or "来源" in text),
        text[:120],
    )
    card = WI.card_for("生物质")
    text = "\n".join(card[1]) if card else ""
    check("★ 材料卡（生物质）", bool(card) and bool(text), text[:120])
    # 三级兜底各自可用（不依赖知识库是否已收录）
    d = WI._drops_card("氩结晶")
    check(
        "掉落表兜底卡可用（氩结晶 → 掉落来源）",
        bool(d) and "掉落来源" in "\n".join(d[1]),
        str(d)[:120],
    )
    u = WI._uses_card("铁氧体")
    check(
        "配方用途兜底卡可用（铁氧体 → 用于制造）",
        bool(u) and "用于制造" in "\n".join(u[1]),
        str(u)[:120],
    )
    check(
        "★ 前缀命中场景：原始查询词也作卡片候选（电路效果/电路）",
        bool(WI.card_for("电路效果", "电路")),
        str(WI.card_for("电路效果", "电路")),
    )
    check("完全无数据的词也出兜底卡（不再回落 None）", bool(WI.card_for("zzz 不存在")))
    # 遗物卡（用户实测「wiki 前纪 V11」不出图）：知识库/掉落表都没有遗物条目
    _rc = WI.card_for("前纪 V11 遗物")
    _rt = "\n".join(_rc[1]) if _rc else ""
    check(
        "★ 遗物卡（前纪 V11 → 三档奖励）",
        bool(_rc)
        and "· 常见　" in _rt
        and "· 稀有　" in _rt,  # 2026-10-11：「常见：」→ 表行「· 常见　」
        _rt[:150],
    )
    # 先锋档位（Vanguard，2026 新增）：档位中英对照必须走 parser.TIER_CN
    _vc = WI.card_for("先锋 C1 遗物")
    check(
        "★ 先锋遗物卡（Vanguard 档位）",
        bool(_vc) and "· 常见　" in "\n".join(_vc[1]),
        str(_vc)[:150],
    )
    _pc = WI._relic_part_card("Ash Prime 机体蓝图")
    check(
        "★ 部件反查卡（Ash Prime 机体蓝图 → 所在遗物）",
        bool(_pc) and "所在遗物" in "\n".join(_pc[1]),
        str(_pc)[:150],
    )
    # 兜底卡：任何已解析条目都出图（用户口径：无论什么内容都得绘制）
    _mc = WI.card_for("某不存在的冷门条目")
    check(
        "★ 兜底卡（未知条目也出图，不再回落 None）",
        bool(_mc) and _mc[0] == "某不存在的冷门条目",
        str(_mc)[:120],
    )
    check("兜底卡对空名字仍返回 None（不造空卡）", WI.card_for("", "  ") is None)
else:
    print("[SKIP] 简介数据不在（开源/市场包）——断言回落行为")
    check("无数据时 available()=False", WI.available() is False)
    check("无数据时 intro() 返回 None", WI.intro("托里德") is None)
    check("无数据时 variants() 返回空", WI.variants("Nekros") == [])

# ------------------------------------------------ 渲染层：标记处理
from core.render import text_card  # noqa: E402

check(
    "text_card 剥离极性标记（纯文本模式不漏 ⟦pol:…⟧）",
    "⟦pol:" not in text_card("t", ["自带极性：⟦pol:vazarin⟧Vazarin（防御）"]),
)
try:
    import tempfile  # noqa: E402
    from core.render import ImageRenderer  # noqa: E402

    _r = ImageRenderer(tempfile.mkdtemp())
    if _r.available:  # 开源包不带字体 → 跳过
        _p = _r.render("冒烟", ["自带极性：⟦pol:vazarin⟧Vazarin（防御）×2", "普通行：无标记"])
        check("渲染器可画含极性标记的行", bool(_p), str(_p))
    else:
        print("[SKIP] 字体不在（开源包）——跳过渲染冒烟")
except Exception as _e:  # noqa: BLE001
    check("渲染器可画含极性标记的行", False, repr(_e))

# ------------------------------------------------ 战甲黑话清单（wiki 段 + 澄清）
# 来源 docs/reference/战甲黑话与招募简称-20260925.md；市场类已落 wm_items，
# 这里验「外号 → 国际服英文页面」与「易混淆澄清」两层（2026-09-25 用户口径：
# 页面名必须英文 —— 中文页面名正是最近修过的那批死链来源）。
from core.api_client import WarframeClient  # noqa: E402

_c = WarframeClient(flare_enabled=False)

for _k, _want in [
    ("花P", "Wisp Prime"),
    ("血P", "Garuda Prime"),
    ("电P", "Volt Prime"),
    ("高P", "Gauss Prime"),
    ("猴P", "Wukong Prime"),
    ("骨P", "Xaku Prime"),
    ("船P", "Sevagoth Prime"),
    ("僧P", "Baruuk Prime"),
]:
    _hit = _c.wiki_lookup(_k) or {}
    check(f"★ wiki 段 {_k} → {_want}", _hit.get("title") == _want, str(_hit))
check(
    "★ 小写 p 后缀等价（花p → Wisp Prime）",
    (_c.wiki_lookup("花p") or {}).get("title") == "Wisp Prime",
)
# 无 Prime 的新战甲只落基体（灰机 API 实测 Kullervo/Dagath/Qorvex/Koumei/Cyte-09
# 的 Prime 页不存在，故不给它们造 P 键）
for _k, _want in [
    ("老九", "Cyte-09"),
    ("刀哥", "Kullervo"),
    ("暖气片", "Qorvex"),
    ("扣妹", "Koumei"),
    ("赛马娘", "Dagath"),
    ("书甲", "Dante"),
    ("黑咖喱", "Excalibur Umbra"),
    ("鸟哥", "Dante"),
]:
    _hit = _c.wiki_lookup(_k) or {}
    check(f"★ wiki 段外号 {_k} → {_want}", _hit.get("title") == _want, str(_hit))

_AL_WIKI = json.loads((ROOT / "core" / "data" / "aliases.json").read_text(encoding="utf-8"))["wiki"]
_CONCEPT = {
    "夜灵",
    "夜灵平野",
    "三傻",
    "平原",
    "紫卡",
    "裂罅",
    "玄骸",
    "信条",
    "杜卡德",
    "奸商",
    "新服",
    "新手",
}  # 概念页：中文标题已核对存在
_bad_titles = [
    k
    for k, v in _AL_WIKI.items()
    if k not in _CONCEPT and isinstance(v, dict) and re.search(r"[一-鿿]", v.get("title", ""))
]
check("★ 战甲外号页面名全为英文（无中文页面名）", not _bad_titles, str(_bad_titles[:5]))
check("wiki 段条目数 ≥ 60（原 12 条概念页 + 外号）", len(_AL_WIKI) >= 60, str(len(_AL_WIKI)))

for _k, _must in [
    ("花甲", "Wisp"),
    ("血妈", "Garuda"),
    ("猫", "Khora"),
    ("奶妈", "Trinity"),
    ("奶爸", "Oberon"),
    ("电", "Volt"),
    ("高", "Gauss"),
    ("跑男", "Gauss"),
    ("明神", "Limbo"),
    ("老猫甲", "Valkyr"),
]:
    _note = _c.wiki_clarify(_k)
    check(f"★ 易混淆澄清 {_k} 提到 {_must}", _must in _note, _note)
check("澄清：鸟姐=Zephyr（并把血妈/鸟妹分清）", "Zephyr" in _c.wiki_clarify("鸟姐"))
check("澄清未命中返回空串", _c.wiki_clarify("zzz 不存在") == "")
# 模糊阈值 0.8：短词不得被「X+P」这类新键劫持（「高」→ 高P / 「跑男」→ 老牌跑男
# 都是错的：应落回检索链给基体，2026-09-25 实测踩到）
check(
    "★ 模糊不劫持短词（高 / 跑男 / 花 不落 P 页或长外号）",
    all((_c.wiki_lookup(_q) or {}).get("title") is None for _q in ("高", "跑男", "花", "花甲")),
)
check(
    "概念页错字容忍仍在（杜卡德金 → 杜卡德金币）",
    (_c.wiki_lookup("杜卡德金") or {}).get("title") == "杜卡德金币",
)

# —— 哨兵：效果表不得再混入半角富文本标签（2026-09-29 v1.1.1 立）——
# 背景：d94da8e 官方整句替换曾把 <DT_FIRE_COLOR>/<LOWER_IS_BETTER> 等 16 种标签
# 带进 92 条效果行（全角〈〉清洗链兜不到半角）。数据侧已清 + 渲染侧
# wiki_intro._clean_hw_tags 双防线；此断言防后续新数据再犯。
_eff_data = json.loads((ROOT / "core" / "data" / "wiki_effect_zh.json").read_text(encoding="utf-8"))
_eff_tbl = _eff_data.get("effects") or {}
_hw_bad = [
    k for k, v in _eff_tbl.items() if isinstance(v, str) and re.search(r"<[A-Z_][A-Z0-9_]*>", v)
]
check(
    "★ 哨兵：wiki 效果表全表无半角富文本标签", not _hw_bad, f"{len(_hw_bad)} 条，例：{_hw_bad[:2]}"
)
# 渲染防线自证：喂一条带标签的合成串，_localize 必须清干净
_probe = WI._localize(
    "效果（满级 3）：造成 <DT_FIRE_COLOR>火焰伤害 <LOWER_IS_BETTER>15 <ENERGY> 能量。"
)
check(
    "★ 哨兵：半角标签渲染防线（_localize 清洗生效）",
    "<" not in _probe and "火焰伤害" in _probe and "15 能量" in _probe and "  " not in _probe,
    _probe,
)

# -------------------------------- 掉落兜底卡：黄金样本（2026-10-04 重建 drops.items 后）
# 背景：build_drops.py 旧实现把 items 从旧文件「原样续传」，而 places 每次重建、pid 重排
# ⇒ items[item] 的 pid 指向错误条目（实测 items['athodai blueprint'] 6 条里 4 条错连
# Axi A16/A17/A19/A2 Relic）。2026-10-04 已重建为「与 places 同源同编号」。
# ★ 黄金样本（固定期望值，不依赖联网）：Athodai Blueprint 的真值 = 6 个金星 Caches 节点，
#   B 轮 Rare 6.45%（与 DE 官方掉落页 / WFCD 一致）。
_drops = json.loads((ROOT / "core" / "data" / "drops.json").read_text(encoding="utf-8"))
_places = _drops.get("places") or {}
_ab = _drops.get("items", {}).get("athodai blueprint") or []
_ab_places = [_places.get(str(r[0])) or "" for r in _ab]
check(
    "★ 黄金样本：items['athodai blueprint'] 6 条全为金星 Caches（B 轮 6.45%）",
    len(_ab) == 6
    and all(p.startswith("金星 ·") and "储藏库" in p and "B轮" in p for p in _ab_places)
    and all(r[1] == "Rare" and r[2] == 6.45 for r in _ab),
    str(_ab_places),
)
check(
    "★ 黄金样本：不得出现遗物被当成掉落地点（旧 bug 形态）",
    not any("Relic" in p for p in _ab_places),
    str(_ab_places),
)
check(
    "★ 数据不变量：items 的每个 pid 都在 places 键域内（越界 = 0）",
    all(str(r[0]) in _places for lst in _drops.get("items", {}).values() for r in lst),
)
check(
    "★ 数据不变量：zh2en.values() ⊆ items.keys()（死映射已剔除）",
    all(en in (_drops.get("items") or {}) for en in (_drops.get("zh2en") or {}).values()),
)

# ★ 补丁回归（2026-10-04）：WFCD 源缺官方页的 Cryotic Front (Excavation) 整表，
#   build_drops.py::PATCH_CRYOTIC_EXCAVATION 补注入 30 行（A/B/C 轮，与官方页逐行一致）。
_exc_pids = {p for p, v in _places.items() if "Cryotic Front · 挖掘" in v}
_exc_labels = sorted(_places[p] for p in _exc_pids)
check(
    "★ 补丁：Cryotic Front (Excavation) 三张轮次表都在 places",
    len(_exc_pids) == 3 and all(any(f"{r}轮" in x for x in _exc_labels) for r in "ABC"),
    str(_exc_labels),
)
_a21 = [(str(r[0]), r[2]) for r in (_drops.get("items", {}).get("axi a21 relic") or [])]
check(
    "★ 补丁：items['axi a21 relic'] 含挖掘 C 轮 11.06% 来源（官方页真值）",
    any(p in _exc_pids and ch == 11.06 for p, ch in _a21),
    str(_a21[:6]),
)
_e400 = [(str(r[0]), r[2]) for r in (_drops.get("items", {}).get("400 endo") or [])]
check(
    "★ 补丁：items['400 endo'] 含挖掘 A 轮 25% 来源",
    any(p in _exc_pids and ch == 25 for p, ch in _e400),
    str(_e400[:6]),
)

# ★ 名称回归（2026-10-04）：WFCD 09-25 已把 Neo K10 的名称修回 Gyre Prime Systems
#   Blueprint；旧快照曾带回「Gyre Systems Blueprint」（与扎里曼赏金的真物品撞名）。
_gs = [
    _places.get(str(r[0])) or ""
    for r in (_drops.get("items", {}).get("gyre systems blueprint") or [])
]
_gp = [
    _places.get(str(r[0])) or ""
    for r in (_drops.get("items", {}).get("gyre prime systems blueprint") or [])
]
check(
    "★ 名称回归：'gyre systems blueprint' 不得指向任何 Neo K10 遗物（真值=扎里曼赏金）",
    bool(_gs) and not any("Neo K10" in p for p in _gs),
    str(_gs),
)
check(
    "★ 名称回归：'gyre prime systems blueprint' 必须含 Neo K10 四档",
    len([p for p in _gp if "Neo K10" in p]) == 4,
    str(sorted(_gp)),
)

# 掉率缺失（chance 为 None）的条目不得让掉落卡崩溃（2026-10-10：asterite 等 6 个物品 TypeError）
_bad = []
for _n in ("asterite", "aucrux capacitors", "gallos rods", "isos", "komms"):
    try:
        _c = WI._drops_card(_n)
        if not _c:
            _bad.append((_n, "无卡"))
    except Exception as _e:  # noqa: BLE001
        _bad.append((_n, repr(_e)))
check("掉率缺失的条目不崩：asterite / aucrux capacitors 等出卡", not _bad, str(_bad))

# ---- 2026-10-17：资料卡显示层兜底（Zaw 部件垃圾伤害 / 全零面板 / float32 残渣）----
_raw = [
    "定位：近战武器（Melee）｜Zaw 部件｜精通等级需求 0",
    "总伤害：0（磁力 0.5｜腐蚀 -28322093000000001802240｜Tau -28315716000000000393216…",
    "数据：暴击几率 0%｜暴击倍率 0×｜触发几率 0%｜射速 0",
    "特性：紫卡倾向 0.8",
]
_clean = WI._sanitize(list(_raw))
check(
    "兜底：总伤害 ≤ 0 的垃圾行不显示", not any(x.startswith("总伤害") for x in _clean), str(_clean)
)
check("兜底：全零「数据」行不显示", not any(x.startswith("数据") for x in _clean), str(_clean))
check("兜底：Zaw 部件补「组合后才有面板」说明", WI.MODULAR_NOTE in _clean, str(_clean))
_normal = ["定位：主武器（Primary）｜步枪", "总伤害：24（冲击 7.92｜穿刺 7.92｜切割 8.16）"]
check("兜底：正常武器原样不动", WI._sanitize(list(_normal)) == _normal)
check(
    "兜底：float32 残渣 → 合理精度",
    WI._sanitize(["效果：explodes after 0.6000000238418579s."]) == ["效果：explodes after 0.6s."],
)
_E, _K, _U = WI._table()
_left = [
    t
    for t, ls in _E
    if any(
        re.search(r"\d{13,}", x) or re.search(r"\d+\.\d{6,}", x)
        for x in WI._sanitize([str(y) for y in ls])
    )
]
check("兜底：全表处理后无 ≥13 位数字与 ≥6 位小数残渣", not _left, str(_left[:5]))
_r = WI.intro("宝拉")
check(
    "兜底：「宝拉」卡片无乱码、条目仍在",
    bool(_r) and not any(re.search(r"\d{13,}", x) for x in _r[1]),
    str(_r),
)
# 旧脏数据用夹具保留；生成器修好后，不能要求随包真数据继续带巨数。
from unittest.mock import patch as _patch  # noqa: E402

with _patch.object(WI, "_table", return_value=([["宝拉", list(_raw)]], {"宝拉": 0}, {})):
    _r_old = WI.intro("宝拉")
    with _patch.object(WI, "_sanitize", side_effect=lambda ls: ls):
        _r_raw = WI.intro("宝拉")
check(
    "旧数据夹具：intro 确实调用兜底并滤掉巨数",
    bool(_r_old) and not any(re.search(r"\d{13,}", x) for x in _r_old[1]),
)
check(
    "反向：没有兜底时旧宝拉夹具确实带巨数（测试有效）",
    bool(_r_raw) and any(re.search(r"\d{13,}", x) for x in _r_raw[1]),
)

# ---- 2026-10-17：wiki 指令错选（知识库审计）—— 调真实 _h_wiki ----
import asyncio as _aio  # noqa: E402

from core import parser as _P  # noqa: E402
from core.api_client import WarframeClient as _WC  # noqa: E402
from core.commands.wiki_misc import WikiMiscCommands as _WMC  # noqa: E402


class _WikiProbe(_WMC):
    def __init__(self):
        self.client = _WC()

    def _wiki_intro_on(self):
        return True


async def _wiki_titles(qs):
    probe = _WikiProbe()
    try:
        out = {}
        for q in qs:
            r = await probe._h_wiki(_P.parse("wiki " + q), None, "pc")
            out[q] = (r.title, r.extra_text or "")
        return out
    finally:
        await probe.client.close()


_res = _aio.run(_wiki_titles(["Balla", "仲裁", "Arbitration", "布莱顿", "花P"]))
check(
    "wiki Balla → 宝拉（Balla），不是 Ballas 的权杖",
    _res["Balla"][0] == "宝拉（Balla）",
    str(_res["Balla"]),
)
check(
    "wiki 仲裁 → 仲裁（搜索直达），不是仲裁雕像",
    _res["仲裁"][0] == "仲裁" and "search=" in _res["仲裁"][1],
    str(_res["仲裁"]),
)
check("wiki Arbitration → 仲裁", _res["Arbitration"][0] == "仲裁", str(_res["Arbitration"]))
check("回归：wiki 布莱顿 照常", _res["布莱顿"][0].startswith("布莱顿"), str(_res["布莱顿"]))
check("回归：wiki 花P 照常（概念页）", _res["花P"][0] == "Wisp Prime", str(_res["花P"]))

# ★ 2026-10-22：资料卡标题兜底 —— **内部资产名 / 路径不许原样上卡面**（维护者 10-19 报
#   `ChromaPrimeHelmetComponent` / `de:/Lotus/…/Forma`）。源头是知识库给资产条目的 id
#   （`scripts/kb/build_kb.py` 的 `"de:" + uniqueName`）：拿不到显示名时标题就成了内部名。
for _raw, _want in (
    ("de:/Lotus/Types/Recipes/Components/OrokinCatalyst", "Orokin Catalyst"),  # 路径 → 末段
    ("/Lotus/Types/Items/MiscItems/Forma", "Forma"),
    ("ChromaPrimeHelmetComponent", "Chroma Prime Helmet Component"),  # 驼峰拆词
    ("Ash", "Ash"),  # 正常名不动
    ("宝拉（Balla）", "宝拉（Balla）"),
):
    check(f"★ 标题兜底：{_raw} → {_want}", WI.display_title(_raw) == _want, WI.display_title(_raw))
check(
    "★ 标题兜底：结果里绝不残留路径",
    "/" not in WI.display_title("de:/Lotus/Types/Items/MiscItems/Forma"),
)
# ★ 反向守卫（2026-10-22 Claude 审出的误伤）：正常标题一律不动 —— 旧版改了 390 条
for _ok in (
    "红/白/蓝（Red/White/Blue）",
    "宽剑今昔纹章（Broadsword Past/Future Sigil）",
    "TennoCon 2016 披饰（Tennocon 2016 Syandana）",
    "RixtyMOL 拉托双枪（Rixtymol Aklato）",
    "Excalibur Prime",
):
    check(f"★ 标题兜底不误伤：{_ok}", WI.display_title(_ok) == _ok, WI.display_title(_ok))
_wi_raw = json.loads(WI._FILE.read_text(encoding="utf-8"))
_all = [e[0] for e in (_wi_raw["entries"] if isinstance(_wi_raw, dict) else _wi_raw)]
_chg = [t for t in _all if WI.display_title(t) != t]
check("★ 全量标题：只动代码名（≤ 20 条，现 16）", len(_chg) <= 20, str(len(_chg)))

# 未传官方词表时仅翻结构词；专名整名/歧义/节点/真实卡片链见 test_kb_quality.py。
for _raw, _want in (
    ("获取：Jupiter/Ganymede (Disruption), Rotation C 10%", "获取：木星/Ganymede（中断）, C轮 10%"),
    (
        "获取：Earth/Cetus (Level 50 - 70 Cetus Bounty), Rotation A 20%",
        "获取：地球/Cetus（等级 50 - 70 Cetus 赏金）, A轮 20%",
    ),
    ("获取：Duviri/Endless: Tier 6 (Hard) 11.9%", "获取：双衍王境/无尽: 层级 6（困难） 11.9%"),
    # 专名整段不动（返工的核心口径）
    ("获取：Fomorian Sabotage 5%", "获取：Fomorian Sabotage 5%"),
    (
        "获取：天王星 · The Kuva Wytch · 遭遇战 14.29%",
        "获取：天王星 · The Kuva Wytch · 遭遇战 14.29%",
    ),
    ("获取：Elite Orm Weaver 0.54%", "获取：Elite Orm Weaver 0.54%"),
    # 只动结构词、别的一律不动
    ("获取：Nightmare Mode Rewards, Rotation A 18.97%", "获取：Nightmare Mode Rewards, A轮 18.97%"),
):
    check(
        f"★ 获取行：{_raw[3:34]}…",
        _BIZ.translate_source(_raw) == _want,
        _BIZ.translate_source(_raw),
    )

# 当前种子再次执行结构性转换应保持专名；完整词表由构建入口显式提供。
_GET = [str(x) for _t, _ls in _ENTRIES for x in _ls if str(x).startswith("获取")]
_GET_NEW = [_BIZ.translate_source(x) for x in _GET]
check(
    "★ 全量：`The ` 计数与改前一致（不许删专名里的冠词）",
    sum(x.count("The ") for x in _GET) == sum(x.count("The ") for x in _GET_NEW),
)
check(
    "★ 全量：改后不出现行首 / 冒号后多余空格",
    not [x for x in _GET_NEW if re.match(r"^获取：\s", x) or re.search(r"：\s{2,}", x)],
)
check(
    "★ 全量：改动条数在预期量级（≤1200，防哪天规则失控全量改写）",
    sum(1 for a, b in zip(_GET, _GET_NEW) if a != b) <= 1200,
    str(sum(1 for a, b in zip(_GET, _GET_NEW) if a != b)),
)
_STRUCT_WORDS = (
    set(_BIZ._PLANET_ZH)
    | set(_BIZ._MISSION_ZH)
    | set(_BIZ._DIFF_ZH)
    | {
        "Rotation",
        "Level",
        "Tier",
        "Bounty",
        "Duviri",
        "Endless",
        "Normal",
        "Hard",
        "Elite",
    }
)
_broke = []
for _a, _b in zip(_GET, _GET_NEW):
    _wa = {w for w in re.findall(r"[A-Za-z][A-Za-z'\-]{3,}", _a)}
    _wb = {w for w in re.findall(r"[A-Za-z][A-Za-z'\-]{3,}", _b)}
    _lost = _wa - _wb - _STRUCT_WORDS
    if _lost:
        _broke.append((_a[:50], sorted(_lost)))
check("★ 全量：专名（≥4 字母的 ASCII 词）一条都没被打断", not _broke, str(_broke[:3]))
check(
    "★ 效果行卫生：未命中译文 ⇒ and → 和、去行末句点",
    WI.tidy_effect_text("tar within 30m and dealing 120 Damage.")
    == "tar within 30m 和 dealing 120 Damage",
)

# ★ 2026-10-23（Claude 定案）：代码名标题的条目**并入同资产的中文孪生条目**
#   （直接改名会造出两张同名卡；代码名那条是游戏内部的套装加成 Mod，玩家不会搜）。
_m, _e2, _a2, _i2 = _BIZ.merge_code_name_twins(
    [["套装 · 诡文枭主（Boreal）", ["类型 MOD 套装"]], ["Borealsetmod", ["类型 MOD 套装 MOD"]]],
    [{"asset_id": "de:/BorealSetMod"}, {"asset_id": "de:/BorealSetMod"}],
    {"de:/BorealSetMod": [0, 1]},
)
check(
    "并入：代码名那条不再单独出条目",
    len(_e2) == 1 and _e2[0][0] == "套装 · 诡文枭主（Boreal）",
    str(_e2),
)
check("并入：孪生缺的行补过去", "类型 MOD 套装 MOD" in _e2[0][1], str(_e2[0][1]))
check("并入：下标整体重映射", _i2 == {"de:/BorealSetMod": [0]}, str(_i2))
check("并入：报告记下并入了哪条", bool(_m) and _m[0]["code_name"] == "Borealsetmod", str(_m))
# 反向护栏：单条目 / 代码名但没有中文孪生 ⇒ 一条都不动
_m3, _e4, _, _ = _BIZ.merge_code_name_twins(
    [["Ash", ["x"]], ["Excalibur", ["y"]]],
    [{"asset_id": "de:/A"}, {"asset_id": "de:/B"}],
    {"de:/A": [0], "de:/B": [1]},
)
check("反向：各自单条 ⇒ 不动", _m3 == [] and len(_e4) == 2, str(_m3))
_m5, _e6, _, _ = _BIZ.merge_code_name_twins(
    [["Borealsetmod", ["a"]]], [{"asset_id": "de:/S"}], {"de:/S": [0]}
)
check("反向：代码名但没有中文孪生 ⇒ 不动", _m5 == [] and len(_e6) == 1, str(_m5))
# 真数据：19 条 setmod 代码名已从标题消失、但仍可当检索别名（不显示）
_W = json.loads((ROOT / "core" / "data" / "wiki_intro.json").read_text(encoding="utf-8"))
_titles = {e[0] for e in _W["entries"]}
_left = sorted(t for t in _titles if t.lower().endswith("setmod"))
check("真数据：`*setmod` 代码名标题已归零", not _left, str(_left))
check(
    "真数据：19 条代码名仍作检索别名",
    sum(1 for k in _W["keys"] if "setmod" in k) == 19,
    str(sum(1 for k in _W["keys"] if "setmod" in k)),
)
# ★ 指令要求的是「无**新增**重名」—— 真数据里本来就有同名条目（如不同来源的同名资源），
#   所以这里只钉「并入没把孪生变成重名」（并入 = 摘掉一条 + 给孪生补行，不该制造同名卡）。
_cnt: dict = {}
for _e in _W["entries"]:
    _cnt[_e[0]] = _cnt.get(_e[0], 0) + 1
_aliases = [k for k in _W["keys"] if "setmod" in k]
_twin_idx = {
    (_W["keys"][k][0] if isinstance(_W["keys"][k], list) else _W["keys"][k]) for k in _aliases
}
_twin_titles = [_W["entries"][i][0] for i in _twin_idx]
check(
    "真数据：并入的孪生条目没变成重名（并入不制造同名卡）",
    len(_twin_titles) == 19 and all(_cnt[t] == 1 for t in _twin_titles),
    str([t for t in _twin_titles if _cnt[t] != 1][:3]) + f" 孪生数={len(_twin_titles)}",
)

# 来源字段迁移：同键官方语言表的固定字段语义，不把普通节点当物品翻译。
_field_values = {
    "Cetus": "希图斯",
    "Cambion Drift": "魔胎之境",
    "Orb Vallis": "奥布山谷",
    "Veil Proxima": "面纱比邻星域",
    "Duviri": "双衍王境",
    "Zariman": "扎里曼号",
    "Höllvania": "霍瓦尼亚",
    "Kuva Fortress": "赤毒要塞",
    "Conclave": "武形秘仪",
    "Cephalon Capture": "中枢争夺",
    "Annihilation": "歼夺",
    "The Descendia": "沉沦之地",
    "Arena": "竞技场",
    "The Circuit": "无尽回廊",
    "The Perita Rebellion": "佩里塔叛乱",
    "Void Armageddon": "虚空决战",
    "Infested Salvage": "INFESTED 资源回收",
    "Defense": "防御",
    "Exterminate": "歼灭",
    "Mistral": "西北风",
    "Whirlwind": "旋风",
    "Tempest": "暴风",
    "Hurricane": "飓风",
    "Typhoon": "台风",
    "Operational Supply": "行动补给",
    "Champion": "英雄",
}
_field_en, _field_zh = {}, {}
for _kind, _fields in _BIZ._SOURCE_FIELD_KEYS.items():
    for _name, _suffix in _fields.items():
        _key = "/Lotus/Language/" + _suffix
        _field_en[_key] = _name.upper() if _kind == "mission" else _name
        _field_zh[_key] = _field_values[_name]
_field_names = _BIZ.build_source_names(_field_en, _field_zh)
check("来源字段：固定官方键接受大小写差异", sum(map(len, _field_names.fields.values())) == 26)
for _raw, _want in (
    (
        "获取：地球/Cetus（等级 50 - 70 Cetus 赏金） 20%",
        "获取：地球/希图斯（等级 50 - 70 希图斯 赏金） 20%",
    ),
    ("获取：Veil Proxima · Arc Silver · Defense 8%", "获取：面纱比邻星域 · Arc Silver · 防御 8%"),
    ("获取：海王星/Cephalon Capture（Conclave） 2%", "获取：海王星/中枢争夺（武形秘仪） 2%"),
    ("获取：Conclave, Typhoon 100%", "获取：武形秘仪, 台风 100%"),
    ("获取：Operational Supply, Champion 100%", "获取：行动补给, 英雄 100%"),
    (
        "获取：The Descendia: Uncommon Rewards (Steel Path) 7%",
        "获取：沉沦之地: Uncommon Rewards (Steel Path) 7%",
    ),
    ("获取：谷神星/Lex（Defense） 10%", "获取：谷神星/Lex（防御） 10%"),
    ("获取：Cambion Drif…；金星/Orb Val…", "获取：Cambion Drif…；金星/Orb Val…"),
    (
        "获取：土星 · Variant Team Annihilation · 武形秘仪 3%",
        "获取：土星 · Variant Team Annihilation · 武形秘仪 3%",
    ),
    (
        "获取：Whirlwind 10%；Carrier 1%；稀有 Corpus 存储容器 1%；Grineer 2%",
        "获取：Whirlwind 10%；Carrier 1%；稀有 Corpus 存储容器 1%；Grineer 2%",
    ),
):
    _translated = _BIZ.translate_source_fields(_raw, _field_names)
    check("来源字段迁移：" + _raw, _translated == _want, _translated)
    check(
        "来源字段幂等：" + _raw,
        _BIZ.translate_source_fields(_translated, _field_names) == _translated,
    )
check(
    "来源结构翻译：魔胎之境采用同批官方译名且保留节点",
    _BIZ.translate_source("获取：Cambion Drift/Test Node") == "获取：魔胎之境/Test Node",
)

# 在真实 Markdown 解析入口验证：先翻来源字段，再执行卡片长度截断。
import tempfile as _tempfile  # noqa: E402

with _tempfile.TemporaryDirectory() as _td:
    _fixture = Path(_td) / "source.md"
    _fixture.write_text(
        "### 来源测试\n- 获取：Earth/Cetus (Level 50 - 70 Cetus Bounty) " + "长" * 200 + "\n"
    )
    _parsed, _mats = _BIZ.parse_kb(_fixture, 6, source_names=_field_names)
    _source_line = _parsed[0][1][0]
    check(
        "真实解析入口：截断前翻译来源字段",
        "希图斯" in _source_line and "Cetus" not in _source_line and _source_line.endswith("…"),
        _source_line,
    )

# 显式迁移入口的保全、dry-run、check 和幂等护栏（独立临时文件，不写真实种子）。
import contextlib as _contextlib  # noqa: E402
import io as _io  # noqa: E402

with _tempfile.TemporaryDirectory() as _td:
    _dir = Path(_td)
    for _lang, _values in (("en", _field_en), ("zh", _field_zh)):
        (_dir / f"dict.{_lang}.json").write_text(json.dumps(_values))
    _, _source_meta = _BIZ.load_source_names(_dir)
    _migration_file = _dir / "cards.json"
    _migration_data = {
        "_meta": {"source_names": _source_meta, "generated": "fixed"},
        "entries": [["原题", ["获取：Conclave, Typhoon 100%", "说明：Conclave"]]],
        "keys": {"原题": 0},
        "views": {"x": [0]},
        "pvp": [0],
        "uses": {"材料": 1},
    }
    _migration_file.write_text(json.dumps(_migration_data, ensure_ascii=False))
    _old_out, _old_argv = _BIZ.OUT, sys.argv
    try:
        _BIZ.OUT = _migration_file

        def _migration_run(*flags):
            sys.argv = ["build_wiki_intro.py", "--source-dict-dir", str(_dir), *flags]
            with _contextlib.redirect_stdout(_io.StringIO()):
                return _BIZ.main()

        _before = _migration_file.read_bytes()
        check(
            "字段迁移 dry-run 零写入",
            _migration_run("--dry-run") == 0 and _migration_file.read_bytes() == _before,
        )
        check(
            "字段迁移 check 报待迁移且零写入",
            _migration_run("--check") == 1 and _migration_file.read_bytes() == _before,
        )
        check("字段迁移显式执行", _migration_run("--repair-source-lines") == 0)
        _after_data = json.loads(_migration_file.read_text())
        check(
            "字段迁移其他顶层字段保全",
            all(v == _after_data[k] for k, v in _migration_data.items() if k != "entries"),
        )
        check(
            "字段迁移标题及非获取行保全",
            _after_data["entries"][0] == ["原题", ["获取：武形秘仪, 台风 100%", "说明：Conclave"]],
        )
        _after = _migration_file.read_bytes()
        check("字段迁移 check 通过", _migration_run("--check") == 0)
        check(
            "字段迁移第二次执行逐字节幂等",
            _migration_run("--repair-source-lines") == 0 and _migration_file.read_bytes() == _after,
        )
        (_dir / "dict.en.json").write_text(json.dumps({**_field_en, "unexpected": "different"}))
        _rejected = False
        try:
            _migration_run("--repair-source-lines")
        except ValueError:
            _rejected = True
        check(
            "字段迁移拒绝不同批词表且零写入", _rejected and _migration_file.read_bytes() == _after
        )
    finally:
        _BIZ.OUT, sys.argv = _old_out, _old_argv

# 掉落来源标签迁移只动 places；离线词表用夹具提供，不依赖开发机 _cache。
from copy import deepcopy as _deepcopy  # noqa: E402
from scripts import build_drops as _BD  # noqa: E402

_drop_en = {
    **_field_en,
    "enemy": "Test Enemy",
    "source_a": "Test Source A",
    "source_b": "Test Source B",
    "ambiguous_a": "Ambiguous Enemy",
    "ambiguous_b": "Ambiguous Enemy",
    "weapon": "Lex",
}
_drop_zh = {
    **_field_zh,
    "enemy": "测试敌人",
    "source_a": "同名来源",
    "source_b": "同名来源",
    "ambiguous_a": "歧义甲",
    "ambiguous_b": "歧义乙",
    "weapon": "列克斯",
}
_drop_names = _BIZ.build_source_names(_drop_en, _drop_zh)
for _raw, _want in (
    ("Test Enemy", "测试敌人"),
    ("地球 · Cetus · 防御 · A轮", "地球 · 希图斯 · 防御 · A轮"),
    ("Conclave, Typhoon 100%", "武形秘仪, 台风 100%"),
    ("地球 · Lex · 防御 · A轮", "地球 · Lex · 防御 · A轮"),
    ("Test Enemy Extra", "Test Enemy Extra"),
    ("Test Ene…", "Test Ene…"),
    ("Ambiguous Enemy", "Ambiguous Enemy"),
    ("Unknown Enemy", "Unknown Enemy"),
):
    check("掉落标签来源边界：" + _raw, _BD.translate_label(_raw, _drop_names) == _want)
_drop_old = {
    "source": "fixed-source",
    "official_source": "fixed-official",
    "updated": "fixed-date",
    # 故意把数值编号排成与来源名相反，防只按排序回接造成串号。
    "places": {"91": "Test Source A", "7": "Test Source B", "23": "地球 · Cetus · 防御 · A轮"},
    "items": {
        "drop a": [[91, "Rare", 1.25]],
        "drop b": [[7, "Common", 98.75]],
        "node drop": [[23, "Rare", 3]],
    },
    "zh2en": {"奖励甲": "drop a"},
    "relic_drops": {"lith test relic": [[91, "Rare", 1.25]]},
    "relic_special_sources": {"lith test relic": ["固定说明"]},
    "future_field": {"nested": [1, 2]},
}
_drop_snapshot = _deepcopy(_drop_old)
_drop_migrated = _BD.migrate_labels(_drop_old, _drop_names)
check("掉落迁移不突变入参", _drop_old == _drop_snapshot)
check(
    "掉落迁移编号集合与业务字段逐值不变",
    set(_drop_migrated["places"]) == set(_drop_old["places"])
    and all(_drop_migrated[k] == v for k, v in _drop_old.items() if k != "places"),
)
check(
    "掉落迁移只改既有标签",
    _drop_migrated["places"]
    == {"91": "同名来源", "7": "同名来源", "23": "地球 · 希图斯 · 防御 · A轮"},
)
check("掉落迁移重复执行幂等", _BD.migrate_labels(_drop_migrated, _drop_names) == _drop_migrated)
_drop_migrated["future_field"]["nested"].append(3)
check("掉落迁移结果深层修改不影响原对象", _drop_old == _drop_snapshot)
_drop_migrated = _BD.migrate_labels(_drop_old, _drop_names)
_drop_rows = [
    {"place": "Test Source A", "item": "Drop A", "rarity": "Rare", "chance": 1.25},
    {"place": "Test Source B", "item": "Drop B", "rarity": "Common", "chance": 98.75},
    {
        "place": "Earth/Cetus (Defense), Rotation A",
        "item": "Node Drop",
        "rarity": "Rare",
        "chance": 3,
    },
]
with _patch.object(_BD, "apply_patches", side_effect=lambda rows, html: rows):
    _drop_html = "<b>Last Update:</b> fixed-date"
    _from_english = _BD.build(_drop_rows, _drop_html, _drop_old, _drop_names)
    _from_chinese = _BD.build(_drop_rows, _drop_html, _drop_migrated, _drop_names)
    _repeat_build = _BD.build(_drop_rows, _drop_html, _from_chinese, _drop_names)
    check(
        "完整重建兼容旧英文/已译标签且同名来源不串号",
        _from_english == _from_chinese
        and _from_chinese["places"] == _drop_migrated["places"]
        and _from_chinese["items"] == _drop_old["items"],
    )
    check("完整重建重复执行保留译文和编号", _repeat_build == _from_chinese)
    _unmatched = _deepcopy(_drop_migrated)
    _unmatched["items"]["drop a"][0][2] = 50
    _rejected = False
    try:
        _BD.build(_drop_rows, _drop_html, _unmatched, _drop_names)
    except ValueError:
        _rejected = True
    check("同名来源奖励无法精确接回时拒绝猜配", _rejected)
    _last_unmatched = _deepcopy(_drop_migrated)
    _last_unmatched["items"]["drop b"][0][2] = 50
    _last_rejected = False
    try:
        _BD.build(_drop_rows, _drop_html, _last_unmatched, _drop_names)
    except ValueError:
        _last_rejected = True
    check("同名最后一个来源奖励变化仍拒绝猜配", _last_rejected)

with _tempfile.TemporaryDirectory() as _td:
    _dir = Path(_td)
    for _lang, _values in (("en", _drop_en), ("zh", _drop_zh)):
        (_dir / f"dict.{_lang}.json").write_text(json.dumps(_values))
    _drop_file = _dir / "drops.json"
    _drop_file.write_text(json.dumps(_drop_old, ensure_ascii=False))

    def _drop_run(*flags):
        _args = [
            "build_drops.py",
            "--migrate-labels",
            "--source-dict-dir",
            str(_dir),
            "--out",
            str(_drop_file),
            *flags,
        ]
        with (
            _patch.object(sys, "argv", _args),
            _patch.object(_BD, "read_source", side_effect=AssertionError("离线迁移不得读取掉落源")),
            _contextlib.redirect_stdout(_io.StringIO()),
        ):
            return _BD.main()

    _before = _drop_file.read_bytes()
    check(
        "掉落迁移 dry-run 零写入",
        _drop_run("--dry-run") == 0 and _drop_file.read_bytes() == _before,
    )
    check(
        "掉落迁移 check 报待迁移且零写入",
        _drop_run("--check") == 1 and _drop_file.read_bytes() == _before,
    )
    check("掉落迁移显式执行", _drop_run() == 0)
    check("掉落迁移真实入口只改 labels", json.loads(_drop_file.read_text()) == _drop_migrated)
    _after = _drop_file.read_bytes()
    check("掉落迁移 check 已完成", _drop_run("--check") == 0)
    check("掉落迁移二次执行逐字节不变", _drop_run() == 0 and _drop_file.read_bytes() == _after)
    (_dir / "dict.en.json").unlink()
    _missing_rejected = False
    try:
        _drop_run()
    except FileNotFoundError:
        _missing_rejected = True
    check("掉落迁移缺本地词表报错且零写入", _missing_rejected and _drop_file.read_bytes() == _after)

# 获取行完整重建：结构与官方字段先组合翻译，最后才截断。
_source_fixture_cases = (
    (
        "获取：Earth/Cetus (Level 20 - 40 Cetus Bounty), Rotation A 5.41%",
        "获取：地球/希图斯（等级 20 - 40 希图斯 赏金）, A轮 5.41%",
    ),
    (
        "获取：地球/Cetus (等级 20 - 40 希图斯 Bounty), Rotation A 5.41%",
        "获取：地球/希图斯（等级 20 - 40 希图斯 赏金）, A轮 5.41%",
    ),
    (
        "获取：Neptune/Cephalon Capture (Conclave), Rotation A 2.48%；Saturn/Annihilation (Conclave), Rotation B 2.48%",
        "获取：海王星/中枢争夺（武形秘仪）, A轮 2.48%；土星/歼夺（武形秘仪）, B轮 2.48%",
    ),
    ("获取：Duviri/Endless: Tier 4 (Normal) 1.49%", "获取：双衍王境/无尽: 层级 4（普通） 1.49%"),
    (
        "获取：Named Region, Deimos/Unknown Node (Survival), Rotation A 2.5%",
        "获取：Named Region, 火卫二/Unknown Node（生存）, A轮 2.5%",
    ),
    ("获取：Earth/Deimos (Survival) 2.5%", "获取：地球/Deimos（生存） 2.5%"),
    ("获取：地球/Unknown Node（间谍） 2.5%", "获取：地球/Unknown Node（间谍） 2.5%"),
    (
        "获取：Unknown Planet/Unknown Node (Unlisted Mode) 2.5%",
        "获取：Unknown Planet/Unknown Node (Unlisted Mode) 2.5%",
    ),
)
for _raw, _want in _source_fixture_cases:
    _translated = _BIZ.translate_source(_raw, _field_names)
    check("完整来源结构：" + _raw, _translated == _want, _translated)
    check("完整来源幂等：" + _raw, _BIZ.translate_source(_translated, _field_names) == _translated)
    check(
        "完整来源数字流：" + _raw,
        re.findall(r"\d+(?:\.\d+)?%?", _translated) == re.findall(r"\d+(?:\.\d+)?%?", _raw),
    )

with _tempfile.TemporaryDirectory() as _td:
    _dir = Path(_td)
    _source_path = _dir / "kb/01_战甲.md"
    _source_path.parent.mkdir()
    _long_source = (
        "获取：Earth/Cetus (Level 20 - 40 Cetus Bounty), Rotation A 5.41%；"
        "Venus/Orb Vallis (Level 30 - 50 Orb Vallis Bounty), Rotation C 7.25%；"
        "Unknown Planet/UnlistedLongNode (Unlisted Mode) 2.5%…（共 3 处）"
    )
    _source_path.write_text("### 通用来源测试\n- 类型：测试\n- " + _long_source + "\n")
    _source_bytes = _source_path.read_bytes()
    _parsed, _ = _BIZ.parse_kb(_source_path, 6, source_names=_field_names)
    _line = _parsed[0][1][1]
    check(
        "完整解析：长来源在截断前结构及专名全译",
        "Earth" not in _line and "Cetus" not in _line and "Bount" not in _line,
        _line,
    )
    check(
        "完整解析：截断不露出下一来源半词",
        "Venus/" not in _line and "UnlistedLong" not in _line and _line.endswith("…（共 3 处）"),
        _line,
    )
    check("完整解析：截断长度受限", len(_line) <= _BIZ.MAX_LEN, _line)
    check("完整解析：原稿逐字节不变", _source_path.read_bytes() == _source_bytes)
    check(
        "单来源截断不切开英文token",
        _BIZ.truncate_source("获取：" + "长" * 90 + " UnlistedLongNode 1.25%", 100).endswith("长…"),
    )

    _dict_dir = _dir / "dict"
    _dict_dir.mkdir()
    for _lang, _values in (("en", _field_en), ("zh", _field_zh)):
        (_dict_dir / f"dict.{_lang}.json").write_text(json.dumps(_values))
    # CLI跑独立完整main，ROOT定位沙箱，不把OUT重定向成真实种子。
    _cli_root = _dir / "cli"
    (_cli_root / "scripts").mkdir(parents=True)
    (_cli_root / "core/data").mkdir(parents=True)
    (_cli_root / "scripts/build_wiki_intro.py").write_bytes(
        (_ROOT / "scripts/build_wiki_intro.py").read_bytes()
    )
    import subprocess as _source_subprocess  # noqa: E402

    _argv = [
        sys.executable,
        str(_cli_root / "scripts/build_wiki_intro.py"),
        "--kb-dir",
        str(_source_path.parent),
        "--source-dict-dir",
        str(_dict_dir),
        "--identity-out",
        str(_cli_root / "identity.json"),
    ]
    _cli_one = _source_subprocess.run(_argv, capture_output=True, text=True)
    check("完整来源CLI真实main成功", _cli_one.returncode == 0, _cli_one.stderr)
    if _cli_one.returncode == 0:
        _cli_bytes = (_cli_root / "core/data/wiki_intro.json").read_bytes()
        _cli_data = json.loads(_cli_bytes)
        check(
            "完整来源CLI真实main使用同一翻译截断链",
            _cli_data["entries"][0][1][1] == _line,
            str(_cli_data["entries"]),
        )
        _cli_two = _source_subprocess.run(_argv, capture_output=True, text=True)
        check(
            "完整来源CLI二次构建逐字节一致",
            _cli_two.returncode == 0
            and (_cli_root / "core/data/wiki_intro.json").read_bytes() == _cli_bytes,
            _cli_two.stderr,
        )
        check("完整来源CLI原稿不变", _source_path.read_bytes() == _source_bytes)

# 生成器真实parse_kb→runtime获取表：只允许括号外分号作为完整来源边界。
with _tempfile.TemporaryDirectory() as _td:
    _nested_path = Path(_td) / "nested.md"
    _nested_source = (
        "获取：虚空遗物（甲部件：古纪 X1 遗物 / 前纪 X2 遗物；"
        "乙部件：古纪 X3 遗物 / 前纪 X4 遗物；"
        "丙部件：古纪 X5 遗物 / 前纪 X6 遗物）"
    )
    _nested_body = _nested_source[len("获取：虚空遗物（") : -1]
    _full_parts = _nested_body.split("；")
    _saved_cap = _BIZ.MAX_LEN
    try:
        for _cap in (56, 64, 100):
            _BIZ.MAX_LEN = _cap
            _nested_path.write_text("### 通用嵌套来源测试\n- " + _nested_source + "\n")
            _nested_rows, _ = _BIZ.parse_kb(_nested_path, 6, source_names=_field_names)
            _nested_short = _nested_rows[0][1][0]
            _rendered_parts = [
                x for x in WI._acquire_rows("◆ " + _nested_short) if x.startswith("· ")
            ]
            if len(_nested_source) <= _cap:
                check(
                    "嵌套来源完整时部件不丢：" + str(_cap),
                    len(_rendered_parts) == 3,
                    str(_rendered_parts),
                )
            else:
                _short_parts = _nested_short[len("获取：虚空遗物（") :].rstrip("…").split("；")
                check(
                    "嵌套来源fallback末项确为原文半项：" + str(_cap),
                    bool(_short_parts[-1])
                    and _full_parts[len(_short_parts) - 1].startswith(_short_parts[-1])
                    and _short_parts[-1] != _full_parts[len(_short_parts) - 1],
                    _nested_short,
                )
                check(
                    "嵌套来源runtime只丢实际半项：" + str(_cap),
                    len(_rendered_parts) == max(1, len(_short_parts) - 1)
                    and all(
                        part.split("：", 1)[0] in row
                        for part, row in zip(_full_parts, _rendered_parts)
                    ),
                    str(_rendered_parts),
                )
        _BIZ.MAX_LEN = 48
        _outside = "获取：来源甲 1%；" + _nested_source[3:]
        _nested_path.write_text("### 通用嵌套来源测试\n- " + _outside + "\n")
        _nested_rows, _ = _BIZ.parse_kb(_nested_path, 6, source_names=_field_names)
        check(
            "嵌套来源优先括号外分号：真实parse到runtime",
            _nested_rows[0][1][0] == "获取：来源甲 1%…"
            and WI._acquire_rows("◆ " + _nested_rows[0][1][0])
            == ["◆ 获取：来源甲 1%（还有其他来源）"],
            str(_nested_rows),
        )
    finally:
        _BIZ.MAX_LEN = _saved_cap

if FAILED:
    print(f"\n失败 {len(FAILED)} 项：{FAILED}")
    sys.exit(1)

# ★ 2026-10-25（Astra 真实宿主补测）：同名多条（膛线 满级 3 / 5 / 10）取满级最高的那条
_t_ser, _l_ser = WI.intro("膛线")
check(
    "★ wiki 膛线：取满级 10（+165%），不取满级 3 的旧条目",
    any("满级 10" in x for x in _l_ser) and not any("满级 3" in x for x in _l_ser),
    str(_l_ser[:3]),
)

print("\n全部通过 ✔")
