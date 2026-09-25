# -*- coding: utf-8 -*-
"""
构建 Warframe 知识库（AstrBot 知识库专用，10 个 Markdown 文件）
分块参数 512 字 / 重叠 50 → 条目按「自包含、开头即实体名」设计
"""
import json
import os
import re
import sys
import time
from collections import Counter, OrderedDict, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from kb_lib import (Sources, pct_frac, pct_raw, trim_num, clean, fmt_damage, zh_stat,
                    residue_words, to_text, norm_code, t2s, demark,
                    DT_ZH, POLARITY_ZH, RARITY_ZH, FACTION_ZH, ENEMY_TYPE_ZH, RELIC_ERA_ZH,
                    REFINE_ZH, MODTYPE_ZH, kb_data_dir, kb_out_dir)

# 产物目录：优先 WF_KB_OUT；未设时取 WF_KB_DATA 上两级的「知识库/」
OUTDIR = kb_out_dir()
WORKDIR = os.path.dirname(kb_data_dir())   # 报表/状态文件落这里，避免散进仓库
os.makedirs(OUTDIR, exist_ok=True)
T0 = time.time()
S = Sources()
I, P = S.items, S.pep

COMP_ZH = {'Blueprint': '蓝图', 'Chassis': '机体', 'Neuroptics': '头部神经光元',
           'Systems': '系统', 'Wings': '机翼', 'Harness': '约束装置'}
SLOT_ZH = {'LongGuns': '步枪', 'Pistols': '手枪', 'Melee': '近战', 'SpaceGuns': '空战枪械',
           'SpaceMelee': '空战近战', 'SpaceSuits': 'Archwing', 'Suits': '战甲',
           'Sentinels': '守护', 'SentinelWeapons': '守护武器', 'KubrowPets': '库狛/库娃',
           'OperatorAmps': '指挥官增幅器', 'MoaPets': '恐鸟', 'Hoverboard': 'K 式悬浮板',
           'SpecialItems': '特殊物品', 'CrewShipWeapons': '九重天武器'}
TRIGGER_ZH = {'Auto': '全自动', 'Semi': '半自动', 'Burst': '点射', 'Charge': '蓄力',
              'Held': '持续', 'Duplex': '双发', 'Active': '主动', 'Melee': '近战'}
NOISE_ZH = {'Alarming': '警报', 'Silent': '静音'}
NAV = ('> **知识库导航（共 10 个文件）**：01 战甲｜02 武器（主武器/副武器/近战）｜'
       '03 MOD 与赋能｜04 遗物｜05 敌人｜06 资源与蓝图｜07 同伴与空战｜'
       '08 其他与机制术语｜09 成就 · 挑战 · 午夜电波｜10 图鉴 · 赏金 · 保育 · 风味。\n'
       '> 译名体系：**DE 官方简中（国际服）**；Grineer / Corpus / Infested / Sentient / '
       'Corrupted 官方保留英文。')


N_NODES = 0      # 星图节点条目数（build_others 填充，供 10 号文件头引用）
N_SYNTH = 0      # 合成目标条目数

# 突击修正词：WFCD 的原名里混了繁体/全角空格，这里统一为简中并规范格式
SORTIE_MOD_ZH = {
    'SORTIE_MODIFIER_LOW_ENERGY': '能量减少',
    'SORTIE_MODIFIER_IMPACT': '敌人物理强化（冲击）',
    'SORTIE_MODIFIER_SLASH': '敌人物理强化（切割）',
    'SORTIE_MODIFIER_PUNCTURE': '敌人物理强化（穿刺）',
    'SORTIE_MODIFIER_EXIMUS': '卓越者大本营',
    'SORTIE_MODIFIER_MAGNETIC': '敌人元素强化（磁力）',
    'SORTIE_MODIFIER_CORROSIVE': '敌人元素强化（腐蚀）',
    'SORTIE_MODIFIER_VIRAL': '敌人元素强化（病毒）',
    'SORTIE_MODIFIER_ELECTRICITY': '敌人元素强化（电击）',
    'SORTIE_MODIFIER_RADIATION': '敌人元素强化（辐射）',
    'SORTIE_MODIFIER_GAS': '敌人元素强化（毒气）',
    'SORTIE_MODIFIER_FIRE': '敌人元素强化（火焰）',
    'SORTIE_MODIFIER_EXPLOSION': '敌人元素强化（爆炸）',
    'SORTIE_MODIFIER_FREEZE': '敌人元素强化（冰冻）',
    'SORTIE_MODIFIER_TOXIN': '敌人元素强化（毒素）',
    'SORTIE_MODIFIER_POISON': '敌人元素强化（毒素）',
    'SORTIE_MODIFIER_HAZARD_RADIATION': '辐射灾害',
    'SORTIE_MODIFIER_HAZARD_MAGNETIC': '电磁异常',
    'SORTIE_MODIFIER_HAZARD_FOG': '浓雾',
    'SORTIE_MODIFIER_HAZARD_FIRE': '环境危害：火灾',
    'SORTIE_MODIFIER_HAZARD_ICE': '低温外泄',
    'SORTIE_MODIFIER_HAZARD_COLD': '极度寒冷',
    'SORTIE_MODIFIER_ARMOR': '敌人护甲强化',
    'SORTIE_MODIFIER_SHIELDS': '敌人护盾强化',
    'SORTIE_MODIFIER_SECONDARY_ONLY': '武器限定：次要武器',
    'SORTIE_MODIFIER_SHOTGUN_ONLY': '武器限定：霰弹枪',
    'SORTIE_MODIFIER_SNIPER_ONLY': '武器限定：狙击枪',
    'SORTIE_MODIFIER_RIFLE_ONLY': '武器限定：突击步枪',
    'SORTIE_MODIFIER_MELEE_ONLY': '武器限定：近战武器',
    'SORTIE_MODIFIER_BOW_ONLY': '武器限定：弓类武器',
}


# WSD 的 zh 数据把部分派系译成了中文（繁体或国服口径）；按官方简中口径还原。
# 官方：Grineer / Corpus / Infested / Sentient / Corrupted 保留英文。
FACTION_ZH_FIX = {'墮落者': 'Corrupted', '堕落者': 'Corrupted', '感染体': 'Infested',
                  '天诺战士': 'Tenno', '低语者': 'Murmur'}


# 伤害类型分组：分组顺序取自 DE 导出 cfg/damageTypes.json 的数组顺序（可反推）
DT_GROUP = [('物理伤害', ['impact', 'puncture', 'slash']),
            ('基础元素', ['heat', 'cold', 'electricity', 'toxin']),
            ('复合元素', ['blast', 'radiation', 'gas', 'magnetic', 'viral', 'corrosive']),
            ('特殊 / 事件伤害', ['void', 'tau', 'cinematic', 'shielddrain',
                             'healthdrain', 'energydrain', 'true'])]

# 敌人防护层中英对照（防护层名为 DE 内部资产名，官方简中无对应串）
LAYER_ZH = {'Ferrite Armor': '铁氧体护甲', 'Alloy Armor': '合金护甲',
            'Cloned Flesh': '克隆体血肉', 'Flesh': '肉体', 'Shield': '护盾',
            'Proto Shield': '原型护盾', 'Robotic': '机械', 'Machinery': '机械装置',
            'Fossilized': '化石', 'Infested': '感染体', 'Infested Flesh': '感染体血肉',
            'Infested Sinew': '感染体肌腱'}

# Kuva 玄骸：赤毒武器关键词 → 专属资源/术语（反推自 PEP ExportResources / Kingpins）
KUVA_TERMS = ['玄骸印记', '帕尔沃斯的姐妹印记', '科技细胞终幕者印记', '赤毒',
              '安魂通牒', '元素恶癖', '候选者']


def protection_matrix():
    """防护层 × 元素修正：反推自 warframe-items Enemy.json 的 resistances（DE 伤害控制器）。"""
    mat = defaultdict(lambda: defaultdict(Counter))
    for r in I['Enemy']:
        for layer in (r.get('resistances') or []):
            lt = layer.get('type')
            if not lt or not layer.get('amount'):
                continue
            for af in (layer.get('affectors') or []):
                el, mod = af.get('element'), af.get('modifier')
                if not el or el == 'None' or mod is None or mod == 0:
                    continue
                mat[lt][el.lower()][mod] += 1
    rows = []
    for lt in sorted(mat, key=lambda x: -sum(sum(c.values()) for c in mat[x].values())):
        weak, res = [], []
        for el in sorted(mat[lt]):
            mod, _ = mat[lt][el].most_common(1)[0]
            (weak if mod > 0 else res).append((mod, DT_ZH.get(el, el)))
        weak.sort(key=lambda x: -x[0])
        res.sort(key=lambda x: x[0])
        seg = []
        if weak:
            seg.append('弱点（增伤）' + '、'.join('%s %+g' % (z, m) for m, z in weak))
        if res:
            seg.append('抗性（减伤）' + '、'.join('%s %+g' % (z, m) for m, z in res))
        lab = LAYER_ZH.get(lt)
        rows.append('- **%s%s**：%s' % (lt, ('（%s）' % lab) if lab else '', '；'.join(seg)))
    return rows


# 伤害与状态效果机制（人工整理自官方社区 Wiki：Damage / Status Effect / Critical Hit /
# Damage Reduction / Hit Points，Update 36 口径）。仅取公式与数值、自行重述，非原文照录。
MECH_BLOCK = [
    '', '### 伤害构成与元素组合', '',
    '- 武器总伤害 = **物理**（冲击 / 穿刺 / 切割）+ **元素**伤害；各伤害类型**独立结算**，'
    'HUD 只显示合并后的数字。',
    '- 通用伤害 MOD（如 膛线 Serration）加成**全部基础伤害**；派系伤害 MOD（如 Expel）'
    '作为对该派系的**最终乘数**。',
    '- **4 种基础元素按 火焰 > 冰冻 > 电击 > 毒素 的优先级两两合成**复合元素：',
    '  - 火焰 + 冰冻 = 爆炸（Blast）｜电击 + 毒素 = 腐蚀（Corrosive）｜火焰 + 毒素 = 毒气（Gas）',
    '  - 冰冻 + 电击 = 磁力（Magnetic）｜火焰 + 电击 = 辐射（Radiation）｜冰冻 + 毒素 = 病毒（Viral）',
    '  - 合成为复合元素后，原基础元素**不再单独触发**状态。',
    '- **派系伤害修正**（自 Update 36.0 / 2024-06-18 起）：命中派系弱点 = **×1.5**，'
    '命中抗性 = **×0.5**。主要派系弱点（Wiki 口径）：'
    'Grineer（冲击 / 腐蚀）｜Corpus（穿刺 / 磁力）｜Infested（切割 / 火焰）｜'
    'Orokin（穿刺 / 病毒，抗 辐射）｜Sentient（冰冻 / 辐射，抗 腐蚀）｜'
    'Narmer（切割 / 毒素，抗 磁力）｜低语者 Murmur（电击 / 辐射，抗 病毒）。',

    '', '### 状态效果（异常状态触发）全表', '',
    '触发后效果如下；持续伤害（DoT）默认 **6 秒、每秒 1 跳**。'
    '注意：**是否触发状态与命中伤害独立结算**——每次命中始终造成所装元素伤害。',
    '- **冲击**：敌人踉跄 1 秒；Parazon 处决阈值 +8%/层（上限 5 层）',
    '- **穿刺**：目标造成伤害 −40%（10 秒）；5 层，每层再 −10% → **满层 −80%**；'
    '同时目标获 +5% 武器暴击率/层（最高 +25%）',
    '- **切割**：基础伤害 **35%/秒** 流血 6 秒，**无视护甲**',
    '- **火焰**：基础 **50%/秒** 燃烧 6 秒、恐慌 4 秒，并**最多剥离 50% 护甲**',
    '- **冰冻**：减速 50%、持续 6 秒；10 层，每层再 +5% 减速；初始暴击倍率 +0.1、每层 +0.05；'
    '第 10 层冻结 3 秒、暴击倍率 +1.0',
    '- **电击**：基础 **50%/秒** 电击 6 秒（3 米内连锁），眩晕 3 秒',
    '- **毒素**：基础 **50%/秒** 中毒 6 秒，**无视护盾**',
    '- **爆炸**：1.5 秒后造成基础 30% 伤害；满 10 层或目标死亡时提前引爆，5 米内每层 300% 基础伤害',
    '- **腐蚀**：护甲 −26%、持续 8 秒；10 层，每层再 −6% → **满层 −80%**',
    '- **毒气**：基础 **50%/秒** 毒气云 6 秒、半径 3 米；10 层，每层 +0.3 米半径',
    '- **磁力**：对护盾 / Overguard 伤害 **+100%**、6 秒；10 层，每层 +25% → **满层 +325%**；'
    '护盾被移除时引发 3%/层 的电击异常',
    '- **辐射**：敌人互相攻击、对友方伤害 **+100%**、12 秒；10 层，每层 +50% → **满层 +550%**',
    '- **病毒**：对生命伤害 **+100%**、6 秒；10 层，每层 +25% → **满层 +325%**',
    '- **虚空**：2.5 米力场吸引抛射物，3 秒',
    '- **Tau**：状态几率 +10%/层，8 秒，最高 10 层',
    '- **真实**：无状态效果',

    '', '### 暴击机制', '',
    '- 总暴击率 = 基础暴击率 ×（1 + 相对加成之和）+ 绝对（固定）加成之和',
    '- 总暴击伤害倍率 = 基础暴伤倍率 ×（1 + 相对加成之和）+ 绝对加成之和',
    '- 近战 Blood Rush / 角斗士套装的额外项：'
    '基础 ×（1 + 相对 + Blood Rush 系数 ×（连击倍率 − 1））+ 绝对',
    '- **暴击层级**：暴击率可超 100% —— 超过 100% 有几率出**橙暴（2 级，附加伤害翻倍）**，'
    '超过 200% 有几率出**红暴（3 级，附加伤害三倍）**。',

    '', '### 护甲减伤与减伤乘算', '',
    '- 护甲减伤：`伤害减免 = 护甲 / (护甲 + 300)`，**只作用于生命，不作用于护盾 / Overguard**。',
    '- 受击伤害 `= 攻击伤害 × (1−DR₁)×(1−DR₂)×… × 300/(300+护甲) × (1+DM₁)×(1+DM₂)×…`',
    '  —— 各来源**乘算**叠加（例：4 个 25% 减伤 → 实际承受 0.75⁴ ≈ 31.5%，而非 100% 免疫）。',
    '- 减伤来源：护甲（仅生命）｜纯减伤（技能，作用生命 + 护盾）｜伤害转移｜伤害类型修正｜'
    'Quick Thinking（能量当生命：`DR = 1 − 100 / 能量效率`）。',

    '', '### 三条血条与有效生命（EHP）', '',
    '- 受击结算顺序：**Overguard → 护盾 → 生命**（**毒素伤害跳过护盾**，直接打生命）。',
    '- 血条特性：**生命**受护甲减伤；**护盾**自带 50% 减伤 + 破盾短暂无敌（护盾门）；'
    '**Overguard** 无减伤但免疫控制。',
    '- 有效生命：',
    '  - `EHP(生命) = 净生命 × (净护甲 + 300)/300 × 1/(1 − 净减伤) × 1/(1 + 伤害类型修正)`',
    '  - `EHP(护盾) = (净护盾 + 净超护盾) × 0.5 × 1/(1 + 伤害类型修正)`',
    '  - `EHP(Overguard) = 净 Overguard × 1/(1 + 伤害类型修正)`',

    '', '### 敌人等级缩放（Enemy Level Scaling）', '',
    '高等级敌人的**生命 / 护盾 / 护甲**按下列曲线放大（`q = 当前等级 − 基础等级`）：',
    '- 过渡：`t = clamp((q − 70)/10, 0, 1)`，`s = t²(3 − 2t)`，`倍率 = f₁ + (f₂ − f₁) × s`',
    '  （即 70 级差以下用 f₁、80 级差以上用 f₂，中间平滑过渡）',
    '- **生命（Grineer / Scaldra）**：`f₁ = 1 + 0.015·q^2.12`｜`f₂ = 1 + 10.7332·q^0.72`',
    '- **护盾（Corpus）**：`f₁ = 1 + 0.02·q^1.76`｜`f₂ = 1 + 2·q^0.76`',
    '- **护甲（通用）**：`f₁ = 1 + 0.005·q^1.75`｜`f₂ = 1 + 0.4·q^0.75`'
    '（护甲硬上限 **2700**，对应 90% 减伤）',
    '- 当前值 = 基础值 × 倍率（基础值见 **05 号敌人文件**的各条「基础属性」）',
    '',
    '> **来源与口径**：以上机制与数值整理自 **wiki.warframe.com**（官方社区 Wiki）的 '
    'Damage / Status Effect / Critical Hit / Damage Reduction / Hit Points / Enemy Level Scaling 页（Update 36 口径），'
    '**仅提取公式与数值并自行重述，非原文照录**（公式与数值属事实，不涉版权）。'
    '数值可能随热修变动，如需最新请以 Wiki 原文为准。',
    '> **口径差异提醒**：本节「派系弱点 ×1.5 / 抗性 ×0.5」是 **Wiki 的简化派系口径**；'
    '上文「敌人防护层 × 元素修正」是 **DE 伤害控制器原始字段**。两者**不要混算**——'
    '前者用于“该拿什么元素打这个派系”，后者用于“这个防护层对某元素的具体修正”。',
]


