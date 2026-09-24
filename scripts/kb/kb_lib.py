# -*- coding: utf-8 -*-
"""
Warframe 知识库构建 · 公共层
============================
数据源：
  A) WFCD/warframe-items                        → 已渲染英文明文 + 掉落/配方/抗性等衍生字段
  B) calamity-inc/warframe-public-export-plus   → DE 官方扁平导出 + dict.<lang> 本地化字典

职责：加载缓存、统一名称解析（5 级回落）、文本清洗与 MOD 效果术语规范化。
"""
import json, os, re
from collections import Counter, OrderedDict

# ---------------------------------------------------------------------------
# 目录约定（环境变量，2026-09-21 阶段 2 去硬编码）：
#   WF_KB_DATA  解包后的数据目录（原「_work/data」），必填，缺失即报错退出
#   WF_KB_OUT   知识库产物目录，可省 —— 缺省取 WF_KB_DATA 上两级的「知识库/」
#   WF_KB_SRC   四个数据包 zip 所在目录，仅 extract*.py 需要
# ---------------------------------------------------------------------------


def _env_dir(var, what):
    v = (os.environ.get(var) or "").strip()
    if not v:
        raise SystemExit("[kb] 必须先设置环境变量 %s（%s 的绝对路径）再运行" % (var, what))
    return v


def kb_data_dir():
    return _env_dir("WF_KB_DATA", "解包数据目录 _work/data")


def kb_src_dir():
    return _env_dir("WF_KB_SRC", "数据包 zip 所在目录")


def kb_out_dir():
    v = (os.environ.get("WF_KB_OUT") or "").strip()
    if v:
        return v
    d = kb_data_dir().rstrip("/\\")
    return os.path.join(os.path.dirname(os.path.dirname(d)), "知识库")


def find_zip(prefix):
    """在 WF_KB_SRC 里找 prefix 开头的 zip;多版本共存取修改时间最新。"""
    import glob
    src = kb_src_dir()
    cands = sorted(glob.glob(os.path.join(src, prefix + "*.zip")),
                   key=os.path.getmtime, reverse=True)
    if not cands:
        raise SystemExit("[kb] 在 %s 找不到 %s*.zip" % (src, prefix))
    return cands[0]


def zip_root(zip_path):
    """取 zip 内部顶层目录前缀(如 warframe-items-master/)——兼容任意版本号打包。"""
    import zipfile as _zf
    with _zf.ZipFile(zip_path) as z:
        first = z.namelist()[0]
    return first.split("/")[0] + "/"


DATA = kb_data_dir()
IDIR = os.path.join(DATA, 'items')
PDIR = os.path.join(DATA, 'pep')
CDIR = os.path.join(DATA, 'cfg')


def jload(path):
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


# ---------------------------------------------------------------- 补充源用词表
def norm_code(s):
    return re.sub(r'[^a-z0-9]', '', (s or '').lower())


_QTY_RE = re.compile(r'^([0-9,]+)\s*[xX]?\s+(.+)$')
_RELIC_RE = re.compile(r'^(Lith|Meso|Neo|Axi|Requiem|Vanguard)\s+([A-Za-z0-9]+)\s+Relic$', re.I)
# 星球：优先手写表（/Lotus/Language/Locations/Lua 会错误映射到节点 Cervantes，不能直接用）
PLANET_ZH = {
    'Mercury': '水星', 'Venus': '金星', 'Earth': '地球', 'Mars': '火星', 'Jupiter': '木星',
    'Saturn': '土星', 'Uranus': '天王星', 'Neptune': '海王星', 'Pluto': '冥王星',
    'Ceres': '谷神星', 'Eris': '阋神星', 'Sedna': '赛德娜', 'Europa': '欧罗巴',
    'Void': '虚空', 'Phobos': '火卫一', 'Deimos': '火卫二', 'Lua': '月球',
    'Kuva Fortress': '赤毒要塞', 'Sanctuary': '圣殿', 'Veil Proxima': '面纱比邻星域',
    'Zariman': '扎里曼', 'Duviri': '双衍王境', 'Höllvania': '霍瓦尼亚',
    'Dark Refractory, Deimos': '火卫二·暗黑折跃', 'Derelict': '奥罗金遗迹船',
}
# gameMode → MissionName_* 代码（官方字典里代码名与显示名不同的）
MISSION_ALIAS = {
    'interception': 'territory', 'defection': 'evacuation', 'conclave': 'pvp',
    'void flood': 'corruption', 'void cascade': 'voidcascade',
    'void armageddon': 'armageddon', 'sanctuary onslaught': 'endlessextermination',
    'the perita rebellion': 'tauwar', "follie's hunt": 'paintflood',
    'rush': 'race', 'legacyte harvest': 'endlesscapture',
    'infested salvage': 'purify',
    'extermination': 'exterminate', 'hijack': 'retrieval',
    'disruption': 'artifact', 'mirror defense': 'dualdefense',
}
# 官方字典里查不到的（WFCD 自己的说法）
MISSION_EXTRA_ZH = {
    'skirmish': '遭遇战', 'caches': '储藏箱', 'hard': '困难（钢铁之路）', 'normal': '普通',
    'shrine defense': '神殿防御', 'the circuit': '环形装置',
}
PART_ZH = {'Blueprint': '蓝图', 'Barrel': '枪管', 'Receiver': '机匣', 'Stock': '枪托',
           'Handle': '握柄', 'Blade': '刀身', 'Hilt': '刀柄', 'Systems': '系统',
           'Chassis': '机体', 'Neuroptics': '头部神经光元', 'Link': '连接器',
           'Carapace': '甲壳', 'Cerebrum': '脑核', 'Head': '刃首', 'String': '弓弦',
           'Grip': '握把', 'Disc': '碟片', 'Pouch': '弹药袋', 'Star': '星核',
           'Barrels': '枪管', 'Engines': '引擎', 'Avionics': '航电系统', 'Reactor': '反应堆'}
