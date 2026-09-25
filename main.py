# -*- coding: utf-8 -*-
"""Warframe SDJKBOT —— AstrBot 插件入口。

无缝配合 NapCat (OneBot v11) 上游运行；指令解析为**自由参数**式：
【主指令 内容 附加指令】位置任意、空格分隔，通用修饰符可叠加
（-pc/-ps/-xb/-sw、-1/-w 文字、-t 图片、-N 翻页、-r 密语）。

架构分层：
  parser.py   自由参数解析（无序指令 + 修饰符 + 词条连写分词）
  api_client  异步数据客户端（世界状态 / WM 市场 / TTL 缓存）
  formatters  中文文本格式化
  render      文本卡片 / Pillow 图片卡片
  push        蹲订阅差量推送守护协程
  main.py     AstrBot 事件响应与指令分发（本文件）
"""
from __future__ import annotations

import asyncio
import difflib
import json
import re
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Image, Plain
from astrbot.api.star import Context, Star, register

try:  # 允许脱离 AstrBot 直接跑单元测试
    from .core import __brand__ as BRAND
    from .core import api_client
    from .core import calculators as calc
    from .core import damage_calc as dc
    from .core import loadout_ocr as lo
    from .core import matching
    from .core import pips as pips_engine
    from .core.api_client import (WarframeAPIError, WarframeClient,
                              parse_url_list, fuzzy_hits)
    from .core import arbi as _arbi
    from .core import baro
    from .core import drops as drops_db
    from .core import paths as core_paths
    from .core import formatters as fmt
    from .core import search as search_engine
    from .core import wiki_intro
    from .core.parser import (Parsed, normalize_platform, parse, parse_duration,
                              parse_fissure_filter, parse_time_window, parse_wm,
                              parse_wr, PLATFORM_DISPLAY)
    from .core.push import PUSH_EVENTS, PushDaemon, build_cancel_selector, normalize_event
    from .core.render import ImageRenderer, WATERMARK_VERSION, text_card
    from .core.store import GroupStore, Subscription, SubscriptionStore
    from .core import de_worldstate as de_ws
except ImportError:  # pragma: no cover
    from core import __brand__ as BRAND
    from core import api_client
    from core import calculators as calc
    from core import damage_calc as dc
    from core import loadout_ocr as lo
    from core import matching
    from core import pips as pips_engine
    from core import de_worldstate as de_ws
    from core.api_client import (WarframeAPIError, WarframeClient,
                             parse_url_list, fuzzy_hits)
    from core import arbi as _arbi
    from core import baro
    from core import drops as drops_db
    from core import paths as core_paths
    from core import formatters as fmt
    from core import search as search_engine
    from core import wiki_intro
    from core.parser import (Parsed, normalize_platform, parse, parse_duration,
                             parse_fissure_filter, parse_time_window, parse_wm,
                             parse_wr, PLATFORM_DISPLAY)
    from core.push import PUSH_EVENTS, PushDaemon, build_cancel_selector, normalize_event
    from core.render import ImageRenderer, WATERMARK_VERSION, text_card
    from core.store import GroupStore, Subscription, SubscriptionStore
    from core import de_worldstate as de_ws


def _relic_tier_en() -> dict:
    """中文档位 → 掉落表英文键（小写）。真源 core/parser.TIER_CN（含先锋/全能）。

    ★ 遗物相关的档位表**统一走这里**——曾在 3 处各自硬编（_norm_relic / 列表卡 /
    单查状态），2026-09 新增「先锋」档时全漏，用户查「遗物 先锋 C1」显示未找到。
    """
    from core.parser import TIER_CN
    return {k: v.lower() for k, v in TIER_CN.items() if not k.isascii()}


def _llm_request_hook():
    """LLM 请求钩子装饰器；测试桩/旧版 AstrBot 没有该钩子时退化为空装饰器。"""
    deco = getattr(filter, "on_llm_request", None)
    if deco is None:
        return lambda fn: fn
    try:
        return deco()
    except Exception:  # noqa: BLE001
        return lambda fn: fn


def _result_hook():
    """发送前结果钩子装饰器；旧版 AstrBot 没有该钩子时退化为空装饰器。

    用途：LLM 回复在交给 QQ 适配器前，剥掉 QQ 无法渲染的 Markdown 标记。
    这是「人格层禁令」之外的兜底 —— 人格可能被改坏或只是偶尔不听话，
    这一层保证无论如何都不会有裸星号出现在群里。
    """
    deco = getattr(filter, "on_decorating_result", None)
    if deco is None:
        return lambda fn: fn
    try:
        return deco()
    except Exception:  # noqa: BLE001
        return lambda fn: fn


# ---------------------------------------------------------------------------
# 指令空间避让（2026-09-19 修「/help / /新闻 被本插件吞掉」线上事故）
# ---------------------------------------------------------------------------
# AstrBot 的 WakingCheckStage 会把 wake_prefix（默认 "/"）**从 message_str 上剥掉**
# （astrbot/core/pipeline/waking_check/stage.py: `event.message_str = event.message_str[len(wake_prefix):]`），
# 所以到了 handler 这一层 "/新闻" 已经变成 "新闻" —— `event.message_str.startswith("/")`
# **永远不可能命中**，靠它挡前缀是无效的。
#
# 后果：本插件 143 个触发词全都能被 "/触发词" 命中；而本插件的 handler 是
# `event_message_type(ALL)`，注册顺序又靠前（插件按加载顺序注册，内置指令
# builtin_commands 与后装的插件都排在后面），处理完还调 `event.stop_event()`，
# 于是后面的处理器被整体跳过（process_stage/method/star_request.py:
# `for handler in activated_handlers: if event.is_stopped(): break`）。
#
# 线上实证（2026-09-19 日志）：
#   19:30:03 用户发 /help  → 渲染的是本插件的「指令一览」（AstrBot 内置帮助被打不开）
#   19:52:16 用户发 /新闻  → 渲染的是本插件的「最近新闻」（dailyhub 的 /新闻 被打不开）
#   同一时刻别的插件打印 preview='新闻'，即前缀已被剥掉 —— 坐实机制。
#
# 修法：**只对「带唤醒前缀 且 该词已被内置/其他插件占用」的输入让路**
# （直接 return，**不** stop_event，让后面的处理器照常接住）。
#   * "/help"、"/新闻" → 让给内置 / dailyhub
#   * "/仲裁"、"/赏金" → 没被占用，照常由本插件响应（不破坏用户习惯）
#   * 裸词 "仲裁" / "新闻" → 本来就没有别的处理器在抢（指令过滤需要前缀），维持原样
_OCCUPIED_FALLBACK = frozenset({
    # AstrBot 内置指令（astrbot/builtin_stars/builtin_commands/main.py）
    "help", "sid", "name", "reset", "stop", "new", "stats", "provider",
    "dashboard_update", "set", "unset",
    # 已知的第三方占用（本机实测）。运行期探测失败时靠它兜底 ——
    # 宁可少避让，也不能把内置帮助这种刚需指令再吞一次。
    "新闻", "news",
})
_OCCUPIED_CACHE: Optional[frozenset] = None
_OCCUPIED_EPOCH: int = -1


def _occupied_command_words() -> frozenset:
    """「已被内置指令 / 其他插件占用」的指令首词集合。

    从 AstrBot 的 handler 注册表读，**按注册表规模做缓存键** —— 装了新插件 /
    卸载 / 重载都会让规模变化，于是自动重扫（今天的 dailyhub 就是运行期新装的：
    固定缓存会让它一直被吞）。任何异常都不许向上抛：探测失败最多少避让，
    不能让消息处理整体挂掉。
    """
    global _OCCUPIED_CACHE, _OCCUPIED_EPOCH
    registry = None
    epoch = -1
    try:
        from astrbot.core.star.star_handler import star_handlers_registry

        registry = star_handlers_registry
        epoch = len(registry)
    except Exception:  # noqa: BLE001
        pass
    if _OCCUPIED_CACHE is not None and epoch == _OCCUPIED_EPOCH:
        return _OCCUPIED_CACHE

    words: set[str] = set(_OCCUPIED_FALLBACK)
    try:
        from astrbot.core.star.filter.command import CommandFilter

        for handler in registry:
            module_path = getattr(handler, "handler_module_path", "") or ""
            if "astrbot_plugin_warframe" in module_path:
                continue                      # 跳过自己，只收集「别人的」
            for flt in getattr(handler, "event_filters", None) or []:
                if not isinstance(flt, CommandFilter):
                    continue
                for name in (flt.command_name, *(flt.alias or ())):
                    if name:
                        # 指令组是「父 子」两级，用户实际敲的是首词
                        words.add(str(name).split()[0].lower())
    except Exception:  # noqa: BLE001
        pass
    _OCCUPIED_CACHE = frozenset(words)
    _OCCUPIED_EPOCH = epoch
    return _OCCUPIED_CACHE


def _raw_candidates(event: AstrMessageEvent) -> list[str]:
    """尽可能还原「未被 AstrBot 改写」的原始消息文本（多个来源，谁有用谁）。

    来源 1：`message_obj.message_str` —— aiocqhttp 适配器把**含前缀**的原始串
            写在这里，且 WakingCheckStage 只改 `event.message_str`，不动它。
    来源 2：消息段里的 Plain 文本拼回来 —— 适配器不填 message_obj.message_str
            时（webchat 等）的退路；@机器人 的首个 At 段不是 Plain，天然被跳过。
    """
    out: list[str] = []
    obj = getattr(event, "message_obj", None)
    raw = getattr(obj, "message_str", None) if obj is not None else None
    if isinstance(raw, str) and raw:
        out.append(raw)
    try:
        parts = [getattr(seg, "text", "") for seg in event.get_messages()
                 if isinstance(seg, Plain) and getattr(seg, "text", None)]
        if parts:
            out.append("".join(parts))
    except Exception:  # noqa: BLE001
        pass
    return out


def _wake_prefix_stripped(event: AstrMessageEvent) -> bool:
    """这条消息的 wake_prefix 是否已被 AstrBot 剥掉（即用户是带着 "/" 发的）。

    判据：某个原始文本比 `event.message_str` 长，且以它结尾 —— 多出来的那一截
    只可能是被剥掉的唤醒前缀。@机器人 的形态两边相同，不会误判。
    拿不到任何原始文本（未知适配器 / 测试桩）时按「无前缀」处理（fail-open）。
    """
    cur = (getattr(event, "message_str", "") or "").strip()
    if not cur:
        return False
    for raw in _raw_candidates(event):
        raw_s = raw.strip()
        if len(raw_s) > len(cur) and raw_s.endswith(cur):
            return True
    return False


# ---------------------------------------------------------------------------
# Markdown 兜底清理
# ---------------------------------------------------------------------------
# QQ（aiocqhttp / NapCat）协议不支持任何富文本，Markdown 标记会被原样显示成
# 裸符号（**加粗**、- 列表、# 标题），是群聊里的明显事故。人格提示词已加禁令，
# 这里是最后一道防线。只处理**文本组件**，绝不碰图片 / Pillow 渲染路径。
_MD_BOLD = re.compile(r"\*\*(.+?)\*\*", re.S)
_MD_ITALIC = re.compile(r"(?<!\*)\*(?!\s)([^*\n]+?)(?<!\s)\*(?!\*)")
_MD_HEAD = re.compile(r"(?m)^\s{0,3}#{1,6}\s*")
_MD_QUOTE = re.compile(r"(?m)^\s{0,3}>\s?")
_MD_LIST = re.compile(r"(?m)^(\s*)[-*+]\s+")
_MD_OLIST = re.compile(r"(?m)^(\s*)\d+[.)]\s+")
_MD_CODE = re.compile(r"`{1,3}")
_MD_HR = re.compile(r"(?m)^\s{0,3}([-*_])\s*\1\s*\1[\s\-*_]*$")


def strip_md(text: str) -> str:
    """剥掉 QQ 无法渲染的 Markdown 标记。

    注意：括号动作（`（指尖轻点桌面）`）属于**语义问题**，正则无法安全区分
    它与「（Rank 30）」这类正常术语补充，因此不在此处处理 —— 那一层由人格
    提示词的禁令负责。
    """
    if not text or not isinstance(text, str):
        return text
    t = text
    t = _MD_HR.sub("", t)          # 分隔线 --- / ***
    t = _MD_BOLD.sub(r"\1", t)     # **加粗** → 加粗
    t = _MD_ITALIC.sub(r"\1", t)   # *斜体* → 斜体
    t = _MD_HEAD.sub("", t)        # ## 标题 → 标题
    t = _MD_QUOTE.sub("", t)       # > 引用 → 引用
    t = _MD_LIST.sub(r"\1· ", t)   # - 列表 → · 列表（保留视觉分段）
    t = _MD_OLIST.sub(r"\1", t)    # 1. 列表 → 列表
    t = _MD_CODE.sub("", t)        # 行内代码 / 围栏
    # 清理因剥离产生的多余空行
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip() if t.strip() else text


PLUGIN_DIR = Path(__file__).resolve().parent
PLUGIN_NAME = "astrbot_plugin_warframe"   # 插件身份（须与 metadata.yaml.name 一致）
ROTATION_FILE = Path(__file__).resolve().parent / "core" / "data" / "rotations.json"
JUNK_FILE = Path(__file__).resolve().parent / "core" / "data" / "junk.json"


def _resolve_data_dir() -> Path:
    """插件运行期数据目录（规范：持久化数据必须放 ``data/plugin_data/<插件名>``）。

    插件包目录对安装用户是只读的（升级还会整包覆盖），所以运行期**不能**往包内
    写任何用户数据。优先走 AstrBot 官方 API（``StarTools.get_data_dir``）；脱离
    AstrBot 运行（离线单测 / 独立脚本）时退回系统临时目录，同样不碰插件包目录。
    """
    try:
        from astrbot.core.star.star_tools import StarTools
        return Path(StarTools.get_data_dir(PLUGIN_NAME))
    except Exception:  # noqa: BLE001 - 无 astrbot 环境（离线脚本 / 单测）
        d = Path(tempfile.gettempdir()) / PLUGIN_NAME
        d.mkdir(parents=True, exist_ok=True)
        return d


def _migrate_legacy_user_data(data_dir: Path) -> None:
    """**一次性**兼容旧版本：把早先写在插件目录 ``runtime/`` 里的用户数据搬到新目录。

    只读旧文件，且只在新目录缺同一份数据时复制；此后运行期不再往插件目录写任何
    东西（插件包目录只读，写进去一升级就丢）。
    """
    legacy = PLUGIN_DIR / "runtime"
    if not legacy.is_dir():
        return
    for name in ("groups.json", "subscriptions.json"):
        src, dst = legacy / name, data_dir / name
        if src.is_file() and not dst.exists():
            try:
                shutil.copy2(src, dst)
            except OSError:
                pass
    src_cards, dst_cards = legacy / "cards", data_dir / "cards"
    if src_cards.is_dir() and not dst_cards.exists():
        try:
            shutil.copytree(src_cards, dst_cards)
        except OSError:
            pass


def _num(cfg: dict, key: str, default: float, cast=float) -> float:
    """面板数值容错读取（BUG-2，2026-09-18 验收）。

    面板 schema 只在 UI 层约束类型，用户手改配置 JSON 可绕过 —— 原来直接
    float()/int() 会让插件 __init__ 抛 ValueError 加载失败。非法值回退默认
    并记 warning；空串 / None 同样视为未填写（回退默认）；0 是合法值
    （scan_cooldown=0 关闭限制等语义不受影响）。
    """
    raw = cfg.get(key, default)
    if isinstance(raw, str) and not raw.strip():
        return default
    try:
        return cast(raw)
    except (TypeError, ValueError):
        logger.warning("[sdjk] 配置项 %s=%r 非法，回退默认值 %s",
                       key, raw, default)
        return default

# 指令一览：(指令写法, 说明) —— 渲染时按全角空格分成两列对齐。
#
# ★ 两条硬规则（2026-09-17 改版，用户反馈「主指令乱排 / 缺指令 / 看不懂」）：
#   1. 左列必须是**真实主指令名**（可照发）＋常用别名，不再是「平台切换」这类
#      描述性标签 —— 用户是照着这行去发指令的。
#   2. **54 个主指令必须全部出现**，由 tests/test_help_coverage.py 锁住，
#      以后新增主指令而忘了写进帮助会直接测试失败。
#   分组依据是「玩家在什么场景下会想用它」，同类指令聚在一组。
def _xh_element(toks: list[str]) -> tuple[Optional[str], Optional[str]]:
    """从参数里认元素，返回 (中文名, WM 英文值)。

    认中英文全称与常见单字/简写（辐射 / radiation / 辐）；认不出返回 (None, None)。
    """
    for t in toks:
        s = (t or "").strip().lower()
        if s in fmt.LICH_ELEM_EN:                 # 中文全称
            return s, fmt.LICH_ELEM_EN[s]
        if s in fmt.LICH_ELEM_CN:                 # 英文
            return fmt.LICH_ELEM_CN[s], s
        if s in fmt.LICH_ELEM_ALT:                # 单字 / 简写
            cn = fmt.LICH_ELEM_ALT[s]
            return cn, fmt.LICH_ELEM_EN[cn]
    return None, None


HELP_TOPIC: dict[str, list[tuple[str, str]]] = {
    "用法速查": [
        ("帮助 / help", "本页指令总览"),
        ("状态", "运行状态、版本与订阅数（需群管理员）"),
        ("平台 -pc / -ps / -xb / -sw", "四平台数据已互通，统一显示「国际服」"),
        ("输出 -1 / -w ｜ -t ｜ -r", "纯文字 ｜ 强制图片 ｜ 生成密语"),
        ("翻页 -2 / -3", "看第 2/3 页（列表类指令通用）"),
        ("群管理", "点号开头（需群管理员）：.默认平台 / .开启 / .关闭 推送 / .状态"),
    ],
    "周期与日常": [
        ("夜灵 / 平原时间", "夜灵·金星·魔胎·地球·双衍·扎里曼 周期轮换"),
        ("赏金", "各地区赏金轮次与奖励"),
        ("裂隙", "虚空裂隙；可筛 钢铁 / 任务类型 / 等级"),
        ("突击 / 执刑官", "每日突击三阶段｜本周执刑官猎杀"),
        ("时效 / 周报", "各周期内容剩余时间总览"),
    ],
    "任务与轮换": [
        ("深层科研 / 时光科研", "本周科研：3 任务 × 普通 / 硬化"),
        ("沉沦之地 / 炼狱塔", "本周 21 层任务与复杂化"),
        ("侵袭", "钢铁之路今日侵袭节点"),
        ("仲裁 / 仲裁表", "当前与未来场次｜整周排期（可筛类型）"),
        ("警报 / 入侵", "进行中的警报与入侵进度"),
        ("九重天 / 虚空风暴", "航道星舰任务与奖励"),
        ("活动 / 武形秘仪", "限时活动｜PVP 挑战"),
    ],
    "商店与周常": [
        ("奸商 / 虚空商人", "Baro Ki'Teer 库存与抵达时间"),
        ("每日特惠 / 商城折扣", "每日折扣商品｜限时礼包"),
        ("言录使", "本周周常商品（苦栓结算）"),
        ("氏族奖励", "氏族研究进度"),
        ("出库 / 阿耶", "Prime 出库清单｜御品阿耶兑换"),
        ("灵化轮换", "钢铁回廊每周灵化适配器"),
        ("终幕 / 信条", "每周轮换武器与元素加成"),
        ("舰队进度", "巴罗尔巨人战舰 / 利刃豺狼舰队"),
    ],
    "资料与进度": [
        ("新闻 / 最近更新", "官方公告与热修"),
        ("电波 / 午夜电波", "本周挑战与等级"),
        ("日历", "1999 日历：当前季与日程"),
        ("结合目标", "Simaris 结合仪式目标与出处"),
        ("wiki 关键词", "词库直达维基页面"),
        ("赤毒 / 钢铁之路", "赤毒历史｜Teshin 荣誉商店"),
        ("对话助手 话题", "剧情与角色问答"),
    ],
    "遗物与资源": [
        ("遗物 / 核桃", "奖励与出处；出库 / 入库 / 列表"),
        ("开核桃", "遗物收益筛选：速刷|全部 低价|高价"),
        ("部件 物品名", "Prime 部件与蓝图出处"),
        ("金垃圾 / 银垃圾 / 铜垃圾", "按杜卡德价值分档清单"),
    ],
    "伤害与配卡": [
        ("伤害 武器 对 敌人 100级 膛线", "面板/单发/DPS/含段合计；可加 爆头 病毒10 剥甲 满镀层 钢路"),
        ("灵化 / 基础形态", "有灵化形态的武器默认按灵化算（MOD 照常叠算）"),
        ("空战 / 地面", "空战枪双部署切换"),
        ("进化 基伤/暴击/爆头…", "灵化进化选项，可多次，改基础面板"),
        ("赤毒辐射60", "玄骸/姐妹武器回响加成（25~60%）"),
        ("识卡 ＋ 配卡截图", "读升级界面：武器/卡片/等级/面板校验"),
        ("识卡伤害 对 重机枪手 150级", "复用最近识卡结果换敌人/等级复算"),
        ("武器融合 电60 火58", "玄骸/姐妹武器效价融合"),
        ("玄骸 / xh", "玄骸与姐妹武器查询"),
    ],
    "紫卡与市场": [
        ("紫卡分析 武器名 词条数值", "词条区间与距中；负词条加「负」"),
        ("倾向 武器名", "紫卡倾向值"),
        ("wr / 紫卡 武器名", "紫卡拍卖：词条·洗数·极性筛选"),
        ("rm 武器名", "源紫卡报价"),
        ("紫卡排行 / 排行", "热度榜；「紫卡排行 刷新」强更"),
        ("wm 物品名", "在售/收购；部件查单加 蓝图/机体/系统/头部；可加 满级 / 光辉 / N个 / -r 密语"),
        ("趋势 物品名", "48h / 90d 价格走势"),
    ],
    "后台推送": [
        ("蹲 类型 筛选 时长 时间", "订阅世界状态变化并推送（需先 .开启 推送）"),
        ("蹲 取消", "不带词=全部；带筛选词=只取消匹配项"),
    ],
}