def has_name(r):
    n = r.get('name')
    return bool(n) and str(n).strip() not in ('None', 'null', 'undefined')


def title_of(zh, en):
    zh = demark(zh).strip()
    en = demark(en).strip()
    if not zh:
        return en or '?'
    return zh if zh == en else '%s（%s）' % (zh, en)


def zhname(rec):
    return title_of((S.zh_item.get(rec.get('uniqueName')) or {}).get('name'), rec.get('name'))


def entry(title, body):
    return '### %s\n%s\n' % (title, body.rstrip())


def dedupe_body(body):
    """同标题条目：正文完全相同 → 去重；正文不同 → 用首个区分行给标题加后缀。"""
    parts = [p for p in re.split(r'\n(?=### )', body) if p.strip()]
    groups = OrderedDict()
    for p in parts:
        head = p.split('\n', 1)[0]
        groups.setdefault(head, []).append(p)
    out, n_drop, n_dis = [], 0, 0
    seen_titles = set()
    for head, ps in groups.items():
        if len(ps) == 1:
            out.append(ps[0])
            seen_titles.add(ps[0].split('\n', 1)[0])
            continue
        if len({p.split('\n', 1)[1] if '\n' in p else '' for p in ps}) == 1:
            out.append(ps[0])
            n_drop += len(ps) - 1
            continue
        for p in ps:
            lines = p.split('\n')
            sig = ''
            for ln in lines[1:4]:
                s = ln.lstrip('- ').strip()
                if s:
                    sig = '｜'.join(s.split('｜')[:2])[:32]
                    break
            t = '%s ｜%s' % (lines[0], sig) if sig else lines[0]
            if t in seen_titles:                  # 连区分行也相同 → 补序号
                i = 2
                while '%s #%d' % (t, i) in seen_titles:
                    i += 1
                t = '%s #%d' % (t, i)
            seen_titles.add(t)
            lines[0] = t
            n_dis += 1
            out.append('\n'.join(lines))
    return '\n'.join(out), n_drop, n_dis


def write_file(fn, intro, body):
    body, n_drop, n_dis = dedupe_body(body)
    txt = intro.rstrip() + '\n\n---\n\n' + body.rstrip() + '\n'
    with open(os.path.join(OUTDIR, fn), 'w', encoding='utf-8', newline='\n') as f:
        f.write(txt)
    if n_drop or n_dis:
        print('%s: 合并重复 %d / 消歧 %d' % (fn, n_drop, n_dis))
    return len(txt)


def split_entries(txt):
    return [p for p in re.split(r'\n(?=### )', txt) if p.strip().startswith('### ')]


def drops_brief(drops, top=2, sep='；'):
    if not drops:
        return ''
    ds = sorted(drops, key=lambda d: -(d.get('chance') or 0))
    out = ['%s%s' % (d.get('location') or '?',
                     (' ' + pct_raw(d.get('chance'))) if d.get('chance') is not None else '')
           for d in ds[:top]]
    s = sep.join(out)
    if len(ds) > top:
        s += '…（共 %d 处）' % len(ds)
    return s


def polarities_brief(pol):
    if not pol:
        return ''
    c = Counter(POLARITY_ZH.get(p, POLARITY_ZH.get(p.lower(), p)) for p in pol)
    return '、'.join(('%s×%d' % (k, v)) if v > 1 else k for k, v in c.items() if k)


def materials(components, cap=10):
    mats, bp = [], ''
    for c in components or []:
        nm = c.get('name') or ''
        if nm == 'Blueprint':
            if c.get('drops'):
                bp = drops_brief(c['drops'], 3)
            continue
        zn = S.name(c.get('uniqueName'), nm)
        cnt = c.get('itemCount')
        mats.append('%s ×%s' % (zn, trim_num(cnt, 0)) if cnt else zn)
    return mats[:cap], bp


def mod_effect(rec):
    """→ ([ (档位, 效果文本) ], 模式)  模式 ∈ official / zh / en

    规则：优先官方简中；官方未收录时按「术语规范化」转写；
    若某条转写后仍残留 ≥2 个英文词（说明是整句而非数值词条），
    则整体回退英文原文，避免出现「半英半中」的混排。
    """
    z = S.mod_effect_zh(rec)
    if z:
        return [('', z)], 'official'
    lv = rec.get('levelStats') or []
    if not lv:
        return [], 'official'

    def one(stats):
        zh, en, ok = [], [], True
        for s in stats or []:
            t = zh_stat(s)
            if not t or len(residue_words(t)) >= 2:
                ok = False
            zh.append(t)
            en.append(clean(s))
        return zh, en, ok

    z_top, e_top, ok_top = one(lv[-1].get('stats'))
    z_low, e_low, ok_low = one(lv[0].get('stats'))
    ok = ok_top and (ok_low or len(lv) == 1)
    top = '；'.join(dict.fromkeys(ok and z_top or e_top))
    low = '；'.join(dict.fromkeys(ok and z_low or e_low))
    label_top = '满级 %d' % (len(lv) - 1) if len(lv) > 1 else '效果'
    pairs = []
    if top:
        pairs.append((label_top, top))
    if low and low != top:
        pairs.append(('0 级', low))
    return pairs, ('zh' if ok else 'en')


# ================================================================== 01 战甲
def build_warframes():
    recs = sorted(I['Warframes'], key=lambda r: (r.get('name') or '').lower())
    body = []
    for r in recs:
        u = r['uniqueName']
        z = S.zh_item.get(u) or {}
        ov = (S.zh_overrides.get('warframes') or {}).get(u) or {}
        nm_zh = z.get('name') or r['name']
        ttl = title_of(nm_zh, r['name'])
        if r.get('type') == 'Necramech':
            kind = '殁世机甲（Necramech）'
        elif not r.get('health') and not r.get('shield') and not r.get('armor'):
            kind = '特殊条目（非可操控战甲）'
        elif r.get('isPrime'):
            kind = '战甲 · Prime 版'
        elif 'Umbra' in u:
            kind = '战甲 · Umbra 版'
        else:
            kind = '战甲 · 普通版'
        L = ['- 定位：%s｜精通等级需求 %s' % (kind, trim_num(r.get('masteryReq'), 0)),
             '- 属性：生命 %s｜护盾 %s｜护甲 %s｜能量 %s｜冲刺速度 %s' % (
                 trim_num(r.get('health'), 0), trim_num(r.get('shield'), 0),
                 trim_num(r.get('armor'), 0), trim_num(r.get('power'), 0),
                 trim_num(r.get('sprintSpeed'), 2))]
        pb = polarities_brief(r.get('polarities'))
        if pb:
            L.append('- 自带极性：%s' % pb)
        pas = clean(z.get('passiveDescription'), 130)
        if pas:
            L.append('- 被动：%s' % pas)
        ab = r.get('abilities') or []
        an = [S.ability_name(a) for a in ab]
        if an:
            L.append('- 技能：%s' % ' / '.join(an))
        d = clean(z.get('description'), 170)
        if d:
            L.append('- 简介：%s' % d)
        parts = []
        for c in (r.get('components') or []):
            if c.get('name') in COMP_ZH and c['name'] != 'Blueprint' and c.get('drops'):
                parts.append('%s %s' % (COMP_ZH[c['name']], drops_brief(c['drops'], 1, sep='')))
        if parts:
            L.append('- 获取（部件蓝图）：%s' % '｜'.join(parts))
        elif ov.get('acquire'):
            L.append('- 获取：%s' % ov['acquire'])
        elif r.get('isPrime'):
            L.append('- 获取：开启对应虚空遗物获得 Prime 部件蓝图')
        if r.get('vaulted'):
            L.append('- 状态：已入库（Vaulted），需等待 Prime 复刻或通过玩家交易获得部件')
        if r.get('marketCost'):
            L.append('- 商店：%s 铂金（可直接购买成品）' % trim_num(r['marketCost'], 0))
        elif r.get('bpCost'):
            L.append('- 蓝图价：%s 现金' % trim_num(r['bpCost'], 0))
        mats, _ = materials(r.get('components'), cap=8)
        if mats:
            L.append('- 建造材料：%s' % '｜'.join(mats))
        body.append(entry(ttl, '\n'.join(L)))
        if ab:
            AL = []
            for a, x in zip(ab, an):
                dd = S.ability_desc(a, 120)
                AL.append('- %s · %s：%s' % (nm_zh, x, dd) if dd else '- %s · %s' % (nm_zh, x))
            body.append(entry('%s 技能详情' % ttl, '\n'.join(AL)))

    # ---------------- 战甲基础属性排行（自建，回答「谁最高 / 排行」这类跨实体问题）----------------
    def _disp(r):
        z = S.zh_item.get(r['uniqueName']) or {}
        return z.get('name') or r['name']

    # 排行池：仅真正可操控战甲（排除殁世机甲 Bonewidow/Voidrig 与无属性的特殊条目 Helminth）
    _rank_pool = [r for r in recs if r.get('type') == 'Warframe' and r.get('health')]

    def _rank_lines(label, getter, nd):
        rows = [r for r in _rank_pool if getter(r) is not None]
        rows.sort(key=getter, reverse=True)
        return ['- %s｜%s %s' % (_disp(r), label, trim_num(getter(r), nd)) for r in rows]

    def _ehp(r):
        h, a = r.get('health'), r.get('armor')
        return None if h is None else h * ((a or 0) + 300) / 300.0

    body += ['', '## 战甲基础属性排行（30 级、未装 MOD 的基础值）', '',
             '下列条目按**单一指标降序**排列 %d 个可操控战甲，用于回答「哪个战甲 ×× 最高」'
             '「×× 排行」这类跨实体问题；每个战甲的完整属性（含被动/技能/获取）见其主条目。'
             '（殁世机甲 Bonewidow / Voidrig 与 Helminth 条目不参与排行。）' % len(_rank_pool), '']
    body.append(entry('战甲基础属性排行 · 护甲（降序）',
                      '\n'.join(_rank_lines('护甲', lambda r: r.get('armor'), 0))))
    body.append(entry('战甲基础属性排行 · 生命（降序）',
                      '\n'.join(_rank_lines('生命', lambda r: r.get('health'), 0))))
    body.append(entry('战甲基础属性排行 · 护盾（降序）',
                      '\n'.join(_rank_lines('护盾', lambda r: r.get('shield'), 0))))
    body.append(entry('战甲基础属性排行 · 能量（降序）',
                      '\n'.join(_rank_lines('能量', lambda r: r.get('power'), 0))))
    body.append(entry('战甲基础属性排行 · 冲刺速度（降序）',
                      '\n'.join(_rank_lines('冲刺速度', lambda r: r.get('sprintSpeed'), 2))))
    body.append(entry('战甲基础属性排行 · 有效生命 EHP（降序，生命计入护甲）',
                      '- 口径：`EHP = 生命 × (护甲 + 300) / 300`（仅生命计入护甲减伤，未计护盾与额外减伤）\n' +
                      '\n'.join(_rank_lines('EHP', _ehp, 0))))

    n_wf = sum(1 for r in recs if r.get('type') == 'Warframe')
    n_mech = sum(1 for r in recs if r.get('type') == 'Necramech')
    n_other = len(recs) - n_wf - n_mech
    intro = f"""# Warframe 知识库 · 战甲（Warframes）

{NAV}

> 收录 **{len(recs)}** 个条目：**{n_wf}** 个可操控战甲（普通版 / Prime 版 / Umbra 版）、
> **{n_mech}** 个殁世机甲（Bonewidow / Voidrig）、**{n_other}** 个特殊条目（Helminth 等），按英文名 A–Z 排序。
> 每个条目有 **2 条**：① 主条目（定位｜属性｜自带极性｜被动｜技能名｜简介｜获取｜建造）② 技能详情条目（技能中文说明）。
> 末尾另有 **6 条「基础属性排行」**（护甲 / 生命 / 护盾 / 能量 / 冲刺速度 / 有效生命 EHP），
> 按单一指标降序排列全部可操控战甲，用于回答「哪个战甲 ×× 最高」这类排行问题（不含殁世机甲与 Helminth）。
> 属性口径：**满级（30 级）未安装 MOD 的基础值**。〈〉内为随等级/技能强度变化的变量。
> 注意：**战甲名在 DE 官方简中中保留英文**（如 Ash / Gauss），因此标题多为英文原名。"""
    return write_file('01_战甲.md', intro, '\n'.join(body))


