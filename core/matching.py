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
import json
import re
import unicodedata
from typing import Optional

# 分隔符：半/全角空格、中点族、连字符族、下划线、引号撇
_SEPS = re.compile(r"[\s\u3000·・•\-_—―'’]+")
# 后缀缩写 p/P/p版/P版：★ 仅当前一个字符是 CJK 时才算 Prime 缩写，
# 避免把英文词尾的 p 误当缩写（Grineer 系武器名等）。
_P_ABBREV = re.compile(r"([\u4e00-\u9fff])[pP](?:版)?$")
# 明写的词尾 prime（CJK 后可带空格/中点）：「绝路 Prime」「绝路prime」
_PRIME_WORD = re.compile(r"([\u4e00-\u9fff])[\s·・]*prime$", re.IGNORECASE)
# 整串含 CJK 时额外认「拉丁词尾 + p」（「驱逐 Grineerp」这类混排查询）；
# 纯拉丁查询不启用（保护 rifle amp / cold snap 这类英文词尾 p）。
_HAS_CJK = re.compile(r"[\u4e00-\u9fff]")
_LATIN_P = re.compile(r"([A-Za-z])[pP](?:版)?$")
# 空格分隔的 p / p版（「Gara p」「绝路 p版」）
_SPACE_P = re.compile(r"[\s·・]+[pP](?:版)?$")


def prime_base(q: str) -> Optional[str]:
    """带 Prime 后缀的查询 → 基名；否则 None。**共享口径，勿再各写正则。**

    认四种形态（2026-10-05 抽出，api_client 的 Prime 意图前置与 match 家族共用）：
      · CJK + ``p`` / ``P`` / ``p版`` / ``P版``（``_P_ABBREV``，前一字必须 CJK）
      · CJK + ``prime`` 词（``_PRIME_WORD``）
      · **整串含 CJK** 且以「拉丁字母 + p/p版」结尾（``_LATIN_P``）——
        「驱逐 Grineerp」「格拉姆 Gramp」这类混排查询；纯拉丁查询不启用
      · 空格分隔的 ``p`` / ``p版``（``_SPACE_P``）——「Gara p」「绝路 p版」
    ★ 拉丁词尾 p（``rifle amp`` / ``cold snap``）**不**算 Prime 缩写；
      拉丁名 + p 连写（``Garap``）由别名/归一化链路处理，不走本函数。
    """
    s = (q or "").strip()
    if not s:
        return None
    m = _P_ABBREV.search(s) or _PRIME_WORD.search(s)
    if m:
        base = (s[: m.start(1)] + m.group(1)).strip()
        return base or None
    if _HAS_CJK.search(s):
        m2 = _LATIN_P.search(s)
        if m2:
            base = (s[: m2.start(1)] + m2.group(1)).strip()
            return base or None
    m3 = _SPACE_P.search(s)
    if m3:
        base = s[: m3.start()].strip()
        return base or None
    return None


