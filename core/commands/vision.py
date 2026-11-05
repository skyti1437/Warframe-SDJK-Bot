# -*- coding: utf-8 -*-
"""紫卡/配装识别链设施（D9 自 main.py 迁入，无路由键）。

成员：图片检测（_event_has_image）、vision 渠道竞速（_vision_race_json）、
紫卡行解析（_riven_lines_legal/_parse_riven_lines_text/_extract_riven_from_image）、
豆子检测（_detect_pips，MUST_BE_STATIC）、排查留档（_dump_scan_debug）、
词条归一化（_STAT_ALIAS/_full_stat_alias/_stat_id_from_name/_normalize_llm_stats）。
⚠ 披露式改写 3 处（相对导入基准 .core→.. ×2、类级调用基准
WarframeSDJK→VisionCommands ×1），导入目标/被调函数不变。
子包纪律：不 import astrbot（事件对象鸭子类型）。
"""

from __future__ import annotations

import asyncio
import copy
import math
import re
import time
import unicodedata
from hashlib import sha256
from decimal import Decimal, InvalidOperation, localcontext
from typing import Optional

from .. import loadout_ocr as lo
from .. import paths as core_paths
from .. import pips as pips_engine
from ..logging_compat import logger
from .base import Reply, _num


async def _cancel_vision_tasks(tasks: list) -> None:
    """回收本层拥有的渠道任务；调用方取消不能中断渠道的异步清理。"""
    for task in tasks:
        cancelling = getattr(task, "cancelling", None)
        if not task.done() and not (cancelling and cancelling()):
            task.cancel()
    # shield 防止调用方再次 cancel 时，gather 把第二次取消传给正在 finally 中清理的渠道。
    # 3.10 没有 Task.cancelling；本层只主动取消一次，后续取消均在此等清理结束。
    waiter = asyncio.gather(*tasks, return_exceptions=True)
    cancelled = False
    while not waiter.done():
        try:
            await asyncio.shield(waiter)
        except asyncio.CancelledError:
            cancelled = True
    if cancelled:
        raise asyncio.CancelledError


def _prov_id(prov) -> str:
    """provider 的配置 id（竞速日志归因用，取不到返回 "?"）。

    ★ 2026-10-07：紫卡名被读成近形字（冰凇→冰松）却查不出**是哪个渠道读的** ——
    原日志只记「命中渠道（3108 ms）+ JSON」，渠道名没进日志。补上 id 后，
    任何一次错读都能直接归因到具体模型（8B / 30B / glm-4v-flash）。
    """
    try:
        raw_id = prov.meta().id
        pid = str(raw_id) if raw_id is not None else ""
        return pid if pid.strip() else "?"
    except Exception:  # noqa: BLE001 —— 取不到 id 绝不能影响识图
        return "?"


def original_vision_prompt(task: str) -> str:
    """生产可达任务的原版提示词；页面与运行入口共用同一真源。"""
    if task == "riven_json":
        return VisionCommands._RIVEN_JSON_PROMPT
    if task == "riven_lines":
        return VisionCommands._RIVEN_LINE_PROMPT
    from .scan import ScanCommands, lo

    if task == "scan_main":
        return lo.VISION_PROMPT
    if task == "scan_title":
        return ScanCommands._TITLE_PROMPT
    if task == "scan_tiles":
        return ScanCommands._TILES_PROMPT
    if task in ("scan_panel", "scan_mods", "scan_arcanes"):
        return ScanCommands._FOCUS_PROMPTS[task.removeprefix("scan_")]
    raise ValueError(f"未知视觉任务：{task}")


def effective_vision_prompt(host, provider, task: str, original: str) -> str:
    """仅按本次真正收图的渠道取覆盖；旧宿主与无稳定 id 的渠道沿用原文。"""
    store = getattr(host, "vision_prompts", None)
    if store is None:
        return original
    pid = _prov_id(provider)
    if pid == "?":
        return original
    return store.effective(pid, task, original)


