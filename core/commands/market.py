# -*- coding: utf-8 -*-
"""市场查询域（D6 自 main.py 迁入，方法体逐字未改）。

覆盖路由键 6 项：wm / rm / wr / rank / trend / ducats；类属性 _RANK_CN
随域迁出。模块级 JUNK_FILE 原文为 Path(__file__).resolve().parent 拼接，
随域迁移后改写为等价的 PLUGIN_DIR 拼接（同值：插件根/core/data/junk.json，
本笔唯一非逐字改写点，语义与目标文件逐字节不变）。
子包纪律：不 import astrbot（事件对象鸭子类型）。
"""

from __future__ import annotations

import collections
import difflib
import json
import re
from typing import Optional

from .. import api_client
from ..i18n import L, is_en
from ..logging_compat import logger
from .. import formatters as fmt
from .. import matching
from ..parser import GROUP_BUY_MAX, WM_PART_EN_MATCH, parse_wm, parse_wr
from .base import PLUGIN_DIR, Action, Reply, err_card

JUNK_FILE = PLUGIN_DIR / "core" / "data" / "junk.json"

# 「蓝图=总图」分支的**排除词**（`_pick_wm_set_part` 用）：
#   「机体蓝图」这类**部件蓝图**不能被当成武器总图挑走。
# ★★ 必须与 `core/parser.py::_PART_SPECIFIC` **同步**（差集只允许「头部神经」——
#   它在 parser 侧已被 `_PART_STRENGTH` 归并成「头部」）。2026-10-07 提到模块级，
#   让 tests/test_wm_part.py 能做**结构断言**（此前只有注释提醒，靠人记）。
_SET_BLUEPRINT_SKIP = (
    "机体",
    "头部",
    "系统",
    "枪管",
    "枪机",
    "枪托",
    "头盔",
    "握柄",
    "刀刃",
    "连接器",
    "外壳",
    "弓弦",
    "弓身",
    "上弓臂",
    "下弓臂",
    "拳套",
    "武器舱",
    "镖袋",
    "护手",
    # ★ 2026-10-07 再同步 8 个新词（饰物/引擎/锤头/机舱/圆盘/机翼/外甲/握把）。
    #   全量 234 套装实测：不同步的话「蓝图」分支有 7 套给错部件
    #   （遂心=握把蓝图、Mantis/Scimitar/Xiphos/星察/午夜电波=引擎蓝图、
    #   刺影=外甲蓝图）；同步后 1 套修真 bug、5 套改给机身蓝图、1 套（刺影）
    #   变成「明说没有蓝图」——不静默给错。
    "饰物",
    "引擎",
    "锤头",
    "机舱",
    "圆盘",
    "机翼",
    "外甲",
    "握把",
)


# 「头」部件黑话消歧（2026-10-02 二修）：「水晶头」「水晶p头」「水晶 头」…
# = 头部部件。判据（用户口径）：**剥掉尾部「头」后的前缀必须精确存在于
# 物品名/黑话里**（水晶 → Citrine ✓）——白霜弹头的前缀「白霜弹」不是黑话
# → 不剥，整名按 MOD 解析。⚠️ 两个教训：
#   ① 消歧必须在整名解析**之前** —— 首版放在「解析失败→给建议」之后，
#      整名解析不到根本走不到（用户实测四种写法全挂）；
#   ② 判据只能用**精确**命中（resolve_wm_exact）—— 模糊链路会把「白霜弹」
#      沾到白霜（Frost）Prime 上，照样误剥。
async def _head_part_resolve(client, item: str):
    """整名以「头」结尾且剥头后的前缀精确命中 → 返回 (剥头物品名, 解析结果)。"""
    full = item.rstrip()
    if not full.endswith("头"):
        return None
    stripped = full[:-1].rstrip()
    if not stripped:
        return None
    if await client.resolve_wm_exact(full):
        return None  # 整名本身就是 MOD/物品（白霜弹头…）
    alt = await client.resolve_wm_exact(stripped)
    if alt:
        return stripped, alt
    return None


async def riven_market_weapon(client, query: str, weapon: dict) -> dict:
    """紫卡**市场**（wr）按**家族主武器**认卡 —— 2026-10-05 / 2026-10-10 用户口径。

    紫卡在家族内通用，挂单按主武器挂 ⇒ 「wr Dex侍刃 / 侍刃p / 棱晶空刃」一律查
    家族主武器（nikana / skana）的挂单；市场路径**不解析倾向**，只要认出是哪把武器。
    WM 拍卖端点只认**自家列表**里的 slug（实测 `weapon_url_name=rubico_prime` →
    **HTTP 400**，`rubico` → 200；`prisma_skana` → 400 item_not_exist）。

    解析顺序（家族判据只认 DE 官方 parentName 谱系 `riven_families.json`，经
    `matching.family_key`；名字不参与判族）：
      ① 家族主武器在自家列表 ⇒ 用它（`_riven_family_root`）——
         Dex侍刃 → nikana（2026-10-10 用户口径：WM 虽另列 dex_nikana，也查主武器）；
      ② 武器本身就在自家列表 ⇒ 原样（Dex Dakra 自成一族、euphona_prime 等独立条目）；
      ③ 剥 Prime 后缀（p / p版 / Prime，含条目 zh 名）回退母武器再解析一次；
      ④ 同族的自家列表成员（`_riven_family_listed`，主武器未上 WM 时）；
      都认不出唯一一把就原样返回（交给下游报空，不静默换武器）。
    """
    if not weapon:
        return weapon
    own = await client.wm_riven_weapon_slugs()
    entries = await client.wm_riven_weapons()
    root = _riven_family_root(weapon, entries, own)
    if root:
        return root
    if weapon.get("url_name") in own:
        return weapon
    base_q = matching.prime_base(query) or matching.prime_base(weapon.get("zh") or "")
    if base_q:
        base_w = await client.resolve_riven_weapon(base_q)
        if base_w and base_w.get("url_name") in own:
            return base_w
    return _riven_family_listed(weapon, entries, own) or weapon


def _same_family_listed(weapon: dict, entries: list, own: set) -> tuple[str, list]:
    """(家族根, 同族且在 WM 自家列表里的条目)；同 url_name 只留一条。

    紫卡类别（riven_type）已知时先收窄：Vinquibus 枪 / 近战两形态同族却是两种紫卡，
    近战形态不得落到步枪主武器；收窄后为空才退回不收窄（与第一版同口径）。
    """
    key = matching.family_key(weapon.get("en") or "")
    if not key:
        return "", []
    seen: set = set()
    cands = []
    for e in entries:
        u = e.get("url_name")
        if u in own and u not in seen and matching.family_key(e.get("en") or "") == key:
            seen.add(u)
            cands.append(e)
    rtype = weapon.get("riven_type") or ""
    if rtype and len(cands) > 1:
        cands = [e for e in cands if e.get("riven_type") == rtype] or cands
    return key, cands


def _riven_family_root(weapon: dict, entries: list, own: set) -> Optional[dict]:
    """家族**主武器**（谱系根）在 WM 自家列表里的条目；认不出 ⇒ None。

    ★ 2026-10-10 用户口径：wr 只查家族主武器（Dex侍刃 → nikana，不查 dex_nikana）。
    根 = `riven_families.json` 的家族根键（ExportWeapons uniqueName 尾段，如 Katana）：
      · WM 条目的 `game_ref`（= DE uniqueName）尾段 == 根键 ⇒ 主武器（nikana 的
        `.../KatanaAndWakizashi/Katana`）；
      · 人工补丁族（Hek / DarkDagger，根键取自基础武器名）：英文名归一化 == 根键
        （Hek 的 uniqueName 是 QuadShotgun，只能这样认）；
      · 都没有 ⇒ None（交给后续回退，不猜）。
    不跨族：Dex Furis 的根是 AkimboAutoPistols（Afuris），Dex Dakra 自成 DexTheSecond。
    """
    key, cands = _same_family_listed(weapon, entries, own)
    if not cands:
        return None
    hit = [e for e in cands if (e.get("game_ref") or "").rsplit("/", 1)[-1] == key]
    if not hit:
        nk = matching.normalize(key)
        hit = [e for e in cands if matching.normalize(e.get("en") or "") == nk]
    return hit[0] if len(hit) == 1 else None


def _riven_family_listed(weapon: dict, entries: list, own: set) -> Optional[dict]:
    """主武器不在 WM 列表时的回退：同族里 **WM 自家列表**的成员（2026-10-10 第一版）。

    ★ 对照 wfsim ce78ffeafd：紫卡按家族通用，WM 拍卖只认家族里它自家列出的条目
    （实测 `weapon_url_name=prisma_skana` → **HTTP 400** item_not_exist）。
    候选 = 同族且在自家列表里的条目（`_same_family_listed`，已按紫卡类别收窄）：
      · 只有一个 ⇒ 就是它；
      · 多个 ⇒ 取**名字就是家族名**的那个（候选英文名被查询英文名包含，且唯一）；
      · 认不出唯一一个 ⇒ None（不猜）。
    """
    _key, cands = _same_family_listed(weapon, entries, own)
    if len(cands) == 1:
        return cands[0]
    qn = matching.normalize(weapon.get("en") or "")
    named = [e for e in cands if (n := matching.normalize(e.get("en") or "")) and n in qn]
    return named[0] if len(named) == 1 else None


