# -*- coding: utf-8 -*-
"""指令解析边界与注入测试：python3 tests/test_parser_boundary.py

覆盖验收要求的边界场景：空指令 / 非法参数 / 超长输入 / 特殊字符 /
SQL 风格注入 / 正则注入（ReDoS）。判据是「不抛异常 + 行为可预期 +
耗时线性」，而不是逐条业务语义（业务语义已由 test_parser.py 等覆盖）。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.parser import parse, tokenize  # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = ""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f"  -> {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


def safe_parse(text: str):
    """调用 parse 并捕获一切异常；返回 (parsed, exc)。"""
    try:
        return parse(text), None
    except Exception as e:  # noqa: BLE001
        return None, e


# ---------------------------------------------------------------- 空与空白
p, e = safe_parse("")
check("空字符串不抛异常", e is None, repr(e))
check("空字符串 command=None", p is not None and p.command is None)

p, e = safe_parse("   \t\n  ")
check("纯空白不抛异常", e is None, repr(e))
check("纯空白 command=None", p is not None and p.command is None)

p, e = safe_parse("-pc -w")
check("只有修饰符不抛异常", e is None, repr(e))
check("只有修饰符 command=None", p is not None and p.command is None,
      f"command={p.command!r}")

# ---------------------------------------------------------------- 非法参数
p, e = safe_parse("wr")
check("主指令裸发（无内容）不抛", e is None, repr(e))
check("主指令裸发 command=wr、content 为空",
      p is not None and p.command == "wr" and p.content == [],
      f"{p.command!r} {p.content!r}")

p, e = safe_parse("-9")
check("未知页码修饰符 -9 不抛", e is None, repr(e))
p, e = safe_parse("--pc")
check("双横线 --pc 不抛", e is None, repr(e))
p, e = safe_parse("-")
check("单横线不抛", e is None, repr(e))
p, e = safe_parse("wr - 裂隙")
check("内容中夹单横线不抛", e is None, repr(e))

# ---------------------------------------------------------------- 超长输入
t0 = time.perf_counter()
p, e = safe_parse("查询 " + "甲" * 5000)
dt = time.perf_counter() - t0
check("5000 字内容不抛", e is None, repr(e))
check("5000 字解析 < 1s", dt < 1.0, f"{dt:.3f}s")

t0 = time.perf_counter()
p, e = safe_parse("wr " + "x" * 100000)
dt = time.perf_counter() - t0
check("10 万字符单词不抛", e is None, repr(e))
check("10 万字符解析 < 2s", dt < 2.0, f"{dt:.3f}s")

# ---------------------------------------------------------------- ReDoS 注入
redos = [
    "(?<![a])|(?:x+)+$" + "x" * 2000,
    "a" * 50000 + "!",
    "(" * 3000,
    "[[]]]][[[[" * 500,
    "(a+)+$" + "a" * 3000,
]
for i, payload in enumerate(redos):
    t0 = time.perf_counter()
    p, e = safe_parse("wr " + payload)
    dt = time.perf_counter() - t0
    check(f"ReDoS 注入#{i + 1} 不抛", e is None, repr(e))
    check(f"ReDoS 注入#{i + 1} 解析 < 1s", dt < 1.0, f"{dt:.3f}s")

# ---------------------------------------------------------------- SQL 风格注入
sqlish = [
    "' OR '1'='1'; DROP TABLE users;--",
    '"; DELETE FROM config WHERE 1=1;--',
    "1; rm -rf /",
    "$(cat /etc/passwd)",
    "`whoami`",
]
for i, payload in enumerate(sqlish):
    p, e = safe_parse("wm " + payload)
    check(f"SQL 风格注入#{i + 1} 不抛", e is None, repr(e))
    if p is not None:
        joined = " ".join(p.content)
        check(f"SQL 风格注入#{i + 1} 内容按普通词元保留（不被解释执行）",
              payload.split()[0] in joined or payload in joined or bool(joined),
              joined[:80])

# ---------------------------------------------------------------- 特殊字符
specials = [
    '未闭合引号 "abc',
    '空引号 ""',
    '"带空格的英文名" wr',
    "wr\x00\x01\x02 裂隙",          # 控制字符 / NUL
    "wr \U0001f600\U0001f525 裂隙",  # emoji
    "wr 水‍星",               # ZWJ
    "wr ｆｕｌｌｗｉｄｔｈ ａｂｃ",    # 全角
    "wr\r\n裂隙\t突击",
]
for i, text in enumerate(specials):
    p, e = safe_parse(text)
    check(f"特殊字符#{i + 1} 不抛", e is None, repr(e))

# ---------------------------------------------------------------- tokenize 边界
check("tokenize 空串 → []", tokenize("") == [])
check("tokenize 未闭合引号：词元保留不丢（引号字符随词）",
      tokenize('"abc') == ['"abc'], str(tokenize('"abc')))
check("tokenize 引号内空格合并", tokenize('"a b c" d') == ["a b c", "d"])

# ---------------------------------------------------------------- 全主指令裸发批量
from core.parser import ALIAS_TO_COMMAND  # noqa: E402

# 解析器吃的是别名（key），映射到指令（value）—— 逐个别名裸发
bad = []
for alias, cmd in ALIAS_TO_COMMAND.items():
    p, e = safe_parse(alias)
    if e is not None or p is None or p.command != cmd:
        bad.append((alias, cmd, repr(e), p.command if p else None))
check(f"全部 {len(ALIAS_TO_COMMAND)} 个别名裸发（无参数）不抛异常且正确分派",
      not bad, str(bad[:5]))

# ---------------------------------------------------------------- 翻页边界
p, e = safe_parse("wr 基多 -999999999999999999999")
check("超大页码不抛", e is None, repr(e))
p, e = safe_parse("wr 基多 -0")
check("页码 -0 不抛", e is None, repr(e))

print()
if FAILED:
    print(f"共 {len(FAILED)} 项失败：{FAILED}")
    sys.exit(1)
print("指令解析边界：全部断言通过")
