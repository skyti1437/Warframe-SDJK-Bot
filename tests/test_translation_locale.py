# -*- coding: utf-8 -*-
"""译名体系守卫：防止**国服**叫法混进国际服卡片。

背景（2026-09-11 实测 DE 官方导出，共 15.5 万条简中字符串）：

    Grineer  英文出现 558 次 ｜「克隆尼」0 次
    Corpus   英文出现 507 次 ｜「科普斯」0 次
    Infested 英文出现 515 次 ｜「感染体」1 次
    Sentient 英文出现 252 次 ｜「心智者」0 次
    Corrupted 英文出现 38 次 ｜「堕落者」0 次
    Narmer   英文出现 39 次 ｜「纳玛」0 次
    Orokin   英文出现 115 次 ｜「奥罗金」3436 次   ← 官方确实译了这个

即：**DE 官方简中不翻译派系名，保留英文**（如「借 Grineer 自己的旗帜来嘲弄、羞辱他们。」），
而「克隆尼 / 科普斯 / 心智者 / 堕落者 / 纳玛」是**国服**叫法。两者混用会让译名体系不一致。

本测试扫描所有**面向输出**的代码与数据文件，一旦出现国服叫法即失败。
`core/data/aliases.json` 与 `translate.json` 是**输入别名/词典表**，保留国服同义词是
有意为之（用户按国服习惯输入也要能匹配上），故显式放行。

另外**国服平台自身需要一套译名表**（-cn 时用 克隆尼/科普斯 …），这类表用行标记
``# >>> CN-LOCALE-TABLE`` … ``# <<< CN-LOCALE-TABLE`` 圈起来即可豁免。
刻意做「区域豁免」而不是「整文件豁免」：国服词只允许出现在那张表里，
散落到任何别处（日志、兜底文案、拼接的 f-string）依然会被拦下。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = ""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f"  -> {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


# 国服叫法（不得出现在面向输出的文案里）
BANNED = [
    "克隆尼", "科普斯", "感染体", "堕落者", "心智者", "纳玛",
    "斯卡德拉", "技术腐化", "塑形块",
    "执刑官碎片", "赋能解锁器", "延伸适配器", "传说级核心",
    "Orokin 催化剂", "Orokin 反应堆",
    # 自造译名，官方分别是「巴罗尔巨人战舰」「利刃豺狼舰队」
    "巴洛巨舰", "剃刀舰队", "剃刀背",
]

# 输入别名 / 词典表：允许含国服同义词
ALLOWLIST = {
    "core/data/aliases.json",
    "core/data/translate.json",
    # 以下为 DE 官方导出（由 build_de_data.py 生成），是**数据**不是面向输出的文案：
    # 官方简中里本身就会出现「堕落者」等词（如某些 Mod 名），不能当自造译名拦下。
    "core/data/de/languages_zh.json",
    "core/data/de/challenges_zh.json",
    "core/data/de/mod_names_zh.json",
    "core/data/de/recipe_names_zh.json",
    "core/data/de/nodes_zh.json",
    "core/data/de/mission_types_zh.json",
    # 敌人基准数值表（由 build_damage_data.py 从极镜数据生成）：
    # 里面的 zh 是**匹配用**的社区译名，且与 DE 官方单位名同构 ——
    # 例如 Corrupted Ancient 作「远古堕落者」，官方 Corrupted 系单位
    # 本身就带「堕落」前缀（堕落枪兵 / 堕落轰击者）。JSON 没法用行尾
    # `# INTL` 豁免，故整表放行。
    "core/data/enemies.json",
    # wiki 简介卡片数据（2026-09-24；2026-09-25 起随包分发）：构建期从知识库抽取。
    # 放行原因同 de/*_zh.json ——
    # 正文是 DE 官方简中原文，会出现「异变感染体」这类**官方复合词**
    # （导出实测：异变感染体立柱爆裂包囊），BANNED 里的裸词「感染体」
    # 会在这类复合词上误报。
    "core/data/wiki_intro.json",
}

targets: list[Path] = [ROOT / "main.py"]
targets += sorted((ROOT / "core").glob("*.py"))
# ★ 排除 core/data/_cache/：那是构建脚本的**原始下载缓存**（DE 的 dict.zh.json
#   同时含国际服/国服全部中文变体，逐词检查必然误报），不会随插件分发
#   （.gitignore 与打包脚本都排除了它）。
targets += sorted(p for p in (ROOT / "core" / "data").rglob("*.json")
                  if "_cache" not in p.parts)

BEG, END = "# >>> CN-LOCALE-TABLE", "# <<< CN-LOCALE-TABLE"


def strip_cn_tables(text: str) -> str:
    """删掉被 ``CN-LOCALE-TABLE`` 标记圈起来的国服译名表区域。

    Args:
        text: 源文件全文。

    Returns:
        去掉国服译名专区后的文本。

    Raises:
        ValueError: 标记不成对（只有开始没有结束）。
    """
    out: list[str] = []
    inside = False
    for line in text.splitlines(True):
        if line.strip() == BEG:
            if inside:
                raise ValueError(f"{BEG} 嵌套")
            inside = True
            continue
        if line.strip() == END:
            if not inside:
                raise ValueError(f"{END} 缺少配对的 {BEG}")
            inside = False
            continue
        if not inside:
            out.append(line)
    if inside:
        raise ValueError(f"{BEG} 未闭合")
    return "".join(out)


scanned = 0
for p in targets:
    rel = p.relative_to(ROOT).as_posix()
    if rel in ALLOWLIST:
        continue
    try:
        text = p.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        continue
    scanned += 1
    try:
        body = strip_cn_tables(text)
    except ValueError as e:
        check(f"国服译名表标记完整：{rel}", False, str(e))
        continue
    # 行级豁免：行尾带 `# INTL` 的行是**官方国际服译名**（例如 DE 词表里
    # Corrupted 系就叫「堕落枪兵 / 远古堕落者」，含「堕落者」子串），
    # 不是自造国服叫法 —— 逐行跳过，别一刀切。
    body = "\n".join(ln for ln in body.splitlines()
                     if not ln.rstrip().endswith("# INTL"))
    hits = [t for t in BANNED if t in body]
    if hits:
        check(f"输出文案不含国服叫法：{rel}", False, f"命中 {hits}")

check(f"扫描了 {scanned} 个输出相关文件", scanned > 20, f"仅 {scanned} 个")

# ---------------------------------------------------------------- 正确定义
from core import de_worldstate as W  # noqa: E402
from core import formatters as F  # noqa: E402

# 派系：直接对齐 DE 官方简中导出（ExportFactions + dict.zh）
#   Grineer / Corpus / Infestation / SENTIENT 官方保留英文；
#   奥罗金 / 合一众 / 低语者 / 炽蛇军 / 科腐者 / 血色面纱 官方有中文译名。
# 数据由 build_de_data.py 重建 factionsData.json，此处是哨兵断言。
check("FC_GRINEER 保持英文", W.faction_name("FC_GRINEER") == "Grineer",
      W.faction_name("FC_GRINEER"))
check("FC_CORPUS 保持英文", W.faction_name("FC_CORPUS") == "Corpus",
      W.faction_name("FC_CORPUS"))
check("FC_INFESTATION 官方作 Infestation（非 Infested）",
      W.faction_name("FC_INFESTATION") == "Infestation",
      W.faction_name("FC_INFESTATION"))
check("FC_SENTIENT 官方作 SENTIENT（全大写）",
      W.faction_name("FC_SENTIENT") == "SENTIENT",
      W.faction_name("FC_SENTIENT"))
check("FC_OROKIN 译作奥罗金", W.faction_name("FC_OROKIN") == "奥罗金",
      W.faction_name("FC_OROKIN"))
check("FC_NARMER 译作合一众", W.faction_name("FC_NARMER") == "合一众",
      W.faction_name("FC_NARMER"))
check("FC_MITW 译作低语者", W.faction_name("FC_MITW") == "低语者",
      W.faction_name("FC_MITW"))
check("FC_SCALDRA 译作炽蛇军", W.faction_name("FC_SCALDRA") == "炽蛇军",
      W.faction_name("FC_SCALDRA"))
check("FC_TECHROT 译作科腐者", W.faction_name("FC_TECHROT") == "科腐者",
      W.faction_name("FC_TECHROT"))
check("未知代码原样返回", W.faction_name("FC_UNKNOWN_XX") == "FC_UNKNOWN_XX")

# 执刑官碎片 -> 官方是「源力石」
check("Amar 碎片官方名",
      F._ARCHON_SHARD["Amar"][1] == "深红执刑官源力石", F._ARCHON_SHARD["Amar"][1])
check("Nira 碎片官方名",
      F._ARCHON_SHARD["Nira"][1] == "琥珀执刑官源力石", F._ARCHON_SHARD["Nira"][1])
check("Boreal 碎片官方名",
      F._ARCHON_SHARD["Boreal"][1] == "蔚蓝执刑官源力石", F._ARCHON_SHARD["Boreal"][1])

# 官方术语表
for en, cn in [
    ("Exilus Weapon Adapter", "武器特殊功能槽连接器"),
    ("Weapon Primary Arcane Unlocker", "主要武器赋能槽连接器"),
    ("Weapon Secondary Arcane Unlocker", "次要武器赋能槽连接器"),
    ("Orokin Catalyst Blueprint", "奥罗金催化剂蓝图"),
    ("Orokin Reactor Blueprint", "奥罗金反应堆蓝图"),
    ("Crimson Archon Shard", "深红执刑官源力石"),
    ("Legendary Core", "传说核心"),
    # 官方写法是「Mod」而不是「MOD」（name_zh.json: 近战裂罅 Mod / Zaw 裂罅 Mod）
    ("Riven Mod", "裂罅 Mod"),
]:
    check(f"官方术语 {en}", F._CAL_REWARD_CN.get(en) == cn,
          str(F._CAL_REWARD_CN.get(en)))

check("突击 3 天加成用官方语序",
      F._REWARD_NAME_CN["Affinity Booster Store Item"] == "3 天经验值加成",
      F._REWARD_NAME_CN["Affinity Booster Store Item"])

# arbi 派系对齐。
# main.py 导入期就会 `from astrbot.api import ...`，所以先塞一个最小 stub。
import types  # noqa: E402


class _FilterStub:
    EventMessageType = type("X", (), {"ALL": 1, "GROUP_MESSAGE": 2,
                                      "PRIVATE_MESSAGE": 3})

    @staticmethod
    def event_message_type(*a, **kw):
        return lambda f: f

    @staticmethod
    def command(*a, **kw):
        return lambda f: f

    @staticmethod
    def platform(*a, **kw):
        return lambda f: f


def _install_astrbot_stub() -> None:
    api = types.ModuleType("astrbot.api")
    api.AstrBotConfig = dict
    api.logger = type("L", (), {m: (lambda *a, **kw: None)
                                for m in ("info", "warning", "error", "debug")})()
    ev = types.ModuleType("astrbot.api.event")
    ev.AstrMessageEvent = object
    ev.MessageChain = list
    ev.filter = _FilterStub()
    mc = types.ModuleType("astrbot.api.message_components")
    mc.Image = mc.Plain = object
    st = types.ModuleType("astrbot.api.star")
    st.Context = object
    st.Star = type("Star", (), {"__init__": lambda self, *a, **kw: None})
    st.register = lambda *a, **kw: (lambda cls: cls)
    api.event, api.message_components, api.star = ev, mc, st
    root = types.ModuleType("astrbot")
    root.api = api
    sys.modules.update({"astrbot": root, "astrbot.api": api,
                        "astrbot.api.event": ev,
                        "astrbot.api.message_components": mc,
                        "astrbot.api.star": st})


_install_astrbot_stub()
from main import _arb_faction  # noqa: E402

check("arbi Infestation 对齐官方 Infested",
      _arb_faction({"factionNameZh": "Infestation"}) == "Infested",
      _arb_faction({"factionNameZh": "Infestation"}))
check("arbi Grineer 保持英文",
      _arb_faction({"factionNameZh": "Grineer"}) == "Grineer")
check("arbi 中系派系原样保留",
      _arb_faction({"factionNameZh": "奥罗金"}) == "奥罗金")

if FAILED:
    print(f"\n失败 {len(FAILED)} 项：{FAILED}")
    sys.exit(1)
print("\n全部通过 ✔")