ITEM_FIXED_ZH = {'Endo': '内融核心', 'Credits Cache': '现金匣', 'Ayatan Amber Star': '阿耶檀识琥珀星',
                  'Kuva': '赤毒', 'Riven Sliver': '裂罅碎片', 'Steel Essence': '钢铁精华'}
ERA_ZH = {'Lith': '古纪', 'Meso': '前纪', 'Neo': '中纪', 'Axi': '后纪',
          'Requiem': '安魂', 'Vanguard': '先锋'}
# drop-data 的集团显示名（18 个）→ ExportSyndicates 的 uniqueName 末段。
# 必须精确映射：子串回退会把 Vox Solaris 误配到 Solaris United（两者都含 solaris）。
SYNDICATE_ALIAS = {
    'arbitersofhexis': 'arbiters', 'cephalonsimaris': 'library',
    'cephalonsuda': 'cephalonsuda', 'conclave': 'conclave',
    'entrati': 'entrati', 'kahlsgarrison': 'kahl', 'necraloid': 'necraloid',
    'newloka': 'newloka', 'operationalsupply': 'event', 'ostron': 'cetus',
    'redveil': 'redveil', 'solarisunited': 'solaris', 'steelmeridian': 'steelmeridian',
    'holdfasts': 'zariman', 'perrinsequence': 'perrin', 'quills': 'quills',
    'ventkids': 'ventkids', 'voxsolaris': 'vox',
}
# WFCD 的部分中文数据是繁体，补一张繁→简映射（只覆盖本数据集出现的字）
T2S = str.maketrans({
    '戰': '战', '為': '为', '並': '并', '復': '复', '會': '会', '動': '动', '衝': '冲',
    '敵': '敌', '強': '强', '護': '护', '蝕': '蚀', '觸': '触', '發': '发', '將': '将',
    '減': '减', '議': '议', '務': '务', '類': '类', '換': '换', '彈': '弹', '槍': '枪',
    '擊': '击', '殲': '歼', '滅': '灭', '獲': '获', '壞': '坏', '雙': '双', '羅': '罗',
    '鬩': '阋', '遺': '遗', '跡': '迹', '穀': '谷', '歐': '欧', '開': '开', '蟲': '虫',
    '機': '机', '藥': '药', '鏡': '镜', '攔': '拦', '諜': '谍', '禦': '御',
    '資': '资', '襲': '袭', '殺': '杀', '斷': '断', '間': '间', '環': '环', '帶': '带',
    '數': '数', '傷': '伤', '溫': '温', '範': '范', '備': '备', '準': '准', '對': '对',
    '應': '应', '單': '单', '費': '费', '現': '现', '實': '实', '無': '无', '與': '与',
    '這': '这', '個': '个', '們': '们', '時': '时', '從': '从', '來': '来', '後': '后',
    '長': '长', '門': '门', '處': '处', '場': '场', '點': '点', '種': '种', '順': '顺',
    '總': '总', '結': '结', '統': '统', '絕': '绝', '對': '对', '於': '于', '較': '较',
    '還': '还', '過': '过', '進': '进', '運': '运', '選': '选', '釋': '释', '銀': '银',
    '錯': '错', '錢': '钱', '鐵': '铁', '關': '关', '隊': '队', '難': '难', '靈': '灵',
    '靜': '静', '響': '响', '頁': '页', '風': '风', '飛': '飞', '飯': '饭', '體': '体',
    '驗': '验', '鹽': '盐', '黃': '黄', '黨': '党', '齊': '齐', '龍': '龙', '龜': '龟',
    # 集团名相关（WSD 的 zh 版是繁体：中樞蘇達 / 佩蘭數列 / 鋼鐵防線 / 新世間 / 通風小子 / 集團）
    '蘭': '兰', '樞': '枢', '蘇': '苏', '鋼': '钢', '線': '线', '團': '团', '聲': '声',
    '聯': '联', '議': '议', '護': '护', '衛': '卫', '營': '营',
    # 突击首领名（WSD zh_sortieData：墮落 Vor）
    '墮': '堕', '墜': '坠', '隕': '陨',
})


def t2s(s):
    return to_text(s).translate(T2S).replace('︰', '：')


# ---------------------------------------------------------------- 文本
def to_text(v):
    """DE 导出里同名字段可能是 str / list / dict，统一成 str。"""
    if v is None:
        return ''
    if isinstance(v, list):
        return ' '.join(x for x in (to_text(i) for i in v) if x)
    if isinstance(v, dict):
        for k in ('value', 'name', 'text', 'desc'):
            if v.get(k) is not None:
                return to_text(v[k])
        return ''
    return str(v)


