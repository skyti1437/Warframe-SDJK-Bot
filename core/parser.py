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
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
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
    "ps": "ps",
    "ps4": "ps",
    "ps5": "ps",
    "playstation": "ps",
    "xb": "xb",
    "xbox": "xb",
    "series": "xb",
    "sw": "sw",
    "switch": "sw",
    "任天堂": "sw",
}
PLATFORM_DISPLAY = {"pc": "国际服", "ps": "国际服", "xb": "国际服", "sw": "国际服"}

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
    # 「声望」：社区黑话 —— 团体声望只能靠刷该地区赏金攒，查声望=查赏金
    #   （2026-10-02 维护者要求；裸「声望」= 赏金一览，「金星声望」等见下方预设表）
    "bounty": {"赏金", "声望"},
    "fissures": {"裂隙", "裂缝"},  # 裂缝：玩家常用写法（2026-10-16 用户口径，原属开核桃）
    "sortie": {"突击"},
    "archon": {"执刑官", "执刑官猎杀", "大猎杀"},
    "voidtrader": {"奸商", "虚空商人", "巴罗"},
    "dailydeals": {"每日特惠", "达沃"},
    "calendar": {"日历", "1999日历"},
    "deeparchimedea": {"深层科研", "深层"},
    "temporalarchimedea": {"时光科研", "时光"},
    "steelpath": {"钢铁之路", "钢精"},
    # ★ 2026-10-03（B1）：中英名称对照（纯文本）
    "translate": {"翻译"},
    # ★ 2026-10-03（A3）：Palladino 裂罅碎块商店（地球「钢铁守望」）
    "slivershop": {"碎银兑换", "碎银"},
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
    "descendia": {"沉沦之地", "炼狱塔", "炼狱", "沉沦"},
    "incursions": {"侵袭", "钢路侵袭", "钢铁侵袭"},
    # ---- 资料与计算器 ----
    "wiki": {"wiki", "wk", "维基"},
    "valence": {"武器融合", "融合"},
    "damage": {"伤害", "伤害计算"},
    "scan": {"识卡", "读卡", "配卡识别", "识图", "识别配卡"},
    "scandamage": {"识卡伤害", "扫伤害"},
    # ---- 市场与查价 ----
    "wm": {"wm"},
    "wr": {"wr", "wmr", "紫卡"},
    "rm": {"rm"},
    "rank": {"排行", "市场排行"},
    "analysis": {"紫卡分析"},
    "trend": {"趋势", "wm趋势", "价格趋势", "紫卡趋势"},
    "openrelic": {"开核桃", "开裂缝"},
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
    "钢铁裂缝": ("fissures", "钢铁"),
    "虚空裂缝": ("fissures", "普通"),
    "九重天裂缝": ("fissures", "九重天"),
    # 赏金/声望的地区预设由下方 _BOUNTY_REGIONS 统一生成
    # （裸地区词 / 「X赏金」/「X声望」三式等价，见 ALIAS_TO_COMMAND 构建处）
    "金垃圾": ("ducats", "金"),
    "银垃圾": ("ducats", "银"),
    "铜垃圾": ("ducats", "铜"),
    # 分类排行
    "甲排行": ("rank", "甲"),
    "战甲排行": ("rank", "甲"),
    "卡排行": ("rank", "卡"),
    "MOD排行": ("rank", "卡"),
    "部件排行": ("rank", "部件"),
    "赋能排行": ("rank", "赋能"),
    "主武排行": ("rank", "主武"),
    "主武器排行": ("rank", "主武"),
    "副武排行": ("rank", "副武"),
    "副武器排行": ("rank", "副武"),
    "近战排行": ("rank", "近战"),
    "遗物排行": ("rank", "遗物"),
    "紫卡排行": ("rank", "紫卡"),
    # 遗物入库 / 出库
    "遗物列表": ("relic", "列表"),
    "遗物入库": ("relic", "入库"),
    "遗物出库": ("relic", "出库"),
    "开核桃速刷": ("openrelic", "速刷"),
}

# 触发词 -> canonical 主指令（统一小写，parse 时按小写匹配）
ALIAS_TO_COMMAND: dict[str, str] = {}
for _cmd, _aliases in COMMAND_ALIASES.items():
    for _a in _aliases:
        ALIAS_TO_COMMAND[_a.lower()] = _cmd

# ---------------------------------------------------------------------------
# 赏金/声望地区词（2026-10-02 维护者要求）：裸地区词 = 地区赏金 = 地区声望，
#   即「地球」「地球赏金」「地球声望」三者等价。键 = 可裸发的地区词；
#   值 = 归并后的地区主词（fmt_bounties._CONTINENT_TARGET 按子串认识这套主词）。
#   三种写法由这一张表统一生成 —— 手写三份必然出现「金星赏金能查、
#   金星声望没反应」的方言断层。
#   ⚠️ 裸词若已被其他主指令占用则跳过（夜灵/平原 → 周期指令），连写不受影响。
# ---------------------------------------------------------------------------
_BOUNTY_REGIONS: dict[str, str] = {
    "地球": "地球",
    "希图斯": "地球",
    "尸鬼": "地球",
    "夜灵": "地球",
    "金星": "金星",
    "山谷": "金星",
    "奥布": "金星",
    "索拉里": "金星",
    "福尔图娜": "金星",
    "火卫二": "火卫二",
    "魔胎": "火卫二",
    "英择谛": "火卫二",
    "隔离库": "火卫二",
    "深矿": "深矿",
    # ★ 2026-10-11：「抢劫」是金星**抢劫（Heist）**奖励池（bounty_pools「抢劫」：陀螺磁抵系统 /
    #   大气系统…），不是深矿；旧映射成「深矿」⇒ 发「抢劫」看到的是深矿档。
    "抢劫": "抢劫",
    "扎里曼": "扎里曼",
    "羽化": "扎里曼",
    "虚空天使": "扎里曼",
    "圣所": "圣所",
    "实验室": "圣所",
    "解剖": "圣所",
    "1999": "1999",
    "六人组": "1999",
    "霍瓦尼亚": "1999",
}
for _w, _region in _BOUNTY_REGIONS.items():
    if _w not in ALIAS_TO_COMMAND:  # 「夜灵」已归周期指令 → 只保留连写形式
        _PRESET_COMMANDS[_w] = ("bounty", _region)
    _PRESET_COMMANDS[_w + "赏金"] = ("bounty", _region)
    _PRESET_COMMANDS[_w + "声望"] = ("bounty", _region)

for _alias, (_cmd, _preset) in _PRESET_COMMANDS.items():
    ALIAS_TO_COMMAND[_alias.lower()] = _cmd
# _PRESET_COMMANDS 自身也小写化，方便 parse() 里按 low 取 preset
_PRESET_COMMANDS = {k.lower(): v for k, v in _PRESET_COMMANDS.items()}

# 无空格连写（2026-10-02 玩家反馈「wm水晶p头」静默无响应）：ASCII 指令别名
# 直接贴着内容时按前缀切开。只收纯 ASCII 指令别名、最长优先（wmr 先于 wm）；
# 中文指令无此输入习惯且误伤面大（「帮助我」「趋势图」这类正文词），不参与。
_ASCII_CMD_ALIASES: tuple[str, ...] = tuple(
    sorted((a for a in ALIAS_TO_COMMAND if a.isascii() and a.isalpha()), key=len, reverse=True)
)


