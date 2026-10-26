# -*- coding: utf-8 -*-
"""趋势折线图（§CHART 图表行）回归。

背景（维护者 2026-10-23）：「趋势」卡原先用 `_spark()` 输出 `▁▂▃…█` 方块字 ——
没有坐标轴、看不出最高/最低是哪一刻、线条「看不见」。本批改为：格式层输出
`§CHART{json}` 机器行，渲染层画**真实折线图**（纵轴价格刻度 + 横轴时间刻度 +
最高/最低/最新直接标数值与时间）；纯文字模式 / 渲染器不可用时回退方块字，
**并额外补一行「最高 X p（时间）｜最低 Y p（时间）｜最新 Z p」**。

钉住四件事：
① 图表行解析（坏 JSON / 点太少 / 长度不齐 / 非机器行一律不认，且**绝不把裸 JSON
   漏给用户**）；
② 降级文字必须含最高 / 最低 / 最新（这是用户原始诉求：文字模式也要说清楚）；
③ 极端序列（全相同 / 只有 2 点 / 有空值）不出异常、不画假线；
④ 出图后正文不越出面板右缘（口径同 tests/test_render_overflow.py），亮暗两主题都过。
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import formatters as F  # noqa: E402
from core import render as R  # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = ""):
    print(
        ("[PASS] " if cond else "[FAIL] ")
        + name
        + (f"  -> {detail}" if detail and not cond else "")
    )
    if not cond:
        FAILED.append(name)


def cline(title: str, labels: list, values: list, unit: str = "p") -> str:
    return "§CHART" + json.dumps(
        {"title": title, "unit": unit, "labels": labels, "values": values},
        ensure_ascii=True,
        separators=(",", ":"),
    )


# ---------------------------------------------------------------------------
# ① 图表行解析
# ---------------------------------------------------------------------------
ok = R.parse_chart(cline("48h 中位走势", ["10-21 00时", "10-21 01时"], [100, 200]))
check("正常行可解析", ok is not None and ok["values"] == [100.0, 200.0], str(ok))
check("标题/单位/标签原样带出", ok and ok["title"] == "48h 中位走势" and ok["unit"] == "p")

# 0 → null（该小时无成交）：解析成 None，且**不**被当成 0 参与最高/最低
z = R.parse_chart(cline("x", ["a", "b", "c"], [0, 150, 200]))
check("0 值记 null（无成交不参与最高最低）", z and z["values"] == [None, 150.0, 200.0], str(z))

REJECT = [
    ("非机器行", "· 48h 中位走势　▁▂▃"),
    ("坏 JSON", "§CHART{not json"),
    ("只有 1 个点", cline("x", ["a"], [1])),
    ("全部为空", cline("x", ["a", "b"], [0, 0])),
    ("values 不是列表", "§CHART" + json.dumps({"values": "nope"})),
    ("空串", ""),
]
for _n, _s in REJECT:
    check(f"拒绝：{_n}", R.parse_chart(_s) is None)

# 标签长度不齐 → 补空串（轴标签缺失不影响出图）
mis = R.parse_chart(cline("x", ["a"], [1, 2, 3]))
check("标签不齐时补空串", mis is not None and mis["labels"] == ["a", "", ""], str(mis))

# ---------------------------------------------------------------------------
# ② 降级文字：必须含最高 / 最低 / 最新
# ---------------------------------------------------------------------------
ch = R.parse_chart(
    cline("48h 中位走势", ["10-21 00时", "10-21 05时", "10-21 23时"], [100, 320, 180])
)
txt = R.chart_text_lines(ch)
joined = "\n".join(txt)
for _need in ("最高 320p", "最低 100p", "最新 180p"):
    check(f"降级文字含「{_need}」", _need in joined, joined)
check("降级文字含时间", "10-21 05时" in joined and "10-21 00时" in joined, joined)
check("降级文字保留方块字走势条", "▁" in joined or "█" in joined, joined)

# 空值不参与最高/最低（否则「最低 0p」会把用户吓一跳）
ch_gap = R.parse_chart(cline("x", ["a", "b", "c"], [0, 150, 200]))
g = "\n".join(R.chart_text_lines(ch_gap))
check(
    "空值不进最高最低",
    "最低 150p" in g and "最低 0p" not in g and "最新 200p" in g,
    g,
)

# 整行经 text_card 后不得残留裸 JSON（坏行/点太少的整行丢掉）
for _n, _s in [("正常", cline("x", ["a", "b"], [1, 2])), ("坏 JSON", "§CHART{oops")]:
    card = R.text_card("测试 价格趋势", ["◆ 走势图", _s], "f")
    check(f"text_card 无裸 JSON 残留（{_n}）", "§CHART" not in card and "{" not in card)

# ---------------------------------------------------------------------------
# ③ 极端序列
# ---------------------------------------------------------------------------
EDGE = {
    "全相同": cline("x", [f"10-21 {i:02d}时" for i in range(6)], [200] * 6),
    "只有 2 点": cline("x", ["10-21 00时", "10-21 01时"], [100, 300]),
    "有空值": cline("x", [f"10-21 {i:02d}时" for i in range(6)], [0, 100, 0, 200, 0, 300]),
    "全 0": cline("x", ["a", "b", "c"], [0, 0, 0]),
}
for _n, _s in EDGE.items():
    _c = R.parse_chart(_s)
    if _n == "全 0":
        check(f"极端序列「{_n}」整行不可用（交由上层不出图）", _c is None)
        continue
    check(f"极端序列「{_n}」可解析", _c is not None)
    if _c:
        _t = "\n".join(R.chart_text_lines(_c))
        check(f"极端序列「{_n}」降级文字有最高+最低", "最高" in _t and "最低" in _t, _t)

# ---------------------------------------------------------------------------
# ④ fmt_trend 产出机器行（而不是方块字），且 0 → null
# ---------------------------------------------------------------------------
H48 = [
    {
        "t": f"2026-10-{21 + i // 24:02d} {i % 24:02d}:00",
        "volume": 1,
        "median": 0 if i == 3 else 100 + i,
        "avg": 100 + i,
        "min": 90,
        "max": 150,
    }
    for i in range(48)
]
D90 = [
    {
        "t": f"2026-08-{i + 1:02d} 00:00",
        "volume": 1,
        "median": 120 + i,
        "avg": 120,
        "min": 100,
        "max": 200,
    }
    for i in range(30)
]
SUMMARY = {"vol48": 48, "median48": 123.4, "avg90": 135.0, "change": 5.0, "last": 147, "first": 140}
_title, lines = F.fmt_trend("绝路Prime", {"h48": H48, "d90": D90}, SUMMARY)
charts = [x for x in lines if x.startswith("§CHART")]
check("fmt_trend 产出 2 条图表行（48h + 90d）", len(charts) == 2, str(len(charts)))
check("走势图分区标题保留", "◆ 走势图" in lines)
check("文字要点行保留（峰值成交 / 48h 最低成交）", any("峰值成交" in x for x in lines))
check("不再输出方块字走势条", not any("▁" in x for x in lines))

c48 = R.parse_chart(charts[0])
check("48h 行 48 点且标签等长", c48 and len(c48["values"]) == 48 == len(c48["labels"]))
check(
    "48h 行无成交点记 null",
    c48 and c48["values"][3] is None,
    str(c48["values"][:6] if c48 else None),
)
check("48h 标签带小时（不是日线口径）", c48 and c48["labels"][0] == "10-21 00时", c48["labels"][0])
c90 = R.parse_chart(charts[1])
check("90d 标签只到日（MM-DD）", c90 and c90["labels"][0] == "08-01", c90["labels"][0])

# 有效点 < 2 的序列不出图，且不留空的分区标题
_t2, _l2 = F.fmt_trend(
    "空物品",
    {
        "h48": [{"t": "2026-10-21 00:00", "volume": 0, "median": 0, "avg": 0, "min": 0, "max": 0}],
        "d90": [],
    },
    SUMMARY,
)
check("无有效成交时不出图、不留空分区标题", "◆ 走势图" not in _l2, str(_l2))

# _short_t 口径由调用方给（48 小时线点数比 90 天日线还多，不能按长度猜）
check("_short_t 小时口径", F._short_t("2026-10-21 13:00", True) == "10-21 13时")
check("_short_t 日线口径", F._short_t("2026-10-21 00:00", False) == "10-21")

# ---------------------------------------------------------------------------
# ④b 图表标题：**只在真的超宽时**才截断 + 补省略号
#     （线上实测过：原写法无条件补「…」，短标题也被缀上 —— 每张趋势图都成了
#      「48h 中位走势（p）…」）
# ---------------------------------------------------------------------------
if R.Image is None:
    print("[SKIP] 未安装 Pillow，跳过标题截断检查")
else:
    _d = R._measure_draw()
    _f = None
    try:
        _probe_fonts = R._Fonts()
        _f = _probe_fonts.get("bold", 22)
    except Exception:  # noqa: BLE001
        _f = None
    if _f is None:
        print("[SKIP] 无可用字体，跳过标题截断检查")
    else:
        _short = "48h 中位走势（p）"
        _w = _d.textlength(_short, font=_f)
        check("短标题原样返回（不补省略号）", R._fit_title(_d, _f, _short, _w + 50) == _short)
        check("短标题即便刚好放得下也不补省略号", R._fit_title(_d, _f, _short, _w) == _short)
        _long = "48h 中位走势（p）" + "非常长的图表标题" * 6
        _got = R._fit_title(_d, _f, _long, _w)
        check("超宽标题被截断并补省略号", _got.endswith("…") and _got != _long, _got[:40])
        check("截断后宽度不超预算", _d.textlength(_got, font=_f) <= _w + 1, str(_got))
        check("预算极小时退化为空串（不抛异常）", R._fit_title(_d, _f, _long, 1) == "")
        check("空标题返回空串", R._fit_title(_d, _f, "", 100) == "")

# ---------------------------------------------------------------------------
# ⑤ 出图：正文不越出面板右缘（口径同 tests/test_render_overflow.py，默认主题）
#     + 亮暗两主题下折线与底板、标注文字与标注底的**对比度**（「线条看不见」的判据）
# ---------------------------------------------------------------------------
LUM = 125


def right_overflow(p: str, safe_from_edge: int = 30):
    """返回（最右越界 x，越界像素数）；正文带不含四角装饰。"""
    im = R.Image.open(p).convert("RGB")
    W, H = im.size
    rgb = im.load()
    total, maxx = 0, 0
    for x in range(W - safe_from_edge, W):
        for y in range(150, H - 96):
            r, g, b = rgb[x, y]
            if 0.2126 * r + 0.7152 * g + 0.0722 * b > LUM:
                total += 1
                maxx = max(maxx, x)
    return maxx, total


def _rel_lum(c) -> float:
    """WCAG 相对亮度（sRGB）。"""
    ch = []
    for v in c:
        v /= 255
        ch.append(v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4)
    return 0.2126 * ch[0] + 0.7152 * ch[1] + 0.0722 * ch[2]


def _contrast(a, b) -> float:
    la, lb = _rel_lum(a), _rel_lum(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


# 亮主题：只改主题槽位（渲染器全程读槽位，不改代码）——「亮暗都出图核对」的一半。
# 颜色选「浅底 + 深字」的常见浅色主题。
LIGHT = {
    "bg": "#f2efe6",
    "ink": "#1b1b1f",
    "ink_dim": "#4a4a55",
    "panel": "#ffffff",
    "line": "#c9bfa4",
    "accent": "#8a6d1f",
    "highlight": "#6b5010",
}

if R.Image is None:
    print("[SKIP] 未安装 Pillow，跳过出图检查")
else:
    probe = R.ImageRenderer(Path(tempfile.mkdtemp(prefix="sdjk_chart_")))
    if not probe.available:
        print("[SKIP] 无可用 CJK 字体，跳过出图检查")
    else:
        CASES = {
            "常规双图": (lines, None),
            "全相同": (["◆ 走势图", EDGE["全相同"]], None),
            "只有 2 点": (["◆ 走势图", EDGE["只有 2 点"]], None),
            "有空值": (["◆ 走势图", EDGE["有空值"]], None),
        }
        for _n, (_ln, _) in CASES.items():
            p = probe.render("绝路Prime 价格趋势", _ln, "warframe.market 统计 · PC")
            if not p:
                check(f"出图：{_n}", False, "render() 返回 None 已降级文本")
                continue
            mx, n = right_overflow(p)
            check(f"出图：{_n} 正文未越出面板右缘", n == 0, f"越界 {n} 像素，最右 x={mx}")

        # 亮主题：出图必须成功（像素越界闸门的 LUM 阈值是按暗底定的，浅底整幅都「亮」，
        # 不能直接套用 —— 浅底改用下面的对比度判据）
        try:
            R.apply_theme(colors=LIGHT)
            p = probe.render("绝路Prime 价格趋势", lines, "warframe.market 统计 · PC")
            check("出图：亮主题渲染成功", bool(p), "render() 返回 None 已降级文本")
            check(
                "亮主题：折线 accent 与底板 PANEL_ALT 对比度 ≥3",
                _contrast(R.GOLD, R.PANEL_ALT) >= 3.0,
                f"对比度={_contrast(R.GOLD, R.PANEL_ALT):.2f}",
            )
            check(
                "亮主题：标注文字 INK 与标注底 PANEL_BG 对比度 ≥4.5",
                _contrast(R.INK, R.PANEL_BG) >= 4.5,
                f"对比度={_contrast(R.INK, R.PANEL_BG):.2f}",
            )
        finally:
            R.apply_theme()  # 还原默认主题，别污染同进程的其它用例
        check(
            "默认主题：折线 accent 与底板 PANEL_ALT 对比度 ≥3",
            _contrast(R.GOLD, R.PANEL_ALT) >= 3.0,
            f"对比度={_contrast(R.GOLD, R.PANEL_ALT):.2f}",
        )
        check(
            "默认主题：标注文字 INK 与标注底 PANEL_BG 对比度 ≥4.5",
            _contrast(R.INK, R.PANEL_BG) >= 4.5,
            f"对比度={_contrast(R.INK, R.PANEL_BG):.2f}",
        )

        # 图表行确实被识别成 chart 行（而不是被当普通正文折行画出来）
        rows: list[dict] = []

        def _fake_classify(r, **_kw):
            rows.extend(r)

        _orig = R._classify_rows
        R._classify_rows = _fake_classify  # type: ignore[assignment]
        try:
            probe.render("绝路Prime 价格趋势", lines, "")
        finally:
            R._classify_rows = _orig  # type: ignore[assignment]
        kinds = [r["kind"] for r in rows]
        check("图表行被识别为 chart 行（未被折行/染色）", kinds.count("chart") == 2, str(kinds))

print()
if FAILED:
    print(f"FAILED {len(FAILED)}: " + ", ".join(FAILED))
    sys.exit(1)

# ★ 2026-10-25（Astra 真实宿主补测）：走势百分比的基线 = 前段均价，括号必须写同一个基线
import core.formatters as _F25  # noqa: E402

_t25, _l25 = _F25.fmt_trend(
    "膛线",
    {"h48": H48, "d90": D90},
    {
        "vol48": 2,
        "median48": 35,
        "avg90": 0,
        "change": -25.0,
        "last": 30,
        "first": 40,
        "base": 40.0,
        "base_span": "48 小时",
    },
)
_w25 = next((x for x in _l25 if x.startswith("◆ 走势")), "")
check(
    "★ 走势括号写真实基线（前段均价），与百分比一致",
    "▼25.0%" in _w25 and "前段均价 40.0p" in _w25,
    _w25,
)


# ZCode 10-25：百分比要能由卡面括号里的两个数复算出来（基线用显示的 1 位小数）
from core.api_client import WarframeClient as _WC25  # noqa: E402

_s25 = _WC25.summarize_stats(
    {
        "h48": [{"median": 35.0, "volume": 1, "avg": 35}],
        "d90": [
            {"median": m, "volume": 1, "avg": m}
            for m in (22.4, 22.6, 22.62, 30, 30, 30, 30, 30, 30)
        ],
    }
)
check(
    "★ 走势百分比 = 卡面两个数复算（最新 / 前段均价 − 1）",
    abs(_s25["change"] - round((_s25["last"] / _s25["base"] - 1) * 100, 1)) < 1e-9,
    str(_s25),
)

print("ALL PASS")
