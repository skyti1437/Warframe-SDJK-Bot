# -*- coding: utf-8 -*-
"""从 worldstate-data 与 drop-data 中提取本次补充所需的文件。

目录由环境变量指定（见 kb_lib.py 头部注释）：WF_KB_DATA=解包数据目录，
WF_KB_SRC=数据包 zip 所在目录；两者缺一即报错退出。
"""
import zipfile, os
from kb_lib import kb_data_dir, kb_src_dir

BASE = kb_data_dir()
SRC = kb_src_dir()
WSD = os.path.join(SRC, 'warframe-worldstate-data-3.16.9.zip')
DROP = os.path.join(SRC, 'warframe-drop-data-main.zip')
R1 = 'warframe-worldstate-data-3.16.9/data/'
R2 = 'warframe-drop-data-main/data/'

z1 = zipfile.ZipFile(WSD)
d1 = os.path.join(BASE, 'wsd')
os.makedirs(d1, exist_ok=True)
want1 = ['archonShards', 'steelPath', 'sortieData', 'synthTargets', 'fissureModifiers',
         'missionTypes', 'syndicatesData', 'factionsData', 'solNodes', 'arcanes']
n1 = 0
for nm in want1:
    for lg in ('', 'zh/'):
        src = R1 + lg + nm + '.json'
        try:
            z1.getinfo(src)
        except KeyError:
            continue
        with z1.open(src) as s, open(os.path.join(d1, (lg.replace('/', '_') or 'en_') + nm + '.json'), 'wb') as d:
            d.write(s.read())
        n1 += 1
print('wsd extracted: %d' % n1)

z2 = zipfile.ZipFile(DROP)
d2 = os.path.join(BASE, 'drop')
os.makedirs(d2, exist_ok=True)
for src, out in ((R2 + 'all.json', 'all.json'),
                 (R2 + 'missionRewards.json', 'missionRewards.json'),
                 (R2 + 'syndicates.json', 'syndicates.json')):
    try:
        z2.getinfo(src)
    except KeyError:
        continue
    with z2.open(src) as s, open(os.path.join(d2, out), 'wb') as d:
        d.write(s.read())
print('drop extracted ok')