# ---------------------------------------------------------------------------
# 解析结果
# ---------------------------------------------------------------------------
@dataclass
class Parsed:
    """一条用户消息的解析产物。"""

    raw: str = ""
    command: Optional[str] = None  # canonical 主指令
    command_raw: Optional[str] = None  # 原始触发词
    preset: Optional[str] = None  # 别名携带的预设内容（如 金垃圾→"金"）
    content: list[str] = field(default_factory=list)  # 查询内容 token
    modifiers: list[str] = field(default_factory=list)  # 已识别修饰符原文
    platform: Optional[str] = None  # -pc/-ps/...
    text_mode: bool = False  # -w / -1
    image_mode: bool = False  # -t
    whisper: bool = False  # -r
    page: int = 1  # -2 / -3 ...
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
        if res.command is None:
            # 无空格连写：「wm水晶p头」= wm + 水晶p头（ASCII 指令前缀，最长优先）
            for pre in _ASCII_CMD_ALIASES:
                if low.startswith(pre) and len(low) > len(pre):
                    res.command_raw = pre
                    res.command = alias_table[pre]
                    if pre in _PRESET_COMMANDS:
                        res.preset = _PRESET_COMMANDS[pre][1]
                    res.content.append(tok[len(pre) :])
                    break
            if res.command is not None:
                continue
        res.content.append(tok)
    return res