_TAG_PAIR = re.compile(r'<([A-Za-z_][A-Za-z_0-9]*)>(.*?)</\1>', re.S)
_TAG_ANY = re.compile(r'<[^>]{1,32}>')
_MACRO = re.compile(r'\|([A-Za-z_][A-Za-z_0-9]*)\|')
# ---- DE 伤害类型标签 <DT_X> / <DT_X_COLOR> ----
# 约定（已用 dict.zh 全量核对）：
#   · `<DT_X_COLOR>` 是**纯颜色标签**，紧跟其后的汉字就是伤害名 → 整段删除；
#   · `<DT_X>` 无 _COLOR 时**二义**：
#       - 若紧跟汉字（如 `<DT_CORROSIVE>腐蚀伤害`）→ 冗余颜色标签 → 删除；
#       - 若后面不是汉字（如「由 <DT_ELECTRICITY> 和 <DT_POISON> 组合而成」）→ 标签本身即名称 → 替换。
#   不处理的话，元素组合提示会退化成「由  和  组合而成」。
_DT_TAG = re.compile(r'<DT_([A-Z_]+?)>')
_DT_ZH = {
    'IMPACT': '冲击', 'PUNCTURE': '穿刺', 'SLASH': '切割',
    'FIRE': '火焰', 'HEAT': '火焰', 'FREEZE': '冰冻', 'COLD': '冰冻',
    'ELECTRICITY': '电击', 'POISON': '毒素', 'TOXIN': '毒素',
    'EXPLOSION': '爆炸', 'BLAST': '爆炸', 'RADIATION': '辐射', 'GAS': '毒气',
    'MAGNETIC': '磁力', 'VIRAL': '病毒', 'CORROSIVE': '腐蚀',
    'RADIANT': '虚空', 'VOID': '虚空', 'TAU': 'Tau', 'SENTIENT': 'Tau',
    'TRUE': '真实', 'CINEMATIC': '剧情',
    'SHIELD_DRAIN': '护盾吸取', 'HEALTH_DRAIN': '生命吸取', 'ENERGY_DRAIN': '能量吸取',
}


def _replace_dt(m):
    key = m.group(1)
    if key.endswith('_COLOR'):
        return ''
    nxt = m.string[m.end():m.end() + 1]
    if nxt and '\u4e00' <= nxt <= '\u9fff':
        return ''
    return _DT_ZH.get(key, '')
# DE 富文本里的**纯排版宏**：必须整段删除，不能留成 〈COLOR〉 这种噪声
_FMT_MACROS = {'COLOR', 'NO_COLOR', 'TITLE_START', 'TITLE_END', 'BREAK', 'NAME',
               'BOLD', 'ITALIC', 'UNDERLINE'}


def _unmacro(t):
    """处理 DE 富文本宏。

    · `|TITLE_START|称号|TITLE_END||BREAK||NAME|` → 取「称号」文字；
    · `|COLOR|航道星舰|NO_COLOR|` → 删除排版宏、保留正文；
    · 其余宏（|COUNT| 等变量）保留为 〈MACRO〉，便于阅读。
    """
    if '|' not in t:
        return t
    t = re.sub(r'\|TITLE_START\|(.*?)\|TITLE_END\|', r'\1', t, flags=re.S)
    t = _MACRO.sub(lambda m: '' if m.group(1).upper() in _FMT_MACROS else '〈%s〉' % m.group(1), t)
    return t


def demark(s):
    """对所有来自 DE 导出的字符串做一次宏清理 + 空白收敛（无 `|` 时原样返回）。"""
    return re.sub(r'\s{2,}', ' ', _unmacro(to_text(s))).strip()


def strip_tags(t):
    t = to_text(t)
    t = _DT_TAG.sub(_replace_dt, t)   # <DT_X> / <DT_X_COLOR> → 名称或删除（必须在 _TAG_ANY 之前）
    for _ in range(3):
        t2 = _TAG_PAIR.sub(r'\2', t)
        if t2 == t:
            break
        t = t2
    return _TAG_ANY.sub('', t)


def clean(text, limit=None):
    """清洗富文本：去颜色标签、`\\n` 字面转分隔、|MACRO| → 〈MACRO〉。"""
    if text is None:
        return ''
    t = strip_tags(text)
    t = t.replace('\\n', '；').replace('\\r', '')
    t = _unmacro(t)
    t = t.replace('\r\n', '\n').replace('\r', '\n')
    t = re.sub(r'\n+', ' ', t)
    t = re.sub(r'\s{2,}', ' ', t).strip(' ；')
    if limit and len(t) > limit:
        cut = t[:limit]
        for p in ('。', '；', '！', '？', '. ', '; '):
            i = cut.rfind(p)
            if i >= int(limit * 0.55):
                return cut[:i + len(p)].rstrip()
        return cut.rstrip() + '…'
    return t


def fmt_damage(arr, dt_order):
    """damagePerShot → '冲击 26｜穿刺 35.2｜切割 8.8'（顺序取 config/damageTypes.json）"""
    out = []
    for i, v in enumerate(arr or []):
        if not v or i >= len(dt_order):
            continue
        out.append('%s %s' % (DT_ZH.get(dt_order[i].lower(), dt_order[i]), trim_num(v, 2)))
    return '｜'.join(out)


def trim_num(v, nd=2):
    """nd<=0 按整数输出且不削尾零（150 不能变成 15）。"""
    if v is None:
        return ''
    try:
        f = float(v)
    except (TypeError, ValueError):
        return to_text(v)
    if nd <= 0:
        return str(int(round(f)))
    s = ('%.*f' % (nd, f)).rstrip('0').rstrip('.')
    return s if s else '0'


def pct_frac(x, nd=1):
    if x is None:
        return ''
    try:
        return trim_num(float(x) * 100.0, nd) + '%'
    except (TypeError, ValueError):
        return ''


def pct_raw(x, nd=2):
    if x is None:
        return ''
    try:
        return trim_num(float(x), nd) + '%'
    except (TypeError, ValueError):
        return ''