def _riven_now_by_name(rt: dict, weapons: list) -> dict:
    """WM 现价快照（键 = WM url）→ 按武器**英文名小写**索引（周报 compatibility 用英文名）。

    只收「跑满过一轮」（``ts`` 非空）的快照里的行；行内带 ``at``（该把抓取时间）。
    """
    rows = (rt or {}).get("rows") or {}
    if not rows or not (rt or {}).get("ts"):
        return {}
    out: dict = {}
    for w in weapons or []:
        r = rows.get(w.get("url_name") or "")
        en = (w.get("en") or "").lower()
        if r and en:
            out[en] = r
    return out


def _disp_name(d: dict) -> str:
    """物品 / 武器的展示名：中文模式官方中文名优先，英文模式英文名优先（回落 slug）。"""
    zh, en = d.get("zh"), d.get("en")
    first, second = (en, zh) if is_en() else (zh, en)
    return first or second or d.get("url_name", "")


def _stat_label(sid: str, riven_type: str = "", group: str = "") -> str:
    """紫卡词条展示名：中文取 RIVEN_STAT_ZH；英文取游戏内英文名（枪械的基伤写 Damage）。"""
    if is_en() and sid == "melee_damage" and not fmt._is_melee_weapon(riven_type, group):
        return "Damage"
    return fmt._stat_name(sid)


