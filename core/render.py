# -*- coding: utf-8 -*-
"""渲染引擎：文本卡片（-w）与图片卡片（-t / auto）。

- 文本模式：全角感知（CJK 宽度按 2 列计算）的字符表格，游戏内可直接复制；
- 图片模式：Warframe Orokin UI 风格的 Pillow 信息卡片（A「Orokin 精修」：
  扁平暗底 + 圆角分区面板 + 切角金框，语义着色、徽章芯片、2x 超采样）；
  缺字体/Pillow 时由调用方降级文本。

绘制层（2026-10-11 起，A「Orokin 精修」）：
  · 画布是扁平 RGB：一条 1×256 竖向渐变列直接放大，不再叠星点 / 拱纹 / 暗角
    三层全幅合成（旧版这三层是大卡渲染耗时与瞬时内存的主要来源）；
  · ◆ 纯标题 = 分区标题，其后的行装进圆角面板；「◆ 标签：值」= KV 行（值列对齐、
    超长按值列折行）；≥4 项的列表 / 表格用斑马纹，不再逐行画分隔线；
    ※ 注释并成注释面板，含 ⚠ 的进琥珀警示面板；头部 = 英文栏目名 + 标题 + 页码胶囊；
  · 半透明只能预混（RGB 画布无 alpha）：_tint 按面板色预混，极性图标走 paste；
  · 卡宽 960～1260（列对齐卡的列宽合计本身超限时放宽到 1500，仍保列对齐）。

性能优化（2026-09-26 / 09-27，仍在用的部分）：
  · token 上色规则表提升到模块级并记忆化（旧版每次调用重建 40 个正则）；
  · 字体度量记忆化 _tlen()：textlength 按 (字体, 文本) 挂缓存（cProfile
    显示 90 行大卡 ~30% 耗时耗在重复度量同一批 token/单元格/说明串）；
  · 预扫描合并：宽度需求 / 说明列规划 / 沉沦三列 / 行构造合为一遍预处理；
  · _wrap 整行快速路径（仅基本布局、无极性标记时与逐字路径严格等价）+ 说明列
    折行结果按渲染缓存；量测画布 / 图标缩放缓存复用；
  · 渲染收尾显式断开 ImageDraw 对画布的强引用（编码阶段少驻留一整幅画布）。
  （旧版的暗角掩码 LRU / 星点 sprite / 渐变金线 / RGBA 渐变画布等优化随背景
  三层一并移除；逐字节对拍基准因有意改像素而失效，改由
  tests/test_render_overflow.py 的「耗时闸门」守住不回退。）
"""

from __future__ import annotations

import json
import random
import re
import sys
import unicodedata
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


# ---------------------------------------------------------------------------
# 图表行（§CHART）：格式层输出机器行，画图模式画真实折线图，文本模式展开回方块字
# ---------------------------------------------------------------------------
# 格式层（formatters.fmt_trend）用一行 `§CHART{"title":…,"unit":…,"labels":[…],"values":[…]}`，
# JSON 走 ``ensure_ascii=True``（全 ASCII）—— 机器行不参与折行/列宽判定，含全角字符
# 会让宽度预扫描把卡撑爆。文本模式（text_card / 渲染器不可用）由 chart_text_lines
# 展开为「方块字走势 + 最高/最低/最新一行」，信息不丢（同 ⟦tents⟧ 的先例）。
_CHART_RE = re.compile(r"^§CHART(\{.*\})$")
_CHART_BLOCKS = "▁▂▃▄▅▆▇█"


def parse_chart(text: str) -> Optional[dict]:
    """解析 ``§CHART{json}`` 机器行 → ``{title, unit, labels, values}``；非机器行/坏行返回 None。

    ``values`` 里的 ``None`` **与 0 同义**（价格统计里 0 = 该小时 / 该日无成交），
    都记成「无数据」：折线在此断开，也不参与最高/最低 —— 否则图上会出现
    「最低 0p」这种把用户吓一跳的假点（旧 `_spark` 的 `if x` 也是这个口径）。

    **至少 2 个有效点**才成图，否则返回 None（调用方退回纯文字，避免画出只有一个点的
    「图」）。``labels`` 按需补空串 / 截齐到与 ``values`` 等长（轴标签缺失不影响出图）。
    """
    m = _CHART_RE.match((text or "").strip())
    if not m:
        return None
    try:
        obj = json.loads(m.group(1))
    except ValueError:
        return None
    if not isinstance(obj, dict):
        return None
    raw = obj.get("values")
    if not isinstance(raw, list) or len(raw) < 2:
        return None
    vals: list[Optional[float]] = []
    for v in raw:
        try:
            f = None if v is None else float(v)
        except (TypeError, ValueError):
            f = None
        vals.append(None if not f else f)
    if sum(1 for v in vals if v is not None) < 2:
        return None
    labs = obj.get("labels")
    if not isinstance(labs, list):
        labs = []
    labs = [str(x) for x in labs[: len(vals)]] + [""] * max(0, len(vals) - len(labs))
    return {
        "title": str(obj.get("title") or "走势"),
        "unit": str(obj.get("unit") or ""),
        "labels": labs,
        "values": vals,
    }


# ---------------------------------------------------------------------------
# 「词条范围」机器行（§RANGES）：格式层给四种组合的区间，图片模式排成五列表格，
# 文字模式塞不下四列 ⇒ 只出**高亮那一列** + 一行「其余组合怎么查」。
# 与 §CHART / ⟦tents⟧ 同一套：格式层只产出机器行，渲染层按模式展开。
# ---------------------------------------------------------------------------
_RANGES_RE = re.compile(r"^§RANGES(\{.*\})$")


def parse_ranges(text: str) -> Optional[dict]:
    """解析 ``§RANGES{json}`` → ``{cols, primary, rows, text_hint}``；坏行返回 None。

    ``rows`` 每行 = ``[词条名, 组合1的值, 组合2的值, …]``（长度 = ``len(cols)+1``）；
    ``primary`` = 高亮列下标（用户指定的组合）。整行坏掉时**丢弃**，不留裸 JSON 给用户。
    """
    m = _RANGES_RE.match((text or "").strip())
    if not m:
        return None
    try:
        obj = json.loads(m.group(1))
    except ValueError:
        return None
    if not isinstance(obj, dict):
        return None
    cols = obj.get("cols")
    rows = obj.get("rows")
    if not isinstance(cols, list) or not cols or not isinstance(rows, list) or not rows:
        return None
    out_rows = []
    for r in rows:
        if not isinstance(r, list) or len(r) != len(cols) + 1:
            continue
        out_rows.append([str(x) for x in r])
    if not out_rows:
        return None
    try:
        primary = int(obj.get("primary") or 0)
    except (TypeError, ValueError):
        primary = 0
    # 组合色字母（每列一个，见 formatters._RIVEN_COMBO_SPAN）：缺/长度不符时留空
    # ⇒ 展开退化成「无组合色」（旧机器行/手工行也不会炸）。
    _spans = obj.get("spans")
    spans = (
        [str(s) for s in _spans] if isinstance(_spans, list) and len(_spans) == len(cols) else []
    )
    return {
        "cols": [str(c) for c in cols],
        "spans": spans,
        "name_span": str(obj.get("name_span") or "w"),
        "primary": max(0, min(primary, len(cols) - 1)),
        "rows": out_rows,
        "text_hint": str(obj.get("text_hint") or ""),
    }


def _ranges_band_col(primary: int) -> int:
    """§RANGES 的 ``primary``（**数值列**下标，不含「词条」名）→ ``cells`` 列下标。

    ``cells[0]`` 是词条名（见 `_split_cells`），故指定列在 cells 里的位置要 +1 ——
    竖底带按这个下标取列坐标（2026-10-24 实测踩过 off-by-one：底带画到了词条名列）。
    """
    return int(primary) + 1


def _expand_ranges_line(line: str, *, for_text: bool = False) -> list[str]:
    """§RANGES 机器行 → 可绘制的普通行。

    · 图片模式：表头行（无「·」，走既有 thead 小灰粗字）+ 每词条一行五格。
      **上色规则（2026-10-24 配色重做）**：颜色跟**组合**走、不跟列位置走 ——
      表头每格用该列组合色；数据格指定列用组合色全饱和、其余三列用「组合色 ×
      INK_DIM 各半」的次列变体；词条名列普通词条 ``INK``、融合词条 ``VIOLET``
      （**显式染段**，绕开语义词表 —— 切割 / 冲击不再单独变青绿）。
    · 文字模式：只出高亮那一列（``· 词条　值``）+ 一行「其余组合」提示；
      染段标记由 ``text_card`` 统一剥掉，故**文字输出与改版前一致**。
    """
    obj = parse_ranges(line)
    if obj is None:
        # ★ 非 §RANGES 行**原样返回**（与 `_expand_tents_line` 同规矩）。
        #   首版写成 `return []` ⇒ 展开时把所有普通行都吃掉：整张卡只剩表格，
        #   ◆ 分区标题与 ※ 注释全没了，且所有别的卡渲染成空
        #   （2026-10-24 实测：4 个渲染测试直接红）。
        return [line]
    if for_text:
        out = [f"· {r[0]}　{r[1 + obj['primary']]}" for r in obj["rows"]]
        if obj["text_hint"]:
            out.append(obj["text_hint"])
        return out
    _spans = obj["spans"] or [""] * len(obj["cols"])
    _nsp = obj["name_span"] or "w"

    def _cell_mark(i: int, v: str) -> str:
        s = _spans[i] if i < len(_spans) else ""
        if not s:
            return f"⟦w⟧{v}⟦/w⟧"
        if i == obj["primary"]:
            return f"⟦{s}⟧{v}⟦/{s}⟧"
        d = _SPAN_DIM.get(s, s)
        return f"⟦{d}⟧{v}⟦/{d}⟧"

    out = [
        "　词条　"
        + "　".join(
            (f"⟦{s}⟧{c}⟦/{s}⟧" if s else c) for s, c in zip(_spans, obj["cols"], strict=False)
        )
    ]
    for r in obj["rows"]:
        cells = [_cell_mark(i, v) for i, v in enumerate(r[1:])]
        out.append(f"· ⟦{_nsp}⟧{r[0]}⟦/{_nsp}⟧　" + "　".join(cells))
    return out


def _fit_title(d, font, text: str, max_w: float) -> str:
    """把标题收进 ``max_w``：**只在真的超宽时**截断并补省略号。

    ★ 2026-10-24 修：原写法是「先逐字砍到放得下、再无条件补一个『…』」，
    于是**没超宽的短标题也被缀上省略号**（线上实测：每张趋势图的
    「48h 中位走势（p）」都渲染成「48h 中位走势（p）…」）。判据改成
    「先问要不要截」——放得下就原样返回。
    """
    if not text or d.textlength(text, font=font) <= max_w:
        return text
    cut = text
    while cut and d.textlength(cut + "…", font=font) > max_w:
        cut = cut[:-1]
    return (cut + "…") if cut else ""


def _fmt_num(v: float) -> str:
    """数值显示：整数不带小数、小数留 1 位（118.667 → 118.7）。"""
    return f"{v:.1f}".rstrip("0").rstrip(".")