# ---------------------------------------------------------------------------
# 紫卡词条词表（标准词条 -> 可识别短名/别名）
# 键为内部标准名（与 WM 属性 url 对齐），值为别名集合。
# 依据社区通行的紫卡词条表整理，并补充单字缩写。
# ---------------------------------------------------------------------------
RIVEN_STAT_ALIASES: dict[str, set[str]] = {
    # ★ 2026-10-07 Riven Splicer（Update 44，实机 2026-10-07 上线）的 18 个**新词条**：
    #   官方 Public Export 七家族 tag 并集 36→54 新增的 18 个 tag，与 wiki「Spliced Traits」
    #   清单一一对应。**必须注册** —— 否则会被静默错绑（实测：病毒伤害→毒素伤害、
    #   毒气伤害→毒素伤害、爆炸伤害→暴击伤害、弹药效率→弹药上限、弱点暴击→暴击几率）
    #   或静默丢弃（⇒ 词条数少算、系数错档、区间偏高约 32%）。
    #   ⚠ 中文名按既有卡面写法推导（「弹药效率」为官方语言表实证），**待一张实卡复核**。
    # ★ 2026-10-10：RIVEN_STAT_ZH 的卡面短名（O伤/S伤/T伤/弱点伤/处决伤）必须也是别名 ——
    #   wr 卡上显示什么，玩家就照抄什么；漏了会被并进武器名（「wr 空刃 弱点伤」→ 武器
    #   「空刃 弱点伤」）。守卫见 tests/test_riven_stat_i18n.py 第 12 段。
    "gas_damage": {"毒气伤害", "毒气伤", "毒气", "Gas Damage", "Gas"},
    "corrosive_damage": {"腐蚀伤害", "腐蚀伤", "腐蚀", "Corrosive Damage", "Corrosive"},
    "viral_damage": {"病毒伤害", "病毒伤", "病毒", "Viral Damage", "Viral"},
    "radiation_damage": {"辐射伤害", "辐射伤", "辐射", "Radiation Damage", "Radiation"},
    "blast_damage": {"爆炸伤害", "爆炸伤", "爆炸", "Blast Damage", "Blast"},
    "magnetic_damage": {"磁力伤害", "磁力伤", "磁力", "Magnetic Damage", "Magnetic"},
    "damage_vs_orokin": [
        "对奥罗金的伤害",
        "对奥罗金伤害",
        "对Orokin伤害",
        "对 Orokin 伤害",
        "对奥罗金",
        "对O伤",
        "O伤",
        "O系",
        "O系伤害",
        "O歧视",
        "O",
        "奥罗金伤害",
        "奥罗金歧视",
        "对Orokin的伤害",
        "Orokin Damage",
        "Damage to Orokin",
        "Damage vs Orokin",
    ],
    "damage_vs_scaldra": [
        "对炽蛇军的伤害",
        "对炽蛇军伤害",
        "对Scaldra伤害",
        "对 Scaldra 伤害",
        "对炽蛇军",
        "对S伤",
        "S伤",
        "S系",
        "S系伤害",
        "S歧视",
        "S",
        "炽蛇军伤害",
        "炽蛇军歧视",
        "对Scaldra的伤害",
        "Scaldra Damage",
        "Damage to Scaldra",
        "Damage vs Scaldra",
    ],
    "damage_vs_techrot": [
        "对科腐者的伤害",
        "对科腐者伤害",
        "对Techrot伤害",
        "对 Techrot 伤害",
        "对科腐者",
        "对T伤",
        "T伤",
        "T系",
        "T系伤害",
        "T歧视",
        "T",
        "科腐者伤害",
        "科腐者歧视",
        "对Techrot的伤害",
        "Techrot Damage",
        "Damage to Techrot",
        "Damage vs Techrot",
    ],
    "weakpoint_damage": [
        "弱点伤害",
        "弱点",
        "弱伤",
        "弱点伤",
        "Weakpoint Damage",
        "Weak Point Damage",
    ],
    "weakpoint_crit_chance": [
        "弱点暴击几率",
        "弱点暴击率",
        "命中弱点暴击几率",
        "弱点暴击",
        "弱点暴",
        "弱爆",
        "弱点爆击",
        "弱点爆击几率",
        "弱点暴率",
        "弱点爆率",
        "Weakpoint Critical Chance",
        "Weak Point Critical Chance",
    ],
    "status_damage": [
        "异常状态伤害",
        "状态伤害",
        "异常伤害",
        "异常状态",
        "异常伤",
        "状态伤",
        "异常",
        "异",
        "Status Damage",
        "异常状态的伤害",
    ],
    "ammo_efficiency": ["弹药效率", "弹效", "弹药效能", "Ammo Efficiency"],
    "reload_holstered": [
        "收起武器时弹匣每秒自动装填",
        "收起时装填",
        "收枪装填",
        "收枪",
        "收起武器时自动装填",
        "收起武器时装填",
        "收起装填",
        "收枪装弹",
        "后台装填",
        "Reload While Holstered",
        "Holster Reload",
    ],
    "melee_heavy_attack_damage": [
        "在重击时的近战伤害",
        "重击伤害",
        "重击",
        "重击伤",
        "近战重击伤害",
        "重攻击伤害",
        "Heavy Attack Damage",
        "Melee Damage on Heavy Attack",
    ],
    "melee_heavy_attack_charge": [
        "重击准备速度",
        "重击蓄力速度",
        "重击蓄力",
        "重击准备",
        "重击速度",
        "重击准备速率",
        "重击蓄力速率",
        "Heavy Attack Charge",
        "Heavy Attack Wind Up Speed",
    ],
    "melee_parry_angle": [
        "招架角度",
        "格挡角度",
        "格挡范围",
        "格挡",
        "招架",
        "Parry Angle",
        "Block Angle",
    ],
    "melee_slam_damage": [
        "震地攻击伤害",
        "震地伤害",
        "震地",
        "震地伤",
        "Slam Damage",
        "Slam Attack Damage",
    ],
    "melee_damage": {
        "近战伤害",
        "基伤",
        "基础伤害",
        "近战",
        "基础",
        "基",
        "伤害",
        "Damage",
        "Base Damage",
        "Melee Damage",
        "DMG",
    },
    "crit_chance": {
        "暴击几率",
        "暴率",
        "暴击率",
        "爆击率",
        "爆率",
        "爆击几率",
        "暴击概率",
        "爆击概率",
        "暴击",
        "爆击",
        "暴",
        "爆",
        "爆率几率",
        "暴几率",
        "Critical Chance",
        "Crit Chance",
        "CC",
    },
    "crit_damage": {
        "暴击伤害",
        "暴伤",
        "爆伤",
        "爆击伤害",
        "暴击伤",
        "爆击伤",
        "Critical Damage",
        "Crit Damage",
        "CD",
    },
    "multishot": {"多重射击", "多重", "多", "Multishot", "Multi Shot", "MS"},
    "attack_speed": {"攻击速度", "攻击速率", "攻速", "速度", "Attack Speed", "AS"},
    "fire_rate": {"射击速度", "射击速率", "射速", "Fire Rate", "Firerate", "FR"},
    "status_chance": {
        "触发几率",
        "触发",
        "触率",
        "触发概率",
        "触发率",
        "触",
        "异常几率",
        "异常概率",
        "异常触发几率",
        "异常状态几率",
        "状态触发几率",
        "Status Chance",
        "SC",
    },
    "status_duration": {
        "触发时间",
        "触时",
        "触发持续时间",
        "触发时长",
        "异常持续时间",
        "异常状态持续时间",
        "异常时间",
        "异常时长",
        "状态持续时间",
        "Status Duration",
    },
    "range": {"攻击范围", "范围", "近战范围", "近战攻击范围", "Range", "Melee Range"},
    "initial_combo": {"初始连击", "初始连击数", "初始", "初始连击点数", "连击数", "Initial Combo"},
    "combo_duration": {"连击持续时间", "连击时间", "连击时长", "连击持续", "Combo Duration"},
    "heavy_attack_efficiency": {"重击效率", "重击效能", "重击连击效率", "Heavy Attack Efficiency"},
    # ★ 2026-10-02 归并（口径统一）：原 combo_efficiency（「连击效率」）在 WM 32
    #   属性与官方基值表里都没有对应行，基值却等于「额外连击数几率」的 58.77 ——
    #   属早期误录的幽灵 sid。按维护者决定归并到额外连击数几率，这两个词不再单列。
    "finisher_damage": {"处决伤害", "处决", "处决伤", "终结伤害", "终结伤", "Finisher Damage"},
    "slide_crit": {
        "滑行攻击时暴击率",
        "滑暴",
        "滑行暴击几率",
        "滑行暴击",
        "滑爆",
        "滑行爆击",
        "滑行",
        "滑行攻击",
        # ★ 2026-09-24：卡面原文就是「滑行攻击暴击几率」——不含「滑暴」
        #   子串，包含匹配会落到短名「暴击」(crit_chance)，让反推倾向
        #   整卡作废（滑行暴击基值 180 被当普通暴击用不上）。
        "滑行攻击暴击几率",
        "滑行攻击暴击率",
        "滑行攻击爆击几率",
        "滑行暴击率",
        "滑行爆击率",
        "滑行攻击爆击率",
        "滑铲暴击",
        "滑铲暴击率",
        "Slide Critical Chance",
        "Critical Chance on Slide Attack",
    },
    "slash_damage": {"切割伤害", "切割", "切割伤", "切伤", "切", "Slash Damage", "Slash"},
    "impact_damage": {"冲击伤害", "冲击", "冲击伤", "冲伤", "冲", "Impact Damage", "Impact"},
    "puncture_damage": {"穿刺伤害", "穿刺", "穿刺伤", "刺伤", "Puncture Damage", "Puncture"},
    "heat_damage": {"火焰伤害", "火伤", "火", "火焰", "火焰伤", "火伤害", "Heat Damage"},
    "cold_damage": {"冰冻伤害", "冰伤", "冰", "冰冻", "冰冻伤", "冰伤害", "Cold Damage", "Cold"},
    "toxin_damage": {"毒素伤害", "毒伤", "毒", "毒素", "毒素伤", "毒伤害", "Toxin Damage", "Toxin"},
    "electric_damage": {
        "电击伤害",
        "电伤",
        "电",
        "电击",
        "电击伤",
        "电伤害",
        "电气伤害",
        "Electric Damage",
        "Electricity",
    },
    "damage_vs_grineer": {
        "对Grineer伤害",
        "G系伤害",
        "G伤",
        "Grineer伤害",
        "G佬伤害",
        "G系",
        "G佬",
        "G歧视",
        "G",
        # ★ 2026-09-27：卡面原文带「的」（「对 Grineer 的伤害」），
        #   逐行解析读到的是卡面原文，不带这个别名会整条认不出。
        "对Grineer的伤害",
        "对G伤",
        "对Grineer",
        "Damage to Grineer",
        "Damage vs Grineer",
    },
    "damage_vs_corpus": {
        "对Corpus伤害",
        "C系伤害",
        "C伤",
        "Corpus伤害",
        "C佬伤害",
        "C系",
        "C佬",
        "C歧视",
        "C",
        "对Corpus的伤害",
        "对C伤",
        "对Corpus",
        "Damage to Corpus",
        "Damage vs Corpus",
    },
    "damage_vs_infested": {
        "对Infested伤害",
        "I系伤害",
        "I伤",
        "Infested伤害",
        "I佬伤害",
        "I系",
        "I佬",
        "I歧视",
        "I",
        "对Infested的伤害",
        "对I伤",
        "对Infested",
        "Damage to Infested",
        "Damage vs Infested",
    },
    "magazine_capacity": {
        "弹匣容量",
        "弹匣",
        "弹夹",
        "弹夹容量",
        "弹容",
        "弹匣上限",
        "Magazine Capacity",
        "Magazine Size",
        "Magazine",
    },
    "ammo_max": {
        "弹药最大值",
        "弹药",
        "弹药上限",
        "最大弹药",
        "最大弹药量",
        "备弹",
        "备弹量",
        "Ammo Maximum",
        "Max Ammo",
        "Ammo Max",
    },
    "projectile_speed": {
        "投射物速度",
        "弹道",
        "投射",
        "弹道飞行速度",
        "弹道速度",
        "弹速",
        "飞行速度",
        "投射物飞行速度",
        "Projectile Speed",
        "Projectile Flight Speed",
        "Flight Speed",
    },
    "punch_through": {"穿透", "穿透深度", "穿透距离", "Punch Through", "Punchthrough"},
    "reload_speed": {
        "装填速度",
        "装填",
        "装弹",
        "装填速率",
        "装弹速度",
        "换弹",
        "换弹速度",
        "Reload Speed",
        "Reload",
    },
    "recoil": {"武器后坐力", "后坐", "后坐力", "后座", "后座力", "Recoil", "Weapon Recoil"},
    "zoom": {"变焦", "缩放", "瞄准变焦", "瞄准倍率", "瞄准缩放", "Zoom"},
    "extra_combo_count": {
        "额外连击数",
        "额外连击",
        "额外连击数几率",
        "减连击获取",
        "减额外连击数几率",
        "减额外连击",
        "减额外连击数",
        # ★ 2026-10-02 归并自原 combo_efficiency 幽灵 sid
        "连击效率",
        "近战连击效率",
        "额外连击几率",
        "额外连击概率",
        "额外连击率",
        "Chance to Gain Extra Combo Count",
        "Extra Combo Count Chance",
    },
    # WM slug chance_to_gain_combo_count（仅负向；DE 官方文案「…% 的几率来获得连击数」）
    "combo_gain_chance": {
        "连击数获取几率",
        "获得连击数几率",
        "连击获取几率",
        "连击获取",
        "连击数获取",
        "获得连击数",
        "连击数几率",
        # ★ 2026-10-02：中文 wiki 基值表里该词条（104.85%，
        #   「³ 仅负向」）的中文行名 —— 缺它会整行「认不出」
        "几率不获得连击数",
        "不获得连击数几率",
        #   用户实卡（翁 Locti-acrium）卡面原文：
        #   「-60.4% 的几率来获得连击数」（剥数值后如下）
        "的几率来获得连击数",
        "几率来获得连击数",
        "来获得连击数",
        "连击获取概率",
        "获得连击几率",
        "不获得连击的几率",
        "Chance to Gain Combo Count",
    },
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
    # ★ 2026-10-07：WM `/v2/riven/attributes` 已补上 Splicer 的 18 个新词条（32→50 条，
    #   zh 名与我方逐字一致）⇒ `wr`（拍卖）侧同步映射，否则新词条会被丢成裸 slug。
    "gas_damage": "gas",
    "corrosive_damage": "corrosive",
    "viral_damage": "viral",
    "radiation_damage": "radiation",
    "blast_damage": "blast",
    "magnetic_damage": "magnetic",
    "damage_vs_orokin": "damage_to_orokin",
    "damage_vs_scaldra": "damage_to_scaldra",
    "damage_vs_techrot": "damage_to_techrot",
    "weakpoint_damage": "weak_point_damage",
    "weakpoint_crit_chance": "weak_point_critical_chance",
    "status_damage": "status_damage",
    "ammo_efficiency": "ammo_efficiency",
    "reload_holstered": "magazine_reloaded_s_when_holstered",
    "melee_heavy_attack_damage": "melee_damage_on_heavy_attack",
    "melee_heavy_attack_charge": "heavy_attack_wind_up_speed",
    "melee_parry_angle": "parry_angle",
    "melee_slam_damage": "slam_attack_damage",
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
    # ★ 2026-10-07 Riven Splicer（Update 44，实机 2026-10-07 上线）的 18 个**新词条**：
    #   官方 Public Export 七家族 tag 并集 36→54 新增的 18 个 tag，与 wiki「Spliced Traits」
    #   清单一一对应。**必须注册** —— 否则会被静默错绑（实测：病毒伤害→毒素伤害、
    #   毒气伤害→毒素伤害、爆炸伤害→暴击伤害、弹药效率→弹药上限、弱点暴击→暴击几率）
    #   或静默丢弃（⇒ 词条数少算、系数错档、区间偏高约 32%）。
    #   ⚠ 中文名按既有卡面写法推导（「弹药效率」为官方语言表实证），**待一张实卡复核**。
    "gas_damage": "毒气",
    "corrosive_damage": "腐蚀",
    "viral_damage": "病毒",
    "radiation_damage": "辐射",
    "blast_damage": "爆炸",
    "magnetic_damage": "磁力",
    "damage_vs_orokin": "O伤",
    "damage_vs_scaldra": "S伤",
    "damage_vs_techrot": "T伤",
    "weakpoint_damage": "弱点伤",
    "weakpoint_crit_chance": "弱点暴",
    "status_damage": "异常伤",
    "ammo_efficiency": "弹效",
    "reload_holstered": "收枪装填",
    "melee_heavy_attack_damage": "重击伤",
    "melee_heavy_attack_charge": "重击速度",
    "melee_parry_angle": "招架",
    "melee_slam_damage": "震地伤",
    "melee_damage": "基伤",
    "crit_chance": "暴击",
    "crit_damage": "暴伤",
    "multishot": "多重",
    "attack_speed": "攻速",
    "fire_rate": "射速",
    "status_chance": "触发",
    "status_duration": "触时",
    "range": "范围",
    "initial_combo": "初始连击",
    "combo_duration": "连击时间",
    "heavy_attack_efficiency": "重击效率",
    "finisher_damage": "处决伤",
    "slide_crit": "滑暴",
    "slash_damage": "切割",
    "impact_damage": "冲击",
    "puncture_damage": "穿刺",
    "heat_damage": "火伤",
    "cold_damage": "冰伤",
    "toxin_damage": "毒伤",
    "electric_damage": "电伤",
    "damage_vs_grineer": "G伤",
    "damage_vs_corpus": "C伤",
    "damage_vs_infested": "I伤",
    "magazine_capacity": "弹匣",
    "ammo_max": "弹药",
    "projectile_speed": "弹道",
    "punch_through": "穿透",
    "reload_speed": "装填",
    "recoil": "后坐",
    "zoom": "变焦",
    # 官方名分别是「额外连击数几率」「…% 的几率来获得连击数」（DE 本地化），
    # 旧值「减连击」与官方语义不符（该词条 WM 标记为正向词条）。
    "extra_combo_count": "额外连击",
    "combo_gain_chance": "连击获取",
}
# 负面语义修饰词
_ANY_MARK = {"任意", "有", "是"}
_NONE_MARK = {"没有", "无", "不"}
_NEG_MARKS = ("带负", "负")

