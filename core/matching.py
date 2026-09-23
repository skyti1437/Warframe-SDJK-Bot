# -*- coding: utf-8 -*-
"""武器/物品名统一归一化与变体别名解析（2026-09-23，用户变体解析需求）。

设计铁律（用户执行注意 ①）：
1. 只做「归一化 + 变体 token 等价展开」，**绝不剥掉变体 token 去配基础武器**——
   紫卡倾向/紫卡类型按变体分别计算，「赤毒沙皇」绝不能落到「沙皇」上；
2. 变体展开只在「归一化完全匹配」之后的层级启用，精确/归一化完全命中时不展开；
3. 纯函数、零第三方依赖、不 import astrbot / api_client——离线脚本与单测直接用。

与既有归一化的关系：
* ``damage_calc._norm`` 同族（去空格/分隔符 + 小写），但本模块额外覆盖全角空格
  与更多分隔符，且**不抹 prime**；
* ``api_client.norm_wm_name`` 是 WM 交易路径专用（会抹独立成词的 prime 以支持
  「省略 Prime 也能查」），语义不同，互不替代——WM 路径本轮不动。
"""
from __future__ import annotations

import difflib
import re
from typing import Optional

# 分隔符：半/全角空格、中点族、连字符族、下划线、引号撇
_SEPS = re.compile(r"[\s\u3000·・•\-_—―'’]+")
# 后缀缩写 p/P/p版/P版：★ 仅当前一个字符是 CJK 时才算 Prime 缩写，
# 避免把英文词尾的 p 误当缩写（Grineer 系武器名等）。
_P_ABBREV = re.compile(r"([\u4e00-\u9fff])[pP](?:版)?$")
# 明写的词尾 prime（CJK 后可带空格/中点）：「绝路 Prime」「绝路prime」
_PRIME_WORD = re.compile(r"([\u4e00-\u9fff])[\s·・]*prime$", re.IGNORECASE)

# 前缀变体表：官方简中 ↔ 英文词头（WM slug / 英文名里是英文侧）
# ★ 棱晶/棱镜 双收：DE 官方简中是「棱晶·」（2026-09-23 数据实测 12:0），
#   「棱镜」为社区常用写法，两者都要能解析。
VARIANT_TOKENS: dict[str, str] = {
    "赤毒": "kuva", "信条": "tenet", "终幕": "coda", "亡魂": "wraith",
    "破坏者": "vandal", "棱晶": "prisma", "棱镜": "prisma", "圣洁": "sancti",
    "安全": "secura", "终焉": "telos", "共生": "synoid", "血光": "rakta",
    "瓦伊科": "vaykor", "玛拉": "mara", "绯红": "carmine",
}
# 中英同形/缩写自成一类的独立 token
_STANDALONE = {"dex", "mk1", "prime"}
_EN_TOKENS = set(VARIANT_TOKENS.values()) | _STANDALONE


def normalize(s: str) -> str:
    """匹配归一化：去空白(含全角 U+3000)/分隔符 + 小写。**不抹 prime**。"""
    return _SEPS.sub("", (s or "")).lower()


def _en_to_zh(tok: str) -> Optional[str]:
    for zh, en in VARIANT_TOKENS.items():
        if tok == en:
            return zh
    return tok if tok in _STANDALONE else None


