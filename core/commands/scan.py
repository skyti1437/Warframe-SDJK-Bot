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
from concurrent.futures import ThreadPoolExecutor
from itertools import count
from typing import Optional

from .. import damage_calc as dc
from .. import damage_engine as de
from .. import formatters as fmt
from .. import loadout_ocr as lo
from .. import pips as pips_engine
from ..i18n import L, is_en
from ..logging_compat import logger
from ..request_queue import QueueUnavailable, send_queue_position
from .base import Action, Reply, _num, err_card
from .vision import (
    _IMAGE_DOWNLOAD_ERROR,
    _ImageRejected,
    _MAX_DERIVED_PIXELS,
    _SCAN_BUDGET,
    _cancel_vision_tasks,
    _prov_id,
    _scan_fit_size,
    _scan_size_ok,
    _vision_chat,
    _vision_slot,
    _vision_thread,
    effective_vision_prompt,
)
from .wiki_misc import _dmg_en_toks

_SCAN_TTL = 1800
_SCAN_LIMIT = 128
_SCAN_SEQUENCE = count(1)
_SCAN_IMAGE_FIELDS = ("_upgrade_slots", "_pixel_layout", "_tile_slots", "_pips_rows")
_SCAN_EXPERIMENTAL = (
    "※ 识卡为实验功能，默认关闭。识别可能较慢或不完整。",
    "※ Build Scan is experimental and off by default. Scans may be slow or incomplete.",
)


_SCAN_WORKERS = ThreadPoolExecutor(max_workers=2, thread_name_prefix="sdjk-scan")


async def _scan_cpu(fn, *args, **kwargs):
    return await _vision_thread(fn, *args, executor=_SCAN_WORKERS, **kwargs)


class _ScanBudget:
    def __init__(self, seconds):
        self.now = asyncio.get_running_loop().time
        self.deadline = self.now() + seconds
        self.partial = None

    async def run(self, fn, *args, **kwargs):
        remaining = self.deadline - self.now()
        if remaining <= 0:
            raise asyncio.TimeoutError
        task = asyncio.create_task(fn(*args, **kwargs))
        try:
            done, _ = await asyncio.wait([task], timeout=remaining)
            if not done:
                raise asyncio.TimeoutError
            return task.result()
        finally:
            if not task.done():
                await _cancel_vision_tasks([task])
            elif not task.cancelled():
                task.exception()  # 调用方取消与任务失败可能在同一轮到达。

    def remember(self, ocr, *, equipment_confirmed=False):
        self.partial = {**ocr}
        if not equipment_confirmed:
            self.partial["_equipment_unconfirmed"] = True


def _parse_scan_main(text):
    return lo.parse_coordinate_ocr(text) or lo.parse_vision_json(text) or {}


def _scan_task_image(image_url, part, crop):
    if part == "mods":
        return image_url, {"bounds": [0, 0, 1, 1]}
    if crop is ScanCommands._scan_focus_image:
        return crop(image_url, part, with_geometry=True)
    return crop(image_url, part), None


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