# ================================================================== 02 武器（主/副/近战 合一）
def weapon_part(title, key):
    recs = sorted(I[key], key=lambda r: (r.get('name') or '').lower())
    body = []
    for r in recs:
        ttl = zhname(r)
        sub = SLOT_ZH.get(r.get('productCategory') or '', r.get('productCategory') or '')
        L = ['- 定位：%s｜%s｜精通等级需求 %s' % (title, sub, trim_num(r.get('masteryReq'), 0))]
        td = r.get('totalDamage')
        dmg = fmt_damage(r.get('damagePerShot'), S.cfg_dt)
        if td is not None:
            L.append('- 总伤害：%s%s' % (trim_num(td, 1), ('（%s）' % dmg) if dmg else ''))
        elif dmg:
            L.append('- 伤害构成：%s' % dmg)
        st = []
        for f, fmt in (('criticalChance', lambda v: '暴击几率 %s' % pct_frac(v)),
                       ('criticalMultiplier', lambda v: '暴击倍率 %s×' % trim_num(v, 1)),
                       ('procChance', lambda v: '触发几率 %s' % pct_frac(v, 2)),
                       ('fireRate', lambda v: '射速 %s' % trim_num(v, 2)),
                       ('magazineSize', lambda v: '弹匣 %s' % trim_num(v, 0)),
                       ('reloadTime', lambda v: '装填 %s 秒' % trim_num(v, 2)),
                       ('multishot', lambda v: '多重射击 %s' % trim_num(v, 1)),
                       ('accuracy', lambda v: '精准度 %s' % trim_num(v, 1))):
            if r.get(f) is not None:
                st.append(fmt(r[f]))
        if st:
            L.append('- 数据：%s' % '｜'.join(st))
        st2 = []
        for f, fmt in (('trigger', lambda v: '扳机 %s' % TRIGGER_ZH.get(v, v)),
                       ('noise', lambda v: '噪音 %s' % NOISE_ZH.get(v, v)),
                       ('range', lambda v: '攻击范围 %s 米' % trim_num(v, 2)),
                       ('comboDuration', lambda v: '连击持续 %s 秒' % trim_num(v, 1)),
                       ('followThrough', lambda v: '穿透 %s' % trim_num(v, 2)),
                       ('omegaAttenuation', lambda v: '紫卡倾向 %s' % trim_num(v, 3))):
            if r.get(f) is not None:
                st2.append(fmt(r[f]))
        if st2:
            L.append('- 特性：%s' % '｜'.join(st2))
        d = clean((S.zh_item.get(r['uniqueName']) or {}).get('description'), 300)
        if d:
            L.append('- 简介：%s' % d)
        mats, bp = materials(r.get('components'))
        if mats:
            L.append('- 建造材料：%s' % '｜'.join(mats))
        if bp:
            L.append('- 蓝图来源：%s' % bp)
        elif r.get('drops'):
            L.append('- 获取：%s' % drops_brief(r['drops'], 3))
        if r.get('marketCost'):
            L.append('- 商店：%s 铂金' % trim_num(r['marketCost'], 0))
        elif r.get('bpCost'):
            L.append('- 蓝图价：%s 现金' % trim_num(r['bpCost'], 0))
        if r.get('vaulted'):
            L.append('- 状态：已入库（Vaulted）')
        body.append(entry(ttl, '\n'.join(L)))
    return recs, body


WEAPON_PARTS = [('主武器（Primary）', 'Primary', ''),
                ('副武器（Secondary）', 'Secondary', ''),
                ('近战武器（Melee）', 'Melee',
                 '近战武器另有连击、滑行攻击、重击与架式（Stance）机制。')]


def build_weapons():
    B, cnt = [], []
    for i, (label, key, note) in enumerate(WEAPON_PARTS):
        B += ['## %s、%s' % ('一二三'[i], label), '']
        if note:
            B += ['> %s' % note, '']
        recs, ents = weapon_part(label, key)
        B += ents + ['']
        cnt.append(len(recs))
    # ---- 四、紫卡倾向一览 ----
    riv = []
    for key, lb in (('Primary', '主武器'), ('Secondary', '副武器'), ('Melee', '近战')):
        for r in I[key]:
            v = r.get('omegaAttenuation')
            if v is None:
                continue
            riv.append((float(v), zhname(r), lb))
    riv.sort(key=lambda x: (-x[0], x[1]))
    B += ['## 四、紫卡倾向一览（Riven Disposition）', '',
          '紫卡（裂罅）MOD 的词条数值由武器的**倾向值（Disposition）**缩放：倾向越高，'
          '洗出的词条数值越强、负面也越弱。倾向随 Prime 武器更替定期调整，区间约 '
          '**0.50（最低）– 1.55（最高）**。下列按倾向**从高到低**排列：', '']
    B.append('### 紫卡倾向一览（按倾向降序，共 %d 件武器）' % len(riv))
    B += ['- %s｜%s｜紫卡倾向 %s' % (nm, lb, trim_num(v, 3)) for v, nm, lb in riv]
    tot_n = sum(cnt)
    intro = f"""# Warframe 知识库 · 武器（主武器 / 副武器 / 近战）

{NAV}

> 收录 **{tot_n}** 件武器：主武器 **{cnt[0]}**｜副武器 **{cnt[1]}**｜近战 **{cnt[2]}**，各按英文名 A–Z 排序。
> 每条格式：**中文名（English）**｜定位｜总伤害与元素构成｜暴击/触发/射速等数值｜特性（含**紫卡倾向**）｜简介｜建造材料｜获取。
> 数值口径：**未安装 MOD 的出厂基础值**。
> 第四部分「**紫卡倾向一览**」按倾向降序汇总全部武器，用于回答「某武器紫卡倾向多少 / 哪些武器倾向最高」。
> 元素顺序：冲击 / 穿刺 / 切割 / 火焰 / 冰冻 / 电击 / 毒素 / 爆炸 / 辐射 / 毒气 / 磁力 / 病毒 / 腐蚀 / 虚空。"""
    return write_file('02_武器.md', intro, '\n'.join(B))


# ================================================================== 05 MOD 与赋能
def build_mods():
    mods = sorted(I['Mods'], key=lambda r: (r.get('name') or '').lower())
    arcs = sorted(I['Arcanes'], key=lambda r: (r.get('name') or '').lower())
    body = ['## 一、MOD', '']
    n_off = n_zh = n_en = 0
    for r in mods:
        L = []
        seg = ['类型 %s' % MODTYPE_ZH.get(r.get('type'), r.get('type') or 'MOD')]
        if r.get('compatName'):
            seg.append('适配 %s' % r['compatName'])
        if r.get('polarity'):
            seg.append('极性 %s' % POLARITY_ZH.get(r['polarity'],
                                                  POLARITY_ZH.get(r['polarity'].lower(), r['polarity'])))
        if r.get('rarity'):
            seg.append('稀有度 %s' % RARITY_ZH.get(r['rarity'], r['rarity']))
        if r.get('baseDrain') is not None:
            seg.append('基础容量 %s' % trim_num(r['baseDrain'], 0))
        if r.get('fusionLimit') is not None:
            seg.append('满级 %s' % trim_num(r['fusionLimit'], 0))
        if r.get('isAugment'):
            seg.append('战甲强化 MOD')
        if r.get('isExilus'):
            seg.append('特殊功能槽')
        L.append('- %s' % '｜'.join(seg))
        pairs, mode = mod_effect(r)
        for label, txt in pairs:
            L.append('- 效果：%s' % txt if not label else '- 效果（%s）：%s' % (label, txt))
        if mode == 'official':
            n_off += 1
        elif mode == 'zh':
            n_zh += 1
        else:
            n_en += 1
        if r.get('drops'):
            L.append('- 获取：%s' % drops_brief(r['drops'], 3))
        if r.get('transmutable'):
            L.append('- 可通过转换（Transmutation）获得')
        if r.get('tradable'):
            L.append('- 可交易')
        body.append(entry(zhname(r), '\n'.join(L)))
    # WSD 旧版赋能表（带官方中文效果），按「X 赋能」↔「赋能·X」与现行 172 个赋能对齐
    wsd_arc = {}
    for a in S.wsd.get('zh_arcanes') or []:
        stem = to_text(a.get('name')).replace(' 赋能', '').replace('赋能', '').strip()
        if stem:
            wsd_arc[stem] = a
    n_arc_off = n_arc_zh = n_arc_en = 0
    body += ['', '## 二、赋能（Arcanes）', '']
    for r in arcs:
        lv = r.get('levelStats') or []
        seg = ['类型 %s' % (r.get('type') or '赋能')]
        if r.get('rarity'):
            seg.append('稀有度 %s' % RARITY_ZH.get(r['rarity'], r['rarity']))
        if len(lv) > 1:
            seg.append('最高 %s 级' % (len(lv) - 1))
        L = ['- %s' % '｜'.join(seg)]
        pairs, mode = mod_effect(r)
        for label, txt in pairs:
            L.append('- 效果：%s' % txt if not label else '- 效果（%s）：%s' % (label, txt))
        if mode == 'official':
            n_arc_off += 1
        else:
            # 回退：WSD 旧版赋能表的官方中文效果
            mine = to_text((S.zh_item.get(r['uniqueName']) or {}).get('name'))
            w = wsd_arc.get(mine.replace('赋能·', '').replace('赋能', '').strip())
            if w and to_text(w.get('effect')):
                L.append('- 效果（官方简中）：%s' % clean(w['effect'], 260))
                if to_text(w.get('rarity')):
                    L.append('- 稀有度（旧表）：%s' % to_text(w['rarity']))
                n_arc_off += 1
            elif mode == 'zh':
                n_arc_zh += 1
            else:
                n_arc_en += 1
        if r.get('drops'):
            L.append('- 获取：%s' % drops_brief(r['drops'], 3))
        if r.get('tradable'):
            L.append('- 可交易')
        body.append(entry(zhname(r), '\n'.join(L)))
    # ---------------- 三、MOD 套装 ----------------
    sets_ = P.get('ModSet') or {}
    by_set = defaultdict(list)
    for r in mods:
        _sk = r.get('set') or r.get('modSet')
        if _sk:
            by_set[_sk].append(r)
    if sets_:
        body += ['', '## 三、MOD 套装（Mod Sets）', '',
                 '同一套装的 MOD 同时装备后获得**套装加成**，加成数值随装备件数递增。'
                 '下列「档位」即套装描述中占位变量（如 〈STAT1〉）在各档的取值。', '']
        for k in sorted(sets_.keys(), key=lambda x: x.split('/Sets/')[-1].lower()):
            v = sets_[k]
            # 键形如 /Lotus/Upgrades/Mods/Sets/Amar/AmarSetMod → 取末段（去掉重复的目录名）
            seg = k.split('/Sets/')[-1].replace('SetMod', '').split('/')[-1]
            members = by_set.get(k, [])
            mzh = []
            for r in members:
                u = r.get('uniqueName')
                zz = (S.i18n.get(u) or {}).get('zh')
                nm = zz.get('name') if isinstance(zz, dict) else (zz if isinstance(zz, str) else None)
                mzh.append(nm or r.get('name'))
            mzh = sorted(dict.fromkeys(mzh))
            zh_set = ''
            if mzh:
                pref = mzh[0]
                for sname in mzh[1:]:
                    i = 0
                    while i < min(len(pref), len(sname)) and pref[i] == sname[i]:
                        i += 1
                    pref = pref[:i]
                zh_set = pref.strip(' ··').rstrip('之').strip()
            desc = clean(S.dz.get(v.get('description')), 240)
            lv = v.get('levelStats') or []
            L = ['- 类型：MOD 套装（%s）｜共 %s 张成员 MOD' % (seg, v.get('numUpgradesInSet'))]
            if mzh:
                L.append('- 成员 MOD：%s' % '、'.join(mzh))
            if desc:
                L.append('- 套装加成：%s' % desc)
            keys_ = sorted({kk for t in lv for kk in t})
            if keys_:
                vals = []
                for kk in keys_:
                    seq = [t.get(kk) for t in lv if kk in t]
                    vals.append('%s = %s' % (kk, ' / '.join(seq)))
                L.append('- 加成档位（随件数递增）：%s' % '；'.join(vals))
            ttl = ('套装 · %s（%s）' % (zh_set, seg)) if zh_set else ('套装 · %s' % seg)
            body.append(entry(ttl, '\n'.join(L)))

    intro = f"""# Warframe 知识库 · MOD 与赋能

{NAV}

> 收录 **{len(mods)}** 张 MOD、**{len(arcs)}** 个赋能，以及 **{len(sets_)}** 个 MOD 套装
> （一 MOD｜二 赋能｜三 MOD 套装），按英文名 A–Z 排序。
> 每条格式：**中文名（English）**｜类型/适配/极性/稀有度/容量/满级｜效果｜获取。
> **效果文本来源**（透明标注）：
> · **{n_off}** 张 MOD / **{n_arc_off}** 个赋能 —— **DE 官方简中**原文；
> · **{n_zh}** 张 MOD / **{n_arc_zh}** 个赋能 —— 按「术语规范化」把英文词条转写为中文（**数值原样保留、属性术语采用官方简中译名**，如 Fire Rate → 射速、Status Chance → 触发几率）；
> · **{n_en}** 张 MOD / **{n_arc_en}** 个赋能 —— 效果是整句描述（强化 MOD 等），无法用术语替换表达，**保留英文原文**，以免出现半英半中的混排。
> 极性：Madurai（攻击）Vazarin（防御）Naramon（战术）Zenurik（威力）Penjaga（预置）Unairu（守护）Umbra（暗影）Aura（光环）。
> 稀有度：普通 Common｜罕见 Uncommon｜稀有 Rare｜传奇 Legendary。"""
    return write_file('03_MOD与赋能.md', intro, '\n'.join(body))


