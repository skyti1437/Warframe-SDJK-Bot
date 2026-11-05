# -*- coding: utf-8 -*-
"""配卡截图识别域（D10 自 main.py 迁入，方法体逐字未改）。

覆盖路由键 2 项：scan / scandamage；_fit_scan_image 为 MUST_BE_STATIC
（tests/test_method_contract 12+ 文件扫描持续断言）。
模块级依赖 _num（配置数值容错）经 base.py 单源提供（双处消费禁双源）。
子包纪律：不 import astrbot（事件对象鸭子类型）。
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import re
import secrets
import time
from typing import Optional

from .. import damage_calc as dc
from .. import formatters as fmt
from .. import loadout_ocr as lo
from .. import pips as pips_engine
from ..logging_compat import logger
from .base import Action, Reply, _num, err_card
from .vision import _cancel_vision_tasks, _prov_id, effective_vision_prompt


_SCAN_TTL = 1800
_SCAN_LIMIT = 128


def _scan_recognition(text: str, parsed: dict, prompt: str, provider_id: str, task: str) -> dict:
    """仅留在识别结果内的来源证据；不写日志或落盘。"""
    return {
        "source": "model",
        "task": task,
        "raw_text": text,
        "parsed": copy.deepcopy(parsed),
        "prompt_hash": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "provider_id": provider_id,
    }


def _scan_revision(
    candidate: dict, previous: dict, focused: dict, fields: list[str], task: str
) -> dict:
    """采纳补读时保留主读证据，并标明哪些最终字段来自补读/代码合并。"""
    evidence = copy.deepcopy(previous.get("_recognition")) or {"source": "provided_ocr"}
    focused_evidence = copy.deepcopy(focused.get("_recognition")) or {
        "source": "focused_read",
        "raw_text": None,  # 旧标题 str 接口/外部调用方未提供原响应，不冒充 raw。
        "parsed": copy.deepcopy({key: focused[key] for key in fields if key in focused}),
    }
    evidence.setdefault("focused_reads", []).append({"task": task, **focused_evidence})
    evidence.setdefault("revisions", []).append(
        {
            "task": task,
            "source": "code_merge_of_focused_read",
            "fields": fields,
            "values": copy.deepcopy({key: candidate.get(key) for key in fields}),
        }
    )
    candidate["_recognition"] = evidence
    return candidate


def _scan_scope(host, event) -> tuple[str, str]:
    """用完整会话和发送者绑定记录；缺任一标识时不共享缓存。"""
    return (
        str(getattr(event, "unified_msg_origin", "") or ""),
        str(host._safe_sender(event) or ""),
    )


def _scan_state(host, now: float) -> tuple[dict, dict]:
    """懒初始化并清理过期/超额记录，保留其他人的有效记录。"""
    states = []
    for name in ("_last_scan", "_scan_records"):
        state = getattr(host, name, None)
        if not isinstance(state, dict):
            state = {}
            setattr(host, name, state)
        for key, record in list(state.items()):
            if now - record[0] >= _SCAN_TTL:
                state.pop(key, None)
        excess = len(state) - _SCAN_LIMIT
        if excess > 0:
            oldest = sorted(state, key=lambda key: state[key][0])[:excess]
            for key in oldest:
                state.pop(key, None)
        states.append(state)
    return states[0], states[1]


class ScanCommands:
    """Mixin：识卡 / 伤害截图 handler（挂载于 main.WarframeSDJK）。"""

    def _scan_equipment_providers(self) -> list:
        """装备文字可单独选渠道；留空继承识卡主读，显式失效不外发其它渠道。"""
        want = self._vision_route_value("vision_provider_id_scan_equipment")
        if want is None:
            return []
        if not want:
            return self._scan_vision_providers()[:1]
        provider = self._try_get_provider(want)
        if provider is not None:
            return [provider]
        logger.warning("[sdjk] 配卡装备文字渠道「%s」不可用，本次不补读装备文字", want)
        return []

    def _scan_vision_providers(self) -> list:
        """隔离模式仅用识卡专用项，旧模式保留留空继承兼容主渠道。"""
        isolated = self._vision_routes_isolated()
        want = (
            self._vision_route_value("vision_provider_id_scan")
            if isolated
            else self._cfg_str("vision_provider_id_scan")
        )
        if want is None:
            return []
        if not want:
            return [] if isolated else self._vision_providers()
        prov = self._try_get_provider(want)
        if prov:
            return [prov]
        if isolated:
            logger.warning("[sdjk] 隔离模式的识卡渠道「%s」不可用，请配置有效专用渠道", want)
            return []
        logger.warning(
            "[sdjk] 配置的 vision_provider_id_scan「%s」取不到；为免截图外发到未指定的渠道，"
            "本次识卡不回落其它渠道 —— 请在面板选择可用的识卡渠道，或清空该项沿用主渠道",
            want,
        )
        return []

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
                    "◆ 用法：发一张武器「升级」界面截图，配上文字「识卡」",
                    "◆ 说明",
                    "· 读出　武器（含等级）＋每张 MOD 卡与其实际等级＋面板数值",
                    "· 折算　加成按卡片容量数字反推实际等级（绿=匹配减半／红=不合+25%／白=原价）",
                    "· 校验　再用面板数值交叉校验；伤害详情可用「识卡伤害」查看（同「伤害计算」公式）",
                    "· 复算　想换敌人/等级/爆头：识卡伤害 对 重机枪手 150级 爆头",
                    "· 前置　需要 AstrBot 里配好一个多模态模型（如 Qwen3-VL / glm-4.1v）",
                ],
            )

        # ★★ 安全审查（2026-09-18）：识卡每次会调用**付费**多模态模型。
        #   原来只有「同会话串行」——那只防自己连发，防不住「同一个人狂刷」
        #   和「多个群同时刷」，而宿主是 2 核小机器：并发几路就能把 CPU 打到
        #   渲染 39s+，账单也会跟着涨。这里加两道闸（都能在面板关掉/调整）：
        #     ① 同一发送者冷却 scan_cooldown 秒（默认 15，设 0 关闭）
        #     ② 全局并发上限 scan_max_concurrent（默认 3，设 0 关闭）
        #   两道闸的实现在 vision._vision_gate（2026-10-10 起与紫卡识图共用）。
        _last = self._vision_last("scan")
        _gate, _sender = self._vision_gate(event, "识卡", task="scan")
        if _gate is not None:
            return _gate
        _stamp = _last.get(_sender)
        _cd = _num(self.cfg, "scan_cooldown", 15)

        # 同一会话串行：识卡是「多次 vision 调用 + 双页渲染」的重活，
        # 连发会让容器 CPU 争抢（实测单张渲染 0.7s → 39s）。
        # 无会话标识也必须计入机器预算；独立对象只负责本请求，不冒充可共享会话。
        _busy_key = str(getattr(event, "unified_msg_origin", "") or "") or object()
        if _busy_key in self._ocr_busy:
            return Reply(
                raw_text="⏳ 上一张配卡还在识别中（约 15–30 秒），请稍等一下再发，避免任务叠加变慢"
            )
        self._ocr_busy.add(_busy_key)
        try:
            imgs = await self._image_data_urls(event)
            if not imgs:
                return Reply(raw_text="⚠️ 图片下载失败，请重发一次截图")
            ocr = await self._extract_loadout_validated(imgs[0])
        finally:
            self._ocr_busy.discard(_busy_key)
        if not ocr:
            # ★ 识别失败返还冷却（2026-09-23）：防刷闸不该惩罚「渠道全挂」的
            #   受害者——失败重试不该干等 15 秒。成功调用才占冷却。
            if _cd > 0 and _sender:
                self._vision_refund(_last, _sender, _stamp)
            return err_card(
                "截图识别失败：视觉渠道没给出可解析结果",
                "先重发一次；仍失败请联系机器人管理员查看日志里的「配卡识别」条目（会写明是哪个渠道、什么原因）",
                ("可能原因", "① 渠道超时/返回空；② 未配视觉模型"),
                title="识卡",
            )

        an = lo.analyze(ocr, ocr.get("_pips_rows"))
        lines = lo.card_lines(an)
        # 认不全时把原始识别结果附上：否则用户只看到「没认出来」，无法判断是图的问题还是库的问题
        if not an.get("ok"):
            lines.append("　原始识别结果：" + self._ocr_echo(ocr))

        # 第一页 analyze 的参考伤害保持；仅把详情页的再次计算改为按需执行。
        cached_spec = None
        if an.get("weapon") and an.get("quality", {}).get("calculation_allowed", False):
            try:
                cached_spec = lo.to_damage_spec(an)
            except Exception:  # noqa: BLE001 —— 缓存失败不影响识卡本身
                cached_spec = None
        token = ""
        scope = _scan_scope(self, event)
        # 新截图不可靠时只撤销本次会话/用户的最近指针；历史按钮仍指向原卡。
        if cached_spec is None:
            latest, _records = _scan_state(self, time.time())
            latest.pop(scope, None)
        if cached_spec is not None and all(part.strip() for part in scope):
            now = time.time()
            latest, records = _scan_state(self, now)
            snapshot = (now, copy.deepcopy(an["weapon"]), copy.deepcopy(cached_spec))
            latest[scope] = snapshot
            # 记录号会写进卡面指令：只用字母数字（不含 _ / -），QQ Markdown 不会因此整卡退回纯文字
            token = secrets.token_urlsafe(9)
            while token in records or not token.isalnum():
                token = secrets.token_urlsafe(9)
            records[token] = (now, *scope, snapshot[1], snapshot[2])
            _scan_state(self, now)

        mode = str(self.cfg.get("scan_detail_mode", "auto")).strip().lower()
        if mode not in {"auto", "lazy", "eager"}:
            mode = "auto"
        try:
            can_actions = bool(getattr(self, "_can_send_actions", lambda _e: False)(event))
        except Exception:  # noqa: BLE001 —— 平台能力不可确认时沿用完整输出
            can_actions = False
        lazy = mode == "lazy" or (mode == "auto" and can_actions and bool(token))
        if lazy and cached_spec is not None:
            actions = []
            if token:
                command = f"识卡伤害 #{token}"
                if can_actions:
                    # 有按钮时只留一行指令（旧客户端可能不显示按钮），复算示例交给按钮
                    lines.append(
                        f"※ 伤害详情：点下方按钮，或发送「{command}」；30 分钟内仅本人可用"
                    )
                    actions = [
                        Action("伤害详情", command, owner_only=True),
                        Action("换目标复算", command + " 对 ", mode="fill", owner_only=True),
                    ]
                else:
                    lines.append(f"※ 伤害详情：发送「{command}」；30 分钟内仅本人在当前会话可用")
                    lines.append(f"· 换目标复算：{command} 对 重机枪手 150级 爆头")
            else:
                lines.append(
                    "※ 未取得会话或用户标识，无法保存复算记录；可用「伤害 武器名」手动配卡计算"
                )
            return Reply("配卡识别", lines, actions=actions)
        detail = self._loadout_detail_lines(an) if cached_spec is not None else None
        if detail:
            return Reply(pages=[("配卡识别", lines), ("配卡识别 · 伤害详情", detail)])
        return Reply("配卡识别", lines)

    @staticmethod
    def _ocr_echo(ocr: dict, limit: int = 300) -> str:
        """「认不全」时回显的识别结果：**只取白名单结构化字段**并截断。

        视觉模型输出可被图中文字注入任意文本（此前整段 json.dumps 回显 700 字），
        这里只留武器名 / 容量 / 每张 MOD 的名称 + 容量数字 + 颜色，逐字段限长、
        去换行与控制字符，总长再截到 ``limit``。
        """

        def _clean(v, n: int) -> str:
            t = re.sub(r"[\x00-\x1f\x7f\u2028\u2029]+", " ", str(v or "")).strip()
            return t[:n] + ("…" if len(t) > n else "")

        parts = []
        weapon = _clean(ocr.get("weapon"), 30)
        parts.append(f"武器「{weapon or '未读出'}」")
        cap = str(ocr.get("capacity") or "")
        if re.fullmatch(r"\s*\d{1,3}\s*/\s*\d{1,3}\s*", cap):
            parts.append(f"容量 {cap.strip()}")
        mods = ocr.get("mods") if isinstance(ocr.get("mods"), list) else []
        shown = []
        for m in mods[:12]:
            if not isinstance(m, dict):
                continue
            name = _clean(m.get("name"), 16) or "?"
            drain = m.get("drain")
            drain_s = str(drain) if isinstance(drain, int) and 0 <= drain < 100 else "?"
            color = str(m.get("color") or "")
            color_s = color if color in ("绿", "红", "白") else "?"
            shown.append(f"{name}({drain_s}{color_s})")
        parts.append(f"MOD {len(mods)} 张：" + ("、".join(shown) or "无"))
        out = "｜".join(parts)
        return out[:limit] + ("…" if len(out) > limit else "")

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
            return fmt.limit_notes(out)
        except Exception:  # noqa: BLE001 —— 第二页是增强项，失败不挡第一页
            logger.exception("[sdjk] 识卡伤害详情页生成失败")
            return None

    async def _h_scan_damage(self, parsed, event, platform) -> Reply:
        """识卡伤害：用本人最近一次识卡或按钮绑定的快照跑伤害计算器。

        用法：识卡伤害 [#记录] [对 敌人名] [派系] [N级] [爆头] [镀层N] [异常N] …
        30 分钟内的识卡缓存有效；MOD 折算值沿用识卡结果，本条指令只覆盖
        目标/条件类参数（敌人、等级、派系、爆头、连击、镀层层数…）。
        """
        scope = _scan_scope(self, event)
        latest, records = _scan_state(self, time.time())
        rest = [str(t).strip() for t in (parsed.content or []) if str(t).strip()]
        token = rest.pop(0)[1:] if rest and rest[0].startswith("#") else None
        hit = None
        if all(part.strip() for part in scope):
            if token is not None:
                record = records.get(token)
                # owner_only 只是界面约束；业务层再次验证完整会话与发送者。
                if record is not None and record[1:3] == scope:
                    hit = (record[0], record[3], record[4])
            else:
                hit = latest.get(scope)
        if hit is None:
            if not all(part.strip() for part in scope):
                reason = "无法确认当前会话或发送者，不能读取识卡记录"
            elif token is not None:
                reason = "该识卡记录不存在、已过期，或不属于当前会话与发送者"
            else:
                reason = "当前会话中，你在 30 分钟内没有识卡记录"
            return Reply(
                "识卡伤害",
                [
                    f"◆ 原因：{reason}",
                    "◆ 用法：先发「识卡 + 武器升级界面截图」，再用本指令复算",
                    "· 示例　识卡伤害 对 重机枪手 150级 爆头",
                    "· 示例　识卡伤害 C系 120级｜识卡伤害 满镀层 异常4 连击120 重击",
                    "※ ⚠ 怎么改：先发一次「识卡」；参数写法与「伤害」指令一致（识卡伤害 = 伤害 + 已识别配卡）",
                ],
            )
        # 深拷贝后再交给计算/格式化；按钮快照和本人最近记录均不接受下游原地修改。
        _ts, weapon, cached = copy.deepcopy(hit)
        # 传 token 列表，不能传会被逐字符拆开的字符串。
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
            reply = Reply(
                "识卡伤害", dc.card_lines(weapon, spec, res, []), footer=dc.card_footer(weapon, res)
            )
            try:
                can_actions = bool(getattr(self, "_can_send_actions", lambda _e: False)(event))
            except Exception:  # noqa: BLE001 —— 平台能力不可确认时不加按钮，正文照常
                can_actions = False
            if can_actions:
                # 详情 / 复算结果后可继续换目标；绑定同一记录，仍由上方会话与发送者校验兜底
                command = f"识卡伤害 #{token}" if token else "识卡伤害"
                reply.actions.append(
                    Action("换目标复算", command + " 对 ", mode="fill", owner_only=True)
                )
                if rest:
                    reply.actions.append(Action("默认目标", command, owner_only=True))
            return reply
        except Exception as exc:  # noqa: BLE001 —— 绝不静默
            logger.exception("[sdjk] 识卡伤害复算失败：%r", exc)
            return Reply(
                "识卡伤害",
                [
                    "◆ 原因：复算出错（内部错误，详情已写入日志）",
                    "※ ⚠ 怎么改：重新发一次「识卡」后再试；参数写法见「伤害」指令的用法页",
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
          按面积比缩到 1600 宽约省一半推理时间（2026-09-20 维护者要求补上）。
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

    @staticmethod
    async def _detect_scan_layout(image_url: str) -> dict:
        """原图确定性几何观测；不调用OCR/模型，坐标与图像字节关联。"""

        def observe():
            import base64
            import io
            from PIL import Image

            raw = base64.b64decode(image_url.split(",", 1)[1])
            with Image.open(io.BytesIO(raw)) as image:
                observed = pips_engine.detect_layout(image)
            observed["image_sha256"] = hashlib.sha256(raw).hexdigest()
            return observed

        if not str(image_url).startswith("data:"):
            return {}
        try:
            return await asyncio.to_thread(observe)
        except Exception as exc:  # noqa: BLE001 - 保留未知，不用异常开计算闸门
            logger.warning("[sdjk] 配卡布局观测失败：%s", type(exc).__name__)
            return {}

    async def _extract_loadout_validated(
        self, image_url: str, max_attempts: int = 2
    ) -> Optional[dict]:
        """识别 + 校验闭环：面板校验不过就换渠道/重试一次，取最好的结果。

        视觉模型偶发漏行/读串（v1.11 实测：毒素行整行漏掉、总计挂到
        穿刺名下），单次识别不可全信 —— 用 analyze 的面板校验当裁判，
        最多试 max_attempts 次；仍不过时追加一次「只读伤害栏」的聚焦
        识别（任务越窄读得越准），把干净的行拼接回去再验。
        """
        original_image_url = image_url
        # ★ 豆子检测要在**缩放前**的原图上做（_detect_pips 的说明）
        pips_rows = await self._detect_pips(image_url)
        pixel_layout = await self._detect_scan_layout(image_url)
        # 缩放 + 重编码是纯 CPU 活（Lanczos），放线程里跑，别卡住事件循环
        image_url = await asyncio.to_thread(self._fit_scan_image, image_url)
        best: Optional[tuple[int, dict]] = None  # (失败项数, ocr)

        def _finish(o: Optional[dict]) -> Optional[dict]:
            """把豆子结果挂到返回的 ocr 上，供上层复用（免得再检测一次）。"""
            if o is not None:
                o["_pips_rows"] = pips_rows
                o["_pixel_layout"] = pixel_layout
            return o

        def _score(ocr: dict):
            """面板校验打分：返回 (失败项数, analyze 结果)，恒为数值。

            ★ 2026-09-23：武器没认出不再「立即接受」——此前第一个返回
            「有 weapon 字段但其实是幻觉」的渠道会直接获胜并取消其它渠道
            （9-20 漏读事故同类风险）。现在按重罚 +6 计入竞速，窗口耗尽后
            才轮到它兜底。「0 张卡」「面板全没读到」「疑似漏读」惩罚不变。
            """
            ocr["_pixel_layout"] = pixel_layout
            lo.observe_capacity_colors(ocr, original_image_url)
            lo.observe_arcane_ranks(ocr, original_image_url)
            an = lo.analyze(ocr, pips_rows)
            # ★ 记一行「豆子有没有真的用上」（2026-09-20 玩家报「北风还是 3 级」后加）：
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
                    # ★ 2026-10-25（Astra 评估 S2）：留存用户截图要**显式开启**（默认关）——
                    #   截图可能带玩家昵称、聊天背景；原先无开关、无告知地留最近 3 张。
                    keep = bool(self.cfg.get("scan_debug_save", False))
                    logger.warning(
                        "[sdjk] ★ 豆子检测到了但**对齐无解**，本次退回容量反推%s",
                        "（截图存到 scan_debug/ 便于排查）"
                        if keep
                        else "（未留存截图；排查时可在配置里开启 scan_debug_save）",
                    )
                    if keep:
                        # 解码 + 存 JPEG 放线程池（_score 是同步内层函数，不等它完成）
                        asyncio.get_running_loop().run_in_executor(
                            None, self._dump_scan_debug, image_url
                        )
            bad = 6 if not an.get("weapon") else 0
            bad += 4 * len(an.get("quality", {}).get("reasons", []))
            bad += 3 * sum(
                r["code"] in {"visible_slot_unread", "inventory_in_loadout"}
                for r in an.get("quality", {}).get("layout_reasons", [])
            )
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
        provs = [p for p in self._scan_vision_providers() if p is not None][:4]
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
                    if t.cancelled():
                        logger.warning("[sdjk] 配卡识别渠道任务已取消，继续等待其它渠道")
                        continue
                    try:
                        ocr = t.result()
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("[sdjk] 配卡识别渠道异常：%s", exc)
                        continue
                    if not ocr:
                        continue
                    bad, an = _score(ocr)
                    if bad == 0:
                        logger.info(
                            "[sdjk] 配卡识别可比数值吻合，继续核对装备区（不代表识别准确性）"
                        )
                        best = (bad, ocr)
                        pending.clear()
                        break
                    logger.warning("[sdjk] 配卡识别一份结果有 %d 项校验不过", bad)
                    if best is None or bad < best[0]:
                        best = (bad, ocr)
        finally:
            await _cancel_vision_tasks(tasks)
        # 完整补读也可接住主读无有效结构的情况；两路各保留独立证据。
        focused = await self._read_scan_focus(image_url, "mods")
        if best is None:
            if not focused or not focused.get("mods") or not focused.get("weapon"):
                return None
            recovered = lo.normalize_scan_structure(focused)
            recovered["_equipment_unconfirmed"] = True
            _score(recovered)
            recovered = await self._read_equipment_tiles(
                recovered, original_image_url, pips_rows, pixel_layout
            )
            _score(recovered)
            return _finish(recovered)
        bad, ocr = best
        # 数值吻合仍须核对装备区：亮豆下界覆盖不到零级卡和部分特殊槽。
        confirmed = False
        if focused and focused.get("mods"):
            old_analysis = lo.analyze(ocr, pips_rows)
            old_overread = any(
                r["code"] == "mods_overread" for r in old_analysis["quality"]["reasons"]
            )
            merged_mods = (
                focused["mods"]
                if old_overread
                else lo.merge_focus_mods(ocr.get("mods") or [], focused["mods"])
            )
            candidate = lo.merge_equipment_read(ocr, focused)
            if candidate.get("_equipment_merge", {}).get("complete_shared_identity"):
                merged_mods = candidate["mods"]
            score, analysis = _score(candidate)
            old_names = {m.get("name") for m in old_analysis.get("mods", []) if m.get("found")}
            new_names = {m.get("name") for m in analysis.get("mods", []) if m.get("found")}
            more_complete = (
                len(candidate["mods"]) > len(ocr.get("mods") or []) and old_names <= new_names
            )
            repaired_overread = old_overread and score < bad and len(candidate["mods"]) <= 10
            if (
                (
                    merged_mods is not None
                    and score <= bad
                    and len(candidate["mods"]) >= len(ocr.get("mods") or [])
                )
                or (merged_mods is not None and more_complete)
                or repaired_overread
            ):
                fields = ["mods"] + (
                    ["arcanes"] if candidate.get("arcanes") != ocr.get("arcanes") else []
                )
                if focused.get("weapon"):
                    fields.append("weapon")
                bad, ocr = score, _scan_revision(candidate, ocr, focused, fields, "scan_mods")
                confirmed = True
        # —— 武器名单读：主读没认出武器时，用一句话只问标题栏，换上后同一把尺重新打分 ——
        # ★ 2026-10-27（ZCode 消融）：qwen-vl-ocr 在长 JSON 提示词下 3/3 把首张姿态卡名当武器名，
        #   单问「左上角标题栏斜杠后面的文字」2/2 读对 ⇒ 问题是提示词过载，拆成独立最小调用。
        if not lo.analyze(ocr, pips_rows).get("weapon"):
            title = await self._read_weapon_title(image_url)
            if title:
                ocr_w = {**ocr, "weapon": title}
                bad_w, _an_w = _score(ocr_w)
                if bad_w < bad:
                    logger.warning(
                        "[sdjk] 武器名单读修正：%r → %r，校验失败 %d → %d",
                        ocr.get("weapon"),
                        title,
                        bad,
                        bad_w,
                    )
                    bad, ocr = (
                        bad_w,
                        _scan_revision(ocr_w, ocr, {"weapon": title}, ["weapon"], "scan_title"),
                    )
        if not confirmed:
            ocr = {**ocr, "_equipment_unconfirmed": True}
            bad, _ = _score(ocr)
        if bad:
            focused = await self._read_scan_focus(image_url, "panel")
            if focused and focused.get("panel"):
                old = lo.analyze(ocr, pips_rows)
                panel = focused["panel"]
                if old["quality"]["panel_safe"]:
                    panel = {**ocr.get("panel", {}), **panel}
                    total = lo._panel_total(old.get("panel", {}))
                    if total is not None and "total" not in focused["panel"]:
                        panel["total"] = total
                candidate = {**ocr, "panel": panel}
                score, analysis = _score(candidate)
                # 独立分区读到的标签不能因旧错读恰好自洽而落选。
                scalar_count = sum(
                    lo.to_float(panel.get(key)) is not None for key in lo._PANEL_SCALARS
                )
                damage_count = len(panel.get("damage_rows") or [])
                enough_rows = scalar_count >= 3 or (scalar_count >= 2 and damage_count >= 2)
                if analysis["quality"]["panel_safe"] and (enough_rows or score < bad):
                    bad, ocr = (
                        score,
                        _scan_revision(candidate, ocr, focused, ["panel"], "scan_panel"),
                    )
        if not ocr.get("arcanes"):
            focused = await self._read_scan_focus(image_url, "arcanes")
            if focused and focused.get("arcanes"):
                ocr = _scan_revision(
                    {**ocr, "arcanes": focused["arcanes"]},
                    ocr,
                    focused,
                    ["arcanes"],
                    "scan_arcanes",
                )
        ocr = await self._read_equipment_tiles(ocr, original_image_url, pips_rows, pixel_layout)
        _score(ocr)
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

    # 武器名单读（2026-10-27）：一句话、不带任何示例 —— 长提示词里夹着武器名时 OCR 模型抓不住重点
    _TITLE_PROMPT = "读这张图左上角标题栏里斜杠后面的文字，只回答那几个字。"

    @staticmethod
    def _scan_focus_image(image_url: str, part: str, *, with_geometry: bool = False):
        """升级界面左右分区放大；失败保留原图。只裁内容，不填补或改写数字。"""
        import base64
        import io

        from PIL import Image as PILImage

        try:
            raw = base64.b64decode(image_url.split(",", 1)[1])
            image = PILImage.open(io.BytesIO(raw)).convert("RGB")
            source_size = image.size
            edge = int(image.width * 0.28)
            bottom = image.height
            if part == "mods":
                # 库存与装备区同时可见时，按像素卡行截掉库存，不能只靠提示词排除。
                rows = pips_engine.detect_pips(image)
                equipped = [r for r in rows if not r.get("is_inventory") and r.get("band")]
                inventory = [r for r in rows if r.get("is_inventory") and r.get("band")]
                if equipped and inventory:
                    cw = pips_engine._card_w_hint(pips_engine._scan(image), image.width)
                    scale = (
                        pips_engine.CARD_W_REF / cw if cw > pips_engine.CARD_W_REF * 1.05 else 1.0
                    )
                    last = max(r["band"][1] for r in equipped)
                    first = min(r["band"][0] for r in inventory)
                    width = max(r.get("card_w", 0) for r in equipped)
                    cutoff = last + width * 0.35
                    if last < cutoff < first:
                        bottom = min(bottom, round(cutoff / scale))
            bounds = (
                (int(image.width * 0.80), 0, image.width, int(image.height * 0.52))
                if part == "arcanes"
                else (
                    (0, 0, edge, image.height)
                    if part == "panel"
                    else (edge, 0, image.width, bottom)
                )
            )
            image = image.crop(bounds)
            scale = min(1280 / image.width, 4.0)
            if scale > 1:
                image = image.resize(
                    (round(image.width * scale), round(image.height * scale)),
                    PILImage.Resampling.LANCZOS,
                )
            buffer = io.BytesIO()
            image.save(buffer, "PNG")
            url = "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()
            transform = {
                "bounds": [
                    bounds[0] / source_size[0],
                    bounds[1] / source_size[1],
                    bounds[2] / source_size[0],
                    bounds[3] / source_size[1],
                ],
                "source_image_size": list(source_size),
                "output_image_size": list(image.size),
            }
            return (url, transform) if with_geometry else url
        except Exception:  # noqa: BLE001 —— 裁剪失败不影响原来的识别入口
            return (image_url, None) if with_geometry else image_url

    _FOCUS_PROMPTS = {
        "panel": "按从上到下的顺序逐行抄录图中文字及同一行对应的全部数字，保留箭头两边的数和单位。只抄原文，不解释、不补全。",
        "mods": "读取 Warframe 装备升级截图中的文字，只输出紧凑、合法的单行 JSON，不解释。\nweapon：左上角升级标题中的装备名；capacity：剩余/总容量原文。\nmods：按装备槽位从上到下、从左到右列出卡片，包括姿态、光环、特殊槽和右侧辅助槽。每项只含 name（完整卡名，保留 Prime）、drain（右上角容量数字，字符串）、color（该数字颜色：绿/红/白/未知）。空槽不列入；仓库候选卡、栏目标题和按钮不列入。\narcanes：赋能列表，每项只含 name。不要把赋能当作 MOD。\npanel：左侧属性面板，rows 每项含 label、left、right、unit。有比较箭头时抄两侧值，只有一列时 right 填 null。数值按原文保留符号和单位，不换算。\n名称或容量看不清填 null，保留该卡位置；不要按常用配卡补造内容。定位和等级由另一条读图链及代码处理，此处不输出坐标、极性图形、等级或分析。每项字段只出现一次，无赋能用空数组。",
        "arcanes": lo.VISION_PROMPT
        + " This is a cropped equipment arcane region. Read only visible text; do not infer the name or rank from the icon.",
    }

    async def _read_scan_focus(self, image_url: str, part: str) -> Optional[dict]:
        """补读原图分区；接受坐标OCR和结构化行，保留左右值与量纲。"""
        import uuid

        prompt = ScanCommands._FOCUS_PROMPTS[part]
        if part == "mods":
            # 装备区补读同时确认标题、特殊槽和赋能；共同提示词使用完整同一张图。
            image, transform = image_url, {"bounds": [0, 0, 1, 1]}
        elif self._scan_focus_image is ScanCommands._scan_focus_image:
            image, transform = await asyncio.to_thread(
                self._scan_focus_image, image_url, part, with_geometry=True
            )
        else:
            image = await asyncio.to_thread(self._scan_focus_image, image_url, part)
            transform = None  # 旧适配器未提供裁剪元数据，禁止拿裁图坐标绑定全图
        providers = (
            self._scan_equipment_providers() if part == "mods" else self._scan_vision_providers()
        )
        for prov in providers[:1]:
            try:
                actual_prompt = effective_vision_prompt(self, prov, f"scan_{part}", prompt)
                response = await asyncio.wait_for(
                    prov.text_chat(
                        prompt=actual_prompt,
                        session_id=f"sdjk-focus-{uuid.uuid4().hex[:8]}",
                        image_urls=[image],
                    ),
                    timeout=75,
                )
                text = str(getattr(response, "completion_text", "") or "")
                parsed = lo.parse_focus_text(text, part)
                if parsed:
                    parsed["_recognition"] = _scan_recognition(
                        text, parsed, actual_prompt, _prov_id(prov), f"scan_{part}"
                    )
                    if part in {"mods", "arcanes"}:
                        parsed = lo.normalize_scan_structure(parsed)
                        parsed = lo.remap_mod_geometry(parsed, transform)
                        parsed["_recognition"]["geometry_transform"] = copy.deepcopy(transform)
                    return parsed
            except Exception as exc:  # noqa: BLE001 —— 不泄露原始响应，失败保留主读结果
                logger.warning("[sdjk] 配卡分区补读失败：%s", type(exc).__name__)
        return None

    _TILES_PROMPT = (
        "读取拼图里的独立 Warframe MOD 卡片。每个区域上方的 T 编号只是区域标记。"
        '输出 JSON：{"mods":[{"tile":"区域编号","name":"卡面完整名称或null","drain":"右上容量数字或null","color":"容量数字颜色：红/绿/白/未知"}]}。'
        "每个区域只记录一次，空槽或只有背景时name为null；不要把界面标签、图标、边框当卡名或容量数字。"
        "保留Prime和完整前后缀，不补缺字，不计算等级，不猜看不清的数字。"
    )

    @staticmethod
    def _equipment_tile_image(image_url, rows, layout):
        import base64
        import io
        from PIL import Image

        image = Image.open(io.BytesIO(base64.b64decode(image_url.split(",", 1)[1])))
        sheet, tiles = pips_engine.equipment_tiles(image, rows, layout)
        if sheet is None:
            return None, []
        stream = io.BytesIO()
        sheet.save(stream, format="PNG")
        return "data:image/png;base64," + base64.b64encode(stream.getvalue()).decode(), tiles

    async def _read_equipment_tiles(self, ocr, image_url, rows, layout):
        import uuid

        analysis = lo.analyze(ocr, rows)
        if analysis.get("mods") and all(
            m.get("rank") is not None and m.get("found") for m in analysis["mods"]
        ):
            return ocr
        try:
            image, tiles = await asyncio.to_thread(
                self._equipment_tile_image, image_url, rows, layout
            )
            if not image:
                return ocr
            for provider in self._scan_equipment_providers()[:1]:
                prompt = effective_vision_prompt(self, provider, "scan_tiles", self._TILES_PROMPT)
                response = await asyncio.wait_for(
                    provider.text_chat(
                        prompt=prompt,
                        image_urls=[image],
                        session_id=f"sdjk-tiles-{uuid.uuid4().hex[:8]}",
                    ),
                    timeout=60,
                )
                text = str(getattr(response, "completion_text", "") or "")
                observed = lo.parse_vision_json(text)
                if not observed:
                    return ocr
                observed["_recognition"] = _scan_recognition(
                    text, observed, prompt, _prov_id(provider), "scan_tiles"
                )
                candidate = lo.merge_tile_read(ocr, observed, tiles)
                return _scan_revision(candidate, ocr, observed, ["mods"], "scan_tiles")
        except Exception as exc:  # noqa: BLE001 — 复读失败保留原始结果
            logger.warning("[sdjk] 配卡槽位复读失败：%s", type(exc).__name__)
        return ocr

    @staticmethod
    def _parse_damage_rows(text: str) -> list:
        parsed = lo.parse_vision_json(text) or {}
        result = []
        for row in parsed.get("rows") or []:
            if isinstance(row, dict):
                label = row.get("label", row.get("name", row.get("行名")))
                value = row.get("value", row.get("数值"))
                row = [label, value]
            if isinstance(row, (list, tuple)) and len(row) >= 2 and row[0]:
                if any(lo.to_float(v) is not None for v in row[1:]):
                    result.append(list(row))
        return result

    async def _read_weapon_title(self, image_url: str) -> Optional[str]:
        """单读标题栏的武器名（原文，交给 clean_weapon_name 清洗）。失败返回 None。前 2 个渠道、单次 45s。"""
        import uuid as _uuid

        for prov in self._scan_vision_providers()[:2]:
            pid = _prov_id(prov)
            try:
                resp = await asyncio.wait_for(
                    prov.text_chat(
                        prompt=effective_vision_prompt(
                            self, prov, "scan_title", self._TITLE_PROMPT
                        ),
                        session_id=f"sdjk-title-{_uuid.uuid4().hex[:8]}",
                        image_urls=[image_url],
                    ),
                    timeout=45,
                )
                text = (getattr(resp, "completion_text", "") or "").strip()
            except Exception as exc:  # noqa: BLE001
                logger.warning("[sdjk] 武器名单读 %s 失败：%s", pid, exc)
                continue
            # 只要第一行、去掉引号；过长的多半是把整屏读出来了，换下一个渠道
            line = text.splitlines()[0].strip().strip("「」\"'`") if text else ""
            if line and len(line) <= 40 and lo.clean_weapon_name(line):
                return line
            logger.warning("[sdjk] 武器名单读 %s 返回不像标题：%r", pid, text[:60])
        return None

    async def _read_damage_rows(self, image_url: str) -> Optional[list]:
        """聚焦识别：只读伤害栏的「行名+数值」。失败返回 None。

        ★ 只取前 2 个渠道 + 单次 75s 上限（2026-09-23）：此处已是竞速后的
        补救路径，串行遍历全部渠道会把最坏等待拉到渠道超时的总和。
        """
        import uuid as _uuid

        for prov in self._scan_vision_providers()[:2]:
            pid = _prov_id(prov)
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
            out = self._parse_damage_rows(text)
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

        targets = [prov] if prov is not None else self._scan_vision_providers()[:1]
        targets = [p for p in targets if p is not None]
        if not targets:
            return None

        async def _one(p):
            pid = _prov_id(p)
            _t0 = _t.perf_counter()
            try:
                import uuid as _uuid

                prompt = effective_vision_prompt(self, p, "scan_main", lo.VISION_PROMPT)
                resp = await p.text_chat(
                    prompt=prompt,
                    session_id=f"sdjk-loadout-{_uuid.uuid4().hex[:8]}",
                    image_urls=[image_url],
                )
                raw_text = str(getattr(resp, "completion_text", "") or "")
                text = raw_text.strip()
            except Exception as exc:  # noqa: BLE001
                logger.warning("[sdjk] 配卡识别 %s 调用失败：%s", pid, exc)
                return None
            ms = (_t.perf_counter() - _t0) * 1000
            ocr = (lo.parse_coordinate_ocr(text) or lo.parse_vision_json(text)) if text else None
            if ocr and (ocr.get("weapon") or ocr.get("mods")):
                ocr["_recognition"] = _scan_recognition(raw_text, ocr, prompt, pid, "scan_main")
                ocr = lo.normalize_scan_structure(ocr)
                logger.info("[sdjk] 配卡识别 %s 返回结构（%.0f ms）", pid, ms)
                return ocr
            logger.warning("[sdjk] 配卡识别 %s 未读出有效内容（%.0f ms）", pid, ms)
            return None

        # targets 恒为单渠道；直接等待，让上一层竞速独占任务的取消与回收。
        return await _one(targets[0])