# alias -> (standard, is_combo)
_STAT_LOOKUP: dict[str, str] = {}
for _std, _aliases in RIVEN_STAT_ALIASES.items():
    for _a in _aliases:
        # 保留原文键供展示名回读；英文大小写统一后也指向同一词条。
        _STAT_LOOKUP.setdefault(_a, _std)
        _STAT_LOOKUP.setdefault(_a.casefold(), _std)
for _combo in RIVEN_STAT_COMBOS:
    _STAT_LOOKUP.setdefault(_combo, _combo)
# 最长别名长度：分词时片段不必试得比它更长
_STAT_KEY_MAX = max(map(len, _STAT_LOOKUP))
_STAT_WORD_MAX = max(len(alias.split()) for alias in _STAT_LOOKUP)
# 连写词条串长度上限：紫卡最多 4 条词条，再长不可能是词条串（直接当普通内容词），
# 也挡住超长 token 让递归分词卡住事件循环 / 递归过深（2026-10-10 实测 4×950 字 3.7 s）
_SEGMENT_MAX_LEN = 64


def segment_stats(blob: str) -> Optional[list[str]]:
    """把无空格连写的词条串（如 基多暴负变焦 的正/负半段）做贪心最长分词。

    成功完整切分时返回标准名列表；否则返回 None（当作普通内容词）。
    """
    blob = blob.casefold()
    n = len(blob)
    if n == 0:
        return []
    if n > _SEGMENT_MAX_LEN:
        return None
    if blob.isascii():
        # 英文只接受完整别名，不能把武器单词拆成多个字母缩写。
        std = _STAT_LOOKUP.get(blob)
        return [std] if std is not None else None
    memo: dict[int, Optional[list[str]]] = {}

    def dp(i: int) -> Optional[list[str]]:
        if i == n:
            return []
        if i in memo:
            return memo[i]
        best: Optional[list[str]] = None
        # 贪心：先长后短
        for j in range(min(n, i + _STAT_KEY_MAX), i, -1):
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

    weapon: str = ""  # 武器名（原样，供模糊匹配）
    stats: list[str] = field(default_factory=list)  # 要求的正/负词条（内部标准名）
    negatives: list[str] = field(default_factory=list)  # 负面条词条
    require_negative: bool = False  # 带负（任意负面）
    forbid_negative: bool = False  # 无负
    polarity: Optional[str] = None  # madurai/vazarin/naramon/zenurik
    max_price: Optional[int] = None  # 1000p
    min_price: Optional[int] = None  # 500p以上（2026-10-01 补占位；服务端忽略
    # price_min/max ⇒ 消费端 _auction_match 本地过滤）
    max_rerolls: Optional[int] = None  # 零洗/低洗（≤8）
    rerolls_min: Optional[int] = None  # 废洗（≥10）
    status: str = "recent"  # latest(仅游戏中)/recent(+在线)/offline(全部)
    positive_count: Optional[int] = None  # 2+ / 3+
    negative_count: Optional[int] = None  # 2+1 的"1"
    unrecognized_stats: list[str] = field(default_factory=list)  # 带负面标记却无法完整解析

    @property
    def stat_filter(self) -> list[str]:
        return self.stats