# 前缀变体表：官方简中 ↔ 英文词头（WM slug / 英文名里是英文侧）
# ★ 棱晶/棱镜 双收：DE 官方简中是「棱晶·」（2026-09-23 数据实测 12:0），
#   「棱镜」为社区常用写法，两者都要能解析。
VARIANT_TOKENS: dict[str, str] = {
    "赤毒": "kuva",
    "信条": "tenet",
    "终幕": "coda",
    "亡魂": "wraith",
    "破坏者": "vandal",
    "棱晶": "prisma",
    "棱镜": "prisma",
    "圣洁": "sancti",
    # ★ 2026-10-03 与**游戏内官方简中**对齐（用本地三表逐条反查而来：
    #   保障·勒克塔 / 枢议·咖玛腕甲枪 / 终极·螺钉步枪 / 勇气·海克 /
    #   天仓·悲痛之刃 / 苦痛·德特昂 / 嫣红·潘塔）。
    "保障": "secura",
    "枢议": "synoid",
    "终极": "telos",
    "勇气": "vaykor",
    "天仓": "ceti",
    "苦痛": "mara",
    "嫣红": "carmine",
    # 旧译 / 社区写法保留（互为别名：剥词与展开都受益）
    "安全": "secura",
    "共生": "synoid",
    "终焉": "telos",
    "瓦伊科": "vaykor",
    "玛拉": "mara",
    # ★ 修正：「绯红」按官方简中是 **Rakta** 的前缀（绯红·西诺斯 = Rakta Cernos、
    #   绯红·暗黑匕首 = Rakta Dark Dagger），不是 Carmine —— Carmine 官方是「嫣红」。
    #   旧表把 carmine 挂在绯红上，会让「绯红·X」展开出错误的变体形态。
    "绯红": "rakta",
    "血光": "rakta",
}
# 中英同形/缩写自成一类的独立 token
_STANDALONE = {"dex", "mk1", "prime"}
_EN_TOKENS = set(VARIANT_TOKENS.values()) | _STANDALONE


