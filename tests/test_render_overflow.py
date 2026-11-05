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
    names = {"_build_content_results", "_render_card", "_schedule_card_cleanup"}
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
        "asyncio": asyncio,
        "Path": Path,
        "Optional": object,
        "Reply": Reply,
        "Parsed": Parsed,
        "AstrMessageEvent": object,
        "logger": SimpleNamespace(warning=lambda *args: warnings.append(args)),
        "Image": SimpleNamespace(fromFileSystem=lambda path: path),
        "Plain": lambda text: text,
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
if FAILED:
    print(f"FAILED {len(FAILED)}: " + ", ".join(FAILED))
    sys.exit(1)
print("ALL PASS")