def expand_variants(raw: str) -> list[str]:
    """把用户输入展开为**归一化后**的等价形态列表（按优先级排序）。

    覆盖的变形（真实数据形态依据 2026-09-23 WM /riven/weapons 实测：
    变体中文名是「赤毒·布拉玛」这类**中点**写法，归一化层即可吃掉）：
    * 后缀缩写：``绝路p`` → ``绝路prime``（仅 CJK 后的 p/P/p版/P版）；
    * 明写 prime：``绝路 Prime`` / ``绝路prime`` 与缩写同待遇；
    * 语序互换：``沙皇赤毒`` ↔ ``赤毒沙皇``（变体 token 在首或在尾）；
    * zh↔en token：``kuva沙皇`` → ``赤毒沙皇``（英文词头换中文前缀）。

    ★ 只做**等价**变形，绝不产出「去掉变体 token」的形态——倾向/紫卡
    按变体分别计算，「绝路p」不是「绝路」（0.95）、「赤毒沙皇」不是「沙皇」。
    表中确无该变体条目时就地未找到（2026-09-23 用户指正后撤销回落 base
    的旧行为；倾向表已由 dispositions_rivenmirror.json 补全 199 条变体）。
    """
    s = (raw or "").strip()
    if not s:
        return []
    forms = [normalize(s)]

    m = _P_ABBREV.search(s) or _PRIME_WORD.search(s)
    if m:
        base = s[:m.start(1)] + m.group(1)
        forms.append(normalize(base + "prime"))
        forms.append(normalize("prime" + base))

    low = normalize(s)
    tok_zh = tok_en = ""
    rest = ""
    for zh, en in VARIANT_TOKENS.items():
        if low.startswith(zh) and len(low) > len(zh):
            tok_zh, tok_en, rest = zh, en, low[len(zh):]
            break
        if low.endswith(zh) and len(low) > len(zh):
            tok_zh, tok_en, rest = zh, en, low[:-len(zh)]
            break
    if not tok_zh:
        for en in sorted(_EN_TOKENS, key=len, reverse=True):
            if low.startswith(en) and len(low) > len(en):
                zh = _en_to_zh(en)
                if zh:
                    tok_zh, tok_en, rest = zh, en, low[len(en):]
                    break
    if tok_zh and rest:
        forms.append(normalize(tok_zh + rest))       # 沙皇赤毒 → 赤毒沙皇
        forms.append(normalize(rest + tok_zh))       # 赤毒沙皇 → 沙皇赤毒
        forms.append(normalize(rest + tok_en))       # 赤毒沙皇 → 沙皇kuva
        forms.append(normalize(tok_en + rest))       # 赤毒沙皇 → kuva沙皇

    return list(dict.fromkeys(f for f in forms if f))


def prime_sibling(base: dict, entries: list, *, zh: str = "zh", en: str = "en"):
    """在 entries 里找 base 的 Prime 版（归一化后 zh/en 恰为 base+prime）。"""
    bz = normalize(base.get(zh) or "")
    be = normalize(base.get(en) or "")
    for e in entries:
        ez = normalize(e.get(zh) or "")
        ee = normalize(e.get(en) or "")
        if (ez and ez == bz + "prime") or (ee and ee == be + "prime"):
            return e
    return None


def resolve_weapon_name(query: str, entries: list, *,
                        zh: str = "zh", en: str = "en",
                        slug: str = "url_name") -> tuple[list, str]:
    """五级优先解析，返回 (hits, stage)。

    1. ``exact``      官方中文名原样完全相等；
    2. ``normalized`` 归一化（去空格/分隔符/大小写）后 zh 或 en 完全相等；
    3. ``variant``    变体展开等价（p→prime / 语序互换 / zh↔en token）；
    4. ``substring``  归一化子串（query ⊆ entry，≥2 字防误配；多命中全返回）；
    5. ``fuzzy``      difflib 对**中文名**（不是英文 slug），cutoff 0.6，取前 3。

    hits 为 entries 的保序子集；无命中返回 ``([], "")``。
    """
    q = (query or "").strip()
    if not q:
        return [], ""
    hits = [e for e in entries if e.get(zh) == q]
    if hits:
        return hits, "exact"

    nq = normalize(q)
    if nq:
        hits = [e for e in entries
                if nq in (normalize(e.get(zh) or ""), normalize(e.get(en) or ""))]
        if hits:
            return hits, "normalized"

        # 变体等价——按 expand_variants 的优先级顺序逐形态尝试：
        # prime 形态在前、p/prime 缩写的「回落 base」形态垫底（表里没有
        # Prime 条目时按口语习惯给 base 的倾向，如「绝路p」）。
        for form in expand_variants(q)[1:]:
            hits = [e for e in entries
                    if form in (normalize(e.get(zh) or ""),
                                normalize(e.get(en) or ""))]
            if hits:
                return hits, "variant"

        if len(nq) >= 2:
            hits = [e for e in entries
                    if nq in normalize(e.get(zh) or "")
                    or nq in normalize(e.get(en) or "")]
            if hits:
                return hits, "substring"

        # prime 缩写的「截短」形态（守望p → 守望者 Prime）：base 段不是完整
        # 武器名而是前缀缩写时，按 zh/en 前缀找同族 → 取各族 Prime 版。
        # 仅在显式 prime 意图下启用；多族全列（调用方给中文候选）。
        m = _P_ABBREV.search(q) or _PRIME_WORD.search(q)
        if m:
            stem = normalize(q[:m.start(1)] + m.group(1))
            if stem:
                fam_zh = [e for e in entries
                          if normalize(e.get(zh) or "").startswith(stem)]
                fam_en = [e for e in entries
                          if normalize(e.get(en) or "").startswith(stem)
                          and e not in fam_zh]
                primes = []
                seen_u = set()
                for e in fam_zh + fam_en:
                    p = prime_sibling(e, entries, zh=zh, en=en) or e
                    u = p.get("url_name") or ""
                    if "prime" not in normalize(p.get(zh) or p.get(en) or ""):
                        continue          # 该族没有 Prime 条目 → 不强推本体
                    if u not in seen_u:
                        seen_u.add(u)
                        primes.append(p)
                if primes:
                    return primes, "prime_prefix"

    # ★ 含变体前缀（赤毒/信条/…）或 prime 后缀（x p / x prime）的查询禁用
    #   模糊层：变体倾向与本体不同，意图明确的变体查询宁可「未找到+候选」，
    #   绝不能被 fuzzy 吞成 base 卡（2026-09-23 用户指正：绝路p ≠ 0.95）。
    explicit_variant = (variant_intent(q)
                        or any(t in nq for t in VARIANT_TOKENS))
    if not explicit_variant:
        named = [(e, normalize(e.get(zh) or e.get(en) or "")) for e in entries]
        pool = [n for _, n in named if n]
        close = difflib.get_close_matches(nq, pool, n=3, cutoff=0.6)
        if close:
            want = set(close)
            hits = [e for e, n in named if n in want]
            order = {n: i for i, n in enumerate(close)}
            hits.sort(key=lambda e: order.get(
                normalize(e.get(zh) or e.get(en) or ""), 99))
            return hits[:3], "fuzzy"
    return [], ""