_POLARITY_MAP = {
    "r槽": "madurai",
    "v槽": "madurai",
    "m槽": "madurai",
    "麦槽": "madurai",
    "d槽": "vazarin",
    "-槽": "naramon",
    "一槽": "naramon",
    "角槽": "zenurik",
    "zen槽": "zenurik",
    "皇槽": "zenurik",
}
_STATUS_MAP = {"最新": "latest", "最近": "recent", "在线": "recent", "离线": "offline"}
_REROLL_WORDS = {"零洗": (0, 0), "低洗": (1, 8), "废洗": (10, None)}
_COUNT_RE = re.compile(r"^(\d)\+([01]?)$")
_PRICE_RE = re.compile(r"^(\d+(?:\.\d+)?)p$", re.I)
_RANK_TOKEN_RE = re.compile(r"^(零|满|\d+)级$")


def _join_stat_tokens(content: Iterable[str]) -> Iterable[str]:
    """最长匹配带空格的词条别名，英文全称不要求用户额外加引号。"""
    tokens = list(content)
    i = 0
    while i < len(tokens):
        # 双持武器名中的 & 两侧属于同一个名称，不能把其中一侧当词条。
        if i + 2 < len(tokens) and tokens[i + 1] in ("&", "＆"):
            yield " ".join(tokens[i : i + 3])
            i += 3
            continue
        for end in range(min(len(tokens), i + _STAT_WORD_MAX + 1), i, -1):
            candidate = " ".join(tokens[i:end])
            candidate = re.sub(r"^(带负|负)\s+", r"\1", candidate)
            name = re.sub(r"^(?:带负|负)", "", candidate)
            if name.casefold() in _STAT_LOOKUP:
                yield candidate
                i = end
                break
        else:
            yield tokens[i]
            i += 1


def parse_wr(content: Iterable[str]) -> WRQuery:
    """解析 wr 内容 token：词条连写、极性槽、价格、洗数、词条数、状态词。"""
    q = WRQuery()
    weapon_parts: list[str] = []
    neg_side = False  # 用于「负 任意」等独立负面修饰符的判断
    for tok in _join_stat_tokens(content):
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
        if tok in ("没有", "无", "不"):
            q.forbid_negative = True
            continue
        # —— 词条串：可能带 负/带负 标记，可能正负连写
        segs = _split_negatives(tok)
        if segs is not None:
            seg_matched = True
            positives: list[str] = []
            negatives: list[str] = []
            require_negative = False
            forbid_negative = False
            for part, is_neg in segs:
                if not part:
                    if is_neg:
                        # 「负」/「带负」单独成词：要求存在任意负面
                        require_negative = True
                    continue
                if part in _ANY_MARK:
                    if is_neg:
                        require_negative = True
                    continue
                if part in _NONE_MARK:
                    forbid_negative = True
                    continue
                stds = segment_stats(part)
                if stds is None:
                    # 该片段无法识别为词条：整个 token 退回武器名
                    seg_matched = False
                    break
                target = negatives if is_neg else positives
                for std in stds:
                    target.extend(RIVEN_STAT_COMBOS.get(std, [std]))
            if seg_matched:
                q.stats.extend(positives)
                q.negatives.extend(negatives)
                q.require_negative |= require_negative
                q.forbid_negative |= forbid_negative
                neg_side = neg_side or any(is_neg for _, is_neg in segs)
                continue
            if any(is_neg for _, is_neg in segs):
                q.unrecognized_stats.append(tok)
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
        if neg and (not parts or not parts[-1][1]):
            # ★ 2026-10-01 修：「任意负」结尾的「负」曾把 neg=True 标在半路就丢——
            #   只要结尾时 neg 为 True 且最后一个片段不在负面侧，就补一个空负面片段
            #   （parse_wr 里 ("", True) ⇒ require_negative=True）。
            #   既有行为不受影响：「负变焦」→ [("变焦",True)]（尾片段已是负面）；
            #   「双暴负变焦」→ [("双暴",False),("变焦",True)]。
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

    item: str = ""  # 物品名（EN 或 CN 别名）
    buy: bool = False  # 收购（查看收购单）
    group_buy: Optional[list[tuple[str, int]]] = None  # 合购 [(item, qty)]
    group_buy_dropped: int = 0  # 合购超出 GROUP_BUY_MAX 被忽略的项数（回复里提示）
    quantity: Optional[int] = None  # 3个 -> 同时出售 >= 3
    rank: Optional[int] = None  # 零级/满级/3级；满级用 -1 表示
    rank_word: str = ""  # 原始 rank 词（展示用）
    refinement: Optional[str] = None  # 完整/优良/无暇/光辉 → WM subtype
    refinement_word: str = ""  # 原始精炼词（提示语回显用）
    moran: bool = False  # 墨染（Atragraph / Foil Mod，subtype=atragraph）
    part: str = ""  # 部件关键词（蓝图/机体/系统/头部/配件…）


# ★ 部件关键词（2026-09-19 玩家反馈「wm 母牛 蓝图」搜出来全是整套）：
#   具体部件词（机体/头部/系统/枪管/枪机/枪托/头盔/蓝图/总图）命中后，
#   查询会切到该部件的订单而不是整套；「配件/部件」泛指 → 保留整套 +
#   部件参考价。★ 具体词优先于「蓝图」——「机体蓝图」是机体，不是总图。
_PART_SPECIFIC = (
    "头部神经",
    "机体",
    "头部",
    "系统",
    "枪管",
    "枪机",
    "枪托",
    "头盔",
    # ★ 2026-10-05 扩充（近战/弓/守护/副手/投掷/殁世机甲）：对 843 个
    #   component/blueprint 中文名做尾词频后，补上表内缺失的高频部件词。
    #   此前 `wm 格拉姆p 刀刃` 解析不到部件（「p 后缀 + 部件词缺」互相放大）。
    #   ⚠ 这些词会与少数 MOD 名相撞（簧压刀刃/爆裂刀刃/锐利刀刃、燃烧外壳/
    #   低温外壳/魔导·外壳）—— 与既有「头盔/枪管」同类；由 `_h_wm` 的
    #   「拆件失败 → 原文重组再试」兜底处理（market.py）。
    "握柄",
    "刀刃",
    "连接器",
    "外壳",
    "弓弦",
    "弓身",
    "上弓臂",
    "下弓臂",
    "拳套",
    "武器舱",
    "镖袋",
    "护手",
    # ★ 2026-10-07 扩充（守护/殁世机甲/曲翼/飞船/玄骸锤类）：玩家报障
    #   「wm 玻之武杖p 饰物」出整套 —— 「饰物」不在表里 ⇒ 整串丢给解析 ⇒ 落 set。
    #   选词口径：WM `/v2/items`（Language: zh-hans）的 component/blueprint
    #   中文名 843 条按「尾词 + 子串」统计，取**每个覆盖 ≥2 条真实部件名**的词；
    #   这 8 词在**非部件物品名（3049 条）里 0 撞名**（比 2026-10-05 那批干净，
    #   不依赖 `_h_wm` 的「原文重组再试」兜底）。
    #   有意未收录（各只覆盖 1 条，等真实报障再加）：链条/星镖/爪刃/铆钉/锯片/
    #   散热片/下皮层/弓臂/马达/刀片/剑刃/手套/靴子。
    "饰物",
    "引擎",
    "锤头",
    "机舱",
    "圆盘",
    "机翼",
    "外甲",
    "握把",
)
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
            part = "头部" if w in ("头部", "头部神经") else w
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
        for w in _PART_GENERIC + _PART_ANY + ("神经光元", "神经元", "光元"):
            rest = rest.replace(w, "")
        rest = rest.strip()
    return rest, part


