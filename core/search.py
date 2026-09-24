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

_HERE = Path(__file__).resolve().parent
_DATA = _HERE / "data"
_ALIAS_FILE = _DATA / "aliases.json"
_DE_ITEMS_FILE = _DATA / "de" / "de_items_zh.json"
_MOD_NAMES_FILE = _DATA / "de" / "mod_names_zh.json"
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


# 变体/套装后缀（WM slug 转写形态 + WM 展示名里的中文后缀）
_VARIANT_WORDS = {"set", "prime", "wraith", "vandal", "kuva", "prisma"}
_ZH_SUFFIXES = (" 一套", " 蓝图", " 机体", " 系统", " 头部神经光元", " 头部", " 配件")
_PRIME_CN_PREFIX = "圣装"


def _strip_variant(slug: str) -> str:
    """slug 剥变体后缀：banshee_prime_set → banshee（用于回落到基体名）。"""
    out = slug
    changed = True
    while changed:
        changed = False
        for suf in ("_prime_set", "_set", "_prime"):
            if out.endswith(suf) and len(out) > len(suf):
                out = out[: -len(suf)]
                changed = True
    return out


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
def _official_mod_names() -> dict[str, str]:
    """官方简中 MOD 名：归一化名 → 原始写法（de/mod_names_zh.json 名字列表）。

    别名键是玩家手打的中文名（瞬时狡诈 / 持久力Prime），与官方写法
    （弹指瞬技 / 持久力 Prime，Prime 前带空格）常差在措辞或空格上；
    拼 wiki 页面名时以官方写法为准。战甲 / 武器不在表内，仍走
    「最长键」规则。
    """
    try:
        data = json.loads(_MOD_NAMES_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, list):
        return {}
    out: dict[str, str] = {}
    for n in data:
        if isinstance(n, str) and n:
            # 空格一并归一：「持久力 Prime」与手打的「持久力Prime」视为同名
            out.setdefault(_norm(n).replace(" ", ""), n)
    return out


@lru_cache(maxsize=1)
def _official_cn_by_slug() -> dict[str, str]:
    """slug / 基体名 → 官方简中名（别名表里同一 slug 的最优中文键）。

    同一 slug 常挂多个中文键（女妖 / 音妈 / 恸哭女妖 / 圣装恸哭女妖），
    页面名按「官方 MOD 名表命中 > 键最长」挑一条；命中官方名的键直接
    采用官方**原始写法**（弹指瞬技 / 持久力 Prime），同时修掉黑话措辞
    与「Prime 缺空格」两处死链源。Prime 形态的「圣装」前缀在查页面时
    剥掉（基体页面覆盖同一套技能，且基体页一定存在）。
    """
    mods = _official_mod_names()
    best: dict[str, tuple[bool, int, str]] = {}
    for sec in ("wm_items", "riven_items"):
        for key, slug in (_jload(_ALIAS_FILE).get(sec) or {}).items():
            if not slug or not _CJK.search(key):
                continue
            official = mods.get(_norm(key).replace(" ", ""))
            rank = (bool(official), len(key))
            cur = best.get(slug)
            if cur is None or rank > cur[:2]:
                best[slug] = (rank[0], rank[1], official or key)
    out: dict[str, str] = {}
    for slug, (_, _, name) in best.items():
        cn = name[len(_PRIME_CN_PREFIX):] if name.startswith(_PRIME_CN_PREFIX) \
            else name
        out[slug] = cn
        out.setdefault(_strip_variant(slug), cn)
    return out


def wiki_page_name(hit: dict) -> str:
    """检索命中 → 灰机 wiki 页面名（**别名不是页面名，官方简中才是**）。

    「wiki 音妈」这类黑话查询命中的是别名条目，展示名就是别名键本身，
    直接拼 ``/wiki/音妈`` 是死链（2026-09-24 用户反馈）。这里按别名条目的
    目标 slug（``banshee prime set`` → ``banshee_prime_set``）在别名表里取
    官方简中名（恸哭女妖）当页面名；WM 展示名里的「 一套/蓝图」后缀同样
    先剥掉再归一到官方名。查不到时原样回落，绝不返回空串。
    """
    name = (hit or {}).get("name") or ""
    if not name:
        return name
    by_slug = _official_cn_by_slug()
    if (hit or {}).get("source") == "别名":
        slug = _alias_slug_index().get(_norm(name)) or \
            (hit.get("en") or "").strip().replace(" ", "_")
        if slug:
            cn = by_slug.get(slug) or by_slug.get(_strip_variant(slug))
            if cn:
                return cn
    cand = name
    for suf in _ZH_SUFFIXES:
        if cand.endswith(suf):
            cand = cand[: -len(suf)]
    parts = cand.split()
    while len(parts) > 1 and parts[-1].lower() in _VARIANT_WORDS:
        parts.pop()
    cand = " ".join(parts)
    if not cand or cand == name:
        return name
    cn = by_slug.get(cand.lower().replace(" ", "_"))
    if cn:
        return cn
    for h in search(cand, limit=5):
        if h.get("source") == "官方" and h.get("name"):
            return h["name"]
    return name
