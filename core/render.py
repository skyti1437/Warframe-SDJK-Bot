# -*- coding: utf-8 -*-
"""渲染引擎：文本卡片（-w）与图片卡片（-t / auto）。

- 文本模式：全角感知（CJK 宽度按 2 列计算）的字符表格，游戏内可直接复制；
- 图片模式：Warframe Orokin UI 风格的 Pillow 信息卡片（暗金切角面板、
  语义着色、徽章芯片、2x 超采样）；缺字体/Pillow 时由调用方降级文本。
"""
from __future__ import annotations

import random
import re
import unicodedata
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# 文本卡片
# ---------------------------------------------------------------------------

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def display_width(text: str) -> int:
    """按终端显示宽度估算：全角/宽字符记 2 列。"""
    text = _ANSI_RE.sub("", text)
    w = 0
    for ch in text:
        if unicodedata.combining(ch):
            continue
        w += 2 if unicodedata.east_asian_width(ch) in ("F", "W") else 1
    return w


def pad(text: str, width: int) -> str:
    """右侧补空格至指定显示宽度。"""
    gap = width - display_width(text)
    return text + " " * max(0, gap)


def text_card(title: str, lines: list[str], footer: str = "",
              max_width: int = 44) -> str:
    """构造排版整齐的字符界面卡片。"""
    inner = [title] + list(lines)
    if footer:
        inner.append(footer)
    width = min(max_width, max((display_width(x) for x in inner), default=20))
    width = max(width, 20)
    bar = "═" * (width + 2)
    sep = "─" * (width + 2)
    out = [f"╔{bar}╗"]
    for i, line in enumerate(inner):
        for j, seg in enumerate(_wrap_cjk(line, width)):
            mark = "" if (i == 0 or (footer and i == len(inner) - 1)) else " "
            prefix = "" if j else mark
            if i == 0 or (footer and i == len(inner) - 1):
                out.append(f"║ {pad(seg, width)} ║")
            else:
                out.append(f"│ {pad(prefix + seg, width)} │")
        if i == 0 and len(inner) > 1:
            out.append(f"╟{sep}╢")
        if footer and i == len(inner) - 2:
            out.append(f"╟{sep}╢")
    out.append(f"╚{bar}╝")
    return "\n".join(out)


def _wrap_cjk(line: str, width: int) -> list[str]:
    """逐字符换行（全角按 2 列），超长行折行显示。"""
    if display_width(line) <= width:
        return [line]
    out: list[str] = []
    cur = ""
    cur_w = 0
    for ch in line:
        cw = 2 if unicodedata.east_asian_width(ch) in ("F", "W") else 1
        if cur_w + cw > width:
            out.append(cur)
            cur, cur_w = ch, cw
        else:
            cur += ch
            cur_w += cw
    if cur:
        out.append(cur)
    return out


# ---------------------------------------------------------------------------
# 图片卡片：Warframe Orokin UI 风格
# ---------------------------------------------------------------------------

try:
    from PIL import Image, ImageDraw, ImageFilter, ImageFont
except ImportError:  # pragma: no cover
    Image = ImageDraw = ImageFilter = ImageFont = None  # type: ignore[assignment]

import logging

_LOGGER = logging.getLogger("astrbot")

FONT_DIR = Path(__file__).resolve().parent / "data" / "fonts"  # core/data/fonts

# 超采样倍率（抗锯齿）。3 倍画布 3360 宽再缩到 1120，像素操作量是 2 倍方案的
# 2.25 倍 —— 测试宿主只有 **2 核**，且常与其它插件（分词 / LLM 调用）抢 CPU，
# 实测单张渲染会被拖到 30 s+（2026-09-17）。降到 2 倍，
# 像素量降 56%，仍是「先放大再缩」的抗锯齿路径（振铃远比 1 倍方案轻）。
SS = 2

# 单卡正文最大行数。指令一览这类清单会超过 40 行，上限过低会静默截断尾部内容。
MAX_BODY_LINES = 90

# 每一级缩进的宽度（设计像素，绘制时乘 SS）。行首「　　」= 正文级（不缩进），
# 「　　　　」= 再低一级（赏金卡的副目标 / 附加条件）。
_INDENT_PX = 34

# 卡片水印（小更新 +0.1，首个满意版本升 1.0）。
# 从 core.__version__ 取主次版本，避免与 metadata.yaml / main.py 注册版本走散
# （曾经三处各写一份、抬版本时漏改，水印留在旧号上）。
try:  # pragma: no cover - 兜底分支只在包结构异常时走到
    from . import __version__ as _CORE_VERSION
    WATERMARK_VERSION = ".".join(str(_CORE_VERSION).split(".")[:2]) or "1.0"
except Exception:  # noqa: BLE001
    WATERMARK_VERSION = "1.0"
WATERMARK = f"WARFRAME  ·  SDJK {WATERMARK_VERSION}"

# 配色：Orokin 暗金 + Tenno 能量色
BG_TOP = (10, 13, 20)
BG_MID = (17, 23, 36)
BG_BOT = (11, 15, 23)
GOLD = (212, 176, 106)
GOLD_BRIGHT = (240, 215, 154)
GOLD_DIM = (122, 100, 62)
GOLD_FAINT = (96, 82, 54)
INK = (238, 232, 218)        # 主文字：暖白
INK_DIM = (154, 163, 178)    # 次文字
INK_FAINT = (112, 120, 134)  # 注释
AMBER = (232, 178, 106)
CYAN = (111, 211, 232)
VIOLET = (180, 143, 232)
RED = (216, 100, 88)
GREEN = (108, 200, 138)
BLUE = (111, 168, 220)
MAGENTA = (200, 111, 168)
TEAL = (88, 184, 168)
STEEL = (159, 195, 232)
BLUEPRINT = (245, 178, 92)   # 战甲/武器部件、蓝图：橙金（与 MOD 的亮金区分）

# 切角面板底色。面板 fill 写的是 (14,18,28,214)，但 alpha 会被丢弃，
# 实际呈现就是这个色，故作为「面板内元素」的半透明混色基准。
PANEL_BG = (14, 18, 28)


def _tint(color: tuple[int, int, int], alpha: int,
          bg: tuple[int, int, int] = PANEL_BG
          ) -> tuple[int, int, int, int]:
    """把带透明度的颜色预混到背景上，返回**不透明**色。

    为什么需要它：ImageDraw 在 RGBA 画布上**不做 alpha 混合**（直接把像素写成
    给定值），收尾的 `.convert("RGB")` 又会**丢弃 alpha**。所以
    `fill=(255,255,255,12)` 最终渲染出来是一整块**纯白**，而不是淡白 ——
    面积大的填充（徽章底、面板）尤其明显，细线则因缩小采样被稀释反而正常。

    想要半透明观感，只能先自己按 alpha 混好颜色再画。

    Args:
        color: 前景 RGB。
        alpha: 等效不透明度（0-255）。
        bg: 背景 RGB，默认面板底色。

    Returns:
        可直接交给 ImageDraw 的不透明 RGBA 四元组。
    """
    a = max(0, min(255, int(alpha))) / 255.0
    mixed = tuple(round(bg[i] + (color[i] - bg[i]) * a) for i in range(3))
    return (mixed[0], mixed[1], mixed[2], 255)

# 裂隙纪元 -> 芯片配色。**中英两套键都要有**：handler 输出的是中文（parser.TIER_CN），
# 但历史数据/兜底路径可能仍是英文原名。
# ⚠️ 遗漏某个键不会报错，只会让该纪元**静默退化成朴素文字**（无彩色芯片）——
#    旧实现漏了 Omnia 的中文「全能」（只写了英文键与旧译名「万灵」），
#    于是扎里曼/联结生存的裂隙里全是没颜色的 `[全能]`，一眼就能看出不齐。
TIER_COLOR = {"古纪": GOLD, "Lith": GOLD, "前纪": STEEL, "Meso": STEEL,
              "中纪": GREEN, "Neo": GREEN, "后纪": (232, 154, 138),
              "Axi": (232, 154, 138), "安魂": VIOLET, "Requiem": VIOLET,
              "万灵": GOLD_BRIGHT, "Omnia": GOLD_BRIGHT,
              "全能": GOLD_BRIGHT, "Vanguard": TEAL, "先锋": TEAL}

# 物品大类芯片色（奸商预测卡用）：把 `[MOD]` `[装饰]` 这类小标签染成一色一类，
# 一眼能看出这行是 MOD 还是外观 —— 用户 2026-09-18 要求「注意颜色运用」。
# 配色沿用已有调色板，不引入新色系。
GROUP_CHIP_COLOR = {
    "MOD": VIOLET, "主要 MOD": VIOLET,
    "武器": CYAN, "遗物": GOLD,
    "装饰": AMBER, "外观": MAGENTA,
    "其他": INK_DIM,
    "消耗品": GREEN, "礼包": TEAL,
}

# ---------------------------------------------------------------------------
# 行内语义配色：把「任务类型 / 挑战名 / 派系 / 元素 / 加成%」和正文分开
#
# 用户反馈「任务类型像和后面文字是一块的」「元素和元素加成分别来个其他颜色」——
# 靠的是把这几类词从正文里**切成 token**，再按规则上色（见 _TOKEN_RE / rules）。
#
# ⚠️ 中文没有词边界，所以任务类型 / 元素 / 派系一律用
#    `(?:^|(?<=[\s　｜（]))…(?=[\s　｜）]|$)` 这种**前后边界**包住：
#    不包的话「电磁力场装置」里的「磁力」、「致命冲击」里的「冲击」都会被染色，
#    看起来像卡出错。★/▣ 开头的 token 因为先被匹配走，不受影响。
# ---------------------------------------------------------------------------
MISSION_TYPE_COLOR = CYAN          # 任务类型（歼灭/生存/虚空决战…）
CHALLENGE_COLOR = VIOLET           # 赏金挑战名（能量超载/终结好戏…）
FACTION_COLOR = (240, 170, 110)    # 派系（Grineer / 科腐者 / 合一众…）与 I系/C系 同色
ELEMENT_COLOR = TEAL               # 伤害/元素类型（磁力/冰冻/毒素…）
BONUS_COLOR = GOLD_BRIGHT          # 加成百分比（25.7%）

