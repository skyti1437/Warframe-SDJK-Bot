# -*- coding: utf-8 -*-
"""第三轮终检：产物统计 / 繁体与富文本宏残留 / 关键小节 / 标题重复 / 抽样。

繁体校验采用「双表」：
  · 硬表 = kb_lib.T2S（构建时实际使用的繁→简字表）
  · 派生表 = 由 PEP dict.tc / dict.zh 逐字符比对派生（补 T2S 未收录的字）
"""
import re
import os
import json
import glob
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from kb_lib import kb_data_dir, kb_out_dir
import kb_lib as KB

K = kb_out_dir()
DATA = kb_data_dir()
OUT = os.path.join(os.path.dirname(DATA), 'out_final2.txt')
L = []
def w(s=''):
    L.append(str(s))

def rd(p):
    return open(p, encoding='utf-8').read()

FILES = sorted(glob.glob(K + os.sep + '*.md'))

# ---------------------------------------------------------------- 1) 产物
w('== 产物 ==')
tot = 0
for p in FILES:
    t = rd(p)
    sz = os.path.getsize(p)
    tot += sz
    w('  %-26s %8.1f KB  %6d 字  %4d 条目'
      % (os.path.basename(p), sz / 1024, len(t), len(re.findall(r'^### (.+)$', t, re.M))))
w('  合计 %.2f MB / %d 个文件' % (tot / 1048576, len(FILES)))

# ---------------------------------------------------------------- 2) 繁体残留
# 硬表 = kb_lib.T2S 里「繁≠简」的字（恒等映射不算繁体）
T2S_CHARS = {chr(k) for k in KB.T2S if KB.T2S[k] != chr(k)}
dz = json.load(open(os.path.join(DATA, 'pep', 'dict.zh.json'), encoding='utf-8'))
dt = json.load(open(os.path.join(DATA, 'pep', 'dict.tc.json'), encoding='utf-8'))


def cjk_set(d):
    """取字典所有字符串值里出现过的汉字集合。"""
    s = set()
    for v in d.values():
        vals = v if isinstance(v, list) else [v]
        for x in vals:
            if isinstance(x, str):
                s |= {c for c in x if '\u4e00' <= c <= '\u9fff'}
    return s


# 「简体基准字集」= DE 官方简中 dict.zh ∪ warframe-items 的 i18n.zh（两者均为 zh-CN）
GOOD = cjk_set(dz)
i18n = json.load(open(os.path.join(DATA, 'items', 'i18n.json'), encoding='utf-8'))
for _v in i18n.values():
    _z = (_v or {}).get('zh') or {}
    for _s in _z.values():
        if isinstance(_s, str):
            GOOD |= {c for c in _s if '\u4e00' <= c <= '\u9fff'}
# 繁体字 = 出现在 dict.tc 但简体基准字集从不使用的汉字
# ⚠ 简繁同形字白名单：这些字简体/繁体写法相同，只是恰好不在游戏简中串表里，
#   会被上式误判（已确认无误的假阳性）。诚实登记，避免误报。
SAME_SHAPE_OK = {'慌'}   # 恐慌（panic）；同形，非繁体
TRAD_ONLY = cjk_set(dt) - GOOD - SAME_SHAPE_OK
CHECK = T2S_CHARS | TRAD_ONLY

w('')
w('== 繁体残留 ==')
w('  校验字表：T2S 非恒等 %d 字 + 「仅在繁体表出现」%d 字 = %d 字'
  % (len(T2S_CHARS), len(TRAD_ONLY), len(CHECK)))
hard_hit = False
for p in FILES:
    t = rd(p)
    hard = sorted({c for c in t if c in CHECK})
    if hard:
        hard_hit = True
    w('  %-26s %s' % (os.path.basename(p), ''.join(hard) or '无 ✓'))
    for c in hard[:4]:
        m = re.search(re.escape(c), t)
        w('     %s …%s…' % (c, t[max(0, m.start() - 34):m.start() + 34].replace('\n', ' ')))
w('  → %s' % ('✗ 存在繁体残留' if hard_hit else '✓ 无繁体残留'))

# ---------------------------------------------------------------- 2b) 富文本宏残留
w('')
w('== 富文本宏残留（|COLOR| / |TITLE_START| / || 等）==')
macro_hit = False
for p in FILES:
    t = rd(p)
    hits = re.findall(r'\|[^|\n]{0,24}\|', t)
    if hits:
        macro_hit = True
        w('  %-26s ✗ %d 处：%s' % (os.path.basename(p), len(hits), sorted(set(hits))[:6]))