class VisionCommands:
    """Mixin：识别链设施（挂载于 main.WarframeSDJK；消费者为 riven 域与 scan 段）。"""

    def _vision_routes_isolated(self) -> bool:
        """缺省保留旧路由；隔离标记损坏或拒读时禁止退回自动选云渠道。"""
        try:
            value = self.cfg.get("vision_routes_isolated", False)
        except Exception:  # noqa: BLE001 - 读取失败按严格隔离处理
            return True
        return value if type(value) is bool else True

    def _vision_route_value(self, key: str) -> Optional[str]:
        """严格路由字段只接受字符串；None 表示拒读/损坏，不等同于主动留空。"""
        try:
            value = self.cfg.get(key, "")
        except Exception:  # noqa: BLE001
            return None
        return value.strip() if isinstance(value, str) else None

    def _vision_last(self, task: str) -> dict:
        """隔离模式分别记紫卡/识卡冷却；旧模式仍使用原来的共享表。"""
        if not self._vision_routes_isolated():
            return self._ocr_last
        if task not in {"riven", "scan"}:
            raise ValueError("未知识图任务")
        states = getattr(self, "_vision_last_by_task", None)
        if not isinstance(states, dict):
            states = self._vision_last_by_task = {}
        return states.setdefault(task, {})

    @staticmethod
    def _vision_refund(last: dict, sender: str, stamp) -> None:
        """只退还本请求写入的冷却，不删除另一任务或后来请求的记录。"""
        if sender and stamp is not None and last.get(sender) == stamp:
            last.pop(sender, None)

    def _vision_gate(self, event, what: str, *, task: str = "scan") -> tuple[Optional[Reply], str]:
        """任务冷却与机器共享资源预算，返回 (拦截回复或 None, 发送者)。

        ① 同一发送者冷却 scan_cooldown 秒（默认 15，设 0 关闭）—— 识卡与紫卡识图
           旧模式共用，隔离模式按任务分别记录；
        ② 机器共享并发上限 scan_max_concurrent（默认 3，设 0 关闭），按 _ocr_busy 计数。
        放行时即记下本次时刻；调用方在识别失败时可 pop 掉以返还冷却。
        """
        _cfg = getattr(self, "cfg", None) or {}  # 绕过 __init__ 构造的实例（单测桩）也能用
        _cd = _num(_cfg, "scan_cooldown", 15)
        _sender = self._safe_sender(event)
        last = self._vision_last(task)
        if _cd > 0 and _sender:
            _last = last.get(_sender, 0.0)
            _left = _cd - (time.time() - _last)
            if _last and _left > 0:
                return (
                    Reply(
                        raw_text=f"⏳ {what}太频繁了，请 {_left:.0f} 秒后再试\n"
                        "（面板「识卡冷却秒数」可调，设 0 关闭限制）"
                    ),
                    _sender,
                )
            # 顺手清掉过期记录，避免长期运行把字典撑大
            if len(last) > 512:
                _now = time.time()
                for _k in [k for k, v in last.items() if _now - v > max(_cd * 4, 120)]:
                    last.pop(_k, None)
            last[_sender] = time.time()
        _cap = int(_num(_cfg, "scan_max_concurrent", 3))
        if _cap > 0 and len(self._ocr_busy) >= _cap:
            return (
                Reply(
                    raw_text=f"⏳ 机器人正在处理其它识图请求（机器共享资源预算，并发上限 {_cap}），请稍后再试"
                ),
                _sender,
            )
        return None, _sender

    @staticmethod
    def _event_has_image(event) -> bool:
        """消息链里是否带图片组件（用于「不支持图片识别」的针对性提示）。"""
        try:
            chain = getattr(getattr(event, "message_obj", None), "message", None) or []
            return any(type(c).__name__ == "Image" for c in chain)
        except Exception:  # noqa: BLE001
            return False

    # vision 渠道优先级：明确的多模态模型 id → id 含视觉关键词 → 当前渠道
    # ⚠️ 实测（2026-09-16，同一张执法者灵化截图 ×3 次对拍真值基准）：
    #    Qwen3-VL-32B-Instruct 伤害行 5/5 全对、零幻觉（但 ~120s，需把
    #    siliconflow 源超时调到 240）；glm-4.1v-thinking-flash 快（20s）
    #    但伤害行反复读串/整轮崩（最差一次 0/9）→ 32B 首位、glm 兜底，
    #    _extract_loadout_from_image 会按序逐个尝试
    _ocr_busy: set = set()  # 正在识卡的会话（并发保护）
    _ocr_last: dict = {}  # 发送者 → 上次识卡时刻（冷却用）
    # 识卡多渠道路由的等待窗口（秒）：窗口内取「校验全过」的最优；
    # 到点即用当前最好结果走聚焦二读，不无限等慢渠道（实测 glm 25 s、
    # 32B 更慢）。窗口 ≳ 最快渠道的响应时间。
    # 2026-09-21 玩家报障：13:43 那次识卡三渠道全废（glm-4v-flash 27 s 空返回，
    # 另两个在 40 s 窗口内没赶上）→ 直接「视觉渠道没给出可解析结果」。
    # 同一张图下一次 30B 是 38.8 s 才返回 —— 距 40 s 只剩 1.2 s，窗口太紧。
    # 放宽到 60 s：慢渠道仍能被等到，最坏等待仍在用户可接受范围（识卡本就 15~30 s 级）。
    RACE_WINDOW_S = 60.0
    _render_lock: Optional[asyncio.Lock] = None  # 渲染串行（PIL 吃 CPU）

    # 按「实测响应速度 + 输出可解析性」排序：识卡是并行竞速 + 面板校验兜底，
    # 先到的先用，所以把小快型号放前面（原来 32B 排第一、取前 3 个时把最快的
    # glm-4v-flash 挤掉了 —— 2026-09-17 实测 3 个慢渠道 30 s 全无返回）。
    #
    # 2026-09-20 用**真实配卡截图**对拍（拉特昂 Prime / 执法者，各 2 任务）后调整：
    #   · zhipu/glm-4v-flash                        3.2 s  伤害行 7/7 全对、输出干净 → 首位
    #   · siliconflow/…/Qwen3-VL-30B-A3B-Instruct   6.7 s  伤害行 7/7 全对   → 新增
    #                                                       （同族 8B 要 67 s，快 10 倍）
    #   · siliconflow/…/Qwen3-VL-8B                67.5 s  最准但极慢        → 降为保底
    #   · zhipu/glm-4.1v-thinking-flash             8.6 s  输出以 <think> 开头、
    #                                                      JSON 解析不出来      → **移除**
    #                                                      （智谱 source 不剥 think，见
    #                                                        astrbot/core/provider/sources/
    #                                                        zhipu_source.py）
    #   · siliconflow/…/Qwen3-VL-32B                1.0 s  HTTP 500/503
    #                                                      「Request failed: Unknown error」
    #                                                      「System is too busy」，4 次全败
    #                                                                          → **移除**
    #   （另测均不入链：Qwen3-Omni-30B-A3B 数值错乱、GLM-4.5V 120 s 超时）
    # ★ 2026-10-06：8B 提到 30B 前 —— S6 提示词下两者准确率持平（各 47/48）、
    #   8B 成本更低；与线上 vision_provider_id=8B 的部署口径对齐（可按需改回）。
    _VISION_PROVIDER_IDS = (
        "zhipu/glm-4v-flash",
        "siliconflow/Qwen/Qwen3-VL-8B-Instruct",
        "siliconflow/Qwen/Qwen3-VL-30B-A3B-Instruct",
    )
    _VISION_ID_HINTS = ("4v", "vl", "vision", "vision-flash", "4o")

    def _cfg_str(self, key: str) -> str:
        """读一个字符串配置项；读取异常一律当空串（识别链不能因配置读取炸掉）。"""
        try:
            return str(self.cfg.get(key) or "").strip()
        except Exception:  # noqa: BLE001
            return ""

    def _try_get_provider(self, pid: str):
        """按 id 取渠道，取不到返回 None（不抛）。"""
        try:
            return self.context.get_provider_by_id(pid)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[sdjk] 取渠道 %s 失败：%s", pid, type(exc).__name__)
            return None

    def _riven_vision_providers(self) -> list:
        """隔离模式仅用紫卡专用项；旧模式保留空值继承兼容配置。"""
        isolated = self._vision_routes_isolated()
        want = (
            self._vision_route_value("vision_provider_id_riven")
            if isolated
            else self._cfg_str("vision_provider_id_riven")
        )
        if want is None:
            return []
        if not want:
            return [] if isolated else self._vision_providers()
        prov = self._try_get_provider(want)
        if prov:
            return [prov]
        if isolated:
            logger.warning("[sdjk] 隔离模式的紫卡主读渠道「%s」不可用，请配置有效专用渠道", want)
            return []
        logger.warning(
            "[sdjk] 配置的 vision_provider_id_riven「%s」取不到；为免紫卡截图外发到未指定的渠道，"
            "本次紫卡主读不回落其它渠道 —— 请在面板选择可用渠道，或清空该项沿用兼容主渠道",
            want,
        )
        return []

    def _line_recheck_vision_providers(self) -> list:
        """分歧复读显式选择；留空关闭，取不到不借用其它渠道。"""
        want = self._vision_route_value("vision_provider_id_line_recheck")
        provider = self._try_get_provider(want) if want else None
        return [provider] if provider is not None else []

    def _line_vision_providers(self) -> list:
        """隔离模式空值只继承紫卡专用主读，显式配置失效不回落。"""
        isolated = self._vision_routes_isolated()
        want = (
            self._vision_route_value("vision_provider_id_line")
            if isolated
            else self._cfg_str("vision_provider_id_line")
        )
        if want is None:
            return []
        if not want:
            return self._riven_vision_providers()
        prov = self._try_get_provider(want)
        if prov:
            return [prov]
        if isolated:
            logger.warning("[sdjk] 隔离模式的紫卡窄读渠道「%s」不可用，本次窄读不回落", want)
            return []
        logger.warning(
            "[sdjk] 配置的 vision_provider_id_line「%s」取不到，窄读路回落紫卡主读渠道", want
        )
        return self._riven_vision_providers()

    def _vision_providers(self) -> list:
        """按优先级返回可用 vision provider 列表（逐个尝试用）。

        ⚠️ 每一步独立容错：以前整段包一个 try/except: pass，任一步异常
        （例如按 id 取渠道时抛错）会**静默**返回空列表 —— 2026-09-17
        识卡全挂、日志里却一条渠道记录都没有，就是栽在这。
        """
        out: list = []
        _try_get = self._try_get_provider

        # ① 面板里显式指定的（可指向任意多模态模型）—— ★ 2026-10-25（Astra 评估 S1）：**独占**。
        #   原写法只把它排在首位，后面照样追加内置候选与带视觉关键词的渠道，识卡又并发取前四个
        #   ⇒ 指定了本地 / 自建模型，截图仍会同时发到其它已配置的云端渠道（离线调用链实测）。
        #   指定了就只用它；取不到也**不回落**（回落等于把截图发给用户没选的渠道），报错让人修配置。
        want = self._cfg_str("vision_provider_id")
        if want:
            prov = _try_get(want)
            if prov:
                return [prov]
            logger.warning(
                "[sdjk] 配置的 vision_provider_id「%s」取不到；为免截图外发到未指定的渠道，"
                "本次不回落其它渠道 —— 请在面板改成可用的渠道 ID，或清空该项改用内置候选",
                want,
            )
            return []
        # ② 内置优先级候选
        for pid in self._VISION_PROVIDER_IDS:
            prov = _try_get(pid)
            if prov and not any(p is prov for p in out):
                out.append(prov)
        # ③ 兜底：扫描全部 provider 里 id 带视觉关键词的
        try:
            allp = self.context.get_all_providers() or []
        except Exception as exc:  # noqa: BLE001
            logger.warning("[sdjk] 枚举渠道失败：%s", type(exc).__name__)
            allp = []
        for prov in allp:
            try:
                pid = getattr(getattr(prov, "meta", lambda: None)(), "id", "") or ""
            except Exception:  # noqa: BLE001
                pid = ""
            if any(h in str(pid).lower() for h in self._VISION_ID_HINTS) and not any(
                p is prov for p in out
            ):
                out.append(prov)
        if not out:
            # 失败时把实际存在的渠道名打出来，便于一眼看出是配置还是代码问题
            ids = []
            for prov in allp:
                try:
                    ids.append(getattr(getattr(prov, "meta", lambda: None)(), "id", "") or "?")
                except Exception:  # noqa: BLE001
                    ids.append("?")
            logger.warning(
                "[sdjk] 没有可用的视觉渠道！现有渠道 %d 个：%s",
                len(ids),
                "、".join(ids[:20]) or "（空）",
            )
        return out

    # 保留旧的 base64 <14MiB 口径；读取原始字节时提前拦截。
    MAX_SCAN_IMAGE_BYTES = (14 * 1024 * 1024 // 4 - 1) * 3
    SCAN_IMAGE_TIMEOUT = 20.0

    @staticmethod
    def _scan_image_headers(url: str) -> dict[str, str]:
        from urllib.parse import urlsplit

        headers = {"Accept-Encoding": "identity"}
        host = (urlsplit(url).hostname or "").lower()
        if any(
            host == suffix or host.endswith("." + suffix)
            for suffix in ("gtimg.cn", "qpic.cn", "qq.com")
        ):
            headers.update(
                {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                    "Referer": "https://im.qq.com/",
                }
            )
        return headers

    async def _read_scan_image(self, ref: str) -> str:
        """自行限量取图；不调用会先完整下载/落盘的宿主 resolver。"""
        import base64
        import os
        import stat
        from urllib.parse import urlsplit
        from urllib.request import url2pathname

        import httpx

        limit = self.MAX_SCAN_IMAGE_BYTES
        if not isinstance(ref, str) or not ref:
            raise ValueError("图片来源为空")
        if ref.startswith(("http://", "https://")):
            # 总时限同时覆盖连接、重定向和持续滴流；每次读取另有 httpx 超时。
            async def download():
                data = bytearray()
                cfg = getattr(self, "cfg", None) or {}
                proxy = cfg.get("proxy") or None
                async with httpx.AsyncClient(
                    timeout=10.0, follow_redirects=False, proxy=proxy
                ) as client:
                    url = ref
                    for hop in range(6):
                        async with client.stream(
                            "GET", url, headers=self._scan_image_headers(url)
                        ) as resp:
                            if resp.is_redirect:
                                if hop == 5 or not resp.next_request:
                                    raise ValueError("图片重定向过多")
                                url = str(resp.next_request.url)
                                if urlsplit(url).scheme not in {"http", "https"}:
                                    raise ValueError("图片重定向协议不支持")
                                continue
                            resp.raise_for_status()
                            if (
                                resp.headers.get("content-encoding", "identity").lower()
                                != "identity"
                            ):
                                raise ValueError("图片压缩传输不支持")
                            declared = resp.headers.get("content-length")
                            if declared is not None and int(declared) > limit:
                                raise ValueError("图片声明体积超限")
                            async for chunk in resp.aiter_raw(chunk_size=64 * 1024):
                                if len(data) + len(chunk) > limit:
                                    raise ValueError("图片实际体积超限")
                                data.extend(chunk)
                            return base64.b64encode(data).decode("ascii")
                raise ValueError("图片重定向失败")

            return await asyncio.wait_for(download(), timeout=self.SCAN_IMAGE_TIMEOUT)
        if ref.startswith("base64://"):
            payload = ref[9:]
        elif ref.startswith("data:"):
            header, payload = ref.split(",", 1)
            if not header.lower().endswith(";base64"):
                raise ValueError("图片 data URI 不是 base64")
        else:
            path = ref
            if ref.startswith("file:"):
                parts = urlsplit(ref)
                netloc = parts.netloc
                if re.fullmatch(r"[A-Za-z]:", netloc):
                    path = url2pathname(netloc + parts.path)
                elif not netloc or netloc.lower() == "localhost":
                    path = url2pathname(parts.path)
                    if re.match(r"^[/\\][A-Za-z]:", path):
                        path = path[1:]
                    elif os.name != "nt" and path.startswith("//"):
                        path = "/" + path.lstrip("/")
                else:
                    raise ValueError("不支持远程 file URI")
            if ref.startswith("file:") or os.path.exists(path):

                def read_local():
                    # O_NONBLOCK 避免伪装成本地文件的 FIFO 卡住工作线程。
                    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
                    try:
                        if not stat.S_ISREG(os.fstat(fd).st_mode):
                            raise ValueError("图片不是普通文件")
                        with os.fdopen(fd, "rb", closefd=False) as source:
                            data = source.read(limit + 1)
                    finally:
                        os.close(fd)
                    if len(data) > limit:
                        raise ValueError("本地图片体积超限")
                    return base64.b64encode(data).decode("ascii")

                return await asyncio.to_thread(read_local)
            payload = ref  # MediaRefStr 的旧版裸 base64
        if len(payload) >= 14 * 1024 * 1024:
            raise ValueError("图片 base64 体积超限")
        payload = "".join(payload.split())
        raw = base64.b64decode(payload + "=" * (-len(payload) % 4), validate=True)
        if len(raw) > limit:
            raise ValueError("图片实际体积超限")
        return base64.b64encode(raw).decode("ascii")

    async def _image_data_urls(self, event) -> list[str]:
        """从消息取第一张合格图片；坏图跳过，后续合格图片不再读取。"""
        out: list[str] = []
        try:
            chain = getattr(getattr(event, "message_obj", None), "message", None) or []
            for c in chain:
                if type(c).__name__ != "Image":
                    continue
                try:
                    ref = getattr(c, "url", None) or getattr(c, "file", None)
                    b64 = await self._read_scan_image(ref)
                    if not b64 or not await asyncio.to_thread(self._image_pixels_ok, b64):
                        continue
                except Exception:  # noqa: BLE001 - 单张失败继续下一张
                    continue
                out.append("data:image/png;base64," + b64)
                break
        except Exception:  # noqa: BLE001 - 图片取不到就当没有
            pass
        return out[:1]

    # 识图截图像素上限：8K 截图约 3300 万像素，留余量取 3600 万。
    # Pillow 默认只在 ~8900 万像素才告警、~1.79 亿才报错，压缩率高的 PNG
    # 在 14MB 体积限制内就能解出几百 MB 位图（解压炸弹），后续缩放/豆子检测吃满 CPU。
    MAX_SCAN_PIXELS = 36_000_000

    @classmethod
    def _image_pixels_ok(cls, b64: str) -> bool:
        """只读图片头拿尺寸（不解码像素），超过 MAX_SCAN_PIXELS 或读不出就拒收。"""
        try:
            import base64
            import io

            from PIL import Image

            with Image.open(io.BytesIO(base64.b64decode(b64))) as img:
                w, h = img.size
        except Exception:  # noqa: BLE001 - 不是合法图片
            return False
        if w * h > cls.MAX_SCAN_PIXELS:
            logger.warning("[sdjk] 截图尺寸 %dx%d 超过像素上限，已拒收", w, h)
            return False
        return True

    async def _vision_race_json(
        self,
        prompt: str,
        image_url: str,
        provs: list,
        *,
        k: int = 2,
        tag: str = "识别",
        window: Optional[float] = None,
        parse=None,
        task: Optional[str] = None,
    ) -> Optional[dict]:
        """向最多 k 个 vision 渠道**并行**请求，取第一个能解析出 JSON 的结果。

        串行试渠道时每条指令要等最慢的那家（实测紫卡识别 13 s）；
        并行竞速后延迟 = 最快渠道的响应时间。失败/解析不出 JSON 的渠道
        自动让位，都不会影响正确性（拿到的必须是能解析的结果）。
        window（秒）：整场竞速的等待上限——到点即返回当前已有结果（None），
        防止渠道级超时（如 siliconflow 240s）把用户晾在原地（2026-09-23 补）。
        parse：自定义判据（2026-09-27 加）——默认按 JSON 解析；紫卡「窄读
        词条行」那一路用它把判据换成「卡面合法」（能解析 ≠ 读全了）。
        """
        import time as _t

        provs = [p for p in (provs or []) if p is not None][: max(1, k)]
        if not provs:
            return None

        async def _one(prov):
            _t0 = _t.perf_counter()
            import uuid as _uuid

            sent_prompt = effective_vision_prompt(self, prov, task, prompt) if task else prompt
            resp = await prov.text_chat(
                prompt=sent_prompt,
                session_id=f"sdjk-{tag}-{_uuid.uuid4().hex[:8]}",
                image_urls=[image_url],
            )
            raw_text = getattr(resp, "completion_text", "") or ""
            text = raw_text.strip()
            return prov, text, (_t.perf_counter() - _t0) * 1000, sent_prompt, raw_text

        tasks = [asyncio.create_task(_one(p)) for p in provs]
        loop = asyncio.get_event_loop()
        deadline = (loop.time() + window) if window else None
        winner = None
        partial = None
        pending = set(tasks)
        try:
            while pending:
                timeout = (deadline - loop.time()) if deadline else None
                if timeout is not None and timeout <= 0:
                    break
                done, pending = await asyncio.wait(
                    pending, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
                )
                for t in done:
                    if t.cancelled():
                        logger.warning("[sdjk] %s渠道任务已取消，继续等待其它渠道", tag)
                        continue
                    try:
                        prov, text, ms, sent_prompt, raw_text = t.result()
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("[sdjk] %s渠道异常：%s", tag, type(exc).__name__)
                        continue
                    if not text:
                        logger.warning(
                            "[sdjk] %s渠道返回空（%.0f ms，渠道 %s）", tag, ms, _prov_id(prov)
                        )
                        continue
                    data = (parse or lo.parse_vision_json)(text)
                    raw_parsed = copy.deepcopy(data)
                    if task == "riven_json" and data:
                        data = self._adapt_riven_transcription(data)
                        lexical = self._adapt_riven_transcription(
                            lo.parse_vision_json(text, preserve_numbers=True)
                        )
                        data["_numeric_lexemes"] = {
                            key: lexical.get(key) for key in ("positive", "negative")
                        }
                        unsafe = False
                        lexical_invalid = False
                        from ..parser import RIVEN_STAT_ZH

                        rev = {v: k for k, v in RIVEN_STAT_ZH.items()}
                        for key in ("positive", "negative"):
                            for name, value in self._iter_stat_pairs(lexical.get(key), key):
                                try:
                                    sid = (
                                        self._stat_id_from_name(str(name), rev)
                                        if name is not None
                                        else None
                                    )
                                    decimal, _multiplier = self._stat_decimal(value, sid)
                                    unsafe |= decimal != Decimal(str(float(decimal)))
                                except (ValueError, InvalidOperation, OverflowError):
                                    lexical_invalid = True
                        data["_numeric_precision_unsafe"] = unsafe
                        data["_numeric_lexical_invalid"] = lexical_invalid
                    # ★ 采信判据（2026-10-05 收紧）：dict 里**至少一个有效字段**。
                    #   「解析成功但字段全 None/空」的快渠道不许赢 —— 否则会抢在
                    #   「慢但完整」的渠道前面被采信，weapon/词条全部落空
                    #   （识图测试卡 6 实证：JSON 少一个 ] 时 30B 吐全 None 结构）。
                    if data and any(
                        data.get(k)
                        for k in ("weapon", "mods", "panel", "positive", "negative", "lines")
                    ):
                        logger.info(
                            "[sdjk] %s命中渠道（%.0f ms，渠道 %s）：解析有效",
                            tag,
                            ms,
                            _prov_id(prov),
                        )
                        if task in {"riven_json", "riven_lines"}:
                            data = dict(data)
                            data["_recognition"] = {
                                "source": "model",
                                "provider_id": _prov_id(prov),
                                "prompt_hash": sha256(sent_prompt.encode("utf-8")).hexdigest(),
                                "raw_text": raw_text,
                                "parsed": raw_parsed,
                                "numeric_lexemes": copy.deepcopy(data.get("_numeric_lexemes")),
                            }
                        if data.get("_line_legal") is False:
                            partial = data
                            continue
                        winner = data
                        break
                    logger.warning(
                        "[sdjk] %s JSON 解析失败（%.0f ms，渠道 %s）",
                        tag,
                        ms,
                        _prov_id(prov),
                    )
                if winner:
                    break
        finally:
            await _cancel_vision_tasks(tasks)
        return winner or partial

    # 双路都只读取观察：主读保留文字与数值原文，窄读附文字坐标。
    # 卡片区域、单位、数值换算与一致性由代码判断；无模型名或具体卡片示例。
    _RIVEN_JSON_PROMPT = '读取这张 Warframe 紫卡，只转录卡面原文，输出一行合法 JSON：{"weapon":"卡面武器名或部件名，看不清用null","stat_lines":["一条完整词条原文"]}。\nweapon 保留卡面印出的变体和 Prime，不含后面的拉丁词缀，不加主要/次要后缀。stat_lines 从上到下逐条记录当前卡面的词条，保留每条数值的正负号、小数、百分号、米/秒单位、x或×倍率及名称限定语；换行续文合在同一条。\n锁定图标只是图标，图标旁的词条仍要完整转录；不把图标抄成数字或字母。未揭示词条用问号保留位置。看不清的字符写问号，不猜数。卡片之外的预览、配装面板、聊天、按钮不属于本卡词条。段位、容量、洗练次数不属于词条。\n每条词条只出现一次，不拆成名称和值两个字段，不做正负面判断、数值换算、武器分析或配装建议。'

    _RIVEN_LINE_PROMPT = "Read all the text in the image. Return each text line as cx,cy,width,height,angle,text. The first five values describe a rotated rectangle: center x, center y, side width, side height, and rotation angle in degrees. Normalize the center and side lengths to 0-1000; angle is in [-90,90]. Preserve the recognized text after the coordinates. Include visible words and Arabic numerals, even faint text. Do not transcribe icons, borders, rank pips or decorative marks as characters. Keep text from each separate region on separate lines. Do not calculate or infer missing text."

    @classmethod
    def _adapt_riven_transcription(cls, observed: dict) -> dict:
        """逐行转录只解码一次；旧二元协议继续兼容，混合协议不静默覆盖。"""
        if "stat_lines" not in observed:
            return observed
        out = copy.deepcopy(observed)
        raw = out["stat_lines"]
        if not isinstance(raw, list):
            raw = [raw]
        pairs = []
        for line in raw:
            pair = cls._signed_stat_line(line)
            pairs.append(
                {"name": pair[0], "value": pair[1]}
                if pair
                else {"name": line if isinstance(line, str) else None, "value": None}
            )
        if out.get("positive") or out.get("negative"):
            pairs.append({"name": None, "value": None})
            out["_transcription_schema_conflict"] = True
        out["positive"], out["negative"] = pairs, []
        return out

    @classmethod
    def _riven_lines_legal(cls, lines, *, normalization: Optional[list] = None) -> tuple:
        """卡面原文行 → (pos, neg, 是否合法卡面, 备注)。

        合法 = 2~3 条正面 + ≤1 条负面（「？？？」未揭示行计入条数）**且没有「像是词条行却没解析成功」的行**。
        判据放在这里共用：窄读竞速的采信判据（读丢一行就不合法）与主流程的
        兜底判据必须是同一套。
        ★ 2026-10-02：线上实证（16:17 海波单剑）——窄读把「触发几率」OCR 成
        「脆发几率」被跳过，剩下 3 正 1 负恰好满足条数判据，整卡被残缺结果
        覆盖（丢一条正词条 + 把派系行当正词条）。现在**致命跳过**（词条名认不出 /
        数值读不出 / 乘数读不出）一律不合法；**结构性杂行**（武器名 / 图例 /
        内融值 / 段位 —— 均记「无极性符号」）不算致命：解析设计上本就靠无极性
        符号排除它们（见 parse_riven_lines 注释），它们不承载词条信息。
        丢符号但像词条的行另有兜底：主流程两路交叉校验（行读条数少于语义表
        ⇒ 退回语义表）。
        """
        try:  # 服务器以包成员加载，相对导入才可靠
            from .. import riven_analysis as RA
            from ..parser import RIVEN_STAT_ZH
        except ImportError:  # pragma: no cover - 本地直跑
            from core import riven_analysis as RA
            from core.parser import RIVEN_STAT_ZH
        rev = {v: k for k, v in RIVEN_STAT_ZH.items()}

        def resolve(name):
            evidence = {}
            sid = cls._stat_id_from_name(name, rev, evidence=evidence)
            if normalization is not None:
                normalization.append(evidence)
            return sid

        pos, neg, notes = RA.parse_riven_lines(
            lines,
            resolve,
            lambda nm: cls._stat_id_exact(nm, rev),
            role_resolve=lambda nm: cls._stat_id_from_name(nm, rev),
        )
        fatal = [n for n in notes if not n.startswith("无极性符号")]
        # 「？？？」未揭示词条（融合未选定，2026-10-24）计入条数：2 正 1 负 + 1 行问号 = 3+1 合法
        counts_ok = bool(RA.count_candidates(len(pos), len(neg), RA.hidden_line_count(lines)))
        return pos, neg, (counts_ok and bool(pos or neg) and not fatal), notes

    @classmethod
    def _parse_riven_lines_text(cls, text: str) -> dict:
        """解包窄读原文并保留合法性；不合法的观察仍交给业务入口核对。"""
        from .. import riven_analysis as RA
        from ..parser import RIVEN_STAT_ZH

        rev = {v: k for k, v in RIVEN_STAT_ZH.items()}
        raw_lines = lo.coordinate_text_lines(text)
        decoded = RA.decode_riven_lines(
            raw_lines,
            lambda name: cls._stat_id_exact(name, rev),
            resolve=lambda name: cls._stat_id_from_name(name, rev),
        )
        lines = decoded["lines"]
        pos, neg, legal, notes = cls._riven_lines_legal(lines)
        # 不合法也是观察证据；不能让竞速入口吞掉疑似漏行或冲突原文。
        return (
            {"lines": lines, "_line_decode": decoded, "_line_legal": legal, "_line_notes": notes}
            if raw_lines
            else {}
        )

    async def _extract_riven_from_image(self, image_url: str) -> Optional[dict]:
        """调 vision 渠道从紫卡截图提取词条，返回解析后的 dict 或 None。

        ★ 两路并行（2026-09-27）：
          · 窄读词条行与语义 JSON 分别保留原文、解码和来源；
          · 业务入口按字段对齐，冲突或疑似漏行转待核对。
        两路都失败才返回 None（调用方给「重发一次」的提示）。
        """
        local = getattr(self, "local_ocr", None)
        if local is not None:
            local_status = local.status()
            # 本地已选中或配置损坏时只走本地结果；失败不能落进下面两条模型渠道。
            if local_status.get("enabled") or local_status.get("ok") is not True:
                from ..local_ocr import decode_image_data_url

                try:
                    observed = await local.recognize(decode_image_data_url(image_url))
                except (ValueError, OSError) as exc:
                    observed = {"ok": False, "state": "invalid_image", "message": str(exc)}
                structured = observed.get("structured")
                return {
                    **(structured if isinstance(structured, dict) else {}),
                    "_local_ocr": observed,
                }
        # ★★ 2026-10-05 去掉全部字面示例值（实测 30B 真泄漏 10/32：把例子里的
        #   数值照抄到不含该值的卡上）。旧例子 50.2/65.1/44.9/95.4/63.4/115.7/110.8
        #   全部来自用户真实卡 ⇒ 本提示词不得再出现任何具体数值，一律用公式/描述表达。
        prompt = VisionCommands._RIVEN_JSON_PROMPT
        # ★ 2026-10-07（维护者裁定）：紫卡这一路**不竞速**（单渠道，`k=2` 退化为单发）。
        #   起因：`zhipu/glm-4v-flash` 把「冰凇」**稳定**读成「冰松」（5/5 全错）且它最快
        #   （~3.1 s）⇒ k=2 竞速里它必赢，武器名又是整卡的承重件 ⇒ 宁慢不赌，只走一个渠道。
        #   ⚠ 识卡（配卡截图）那一路**保留**原竞速：那里用真实配卡对拍，glm 是 7/7 全对
        #   且要速度（见 `_VISION_PROVIDER_IDS` 上方的 2026-09-20 记录）。
        #
        # ★ 2026-10-12 改选渠道口径（维护者裁定"别再有定死"）：**完全按
        #   `_riven_vision_providers()` 的顺序取第一个**；专用配置留空时沿用兼容主渠道。
        #   原实现有两处名字级硬编码（无条件优先 8B），
        #   使面板配置对紫卡一路**静默无效**（用户实测：配置改 30B、插件重载两次仍走 8B）。
        #   此次连"候选为空时按名字回落 8B"的分支也一并删除：没有候选就交给调用方
        #   按"无可用视觉渠道"正常报错，不再靠名字猜。
        #   换 30B 的依据（bench3，2026-10-12，同一批 16+2 卡）：30B 数值 65/70 优于
        #   8B 的 62/70、16 卡 perfect 15 vs 12；耗时 9.1 s vs 18.3 s。
        _all_provs = self._riven_vision_providers()
        provs = _all_provs[:1]
        if not provs:
            logger.warning("[sdjk] 紫卡识别：没有可用视觉渠道（配置/渠道状态见上一条）")
        if provs:
            logger.info("[sdjk] 紫卡识别渠道（不竞速）：%s", _prov_id(provs[0]))
        # ★ 2026-10-15：窄读路可用**独立渠道**（`vision_provider_id_line`）。
        #   窄读按现有提示逐行返回词条，并保留正负号、小数与百分号。
        #   未配置时与紫卡主读同源；独立配置取不到时也沿用紫卡主读渠道。
        # 未配置独立渠道时直接复用主渠道结果（别再枚举一遍、重复打「取不到」告警）
        line_provs = (
            self._line_vision_providers()[:1]
            if self._vision_routes_isolated() or self._cfg_str("vision_provider_id_line")
            else provs
        )
        if line_provs and (not provs or line_provs[0] is not provs[0]):
            logger.info("[sdjk] 紫卡窄读渠道（独立配置）：%s", _prov_id(line_provs[0]))
        try:
            # 并行竞速（原来串行试渠道，实测要 13 s）；窗口 60s（2026-09-23 补：
            # 渠道级超时可达 240s，不能让用户干等）
            data, lines = await asyncio.gather(
                self._vision_race_json(
                    prompt,
                    image_url,
                    provs,
                    k=2,
                    tag="紫卡识别",
                    window=self.RACE_WINDOW_S,
                    task="riven_json",
                ),
                self._vision_race_json(
                    self._RIVEN_LINE_PROMPT,
                    image_url,
                    line_provs,
                    k=2,
                    tag="紫卡行读",
                    window=self.RACE_WINDOW_S,
                    parse=self._parse_riven_lines_text,
                    task="riven_lines",
                ),
                return_exceptions=True,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[sdjk] 紫卡识别失败：%s", type(exc).__name__)
            return None
        for name, val in (("识别", data), ("行读", lines)):
            if isinstance(val, BaseException):
                logger.warning("[sdjk] 紫卡%s异常：%s", name, type(val).__name__)
        data = None if isinstance(data, BaseException) else data
        lines = None if isinstance(lines, BaseException) else lines
        if not data and not lines:
            logger.warning("[sdjk] 紫卡识别：两路都没给出可用结果")
            return None
        recheck_evidence = None
        recheck_id = self._cfg_str("vision_provider_id_line_recheck")
        if data and recheck_id:
            from .. import riven_analysis as RA
            from ..parser import RIVEN_STAT_ZH

            rev = {value: key for key, value in RIVEN_STAT_ZH.items()}
            semantic_pos, semantic_neg = self._normalize_llm_stats(copy.deepcopy(data), rev)

            def signature(positive, negative):
                return sorted(
                    (sid, bucket, str(Decimal(str(value)).normalize()))
                    for rows, bucket in ((positive, "positive"), (negative, "negative"))
                    for sid, value in rows
                )

            main_signature = signature(semantic_pos, semantic_neg)
            lp, ln, legal, _ = self._riven_lines_legal((lines or {}).get("lines", []))
            # 主读残缺时不以复读替它补答案；只裁决两个完整转录间的分歧。
            raw_count = sum(
                len(self._iter_stat_pairs(data.get(k), k)) for k in ("positive", "negative")
            )
            main_complete = bool(
                RA.count_candidates(len(semantic_pos), len(semantic_neg), 0)
            ) and raw_count == len(main_signature)
            needs_recheck = not legal or signature(lp, ln) != main_signature
            provider = (
                self._try_get_provider(recheck_id) if main_complete and needs_recheck else None
            )
            if provider and all(_prov_id(provider) != _prov_id(p) for p in line_provs):
                checked = await self._vision_race_json(
                    self._RIVEN_LINE_PROMPT,
                    image_url,
                    [provider],
                    k=1,
                    tag="紫卡分歧复读",
                    window=self.RACE_WINDOW_S,
                    parse=self._parse_riven_lines_text,
                    task="riven_lines",
                )
                cp, cn, check_legal, _ = self._riven_lines_legal((checked or {}).get("lines", []))
                observed_count = sum(
                    bool(RA._POL_RE.match(row) or RA._HIDDEN_RE.match(row))
                    for row in (lines or {}).get("lines", [])
                )
                adopt = (
                    check_legal
                    and signature(cp, cn) == main_signature
                    and observed_count <= len(main_signature)
                    and not data.get("_numeric_precision_unsafe")
                    and not data.get("_numeric_lexical_invalid")
                )
                recheck_evidence = {
                    "original_line_read": copy.deepcopy(lines),
                    "recheck": copy.deepcopy(checked),
                    "adopted": adopt,
                    "reason": "main_and_complete_recheck_agree" if adopt else "unresolved_recheck",
                    "original_observed_stat_rows": observed_count,
                }
                if adopt:
                    lines = checked
        out = dict(data or {})
        if recheck_evidence is not None:
            out.setdefault("_recognition", {})["line_recheck"] = recheck_evidence
        # 主读 JSON 的附加键不是独立逐行观察。原响应仍在 recognition.parsed
        # 留档；只有下方真实窄读响应可提供业务消费的 lines 及解包元数据。
        # 本地 OCR 在上方提前返回，其 lines 来自 worker，不经过此云端合并。
        for key in ("lines", "_line_decode", "_line_legal", "_line_notes"):
            out.pop(key, None)
        if lines:
            out["lines"] = list(lines.get("lines") or [])
            for key in ("_line_decode", "_line_legal", "_line_notes"):
                if key in lines:
                    out[key] = copy.deepcopy(lines[key])
            line_recognition = lines.get("_recognition")
            if isinstance(line_recognition, dict):
                recognition = out.setdefault("_recognition", {})
                if not isinstance(recognition, dict):
                    recognition = out["_recognition"] = {}
                recognition["line_read"] = copy.deepcopy(line_recognition)
        return out

    @staticmethod
    async def _detect_pips(image_url: str) -> list:
        """豆子（卡片底部的等级刻度）像素检测 —— 识卡等级的**第二信号**。

        与「容量数字反推」互相独立（一个读数字、一个数像素），两者一致才采信。
        为什么需要：容量反推常有多解（私法补给 容量 5 → 1白/5绿/0红），
        旧策略「取最高」会把 0 级卡判成满级；豆子能唯一定出答案。

        ⚠️ 必须传**原图**（在 `_fit_scan_image` 缩放**之前**）—— 检测本身是尺度
             自适应的，但缩放会引入插值模糊，直接影响豆子边界判定。
        ⚠️ 纯 CPU 活，必须 to_thread（直调会卡住整个事件循环 → 整台机器人变卡）。
        ⚠️ 任何失败都返回空表：豆子只是**增强信号**，绝不能拖垮识卡主流程。
        """
        if not image_url or not image_url.startswith("data:"):
            return []

        def _run() -> list:
            import base64
            import io

            from PIL import Image

            _head, b64 = image_url.split(",", 1)
            img = Image.open(io.BytesIO(base64.b64decode(b64)))
            return pips_engine.detect_pips(img)

        try:
            rows = await asyncio.to_thread(_run)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[sdjk] 豆子检测失败（不影响识卡）：%s", type(exc).__name__)
            return []
        if rows:
            eq = [r for r in rows if not r.get("is_inventory")]
            logger.info(
                "[sdjk] 豆子检测：装备区 %d 行，豆数 %s", len(eq), [r.get("counts") for r in eq]
            )
        return rows

    @staticmethod
    def _dump_scan_debug(image_url: str, keep: int = 3) -> None:
        """把「豆子检测到了、但对齐无解」的原图存一份，便于事后排查。

        只在**这种少见情形**下落盘（正常识卡不写任何图），且只保留最近 `keep` 张。
        起因：2026-09-20 用户 4K 截图出现「北风 1 级被判 3 级」，根因在检测器内部，
        但服务端拿不到用户的原图 → 只能靠日志猜。存下原图后可以直接复现。
        """
        try:
            import base64 as _b64
            import io as _io
            import time as _time

            from PIL import Image as _Image

            if not image_url.startswith("data:"):
                return
            _head, _b = image_url.split(",", 1)
            img = _Image.open(_io.BytesIO(_b64.b64decode(_b)))
            d = core_paths.write_path(f"scan_debug/{int(_time.time())}.jpg")
            img.convert("RGB").save(d, "JPEG", quality=90)
            files = sorted((core_paths.run_dir() / "scan_debug").glob("*.jpg"))
            for old in files[:-keep]:
                try:
                    old.unlink()
                except OSError:
                    pass
            logger.info("[sdjk] 已存排查用原图：%s", d)
        except Exception as exc:  # noqa: BLE001 —— 排查辅助，绝不能影响识卡
            logger.debug("[sdjk] 存排查图失败：%s", type(exc).__name__)

    # LLM 可能输出词条全称，先归一到缩写
    # ★ 2026-10-03：只做「全称 → 缩写」归一，**不得做语义相反的跨词条改写**。
    #   删过两条：「射速」→「攻速」（把远程词条压成近战 attack_speed —— 用户
    #   口径「远程叫射速」；RIVEN_STAT_ALIASES 已把「射速」映射到 fire_rate）与
    #   「元素伤害」→「基伤」（紫卡没有「元素伤害」这一通用词条，元素是
    #   火/冰/毒/电伤；删后交给 RIVEN_STAT_ALIASES/形近容错各自处理）。
    _STAT_ALIAS = {
        "滑行暴击": "滑暴",
        "攻击速度": "攻速",
        "伤害": "基伤",
        "装填速度": "装填",
        "触发几率": "触发",
        "多重射击": "多重",
        "暴击几率": "暴击",
    }
    # 卡面全称/别名 → 标准 id（parser.RIVEN_STAT_ALIASES 反查，首次用时构建）。
    # 必须有这张表：「暴击伤害」走包含匹配会先撞上短名「暴击」（crit_chance），
    # 2026-09-24 实测把暴伤按暴击率的基值算（手枪 149.99 vs 90），区间对不上后
    # 误报「武器名可能识别有误」。
    _STAT_ALIAS_FULL: "dict[str, str] | None" = None

    @classmethod
    def _full_stat_alias(cls) -> dict:
        if cls._STAT_ALIAS_FULL is None:
            try:  # 服务器以包成员加载，相对导入才可靠
                from ..parser import RIVEN_STAT_ALIASES
            except ImportError:  # pragma: no cover - 本地直跑
                from core.parser import RIVEN_STAT_ALIASES
            full: dict[str, str] = {}
            for sid, names in RIVEN_STAT_ALIASES.items():
                for n in names:
                    full.setdefault(n, sid)
            cls._STAT_ALIAS_FULL = full
        return cls._STAT_ALIAS_FULL

    @classmethod
    def _stat_id_exact(cls, name: str, rev: dict) -> "str | None":
        """只认**整名精确命中**（别名 / 全称 / 短名表），不做包含匹配与形近 ——
        给折行拼接当闸门用（2026-10-24），宽匹配会把「词条 + 图例行」拼出假词条。"""
        name = re.sub(r"\s+", "", str(name or ""))
        name = cls._STAT_ALIAS.get(name, name)
        return cls._full_stat_alias().get(name) or rev.get(name)

    @classmethod
    def _stat_id_from_name(
        cls, name: str, rev: dict, *, evidence: Optional[dict] = None
    ) -> "str | None":
        """整名精确 → 唯一的一字近名 → 唯一最长完整别名；跨 SID 歧义拒绝。

        括注仅接受已知类别说明，且主体须整名精确命中对应 SID；未知、错配、
        未闭合括号不能通过裁头或近名匹配变成已知词条。evidence 可选记录来源。
        """
        from ..riven_analysis import split_stat_annotation

        raw_name = name
        name = re.sub(r"\s+", "", str(name or ""))

        def finish(sid, method, alias=None, candidates=None):
            if evidence is not None:
                evidence.update(
                    raw_name=raw_name,
                    lookup_name=name,
                    sid=sid,
                    method=method,
                    matched_name=alias,
                )
                if candidates:
                    evidence["candidates"] = sorted(candidates)
            return sid

        if not name:
            return finish(None, "unknown")
        full = cls._full_stat_alias()
        normalized = cls._STAT_ALIAS.get(name, name)
        sid = full.get(normalized) or rev.get(normalized)
        if sid:
            return finish(sid, "exact", name)
        base, required_sid, valid = split_stat_annotation(name)
        if not valid:
            return finish(None, "rejected_annotation")
        if required_sid:
            sid = cls._stat_id_exact(base.strip(), rev)
            if sid == required_sid:
                return finish(sid, "category_annotation", base.strip())
            return finish(None, "rejected_annotation")
        pool = dict(full)
        for alias, sid in rev.items():
            pool.setdefault(alias, sid)
        if len(name) >= 3:
            near: dict[str, str] = {}
            for alias in sorted(pool):
                if len(alias) >= 3 and cls._one_edit_apart(name, alias):
                    near.setdefault(pool[alias], alias)
            if len(near) == 1:
                sid, alias = next(iter(near.items()))
                logger.info("[sdjk] 紫卡词条名容错：状态=已归一，类别=单字差异")
                return finish(sid, "one_edit", alias)
            if near:
                return finish(None, "ambiguous_one_edit", candidates=near)
        # 仅整段包含至少三字的完整别名。不同 SID 同为最长时拒绝；两字短名
        # 不能把完整错字词条抢成「暴击」或把陌生「武器伤害」吞成「伤害」。
        contained: dict[str, str] = {}
        for alias, sid in pool.items():
            if len(alias) >= 3 and alias in name:
                if len(alias) > len(contained.get(sid, "")):
                    contained[sid] = alias
        if contained:
            longest = max(map(len, contained.values()))
            winners = {sid: alias for sid, alias in contained.items() if len(alias) == longest}
            if len(winners) == 1:
                sid, alias = next(iter(winners.items()))
                return finish(sid, "contained_alias", alias)
            return finish(None, "ambiguous_contained", candidates=winners)
        return finish(None, "unknown")

    @staticmethod
    def _one_edit_apart(left: str, right: str) -> bool:
        """恰好一次插入、删除或替换；不把换位当一次编辑。"""
        if abs(len(left) - len(right)) > 1:
            return False
        if len(left) == len(right):
            return sum(a != b for a, b in zip(left, right)) == 1
        short, long = sorted((left, right), key=len)
        for i, (a, b) in enumerate(zip(short, long)):
            if a != b:
                return short[i:] == long[i + 1 :]
        return True

    @staticmethod
    def _faction_val_fix(sid: str, num: float) -> float:
        """对派系伤害：把模型可能填成「乘数」的数值换算回百分数 magnitude。

        卡面这行是乘数写法（净伤害倍率），模型有三种填法都要能接住：
            x1.51 → 正确填 51（不动）；也可能填 1.51 或 151（都要还原成 +51）
            x0.55 → 正确填 45（不动）；也可能填 0.55 或 55（还原成 −45）
        判据：真 magnitude ∈ [≈11, ≈95]（基值 45 × 倾向 0.5~1.55 × 系数 ≤1.2375
        × 1.1）⇒ 落在 [10, 100) 之外的数值一定是乘数（或乘数×100）。
        返回带符号的数：负值交给 `_normalize_llm_stats._route` 归到 negative。
        """
        if not sid or not sid.startswith("damage_vs_"):
            return num
        try:  # 服务器以包成员加载，相对导入才可靠
            from .. import riven_analysis as RA
        except ImportError:  # pragma: no cover - 本地直跑
            from core import riven_analysis as RA
        if not (num >= 100 or num < RA._FACTION_MIN_MAG):
            return num
        k = num / 100.0 if num >= 100 else num
        if not RA.is_faction_mult(k):
            # 出界的「乘数」不是派系乘数（线上实证：5800 这类读数被当乘数换算
            # 会得到 |1−58|×100 = 5700）⇒ 原样返回，不做乘法换算。
            return num
        mag, neg = RA.faction_mult_to_mag(k)
        return -mag if neg else mag

    # ★ 2026-10-14：视觉模型对 positive/negative 的**元素形状**会漂移，键名也不固定。
    #   实测四种形态（同一张卡两次跑就换一种）：
    #     [["名", 值], ...]（正常）/ ["名", 值]（整条扁平）/ [{"name":..,"value":..}]
    #     / [{"词条缩写":..,"数值":..}]。
    _STAT_NAME_KEYS = ("词条名原文", "name", "词条缩写", "词条", "词条名", "stat")
    _STAT_VAL_KEYS = ("value", "数值", "val", "值")

    @staticmethod
    def _signed_stat_line(raw):
        """只识别完整签名行；不从混杂名称中剥数字。"""
        if not isinstance(raw, str):
            return None
        # 可见图标可被模型转写为 Unicode 符号。只剥行首图标与空白；
        # 数字、字母、问号、正负号均不能被当装饰删除。
        raw = raw.lstrip()
        while raw and unicodedata.category(raw[0]) in {"So", "Cf", "Mn"}:
            raw = raw[1:].lstrip()
        from ..riven_analysis import _trailing_to_leading

        raw = _trailing_to_leading(raw) or raw
        match = re.fullmatch(
            r"\s*((?:[+−-]\s*\d+(?:\.\d+)?(?:\s*(?:%|m|米|秒))?|[x×]\s*\d+(?:\.\d+)?))\s*(\S.*?)\s*",
            raw,
        )
        if not match:
            return None
        # 无单位的数字与名称之间必须有空白，防止kg等未知单位吞入名称。
        token = match[1]
        if not token.endswith(("%", "m", "米", "秒")) and not token.startswith(("x", "×")):
            boundary = match.end(1)
            if boundary < len(raw) and not raw[boundary].isspace():
                return None
        return match[2], re.sub(r"\s+", "", match[1]).replace("−", "-")

    @staticmethod
    def _stat_decimal(raw, sid=None):
        """数值词法和精度独立；x只允许派系SID，未知单位拒绝。"""
        if raw is None or isinstance(raw, bool):
            raise ValueError("missing numeric observation")
        match = re.fullmatch(r"\s*([x×]?)\s*([+−-]?\s*\d+(?:\.\d+)?)\s*(%|m|米|秒)?\s*", str(raw))
        if not match:
            raise ValueError("unsupported numeric lexeme")
        value = Decimal(re.sub(r"\s+", "", match[2]).replace("−", "-"))
        if sid and (
            (match[3] in ("m", "米") and sid != "range")
            or (match[3] == "秒" and sid != "combo_duration")
        ):
            raise ValueError("unit does not belong to stat")
        if match[1]:
            if match[3] or not sid or not sid.startswith("damage_vs_"):
                raise ValueError("multiplier requires faction stat")
            from .. import riven_analysis as RA

            if not RA.is_faction_mult(float(value)):
                raise ValueError("invalid faction multiplier")
        return value, bool(match[1])

    @classmethod
    def _structured_stat_pair(cls, name, value, bucket=None):
        """适配现存观察，不生成额外行；嵌入数值与独立字段必须一致。"""
        from ..parser import RIVEN_STAT_ZH

        rev = {v: k for k, v in RIVEN_STAT_ZH.items()}
        # 倒置二元对只有数字侧/名称侧角色唯一时才能反转。
        # x词法须先由另一侧唯一名称确认派系SID，不能拿无SID的数值探针判非法。
        if isinstance(name, str) and re.match(r"\s*[x×]", name):
            reverse_sid = (
                cls._stat_id_from_name(str(value), rev) if isinstance(value, str) else None
            )
            if reverse_sid and reverse_sid.startswith("damage_vs_"):
                try:
                    _number, explicit_multiplier = cls._stat_decimal(name, reverse_sid)
                except (ValueError, InvalidOperation, OverflowError):
                    explicit_multiplier = False
                if explicit_multiplier:
                    return value, name
        try:
            cls._stat_decimal(name)
            first_numeric = True
        except (ValueError, InvalidOperation):
            first_numeric = False
        try:
            cls._stat_decimal(value)
            second_numeric = True
        except (ValueError, InvalidOperation):
            second_numeric = False
        if first_numeric and not second_numeric and isinstance(value, str):
            name, value = value, name
        embedded = cls._signed_stat_line(name)
        if not embedded:
            if isinstance(name, str) and re.match(r"\s*(?:[+−-]\d|[x×]\d)", name):
                return name, {"invalid_observation": value}
            return name, value
        clean_name, observed = embedded
        # 同一完整原文被重复放进名称和值两个字段时，只解码一次。
        # 不按某个词条/模型宽松放行：整行须完全一致，且名称、单位仍走共同校验。
        if isinstance(value, str) and value.strip() == name.strip():
            value = observed
        name_evidence = {}
        sid = cls._stat_id_from_name(clean_name, rev, evidence=name_evidence)
        if not sid or name_evidence.get("method") == "contained_alias" or value is None:
            # 数值已剥出后，剩余名称不能靠裁掉未知单位/前缀得到一个局部命中。
            return name, {"invalid_observation": value}
        try:
            left, lm = cls._stat_decimal(observed, sid)
            right, rm = cls._stat_decimal(value, sid)

            # 无单位沿用既有幅度兼容；双方显式单位必须相同（米/m等价）。
            def explicit_unit(token):
                match = re.search(r"(%|m|米|秒)\s*$", str(token))
                return "m" if match and match[1] in ("m", "米") else match[1] if match else None

            left_unit, right_unit = explicit_unit(observed), explicit_unit(value)
            if left_unit and right_unit and left_unit != right_unit:
                return name, {"invalid_observation": value}

            # 明确倍率或派系历史裸倍率均用已有业务定义比较。
            def comparable(number, multiplier):
                # 比较保留原词法有效位；默认28位会把不同倍率舍入成相等。
                with localcontext() as ctx:
                    digits = number.as_tuple()
                    ctx.prec = max(ctx.prec, len(digits.digits) + abs(digits.exponent) + 8)
                    if multiplier:
                        return (number - 1) * 100
                    if sid.startswith("damage_vs_"):
                        from .. import riven_analysis as RA

                        if number >= 100 or number < Decimal(str(RA._FACTION_MIN_MAG)):
                            factor = number / 100 if number >= 100 else number
                            if RA.is_faction_mult(float(factor)):
                                return (factor - 1) * 100
                    return number

            unsigned_magnitude = (
                bucket == "negative"
                and re.fullmatch(r"\d+(?:\.\d+)?\s*(?:%|m|米|秒)?", str(value).strip())
                and comparable(left, lm).copy_abs() == comparable(right, rm)
            )
            if comparable(left, lm) != comparable(right, rm) and not unsigned_magnitude:
                # 显式符号冲突不能转成null后被另一条观察唯一补足。
                return name, {"invalid_observation": value}
        except (ValueError, InvalidOperation, OverflowError):
            return name, {"invalid_observation": value}
        return clean_name, observed

    @classmethod
    def _iter_stat_pairs(cls, raw, bucket=None) -> list:
        """模型输出的 positive/negative → [(词条名, 数值)]，兼容形状漂移。

        ★ 2026-10-14 线上事故（`紫卡分析` + 截图连续报「内部错误」）：
        旧实现 `to_stat(*item)` 假定每条都是二元序列 ——
          · 整条是**扁平** `["变焦", "60.8"]` 时，迭代拿到裸字符串，`*item` 把它
            **按字符拆成位置参数**（"60.8"→4 个）⇒ TypeError ⇒ 整条指令崩掉；
          · 元素是**字典**时，`*item` 解包的是**键**（"name"/"value"）⇒ to_stat 恒
            返回 None ⇒ 词条**静默全丢**（日志实证：语义表 0 正 0 负）。
        这里统一成二元对；坏行保留 (None, None) 占位，不补字段、不缩短整卡分母。
        """
        if raw is None:
            return []
        if isinstance(raw, dict):
            raw = [raw]
        if not isinstance(raw, (list, tuple)):
            return [(None, None)]
        # 整行列表和扁平键值必须分开；坏行保持原分母。
        if all(not isinstance(x, (list, tuple, dict)) for x in raw):
            if raw and all(cls._signed_stat_line(x) for x in raw):
                return [cls._signed_stat_line(x) for x in raw]
            if (
                len(raw) > 1
                and any(cls._signed_stat_line(x) for x in raw)
                and not (
                    len(raw) == 2
                    and re.fullmatch(r"[+−-]?\d+(?:\.\d+)?(?:%|m|米|秒)?", str(raw[1]).strip())
                )
            ):
                return [cls._signed_stat_line(x) or (None, None) for x in raw]
            pairs = [
                cls._structured_stat_pair(raw[i], raw[i + 1], bucket)
                for i in range(0, len(raw) - 1, 2)
            ]
            if len(raw) % 2:
                pairs.append((None, None))
            return pairs
        out: list = []
        for item in raw:
            if isinstance(item, dict):
                name_key = next((k for k in cls._STAT_NAME_KEYS if k in item), None)
                value_key = next((k for k in cls._STAT_VAL_KEYS if k in item), None)
                if name_key is not None and value_key is not None:
                    out.append(cls._structured_stat_pair(item[name_key], item[value_key], bucket))
                elif isinstance(item.get("text"), str) and isinstance(item.get("modifier"), dict):
                    modifier = item["modifier"]
                    key = next((k for k in cls._STAT_VAL_KEYS if k in modifier), None)
                    pair = cls._structured_stat_pair(item["text"], modifier.get(key), bucket)
                    modifier_name = next(
                        (modifier[k] for k in cls._STAT_NAME_KEYS if k in modifier), None
                    )
                    if modifier_name is not None and modifier_name != pair[0]:
                        pair = (item["text"], {"invalid_observation": modifier})
                    out.append(pair)
                else:
                    out.append((None, None))
            elif isinstance(item, (list, tuple)) and len(item) == 2:
                out.append(cls._structured_stat_pair(item[0], item[1], bucket))
            elif isinstance(item, str):
                out.append(cls._signed_stat_line(item) or (None, None))
            else:
                out.append((None, None))
        return out

    @staticmethod
    def _normalize_llm_stats(data: dict, rev: dict) -> tuple[list, list]:
        """LLM 提取结果 → 两组词条；未知值不补数，归一记录不改原始数组。"""
        recognition = data.setdefault("_recognition", {})
        if not isinstance(recognition, dict):
            recognition = data["_recognition"] = {}
        trace = recognition["stat_normalization"] = []

        def to_stat(name: str, val, bucket: str):
            record = {"raw_name": name, "raw_value": val, "input_bucket": bucket}
            trace.append(record)
            if name is None or val is None:
                record["decision"] = "unknown_name_or_value"
                return None
            name = str(name).strip().replace("%", "").replace("+", "").replace("-", "")
            evidence = {}
            sid = VisionCommands._stat_id_from_name(name, rev, evidence=evidence)
            record["name_match"] = evidence
            if not sid:
                record["decision"] = "unknown_or_ambiguous_name"
                return None
            try:
                decimal, multiplier = VisionCommands._stat_decimal(val, sid)
                num = float(decimal)
            except (TypeError, ValueError, InvalidOperation, OverflowError):
                record["decision"] = "invalid_value"
                return None
            if not math.isfinite(num):
                record["decision"] = "non_finite_value"
                return None
            value = (
                float((decimal - 1) * 100)
                if multiplier
                else VisionCommands._faction_val_fix(sid, num)
            )
            record.update(decision="accepted", sid=sid, numeric_value=num, converted_value=value)
            return (sid, value)

        pos: list[tuple[str, float]] = []
        neg: list[tuple[str, float]] = []
        try:  # 服务器以包成员加载，相对导入才可靠
            from .. import riven_analysis as _RA
        except ImportError:  # pragma: no cover - 本地直跑
            from core import riven_analysis as _RA

        # ★ 2026-09-24：卡面负词条常被 vision 整行归进 positive（实测
        #   「-63.4% 滑行攻击暴击几率」→ 4 正 0 负，整卡被词条数校验挡掉）。
        #   规则：**已经放在 negative 的照旧按负词条收**；放在 positive 但
        #   数值带负号的改判为负词条（magnitude 取绝对值）。
        # ★ 2026-10-02：反转词条（recoil）**以符号为准** —— 卡面「+95.4% 武器
        #   后坐力」是负面、「-20%」是正面（WM 1500 条实测，见 RA.INVERTED_STATS）；
        #   数组不作判据（线上实证：模型把 ±43 都塞进了 positive），prompt 已要求
        #   这行**按卡面原样保留正负号**。仅数值缺失（0）时退回数组。
        def _route(r, bucket: str):
            sid, num = r
            if _RA.is_negative_only(sid):
                # ★ 2026-10-02「仅负向」词条（连击获取，卡面恒为 -X%）：
                #   模型丢符号/放进正面槽时仍按负面收（wiki Legend ³）。
                neg_flag = True
            elif _RA.is_inverted(sid):
                neg_flag = (num > 0) if num else (bucket == "neg")
            else:
                neg_flag = (bucket == "neg") or (num < 0)
            (neg if neg_flag else pos).append((sid, abs(num)))
            trace[-1].update(output_bucket="negative" if neg_flag else "positive", value=abs(num))

        for _name, _val in VisionCommands._iter_stat_pairs(data.get("positive"), "positive"):
            r = to_stat(_name, _val, "positive")
            if r:
                _route(r, "pos")
        for _name, _val in VisionCommands._iter_stat_pairs(data.get("negative"), "negative"):
            r = to_stat(_name, _val, "negative")
            if r:
                _route(r, "neg")
        return pos, neg