# 任务类型全集 = DE 官方 missionName 中文（ExportRegions / MissionName_*）
#                  ∪ ExportBounties 末阶段映射出来的那几个。
# 长的必须排在前面：「资源回收」要优先于「回收」，「移动防御」优先于「防御」。
MISSION_TYPE_WORDS: tuple[str, ...] = (
    "INFESTED 资源回收", "资源回收", "物资回收", "移动防御", "镜像防御",
    "元素转换", "虚空洪流", "虚空覆涌", "虚空决战", "虚空天使", "传承种收割",
    "联结生存", "圣殿突袭", "无尽回廊", "黑暗地带战争", "多方交战", "自由漫游",
    "沉沦之地", "星际航道结合点", "祈运坛防御", "Follie 的狩猎", "佩里塔叛乱",
    "前哨战 + 刺杀", "武形秘仪", "对战", "前哨战",
    "歼灭", "刺杀", "捕获", "拦截", "挖掘", "防御", "破坏", "救援",
    "间谍", "生存", "劫持", "回收", "净化", "伏击",
    "中断", "扬升", "强袭", "清巢", "叛逃", "追击", "突袭",
    "爆发", "奥影", "衰退室", "竞技场",
)
ELEMENT_WORDS: tuple[str, ...] = (
    "磁力", "辐射", "腐蚀", "毒气", "病毒", "爆炸",
    "冲击", "穿刺", "切割", "火焰", "冰冻", "电击", "毒素",
)
FACTION_WORDS: tuple[str, ...] = (
    "Grineer", "Corpus", "Infested", "Sentient", "Corrupted",
    "奥罗金", "低语者", "低语", "合一众", "炽蛇军", "科腐者",
)
_W_L = r"(?:^|(?<=[\s　｜（]))"      # 词左边界
_W_R = r"(?=[\s　｜）]|$)"           # 词右边界


def _word_alt(words: tuple[str, ...]) -> str:
    """把词表编成「带左右边界的 alternation」，直接塞进 _TOKEN_RE。"""
    return _W_L + "(?:" + "|".join(re.escape(w) for w in words) + ")" + _W_R


_MISSION_TYPE_SET = frozenset(MISSION_TYPE_WORDS)
_ELEMENT_SET = frozenset(ELEMENT_WORDS)
_FACTION_SET = frozenset(FACTION_WORDS)
_PCT_RE = re.compile(r"\d+(?:\.\d+)?%\Z")
# 行首的任务类型（长的优先，`re` 的 alternation 是「先匹配先赢」，词表已排好序）
_LEAD_TYPE_RE = re.compile(
    "^(?:" + "|".join(re.escape(w) for w in MISSION_TYPE_WORDS) + ")")


def _split_leading_type(text: str) -> tuple[str, str]:
    """切出行首的任务类型（含其后紧跟的「：」），返回 (类型, 余下文本)。

    官方赏金名自带类型词的档位（刺杀指挥官 / 物资回收 / 破坏 Grineer 的补给线）
    在 formatter 里**不加类型前缀**（加了会重复），于是这一档表面上没有独立的
    类型段；这里把开头的类型词本身染成类型色，保证每一档都有类型色。
    「刺杀：H-09 坦克」这种节点名顺带把「：」一起吃掉，剩下的就是纯节点名。
    """
    m = _LEAD_TYPE_RE.match(text or "")
    if not m:
        return "", text
    word = m.group(0)
    rest = text[len(word):]
    if rest.startswith("："):
        word += "："
        rest = rest[1:]
    return word, rest.lstrip(" ")

# 标题关键词 -> (主题色, 英文副标)
TITLE_THEME: list[tuple[tuple[str, ...], tuple[tuple[int, int, int], str]]] = [
    (("裂隙",), (CYAN, "VOID FISSURES")),
    (("平原", "夜灵", "周期"), (AMBER, "OPEN WORLD CYCLES")),
    (("时效",), (CYAN, "TIMERS OVERVIEW")),
    (("突击",), ((224, 130, 74), "SORTIE")),
    (("执刑官",), (RED, "ARCHON HUNT")),
    (("商人", "奸商"), (GOLD, "VOID TRADER")),
    (("特惠",), (TEAL, "DAILY DEALS")),
    (("日历",), (GREEN, "1999 CALENDAR")),
    (("深层",), (STEEL, "DEEP ARCHIMEDEA")),
    (("时光",), (STEEL, "TEMPORAL ARCHIMEDEA")),
    (("钢铁之路",), (RED, "STEEL PATH")),
    (("仲裁",), (CYAN, "ARBITRATION")),
    (("警报",), (RED, "ALERTS")),
    (("入侵",), ((224, 130, 74), "INVASIONS")),
    (("新闻",), (BLUE, "NEWS FEED")),
    (("电波",), (MAGENTA, "NIGHTWAVE")),
    (("赤毒",), (RED, "KUVA SIPHONS")),
    (("赏金",), (AMBER, "SYNDICATE BOUNTIES")),
    (("结合",), (CYAN, "SIMARIS TARGETS")),
    (("舰队", "建造"), (GOLD, "CONSTRUCTION")),
    (("紫卡",), (GOLD, "RIVEN AUCTIONS")),
    (("在售", "收购", "合购"), (CYAN, "WARFRAME.MARKET")),
    (("垃圾", "杜卡德"), (GOLD, "DUCAT DIRECTORY")),
    (("物品", "搜索"), (TEAL, "ITEM SEARCH")),
    (("安魂", "升级", "融合", "赋能"), (VIOLET, "CALCULATOR")),
    (("对话",), (MAGENTA, "KIM CONSOLE")),
    (("指令", "帮助"), (GOLD, "COMMAND CODEX")),
    (("仲裁",), (CYAN, "ARBITRATION SCHEDULE")),
    (("蹲", "可蹲"), (MAGENTA, "SUBSCRIBE CODEX")),
]

_PAGE_RE = re.compile(r"（第(\d+)/(\d+)页(?:[，,]\s*共(\d+)[^\d）]*)?）")
# 「（第N/M页，共K<量词>）」的量词各 handler 不统一：条 / 场 / 个 / 件 / 项。
# ⚠️ 不能只写「条」——遗物列表用的是「个」，漏匹配会让整段页码留在标题里，
#    标题变长后压到右上角平台徽章上并被面板边框裁切。
_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FBFF\u2600-\u2604\u2606-\u2609\u260a-\u26FF"
    "\u2700-\u27BF\u2B00-\u2BFF\u2300-\u23FF\uFE0F\u2049\u203C\u2B50]")
# 时长片段：必须「数字 + 单位」成对出现。
# ⚠️ 绝不能写成 [0-9天小时分钟秒]+ —— 单位字符集里的「时」会让散文中的
#    「剩余时间」被当成计时器匹配成「剩余时」，被抽走并右对齐成行尾琥珀字，
#    原句还会被挖掉三个字变成「商城折扣商品与间」。
_DUR = r"\d+\s*(?:天|小时|分钟|分|秒)(?:\s*\d+\s*(?:天|小时|分钟|分|秒))*"
# 计时标记必须显式出现：只有带「剩余/剩/不到」的才是计时文本。
# ⚠️ 不要写成 `剩余?` —— `?` 只作用于「余」，等价于「必须有『剩』」，
#    会漏掉「不到1分钟」；而若干脆放开标记，又会把「（第190天）」当成计时。
# 末尾的 `(?:不到)?` 是为了吃掉「剩余 不到1分钟」里的第二个标记词，
# 否则「剩余」会残留在正文里（countdown() 剩余不足 1 分钟时就是这个文案）。
_TIMER_MARK = r"(?:剩余|剩|不到)\s*(?:不到)?\s?"
_TOKEN_RE = re.compile(
    r"(\[[^\]]*\]|★+[^、\s　]*|▣[^、\s　]+|\d+p(?![a-zA-Z])|" + _TIMER_MARK + _DUR +
    r"|（[0-9hms ]+）|已结束"
    r"|▲[^、\s　]+|▼[^、\s　]+|信\d+"
    r"|网页在线|在线|离线|钢铁|九重天|执刑官|满级|零级"
    r"|\d+日\d+时"
    r"|(?:I系|C系|G系|O系)"
    # 语义配色：任务类型 / 元素 / 派系 / 加成百分比（见上面的说明）
    # ⚠️ 必须排在旧分组**前面**（尤其派系要压过原来的「低语」），
    #    alternation 是「先匹配先赢」，排在后面就会被短词抢先。
    r"|" + _word_alt(FACTION_WORDS) +
    r"|" + _word_alt(MISSION_TYPE_WORDS) +
    r"|" + _word_alt(ELEMENT_WORDS) +
    r"|\d+(?:\.\d+)?%"
    # 奸商卡：杜卡德报价（金色，全称与「N杜」缩写两种写法都要认）、
    # 现金（青色，含「N万现金」折叠式）。行首序号用暗金弱化。
    # ⚠️ 序号这里用 `(?:^|(?<=[\s　]))` 而不是 `(?<=^|\s|　)` ——
    #    后者是**变长 lookbehind**，Python `re` 直接报错。
    r"|\d+\s*杜卡德"
    r"|\d+杜(?!卡)"
    r"|\d+(?:\.\d+)?万?现金"
    r"|(?:^|(?<=[\s　]))\d+\.(?=[\s　])"
    r"|[0-9]{2,4}\s\[(?:S|A\+|A-|A|B\+|B|F)\]"
    r"|[0-9]{2,4}\s(?:S|A\+|A-|A|B\+|B|F)\b"
    r"|(?:^|(?<=[\s　]))(?:S|A\+|A-|A|B\+|B|C|未评级)(?=[\s　]|$))")
