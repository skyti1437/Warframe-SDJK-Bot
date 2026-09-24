# -*- coding: utf-8 -*-
"""从知识库 Markdown 抽取「物品简介条目」：core/data/wiki_intro.json

为什么需要
----------
`wiki <关键词>` 的卡片要给出**简短介绍**（战甲 / 武器 / MOD / 资源 / 同伴…）。
AstrBot 侧知识库（RAG）插件**不直连**（见 main.py::_kb_hint），完整知识库
Markdown 也只在本机（warframe知识库/知识库/，技能 warframe-kb-build 产物），
所以构建期抽成随包数据文件，运行时离线查。

⚠️ 产物**不进开源 / 市场包**（dist/package_release.py::EXCLUDE_FILES 已列）：
公开版 wiki 只给链接，简介卡片只在本机部署（有简介数据）时生效。

条目格式（各文档统一）：`### 中文名（English）` + `- 字段：值` 行。

用法
----
    python3 scripts/build_wiki_intro.py                 # 用默认知识库路径
    python3 scripts/build_wiki_intro.py --kb-dir <路径>  # 或环境变量 WF_KB_OUT
"""
from __future__ import annotations

import argparse
import json
import os
import re
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "core" / "data" / "wiki_intro.json"

# 参与抽取的知识库文件 → 每条保留几行（卡片要「简短」；MOD 条目多、收紧）
FILES: dict[str, int] = {
    "01_战甲.md": 6,
    "02_武器.md": 7,
    "03_MOD与赋能.md": 4,
    "06_资源与蓝图.md": 3,
    "07_同伴与空战.md": 5,
    "08_其他与机制术语.md": 3,
}
# 08 第五节「外观装饰与杂项」是**紧凑单行条目**（`- 名称（English）：说明`，
# 4856 条：披饰/外观/浮印/徽章/装饰），不走 ###-字段 口径，单独解析。
LINE_ITEM_FILE = "08_其他与机制术语.md"
LINE_ITEM_SECTION = "外观装饰与杂项"
_LINE_ITEM = re.compile(
    r"^(?P<zh>[^：（）()]+)[（(](?P<en>[^）)]+)[）)]：(?P<desc>.+)$")
# 标题含这些词的不是物品条目（技能详情是补充段；排行/一览是统计表）
SKIP_TITLE = ("技能详情", "排行", "一览")
# 06 的第四部分「制造配方」是配方清单，不是物品简介
SKIP_SECTION = ("制造配方",)

MAX_LEN = 100        # 单行截断长度
# 效果行例外：MOD 效果是整句（最长实测 309 字），截断会变成残句
# （翻译表按整句对齐，截断的键与译文都对不上）
MAX_LEN_EFFECT = 340

_TITLE_TAIL = re.compile(r"\s*[｜|].*$")          # 03 的标题带「｜类型 …」尾巴
_PAREN = re.compile(r"^(?P<zh>[^（(]+)[（(](?P<en>[^）)]+)[）)]\s*$")
# 材料行：「建造材料：合金板 ×1000｜生物质 ×500…」（战甲/武器/配方条目都有）
_MAT_LINE = re.compile(r"^(?:建造材料|所需材料)[：:](.+)$")
_MAT_ITEM = re.compile(r"^(?P<name>.+?)\s*×\s*(?P<n>\d+)$")
USES_MAX_PROD = 8          # 每个材料最多列几个产物


def norm(s: str) -> str:
    """与 core/search.py::_norm 同口径。"""
    return " ".join(re.sub(r"[^0-9a-z一-鿿]+", " ", (s or "").lower()).split())


def keys_for(title: str) -> list[str]:
    """条目标题 → 可命中的归一化键（整名 / 中文部分 / 英文部分）。"""
    t = _TITLE_TAIL.sub("", title).strip()
    out = [norm(t)]
    m = _PAREN.match(t)
    if m:
        out.append(norm(m.group("zh")))
        out.append(norm(m.group("en")))
    return [k for k in dict.fromkeys(out) if k]


def materials_of(lines: list[str]) -> list[tuple[str, int]]:
    """条目行 → 建造/所需材料 [(名, 数量), …]（用于反查材料的「用途」）。"""
    for line in lines:
        m = _MAT_LINE.match(line)
        if not m:
            continue
        out: list[tuple[str, int]] = []
        for part in m.group(1).split("｜"):
            mm = _MAT_ITEM.match(part.strip())
            if mm and mm.group("name").strip():
                out.append((mm.group("name").strip(), int(mm.group("n"))))
        return out
    return []


