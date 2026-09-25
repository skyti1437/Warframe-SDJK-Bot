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
import os
import sys
import time
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
    # 2026-09-21 阶段 3：与主清单对齐 —— 移除本机宿主平台的工作区目录条目
    # （stage 内本就不存在该目录，字面量写入公开仓属私有痕迹残留），补
    # ".zcode"/".archive"（阶段 1 起主清单新增的排除项）。
    SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", "runtime",
                 ".venv", "venv", "node_modules", ".idea", ".vscode", "dist",
                 ".audit", "kb_src", "_cache", "docs", "outputs", "output",
                 ".zcode", ".archive"}
    # 注：fonts/ 不再整体排除（2026-09-25）——随包放行子集字体
    # NotoSansCJKsc-Subset-{Regular,Bold}.otf（约 6.6MB，市场版开箱出图）；
    # 完整 ttc 由 SKIP_FILES 兜底排除。
    SKIP_FILES = {"deploy.sh", ".DS_Store", ".gitattributes", ".gitignore",
                  "SDJKwfbot_README.md", "wm_ranks.json", "riven_weekly.json",
                  "wiki_disp.json", "package_reverse_searcher_sdjk.py",
                  "NotoSansCJK-Regular.ttc", "NotoSansCJK-Bold.ttc"}
    SKIP_SUFFIX = {".pyc", ".pyo"}
    SKIP_GLOBS = ("*报告*.md", "*调研*.md", "*诊断*.md", "*对照*.md", "*核验*.md",
                  "*体检*.md", "*复评*.md", "*选型*.md", "*实测*.md", "*结案*.md",
                  "*澄清*.md", "*方案*.md", "*.diff", "*.patch", "*_before_*.json")
    SKIP_SOURCE = "内置回退清单"

# ---------------------------------------------------------------------------
# 市场件专属排除（2026-09-22 瘦身：条目 177 → 80）
# ---------------------------------------------------------------------------
# 开源**仓库**保留 tests / scripts / .github 等开发与验证物料（透明、可复现）；
# 但市场安装用户只需要运行时必需件——这些物料进安装包是死代码，白占体积还
# 扩大审读面。目标形态：core / main.py / metadata.yaml / _conf_schema.json /
# requirements.txt / README.md / CHANGELOG.md / LICENSE / kb/。
# （.gitignore / .gitattributes 已被上方 SKIP_FILES 排除，不重复列。）
MARKET_SKIP_DIRS = {"tests", "scripts", ".github"}
MARKET_SKIP_FILES = {".gitleaks.toml",        # 仓库门面（防泄漏 CI 配置），非运行件
                     # ruff 配置（2026-09-25）：开发物料，市场安装用户不需要
                     "pyproject.toml",
                     # 根目录的三个数据构建入口（scripts/ 里的同族已随目录整体排除）
                     "build_damage_data.py",
                     "build_de_data.py",
                     "build_stances.py"}
# 条目基线：177（v1.0.5 前）→ 80（v1.0.5 瘦身）→ 82（v1.0.6：+core/matching.py、
# +core/data/dispositions_rivenmirror.json，变体解析倾向数据随市场件分发）。
# 与 package_release.EXPECTED_OSS_STAGE_FILES 同理——有意变更须同步
# 此常量并在 commit 正文列文件名与理由。
EXPECTED_MARKET_ENTRIES = 97

# ★ 可复现打包（2026-09-26 用户侧建议）：统一 zip 条目时间戳 = 2026-01-01T00:00:00Z。
#   之前取文件 mtime，导致「内容没变、重建却换 sha」（上传期两次被迫冻结重建：
#   727ba7a7 → a5159a32 这类）。可用 SOURCE_DATE_EPOCH 覆盖。
ZIP_EPOCH_DEFAULT = 1767225600        # 2026-01-01T00:00:00Z


def _zip_datetime() -> tuple:
    """zip 条目统一时间戳 ``(Y,M,D,h,m,s)`` —— 可复现打包（2026-09-26）。

    背景：zip 条目时间戳默认取**文件 mtime**，所以只要文件被重写（哪怕内容一字
    未变），重建出来的 sha 就不同 —— 我们已经两次因此在上传期被迫「冻结重建」
    （`727ba7a7 → a5159a32` 这类换号）。这里把所有条目钉到**固定时刻**：
    **同内容 ⇒ 同 sha**。可被 ``SOURCE_DATE_EPOCH`` 覆盖（reproducible-builds 惯例）；
    早于 zip 格式下限（1980-01-01）的值抬到下限，避免打包报错。
    """
    raw = os.environ.get("SOURCE_DATE_EPOCH", "")
    try:
        epoch = int(raw) if raw.strip() else ZIP_EPOCH_DEFAULT
    except ValueError:
        epoch = ZIP_EPOCH_DEFAULT
    t = time.gmtime(max(epoch, 315532800))          # 315532800 = 1980-01-01T00:00:00Z
    return (t.tm_year, t.tm_mon, t.tm_mday, t.tm_hour, t.tm_min, t.tm_sec)


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
    dt = _zip_datetime()                    # ★ 固定时间戳（同内容 ⇒ 同 sha）
    with zipfile.ZipFile(out, "w") as z:
        for f in sorted(OSS_DIR.rglob("*")):
            if not f.is_file():
                continue
            rel = f.relative_to(OSS_DIR)
            if set(rel.parts) & SKIP_DIRS:
                continue
            if set(rel.parts) & MARKET_SKIP_DIRS or rel.name in MARKET_SKIP_FILES:
                continue
            if rel.name in SKIP_FILES or rel.suffix in SKIP_SUFFIX:
                continue
            if any(rel.match(g) for g in SKIP_GLOBS):
                continue
            zi = zipfile.ZipInfo(rel.as_posix(), date_time=dt)
            zi.compress_type = zipfile.ZIP_DEFLATED
            zi.external_attr = 0o100644 << 16     # 稳定权限位（不受 umask 影响）
            z.writestr(zi, f.read_bytes())        # ★ 不套顶层目录
            n += 1

    size_mb = out.stat().st_size / 1024 / 1024
    print(f"[flat zip] {out}  ({n} 条目, {size_mb:.2f} MB)")
    print(f"  排除清单来源：{SKIP_SOURCE}")
    if n != EXPECTED_MARKET_ENTRIES:
        print(f"✗ 条目数 {n} != 基线 {EXPECTED_MARKET_ENTRIES}"
              "（有意变更请同步常量并在 commit 正文列文件名与理由）")
        return 1
    if size_mb > 16:
        print("✗ 超过市场 16MB 上限")
        return 1
    with zipfile.ZipFile(out) as z:
        names = z.namelist()
        top = sorted({x.split("/")[0] for x in names if "/" in x})
        print("  顶层目录（应只有 core 与 kb）：", top or "无")
        for must in ("metadata.yaml", "main.py", "README.md", "CHANGELOG.md"):
            mark = "✓" if must in names else "✗ 缺失"
            print(f"  {mark} {must}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