# ================================================================== 06 遗物
def _droppable_relic_bases() -> set:
    """当前掉落表(missionRewards)中出现的遗物基名(如 'Lith S19')。

    用途:WFCD items 表的 `vaulted` 标记有滞后(2026-09-24 实测:Citrine Prime
    四张新遗物已上掉落表但 items 仍标 vaulted=true),以「出现在当前掉落表」为准覆盖。
    """
    # 首选 all.slim(与插件 drops.json 同源、最新);缺它时回退 all.json 的非事件节点
    slim = os.path.join(kb_data_dir(), 'drop', 'all.slim.json')
    if os.path.exists(slim):
        rows = json.load(open(slim, encoding='utf-8'))
        return {(r.get('item') or '')[:-len(' Relic')].strip()
                for r in rows if (r.get('item') or '').endswith(' Relic')}
    mr = (S.drop.get('all') or {}).get('missionRewards') or {}
    out = set()
    for _p, nodes in mr.items():
        for _n, vv in (nodes or {}).items():
            if (vv or {}).get('isEvent'):
                continue          # 事件节点含历史/轮换奖励,会误判退役遗物为在刷
            rw = (vv or {}).get('rewards')
            rows = [x for lst in rw.values() for x in lst] if isinstance(rw, dict) else (rw or [])
            for row in rows:
                it = to_text((row or {}).get('itemName') or (row or {}).get('item') or '')
                if it.endswith(' Relic'):
                    out.add(it[:-len(' Relic')].strip())
    return out


_DROPPABLE_RELICS = None


def relic_is_farmable(name: str) -> bool:
    """遗物基名是否在当前掉落表中(Intact/Exceptional/Flawless/Radiant 后缀先剥离)。"""
    global _DROPPABLE_RELICS
    if _DROPPABLE_RELICS is None:
        _DROPPABLE_RELICS = _droppable_relic_bases()
    base = re.sub(r'\s+(Intact|Exceptional|Flawless|Radiant)$', '', to_text(name or '')).strip()
    return base in _DROPPABLE_RELICS


def build_relics():
    groups = OrderedDict()
    for x in I['Relics']:
        m = re.match(r'^(.+?)\s+(Intact|Exceptional|Flawless|Radiant)$', x.get('name') or '')
        if m:
            groups.setdefault(m.group(1), {})[m.group(2)] = x
        else:
            groups.setdefault(x.get('name') or '?', {})['__other__'] = x
    groups = {k: v for k, v in groups.items() if any(x.get('rewards') for x in v.values())}
    order = ['Intact', 'Exceptional', 'Flawless', 'Radiant']
    body = ['## 一、遗物明细（按纪元 + 编号）', '']
    rev = defaultdict(dict)
    miss = 0
    for gname in sorted(groups):
        g = groups[gname]
        rep = g.get('Exceptional') or g.get('Intact') or next(iter(g.values()))
        era_en = gname.split(' ')[0]
        zn = title_of((S.zh_item.get(rep['uniqueName']) or {}).get('name'), gname)
        short = zn.split('（')[0]
        L = ['- 类型：%s（%s）虚空遗物' % (RELIC_ERA_ZH.get(era_en, era_en), era_en)]
        if not relic_is_farmable(rep.get('name')):
            # 状态一律以「在不在当前掉落表」为准:WFCD items 的 vaulted 标记
            # 双向滞后(新遗物误标 true、刚退役误标 false),2026-09-24 实测
            L.append('- 状态：已入库（Vaulted）')
        for k in order:
            x = g.get(k)
            if not x or not x.get('rewards'):
                continue
            items = []
            for rw in sorted(x['rewards'], key=lambda a: -(a.get('chance') or 0)):
                it = rw.get('item') or {}
                inm = S.name(it.get('uniqueName'))
                if not inm:
                    inm = it.get('name') or '?'
                    miss += 1
                items.append('%s %s' % (inm, pct_raw(rw.get('chance'))))
                prev = rev[it.get('uniqueName')]
                cur = prev.get(k)
                if cur is None or (rw.get('chance') or 0) > cur[2]:
                    prev[k] = (short, gname, rw.get('chance'))
            L.append('- %s %s（%s）掉落：%s' % (short, REFINE_ZH[k], k, '｜'.join(items)))
        body.append(entry(zn, '\n'.join(L)))
    body += ['', '## 二、按奖励反查遗物（某部件由哪些遗物产出）', '']
    n_rev = 0
    for u in sorted(rev, key=lambda k: (S.name(k) or 'zzz')):
        zn = S.name(u)
        if not zn:
            continue
        n_rev += 1
        m = rev[u]
        lst = ['%s（%s档 %s）' % (m[k][0], REFINE_ZH[k], pct_raw(m[k][2]))
               for k in order if k in m]
        peer = '；'.join(lst[:10]) + ('…（共 %d 个遗物）' % len(lst) if len(lst) > 10 else '')
        body.append(entry(title_of(zn, u.rsplit('/', 1)[-1]), '- 产出遗物：%s' % peer))
    intro = f"""# Warframe 知识库 · 遗物（虚空遗物 / Void Relics）

{NAV}

> **第一部分「遗物明细」**：**{len(groups)}** 个遗物（按「纪元 + 编号」去重，4 档精炼写在同一条内），按纪元与编号排序。
> **第二部分「按奖励反查遗物」**：**{n_rev}** 个可产出物，反查它由哪些遗物产出——用于回答「XX 部件在哪个遗物里」。
> 纪元：古纪 Lith｜前纪 Meso｜中纪 Neo｜后纪 Axi｜安魂 Requiem｜先锋 Vanguard｜全能 Omni。
> 精炼：完整 Intact → 优良 Exceptional → 无瑕 Flawless → 光辉 Radiant（用虚空光体精炼，越高级越容易出高稀有奖励）。稀有度：普通 / 罕见 / 稀有。
> 遗物明细条目较长（4 档 × 6 项奖励），会被切成 2 个分块，但**每行都自带遗物名与档位**，分块后仍可独立命中。"""
    return write_file('04_遗物.md', intro, '\n'.join(body))


# ================================================================== 07 敌人
def build_enemies():
    av = (P.get('Enemies') or {}).get('avatars') or {}
    body = []
    zh_hit = 0
    used = Counter()
    dup_fix = 0
    for r in sorted(I['Enemy'], key=lambda x: (x.get('name') or '').lower()):
        a = av.get(r['uniqueName']) or {}
        zn = to_text(S.dz.get(a.get('name'))) if a.get('name') else ''
        if zn:
            zh_hit += 1
        else:
            zn = r.get('name') or '?'
        ttl = title_of(zn, r['name'])
        used[ttl] += 1
        if used[ttl] > 1:                      # 官方译名重复 → 用资产末段消歧
            seg = r['uniqueName'].rsplit('/', 1)[-1]
            ttl = '%s ｜%s' % (ttl, seg)
            dup_fix += 1
        f = a.get('faction') or r.get('faction')
        L = ['- 派系：%s｜类型 %s' % (FACTION_ZH.get(f, f or '未标注'),
                                    ENEMY_TYPE_ZH.get(r.get('type'), r.get('type') or '未分类')),
             '- 基础属性：生命 %s｜护盾 %s｜护甲 %s%s' % (
                 trim_num(r.get('health'), 0), trim_num(r.get('shield'), 0),
                 trim_num(r.get('armor'), 0),
                 ('｜击杀经验 %s' % trim_num(a['killXPReward'], 0)) if a.get('killXPReward') else '')]
        for layer in (r.get('resistances') or []):
            amt = layer.get('amount')
            if not amt:
                continue
            weak, res = [], []
            for af in (layer.get('affectors') or []):
                el = af.get('element')
                mod = af.get('modifier')
                if not el or el == 'None' or not mod:
                    continue
                z = DT_ZH.get(el.lower(), el)
                (weak if mod > 0 else res).append('%s %+g' % (z, mod))
            if not weak and not res:
                continue
            seg = []
            if weak:
                seg.append('弱点（增伤）%s' % '、'.join(weak))
            if res:
                seg.append('抗性（减伤）%s' % '、'.join(res))
            L.append('- 「%s」防护层 %s：%s' % (layer.get('type'), trim_num(amt, 0), '；'.join(seg)))
        d = S.zhdesc(r, 200)
        if d:
            L.append('- 简介：%s' % d)
        if r.get('drops'):
            L.append('- 掉落：%s' % drops_brief(r['drops'], 3))
        body.append(entry(title_of(zn, r['name']), '\n'.join(L)))
    intro = f"""# Warframe 知识库 · 敌人（Enemies）

{NAV}

> 收录 **{len(I['Enemy'])}** 个敌人 / NPC，其中 **{zh_hit}** 个有 DE 官方简中名（其余官方保留英文）。按英文名 A–Z 排序；{dup_fix} 条译名重复的条目以资产名后缀消歧。
> 每条格式：**中文名（English）**｜派系｜敌人类型｜基础属性｜防护层弱点与抗性｜简介｜掉落。
> **弱点 / 抗性数值**为 DE 伤害控制器原始修正值：**正值 = 该元素对此防护层增伤（弱点），负值 = 减伤（抗性）**。
> 常见防护层：Cloned Flesh（克隆体血肉）｜Ferrite Armor（铁氧体护甲）｜Alloy Armor（合金护甲）｜Shield / Proto Shield（护盾）｜Flesh（肉体）｜Infested Flesh 等。
> 派系译名：Grineer / Corpus / Infested / Sentient / Corrupted 官方保留英文；Orokin → 奥罗金；合一众 Narmer、炽蛇军 Scaldra、科腐者 Techrot、墙中人 Man in the Wall 用中文。
> ⚠ **单位名与派系名口径不同**：Corrupted 系**敌人单位**在官方简中确有中文（Corrupted Lancer → 堕落枪兵、Corrupted Ancient → 远古堕落者），但**派系名** Corrupted 官方保留英文 —— 两者都是 DE 官方原文。"""
    return write_file('05_敌人.md', intro, '\n'.join(body))