# 遗物精炼档：**官方简中「无瑕」在前、社区错写「无暇」兼容**（2026-09-25 总任务会话
# 复核报障：帮助卡/KB 文案用的是官方「无瑕」，词典只收了「无暇」→ 照帮助卡输入时
# 该词不被消费，会残留在物品名里且精炼档过滤静默失效）。
_REFINEMENT_MAP = {
    "完整": "intact",
    "优良": "exceptional",
    "无瑕": "flawless",
    "无暇": "flawless",
    "光辉": "radiant",
}


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
                full = _parse_group_list(tokens[i])
                q.group_buy = full[:GROUP_BUY_MAX]
                q.group_buy_dropped = len(full) - len(q.group_buy)
            else:
                item_parts.append(tok)
        elif re.fullmatch(r"(\d+)个", tok):
            q.quantity = int(tok[:-1])
        elif tok in ("零级", "满级") or _RANK_TOKEN_RE.match(tok):
            q.rank_word = tok
            q.rank = 0 if tok == "零级" else (-1 if tok == "满级" else int(tok[:-1]))
        elif tok in _REFINEMENT_MAP:
            q.refinement = _REFINEMENT_MAP[tok]
            q.refinement_word = tok
        elif tok == "墨染":
            q.moran = True
        else:
            rest, part = _extract_part(tok)
            if part and _PART_STRENGTH[part] >= _PART_STRENGTH.get(q.part, -1):
                q.part = part  # 具体部件 > 蓝图 > 配件（跨 token 不被弱词覆盖）
            if rest:
                item_parts.append(rest)
        i += 1
    q.item = " ".join(item_parts).strip()
    return q


# 合购单项数上限：每项一次 WM 订单请求，共享全局限速锁；超出部分忽略并在回复里提示。
GROUP_BUY_MAX = 10


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
    "capture": "捕获",
    "exterminate": "歼灭",
    "survival": "生存",
    "defense": "防御",
    "interception": "拦截",
    "extermination": "歼灭",
    "defection": "叛逃",
    "infested salvage": "INFESTED 资源回收",
    "recovery": "回收",
    "pursuit": "追击",
    "salvage": "打捞",
    "mobile defense": "移动防御",
    "mobiledefense": "移动防御",
    "rescue": "救援",
    "spy": "间谍",
    "sabotage": "破坏",
    "excavation": "挖掘",
    "disruption": "中断",
    "assassination": "刺杀",
    "hijack": "劫持",
    "assault": "强袭",
    "skirmish": "遭遇战",
    "rush": "冲刺",
    "void flood": "虚空洪流",
    "voidflood": "虚空洪流",
    "void cascade": "虚空覆涌",
    "voidcascade": "虚空覆涌",
    "orphix": "奥菲克斯",
    "alchemy": "元素转换",
    "mirror defense": "镜像防御",
    "eidelonhunt": "夜灵狩猎",
    "hunt": "狩猎",
    # ↓ DE ExportMissionTypes 有、早期手写表漏掉的类型（补全，避免展示/筛选落空）
    "legacyte harvest": "传承种收割",
    "ascension": "扬升",
    "hive": "清巢",
    "free roam": "自由漫游",
    "conclave": "武形秘仪",
    "void armageddon": "虚空决战",
    "descent": "沉沦之地",
    "endless duviri": "无尽回廊",
    "sanctuary onslaught": "圣殿突袭",
    "offering defense": "祈运坛防御",
    "vaults": "衰退室",
    "dark sector": "黑暗地带战争",
    "rathuum": "竞技场",
    "junction": "星际航道结合点",
    "pvpve": "对战",
    "tau war": "佩里塔叛乱",
    "paint flood": "Follie 的狩猎",
    "unknown": "未知",
}
_CN_TO_MISSION: dict[str, str] = {}
for _en, _cn in MISSION_CN.items():
    _CN_TO_MISSION.setdefault(_cn, _en)
# 旧手写译名 / 社区叫法：仍然接受，用户按老习惯输入也能筛到
# （`MT_PURIFY` 的正名已按官方订正为「INFESTED 资源回收」，旧的「感染打捞」留作别名 ——
#   2026-09-28 §六：订正后 `MISSION_CN` 与官方任务类型表**零冲突**，此前仅此 1 条冲突。）
_CN_TO_MISSION.update(
    {
        "感染打捞": "infested salvage",
        "营救": "rescue",
        "炼金": "alchemy",
        "虚空级联": "void cascade",
        "追猎": "pursuit",
        "突袭": "assault",
        # 九重天风暴任务类型（与 formatters._RAILJACK_TYPE_CN 卡面译名一致；
        # 「蹲 裂隙 九重天前哨战」靠它命中 skirmish）
        "前哨战": "skirmish",
        "爆发": "volatile",
        "奥影母艇": "orphix",
    }
)

# 遗物纪元中文名（DE 官方简中，见 /Lotus/Language/Relics/Era_*）
# ⚠️ Omnia（全能）与 Requiem（安魂）是两个不同的纪元，绝不能都译成「安魂」——
#    旧实现把 Omnia 也写成「安魂」，于是扎里曼/联结生存的裂隙全部显示成安魂裂缝。
TIER_CN = {
    "Lith": "古纪",
    "Meso": "前纪",
    "Neo": "中纪",
    "Axi": "后纪",
    "Requiem": "安魂",
    "Omnia": "全能",
    "Vanguard": "先锋",
    "古纪": "Lith",
    "前纪": "Meso",
    "中纪": "Neo",
    "后纪": "Axi",
    "安魂": "Requiem",
    "全能": "Omnia",
    "先锋": "Vanguard",
}
_TIER_KEYS = ("Lith", "Meso", "Neo", "Axi", "Requiem", "Omnia")

# ★ 2026-10-03：裂隙档位的 T 编号别名（DE 官方 VoidT1..T6 的编号）。
#   ★ **不要并入 TIER_CN** —— TIER_CN 被「遗物」等共用（relic.py 明说以它作
#     真源），塞进去会串味。T 别名只在裂隙筛选侧生效（经 FISSURE_TIER_WORDS
#     过滤）。映射与 core/data/de/fissureModifiers.json（VoidT1..T6）及
#     core/de_worldstate.py::fissure_tier() 完全一致（单一真源，勿另写）。
TIER_T_ALIAS: dict[str, str] = {
    "t1": "Lith",
    "t2": "Meso",
    "t3": "Neo",
    "t4": "Axi",
    "t5": "Requiem",
    "t6": "Omnia",
}


