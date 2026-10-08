# -*- coding: utf-8 -*-
"""渲染引擎：文本卡片（-w）与图片卡片（-t / auto）。

- 文本模式：全角感知（CJK 宽度按 2 列计算）的字符表格，游戏内可直接复制；
- 图片模式：Warframe Orokin UI 风格的 Pillow 信息卡片（暗金切角面板、
  语义着色、徽章芯片、2x 超采样）；缺字体/Pillow 时由调用方降级文本。

性能优化（2026-09-26，逐像素输出不变）：
  · 暗角缓存改存 L 模式 alpha 掩码 + 字节预算 LRU（旧版存全尺寸 RGBA 层、
    上限 24 条 —— 大卡单条 73MB、极端可到 ~1.7GB 常驻，是低内存机器上
    「发作式」慢渲染（swap 换入风暴）的最大单一来源）；
  · 暗角合成从「整幅黑色层 + 一次全幅 alpha_composite」改为分条带原位合成
    （省掉两张全尺寸 RGBA 的瞬时分配）；星点/拱纹层同理瓦片化；
  · token 上色规则表提升到模块级并记忆化（旧版每次调用重建 40 个正则）；
  · 渐变背景直接作为画布、量测画布/渐变列/图标缩放缓存复用。
  全部改动均经 bench_compare.py 旧版 vs 新版逐字节对拍验证。

性能优化（2026-09-27，逐像素输出不变）：
  · 字体度量记忆化 _tlen()：textlength 按 (字体, 文本) 挂缓存（cProfile
    显示 90 行大卡 ~30% 耗时耗在重复度量同一批 token/单元格/说明串）；
  · 预扫描合并：旧版对 lines 有 4 遍独立扫描（宽度需求 / 说明列规划 /
    沉沦三列 / 行构造），每遍都重复 _strip_emoji + _split_cells +
    计时正则搜索，现合并为一遍预处理（语义逐行等价，纯去重）；
  · _wrap 整行快速路径（仅基本布局、无极性标记；此时 textlength 是逐字
    步进纯加和、累计宽单调不减，判定与逐字路径严格等价）+ 说明列折行
    结果按渲染缓存（列宽规划与行构造各折一次的重复工作归零）；
  · 渐变列放大 NEAREST 直出 RGBA 画布（源为 1×h 单列、水平全同像素，
    与「BICUBIC 放大 RGB + 全幅 convert(RGBA)」逐字节等价，省一张
    全幅 RGB 中转与一次全幅通道转换，90 行大卡省 ~70ms）；
  · 渐变金线从逐 2px 的 ~1500 段 draw.line 合并为单笔 rectangle：各段
    可见 RGB 相同（仅 alpha 不同、终图丢弃 alpha），合并后逐字节一致；
  · 孤立星点改预渲染 sprite（形状只取决于半径+颜色，~110 颗只有 ~24 种
    组合）；暗角黑色层跨条带复用同一缓冲；暗角掩码 LRU 预算 96MB→48MB
    （6 张基准卡全尺寸合计 ~26MB，仍全缓存，峰值驻留减半）；
  · 渲染收尾显式断开 ImageDraw 对画布的强引用：旧版 draw 对象存活到
    函数返回，编码阶段整幅画布多驻留 ~80MB，是峰值工作集的主要构成。
  全部改动经 bench_selfcontained.py 六卡 sha256 逐字节对拍验证。
"""

from __future__ import annotations

import random
import re
import sys
import unicodedata
from collections import OrderedDict
from pathlib import Path
from typing import Optional

from .logging_compat import logger as _LOGGER

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


# 行内标记：⟦pol:<key>⟧ = 极性小图标（画图模式就地绘制，文本模式剥掉）。
# 生成方见 core/wiki_intro.py（知识库「自带极性：…」行）。
_POL_MARK_RE = re.compile(r"⟦pol:([a-z_]+)⟧")


def _expand_tents_line(line: str) -> list[str]:
    """⟦tents⟧ 点位机器行 → 文本模式纵列（「　小帐篷 X：任务｜任务」）。

    机器行是图片模式的排版指令；文本模式没有块与颜色，展开回原来的
    每点位一行，信息不丢（⟦pol:…⟧ 的「剥掉」先例在这里不够 —— 剥掉
    等于整个点位块消失）。
    """
    if "⟦tents⟧" not in line:
        return [line]
    out: list[str] = []
    for seg in _TENT_SPLIT_RE.split(line):
        parts = [p.strip() for p in seg.split("｜") if p.strip()]
        if parts:
            out.append(f"　{parts[0]}：{'｜'.join(parts[1:])}")
    return out or [line]


def text_card(title: str, lines: list[str], footer: str = "", max_width: int = 44) -> str:
    """构造排版整齐的字符界面卡片。"""
    title = _POL_MARK_RE.sub("", title or "")
    lines = [seg for x in lines for seg in _expand_tents_line(_POL_MARK_RE.sub("", x or ""))]
    # ⟦c⟧/⟦v⟧/⟦w⟧ 及其闭合 = 染段标记（画图模式分段上色），文本模式剥掉
    lines = [_SEG_MARK_RE.sub("", x) for x in lines]
    footer = _POL_MARK_RE.sub("", footer or "")
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

# 共享量测画布：textlength 只查字体度量、不触碰画布，全程可复用。
# 旧版 _wrap 每调用一次（= 每正文行一次，大卡 680+ 次）都新建 8×8 图像
# + ImageDraw 对象，纯属分配开销。
_MEASURE_DRAW: Optional["ImageDraw.ImageDraw"] = None

# 基本布局枚举（Pillow ≥ 9.2）。基本布局下 textlength 是字形步进的**纯加和**
# （无整形/字距调整），这是 _wrap 增量测量的前提；Raqm 环境退回原路径。
try:
    _LAYOUT_BASIC = ImageFont.Layout.BASIC
except AttributeError:  # pragma: no cover —— 旧版 Pillow 无 Layout 枚举
    _LAYOUT_BASIC = None


def _measure_draw() -> "ImageDraw.ImageDraw":
    global _MEASURE_DRAW
    if _MEASURE_DRAW is None:
        _MEASURE_DRAW = ImageDraw.Draw(Image.new("RGB", (8, 8)))
    return _MEASURE_DRAW


def _malloc_trim() -> None:
    """best-effort 把已释放的堆内存还给 OS（glibc；其它平台静默跳过）。

    渲染峰值可达数百 MB，glibc 默认不归还空闲页，进程 RSS 水位长期居高
    ——低内存机器上后续任何触到被换出页的操作都会拖慢（「发作式」慢渲染
    的另一诱因）。大卡渲染完 trim 一次，成本微秒级。
    """
    if sys.platform != "linux":
        return
    try:
        import ctypes

        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception:  # noqa: BLE001 —— musl / 无 libc.so.6 等一律忽略
        pass


def _tlen(d, font, text: str) -> float:
    """textlength 记忆化：同 (字体, 文本) 的像素宽度只算一次。

    PIL 的 textlength 每次调用都要走一遍 FreeType 字形度量（CJK 长串尤其
    昂贵），而同一 (字体, 文本) 的结果恒定。token、表格单元格、说明串、
    计时段这些短串在整个排版流程里会被反复测量（预扫描、列宽、行构造、
    绘制推进各一遍），缓存后全部变为一次 dict 命中。缓存挂在字体对象上：
    字体被回收时缓存随之释放，不产生跨渲染的全局强引用（热重载安全）。
    超长串不缓存（几乎不重复，白占内存）；命中上限防长期驻留膨胀。
    """
    if len(text) > 160:
        return d.textlength(text, font=font)
    try:
        cache = font._wf_len
    except AttributeError:
        cache = {}
        try:
            font._wf_len = cache
        except AttributeError:  # pragma: no cover —— 字体对象禁挂属性时直测
            return d.textlength(text, font=font)
    w = cache.get(text)
    if w is None:
        w = d.textlength(text, font=font)
        if len(cache) >= 16384:
            cache.clear()
        cache[text] = w
    return w


FONT_DIR = Path(__file__).resolve().parent / "data" / "fonts"  # core/data/fonts
# 极性小图标（wiki 抓取的 64px PNG，2026-09-24）。该目录不进开源/市场包
# （同 fonts，见 dist/package_release.py::OSS_EXCLUDE_DIRS）——文件缺失时
# 行内标记静默退化为不画图标，纯文本照常。
POLARITY_DIR = Path(__file__).resolve().parent / "data" / "icons" / "polarity"
_POL_ICONS: dict = {}


def _polarity_icon(key: str):
    """极性图标（RGBA Image）；缺失或坏文件返回 None（装饰项，不抛）。"""
    if key in _POL_ICONS:
        return _POL_ICONS[key]
    img = None
    if Image is not None:
        try:
            p = POLARITY_DIR / f"{key}.png"
            if p.exists():
                with Image.open(p) as im:
                    img = im.convert("RGBA")
        except Exception:  # noqa: BLE001
            img = None
    _POL_ICONS[key] = img
    return img


def _pol_mark_px(font) -> float:
    """一个 ``⟦pol:…⟧`` 标记的绘制宽度（设备像素，含图标后间距）。

    与 ``_draw_polarity_icon`` 的宽度算法保持一致：换行测量时标记按这个
    宽度计入（不能按 ⟦pol:vazarin⟧ 这串字符的宽度算，否则长行会提前折行）。
    图标缺失（开源包不带 icons 目录）时返回 0。
    """
    ref = _polarity_icon("madurai")
    if ref is None or not ref.height:
        return 0.0
    size = int(getattr(font, "size", 30) * 1.1)
    size = max(12, min(size, 96))
    return (round(ref.width * size / ref.height) + 7) * SS


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
# 版本从 core.__version__ 取**完整三位**、品牌从 core.__brand_card__ 取，
# 避免与 metadata.yaml / main.py 注册版本走散
# （曾经三处各写一份、抬版本或换品牌时漏改，水印留在旧号/旧名上）。
# ★ 2026-10-02 用户要求：水印/状态卡标题显示**完整版本号**（如 1.1.3），
#   不再取主次版本（1.1）—— 抬版本时这里无需任何手动改动，单一来源自动跟。
try:  # pragma: no cover - 兜底分支只在包结构异常时走到
    from . import __version__ as _CORE_VERSION
    from . import __brand_card__ as _CARD_BRAND

    WATERMARK_VERSION = str(_CORE_VERSION) or "1.0"
    CARD_BRAND = str(_CARD_BRAND)
except Exception:  # noqa: BLE001
    WATERMARK_VERSION = "1.0"
    CARD_BRAND = "SDJKBOT"
WATERMARK = f"WARFRAME  ·  {CARD_BRAND} {WATERMARK_VERSION}"

# 配色：Orokin 暗金 + Tenno 能量色
BG_TOP = (10, 13, 20)
BG_MID = (17, 23, 36)
BG_BOT = (11, 15, 23)
GOLD = (212, 176, 106)
GOLD_BRIGHT = (240, 215, 154)
GOLD_DIM = (122, 100, 62)
GOLD_FAINT = (96, 82, 54)
INK = (238, 232, 218)  # 主文字：暖白
INK_DIM = (154, 163, 178)  # 次文字
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
BLUEPRINT = (245, 178, 92)  # 战甲/武器部件、蓝图：橙金（与 MOD 的亮金区分）

# 切角面板底色。面板 fill 写的是 (14,18,28,214)，但 alpha 会被丢弃，
# 实际呈现就是这个色，故作为「面板内元素」的半透明混色基准。
PANEL_BG = (14, 18, 28)


