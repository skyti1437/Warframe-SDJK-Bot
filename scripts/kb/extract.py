# -*- coding: utf-8 -*-
"""从两个 zip 中提取本次建模需要的 JSON 到 WF_KB_DATA 下，避免反复解压大包。

目录由环境变量指定（见 kb_lib.py 头部注释）：WF_KB_DATA=解包数据目录，
WF_KB_SRC=数据包 zip 所在目录；两者缺一即报错退出。
"""
import zipfile, os, time
from kb_lib import kb_data_dir, kb_src_dir

BASE = kb_data_dir()
os.makedirs(BASE, exist_ok=True)

SRC = kb_src_dir()
Z_ITEMS = os.path.join(SRC, 'warframe-items-1.1275.85.zip')
Z_PEP   = os.path.join(SRC, 'warframe-public-export-plus-0.6.8.zip')

I_ROOT = 'warframe-items-1.1275.85/'
P_ROOT = 'warframe-public-export-plus-0.6.8/'

t0 = time.time()

# --- warframe-items: data/json/*.json ---
zi = zipfile.ZipFile(Z_ITEMS)
items_json = [n for n in zi.namelist()
              if n.startswith(I_ROOT + 'data/json/') and n.endswith('.json')]
os.makedirs(os.path.join(BASE, 'items'), exist_ok=True)
for n in items_json:
    out = os.path.join(BASE, 'items', os.path.basename(n))
    with zi.open(n) as src, open(out, 'wb') as dst:
        dst.write(src.read())
print('items json extracted: %d' % len(items_json))

# --- pep: Export*.json + dict.zh/en/tc + languages.csv ---
zp = zipfile.ZipFile(Z_PEP)
os.makedirs(os.path.join(BASE, 'pep'), exist_ok=True)
want = [n for n in zp.namelist()
        if n.startswith(P_ROOT) and (
            os.path.basename(n).startswith('Export') and n.endswith('.json')
            or os.path.basename(n) in ('dict.zh.json', 'dict.en.json', 'dict.tc.json',
                                       'languages.csv', 'index.d.ts'))]
for n in want:
    out = os.path.join(BASE, 'pep', os.path.basename(n))
    with zp.open(n) as src, open(out, 'wb') as dst:
        dst.write(src.read())
print('pep extracted: %d' % len(want))
print('elapsed %.1fs' % (time.time() - t0))
