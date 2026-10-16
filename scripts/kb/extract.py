# -*- coding: utf-8 -*-
"""从两个 zip 中提取本次建模需要的 JSON 到 WF_KB_DATA 下，避免反复解压大包。

目录由环境变量指定（见 kb_lib.py 头部注释）：WF_KB_DATA=解包数据目录，
WF_KB_SRC=数据包 zip 所在目录；两者缺一即报错退出。
"""

import zipfile
import os
import time
from kb_lib import kb_data_dir, kb_src_dir, find_zip, zip_root

BASE = kb_data_dir()
os.makedirs(BASE, exist_ok=True)

SRC = kb_src_dir()
Z_ITEMS = find_zip("warframe-items")
Z_PEP = find_zip("warframe-public-export-plus")

I_ROOT = zip_root(Z_ITEMS)
P_ROOT = zip_root(Z_PEP)
print("数据包:", os.path.basename(Z_ITEMS), "/", os.path.basename(Z_PEP))

t0 = time.time()

# --- warframe-items: data/json/*.json ---
# ★ 2026-10-16：**保留 data/json/ 下的子目录**（不再一律 basename 拍平）。
#   上游 2026-09-24 起把 i18n.json 拆成 data/json/i18n/<14 语言>.json，
#   拍平后落成 items/<lang>.json ⇒ 两处都错：
#     ① kb_lib.load_i18n 的新布局判据是 items/i18n/ **目录** ⇒ 找不到，抛 FileNotFoundError；
#     ② Sources.__init__ 遍历 IDIR 时会把 zh.json/de.json 当成**物品表**载入（污染 items）。
#   顶层文件（Mods.json 等）仍然平铺在 items/ 下，行为不变。
zi = zipfile.ZipFile(Z_ITEMS)
REL_ROOT = I_ROOT + "data/json/"
items_json = [n for n in zi.namelist() if n.startswith(REL_ROOT) and n.endswith(".json")]
os.makedirs(os.path.join(BASE, "items"), exist_ok=True)
for n in items_json:
    out = os.path.join(BASE, "items", n[len(REL_ROOT) :])
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with zi.open(n) as src, open(out, "wb") as dst:
        dst.write(src.read())
print("items json extracted: %d" % len(items_json))

# --- pep: Export*.json + dict.zh/en/tc + languages.csv ---
zp = zipfile.ZipFile(Z_PEP)
os.makedirs(os.path.join(BASE, "pep"), exist_ok=True)
want = [
    n
    for n in zp.namelist()
    if n.startswith(P_ROOT)
    and (
        os.path.basename(n).startswith("Export")
        and n.endswith(".json")
        or os.path.basename(n)
        in ("dict.zh.json", "dict.en.json", "dict.tc.json", "languages.csv", "index.d.ts")
    )
]
for n in want:
    out = os.path.join(BASE, "pep", os.path.basename(n))
    with zp.open(n) as src, open(out, "wb") as dst:
        dst.write(src.read())
print("pep extracted: %d" % len(want))
print("elapsed %.1fs" % (time.time() - t0))
