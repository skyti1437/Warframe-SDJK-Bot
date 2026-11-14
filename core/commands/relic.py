# -*- coding: utf-8 -*-
"""遗物指令域（D5 自 main.py 迁入，方法体逐字未改）。

覆盖路由键 3 项：relic / parts / openrelic；模块级 _relic_tier_en（档位表
统一真源封装）与 RELIC_USAGE（用法串唯一真源）随迁。
子包纪律：不 import astrbot（事件对象鸭子类型）。
"""

from __future__ import annotations

import json
import re

from .. import drops as drops_db
from .. import formatters as fmt
from .. import matching
from ..i18n import L, is_en
from ..logging_compat import logger
from ..parser import COMMAND_ALIASES, COMMAND_ALIASES_EN, TIER_CN, parse
from .base import PLUGIN_DIR, Reply, err_card


def _en_name_to_zh(q: str) -> str:
    """英文物品/武器名 → 官方中文（复用双语表；C1，2026-10-03）。

    混合写法也认：「afentis prime 蓝图」→「圣英 Prime 蓝图」（英文段查表、
    中文段原样保留）。查不到返回空串（调用方保持原路径，不阻断）。
    """
    toks = (q or "").split()
    for k in range(len(toks), 0, -1):
        head = " ".join(toks[:k])
        if any("\u4e00" <= c <= "\u9fff" for c in head):
            continue  # 含中文的段不查英文侧（避免误换）
        r = matching.bilingual_lookup(head, limit=1)
        if r.get("hits"):
            zh = r["hits"][0][1]
            rest = " ".join(toks[k:])
            return (zh + (" " + rest if rest else "")).strip()
    return ""


def _en_part_to_inv_key(q: str, inv_keys) -> str:
    """英文部件名 → 部件反查表（relic_inverse）里的中文键；认不出返回空串。

    反查表只有中文键（「月神 Prime 枪管」「Mesa Prime 机体蓝图」），双语名表里又没有部件名，
    所以把句尾的英文部件词（与 wm 同一张 ``parser.WM_PART_EN``）换成中文部件词，
    物品名部分走双语名表（战甲名官方简中不翻译，原样保留），最后必须真能命中反查键才算：
    不在表里的组合（Kavasa Prime Band 之类）不猜。
    """
    from ..parser import WM_PART_EN

    toks = (q or "").split()
    if len(toks) < 2 or any("\u4e00" <= c <= "\u9fff" for c in q):
        return ""
    low = [t.lower() for t in toks]
    # 句尾部件词：先试两词（upper limb），再试一词；`Chassis Blueprint` 这类「部件 + Blueprint」只取前一个
    part_zh = ""
    cut = len(toks)
    if len(toks) >= 3 and low[-1] == "blueprint" and low[-2] in WM_PART_EN:
        cut = len(toks) - 2
        part_zh = WM_PART_EN[low[-2]]
    elif len(toks) >= 3 and f"{low[-2]} {low[-1]}" in WM_PART_EN:
        cut = len(toks) - 2
        part_zh = WM_PART_EN[f"{low[-2]} {low[-1]}"]
    elif low[-1] in WM_PART_EN:
        cut = len(toks) - 1
        part_zh = WM_PART_EN[low[-1]]
    if not part_zh or cut < 1:
        return ""
    base_en = " ".join(toks[:cut])
    norm_keys = {matching.normalize_name(k): k for k in inv_keys}
    for base in (base_en, base_en + " Prime"):
        hits = matching.bilingual_lookup(base, limit=1).get("hits")
        base_zh = hits[0][1] if hits else base  # 战甲名等官方简中保留英文
        pre = matching.normalize_name(base_zh) + matching.normalize_name(part_zh)
        found = [k for nk, k in norm_keys.items() if nk.startswith(pre)]
        if len(found) == 1:
            return found[0]
        # 「头部」「系统」「机体」后面还跟着 蓝图 / 神经光元 等字，多命中时取最短的完整名
        if found:
            found.sort(key=len)
            if len(found[0]) < len(found[1]) if len(found) > 1 else True:
                return found[0]
    return ""


# DE 官方英文掉落表原文；旧索引未保存资产键的蓝图/部件。
_RELIC_REWARD_EN = {
    "石晶之花 Prime 枪托": "Steflos Prime Stock",
    "石晶之花 Prime 枪机": "Steflos Prime Receiver",
    "石晶之花 Prime 枪管": "Steflos Prime Barrel",
    "闪劫 Prime 握柄": "Corufell Prime Handle",
    "闪劫 Prime 枪托": "Corufell Prime Stock",
    "闪劫 Prime 枪机": "Corufell Prime Receiver",
    "闪劫 Prime 蓝图": "Corufell Prime Blueprint",
    "陨蜓 Prime 外甲蓝图": "Odonata Prime Harness Blueprint",
    "陨蜓 Prime 机翼蓝图": "Odonata Prime Wings Blueprint",
    "陨蜓 Prime 系统蓝图": "Odonata Prime Systems Blueprint",
}


def _item_en(zh: str) -> str:
    """英文模式下把遗物表里的中文物品名换成官方英文名（双语表精确命中才换，否则原样）。

    ★ 遗物索引（relic_index / relic_inverse）只有中文名；奖励行按名字查杜卡德 / 白金价，
      价格表在英文模式下以英文名为键，不换名就查不到价格列。
    """
    if not is_en() or not zh:
        return zh
    if zh in _RELIC_REWARD_EN:
        return _RELIC_REWARD_EN[zh]
    official = fmt._pool_name_en(zh)
    if official != zh:
        return official
    hits = matching.bilingual_lookup(zh, limit=1).get("hits")
    if hits:
        return hits[0][0]
    return _part_zh_to_en(zh)


# 反查键末尾的中文部件词 → 英文（与 parser.WM_PART_EN 同一张表反向；战甲部件键带「蓝图」尾）
_INV_PART_EN = {
    "头部神经光元蓝图": "Neuroptics Blueprint",
    "机体蓝图": "Chassis Blueprint",
    "系统蓝图": "Systems Blueprint",
    "蓝图": "Blueprint",
}


def _part_zh_to_en(zh: str) -> str:
    """双语名表没有的部件名（「月神 Prime 枪管」「Mesa Prime 机体蓝图」）→ 英文；认不出原样返回。"""
    from ..parser import WM_PART_EN

    for tail, en_tail in [*_INV_PART_EN.items(), *((v, k.title()) for k, v in WM_PART_EN.items())]:
        if zh.endswith(" " + tail) or (zh.endswith(tail) and tail in _INV_PART_EN):
            head = zh[: -len(tail)].strip()
            if not head:
                break
            hits = matching.bilingual_lookup(head, limit=1).get("hits")
            return f"{hits[0][0] if hits else head} {en_tail}"
    return zh


def _relic_tier_en() -> dict:
    """中文档位 → 掉落表英文键（小写）。真源 core/parser.TIER_CN（含先锋/全能）。

    ★ 遗物相关的档位表**统一走这里**——曾在 3 处各自硬编（_norm_relic / 列表卡 /
    单查状态），2026-09 新增「先锋」档时全漏，用户查「遗物 先锋 C1」显示未找到。
    """
    # TIER_CN 走**模块级**导入（顶部 try 双分支）—— 2026-09-26 修：
    #   原先这里是函数内裸 `from core.parser import TIER_CN`，服务器以包成员加载
    #   （`data.plugins.astrbot_plugin_warframe_sdjkbot.main`）时 `core` 不可解析 ⇒
    #   遗物全套指令（出库/入库/列表/单查）直接 `No module named 'core'`。
    return {k: v.lower() for k, v in TIER_CN.items() if not k.isascii()}


