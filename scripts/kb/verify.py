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

# ---- 7. 官方名对拍（PEP 主表基准，标题层覆盖率） ----
# 用 DE 官方导出（Export*.json 的 name 语言键 → dict.zh）作为基准，
# 与各文档 ### 标题做「真缺」对拍。标题归一规则与误报教训（2026-09-30 实战）：
#   ① 剥尾部（English）后缀、消歧后缀「 ｜类型…」、重名序号「 #N」（须循环剥到稳定）；
#   ② dict.zh 的值常带 \r\n 尾巴——两侧都要 strip 控制字符；
#   ③ 官方名可能带 |COLOR| 富文本宏（挑战类）——两侧都过 demark。
# Resources（装饰类为主）、Enemies（任务变体/NPC 为主）、Weapons 的 Exalted 属口径外，
# 只做计数参考；核心表真缺 > 0 时人工复核是否该补（zh_overrides 或 build 补源）。
w(''); w('=' * 68); w('官方名对拍（PEP 主表 → dict.zh 官方中文名 vs 文档标题）')
try:
    from kb_lib import jload, demark
    PDIR = os.path.join(kb_data_dir(), 'pep')
    dz = jload(os.path.join(PDIR, 'dict.zh.json'))
    def _norm(s):
        s = demark(str(s or '')).replace('\r', ' ').replace('\n', ' ').strip()
        prev = None
        while prev != s:
            prev = s
            s = re.sub(r'（[^）]*）\s*$', '', s).strip()
            s = re.sub(r'\s*｜.*$', '', s).strip()
            s = re.sub(r'\s*#\d+$', '', s).strip()
        return s
    def _offical(fname, sub=None):
        d = jload(os.path.join(PDIR, fname))
        if sub:
            d = d[sub]
        out = set()
        for _un, e in d.items():
            if isinstance(e, dict):
                nm = _norm(dz.get(e.get('name')))
                if nm:
                    out.add(nm)
        return out
    _docs = {}
    for fn in sorted(os.listdir(K)):
        if fn.endswith('.md'):
            _docs[fn] = {_norm(t) for t in re.findall(r'^### (.+)$', rd(fn), re.M)}
    _all = set().union(*_docs.values()) if _docs else set()
    AUDIT = [('ExportWarframes.json', None, ['01_战甲.md'], 'core'),
             ('ExportWeapons.json', None, ['02_武器.md', '06_资源与蓝图.md', '07_同伴与空战.md'], 'ref'),
             ('ExportUpgrades.json', None, ['03_MOD与赋能.md'], 'core'),
             ('ExportArcanes.json', None, ['03_MOD与赋能.md'], 'core'),
             ('ExportSentinels.json', None, ['07_同伴与空战.md'], 'core'),
             ('ExportAchievements.json', None, ['09_成就挑战与午夜电波.md'], 'core'),
             ('ExportChallenges.json', None, ['09_成就挑战与午夜电波.md'], 'core'),
             ('ExportEnemies.json', 'avatars', ['05_敌人.md'], 'ref'),
             ('ExportResources.json', None, ['06_资源与蓝图.md'], 'ref')]
    for fname, sub, files, kind in AUDIT:
        off = _offical(fname, sub)
        doc = set()
        for f in files:
            doc |= _docs.get(f, set())
        miss = off - doc - _all
        tag = '核心' if kind == 'core' else '参考（口径外条目不计缺陷）'
        w('  %-24s 官方 %4d｜真缺 %4d｜%s' % (fname.replace('Export', '').replace('.json', ''),
                                            len(off), len(miss), tag))
        if kind == 'core' and miss:
            for nm in sorted(miss)[:10]:
                w('      ⚠ 真缺: %s' % nm)
            if len(miss) > 10:
                w('      … 另 %d 条' % (len(miss) - 10))
except Exception as ex:  # PEP 数据不在时不阻断其余自检
    w('  !! 对拍跳过：%s' % ex)

open(OUT, 'w', encoding='utf-8').write('\n'.join(L))
print('ok')