def _tint(
    color: tuple[int, int, int], alpha: int, bg: tuple[int, int, int] = PANEL_BG
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
TIER_COLOR = {
    "古纪": GOLD,
    "Lith": GOLD,
    "前纪": STEEL,
    "Meso": STEEL,
    "中纪": GREEN,
    "Neo": GREEN,
    "后纪": (232, 154, 138),
    "Axi": (232, 154, 138),
    "安魂": VIOLET,
    "Requiem": VIOLET,
    "万灵": GOLD_BRIGHT,
    "Omnia": GOLD_BRIGHT,
    "全能": GOLD_BRIGHT,
    "Vanguard": TEAL,
    "先锋": TEAL,
}

# 物品大类芯片色（奸商预测卡用）：把 `[MOD]` `[装饰]` 这类小标签染成一色一类，
# 一眼能看出这行是 MOD 还是外观 —— 用户 2026-09-18 要求「注意颜色运用」。
# 配色沿用已有调色板，不引入新色系。
GROUP_CHIP_COLOR = {
    "MOD": VIOLET,
    "主要 MOD": VIOLET,
    "武器": CYAN,
    "遗物": GOLD,
    "装饰": AMBER,
    "外观": MAGENTA,
    "其他": INK_DIM,
    "消耗品": GREEN,
    "礼包": TEAL,
}

# ---------------------------------------------------------------------------
# 行内语义配色：把「任务类型 / 挑战名 / 派系 / 元素 / 加成%」和正文分开
#
# 用户反馈「任务类型像和后面文字是一块的」「元素和元素加成分别来个其他颜色」——
# 靠的是把这几类词从正文里**切成 token**，再按规则上色（见 _TOKEN_RE / _TOKEN_RULES）。
#
# ⚠️ 中文没有词边界，所以任务类型 / 元素 / 派系一律用
#    `(?:^|(?<=[\s　｜（]))…(?=[\s　｜）]|$)` 这种**前后边界**包住：
#    不包的话「电磁力场装置」里的「磁力」、「致命冲击」里的「冲击」都会被染色，
#    看起来像卡出错。★/▣ 开头的 token 因为先被匹配走，不受影响。
# ---------------------------------------------------------------------------
MISSION_TYPE_COLOR = CYAN  # 任务类型（歼灭/生存/虚空决战…）
CHALLENGE_COLOR = VIOLET  # 赏金挑战名（能量超载/终结好戏…）
FACTION_COLOR = (240, 170, 110)  # 派系（Grineer / 科腐者 / 合一众…）与 I系/C系 同色
# 赏金卡不做派系染色的英文派系名（2026-10-02 用户要求：Grineer/Corpus 在赏金里
# 默认不染色 —— 任务名/奖励里混着大段英文名，整片发橙太吵）；其他卡照旧。
_BOUNTY_PLAIN_FACTION = frozenset({"Grineer", "Corpus"})

# ---------------------------------------------------------------------------
# 行内染段标记（formatters 侧打标，2026-10-02）：⟦c⟧…⟦/c⟧ 蓝、⟦v⟧…⟦/v⟧ 紫、
# ⟦w⟧…⟦/w⟧ 白。首标记前的文本走正常绘制路径（行首任务类型染色、标签加粗等）；
# 首标记之后的所有文本（含标记外的余文与空格）按段直绘、**不走语义 token** ——
# 目标/节点名里的派系词（低语者/炽蛇军）、类型词（捕获）不能被词表串色。
# 用于 oracle 三地区档位行的三段染色：类型青 / 挑战名紫 / 目标白（1999 的
# 节点名整体蓝 —— 派系词在节点名里必须跟整体同色）。
# ⚠️ 文本模式由 text_card 剥掉；⟦tents⟧/⟦pol:…⟧ 是别的机制，不匹配本模式。
# ---------------------------------------------------------------------------
_SEG_MARK_RE = re.compile(r"(⟦/?[a-z]⟧)")
_SPAN_COLORS = {"c": BLUE, "v": VIOLET, "w": INK}


def _parse_color_segs(t: str) -> tuple[str, list[tuple[str, tuple[int, int, int]]]]:
    """拆行内染段标记，返回 (主文本, [(段文本, 颜色), ...])。

    主文本 = 首标记前的部分（可能为空）；其后每段未标记文本沿用当前段色
    （段外默认白），开标记切换段色、闭标记回到白。无标记时返回 (原文, [])。
    """
    if "⟦" not in t:
        return t, []
    main_parts: list[str] = []
    segs: list[tuple[str, tuple[int, int, int]]] = []
    color: Optional[tuple[int, int, int]] = None
    seen = False
    for part in _SEG_MARK_RE.split(t):
        if not part:
            continue
        if part.startswith("⟦"):
            seen = True
            mm = re.fullmatch(r"⟦([a-z])⟧", part)
            color = _SPAN_COLORS.get(mm.group(1), INK) if mm else INK
            continue
        if not seen:
            main_parts.append(part)
        else:
            segs.append((part, color or INK))
    return "".join(main_parts), segs


ELEMENT_COLOR = TEAL  # 伤害/元素类型（磁力/冰冻/毒素…）
BONUS_COLOR = GOLD_BRIGHT  # 加成百分比（25.7%）

# ---------------------------------------------------------------------------
# 赏金档位框（_bounty_mode）：每个档位（任务名 + 任务 + 奖励）包一个圆角细框
# ---------------------------------------------------------------------------
# ★ 2026-09-27：**采用外援优化版的设计**（用户选定，替代我这版「40% 白中性框」）——
# 底色比面板亮一档 + **暖金描边**（沿用主面板内框线的色，不引入新色系）；
# 等级徽章嵌框内右上；档位名前的圆点随之去掉（框已承担分组视觉，用户 2026-09-26 口径）。
# 起因：块间那条 `alpha=9`（≈3.5% 白）的淡线用户实测「看不清」，块与块难区分。
TIER_FRAME_FILL = (23, 27, 37, 214)  # 底色比面板亮一档；alpha 与主面板同为 214
TIER_FRAME_LINE = (74, 64, 46, 255)  # 描边沿用主面板内框线色（暖金）
_TIER_PAD = 8  # 框缘到行顶/行底的内边距（设计像素）
_TIER_TAIL = 24  # 组尾距：组间距 = 尾距 − 2×内边距 = 8px
# 档位框的左右边界（相对正文列）：框比文字列**外扩**，视觉上包住等级徽章。
# 点位三块（_draw_tent_blocks）沿用同一对边界 —— 否则最左/最右块与上方档位框
# 对不齐（2026-10-02 用户反馈「左右边距去掉」）。⚠️ 两处共用，别再各写数字。
_TIER_FRAME_L = 16  # 左缘 = x_text − 16
_TIER_FRAME_R = 46  # 右缘 = box[2] − 46

# ---------------------------------------------------------------------------
# 赏金点位块（2026-10-02 用户要求改版）：底部横向三块（小帐篷 A/B/C）
# ---------------------------------------------------------------------------
# 格式层把每个点位的「标题｜任务1｜任务2…」用 ⟦tents⟧ 分隔串成**一行机器行**
# （formatters._tent_lines）；渲染层拆开后每点位画一个圆角框（沿用档位框配色），
# 块顶标题、下方纵排任务名。文本模式由 text_card 展开回纵列。
_TENT_SPLIT_RE = re.compile(r"⟦tents⟧")
_TENT_GAP = 18  # 块间距（设计像素，下同）
_TENT_PAD_X = 14  # 块内水平内边距
_TENT_PAD_TOP = 10  # 块内顶部内边距（标题行之上）
_TENT_HEAD_H = 44  # 标题行高
_TENT_LINE_H = 40  # 任务行高
_TENT_PAD_BOT = 14  # 末行之下余量
# 蓝色高亮的点位任务（官方简中链名；比对前去空白——「捕获 Grineer 特工」带空格）。
# ⚠️ 拼写以 tents 实际用的 bounty_jobs_zh.json 为准：「搜索并救援」（另一张
#    官方表 bounty_job_names.json 写「搜索与救援」，两种拼写都收防串表）。
_TENT_BLUE_TASKS = frozenset(
    {"捕获grineer特工", "找出遗失的器物", "取回被偷的器物", "搜索并救援", "搜索与救援"}
)


def _tents_of(text: str) -> Optional[list[list[str]]]:
    """拆 ⟦tents⟧ 机器行 → ``[[标题, 任务…], …]``；非机器行返回 None。"""
    if "⟦tents⟧" not in text:
        return None
    blocks: list[list[str]] = []
    for seg in _TENT_SPLIT_RE.split(text):
        parts = [p.strip() for p in seg.split("｜") if p.strip()]
        if parts:
            blocks.append(parts)
    return blocks or None


def _tent_norm(name: str) -> str:
    """任务名比对键：去空白 + 小写（「捕获 Grineer 特工」→「捕获grineer特工」）。"""
    return re.sub(r"[\s\u3000]+", "", name).lower()


# 任务类型全集 = DE 官方 missionName 中文（ExportRegions / MissionName_*）
#                  ∪ ExportBounties 末阶段映射出来的那几个。
# 长的必须排在前面：「资源回收」要优先于「回收」，「移动防御」优先于「防御」。
MISSION_TYPE_WORDS: tuple[str, ...] = (
    "INFESTED 资源回收",
    "资源回收",
    "物资回收",
    "移动防御",
    "镜像防御",
    "元素转换",
    "虚空洪流",
    "虚空覆涌",
    "虚空决战",
    "虚空天使",
    "传承种收割",
    "联结生存",
    "圣殿突袭",
    "无尽回廊",
    "黑暗地带战争",
    "多方交战",
    "自由漫游",
    "沉沦之地",
    "星际航道结合点",
    "祈运坛防御",
    "Follie 的狩猎",
    "佩里塔叛乱",
    "前哨战 + 刺杀",
    "武形秘仪",
    "对战",
    "前哨战",
    "歼灭",
    "刺杀",
    "捕获",
    "拦截",
    "挖掘",
    "防御",
    "破坏",
    "救援",
    "间谍",
    "生存",
    "劫持",
    "回收",
    "净化",
    "伏击",
    "中断",
    "扬升",
    "强袭",
    "清巢",
    "叛逃",
    "追击",
    "突袭",
    "爆发",
    "奥影母艇",
    "奥影",
    "衰退室",
    "竞技场",
)
ELEMENT_WORDS: tuple[str, ...] = (
    "磁力",
    "辐射",
    "腐蚀",
    "毒气",
    "病毒",
    "爆炸",
    "冲击",
    "穿刺",
    "切割",
    "火焰",
    "冰冻",
    "电击",
    "毒素",
)
FACTION_WORDS: tuple[str, ...] = (
    "Grineer",
    "Corpus",
    "Infested",
    "Sentient",
    "Corrupted",
    "奥罗金",
    "低语者",
    "低语",
    "合一众",
    "炽蛇军",
    "科腐者",
)
_W_L = r"(?:^|(?<=[\s　｜（]))"  # 词左边界
_W_R = r"(?=[\s　｜）]|$)"  # 词右边界


def _word_alt(words: tuple[str, ...]) -> str:
    """把词表编成「带左右边界的 alternation」，直接塞进 _TOKEN_RE。"""
    return _W_L + "(?:" + "|".join(re.escape(w) for w in words) + ")" + _W_R


_MISSION_TYPE_SET = frozenset(MISSION_TYPE_WORDS)
_ELEMENT_SET = frozenset(ELEMENT_WORDS)
_FACTION_SET = frozenset(FACTION_WORDS)
_PCT_RE = re.compile(r"\d+(?:\.\d+)?%\Z")
# 行首的任务类型（长的优先，`re` 的 alternation 是「先匹配先赢」，词表已排好序）
_LEAD_TYPE_RE = re.compile("^(?:" + "|".join(re.escape(w) for w in MISSION_TYPE_WORDS) + ")")


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
    rest = text[len(word) :]
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
    "[\U0001f000-\U0001fbff\u2600-\u2604\u2606-\u2609\u260a-\u26ff"
    "\u2700-\u27bf\u2b00-\u2bff\u2300-\u23ff\ufe0f\u2049\u203c\u2b50]"
)
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
    r"(\[[^\]]*\]|★+[^、\s　]*|▣[^、\s　]+|\d+p(?![a-zA-Z])|"
    + _TIMER_MARK
    + _DUR
    + r"|（[0-9hms ]+）|已结束"
    r"|▲[^、\s　]+|▼[^、\s　]+|信\d+"
    # 「钢铁之路」必须在「钢铁」前面：alternation 先匹配先赢，反了的话
    # 赏金标签「钢铁之路」只有前两个字被染红（2026-10-02 用户要求 4 字全红）。
    r"|网页在线|在线|离线|钢铁之路|钢铁|九重天|执刑官|满级|零级"
    r"|\d+日\d+时"
    r"|(?:I系|C系|G系|O系)"
    # 语义配色：任务类型 / 元素 / 派系 / 加成百分比（见上面的说明）
    # ⚠️ 必须排在旧分组**前面**（尤其派系要压过原来的「低语」），
    #    alternation 是「先匹配先赢」，排在后面就会被短词抢先。
    r"|"
    + _word_alt(FACTION_WORDS)
    + r"|"
    + _word_alt(MISSION_TYPE_WORDS)
    + r"|"
    + _word_alt(ELEMENT_WORDS)
    + r"|\d+(?:\.\d+)?%"
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
    r"|(?:^|(?<=[\s　]))(?:S|A\+|A-|A|B\+|B|C|未评级)(?=[\s　]|$))"
)
# 行尾独立计时（右对齐时间列用），如 “剩余 58分钟（58m 36s）”
_TIMER_RE = re.compile(_TIMER_MARK + _DUR + r"(?:（[0-9hms ]+）)?")
# 「N分钟 后」型（时效总览行尾）
_TIMER_ALT_RE = re.compile(_DUR + r"\s*后(?:开始|抵达)?\s*$")
# 赏金行内的等级（“｜5-15级｜” / 行尾“｜5-15级”）—— 提取后右对齐成等级列，便于整列扫读。
# ⚠️ 行尾必须也认：赏金行只有「任务名｜等级｜标签」三段的才有尾随「｜」，
#    而「任务名｜等级」这种两段行（无钢铁之路/合一众标签）旧正则匹配不到，
#    于是同一张卡上前面几行等级内联、后面几行等级右对齐，看起来像排版坏了。
_LV_RE = re.compile(r"｜(\d+-\d+级)(?:｜|$)")
# 「上架 6h / 12m / 45s / >100h」—— wr 拍卖行的挂单时长（2026-10-08）：
#   放行尾，交给同一套「行尾抽取 → 右对齐列」机制，与赏金行的等级/计时同列对齐。
_AGE_RE = re.compile(r"(?:^|[\s　])(上架\d+[smh]|上架>\d+h)\s*$")

# 「· 武器名　元素 25.7%」—— 第二格是「元素 + 百分比」时，整列按**实测像素宽**对齐
# （只有渲染层知道真实字形宽度；formatter 端按 CJK=2 / ASCII=1 手算会差出一个字，
#  用户反馈「没对齐真的好丑」就是这么来的）。
_PAIR_RE = re.compile(r"^[^\s　]+ [\d.]+%\Z")

# 原内联在 _render 里的三个热路径正则提升为模块常量（每次渲染少过一遍
# re 模块的缓存查找；语义逐字节不变）。
_NUM_HEAD_RE = re.compile(r"^\d+\.")  # 行首序号（numbered 行）
_DESC_HEAD_RE = re.compile(r"^炼狱 \[\d+\]$")  # 沉沦之地三列的首格
_TIMER_PAREN_RE = re.compile(r"（[0-9hms ]+）$")  # 计时段尾的括号补充

