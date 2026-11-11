# -*- coding: utf-8 -*-
"""Warframe SDJKBOT —— AstrBot 插件入口。

无缝配合 NapCat (OneBot v11) 上游运行；指令解析为**自由参数**式：
【主指令 内容 附加指令】位置任意、空格分隔，通用修饰符可叠加
（-pc/-ps/-xb/-sw、-w 文字、-t 图片、-N 翻页（含 -1）、-r 密语）。

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
import random
import re
import shutil
import tempfile
import time
import traceback
from dataclasses import replace
from pathlib import Path
from typing import Optional

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Image, Plain
from astrbot.api.star import Context, Star, register

try:  # 允许脱离 AstrBot 直接跑单元测试
    from .core import __brand__ as BRAND
    from .core import api_client, display_time
    from .core.api_client import (
        DEFAULT_DE_WORLDSTATE,
        WarframeAPIError,
        WarframeClient,
        parse_url_list,
    )
    from .core import formatters as fmt
    from .core import paths as core_paths
    from .core import wiki_intro
    from .core import i18n
    from .core.parser import (
        BEIJING_TZ,
        MENU_COMMANDS,
        Parsed,
        normalize_platform,
        parse,
        PLATFORM_DISPLAY,
    )
    from .core.push import PushDaemon, at_targets
    from .core.render import (
        ImageRenderer,
        WATERMARK_VERSION,
        display_footer,
        migrate_legacy_user_fonts,
        text_card,
        card_markdown,
    )
    from .core.commands import DailyCommands, Reply
    from .core.commands import ProgressCommands
    from .core.commands import ArbitrationCommands
    from .core.commands import RotationCommands
    from .core.commands import RelicCommands
    from .core.commands import MarketCommands
    from .core.commands import RivenCommands
    from .core.commands import WikiMiscCommands
    from .core.commands import ScanCommands, VisionCommands
    from .core.commands import DunCommands
    from .core.commands.base import Action, _num
    from .core.messaging import MessagingBridge
    from .core.store import GroupStore, SubscriptionStore
    from .core.vision_prompts import VisionPromptStore
    from .core.local_ocr import LocalRivenOCR
    from .core.local_ocr_install import LocalOCRInstaller
    from .core.webpanel import WebPanel
except ImportError as _imp_err:  # pragma: no cover
    # ★ 2026-09-25 事故修正：**只在确实不是包上下文时**才回退到绝对导入
    #   （脱离 AstrBot 单独跑脚本/测试的情形）。包内导入失败 = 真错误，
    #   必须原样抛出 —— 否则真错误会被换成 "No module named 'core'"：
    #   当天服务器从市场安装 v1.0.7 时，新 main.py 要的 render 函数还没落地，
    #   报错就这样被伪装成"模块结构坏了"，面板与日志第一眼全被误导。
    if __package__:
        raise
    from core import __brand__ as BRAND

    from core import api_client, display_time
    from core.api_client import (
        DEFAULT_DE_WORLDSTATE,
        WarframeAPIError,
        WarframeClient,
        parse_url_list,
    )
    from core import formatters as fmt
    from core import paths as core_paths
    from core import wiki_intro
    from core import i18n
    from core.parser import (
        BEIJING_TZ,
        MENU_COMMANDS,
        Parsed,
        normalize_platform,
        parse,
        PLATFORM_DISPLAY,
    )
    from core.push import PushDaemon, at_targets
    from core.render import (
        ImageRenderer,
        WATERMARK_VERSION,
        display_footer,
        migrate_legacy_user_fonts,
        text_card,
        card_markdown,
    )
    from core.commands import DailyCommands, Reply
    from core.commands import ProgressCommands
    from core.commands import ArbitrationCommands
    from core.commands import RotationCommands
    from core.commands import RelicCommands
    from core.commands import MarketCommands
    from core.commands import RivenCommands
    from core.commands import WikiMiscCommands
    from core.commands import ScanCommands, VisionCommands
    from core.commands import DunCommands
    from core.commands.base import Action, _num
    from core.messaging import MessagingBridge
    from core.store import GroupStore, SubscriptionStore
    from core.vision_prompts import VisionPromptStore
    from core.local_ocr import LocalRivenOCR
    from core.local_ocr_install import LocalOCRInstaller
    from core.webpanel import WebPanel


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
# AstrBot 内置指令（astrbot/builtin_stars/builtin_commands/main.py）
_BUILTIN_COMMANDS = frozenset(
    {
        "help",
        "sid",
        "name",
        "reset",
        "stop",
        "new",
        "stats",
        "provider",
        "dashboard_update",
        "set",
        "unset",
    }
)
_OCCUPIED_FALLBACK = _BUILTIN_COMMANDS | frozenset(
    {
        # 已知的第三方占用（本机实测）。运行期探测失败时靠它兜底 ——
        # 宁可少避让，也不能把内置帮助这种刚需指令再吞一次。
        "新闻",
        "news",
    }
)
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
            if "astrbot_plugin_warframe_sdjkbot" in module_path:
                continue  # 跳过自己，只收集「别人的」
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


def _register_menu_shells() -> list[str]:
    """为平台指令菜单注册英文指令壳（Telegram 菜单 / Discord 斜杠指令由 AstrBot 自动同步）。

    AstrBot 只同步 `filter.command` 注册的指令，而本插件靠 on_message 统一分发。
    这里给 MENU_COMMANDS 每条注册一个**空壳**：不声明参数（多余的词 AstrBot 直接忽略）、
    不产出结果、不 stop_event，AstrBot 会接着执行 on_message，照旧只回复一次。
    本插件的壳不计入 `_occupied_command_words`（按模块路径跳过自己），不会给自己让路。
    取不到 filter.command（旧版或测试桩）时不注册，不影响其它功能。
    """
    command = getattr(filter, "command", None)
    if not callable(command):
        return []
    names = []
    for entry in MENU_COMMANDS:

        async def _shell(self, event: AstrMessageEvent):  # noqa: ARG001 - 只为菜单注册
            return None

        _shell.__name__ = _shell.__qualname__ = f"menu_{entry.en}"
        _shell.__doc__ = f"{entry.desc_en} · {entry.zh}"
        try:
            command(entry.en, desc=_shell.__doc__)(_shell)
        except Exception as exc:  # noqa: BLE001 - 菜单注册失败不能拖垮插件加载
            logger.warning("[sdjk] 平台菜单指令「%s」注册失败：%s", entry.en, exc)
            continue
        names.append(entry.en)
    return names


MENU_SHELLS = _register_menu_shells()


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
        parts = [
            getattr(seg, "text", "")
            for seg in event.get_messages()
            if isinstance(seg, Plain) and getattr(seg, "text", None)
        ]
        if parts:
            out.append("".join(parts))
    except Exception:  # noqa: BLE001
        pass
    text = getattr(event, "message_str", None)
    if not out and isinstance(text, str) and text:
        out.append(text)
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


PLUGIN_DIR = Path(__file__).resolve().parent
PLUGIN_NAME = "astrbot_plugin_warframe_sdjkbot"  # 插件身份（须与 metadata.yaml.name 一致）
# ★ 2026-09-28 裁定：原先这里有个指向 `core/data/rotations.json` 的**包内路径常量**，
#   两个读点（时效汇总 / 轮换卡）都直接读它 ⇒ **运行期自动刷新的回写到不了卡面**。
#   现已统一改走 `core_paths.read_path("rotations.json")`（运行期副本优先、内置包内种子回退），
#   该常量随之**删除**（不留无注释的死引用 —— 同类教训见 `language_text_zh` 的 tail 死代码）。
#   ⚠️ 源码级护栏 `tests/test_de_worldstate.py` 有一条「main.py 里不得再出现该常量名」的断言。


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


# 指令一览：(指令写法, 说明) —— 渲染时按全角空格分成两列对齐。
#
# ★ 两条硬规则（2026-09-17 改版，玩家反馈「主指令乱排 / 缺指令 / 看不懂」）：
#   1. 左列必须是**真实主指令名**（可照发）＋常用别名，不再是「平台切换」这类
#      描述性标签 —— 用户是照着这行去发指令的。
#   2. **全部主指令（当前 57 个）必须出现**，由 tests/test_help_coverage.py 锁住，
#      以后新增主指令而忘了写进帮助会直接测试失败（条数按 COMMAND_ALIASES 现算，
#      不写死在测试里）。
#   分组依据是「玩家在什么场景下会想用它」，同类指令聚在一组。


HELP_TOPIC: dict[str, list[tuple[str, str]]] = {
    "最常用": [
        ("时效 / 周报", "各周期剩余时间总览"),
        ("裂隙 / 开核桃", "虚空裂隙（可筛 钢铁/类型/纪元）｜刷遗物建议"),
        ("赏金 / 声望", "各地区赏金与奖励（地球 = 地球赏金）"),
        ("wm 物品名", "市场在售/收购（加 蓝图/机体 查部件）"),
        ("伤害 武器 对 敌人 100级", "面板 / 单发 / DPS / 分段"),
        (
            "紫卡分析 武器名 词条数值",
            "词条区间与距中、融合建议、同词条挂单价（负词条加「负」；也可直接发紫卡截图）",
        ),
        ("词条范围 武器名 [3+1-]", "该武器能刷出的词条区间（2+1 = 2+1-；可点名词条 / 切组合）"),
        ("紫卡融合 [武器/词条]", "融合词条由哪两条原词条拼成（也认 紫卡拼接 / 融合词条）"),
    ],
    "周期与日常": [
        ("夜灵 / 平原时间", "各开放世界昼夜 / 派系周期"),
        ("突击 / 执刑官", "每日突击｜本周执刑官猎杀"),
        ("仲裁 / 仲裁表", "当前场次｜整周排期（可筛类型）"),
        ("警报 / 入侵 / 活动 / 武形秘仪", "进行中的限时内容｜PVP 挑战"),
        ("电波 / 日历", "午夜电波挑战｜1999 日历"),
    ],
    "周常轮换": [
        ("深层科研 / 时光科研", "本周 3 任务 × 普通 / 硬化"),
        ("沉沦之地 / 炼狱塔", "本周 21 层与复杂化"),
        ("侵袭 / 九重天", "钢路侵袭节点｜虚空风暴"),
        ("灵化轮换 / 信条 / 终幕", "每周轮换武器与元素加成（加 刷新 立即重抓）"),
        ("舰队进度 / 氏族奖励", "舰队建造｜氏族研究"),
    ],
    "商店": [
        ("奸商 / 虚空商人", "Baro 库存与抵达时间"),
        ("每日特惠 / 商城折扣", "每日折扣｜限时礼包"),
        ("言录使 / 碎银 / 钢铁之路", "苦栓周常（加 刷新 重抓）｜碎银｜Teshin"),
        ("出库 / 阿耶", "Prime 出库清单｜阿耶兑换"),
    ],
    "市场与紫卡": [
        ("趋势 物品名", "48h / 90d 价格走势"),
        ("wr / 紫卡 / rm 武器名", "紫卡拍卖｜源紫卡报价"),
        ("wr 未开 [类别]", "未开紫卡挂单，如「wr 手枪未开」；不加类别看 8 类行情"),
        (
            "倾向 / 紫卡排行",
            "倾向已统一 1.2（看原倾向与缩放）｜DE 周报热度榜（管理员加 刷新 拉 WM 现价）",
        ),
    ],
    "遗物与资源": [
        ("遗物 / 核桃 名称", "奖励与出处；出库 / 入库 / 列表"),
        ("部件 物品名", "Prime 部件与蓝图出处"),
        ("金垃圾 / 银垃圾 / 铜垃圾", "按杜卡德分档"),
    ],
    "伤害与配卡": [
        ("识卡 + 截图", "读配卡界面并校验面板"),
        ("识卡伤害 对 敌人 等级", "复用识卡结果复算"),
        ("武器融合 / 玄骸 / xh", "效价融合｜玄骸拍卖"),
    ],
    "资料与推送": [
        ("新闻 / 结合目标 / 赤毒", "官方公告｜Simaris 目标｜赤毒历史"),
        ("wiki 词 / 翻译 词", "维基直达｜中英对照"),
        ("蹲 类型 筛选 时长 时间", "订阅变化推送（需先 .开启 推送）"),
        ("蹲 取消 / 蹲 帮助", "取消订阅｜可蹲类型与写法"),
    ],
}

# 帮助卡的注脚（挂在某个分组之后；"" = 卡首）。2026-10-11 卡片改版：伤害修饰词 / 群管理 /
# 平台说明从指令行挪到注脚 —— 指令行只留「可照发」的主指令；注脚里的写法仍由
# tests/test_help_coverage.py 校验（主指令覆盖、修饰符可解析、管理员标注）。
HELP_NOTES: dict[str, list[str]] = {
    "": [
        "指令直接发即可，无需 / 前缀（/help 属系统内置指令）；列表类加 -2 / -3 翻页；"
        "-w 纯文字 · -t 强制图片 · -1 第一页 · -r 生成密语",
    ],
    "伤害与配卡": [
        "伤害可加：爆头 病毒10 剥甲 满镀层 钢路 开火N秒｜灵化 / 基础形态 / 副开火 / 蓄力 / "
        "未蓄力 / 半自动 / 空战 / 地面 / 进化 X / 赤毒辐射60（玄骸回响 25–60%）",
    ],
    "资料与推送": [
        "帮助 / help = 本页；状态 = 运行状态、版本与订阅数（需群管理员）",
        "群管理：.默认平台 / .开启 / .关闭 推送 / .状态（点号开头，需群管理员）",
        "平台后缀 -pc / -ps / -xb / -sw：四平台数据已互通，统一显示「国际服」",
        "「赤毒」虹吸数据源已停摆，发它会给出替代查询；「蹲 取消」不带词 = 自己发起的全部（群管 = 全群）",
    ],
}


# 英文版帮助（英文模式用；结构与 HELP_TOPIC / HELP_NOTES 一致，左列是可照发的英文写法）。
# 中文模式只用上面两张表，帮助卡逐字节不变。
HELP_TOPIC_EN: dict[str, list[tuple[str, str]]] = {
    "Most Used": [
        ("timers", "Time left on every cycle, at a glance"),
        (
            "fissures / openrelic",
            "Void Fissures (filter by Steel Path / mission type / era) | relic-cracking tips",
        ),
        ("bounty", "Bounties and rewards by region"),
        ("wm <item>", "Market sell/buy orders (add blueprint / part words to look up a part)"),
        ("damage <weapon> vs <enemy> 100", "Panel / single hit / DPS / breakdown"),
        (
            "rivenanalysis <weapon> <stats>",
            "Stat ranges, roll quality, splice tips and similar listings (or just send a riven screenshot)",
        ),
        (
            "rivenranges <weapon> [3+1-]",
            "Stat ranges a weapon can roll (pick stats or switch combos)",
        ),
        ("splice [weapon/stat]", "Which two stats a spliced stat comes from"),
    ],
    "Cycles and Daily": [
        ("cetus / cycles", "Day/night and faction cycles of the open worlds"),
        ("sortie / archon", "Daily Sortie | this week's Archon Hunt"),
        ("arbitration / arbtable", "Current mission | the weekly schedule (filter by type)"),
        ("alerts / invasions / events / conclave", "Limited-time content | PvP challenges"),
        ("nightwave / calendar", "Nightwave challenges | 1999 Calendar"),
    ],
    "Weekly Rotations": [
        ("deeparchimedea / temporalarchimedea", "This week's 3 missions x Normal / Hard"),
        ("descendia", "This week's 21 floors and complications"),
        ("incursions / voidstorms", "Steel Path Incursions | Void Storms"),
        (
            "incarnon / tenet / coda",
            "Weekly rotation weapons and element bonuses (add refresh to re-fetch)",
        ),
        ("construction / clanrewards", "Fleet Construction | Clan research"),
    ],
    "Shops": [
        ("baro / voidtrader", "Baro Ki'Teer's stock and arrival time"),
        ("darvo / flashsales", "Daily deal | limited-time bundles"),
        (
            "acrithis / slivers / steelpath",
            "Acrithis weekly offers (add refresh to re-fetch) | Riven Sliver shop | Teshin",
        ),
        ("primevault / varzia", "Prime Vault list | Varzia exchange"),
    ],
    "Market and Rivens": [
        ("trend <item>", "48h / 90d price trend"),
        ("wr / rm <weapon>", "Riven auctions | source riven prices"),
        (
            "wr <category> unrolled",
            'Unrolled riven listings, e.g. "wr pistol unrolled"; without a category, shows 8 categories',
        ),
        (
            "dispo / rank",
            "Dispositions (all unified to 1.2: original and scaled) | weekly DE trend ranking (admins: add refresh to pull live WM prices)",
        ),
    ],
    "Relics and Resources": [
        ("relic <name>", "Rewards and sources; also vaulted / unvaulted / list"),
        ("parts <item>", "Prime part and blueprint sources"),
        ("ducats", "Gold / Silver / Bronze parts by ducat value"),
    ],
    "Damage and Builds": [
        ("scan + screenshot", "Read a build screen and verify the panel"),
        ("scandamage vs <enemy> <level>", "Recalculate from the last scan"),
        ("valence / xh", "Valence Fusion | Kuva / Tenet weapon auctions"),
    ],
    "Info and Push": [
        ("news / synthesis / kuva", "Official news | Synthesis Targets | Kuva history"),
        ("wiki <term> / translate <term>", "Wiki link | Chinese-English lookup"),
        (
            "subscribe <type> <filter> <duration>",
            "Subscribe to change alerts (admins must run .enable push first)",
        ),
        ("subscribe cancel / subscribe help", "Cancel subscriptions | watchable types and syntax"),
    ],
}

HELP_NOTES_EN: dict[str, list[str]] = {
    "": [
        "Send commands as plain words, no / prefix (/help is a built-in system command). "
        "Add -2 / -3 for more pages | -w plain text | -t force image | -1 first page | -r whisper text",
        "Language: add -en or -cn to any command for a one-off switch; admins can set the chat default with .language en / .language cn / .language default",
    ],
    "Damage and Builds": [
        "Damage modifiers: headshot, viral10, strip, full-armor, steelpath, fire Ns | incarnon / base form / alt fire / charged / uncharged / semi-auto / aerial / ground / evolve X / kuva-radiation60",
    ],
    "Info and Push": [
        "help = this page; status = runtime status, version and subscription count (group admins only)",
        "Group admin: .platform / .enable / .disable push / .status / .language (start with a dot, group admins only)",
        "Platform suffixes -pc / -ps / -xb / -sw: all four platforms share data and show as International",
        'Kuva siphon data is no longer available, so asking for it points you to alternatives; "subscribe cancel" with no word = all of your own (group admins = whole group)',
    ],
}


# 仲裁的任务类型 / 派系 / 节点渲染：**唯一实现**在 core/arbi.py
# （「仲裁」查询指令与「蹲 仲裁」推送共用；2026-09-19 收敛，避免两套口径漂移）。
# 2026-09-28 D3：别名块随仲裁域迁 core/commands/arbitration.py（逐字）。


# ---------------------------------------------------------------------------
# 信条 / 终幕的「元素 + 加成%」显示
#
# 加成值是**玩家上报数据**（DE 无官方 API），只有 wiki「Reset」页的
# Current Valence Bonuses 两张表；而插件侧访问 wiki.warframe.com 会被
# Cloudflare 的 JS 挑战拦（index.php / api.php / rest.php 全 403），
# 所以只能把抓到的值**存快照**进 rotations.json，换轮后人工刷新。
# ---------------------------------------------------------------------------


@register(
    "astrbot_plugin_warframe_sdjkbot", "SDJK", f"{BRAND}：世界状态 / 市场查价 / 蹲点推送", "1.3.0"
)
class WarframeSDJK(
    DailyCommands,
    ProgressCommands,
    ArbitrationCommands,
    RotationCommands,
    RelicCommands,
    MarketCommands,
    RivenCommands,
    WikiMiscCommands,
    VisionCommands,
    ScanCommands,
    DunCommands,
    Star,
):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.cfg: dict = dict(config) if config else {}
        self.cfg["text_chunk_limit"] = _num(self.cfg, "text_chunk_limit", 0, cast=int)
        self._config = config  # 运维面板「设置」页写回用（save_config）；cfg 是快照
        # 运行期数据目录：data/plugin_data/<插件名>（规范要求），不再写插件包目录。
        data_dir = _resolve_data_dir()
        self.data_dir = data_dir
        self.vision_prompts = VisionPromptStore(data_dir / "vision_prompts.json")
        # 构造仅建立管理器；默认关闭，不装包、不下载、不加载权重。
        self.local_ocr = LocalRivenOCR(data_dir, Path(__file__).resolve().parent)
        self.local_ocr_installer = LocalOCRInstaller(data_dir, Path(__file__).resolve().parent)
        self._messaging = MessagingBridge(
            context, media_cache_dir=data_dir / "qq_media", settings=lambda: self.cfg
        )
        _migrate_legacy_user_data(data_dir)  # 旧版写在插件目录的数据一次性搬家
        core_paths.set_run_dir(data_dir)  # core 层的快照/缓存同源
        # FlareSolverr / 知识库：**开源版要能自己填**（自用版默认值照旧）。
        #   地址按逗号或换行分隔；填了非法值就回退内置默认，不静默猜。
        _flare_urls = parse_url_list(str(self.cfg.get("flaresolverr_urls") or ""))
        self.client = WarframeClient(
            kuvalog_url=(self.cfg.get("kuvalog_url") or ""),
            worldsource=self.cfg.get("worldsource", "de"),
            de_worldstate_url=(self.cfg.get("de_worldstate_url") or DEFAULT_DE_WORLDSTATE),
            worldstate_base=self.cfg.get("worldstate_base", "https://api.warframestat.us"),
            timeout=_num(self.cfg, "http_timeout", 15.0),
            proxy=(self.cfg.get("proxy") or None),
            flare_enabled=bool(self.cfg.get("flaresolverr_enabled", True)),
            flare_urls=_flare_urls or None,
            community_base=str(self.cfg.get("community_data_url") or ""),
            allow_private_urls=bool(self.cfg.get("allow_private_urls", False)),
            wfhelper_valence=bool(self.cfg.get("wfhelper_valence", False)),
        )
        self.groups = GroupStore(
            data_dir / "groups.json", default_platform=self.cfg.get("default_platform", "pc")
        )
        self.subs = SubscriptionStore(data_dir / "subscriptions.json")
        # 用户自带字体迁移（AstrBot 开发原则：持久化数据进 data 目录）：
        # 插件目录里的手放字体搬到 data 目录；随包子集 otf 留在原地。
        # 必须在构造 renderer 之前跑，新位置才能在同一轮启动就被采纳。
        try:
            _moved_fonts = migrate_legacy_user_fonts(data_dir / "fonts")
            if _moved_fonts:
                logger.info(
                    "[sdjk] 已把插件目录里的 %d 个用户字体搬到 %s（插件更新/重装不再丢）",
                    len(_moved_fonts),
                    data_dir / "fonts",
                )
        except Exception:  # noqa: BLE001 - 迁移失败不阻断启动
            logger.warning("[sdjk] 用户字体迁移失败（不影响启动）", exc_info=True)
        self.renderer = ImageRenderer(data_dir / "cards")
        self._last_scan: dict[tuple[str, str], tuple[float, dict, dict]] = {}  # (会话, 发送者)
        self._scan_records: dict[str, tuple] = {}  # 短标识→有界、按会话/发送者隔离的配卡快照
        # 识卡限流状态：**实例级**（类级默认值是兜底，实例级才隔离）。
        # 类属性是可变对象时，写入会命中类本身 —— 多实例/多测试之间会串。
        self._ocr_busy: set = set()
        self._ocr_last: dict[str, float] = {}
        self.render_mode = self.cfg.get("render_mode", "image")
        self.page_size = int(_num(self.cfg, "page_size", 12))
        self.push = PushDaemon(
            self.client,
            self.subs,
            self._push_send,
            logger,
            interval=int(_num(self.cfg, "push_interval", 45)),
            groups=self.groups,  # 派发前查群推送开关（.关闭 推送 = 订阅暂停）
            state_path=data_dir / "push_baseline.json",  # 基线落盘：30 分钟内重启补推
            display_timezone=self.cfg.get("display_timezone", display_time.DEFAULT),
        )
        self._routes = self._build_routes()
        # 插件页面「运维面板」（pages/sdjk/）的接口；AstrBot 不支持时只记日志
        self._panel = WebPanel(self, PLUGIN_NAME)
        self._panel.register(context)

    def _build_routes(self) -> dict:
        """指令名 -> handler 映射（独立成方法便于离线自检）。"""
        return {
            "help": self._h_help,
            "cetus": self._h_cetus,
            "timers": self._h_timers,
            "bounty": self._h_bounty,
            "fissures": self._h_fissures,
            "sortie": self._h_sortie,
            "archon": self._h_archon,
            "voidtrader": self._h_voidtrader,
            "dailydeals": self._h_dailydeals,
            "calendar": self._h_calendar,
            "deeparchimedea": self._h_deep,
            "temporalarchimedea": self._h_temporal,
            "steelpath": self._h_steelpath,
            "slivershop": self._h_slivershop,
            "translate": self._h_translate,
            "arbitration": self._h_arbitration,
            "arbtable": self._h_arbtable,
            "alerts": self._h_alerts,
            "invasions": self._h_invasions,
            "nightwave": self._h_nightwave,
            "news": self._h_news,
            "kuva": self._h_kuva,
            "synthtargets": self._h_synth,
            "construction": self._h_construction,
            "voidstorms": self._h_voidstorms,
            "events": self._h_events,
            "conclave": self._h_conclave,
            "primevault": self._h_primevault,
            "clanrewards": self._h_clanrewards,
            "flashsales": self._h_flashsales,
            "incarnon": self._h_rotation_incarnon,
            "tenet": self._h_rotation_tenet,
            "coda": self._h_rotation_coda,
            "acrichis": self._h_acrichis,
            "descendia": self._h_descendia,
            "incursions": self._h_incursions,
            "wiki": self._h_wiki,
            "valence": self._h_valence,
            "damage": self._h_damage,
            "scan": self._h_scan,
            "scandamage": self._h_scan_damage,
            "wm": self._h_wm,
            "wr": self._h_wr,
            "rm": self._h_rm,
            "rank": self._h_rank,
            "trend": self._h_trend,
            "openrelic": self._h_openrelic,
            "xh": self._h_xh,
            "disposition": self._h_disposition,
            "analysis": self._h_riven_analysis,
            "ranges": self._h_riven_ranges,
            "splice": self._h_riven_splice,
            "relic": self._h_relic,
            "parts": self._h_parts,
            "ducats": self._h_ducats,
            "dun": self._h_dun,
            "status": self._h_status_cmd,
        }

    # 默认出文字卡的平台：AstrBot 的 Slack 适配器上传本地图片失败时整条消息丢失、
    # 上传的图片也不分享到会话（4.28.1～4.29.4），先按文字输出；「-t」仍可强制出图。
    TEXT_DEFAULT_PLATFORMS = frozenset({"slack"})

    def _render_mode_for(self, event) -> str:
        try:
            if event.get_platform_name() in self.TEXT_DEFAULT_PLATFORMS:
                return "text"
        except (AttributeError, TypeError):
            pass
        return self.render_mode

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    @staticmethod
    def _log_task_death(name: str):
        """后台常驻任务的 done-callback：死亡必须留痕。

        ★ 2026-10-03（价格排行静默失效事故）：协程在 try 之外抛异常 ⇒ 无日志、
        无人 await（"Task exception was never retrieved" 也被上层吞掉）⇒ 坏了
        8 天没人知道。凡 fire-and-forget 的常驻任务一律挂本回调：只要**不是
        主动取消**（CancelledError）就以 WARNING 记录，便于 grep 发现。
        """

        def _cb(task: "asyncio.Task") -> None:
            if task.cancelled():
                return
            exc = task.exception()
            if exc is not None:
                logger.warning("[sdjk] 后台任务 %s 异常退出：%r", name, exc)

        return _cb

    def _note_background(self, name: str, state: str, detail: str = "", delay=None):
        """本进程任务状态；正常检查不等同于采用新数据，时间只在实际执行时推进。"""
        if not hasattr(self, "_background_status"):
            self._background_status = {}
        now = time.time()
        row = dict(self._background_status.get(name) or {})
        row["state"] = state
        if state == "running":
            row.update(last_attempt=now, next_run=None)
        elif state in ("ok", "degraded", "error", "skipped"):
            row.update(result=state, last_finished=now, detail=detail[:180], next_run=None)
            row["state"] = "completed"
            if state == "ok":
                row["last_success"] = now
            elif state in ("degraded", "error"):
                row.update(last_error=detail[:180], last_error_at=now)
        if delay is not None:
            row["next_run"] = now + max(0, delay)
        self._background_status[name] = row

    async def _background_sleep(self, name: str, seconds: float):
        self._note_background(name, "waiting", delay=seconds)
        await asyncio.sleep(seconds)

    @staticmethod
    def _refresh_outcome(result) -> tuple[str, str]:
        """汇总实际刷新返回值，不把吞掉错误后正常返回当作数据更新成功。"""
        if not isinstance(result, dict):
            return "ok", "检查流程结束"
        if not result:
            return "skipped", "本次没有刷新结果"

        def good(v):
            return str(v) == "fresh" or str(v).startswith("refreshed")

        failed = [str(k) for k, v in result.items() if not good(v)]
        if failed:
            return "degraded", "未更新或不可用：" + "、".join(failed)
        return "ok", "已采用新数据" if any(
            str(v).startswith("refreshed") for v in result.values()
        ) else "本地数据仍有效"

    async def initialize(self):
        self._background_status = {}
        self.push.start()
        # 守护协程意外退出必须留痕（stop/重载的主动取消不报）
        if self.push.task is not None:
            self.push.task.add_done_callback(self._log_task_death("push_daemon"))
        self._auto_task = asyncio.create_task(self._rank_autoloop())
        self._auto_task.add_done_callback(self._log_task_death("_rank_autoloop"))
        # 紫卡 WM 现价快照不再自动抓（2026-10-25 维护者定：只由管理员「紫卡排行 刷新」/ 面板按钮手动拉）
        self._valence_task = asyncio.create_task(self._valence_autoloop())
        self._valence_task.add_done_callback(self._log_task_death("_valence_autoloop"))
        self._revision_task = asyncio.create_task(self._revision_autoloop())
        self._revision_task.add_done_callback(self._log_task_death("_revision_autoloop"))
        # 社区快照独立轻轮询，业务入口按数据新鲜度短路（见 _community_autoloop）
        self._community_task = asyncio.create_task(self._community_autoloop())
        self._community_task.add_done_callback(self._log_task_death("_community_autoloop"))
        # 首启后台预热（2026-09-29 追加批，2026-10-01 扩展为 _boot_warm）：
        # ①社区快照 ②WM 物品/紫卡武器表（见 `_boot_warm`）。此前社区快照只有
        # 轻轮询的 30 分钟 tick 才拉，没装 FS 的用户首启最坏要等半小时；
        # 而 WM 表是 reload 后首条 wr/wm 指令要现下的，挪到启动期后台预拉。
        # fire-and-forget：异步不阻塞加载、超时收尾、失败记 WARNING（可见）。
        # ★ 2026-10-17：记到 self 上，terminate 才取消得到（v1.2.2 部署实测：reload 后旧实例的预热还在跑，
        #   在已关闭的 httpx client 上报「Cannot send a request, as the client has been closed」、打出「2/4 项」）
        self._boot_task = asyncio.create_task(self._boot_warm())
        self._boot_task.add_done_callback(self._log_task_death("_boot_warm"))
        # 后台预热伤害计算的全部重 JSON + 武器名索引（不阻塞启动）：
        # 不预热时第一条指令要现读武器库/进化/灵化形态/多段/部署表，叠加后
        # 会让首条指令明显变慢（2026-09-17 玩家反馈「半天才出来」）。
        # ★ 2026-10-21（GPT 评估 P08）：记到 self 上并挂死亡日志 —— 原先不落引用，异常只剩一条
        #   「Task exception was never retrieved」。线程本身取消不了（只读本地 JSON、不联网，旧实例的
        #   预热跑完自行结束）；terminate 里 cancel 只是不再等它，避免卸载时悬着一个无主任务。
        self._warm_task = asyncio.create_task(asyncio.to_thread(self._warmup))
        self._warm_task.add_done_callback(self._log_task_death("_warmup"))
        logger.info(
            "[sdjk] 插件已加载，共注册 %d 个主指令（输出模式 %s，渲染器%s）",
            len(self._routes),
            self.render_mode,
            "可用" if self.renderer.available else "不可用·降级文字",
        )
        if not self.renderer.available:
            # ★ 别只说「降级文字」（2026-09-25 立）：市场/开源包不带字体，
            #   给用户能直接照做的排查指引。
            logger.warning(
                "[sdjk] 未找到可用中文字体，图片卡片已降级为纯文本。排查："
                "① Linux/Docker 装系统字体 `apt-get install -y fonts-noto-cjk`"
                "（或 fonts-wqy-microhei）；"
                "② 或下载字体 `python scripts/fetch_font.py`；"
                "③ 或把任意中文字体（ttc/ttf/otf）放到插件数据目录的 fonts/ 下"
                "（随插件更新保留）；改完重载插件，本行应变为「渲染器可用」"
            )
        if self._wiki_intro_on():
            try:
                # 用模块级 `wiki_intro`（顶部 try 双分支已导入）—— 原先这里函数内裸
                # `from core import wiki_intro`，同样会在包成员加载下失败（2026-09-26 修）
                _wi = wiki_intro
                if not _wi.available():
                    # 开关开着但数据缺失（市场/开源包按设计不带 wiki_intro.json）
                    # ——明确说清现象与预期，别让用户以为是故障（2026-09-25 立）
                    logger.warning(
                        "[sdjk] wiki 简介卡数据缺失（core/data/wiki_intro.json 被删？"
                        "v1.0.7 起随包分发）：该类条目将退「最小卡」+ 可点链接；"
                        "MOD 效果行回落英文。重装插件或从发行包补齐该文件即可恢复"
                    )
            except Exception:  # noqa: BLE001 - 探测失败不影响启动
                pass

    def _warmup(self) -> None:
        """逐段记录本地预热的墙钟/线程CPU耗时，区分计算与等待。"""
        self._note_background("warmup", "running")
        try:
            from .core import damage_calc as dc, de_worldstate as ws, search, wiki_intro as wi
        except ImportError:
            from core import damage_calc as dc, de_worldstate as ws, search, wiki_intro as wi
        segments, bad = [], []
        for name, fn in (
            ("伤害数据", dc.warmup),
            ("wiki简介", wi._table),
            ("检索索引", search._index),
            ("简中词表", lambda: ws._load("languages_zh.json")),
        ):
            wall, cpu = time.perf_counter(), time.thread_time()
            try:
                loaded = fn()
                if (name == "wiki简介" and not loaded[0]) or (
                    name in ("检索索引", "简中词表") and not loaded
                ):
                    raise ValueError("没有可用数据")
                outcome = "ok"
            except Exception as exc:  # noqa: BLE001 - 一项失败不影响其它预热
                outcome = type(exc).__name__
                bad.append(name)
                logger.warning("[sdjk] 预热 %s 失败（不影响功能）：%r", name, exc)
            record = {
                "name": name,
                "wall_ms": round((time.perf_counter() - wall) * 1000, 1),
                "cpu_ms": round((time.thread_time() - cpu) * 1000, 1),
                "result": outcome,
            }
            segments.append(record)
            logger.info(
                "[sdjk] 预热分段 %s：墙钟 %.1f ms / 线程CPU %.1f ms / %s",
                name,
                record["wall_ms"],
                record["cpu_ms"],
                outcome,
            )
        self._note_background(
            "warmup",
            "degraded" if bad else "ok",
            "部分加载失败：" + "、".join(bad) if bad else "本地数据加载完成",
        )
        self._background_status["warmup"] = {
            **self._background_status["warmup"],
            "segments": segments,
        }

    # 价格榜单全量抓取只在**闲时**跑。2026-09-14 玩家反馈「bot 回话间隔很久」：
    # 全量抓取实测远超 18 分钟（约 3200 项、实测 1 项/4 秒，跑好几个小时），
    # 期间持续占用 WM 全局限速器，用户的 wm/wr 查询得排在爬虫后面等。
    RANK_CRAWL_IDLE_HOURS = (2, 3, 4, 5, 6)
    # 每晚最多抓多少项（约 4 秒/项 → 900 项 ≈ 1 小时，凌晨窗口内可完成）
    RANK_CRAWL_MAX_PER_RUN = 900
    RANK_TTL_HOURS = 44  # 名义「约两天一刷」，见 _rank_autoloop 里的到期点说明

    async def _rank_autoloop(self):
        """插件内置定时：价格榜单每 48 小时自动重建（且只在凌晨闲时）。

        每 30 分钟检查一次落盘时间戳；超 48h **且**当前处于闲时（02:00-06:59）
        才开爬，否则继续等下一个检查点。抓取本身每 50 项落一次盘，中断可续。
        失败退避 10 分钟后重试，不阻断插件其他功能。
        """
        # ★ 2026-10-03 修复（价格排行自动刷新失效事故）：原写
        #   `api_client.RANKS_FILE` —— 该常量不存在（api_client 只有 RANKS_NAME），
        #   且本行在 try 之外 ⇒ AttributeError 冒泡出协程、零日志静默死亡
        #   （榜单陈旧 8 天，48h 内「价格榜单」日志 0 条）。
        #   两处一并修：① 经 core_paths.read_path() 解析为**绝对路径**（裸字符串
        #   "wm_ranks.json" 会被当相对路径、随 CWD 读不到 ⇒ ts="" ⇒ 永远判过期）；
        #   ② 挪进 try —— 后续任何异常都能落到下面的 warning，不再静默。
        while True:
            try:
                self._note_background("rank", "running")
                RANKS_FILE = core_paths.read_path(api_client.RANKS_NAME)
                data = self.client._load_json_file(RANKS_FILE) or {}
                ts = data.get("ts") or ""
                import time as _t
                from datetime import datetime

                try:
                    age = _t.time() - datetime.fromisoformat(ts).timestamp()
                except ValueError:
                    age = float("inf")
                # ★ 2026-10-25（ZCode 实测）：48h 的到期点总落在白天 ⇒ 要等到再下一个凌晨，
                #   实际约 64h 才刷一次；44h ⇒ 到期点落在前一晚，下个凌晨窗口即抓（实际约 47h）
                if age > self.RANK_TTL_HOURS * 3600:
                    # 闲时闸门：白天不爬（会拖慢用户的 wm/wr 查询）
                    # ★ 2026-10-25（Astra 时区专项）：闲时按**北京时间**判断 —— 原用服务器本地
                    #   `datetime.now().hour`，UTC 宿主上「02–06 点」实为北京时间 10–14 点。
                    bj_hour = datetime.now(BEIJING_TZ).hour
                    if bj_hour not in self.RANK_CRAWL_IDLE_HOURS:
                        logger.info(
                            "[sdjk] 价格榜单已过期，但非闲时（北京时间 %02d 点），凌晨 %d-%d 点再爬",
                            bj_hour,
                            self.RANK_CRAWL_IDLE_HOURS[0],
                            self.RANK_CRAWL_IDLE_HOURS[-1],
                        )
                        self._note_background("rank", "skipped", "等待北京时间凌晨闲时")
                        await self._background_sleep("rank", 1800)
                        continue
                    logger.info(
                        "[sdjk] 价格榜单缺失或超 %d 小时，开始自动重建", self.RANK_TTL_HOURS
                    )
                    n = await self.client.crawl_wm_ranks(limit=self.RANK_CRAWL_MAX_PER_RUN)
                    logger.info("[sdjk] 价格榜单本轮抓取结束（累计 %d 项）", n)
                    snap = (
                        self.client._load_json_file(core_paths.read_path(api_client.RANKS_NAME))
                        or {}
                    )
                    write_failed = getattr(self.client, "_rank_save_failed", False)
                    source_empty = getattr(self.client, "_rank_source_empty", False)
                    pending = bool(
                        snap.get("retry")
                        or snap.get("round_failed")
                        or write_failed
                        or source_empty
                    )
                    self._note_background(
                        "rank",
                        "degraded" if pending else "ok",
                        "OSError（排行落盘失败）"
                        if write_failed
                        else "WM 物品表为空，本轮未抓取"
                        if source_empty
                        else ("仍有待补抓条目" if pending else "本轮抓取结束，整榜时效见数据页"),
                    )
                else:
                    self._note_background("rank", "ok", "本地榜单仍有效")
                await self._background_sleep("rank", 1800)
            except asyncio.CancelledError:
                self._note_background("rank", "cancelled")
                raise
            except Exception as e:  # noqa: BLE001
                logger.warning("[sdjk] 价格榜单自动刷新失败，10 分钟后重试：%s", e)
                self._note_background("rank", "error", type(e).__name__)
                await self._background_sleep("rank", 600)

    async def _flare_phase(self) -> str:
        """wiki 类刷新的前置判定：``off`` / ``absent`` / ``ready``。

        ★ 这是「告警分级」的唯一入口（v1.0.8，issue #1 实测）：三态分别对应
        「不尝试也不告警 / 降频提示一次 / 正常刷新（失败即真故障，照旧 WARN）」。
        探测本身异常按 ``absent`` 处理（保守：宁可少打扰，也不误报故障）。
        """
        if not self.client.flare_enabled:
            return "off"
        try:
            if await self.client.flare_reachable():
                return "ready"
        except Exception:  # noqa: BLE001 - 探测异常按不可达处理
            return "absent"
        return "absent"

    def _warn_flare_absent_once(self) -> None:
        """「没部署 FlareSolverr」的**降频**提示（24h 一次；v1.0.8，issue #1 实测）。

        与真故障分开：FS **可达但求解失败**仍每轮 WARN（不把真问题静默）；
        FS **压根不可达**（没部署/地址不对）只提示一次并给出两条处置路径，
        不再每小时打扰不用这个可选组件的用户。
        """
        now = time.time()
        if now - getattr(self, "_flare_absent_warned_at", 0.0) < 24 * 3600:
            return
        self._flare_absent_warned_at = now
        logger.warning(
            "[sdjk] 未检测到可用的 FlareSolverr（可选组件）：wiki 类快照"
            "（信条/终幕元素加成、言录使货单）将使用**社区快照或已有运行缓存**，缺值时等待后台获取"
            "（社区快照来自公开仓 bot-data 分支，免盾，有就自动用）；仍会尝试普通 HTTP 获取 wiki。"
            "· 不需要该功能：在配置面板关闭「启用 CF 绕过代理（FlareSolverr）」，本提示随之消失；"
            "· 需要：部署 FlareSolverr 并把可达地址填进「FlareSolverr 地址」"
            "（容器内 127.0.0.1 到不了宿主机，常用 http://172.17.0.1:8191）。"
        )

    async def _boot_warm(self) -> None:
        """首启后台预热（fire-and-forget，2026-09-29 追加批 + 2026-10-01 扩展）。

        两段各自独立；失败记 WARNING（带异常类型 %r，INFO 级日志即可见）：
        1) **社区快照**（元素加成 + 言录使）：内部先判「本地是否过期」，
           刚拉过/未过期时是零成本空转（幂等）。
        2) **WM 物品/紫卡武器表**（2026-10-01 扩展）：reload 后缓存全冷，
           首个 wr/wm 指令要现下 2.5MB 物品表——在低内存宿主机上还会叠加
           **swap-in 风暴**（2026-10-01 实测：宿主内存吃紧、进程 VmSwap 高达
           834MB，首个指令触碰冷内存引发换页风暴，Event loop lag 45s，
           用户观感「半天没反应」）。启动时后台预拉，把这笔成本挪出用户
           首次指令的关键路径。幂等：表缓存 TTL 24h，刚拉过是零成本空转。
        ★ 这两张表的方法在 `self.client`（WarframeClient）上，不在插件类上
        （插件类没有 __getattr__ 代理）——2026-10-01 首版写成 `self.wm_items()`
        抛 AttributeError 被静默吞掉，预热从未执行（「已就绪」日志不出现）。
        守卫：tests/test_valence_autorefresh.py 文末「boot warm」段。
        """
        # ★ 2026-10-16（GPT 评估 P04）：逐项独立 try + 各自限时 —— 原先两段社区刷新共用一个 try，
        #   前一段超时会把后一段（静态快照）一起跳过；WM 两张表同理。
        WarframeSDJK._note_background(self, "boot", "running")
        ok, wm_ok, checks = 0, 0, {}
        for tag, make, limit in (
            ("社区快照（元素加成 / 言录使）", self._refresh_from_community, 90),
            ("社区静态快照（仲裁评级 / 奸商）", self._refresh_static_community, 90),
            ("WM 物品表", self.client.wm_items, 120),
            ("WM 紫卡武器表", self.client.wm_riven_weapons, 120),
        ):
            started = time.perf_counter()
            try:
                result = await asyncio.wait_for(make(), timeout=limit)
                outcome, detail = WarframeSDJK._refresh_outcome(result)
                if tag.startswith("WM") and not result:
                    outcome, detail = "degraded", "物品表为空"
                if outcome == "ok":
                    ok += 1
                    wm_ok += tag.startswith("WM")
            except asyncio.CancelledError:
                WarframeSDJK._note_background(self, "boot", "cancelled")
                raise
            except Exception as exc:  # noqa: BLE001 - 单项失败不连累其它预热
                outcome, detail = "error", type(exc).__name__
                logger.warning("[sdjk] 首启预热「%s」跳过/失败：%r", tag, exc)
            checks[tag] = {
                "result": outcome,
                "detail": detail,
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
            }
        if wm_ok == 2:
            logger.info("[sdjk] 首启预热：WM 物品/紫卡武器表已就绪")
        logger.info("[sdjk] 首启预热完成 %d/4 项", ok)
        WarframeSDJK._note_background(
            self,
            "boot",
            "ok" if ok == 4 else "degraded",
            f"{ok}/4 项检查正常；实际采用情况见数据页",
        )
        self._background_status["boot"] = {**self._background_status["boot"], "checks": checks}

    async def _refresh_static_community(self) -> dict:
        """静态快照（仲裁评级 / 奸商到访历史）从 bot-data 拉新；与有没有 FS 无关。"""
        try:
            res = await self.client.refresh_community_static()
        except Exception as exc:  # noqa: BLE001
            logger.debug("[sdjk] 社区静态快照不可用：%s", exc)
            return {"社区静态": "failed"}
        for name, st in res.items():
            if st.startswith("refreshed"):
                logger.info("[sdjk] %s 已从社区快照更新：%s", name, st)
            elif st.startswith("invalid"):
                logger.warning("[sdjk] 社区快照 %s 校验不过，未采用：%s", name, st)
        return res

    async def _refresh_from_community(self) -> dict:
        """无 FS 时的社区快照刷新（元素加成 + 言录使货单）。

        与 `_valence_autoloop` 的 ready 分支相比，这里**只读公开仓的社区快照**
        （普通 GET、免盾），完全不碰 FlareSolverr；也刻意保持安静 —— 成功记一条
        INFO，取不到 / 校验不过一律不吭声（没开该组件的用户不该被这类提示打扰）。
        """
        results = {}
        for tag, make in (
            ("元素加成", lambda: self.client.refresh_valence(community_only=True)),
            ("言录使货单", lambda: self.client.refresh_acrichis_week(community_only=True)),
        ):
            try:
                st = await make()
            except Exception as exc:  # noqa: BLE001
                logger.debug("[sdjk] 社区快照（%s）不可用：%s", tag, exc)
                results[tag] = "failed"
                continue
            results[tag] = st
            if isinstance(st, str) and st.startswith("refreshed"):
                logger.info("[sdjk] %s已从社区快照刷新：%s", tag, st)
        return results

    @staticmethod
    def _secs_to_aligned_tick(step_hours: int = 6, minute: int = 5, jitter_min: int = 15) -> float:
        """距下一个「整 step 小时 + minute 分」UTC 边界的秒数（含随机抖动）。

        ★ 2026-09-25：旧写法是 ``sleep(6*3600)``，检查点跟着**开机时刻**漂；
        而换轮全在 **00:00 UTC**（效价每 4 天、言录使每周）—— 最坏要等 6h 才
        发现新轮次，社区快照（给没装 FS 的用户）也就跟着晚。对齐到 ``XX:05``
        后在对应检查点刷新；抖动 0–15 分钟避免所有实例在同一分钟扎堆打 wiki。
        """
        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc)
        nxt = now.replace(minute=0, second=0, microsecond=0)
        nxt -= timedelta(hours=nxt.hour % step_hours)
        nxt += timedelta(minutes=minute)
        if nxt <= now:
            nxt += timedelta(hours=step_hours)
        nxt += timedelta(minutes=random.randint(0, jitter_min))
        return max(60.0, (nxt - now).total_seconds())

    async def _community_autoloop(self):
        """每 30 分钟检查社区快照，本地仍新鲜时由业务入口直接短路。

        与源页检查独立：HTTP/浏览器不可用时也能更新；FS 开关不控制该任务。
        """
        n = 0
        while True:
            try:
                await self._background_sleep("community", 30 * 60)
                self._note_background("community", "running")
                n += 1
                results = {}
                if n % 6 == 0:
                    results.update(await self._refresh_static_community())
                results.update(await self._refresh_from_community())
                state, detail = self._refresh_outcome(results)
                self._note_background("community", state, detail)
            except asyncio.CancelledError:
                self._note_background("community", "cancelled")
                raise
            except Exception as exc:  # noqa: BLE001 - 轻轮询失败只记状态，不打扰群聊
                self._note_background("community", "error", type(exc).__name__)
                await self._background_sleep("community", 60)

    async def _revision_autoloop(self):
        """轮询文档修订，取固定正文比较业务内容；原六小时刷新仍独立兜底。"""
        delay = random.uniform(60, 120)
        failures = 0
        labels = {"tenet": "信条", "coda": "终幕", "acrithis": "言录使"}
        while True:
            try:
                # 首轮稍后执行，避免与启动预热同时争用源页；重载后仍会恢复观测。
                await self._background_sleep("revision", delay)
                if getattr(self.client, "_closing", False):
                    raise asyncio.CancelledError
                self._note_background("revision", "running")
                result = await asyncio.wait_for(self.client.check_rotation_revisions(), timeout=300)
                allowed = {"fresh", "unchanged", "refreshed", "not-updated", "failed"}
                if (
                    not isinstance(result, dict)
                    or set(result) != set(labels)
                    or any(value not in allowed for value in result.values())
                ):
                    raise ValueError("文档修订检查返回了不完整的结果")
                failed = [labels[key] for key, value in result.items() if value == "failed"]
                pending = [labels[key] for key, value in result.items() if value == "not-updated"]
                changed = [labels[key] for key, value in result.items() if value == "refreshed"]
                if failed:
                    failures += 1
                    state, detail = "degraded", "修订检查失败：" + "、".join(failed)
                elif pending:
                    failures = 0
                    state, detail = "degraded", "尚无可采用的本轮内容：" + "、".join(pending)
                else:
                    failures = 0
                    state = "ok"
                    detail = "文档修订已检查" if changed else "文档修订已检查，未发现内容变化"
                if changed:
                    detail += "；已更新：" + "、".join(changed)
                    logger.info("[sdjk] 文档修订驱动已更新：%s", "、".join(changed))
                self._note_background("revision", state, detail)
            except asyncio.CancelledError:
                self._note_background("revision", "cancelled")
                raise
            except Exception as exc:  # noqa: BLE001 - 新检查失败不影响原定时兜底
                failures += 1
                self._note_background("revision", "error", type(exc).__name__)
                logger.warning("[sdjk] 文档修订检查失败，将退避重试：%s", type(exc).__name__)
            delay = (min(3600, 15 * 60 * 2 ** min(failures, 2))) + random.uniform(0, 120)

    async def _valence_autoloop(self):
        """插件内置定时：每 6 小时检查一轮 wiki 类快照。

        信条/终幕元素加成与言录使货单先尝试普通 HTTP；遇到拦截后，
        按配置使用 FlareSolverr，再由业务入口回退社区快照。
        关闭浏览器代理不关闭源页刷新；失败仍按一小时退避，成功按六小时对齐。
        """
        while True:
            try:
                self._note_background("valence", "running")
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
                        logger.info(
                            "[sdjk] 进程内存自报：%s", " ".join(f"{k}={v}" for k, v in _st.items())
                        )
                except Exception:  # noqa: BLE001 - 非 Linux（本地调试）跳过
                    pass

                # 传输入口先直连，再按配置决定是否启用浏览器；不以前置探活挡住直连。
                status = await self.client.refresh_valence()
                if status != "fresh":
                    logger.info("[sdjk] 元素加成快照已刷新：%s", status)
                # 言录使（Acrithis）本周货单：DE 不下发。过期后自动抓 wiki
                # 《Acrithis/Current Offerings》子页（社区人工维护的当期 5 件，
                # 2026-09-21 起；原先只告警等人工，实际永远没人更）。
                acr_status = "failed"
                try:
                    acr_status = await self.client.refresh_acrichis_week()
                    if acr_status == "refreshed":
                        logger.info("[sdjk] 言录使本周货单已自动刷新")
                    elif acr_status == "not-updated":
                        logger.warning(
                            "[sdjk] 言录使货单待获取或更新，wiki 当期上报尚未更新；保留已有缓存，卡面暂以候选池展示"
                        )
                    elif acr_status == "failed":
                        logger.warning(
                            "[sdjk] 言录使本周货单自动抓取失败："
                            "保留已有运行缓存，后台下轮自动重试；缺值卡面展示待获取与候选池"
                        )
                except Exception:  # noqa: BLE001 - 自检失败不阻断
                    pass
                state, detail = self._refresh_outcome({"效价": status, "言录使": acr_status})
                self._note_background("valence", state, detail)
                await self._background_sleep("valence", self._secs_to_aligned_tick())
            except asyncio.CancelledError:
                self._note_background("valence", "cancelled")
                raise
            except Exception as e:  # noqa: BLE001
                logger.warning("[sdjk] 元素加成快照刷新失败（1 小时后重试）：%s", e)
                self._note_background("valence", "error", type(e).__name__)
                await self._background_sleep("valence", 3600)

    async def terminate(self):
        """合并卸载请求；隔离调用方取消，超时的资源释放在后台继续。"""
        # 先封住新任务入口，再扫描/等待旧任务；兼容没有新生命周期方法的适配器。
        begin_close = getattr(self.client, "begin_close", None)
        if callable(begin_close):
            begin_close()
        else:
            self.client._closing = True

        async def finish():
            async def bounded(coro, label):
                task = asyncio.create_task(coro)
                retained = getattr(self, "_terminate_pending", None)
                if retained is None:
                    retained = self._terminate_pending = set()
                retained.add(task)

                def consume(done):
                    retained.discard(done)
                    if not done.cancelled():
                        try:
                            done.result()
                        except Exception as exc:  # noqa: BLE001 - 超时后的异常也要消费
                            logger.warning("[sdjk] %s收尾异常：%s", label, exc)

                task.add_done_callback(consume)
                done, _ = await asyncio.wait({task}, timeout=5)
                if not done:
                    logger.warning("[sdjk] %s超过 5 秒，后台继续收尾", label)
                    return False
                await task
                return True

            bridge = getattr(self, "_messaging", None)
            local_ocr = getattr(self, "local_ocr", None)
            if local_ocr is not None:
                await bounded(local_ocr.close(), "本地紫卡 OCR 关闭")
            installer = getattr(self, "local_ocr_installer", None)
            if installer is not None:
                await bounded(installer.close(), "本地 OCR 部署任务关闭")
            if bridge is not None:
                await bounded(bridge.close(), "按钮回调关闭")
            getattr(self, "_scan_records", {}).clear()
            getattr(self, "_last_scan", {}).clear()
            # AstrBot 没有注销路由的 API ⇒ 停用后面板接口一律拒绝（见 core/webpanel.py）
            panel = getattr(self, "_panel", None)
            font_task = panel.close() if panel else None
            pending = []
            compare_task = getattr(panel, "_compare_task", None) if panel else None
            if compare_task is not None and not compare_task.done():
                pending.append(compare_task)
            if font_task is not None:
                font_task.add_done_callback(self._log_task_death("font_download"))
                if not font_task.done():
                    pending.append(font_task)
            for name in (
                "_auto_task",
                "_valence_task",
                "_revision_task",
                "_community_task",
                "_boot_task",
                "_warm_task",
            ):
                task = getattr(self, name, None)
                if task and not task.done():
                    task.cancel()
                    pending.append(task)
            # 手动触发的爬取：「排行 刷新」全量价格榜、「紫卡排行 刷新」紫卡 WM 现价（含面板按钮）
            for name in ("_rank_crawl_task", "_riven_rt_crawl_task"):
                crawl = getattr(self.client, name, None)
                if crawl and not crawl.done():
                    crawl.cancel()
                    pending.append(crawl)
            unfinished = set()
            if pending:
                # 等它们真正退出再关 client（只 cancel 不等，在飞的请求会撞上已关闭的连接）；最多等 5 秒，不卡卸载
                _, unfinished = await asyncio.wait(pending, timeout=5)
                if font_task is not None and not font_task.done():
                    logger.warning("[sdjk] 字体下载清理超过 5 秒，后台继续收尾")
            try:
                # ★ 2026-09-26：确认 await 生效并留痕（旧实现在这里静默 —— 一旦 stop 没生效，
                #   旧推送守护会与新实例的并存 → 同一事件推两次，且日志里查不到线索）。
                if await bounded(self.push.stop(), "推送守护停止"):
                    logger.info("[sdjk] 推送守护已确认停止（注销登记后活跃数应为 0）")
            except Exception as exc:  # noqa: BLE001 - 停止失败也要留痕，别静默
                logger.warning("[sdjk] 推送守护停止异常：%s", exc)
            finally:
                await bounded(self.client.close(), "连接池关闭")
            if getattr(self, "_terminate_pending", None) or any(
                not task.done() for task in unfinished
            ):
                logger.warning("[sdjk] 插件卸载已返回，资源仍在后台收尾")
            else:
                logger.info("[sdjk] 插件已卸载")

        task = getattr(self, "_terminate_task", None)
        if task is None:
            task = self._terminate_task = asyncio.create_task(finish())
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
            except Exception:  # 统一在下方取回收尾异常
                break
        if cancelled:
            if not task.cancelled():
                try:
                    task.result()
                except Exception as exc:  # noqa: BLE001 - 保留调用方取消，同时消费收尾异常
                    logger.warning("[sdjk] 已取消卸载请求的收尾异常：%s", exc)
            raise asyncio.CancelledError
        task.result()

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
        # ★ 2026-10-22（玩家报「/help 又被本插件吞了」）：只靠「原文比 message_str 长」还原前缀不稳 ——
        #   适配器 / AstrBot 升级后原文也可能被改写，判据失效就 fail-open 抢答。**内置指令**另看
        #   AstrBot 自己设的 `is_at_or_wake_command`（内置 CommandFilter 正是看它才响应）⇒ 只要内置会接，
        #   我们就让路。第三方占用词（新闻）仍只按前缀判 —— 私聊 / @机器人 也会置该标记，
        #   对它们一并让路会改变「私聊发新闻由本插件回答」的既有行为。
        head = text.split(" ", 1)[0].lower()
        if head in _BUILTIN_COMMANDS and getattr(event, "is_at_or_wake_command", False) is True:
            logger.info("[sdjk] 「%s」是 AstrBot 内置指令（已唤醒），本次让路", head)
            return
        if _wake_prefix_stripped(event):
            if head in _occupied_command_words():
                logger.info("[sdjk] 「/%s」已由内置指令或其他插件占用，本次让路", head)
                return

        # 管理指令命名空间：“.”
        if text.startswith("."):
            admin_parsed = parse(text)
            lang_token = i18n.set_lang(self._resolve_lang(admin_parsed, event.unified_msg_origin))
            try:
                reply = await self._handle_admin(event, text)
                if reply:
                    async for result in self._build_results(event, reply, admin_parsed):
                        yield result
                    event.stop_event()
            finally:
                try:
                    i18n.reset_lang(lang_token)
                except ValueError:
                    pass
            return

        parsed = parse(text)
        if not parsed.command:
            return
        handler = self._routes.get(parsed.command)
        if handler is None:
            return
        platform = parsed.platform or self.groups.platform(event.unified_msg_origin)
        lang_token = i18n.set_lang(self._resolve_lang(parsed, event.unified_msg_origin))
        try:
            async for result in self._dispatch(handler, parsed, event, platform):
                yield result
        finally:
            try:
                i18n.reset_lang(lang_token)
            except ValueError:  # 生成器在别的上下文里被收尾时 token 不可用，保持当前值即可
                pass

    def _resolve_lang(self, parsed: Parsed, umo: str) -> str:
        """显示语言：消息后缀 -en/-zh > 会话 .语言 设置 > 全局 default_language。"""
        groups = getattr(self, "groups", None)
        cfg = getattr(self, "cfg", None) or {}
        return (
            getattr(parsed, "lang", None)
            or (groups.lang(umo) if groups is not None and hasattr(groups, "lang") else None)
            or i18n.coerce_lang(cfg.get("default_language", i18n.DEFAULT_LANG))
        )

    async def _dispatch(self, handler, parsed: Parsed, event: AstrMessageEvent, platform: str):
        try:
            with display_time.use_zone((getattr(self, "cfg", None) or {}).get("display_timezone")):
                reply = await handler(parsed, event, platform)
                if reply and reply.footer:
                    reply = replace(reply, footer=display_footer(reply.footer))
        except WarframeAPIError as exc:
            # 日志仅记异常类别，避免上游异常中的凭据或签名地址泄漏。
            logger.warning("[sdjk] 指令 %s 接口错误：%s", parsed.command, type(exc).__name__)
            reply = Reply(raw_text=f"⚠️ {api_client.public_error_text(exc)}")
        except Exception as exc:  # noqa: BLE001
            frames = " ← ".join(
                f"{Path(frame.filename).name}:{frame.lineno}:{frame.name}"
                for frame in traceback.extract_tb(exc.__traceback__)
            )
            logger.error(
                "[sdjk] 指令 %s 处理异常：%s（位置 %s）",
                parsed.command,
                type(exc).__name__,
                frames,
            )
            reply = Reply(
                raw_text=i18n.L(
                    "⚠️ 内部错误，请稍后重试或联系管理员查看日志",
                    "⚠️ Internal error. Please try again later or ask an admin to check the logs",
                )
            )

        if reply is None:
            return
        async for result in self._build_results(event, reply, parsed):
            yield result
        event.stop_event()

    # ------------------------------------------------------------------
    # 输出构建：-w 文字 / -t 图片 / 自适应
    # ------------------------------------------------------------------
    def _can_send_actions(self, event: AstrMessageEvent) -> bool:
        bridge = getattr(self, "_messaging", None)
        return bool(bridge and bridge.can_send_actions(event))

    @staticmethod
    def _action_text(actions: list[Action]) -> str:
        """保留三种操作的含义；复制游戏密语不能降级成执行机器人指令。"""
        lines = []
        seen = set()
        for action in actions:
            key = (action.mode, action.send)
            if key in seen:
                continue
            seen.add(key)
            if action.mode == "copy":
                lines.append(
                    i18n.L(
                        f"{action.label}（复制到游戏内）：\n{action.send}",
                        f"{action.label} (copy into the game):\n{action.send}",
                    )
                )
            elif action.mode == "fill":
                # 填入类指令末尾留给玩家补参数（如「识卡伤害 #记录 对 」）：引号里不带悬空空格，用「…」示意
                lines.append(
                    i18n.L(
                        f"{action.label}：复制「{action.send.rstrip()} …」补全后发送",
                        f'{action.label}: copy "{action.send.rstrip()} ..." and complete it before sending',
                    )
                )
            else:
                lines.append(
                    i18n.L(
                        f"{action.label}：发送「{action.send}」",
                        f'{action.label}: send "{action.send}"',
                    )
                )
        return "\n".join(lines)

    @staticmethod
    def _page_command(raw: str, page: int) -> str:
        # 保留双引号内的查询和显示模式，替换旧页码（包含第一页 -1）。
        tokens = re.findall(r'"[^\"]*"|\S+', raw)
        tokens = [
            token for token in tokens if not (re.fullmatch(r"-\d+", token) and int(token[1:]) >= 1)
        ]
        if page >= 2:
            tokens.append(f"-{page}")
        return " ".join(tokens)

    @staticmethod
    def _reply_actions(reply: Reply, parsed: Parsed) -> list[Action]:
        # 行布局：复制类（密语）在最上（每行 4 个，最多两行）→ 指令自带的其它按钮按原分组
        # 依次往下 → 快捷指令紧接其后 → 翻页固定末行。每行 4 个是各平台的共同下限
        # （QQ/Discord 5、KOOK 4），指令里写的 row 只当分组用，最终行号在这里统一排。
        per_row = 4
        copies = [a for a in reply.actions if a.mode == "copy"]
        if not copies:
            # 指令没自带复制按钮时才把密语补成按钮；自带了（wm/wr 的「N号」）就不再出第二套编号，
            # 没对上按钮的密语留在正文里。编号沿用卡面序号。
            copies = [
                Action(i18n.L(f"{i}号密语", f"Whisper #{i}"), w, "copy")
                for i, w in enumerate(reply.whisper, 1)
            ]
        actions = [
            replace(a, row=k // per_row if k < per_row * 2 else -1) for k, a in enumerate(copies)
        ]
        first = max([a.row + 1 for a in actions if a.row >= 0], default=0)
        groups: dict[int, list[Action]] = {}
        for a in reply.actions:
            if a.mode != "copy":
                groups.setdefault(a.row, []).append(a)
        for _, items in sorted(groups.items()):
            actions += [replace(a, row=first + k // per_row) for k, a in enumerate(items)]
            first += (len(items) + per_row - 1) // per_row
        shortcuts = []
        if parsed.command == "help":
            shortcuts = i18n.L(
                ["突击", "执刑官", "电波", "奸商", "裂隙", "仲裁", "时效", "舰队进度"],
                [
                    "sortie",
                    "archon",
                    "nightwave",
                    "baro",
                    "fissures",
                    "arbitration",
                    "timers",
                    "construction",
                ],
            )
        elif parsed.command == "timers":
            shortcuts = i18n.L(
                ["突击", "执刑官", "电波", "奸商", "裂隙"],
                ["sortie", "archon", "nightwave", "baro", "fissures"],
            )
        elif parsed.command == "fissures":
            shortcuts = i18n.L(["仲裁"], ["arbitration"])
            actions.append(
                Action(
                    i18n.L("蹲裂隙", "Watch Fissures"),
                    i18n.L("蹲 裂隙 ", "subscribe fissures "),
                    "fill",
                    row=first,
                )
            )
        elif parsed.command == "arbitration":
            shortcuts = i18n.L(["仲裁表"], ["arbtable"])
        taken = sum(1 for a in actions if a.row == first)
        actions.extend(
            Action(word, word, row=first + (taken + i) // per_row)
            for i, word in enumerate(shortcuts)
        )
        _page_text = "\n".join([reply.title, *reply.lines])
        paging = re.search(r"第\s*(\d+)\s*/\s*(\d+)\s*页", _page_text) or re.search(
            r"\(Page\s+(\d+)\s*/\s*(\d+)", _page_text
        )
        # 超出布局容量的动作保留，负行号交给桥接降级文字；分页独占第五行。
        row_limit = 4 if paging and parsed.command and parsed.raw else 5
        actions = [replace(a, row=-1) if a.row >= row_limit else a for a in actions]
        if paging and parsed.command and parsed.raw:
            current, total = map(int, paging.groups())
            if current > 1:
                actions.append(
                    Action(
                        i18n.L("上一页", "Prev"),
                        WarframeSDJK._page_command(parsed.raw, current - 1),
                        row=4,
                    )
                )
            if current < total:
                actions.append(
                    Action(
                        i18n.L("下一页", "Next"),
                        WarframeSDJK._page_command(parsed.raw, current + 1),
                        row=4,
                    )
                )
        if parsed.platform:
            actions = [
                replace(action, send=f"-{parsed.platform} {action.send}")
                if action.mode != "copy" and not parse(action.send).platform
                else action
                for action in actions
            ]
        if getattr(parsed, "lang", None):
            actions = [
                replace(action, send=f"-{i18n.lang_suffix(parsed.lang)} {action.send}")
                if action.mode != "copy" and not parse(action.send).lang
                else action
                for action in actions
            ]
        return actions

    @staticmethod
    def _literal_result(result):
        """关闭本插件纯文字正文的宿主 Markdown，保留玩家名与游戏密语的原始字符。"""
        markdown = getattr(result, "use_markdown", None)
        if callable(markdown):
            markdown(False)
        return result

    async def _build_results(self, event: AstrMessageEvent, reply: Reply, parsed: Parsed):
        bridge = getattr(self, "_messaging", None)
        if bridge is not None:
            bridge.prepare_event(event)
        available = self._can_send_actions(event)
        actions = self._reply_actions(reply, parsed) if available else list(reply.actions)
        body = replace(reply, actions=[])
        # 按钮携带完整密语，成功时不重复输出旧的快捷回复；降级会逐项补回。
        if actions:
            copied = {action.send for action in actions if action.mode == "copy"}
            body.whisper = [value for value in reply.whisper if value not in copied]
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        classes = {cls.__name__ for cls in type(raw).__mro__}
        budget = None
        try:
            if event.get_platform_name() == "qq_official":
                budget = (
                    5 if "GroupMessage" in classes else (4 if "C2CMessage" in classes else None)
                )
        except AttributeError:
            pass
        image_mode = (
            reply.raw_text is None
            and not reply.text_only
            and not parsed.force_text
            and (
                parsed.force_image
                or (render_mode := self._render_mode_for(event)) == "image"
                or (render_mode != "text" and len(reply.lines) > 14 and self.renderer.available)
            )
        )
        markdown_body = None
        if bridge is not None and not image_mode and event.get_platform_name() == "qq_official":
            markdown_body = body.markdown
            # 普通文字卡自动转 Markdown 正文；含特殊字符时 card_markdown 返回 None，照旧发纯文字。
            if (
                markdown_body is None
                and body.raw_text is None
                and not body.pages
                and self.cfg.get("qq_markdown_cards", True)
            ):
                markdown_body = card_markdown(body.title, body.lines, body.footer)
        merge_images = bool(
            image_mode
            and available
            and actions
            and bridge
            and self.cfg.get("qq_merge_image_actions", True)
            and bridge.can_send_image_cards(event)
        )
        expected = max(1, len(reply.pages)) if image_mode else 1
        if merge_images and (body.whisper or body.extra_text):
            expected += 1
        if markdown_body and (body.whisper or body.extra_text):
            expected += 1  # 密语与补充文字独立走字面文本，不能进入 Markdown。
        if budget is not None and expected > budget:
            # 超出图页额度时提示切换到完整文字结果，避免只发部分页面。
            yield self._literal_result(
                event.plain_result(
                    i18n.L(
                        "本次结果超过 QQ 回复页数额度，请在原指令后加 -w 查看完整文字。",
                        "This result exceeds the QQ reply page limit. Add -w to the command to see the full text.",
                    )
                )
            )
            return
        if merge_images and budget is not None and expected >= budget:
            _accepted, unsupported = bridge._select_actions(event, actions)
            if unsupported:
                merge_images = False
        if actions and (
            not available or (budget is not None and expected + (not merge_images) > budget)
        ):
            hint = self._action_text(actions)
            if body.raw_text is not None:
                body.raw_text += "\n\n" + hint
            else:
                body.extra_text = "\n".join(value for value in [body.extra_text, hint] if value)
            actions = []
            merge_images = False
        if markdown_body:
            outcome = await bridge.send_markdown(event, markdown_body, remaining_messages=budget)
            if outcome.sent or outcome.uncertain:
                tail = []
                if body.whisper:
                    tail.append("\n".join(body.whisper))
                if body.extra_text:
                    tail.append(body.extra_text)
                body = replace(body, raw_text="\n\n".join(tail))
                if not body.raw_text:
                    body = None
        image_paths = {}
        used = 1 if markdown_body and body is None else 0
        async for result in self._build_content_results(
            event, body, parsed, image_paths=image_paths
        ):
            result = self._literal_result(result)
            chain = getattr(result, "chain", None)
            paths = image_paths.pop(id(result), None)
            if merge_images and actions and paths and chain:
                images = [seg for seg in chain if isinstance(seg, Image)]
                tail = [seg.text for seg in chain if isinstance(seg, Plain) and seg.text]
                if len(images) == len(paths) and (not body.pages or len(paths) == len(body.pages)):
                    for image in images[:-1]:
                        yield self._literal_result(event.chain_result([image]))
                        used += 1
                    remaining = None if budget is None else budget - used
                    outcome = await bridge.send_image_card(
                        event, paths[-1], actions, remaining_messages=remaining
                    )
                    if outcome.sent or outcome.uncertain:
                        used += 1
                        if outcome.action_fallback:
                            tail.append(self._action_text(outcome.action_fallback))
                        actions = []
                        if tail and (budget is None or used < budget):
                            text = "\n\n".join(tail)
                            if not await bridge.send_literal(event, text):
                                yield self._literal_result(event.plain_result(text))
                            used += 1
                        continue
                    # 明确未发送才回退本页；已发前页不重新渲染或重发。
                    if remaining is not None and remaining < 2:
                        tail.append(self._action_text(actions))
                        actions = []
                    segments = [images[-1], *[Plain(text) for text in tail]]
                    yield self._literal_result(event.chain_result(segments))
                    used += 1
                    continue
            if bridge is not None and chain and all(isinstance(seg, Plain) for seg in chain):
                if await bridge.send_literal(event, "".join(seg.text for seg in chain)):
                    used += 1
                    continue
            yield result
            used = expected
        if not merge_images:
            used = expected
        if actions:
            outcome = await bridge.send_actions(
                event, actions, remaining_messages=None if budget is None else budget - used
            )
            # 未知送达不重发正文或按钮；只提供独立的手动操作说明。
            fallback = actions if outcome.uncertain else outcome.fallback
            if fallback:
                hint = self._action_text(fallback)
                if not await bridge.send_literal(event, hint):
                    yield self._literal_result(event.plain_result(hint))

    async def _build_content_results(
        self, event: AstrMessageEvent, reply: Reply, parsed: Parsed, *, image_paths=None
    ):
        """输出构建（异步生成器）。

        渲染是 PIL 同步重活，必须扔到线程池 —— AstrBot 的单事件循环里
        直接调它会整机卡住（实测 watchdog 记到过 33s 的 stall，栈顶就是
        PIL 的 resize/convert）。文字分支是纯字符串拼接，留在协程里。
        """
        if reply is None:
            return
        if reply.raw_text is not None:
            yield event.plain_result(reply.raw_text)
            return

        whisper_txt = ""
        if reply.whisper:
            whisper_txt = i18n.L(
                "\n\n🔑 快捷回复（复制后游戏内粘贴）：\n",
                "\n\n🔑 Quick replies (copy and paste in game):\n",
            ) + "\n".join(reply.whisper)
        # 卡片之外的补充文本（wiki 链接这类必须可点击的内容）：
        # 与卡片同链发出，链尾的 Plain 段在 QQ 里是可点的文本。
        tail_txt = whisper_txt + (("\n" + reply.extra_text) if reply.extra_text else "")

        use_image = False
        if not reply.text_only:
            if parsed.force_image:
                use_image = True
            elif parsed.force_text:
                use_image = False
            elif self._render_mode_for(event) == "image":
                use_image = True
            elif self._render_mode_for(event) == "text":
                use_image = False
            else:
                use_image = len(reply.lines) > 14 and self.renderer.available

        if use_image and reply.pages:
            # 多页卡片：每页一张图，一链发出（识卡+伤害详情这类组合用）
            comps = []
            has_image = False
            pending_paths = []
            n = len(reply.pages)
            try:
                for i, (ptitle, plines) in enumerate(reply.pages, 1):
                    # 渲染串行：PIL 单线程吃 CPU，容器算力弱，多任务同时渲染
                    # 会互相拖慢（实测叠加时单张 0.7s → 39s）
                    page_title = f"{ptitle}（第{i}/{n}页）"
                    path = await self._render_card(page_title, plines, reply.footer)
                    if path:
                        pending_paths.append(path)
                        comps.append(Image.fromFileSystem(path))
                        has_image = True
                    else:
                        # 任一页失败也保留正文与顺序，不能只发送成功的页。
                        comps.append(Plain(text_card(page_title, plines, reply.footer)))
                        logger.warning("[sdjk] 多页卡片单页渲染为空，已降级文字：%s", page_title)
                if has_image:
                    if tail_txt:
                        comps.append(Plain(tail_txt))
                    result = event.chain_result(comps)
                    if image_paths is not None:
                        image_paths[id(result)] = list(pending_paths)
                    # 整链准备好后才开始上传缓冲计时，避免前页在后页渲染时被删除。
                    for path in pending_paths:
                        self._schedule_card_cleanup(path)
                    pending_paths.clear()
                    yield result
                    return
            finally:
                # 循环中取消/异常时，已经生成却没有交付的前页也要清理。
                for path in pending_paths:
                    self._schedule_card_cleanup(path, delay=0)
            logger.warning(
                "[sdjk] 多页卡片渲染为空，已降级文字输出：%s",
                reply.pages[0][0] if reply.pages else "?",
            )
            # 全部失败直接走完整文字降级，避免再渲染一个空的单页封面。
            use_image = False

        if use_image:
            path = await self._render_card(reply.title, reply.lines, reply.footer)
            if path:
                components = [Image.fromFileSystem(path)]
                if tail_txt:
                    components.append(Plain(tail_txt))
                self._schedule_card_cleanup(path)
                result = event.chain_result(components)
                if image_paths is not None:
                    image_paths[id(result)] = [path]
                yield result
                return
            # 字体缺失等渲染失败 → 降级文字（失败原因见 render.py 的异常日志）
            logger.warning("[sdjk] 图片渲染为空，已降级文字输出：%s", reply.title)

        if reply.pages:
            # 文本降级：多页按分节拼接
            card = "\n\n".join(text_card(pt, pl, reply.footer) for pt, pl in reply.pages)
        else:
            card = text_card(reply.title, reply.lines, reply.footer)
        yield event.plain_result(card + tail_txt)

    async def _render_card(self, title: str, lines: list[str], footer: str) -> Optional[str]:
        """调用方取消不停止渲染线程：持锁直到线程结束，并清理未发送的图片。"""
        if WarframeSDJK._render_lock is None:
            WarframeSDJK._render_lock = asyncio.Lock()

        async def run():
            async with WarframeSDJK._render_lock:
                return await asyncio.to_thread(self.renderer.render, title, lines, footer)

        tasks = getattr(self, "_card_render_tasks", None)
        if tasks is None:
            tasks = self._card_render_tasks = set()
        task = asyncio.create_task(run())
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:

            def cleanup(finished):
                if finished.cancelled():
                    return
                try:
                    path = finished.result()
                except Exception as exc:  # noqa: BLE001 - 取消后的线程异常也要取回并留痕
                    logger.warning("[sdjk] 已取消回复的渲染任务失败：%r", exc)
                else:
                    if path:
                        self._schedule_card_cleanup(path, delay=0)

            task.add_done_callback(cleanup)
            raise

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
    @staticmethod
    def _qq_group_role(event: AstrMessageEvent) -> Optional[str]:
        """读取官方群事件当前发送者的角色，不提升 AstrBot 全局权限。"""
        try:
            if event.get_platform_name() != "qq_official":
                return None
            group_id = str(event.get_group_id() or "")
            sender_id = str(event.get_sender_id() or "")
            raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
            data = getattr(raw, "raw_data", None)
            if not group_id or not sender_id or not isinstance(data, dict):
                return None
            author = data.get("author")
            if not isinstance(author, dict):
                return None
            group_matches = data.get("group_openid") == group_id
            sender_matches = author.get("member_openid") == sender_id
            role = author.get("member_role")
            valid_role = (
                role if isinstance(role, str) and role in ("member", "admin", "owner") else None
            )
            # 只记角色与匹配结果，不记录群号、OpenID 或消息正文。
            logger.info(
                f"[warframe] QQ 官方群角色检查: member_role={valid_role or 'missing_or_invalid'}, "
                f"role_present={'member_role' in author}, "
                f"group_matches={group_matches}, sender_matches={sender_matches}"
            )
            return valid_role if group_matches and sender_matches else None
        except Exception:  # 角色缺失或不完整时保持拒绝，由明确配置的超管兜底。
            return None

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
        if self._qq_group_role(event) in ("admin", "owner"):
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

    @staticmethod
    def _menu_text() -> str:
        """平台指令菜单清单：QQ 官方面板照此手动录入；Telegram/Discord 由 AstrBot 自动同步。"""
        L = i18n.L
        lines = [
            L(
                "📋 QQ 官方「指令面板」录入清单（开放平台管理端 → 高级设置，指令类型选「指令」）",
                "📋 QQ official bot command panel (open platform console → Advanced settings, type: command)",
            )
        ]
        for i, entry in enumerate(MENU_COMMANDS, 1):
            lines.append(f"{i}. {entry.zh} — {entry.desc_zh}")
        lines.append(
            L(
                "点选后指令会填进输入框，带参数的补上名称再发送；群聊需要 @机器人。",
                "Tapping an entry fills it into the input box; add the name where needed, then send. "
                "In groups, mention the bot.",
            )
        )
        lines.append("")
        if MENU_SHELLS:
            lines.append(
                L(
                    "Telegram / Discord：已注册以下指令，AstrBot 会自动同步到平台菜单：",
                    "Telegram / Discord: these commands are registered and AstrBot syncs them to the platform menu:",
                )
            )
            lines.append(" ".join(f"/{name}" for name in MENU_SHELLS))
        else:
            lines.append(
                L(
                    "Telegram / Discord：当前 AstrBot 不支持指令注册，平台菜单不会同步。",
                    "Telegram / Discord: this AstrBot version cannot register commands, so no menu is synced.",
                )
            )
        return "\n".join(lines)

    async def _handle_admin(self, event: AstrMessageEvent, text: str) -> Optional[Reply]:
        tokens = text[1:].strip().split()
        if not tokens:
            return None
        cmd, args = tokens[0], tokens[1:]
        umo = event.unified_msg_origin

        if cmd.lower() in ("语言", "language", "lang"):
            L = i18n.L
            if not self._is_admin(event):
                return Reply(
                    raw_text=L("⛔ 该指令需要群管理员权限", "⛔ Group admin permission required")
                )
            usage = L(
                "用法：.语言 cn / .语言 en / .语言 默认（跟随全局设置）",
                "Usage: .language en / .language cn / .language default (follow the global setting)",
            )
            if args:
                word = args[0].strip().lower()
                if word in ("默认", "跟随", "default", "reset", "auto"):
                    await self.groups.set_lang(umo, None)
                    with i18n.use_lang(self._resolve_lang(parse(""), umo)):
                        cur = i18n.lang_label(i18n.current_lang())
                        return Reply(
                            raw_text=i18n.L(
                                f"✅ 本会话语言已恢复跟随全局设置（当前：{cur}）",
                                f"✅ This chat now follows the global language setting (currently: {cur})",
                            )
                        )
                new_lang = i18n.normalize_lang(word)
                if not new_lang:
                    return Reply(raw_text=usage)
                await self.groups.set_lang(umo, new_lang)
                with i18n.use_lang(new_lang):
                    return Reply(
                        raw_text=i18n.L(
                            "✅ 本会话显示语言已设为中文，可用 -en 临时切换",
                            "✅ Display language for this chat set to English. Use -cn for a one-off switch",
                        )
                    )
            own = self.groups.lang(umo)
            cur = i18n.lang_label(i18n.current_lang())
            return Reply(
                raw_text=(
                    L(f"当前显示语言：{cur}", f"Current display language: {cur}")
                    + ("" if own else L("（跟随全局设置）", " (following the global setting)"))
                    + "\n"
                    + usage
                )
            )

        if cmd.lower() in ("菜单", "menu"):
            return Reply(raw_text=self._menu_text())

        # 管理指令的英文写法：归一成中文规范词再走原逻辑
        _lc = cmd.lower()
        if _lc in ("platform", "defaultplatform"):
            cmd = "默认平台"
        elif _lc in ("enable", "on"):
            cmd = "开启"
        elif _lc in ("disable", "off"):
            cmd = "关闭"
        elif _lc == "status":
            cmd = "状态"
        if cmd in ("开启", "关闭") and args:
            _a0 = {"push": "推送", "chat": "聊天"}.get(args[0].lower())
            if _a0:
                args = [_a0, *args[1:]]
        L = i18n.L

        if cmd in ("默认平台", "平台"):
            if not self._is_admin(event):
                return Reply(
                    raw_text=L("⛔ 该指令需要群管理员权限", "⛔ Group admin permission required")
                )
            if args:
                plat = normalize_platform(args[0])
                if not plat:
                    return Reply(
                        raw_text=L("用法：.默认平台 pc/ps/xb/sw", "Usage: .platform pc/ps/xb/sw")
                    )
                await self.groups.set_platform(umo, plat)
                return Reply(
                    raw_text=L(
                        f"✅ 本群默认平台已设为 {PLATFORM_DISPLAY[plat]}"
                        f"（{plat}），可用 -pc/-ps 等临时覆盖",
                        f"✅ Default platform for this chat set to {PLATFORM_DISPLAY[plat]} "
                        f"({plat}). Use -pc/-ps etc. for a one-off override",
                    )
                )
            cur = self.groups.get(umo)
            _pd = PLATFORM_DISPLAY.get(cur["platform"], cur["platform"])
            return Reply(raw_text=L(f"当前默认平台：{_pd}", f"Current default platform: {_pd}"))

        if cmd in ("开启", "关闭"):
            if not self._is_admin(event):
                return Reply(
                    raw_text=L("⛔ 该指令需要群管理员权限", "⛔ Group admin permission required")
                )
            if not args or args[0] not in ("推送", "聊天"):
                return Reply(
                    raw_text=L(
                        "用法：.开启 推送 / .关闭 推送", "Usage: .enable push / .disable push"
                    )
                )
            value = cmd == "开启"
            await self.groups.set_switch(umo, args[0], value)
            state = "已开启" if value else "已关闭"
            tail = ""
            if args[0] == "推送":
                n_subs = len(self.subs.for_umo(umo))
                if value:
                    tail = (
                        L(f"，已有 {n_subs} 条订阅恢复推送", f"; {n_subs} subscription(s) resumed")
                        if n_subs
                        else L(
                            "，可以用「蹲 类型」订阅推送了",
                            '; you can now subscribe with "subscribe <type>"',
                        )
                    )
                else:
                    tail = (
                        L(
                            f"，已有 {n_subs} 条订阅暂停（订阅保留，重新 .开启 推送 后恢复）",
                            f"; {n_subs} subscription(s) paused (kept; they resume after .enable push)",
                        )
                        if n_subs
                        else ""
                    )
            _feat = {"推送": "push", "聊天": "chat"}[args[0]]
            _st_en = "enabled" if value else "disabled"
            return Reply(
                raw_text=L(
                    f"✅ {args[0]}功能{state}{tail}",
                    f"✅ {_feat.capitalize()} {_st_en}{tail}",
                )
            )

        if cmd == "状态":
            # ★ 安全审查（2026-09-18）：这条会列出**本群全部订阅明细**
            #   （谁订了什么、还剩多久）与缓存统计，却完全没有权限校验 ——
            #   群里任何人都能看。改为与其它管理指令一致。
            if not self._is_admin(event):
                return Reply(
                    raw_text=L(
                        "⛔ 该指令需要群管理员权限（会列出本群订阅明细）",
                        "⛔ Group admin permission required (lists this chat's subscription details)",
                    )
                )
            g = self.groups.get(umo)
            subs = self.subs.for_umo(umo)
            cache = self.client.cache.stats()
            lines = [L("◆ 本群", "◆ This Chat")]
            _pd = PLATFORM_DISPLAY.get(g["platform"], g["platform"])
            lines.append(L(f"· 默认平台　{_pd}", f"· Default platform　{_pd}"))
            own_lang = self.groups.lang(umo)
            _ll = i18n.lang_label(i18n.current_lang())
            lines.append(
                L(f"· 显示语言　{_ll}", f"· Display language　{_ll}")
                + ("" if own_lang else L("（跟随全局）", " (global setting)"))
            )
            lines.append(
                L(
                    f"· 推送　{'开' if g['push'] else '关'}",
                    f"· Push　{'On' if g['push'] else 'Off'}",
                )
            )
            lines.append(L(f"· 蹲订阅　{len(subs)} 条", f"· Subscriptions　{len(subs)}"))
            if subs:
                lines.append(L("◆ 订阅明细", "◆ Subscription Details"))
                for i, s in enumerate(subs, 1):
                    if s.until < 0:
                        life = (
                            L("不限时", "no time limit")
                            if s.hits_left is not None
                            else L("永久", "permanent")
                        )
                    else:
                        left = s.until - time.time()
                        life = (
                            L(
                                f"剩{left // 86400:.0f}天{left % 86400 // 3600:.0f}小时",
                                f"{left // 86400:.0f}d {left % 86400 // 3600:.0f}h left",
                            )
                            if left >= 3600
                            else L(
                                f"剩{max(left, 0) // 60:.0f}分钟", f"{max(left, 0) // 60:.0f}m left"
                            )
                        )
                    mode = (
                        L(f"剩{s.hits_left}次", f"{s.hits_left} hit(s) left")
                        if s.hits_left is not None
                        else (L("一次", "once") if s.once else L("持续", "ongoing"))
                    )
                    lines.append(
                        f"· {i}. {s.event}"
                        + (L(f"：{s.rule}", f": {s.rule}") if s.rule else "")
                        + f"　{mode}·{life}"
                    )
            lines.append(L("◆ 系统", "◆ System"))
            lines.append(
                L(
                    f"· 缓存　{cache['size']} 项 · 命中率 {cache['hit_rate'] * 100:.0f}%",
                    f"· Cache　{cache['size']} items · hit rate {cache['hit_rate'] * 100:.0f}%",
                )
            )
            return Reply(
                f"{BRAND} {WATERMARK_VERSION}",
                lines,
                footer=fmt.fmt_platform_footer(self.groups.platform(umo)),
            )

        if cmd == "锚点":
            # ★★ 安全审查（2026-09-18）发现：本指令写入的锚点**没有任何读取方**。
            #   仲裁表已改走 arbi.wf.wiki 的确定性排期，当初承接校准的
            #   `de_worldstate.arb_from_anchor` 已删除（2026-09-25 清死代码）
            #   —— 也就是说用户「校准」完其实毫无效果；而写入点是
            #   全局的（cfg + runtime/arb_anchor.json），任何群的群管都能覆盖，
            #   属于跨租户写入。
            #   按项目铁律「失效功能必须给出真实可用的替代指令」，这里保留指令名
            #   但**明确告知已废弃**，且不再写任何全局状态。
            return Reply(
                raw_text=i18n.L(
                    "「.锚点」已废弃：仲裁表现在按 arbi.wf.wiki 的"
                    "确定性排期自动推算，不需要手动校准。\n"
                    "· 看当前与下一小时场次：发「仲裁」\n"
                    "· 看未来 14 天排期：发「仲裁表」（可筛类型，如「仲裁表 生存」）",
                    '".锚点" is deprecated: the Arbitration table is now computed from the '
                    "arbi.wf.wiki fixed schedule and needs no manual calibration.\n"
                    '- Current and next-hour missions: send "arbitration"\n'
                    '- The next 14 days: send "arbtable" (filter by type, e.g. "arbtable survival")',
                )
            )
        if cmd in ("帮助", "help"):
            return await self._h_help(None, event, self.groups.platform(umo))
        return None

    # ------------------------------------------------------------------
    # 世界状态类 handler
    # ------------------------------------------------------------------
    async def _gather(self, coros):
        return await asyncio.gather(*coros, return_exceptions=True)

    # ------------------------------------------------------------------
    # 资料与计算器
    # ------------------------------------------------------------------
    async def _h_help(self, parsed, event, platform) -> Reply:
        """裸「帮助」：单页指令总览（2026-09-14 应维护者要求去掉「帮助 分类」
        子指令——意义不明，一页足够）。

        2026-09-19 加注脚：本插件指令是**裸词**触发（AstrBot 会把 "/" 前缀
        剥掉，见文件头「指令空间避让」），而 "/help" 属于 AstrBot 内置指令，
        已被本插件主动让路 —— 说明一句，免得用户以为帮助坏了。
        """
        topics, notes = (HELP_TOPIC_EN, HELP_NOTES_EN) if i18n.is_en() else (HELP_TOPIC, HELP_NOTES)
        lines = [f"※ {n}" for n in notes.get("", [])]
        for t, items in topics.items():
            lines.append(f"◆ {t}")
            # 全角空格分隔：渲染层按此切成两列并做首字符垂直线对齐
            lines += [f"· {c}　{d}" for c, d in items]
            lines += [f"※ {n}" for n in notes.get(t, [])]
        if i18n.is_en():
            return Reply(f"{BRAND} Command List", lines, footer=fmt.fmt_platform_footer(platform))
        return Reply(f"{BRAND} 指令一览", lines, footer=fmt.fmt_platform_footer(platform))

    # ------------------------------------------------------------------
    # 蹲（后台推送订阅）
    # ------------------------------------------------------------------
    async def _h_status_cmd(self, parsed, event, platform) -> Reply:
        """状态指令（同 .状态）。"""
        return await self._handle_admin(event, ".状态")

    # ------------------------------------------------------------------
    # 推送回调
    # ------------------------------------------------------------------
    async def _push_send(self, umo: str, text: str, at=None):
        """推送发送回调。`at` = 本批命中订阅的发起人 id（A1 特例，可空）。

        ★ A1（2026-10-03 用户批准）：aiocqhttp / qq_official 平台拼 At 组件；
        其余平台/解析不到一律回落纯文本（不得因 @ 不支持而丢推送）；
        不做 @全体（at_targets 已剔除 `all`）。
        """
        bridge = getattr(self, "_messaging", None)
        if bridge is not None and await bridge.send_proactive(umo, text):
            return
        chain = MessageChain().message(text)
        platform = self._platform_name_of(umo)
        ids = at_targets(platform, at)
        qq_mentions = False
        if ids:
            try:
                from astrbot.api.message_components import At

                chain = MessageChain()
                for i, q in enumerate(ids):
                    if i and platform == "qq_official":
                        chain.message(" ")
                    chain.chain.append(At(qq=q))
                # QQ 官方纯文本不解析提及；反斜线方括号在客户端会被错误排版。
                # 用已在首尔手机验证的实体保留正文符号，并将名单与正文分行。
                qq_mentions = platform == "qq_official"
                body = (
                    re.sub(r"([\\`*_{}\[\]()#+.!|><~&-])", lambda m: f"&#{ord(m.group(1))};", text)
                    if qq_mentions
                    else text
                )
                chain.message("\n" + body if qq_mentions else body)
            except Exception:  # noqa: BLE001 - At 不可用 ⇒ 纯文本，不丢推送
                qq_mentions = False
                chain = MessageChain().message(text)
        markdown = getattr(chain, "use_markdown", None)
        if callable(markdown):
            markdown(qq_mentions)
        if await self.context.send_message(umo, chain) is False:
            raise RuntimeError("推送未发送：目标平台实例不可用")

    def _platform_name_of(self, umo: str) -> str:
        """umo 首段 = 平台实例 id（如「客服小祥」）⇒ 经 Context 解析适配器
        名（如 aiocqhttp）。解析不到返回空串（@ 回落纯文本）。"""
        try:
            pid = (umo or "").split(":", 1)[0]
            inst = self.context.get_platform_inst(pid) if pid else None
            return (inst.meta().name or "") if inst else ""
        except Exception:  # noqa: BLE001
            return ""