def variant_intent(raw: str) -> bool:
    """查询是否带显式变体意图（p/P/P版/prime 后缀）。"""
    return bool(_P_ABBREV.search(raw or "") or _PRIME_WORD.search(raw or ""))


def strip_variant_norm(norm: str) -> str:
    """从归一化名里剥掉变体 token（zh 前缀 + en 词头 + prime 后缀）→ 基础名。"""
    norm = norm or ""
    changed = True
    while changed and norm:
        changed = False
        for zh in VARIANT_TOKENS:
            if norm.startswith(zh):
                norm = norm[len(zh):]
                changed = True
                break
        for en in _EN_TOKENS:
            if norm.startswith(en):
                norm = norm[len(en):]
                changed = True
                break
        if norm.endswith("prime"):
            norm = norm[:-5]
            changed = True
    return norm


def family_of(entry: dict, entries: list, *, zh: str = "zh",
              en: str = "en") -> list:
    """本体 → 全变体家族（含本体，本体排最前，其余按中文名）。

    倾向指令「只报本体名就列出全部变体」用的纯函数；只收 disposition
    非空的条目。判定：归一化名剥掉变体 token 后同基名即同族。
    """
    bz = strip_variant_norm(normalize(entry.get(zh) or ""))
    be = strip_variant_norm(normalize(entry.get(en) or ""))
    fam = []
    for e in entries:
        if e.get("disposition") is None:
            continue
        nz, ne = normalize(e.get(zh) or ""), normalize(e.get(en) or "")
        if (bz and strip_variant_norm(nz) == bz) or \
                (be and strip_variant_norm(ne) == be):
            fam.append(e)

    def _is_base(e):
        nz = normalize(e.get(zh) or "")
        return nz == bz or (nz and not variant_intent(nz)
                            and not any(t in nz for t in VARIANT_TOKENS))

    fam.sort(key=lambda e: (0 if _is_base(e) else 1,
                            normalize(e.get(zh) or "")))
    return fam


def zh_names(hits: list, *, zh: str = "zh", en: str = "en",
             slug: str = "url_name", n: int = 3) -> list[str]:
    """多命中时的中文候选文案（官方中文名 → 英文名 → slug 兜底）。"""
    out = []
    for e in hits[:n]:
        out.append(e.get(zh) or e.get(en) or e.get(slug) or "?")
    return out


def suggest_zh(query: str, entries: list, *, zh: str = "zh", en: str = "en",
               n: int = 3, cutoff: float = 0.5) -> list[str]:
    """「未找到」时的中文名候选（模糊对**中文名**，宽 cutoff；不作命中依据）。"""
    nq = normalize(query)
    if not nq:
        return []
    pool = {normalize(e.get(zh) or ""): (e.get(zh) or e.get(en))
            for e in entries if (e.get(zh) or e.get(en))}
    close = difflib.get_close_matches(nq, list(pool), n=n, cutoff=cutoff)
    return [pool[c] for c in close if pool.get(c)]
