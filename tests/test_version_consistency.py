# -*- coding: utf-8 -*-
"""版本号一致性守卫（python3 tests/test_version_consistency.py）

抬版本时要同时改 metadata.yaml、main.py 的 ``@register`` 版本、卡片水印三处，
曾经因为漏改导致卡片水印停在旧号上。这里把「单一来源」钉住：

* ``core/__init__.py:__version__`` 是主版本来源；
* ``core/render.WATERMARK_VERSION`` 必须由它派生（主次版本）；
* ``metadata.yaml`` 的 ``version`` 与 ``@register`` 的版本必须一致。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import __version__ as CORE_VERSION          # noqa: E402
from core import render as R                          # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = ""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f"  -> {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


MM = ".".join(str(CORE_VERSION).split(".")[:2])       # 1.3.0 -> 1.3

check("core.__version__ 形如 X.Y.Z",
      bool(re.fullmatch(r"\d+\.\d+\.\d+", str(CORE_VERSION))), str(CORE_VERSION))
check("卡片水印版本由 core.__version__ 派生",
      R.WATERMARK_VERSION == MM, f"{R.WATERMARK_VERSION} vs {MM}")
# 水印断言用**派生值**比较（不写死品牌）：2026-09-18 换品牌时因为断言写死
# 品牌名，改名连带崩了测试 —— 教训写在这儿。
check("水印串含派生版本",
      R.WATERMARK.endswith(R.WATERMARK_VERSION), R.WATERMARK)
check("水印含品牌 SDJK", "SDJK" in R.WATERMARK, R.WATERMARK)

meta = (ROOT / "metadata.yaml").read_text(encoding="utf-8")
m = re.search(r"^version:\s*v?([\d.]+)\s*$", meta, re.M)
check("metadata.yaml 有 version 字段", bool(m))


def _mm(v: str) -> str:
    """取主次版本 —— AstrBot 插件市场规范要求三位 semver（2.0.0），
    而 core.__version__ 也是三位；旧断言写死两位（2.0）会在开源包里误报。"""
    parts = (v or "").lstrip("v").split(".")
    return ".".join(parts[:2]) if len(parts) >= 2 else (v or "")


check("metadata.yaml 版本 == core 主次版本",
      bool(m) and _mm(m.group(1)) == MM, m.group(1) if m else "?")

# ★ metadata 的 ``name`` 必须是**合法 Python 标识符**：AstrBot 4.x 用它当
#   插件模块名来 import（star_manager._validate_importable_name 会校验
#   `name.isidentifier()`）。2026-09-18 实测：服务端那份写成了显示名
#   「Warframe 查询助手」，带空格 → 从仓库/zip 安装时直接抛
#   「metadata 文件中 name 不是合法的模块名称」而失败。
#   显示名要放 ``display_name`` 字段，别占用 name。
def _meta_name(text: str) -> str:
    """取 metadata 的 name —— 用逐行解析而不是正则。

    用正则写过一版（`^name:` + 空白通配），但经 heredoc 落盘后正则里的
    反斜杠被吞成 `//s`，匹配恒为空 → 断言假失败。逐行解析没有转义问题。
    """
    for line in (text or "").splitlines():
        if line.startswith("name:"):
            return line.split(":", 1)[1].strip()
    return ""


mn = _meta_name(meta)
check("metadata.yaml 的 name 是合法模块名（可被 importlib 加载）",
      bool(mn) and mn.isidentifier(), mn or "缺 name 字段")
check("显示名放 display_name，不占用 name 字段",
      "display_name:" in meta, "缺 display_name")

# 开源包那份（由 dist/package_release.py 生成）同样要合法
oss_meta = ROOT / "dist" / "opensource" / "astrbot_plugin_warframe_sdjk" / "metadata.yaml"
if oss_meta.exists():
    om = oss_meta.read_text(encoding="utf-8")
    omn = _meta_name(om)
    check("开源包 metadata.yaml 的 name 也是合法模块名",
          bool(omn) and omn.isidentifier(), omn or "缺 name 字段")

main_src = (ROOT / "main.py").read_text(encoding="utf-8")
m2 = re.search(r'@register\(\s*"[^"]+",\s*"[^"]+",\s*\n\s*"[^"]*",\s*\n\s*"([\d.]+)"',
               main_src)
check("@register 版本与 core 主次版本一致",
      bool(m2) and m2.group(1) == MM, m2.group(1) if m2 else "?")
check("状态卡文案版本与 core 主次版本一致",
      f"Warframe SDJK {MM}" in main_src, "未找到状态卡版本文案")

print()
if FAILED:
    print(f"✗ {len(FAILED)} 项失败：" + "、".join(FAILED))
    sys.exit(1)
print("✓ 全部通过")
