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
from ..logging_compat import logger
from ..parser import TIER_CN, parse
from .base import PLUGIN_DIR, Reply


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
RELIC_USAGE = ("用法：遗物 后纪A2（查奖励）｜ 遗物 绝路 枪机（部件反查出处）｜"
               " 遗物 出库（当前可掉落）｜ 遗物 入库（已入库、不可刷取）")

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
        """"后纪A2 / 先锋C1 / 安魂 I / Axi A2" -> "后纪 Axi A2" 简化归一。

        ★ 档位表一律取 core/parser.TIER_CN（含 Omnia/Vanguard）——曾本地硬编 5 档，
        2026-09 新增「先锋」档后「遗物 先锋 C1」直接「未找到」（资料会话交接）；
        代号支持 字母+数字（A2/A 2）、罗马数字（I..IV）、词式（Eterna，安魂档）。
        """
        import re as _re
        zh2en = {k: v for k, v in TIER_CN.items() if not k.isascii()}
        q = _re.sub(r"\s+", " ", q.strip().replace("纪元", ""))
        era = next((k for k in zh2en if q.startswith(k)), None)
        if not era:
            return q
        code = q[len(era):].strip()
        code = code.split()[0] if code else ""
        m2 = (_re.match(r"^([A-Za-z])(\d{1,2})$", code)
              or _re.match(r"^([A-Za-z])\s+(\d{1,2})$", code))
        if m2:
            return f"{era} {zh2en[era]} {m2.group(1).upper()}{int(m2.group(2))}"
        if code and _re.fullmatch(r"[IVX]+", code, _re.I):
            return f"{era} {zh2en[era]} {code.upper()}"
        if code and _re.fullmatch(r"[A-Za-z]{2,}", code):
            return f"{era} {zh2en[era]} {code.capitalize()}"
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
            logger.warning("[sdjk] 阿耶商店数据取不到，"
                           "本次按「不在售」处理：%s", exc)
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
            return Reply(raw_text=RELIC_USAGE)
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
                row = {"cn": k, "tier_cn": m[0], "unvaulted": unvaulted,
                       "varzia": en_key in varzia}
                (unv_rows if unvaulted else vaulted_rows).append(row)

            # 语义：**出库 = 从金库放出 = 当前可以掉落**（unvaulted）；
            #        **入库 = 收回金库 = 当前不能刷取**（vaulted）。
            # 旧实现把两者写反了：`出库` 显示的是已下架清单，与游戏内认知相反。
            if q == "出库":
                rows = unv_rows
                title = "遗物出库（当前可掉落）"
            elif q == "入库":
                rows = vaulted_rows
                title = "遗物入库（已入库、不可刷取）"
            else:  # "列表"
                rows = unv_rows + vaulted_rows
                title = (f"遗物列表：当前可掉落 {len(unv_rows)}，"
                         f"已入库 {len(vaulted_rows)}")

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
                        specials[nm] = "仅阿耶兑换"
                        continue
                    m = r["cn"].split()
                    if len(m) < 3:
                        continue
                    why = drops_db.special_source(
                        f"{tier_en.get(m[0], m[0].lower())} "
                        f"{m[2].lower()} relic")
                    if why:
                        specials[nm] = why

            title, lines = fmt.fmt_relic_by_tier(
                rows, title, page=parsed.page,
                # 列表按纪元分组并排展示，每页要装得下整个纪元；
                # self.page_size（默认 12）是给逐条带详情的卡片用的，太碎。
                # 每行 9 列后每页 90 个 ≈ 10 行，13 页降到 9 页。
                page_size=max(90, self.page_size),
                # 推荐刷取点只在出库卡给（已入库的刷不到）
                farm_hints=drops_db.farm_hints() if q == "出库" else None,
                specials=specials or None)
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
                    rows.append({"cn": k, "tier_cn": m[0],
                                 "unvaulted": en_key in all_uv,
                                 "varzia": en_key in varzia})
                title, lines = fmt.fmt_relic_by_tier(
                    rows, f"遗物列表：{q.strip()}（{len(rows)} 把）",
                    page=parsed.page, page_size=max(90, self.page_size))
                return Reply(title, lines, footer=fmt.fmt_platform_footer(platform))
        # ② 部件优先：带空格或能直接命中部件表
        # 名称归一：去空格精确匹配；非 Prime 输入自动补 Prime 试一次（wiki 上架过的只有 Prime 系）
        q_nospace = q.replace(" ", "")
        inv_norm = {k.replace(" ", ""): k for k in inv}
        cands = [q_nospace]
        # 非 Prime 输入自动补 Prime：**把 Prime 插到每个位置都试一遍**。
        # 旧实现只插在「第一个部件类型字（枪/机/托/蓝/图/弦/管）」之前，
        # 而武器名里本身就可能带这些字 —— 「席尔火枪枪管」会插成
        # 「席尔火Prime枪枪管」，永远查不到（用户真实输入就是不带 Prime 的）。
        # 代价只是几十次 dict 查询，从**靠后**的位置开始插（部件类型词
        # 一般在末尾：枪管/枪机/蓝图），先命中的更可能是正确切分。
        if "prime" not in q_nospace.lower():
            for _i in range(len(q_nospace) - 1, 0, -1):
                cands.append(q_nospace[:_i] + "Prime" + q_nospace[_i:])
        cands.append(q_nospace + "Prime")
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
            _tbl = (getattr(self.client, "_aliases", None) or {}).get("wm_items") or {}
            _lower = {k.replace(" ", "").lower(): k for k in inv}
            for _i in range(len(q_nospace) - 1, 0, -1):
                _slug = _tbl.get(q_nospace[:_i].lower())
                if not _slug:
                    continue
                _rest = re.sub(r"^(?:prime|p)(?=[\u4e00-\u9fff])", "",
                               q_nospace[_i:], flags=re.I)
                _en = re.sub(r"_set$", "", _slug).replace("_", " ").title()
                q = _lower.get((_en + _rest).replace(" ", "").lower()) or q
                if q in inv:
                    break
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
                rows.append({"relic": o.get("relic"), "rarity": o.get("rarity"),
                             "state": state})
            title, lines = fmt.fmt_relic_piece(
                q, rows, farm_hints=drops_db.farm_hints())
            return Reply(title, lines, footer=fmt.fmt_platform_footer(platform))
        # ② 归一化遗物名
        nq = self._norm_relic(q)
        hit = idx.get(nq) or idx.get(q)
        if not hit:
            # 模糊：任一槽位包含 q
            cand = [r for r in idx if q.replace(" ", "") in r.replace(" ", "")]
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
                    slots.append({"rarity": rarity, "name": nm})
            for rarity, items in hit.items():
                if rarity in ("常见", "罕见", "稀有"):
                    continue
                for nm in items or []:
                    slots.append({"rarity": rarity, "name": nm})
            title, lines = fmt.fmt_relic_rewards(nq, slots, prices)
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
                    lines.append("◆ 该档位不在官方任务掉落表（新档位/特殊渠道）")
                else:
                    varzia = await self._varzia_relic_keys(platform)
                    if any(x.startswith(k + " ") for x in unv):
                        lines.append("◆ 该遗物当前可掉落（出库中）")
                    elif k in varzia:
                        lines.append("◆ 该遗物可在阿耶商店兑换（当前可获取）")
                    else:
                        lines.append("◆ 该遗物已入库，当前不可刷取")
            return Reply(title, lines, footer=fmt.fmt_platform_footer(platform))
        # ③ 视为部件名模糊（词序无关：把 q 的 token 任意拼接匹配）
        qn = q.replace(" ", "")
        fz = [k for k in inv if qn in k.replace(" ", "")]
        # 词序反转：绝路枪机 -> 枪机绝路
        if not fz:
            fz = [k for k in inv
                  if "".join(sorted(qn)) == "".join(sorted(k.replace(" ", "")[:len(qn)]))]
        fz = list(dict.fromkeys(fz))[:6]
        if len(fz) == 1:
            parsed2 = parse(f"遗物 {fz[0]}")
            parsed2.command = "relic"
            return await self._h_relic(parsed2, event, platform)
        tip = f"未找到「{q}」。"
        if fz:
            tip += f"你是不是想找：{'、'.join(fz[:3])}"
        tip += "\n" + RELIC_USAGE          # 顺序固定：未找到 → 你是不是想找 → 用法
        return Reply(raw_text=tip)

    async def _h_parts(self, parsed, event, platform) -> Reply:
        """部件反查：部件名 → 出自哪些遗物及槽位（走遗物反查）。"""
        return await self._h_relic(parsed, event, platform)

    async def _h_openrelic(self, parsed, event, platform) -> Reply:
        """开核桃：当前裂隙 + 各纪元是否仍在掉落池（未入库）。"""
        toks = list(parsed.content or [])
        if parsed.preset:
            toks.insert(0, parsed.preset)
        quick_only = any(t in ("速刷", "快") for t in toks)
        state = next((t for t in toks if t in ("未入库", "已入库", "可掉落")), "")
        if state == "可掉落":
            state = "未入库"
        price = next((t for t in toks if t in ("低价", "高价")), "")
        fissures = await self.client.fissures(platform)
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        act = [f for f in fissures
               if fmt.parse_iso(f.get("expiry", "")) and
               fmt.parse_iso(f["expiry"]) > now]
        unv = drops_db.unvaulted_relics()

        def tier_key(tier: str) -> str:
            return {"Lith": "lith", "Meso": "meso", "Neo": "neo",
                    "Axi": "axi", "Requiem": "requiem",
                    "Omnia": "omnia", "Vanguard": "vanguard"}.get(tier, "")

        tier_unvaulted: dict[str, int] = {}
        for k, cn in unv.items():
            tier_unvaulted.setdefault(k.split()[0], 0)
            tier_unvaulted[k.split()[0]] += 1

        # 速刷 = 任务流程短、结算快的类型。**不含移动防御 / 生存 / 防御 / 拦截**
        # —— 那些要站桩守点，维护者明确说「移动防御算不上速刷」。
        QUICK = {"捕获", "歼灭", "破坏", "救援", "间谍"}
        rows = []
        for f in act:
            mt = fmt.mission_cn(f.get("missionType", ""))
            tier = f.get("tier", "")
            tk = tier_key(tier)
            cnt = tier_unvaulted.get(tk, 0)
            kind = []
            if f.get("isHard"):
                kind.append("钢铁")
            if f.get("isStorm"):
                kind.append("九重天")
            rows.append({"node": f.get("node", "?"), "type": mt, "tier": tier,
                         "tier_cn": fmt.tier_cn(tier) if tier and tier != "?" else "?",
                         "unv": cnt, "quick": mt in QUICK, "kind": "/".join(kind),
                         "faction": fmt._fissure_faction(f),
                         "expiry": f.get("expiry", "")})
        if quick_only:
            rows = [r for r in rows if r["quick"]]
        if state == "未入库":
            rows = [r for r in rows if r["unv"] > 0]
        elif state == "已入库":
            rows = [r for r in rows if r["unv"] == 0]
        rows.sort(key=lambda r: (fmt._TIER_ORDER.get(r["tier"], 99),
                                 not r["quick"], -r["unv"]))
        if price:
            rows.sort(key=lambda r: r["unv"], reverse=(price == "高价"))
        # 不翻页：全部场次放同一张卡（维护者要求「那几页内容都放一张截图上」）。
        total = len(rows)
        chunk = rows[:fmt._ALL_ROWS_CAP]
        lines = []
        for r in chunk:
            tag = f"[{r['tier_cn']}]"
            # 行结构（★ 2026-09-27 用户口径，与裂隙卡一致）：
            # [纪元] **任务类型 节点** · 派系　可掉落 N 种 · 钢铁/九重天 · 速刷 · 剩X
            # 前半（纪元芯片 / 类型 / 节点）用**空格**连接，后半仍用「 · 」；
            # 标签顺序按维护者要求：**速刷放最后**、钢铁/九重天放它前面（倒数第二）。
            mtype = r["type"] if r["type"] and r["type"] != "?" else ""
            line = " ".join(p for p in (tag, mtype, r["node"]) if p)
            if r["faction"]:
                line += f" · {r['faction']}"
            line += f"　可掉落 {r['unv']} 种"
            if r["kind"]:
                line += f" · {r['kind']}"
            if r["quick"]:
                line += " · 速刷"
            lines.append(f"{line} · 剩{fmt.countdown(r['expiry'])}")
        if not lines:
            lines = ["当前条件下没有可开的裂隙"]
        lines.append("※ 按纪元排序（古纪→前纪→中纪→后纪→安魂→全能），"
                     "「可掉落 N 种」= 该纪元当前仍在掉落池的遗物数")
        lines.append("※ 「速刷」= 捕获 / 歼灭 / 破坏 / 救援 / 间谍 这类快节奏任务")
        if total > len(chunk):
            lines.append(f"※ 共 {total} 场，本卡只列前 {len(chunk)} 场（按上面口径排序）")
        flt = "，".join(x for x in [("速刷" if quick_only else ""), state, price] if x)
        title = f"开核桃建议（共{total}场）" + (f"　筛选：{flt}" if flt else "")
        return Reply(title, lines, footer=fmt.fmt_platform_footer(platform))
