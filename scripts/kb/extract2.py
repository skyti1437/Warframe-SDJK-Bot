# -*- coding: utf-8 -*-
"""提取 warframe-items 的 config/*.json（damageTypes/polarities/relicGrades 等枚举表）。

目录由环境变量指定（见 kb_lib.py 头部注释）：WF_KB_DATA=解包数据目录，
WF_KB_SRC=数据包 zip 所在目录；两者缺一即报错退出。
"""
import zipfile, os
from kb_lib import kb_data_dir, kb_src_dir

BASE = kb_data_dir()
zi = zipfile.ZipFile(os.path.join(kb_src_dir(), 'warframe-items-1.1275.85.zip'))
R = 'warframe-items-1.1275.85/'
os.makedirs(os.path.join(BASE, 'cfg'), exist_ok=True)
for n in zi.namelist():
    if n.startswith(R + 'config/') and n.endswith('.json'):
        out = os.path.join(BASE, 'cfg', os.path.basename(n))
        with zi.open(n) as s, open(out, 'wb') as d:
            d.write(s.read())
print('cfg:', os.listdir(os.path.join(BASE, 'cfg')))