# 仲裁的任务类型 / 派系 / 节点渲染：**唯一实现**在 core/arbi.py
# （「仲裁」查询指令与「蹲 仲裁」推送共用；2026-09-19 收敛，避免两套口径漂移）。
_arb_mission = _arbi.mission_of
_arb_faction = _arbi.faction_of
_ARB_FACTION_FIX = _arbi._FACTION_FIX   # 兼容旧引用


# ---------------------------------------------------------------------------
# 信条 / 终幕的「元素 + 加成%」显示
#
# 加成值是**玩家上报数据**（DE 无官方 API），只有 wiki「Reset」页的
# Current Valence Bonuses 两张表；而插件侧访问 wiki.warframe.com 会被
# Cloudflare 的 JS 挑战拦（index.php / api.php / rest.php 全 403），
# 所以只能把抓到的值**存快照**进 rotations.json，换轮后人工刷新。
# ---------------------------------------------------------------------------
def _elem_txt(it: dict) -> str:
    """条目行尾的「磁力 25.7%」；没有快照数据时返回空串。"""
    elem = calc.ELEM_ZH.get((it.get("element") or "").lower())
    pct = it.get("bonus")
    if not elem or pct in (None, ""):
        return ""
    try:
        num = float(pct)
    except (TypeError, ValueError):
        return ""
    return f"{elem} {num:g}%"


def _weapon_rows(items: list[dict]) -> list[str]:
    """信条 / 终幕的条目行：``· 中文名（英文名）　磁力 25.7%``。

    只用**一个全角空格**分隔两格，列对齐交给渲染层 —— 那里才有真实字形宽度
    （`render._PAIR_RE` / `pair_col`）。早先在 formatter 里按「CJK 记 2、
    ASCII 记 1」手算补空格，可 Noto CJK 的拉丁字母是**比例宽**，算出来仍差一个
    汉字宽，用户反馈「没对齐真的好丑」。
    """
    out: list[str] = []
    for it in items:
        name = it.get("cn") or it.get("en") or ""
        en = it.get("en") or ""
        label = f"{name}（{en}）" if en and en != name else name
        elem = _elem_txt(it)
        out.append(f"· {label}　{elem}" if elem else f"· {label}")
    return out


@dataclass
class Reply:
    """handler 的统一产出。"""

    title: str = ""
    lines: list[str] = field(default_factory=list)
    footer: str = ""
    whisper: list[str] = field(default_factory=list)  # -r 生成的密语文本
    raw_text: Optional[str] = None                    # 直接输出纯文本（wiki/管理类）
    text_only: bool = False                           # 强制不适配图片
    extra_text: str = ""                              # 卡片之外的补充文本（如 wiki 链接）
    pages: list[tuple[str, list[str]]] = field(       # 多页卡片：[(标题, 行), …]
        default_factory=list)                         # 优先于 title/lines；渲染层自动加页码


@register("astrbot_plugin_warframe", "skyti1437",
          f"{BRAND}：世界状态 / 市场查价 / 蹲点推送",
          "1.0.7")
