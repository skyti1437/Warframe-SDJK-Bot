# -*- coding: utf-8 -*-
"""轮换表武器译名守卫：python3 tests/test_rotation_names.py

背景（2026-09-14 用户报障）：「本周钢铁回廊灵化」卡里 45 把武器有 **29 把中文名是错的**
—— 长棍(Bo) / 铁拳(Furax) / 雷克斯手枪(Furis) / 猛椎(Magistar) / 索玛(Soma) …
全是照着英文字面音译/意译硬编的，与 DE 官方译名毫无关系（官方分别是
玻之武杖 / 弗拉克斯 / 盗贼 / 执法者 / 月神）。用户原话：「这个长棍太离谱了」。

权威来源（**全部离线**，不依赖网络；`core/data/de/name_zh.json` 由 build_de_data.py
从 DE 官方 PublicExport 生成）：

* **灵化**：灵化之源条目
  ``/lotus/types/items/miscitems/incarnonadapters/<类>/<武器>incarnonunlocker``
  值去掉「灵化之源」后缀即该武器官方中文名。
  例：``boincarnonunlocker`` = 「玻之武杖灵化之源」→ **玻之武杖**
* **信条 / 终幕**：同一份文件里以「信条·」/「终幕·」开头的官方名集合。

本测试把两张表逐条比对，任何一条对不上就失败 —— 以后改轮换表也不会再手滑。
"""
from __future__ import annotations

import io
import json
import re
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


_DE = ROOT / "core" / "data" / "de"
name_zh = json.load(io.open(_DE / "name_zh.json", encoding="utf-8"))
rot = json.load(io.open(ROOT / "core" / "data" / "rotations.json", encoding="utf-8"))

# ---------------------------------------------------------------- 官方译名抽取
_INC = re.compile(
    r"/lotus/types/items/miscitems/incarnonadapters/[a-z]+/([a-z0-9]+)incarnonunlocker$")
_incarnon_off: dict[str, str] = {}
for k, v in name_zh.items():
    m = _INC.match(k.lower())
    if m and isinstance(v, str) and v.endswith("灵化之源"):
        _incarnon_off[m.group(1)] = v[: -len("灵化之源")]

_tenet_off = {v for v in name_zh.values()
              if isinstance(v, str) and v.startswith("信条·")}
_coda_off = {v for v in name_zh.values()
             if isinstance(v, str) and v.startswith("终幕·")}

check("灵化之源条目抽到 45 条", len(_incarnon_off) == 45, str(len(_incarnon_off)))
check("信条· 官方名 ≥ 10 条", len(_tenet_off) >= 10, str(len(_tenet_off)))
check("终幕· 官方名 ≥ 10 条", len(_coda_off) >= 10, str(len(_coda_off)))


def norm(s: str) -> str:
    """英文名 → 与灵化之源文件名对齐的键（Ack & Brunt → ackandbrunt）。"""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower().replace("&", "and"))


# ---------------------------------------------------------------- ① 灵化（9 周 × 5）
weeks = rot["incarnon"]["weeks"]
check("灵化表为 9 周", len(weeks) == 9, str(len(weeks)))
check("灵化表每周 5 把", all(len(w) == 5 for w in weeks),
      str([len(w) for w in weeks]))

_n, _miss = 0, []
for wi, wk in enumerate(weeks, 1):
    for it in wk:
        en, cn = it.get("en", ""), it.get("cn", "")
        off = _incarnon_off.get(norm(en))
        if off is None:
            _miss.append((wi, en))
            continue
        _n += 1
        check(f"灵化 W{wi} {en} → {off}", cn == off, f"表里写的是「{cn}」")

check("灵化表每把武器都能对上官方条目", not _miss, str(_miss))
check("灵化表共核对 45 把", _n == 45, str(_n))
check("灵化表每把都有 variant（卡面要显示变体）",
      all(it.get("variant") for w in weeks for it in w))

# ---------------------------------------------------------------- ② 信条 / 终幕
for it in rot["tenet"].get("items") or []:
    check(f"信条 {it.get('en')}", it.get("cn") in _tenet_off,
          f"「{it.get('cn')}」不在官方「信条·」名单里")

_coda = [it for b in rot["coda"].get("batches") or [] for it in b]
check("终幕表非空", bool(_coda), str(len(_coda)))
for it in _coda:
    check(f"终幕 {it.get('en')}", it.get("cn") in _coda_off,
          f"「{it.get('cn')}」不在官方「终幕·」名单里")

# ---------------------------------------------------------------- ③ 反向守卫
# 报障的那几个错名必须**彻底消失**（防止有人又抄回来）
_BANNED = ["长棍", "铁拳", "雷克斯手枪", "猛椎", "钩镰", "双子恶煞", "割线",
           "熔岩冲击炮", "阿克里德与布伦特", "浪人索罗", "臭弹枪", "铁刺手套",
           "隐刃", "伽玛科尔", "安格斯特朗", "斯卡纳"]
_blob = json.dumps(rot["incarnon"], ensure_ascii=False)
_hit = [b for b in _BANNED if b in _blob]
check("灵化表不再出现已修正的错名", not _hit, str(_hit))

# 用户最先点名的两个
check("Bo 不再叫长棍", "长棍" not in _blob)
check("Bo 的 cn 是玻之武杖",
      next(it["cn"] for w in weeks for it in w if it["en"] == "Bo") == "玻之武杖")

if FAILED:
    print(f"\n失败 {len(FAILED)} 项：{FAILED}")
    sys.exit(1)
print("\n全部通过 ✔")