class Sources:
    def __init__(self):
        self.i18n = jload(os.path.join(IDIR, 'i18n.json'))
        self.dz = jload(os.path.join(PDIR, 'dict.zh.json'))
        self.de = jload(os.path.join(PDIR, 'dict.en.json'))
        self.cfg_dt = jload(os.path.join(CDIR, 'damageTypes.json'))
        self.cfg_pol = jload(os.path.join(CDIR, 'polarities.json'))
        self.cfg_grade = jload(os.path.join(CDIR, 'relicGrades.json'))
        self.items = {}
        for fn in os.listdir(IDIR):
            if fn != 'i18n.json' and fn.endswith('.json'):
                self.items[fn[:-5]] = jload(os.path.join(IDIR, fn))
        self.pep = {}
        for fn in os.listdir(PDIR):
            if fn.startswith('Export') and fn.endswith('.json'):
                self.pep[fn[6:-5]] = jload(os.path.join(PDIR, fn))

        self.zh_item = {}
        for k, v in self.i18n.items():
            z = (v or {}).get('zh') or {}
            if z.get('name'):
                self.zh_item[k] = z
        self.by_base = {}
        for k, v in self.dz.items():
            self.by_base.setdefault(k.rsplit('/', 1)[-1], to_text(v))
        self.en2zh = {}
        for k, v in self.de.items():
            if k.endswith('Name'):
                z = self.dz.get(k)
                if z and to_text(z) != to_text(v):
                    self.en2zh.setdefault(to_text(v), to_text(z))
        self.asset2key = {}
        for grp in self.pep.values():
            if isinstance(grp, dict):
                for k, v in grp.items():
                    if isinstance(v, dict) and isinstance(v.get('name'), str):
                        self.asset2key.setdefault(k, v['name'])
        self.menu_zh = {}
        for k, v in self.dz.items():
            if k.startswith('/Lotus/Language/Menu/CraftingComponent_'):
                self.menu_zh.setdefault(k.split('CraftingComponent_', 1)[1], to_text(v))
        self.planet_zh = {}
        for p in ('Mercury Venus Earth Mars Phobos Deimos Ceres Jupiter Europa Saturn '
                  'Uranus Neptune Pluto Sedna Eris Void Lua KuvaFortress Zariman Duviri').split():
            self.planet_zh[p] = to_text(self.dz.get('/Lotus/Language/Locations/' + p)) or p

        # ---- 补充数据源：worldstate-data + drop-data ----
        _d = os.path.join(DATA, 'wsd')
        self.wsd = {}
        if os.path.isdir(_d):
            for fn in os.listdir(_d):
                if fn.endswith('.json'):
                    self.wsd[fn[:-5]] = jload(os.path.join(_d, fn))
        _d2 = os.path.join(DATA, 'drop')
        self.drop = {}
        if os.path.isdir(_d2):
            for nm in ('all', 'syndicates'):
                p = os.path.join(_d2, nm + '.json')
                if os.path.exists(p):
                    self.drop[nm] = jload(p)
        # 官方任务类型：/Lotus/Language/Missions/MissionName_*
        self.mission_name = {}
        for k, v in self.dz.items():
            if '/Lotus/Language/Missions/MissionName_' in k:
                self.mission_name[norm_code(k.split('MissionName_', 1)[1])] = to_text(v)
        # 官方集团简中名：ExportSyndicates 的 name 语言键 → dict.zh（★这是简体，WSD 的 zh 版是繁体）
        self.syn_zh = {}
        for _un, _rec in (self.pep.get('Syndicates') or {}).items():
            if not isinstance(_rec, dict):
                continue
            _nk = _rec.get('name')
            _zv = self.dz.get(_nk) if isinstance(_nk, str) else None
            if _zv:
                _seg = _un.rsplit('/', 1)[-1]
                self.syn_zh[norm_code(_seg.replace('Syndicate', ''))] = to_text(_zv)
        # 英文名 → 中文名（用于掉落表物品名）；zh==en 的也收，便于部件名拼接
        self.en2zh = {}
        for tbl, recs in self.items.items():
            if tbl == 'i18n':
                continue
            for r in recs or []:
                if not isinstance(r, dict):
                    continue
                en = r.get('name')
                if not en or en in self.en2zh:
                    continue
                zh = to_text((self.zh_item.get(r.get('uniqueName')) or {}).get('name'))
                self.en2zh[en] = zh or en
        # solNodes：节点显示名 → 派系（英文）
        self.node_faction = {}
        sn = self.wsd.get('en_solNodes') or {}
        for k, v in sn.items():
            if not isinstance(v, dict):
                continue
            val = to_text(v.get('value'))
            if not val or val.startswith('SolNode'):
                continue
            nm = re.sub(r'\s*[（(].*?[）)]\s*$', '', val).strip().lower()
            if nm and v.get('enemy'):
                self.node_faction.setdefault(nm, v['enemy'])
        # ---- 人工覆盖层（官方游戏内简中，data 包快照未同步的新内容）----
        # 结构/来源/再生成方法见 zh_overrides.json 的 _meta；键均为 uniqueName。
        self.zh_overrides = {}
        _ovp = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'zh_overrides.json')
        if os.path.exists(_ovp):
            self.zh_overrides = jload(_ovp)
        self.ov_ability = {}
        self.ov_name = {}
        for _u, _rec in (self.zh_overrides.get('warframes') or {}).items():
            _z = self.zh_item.setdefault(_u, {})
            for _f, _kz in (('name', 'name'), ('description', 'description'),
                            ('passive', 'passiveDescription')):
                if _rec.get(_f):
                    _z[_kz] = _rec[_f]
            for _au, _ar in (_rec.get('abilities') or {}).items():
                self.ov_ability[_au] = _ar
            for _cu, _cn in (_rec.get('components') or {}).items():
                self.ov_name[_cu] = _cn
        self._stats = Counter()

    # ------------------------------------------------ 补充数据的译名
    def planet(self, en):
        return PLANET_ZH.get(en) or self.planet_zh.get(en) or en

    def syndicate(self, display):
        """集团英文显示名 → 官方简中名。优先精确别名表 → 官方表精确命中 → WSD 繁转简兜底。"""
        s = to_text(display)
        n = norm_code(s.replace('The ', ''))
        # 1) 精确别名（drop-data 显示名 → 官方 uniqueName 末段）
        seg = SYNDICATE_ALIAS.get(n)
        if seg and self.syn_zh.get(seg):
            return self.syn_zh[seg]
        # 2) 官方表按归一化键精确命中
        if n in self.syn_zh:
            return self.syn_zh[n]
        # 3) WSD 的 zh 版兜底（繁转简）
        wsd = self.wsd.get('zh_syndicatesData') or {}
        for k, v in wsd.items():
            if isinstance(v, dict) and v.get('name') and norm_code(k.replace('Syndicate', '')) == n:
                return t2s(v['name'])
        return s

    def mission(self, en):
        """任务类型英文 → 官方简中"""
        if not en:
            return ''
        k = norm_code(en)
        v = self.mission_name.get(k)
        if v:
            return v
        k2 = norm_code(MISSION_ALIAS.get(en.lower(), ''))
        if k2 and self.mission_name.get(k2):
            return self.mission_name[k2]
        mt = (self.wsd.get('zh_missionTypes') or {}).get(en.upper().replace(' ', '_'))
        if isinstance(mt, dict) and mt.get('value'):
            return to_text(mt['value'])
        return MISSION_EXTRA_ZH.get(en.lower(), en)

    def _name_lookup(self, base, depth=0):
        base = re.sub(r'\s*[（(][^）)]*[）)]\s*$', '', base).strip() or base
        v = self.en2zh.get(base)
        if v:
            return v
        if depth >= 3:
            return None
        parts = base.rsplit(' ', 1)
        if len(parts) == 2:
            head, tail = parts
            hz = self.en2zh.get(head)
            if hz:
                tz = PART_ZH.get(tail.title()) or PART_ZH.get(tail.lower())
                if tz:
                    return '%s %s' % (hz, tz)
                if tail.title() in ('Blueprint', 'Blueprints'):
                    return hz + '蓝图'
            inner = self._name_lookup(head, depth + 1)
            if inner and inner != head:
                tz = PART_ZH.get(tail.title()) or PART_ZH.get(tail.lower())
                if tz:
                    return '%s %s' % (inner, tz)
                if tail.title() in ('Blueprint', 'Blueprints'):
                    return inner + '蓝图'
        return None

    def item(self, name):
        """掉落物品名（可能带数量前缀）→ 中文显示名"""
        if not name:
            return ''
        s = to_text(name).strip()
        qty = ''
        m = _QTY_RE.match(s)
        if m:
            qty, s = m.group(1).replace(',', '') + ' ', m.group(2).strip()
        base = s
        zh = self._name_lookup(base)
        if not zh:
            rm = _RELIC_RE.match(base)
            if rm:
                zh = '%s %s 遗物' % (ERA_ZH.get(rm.group(1).title(), rm.group(1)), rm.group(2))
        if not zh:
            zh = ITEM_FIXED_ZH.get(base, base)
        return demark(qty + zh)

    def name(self, path, fallback=None):
        """对外入口：统一做一次宏清理，避免 DE 富文本宏（|COLOR| 等）漏进产物。"""
        return demark(self._name(path, fallback))

    def _name(self, path, fallback=None):
        if not path:
            return fallback
        if path in self.ov_name:
            self._stats['L0_override'] += 1
            return self.ov_name[path]
        z = self.zh_item.get(path)
        if z:
            self._stats['L1_i18n'] += 1
            return z['name']
        seg = path.rsplit('/', 1)[-1]
        v = self.menu_zh.get(seg)
        if v:
            self._stats['L2_craft'] += 1
            return v
        # L2b: xxxBlueprint → CraftingComponent_xxx（+Name）→ 值 + 「蓝图」
        if seg.endswith('Blueprint'):
            stem = seg[:-len('Blueprint')]
            for cand in (stem, stem + 'Name'):
                v = self.menu_zh.get(cand)
                if v:
                    self._stats['L2b_blueprint'] += 1
                    return v + '蓝图'
        k = self.asset2key.get(path)
        if k:
            v = to_text(self.dz.get(k))
            if v:
                self._stats['L3_bridge'] += 1
                return v
        for cand in (seg + 'Name', seg):
            v = self.by_base.get(cand)
            if v:
                self._stats['L4_base'] += 1
                return v
        stem = re.sub(r'(Ability|Card|Item|Blueprint|Component|Weapon|Avatar|Suit)$', '', seg)
        for cand in (stem, seg):
            v = self.en2zh.get(cand)
            if v:
                self._stats['L5_rev'] += 1
                return v
        self._stats['MISS'] += 1
        return fallback

    def zhdesc(self, rec, limit=None):
        if not isinstance(rec, dict):
            return ''
        z = self.zh_item.get(rec.get('uniqueName')) or {}
        t = z.get('description') or rec.get('description') or ''
        return clean(t, limit)

    def ability_name(self, ability):
        _ov = self.ov_ability.get(ability.get('uniqueName')) or {}
        if _ov.get('name'):
            self._stats['ab_override'] += 1
            return _ov['name']
        seg = ability['uniqueName'].rsplit('/', 1)[-1]
        for pref in ('Suits', 'Necramech', 'Necromech'):
            v = to_text(self.dz.get('/Lotus/Language/%s/%sName' % (pref, seg)))
            if v:
                self._stats['ab_suit'] += 1
                return v
        v = self.by_base.get(seg + 'Name')
        if v:
            self._stats['ab_base'] += 1
            return v
        en = ability.get('name')
        v = self.en2zh.get(en) if en else None
        if v:
            self._stats['ab_rev'] += 1
            return v
        self._stats['ab_miss'] += 1
        return en or '?'

    def ability_desc(self, ability, limit=170):
        _ov = self.ov_ability.get(ability.get('uniqueName')) or {}
        if _ov.get('desc'):
            return clean(_ov['desc'], limit)
        seg = ability['uniqueName'].rsplit('/', 1)[-1]
        for pref in ('Suits', 'Necramech', 'Necromech'):
            v = to_text(self.dz.get('/Lotus/Language/%s/%sDesc' % (pref, seg)))
            if v:
                return clean(v, limit)
        return clean(ability.get('description'), limit)

    def mod_effect_zh(self, rec):
        z = self.zh_item.get(rec.get('uniqueName')) or {}
        if z.get('description'):
            return clean(z['description'], 320)
        uu = (self.pep.get('Upgrades') or {}).get(rec.get('uniqueName')) or {}
        nk = uu.get('name')
        cands = []
        if isinstance(nk, str) and nk.endswith('Name'):
            cands.append(nk[:-4] + 'Desc')
        if isinstance(uu.get('description'), str):
            cands.append(uu['description'])
        for c in cands:
            v = to_text(self.dz.get(c))
            if v:
                return clean(v, 320)
        return ''


