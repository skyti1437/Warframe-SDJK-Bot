# -*- coding: utf-8 -*-
"""配卡截图识别域（D10 自 main.py 迁入，方法体逐字未改）。

覆盖路由键 2 项：scan / scandamage；_fit_scan_image 为 MUST_BE_STATIC
（tests/test_method_contract 12+ 文件扫描持续断言）。
模块级依赖 _num（配置数值容错）经 base.py 单源提供（双处消费禁双源）。
子包纪律：不 import astrbot（事件对象鸭子类型）。
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Optional

from .. import damage_calc as dc
from .. import loadout_ocr as lo
from .. import pips as pips_engine
from ..logging_compat import logger
from .base import Reply, _num


class ScanCommands:
    """Mixin：识卡 / 伤害截图 handler（挂载于 main.WarframeSDJK）。"""

    # 配卡截图识别
    # ------------------------------------------------------------------
    async def _h_scan(self, parsed, event, platform) -> Reply:
        """识卡：读游戏内「升级」界面截图，按**真实 MOD 等级**折算配卡加成。

        视觉模型只负责读字（MOD 名 / 卡片容量数字 / 面板数值），加成由
        `core/loadout_ocr.py` 按容量数字反推出的实际等级重算，再用面板数值校验。
        """
        if not self._event_has_image(event):
            return Reply(
                "配卡识别",
                [
                    "用法：发一张武器「升级」界面截图，配上文字「识卡」",
                    "　读出：武器（含等级）＋每张 MOD 卡与其实际等级＋面板数值",
                    "　加成按卡片容量数字反推实际等级（绿=匹配减半／红=不合+25%／白=原价）",
                    "　再用面板数值交叉校验；第 2 页直接给伤害详情（同「伤害计算」公式）",
                    "　想换敌人/等级/爆头复算：识卡伤害 对 重机枪手 150级 爆头",
                    "　需要 AstrBot 里配好一个多模态模型（如 Qwen3-VL / glm-4.1v）",
                ],
            )

        # ★★ 安全审查（2026-09-18）：识卡每次会调用**付费**多模态模型。
        #   原来只有「同会话串行」——那只防自己连发，防不住「同一个人狂刷」
        #   和「多个群同时刷」，而宿主是 2 核小机器：并发几路就能把 CPU 打到
        #   渲染 39s+，账单也会跟着涨。这里加两道闸（都能在面板关掉/调整）：
        #     ① 同一发送者冷却 scan_cooldown 秒（默认 15，设 0 关闭）
        #     ② 全局并发上限 scan_max_concurrent（默认 3，设 0 关闭）
        _cd = _num(self.cfg, "scan_cooldown", 15)
        _sender = self._safe_sender(event)
        if _cd > 0 and _sender:
            _last = self._ocr_last.get(_sender, 0.0)
            _left = _cd - (time.time() - _last)
            if _last and _left > 0:
                return Reply(
                    raw_text=f"⏳ 识卡太频繁了，请 {_left:.0f} 秒后再试\n"
                    "（面板「识卡冷却秒数」可调，设 0 关闭限制）"
                )
            # 顺手清掉过期记录，避免长期运行把字典撑大
            if len(self._ocr_last) > 512:
                _now = time.time()
                for _k in [k for k, v in self._ocr_last.items() if _now - v > max(_cd * 4, 120)]:
                    self._ocr_last.pop(_k, None)
            self._ocr_last[_sender] = time.time()
        _cap = int(_num(self.cfg, "scan_max_concurrent", 3))
        if _cap > 0 and len(self._ocr_busy) >= _cap:
            return Reply(raw_text=f"⏳ 机器人正在处理其它识卡请求（并发上限 {_cap}），请稍后再试")

        # 同一会话串行：识卡是「多次 vision 调用 + 双页渲染」的重活，
        # 连发会让容器 CPU 争抢（实测单张渲染 0.7s → 39s）。
        _busy_key = f"{getattr(event, 'unified_msg_origin', '') or ''}"
        if _busy_key and _busy_key in self._ocr_busy:
            return Reply(
                raw_text="上一张配卡还在识别中（约 15~30 秒），请稍等一下再发，避免任务叠加变慢"
            )
        if _busy_key:
            self._ocr_busy.add(_busy_key)
        try:
            imgs = await self._image_data_urls(event)
            if not imgs:
                return Reply(raw_text="图片下载失败，请重发一次截图")
            ocr = await self._extract_loadout_validated(imgs[0])
        finally:
            if _busy_key:
                self._ocr_busy.discard(_busy_key)
        if not ocr:
            # ★ 识别失败返还冷却（2026-09-23）：防刷闸不该惩罚「渠道全挂」的
            #   受害者——失败重试不该干等 15 秒。成功调用才占冷却。
            if _cd > 0 and _sender:
                self._ocr_last.pop(_sender, None)
            return Reply(
                raw_text="截图识别失败：视觉渠道没给出可解析结果。"
                "可能原因：① 渠道超时/返回空；② 未配视觉模型。"
                "可先重发一次；仍失败请查看机器人日志里的"
                "「配卡识别」条目（会写明是哪个渠道、什么原因）"
            )

        an = lo.analyze(ocr, ocr.get("_pips_rows"))
        lines = lo.card_lines(an)
        # 认不全时把原始识别结果附上：否则用户只看到「没认出来」，无法判断是图的问题还是库的问题
        if not an.get("ok"):
            lines.append("　原始识别结果：" + json.dumps(ocr, ensure_ascii=False)[:700])

        # 缓存折算结果供「识卡伤害」复算（30 分钟）
        umo = event.unified_msg_origin
        cached_spec = None
        if an.get("weapon"):
            try:
                cached_spec = lo.to_damage_spec(an)
            except Exception:  # noqa: BLE001 —— 缓存失败不影响识卡本身
                cached_spec = None
        if cached_spec is not None:
            # ★ 按 会话+发送者 分键（2026-09-23）：此前按会话只存 1 份，
            #   同群两人先后识卡会互相顶掉「识卡伤害」的上下文。
            key = f"{umo}|{self._safe_sender(event) or ''}"
            self._last_scan[key] = (time.time(), an["weapon"], cached_spec)
            if len(self._last_scan) > 128:  # 防长期累积
                for _k in list(self._last_scan)[: len(self._last_scan) - 128]:
                    self._last_scan.pop(_k, None)

        detail = self._loadout_detail_lines(an) if cached_spec is not None else None
        if detail:
            return Reply(pages=[("配卡识别", lines), ("配卡识别 · 伤害详情", detail)])
        return Reply("配卡识别", lines)

    def _loadout_detail_lines(self, an: dict) -> Optional[list[str]]:
        """识卡第二页：用与「伤害计算」完全同一套管线出详情。

        识别产物（净基础 + MOD 折算 spec）直接喂 dc.calculate —— 不重写任何
        公式，页脚提示可用「识卡伤害」换敌人/等级等参数复算。
        """
        try:
            spec = lo.to_damage_spec(an)
            weapon = an["weapon"]
            if not spec or not weapon:
                return None
            res = dc.calculate(spec, weapon)
            if not res.get("ok"):
                return None
            out = dc.card_lines(weapon, spec, res, [])
            out.append(
                "※ 与「伤害计算」同一套公式（G系 100 级·身体，无 buff 基准）；"
                "换敌人/等级/爆头请发「识卡伤害 对 重机枪手 150级 爆头」"
            )
            return out
        except Exception:  # noqa: BLE001 —— 第二页是增强项，失败不挡第一页
            logger.exception("[sdjk] 识卡伤害详情页生成失败")
            return None

    async def _h_scan_damage(self, parsed, event, platform) -> Reply:
        """识卡伤害：用最近一次「识卡」的配卡折算结果跑伤害计算器。

        用法：识卡伤害 [对 敌人名] [派系] [N级] [爆头] [镀层N] [异常N] …
        30 分钟内的识卡缓存有效；MOD 折算值沿用识卡结果，本条指令只覆盖
        目标/条件类参数（敌人、等级、派系、爆头、连击、镀层层数…）。
        """
        umo = event.unified_msg_origin
        snd = self._safe_sender(event) or ""
        # ★ 优先取「本人」的识卡缓存；本人没有再退回同会话最近一条
        #   （2026-09-23 起识卡按 会话|发送者 分键，防同群互相顶掉）
        hit = self._last_scan.get(f"{umo}|{snd}")
        if not hit:
            same_umo = [
                (k, v) for k, v in self._last_scan.items() if k.startswith(umo + "|") or k == umo
            ]
            if same_umo:
                hit = max(same_umo, key=lambda kv: kv[1][0])[1]
        if not hit or time.time() - hit[0] > 1800:
            for k in [k for k in self._last_scan if k == umo or k.startswith(umo + "|")]:
                self._last_scan.pop(k, None)
            return Reply(
                "识卡伤害",
                [
                    "本会话 30 分钟内没有识卡记录。",
                    "　先发「识卡 + 武器升级界面截图」，再用本指令复算：",
                    "　　识卡伤害 对 重机枪手 150级 爆头",
                    "　　识卡伤害 C系 120级｜识卡伤害 满镀层 异常4 连击120 重击",
                    "　参数写法与「伤害」指令一致（识卡伤害 = 伤害 + 已识别配卡）。",
                ],
            )
        _ts, weapon, cached = hit
        # ⚠️ 传 token 列表（与「伤害」指令 wiki_misc._h_damage 同口径）：
        #   传字符串会被逐字符拆开 → 「150级/爆头」全部失效、单字进「未识别」
        rest = [t for t in (parsed.content or []) if str(t).strip()]
        try:
            if rest:
                arg_spec, _tokens = dc.parse_args(rest)
                default_spec, _ = dc.parse_args([])
                # 只覆盖「目标/条件」类参数（相对默认值有变化的项），
                # MOD 折算值沿用识卡结果 —— 避免默认 spec 里的零值冲掉折算
                overrides = {k: v for k, v in arg_spec.items() if v != default_spec.get(k)}
                spec = {**cached, **overrides}
            else:
                spec = dict(cached)
            res = dc.calculate(spec, weapon)
            return Reply("识卡伤害", dc.card_lines(weapon, spec, res, []))
        except Exception as exc:  # noqa: BLE001 —— 绝不静默
            logger.warning("[sdjk] 识卡伤害复算失败：%r", exc)
            return Reply(
                "识卡伤害",
                [
                    f"❗ 复算出错：{type(exc).__name__}: {exc}",
                    "　重新发一次「识卡」后再试；参数写法见「伤害」指令的用法页。",
                ],
            )

    @staticmethod
    def _fit_scan_image(image_url: str, min_width: int = 1600, max_width: int = 1600) -> str:
        """把配卡截图规整到「能读清又不过大」的宽度区间：**小图放大、大图缩小**。

        · 小图（宽 < min_width）→ Lanczos 放大到 ~1280。
          实测（2026-09-17）：648×242 的剪贴板截图直接喂模型会大面积幻觉
          （MOD 名编造、容量数字全错），放大 3 倍后恢复正常。
          上限一路从 2000 → 1600 → 1280 收窄：vision 推理耗时随像素近似线性，
          1600 宽实测单次就要 22~26 s（30 s 竞速窗口都不够）。
        · 大图（宽 > max_width）→ 缩到 max_width。
          ★ 识卡走 **provider 直连**（`text_chat(image_urls=…)`），**不经过**
          AstrBot agent 的图片预处理 / 512KB 压缩 —— 传的就是原图。生产实测
          2326×870 要 39 s（实验室把图压到 134 KB 时只要 6.65 s，差 5.9×），
          按面积比缩到 1600 宽约省一半推理时间（2026-09-20 用户要求补上）。
          配卡截图的 MOD 名/数字在 1600 宽下仍清晰（原图本身多为 2 倍速截图）。
        """
        if not image_url.startswith("data:"):
            return image_url
        try:
            import base64
            import io

            from PIL import Image as PILImage

            head, b64 = image_url.split(",", 1)
            img = PILImage.open(io.BytesIO(base64.b64decode(b64)))
            w, h = img.size
            if min_width <= w <= max_width:
                return image_url  # 已在目标区间，原样送
            if w < min_width:
                scale, target = min(1280.0 / w, 4.0), 1280
            else:
                scale, target = max_width / w, max_width
            # 标签按**实际**缩放方向写：1599 宽这类「略低于 min_width」的图
            # 会被规整到 1280，其实是缩小而不是放大
            action = "放大" if scale > 1 else "缩小"
            nw, nh = max(1, int(w * scale)), max(1, int(h * scale))
            img2 = img.resize((nw, nh), PILImage.LANCZOS)
            fmt = "PNG" if "png" in head.lower() else "JPEG"
            buf = io.BytesIO()
            img2.convert("RGB" if fmt == "JPEG" else img2.mode).save(buf, fmt)
            new_b64 = base64.b64encode(buf.getvalue()).decode()
            logger.info(
                "[sdjk] 配卡截图 %dx%d → %dx%d（已%s，目标宽 %d）", w, h, nw, nh, action, target
            )
            return f"data:image/{fmt.lower()};base64,{new_b64}"
        except Exception:  # noqa: BLE001 —— 规整失败就用原图，别让识卡挂掉
            return image_url

    async def _extract_loadout_validated(
        self, image_url: str, max_attempts: int = 2
    ) -> Optional[dict]:
        """识别 + 校验闭环：面板校验不过就换渠道/重试一次，取最好的结果。

        视觉模型偶发漏行/读串（v1.11 实测：毒素行整行漏掉、总计挂到
        穿刺名下），单次识别不可全信 —— 用 analyze 的面板校验当裁判，
        最多试 max_attempts 次；仍不过时追加一次「只读伤害栏」的聚焦
        识别（任务越窄读得越准），把干净的行拼接回去再验。
        """
        # ★ 豆子检测要在**缩放前**的原图上做（_detect_pips 的说明）
        pips_rows = await self._detect_pips(image_url)
        image_url = self._fit_scan_image(image_url)
        best: Optional[tuple[int, dict]] = None  # (失败项数, ocr)

        def _finish(o: Optional[dict]) -> Optional[dict]:
            """把豆子结果挂到返回的 ocr 上，供上层复用（免得再检测一次）。"""
            if o is not None:
                o["_pips_rows"] = pips_rows
            return o

        def _score(ocr: dict):
            """面板校验打分：返回 (失败项数, analyze 结果)，恒为数值。

            ★ 2026-09-23：武器没认出不再「立即接受」——此前第一个返回
            「有 weapon 字段但其实是幻觉」的渠道会直接获胜并取消其它渠道
            （9-20 漏读事故同类风险）。现在按重罚 +6 计入竞速，窗口耗尽后
            才轮到它兜底。「0 张卡」「面板全没读到」「疑似漏读」惩罚不变。
            """
            an = lo.analyze(ocr, pips_rows)
            # ★ 记一行「豆子有没有真的用上」（2026-09-20 用户报「北风还是 3 级」后加）：
            #   检测成功但**对齐失败**时豆子会被整体弃用，光看检测日志看不出来。
            st = an.get("pips") or {}
            if pips_rows and st:
                logger.info(
                    "[sdjk] 豆子对齐：采用 %s 张 / 冲突 %s 张（检测到 %s 行）",
                    st.get("used"),
                    st.get("conflict"),
                    st.get("rows"),
                )
                if not st.get("used"):
                    logger.warning(
                        "[sdjk] ★ 豆子检测到了但**对齐无解**，本次退回容量反推"
                        "（截图存到 scan_debug/ 便于排查）"
                    )
                    self._dump_scan_debug(image_url)
            bad = 6 if not an.get("weapon") else 0
            checks = an.get("checks") or []
            bad += sum(1 for c in checks if not c["ok"])
            n_mods = len(an.get("mods") or [])
            if not n_mods:
                bad += 4
            if not checks:
                bad += 2
            # ★ 漏读交叉校验（2026-09-20 用户 4K 报障后加）：
            #   像素网格里「有豆的格数」是**这张图至少有多少张卡**的硬下界
            #   （有豆 ⇒ 等级 > 0 ⇒ 该格必然有卡）。模型读到的卡数低于这个下界
            #   就一定是漏读，必须重罚 —— 否则会出现「漏读一半的结果因为
            #   面板校验碰巧少错 1 项而被选中」。
            #   实测事故：glm-4v-flash 只读了上排 3 张（漏掉下排 4 张），
            #   30B 正确读全 7 张，最终却选了 glm 的（1 项不过 vs 2 项不过）。
            if pips_rows:
                low = pips_engine.expected_min_cards(pips_rows)
                pen = pips_engine.underread_penalty(pips_rows, n_mods)
                if pen:
                    logger.warning(
                        "[sdjk] ★ 疑似漏读：模型只读到 %d 张，但像素网格里"
                        "已有 %d 格有豆 → 判为漏读并重罚 +%d",
                        n_mods,
                        low,
                        pen,
                    )
                    bad += pen
            return bad, an

        # **并行 + 边到边校验**：原实现是「串行重试同一批渠道」——
        # 实测单次识别 22~26 s，两次就是 50 s（用户视角「快一分钟了」）。
        # 现在同时跑最多 3 个渠道，谁先返回就立刻校验：全过立即收工；
        # 窗口内都没全过 → 取失败项最少的那个走「聚焦二读」。
        provs = [p for p in self._vision_providers() if p is not None][:4]
        if not provs:
            logger.warning("[sdjk] 识卡：没有可用视觉渠道（配置/渠道状态见上一条）")
            return None
        _names = []
        for _p in provs:
            try:
                _names.append(getattr(getattr(_p, "meta", lambda: None)(), "id", "") or "?")
            except Exception:  # noqa: BLE001
                _names.append("?")
        logger.info(
            "[sdjk] 识卡：并行 %d 个渠道（窗口 %.0f s）：%s",
            len(provs),
            self.RACE_WINDOW_S,
            "、".join(_names),
        )
        loop = asyncio.get_event_loop()
        deadline = loop.time() + max(0.5, float(self.RACE_WINDOW_S))
        tasks = [asyncio.create_task(self._extract_loadout_from_image(image_url, p)) for p in provs]
        try:
            pending = set(tasks)
            while pending:
                timeout = deadline - loop.time()
                if timeout <= 0:
                    break
                done, pending = await asyncio.wait(
                    pending, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
                )
                if not done:
                    break
                for t in done:
                    try:
                        ocr = t.result()
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("[sdjk] 配卡识别渠道异常：%s", exc)
                        continue
                    if not ocr:
                        continue
                    bad, an = _score(ocr)
                    if bad == 0:
                        logger.info("[sdjk] 配卡识别校验全过，收工")
                        return _finish(ocr)
                    logger.warning("[sdjk] 配卡识别一份结果有 %d 项校验不过", bad)
                    if best is None or bad < best[0]:
                        best = (bad, ocr)
        finally:
            for t in tasks:
                t.cancel()
        if best is None:
            return None
        # —— 聚焦二读：只读伤害栏，拼接后重新校验 ——
        # ★ 与主路径同一把尺（_score，2026-09-23）：否则「拼接后 0 张卡/
        #   checks 全空」的退化结果可能因碰巧 0 分被误选。
        bad, ocr = best
        rows = await self._read_damage_rows(image_url)
        if rows:
            ocr2 = lo.splice_damage_rows(ocr, rows)
            bad2, _an2 = _score(ocr2)
            if bad2 < bad:
                logger.warning("[sdjk] 聚焦读行修正：校验失败 %d → %d", bad, bad2)
                return _finish(ocr2)
        return _finish(ocr)

    # ★★ 2026-10-05 去掉字面示例值（阴性对照实测：喂**紫卡图**——卡面根本没有
    #   「伤害」栏——本提示词 8B 9/9、30B 9/9 **原样吐出**旧例子那串数字，
    #   即模型完全不看图、直接抄提示词。而本路径是「聚焦二读」，其输出会被
    #   `splice_damage_rows` 拼进主读结果、校验分够低就**被采纳** ⇒ 静默污染。
    #   旧例子的数字正是某张真实配卡截图（scan_debug/1790654730.jpg）的真值：
    #   冲击 545.6 / 切割 34.1 / 102.3 / 毒素 1,125.3 / 总计 1,807.3。
    #   ⇒ **本提示词不得再出现任何具体数值**，格式一律用占位符表达。
    _ROWS_PROMPT = (
        "只读这张 Warframe 截图左侧「伤害」栏里的每一行数值，"
        "按从上到下的顺序输出 JSON（不要 markdown 围栏、不要解释）：\n"
        '{"rows": [["行名", 数值], ["行名", 数值]]}\n'
        "要求：伤害栏里的每一行都要给（含最后一行「总计」）；"
        "行名与数值严格对齐；数字保留小数、去掉千分位逗号；"
        "看不清的行填 null。除了这个 JSON 什么都不要输出。"
    )

    async def _read_damage_rows(self, image_url: str) -> Optional[list]:
        """聚焦识别：只读伤害栏的「行名+数值」。失败返回 None。

        ★ 只取前 2 个渠道 + 单次 75s 上限（2026-09-23）：此处已是竞速后的
        补救路径，串行遍历全部渠道会把最坏等待拉到渠道超时的总和。
        """
        import re as _re
        import uuid as _uuid

        for prov in self._vision_providers()[:2]:
            pid = getattr(getattr(prov, "meta", lambda: None)(), "id", "")
            try:
                resp = await asyncio.wait_for(
                    prov.text_chat(
                        prompt=self._ROWS_PROMPT,
                        session_id=f"sdjk-rows-{_uuid.uuid4().hex[:8]}",
                        image_urls=[image_url],
                    ),
                    timeout=75,
                )
                text = (getattr(resp, "completion_text", "") or "").strip()
            except Exception as exc:  # noqa: BLE001
                logger.warning("[sdjk] 聚焦读行 %s 失败：%s", pid, exc)
                continue
            m = _re.search(r"\{.*\}", text or "", _re.S)
            if not m:
                continue
            try:
                rows = json.loads(m.group()).get("rows") or []
            except Exception:  # noqa: BLE001
                continue
            out = []
            for r in rows:
                if isinstance(r, (list, tuple)) and len(r) >= 2:
                    label = str(r[0]).strip()
                    v = lo.to_float(r[1])
                    if label and v is not None:
                        out.append([label, v])
            if len(out) >= 3:
                return out
            logger.warning("[sdjk] 聚焦读行 %s 只读到 %d 行，换下一个渠道", pid, len(out))
        return None

    async def _extract_loadout_from_image(self, image_url: str, prov=None) -> Optional[dict]:
        """调**单个** vision 渠道读配卡截图，返回结构化 dict 或 None。

        prov=None 时取首选渠道。多渠道路由与「边到边校验取最优」在
        _extract_loadout_validated 里做（那边能看到面板校验结果）。
        """
        import time as _t

        targets = [prov] if prov is not None else self._vision_providers()[:1]
        targets = [p for p in targets if p is not None]
        if not targets:
            return None

        async def _one(p):
            pid = getattr(getattr(p, "meta", lambda: None)(), "id", "")
            _t0 = _t.perf_counter()
            try:
                import uuid as _uuid

                resp = await p.text_chat(
                    prompt=lo.VISION_PROMPT,
                    session_id=f"sdjk-loadout-{_uuid.uuid4().hex[:8]}",
                    image_urls=[image_url],
                )
                text = (getattr(resp, "completion_text", "") or "").strip()
            except Exception as exc:  # noqa: BLE001
                logger.warning("[sdjk] 配卡识别 %s 调用失败：%s", pid, exc)
                return None
            ms = (_t.perf_counter() - _t0) * 1000
            ocr = lo.parse_vision_json(text or "") if text else None
            if ocr and (ocr.get("weapon") or ocr.get("mods")):
                logger.info("[sdjk] 配卡识别 %s 返回结构（%.0f ms）", pid, ms)
                return ocr
            logger.warning("[sdjk] 配卡识别 %s 未读出有效内容（%.0f ms）", pid, ms)
            return None

        tasks = [asyncio.create_task(_one(p)) for p in targets]
        try:
            for coro in asyncio.as_completed(tasks):
                ocr = await coro
                if ocr:
                    return ocr
        finally:
            for t in tasks:
                t.cancel()
        return None