def _spark(values: list[float], target: int = 30) -> str:
    """把价格序列压成一行趋势条（8 档；分段取均值降到 ``target`` 格以内）。

    分段的点**取段均值**而不是抽样 —— 抽样会把单小时的尖峰整个丢掉。
    """
    v = [x for x in values if x]
    if len(v) < 2:
        return ""
    n = len(v)
    if n > target:
        out: list[float] = []
        for i in range(target):
            lo = i * n // target
            hi = max(lo + 1, (i + 1) * n // target)
            seg = v[lo:hi]
            out.append(sum(seg) / len(seg))
        v = out
    lo, hi = min(v), max(v)
    if hi <= lo:
        return _CHART_BLOCKS[0] * len(v)
    return "".join(_CHART_BLOCKS[min(7, int((x - lo) / (hi - lo) * 7.999))] for x in v)


def chart_text_lines(chart: dict) -> list[str]:
    """图表行的文本模式展开：方块字走势 + 「最高 / 最低 / 最新」一行。

    文字模式没有坐标轴，最高最低必须用这一行说清楚（维护者 2026-10-23 的原始诉求
    就是「最高最低不明确」）。空值点跳过，时间取该点的轴标签。
    """
    vals = chart.get("values") or []
    labs = chart.get("labels") or []
    title = chart.get("title") or "走势"
    unit = chart.get("unit") or ""
    idx = [i for i, v in enumerate(vals) if v is not None]
    if not idx:
        return [f"· {title}　（区间内无有效数据）"]

    def _lab(i: int) -> str:
        s = labs[i] if i < len(labs) else ""
        return f"（{s}）" if s else ""

    out: list[str] = []
    spark = _spark([vals[i] for i in idx])
    if spark:
        out.append(f"· {title}　{spark}")
    hi = max(idx, key=lambda i: vals[i])
    lo = min(idx, key=lambda i: vals[i])
    last = idx[-1]
    out.append(
        f"· 最高 {_fmt_num(vals[hi])}{unit}{_lab(hi)}｜"
        f"最低 {_fmt_num(vals[lo])}{unit}{_lab(lo)}｜"
        f"最新 {_fmt_num(vals[last])}{unit}{_lab(last)}"
    )
    return out


def _expand_chart_line(line: str) -> list[str]:
    """``§CHART{…}`` 机器行 → 文本模式行（方块字 + 最高/最低/最新）；非机器行原样返回。"""
    if not line.startswith("§CHART"):
        return [line]
    chart = parse_chart(line)
    if chart is None:
        return []  # 坏行/点太少：整行丢掉，不留一串裸 JSON 给用户看
    return chart_text_lines(chart)


def text_card(title: str, lines: list[str], footer: str = "", max_width: int = 44) -> str:
    """构造排版整齐的字符界面卡片。"""
    title = _POL_MARK_RE.sub("", title or "")
    lines = [seg for x in lines for seg in _expand_tents_line(_POL_MARK_RE.sub("", x or ""))]
    lines = [seg for x in lines for seg in _expand_chart_line(x)]
    # §RANGES 机器行：文字模式塞不下四列 ⇒ 只出高亮那一列；「其余组合」提示全卡只留一行
    _rng: list[str] = []
    _hints: set[str] = set()
    for _x in lines:
        if not _x.startswith("§RANGES"):
            _rng.append(_x)
            continue
        _o = parse_ranges(_x)
        if _o is None:
            continue
        _h = _o["text_hint"]
        _rng += (
            _expand_ranges_line(_x, for_text=True)
            if _h not in _hints
            else [f"· {r[0]}　{r[1 + _o['primary']]}" for r in _o["rows"]]
        )
        _hints.add(_h)
    lines = _rng
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
    from PIL import Image, ImageDraw, ImageFont
except ImportError:  # pragma: no cover
    Image = ImageDraw = ImageFont = None  # type: ignore[assignment]

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
# ★ 2026-10-02 维护者要求：水印/状态卡标题显示**完整版本号**（如 1.1.3），
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

# 配色：Orokin 暗金 + Tenno 能量色（A「Orokin 精修」，2026-10-11 起）
BG_TOP = (12, 15, 22)  # 底色竖向渐变：顶 → 底
BG_BOT = (9, 11, 17)
GOLD = (212, 176, 106)
GOLD_BRIGHT = (240, 215, 154)
# ★ 2026-10-16 遗物部件按**杜卡德价值**金 / 银 / 铜三色（维护者要求，参考 warframe.market 重生页）：
#   100 = 金、45 / 65 = 银、15 / 25 = 铜。⚠ 按部件自身价值，不按遗物槽位稀有度 ——
#   100 金币的部件也会出现在罕见 / 常见槽位（维护者 10-16 指出）。
RARITY_GOLD = GOLD_BRIGHT
RARITY_SILVER = (178, 202, 236)  # 冷蓝银：须与正文暖白（INK）和次列暗灰（INK_DIM）都拉开
RARITY_BRONZE = (214, 146, 92)
GOLD_DIM = (122, 100, 62)
GOLD_FAINT = (96, 82, 54)
INK = (236, 232, 222)  # 主文字：暖白
INK_DIM = (168, 176, 190)  # 次文字
INK_FAINT = (132, 140, 154)  # 注释
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

# 面板与分区色（全部不透明；画布是扁平 RGB，没有 alpha 可言）
PANEL_BG = (19, 24, 35)  # 分区面板底；「面板内元素」的半透明混色基准
PANEL_ALT = (24, 30, 43)  # 斑马纹
PANEL_LINE = (52, 47, 38)  # 面板描边（暖金）与分区标题延伸线
HAIR = (40, 44, 54)  # 头/脚分隔细线
NOTE_BG = (15, 19, 28)  # ※ 注释面板
NOTE_BAR = (92, 80, 56)
WARN_BG = (40, 30, 18)  # 含 ⚠ 的警示面板（琥珀）
WARN_BAR = (232, 178, 106)

# 版式尺度（设计像素）
CARD_W_MIN, CARD_W_MAX = 960, 1260  # 卡宽范围
# 列对齐卡（遗物出库/入库等 9 列网格）的列宽合计本身就超过 1260 时，允许放宽到
# 旧版上限 1500：宁可卡宽，也不能触发下面的「安全阀」放弃列对齐（对齐是这类卡的存在意义）。
CARD_W_TABLE_MAX = 1500
# 奸商卡专用上限（2026-10-16 用户：「宽度不够你可以拉长」）：货单两件一行共 6 列，长商品名
# （如「蒙面者的经纬仪号」船员套服袖件）会让 Σ列宽超过 1500 → 安全阀关掉列对齐、左右挤成一团。
# 只对这张卡放宽，宁可卡宽也保住列对齐；其他卡不受影响。
CARD_W_BARO_MAX = 2000
FS_BODY, FS_NOTE, FS_TITLE = 30, 24, 44  # 正文 / 注释 / 标题字号
RH_ROW, RH_CONT, RH_NOTE, RH_SEC = 50, 40, 36, 58  # 行高：正文 / 续行 / 注释 / 分区标题


def _mix(a, b, t: float) -> tuple[int, int, int]:
    return (
        round(a[0] + (b[0] - a[0]) * t),
        round(a[1] + (b[1] - a[1]) * t),
        round(a[2] + (b[2] - a[2]) * t),
    )


def _tint(
    color: tuple[int, int, int], alpha: int, bg: Optional[tuple[int, int, int]] = None
) -> tuple[int, int, int]:
    """把带透明度的颜色预混到背景上，返回**不透明** RGB。

    画布是扁平 RGB，ImageDraw 不做 alpha 混合：`fill=(255,255,255,12)` 会被
    直接写成一整块纯白而不是淡白。想要半透明观感，只能先自己按 alpha 混好
    颜色再画 —— 面积大的填充（徽章底、芯片底、面板）尤其要走这里。

    Args:
        color: 前景 RGB。
        alpha: 等效不透明度（0-255）。
        bg: 背景 RGB，默认面板底色（运行时取：主题会改 PANEL_BG，默认参数不能在定义时绑死）。
    """
    if bg is None:
        bg = PANEL_BG
    return _mix(bg, color, max(0, min(255, int(alpha))) / 255.0)


_BG_COL: Optional["Image.Image"] = None


def _bg_column() -> "Image.Image":
    """底色渐变的 1×256 列（只读、全进程复用）；放大即得整幅画布。"""
    global _BG_COL
    if _BG_COL is None:
        col = Image.new("RGB", (1, 256))
        col.putdata([_mix(BG_TOP, BG_BOT, i / 255) for i in range(256)])
        _BG_COL = col
    return _BG_COL


# 栏目英文名补表（TITLE_THEME 没覆盖或误命中的卡；先于 TITLE_THEME 匹配）
_SLUG_OVERRIDE = [
    (("词条范围",), "RIVEN RANGES"),
    (
        ("紫卡融合",),
        "RIVEN SPLICING",
    ),  # 2026-10-24：别被「紫卡」子串带去 RIVEN AUCTIONS  # 2026-10-24：原先按「紫卡」子串命中 RIVEN AUCTIONS
    (("紫卡分析",), "RIVEN ANALYSIS"),
    (("紫卡倾向",), "RIVEN DISPOSITION"),
    (("伤害计算",), "DAMAGE CALCULATOR"),
    (("配卡识别",), "LOADOUT SCAN"),
    (("效价融合",), "VALENCE FUSION"),
    (("遗物",), "VOID RELICS"),
    (("部件出处",), "PART SOURCES"),
    (("价格趋势",), "PRICE TREND"),
    (("紫卡热度",), "RIVEN TRENDS"),
    (("灵化",), "INCARNON ROTATION"),
    (("信条",), "TENET ROTATION"),
    (("终幕",), "CODA ROTATION"),
    (("沉沦",), "DESCENDIA"),
    (("九重天",), "VOID STORMS"),
    (("钢铁精华",), "STEEL ESSENCE SHOP"),
    (("言录使",), "ACRITHIS"),
    (("碎块兑换",), "PALLADINO SHOP"),
    (("Prime 出库",), "PRIME VAULT"),
    (("开核桃",), "RELIC CRACKING"),
    (("进行中的活动",), "LIVE EVENTS"),
    (("（Arcane",), "CODEX ENTRY"),  # 赋能词条页：别被「赋能 → CALCULATOR」误命中
]


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
# 一眼能看出这行是 MOD 还是外观 —— 维护者 2026-09-18 要求「注意颜色运用」。
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
    "精英": MAGENTA,
    "常见": INK_DIM,
    "罕见": STEEL,
    "稀有": GOLD_BRIGHT,
    "本期": GOLD_BRIGHT,
    "常驻": STEEL,
}

# ---------------------------------------------------------------------------
# 行内语义配色：把「任务类型 / 挑战名 / 派系 / 元素 / 加成%」和正文分开
#
# 玩家反馈「任务类型像和后面文字是一块的」「元素和元素加成分别来个其他颜色」——
# 靠的是把这几类词从正文里**切成 token**，再按规则上色（见 _TOKEN_RE / _TOKEN_RULES）。
#
# ⚠️ 中文没有词边界，所以任务类型 / 元素 / 派系一律用
#    `(?:^|(?<=[\s　｜（]))…(?=[\s　｜）]|$)` 这种**前后边界**包住：
#    不包的话「电磁力场装置」里的「磁力」、「致命冲击」里的「冲击」都会被染色，
#    看起来像卡出错。★/▣ 开头的 token 因为先被匹配走，不受影响。
# ---------------------------------------------------------------------------
MISSION_TYPE_COLOR = CYAN  # 任务类型（歼灭/生存/虚空决战…）
CHALLENGE_COLOR = VIOLET  # 赏金挑战名（能量超载/终结好戏…）
FACTION_COLOR = (176, 182, 194)  # 派系（Grineer / 科腐者 / 合一众…）与 I系/C系 同色：A 款去橙，冷灰
# 赏金卡不做派系染色的英文派系名（2026-10-02 维护者要求：Grineer/Corpus 在赏金里
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
# g / s / b = 部件杜卡德价值金 / 银 / 铜（formatters.ducat_span 生成，2026-10-16）
# a / m / y / t = 「词条范围」四种组合色（3正1负 / 2正1负 / 3正 / 2正；字母由
#   formatters._RIVEN_COMBO_SPAN 决定）—— **颜色跟组合走、不跟列位置走**；
# n = 次要文字（未确认词条名后的「?」）。
# d / e / f / h = 上述四色的**非指定列**变体（与 INK_DIM 各半混合）。★ 值写成 lambda：
#   派生色必须**绘制时现算**才跟后台改色走；写成常量或另建模块级 dict 会在换肤后不变。
_SPAN_COLORS = {
    "c": BLUE,
    "v": VIOLET,
    "w": INK,
    "g": RARITY_GOLD,
    "s": RARITY_SILVER,
    "b": RARITY_BRONZE,
    "a": AMBER,
    "m": MAGENTA,
    "y": CYAN,
    "t": STEEL,
    "n": INK_DIM,
    "d": lambda: _mix(AMBER, INK_DIM, 0.5),
    "e": lambda: _mix(MAGENTA, INK_DIM, 0.5),
    "f": lambda: _mix(CYAN, INK_DIM, 0.5),
    "h": lambda: _mix(STEEL, INK_DIM, 0.5),
}
# 组合色 → 其「非指定列」变体字母（§RANGES 展开用）
_SPAN_DIM = {"a": "d", "m": "e", "y": "f", "t": "h"}


def _span_color(letter: str, default=None):
    """染段字母 → 颜色；值为 callable 的（组合色次列）**现算**，跟主题走。"""
    c = _SPAN_COLORS.get(letter)
    if c is None:
        return default
    return c() if callable(c) else c


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
            color = _span_color(mm.group(1), INK) if mm else INK
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
# ★ 2026-09-27：**采用外援优化版的设计**（维护者选定，替代我这版「40% 白中性框」）——
# 底色比面板亮一档 + **暖金描边**（沿用主面板内框线的色，不引入新色系）；
# 等级徽章嵌框内右上；档位名前的圆点随之去掉（框已承担分组视觉，维护者 2026-09-26 口径）。
# 起因：块间那条 `alpha=9`（≈3.5% 白）的淡线用户实测「看不清」，块与块难区分。
TIER_FRAME_FILL = (23, 27, 37)  # 底色比面板亮一档
TIER_FRAME_LINE = (74, 64, 46)  # 描边：暖金
_TIER_PAD = 8  # 框缘到行顶/行底的内边距（设计像素）
_TIER_TAIL = 24  # 组尾距：组间距 = 尾距 − 2×内边距 = 8px
# 档位框的左右边界（相对正文列）：框比文字列**外扩**，视觉上包住等级徽章。
# 点位三块（_draw_tent_blocks）沿用同一对边界 —— 否则最左/最右块与上方档位框
# 对不齐（2026-10-02 玩家反馈「左右边距去掉」）。⚠️ 两处共用，别再各写数字。
_TIER_FRAME_L = 16  # 左缘 = x_text − 16
_TIER_FRAME_R = 46  # 右缘 = box[2] − 46

# ---------------------------------------------------------------------------
# 赏金点位块（2026-10-02 维护者要求改版）：底部横向三块（小帐篷 A/B/C）
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

# ---------------------------------------------------------------------------
# 图表行（§CHART）：折线图的几何参数（设计像素；绘制时乘 SS）
# ---------------------------------------------------------------------------
# 一行机器行 = 一整块绘图区。绘图区内部再切：顶部标题带、左侧价格刻度沟、底部时间轴。
# 留白是给刻度标签的，不能让折线压到标签上（维护者 2026-10-23：「线条看不见」）。
CHART_MIN_W = 620  # 图表行要求的最小卡宽（其余正文行更宽时以它们为准）
CHART_H = 236  # 图表行总高
CHART_PAD_T = 40  # 顶部标题带（标题 + 与顶网格线标签的净空）
CHART_PAD_B = 34  # 底部时间轴带（刻度标签）
CHART_PAD_L = 92  # 左侧价格刻度沟（「1.2kp」这类最宽标签）
CHART_PAD_R = 14  # 右侧留白（末端「最新」标注不许贴边）
CHART_LINE_W = 3  # 折线线宽
CHART_DOT_R = 5  # 最高/最低/最新三个点标记的半径
CHART_AXIS_LINES = 4  # 横向网格线条数（含顶/底）


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
# 词表染色的右边界：后面不能紧跟汉字/字母/数字（「钢铁意志」「执刑官长」不再被半词染色）
_W_R_ZH = r"(?![\u4e00-\u9fffA-Za-z0-9])"


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

# 避头标点：这些字不得单独起一行（宁可行尾多探出一个字，也不留「］」「。」孤行）
_NO_LINE_START = frozenset("］）」』】》〕、。，；：！？,.;:!?)]}%")

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
    r"(\[[^\]]*\]|★+[^、\s　]*|▣[^、\s　]+|(?<![\d.])\d+(?:\.\d+)?p(?![a-zA-Z])|"
    + _TIMER_MARK
    + _DUR
    + r"|（[0-9hms ]+）|已结束"
    r"|▲[^、\s　]+|▼[^、\s　]+|信\d+"
    # 「钢铁之路」必须在「钢铁」前面：alternation 先匹配先赢，反了的话
    # 赏金标签「钢铁之路」只有前两个字被染红（2026-10-02 维护者要求 4 字全红）。
    r"|网页在线|在线|离线|钢铁之路|钢铁"
    + _W_R_ZH
    + r"|九重天|速刷|执刑官"
    + _W_R_ZH
    + r"|满级|零级"
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
_LV_RE = re.compile(r"｜(\d+(?:-\d+)?级)(?:｜|$)")
# 「上架 6h / 12m / 45s / >100h」—— wr 拍卖行的挂单时长（2026-10-08）：
#   放行尾，交给同一套「行尾抽取 → 右对齐列」机制，与赏金行的等级/计时同列对齐。
_AGE_RE = re.compile(r"(?:^|[\s　])(上架\d+[smh]|上架>\d+h)\s*$")

