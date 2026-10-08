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
import random
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Optional

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Image, Plain
from astrbot.api.star import Context, Star, register

try:  # 允许脱离 AstrBot 直接跑单元测试
    from .core import __brand__ as BRAND
    from .core import api_client
    from .core.api_client import WarframeAPIError, WarframeClient, parse_url_list
    from .core import formatters as fmt
    from .core import paths as core_paths
    from .core import wiki_intro
    from .core.parser import Parsed, normalize_platform, parse, PLATFORM_DISPLAY
    from .core.push import PushDaemon, at_targets
    from .core.render import ImageRenderer, WATERMARK_VERSION, migrate_legacy_user_fonts, text_card
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
    from .core.commands.base import _num
    from .core.store import GroupStore, SubscriptionStore
except ImportError as _imp_err:  # pragma: no cover
    # ★ 2026-09-25 事故修正：**只在确实不是包上下文时**才回退到绝对导入
    #   （脱离 AstrBot 单独跑脚本/测试的情形）。包内导入失败 = 真错误，
    #   必须原样抛出 —— 否则真错误会被换成 "No module named 'core'"：
    #   当天服务器从市场安装 v1.0.7 时，新 main.py 要的 render 函数还没落地，
    #   报错就这样被伪装成"模块结构坏了"，面板与日志第一眼全被误导。
    if __package__:
        raise
    from core import __brand__ as BRAND

    # 打包器门面锚点：OSS_FACADE_DELETIONS 按此行整块删除，勿删/勿改前缀
    from core import api_client
    from core.api_client import WarframeAPIError, WarframeClient, parse_url_list
    from core import formatters as fmt
    from core import paths as core_paths
    from core import wiki_intro
    from core.parser import Parsed, normalize_platform, parse, PLATFORM_DISPLAY
    from core.push import PushDaemon, at_targets
    from core.render import ImageRenderer, WATERMARK_VERSION, migrate_legacy_user_fonts, text_card
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
    from core.commands.base import _num
    from core.store import GroupStore, SubscriptionStore


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
_OCCUPIED_FALLBACK = frozenset(
    {
        # AstrBot 内置指令（astrbot/builtin_stars/builtin_commands/main.py）
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
    t = _MD_HR.sub("", t)  # 分隔线 --- / ***
    t = _MD_BOLD.sub(r"\1", t)  # **加粗** → 加粗
    t = _MD_ITALIC.sub(r"\1", t)  # *斜体* → 斜体
    t = _MD_HEAD.sub("", t)  # ## 标题 → 标题
    t = _MD_QUOTE.sub("", t)  # > 引用 → 引用
    t = _MD_LIST.sub(r"\1· ", t)  # - 列表 → · 列表（保留视觉分段）
    t = _MD_OLIST.sub(r"\1", t)  # 1. 列表 → 列表
    t = _MD_CODE.sub("", t)  # 行内代码 / 围栏
    # 清理因剥离产生的多余空行
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip() if t.strip() else text


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
# ★ 两条硬规则（2026-09-17 改版，用户反馈「主指令乱排 / 缺指令 / 看不懂」）：
#   1. 左列必须是**真实主指令名**（可照发）＋常用别名，不再是「平台切换」这类
#      描述性标签 —— 用户是照着这行去发指令的。
#   2. **全部主指令（当前 55 个）必须出现**，由 tests/test_help_coverage.py 锁住，
#      以后新增主指令而忘了写进帮助会直接测试失败（条数按 COMMAND_ALIASES 现算，
#      不写死在测试里）。
#   分组依据是「玩家在什么场景下会想用它」，同类指令聚在一组。


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
        (
            "赏金 / 声望",
            "各地区赏金轮次与奖励（「声望」= 赏金；裸发地区词亦可，地球 = 地球赏金 = 地球声望）",
        ),
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
        ("碎银兑换 / 碎银", "Palladino 裂罅碎块商店（钢铁守望）"),
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
        ("翻译", "中英名称对照（纯文本，如 翻译 腐蚀投射）"),
        ("赤毒 / 钢铁之路", "赤毒历史｜Teshin 荣誉商店"),
    ],
    "遗物与资源": [
        ("遗物 / 核桃", "奖励与出处；出库 / 入库 / 列表"),
        ("开核桃", "遗物收益筛选：速刷|全部 低价|高价"),
        ("部件 物品名", "Prime 部件与蓝图出处"),
        ("金垃圾 / 银垃圾 / 铜垃圾", "按杜卡德价值分档清单"),
    ],
    "伤害与配卡": [
        (
            "伤害 武器 对 敌人 100级 膛线",
            "面板/单发/DPS/含段合计；可加 爆头 病毒10 剥甲 满镀层 钢路",
        ),
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
        (
            "wm 物品名",
            "在售/收购；部件查单加 蓝图/机体/系统/头部；可加 满级 / 光辉 / 墨染 / N个 / -r 密语",
        ),
        ("趋势 物品名", "48h / 90d 价格走势"),
    ],
    "后台推送": [
        ("蹲 类型 筛选 时长 时间", "订阅世界状态变化并推送（需先 .开启 推送）"),
        ("蹲 取消", "不带词=全部；带筛选词=只取消匹配项"),
        ("蹲 筛选写法", "一个词=一个条件（钢铁防御）；多词=多条件取或"),
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
    "astrbot_plugin_warframe_sdjkbot", "SDJK", f"{BRAND}：世界状态 / 市场查价 / 蹲点推送", "1.1.8"
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
        # 运行期数据目录：data/plugin_data/<插件名>（规范要求），不再写插件包目录。
        data_dir = _resolve_data_dir()
        self.data_dir = data_dir
        _migrate_legacy_user_data(data_dir)  # 旧版写在插件目录的数据一次性搬家
        core_paths.set_run_dir(data_dir)  # core 层的快照/缓存同源
        # FlareSolverr / 知识库：**开源版要能自己填**（自用版默认值照旧）。
        #   地址按逗号或换行分隔；填了非法值就回退内置默认，不静默猜。
        _flare_urls = parse_url_list(str(self.cfg.get("flaresolverr_urls") or ""))
        self.client = WarframeClient(
            kuvalog_url=(self.cfg.get("kuvalog_url") or ""),
            worldsource=self.cfg.get("worldsource", "de"),
            worldstate_base=self.cfg.get("worldstate_base", "https://api.warframestat.us"),
            timeout=_num(self.cfg, "http_timeout", 15.0),
            proxy=(self.cfg.get("proxy") or None),
            flare_enabled=bool(self.cfg.get("flaresolverr_enabled", True)),
            flare_urls=_flare_urls or None,
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
        self._last_scan: dict[str, tuple[float, dict, dict]] = {}  # 会话→(时刻, 武器, 折算spec)
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
        )
        self._routes = self._build_routes()

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
            "relic": self._h_relic,
            "parts": self._h_parts,
            "ducats": self._h_ducats,
            "dun": self._h_dun,
            "status": self._h_status_cmd,
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

    async def initialize(self):
        self.push.start()
        self._auto_task = asyncio.create_task(self._rank_autoloop())
        self._auto_task.add_done_callback(self._log_task_death("_rank_autoloop"))
        self._valence_task = asyncio.create_task(self._valence_autoloop())
        self._valence_task.add_done_callback(self._log_task_death("_valence_autoloop"))
        # 社区快照轻轮询：只对「没装/连不上 FS」的用户生效（见 _community_autoloop）
        self._community_task = asyncio.create_task(self._community_autoloop())
        self._community_task.add_done_callback(self._log_task_death("_community_autoloop"))
        # 首启后台预热（2026-09-29 追加批，2026-10-01 扩展为 _boot_warm）：
        # ①社区快照 ②WM 物品/紫卡武器表（见 `_boot_warm`）。此前社区快照只有
        # 轻轮询的 30 分钟 tick 才拉，没装 FS 的用户首启最坏要等半小时；
        # 而 WM 表是 reload 后首条 wr/wm 指令要现下的，挪到启动期后台预拉。
        # fire-and-forget：异步不阻塞加载、超时收尾、失败记 WARNING（可见）。
        _boot_task = asyncio.create_task(self._boot_warm())
        _boot_task.add_done_callback(self._log_task_death("_boot_warm"))
        # 后台预热伤害计算的全部重 JSON + 武器名索引（不阻塞启动）：
        # 不预热时第一条指令要现读武器库/进化/灵化形态/多段/部署表，叠加后
        # 会让首条指令明显变慢（2026-09-17 用户反馈「半天才出来」）。
        asyncio.create_task(asyncio.to_thread(self._warmup))
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
            logger.info(
                "[sdjk] 预热完成 %.0f ms（武器库/进化/灵化形态/多段/部署）",
                (_t.perf_counter() - _t0) * 1000,
            )
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
        # ★ 2026-10-03 修复（价格排行自动刷新失效事故）：原写
        #   `api_client.RANKS_FILE` —— 该常量不存在（api_client 只有 RANKS_NAME），
        #   且本行在 try 之外 ⇒ AttributeError 冒泡出协程、零日志静默死亡
        #   （榜单陈旧 8 天，48h 内「价格榜单」日志 0 条）。
        #   两处一并修：① 经 core_paths.read_path() 解析为**绝对路径**（裸字符串
        #   "wm_ranks.json" 会被当相对路径、随 CWD 读不到 ⇒ ts="" ⇒ 永远判过期）；
        #   ② 挪进 try —— 后续任何异常都能落到下面的 warning，不再静默。
        while True:
            try:
                RANKS_FILE = core_paths.read_path(api_client.RANKS_NAME)
                data = self.client._load_json_file(RANKS_FILE) or {}
                ts = data.get("ts") or ""
                import time as _t
                from datetime import datetime

                try:
                    age = _t.time() - datetime.fromisoformat(ts).timestamp()
                except ValueError:
                    age = float("inf")
                if age > 48 * 3600:
                    # 闲时闸门：白天不爬（会拖慢用户的 wm/wr 查询）
                    if datetime.now().hour not in self.RANK_CRAWL_IDLE_HOURS:
                        logger.info(
                            "[sdjk] 价格榜单已过期，但非闲时（%02d 点），凌晨 %d-%d 点再爬",
                            datetime.now().hour,
                            self.RANK_CRAWL_IDLE_HOURS[0],
                            self.RANK_CRAWL_IDLE_HOURS[-1],
                        )
                        await asyncio.sleep(1800)
                        continue
                    logger.info("[sdjk] 价格榜单缺失或超 48 小时，开始自动重建")
                    n = await self.client.crawl_wm_ranks(limit=self.RANK_CRAWL_MAX_PER_RUN)
                    logger.info("[sdjk] 价格榜单本轮抓取结束（累计 %d 项）", n)
                await asyncio.sleep(1800)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.warning("[sdjk] 价格榜单自动刷新失败，10 分钟后重试：%s", e)
                await asyncio.sleep(600)

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
            "（信条/终幕元素加成、言录使货单）将沿用**社区快照或随包快照**"
            "（社区快照来自公开仓 bot-data 分支，免盾，有就自动用），不再直连 wiki。"
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
        try:
            await asyncio.wait_for(self._refresh_from_community(), timeout=90)
        except Exception as exc:  # noqa: BLE001 - 首启拉取不阻塞启动
            logger.warning("[sdjk] 首启社区快照拉取跳过/失败：%r", exc)
        try:
            await asyncio.wait_for(self.client.wm_items(), timeout=120)
            await asyncio.wait_for(self.client.wm_riven_weapons(), timeout=120)
            logger.info("[sdjk] 首启预热：WM 物品/紫卡武器表已就绪")
        except Exception as exc:  # noqa: BLE001 - 首启预热不阻塞启动
            logger.warning("[sdjk] 首启 WM 表预热失败：%r", exc)

    async def _refresh_from_community(self) -> None:
        """无 FS 时的社区快照刷新（元素加成 + 言录使货单）。

        与 `_valence_autoloop` 的 ready 分支相比，这里**只读公开仓的社区快照**
        （普通 GET、免盾），完全不碰 FlareSolverr；也刻意保持安静 —— 成功记一条
        INFO，取不到 / 校验不过一律不吭声（没开该组件的用户不该被这类提示打扰）。
        """
        for tag, make in (
            ("元素加成", lambda: self.client.refresh_valence(community_only=True)),
            ("言录使货单", lambda: self.client.refresh_acrichis_week(community_only=True)),
        ):
            try:
                st = await make()
            except Exception as exc:  # noqa: BLE001
                logger.debug("[sdjk] 社区快照（%s）不可用：%s", tag, exc)
                continue
            if isinstance(st, str) and st.startswith("refreshed"):
                logger.info("[sdjk] %s已从社区快照刷新：%s", tag, st)

    @staticmethod
    def _secs_to_aligned_tick(step_hours: int = 6, minute: int = 5, jitter_min: int = 15) -> float:
        """距下一个「整 step 小时 + minute 分」UTC 边界的秒数（含随机抖动）。

        ★ 2026-09-25：旧写法是 ``sleep(6*3600)``，检查点跟着**开机时刻**漂；
        而换轮全在 **00:00 UTC**（效价每 4 天、言录使每周）—— 最坏要等 6h 才
        发现新轮次，社区快照（给没装 FS 的用户）也就跟着晚。对齐到 ``XX:05``
        后最坏约 5 分钟；抖动 0–15 分钟避免所有实例在同一分钟扎堆打 wiki。
        """
        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc)
        nxt = (now + timedelta(hours=step_hours)).replace(minute=0, second=0, microsecond=0)
        nxt -= timedelta(hours=nxt.hour % step_hours)
        nxt += timedelta(minutes=minute + random.randint(0, jitter_min))
        return max(60.0, (nxt - now).total_seconds())

    async def _community_autoloop(self):
        """社区快照轻轮询（每 30 分钟，只服务「没有可用 FS」的用户）。

        与 `_valence_autoloop` 分开的原因：那条循环对 FS 关闭/不可达的用户是
        **整段跳过**的，而他们正是社区快照要服务的人。这里每次只读一个几 KB 的
        公开 JSON，且只有本地快照确实过期时才会落盘；成功记 INFO，其余静默。
        """
        while True:
            try:
                await asyncio.sleep(30 * 60)
                if await self._flare_phase() == "ready":
                    continue  # 有 FS 的用户直连 wiki，不必读快照
                await self._refresh_from_community()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - 轻轮询失败不打扰
                await asyncio.sleep(60)

    async def _valence_autoloop(self):
        """插件内置定时：每 6 小时检查一轮 wiki 类快照。

        ① 信条/终幕元素加成（换轮即刷新）；② 变体倾向表（7 天过期重抓）；
        ③ 言录使货单。三者都需要 FlareSolverr（**可选组件**）。

        ★ 告警分级（2026-09-25 v1.0.8，issue #1 用户实测反馈：更新后连收
        「元素加成/变体倾向表刷新失败（All connection attempts failed）」）：
          · 配置关掉 FS      → 整段跳过：不尝试、不告警（那是他自己的选择）；
          · 开着但不可达     → 跳过尝试 + 24h 一次的处置提示（见上），
                              顺带省掉每轮 3×超时的等待；
          · 可达但求解失败   → 照旧每轮 WARN（真故障，绝不静默）。
        """
        while True:
            try:
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

                # ── FlareSolverr 告警分级（v1.0.8）：三态分流见 _flare_phase ──
                phase = await self._flare_phase()
                if phase != "ready":
                    if phase == "absent":
                        # 开着但没有可达的 FS：24h 一次的处置提示（不每小时打扰）
                        self._warn_flare_absent_once()
                    # phase == "off"（面板关掉）→ 不尝试 FS 也不告警
                    # ★ 2026-09-25：但**社区快照与 FS 无关**（普通 GET，免盾）——
                    #   没部署 FS 的用户正是它要服务的人，所以这里仍走一次
                    #   community_only 刷新；全程静默（成功记 INFO，取不到不说话）。
                    await self._refresh_from_community()
                    await asyncio.sleep(self._secs_to_aligned_tick())
                    continue
                self._flare_absent_warned_at = 0.0  # 恢复可达 → 重置降频计时

                # 每轮刷新前回收 FlareSolverr 会话（destroy→create）：换一个全新
                # 标签页，防 Chromium 长跑崩掉后整轮连败（2026-09-25 事故：
                # 02:18 的标签页 08:18 崩掉，随后每小时拿坏会话重试全败）。
                # best-effort：回收失败不阻断刷新本身。
                try:
                    if await self.client.recycle_flare_session():
                        logger.info("[sdjk] FlareSolverr 会话已回收（换用新标签页）")
                except Exception:  # noqa: BLE001 - 回收失败照常刷新
                    pass
                # ③ FS 可达：正常刷新；这里失败都是**真故障**，WARN 保留
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
                        logger.warning(
                            "[sdjk] 言录使货单已过期且 wiki 当期上报尚未更新，卡面暂以候选池展示"
                        )
                    elif acr_status == "failed":
                        logger.warning(
                            "[sdjk] 言录使本周货单自动抓取失败："
                            "需人工更新 core/data/de/acrichis_week.json"
                            "（卡面已标注「货单待更新」）"
                        )
                except Exception:  # noqa: BLE001 - 自检失败不阻断
                    pass
                await asyncio.sleep(self._secs_to_aligned_tick())
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.warning("[sdjk] 元素加成快照刷新失败（1 小时后重试）：%s", e)
                await asyncio.sleep(3600)

    async def terminate(self):
        for name in ("_auto_task", "_valence_task", "_community_task"):
            task = getattr(self, name, None)
            if task:
                task.cancel()
        try:
            # ★ 2026-09-26：确认 await 生效并留痕（旧实现在这里静默 —— 一旦 stop 没生效，
            #   旧推送守护会与新实例的并存 → 同一事件推两次，且日志里查不到线索）。
            await self.push.stop()
            logger.info("[sdjk] 推送守护已确认停止（注销登记后活跃数应为 0）")
        except Exception as exc:  # noqa: BLE001 - 停止失败也要留痕，别静默
            logger.warning("[sdjk] 推送守护停止异常：%s", exc)
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
    async def _build_results(self, event: AstrMessageEvent, reply: Reply, parsed: Parsed):
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
                        self.renderer.render, f"{ptitle}（第{i}/{n}页）", plines, reply.footer
                    )
                if path:
                    comps.append(Image.fromFileSystem(path))
                    self._schedule_card_cleanup(path)
            if comps:
                if tail_txt:
                    comps.append(Plain(tail_txt))
                yield event.chain_result(comps)
                return
            logger.warning(
                "[sdjk] 多页卡片渲染为空，已降级文字输出：%s",
                reply.pages[0][0] if reply.pages else "?",
            )

        if use_image:
            if WarframeSDJK._render_lock is None:
                WarframeSDJK._render_lock = asyncio.Lock()
            async with WarframeSDJK._render_lock:
                path = await asyncio.to_thread(
                    self.renderer.render, reply.title, reply.lines, reply.footer
                )
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
            card = "\n\n".join(text_card(pt, pl, reply.footer) for pt, pl in reply.pages)
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
                return Reply(
                    raw_text=f"✅ 本群默认平台已设为 {PLATFORM_DISPLAY[plat]}"
                    f"（{plat}），可用 -pc/-ps 等临时覆盖"
                )
            cur = self.groups.get(umo)
            return Reply(
                raw_text=f"当前默认平台：{PLATFORM_DISPLAY.get(cur['platform'], cur['platform'])}"
            )

        if cmd in ("开启", "关闭"):
            if not self._is_admin(event):
                return Reply(raw_text="⛔ 该指令需要群管理员权限")
            if not args or args[0] not in ("推送", "聊天"):
                return Reply(raw_text="用法：.开启 推送 / .关闭 推送")
            value = cmd == "开启"
            await self.groups.set_switch(umo, args[0], value)
            state = "已开启" if value else "已关闭"
            return Reply(
                raw_text=f"✅ {args[0]}功能{state}"
                + ("，可以用「蹲 类型」订阅推送了" if value and args[0] == "推送" else "")
            )

        if cmd == "状态":
            # ★ 安全审查（2026-09-18）：这条会列出**本群全部订阅明细**
            #   （谁订了什么、还剩多久）与缓存统计，却完全没有权限校验 ——
            #   群里任何人都能看。改为与其它管理指令一致。
            if not self._is_admin(event):
                return Reply(raw_text="⛔ 该指令需要群管理员权限（会列出本群订阅明细）")
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
                        life = (
                            f"剩{left // 86400:.0f}天{left % 86400 // 3600:.0f}小时"
                            if left >= 3600
                            else f"剩{max(left, 0) // 60:.0f}分钟"
                        )
                    mode = "一次" if s.once else "持续"
                    lines.append(
                        f"· {i}. {s.event}" + (f"：{s.rule}" if s.rule else "") + f"　{mode}·{life}"
                    )
            lines.append("◆ 系统")
            lines.append(f"· 缓存　{cache['size']} 项 · 命中率 {cache['hit_rate'] * 100:.0f}%")
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
                raw_text="「.锚点」已废弃：仲裁表现在按 arbi.wf.wiki 的"
                "确定性排期自动推算，不需要手动校准。\n"
                "· 看当前与下一小时场次：发「仲裁」\n"
                "· 看整周 30 天排期：发「仲裁表」（可筛类型）"
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

        ★ A1（2026-10-03 用户批准）：仅 aiocqhttp（QQ）平台拼 At 组件；
        其余平台/解析不到一律回落纯文本（不得因 @ 不支持而丢推送）；
        不做 @全体（at_targets 已剔除 `all`）。
        """
        chain = MessageChain().message(text)
        ids = at_targets(self._platform_name_of(umo), at)
        if ids:
            try:
                from astrbot.api.message_components import At

                chain = MessageChain()
                for q in ids:
                    chain.chain.append(At(qq=q))
                chain.message(text)
            except Exception:  # noqa: BLE001 - At 不可用 ⇒ 纯文本，不丢推送
                chain = MessageChain().message(text)
        await self.context.send_message(umo, chain)

    def _platform_name_of(self, umo: str) -> str:
        """umo 首段 = 平台实例 id（如「客服小祥」）⇒ 经 Context 解析适配器
        名（如 aiocqhttp）。解析不到返回空串（@ 回落纯文本）。"""
        try:
            pid = (umo or "").split(":", 1)[0]
            inst = self.context.get_platform_inst(pid) if pid else None
            return (inst.meta().name or "") if inst else ""
        except Exception:  # noqa: BLE001
            return ""