# 行尾独立计时（右对齐时间列用），如 “剩余 58分钟（58m 36s）”
_TIMER_RE = re.compile(_TIMER_MARK + _DUR + r"(?:（[0-9hms ]+）)?")
# 「N分钟 后」型（时效总览行尾）
_TIMER_ALT_RE = re.compile(_DUR + r"\s*后(?:开始|抵达)?\s*$")
# 赏金行内的等级（“｜5-15级｜” / 行尾“｜5-15级”）—— 提取后右对齐成等级列，便于整列扫读。
# ⚠️ 行尾必须也认：赏金行只有「任务名｜等级｜标签」三段的才有尾随「｜」，
#    而「任务名｜等级」这种两段行（无钢铁之路/合一众标签）旧正则匹配不到，
#    于是同一张卡上前面几行等级内联、后面几行等级右对齐，看起来像排版坏了。
_LV_RE = re.compile(r"｜(\d+-\d+级)(?:｜|$)")

# 「· 武器名　元素 25.7%」—— 第二格是「元素 + 百分比」时，整列按**实测像素宽**对齐
# （只有渲染层知道真实字形宽度；formatter 端按 CJK=2 / ASCII=1 手算会差出一个字，
#  用户反馈「没对齐真的好丑」就是这么来的）。
_PAIR_RE = re.compile(r"^[^\s　]+ [\d.]+%\Z")



def _strip_emoji(text: str) -> tuple[str, bool]:
    """去掉彩色 emoji（CJK 字体缺字形），返回 (净化文本, 行首是否曾有 emoji)。"""
    had_head = bool(text) and bool(_EMOJI_RE.match(text))
    clean = _EMOJI_RE.sub("", text).strip()
    return clean, had_head


class _Fonts:
    """字体加载：打包 Noto CJK 优先，随后系统常见 CJK 字体。"""

    CANDIDATES = [
        FONT_DIR / "NotoSansCJK-Regular.ttc",
        FONT_DIR / "NotoSansCJK-Bold.ttc",
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"),
        Path("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc"),
        Path("/usr/local/lib/python3.12/site-packages/pillowmd/data/fonts/yahei.ttf"),
        Path("C:/Windows/Fonts/msyh.ttc"),
        Path("/System/Library/Fonts/PingFang.ttc"),
    ]

    def __init__(self):
        self.regular: Optional[Path] = None
        self.bold: Optional[Path] = None
        self._sc_index = 0
        self._cache: dict[tuple[str, int], "ImageFont.FreeTypeFont"] = {}
        for path in self.CANDIDATES:
            if not path.exists():
                continue
            if "Bold" in path.name or "bold" in path.name:
                if self.bold is None and self._probe(path):
                    self.bold = path
            elif self.regular is None and self._probe(path):
                self.regular = path
        if self.regular is None:
            self.regular = self.bold
        if self.bold is None:
            self.bold = self.regular

    def _probe(self, path: Path) -> bool:
        """选到含简体中文的字体面；ttc 多面集合时优先非 Mono 的 SC 面。"""
        try:
            best = None
            for idx in range(12):
                try:
                    f = ImageFont.truetype(str(path), 24, index=idx)
                except Exception:  # noqa: BLE001
                    break
                name = f.getname()[0]
                if name in ("Noto Sans CJK SC",) or "YaHei" in name \
                        or "PingFang" in name or "WenQuanYi" in name:
                    self._sc_index = idx
                    return True
                if "CJK SC" in name and best is None:  # Mono 兜底
                    best = idx
            if best is not None:
                self._sc_index = best
                return True
            return False
        except Exception:  # noqa: BLE001
            return False

    def get(self, which: str, size: int):
        path = self.regular if which == "regular" else self.bold
        if path is None:
            return None
        key = (str(path), size)
        if key not in self._cache:
            try:
                self._cache[key] = ImageFont.truetype(
                    str(path), size * SS, index=self._sc_index)
            except Exception:  # noqa: BLE001
                return None
        return self._cache[key]