# ================================================================== 08 资源与蓝图
def build_resources():
    def simple(items, label, extra=None):
        out = []
        for r in sorted(items, key=lambda x: (x.get('name') or '').lower()):
            L = ['- 类型：%s' % label]
            if extra:
                L += extra(r)
            d = S.zhdesc(r, 320)
            if d:
                L.append('- 说明：%s' % d)
            if r.get('drops'):
                L.append('- 用途 / 掉落：%s' % drops_brief(r['drops'], 3))
            if r.get('tradable'):
                L.append('- 可交易')
            out.append(entry(zhname(r), '\n'.join(L)))
        return out

    B = ['## 一、资源与材料', '']
    B += simple(I['Resources'], '资源 / 材料')

    B += ['', '## 二、装备品（Gear：消耗品与工具）', '']
    def gear_extra(r):
        x = []
        if r.get('masteryReq') is not None:
            x.append('- 精通等级需求 %s' % trim_num(r['masteryReq'], 0))
        return x
    for r in sorted(I['Gear'], key=lambda x: (x.get('name') or '').lower()):
        L = ['- 类型：装备品（Gear）']
        if r.get('masteryReq') is not None:
            L.append('- 精通等级需求 %s' % trim_num(r['masteryReq'], 0))
        d = S.zhdesc(r, 280)
        if d:
            L.append('- 说明：%s' % d)
        mats, bp = materials(r.get('components'))
        if mats:
            L.append('- 建造材料：%s' % '｜'.join(mats))
        if bp:
            L.append('- 蓝图来源：%s' % bp)
        if r.get('marketCost'):
            L.append('- 商店：%s 铂金' % trim_num(r['marketCost'], 0))
        if r.get('drops'):
            L.append('- 获取：%s' % drops_brief(r['drops'], 3))
        B.append(entry(zhname(r), '\n'.join(L)))

    B += ['', '## 三、鱼类（开放世界垂钓）', '']
    B += simple(I['Fish'], '鱼类（开放世界垂钓）')

    B += ['', '## 四、制造配方（DE 官方导出，材料清单最权威）', '']
    rec = P.get('Recipes') or {}
    n = 0
    for k in sorted(rec):
        v = rec[k]
        rt = v.get('resultType')
        zn = S.name(rt)
        if not zn:
            continue
        n += 1
        seg = rt.rsplit('/', 1)[-1]
        L = []
        if v.get('buildPrice') is not None:
            L.append('- 建造费用：%s 现金｜建造时间 %s 小时' % (
                trim_num(v['buildPrice'], 0), trim_num((v.get('buildTime') or 0) / 3600.0, 1)))
        ings = []
        for ing in (v.get('ingredients') or [])[:10]:
            inm = S.name(ing.get('ItemType')) or ing.get('ItemType', '').rsplit('/', 1)[-1]
            ings.append('%s ×%s' % (inm, trim_num(ing.get('ItemCount'), 0)))
        if ings:
            L.append('- 所需材料：%s' % '｜'.join(ings))
        if v.get('consumeOnUse'):
            L.append('- 使用后消耗')
        if v.get('tradable'):
            L.append('- 可交易')
        B.append(entry(title_of(zn, seg), '\n'.join(L)))
    intro = f"""# Warframe 知识库 · 资源、装备品、鱼类与制造配方

{NAV}

> 四部分：① 资源与材料 **{len(I['Resources'])}** 条 ② 装备品 Gear **{len(I['Gear'])}** 条 ③ 鱼类 **{len(I['Fish'])}** 条 ④ 制造配方 **{n}** 条（DE 官方导出，材料清单最权威）。
> 每条格式：**中文名（English）**｜类型｜说明（常含获取地点 Location）｜建造材料｜获取。
> 说明文本优先使用 DE 官方简中，未收录时保留英文原文。"""
    return write_file('06_资源与蓝图.md', intro, '\n'.join(B))


# ================================================================== 09 同伴与空战
def build_companions():
    def generic(key, label, note=''):
        out = []
        for r in sorted(I.get(key, []), key=lambda x: (x.get('name') or '').lower()):
            L = ['- 类型：%s' % label]
            if note:
                L.append('- %s' % note)
            st = []
            for f, lbl in (('health', '生命'), ('shield', '护盾'), ('armor', '护甲'),
                           ('power', '能量'), ('masteryReq', '精通需求')):
                if r.get(f) is not None:
                    st.append('%s %s' % (lbl, trim_num(r[f], 0)))
            if st:
                L.append('- 属性：%s' % '｜'.join(st))
            pb = polarities_brief(r.get('polarities'))
            if pb:
                L.append('- 自带极性：%s' % pb)
            ab = r.get('abilities') or []
            if ab:
                L.append('- 技能 / 指令：%s' % ' / '.join(S.ability_name(a) for a in ab))
            d = S.zhdesc(r, 260)
            if d:
                L.append('- 说明：%s' % d)
            mats, bp = materials(r.get('components'))
            if mats:
                L.append('- 建造材料：%s' % '｜'.join(mats))
            if bp:
                L.append('- 蓝图来源：%s' % bp)
            if r.get('drops'):
                L.append('- 获取：%s' % drops_brief(r['drops'], 3))
            if r.get('marketCost'):
                L.append('- 商店：%s 铂金' % trim_num(r['marketCost'], 0))
            out.append(entry(zhname(r), '\n'.join(L)))
        return out

    B = ['## 一、守护（Sentinels）与守护武器', '']
    B += generic('Sentinels', '守护（Sentinel）')
    B += generic('SentinelWeapons', '守护武器（Sentinel Weapon）')
    B += ['', '## 二、宠物（库狛 / 库娃 / 恐鸟 / 捕猎兽）', '']
    B += generic('Pets', '宠物同伴（Companion）')
    B += ['', '## 三、Archwing 空战与空战武器', '']
    B += generic('Archwing', 'Archwing 空战战甲', 'Archwing 为太空战专用飞行战甲，独立装备与升级')
    B += generic('Arch-Gun', '空战枪械（Arch-Gun）')
    B += generic('Arch-Melee', '空战近战（Arch-Melee）')
    B += ['', '## 四、九重天（Railjack）', '']
    B += generic('Railjack', '九重天条目（Railjack）')
    intro = f"""# Warframe 知识库 · 同伴、Archwing 空战与九重天

{NAV}

> 收录：守护 {len(I.get('Sentinels', []))}｜守护武器 {len(I.get('SentinelWeapons', []))}｜宠物 {len(I.get('Pets', []))}｜Archwing {len(I.get('Archwing', []))}｜空战枪械 {len(I.get('Arch-Gun', []))}｜空战近战 {len(I.get('Arch-Melee', []))}｜九重天条目 {len(I.get('Railjack', []))}。
> 每条格式：**中文名（English）**｜类型｜属性｜自带极性｜技能｜说明｜建造材料｜获取。"""
    return write_file('07_同伴与空战.md', intro, '\n'.join(B))


