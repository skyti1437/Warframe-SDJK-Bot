# -*- coding: utf-8 -*-
"""下载卡片渲染用的中文字体（Noto Sans CJK）。

为什么字体不直接放仓库里
------------------------
Noto Sans CJK 的 Regular + Bold 两个 ttc 加起来约 40 MB，比插件本体还大：
  · 仓库体积翻 5 倍，每次更新都要重新传一遍；
  · 会让 zip 超过 AstrBot 插件市场的 16 MB 上限，失去市场分发渠道。

所以字体按需下载：**装完跑一次本脚本**即可。下载**落点 = 插件数据目录**
``<AstrBot>/data/plugin_data/astrbot_plugin_warframe/fonts/``（AstrBot 开发原则：
持久化数据进 data 目录，别放插件自身目录——放包内更新/重装会被整包替换掉，
issue #1 报告者就是这么丢的）。部署布局识别不出来时回落到插件内
``core/data/fonts/`` 并**打印告警**（仅开发树适用）。可用 ``--data-dir`` 显式指定。
不跑也能用 —— ``core/render.py`` 的字体候选里带了各平台常见中文字体
（Windows 的 msyh.ttc、macOS 的 PingFang、Linux 的 Noto/WQY），
只是字形可能与作者出图略有差异，Linux 服务器上若一个都没装则会渲染成方块。

用法：
    python scripts/fetch_font.py            # 下载 Regular + Bold
    python scripts/fetch_font.py --check    # 只看当前用的是哪个字体
"""
from __future__ import annotations

import argparse
import os
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = "astrbot_plugin_warframe"
LEGACY_DIR = ROOT / "core" / "data" / "fonts"      # 插件包内：旧落点/迁移源 + 随包资产


def discover_data_root() -> Path | None:
    """推断 AstrBot 的 data 根目录。

    优先环境变量 ``ASTRBOT_DATA``；否则按部署布局推断——本脚本在
    ``<AstrBot>/data/plugins/<插件>/scripts/`` 下时，其 `data` 祖先即所求。
    开发树（仓库根直接跑）推断不出来，返回 None。
    """
    env = os.environ.get("ASTRBOT_DATA")
    if env and (Path(env) / "plugin_data").is_dir():
        return Path(env)
    for up in Path(__file__).resolve().parents:
        if up.name == "data" and (up / "plugin_data").is_dir():
            return up
    return None


def resolve_font_dir(explicit: str = "") -> Path:
    """字体落点：显式 --data-dir > 自动推断的 plugin_data > 开发树回落（带告警）。"""
    if explicit:
        return Path(explicit).expanduser() / "plugin_data" / PLUGIN / "fonts"
    root = discover_data_root()
    if root:
        return root / "plugin_data" / PLUGIN / "fonts"
    print("⚠ 未识别出 AstrBot 部署布局（在插件数据目录外运行？）——"
          "本次下载将落到插件包内 core/data/fonts/，"
          "**更新/重装插件会丢**；部署环境请加 --data-dir <AstrBot>/data")
    return LEGACY_DIR

# 按顺序尝试的下载源：jsDelivr CDN（国内通常可达）→ GitHub raw
SOURCES = {
    "NotoSansCJK-Regular.ttc": [
        "https://cdn.jsdelivr.net/gh/notofonts/noto-cjk@main/Sans/OTC/"
        "NotoSansCJK-Regular.ttc",
        "https://raw.githubusercontent.com/notofonts/noto-cjk/main/Sans/OTC/"
        "NotoSansCJK-Regular.ttc",
    ],
    "NotoSansCJK-Bold.ttc": [
        "https://cdn.jsdelivr.net/gh/notofonts/noto-cjk@main/Sans/OTC/"
        "NotoSansCJK-Bold.ttc",
        "https://raw.githubusercontent.com/notofonts/noto-cjk/main/Sans/OTC/"
        "NotoSansCJK-Bold.ttc",
    ],
}

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def _download(url: str, dst: Path) -> bool:
    """下载到临时文件再改名（中断不会留半个字库）。"""
    tmp = dst.with_suffix(dst.suffix + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=300) as r, \
                open(tmp, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            got = 0
            while True:
                chunk = r.read(256 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                got += len(chunk)
                if total:
                    pct = got * 100 // total
                    print(f"\r    {pct:3d}%  {got / 1048576:.1f}/"
                          f"{total / 1048576:.1f} MB", end="", flush=True)
        print()
        if tmp.stat().st_size < 1024 * 1024:
            raise ValueError(f"文件过小（{tmp.stat().st_size} 字节），疑似不是字库")
        tmp.replace(dst)
        return True
    except Exception as exc:                     # noqa: BLE001
        print(f"\n    ✗ {type(exc).__name__}: {str(exc)[:80]}")
        tmp.unlink(missing_ok=True)
        return False


def check(font_dir: Path) -> int:
    """报告当前渲染实际会用到哪个字体（与 render._Fonts 的候选顺序一致）。"""
    FONT_DIR = font_dir
    sys.path.insert(0, str(ROOT))
    try:
        from core.render import _Fonts
    except Exception as exc:                     # noqa: BLE001
        print(f"无法导入 render._Fonts：{exc}")
        return 1
    f = _Fonts(user_dirs=[FONT_DIR])
    print(f"  regular = {f.regular}")
    print(f"  bold    = {f.bold}")
    reg = str(f.regular or "")
    if FONT_DIR in Path(reg).parents:
        print("  → 用的是本脚本下载的完整字库 ✔")
    elif "Subset" in reg:
        print("  → 用的是随包子集字体（开箱默认；想要完整字形跑一次本脚本）")
    else:
        print("  → 用的是系统字体（想与作者出图一致，请跑一次本脚本）")
    print(f"  完整字库落点：{FONT_DIR}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只检查不下载")
    ap.add_argument("--data-dir", default="",
                    help="AstrBot 的 data 目录（默认自动推断；指定后写入其 plugin_data/）")
    args = ap.parse_args()
    font_dir = resolve_font_dir(args.data_dir)
    if args.check:
        return check(font_dir)

    FONT_DIR = font_dir
    FONT_DIR.mkdir(parents=True, exist_ok=True)
    ok = 0
    for name, urls in SOURCES.items():
        dst = FONT_DIR / name
        if dst.exists() and dst.stat().st_size > 1024 * 1024:
            print(f"[跳过] {name}（已存在 {dst.stat().st_size / 1048576:.1f} MB）")
            ok += 1
            continue
        print(f"[下载] {name}")
        for u in urls:
            print(f"    ← {u[:78]}")
            if _download(u, dst):
                print(f"    ✓ 完成 {dst.stat().st_size / 1048576:.1f} MB")
                ok += 1
                break
        else:
            print(f"    ✗ 所有下载源都失败：{name}")
    print()
    if ok == len(SOURCES):
        print("字体就绪 ✔  卡片会使用与本项目出图一致的 Noto Sans CJK")
        return 0
    print("部分字体缺失：仍可用（回落系统字体），但字形可能与作者出图不同。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
