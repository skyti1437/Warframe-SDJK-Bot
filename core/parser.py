# -*- coding: utf-8 -*-
"""自由参数指令解析器。

约定：
  1. 指令格式为【主指令 内容 附加指令】，三者位置可任意调换，中间以空格分隔；
  2. 通用修饰符（可叠加）：
       -pc/-ps/-xb/-sw  平台覆盖（优先于群默认平台）
       -1/-w                强制纯文字输出
       -t                   强制图片输出
       -数字                翻页
       -r                   快捷回复（交易密语）
  3. 管理类指令以英文点 "." 为前缀（.默认平台/.开启/.关闭）；
  4. 紫卡词条支持无空格连写，例如 `wr 基多暴负变焦 绝路 -2`。

解析策略：
  - 先按空白切词（支持引号包裹含空格的物品名），再逐词分类：
    修饰符 / 主指令（词表中第一个命中的词即为主指令，其后同名词退化为内容）/
    查询内容；
  - 内容层的二级解析（wm / wr / 蹲 / 裂隙筛选）由各 handler 调用本模块的
    parse_wm() / parse_wr() / parse_fissure_filter() / parse_duration() 完成。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Optional

# ---------------------------------------------------------------------------
# 平台
# ---------------------------------------------------------------------------
# Warframe 已全面跨平台互通（crossplay），PC/PS/XBOX/NS 四个国际服入口
# 拿到的是**同一份世界状态数据**，价格库也早已打通。因此展示层统一合并成
# 「国际服」，只在请求 warframe.market 时保留原始平台码（价格库仍分平台）。
PLATFORMS = ("pc", "ps", "xb", "sw")
PLATFORM_ALIASES = {
    "pc": "pc",
    "ps": "ps", "ps4": "ps", "ps5": "ps", "playstation": "ps",
    "xb": "xb", "xbox": "xb", "series": "xb",
    "sw": "sw", "switch": "sw", "任天堂": "sw",
}
PLATFORM_DISPLAY = {"pc": "国际服", "ps": "国际服",
                    "xb": "国际服", "sw": "国际服"}

_MODIFIER_RE = re.compile(r"^-(?P<body>[A-Za-z0-9]+)$")


def normalize_platform(word: str) -> Optional[str]:
    return PLATFORM_ALIASES.get(word.strip().lower())


# ---------------------------------------------------------------------------
# 主指令词表（canonical -> 触发别名）
# ---------------------------------------------------------------------------
COMMAND_ALIASES: dict[str, set[str]] = {
    # 帮助
    "help": {"帮助", "help"},
    "status": {"状态"},
    # ---- 世界状态与周期 ----
    "cetus": {"夜灵", "平原时间", "平原", "夜灵平野", "平原昼夜"},
    "timers": {"时效", "周报"},
    "bounty": {"赏金"},
    "fissures": {"裂隙"},
    "sortie": {"突击"},
    "archon": {"执刑官", "执刑官猎杀", "大猎杀"},
    "voidtrader": {"奸商", "虚空商人", "巴罗"},
    "dailydeals": {"每日特惠", "达沃"},
    "calendar": {"日历", "1999日历"},
    "deeparchimedea": {"深层科研", "深层"},
    "temporalarchimedea": {"时光科研", "时光"},
    "steelpath": {"钢铁之路"},
    "arbitration": {"仲裁"},
    "arbtable": {"仲裁表"},
    "alerts": {"警报"},
    "invasions": {"入侵"},
    "nightwave": {"电波", "午夜电波", "夜波"},
    "news": {"新闻", "最近新闻", "最近更新", "最近版本", "热修文字"},
    "kuva": {"赤毒", "血紊"},
    "synthtargets": {"结合目标", "结合仪式"},
    "construction": {"舰队进度"},
    "voidstorms": {"九重天", "异变", "虚空风暴", "航道星舰"},
    "events": {"活动", "限时活动"},
    "conclave": {"武形秘仪", "武形", "秘仪"},
    "primevault": {"阿耶", "阿耶兑换", "御品阿耶", "出库", "御品"},
    "clanrewards": {"氏族奖励", "氏族"},
    "flashsales": {"商城折扣", "礼包"},
    "incarnon": {"本周灵化", "灵化轮换", "灵化"},
    "tenet": {"信条"},
    "coda": {"终幕"},
    "acrichis": {"言录使"},
    "descendia": {"沉沦之地", "炼狱塔", "炼狱"},
    "incursions": {"侵袭", "钢路侵袭", "钢铁侵袭"},
    # ---- 资料与计算器 ----
    "wiki": {"wiki", "wk", "维基"},
    "valence": {"武器融合", "融合"},
    "damage": {"伤害", "伤害计算"},
    "scan": {"识卡", "读卡", "配卡识别", "识图", "识别配卡"},
    "scandamage": {"识卡伤害", "扫伤害"},
    "kim": {"对话助手"},
    # ---- 市场与查价 ----
    "wm": {"wm"},
    "wr": {"wr", "wmr", "紫卡"},
    "rm": {"rm"},
    "rank": {"排行", "市场排行"},
    "analysis": {"紫卡分析"},
    "trend": {"趋势", "wm趋势", "价格趋势", "紫卡趋势"},
    "openrelic": {"开核桃"},
    "xh": {"xh", "玄骸"},
    "relic": {"遗物", "核桃"},
    "parts": {"部件", "零件", "组件"},
    "disposition": {"倾向", "紫卡倾向"},
    "ducats": {"金垃圾", "银垃圾", "铜垃圾"},
    # ---- 后台推送 ----
    "dun": {"蹲"},
}

# 别名携带“预设内容”的指令：钢铁裂隙 → 裂隙 + 内容"钢铁" 等
_PRESET_COMMANDS: dict[str, tuple[str, str]] = {
    "钢铁裂隙": ("fissures", "钢铁"),
    "虚空裂隙": ("fissures", "普通"),
    "九重天裂隙": ("fissures", "九重天"),
    "地球赏金": ("bounty", "地球"),
    "夜灵赏金": ("bounty", "地球"),
    "金星赏金": ("bounty", "金星"),
    "山谷赏金": ("bounty", "金星"),
    "火卫二赏金": ("bounty", "火卫二"),
    "魔胎赏金": ("bounty", "火卫二"),
    "深矿赏金": ("bounty", "深矿"),
    "金垃圾": ("ducats", "金"),
    "银垃圾": ("ducats", "银"),
    "铜垃圾": ("ducats", "铜"),
    # 分类排行
    "甲排行": ("rank", "甲"), "战甲排行": ("rank", "甲"),
    "卡排行": ("rank", "卡"), "MOD排行": ("rank", "卡"),
    "部件排行": ("rank", "部件"), "赋能排行": ("rank", "赋能"),
    "主武排行": ("rank", "主武"), "主武器排行": ("rank", "主武"),
    "副武排行": ("rank", "副武"), "副武器排行": ("rank", "副武"),
    "近战排行": ("rank", "近战"), "遗物排行": ("rank", "遗物"),
    "紫卡排行": ("rank", "紫卡"),
    # 遗物入库 / 出库
    "遗物列表": ("relic", "列表"), "遗物入库": ("relic", "入库"),
    "遗物出库": ("relic", "出库"),
    "开核桃速刷": ("openrelic", "速刷"),
}

# 触发词 -> canonical 主指令（统一小写，parse 时按小写匹配）
ALIAS_TO_COMMAND: dict[str, str] = {}
for _cmd, _aliases in COMMAND_ALIASES.items():
    for _a in _aliases:
        ALIAS_TO_COMMAND[_a.lower()] = _cmd
for _alias, (_cmd, _preset) in _PRESET_COMMANDS.items():
    ALIAS_TO_COMMAND[_alias.lower()] = _cmd
# _PRESET_COMMANDS 自身也小写化，方便 parse() 里按 low 取 preset
_PRESET_COMMANDS = {k.lower(): v for k, v in _PRESET_COMMANDS.items()}


# ---------------------------------------------------------------------------
# 解析结果
# ---------------------------------------------------------------------------
@dataclass
class Parsed:
    """一条用户消息的解析产物。"""

    raw: str = ""
    command: Optional[str] = None        # canonical 主指令
    command_raw: Optional[str] = None    # 原始触发词
    preset: Optional[str] = None         # 别名携带的预设内容（如 金垃圾→"金"）
    content: list[str] = field(default_factory=list)   # 查询内容 token
    modifiers: list[str] = field(default_factory=list)  # 已识别修饰符原文
    platform: Optional[str] = None       # -pc/-ps/...
    text_mode: bool = False              # -w / -1
    image_mode: bool = False             # -t
    whisper: bool = False                # -r
    page: int = 1                        # -2 / -3 ...
    unknown_modifiers: list[str] = field(default_factory=list)

    @property
    def content_str(self) -> str:
        return " ".join(self.content)

    @property
    def force_text(self) -> bool:
        return self.text_mode and not self.image_mode

    @property
    def force_image(self) -> bool:
        return self.image_mode and not self.text_mode


def tokenize(text: str) -> list[str]:
    """空白切词，双引号内的内容（可含空格，如英文物品名）算一个词。"""
    tokens: list[str] = []
    for m in re.finditer(r'"([^"]*)"|(\S+)', text):
        tokens.append((m.group(1) if m.group(1) is not None else m.group(2)) or "")
    return [t for t in tokens if t]


def parse(message: str, *, extra_commands: Optional[dict[str, str]] = None) -> Parsed:
    """把一条消息解析为 Parsed。没有命中主指令时 command 为 None。"""
    res = Parsed(raw=message)
    tokens = tokenize(message.strip())
    alias_table = ALIAS_TO_COMMAND
    if extra_commands:
        alias_table = dict(ALIAS_TO_COMMAND)
        alias_table.update(extra_commands)

    for tok in tokens:
        m = _MODIFIER_RE.match(tok)
        if m:
            body = m.group("body").lower()
            if body in ("pc", "ps", "xb", "sw"):
                res.platform = body
                res.modifiers.append(tok)
            elif body == "w" or body == "1":
                res.text_mode = True
                res.modifiers.append(tok)
            elif body == "t":
                res.image_mode = True
                res.modifiers.append(tok)
            elif body == "r":
                res.whisper = True
                res.modifiers.append(tok)
            elif body.isdigit() and int(body) >= 2:
                res.page = int(body)
                res.modifiers.append(tok)
            else:
                res.unknown_modifiers.append(tok)
            continue

        low = tok.lower()
        if res.command is None and low in alias_table:
            res.command_raw = tok
            res.command = alias_table[low]
            if low in _PRESET_COMMANDS:
                res.preset = _PRESET_COMMANDS[low][1]
            continue
        res.content.append(tok)
    return res


# ---------------------------------------------------------------------------
# 紫卡词条词表（标准词条 -> 可识别短名/别名）
# 键为内部标准名（与 WM 属性 url 对齐），值为别名集合。
# 依据社区通行的紫卡词条表整理，并补充单字缩写。
# ---------------------------------------------------------------------------
RIVEN_STAT_ALIASES: dict[str, set[str]] = {
    "melee_damage": {"近战伤害", "基伤", "基础伤害", "近战", "基础", "基"},
    "crit_chance": {"暴击几率", "暴率", "暴击率", "爆击率", "爆率", "爆击几率",
                    "暴击概率", "爆击概率", "暴击", "爆击", "暴", "爆",
                    "爆率几率", "暴几率"},
    "crit_damage": {"暴击伤害", "暴伤", "爆伤", "爆击伤害", "暴击伤", "爆击伤"},
    "multishot": {"多重射击", "多重", "多"},
    "attack_speed": {"攻击速度", "攻速", "速度"},
    "fire_rate": {"射击速度", "射击速率", "射速"},
    "status_chance": {"触发几率", "触发", "触率", "触发概率"},
    "status_duration": {"触发时间", "触时", "触发持续时间"},
    "range": {"攻击范围", "范围"},
    "initial_combo": {"初始连击", "初始连击数", "连击数"},
    "combo_duration": {"连击持续时间", "连击时间"},
    "heavy_attack_efficiency": {"重击效率"},
    "combo_efficiency": {"近战连击效率", "连击效率"},
    "finisher_damage": {"处决伤害", "处决"},
    "slide_crit": {"滑行攻击时暴击率", "滑暴", "滑行暴击几率", "滑行暴击",
                   "滑爆", "滑行爆击", "滑行", "滑行攻击"},
    "slash_damage": {"切割伤害", "切割"},
    "impact_damage": {"冲击伤害", "冲击"},
    "puncture_damage": {"穿刺伤害", "穿刺"},
    "heat_damage": {"火焰伤害", "火伤", "火", "火焰"},
    "cold_damage": {"冰冻伤害", "冰伤", "冰", "冰冻"},
    "toxin_damage": {"毒素伤害", "毒伤", "毒", "毒素"},
    "electric_damage": {"电击伤害", "电伤", "电", "电击"},
    "damage_vs_grineer": {"对Grineer伤害", "G系伤害", "G伤", "Grineer伤害",
                          "G佬伤害", "G系", "G佬", "G歧视", "G"},
    "damage_vs_corpus": {"对Corpus伤害", "C系伤害", "C伤", "Corpus伤害",
                         "C佬伤害", "C系", "C佬", "C歧视", "C"},
    "damage_vs_infested": {"对Infested伤害", "I系伤害", "I伤", "Infested伤害",
                           "I佬伤害", "I系", "I佬", "I歧视", "I"},
    "magazine_capacity": {"弹匣容量", "弹匣", "弹夹", "弹夹容量", "弹容"},
    "ammo_max": {"弹药最大值", "弹药", "弹药上限"},
    "projectile_speed": {"投射物速度", "弹道", "投射", "弹道飞行速度", "弹道速度"},
    "punch_through": {"穿透"},
    "reload_speed": {"装填速度", "装填", "装弹"},
    "recoil": {"武器后坐力", "后坐", "后坐力", "后座", "后座力"},
    "zoom": {"变焦", "缩放"},
    "extra_combo_count": {"额外连击数", "额外连击", "额外连击数几率",
                          "减连击获取", "减额外连击数几率",
                          "减额外连击", "减额外连击数"},
    # WM slug chance_to_gain_combo_count（仅负向；DE 官方文案「…% 的几率来获得连击数」）
    "combo_gain_chance": {"连击数获取几率", "获得连击数几率", "连击获取几率",
                          "连击获取", "连击数获取", "获得连击数", "连击数几率"},
}
# 组合词：一个别名展开为多个词条需求
RIVEN_STAT_COMBOS: dict[str, list[str]] = {
    "双暴": ["crit_chance", "crit_damage"],
    "双爆": ["crit_chance", "crit_damage"],
    "三基": ["melee_damage", "crit_chance", "crit_damage"],
    "基多暴": ["melee_damage", "multishot", "crit_chance"],
}
# 本插件标准词条 id -> WM v1 拍卖数据里的 url_name（部分为合并名）
RIVEN_URL_COMPAT: dict[str, str] = {
    "damage": "base_damage_/_melee_damage",
    "melee_damage": "base_damage_/_melee_damage",
    "crit_chance": "critical_chance",
    "crit_damage": "critical_damage",
    "attack_speed": "fire_rate_/_attack_speed",
    "fire_rate": "fire_rate_/_attack_speed",
    "ammo_max": "ammo_maximum",
    # ↓ 下面 5 条的 slug 与插件标准名**完全不同**（WM 沿用了远古 Channeling 时代的
    #   slug，2026-09-14 拉 /v2/riven/attributes 全量 32 个 slug 后补齐）。
    #   漏映射有两个后果：①拍卖卡直接漏英文（如「critical chance on slide attack」）；
    #   ②紫卡搜索的 positive_stats 过滤参数发了个 WM 不认识的 slug，筛不出东西。
    "slide_crit": "critical_chance_on_slide_attack",
    "initial_combo": "channeling_damage",
    "heavy_attack_efficiency": "channeling_efficiency",
    "extra_combo_count": "chance_to_gain_extra_combo_count",
    "combo_gain_chance": "chance_to_gain_combo_count",
}
# 标准词条 id -> 展示主中文名（拍卖展示用，精选短名）
RIVEN_STAT_ZH: dict[str, str] = {
    "melee_damage": "基伤", "crit_chance": "暴击", "crit_damage": "暴伤",
    "multishot": "多重", "attack_speed": "攻速", "fire_rate": "射速",
    "status_chance": "触发", "status_duration": "触时", "range": "范围",
    "initial_combo": "初始连击", "combo_duration": "连击时间",
    "heavy_attack_efficiency": "重击效率", "combo_efficiency": "连击效率",
    "finisher_damage": "处决伤", "slide_crit": "滑暴",
    "slash_damage": "切割", "impact_damage": "冲击", "puncture_damage": "穿刺",
    "heat_damage": "火伤", "cold_damage": "冰伤", "toxin_damage": "毒伤",
    "electric_damage": "电伤",
    "damage_vs_grineer": "G伤", "damage_vs_corpus": "C伤",
    "damage_vs_infested": "I伤",
    "magazine_capacity": "弹匣", "ammo_max": "弹药",
    "projectile_speed": "弹道", "punch_through": "穿透",
    "reload_speed": "装填", "recoil": "后坐", "zoom": "变焦",
    # 官方名分别是「额外连击数几率」「…% 的几率来获得连击数」（DE 本地化），
    # 旧值「减连击」与官方语义不符（该词条 WM 标记为正向词条）。
    "extra_combo_count": "额外连击", "combo_gain_chance": "连击获取",
}
# 负面语义修饰词
_ANY_MARK = {"任意", "有", "是"}
_NONE_MARK = {"没有", "无", "不"}
_NEG_MARKS = ("带负", "负")

# alias -> (standard, is_combo)
_STAT_LOOKUP: dict[str, str] = {}
for _std, _aliases in RIVEN_STAT_ALIASES.items():
    for _a in _aliases:
        # 短别名（<=2 字符且为中文）允许覆盖，保留最先注册的标准名
        _STAT_LOOKUP.setdefault(_a, _std)
for _combo in RIVEN_STAT_COMBOS:
    _STAT_LOOKUP.setdefault(_combo, _combo)


def segment_stats(blob: str) -> Optional[list[str]]:
    """把无空格连写的词条串（如 基多暴负变焦 的正/负半段）做贪心最长分词。

    成功完整切分时返回标准名列表；否则返回 None（当作普通内容词）。
    """
    n = len(blob)
    if n == 0:
        return []
    memo: dict[int, Optional[list[str]]] = {}

    def dp(i: int) -> Optional[list[str]]:
        if i == n:
            return []
        if i in memo:
            return memo[i]
        best: Optional[list[str]] = None
        # 贪心：先长后短
        for j in range(n, i, -1):
            piece = blob[i:j]
            std = _STAT_LOOKUP.get(piece)
            if std is None:
                continue
            tail = dp(j)
            if tail is None:
                continue
            cand = [std] + tail
            if best is None or len(cand) < len(best):
                best = cand
                if len(best) == 1:
                    break
        memo[i] = best
        return best

    return dp(0)


@dataclass
class WRQuery:
    """wr / rm 紫卡拍卖查询的二级解析结果。"""

    weapon: str = ""                      # 武器名（原样，供模糊匹配）
    stats: list[str] = field(default_factory=list)          # 要求的正/负词条（内部标准名）
    negatives: list[str] = field(default_factory=list)      # 负面条词条
    require_negative: bool = False        # 带负（任意负面）
    forbid_negative: bool = False         # 无负
    polarity: Optional[str] = None        # madurai/vazarin/naramon/zenurik
    max_price: Optional[int] = None       # 1000p
    max_rerolls: Optional[int] = None     # 零洗/低洗（≤8）
    rerolls_min: Optional[int] = None     # 废洗（≥10）
    status: str = "recent"                # latest(仅游戏中)/recent(+在线)/offline(全部)
    positive_count: Optional[int] = None  # 2+ / 3+
    negative_count: Optional[int] = None  # 2+1 的"1"

    @property
    def stat_filter(self) -> list[str]:
        return self.stats


_POLARITY_MAP = {
    "r槽": "madurai", "v槽": "madurai", "m槽": "madurai", "麦槽": "madurai",
    "d槽": "vazarin",
    "-槽": "naramon", "一槽": "naramon",
    "角槽": "zenurik", "zen槽": "zenurik", "皇槽": "zenurik",
}
_STATUS_MAP = {"最新": "latest", "最近": "recent", "在线": "recent", "离线": "offline"}
_REROLL_WORDS = {"零洗": (0, 0), "低洗": (1, 8), "废洗": (10, None)}
_COUNT_RE = re.compile(r"^(\d)\+([01]?)$")
_PRICE_RE = re.compile(r"^(\d+(?:\.\d+)?)p$", re.I)
_RANK_TOKEN_RE = re.compile(r"^(零|满|\d+)级$")


def parse_wr(content: Iterable[str]) -> WRQuery:
    """解析 wr 内容 token：词条连写、极性槽、价格、洗数、词条数、状态词。"""
    q = WRQuery()
    weapon_parts: list[str] = []
    neg_side = False  # 一旦出现 负/带负，后续词条归入负面
    for tok in list(content):
        low = tok.lower()
        if low in _POLARITY_MAP:
            q.polarity = _POLARITY_MAP[low]
            continue
        if tok in _STATUS_MAP:
            q.status = _STATUS_MAP[tok]
            continue
        m = _PRICE_RE.match(low)
        if m:
            q.max_price = int(float(m.group(1)))
            continue
        if tok in _REROLL_WORDS:
            lo, hi = _REROLL_WORDS[tok]
            q.max_rerolls, q.rerolls_min = hi, lo
            continue
        m = re.match(r"^(\d+)洗$", tok)
        if m:
            q.max_rerolls = int(m.group(1))
            continue
        m = _COUNT_RE.match(tok)
        if m:
            q.positive_count = int(m.group(1))
            if m.group(2):
                q.negative_count = int(m.group(2))
            continue
        if tok in ("任意", "有", "是") and (neg_side or not weapon_parts):
            q.require_negative = True
            continue
        if tok in ("没有", "无", "不") :
            q.forbid_negative = True
            continue
        # —— 词条串：可能带 负/带负 标记，可能正负连写
        segs = _split_negatives(tok)
        if segs is not None:
            seg_matched = True
            for part, is_neg in segs:
                if not part:
                    if is_neg:
                        # 「负」/「带负」单独成词：要求存在任意负面
                        q.require_negative = True
                    continue
                if part in _ANY_MARK:
                    if is_neg:
                        q.require_negative = True
                    continue
                if part in _NONE_MARK:
                    q.forbid_negative = True
                    continue
                stds = segment_stats(part)
                if stds is None:
                    # 该片段无法识别为词条：整个 token 退回武器名
                    seg_matched = False
                    break
                target = q.negatives if is_neg else q.stats
                for std in stds:
                    target.extend(RIVEN_STAT_COMBOS.get(std, [std]))
            if seg_matched:
                neg_side = neg_side or any(is_neg for _, is_neg in segs)
                continue
        weapon_parts.append(tok)
    q.weapon = " ".join(weapon_parts).strip()
    return q


def _split_negatives(tok: str) -> Optional[list[tuple[str, bool]]]:
    """把 token 按 负/带负 切成 [(片段, 是否负面), ...]。

    仅当 token 含负面标记、或可完整分词为词条时才算词条串；否则返回 None。
    """
    if not re.search(r"[\u4e00-\u9fffA-Za-z]", tok):
        return None
    if any(m in tok for m in _NEG_MARKS):
        parts: list[tuple[str, bool]] = []
        neg = False
        buf = ""
        i = 0
        while i < len(tok):
            if tok.startswith("带负", i):
                if buf:
                    parts.append((buf, neg))
                    buf = ""
                neg = True
                i += 2
            elif tok[i] == "负":
                if buf:
                    parts.append((buf, neg))
                    buf = ""
                neg = True
                i += 1
            else:
                buf += tok[i]
                i += 1
        if buf:
            parts.append((buf, neg))
        if not parts and neg:
            # 「负」/「带负」单独成词：负面侧为空
            parts.append(("", True))
        return parts
    # 无负面标记：整词必须是（组合）词条才吃掉
    if tok in _STAT_LOOKUP or segment_stats(tok):
        return [(tok, False)]
    return None


# ---------------------------------------------------------------------------
# wm 二级解析
# ---------------------------------------------------------------------------
@dataclass
class WMQuery:
    """wm 普通物品买卖单查询。"""

    item: str = ""                 # 物品名（EN 或 CN 别名）
    buy: bool = False              # 收购（查看收购单）
    group_buy: Optional[list[tuple[str, int]]] = None  # 合购 [(item, qty)]
    quantity: Optional[int] = None  # 3个 -> 同时出售 >= 3
    rank: Optional[int] = None      # 零级/满级/3级；满级用 -1 表示
    rank_word: str = ""             # 原始 rank 词（展示用）
    refinement: Optional[str] = None  # 完整/优良/无暇/光辉
    moran: bool = False             # 墨染
    part: str = ""                  # 部件关键词（蓝图/机体/系统/头部/配件…）


# ★ 部件关键词（2026-09-19 用户反馈「wm 母牛 蓝图」搜出来全是整套）：
#   具体部件词（机体/头部/系统/枪管/枪机/枪托/头盔/蓝图/总图）命中后，
#   查询会切到该部件的订单而不是整套；「配件/部件」泛指 → 保留整套 +
#   部件参考价。★ 具体词优先于「蓝图」——「机体蓝图」是机体，不是总图。
_PART_SPECIFIC = ("头部神经", "机体", "头部", "系统", "枪管", "枪机", "枪托", "头盔")
_PART_GENERIC = ("蓝图", "总图")
_PART_ANY = ("配件", "部件")
# 跨 token 的部件词优先级：具体部件词（2）> 蓝图/总图（1）> 配件泛指（0）。
# 「母牛 头部神经光元 蓝图」里的「蓝图」不该把「头部」顶掉 —— 蓝图只是
# 对同一部件的限定。
_PART_STRENGTH = {"蓝图": 1, "配件": 0}
for _w in _PART_SPECIFIC:
    _PART_STRENGTH[("头部" if _w in ("头部", "头部神经") else _w)] = 2


def _extract_part(tok: str) -> tuple[str, str]:
    """从 token 里抽出部件关键词，返回 (剩余文本, 部件键)。

    「母牛蓝图」→ ("母牛", "蓝图")；「机体蓝图」→ ("", "机体")；
    「母牛」→ ("母牛", "")。剩余文本为空表示整个 token 都是部件词。
    命中具体部件后，同 token 里的限定词（蓝图/总图/神经光元…）一并剥掉
    ——「母牛头部神经光元蓝图」剩余的就是「母牛」。
    """
    rest = tok
    part = ""
    for w in _PART_SPECIFIC:
        if w in rest:
            part = ("头部" if w in ("头部", "头部神经") else w)
            rest = rest.replace(w, "", 1)
            break
    if not part:
        for w in _PART_GENERIC:
            if w in rest:
                part = "蓝图"
                rest = rest.replace(w, "", 1)
                break
    if not part:
        for w in _PART_ANY:
            if w in rest:
                part = "配件"
                rest = rest.replace(w, "", 1)
                break
    if part:
        # 剥掉同 token 里残留的部件限定词（「机体蓝图」「头部神经光元」…）
        for w in (_PART_GENERIC + _PART_ANY
                  + ("神经光元", "神经元", "光元")):
            rest = rest.replace(w, "")
        rest = rest.strip()
    return rest, part


_REFINEMENT_MAP = {"完整": "intact", "优良": "exceptional",
                   "无暇": "flawless", "光辉": "radiant"}


def parse_wm(content: Iterable[str], preset: Optional[str] = None) -> WMQuery:
    q = WMQuery()
    item_parts: list[str] = []
    tokens = list(content)
    if preset:
        tokens.insert(0, preset)
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok == "收购":
            q.buy = True
        elif tok == "合购":
            i += 1
            if i < len(tokens) and ("," in tokens[i] or "*" in tokens[i]):
                q.group_buy = _parse_group_list(tokens[i])
            else:
                item_parts.append(tok)
        elif re.fullmatch(r"(\d+)个", tok):
            q.quantity = int(tok[:-1])
        elif tok in ("零级", "满级") or _RANK_TOKEN_RE.match(tok):
            q.rank_word = tok
            q.rank = 0 if tok == "零级" else (-1 if tok == "满级" else int(tok[:-1]))
        elif tok in _REFINEMENT_MAP:
            q.refinement = _REFINEMENT_MAP[tok]
        elif tok == "墨染":
            q.moran = True
        else:
            rest, part = _extract_part(tok)
            if part and _PART_STRENGTH[part] >= _PART_STRENGTH.get(q.part, -1):
                q.part = part   # 具体部件 > 蓝图 > 配件（跨 token 不被弱词覆盖）
            if rest:
                item_parts.append(rest)
        i += 1
    q.item = " ".join(item_parts).strip()
    return q


def _parse_group_list(blob: str) -> list[tuple[str, int]]:
    out: list[tuple[str, int]] = []
    for part in re.split(r"[,，]", blob):
        part = part.strip()
        if not part:
            continue
        if "*" in part:
            name, _, cnt = part.partition("*")
            out.append((name.strip(), int(cnt or 1)))
        else:
            out.append((part, 1))
    return out


# ---------------------------------------------------------------------------
# 裂隙筛选（裂隙/蹲 共用）
# ---------------------------------------------------------------------------
MISSION_CN: dict[str, str] = {
    # EN -> CN（展示 + 反查共用）。中文名以 **DE 官方简中**为准 ——
    # `de_worldstate.mission_type()` 输出的就是这套（游戏内文本），
    # 两边必须同名，`FissureFilter` 才匹配得上。
    "capture": "捕获", "exterminate": "歼灭", "survival": "生存",
    "defense": "防御",     "interception": "拦截", "extermination": "歼灭",
    "defection": "叛逃", "infested salvage": "感染打捞",
    "recovery": "回收", "pursuit": "追击", "salvage": "打捞",
    "mobile defense": "移动防御",
    "mobiledefense": "移动防御", "rescue": "救援", "spy": "间谍",
    "sabotage": "破坏", "excavation": "挖掘", "disruption": "中断",
    "assassination": "刺杀", "hijack": "劫持", "assault": "强袭",
    "skirmish": "遭遇战", "rush": "冲刺", "void flood": "虚空洪流",
    "voidflood": "虚空洪流", "void cascade": "虚空覆涌",
    "voidcascade": "虚空覆涌",
    "orphix": "奥菲克斯", "alchemy": "元素转换", "mirror defense": "镜像防御",
    "eidelonhunt": "夜灵狩猎", "hunt": "狩猎",
    # ↓ DE ExportMissionTypes 有、早期手写表漏掉的类型（补全，避免展示/筛选落空）
    "legacyte harvest": "传承种收割", "ascension": "扬升", "hive": "清巢",
    "free roam": "自由漫游", "conclave": "武形秘仪",
    "void armageddon": "虚空决战", "descent": "沉沦之地",
    "endless duviri": "无尽回廊", "sanctuary onslaught": "圣殿突袭",
    "offering defense": "祈运坛防御", "vaults": "衰退室",
    "dark sector": "黑暗地带战争", "rathuum": "竞技场",
    "junction": "星际航道结合点", "pvpve": "对战", "tau war": "佩里塔叛乱",
    "paint flood": "Follie 的狩猎", "unknown": "未知",
}
_CN_TO_MISSION: dict[str, str] = {}
for _en, _cn in MISSION_CN.items():
    _CN_TO_MISSION.setdefault(_cn, _en)
# 同一任务在 DE 表里有多个官方中文写法（MT_PURIFY 的官方名是「INFESTED 资源回收」，
# 与更早的「感染打捞」并存）——都登记，避免筛选/展示落空。
_CN_TO_MISSION["INFESTED 资源回收"] = "infested salvage"
# 旧手写译名 / 社区叫法：仍然接受，用户按老习惯输入也能筛到
_CN_TO_MISSION.update({
    "营救": "rescue", "炼金": "alchemy", "虚空级联": "void cascade",
    "追猎": "pursuit", "突袭": "assault",
})

# 遗物纪元中文名（DE 官方简中，见 /Lotus/Language/Relics/Era_*）
# ⚠️ Omnia（全能）与 Requiem（安魂）是两个不同的纪元，绝不能都译成「安魂」——
#    旧实现把 Omnia 也写成「安魂」，于是扎里曼/联结生存的裂隙全部显示成安魂裂缝。
TIER_CN = {"Lith": "古纪", "Meso": "前纪", "Neo": "中纪", "Axi": "后纪",
           "Requiem": "安魂", "Omnia": "全能", "Vanguard": "先锋",
           "古纪": "Lith", "前纪": "Meso", "中纪": "Neo", "后纪": "Axi",
           "安魂": "Requiem", "全能": "Omnia", "先锋": "Vanguard"}
_TIER_KEYS = ("Lith", "Meso", "Neo", "Axi", "Requiem", "Omnia")


@dataclass
class FissureFilter:
    """裂隙筛选条件：多个分组任一命中即可（OR）。"""

    groups: list[dict] = field(default_factory=list)

    def match(self, fissure: dict) -> bool:
        if not self.groups:
            return True
        return any(self._match_one(g, fissure) for g in self.groups)

    @staticmethod
    def _mission_keys(f: dict) -> set[str]:
        """裂隙字典的任务类型 -> 可与筛选比对的键集合。

        裂隙字典里 `missionType` 是**中文**（DE 官方简中），而筛选条件里存的是
        **英文标准键**，两边直接比会永远不命中 —— 这里把中文反查回英文。
        """
        keys: set[str] = set()
        for field in ("missionKey", "missionType"):
            v = f.get(field)
            if isinstance(v, str) and v.strip():
                keys.add(v.strip().lower())
        mt = f.get("missionType")
        if isinstance(mt, str) and mt.strip():
            en = _CN_TO_MISSION.get(mt.strip())
            if en:
                keys.add(en)
        return keys

    @staticmethod
    def _match_one(g: dict, f: dict) -> bool:
        if g.get("hard") is not None and bool(f.get("isHard", False)) != g["hard"]:
            return False
        if g.get("storm") is not None and bool(f.get("isStorm", False)) != g["storm"]:
            return False
        # 虚空星系：节点中文名带「（虚空）」后缀（_node_name 拼星球名），
        # 兼容 DE 英文键含 void 的写法
        if g.get("void") and "虚空" not in (f.get("node") or "") \
                and "void" not in (f.get("nodeKey") or "").lower() \
                and "void" not in (f.get("missionKey") or "").lower():
            return False
        if g.get("missions") and not (FissureFilter._mission_keys(f) & g["missions"]):
            return False
        if g.get("tiers") and f.get("tier") not in g["tiers"]:
            return False
        node = (f.get("node") or "").lower()
        if g.get("substr") and g["substr"].lower() not in node:
            return False
        return True

    def describe(self) -> str:
        if not self.groups:
            return "全部裂隙"
        parts = []
        for g in self.groups:
            seg = []
            if g.get("hard"):
                seg.append("钢铁")
            if g.get("storm"):
                seg.append("九重天")
            if g.get("void"):
                seg.append("虚空")
            if not g.get("hard") and not g.get("storm") and not g.get("void") \
                    and (g.get("missions") or g.get("tiers")):
                seg.append("普通")
            if g.get("missions"):
                seg.append("/".join(MISSION_CN.get(m, m) for m in sorted(g["missions"])))
            if g.get("tiers"):
                seg.append("/".join(TIER_CN.get(t, t) for t in g["tiers"]))
            if g.get("substr"):
                seg.append(g["substr"])
            parts.append("".join(seg) or "全部")
        return "，".join(parts)


def parse_fissure_filter(text: str) -> FissureFilter:
    """解析 `普通捕获,钢铁虚空生存,九重天拦截` 这类筛选串。"""
    flt = FissureFilter()
    for blob in re.split(r"[,，;；]", text.strip()):
        blob = blob.strip()
        if not blob:
            continue
        g: dict = {}
        words = re.split(r"\s+", blob)
        merged = "".join(words)
        # 纪元层级
        tiers = set()
        for cn, en in TIER_CN.items():
            if cn in merged and cn in ("古纪", "前纪", "中纪", "后纪"):
                tiers.add(en)
        if tiers:
            g["tiers"] = tiers
        # 平台修饰（普通 = 排除钢铁/九重天）
        if "钢铁" in merged or "钢路" in merged:
            g["hard"] = True
        elif "普通" in merged:
            g["hard"] = False
        if "九重天" in merged or "empyrean" in merged.lower():
            g["storm"] = True
        elif "普通" in merged:
            g["storm"] = False
        # 任务类型：逐个 CN 词匹配后剔除，剩余部分作为节点/星球子串
        rest = merged
        missions = set()
        for cn, en in sorted(_CN_TO_MISSION.items(), key=lambda kv: -len(kv[0])):
            if cn in rest:
                missions.add(en)
                rest = rest.replace(cn, "")
        for kw in ("钢铁", "钢路", "九重天", "empyrean", "普通"):
            rest = rest.replace(kw, "")
        for cn in ("古纪", "前纪", "中纪", "后纪"):
            rest = rest.replace(cn, "")
        # 地区修饰：「虚空」= 只收虚空星系节点（2026-09-14 修「蹲 虚空捕获
        # 却推了木星捕获」——旧版把「虚空」当纯修饰词剔除，等于没写）。
        # 必须在任务名剔除**之后**再判定：虚空覆涌/虚空洪流这类任务名本身
        # 含「虚空」，不代表用户要限定虚空地区。
        if "虚空" in rest:
            g["void"] = True
        rest = rest.replace("虚空", "")
        if missions:
            g["missions"] = missions
        rest = rest.strip()
        if rest and len(rest) >= 2:
            g["substr"] = rest
        if g:
            flt.groups.append(g)
    return flt


# ---------------------------------------------------------------------------
# 蹲：时长与免打扰时间窗
# ---------------------------------------------------------------------------
_DURATION_UNITS = {
    "小时": 3600, "h": 3600,
    "天": 86400, "日": 86400, "d": 86400,
    "周": 604800, "星期": 604800, "w": 604800,
    "月": 2592000, "yue": 2592000,
    "年": 31536000, "y": 31536000,
}
_DURATION_WORDS = {
    "永久": -1, "长期": -1, "无限": -1,
    "两周": 1209600, "半月": 1296000,
    "一周": 604800, "一天": 86400, "今天": 86400,
}


def parse_duration(text: str) -> Optional[int]:
    """时长 -> 秒数；永久为 -1；缺省 None（命中一次后取消）。"""
    t = text.strip()
    if not t:
        return None
    if t in _DURATION_WORDS:
        return _DURATION_WORDS[t]
    m = re.fullmatch(r"(\d+)\s*(小时|天|日|周|星期|月|年|h|d|w|y)", t, re.I)
    if m:
        return int(m.group(1)) * _DURATION_UNITS[m.group(2).lower()]
    m = re.fullmatch(r"(\d+)天(\d+)小时?", t)
    if m:
        return int(m.group(1)) * 86400 + int(m.group(2)) * 3600
    return None


@dataclass
class TimeWindow:
    """免打扰/允许推送时间窗。空对象表示全天允许。"""

    start: Optional[int] = None   # 小时 0-23
    end: Optional[int] = None     # 小时 0-23
    days: Optional[set[int]] = None  # 星期集合（1=周一 ... 7=周日）
    at_hour: Optional[int] = None    # 每天 HH 点推送

    @property
    def is_always(self) -> bool:
        return self.start is None and self.days is None and self.at_hour is None

    def allows(self, now) -> bool:
        """now: datetime（本地时区）。"""
        if self.days is not None and now.isoweekday() not in self.days:
            return False
        if self.at_hour is not None:
            return now.hour == self.at_hour and now.minute < 30
        if self.start is None:
            return True
        h = now.hour
        if self.start <= self.end:
            return self.start <= h <= self.end
        return h >= self.start or h <= self.end  # 跨零点，如 22到8

    def describe(self) -> str:
        bits = []
        if self.start is not None:
            bits.append(f"{self.start}:00-{self.end}:00")
        if self.days:
            bits.append("周" + "/".join(map(str, sorted(self.days))))
        if self.at_hour is not None:
            bits.append(f"每天{self.at_hour}点")
        return " ".join(bits) or "全天"


def parse_time_window(text: str) -> Optional[TimeWindow]:
    """解析 `0到24`、`22到8`、`每天19点`、`周1/3/5 23点`。"""
    t = text.strip().replace("：", ":")
    if not t:
        return None
    m = re.fullmatch(r"周([1-7](?:[/，][1-7])*)\s*(\d{1,2})点?", t)
    if m:
        days = {int(x) for x in re.split(r"[/，]", m.group(1))}
        return TimeWindow(days=days, start=int(m.group(2)), end=int(m.group(2)))
    m = re.fullmatch(r"每天\s*(\d{1,2})点?", t)
    if m:
        return TimeWindow(at_hour=int(m.group(1)))
    m = re.fullmatch(r"(\d{1,2})到(\d{1,2})(?:点)?", t)
    if m:
        return TimeWindow(start=int(m.group(1)) % 24, end=int(m.group(2)) % 24)
    return None