# ================================================================== 10 其他与机制术语
def build_others():
    B = ['## 一、伤害机制与术语对照表', '',
         '### 伤害类型（Damage Types，共 20 种）', '',
         '按 DE 导出的内部顺序排列并归类；括号内为 DE 资产字段值（游戏内元素标签与此一一对应）：']
    dt_order = [d.lower() for d in S.cfg_dt]
    for lab, ds in DT_GROUP:
        seg = ['%s（%s）' % (DT_ZH.get(d, d), d) for d in ds if d in dt_order]
        if seg:
            B.append('- **%s**：%s' % (lab, '｜'.join(seg)))
    B += ['', '> 注：**元素组合规则、各状态触发的 DoT 百分比、层数上限、剥甲比例**属于游戏引擎逻辑，'
          '数据包中**没有**任何对应字段（44 张 DE 导出表里不存在 proc / curve 数值表），'
          '无法从数据反推——如需这部分请以官方 Wiki 的人工说明为准。', '',

          '### 敌人防护层 × 元素修正（反推自 DE 伤害控制器）', '',
          '每个敌人由若干**防护层**（护甲 / 肉体 / 护盾…）叠加构成。下表为各防护层对伤害类型的**原始修正**：'
          '**正值 = 增伤（弱点），负值 = 减伤（抗性）**。这是 DE 伤害控制器的字段值，'
          '**不是最终倍率**（最终倍率还受护甲值、等级缩放影响）。', '']
    B += protection_matrix()
    B += MECH_BLOCK
    B += ['', '### MOD 极性（Polarities）', '',
          '极性决定 MOD 装入对应槽位时的容量消耗（同极性容量减半、反向极性容量增加）：']
    for p in S.cfg_pol:
        B.append('- %s（%s）' % (POLARITY_ZH.get(p['id'], p['name']), p['name']))
    B += ['', '### 稀有度（Rarity）', '']
    for k in ('Common', 'Uncommon', 'Rare', 'Legendary'):
        B.append('- %s（%s）' % (RARITY_ZH[k], k))
    B += ['', '### 派系（Factions）', '',
          'DE 官方简中**不翻译** Grineer / Corpus / Infested / Sentient / Corrupted，卡面保留英文；'
          '只有奥罗金等有官方中文：']
    seen = set()
    for k, v in FACTION_ZH.items():
        if v in seen:
            continue
        seen.add(v)
        B.append('- %s（%s）' % (v, k))
    B += ['', '### 遗物纪元（Relic Eras）', '']
    for k, v in RELIC_ERA_ZH.items():
        B.append('- %s（%s）' % (v, k))
    B += ['', '### 遗物精炼档位（Refinement）', '']
    for k, v in REFINE_ZH.items():
        B.append('- %s（%s）' % (v, k))
    B += ['', '### 任务类型（Mission Types）', '']
    mt = P.get('MissionTypes') or {}
    sn = set()
    for k in sorted(mt):
        nm = to_text(S.dz.get(mt[k].get('name')))
        if nm and nm not in sn:
            sn.add(nm)
            B.append('- %s（%s）' % (nm, k))
    B += ['', '### 集团（Syndicates）', '']
    for k in sorted(P.get('Syndicates') or {}):
        v = P['Syndicates'][k]
        nm = to_text(S.dz.get(v.get('name')))
        if not nm:
            continue
        d = clean(S.dz.get(v.get('description')), 150)
        B.append('- **%s**（%s）%s' % (nm, k, ('：' + d) if d else ''))
        tt = v.get('titles') or []
        if tt:
            B.append('  - 声望等级：%s' % ' → '.join(
                '%s Lv%s' % (to_text(S.dz.get(t.get('name'))) or t.get('name'), t.get('level'))
                for t in tt))
    B += ['', '### 专精流派（Focus Schools）', '']
    POL2SCH = {'AP_ATTACK': 'Madurai（攻击）', 'AP_DEFENSE': 'Vazarin（防御）',
               'AP_TACTIC': 'Naramon（战术）', 'AP_POWER': 'Zenurik（威力）',
               'AP_WARD': 'Unairu（守护）'}
    schools = defaultdict(list)
    for k in sorted(P.get('FocusUpgrades') or {}):
        v = P['FocusUpgrades'][k]
        nm = to_text(S.dz.get(v.get('name')))
        if not nm:
            continue
        d = clean(S.dz.get(v.get('description')), 170)
        schools[POL2SCH.get(v.get('polarity'), v.get('polarity') or '其他')].append(
            '- **%s**（%s）%s' % (nm, k.rsplit('/', 1)[-1], ('：' + d) if d else ''))
    for sch in sorted(schools, key=str):
        B += ['#### %s' % sch, ''] + schools[sch] + ['']
    B += ['### 九重天内源之力（Railjack Intrinsics）', '']
    for k in sorted(P.get('Intrinsics') or {}):
        v = P['Intrinsics'][k]
        nm = to_text(S.dz.get(v.get('name')))
        if not nm:
            continue
        B.append('- **%s**（%s）：%s' % (nm, k, clean(S.dz.get(v.get('description')), 190)))
        for rk in (v.get('ranks') or []):
            rn = to_text(S.dz.get(rk.get('name')))
            if rn:
                B.append('  - %s：%s' % (rn, clean(S.dz.get(rk.get('description')), 150)))

    # ---------------- 二、核心机制 ----------------
    B += ['', '## 二、核心机制（执刑官源力石 / 钢铁之路 / 突击 / 合成目标 / 集团商店）', '']

    B += ['### 执刑官源力石（Archon Shards）', '',
          '战甲满级并安装 Helminth 消化系统后可镶嵌源力石。**Tau 融造**版数值更高。'
          '同一颜色可叠加，装备多颗同色源力石还会触发额外的颜色加成：', '']
    ash = S.wsd.get('zh_archonShards') or {}
    for k, v in ash.items():
        if not isinstance(v, dict):
            continue
        ups = v.get('upgradeTypes') or {}
        vals = [to_text(u.get('value')) for u in ups.values() if isinstance(u, dict)]
        if vals:
            B.append('- **%s**（%s）：%s' % (to_text(v.get('value')), k, '；'.join(vals)))

    B += ['', '### 钢铁之路（Steel Path）商店', '',
          '通关全部星图后解锁的困难模式，用**钢铁精华**（Steel Essence）在 Teshin 处兑换：', '']
    sp = S.wsd.get('zh_steelPath') or {}
    for key, label in (('rotation', '轮换商品 · 每周一 00:00 UTC 轮换'),
                       ('evergreen', '常驻商品')):
        items = sp.get(key) or []
        if not items:
            continue
        B += ['#### %s（%d 项）' % (label, len(items)), '']
        for it in items:
            B.append('- %s｜%s 钢铁精华' % (to_text(it.get('name')), trim_num(it.get('cost'), 0)))
        B.append('')

    B += ['### 突击（Sortie）', '',
          '每日 3 个连续任务（敌人等级 50–100），每个任务附带一个修正词。', '', '#### 修正词', '']
    sd = S.wsd.get('zh_sortieData') or {}
    mts = sd.get('modifierTypes') or {}
    mds = sd.get('modifierDescriptions') or {}
    for k in sorted(mts):
        nm = SORTIE_MOD_ZH.get(k) or t2s(mts[k])
        d = clean(t2s(mds.get(k)), 150) if isinstance(mds, dict) else ''
        B.append('- %s（%s）%s' % (nm, k, ('：' + d) if d else ''))
    B += ['', '#### 突击首领（Assassination Bosses）', '']
    for k in sorted(sd.get('bosses') or {}):
        v = sd['bosses'][k]
        if isinstance(v, dict) and v.get('name'):
            f = to_text(v.get('faction'))
            B.append('- %s（%s）｜派系：%s' % (t2s(v['name']), k,
                                          FACTION_ZH_FIX.get(f, FACTION_ZH.get(f, f))))

    B += ['', '### 合成目标（Synth Targets）', '',
          'Helminth 消化系统与圣殿突袭合成所需的指定敌人，下列为各目标的推荐刷取点：', '']
    zh_by_key = {t.get('imageKey'): t for t in S.wsd.get('zh_synthTargets') or []}
    n_st = 0
    for t in S.wsd.get('en_synthTargets') or []:
        zt = zh_by_key.get(t.get('imageKey')) or {}
        nm = (to_text(zt.get('name')) or to_text(t.get('name'))).replace('[Research]', '').strip()
        if not nm:
            continue
        n_st += 1
        locs = []
        for lo in (t.get('locations') or [])[:3]:
            f = to_text(lo.get('faction'))
            mis = re.sub(r'\s*\(Unconfirmed\)\s*', '', to_text(lo.get('mission'))).strip()
            locs.append('%s·%s（%s 级，%s，%s）' % (
                S.planet(to_text(lo.get('planet'))), mis,
                to_text(lo.get('level')), FACTION_ZH.get(f, f), S.mission(to_text(lo.get('type')))))
        B.append('- **%s**：%s' % (nm, '；'.join(locs) or '见游戏内扫描'))

    B += ['', '### 虚空裂隙分级（Void Fissure Tiers）', '']
    for k, v in (S.wsd.get('zh_fissureModifiers') or {}).items():
        if isinstance(v, dict) and v.get('value'):
            B.append('- %s（%s）' % (to_text(v['value']), k))

    B += ['', '### 集团声望商店（Syndicate Offerings）', '',
          '用集团声望（Standing）兑换的商品，按声望等级列出：', '']
    syn_data = (S.drop.get('all') or {}).get('syndicates') or S.drop.get('syndicates') or {}
    if isinstance(syn_data.get('syndicates'), dict):        # 独立文件多包了一层
        syn_data = syn_data['syndicates']

    for g in sorted(syn_data):
        if not isinstance(syn_data[g], list):
            continue
        zh = S.syndicate(g)                                  # 官方 ExportSyndicates → dict.zh（简体）
        byplace = OrderedDict()
        for it in syn_data[g] or []:
            if not isinstance(it, dict):
                continue
            place = to_text(it.get('place') or '')
            lvl = place.rsplit(',', 1)[-1].strip() if ',' in place else place
            byplace.setdefault(lvl, []).append(
                '%s（%s 声望）' % (S.item(to_text(it.get('item'))), trim_num(it.get('standing'), 0)))
        segs = ['%s：%s' % (lv, '；'.join(vs[:8]) + ('…' if len(vs) > 8 else ''))
                for lv, vs in byplace.items()]
        # 标题加「声望商店」后缀：避免与同名剧情任务冲突（例如任务 Vox Solaris 与集团 Vox Solaris 同名「索拉里斯之声」）
        B.append(entry('%s · 声望商店（%s）' % (zh, g), '- 声望商品：\n  - ' + '\n  - '.join(segs)))

    # ---------------- 三、星图节点与任务掉落表 ----------------
    B += ['', '## 三、星图节点与任务掉落表（Star Chart & Mission Rewards）', '',
          '按「星球 → 节点」列出每个节点的**任务类型、派系与掉落表**。'
          'A / B / C 为任务轮次（Rotation）：生存/防御/拦截等无尽任务按轮次轮换奖励，'
          '多数一次性任务只有 A 轮；每个轮次按稀有度降序最多列 6 项。'
          '括号内为 WFCD 数据表的掉落分级（普通 Common / 罕见 Uncommon / 稀有 Rare / 传奇 Legendary），'
          '**与 MOD 自身的稀有度不是一回事**，仅表示该奖励在此表中的档位。', '']
    node_idx = {}
    for r in I['Node']:
        node_idx[(norm_code(r.get('systemName')), norm_code(r.get('name')))] = r
    mr = (S.drop.get('all') or {}).get('missionRewards') or {}
    # 同一节点名可能出现在多个星球（如 Cephalon Capture）→ 标题加星球消歧
    node_planets = defaultdict(set)
    for _p, _ns in mr.items():
        for _n in _ns:
            node_planets[re.sub(r'\s*[（(][^）)]*[）)]\s*$', '',
                                to_text(_n)).strip().lower()].add(_p)
    RORDER = {'legendary': 0, 'rare': 1, 'uncommon': 2, 'common': 3}
    n_node = 0
    for planet in sorted(mr):
        ns = mr.get(planet) or {}
        for nodename in sorted(ns):
            nd = ns[nodename] or {}
            base = re.sub(r'\s*[（(][^）)]*[）)]\s*$', '', to_text(nodename)).strip()
            rec = node_idx.get((norm_code(planet), norm_code(base)))
            zn = to_text((S.zh_item.get(rec['uniqueName']) or {}).get('name')) if rec else ''
            if zn and zn != base:
                ttl = '%s（%s）' % (zn, nodename)
            elif len(node_planets.get(base.lower(), ())) > 1:
                ttl = '%s（%s）' % (nodename, S.planet(planet))
            else:
                ttl = nodename
            L = ['- 星球：%s（%s）' % (S.planet(planet), planet)]
            gm = to_text(nd.get('gameMode'))
            if gm:
                L.append('- 任务类型：%s（%s）' % (S.mission(gm), gm))
            fac = S.node_faction.get(base.lower())
            if fac:
                L.append('- 派系：%s' % FACTION_ZH.get(fac, fac))
            if rec:
                L.append('- 敌人等级：%s - %s' % (trim_num(rec.get('minEnemyLevel'), 0),
                                                 trim_num(rec.get('maxEnemyLevel'), 0)))
                if rec.get('masteryReq'):
                    L.append('- 精通等级需求 %s' % trim_num(rec['masteryReq'], 0))
            rw = nd.get('rewards')
            total = 0
            if isinstance(rw, dict):
                lines = []
                for rot in sorted(rw):
                    items = rw[rot] or []
                    if not isinstance(items, list) or not items:
                        continue
                    srt = sorted(items, key=lambda x: RORDER.get(
                        to_text(x.get('rarity')).lower(), 9))
                    seg, seen = [], set()
                    for it in srt:
                        rar = to_text(it.get('rarity'))
                        s = '%s %s（%s）' % (S.item(to_text(it.get('itemName'))),
                                            pct_raw(it.get('chance'), 2), RARITY_ZH.get(rar, rar))
                        if s in seen:
                            continue
                        seen.add(s)
                        seg.append(s)
                        total += 1
                        if len(seg) >= 6:
                            break
                    if seg:
                        lines.append('  - %s 轮：%s' % (rot, '｜'.join(seg)))
                if lines:
                    L.append('- 掉落：')
                    L += lines
            elif isinstance(rw, list) and rw:
                seg = ['%s %s' % (S.item(to_text(it.get('itemName'))), pct_raw(it.get('chance'), 2))
                       for it in rw[:10]]
                L.append('- 掉落：%s' % '｜'.join(seg))
                total = len(seg)
            if not total:
                continue
            n_node += 1
            B.append(entry(ttl, '\n'.join(L)))
    print('  星图节点条目: %d ；合成目标 %d' % (n_node, n_st))
    globals()['N_NODES'] = n_node
    globals()['N_SYNTH'] = n_st

    B += ['', '## 四、剧情任务（Quests）', '']
    for r in sorted(I['Quests'], key=lambda x: (x.get('name') or '').lower()):
        L = []
        d = S.zhdesc(r, 300)
        if d:
            L.append('- 说明：%s' % d)
        if r.get('drops'):
            L.append('- 奖励：%s' % drops_brief(r['drops'], 4))
        B.append(entry(zhname(r), '\n'.join(L) or '- 类型：剧情任务'))

    B += ['', '## 五、外观装饰与杂项', '',
          '> 外观（Skins / Cosmetics）按**类型**分组；浮印 / 徽章 / 杂项各自成条。以下为紧凑单行条目。', '']
    bytype = defaultdict(list)
    for r in I['Skins']:
        if not has_name(r):
            continue
        bytype[r.get('type') or 'Misc'].append(r)
    for t in sorted(bytype, key=lambda x: -len(bytype[x])):
        rs = sorted(bytype[t], key=lambda x: (x.get('name') or '').lower())
        # 再按英文名首字母分桶：避免单个 type（如 Misc 桶）长到几千行、分块后失去标题上下文
        buckets = OrderedDict()
        for r in rs:
            c = (r.get('name') or '?')[:1].upper()
            buckets.setdefault(c if 'A' <= c <= 'Z' else '其他', []).append(r)
        for b in sorted(buckets):
            lines = []
            for r in buckets[b]:
                zn = to_text((S.zh_item.get(r['uniqueName']) or {}).get('name')) or r['name']
                d = clean((S.zh_item.get(r['uniqueName']) or {}).get('description'), 110)
                lines.append('- %s%s%s' % (zn, ('（%s）' % r['name']) if zn != r['name'] else '',
                                           ('：' + d) if d else ''))
            B.append(entry('外观装饰 · %s · %s（%d 条）' % (t, b, len(lines)), '\n'.join(lines)))
    for label, key in (('浮印（Glyphs）', 'Glyphs'), ('徽章（Sigils）', 'Sigils'),
                       ('其他杂项条目（Misc）', 'Misc')):
        lines = []
        for r in sorted(I[key], key=lambda x: (x.get('name') or '').lower()):
            if not has_name(r):
                continue
            zn = to_text((S.zh_item.get(r['uniqueName']) or {}).get('name')) or r['name']
            d = clean((S.zh_item.get(r['uniqueName']) or {}).get('description'), 120)
            lines.append('- %s%s%s' % (zn, ('（%s）' % r['name']) if zn != r['name'] else '',
                                       ('：' + d) if d else ''))
        B.append(entry('%s（%d 条）' % (label, len(lines)), '\n'.join(lines)))
    intro = f"""# Warframe 知识库 · 术语、机制、星图、任务与装饰杂项

{NAV}

> 「其他」类目，含五部分：
> ① **伤害机制与术语对照表** —— 伤害类型分组 + 敌人「防护层 × 元素」修正矩阵（反推自 DE 伤害控制器）
>    + **伤害构成 / 状态效果全表 / 暴击 / 护甲减伤 / EHP**（整理自官方社区 Wiki） / 极性 / 稀有度 / 派系 / 遗物纪元与精炼 / 任务类型 / 集团 / 专精流派 / 九重天内源之力
> ② **核心机制** —— 执刑官源力石（Archon Shards）／钢铁之路商店／突击修正词／合成目标（{N_SYNTH} 个）／虚空裂隙分级／集团声望商店
> ③ **星图节点与任务掉落表**（{N_NODES} 个节点，含任务类型、派系、敌人等级与 A/B/C 轮次掉落）
> ④ **剧情任务 Quests**（{len(I['Quests'])} 条）
> ⑤ **外观装饰与杂项** —— 外观 {len(I['Skins'])}｜浮印 {len(I['Glyphs'])}｜徽章 {len(I['Sigils'])}｜杂项 {len(I['Misc'])}（此部分为紧凑单行条目）
> 译名：DE 官方简中（国际服）。普通星图节点官方保留英文（Pacific / Lith / Stephano 等），开放世界与特殊地标才有中文。
> ⚠ **机制数值边界**：从导出**可反推** = 伤害类型清单、防护层元素修正、敌人基础属性与基础等级；**反推不出** = 元素组合公式、状态 DoT 数值与层数、剥甲比例、**敌人等级缩放曲线**——这些来自人工整理（官方社区 Wiki，公式与数值），已在 ① 中补齐。"""
    return write_file('08_其他与机制术语.md', intro, '\n'.join(B))