# 遗物指令的用法串 —— **唯一真源**（2026-09-26 抽常量：原先空参数处与未命中处各写一份，必然漂移）。
# 文案逐字保留自原实现；纯文本、<=3 行，不含卡片渲染（避开字体子集/豆腐块风险）。
RELIC_USAGE = (
    "用法：遗物 后纪A2（查奖励）｜ 遗物 绝路 枪机（部件反查出处）｜"
    " 遗物 出库（当前可掉落）｜ 遗物 入库（已入库、不可刷取）"
)


# 英文用法串（与 RELIC_USAGE 一一对应）
RELIC_USAGE_EN = (
    "relic Axi A2 (rewards) | parts Rubico Prime Barrel (part sources) | "
    "relic unvaulted (currently droppable) | relic vaulted (vaulted, not farmable)"
)

# 英文参数写法 → 中文内部写法。只在遗物 / 开核桃的参数解析处认，不进通用别名表。
_RELIC_LIST_EN = {"unvaulted": "出库", "vaulted": "入库", "list": "列表", "all": "全部"}
_EN_TIER_ZH = {k.lower(): v for k, v in TIER_CN.items() if k.isascii()}
_EN_TIER_ZH_REV = {v: k for k, v in TIER_CN.items() if k.isascii()}
_OPEN_EN = {
    "endless": "无尽",
    "quick": "速刷",
    "fast": "速刷",
    "unvaulted": "未入库",
    "droppable": "未入库",
    "vaulted": "已入库",
    "cheap": "低价",
    "low": "低价",
    "expensive": "高价",
    "high": "高价",
}
# 速刷任务类型的英文名（任务类型来自世界状态，英文模式下是 DE 官方英文名）
_QUICK_EN = {"Capture", "Exterminate", "Extermination", "Sabotage", "Rescue", "Spy"}