# 「· 武器名　元素 25.7%」—— 第二格是「元素 + 百分比」时，整列按**实测像素宽**对齐
# （只有渲染层知道真实字形宽度；formatter 端按 CJK=2 / ASCII=1 手算会差出一个字，
#  玩家反馈「没对齐真的好丑」就是这么来的）。
_PAIR_RE = re.compile(r"^[^\s　]+ [\d.]+%\Z")

# 原内联在 _render 里的三个热路径正则提升为模块常量（每次渲染少过一遍
# re 模块的缓存查找；语义逐字节不变）。
_NUM_HEAD_RE = re.compile(r"^\d+\.(?!\d)")  # 行首序号（numbered 行）
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
    (re.compile(r"^\d+(?:\.\d+)?p$"), CYAN),
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
    (re.compile(r"^(?:I系|C系|G系|O系|低语)$"), FACTION_COLOR),
    (re.compile(r"^\d{2,4}\s(?:S)$"), RED),
    (re.compile(r"^\d{2,4}\s(?:A\+)$"), GOLD_BRIGHT),
    (re.compile(r"^\d{2,4}\s(?:A-|A|B\+|B)$"), GREEN),
    (re.compile(r"^\d{2,4}\s(?:F)$"), INK_FAINT),
    (re.compile(r"^\d{2,4}\s?\[(?:A-|A|B\+|B)\]$"), GREEN),
    (re.compile(r"^\d{2,4}\s?\[F\]$"), INK_FAINT),
    # 奸商预测卡：序号用暗金（弱化）、杜卡德报价金色（价格类统一高亮）
    (re.compile(r"^\d+\.$"), INK_FAINT),
    (re.compile(r"^\d+\s*杜卡德$"), GOLD_BRIGHT),
    # 部件杜卡德价值按金 / 银 / 铜分档（100 / 45·65 / 15·25）；其余数值（奸商报价等）保持金色
    (re.compile(r"^100杜$"), RARITY_GOLD),
    (re.compile(r"^(?:45|65)杜$"), RARITY_SILVER),
    (re.compile(r"^(?:15|25)杜$"), RARITY_BRONZE),
    (re.compile(r"^\d+杜$"), GOLD_BRIGHT),
    (re.compile(r"^\d+(?:\.\d+)?万?现金$"), CYAN),
)

# 小号标记芯片：裂隙等列表里的「钢铁 / 九重天」标记（不是大红字）
_MARK_CHIPS: dict[str, tuple[int, int, int]] = {"钢铁": RED, "九重天": CYAN, "速刷": GREEN}

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


# 字形覆盖探测（2026-10-16：原先 _probe 只认 Noto/雅黑/苹方/文泉驿的族名，用户导入的
# 霞鹜文楷、思源宋体等一律选不中）。改为「这些字都有字形」才算能用：
FONT_PROBE_HANZI = "国的一是战甲紫卡遗物仲裁蹲赏金"  # 必须全有（卡片正文最常见的字）
FONT_PROBE_SYMBOLS = "◆※·★▲▼▣"  # 卡片符号；缺了只提示（会显示成方块），不拒用
_NOTDEF_PROBE = "\U0010fffd"  # 私用区末位：任何字体都不会有 ⇒ 拿它画出来的就是 .notdef


def _glyph_missing(font, ch: str, notdef) -> bool:
    """该字符是否缺字形：画出来是空的，或与 .notdef 位图完全一样。"""
    try:
        m = font.getmask(ch)
    except Exception:  # noqa: BLE001
        return True
    if not m.getbbox():
        return True
    return notdef is not None and m.size == notdef[0] and bytes(m) == notdef[1]


def font_face_coverage(font) -> dict:
    """单个字体面的覆盖情况：{hanzi_missing, symbol_missing, ok}。"""
    try:
        nd = font.getmask(_NOTDEF_PROBE)
        notdef = (nd.size, bytes(nd)) if nd.getbbox() else None
    except Exception:  # noqa: BLE001
        notdef = None
    hz = [c for c in FONT_PROBE_HANZI if _glyph_missing(font, c, notdef)]
    sy = [c for c in FONT_PROBE_SYMBOLS if _glyph_missing(font, c, notdef)]
    return {"hanzi_missing": hz, "symbol_missing": sy, "ok": not hz}


_PREFERRED_FACE = ("Noto Sans CJK SC", "YaHei", "PingFang", "WenQuanYi")


def pick_cjk_face(path) -> Optional[dict]:
    """在字体文件（ttc 多面）里挑能用的中文面：已知简中族名优先，否则取第一个覆盖达标的面。

    返回 {index, name, coverage} 或 None（没有可用面 / 打不开）。
    """
    if ImageFont is None:
        return None
    first_ok = None
    for idx in range(12):
        try:
            f = ImageFont.truetype(str(path), 24, index=idx)
        except Exception:  # noqa: BLE001
            break
        try:
            name = " ".join(x for x in f.getname() if x)
        except Exception:  # noqa: BLE001
            name = ""
        if any(k in name for k in _PREFERRED_FACE) and "Mono" not in name:
            cov = font_face_coverage(f)
            if cov["ok"]:
                return {"index": idx, "name": name, "coverage": cov}
        if first_ok is None:
            cov = font_face_coverage(f)
            if cov["ok"]:
                first_ok = {"index": idx, "name": name, "coverage": cov}
    return first_ok


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

    def __init__(
        self,
        user_dirs: Optional[list[Path]] = None,
        prefer_regular: Optional[Path] = None,
        prefer_bold: Optional[Path] = None,
    ):
        self.regular: Optional[Path] = None
        self.bold: Optional[Path] = None
        self._sc_index = 0
        self._idx: dict[str, int] = {}  # 每个文件各自的字体面下标（正文 / 粗体可来自不同 ttc）
        self._cache: dict[tuple[str, int], "ImageFont.FreeTypeFont"] = {}
        # 运维面板「外观」里指定的字体优先（appearance.json）；不存在或不含中文就照常自动挑
        for want, slot in ((prefer_regular, "regular"), (prefer_bold, "bold")):
            if want and Path(want).is_file() and self._probe(Path(want)):
                setattr(self, slot, Path(want))
        # 包内完整字库 → 用户字体目录 → 随包子集 → 系统。
        # ★ 2026-10-16：原先是「整个 PACKED（含子集）→ 用户 → 系统」⇒ 市场版永远带着子集，
        #   用户放进 plugin_data/<插件>/fonts/ 的完整字库（fetch_font.py / 运维面板下载）**永远用不上**，
        #   生僻字仍是方块。_probe 要求常用汉字都有字形（pick_cjk_face），误放的西文字体选不中，挪前是安全的。
        full = [p for p in self.PACKED if "Subset" not in p.name]
        subset = [p for p in self.PACKED if "Subset" in p.name]
        for path in full + self._user_fonts(user_dirs or []) + subset + self.SYSTEM:
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
        """选到含简体中文的字体面并记下下标；已知简中族名优先，其次按字形覆盖判断（见 pick_cjk_face）。"""
        try:
            face = pick_cjk_face(path)
        except Exception:  # noqa: BLE001
            return False
        if face is None:
            return False
        self._idx[str(path)] = face["index"]
        self._sc_index = face["index"]
        return True

    def get(self, which: str, size: int):
        path = self.regular if which == "regular" else self.bold
        if path is None:
            return None
        key = (str(path), size)
        if key not in self._cache:
            try:
                idx = self._idx.get(str(path), self._sc_index)
                self._cache[key] = ImageFont.truetype(str(path), size * SS, index=idx)
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
    "可蹲类型",  # 2026-10-11：蹲帮助卡「类型　说明」两列对齐（与指令一览同一套语法文档排版）
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
    "词条范围",  # 2026-10-23：紫卡词条范围卡「词条　区间」两列对齐
    "紫卡融合",  # 2026-10-24：融合词条 / 原词条一 / 原词条二 三列
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


def _card_slug(title: str) -> str:
    """头部英文栏目名（kicker）：补表优先，其次 TITLE_THEME，都没有就用品牌名。"""
    for keys, slug in _SLUG_OVERRIDE:
        if any(k in title for k in keys):
            return slug
    for keys, (_c, slug) in TITLE_THEME:
        if any(k in title for k in keys):
            return slug
    return f"WARFRAME {CARD_BRAND}"