async def _scanned_damage_result(host, weapon, spec, overrides, event, platform):
    cfg, note = getattr(host, "cfg", None), ""
    if port := de.portable_port(cfg):
        try:
            lines = await de.portable_fixed_lines(host, port, weapon, spec, overrides)
        except de.ScanReplayUnconfirmed as exc:
            return [L("◆ 待核对 ", "◆ Needs confirmation ") + str(exc)], False, ""
        except (de.EngineError, QueueUnavailable) as exc:
            note = str(exc)
            if spec.get("evo_keys") and "loadout_slots" in spec:
                return [L("◆ 独立引擎 ", "◆ Standalone engine ") + note], False, ""
        else:
            footer = fmt.fmt_platform_footer(
                platform, L("按已识别的配装计算", "Uses the scanned loadout")
            )
            return lines, True, footer
    elif de.full_enabled(cfg):
        lines, ok = await de.simulation_lines(host, weapon, spec, overrides, event=event)
        return lines, ok, ""

    if spec.get("builtin_calculation_allowed") is False:
        lines = [
            L(
                "◆ 原图还有未核对的面板或等级，内置估算暂不可用",
                "◆ Built-in estimate requires verified screenshot stats and ranks",
            )
        ]
        ok, footer = False, ""
    else:
        result = lo.calculate_bundled_scan(spec, weapon)
        lines = [
            L("· 计算：内置估算", "· Engine: Built-in estimate"),
            *dc.card_lines(weapon, spec, result, []),
        ]
        ok, footer = result.get("ok"), dc.card_footer(weapon, result)
    if note:
        lines.insert(0, L(f"※ 内置估算 · {note}", f"※ Built-in estimate · {note}"))
    return lines, ok, footer


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

        读取 MOD 名、容量数字和面板，用同槽等级刻度及数字颜色核对等级，
        再按已确认等级折算加成。
        """
        if not self._event_has_image(event):
            return Reply(
                L("配卡识别", "Build Scan"),
                L(
                    [
                        "◆ 用法：发一张武器「升级」界面截图，配上文字「识卡」",
                        _SCAN_EXPERIMENTAL[0],
                        "◆ 说明",
                        "· 读出　武器（含等级）＋每张 MOD 卡与其实际等级＋面板数值",
                        "· 定级　数同槽亮刻度，用右上容量数字及颜色核对",
                        "· 普通槽扣容量　绿减半向上取整／红多扣约25%／白或卡面本色原价",
                        "· 架势和光环加容量　绿加倍／红约为原值的80%／白或卡面本色原值",
                        "· 校验　再用面板数值交叉校验；伤害详情可用「识卡伤害」查看（同「伤害计算」公式）",
                        "· 复算　想换敌人/等级/爆头：识卡伤害 对 重机枪手 150级 爆头",
                        "· 前置　需要 AstrBot 里配好一个多模态模型（如 Qwen3-VL / glm-4.1v）",
                    ],
                    [
                        '◆ Usage: send a screenshot of the weapon "Upgrade" screen with the text "scan"',
                        _SCAN_EXPERIMENTAL[1],
                        "◆ Details",
                        "· Reads　the weapon (with rank) + every MOD card with its actual rank + panel values",
                        "· Rank　count the lit pips on the same card and cross-check its capacity number and color",
                        "· Regular mods consume capacity　green halves it, rounded up; red adds about 25%; "
                        "the neutral color uses the base cost",
                        "· Stances and auras supply capacity　green doubles it; red supplies about 80%; "
                        "the neutral color uses the base value",
                        '· Verification　cross-checked against the panel values; details via "scandamage" '
                        '(same formula as "damage")',
                        "· Recalculate　change enemy / level / headshot: scandamage vs Heavy Gunner 150 headshot",
                        "· Requirement　a multimodal model must be configured in AstrBot (e.g. Qwen3-VL / glm-4.1v)",
                    ],
                ),
            )

        # 先检查付费视觉请求的冷却，再登记等待。下载图片与调用模型等到有运行名额才开始。
        _last = self._vision_last("scan")
        _gate, _sender = self._vision_gate(
            event, L("识卡", "Build scan"), task="scan", check_budget=False
        )
        if _gate is not None:
            return _gate
        _stamp = _last.get(_sender) if _num(self.cfg, "scan_cooldown", 15) > 0 else None
        queue = self._scan_requests()

        # 一个会话只占一个识卡名额。等待中的请求也占该会话的位置。
        # 无会话标识也必须计入机器预算；独立对象只负责本请求，不冒充可共享会话。
        _busy_key = str(getattr(event, "unified_msg_origin", "") or "") or object()
        if _busy_key in self._ocr_busy or queue.is_waiting(_busy_key):
            self._vision_refund(_last, _sender, _stamp)
            return Reply(
                raw_text=L(
                    "⏳ 本会话的上一张配卡还在识别或排队，请等结果出来后再发",
                    "⏳ This chat already has a build being scanned or queued. Wait for its result.",
                )
            )

        def room():
            cap = self._vision_limit()
            return len(getattr(self, "_scan_running", ())) < queue.workers and (
                cap <= 0 or len(self._ocr_busy) < cap
            )

        async def notify(position):
            await send_queue_position(
                event, getattr(self, "_messaging", None), L("配卡识别", "Build scan"), position
            )

        budget = _ScanBudget(self.RACE_WINDOW_S)
        scope = _scan_scope(self, event)
        if not hasattr(self, "_scan_sequences"):
            self._scan_sequences = {}
        sequence = None
        try:
            async with queue.slot(
                notify,
                room=room,
                key=_busy_key,
                wait_timeout=max(0, budget.deadline - budget.now()),
            ):
                with _vision_slot(self, _busy_key, scan=True):
                    sequence = next(_SCAN_SEQUENCE)
                    self._scan_sequences[scope] = sequence
                    token = _SCAN_BUDGET.set(budget)
                    try:
                        imgs = await self._image_data_urls(event)
                        if not imgs:
                            raise _ImageRejected(L(*_IMAGE_DOWNLOAD_ERROR))
                        ocr = await self._extract_loadout_validated(imgs[0])
                    finally:
                        _SCAN_BUDGET.reset(token)
            if ocr:
                return await self._finish_scan(ocr, budget, scope, sequence, event, platform)
        except _ImageRejected as exc:
            self._vision_refund(_last, _sender, _stamp)
            return Reply(raw_text=str(exc))
        except asyncio.TimeoutError:
            self._vision_refund(_last, _sender, _stamp)
            return Reply(raw_text=L("识卡超时，请稍后重试", "Build scan timed out. Please retry."))
        except QueueUnavailable as exc:
            self._vision_refund(_last, _sender, _stamp)
            return Reply(raw_text=f"⏳ {L('配卡识别', 'Build scan')} · {exc}")
        except asyncio.CancelledError:
            self._vision_refund(_last, _sender, _stamp)
            raise
        finally:
            # 只保存仍可更新指针的请求。新请求收尾后，旧请求也不能重新取得写权。
            if sequence is not None and self._scan_sequences.get(scope) == sequence:
                del self._scan_sequences[scope]
        self._vision_refund(_last, _sender, _stamp)
        return err_card(
            L(
                "截图识别失败：视觉渠道没给出可解析结果",
                "Scan failed: the vision channel returned nothing parseable",
            ),
            L(
                "先重发一次；仍失败请联系机器人管理员查看日志里的「配卡识别」条目（会写明是哪个渠道、什么原因）",
                'Resend once; if it still fails, ask the bot admin to check the "配卡识别" entries in the log '
                "(they name the channel and the reason)",
            ),
            (
                L("可能原因", "Possible causes"),
                L(
                    "① 渠道超时/返回空；② 未配视觉模型",
                    "(1) channel timed out / returned empty; (2) no vision model configured",
                ),
            ),
            title=L("识卡", "Build Scan"),
        )

    async def _finish_scan(self, ocr, budget, scope, sequence, event, platform):
        simulation = de.full_enabled(self.cfg)
        try:
            options = {"calculate_reference": False} if simulation else {}
            an = await budget.run(_scan_cpu, lo.analyze, ocr, ocr.get("_pips_rows"), **options)
        except asyncio.TimeoutError:
            if self._scan_sequences.get(scope) == sequence:
                latest, _ = _scan_state(self, time.time())
                latest.pop(scope, None)
            return self._partial_scan_reply(ocr)
        cached_spec = None
        if an.get("weapon"):
            try:
                cached_spec = (
                    lo.to_damage_spec(an, simulation=True) if simulation else lo.to_damage_spec(an)
                )
            except Exception:  # noqa: BLE001 —— 缓存失败不影响识卡本身
                cached_spec = None
        token = ""
        # 新截图不可靠时只撤销本次会话/用户的最近指针；历史按钮仍指向原卡。
        if cached_spec is None and self._scan_sequences.get(scope) == sequence:
            latest, _records = _scan_state(self, time.time())
            latest.pop(scope, None)
        if cached_spec is not None and all(part.strip() for part in scope):
            now = time.time()
            latest, records = _scan_state(self, now)
            snapshot = (now, copy.deepcopy(an["weapon"]), copy.deepcopy(cached_spec))
            if self._scan_sequences.get(scope) == sequence:
                latest[scope] = snapshot
            # 记录号会写进卡面指令：只用字母数字（不含 _ / -），QQ Markdown 不会因此整卡退回纯文字
            token = secrets.token_urlsafe(9)
            while token in records or not token.isalnum():
                token = secrets.token_urlsafe(9)
            records[token] = (now, *scope, snapshot[1], snapshot[2])
            _scan_state(self, now)

        comparison = None
        if cached_spec is not None:
            try:
                comparison = await budget.run(
                    asyncio.to_thread, lo.mod_dps_comparison, an, cached_spec
                )
            except asyncio.TimeoutError:
                pass
            except Exception:  # noqa: BLE001 —— 局部复算失败时照常回图
                logger.exception("[sdjk] 识卡单卡 DPS 对照生成失败")
        lines = lo.card_lines(
            an,
            simulation=simulation,
            simulation_ready=cached_spec is not None and simulation,
            comparison=comparison,
        )
        # 认不全时回显白名单字段，供玩家核对视觉通道的读取结果。
        if not an.get("ok"):
            lines.append(L("　原始识别结果：", "　Raw scan result: ") + self._ocr_echo(ocr))

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
                command = L(f"识卡伤害 #{token}", f"scandamage #{token}")
                _vs = L(" 对 ", " vs ")
                if can_actions:
                    # 有按钮时只留一行指令（旧客户端可能不显示按钮），复算示例交给按钮
                    lines.append(
                        L(
                            f"※ 伤害详情：点下方按钮，或发送「{command}」；30 分钟内仅本人可用",
                            f'※ Damage details: tap the button below or send "{command}"; '
                            "usable by you only, within 30 minutes",
                        )
                    )
                    actions = [
                        Action(L("伤害详情", "Damage details"), command, owner_only=True),
                        Action(
                            L("换目标复算", "Recalculate vs another target"),
                            command + _vs,
                            mode="fill",
                            owner_only=True,
                        ),
                    ]
                else:
                    lines.append(
                        L(
                            f"※ 伤害详情：发送「{command}」；30 分钟内仅本人在当前会话可用",
                            f'※ Damage details: send "{command}"; usable by you only, '
                            "in this chat, within 30 minutes",
                        )
                    )
                    lines.append(
                        L(
                            f"· 换目标复算：{command} 对 重机枪手 150级 爆头",
                            f"· Recalculate vs another target: {command} vs Heavy Gunner 150 headshot",
                        )
                    )
            else:
                lines.append(
                    L(
                        "※ 未取得会话或用户标识，无法保存复算记录；可用「伤害 武器名」手动配卡计算",
                        "※ No chat or user id available, so the recalculation record cannot be saved; "
                        'use "damage <weapon>" to calculate a build by hand',
                    )
                )
            return Reply(L("配卡识别", "Build Scan"), lines, actions=actions)
        detail = None
        if cached_spec is not None:
            try:
                if simulation:
                    detail, _, _ = await budget.run(
                        _scanned_damage_result, self, an["weapon"], cached_spec, {}, event, platform
                    )
                else:
                    detail = await budget.run(_scan_cpu, self._loadout_detail_lines, an)
            except asyncio.TimeoutError:
                detail = None
        if detail:
            return Reply(
                pages=[
                    (L("配卡识别", "Build Scan"), lines),
                    (L("配卡识别 · 伤害详情", "Build Scan · Damage Details"), detail),
                ]
            )
        return Reply(L("配卡识别", "Build Scan"), lines)

    def _partial_scan_reply(self, ocr):
        lines = [
            L("识卡时间已到，已读到以下字段。", "Scan timed out. Fields read so far:"),
            self._ocr_echo(ocr),
        ]
        panel = ocr.get("panel") or {}
        missing = []
        for key, title in (
            ("weapon", L("武器", "Weapon")),
            ("capacity", L("容量", "Capacity")),
            ("mods", "MOD"),
            ("panel", L("面板", "Panel")),
        ):
            if not ocr.get(key):
                missing.append(title)
        if isinstance(panel, dict):
            for key, label in (
                ("crit_chance", L("暴击几率", "Critical chance")),
                ("crit_damage", L("暴击倍率", "Critical multiplier")),
                ("status_chance", L("触发几率", "Status chance")),
                ("total", L("总伤害", "Total damage")),
            ):
                if (value := lo._panel_columns(panel.get(key)).get("right")) is not None:
                    lines.append(f"· {label}　{value:g}")
        slots = ocr.get("_upgrade_slots") or []
        if not slots:
            missing.append(L("逐槽装备与等级", "Slot equipment and ranks"))
        if not any(slot["kind"] == "arcane" and slot["state"] != "unknown" for slot in slots):
            missing.append(L("赋能槽", "Arcane slot"))
        for slot in slots:
            if slot["state"] == "unknown":
                missing.append(slot["id"])
            elif slot["state"] == "installed":
                name = slot.get("name") or slot.get("mod_name") or "?"
                rank = slot.get("rank")
                lines.append(f"· {slot['id']}　{name}　{rank if rank is not None else '?'}")
                if rank is None:
                    missing.append(slot["id"] + L(" 等级", " rank"))
        if missing:
            lines.append(L("待确认　", "Unconfirmed  ") + L("、", ", ").join(missing))
        return Reply(L("配卡识别", "Build Scan"), lines)

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
        parts.append(L(f"武器「{weapon or '未读出'}」", f'Weapon "{weapon or "not read"}"'))
        cap = str(ocr.get("capacity") or "")
        if re.fullmatch(r"\s*\d{1,3}\s*/\s*\d{1,3}\s*", cap):
            parts.append(L(f"容量 {cap.strip()}", f"Capacity {cap.strip()}"))
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
            if is_en():
                color_s = {"绿": "G", "红": "R", "白": "W"}.get(color_s, color_s)
            shown.append(f"{name}({drain_s}{color_s})")
        parts.append(
            L(
                f"MOD {len(mods)} 张：" + ("、".join(shown) or "无"),
                f"MODs ({len(mods)}): " + (", ".join(shown) or "none"),
            )
        )
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
            res = lo.calculate_bundled_scan(spec, weapon)
            if not res.get("ok"):
                return None
            out = dc.card_lines(weapon, spec, res, [])
            out.insert(0, L("· 计算：内置估算", "· Engine: Built-in estimate"))
            out.append(
                L(
                    "※ 要换目标，可发「识卡伤害 对 重机枪手 150级 爆头」。",
                    '※ To change the target, try "scandamage vs Heavy Gunner 150 headshot".',
                )
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
        command = L("识卡伤害", "scandamage")
        title = L("识卡伤害", "Build Scan Damage")
        scope = _scan_scope(self, event)
        latest, records = _scan_state(self, time.time())
        rest = [str(t).strip() for t in (parsed.content or []) if str(t).strip()]
        if is_en():  # 英文伤害参数并入中文写法；「#记录」原样保留
            _ref = rest[:1] if rest[:1] and rest[0].startswith("#") else []
            rest = _ref + _dmg_en_toks(rest[len(_ref) :])
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
                reason = L(
                    "无法确认当前会话或发送者，不能读取识卡记录",
                    "Cannot confirm the chat or sender, so the scan record cannot be read",
                )
            elif token is not None:
                reason = L(
                    "该识卡记录不存在、已过期，或不属于当前会话与发送者",
                    "That scan record does not exist, has expired, or does not belong to this chat and sender",
                )
            else:
                reason = L(
                    "当前会话中，你在 30 分钟内没有识卡记录",
                    "You have no scan record in this chat within the last 30 minutes",
                )
            return Reply(
                L("识卡伤害", "Build Scan Damage"),
                [
                    L(f"◆ 原因：{reason}", f"◆ Reason: {reason}"),
                    L(
                        "◆ 用法：先发「识卡 + 武器升级界面截图」，再用本指令复算",
                        '◆ Usage: first send "scan" + a weapon Upgrade screenshot, then use this command to recalculate',
                    ),
                    L(*_SCAN_EXPERIMENTAL),
                    *L(dc.TARGET_HELP_ZH, dc.TARGET_HELP_EN),
                    L(
                        "· 示例　识卡伤害 对 重机枪手 150级 爆头",
                        "· Example　scandamage vs Heavy Gunner 150 headshot",
                    ),
                    L(
                        "· 示例　识卡伤害 C系 120级｜识卡伤害 满镀层 异常4 连击120 重击",
                        "· Example　scandamage corpus 120 | scandamage combo120",
                    ),
                    L(
                        "※ ⚠ 怎么改：先发一次「识卡」；参数写法与「伤害」指令一致（识卡伤害 = 伤害 + 已识别配卡）",
                        '※ ⚠ How to fix: send "scan" first; arguments are written like the "damage" command '
                        "(scandamage = damage + the scanned build)",
                    ),
                ],
            )
        # 深拷贝后再交给计算/格式化；按钮快照和本人最近记录均不接受下游原地修改。
        _ts, weapon, cached = copy.deepcopy(hit)
        # 传 token 列表，不能传会被逐字符拆开的字符串。
        try:
            overrides = {}
            if rest:
                target = {}
                arg_spec, _tokens = dc.parse_args(rest, target_overrides=target)
                default_spec, _ = dc.parse_args([])
                overrides = {k: v for k, v in arg_spec.items() if v != default_spec.get(k)}
                dc.merge_target_options(overrides, target)
                spec = dict(cached)
                dc.merge_target_options(spec, overrides)
            else:
                spec = dict(cached)
            lines, ok, footer = await _scanned_damage_result(
                self, weapon, spec, overrides, event, platform
            )
            reply = Reply(title, lines, footer=footer)
            try:
                can_actions = bool(getattr(self, "_can_send_actions", lambda _e: False)(event))
            except Exception:  # noqa: BLE001 —— 平台能力不可确认时不加按钮，正文照常
                can_actions = False
            if can_actions and ok:
                # 换目标沿用同一识卡记录，由会话与发送者校验兜底。
                original = f"{command} #{token}" if token else command
                if "loadout_slots" in spec and (
                    de.portable_port(self.cfg) or spec.get("builtin_scan_context")
                ):
                    modes = {
                        "base": ("基础形态", "base form"),
                        "alt_fire": ("副开火", "alt fire"),
                        "incarnon": ("灵化", "incarnon"),
                    }
                    if mode := modes.get(overrides.get("form")):
                        original += " " + L(*mode)
                    for code in overrides.get("evo_keys") or ():
                        original += L(f" 进化 {code}", f" evolve {code}")
                reply.actions.append(
                    Action(
                        L("换目标复算", "Recalculate vs another target"),
                        original + L(" 对 ", " vs "),
                        mode="fill",
                        owner_only=True,
                    )
                )
                if overrides.keys() - {"form", "form_word", "evo_keys"}:
                    reply.actions.append(
                        Action(L("默认目标", "Default target"), original, owner_only=True)
                    )
            return reply
        except Exception as exc:  # noqa: BLE001 —— 绝不静默
            logger.exception("[sdjk] 识卡伤害复算失败：%r", exc)
            return Reply(
                L("识卡伤害", "Build Scan Damage"),
                [
                    L(
                        "◆ 原因：复算出错（内部错误，详情已写入日志）",
                        "◆ Reason: recalculation error (internal error, details are in the log)",
                    ),
                    L(
                        "※ ⚠ 怎么改：重新发一次「识卡」后再试；参数写法见「伤害」指令的用法页",
                        '※ ⚠ How to fix: send "scan" again and retry; for the argument syntax see the "damage" usage page',
                    ),
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
            size = _scan_fit_size(w, h, min_width, max_width)
            if size is None or size == img.size:
                return image_url
            # 标签按**实际**缩放方向写：1599 宽这类「略低于 min_width」的图
            # 会被规整到 1280，其实是缩小而不是放大
            nw, nh = size
            action = "放大" if nw > w else "缩小"
            img2 = img.resize((nw, nh), PILImage.LANCZOS)
            fmt = "JPEG" if "jpeg" in head.lower() or "jpg" in head.lower() else "PNG"
            buf = io.BytesIO()
            img2.convert("RGB" if fmt == "JPEG" else img2.mode).save(buf, fmt)
            new_b64 = base64.b64encode(buf.getvalue()).decode()
            logger.info("[sdjk] 配卡截图 %dx%d → %dx%d（已%s）", w, h, nw, nh, action)
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
                if _scan_fit_size(*image.size) is None:
                    return {}
                observed = pips_engine.detect_layout(image)
            observed["image_sha256"] = hashlib.sha256(raw).hexdigest()
            return observed

        if not str(image_url).startswith("data:"):
            return {}
        try:
            return await _vision_thread(observe)
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
        budget = _SCAN_BUDGET.get() or _ScanBudget(self.RACE_WINDOW_S)
        token = _SCAN_BUDGET.set(budget)
        try:
            result = await budget.run(self._scan_loadout, image_url, budget)
            if not result and budget.now() >= budget.deadline:
                raise asyncio.TimeoutError
            return result
        except asyncio.CancelledError:
            budget.deadline = budget.now()
            raise
        except asyncio.TimeoutError:
            if budget.partial is not None:
                return {**budget.partial, "_scan_timed_out": True}
            raise
        finally:
            _SCAN_BUDGET.reset(token)

    async def _scan_loadout(self, image_url, budget):
        original_image_url = image_url
        # ★ 豆子检测要在**缩放前**的原图上做（_detect_pips 的说明）
        pips_rows = await self._detect_pips(image_url)
        pixel_layout = await self._detect_scan_layout(image_url)
        # 缩放 + 重编码是纯 CPU 活（Lanczos），放线程里跑，别卡住事件循环
        image_url = await _vision_thread(self._fit_scan_image, image_url)
        best: Optional[tuple[int, dict, dict]] = None  # 失败项数、观察和对应分析

        def _finish(o: Optional[dict]) -> Optional[dict]:
            """把豆子结果挂到返回的 ocr 上，供上层复用（免得再检测一次）。"""
            if o is not None:
                o["_pips_rows"] = pips_rows
                o["_pixel_layout"] = pixel_layout
            return o

        pixel_evidence = {}

        def evaluate(ocr: dict):
            """面板校验打分：返回 (失败项数, analyze 结果)，恒为数值。

            ★ 2026-09-23：武器没认出不再「立即接受」——此前第一个返回
            「有 weapon 字段但其实是幻觉」的渠道会直接获胜并取消其它渠道
            （9-20 漏读事故同类风险）。现在按重罚 +6 计入竞速，窗口耗尽后
            才轮到它兜底。「0 张卡」「面板全没读到」「疑似漏读」惩罚不变。
            """
            ocr["_pixel_layout"] = pixel_layout
            lo.observe_capacity_colors(ocr, original_image_url, evidence_cache=pixel_evidence)
            lo.observe_arcane_ranks(ocr, original_image_url, evidence_cache=pixel_evidence)
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

        async def _score(ocr):
            bad, an = await _scan_cpu(evaluate, ocr)
            st = an.get("pips") or {}
            if pips_rows and st and not st.get("used") and self.cfg.get("scan_debug_save", False):
                await _vision_thread(self._dump_scan_debug, image_url)
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
        deadline = budget.deadline
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
                    if budget.partial is None:
                        budget.remember(_finish(ocr))
                    bad, an = await _score(ocr)
                    if bad == 0:
                        logger.info(
                            "[sdjk] 配卡识别可比数值吻合，继续核对装备区（不代表识别准确性）"
                        )
                        best = (bad, ocr, an)
                        budget.remember(_finish(ocr))
                        pending.clear()
                        break
                    logger.warning("[sdjk] 配卡识别一份结果有 %d 项校验不过", bad)
                    if best is None or bad < best[0]:
                        best = (bad, ocr, an)
                        budget.remember(_finish(ocr))
        finally:
            await _cancel_vision_tasks(tasks)
        # 完整补读也可接住主读无有效结构的情况；两路各保留独立证据。
        focused = await self._read_scan_focus(image_url, "mods")
        if best is None:
            if not focused or not focused.get("mods") or not focused.get("weapon"):
                focused = await self._read_scan_recovery(
                    original_image_url, pips_rows, pixel_layout
                )
            if not focused:
                return None
            recovered = lo.normalize_scan_structure(focused)
            recovered["_equipment_unconfirmed"] = True
            budget.remember(_finish(recovered))
            if not recovered.get("arcanes"):
                arcanes = await self._read_scan_focus(image_url, "arcanes")
                if arcanes and arcanes.get("arcanes"):
                    recovered = _scan_revision(
                        {**recovered, "arcanes": arcanes["arcanes"]},
                        recovered,
                        arcanes,
                        ["arcanes"],
                        "scan_arcanes",
                    )
            budget.remember(_finish(recovered))
            await _score(recovered)
            recovered = await self._read_equipment_tiles(
                recovered, original_image_url, pips_rows, pixel_layout
            )
            if not recovered.get("_upgrade_slots"):
                await _score(recovered)
            return _finish(recovered)
        bad, ocr, current_analysis = best
        main_read = ocr
        equipment_read = focused
        # 数值吻合仍须核对装备区：亮豆下界覆盖不到零级卡和部分特殊槽。
        confirmed = False
        if focused and focused.get("mods"):
            old_analysis = current_analysis
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
            score, analysis = await _score(candidate)
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
                if candidate.get("weapon") != ocr.get("weapon"):
                    fields.append("weapon")
                bad, ocr = score, _scan_revision(candidate, ocr, focused, fields, "scan_mods")
                current_analysis = analysis
                confirmed = True
                budget.remember(_finish(ocr), equipment_confirmed=True)
        # —— 武器名单读：主读没认出武器时，用一句话只问标题栏，换上后同一把尺重新打分 ——
        # ★ 2026-10-27（ZCode 消融）：qwen-vl-ocr 在长 JSON 提示词下 3/3 把首张姿态卡名当武器名，
        #   单问「左上角标题栏斜杠后面的文字」2/2 读对 ⇒ 问题是提示词过载，拆成独立最小调用。
        if not current_analysis.get("weapon"):
            title = await self._read_weapon_title(image_url)
            if title:
                ocr_w = {**ocr, "weapon": title}
                bad_w, title_analysis = await _score(ocr_w)
                if bad_w < bad:
                    logger.warning(
                        "[sdjk] 武器名单读修正：%r → %r，校验失败 %d → %d",
                        ocr.get("weapon"),
                        title,
                        bad,
                        bad_w,
                    )
                    current_analysis = title_analysis
                    bad, ocr = (
                        bad_w,
                        _scan_revision(ocr_w, ocr, {"weapon": title}, ["weapon"], "scan_title"),
                    )
        budget.remember(_finish(ocr), equipment_confirmed=confirmed)
        if not confirmed:
            ocr = {**ocr, "_equipment_unconfirmed": True}
            bad, current_analysis = await _score(ocr)
        if bad:
            focused = await self._read_scan_focus(image_url, "panel")
            if focused and focused.get("panel"):
                old = current_analysis
                panel = focused["panel"]
                if old["quality"]["panel_safe"]:
                    panel = {**ocr.get("panel", {}), **panel}
                    total = lo._panel_total(old.get("panel", {}))
                    if total is not None and "total" not in focused["panel"]:
                        panel["total"] = total
                candidate = {**ocr, "panel": panel}
                score, analysis = await _score(candidate)
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
        budget.remember(_finish(ocr), equipment_confirmed=confirmed)
        if not ocr.get("arcanes"):
            focused = await self._read_scan_focus(image_url, "arcanes")
            if focused and focused.get("arcanes"):
                main_read = {**main_read, "arcanes": focused["arcanes"]}
                ocr = _scan_revision(
                    {**ocr, "arcanes": focused["arcanes"]},
                    ocr,
                    focused,
                    ["arcanes"],
                    "scan_arcanes",
                )
        budget.remember(_finish(ocr), equipment_confirmed=confirmed)
        ocr = await self._read_equipment_tiles(
            ocr,
            original_image_url,
            pips_rows,
            pixel_layout,
            main_read=main_read,
            equipment_read=equipment_read,
        )
        # 完整槽位的最终分析交给消息入口，避免重复计算参考伤害。
        if not ocr.get("_upgrade_slots"):
            await _score(ocr)
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
            with PILImage.open(io.BytesIO(raw)) as source:
                if not _scan_size_ok(*source.size):
                    return (image_url, None) if with_geometry else image_url
                image = source.convert("RGB")
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
            cw, ch = bounds[2] - bounds[0], bounds[3] - bounds[1]
            if not _scan_size_ok(cw, ch):
                return (image_url, None) if with_geometry else image_url
            image = image.crop(bounds)
            scale = min(1280 / cw, 4.0)
            size = round(cw * scale), round(ch * scale)
            if scale > 1 and _scan_size_ok(*size):
                image = image.resize(size, PILImage.Resampling.LANCZOS)
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
        image, transform = await _vision_thread(
            _scan_task_image, image_url, part, self._scan_focus_image
        )
        providers = (
            self._scan_equipment_providers() if part == "mods" else self._scan_vision_providers()
        )
        for prov in providers[:1]:
            try:
                actual_prompt = effective_vision_prompt(self, prov, f"scan_{part}", prompt)
                response = await asyncio.wait_for(
                    _vision_chat(
                        prov,
                        prompt=actual_prompt,
                        session_id=f"sdjk-focus-{uuid.uuid4().hex[:8]}",
                        image_urls=[image],
                    ),
                    timeout=75,
                )
                text = str(getattr(response, "completion_text", "") or "")
                parsed = await _scan_cpu(lo.parse_focus_text, text, part)
                if parsed:
                    recognition = _scan_recognition(
                        text, parsed, actual_prompt, _prov_id(prov), f"scan_{part}"
                    )
                    for key in _SCAN_IMAGE_FIELDS:
                        parsed.pop(key, None)
                    parsed["_recognition"] = recognition
                    if part in {"mods", "arcanes"}:
                        parsed = lo.normalize_scan_structure(parsed)
                        parsed = lo.remap_mod_geometry(parsed, transform)
                        parsed["_recognition"]["geometry_transform"] = copy.deepcopy(transform)
                    return parsed
            except Exception as exc:  # noqa: BLE001 —— 不泄露原始响应，失败保留主读结果
                logger.warning("[sdjk] 配卡分区补读失败：%s", type(exc).__name__)
        return None

    async def _read_scan_recovery(self, image_url: str, rows: list, layout: dict) -> Optional[dict]:
        """整图主读和装备补读均缺失时，从原图裁掉下方仓库再读标题和装备。"""
        import base64
        import io
        import uuid
        from PIL import Image

        if not rows:
            return None

        def crop():
            with Image.open(io.BytesIO(base64.b64decode(image_url.split(",", 1)[1]))) as original:
                if _scan_fit_size(*original.size) is None:
                    return None, None
                slots = pips_engine.upgrade_slots(original, rows, layout, melee=False)
                if not slots:
                    return None, None
                edge = min(1.0, max(slot["box"][3] for slot in slots) + 0.025)
                bottom = round(edge * original.height)
                if bottom <= 0:
                    return None, None
                bounds = (0, 0, original.width, bottom)
                if _scan_size_ok(original.width, bottom):
                    image = original.crop(bounds)
                else:
                    scale = (_MAX_DERIVED_PIXELS / (original.width * bottom)) ** 0.5
                    size = max(1, int(original.width * scale)), max(1, int(bottom * scale))
                    image = original.resize(size, Image.Resampling.LANCZOS, box=bounds)
                buffer = io.BytesIO()
                image.save(buffer, format="PNG")
                encoded = base64.b64encode(buffer.getvalue()).decode()
                return "data:image/png;base64," + encoded, {
                    "bounds": [0, 0, 1, bottom / original.height]
                }

        try:
            image, transform = await _vision_thread(crop)
            if not image:
                return None
            for provider in self._scan_equipment_providers()[:1]:
                prompt = lo.VISION_PROMPT
                response = await asyncio.wait_for(
                    _vision_chat(
                        provider,
                        prompt=prompt,
                        image_urls=[image],
                        session_id=f"sdjk-recovery-{uuid.uuid4().hex[:8]}",
                    ),
                    timeout=60,
                )
                text = str(getattr(response, "completion_text", "") or "")
                parsed = lo.parse_coordinate_ocr(text)
                if parsed and parsed.get("weapon") and parsed.get("mods"):
                    result = lo.remap_mod_geometry(parsed, transform)
                    result["_recognition"] = _scan_recognition(
                        text, parsed, prompt, _prov_id(provider), "scan_recovery"
                    )
                    result["_recognition"]["geometry_transform"] = transform
                    return result
        except Exception as exc:  # noqa: BLE001
            logger.warning("[sdjk] 配卡装备区坐标补读失败：%s", type(exc).__name__)
        return None

    _TILES_PROMPT = (
        "这是一张 Warframe 装备槽位拼图。每格上方的 T 标签只用于定位，文字在标签下方的卡片内。"
        "逐格读取，只输出 JSON 对象，包含 slots 数组。每项含 tile（照抄标签）、state、name、drain 和 mark。\n"
        "看见已装卡片或武器赋能，state 填 installed，name 抄完整名称（保留 Prime 及赋能前缀），"
        "drain 只抄同格卡片右上角的容量数字，填字符串。赋能没有容量，填 null。"
        "已装时 mark 填 null。\n"
        "只有明确看见空框才填 state=empty、mark=empty_frame；"
        "只有明确看见锁才填 state=locked、mark=padlock。两种情况 name 和 drain 均为 null。"
        "格子被裁切、看不清或无法确认时填 state=unknown，未知字段填 null。\n"
        "每个标签最多一项。保持同格内容，勿使用相邻卡的文字，不输出等级、极性或推测数值。"
    )

    @staticmethod
    def _equipment_tile_image(image_url, rows, layout, *, melee=None, badges=False, slot_ids=None):
        import base64
        import io

        from PIL import Image

        with Image.open(io.BytesIO(base64.b64decode(image_url.split(",", 1)[1]))) as image:
            sheet, tiles = pips_engine.equipment_tiles(
                image, rows, layout, melee=melee, badges=badges, slot_ids=slot_ids
            )
        if sheet is None:
            return None, []
        stream = io.BytesIO()
        sheet.save(stream, format="PNG")
        return "data:image/png;base64," + base64.b64encode(stream.getvalue()).decode(), tiles

    async def _read_equipment_tiles(
        self, ocr, image_url, rows, layout, *, main_read=None, equipment_read=None
    ):
        import uuid

        analysis = await _scan_cpu(lo.analyze, ocr, rows)
        weapon = analysis.get("weapon") or {}
        category = weapon.get("category") if isinstance(weapon, dict) else None
        melee = category == "Melee" if category in {"Melee", "Primary", "Secondary"} else None
        if (
            melee is None
            and analysis.get("mods")
            and all(m.get("rank") is not None and m.get("found") for m in analysis["mods"])
        ):
            return ocr
        try:
            image, tiles = await _vision_thread(
                self._equipment_tile_image, image_url, rows, layout, melee=melee
            )
            if not image:
                return ocr
            for provider in self._scan_equipment_providers()[:1]:
                prompt = effective_vision_prompt(self, provider, "scan_tiles", self._TILES_PROMPT)
                response = await asyncio.wait_for(
                    _vision_chat(
                        provider,
                        prompt=prompt,
                        image_urls=[image],
                        session_id=f"sdjk-tiles-{uuid.uuid4().hex[:8]}",
                    ),
                    timeout=60,
                )
                text = str(getattr(response, "completion_text", "") or "")
                boxes = lo.coordinate_ocr_boxes(text)
                if boxes:
                    observed = {
                        "_recognition": _scan_recognition(
                            text, {"boxes": boxes}, prompt, _prov_id(provider), "scan_tiles"
                        )
                    }
                    if melee is None:
                        candidate = lo.merge_coordinate_tile_read(ocr, boxes, tiles)
                        return _scan_revision(candidate, ocr, observed, ["mods"], "scan_tiles")
                    main = main_read or ocr
                    slots = await _vision_thread(
                        lo.verify_coordinate_slots, main, equipment_read, tiles, boxes, image_url
                    )
                    candidate = _scan_revision(
                        {**ocr, "_upgrade_slots": slots},
                        ocr,
                        observed,
                        ["_upgrade_slots"],
                        "scan_tiles",
                    )
                    if budget := _SCAN_BUDGET.get():
                        budget.remember(candidate, equipment_confirmed=True)
                    if any(
                        s["kind"] != "arcane" and s["reason"] == "capacity_unconfirmed"
                        for s in slots
                    ):
                        candidate = await self._read_slot_capacities(
                            candidate,
                            provider,
                            image_url,
                            rows,
                            layout,
                            melee,
                            main,
                            equipment_read,
                            tiles,
                            boxes,
                        )
                    if budget := _SCAN_BUDGET.get():
                        budget.remember(candidate, equipment_confirmed=True)
                    return await self._read_unconfirmed_slots(
                        candidate, provider, image_url, rows, layout, melee, main, equipment_read
                    )
                observed = lo.parse_vision_json(text)
                if not observed:
                    return ocr
                observed["_recognition"] = _scan_recognition(
                    text, observed, prompt, _prov_id(provider), "scan_tiles"
                )
                if melee is not None and "slots" in observed:
                    main = main_read or ocr
                    slots = await _vision_thread(
                        lo.verify_upgrade_slots,
                        main,
                        observed,
                        tiles,
                        equipment_read,
                        image_url=image_url,
                    )
                    if not slots:
                        return ocr
                    candidate = _scan_revision(
                        {**ocr, "_upgrade_slots": slots},
                        ocr,
                        observed,
                        ["_upgrade_slots"],
                        "scan_tiles",
                    )
                    if budget := _SCAN_BUDGET.get():
                        budget.remember(candidate, equipment_confirmed=True)
                    return await self._read_unconfirmed_slots(
                        candidate, provider, image_url, rows, layout, melee, main, equipment_read
                    )
                if melee is None and isinstance(observed.get("slots"), list):
                    # 未确认武器类别时仍可用裁图补已装卡名，保留候选布局。
                    observed["mods"] = [
                        row
                        for row in observed["slots"]
                        if isinstance(row, dict)
                        and isinstance(row.get("tile"), str)
                        and row.get("state") == "installed"
                    ]
                candidate = lo.merge_tile_read(ocr, observed, tiles)
                return _scan_revision(candidate, ocr, observed, ["mods"], "scan_tiles")
        except Exception as exc:  # noqa: BLE001 — 复读失败保留原始结果
            logger.warning("[sdjk] 配卡槽位复读失败：%s", type(exc).__name__)
        return ocr

    async def _read_unconfirmed_slots(
        self, ocr, provider, image_url, rows, layout, melee, main, equipment
    ):
        import uuid

        slots = ocr["_upgrade_slots"]
        slot_ids = {
            slot["id"]
            for slot in slots
            if slot["reason"] in {"slot_unread", "mod_name_unconfirmed", "arcane_name_unconfirmed"}
            and slot.get("observed_occupied")
        }
        if not slot_ids:
            return ocr
        try:
            image, tiles = await _vision_thread(
                self._equipment_tile_image, image_url, rows, layout, melee=melee, slot_ids=slot_ids
            )
            if not image:
                return ocr
            prompt = lo.VISION_PROMPT
            response = await asyncio.wait_for(
                _vision_chat(
                    provider,
                    prompt=prompt,
                    image_urls=[image],
                    session_id=f"sdjk-slot-focus-{uuid.uuid4().hex[:8]}",
                ),
                timeout=60,
            )
            text = str(getattr(response, "completion_text", "") or "")
            boxes = lo.coordinate_ocr_boxes(text)
            if not boxes:
                return ocr
            checked = await _vision_thread(
                lo.verify_coordinate_slots, main, equipment, tiles, boxes, image_url
            )
            recovered = {
                slot["id"]: slot
                for slot in checked
                if slot["state"] == "installed" and slot["rank"] is not None
            }
            # 已有占位证据的槽不能通过补读改判为空位。并回全表后仍需检查重名和容量。
            combined = [dict(recovered.get(slot["id"], slot)) for slot in slots]
            lo.reject_duplicate_slots(combined)
            observed = {
                "_recognition": _scan_recognition(
                    text,
                    {"boxes": boxes, "slot_ids": sorted(slot_ids)},
                    prompt,
                    _prov_id(provider),
                    "scan_slot_focus",
                )
            }
            return _scan_revision(
                {**ocr, "_upgrade_slots": combined},
                ocr,
                observed,
                ["_upgrade_slots"],
                "scan_slot_focus",
            )
        except Exception as exc:  # noqa: BLE001 — 补读失败保留逐槽证据
            logger.warning("[sdjk] 配卡未确认槽位补读失败：%s", type(exc).__name__)
            return ocr

    async def _read_slot_capacities(
        self, ocr, provider, image_url, rows, layout, melee, main, equipment, tiles, boxes
    ):
        import uuid

        try:
            image, crops = await _vision_thread(
                self._equipment_tile_image, image_url, rows, layout, melee=melee, badges=True
            )
            prompt = lo.VISION_PROMPT
            response = await asyncio.wait_for(
                _vision_chat(
                    provider,
                    prompt=prompt,
                    image_urls=[image],
                    session_id=f"sdjk-capacity-{uuid.uuid4().hex[:8]}",
                ),
                timeout=60,
            )
            text = str(getattr(response, "completion_text", "") or "")
            capacity_boxes = lo.coordinate_ocr_boxes(text)
            if capacity_boxes:
                slots = await _vision_thread(
                    lo.verify_coordinate_slots,
                    main,
                    equipment,
                    tiles,
                    boxes,
                    image_url,
                    capacity_boxes=capacity_boxes,
                    capacity_tiles=crops,
                )
                observed = {
                    "_recognition": _scan_recognition(
                        text, {"boxes": capacity_boxes}, prompt, _prov_id(provider), "scan_capacity"
                    )
                }
                return _scan_revision(
                    {**ocr, "_upgrade_slots": slots},
                    ocr,
                    observed,
                    ["_upgrade_slots"],
                    "scan_capacity",
                )
        except Exception as exc:  # noqa: BLE001 — 数字补读失败时保留已核对的槽位
            logger.warning("[sdjk] 配卡容量复读失败：%s", type(exc).__name__)
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
                    _vision_chat(
                        prov,
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
                    _vision_chat(
                        prov,
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
                resp = await _vision_chat(
                    p,
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
            ocr = await _scan_cpu(_parse_scan_main, text) if text else None
            if ocr and (ocr.get("weapon") or ocr.get("mods")):
                recognition = _scan_recognition(raw_text, ocr, prompt, pid, "scan_main")
                # 槽位与许可由程序创建。模型响应同名字段只留在原始观察。
                for key in _SCAN_IMAGE_FIELDS:
                    ocr.pop(key, None)
                ocr["_recognition"] = recognition
                ocr = lo.normalize_scan_structure(ocr)
                logger.info("[sdjk] 配卡识别 %s 返回结构（%.0f ms）", pid, ms)
                return ocr
            logger.warning("[sdjk] 配卡识别 %s 未读出有效内容（%.0f ms）", pid, ms)
            return None

        # targets 恒为单渠道；直接等待，让上一层竞速独占任务的取消与回收。
        return await _one(targets[0])