def normalize(s: str) -> str:
    """匹配归一化：NFKC 全半角 + 去空白(含全角 U+3000)/分隔符 + 小写。**不抹 prime**。

    ★ 2026-10-10：补 NFKC —— DE 官方简中名用全角括号（「制胜者（近战）」），
      WM/补全表用半角（「制胜者 (近战)」），不折叠就互不相等，落到形近兜底
      配成「制胜者（主要）」（另一形态、另一紫卡类别）。
    """
    return _SEPS.sub("", unicodedata.normalize("NFKC", s or "")).lower()


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
    表中确无该变体条目时就地未找到（2026-09-23 维护者指正后撤销回落 base
    的旧行为；倾向表已由 dispositions_rivenmirror.json 补全 199 条变体）。
    """
    s = (raw or "").strip()
    if not s:
        return []
    forms = [normalize(s)]

    m = _P_ABBREV.search(s) or _PRIME_WORD.search(s)
    if m:
        base = prime_base(s) or ""
        forms.append(normalize(base + "prime"))
        forms.append(normalize("prime" + base))

    low = normalize(s)
    tok_zh = tok_en = ""
    rest = ""
    for zh, en in VARIANT_TOKENS.items():
        if low.startswith(zh) and len(low) > len(zh):
            tok_zh, tok_en, rest = zh, en, low[len(zh) :]
            break
        if low.endswith(zh) and len(low) > len(zh):
            tok_zh, tok_en, rest = zh, en, low[: -len(zh)]
            break
    if not tok_zh:
        for en in sorted(_EN_TOKENS, key=len, reverse=True):
            if low.startswith(en) and len(low) > len(en):
                zh = _en_to_zh(en)
                if zh:
                    tok_zh, tok_en, rest = zh, en, low[len(en) :]
                    break
    if tok_zh and rest:
        forms.append(normalize(tok_zh + rest))  # 沙皇赤毒 → 赤毒沙皇
        forms.append(normalize(rest + tok_zh))  # 赤毒沙皇 → 沙皇赤毒
        forms.append(normalize(rest + tok_en))  # 赤毒沙皇 → 沙皇kuva
        forms.append(normalize(tok_en + rest))  # 赤毒沙皇 → kuva沙皇

    return list(dict.fromkeys(f for f in forms if f))


_NAME_SUFFIXES = ("一套", "组合包", "蓝图", "set", "blueprint", "blueprints")


def _strip_name_suffix(name: str) -> str:
    """归一化 + 逐级剥掉套装/部件后缀（「鹦鹉螺 Prime 一套」→「鹦鹉螺prime」）。

    ★ 2026-09-24：WM 的物品名带后缀，而「Prime 兄弟」判定是纯字符串比对 ——
    不剥后缀时「wm 鹦鹉螺p」找不到 nautilus_prime_set（玩家报障）。
    """
    n = normalize(name or "")
    changed = True
    while changed and n:
        changed = False
        for suf in _NAME_SUFFIXES:
            if n.endswith(suf) and len(n) > len(suf):
                n = n[: -len(suf)]
                changed = True
    return n


def prime_sibling(base: dict, entries: list, *, zh: str = "zh", en: str = "en"):
    """在 entries 里找 base 的 Prime 版（zh/en 归一化剥后缀后恰为 base+prime）。"""
    bz = _strip_name_suffix(base.get(zh) or "")
    be = _strip_name_suffix(base.get(en) or "")
    for e in entries:
        ez = _strip_name_suffix(e.get(zh) or "")
        ee = _strip_name_suffix(e.get(en) or "")
        if (bz and ez == bz + "prime") or (be and ee == be + "prime"):
            return e
    return None


def resolve_weapon_name(
    query: str, entries: list, *, zh: str = "zh", en: str = "en", slug: str = "url_name"
) -> tuple[list, str]:
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
        hits = [
            e for e in entries if nq in (normalize(e.get(zh) or ""), normalize(e.get(en) or ""))
        ]
        if hits:
            return hits, "normalized"

        # 变体等价——按 expand_variants 的优先级顺序逐形态尝试：
        # prime 形态在前、p/prime 缩写的「回落 base」形态垫底（表里没有
        # Prime 条目时按口语习惯给 base 的倾向，如「绝路p」）。
        for form in expand_variants(q)[1:]:
            hits = [
                e
                for e in entries
                if form in (normalize(e.get(zh) or ""), normalize(e.get(en) or ""))
            ]
            if hits:
                return hits, "variant"

        if len(nq) >= 2:
            hits = [
                e
                for e in entries
                if nq in normalize(e.get(zh) or "") or nq in normalize(e.get(en) or "")
            ]
            if hits:
                return hits, "substring"

        # prime 缩写的「截短」形态（守望p → 守望者 Prime）：base 段不是完整
        # 武器名而是前缀缩写时，按 zh/en 前缀找同族 → 取各族 Prime 版。
        # 仅在显式 prime 意图下启用；多族全列（调用方给中文候选）。
        stem = normalize(prime_base(q) or "")
        if stem:
            fam_zh = [e for e in entries if normalize(e.get(zh) or "").startswith(stem)]
            fam_en = [
                e
                for e in entries
                if normalize(e.get(en) or "").startswith(stem) and e not in fam_zh
            ]
            primes = []
            seen_u = set()
            for e in fam_zh + fam_en:
                p = prime_sibling(e, entries, zh=zh, en=en) or e
                u = p.get("url_name") or ""
                if "prime" not in normalize(p.get(zh) or p.get(en) or ""):
                    continue  # 该族没有 Prime 条目 → 不强推本体
                if u not in seen_u:
                    seen_u.add(u)
                    primes.append(p)
            if primes:
                return primes, "prime_prefix"

    # ★ 含变体前缀（赤毒/信条/…）或 prime 后缀（x p / x prime）的查询禁用
    #   模糊层：变体倾向与本体不同，意图明确的变体查询宁可「未找到+候选」，
    #   绝不能被 fuzzy 吞成 base 卡（2026-09-23 维护者指正：绝路p ≠ 0.95）。
    explicit_variant = variant_intent(q) or any(t in nq for t in VARIANT_TOKENS)
    if not explicit_variant:
        named = [(e, normalize(e.get(zh) or e.get(en) or "")) for e in entries]
        pool = [n for _, n in named if n]
        close = difflib.get_close_matches(nq, pool, n=3, cutoff=0.6)
        if close:
            want = set(close)
            hits = [e for e, n in named if n in want]
            order = {n: i for i, n in enumerate(close)}
            hits.sort(key=lambda e: order.get(normalize(e.get(zh) or e.get(en) or ""), 99))
            return hits[:3], "fuzzy"
    return [], ""


def variant_intent(raw: str) -> bool:
    """查询是否带显式变体意图（p/P/P版/prime 后缀）。"""
    return bool(_P_ABBREV.search(raw or "") or _PRIME_WORD.search(raw or ""))


_VARIANT_WORDS_EN = set(VARIANT_TOKENS.values()) | _STANDALONE


def strip_variant_query(raw: str) -> str:
    """剥掉查询尾部的 p/P/p版/P版/prime（本地检索回退用）。

    ``摸尸p`` → ``摸尸``；词库里通常只登记无后缀形态（别名表有 摸尸 没有 摸尸p）。
    """
    s = (raw or "").strip()
    m = _P_ABBREV.search(s) or _PRIME_WORD.search(s)
    if m:
        return (s[: m.start(1)] + m.group(1)).strip()
    return s


def variant_intent_any(raw: str) -> bool:
    """变体意图（**中英通吃**）：中文 p/prime 后缀，或英文变体词。

    ``variant_intent`` 只看「CJK + p/prime」，纯英文查询（``Nekros Prime`` /
    ``Kuva Bramma``）会漏判 —— wiki 指令据此决定「介绍本体 + 列变体」还是
    「直接介绍指定变体」（2026-09-24 用户口径：没指明变体就只介绍基础的）。
    """
    s = (raw or "").strip()
    if not s:
        return False
    if variant_intent(s):
        return True
    low = s.lower()
    for tok in _VARIANT_WORDS_EN:
        if re.search(rf"(?<![a-z]){re.escape(tok)}(?![a-z])", low):
            return True
    return False


_VARIANT_SUFFIX_TOKENS = tuple(VARIANT_TOKENS) + tuple(sorted(_EN_TOKENS))


def strip_variant_norm(norm: str) -> str:
    """从归一化名里剥掉变体 token（zh/en 的**前缀与后缀** + prime 后缀）→ 基础名。

    ★ 2026-09-27：中文变体词在 WM 数据里是**后缀**写法（「布莱顿·破坏者」
    /「格雷戈·亡魂」），旧版只剥前缀 ⇒ 永远剥不出「布莱顿」/「格雷戈」，
    倾向卡的家族展开因此**系统性漏掉全部 Vandal / Wraith 变体**（玩家报障：
    「紫卡倾向 布莱顿」少列布莱顿·破坏者）。`expand_variants()` 早已按前后缀
    两种写法处理，这里补齐同一口径。

    ⚠️ 前缀循环**不加长度保护**：``strip_variant_norm("亡魂") == ""`` 是
    `core/search.py` 用来削掉输入里纯变体词的判据，动了会连带改到 wiki 检索。
    后缀循环必须加长度保护（否则同样会把「亡魂」削成空串）。
    ⚠️ 后缀保护只在**长度大于 token** 时才削：`family_of` 还需本体名非空
    （见其 ``bz and …`` 守卫），留着 token 本身不会误合并。
    """
    norm = norm or ""
    changed = True
    while changed and norm:
        changed = False
        for zh in VARIANT_TOKENS:
            if norm.startswith(zh):
                norm = norm[len(zh) :]
                changed = True
                break
        for en in _EN_TOKENS:
            if norm.startswith(en):
                norm = norm[len(en) :]
                changed = True
                break
        if norm.endswith("prime"):
            norm = norm[:-5]
            changed = True
        if changed:
            continue
        for tok in _VARIANT_SUFFIX_TOKENS:  # 后缀写法：布莱顿·破坏者 / Braton Vandal
            if len(norm) > len(tok) and norm.endswith(tok):
                norm = norm[: -len(tok)]
                changed = True
                break
    return norm


# ---------------------------------------------------------------------------
# ★ 2026-10-02：紫卡家族判定改用 DE 官方 `parentName` 谱系
#   （玩家报障「紫卡倾向 盗贼」多列 `Dex 盗贼双枪`：官方数据里 `Dex Furis`
#   的 parentName 指向 **Afuris**，而旧「中文/英文主干相等」双分支把它同时
#   并进了 Furis 族）。判据唯一权威 = core/data/de/riven_families.json
#   （DE 官方导出 ExportWeapons.json 沿 parentName 上溯，仅当 parent 本身可上
#   紫卡时继续爬；+ Hek / Dark Dagger 两条注明理由的人工补丁）。
#   ★ 中文**永不参与判定**（只用于显示）；名字包含/子串/主干相等**都不得**
#   再作为主判据 —— 那是本次两个 bug 的共同成因。
# ---------------------------------------------------------------------------
_RIVEN_TABLE: Optional[dict] = None  # by_name：英文名 → 家族根
_RIVEN_INDEX: Optional[tuple] = None  # (exact, low, norm) 三级查找索引
_RIVEN_TABLE_STAMP: Optional[tuple] = None  # 表文件 (mtime_ns, size)，变更即失效

# WM 套装/部件条目的英文尾缀（「Okina Prime Set」→「Okina Prime」；
# 与 api_client 旧 _SET_SUFFIXES 的英文侧同口径，中文侧由显示层处理）
_SET_SUFFIXES_EN = (" set", " blueprint", " blueprints")


def _riven_families_stamp() -> Optional[tuple]:
    """表文件的 (mtime_ns, size)；stat 不到返回 None（不触发失效）。"""
    try:
        from . import paths

        st = paths.read_path("de/riven_families.json").stat()
        return (st.st_mtime_ns, st.st_size)
    except Exception:  # noqa: BLE001 - 文件缺失/不可读：保持现状
        return None


def _invalidate_riven_table_if_changed() -> None:
    """★ mtime 护栏（2026-10-03 线上事故）：模块级缓存与磁盘解耦。

    插件热重载偶发清不掉本模块（AstrBot 按 sys.modules 前缀 purge，异常
    序列下旧模块对象幸存时，其全局缓存会一直拿着**旧表**）——线上表现：
    组合枪双模式家族候选为空、用户卡被误判「老卡」。每次调用 stat 一次
    文件，变了就把表和索引一起作废，下次取数重读磁盘。
    """
    global _RIVEN_TABLE, _RIVEN_INDEX, _RIVEN_TABLE_STAMP
    stamp = _riven_families_stamp()
    if stamp is not None and stamp != _RIVEN_TABLE_STAMP:
        _RIVEN_TABLE = None
        _RIVEN_INDEX = None
        _RIVEN_TABLE_STAMP = stamp


def _load_riven_families() -> dict:
    """惰性加载官方家族表（by_name）。表缺失/损坏 ⇒ 空表（全部走主干兜底）。

    实际（重）加载时打一条 INFO（含条数指纹）—— 线上排障时从日志就能
    看出进程里这张表是哪个版本、热重载后有没有重读磁盘。
    """
    global _RIVEN_TABLE
    _invalidate_riven_table_if_changed()
    if _RIVEN_TABLE is None:
        raw: dict = {}
        try:
            from . import paths

            raw = json.loads(paths.read_path("de/riven_families.json").read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - 表缺失退回主干判定，不阻断查询
            raw = {}
        _RIVEN_TABLE = raw.get("by_name") or {}
        from .logging_compat import logger

        logger.info("[sdjk] 紫卡家族表已加载：by_name %d 条", len(_RIVEN_TABLE))
    return _RIVEN_TABLE


def _riven_index() -> tuple:
    """(exact, low, norm) 三级索引：容忍大小写、空格/连字符写法差异。"""
    global _RIVEN_INDEX
    _invalidate_riven_table_if_changed()
    if _RIVEN_INDEX is None:
        tbl = _load_riven_families()
        _RIVEN_INDEX = (
            dict(tbl),
            {k.lower(): v for k, v in tbl.items()},
            {normalize(k): v for k, v in tbl.items()},
        )
    return _RIVEN_INDEX


def family_key(en: str) -> str:
    """英文名 → 家族根（DE 官方 parentName 谱系优先；查不到退回英文主干）。

    取值域与比较语义（调用方只应比较两个 family_key 的**返回值是否相等**）：
      ① 命中官方表（含剥套装尾缀 / 大小写 / 空格连字符归一化）→ 官方家族根；
      ② 未命中 ⇒ 先剥变体词再看表（表快照落后于新武器发布时，避免把
         「新变体」漏在家族外 —— 官方表结果永远压过名字推断）；
      ③ 仍未命中 ⇒ 返回英文主干（两侧都查不到表时互相比对）；
      ④ 英文为空（或主干为空）⇒ 返回 ""，调用方据此**不入家族**
         （宁可少列，不可错列）。
    中文永不参与判定。
    """
    s = (en or "").strip()
    if not s:
        return ""
    low = s.lower()
    for suf in _SET_SUFFIXES_EN:  # 「Okina Prime Set」→「Okina Prime」
        if low.endswith(suf) and len(s) > len(suf):
            s = s[: -len(suf)].strip()
            break
    exact, low_i, norm = _riven_index()
    k = exact.get(s) or low_i.get(s.lower()) or norm.get(normalize(s))
    if k:
        return k
    stem = strip_variant_norm(normalize(s))
    if stem and stem != normalize(s):
        k = norm.get(stem)  # ② 剥变体词后命中官方表
        if k:
            return k
    return stem  # ③ 主干（可能为空 ⇒ 不入家族）


def family_of(entry: dict, entries: list, *, zh: str = "zh", en: str = "en") -> list:
    """本体 → 全变体家族（含本体，本体排最前，其余按中文名）。

    倾向指令「只报本体名就列出全部变体」用的纯函数；只收 disposition
    非空的条目。

    ★ 2026-10-02 判据换成 `family_key`（见上）：官方 parentName 谱系相等
    才是同族；中文只用于**显示/排序**，英文为空则不入家族。
    """
    bz = strip_variant_norm(normalize(entry.get(zh) or ""))  # ★ 仅排序用
    bk = family_key(entry.get(en) or "")
    fam = []
    for e in entries:
        if e.get("disposition") is None:
            continue
        if bk and family_key(e.get(en) or "") == bk:
            fam.append(e)

    # ★ 2026-09-27：同一件武器在数据里可能有两行（url_name 相同：一行字段全但
    #   zh 空、另一行只有 zh），旧行为会把「MK1-Braton」与「MK1-布莱顿」并排
    #   列成两行。按 url_name 去重，优先保留**有中文名**的那行。
    uniq: dict = {}
    for e in fam:
        key = e.get("url_name") or normalize(e.get(zh) or e.get(en) or "")
        cur = uniq.get(key)
        if cur is None or (not (cur.get(zh) or "").strip() and (e.get(zh) or "").strip()):
            uniq[key] = e
    fam = list(uniq.values())

    def _is_base(e):
        nz = normalize(e.get(zh) or "")
        return nz == bz or (
            nz and not variant_intent(nz) and not any(t in nz for t in VARIANT_TOKENS)
        )

    fam.sort(key=lambda e: (0 if _is_base(e) else 1, normalize(e.get(zh) or "")))
    return fam


def zh_names(
    hits: list, *, zh: str = "zh", en: str = "en", slug: str = "url_name", n: int = 3
) -> list[str]:
    """多命中时的中文候选文案（官方中文名 → 英文名 → slug 兜底）。"""
    out = []
    for e in hits[:n]:
        out.append(e.get(zh) or e.get(en) or e.get(slug) or "?")
    return out


def suggest_zh(
    query: str, entries: list, *, zh: str = "zh", en: str = "en", n: int = 3, cutoff: float = 0.5
) -> list[str]:
    """「未找到」时的中文名候选（模糊对**中文名**，宽 cutoff；不作命中依据）。"""
    nq = normalize(query)
    if not nq:
        return []
    pool = {
        normalize(e.get(zh) or ""): (e.get(zh) or e.get(en))
        for e in entries
        if (e.get(zh) or e.get(en))
    }
    close = difflib.get_close_matches(nq, list(pool), n=n, cutoff=cutoff)
    return [pool[c] for c in close if pool.get(c)]


# ---------------------------------------------------------------------------
# ★ C1/B1（2026-10-03）：名称归一化与双语对照（**共用入口**）
#   线上实证：`部件 阿索代prime` 未找到（库里是「阿索代 Prime」）—— 按名查表
#   的指令对大小写/空格敏感。此后 部件 / wiki / 翻译 等一律先过 normalize_name。
# ---------------------------------------------------------------------------
def normalize_name(s: str) -> str:
    """名称归一化（所有「按名查表」的统一匹配键；显示仍用原串）。

    NFKC 全半角（Ｔ５→t5）+ casefold + **去除全部空白与中点变体**（· ・ •）：
    「阿索代prime」≡「阿索代 Prime」≡「阿索代·Prime」⇒ 同一键；
    英文「Corrosive Projection」≡「corrosive  projection」同理。
    """
    s = unicodedata.normalize("NFKC", s or "").casefold()
    return re.sub(r"[·・•·\s]+", "", s)


_BILINGUAL: Optional[dict] = None


def load_bilingual() -> dict:
    """双语名称表索引（构建期 core/data/de/name_bilingual.json）。

    返回 {"en": {norm: [(en, zh), ...]}, "zh": {norm: [(en, zh), ...]}}；
    表缺失时返回空索引（查表方按「未收录」处理，不炸）。
    """
    global _BILINGUAL
    if _BILINGUAL is None:
        pairs: list = []
        try:
            from . import paths

            raw = json.loads(paths.read_path("de/name_bilingual.json").read_text(encoding="utf-8"))
            pairs = raw.get("pairs") or []
        except Exception:  # noqa: BLE001 - 表缺失不阻断（未收录提示兜底）
            pairs = []
        en_i: dict = {}
        zh_i: dict = {}
        for en, zh in pairs:
            en_i.setdefault(normalize_name(en), []).append((en, zh))
            zh_i.setdefault(normalize_name(zh), []).append((en, zh))
        _BILINGUAL = {"en": en_i, "zh": zh_i}
    return _BILINGUAL


def _dedupe_pairs(pairs: list, limit: int) -> list:
    out, seen = [], set()
    for p in pairs:
        if p in seen:
            continue
        seen.add(p)
        out.append(p)
        if len(out) >= limit:
            break
    return out


def bilingual_lookup(query: str, limit: int = 5) -> dict:
    """翻译查询（中英双向）。返回 {"direction", "hits", "candidates"}。

    · 含 CJK ⇒ 按中文侧查（zh→en），否则按英文侧查（en→zh）；
    · 精确（归一化后全等）优先，多义最多 limit 条；
    · 未精确命中 ⇒ 子串候选（≤limit）；仍无 ⇒ 两边皆空（调用方明确提示，不猜）。
    """
    q = (query or "").strip()
    has_cjk = any("一" <= c <= "鿿" for c in q)
    direction = "zh→en" if has_cjk else "en→zh"
    nq = normalize_name(q)
    if not nq:
        return {"direction": direction, "hits": [], "candidates": []}
    idx = load_bilingual()
    table = idx["zh"] if has_cjk else idx["en"]
    hits = table.get(nq) or []
    if hits:
        return {"direction": direction, "hits": _dedupe_pairs(hits, limit), "candidates": []}
    cands: list = []
    for k, v in table.items():
        if nq in k:
            cands.extend(v)
            if len(cands) >= limit * 4:
                break
    return {"direction": direction, "hits": [], "candidates": _dedupe_pairs(cands, limit)}