# ================================================================== 09 成就 / 挑战 / 午夜电波
def build_achievements():
    A = P.get('Achievements') or {}
    ch = P.get('Challenges') or {}
    nw = P.get('Nightwave') or {}

    # ---------------- 一、成就 ----------------
    B = ['## 一、成就（Achievements）', '',
         '成就是个人档案中的长期目标，达成后在档案中点亮。下列按中文名排序。', '']
    ach, n_az, n_skip = [], 0, 0
    for k, v in A.items():
        if not isinstance(v, dict):
            continue
        zn = to_text(S.dz.get(v.get('name')))
        en = to_text(S.de.get(v.get('name')))
        dz_ = clean(S.dz.get(v.get('description')), 220)
        if not zn and not en and not dz_:
            n_skip += 1          # DE 内部隐藏占位项（{"hidden": true}）：无名称无描述，收录无意义
            continue
        if zn:
            n_az += 1
        ach.append((zn or en or k, en or k, dz_))
    n_ach = len(ach)
    for zn, en, dz_ in sorted(ach, key=lambda x: x[0].lower()):
        L = ['- 类型：成就（Achievement）']
        if dz_:
            L.append('- 达成条件：%s' % dz_)
        B.append(entry(title_of(zn, en), '\n'.join(L)))

    # ---------------- 二、挑战 ----------------
    nw_keys = set((nw.get('challenges') or {}).keys())
    B += ['', '## 二、挑战（Challenges）', '',
          '挑战是完成指定行为后给予奖励的任务。分为**星图进度挑战**、**1999 年历挑战**、'
          '**霍瓦尼亚（赤毒巫妖）挑战**等；**午夜电波赛季挑战**单独收录于下一节，此处不重复。', '']
    CATS = [('StarChart/', '星图进度挑战'), ('Calendar1999/', '1999 年历挑战'),
            ('Vania/', '霍瓦尼亚 · 赤毒巫妖挑战'), ('Seasons/', '午夜电波赛季挑战')]
    groups = OrderedDict((c[1], []) for c in CATS)
    groups['其他挑战'] = []
    n_ch = n_ch_skip = 0
    for k, v in ch.items():
        if k in nw_keys or not isinstance(v, dict):
            continue
        zn = to_text(S.dz.get(v.get('name')))
        en = to_text(S.de.get(v.get('name')))
        dz_ = clean(S.dz.get(v.get('description')), 200)
        if not zn and not en and not dz_:
            n_ch_skip += 1       # 同上：DE 内部隐藏占位项，无名称与描述
            continue
        lab = '其他挑战'
        for pref, nm in CATS:
            if k.startswith('/Lotus/Types/Challenges/' + pref):
                lab = nm
                break
        seg = k.rsplit('/', 1)[-1]
        groups[lab].append((zn or en or seg, en or seg, dz_, v.get('requiredCount')))
        n_ch += 1
    for lab, rows in groups.items():
        if not rows:
            continue
        B += ['#### %s（%d 条）' % (lab, len(rows)), '']
        for zn, en, dz_, rc in sorted(rows, key=lambda x: x[0].lower()):
            L = ['- 类型：挑战 · %s' % lab]
            if rc is not None:
                L.append('- 要求次数：%s' % trim_num(rc, 0))
            if dz_:
                L.append('- 说明：%s' % dz_)
            B.append(entry(title_of(zn, en), '\n'.join(L)))

    # ---------------- 三、午夜电波 ----------------
    nwc = nw.get('challenges') or {}
    rw = nw.get('rewards') or []
    B += ['', '## 三、午夜电波（Nightwave）', '',
          '午夜电波是长期轮换的免费奖励系统：完成**每日 / 每周 / 精英挑战**积攒午夜电波声望，'
          '提升等级解锁奖励；声望亦可兑换当季「诺拉的混选」贡品。', '']
    B += ['### 午夜电波 · 当前赛季标识', '',
          '- 赛季标识（affiliationTag）：%s' % (to_text(nw.get('affiliationTag')) or '—'), '']
    B += ['### 午夜电波挑战（%d 个）' % len(nwc), '',
          '每条含**声望值**与**要求次数**。描述中的 〈COUNT〉 即要求次数。', '']
    rows = []
    for k, v in nwc.items():
        if not isinstance(v, dict):
            continue
        zn = to_text(S.dz.get(v.get('name')))
        en = to_text(S.de.get(v.get('name'))) or k.rsplit('/', 1)[-1]
        rows.append((zn or en, en, clean(S.dz.get(v.get('description')), 180),
                     clean(S.dz.get(v.get('tip')), 170), v.get('standing'), v.get('required')))
    for zn, en, dz_, tip, st, rq in sorted(rows, key=lambda x: x[0].lower()):
        L = ['- 类型：午夜电波挑战']
        if st is not None:
            L.append('- 声望：%s' % trim_num(st, 0))
        if rq is not None:
            L.append('- 要求次数：%s' % trim_num(rq, 0))
        if dz_:
            L.append('- 描述：%s' % dz_)
        if tip:
            L.append('- 提示：%s' % tip)
        B.append(entry(title_of(zn, en), '\n'.join(L)))
    # 奖励清单按等级槽位重复（180 条里 156 条是同一种代币）→ 先按 uniqueName 去重
    uniq = OrderedDict()
    for r in rw:
        if not isinstance(r, dict):
            continue
        u = r.get('uniqueName') or ('#%d' % len(uniq))
        if u in uniq:
            uniq[u]['n'] += 1
        else:
            uniq[u] = {'r': r, 'n': 1}
    B += ['', '### 午夜电波奖励与贡品（去重后 %d 项 / 清单原始 %d 条）' % (len(uniq), len(rw)), '',
          '用午夜电波声望 / 诺拉的混选代币兑换。清单中同一种物品会按等级槽位重复出现，'
          '此处已去重并标注出现次数：', '']
    for e in uniq.values():
        r = e['r']
        zn = to_text(S.dz.get(r.get('name')))
        en = to_text(S.de.get(r.get('name'))) or to_text(r.get('uniqueName')).rsplit('/', 1)[-1]
        dz_ = clean(S.dz.get(r.get('description')), 200)
        L = ['- 类型：午夜电波奖励 / 贡品']
        if e['n'] > 1:
            L.append('- 清单中出现次数：×%s' % e['n'])
        if dz_:
            L.append('- 说明：%s' % dz_)
        B.append(entry(title_of(zn, en), '\n'.join(L)))
    intro = f"""# Warframe 知识库 · 成就、挑战与午夜电波

{NAV}

> 三部分：
> ① **成就 Achievements**（收录 **{n_ach}** 条，其中 **{n_az}** 条有 DE 官方简中名；另有 **{n_skip}** 条为 DE 内部隐藏占位项、无名称与描述，未收录）
> ② **挑战 Challenges**（收录 **{n_ch}** 条，已剔除与午夜电波赛季重复的条目；另有 **{n_ch_skip}** 条为 DE 内部隐藏占位项，未收录）
> ③ **午夜电波 Nightwave**（当前赛季挑战 **{len(nwc)}** 个 + 奖励 / 贡品去重后 **{len(uniq)}** 项）
> 每条格式：**中文名（English）**｜类型｜达成条件 / 说明。
> 文案来自 **DE 官方简中**；〈COUNT〉等 〈〉 内为游戏内变量占位符。"""
    return write_file('09_成就挑战与午夜电波.md', intro, '\n'.join(B))


# ================================================================== 10 图鉴 / 赏金 / 保育 / 商人 / 风味
BOUNTY_AREA = OrderedDict([
    ('Eidolon', '夜灵平原（希图斯）'), ('Venus', '奥布山谷（福尔图娜）'),
    ('Deimos', '火卫二（殁世幽都）'), ('Zariman', '扎里曼号'), ('Duviri', '双衍王境'),
    ('Hollvania', '霍瓦尼亚'), ('Höllvania', '霍瓦尼亚'), ('Cetus', '希图斯'),
    ('Solaris', '福尔图娜'), ('Entrati', '殁世幽都'),
])
FLAVOUR_GROUP = OrderedDict([
    ('StoreItems/AvatarImages', '个人档案浮印图（Avatar Images）'),
    ('Items/Titles', '称号（Titles）'), ('Items/Emotes', '表情动作（Emotes）'),
    ('Game/KubrowPet', '库狛装饰（Kubrow）'), ('Game/CatbrowPet', '库娃装饰（Catbrow）'),
    ('Skins/Liset', 'Lisets 登陆艇外观'), ('StoreItems/SuitCustomizations', '战甲自定义装饰'),
    ('Graphics/CustomUI', '界面背景与主题（Custom UI）'),
    ('Game/QuartersWallpapers', '居所壁纸'), ('Game/ActionFigureDioramas', '可动模型场景'),
    ('Game/NotePacks', '乐谱包'), ('Game/PoseSets', '姿态组'),
    ('Items/VideoWallSoundscapes', '影像墙音景'), ('Game/ShipScenes', '飞船场景'),
    ('Items/Arcade', '街机小游戏'), ('Items/Events', '活动相关'),
    ('Items/VideoWallBackdrops', '影像墙背景'),
])


