# -*- coding: utf-8 -*-
"""Warframe SDJKBOT（AstrBot 插件）核心业务层。"""

__version__ = "1.0.4"

# ★ 品牌名**唯一来源**（2026-09-19 改名时立）。
#   以前品牌字符串散在 metadata / main.py / render.py / 测试断言里各写一份，
#   改名时要逐处比对，且有一版测试把品牌名写死导致换品牌连测试一起崩。
#   现在：代码里一律引用这两个常量，测试也从这里派生。
#   ⚠️ dist/package_release.py 的 rename_brand() 按**字面量**替换这两个值来产出
#   自用包（展示名 → SDJKwfbot、卡片短品牌 → SDJK），改常量时要同步那条规则表。
__brand__ = "Warframe SDJKBOT"      # 展示名：市场 / 面板 / 卡片标题
__brand_card__ = "SDJKBOT"          # 卡片水印与副标题用的短品牌（渲染时大写）

from .parser import parse, parse_wm, parse_wr, parse_fissure_filter  # noqa: F401