w('  → %s' % ('✗ 存在宏残留' if macro_hit else '✓ 无宏残留'))

# ---------------------------------------------------------------- 3) 关键小节
t08 = rd(os.path.join(K, '08_其他与机制术语.md'))
w('')
w('== 08 号关键小节 ==')
for key in ('### 伤害类型（Damage Types', '### 敌人防护层 × 元素修正',
            '### 伤害构成与元素组合', '### 状态效果（异常状态触发）全表', '### 暴击机制',
            '### 护甲减伤与减伤乘算', '### 三条血条与有效生命（EHP）', '### 敌人等级缩放（Enemy Level Scaling）',
            '### 执刑官源力石', '### 钢铁之路', '### 突击（Sortie）', '#### 修正词',
            '#### 突击首领', '### 合成目标', '### 虚空裂隙分级', '### 集团声望商店',
            '## 三、星图节点与任务掉落表', '## 四、剧情任务', '## 五、外观装饰与杂项'):
    w('  %-34s %s' % (key, '✓' if key in t08 else '✗ 缺失'))

w('')
w('== 09 / 10 / 02 号关键小节 ==')
for nm, fn, keys in (
        ('01', '01_战甲.md',
         ('## 战甲基础属性排行', '### 战甲基础属性排行 · 护甲（降序）',
          '### 战甲基础属性排行 · 生命（降序）', '### 战甲基础属性排行 · 护盾（降序）',
          '### 战甲基础属性排行 · 能量（降序）', '### 战甲基础属性排行 · 冲刺速度（降序）',
          '### 战甲基础属性排行 · 有效生命 EHP（降序，生命计入护甲）')),
        ('09', '09_成就挑战与午夜电波.md',
         ('## 一、成就（Achievements）', '## 二、挑战（Challenges）',
          '## 三、午夜电波（Nightwave）', '### 午夜电波挑战', '### 午夜电波奖励与贡品')),
        ('10', '10_图鉴与生态.md',
         ('## 一、图鉴与背景故事', '### 造物图鉴', '### 资料片段', '### 音乐片段',
          '### 战甲霸王资料', '## 二、赏金任务', '## 三、保育动物',
          '## 四、商人库存', '## 五、界面风味文本',
          '## 六、Kuva 巫妖与帕尔沃斯姐妹', '### 赤毒武器清单', '### 信条武器清单',
          '### 安魂 MOD', '### 玄骸印记与关键资源')),
        ('02', '02_武器.md',
         ('## 一、主武器（Primary）', '## 二、副武器（Secondary）',
          '## 三、近战武器（Melee）', '## 四、紫卡倾向一览', '### 紫卡倾向一览')),
        ('03', '03_MOD与赋能.md',
         ('## 一、MOD', '## 二、赋能（Arcanes）', '## 三、MOD 套装（Mod Sets）',
          '### 套装 · 私法', '### 套装 · 角斗士'))):
    t = rd(os.path.join(K, fn))
    w('  -- %s 号' % nm)
    for key in keys:
        w('     %-30s %s' % (key, '✓' if key in t else '✗ 缺失'))

# ---------------------------------------------------------------- 4) 标题重复
w('')
w('== 标题重复 ==')
for p in FILES:
    es = re.findall(r'^### (.+)$', rd(p), re.M)
    w('  %-26s 重复 %d' % (os.path.basename(p), len(es) - len(set(es))))

# ---------------------------------------------------------------- 5) 抽样
t08 = rd(os.path.join(K, '08_其他与机制术语.md'))
i = t08.find('- **远古干扰者**')
w('')
w('== 合成目标样例 ==')
w(t08[i:i + 600] if i >= 0 else '未找到')
j = t08.find('### 集团声望商店')
w('')
w('== 集团声望商店样例 ==')
w(t08[j:j + 900] if j >= 0 else '未找到')

t10 = rd(os.path.join(K, '10_图鉴与生态.md'))
k = t10.find('### 商人库存 ·')
w('')
w('== 商人库存样例 ==')
w(t10[k:k + 800] if k >= 0 else '未找到')

m = re.search(r'^### Exta.*$', t08, re.M)
w('')
w('== Exta 条目 ==')
w(t08[m.start():m.start() + 500] if m else '未找到')

open(OUT, 'w', encoding='utf-8').write('\n'.join(L))
open(os.path.join(os.path.dirname(DATA), 'final_status.txt'), 'w',
     encoding='utf-8').write('OK')
print('ok')
