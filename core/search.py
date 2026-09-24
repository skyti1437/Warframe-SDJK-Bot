# -*- coding: utf-8 -*-
"""统一的本地物品检索（服务于 wiki 指令）。

为什么需要它
------------
「wiki」原先只查 **warframe.market 物品字典**：那是**可交易品**清单，
Forma、内融核心、赤毒、各种资源都不在里面，于是用户搜这些永远「查无此物」。

本模块把三份本地数据合成一份索引：

===============  ==========  ============================================
来源              条数        覆盖
===============  ==========  ============================================
de_items_zh      17501       DE 官方本地化导出（EN↔CN，最权威）
aliases          1378        玩家黑话 → WM slug（充沛 / 猎人战备 …）
drops            3561        DE 官方掉落表里的物品名（英文为主）
===============  ==========  ============================================

排序规则刻意做成 **精确 > 前缀 > 词边界包含 > 部分词**：
WM 那边的 bug 就是栽在「任意子串」上 —— 搜 ``Forma`` 会命中
``valence_formation``（Valence Formation / 效价炼成），因为 "forma" 是
"formation" 的子串。
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Optional

try:
    from . import matching        # core 包内正常导入（变体词表同源）
except ImportError:               # 离线脚本把 core/ 当顶层路径导入时
    import matching

_HERE = Path(__file__).resolve().parent
_DATA = _HERE / "data"
_ALIAS_FILE = _DATA / "aliases.json"
_DE_ITEMS_FILE = _DATA / "de" / "de_items_zh.json"
_EN_ZH_FILE = _DATA / "de" / "name_en_zh.json"
_CJK = re.compile(r"[一-鿿]")
_DROPS_FILE = _DATA / "drops.json"

_NON_WORD = re.compile(r"[^0-9a-z一-鿿]+")


def _jload(path: Path) -> dict:
    """读 JSON，文件缺失或损坏时返回空字典（数据文件是可选项）。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _norm(s: str) -> str:
    """归一化：小写、去标点、压空格。用于比较而非展示。"""
    return " ".join(_NON_WORD.split((s or "").lower())).strip()


@lru_cache(maxsize=1)
def _index() -> tuple[tuple[str, str, str, str], ...]:
    """构建检索索引。

    Returns:
        每项为 ``(norm_key, 展示名, 次级名, 来源)`` 的元组（不可变，便于缓存）。
    """
    rows: dict[str, list[str]] = {}   # norm_key -> [展示名, 次级名, 来源]

    def add(*names: str, source: str) -> None:
        """把一组等价名（中/英/别名）写进索引。

        展示名一律**优先取中文**：同一个英文 key 会被多条来源重复注册
        （掉落表只给英文、官方导出给中文），若按先到先得就会把中文顶掉，
        用户搜英文看到的却是英文名。
        """
        cn = next((n for n in names if n and _CJK.search(n)), "")
        en = next((n for n in names if n and not _CJK.search(n)), "")
        shown = cn or next((n for n in names if n), "")
        second = en or next((n for n in names[1:] if n and n != shown), "")
        for n in filter(None, names):
            k = _norm(n)
            if not k:
                continue
            cur = rows.get(k)
            if cur is None or (_CJK.search(shown) and not _CJK.search(cur[0])):
                rows[k] = [shown, second, source]

    # 1) DE 官方本地化导出：权威中文名，覆盖 Forma / 赤毒 / 氩结晶 这类
    #    既不可交易、本地词典又漏收的物品
    for it in _jload(_DE_ITEMS_FILE).get("items") or []:
        if isinstance(it, dict):
            add(it.get("zh", ""), it.get("en", ""), source="官方")
    for zh, slug in (_jload(_ALIAS_FILE).get("wm_items") or {}).items():
        add(zh, (slug or "").replace("_", " "), source="别名")
    for zh, slug in (_jload(_ALIAS_FILE).get("riven_items") or {}).items():
        add(zh, (slug or "").replace("_", " "), source="别名")
    for en in (_jload(_DROPS_FILE).get("items") or {}):
        add(en, source="掉落表")
    return tuple((k, v[0], v[1], v[2]) for k, v in rows.items())