# ---------------------------------------------------------------------------
# ★ 裂隙筛选的「纯修饰词」（2026-09-26）
#   这类词**单独出现时不构成条件**，本意是与任务/纪元词**合成一个词元**
#   （「钢铁防御」= 钢铁之路的防御裂隙）。★ 2026-10-16 维护者定：「蹲」与「裂隙」查询统一为
#   **空格 = 且、逗号 = 或**（此前蹲把空格转成逗号 = 或，9-26 只加了提示，10-16 再次踩坑后改语义）。
#   ⚠️ 别再各写一份清单（「同源两处」是这个项目反复踩的坑）。
# ---------------------------------------------------------------------------
FISSURE_HARD_WORDS = ("钢铁", "钢路")
FISSURE_STORM_WORDS = ("九重天", "empyrean")
FISSURE_NORMAL_WORD = "普通"
FISSURE_VOID_WORD = "虚空"
# ★ 2026-10-03：档位词表扩为 古纪/前纪/中纪/后纪 + 安魂 + 全能 + T1–T6。
#   此前安魂/全能不在表里 ⇒ 退化成节点子串、永不命中（实测坑 2）；T1..T6
#   同理（坑 1）。FISSURE_MODIFIER_WORDS 是它的派生 ——**同源一处，别各写一份**。小写 t1..t6 供 p.lower() 匹配。
FISSURE_TIER_WORDS = (
    "古纪",
    "前纪",
    "中纪",
    "后纪",
    "安魂",
    "全能",
    "t1",
    "t2",
    "t3",
    "t4",
    "t5",
    "t6",
)
FISSURE_MODIFIER_WORDS = (
    FISSURE_HARD_WORDS
    + FISSURE_STORM_WORDS
    + (FISSURE_NORMAL_WORD, FISSURE_VOID_WORD)
    + FISSURE_TIER_WORDS
)


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
        for key in ("missionKey", "missionType"):
            v = f.get(key)
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
        if (
            g.get("void")
            and "虚空" not in (f.get("node") or "")
            and "void" not in (f.get("nodeKey") or "").lower()
            and "void" not in (f.get("missionKey") or "").lower()
        ):
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
            # ★ 2026-10-11：只有**显式**写了「普通」（hard=False）才写「普通」。旧判据
            #   「没写钢铁/九重天就补普通」⇒「裂隙 生存」注脚写「普通生存」，而实际条件
            #   不限难度（钢铁生存也在列表里），注脚与筛选结果对不上。「蹲」共用本函数。
            if g.get("hard") is False:
                seg.append("普通")
            if g.get("missions"):
                seg.append("/".join(MISSION_CN.get(m, m) for m in sorted(g["missions"])))
            if g.get("tiers"):
                # ★ 2026-10-03：档位统一显示「中文名（Tn）」（古纪（T1）…全能（T6）），
                #   编号顺序与 formatters._TIER_ORDER（Lith=0…Omnia=5）对齐；此处用
                #   本地有序元组推编号，避免 parser ↔ formatters 循环 import。
                seg.append(
                    "/".join(
                        (
                            f"{TIER_CN.get(t, t)}（T{_TIER_ORDER_LOCAL.index(t) + 1}）"
                            if t in _TIER_ORDER_LOCAL
                            else TIER_CN.get(t, t)
                        )
                        for t in g["tiers"]
                    )
                )
            if g.get("substr"):
                seg.append(g["substr"])
            parts.append("".join(seg) or "全部")
        # ★ 2026-10-16：分组之间是「或」（逗号分组 = 任一命中）。旧写法用「，」连接，读起来像「且」，
        #   用户看不出自己建的是或订阅（「钢铁,古纪」显示成「钢铁，古纪（T1）」）。
        return " 或 ".join(parts)


# ★ 2026-10-16：筛选串里的「裂隙 / 裂缝」本身是事件名，不是节点名。旧实现把剔除档位词后的
#   残余当节点子串 ⇒「蹲 钢铁裂隙古纪」残余「裂隙」要求节点名含「裂隙」⇒ 永不命中（静默死订阅，
#   ZCode 实测）。连写词先归一成档位词（与 _PRESET_COMMANDS 同口径：虚空裂隙 = 普通），
#   单独的「裂隙 / 裂缝」直接去掉。节点名里带「裂缝」的（如「热美亚裂缝」）剩余部分仍能子串命中。
_FISSURE_WORD_MAP = {
    "钢铁裂隙": "钢铁",
    "钢铁裂缝": "钢铁",
    "虚空裂隙": "普通",
    "虚空裂缝": "普通",
    "九重天裂隙": "九重天",
    "九重天裂缝": "九重天",
}
_FISSURE_WORD_RE = re.compile("|".join([*_FISSURE_WORD_MAP, "裂隙", "裂缝"]))


def parse_fissure_filter(text: str) -> FissureFilter:
    """解析 `普通捕获,钢铁虚空生存,九重天拦截` 这类筛选串。"""
    flt = FissureFilter()
    for blob in re.split(r"[,，;；]", text.strip()):
        blob = _FISSURE_WORD_RE.sub(lambda m: _FISSURE_WORD_MAP.get(m.group(0), ""), blob).strip()
        if not blob:
            continue
        g: dict = {}
        words = re.split(r"\s+", blob)
        merged = "".join(words)
        # 全角/大小写归一：Ｔ５→t5、１→1（中文词不受影响）——T5 / t5 / Ｔ５ 等价。
        norm = unicodedata.normalize("NFKC", merged)
        low = norm.lower()
        # 纪元层级：中文档位词按子串匹配；T 别名（t1..t6）要求词边界
        # （「T5x / t55 / T05」一律不认 —— 走 fissure_tier_hint 提示，不猜）。
        tiers = set()
        for cn, en in {**TIER_CN, **TIER_T_ALIAS}.items():
            if cn not in FISSURE_TIER_WORDS:
                continue
            if cn in TIER_T_ALIAS:
                if re.search(rf"(?<![a-z0-9]){cn}(?![0-9a-z])", low):
                    tiers.add(en)
            elif cn in merged:
                tiers.add(en)
        if tiers:
            g["tiers"] = tiers
        # 平台修饰（普通 = 排除钢铁/九重天）
        if any(w in merged for w in FISSURE_HARD_WORDS):
            g["hard"] = True
        elif FISSURE_NORMAL_WORD in merged:
            g["hard"] = False
        if any(w in merged.lower() for w in FISSURE_STORM_WORDS):
            g["storm"] = True
        elif FISSURE_NORMAL_WORD in merged:
            g["storm"] = False
        # 任务类型：逐个 CN 词匹配后剔除，剩余部分作为节点/星球子串。
        # rest 从**归一化文本**起算：T 别名是全角/大小写无关的（rest 清理
        # 必须一并剔除 T 别名，否则 T5 残留成 len≥2 的节点子串 —— 实测坑 3）。
        rest = norm
        missions = set()
        for cn, en in sorted(_CN_TO_MISSION.items(), key=lambda kv: -len(kv[0])):
            if cn in rest:
                missions.add(en)
                rest = rest.replace(cn, "")
        for kw in FISSURE_HARD_WORDS + FISSURE_STORM_WORDS + (FISSURE_NORMAL_WORD,):
            rest = rest.replace(kw, "")
        for cn in FISSURE_TIER_WORDS:
            # 大小写不敏感剔除（T5/t5 都清掉；中文词不受影响）
            rest = re.sub(re.escape(cn), "", rest, flags=re.IGNORECASE)
        # 地区修饰：「虚空」= 只收虚空星系节点（2026-09-14 修「蹲 虚空捕获
        # 却推了木星捕获」——旧版把「虚空」当纯修饰词剔除，等于没写）。
        # 必须在任务名剔除**之后**再判定：虚空覆涌/虚空洪流这类任务名本身
        # 含「虚空」，不代表用户要限定虚空地区。
        if FISSURE_VOID_WORD in rest:
            g["void"] = True
        rest = rest.replace(FISSURE_VOID_WORD, "")
        if missions:
            g["missions"] = missions
        rest = rest.strip()
        if rest and len(rest) >= 2:
            g["substr"] = rest
        if g:
            flt.groups.append(g)
    return flt


# 裂隙筛选 0 结果时给出的可用筛选词（「裂隙」查询卡用；与 parse_fissure_filter 的词表一致）
FISSURE_FILTER_HELP = (
    "※ 可用筛选词：钢铁 / 九重天 / 普通 / 虚空 ｜ 纪元 古纪…全能（T1–T6）｜"
    " 任务类型（生存 / 防御 / 中断…）｜ 节点或星球名；空格分开 = 同时满足，逗号分开 = 任一满足"
)


# T1–T6 展示编号顺序（与 formatters._TIER_ORDER 的 Lith=0…Omnia=5 对齐；
# 本地元组避免 parser ↔ formatters 循环 import —— 两处语义必须一致）。
_TIER_ORDER_LOCAL = ("Lith", "Meso", "Neo", "Axi", "Requiem", "Omnia")
_TIER_RANGE_TEXT = "T1古纪 / T2前纪 / T3中纪 / T4后纪 / T5安魂 / T6全能"