class WarframeSDJK(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.cfg: dict = dict(config) if config else {}
        # 运行期数据目录：data/plugin_data/<插件名>（规范要求），不再写插件包目录。
        data_dir = _resolve_data_dir()
        self.data_dir = data_dir
        _migrate_legacy_user_data(data_dir)   # 旧版写在插件目录的数据一次性搬家
        core_paths.set_run_dir(data_dir)      # core 层的快照/缓存同源
        # FlareSolverr / 知识库：**开源版要能自己填**（自用版默认值照旧）。
        #   地址按逗号或换行分隔；填了非法值就回退内置默认，不静默猜。
        _flare_urls = parse_url_list(str(self.cfg.get("flaresolverr_urls") or ""))
        self.client = WarframeClient(
            kuvalog_url=(self.cfg.get("kuvalog_url") or ""),
            worldsource=self.cfg.get("worldsource", "de"),
            worldstate_base=self.cfg.get("worldstate_base",
                                         "https://api.warframestat.us"),
            timeout=_num(self.cfg, "http_timeout", 15.0),
            proxy=(self.cfg.get("proxy") or None),
            flare_enabled=bool(self.cfg.get("flaresolverr_enabled", True)),
            flare_urls=_flare_urls or None,
        )
        self.groups = GroupStore(data_dir / "groups.json",
                                 default_platform=self.cfg.get("default_platform", "pc"))
        self.subs = SubscriptionStore(data_dir / "subscriptions.json")
        self.renderer = ImageRenderer(data_dir / "cards")
        self._last_scan: dict[str, tuple[float, dict, dict]] = {}  # 会话→(时刻, 武器, 折算spec)
        # 识卡限流状态：**实例级**（类级默认值是兜底，实例级才隔离）。
        # 类属性是可变对象时，写入会命中类本身 —— 多实例/多测试之间会串。
        self._ocr_busy: set = set()
        self._ocr_last: dict[str, float] = {}
        self.render_mode = self.cfg.get("render_mode", "image")
        self.page_size = int(_num(self.cfg, "page_size", 12))
        self.push = PushDaemon(self.client, self.subs, self._push_send, logger,
                               interval=int(_num(self.cfg, "push_interval", 45)))
        self._routes = self._build_routes()

    def _build_routes(self) -> dict:
        """指令名 -> handler 映射（独立成方法便于离线自检）。"""
        return {
            "help": self._h_help,
            "cetus": self._h_cetus, "timers": self._h_timers,
            "bounty": self._h_bounty, "fissures": self._h_fissures,
            "sortie": self._h_sortie, "archon": self._h_archon,
            "voidtrader": self._h_voidtrader, "dailydeals": self._h_dailydeals,
            "calendar": self._h_calendar, "deeparchimedea": self._h_deep,
            "temporalarchimedea": self._h_temporal, "steelpath": self._h_steelpath,
            "arbitration": self._h_arbitration, "arbtable": self._h_arbtable, "alerts": self._h_alerts,
            "invasions": self._h_invasions, "nightwave": self._h_nightwave,
            "news": self._h_news, "kuva": self._h_kuva,
            "synthtargets": self._h_synth, "construction": self._h_construction,
            "voidstorms": self._h_voidstorms, "events": self._h_events,
            "conclave": self._h_conclave, "primevault": self._h_primevault,
            "clanrewards": self._h_clanrewards, "flashsales": self._h_flashsales,
            "incarnon": self._h_rotation_incarnon, "tenet": self._h_rotation_tenet,
            "coda": self._h_rotation_coda, "acrichis": self._h_acrichis,
            "descendia": self._h_descendia, "incursions": self._h_incursions,
            "wiki": self._h_wiki,
            "valence": self._h_valence,
            "damage": self._h_damage,
            "scan": self._h_scan,
            "scandamage": self._h_scan_damage,
            "kim": self._h_kim,
            "wm": self._h_wm, "wr": self._h_wr, "rm": self._h_rm,
            "rank": self._h_rank, "trend": self._h_trend,
            "openrelic": self._h_openrelic,
            "xh": self._h_xh, "disposition": self._h_disposition,
            "analysis": self._h_riven_analysis,
            "relic": self._h_relic, "parts": self._h_parts,
            "ducats": self._h_ducats,
            "dun": self._h_dun, "status": self._h_status_cmd,
        }


    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # 出站兜底：剥掉 QQ 渲染不了的 Markdown（2026-09-15 用户要求）
    # ------------------------------------------------------------------
    @_result_hook()
    async def _on_decorating_result(self, event: AstrMessageEvent):
        """发送前把文本组件里的 Markdown 标记去掉。

        只管 Plain 文本；图片 / 卡片渲染路径不经过这里，不受影响。
        可用配置项 ``strip_markdown``（默认 true）关闭。
        """
        if not self.cfg.get("strip_markdown", True):
            return
        try:
            result = event.get_result()
        except Exception:  # noqa: BLE001
            return
        if result is None:
            return
        chain = getattr(result, "chain", None)
        if not chain:
            return
        changed = False
        for seg in chain:
            if isinstance(seg, Plain):
                raw = getattr(seg, "text", "") or ""
                cleaned = strip_md(raw)
                if cleaned != raw:
                    seg.text = cleaned
                    changed = True
        if changed:
            logger.debug("[sdjk] 已清理回复中的 Markdown 标记")

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def initialize(self):
        self.push.start()
        self._auto_task = asyncio.create_task(self._rank_autoloop())
        self._valence_task = asyncio.create_task(self._valence_autoloop())
        # 后台预热伤害计算的全部重 JSON + 武器名索引（不阻塞启动）：
        # 不预热时第一条指令要现读武器库/进化/灵化形态/多段/部署表，叠加后
        # 会让首条指令明显变慢（2026-09-17 用户反馈「半天才出来」）。
        asyncio.create_task(asyncio.to_thread(self._warmup))
        logger.info("[sdjk] 插件已加载，共注册 %d 个主指令（输出模式 %s，渲染器%s）",
                    len(self._routes), self.render_mode,
                    "可用" if self.renderer.available else "不可用·降级文字")
        if not self.renderer.available:
            # ★ 别只说「降级文字」（2026-09-25 立）：市场/开源包不带字体，
            #   给用户能直接照做的排查指引。
            logger.warning(
                "[sdjk] 未找到可用中文字体，图片卡片已降级为纯文本。排查："
                "① Linux/Docker 装系统字体 `apt-get install -y fonts-noto-cjk`"
                "（或 fonts-wqy-microhei）；"
                "② 或下载字体 `python scripts/fetch_font.py`；"
                "③ 或把任意中文字体（ttc/ttf/otf）放到插件数据目录的 fonts/ 下"
                "（随插件更新保留）；改完重载插件，本行应变为「渲染器可用」")
        if self._wiki_intro_on():
            try:
                from core import wiki_intro as _wi
                if not _wi.available():
                    # 开关开着但数据缺失（市场/开源包按设计不带 wiki_intro.json）
                    # ——明确说清现象与预期，别让用户以为是故障（2026-09-25 立）
                    logger.info(
                        "[sdjk] wiki 简介卡数据缺失（市场/开源包按设计不含）："
                        "该类条目将出「最小卡」+ 可点链接；遗物卡/部件卡不受影响")
            except Exception:  # noqa: BLE001 - 探测失败不影响启动
                pass

    @staticmethod
    def _warmup() -> None:
        """预热：伤害计算/紫卡分析用到的静态数据一次性载入。"""
        import time as _t
        _t0 = _t.perf_counter()
        try:
            from .core import damage_calc as _dc
        except ImportError:
            from core import damage_calc as _dc
        try:
            _dc.warmup()
            logger.info("[sdjk] 预热完成 %.0f ms（武器库/进化/灵化形态/多段/部署）",
                        (_t.perf_counter() - _t0) * 1000)
        except Exception as exc:  # noqa: BLE001 —— 预热失败不影响功能
            logger.warning("[sdjk] 预热失败（不影响功能）：%s", exc)

    # 价格榜单全量抓取只在**闲时**跑。2026-09-14 用户反馈「bot 回话间隔很久」：
    # 全量抓取实测远超 18 分钟（约 3200 项、实测 1 项/4 秒，跑好几个小时），
    # 期间持续占用 WM 全局限速器，用户的 wm/wr 查询得排在爬虫后面等。
    RANK_CRAWL_IDLE_HOURS = (2, 3, 4, 5, 6)
    # 每晚最多抓多少项（约 4 秒/项 → 900 项 ≈ 1 小时，凌晨窗口内可完成）
    RANK_CRAWL_MAX_PER_RUN = 900

    async def _rank_autoloop(self):
        """插件内置定时：价格榜单每 48 小时自动重建（且只在凌晨闲时）。

        每 30 分钟检查一次落盘时间戳；超 48h **且**当前处于闲时（02:00-06:59）
        才开爬，否则继续等下一个检查点。抓取本身每 50 项落一次盘，中断可续。
        失败退避 10 分钟后重试，不阻断插件其他功能。
        """
        RANKS_FILE = api_client.RANKS_FILE  # 顶层已导入，懒加载绝对导入在服务器上会炸
        while True:
            try:
                data = self.client._load_json_file(RANKS_FILE) or {}
                ts = data.get("ts") or ""
                import time as _t
                from datetime import datetime, timezone
                try:
                    age = (_t.time() - datetime.fromisoformat(ts).timestamp())
                except ValueError:
                    age = float("inf")
                if age > 48 * 3600:
                    # 闲时闸门：白天不爬（会拖慢用户的 wm/wr 查询）
                    if datetime.now().hour not in self.RANK_CRAWL_IDLE_HOURS:
                        logger.info(
                            "[sdjk] 价格榜单已过期，但非闲时（%02d 点），"
                            "凌晨 %d-%d 点再爬", datetime.now().hour,
                            self.RANK_CRAWL_IDLE_HOURS[0],
                            self.RANK_CRAWL_IDLE_HOURS[-1])
                        await asyncio.sleep(1800)
                        continue
                    logger.info("[sdjk] 价格榜单缺失或超 48 小时，开始自动重建")
                    n = await self.client.crawl_wm_ranks(
                        limit=self.RANK_CRAWL_MAX_PER_RUN)
                    logger.info("[sdjk] 价格榜单本轮抓取结束（累计 %d 项）", n)
                await asyncio.sleep(1800)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.warning("[sdjk] 价格榜单自动刷新失败，10 分钟后重试：%s", e)
                await asyncio.sleep(600)

    async def _valence_autoloop(self):
        """插件内置定时：每 6 小时检查一轮 wiki 类快照。

        ① 信条/终幕元素加成（换轮即刷新）；② 变体倾向表（7 天过期重抓）。
        服务器上有 FlareSolverr 代理后，wiki 页面可以直接抓取；FlareSolverr
        未部署/求解失败时只记日志，外部定时任务仍是兜底。
        """
        while True:
            try:
                # 每轮刷新前回收 FlareSolverr 会话（destroy→create）：换一个全新
                # 标签页，防 Chromium 长跑崩掉后整轮连败（2026-09-25 事故：
                # 02:18 的标签页 08:18 崩掉，随后每小时拿坏会话重试全败）。
                # best-effort：回收失败不阻断刷新本身。
                try:
                    if await self.client.recycle_flare_session():
                        logger.info("[sdjk] FlareSolverr 会话已回收（换用新标签页）")
                except Exception:  # noqa: BLE001 - 回收失败照常刷新
                    pass
                # 内存自报（2026-09-25 立）：宿主 1.7G RAM 而 astrbot 有 2.25G 压在
                # swap，卡顿疑似由此而来。每轮记一次 RSS/VmSwap，用于判断是
                # 「随 uptime 线性涨」还是「渲染后台阶式涨」，为降占用/升配定方向。
                try:
                    _st = {}
                    with open("/proc/self/status", "r", encoding="utf-8") as _fh:
                        for _ln in _fh:
                            if _ln.startswith(("VmRSS:", "VmSwap:")):
                                _k, _v = _ln.split(":", 1)
                                _st[_k] = _v.strip()
                    if _st:
                        logger.info("[sdjk] 进程内存自报：%s",
                                    " ".join(f"{k}={v}" for k, v in _st.items()))
                except Exception:  # noqa: BLE001 - 非 Linux（本地调试）跳过
                    pass
                status = await self.client.refresh_valence()
                if status != "fresh":
                    logger.info("[sdjk] 元素加成快照已刷新：%s", status)
                try:
                    disp_status = await self.client.refresh_wiki_disp()
                    if disp_status != "fresh":
                        logger.info("[sdjk] 变体倾向表已刷新：%s", disp_status)
                except Exception as e:  # noqa: BLE001 - 倾向表失败不阻断
                    logger.warning("[sdjk] 变体倾向表刷新失败：%s", e)
                # 言录使（Acrithis）本周货单：DE 不下发。过期后自动抓 wiki
                # 《Acrithis/Current Offerings》子页（社区人工维护的当期 5 件，
                # 2026-09-21 起；原先只告警等人工，实际永远没人更）。
                try:
                    acr_status = await self.client.refresh_acrichis_week()
                    if acr_status == "refreshed":
                        logger.info("[sdjk] 言录使本周货单已自动刷新")
                    elif acr_status == "not-updated":
                        logger.warning("[sdjk] 言录使货单已过期且 wiki 当期上报"
                                       "尚未更新，卡面暂以候选池展示")
                    elif acr_status == "failed":
                        logger.warning("[sdjk] 言录使本周货单自动抓取失败："
                                       "需人工更新 core/data/de/acrichis_week.json"
                                       "（卡面已标注「货单待更新」）")
                except Exception:  # noqa: BLE001 - 自检失败不阻断
                    pass
                await asyncio.sleep(6 * 3600)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.warning("[sdjk] 元素加成快照刷新失败（1 小时后重试）：%s", e)
                await asyncio.sleep(3600)

    async def terminate(self):
        for name in ("_auto_task", "_valence_task"):
            task = getattr(self, name, None)
            if task:
                task.cancel()
        await self.push.stop()
        await self.client.close()
        logger.info("[sdjk] 插件已卸载")

    # ------------------------------------------------------------------
    # 总分发：捕获全部消息，交由 SDJK 解析器处理
    # ------------------------------------------------------------------
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        text = (getattr(event, "message_str", "") or "").strip()
        if not text or text.startswith("/"):
            return

        # 带唤醒前缀（"/xxx"）时，AstrBot 已把 "/" 剥掉，此处按原始消息还原判断：
        # 若该词已被内置指令或其他插件占用，就让路 —— 直接 return 且**不** stop_event，
        # 后面的处理器照常接住（详见文件头「指令空间避让」注释）。
        if _wake_prefix_stripped(event):
            head = text.split(" ", 1)[0].lower()
            if head in _occupied_command_words():
                logger.info("[sdjk] 「/%s」已由内置指令或其他插件占用，本次让路", head)
                return

        # 管理指令命名空间：“.”
        if text.startswith("."):
            reply = await self._handle_admin(event, text)
            if reply:
                yield event.plain_result(reply.raw_text or "")
                event.stop_event()
            return

        parsed = parse(text)
        if not parsed.command:
            return
        handler = self._routes.get(parsed.command)
        if handler is None:
            return
        platform = parsed.platform or self.groups.platform(event.unified_msg_origin)

        try:
            reply = await handler(parsed, event, platform)
        except WarframeAPIError as exc:
            reply = Reply(raw_text=f"⚠️ {exc}")
        except Exception as exc:  # noqa: BLE001
            logger.exception("[sdjk] 指令处理异常：%s", exc)
            reply = Reply(raw_text="⚠️ 内部错误，请稍后重试或联系管理员查看日志")

        if reply is None:
            return
        async for result in self._build_results(event, reply, parsed):
            yield result
        event.stop_event()

    # ------------------------------------------------------------------
    # 输出构建：-w 文字 / -t 图片 / 自适应
    # ------------------------------------------------------------------
    async def _build_results(self, event: AstrMessageEvent, reply: Reply,
                             parsed: Parsed):
        """输出构建（异步生成器）。

        渲染是 PIL 同步重活，必须扔到线程池 —— AstrBot 的单事件循环里
        直接调它会整机卡住（实测 watchdog 记到过 33s 的 stall，栈顶就是
        PIL 的 resize/convert）。文字分支是纯字符串拼接，留在协程里。
        """
        if reply.raw_text is not None:
            yield event.plain_result(reply.raw_text)
            return

        whisper_txt = ""
        if reply.whisper:
            whisper_txt = "\n\n🔑 快捷回复（复制后游戏内粘贴）：\n" + "\n".join(reply.whisper)
        # 卡片之外的补充文本（wiki 链接这类必须可点击的内容）：
        # 与卡片同链发出，链尾的 Plain 段在 QQ 里是可点的文本。
        tail_txt = whisper_txt + (("\n" + reply.extra_text) if reply.extra_text else "")

        use_image = False
        if not reply.text_only:
            if parsed.force_image:
                use_image = True
            elif parsed.force_text:
                use_image = False
            elif self.render_mode == "image":
                use_image = True
            elif self.render_mode == "text":
                use_image = False
            else:
                use_image = len(reply.lines) > 14 and self.renderer.available

        if use_image and reply.pages:
            # 多页卡片：每页一张图，一链发出（识卡+伤害详情这类组合用）
            comps = []
            n = len(reply.pages)
            for i, (ptitle, plines) in enumerate(reply.pages, 1):
                # 渲染串行：PIL 单线程吃 CPU，容器算力弱，多任务同时渲染
                # 会互相拖慢（实测叠加时单张 0.7s → 39s）
                if WarframeSDJK._render_lock is None:
                    WarframeSDJK._render_lock = asyncio.Lock()
                async with WarframeSDJK._render_lock:
                    path = await asyncio.to_thread(
                        self.renderer.render, f"{ptitle}（第{i}/{n}页）",
                        plines, reply.footer)
                if path:
                    comps.append(Image.fromFileSystem(path))
                    self._schedule_card_cleanup(path)
            if comps:
                if tail_txt:
                    comps.append(Plain(tail_txt))
                yield event.chain_result(comps)
                return
            logger.warning("[sdjk] 多页卡片渲染为空，已降级文字输出：%s",
                           reply.pages[0][0] if reply.pages else "?")

        if use_image:
            if WarframeSDJK._render_lock is None:
                WarframeSDJK._render_lock = asyncio.Lock()
            async with WarframeSDJK._render_lock:
                path = await asyncio.to_thread(
                    self.renderer.render, reply.title,
                    reply.lines, reply.footer)
            if path:
                components = [Image.fromFileSystem(path)]
                if tail_txt:
                    components.append(Plain(tail_txt))
                self._schedule_card_cleanup(path)
                yield event.chain_result(components)
                return
            # 字体缺失等渲染失败 → 降级文字（失败原因见 render.py 的异常日志）
            logger.warning("[sdjk] 图片渲染为空，已降级文字输出：%s", reply.title)

        if reply.pages:
            # 文本降级：多页按分节拼接
            card = "\n\n".join(text_card(pt, pl, reply.footer)
                                for pt, pl in reply.pages)
        else:
            card = text_card(reply.title, reply.lines, reply.footer)
        yield event.plain_result(card + tail_txt)

    def _schedule_card_cleanup(self, path: str, delay: float = 120.0):
        """卡片发出后延迟删除本地文件（缓冲期内完成 QQ 上传）。"""
        async def _rm():
            await asyncio.sleep(delay)
            try:
                Path(path).unlink(missing_ok=True)
            except OSError:
                pass
        try:
            asyncio.get_running_loop().create_task(_rm())
        except RuntimeError:
            pass

    # ------------------------------------------------------------------
    # 群管理（. 前缀）
    # ------------------------------------------------------------------
    def _is_admin(self, event: AstrMessageEvent) -> bool:
        """是否具备群管理权限。

        ★ 安全审查（2026-09-18）修正两点：
          ① 优先用 AstrBot 自带的 ``event.is_admin()``（4.x 已有）——
             自己解析 ``role`` 字符串，会在平台/版本差异下漏判（例如对方
             新增了角色取值）。自研解析只作为兜底。
          ② ``super_admins`` 白名单比较统一转成字符串：配置面板里填成
             数字时，``"123" in [123]`` 永远是 False —— 表现为「配了超管却
             没权限」，是静默失效（fail-closed，不危险但很坑）。
        """
        try:
            fn = getattr(event, "is_admin", None)
            if callable(fn) and fn():
                return True
        except Exception:  # noqa: BLE001
            pass
        role = str(getattr(event, "role", "") or "").lower()
        if role in ("admin", "owner", "administrator"):
            return True
        admins = (getattr(self, "cfg", None) or {}).get("super_admins") or []
        try:
            sid = str(event.get_sender_id() or "")
            return bool(sid) and sid in {str(a).strip() for a in admins}
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _safe_sender(event) -> str:
        """取发送者 id；取不到返回空串（调用方据此跳过按人限制）。"""
        try:
            return str(event.get_sender_id() or "")
        except Exception:  # noqa: BLE001
            return ""

    async def _handle_admin(self, event: AstrMessageEvent, text: str) -> Optional[Reply]:
        tokens = text[1:].strip().split()
        if not tokens:
            return None
        cmd, args = tokens[0], tokens[1:]
        umo = event.unified_msg_origin

        if cmd in ("默认平台", "平台"):
            if not self._is_admin(event):
                return Reply(raw_text="⛔ 该指令需要群管理员权限")
            if args:
                plat = normalize_platform(args[0])
                if not plat:
                    return Reply(raw_text="用法：.默认平台 pc/ps/xb/sw")
                await self.groups.set_platform(umo, plat)
                return Reply(raw_text=f"✅ 本群默认平台已设为 {PLATFORM_DISPLAY[plat]}"
                                      f"（{plat}），可用 -pc/-ps 等临时覆盖")
            cur = self.groups.get(umo)
            return Reply(raw_text=f"当前默认平台：{PLATFORM_DISPLAY.get(cur['platform'], cur['platform'])}")

        if cmd in ("开启", "关闭"):
            if not self._is_admin(event):
                return Reply(raw_text="⛔ 该指令需要群管理员权限")
            if not args or args[0] not in ("推送", "聊天"):
                return Reply(raw_text="用法：.开启 推送 / .关闭 推送")
            value = cmd == "开启"
            await self.groups.set_switch(umo, args[0], value)
            state = "已开启" if value else "已关闭"
            return Reply(raw_text=f"✅ {args[0]}功能{state}"
                                  + ("，可以用「蹲 类型」订阅推送了" if value and args[0] == "推送" else ""))

        if cmd == "状态":
            # ★ 安全审查（2026-09-18）：这条会列出**本群全部订阅明细**
            #   （谁订了什么、还剩多久）与缓存统计，却完全没有权限校验 ——
            #   群里任何人都能看。改为与其它管理指令一致。
            if not self._is_admin(event):
                return Reply(raw_text="⛔ 该指令需要群管理员权限"
                                      "（会列出本群订阅明细）")
            g = self.groups.get(umo)
            subs = self.subs.for_umo(umo)
            cache = self.client.cache.stats()
            lines = ["◆ 本群"]
            lines.append(f"· 默认平台　{PLATFORM_DISPLAY.get(g['platform'], g['platform'])}")
            lines.append(f"· 推送　{'开' if g['push'] else '关'}")
            lines.append(f"· 蹲订阅　{len(subs)} 条")
            if subs:
                lines.append("◆ 订阅明细")
                for i, s in enumerate(subs, 1):
                    if s.until < 0:
                        life = "永久"
                    else:
                        left = s.until - time.time()
                        life = (f"剩{left // 86400:.0f}天{left % 86400 // 3600:.0f}小时"
                                if left >= 3600 else f"剩{max(left, 0) // 60:.0f}分钟")
                    mode = "一次" if s.once else "持续"
                    lines.append(f"· {i}. {s.event}"
                                 + (f"：{s.rule}" if s.rule else "")
                                 + f"　{mode}·{life}")
            lines.append("◆ 系统")
            lines.append(f"· 缓存　{cache['size']} 项 · 命中率 {cache['hit_rate'] * 100:.0f}%")
            return Reply(f"{BRAND} {WATERMARK_VERSION}", lines,
                         footer=fmt.fmt_platform_footer(self.groups.platform(umo)))

        if cmd == "锚点":
            # ★★ 安全审查（2026-09-18）发现：本指令写入的锚点**没有任何读取方**。
            #   仲裁表已改走 arbi.wf.wiki 的确定性排期，当初承接校准的
            #   `de_worldstate.arb_from_anchor` 已删除（2026-09-25 清死代码）
            #   —— 也就是说用户「校准」完其实毫无效果；而写入点是
            #   全局的（cfg + runtime/arb_anchor.json），任何群的群管都能覆盖，
            #   属于跨租户写入。
            #   按项目铁律「失效功能必须给出真实可用的替代指令」，这里保留指令名
            #   但**明确告知已废弃**，且不再写任何全局状态。
            return Reply(raw_text="「.锚点」已废弃：仲裁表现在按 arbi.wf.wiki 的"
                                  "确定性排期自动推算，不需要手动校准。\n"
                                  "· 看当前与下一小时场次：发「仲裁」\n"
                                  "· 看整周 30 天排期：发「仲裁表」（可筛类型）")
        if cmd in ("帮助", "help"):
            return await self._h_help(None, event, self.groups.platform(umo))
        return None

    # ------------------------------------------------------------------
    # 世界状态类 handler
    # ------------------------------------------------------------------
    async def _gather(self, coros):
        return await asyncio.gather(*coros, return_exceptions=True)

    async def _h_cetus(self, parsed, event, platform) -> Reply:
        res = await self._gather([self.client.cycle(platform, n)
                                  for n in ("cetus", "vallis", "cambion", "earth",
                                            "duviri", "zariman")])
        cycles = [r for r in res if isinstance(r, dict)]
        title, lines = fmt.fmt_cetus(*cycles)
        return Reply(title, lines, footer=fmt.fmt_platform_footer(platform))

    @staticmethod
    def _rotation_expiry(data: dict) -> str:
        """轮换表（信条 / 终幕等）的下一次重置时间（ISO）。"""
        from datetime import datetime, timedelta, timezone
        try:
            epoch = datetime.fromisoformat(data["epoch"])
            period = timedelta(hours=int(data.get("period_hours", 96)))
            now = datetime.now(timezone.utc)
            passed = int((now - epoch) // period)
            return (epoch + period * (passed + 1)).isoformat()
        except (KeyError, TypeError, ValueError):
            return ""

    @staticmethod
    def _weekly_reset() -> str:
        """每周重置锚点（周一 00:00 UTC）—— 灵化回廊 / 言录使等周常的到期点。"""
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        base = (now - timedelta(days=now.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0)
        if base <= now:
            base += timedelta(days=7)
        return base.isoformat()

    async def _h_timers(self, parsed, event, platform) -> Reply:
        # worldState 是整包缓存的，这里多取几个键几乎不加延迟
        res = await self._gather([
            self.client.cycle(platform, "cetus"), self.client.cycle(platform, "vallis"),
            self.client.cycle(platform, "cambion"), self.client.cycle(platform, "duviri"),
            self.client.cycle(platform, "zariman"),
            self.client.arbitration(platform), self.client.sortie(platform),
            self.client.void_trader(platform), self.client.archon_hunt(platform),
            self.client.steel_path(platform), self.client.nightwave(platform),
            self.client.calendar(platform), self.client.deep_archimedea(platform),
            self.client.temporal_archimedea(platform),
        ])
        names = ["夜灵平野", "奥布山谷", "魔胎之境", "双衍王境", "扎里曼派系",
                 "仲裁", "每日突击", "虚空奸商", "执刑官猎杀", "钢铁侵蚀",
                 "午夜电波", "1999日历", "深层科研", "时光科研"]
        # ★ 不许静默丢行（2026-09-25）：源恒抛/未下发（如 DE 源没有的
        #   仲裁、钢铁侵蚀）也要把 None 传给 formatter，由它显式打
        #   「暂无时效数据（源未下发）」；旧写法 isinstance 过滤会让这两行
        #   从卡面里静默消失（用户以为看全了）。
        timers = [(n, r if isinstance(r, dict) else None) for n, r in zip(names, res)]
        timers.append(("沉沦之地", await self.client.descendia(platform)))
        # 本地可推算的确定性轮换（不占网络请求）
        rot = {}
        try:
            rot = json.loads(ROTATION_FILE.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            rot = {}
        for key, name in (("incarnon", "钢铁回廊灵化"), ("tenet", "信条元素加成"),
                          ("coda", "终幕换批")):
            exp = self._rotation_expiry(rot.get(key) or {})
            if exp:
                timers.append((name, {"expiry": exp}))
        timers.append(("周常重置", {"expiry": self._weekly_reset()}))
        title, lines = fmt.fmt_timers(timers)
        return Reply(title, lines, footer=fmt.fmt_platform_footer(platform))

    async def _h_bounty(self, parsed, event, platform) -> Reply:
        keyword = " ".join([parsed.preset] if parsed.preset else []) + " " + parsed.content_str
        keyword = keyword.strip()
        data = await self.client.syndicate_missions(platform)
        # oracle 补「扎里曼 / 实验室 / 1999」的节点与挑战（DE 侧 Jobs 恒为空）；
        # 客户端内置 15 分钟缓存 + 3.5s 硬超时，拿不到就降级为只列等级，不拖主流程。
        cycle = await self.client.bounty_cycle()
        title, lines = fmt.fmt_bounties(data, keyword, cycle)
        return Reply(title, lines, footer=fmt.fmt_platform_footer(platform))

    async def _h_fissures(self, parsed, event, platform) -> Reply:
        content = parsed.content_str
        if parsed.preset:
            content = (parsed.preset + " " + content).strip()
        flt = parse_fissure_filter(content) if content else None
        data = await self.client.fissures(platform)
        title, lines = fmt.fmt_fissures(
            data, flt=flt.match if flt else None,
            page=parsed.page, page_size=self.page_size, all_rows=True)
        extra = f"筛选：{flt.describe()}" if flt else ""
        return Reply(title, lines, footer=fmt.fmt_platform_footer(platform, extra))

    async def _h_sortie(self, parsed, event, platform) -> Reply:
        title, lines = fmt.fmt_sortie(await self.client.sortie(platform))
        return Reply(title, lines, footer=fmt.fmt_platform_footer(platform))

    async def _h_archon(self, parsed, event, platform) -> Reply:
        title, lines = fmt.fmt_archon(await self.client.archon_hunt(platform))
        return Reply(title, lines, footer=fmt.fmt_platform_footer(platform))

    async def _h_voidtrader(self, parsed, event, platform) -> Reply:
        """虚空商人（Baro Ki'Teer）：当前库存 / 下期预测。

        「奸商 预测」给出**基于 wiki 历史到访记录的候选排序**（统计推测，
        非官方 —— DE 不公布下期库存），口径写在卡面上。
        """
        toks = [str(t).lower() for t in (parsed.content or [])]
        if any(t in ("预测", "predict") for t in toks):
            rows = baro.predict(8)
            title, lines = fmt.fmt_baro_predict(
                rows, baro.next_visit_est() or "",
                len(baro.visits()), baro.last_visit() or "",
                names_zh=baro.names_zh())
            return Reply(title, lines,
                         footer=fmt.fmt_platform_footer(platform, "wiki 历史统计"))
        title, lines = fmt.fmt_void_trader(await self.client.void_trader(platform))
        if baro.visits():
            lines.append(f"※ 想看下期可能卖什么：发「奸商 预测」"
                         f"（基于 wiki 的 {len(baro.visits())} 次到访统计）")
        return Reply(title, lines, footer=fmt.fmt_platform_footer(platform))

    async def _h_dailydeals(self, parsed, event, platform) -> Reply:
        title, lines = fmt.fmt_daily_deals(await self.client.daily_deals(platform))
        return Reply(title, lines, footer=fmt.fmt_platform_footer(platform))

    async def _h_calendar(self, parsed, event, platform) -> Reply:
        toks = list(parsed.content or [])
        if parsed.preset:
            toks.insert(0, parsed.preset)
        mode = next((t for t in toks if t in ("奖励", "清单", "覆写")), "")
        title, lines = fmt.fmt_calendar(await self.client.calendar(platform),
                                        mode=mode)
        return Reply(title, lines, footer=fmt.fmt_platform_footer(platform))

    async def _h_deep(self, parsed, event, platform) -> Reply:
        title, lines = fmt.fmt_archimedea(
            await self.client.deep_archimedea(platform), "深层科研")
        return Reply(title, lines, footer=fmt.fmt_platform_footer(platform))

    async def _h_temporal(self, parsed, event, platform) -> Reply:
        title, lines = fmt.fmt_archimedea(
            await self.client.temporal_archimedea(platform), "时光科研")
        return Reply(title, lines, footer=fmt.fmt_platform_footer(platform))

    async def _h_steelpath(self, parsed, event, platform) -> Reply:
        """钢铁之路 = 钢铁精华兑换（Teshin 荣誉商店）。

        数据源是 warframe wiki 的 Steel Essence 页（常驻 24 件 + 每周轮换 8 件），
        中文名取自 DE 官方 language 表。warframestat 的 steelPath 端点、
        完整版 worldState 均已不可用（403 / 404），改用本地表 + 官方轮换锚点推算。
        """
        title, lines = fmt.fmt_steel_essence_shop()
        return Reply(title, lines, footer=fmt.fmt_platform_footer(platform, "钢铁精华兑换"))

    # 仲裁筛选关键词 -> arbi.wf.wiki 的 missionNameZh 规范值
    # （该站中文表把 Infested Salvage 写作「INFESTED 资源回收」，比对前需去前缀）
    _ARB_TYPES = {
        "生存": "生存", "防御": "防御", "镜像防御": "镜像防御",
        "拦截": "拦截", "挖掘": "挖掘", "叛逃": "叛逃",
        "回收": "资源回收", "资源回收": "资源回收",
        "中断": "中断", "歼灭": "歼灭", "捕获": "捕获",
        "虚空洪流": "虚空洪流", "虚空覆涌": "虚空覆涌", "虚空决战": "虚空决战",
        "联结生存": "联结生存", "元素转换": "元素转换",
    }
    _ARB_RATING = {"高效": ("S", "A+", "A"), "传奇": ("S",)}
    _ARB_TYPES_STR = "生存 / 防御 / 镜像防御 / 拦截 / 挖掘 / 叛逃 / 回收 / 中断 /" \
                     " 歼灭 / 捕获 / 虚空洪流 / 虚空覆涌 / 虚空决战 / 联结生存 / 元素转换"

    async def _h_arbitration(self, parsed, event, platform) -> Reply:
        """当前仲裁 / 按类型筛选未来排期（arbi.wf.wiki 官方数据）。"""
        import time as _t
        from datetime import datetime, timezone, timedelta

        toks = list(parsed.content or [])
        if parsed.preset:
            toks.insert(0, parsed.preset)
        want_types = {self._ARB_TYPES[t] for t in toks if t in self._ARB_TYPES}
        want_ratings = next((self._ARB_RATING[t] for t in toks
                             if t in self._ARB_RATING), ())
        today_only = any(t in ("今天", "今日") for t in toks)
        filtered = bool(want_types or want_ratings or today_only)

        if not filtered:      # 默认：当前 + 下一小时
            return await self._arb_now(platform)

        sched, nodes, tier_of = await self._arb_fetch()
        seq, step, start = sched["seq"], sched.get("stepSec", 3600), sched["startTs"]
        idx0 = int((_t.time() - start) // step)
        bj = timezone(timedelta(hours=8))
        now_bj = datetime.now(bj)
        end_bj = now_bj.replace(hour=23, minute=59, second=59) if today_only \
            else now_bj + timedelta(days=14)
        rows = []
        for h in range(0, min(24 * 15, len(seq) - idx0)):
            key = sched["nodes"][seq[idx0 + h]]
            n = nodes.get(key, {})
            mt = _arb_mission(n)
            tv = tier_of.get(key, "")
            if tv == "未评级":
                tv = ""
            t = datetime.fromtimestamp(start + (idx0 + h) * step, tz=timezone.utc) \
                .astimezone(bj)
            if t > end_bj:
                break
            if want_types and mt not in want_types:
                continue
            if want_ratings and tv not in want_ratings:
                continue
            line = self._arb_node_line(nodes, key, tier_of)
            rows.append((t, line, tv))
        if not rows:
            return Reply(raw_text=f"该筛选条件下未来 14 天内没有仲裁场次\n"
                                  f"可筛选类型：{self._ARB_TYPES_STR}\n"
                                  f"评级筛选：高效（S/A+/A） / 传奇（S）")
        page_size = self.page_size
        total = len(rows)
        pages = max(1, (total + page_size - 1) // page_size)
        page = max(1, min(parsed.page or 1, pages))
        chunk = rows[(page - 1) * page_size: page * page_size]
        lines = [f"{t.month}月{t.day}日 {t.hour:02d}时　{desc}"
                 for t, desc, _tv in chunk]
        lines.append("※ 每行格式：节点（星球） · 任务类型 · 派系 · [站点评级]")
        lines.append("※ 评级来源 arbi.wf.wiki 社区评级；排期为确定性序列，非随机")
        fdesc = "、".join([*(t for t in toks if t in self._ARB_TYPES),
                           *(("高效" if want_ratings == self._ARB_RATING["高效"] else
                              "传奇") for _ in [0] if want_ratings),
                           *(["今天"] if today_only else [])]) or "全部"
        title = f"仲裁排期 · {fdesc}（第{page}/{pages}页，共{total}场）"
        return Reply(title, lines, footer=fmt.fmt_platform_footer(platform, "arbi.wf.wiki"))

    async def _arb_fetch(self) -> tuple[dict, dict, dict]:
        """拉 arbi.wf.wiki 三张表：排期序列 / 节点中文表 / 站点评级。

        数据源说明（用户可核对）：
          · arbys.schedule.v2.json —— 确定性的逐小时排期（官方数据推导，非随机）
          · arbys.nodes.zh.json    —— 节点/星球/任务类型/派系/等级的中文名
          · tierlist.default.json  —— 社区站点评级（S/A+/A/A-/B/C）
        """
        return await _arbi.fetch_tables(self.client)

    @staticmethod
    def _arb_node_line(nodes: dict, key: str, tier_of: dict) -> str:
        """把节点渲染成一行：节点（星球） · 类型 · 派系 · 评级。

        Args:
            nodes: 节点字典（key -> 节点数据）。
            key: 目标节点 key。
            tier_of: 节点 -> 评级。

        Notes:
            arbi 数据里的 ``minEnemyLevel/maxEnemyLevel`` 是**敌人等级区间**（不是
            「原始难度」之类的概念 —— 仲裁根本没有那种说法），但很多用户看着
            「Lv 6-11」会以为和之前那条「高效 / 传奇」筛选是一回事，反而起干扰，
            砍掉。
        """
        n = nodes.get(key) or {}
        return _arbi.node_line(nodes, key, tier_of)
    async def _arb_now(self, platform) -> Reply:
        """当前仲裁 + 下一小时（含节点/星球/类型/派系/等级/站点评级 + 数据源说明）。"""
        import time as _t
        sched, nodes, tier_of = await self._arb_fetch()
        seq = sched["seq"]
        start, step = sched["startTs"], sched.get("stepSec", 3600)
        idx = int((_t.time() - start) // step) % len(seq)
        key_now = sched["nodes"][seq[idx]]
        key_nxt = sched["nodes"][seq[(idx + 1) % len(seq)]]
        n = nodes.get(key_now) or {}
        eff = ""

        # 生息效率数值：本地社区实测均值（非官方），明确标注来源
        ratings = {}
        try:
            ratings = json.loads((PLUGIN_DIR / "core" / "data" / "arb_ratings.json")
                                 .read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
        en_type = (n.get("missionType") or "").replace("MT_", "").title()
        rec = ratings.get(f"{n.get('nameZh', '')}|{en_type}")
        eff = f"　生息效率 {rec[0]}/小时（社区实测 {rec[1]} 档）" if rec else ""

        lines = [f"节点：{n.get('nameZh', '?')}"
                 + (f"（{n.get('systemNameZh')}）" if n.get("systemNameZh") else ""),
                 f"类型：{_arb_mission(n)} · 派系："
                 f"{_arb_faction(n) or '?'}",
                 "评级：" + (tier_of.get(key_now) or "未评级")
                 + "（arbi.wf.wiki 社区评级）" + eff,
                 "下一小时："
                 + self._arb_node_line(nodes, key_nxt, tier_of),
                 f"筛选：仲裁 {self._ARB_TYPES_STR} / 高效 / 传奇 / 今天"]
        lines.append("※ 排期·节点·派系·等级·评级：arbi.wf.wiki（社区维护的确定性序列）")
        lines.append("※ 生息效率：社区实测均值表 core/data/arb_ratings.json，非官方数据，仅供参考")
        return Reply("当前仲裁", lines, footer=fmt.fmt_platform_footer(platform))

    async def _h_arbtable(self, parsed, event, platform) -> Reply:
        """仲裁时间表：arbi.wf.wiki 官方数据（中文节点+派系+站点评级）。"""
        import time as _t
        from datetime import datetime, timezone, timedelta
        base = "https://arbi.wf.wiki/data/"
        sched = await self.client._fetch_json(base + "arbys.schedule.v2.json", ttl=3600)
        nodes = (await self.client._fetch_json(base + "arbys.nodes.zh.json", ttl=86400))["nodes"]
        tier = await self.client._fetch_json(base + "tierlist.default.json", ttl=86400)
        node_tier = {}
        for t_name, lst in tier.get("tierBuckets", {}).items():
            for nk in lst:
                node_tier[nk] = t_name
        start, step = sched["startTs"], sched.get("stepSec", 3600)
        seq = sched["seq"]
        idx0 = int((_t.time() - start) // step)
        rows = []
        for h in range(0, min(24 * 14, len(seq) - idx0)):  # 逐小时，14 天
            key = sched["nodes"][seq[idx0 + h]]
            n = nodes.get(key, {})
            name = n.get("nameZh", "?")
            system = n.get("systemNameZh", "")
            mtype = _arb_mission(n)
            fac = _arb_faction(n)
            tv = node_tier.get(key, "")
            if tv == "未评级":
                tv = ""
            t = datetime.fromtimestamp(start + (idx0 + h) * step,
                                       tz=timezone.utc) + timedelta(hours=8)
            rows.append(f"{t.day}日{t.hour:02d}时　{name} {system} {mtype} {fac} {tv}".rstrip())
        size = 15
        total = len(rows)
        pages = max(1, (total + size - 1) // size)
        page = min(max(1, parsed.page or 1), pages)
        page_rows = rows[(page - 1) * size: page * size]
        # 列对齐：节点/星球/模式/派系 四列按本页最大宽度 pad
        import unicodedata as _ud

        def dw(s: str) -> int:
            return sum(2 if _ud.east_asian_width(c) in ("F", "W") else 1
                       for c in s)

        def pad(s: str, w: int) -> str:
            return s + " " * max(2, w - dw(s) + 2)

        cols = [r.split("　", 1) for r in page_rows]
        fields = []
        for _, rest in cols:
            parts = rest.split(" ")
            # 节点 星球 模式 派系 [评级]（派系可能带词尾，评级可选）
            f = [p2 for p2 in parts if p2]
            fields.append(f)
        w_node = max(dw(f[0]) for f in fields)
        w_sys = max(dw(f[1]) for f in fields)
        w_type = max(dw(f[2]) for f in fields)
        chunk = []
        for (t_head, _), f in zip(cols, fields):
            node = f[0]
            sysx = f[1]
            typ = f[2]
            fac = f[3] if len(f) > 3 else ""
            tier = f[4] if len(f) > 4 else ""
            cells = [t_head, node, sysx, typ, fac]
            if tier:
                cells.append(tier)
            chunk.append("　".join(c for c in cells if c))
        title = f"仲裁时间表（第{page}/{pages}页，共{total}条）"
        return Reply(title, chunk, footer=fmt.fmt_platform_footer(platform))

    async def _h_alerts(self, parsed, event, platform) -> Reply:
        title, lines = fmt.fmt_alerts(await self.client.alerts(platform))
        return Reply(title, lines, footer=fmt.fmt_platform_footer(platform))

    async def _h_invasions(self, parsed, event, platform) -> Reply:
        inv = await self.client.invasions(platform)
        title, lines = fmt.fmt_invasions(inv, page=parsed.page)
        return Reply(title, lines, footer=fmt.fmt_platform_footer(platform))

    async def _h_nightwave(self, parsed, event, platform) -> Reply:
        title, lines = fmt.fmt_nightwave(await self.client.nightwave(platform))
        return Reply(title, lines, footer=fmt.fmt_platform_footer(platform))

    async def _h_news(self, parsed, event, platform) -> Reply:
        title, lines = fmt.fmt_news(await self.client.news(platform))
        return Reply(title, lines, footer=fmt.fmt_platform_footer(platform))

    async def _h_kuva(self, parsed, event, platform) -> Reply:
        title, lines = fmt.fmt_kuva(await self.client.kuva(platform))
        return Reply(title, lines, footer=fmt.fmt_platform_footer(platform))

    async def _h_synth(self, parsed, event, platform) -> Reply:
        data = await self.client.synth_targets(platform)
        title, lines = fmt.fmt_synth_targets(data)
        return Reply(title, lines, footer=fmt.fmt_platform_footer(platform))

    async def _h_construction(self, parsed, event, platform) -> Reply:
        data = await self.client.construction(platform)
        title, lines = fmt.fmt_construction(data)
        return Reply(title, lines, footer=fmt.fmt_platform_footer(platform))

    # ------------------------------------------------------------------
    # 新增世界状态：九重天 / 活动 / 武形秘仪 / 阿耶兑换 / 氏族奖励 / 商城折扣
    # ------------------------------------------------------------------
    async def _h_voidstorms(self, parsed, event, platform) -> Reply:
        title, lines = fmt.fmt_void_storms(await self.client.void_storms(platform))
        return Reply(title, lines, footer=fmt.fmt_platform_footer(platform))

    async def _h_events(self, parsed, event, platform) -> Reply:
        title, lines = fmt.fmt_events(await self.client.events(platform))
        return Reply(title, lines, footer=fmt.fmt_platform_footer(platform))

    async def _h_conclave(self, parsed, event, platform) -> Reply:
        title, lines = fmt.fmt_conclave(await self.client.conclave(platform))
        return Reply(title, lines, footer=fmt.fmt_platform_footer(platform))

    async def _h_primevault(self, parsed, event, platform) -> Reply:
        """「出库」与「阿耶」共用本 handler，按**用户实际发出的词**分流。

        · 出库 / 御品            → 按 战甲 / 武器 / 守护 分组，只列整套名
        · 阿耶 / 御品阿耶 / 阿耶兑换 → 带价格的完整兑换表（可翻页）

        此前两者输出完全相同，用户 2026-09-17 反馈「出库和阿耶为什么
        是一样的」，故按 ``command_raw`` 拆开。
        """
        vault = await self.client.prime_vault(platform)
        trigger = (parsed.command_raw or "").strip()
        if trigger in ("出库", "御品"):
            title, lines = fmt.fmt_prime_vault_list(vault)
        else:
            title, lines = fmt.fmt_prime_vault(vault, page=parsed.page)
        return Reply(title, lines, footer=fmt.fmt_platform_footer(platform))

    async def _h_clanrewards(self, parsed, event, platform) -> Reply:
        title, lines = fmt.fmt_clan_rewards(await self.client.clan_rewards(platform))
        return Reply(title, lines, footer=fmt.fmt_platform_footer(platform))

    async def _h_flashsales(self, parsed, event, platform) -> Reply:
        sales = await self.client.flash_sales(platform)
        title, lines = fmt.fmt_flash_sales(sales, page=parsed.page)
        return Reply(title, lines, footer=fmt.fmt_platform_footer(platform))

    # ------------------------------------------------------------------
    # 确定性轮换（灵化/信条/终幕/言录使）：数据驱动，见 core/data/rotations.json
    # ------------------------------------------------------------------
    @staticmethod
    def _valence_note(data: dict, window_start) -> str:
        """元素加成快照的说明行（快照落在上一轮时明确标「可能已变」）。"""
        from datetime import datetime, timezone
        raw = data.get("valence_snapshot") or ""
        if not raw:
            return ""
        try:
            snap = datetime.fromisoformat(raw)
        except (TypeError, ValueError):
            return ""
        if snap.tzinfo is None:
            snap = snap.replace(tzinfo=timezone.utc)
        stale = snap < window_start
        when = snap.astimezone(timezone.utc).strftime("%m-%d %H:%M")
        flag = "（上一轮快照，数值可能已变）" if stale else "（本轮快照）"
        return (f"※ 元素与加成为 {when} UTC 快照{flag}：wiki「Reset」页玩家上报值"
                f"（无官方 API，换轮后需人工刷新），以游戏内商店为准")

    def _rotation_lines(self, key: str, title: str) -> Reply:
        """轮换表查询。支持三种模式：
        · weekly_cycle：N 周固定循环（灵化，锚点周次已知）
        · batch_cycle：A/B 两批交替（终幕）
        · refresh_only：常驻 + 定期重生成（信条，只刷新元素加成）
        · 默认：按 period_hours 从 epoch 起算的滑动窗口
        """
        data: dict = {}
        try:
            data = json.loads(ROTATION_FILE.read_text(encoding="utf-8")).get(key, {})
        except Exception:  # noqa: BLE001
            pass
        from datetime import datetime, timedelta, timezone

        # —— 模式一：固定周循环 ——
        weeks = data.get("weeks") or []
        if weeks:
            epoch = datetime.fromisoformat(data["epoch"])
            anchor_week = int(data.get("anchor_week", 1))
            period = timedelta(hours=int(data.get("period_hours", 168)))
            now = datetime.now(timezone.utc)
            passed = int((now - epoch) // period)
            wk_no = (anchor_week - 1 + passed) % len(weeks) + 1
            cur = weeks[wk_no - 1]
            nxt = weeks[wk_no % len(weeks)]
            # 本周重置点（周一 00:00 UTC）
            wd = now.weekday()
            reset = (now - timedelta(days=wd)).replace(hour=0, minute=0, second=0,
                                                       microsecond=0)
            nxt_reset = reset + period
            left = nxt_reset - now
            hrs = int(left.total_seconds() // 3600)
            lines = [f"◆ 本周为第 {wk_no}/{len(weeks)} 周（周一 00:00 UTC 换轮）"]
            for it in cur:
                name = it.get("cn") or it.get("en")
                var = it.get("variant") or ""
                same = var.lower().replace(" ", "") == (it.get("en") or "").lower().replace(" ", "")
                lines.append(f"· {name}" + (f"（{var}）" if var and not same else ""))
            lines.append("◆ 下周：" + "、".join(x.get("cn") or x.get("en") for x in nxt))
            lines.append(f"※ 距换轮 {hrs // 24}天{hrs % 24}小时　"
                         f"（本周期共 {len(weeks)} 周，循环往复）")
            lines.append("※ 数据源：Update 43 官方轮换表；锚点 Week "
                         f"{anchor_week} 起于 {epoch.strftime('%Y-%m-%d')}（周一 UTC）")
            lines.append("※ 每周可在钢铁回廊 Tier 5 / Tier 10 各选 1 个灵化适配器")
            return Reply(title, lines)

        # —— 模式：批次轮换（Coda 终幕：A/B 两批每 4 天交替）——
        batches = data.get("batches") or []
        if batches:
            epoch = datetime.fromisoformat(data["epoch"])
            period = timedelta(hours=int(data.get("period_hours", 96)))
            now = datetime.now(timezone.utc)
            passed = int((now - epoch) // period)
            anchor = int(data.get("anchor_idx", 0))
            idx = (anchor + passed) % len(batches)
            labels = data.get("batch_label") or [str(i + 1) for i in range(len(batches))]
            cur, nxt = batches[idx], batches[(idx + 1) % len(batches)]
            nxt_at = epoch + period * (passed + 1)
            hrs = max(0, int((nxt_at - now).total_seconds() // 3600))
            days = int(data.get("period_hours", 96)) // 24
            lines = [f"◆ 当前为 {labels[idx]} 批（共 {len(batches)} 批轮换，每 {days} 天换一次）"]
            lines.extend(_weapon_rows(cur))
            lines.append(f"◆ 下一批 {labels[(idx + 1) % len(batches)]}："
                         + "、".join(x.get("cn") or x.get("en") for x in nxt))
            lines.append(f"※ 距换批 {hrs // 24}天{hrs % 24}小时"
                         f"（{nxt_at.strftime('%m-%d %H:%M')} UTC）")
            _vn = self._valence_note(data, epoch + period * passed)
            if _vn:
                lines.append(_vn)
            if data.get("_note"):
                lines.append(f"※ {data['_note']}")
            return Reply(title, lines)

        # —— 模式：常驻 + 定期刷新（Tenet 信条：武器常驻，元素加成每 4 天重生成）——
        items = data.get("items") or []
        if items and isinstance(items[0], dict):
            epoch = datetime.fromisoformat(data["epoch"])
            period = timedelta(hours=int(data.get("period_hours", 96)))
            now = datetime.now(timezone.utc)
            passed = int((now - epoch) // period)
            nxt_at = epoch + period * (passed + 1)
            hrs = max(0, int((nxt_at - now).total_seconds() // 3600))
            days = int(data.get("period_hours", 96)) // 24
            lines = [f"◆ 常驻 {len(items)} 把（随到随买，各 40 个腐化全息密钥）"]
            lines.extend(_weapon_rows(items))
            lines.append(f"※ 元素加成每 {days} 天重生成　距下次 {hrs // 24}天{hrs % 24}小时"
                         f"（{nxt_at.strftime('%m-%d %H:%M')} UTC）")
            _vn = self._valence_note(data, epoch + period * passed)
            if _vn:
                lines.append(_vn)
            if data.get("_note"):
                lines.append(f"※ {data['_note']}")
            return Reply(title, lines)

        # —— 模式二：滑动窗口 ——
        items = data.get("items") or []
        if not items:
            return Reply(title, ["该轮换的数据表（core/data/rotations.json -> "
                                 f"{key}）尚未接线，请按当前版本校准后填入"])
        epoch = datetime.fromisoformat(data["epoch"])
        hours = max(1, int(data.get("period_hours", 168)))
        pick = max(1, int(data.get("pick", 1)))
        slot = int((datetime.now(timezone.utc) - epoch).total_seconds() // 3600 // hours)
        cur = [items[(slot * pick + i) % len(items)] for i in range(pick)]
        nxt = [items[((slot + 1) * pick + i) % len(items)] for i in range(pick)]
        lines = ["当前：" + "、".join(cur), "下一轮：" + "、".join(nxt)]
        return Reply(title, lines)

    async def _h_rotation_incarnon(self, parsed, event, platform) -> Reply:
        return self._rotation_lines("incarnon", "本周钢铁回廊灵化")

    async def _h_rotation_tenet(self, parsed, event, platform) -> Reply:
        return self._rotation_lines("tenet", "Ergo 信条武器轮换")

    async def _h_rotation_coda(self, parsed, event, platform) -> Reply:
        return self._rotation_lines("coda", "Coda 终幕武器轮换")

    async def _h_acrichis(self, parsed, event, platform) -> Reply:
        # 本周货单是社区维护快照（DE 不下发），过期后回落到静态商品池
        week = await self.client.acrithis_week()
        if week:
            title, lines = fmt.fmt_acrichis_week(week)
        else:
            # 过期时必须明说「这只是候选池」，否则会被当成本周实际在卖的 5 件
            title, lines = fmt.fmt_acrichis(
                await self.client.acrithis_pool(), stale=True)
        return Reply(title, lines, footer=fmt.fmt_platform_footer(platform))

    async def _h_descendia(self, parsed, event, platform) -> Reply:
        data = await self.client.descendia(platform)
        title, lines = fmt.fmt_descendia(data)
        return Reply(title, lines, footer=fmt.fmt_platform_footer(platform))

    async def _h_incursions(self, parsed, event, platform) -> Reply:
        data = await self.client.steel_path_incursions(platform)
        title, lines = fmt.fmt_incursions(data)
        return Reply(title, lines, footer=fmt.fmt_platform_footer(platform))

    # ------------------------------------------------------------------
    # 资料与计算器
    # ------------------------------------------------------------------
    async def _h_help(self, parsed, event, platform) -> Reply:
        """裸「帮助」：单页指令总览（2026-09-14 应用户要求去掉「帮助 分类」
        子指令——意义不明，一页足够）。

        2026-09-19 加注脚：本插件指令是**裸词**触发（AstrBot 会把 "/" 前缀
        剥掉，见文件头「指令空间避让」），而 "/help" 属于 AstrBot 内置指令，
        已被本插件主动让路 —— 说明一句，免得用户以为帮助坏了。
        """
        lines = ["※ 指令直接发即可，无需 / 前缀（/help 属系统内置指令）"]
        for t, items in HELP_TOPIC.items():
            lines.append(f"◆ {t}")
            # 全角空格分隔：渲染层按此切成两列并做首字符垂直线对齐
            lines += [f"· {c}　{d}" for c, d in items]
        return Reply(f"{BRAND} 指令一览", lines,
                     footer=fmt.fmt_platform_footer(platform))

    # ------------------------------------------------------------------
    # wiki（资料）
    # ------------------------------------------------------------------
    def _kb_hint(self) -> str:
        """知识库（AstrBot 平台侧 RAG）引导 —— 只在本地查不到时补一句。

        插件**不直连**知识库：检索由 AstrBot 平台提供，插件只负责告诉用户
        「有这份文档、放哪」。开源版用户没配知识库时可以关掉（面板
        「知识库引导」= 否），免得每次都多一句。
        """
        if not self.cfg.get("kb_enabled", True):
            return ""
        docs_dir = str(self.cfg.get("kb_docs_dir") or "kb").strip() or "kb"
        kb_id = str(self.cfg.get("kb_id") or "").strip()
        tail = f"（知识库 id：{kb_id}）" if kb_id else ""
        return (f"\n\n💡 推荐 / 攻略类问题可把 {docs_dir}/ 下的文档"
                f"传进 AstrBot 知识库{tail}")

    def _wiki_intro_on(self) -> bool:
        """wiki 卡片开关（默认开）。

        卡片是**分层出图**的：遗物卡 / 部件反查卡 / 最小兜底卡只依赖随包分发的
        `relic_index.json`、`relic_inverse.json`，**市场版也有**；只有「简介卡」
        正文需要 `core/data/wiki_intro.json`（**不进开源 / 市场包**）——
        数据缺失时该类条目出「最小卡」（一行说明 + 可点链接），不再是纯文本。
        启动日志会提示数据缺失（见 initialize）。
        """
        return bool(self.cfg.get("wiki_intro", True))

    def _wiki_reply(self, page_name: str, url: str, alts: list[str],
                    platform, *names: str, variants: list[str] | None = None) -> Reply:
        """wiki 结果统一出口：有简介数据 → 卡片 + 链接；没有 → 纯文本链接。

        链接必须**可点击**（issue #1 需求②），所以卡片之外永远另发一段
        Plain 文本；只有链接时保持纯文本直发（渲成图片会把 URL 烧死在图里）。
        查询没指明变体时 ``variants`` 给出「变体：…」一行（用户口径：
        没指明就只介绍基础的，变体在下面说明）。
        """
        var_line = f"变体：{'、'.join(variants)}" if variants else ""
        link = f"🔗 wiki 链接：{url}"
        if alts:
            link += f"\n同类候选：{'、'.join(alts)}"
        if self._wiki_intro_on():
            card = wiki_intro.card_for(page_name, *names)
            if card:
                title, lines = card
                if var_line:
                    lines = [*lines, var_line]
                return Reply(title, lines, extra_text=link,
                             footer=fmt.fmt_platform_footer(platform))
        lines = [f"📖 {page_name}", url]
        if var_line:
            lines.append(var_line)
        if alts:
            lines.append(f"同类候选：{'、'.join(alts)}")
        return Reply("wiki 直达", lines, text_only=True,
                     footer=fmt.fmt_platform_footer(platform))

    async def _h_wiki(self, parsed, event, platform) -> Reply:
        """维基页面直达。

        先走本地 12 条概念页，再走统一索引拼页面名；只有都落空才给搜索链接。
        页面名按**国际服**口径（DE 官方简中不翻译战甲名，见
        ``search_engine.wiki_page_name``）；查询没指明变体（p / prime / 亡魂…）
        时介绍基体并列一行变体，指明了就直接给该变体。配了简介数据时附卡片。
        """
        query = parsed.content_str
        if not query:
            return Reply(raw_text="用法：wiki 关键词（本地词库优先，未命中给搜索链接）")
        hit = self.client.wiki_lookup(query)
        if hit:
            return Reply(raw_text=f"📖 {hit['title']}\n{hit['url']}")
        from urllib.parse import quote as _q

        want_variant = matching.variant_intent_any(query)
        found = search_engine.search(query, limit=3)
        if not found and want_variant:
            # 「摸尸p」这类带 p 后缀的查询词库里没有（别名表只登记无后缀形态）
            # → 剥掉 p/prime 再查，命中后按变体解析（want_variant 已为 True）
            stripped = matching.strip_variant_query(query)
            if stripped and stripped != query:
                found = search_engine.search(stripped, limit=3)
        if found:
            # 黑话命中（别名）时页面名取国际服官方名——别名键/国服旧译都不是
            # wiki 页面（「wiki 音妈」「wiki 摸尸」实测死链，2026-09-24）。
            best_name = search_engine.wiki_page_name(found[0],
                                                     base=not want_variant)
            # 灰机标题归一：含中文的名字要去掉空格与中点（「玻之武杖 Prime」→
            # 「玻之武杖Prime」），否则页面不存在（2026-09-25 浏览器 API 实测）
            page = _q(search_engine.wiki_title(best_name).replace(" ", "_"))
            alts = [f["name"] for f in found[1:] if f["name"] != best_name]
            return self._wiki_reply(
                best_name, f"https://warframe.huijiwiki.com/wiki/{page}",
                alts, platform, found[0].get("name") or "",
                found[0].get("en") or "",
                query,          # 前缀命中（「电路」→「电路效果」）时按原词找卡片
                variants=None if want_variant
                else wiki_intro.variants(best_name))
        # WM 中文名 → 灰机 wiki 对应页面（用官方名构造 URL）
        try:
            item = await self.client.resolve_wm_item(query)
        except WarframeAPIError:
            item = None
        if item and (item.get("zh") or item.get("en")):
            # WM 的展示名带「一套/蓝图」这类后缀；有 slug 就走 slug（国际服名）
            slug = str(item.get("url_name") or "").strip()
            if slug:
                page_name = search_engine.page_name_from_slug(
                    search_engine.base_slug(slug) if not want_variant else slug)
            else:
                page_name = search_engine.wiki_page_name(
                    {"name": item.get("zh") or "", "en": item.get("en") or ""},
                    base=not want_variant)
            page = _q(search_engine.wiki_title(page_name).replace(" ", "_"))
            return self._wiki_reply(
                page_name, f"https://warframe.huijiwiki.com/wiki/{page}",
                [], platform, item.get("zh") or "", item.get("en") or "",
                variants=None if want_variant
                else wiki_intro.variants(page_name))
        link = await self.client.wiki_search_link(query)
        return Reply(raw_text=f"本地词库未收录「{query}」，请前往维基搜索：\n{link}"
                              f"{self._kb_hint()}")

    async def _h_valence(self, parsed, event, platform) -> Reply:
        """玄骸 / 信条 / 科达武器的**效价融合**（Valence Fusion）。

        官方规则：两者必须是**同一种**武器；结果 = 较高值 × 1.1（≥58% 直接给 60%）；
        元素由玩家在两者间二选一，**不会**合成复合元素。
        """
        import re as _re
        toks = parsed.content or []
        pairs: list[tuple[str, float]] = []
        for tok in toks:
            m = _re.fullmatch(r"([^\d]*)(\d+(?:\.\d+)?)%?", tok)
            if m and m.group(2):
                pairs.append((m.group(1).strip(), float(m.group(2))))

        if len(pairs) >= 2:
            (e1, p1), (e2, p2) = pairs[0], pairs[1]
            has_elem = bool(e1 or e2)
            res = calc.valence_fusion(e1 or "火", p1, e2 or "火", p2)
            if "error" in res:
                return Reply(raw_text=res["error"])
            # 没给元素时不硬编一个「火」，也不输出问号
            if has_elem:
                labels = [f"{e or '—'}{p:g}%" for e, p in pairs[:2]]
            else:
                labels = [f"{p:g}%" for _, p in pairs[:2]]
            lines = [
                f"◆ 融合材料：{' ＋ '.join(labels)}",
                f"◆ 融合结果：加成 {res['percent']:g}%",
                f"　公式 结果 = 较高值 {max(p1, p2):g}% × 1.1"
                + (" → ≥58% 直接进位 60%" if res["percent"] >= 60 else ""),
            ]
            # 没给元素时别硬编一个「火」，也别输出「?」
            if has_elem:
                lines.insert(2, f"◆ 元素：{' 或 '.join(res['options'])}"
                                f"（{res['note']}）")
            if res["percent"] < calc.VALENCE_CAP:
                steps = calc.fusion_to_cap(res["percent"])
                lines.append(f"◆ 从 {res['percent']:g}% 到满值还需 {len(steps)} 次融合"
                             f"（用同数值材料）")
            lines.append("※ 必须是同款武器（同一把 Kuva/Tenet/Coda，同系列不同名不行）")
            lines.append("※ 只有受体会被保留：材料的催化剂 / Forma / 架式 Forma / 透镜"
                         "都不转移，务必用投资多的那把当受体")
            lines.append("※ 满值判定：≥58% 即进位到 60%，所以材料 ≥52.8% 可一步满值")
            return Reply("效价融合", lines)

        if len(pairs) == 1:
            _e1, p1 = pairs[0]
            trace = calc.fusion_to_cap(p1)
            lines = [f"◆ 从 {p1:g}% 用同数值材料融合到 60% 的轨迹：",
                     "　→ ".join(f"{x:g}%" for x in trace) or "已是满值",
                     f"◆ 共需 {len(trace)} 次融合（材料数值越高可少几次）"]
            lines.append("※ 材料 ≥58% 时受体无需再看自身数值，融合后直接 60%")
            return Reply("效价融合规划", lines)

        return Reply(raw_text="用法：\n"
                              "· 武器融合 电60 火58　两把融合的结果与可选元素\n"
                              "· 武器融合 44　　　　　从 44% 到 60% 需要融合几次\n"
                              "支持元素：电 / 火 / 冰 / 毒 / 冲击 / 磁力 / 辐射")

    async def _h_damage(self, parsed, event, platform) -> Reply:
        """伤害计算器 v1：武器 + 配卡加成 → 对指定派系/等级敌人的期望伤害。

        用法：伤害 <武器名> [G系/C系/I系/炽蛇军/科腐者…] [N级] [爆头]
                  [基伤N] [多重N] [暴率N] [暴伤N] [派系N] [电/火/冰/毒N] [基甲N]
        公式与机制基准见 core/damage_calc.py 模块注释（U36 抗性重构后）。
        """
        spec, name_tokens = dc.parse_args(parsed.content)
        query = " ".join(name_tokens).strip()
        if not query:
            return Reply("伤害计算", [
                "◆ 用法",
                "　伤害 <武器名> [对 敌人名] [派系] [N级] [爆头] [加成项…]",
                "　例：伤害 布拉玛 对 重机枪手 100级 膛线 分裂膛室 地狱火 电90",
                "　例：伤害 空刃 对 重机枪手 100级 钢铁凤凰 异况超量 急进猛突 连击120",
                "◆ 配卡（三种写法可混用）",
                "　· MOD 名（满级值，可整条配卡直接粘贴）",
                "　　膛线 / 分裂膛室 / 镀层分裂膛室 / 地狱火 / 关键延迟 / 瞄准目标 …",
                "　· 手写：基伤 / 多重 / 暴率 / 暴伤 / 爆头倍率 / 派系（%）",
                "　　电 火 冰 毒（自动合成复合）｜冲击 穿刺 切割（只加同类型）",
                "　　状态伤害N（只放大 DoT）｜基甲N（覆盖默认敌人基准甲）",
                "　· 敌人名：枪兵 / 重机枪手 / 轰击者 / 屠夫 / 船员 …（支持模糊）",
                "　　给了就用它的真实基准甲/血/盾，并算击杀发数",
                "◆ 异常（U36 后元素差异的主要来源）",
                "　病毒N / 磁力N（1-10 层，对血 / 对盾加伤）",
                "　腐蚀N（剥甲 26%+6%×每层，满层 −80%）｜火剥甲（−50%）",
                "◆ 近战",
                "　连击N 重击｜架势名（钢铁凤凰 / 猎鹰俯击 …：每段倍率+强制异常）",
                "　急进猛突 / 创口溃烂 / 异况超量 / 一击必杀 / 奋力一掷 按实际等级折算",
                "◆ 其它",
                "　派系：G系 / C系 / I系 / 合一众 / 奥罗金 / 低语者 / 扎里曼 / 炽蛇军 / 科腐者 …",
                "　赋能 <名>（条件触发只展示不折算）｜异常N（目标异常种类数）",
                "　镀层N / 满镀层（镀层类击杀堆叠按 N 层计入，如镀层 分裂膛室）",
                "　超宏[N]（不吃护甲/派系、免疫异常）｜适应N（Sentient 适应 1-4 层）",
                "　灵化 / 基础形态（有灵化数据的武器默认开灵化形态）",
                "　空战 / 地面（Archgun 双部署：默认地面・大气；空战用空战面板）",
                "　进化 基伤/暴击/爆头…（灵化进化选项，可多次；改基础面板）",
                "　赤毒辐射60（赤毒/信条/终幕回响加成 25-60%，元素要写）",
                "◆ 输出",
                "　单发对血/对盾（无暴击）→ 暴击与爆头期望（含暴击）",
                "　→ 每次扳机、DPS 爆发/持续（均给含暴击与无暴击）",
            ])

        try:
            weapon, alts = dc.find_weapon(query)
            if weapon is None:
                miss = "、".join(spec.get("unknown") or []) or "—"
                return Reply("伤害计算", [
                    f"武器库（warframe-items）里没找到「{query}」。",
                    f"　本串里没认出来的词：{miss}",
                    "　换英文名或完整中文名再试，例如「Kuva Bramma / 赤毒布拉玛」。",
                ])
            res = dc.calculate(spec, weapon)
            return Reply("伤害计算", dc.card_lines(weapon, spec, res, alts))
        except Exception as exc:  # noqa: BLE001 - 绝不静默：群聊里没输出最难查
            logger.warning(f"[sdjk] 伤害计算失败：{exc!r}")
            return Reply("伤害计算", [
                f"❗ 计算出错：{type(exc).__name__}: {exc}",
                f"　武器：{query}｜已识别 MOD："
                + ("、".join(m.get("zh") or m.get("name") or ""
                             for m in spec.get("mods") or []) or "无"),
            ])

    # ------------------------------------------------------------------
    # 配卡截图识别
    # ------------------------------------------------------------------
    async def _h_scan(self, parsed, event, platform) -> Reply:
        """识卡：读游戏内「升级」界面截图，按**真实 MOD 等级**折算配卡加成。

        视觉模型只负责读字（MOD 名 / 卡片容量数字 / 面板数值），加成由
        `core/loadout_ocr.py` 按容量数字反推出的实际等级重算，再用面板数值校验。
        """
        if not self._event_has_image(event):
            return Reply("配卡识别", [
                "用法：发一张武器「升级」界面截图，配上文字「识卡」",
                "　读出：武器（含等级）＋每张 MOD 卡与其实际等级＋面板数值",
                "　加成按卡片容量数字反推实际等级（绿=匹配减半／红=不合+25%／白=原价）",
                "　再用面板数值交叉校验；第 2 页直接给伤害详情（同「伤害计算」公式）",
                "　想换敌人/等级/爆头复算：识卡伤害 对 重机枪手 150级 爆头",
                "　需要 AstrBot 里配好一个多模态模型（如 Qwen3-VL / glm-4.1v）",
            ])

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
                return Reply(raw_text=f"⏳ 识卡太频繁了，请 {_left:.0f} 秒后再试\n"
                                      "（面板「识卡冷却秒数」可调，设 0 关闭限制）")
            # 顺手清掉过期记录，避免长期运行把字典撑大
            if len(self._ocr_last) > 512:
                _now = time.time()
                for _k in [k for k, v in self._ocr_last.items()
                           if _now - v > max(_cd * 4, 120)]:
                    self._ocr_last.pop(_k, None)
            self._ocr_last[_sender] = time.time()
        _cap = int(_num(self.cfg, "scan_max_concurrent", 3))
        if _cap > 0 and len(self._ocr_busy) >= _cap:
            return Reply(raw_text=f"⏳ 机器人正在处理其它识卡请求"
                                  f"（并发上限 {_cap}），请稍后再试")

        # 同一会话串行：识卡是「多次 vision 调用 + 双页渲染」的重活，
        # 连发会让容器 CPU 争抢（实测单张渲染 0.7s → 39s）。
        _busy_key = f"{getattr(event, 'unified_msg_origin', '') or ''}"
        if _busy_key and _busy_key in self._ocr_busy:
            return Reply(raw_text="上一张配卡还在识别中（约 15~30 秒），"
                                  "请稍等一下再发，避免任务叠加变慢")
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
            return Reply(raw_text="截图识别失败：视觉渠道没给出可解析结果。"
                                  "可能原因：① 渠道超时/返回空；② 未配视觉模型。"
                                  "可先重发一次；仍失败请查看机器人日志里的"
                                  "「配卡识别」条目（会写明是哪个渠道、什么原因）")

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
            if len(self._last_scan) > 128:      # 防长期累积
                for _k in list(self._last_scan)[:len(self._last_scan) - 128]:
                    self._last_scan.pop(_k, None)

        detail = self._loadout_detail_lines(an) if cached_spec is not None else None
        if detail:
            return Reply(pages=[("配卡识别", lines),
                                ("配卡识别 · 伤害详情", detail)])
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
            out.append("※ 与「伤害计算」同一套公式（G系 100 级·身体，无 buff 基准）；"
                       "换敌人/等级/爆头请发「识卡伤害 对 重机枪手 150级 爆头」")
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
            same_umo = [(k, v) for k, v in self._last_scan.items()
                        if k.startswith(umo + "|") or k == umo]
            if same_umo:
                hit = max(same_umo, key=lambda kv: kv[1][0])[1]
        if not hit or time.time() - hit[0] > 1800:
            for k in [k for k in self._last_scan
                      if k == umo or k.startswith(umo + "|")]:
                self._last_scan.pop(k, None)
            return Reply("识卡伤害", [
                "本会话 30 分钟内没有识卡记录。",
                "　先发「识卡 + 武器升级界面截图」，再用本指令复算：",
                "　　识卡伤害 对 重机枪手 150级 爆头",
                "　　识卡伤害 C系 120级｜识卡伤害 满镀层 异常4 连击120 重击",
                "　参数写法与「伤害」指令一致（识卡伤害 = 伤害 + 已识别配卡）。",
            ])
        _ts, weapon, cached = hit
        rest = (parsed.content_str or "").strip()
        try:
            if rest:
                arg_spec, _tokens = dc.parse_args(rest)
                default_spec, _ = dc.parse_args([])
                # 只覆盖「目标/条件」类参数（相对默认值有变化的项），
                # MOD 折算值沿用识卡结果 —— 避免默认 spec 里的零值冲掉折算
                overrides = {k: v for k, v in arg_spec.items()
                             if v != default_spec.get(k)}
                spec = {**cached, **overrides}
            else:
                spec = dict(cached)
            res = dc.calculate(spec, weapon)
            return Reply("识卡伤害", dc.card_lines(weapon, spec, res, []))
        except Exception as exc:  # noqa: BLE001 —— 绝不静默
            logger.warning("[sdjk] 识卡伤害复算失败：%r", exc)
            return Reply("识卡伤害", [
                f"❗ 复算出错：{type(exc).__name__}: {exc}",
                "　重新发一次「识卡」后再试；参数写法见「伤害」指令的用法页。",
            ])

    async def _h_kim(self, parsed, event, platform) -> Reply:
        who = parsed.content_str
        if not who:
            guide = calc.load_kim_guide()
            names = [k for k in guide if not k.startswith("_")]
            return Reply(raw_text="用法：对话助手 角色名\n当前收录：" + "、".join(names))
        res = calc.kim_advice(who)
        if not res:
            return Reply(raw_text=f"未收录角色「{who}」，发送「对话助手」查看已收录列表")
        lines = [f"· {t}" for t in res.get("tips", [])]
        return Reply(f"1999 对话助手：{res['name']}", lines)

    # ------------------------------------------------------------------
    # 市场与查价
    # ------------------------------------------------------------------
    @staticmethod
    def _pick_wm_set_part(parts: list[dict], part: str) -> Optional[dict]:
        """从套装部件里挑出部件词对应的那个；没有返回 None。

        「蓝图/总图」= 战甲/武器**总图**：zh 以「蓝图」结尾且不含其他部件词
        （「机体蓝图」也以蓝图结尾，得排除）。其余部件词按 zh 包含匹配
        （「头部」命中「XX Prime 头部神经光元蓝图」）。
        """
        if part in ("蓝图", "总图"):
            skip = ("机体", "头部", "系统", "枪管", "枪机", "枪托")
            cands = [p for p in parts
                     if (p.get("zh") or "").endswith("蓝图")
                     and not any(w in (p.get("zh") or "") for w in skip)]
            return cands[0] if cands else None
        for p in parts:
            if part in (p.get("zh") or ""):
                return p
        return None

    async def _h_wm(self, parsed, event, platform) -> Reply:
        pass

        q = parse_wm(parsed.content, parsed.preset)
        if q.group_buy:
            return await self._wm_group_buy(q, platform)
        if not q.item:
            return Reply(raw_text="用法：wm 物品名 [部件] [收购|合购a*2,b] [N个] [零级/满级/N级] "
                                  "[完整/优良/无暇/光辉] [-r]\n"
                                  "部件：蓝图（总图）/ 机体 / 系统 / 头部 / 配件（全部部件比价），"
                                  "如 wm 母牛 蓝图")
        item = await self.client.resolve_wm_item(q.item)
        if not item:
            return await self._wm_suggest(q.item)
        # ★ 部件关键词（2026-09-19 用户反馈「wm 母牛 蓝图」出的是整套）：
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
                picked = self._pick_wm_set_part(set_parts, q.part)
                if picked is None:
                    names = [p.get("zh") or p.get("en") or p.get("url_name", "")
                             for p in set_parts]
                    return Reply(raw_text=(
                        f"「{item.get('zh') or item.get('en') or item['url_name']}」"
                        f"没有「{q.part}」这个部件。\n"
                        f"可用部件：{'、'.join(names) or '（未同步到部件表）'}\n"
                        f"也可以发整套看全部：wm {q.item}"))
                item = picked
        rank = q.rank
        if rank == -1:
            rank = 10  # 满级近似
        orders, _info = await self.client.wm_orders(item["url_name"], platform)
        display = item.get("zh") or item.get("en") or item["url_name"]
        title, lines, best, pool = fmt.fmt_wm_orders(
            display, orders, buy=q.buy, page=parsed.page, page_size=self.page_size,
            quantity=q.quantity, rank=rank)
        hint = "" if parsed.whisper or not best else " · 加 -r 生成游戏密语"
        # 套装附带部件参考价（2026-09-14 用户要求）：单查部件走上面的
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
                rows.append({
                    "name": p_.get("zh") or p_.get("en") or p_.get("url_name", ""),
                    "sell": fmt.wm_best_price(po, "sell"),
                    "buy": fmt.wm_best_price(po, "buy"),
                })
            lines.extend(fmt.fmt_wm_set_parts(rows))
            if q.part == "配件":
                lines.append(f"※ 想看某个部件的在售/收购单："
                             f"wm {q.item} 蓝图（或 机体 / 系统 / 头部）")
        reply = Reply(title, lines,
                      footer=fmt.fmt_platform_footer(platform, "warframe.market" + hint))
        if parsed.whisper and pool:
            # -r 给前 5 个卖家各生成一条密语（在线优先同展示顺序，
            # 2026-09-14 用户要求：原来只有第一个）
            whisper_item = item.get("en") or item["url_name"]
            for o in pool[:5]:
                reply.whisper.append(
                    fmt.build_whisper(o, whisper_item, sell=q.buy))
        return reply

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
                return Reply(raw_text=(
                    f"「{query}」是玄骸武器（{_zh}），不在集市物品表里。\n"
                    f"价格用：xh {_zh}　（支持元素/数值筛选，如 xh {_zh} 辐射 50）"))
        items = await self.client.wm_items()
        slugs = [it.get("url_name", "") for it in items]
        names = [(it.get("zh") or it.get("en") or it.get("url_name", "")) for it in items]
        close = difflib.get_close_matches(
            query.lower().replace(" ", "_"), slugs, n=3, cutoff=0.5)
        hits = list(dict.fromkeys(close))
        if len(hits) < 3:
            for h in api_client.fuzzy_hits(query, names, n=3):
                if h not in hits:
                    hits.append(h)
        tip = f"没有找到「{query}」" + (f"，你是不是想找：{'、'.join(hits[:3])}"
                                  if hits else
                                  "（可用英文名或在 core/data/aliases.json 中补词条）")
        return Reply(raw_text=tip)

    async def _wm_group_buy(self, q, platform) -> Reply:
        sellers: dict[str, dict] = {}
        for name, qty in q.group_buy:
            item = await self.client.resolve_wm_item(name)
            if not item:
                return Reply(raw_text=f"合购中有物品未找到：{name}")
            orders, _ = await self.client.wm_orders(item["url_name"], platform)
            sells = [o for o in orders if o.get("order_type") == "sell"
                     and o.get("platform", platform) == platform
                     and (o.get("user", {}).get("status") in ("ingame", "online"))]
            sells.sort(key=lambda o: o["platinum"])
            for o in sells[:5]:
                seller = o["user"]["ingame_name"]
                sellers.setdefault(seller, {"items": {}, "total": 0})
                sellers[seller]["items"][item["url_name"]] = (o["platinum"], qty)
        ranked = sorted(sellers.items(),
                        key=lambda kv: (-len(kv[1]["items"]), sum(
                            p * c for p, c in kv[1]["items"].values())))
        lines = []
        for seller, info in ranked[:3]:
            total = sum(p * c for p, c in info["items"].values())
            covered = len(info["items"])
            lines.append(f"🟢 {seller}｜覆盖 {covered}/{len(q.group_buy)} 件｜合计约 {total}p")
            for url, (p, c) in info["items"].items():
                lines.append(f"　· {url} × {c} = {p * c}p")
        if not lines:
            return Reply(raw_text="暂时没有在线卖家能凑齐合购单")
        return Reply("合购最优卖家（同卖家买齐更省事）", lines,
                     footer=fmt.fmt_platform_footer(platform))

    async def _h_wr(self, parsed, event, platform) -> Reply:
        pass

        q = parse_wr(parsed.content)
        if not q.weapon:
            return Reply(raw_text="用法：wr 武器名 [最新|离线] [r槽/-槽/角槽] [1000p] "
                                  "[零洗/低洗/废洗/N洗] [词条连写如:基多暴负变焦] [2+|3+1] [-r]")
        weapon = await self.client.resolve_riven_weapon(q.weapon)
        if not weapon:
            tips = await self.client.suggest_riven_weapons(q.weapon)
            tip = ("，你是不是想找：" + "、".join(tips)) if tips else \
                "，请使用英文名或补充别名表"
            return Reply(raw_text=f"未找到紫卡武器「{q.weapon}」{tip}")
        url_name = weapon["url_name"]
        rtype = weapon.get("riven_type", "")
        positives = self.client.normalize_riven_stats(q.stats, rtype)
        negatives = self.client.normalize_riven_stats(q.negatives, rtype)
        auctions = await self.client.wm_riven_auctions(
            url_name, platform, positives=positives, negatives=negatives,
            polarity=q.polarity, max_price=q.max_price,
            max_rerolls=q.max_rerolls, min_rerolls=q.rerolls_min)
        neg_set = set(negatives)
        pos_set = set(positives)
        pool = [a for a in auctions if self._auction_match(a, q, neg_set, pos_set)]
        relaxed = ""
        if not pool and auctions and (q.stats or q.negatives):
            # 严格匹配为空时分两档放宽（2026-09-24 用户报障「前排出现不匹配的
            # 项目」——旧实现直接跳到「近似匹配」，而近似排序又被渲染层按
            # 在线+价格重排，于是前排全是便宜但与词条无关的挂单）：
            #   ① 先只放宽**在线状态**：词条完全匹配但卖家离线 → 仍排前面
            #   ② 真没有完全匹配的，才按词条命中率给最接近的选项
            strict_offline = [a for a in auctions
                              if self._auction_match(a, q, neg_set, pos_set,
                                                     ignore_status=True)]
            if strict_offline:
                pool = sorted(strict_offline, key=lambda a: (
                    fmt._ONLINE_RANK.get(
                        (a.get("owner") or {}).get("status") or "offline", 3),
                    a.get("buyout_price") or a.get("starting_price") or 0))
                relaxed = "offline"
        if not pool and auctions and (q.stats or q.negatives):
            # 服务端词条过滤为 OR 语义，精确匹配仍需本地二次筛；严格匹配为空时
            # 按 词条命中率+价格 给出最接近的选项，而不是一句「没有」
            def _rank(a: dict):
                item = a.get("item", {}) or {}
                attrs = item.get("attributes") or []
                urls_p = {at.get("url_name") for at in attrs if at.get("positive")}
                urls_n = {at.get("url_name") for at in attrs
                          if not at.get("positive")}
                pos_hit = len(pos_set & urls_p)
                neg_hit = len(neg_set & urls_n)
                want_neg = 1 if (q.require_negative or q.negatives) else 0
                score = pos_hit * 4 + neg_hit * 2 + (want_neg & (1 if urls_n else 0))
                price = a.get("buyout_price") or a.get("starting_price") or 0
                # 同档内再按「在线优先 → 价格升序」（与渲染层展示口径一致）
                online = fmt._ONLINE_RANK.get(
                    (a.get("owner") or {}).get("status") or "offline", 3)
                return (-score, online, price)
            # 洗数过滤是硬条件，放宽词条时不能把它一起放开
            cand = [a for a in auctions if self._rolls_ok(a, q)] if (
                q.max_rerolls is not None or q.rerolls_min is not None) else auctions
            pool = sorted(cand, key=_rank)[:max(4, self.page_size - 4)]
            relaxed = "loose"
        wname = weapon.get("zh") or weapon.get("en") or url_name
        title, lines, best = fmt.fmt_wr_auctions(
            wname, pool,
            page=parsed.page, page_size=max(4, self.page_size - 4),
            riven_type=rtype, group=weapon.get("group", ""),
            # 放宽档的排序是「词条命中率优先」，必须原样保留 —— 渲染层默认
            # 按 在线+价格 重排会把命中的挂单冲散
            presorted=bool(relaxed))
        if weapon.get("_fuzzy_from"):
            lines.insert(0, f"※ 「{weapon['_fuzzy_from']}」按「{weapon.get('zh') or wname}」查询")
        if relaxed == "offline":
            lines.append("※ 完全符合词条的挂单卖家目前都不在线，"
                         "已按 在线优先 → 价格升序 列出")
        elif relaxed == "loose":
            lines.append("※ 没有完全符合条件的挂单，"
                         "以上按词条命中率与价格给出最接近选项")
        reply = Reply(title, lines, footer=fmt.fmt_platform_footer(platform, "warframe.market 紫卡"))
        if not parsed.whisper:
            reply.lines.append("※ 加 -r 生成游戏内私聊密语（与卖家的 /w 消息）")
        if parsed.whisper and best:
            item = best.get("item", {}) or {}
            price = best.get("buyout_price") or best.get("starting_price")
            reply.whisper.append(
                f"/w {best.get('owner', {}).get('ingame_name', '?')} Hi! I want to buy: "
                f"\"{item.get('name', weapon.get('url_name'))}\" for {price} platinum. (warframe.market)")
        return reply

    async def _h_xh(self, parsed, event, platform) -> Reply:
        """玄骸拍卖查询（WM lich auctions）。

        覆盖**三类**：赤毒 Kuva（type=lich）/ 信条 Tenet（type=sister）/
        科达 Coda（type=coda，WM 未开放则提示无挂单）。
        1986 年这个指令只查了 lich，信条武器必然「未找到」—— 2026-09-18 修。

        分支筛选：``xh 武器名 [元素] [数值]``
          · 元素：中文（辐射/毒素/火焰…）或英文（radiation/toxin…）
          · 数值：伤害加成下限（如 ``50`` 表示只要 ≥50%）
        """
        toks = parsed.content or []
        if not toks:
            return Reply(raw_text=(
                "用法：xh 武器名 [元素] [数值]\n"
                "　例：xh 赤毒怒雷 ｜ xh 信条弧电离子枪 辐射 ｜ xh 赤毒海克 50\n"
                "　元素：磁力/电击/毒素/火焰/冰冻/冲击/切割/辐射\n"
                f"　已收录 {len(self.client._aliases.get('lich_items', {}))} 个中文写法"
                "（赤毒/信条/科达三类）"))
        first = toks[0]
        slug = self.client.resolve_lich_weapon(first) or await self._lich_slug_by_riven(first)
        if not slug:
            near = fuzzy_hits(
                first, list(self.client._aliases.get("lich_items", {})), n=3) or []
            hint = ("；你是不是想找：" + "、".join(near)) if near else ""
            return Reply(raw_text=f"未找到玄骸武器「{first}」{hint}\n"
                                  "支持赤毒/信条/科达三类，可只写后半段（如「怒雷」）")

        info = self.client.lich_weapon_info(slug)
        name = info.get("zh") or slug
        kind = info.get("type") or "lich"
        toks_tail = toks[1:]
        want_eph = any("幻纹" in t for t in toks_tail)
        elem_cn, elem_en = _xh_element(toks_tail)
        min_dmg = next((int(t.rstrip("%")) for t in toks_tail
                        if t.rstrip("%").isdigit() and 1 <= int(t.rstrip("%")) <= 100),
                       None)

        # 表中已标注 wm=False 的（逐把核对过），直接走「无类目」分支，省一次注定 400 的请求
        if info.get("wm") is False:
            return self._xh_no_category(name, slug, kind, platform)
        try:
            auctions = await self.client.wm_lich_auctions(
                slug, platform, lich_type=kind)
        except WarframeAPIError as exc:
            # 市场侧失败要说清原因（限速/网络），不能糊成「内部错误」
            return Reply(raw_text=f"warframe.market 查询失败：{exc}\n"
                                  "多为市场限速（3 请求/秒）或网络抖动，"
                                  "过几秒重试即可。")
        pool = auctions
        if want_eph:
            pool = [a for a in pool if (a.get("item") or {}).get("having_ephemera")]
        if elem_en:
            pool = [a for a in pool if (a.get("item") or {}).get("element") == elem_en]
        if min_dmg is not None:
            pool = [a for a in pool
                    if int((a.get("item") or {}).get("damage") or 0) >= min_dmg]

        # 排序：**在线优先**（能立刻交易）→ 伤害降序 → 价格升序
        def _rank(a: dict):
            o = a.get("owner") or {}
            it = a.get("item") or {}
            on = {"ingame": 0, "online": 1}.get(str(o.get("status") or ""), 2)
            price = a.get("buyout_price") or a.get("starting_price") or 999999
            return (on, -int(it.get("damage") or 0), price)
        pool = sorted(pool, key=_rank)

        filters = []
        if elem_cn:
            filters.append(elem_cn)
        if min_dmg is not None:
            filters.append(f"伤害≥{min_dmg}%")
        if want_eph:
            filters.append("带幻纹")
        title = f"{name} 玄骸拍卖（{len(pool)}条" + \
            ("，" + "·".join(filters) if filters else "") + "）"
        if not pool:
            if self.client.lich_unsupported(slug):
                return self._xh_no_category(name, slug, kind, platform)
            elif not auctions:
                msg = "该武器当前没有挂单（冷门武器挂单少，可过段时间再看）"
            else:
                msg = "暂无符合条件的挂单" + \
                    (f"（筛选：{'·'.join(filters)}）" if filters else "")
            return Reply(title, msg.splitlines(),
                         footer=fmt.fmt_platform_footer(platform, "warframe.market 玄骸"))
        lines = [fmt.fmt_lich_row(i, a) for i, a in enumerate(pool[:12], 1)]
        lines.append("※ 在线优先排序；信用=卖家交易信誉等级（0~5），"
                     "幻纹✦ 表示带幻纹")
        return Reply(title, lines,
                     footer=fmt.fmt_platform_footer(platform, "warframe.market 玄骸"))

    def _xh_no_category(self, name: str, slug: str, kind: str,
                        platform: str) -> Reply:
        """WM 没有该武器的拍卖类目 —— 给**替代路径**，而不是只说「查不到」。

        ★ 2026-09-18 逐把核对过全部 47 把（`lich_weapons.json` 的 ``wm`` 字段）：
          30 把有类目、17 把没有（全部终幕 Coda + 5 把近战/异形信条）。
          用户看到「0 条」时最容易以为插件坏了，所以要写清「是市场没有，
          不是我们没查到」，并给出可操作的替代。
        """
        db = self.client._lich_db()
        has = sum(1 for r in db.values() if r.get("wm") is True)
        total = len(db)
        sibling = {"sister": "信条", "lich": "赤毒", "coda": "终幕"}.get(kind, "")
        example = {"sister": "xh 信条弧电离子枪", "lich": "xh 赤毒怒雷",
                   "coda": "xh 信条弧电离子枪"}.get(kind, "xh 信条弧电离子枪")
        return Reply(
            f"{name} 玄骸拍卖（市场无此类目）",
            [f"warframe.market 没有「{name}」的拍卖类目。",
             f"这**不是识别失败**：中文名已正常匹配到 {slug}，是市场侧没这个类目。",
             f"（已逐把核对全部 {total} 把玄骸武器：{has} 把有挂单、"
             f"{total - has} 把没有）",
             f"· 换一把同系列：发「{example}」",
             f"· 网站自查：warframe.market/zh-hans/auctions/search"
             f"?type={kind}&weapon_url_name={slug}",
             f"· 这类武器（全部终幕 + 部分近战{sibling}）只能游戏内交易频道收"],
            footer=fmt.fmt_platform_footer(platform, "warframe.market 玄骸"))

    async def _lich_slug_by_riven(self, q: str) -> Optional[str]:
        """黑话兜底：尝试从 riven 别名表取基础武器 slug 前缀匹配玄骸。"""
        hit = self.client.alias_lookup(q.lower(), "riven_items")
        return f"kuva_{hit.split('_prime')[0]}" if hit and hit.split('_prime')[0] else None

    def _relic_db(self) -> tuple[dict, dict]:
        from pathlib import Path as _P
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
        from core.parser import TIER_CN
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
            return Reply(raw_text="用法：遗物 后纪A2（查奖励）｜ 遗物 绝路 枪机（部件反查出处）｜"
                                  " 遗物 出库（当前可掉落）｜ 遗物 入库（已入库、不可刷取）")
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
                # （用户 2026-09-17：「列出一大堆重复星系没啥用阿」）。
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

            # 特殊获取渠道标记（用户要求：「有一部分遗物只要指定位置能获取，
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
        return Reply(raw_text=tip)

    async def _h_parts(self, parsed, event, platform) -> Reply:
        """部件反查：部件名 → 出自哪些遗物及槽位（走遗物反查）。"""
        return await self._h_relic(parsed, event, platform)

    async def _h_disposition(self, parsed, event, platform) -> Reply:
        """紫卡倾向查询。"""
        query = parsed.content_str
        weapons = await self.client.wm_riven_weapons()
        if not query:
            top = sorted([w for w in weapons if w.get("disposition")],
                         key=lambda w: -w["disposition"])[:8]
            lines = [f"· {(w.get('zh') or w.get('en') or w['url_name'])}　"
                     f"倾向 {w['disposition']:.2f}" for w in top]
            return Reply("紫卡倾向 Top8（越高越容易出好卡）", lines,
                         footer=fmt.fmt_platform_footer(platform))
        hits, stage = matching.resolve_weapon_name(
            query, weapons, zh="zh", en="en", slug="url_name")
        if not hits:
            # 紫卡黑话别名兜底（riven_items 词库），命中优先级低于官方名各层
            aurl = self.client.alias_lookup(query.lower(), "riven_items")
            if aurl:
                hits = [w for w in weapons if w.get("url_name") == aurl]
                stage = "alias"
        if not hits:
            close = matching.suggest_zh(query, weapons)
            return Reply(raw_text="未找到该武器" + (f"，你是不是想找：{'、'.join(close)}" if close else ""))
        logger.info("[sdjk] 倾向武器解析：%s → %s（%s）",
                    query, "/".join(matching.zh_names(hits)), stage)
        # 只报本体名（无变体意图）→ 列出全部变体家族（本体在前），
        # 对齐 Warframe Rabbit 的家族卡；显式变体查询（绝路p/赤毒沙皇）不展开
        nq = matching.normalize(query)
        if (len(hits) == 1 and not matching.variant_intent(query)
                and not any(t in nq for t in matching.VARIANT_TOKENS)):
            fam = matching.family_of(hits[0], weapons)
            if len(fam) > 1:
                hits = fam
        lines = [f"· {(w.get('zh') or w.get('en') or w['url_name'])}　"
                 f"倾向 {w.get('disposition', 0):.2f}　"
                 # ★ 2026-09-24：带上 group —— WM 把曲翼枪械的 rivenType 也标成
                 #   rifle（翠雀显示成「步枪」），group 才是准的（曲翼枪械/守护武器）
                 f"{fmt.riven_type_cn(w.get('riven_type', ''), w.get('group', ''))}"
                 for w in hits[:8]]
        return Reply(f"紫卡倾向：{query}", lines, footer=fmt.fmt_platform_footer(platform))

    # ------------------------------------------------------------------
    # 排行 / 趋势 / 开核桃（P1 批次）
    # ------------------------------------------------------------------
    _RANK_CN = ("甲", "卡", "部件", "赋能", "主武", "副武", "近战", "遗物")

    @staticmethod
    def _rank_guide(platform) -> Reply:
        """榜单未建立时的引导卡。"""
        return Reply("价格排行", [
            "◆ 全量价格榜单还未建立",
            "　发「排行 刷新」启动后台抓取",
            "　（WM 全量约 3800 项、限速约 20 分钟，期间排行照常可查）",
            "　完成后：「排行」看 甲/武器/卡 三榜，「排行 分类」看 20 名完整榜",
        ], footer=fmt.fmt_platform_footer(platform, "warframe.market"))

    async def _h_rank(self, parsed, event, platform) -> Reply:
        """价格排行：落盘全量榜，按当前成交中位价降序。"""
        content = parsed.content_str or ""
        if "刷新" in content and (parsed.preset or "") != "紫卡":
            started, done, total = self.client.start_rank_crawl()
            state = "已启动新一轮全量抓取" if started else "抓取已在进行中"
            return Reply("排行刷新", [
                f"◆ {state}（WM 全量 {total or '约 3800'} 项，限速约 20 分钟）",
                f"　当前进度：{done}/{total or '?'}",
                "※ 抓取期间排行照常可查（显示已完成部分）；可稍后重发本指令看进度",
            ], footer=fmt.fmt_platform_footer(platform, "warframe.market"))
        cat = parsed.preset or ""
        if not cat:
            for t in parsed.content or []:
                c = t.replace("排行", "").strip()
                if c in self._RANK_CN or c == "紫卡":
                    cat = c
                    break
            else:
                cat = content.strip()
        if cat == "紫卡":
            return await self._h_riven_rank(parsed, platform)
        if not cat:
            rows = self.client.rank_rows()
            if not rows:
                return self._rank_guide(platform)
            title, lines = fmt.fmt_rank_overview(rows)
            return Reply(title, lines,
                         footer=fmt.fmt_platform_footer(platform, "warframe.market 成交"))
        if cat not in self.client.RANK_CATEGORIES:
            return Reply(raw_text=f"未知分类「{cat}」。可选："
                                  + " / ".join(self._RANK_CN)
                                  + "；紫卡排行请发「紫卡排行」")
        rows = self.client.rank_rows()
        if not rows:
            return self._rank_guide(platform)
        title, lines = fmt.fmt_rank_table(cat, rows)
        return Reply(title, lines,
                     footer=fmt.fmt_platform_footer(platform, "warframe.market 成交"))

    async def _h_riven_rank(self, parsed, platform) -> Reply:
        """紫卡热度排行：DE 官方周报（0洗/已洗 中位价与热度，周更）。"""
        content = (parsed.content_str or "").strip()
        try:
            snap = await self.client.de_weekly_rivens(
                platform, force="刷新" in content)
        except Exception as e:  # noqa: BLE001
            return Reply(raw_text=f"紫卡周报暂时拉取失败：{e}")
        zhm = {w.get("en", "").lower(): (w.get("zh") or "")
               for w in await self.client.wm_riven_weapons()}
        cat = ""
        for t in (parsed.content or []):
            k = t.replace("排行", "").strip()
            if k in fmt._RIVEN_ITEM_TYPE or k in ("主武", "副武", "未开"):
                cat = k
                break
        if not cat:
            title, lines = fmt.fmt_riven_weekly(snap, zhm)
            return Reply(title, lines,
                         footer=fmt.fmt_platform_footer(platform, "DE 官方周报"))
        veiled = await self.client.wm_veiled_stats(platform) \
            if cat == "未开" else None
        title, lines = fmt.fmt_riven_type(cat, snap, zhm, page=parsed.page,
                                          page_size=self.page_size,
                                          veiled_wm=veiled)
        return Reply(title, lines,
                     footer=fmt.fmt_platform_footer(platform, "DE 官方周报"))

    @staticmethod
    def _event_has_image(event) -> bool:
        """消息链里是否带图片组件（用于「不支持图片识别」的针对性提示）。"""
        try:
            chain = getattr(getattr(event, "message_obj", None),
                            "message", None) or []
            return any(type(c).__name__ == "Image" for c in chain)
        except Exception:  # noqa: BLE001
            return False

    # vision 渠道优先级：明确的多模态模型 id → id 含视觉关键词 → 当前渠道
    # ⚠️ 实测（2026-09-16，同一张执法者灵化截图 ×3 次对拍真值基准）：
    #    Qwen3-VL-32B-Instruct 伤害行 5/5 全对、零幻觉（但 ~120s，需把
    #    siliconflow 源超时调到 240）；glm-4.1v-thinking-flash 快（20s）
    #    但伤害行反复读串/整轮崩（最差一次 0/9）→ 32B 首位、glm 兜底，
    #    _extract_loadout_from_image 会按序逐个尝试
    _ocr_busy: set = set()        # 正在识卡的会话（并发保护）
    _ocr_last: dict = {}          # 发送者 → 上次识卡时刻（冷却用）
    # 识卡多渠道路由的等待窗口（秒）：窗口内取「校验全过」的最优；
    # 到点即用当前最好结果走聚焦二读，不无限等慢渠道（实测 glm 25 s、
    # 32B 更慢）。窗口 ≳ 最快渠道的响应时间。
    # 2026-09-21 用户报障：13:43 那次识卡三渠道全废（glm-4v-flash 27 s 空返回，
    # 另两个在 40 s 窗口内没赶上）→ 直接「视觉渠道没给出可解析结果」。
    # 同一张图下一次 30B 是 38.8 s 才返回 —— 距 40 s 只剩 1.2 s，窗口太紧。
    # 放宽到 60 s：慢渠道仍能被等到，最坏等待仍在用户可接受范围（识卡本就 15~30 s 级）。
    RACE_WINDOW_S = 60.0
    _render_lock: Optional[asyncio.Lock] = None   # 渲染串行（PIL 吃 CPU）

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
    _VISION_PROVIDER_IDS = ("zhipu/glm-4v-flash",
                            "siliconflow/Qwen/Qwen3-VL-30B-A3B-Instruct",
                            "siliconflow/Qwen/Qwen3-VL-8B-Instruct")
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
                logger.warning("[sdjk] 配置的 vision_provider_id「%s」取不到，"
                               "回落到内置候选", want)
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
                pid = getattr(getattr(prov, "meta",
                                      lambda: None)(), "id", "") or ""
            except Exception:  # noqa: BLE001
                pid = ""
            if any(h in str(pid).lower() for h in self._VISION_ID_HINTS) \
                    and not any(p is prov for p in out):
                out.append(prov)
        if not out:
            # 失败时把实际存在的渠道名打出来，便于一眼看出是配置还是代码问题
            ids = []
            for prov in allp:
                try:
                    ids.append(getattr(getattr(prov, "meta",
                                               lambda: None)(), "id", "") or "?")
                except Exception:  # noqa: BLE001
                    ids.append("?")
            logger.warning("[sdjk] 没有可用的视觉渠道！现有渠道 %d 个：%s",
                           len(ids), "、".join(ids[:20]) or "（空）")
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
        import base64 as _b64
        out: list[str] = []
        try:
            chain = getattr(getattr(event, "message_obj", None),
                            "message", None) or []
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

    async def _vision_race_json(self, prompt: str, image_url: str,
                                provs: list, *, k: int = 2,
                                tag: str = "识别",
                                window: Optional[float] = None) -> Optional[dict]:
        """向最多 k 个 vision 渠道**并行**请求，取第一个能解析出 JSON 的结果。

        串行试渠道时每条指令要等最慢的那家（实测紫卡识别 13 s）；
        并行竞速后延迟 = 最快渠道的响应时间。失败/解析不出 JSON 的渠道
        自动让位，都不会影响正确性（拿到的必须是能解析的结果）。
        window（秒）：整场竞速的等待上限——到点即返回当前已有结果（None），
        防止渠道级超时（如 siliconflow 240s）把用户晾在原地（2026-09-23 补）。
        """
        import time as _t
        provs = [p for p in (provs or []) if p is not None][:max(1, k)]
        if not provs:
            return None

        async def _one(prov):
            _t0 = _t.perf_counter()
            import uuid as _uuid
            resp = await prov.text_chat(
                prompt=prompt,
                session_id=f"sdjk-{tag}-{_uuid.uuid4().hex[:8]}",
                image_urls=[image_url])
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
                    pending, timeout=timeout,
                    return_when=asyncio.FIRST_COMPLETED)
                for t in done:
                    try:
                        prov, text, ms = t.result()
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("[sdjk] %s渠道异常：%s", tag, exc)
                        continue
                    if not text:
                        logger.warning("[sdjk] %s渠道返回空（%.0f ms）", tag, ms)
                        continue
                    data = lo.parse_vision_json(text)
                    if data:
                        logger.info("[sdjk] %s命中渠道（%.0f ms）：%s", tag, ms,
                                    json.dumps(data, ensure_ascii=False)[:200])
                        winner = data
                        break
                    logger.warning("[sdjk] %s JSON 解析失败（%.0f ms）：%s",
                                   tag, ms, text[:160])
                if winner:
                    break
        finally:
            for t in tasks:
                t.cancel()
        return winner

    async def _extract_riven_from_image(self, image_url: str) -> Optional[dict]:
        """调 vision 渠道从紫卡截图提取词条，返回解析后的 dict 或 None。"""
        import json as _json
        prompt = (
            "你是 Warframe 紫卡识别器。从这张紫卡截图中提取信息，"
            "只输出一行 JSON（不要 markdown 围栏、不要解释）：\n"
            '{"weapon": "武器名（卡面简中，如 欧玛 / 棱晶·欧玛 / 哈利卡）",'            ' "positive": [["词条缩写", 数值], ...],'
            ' "negative": [["词条缩写", 数值], ...]}'
            "\n"
            "词条缩写用：基伤/暴伤/暴击/攻速/范围/多重/触发/持续/效率/装填/"
            "弹速/滑暴/冲击/穿刺/切割/电击/火焰/冰冻/毒素/磁力/辐射等，"
            "负词条也放 negative。数值只写数字（去掉 % 和 m 单位）。\n"
            "⚠ 负词条常写成乘数形式，如「x0.55 对 Corpus 的伤害」——"
            "这类必须写进 negative：词条名用「对Corpus伤害」（或 Grineer/"
            "Infested），数值写 55（即 (1−0.55)×100，保留两位小数即可）。"
            "卡面右下角的数字是内融值，与倾向无关，不要输出倾向。"
            "若截图里出现变体前缀（棱晶/Prime/亡魂/破坏者/赤毒/信条，"
            "或 Prisma/Wraith/Vandal/Kuva/Tenet），务必保留在 weapon 里"
            "（紫卡卡面通常只写母武器名，没有前缀就照原样输出）。\n"
            "⚠ 词条数值**带负号**的（卡面写成「-63.4% 滑行攻击暴击几率」），"
            "必须放进 negative，数值写正数 63.4 —— 放进 positive 会让整张卡"
            "被判成「词条数不对」而失败。\n"
            "⚠ weapon 只填**中文武器名**（卡面第一行的中文部分，如「翁」「视使之触」）。"
            "名字后面那串拉丁文是紫卡自命名（Acri-paracron / Locti-acrium 之类），"
            "不要输出它、也不要把它音译成中文，更不要把词条名混进 weapon。\n"
            "⚠ 数值要连**小数点**一起读：卡面「+115.7%」就是 115.7，不能读成 1157；"
            "「-110.8%」就是 110.8。点号看不清时宁可按最接近的两位有效数字估，"
            "也不要直接丢掉点号。")
        try:
            # 并行竞速（原来串行试渠道，实测要 13 s）；窗口 60s（2026-09-23 补：
            # 渠道级超时可达 240s，不能让用户干等）
            data = await self._vision_race_json(prompt, image_url,
                                                self._vision_providers(),
                                                k=2, tag="紫卡识别",
                                                window=self.RACE_WINDOW_S)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[sdjk] 紫卡识别失败：%s", exc)
            return None
        if not data:
            logger.warning("[sdjk] 紫卡识别：所有渠道都没给出可解析结果")
        return data

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
            logger.info("[sdjk] 豆子检测：装备区 %d 行，豆数 %s",
                        len(eq), [r.get("counts") for r in eq])
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

    @staticmethod
    def _fit_scan_image(image_url: str, min_width: int = 1600,
                        max_width: int = 1600) -> str:
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
                return image_url                      # 已在目标区间，原样送
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
            logger.info("[sdjk] 配卡截图 %dx%d → %dx%d（已%s，目标宽 %d）",
                        w, h, nw, nh, action, target)
            return f"data:image/{fmt.lower()};base64,{new_b64}"
        except Exception:  # noqa: BLE001 —— 规整失败就用原图，别让识卡挂掉
            return image_url

    async def _extract_loadout_validated(self, image_url: str,
                                         max_attempts: int = 2) -> Optional[dict]:
        """识别 + 校验闭环：面板校验不过就换渠道/重试一次，取最好的结果。

        视觉模型偶发漏行/读串（v1.11 实测：毒素行整行漏掉、总计挂到
        穿刺名下），单次识别不可全信 —— 用 analyze 的面板校验当裁判，
        最多试 max_attempts 次；仍不过时追加一次「只读伤害栏」的聚焦
        识别（任务越窄读得越准），把干净的行拼接回去再验。
        """
        # ★ 豆子检测要在**缩放前**的原图上做（_detect_pips 的说明）
        pips_rows = await self._detect_pips(image_url)
        image_url = self._fit_scan_image(image_url)
        best: Optional[tuple[int, dict]] = None   # (失败项数, ocr)

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
                logger.info("[sdjk] 豆子对齐：采用 %s 张 / 冲突 %s 张（检测到 %s 行）",
                            st.get("used"), st.get("conflict"), st.get("rows"))
                if not st.get("used"):
                    logger.warning("[sdjk] ★ 豆子检测到了但**对齐无解**，本次退回容量反推"
                                   "（截图存到 scan_debug/ 便于排查）")
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
                    logger.warning("[sdjk] ★ 疑似漏读：模型只读到 %d 张，但像素网格里"
                                   "已有 %d 格有豆 → 判为漏读并重罚 +%d", n_mods, low, pen)
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
                _names.append(getattr(getattr(_p, "meta", lambda: None)(),
                                      "id", "") or "?")
            except Exception:  # noqa: BLE001
                _names.append("?")
        logger.info("[sdjk] 识卡：并行 %d 个渠道（窗口 %.0f s）：%s",
                    len(provs), self.RACE_WINDOW_S, "、".join(_names))
        loop = asyncio.get_event_loop()
        deadline = loop.time() + max(0.5, float(self.RACE_WINDOW_S))
        tasks = [asyncio.create_task(
                     self._extract_loadout_from_image(image_url, p))
                 for p in provs]
        try:
            pending = set(tasks)
            while pending:
                timeout = deadline - loop.time()
                if timeout <= 0:
                    break
                done, pending = await asyncio.wait(
                    pending, timeout=timeout,
                    return_when=asyncio.FIRST_COMPLETED)
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

    _ROWS_PROMPT = (
        "只读这张 Warframe 截图左侧「伤害」栏里的每一行数值，"
        "按从上到下的顺序输出 JSON（不要 markdown 围栏、不要解释）：\n"
        '{"rows": [["冲击", 545.6], ["穿刺", 34.1], ["切割", 102.3],'
        ' ["毒素", 1125.3], ["总计", 1807.3]]}\n'
        "要求：伤害栏里的每一行都要给（含最后一行「总计」）；"
        "行名与数值严格对齐；数字保留小数、去掉千分位逗号；"
        "看不清的行填 null。除了这个 JSON 什么都不要输出。")

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
                        image_urls=[image_url]),
                    timeout=75)
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
            logger.warning("[sdjk] 聚焦读行 %s 只读到 %d 行，换下一个渠道",
                           pid, len(out))
        return None

    async def _extract_loadout_from_image(self, image_url: str,
                                          prov=None) -> Optional[dict]:
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
                    image_urls=[image_url])
                text = (getattr(resp, "completion_text", "") or "").strip()
            except Exception as exc:  # noqa: BLE001
                logger.warning("[sdjk] 配卡识别 %s 调用失败：%s", pid, exc)
                return None
            ms = (_t.perf_counter() - _t0) * 1000
            ocr = lo.parse_vision_json(text or "") if text else None
            if ocr and (ocr.get("weapon") or ocr.get("mods")):
                logger.info("[sdjk] 配卡识别 %s 返回结构（%.0f ms）", pid, ms)
                return ocr
            logger.warning("[sdjk] 配卡识别 %s 未读出有效内容（%.0f ms）",
                           pid, ms)
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

    # LLM 可能输出词条全称，先归一到缩写
    _STAT_ALIAS = {"滑行暴击": "滑暴", "攻击速度": "攻速", "伤害": "基伤",
                   "装填速度": "装填", "触发几率": "触发", "多重射击": "多重",
                   "暴击几率": "暴击", "元素伤害": "基伤", "射速": "攻速"}
    # 卡面全称/别名 → 标准 id（parser.RIVEN_STAT_ALIASES 反查，首次用时构建）。
    # 必须有这张表：「暴击伤害」走包含匹配会先撞上短名「暴击」（crit_chance），
    # 2026-09-24 实测把暴伤按暴击率的基值算（手枪 149.99 vs 90），区间对不上后
    # 误报「武器名可能识别有误」。
    _STAT_ALIAS_FULL: "dict[str, str] | None" = None

    @classmethod
    def _full_stat_alias(cls) -> dict:
        if cls._STAT_ALIAS_FULL is None:
            try:  # 服务器以包成员加载，相对导入才可靠
                from .core.parser import RIVEN_STAT_ALIASES
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
        if name in full:                  # 全称整表命中（暴击伤害 → crit_damage）
            return full[name]
        if name in rev:
            return rev[name]
        # 包含匹配：长名优先，避免短名抢走全称（「暴击」vs「暴击伤害」）
        for abbr in sorted(rev, key=len, reverse=True):
            if name in abbr or abbr in name:
                return rev[abbr]
        close = difflib.get_close_matches(name, list(rev), n=1, cutoff=0.5)
        return rev[close[0]] if close else None

    @staticmethod
    def _normalize_llm_stats(data: dict, rev: dict) -> tuple[list, list]:
        """LLM 提取结果 → ([(stat_id, float)...], [...])；词条名宽松匹配。"""

        def to_stat(name: str, val):
            if name is None or val is None:
                return None
            name = str(name).strip().replace("%", "").replace("+", "") \
                .replace("-", "")
            try:
                num = float(str(val).strip().rstrip("%m米"))
            except (TypeError, ValueError):
                return None
            sid = WarframeSDJK._stat_id_from_name(name, rev)
            return (sid, num) if sid else None

        pos: list[tuple[str, float]] = []
        neg: list[tuple[str, float]] = []

        # ★ 2026-09-24：卡面负词条常被 vision 整行归进 positive（实测
        #   「-63.4% 滑行攻击暴击几率」→ 4 正 0 负，整卡被词条数校验挡掉）。
        #   规则：**已经放在 negative 的照旧按负词条收**；放在 positive 但
        #   数值带负号的改判为负词条（magnitude 取绝对值）。
        def _route(r, bucket: str):
            sid, num = r
            if bucket == "neg" or num < 0:
                neg.append((sid, abs(num)))
            else:
                pos.append(r)

        for item in data.get("positive") or []:
            r = to_stat(*item)
            if r:
                _route(r, "pos")
        for item in data.get("negative") or []:
            r = to_stat(*item)
            if r:
                _route(r, "neg")
        return pos, neg

    async def _h_riven_analysis(self, parsed, event, platform) -> Reply:
        import time as _tt
        _t_start = _tt.perf_counter()
        """紫卡分析：按 DE 属性基值 × 倾向 × 词条数系数算每条词条的取值区间，
        标出实际数值是高卷还是低卷。

        用法：紫卡分析 武器名 暴伤82.8 范围1.6 攻速45.8 负滑暴81.3
        也可直接发「紫卡分析 + 紫卡截图」（vision 渠道识别，如 glm-4v-flash）。
        负词条用「负」或「-」前缀标记；数值不写正负号。
        """
        import re as _re
        try:  # 服务器以包成员加载，相对导入才可靠（绝对导入会被 sys.path 清理坑掉）
            from .core.parser import RIVEN_STAT_ZH
            from .core import riven_analysis as RA
        except ImportError:  # pragma: no cover - 本地直跑
            from core.parser import RIVEN_STAT_ZH
            from core import riven_analysis as RA
        rev = {v: k for k, v in RIVEN_STAT_ZH.items()}
        disp_override = 0.0  # 手输倾向（倾向0.95 / @0.95 / d0.95）
        stats_pos: list[tuple[str, float]] = []
        stats_neg: list[tuple[str, float]] = []
        weapon_name = ""
        for tok in (parsed.content or []):
            t = tok.strip()
            if not t:
                continue
            neg = t.startswith(("负", "-"))
            body = t[1:] if neg else t
            # ★ 2026-09-24：词条名放宽到 1~8 字（卡面原文「滑行攻击暴击几率」6 字，
            #   旧限 1~4 字会整条落到武器名里 → 负词条丢失、反推区间算错）
            m = _re.fullmatch(r"([\u4e00-\u9fa5]{1,8}?)(\d+(?:\.\d+)?)", body)
            if m:
                sid = self._stat_id_from_name(m.group(1), rev)
                if sid:
                    (stats_neg if neg else stats_pos).append(
                        (sid, float(m.group(2))))
                    continue
            if _re.fullmatch(r"\d\+(?:\d)?", t):
                continue  # 3+1 之类的词条数标注，P/N 直接按实际词条算
            m_d = _re.fullmatch(r"(?:倾向|d|@)(\d+(?:\.\d+)?)", t, _re.I)
            if m_d:
                disp_override = float(m_d.group(1))
                continue  # 手输倾向覆盖（棱晶等变体 WM 没有数据）
            weapon_name += t

        has_image = self._event_has_image(event)
        source_note = ""
        if not weapon_name or not (stats_pos or stats_neg):
            # —— 图片识别路径 ——
            if not has_image:
                return Reply(raw_text="用法：紫卡分析 武器名 词条数值…（负词条加「负」前缀）\n"
                                      "　例：紫卡分析 欧玛 暴伤82.8 范围1.6 攻速45.8 负滑暴81.3\n"
                                      "　也可直接发「紫卡分析 + 紫卡截图」")
            imgs = await self._image_data_urls(event)
            if not imgs:
                return Reply(raw_text="图片下载失败，请重发一次截图")
            data = await self._extract_riven_from_image(imgs[0])
            if not data:
                return Reply(raw_text="图片识别失败（vision 渠道不可用或未配）——"
                                      "请按文字格式发送：紫卡分析 武器名 暴伤82.8 范围1.6 "
                                      "负滑暴81.3")
            stats_pos, stats_neg = self._normalize_llm_stats(data, rev)
            weapon_name = (data.get("weapon") or weapon_name).strip()
            # 紫卡卡面是「武器名 + 自命名」（欧玛 Acri-loctida）：去掉拉丁
            # 尾巴只留简中母名（WM 紫卡表按母武器挂）
            _w = _re.sub(r"[A-Za-z\-].*$", "", weapon_name).strip(" ··")
            if _w:
                weapon_name = _w
            source_note = "（图片识别）"
            if not stats_pos:
                return Reply(raw_text="图片识别到了武器但没读出词条，请按文字格式重发："
                                      "紫卡分析 武器名 暴伤82.8 范围1.6 负滑暴81.3")
            if not weapon_name:
                return Reply(raw_text="图片识别到了词条但没读出武器名，请按文字格式补一次："
                                      "紫卡分析 武器名 " + " ".join(
                                          f"{abbr}{num:g}" for sid, num in stats_pos))

        if not 2 <= len(stats_pos) <= 3 or len(stats_neg) > 1:
            return Reply(raw_text="紫卡词条应为 2~3 条正面 + 0~1 条负面，"
                                  f"当前解析到 {len(stats_pos)} 正 {len(stats_neg)} 负")
        # 武器解析 + 变体倾向查询互不依赖 → 并行（原来串行，实测分析段 4 s）
        _t_res0 = asyncio.gather(
            self.client.resolve_riven_weapon(weapon_name.strip()),
            self.client.resolve_variant_disp(weapon_name.strip()),
            return_exceptions=True)
        weapon, _t_variant = await _t_res0
        if isinstance(weapon, BaseException):
            weapon = None
        if isinstance(_t_variant, BaseException):
            _t_variant = (None, "")
        variant_disp_pre, variant_key_pre = _t_variant or (None, "")
        if not weapon:
            # 变体兜底：棱晶·X / Prime X → 母武器（WM 紫卡表只挂母武器，
            # 变体倾向取母武器值）
            import re as _re2
            base = _re2.sub(r"^(棱晶|Prime|P)", "", weapon_name.strip())
            base = _re2.sub(r"\s*Prime\s*$", "", base, flags=_re2.I)
            if base != weapon_name.strip():
                weapon = await self.client.resolve_riven_weapon(base)
        if not weapon:
            tips = await self.client.suggest_riven_weapons(weapon_name.strip())
            tip = ("，你是不是想找：" + "、".join(tips)) if tips else ""
            return Reply(raw_text=f"未找到紫卡武器「{weapon_name.strip()}」{tip}")
        cls = RA.weapon_class(weapon.get("riven_type", ""),
                              weapon.get("group", ""))
        # 倾向来历：手输覆盖（棱晶等变体 WM 没数据，卡主最准）> WM/母武器值。
        # 游戏内紫卡不显示倾向数值，LLM 从卡面"读倾向"只会把内融值之类的
        # 数字当倾向（教训：49 → 区间爆表），所以永远不采信 LLM。
        wm_disp = float(weapon.get("disposition") or 0)
        # 倾向来历：手输覆盖 > wiki 变体表（棱晶等变体 WM 没数据）> WM 母武器。
        variant_disp, variant_key = None, ""
        if weapon_name.strip() != (weapon.get("zh") or weapon.get("en") or ""):
            variant_disp, variant_key = (variant_disp_pre,
                                         variant_key_pre)
        disp = disp_override or variant_disp or wm_disp
        if disp <= 0:
            return Reply(raw_text=f"WM 未返回「{weapon_name.strip()}」的倾向数值，无法计算区间；"
                                  "变体武器可手输：紫卡分析 武器名 倾向0.95 词条…")
        name = weapon.get("zh") or weapon.get("en") or weapon["url_name"]
        mother_name = name
        # ── 小数点修正（2026-09-24 用户报障：115.7% 读成 1157%）────────────
        # vision 偶发把小数点读丢，整卡数值随之「都不吻合」。用**当前倾向的合法
        # 区间**做判据：原值明显超出区间、除以 10 落回区间内 → 修正并在卡面注明。
        decimal_fix: list[str] = []

        def _fix_decimal(pairs, negative: bool):
            out = []
            for sid, v in pairs:
                lo, hi = RA.stat_range(sid, cls, disp, len(stats_pos),
                                       len(stats_neg), negative=negative)
                if not lo or not hi:
                    out.append((sid, v))
                    continue
                if not (lo * 0.7 <= v <= hi * 1.4):
                    v10 = v / 10.0
                    if lo * 0.85 <= v10 <= hi * 1.15:
                        decimal_fix.append(
                            f"{RA.fmt_value(sid, v)} → {RA.fmt_value(sid, v10)}")
                        v = v10
                out.append((sid, v))
            return out

        stats_pos = _fix_decimal(stats_pos, False)
        stats_neg = _fix_decimal(stats_neg, True)
        # 负词条可能被漏识别：卡面负词条常写成「x0.55 对 Corpus 的伤害」这类
        # 乘数形式，vision 容易整行丢掉 —— 词条数系数会从 (n正,1)=0.9375
        # 错成 (n正,0)=0.75，区间整体偏小 20%，表现为「卡面数值与倾向都不吻合」。
        # 判据：0 负解释不了、1 负能解释 → 按 1 负算（区间只依赖正词条系数）。
        neg_fix_note = ""
        if not stats_neg and stats_pos and len(stats_pos) == 3:
            try:
                if not RA.disp_feasible(stats_pos, [], cls, disp) and \
                        RA.disp_feasible(stats_pos,
                                         [("damage_vs_corpus", 45.0)], cls, disp):
                    stats_neg = [("damage_vs_corpus", 45.0)]
                    neg_fix_note = ("⚠ 卡面疑似有未被识别的负词条（常见写法"
                                    "「x0.55 对 Corpus 的伤害」这类乘数形式），"
                                    "已按 3正1负 的系数计算")
            except Exception:  # noqa: BLE001 —— 纠错失败不影响主流程
                pass
        if variant_disp or disp_override:
            name = weapon_name.strip() or name   # 保留用户输入的变体名
        # ── 数值反推倾向 ────────────────────────────────────────────────
        # 卡面只写母武器名（变体信息根本不在截图里），但数值 =
        # 基值 × 倾向 × 词条系数 × U(0.9~1.1) 可以反着解出倾向；家族内
        # （母武器 + 棱晶/Prime/亡魂…）通常只有一个候选能解释全部词条，
        # 据此自动判定该按谁的倾向算 —— 不用手输、也不用带变体名。
        infer_note = ""
        fam_all = []
        if not disp_override:
            fam_all = [(n, v) for n, v in await self.client.riven_family(weapon)
                       if abs(v - wm_disp) > 1e-9]
            if not variant_disp:
                fits = RA.match_disposition(stats_pos, stats_neg, cls,
                                            [(mother_name, wm_disp)] + fam_all)
                if len(fits) == 1 and abs(fits[0][1] - wm_disp) > 1e-9:
                    name, disp = fits[0]          # 唯一吻合且不是母武器
                    infer_note = (f"数值反推倾向 {disp:g}：唯一吻合 {name}"
                                  f"（母武器 {mother_name} {wm_disp:g} 不吻合）")
                elif len(fits) > 1:
                    infer_note = ("数值与多个倾向都吻合：" +
                                  "、".join(f"{n} {v:g}" for n, v in fits) +
                                  "　请带变体名重发：紫卡分析 棱晶欧玛 [截图]")
                elif not fits:
                    # ★ 2026-09-24 用户报障：老卡（洗出后该武器倾向被上调过，
                    #   游戏不回溯重算旧卡数值）会四条词条整体偏低、全落 0%，
                    #   旧实现只丢一句「武器名可能识别有误」（误导）。数值本身就
                    #   能反推倾向：区间够紧（≤25%）时直接按反推值算区间。
                    iv = RA.disposition_interval(stats_pos, stats_neg, cls)
                    if iv[0] and iv[1] / iv[0] <= 1.25:
                        disp = round((iv[0] + iv[1]) / 2, 2)
                        infer_note = (
                            f"卡面数值反推倾向 ≈{disp:g}（{mother_name} 当前值 "
                            f"{wm_disp:g} 对不上，反推区间 {iv[0]:g}~{iv[1]:g}）"
                            "—— 疑似倾向调整前洗出的老卡，区间已按反推值计算")
                    else:
                        infer_note = (f"⚠️ 卡面数值与「{mother_name}」家族的已知倾向"
                                      "都不吻合，武器名可能识别有误")
            elif not RA.disp_feasible(stats_pos, stats_neg, cls, disp):
                iv = RA.disposition_interval(stats_pos, stats_neg, cls)
                rng = f"（反推应在 {iv[0]:g}~{iv[1]:g}）" if iv[0] else ""
                infer_note = f"⚠️ 卡面数值与倾向 {disp:g} 不吻合{rng}，请核对武器"
        if disp_override:
            disp_note = (f"（已按手输倾向 {disp:g} 计算，母武器 WM 值 {wm_disp:g}）"
                         if wm_disp else f"（已按手输倾向 {disp:g} 计算）")
        elif variant_disp:
            disp_note = f"（倾向取自 wiki 变体表：{variant_key} {variant_disp:g}）"
        else:
            disp_note = ""
        # 家族提示：数值反推没结论时，列出家族变体倾向供对照/手输
        family_note = ""
        if not infer_note and not disp_override and not variant_disp and \
                weapon_name.strip() == mother_name:
            fam = [(n, v) for n, v in fam_all if abs(v - disp) > 1e-9]
            if fam:
                family_note = ("该武器家族有其它倾向：" +
                               "、".join(f"{n} {v:g}" for n, v in fam[:4]) +
                               "　卡面不显示变体，装在棱晶等变体上请发"
                               "「紫卡分析 棱晶欧玛 [截图]」")
        title, lines = fmt.fmt_riven_analysis(name, disp, cls,
                                              stats_pos, stats_neg)
        if family_note:
            lines.insert(1, f"※ {family_note}")
        if disp_note:
            lines.insert(1, f"※ {disp_note}")
        if infer_note:
            lines.insert(1, f"※ {infer_note}")
        if neg_fix_note:
            lines.insert(1, f"※ {neg_fix_note}")
        if decimal_fix:
            lines.insert(1, "※ 已修正小数点（截图未读出点号）：" +
                         "、".join(decimal_fix))
        if source_note:
            lines.insert(1, f"※ 来源：{source_note.strip('（）')}")
        logger.info("[sdjk] 紫卡分析耗时 %.0f ms（含识别/查询/计算）",
                    (_tt.perf_counter() - _t_start) * 1000)
        return Reply(title, lines,
                     footer=fmt.fmt_platform_footer(
                         platform, "DE 属性基值公式 · 倾向可由卡面数值反推"))

    async def _h_trend(self, parsed, event, platform) -> Reply:
        """物品价格趋势（48h 逐小时 + 90d 逐日）。"""
        name = parsed.content_str.strip()
        if not name:
            return Reply(raw_text="用法：趋势 物品名（也支持「wm趋势 物品名」）\n"
                                  "　例：趋势 膛线　｜　趋势 绝路Prime")
        item = await self.client.resolve_wm_item(name)
        if not item:
            return await self._wm_suggest(name)
        stats = await self.client.wm_statistics(item["url_name"], platform)
        summary = self.client.summarize_stats(stats)
        display = item.get("zh") or item.get("en") or item["url_name"]
        title, lines = fmt.fmt_trend(display, stats, summary)
        return Reply(title, lines, footer=fmt.fmt_platform_footer(platform, "warframe.market 统计"))

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
        # —— 那些要站桩守点，用户明确说「移动防御算不上速刷」。
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
        # 不翻页：全部场次放同一张卡（用户要求「那几页内容都放一张截图上」）。
        total = len(rows)
        chunk = rows[:fmt._ALL_ROWS_CAP]
        lines = []
        for r in chunk:
            tag = f"[{r['tier_cn']}]"
            # 行结构：[纪元] 节点 · 任务 · 派系　可掉落 N 种 · 钢铁/九重天 · 速刷 · 剩X
            # 标签顺序按用户要求：**速刷放最后**、钢铁/九重天放它前面（倒数第二）。
            parts = [f"{tag} {r['node']}"]
            if r["type"] and r["type"] != "?":
                parts.append(r["type"])
            if r["faction"]:
                parts.append(r["faction"])
            line = " · ".join(parts) + f"　可掉落 {r['unv']} 种"
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

    async def _h_rm(self, parsed, event, platform) -> Reply:
        reply = await self._h_wr(parsed, event, platform)
        # 2026-09-14 实测：riven.market 后端（Firebase riven-market）已停用
        # （423 "database has been deactivated"，拍卖页 404），站点只剩静态页，
        # 没有可用 API。紫卡数据统一走 warframe.market 的紫卡拍卖接口，
        # 这行提示同步改掉，免得用户以为只是"没接线"。
        if reply.title:
            reply.footer = ("riven.market 已停服（后端数据库停用），"
                            "紫卡数据走 warframe.market 拍卖")
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
    def _auction_match(a: dict, q, neg_set: set, pos_set: Optional[set] = None,
                       *, ignore_status: bool = False) -> bool:
        item = a.get("item", {}) or {}
        attrs = item.get("attributes") or []
        pos = [at for at in attrs if at.get("positive")]
        neg = [at for at in attrs if not at.get("positive")]
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
        rerolls = fmt._riven_rolls(a)   # WM 字段名是 re_rolls，取错会恒为 0
        if q.rerolls_min is not None and rerolls < q.rerolls_min:
            return False
        if q.max_rerolls is not None and rerolls > q.max_rerolls:
            return False
        if ignore_status:
            # 放宽「在线状态」单独一档（词条条件仍然全保留）——完全匹配但卖家
            # 离线，也比「词条不匹配的在线单」值得排在前面（2026-09-24）。
            return True
        status = (a.get("owner", {}) or {}).get("status")
        if q.status == "latest" and status != "ingame":
            return False
        if q.status == "recent" and status not in ("ingame", "online"):
            return False
        return True

    async def _h_ducats(self, parsed, event, platform) -> Reply:
        """杜卡德垃圾榜：按「杜卡德/白金」排序，数据取自 WM 官方计算器。

        以前是逐项查订单自己算，只能抽样十几个，排名和官网对不上；
        现在直接接 WM 的 tools/ducats，与网页版同一份数据。
        """
        pass

        tier = parsed.preset or next((t for t in parsed.content if t in ("金", "银", "铜")), None) \
            or ("金" if parsed.command_raw in ("金垃圾",) else
                "银" if parsed.command_raw in ("银垃圾",) else
                "铜" if parsed.command_raw in ("铜垃圾",) else "金")
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
        chunk = pool[(page - 1) * page_size: page * page_size]
        title, lines = fmt.fmt_ducat_junk(tier, chunk, page=page, pages=pages)
        if pages > 1:
            lines.append(f"※ 第{page}/{pages}页，共{total}件；加 -2 / -3 翻页")
        return Reply(title, lines,
                     footer=fmt.fmt_platform_footer(platform, "杜卡德/白金 越高越值得换"))

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
        step = max(1, len(cands) // 40)          # 兜底才抽样，正路走全量榜单
        pool = cands[::step][:40]
        results = await self._gather([
            self.client.wm_orders(x["url_name"], platform) for x in pool])
        rows = []
        for it, res in zip(pool, results):
            if isinstance(res, Exception) or not isinstance(res, tuple):
                continue
            sells = [o for o in res[0]
                     if o.get("order_type") == "sell"
                     and o.get("platform", platform) == platform
                     and (o.get("platinum") or 0) > 0]
            if not sells:
                continue
            cheapest = min(o["platinum"] for o in sells)
            ducats = it.get("ducats") or 0
            rows.append({"name": it.get("zh") or it.get("en") or it["url_name"],
                         "ducats": ducats, "dpp": ducats / cheapest, "dpp_wa": 0.0,
                         "plat": float(cheapest), "volume": 0})
        rows.sort(key=lambda r: -r["dpp"])
        return rows

    @staticmethod
    def _junk_list(tier: str) -> list:
        try:
            data = json.loads(JUNK_FILE.read_text(encoding="utf-8"))
            return data.get(tier) or []
        except Exception:  # noqa: BLE001
            return []

    # ------------------------------------------------------------------
    # 蹲（后台推送订阅）
    # ------------------------------------------------------------------
    async def _h_status_cmd(self, parsed, event, platform) -> Reply:
        """状态指令（同 .状态）。"""
        return await self._handle_admin(event, ".状态")

    async def _h_dun(self, parsed, event, platform) -> Reply:
        umo = event.unified_msg_origin
        toks = list(parsed.content or [])

        if not toks or toks == ["帮助"]:
            lines = []
            for ev, (desc, wired) in PUSH_EVENTS.items():
                lines.append(f"· {ev}：{desc}" + ("" if wired else "（未接线）"))
            lines += ["", "时长：永久/7天/两周/N小时…（不写=命中一次后取消）",
                      "时间：22到8 / 每天19点 / 周1/3/5 23点",
                      "取消：蹲 取消（全部）/ 蹲 取消 裂隙 捕获（只删匹配项）"]
            # 2026-09-21 修：裸「蹲」应出卡片图（与其它指令一致）。
            # 原 text_only=True 是 v0.5 接手时的祖传写法，全插件唯一一处强制纯文本；
            # 渲染失败时 _build_results 本就会自动降级文字，无需在此抢降级。
            return Reply("可蹲类型", lines)

        # 取消：「取消」位置无关——「蹲 取消」「蹲 裂隙 取消」「蹲 取消 裂隙 捕获」
        # 都合法；其余词构成筛选条件（2026-09-14 修：旧版见「取消」就删全群）。
        if "取消" in toks:
            heads = [t for t in toks if t != "取消"]
            if not heads:
                removed = await self.subs.remove(lambda s: s.umo == umo)
                return Reply(raw_text=f"已取消本群全部 {removed} 条蹲订阅"
                                      if removed else "本群没有蹲订阅")
            _ev, _keys, _exact, _fuzzy, _label = build_cancel_selector(umo, heads)
            removed = await self.subs.remove(_exact)
            if not removed and _keys:
                removed = await self.subs.remove(_fuzzy)
            return Reply(raw_text=f"已取消「{_label}」相关订阅 {removed} 条"
                                  if removed else f"本群没有「{_label}」相关订阅")

        event_type = None
        rest: list[str] = []
        for tok in toks:
            ev = normalize_event(tok)
            if ev and event_type is None:
                event_type = ev
            else:
                rest.append(tok)
        if len(toks) >= 2 and toks[1] == "帮助" and event_type:
            desc, _ = PUSH_EVENTS[event_type]
            return Reply(raw_text=f"【蹲 {event_type}】{desc}" +
                         ("；示例：蹲 " + event_type + " 普通捕获,钢铁虚空生存 永久"
                          if event_type == "裂隙" else ""))
        # 没有识别出事件类型，且第一项不是「帮助/取消」时，**不要**静默降级
        # 为「裂隙 + 筛选=...」——这正是用户反馈「蹲功能不生效」的根因：地点词
        # 被错当成裂隙筛选加入订阅，而该地点根本不刷裂隙，所以永远不会触发。
        if event_type is None:
            # 可蹲清单从 PUSH_EVENTS 现算（只列已接线的），别手抄——
            # 手抄版把不可订阅的「警报」也列了进去，还漏了山谷/魔胎等类型。
            wired = " / ".join(ev for ev, (_, ok) in PUSH_EVENTS.items() if ok)
            return Reply(raw_text=(
                "未识别为可蹲类型「" + (toks[0] if toks else "") + "」。\n"
                f"可蹲类型：{wired}\n"
                "发送「蹲 帮助」查看完整说明"))
        # 「蹲 类型」正常订阅路径：此处 event_type 已确定，必须先取 desc/wired，
        # 否则下面 `if not wired` 会在未赋值分支触发 NameError（「蹲 类型」直接失效的根因）。
        desc, wired = PUSH_EVENTS[event_type]
        if not wired:
            return Reply(raw_text=f"「{event_type}」当前无法订阅。\n"
                                  f"原因：{desc}")
        if not self.groups.get(umo)["push"]:
            return Reply(raw_text="本群推送功能未开启，请管理员发送 .开启 推送 后再蹲")

        duration = None
        window = None
        rule_parts: list[str] = []
        i = 0
        while i < len(rest):
            tok = rest[i]
            if duration is None and parse_duration(tok) is not None:
                duration = parse_duration(tok)
                i += 1
                continue
            if window is None:
                w = parse_time_window(tok)
                if w is None and i + 1 < len(rest) and tok.startswith("周"):
                    w = parse_time_window(tok + " " + rest[i + 1])
                    if w is not None:
                        i += 1
                if w is not None:
                    window = w
                    i += 1
                    continue
            rule_parts.append(tok)
            i += 1

        now = time.time()
        # L-4: 单 umo 订阅上限（防滥用/防误循环订阅撑爆存储）
        MAX_SUBS_PER_UMO = 30
        existing = self.subs.for_umo(umo)
        if len(existing) >= MAX_SUBS_PER_UMO:
            return Reply(raw_text=f"⛔ 本群蹲订阅已达上限（{MAX_SUBS_PER_UMO} 条），"
                                  f"请先「蹲 取消」或「蹲 {event_type} 取消」清理后再试")
        sub = Subscription(
            umo=umo, platform=platform, event=event_type,
            rule=",".join(rule_parts),
            windows={} if window is None or window.is_always else {
                "start": window.start, "end": window.end,
                "days": sorted(window.days) if window.days else None,
                "at_hour": window.at_hour},
            until=-1 if duration == -1 else (now + duration if duration else -1),
            once=duration is None,
            hits_left=None,
            created_by="",  # L-2: 不再落盘 QQ 昵称/可识别字符串
        )
        await self.subs.add(sub)
        dur_text = ("永久" if duration == -1 else
                    (f"{duration // 3600} 小时" if duration else "命中一次即取消"))
        win_text = window.describe() if window else "全天"
        rule_text = sub.rule or "无"
        if event_type == "裂隙" and sub.rule:
            rule_text += f"（{parse_fissure_filter(sub.rule).describe()}）"
        lines = [
            f"类型　{event_type}（{PLATFORM_DISPLAY.get(platform, platform)}）",
            f"筛选　{rule_text}",
            f"时长　{dur_text}",
            f"时间　{win_text}",
            "─" * 16,
            f"订阅 #{sub.sid} 已加入本群推送队列",
            f"取消方式：蹲 取消 · 蹲 {event_type} 取消",
        ]
        # ★ 筛选词「写错/用在不支持的类型上」必须当场说清（铁律 A：不能静默失效）。
        #   2026-09-19 用户反馈「蹲 仲裁 高效 永久」没生效 —— 一半原因就是
        #   仲裁的筛选词当时被整体忽略，用户看不到任何提示。
        if sub.rule and not _arbi.rule_supported(event_type):
            lines.append(f"※ 注意：「{event_type}」不支持筛选，"
                         f"上面的「{sub.rule}」不会生效"
                         f"（目前只有 裂隙 / 仲裁 支持筛选）")
        elif event_type == "仲裁":
            _types, _ratings, _unknown = _arbi.parse_rule(sub.rule)
            if _unknown:
                lines.append(f"※ 未识别的筛选词：{'、'.join(_unknown)}"
                             f"（可用：高效 / 传奇 / {_arbi.TYPES_STR}）")
            if _ratings:
                lines.append(f"※ 只在评级 {'/'.join(_ratings)} 的场次推送"
                             f"（想全都收就别写「高效/传奇」）")
        return Reply(f"◆ 蹲订阅成功", lines)

    # ------------------------------------------------------------------
    # 推送回调
    # ------------------------------------------------------------------
    async def _push_send(self, umo: str, text: str):
        chain = MessageChain().message(text)
        await self.context.send_message(umo, chain)