# ---------------------------------------------------------------- 常量表
DT_ZH = {
    'impact': '冲击', 'puncture': '穿刺', 'slash': '切割',
    'heat': '火焰', 'cold': '冰冻', 'electricity': '电击', 'toxin': '毒素',
    'blast': '爆炸', 'radiation': '辐射', 'gas': '毒气',
    'magnetic': '磁力', 'viral': '病毒', 'corrosive': '腐蚀', 'void': '虚空',
    'tau': 'Tau', 'cinematic': '剧情', 'shielddrain': '护盾吸取',
    'healthdrain': '生命吸取', 'energydrain': '能量吸取', 'true': '真实',
}
# 极性：兼容 AP_* 与 warframe-items 的小写简写
POLARITY_ZH = {
    'AP_ATTACK': 'Madurai（攻击）', 'AP_DEFENSE': 'Vazarin（防御）',
    'AP_TACTIC': 'Naramon（战术）', 'AP_POWER': 'Zenurik（威力）',
    'AP_PRECEPT': 'Penjaga（预置）', 'AP_WARD': 'Unairu（守护）',
    'AP_UMBRA': 'Umbra（暗影）', 'AP_ANY': 'Aura（光环）',
    'AP_UNIVERSAL': '万能（Umbra 除外）',
    'attack': 'Madurai（攻击）', 'madurai': 'Madurai（攻击）',
    'defense': 'Vazarin（防御）', 'vazarin': 'Vazarin（防御）',
    'tactic': 'Naramon（战术）', 'naramon': 'Naramon（战术）',
    'power': 'Zenurik（威力）', 'zenurik': 'Zenurik（威力）',
    'precept': 'Penjaga（预置）', 'penjaga': 'Penjaga（预置）',
    'ward': 'Unairu（守护）', 'unairu': 'Unairu（守护）',
    'umbra': 'Umbra（暗影）', 'any': 'Aura（光环）', 'aura': 'Aura（光环）',
    'universal': '万能（Umbra 除外）', '': '',
}
RARITY_ZH = {'Common': '普通', 'Uncommon': '罕见', 'Rare': '稀有', 'Legendary': '传奇'}
FACTION_ZH = OrderedDict([
    ('Grineer', 'Grineer'), ('Corpus', 'Corpus'), ('Infested', 'Infested'),
    ('Infestation', 'Infested'), ('Sentient', 'Sentient'), ('Corrupted', 'Corrupted'),
    ('Orokin', '奥罗金'), ('Orokin Empire', '奥罗金帝国'), ('OrokinEmpire', '奥罗金帝国'),
    ('Narmer', '合一众'), ('Scaldra', '炽蛇军'), ('Techrot', '科腐者'),
    ('MITW', '墙中人'), ('Stalker', 'Stalker'), ('Tenno', 'Tenno'),
    ('Duviri', '双衍王境'), ('Anarch', '无政府主义者'), ('Prey', '猎物'),
    ('Crossfire', '多方交战'),
])
# 敌人 type 字段：DE 侧为英文派系名，按同一规则保留英文/转中文
ENEMY_TYPE_ZH = {'Corpus': 'Corpus', 'Grineer': 'Grineer', 'Infested': 'Infested',
                 'Orokin': '奥罗金', 'Tenno': 'Tenno', 'Sentient': 'Sentient',
                 'Corrupted': 'Corrupted', 'Narmer': '合一众', 'Stalker': 'Stalker',
                 'Wild': '野生生物', 'Neutral': '中立', 'Scaldra': '炽蛇军',
                 'Techrot': '科腐者', 'Murmur': '低语者', 'Duviri': '双衍王境'}