def _classify_rows(rows: list[dict], *, bounty: bool, plain: bool) -> None:
    """行类型二次判定（就地改 rows）：KV 行 / 结论行 / 表头行。

    · ``◆ 标签：值``（标签 1～30 字、值非空）→ ``kv``，``r["kv"] = (标签, 值)``；
    · ``◆ 纯标题`` 仍是分区标题（section）—— 唯一例外是**结论行**：卡上已有
      「◆ 标签：值」型 KV，且本行紧跟着另一个 ◆ 行（后面没有内容行可装），
      那它不是标题，是「◆ DPS 持续 ≈ 48（…）」这类整句结论，按 KV 整行处理
      （``kv = (整句, "")``）；
    · 赏金卡（bounty）的 ◆ 是地区横幅，不参与 KV 判定；折行续行（cont）不判；
    · 表头行（thead）：分列行、非「·」开头，且下一行是「·」列表项、上一行是
      分区标题或本行即首行 —— 画成小号灰粗字 + 细线。语法文档卡（plain）不判。
    """
    for i, r in enumerate(rows):
        if r["kind"] == "section" and not bounty and not r.get("cont"):
            _b = r["text"][1:].strip()
            _h, _s, _v = _b.partition("：")
            _nx = rows[i + 1] if i + 1 < len(rows) else None
            if _s and _v.strip() and 1 <= len(_h) <= 30:
                r["kind"] = "kv"
                r["kv"] = (_h, _v.strip())
            elif (
                _nx is not None
                and _nx["kind"] == "section"
                and any(rr["kind"] == "section" and "：" in rr["text"] for rr in rows)
            ):
                r["kind"] = "kv"
                r["kv"] = (_b, "")
    if not plain and not bounty:
        for r in rows:
            if r.get("subcont") and r.get("cont"):
                # 从属行折出来的后续行：与首行同缩进、同灰度
                r["sub"] = True
                r["indent"] = r.get("indent", 0) + 1
        # 从属行（行首全角空格、非「·」、非表头）= 上一条目的缩进续行：
        # 并入上一条的斑马纹 / 行高（旧版「标题行 + 说明行」被拆成两种底色）。
        # 表头行（紧跟分区标题、下一行是「·」项）不算从属。
        for i, r in enumerate(rows):
            if (
                r["kind"] == "normal"
                and not r.get("cont")
                and r.get("lead")
                and not r["text"].startswith("·")
                and i > 0
                and rows[i - 1]["kind"] in ("normal", "numbered", "kv", "kvcont")
                # 上一行必须是「父条目」（「·」列表项 / 编号行 / KV / 已判从属 / 折行续行）：
                # 一整排都缩进的普通行（如氏族奖励的「　　500 分：…」）是同级列表，不是从属
                and (
                    rows[i - 1]["kind"] != "normal"
                    or rows[i - 1]["text"].lstrip().startswith("·")
                    or rows[i - 1].get("sub")
                    or rows[i - 1].get("cont")
                )
                and not (
                    i + 1 < len(rows)
                    and rows[i + 1]["text"].lstrip().startswith("·")
                    and rows[i - 1]["kind"] == "section"
                )
            ):
                r["cont"] = True
                r["sub"] = True
                r["cells"] = None
                r["indent"] = max(r.get("indent", 0), 0) + 1
    if not plain:
        for i, r in enumerate(rows):
            if (
                r.get("cells")
                and r["kind"] == "normal"
                and not r["text"].lstrip().startswith("·")
                and i + 1 < len(rows)
                and rows[i + 1]["text"].lstrip().startswith("·")
                and (i == 0 or rows[i - 1]["kind"] == "section")
            ):
                r["thead"] = True


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
        self._user_dirs = user_dirs
        self.fonts = None
        self.reload_fonts()
        self.reload_theme()

    @property
    def theme_dir(self) -> Path:
        """主题图片（背景图 bg.png / 角标 logo.png）：plugin_data/<插件>/theme/。"""
        return self.cache_dir.parent / "theme"

    def theme_image(self, kind: str):
        """读主题图片（kind = bg / logo）；没有或坏了返回 None。"""
        if Image is None:
            return None
        f = self.theme_dir / f"{kind}.png"
        try:
            with Image.open(f) as im:
                im.load()
                return im.convert("RGBA" if kind == "logo" else "RGB")
        except Exception:  # noqa: BLE001
            return None

    def reload_theme(self) -> None:
        """按 appearance.json 的 theme 套用主题（主题是模块级状态，见文件末「主题」一节）。"""
        if Image is None:
            return
        t = self.load_appearance().get("theme") or {}
        try:
            apply_theme(
                t.get("colors") or {},
                bg_image=self.theme_image("bg") if t.get("bg_image") else None,
                bg_dim=float(t.get("bg_dim", 0.55)),
                panel_alpha=float(t.get("panel_alpha", 1.0)),
                logo=self.theme_image("logo") if t.get("logo") else None,
            )
        except Exception:  # noqa: BLE001 - 主题坏了退回默认，别让出图挂掉
            apply_theme({})

    @property
    def appearance_path(self) -> Path:
        """外观设置（字体选择 / 主题）：plugin_data/<插件>/appearance.json，运维面板写。"""
        return self.cache_dir.parent / "appearance.json"

    def load_appearance(self) -> dict:
        try:
            import json

            data = json.loads(self.appearance_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - 没有 / 坏了都按默认
            return {}
        return data if isinstance(data, dict) else {}

    @property
    def user_font_dir(self) -> Path:
        """用户字体目录（plugin_data/<插件>/fonts/；运维面板下载完整字库的落点）。"""
        return self.cache_dir.parent / "fonts"

    def reload_fonts(self) -> None:
        """重新挑选字体（下载 / 导入字库或改了字体选择后调用，不必重载插件）。"""
        if Image is None:
            return
        app = self.load_appearance()
        pr, pb = app.get("font_regular"), app.get("font_bold")
        self.fonts = _Fonts(
            user_dirs=self._user_dirs,
            prefer_regular=Path(pr) if pr else None,
            prefer_bold=Path(pb) if pb else None,
        )

    def font_kind(self) -> str:
        """当前正文字体的来源：full（完整字库）/ subset（随包子集）/ user（导入）/ system / none。"""
        reg = getattr(self.fonts, "regular", None)
        if reg is None:
            return "none"
        if "Subset" in reg.name:
            return "subset"
        if reg.name in ("NotoSansCJK-Regular.ttc", "NotoSansCJK-Bold.ttc"):
            return "full"
        try:
            if reg.parent.resolve() == self.user_font_dir.resolve():
                return "user"  # 运维面板导入 / 上传的字体
        except OSError:
            pass
        return "system"

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
        body_font = f.get("regular", FS_BODY)
        title_font = f.get("bold", 46)
        sub_font = f.get("regular", 19)
        small_font = f.get("bold", 27)
        note_font = f.get("regular", FS_NOTE)
        if not all((body_font, title_font, sub_font, small_font, note_font)):
            return None

        # —— 页码芯片 & 主题 ——
        m = _PAGE_RE.search(title)
        page_chip = f"{m.group(1)}/{m.group(2)}" if m else ""
        title = _PAGE_RE.sub("", title).strip()
        accent = GOLD
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
        # 奸商卡：正文全是商品名 ——「净化 Corpus Prime」的「净化」不是任务类型、
        # 「冲击」类 MOD 名也不是元素，词表染色在这张卡上只会误染（2026-10-16 用户截图）
        self._baro_mode = "虚空商人" in title
        # 侵袭卡的 4 列（类型/派系/等级/地点）走既有列对齐机制；
        # 价格榜 / 紫卡热度榜的多列数字同样按全角空格切列对齐
        # 遗物三张列表卡（入库/出库/列表）同样是网格排版：不开这个模式的话
        # 单元格只会顺序拼接，「Lith A1/Lith A10/…」长短不一会导致列位抖动
        # （维护者 2026-09-17：「排版有点干燥，也没对齐」）。
        # 部件反查卡（2026-09-18）是「遗物 / 槽位 / 状态」三列，同理。
        self._table_mode = is_table_title(title)
        plat_chip, foot_notes = self._parse_footer(footer)

        # —— 宽度优先：逐行实测需求（文本+芯片外扩+时间列），卡宽 960～1260 ——
        # ★ 2026-09-27 优化：旧版对 (lines) 有 4 遍独立扫描 —— 宽度需求、
        #   说明列规划（_plain_body 表格）、沉沦之地三列、正文行构造 —— 每遍
        #   都重复 _strip_emoji + _split_cells + 计时正则。现把预处理结果
        #   (raw, clean, had_icon) 存一次，宽度/列宽/配对/双列/三列收集合并进
        #   同一遍循环：语义逐行等价（各收集器判据原样保留），纯去重。
        #   另：textlength 全部走 _tlen 记忆化（token/单元格/说明串在
        #   预扫描、列宽、行构造、绘制推进里被反复测量）。
        self._wrap_cache = {}  # 说明列折行结果缓存（本次渲染内有效）
        pad = 58
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
        grid_rows: list[tuple[list[str], float]] = []
        # §RANGES 机器行在**宽度扫描之前**展开成普通表格行 —— 否则五列宽度不进
        # `needed`，卡宽会按没展开的机器行算（本卡就会窄到放不下四列）。
        # 展开时顺带记下「表头行下标 → 载荷」，供后面把组合色竖底带挂在行上。
        _rng_lines: list[str] = []
        _rng_meta: dict[int, dict] = {}
        for _x in lines or [""]:
            _segs = _expand_ranges_line(_x)
            _pay = parse_ranges(_x) if str(_x).startswith("§RANGES") else None
            for _k, _sg in enumerate(_segs):
                if _pay is not None and _k == 0:
                    _rng_meta[len(_rng_lines)] = _pay
                _rng_lines.append(_sg)
        lines = _rng_lines
        src = lines[:MAX_BODY_LINES]
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
            # 图表机器行：内容是 JSON，按原文测宽会把卡撑爆 —— 只保证图表有最小可用宽度，
            # 实际绘图铺满正文区（其余正文行照常决定卡宽）。
            if clean.startswith("§CHART"):
                needed = max(needed, CHART_MIN_W)
                continue
            if "　" in clean:
                _cells = _split_cells(clean)
                if (
                    _cells
                    and len(_cells) >= 3
                    and clean.lstrip().startswith("·")
                    and not self._plain_body
                    and not self._table_mode
                ):
                    # 非表格卡的「· 列表项」多格行：块内自动列对齐（见 auto_cols）要的是
                    # **整张网格**的宽度，不是单行宽度；这里先按去掉计时后的格子累计列宽，
                    # 稍后并入卡宽 / 折行预算，避免带标记的长行被误折成孤字。
                    _gt = _TIMER_RE.search(clean) or _TIMER_ALT_RE.search(clean)
                    _gw = 0.0
                    if _gt:
                        _gw = _tlen(measure, timer_font, _gt.group(0).strip()) / SS + 40
                        _gc = _split_cells((clean[: _gt.start()] + clean[_gt.end() :]).rstrip(" ·"))
                    else:
                        _gc = _cells
                    grid_rows.append((_gc or _cells, _gw))
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
            if raw.startswith("　") and not clean.lstrip().startswith(("·", "▸", "◆", "※")):
                w_line += _INDENT_PX + 30  # 从属行缩进一级（见 _classify_rows）
            if not clean.startswith(("※", "◆")):
                mt = _TIMER_RE.search(clean) or _TIMER_ALT_RE.search(clean) or _AGE_RE.search(clean)
                if mt and timer_font:
                    w_line += _tlen(measure, timer_font, mt.group(0).strip()) / SS + 30
            # ★ 2026-10-24（词条范围四列并排）：**表模式卡的注释不参与卡宽**。
            #   注释绘制时按 x_right 折行 ⇒ 拿它的未折行原文算宽度，等于为一条
            #   永远不会以那个宽度出现的行买宽（实测「词条范围」卡被一条 1118px 的
            #   注释撑到上限 1260，而表格只用到 ~40% 宽度）。
            if self._table_mode and clean.startswith("※"):
                continue
            needed = max(needed, w_line)
            _is_sub = (
                raw.startswith("　")
                and not clean.lstrip().startswith(("·", "▸"))
                and not (
                    _li > 0
                    and src[_li - 1].startswith("◆")
                    and _li + 1 < len(src)
                    and src[_li + 1].lstrip().startswith("·")
                )
            )  # 从属行（缩进续行）不进表格列宽；表头行（紧跟 ◆ 标题、下一行是「·」项）除外
            if (
                self._table_mode
                and "　" in clean
                and not clean.startswith(("※", "◆"))
                and not _is_sub
            ):
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
                for ci, cell in enumerate(_SEG_MARK_RE.sub("", clean).split("　")):
                    if ci == 0 and cell.lstrip().startswith("·"):
                        cell = cell.lstrip()[1:].strip()  # 与 _split_cells 一致
                    if ci >= len(col_max):
                        col_max.append(0.0)
                    col_max[ci] = max(col_max[ci], _tlen(measure, body_font, cell) / SS)
        # —— 语法文档卡（指令一览）：说明列过宽时做「列内折行」规划 ——
        # 以前列宽超限会触发下面的安全阀整体放弃列对齐，后果是指令/说明
        # 双色与「A / B / C」多指令分色**全部失效**（2026-09-14 玩家反馈
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
                w_cap=CARD_W_MAX,
                reserve=2 * pad + 56 + 30,  # +30：留一字余量（避头标点允许行尾多探出一字）
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

        grid_need = 0.0
        if grid_rows:
            _n = max(len(c) for c, _ in grid_rows)
            _cw = [
                max(
                    (_tlen(measure, body_font, c[i]) for c, _ in grid_rows if len(c) > i), default=0
                )
                / SS
                for i in range(_n)
            ]
            grid_need = sum(_cw) + 30 * _n + max(g for _, g in grid_rows)
            needed = max(needed, grid_need)

        # 沉沦之地（炼狱塔）的三列：炼狱[N] / 任务类型 / 目标 ——
        # 同样按实测像素拉列，并且**任务类型列统一上类型色**
        # （玩家反馈「任务类型有的标记有的不标记」：之前靠 token 规则，
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
        w_cap = CARD_W_MAX
        if self._table_mode and len(col_max) > 1:
            _tbl_need = sum(col_max) + 24 * (len(col_max) - 1) + pad * 2 + 56
            _tbl_cap = CARD_W_BARO_MAX if getattr(self, "_baro_mode", False) else CARD_W_TABLE_MAX
            w_cap = int(
                max(CARD_W_MAX, min(_tbl_cap, int(_tbl_need) + 1))
            )  # +1：向上取整，防安全阀差 1px 误关
        elif grid_need:
            # 列表网格（自动列对齐）放不进常规卡宽时，同表格卡一样放宽到 1500，
            # 宁可卡宽也不放弃对齐（对齐是这类行的存在意义）
            w_cap = int(max(CARD_W_MAX, min(CARD_W_TABLE_MAX, int(grid_need + pad * 2 + 56) + 1)))
        W = int(max(CARD_W_MIN, min(w_cap, needed + pad * 2 + 96)))
        W = int(max(W, min(title_min + 120, w_cap)))
        # ★ 2026-10-24 Claude 审「词条范围」四列表：w_cap 已按表格总宽放到 ≤1500，但 W 只按逐行
        #   文字宽算 ⇒ 表格总宽 > W 时最宽的行被折行（折点还会切进染段标记，露出「⟦/w⟧」）。
        #   表格模式下卡宽至少放得下整张表（仍受 w_cap 约束；放不下照旧走下面的安全阀）。
        if self._table_mode and len(col_max) > 1:
            W = int(max(W, min(w_cap, int(_tbl_need) + 1)))
        # 安全阀：卡宽有 960/1260 上下限，列对齐即便按上面的公式也可能在
        # 极端数据下仍放不进正文区。此时放弃列对齐、退回逐行折行，
        # 宁可少一点对齐观感，也不能把文字压出面板边框。
        if (
            self._table_mode
            and len(col_max) > 1
            and (sum(col_max) + 24 * (len(col_max) - 1)) > W - (2 * pad + 56)
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
            if t.startswith("§CHART"):
                return "chart"
            # ★ 2026-09-27：赏金**档位行**（块首，格式层用 `_BOUNTY_HEAD_PREFIX`
            #   显式标记）—— 赏金卡「块与块之间画线」的判据。用显式标记而不是
            #   内容启发式：同卡上档位/奖励/点位行混排，猜会漏 oracle 与退路路径。
            if t.startswith("▸"):
                return "bounty_head"
            if _NUM_HEAD_RE.match(t):
                return "numbered"
            return "normal"

        rows: list[dict] = []
        _cur_rng: Optional[dict] = None  # 当前「词条范围」表格的载荷（表头行起、注释行止）
        for _li, (raw, clean, had_icon) in enumerate(cleaned):
            # 行首全角空格 = 缩进层级。**≥4 个**才额外缩进一级（赏金卡里
            # 「主目标 / 副目标」的分级就靠它）；2 个空格的行（奖励串、
            # 「任务：」标签）保持原样，不然整张卡会整体位移。
            _is_rng_head = _li in _rng_meta
            if _is_rng_head:
                _cur_rng = _rng_meta[_li]
            elif _cur_rng is not None and not clean.lstrip().startswith("·"):
                _cur_rng = None
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
            elif _n_sp and not clean.lstrip().startswith(("·", "▸", "◆", "※")):
                wrap_w -= 2 * _INDENT_PX  # 从属行会缩进一级，折行预算同步扣掉（含避头标点余量）
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
                                "cont": _j > 0,
                            }
                        )
                    continue
            # ※ 注释行画的是 note_font（25px），但折行一直按 body_font（30px）
            # 测量 —— 量出来的宽度比实际大约 20%，注释因此**提前折行**、尾行
            # 只剩几个字（维护者 2026-09-17：「排版有点干燥」的一个来源）。
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
            if base_kind == "chart":
                # 图表机器行：JSON 原样存行上（不折行、不参与计时/分列判定），
                # 高度全走 _extra_h（_base_h 返回 0），由 _draw_chart 铺满这一块。
                _chart = parse_chart(clean)
                if _chart is not None:
                    rows.append(
                        {
                            "text": "",
                            "icon": False,
                            "timer": "",
                            "level": "",
                            "kind": "chart",
                            "indent": 0,
                            "cells": None,
                            "chart": _chart,
                            "_extra_h": CHART_H,
                        }
                    )
                continue
            _wrap_font = note_font if base_kind == "note" else body_font
            if (
                grid_need
                and base_kind == "normal"
                and clean.lstrip().startswith("·")
                and clean.count("　") >= 2
                and grid_need <= W - pad * 2 - 56
            ):
                wrapped = [clean]  # 网格整体放得下：不按「单行 + 空格」折行（会把标记折成孤字）
            elif (
                getattr(self, "_baro_mode", False)
                and self._table_mode
                and base_kind == "normal"
                and clean.lstrip().startswith("·")
                and "　" in clean
            ):
                # 奸商货单行按列对齐绘制：列宽已逐列计入卡宽（安全阀保证放得下），
                # 不能再按「整行原文」折行 —— 原文的全角空格分隔比列间隙宽，两件长名
                # 同行时原文超宽被折、退回普通排版，整行脱离列对齐（2026-10-16 实测）。
                wrapped = [clean]
            else:
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
                        "cont": j > 0,
                        "warn": "⚠" in raw,
                        "li": _li,
                        "lead": _n_sp if j == 0 else 0,
                        "subcont": bool(
                            j > 0 and _n_sp and not clean.lstrip().startswith(("·", "▸", "◆", "※"))
                        ),
                        # 「词条范围」表格：载荷挂在表头行与随后的数据行上（画组合色竖底带用）
                        "ranges": _cur_rng,
                        "ranges_head": _is_rng_head and j == 0,
                    }
                )

        # —— 等级徽章统一宽度 ——
        # 徽章按「宽的那个」统一宽度，"75-80级 / 95-100级 / 115-120级" 三个才能
        # 左缘也齐（只右对齐的话左缘参差，玩家反馈「等级做的有些不美观」）。
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

        # ==================================================================
        # 绘制管线（A「Orokin 精修」，2026-10-11 起）：
        #   · 扁平 RGB 画布（去星点/拱纹/暗角三道全幅合成）；
        #   · 分区面板：◆ 纯标题 = 区块标题，其下各行装进一块圆角面板；
        #     「◆ 标签：值」= KV 行（标签弱化、数值提亮，值列对齐）；
        #   · 去逐行分隔线 → 斑马纹（≥4 项的列表/表格）；折行续行紧排同一条纹；
        #   · ※ 注释并入一块注释面板（⚠ 行单独警示面板）；
        #   · 文字按行盒**垂直居中**（旧版顶对齐、贴底线）。
        # ==================================================================
        slug = _card_slug(title)
        bounty = getattr(self, "_bounty_mode", False)
        plain = self._plain_body
        if not bounty:
            _m = []
            for r in rows:
                if r.get("cont") and _m and _m[-1]["kind"] == "section" and r["kind"] != "note":
                    # 用预处理后的整行原文（_wrap 会吃掉段尾「、」，不能拼接折行片段）
                    _m[-1] = dict(_m[-1], text=cleaned[_m[-1]["li"]][1])
                    continue
                _m.append(r)
            rows = _m
        _classify_rows(rows, bounty=bounty, plain=plain)

        outer = 22
        PADX = 48
        kv_lf = f.get("bold", FS_BODY - 2)
        kv_col = 0.0
        for r in rows:
            if r.get("kv") and r["kv"][1]:
                _w = _tlen(measure, kv_lf, r["kv"][0] + "：")
                if _w <= 300 * SS:
                    kv_col = max(kv_col, _w)
        _xk = (outer + PADX + 4) * SS
        _xr = (W - outer - PADX - 6) * SS
        _new = []
        for r in rows:
            if r.get("kv") and r["kv"][1]:
                lab, val = r["kv"]
                lw = _tlen(measure, kv_lf, lab + "：")
                xv_rel = kv_col + 18 * SS if lw <= kv_col else lw + 10 * SS
                r["kv_x"] = xv_rel
                budget = int((_xr - _xk - xv_rel) / SS)
                if r.get("timer"):
                    budget -= int(_tlen(measure, timer_font, r["timer"]) / SS) + 40
                if (
                    "[" in val and "]" in val
                ):  # 芯片底框外扩（同普通行的 -64 口径），防「]」被折到下一行
                    budget -= 64
                segs = self._wrap_cached(val, body_font, max(200, budget))
                r["kv"] = (lab, segs[0] if segs else "")
                _new.append(r)
                for sg in segs[1:]:
                    _new.append(
                        {
                            "text": sg,
                            "icon": False,
                            "timer": "",
                            "level": "",
                            "kind": "kvcont",
                            "indent": 0,
                            "cells": None,
                            "cont": True,
                            "kv_x": xv_rel,
                        }
                    )
            else:
                _new.append(r)
        rows = _new
        _dense = 6 if len(rows) > 40 else 0
        RH = {
            "normal": RH_ROW - _dense,
            "numbered": RH_ROW + 2,
            "bounty_head": RH_ROW,
            "kv": RH_ROW + 4,
            "section": RH_SEC,
            "note": RH_NOTE,
            "tents": 0,
            "chart": 0,
            "kvcont": RH_CONT,
        }

        def _base_h(r):
            if r["kind"] == "note":
                return RH_NOTE
            if r.get("cont") and r["kind"] != "section":
                return RH_CONT - _dense // 2
            if r["kind"] == "section":
                return RH_SEC - _dense
            return RH[r["kind"]]

        PADB, GAP = 10, 14
        layout = []  # (row, y, h_text, h_total, block_idx)
        blocks = []  # [kind, y0, y1]
        yy = 0
        cur = None

        def _close():
            nonlocal yy, cur
            if cur is not None:
                yy += PADB
                cur[2] = yy
                blocks.append(cur)
                yy += GAP
                cur = None

        for r in rows:
            k = r["kind"]
            bh = _base_h(r)
            tot = bh + r.get("_tier_tail", 0) + r.get("_extra_h", 0)
            if k == "section":
                _close()
                layout.append((r, yy, bh, tot, -1))
                yy += tot
                continue
            want = "body"
            if k == "note":
                want = "warn" if r.get("warn") else "note"
            if bounty and k != "note":
                want = "none"
            if cur is None or cur[0] != want:
                _close()
                cur = [want, yy, 0]
                yy += PADB
            layout.append((r, yy, bh, tot, len(blocks)))
            yy += tot
        _close()
        body_h = max(0, yy - GAP)

        HEAD = 128
        FOOT = 70
        H = outer * 2 + 30 + HEAD + body_h + 26 + FOOT

        # —— 画布：扁平 RGB + 一条竖向细渐变（1×256 列 BILINEAR 直接放大，无叠层）——
        Wp, Hp = W * SS, H * SS
        img = _bg_column().resize((Wp, Hp), Image.BILINEAR)
        if _THEME["bg_image"] is not None:
            img = _with_bg_image(img, _THEME["bg_image"], _THEME["bg_dim"])
        d = ImageDraw.Draw(img)

        X0 = (outer + PADX) * SS
        X1 = (W - outer - PADX) * SS
        # —— 外框：Orokin 切角金框 + 四角金饰 ——
        box = (outer * SS, outer * SS, (W - outer) * SS, (H - outer) * SS)
        _chamfer(d, box, 22 * SS, outline=GOLD_DIM, width=2 * SS)
        for cx, cy, sx, sy in (
            (box[0], box[1], 1, 1),
            (box[2], box[1], -1, 1),
            (box[0], box[3], 1, -1),
            (box[2], box[3], -1, -1),
        ):
            L = 30 * SS
            d.line([(cx, cy + sy * L), (cx + sx * L, cy)], fill=GOLD_BRIGHT, width=3 * SS)

        _vc_cache: dict = {}

        def _ty(font, yc):
            v = _vc_cache.get(id(font))
            if v is None:
                bb = d.textbbox((0, 0), "国Ag", font=font, anchor="la")
                v = (bb[1] + bb[3]) / 2
                _vc_cache[id(font)] = v
            return yc - v

        # —— 头部：kicker（英文栏目名，强调色）+ 标题 + 页码胶囊 + 分隔线 ——
        y0 = (outer + 30) * SS
        kf = f.get("bold", 18)
        kx = X0
        tx = kx
        for ch in slug:
            d.text((kx, y0), ch, font=kf, fill=accent)
            kx += _tlen(d, kf, ch) + 3 * SS
        pf = f.get("bold", 24)
        chip_w = (_tlen(d, pf, page_chip) + 34 * SS) if page_chip else 0
        title_right = X1 - (chip_w + 20 * SS if page_chip else 0)
        t_font = None
        for _sz in (FS_TITLE, 40, 36, 32, 29):
            t_font = f.get("bold", _sz)
            if _tlen(d, t_font, title) <= title_right - tx:
                break
        else:
            _cut = title
            while _cut and _tlen(d, t_font, _cut + "…") > title_right - tx:
                _cut = _cut[:-1]
            title = (_cut + "…") if _cut else title
        ty_title = y0 + 28 * SS
        d.text((tx, ty_title), title, font=t_font, fill=INK)
        if page_chip:
            ch_y0, ch_y1 = ty_title + 8 * SS, ty_title + 52 * SS
            d.rounded_rectangle(
                [X1 - chip_w, ch_y0, X1, ch_y1],
                radius=22 * SS,
                fill=_tint(accent, 40),
                outline=accent,
                width=SS,
            )
            d.text(
                (X1 - chip_w / 2 - _tlen(d, pf, page_chip) / 2, _ty(pf, (ch_y0 + ch_y1) / 2)),
                page_chip,
                font=pf,
                fill=INK,
            )
        rule_y = y0 + 98 * SS
        d.line([(X0, rule_y), (X1, rule_y)], fill=HAIR, width=SS)
        d.rectangle([X0, rule_y - SS, X0 + 72 * SS, rule_y + 2 * SS], fill=accent)

        by = (outer + 30 + HEAD) * SS
        x_text = X0 + 28 * SS
        x_bullet = X0 + 10 * SS
        x_num_text = X0 + 50 * SS
        x_right = X1 - 6 * SS
        PX0, PX1 = X0 - 16 * SS, X1 + 16 * SS

        # —— 面板 + 斑马纹 ——
        zebra_rows: set = set()
        zebra_blocks: set = set()
        for bi, (bk, b0, b1) in enumerate(blocks):
            if bk == "none":
                continue
            rect = [PX0, by + b0 * SS, PX1, by + b1 * SS]
            if bk == "body":
                if _panel_translucent():
                    _translucent_fill(img, rect, PANEL_BG, _THEME["panel_alpha"], 12 * SS)
                    d.rounded_rectangle(rect, radius=12 * SS, outline=PANEL_LINE, width=SS)
                else:
                    d.rounded_rectangle(
                        rect, radius=12 * SS, fill=PANEL_BG, outline=PANEL_LINE, width=SS
                    )
                items = [lay for lay in layout if lay[4] == bi]
                has_kv = any(lay[0]["kind"] == "kv" for lay in items)
                n_items = sum(1 for lay in items if not lay[0].get("cont"))
                if not has_kv and n_items >= 4:
                    zebra_blocks.add(bi)
                    idx = -1
                    for lay in items:
                        r = lay[0]
                        if r.get("thead"):
                            continue
                        if not r.get("cont"):
                            idx += 1
                        if idx % 2 == 1:
                            zebra_rows.add(id(r))
                            ry = by + lay[1] * SS
                            zr = [PX0 + SS, ry, PX1 - SS, ry + lay[3] * SS]
                            if _panel_translucent():
                                _translucent_fill(img, zr, PANEL_ALT, _THEME["panel_alpha"], 0)
                            else:
                                d.rectangle(zr, fill=PANEL_ALT)
            else:
                fill = WARN_BG if bk == "warn" else NOTE_BG
                bar = WARN_BAR if bk == "warn" else NOTE_BAR
                if _panel_translucent():
                    _translucent_fill(img, rect, fill, _THEME["panel_alpha"], 10 * SS)
                else:
                    d.rounded_rectangle(rect, radius=10 * SS, fill=fill)
                d.rectangle([PX0, rect[1] + 6 * SS, PX0 + 4 * SS, rect[3] - 6 * SS], fill=bar)

        # KV 值列：标签宽取最大（上限 300px），值整列对齐
        # 非表格卡：同一面板内 ≥2 行「≥3 格」的行 → 块内自动列对齐（统一网格）
        auto_cols: dict = {}
        if not self._table_mode and not plain:
            _by_blk: dict = {}
            for lay in layout:
                _c = lay[0].get("cells")
                if (
                    _c
                    and len(_c) >= 2
                    and lay[0]["kind"] == "normal"
                    and not (pair_col is not None and len(_c) == 2 and _PAIR_RE.match(_c[1]))
                ):
                    _by_blk.setdefault(lay[4], []).append(_c)
            for _bi, _cl in _by_blk.items():
                if len(_cl) < 3 and not any(len(c) >= 3 for c in _cl):
                    continue
                _n = max(len(c) for c in _cl)
                _ws = [
                    max(
                        (
                            _tlen(d, body_font, _SEG_MARK_RE.sub("", c[i]))
                            for c in _cl
                            if len(c) > i
                        ),
                        default=0,
                    )
                    for i in range(_n)
                ]
                _pos, _acc = [], 0.0
                for _w in _ws:
                    _pos.append(_acc)
                    _acc += _w + 30 * SS
                _tw_blk = max(
                    (
                        _tlen(d, timer_font, lay[0]["timer"]) + 40 * SS
                        for lay in layout
                        if lay[4] == _bi and lay[0].get("timer")
                    ),
                    default=0.0,
                )
                if _acc - 30 * SS <= x_right - x_text - _tw_blk:
                    auto_cols[_bi] = _pos

        # 列对齐（同旧版口径）
        arb_cols = None
        arb_w: list[float] = []  # 各列像素宽（组合色竖底带要按列宽铺）
        if getattr(self, "_table_mode", False):
            split_rows = [
                r["cells"] for r in rows if r.get("cells") and r["kind"] in ("normal", "numbered")
            ]
            ncol = max((len(r) for r in split_rows), default=0)
            arb_cols = []
            acc = 0
            for ci in range(ncol):
                # ★ 量列宽必须先剥掉行内染段标记（`⟦g⟧…⟦/g⟧` 之类）：标记本身不显示，
                #   但会被 textlength 当成可见字符算进去 —— 「词条范围」卡的高亮列因此
                #   量宽 ~120px，把后面各列整体右推、末列被切在面板外（2026-10-24 实测）。
                #   宽度预扫描（col_max）本来就剥了，这里漏了 ⇒ 两处口径不一致。
                wpx = max(
                    (
                        _tlen(d, body_font, _SEG_MARK_RE.sub("", r[ci]))
                        for r in split_rows
                        if len(r) > ci
                    ),
                    default=0,
                )
                arb_cols.append(acc)
                arb_w.append(wpx)
                acc += wpx + 24 * SS
            if not split_rows:
                arb_cols = None

        # 赏金档位框（沿用旧逻辑，坐标改由 layout 给出）
        if tier_frames:
            pos = {id(lay[0]): lay for lay in layout}
            for _s, _e in tier_frames:
                ls, le = pos.get(id(rows[_s])), pos.get(id(rows[_e]))
                if not ls or not le:
                    continue
                fy0 = by + ls[1] * SS - _TIER_PAD * SS
                fy1 = by + (le[1] + le[2]) * SS + _TIER_PAD * SS
                d.rounded_rectangle(
                    (x_text - _TIER_FRAME_L * SS, fy0, X1 + 10 * SS, fy1),
                    radius=10 * SS,
                    fill=TIER_FRAME_FILL,
                    outline=TIER_FRAME_LINE,
                    width=SS,
                )

        # —— 「词条范围」指定列的组合色竖底带 ——
        # 在**文字之前**铺（后面逐行绘制会盖在上面），只在表模式下有列坐标。
        # 色值走 _tint(组合色, 14)（≈14% 不透明度，预混到当前面板底色）⇒ 跟主题走。
        if arb_cols:
            for _hi, _lay in enumerate(layout):
                _r0 = _lay[0]
                _pay = _r0.get("ranges")
                if not _pay or not _r0.get("ranges_head"):
                    continue
                _p = int(_pay.get("primary") or 0)
                _spans = _pay.get("spans") or []
                # ★ primary 是**数值列**下标（不含名字列）⇒ cells 下标要 +1
                #   （表头/数据行的 cells[0] 都是「词条」名，见 _split_cells）
                _pc = _ranges_band_col(_p)
                if _pc >= len(arb_cols) or _pc >= len(arb_w) or _p >= len(_spans):
                    continue
                # 末行 = 本块内紧随其后的「·」数据行（表头之后的连续数据行）
                _last = _hi
                for _j in range(_hi + 1, len(layout)):
                    _r1 = layout[_j][0]
                    if layout[_j][4] != _lay[4] or not (
                        _r1.get("cells") and _r1["text"].lstrip().startswith("·")
                    ):
                        break
                    _last = _j
                _band = _tint(_span_color(_spans[_p], INK) or INK, 14)
                _x0 = x_text + arb_cols[_pc] - 8 * SS
                _x1 = x_text + arb_cols[_pc] + arb_w[_pc] + 8 * SS
                _zr = [
                    _x0,
                    by + _lay[1] * SS,
                    _x1,
                    by + (layout[_last][1] + layout[_last][2]) * SS,
                ]
                if _panel_translucent():
                    _translucent_fill(img, _zr, _band, _THEME["panel_alpha"], 0)
                else:
                    d.rectangle(_zr, fill=_band)

        last_block = None
        for r_, ry, bh, tot, bi in layout:
            t = r_["text"]
            k = r_["kind"]
            y = by + ry * SS
            h = bh * SS
            yc = y + h / 2
            if k == "section":
                st = t[1:].strip()
                sf = f.get("bold", FS_BODY + 1)
                yc_s = y + h * 0.58
                # 金色菱形 + 亮字 + 延伸细线
                d.polygon(self._diamond(PX0 + 8 * SS, yc_s, 8 * SS), fill=accent)
                xe = self._draw_tokens(d, st, PX0 + 26 * SS, _ty(sf, yc_s), sf, GOLD_BRIGHT, X1)
                if xe < X1 - 40 * SS:
                    d.line([(xe + 18 * SS, yc_s), (PX1, yc_s)], fill=PANEL_LINE, width=SS)
                continue
            if bi != last_block:
                last_block = bi
                first = True
            else:
                first = False
            if k == "note":
                nf = note_font
                self._draw_tokens(
                    d,
                    t.lstrip("※").strip(),
                    x_text - 8 * SS,
                    _ty(nf, yc),
                    nf,
                    INK_DIM if r_.get("warn") else INK_FAINT,
                    x_right,
                )
                continue
            if k == "tents":
                self._draw_tent_blocks(
                    d,
                    r_.get("tents") or [],
                    x_text - _TIER_FRAME_L * SS,
                    y,
                    tot * SS,
                    (X1 + 10 * SS) - (x_text - _TIER_FRAME_L * SS),
                    body_font,
                    f.get("bold", 29),
                    accent,
                )
                continue
            if k == "chart":
                self._draw_chart(
                    img,
                    d,
                    r_.get("chart") or {},
                    x_text - 6 * SS,
                    x_right + 6 * SS,
                    y,
                    tot * SS,
                    accent,
                    note_font,
                    f.get("bold", 22),
                )
                continue
            if k == "kvcont":
                self._draw_tokens(
                    d, t, X0 + 4 * SS + r_["kv_x"], _ty(body_font, yc), body_font, INK, x_right
                )
                continue
            if k == "kv":
                lab, val = r_["kv"]
                if not first:
                    d.line([(x_text - 20 * SS, y), (X1 + 4 * SS, y)], fill=HAIR, width=SS)
                lab_col = GOLD
                xk = X0 + 4 * SS
                if not val:
                    self._draw_tokens(d, lab, xk, _ty(kv_lf, yc), kv_lf, lab_col, x_right)
                    continue
                d.text((xk, _ty(kv_lf, yc)), lab, font=kv_lf, fill=lab_col)
                xv = xk + r_["kv_x"]
                vt, _tail = _parse_color_segs(val)
                xe = self._draw_tokens(d, vt, xv, _ty(body_font, yc), body_font, INK, x_right)
                for _st, _sc in _tail:
                    d.text((xe, _ty(body_font, yc)), _st, font=body_font, fill=_sc)
                    xe += _tlen(d, body_font, _st)
                # 计时/等级列照旧
            elif k == "numbered":
                num, rest = t.split(".", 1)
                nb = f.get("bold", 22)
                bw_, bh_ = 38 * SS, 30 * SS
                bx0 = X0 - 4 * SS
                d.rounded_rectangle(
                    [bx0, yc - bh_ / 2, bx0 + bw_, yc + bh_ / 2],
                    radius=8 * SS,
                    fill=_tint(accent, 46),
                )
                d.text(
                    (bx0 + (bw_ - _tlen(d, nb, num)) / 2, _ty(nb, yc)),
                    num,
                    font=nb,
                    fill=GOLD_BRIGHT,
                )
                self._draw_tokens(
                    d, rest.strip(), x_num_text, _ty(body_font, yc), body_font, INK, x_right
                )
            elif r_.get("thead") and (arb_cols is not None or bi in auto_cols):
                hf = f.get("bold", FS_NOTE)
                xt = x_text + r_.get("indent", 0) * _INDENT_PX * SS
                _cols = arb_cols if arb_cols is not None else auto_cols[bi]
                for ci, cell in enumerate(r_["cells"]):
                    if ci < len(_cols):
                        # ★ 2026-10-24：单元格带染段标记时走 _draw_tokens 逐格上色
                        #   （「词条范围」表头按组合色）；其余表格表头不带标记 ⇒
                        #   仍走 d.text(INK_FAINT)，**零行为变化**（语义 token 不会
                        #   误染别的卡的表头）。
                        if "⟦" in cell:
                            self._draw_tokens(
                                d, cell, xt + _cols[ci], _ty(hf, yc), hf, INK_FAINT, x_right
                            )
                        else:
                            d.text((xt + _cols[ci], _ty(hf, yc)), cell, font=hf, fill=INK_FAINT)
                d.line([(PX0 + 12 * SS, y + h), (PX1 - 12 * SS, y + h)], fill=HAIR, width=SS)
            else:
                xt = x_text + r_.get("indent", 0) * _INDENT_PX * SS
                ty = _ty(body_font, yc)
                if r_["icon"]:
                    d.polygon(self._diamond(x_bullet, yc, 6 * SS), fill=accent)
                elif r_.pop("_tier_head", False):
                    t = t.lstrip("　")[1:].strip()
                elif t.startswith(("·", "▸")):
                    t = t[1:].strip()
                    if (
                        bi not in zebra_blocks
                        and bi not in auto_cols
                        and not (arb_cols is not None and r_.get("cells"))
                    ):
                        d.ellipse(
                            [x_bullet - 3 * SS, yc - 3 * SS, x_bullet + 3 * SS, yc + 3 * SS],
                            fill=GOLD_DIM,
                        )
                if r_.get("cont"):
                    pass
                t, _tail_segs = _parse_color_segs(t)
                if self._bounty_mode and r_.get("level") and not r_["icon"]:
                    _mt, _rest = _split_leading_type(t)
                    if _mt:
                        d.text((xt, ty), _mt, font=body_font, fill=MISSION_TYPE_COLOR)
                        _gap = "" if _mt.endswith("：") else " "
                        xt += _tlen(d, body_font, _mt) + _tlen(d, body_font, _gap)
                        t = _rest
                if r_.get("timer") and t.endswith("："):
                    t = t[:-1]
                head, sep, rest = t.partition("：")
                _x_end = None
                if _is_label_row(head, bool(sep), bool(arb_cols is not None and r_.get("cells"))):
                    if not rest.strip():
                        # 值被抽成右侧计时列：整句当标题，去掉悬空的冒号
                        _x_end = self._draw_tokens(d, head, xt, ty, body_font, INK, x_right)
                    elif rest.startswith("暂无"):
                        d.text((xt, ty), head + "：", font=body_font, fill=INK)
                        d.text(
                            (xt + _tlen(d, body_font, head + "："), ty),
                            rest,
                            font=body_font,
                            fill=INK_FAINT,
                        )
                    else:
                        d.text((xt, ty), head + "：", font=body_font, fill=INK_DIM)
                        adv = _tlen(d, body_font, head + "：")
                        if self._bounty_mode and head == "任务":
                            self._draw_task_rest(d, rest, xt + adv, ty, body_font, x_right)
                        else:
                            _x_end = self._draw_tokens(
                                d, rest, xt + adv, ty, body_font, INK, x_right
                            )
                elif arb_cols is not None and r_.get("cells"):
                    for ci, cell in enumerate(r_["cells"]):
                        if ci >= len(arb_cols):
                            break
                        cx = xt + arb_cols[ci]
                        if plain:
                            self._draw_plain_cell(d, ci, cell, cx, ty, body_font)
                        else:
                            self._draw_tokens(d, cell, cx, ty, body_font, INK, x_right)
                else:
                    _cells = r_.get("cells")
                    if (
                        descent_col is not None
                        and _cells
                        and len(_cells) == 3
                        and _DESC_HEAD_RE.match(_cells[0])
                    ):
                        self._draw_tokens(d, _cells[0], xt, ty, body_font, INK, x_right)
                        self._draw_tokens(
                            d,
                            _cells[1],
                            xt + descent_col[0],
                            ty,
                            body_font,
                            MISSION_TYPE_COLOR,
                            x_right,
                        )
                        self._draw_tokens(
                            d,
                            _cells[2],
                            xt + descent_col[0] + descent_col[1],
                            ty,
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
                        self._draw_tokens(d, _cells[0], xt, ty, body_font, INK, x_right)
                        self._draw_tokens(d, _cells[1], xt + pair_col, ty, body_font, INK, x_right)
                    elif _cells and bi in auto_cols and len(_cells) >= 2:
                        for ci, cell in enumerate(_cells[: len(auto_cols[bi])]):
                            self._draw_tokens(
                                d,
                                cell,
                                xt + auto_cols[bi][ci],
                                ty,
                                body_font,
                                INK if ci == 0 else INK_DIM,
                                x_right,
                            )
                    elif _cells and len(_cells) >= 2 and not self._table_mode:
                        # 非表格卡的「名称　说明」双格：第二格弱化，形成主次
                        xe = self._draw_tokens(d, _cells[0], xt, ty, body_font, INK, x_right)
                        rest2 = "　".join(_cells[1:])
                        self._draw_tokens(
                            d,
                            rest2,
                            xe + _tlen(d, body_font, "　"),
                            ty,
                            body_font,
                            INK_DIM,
                            x_right,
                        )
                    else:
                        _x_end = self._draw_tokens(
                            d, t, xt, ty, body_font, INK_DIM if r_.get("sub") else INK, x_right
                        )
                if _tail_segs and _x_end is not None:
                    for _seg_txt, _seg_col in _tail_segs:
                        d.text((_x_end, ty), _seg_txt, font=body_font, fill=_seg_col)
                        _x_end += _tlen(d, body_font, _seg_txt)
            lv = r_.get("level")
            if lv and not r_.get("timer"):
                tw = _tlen(d, timer_font, lv)
                bw = max(lv_box_w, tw + 28 * SS)
                dh = 36 * SS
                d.rounded_rectangle(
                    (x_right - bw, yc - dh / 2, x_right, yc + dh / 2),
                    radius=8 * SS,
                    fill=_tint(STEEL, 40),
                )
                d.text((x_right - bw / 2, yc), lv, font=timer_font, fill=STEEL, anchor="mm")
            if r_.get("timer"):
                tv = r_["timer"]
                main_txt, paren = tv, ""
                mp = _TIMER_PAREN_RE.search(tv)
                if mp:
                    main_txt, paren = tv[: mp.start()], tv[mp.start() :]
                tw_main = _tlen(d, timer_font, main_txt)
                tw_par = _tlen(d, note_font, paren) if paren else 0
                tx0 = x_right - tw_main - (tw_par + 8 * SS if paren else 0)
                d.text((tx0, _ty(timer_font, yc)), main_txt, font=timer_font, fill=AMBER)
                if paren:
                    d.text(
                        (tx0 + tw_main + 8 * SS, _ty(note_font, yc)),
                        paren,
                        font=note_font,
                        fill=(170, 148, 106),
                    )

        # —— 底部：平台 + 来源（左）· 水印（右）——
        fy = by + (body_h + 26) * SS
        d.line([(X0, fy), (X1, fy)], fill=HAIR, width=SS)
        sf_ = f.get("regular", 20)
        fyc = fy + 34 * SS
        left = " · ".join(x for x in ([plat_chip] if plat_chip else []) + foot_notes)
        if left:
            d.text((X0, _ty(sf_, fyc)), left, font=sf_, fill=INK_FAINT)
        wm = f"{CARD_BRAND} {WATERMARK_VERSION}"
        d.text((X1, fyc), wm, font=f.get("bold", 18), fill=GOLD_FAINT, anchor="rm")
        if _THEME["logo"] is not None:  # 用户角标：水印左侧，高 40 设计像素
            wm_w = d.textlength(wm, font=f.get("bold", 18))
            _paste_logo(img, _THEME["logo"], int(X1 - wm_w - 14 * SS), fyc, 40 * SS)

        d = None
        final = img.resize((W, H), Image.Resampling.BOX)
        img = None
        out = self.cache_dir / f"card_{random.randrange(1 << 40):010x}.png"
        final.save(out, "PNG", optimize=False, compress_level=1)
        final = None
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
            # ★ 2026-10-24 Claude 审：染段标记**逐字零宽、标记内不断行**。旧做法是读到闭合「⟧」
            #   才冲账，读标记中间那几个字时宽度临时计入 ⇒ 恰在此处超宽就从标记中间折断，
            #   卡面露出「⟦/w⟧」（「词条范围」四列表实测）。
            seg_chars: set[int] = set()
            if has_seg:
                for m2 in _SEG_MARK_RE.finditer(text):
                    seg_chars.update(range(*m2.span()))
            i = 0
            while i < len(text):
                ch = text[i]
                if i in seg_chars:
                    line += ch
                    i += 1
                    continue
                w_try = w_line + _adv(ch)
                comp = span_end_at.get(i)
                if comp is not None:  # 标记闭合：换成目标宽（与旧版等值）
                    w_try = w_try - span_adv[comp] + span_target[comp]
                if line and w_try > max_w and ch not in _NO_LINE_START:
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

            # 同上：染段标记内的字不参与断行判定（半截标记「⟦/」剥不掉、会被当可见字计宽）
            seg_chars2: set[int] = set()
            if has_seg:
                for m2 in _SEG_MARK_RE.finditer(text):
                    seg_chars2.update(range(*m2.span()))
            for ci, ch in enumerate(text):
                if ci in seg_chars2:
                    line += ch
                    continue
                if line and _mw(line + ch) > max_w and ch not in _NO_LINE_START:
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

    @staticmethod
    def _chart_label_rect(
        d,
        cx: float,
        cy: float,
        lines: list[str],
        font,
        side: str,
        box: tuple[float, float, float, float],
    ) -> Optional[tuple[float, float, float, float]]:
        """算关键点标注小片的外接框 —— **纯几何、不落笔**；放不下返回 None。

        ``side`` = ``"up"`` / ``"down"``：放在关键点的上方还是下方。横向不按锚点放，
        先按「点上方居中」定位再整体收进画区（``box``）—— 序列末点贴着右缘时，
        任何右锚标注都会越界（2026-10-23 实测：48h 图的「最新」标注因此消失）。

        ★ 必须与落笔分离：调用方要拿框先去重（撞上已放的标注就翻面重算）。
        先画后判会在画布上留下被否决的残片（同日实测：48h 图出现两个「最新」小片）。
        """
        lh = font.size + 4 * SS
        th = lh * len(lines) + 2 * 4 * SS
        tw = max((d.textlength(s, font=font) for s in lines), default=0.0)
        w = tw + 2 * 8 * SS
        if w > box[2] - box[0]:
            return None
        bx = min(max(cx - w / 2, box[0]), box[2] - w)
        if side == "up":
            by = cy - CHART_DOT_R * SS - 5 * SS - th
        else:
            by = cy + CHART_DOT_R * SS + 5 * SS
        rect = (bx, by, bx + w, by + th)
        if rect[1] < box[1] or rect[3] > box[3]:
            return None
        return rect

    @staticmethod
    def _draw_chart_label(img, d, rect, lines: list[str], font) -> None:
        """把已定好位的标注小片画出来：底色小片 + 居中两行（数值行 / 时间行）。

        底色用面板底色 —— 折线与网格穿字而过时读不清（维护者 2026-10-23 的原始诉求），
        压一层底再写字。
        """
        _translucent_fill(img, rect, PANEL_BG, 0.86, 6 * SS)
        d.rectangle(list(rect), outline=PANEL_LINE, width=max(1, SS // 2))
        lh = font.size + 4 * SS
        cy = rect[1] + 4 * SS + lh / 2
        for i, s in enumerate(lines):
            d.text(((rect[0] + rect[2]) / 2, cy + i * lh), s, font=font, fill=INK, anchor="mm")

    def _draw_chart(
        self,
        img,
        d,
        chart: dict,
        x0: float,
        x1: float,
        y: float,
        h: float,
        accent,
        label_font,
        title_font,
    ) -> None:
        """价格趋势折线图（§CHART 行）：坐标轴 + 网格 + 折线 + 最高/最低/最新标注。

        坐标：``x0/x1`` 是横向可用区（光栅像素），``y`` 是行顶、``h`` 是行高。
        画法固定四层：底板 → 网格与刻度 → 折线 → 三个关键点标注。配色全部取
        主题槽位（底板 PANEL_ALT、网格 PANEL_LINE、刻度 INK_DIM、折线 accent），
        所以**亮暗主题都成立**（主题改的是这些槽位本身，不是写死的颜色）。
        空值（该小时/该日无成交）断开折线，不连成假直线。
        """
        vals: list[Optional[float]] = chart.get("values") or []
        labs: list[str] = chart.get("labels") or []
        unit = chart.get("unit") or ""
        n = len(vals)
        px0 = x0 + CHART_PAD_L * SS
        px1 = x1 - CHART_PAD_R * SS
        py0 = y + CHART_PAD_T * SS
        py1 = y + h - CHART_PAD_B * SS
        if n < 2 or px1 - px0 < 60 * SS or py1 - py0 < 40 * SS:
            return
        plot = (px0, py0, px1, py1)
        # —— 底板：整行一块圆角板，折线不会「浮」在背景图上 ——
        _translucent_fill(img, (x0, y + 2 * SS, x1, y + h - 2 * SS), PANEL_ALT, 1.0, 10 * SS)
        d.rounded_rectangle(
            (x0, y + 2 * SS, x1, y + h - 2 * SS), radius=10 * SS, outline=PANEL_LINE, width=SS
        )
        # —— 标题带（超宽截断：标题来自机器行字段，长度不受本函数控制）——
        ttl = chart.get("title") or ""
        if unit:
            ttl = f"{ttl}（{unit}）"
        if ttl:
            ttl = _fit_title(d, title_font, ttl, (x1 - 14 * SS) - (x0 + 14 * SS))
            d.text((x0 + 14 * SS, y + 5 * SS), ttl, font=title_font, fill=INK_DIM)
        # —— 纵轴值域：上下各留 8% 余量，全等序列也要有高度（否则除零）——
        known = [v for v in vals if v is not None]
        lo, hi = min(known), max(known)
        if hi <= lo:
            span = max(1.0, abs(hi) * 0.1)
            lo, hi = lo - span, hi + span
        else:
            pad = (hi - lo) * 0.08
            lo, hi = lo - pad, hi + pad
        if lo < 0:
            lo = 0.0

        def _xv(i: int) -> float:
            return px0 + (px1 - px0) * i / (n - 1)

        def _yv(v: float) -> float:
            return py1 - (py1 - py0) * (v - lo) / (hi - lo)

        # —— 网格 + 纵轴刻度（含顶/底）——
        for gi in range(CHART_AXIS_LINES):
            t = gi / (CHART_AXIS_LINES - 1)
            gy = py1 - (py1 - py0) * t
            d.line([(px0, gy), (px1, gy)], fill=PANEL_LINE, width=max(1, SS // 2))
            # 刻度标签纵向收进画区：顶/底那条网格线的标签居中画会探进标题带
            # 与横轴标签带（2026-10-23 实测「48h 中位走势」被 270p 压住）。
            _lh = label_font.size
            ly = min(max(gy, py0 + _lh / 2), py1 - _lh / 2)
            d.text(
                (px0 - 12 * SS, ly),
                f"{_fmt_num(lo + (hi - lo) * t)}{unit}",
                font=label_font,
                fill=INK_DIM,
                anchor="rm",
            )
        # —— 横轴刻度：首/中/尾三点，避免 48 个小时标签糊成一片 ——
        # 首点左对齐、末点右对齐：居中画会有一半探进左侧价格沟里（与底部的
        # 纵轴刻度标签叠在一起）。
        for ti in sorted({0, n // 2, n - 1}):
            lab = labs[ti] if ti < len(labs) else ""
            if not lab:
                continue
            d.text(
                (_xv(ti), py1 + 6 * SS),
                lab,
                font=label_font,
                fill=INK_FAINT,
                anchor="la" if ti == 0 else ("ra" if ti == n - 1 else "ma"),
            )
        # —— 折线：空值断段（相邻索引才连），末端圆角接合 ——
        prev: Optional[tuple[float, float]] = None
        for i, v in enumerate(vals):
            if v is None:
                prev = None
                continue
            pt = (_xv(i), _yv(v))
            if prev is not None:
                d.line([prev, pt], fill=accent, width=CHART_LINE_W * SS, joint="curve")
            prev = pt
        # —— 三个关键点：最高 / 最低 / 最新，各自标数值 + 时间 ——
        idx = [i for i, v in enumerate(vals) if v is not None]
        hi_i = max(idx, key=lambda i: vals[i])
        lo_i = min(idx, key=lambda i: vals[i])
        last_i = idx[-1]
        # 同一索引可能身兼数职：单调上升的序列上「最高」就是「最新」，全程持平的序列
        # 「最高」就是「最低」。合成一条标注 —— 拆成两条会抢同一位置，去重逻辑只留先放的
        # 那条，用户就看不到「最新」了（2026-10-23 实测）。
        roles: dict[int, list[str]] = {}
        for _tag, _i in (("最高", hi_i), ("最低", lo_i), ("最新", last_i)):
            roles.setdefault(_i, []).append(_tag)
        marks = []
        for _i, _tags in roles.items():  # dict 保序 = 最高 → 最低 → 最新（先放的占好位置）
            if "最低" in _tags and "最高" not in _tags:
                _col, _side = RED, ("down", "up")
            elif "最高" in _tags:
                _col, _side = GREEN, ("up", "down")
            else:
                _col, _side = GOLD_BRIGHT, ("up", "down")
            marks.append((_i, _col, "·".join(_tags), _side))
        placed: list[tuple[float, float, float, float]] = []
        for mi, mcol, mtag, sides in marks:
            mv = vals[mi]
            cx, cy = _xv(mi), _yv(mv)
            d.ellipse(
                [
                    cx - CHART_DOT_R * SS,
                    cy - CHART_DOT_R * SS,
                    cx + CHART_DOT_R * SS,
                    cy + CHART_DOT_R * SS,
                ],
                fill=mcol,
                outline=PANEL_BG,
                width=SS,
            )
            lab = labs[mi] if mi < len(labs) else ""
            head = f"{mtag} {_fmt_num(mv)}{unit}"
            # 两行小片（数值行 + 时间行）比一行窄近一半 —— 48 小时序列上「最高」与
            # 「最新」常常只隔几个点，一行装不下两个标注。
            variants = [[head, lab] if lab else [head], [head]]
            # 先按偏好面（最高朝上、最低朝下、最新朝上）放；放不下或撞上已放的标注就
            # 翻面，再不行丢掉时间行 —— 宁可少一行时间，也不把字压出画区或互相盖住。
            rect: Optional[tuple[float, float, float, float]] = None
            chosen: list[str] = []
            for side in sides:
                for lines_ in variants:
                    cand = self._chart_label_rect(d, cx, cy, lines_, label_font, side, plot)
                    if cand is not None and not any(
                        cand[0] < p[2] and cand[2] > p[0] and cand[1] < p[3] and cand[3] > p[1]
                        for p in placed
                    ):
                        rect, chosen = cand, lines_
                        break
                if rect is not None:
                    break
            if rect is not None:
                self._draw_chart_label(img, d, rect, chosen, label_font)
                placed.append(rect)

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
                fill=accent,
            )
            ty = y + (_TENT_PAD_TOP + _TENT_HEAD_H) * SS
            for name in parts[1:]:
                color = BLUE if _tent_norm(name) in _TENT_BLUE_TASKS else INK
                _tb = d.textbbox((0, 0), name, font=font, anchor="la")
                d.text(
                    (bx + (bw - (_tb[2] - _tb[0])) / 2 - _tb[0], ty - _tb[1]),
                    name,
                    font=font,
                    fill=color,
                )
                ty += _TENT_LINE_H * SS

    def _draw_task_rest(self, d, rest: str, x: float, y: float, font, max_w: float) -> None:
        """赏金「任务：**类型** **挑战名**」两段上色。

        维护者要求任务类型与挑战名各自有颜色（「任务类型现在像和后面文字是一块的」）。
        ★ 目标描述段已于 2026-10-02 整体移除（玩家反馈「任务描述没任何作用」），
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
            d.text((x, y), text, font=font, fill=color)
            x += _tlen(d, font, text)
            if i < len(segs) - 1:
                x += gap

    # 语法文档类卡片（指令一览等 _plain_body）的配色：
    # 指令列亮色 + 说明列次色，合并词条「A / B / C」逐个异色（2026-09-14 维护者要求
    # 「介绍和指令别混在一起」「本质三个指令的用三个颜色」）。
    _HELP_CMD_COLORS = (GOLD_BRIGHT, CYAN, VIOLET, GREEN)
    # 说明列专用暗色：比 INK_DIM 再压一档（2026-09-14 玩家反馈「指令和介绍
    # 颜色太接近，不放大容易混在一起」——金色指令列 vs 暗灰说明列才够分明）。
    HELP_DESC_COLOR = (150, 158, 172)

    def _draw_plain_cell(self, d, ci: int, cell: str, cx: float, y: float, font) -> None:
        """_plain_body 表格单元格：指令列上色、说明列压暗。"""
        if not cell:
            return
        if ci != 0:
            d.text((cx, y), cell, font=font, fill=self.HELP_DESC_COLOR)
            return
        parts = cell.split(" / ")
        if len(parts) == 1:
            d.text((cx, y), cell, font=font, fill=GOLD_BRIGHT)
            return
        x = cx
        sep = " / "
        for i, p in enumerate(parts):
            col = self._HELP_CMD_COLORS[i % len(self._HELP_CMD_COLORS)]
            d.text((x, y), p, font=font, fill=col)
            x += _tlen(d, font, p)  # ★ 记忆化：指令片段/分隔符重复测量归零
            if i < len(parts) - 1:
                d.text((x, y), sep, font=font, fill=GOLD_DIM)
                x += _tlen(d, font, sep)

    def _draw_tokens(self, d, text: str, x: float, y: float, font, base_color, max_w: float):
        """按语义 token 上色绘制（行内高亮）+ 行内极性图标。

        文本含 ``⟦pol:<key>⟧`` 标记时，就地画小图标再继续画后面的文本；
        无标记时与原实现完全一致（零行为变化）。返回绘制结束的 x。
        """
        # ★ 2026-10-16：颜色染段标记（⟦g⟧…⟦/g⟧ 等，见 _SPAN_COLORS）在任何绘制路径都生效 ——
        #   此前只有 kv 行与普通条目行先过 _parse_color_segs，表格单元格直接进来会把标记原样画出（方块）。
        if text and "⟦" in text and _SEG_MARK_RE.search(text):
            color = None
            for part in _SEG_MARK_RE.split(text):
                if not part:
                    continue
                mm = re.fullmatch(r"⟦(/?)([a-z])⟧", part)
                if mm:
                    color = None if mm.group(1) else _span_color(mm.group(2))
                    continue
                if color is None:
                    x = self._draw_tokens(d, part, x, y, font, base_color, max_w)
                else:
                    d.text((x, y), part, font=font, fill=color)
                    x += _tlen(d, font, part)
            return x
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
        canvas = getattr(d, "_image", None)  # 出图画布（RGB）
        if canvas is None:  # pragma: no cover - 兜底
            return 0.0
        canvas.paste(small, (int(x), int(y - 2 * SS)), small)
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
                d.text((x, y), seg, font=font, fill=base_color)
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
                if (
                    special is not None
                    and not (getattr(self, "_bounty_mode", False) and tok in _BOUNTY_PLAIN_FACTION)
                    and not (
                        getattr(self, "_baro_mode", False)
                        and (tok in _MISSION_TYPE_SET or tok in _ELEMENT_SET)
                    )
                ):
                    color = special
            wch = _tlen(d, font, tok)  # ★ 记忆化：高频 token（生存/剩余…）只测一次
            if (
                not plain_body
                and not has_inner
                and tok in _MARK_CHIPS
                and not getattr(self, "_bounty_mode", False)
            ):
                # 小号标记芯片（钢铁 / 九重天）：比正文小一号、描边 + 淡底，不再整词大红字
                mcol = _MARK_CHIPS[tok]
                mf = self.fonts.get("bold", max(14, int(getattr(font, "size", 60) / SS * 0.66)))
                mw = _tlen(d, mf, tok)
                gap, pad = 6 * SS, 9 * SS
                mh = int(getattr(mf, "size", 40) * 1.5)
                if pos > 0:
                    x += gap
                if x + mw + pad * 2 <= max_w:
                    ref = d.textbbox((0, 0), "国Ag", font=font, anchor="la")
                    cy = y + (ref[1] + ref[3]) / 2
                    cb = (int(x), int(cy - mh / 2), int(x + mw + pad * 2), int(cy + mh / 2))
                    _chamfer(d, cb, 5 * SS, fill=_tint(mcol, 56), outline=mcol)
                    mb = d.textbbox((0, 0), tok, font=mf, anchor="la")
                    d.text(
                        (x + pad, cy - (mb[1] + mb[3]) / 2),
                        tok,
                        font=mf,
                        fill=mcol,
                    )
                    x = cb[2] + gap
                    pos = m.end()
                    continue
                x -= gap if pos > 0 else 0
            if chip:
                gap, pad, bh = 7 * SS, 10 * SS, 40 * SS
                if pos > 0:
                    x += gap
                if x + wch + pad * 2 > max_w:  # 越界则退回纯文字
                    chip = False
                    x -= gap if pos > 0 else 0
            if chip:
                # 芯片竖向对齐到正文行的视觉中心（旧版固定偏上 3px，纪元芯片比文字略高）
                ref = d.textbbox((0, 0), "国Ag", font=font, anchor="la")
                cy = y + (ref[1] + ref[3]) / 2
                cb = (int(x - pad), int(cy - bh / 2), int(x + wch + pad), int(cy + bh / 2))
                _chamfer(d, cb, 8 * SS, fill=_tint(color, 72), outline=color)
                d.text((x, y), tok, font=font, fill=color)
                x = cb[2] + gap
            else:
                d.text((x, y), tok, font=font, fill=color)
                x += wch
            pos = m.end()
        if pos < len(text):
            tail = text[pos:]
            d.text((x, y), tail, font=font, fill=base_color)
            x += _tlen(d, font, tail)
        return x

    @staticmethod
    def _diamond(cx: float, cy: float, r: float):
        return [(cx, cy - r), (cx + r, cy), (cx, cy + r), (cx - r, cy)]

    def _cleanup(self, keep: int = 6):
        try:
            cards = sorted(self.cache_dir.glob("card_*.png"), key=lambda p: p.stat().st_mtime)
            for old in cards[:-keep]:
                old.unlink()
        except OSError:
            pass


# ===========================================================================
# 主题（运维面板「外观」，2026-10-16 维护者要求：可调颜色 / 背景图 / 角标，附实时预览）
# ===========================================================================
# 设计：
#   · 默认主题 = 上面的常量原值；**不覆盖任何项时像素级不变**（tests/test_render_* 的对拍依赖这一点）。
#   · 颜色常量大多在绘制时按全局名读取，直接改模块全局即可；但 TIER_COLOR / TITLE_THEME / _TOKEN_RULES
#     等表在 import 时就把颜色值抄进去了 ⇒ 首次换肤时给这些表拍快照，每次 apply_theme 从快照
#     按「原值 → 新值」重建（只换 _CONTAINER_CONSTS 里那些色，警示竖条与琥珀同值，不能混在一起换）。
#   · 派生色（GOLD_DIM / PANEL_ALT / INK_FAINT …）只在其来源色被改时才按比例重算，系数按默认值反推。
#   · 主题是**模块级**状态：主渲染器与面板预览共用 —— 预览要在 main 的渲染锁里「套用→渲染→还原」。
THEME_SLOTS: list[tuple[str, str, str, str, str]] = [
    # (键, 名称, 影响的部分, 常量名, 分组)
    ("bg", "背景", "整张卡的底色（竖向渐变，底部自动压暗一档）", "BG_TOP", "base"),
    (
        "accent",
        "强调色",
        "外框描边、表头分隔线、KV 标签、未单独配色的栏目色条 / 页码 / 序号底；水印与注释竖条按它自动推算",
        "GOLD",
        "base",
    ),
    (
        "highlight",
        "高亮色",
        "四角金饰、分区标题文字、序号数字、★ / 满级 / 杜卡德 100 档",
        "GOLD_BRIGHT",
        "base",
    ),
    ("ink", "正文", "标题与正文文字", "INK", "base"),
    (
        "ink_dim",
        "次要文字",
        "副行、多列的非首列、页脚；注释文字与分隔细线按它自动推算",
        "INK_DIM",
        "base",
    ),
    ("panel", "面板", "正文面板底色；斑马纹、注释面板、档位框按它自动推算", "PANEL_BG", "base"),
    ("line", "描边线", "面板描边、分区标题后的延伸线", "PANEL_LINE", "base"),
    ("warn", "警示", "含 ⚠ 的警示面板左侧竖条（面板底色自动推算）", "WARN_BAR", "base"),
    ("cyan", "青", "任务类型、裂隙 / 仲裁栏目、寒冷", "CYAN", "adv"),
    ("violet", "紫", "赏金挑战、MOD、夜晚、安魂", "VIOLET", "adv"),
    ("red", "红", "钢铁、S 级、下跌 ▼、警报 / 赤毒栏目", "RED", "adv"),
    ("green", "绿", "上涨 ▲、在线、A 级、已抵达", "GREEN", "adv"),
    ("amber", "琥珀", "倒计时、平原 / 赏金栏目、「剩 / 已结束」", "AMBER", "adv"),
    ("blue", "蓝", "染色段、点位、新闻栏目", "BLUE", "adv"),
    ("magenta", "品红", "电波 / 对话栏目、外观 / 精英标签", "MAGENTA", "adv"),
    ("teal", "青绿", "伤害元素类型、先锋纪元、特惠栏目", "TEAL", "adv"),
    ("steel", "钢蓝", "前纪、罕见 / 常驻标签、页码", "STEEL", "adv"),
    ("blueprint", "橙金", "蓝图 ▣", "BLUEPRINT", "adv"),
    ("rarity_silver", "银", "杜卡德 45 / 65 档部件", "RARITY_SILVER", "adv"),
    ("rarity_bronze", "铜", "杜卡德 15 / 25 档部件", "RARITY_BRONZE", "adv"),
]
_DERIVED = (
    "BG_BOT",
    "GOLD_DIM",
    "GOLD_FAINT",
    "NOTE_BAR",
    "TIER_FRAME_LINE",
    "RARITY_GOLD",
    "BONUS_COLOR",
    "INK_FAINT",
    "HAIR",
    "PANEL_ALT",
    "NOTE_BG",
    "TIER_FRAME_FILL",
    "WARN_BG",
    "MISSION_TYPE_COLOR",
    "CHALLENGE_COLOR",
    "ELEMENT_COLOR",
)
# 会被抄进表里的颜色（重建表时按「原值 → 新值」替换）；值两两不同，替换不会串
_CONTAINER_CONSTS = (
    "GOLD",
    "GOLD_BRIGHT",
    "AMBER",
    "CYAN",
    "VIOLET",
    "RED",
    "GREEN",
    "BLUE",
    "MAGENTA",
    "TEAL",
    "STEEL",
    "BLUEPRINT",
    "INK",
    "INK_DIM",
    "INK_FAINT",
    "RARITY_SILVER",
    "RARITY_BRONZE",
)
_CONTAINERS = (
    "TIER_COLOR",
    "GROUP_CHIP_COLOR",
    "_SPAN_COLORS",
    "TITLE_THEME",
    "_TOKEN_RULES",
    "_MARK_CHIPS",
)
_THEME_DEFAULTS: dict[str, tuple[int, int, int]] = {
    n: globals()[n]
    for n in {c for *_, c, _g in THEME_SLOTS} | set(_DERIVED) | set(_CONTAINER_CONSTS)
}
_THEME_SNAPSHOT: dict = {}
_THEME: dict = {"bg_image": None, "bg_dim": 0.55, "panel_alpha": 1.0, "logo": None, "colors": {}}


def theme_defaults() -> dict[str, str]:
    """各槽位默认色（#rrggbb）。"""
    return {k: "#%02x%02x%02x" % _THEME_DEFAULTS[c] for k, _l, _w, c, _g in THEME_SLOTS}


def parse_hex_color(v) -> Optional[tuple[int, int, int]]:
    m = re.fullmatch(r"#?([0-9a-fA-F]{6})", str(v or "").strip())
    if not m:
        return None
    h = m.group(1)
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def _sub_colors(obj, cmap: dict):
    """深拷贝式替换：RGB 三元组按 cmap 换，其余（字符串、正则、嵌套结构）原样保留。"""
    if isinstance(obj, tuple) and len(obj) == 3 and all(isinstance(x, int) for x in obj):
        return cmap.get(obj, obj)
    if isinstance(obj, tuple):
        return tuple(_sub_colors(x, cmap) for x in obj)
    if isinstance(obj, list):
        return [_sub_colors(x, cmap) for x in obj]
    if isinstance(obj, dict):
        return {k: _sub_colors(v, cmap) for k, v in obj.items()}
    return obj


def apply_theme(
    colors: Optional[dict] = None,
    bg_image=None,
    bg_dim: float = 0.55,
    panel_alpha: float = 1.0,
    logo=None,
) -> None:
    """套用主题（colors：槽位键 → RGB 或 #hex；不给的用默认）。幂等，可反复调用 / 还原。

    bg_image / logo：PIL Image 或 None；bg_dim：背景图上压一层底色的不透明度（越大越暗、文字越清楚）；
    panel_alpha：有背景图时面板的不透明度（1 = 不透明，背景只在边框与留白处露出）。
    """
    g = globals()
    if not _THEME_SNAPSHOT:
        _THEME_SNAPSHOT.update({n: g[n] for n in _CONTAINERS})
        _THEME_SNAPSHOT["_HELP_CMD_COLORS"] = ImageRenderer._HELP_CMD_COLORS
    vals = dict(_THEME_DEFAULTS)
    changed: set[str] = set()
    for key, _l, _w, const, _g in THEME_SLOTS:
        raw = (colors or {}).get(key)
        rgb = raw if isinstance(raw, tuple) else parse_hex_color(raw)
        if rgb is not None and tuple(rgb) != _THEME_DEFAULTS[const]:
            vals[const] = tuple(rgb)
            changed.add(key)
    v = vals
    if "bg" in changed:
        v["BG_BOT"] = _mix(v["BG_TOP"], (0, 0, 0), 0.24)
    if changed & {"accent", "bg", "panel"}:
        v["GOLD_DIM"] = _mix(v["GOLD"], v["BG_BOT"], 0.46)
        v["GOLD_FAINT"] = _mix(v["GOLD"], v["BG_BOT"], 0.575)
        v["NOTE_BAR"] = _mix(v["GOLD"], v["PANEL_BG"], 0.62)
        v["TIER_FRAME_LINE"] = _mix(v["GOLD"], v["PANEL_BG"], 0.72)
    if "highlight" in changed:
        v["RARITY_GOLD"] = v["BONUS_COLOR"] = v["GOLD_BRIGHT"]
    if changed & {"ink_dim", "panel"}:
        v["INK_FAINT"] = _mix(v["INK_DIM"], v["PANEL_BG"], 0.24)
        v["HAIR"] = _mix(v["PANEL_BG"], v["INK_DIM"], 0.14)
    if changed & {"panel", "ink", "bg"}:
        v["PANEL_ALT"] = _mix(v["PANEL_BG"], v["INK"], 0.025)
        v["NOTE_BG"] = _mix(v["PANEL_BG"], v["BG_BOT"], 0.4)
        v["TIER_FRAME_FILL"] = _mix(v["PANEL_BG"], v["INK"], 0.018)
    if changed & {"warn", "bg"}:
        v["WARN_BG"] = _mix(v["WARN_BAR"], v["BG_BOT"], 0.87)
    v["MISSION_TYPE_COLOR"], v["CHALLENGE_COLOR"], v["ELEMENT_COLOR"] = (
        v["CYAN"],
        v["VIOLET"],
        v["TEAL"],
    )
    for name, rgb in v.items():
        g[name] = rgb
    cmap = {_THEME_DEFAULTS[c]: v[c] for c in _CONTAINER_CONSTS if v[c] != _THEME_DEFAULTS[c]}
    for name in _CONTAINERS:
        g[name] = _sub_colors(_THEME_SNAPSHOT[name], cmap)
    ImageRenderer._HELP_CMD_COLORS = _sub_colors(_THEME_SNAPSHOT["_HELP_CMD_COLORS"], cmap)
    g["_BG_COL"] = None
    _TOKEN_STYLE.clear()
    _THEME.update(
        bg_image=bg_image,
        bg_dim=max(0.0, min(0.95, float(bg_dim))),
        panel_alpha=max(0.3, min(1.0, float(panel_alpha))),
        logo=logo,
        colors={k: "#%02x%02x%02x" % v[c] for k, _l, _w, c, _g in THEME_SLOTS if k in changed},
    )


def theme_state() -> dict:
    """当前生效的主题（预览后还原用）。"""
    return {
        "colors": dict(_THEME["colors"]),
        "bg_image": _THEME["bg_image"],
        "bg_dim": _THEME["bg_dim"],
        "panel_alpha": _THEME["panel_alpha"],
        "logo": _THEME["logo"],
    }


def _panel_translucent() -> bool:
    return _THEME["bg_image"] is not None and _THEME["panel_alpha"] < 0.999


def _with_bg_image(canvas: "Image.Image", src: "Image.Image", dim: float) -> "Image.Image":
    """背景图按 cover 缩放铺满画布，再压一层底色渐变（dim = 底色的不透明度），保证文字对比度。"""
    W_, H_ = canvas.size
    sw, sh = src.size
    k = max(W_ / sw, H_ / sh)
    im = src.convert("RGB").resize(
        (max(1, int(sw * k + 0.5)), max(1, int(sh * k + 0.5))), Image.BILINEAR
    )
    l_, t_ = (im.width - W_) // 2, (im.height - H_) // 2
    im = im.crop((l_, t_, l_ + W_, t_ + H_))
    return Image.blend(im, canvas, max(0.0, min(1.0, dim)))


def _translucent_fill(img: "Image.Image", rect, color, alpha: float, radius: int) -> None:
    """半透明填充：按形状画遮罩，用纯色带遮罩贴上去（PIL 的 paste+mask 就是 alpha 混合）。"""
    x0, y0, x1, y1 = (int(round(x)) for x in rect)
    w, h = max(1, x1 - x0), max(1, y1 - y0)
    m = Image.new("L", (w, h), 0)
    md = ImageDraw.Draw(m)
    a = int(255 * max(0.0, min(1.0, alpha)))
    if radius:
        md.rounded_rectangle([0, 0, w - 1, h - 1], radius=radius, fill=a)
    else:
        md.rectangle([0, 0, w - 1, h - 1], fill=a)
    img.paste(tuple(color), (x0, y0, x0 + w, y0 + h), m)


def _paste_logo(img: "Image.Image", logo: "Image.Image", right: int, yc: int, height: int) -> None:
    lg = logo.convert("RGBA")
    k = height / max(1, lg.height)
    lg = lg.resize((max(1, int(lg.width * k)), height), Image.LANCZOS)
    img.paste(lg, (right - lg.width, int(yc - height / 2)), lg)