# ---------------------------------------------------------------------------
# token 上色：规则表 + 结果记忆化
#
# 旧版把下面这张 40 条的规则表写在 _draw_tokens_text **函数体内**，每画一段
# 文本就 re.compile 40 次（靠 re 内部缓存兜底，但 40 次查找 + 建表本身
# 每行都要付一遍）。提到模块级只建一次；匹配结果再按 token 文本记忆化，
# 「在线 / 离线 / 生存」这类高频词从「逐条试规则」变成一次 dict 命中。
# 记忆化只存与 base_color 无关的**判定**，上色时再结合 base_color，
# 输出与逐条试规则完全一致。
# ---------------------------------------------------------------------------
_TOKEN_RULES: tuple[tuple["re.Pattern[str]", tuple[int, int, int]], ...] = (
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
    (re.compile(r"^(钢铁之路|钢铁|执刑官)$"), RED),
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
)

# token 文本 -> (是否方括号词条, 能否做芯片, 芯片色, 语义配色或 None)
_TOKEN_STYLE: dict[
    str, tuple[bool, bool, Optional[tuple[int, int, int]], Optional[tuple[int, int, int]]]
] = {}


def _token_style(
    text: str,
) -> tuple[bool, bool, Optional[tuple[int, int, int]], Optional[tuple[int, int, int]]]:
    """解析单个 token 的上色判定（与旧版逐条试规则的控制流等价）。

    Returns:
        (has_inner, chip_capable, chip_color, special_color)：
        has_inner     = 是否 [xxx] 形态；
        chip_capable  = 词条属于遗物纪元 / 物品大类（芯片候选）；
        chip_color    = 芯片色（chip_capable 时非 None）；
        special_color = 非词条 token 的语义配色；None = 沿用 base_color。
    """
    st = _TOKEN_STYLE.get(text)
    if st is not None:
        return st
    inner = text[1:-1] if (text.startswith("[") and text.endswith("]")) else None
    if inner is not None:
        if inner in TIER_COLOR or inner in GROUP_CHIP_COLOR:
            color = TIER_COLOR.get(inner) or GROUP_CHIP_COLOR.get(inner)
            st = (True, True, color, None)
        else:
            st = (True, False, None, None)
    else:
        color = None
        if text in _MISSION_TYPE_SET:
            color = MISSION_TYPE_COLOR
        elif text in _ELEMENT_SET:
            color = ELEMENT_COLOR
        elif text in _FACTION_SET:
            color = FACTION_COLOR
        elif _PCT_RE.match(text):
            color = BONUS_COLOR
        else:
            for pat, c in _TOKEN_RULES:
                if pat.match(text):
                    color = c
                    break
        st = (False, False, None, color)
    if len(_TOKEN_STYLE) > 8192:  # 词条含价格/百分比，量级有限但设上限防膨胀
        _TOKEN_STYLE.clear()
    _TOKEN_STYLE[text] = st
    return st


def _strip_emoji(text: str) -> tuple[str, bool]:
    """去掉彩色 emoji（CJK 字体缺字形），返回 (净化文本, 行首是否曾有 emoji)。"""
    had_head = bool(text) and bool(_EMOJI_RE.match(text))
    clean = _EMOJI_RE.sub("", text).strip()
    return clean, had_head


class _Fonts:
    """字体加载：打包 Noto CJK 优先，其次**用户自放字体**，随后系统常见 CJK 字体。

    用户字体目录 = ``plugin_data/<插件名>/fonts/``（渲染器按 cache_dir 的父目录
    推出）。放这里**随插件更新保留**：市场更新是整包替换插件目录
    （AstrBot star_manager 的更新流程，2026-09-25 实测确认），
    手放进包内 ``core/data/fonts/`` 的字体会被删掉。
    """

    # 打包字体：完整字库（fetch_font.py 下载，**不进发行包**）优先；
    # 其次是**随包分发的子集**（v1.0.7 起，GB2312 全表 6763 字 + 语料符号，
    # 两档共约 6.6MB）——市场版没有完整字库，靠它开箱出图（issue #1）。
    PACKED = [
        FONT_DIR / "NotoSansCJK-Regular.ttc",
        FONT_DIR / "NotoSansCJK-Bold.ttc",
        FONT_DIR / "NotoSansCJKsc-Subset-Regular.otf",
        FONT_DIR / "NotoSansCJKsc-Subset-Bold.otf",
    ]
    # 系统常见 CJK 字体（Windows / macOS / Linux 各一）
    SYSTEM = [
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"),
        Path("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc"),
        Path("/usr/local/lib/python3.12/site-packages/pillowmd/data/fonts/yahei.ttf"),
        Path("C:/Windows/Fonts/msyh.ttc"),
        Path("/System/Library/Fonts/PingFang.ttc"),
    ]
    CANDIDATES = PACKED + SYSTEM  # 兼容既有引用（顺序：打包 → 系统）

    _FONT_SUFFIXES = (".ttc", ".ttf", ".otf")

    def __init__(self, user_dirs: Optional[list[Path]] = None):
        self.regular: Optional[Path] = None
        self.bold: Optional[Path] = None
        self._sc_index = 0
        self._cache: dict[tuple[str, int], "ImageFont.FreeTypeFont"] = {}
        # 打包 → 用户自放 → 系统（用户字体优先于系统字体：明确放了就该用它）
        for path in self.PACKED + self._user_fonts(user_dirs or []) + self.SYSTEM:
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

    # 随包字体资产：更新/重装随包替换属预期，**不参与用户字体迁移**
    SHIPPED_FONTS = frozenset({"NotoSansCJKsc-Subset-Regular.otf", "NotoSansCJKsc-Subset-Bold.otf"})

    @classmethod
    def _user_fonts(cls, entries: list[Path]) -> list[Path]:
        """展开用户字体入口：文件直接用；目录按名排序取全部字体文件。"""
        out: list[Path] = []
        for e in entries:
            try:
                p = Path(e)
                if p.is_file() and p.suffix.lower() in cls._FONT_SUFFIXES:
                    out.append(p)
                elif p.is_dir():
                    out.extend(
                        sorted(
                            q
                            for q in p.iterdir()
                            if q.is_file() and q.suffix.lower() in cls._FONT_SUFFIXES
                        )
                    )
            except OSError:
                continue
        return out

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
                if (
                    name in ("Noto Sans CJK SC",)
                    or "YaHei" in name
                    or "PingFang" in name
                    or "WenQuanYi" in name
                ):
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
                self._cache[key] = ImageFont.truetype(str(path), size * SS, index=self._sc_index)
            except Exception:  # noqa: BLE001
                return None
        return self._cache[key]