RELIC_ERA_ZH = {'Lith': '古纪', 'Meso': '前纪', 'Neo': '中纪', 'Axi': '后纪',
                'Requiem': '安魂', 'Vanguard': '先锋', 'Omnia': '全能', 'Void': '虚空'}
REFINE_ZH = {'Intact': '完整', 'Exceptional': '优良', 'Flawless': '无瑕', 'Radiant': '光辉'}
MODTYPE_ZH = {
    'Warframe Mod': '战甲 MOD', 'Primary Mod': '主武器 MOD', 'Secondary Mod': '副武器 MOD',
    'Melee Mod': '近战 MOD', 'Shotgun Mod': '霰弹枪 MOD', 'Companion Mod': '同伴 MOD',
    'Stance Mod': '近战架式', 'Posture Mod': '姿态 MOD', 'Arch-Gun Mod': '空战枪械 MOD',
    'Arch-Melee Mod': '空战近战 MOD', 'Archwing Mod': 'Archwing MOD',
    'Focus Way': '专精流派', 'Plexus Mod': 'Plexus（九重天）MOD',
    'Railjack Mod': '九重天 MOD', 'Necramech Mod': '死灵机甲 MOD',
    'K-Drive Mod': 'K 式悬浮板 MOD', 'Parazon Mod': 'Parazon（数据匕首）MOD',
    'Tektolyst Artifact Mod': 'Tek Tolyst 遗物 MOD', 'Mod Set Mod': 'MOD 套装 MOD',
    'Transmutation Mod': '转换 MOD',
}

