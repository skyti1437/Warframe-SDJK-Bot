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


# ------------------------------------------------------------------ 页码正则
# 量词各 handler 不统一：条 / 场 / 个 / 件 / 项
PAGE_CASES = [
    ("仲裁时间表（第1/23页，共336条）", ("1", "23", "336"), "仲裁时间表"),
    ("开核桃建议（第1/4页，共32场）", ("1", "4", "32"), "开核桃建议"),
    ("遗物列表：当前可掉落 34，已入库 734（第1/77页，共768个）",
     ("1", "77", "768"), "遗物列表：当前可掉落 34，已入库 734"),
    ("遗物入库（已入库、不可刷取）（第1/74页，共734个）",
     ("1", "74", "734"), "遗物入库（已入库、不可刷取）"),
    ("膛线 在售单（第1/39页，共386条）", ("1", "39", "386"), "膛线 在售单"),
]
for title, groups, rest in PAGE_CASES:
    m = R._PAGE_RE.search(title)
    check(f"页码可识别：{title[:20]}…",
          bool(m) and m.groups() == groups,
          f"groups={m.groups() if m else None}")
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
        LUM = 125     # 正文暖白/亮金/青均 > 125；面板底/暗金框约 17~102

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
            check("6 列表格：正文未越出面板右缘", n == 0,
                  f"W={W} 越界 {n} 像素，最右 x={mx}")
        else:
            check("6 列表格：渲染成功", False, "render() 返回 None 已降级文本")

        # 2) 长标题（页码须已被抽走，标题自适应不得压到徽章）
        long_title = ("遗物列表：当前可掉落 34，已入库 734"
                      "（第1/77页，共768个）")
        p = probe.render(long_title, ["[可掉落]中纪 Neo T11", "掉落位置：金星 · Romula · 防御（C轮）"], "")
        if p:
            im = R.Image.open(p).convert("RGB")
            W, H = im.size
            rgb = im.load()
            # 徽章右缘 = W-64；标题带里 x ∈ [W-56, W-4] 必须为空
            hot = sum(
                1 for x in range(W - 56, W - 4)
                for y in range(68, 118)
                if 0.2126 * rgb[x, y][0] + 0.7152 * rgb[x, y][1]
                + 0.0722 * rgb[x, y][2] > LUM)
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
    src = next((p for p in _R._Fonts.SYSTEM if p.exists()), None)
    if src is None:
        print("[SKIP] 本机无系统 CJK 字体，跳过用户字体目录用例")
        return
    tmp = Path(tempfile.mkdtemp(prefix="wf_fonts_"))
    try:
        shutil.copy2(src, tmp / "MyCJK.ttc")
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
        shutil.copy2(src, fonts_dir / "MyCJK.ttc")
        _R._Fonts.PACKED, _R._Fonts.SYSTEM = [], []
        try:
            r = _R.ImageRenderer(tmp / "cards")
            check("ImageRenderer 采纳 plugin_data/fonts 下的用户字体",
                  r.available is True, f"available={r.available}")
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
_SUB = {"Regular": _FONT_DIR / "NotoSansCJKsc-Subset-Regular.otf",
        "Bold": _FONT_DIR / "NotoSansCJKsc-Subset-Bold.otf"}
_FULL = {"Regular": _FONT_DIR / "NotoSansCJK-Regular.ttc",
         "Bold": _FONT_DIR / "NotoSansCJK-Bold.ttc"}


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
        ("符号压力卡", ["★⚠※▣◆→⏳①②③④⑤·—…", "（）、。，；：？！“”「」《》【】",
                        "中英混排 ABC 123 % + - / : 4.25x"]),
    ]
    try:
        from core import formatters as F
        cards.append(F.fmt_timers([("仲裁", None),
                                   ("每日突击", {"expiry": "2030-01-01T00:00:00+00:00"})]))
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
        eff = _json.loads((Path(__file__).resolve().parent.parent
                           / "core" / "data" / "wiki_effect_zh.json")
                          .read_text(encoding="utf-8")).get("effects") or {}
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
        print(f"[INFO] 语料非 ASCII {len(corpus)} 个；完整字库也缺 {len(both_missing)} 个"
              f"（原有，非子集引入）：{''.join(both_missing[:12])}")
    else:
        need = corpus
    missing = sorted(c for c in need if ord(c) not in cmap)
    check("子集覆盖「完整字库能渲染的全部语料字符」", not missing,
          f"缺 {len(missing)} 个：{missing[:12]}")
    syms = "★⚠※▣◆→①②③④⑤·—…“”「」（）、。，；：？！%"
    check("UI 符号与 ASCII 标点字形齐备", all(ord(s) in cmap for s in syms),
          str([s for s in syms if ord(s) not in cmap]))
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
        import core.render as _R
        old_p, old_s = _R._Fonts.PACKED, _R._Fonts.SYSTEM
        for i, (title, lines) in enumerate(cards):
            r_full = _render_one(_FULL["Regular"], _FULL["Bold"], title, lines,
                                 tmp / f"full_{i}")
            r_sub = _render_one(_SUB["Regular"], _SUB["Bold"], title, lines,
                                tmp / f"sub_{i}")
            if not r_full or not r_sub or not Path(r_full).exists() or not Path(r_sub).exists():
                check(f"对拍渲染成功：{title}", False, f"{r_full} / {r_sub}")
                continue
            ia, ib = Image.open(r_full).convert("RGB"), Image.open(r_sub).convert("RGB")
            if ia.size != ib.size:
                check(f"逐像素一致：{title}", False, f"尺寸 {ia.size} vs {ib.size}")
                continue
            bbox = ImageChops.difference(ia, ib).getbbox()
            check(f"逐像素一致（完整库 vs 子集）：{title}", bbox is None,
                  f"差异区域 {bbox}")
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

print()
if FAILED:
    print(f"FAILED {len(FAILED)}: " + ", ".join(FAILED))
    sys.exit(1)
print("ALL PASS")