def _score(query: str, key: str) -> int:
    """匹配分档，越小越相关。``-1`` 表示不匹配。

    Args:
        query: 归一化后的查询串。
        key: 归一化后的索引键。

    Returns:
        ``0`` 精确 / ``1`` 前缀 / ``2`` 词边界包含 / ``3`` 部分词命中；
        不匹配返回 ``-1``。
    """
    if key == query:
        return 0
    if key.startswith(query):
        return 1
    # 词边界包含：避免 forma 命中 valence_formation
    if re.search(rf"(?<![0-9a-z]){re.escape(query)}(?![0-9a-z])", key):
        return 2
    # 多词查询里任意一个词命中即可（"primed flow" 能搜到 "primed flow"）
    # 注意这里**不能**退化成「任意子串」——那正是 forma -> valence_formation
    # 的成因。词边界检查必须始终成立。
    for part in query.split():
        if len(part) >= 2 and re.search(
                rf"(?<![0-9a-z]){re.escape(part)}(?![0-9a-z])", key):
            return 3
    return -1


def search(query: str, limit: int = 8) -> list[dict]:
    """在本地索引里检索物品。

    Args:
        query: 用户输入（中文名 / 英文名 / 黑话均可）。
        limit: 最多返回几条。

    Returns:
        ``[{"name", "en", "source", "score"}]``，按相关度升序。
    """
    q = _norm(query)
    if len(q) < 1:
        return []
    scored: list[tuple[int, str, str, str, str]] = []
    for key, shown, second, source in _index():
        s = _score(q, key)
        if s < 0:
            continue
        scored.append((s, shown, second, source, key))
    scored.sort(key=lambda r: (r[0], len(r[4]), r[1]))
    out: list[dict] = []
    seen: set[str] = set()
    for s, shown, second, source, key in scored:
        if shown.lower() in seen:
            continue
        seen.add(shown.lower())
        out.append({"name": shown, "en": second, "source": source, "score": s})
        if len(out) >= limit:
            break
    return out


def best(query: str) -> Optional[dict]:
    """返回最相关的一条，无结果时返回 ``None``。"""
    hits = search(query, limit=1)
    return hits[0] if hits else None


# WM 展示名里的中文后缀（「Banshee Prime 一套」这类）
_ZH_SUFFIXES = (" 一套", " 蓝图", " 机体", " 系统", " 头部神经光元", " 头部", " 配件")


@lru_cache(maxsize=1)
def _alias_slug_index() -> dict[str, str]:
    """别名键（归一化）→ slug；wm_items 优先于 riven_items。

    别名条目的展示名可能是英文缩写（DJ / AMP / octavia），此时 ``en`` 字段
    拿不到 slug，只能回别名词典按归一化键反查（2026-09-24 实测）。
    """
    out: dict[str, str] = {}
    for sec in ("riven_items", "wm_items"):      # wm 后写 → wm 覆盖
        for key, slug in (_jload(_ALIAS_FILE).get(sec) or {}).items():
            if key and slug:
                out[_norm(key)] = slug
    return out


@lru_cache(maxsize=1)
def _official_zh_by_en() -> dict[str, str]:
    """归一化英文显示名 → DE 官方简中名（只含已翻译条目）。

    数据由 ``scripts/build_name_en_zh.py`` 从 DE 官方双语词表
    （dict.en.json / dict.zh.json，35,865 条同键）交集生成。
    """
    return dict(_jload(_EN_ZH_FILE).get("names") or {})


def _title_en(name: str) -> str:
    """小写英文转写 → 国际服英文原名写法（逐词首字母大写）。"""
    return " ".join(w[:1].upper() + w[1:] for w in (name or "").split())


def base_slug(slug: str) -> str:
    """剥套装/变体前后缀 → 基体 slug（``nekros_prime_set`` → ``nekros``）。

    变体词表复用 ``core.matching``（赤毒/信条/亡魂/破坏者/棱晶/圣洁/终幕/
    MK1…中英同表），保证与紫卡/倾向路径同一口径。按**下划线词边界**剥，
    保留词形（``arcane_energize`` 不能被归一化成 ``arcaneenergize``）；
    剥空了就原样返回。
    """
    s = str(slug or "").strip().lower()
    for suf in ("_set", "_blueprint"):
        if s.endswith(suf) and len(s) > len(suf):
            s = s[: -len(suf)]
    parts = [p for p in s.split("_") if p]
    while parts and matching.strip_variant_norm(parts[0]) == "":
        parts.pop(0)                      # 词头变体：kuva_ / prisma_ / mk1_ …
    while parts and matching.strip_variant_norm(parts[-1]) == "":
        parts.pop()                       # 词尾变体：_prime / _wraith / _vandal …
    return "_".join(parts) or s


