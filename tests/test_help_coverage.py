# -*- coding: utf-8 -*-
"""锁住「帮助卡必须列全所有主指令」这条不变量。

背景（2026-09-17 用户反馈）：帮助卡改版时把 54 个主指令压成了 36 条描述性
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
    print(f"[{'PASS' if cond else 'FAIL'}] {name}"
          + (f"  -> {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


def load_help_topic() -> dict:
    tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign):
            tgt = node.target
            if isinstance(tgt, ast.Name) and tgt.id == "HELP_TOPIC":
                return ast.literal_eval(node.value)
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "HELP_TOPIC":
                    return ast.literal_eval(node.value)
    raise SystemExit("main.py 里没找到 HELP_TOPIC")


topic = load_help_topic()
lefts = [c for items in topic.values() for c, _ in items]
blob = "\n".join(lefts)

print("=" * 64)
print(f"帮助卡：{len(topic)} 个分类 / {len(lefts)} 条指令行")
print("=" * 64)

# —— 1. 每个主指令都必须出现 ——
missing = []
for cmd in sorted(P.COMMAND_ALIASES):
    names = set(P.COMMAND_ALIASES[cmd]) | {cmd}
    if not any(n and n in blob for n in names):
        missing.append(f"{cmd}（可用名：{'/'.join(sorted(names)[:3])}）")
check(f"54 个主指令全部出现在帮助卡里（实缺 {len(missing)}）", not missing,
      "；".join(missing))

# —— 2. 左列不能是纯描述性标签 ——
# 判据：左列里若出现「指令名 / 指令名」或单独一个词，至少要有一个 token
# 落在指令词表里，否则用户照着发是发不出来的。
VALID: set[str] = set(P.COMMAND_ALIASES)
for _c, _al in P.COMMAND_ALIASES.items():
    VALID |= set(_al)
# 允许出现的非指令词（修饰符 / 参数 / 分类提示）
ALLOW = {
    "帮助", "状态", "平台", "输出", "翻页", "群管理", "伤害", "灵化", "空战",
    "地面", "进化", "赤毒辐射60", "识卡", "识卡伤害", "武器融合", "玄骸",
    "倾向", "紫卡排行", "毒", "蹲", "夜灵", "赏金", "裂隙", "突击", "执刑官",
    "时效", "周报", "深层科研", "时光科研", "沉沦之地", "炼狱塔", "侵袭",
    "仲裁", "仲裁表", "警报", "入侵", "九重天", "虚空风暴", "活动", "武形秘仪",
    "奸商", "虚空商人", "每日特惠", "商城折扣", "言录使", "氏族奖励", "出库",
    "阿耶", "灵化轮换", "终幕", "信条", "舰队进度", "新闻", "最近更新", "电波",
    "午夜电波", "日历", "结合目标", "wiki", "赤毒", "钢铁之路", "对话助手",
    "遗物", "核桃", "开核桃", "部件", "金垃圾", "银垃圾", "铜垃圾", "伤害",
    "紫卡分析", "wr", "紫卡", "rm", "wm", "趋势", "蹲", "基础形态",
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
check("左列都是真实指令（无凭空标签）", not bad_rows,
      "；".join(bad_rows[:6]))

# —— 3. 分类标题不能是空组 ——
empty = [k for k, v in topic.items() if not v]
check("没有空的分类", not empty, "；".join(empty))

print()
if FAILED:
    print(f"FAILED {len(FAILED)}: " + ", ".join(FAILED))
    sys.exit(1)
print("ALL PASS ✔  帮助卡覆盖全部主指令")
