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
import json
from typing import Optional

from .. import loadout_ocr as lo
from .. import paths as core_paths
from .. import pips as pips_engine
from ..logging_compat import logger


def _prov_id(prov) -> str:
    """provider 的配置 id（竞速日志归因用，取不到返回 "?"）。

    ★ 2026-10-07：紫卡名被读成近形字（冰凇→冰松）却查不出**是哪个渠道读的** ——
    原日志只记「命中渠道（3108 ms）+ JSON」，渠道名没进日志。补上 id 后，
    任何一次错读都能直接归因到具体模型（8B / 30B / glm-4v-flash）。
    """
    try:
        return getattr(getattr(prov, "meta", lambda: None)(), "id", "") or "?"
    except Exception:  # noqa: BLE001 —— 取不到 id 绝不能影响识图
        return "?"


class VisionCommands:
    """Mixin：识别链设施（挂载于 main.WarframeSDJK；消费者为 riven 域与 scan 段）。"""

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
    # 2026-09-21 用户报障：13:43 那次识卡三渠道全废（glm-4v-flash 27 s 空返回，
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

    def _vision_providers(self) -> list:
        """按优先级返回可用 vision provider 列表（逐个尝试用）。

        ⚠️ 每一步独立容错：以前整段包一个 try/except: pass，任一步异常
        （例如按 id 取渠道时抛错）会**静默**返回空列表 —— 2026-09-17
        识卡全挂、日志里却一条渠道记录都没有，就是栽在这。
        """
        out: list = []

        def _try_get(pid: str):
            try:
                return self.context.get_provider_by_id(pid)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[sdjk] 取渠道 %s 失败：%s", pid, exc)
                return None

        # ① 面板里显式指定的（可指向任意多模态模型）
        want = ""
        try:
            want = str(self.cfg.get("vision_provider_id") or "").strip()
        except Exception:  # noqa: BLE001
            want = ""
        if want:
            prov = _try_get(want)
            if prov:
                out.append(prov)
            else:
                logger.warning("[sdjk] 配置的 vision_provider_id「%s」取不到，回落到内置候选", want)
        # ② 内置优先级候选
        for pid in self._VISION_PROVIDER_IDS:
            prov = _try_get(pid)
            if prov and not any(p is prov for p in out):
                out.append(prov)
        # ③ 兜底：扫描全部 provider 里 id 带视觉关键词的
        try:
            allp = self.context.get_all_providers() or []
        except Exception as exc:  # noqa: BLE001
            logger.warning("[sdjk] 枚举渠道失败：%s", exc)
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

    def _pick_vision_provider(self):
        """挑一个支持图片输入的 provider（找不到返回 None）。"""
        try:
            return self._vision_providers()[:1] or [None][0]
        except Exception:  # noqa: BLE001
            return None

    async def _image_data_urls(self, event) -> list[str]:
        """消息链里所有图片的 data URL。

        用 AstrBot 自带的 ``Image.convert_to_base64()``：统一处理 QQ 图床
        下载（gtimg 需要特定 UA/Referer，手写 httpx 会被 403）、本地路径、
        base64:// 三种来源。
        """
        out: list[str] = []
        try:
            chain = getattr(getattr(event, "message_obj", None), "message", None) or []
            for c in chain:
                if type(c).__name__ != "Image":
                    continue
                try:
                    b64 = await c.convert_to_base64()
                except Exception:  # noqa: BLE001 - 单张失败继续下一张
                    continue
                if b64 and len(b64) < 14 * 1024 * 1024:
                    out.append("data:image/png;base64," + b64)
        except Exception:  # noqa: BLE001 - 图片取不到就当没有
            pass
        return out[:1]  # 一次只认一张（紫卡截图）

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

            resp = await prov.text_chat(
                prompt=prompt,
                session_id=f"sdjk-{tag}-{_uuid.uuid4().hex[:8]}",
                image_urls=[image_url],
            )
            text = (getattr(resp, "completion_text", "") or "").strip()
            return prov, text, (_t.perf_counter() - _t0) * 1000

        tasks = [asyncio.create_task(_one(p)) for p in provs]
        loop = asyncio.get_event_loop()
        deadline = (loop.time() + window) if window else None
        winner = None
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
                    try:
                        prov, text, ms = t.result()
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("[sdjk] %s渠道异常：%s", tag, exc)
                        continue
                    if not text:
                        logger.warning(
                            "[sdjk] %s渠道返回空（%.0f ms，渠道 %s）", tag, ms, _prov_id(prov)
                        )
                        continue
                    data = (parse or lo.parse_vision_json)(text)
                    # ★ 采信判据（2026-10-05 收紧）：dict 里**至少一个有效字段**。
                    #   「解析成功但字段全 None/空」的快渠道不许赢 —— 否则会抢在
                    #   「慢但完整」的渠道前面被采信，weapon/词条全部落空
                    #   （识图测试卡 6 实证：JSON 少一个 ] 时 30B 吐全 None 结构）。
                    if data and any(
                        data.get(k)
                        for k in ("weapon", "mods", "panel", "positive", "negative", "lines")
                    ):
                        logger.info(
                            "[sdjk] %s命中渠道（%.0f ms，渠道 %s）：%s",
                            tag,
                            ms,
                            _prov_id(prov),
                            json.dumps(data, ensure_ascii=False)[:200],
                        )
                        winner = data
                        break
                    logger.warning(
                        "[sdjk] %s JSON 解析失败（%.0f ms，渠道 %s）：%s",
                        tag,
                        ms,
                        _prov_id(prov),
                        text[:160],
                    )
                if winner:
                    break
        finally:
            for t in tasks:
                t.cancel()
        return winner

    # ★ 窄读提示词（2026-09-27）：只要卡面词条行原文，不要 JSON/解释/武器名。
    #   实测（用户那张 321×450 的卡 ×3 次）：同一条渠道用完整 JSON 提示词稳定
    #   只照抄 2/4 行（**放大图片也救不了**），换这条窄提示词后三个渠道 6/6 全对。
    #   ⇒ 词条/极性一律以窄读为准，JSON 那路只负责武器名与兜底。
    # ★★ 2026-10-05 二次精简（213→60 字，S 系列探针 + S4/S5/S6 全量对照）：
    #   · **提问式 + 极简**胜过一切长约束：8B 在 213 字版下 11 个版次全漏
    #     `🔒+59% 多重射击`（05 卡），换本版后 47/48 读到；
    #   · 中性探针「这张卡面上有几行词条？逐行念出来」→ 8B 5/5 完整念出锁行
    #     ⇒ **不是看不见、不是分辨率**，是长提示词的约束框把它筛掉了；
    #   · ★ **「卡面上没有的行绝对不要编造」是副作用源**：S5 只是把这句话加回
    #     S4，8B 立刻复现 05 漏锁行（23/24）、30B 输出「第一行：」前缀 3/8 被拒。
    #     ⇒ 该句**不得**再加回来（它防的幻觉来自「x1.51」字面示例，示例已删）。
    #   · ★ 纯格式约束（不要编号/列表符号）收掉 markdown 噪声：S4 3 次被拒 → S6 0 次。
    #   · 实测（16 卡 × 2 模型 × 3 轮 = 96）：全对 94、**危险 0**、被拒 0（2 次 API 失败）；
    #     对照 F 版 60/64、**危险 3**。每请求省 128 input tokens（218 vs 346）。
    _RIVEN_LINE_PROMPT = (
        "这张卡面上有几行词条？逐行念出来，只念词条行（卡名、段位不用念）。"
        "直接逐行输出，不要编号、不要列表符号、不要任何说明文字。"
    )

    @classmethod
    def _riven_lines_legal(cls, lines) -> tuple:
        """卡面原文行 → (pos, neg, 是否合法卡面, 备注)。

        合法 = 2~3 条正面 + ≤1 条负面 **且没有「像是词条行却没解析成功」的行**。
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
        pos, neg, notes = RA.parse_riven_lines(lines, lambda nm: cls._stat_id_from_name(nm, rev))
        fatal = [n for n in notes if not n.startswith("无极性符号")]
        return pos, neg, (2 <= len(pos) <= 3 and len(neg) <= 1 and not fatal), notes

    @classmethod
    def _parse_riven_lines_text(cls, text: str) -> dict:
        """窄读回答（纯文本）→ {"lines": [...]}；卡面不合法则返回 {} 让位下一个渠道。

        「能解析」不等于「读全了」：模型漏一行时整卡就落在非法区间，所以这里
        用 `_riven_lines_legal` 当判据，宁可多等一个渠道也不采信残缺结果。
        """
        lines = [ln.strip() for ln in str(text or "").splitlines() if ln.strip()]
        pos, neg, legal, notes = cls._riven_lines_legal(lines)
        logger.info(
            "[sdjk] 紫卡行读候选：%d 正 %d 负（%s）%s",
            len(pos),
            len(neg),
            "采信" if legal else "不合法，继续等",
            ("；跳过 " + " / ".join(notes[:4])) if notes else "",
        )
        return {"lines": lines} if legal else {}

    async def _extract_riven_from_image(self, image_url: str) -> Optional[dict]:
        """调 vision 渠道从紫卡截图提取词条，返回解析后的 dict 或 None。

        ★ 两路并行（2026-09-27）：
          · 窄读词条行 → 词条与极性的**唯一权威来源**；
          · 语义 JSON → 武器名（含变体前缀），并在窄读失败时兜底词条。
        两路都失败才返回 None（调用方给「重发一次」的提示）。
        """
        # ★★ 2026-10-05 去掉全部字面示例值（实测 30B 真泄漏 10/32：把例子里的
        #   数值照抄到不含该值的卡上）。旧例子 50.2/65.1/44.9/95.4/63.4/115.7/110.8
        #   全部来自用户真实卡 ⇒ 本提示词不得再出现任何具体数值，一律用公式/描述表达。
        prompt = '你是 Warframe 紫卡识别器。从这张紫卡截图中提取信息，只输出一行 JSON（不要 markdown 围栏、不要解释）：\n{"weapon": "武器名（卡面简中，逐字照抄第一行的中文部分）", "positive": [["词条缩写", 数值], ...], "negative": [["词条缩写", 数值], ...]}\n词条名**照卡面原样照抄**：不要替换成其它词条名，也不要把认不准的词猜成熟悉的词条（实测教训：把「弹匣容量」猜成「暴伤」、把「变焦」猜成「集束」，整卡判据全错）。\n常见词条：基伤/暴伤/暴击/攻速/范围/多重/触发/持续/效率/装填/弹速/滑暴/冲击/穿刺/切割/电击/火焰/冰冻/毒素/磁力/辐射/**弹匣容量**/**变焦**/后坐力/**病毒伤害**/**腐蚀伤害**/**毒气伤害**/**爆炸伤害**/**磁力伤害**/**辐射伤害**/弱点伤害/弱点暴击/状态伤害/弹药效率/收枪装填/重击伤害/重击蓄力/格挡角度/震地伤害/对Orokin·Scaldra·Techrot伤害等（含 Update 44「Riven Splicer」拼接词条，卡面写什么就照抄什么）——「+x% 弹匣容量」就写 弹匣容量、「-x% 变焦」就写 变焦（两者都是真实存在的词条）。\n⚠ **负词条绝不能丢**：卡面通常 3~4 条，少一条整卡判据就废；负词条也放 negative。数值只写数字（去掉 % 和 m 单位）。\n⚠ 「对 Grineer/Corpus/Infested 的伤害」在卡面上写的是**乘数**（净伤害倍率），必须先换算成百分数再填：\n    倍率 > 1 ⇒ 加伤，填 positive，数值 = (倍率 − 1) × 100\n    倍率 < 1 ⇒ 减伤，填 negative，数值 = (1 − 倍率) × 100\n  词条名用「对Infested伤害」（或 Grineer/Corpus）。**不要**把倍率直接乘 100 当百分数（那是错的），也不要漏掉这一条。卡面右下角的数字是内融值，与倾向无关，不要输出倾向。⚠ weapon 必须**逐字照抄卡面第一行**，变体前缀一个不漏：赤毒/信条/终幕/棱晶/Prime/亡魂/破坏者（或 Kuva/Tenet/Coda/Prisma/Wraith/Vandal）。卡面写了前缀就**必须**带上，漏掉前缀会让倾向取值整体错档（差数倍），整卡区间全错。（紫卡卡面通常只写母武器名，没有前缀才照原样输出）。\n⚠ **不要改字**：带元素符号的那行（⚡/☠/❄ 等）必须写对应的元素名（电击/火焰/冰冻/毒素/磁力/辐射），不得写成「暴击伤害」（元素伤害与暴击伤害是不同词条）；同一条词条在卡上**不会出现两次**。\n⚠ 词条数值**带负号**的（卡面行首是「-」），必须放进 negative，数值写**正数**（去掉负号）—— 放进 positive 会让整张卡被判成「词条数不对」而失败。\n⚠ 例外：**武器后坐力**的符号与好坏相反 —— 卡面带「+」是**负面**（后坐力越大越差）、带「-」是**正面**。这一行请**按卡面原样保留正负号**（别把负号丢掉），放在 positive 或 negative 数组都不影响（机器人以符号为准自行换算）。\n⚠ weapon 只填**中文武器名**（卡面第一行的中文部分）。名字后面那串拉丁文是紫卡自命名（形如 Xxx-xxxxxx），不要输出它、也不要把它音译成中文，更不要把词条名混进 weapon。\n⚠ 组合枪/魔典（Kitgun/Zaw/Amp 类模块化武器）的紫卡卡面第一行写的是**腔体/部件中文名**+ 空格 + 拉丁自命名 —— weapon 只填腔体中文名，自命名照旧不要输出。腔体名后**没有**主要/次要模式标注，不要自行加「（主要）/（次要）」后缀，机器人会按卡面数值判定模式。若第一行确实没有任何中文武器名，weapon 才输出空字符串 ""。\n⚠ 数值要连**小数点**一起读：卡面写的小数点不能丢（丢掉会让数值差 10 倍）。点号看不清时宁可按最接近的两位有效数字估，也不要直接丢掉点号。'
        provs = self._vision_providers()
        # ★ 2026-10-07（用户裁示）：紫卡这一路**不竞速，固定用 8B**。
        #   实测（同一张「冰凇」卡 × 各 5 次）：`zhipu/glm-4v-flash` 把「冰凇」
        #   **稳定**读成「冰松」（5/5 全错）且它最快（~3.1 s）⇒ k=2 竞速里它必赢，
        #   线上 10-07 10:59 / 17:11 / 17:13 三次「冰松」全是它读的（3.1 s 吻合）；
        #   30B 只有 2/5 读对；**8B 5/5 全对**。武器名是整卡的承重件 ⇒ 宁慢不赌：
        #   只取一个渠道（优先 8B，其次配置渠道/第一个可用），`k=2` 自然退化为单发。
        #   ⚠ 识卡（配卡截图）那一路**保留**原竞速：那里用真实配卡对拍，glm 是
        #   7/7 全对且要速度（见 `_VISION_PROVIDER_IDS` 上方的 2026-09-20 记录）。
        _all_provs = self._vision_providers()
        provs = [p for p in _all_provs if "qwen3-vl-8b" in _prov_id(p).lower()][:1] or _all_provs[:1]
        if provs:
            logger.info("[sdjk] 紫卡识别固定渠道：%s（不竞速）", _prov_id(provs[0]))
        try:
            # 并行竞速（原来串行试渠道，实测要 13 s）；窗口 60s（2026-09-23 补：
            # 渠道级超时可达 240s，不能让用户干等）
            data, lines = await asyncio.gather(
                self._vision_race_json(
                    prompt, image_url, provs, k=2, tag="紫卡识别", window=self.RACE_WINDOW_S
                ),
                self._vision_race_json(
                    self._RIVEN_LINE_PROMPT,
                    image_url,
                    provs,
                    k=2,
                    tag="紫卡行读",
                    window=self.RACE_WINDOW_S,
                    parse=self._parse_riven_lines_text,
                ),
                return_exceptions=True,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[sdjk] 紫卡识别失败：%s", exc)
            return None
        for name, val in (("识别", data), ("行读", lines)):
            if isinstance(val, BaseException):
                logger.warning("[sdjk] 紫卡%s异常：%s", name, val)
        data = None if isinstance(data, BaseException) else data
        lines = None if isinstance(lines, BaseException) else lines
        if not data and not lines:
            logger.warning("[sdjk] 紫卡识别：两路都没给出可用结果")
            return None
        out = dict(data or {})
        if lines:
            out["lines"] = list(lines.get("lines") or [])
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
            logger.warning("[sdjk] 豆子检测失败（不影响识卡）：%s", exc)
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
            logger.debug("[sdjk] 存排查图失败：%s", exc)

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
    def _stat_id_from_name(cls, name: str, rev: dict) -> "str | None":
        """词条名（缩写 / 全称 / 卡面原文）→ 标准词条 id；认不出返回 None。

        两条路径共用（截图识别 `_normalize_llm_stats` 与文字输入）：
        全称整表命中 → 展示名 → 别名归一 → 包含匹配（长名优先）→ 形近。
        ★ 2026-09-24：卡面原文「滑行攻击暴击几率」不含短名「滑暴」子串，
        包含匹配会落到「暴击」，必须靠别名整表（见 parser.RIVEN_STAT_ALIASES）。
        """
        if not name:
            return None
        import difflib

        name = cls._STAT_ALIAS.get(name, name)
        full = cls._full_stat_alias()
        if name in full:  # 全称整表命中（暴击伤害 → crit_damage）
            return full[name]
        if name in rev:
            return rev[name]
        # 包含匹配：长名优先，避免短名抢走全称（「暴击」vs「暴击伤害」）
        for abbr in sorted(rev, key=len, reverse=True):
            if name in abbr or abbr in name:
                return rev[abbr]
        # ★ 2026-10-02 形近容错（线上实证：OCR 把「触发几率」读成「脆发几率」
        #   被整行跳过，卡面缺一条正词条）。旧实现只在**短名表**（rev）上找、
        #   且 cutoff 0.5 —— 「触发几率」是**全称表**的键，永远命中不了。
        #   现在：[全称 + 短名] 并集上找，且必须**高置信**（相似度 ≥0.7 且与
        #   次名差距 ≥0.1）才采纳；采纳时写日志（红线：禁静默改判）。
        pool: dict = {}
        for tbl in (full, rev):
            for k, v in tbl.items():
                pool.setdefault(k, v)
        close = difflib.get_close_matches(name, list(pool), n=2, cutoff=0.7)
        if close:
            best = close[0]
            r1 = difflib.SequenceMatcher(None, name, best).ratio()
            r2 = difflib.SequenceMatcher(None, name, close[1]).ratio() if len(close) > 1 else 0.0
            if r1 >= 0.7 and r1 - r2 >= 0.1:
                logger.info("[sdjk] 紫卡词条名容错：%s → %s（相似度 %.2f）", name, best, r1)
                return pool[best]
        return None

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

    @staticmethod
    def _normalize_llm_stats(data: dict, rev: dict) -> tuple[list, list]:
        """LLM 提取结果 → ([(stat_id, float)...], [...])；词条名宽松匹配。"""

        def to_stat(name: str, val):
            if name is None or val is None:
                return None
            name = str(name).strip().replace("%", "").replace("+", "").replace("-", "")
            try:
                num = float(str(val).strip().rstrip("%m米"))
            except (TypeError, ValueError):
                return None
            sid = VisionCommands._stat_id_from_name(name, rev)
            if not sid:
                return None
            return (sid, VisionCommands._faction_val_fix(sid, num))

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

        for item in data.get("positive") or []:
            r = to_stat(*item)
            if r:
                _route(r, "pos")
        for item in data.get("negative") or []:
            r = to_stat(*item)
            if r:
                _route(r, "neg")
        return pos, neg