def wiki_title(name: str) -> str:
    """灰机 wiki 页面标题归一（2026-09-25 浏览器实测，API 批量核对）。

    含中文的名字**去掉空格与中点**：``玻之武杖 Prime`` → ``玻之武杖Prime``
    （带空格就是「本页面不存在」）、``赤毒·布拉玛`` → ``赤毒布拉玛``、
    ``猎人 战备`` → ``猎人战备``、``Mesa 的华尔兹`` → ``Mesa的华尔兹``；
    **纯拉丁名保持原样**（``Nekros Prime`` / ``Excalibur Umbra`` 页面就带空格，
    去掉反而 404）；连字符不动（``MK1-布莱顿`` 是页面名）。
    """
    if not name or not _CJK.search(name):
        return name
    return re.sub(r"[\s\u3000·・]+", "", name)


def page_name_from_slug(slug: str) -> str:
    """WM slug → wiki 页面名（**国际服口径**）。

    ``nekros_prime_set`` → ``Nekros Prime``（DE 简中不翻译战甲名，直接用英文）；
    ``torid`` → ``托里德``；``fleeting_expertise`` → ``弹指瞬技``。
    先剥 ``_set`` / ``_blueprint``（套装/蓝图页与本体同页），再查官方对照表，
    表里没有（= 未翻译）就用英文原名。
    """
    s = str(slug or "").strip().lower()
    if not s:
        return ""
    cands: list[str] = []
    for suf in ("_set", "_blueprint"):
        if s.endswith(suf) and len(s) > len(suf):
            cands.append(s[: -len(suf)])
    cands.append(s)
    table = _official_zh_by_en()
    for c in cands:
        en = _title_en(c.replace("_", " "))
        zh = table.get(_norm(en))
        if zh:
            return zh
    return _title_en(cands[0].replace("_", " "))


def wiki_page_name(hit: dict, base: bool = False) -> str:
    """检索命中 → 灰机 wiki 页面名（**国际服口径**）。

    DE 官方简中**不翻译战甲名**（Nekros / Banshee / Volt 直接用英文），
    只翻译武器 / MOD / 赋能等（Torid → 托里德、Fleeting Expertise →
    弹指瞬技）；国服旧译（御魂主宰 / 恸哭女妖）与社区黑话拼进
    ``/wiki/<名>`` 都是死链（2026-09-24 用户实测：搜「摸尸」给出
    「御魂主宰」，wiki 无此页）。所以：

    1. 别名命中 → 按 slug 落官方名（未翻译即英文原名）；
    2. 官方 / 掉落表命中 → 英文名查官方对照表；
    3. WM 展示名剥掉「一套 / 蓝图」等后缀再试；
    4. 都查不到时原样回落，绝不返回空串。

    Args:
        base: 查询**没有**指明变体（p/prime/亡魂…）时置 True —— 页面名落到
            基体（``nekros_prime_set`` → ``Nekros``），变体由调用方另列
            （2026-09-24 用户口径：没指明就只介绍基础的）。
    """
    name = (hit or {}).get("name") or ""
    if not name:
        return name
    # 1) 别名表反查 slug（黑话 / 中文名 → 官方名；任何来源都先试，
    #    官方条目里也有 en 字段是内部名（如 rifle）的脏行，不能先信 en）
    slug = _alias_slug_index().get(_norm(name))
    if slug:
        return page_name_from_slug(base_slug(slug) if base else slug)
    # 2) 别名条目但反查不到：en 是 slug 转写形态（banshee prime set）
    if (hit or {}).get("source") == "别名":
        en = (hit.get("en") or "").strip()
        if en and not _CJK.search(en):
            s = en.replace(" ", "_")
            return page_name_from_slug(base_slug(s) if base else s)
        return name
    # 3) 官方条目：展示名本身就是 DE 官方简中名
    if (hit or {}).get("source") == "官方":
        return name
    # 4) 英文名（掉落表 / WM 展示名）：剥中文后缀 → 查官方对照表
    table = _official_zh_by_en()
    cand = name
    for suf in _ZH_SUFFIXES:
        if cand.endswith(suf):
            cand = cand[: -len(suf)]
    for c in (cand, hit.get("en") or ""):
        if not c:
            continue
        for s in (c, re.sub(r"(?i)\s+(blueprint|set)$", "", c)):
            zh = table.get(_norm(s))
            if zh:
                return zh
    return cand or name