def _chamfer(draw: "ImageDraw.ImageDraw", box, c: int, fill=None, outline=None, width: int = 1):
    """切角八边形：Warframe UI 面板的基础形。"""
    x0, y0, x1, y1 = box
    c = max(1, min(c, (x1 - x0) // 2, (y1 - y0) // 2))
    pts = [
        (x0 + c, y0),
        (x1 - c, y0),
        (x1, y0 + c),
        (x1, y1 - c),
        (x1 - c, y1),
        (x0 + c, y1),
        (x0, y1 - c),
        (x0, y0 + c),
    ]
    draw.polygon(pts, fill=fill, outline=outline, width=width)


# ---------------------------------------------------------------------------
# 星点 sprite 预渲染缓存
#
# ★ 2026-09-27：星点形状只取决于 (半径, 颜色) 两个量 —— 110 颗随机星点里
# 实际只有 半径{2,4} × alpha(16..78) × 两套色 ≈ 248 种组合且高度重复。
# 预渲染成小 sprite 后，每颗孤立星点只剩一次小图 alpha_composite，省掉
# 每颗一次的 Image.new + ImageDraw + ellipse（瓦片合成数学不变，等价性
# 由 bench 六卡 sha 对拍兜底）。
# ---------------------------------------------------------------------------
_STAR_SPRITES: dict[tuple[int, tuple[int, int, int, int]], Image.Image] = {}


def _star_sprite(r: int, color: tuple[int, int, int, int]) -> Image.Image:
    """半径 r 的星点 sprite（(2r+1)² RGBA），与瓦片路径同一几何。"""
    ck = (r, color)
    sp = _STAR_SPRITES.get(ck)
    if sp is None:
        sp = Image.new("RGBA", (2 * r + 1, 2 * r + 1), (0, 0, 0, 0))
        ImageDraw.Draw(sp).ellipse([0, 0, 2 * r, 2 * r], fill=color)
        if len(_STAR_SPRITES) > 512:
            _STAR_SPRITES.clear()
        _STAR_SPRITES[ck] = sp
    return sp


# ---------------------------------------------------------------------------
# 表格式卡片：哪些标题走「按全角空格切列」的列对齐
# ---------------------------------------------------------------------------
# 2026-09-27 从 `render()` 里的内联 or-链抽成函数，为的是能用**正/反两组用例**
# 锁住（审核要求：新规则不得改变既有卡片行为）。新增卡片时**同笔**补：
#   ① 这里 ② `tests/test_render_parse.py` 的白名单正反用例。
# 判定沿用历史口径：**子串**命中（注意「仲裁排期」不是「仲裁时间表」的子串，
# 两者互不误命中）。
_TABLE_TITLE_PATTERNS = (
    "仲裁时间表",
    "指令一览",
    "侵袭",
    "价格排行",
    "紫卡热度",
    "遗物入库",
    "遗物出库",
    "遗物列表",
    "部件出处",
    "虚空商人",  # 奸商当期货单：两列 ×（名称/杜卡德/现金）六列网格
    "九重天虚空风暴",  # 2026-09-27：节点/类型/剩余时间 三列（原来用 ｜ 串联）
    "仲裁排期",  # 2026-09-27：时间/节点（星球）/类型/派系/[评级] 分列
    "结合仪式目标",  # 2026-09-27：名称/节点/类型（派系）三列
)


def is_table_title(title: str) -> bool:
    """标题是否走列对齐（表模式）。子串命中，语义与抽函数前一字不变。"""
    return any(p in (title or "") for p in _TABLE_TITLE_PATTERNS)


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


def _plan_desc_col_wrap(
    cells, col0: float, wrap_fn, measure_fn, w_cap: int = 1500, reserve: int = 178, gap: int = 24
) -> tuple[dict, float]:
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
    if budget < max(8.0, w_cap * 0.15):  # 指令列本身离谱宽时别硬折，交安全阀
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


_VGRAD_COLS: dict[int, "Image.Image"] = {}  # 高度 -> 1×h 渐变列（只读，可复用）


def _vgrad(size, stops):
    """垂直多段渐变背景，**直接产出 RGBA 画布**。

    ★ 2026-09-27 优化：
    · 列仍只与高度有关，按高度缓存（逐行 Python 循环不重复付）；
    · 旧版「RGB 列 → 整幅 resize(RGB) → 整幅 convert(RGBA)」要在 90 行大卡
      上先付一张 ~60MB 的全幅 RGB 中转、再一次全幅通道转换；现在把 1×h
      列先转 RGBA（1×h，可忽略）再放大，全流程只有一次全幅分配；
    · 放大核 BICUBIC → NEAREST：源是 1×h 单列，水平方向所有像素相同、
      垂直方向 1:1，两种核的插值结果逐字节一致（含 2240×4888 / 2240×8936
      对拍），但省掉 4-tap 卷积，大卡再省 ~40ms。
    """
    w, h = size
    col = _VGRAD_COLS.get(h)
    if col is None:
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
        if len(_VGRAD_COLS) > 12:
            _VGRAD_COLS.clear()
        _VGRAD_COLS[h] = col
    return col.convert("RGBA").resize((w, h), Image.Resampling.NEAREST)


def _hgrad_line(draw, x0: int, x1: int, y: int, color, peak_alpha: int = 220):
    """两端渐隐的水平金线。

    ⚠️ 此处**不能**把逐 2px 的 line 段合并成单笔 rectangle（哪怕可见 RGB
    相同）：各段写入画布的 alpha 各不相同，而这些像素落在 alpha=214 的
    面板底色上，收尾暗角 alpha_composite 的 over 归一化会读取目标 alpha
    —— alpha 马赛克一变，暗角后的 RGB 就变（实测 sha 不一致）。逐段绘制
    是输出不变的底线，勿再「优化」。
    """
    mid = (x0 + x1) / 2
    span = max(1.0, (x1 - x0) / 2)
    for x in range(int(x0), int(x1), SS):
        k = 1 - abs(x - mid) / span
        a = int(peak_alpha * (k**1.5))
        if a > 2:
            draw.line([(x, y), (min(x + SS, x1), y)], fill=color + (a,), width=SS)


def migrate_legacy_user_fonts(
    user_dir: Path, legacy_dir: Path = FONT_DIR
) -> list[tuple[Path, Path]]:
    """把插件目录里的**用户自带字体**搬进插件数据目录（一次性、幂等、只移不抄）。

    依据 AstrBot 开发原则（官方插件文档「开发原则」）：**持久化数据请存储于 data
    目录下，而非插件自身目录，防止更新/重装插件时数据被覆盖**。用户按旧 README
    把手放字体放进 ``core/data/fonts/`` 后，市场更新整包替换即丢（issue #1 报告者
    正是这种情形）——本函数在启动时把它们搬到 ``plugin_data/<插件>/fonts/``。

    · 随包子集 otf（``_Fonts.SHIPPED_FONTS``）是**分发资产**，留在原地不动；
    · 目标已有同名文件时跳过（不覆盖用户已有字体）；
    · 只处理 ``.ttc/.ttf/.otf``；移动而非复制（避免同一字体被加载两次）。
    返回 [(源, 目标)] 迁移清单（空 = 无旧字体或已迁完）。
    """
    moved: list[tuple[Path, Path]] = []
    if not legacy_dir.is_dir():
        return moved
    # 护栏：开发树 / git 克隆（插件根有 .git）不迁移 —— 那里的完整字库是**仓库
    # 跟踪的资产**，搬走会弄脏工作树；git 用户更新走 pull，不会删未跟踪文件，
    # 手放字体本来就安全。只有「市场安装」这种整包替换的场景才需要迁移。
    plugin_root = legacy_dir.parents[2]  # <插件>/core/data/fonts → <插件>
    if (plugin_root / ".git").exists():
        return moved
    for src in sorted(legacy_dir.iterdir()):
        try:
            if not src.is_file() or src.name in _Fonts.SHIPPED_FONTS:
                continue
            if src.suffix.lower() not in _Fonts._FONT_SUFFIXES:
                continue
            dst = user_dir / src.name
            if dst.exists():
                continue
            user_dir.mkdir(parents=True, exist_ok=True)
            src.replace(dst)
            moved.append((src, dst))
        except OSError:
            continue
    return moved


class ImageRenderer:
    """Orokin 风格 Pillow 渲染器；不可用时 render() 返回 None 降级文本。"""

    def __init__(self, cache_dir: Path, font_path: Optional[str] = None):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        # 用户自放字体：plugin_data/<插件>/fonts/（随插件更新保留，见 _Fonts 文档）；
        # font_path 传入时优先（文件或目录均可）
        user_dirs: list[Path] = []
        if font_path:
            user_dirs.append(Path(font_path))
        user_dirs.append(self.cache_dir.parent / "fonts")
        self.fonts = _Fonts(user_dirs=user_dirs) if Image is not None else None

    @property
    def available(self) -> bool:
        return Image is not None and self.fonts is not None and self.fonts.regular is not None

    def render(self, title: str, lines: list[str], footer: str = "") -> Optional[str]:
        if not self.available:
            return None
        import time as _t

        _t0 = _t.perf_counter()
        try:
            path = self._render(title, lines, footer)
            # 耗时埋点（定位「出图慢」用；>1200ms 抬到 WARNING 级别提醒）
            _ms = (_t.perf_counter() - _t0) * 1000
            _msg = "[warframe] 卡片渲染 %.0f ms（%d 行）：%s"
            if _ms > 1200:
                _LOGGER.warning(_msg, _ms, len(lines), title)
            else:
                _LOGGER.info(_msg, _ms, len(lines), title)
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
        accent, subtitle = GOLD, f"WARFRAME {CARD_BRAND}"
        for keys, (color, _slug) in TITLE_THEME:
            if any(k in title for k in keys):
                accent = color
                break
        self._plain_body = (
            "指令一览" in title
            or "可蹲" in title
            or title.startswith("蹲")
            or title.startswith("帮助")
        )  # 语法文档不做行内装饰
        # 赏金卡：地区行做横幅、等级右对齐成列、同组行之间不画分隔线
        self._bounty_mode = "赏金任务" in title
        # 侵袭卡的 4 列（类型/派系/等级/地点）走既有列对齐机制；
        # 价格榜 / 紫卡热度榜的多列数字同样按全角空格切列对齐
        # 遗物三张列表卡（入库/出库/列表）同样是网格排版：不开这个模式的话
        # 单元格只会顺序拼接，「Lith A1/Lith A10/…」长短不一会导致列位抖动
        # （用户 2026-09-17：「排版有点干燥，也没对齐」）。
        # 部件反查卡（2026-09-18）是「遗物 / 槽位 / 状态」三列，同理。
        self._table_mode = is_table_title(title)
        plat_chip, foot_notes = self._parse_footer(footer)

        # —— 宽度优先：逐行实测需求（文本+芯片外扩+时间列），卡宽上限 1500 ——
        # ★ 2026-09-27 优化：旧版对 (lines) 有 4 遍独立扫描 —— 宽度需求、
        #   说明列规划（_plain_body 表格）、沉沦之地三列、正文行构造 —— 每遍
        #   都重复 _strip_emoji + _split_cells + 计时正则。现把预处理结果
        #   (raw, clean, had_icon) 存一次，宽度/列宽/配对/双列/三列收集合并进
        #   同一遍循环：语义逐行等价（各收集器判据原样保留），纯去重。
        #   另：textlength 全部走 _tlen 记忆化（token/单元格/说明串在
        #   预扫描、列宽、行构造、绘制推进里被反复测量）。
        self._wrap_cache = {}  # 说明列折行结果缓存（本次渲染内有效）
        pad = 46
        measure = _measure_draw()
        timer_font = f.get("bold", 27)
        needed = 0.0
        col_max: list[float] = []  # 表格式卡片：**逐列**的最大像素宽
        # 「· 武器名　元素 25.7%」这类两格行：记下来，稍后拉一条公共列把第二格对齐。
        # **必须在这里算**：卡宽 W 是用 needed 定的，对齐会让最宽行变宽，
        # 只在排版后补算就来不及调卡宽了（会压出面板边框）。
        pair_cells: list[list[str]] = []
        two_cells: list[tuple[int, str, str]] = []  # 说明列规划用的双列行
        descent_cells: list[list[str]] = []
        src = (lines or [""])[:MAX_BODY_LINES]
        cleaned: list[tuple[str, str, bool]] = []
        for _li, raw in enumerate(src):
            clean, had_icon = _strip_emoji(raw)
            cleaned.append((raw, clean, had_icon))
            # 点位机器行：按「三块总宽」计入卡宽（块内文字最大宽 + 内边距×2，
            # 等宽三块 + 块间距），不走下面的普通行宽口径
            if clean.startswith("⟦tents⟧"):
                _blocks = _tents_of(clean) or []
                _tw = max((_tlen(measure, body_font, s) for b in _blocks for s in b), default=0.0)
                _nb = max(1, len(_blocks))
                needed = max(
                    needed,
                    (_nb * (_tw + 2 * _TENT_PAD_X * SS) + (_nb - 1) * _TENT_GAP * SS) / SS + 10,
                )
                continue
            if "　" in clean:
                _cells = _split_cells(clean)
                if _cells:
                    if len(_cells) == 2 and _PAIR_RE.match(_cells[1]):
                        pair_cells.append(_cells)
                    if len(_cells) == 3 and _DESC_HEAD_RE.match(_cells[0]):
                        descent_cells.append(_cells)
                    if self._table_mode and self._plain_body and len(_cells) == 2:
                        two_cells.append((_li, _cells[0], _cells[1]))
            # PIL 的 textlength 拒绝测多行串；而 handler 拼出来的行可能自带 \n
            # （例如 wiki 的「标题\n链接」）。这里取最长的一行作为该行的宽度需求。
            # ★ 注脚（※）行画的是 note_font（25px），量宽度也必须用它：
            #   按 body_font（30px）量会虚高 20%，把卡宽硬生生撑大 ——
            #   部件反查卡实测行内容只到 810px、卡宽却被注脚撑到 1416px，
            #   右半边空着（与折行字号那处是同一个 bug 的两半）。
            _lf = note_font if clean.startswith("※") else body_font
            # 极性图标标记：按图标宽度计入、字符串本身剥离（同 _wrap 口径）
            _n_mark = clean.count("⟦pol:")
            _meas_txt = _POL_MARK_RE.sub("", clean) if _n_mark else clean
            if "⟦" in _meas_txt:
                # 染段标记零宽（与 _wrap 的口径一致），不剥会虚增卡宽
                _meas_txt = _SEG_MARK_RE.sub("", _meas_txt)
            w_line = max((_tlen(measure, _lf, s) for s in _meas_txt.splitlines()), default=0.0) / SS
            if _n_mark:
                w_line += _n_mark * _pol_mark_px(_lf) / SS
            if "[" in clean and "]" in clean:
                w_line += 52
            if not clean.startswith(("※", "◆")):
                mt = _TIMER_RE.search(clean) or _TIMER_ALT_RE.search(clean) or _AGE_RE.search(clean)
                if mt and timer_font:
                    w_line += _tlen(measure, timer_font, mt.group(0).strip()) / SS + 30
            needed = max(needed, w_line)
            if self._table_mode and "　" in clean and not clean.startswith(("※", "◆")):
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
                        cell = cell.lstrip()[1:].strip()  # 与 _split_cells 一致
                    if ci >= len(col_max):
                        col_max.append(0.0)
                    col_max[ci] = max(col_max[ci], _tlen(measure, body_font, cell) / SS)
        # —— 语法文档卡（指令一览）：说明列过宽时做「列内折行」规划 ——
        # 以前列宽超限会触发下面的安全阀整体放弃列对齐，后果是指令/说明
        # 双色与「A / B / C」多指令分色**全部失效**（2026-09-14 用户反馈
        # 「指令和介绍颜色太接近」「一行三个指令要三个颜色」的根因就在
        # 这：帮助卡从未真正走过列对齐路径）。现在把超宽说明折成多段、
        # 画在说明列内，保住列对齐与整套配色。
        desc_wrap: dict[int, list[str]] = {}
        if self._table_mode and self._plain_body and len(col_max) > 1:
            desc_wrap, _w1 = _plan_desc_col_wrap(
                two_cells,
                col_max[0],
                lambda t, b: self._wrap_cached(t, body_font, b),
                lambda t: _tlen(measure, body_font, t) / SS,
            )
            if desc_wrap:
                col_max[1] = _w1
        if self._table_mode and len(col_max) > 1:
            # 列对齐时每列按「该列最大宽度」绘制、列间固定 24px 间隙，
            # 故整表实际宽度 = Σ列宽 + 24×(列数-1)，必须整体参与卡宽计算。
            needed = max(needed, sum(col_max) + 24 * (len(col_max) - 1))
        # 元素/加成两列对齐：列位 = 第 1 格实测最大宽 + 间隙，整行宽按两格之和重算
        pair_col = None
        if len(pair_cells) >= 2:
            pair_col = max(_tlen(measure, body_font, c[0]) for c in pair_cells) + 30 * SS
            _w2 = max(_tlen(measure, body_font, c[1]) for c in pair_cells)
            needed = max(needed, (pair_col + _w2) / SS)
        else:
            pair_cells = []

        # 沉沦之地（炼狱塔）的三列：炼狱[N] / 任务类型 / 目标 ——
        # 同样按实测像素拉列，并且**任务类型列统一上类型色**
        # （用户反馈「任务类型有的标记有的不标记」：之前靠 token 规则，
        #   传承种捕获/压力锅这类模式名不在词表里就没色）。
        descent_col = None
        if len(descent_cells) >= 2:
            c0 = max(_tlen(measure, body_font, c[0]) for c in descent_cells) + 30 * SS
            c1 = max(_tlen(measure, body_font, c[1]) for c in descent_cells) + 30 * SS
            c2 = max(_tlen(measure, body_font, c[2]) for c in descent_cells)
            descent_col = (c0, c1)
            needed = max(needed, (c0 + c1 + c2) / SS)
        else:
            descent_cells = []
        title_min = _tlen(measure, title_font, title) / SS + 42 + 26 + 200
        W = int(max(880, min(1500, needed + pad * 2 + 96, 1500)))
        W = int(max(W, min(title_min + 120, 1500)))
        # 安全阀：卡宽有 880/1500 上下限，列对齐即便按上面的公式也可能在
        # 极端数据下仍放不进正文区。此时放弃列对齐、退回逐行折行，
        # 宁可少一点对齐观感，也不能把文字压出面板边框。
        if (
            self._table_mode
            and len(col_max) > 1
            and (sum(col_max) + 24 * (len(col_max) - 1)) > W - 178
        ):
            self._table_mode = False

        # —— 预排版：剥离行尾计时（右对齐成时间列）后折行 ——
        def kind_of(t: str) -> str:
            if t.startswith("※"):
                return "note"
            if t.startswith("◆"):
                return "section"
            if t.startswith("⟦tents⟧"):
                return "tents"
            # ★ 2026-09-27：赏金**档位行**（块首，格式层用 `_BOUNTY_HEAD_PREFIX`
            #   显式标记）—— 赏金卡「块与块之间画线」的判据。用显式标记而不是
            #   内容启发式：同卡上档位/奖励/点位行混排，猜会漏 oracle 与退路路径。
            if t.startswith("▸"):
                return "bounty_head"
            if _NUM_HEAD_RE.match(t):
                return "numbered"
            return "normal"

        rows: list[dict] = []
        for _li, (raw, clean, had_icon) in enumerate(cleaned):
            # 行首全角空格 = 缩进层级。**≥4 个**才额外缩进一级（赏金卡里
            # 「主目标 / 副目标」的分级就靠它）；2 个空格的行（奖励串、
            # 「任务：」标签）保持原样，不然整张卡会整体位移。
            _n_sp = len(raw) - len(raw.lstrip("　"))
            indent_lv = (_n_sp - 2) // 2 if _n_sp >= 4 else 0
            timer_txt = ""
            level_txt = ""
            mlv = _LV_RE.search(clean)  # 赏金行的「｜N-M级」抽出来右对齐
            if mlv:
                level_txt = mlv.group(1)
                # 等级后面若还有「｜标签」就保留分隔符，否则把行尾多余的分隔符一并去掉，
                # 不然会出现「◆ 削弱敌人的据点｜」这种吊着一根竖线的行。
                _tail = clean[mlv.end() :]
                clean = (clean[: mlv.start()] + "｜" + _tail) if _tail else clean[: mlv.start()]
            if not clean.startswith(("※", "◆")):
                mt = _TIMER_RE.search(clean) or _TIMER_ALT_RE.search(clean) or _AGE_RE.search(clean)
                if mt:
                    timer_txt = mt.group(0).strip()
                    clean = (clean[: mt.start()] + clean[mt.end() :]).rstrip(" ··")
            # 折行宽度预留：右侧时间列 + 芯片底框外扩，防止溢出压字
            wrap_w = W - pad * 2 - 56
            if "[" in clean and "]" in clean:
                wrap_w -= 64
            if indent_lv:
                wrap_w -= indent_lv * _INDENT_PX
            if timer_txt:
                wrap_w -= int(_tlen(measure, timer_font, timer_txt) / SS) + 40
            if level_txt:
                wrap_w -= int(_tlen(measure, timer_font, level_txt) / SS) + 46
            # 说明列折行规划命中：指令列原位、说明按预算折成多段，
            # 第 2..N 段作为续行（cells=["", 段]）画在说明列下方。
            if _li in desc_wrap:
                _c = _split_cells(clean)
                if _c and len(_c) == 2:
                    for _j, _seg in enumerate(desc_wrap[_li]):
                        rows.append(
                            {
                                "text": _c[0] if _j == 0 else _seg,
                                "icon": had_icon and _j == 0,
                                "timer": "",
                                "level": "",
                                "kind": "normal",
                                "indent": indent_lv,
                                "cells": [_c[0], _seg] if _j == 0 else ["", _seg],
                            }
                        )
                    continue
            # ※ 注释行画的是 note_font（25px），但折行一直按 body_font（30px）
            # 测量 —— 量出来的宽度比实际大约 20%，注释因此**提前折行**、尾行
            # 只剩几个字（用户 2026-09-17：「排版有点干燥」的一个来源）。
            # 按实际绘制字号测量。
            base_kind = kind_of(clean)
            if base_kind == "tents":
                # 点位机器行：整行原样保留（不折行），块数据存行上；
                # 行高 = 顶距 + 标题 + n×任务行 + 底距（n 取各块任务数最大值）
                _blocks = _tents_of(clean) or []
                _n = max((len(b) - 1 for b in _blocks), default=0)
                rows.append(
                    {
                        "text": "",
                        "icon": False,
                        "timer": "",
                        "level": "",
                        "kind": "tents",
                        "indent": 0,
                        "cells": None,
                        "tents": _blocks,
                        "_extra_h": (
                            _TENT_PAD_TOP + _TENT_HEAD_H + _n * _TENT_LINE_H + _TENT_PAD_BOT
                        ),
                    }
                )
                continue
            _wrap_font = note_font if base_kind == "note" else body_font
            wrapped = self._wrap_cached(clean, _wrap_font, max(300, wrap_w))
            # 分列单元格（见 _split_cells 的说明）。
            # 折过行的文本不能再按原串切列：否则第 0 个折行会重画整行、
            # 后续折行又各画一次。折行行退回普通排版更安全。
            cells_split = _split_cells(clean) if len(wrapped) == 1 else None
            for j, seg in enumerate(wrapped):
                k = base_kind if (j == 0 or base_kind == "note") else kind_of(seg)
                rows.append(
                    {
                        "text": seg,
                        "icon": had_icon and j == 0,
                        "timer": timer_txt if j == 0 else "",
                        "level": level_txt if j == 0 else "",
                        "kind": k,
                        "indent": indent_lv,
                        "cells": cells_split if j == 0 else None,
                    }
                )

        # —— 等级徽章统一宽度 ——
        # 徽章按「宽的那个」统一宽度，"75-80级 / 95-100级 / 115-120级" 三个才能
        # 左缘也齐（只右对齐的话左缘参差，用户反馈「等级做的有些不美观」）。
        lv_box_w = (
            max(
                (_tlen(measure, timer_font, r["level"]) for r in rows if r.get("level")),
                default=0.0,
            )
            + 34 * SS
        )

        # —— 赏金卡：档位分组（每个档位包一个圆角框）★ 2026-09-27 移植外援版 ——
        # 档位头 = 我们的 `bounty_head` 行（格式层 `_BOUNTY_HEAD_PREFIX` 的「▸」判定）；
        # 其后的「任务：/奖励」行归入同组，直到下一档位头、◆ 地区横幅或 ※ 注脚。
        tier_frames: list[tuple[int, int]] = []
        if getattr(self, "_bounty_mode", False) and not self._plain_body:
            _gi = None
            for _i, _r in enumerate(rows):
                if _r["kind"] == "bounty_head":
                    if _gi is not None:
                        tier_frames.append((_gi, _i - 1))
                    _gi = _i
                    _r["_tier_head"] = True
                elif _gi is not None and _r["kind"] in ("section", "note", "tents"):
                    tier_frames.append((_gi, _i - 1))
                    _gi = None
            if _gi is not None:
                tier_frames.append((_gi, len(rows) - 1))
            # 组尾距：本组末行（组与组/组与注脚/卡尾之间留出框间空隙）；
            # 组前一行若是横幅或轮换行也加尾距，避免框顶贴住其文字。
            for _s, _e in tier_frames:
                rows[_e]["_tier_tail"] = _TIER_TAIL
                if _s > 0 and "_tier_tail" not in rows[_s - 1]:
                    rows[_s - 1]["_tier_tail"] = _TIER_TAIL

        row_h = {
            "normal": 46,
            "section": 62,
            "note": 40,
            "numbered": 54,
            "bounty_head": 46,  # 档位行高度同 normal（只是多了块首语义）
            "tents": 0,
        }  # 点位块行高全部走 _extra_h（块内自算）
        # 行高用行上存的 kind（说明列续行的 text 是说明片段，可能恰好以
        # 数字开头被 kind_of 误判成 numbered；存 kind 才是真实排版档位）
        body_h = (
            sum(row_h[r["kind"]] + r.get("_tier_tail", 0) + r.get("_extra_h", 0) for r in rows) + 10
        )
        header_h = 150
        footer_h = 96
        H = header_h + body_h + footer_h + pad + 26

        # —— 画布 / 背景 / 星点 / 拱纹 ——
        # 渐变背景直接作为画布：旧版「新建纯色画布 + 全幅粘贴渐变」的粘贴
        # 是全尺寸覆盖，结果与渐变图本身逐像素相同，省一张全幅 RGB 的分配+拷贝。
        # ★ 2026-09-27：_vgrad 直接产出 RGBA 画布（1×h 列转 RGBA 后 NEAREST
        #   放大，等价性见 _vgrad 文档）—— 不再有旧版「整幅 RGB resize →
        #   整幅 convert(RGBA)」的 60MB 中转 + 全幅转换。
        # ★ 暗角掩码在画布创建**之前**取/算：首渲某尺寸时掩码的 resize +
        #   GaussianBlur 需要 ~60MB 级临时缓冲，与 79MB 画布同时存在会把峰值
        #   顶上去；先算掩码（此时画布尚不存在），两个峰值阶段互相错开。
        #   收尾 _apply_vignette 时是 LRU 命中，零成本。
        self._vignette_mask(W * SS, H * SS)
        img = _vgrad((W * SS, H * SS), [(0.0, BG_TOP), (0.35, BG_MID), (1.0, BG_BOT)])
        # 星点 / 拱纹瓦片化：旧版先画到一张全幅透明层再整幅 alpha_composite，
        # 大卡上要多付「全幅 RGBA 层 + 全幅合成结果」两次分配与一次全幅混合。
        # 装饰彼此按包围盒归组（组间必不交叠），每组一张小瓦片原位合成 ——
        # 与整幅合成逐像素一致（Image.alpha_composite 的 over 运算逐像素独立，
        # 原位/偏移路径已单独验证等价）。孤立星点走预渲染 sprite（见
        # _overlay_tiles 内说明）。
        self._overlay_tiles(img, W * SS, H * SS, accent)
        d = ImageDraw.Draw(img)

        # —— 切角面板外框 ——
        inset = 22
        box = (inset * SS, inset * SS, (W - inset) * SS, (H - inset) * SS)
        _chamfer(d, box, 26 * SS, fill=(14, 18, 28, 214))
        _chamfer(d, box, 26 * SS, outline=GOLD_DIM + (255,), width=2 * SS)
        _chamfer(
            d,
            (box[0] + 5 * SS, box[1] + 5 * SS, box[2] - 5 * SS, box[3] - 5 * SS),
            22 * SS,
            outline=(74, 64, 46, 170),
            width=1 * SS,
        )
        for cx, cy, sx, sy in (
            (box[0], box[1], 1, 1),
            (box[2], box[1], -1, 1),
            (box[0], box[3], 1, -1),
            (box[2], box[3], -1, -1),
        ):
            L = 36 * SS
            d.line([(cx, cy + sy * L), (cx + sx * L, cy)], fill=GOLD_BRIGHT + (255,), width=3 * SS)
            r = 5 * SS
            d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=GOLD_BRIGHT + (255,))

        # —— 头部 ——
        y = box[1] + 44 * SS
        x0 = box[0] + 42 * SS
        d.rectangle([x0, y + 2 * SS, x0 + 4 * SS, y + 66 * SS], fill=accent + (255,))
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
            chip_tw = _tlen(d, cf, chip_text)
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
            if _tlen(d, t_font, title) <= title_right - tx:
                break
        else:
            _avail = title_right - tx
            _cut = title
            while _cut and _tlen(d, t_font, _cut + "…") > _avail:
                _cut = _cut[:-1]
            title = (_cut + "…") if _cut else title
        d.text((tx, y), title, font=t_font, fill=INK + (255,))
        tw = _tlen(d, t_font, title)
        ly = y + 68 * SS
        _hgrad_line(d, tx, tx + max(260 * SS, tw), ly, accent, 210)
        r = 6 * SS
        dx = tx + 14 * SS
        d.polygon(
            [(dx, ly - r), (dx + r, ly), (dx, ly + r), (dx - r, ly)], fill=GOLD_BRIGHT + (255,)
        )
        sx_, sy_ = tx, y + 84 * SS
        for ch in subtitle:
            d.text((sx_, sy_), ch, font=sub_font, fill=GOLD + (225,))
            sx_ += _tlen(d, sub_font, ch) + 4 * SS

        if chip_text:
            # 平台徽章：切角标签 + 菱形标记（与正文 ◆ 记号同一套视觉语言，
            # 不用「点 + 环」——缩小后那一对同心圆会被看成乱码字符）。
            # 底色必须是**预混好的不透明色**：ImageDraw 在 RGBA 上不混合，
            # 收尾 convert("RGB") 又丢 alpha，写 (255,255,255,12) 会渲染成纯白块。
            _chamfer(
                d,
                (ch_x0, chip_y0, ch_x1, chip_y0 + chip_h),
                12 * SS,
                fill=_tint((255, 255, 255), 26),
                outline=accent + (255,),
                width=max(1, SS),
            )
            cy = chip_y0 + chip_h / 2
            mx = ch_x0 + chip_pad_l + chip_mark_r
            d.polygon(
                [
                    (mx, cy - chip_mark_r),
                    (mx + chip_mark_r, cy),
                    (mx, cy + chip_mark_r),
                    (mx - chip_mark_r, cy),
                ],
                fill=accent + (255,),
            )
            d.text(
                (mx + chip_mark_r + chip_gap, cy),
                chip_text,
                font=cf,
                fill=GOLD_BRIGHT + (255,),
                anchor="lm",
            )

        # —— 仲裁表列对齐：按显示宽度计算各列像素起点 ——
        arb_cols = None
        if getattr(self, "_table_mode", False):
            # 只有真正分列的行才参与列宽计算；整行文本（如 ※ 注释、◆ 分组、
            # 单列说明）不能算进第一列，否则会把第二列整体推到卡片外。
            # ※/◆ 行即使含全角空格也按整行绘制（分支里不走 cells），所以
            # 它们的分格宽度也绝不能计入 —— 价格榜的注释行曾把数字列撑开
            split_rows = [
                r["cells"] for r in rows if r.get("cells") and r["kind"] in ("normal", "numbered")
            ]
            ncol = max((len(r) for r in split_rows), default=0)
            measure_f = body_font
            arb_cols = []
            acc = 0
            for ci in range(ncol):
                # ★ 2026-09-27：这里测的单元格与预扫描 col_max 是同一批字符串
                # （同字体同文本），_tlen 记忆化后整段是 dict 命中。
                wpx = max(
                    (measure_f and _tlen(d, measure_f, r[ci]) or 0)
                    for r in split_rows
                    if len(r) > ci
                )
                arb_cols.append(acc)
                acc += wpx + 24 * SS
            if not split_rows:  # 没有任何分列行时退回普通排版
                arb_cols = None

        # —— 正文 ——
        y = box[1] + header_h * SS
        x_badge = box[0] + 40 * SS
        x_bullet = box[0] + 54 * SS
        x_text = box[0] + 82 * SS
        x_num_text = box[0] + 96 * SS
        x_right = box[2] - 52 * SS

        # —— 赏金档位框：先于文字绘制（底色 + 圆角描边），文字与徽章叠在其上 ——
        # 行 y 坐标必须与下面正文循环的推进**完全一致**（行高含组尾距）。
        if tier_frames:
            _row_y = []
            _yy = y
            for _r in rows:
                _row_y.append(_yy)
                _yy += (row_h[_r["kind"]] + _r.get("_tier_tail", 0) + _r.get("_extra_h", 0)) * SS
            for _s, _e in tier_frames:
                _fy1 = _row_y[_e] + row_h[rows[_e]["kind"]] * SS + _TIER_PAD * SS
                _fr = (
                    x_text - _TIER_FRAME_L * SS,
                    _row_y[_s] - _TIER_PAD * SS,
                    box[2] - _TIER_FRAME_R * SS,
                    _fy1,
                )
                _rad = min(10 * SS, (_fy1 - _row_y[_s] + _TIER_PAD * SS) // 2)
                d.rounded_rectangle(_fr, radius=_rad, fill=TIER_FRAME_FILL)
                d.rounded_rectangle(_fr, radius=_rad, outline=TIER_FRAME_LINE, width=1 * SS)

        for i, r_ in enumerate(rows):
            t = r_["text"]
            k = r_["kind"]
            h = (row_h[k] + r_.get("_tier_tail", 0) + r_.get("_extra_h", 0)) * SS
            # 地区行 = ◆ 开头且不含「｜」（赏金行才含）；做横幅化处理
            is_board = getattr(self, "_bounty_mode", False) and k == "section" and "｜" not in t
            if k == "section":
                if is_board:
                    d.polygon(
                        self._diamond(x_bullet - 4 * SS, y + h / 2, 10 * SS), fill=accent + (255,)
                    )
                    self._draw_tokens(
                        d,
                        t[1:].strip(),
                        x_text - 8 * SS,
                        y + 6 * SS,
                        f.get("bold", 34),
                        accent,
                        x_right,
                    )
                else:
                    d.polygon(
                        self._diamond(x_bullet - 4 * SS, y + h / 2, 9 * SS), fill=accent + (255,)
                    )
                    self._draw_tokens(
                        d,
                        t[1:].strip(),
                        x_text - 8 * SS,
                        y + 8 * SS,
                        f.get("bold", 31),
                        accent,
                        x_right,
                    )
            elif k == "note":
                self._draw_tokens(
                    d, t.lstrip("※").strip(), x_text, y + 8 * SS, note_font, INK_FAINT, x_right
                )
            elif k == "tents":
                # 与档位框同宽（左缘外扩 16 / 右缘内缩 46），三块与上面对齐
                self._draw_tent_blocks(
                    d,
                    r_.get("tents") or [],
                    x_text - _TIER_FRAME_L * SS,
                    y,
                    h,
                    (box[2] - _TIER_FRAME_R * SS) - (x_text - _TIER_FRAME_L * SS),
                    body_font,
                    f.get("bold", 29),
                    accent,
                )
            elif k == "numbered":
                num, rest = t.split(".", 1)
                bh = 38 * SS
                badge = (x_badge, y + (h - bh) / 2, x_badge + bh, y + (h + bh) / 2)
                _chamfer(
                    d,
                    badge,
                    9 * SS,
                    fill=_tint((255, 255, 255), 22),
                    outline=accent + (215,),
                    width=1 * SS,
                )
                nb = f.get("bold", 23)
                cx_b = (badge[0] + badge[2]) / 2
                cy_b = (badge[1] + badge[3]) / 2
                bb = d.textbbox((0, 0), num, font=nb, anchor="la")
                d.text(
                    (cx_b - (bb[0] + bb[2]) / 2, cy_b - (bb[1] + bb[3]) / 2),
                    num,
                    font=nb,
                    fill=GOLD_BRIGHT + (255,),
                )
                self._draw_tokens(d, rest.strip(), x_num_text, y + 8 * SS, body_font, INK, x_right)
            else:
                # 缩进：由行首全角空格数决定（见上面的 indent_lv），
                # 赏金卡用它把「副目标 / 附加条件」压到主目标下一级。
                xt = x_text + r_.get("indent", 0) * _INDENT_PX * SS
                if r_["icon"]:
                    d.polygon(self._diamond(x_bullet, y + h / 2, 7 * SS), fill=accent + (235,))
                elif r_.pop("_tier_head", False):
                    # 赏金档位名：框已承担分组视觉，行首标记（▸/·）**不画圆点**，
                    # 只把标记剥掉（用户 2026-09-26 口径：加框后左边的点可以去掉）
                    t = t.lstrip("　")[1:].strip()
                elif t.startswith(("·", "▸")):
                    # 「▸」是赏金档位行的块首标记（非档位行时才画圆点）
                    t = t[1:].strip()
                    d.ellipse(
                        [
                            x_bullet - 3 * SS,
                            y + h / 2 - 3 * SS,
                            x_bullet + 3 * SS,
                            y + h / 2 + 3 * SS,
                        ],
                        fill=accent + (205,),
                    )
                # 行内染段标记解析（⟦c⟧蓝 / ⟦v⟧紫 / ⟦w⟧白，见 _parse_color_segs）：
                # 主文本走下面的正常路径，染段在行尾依序补画
                t, _tail_segs = _parse_color_segs(t)
                # 赏金档位行：把行首任务类型单独用类型色画，再接着画后面的
                # 赏金名 / 等级标签（等级稍后由右对齐徽章覆盖）
                if self._bounty_mode and r_.get("level") and not r_["icon"]:
                    _mt, _rest = _split_leading_type(t)
                    if _mt:
                        d.text(
                            (xt, y + 8 * SS), _mt, font=body_font, fill=MISSION_TYPE_COLOR + (255,)
                        )
                        # 「刺杀：H-09 坦克」这类已自带全角冒号的，不再补空格
                        _gap = "" if _mt.endswith("：") else " "
                        xt += _tlen(d, body_font, _mt) + _tlen(d, body_font, _gap)
                        t = _rest
                # “标签：值” 结构 -> 标签加粗，值常规；
                # 但分列行一律让位给列对齐（见 _is_label_row）。
                head, sep, rest = t.partition("：")
                _x_end = None
                _tail_y = y + 9 * SS
                if _is_label_row(head, bool(sep), bool(arb_cols is not None and r_.get("cells"))):
                    bold_font = f.get("bold", 29)
                    # 标签本身就是任务类型时（「刺杀：H-09 坦克」六人组节点名）
                    # 用任务类型色，别让类型看起来像正文标签
                    head_col = MISSION_TYPE_COLOR if head in _MISSION_TYPE_SET else INK
                    d.text((xt, y + 8 * SS), head + "：", font=bold_font, fill=head_col + (255,))
                    adv = _tlen(d, bold_font, head + "：")
                    if self._bounty_mode and head == "任务":
                        self._draw_task_rest(d, rest, xt + adv, y + 9 * SS, body_font, x_right)
                    else:
                        _x_end = self._draw_tokens(
                            d, rest, xt + adv, y + 9 * SS, body_font, INK, x_right
                        )
                else:
                    if arb_cols is not None and r_.get("cells"):
                        for ci, cell in enumerate(r_["cells"]):
                            if ci >= len(arb_cols):
                                break
                            cx = xt + arb_cols[ci]
                            if self._plain_body:
                                # 语法文档类：指令/说明双色直排，宽度与列宽
                                # 测量严格一致（色不改变字宽，不会破坏列对齐）
                                self._draw_plain_cell(d, ci, cell, cx, y + 8 * SS, body_font)
                            else:
                                self._draw_tokens(d, cell, cx, y + 8 * SS, body_font, INK, x_right)
                    else:
                        _cells = r_.get("cells")
                        if (
                            descent_col is not None
                            and _cells
                            and len(_cells) == 3
                            and _DESC_HEAD_RE.match(_cells[0])
                        ):
                            # 沉沦之地三列：[层] / 任务类型(青) / 目标
                            self._draw_tokens(d, _cells[0], xt, y + 8 * SS, body_font, INK, x_right)
                            self._draw_tokens(
                                d,
                                _cells[1],
                                xt + descent_col[0],
                                y + 8 * SS,
                                body_font,
                                MISSION_TYPE_COLOR,
                                x_right,
                            )
                            self._draw_tokens(
                                d,
                                _cells[2],
                                xt + descent_col[0] + descent_col[1],
                                y + 8 * SS,
                                body_font,
                                INK,
                                x_right,
                            )
                        elif (
                            pair_col is not None
                            and _cells
                            and len(_cells) == 2
                            and _PAIR_RE.match(_cells[1])
                        ):
                            # 元素/加成：按公共列绘制（见 pair_col 的说明）
                            self._draw_tokens(d, _cells[0], xt, y + 8 * SS, body_font, INK, x_right)
                            self._draw_tokens(
                                d, _cells[1], xt + pair_col, y + 8 * SS, body_font, INK, x_right
                            )
                        else:
                            _x_end = self._draw_tokens(
                                d, t, xt, y + 8 * SS, body_font, INK, x_right
                            )
                            _tail_y = y + 8 * SS
                if _tail_segs and _x_end is not None:
                    # 尾段（挑战名蓝段 / 挑战名+目标白段）：依序补画在行尾
                    for _seg_txt, _seg_col in _tail_segs:
                        d.text((_x_end, _tail_y), _seg_txt, font=body_font, fill=_seg_col + (255,))
                        _x_end += _tlen(d, body_font, _seg_txt)
            # 右对齐等级列（赏金卡）：钢蓝色徽章，整列扫读。
            # 徽章宽度**全卡统一**（lv_box_w），文字居中 —— 早先按各自文本宽画，
            # 「75-80级 / 95-100级 / 115-120级」左缘参差，看着像没对齐。
            lv = r_.get("level")
            if lv and not r_.get("timer"):
                tw = _tlen(d, timer_font, lv)
                bw = max(lv_box_w, tw + 28 * SS)
                bx0 = x_right - bw
                dh = 40 * SS
                _chamfer(
                    d,
                    (bx0, y + (h - dh) / 2, x_right, y + (h + dh) / 2),
                    9 * SS,
                    fill=_tint(STEEL, 34),
                    outline=_tint(STEEL, 150),
                    width=1 * SS,
                )
                # 垂直居中：用 textbbox 的实际上/下伸部算基线位置。
                # 之前写死 `y + 9*SS`，数字与「级」的字形高度不同 → 看着上飘
                # （用户反馈「左右居中但是上下没居中」）。
                bb = d.textbbox((0, 0), lv, font=timer_font, anchor="la")
                ty = (y + (h - dh) / 2) + (dh - (bb[3] - bb[1])) / 2 - bb[1]
                d.text((bx0 + (bw - tw) / 2, ty), lv, font=timer_font, fill=STEEL + (255,))
            # 右对齐时间列（琥珀主时长 + 暗金括号补充）
            if r_.get("timer"):
                tv = r_["timer"]
                main_txt, paren = tv, ""
                mp = _TIMER_PAREN_RE.search(tv)
                if mp:
                    main_txt, paren = tv[: mp.start()], tv[mp.start() :]
                tw_main = _tlen(d, timer_font, main_txt)
                tw_par = _tlen(d, note_font, paren) if paren else 0
                tx0 = x_right - tw_main - (tw_par + 8 * SS if paren else 0)
                d.text((tx0, y + 9 * SS), main_txt, font=timer_font, fill=AMBER + (255,))
                if paren:
                    d.text(
                        (tx0 + tw_main + 8 * SS, y + 10 * SS),
                        paren,
                        font=note_font,
                        fill=(170, 148, 106, 255),
                    )
            y += h
            if i < len(rows) - 1:
                nxt = rows[i + 1]
                if getattr(self, "_bounty_mode", False):
                    # 赏金卡：地区行下方画主题色细线（层级标识，保留）；
                    # 其余在「新赏金块」前分隔 —— 让「赏金名 + 其奖励」成为
                    # 视觉上的一组。★ 2026-09-27：判据加上 `bounty_head`
                    # （档位行是 normal 行，旧判据只在下一行是 ◆/数字行时才画，
                    # 于是**块与块之间一条线都没有**）。
                    if is_board:
                        _hgrad_line(d, box[0] + 46 * SS, box[2] - 46 * SS, y, accent, 115)
                    # ★ 2026-09-27：赏金块首改用**圆角框**（见正文开头的预扫绘制），
                    #   这里不再为 bounty_head 画淡线；◆/数字行仍保留细线。
                    elif nxt["kind"] in ("section", "numbered"):
                        d.line(
                            [(box[0] + 46 * SS, y), (box[2] - 46 * SS, y)],
                            fill=(255, 255, 255, 9),
                            width=1 * SS,
                        )
                else:
                    d.line(
                        [(box[0] + 46 * SS, y), (box[2] - 46 * SS, y)],
                        fill=(255, 255, 255, 9),
                        width=1 * SS,
                    )

        # —— 底部 ——
        fy = box[3] - (footer_h - 6) * SS
        _hgrad_line(d, box[0] + 46 * SS, box[2] - 46 * SS, fy, GOLD, 130)
        mark = WATERMARK
        mx = box[2] - 46 * SS
        for ch in reversed(mark):
            cw = _tlen(d, sub_font, ch)
            mx -= cw
            d.text((mx, fy + 18 * SS), ch, font=sub_font, fill=GOLD_FAINT + (255,))
            mx -= 4 * SS
        ftxt = " · ".join(foot_notes)
        if ftxt:
            d.text((box[0] + 46 * SS, fy + 18 * SS), ftxt, font=sub_font, fill=INK_FAINT + (220,))

        # —— 暗角 + 缩小 + 保存 ——
        # ★ 2026-09-27 内存关键点：ImageDraw 对象内部持有整幅画布的强引用
        #   （d._image / d.im）。旧版 `img = None` 之后 draw 对象仍活着，
        #   全尺寸画布要等到函数返回才释放 —— 编码阶段（_resize/convert/save）
        #   峰值工作集凭空多扛 ~80MB。这里在进入重内存收尾前先断开画笔。
        d = None
        # 用 BOX（面积平均）而非 LANCZOS：后者在高对比文字/描边边缘会产生
        # 振铃，缩小后表现为彩边与发虚。
        #
        # ★ 2026-09-27 内存关键点：SS=2 ⇒ 缩小率恰为 2（整数），BOX 核的每个
        #   目标行只依赖**恰好 2 个源行**（水平同理、各通道独立），因此可以
        #   按目标行带切分处理，与整幅一次 resize 逐字节等价（随机数据对拍）。
        #   整幅一次 resize 时 Pillow 两遍重采样要分配 ~100MB 级中间缓冲
        #   （90 行大卡实测峰值 +105MB、全局峰值的主犯）；分带后每带瞬时
        #   缓冲只有几 MB。resize 结果（small）仍需整体持有，但画布在分带
        #   结束后立即释放。
        self._apply_vignette(img, W * SS, H * SS)
        _rs = getattr(Image, "Resampling", Image)
        sw, sh = img.size
        if sw == W * SS and sh == H * SS:
            # ★ 2026-09-27 内存关键点：SS=2 ⇒ 缩小率恰为 2（整数），BOX 核的
            #   每个目标行只依赖**恰好 2 个源行**（水平同理、各通道独立），
            #   因此按目标行带切分缩小 + 分带转 RGB，与「整幅 resize(RGBA) →
            #   整幅 convert(RGB)」逐字节等价（随机数据对拍）。整幅一次
            #   resize 时 Pillow 两遍重采样要分配 ~100MB 级中间缓冲（90 行
            #   大卡实测峰值 +105MB、全局峰值主犯）；分带后每带瞬时缓冲只有
            #   几 MB，且成品直接是 RGB，省掉整幅 RGBA 中间图（再 -18MB）。
            final = Image.new("RGB", (W, H))
            _band = 512  # 每带目标 512 行 ⇒ 源带 1024 行，crop ~9MB
            for y0 in range(0, H, _band):
                y1 = min(y0 + _band, H)
                band = (
                    img.crop((0, y0 * SS, sw, y1 * SS)).resize((W, y1 - y0), _rs.BOX).convert("RGB")
                )
                final.paste(band, (0, y0))
                band = None  # 每带用完立即释放，瞬时缓冲不叠加
            img = None  # 全尺寸画布释放后才能进入编码阶段
        else:  # pragma: no cover —— 尺寸异常时退回整幅路径
            small = img.resize((W, H), _rs.BOX)
            img = None
            final = small.convert("RGB")
            small = None
        out = self.cache_dir / f"card_{random.randrange(1 << 40):010x}.png"
        # optimize=True 在容器里要 ~480ms（剖析结论）；compress_level=1
        # 同样无损、编码快 3~4 倍，代价只是文件略大（QQ 上传无感）
        final.save(out, "PNG", optimize=False, compress_level=1)
        final = None  # 编码完即释放最后一张全幅图
        self._cleanup(keep=2)
        _malloc_trim()
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

    def _wrap_cached(self, text: str, font, max_w_px: int) -> list[str]:
        """_wrap 的**按渲染**结果缓存：同一 (字体, 文本, 宽度) 只折一次。

        说明列规划（_plan_desc_col_wrap）与正文行构造会对同一段说明以同一
        预算各折一次 —— 90 行大卡上等于白付 90 次逐字折行。缓存字典在
        _render 开头重建（self._wrap_cache = {}），不跨渲染驻留、无全局
        强引用（热重载安全）。
        """
        cache = self._wrap_cache
        key = (id(font), text, max_w_px)
        segs = cache.get(key)
        if segs is None:
            segs = self._wrap(text, font, max_w_px)
            if len(cache) >= 4096:
                cache.clear()
            cache[key] = segs
        return segs

    @staticmethod
    def _wrap(text: str, font, max_w_px: int) -> list[str]:
        if not text:
            return [""]
        # 行内自带换行符时先按 \n 拆开，否则下面的 textlength 会抛
        # "can't measure length of multiline text"（PIL 拒绝测多行串）。
        if "\n" in text:
            return [
                seg
                for parts in text.split("\n")
                for seg in ImageRenderer._wrap(parts, font, max_w_px)
            ]
        max_w = max_w_px * SS
        d = _measure_draw()
        # 极性图标标记：测量时按图标实际宽度计入（剥离标记字符串本身），
        # 否则「⟦pol:vazarin⟧」这 13 个字符会把长行提前挤折。
        mark_px = _pol_mark_px(font) if "⟦pol:" in text else 0.0
        # 染段标记（⟦c⟧/⟦v⟧/⟦w⟧ 及闭合）从不绘制 → 测量按 0 宽，
        # 否则 6 个标记 ≈ 200px 的虚宽会把档位行提前挤折（2026-10-02 实测）。
        has_seg = _SEG_MARK_RE.search(text) is not None

        # ------------------------------------------------------------------
        # 增量测量快速路径（仅基本布局）：
        # 旧版每加一个字就 textlength(整行前缀) 一次 —— 每行 O(n²) 次测量，
        # 长说明列的大卡上（90 行 × 50 字）仅折行规划就上万次 C 调用。
        # 基本布局下 textlength(s) = Σ textlength(c)（逐字步进纯加和，已对
        # 全部用例语料做过 行级+前缀级 6400+ 次校验零偏差），因此维护累计宽
        # w_line 即可做出**完全相同**的折行判定，降为 O(n) 次查表。
        # 极性标记的处理与旧版口径逐位一致：标记内部字符按普通字形宽度
        # 累计，闭合「⟧」到位的一步换成 mark_px（等价于旧版剥离后重测）。
        # Raqm 整形布局（可能带字距调整）不走此路径，退回原版逐次测量。
        # ------------------------------------------------------------------
        incremental = (
            _LAYOUT_BASIC is not None and getattr(font, "layout_engine", None) == _LAYOUT_BASIC
        )
        out: list[str] = []
        line = ""
        w_line = 0.0
        if incremental:
            adv_cache = getattr(font, "_wf_adv", None)
            if adv_cache is None:
                adv_cache = {}
                try:
                    font._wf_adv = adv_cache
                except AttributeError:  # pragma: no cover
                    incremental = False
            # ★ 2026-09-27 快速路径：整行放得下就不进逐字循环（旧版即使整行
            #   放得下也要 O(n) 逐字推进 + O(n²) 字符串拼接；90 行大卡的说明
            #   列几乎全部命中本路径）。仅限基本布局且**无极性标记**：此时
            #   textlength 是逐字步进的纯加和、累计宽单调不减，
            #   「整行宽 ≤ max_w」⟺「逐字路径不触发断行」，结果严格等价；
            #   带极性标记的行在标记闭合处宽度非单调，仍走原逐字路径。
            if not mark_px and not has_seg and _tlen(d, font, text) <= max_w:
                seg = text.rstrip(" ·、")
                return [seg] if seg else []

        def _adv(ch: str) -> float:
            a = adv_cache.get(ch)
            if a is None:
                a = d.textlength(ch, font=font)
                adv_cache[ch] = a
            return a

        def _sum_adv(s: str) -> float:
            """与旧版 _mw 同口径的精确串宽：完整标记按各自目标宽、其余逐字。"""
            w = 0.0
            k = 0
            while k < len(s):
                m2 = _POL_MARK_RE.match(s, k) if mark_px else None
                if m2:
                    w += mark_px
                    k = m2.end()
                    continue
                m3 = _SEG_MARK_RE.match(s, k) if has_seg else None
                if m3:
                    k = m3.end()  # 染段标记零宽
                    continue
                w += _adv(s[k])
                k += 1
            return w

        if incremental:
            # 预扫描标记：记录闭合位与该标记内字符的步进和 + 闭合处的目标宽
            # （极性标记 = 图标宽；染段标记 = 0）。闭合时按「减字符和 + 加目标宽」冲账
            span_adv: dict[tuple[int, int], float] = {}
            span_end_at: dict[int, tuple[int, int]] = {}
            span_target: dict[tuple[int, int], float] = {}
            if mark_px:
                for m2 in _POL_MARK_RE.finditer(text):
                    s0, e0 = m2.span()
                    span_end_at[e0 - 1] = (s0, e0)
                    span_adv[(s0, e0)] = sum(_adv(c) for c in m2.group(0))
                    span_target[(s0, e0)] = mark_px
            if has_seg:
                for m2 in _SEG_MARK_RE.finditer(text):
                    s0, e0 = m2.span()
                    span_end_at[e0 - 1] = (s0, e0)
                    span_adv[(s0, e0)] = sum(_adv(c) for c in m2.group(0))
                    span_target[(s0, e0)] = 0.0
            i = 0
            while i < len(text):
                ch = text[i]
                w_try = w_line + _adv(ch)
                comp = span_end_at.get(i)
                if comp is not None:  # 标记闭合：换成目标宽（与旧版等值）
                    w_try = w_try - span_adv[comp] + span_target[comp]
                if line and w_try > max_w:
                    # 优先在中文分隔符后断开：不这样，「▣Xaku机体蓝图、★雷射瞄具」
                    # 这类用「、」连起来的奖励串会被从词中间劈开。
                    sep = max((line.rfind(c) for c in "、·；，～"), default=-1)
                    if sep > len(line) * 0.5:
                        out.append(line[: sep + 1])
                        line = line[sep + 1 :] + ch
                    # 英文单词断在一半时，回退到最近的空格处换行
                    elif (
                        ch.isascii() and ch.isalnum() and line[-1].isascii() and line[-1].isalnum()
                    ):
                        sp = line.rfind(" ")
                        if sp > len(line) * 0.5:
                            out.append(line[:sp])
                            line = line[sp + 1 :] + ch
                        else:
                            out.append(line)
                            line = ch
                    else:
                        out.append(line)
                        line = ch
                    w_line = _sum_adv(line)
                else:
                    w_line = w_try
                    line += ch
                i += 1
        else:

            def _mw(s: str) -> float:
                if has_seg:
                    s = _SEG_MARK_RE.sub("", s)
                if not mark_px:
                    return d.textlength(s, font=font)
                n = s.count("⟦pol:")
                return d.textlength(_POL_MARK_RE.sub("", s), font=font) + n * mark_px

            for ch in text:
                if line and _mw(line + ch) > max_w:
                    # 优先在中文分隔符后断开：不这样，「▣Xaku机体蓝图、★雷射瞄具」这类
                    # 用「、」连起来的奖励串会被从词中间劈开（「▣X」/「aku机体蓝图」）。
                    sep = max((line.rfind(c) for c in "、·；，～"), default=-1)
                    if sep > len(line) * 0.5:
                        out.append(line[: sep + 1])
                        line = line[sep + 1 :] + ch
                        continue
                    # 英文单词断在一半时，回退到最近的空格处换行
                    if ch.isascii() and ch.isalnum() and line[-1].isascii() and line[-1].isalnum():
                        sp = line.rfind(" ")
                        if sp > len(line) * 0.5:
                            out.append(line[:sp])
                            line = line[sp + 1 :] + ch
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

    def _draw_tent_blocks(
        self,
        d,
        blocks: list[list[str]],
        x0: float,
        y: float,
        h: float,
        avail_w: float,
        font,
        head_font,
        accent,
    ) -> None:
        """赏金卡底部的点位三块（小帐篷 A/B/C）：等宽圆角框横向排开。

        每块顶部是点位标题（主题色加粗、居中），下方纵排任务名（居中）：
        命中 :data:`_TENT_BLUE_TASKS` 的染蓝（用户指定的 4 条高亮任务），
        其余正文色。框配色沿用赏金档位框（TIER_FRAME_FILL/LINE），
        不引入新色系。
        """
        if not blocks:
            return
        gap = _TENT_GAP * SS
        nb = len(blocks)
        bw = (avail_w - gap * (nb - 1)) / nb
        for bi, parts in enumerate(blocks):
            bx = x0 + bi * (bw + gap)
            d.rounded_rectangle(
                [bx, y, bx + bw, y + h],
                radius=10 * SS,
                fill=TIER_FRAME_FILL,
                outline=TIER_FRAME_LINE,
                width=1 * SS,
            )
            head = parts[0]
            _hb = d.textbbox((0, 0), head, font=head_font, anchor="la")
            d.text(
                (bx + (bw - (_hb[2] - _hb[0])) / 2 - _hb[0], y + _TENT_PAD_TOP * SS - _hb[1]),
                head,
                font=head_font,
                fill=accent + (255,),
            )
            ty = y + (_TENT_PAD_TOP + _TENT_HEAD_H) * SS
            for name in parts[1:]:
                color = BLUE if _tent_norm(name) in _TENT_BLUE_TASKS else INK
                _tb = d.textbbox((0, 0), name, font=font, anchor="la")
                d.text(
                    (bx + (bw - (_tb[2] - _tb[0])) / 2 - _tb[0], ty - _tb[1]),
                    name,
                    font=font,
                    fill=color + (255,),
                )
                ty += _TENT_LINE_H * SS

    def _draw_task_rest(self, d, rest: str, x: float, y: float, font, max_w: float) -> None:
        """赏金「任务：**类型** **挑战名**」两段上色。

        用户要求任务类型与挑战名各自有颜色（「任务类型现在像和后面文字是一块的」）。
        ★ 目标描述段已于 2026-10-02 整体移除（用户反馈「任务描述没任何作用」），
        DE 三地区的纯描述行同时下线 —— 本行只剩类型 / 挑战名两段。

        结构由 ``formatters._oracle_task_lines`` 保证：**前两段不含 ASCII 空格**
        （名字里的空格被换成 NBSP），所以按空格切＝类型 / 挑战名。
        """
        parts = rest.split(" ", 2)
        segs: list[tuple[str, tuple[int, int, int]]] = []
        if parts and parts[0]:
            segs.append((parts[0], MISSION_TYPE_COLOR))
        if len(parts) > 1 and parts[1]:
            segs.append((parts[1], CHALLENGE_COLOR))
        gap = _tlen(d, font, " ")  # ★ 记忆化：空格宽度全卡只算一次
        for i, (text, color) in enumerate(segs):
            d.text((x, y), text, font=font, fill=color + (255,))
            x += _tlen(d, font, text)
            if i < len(segs) - 1:
                x += gap

    # 语法文档类卡片（指令一览等 _plain_body）的配色：
    # 指令列亮色 + 说明列次色，合并词条「A / B / C」逐个异色（2026-09-14 用户要求
    # 「介绍和指令别混在一起」「本质三个指令的用三个颜色」）。
    _HELP_CMD_COLORS = (GOLD_BRIGHT, CYAN, VIOLET, GREEN)
    # 说明列专用暗色：比 INK_DIM 再压一档（2026-09-14 用户反馈「指令和介绍
    # 颜色太接近，不放大容易混在一起」——金色指令列 vs 暗灰说明列才够分明）。
    HELP_DESC_COLOR = (118, 127, 143)

    def _draw_plain_cell(self, d, ci: int, cell: str, cx: float, y: float, font) -> None:
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
            x += _tlen(d, font, p)  # ★ 记忆化：指令片段/分隔符重复测量归零
            if i < len(parts) - 1:
                d.text((x, y), sep, font=font, fill=GOLD_DIM + (255,))
                x += _tlen(d, font, sep)

    def _draw_tokens(self, d, text: str, x: float, y: float, font, base_color, max_w: float):
        """按语义 token 上色绘制（行内高亮）+ 行内极性图标。

        文本含 ``⟦pol:<key>⟧`` 标记时，就地画小图标再继续画后面的文本；
        无标记时与原实现完全一致（零行为变化）。返回绘制结束的 x。
        """
        if text and "⟦pol:" in text:
            for i, part in enumerate(_POL_MARK_RE.split(text)):
                if i % 2 == 0:
                    if part:
                        x = self._draw_tokens_text(d, part, x, y, font, base_color, max_w)
                else:
                    x += self._draw_polarity_icon(d, part, x, y, font)
            return x
        return self._draw_tokens_text(d, text, x, y, font, base_color, max_w)

    # 缩放后的极性图标缓存：(key, size) -> RGBA 小图（resize 结果只读可复用）
    _ICON_SCALED: dict = {}

    def _draw_polarity_icon(self, d, key: str, x: float, y: float, font) -> float:
        """画一个极性小图标，返回推进宽度；图标缺失返回 0（文本照常）。"""
        img = _polarity_icon(key)
        if img is None or not img.width:
            return 0.0
        size = int(getattr(font, "size", 30) * 1.1)
        size = max(12, min(size, 96))
        w = max(1, round(img.width * size / img.height))
        ck = (key, size)
        small = ImageRenderer._ICON_SCALED.get(ck)
        if small is None:
            small = img.resize((w, size), Image.LANCZOS)
            if len(ImageRenderer._ICON_SCALED) > 64:
                ImageRenderer._ICON_SCALED.clear()
            ImageRenderer._ICON_SCALED[ck] = small
        canvas = getattr(d, "_image", None)  # 出图画布（RGBA）
        if canvas is None:  # pragma: no cover - 兜底
            return 0.0
        canvas.alpha_composite(small, (int(x), int(y - 2 * SS)))
        return w + 7 * SS

    def _draw_tokens_text(self, d, text: str, x: float, y: float, font, base_color, max_w: float):
        """按语义 token 上色绘制（行内高亮）。超过 3 个方括号 token 的行降级为
        纯色词条（避免芯片墙溢出）；芯片超出右缘时同样回退纯文本。"""
        plain_body = getattr(self, "_plain_body", False)
        brackets = len(re.findall(r"\[[^\]]*\]", text))
        chip_ok = brackets <= 3
        pos = 0
        for m in _TOKEN_RE.finditer(text):
            if m.start() > pos:
                seg = text[pos : m.start()]
                d.text((x, y), seg, font=font, fill=base_color + (255,))
                x += _tlen(d, font, seg)
            tok = m.group(0)
            # 上色判定走模块级记忆化（_token_style，与旧版逐条试规则等价）：
            # has_inner / chip_capable / chip_color / special_color
            has_inner, chip_capable, chip_color, special = _token_style(tok)
            color, chip = base_color, False
            if has_inner:
                if not plain_body and chip_ok and chip_capable:
                    # 只有真正的遗物等级 / 物品大类才做词条芯片，
                    # 语法文档里的 [...] 保持朴素
                    color, chip = chip_color, True
            elif not plain_body:
                # 语义配色优先（任务类型 / 元素 / 派系 / 加成%）——
                # 用集合直查，不走正则表，避免和评级、平台等 token 抢匹配。
                # 赏金卡例外：Grineer/Corpus 保持正文色（见 _BOUNTY_PLAIN_FACTION）。
                if special is not None and not (
                    getattr(self, "_bounty_mode", False) and tok in _BOUNTY_PLAIN_FACTION
                ):
                    color = special
            wch = _tlen(d, font, tok)  # ★ 记忆化：高频 token（生存/剩余…）只测一次
            if chip:
                gap, pad, bh = 7 * SS, 10 * SS, 40 * SS
                if pos > 0:
                    x += gap
                if x + wch + pad * 2 > max_w:  # 越界则退回纯文字
                    chip = False
                    x -= gap if pos > 0 else 0
            if chip:
                cb = (int(x - pad), int(y - 3 * SS), int(x + wch + pad), int(y - 3 * SS + bh))
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
            tail = text[pos:]
            d.text((x, y), tail, font=font, fill=base_color + (255,))
            x += _tlen(d, font, tail)
        return x

    @staticmethod
    def _diamond(cx: float, cy: float, r: float):
        return [(cx, cy - r), (cx + r, cy), (cx, cy + r), (cx - r, cy)]

    # ------------------------------------------------------------------
    # 背景装饰：星点 + 右上角 Orokin 拱环暗纹
    #
    # 旧版：先画到一张**全幅**透明层，再整幅 alpha_composite —— 大卡上
    # 要多付「全幅 RGBA 层 + 全幅合成结果」两次分配和一次全幅混合。
    # over 运算逐像素独立，因此可以按包围盒把互相重叠的装饰归为一组
    # （组与组之间像素必不相交），每组画一张小瓦片、原位合成贴回：
    # 逐像素结果与整幅合成完全一致（等价性已单独验证，见 bench_compare.py）。
    # rng 消耗顺序与旧版 _stars 完全一致，星点布局不变。
    # ------------------------------------------------------------------
    _STARS_N = 110

    def _overlay_tiles(self, img, w: int, h: int, accent_color) -> None:
        ops: list[tuple] = []
        rng = random.Random(20260909)
        for _ in range(self._STARS_N):
            x, y = rng.randrange(w), rng.randrange(int(h * 0.85))
            a = rng.randrange(16, 78)
            r = rng.choice((1, 1, 1, 2)) * SS
            if rng.random() < 0.18:
                color = GOLD_BRIGHT + (a,)
            else:
                color = (200, 214, 235, a)
            ops.append(("e", (x - r, y - r, x + r, y + r), color))
        cx, cy = int(w * 0.92), int(w * 0.085)
        for i, rr in enumerate((150, 118, 86, 54)):
            a = 26 - i * 5
            col = (accent_color if i % 2 else GOLD) + (a,)
            ops.append(
                ("a", (cx - rr * SS, cy - rr * SS, cx + rr * SS, cy + rr * SS), 200, 520, col, 3)
            )

        # 包围盒（右/下 +1 化为半开区间：贴边也算重叠，保证组间不交叠）
        boxes = [(op[1][0], op[1][1], op[1][2] + 1, op[1][3] + 1) for op in ops]
        parent = list(range(len(ops)))

        def find(i: int) -> int:
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        for i in range(len(ops)):
            bi = boxes[i]
            for j in range(i + 1, len(ops)):
                bj = boxes[j]
                if not (bi[0] >= bj[2] or bj[0] >= bi[2] or bi[1] >= bj[3] or bj[1] >= bi[3]):
                    ri, rj = find(i), find(j)
                    if ri != rj:
                        parent[rj] = ri

        groups: dict[int, list[int]] = {}
        for i in range(len(ops)):  # 按全局顺序收集 → 组内顺序保持
            groups.setdefault(find(i), []).append(i)

        for members in groups.values():
            # ★ 2026-09-27：孤立星点（绝大多数）走预渲染 sprite —— 形状只取决于
            #   (半径, 颜色)，见 _star_sprite。合成仍走 alpha_composite（画布
            #   上还有 alpha=214 的面板像素，必须保持 over 归一化语义），
            #   贴边裁切按原瓦片几何裁 sprite，逐像素等价。
            if len(members) == 1 and ops[members[0]][0] == "e":
                op = ops[members[0]]
                b = op[1]
                gx0, gy0 = max(0, b[0]), max(0, b[1])
                gx1, gy1 = min(w, b[2] + 1), min(h, b[3] + 1)
                if gx1 > gx0 and gy1 > gy0:
                    sp = _star_sprite((b[2] - b[0]) // 2, op[2])
                    if (gx1 - gx0) == sp.width and (gy1 - gy0) == sp.height:
                        img.alpha_composite(sp, (gx0, gy0))
                    else:
                        img.alpha_composite(
                            sp.crop((gx0 - b[0], gy0 - b[1], gx1 - b[0], gy1 - b[1])), (gx0, gy0)
                        )
                continue
            x0 = min(boxes[i][0] for i in members)
            y0 = min(boxes[i][1] for i in members)
            x1 = max(boxes[i][2] for i in members)
            y1 = max(boxes[i][3] for i in members)
            x0, y0 = max(0, x0), max(0, y0)
            x1, y1 = min(w, x1), min(h, y1)
            tile = Image.new("RGBA", (x1 - x0, y1 - y0), (0, 0, 0, 0))
            td = ImageDraw.Draw(tile)
            for i in members:
                op = ops[i]
                b = op[1]
                if op[0] == "e":
                    td.ellipse([b[0] - x0, b[1] - y0, b[2] - x0, b[3] - y0], fill=op[2])
                else:
                    td.arc(
                        [b[0] - x0, b[1] - y0, b[2] - x0, b[3] - y0],
                        start=op[2],
                        end=op[3],
                        fill=op[4],
                        width=op[5],
                    )
            img.alpha_composite(tile, (x0, y0))

    # ------------------------------------------------------------------
    # 暗角
    #
    # 与旧版（git 5e3dac6）的差异只有**缓存的存法与合成方式**，暗角几何
    # 一字未动：掩码仍是 (w/4,h/4) 圆角矩形 → resize 到 (w,h) →
    # GaussianBlur(w//14) → ×0.55 → over 到超采样画布 → 整图 BOX 缩小。
    #
    # 旧版按 (w,h) 缓存**全尺寸 RGBA 层**、上限 24 条、超限整体 clear()：
    #   · 689 行卡的画布 1760×10376 ⇒ 单条 73MB，24 条极端 ~1.7GB 常驻
    #     ——低内存机器（RAM 1.7GB）被顶进 swap，「发作式」慢渲染元凶；
    #   · 缓存键含高度、命中率低，超限 clear 又让 miss 连片重算。
    # 现在只缓存 ×0.55 后的 **L 模式 alpha 掩码**（1 字节/像素，省 3/4），
    # 用**字节预算 + LRU** 代替「条数上限 + 全清」；合成改为分条带构建
    # 黑色层后**原位** over（不再分配整幅黑色 RGBA 与整幅合成结果）。
    # 逐像素输出与旧版一致（bench_compare.py 对拍验证）。
    # ------------------------------------------------------------------
    _VIGNETTE_LRU: "OrderedDict[tuple[int, int], Image.Image]" = OrderedDict()
    # 掩码缓存字节预算。★ 2026-09-27：96MB → 24MB —— 6 张基准卡全尺寸掩码
    # 实测合计 22.2MB，24MB 足以全缓存（bench 命中率不受影响）；多尺寸大卡
    # 连续渲染时长驻掩码从最多 48MB 压到 24MB，直接压「渲完 RSS」水位。
    _VIGNETTE_BUDGET = 24 * 1024 * 1024
    _VIGNETTE_STRIP = 1024  # 原位合成条带高（超采样像素；
    #                                     # 1024 时黑色层 8.4MB，瞬时更低）

    def _vignette_mask(self, w: int, h: int) -> Image.Image:
        """×0.55 后的暗角 alpha 掩码（L 模式，字节预算 LRU 缓存）。

        与旧版（git 5e3dac6）的暗角几何一字未动：掩码仍是 (w/4,h/4) 圆角
        矩形 → resize 到 (w,h) → GaussianBlur(w//14) → ×0.55。缓存存法
        与预算见 _VIGNETTE_BUDGET 注释。★ 2026-09-27 从 _apply_vignette
        拆出独立方法，供 _render 在画布创建前预热（错开掩码计算与画布的
        内存峰值）；收尾时走 LRU 命中。
        """
        lru = ImageRenderer._VIGNETTE_LRU
        scaled = lru.get(key := (w, h))
        if scaled is None:
            # —— miss：按旧版几何原样重算（几何变更必须逐像素对拍，勿动）——
            mask = Image.new("L", (w // 4, h // 4), 0)
            md = ImageDraw.Draw(mask)
            mw, mh = mask.size
            md.rounded_rectangle(
                [mw // 10, mh // 10, mw * 9 // 10, mh * 9 // 10], radius=mw // 6, fill=110
            )
            mask = mask.resize((w, h))
            mask = mask.filter(ImageFilter.GaussianBlur(w // 14))
            scaled = mask.point(lambda v: int(v * 0.55))
            del mask
            lru[key] = scaled
            total = sum(m.width * m.height for m in lru.values())
            while len(lru) > 1 and total > ImageRenderer._VIGNETTE_BUDGET:
                _, old = lru.popitem(last=False)  # LRU：最久未用的先出
                total -= old.width * old.height
        lru.move_to_end(key)  # 命中/新建都算「最近使用」
        return scaled

    def _apply_vignette(self, img, w: int, h: int) -> None:
        scaled = self._vignette_mask(w, h)
        # —— 分条带构建黑色层、原位 over（不再分配整幅 RGBA）——
        # ★ 2026-09-27：黑色层跨条带**复用同一块缓冲**（每渲染只分配一次，
        #   旧版每条带一次 Image.new + 一次 fill）；putalpha 只改写 alpha 带，
        #   RGB 恒为 (2,3,6)，复用完全等价。
        strip_h = min(ImageRenderer._VIGNETTE_STRIP, h)
        black = Image.new("RGBA", (w, strip_h), (2, 3, 6, 255))
        for y in range(0, h, strip_h):
            y1 = min(y + strip_h, h)
            blk = black if y1 - y == strip_h else black.crop((0, 0, w, y1 - y))
            blk.putalpha(scaled.crop((0, y, w, y1)))
            img.alpha_composite(blk, (0, y))

    def _cleanup(self, keep: int = 6):
        try:
            cards = sorted(self.cache_dir.glob("card_*.png"), key=lambda p: p.stat().st_mtime)
            for old in cards[:-keep]:
                old.unlink()
        except OSError:
            pass