def parse_kb(path: Path, max_lines: int) -> tuple[list[tuple[str, list[str]]],
                                                  list[tuple[str, str, int]]]:
    """解析一个知识库文档 → (卡片条目, 材料三元组)。

    材料三元组 ``(产物名, 材料名, 数量)`` 覆盖**全部条目**（含被卡片跳过的
    配方段/技能详情段），用于反查材料的「用途」。
    """
    out: list[tuple[str, list[str]]] = []
    mats: list[tuple[str, str, int]] = []
    title, lines, in_recipe, card_ok = "", [], False, False

    def flush() -> None:
        if not title:
            return
        if lines and card_ok:
            out.append((title, lines))
        for name, n in materials_of(lines):
            mats.append((_TITLE_TAIL.sub("", title).strip(), name, n))

    for raw in path.read_text(encoding="utf-8").splitlines():
        if raw.startswith("## "):
            flush()
            title, lines = "", []
            in_recipe = any(s in raw for s in SKIP_SECTION)
            continue
        if raw.startswith("### "):
            flush()
            t = raw[4:].strip()
            title = t
            card_ok = (not in_recipe) and not any(s in t for s in SKIP_TITLE)
            lines = []
            continue
        if title and raw.startswith("- "):
            line = raw[2:].strip()
            cap = MAX_LEN_EFFECT if line.startswith("效果") else MAX_LEN
            if len(line) > cap:
                line = line[: cap - 1] + "…"
            lines.append(line)
    flush()
    return ([(t, ls[:max_lines]) for t, ls in out], mats)


def parse_line_items(path: Path) -> list[tuple[str, list[str]]]:
    """解析「外观装饰与杂项」节里的紧凑单行条目 → [(标题, [说明]), …]。"""
    out: list[tuple[str, list[str]]] = []
    in_sec = False
    for raw in path.read_text(encoding="utf-8").splitlines():
        if raw.startswith("## "):
            in_sec = LINE_ITEM_SECTION in raw
            continue
        if not in_sec or not raw.startswith("- "):
            continue
        m = _LINE_ITEM.match(raw[2:].strip())
        if not m:
            continue
        zh = m.group("zh").strip()
        en = m.group("en").strip()
        desc = m.group("desc").strip()
        if not zh or not desc:
            continue
        out.append((f"{zh}（{en}）", [desc[:MAX_LEN]]))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kb-dir", default=os.environ.get("WF_KB_OUT") or "",
                    help="知识库产物目录（也可用环境变量 WF_KB_OUT）")
    args = ap.parse_args()
    if not args.kb_dir:
        print("未指定知识库目录：用 --kb-dir <路径> 或环境变量 WF_KB_OUT 指定"
              "（目录内应有 01_战甲.md / 02_武器.md 等知识库文档）")
        return 1
    kb = Path(args.kb_dir)
    if not kb.is_dir():
        print(f"知识库目录不存在：{kb}")
        return 1

    entries: list[list] = []                 # [[标题, [行…]], …]
    index: dict[str, int] = {}               # 归一化键 → entries 下标
    uses: dict[str, dict] = {}               # 材料名(归一化) → {"n": 配方数, "p": [产物…]}
    dup = 0
    for name, budget in FILES.items():
        f = kb / name
        if not f.exists():
            print(f"  跳过缺失文件 {name}")
            continue
        n = 0
        if name == LINE_ITEM_FILE:
            # 08 只取第五节的外观装饰单行条目：其余小节是术语/机制/星图，
            # 不是物品简介（全收会把数据顶到 4 MB+）
            card_entries, mats = parse_line_items(f), []
        else:
            card_entries, mats = parse_kb(f, budget)
        for title, lines in card_entries:
            t = _TITLE_TAIL.sub("", title).strip()
            i = len(entries)
            entries.append([t, lines])
            for k in keys_for(title):
                if k in index:
                    dup += 1
                    continue
                index[k] = i
                n += 1
        for product, mat, _amt in mats:
            e = uses.setdefault(norm(mat), {"n": 0, "p": []})
            e["n"] += 1
            if len(e["p"]) < USES_MAX_PROD and product not in e["p"]:
                e["p"].append(product)
        print(f"  {name}: 收录 {n} 键 / 行预算 {budget}"
              f"（材料用量 {len(mats)} 条）")

    uses = {k: v for k, v in sorted(uses.items()) if v["n"] >= 1}
    OUT.write_text(json.dumps({
        "_meta": {
            "source": "知识库 Markdown（warframe-kb-build 产物）构建期抽取，"
                      "见 scripts/build_wiki_intro.py",
            "note": "keys=归一化名 → entries 下标；entries=[标题, 行…]；"
                    "uses=材料名 → 配方数 + 前几个产物（反查「用途」）。"
                    "卡片用；本文件不进开源/市场包。",
            "generated": time.strftime("%Y-%m-%d"),
            "count": len(entries),
            "uses": len(uses),
        },
        "entries": entries,
        "keys": dict(sorted(index.items())),
        "uses": uses,
    }, ensure_ascii=False, indent=0) + "\n", encoding="utf-8")

    size = OUT.stat().st_size
    print(f"\n写入 {OUT.relative_to(ROOT)}：{len(entries)} 个条目 / {len(index)} 键"
          f" / {len(uses)} 种材料，{size / 1024:.0f} KB")
    for probe in ("托里德", "torid", "nekros prime", "膛线", "serration",
                  "搬运者", "ash prime"):
        i = index.get(norm(probe))
        print(f"   {probe:14s} -> {entries[i][0] if i is not None else '（无）'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
