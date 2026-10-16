# -*- coding: utf-8 -*-
"""`scripts/kb/extract.py` 解包布局回归：`data/json/` 下的子目录必须**保留**。

2026-10-16 实测的管道缺口（本用例冻死它）：
上游 2026-09-24 把 `data/json/i18n.json` 拆成 `data/json/i18n/<14 语言>.json`
（`kb_lib.load_i18n` 已在 2026-10-07 加双布局兼容），但 `extract.py` 仍一律 `basename()` 拍平
⇒ 新布局下两处同时错：
  ① `load_i18n` 的新布局判据是 `<items>/i18n/` **目录** ⇒ 找不到 ⇒ 抛 FileNotFoundError
     （重建时先撞的就是这个）；
  ② `Sources.__init__` 遍历 IDIR 时会把 `zh.json` / `de.json` 当成**物品表**载入（污染 items）。

断言：解包后 `items/Mods.json`、`items/i18n/zh.json` 都在，且 `items/` 顶层**没有** `zh.json`。
（顶层仍平铺 —— 这一点也在用例里钉住，避免有人为了修子目录把顶层也改成带路径。）

不联网：假包在临时目录里用 zipfile 现造。
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXTRACT = ROOT / "scripts" / "kb" / "extract.py"

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(
        f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -> {detail}" if detail and not cond else "")
    )
    if not cond:
        FAILED.append(name)


tmp = Path(tempfile.mkdtemp(prefix="kb_extract_"))
src = tmp / "src"
data = tmp / "data"
src.mkdir()
data.mkdir()

# --- 假包①：warframe-items（顶层 Mods.json + 子目录 i18n/zh.json）---
with zipfile.ZipFile(src / "warframe-items-master.zip", "w") as z:
    z.writestr("warframe-items-master/data/json/Mods.json", '{"Mods": []}')
    z.writestr("warframe-items-master/data/json/Enemy.json", '{"Enemy": []}')
    z.writestr("warframe-items-master/data/json/i18n/zh.json", '{"zh": {}}')
    z.writestr("warframe-items-master/data/json/i18n/de.json", '{"de": {}}')
    z.writestr("warframe-items-master/config/damageTypes.json", "{}")
    # 干扰项：真包里 data/json 之外的 json 不该被抽
    z.writestr("warframe-items-master/README.json", "{}")

# --- 假包②：public-export-plus（extract.py 也要求它存在）---
with zipfile.ZipFile(src / "warframe-public-export-plus-master.zip", "w") as z:
    z.writestr("warframe-public-export-plus-master/ExportTest.json", "{}")
    z.writestr("warframe-public-export-plus-master/dict.zh.json", "{}")

env = dict(os.environ, WF_KB_DATA=str(data), WF_KB_SRC=str(src))
r = subprocess.run(
    [sys.executable, str(EXTRACT)],
    capture_output=True,
    text=True,
    encoding="utf-8",
    errors="ignore",
    env=env,
    cwd=str(ROOT / "scripts" / "kb"),
    timeout=120,
)
print(f"  extract.py rc={r.returncode}")
if r.returncode != 0:
    print((r.stdout or "")[-600:])
    print((r.stderr or "")[-600:])

items = data / "items"
check("extract.py 正常退出", r.returncode == 0)
check("顶层物品表平铺：items/Mods.json 存在", (items / "Mods.json").is_file())
check("顶层物品表平铺：items/Enemy.json 存在", (items / "Enemy.json").is_file())
check("★ 子目录保留：items/i18n/zh.json 存在", (items / "i18n" / "zh.json").is_file())
check("★ 子目录保留：items/i18n/de.json 存在", (items / "i18n" / "de.json").is_file())
check(
    "★ 拍平回归：items/ 顶层**没有** zh.json（会被当成物品表载入）",
    not (items / "zh.json").exists(),
    f"实际存在：{(items / 'zh.json')}",
)
check(
    "★ 拍平回归：items/ 顶层没有 de.json",
    not (items / "de.json").exists(),
)
check("data/json 之外的 json 未被抽", not (items / "README.json").exists())
check("config/ 未被 extract.py 抽（归 extract2.py）", not (items / "damageTypes.json").exists())
check("pep 正常解出：pep/ExportTest.json", (data / "pep" / "ExportTest.json").is_file())
check("pep dict.zh.json 解出", (data / "pep" / "dict.zh.json").is_file())

# --- 与读取侧对齐：Sources 只把顶层 *.json 当物品表 ---
sys.path.insert(0, str(ROOT / "scripts" / "kb"))
loaded = sorted(p.name for p in items.glob("*.json"))
check(
    "读取侧口径：顶层 *.json 恰为两张物品表",
    loaded == ["Enemy.json", "Mods.json"],
    f"实际 {loaded}",
)

print()
if FAILED:
    print(f"FAILED {len(FAILED)}: {FAILED}")
    sys.exit(1)
print("ALL PASS")