def contains_fissure_tier(text: str) -> bool:
    """文本是否含裂隙档位词（T1–T6 别名 / 古纪…全能）。

    ★ 2026-10-03（口径）：**档位词本身就含裂隙语义** —— 「蹲」用它在
    用户漏写类型词时自动判定为裂隙（「蹲 钢铁t5歼灭」直接生效）。
    ⚠ 只认**档位词**（裂隙专属词汇）；钢铁/虚空/地点词一律不推断 ——
    不重蹈 2026-09-14「地点词被静默当裂隙筛选」的覆辙。
    """
    low = unicodedata.normalize("NFKC", text or "").lower()
    return any(w in low for w in FISSURE_TIER_WORDS)


def fissure_tier_hint(text_or_parts) -> str:
    """裂隙档位写法提示（T 越界编号 / 孤立的 t）；无需提示返回空串。

    合法：T1–T6（大小写与全角均可，与中文档位词等价）。以下一律**明确提示**
    （铁律 A：不静默）：T0 / T7 / T9、T05、t55、T5x、5T、孤立的 t。
    **只提示、不改解析语义**。
    """
    if isinstance(text_or_parts, str):
        parts = [text_or_parts]
    else:
        parts = [str(p) for p in (text_or_parts or []) if p]
    low = " ".join(unicodedata.normalize("NFKC", p).lower() for p in parts)
    bad = False
    for m in re.finditer(r"(?<![a-z0-9])t(\d+)", low):
        if m.group(1) not in ("1", "2", "3", "4", "5", "6"):
            bad = True
            break
    if not bad:
        bad = bool(
            re.search(r"(?<![a-z0-9])t(?![0-9a-z])", low)  # 孤立的 t（后面不跟数字）
            or re.search(r"(?<![a-z0-9])t\d+[a-z]", low)  # T5x 这类
            or re.search(r"\d+t(?![0-9a-z])", low)  # 5T 这类
        )
    if not bad:
        return ""
    return f"※ 裂隙档位只支持 T1–T6（{_TIER_RANGE_TEXT}）"


# ---------------------------------------------------------------------------
# 蹲：时长与免打扰时间窗
# ---------------------------------------------------------------------------
_DURATION_UNITS = {
    "小时": 3600,
    "h": 3600,
    "天": 86400,
    "日": 86400,
    "d": 86400,
    "周": 604800,
    "星期": 604800,
    "w": 604800,
    "月": 2592000,
    "yue": 2592000,
    "年": 31536000,
    "y": 31536000,
}
_DURATION_WORDS = {
    "永久": -1,
    "长期": -1,
    "无限": -1,
    "两周": 1209600,
    "半月": 1296000,
    "一周": 604800,
    "一天": 86400,
    "今天": 86400,
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


# 推送时间窗一律按**北京时间**（UTC+8）判定，与服务器时区无关（2026-10-11 立）。
try:  # 优先 IANA 名；容器缺 tzdata 时退回固定 UTC+8（中国无夏令时，二者等价）
    from zoneinfo import ZoneInfo as _ZoneInfo

    BEIJING_TZ = _ZoneInfo("Asia/Shanghai")
except Exception:  # noqa: BLE001 - 无 tzdata
    BEIJING_TZ = timezone(timedelta(hours=8))


def beijing_now() -> datetime:
    """当前北京时间（带 tzinfo）。"""
    return datetime.now(BEIJING_TZ)


@dataclass
class TimeWindow:
    """允许推送的时间窗（北京时间）。空对象表示全天允许。

    口径（2026-10-11 维护者定）：
    · ``start``~``end`` 为小时，**结束小时是开区间**：「22到8」= 22:00 到次日 8:00（8 点整结束）；
      「8到22」= 8:00–21:59；「0到24」= 全天（解析时直接归一成空窗）；
      ``start == end``（如「周1 23点」存成 23/23）= 只那一个整点小时。
    · ``days`` 的星期按**窗口起始那天**算：「周五 22到8」含周六凌晨 0–8 点。
    · ``at_hour``（每天 HH 点）= 该整点小时内（HH:00–HH:59）。
    """

    start: Optional[int] = None  # 起始小时 0-23
    end: Optional[int] = None  # 结束小时 0-24（开区间）
    days: Optional[set[int]] = None  # 星期集合（1=周一 ... 7=周日），按窗口起始日
    at_hour: Optional[int] = None  # 每天 HH 点（整个小时）

    @property
    def is_always(self) -> bool:
        return self.start is None and self.days is None and self.at_hour is None

    def allows(self, now) -> bool:
        """now: datetime（北京时间；调用方负责换算）。"""
        h = now.hour
        wd = now.isoweekday()
        if self.at_hour is not None:
            return h == self.at_hour and (self.days is None or wd in self.days)
        if self.start is None:
            return self.days is None or wd in self.days
        end = self.end if self.end is not None else self.start
        if end == self.start:  # 单个整点小时
            return h == self.start and (self.days is None or wd in self.days)
        if self.start < end:  # 不跨午夜
            ok = self.start <= h < end
            start_wd = wd
        else:  # 跨午夜，如 22到8
            ok = h >= self.start or h < end
            # 凌晨段属于「前一天开始」的窗口
            start_wd = wd if h >= self.start else (wd - 2) % 7 + 1
        return ok and (self.days is None or start_wd in self.days)

    def describe(self) -> str:
        bits = []
        if self.start is not None:
            end = self.end if self.end is not None else self.start
            if end == self.start:
                bits.append(f"{self.start}:00-{self.start + 1}:00")
            elif end < self.start:
                bits.append(f"{self.start}:00-次日{end}:00")
            else:
                bits.append(f"{self.start}:00-{end}:00")
        if self.days:
            bits.append("周" + "/".join(map(str, sorted(self.days))))
        if self.at_hour is not None:
            bits.append(f"每天{self.at_hour}点（{self.at_hour}:00-{self.at_hour}:59）")
        return " ".join(bits) or "全天"


def parse_time_window(text: str) -> Optional[TimeWindow]:
    """解析 `0到24`、`22到8`、`每天19点`、`周1/3/5 23点`、`周5 22到8`（北京时间）。"""
    t = text.strip().replace("：", ":")
    if not t:
        return None
    m = re.fullmatch(r"周([1-7](?:[/，][1-7])*)\s*(\d{1,2})点?", t)
    if m:
        days = {int(x) for x in re.split(r"[/，]", m.group(1))}
        hh = int(m.group(2))
        if hh > 23:
            return None
        return TimeWindow(days=days, start=hh, end=hh)
    m = re.fullmatch(r"周([1-7](?:[/，][1-7])*)\s*(\d{1,2})到(\d{1,2})(?:点)?", t)
    if m:
        days = {int(x) for x in re.split(r"[/，]", m.group(1))}
        w = _range_window(int(m.group(2)), int(m.group(3)))
        if w is None:
            return None
        if w.is_always:  # 「周5 0到24」= 周五全天
            return TimeWindow(days=days, start=0, end=24)
        w.days = days
        return w
    m = re.fullmatch(r"每天\s*(\d{1,2})点?", t)
    if m:
        hh = int(m.group(1))
        return TimeWindow(at_hour=hh) if hh <= 23 else None
    m = re.fullmatch(r"(\d{1,2})到(\d{1,2})(?:点)?", t)
    if m:
        return _range_window(int(m.group(1)), int(m.group(2)))
    return None


def _range_window(a: int, b: int) -> Optional[TimeWindow]:
    """「a到b」→ 窗口；0到24 / 24到0 归一成全天（空窗）；越界返回 None。"""
    if a > 24 or b > 24:
        return None
    a = a % 24  # 24 点即次日 0 点
    if a == 0 and b in (0, 24):
        return TimeWindow()  # 0到24 = 全天（0到0 视同：旧版把 0到24 存成 0/0，口径统一）
    return TimeWindow(start=a, end=b)
