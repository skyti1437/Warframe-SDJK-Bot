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
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _txt(r) -> str:
    """Reply 的可读文本：raw_text 或 title + lines（报错卡片迁移后断言用）。"""
    if r is None:
        return ""
    return r.raw_text or (r.title + "\n" + "\n".join(r.lines))


from core import matching as M  # noqa: E402
from core.commands.wiki_misc import WikiMiscCommands  # noqa: E402
from core.parser import parse  # noqa: E402

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = ""):
    print(
        f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -> {detail}" if detail and not cond else "")
    )
    if not cond:
        FAILED.append(name)


# ---------------------------------------------------------------- ① 数据表
_tbl = ROOT / "core" / "data" / "de" / "name_bilingual.json"
_raw = json.loads(_tbl.read_text(encoding="utf-8"))
_pairs = _raw.get("pairs") or []
_size = _tbl.stat().st_size
check(f"数据表存在且条目充足（{len(_pairs)} 对）", len(_pairs) >= 4000, str(len(_pairs)))
check(f"数据表体积 ≤1.5MB（实际 {_size / 1024:.0f} KB）", _size <= 1536 * 1024)
# 生成逻辑：用内联 dict 对 fixture 跑 build()（不依赖未入库的 core/data/_cache）
_bspec = importlib.util.spec_from_file_location(
    "build_name_bilingual_under_test", ROOT / "scripts" / "build_name_bilingual.py"
)
_bnb = importlib.util.module_from_spec(_bspec)
_bspec.loader.exec_module(_bnb)
_L = "/Lotus/Language/"
_fx_en = {
    _L + "Weapons/AthodaiName": "Athodai",
    _L + "Primes/AthodaiAgain": "Athodai",  # 与上一条同对 ⇒ 去重
    _L + "Mods/VitalityName": "Vitality",
    _L + "ClanTech/MutagenMass": "Mutagen Mass",
    _L + "Weapons/AthodaiDesc": "Athodai",  # 描述类键尾 ⇒ 丢
    _L + "Menu/SettingsTitle": "Settings",  # 命名空间不在白名单 ⇒ 丢
    _L + "Items/FormaName": "Forma",  # en == zh ⇒ 丢
    _L + "Items/KuvaName": "Kuva",  # zh 无 CJK ⇒ 丢
    _L + "Suits/TooLong": "X" * 61,  # en > 60 ⇒ 丢
    _L + "Items/TwoLines": "A\nB",  # 含换行 ⇒ 丢
    _L + "Items/NoZh": "Orphan",  # zh 缺键 ⇒ 丢
}
_fx_zh = {
    _L + "Weapons/AthodaiName": "阿索代",
    _L + "Primes/AthodaiAgain": "阿索代",
    _L + "Mods/VitalityName": "生命力",
    _L + "ClanTech/MutagenMass": "突变原聚合物",
    _L + "Weapons/AthodaiDesc": "一把手枪",
    _L + "Menu/SettingsTitle": "设置",
    _L + "Items/FormaName": "Forma",
    _L + "Items/KuvaName": "Kuva2",
    _L + "Suits/TooLong": "长",
    _L + "Items/TwoLines": "甲",
}
_fx_cache = Path(tempfile.mkdtemp())
(_fx_cache / "dict.en.json").write_text(json.dumps(_fx_en), encoding="utf-8")
(_fx_cache / "dict.zh.json").write_text(json.dumps(_fx_zh, ensure_ascii=False), encoding="utf-8")
_real_cache, _bnb.CACHE = _bnb.CACHE, _fx_cache
try:
    _fx_pairs = _bnb.build()["pairs"]
finally:
    _bnb.CACHE = _real_cache
    shutil.rmtree(_fx_cache, ignore_errors=True)
check(
    "生成逻辑（fixture）：白名单命名空间 / 去描述键尾 / 去同名与无 CJK / 长度与换行 / 去重 / 按英文排序",
    _fx_pairs == [["Athodai", "阿索代"], ["Mutagen Mass", "突变原聚合物"], ["Vitality", "生命力"]],
    str(_fx_pairs),
)

# 全量重建：依赖构建机上的 DE language 表离线缓存（.gitignore 排除，不随包）⇒ 按能力执行
if (_bnb.CACHE / "dict.en.json").is_file():
    _r = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "build_name_bilingual.py"), "--check"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="ignore",
    )
    check("构建脚本可重跑（--check 通过）", _r.returncode == 0, (_r.stdout or "")[-200:])
else:
    print(
        "[SKIP] 全量重建 --check：本树无 core/data/_cache/dict.en.json"
        "（DE language 表离线缓存，构建机能力，未入库、不随包）"
    )

# ---------------------------------------------------------------- ② 归一化
check(
    "归一化：阿索代prime ≡ 阿索代 Prime ≡ 阿索代·Prime",
    M.normalize_name("阿索代prime")
    == M.normalize_name("阿索代 Prime")
    == M.normalize_name("阿索代·Prime"),
)
check(
    "归一化：全半角/大小写折叠（ＡＢＣ ≡ abc）",
    M.normalize_name("ＡＢＣ") == M.normalize_name("abc") == "abc",
)
check(
    "归一化：英文多空格等价（Corrosive  Projection ≡ corrosive projection）",
    M.normalize_name("Corrosive  Projection") == M.normalize_name("corrosive projection"),
)

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
check(
    "未收录：明确空结果（不猜）",
    M.bilingual_lookup("绝对不存在的词xyzzy").get("hits") == []
    and M.bilingual_lookup("绝对不存在的词xyzzy").get("candidates") == [],
)


# ---------------------------------------------------------------- ④ 指令出口
class _P:  # 最小 Parsed 桩：只用 content_str
    def __init__(self, t):
        self.content_str = t


def _run(q: str):
    rep = asyncio.run(WikiMiscCommands._h_translate(None, _P(q), None, "pc"))
    return rep


_r1 = _run("腐蚀投射")
check(
    "指令出口：纯文本（raw_text，无图片渲染）",
    _r1.raw_text == "[腐蚀投射] => Corrosive Projection",
    repr(_r1.raw_text),
)
_r2 = _run("Corrosive Projection")
check("指令出口：英→中", _r2.raw_text == "[Corrosive Projection] => 腐蚀投射", repr(_r2.raw_text))
_r3 = _run("绝对不存在的词xyzzy")
check(
    "指令出口：未找到明确提示（不静默）",
    "未找到" in _txt(_r3) and "怎么改" in _txt(_r3),
    repr(_txt(_r3)),
)
_r4 = _run("projection")
check(
    "指令出口：未精确命中给相近候选（每行一条）",
    _r4.raw_text and "相近候选" in _r4.raw_text and "=>" in _r4.raw_text,
    repr(_r4.raw_text),
)
_r5 = _run("")
check(
    "指令出口：空参给用法（报错卡含用法与怎么改）",
    "用法" in _txt(_r5) and "怎么改" in _txt(_r5),
    repr(_txt(_r5)),
)
check("路由：翻译 → translate", parse("翻译").command == "translate")

if FAILED:
    print(f"\n失败 {len(FAILED)} 项：{FAILED}")
    sys.exit(1)
print("\n全部通过 ✔")