def build_codex_life():
    cx = P.get('Codex') or {}
    bo = P.get('Bounties') or {}
    an = P.get('Animals') or {}
    vd = P.get('Vendors') or {}
    fl = P.get('Flavour') or {}
    B = []

    # ---------------- 一、图鉴与背景故事 ----------------
    objs = cx.get('objects') or {}
    lf = cx.get('loreFragments') or {}
    songs = cx.get('songs') or {}
    ff = cx.get('fighterFrames') or {}
    B += ['## 一、图鉴与背景故事（Codex / Lore）', '',
          '来自 DE Codex 导出：**造物图鉴**（可扫描的场景物件与机关）、**资料片段**、'
          '**音乐片段**、**战甲霸王资料**。', '']

    B += ['### 造物图鉴（Objects，%d 条）' % len(objs), '',
          '即任务中「扫描」的目标：扫描指定次数后解锁图鉴条目。', '']
    rows = []
    for k, v in objs.items():
        if not isinstance(v, dict):
            continue
        zn = to_text(S.dz.get(v.get('name')))
        en = to_text(S.de.get(v.get('name'))) or k.rsplit('/', 1)[-1]
        rows.append((zn or en, en, clean(S.dz.get(v.get('description')), 220),
                     v.get('reqScans'), v.get('secret')))
    for zn, en, dz_, rs, sec in sorted(rows, key=lambda x: x[0].lower()):
        L = ['- 类型：造物图鉴（可扫描目标）']
        if rs is not None:
            L.append('- 扫描需求：%s 次' % trim_num(rs, 0))
        if sec:
            L.append('- 隐藏条目（需先满足条件才会出现）')
        if dz_:
            L.append('- 说明：%s' % dz_)
        B.append(entry(title_of(zn, en), '\n'.join(L)))

    B += ['### 资料片段（Lore Fragments，%d 条）' % len(lf), '',
          '集齐后解锁背景故事与隐藏通讯（Cephalon 碎片 / Ordis 记忆等）。', '']
    rows = []
    for k, v in lf.items():
        if not isinstance(v, dict):
            continue
        zn = to_text(S.dz.get(v.get('name')))
        en = to_text(S.de.get(v.get('name'))) or k.rsplit('/', 1)[-1]
        rows.append((zn or en, en, clean(S.dz.get(v.get('description')), 400), v.get('reqScans')))
    for zn, en, dz_, rs in sorted(rows, key=lambda x: x[0].lower()):
        L = ['- 类型：资料片段（Lore Fragment）']
        if rs is not None:
            L.append('- 扫描需求：%s 次' % trim_num(rs, 0))
        if dz_:
            L.append('- 内容：%s' % dz_)
        B.append(entry(title_of(zn, en), '\n'.join(L)))

    rows = []
    for k, v in songs.items():
        if not isinstance(v, dict):
            continue
        zn = to_text(S.dz.get(v.get('name')))
        en = to_text(S.de.get(v.get('name'))) or k.rsplit('/', 1)[-1]
        rows.append((zn or en, en, v.get('reqScans')))
    B += ['### 音乐片段（Songs / 三线琴谱，%d 条）' % len(rows), '']
    for zn, en, rs in sorted(rows, key=lambda x: x[0].lower()):
        B.append('- %s｜扫描需求 %s 次' % (title_of(zn, en), trim_num(rs, 0)))

    rows = []
    for k, v in ff.items():
        if not isinstance(v, dict):
            continue
        zn = to_text(S.dz.get(v.get('name')))
        en = to_text(S.de.get(v.get('name'))) or k.rsplit('/', 1)[-1]
        rows.append((zn or en, en, to_text(v.get('suit')).rsplit('/', 1)[-1], v.get('reqScans')))
    B += ['', '### 战甲霸王资料（Frame Fighter Data，%d 条）' % len(rows), '']
    for zn, en, su, rs in sorted(rows, key=lambda x: x[0].lower()):
        B.append('- %s｜对应战甲 %s｜扫描 %s 次' % (title_of(zn, en), su, trim_num(rs, 0)))

    # ---------------- 二、赏金任务 ----------------
    byarea = OrderedDict()
    for k, v in bo.items():
        if not isinstance(v, dict):
            continue
        seg = k.split('/')
        byarea.setdefault(seg[4] if len(seg) > 4 else '其他', []).append((k, v))
    B += ['', '## 二、赏金任务（Bounties）', '',
          '开放世界可重复的**赏金任务**：由多个**阶段（stage）**组成，完成后按轮次发放奖励。'
          '按发布区域分组。', '']
    for area, rows in byarea.items():
        lab = BOUNTY_AREA.get(area, area)
        B += ['#### %s（%d 条）' % (lab, len(rows)), '']
        for k, v in sorted(rows):
            zn = to_text(S.dz.get(v.get('name')))
            en = to_text(S.de.get(v.get('name'))) or k.rsplit('/', 1)[-1]
            L = ['- 类型：赏金任务（Bounty）｜区域 %s' % lab]
            st = v.get('stages')
            if isinstance(st, list):
                L.append('- 阶段数：%s' % len(st))
            dz_ = clean(S.dz.get(v.get('description')), 240)
            if dz_:
                L.append('- 说明：%s' % dz_)
            B.append(entry(title_of(zn or en, en), '\n'.join(L)))

    # ---------------- 三、保育动物 ----------------
    seen = OrderedDict()
    for k, v in an.items():
        if not isinstance(v, dict):
            continue
        zn = to_text(S.dz.get(v.get('name')))
        en = to_text(S.de.get(v.get('name'))) or k.rsplit('/', 1)[-1]
        cons = v.get('conservation') or {}
        tag = to_text(cons.get('itemReward'))
        key = (zn, en, tag)
        r = seen.get(key)
        if r is None:
            seen[key] = {'zh': zn, 'en': en, 'd': clean(S.dz.get(v.get('description')), 260),
                         'hp': v.get('health'), 'xp': v.get('killXPReward'),
                         'tag': tag, 'st': cons.get('standingReward'), 'n': 1}
        else:
            r['n'] += 1
    B += ['', '## 三、保育动物（Conservation）', '',
          '开放世界**保育**玩法中的可捕捉动物。同种动物的「常见 / 稀有 / 雌 / 雄」变体'
          '资产名不同但属同一物种，此处按**物种去重**。', '']
    for (zn, en, tag), r in sorted(seen.items(), key=lambda kv: (kv[1]['zh'] or kv[1]['en']).lower()):
        L = ['- 类型：保育动物（Conservation）']
        stt = []
        if r['hp'] is not None:
            stt.append('生命 %s' % trim_num(r['hp'], 0))
        if r['xp']:
            stt.append('击杀经验 %s' % trim_num(r['xp'], 0))
        if r['n'] > 1:
            stt.append('共 %d 个变体资产' % r['n'])
        if stt:
            L.append('- 属性：%s' % '｜'.join(stt))
        if r['tag']:
            L.append('- 保育标签：%s' % (S.name(r['tag']) or r['tag'].rsplit('/', 1)[-1]))
        if r['st'] is not None:
            L.append('- 声望奖励：%s' % trim_num(r['st'], 0))
        if r['d']:
            L.append('- 说明：%s' % r['d'])
        B.append(entry(title_of(r['zh'] or r['en'], r['en']), '\n'.join(L)))

    # ---------------- 四、商人库存 ----------------
    bygrp = OrderedDict()
    for k, v in vd.items():
        if not isinstance(v, dict):
            continue
        seg = k.split('/')
        bygrp.setdefault(seg[5] if len(seg) > 5 else '其他', []).append((k, v))
    B += ['', '## 四、商人库存（Vendors / Offerings）', '',
          '各**商人 / 兑换处**的库存清单（含价格与轮换档位）。'
          '标注「常驻」的为固定商品，其余为轮换或按概率出现；价格为所需兑换材料。', '']
    for grp, rows in bygrp.items():
        B += ['#### %s（%d 个库存清单）' % (grp, len(rows)), '']
        for k, v in sorted(rows):
            nm = k.rsplit('/', 1)[-1]
            L = ['- 类型：商人库存清单（Vendor）｜分组 %s' % grp]
            if v.get('isDynamic'):
                L.append('- 动态库存（内容 / 价格随轮换变化）')
            items = v.get('items') or []
            if isinstance(items, list) and items:
                L.append('- 库存商品（%d 项）：' % len(items))
                for it in items[:40]:
                    if not isinstance(it, dict):
                        continue
                    sn = S.name(it.get('storeItem')) or to_text(it.get('storeItem')).rsplit('/', 1)[-1]
                    parts = ['%s ×%s' % (sn, trim_num(it.get('quantity'), 0))]
                    if it.get('alwaysOffered'):
                        parts.append('常驻')
                    elif it.get('bin'):
                        parts.append('第 %s 档' % trim_num((it.get('bin') or 0) + 1, 0))
                    if it.get('probability') is not None:
                        parts.append('概率 %s' % pct_frac(it.get('probability'), 0))
                    pr = []
                    for p in (it.get('itemPrices') or []):
                        if isinstance(p, dict):
                            pn = S.name(p.get('ItemType')) or to_text(p.get('ItemType')).rsplit('/', 1)[-1]
                            pr.append('%s ×%s' % (pn, trim_num(p.get('ItemCount'), 0)))
                    pl = it.get('platinum')
                    if isinstance(pl, dict):
                        pr.append('%s–%s 铂金' % (trim_num(pl.get('minValue'), 0),
                                                 trim_num(pl.get('maxValue'), 0)))
                    if pr:
                        parts.append('价格 ' + ' + '.join(pr))
                    L.append('  - ' + '｜'.join(parts))
            rp = v.get('randomItemPricesPerBin')
            if isinstance(rp, list) and rp:
                for bi, b in enumerate(rp):
                    comp = []
                    for p in (b or []):
                        if isinstance(p, dict):
                            pn = S.name(p.get('type')) or to_text(p.get('type')).rsplit('/', 1)[-1]
                            c = p.get('count') or {}
                            comp.append('%s ×%s–%s' % (pn, trim_num(c.get('minValue'), 0),
                                                       trim_num(c.get('maxValue'), 0)))
                    if comp:
                        L.append('  - 第 %d 档随机定价：%s' % (bi + 1, ' + '.join(comp)))
            B.append(entry('商人库存 · %s · %s' % (grp, nm), '\n'.join(L)))

    # ---------------- 五、界面风味文本 ----------------
    byg = OrderedDict()
    for k, v in fl.items():
        if not isinstance(v, dict):
            continue
        p = k.split('/')
        g = '/'.join(p[3:5]) if len(p) > 4 else ('/'.join(p[3:]) or '其他')
        byg.setdefault(g, []).append((k, v))
    B += ['', '## 五、界面风味文本（Flavour）', '',
          'DE 导出的界面 / 商城文案（个人档案浮印图、称号、表情、装饰、界面背景等）。'
          '**此部分为紧凑单行条目，问答价值较低**，主要为「游戏里有没有这件东西」提供名称对照。', '']
    for g, rows in sorted(byg.items(), key=lambda kv: -len(kv[1])):
        lab = FLAVOUR_GROUP.get(g, g)
        lines = []
        for k, v in sorted(rows):
            zn = to_text(S.dz.get(v.get('name')))
            en = to_text(S.de.get(v.get('name'))) or k.rsplit('/', 1)[-1]
            seg = [title_of(zn or en, en)]
            pc = v.get('platinumCost')
            if pc:
                seg.append('%s 铂金' % trim_num(pc, 0))
            d = clean(S.dz.get(v.get('description')), 70)
            if d:
                seg.append(d)
            lines.append('- ' + '｜'.join(seg))
        B.append(entry('界面风味 · %s（%d 条）' % (lab, len(lines)), '\n'.join(lines)))

    # ---------------- 六、Kuva 巫妖与帕尔沃斯姐妹（系统索引）----------------
    kw_rows = []
    tenet_rows = []
    for _k in ('Primary', 'Secondary', 'Melee', 'Arch-Gun', 'Arch-Melee'):
        for r in I.get(_k, []):
            _n = r.get('name') or ''
            if 'kuva' in _n.lower():
                kw_rows.append(zhname(r))
            if _n.lower().startswith('tenet'):
                tenet_rows.append(zhname(r))
    REQ_SET = ('Fass', 'Jahu', 'Khra', 'Lohk', 'Netra', 'Oull', 'Ris', 'Vome', 'Xata')
    req_mods = [r.get('name') for r in I['Mods'] if (r.get('name') or '') in REQ_SET]
    req_rel = sorted({re.sub(r'\s+(Intact|Exceptional|Flawless|Radiant)$', '',
                             r.get('name') or '')
                      for r in I['Relics'] if 'Requiem' in (r.get('name') or '')})
    toks = []
    for _k, _v in (P.get('Resources') or {}).items():
        if 'Nemesis/KuvaLich' in _k and 'Token' in _k:
            zn = to_text(S.dz.get(_v.get('name')))
            if zn and zn not in toks:
                toks.append(zn)
    B += ['', '## 六、Kuva 巫妖与帕尔沃斯姐妹（玄骸系统索引）', '',
          '> ⚠ 本节只是**把散落在各文件里的玄骸系统实体聚成一页索引**，方便一问即答。'
          '**系统机制规则（元素加成百分比、Valence 融合规则、Requiem 序列判定）属于引擎逻辑，'
          '数据包里没有，无法反推**，需以官方 Wiki 为准。', '']
    B.append(entry('玄骸系统概览 · Kuva 巫妖 / 帕尔沃斯姐妹 / 科技细胞终幕者',
                   '\n'.join([
                       '- 类型：敌对玄骸系统（Nemesis）',
                       '- 赤毒巫妖（Kuva Lich）：Grineer 系，来自赤毒要塞；对应武器为「**赤毒**」前缀武器',
                       '- 帕尔沃斯姐妹（Sisters of Parvos）：Corpus 系，来自帕尔沃斯·格拉努姆；对应「**信条**」前缀武器（Tenet）',
                       '- 科技细胞终幕者（Coda）：Infested 系，来自霍瓦尼亚（1999）',
                       '- 关键道具：安魂通牒（NemesisBait，直接引出玄骸对峙）｜元素恶癖（ValenceAdapter，改变对手武器的元素伤害类型）',
                       '- 机制数值（元素加成 % / 融合规则 / 安魂序列）：**数据包内无字段，不可反推**'])))
    if kw_rows:
        B.append(entry('赤毒武器清单（Kuva Weapons，%d 件）' % len(kw_rows),
                       '- 类型：玄骸武器（赤毒 / Kuva）\n- 武器：%s\n'
                       '- 详见本知识库 **02 号武器文件**（含逐件数值、紫卡倾向、获取）' % '｜'.join(sorted(kw_rows))))
    if tenet_rows:
        B.append(entry('信条武器清单（Tenet Weapons，%d 件）' % len(tenet_rows),
                       '- 类型：帕尔沃斯姐妹武器（信条 / Tenet）\n- 武器：%s\n'
                       '- 详见本知识库 **02 号武器文件**（含逐件数值、紫卡倾向、获取）' % '｜'.join(sorted(tenet_rows))))
    if req_mods:
        B.append(entry('安魂 MOD（Requiem Mods，%d 个）' % len(req_mods),
                       '- 类型：安魂 MOD（用于赤毒玄骸 / 帕尔沃斯姐妹的序列判定）\n- MOD：%s\n'
                       '- 获取：安魂纪元遗物（Requiem Relic）；详见 **03 号 MOD 文件**'
                       % '、'.join(sorted(req_mods))))
    if req_rel:
        B.append(entry('安魂纪元遗物（Requiem Relics，%d 种）' % len(req_rel),
                       '- 类型：安魂纪元遗物（产出安魂 MOD 与赤毒）\n- 遗物：%s\n'
                       '- 详见 **04 号遗物文件**' % '｜'.join(req_rel)))
    if toks:
        B.append(entry('玄骸印记与关键资源',
                       '- 类型：玄骸系统交易 / 进度物品\n' +
                       '\n'.join('- %s' % t for t in toks) +
                       '\n- 「赤毒（Kuva）」为玄骸系统的核心资源（洗练紫卡亦消耗赤毒）'))
    kp_rows = []
    for k, v in S.dz.items():
        if '/Kingpins/' in k and isinstance(v, str):
            kp_rows.append((k.rsplit('/', 1)[-1], v))
    if kp_rows:
        B.append(entry('玄骸系统官方文案术语（Kingpins）',
                       '- 类型：玄骸系统术语（DE 官方简中）\n' +
                       '\n'.join('- %s：%s' % (n, clean(d, 150)) for n, d in sorted(kp_rows))))

    intro = f"""# Warframe 知识库 · 图鉴、赏金、保育、商人与风味文本

{NAV}

> 六部分：
> ① **图鉴与背景故事** —— 造物图鉴 **{len(objs)}**｜资料片段 **{len(lf)}**｜音乐片段 **{len(songs)}**｜战甲霸王资料 **{len(ff)}**
> ② **赏金任务 Bounties**（**{len(bo)}** 条，按区域分组）
> ③ **保育动物 Conservation**（按物种去重后 **{len(seen)}** 种）
> ④ **商人库存 Vendors**（**{len(vd)}** 个库存清单，含价格与轮换档位）
> ⑤ **界面风味文本 Flavour**（**{len(fl)}** 条，紧凑单行）
> ⑥ **Kuva 巫妖与帕尔沃斯姐妹（玄骸系统索引）** —— 赤毒武器 **{len(kw_rows)}**｜信条武器 **{len(tenet_rows)}**｜安魂 MOD **{len(req_mods)}**｜安魂遗物 **{len(req_rel)}** 种｜印记与术语
> 名称与文案优先使用 **DE 官方简中**；未收录时回退英文原文。
> ⚠ 玄骸系统的**机制规则**（元素加成 %、Valence 融合、安魂序列）无数据字段、不可反推，仅收录可导出的实体清单。"""
    return write_file('10_图鉴与生态.md', intro, '\n'.join(B))


# ================================================================== main
def main():
    print('构建中…')
    files = [
        ('01_战甲.md', build_warframes()),
        ('02_武器.md', build_weapons()),
        ('03_MOD与赋能.md', build_mods()),
        ('04_遗物.md', build_relics()),
        ('05_敌人.md', build_enemies()),
        ('06_资源与蓝图.md', build_resources()),
        ('07_同伴与空战.md', build_companions()),
        ('08_其他与机制术语.md', build_others()),
        ('09_成就挑战与午夜电波.md', build_achievements()),
        ('10_图鉴与生态.md', build_codex_life()),
    ]
    # 剔除旧编号产物：重编号后旧文件（02/03/04 武器、05~10 旧名）必须删除，
    # 否则会超出 AstrBot「单次最多 10 个文件」的上限。
    keep = {fn for fn, _ in files}
    stale = sorted(f for f in os.listdir(OUTDIR) if f.endswith('.md') and f not in keep)
    for f in stale:
        os.remove(os.path.join(OUTDIR, f))
    if stale:
        print('  清理旧编号文件 %d 个：%s' % (len(stale), '、'.join(stale)))
    stats = {k: v for k, v in S._stats.items()}
    rep = []
    tot = 0
    for fn, n in files:
        txt = open(os.path.join(OUTDIR, fn), encoding='utf-8').read()
        ents = split_entries(txt)
        lens = sorted(len(e) for e in ents) or [0]
        bl = [len(l) for l in txt.split('\n') if l.startswith('- ')] or [0]
        over = sum(1 for l in lens if l > 512)
        tcount = Counter(re.findall(r'^### (.+)$', txt, re.M))
        ndup = sum(v - 1 for v in tcount.values() if v > 1)
        tot += n
        rep.append(dict(file=fn, chars=n, kb=round(n / 1024.0, 1), lines=txt.count('\n') + 1,
                        entries=len(ents),
                        le512=round(100.0 * (len(lens) - over) / len(lens), 1),
                        median=lens[len(lens) // 2], p90=lens[int(len(lens) * 0.9)],
                        max_entry=lens[-1], max_line=max(bl), dup_titles=ndup))
    out = dict(name_stats=stats, files=rep, total_chars=tot, total_kb=round(tot / 1024.0, 1))
    with open(os.path.join(WORKDIR, 'build_report.json'), 'w',
              encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print('耗时 %.1fs' % (time.time() - T0))


if __name__ == '__main__':
    import traceback
    try:
        main()
        open(os.path.join(WORKDIR, 'build_status.txt'), 'w',
             encoding='utf-8').write('OK')
    except Exception:
        open(os.path.join(WORKDIR, 'build_status.txt'), 'w',
             encoding='utf-8').write(traceback.format_exc())
        raise