# ---- MOD 效果「术语规范化」：英文属性 → 官方简中术语（长词优先）----
STAT_TERMS = [
    ('Ability Strength', '技能强度'), ('Ability Duration', '技能持续时间'),
    ('Ability Efficiency', '技能效率'), ('Ability Range', '技能范围'),
    ('Power Strength', '技能强度'), ('Power Duration', '技能持续时间'),
    ('Power Efficiency', '技能效率'), ('Power Range', '技能范围'),
    ('Melee Attack Speed', '近战攻击速度'), ('Attack Speed', '攻击速度'),
    ('Heavy Attack Efficiency', '重击效率'), ('Heavy Attack', '重击'),
    ('Combo Duration', '连击持续时间'), ('Initial Combo', '初始连击'),
    ('Combo Count', '连击数'), ('Finisher Damage', '处决伤害'),
    ('Ground Slam', '震地攻击'), ('Slide Attack', '滑行攻击'),
    ('Status Chance', '触发几率'), ('Status Duration', '触发持续时间'),
    ('Critical Chance', '暴击几率'), ('Critical Damage', '暴击伤害'),
    ('Critical Multiplier', '暴击倍率'), ('Headshot Multiplier', '爆头倍率'),
    ('Headshot Damage', '爆头伤害'), ('Weak Point Damage', '弱点伤害'),
    ('Fire Rate', '射速'), ('Reload Speed', '装填速度'), ('Reload Time', '装填时间'),
    ('Magazine Capacity', '弹匣容量'), ('Ammo Maximum', '弹药最大值'),
    ('Ammo Capacity', '弹药容量'), ('Ammo Efficiency', '弹药效率'),
    ('Multishot', '多重射击'), ('Punch Through', '穿透'),
    ('Projectile Speed', '投射物速度'), ('Zoom', '变焦'), ('Recoil', '后坐力'),
    ('Accuracy', '精准度'), ('Sprint Speed', '冲刺速度'), ('Movement Speed', '移动速度'),
    ('Parkour Velocity', '跑酷速度'), ('Shield Recharge Delay', '护盾恢复延迟'),
    ('Shield Recharge', '护盾恢复'), ('Shield Capacity', '护盾容量'),
    ('Max Shields', '最大护盾'), ('Shield', '护盾'), ('Max Health', '最大生命值'),
    ('Health', '生命值'), ('Armor', '护甲'), ('Energy Max', '最大能量'),
    ('Energy Regen', '能量恢复'), ('Energy', '能量'), ('Bleedout', '濒死倒计时'),
    ('Life Steal', '生命窃取'), ('Amp', '增幅器'), ('Range', '攻击范围'),
    ('Critical', '暴击'), ('Headshot', '爆头'), ('Weak Point', '弱点'),
    ('Kill', '击杀'), ('Death', '死亡'), ('Hit', '命中'),
    ('Heat', '火焰'), ('Cold', '冰冻'), ('Electricity', '电击'),
    ('Toxin', '毒素'), ('Blast', '爆炸'), ('Radiation', '辐射'),
    ('Gas', '毒气'), ('Magnetic', '磁力'), ('Viral', '病毒'),
    ('Corrosive', '腐蚀'), ('Void', '虚空'), ('Impact', '冲击'),
    ('Puncture', '穿刺'), ('Slash', '切割'), ('Tau', 'Tau'),
]
# 短语（含语序调整），长词优先
PHRASE_TERMS = [
    ('chance to apply', '几率造成'), ('chance for', '几率获得'),
    ('chance to', '几率'),
    ('on Weak Point Kill', '弱点击杀时'), ('on Weak Point Hit', '命中弱点时'),
    ('on Headshot', '爆头时'), ('on Critical Hit', '暴击时'), ('on Critical', '暴击时'),
    ('on Melee Kill', '近战击杀时'), ('on Kill', '击杀时'), ('on Hit', '命中时'),
    ('on Ability Cast', '施放技能时'), ('on Reload', '装填时'), ('on Equip', '装备时'),
    ('while Aim Gliding', '飞身瞄准时'), ('while Sprinting', '冲刺时'),
    ('while Airborne', '空中时'), ('while Sliding', '滑行时'), ('while Aiming', '瞄准时'),
    ('while Blocking', '格挡时'), ('while Equipped', '装备时'),
    ('for each enemy hit', '每命中一名敌人'),
    ('Stacks up to', '最多叠加'), ('max stacks of Mutation', '突变最大层数'),
    ('to Melee Weapons', '（近战武器）'), ('to Primary Weapons', '（主武器）'),
    ('to Secondary Weapons', '（副武器）'), ('to Melee', '（近战）'),
    ('of Ammo Maximum', '最大弹药'), ('of Ammo Pick Up', '弹药拾取量'),
    ('Damage on Health', '生命值伤害'), ('Damage on Shields', '护盾伤害'),
    ('Void Damage', '虚空伤害'), ('of Damage', '的伤害'),
    ('of invulnerability', '的无敌时间'), ('an additional', '额外'),
    ('Energy Orb', '能量球'), ('Health Orb', '生命球'),
    ('Arcane Revive', '赋能复活'), ('Arcane', '赋能'),
    ('Shield Regen', '护盾恢复'), ('Health Regen', '生命恢复'),
    ('to Infested', '（对 Infested）'), ('to Grineer', '（对 Grineer）'),
    ('to Corpus', '（对 Corpus）'),
    ('ammo efficiency', '弹药效率'), ('Ammo Efficiency', '弹药效率'),
    ('multiplier', '倍率'), ('Converts', '转换'),
    ('Damage', '伤害'), ('Health', '生命值'), ('Shields', '护盾'), ('Shield', '护盾'),
    ('Energy', '能量'), ('Armor', '护甲'), ('Zoom', '变焦'),
    ('enemies', '敌人'), ('Enemies', '敌人'), ('ally', '队友'),
    ('revive', '复活'), ('Revive', '复活'),
    ('max stacks', '最大层数'), ('stacks', '层'), ('Stack', '层'),
    ('weapons', '武器'), ('Weapons', '武器'), ('weapon', '武器'), ('Weapon', '武器'),
]
_TERM_ORDER = sorted(STAT_TERMS, key=lambda p: -len(p[0]))
_PHRASE_ORDER = sorted(PHRASE_TERMS, key=lambda p: -len(p[0]))
_UNIT_ZH = {'%': '%', 'x': '倍', '×': '倍', 's': '秒', 'm': '米', 'sec': '秒',
            'secs': '秒', 'seconds': '秒', 'meters': '米'}