class RelicCommands:
    """Mixin：遗物 / 开核桃 handler（挂载于 main.WarframeSDJK）。"""

    def _relic_db(self) -> tuple[dict, dict]:
        cache = getattr(self, "_relic_cache", None)
        if cache:
            return cache
        base = PLUGIN_DIR / "core" / "data"
        try:
            idx = json.loads((base / "relic_index.json").read_text(encoding="utf-8"))
            inv = json.loads((base / "relic_inverse.json").read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            idx, inv = {}, {}
        self._relic_cache = (idx, inv)
        return idx, inv

    @staticmethod
    def _norm_relic(q: str) -> str:
        """ "后纪A2 / 先锋C1 / 安魂 I / Axi A2" -> "后纪 Axi A2" 简化归一。

        ★ 档位表一律取 core/parser.TIER_CN（含 Omnia/Vanguard）——曾本地硬编 5 档，
        2026-09 新增「先锋」档后「遗物 先锋 C1」直接「未找到」（资料会话交接）；
        代号支持 字母+数字（A2/A 2）、罗马数字（I..IV）、词式（Eterna，安魂档）。
        """
        zh2en = {k: v for k, v in TIER_CN.items() if not k.isascii()}
        q = re.sub(r"\s+", " ", q.strip().replace("纪元", ""))
        q = re.sub(r"\s+(?:relic|遗物)$", "", q, flags=re.I)
        for zh, en in zh2en.items():
            match = re.fullmatch(rf"(?:{zh}\s*(?:{en}\s+)?|{en}\s+)(.+)", q, re.I)
            if not match:
                continue
            code = re.sub(r"\s+", "", match[1])
            if m := re.fullmatch(r"([A-Za-z])(\d+)", code):
                return f"{zh} {en} {m[1].upper()}{int(m[2])}"
            if re.fullmatch(r"[IVX]+", code, re.I):
                return f"{zh} {en} {code.upper()}"
            if code.casefold() == "eterna":
                return f"{zh} {en} Eterna"
        return q

    async def _varzia_relic_keys(self, platform: str) -> set:
        """阿耶（Varzia）商店在售遗物的键前缀（``{"lith k5", …}``）。

        这些遗物同样属于「当前可获取」，但它们**不在任务掉落表里** ——
        2026-09-17 实测：古纪 K5/M7、前纪 E5、中纪 B6、后纪 H5/A12 六把
        全不在 WFCD 实时掉落表（44016 条）中。出库卡不并进来，用户就会觉得
        「缺遗物」；部件反查卡不并进来，会把能买的遗物写成「已入库」。

        取不到时返回**空集合**并打日志（不静默降级成「都在入库」）。
        """
        keys: set = set()
        try:
            vault = await self.client.prime_vault(platform)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[sdjk] 阿耶商店数据取不到，本次按「不在售」处理：%s", exc)
            return keys
        for it in vault.get("items") or []:
            if (it.get("kind") or "") != "relic":
                continue
            key = fmt.relic_en_key(it.get("name") or "")
            if key:
                keys.add(key)
        return keys

    async def _h_relic(self, parsed, event, platform) -> Reply:
        """遗物查询：遗物名 → 三槽位奖励；部件名 → 出遗物出处；
        入库/全部/列表 → 当前掉落池状态。
        """
        idx, inv = self._relic_db()
        q = parsed.content_str.strip()
        preset = (parsed.preset or "").strip()
        if preset in ("列表", "入库", "出库"):
            q = preset  # 让下面入库/出库分支生效
        if not q:
            return err_card(
                L("没有写遗物名或部件名", "No relic or part name given"),
                L("按用法重发，例如「遗物 后纪A2」", 'Resend using the usage, e.g. "relic Axi A2"'),
                (L("用法", "Usage"), L(RELIC_USAGE.removeprefix("用法："), RELIC_USAGE_EN)),
                title=L("遗物查询", "Relic Lookup"),
            )
        q = _RELIC_LIST_EN.get(q.lower(), q)
        if _EN_TIER_ZH.get(q.strip().lower()):
            q = _EN_TIER_ZH[q.strip().lower()]
        # ① 列出可掉落/已入库遗物
        if q in ("全部", "列表", "入库", "出库"):
            unv = drops_db.unvaulted_relics()
            tier_en = _relic_tier_en()
            # en_key("axi v12") 用于判断该遗物是否在官方掉落池里（出库/入库）
            uv_set = set()
            for k in unv:
                parts = k.split()
                if len(parts) >= 3:
                    uv_set.add(f"{parts[0]} {parts[1]}")
            # ★ 阿耶（Varzia）商店在售的遗物同样属于「当前可获取」，但它们
            #   **不在任务掉落表里**（见 _varzia_relic_keys 的说明）。
            varzia = await self._varzia_relic_keys(platform)
            all_uv = uv_set | varzia
            unv_rows, vaulted_rows = [], []
            for k in idx:
                m = k.split()
                if len(m) < 3:
                    continue
                en_key = f"{tier_en.get(m[0], m[0].lower())} {m[2].lower()}"
                unvaulted = en_key in all_uv
                # 列表模式只按纪元分组列名字，**不再逐条查掉落位置**：
                # 那既慢（每个遗物一次查表）又会把同一批星球/节点重复贴几十遍
                # （维护者 2026-09-17：「列出一大堆重复星系没啥用阿」）。
                # 单个遗物的奖励与出处仍用「遗物 名称」查；出库卡另附
                # 「推荐刷取 / 特殊渠道」两行（见下面的 specials / farm_hints）。
                row = {"cn": k, "tier_cn": m[0], "unvaulted": unvaulted, "varzia": en_key in varzia}
                (unv_rows if unvaulted else vaulted_rows).append(row)

            # 语义：**出库 = 从金库放出 = 当前可以掉落**（unvaulted）；
            #        **入库 = 收回金库 = 当前不能刷取**（vaulted）。
            # 旧实现把两者写反了：`出库` 显示的是已下架清单，与游戏内认知相反。
            if q == "出库":
                rows = unv_rows
                title = L(
                    "遗物出库（当前可掉落）", "Relics Leaving the Vault (currently droppable)"
                )
            elif q == "入库":
                rows = vaulted_rows
                title = L(
                    "遗物入库（已入库、不可刷取）",
                    "Relics Entering the Vault (vaulted, not farmable)",
                )
            else:  # "列表"
                rows = unv_rows + vaulted_rows
                title = L(
                    f"遗物列表：当前可掉落 {len(unv_rows)}，已入库 {len(vaulted_rows)}",
                    f"Relic List: {len(unv_rows)} droppable, {len(vaulted_rows)} vaulted",
                )

            # 特殊获取渠道标记（维护者要求：「有一部分遗物只要指定位置能获取，
            # 那种单独去标记」）—— 只在出库卡算：
            #   · 阿耶商店在售 → 「仅阿耶兑换」
            #   · 虽在掉落表但出处极窄（如只在比邻星域储藏库）→「仅XX · 储藏库」
            # 入库的清单一律是「不可刷取」，标记没有意义，也省掉逐条查表开销。
            specials: dict[str, str] = {}
            if q == "出库":
                for r in rows:
                    nm = fmt.relic_cn(r["cn"])
                    if r.get("varzia"):
                        specials[nm] = L("仅阿耶兑换", "Varzia only")
                        continue
                    m = r["cn"].split()
                    if len(m) < 3:
                        continue
                    why = drops_db.special_source(
                        f"{tier_en.get(m[0], m[0].lower())} {m[2].lower()} relic"
                    )
                    if why:
                        specials[nm] = why

            title, lines = fmt.fmt_relic_by_tier(
                rows,
                title,
                page=parsed.page,
                # 列表按纪元分组并排展示，每页要装得下整个纪元；
                # self.page_size（默认 12）是给逐条带详情的卡片用的，太碎。
                # 每行 9 列后每页 90 个 ≈ 10 行，13 页降到 9 页。
                page_size=max(90, self.page_size),
                # 推荐刷取点只在出库卡给（已入库的刷不到）
                farm_hints=drops_db.farm_hints() if q == "出库" else None,
                specials=specials or None,
            )
            return Reply(title, lines, footer=fmt.fmt_platform_footer(platform))
        # ①-b 单档位词（「遗物 先锋」）→ 该档位遗物一览（复用列表卡渲染）。
        #     用户实测反馈：新档「先锋」不知道有哪些遗物，直接查档位词最自然。
        if q.strip() in _relic_tier_en():
            _sub = [k for k in idx if k.startswith(q.strip() + " ")]
            if _sub:
                unv = drops_db.unvaulted_relics()
                tier_en = _relic_tier_en()
                uv_set = set()
                for x in unv:
                    parts = x.split()
                    if len(parts) >= 3:
                        uv_set.add(f"{parts[0]} {parts[1]}")
                varzia = await self._varzia_relic_keys(platform)
                all_uv = uv_set | varzia
                rows = []
                for k in _sub:
                    m = k.split()
                    en_key = f"{tier_en.get(m[0], m[0].lower())} {m[2].lower()}"
                    rows.append(
                        {
                            "cn": k,
                            "tier_cn": m[0],
                            "unvaulted": en_key in all_uv,
                            "varzia": en_key in varzia,
                        }
                    )
                title, lines = fmt.fmt_relic_by_tier(
                    rows,
                    L(
                        f"遗物列表：{q.strip()}（{len(rows)} 把）",
                        f"Relic List: {fmt.tier_cn(_EN_TIER_ZH_REV.get(q.strip(), q.strip()))} "
                        f"({len(rows)} relic{'' if len(rows) == 1 else 's'})",
                    ),
                    page=parsed.page,
                    page_size=max(90, self.page_size),
                )
                return Reply(title, lines, footer=fmt.fmt_platform_footer(platform))
        # ② 部件优先：带空格或能直接命中部件表
        # 名称归一：去空格精确匹配；非 Prime 输入自动补 Prime 试一次（wiki 上架过的只有 Prime 系）
        # ★ C1（2026-10-03）：匹配一律走 matching.normalize_name（大小写/空格/
        #   中点/全半角无关）——线上实证「部件 阿索代prime」因裸 replace(" ","")
        #   大小写敏感而查不到（库里是「阿索代 Prime 蓝图」）。
        q_raw = q  # 用户原输入（「未找到」回显用，勿被改写带走）
        exact_relic = self._norm_relic(q)
        if exact_relic in idx:
            q = exact_relic  # 英文 Axi A2 必须先精确命中，不能与 A20/A21 做包含匹配
        q_nospace = matching.normalize_name(q)
        inv_norm = {matching.normalize_name(k): k for k in inv}
        # ★ C1：英文名 → 中文名（「afentis prime 蓝图」⇒「圣英 Prime 蓝图」），
        #   查不到不阻断（原路径继续）。
        _zh_alt = _en_part_to_inv_key(q, inv) or _en_name_to_zh(q)
        bases = [q_nospace]
        if _zh_alt:
            bases.insert(0, matching.normalize_name(_zh_alt))
        cands = []
        # 非 Prime 输入自动补 Prime：**把 prime 插到每个位置都试一遍**。
        # 旧实现只插在「第一个部件类型字（枪/机/托/蓝/图/弦/管）」之前，
        # 而武器名里本身就可能带这些字 —— 「席尔火枪枪管」会插成
        # 「席尔火Prime枪枪管」，永远查不到（用户真实输入就是不带 Prime 的）。
        # 代价只是几十次 dict 查询，从**靠后**的位置开始插（部件类型词
        # 一般在末尾：枪管/枪机/蓝图），先命中的更可能是正确切分。
        for _b in bases:
            cands.append(_b)
            if "prime" not in _b:
                for _i in range(len(_b) - 1, 0, -1):
                    cands.append(_b[:_i] + "prime" + _b[_i:])
            cands.append(_b + "prime")
        inv_key = next((inv_norm.get(c) for c in cands if inv_norm.get(c)), None)
        if inv_key is not None:
            q = inv_key
        if q not in inv:
            # ②-b 黑话兜底（2026-09-25）：「中文简称 + 部件词」→ 别名表的 WM 物品名
            #   拼部件词再查 inverse（例：「水晶p 蓝图」→ Citrine Prime 蓝图）。
            #   · 用**精确键**做前缀切分 —— alias_lookup 的双向包含算不出剩余部件词；
            #   · 黑话键可能不带 p（「水晶甲」），而用户把 p/prime 跟在黑话后面
            #     （「水晶甲p 蓝图」）→ 拼装前剥掉 rest 前导的 p/prime；
            #   · 只在直接匹配失败后兜底，不改变既有命中路径。
            #   ★ C1：两侧都用 normalize_name（大小写/空格无关）。
            _tbl = (getattr(self.client, "_aliases", None) or {}).get("wm_items") or {}
            _lower = {matching.normalize_name(k): k for k in inv}
            for _i in range(len(q_nospace) - 1, 0, -1):
                _slug = _tbl.get(q_nospace[:_i].lower())
                if not _slug:
                    continue
                _rest = re.sub(r"^(?:prime|p)(?=[\u4e00-\u9fff])", "", q_nospace[_i:], flags=re.I)
                _en = re.sub(r"_set$", "", _slug).replace("_", " ").title()
                q = _lower.get(matching.normalize_name(_en + _rest)) or q
                if q in inv:
                    break
        if (
            q not in inv
            and _zh_alt
            and matching.normalize_name(q) != matching.normalize_name(_zh_alt)
        ):
            # ★ C1：英文名未直中 ⇒ 用解析出的中文名**接管后续路径**——
            #   「部件 athodai」与「部件 阿索代 Prime」都给出同一份候选
            #   （阿索代 Prime 蓝图/枪管/枪机）。
            q = _zh_alt
        if q in inv:
            origins = inv[q]
            # 「能不能获取」三态：在掉落表 / 仅阿耶在售 / 已入库。
            # ★ 必须先经 fmt.relic_en_key 归一：relic_inverse 里存的遗物名是
            #   「后纪 Axi A20」这种中文纪元写法，而掉落库键是「axi a20」——
            #   直接拿名字去比对，34 把可掉落遗物会全被判成「已入库」
            #   （2026-09-18 做状态标记时实测踩到）。
            droppable = drops_db.droppable_keys()
            varzia = await self._varzia_relic_keys(platform)
            rows = []
            for o in origins:
                key = fmt.relic_en_key(o.get("relic") or "")
                if key and key in droppable:
                    state = "drop"
                elif key and key in varzia:
                    state = "varzia"
                else:
                    state = "vaulted"
                rows.append({"relic": o.get("relic"), "rarity": o.get("rarity"), "state": state})
            title, lines = fmt.fmt_relic_piece(_item_en(q), rows, farm_hints=drops_db.farm_hints())
            return Reply(title, lines, footer=fmt.fmt_platform_footer(platform))
        # ② 归一化遗物名
        nq = self._norm_relic(q)
        hit = idx.get(nq) or idx.get(q)
        if not hit:
            # 模糊：任一槽位包含 q
            # ★ C1：归一化包含（大小写/空格/中点无关）
            _nq = matching.normalize_name(q)
            cand = [r for r in idx if _nq in matching.normalize_name(r)]
            if len(cand) == 1:
                hit = idx[cand[0]]
                nq = cand[0]
        if hit:
            # 槽位奖励 + 杜卡德/白金/杜·p（价格来自 WM 杜卡德计算器同源榜单，
            # 不可用时自动省略价格列，不影响奖励本体展示）
            try:
                prices = await self.client.ducats_price_map()
            except Exception:  # noqa: BLE001
                prices = {}
            slots = []
            for rarity in ("常见", "罕见", "稀有"):
                for nm in hit.get(rarity) or []:
                    slots.append({"rarity": rarity, "name": _item_en(nm)})
            for rarity, items in hit.items():
                if rarity in ("常见", "罕见", "稀有"):
                    continue
                for nm in items or []:
                    slots.append({"rarity": rarity, "name": _item_en(nm)})
            title, lines = fmt.fmt_relic_rewards(nq, slots, prices)
            status = ""
            varzia_status = False  # 状态是「阿耶商店可兑换」（比活动 / 特殊渠道说明更强）
            tier_key, k = "", ""
            unv = drops_db.unvaulted_relics()
            en = nq.split()
            if len(en) >= 3:
                tier_en = _relic_tier_en()
                tier_key = tier_en.get(en[0], en[0].lower())
                k = f"{tier_key} {en[2].lower()}"
                if tier_key not in {x.split()[0] for x in unv}:
                    # 官方任务掉落表里就没有这个档位（先锋/全能这类新档、安魂系
                    # 特殊渠道）——不能按「已入库」误导（WFCD 掉落表快照不含
                    # vanguard 是数据事实，2026-09-25 资料会话交接确认）。
                    status = L(
                        "该档位不在官方任务掉落表（新档位/特殊渠道）",
                        "This tier is not in the official mission drop table (new tier / special channels)",
                    )
                else:
                    varzia = await self._varzia_relic_keys(platform)
                    if any(x.startswith(k + " ") for x in unv):
                        status = L(
                            "该遗物当前可掉落（出库中）",
                            "This relic is currently droppable (unvaulted)",
                        )
                    elif k in varzia:
                        status = L(
                            "该遗物可在阿耶商店兑换（当前可获取）",
                            "This relic can be exchanged at Varzia (currently obtainable)",
                        )
                        varzia_status = True
                    else:
                        status = L(
                            "该遗物已入库，当前不可刷取",
                            "This relic is vaulted and cannot be farmed right now",
                        )
            source_key = f"{tier_key} {en[2].lower()} relic" if len(en) >= 3 else ""
            notes = drops_db.relic_source_notes(source_key)
            # 「阿耶商店当前可兑换」是更强的当前可获取结论，不被活动 / 特殊渠道说明覆盖（Claude 审阅补）
            if not any(x.startswith(k + " ") for x in unv) and not varzia_status:
                if notes:
                    status = L("特殊渠道获取（见来源说明）", "Special channels (see source notes)")
                elif source_key in (drops_db.load().get("relic_drops") or {}):
                    status = L(
                        "仅见于活动掉落表（以活动开放情况为准）",
                        "Only seen in event drop tables (depends on whether the event is live)",
                    )
            sources = drops_db.relic_source_lines(source_key)
            if sources:
                # 先给特殊渠道和一处活动样例，再列常规地点，避免历史活动挤掉可刷节点。
                event_sample = [x for x in sources if x.startswith("活动 · ") and x not in notes][
                    :1
                ]
                regular = [
                    x for x in sources if not x.startswith(("活动 · ", "奸商 · ", "敌人 · "))
                ]
                shown = list(dict.fromkeys([*notes, *event_sample, *regular, *sources]))[:8]
                lines.extend(
                    [
                        L("◆ 获取来源", "◆ Sources"),
                        *[f"· {drops_db.place_en(x) if is_en() else x}" for x in shown],
                    ]
                )
                if len(sources) > len(shown):
                    _more = len(sources) - len(shown)
                    lines.append(
                        L(
                            f"※ 另有 {_more} 处来源未列出",
                            f"※ {_more} more source{'' if _more == 1 else 's'} not shown",
                        )
                    )
            if status:
                # 状态是卡首「答案」：放最前，不再挂在注脚后面（原来是一条孤零零的 ◆ 行）
                lines.insert(0, L(f"◆ 状态：{status}", f"◆ Status: {status}"))
            return Reply(title, lines, footer=fmt.fmt_platform_footer(platform))
        # ③ 视为部件名模糊（词序无关：把 q 的 token 任意拼接匹配）
        #   ★ C1：两侧归一化（大小写/空格/中点无关）——「阿索代prime」曾因
        #   裸 replace(" ","") 大小写敏感而在此静默落空。
        qn = matching.normalize_name(q)
        fz = [k for k in inv if qn in matching.normalize_name(k)]
        # 词序反转：绝路枪机 -> 枪机绝路
        if not fz:
            fz = [
                k
                for k in inv
                if "".join(sorted(qn)) == "".join(sorted(matching.normalize_name(k)[: len(qn)]))
            ]
        fz = list(dict.fromkeys(fz))[:6]
        if len(fz) == 1:
            parsed2 = parse(f"遗物 {fz[0]}")
            parsed2.command = "relic"
            return await self._h_relic(parsed2, event, platform)
        # 顺序固定：未找到 → 你是不是想找 → 用法
        return err_card(
            L(f"未找到「{q_raw}」", f'Not found: "{q_raw}"'),
            L("照候选或用法改写后重发", "Resend using a suggestion or the usage"),
            (
                L("你是不是想找", "Did you mean"),
                L("、", ", ").join(_item_en(k) for k in fz[:3]),
            )
            if fz
            else "",
            (L("用法", "Usage"), L(RELIC_USAGE.removeprefix("用法："), RELIC_USAGE_EN)),
            title=L("遗物查询", "Relic Lookup"),
        )

    async def _h_parts(self, parsed, event, platform) -> Reply:
        """部件反查：部件名 → 出自哪些遗物及槽位（走遗物反查）。"""
        return await self._h_relic(parsed, event, platform)

    async def _h_openrelic(self, parsed, event, platform) -> Reply:
        """开核桃：当前裂隙 + 各纪元是否仍在掉落池（未入库）。"""
        toks = list(parsed.content or [])
        if parsed.preset:
            toks.insert(0, parsed.preset)
        toks = [_OPEN_EN.get(str(t).lower(), t) for t in toks]  # 英文写法并入中文内部键
        quick_only = any(t in ("速刷", "快") for t in toks)
        endless_only = any(t == "无尽" for t in toks)
        state = next((t for t in toks if t in ("未入库", "已入库", "可掉落")), "")
        if state == "可掉落":
            state = "未入库"
        price = next((t for t in toks if t in ("低价", "高价")), "")
        fissures = await self.client.fissures(platform)
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        act = [
            f
            for f in fissures
            if fmt.parse_iso(f.get("expiry", "")) and fmt.parse_iso(f["expiry"]) > now
        ]
        unv = drops_db.unvaulted_relics()

        def tier_key(tier: str) -> str:
            return {
                "Lith": "lith",
                "Meso": "meso",
                "Neo": "neo",
                "Axi": "axi",
                "Requiem": "requiem",
                "Omnia": "omnia",
                "Vanguard": "vanguard",
            }.get(tier, "")

        tier_unvaulted: dict[str, int] = {}
        for k, cn in unv.items():
            tier_unvaulted.setdefault(k.split()[0], 0)
            tier_unvaulted[k.split()[0]] += 1

        # 速刷 = 任务流程短、结算快的类型。**不含移动防御 / 生存 / 防御 / 拦截**
        # —— 那些要站桩守点，维护者明确说「移动防御算不上速刷」。
        QUICK = {"捕获", "歼灭", "破坏", "救援", "间谍"}
        rows = []
        for f in act:
            # 九重天风暴的类型来自节点表英文名（Skirmish / Volatile …），与裂隙卡同一张译表
            mt = (
                fmt._RAILJACK_TYPE_CN.get((f.get("missionType") or "").lower(), "")
                if f.get("isStorm") and not is_en()
                else ""
            ) or fmt.mission_cn(f.get("missionType", ""))
            tier = f.get("tier", "")
            tk = tier_key(tier)
            cnt = tier_unvaulted.get(tk, 0)
            kind = []
            if f.get("isHard"):
                kind.append(L("钢铁", "Steel Path"))
            rows.append(
                {
                    "node": f.get("node", "?"),
                    "type": mt,
                    "tier": tier,
                    "tier_cn": fmt.tier_cn(tier) if tier and tier != "?" else "?",
                    "unv": cnt,
                    "quick": _fissure_quick(f, mt, QUICK),
                    # 九重天的生存 / 防御同样逐波结算遗物（wiki Void Fissure 更新记录：
                    # 「Void Storm Survival … play through the entire wave」「unlocking 2 or more Relics」）
                    "endless": mt in _ENDLESS_MISSIONS or (is_en() and mt in _ENDLESS_MISSIONS_EN),
                    "kind": "/".join(kind),
                    "faction": fmt._fissure_faction(f),
                    "expiry": f.get("expiry", ""),
                    "storm": bool(f.get("isStorm")),
                    "srank": _storm_rank(f) if f.get("isStorm") else 0,
                }
            )
        if quick_only:
            rows = [r for r in rows if r["quick"]]
        if endless_only:
            rows = [r for r in rows if r["endless"]]
        if state == "未入库":
            rows = [r for r in rows if r["unv"] > 0]
        elif state == "已入库":
            rows = [r for r in rows if r["unv"] == 0]
        # 地面裂隙（含钢铁）：同纪元 无尽 → 速刷 → 其它。批量开遗物玩家都挂无尽（英文 wiki Void
        # Fissure「Endless Missions」：每轮可换一把遗物接着开，第 5 / 10 轮另送随机遗物；维护者 11-09 定）。
        # 九重天单独一节、单独排序（见 _storm_rank），不和地面裂隙混排。
        ground = [r for r in rows if not r["storm"]]
        storms = [r for r in rows if r["storm"]]
        ground.sort(
            key=lambda r: (
                fmt._TIER_ORDER.get(r["tier"], 99),
                not r["endless"],
                not r["quick"],
                -r["unv"],
            )
        )
        storms.sort(key=lambda r: (fmt._TIER_ORDER.get(r["tier"], 99), r["srank"], -r["unv"]))
        if price:
            for _lst in (ground, storms):
                _lst.sort(key=lambda r: r["unv"], reverse=(price == "高价"))
        # 不翻页：全部场次放同一张卡（维护者要求「那几页内容都放一张截图上」）。
        total = len(ground) + len(storms)
        cap = fmt._ALL_ROWS_CAP - 4  # 两节各多一行标题 + 表头
        ground_shown = ground[:cap]
        storm_shown = storms[: max(0, cap - len(ground_shown))]
        chunk = ground_shown + storm_shown
        S = "　"
        lines = []

        def _row_line(r: dict) -> str:
            tag = f"[{r['tier_cn']}]"
            mtype = r["type"] if r["type"] and r["type"] != "?" else ""
            marks = " ".join(
                x
                for x in (
                    *(r["kind"].split("/") if r["kind"] else ()),
                    L("无尽", "Endless") if r["endless"] else "",
                    L("速刷", "Quick") if r["quick"] else "",
                )
                if x
            )
            return f"· {tag}{S}{mtype}{S}{r['node']}{S}{r['faction']}{S}{marks}{S}" + fmt._left(
                fmt.countdown(r["expiry"]), "剩"
            )

        if chunk:
            # ★ 2026-10-11 第二步改版：单张对齐表「纪元　类型　节点　派系　标记　剩余」，与裂隙卡同形；
            #   「可掉落 N 种」是**纪元级**数字（同纪元每行相同）⇒ 不再逐行重复，汇总进卡首答案行。
            per_tier: dict[str, tuple[str, int]] = {}
            for r in chunk:
                per_tier.setdefault(r["tier"], (r["tier_cn"], r["unv"]))
            lines.append(
                L("◆ 各纪元可掉落遗物：", "◆ Droppable relics per era: ")
                + " · ".join(
                    L(f"{cn} {n} 种", f"{cn} {n}")
                    for _t, (cn, n) in sorted(
                        per_tier.items(), key=lambda kv: fmt._TIER_ORDER.get(kv[0], 99)
                    )
                )
            )
            n_quick = sum(1 for r in ground if r["quick"])
            n_endless = sum(1 for r in ground if r["endless"])
            lines.append(
                L(
                    f"◆ 可开场次：{total} 场｜无尽 {n_endless} 场｜速刷 {n_quick} 场｜九重天 {len(storms)} 场",
                    f"◆ Crackable fissures: {total}｜Endless {n_endless}｜Quick {n_quick}"
                    f"｜Void Storms {len(storms)}",
                )
            )
        if ground_shown:
            lines += [
                L("◆ 裂隙列表（按纪元排序）", "◆ Fissure List (sorted by era)"),
                L(f"{S}纪元{S}类型{S}节点{S}派系{S}标记", f"{S}Era{S}Type{S}Node{S}Faction{S}Tags"),
                *(_row_line(r) for r in ground_shown),
            ]
        if storm_shown:
            lines += [
                L("◆ 九重天裂隙（航道星舰）", "◆ Void Storm Fissures (Railjack)"),
                L(f"{S}纪元{S}类型{S}节点{S}派系{S}标记", f"{S}Era{S}Type{S}Node{S}Faction{S}Tags"),
                *(_row_line(r) for r in storm_shown),
            ]
        if not lines:
            lines = [
                L(
                    "◆ 结果：当前条件下没有可开的裂隙",
                    "◆ Result: no crackable fissures under the current filters",
                )
            ]
        if ground_shown or not storm_shown:
            lines.append(
                L(
                    "※ 按纪元排序（古纪→前纪→中纪→后纪→安魂→全能），同纪元无尽在前；"
                    "无尽 = 每轮能换一把遗物接着开，第 5 / 10 轮还送遗物；"
                    "速刷 = 捕获 / 歼灭 / 破坏 / 救援 / 间谍，只开一两把时挑它",
                    "※ Sorted by era (Lith → Meso → Neo → Axi → Requiem → Omnia), endless first within an era; "
                    "Endless = crack a new relic every rotation, bonus relics at rounds 5 / 10; "
                    "Quick = Capture / Exterminate / Sabotage / Rescue / Spy, for just one or two",
                )
            )
        if storm_shown:
            lines.append(
                L(
                    "※ 九重天：做完可在导航台直接接下一场，不用回船坞，结算还多一份奖励；"
                    "同纪元按 地球 / 土星前哨战（全程太空最快）→ 其它格里尼尔 → Corpus 星域（要登舰，慢）排",
                    "※ Void Storms: start the next one from the navigation console without docking, plus an extra "
                    "reward; within an era: Earth / Saturn Proxima Skirmish (fastest, all in space) → other Grineer "
                    "→ Corpus Proxima (needs boarding, slow)",
                )
            )
        if total > len(chunk):
            lines.append(
                L(
                    f"※ 共 {total} 场，本卡只列前 {len(chunk)} 场（按上面口径排序）",
                    f"※ {total} total; this card lists the first {len(chunk)} (sorted as above)",
                )
            )
        _state_en = {"未入库": "Unvaulted", "已入库": "Vaulted"}.get(state, state)
        _price_en = {"低价": "Low price", "高价": "High price"}.get(price, price)
        flt = L("，", ", ").join(
            x
            for x in [
                L("速刷", "Quick") if quick_only else "",
                L("无尽", "Endless") if endless_only else "",
                L(state, _state_en),
                L(price, _price_en),
            ]
            if x
        )
        title = L(
            f"开核桃建议（共{total}场）" + (f"　筛选：{flt}" if flt else ""),
            f"Crack Relics Tips ({total} total)" + (f"　Filter: {flt}" if flt else ""),
        )
        return Reply(title, lines, footer=fmt.fmt_platform_footer(platform))

    async def _h_farm(self, parsed, event, platform) -> Reply:
        """刷取规划：Prime 物品 → 各部件的遗物 → 最少开哪几把 → 遗物去哪刷 → 现在能开的裂隙。"""
        q = parsed.content_str.strip()
        usage = (L("用法", "Usage"), L(FARM_USAGE.removeprefix("用法："), FARM_USAGE_EN))
        title = L("刷取规划", "Farming Plan")
        # 「刷」也是日常用语，且指令词出现在句中任意位置都会命中：
        # 指令词不在句首（「今天 刷 什么」）又认不出物品时，按闲聊处理，不回报错卡。
        raw_toks = (getattr(parsed, "raw", "") or "").split()
        chat_like = bool(raw_toks) and raw_toks[0].lower() not in (
            _FARM_WORDS | {(parsed.command_raw or "").lower()}
        )
        if not q:
            if chat_like:
                return None
            return err_card(
                L("没有写要刷的物品", "No item given"),
                L(
                    "按用法重发，例如「刷 Wisp Prime」",
                    'Resend using the usage, e.g. "farm Wisp Prime"',
                ),
                usage,
                title=title,
            )
        _idx, inv = self._relic_db()
        aliases = (getattr(self.client, "_aliases", None) or {}).get("wm_items") or {}
        hit = _farm_resolve(q, inv, aliases)
        if hit is None:
            if chat_like:
                return None
            sugg = _farm_suggest(q, inv)
            return err_card(
                L(f"未找到「{q}」的遗物部件", f'No relic parts found for "{q}"'),
                L(
                    "「刷」只查遗物里出的 Prime 部件；照候选或用法改写后重发",
                    '"farm" only covers Prime parts from relics; resend using a suggestion or the usage',
                ),
                (L("你是不是想找", "Did you mean"), L("、", ", ").join(_item_en(s) for s in sugg))
                if sugg
                else "",
                usage,
                title=title,
            )
        base, keys, bad_filter = hit
        if bad_filter:
            have = L("、", ", ").join(_farm_part_label(k) for k in keys)
            return err_card(
                L(
                    f"{_item_en(base)} 没有「{bad_filter}」这个部件",
                    f'{_item_en(base)} has no "{bad_filter}" part',
                ),
                L(
                    "去掉部件词或换成下面的部件后重发",
                    "Drop the part word or pick one of the parts below",
                ),
                (L("可选部件", "Parts"), have),
                title=title,
            )

        droppable = drops_db.droppable_keys()
        varzia = await self._varzia_relic_keys(platform)
        plan = _farm_plan(keys, inv, droppable, varzia)

        # 现在能开的裂隙：只看建议遗物所属的纪元（普通 / 钢铁 / 九重天都算）
        want_tiers = set(plan["crack_tiers"])
        fissure_rows: list[dict] = []
        fissure_error = False
        if want_tiers:
            from datetime import datetime, timezone

            try:
                fissures = await self.client.fissures(platform)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[sdjk] 刷取规划取裂隙失败，本次不列裂隙：%s", exc)
                fissures, fissure_error = [], True
            now = datetime.now(timezone.utc)
            for f in fissures or []:
                end = fmt.parse_iso(f.get("expiry", ""))
                tier_zh = fmt._tier_key(f.get("tier", ""))
                if not end or end <= now or tier_zh not in want_tiers:
                    continue
                mt = (
                    fmt._RAILJACK_TYPE_CN.get((f.get("missionType") or "").lower(), "")
                    if f.get("isStorm") and not is_en()
                    else ""
                ) or fmt.mission_cn(f.get("missionType", ""))
                fissure_rows.append(
                    {
                        "tier": tier_zh,
                        "type": mt,
                        "node": f.get("node", "?"),
                        "hard": bool(f.get("isHard")),
                        "storm": bool(f.get("isStorm")),
                        "srank": _storm_rank(f) if f.get("isStorm") else 0,
                        "quick": _fissure_quick(f, mt, _FARM_QUICK),
                        "endless": mt in _ENDLESS_MISSIONS
                        or (is_en() and mt in _ENDLESS_MISSIONS_EN),
                        "expiry": f.get("expiry", ""),
                    }
                )
        title, lines = fmt.fmt_farm_plan(
            _item_en(base),
            plan,
            fissure_rows,
            fissure_error=fissure_error,
            part_label=_farm_part_label,
            item_name=_item_en,
        )
        return Reply(title, lines, footer=fmt.fmt_platform_footer(platform))


# ---------------------------------------------------------------------------
# 「刷」指令的纯函数部分（解析物品名、挑遗物）；卡面在 formatters.fmt_farm_plan
# ---------------------------------------------------------------------------

FARM_USAGE = "用法：刷 Wisp Prime（整套规划）｜ 刷 圣英prime 枪管（只看某个部件）｜ 刷 水晶p"
FARM_USAGE_EN = (
    "farm Wisp Prime (whole set) | farm Afentis Prime Barrel (one part) | farm Citrine Prime"
)

# 「刷」的指令词（判断是否在句首用）
_FARM_WORDS = {w.lower() for w in (*COMMAND_ALIASES["farm"], *COMMAND_ALIASES_EN["farm"])}

# 速刷任务类型（与开核桃同一口径：不含要站桩的防御 / 生存 / 拦截 / 移动防御）
_FARM_QUICK = {"捕获", "歼灭", "破坏", "救援", "间谍"}

# 无尽裂隙：每轮结束可换一把遗物接着开，集够反应物另有加成，第 5 / 10 轮送随机遗物
# （英文 wiki Void Fissure「Endless Missions」原文列的 8 种）——批量开遗物首选
_ENDLESS_MISSIONS = {"拦截", "防御", "中断", "生存", "虚空覆涌", "元素转换", "虚空洪流", "挖掘"}
_ENDLESS_MISSIONS_EN = {
    "Interception",
    "Defense",
    "Disruption",
    "Survival",
    "Void Cascade",
    "Alchemy",
    "Void Flood",
    "Excavation",
}

# 九重天里只有地球 / 土星比邻星域的前哨战算速刷：全程太空作战，开核桃最快；
# Corpus 星域（金星 / 海王星 / 冥王星）要步行登舰，慢，不标速刷（维护者 11-09 提供的社区共识）。
_STORM_QUICK_REGIONS = ("Earth Proxima", "Saturn Proxima", "地球比邻星域", "土星比邻星域")


def _storm_quick(f: dict) -> bool:
    if (f.get("missionType") or "").strip().lower() != "skirmish":
        return False
    node = f.get("node") or ""
    return any(r in node for r in _STORM_QUICK_REGIONS)


# Corpus 控制的比邻星域（Empyrean 页：金星 / 天王星 / 海王星 / 冥王星；面纱两家都有，看节点派系）
_CORPUS_STORM_REGIONS = (
    "Venus Proxima",
    "Uranus Proxima",
    "Neptune Proxima",
    "Pluto Proxima",
    "金星比邻星域",
    "天王星比邻星域",
    "海王星比邻星域",
    "冥王星比邻星域",
)


def _storm_is_corpus(f: dict) -> bool:
    enemy = (f.get("enemy") or "").strip().lower()
    if enemy:
        return enemy == "corpus"
    node = f.get("node") or ""
    return any(r in node for r in _CORPUS_STORM_REGIONS)


def _storm_rank(f: dict) -> int:
    """九重天单独排序（维护者 11-09：九重天和普通裂隙玩法不一样，单独配置）：

    0 地球 / 土星前哨战（全程太空，开核桃最快）→ 1 其它格里尼尔九重天（如面纱前哨战）
    → 2 Corpus 九重天（要步行登舰，慢；生存 / 防御虽能逐波开遗物，也排在这里）。
    """
    if _storm_quick(f):
        return 0
    return 2 if _storm_is_corpus(f) else 1


def _fissure_quick(f: dict, mt: str, quick_zh: set) -> bool:
    """速刷判定：地面任务看任务类型；九重天只认地球 / 土星前哨战。"""
    if f.get("isStorm"):
        return _storm_quick(f)
    return mt in quick_zh or (is_en() and mt in _QUICK_EN)


# 稀有度 → 挑遗物时的权重（同样能覆盖时，优先部件在常见槽的遗物：完整遗物出货率 25.33% / 11% / 2%）
_RARITY_WEIGHT = {"常见": 3, "罕见": 2, "稀有": 1}
_FARM_TIER_ORDER = ("古纪", "前纪", "中纪", "后纪", "安魂", "全能", "先锋")


def _farm_base(key: str) -> tuple[str, str]:
    """反查键 → (物品名, 部件名)：「Wisp Prime 系统蓝图」→ (「Wisp Prime」, 「系统蓝图」)。

    非 Prime 的键（「Forma 蓝图」「2X Forma 蓝图」）按最后一个词切；单词键（「赤毒」）部件名为空。
    """
    i = key.find(" Prime")
    if i >= 0:
        return key[: i + len(" Prime")], key[i + len(" Prime") :].strip()
    if " " in key:
        head, tail = key.rsplit(" ", 1)
        return head, tail
    return key, ""


def _farm_bases(inv) -> dict[str, tuple[str, list[str]]]:
    """归一化物品名 → (物品名, [反查键…])。"""
    out: dict[str, tuple[str, list[str]]] = {}
    for k in inv:
        base, _part = _farm_base(k)
        nb = matching.normalize_name(base)
        out.setdefault(nb, (base, []))[1].append(k)
    return out


def _farm_name_cands(head: str, aliases: dict) -> list[str]:
    """一段输入可能对应的归一化物品名（原样 / 补 Prime / p→Prime / 英文名转中文 / 黑话）。"""
    raw: list[str] = [head]
    zh = _en_name_to_zh(head)
    if zh:
        raw.append(zh)
    slug = aliases.get(head.lower()) or aliases.get(matching.normalize_name(head))
    if slug:
        en = re.sub(r"_set$", "", slug).replace("_", " ").title()
        raw.append(en)
        zh2 = _en_name_to_zh(en)
        if zh2:
            raw.append(zh2)
    out: list[str] = []
    for r in raw:
        n = matching.normalize_name(r)
        if not n:
            continue
        forms = [n]
        if "prime" not in n:
            forms.append(re.sub(r"p$", "prime", n) if n.endswith("p") else n + "prime")
            if n.endswith("p"):
                forms.append(n + "prime")
        for f in forms:
            if f not in out:
                out.append(f)
    return out


def _farm_part_match(keys: list[str], words: list[str]) -> list[str]:
    """按部件词筛反查键；「蓝图」单独出现时只指主蓝图（不把「系统蓝图」也算进来）。"""
    from ..parser import WM_PART_EN

    picked: list[str] = []
    for w in words:
        wl = w.lower()
        zh = WM_PART_EN.get(wl, w)
        nz = matching.normalize_name(zh)
        for k in keys:
            part = matching.normalize_name(_farm_base(k)[1])
            ok = part == nz if nz == "蓝图" else (nz in part)
            if ok and k not in picked:
                picked.append(k)
    return picked


def _farm_resolve(q: str, inv, aliases: dict):
    """输入 → (物品名, [部件反查键], 认不出的部件词)；物品都认不出返回 None。

    从最长前缀开始试：前段当物品名，剩下的当部件词（「刷 圣英 prime 枪管」）。
    物品名必须**整名**命中（「wisp」补成「wispprime」），不做子串匹配，免得
    「刷 ash」带出别的物品。整句是单个部件全名（「Wisp Prime 系统蓝图」）也认。
    """
    bases = _farm_bases(inv)
    toks = q.split()
    for k in range(len(toks), 0, -1):
        head, rest = " ".join(toks[:k]), toks[k:]
        for c in _farm_name_cands(head, aliases):
            if c not in bases:
                continue
            base, keys = bases[c]
            if not rest:
                return base, sorted(keys, key=_farm_part_order), ""
            picked = _farm_part_match(keys, rest)
            if picked:
                return base, sorted(picked, key=_farm_part_order), ""
            return base, sorted(keys, key=_farm_part_order), " ".join(rest)
    # 单个部件全名 / 英文部件名（「Wisp Prime Systems Blueprint」）
    norm_keys = {matching.normalize_name(k): k for k in inv}
    one = norm_keys.get(matching.normalize_name(q)) or _en_part_to_inv_key(q, inv)
    if one:
        return _farm_base(one)[0], [one], ""
    return None


def _farm_part_order(key: str):
    """部件展示顺序：主蓝图在前，其余按名字。"""
    part = _farm_base(key)[1]
    return (part != "蓝图", part)


def _farm_suggest(q: str, inv, limit: int = 3) -> list[str]:
    """认不出时的候选物品名（归一化包含）。"""
    nq = matching.normalize_name(q)
    if not nq:
        return []
    out: list[str] = []
    for nb, (base, _keys) in _farm_bases(inv).items():
        if nq in nb and base not in out:
            out.append(base)
    out.sort(key=len)
    return out[:limit]


def _farm_part_label(key: str) -> str:
    """卡面部件列：只写部件名（物品名在标题里）；英文模式换官方英文部件名。"""
    base, part = _farm_base(key)
    if not part:
        return _item_en(key)
    if not is_en():
        return part
    full = _item_en(key)
    base_en = _item_en(base)
    return full[len(base_en) :].strip() if full.startswith(base_en) and full != base_en else full


def _farm_relic_state(relic: str, droppable: set, varzia: set) -> str:
    key = fmt.relic_en_key(relic or "")
    if key and key in droppable:
        return "drop"
    if key and key in varzia:
        return "varzia"
    return "vaulted"


def _farm_plan(keys: list[str], inv, droppable: set, varzia: set) -> dict:
    """挑遗物：在**当前可掉落**的遗物里用贪心法找最少几把覆盖全部能刷的部件。

    每轮选「新覆盖部件最多」的一把；同数时选部件所在槽位更常见的（出货率高），
    再同则选低纪元（古纪最好刷）。刷不到的部件看阿耶商店，都没有就算已入库。

    Returns:
        ``{"parts": [{"key", "state", "relic", "rarity", "others"}],
        "picks": [{"relic", "tier", "keys", "places"}]}``；
        ``relic`` 为卡面写法（「后纪 A21」），``tier`` 为中文纪元。
    """
    by_relic: dict[str, dict[str, str]] = {}  # 可掉落遗物 → {部件键: 槽位}
    varzia_of: dict[str, list[tuple[str, str]]] = {}
    for k in keys:
        for o in inv.get(k) or []:
            relic = o.get("relic") or ""
            st = _farm_relic_state(relic, droppable, varzia)
            if st == "drop":
                by_relic.setdefault(relic, {})[k] = o.get("rarity") or ""
            elif st == "varzia":
                varzia_of.setdefault(k, []).append((relic, o.get("rarity") or ""))

    def _tier(relic: str) -> str:
        return fmt._tier_key(relic.split()[0]) if relic else ""

    def _tier_rank(relic: str) -> int:
        t = _tier(relic)
        return _FARM_TIER_ORDER.index(t) if t in _FARM_TIER_ORDER else len(_FARM_TIER_ORDER)

    place_cache: dict[str, list] = {}

    def _places(relic: str) -> list:
        if relic not in place_cache:
            rkey = fmt.relic_en_key(relic)
            place_cache[relic] = drops_db.relic_best_places(f"{rkey} relic") if rkey else []
        return place_cache[relic]

    need = {k for k in keys if any(k in got for got in by_relic.values())}
    covered: dict[str, tuple[str, str]] = {}
    picks: list[dict] = []
    while need - set(covered):
        left = need - set(covered)

        def _score(item):
            relic, got = item
            new = [k for k in got if k in left]
            return (
                -len(new),
                not _places(relic),  # 能在常规任务里刷的优先（只在储藏库 / 活动掉的排后）
                -sum(_RARITY_WEIGHT.get(got[k], 0) for k in new),
                _tier_rank(relic),
                relic,
            )

        relic, got = min(by_relic.items(), key=_score)
        new = [k for k in got if k in left]
        if not new:
            break
        for k in new:
            covered[k] = (relic, got[k])
        picks.append(
            {
                "relic": fmt.relic_cn(relic),
                "tier": _tier(relic),
                "rank": (_tier_rank(relic), fmt._relic_sort_key(fmt.relic_cn(relic))),
                "keys": new,
                "places": _places(relic),
            }
        )
    picks.sort(key=lambda pk: pk.pop("rank"))  # 卡面按纪元从低到高列

    parts: list[dict] = []
    for k in keys:
        if k in covered:
            relic, rarity = covered[k]
            others = sum(1 for got in by_relic.values() if k in got) - 1
            parts.append(
                {
                    "key": k,
                    "state": "drop",
                    "relic": fmt.relic_cn(relic),
                    "rarity": rarity,
                    "others": others,
                }
            )
        elif k in varzia_of:
            relic, rarity = sorted(varzia_of[k], key=lambda x: -_RARITY_WEIGHT.get(x[1], 0))[0]
            parts.append(
                {
                    "key": k,
                    "state": "varzia",
                    "relic": fmt.relic_cn(relic),
                    "rarity": rarity,
                    "others": len(varzia_of[k]) - 1,
                }
            )
        else:
            parts.append({"key": k, "state": "vaulted", "relic": "", "rarity": "", "others": 0})
    # 要进裂隙开的纪元：建议遗物 + 阿耶换来的遗物（换到手也得进同纪元裂隙开）
    crack = {pk["tier"] for pk in picks} | {
        _tier(p["relic"]) for p in parts if p["state"] == "varzia"
    }
    crack_tiers = sorted(
        (t for t in crack if t),
        key=lambda t: _FARM_TIER_ORDER.index(t) if t in _FARM_TIER_ORDER else len(_FARM_TIER_ORDER),
    )
    return {"parts": parts, "picks": picks, "crack_tiers": crack_tiers}
