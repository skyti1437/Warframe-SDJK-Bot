# -*- coding: utf-8 -*-
"""KB 取数：warframe-items 的 i18n **两种布局**都要能读（离线，不联网）。

背景（2026-10-07 实测）：上游 `WFCD/warframe-items` 于 2026-09-24（v1.1276.17，
commit 47aa17ef85）把 `data/json/i18n.json`（46.8MB）**拆成
`data/json/i18n/<14 语言>.json`**（无 `en.json`），而 npm 包仍带老文件 ——
两条分发通道布局不一致；本仓 `scripts/kb/kb_lib.py` 原先只认老路径 ⇒
**下一次从 GitHub 下 zip 重建 KB 会直接 FileNotFoundError**。

本测试钉住三件事：
1. 老布局（`i18n.json`）照旧可读；
2. 新布局（`i18n/<lang>.json`）能**转置合并**成老形状（键=uniqueName、内层=语言）；
3. 两种都没有 ⇒ **抛错**（不许静默降级成空表 —— 那会让整批 KB 的译名悄悄丢失）。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# kb_lib 导入期就要算 IDIR/PDIR/CDIR（缺 WF_KB_DATA 会直接退出）——本测试只用
# `load_i18n(显式目录)`，所以给个临时目录让它过关即可。
os.environ.setdefault("WF_KB_DATA", tempfile.mkdtemp())
sys.path.insert(0, str(ROOT / "scripts" / "kb"))

import kb_lib  # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = ""):
    print(
        ("[PASS] " if cond else "[FAIL] ")
        + name
        + (f"  -> {detail}" if detail and not cond else "")
    )
    if not cond:
        FAILED.append(name)


U = "/Lotus/Weapons/Tenno/Rifle/Foo"
OLD = {U: {"zh": {"name": "旧形状"}, "de": {"name": "Alt"}}}

# ① 老布局
d1 = Path(tempfile.mkdtemp())
(d1 / "i18n.json").write_text(json.dumps(OLD, ensure_ascii=False), encoding="utf-8")
check(
    "老布局 i18n.json 可读且原样返回",
    kb_lib.load_i18n(str(d1)) == OLD,
    str(kb_lib.load_i18n(str(d1)))[:120],
)

# ② 新布局（语言文件，无 en.json）
d2 = Path(tempfile.mkdtemp())
(d2 / "i18n").mkdir()
(d2 / "i18n" / "zh.json").write_text(
    json.dumps({U: {"name": "新形状"}}, ensure_ascii=False), encoding="utf-8"
)
(d2 / "i18n" / "de.json").write_text(
    json.dumps({U: {"name": "Alt"}}, ensure_ascii=False), encoding="utf-8"
)
got = kb_lib.load_i18n(str(d2))
check(
    "新布局转置合并成老形状（uniqueName → 语言 → 字段）",
    got.get(U, {}).get("zh", {}).get("name") == "新形状"
    and got.get(U, {}).get("de", {}).get("name") == "Alt",
    str(got)[:160],
)
check(
    "新布局与老布局消费口径一致（kb_lib 只用 .get('zh')）",
    (got.get(U) or {}).get("zh") == {"name": "新形状"},
    str((got.get(U) or {}).get("zh")),
)

# ③ 两种都没有 ⇒ 抛错（不静默）
d3 = Path(tempfile.mkdtemp())
try:
    kb_lib.load_i18n(str(d3))
    check("两种布局都缺时抛 FileNotFoundError", False, "未抛错（静默降级）")
except FileNotFoundError as exc:
    check("两种布局都缺时抛 FileNotFoundError", "i18n" in str(exc), str(exc)[:120])
except Exception as exc:  # noqa: BLE001
    check("两种布局都缺时抛 FileNotFoundError", False, f"{type(exc).__name__}: {exc}")

print()
if FAILED:
    print(f"✗ {len(FAILED)} 项失败：{FAILED}")
    raise SystemExit(1)
print("✓ KB i18n 布局兼容全部通过")
