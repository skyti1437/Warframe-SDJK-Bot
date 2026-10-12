# -*- coding: utf-8 -*-
"""锁住「帮助卡必须列全所有主指令」这条不变量。

背景（2026-09-17 玩家反馈）：帮助卡改版时把全部主指令压成了 36 条描述性
条目，既缺指令、又看不出「到底能发什么」。此后约定：

  1. **每个主指令都必须能在帮助卡里找到**（用主名或其任一别名匹配）。
     新增主指令却忘了写进帮助 → 本测试直接失败。
  2. 帮助卡的左列必须是**真实指令名**，不能是「平台切换」这种描述性标签 ——
     用户是照着左列去发指令的，标签发出去没有任何反应。

用法：python tests/test_help_coverage.py   （非 0 退出即失败）
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import parser as P  # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = ""):
    print(
        f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -> {detail}" if detail and not cond else "")
    )
    if not cond:
        FAILED.append(name)


def load_main_literal(name: str) -> dict:
    tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign):
            tgt = node.target
            if isinstance(tgt, ast.Name) and tgt.id == name:
                return ast.literal_eval(node.value)
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == name:
                    return ast.literal_eval(node.value)
    raise SystemExit(f"main.py 里没找到 {name}")


topic = load_main_literal("HELP_TOPIC")
# 2026-10-11 卡片改版：伤害修饰词 / 群管理 / 平台说明从指令行挪进注脚（HELP_NOTES）。
# 「主指令覆盖」「管理员标注」「修饰符照发」三条护栏的取材范围 = 指令行左列 + 注脚文本。
notes = load_main_literal("HELP_NOTES")
note_texts = [n for ns in notes.values() for n in ns]
lefts = [c for items in topic.values() for c, _ in items]
blob = "\n".join(lefts + note_texts)

print("=" * 64)
print(f"帮助卡：{len(topic)} 个分类 / {len(lefts)} 条指令行")
print("=" * 64)

# —— 1. 每个主指令都必须出现 ——
missing = []
for cmd in sorted(P.COMMAND_ALIASES):
    names = set(P.COMMAND_ALIASES[cmd]) | {cmd}
    if not any(n and n in blob for n in names):
        missing.append(f"{cmd}（可用名：{'/'.join(sorted(names)[:3])}）")
check(
    f"{len(P.COMMAND_ALIASES)} 个主指令全部出现在帮助卡里（实缺 {len(missing)}）",
    not missing,
    "；".join(missing),
)

# —— 2. 左列不能是纯描述性标签 ——
# 判据：左列里若出现「指令名 / 指令名」或单独一个词，至少要有一个 token
# 落在指令词表里，否则用户照着发是发不出来的。
VALID: set[str] = set(P.COMMAND_ALIASES)
for _c, _al in P.COMMAND_ALIASES.items():
    VALID |= set(_al)
# 允许出现的非指令词（修饰符 / 参数 / 分类提示）
ALLOW = {
    "帮助",
    "状态",
    "平台",
    "输出",
    "翻页",
    "群管理",
    "伤害",
    "灵化",
    "空战",
    "地面",
    "进化",
    "赤毒辐射60",
    "识卡",
    "识卡伤害",
    "武器融合",
    "玄骸",
    "倾向",
    "紫卡排行",
    "毒",
    "蹲",
    "夜灵",
    "赏金",
    "裂隙",
    "突击",
    "执刑官",
    "时效",
    "周报",
    "深层科研",
    "时光科研",
    "沉沦之地",
    "炼狱塔",
    "侵袭",
    "仲裁",
    "仲裁表",
    "警报",
    "入侵",
    "九重天",
    "虚空风暴",
    "活动",
    "武形秘仪",
    "奸商",
    "虚空商人",
    "每日特惠",
    "商城折扣",
    "言录使",
    "氏族奖励",
    "出库",
    "阿耶",
    "灵化轮换",
    "终幕",
    "信条",
    "舰队进度",
    "新闻",
    "最近更新",
    "电波",
    "午夜电波",
    "日历",
    "结合目标",
    "wiki",
    "赤毒",
    "钢铁之路",
    "遗物",
    "核桃",
    "开核桃",
    "部件",
    "金垃圾",
    "银垃圾",
    "铜垃圾",
    "伤害",
    "紫卡分析",
    "wr",
    "紫卡",
    "rm",
    "wm",
    "趋势",
    "蹲",
    "基础形态",
}

bad_rows = []
for cell in lefts:
    # 取左列里用 / 或 ｜ 分出来的每个 token，剥掉参数尾巴
    toks = []
    for seg in cell.replace("｜", "/").split("/"):
        seg = seg.strip()
        if not seg:
            continue
        # 「伤害 武器 对 敌人 100级 膛线」这类取首词
        toks.append(seg.split()[0] if seg.split() else seg)
    if not any(t in VALID or t in ALLOW for t in toks):
        bad_rows.append(cell)
check("左列都是真实指令（无凭空标签）", not bad_rows, "；".join(bad_rows[:6]))

# —— 3. 分类标题不能是空组 ——
empty = [k for k, v in topic.items() if not v]
check("没有空的分类", not empty, "；".join(empty))

# —— 4. 需管理员的指令必须在卡面标注（指令行 + 注脚两处都查）——
# 「状态」与「群管理」两组都要过 _handle_admin 的 _is_admin 闸门（.状态 会列出
# 本群订阅明细，2026-09-18 安全审查收口；「状态」指令直接委托它）。卡面不写
# 「需群管理员」，普通成员照卡发就被拦 —— 卡是给用户照着发的，属误导。
# 闸门行为本身由 tests/test_admin.py 锁，这里只锁卡面标注与它对齐。
admin_rows = [
    cell
    for cell, desc in ((c, d) for items in topic.values() for c, d in items)
    if cell.split()[0] in ("状态", "群管理") and "管理员" not in desc
]
check("需管理员的指令行都标了「管理员」", not admin_rows, "；".join(admin_rows))
# 注脚里提到「状态 / 群管理」的每一条都必须带「管理员」字样（原「用法速查」行挪进注脚后的护栏）
admin_notes = [
    n
    for n in note_texts
    if ("状态" in n or "群管理" in n) and "管理员" not in n.split("状态", 1)[-1]
]
check(
    "注脚里的「状态 / 群管理」都标了「管理员」",
    not admin_notes and any("群管理" in n for n in note_texts),
    "；".join(admin_notes),
)

# —— 5. 左列「照发」都能落到一个真实 handler（2026-10-11 B11）——
# 判据（离线、不联网）：左列按「 / 」「｜」切段，每段 parse 出的 command 必须在
# main._build_routes 的路由表里；后续段不能独立解析时，带上首段的「指令 + 占位」前缀再试
# （「伤害 <武器> 灵化 / 基础形态」⇒「伤害 <武器> 基础形态」）。
# 「用法速查」里的修饰符行（平台 / 输出 / 翻页）挂到「夜灵」上验证；「群管理」走点号指令，不在此列。
# 额外：「伤害与配卡」组每段都必须落到伤害族 handler —— 旧写法「灵化 / 基础形态」
# 照发「灵化」会落到**灵化轮换**卡（incarnon），与该行说明对不上。
import re as _re  # noqa: E402

_src_main = (ROOT / "main.py").read_text(encoding="utf-8")
ROUTES = set(_re.findall(r'"(\w+)": self\._h_\w+', _src_main))
_DAMAGE_FAMILY = {"damage", "scan", "scandamage", "valence", "xh"}
_MODIFIER_ROWS = ("平台", "输出", "翻页")  # 旧「用法速查」组的行首词（组已撤，保留给旧数据兜底）
_bad_send: list[str] = []
for _grp, _items in topic.items():
    for _cell, _desc in _items:
        _head = _cell.split()[0]
        if _head == "群管理":
            continue
        _segs = [x.strip() for x in _re.split(r" / |｜", _cell) if x.strip()]
        if _head in _MODIFIER_ROWS:
            for _seg in _segs:
                _mods = [t for t in _seg.split() if t.startswith("-")]
                for _m in _mods:
                    _p = P.parse(f"夜灵 {_m}")
                    if not _p or _p.command != "cetus" or _p.unknown_modifiers:
                        _bad_send.append(f"夜灵 {_m}")
            continue
        _first = P.parse(_segs[0])
        _stem = " ".join(_segs[0].split()[:2]) if len(_segs[0].split()) > 1 else ""
        for _seg in _segs:
            _p = P.parse(_seg)
            if (not _p or _p.command not in ROUTES) and _stem:
                _p = P.parse(f"{_stem} {_seg}")
            if not _p or _p.command not in ROUTES:
                _bad_send.append(f"{_grp}:{_seg}")
            elif _grp == "伤害与配卡" and _p.command not in _DAMAGE_FAMILY:
                _bad_send.append(f"{_grp}:{_seg}→{_p.command}")
# 注脚里写的修饰后缀（-pc / -2 / -w …）也必须能照发：挂到「夜灵」上验证（语义同旧「用法速查」行）
_mods_in_notes = sorted(
    set(_re.findall(r"(?<![\w-])-(?:pc|ps|xb|sw|1|2|3|w|t|r)(?![\w-])", "\n".join(note_texts)))
)
for _m in _mods_in_notes:
    _p = P.parse(f"夜灵 {_m}")
    if not _p or _p.command != "cetus" or _p.unknown_modifiers:
        _bad_send.append(f"夜灵 {_m}")
check(
    "注脚里的修饰后缀（平台 / 输出 / 翻页）都能照发",
    len(_mods_in_notes) >= 9 and not [x for x in _bad_send if x.startswith("夜灵")],
    str(_mods_in_notes),
)
check(
    f"左列照发都能落到真实 handler（{len(ROUTES)} 条路由）",
    not _bad_send and bool(ROUTES),
    "；".join(_bad_send[:8]),
)

print()
if FAILED:
    print(f"FAILED {len(FAILED)}: " + ", ".join(FAILED))
    sys.exit(1)
print("ALL PASS ✔  帮助卡覆盖全部主指令")
