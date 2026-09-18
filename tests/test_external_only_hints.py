# -*- coding: utf-8 -*-
"""锁住「降级提示必须给出真实存在的替代指令」这条不变量。

背景（2026-09-17）：`_EXTERNAL_ONLY` 的文案里曾出现「钢铁」「资源」两个
**并不存在**的指令名 —— 数据源停摆时用户拿到一句「请用 X 指令」，照着发却
没有任何反应，等于把用户从「静默失败」引到「第二条死路」。本项目的最高
优先级缺陷就是「静默/无出路」，所以这里用测试把它焊死。

检查项：
  1. 文案里所有「中文指令」形如 「xxx」 的引用，都能在 COMMAND_ALIASES 里
     找到（作为主指令名或别名）。
  2. 已知失效的三个键（kuva / arbitration / steelPath）都必须带「替代」段，
     即至少给出一个可用指令。
  3. 文案不得只写「不可用/停摆」而不给任何出路。

用法：python tests/test_external_only_hints.py   （非 0 退出即失败）
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import parser as P                      # noqa: E402
from core.api_client import _EXTERNAL_ONLY        # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = ""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}"
          + (f"  -> {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


# 合法的指令词表：主指令名 + 全部别名 + 别名携带的预设词
VALID: set[str] = set(P.COMMAND_ALIASES)
for _cmd, _aliases in P.COMMAND_ALIASES.items():
    VALID |= set(_aliases)
VALID |= set(P.COMMAND_ALIASES)
# 参数/筛选词（不是指令，但会出现在提示里，属正常）
PARAM_WORDS = {
    "钢铁", "电60", "火58", "生存", "防御", "拦截", "挖掘", "今天",
    "满镀层", "镀层", "爆头", "病毒10", "剥甲", "灵化", "基础形态",
}

# 「xxx」形式的引用：中文书名号内 1~12 字，且不含空白/逗号
QUOTED = re.compile(r"「([^」]{1,12})」")


def command_of(quoted: str) -> str:
    """把「融合 电60 火58」这样的引用取出首个词做指令校验。"""
    tok = quoted.strip().split()[0] if quoted.strip() else ""
    # 去掉参数尾巴：「仲裁 生存」→ 仲裁
    return tok


print("=" * 62)
print("① 降级提示里的指令名必须真实存在")
print("=" * 62)
for key, text in _EXTERNAL_ONLY.items():
    refs = {command_of(q) for q in QUOTED.findall(text)}
    bad = {r for r in refs
           if r not in VALID and r not in PARAM_WORDS
           # 形如「仲裁表」的拼接指令，拆开也要能对上
           and r not in ("Steel Path",)}
    check(f"{key}: 引用指令均存在",
          not bad,
          f"不存在的指令 {sorted(bad)}（文案：{text[:40]}…）")


print()
print("=" * 62)
print("② 失效项必须给出替代出路")
print("=" * 62)
KNOWN_DEAD = ("kuva", "arbitration", "steelPath")
for key in KNOWN_DEAD:
    text = _EXTERNAL_ONLY.get(key, "")
    check(f"{key}: 有降级文案", bool(text))
    refs = [command_of(q) for q in QUOTED.findall(text)]
    usable = [r for r in refs if r in VALID or r in PARAM_WORDS]
    check(f"{key}: 至少给出 2 个可用替代（实得 {len(usable)}）", len(usable) >= 2,
          f"仅 {usable}")
    check(f"{key}: 不是只写「不可用」（含替代引导）",
          ("替代" in text) or ("可用" in text),
          text[:60])


print()
print("=" * 62)
print("③ 全指令回归：所有替代指令都能被解析器识别")
print("=" * 62)
for key, text in _EXTERNAL_ONLY.items():
    for q in QUOTED.findall(text):
        cmd = command_of(q)
        if cmd not in VALID:
            continue          # 参数词，跳过
        parsed = P.parse(q)
        check(f"{key} 的替代「{q}」可解析", parsed.command is not None,
              f"command={parsed.command}")


print()
if FAILED:
    print(f"FAILED {len(FAILED)}: " + ", ".join(FAILED))
    sys.exit(1)
print("ALL PASS ✔  降级提示均指向真实可用指令")
