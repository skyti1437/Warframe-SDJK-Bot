# -*- coding: utf-8 -*-
"""产出**插件市场专用**的 flat zip（无顶层目录）。

为什么要单独一个脚本：市场与「本地安装」要的 zip 结构**不一样**。

* `dist/package_release.py` 产出的 `astrbot_plugin_warframe.zip` 是
  **嵌套一层** `astrbot_plugin_warframe/...` —— 适合 AstrBot 从 zip 安装插件。
* 市场（cloud.astrbot.app 上传压缩包通道）要的是 **flat**：`main.py` / `README.md`
  直接在最外层。2026-09-19 下载线上 v1.0.2 产物核实：159 条目、2.41MB、
  顶层元素就是 `.gitattributes / .github / README.md / _conf_schema.json …`，
  **没有** 插件名目录。GitHub 的 "Download ZIP" 因为会多套一层 repo-branch/ 而解析失败。

用法（先跑 `python dist/package_release.py --opensource` 准备好开源目录）：

    python scripts/make_market_zip.py [输出路径]

默认输出 `dist/astrbot_plugin_warframe-<版本>-market.zip`。
"""
from __future__ import annotations

import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OSS_DIR = ROOT / "dist" / "opensource" / "astrbot_plugin_warframe"

# 排除清单优先复用打包脚本的（避免两处走散）；但 `dist/` 不进分发包，
# 所以别人拿到的是开源包时这个 import 会失败 —— 那时退回下面这份等价清单。
try:
    sys.path.insert(0, str(ROOT / "dist"))
    import package_release as pr  # noqa: E402

    SKIP_DIRS = set(pr.EXCLUDE_DIRS) | set(pr.OSS_EXCLUDE_DIRS)
    SKIP_FILES = set(pr.EXCLUDE_FILES) | set(pr.OSS_EXCLUDE_FILES)
    SKIP_SUFFIX = set(pr.EXCLUDE_SUFFIX)
    SKIP_GLOBS = tuple(pr.EXCLUDE_GLOBS)
    SKIP_SOURCE = "dist/package_release.py"
except Exception:  # noqa: BLE001
    SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", "runtime", ".workbuddy",
                 ".venv", "venv", "node_modules", ".idea", ".vscode", "dist",
                 ".audit", "kb_src", "_cache", "docs", "outputs", "fonts"}
    SKIP_FILES = {"deploy.sh", ".DS_Store", ".gitattributes", ".gitignore",
                  "SDJKwfbot_README.md", "wm_ranks.json", "riven_weekly.json",
                  "wiki_disp.json", "package_reverse_searcher_sdjk.py"}
    SKIP_SUFFIX = {".pyc", ".pyo"}
    SKIP_GLOBS = ("*报告*.md", "*调研*.md", "*诊断*.md", "*对照*.md", "*核验*.md",
                  "*体检*.md", "*复评*.md", "*选型*.md", "*实测*.md", "*结案*.md",
                  "*澄清*.md", "*方案*.md", "*.diff", "*.patch", "*_before_*.json")
    SKIP_SOURCE = "内置回退清单"


def main() -> int:
    if not OSS_DIR.is_dir():
        print(f"✗ 找不到开源目录 {OSS_DIR}\n  先跑：python dist/package_release.py --opensource")
        return 2

    meta = (OSS_DIR / "metadata.yaml").read_text(encoding="utf-8")
    ver = re.search(r"^version:\s*v?([\d.]+)\s*$", meta, re.M)
    ver = ver.group(1) if ver else "0.0.0"
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else (
        ROOT / "dist" / f"astrbot_plugin_warframe-{ver}-market.zip")

    n = 0
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for f in sorted(OSS_DIR.rglob("*")):
            if not f.is_file():
                continue
            rel = f.relative_to(OSS_DIR)
            if set(rel.parts) & SKIP_DIRS:
                continue
            if rel.name in SKIP_FILES or rel.suffix in SKIP_SUFFIX:
                continue
            if any(rel.match(g) for g in SKIP_GLOBS):
                continue
            z.write(f, rel.as_posix())          # ★ 不套顶层目录
            n += 1

    size_mb = out.stat().st_size / 1024 / 1024
    print(f"[flat zip] {out}  ({n} 条目, {size_mb:.2f} MB)")
    print(f"  排除清单来源：{SKIP_SOURCE}")
    if size_mb > 16:
        print("✗ 超过市场 16MB 上限")
        return 1
    with zipfile.ZipFile(out) as z:
        names = z.namelist()
        top = sorted({x.split("/")[0] for x in names if "/" in x})
        print("  顶层目录（应只有 .github）：", top or "无")
        for must in ("metadata.yaml", "main.py", "README.md", "CHANGELOG.md"):
            mark = "✓" if must in names else "✗ 缺失"
            print(f"  {mark} {must}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
