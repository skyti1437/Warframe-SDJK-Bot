# -*- coding: utf-8 -*-
"""渲染溢出回归测试：python3 tests/test_render_overflow.py

锁住两类「内容压出面板」的回归：

1. **多列表格**（仲裁时间表 6 列）：列对齐时每列按「该列最大宽度」绘制、
   列间固定 24px 间隙，整表宽度 = Σ列宽 + 24×(列数-1)。
   历史 bug：卡宽只按「首列 + 剩余整串」两列估算，第 3..N 列的对齐扩张
   没进卡宽，末尾的评级列直接压到面板边框外。

2. **长标题**：标题字号 46、不换行、无裁剪。历史 bug：遗物列表标题里的
   页码是「共768**个**」，而页码正则只认「共N**条**」→ 页码没被抽走 →
   标题过长压到右上角平台徽章上、再被面板边框裁掉。

判定方式与人工核对一致：渲染成图后按像素扫面板右缘，越界即失败。
缺 Pillow / 字体时（如精简 CI 容器）跳过，不误报。
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import i18n as _i18n  # noqa: E402
from core import render as R  # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = ""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f"  -> {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


# ------------------------------------------------------------------ 子集字符历史


def _check_font_subset_history(builder):
    from unittest.mock import patch

    with tempfile.TemporaryDirectory(prefix="wf-font-charset-") as folder:
        charset = Path(folder) / "charset.txt"
        corpus = Path(folder) / "corpus.py"
        corpus.write_text("源", encoding="utf-8")
        with (
            patch.object(builder, "CHARSET", charset),
            patch.object(builder, "_iter_sources", return_value=[corpus]),
        ):
            initial = builder.collect_chars(extra_cjk=False)
            check("无历史字符表仍可收集当前语料", "源" in initial and not charset.exists())
            independent = chr(0x20000)
            builder.write_charset(initial | {independent})
            corpus.write_text("新", encoding="utf-8")
            shrunk = builder.collect_chars(extra_cjk=False)
            check(
                "语料缩小仍保留已分发字符表及独立字符",
                initial | {independent, "新"} <= shrunk,
            )


from scripts import build_font_subset as _font_subset  # noqa: E402

_check_font_subset_history(_font_subset)


# ------------------------------------------------------------------ 页码正则
# 量词各 handler 不统一：条 / 场 / 个 / 件 / 项
PAGE_CASES = [
    ("仲裁时间表（第1/23页，共336条）", ("1", "23", "336"), "仲裁时间表"),
    ("开核桃建议（第1/4页，共32场）", ("1", "4", "32"), "开核桃建议"),
    (
        "遗物列表：当前可掉落 34，已入库 734（第1/77页，共768个）",
        ("1", "77", "768"),
        "遗物列表：当前可掉落 34，已入库 734",
    ),
    (
        "遗物入库（已入库、不可刷取）（第1/74页，共734个）",
        ("1", "74", "734"),
        "遗物入库（已入库、不可刷取）",
    ),
    ("膛线 在售单（第1/39页，共386条）", ("1", "39", "386"), "膛线 在售单"),
]
for title, groups, rest in PAGE_CASES:
    m = R._PAGE_RE.search(title)
    check(
        f"页码可识别：{title[:20]}…",
        bool(m) and m.groups() == groups,
        f"groups={m.groups() if m else None}",
    )
    stripped = R._PAGE_RE.sub("", title).strip()
    check(f"页码已抽离：{stripped[:20]}…", stripped == rest, stripped)

# ------------------------------------------------------------------ 渲染成图
if R.Image is None:
    print("[SKIP] 未安装 Pillow，跳过渲染溢出检查")
else:
    probe = R.ImageRenderer(Path(tempfile.mkdtemp(prefix="sdjk_render_")))
    if not probe.available:
        print("[SKIP] 无可用 CJK 字体，跳过渲染溢出检查")
    else:
        LUM = 125  # 正文暖白/亮金/青均 > 125；面板底/暗金框约 17~102

        def right_overflow(img_path: str, safe_from_edge: int = 30):
            """返回（最右越界 x，越界像素数）；正文带不含四角装饰。"""
            im = R.Image.open(img_path).convert("RGB")
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

        font = probe.fonts.get("regular", R.FS_BODY)

        def text_width(value):
            return R._inline_width(R._measure_draw(), font, value) / R.SS

        for word in ("categories", "unvaulted", "subscriptions", "caf\u00e9"):
            text = "Use " + word
            parts = probe._wrap(text, font, text_width(text) - text_width(word[-1]) / 2)
            check(f"跨行保留完整拉丁词 {word}", parts == ["Use", word], str(parts))
        word = "subscriptions"
        budget = text_width(word) * 0.6
        parts = probe._wrap(word, font, budget, strict=True)
        check(
            "超过整列宽度的单词才按字符拆分",
            len(parts) > 1
            and "".join(parts) == word
            and all(text_width(p) <= budget for p in parts),
            str(parts),
        )

        lines = ["※ 说明", "◆ 出售参考", "· 出售参考表未列出该武器"]
        path = probe.render("词条推荐", lines)
        with_blank = probe.render("词条推荐", [lines[0], "", "　", "· ", "⟦w⟧ ⟦/w⟧", *lines[1:]])
        check(
            "空白普通行不产生圆角框或额外行高",
            bool(path and with_blank and Path(path).read_bytes() == Path(with_blank).read_bytes()),
        )

        # 1) 仲裁时间表式 6 列（含长节点名 Apollodorus 与可选评级列）
        arb = [
            "12日12时　光理塔　虚空　拦截　奥罗金　A",
            "12日13时　Apollodorus　水星　生存　Infested",
            "12日14时　奥金工场　扎里曼号　虚空决战　Grineer",
            "12日15时　Stöfler　月球　防御　Grineer",
            "12日23时　Alator　火星　拦截　Grineer　S",
            "13日00时　Cinxia　谷神星　拦截　Grineer　A+",
        ]
        p = probe.render("仲裁时间表（第1/23页，共336条）", arb, "")
        if p:
            mx, n = right_overflow(p)
            W = R.Image.open(p).size[0]
            check("6 列表格：正文未越出面板右缘", n == 0, f"W={W} 越界 {n} 像素，最右 x={mx}")
        else:
            check("6 列表格：渲染成功", False, "render() 返回 None 已降级文本")

        # 1b) ★ 2026-10-22：评级列带**社区实测中位数**（「A+* 705」）—— 末列比原来宽 4 字符。
        #     仍是 6 列、仍不得越界（这条正是「加数字后把末列压出面板」的回归哨）。
        arb_med = [
            "12日12时　光理塔　虚空　拦截　奥罗金　A 612",
            "12日13时　Apollodorus　水星　生存　Infested",
            "12日15时　Stöfler　月球　防御　Grineer　A+* 705",
            "13日00时　Cinxia　谷神星　拦截　Grineer　A- 480",
        ]
        p = probe.render("仲裁时间表·北京时间（第1/23页，共336条）", arb_med, "")
        if p:
            mx, n = right_overflow(p)
            W = R.Image.open(p).size[0]
            check(
                "★ 评级带中位数（A+* 705）时正文未越出面板右缘",
                n == 0,
                f"W={W} 越界 {n} 像素，最右 x={mx}",
            )
        else:
            check("★ 评级带中位数：渲染成功", False, "render() 返回 None 已降级文本")

        # 1c) ★ 2026-10-22：「仲裁」卡新增的「下次高效」行（时间｜节点｜类型｜派系｜评级+中位数｜还有 X 小时）
        arb_now = [
            "◆ 当前仲裁：Seimeni（谷神星）　防御　Infested",
            "◆ 评级：A（社区实测中位）　生息效率 638.8/小时（n=824，社区实测中位数）",
            "◆ 本场时段：22:00–23:00（北京时间）｜剩余 49分钟",
            "◆ 下一小时：死灵塔（虚空）　生存　奥罗金　[S 814]",
            "◆ 下次高效：22日23时（北京）　阿波罗多罗斯撞击坑（水星）　生存　Infested　[A+* 705]｜还有 49分钟",
        ]
        p = probe.render("当前仲裁", arb_now, "")
        if p:
            mx, n = right_overflow(p)
            W = R.Image.open(p).size[0]
            check("★「下次高效」长行未越出面板右缘", n == 0, f"W={W} 越界 {n} 像素，最右 x={mx}")
        else:
            check("★「下次高效」长行：渲染成功", False, "render() 返回 None 已降级文本")

        # 1d) ★ 2026-10-23：趋势卡改画**真实折线图**（§CHART 图表行）—— 图表行是
        #     机器行，绘图区铺满正文宽度，刻度标签、三个关键点标注（最高/最低/最新）
        #     都在正文区内。这条哨盯「标注小片被挤到面板外」（首版实测：末点的
        #     「最新」标注贴右缘，标注被整条丢掉）。
        import json as _json

        def _cline(_title, _labels, _vals):
            return "§CHART" + _json.dumps(
                {"title": _title, "unit": "p", "labels": _labels, "values": _vals},
                ensure_ascii=True,
                separators=(",", ":"),
            )

        _h48 = [f"10-21 {i % 24:02d}时" for i in range(48)]
        _trend = [
            "◆ 近 48 小时：120 笔成交｜中位 214.5p｜90 天均价 190.2p",
            "◆ 走势图",
            _cline(
                "48h 中位走势",
                _h48,
                [None if i in (12, 40) else 180 + (i % 7) * 12 for i in range(48)],
            ),
            _cline(
                "90d 中位走势",
                [f"0{(i % 9) + 1}-01" for i in range(90)],
                [120 + i for i in range(90)],
            ),
            "◆ 成交要点",
            "· 峰值成交　07-03｜2 笔｜中位 122p",
        ]
        p = probe.render("绝路Prime 价格趋势", _trend, "warframe.market 统计 · PC")
        if p:
            mx, n = right_overflow(p)
            W = R.Image.open(p).size[0]
            check("★ 趋势折线图未越出面板右缘", n == 0, f"W={W} 越界 {n} 像素，最右 x={mx}")
        else:
            check("★ 趋势折线图：渲染成功", False, "render() 返回 None 已降级文本")

        # 2) 长标题（页码须已被抽走，标题自适应不得压到徽章）
        long_title = "遗物列表：当前可掉落 34，已入库 734（第1/77页，共768个）"
        p = probe.render(
            long_title, ["[可掉落]中纪 Neo T11", "掉落位置：金星 · Romula · 防御（C轮）"], ""
        )
        if p:
            im = R.Image.open(p).convert("RGB")
            W, H = im.size
            rgb = im.load()
            # 徽章右缘 = W-64；标题带里 x ∈ [W-56, W-4] 必须为空
            hot = sum(
                1
                for x in range(W - 56, W - 4)
                for y in range(68, 118)
                if 0.2126 * rgb[x, y][0] + 0.7152 * rgb[x, y][1] + 0.0722 * rgb[x, y][2] > LUM
            )
            check("长标题：未压到右上角徽章", hot == 0, f"W={W} 越界像素={hot}")
        else:
            check("长标题：渲染成功", False, "render() 返回 None 已降级文本")

# ---------------------------------------------------------------------------
# 字体解析（2026-09-25）：用户自放字体必须被采纳
# ---------------------------------------------------------------------------
# 背景：市场/开源包不带字体，图片渲染依赖系统 CJK 字体；AstrBot 更新插件是
# **整包替换**插件目录，手放 core/data/fonts/ 的字体更新后会丢。故渲染器新增
# 「用户字体目录」= plugin_data/<插件>/fonts/（按 cache_dir 的父目录推导），
# 随插件更新保留。
import shutil  # noqa: E402

import core.render as _R  # noqa: E402


def _user_font_checks():
    # 样本只需「一个真实 CJK 字体文件」（下面会临时清空 PACKED/SYSTEM）：先用系统字体，
    # 没有就用随包子集 otf —— 2026-10-16 起不再依赖系统字体，Linux CI / macOS / 开源树都能真跑
    # （此前 SYSTEM 里唯一的 macOS 路径 PingFang.ttc 在实机上不存在 ⇒ Mac 上恒 SKIP）。
    src = next((p for p in list(_R._Fonts.SYSTEM) + list(_R._Fonts.PACKED) if p.exists()), None)
    if src is None:
        print("[SKIP] 无系统 CJK 字体且随包子集字体缺失，跳过用户字体目录用例")
        return
    tmp = Path(tempfile.mkdtemp(prefix="wf_fonts_"))
    _name = "MyCJK" + src.suffix
    try:
        shutil.copy2(src, tmp / _name)
        # ① _Fonts 直接吃用户目录（屏蔽打包/系统候选，证明能独立供字体）
        old_p, old_s = _R._Fonts.PACKED, _R._Fonts.SYSTEM
        _R._Fonts.PACKED, _R._Fonts.SYSTEM = [], []
        try:
            f = _R._Fonts(user_dirs=[tmp])
            check("用户字体目录可独立供字体", f.regular is not None, str(f.regular))
        finally:
            _R._Fonts.PACKED, _R._Fonts.SYSTEM = old_p, old_s
        # ② ImageRenderer 从 cache_dir 父目录推导 fonts/（plugin_data/<插件>/fonts）
        fonts_dir = tmp / "fonts"
        fonts_dir.mkdir(exist_ok=True)
        shutil.copy2(src, fonts_dir / _name)
        _R._Fonts.PACKED, _R._Fonts.SYSTEM = [], []
        try:
            r = _R.ImageRenderer(tmp / "cards")
            check(
                "ImageRenderer 采纳 plugin_data/fonts 下的用户字体",
                r.available is True,
                f"available={r.available}",
            )
        finally:
            _R._Fonts.PACKED, _R._Fonts.SYSTEM = old_p, old_s
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


_user_font_checks()

# ---------------------------------------------------------------------------
# 子集字体（随包分发）覆盖率 + 与完整字库逐像素对拍（2026-09-25）
# ---------------------------------------------------------------------------
# 背景：市场版过去完全依赖系统中文字体（issue #1：用户字体被整包更新带走 → 不出图）。
# v1.0.7 起随包带 NotoSansCJKsc-Subset-{Regular,Bold}.otf（GB2312 全表 + 语料符号）。
# 本段保证：① 仓库语料里出现过的每个非 ASCII 字符都在子集 cmap 内（漏一个就是豆腐块）；
# ② 子集渲染与完整字库**逐像素一致**（同一套轮廓；缺字形/换面会立刻露馅）。
_FONT_DIR = Path(__file__).resolve().parent.parent / "core" / "data" / "fonts"
_SUB = {
    "Regular": _FONT_DIR / "NotoSansCJKsc-Subset-Regular.otf",
    "Bold": _FONT_DIR / "NotoSansCJKsc-Subset-Bold.otf",
}
_FULL = {
    "Regular": _FONT_DIR / "NotoSansCJK-Regular.ttc",
    "Bold": _FONT_DIR / "NotoSansCJK-Bold.ttc",
}


def _corpus_chars() -> set[str]:
    root = Path(__file__).resolve().parent.parent
    files = list(root.glob("*.py")) + list((root / "core").rglob("*.py"))
    files += list((root / "core" / "data").rglob("*.json"))
    files += list((root / "kb").rglob("*.md"))
    chars: set[str] = set()
    for f in files:
        try:
            chars.update(f.read_text(encoding="utf-8", errors="ignore"))
        except OSError:
            continue
    return {c for c in chars if ord(c) > 0x7F}


def _subset_cards():
    """对拍语料：符号压力卡 + 真实卡片（能取到就加，取不到不阻断）。"""
    cards = [
        (
            "符号压力卡",
            [
                "★⚠※▣◆→⏳①②③④⑤·—…",
                "（）、。，；：？！“”「」《》【】",
                "中英混排 ABC 123 % + - / : 4.25x",
            ],
        ),
    ]
    try:
        from core import formatters as F

        cards.append(
            F.fmt_timers([("仲裁", None), ("每日突击", {"expiry": "2030-01-01T00:00:00+00:00"})])
        )
    except Exception:
        pass
    try:
        from core import wiki_intro as W

        for q in ("前纪 V11 遗物", "Ash Prime 机体蓝图"):
            c = W.card_for(q)
            if c:
                cards.append(c)
    except Exception:
        pass
    try:
        import json as _json

        eff = (
            _json.loads(
                (
                    Path(__file__).resolve().parent.parent / "core" / "data" / "wiki_effect_zh.json"
                ).read_text(encoding="utf-8")
            ).get("effects")
            or {}
        )
        k = next(iter(eff))
        cards.append(("效果汉化样例", [f"效果：{eff[k]}"[:60], str(k)[:50]]))
    except Exception:
        pass
    return cards


def _render_with(regular: Path, bold: Path, cards, cache: Path):
    import core.render as R

    old_p, old_s = R._Fonts.PACKED, R._Fonts.SYSTEM
    R._Fonts.PACKED, R._Fonts.SYSTEM = [regular, bold], []
    try:
        r = R.ImageRenderer(cache)
        return [r.render(t, lines, "测试页脚") for t, lines in cards]
    finally:
        R._Fonts.PACKED, R._Fonts.SYSTEM = old_p, old_s


def _font_checks():
    if not _SUB["Regular"].exists():
        print("[SKIP] 子集字体不存在（未构建），跳过字体用例")
        return
    try:
        from fontTools.ttLib import TTCollection, TTFont
    except ImportError:
        print("[SKIP] 未安装 fonttools，跳过字体覆盖用例")
        return
    cmap = TTFont(_SUB["Regular"], lazy=True).getBestCmap()
    # ★ 判据对齐「完整字库能渲染的集合」：语料里少数符号（⏳ ✅ ⚔ 等 42 个）
    #   连完整字库的 SC 面都没有（原有豆腐，非子集引入）——只要求子集不丢
    #   完整字库有的字形；同时报出「两边都缺」的数量供排查，不判失败。
    corpus = _corpus_chars()
    if _FULL["Regular"].exists():
        full_cmap = TTCollection(str(_FULL["Regular"]), lazy=True).fonts[2].getBestCmap()
        need = {c for c in corpus if ord(c) in full_cmap}
        both_missing = sorted(c for c in corpus if ord(c) not in full_cmap)
        print(
            f"[INFO] 语料非 ASCII {len(corpus)} 个；完整字库也缺 {len(both_missing)} 个"
            f"（原有，非子集引入）：{''.join(both_missing[:12])}"
        )
    else:
        # 开源树不带完整 TTC ⇒ 无法知道「完整字库能渲染什么」。改用独立依据：子集构建承诺
        # GB2312 汉字区全表（scripts/build_font_subset.py::_gb2312），GB2312 符号区是中文排版
        # 标点/全角字符 ⇒ 语料里落在 GB2312 内的字必须全在子集；其余（emoji 等）无从判定，只报数。
        def _in_gb2312(c: str) -> bool:
            try:
                c.encode("gb2312")
                return True
            except UnicodeEncodeError:
                return False

        need = {c for c in corpus if _in_gb2312(c)}
        print(
            f"[SKIP] 完整字库不存在（开源树）：覆盖判据降为「语料 ∩ GB2312」{len(need)} 字；"
            f"其余 {len(corpus) - len(need)} 个非 GB2312 字符无独立依据，不判定"
        )
    missing = sorted(c for c in need if ord(c) not in cmap)
    check(
        "子集覆盖「完整字库能渲染的全部语料字符」"
        if _FULL["Regular"].exists()
        else "子集覆盖语料中的全部 GB2312 字符",
        not missing,
        f"缺 {len(missing)} 个：{missing[:12]}",
    )
    syms = "★⚠※▣◆→①②③④⑤·—…“”「」（）、。，；：？！%"
    check(
        "UI 符号与 ASCII 标点字形齐备",
        all(ord(s) in cmap for s in syms),
        str([s for s in syms if ord(s) not in cmap]),
    )
    check("ASCII 可打印区齐备", all(ord(chr(c)) in cmap for c in range(0x20, 0x7F)))

    if not _FULL["Regular"].exists():
        print("[SKIP] 完整字库不存在（开源树），跳过逐像素对拍")
        return
    from PIL import Image, ImageChops

    cards = _subset_cards()
    tmp = Path(tempfile.mkdtemp(prefix="wf_subset_"))
    try:
        # ★ 逐卡「渲完整库 → 渲子集 → 立刻比对」：渲染器有磁盘缓存裁剪
        #   （_cleanup 只保留最新若干张），批量渲染后再比会把早期产物裁掉。
        for i, (title, lines) in enumerate(cards):
            r_full = _render_one(_FULL["Regular"], _FULL["Bold"], title, lines, tmp / f"full_{i}")
            r_sub = _render_one(_SUB["Regular"], _SUB["Bold"], title, lines, tmp / f"sub_{i}")
            if not r_full or not r_sub or not Path(r_full).exists() or not Path(r_sub).exists():
                check(f"对拍渲染成功：{title}", False, f"{r_full} / {r_sub}")
                continue
            ia, ib = Image.open(r_full).convert("RGB"), Image.open(r_sub).convert("RGB")
            if ia.size != ib.size:
                check(f"逐像素一致：{title}", False, f"尺寸 {ia.size} vs {ib.size}")
                continue
            bbox = ImageChops.difference(ia, ib).getbbox()
            check(f"逐像素一致（完整库 vs 子集）：{title}", bbox is None, f"差异区域 {bbox}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _render_one(regular: Path, bold: Path, title: str, lines, cache: Path):
    import core.render as R

    old_p, old_s = R._Fonts.PACKED, R._Fonts.SYSTEM
    R._Fonts.PACKED, R._Fonts.SYSTEM = [regular, bold], []
    try:
        return R.ImageRenderer(cache).render(title, lines, "测试页脚")
    finally:
        R._Fonts.PACKED, R._Fonts.SYSTEM = old_p, old_s


_font_checks()


# ---------------------------------------------------------------------------
# 用户字体迁移（AstrBot 开发原则：持久化数据进 data 目录，别放插件自身目录）
# ---------------------------------------------------------------------------
# 背景（issue #1 报告者）：用户把手放字体放进插件目录 core/data/fonts/，市场更新
# 整包替换即丢。v1.0.8 起启动时一次性把「非随包资产」的字体搬进
# plugin_data/<插件>/fonts/（渲染查找链已含该目录）；随包子集 otf 是分发资产，
# 留在原地；开发/克隆树（有 .git）不迁，避免弄脏工作树。
def _migration_checks():
    import core.render as _R

    T = Path(tempfile.mkdtemp(prefix="wf_migrate_"))
    try:
        # ① 伪造「市场安装树」（无 .git）：2 个随包资产 + 1 个用户手放字体 + 非字体文件
        legacy = T / "plugin" / "core" / "data" / "fonts"
        legacy.mkdir(parents=True)
        (legacy / "NotoSansCJKsc-Subset-Regular.otf").write_bytes(b"SHIPPED-REG")
        (legacy / "NotoSansCJKsc-Subset-Bold.otf").write_bytes(b"SHIPPED-BOLD")
        (legacy / "MyHandPlaced.ttc").write_bytes(b"USER-TTC")
        (legacy / "notes.txt").write_bytes(b"not a font")
        user = T / "plugin_data" / "astrbot_plugin_warframe_sdjkbot" / "fonts"
        moved = _R.migrate_legacy_user_fonts(user, legacy)
        check(
            "迁移：用户手放字体被搬进 plugin_data",
            (user / "MyHandPlaced.ttc").exists() and not (legacy / "MyHandPlaced.ttc").exists(),
            str(moved),
        )
        check(
            "迁移：随包子集 otf 留在原地（分发资产不迁）",
            (legacy / "NotoSansCJKsc-Subset-Regular.otf").exists()
            and (legacy / "NotoSansCJKsc-Subset-Bold.otf").exists(),
        )
        check("迁移：非字体文件不动", (legacy / "notes.txt").exists())
        check("迁移：幂等（再跑一次为空）", _R.migrate_legacy_user_fonts(user, legacy) == [])
        # ② 目标已有同名文件 → 跳过、不覆盖
        (legacy / "MyHandPlaced.ttc").write_bytes(b"USER-TTC-2")
        check(
            "迁移：同名冲突跳过且不覆盖",
            _R.migrate_legacy_user_fonts(user, legacy) == []
            and (user / "MyHandPlaced.ttc").read_bytes() == b"USER-TTC",
        )
        # ③ 开发/克隆树（插件根有 .git）→ 一律不迁
        (T / "devplugin" / ".git").mkdir(parents=True)
        dev_legacy = T / "devplugin" / "core" / "data" / "fonts"
        dev_legacy.mkdir(parents=True)
        (dev_legacy / "RepoFont.ttc").write_bytes(b"TRACKED")
        dev_user = T / "devplugin_data" / "fonts"
        check(
            "迁移：开发/克隆树（有 .git）不迁移",
            _R.migrate_legacy_user_fonts(dev_user, dev_legacy) == []
            and (dev_legacy / "RepoFont.ttc").exists(),
        )
        # ④ 迁移目录里的**真字体**能被渲染器采纳（PACKED/SYSTEM 清空，只剩用户目录）
        #   注：上面用的是假字节文件，_probe() 正确地拒绝它；这一步换成真字体。
        real_src = _SUB["Regular"] if _SUB["Regular"].exists() else None
        if real_src:
            shutil.copy2(real_src, user / "MovedReal.otf")
            old_p, old_s = _R._Fonts.PACKED, _R._Fonts.SYSTEM
            _R._Fonts.PACKED, _R._Fonts.SYSTEM = [], []
            try:
                f = _R._Fonts(user_dirs=[user])
                check(
                    "迁移：搬过去的真字体能被渲染器采用",
                    f.regular is not None and "MovedReal" in f.regular.name,
                    str(f.regular),
                )
            finally:
                _R._Fonts.PACKED, _R._Fonts.SYSTEM = old_p, old_s
        else:
            print("[SKIP] 无子集字体可复制作真字体样本")
    finally:
        shutil.rmtree(T, ignore_errors=True)


_migration_checks()


# ===========================================================================
# B12（2026-10-11）：A「Orokin 精修」绘制层 —— 版式用例
# ===========================================================================
# 不手抄版式规则，直接打桩 ImageDraw 量「实际画了什么」：规则改了不会假绿。
def _spy_render(title: str, lines: list, footer: str = "国际服", draw_tokens: bool = False):
    """渲染一张卡，返回 dict：png 路径 + 各类绘制调用记录（坐标均为超采样像素）。"""
    from PIL import ImageDraw as _ID

    rec: dict = {"rr": [], "rect": [], "line": [], "text": [], "tok": [], "png": None}
    o_rr, o_rect, o_line, o_text = (
        _ID.ImageDraw.rounded_rectangle,
        _ID.ImageDraw.rectangle,
        _ID.ImageDraw.line,
        _ID.ImageDraw.text,
    )
    o_tok = R.ImageRenderer._draw_tokens

    def s_rr(self, xy, radius=0, fill=None, outline=None, width=1, **k):
        rec["rr"].append((list(xy), fill))
        return o_rr(self, xy, radius=radius, fill=fill, outline=outline, width=width, **k)

    def s_rect(self, xy, fill=None, outline=None, width=1):
        rec["rect"].append((list(xy), fill))
        return o_rect(self, xy, fill=fill, outline=outline, width=width)

    def s_line(self, xy, fill=None, width=0, joint=None):
        rec["line"].append((list(xy), fill))
        return o_line(self, xy, fill=fill, width=width, joint=joint)

    def s_text(self, xy, text, fill=None, font=None, *a, **k):
        rec["text"].append((tuple(xy), text, fill, font))
        return o_text(self, xy, text, fill, font, *a, **k)

    def s_tok(self, d, text, x, y, font, base_color, max_w):
        rec["tok"].append((text, x, base_color))
        return o_tok(self, d, text, x, y, font, base_color, max_w)

    _ID.ImageDraw.rounded_rectangle, _ID.ImageDraw.rectangle = s_rr, s_rect
    _ID.ImageDraw.line, _ID.ImageDraw.text = s_line, s_text
    R.ImageRenderer._draw_tokens = s_tok
    try:
        r = R.ImageRenderer(Path(tempfile.mkdtemp(prefix="sdjk_b12_")))
        rec["png"] = r.render(title, lines, footer)
        rec["r"] = r
    finally:
        _ID.ImageDraw.rounded_rectangle, _ID.ImageDraw.rectangle = o_rr, o_rect
        _ID.ImageDraw.line, _ID.ImageDraw.text = o_line, o_text
        R.ImageRenderer._draw_tokens = o_tok
    return rec


def _count(rec, kind, fill):
    return sum(1 for _xy, f in rec[kind] if f == fill)


if R.Image is None or not probe.available:
    print("[SKIP] 无 Pillow/字体，跳过 A 款版式用例")
else:
    # ---- 去逐行分隔线 + 斑马纹 + 续行紧排同一条纹
    _long = "这是一条很长的说明文字用来触发折行" * 8
    _zl = ["◆ 列表"] + [f"· 条目{i}" for i in range(5)]
    _zl[2] = "· " + _long  # 第 2 项（斑马序号 1）折成多行
    rec = _spy_render("斑马纹测试", _zl)
    check("渲染成功（斑马纹卡）", bool(rec["png"]))
    _raw = sorted((xy for xy, f in rec["rect"] if f == R.PANEL_ALT), key=lambda xy: xy[1])
    _stripes: list = []  # 折行续行各画一块、纵向首尾相接 → 合并后才是「一条」条纹
    for xy in _raw:
        if _stripes and xy[1] <= _stripes[-1][3] + 1:
            _stripes[-1][3] = xy[3]
        else:
            _stripes.append(list(xy))
    check("斑马纹：5 项列表 → 奇数项 2 条纹", len(_stripes) == 2, str(len(_stripes)))
    if len(_stripes) == 2:
        _h = [xy[3] - xy[1] for xy in _stripes]
        check("续行紧排同一条纹：折行项的条纹比单行项高", _h[0] > _h[1], str(_h))
        check("续行紧排同一条纹：折行项占多块（>1 个矩形）", len(_raw) > 2, str(len(_raw)))
    check(
        "去逐行分隔线：正文区没有 HAIR 色横线（只剩头/脚各一条）",
        _count(rec, "line", R.HAIR) == 2,
        str(_count(rec, "line", R.HAIR)),
    )
    rec3 = _spy_render("斑马纹测试", ["◆ 列表", "· 甲", "· 乙", "· 丙"])
    check("斑马纹反例：不足 4 项的列表不画条纹", _count(rec3, "rect", R.PANEL_ALT) == 0)

    # ---- 注释合并成注释面板；含 ⚠ 的进琥珀警示面板
    rec = _spy_render("注释测试", ["◆ 区块", "· 甲", "※ 注释一", "※ 注释二", "※ ⚠ 小心"])
    check(
        "注释：相邻 ※ 行并成 1 块注释面板",
        _count(rec, "rr", R.NOTE_BG) == 1,
        str(_count(rec, "rr", R.NOTE_BG)),
    )
    check(
        "警示：含 ⚠ 的 ※ 行进琥珀警示面板",
        _count(rec, "rr", R.WARN_BG) == 1,
        str(_count(rec, "rr", R.WARN_BG)),
    )
    rec = _spy_render("注释测试", ["◆ 区块", "· 甲", "※ 只是注释"])
    check("注释反例：无 ⚠ 时不出警示面板", _count(rec, "rr", R.WARN_BG) == 0)

    # ---- KV 行：标签金色、值提亮、值列对齐
    rec = _spy_render("KV 测试", ["◆ 状态：在线", "◆ 位置：地球 夜灵平原", "◆ 很长很长的标签：值"])
    _lab = [
        (xy, tx)
        for xy, tx, f, _ft in rec["text"]
        if f == R.GOLD and tx in ("状态", "位置", "很长很长的标签")
    ]
    check("KV：标签以 GOLD 绘制", {tx for _xy, tx in _lab} >= {"状态", "位置"}, str(_lab))
    _vals = {tx: x for tx, x, _c in rec["tok"] if tx in ("在线", "地球 夜灵平原", "值")}
    check(
        "KV：值走 INK 基色",
        all(c == R.INK for tx, _x, c in rec["tok"] if tx in _vals),
        str(rec["tok"][:4]),
    )
    check(
        "KV：各行值列起点对齐",
        len({round(x) for x in _vals.values()}) == 1 and len(_vals) == 3,
        str(_vals),
    )

    # ---- KV 值超宽：在值列内折行，不越界
    _bigv = "很长的值" * 40
    rec = _spy_render("KV 折行", ["◆ 标签：" + _bigv, "◆ 另一个：短"])
    check("渲染成功（KV 折行卡）", bool(rec["png"]))
    if rec["png"]:
        _W = R.Image.open(rec["png"]).size[0]
        _vx = {round(x) for tx, x, _c in rec["tok"] if tx and tx[0] in "很长的值"}
        check("KV 折行：值的各行共用同一个值列起点", len(_vx) == 1 and len(_vx) > 0, str(_vx))
        _rights = [
            xy[0] + font.getlength(tx)
            for xy, tx, _f, font in rec["text"]
            if tx and tx[0] in "很长的值"
        ]
        check(
            "KV 折行：每一行值的右缘都在内容区内（W−70 设计像素）",
            bool(_rights) and max(_rights) <= (_W - 70) * R.SS,
            f"max={max(_rights, default=0):.0f} 上限={(_W - 70) * R.SS}",
        )
        _mx, _n = right_overflow(rec["png"])
        check("KV 折行：正文未越出面板右缘", _n == 0, f"W={_W} 越界 {_n} 像素")
        check(
            "KV 折行：折成多行（值被拆开）",
            sum(1 for tx, _x, _c in rec["tok"] if tx and tx[0] in "很长的值") >= 2,
        )

    # ---- 同面板 ≥2 格自动列对齐 + 表头
    rec = _spy_render(
        "对齐测试",
        [
            "◆ 榜单",
            "名称　数量　备注",
            "· 短　一二三四五六七　尾",
            "· 比较长的第一格内容　乙　丙丙丙",
            "· 中等长度　乙乙乙　丁",
        ],
    )
    _by = {}
    for tx, x, _c in rec["tok"]:
        _by.setdefault(tx, x)
    check(
        "自动列对齐：第 2 格同一列起点",
        round(_by["一二三四五六七"]) == round(_by["乙"]) == round(_by["乙乙乙"]),
        str({k: _by.get(k) for k in ("一二三四五六七", "乙", "乙乙乙")}),
    )
    check(
        "自动列对齐：第 3 格同一列起点",
        round(_by["尾"]) == round(_by["丙丙丙"]) == round(_by["丁"]),
        str({k: _by.get(k) for k in ("尾", "丙丙丙", "丁")}),
    )
    _head = [(tx, f) for _xy, tx, f, _ft in rec["text"] if tx in ("名称", "数量", "备注")]
    check(
        "表头：小号灰粗字（INK_FAINT）",
        len(_head) == 3 and all(f == R.INK_FAINT for _tx, f in _head),
        str(_head),
    )
    check(
        "表头：下方一条细线（头/脚 + 表头线 = 3）",
        _count(rec, "line", R.HAIR) == 3,
        str(_count(rec, "line", R.HAIR)),
    )

    # ---- 卡宽在 [960, 1260]（列对齐卡的列宽合计本身超限时放宽到 1500，仍保列对齐）
    _wide_cases = {
        "极短": ["· 甲"],
        "中等": ["◆ 区块"] + [f"· 第 {i} 条说明文字，内容中等长度" for i in range(6)],
        "超长无空格": ["· " + "很长的一整行文字" * 40],
        "超长标题": ["· 甲"],
    }
    for _nm, _ls in _wide_cases.items():
        _ti = "很长很长的标题" * 6 if _nm == "超长标题" else "宽度测试"
        _p = probe.render(_ti, _ls, "国际服")
        _w = R.Image.open(_p).size[0] if _p else 0
        check(f"卡宽 ∈ [960,1260]：{_nm}", 960 <= _w <= 1260, f"W={_w}")
    _tab = ["◆ 古纪"] + ["· " + "　".join(f"古纪 A{i}{j}" for j in range(9)) for i in range(3)]
    _p = probe.render("遗物出库（当前可掉落）", _tab, "国际服")
    _w = R.Image.open(_p).size[0] if _p else 0
    check(
        "列对齐卡：列宽合计超 1260 时放宽（≤1500）而不放弃列对齐",
        960 <= _w <= R.CARD_W_TABLE_MAX and probe._table_mode is True,
        f"W={_w} table_mode={probe._table_mode}",
    )

# ---- 耗时闸门（2026-10-11 起取代「旧版 vs 新版逐字节对拍」）
# 背景：A 款有意改了像素（去星点/拱纹/暗角、面板/斑马纹、配色），逐字节基准必然失效；
# 但护栏不能就此消失 —— 换成「相对耗时」闸门：渲染 min-of-N 耗时 ÷ 同机校准负载耗时，
# 与旧版（git 3e16266 的 render.py，同一批合成卡、同机同法实测）的比值上限对比。
# 比值而不是绝对毫秒 —— 不随机器快慢/并行负载漂移；上限取旧版实测比值（"不高于旧版"）。
# 旧版比值实测（2 轮）：小 0.34/0.35、中 1.10/1.15、大 2.32/2.49；新版 0.21 / 0.81 / 1.73。
# 想临时跳过（极端抖动的宿主机）：环境变量 WF_SKIP_PERF_GATE=1。
import os as _os  # noqa: E402
import time as _time  # noqa: E402


def _perf_cards() -> dict:
    s = ["◆ 区块一"] + [f"· 第 {i} 条说明文字　数值 {i}%　剩余 {i}分钟" for i in range(8)]
    m = (
        ["◆ 状态：在线"]
        + [f"· 条目{i}　元素 25.7%　钢铁之路 [古纪] 120p" for i in range(40)]
        + ["※ 注释一"]
    )
    big: list = []
    for g in range(6):
        big.append(f"◆ 分组{g}")
        big += [f"· 武器{g}-{i}　辐射 25.{i}%　暴击　一段说明文字用来撑宽" for i in range(14)]
    return {"小(9行)": s, "中(42行)": m, "大(90行)": big}


_PERF_CEIL = {"小(9行)": 0.34, "中(42行)": 1.10, "大(90行)": 2.30}


def _perf_calib(rr, n: int = 5) -> float:
    from PIL import Image, ImageDraw

    f = rr.fonts.get("regular", 30)
    best = 1e9
    for _ in range(n):
        t0 = _time.perf_counter()
        im = Image.new("RGB", (2400, 1600), (12, 15, 22))
        d = ImageDraw.Draw(im)
        for i in range(300):
            d.text(
                (20 + (i % 7) * 300, 10 + (i % 40) * 36),
                f"校准文字 {i} Warframe",
                font=f,
                fill=(236, 232, 222),
            )
        im.resize((1200, 800), Image.BOX)
        best = min(best, _time.perf_counter() - t0)
    return best * 1000


def _perf_ratios(rr, cards: dict) -> dict:
    calib = _perf_calib(rr)
    out = {}
    for nm, ls in cards.items():
        rr._render("性能闸门", ls, "国际服")  # 预热（字体度量缓存/首次分配）
        ts = []
        for _ in range(5):
            t0 = _time.perf_counter()
            rr._render("性能闸门", ls, "国际服")
            ts.append((_time.perf_counter() - t0) * 1000)
        out[nm] = min(ts) / calib
    return out


if _os.environ.get("WF_SKIP_PERF_GATE") == "1":
    print("[SKIP] WF_SKIP_PERF_GATE=1，跳过耗时闸门")
elif _os.environ.get("GITHUB_ACTIONS") == "true":
    # 2026-10-17：上限是本机同法实测的比值（旧版本机 0.34/0.35 …），而 GitHub 共享 runner 上同一份代码
    #   比值约为本机 1.6 倍且随 runner 浮动（10-16 23:56 UTC 起 3.12 / oss-stage 时红时绿：0.347 vs 0.34）。
    #   比值并非真的与机器无关 ⇒ 这道闸门只在本机 / 服务器（同机前后对比）上有意义；CI 上跳过，其余渲染检查照跑。
    print(
        "[SKIP] GitHub Actions 共享 runner：耗时比值与本机基准不可比，耗时闸门只在本机 / 服务器跑"
    )
elif R.Image is None or not probe.available:
    print("[SKIP] 无 Pillow/字体，跳过耗时闸门")
else:
    _cards = _perf_cards()
    _ratios = _perf_ratios(probe, _cards)
    if any(_ratios[k] > _PERF_CEIL[k] for k in _cards):  # 抖动重试一次
        _ratios = _perf_ratios(probe, _cards)
    for _k in _cards:
        check(
            f"耗时闸门：{_k} 相对耗时 ≤ 旧版比值 {_PERF_CEIL[_k]}",
            _ratios[_k] <= _PERF_CEIL[_k],
            f"比值={_ratios[_k]:.3f}",
        )


async def _render_cancel_checks():
    import ast
    import asyncio
    import threading
    from types import SimpleNamespace

    from core.commands.base import Reply
    from core.parser import Parsed

    source = Path(__file__).resolve().parent.parent / "main.py"
    tree = ast.parse(source.read_text())
    names = {"_build_content_results", "_render_card", "_schedule_card_cleanup", "terminate"}
    methods = [
        f
        for c in tree.body
        if isinstance(c, ast.ClassDef)
        for f in c.body
        if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef)) and f.name in names
    ]
    cls = ast.ClassDef(name="WarframeSDJK", bases=[], keywords=[], body=methods, decorator_list=[])
    warnings = []
    scope = {
        "text_card": R.text_card,
        "asyncio": asyncio,
        "Path": Path,
        "Optional": object,
        "Reply": Reply,
        "Parsed": Parsed,
        "AstrMessageEvent": object,
        "logger": SimpleNamespace(
            warning=lambda *args: warnings.append(args), info=lambda *args: None
        ),
        "Image": SimpleNamespace(fromFileSystem=lambda path: path),
        "Plain": lambda text: text,
        "i18n": _i18n,
    }
    module = ast.Module(
        body=[
            ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
            cls,
        ],
        type_ignores=[],
    )
    exec(compile(ast.fix_missing_locations(module), str(source), "exec"), scope)
    Host = scope["WarframeSDJK"]
    Host._render_waiters = {}
    Host.MAX_RENDER_WAITERS = 1

    for mode in ("normal", "cancel", "late_error"):
        with tempfile.TemporaryDirectory(prefix="wf-render-stop-") as directory:
            root = Path(directory)
            first_started, second_started, release = (
                threading.Event(),
                threading.Event(),
                threading.Event(),
            )
            Host._render_lock = None
            obj, cleanups = Host(), []
            obj.render_mode = "image"

            class Renderer:
                available = True

                def render(self, title, lines, footer):
                    if title == "first":
                        first_started.set()
                        if not release.wait(3):
                            raise TimeoutError("test release was not signalled")
                        if mode == "late_error":
                            raise RuntimeError("late render error")
                    else:
                        second_started.set()
                    path = root / (title.replace("/", "_") + ".png")
                    path.write_bytes(b"rendered image")
                    return str(path)

            obj.renderer = Renderer()

            def cleanup(path, delay=120.0):
                cleanups.append((Path(path).name, delay))
                if delay == 0:
                    Host._schedule_card_cleanup(obj, path, delay=0)

            obj._schedule_card_cleanup = cleanup
            event = SimpleNamespace(chain_result=lambda parts: parts)
            parsed = SimpleNamespace(force_image=True, force_text=False)
            first_gen = obj._build_content_results(
                event, Reply(title="first", lines=["line"]), parsed
            )
            first = asyncio.create_task(anext(first_gen))
            second_gen = second = None
            try:
                for _ in range(1000):
                    if first_started.is_set():
                        break
                    await asyncio.sleep(0.001)
                check("取消探针已进入实际渲染线程", first_started.is_set())
                if mode == "normal":
                    release.set()
                    await first
                    check(
                        "正常发送保留120秒上传缓冲",
                        cleanups == [("first.png", 120.0)] and (root / "first.png").exists(),
                    )
                    # 多页路径同样经过共用渲染方法，保持原有逐页清理。
                    pages = obj._build_content_results(
                        event, Reply(pages=[("page1", ["one"]), ("page2", ["two"])]), parsed
                    )
                    await anext(pages)
                    await pages.aclose()
                    check(
                        "正常多页分别保留120秒缓冲", [delay for _, delay in cleanups] == [120.0] * 3
                    )
                else:
                    first.cancel()
                    await asyncio.wait_for(asyncio.gather(first, return_exceptions=True), 1)
                    check(
                        "调用方取消立即结束且线程仍持锁",
                        first.cancelled() and Host._render_lock.locked() and not release.is_set(),
                    )
                    second_gen = obj._build_content_results(
                        event, Reply(title="second", lines=["line"]), parsed
                    )
                    second = asyncio.create_task(anext(second_gen))
                    await asyncio.sleep(0.02)
                    check("旧线程完成前后继渲染不得启动", not second_started.is_set())
                    release.set()
                    await asyncio.wait_for(second, 1)
                    for _ in range(10):
                        await asyncio.sleep(0)
                    check(
                        "旧线程结束后后继渲染完成",
                        second_started.is_set() and (root / "second.png").exists(),
                    )
                    if mode == "cancel":
                        check(
                            "取消后生成的PNG立即安排清理",
                            ("first.png", 0) in cleanups and not (root / "first.png").exists(),
                        )
                    else:
                        check(
                            "取消后迟到异常被取回并记录",
                            any("late render error" in repr(args) for args in warnings),
                        )
                    check("正常后继图片仍保留上传缓冲", ("second.png", 120.0) in cleanups)
                await asyncio.sleep(0)
                check("已完成的渲染任务解除保活引用", not obj._card_render_tasks)
            finally:
                release.set()
                await asyncio.gather(
                    first, *([second] if second is not None else []), return_exceptions=True
                )
                await asyncio.gather(
                    *list(getattr(obj, "_card_render_tasks", ())), return_exceptions=True
                )
                await first_gen.aclose()
                if second_gen is not None:
                    await second_gen.aclose()

    from unittest.mock import AsyncMock

    for unload in (False, True):
        Host._render_lock = None
        obj = Host()
        started, release = threading.Event(), threading.Event()
        calls = []

        def render(title, lines, footer):
            calls.append(title)
            if title == "A":
                started.set()
                assert release.wait(3)
            return title

        obj.renderer = SimpleNamespace(render=render, available=True)
        obj.client = SimpleNamespace(close=AsyncMock())
        obj.push = SimpleNamespace(stop=AsyncMock())
        first = asyncio.create_task(obj._render_card("A", [], ""))
        try:
            assert await asyncio.to_thread(started.wait, 1)
            waiting = asyncio.create_task(obj._render_card("B", [], ""))
            await asyncio.sleep(0)
            check("B 已在渲染锁上排队", waiting in Host._render_waiters)
            gen = obj._build_content_results(
                SimpleNamespace(plain_result=str),
                Reply("队满", ["原正文"]),
                SimpleNamespace(force_image=True, force_text=False),
            )
            try:
                check("队满经回复入口降级文字", "原正文" in await asyncio.wait_for(anext(gen), 1))
            finally:
                await gen.aclose()
            if unload:
                await asyncio.wait_for(obj.terminate(), 1)
            else:
                waiting.cancel()
            await asyncio.wait_for(asyncio.gather(waiting, return_exceptions=True), 1)
            check(
                "等待者被清退且 A 仍持锁",
                waiting.cancelled() and not Host._render_waiters and Host._render_lock.locked(),
            )
            successor = asyncio.create_task(obj._render_card("C", [], ""))
            await asyncio.sleep(0)
            release.set()
            results = await asyncio.gather(first, successor)
            check(
                f"排队取消后实际渲染顺序（卸载={unload}）",
                calls == (["A"] if unload else ["A", "C"])
                and results == ["A", None if unload else "C"],
            )
        finally:
            release.set()
            await first


import asyncio as _asyncio  # noqa: E402

_asyncio.run(_render_cancel_checks())


async def _multipage_delivery_checks():
    import ast
    from types import SimpleNamespace

    from core.commands.base import Reply

    source = Path(__file__).resolve().parent.parent / "main.py"
    tree = ast.parse(source.read_text())
    method = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_build_content_results"
    )
    scope = {
        "text_card": R.text_card,
        "Image": SimpleNamespace(fromFileSystem=lambda path: {"image": path}),
        "Plain": lambda text: {"text": text},
        "logger": SimpleNamespace(warning=lambda *args: None),
        "i18n": _i18n,
    }
    module = ast.Module(
        body=[
            ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
            method,
        ],
        type_ignores=[],
    )
    exec(compile(ast.fix_missing_locations(module), str(source), "exec"), scope)
    build = scope["_build_content_results"]
    pages = [(f"PAGE_{i}", [f"BODY_{i}"]) for i in range(1, 4)]
    reply = Reply(pages=pages, footer="FOOTER", whisper=["WHISPER"], extra_text="EXTRA")
    event = SimpleNamespace(chain_result=lambda x: x, plain_result=lambda x: [{"text": x}])
    for failed in ((), (1,), (2,), (3,), (1, 2, 3)):
        calls, cleanups = [], []

        async def render(title, lines, footer):
            calls.append(title)
            index = next((i for i in range(1, 4) if f"PAGE_{i}" in title), 0)
            return None if index in failed else "|".join([title, *lines, footer])

        host = SimpleNamespace(
            render_mode="image",
            renderer=SimpleNamespace(available=True),
            _render_card=render,
            _schedule_card_cleanup=cleanups.append,
        )
        parsed = SimpleNamespace(force_image=True, force_text=False)
        results = [row async for row in build(host, event, reply, parsed)]
        text = str(results)
        label = f"多页失败{failed}"
        check(f"{label}：三页正文齐全", all(f"BODY_{i}" in text for i in range(1, 4)))
        check(
            f"{label}：原页顺序保留",
            text.find("BODY_1") < text.find("BODY_2") < text.find("BODY_3"),
        )
        check(f"{label}：只渲染三页不补空封面", len(calls) == 3)
        check(f"{label}：只清理成功图片", len(cleanups) == 3 - len(failed))
        check(f"{label}：密语和链接各一次", text.count("WHISPER") == text.count("EXTRA") == 1)

    for mode in ("normal", "cancel", "error"):
        started, release = _asyncio.Event(), _asyncio.Event()
        cleanup_requests = []

        async def delayed_render(title, lines, footer):
            if title.startswith("SECOND"):
                started.set()
                await release.wait()
                if mode == "error":
                    raise RuntimeError("synthetic second page failure")
                return "second.png"
            return "first.png"

        host_ = SimpleNamespace(
            render_mode="image",
            renderer=SimpleNamespace(available=True),
            _render_card=delayed_render,
            _schedule_card_cleanup=lambda path, delay=120.0: cleanup_requests.append((path, delay)),
        )
        gen = build(
            host_,
            event,
            Reply(pages=[("FIRST", ["one"]), ("SECOND", ["two"])]),
            SimpleNamespace(force_image=True, force_text=False),
        )
        task = _asyncio.create_task(anext(gen))
        await _asyncio.wait_for(started.wait(), 1)
        check(f"多页清理{mode}：后页未完成时不启动前页清理", not cleanup_requests)
        if mode == "cancel":
            task.cancel()
        else:
            release.set()
        results_ = await _asyncio.gather(task, return_exceptions=True)
        if mode == "normal":
            check(
                "多页交付时两图各获得120秒缓冲",
                cleanup_requests == [("first.png", 120.0), ("second.png", 120.0)],
            )
            check("多页缓冲登记后正常交付整链", len(results_[0]) == 2)
        else:
            check(f"多页清理{mode}：前页未交付立即清理", cleanup_requests == [("first.png", 0)])
            check(
                f"多页清理{mode}：保留调用方异常",
                task.cancelled() if mode == "cancel" else isinstance(results_[0], RuntimeError),
            )
        await gen.aclose()

    for text_only in (False, True):
        calls.clear()
        reply.text_only = text_only
        parsed = SimpleNamespace(force_image=text_only, force_text=not text_only)
        results = [row async for row in build(host, event, reply, parsed)]
        check(f"多页文字模式{text_only}：不调用渲染", not calls)
        check(
            f"多页文字模式{text_only}：三页与补充内容保留",
            all(
                token in str(results)
                for token in ["BODY_1", "BODY_2", "BODY_3", "WHISPER", "EXTRA"]
            ),
        )


_asyncio.run(_multipage_delivery_checks())


def _long_heading_and_build_checks(renderer):
    from unittest.mock import patch

    header = "◆ 灵化循环（含普通形态充能） · 钢铁之路 堕落重型机枪手 Lv200 · 弱点 50% · 10 米"
    lines = [
        header,
        "· 去超宏 · 无盾",
        "◆ 武器面板",
        "· 伤害 2457.5 · 暴击几率 46.7% · 暴击倍率 ×4.11",
        "◆ 推荐配装 · 容量 58/60 · 极化 3 次",
        "　槽位　MOD　等级　容量　极性　拆下后 DPS　变化",
        *[
            f"· {i:02d}　镀层定向增幅与弱点专精　10　8　⟦pol:madurai⟧ Madurai　"
            "⟦g⟧12,400,001⟦/g⟧　⟦r⟧▼ 81.1%⟦/r⟧"
            for i in range(1, 9)
        ],
        "◆ 辅助装备",
        "· 扩展槽　致命机动（3级，容量 5） · ⟦pol:naramon⟧ Naramon",
        "· 赋能　次要·无情（5级）",
        "◆ 灵化进化",
        "· 2-1 弹药储备 · " + "普通形态命中弱点时恢复充能，" * 12 + "恢复结束。",
        "※ " + "拆下这一张后的 DPS 与上方整套对比。" * 8,
    ]
    seen = []
    build_rows = []
    diamonds = []
    section_rules = []
    draw_tokens = renderer._draw_tokens
    draw_build = renderer._draw_build_cells
    draw_diamond = renderer._diamond
    draw_line = R.ImageDraw.ImageDraw.line

    def tokens(d, text, x, y, font, color, edge):
        end = draw_tokens(d, text, x, y, font, color, edge)
        if font is renderer.fonts.get("bold", R.FS_BODY + 1):
            seen.append((text, x, end, edge))
        return end

    def build(d, row, x, y, font, is_header, edge):
        build_rows.append((row, font))
        draw_build(d, row, x, y, font, is_header, edge)

    def diamond(x, y, radius):
        if x == 62 * R.SS:
            diamonds.append(y)
        return draw_diamond(x, y, radius)

    def line(d, xy, fill=None, width=0):
        if fill == R.PANEL_LINE:
            section_rules.append(xy[0][1])
        return draw_line(d, xy, fill, width)

    with (
        patch.object(renderer, "_draw_tokens", side_effect=tokens),
        patch.object(renderer, "_draw_build_cells", side_effect=build),
        patch.object(renderer, "_diamond", side_effect=diamond),
        patch.object(R.ImageDraw.ImageDraw, "line", line),
    ):
        path = renderer.render("配卡推荐 · 布里斯提卡 Prime", lines, "来源 数据 " * 100)
    heading_rows = seen[: next((i for i, row in enumerate(seen) if row[0] == "武器面板"), 0)]
    check(
        "超长 ◆ 标题按粗体折行，尾词仍可见且续行对齐",
        len(heading_rows) >= 2
        and heading_rows[-1][0].endswith("10 米")
        and all(x == heading_rows[0][1] and end <= edge for _, x, end, edge in heading_rows),
        repr(heading_rows),
    )
    check(
        "多行分区标题仅首行画菱形，延伸线落在最后一行",
        len(diamonds) == 5
        and len(section_rules) == 5
        and diamonds[0] < section_rules[0] < diamonds[1],
        f"菱形 y={diamonds}，延伸线 y={section_rules}",
    )
    check(
        "布里斯提卡长靶标、8 槽、辅助装备与灵化进化不越界",
        bool(path and right_overflow(path)[1] == 0) and len(build_rows) == 11,
    )
    check(
        "配卡表所有可见单元格，包括染色数字与极性，按自己的列宽折行",
        bool(build_rows)
        and all(
            R._inline_width(R._measure_draw(), font, part) <= width * R.SS
            for row, font in build_rows
            for (_, width), parts in zip(row["build_columns"], row["build_cells"], strict=True)
            for part in parts
        ),
    )
    check(
        "标准配卡极性与 DPS 在同一行显示",
        len(build_rows) >= 9
        and all(
            len(row["build_cells"][index]) == 1 for row, _ in build_rows[1:9] for index in (4, 5)
        ),
    )
    return lines


def _check_audit_themes(renderer, cards):
    from core.webpanel import THEME_PRESETS

    light = next(preset["colors"] for preset in THEME_PRESETS if preset["key"] == "ivory")
    vivid = {"accent": "#55d4e6", "highlight": "#a9edff", "red": "#ee7573"}
    previous = R.theme_state()
    try:
        for theme in (light, vivid):
            R.apply_theme(theme)
            expected = R.GOLD
            for title, lines, footer, slug in cards:
                path = renderer.render(title, lines, footer)
                if path:
                    with R.Image.open(path) as im:
                        # 栏目小标位于顶部标题区，排除边框、下方标题和分隔线。
                        kicker = im.crop((70, 49, im.width - 70, 79))
                        colors = {c for _, c in kicker.getcolors(kicker.width * kicker.height)}
                        themed = im.getpixel((2, 0)) == R.BG_TOP and expected in colors
                else:
                    themed = False
                check(
                    f"{theme['accent']} {slug} 栏目小标与整卡配色应用主题",
                    themed and R._card_accent(title) == expected and R._card_slug(title) == slug,
                )
    finally:
        R.apply_theme(**previous)


async def _damage_and_riven_card_checks():
    import re
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch

    from core import loadout_ocr, portable_riven
    from core import riven_sale as sale_source
    from core.commands.base import Reply
    from core.commands.riven import RivenCommands
    from core.commands.vision import VisionCommands
    from core.commands.wiki_misc import WikiMiscCommands
    from core.parser import parse

    if R.Image is None or not probe.available:
        return

    class Host(WikiMiscCommands, RivenCommands, VisionCommands):
        cfg = {}
        client = SimpleNamespace(
            resolve_riven_weapon=AsyncMock(
                return_value={
                    "url_name": "bandit",
                    "zh": "盗贼",
                    "en": "Bandit",
                    "riven_type": "pistol",
                    "group": "secondary",
                    "disposition": 1.4,
                }
            ),
            riven_family=AsyncMock(return_value=[]),
        )
        riven_sale = SimpleNamespace(
            lookup=AsyncMock(
                return_value=sale_source.SaleResult(
                    url="",
                    status="ok",
                    row=sale_source.SaleRow("Braton Prime", "多重", "变焦", "示例参考"),
                )
            )
        )

        async def _riven_analysis_price(self, weapon, positive, negative, platform):
            return {
                "pos": [key for key, _ in positive],
                "neg": [key for key, _ in negative],
                "sample_count": 8,
                "low": 100,
                "online_low": 130,
                "median": 180,
            }

    patterns = re.compile(
        r"/Lotus/|\b(?:loadoutValidation|windowDps|fireMode|armourStrip|slotPolarities|"
        r"replayContextHash|confirmedAnalysisOptions|critical_damage|toxin_damage|DT_\w+|AP_\w+)\b"
    )
    host = Host()
    with tempfile.TemporaryDirectory(prefix="wf-damage-riven-cards-") as folder:
        renderer = R.ImageRenderer(Path(folder))
        polarity_cases = {
            "AP_ATTACK": "madurai",
            "AP_TACTIC": "naramon",
            "AP_DEFENSE": "vazarin",
            "AP_ANY": "universal",
            "AP_UMBRA": "umbra",
            "AP_POWER": "zenurik",
            "AP_WARD": "unairu",
            "AP_PRECEPT": "penjaga",
        }
        no_polarity = (None, "", "AP_UNIVERSAL", "AP_NONE", "NONE", "NULL", "AP_UNKNOWN")
        slots = [
            {
                "path": f"/Mod/{key}",
                "nameEn": "Test MOD",
                "nameZh": "测试 MOD",
                "rank": 5,
                "slotPolarity": code,
            }
            for code, key in polarity_cases.items()
            if code not in {"AP_ATTACK", "AP_TACTIC", "AP_DEFENSE"}
        ]
        equipment = {
            "exilus": {"nameEn": "Exilus test", "rank": 0, "drain": 2, "slotPolarity": "AP_ANY"},
            "stance": {"nameEn": "Stance test", "capacityGain": 10, "slotPolarity": "AP_PRECEPT"},
        }
        if icon := R._polarity_icon("universal"):
            check(
                "通用极性图标为单色 64×64 RGBA 镂空图",
                icon.size == (64, 64)
                and icon.mode == "RGBA"
                and icon.getpixel((32, 32))[3] == 0
                and all(pixel[:3] == (226, 235, 248) for pixel in icon.getdata()),
            )
        for lang, universal in (("zh", "通用"), ("en", "Universal")):
            with _i18n.use_lang(lang):
                labels = {
                    code: f"⟦pol:{key}⟧{universal if key == 'universal' else key.capitalize()}"
                    for code, key in polarity_cases.items()
                }
                check(
                    f"{lang} 全部极性的图标标记和名称",
                    all(
                        portable_riven._polarity_label(code) == label
                        for code, label in labels.items()
                    )
                    and all(portable_riven._polarity_label(code) is None for code in no_polarity),
                )
                polarity_lines = portable_riven._fixed_build_lines(
                    {},
                    [
                        *slots,
                        {**slots[0], "slotPolarity": "AP_UNIVERSAL"},
                        {**slots[0], "slotPolarity": "AP_UNKNOWN"},
                        None,
                    ],
                    {"total": 0},
                    {"equipment": equipment},
                    (),
                    bundled=True,
                )
                joined = "\n".join(polarity_lines)
                check(
                    f"{lang} Mod、扩展槽与架势不显示内部极性键",
                    not re.search(r"\bAP_[A-Z_]+\b", joined)
                    and all(labels[row["slotPolarity"]] in joined for row in slots)
                    and all(
                        next(line for line in polarity_lines if line.startswith(f"· {slot}")).split(
                            "　"
                        )[4]
                        == "—"
                        for slot in ("06", "07")
                    )
                    and any(
                        "Exilus test" in line and labels["AP_ANY"] in line
                        for line in polarity_lines
                    )
                    and any(
                        "Stance test" in line and labels["AP_PRECEPT"] in line
                        for line in polarity_lines
                    ),
                    joined,
                )
                drawn = []
                draw_text = R.ImageDraw.ImageDraw.text

                def record_text(draw, xy, text, *args, **kwargs):
                    drawn.append(text)
                    return draw_text(draw, xy, text, *args, **kwargs)

                with (
                    patch.dict(R._POL_ICONS, {"universal": None}),
                    patch.object(R.ImageDraw.ImageDraw, "text", record_text),
                ):
                    polarity_card = renderer.render("Polarity", polarity_lines)
                check(
                    f"{lang} 缺通用图标时名称仍完整绘制且不越界",
                    universal in "".join(drawn)
                    and bool(polarity_card and right_overflow(polarity_card)[1] == 0),
                )
                unknown = portable_riven._fixed_build_lines(
                    {},
                    [{"rank": 8}] + [None] * 7,
                    {
                        "slots": {"0": "AP_TACTIC"},
                        "drain": 9,
                        "limit": 60,
                        "total": 1,
                        "placement": [{"drain": 9, "polarity": "AP_TACTIC"}] + [None] * 7,
                    },
                    {},
                    (),
                    with_riven=True,
                    unspecified_polarity=True,
                )
                drawn.clear()
                with patch.object(R.ImageDraw.ImageDraw, "text", record_text):
                    unknown_card = renderer.render(
                        "配卡推荐" if lang == "zh" else "Build recommendations", unknown
                    )
                label = "紫卡极性" if lang == "zh" else "Riven polarity"
                check(
                    f"{lang} 未知紫卡极性只绘制本地化名称且不越界",
                    label in " ".join(drawn)
                    and not any("⟦pol:" in row for row in unknown)
                    and bool(unknown_card and right_overflow(unknown_card)[1] == 0),
                )
        loadout_lines = _long_heading_and_build_checks(renderer)
        stress_damage = await host._h_damage(
            parse("伤害 布莱顿p 百夫长9999 有盾 带超宏 膛线 弱点感应"), None, "pc"
        )
        damage_text = "\n".join(stress_damage.lines)
        stress_path = renderer.render(
            stress_damage.title, stress_damage.lines, stress_damage.footer
        )
        check(
            "9999 级钢路凶魂百夫长带超宏和 3600 基准模拟盾：真实指令出图不越界",
            all(
                word in damage_text
                for word in ("9999级", "凶魂百夫长", "带超宏", "模拟有盾（基准 3600）")
            )
            and bool(stress_path and right_overflow(stress_path)[1] == 0),
            damage_text,
        )
        for language in ("zh", "en"):
            with _i18n.use_lang(language):
                english = language == "en"
                read = loadout_ocr.analyze(
                    {
                        "weapon": "布莱顿 Prime",
                        "capacity": "20/60",
                        "mods": [{"name": "膛线", "drain": 7}, {"name": "弱点感应", "drain": 5}],
                        "arcanes": [
                            {"name": "Primary Deadhead" if english else "主要·死首", "rank": 5}
                        ],
                    }
                )
                cards = (
                    (
                        "手输伤害",
                        await host._h_damage(
                            parse(
                                "damage Amprex vs Heavy Gunner Serration arcane Primary Deadhead 5 headshot"
                                if english
                                else "伤害 安培克斯 对 重机枪手 膛线 赋能 主要·死首 5级 爆头"
                            ),
                            None,
                            "pc",
                        ),
                        ("Built-in estimate", "Rank 5") if english else ("内置估算", "5 级"),
                    ),
                    (
                        "配卡推荐",
                        await host._h_loadout_recommend(
                            parse(
                                "loadoutrecommend Braton Prime overguard shield"
                                if english
                                else "配卡推荐 布莱顿p 超宏 护盾"
                            ),
                            None,
                            "pc",
                        ),
                        ("Full Simulation under Damage Engine",)
                        if english
                        else ("后台「伤害引擎」启用完整模拟",),
                    ),
                    (
                        "词条推荐",
                        await host._h_riven_traits(
                            parse("riventraits Braton Prime" if english else "词条推荐 布莱顿p"),
                            None,
                            "pc",
                        ),
                        ("Selling reference",) if english else ("出售参考",),
                    ),
                    (
                        "紫卡分析",
                        await host._h_riven_analysis(
                            parse(
                                ("rivenanalysis Bandit" if english else "紫卡分析 盗贼")
                                + " 暴伤108.2 多重141.7 毒伤98.2 负切割99.9"
                            ),
                            None,
                            "pc",
                        ),
                        ("Range", "Splice Suggestions", "Same-Stat Listings")
                        if english
                        else ("区间", "融合建议", "同词条挂单"),
                    ),
                    (
                        "识卡",
                        Reply("Loadout Scan" if english else "识卡", loadout_ocr.card_lines(read)),
                        ("Weapon:", "Primary Deadhead") if english else ("武器", "主要·死首"),
                    ),
                )
                if not english:
                    traits, analysis = cards[2][1], cards[3][1]
                    _check_audit_themes(
                        renderer,
                        [
                            (
                                "配卡推荐 · 布里斯提卡 Prime",
                                loadout_lines,
                                "",
                                "BUILD RECOMMENDATIONS",
                            ),
                            (traits.title, traits.lines, traits.footer, "RIVEN TRAITS"),
                            (analysis.title, analysis.lines, analysis.footer, "RIVEN ANALYSIS"),
                            (
                                stress_damage.title,
                                stress_damage.lines,
                                stress_damage.footer,
                                "DAMAGE CALCULATOR",
                            ),
                        ],
                    )
                for name, reply, labels in cards:
                    if name == "词条推荐":
                        check(
                            f"{language} 词条推荐正文无空行",
                            all(line.strip() for line in reply.lines),
                        )
                    shown = "\n".join(reply.lines)
                    check(
                        f"{language} {name}保留可见字段且无内部键",
                        all(label.casefold() in shown.casefold() for label in labels)
                        and not patterns.search(shown),
                        shown[:1800],
                    )
                    pages = reply.pages or [(reply.title, reply.lines)]
                    for title, lines in pages:
                        rendered = renderer.render(title, lines, reply.footer)
                        check(
                            f"{language} {name}回图正文未越出右边缘",
                            bool(rendered and right_overflow(rendered)[1] == 0),
                        )


_asyncio.run(_damage_and_riven_card_checks())


async def _bundled_reference_smoke_cards():
    import os
    import shutil
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch

    from core import damage_engine, paths, portable_riven, riven_rating
    from core import riven_sale as sale_source
    from core.commands.riven import RivenCommands
    from core.commands.vision import VisionCommands
    from core.parser import parse

    fixture = Path(os.environ.get("SDJK_PORTABLE_SMOKE_DIR", str(paths.PKG_DATA)))
    names = portable_riven.BUNDLED_BUILDS, portable_riven.BUNDLED_TRAITS
    if not all((fixture / name).is_file() for name in names):
        print("[SKIP] 缺少预计算 smoke 表，跳过随包样张")
        return
    if R.Image is None or not probe.available:
        return

    def compact_text(text):
        return "".join(R._SEG_MARK_RE.sub("", text).split())

    def render_sample(renderer, name, reply, *, expected_note=None):
        drawn = []
        draw_text = R.ImageDraw.ImageDraw.text

        def record_text(draw, xy, text, *args, **kwargs):
            drawn.append(text)
            return draw_text(draw, xy, text, *args, **kwargs)

        with patch.object(R.ImageDraw.ImageDraw, "text", record_text):
            path = renderer.render(reply.title, reply.lines, reply.footer)

        fusion_prefix = ("· ⟦w⟧融合选项⟦/w⟧", "· ⟦w⟧Fusion options⟦/w⟧")
        expected = [line for line in reply.lines if line.startswith(fusion_prefix)]
        expected.extend(
            line for line in reply.lines if line.endswith(("切换路线。", "to switch routes."))
        )
        expected.append(reply.lines[-1])
        visible = compact_text("".join(drawn))
        check(
            f"{name} 融合选项与正文末行完整绘制",
            all(compact_text(line.lstrip("·※ ")) in visible for line in expected),
            str(expected),
        )
        if expected_note:
            check(
                f"{name} 灵化近战提示完整绘制",
                expected_note in reply.lines and compact_text(expected_note[2:]) in visible,
            )
        check(f"{name} 正文无溢出", bool(path and right_overflow(path)[1] == 0))
        return path

    class Host(RivenCommands, VisionCommands):
        cfg = {}
        damage_engine = SimpleNamespace(
            ensure_portable=AsyncMock(side_effect=AssertionError("随包命中仍连接引擎"))
        )
        riven_sale = SimpleNamespace(
            lookup=AsyncMock(
                return_value=sale_source.SaleResult(
                    "", "ok", sale_source.SaleRow("Burston Prime", "CC CD", "ZOOM", "仅供测试")
                )
            )
        )

    host = Host()
    request = AsyncMock(side_effect=AssertionError("随包命中仍调用实时接口"))
    cases = (
        (
            "zh_loadout_burston",
            "配卡推荐 伯斯顿p",
            (
                "灵化循环（攒条全弱点）",
                "随包参考 · 30 秒平均 DPS 830,907",
                "主要·无情（5级）",
                "私法 补给（0级，容量 2；套装特效 0 级满收益、省极化）",
                "武器面板",
                "灵化进化",
                "※ 默认展示灵化循环，可加「主射」「灵化形态」切换。",
            ),
        ),
        (
            "zh_loadout_nikana",
            "配卡推荐 侍刃p",
            ("近战·暴露（5级）", "镀层 斩铁", "创口溃烂", "架势　无明制裁", "443,871"),
        ),
        (
            "en_loadout_nikana",
            "loadoutrecommend Nikana Prime -en",
            (
                "Melee Exposure (rank 5)",
                "Galvanized Steel",
                "Weeping Wounds",
                "Stance　Blind Justice",
                "443,871",
            ),
        ),
        (
            "zh_traits_boar_31",
            "词条推荐 野猪 Prime",
            ("主射 KPM　灵化形态 KPM", "「XX套」为对应元素配装同套对照"),
        ),
        (
            "en_traits_boar_31",
            "riventraits Boar Prime -en",
            (
                "Primary fire KPM　Incarnon form KPM",
                "named element tags use a matching no-Riven build",
            ),
        ),
        (
            "zh_traits_braton_31",
            "词条推荐 布莱顿",
            ("主射 KPM　灵化形态 KPM", "⟦v⟧腐蚀套⟦/v⟧"),
        ),
        (
            "en_traits_braton_31",
            "riventraits Braton -en",
            ("Primary fire KPM　Incarnon form KPM", "⟦v⟧Corrosive⟦/v⟧"),
        ),
        (
            "zh_traits_burston_31",
            "词条推荐 伯斯顿p",
            (
                "※ 按满级 3+1− 中位值试算；「XX套」为对应元素配装同套对照。可加「2+1-」「超宏」「护盾」。",
                "主射 KPM　灵化形态 KPM",
                "⟦v⟧腐蚀套⟦/v⟧",
                "弱点命中",
                "看手感",
                "看弹道/衰减",
                "看续航",
                "按派系",
                "未模拟",
            ),
        ),
        (
            "zh_traits_lenz_31",
            "词条推荐 楞次弓",
            ("满级 3+1−", "弹药最大值", "⟦m⟧影响续航⟦/m⟧"),
        ),
        (
            "en_traits_lenz_31",
            "riventraits Lenz -en",
            ("Median max-rank 3+1−", "Ammo Maximum", "⟦m⟧Low ammo⟦/m⟧"),
        ),
        (
            "en_traits_burston_31",
            "riventraits Burston Prime -en",
            (
                "※ Median max-rank 3+1− rolls; named element tags use a matching no-Riven build. "
                'Add "2+1-", "overguard", or "shield".',
                "⟦v⟧Corrosive⟦/v⟧",
                "Weakpoint",
                "Handling",
                "Ballistics",
                "Reserve",
            ),
        ),
        (
            "zh_traits_burston_21",
            "词条推荐 伯斯顿p 2+1-",
            (
                "※ 按满级 2+1− 中位值试算；「XX套」为对应元素配装同套对照。默认看 3+1−，可加「超宏」「护盾」。",
                "主射 KPM　灵化形态 KPM",
                "弱点命中",
                "看组合",
                "低影响",
                "需权衡",
                "高风险",
                "弱点暴 ⟦v⟧+57.1%⟦/v⟧⟦n⟧（100%弱点）⟦/n⟧",
            ),
        ),
        (
            "en_traits_burston_21",
            "riventraits Burston Prime 2+1- -en",
            (
                "※ Median max-rank 2+1− rolls; named element tags use a matching no-Riven build. "
                'The default is 3+1−. Add "overguard" or "shield".',
                "Primary fire KPM　Incarnon form KPM",
                "Steel Path Thrax Centurion Lv170",
                "Fusion traits",
                "Weak Point Critical Chance ⟦v⟧+57.1%⟦/v⟧ ⟦n⟧(100% weakpoint)⟦/n⟧",
            ),
        ),
        (
            "zh_traits_akarius_21",
            "词条推荐 阿利乌双枪 Prime 2+1-",
            (
                "· ⟦w⟧融合选项⟦/w⟧  腐蚀 ⟦v⟧+39.8%⟦/v⟧ / "
                "弱点伤 ⟦v⟧+39.1%⟦/v⟧⟦n⟧（100%弱点）⟦/n⟧ / 弹效 ⟦v⟧+19.8%⟦/v⟧",
            ),
        ),
        (
            "en_traits_akarius_21",
            "riventraits Akarius Prime 2+1- -en",
            (
                "· ⟦w⟧Fusion options⟦/w⟧  Corrosive ⟦v⟧+39.8%⟦/v⟧ / "
                "Weak Point Damage ⟦v⟧+39.1%⟦/v⟧ ⟦n⟧(100% weakpoint)⟦/n⟧ / Ammo Efficiency ⟦v⟧+19.8%⟦/v⟧",
            ),
        ),
        (
            "en_loadout_burston",
            "loadoutrecommend Burston Prime -en",
            (
                "Primary Merciless (rank 5)",
                "Vigilante Supplies (rank 0, drain 2; full set bonus at rank 0, saves Forma)",
            ),
        ),
    )
    burston_notes = {
        "zh": [
            "※ 弱点边界：仅「弱点伤害/弱点暴击几率」按同套配装 100% 弱点命中单独试算，"
            "代表满弱点上限，不与躯干词条直比；不含弱点专属换卡/赋能，无弱点攻击（如范围爆炸）不生效。",
            "※ KPM 为每分钟击杀数。",
        ],
        "en": [
            "※ Weakpoint scope: only Weak Point Damage/Critical Chance use 100% weakpoint hits "
            "on the same build (upper bound; not directly comparable to body-hit traits); "
            "excludes weakpoint mod/arcane swaps, and hits without weakpoints (e.g., AoE blasts) gain 0%.",
            "※ KPM means kills per minute.",
        ],
    }
    output = os.environ.get("SDJK_PORTABLE_VISUAL_DIR")
    with (
        tempfile.TemporaryDirectory(prefix="wf-bundled-reference-") as folder,
        patch.object(portable_riven, "paths", SimpleNamespace(PKG_DATA=fixture)),
        patch.object(damage_engine, "portable_request", request),
    ):
        renderer = R.ImageRenderer(Path(folder))
        for filename, command, labels in cases:
            lang = filename.split("_")[0]
            with _i18n.use_lang(lang):
                parsed = parse(command)
                if "loadout" in filename:
                    reply = await host._h_loadout_recommend(parsed, None, "pc")
                    numbers = tuple(f"· {i:02d}　" for i in range(1, 9))
                    slots = [line.split("　") for line in reply.lines if line.startswith(numbers)]
                    check(
                        f"{filename} 8/8 槽均有拆卡 DPS",
                        len(slots) == 8
                        and all(
                            len(slot) == 7 and slot[-2].replace(",", "").isdigit() for slot in slots
                        ),
                        str(slots),
                    )
                else:
                    reply = await host._h_riven_traits(parsed, None, "pc")
                    mode_count = 1 if any(name in filename for name in ("lenz", "akarius")) else 3
                    if "traits_burston" in filename:
                        notes = [line for line in reply.lines if line.startswith("※")]
                        check(
                            f"{filename} 试算口径、弱点边界与 KPM 注释准确",
                            notes == [labels[0], *burston_notes[lang]],
                            str(notes),
                        )
                    headers = (
                        ("正词条", "负词条", "融合词条")
                        if filename.startswith("zh")
                        else ("Bonuses", "Maluses", "Fusion traits")
                    )
                    check(
                        f"{filename} 场景摘要、三组表和出售参考",
                        sum(
                            line.startswith(("◆ 单词条模拟推荐 · ", "◆ Single-trait simulation · "))
                            for line in reply.lines
                        )
                        == mode_count
                        and all(
                            any(
                                line.startswith("◆ ") and f"⟧{label}⟦/" in line
                                for line in reply.lines
                            )
                            for label in headers
                        )
                        and reply.lines[-3] in {"◆ 出售参考", "◆ Selling reference"},
                    )
                    summaries = [
                        line
                        for line in reply.lines
                        if "⟦w⟧低影响负词条" in line or "⟦w⟧Low-impact maluses" in line
                    ]
                    check(
                        f"{filename} 手感、弹道与备弹词条不进入低影响摘要",
                        all(
                            term.casefold() not in line.casefold()
                            for term in (
                                "后坐力",
                                "投射物速度",
                                "弹药最大值",
                                "Recoil",
                                "Projectile",
                                "Ammo",
                            )
                            for line in summaries
                        ),
                    )
                body = "\n".join(reply.lines)
                check(
                    f"{filename} 随包内容准确且无空白行",
                    all(label in body for label in labels)
                    and all(R._strip_emoji(line)[0].strip("· 　") for line in reply.lines)
                    and "仅供测试" not in body,
                    body[:1900],
                )
                with patch.object(
                    renderer, "_draw_build_cells", wraps=renderer._draw_build_cells
                ) as draw_cells:
                    path = render_sample(renderer, filename, reply)
                if "traits" in filename:
                    rows = [call.args[1] for call in draw_cells.call_args_list]
                    separators = mode_count + 1 if mode_count > 1 else 3
                    check(
                        f"{filename} 三组使用同一列坐标且每条词条均分列绘制",
                        bool(rows)
                        and len(rows) == sum(line.count("　") == separators for line in reply.lines)
                        and sum(row["thead"] for row in rows) == 3
                        and all(row["build_columns"] == rows[0]["build_columns"] for row in rows),
                    )
                    lang_index = int(filename.startswith("en"))
                    if any(name in filename for name in ("boar", "burston")):
                        invalid = ("灵化无效", "Incarnon N/A")[lang_index]
                        reserve = ("看续航", "Reserve")[lang_index]
                        charge = ("仅充能期", "Charge only")[lang_index]
                        for stat, expected_count in (
                            ("magazine_capacity", 2),
                            ("reload_speed", 2),
                            ("ammo_efficiency", 1),
                            ("ammo_max", 2),
                        ):
                            label = f"⟦w⟧{riven_rating.stat_label(stat, full=True)}⟦/w⟧"
                            cells = [row["cells"][2:] for row in rows if row["cells"][0] == label]
                            check(
                                f"{filename} {stat} 三种模式显示各自的弹药影响",
                                len(cells) == expected_count
                                and all(
                                    incarnon == f"⟦n⟧—⟦/n⟧ ⟦n⟧{invalid}⟦/n⟧"
                                    and (
                                        base == f"⟦n⟧—⟦/n⟧ ⟦v⟧{reserve}⟦/v⟧"
                                        if stat == "ammo_max"
                                        else base.startswith(("⟦y⟧+", "⟦m⟧-")) and "%⟦/" in base
                                    )
                                    and charge in cycle
                                    for base, incarnon, cycle in cells
                                ),
                                str(cells),
                            )
                    if any(name in filename for name in ("boar", "braton", "burston")):
                        tags = tuple(
                            f"⟦v⟧{label[lang_index]}⟦/v⟧"
                            for label in riven_rating._ELEMENT_BUILD_LABELS.values()
                        )
                        tagged = [
                            parts
                            for row in rows
                            for cell, parts in zip(row["cells"], row["build_cells"], strict=True)
                            if any(tag in cell for tag in tags)
                        ]
                        check(
                            f"{filename} 元素套标签单行显示",
                            bool(tagged) and all(len(parts) == 1 for parts in tagged),
                        )
                    if "boar" in filename:
                        compounds = ("corrosive", "viral", "blast", "gas", "magnetic", "radiation")
                        fusion_tags = {
                            f"⟦w⟧{riven_rating.stat_label(f'{element}_damage', full=True)}⟦/w⟧": (
                                f"⟦v⟧{riven_rating._ELEMENT_BUILD_LABELS[element][lang_index]}⟦/v⟧"
                            )
                            for element in compounds
                        }
                        fusion_rows = [
                            row["cells"] for row in rows if row["cells"][0] in fusion_tags
                        ]
                        check(
                            f"{filename} 六种复合元素无红色负值且灵化形态有同套标签",
                            len(fusion_rows) == len(compounds)
                            and all("⟦m⟧" not in cell for row in fusion_rows for cell in row[2:])
                            and any(fusion_tags[row[0]] in row[3] for row in fusion_rows),
                            str(fusion_rows),
                        )
                if path and output:
                    destination = Path(output)
                    destination.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(path, destination / f"{filename}.png")
        host.cfg = {"damage_engine_mode": "simulation", "damage_engine_portable_port": 8765}
        primary = await host._h_riven_traits(parse("词条推荐 伯斯顿p 主射 3+1"), None, "pc")
        rubico = await host._h_riven_traits(parse("词条推荐 绝路p 2+1"), None, "pc")
        check(
            "显式模式和构型短写只显示目标模式，配置引擎也无实时请求",
            sum("单词条模拟推荐" in line for line in primary.lines) == 1
            and "词条　牌面中位值　KPM 变化　备注" in primary.lines
            and not any("灵化" in line for line in primary.lines)
            and any("2+1−" in line for line in rubico.lines)
            and not request.await_count
            and not host.damage_engine.ensure_portable.await_count,
        )
        with patch.object(
            portable_riven, "recommendation_weapons", AsyncMock(return_value=((), {}))
        ) as live:
            for option, target in (
                ("超宏", "带超宏 · 无盾"),
                ("有盾", "去超宏 · 模拟有盾（基准 3600）"),
            ):
                command = f"词条推荐 伯斯顿p 2+1- {option}"
                reply = await host._h_riven_traits(parse(command), None, "pc")
                check(
                    f"{command} 命中对应随包表且零实时调用",
                    any(target in line for line in reply.lines)
                    and any(line.startswith("※ 按满级 2+1−") for line in reply.lines)
                    and not live.call_count
                    and not request.call_count
                    and not host.damage_engine.ensure_portable.call_count,
                )
                render_sample(renderer, command, reply)
            await host._h_riven_traits(parse("词条推荐 伯斯顿p 2+1- 超宏 护盾"), None, "pc")
            await host._h_loadout_recommend(parse("配卡推荐 伯斯顿p 百夫长9999"), None, "pc")
            await host._h_loadout_recommend(
                parse("配卡推荐 伯斯顿p 赋能 主要·无情 5级"), None, "pc"
            )
            await host._h_riven_traits(parse("词条推荐 楞次弓 灵化循环"), None, "pc")
            check("自定义靶标、辅装和未收录模式进入实时目录", live.await_count == 4)
        for lang in ("zh", "en"):
            with _i18n.use_lang(lang):
                result = {
                    "evaluationScenario": {"incarnonCycle": True},
                    "recommendedPositiveTiers": [
                        {"stats": [{"label": "Reload Speed", "code": "F", "gain": 6.5}]}
                    ],
                    "recommendedNegative": [],
                    "fatalNegative": [],
                    "fatalNegativeAbove": {},
                }
                reply = SimpleNamespace(
                    title=_i18n.L(
                        "词条推荐 · 伯斯顿 Prime", "Riven Reroll Suggestions · Burston Prime"
                    ),
                    lines=portable_riven.trait_lines(result),
                    footer="",
                )
                note = _i18n.L("仅充能期", "Charge only")
                check(
                    f"{lang} 灵化循环保留装填收益并标注充能期",
                    any("⟦y⟧+6.5%⟦/y⟧" in line for line in reply.lines)
                    and any(f"⟦v⟧{note}⟦/v⟧" in line for line in reply.lines),
                )
                render_sample(renderer, f"{lang}_traits_burston_cycle", reply)

        for lang, labels in (
            ("zh", ("主射", "灵化形态", "灵化循环")),
            ("en", ("Primary fire", "Incarnon form", "Incarnon cycle")),
        ):
            with _i18n.use_lang(lang):
                reply = await host._h_riven_traits(parse("词条推荐 Burston Prime"), None, "pc")
                headings = [line for line in reply.lines if line.startswith(("词条　", "Trait　"))]
                expected_modes = [f"{label} KPM" for label in labels]
                check(
                    f"{lang} Burston Prime 三模式表头使用五列",
                    bool(headings)
                    and all(line.split("　")[2:] == expected_modes for line in headings),
                    str(headings),
                )
                for table in ("随包", "实时"):
                    if table == "实时":
                        bundled = _i18n.L(
                            "◆ 随包参考配装 · 全词条", "◆ Bundled reference builds · All traits"
                        )
                        live = _i18n.L(
                            "◆ 实时参考配装 · 全词条", "◆ Live reference builds · All traits"
                        )
                        reply.lines[reply.lines.index(bundled)] = live
                    name = f"{lang} Burston Prime {table}三模式"
                    with patch.object(R, "MAX_BODY_LINES", 30):
                        image = render_sample(renderer, name, reply)
                    with R.Image.open(image) as card:
                        check(
                            f"{name} 宽卡在专用上限内",
                            R.CARD_W_MIN <= card.width <= R.CARD_W_TRAITS_MAX,
                            str(card.size),
                        )

        for name, labels in (
            ("Magistar", ("重击震地", "Heavy slam")),
            ("Sancti Magistar", ("重击震地", "Heavy slam")),
            ("Glaive Prime", ("重击投掷", "Glaive throw")),
            ("Nikana Prime", ("平砍", "Base")),
        ):
            for lang, label in zip(("zh", "en"), labels, strict=True):
                with _i18n.use_lang(lang):
                    traits = await host._h_riven_traits(parse(f"词条推荐 {name}"), None, "pc")
                    build = await host._h_loadout_recommend(parse(f"配卡推荐 {name}"), None, "pc")
                    heading = _i18n.L(
                        "词条　牌面中位值　KPM 变化　备注", "Trait　Median roll　KPM change　Notes"
                    )
                    check(
                        f"{lang} {name} 默认词条与配卡只显示 {label}",
                        traits.lines[1].startswith(f"· {label} · ")
                        and build.lines[0].startswith(f"◆ {label} · ")
                        and sum(
                            line.startswith(("◆ 单词条模拟推荐", "◆ Single-trait simulation"))
                            for line in traits.lines
                        )
                        == 1
                        and heading in traits.lines,
                    )
                    note = next(
                        line
                        for line in traits.lines
                        if line.endswith(("切换路线。", "to switch routes."))
                    )
                    check(f"{lang} {name} 两张卡均有路线切换注释", note in build.lines)
                    expected_note = None
                    if name == "Magistar":
                        expected_note = _i18n.L(
                            "※ 灵化近战提示：已计入进化 2–4，进化 1 灵化变身期间的临时加成上游尚未建模，未计入当前数值。",
                            "※ Incarnon Melee note: Tier 2–4 evolutions are modeled; "
                            "temporary Tier 1 Incarnon form bonuses are not yet modeled upstream.",
                        )
                    render_sample(
                        renderer, f"{lang} {name} 默认词条", traits, expected_note=expected_note
                    )
                    render_sample(
                        renderer, f"{lang} {name} 默认配卡", build, expected_note=expected_note
                    )


_asyncio.run(_bundled_reference_smoke_cards())


with tempfile.TemporaryDirectory(prefix="wf-render-png-lifetime-") as _folder:
    _r = R.ImageRenderer(Path(_folder))
    _pngs = [_r.render(f"保留图片{i}", [f"第{i}张待交付正文"]) for i in range(3)]
    check("三张真实图片均成功渲染", all(_pngs))
    check("第三张渲染不能抢删尚未交付的首张", all(p and Path(p).exists() for p in _pngs))
    _unrelated = Path(_folder) / "user-image.png"
    _unrelated.write_bytes(b"unrelated image")
    _r._cleanup(keep=1)
    check("离线显式清理仍保留指定数量", len(list(Path(_folder).glob("card_*.png"))) == 1)
    check("离线显式清理不碰非卡片文件", _unrelated.read_bytes() == b"unrelated image")

# 启动兜底：重载 / 重启丢失延迟清理任务后，旧卡片图在下次构造渲染器时按修改时间清掉
with tempfile.TemporaryDirectory() as _folder:
    import os as _os
    import time as _time

    _old = Path(_folder) / "card_00000000aa.png"
    _new = Path(_folder) / "card_00000000bb.png"
    _other = Path(_folder) / "user-image.png"
    for _p in (_old, _new, _other):
        _p.write_bytes(b"x")
    _past = _time.time() - 3600
    _os.utime(_old, (_past, _past))
    _os.utime(_other, (_past, _past))
    R.ImageRenderer(Path(_folder))
    check("启动清理删除超过 10 分钟的旧卡片图", not _old.exists())
    check("启动清理保留缓冲期内的新卡片图", _new.exists())
    check("启动清理不碰非卡片文件", _other.exists())

print()


def _canvas_budget_checks():
    from unittest.mock import patch

    canvas = R.Image.new("RGB", (64, 384))
    banner = R.Image.new("RGB", (256, 16))
    for x in range(banner.width):
        for y in range(banner.height):
            banner.putpixel((x, y), (x, y, 0))
    resize = R.Image.Image.resize
    sizes = []

    def measured(image, size, *args, **kwargs):
        sizes.append(size)
        assert size[0] <= canvas.width and size[1] <= canvas.height
        return resize(image, size, *args, **kwargs)

    with patch.object(R.Image.Image, "resize", measured):
        for pos, red in (("l", 1), ("c", 128), ("r", 254)):
            out = R._with_bg_image(canvas, banner, 0, "cover", pos)
            check(f"横幅长卡 {pos} 裁切位置", abs(out.getpixel((32, 192))[0] - red) <= 1)
    check("横幅铺长卡只缩放可见区域至画布", sizes == [canvas.size] * 3)

    # 用小棋盘格逐像素比对旧铺图，覆盖裁剪边沿依赖邻像素的情形。
    from PIL import ImageChops

    for source_size, target in (((9, 12), (41, 18)), ((160, 80), (10, 11))):
        src = R.Image.new("RGB", source_size)
        src.putdata(
            [(255 * (x % 2), 255 * (y % 2), 0) for y in range(src.height) for x in range(src.width)]
        )
        w, h = target
        scale = max(w / src.width, h / src.height)
        full = src.resize(
            (int(src.width * scale + 0.5), int(src.height * scale + 0.5)), R.Image.BILINEAR
        )
        for pos in R.BG_POSITIONS:
            fx, fy = R._anchor(pos)
            x, y = int((full.width - w) * fx), int((full.height - h) * fy)
            expected = full.crop((x, y, x + w, y + h))
            actual = R._with_bg_image(R.Image.new("RGB", target), src, 0, "cover", pos)
            diff = ImageChops.difference(actual, expected)
            check(
                f"cover {source_size} 九宫格 {pos} 滤波等价",
                max(high for _, high in diff.getextrema()) <= 1,
            )

    if not probe.available:
        return
    title, lines = "画布预算", ["很长的装备说明与掉落地点" * 40]
    with tempfile.TemporaryDirectory() as folder:
        renderer = R.ImageRenderer(Path(folder))
        normal = renderer._render(title, lines, "")
        assert normal
        with R.Image.open(normal) as output:
            pixels = output.width * output.height * R.SS**2
        with patch.object(R, "MAX_CANVAS_PIXELS", pixels):
            check("折行后 2x 画布恰好到预算仍出图", bool(renderer._render(title, lines, "")))
        with patch.object(R, "MAX_CANVAS_PIXELS", pixels - 1), patch.object(R, "_bg_column") as bg:
            check(
                "折行后超预算一个像素即在分配前降级",
                renderer._render(title, lines, "") is None and not bg.called,
            )


def _gradient_equivalence():
    from io import BytesIO

    previous = R.theme_state()
    try:
        for theme in ({}, {"bg": "#ed81c3"}):
            R.apply_theme(theme)
            for size in ((1, 1), (31, 84), (32, 255), (33, 256), (34, 513), (35, 514)):
                expected = R._bg_column().resize(size, R.Image.BILINEAR)
                actual = R._gradient_background(*size)
                assert actual.tobytes() == expected.tobytes(), (theme, size)
                old, new = BytesIO(), BytesIO()
                expected.save(old, format="PNG")
                actual.save(new, format="PNG")
                assert new.getvalue() == old.getvalue(), (theme, size)
    finally:
        R.apply_theme(**previous)


def _background_windows():
    from unittest.mock import patch
    from PIL import ImageChops

    canvas = R.Image.new("RGB", (1600, 600))
    narrow = R.Image.new("RGB", (1, 512))
    narrow.putdata([(y // 2, 0, 0) for y in range(narrow.height)])
    resize = R.Image.Image.resize
    sizes = []

    def bounded_resize(image, size, *args, **kwargs):
        sizes.append(size)
        assert size[0] <= canvas.width and size[1] <= canvas.height
        assert size[0] * size[1] <= R.MAX_CANVAS_PIXELS
        return resize(image, size, *args, **kwargs)

    with patch.object(R.Image.Image, "resize", bounded_resize):
        for mode, pos, red in (
            ("width", "b", 0),
            ("custom", "t", 0),
            ("custom", "c", 128),
            ("custom", "b", 255),
        ):
            image = R._with_bg_image(canvas, narrow, 0, mode, pos, 100)
            assert abs(image.getpixel((800, 300))[0] - red) <= 1
        assert sizes == [canvas.size] * 4
        for mode in ("contain", "tile"):
            assert R._with_bg_image(canvas, narrow, 0, mode).size == canvas.size

    small = R.Image.new("RGB", (8, 8))
    large = R.Image.new("RGBA", (16, 16))
    with (
        patch.object(R, "MAX_CANVAS_PIXELS", 64),
        patch.object(R.Image.Image, "convert", side_effect=AssertionError("超预算转色")),
    ):
        assert R._with_bg_image(small, large, 0, "contain") is small

    # 小图可安全生成完整参考图，逐像素核对短图重复、平铺余边和九宫格。
    canvas = R.Image.new("RGB", (30, 21), "navy")
    for size in ((11, 3), (3, 11)):
        source = R.Image.new("RGBA", size)
        source.putdata([(x * 23, y * 23, 10, 128) for y in range(size[1]) for x in range(size[0])])
        for fit, scale in (
            ("width", 1),
            ("custom", 0.6),
            ("custom", 1.5),
            ("contain", 1),
            ("tile", 1),
        ):
            k = {"tile": R.SS, "contain": min(canvas.width / size[0], canvas.height / size[1])}.get(
                fit, canvas.width * scale / size[0]
            )
            full = source.convert("RGB").resize(
                tuple(max(1, int(v * k + 0.5)) for v in size), R.Image.BILINEAR
            )
            for pos in R.BG_POSITIONS:
                expected = canvas.copy()
                fx, fy = R._anchor(pos)
                if fit in ("width", "tile"):
                    for y in range(0, canvas.height, full.height):
                        for x in range(0, canvas.width, full.width):
                            expected.paste(full, (x, y))
                else:
                    expected.paste(
                        full,
                        (
                            int((canvas.width - full.width) * fx),
                            int((canvas.height - full.height) * fy),
                        ),
                    )
                actual = R._with_bg_image(canvas, source, 0, fit, pos, scale * 100)
                diff = ImageChops.difference(expected, actual)
                assert max(hi for _, hi in diff.getextrema()) <= 1, (fit, size, pos, scale)


if R.Image is not None:
    _gradient_equivalence()
    _canvas_budget_checks()
    _background_windows()

if FAILED:
    print(f"FAILED {len(FAILED)}: " + ", ".join(FAILED))
    sys.exit(1)
print("ALL PASS")