def _chamfer(draw: "ImageDraw.ImageDraw", box, c: int,
             fill=None, outline=None, width: int = 1):
    """切角八边形：Warframe UI 面板的基础形。"""
    x0, y0, x1, y1 = box
    c = max(1, min(c, (x1 - x0) // 2, (y1 - y0) // 2))
    pts = [(x0 + c, y0), (x1 - c, y0), (x1, y0 + c), (x1, y1 - c),
           (x1 - c, y1), (x0 + c, y1), (x0, y1 - c), (x0, y0 + c)]
    draw.polygon(pts, fill=fill, outline=outline, width=width)


def _split_cells(text: str) -> Optional[list[str]]:
    """把「· 指令　说明」这类行按全角空格切成两列；切不开返回 None。

    行首的「·」由渲染层统一画成圆点，这里必须先剥掉：
    否则「画出来的圆点 + 字面 ·」会让每行开头显示成**两个点**。

    Args:
        text: 单行正文（可能带行首「·」）。

    Returns:
        列文本列表，或 None（该行没有全角空格，不参与分列）。
    """
    if "　" not in text:
        return None
    parts = text.split("　")
    if parts and parts[0].lstrip().startswith("·"):
        parts[0] = parts[0].lstrip()[1:].strip()
    return parts


def _plan_desc_col_wrap(cells, col0: float, wrap_fn, measure_fn,
                        w_cap: int = 1500, reserve: int = 178,
                        gap: int = 24) -> tuple[dict, float]:
    """语法文档卡（指令一览）说明列的「列内折行」规划。

    双列卡片说明列过宽（cmd + gap + desc 超出 ``w_cap - reserve`` 的正文
    预算）时，把说明按剩余预算折成多段；折行规划交给调用方在行构造时
    展开成续行（首行 cells=[cmd, 段0]，续行 cells=["", 段k]）。

    Args:
        cells: ``[(行号, 指令列, 说明列)]``，仅双列行。
        col0: 指令列实测最大像素宽（CSS px）。
        wrap_fn: ``wrap_fn(desc, budget) -> [段]``。
        measure_fn: ``measure_fn(text) -> 像素宽``（CSS px）。
        w_cap: 卡宽上限；reserve: 正文左右留换折行预留；gap: 列间隙。

    Returns:
        ``(wrap_map, new_col1)``：wrap_map 为 ``{行号: [段]}``（只含需要
        折行的行）；new_col1 为折行后说明列的新最大宽。
    """
    col1 = max((measure_fn(d) for _, _, d in cells), default=0.0)
    if not cells or col0 + gap + col1 <= w_cap - reserve:
        return {}, col1
    budget = int(w_cap - reserve - col0 - gap)
    if budget < max(8.0, w_cap * 0.15):   # 指令列本身离谱宽时别硬折，交安全阀
        return {}, col1
    wrap_map: dict = {}
    widest = 0.0
    for idx, _cmd, desc in cells:
        segs = wrap_fn(desc, budget)
        if len(segs) > 1:
            wrap_map[idx] = segs
        widest = max(widest, max(measure_fn(s) for s in segs))
    return wrap_map, widest


def _is_label_row(head: str, has_sep: bool, has_cells: bool) -> bool:
    """是否走「标签：值」的加粗标签排版（否则走普通/双列排版）。

    `has_cells=True` 表示该行属于表格式卡片（指令一览 / 仲裁时间表）的分列行，
    此时**必须**一律走列对齐：否则「头部 ≤10 字 + ：」的行会被标签规则截走，
    渲染成加粗顶格，与其余行的列对齐样式对不上（外观上就是“这行怎么不一样”）。

    Args:
        head: 第一个全角冒号之前的部分。
        has_sep: 该行是否存在全角冒号。
        has_cells: 该行是否已切好双列单元格。

    Returns:
        是否按「标签：值」排版。
    """
    if has_cells:
        return False
    return has_sep and 1 <= len(head) <= 10


def _vgrad(size, stops):
    """垂直多段渐变背景。"""
    w, h = size
    col = Image.new("RGB", (1, h))
    px = col.load()
    for y in range(h):
        t = y / max(1, h - 1)
        for i in range(len(stops) - 1):
            t0, c0 = stops[i]
            t1, c1 = stops[i + 1]
            if t0 <= t <= t1:
                k = (t - t0) / max(1e-6, t1 - t0)
                px[0, y] = tuple(int(a + (b - a) * k) for a, b in zip(c0, c1))
                break
        else:
            px[0, y] = stops[-1][1]
    return col.resize((w, h))


def _hgrad_line(draw, x0: int, x1: int, y: int, color, peak_alpha: int = 220):
    """两端渐隐的水平金线。"""
    mid = (x0 + x1) / 2
    span = max(1.0, (x1 - x0) / 2)
    for x in range(int(x0), int(x1), SS):
        k = 1 - abs(x - mid) / span
        a = int(peak_alpha * (k ** 1.5))
        if a > 2:
            draw.line([(x, y), (min(x + SS, x1), y)], fill=color + (a,),
                      width=SS)


class ImageRenderer:
    """Orokin 风格 Pillow 渲染器；不可用时 render() 返回 None 降级文本。"""

    def __init__(self, cache_dir: Path, font_path: Optional[str] = None):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.fonts = _Fonts() if Image is not None else None

    @property
    def available(self) -> bool:
        return (Image is not None and self.fonts is not None
                and self.fonts.regular is not None)

    def render(self, title: str, lines: list[str], footer: str = "") -> Optional[str]:
        if not self.available:
            return None
        import time as _t
        _t0 = _t.perf_counter()
        try:
            path = self._render(title, lines, footer)
            # 耗时埋点（定位「出图慢」用；>1200ms 记 WARNING 级别提醒）
            _ms = (_t.perf_counter() - _t0) * 1000
            _LOGGER.log(logging.WARNING if _ms > 1200 else logging.INFO,
                        "[warframe] 卡片渲染 %.0f ms（%d 行）：%s",
                        _ms, len(lines), title)
            return path
        except Exception:  # noqa: BLE001 —— 降级文本但必须留下失败现场
            _LOGGER.exception("[warframe] 图片渲染失败，已降级文本：%s", title)
            return None

    # ------------------------------------------------------------------
    def _render(self, title: str, lines: list[str], footer: str) -> Optional[str]:
        f = self.fonts
        W = 1120
        body_font = f.get("regular", 30)
        title_font = f.get("bold", 46)
        sub_font = f.get("regular", 19)
        small_font = f.get("bold", 27)
        note_font = f.get("regular", 25)
        if not all((body_font, title_font, sub_font, small_font, note_font)):
            return None

        # —— 页码芯片 & 主题 ——
        m = _PAGE_RE.search(title)
        page_chip = f"{m.group(1)}/{m.group(2)}" if m else ""
        title = _PAGE_RE.sub("", title).strip()
        accent, subtitle = GOLD, "WARFRAME SDJK"
        for keys, (color, _slug) in TITLE_THEME:
            if any(k in title for k in keys):
                accent = color
                break
        self._plain_body = ("指令一览" in title or "可蹲" in title
                            or title.startswith("蹲")
                            or title.startswith("帮助"))  # 语法文档不做行内装饰
        # 按全角空格切列并对齐绘制的卡片：仲裁时间表 / 指令一览
        self._table_mode = ("仲裁时间表" in title) or ("指令一览" in title)
        # 赏金卡：地区行做横幅、等级右对齐成列、同组行之间不画分隔线
        self._bounty_mode = "赏金任务" in title
        # 侵袭卡的 4 列（类型/派系/等级/地点）走既有列对齐机制；
        # 价格榜 / 紫卡热度榜的多列数字同样按全角空格切列对齐
        # 遗物三张列表卡（入库/出库/列表）同样是网格排版：不开这个模式的话
        # 单元格只会顺序拼接，「Lith A1/Lith A10/…」长短不一会导致列位抖动
        # （用户 2026-09-17：「排版有点干燥，也没对齐」）。
        # 部件反查卡（2026-09-18）是「遗物 / 槽位 / 状态」三列，同理。
        self._table_mode = (("仲裁时间表" in title) or ("指令一览" in title)
                            or ("侵袭" in title) or ("价格排行" in title)
                            or ("紫卡热度" in title)
                            or ("遗物入库" in title) or ("遗物出库" in title)
                            or ("遗物列表" in title) or ("部件出处" in title)
                            # 奸商当期货单：两列 ×（名称/杜卡德/现金）六列网格
                            or ("虚空商人" in title))
        plat_chip, foot_notes = self._parse_footer(footer)

        # —— 宽度优先：逐行实测需求（文本+芯片外扩+时间列），卡宽上限 1500 ——
        pad = 46
        measure = ImageDraw.Draw(Image.new("RGB", (8, 8)))
        timer_font = f.get("bold", 27)
        needed = 0.0
        col_max: list[float] = []     # 表格式卡片：**逐列**的最大像素宽
        # 「· 武器名　元素 25.7%」这类两格行：记下来，稍后拉一条公共列把第二格对齐。
        # **必须在这里算**：卡宽 W 是用 needed 定的，对齐会让最宽行变宽，
        # 只在排版后补算就来不及调卡宽了（会压出面板边框）。
        pair_cells: list[list[str]] = []
        for raw in (lines or [""])[:MAX_BODY_LINES]:
            clean, _ = _strip_emoji(raw)
            if "　" in clean:
                _cells = _split_cells(clean)
                if _cells and len(_cells) == 2 and _PAIR_RE.match(_cells[1]):
                    pair_cells.append(_cells)
            # PIL 的 textlength 拒绝测多行串；而 handler 拼出来的行可能自带 \n
            # （例如 wiki 的「标题\n链接」）。这里取最长的一行作为该行的宽度需求。
            # ★ 注脚（※）行画的是 note_font（25px），量宽度也必须用它：
            #   按 body_font（30px）量会虚高 20%，把卡宽硬生生撑大 ——
            #   部件反查卡实测行内容只到 810px、卡宽却被注脚撑到 1416px，
            #   右半边空着（与折行字号那处是同一个 bug 的两半）。
            _lf = note_font if clean.startswith("※") else body_font
            w_line = (max((measure.textlength(s, font=_lf)
                           for s in clean.splitlines()), default=0.0) / SS)
            if "[" in clean and "]" in clean:
                w_line += 52
            if not clean.startswith(("※", "◆")):
                mt = _TIMER_RE.search(clean) or _TIMER_ALT_RE.search(clean)
                if mt and timer_font:
                    w_line += measure.textlength(
                        mt.group(0).strip(), font=timer_font) / SS + 30
            needed = max(needed, w_line)
            if self._table_mode and "　" in clean \
                    and not clean.startswith(("※", "◆")):
                # 列宽必须**逐列**取最大值：整列的宽度由该列最宽的那一行决定，
                # 而每行的绘制起点又是按列累计的。若只按「首列 + 剩余整串」两列
                # 估算，第 3..N 列的对齐扩张就完全没进卡宽 —— 仲裁时间表 6 列时
                # 末尾的评级列会直接压到面板边框外（见 render 的列对齐绘制）。
                #
                # ⚠️ ※/◆ 行必须排除（与下面渲染期 arb_cols 的口径一致）：
                #    它们走的是整行绘制分支、拿不到 cells，但注释里常带全角空格
                #    （例如遗物卡的「※ 共 734 个：古纪 188　前纪 179　…」），
                #    混进来会把前几列的列宽算成注释片的宽度 → 表格总宽虚高
                #    → 触发下面的安全阀整体放弃列对齐（遗物列表曾因此不对齐）。
                for ci, cell in enumerate(clean.split("　")):
                    if ci == 0 and cell.lstrip().startswith("·"):
                        cell = cell.lstrip()[1:].strip()   # 与 _split_cells 一致
                    if ci >= len(col_max):
                        col_max.append(0.0)
                    col_max[ci] = max(
                        col_max[ci],
                        measure.textlength(cell, font=body_font) / SS)
        # —— 语法文档卡（指令一览）：说明列过宽时做「列内折行」规划 ——
        # 以前列宽超限会触发下面的安全阀整体放弃列对齐，后果是指令/说明
        # 双色与「A / B / C」多指令分色**全部失效**（2026-09-14 用户反馈
        # 「指令和介绍颜色太接近」「一行三个指令要三个颜色」的根因就在
        # 这：帮助卡从未真正走过列对齐路径）。现在把超宽说明折成多段、
        # 画在说明列内，保住列对齐与整套配色。
        desc_wrap: dict[int, list[str]] = {}
        if self._table_mode and self._plain_body and len(col_max) > 1:
            _two: list[tuple[int, str, str]] = []
            for _i, _raw in enumerate((lines or [""])[:MAX_BODY_LINES]):
                _clean, _ = _strip_emoji(_raw)
                _c = _split_cells(_clean) if "　" in _clean else None
                if _c and len(_c) == 2:
                    _two.append((_i, _c[0], _c[1]))
            desc_wrap, _w1 = _plan_desc_col_wrap(
                _two, col_max[0],
                lambda t, b: self._wrap(t, body_font, b),
                lambda t: measure.textlength(t, font=body_font) / SS)
            if desc_wrap:
                col_max[1] = _w1
        if self._table_mode and len(col_max) > 1:
            # 列对齐时每列按「该列最大宽度」绘制、列间固定 24px 间隙，
            # 故整表实际宽度 = Σ列宽 + 24×(列数-1)，必须整体参与卡宽计算。
            needed = max(needed, sum(col_max) + 24 * (len(col_max) - 1))
        # 元素/加成两列对齐：列位 = 第 1 格实测最大宽 + 间隙，整行宽按两格之和重算
        pair_col = None
        if len(pair_cells) >= 2:
            pair_col = (max(measure.textlength(c[0], font=body_font)
                            for c in pair_cells) + 30 * SS)
            _w2 = max(measure.textlength(c[1], font=body_font)
                      for c in pair_cells)
            needed = max(needed, (pair_col + _w2) / SS)
        else:
            pair_cells = []

        # 沉沦之地（炼狱塔）的三列：炼狱[N] / 任务类型 / 目标 ——
        # 同样按实测像素拉列，并且**任务类型列统一上类型色**
        # （用户反馈「任务类型有的标记有的不标记」：之前靠 token 规则，
        #   传承种捕获/压力锅这类模式名不在词表里就没色）。
        _DESC_HEAD = re.compile(r"^炼狱 \[\d+\]$")
        descent_cells = []
        for raw in (lines or [""])[:MAX_BODY_LINES]:
            clean, _ = _strip_emoji(raw)
            if "　" not in clean:
                continue
            _c = _split_cells(clean)
            if _c and len(_c) == 3 and _DESC_HEAD.match(_c[0]):
                descent_cells.append(_c)
        descent_col = None
        if len(descent_cells) >= 2:
            c0 = max(measure.textlength(c[0], font=body_font)
                     for c in descent_cells) + 30 * SS
            c1 = max(measure.textlength(c[1], font=body_font)
                     for c in descent_cells) + 30 * SS
            c2 = max(measure.textlength(c[2], font=body_font)
                     for c in descent_cells)
            descent_col = (c0, c1)
            needed = max(needed, (c0 + c1 + c2) / SS)
        else:
            descent_cells = []
        title_min = measure.textlength(title, font=title_font) / SS + 42 + 26 + 200
        W = int(max(880, min(1500, needed + pad * 2 + 96, 1500)))
        W = int(max(W, min(title_min + 120, 1500)))
        # 安全阀：卡宽有 880/1500 上下限，列对齐即便按上面的公式也可能在
        # 极端数据下仍放不进正文区。此时放弃列对齐、退回逐行折行，
        # 宁可少一点对齐观感，也不能把文字压出面板边框。
        if self._table_mode and len(col_max) > 1 \
                and (sum(col_max) + 24 * (len(col_max) - 1)) > W - 178:
            self._table_mode = False

        # —— 预排版：剥离行尾计时（右对齐成时间列）后折行 ——
        def kind_of(t: str) -> str:
            if t.startswith("※"):
                return "note"
            if t.startswith("◆"):
                return "section"
            if re.match(r"^\d+\.", t):
                return "numbered"
            return "normal"

        rows: list[dict] = []
        for _li, raw in enumerate((lines or [""])[:MAX_BODY_LINES]):
            clean, had_icon = _strip_emoji(raw)
            # 行首全角空格 = 缩进层级。**≥4 个**才额外缩进一级（赏金卡里
            # 「主目标 / 副目标」的分级就靠它）；2 个空格的行（奖励串、
            # 「任务：」标签）保持原样，不然整张卡会整体位移。
            _n_sp = len(raw) - len(raw.lstrip("　"))
            indent_lv = (_n_sp - 2) // 2 if _n_sp >= 4 else 0
            timer_txt = ""
            level_txt = ""
            mlv = _LV_RE.search(clean)      # 赏金行的「｜N-M级」抽出来右对齐
            if mlv:
                level_txt = mlv.group(1)
                # 等级后面若还有「｜标签」就保留分隔符，否则把行尾多余的分隔符一并去掉，
                # 不然会出现「◆ 削弱敌人的据点｜」这种吊着一根竖线的行。
                _tail = clean[mlv.end():]
                clean = (clean[:mlv.start()] + "｜" + _tail) if _tail \
                    else clean[:mlv.start()]
            if not clean.startswith(("※", "◆")):
                mt = _TIMER_RE.search(clean) or _TIMER_ALT_RE.search(clean)
                if mt:
                    timer_txt = mt.group(0).strip()
                    clean = (clean[:mt.start()] + clean[mt.end():]).rstrip(" ··")
            # 折行宽度预留：右侧时间列 + 芯片底框外扩，防止溢出压字
            wrap_w = W - pad * 2 - 56
            if "[" in clean and "]" in clean:
                wrap_w -= 64
            if indent_lv:
                wrap_w -= indent_lv * _INDENT_PX
            if timer_txt:
                wrap_w -= int(measure.textlength(timer_txt, font=f.get("bold", 27))
                              / SS) + 40
            if level_txt:
                wrap_w -= int(measure.textlength(level_txt, font=f.get("bold", 27))
                              / SS) + 46
            # 说明列折行规划命中：指令列原位、说明按预算折成多段，
            # 第 2..N 段作为续行（cells=["", 段]）画在说明列下方。
            if _li in desc_wrap:
                _c = _split_cells(clean)
                if _c and len(_c) == 2:
                    for _j, _seg in enumerate(desc_wrap[_li]):
                        rows.append({
                            "text": _c[0] if _j == 0 else _seg,
                            "icon": had_icon and _j == 0,
                            "timer": "", "level": "", "kind": "normal",
                            "indent": indent_lv,
                            "cells": [_c[0], _seg] if _j == 0 else ["", _seg]})
                    continue
            # ※ 注释行画的是 note_font（25px），但折行一直按 body_font（30px）
            # 测量 —— 量出来的宽度比实际大约 20%，注释因此**提前折行**、尾行
            # 只剩几个字（用户 2026-09-17：「排版有点干燥」的一个来源）。
            # 按实际绘制字号测量。
            base_kind = kind_of(clean)
            _wrap_font = note_font if base_kind == "note" else body_font
            wrapped = self._wrap(clean, _wrap_font, max(300, wrap_w))
            # 分列单元格（见 _split_cells 的说明）。
            # 折过行的文本不能再按原串切列：否则第 0 个折行会重画整行、
            # 后续折行又各画一次。折行行退回普通排版更安全。
            cells_split = _split_cells(clean) if len(wrapped) == 1 else None
            for j, seg in enumerate(wrapped):
                k = base_kind if (j == 0 or base_kind == "note") else kind_of(seg)
                rows.append({"text": seg, "icon": had_icon and j == 0,
                             "timer": timer_txt if j == 0 else "",
                             "level": level_txt if j == 0 else "", "kind": k,
                             "indent": indent_lv,
                             "cells": cells_split if j == 0 else None})

        # —— 等级徽章统一宽度 ——
        # 徽章按「宽的那个」统一宽度，"75-80级 / 95-100级 / 115-120级" 三个才能
        # 左缘也齐（只右对齐的话左缘参差，用户反馈「等级做的有些不美观」）。
        _lv_font = f.get("bold", 27)
        lv_box_w = max((measure.textlength(r["level"], font=_lv_font)
                        for r in rows if r.get("level")), default=0.0) + 34 * SS

        row_h = {"normal": 46, "section": 62, "note": 40, "numbered": 54}
        # 行高用行上存的 kind（说明列续行的 text 是说明片段，可能恰好以
        # 数字开头被 kind_of 误判成 numbered；存 kind 才是真实排版档位）
        body_h = sum(row_h[r["kind"]] for r in rows) + 10
        header_h = 150
        footer_h = 96
        H = header_h + body_h + footer_h + pad + 26

        # —— 画布 / 背景 / 星点 / 拱纹 ——
        img = Image.new("RGB", (W * SS, H * SS), BG_TOP)
        img.paste(_vgrad((W * SS, H * SS),
                         [(0.0, BG_TOP), (0.35, BG_MID), (1.0, BG_BOT)]), (0, 0))
        lay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        ld = ImageDraw.Draw(lay)
        self._stars(ld, W * SS, H * SS)
        self._orokin_arch(ld, W * SS, accent)
        img = Image.alpha_composite(img.convert("RGBA"), lay)
        d = ImageDraw.Draw(img)

        # —— 切角面板外框 ——
        inset = 22
        box = (inset * SS, inset * SS, (W - inset) * SS, (H - inset) * SS)
        _chamfer(d, box, 26 * SS, fill=(14, 18, 28, 214))
        _chamfer(d, box, 26 * SS, outline=GOLD_DIM + (255,), width=2 * SS)
        _chamfer(d, (box[0] + 5 * SS, box[1] + 5 * SS, box[2] - 5 * SS,
                     box[3] - 5 * SS), 22 * SS,
                 outline=(74, 64, 46, 170), width=1 * SS)
        for cx, cy, sx, sy in ((box[0], box[1], 1, 1), (box[2], box[1], -1, 1),
                               (box[0], box[3], 1, -1), (box[2], box[3], -1, -1)):
            L = 36 * SS
            d.line([(cx, cy + sy * L), (cx + sx * L, cy)],
                   fill=GOLD_BRIGHT + (255,), width=3 * SS)
            r = 5 * SS
            d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=GOLD_BRIGHT + (255,))

        # —— 头部 ——
        y = box[1] + 44 * SS
        x0 = box[0] + 42 * SS
        d.rectangle([x0, y + 2 * SS, x0 + 4 * SS, y + 66 * SS],
                    fill=accent + (255,))
        tx = x0 + 26 * SS
        # 芯片先测量：标题必须在芯片左侧收住。长标题（例：遗物列表「…已入库
        # 734」+ 页码）在卡宽上限内也放不下，不收就会压到右上角徽章上、
        # 再被面板边框裁掉（标题字号 46、无换行、无裁剪保护）。
        chip_text = " · ".join(x for x in (plat_chip, page_chip) if x)
        cf = small_font
        chip_pad_l, chip_mark_r, chip_gap = 22 * SS, 7 * SS, 14 * SS
        chip_h = 56 * SS
        chip_y0 = y + 5 * SS
        if chip_text:
            chip_tw = d.textlength(chip_text, font=cf)
            ch_x1 = box[2] - 42 * SS
            ch_x0 = ch_x1 - (chip_pad_l * 2 + chip_tw + chip_mark_r * 2 + chip_gap)
        else:
            chip_tw, ch_x0, ch_x1 = 0.0, 0.0, 0.0
        title_right = (ch_x0 - 24 * SS) if chip_text else (box[2] - 42 * SS)
        # 标题自适应：优先缩字号；最小字号仍放不下才省略号截断。绝不越界。
        t_font = title_font
        for _sz in (46, 42, 38, 34, 31, 28):
            _cand = f.get("bold", _sz)
            if _cand is None:
                continue
            t_font = _cand
            if d.textlength(title, font=t_font) <= title_right - tx:
                break
        else:
            _avail = title_right - tx
            _cut = title
            while _cut and d.textlength(_cut + "…", font=t_font) > _avail:
                _cut = _cut[:-1]
            title = (_cut + "…") if _cut else title
        d.text((tx, y), title, font=t_font, fill=INK + (255,))
        tw = d.textlength(title, font=t_font)
        ly = y + 68 * SS
        _hgrad_line(d, tx, tx + max(260 * SS, tw), ly, accent, 210)
        r = 6 * SS
        dx = tx + 14 * SS
        d.polygon([(dx, ly - r), (dx + r, ly), (dx, ly + r), (dx - r, ly)],
                  fill=GOLD_BRIGHT + (255,))
        sx_, sy_ = tx, y + 84 * SS
        for ch in subtitle:
            d.text((sx_, sy_), ch, font=sub_font, fill=GOLD + (225,))
            sx_ += d.textlength(ch, font=sub_font) + 4 * SS

        if chip_text:
            # 平台徽章：切角标签 + 菱形标记（与正文 ◆ 记号同一套视觉语言，
            # 不用「点 + 环」——缩小后那一对同心圆会被看成乱码字符）。
            # 底色必须是**预混好的不透明色**：ImageDraw 在 RGBA 上不混合，
            # 收尾 convert("RGB") 又丢 alpha，写 (255,255,255,12) 会渲染成纯白块。
            _chamfer(d, (ch_x0, chip_y0, ch_x1, chip_y0 + chip_h), 12 * SS,
                     fill=_tint((255, 255, 255), 26),
                     outline=accent + (255,), width=max(1, SS))
            cy = chip_y0 + chip_h / 2
            mx = ch_x0 + chip_pad_l + chip_mark_r
            d.polygon([(mx, cy - chip_mark_r), (mx + chip_mark_r, cy),
                       (mx, cy + chip_mark_r), (mx - chip_mark_r, cy)],
                      fill=accent + (255,))
            d.text((mx + chip_mark_r + chip_gap, cy), chip_text, font=cf,
                   fill=GOLD_BRIGHT + (255,), anchor="lm")

        # —— 仲裁表列对齐：按显示宽度计算各列像素起点 ——
        arb_cols = None
        if getattr(self, "_table_mode", False):
            import unicodedata as _ud

            def _dw(s: str) -> int:
                return sum(2 if _ud.east_asian_width(c) in ("F", "W") else 1
                           for c in s)

            # 只有真正分列的行才参与列宽计算；整行文本（如 ※ 注释、◆ 分组、
            # 单列说明）不能算进第一列，否则会把第二列整体推到卡片外。
            # ※/◆ 行即使含全角空格也按整行绘制（分支里不走 cells），所以
            # 它们的分格宽度也绝不能计入 —— 价格榜的注释行曾把数字列撑开
            split_rows = [r["cells"] for r in rows if r.get("cells")
                          and r["kind"] in ("normal", "numbered")]
            ncol = max((len(r) for r in split_rows), default=0)
            measure_f = body_font
            arb_cols = []
            acc = 0
            for ci in range(ncol):
                wpx = max((measure_f and d.textlength(
                    r[ci], font=measure_f) or 0)
                    for r in split_rows if len(r) > ci)
                arb_cols.append(acc)
                acc += wpx + 24 * SS
            if not split_rows:      # 没有任何分列行时退回普通排版
                arb_cols = None

        # —— 正文 ——
        y = box[1] + header_h * SS
        x_badge = box[0] + 40 * SS
        x_bullet = box[0] + 54 * SS
        x_text = box[0] + 82 * SS
        x_num_text = box[0] + 96 * SS
        x_right = box[2] - 52 * SS
        for i, r_ in enumerate(rows):
            t = r_["text"]
            k = r_["kind"]
            h = row_h[k] * SS
            # 地区行 = ◆ 开头且不含「｜」（赏金行才含）；做横幅化处理
            is_board = (getattr(self, "_bounty_mode", False) and k == "section"
                        and "｜" not in t)
            if k == "section":
                if is_board:
                    d.polygon(self._diamond(x_bullet - 4 * SS, y + h / 2, 10 * SS),
                              fill=accent + (255,))
                    self._draw_tokens(d, t[1:].strip(), x_text - 8 * SS, y + 6 * SS,
                                      f.get("bold", 34), accent, x_right)
                else:
                    d.polygon(self._diamond(x_bullet - 4 * SS, y + h / 2, 9 * SS),
                              fill=accent + (255,))
                    self._draw_tokens(d, t[1:].strip(), x_text - 8 * SS, y + 8 * SS,
                                      f.get("bold", 31), accent, x_right)
            elif k == "note":
                self._draw_tokens(d, t.lstrip("※").strip(), x_text, y + 8 * SS,
                                  note_font, INK_FAINT, x_right)
            elif k == "numbered":
                num, rest = t.split(".", 1)
                bh = 38 * SS
                badge = (x_badge, y + (h - bh) / 2, x_badge + bh, y + (h + bh) / 2)
                _chamfer(d, badge, 9 * SS, fill=_tint((255, 255, 255), 22),
                         outline=accent + (215,), width=1 * SS)
                nb = f.get("bold", 23)
                cx_b = (badge[0] + badge[2]) / 2
                cy_b = (badge[1] + badge[3]) / 2
                bb = d.textbbox((0, 0), num, font=nb, anchor="la")
                d.text((cx_b - (bb[0] + bb[2]) / 2, cy_b - (bb[1] + bb[3]) / 2),
                       num, font=nb, fill=GOLD_BRIGHT + (255,))
                self._draw_tokens(d, rest.strip(), x_num_text, y + 8 * SS,
                                  body_font, INK, x_right)
            else:
                # 缩进：由行首全角空格数决定（见上面的 indent_lv），
                # 赏金卡用它把「副目标 / 附加条件」压到主目标下一级。
                xt = x_text + r_.get("indent", 0) * _INDENT_PX * SS
                if r_["icon"]:
                    d.polygon(self._diamond(x_bullet, y + h / 2, 7 * SS),
                              fill=accent + (235,))
                elif t.startswith("·"):
                    t = t[1:].strip()
                    d.ellipse([x_bullet - 3 * SS, y + h / 2 - 3 * SS,
                               x_bullet + 3 * SS, y + h / 2 + 3 * SS],
                              fill=accent + (205,))
                # 赏金档位行：把行首任务类型单独用类型色画，再接着画后面的
                # 赏金名 / 等级标签（等级稍后由右对齐徽章覆盖）
                if self._bounty_mode and r_.get("level") and not r_["icon"]:
                    _mt, _rest = _split_leading_type(t)
                    if _mt:
                        d.text((xt, y + 8 * SS), _mt, font=body_font,
                               fill=MISSION_TYPE_COLOR + (255,))
                        # 「刺杀：H-09 坦克」这类已自带全角冒号的，不再补空格
                        _gap = "" if _mt.endswith("：") else " "
                        xt += d.textlength(_mt, font=body_font) + \
                            d.textlength(_gap, font=body_font)
                        t = _rest
                # “标签：值” 结构 -> 标签加粗，值常规；
                # 但分列行一律让位给列对齐（见 _is_label_row）。
                head, sep, rest = t.partition("：")
                if _is_label_row(head, bool(sep),
                                 bool(arb_cols is not None and r_.get("cells"))):
                    bold_font = f.get("bold", 29)
                    # 标签本身就是任务类型时（「刺杀：H-09 坦克」六人组节点名）
                    # 用任务类型色，别让类型看起来像正文标签
                    head_col = (MISSION_TYPE_COLOR if head in _MISSION_TYPE_SET
                                else INK)
                    d.text((xt, y + 8 * SS), head + "：", font=bold_font,
                           fill=head_col + (255,))
                    adv = d.textlength(head + "：", font=bold_font)
                    if self._bounty_mode and head == "任务":
                        self._draw_task_rest(d, rest, xt + adv, y + 9 * SS,
                                             body_font, x_right)
                    else:
                        self._draw_tokens(d, rest, xt + adv, y + 9 * SS,
                                          body_font, INK, x_right)
                else:
                    if arb_cols is not None and r_.get("cells"):
                        for ci, cell in enumerate(r_["cells"]):
                            if ci >= len(arb_cols):
                                break
                            cx = xt + arb_cols[ci]
                            if self._plain_body:
                                # 语法文档类：指令/说明双色直排，宽度与列宽
                                # 测量严格一致（色不改变字宽，不会破坏列对齐）
                                self._draw_plain_cell(d, ci, cell, cx,
                                                      y + 8 * SS, body_font)
                            else:
                                self._draw_tokens(d, cell, cx, y + 8 * SS,
                                                  body_font, INK, x_right)
                    else:
                        _cells = r_.get("cells")
                        if descent_col is not None and _cells \
                                and len(_cells) == 3 \
                                and _DESC_HEAD.match(_cells[0]):
                            # 沉沦之地三列：[层] / 任务类型(青) / 目标
                            self._draw_tokens(d, _cells[0], xt, y + 8 * SS,
                                              body_font, INK, x_right)
                            self._draw_tokens(d, _cells[1], xt + descent_col[0],
                                              y + 8 * SS, body_font,
                                              MISSION_TYPE_COLOR, x_right)
                            self._draw_tokens(d, _cells[2],
                                              xt + descent_col[0] + descent_col[1],
                                              y + 8 * SS, body_font, INK, x_right)
                        elif pair_col is not None and _cells \
                                and len(_cells) == 2 \
                                and _PAIR_RE.match(_cells[1]):
                            # 元素/加成：按公共列绘制（见 pair_col 的说明）
                            self._draw_tokens(d, _cells[0], xt, y + 8 * SS,
                                              body_font, INK, x_right)
                            self._draw_tokens(d, _cells[1], xt + pair_col,
                                              y + 8 * SS, body_font, INK,
                                              x_right)
                        else:
                            self._draw_tokens(d, t, xt, y + 8 * SS, body_font,
                                              INK, x_right)
            # 右对齐等级列（赏金卡）：钢蓝色徽章，整列扫读。
            # 徽章宽度**全卡统一**（lv_box_w），文字居中 —— 早先按各自文本宽画，
            # 「75-80级 / 95-100级 / 115-120级」左缘参差，看着像没对齐。
            lv = r_.get("level")
            if lv and not r_.get("timer"):
                lf = f.get("bold", 27)
                tw = d.textlength(lv, font=lf)
                bw = max(lv_box_w, tw + 28 * SS)
                bx0 = x_right - bw
                dh = 40 * SS
                _chamfer(d, (bx0, y + (h - dh) / 2, x_right,
                             y + (h + dh) / 2), 9 * SS,
                         fill=_tint(STEEL, 34), outline=_tint(STEEL, 150),
                         width=1 * SS)
                # 垂直居中：用 textbbox 的实际上/下伸部算基线位置。
                # 之前写死 `y + 9*SS`，数字与「级」的字形高度不同 → 看着上飘
                #（用户反馈「左右居中但是上下没居中」）。
                bb = d.textbbox((0, 0), lv, font=lf, anchor="la")
                ty = (y + (h - dh) / 2) + (dh - (bb[3] - bb[1])) / 2 - bb[1]
                d.text((bx0 + (bw - tw) / 2, ty), lv, font=lf,
                       fill=STEEL + (255,))
            # 右对齐时间列（琥珀主时长 + 暗金括号补充）
            if r_.get("timer"):
                tv = r_["timer"]
                main_txt, paren = tv, ""
                mp = re.search(r"（[0-9hms ]+）$", tv)
                if mp:
                    main_txt, paren = tv[:mp.start()], tv[mp.start():]
                tf = f.get("bold", 27)
                tw_main = d.textlength(main_txt, font=tf)
                tw_par = d.textlength(paren, font=note_font) if paren else 0
                tx0 = x_right - tw_main - (tw_par + 8 * SS if paren else 0)
                d.text((tx0, y + 9 * SS), main_txt, font=tf,
                       fill=AMBER + (255,))
                if paren:
                    d.text((tx0 + tw_main + 8 * SS, y + 10 * SS), paren,
                           font=note_font, fill=(170, 148, 106, 255))
            y += h
            if i < len(rows) - 1:
                nxt = rows[i + 1]
                if getattr(self, "_bounty_mode", False):
                    # 赏金卡：地区行下方画主题色细线，其余仅在「新赏金」前分隔，
                    # 让「赏金名 + 其奖励」成为视觉上的一组
                    if is_board:
                        _hgrad_line(d, box[0] + 46 * SS, box[2] - 46 * SS, y,
                                    accent, 115)
                    elif nxt["kind"] in ("section", "numbered"):
                        d.line([(box[0] + 46 * SS, y), (box[2] - 46 * SS, y)],
                               fill=(255, 255, 255, 9), width=1 * SS)
                else:
                    d.line([(box[0] + 46 * SS, y), (box[2] - 46 * SS, y)],
                           fill=(255, 255, 255, 9), width=1 * SS)

        # —— 底部 ——
        fy = box[3] - (footer_h - 6) * SS
        _hgrad_line(d, box[0] + 46 * SS, box[2] - 46 * SS, fy, GOLD, 130)
        mark = WATERMARK
        mx = box[2] - 46 * SS
        for ch in reversed(mark):
            cw = d.textlength(ch, font=sub_font)
            mx -= cw
            d.text((mx, fy + 18 * SS), ch, font=sub_font,
                   fill=GOLD_FAINT + (255,))
            mx -= 4 * SS
        ftxt = " · ".join(foot_notes)
        if ftxt:
            d.text((box[0] + 46 * SS, fy + 18 * SS), ftxt, font=sub_font,
                   fill=INK_FAINT + (220,))

        # —— 暗角 + 缩小 + 保存 ——
        # 用 BOX（面积平均）而非 LANCZOS：后者在高对比文字/描边边缘会产生
        # 振铃，缩小后表现为彩边与发虚。
        img = self._vignette(img, W * SS, H * SS)
        _rs = getattr(Image, "Resampling", Image)
        img = img.resize((W, H), _rs.BOX).convert("RGB")
        out = self.cache_dir / f"card_{random.randrange(1 << 40):010x}.png"
        # optimize=True 在容器里要 ~480ms（剖析结论）；compress_level=1
        # 同样无损、编码快 3~4 倍，代价只是文件略大（QQ 上传无感）
        img.save(out, "PNG", optimize=False, compress_level=1)
        self._cleanup(keep=2)
        return str(out)

    # ------------------------------------------------------------------
    @staticmethod
    def _parse_footer(footer: str) -> tuple[str, list[str]]:
        """解析「平台：PC · warframe.market 紫卡」→ ("PC", ["warframe.market 紫卡"])"""
        plat, notes = "", []
        for seg in (footer or "").split("·"):
            seg = seg.strip()
            if not seg:
                continue
            if seg.startswith("平台：") or seg.startswith("平台:"):
                plat = seg.split("：", 1)[-1].split(":", 1)[-1].strip().upper()
            else:
                notes.append(seg)
        return plat, notes

    @staticmethod
    def _wrap(text: str, font, max_w_px: int) -> list[str]:
        if not text:
            return [""]
        # 行内自带换行符时先按 \n 拆开，否则下面的 textlength 会抛
        # "can't measure length of multiline text"（PIL 拒绝测多行串）。
        if "\n" in text:
            return [seg for parts in text.split("\n")
                    for seg in ImageRenderer._wrap(parts, font, max_w_px)]
        max_w = max_w_px * SS
        d = ImageDraw.Draw(Image.new("RGB", (8, 8)))
        out, line = [], ""
        for ch in text:
            if line and d.textlength(line + ch, font=font) > max_w:
                # 优先在中文分隔符后断开：不这样，「▣Xaku机体蓝图、★雷射瞄具」这类
                # 用「、」连起来的奖励串会被从词中间劈开（「▣X」/「aku机体蓝图」）。
                sep = max((line.rfind(c) for c in "、·；，～"), default=-1)
                if sep > len(line) * 0.5:
                    out.append(line[:sep + 1])
                    line = line[sep + 1:] + ch
                    continue
                # 英文单词断在一半时，回退到最近的空格处换行
                if ch.isascii() and ch.isalnum() and line[-1].isascii() \
                        and line[-1].isalnum():
                    sp = line.rfind(" ")
                    if sp > len(line) * 0.5:
                        out.append(line[:sp])
                        line = line[sp + 1:] + ch
                        continue
                out.append(line)
                line = ch
            else:
                line += ch
        if line or not out:
            out.append(line)
        # 清理断点处的悬空分隔点
        out = [seg.rstrip(" ·、") for seg in out]
        out = [out[0]] + [seg.lstrip(" ·、") for seg in out[1:]]
        return [seg for seg in out if seg]

    def _draw_task_rest(self, d, rest: str, x: float, y: float, font,
                        max_w: float) -> None:
        """赏金「任务：**类型** **挑战名** 目标」三段分别上色。

        用户要求任务类型与挑战名各自有颜色（「任务类型现在像和后面文字是一块的」）。
        结构由 ``formatters._oracle_task_lines`` 保证：**前两段不含 ASCII 空格**
        （名字里的空格被换成 NBSP），所以按空格切最多三段＝类型 / 挑战名 / 目标。
        挑战名缺失时第二段会被当成目标（罕见：bounty 恒带 challenge）。
        """
        parts = rest.split(" ", 2)
        segs: list[tuple[str, Optional[tuple[int, int, int]]]] = []
        if parts and parts[0]:
            segs.append((parts[0], MISSION_TYPE_COLOR))
        if len(parts) > 1 and parts[1]:
            segs.append((parts[1], CHALLENGE_COLOR))
        if len(parts) > 2 and parts[2]:
            segs.append((parts[2], None))
        gap = d.textlength(" ", font=font)
        for i, (text, color) in enumerate(segs):
            if color is None:
                self._draw_tokens(d, text, x, y, font, INK, max_w)
                x += d.textlength(text, font=font)
            else:
                d.text((x, y), text, font=font, fill=color + (255,))
                x += d.textlength(text, font=font)
            if i < len(segs) - 1:
                x += gap

    # 语法文档类卡片（指令一览等 _plain_body）的配色：
    # 指令列亮色 + 说明列次色，合并词条「A / B / C」逐个异色（2026-09-14 用户要求
    # 「介绍和指令别混在一起」「本质三个指令的用三个颜色」）。
    _HELP_CMD_COLORS = (GOLD_BRIGHT, CYAN, VIOLET, GREEN)
    # 说明列专用暗色：比 INK_DIM 再压一档（2026-09-14 用户反馈「指令和介绍
    # 颜色太接近，不放大容易混在一起」——金色指令列 vs 暗灰说明列才够分明）。
    HELP_DESC_COLOR = (118, 127, 143)

    def _draw_plain_cell(self, d, ci: int, cell: str, cx: float, y: float,
                         font) -> None:
        """_plain_body 表格单元格：指令列上色、说明列压暗。"""
        if not cell:
            return
        if ci != 0:
            d.text((cx, y), cell, font=font, fill=self.HELP_DESC_COLOR + (255,))
            return
        parts = cell.split(" / ")
        if len(parts) == 1:
            d.text((cx, y), cell, font=font, fill=GOLD_BRIGHT + (255,))
            return
        x = cx
        sep = " / "
        for i, p in enumerate(parts):
            col = self._HELP_CMD_COLORS[i % len(self._HELP_CMD_COLORS)]
            d.text((x, y), p, font=font, fill=col + (255,))
            x += d.textlength(p, font=font)
            if i < len(parts) - 1:
                d.text((x, y), sep, font=font, fill=GOLD_DIM + (255,))
                x += d.textlength(sep, font=font)

    def _draw_tokens(self, d, text: str, x: float, y: float, font, base_color,
                     max_w: float):
        """按语义 token 上色绘制（行内高亮）。超过 3 个方括号 token 的行降级为
        纯色词条（避免芯片墙溢出）；芯片超出右缘时同样回退纯文本。"""
        rules = [
            (re.compile(r"^(剩|已结束)"), AMBER),
            (re.compile(r"^（[0-9hms ]+）$"), (170, 148, 106)),
            (re.compile(r"^(夜晚|夜间)$"), VIOLET),
            (re.compile(r"^(白天|白昼|温暖)$"), (232, 150, 96)),
            (re.compile(r"^寒冷$"), CYAN),
            (re.compile(r"^(法斯\(白天\)|法斯)$"), (232, 150, 96)),
            (re.compile(r"^沃姆\(夜晚\)|沃姆$"), VIOLET),
            (re.compile(r"^已抵达$"), GREEN),
            (re.compile(r"^\d+p$"), CYAN),
            (re.compile(r"^★"), GOLD_BRIGHT),
            (re.compile(r"^▣"), BLUEPRINT),
            # 紫卡词条：▲ 正面统一青绿、▼ 负面统一红（同一属性只有一种颜色）
            (re.compile(r"^▲"), GREEN),
            (re.compile(r"^▼"), RED),
            (re.compile(r"^信\d+$"), (168, 148, 112)),
            (re.compile(r"^网页在线$"), BLUE),
            (re.compile(r"^在线$"), GREEN),
            (re.compile(r"^离线$"), INK_FAINT),
            (re.compile(r"^(钢铁|执刑官)$"), RED),
            (re.compile(r"^九重天$"), CYAN),
            (re.compile(r"^(满级|零级)$"), GOLD_BRIGHT),
            (re.compile(r"^\d+日\d+时$"), (140, 190, 240)),
            (re.compile(r"^\d{2,4}\s?\[(S)\]$", re.I), RED),
            (re.compile(r"^\d{2,4}\s?\[(?:A\+)\]$"), GOLD_BRIGHT),
            (re.compile(r"^S$|^\d{2,4}\sS$"), RED),
            (re.compile(r"^A\+$"), GOLD_BRIGHT),
            (re.compile(r"^A$|^A-$"), GREEN),
            (re.compile(r"^B\+$|^B$"), (150, 200, 150)),
            (re.compile(r"^C$|^未评级$"), INK_FAINT),
            (re.compile(r"^(?:I系|C系|G系|O系|低语)$"), (240, 170, 110)),
            (re.compile(r"^\d{2,4}\s(?:S)$"), RED),
            (re.compile(r"^\d{2,4}\s(?:A\+)$"), GOLD_BRIGHT),
            (re.compile(r"^\d{2,4}\s(?:A-|A|B\+|B)$"), GREEN),
            (re.compile(r"^\d{2,4}\s(?:F)$"), INK_FAINT),
            (re.compile(r"^\d{2,4}\s?\[(?:A-|A|B\+|B)\]$"), GREEN),
            (re.compile(r"^\d{2,4}\s?\[F\]$"), INK_FAINT),
            # 奸商预测卡：序号用暗金（弱化）、杜卡德报价金色（价格类统一高亮）
            (re.compile(r"^\d+\.$"), (150, 128, 88)),
            (re.compile(r"^\d+\s*杜卡德$"), GOLD_BRIGHT),
            (re.compile(r"^\d+杜$"), GOLD_BRIGHT),
            (re.compile(r"^\d+(?:\.\d+)?万?现金$"), CYAN),
        ]
        plain_body = getattr(self, "_plain_body", False)
        brackets = len(re.findall(r"\[[^\]]*\]", text))
        chip_ok = brackets <= 3
        pos = 0
        for m in _TOKEN_RE.finditer(text):
            if m.start() > pos:
                seg = text[pos:m.start()]
                d.text((x, y), seg, font=font, fill=base_color + (255,))
                x += d.textlength(seg, font=font)
            tok = m.group(0)
            color, chip = base_color, False
            inner = tok[1:-1] if (tok.startswith("[") and tok.endswith("]")) else None
            if inner is not None and not plain_body and chip_ok \
                    and (inner in TIER_COLOR or inner in GROUP_CHIP_COLOR):
                # 只有真正的遗物等级 / 物品大类才做词条芯片，
                # 语法文档里的 [...] 保持朴素
                color = TIER_COLOR.get(inner) or GROUP_CHIP_COLOR[inner]
                chip = True
            elif inner is None and not plain_body:
                # 语义配色优先（任务类型 / 元素 / 派系 / 加成%）——
                # 用集合直查，不走下面的正则表，避免和评级、平台等 token 抢匹配。
                if tok in _MISSION_TYPE_SET:
                    color = MISSION_TYPE_COLOR
                elif tok in _ELEMENT_SET:
                    color = ELEMENT_COLOR
                elif tok in _FACTION_SET:
                    color = FACTION_COLOR
                elif _PCT_RE.match(tok):
                    color = BONUS_COLOR
                else:
                    for pat, c in rules:
                        if pat.match(tok):
                            color = c
                            break
            wch = d.textlength(tok, font=font)
            if chip:
                gap, pad, bh = 7 * SS, 10 * SS, 40 * SS
                if pos > 0:
                    x += gap
                if x + wch + pad * 2 > max_w:   # 越界则退回纯文字
                    chip = False
                    x -= gap if pos > 0 else 0
            if chip:
                cb = (int(x - pad), int(y - 3 * SS), int(x + wch + pad),
                      int(y - 3 * SS + bh))
                _chamfer(d, cb, 8 * SS, fill=color + (24,), outline=color + (150,))
                bb = d.textbbox((0, 0), tok, font=font, anchor="la")
                ty = cb[1] + (bh - (bb[3] - bb[1])) / 2 - bb[1]
                d.text((x, ty), tok, font=font, fill=color + (255,))
                x = cb[2] + gap
            else:
                d.text((x, y), tok, font=font, fill=color + (255,))
                x += wch
            pos = m.end()
        if pos < len(text):
            d.text((x, y), text[pos:], font=font, fill=base_color + (255,))

    @staticmethod
    def _diamond(cx: float, cy: float, r: float):
        return [(cx, cy - r), (cx + r, cy), (cx, cy + r), (cx - r, cy)]

    @staticmethod
    def _stars(d, w: int, h: int, n: int = 110):
        rng = random.Random(20260909)
        for _ in range(n):
            x, y = rng.randrange(w), rng.randrange(int(h * 0.85))
            a = rng.randrange(16, 78)
            r = rng.choice((1, 1, 1, 2)) * SS
            if rng.random() < 0.18:
                color = GOLD_BRIGHT + (a,)
            else:
                color = (200, 214, 235, a)
            d.ellipse([x - r, y - r, x + r, y + r], fill=color)

    @staticmethod
    def _orokin_arch(d, w: int, accent_color):
        """右上角大 Orokin 拱环暗纹。"""
        cx, cy = int(w * 0.92), int(w * 0.085)
        for i, rr in enumerate((150, 118, 86, 54)):
            a = 26 - i * 5
            col = (accent_color if i % 2 else GOLD) + (a,)
            d.arc([cx - rr * SS, cy - rr * SS, cx + rr * SS, cy + rr * SS],
                  start=200, end=520, fill=col, width=3)

    _VIGNETTE: dict = {}          # (w,h) → 暗角层（尺寸固定，永久缓存）

    @staticmethod
    def _vignette(img, w: int, h: int):
        # 暗角层与内容无关、只跟尺寸有关：resize + gaussian_blur 要 ~380ms，
        # 每张卡都重算是纯浪费（2026-09-17 容器内剖析结论）。整体缓存复用。
        key = (w, h)
        black = ImageRenderer._VIGNETTE.get(key)
        if black is None:
            mask = Image.new("L", (w // 4, h // 4), 0)
            md = ImageDraw.Draw(mask)
            mw, mh = mask.size
            md.rounded_rectangle(
                [mw // 10, mh // 10, mw * 9 // 10, mh * 9 // 10],
                radius=mw // 6, fill=110)
            mask = mask.resize((w, h)).filter(
                ImageFilter.GaussianBlur(w // 14))
            black = Image.new("RGBA", (w, h), (2, 3, 6, 255))
            black.putalpha(mask.point(lambda v: int(v * 0.55)))
            if len(ImageRenderer._VIGNETTE) > 24:      # 尺寸种类有限
                ImageRenderer._VIGNETTE.clear()
            ImageRenderer._VIGNETTE[key] = black
        return Image.alpha_composite(img, black)

    def _cleanup(self, keep: int = 6):
        try:
            cards = sorted(self.cache_dir.glob("card_*.png"),
                           key=lambda p: p.stat().st_mtime)
            for old in cards[:-keep]:
                old.unlink()
        except OSError:
            pass
