# -*- coding: utf-8 -*-
"""字体子集化构建器（2026-09-25）：把 Noto Sans CJK SC 裁成随包分发的子集。

背景：市场包上限 16MB，完整字库 38MB 不能进包；此前市场版出图**完全依赖
系统中文字体**（Linux 容器一个都没有就渲染成方块，issue #1 用户实测）。
本脚本产出「够用且小」的子集，随 v1.0.7 起进包 —— 市场用户开箱出图。

用法（先确保 core/data/fonts/ 有完整 ttc，由 scripts/fetch_font.py 下载）：
    pip install fonttools
    python scripts/build_font_subset.py            # 两档（Regular + Bold）
    python scripts/build_font_subset.py --regular-only

字符集来源（全部并入，宁多勿漏）：
  1. **实测语料**：仓库里所有 .py / core/data/*.json / kb/*.md 出现过的非 ASCII 字符
     （卡片文案、数据表里的物品名、机制术语等）——这是覆盖率的硬依据；
  2. ASCII 可打印区（0x20–0x7E，英文名/数字/百分号）；
  3. CJK 标点与全角块（U+3000–U+303F、U+FF00–U+FFEF，动态文本兜底）；
  4. 常用汉字补充表（GB2312 全表 6763 字，防用户查询/市场名出现语料外汉字；
     `--no-extra-cjk` 可关）——体积换覆盖，实测后按预算取舍。

产物：core/data/fonts/NotoSansCJKsc-Subset-{Regular,Bold}.otf + charset.txt
（charset 一并提交，便于复现与审计）。
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FONT_DIR = ROOT / "core" / "data" / "fonts"
FULL = {"Regular": FONT_DIR / "NotoSansCJK-Regular.ttc",
        "Bold": FONT_DIR / "NotoSansCJK-Bold.ttc"}
OUT = {"Regular": FONT_DIR / "NotoSansCJKsc-Subset-Regular.otf",
       "Bold": FONT_DIR / "NotoSansCJKsc-Subset-Bold.otf"}
CHARSET = ROOT / "scripts" / "font_subset_charset.txt"   # 开发物料（不进市场件）

# .ttc 里的 SC 面索引（fontTools TTCollection 顺序，2026-09-25 实测两档均为 2）
SC_FACE_INDEX = 2

# 人工补充符号（卡片版式用到、但语料里可能只以转义形式出现的）
EXTRA_SYMBOLS = "★☆⚠※▣◈◆◇●○▲▶▼←↑→↓↔⇄⇒≈≠≤≥∈∪∩√−×÷°′″～～·—–…‥“”‘’「」『』（）【】《》〈〉〔〕、。，；：？！⏳①②③④⑤⑥⑦⑧⑨⑩"


def _iter_sources():
    for p in list(ROOT.glob("*.py")) + list((ROOT / "core").rglob("*.py")):
        yield p
    for p in (ROOT / "core" / "data").rglob("*.json"):
        yield p
    for p in (ROOT / "kb").rglob("*.md"):
        yield p
    for p in (ROOT / "scripts").rglob("*.py"):
        yield p


def collect_chars(extra_cjk: bool) -> set[str]:
    chars: set[str] = set()
    for p in _iter_sources():
        try:
            chars.update(p.read_text(encoding="utf-8", errors="ignore"))
        except OSError:
            continue
    # 只保留非 ASCII（ASCII 单独成区间，下面显式并回）
    chars = {c for c in chars if ord(c) > 0x7F}
    # ★ ASCII 可打印区必须显式并入（2026-09-25 修复：此前只在 docstring 写了，
    #   代码漏加 → 子集里没有 `%`/`+`/`/`/`:` 等，卡面百分号会变豆腐块）
    chars.update(chr(c) for c in range(0x20, 0x7F))
    chars.update(EXTRA_SYMBOLS)
    if extra_cjk:
        chars.update(_gb2312())
    # 去重 + 排序（可复现）
    return chars


def _gb2312() -> set[str]:
    """GB2312 全表汉字（一级 3755 + 二级 3008 = 6763 字，0xB0A1–0xF7FE）。

    覆盖现代简体中文的 99.7%+ 用字——比「只收实测语料」多一层保险：卡面还会
    出现**运行时动态文本**（WM 实时物品名、用户查询词、wiki 页面名），
    这些字不一定出现在仓库语料里。实测代价约 +1.5MB/档（预算内）。
    """
    out: set[str] = set()
    for hi in range(0xB0, 0xF8):
        for lo in range(0xA1, 0xFF):
            try:
                out.add(bytes([hi, lo]).decode("gb2312"))
            except UnicodeDecodeError:
                continue
    return out


def write_charset(chars: set[str]) -> int:
    items = sorted(chars)
    CHARSET.write_text("".join(items), encoding="utf-8")
    return len(items)


def subset(weight: str, chars: set[str]) -> tuple[int, int]:
    src, dst = FULL[weight], OUT[weight]
    if not src.exists():
        raise SystemExit(f"缺完整字库 {src}（先跑 python scripts/fetch_font.py）")
    text = "".join(sorted(chars))
    # ★ 用 --text-file 传字符集；--font-number 选 SC 面；保留默认 layout 特性
    cmd = [sys.executable, "-m", "fontTools.subset", str(src),
           f"--font-number={SC_FACE_INDEX}",
           f"--output-file={dst}",
           f"--text={text}",
           "--layout-features=*",      # 保留全部 GSUB/GPOS：渲染与完整字库逐像素一致
           "--name-IDs=*", "--name-legacy", "--name-languages=*",
           "--notdef-outline", "--recommended-glyphs"]
    subprocess.run(cmd, check=True)
    return src.stat().st_size, dst.stat().st_size


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--regular-only", action="store_true", help="只产出 Regular 一档")
    ap.add_argument("--no-extra-cjk", action="store_true", help="不并入 GB2312 一级字表")
    args = ap.parse_args()

    chars = collect_chars(extra_cjk=not args.no_extra_cjk)
    n = write_charset(chars)
    cjk = sum(1 for c in chars if "\u4e00" <= c <= "\u9fff")
    print(f"字符集：合计 {n}（CJK 表意 {cjk} + 其他 {n - cjk}）→ {CHARSET.relative_to(ROOT)}")

    weights = ["Regular"] if args.regular_only else ["Regular", "Bold"]
    total_out = 0
    for w in weights:
        before, after = subset(w, chars)
        total_out += after
        print(f"  {w}: {before/1048576:6.1f} MB → {after/1048576:5.2f} MB"
              f"（{OUT[w].name}）")
    print(f"子集合计 {total_out/1048576:.2f} MB（两档）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
