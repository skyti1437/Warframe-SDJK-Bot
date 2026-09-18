# -*- coding: utf-8 -*-
"""版本号一致性守卫（python3 tests/test_version_consistency.py）

抬版本时要同时改 metadata.yaml、main.py 的 ``@register`` 版本、卡片水印三处，
曾经因为漏改导致卡片水印停在旧号上。这里把「单一来源」钉住：

* ``core/__init__.py:__version__`` 是主版本来源；
* ``core/render.SDJK_VERSION`` 必须由它派生（主次版本）；
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
      R.SDJK_VERSION == MM, f"{R.SDJK_VERSION} vs {MM}")
check("水印串含派生版本",
      R.WATERMARK.endswith("SDJK " + MM), R.WATERMARK)

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
