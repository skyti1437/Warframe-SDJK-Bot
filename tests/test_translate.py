# -*- coding: utf-8 -*-
"""B1「翻译」指令回归：python3 tests/test_translate.py

覆盖四件事：
1. 构建期数据表完整（core/data/de/name_bilingual.json 条数/体积/可重跑 --check）；
2. 归一化（C1 共用入口）：「阿索代prime」≡「阿索代 Prime」≡「阿索代·Prime」、
   全半角/大小写折叠；
3. 双语查询：中→英、英→中、用户线上实测词（腐蚀投射/生命力/突变原聚合物/
   athodai/阿索代prime）与未收录的明确提示；
4. 指令出口：纯文本（Reply.raw_text，不走图片渲染）、路由、候选与未找到文案。
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import matching as M                          # noqa: E402
from core.commands.wiki_misc import WikiMiscCommands    # noqa: E402
from core.parser import parse                           # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = ""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}"
          + (f"  -> {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


# ---------------------------------------------------------------- ① 数据表
_tbl = ROOT / "core" / "data" / "de" / "name_bilingual.json"
_raw = json.loads(_tbl.read_text(encoding="utf-8"))
_pairs = _raw.get("pairs") or []
_size = _tbl.stat().st_size
check(f"数据表存在且条目充足（{len(_pairs)} 对）", len(_pairs) >= 4000, str(len(_pairs)))
check(f"数据表体积 ≤1.5MB（实际 {_size / 1024:.0f} KB）", _size <= 1536 * 1024)
_r = subprocess.run([sys.executable, str(ROOT / "scripts" / "build_name_bilingual.py"),
                     "--check"], capture_output=True, text=True,
                    encoding="utf-8", errors="ignore")
check("构建脚本可重跑（--check 通过）", _r.returncode == 0, (_r.stdout or "")[-200:])

# ---------------------------------------------------------------- ② 归一化
check("归一化：阿索代prime ≡ 阿索代 Prime ≡ 阿索代·Prime",
      M.normalize_name("阿索代prime") == M.normalize_name("阿索代 Prime")
      == M.normalize_name("阿索代·Prime"))
check("归一化：全半角/大小写折叠（ＡＢＣ ≡ abc）",
      M.normalize_name("ＡＢＣ") == M.normalize_name("abc") == "abc")
check("归一化：英文多空格等价（Corrosive  Projection ≡ corrosive projection）",
      M.normalize_name("Corrosive  Projection")
      == M.normalize_name("corrosive projection"))

# ---------------------------------------------------------------- ③ 双语查询
_cases = [
    ("腐蚀投射", "Corrosive Projection"),
    ("生命力", "Vitality"),
    ("突变原聚合物", "Mutagen Mass"),
    ("athodai", "阿索代"),
    ("阿索代prime", "阿索代 Prime"),
    ("Corrosive Projection", "腐蚀投射"),
    ("Athodai Prime", "阿索代 Prime"),
]
for q, want in _cases:
    r = M.bilingual_lookup(q)
    got = {dst for _src, dst in r["hits"]} | {src for src, _dst in r["hits"]}
    check(f"翻译查询：{q} → {want}", want in got, str(r))
check("未收录：明确空结果（不猜）",
      M.bilingual_lookup("绝对不存在的词xyzzy").get("hits") == []
      and M.bilingual_lookup("绝对不存在的词xyzzy").get("candidates") == [])

# ---------------------------------------------------------------- ④ 指令出口
class _P:  # 最小 Parsed 桩：只用 content_str
    def __init__(self, t):
        self.content_str = t


def _run(q: str):
    rep = asyncio.run(WikiMiscCommands._h_translate(None, _P(q), None, "pc"))
    return rep


_r1 = _run("腐蚀投射")
check("指令出口：纯文本（raw_text，无图片渲染）",
      _r1.raw_text == "[腐蚀投射] => Corrosive Projection", repr(_r1.raw_text))
_r2 = _run("Corrosive Projection")
check("指令出口：英→中", _r2.raw_text == "[Corrosive Projection] => 腐蚀投射",
      repr(_r2.raw_text))
_r3 = _run("绝对不存在的词xyzzy")
check("指令出口：未找到明确提示（不静默）",
      _r3.raw_text and "未找到" in _r3.raw_text, repr(_r3.raw_text))
_r4 = _run("projection")
check("指令出口：未精确命中给相近候选（每行一条）",
      _r4.raw_text and "相近候选" in _r4.raw_text
      and "=>" in _r4.raw_text, repr(_r4.raw_text))
_r5 = _run("")
check("指令出口：空参给用法", _r5.raw_text and "用法" in _r5.raw_text,
      repr(_r5.raw_text))
check("路由：翻译 → translate", parse("翻译").command == "translate")

if FAILED:
    print(f"\n失败 {len(FAILED)} 项：{FAILED}")
    sys.exit(1)
print("\n全部通过 ✔")