_PREFIX_ZH = [('On Weak Point Kill:', '弱点击杀时：'), ('On Melee Kill:', '近战击杀时：'),
              ('On Hit:', '命中时：'), ('On Kill:', '击杀时：'), ('On Headshot:', '爆头时：'),
              ('On Critical Hit:', '暴击时：'), ('On Ability Cast:', '施放技能时：'),
              ('On Reload:', '装填时：'), ('On Equip:', '装备时：'),
              ('While Airborne:', '空中时：'), ('While Sliding:', '滑行时：'),
              ('While Aiming:', '瞄准时：'), ('While Blocking:', '格挡时：')]
# 保持英文的官方专有名词（用于判定「中文规范化是否仍残留英文」）
_LATIN_OK = re.compile(r'\b(Prime|Umbra|Grineer|Corpus|Infested|Sentient|Corrupted|'
                       r'Narmer|Orokin|Tenno|Void|Tau|Incarnon|Necramech|Archwing|'
                       r'Kuva|Mod|Forma|Aura|Exilus|Excalibur|Wisp|Styanax|Nidus|Khora|'
                       r'Whipclaw|Shuriken|Nezha|Gauss|Subsumed|Helminth|Parazon|'
                       r'Melee|Riven|Amp|Overguard|Hildryn|Harrow|Zephyr|Titania|'
                       r'Lavos|Grendel|Yareli|Protea|Xaku|Caliban|Gyra|Dante|Jade|Qorvex|'
                       r'Varuna|Wraithe|Koumei|Cyte|Temple|Oraxia|Uriel|Roathe)\b', re.I)


def _apply(text, table):
    out = text
    for en, zh in table:
        out = re.sub(r'(?<![A-Za-z])%s(?![A-Za-z])' % re.escape(en), zh, out)
    return out


def zh_stat(text):
    """把 MOD 属性短语规范化成中文：数值原样保留、术语用官方简中译名。"""
    if not text:
        return ''
    t = strip_tags(text).replace('\\n', '；').strip()
    pre = ''
    for a, b in _PREFIX_ZH:
        if t.startswith(a):
            pre, t = b, t[len(a):].strip()
            break
    m = re.match(r'^([+\-]?[\d.]+)%\s+chance to apply\s+(.+?)\s+on\s+(.+)$', t, re.I)
    if m:
        val, what, when = m.groups()
        return '%s%s：有 %s%% 几率施加 %s' % (pre, _finish(when), val, _finish(what))
    # 倍率写法 x1.55 Damage
    m = re.match(r'^x\s*([\d.]+)\s*(.*)$', t, re.I)
    if m:
        rz = _finish(m.group(2))
        return '%s%s×%s' % (pre, rz or '倍率', m.group(1))
    m = re.match(r'^([+\-])?\s*([\d.]+)\s*(%|x|×|s|m|sec|secs|seconds|meters)?\s*(.*)$', t)
    if not m:
        return pre + _finish(t)
    sign, val, unit, rest = m.groups()
    num = '%s%s%s' % (sign or '', val, _UNIT_ZH.get(unit or '', unit or ''))
    rz = _finish(rest) if rest else ''
    if rz:
        return '%s%s %s' % (pre, rz, num)
    return '%s%s' % (pre, num)


def _finish(s):
    if not s:
        return ''
    out = _apply(s, _PHRASE_ORDER)
    out = _apply(out, _TERM_ORDER)
    # 时间/距离单位
    out = re.sub(r'\bfor (\d+(?:\.\d+)?)s\b', r'持续 \1 秒', out)
    out = re.sub(r'\b(\d+(?:\.\d+)?)s\b', r'\1 秒', out)
    out = re.sub(r'\b(\d+(?:\.\d+)?)m\b', r'\1 米', out)
    out = re.sub(r'\bStacks up to (\d+)x\b', r'最多叠加 \1 层', out)
    out = re.sub(r'\b(\d+)x\b', r'\1 层', out)
    out = re.sub(r'\.\s+', '；', out).strip(' .；,')
    out = re.sub(r'\s{2,}', ' ', out).strip(' ；,')
    return out


def residue_latin(s):
    """规范化后是否仍残留明显英文（用于决定是否附英文原文）。"""
    if not s:
        return False
    t = _LATIN_OK.sub(' ', s)
    return bool(re.search(r'[A-Za-z]{3,}', t))


def residue_words(s):
    """规范化后残留的英文词列表（白名单除外）。"""
    return re.findall(r'[A-Za-z]{3,}', _LATIN_OK.sub(' ', s or ''))