def _market_page(pool: list[dict], page: int, page_size: int) -> list[dict]:
    """按卡片相同的页码钳制取当前展示行，不把别页卖家挂到本页按钮。"""
    pages = max(1, (len(pool) + page_size - 1) // page_size)
    current = max(1, min(page, pages))
    return pool[(current - 1) * page_size : current * page_size]


def _prime_relic_query(host, item: dict) -> str:
    """只提供本地遗物反查表确有的 Prime 部件名，不猜名称或额外抓取。"""
    lookup = getattr(host, "_relic_db", None)
    if not callable(lookup):
        return ""
    names = {matching.normalize_name(item.get(key) or "") for key in ("zh", "en")}
    names = {name for name in names if "prime" in name}
    if not names:
        return ""
    try:
        _index, inverse = lookup()
        return next(
            (
                key
                for key, origins in inverse.items()
                if origins and matching.normalize_name(key) in names
            ),
            "",
        )
    except Exception:  # noqa: BLE001 —— 可选遗物按钮不影响市场查询
        return ""


# 排行 / 紫卡排行 / 杜卡德 的英文参数写法 → 中文内部键。
# ★ 只在这三个命令的参数解析处使用，不放进 parser 的通用输入别名表
#   （放进去会把武器名拆坏，例如 Heat Sword）；中文输入行为不变。
_RANK_CAT_ALIAS_EN = {
    "warframe": "甲",
    "warframes": "甲",
    "frame": "甲",
    "frames": "甲",
    "mod": "卡",
    "mods": "卡",
    "parts": "部件",
    "part": "部件",
    "arcane": "赋能",
    "arcanes": "赋能",
    "primary": "主武",
    "secondary": "副武",
    "melee": "近战",
    "relic": "遗物",
    "relics": "遗物",
}
_RIVEN_RANK_CAT_ALIAS_EN = {
    "melee": "近战",
    "rifle": "步枪",
    "shotgun": "霰弹枪",
    "pistol": "手枪",
    "archgun": "空战",
    "zaw": "Zaw",
    "kitgun": "组合枪",
    "primary": "主武",
    "secondary": "副武",
    "unrevealed": "未开",
    "unrolled": "未开",
    "veiled": "未开",
}
_DUCAT_TIER_ALIAS_EN = {"gold": "金", "silver": "银", "bronze": "铜"}


def _is_refresh(text: str) -> bool:
    """参数里有没有「刷新」（英文 refresh 同义）。"""
    return "刷新" in text or "refresh" in text.lower().split()


class MarketCommands:
    """Mixin：warframe.market / 排行 / 趋势 handler（挂载于 main.WarframeSDJK）。"""

    # ------------------------------------------------------------------
    # 市场与查价
    # ------------------------------------------------------------------
    @staticmethod
    def _pick_wm_set_part(parts: list[dict], part: str, en: bool = False) -> Optional[dict]:
        """从套装部件里挑出部件词对应的那个；没有返回 None。

        「蓝图/总图」= 战甲/武器**总图**：zh 以「蓝图」结尾且不含其他部件词
        （「机体蓝图」也以蓝图结尾，得排除）。其余部件词按 zh 包含匹配
        （「头部」命中「XX Prime 头部神经光元蓝图」）。
        """
        if en:
            # 英文写法的部件词：按英文名匹配（匹配不到再走下面的中文名路径）
            words = WM_PART_EN_MATCH.get(part, ())
            if part in ("蓝图", "总图"):
                others = {w for k, ws in WM_PART_EN_MATCH.items() if k != "蓝图" for w in ws}
                for p in parts:
                    nm = (p.get("en") or "").lower()
                    if nm.endswith("blueprint") and not any(
                        w in nm.replace("blueprint", "").split() for w in others
                    ):
                        return p
            else:
                for p in parts:
                    toks = (p.get("en") or "").lower().replace("blueprint", "").split()
                    if toks and toks[-1] in words:
                        return p
                    if any(" ".join(toks[-2:]) == w for w in words if " " in w):
                        return p
        if part in ("蓝图", "总图"):
            # ★ skip 与 _PART_SPECIFIC 必须同步（结构断言见 tests/test_wm_part.py）；
            #   常量已提到模块级 `_SET_BLUEPRINT_SKIP`（2026-10-07）。
            skip = _SET_BLUEPRINT_SKIP
            cands = [
                p
                for p in parts
                if (p.get("zh") or "").endswith("蓝图")
                and not any(w in (p.get("zh") or "") for w in skip)
            ]
            return cands[0] if cands else None
        for p in parts:
            if part in (p.get("zh") or ""):
                return p
        return None

    async def _h_wm(self, parsed, event, platform) -> Reply:
        capability = getattr(self, "_can_send_actions", None)
        can_actions = callable(capability) and capability(event)

        q = parse_wm(parsed.content, parsed.preset)
        if q.group_buy:
            return await self._wm_group_buy(q, platform)
        if not q.item:
            reply = err_card(
                L(
                    "没有写物品名",
                    "No item name given",
                ),
                L(
                    "按 wm 物品名 [部件] … 的格式重发，例如「wm 母牛 蓝图」",
                    'Resend as wm <item> [-r], e.g. "wm Mesa Prime Blueprint"',
                ),
                (
                    L("用法", "Usage"),
                    L(
                        "wm 物品名 [部件] [收购|合购a*2,b] [N个] [零级/满级/N级] "
                        "[完整/优良/无瑕/光辉] [墨染] [-r]",
                        "wm <item name> [-r]",
                    ),
                ),
                (
                    L("部件", "Parts"),
                    L(
                        "蓝图（总图）/ 机体 / 系统 / 头部 / 配件（全部部件比价），"
                        "如 wm 母牛 蓝图；头部可连写「头」（wm 水晶头 = wm 水晶 头部）",
                        "Put the part in the item name, e.g. wm Mesa Prime Blueprint / "
                        "Chassis / Neuroptics / Systems; for a whole set, send the set name",
                    ),
                ),
                (
                    L("品级", "Rank"),
                    L(
                        "满级按物品实际满级（赋能 5 级 / 川流不息 5 级 / "
                        "生命力 10 级）；精炼档只对遗物，墨染只看墨染 Mod",
                        "Max rank follows the item's real max rank "
                        "(Arcanes 5, Streamline 5, Vitality 10); refinement tiers only apply to relics",
                    ),
                ),
                title=L("wm 查价", "wm Price Check"),
            )
            if can_actions:
                reply.actions.append(
                    Action(
                        L("照示例发", "Send example"),
                        L("wm 母牛 蓝图", "wm Mesa Prime Blueprint"),
                        mode="fill",
                    )
                )
            return reply
        # 「头」部件黑话：消歧在整名解析之前（教训见 _head_part_resolve 注释）
        # 英文写的部件词：先试整串原文能否精确命中某个物品（Flame Blade、Mesa Prime Chassis…），
        # 命中就按原名走，不拆件
        _en_exact = None
        if q.part_en_raw and q.item:
            _full = f"{q.item} {q.part_en_raw}"
            _cand = await self.client.resolve_wm_item(_full)
            if _cand and (_cand.get("en") or "").lower() == _full.lower():
                _en_exact = _cand
                q.item, q.part = _full, ""
        _head = None if _en_exact else await _head_part_resolve(self.client, q.item)
        if _en_exact:
            item = _en_exact
        elif _head:
            q.item, item = _head
            q.part = "头部"
        else:
            item = await self.client.resolve_wm_item(q.item)
        if not item and q.part:
            # ★ 2026-10-05：拆件后解析失败 → 用「原文重组」再试一次。部件词表
            #   扩充后（刀刃/外壳/头盔/枪管…）会与少数 MOD 名相撞（簧压刀刃、
            #   爆裂刀刃、锐利刀刃、燃烧外壳、震击 秘奥头盔、红晶枪管…），
            #   这些条目的正解是整名解析。只在主解析失败时触发 ⇒ 只会改善，
            #   不改变任何现有成功路径。
            item = await self.client.resolve_wm_item(f"{q.item}{q.part}")
            if not item and q.part_en_raw:
                item = await self.client.resolve_wm_item(f"{q.item} {q.part_en_raw}")
        if not item:
            return await self._wm_suggest(q.item)
        # ★ 部件关键词（2026-09-19 玩家反馈「wm 母牛 蓝图」出的是整套）：
        #   命中具体部件词时切到**该部件**的订单；「配件/部件」泛指时保留
        #   整套 + 部件参考价（见尾部提示）。只在命中套装时生效——
        #   「wm 母牛机体」直接解析成部件的走原路。
        set_parts: list[dict] = []
        if q.part and "set" in set(item.get("tags") or []):
            try:
                set_parts = await self.client.wm_set_parts(item["url_name"])
            except Exception:  # noqa: BLE001 - 部件拆价是增强项，失败不影响主输出
                set_parts = []
            if q.part != "配件":
                picked = self._pick_wm_set_part(set_parts, q.part, bool(q.part_en_raw))
                if picked is None:
                    names = [_disp_name(p) for p in set_parts]
                    _nm = _disp_name(item)
                    return err_card(
                        L(
                            f"「{_nm}」没有「{q.part}」这个部件",
                            f'"{_nm}" has no "{q.part}" part',
                        ),
                        L(
                            f"从可用部件里选一个重发；或发整套看全部：wm {q.item}",
                            f"Pick one of the available parts and resend, or send the set to see all: wm {q.item}",
                        ),
                        (
                            L("可用部件", "Available parts"),
                            L("、", ", ").join(names)
                            or L("（未同步到部件表）", "(not synced to the parts table)"),
                        ),
                        title=L("wm 查价", "wm Price Check"),
                    )
                item = picked
        # ── 品级 / 遗物精炼 / 墨染 过滤（2026-09-25 补：解析早就有，一直没接）──
        # 先取订单再判品级：满级要参考「挂单里出现的最高级」（WM 有 44 个 MOD
        # 条目没给 maxRank），无级挂单则忽略该词并说明。
        orders, _info = await self.client.wm_orders(item["url_name"], platform)
        rank, rank_note = self._resolve_rank_filter(q.rank, q.rank_word, item, orders)
        notes = [rank_note] if rank_note else []
        subtypes = {str(o.get("subtype") or "") for o in orders}
        refinement = q.refinement
        if refinement and not (subtypes & {"intact", "exceptional", "flawless", "radiant"}):
            _w = q.refinement_word or refinement
            notes.append(
                L(
                    f"该物品没有遗物精炼档，「{_w}」已忽略",
                    f'This item has no relic refinement tiers; "{_w}" ignored',
                )
            )
            refinement = None
        elif refinement:
            _w = q.refinement_word or refinement
            notes.append(L(f"只看「{_w}」档遗物", f'Showing "{_w}" relics only'))
        moran = q.moran
        if moran and "atragraph" not in subtypes:
            notes.append(
                L(
                    "该物品没有墨染 Mod 变体，「墨染」已忽略",
                    "This item has no Atragraph variant; Atragraph filter ignored",
                )
            )
            moran = False
        elif moran:
            notes.append(L("只看墨染 Mod", "Showing Atragraph mods only"))
        display = _disp_name(item)
        title, lines, best, pool = fmt.fmt_wm_orders(
            display,
            orders,
            buy=q.buy,
            page=parsed.page,
            page_size=self.page_size,
            quantity=q.quantity,
            rank=rank,
            refinement=refinement,
            moran=moran,
            notes=notes,
        )
        hint = (
            ""
            if parsed.whisper or not best
            else L(" · 加 -r 生成游戏密语", " · add -r to generate a whisper")
        )
        # 套装附带部件参考价（2026-09-14 维护者要求）：单查部件走上面的
        # 归一化匹配（wm 席瓦蓝图），这里只在命中套装时多拉几个部件订单。
        # 部件切换路径（q.part 具体词）已在上面拉过 set_parts 且 item 已是
        # 部件（非 set root），这里不会再命中。
        parts = set_parts
        if not q.part:
            try:
                parts = await self.client.wm_set_parts(item["url_name"])
            except Exception:  # noqa: BLE001
                parts = []
        if parts:
            rows = []
            for p_ in parts:
                try:
                    po, _ = await self.client.wm_orders(p_["url_name"], platform)
                except Exception:  # noqa: BLE001
                    po = []
                rows.append(
                    {
                        "name": _disp_name(p_),
                        "sell": fmt.wm_best_price(po, "sell"),
                        "buy": fmt.wm_best_price(po, "buy"),
                    }
                )
            lines.extend(fmt.fmt_wm_set_parts(rows))
            if q.part == "配件":
                lines.append(
                    L(
                        f"※ 想看某个部件的在售/收购单：wm {q.item} 蓝图（或 机体 / 系统 / 头部）",
                        f"※ For one part's sell/buy orders, send its full name, "
                        f"e.g. wm {q.item} Blueprint (or Chassis / Neuroptics / Systems)",
                    )
                )
        lines = fmt.limit_notes(lines)  # 注脚收口到卡尾、最多 3 行
        reply = Reply(
            title, lines, footer=fmt.fmt_platform_footer(platform, "warframe.market" + hint)
        )
        if parsed.whisper and pool:
            # -r 给前 5 个卖家各生成一条密语（在线优先同展示顺序，
            # 2026-09-14 维护者要求：原来只有第一个）
            whisper_item = item.get("en") or item["url_name"]
            for o in pool[:5]:
                reply.whisper.append(fmt.build_whisper(o, whisper_item, sell=q.buy))
        if can_actions:
            whisper_item = item.get("en") or item["url_name"]
            shown = _market_page(pool, parsed.page, self.page_size)
            for number, order in enumerate(shown[:4], 1):
                seller = (order.get("user") or {}).get("ingame_name")
                if seller and seller != "?":
                    reply.actions.append(
                        Action(
                            L(f"{number}号", f"#{number}"),
                            fmt.build_whisper(order, whisper_item, sell=q.buy),
                            mode="copy",
                        )
                    )
            query_name = _disp_name(item)
            if query_name:
                reply.actions.append(
                    Action(
                        L("价格趋势", "Price Trend"),
                        L(f"趋势 {query_name}", f"trend {query_name}"),
                        row=1,
                    )
                )
            relic_query = _prime_relic_query(self, item)
            if relic_query:
                reply.actions.append(
                    Action(
                        L("所属遗物", "Relic"),
                        L(f"遗物 {relic_query}", f"relic {item.get('en') or relic_query}"),
                        row=1,
                    )
                )
        return reply

    @staticmethod
    def _resolve_rank_filter(q_rank, q_rank_word, item, orders=None) -> tuple:
        """品级过滤解析 → ``(rank, note)``（2026-09-25，同日二修：数据驱动）。

        满级（q_rank == -1）取物品**实际**满级，来源逐级降级并在卡面标明：
          ① WM items 的 maxRank（赋能 5 / 川流不息 5 / 生命力 10）
          ② 挂单里出现的最高级（WM 有 44 个 MOD 条目没给 maxRank，如 intruder）
          ③ 类别保守兜底（赋能 5，其余 10）

        挂单完全没有品级信息时（遗物、intruder 这类无级挂单）忽略该词并说明，
        避免静默返回空表。
        """
        if q_rank is None:
            return None, ""
        ranks = {o.get("mod_rank") for o in (orders or []) if o.get("mod_rank") is not None}
        tags = set(item.get("tags") or [])
        has_rank_meta = bool(item.get("max_rank")) or bool({"mod", "arcane_enhancement"} & tags)
        if orders is None:
            # 单测/旧调用：只看元数据
            if not has_rank_meta:
                _w = q_rank_word or q_rank
                return None, L(
                    f"该物品没有品级，「{_w}」已忽略",
                    f'This item has no ranks; "{_w}" ignored',
                )
        elif not ranks:
            _w = q_rank_word or q_rank
            return None, L(
                f"该物品的挂单没有品级信息，「{_w}」已忽略",
                f'Listings for this item carry no rank info; "{_w}" ignored',
            )
        if q_rank == -1:
            rank, src = item.get("max_rank"), "item"
            if not rank and ranks:
                rank, src = max(ranks), "orders"
            if not rank:
                rank, src = (5 if "arcane_enhancement" in tags else 10), "fallback"
            rank = int(rank)
            note = (
                L(
                    f"只列满级（按挂单最高 {rank} 级）",
                    f"Max rank only (highest listed: rank {rank})",
                )
                if src == "orders"
                else L(f"只列满级（{rank}级）的单", f"Max rank only (rank {rank})")
            )
            return rank, note
        return q_rank, L(f"只列 {q_rank} 级的单", f"Rank {q_rank} listings only")

    async def _wm_suggest(self, query: str) -> Reply:
        """未命中时给中英文候选（含错别字容忍，如 波斯顿→伯斯顿）。"""
        # 玄骸武器不在 WM 普通物品表（价格走 xh 拍卖）——先给正确入口，
        # 否则「wm 沙皇」这类查询只能拿到一串无关候选（沙皇=赤毒·沙皇，
        # 2026-09-24 实测：旧词典把「沙皇」错映射到 Inaros，已删）。
        # 这里只做**归一化精确**匹配，不借 resolve_lich_weapon 的模糊兜底，
        # 避免未命中路径被形近字劫持。
        _nq = re.sub(r"[\s·・]+", "", query).lower()
        for _k, _slug in (self.client._aliases.get("lich_items") or {}).items():
            if re.sub(r"[\s·・]+", "", _k).lower() == _nq:
                _info = self.client.lich_weapon_info(_slug)
                _zh = _info.get("zh") or _slug
                _nm = (_info.get("en") or _slug) if is_en() else _zh
                return err_card(
                    L(
                        f"「{query}」是玄骸武器（{_zh}），不在集市物品表里",
                        f'"{query}" is a Kuva/Tenet weapon ({_nm}), not in the market item table',
                    ),
                    L(
                        f"改用 xh {_zh}　（支持元素/数值筛选，如 xh {_zh} 辐射 50）",
                        f"Use xh {_nm} instead (auction listings of Kuva / Tenet weapons)",
                    ),
                    title=L("wm 查价", "wm Price Check"),
                )
        items = await self.client.wm_items()
        slugs = [it.get("url_name", "") for it in items]
        names = [_disp_name(it) for it in items]
        close = difflib.get_close_matches(query.lower().replace(" ", "_"), slugs, n=3, cutoff=0.5)
        hits = list(dict.fromkeys(close))
        if len(hits) < 3:
            for h in api_client.fuzzy_hits(query, names, n=3):
                if h not in hits:
                    hits.append(h)
        if hits:
            return err_card(
                L(f"没有找到「{query}」", f'No match for "{query}"'),
                L("照候选改名重发，或换英文名", "Resend using one of the suggestions"),
                (L("你是不是想找", "Did you mean"), L("、", ", ").join(hits[:3])),
                title=L("wm 查价", "wm Price Check"),
            )
        return err_card(
            L(f"没有找到「{query}」", f'No match for "{query}"'),
            L(
                "换英文名重发（或在 core/data/aliases.json 中补词条）",
                "Check the spelling and resend with the item's English name",
            ),
            title=L("wm 查价", "wm Price Check"),
        )

    async def _wm_group_buy(self, q, platform) -> Reply:
        sellers: dict[str, dict] = {}
        for name, qty in q.group_buy:
            item = await self.client.resolve_wm_item(name)
            if not item:
                return err_card(
                    L(f"合购中有物品未找到：{name}", f"Item in the group buy not found: {name}"),
                    L(
                        "检查该名称写法（可换英文名）后重发，或把它从合购单里去掉",
                        "Check the spelling and resend, or remove it from the list",
                    ),
                    title=L("wm 合购", "wm Group Buy"),
                )
            orders, _ = await self.client.wm_orders(item["url_name"], platform)
            sells = [
                o
                for o in orders
                if o.get("order_type") == "sell"
                and o.get("platform", platform) == platform
                and (o.get("user", {}).get("status") in ("ingame", "online"))
            ]
            sells.sort(key=lambda o: o["platinum"])
            for o in sells[:5]:
                seller = o["user"]["ingame_name"]
                sellers.setdefault(seller, {"items": {}, "total": 0})
                sellers[seller]["items"][item["url_name"]] = (o["platinum"], qty)
        ranked = sorted(
            sellers.items(),
            key=lambda kv: (-len(kv[1]["items"]), sum(p * c for p, c in kv[1]["items"].values())),
        )
        n_items = len(q.group_buy)
        lines = []
        for rank_i, (seller, info) in enumerate(ranked[:3]):
            total = sum(p * c for p, c in info["items"].values())
            covered = len(info["items"])
            if rank_i == 0:  # 卡首答案：最优卖家
                lines.append(
                    L(
                        f"◆ 最优卖家：{seller}｜覆盖 {covered}/{n_items} 件｜合计约 {total}p",
                        f"◆ Best Seller: {seller}｜Covers {covered}/{n_items} items｜About {total}p total",
                    )
                )
                lines.append(
                    L(
                        "◆ 候选卖家（覆盖件数多优先，其次合计价低）",
                        "◆ Candidate Sellers (most items covered first, then lowest total)",
                    )
                )
                lines.append(L("　卖家　覆盖　合计", "　Seller　Covered　Total"))
            lines.append(
                L(
                    f"· {seller}　{covered}/{n_items} 件　约 {total}p",
                    f"· {seller}　{covered}/{n_items} items　about {total}p",
                )
            )
            for url, (p, c) in info["items"].items():
                lines.append(f"　{url} × {c} = {p * c}p")
        dropped = getattr(q, "group_buy_dropped", 0)
        cap_tip = (
            L(
                f"※ ⚠ 合购单最多 {GROUP_BUY_MAX} 项，超出的 {dropped} 项已忽略",
                f"※ ⚠ A group buy holds at most {GROUP_BUY_MAX} items; {dropped} extra ignored",
            )
            if dropped
            else ""
        )
        if not lines:
            return err_card(
                L(
                    "暂时没有在线卖家能凑齐合购单",
                    "No online seller can cover the whole group buy right now",
                ),
                L(
                    "稍后重试；或减少合购项数、换成单件分开查（wm 物品名）",
                    "Try again later, or shorten the list / check items one by one (wm <item>)",
                ),
                cap_tip.replace("※ ⚠ ", "⚠ ") if cap_tip else "",
                title=L("wm 合购", "wm Group Buy"),
            )
        if cap_tip:
            lines.append(cap_tip)
        return Reply(
            L(
                "合购最优卖家（同卖家买齐更省事）",
                "Group Buy Best Seller (one seller for everything)",
            ),
            lines,
            footer=fmt.fmt_platform_footer(platform),
        )

    @staticmethod
    def _is_exact_match(a: dict, q, neg_set: set, pos_set: set) -> bool:
        """「完全命中词条」判定（2026-10-01 B 口径，**仅排序用**，非硬过滤）。

        与 ``_auction_match``（子集判定 + 在线状态/价格过滤）不同：这里只看
        词条集合**相等**，不看在线状态与价格：
          · 指定了正词条 → 实际正词条集合 == 指定集合（多一个即超集）；
          · 指定了具体负词条 → 实际负词条集合 == 指定集合；
          · 只是「任意负」（require_negative、无具体负词条）→ 实际负词条 ≥1
            即算命中（紫卡最多 1 条负，等价于 ==1；不得因「没指定名字」判超集）；
          · 完全没指定词条 → 全部算命中（调用方传 exact_ids=None 退化）。
        """
        item = a.get("item", {}) or {}
        attrs = item.get("attributes") or []
        urls_p = {at.get("url_name") for at in attrs if at.get("positive")}
        urls_n = {at.get("url_name") for at in attrs if not at.get("positive")}
        if pos_set and urls_p != pos_set:
            return False
        if neg_set and urls_n != neg_set:
            return False
        if q.require_negative and not neg_set and not urls_n:
            return False
        return True

    # ★ 2026-10-23：wr「未开紫卡」词表（玩家报「wr 手枪未开 搜不到」）。
    #   类别词**不新造**，对齐 `fmt._RIVEN_ITEM_TYPE` / `_VEILED_SLUGS` 的既有中文与键；
    #   同义词（副武/主武/曲翼/霰弹/守护/同伴）来自用户在用的叫法。
    #   英文写法（unrevealed / unrolled / veiled）只在这里认，不进通用输入别名表。
    _WR_VEILED_WORDS = ("未揭示", "未开", "未解", "veiled", "unrevealed", "unrolled")
    # ★ 长词在前（霰弹枪 先于 霰弹），否则匹配到「霰弹」会残留「枪」被判成非未开查询
    _WR_VEILED_CATS = (
        ("霰弹枪", "Shotgun"),
        ("霰弹", "Shotgun"),
        ("组合枪", "Kitgun"),
        ("手枪", "Pistol"),
        ("副武", "Pistol"),
        ("步枪", "Rifle"),
        ("主武", "Rifle"),
        ("近战", "Melee"),
        ("曲翼", "Archgun"),
        ("空战", "Archgun"),
        ("zaw", "Zaw"),
        ("守护", "Robotic"),
        ("同伴", "Robotic"),
        ("shotgun", "Shotgun"),
        ("kitgun", "Kitgun"),
        ("pistol", "Pistol"),
        ("secondary", "Pistol"),
        ("rifle", "Rifle"),
        ("primary", "Rifle"),
        ("melee", "Melee"),
        ("archgun", "Archgun"),
        ("companion", "Robotic"),
    )

    def _wr_veiled_query(self, content) -> "str | None":
        """识别「类别 + 未开」；返回类别键（无类别时返回 ``""``）；不是未开查询返回 None。

        ★ 只在「除未开词与类别词之外没有别的实质内容」时才认（维护者要求「wr 武器名」
        原行为一字不改，别把「未开」误吞成武器名的一部分）。
        四种写法都认：`wr 手枪未开` / `wr 手枪 未开` / `wr 未开 手枪` / `wr 未开手枪`。
        """
        toks = [str(t).strip() for t in (content or []) if str(t).strip()]
        if not toks:
            return None
        blob = " ".join(toks).lower()
        if not any(w in blob for w in self._WR_VEILED_WORDS):
            return None
        cat = ""
        for word, key in self._WR_VEILED_CATS:
            if word in blob:
                cat = key
                break
        # 去掉未开词与类别词后应无残留（否则当普通 wr 处理）
        rest = blob
        for w in self._WR_VEILED_WORDS:
            rest = rest.replace(w, " ")
        if cat:
            for word, key in self._WR_VEILED_CATS:
                if key == cat:
                    rest = rest.replace(word, " ")
        if rest.strip():
            return None
        return cat

    async def _wr_veiled(self, cat: str, parsed, platform) -> Reply:
        """未开紫卡：有类别 ⇒ 该 slug 的 WM 挂单；无类别 ⇒ 8 类中位/最低/最高。"""
        if not cat:
            stats = await self.client.wm_veiled_stats(platform)
            title, lines = fmt.fmt_wm_veiled_summary(stats)
            return Reply(title, lines, footer=fmt.fmt_platform_footer(platform))
        slug = self.client._VEILED_SLUGS.get(cat)
        if not slug:
            return err_card(
                L("没有这个未开紫卡类别", "No such unrevealed riven category"),
                L(
                    "改用：近战/步枪/霰弹枪/手枪/曲翼/组合枪/Zaw/守护",
                    "Use: melee / rifle / shotgun / pistol / archgun / kitgun / zaw / companion",
                ),
            )
        orders, _info = await self.client.wm_orders(slug, platform)
        # 短名（「手枪」而不是「手枪未开紫卡」），免得标题变成「未开紫卡 · 手枪未开紫卡」；
        # 短名从既有 _VEILED_TYPE_ZH 去后缀得到，不新造词表。
        label = (
            cat
            if is_en()
            else fmt._VEILED_TYPE_ZH.get(cat, cat).removesuffix("未开紫卡").strip() or cat
        )
        title, lines, _best, _pool = fmt.fmt_wm_orders(
            L(f"未开紫卡 · {label}", f"Unrevealed Riven · {label}"),
            orders,
            buy=False,
            page=parsed.page,
            page_size=self.page_size,
        )
        lines.append(
            L(
                "※ 未开紫卡只有「武器类别」没有具体武器名；加类别看别的：wr 未开",
                '※ Unrevealed rivens only have a weapon category, no weapon name; add a category to see others: "wr unrevealed"',
            )
        )
        return Reply(title, lines, footer=fmt.fmt_platform_footer(platform))

    async def _h_wr(self, parsed, event, platform) -> Reply:
        capability = getattr(self, "_can_send_actions", None)
        can_actions = callable(capability) and capability(event)

        _vc = self._wr_veiled_query(parsed.content)
        if _vc is not None:
            return await self._wr_veiled(_vc, parsed, platform)

        q = parse_wr(parsed.content)
        if q.unrecognized_stats:
            return err_card(
                L(
                    "无法识别紫卡筛选词：「" + "、".join(q.unrecognized_stats) + "」",
                    'Unrecognized riven filter: "' + ", ".join(q.unrecognized_stats) + '"',
                ),
                L(
                    "检查负词条写法后重发；可使用卡面全称或短名，英文不区分大小写",
                    "Check how the negative stat is written and resend; full in-game names or short names both work, case-insensitive",
                ),
                title=L("紫卡拍卖", "Riven Auctions"),
            )
        if not q.weapon:
            reply = err_card(
                L("没有写武器名", "No weapon name given"),
                L(
                    "按 wr 武器名 [筛选词] 的格式重发，例如「wr 绝路 暴击」",
                    'Resend as wr <weapon> [filters], e.g. "wr Rubico Critical Chance"',
                ),
                (
                    L("用法", "Usage"),
                    L(
                        "wr 武器名 [最新|离线] [r槽/-槽/角槽] [1000p] "
                        "[零洗/低洗/废洗/N洗] [词条连写如:基多暴负变焦] [2+|3+1] [-r]",
                        "wr <weapon> [stats, e.g. Critical Chance] [1000p] [2+|3+1] [-r]",
                    ),
                ),
                title=L("紫卡拍卖", "Riven Auctions"),
            )
            if can_actions:
                reply.actions.append(
                    Action(
                        L("照示例发", "Send example"),
                        L("wr 绝路 暴击", "wr Rubico Critical Chance"),
                        mode="fill",
                    )
                )
            return reply
        weapon = await self.client.resolve_riven_weapon(q.weapon)
        if not weapon:
            tips = await self.client.suggest_riven_weapons(q.weapon)
            if tips:
                return err_card(
                    L(f"未找到紫卡武器「{q.weapon}」", f'Riven weapon not found: "{q.weapon}"'),
                    L("照候选改名重发", "Resend using one of the suggestions"),
                    (L("你是不是想找", "Did you mean"), L("、", ", ").join(tips)),
                    title=L("紫卡拍卖", "Riven Auctions"),
                )
            return err_card(
                L(f"未找到紫卡武器「{q.weapon}」", f'Riven weapon not found: "{q.weapon}"'),
                L(
                    "请使用英文名，或补充别名表后重发",
                    "Check the spelling and resend with the weapon's English name",
                ),
                title=L("紫卡拍卖", "Riven Auctions"),
            )
        asked = weapon
        weapon = await riven_market_weapon(self.client, q.weapon, weapon)
        url_name = weapon["url_name"]
        rtype = weapon.get("riven_type", "")
        positives = self.client.normalize_riven_stats(q.stats, rtype)
        negatives = self.client.normalize_riven_stats(q.negatives, rtype)
        auctions = await self.client.wm_riven_auctions(
            url_name,
            platform,
            positives=positives,
            negatives=negatives,
            polarity=q.polarity,
            max_price=q.max_price,
            max_rerolls=q.max_rerolls,
            min_rerolls=q.rerolls_min,
            require_negative=q.require_negative,
        )
        neg_set = set(negatives)
        pos_set = set(positives)
        # 显式 3+1，或三个指定正词条 + 具体/任意负面，均不自动放宽条件。
        strict_3p1 = (q.positive_count == 3 or len(pos_set) >= 3) and (
            q.negative_count == 1 or q.require_negative or bool(neg_set)
        )
        pool = [a for a in auctions if self._auction_match(a, q, neg_set, pos_set)]
        # ★ 2026-10-08：可追溯日志 —— 「卡上少一条」类问题直接从日志对：
        #   API 回来多少 / 本地筛完多少 / 各状态几条（玩家报障时按这个对 WM 网页）。
        _st = collections.Counter(
            ((a.get("owner") or {}).get("status") or "offline") for a in auctions
        )
        _st2 = collections.Counter(
            ((a.get("owner") or {}).get("status") or "offline") for a in pool
        )
        logger.info(
            "[sdjk] wr 挂单池：API %d 条%s → 本地筛后 %d 条%s（武器 %s，词条 %s）",
            len(auctions),
            dict(_st),
            len(pool),
            dict(_st2),
            url_name,
            sorted(pos_set),
        )
        relaxed = ""
        if not strict_3p1 and not pool and auctions and (q.stats or q.negatives):
            # 严格匹配为空时分两档放宽（2026-09-24 玩家报障「前排出现不匹配的
            # 项目」——旧实现直接跳到「近似匹配」，而近似排序又被渲染层按
            # 在线+价格重排，于是前排全是便宜但与词条无关的挂单）：
            #   ① 先只放宽**在线状态**：词条完全匹配但卖家离线 → 仍排前面
            #   ② 真没有完全匹配的，才按词条命中率给最接近的选项
            strict_offline = [
                a
                for a in auctions
                if self._auction_match(a, q, neg_set, pos_set, ignore_status=True)
            ]
            if strict_offline:
                pool = sorted(
                    strict_offline,
                    key=lambda a: (
                        fmt._ONLINE_RANK.get((a.get("owner") or {}).get("status") or "offline", 3),
                        a.get("buyout_price") or a.get("starting_price") or 0,
                    ),
                )
                relaxed = "offline"
        if not strict_3p1 and not pool and auctions and (q.stats or q.negatives):
            # 服务端降级时可能未筛词条，本地二次筛为空则按命中率给近似选项。
            # 3+1 查询不进入此分支，避免指定整张卡的条件被自动放宽。
            def _rank(a: dict):
                item = a.get("item", {}) or {}
                attrs = item.get("attributes") or []
                urls_p = {at.get("url_name") for at in attrs if at.get("positive")}
                urls_n = {at.get("url_name") for at in attrs if not at.get("positive")}
                pos_hit = len(pos_set & urls_p)
                neg_hit = len(neg_set & urls_n)
                want_neg = 1 if (q.require_negative or q.negatives) else 0
                score = pos_hit * 4 + neg_hit * 2 + (want_neg & (1 if urls_n else 0))
                price = a.get("buyout_price") or a.get("starting_price") or 0
                # 同档内再按「在线优先 → 价格升序」（与渲染层展示口径一致）
                online = fmt._ONLINE_RANK.get((a.get("owner") or {}).get("status") or "offline", 3)
                return (-score, online, price)

            # 只放宽具体词条与在线状态，价格/洗数/词条数/有无负面仍是硬条件。
            cand = [
                a for a in auctions if self._auction_match(a, q, set(), set(), ignore_status=True)
            ]
            pool = sorted(cand, key=_rank)[: max(4, self.page_size - 4)]
            if pool:
                relaxed = "loose"
        wname = _disp_name(weapon) or url_name
        # 完全命中词条的 id 集合（2026-10-01 B 口径，仅排序用）：在线档优先、
        # 档内恰好在前、超集仍可翻页。未指定词条 → None（退化为现行为）。
        # 放宽档（offline/loose）走 presorted，此集合不参与。
        exact_ids = (
            {a.get("id") for a in pool if self._is_exact_match(a, q, neg_set, pos_set)}
            if (q.stats or q.negatives or q.require_negative)
            else None
        )
        ordered = fmt.wr_sorted_auctions(pool, presorted=bool(relaxed), exact_ids=exact_ids)
        title, lines, best = fmt.fmt_wr_auctions(
            wname,
            ordered,
            page=parsed.page,
            page_size=max(4, self.page_size - 4),
            riven_type=rtype,
            group=weapon.get("group", ""),
            # 放宽档的排序是「词条命中率优先」，必须原样保留 —— 渲染层默认
            # 按 在线+价格 重排会把命中的挂单冲散
            presorted=True,  # 卡片与按钮使用上面的同一个排序结果
            exact_ids=exact_ids,
        )
        _grp = weapon.get("group", "")
        filters = [f"▲{_stat_label(s, rtype, _grp)}" for s in dict.fromkeys(q.stats)]
        filters += [f"▼{_stat_label(s, rtype, _grp)}" for s in dict.fromkeys(q.negatives)]
        if q.require_negative and not q.negatives:
            filters.append(L("任意负面", "Any negative"))
        if filters or strict_3p1:
            suffix = L("（3+1，不自动放宽）", " (3+1, not relaxed)") if strict_3p1 else ""
            lines.insert(
                0,
                L(
                    "◆ 筛选：" + "、".join(filters) + suffix,
                    "◆ Filter: " + ", ".join(filters) + suffix,
                ),
            )
        _asked = _disp_name(asked)
        if url_name != asked.get("url_name"):
            # ★ 2026-10-10：变体（Dex/Prime/棱晶/赤毒…）改查家族主武器 ⇒ 卡面写明，
            #   免得玩家以为把「Dex 侍刃」看成了「侍刃」。
            lines.insert(
                0,
                L(
                    f"※ 紫卡按家族挂单，「{_asked}」已按主武器「{wname}」查",
                    f'※ Rivens are listed per weapon family; "{_asked}" was checked as its base weapon "{wname}"',
                ),
            )
        if asked.get("_fuzzy_from"):  # 形近兜底标记在解析结果上（改查主武器后仍要提示）
            lines.insert(
                0,
                L(
                    f"※ 「{asked['_fuzzy_from']}」按「{_asked}」查询",
                    f'※ "{asked["_fuzzy_from"]}" was checked as "{_asked}"',
                ),
            )
        _off = [a for a in pool if ((a.get("owner") or {}).get("status") or "offline") == "offline"]
        if _off:
            lines.append(
                L(
                    f"※ 含 {len(_off)} 条离线挂单（已排在在线之后）",
                    f"※ Includes {len(_off)} offline listing(s) (ranked after online ones)",
                )
            )
        if relaxed == "offline":
            lines.append(
                L(
                    "※ 完全符合词条的挂单卖家目前都不在线，已按 在线优先 → 价格升序 列出",
                    "※ Sellers of exact-match listings are all offline; sorted online first, then price ascending",
                )
            )
        elif relaxed == "loose":
            lines.append(
                L(
                    "※ 没有完全符合条件的挂单，以上按词条命中率与价格给出最接近选项",
                    "※ No listing matches exactly; the closest options by stat match and price are shown",
                )
            )
        # 「加 -r 生成密语」是用法提示，进页脚（与 wm 同口径）；注脚收口到卡尾、最多 3 行
        _hint = (
            ""
            if parsed.whisper or not best
            else L(" · 加 -r 生成游戏内私聊密语", " · add -r to generate an in-game whisper")
        )
        reply = Reply(
            title,
            fmt.limit_notes(lines),
            footer=fmt.fmt_platform_footer(
                platform, L("warframe.market 紫卡", "warframe.market Rivens") + _hint
            ),
        )

        def auction_whisper(auction):
            item = auction.get("item", {}) or {}
            price = auction.get("buyout_price") or auction.get("starting_price")
            return (
                f"/w {auction.get('owner', {}).get('ingame_name', '?')} Hi! I want to buy: "
                f'"{item.get("name", weapon.get("url_name"))}" for {price} platinum. (warframe.market)'
            )

        if parsed.whisper and best:
            reply.whisper.append(auction_whisper(best))
        if can_actions:
            shown = _market_page(ordered, parsed.page, max(4, self.page_size - 4))
            for number, auction in enumerate(shown, 1):
                owner = auction.get("owner") or {}
                seller = owner.get("ingame_name")
                if not seller or seller == "?":
                    continue
                reply.actions.append(
                    Action(
                        L(f"{number}号", f"#{number}"),
                        auction_whisper(auction),
                        mode="copy",
                        row=(number - 1) // 5,
                    )
                )
            row = (len(shown) + 4) // 5
            if _asked:
                reply.actions.extend(
                    [
                        Action(
                            L("倾向", "Disposition"),
                            L(f"倾向 {_asked}", f"dispo {_asked}"),
                            row=row,
                        ),
                        Action(
                            L("词条范围", "Stat Ranges"),
                            L(f"词条范围 {_asked}", f"rivenranges {_asked}"),
                            row=row,
                        ),
                    ]
                )
        return reply

    # ------------------------------------------------------------------
    # 排行 / 趋势 / 开核桃（P1 批次）
    # ------------------------------------------------------------------
    _RANK_CN = ("甲", "卡", "部件", "赋能", "主武", "副武", "近战", "遗物")

    @staticmethod
    def _rank_guide(platform) -> Reply:
        """榜单未建立时的引导卡。"""
        return Reply(
            L("价格排行", "Price Rankings"),
            [
                L(
                    "◆ 状态：全量价格榜单还未建立",
                    "◆ Status: the full price board has not been built yet",
                ),
                L(
                    "※ 请管理员发「排行 刷新」启动后台抓取（耗时随候选数量和行情额度变化，"
                    "期间排行照常可查）",
                    '※ An admin can send "rank refresh" to start the background crawl '
                    "(time depends on candidates and market quota; rankings stay available meanwhile)",
                ),
                L(
                    "※ 完成后：「排行」看 甲/武器/卡 三榜，「排行 分类」看 20 名完整榜",
                    '※ Afterwards: "rank" shows the Warframe / Weapon / MOD boards, '
                    '"rank <category>" shows the full top 20',
                ),
            ],
            footer=fmt.fmt_platform_footer(platform, "warframe.market"),
        )

    async def _h_rank(self, parsed, event, platform) -> Reply:
        """价格排行：落盘全量榜，按当前成交中位价降序。"""
        content = parsed.content_str or ""
        # 英文写法「rank riven …」等价于「紫卡排行 …」（别名只在这里认）
        _toks_en = [t.lower() for t in (parsed.content or [])]
        _riven_en = (parsed.preset or "") != "紫卡" and _toks_en[:1] == ["riven"]
        if _is_refresh(content) and (parsed.preset or "") != "紫卡" and not _riven_en:
            if not self._is_admin(event):
                return Reply(
                    L("排行刷新", "Rankings Refresh"),
                    [
                        L(
                            "只有管理员才能重抓 WM 全量排行；可发送「排行」查阅或「排行 进度」查看进度。",
                            'Only admins can re-crawl the full WM rankings; send "rank" to view them '
                            'or "rank progress" to check progress.',
                        )
                    ],
                    text_only=True,
                )
            try:
                started, done, total = self.client.start_rank_crawl()
            except api_client.WarframeAPIError as exc:
                return Reply(
                    L("排行刷新", "Rankings Refresh"),
                    [
                        L(
                            f"未启动抓取：{api_client.public_error_text(exc)}",
                            f"Crawl not started: {api_client.public_error_text(exc)}",
                        )
                    ],
                    text_only=True,
                )
            state = (
                L("已启动新一轮全量抓取", "A new full crawl has started")
                if started
                else L("抓取已在进行中", "A crawl is already running")
            )
            return Reply(
                L("排行刷新", "Rankings Refresh"),
                [
                    L(
                        f"◆ 状态：{state}（候选 {total or '待统计'} 项，候选数不等于联网次数；耗时看进度）",
                        f"◆ Status: {state} ({total or 'counting'} candidates; "
                        "candidates are not network calls; see progress for timing)",
                    ),
                    L(f"◆ 当前进度：{done}/{total or '?'}", f"◆ Progress: {done}/{total or '?'}"),
                    L(
                        "※ 抓取期间排行照常可查（显示已完成部分）；可稍后重发本指令看进度",
                        "※ Rankings stay available during the crawl (completed part shown); "
                        "resend this command later to check progress",
                    ),
                ],
                footer=fmt.fmt_platform_footer(platform, "warframe.market"),
            )
        if content.strip().lower() in ("进度", "progress"):
            data = self.client._load_json_file(api_client.paths.read_path(api_client.RANKS_NAME))
            task = getattr(self.client, "_rank_crawl_task", None)
            running = (
                bool(task and not task.done())
                or self.client._crawl_lock("_rank_crawl_lock").locked()
            )
            _state = (
                L("抓取进行中", "Crawl in progress")
                if running
                else L("当前没有抓取任务", "No crawl running")
            )
            _done = f"{int(data.get('done') or 0)}/{int(data.get('total') or 0) or '?'}"
            return Reply(
                L("排行进度", "Rankings Progress"),
                [
                    L(f"◆ 状态：{_state}", f"◆ Status: {_state}"),
                    L(f"◆ 当前进度：{_done}", f"◆ Progress: {_done}"),
                ],
            )
        cat = parsed.preset or ""
        if not cat:
            for t in parsed.content or []:
                c = t.replace("排行", "").strip()
                c = _RANK_CAT_ALIAS_EN.get(c.lower(), c)
                if c.lower() == "riven":
                    c = "紫卡"
                if c in self._RANK_CN or c == "紫卡":
                    cat = c
                    break
            else:
                cat = content.strip()
                cat = _RANK_CAT_ALIAS_EN.get(cat.lower(), cat)
        if cat == "紫卡":
            return await self._h_riven_rank(parsed, event, platform)
        if not cat:
            rows = self.client.rank_rows()
            if not rows:
                return self._rank_guide(platform)
            title, lines = fmt.fmt_rank_overview(rows)
            return Reply(
                title,
                lines,
                footer=fmt.fmt_platform_footer(
                    platform, L("warframe.market 成交", "warframe.market trades")
                ),
            )
        if cat not in self.client.RANK_CATEGORIES:
            return err_card(
                L(f"未知分类「{cat}」", f'Unknown category "{cat}"'),
                L(
                    "从可选分类里选一个重发；紫卡排行请发「紫卡排行」",
                    'Pick one of the categories and resend; for riven rankings send "rank riven"',
                ),
                (
                    L("可选", "Options"),
                    " / ".join(self._RANK_CN)
                    if not is_en()
                    else " / ".join(fmt._RANK_CAT_EN[c] for c in self._RANK_CN),
                ),
                title=L("市场排行", "Market Rankings"),
            )
        rows = self.client.rank_rows()
        if not rows:
            return self._rank_guide(platform)
        title, lines = fmt.fmt_rank_table(cat, rows)
        return Reply(
            title,
            lines,
            footer=fmt.fmt_platform_footer(
                platform, L("warframe.market 成交", "warframe.market trades")
            ),
        )

    async def _h_riven_rank(self, parsed, event, platform) -> Reply:
        """紫卡排行：DE 官方周报为主体；管理员手动拉过 WM 现价后，附「WM挂单 / 变化」两列。

        ★ 2026-10-25 维护者改口径：上一版「实时排行」（凌晨自动抓 WM 拍卖、按挂单数排序）用处不大
        —— WM 拍卖搜索单次最多回 100 条，挂单数一律 100，排序没意义；整轮约 30 分钟还容易触发限流。
        改为：周报热度榜照旧（不打 WM），**大更后由管理员手动拉一次现价**（「紫卡排行 刷新」或
        面板按钮，约 30 分钟），对比周报参考价看哪些武器涨跌明显。不再自动抓。
        「紫卡排行 刷新」：所有人都会顺带强更 DE 周报文件；只有管理员会启动 WM 现价抓取。
        """
        content = (parsed.content_str or "").strip()
        try:
            snap = await self.client.de_weekly_rivens(platform, force=_is_refresh(content))
        except Exception as e:  # noqa: BLE001
            logger.warning("[sdjk] 紫卡周报拉取失败：%r", e)
            return err_card(
                L(
                    f"紫卡周报暂时拉取失败：{api_client.public_error_text(e)}",
                    f"Weekly riven report temporarily unavailable: {api_client.public_error_text(e)}",
                ),
                L("稍后重发「紫卡排行」", 'Try "rank riven" again later'),
                title=L("紫卡排行", "Riven Rankings"),
            )
        requested_platform = api_client._de_riven_platform(platform)
        actual_platform = api_client._de_riven_platform(snap.get("platform") or platform)
        source = L("DE 官方周报", "DE Official Weekly Report")
        if requested_platform != "PC" or actual_platform != "PC":
            source += f"（{actual_platform}）"

        def weekly_reply(reply):
            if requested_platform != "PC":
                # 格式器的通用提示默认能启动PC现价；主机请求须给出明确的平台命令。
                reply.lines = [
                    L(
                        "※ WM 现价目前仅维护 PC，可用「紫卡排行 -pc」查看。",
                        '※ Live WM prices are only maintained for PC; use "rank riven -pc" to view them.',
                    )
                    if line == fmt._riven_now_note("")
                    else line
                    for line in reply.lines
                ]
            if snap.get("fallback_from"):
                reply.lines.append(
                    L(
                        f"※ {snap['fallback_from']} 周报暂不可用，本次回落 PC 官方周报。",
                        f"※ The {snap['fallback_from']} weekly report is unavailable; fell back to the PC report.",
                    )
                )
            return reply

        weapons = await self.client.wm_riven_weapons()
        zhm = {w.get("en", "").lower(): (w.get("zh") or "") for w in weapons}
        cat = ""
        for t in parsed.content or []:
            k = t.replace("排行", "").strip()
            if k.lower() == "riven":  # 「rank riven melee」里的 riven 本身不是分类
                continue
            k = _RIVEN_RANK_CAT_ALIAS_EN.get(k.lower(), k)
            if k in fmt._RIVEN_ITEM_TYPE or k in ("主武", "副武", "未开"):
                cat = k
                break
        # 现价抓取只维护PC；主机官方周报不能拼进PC挂单价计算涨跌。
        rt = self.client.riven_realtime_rows() if actual_platform == "PC" else {}
        if _is_refresh(content):
            if requested_platform != "PC":
                return weekly_reply(
                    Reply(
                        L("紫卡排行刷新", "Riven Rankings Refresh"),
                        [
                            L(
                                "◆ 状态：已强制更新 DE 官方周报",
                                "◆ Status: DE official weekly report force-updated",
                            ),
                            L(
                                "◆ WM 现价：目前仅维护 PC，未启动抓取；需要时使用「紫卡排行 刷新 -pc」",
                                '◆ WM live prices: only maintained for PC, no crawl started; use "rank riven refresh -pc" if needed',
                            ),
                        ],
                        footer=fmt.fmt_platform_footer(platform, source),
                    )
                )
            if not self._is_admin(event):
                when = fmt.format_time(rt.get("ts"), "%Y-%m-%d %H:%M")
                return Reply(
                    L("紫卡排行刷新", "Riven Rankings Refresh"),
                    [
                        L(
                            "◆ 状态：已强制更新 DE 官方周报",
                            "◆ Status: DE official weekly report force-updated",
                        ),
                        L(
                            "◆ WM 现价：由管理员手动拉取（「紫卡排行 刷新」或面板按钮），避免频繁触发市场限流",
                            "◆ WM live prices: pulled manually by an admin ("
                            '"rank riven refresh" or the panel button) to avoid market rate limits',
                        ),
                        L(
                            f"◆ 最近一次现价：{when or '还没拉过'}",
                            f"◆ Last live-price pull: {when or 'never'}",
                        ),
                    ],
                    footer=fmt.fmt_platform_footer(platform, source),
                )
            state, n, total = self.client.start_riven_rt_crawl()
            say = L(
                {
                    "started": "已启动拉取",
                    "running": "拉取已在进行中",
                    "cooldown": "冷却中，未启动抓取；距上次开始及结束均需满 10 分钟后再试",
                }.get(state, state),
                {
                    "started": "Pull started",
                    "running": "A pull is already running",
                    "cooldown": "On cooldown, not started; wait 10 minutes after the last start and finish",
                }.get(state, state),
            )
            return Reply(
                L("紫卡排行刷新", "Riven Rankings Refresh"),
                [
                    L(
                        "◆ 周报：已强制更新 DE 官方周报",
                        "◆ Weekly report: DE official weekly report force-updated",
                    ),
                    L(
                        f"◆ WM 现价：{say}（目标 {total or '待统计'} 把，与其它行情查询共用额度；耗时看进度）",
                        f"◆ WM live prices: {say} (target {total or 'counting'} weapons; "
                        "shares quota with other market queries; see progress for timing)",
                    ),
                    L(
                        f"◆ 当前已收录：{n}/{total or '?'} 把",
                        f"◆ Collected so far: {n}/{total or '?'} weapons",
                    ),
                    L(
                        "※ 拉完后「紫卡排行」各行末尾会多出「WM挂单 / 变化」两列",
                        '※ Once done, "rank riven" rows gain two columns: "WM Listing / Change"',
                    ),
                ],
                footer=fmt.fmt_platform_footer(
                    platform, L("warframe.market 拍卖", "warframe.market auctions")
                ),
            )
        now = _riven_now_by_name(rt, weapons)
        now_ts = rt.get("ts") or ""
        if not cat:
            title, lines = fmt.fmt_riven_weekly(snap, zhm, now=now, now_ts=now_ts)
            return weekly_reply(
                Reply(title, lines, footer=fmt.fmt_platform_footer(platform, source))
            )
        veiled = await self.client.wm_veiled_stats(platform) if cat == "未开" else None
        title, lines = fmt.fmt_riven_type(
            cat,
            snap,
            zhm,
            page=parsed.page,
            page_size=self.page_size,
            veiled_wm=veiled,
            now=now,
            now_ts=now_ts,
        )
        if cat == "未开" and veiled:
            return Reply(
                title,
                lines,
                footer=fmt.fmt_platform_footer(
                    platform,
                    L(
                        f"warframe.market 成交（{requested_platform}）",
                        f"warframe.market trades ({requested_platform})",
                    ),
                ),
            )
        return weekly_reply(Reply(title, lines, footer=fmt.fmt_platform_footer(platform, source)))

    async def _h_trend(self, parsed, event, platform) -> Reply:
        """物品价格趋势（48h 逐小时 + 90d 逐日）。"""
        name = parsed.content_str.strip()
        if not name:
            return err_card(
                L("没有写物品名", "No item name given"),
                L(
                    "按 趋势 物品名 的格式重发，例如「趋势 膛线」「趋势 绝路Prime」",
                    'Resend as trend <item>, e.g. "trend Serration" or "trend Rubico Prime"',
                ),
                (L("用法", "Usage"), L("趋势 物品名（也支持「wm趋势 物品名」）", "trend <item>")),
                title=L("价格趋势", "Price Trend"),
            )
        item = await self.client.resolve_wm_item(name)
        if not item:
            return await self._wm_suggest(name)
        stats = await self.client.wm_statistics(item["url_name"], platform)
        summary = self.client.summarize_stats(stats)
        display = _disp_name(item)
        title, lines = fmt.fmt_trend(display, stats, summary)
        return Reply(
            title,
            lines,
            footer=fmt.fmt_platform_footer(
                platform, L("warframe.market 统计", "warframe.market statistics")
            ),
        )

    async def _h_rm(self, parsed, event, platform) -> Reply:
        reply = await self._h_wr(parsed, event, platform)
        # 2026-09-14 实测：riven.market 后端（Firebase riven-market）已停用
        # （423 "database has been deactivated"，拍卖页 404），站点只剩静态页，
        # 没有可用 API。紫卡数据统一走 warframe.market 的紫卡拍卖接口，
        # 这行提示同步改掉，免得用户以为只是"没接线"。
        if reply.title:
            reply.footer = L(
                "riven.market 已停服（后端数据库停用），紫卡数据走 warframe.market 拍卖",
                "riven.market is offline (backend database deactivated); riven data comes from warframe.market auctions",
            )
        return reply

    @staticmethod
    def _rolls_ok(a: dict, q) -> bool:
        """洗数硬条件（放宽词条匹配时单独保留）。"""
        r = fmt._riven_rolls(a)
        if q.rerolls_min is not None and r < q.rerolls_min:
            return False
        if q.max_rerolls is not None and r > q.max_rerolls:
            return False
        return True

    @staticmethod
    def _auction_match(
        a: dict, q, neg_set: set, pos_set: Optional[set] = None, *, ignore_status: bool = False
    ) -> bool:
        item = a.get("item", {}) or {}
        attrs = item.get("attributes") or []
        pos = [at for at in attrs if at.get("positive")]
        neg = [at for at in attrs if not at.get("positive")]
        # 价格本地过滤（2026-10-01）：WM auctions/search 实测忽略 price_min/price_max
        # 参数（服务端返回与基线逐字节同构）⇒ 此前「1000p 以内」这类条件静默不生效。
        # 拍卖一口价优先（buyout_policy=direct 恒有 buyout），无 buyout 用起始价兜底。
        price = a.get("buyout_price")
        if price is None:
            price = a.get("starting_price") or 0
        if q.max_price is not None and price > q.max_price:
            return False
        if q.min_price is not None and price < q.min_price:
            return False
        if q.forbid_negative and neg:
            return False
        if q.require_negative and not neg:
            return False
        if q.positive_count is not None and len(pos) != q.positive_count:
            return False
        if q.negative_count is not None and len(neg) != q.negative_count:
            return False
        if pos_set:
            urls = {at.get("url_name") for at in pos}
            if not pos_set <= urls:
                return False
        if neg_set:
            nurls = {at.get("url_name") for at in neg}
            if not neg_set <= nurls:
                return False
        rerolls = fmt._riven_rolls(a)  # WM 字段名是 re_rolls，取错会恒为 0
        if q.rerolls_min is not None and rerolls < q.rerolls_min:
            return False
        if q.max_rerolls is not None and rerolls > q.max_rerolls:
            return False
        if ignore_status:
            # 放宽「在线状态」单独一档（词条条件仍然全保留）——完全匹配但卖家
            # 离线，也比「词条不匹配的在线单」值得排在前面（2026-09-24）。
            return True
        status = (a.get("owner", {}) or {}).get("status")
        # ★ 2026-10-08（用户口径）：**默认不再把离线挂单藏起来** ——
        #   「即使某个词条的在线挂单只有一张，也要把离线的也列出来」。旧行为只在
        #   「完全匹配的在线单为 0」时才走 relaxed=offline 兜底 ⇒ 像「伯斯顿 + 弱点暴击几率」
        #   那种「唯一一张还离线」的场景直接空卡。现在默认（recent/在线）= 全部状态，
        #   靠渲染层「在线优先」把离线排到后面并标 ⚫离线；只有显式「最新」才只留游戏中。
        if q.status == "latest" and status != "ingame":
            return False
        return True

    async def _h_ducats(self, parsed, event, platform) -> Reply:
        """杜卡德垃圾榜：按「杜卡德/白金」排序，数据取自 WM 官方计算器。

        以前是逐项查订单自己算，只能抽样十几个，排名和官网对不上；
        现在直接接 WM 的 tools/ducats，与网页版同一份数据。
        """
        pass

        tier = (
            parsed.preset
            or next(
                (
                    _DUCAT_TIER_ALIAS_EN.get(t.lower(), t)
                    for t in parsed.content
                    if t in ("金", "银", "铜") or t.lower() in _DUCAT_TIER_ALIAS_EN
                ),
                None,
            )
            or (
                "金"
                if parsed.command_raw in ("金垃圾",)
                else "银"
                if parsed.command_raw in ("银垃圾",)
                else "铜"
                if parsed.command_raw in ("铜垃圾",)
                else "金"
            )
        )
        want = getattr(fmt, "_DUCAT_TIER", {}).get(tier, {}).get("values") or (100,)

        board = await self.client.ducats_board()
        if board:
            pool = [r for r in board if r["ducats"] in want]
        else:
            pool = await self._ducats_fallback(tier, want, platform)
        total = len(pool)
        page_size = self.page_size
        pages = max(1, (total + page_size - 1) // page_size)
        page = max(1, min(parsed.page, pages))
        chunk = pool[(page - 1) * page_size : page * page_size]
        title, lines = fmt.fmt_ducat_junk(tier, chunk, page=page, pages=pages, total=total)
        return Reply(
            title,
            lines,
            footer=fmt.fmt_platform_footer(
                platform,
                L("杜卡德/白金 越高越值得换", "Higher Ducats/Platinum is better value"),
            ),
        )

    async def _ducats_fallback(self, tier: str, want: tuple, platform: str) -> list[dict]:
        """tools/ducats 不可用时的兜底：自己查订单算，样本有限。

        Args:
            tier: 档位，仅用于日志与提示。
            want: 该档位对应的杜卡德值集合。
            platform: 平台码。

        Returns:
            与 ``ducats_board`` 同结构的榜单。
        """
        wm_items = await self.client.wm_items()
        cands = [x for x in wm_items if (x.get("ducats") or 0) in want]
        cands.sort(key=lambda x: x.get("url_name", ""))
        if not cands:
            return []
        step = max(1, len(cands) // 40)  # 兜底才抽样，正路走全量榜单
        pool = cands[::step][:40]
        results = await self._gather([self.client.wm_orders(x["url_name"], platform) for x in pool])
        rows = []
        for it, res in zip(pool, results):
            if isinstance(res, Exception) or not isinstance(res, tuple):
                continue
            sells = [
                o
                for o in res[0]
                if o.get("order_type") == "sell"
                and o.get("platform", platform) == platform
                and (o.get("platinum") or 0) > 0
            ]
            if not sells:
                continue
            cheapest = min(o["platinum"] for o in sells)
            ducats = it.get("ducats") or 0
            rows.append(
                {
                    "name": _disp_name(it),
                    "ducats": ducats,
                    "dpp": ducats / cheapest,
                    "dpp_wa": 0.0,
                    "plat": float(cheapest),
                    "volume": 0,
                }
            )
        rows.sort(key=lambda r: -r["dpp"])
        return rows

    @staticmethod
    def _junk_list(tier: str) -> list:
        try:
            data = json.loads(JUNK_FILE.read_text(encoding="utf-8"))
            return data.get(tier) or []
        except Exception:  # noqa: BLE001
            return []
