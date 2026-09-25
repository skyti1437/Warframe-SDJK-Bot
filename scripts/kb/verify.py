# -*- coding: utf-8 -*-
"""构建后抽查：条目抽样 / 术语规范化 / 遗物反查 / 分块模拟 / 译名自检 / 异常自检。

目录由环境变量指定（见 kb_lib.py 头部注释）：WF_KB_DATA 必填；
知识库目录优先 WF_KB_OUT，未设时取 WF_KB_DATA 上两级的「知识库/」。
"""
import os
import re
import sys
import random
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from kb_lib import kb_data_dir, kb_out_dir
K = kb_out_dir()
OUT = os.path.join(os.path.dirname(kb_data_dir()), 'out_verify.txt')
L = []
def w(s=''): L.append(str(s))

def rd(fn):
    return open(os.path.join(K, fn), encoding='utf-8').read()

# ---- 1. 抽查条目 ----
for fn, pat in [('01_战甲.md', '### Ash'), ('01_战甲.md', '### Ash 技能详情'),
                ('02_武器.md', '### 紫卡倾向一览'),
                ('03_MOD与赋能.md', '### 耗弱链接（Abating Link）'),
                ('03_MOD与赋能.md', '### 加速冲击（Accelerated Blast）'),
                ('04_遗物.md', '### 后纪 A1 遗物（Axi A1）'),
                ('05_敌人.md', '### 空中指挥官（Aerial Commander）'),
                ('06_资源与蓝图.md', '### 迅发电浆炮 Prime（PrimeAcceltraWeapon）'),
                ('09_成就挑战与午夜电波.md', '### 滑翔者（Glider）'),
                ('10_图鉴与生态.md', '### 常见秃鹰（Common Condroc）')]:
    t = rd(fn)
    i = t.find(pat)
    w('=' * 68); w('%s  →  %s' % (fn, pat))
    w(t[i:i+1400] if i >= 0 else '!! 未找到')

# ---- 2. MOD 术语规范化质量抽查 ----
w(''); w('=' * 68); w('MOD 效果「术语规范化」抽查')
t = rd('03_MOD与赋能.md')
ents = [e for e in re.split(r'\n(?=### )', t) if e.startswith('### ')]
random.seed(7)
for e in random.sample(ents, 14):
    w('-' * 50)
    w(e.strip()[:400])

# ---- 3. 遗物反查索引抽查 ----
w(''); w('=' * 68); w('遗物·按奖励反查（前 3 条 + 随机 3 条）')
t6 = rd('04_遗物.md')
i = t6.find('## 二、按奖励反查遗物')
seg = t6[i:]
es = [e for e in re.split(r'\n(?=### )', seg) if e.startswith('### ')]
w('反查条目数: %d' % len(es))
for e in es[:3] + random.sample(es, 3):
    w('-' * 50); w(e.strip()[:400])

# ---- 4. 分块模拟（512 / 重叠 50）----
w(''); w('=' * 68)
w('分块模拟：chunk=512, overlap=50')
tot_chunks = 0
rows = []
for fn in sorted(os.listdir(K)):
    if not fn.endswith('.md'):
        continue
    txt = rd(fn)
    step = 512 - 50
    chunks = [txt[i:i+512] for i in range(0, max(1, len(txt) - 50), step)]
    tot_chunks += len(chunks)
    # 有多少 chunk 里含有 ### 标题（便于定位实体）
    withhead = sum(1 for c in chunks if '### ' in c)
    # 平均每条实体名出现在多少 chunk
    rows.append((fn, len(txt), len(chunks), withhead))
    w('  %-22s %8d 字 → %6d 块（含条目标题的块 %5.1f%%）'
      % (fn, len(txt), len(chunks), 100.0 * withhead / max(1, len(chunks))))
w('  合计 %d 块' % tot_chunks)

# ---- 5. 译名体系合规自检 ----
w(''); w('=' * 68); w('译名体系自检（国际服官方简中）')
# 国服译名关键词：标题层必须 0；正文层可能是 DE 官方说明里的用词，单独报告。
# 「堕落者」用负向断言排除官方单位名「远古堕落者」。
BAD_RE = [('克隆尼', r'克隆尼'), ('科普斯', r'科普斯'), ('心智者', r'心智者'),
          ('纳玛', r'纳玛'), ('天诺战士', r'天诺战士'), ('感染体', r'感染体'),
          ('堕落者', r'(?<!远古)堕落者')]
for fn in sorted(os.listdir(K)):
    if not fn.endswith('.md'):
        continue
    txt = rd(fn)
    heads = '\n'.join(re.findall(r'^#{2,4} .*$', txt, re.M))
    hh = {nm: len(re.findall(rx, heads)) for nm, rx in BAD_RE if re.search(rx, heads)}
    body = {nm: len(re.findall(rx, txt)) for nm, rx in BAD_RE if re.search(rx, txt)}
    w('  %-22s 标题层 %s ｜ 正文层 %s'
      % (fn, ('⚠ %s' % hh) if hh else '✓', body or '—'))
w('  （标题层为空 = 通过；正文层命中通常是 DE 官方说明原文，如「远古堕落者」「天诺战士之间的情谊」，不作修改）')
# 必备英文派系名存在性
for fn in ['01_战甲.md', '05_敌人.md']:
    txt = rd(fn)
    w('  %-22s Grineer×%d Corpus×%d Infested×%d 奥罗金×%d'
      % (fn, txt.count('Grineer'), txt.count('Corpus'), txt.count('Infested'), txt.count('奥罗金')))

# ---- 6. 残留占位符 / 异常自检 ----
w(''); w('=' * 68); w('异常自检')
for fn in sorted(os.listdir(K)):
    if not fn.endswith('.md'):
        continue
    txt = rd(fn)
    probs = []
    if re.search(r'\|', txt): probs.append('残留竖线 | ×%d' % txt.count('|'))
    if '<DT_' in txt: probs.append('残留颜色标签')
    if re.search(r'\bNone\b', txt): probs.append('None 字面量×%d' % len(re.findall(r'\bNone\b', txt)))
    if re.search(r'[（(]\s*[）)]', txt): probs.append('空括号')
    if '  ｜' in txt or '｜ ' in txt: probs.append('分隔符空白')
    w('  %-22s %s' % (fn, probs or '✔ 无异常'))

open(OUT, 'w', encoding='utf-8').write('\n'.join(L))
print('ok')
