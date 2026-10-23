# -*- coding: utf-8 -*-
"""从知识库 Markdown 抽取「物品简介条目」：core/data/wiki_intro.json

为什么需要
----------
`wiki <关键词>` 的卡片要给出**简短介绍**（战甲 / 武器 / MOD / 资源 / 同伴…）。
AstrBot 侧知识库（RAG）插件**不直连**，完整知识库
Markdown 也只在本机（warframe知识库/知识库/，技能 warframe-kb-build 产物），
所以构建期抽成随包数据文件，运行时离线查。

产物随开源 / 市场包分发；资产注释由知识库生成器写入，供同名候选保真。

条目格式（各文档统一）：`### 中文名（English）` + `- 字段：值` 行。

用法
----
    python3 scripts/build_wiki_intro.py                 # 用默认知识库路径
    python3 scripts/build_wiki_intro.py --kb-dir <路径>  # 或环境变量 WF_KB_OUT
"""

from __future__ import annotations

import argparse
import hashlib
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
_LINE_ITEM = re.compile(r"^(?P<zh>[^：（）()]+)[（(](?P<en>[^）)]+)[）)]：(?P<desc>.+)$")
# 标题含这些词的不是物品条目（技能详情是补充段；排行/一览是统计表）
SKIP_TITLE = ("技能详情", "排行", "一览")
# 06 的第四部分「制造配方」是配方清单，不是物品简介
SKIP_SECTION = ("制造配方",)

MAX_LEN = 100  # 单行截断长度
# 效果行例外：MOD 效果是整句（最长实测 309 字），截断会变成残句
# （翻译表按整句对齐，截断的键与译文都对不上）
MAX_LEN_EFFECT = 340

_TITLE_TAIL = re.compile(r"\s*[｜|].*$")  # 03 的标题带「｜类型 …」尾巴
_PAREN = re.compile(r"^(?P<zh>[^（(]+)[（(](?P<en>[^）)]+)[）)]\s*$")
# 材料行：「建造材料：合金板 ×1000｜生物质 ×500…」（战甲/武器/配方条目都有）
_MAT_LINE = re.compile(r"^(?:建造材料|所需材料)[：:](.+)$")
_MAT_ITEM = re.compile(r"^(?P<name>.+?)\s*×\s*(?P<n>\d+)$")
USES_MAX_PROD = 8  # 每个材料最多列几个产物

# ★ 2026-10-23（Claude 定案）：识别「代码名标题」用 —— 单 token ASCII 标识符
#   （`Borealsetmod` / `Entratilabdogtag`；带空格或连字符的如 `Ash Prime`、`AX-52` 不算）。
_CJK_RE = re.compile(r"[\u4e00-\u9fa5]")
_CODE_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*$")


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


# ★ 2026-10-22（返工版，Claude 审 7d14b93 的两条）：「获取」行的结构性词汉化。
#   ① **不设任何「译成空串」的规则** —— 上一版 `"The": ""` 把 `The Severed Warden` 削成
#      ` Severed Warden`（名字残缺 + 行首多空格，全量 80 处）；
#   ② **不逐词拆译专名** —— `Operation: Orphix Venom` → `Operation: 奥菲斯 Venom` 之类
#      中英夹杂比原文更难读。
#   现行口径：**只翻结构性词，且只在识别到的结构位置**；其余整段原样。实测结构形态：
#     `Planet/Node (Mission), Rotation X 20%` / `Planet/Node (Mission) 20%`
#     `Earth/Cetus (Level 50 - 70 Cetus Bounty), Rotation A 20%`
#     `Duviri/Endless: Tier 6 (Hard) 11.9%`
#   明确**不翻**（不落在任何结构里）：`Fomorian Sabotage`（专名+任务类型）、
#   `Elite Orm Weaver`（敌名）、`Nightmare Mode Rewards`（模式名）。
_PLANET_ZH = {
    "Mercury": "水星",
    "Venus": "金星",
    "Earth": "地球",
    "Mars": "火星",
    "Phobos": "火卫一",
    "Deimos": "火卫二",
    "Ceres": "谷神星",
    "Jupiter": "木星",
    "Europa": "欧罗巴",
    "Saturn": "土星",
    "Uranus": "天王星",
    "Neptune": "海王星",
    "Pluto": "冥王星",
    "Sedna": "赛德娜",
    "Eris": "阋神星",
    "Lua": "月球",
    "Void": "虚空",
    "Kuva Fortress": "赤毒要塞",
    "Cambion Drift": "恶魔漂移",
}
_MISSION_ZH = {
    "Spy": "间谍",
    "Defense": "防御",
    "Mobile Defense": "移动防御",
    "Survival": "生存",
    "Interception": "拦截",
    "Excavation": "挖掘",
    "Disruption": "中断",
    "Capture": "捕获",
    "Exterminate": "歼灭",
    "Sabotage": "破坏",
    "Rescue": "救援",
    "Hijack": "劫持",
    "Assault": "强袭",
    "Defection": "叛逃",
    "Pursuit": "追击",
    "Rush": "突袭",
    "Assassination": "刺杀",
    "Conclave": "武形秘仪",
    "Skirmish": "遭遇战",
    "Volatile": "易爆",
    "Orphix": "奥菲斯",
    "Isolation Vault": "隔离库",
    "Sanctuary Onslaught": "圣殿突袭",
    "Elite Sanctuary Onslaught": "精英圣殿突袭",
    "Circuit": "回廊",
    "Undercroft": "地下区",
    "Ascension": "扬升",
    "Alchemy": "炼金",
    "Mirror Defense": "镜像防御",
}
_DIFF_ZH = {"Normal": "普通", "Hard": "困难", "Elite": "精英"}
_ROTATION_RE = re.compile(r"\bRotation\s+([A-C])\b")
_LEVEL_RE = re.compile(r"\bLevel\s+(\d+)\s*-\s*(\d+)\b")
_TIER_RE = re.compile(r"\bTier\s+(\d+)\b")
_DIFF_RE = re.compile(r"\((Normal|Hard|Elite)\)")
_BOUNTY_RE = re.compile(r"\bBount(?:y|ies)\b")
# `星球/节点 …`：行星**只在段首这个位置**翻（别处一律不动，免得动到专名）
_LEAD_PLANET_RE = re.compile(r"(?<![A-Za-z])((?:[A-Z][A-Za-z'\-]*\s?){1,3})/(?=[A-Z])")
_PAREN_RE = re.compile(r"\(([A-Za-z][A-Za-z0-9 /\-]*)\)")


def translate_source(line: str) -> str:
    """「获取」行里的**结构性词** → 中文；**专名整段保持英文**。

    ★ **绝不删词**（没有译成空串的规则）⇒ 不会出现行首 / 冒号后多余空格。
    ★ 只做四类替换，且都限定在可识别的结构位置：
      ① `Rotation X` → `X轮`（与数据里已有的「C轮」同写法）；
      ② `Level a - b` / `Tier n` / `(Normal|Hard|Elite)` / `Duviri/Endless` / `Bounty`；
      ③ **段首**的 `行星/` → 中文行星；
      ④ `(任务类型)` → 括号内任务类型（只翻表内的）。
    """
    if not line.startswith("获取"):
        return line

    def _paren(m: re.Match) -> str:
        inner = m.group(1).strip()
        if inner in _MISSION_ZH:
            return f"（{_MISSION_ZH[inner]}）"
        out = _LEVEL_RE.sub(lambda x: f"等级 {x.group(1)} - {x.group(2)}", inner)
        out = _BOUNTY_RE.sub("赏金", out)
        return f"（{out}）" if out != inner else m.group(0)

    # ★ 顺序要紧：**先处理括号**（此时括号内还是纯 ASCII，`_PAREN_RE` 才认得出），
    #   再跑全局结构词 —— 否则 `Level` 会先把括号内容译成中文、括号正则就再也匹配不上。
    out = _PAREN_RE.sub(_paren, line)
    out = _ROTATION_RE.sub(lambda m: f"{m.group(1).upper()}轮", out)
    out = _LEVEL_RE.sub(lambda m: f"等级 {m.group(1)} - {m.group(2)}", out)
    out = _TIER_RE.sub(lambda m: f"层级 {m.group(1)}", out)
    out = _DIFF_RE.sub(lambda m: f"（{_DIFF_ZH[m.group(1)]}）", out)
    out = out.replace("Duviri/Endless", "双衍王境/无尽")

    def _planet(m: re.Match) -> str:
        zh = _PLANET_ZH.get(m.group(1).strip())
        return f"{zh}/" if zh else m.group(0)

    out = _LEAD_PLANET_RE.sub(_planet, out)
    # 中文全角括号前不留半角空格（`Ganymede （中断）` → `Ganymede（中断）`）
    return re.sub(r"\s+（", "（", out)


def untranslated_source_tokens(lines: list[str]) -> list[str]:
    """「获取」行里**仍剩的**英文 token（回执用）。"""
    left: set = set()
    for x in lines:
        s = str(x)
        if s.startswith("获取"):
            for w in re.findall(r"[A-Za-z][A-Za-z'\-]*", translate_source(s)):
                if len(w) > 1:
                    left.add(w)
    return sorted(left)


def dedup_effect_lines(lines: list[str]) -> list[str]:
    """「效果（满级 N）」与「效果（0 级）」**逐字相同**时丢掉 0 级那条（重复占位）。

    只在**完全相同**时丢；数值不同（正常的两档）一律保留。
    """
    fx = [i for i, x in enumerate(lines) if str(x).startswith("效果（")]
    if len(fx) < 2:
        return lines
    drop = set()
    for a in range(len(fx)):
        for b in range(a + 1, len(fx)):
            ta = str(lines[fx[a]]).split("）：", 1)[-1]
            tb = str(lines[fx[b]]).split("）：", 1)[-1]
            if ta and ta == tb:
                drop.add(fx[b])
    return [x for i, x in enumerate(lines) if i not in drop]


def parse_kb(
    path: Path, max_lines: int, identities: list[dict] | None = None
) -> tuple[list[tuple[str, list[str]]], list[tuple[str, str, int]]]:
    """解析一个知识库文档 → (卡片条目, 材料三元组)。

    材料三元组 ``(产物名, 材料名, 数量)`` 覆盖**全部条目**（含被卡片跳过的
    配方段/技能详情段），用于反查材料的「用途」。
    """
    out: list[tuple[str, list[str]]] = []
    mats: list[tuple[str, str, int]] = []
    title, lines, in_recipe, card_ok = "", [], False, False
    identity = {}

    def flush() -> None:
        if not title:
            return
        if lines and card_ok:
            out.append((title, dedup_effect_lines(lines)))
            if identities is not None:
                identities.append(dict(identity))
        for name, n in materials_of(lines):
            mats.append((_TITLE_TAIL.sub("", title).strip(), name, n))

    for raw in path.read_text(encoding="utf-8").splitlines():
        if raw.startswith("## "):
            flush()
            title, lines, identity = "", [], {}
            in_recipe = any(s in raw for s in SKIP_SECTION)
            continue
        if raw.startswith("### "):
            flush()
            t = raw[4:].strip()
            title = t
            identity = {}
            card_ok = (not in_recipe) and not any(s in t for s in SKIP_TITLE)
            lines = []
            continue
        if raw.startswith("<!-- wf-asset "):
            identity = json.loads(raw[len("<!-- wf-asset ") : -len(" -->")])
            continue
        if title and raw.startswith("- "):
            line = raw[2:].strip()
            cap = MAX_LEN_EFFECT if line.startswith("效果") else MAX_LEN
            if len(line) > cap:
                line = line[: cap - 1] + "…"
            if line.startswith("获取"):
                line = translate_source(line)
            # ★ 效果行**不在这里动** —— 数据里的效果行是 `wiki_effect_zh.json` 的**翻译键**，
            #   在这里改会打坏键（卫生放在 core/wiki_intro.py 的查表之后）。
            lines.append(line)
    flush()
    return ([(t, ls[:max_lines]) for t, ls in out], mats)


def parse_line_items(
    path: Path, identities: list[dict] | None = None
) -> list[tuple[str, list[str]]]:
    """解析「外观装饰与杂项」节里的紧凑单行条目 → [(标题, [说明]), …]。"""
    out: list[tuple[str, list[str]]] = []
    in_sec = False
    identity = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        if raw.startswith("## "):
            in_sec = LINE_ITEM_SECTION in raw
            identity = {}
            continue
        if in_sec and raw.startswith("<!-- wf-asset "):
            identity = json.loads(raw[len("<!-- wf-asset ") : -len(" -->")])
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
        if identities is not None:
            identities.append(dict(identity))
        identity = {}
    return out


def compact_cards(data: dict) -> dict:
    """随包只带卡片与数字引用；完整来源身份保存在构建旁挂件。"""
    identities = data["id_entries"]
    first = {asset_id: indices[0] for asset_id, indices in identities.items()}
    keys = {}
    for name, ids in data["keys"].items():
        indices = [first[asset_id] for asset_id in ids]
        keys[name] = indices[0] if len(indices) == 1 else indices
    meta = dict(data["_meta"])
    meta.update(
        schema=3,
        note="keys=归一化名→卡片数字引用（单候选为整数，多候选为列表）；"
        "引用同时区分资产；views 保留同资产其它视图；pvp 标记变体；完整来源见构建身份旁挂件。",
    )
    return {
        "_meta": meta,
        "entries": data["entries"],
        "keys": keys,
        "views": {
            str(indices[0]): indices[1:] for indices in identities.values() if len(indices) > 1
        },
        "pvp": sorted(first[a] for a in first if "pvp" in a.lower()),
        "uses": data["uses"],
    }


def merge_code_name_twins(
    entries: list, assets: list, id_entries: dict
) -> tuple[list, list, list, dict]:
    """把**代码名标题**的条目并入同 `de:` 资产的中文孪生条目（2026-10-23 Claude 定案）。

    背景：KB 里同一资产常有两张 —— 一张标题是内部代码名（如 `Borealsetmod`，type
    `Mod Set Mod`，是游戏内部的套装加成 Mod，玩家不会搜它），一张是正常显示名
    （如 `套装 · 诡文枭主（Boreal）`）。**直接改名会造出两张同名卡**（查「诡文枭主」撞名），
    故改为：代码名那条**缺的内容行补进孪生（去重）**、它**不再单独出条目**；
    它的代码名靠既有 `keys`（`keys_for(标题)`）继续当**检索别名**（不显示）。

    并入后 `entries` / `assets` / `id_entries` 的下标整体重映射（`keys` 存的是资产 ID，
    不受影响）。只认「同一 asset_id + 中文孪生 + 单 token ASCII 代码名」三条件齐备者。

    返回 ``(merged 报告, entries, assets, id_entries)``。
    """
    merged: list[dict] = []
    drop: set[int] = set()
    for aid, idxs in list(id_entries.items()):
        if len(idxs) < 2:
            continue
        twin = next((i for i in idxs if _CJK_RE.search(entries[i][0])), None)
        if twin is None:
            continue
        for i in idxs:
            if i == twin or i in drop:
                continue
            title = entries[i][0]
            if not _CODE_NAME_RE.match(title):
                continue
            added = [ln for ln in entries[i][1] if ln not in entries[twin][1]]
            entries[twin][1].extend(added)
            drop.add(i)
            merged.append(
                {
                    "code_name": title,
                    "asset_id": aid,
                    "into": entries[twin][0],
                    "added_lines": added,
                    "source_file": assets[i].get("source_file", ""),
                }
            )
    if not drop:
        return merged, entries, assets, id_entries
    keep = [i for i in range(len(entries)) if i not in drop]
    remap = {old: new for new, old in enumerate(keep)}
    new_id_entries = {
        aid: [remap[i] for i in idxs if i not in drop] for aid, idxs in id_entries.items()
    }
    return (
        merged,
        [entries[i] for i in keep],
        [assets[i] for i in keep],
        {k: v for k, v in new_id_entries.items() if v},
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--kb-dir",
        default=os.environ.get("WF_KB_OUT") or "",
        help="知识库产物目录（也可用环境变量 WF_KB_OUT）",
    )
    ap.add_argument(
        "--identity-out",
        type=Path,
        default=ROOT / "output/kb-build/wiki_intro.identity.json",
        help="完整资产身份构建旁挂件（不随插件分发）",
    )
    args = ap.parse_args()
    if not args.kb_dir:
        print(
            "未指定知识库目录：用 --kb-dir <路径> 或环境变量 WF_KB_OUT 指定"
            "（目录内应有 01_战甲.md / 02_武器.md 等知识库文档）"
        )
        return 1
    kb = Path(args.kb_dir)
    if not kb.is_dir():
        print(f"知识库目录不存在：{kb}")
        return 1

    entries: list[list] = []  # [[标题, [行…]], …]
    index: dict[str, list[str]] = {}
    assets: list[dict] = []
    id_entries: dict[str, list[int]] = {}  # 归一化键 → entries 下标
    uses: dict[str, dict] = {}  # 材料名(归一化) → {"n": 配方数, "p": [产物…]}
    dup = 0
    for name, budget in FILES.items():
        f = kb / name
        if not f.exists():
            print(f"  跳过缺失文件 {name}")
            continue
        n = 0
        identities: list[dict] = []
        if name == LINE_ITEM_FILE:
            # 08 只取第五节的外观装饰单行条目：其余小节是术语/机制/星图，
            # 不是物品简介（全收会把数据顶到 4 MB+）
            card_entries, mats = parse_line_items(f, identities), []
        else:
            card_entries, mats = parse_kb(f, budget, identities)
        for (title, lines), identity in zip(card_entries, identities):
            t = _TITLE_TAIL.sub("", title).strip()
            i = len(entries)
            entries.append([t, lines])
            meta = dict(identity)
            meta.setdefault(
                "asset_id",
                "kb:"
                + hashlib.sha256(
                    (name + "\n" + title + "\n" + "\n".join(lines)).encode()
                ).hexdigest(),
            )
            meta["source_file"] = name
            asset_id = meta["asset_id"]
            meta["variant"] = "pvp" if "pvp" in asset_id.lower() else "normal"
            assets.append(meta)
            id_entries.setdefault(asset_id, []).append(i)
            names = keys_for(title)
            if meta["variant"] == "pvp":
                names += [norm(k + " pvp") for k in names]
            for key in names:
                candidates = index.setdefault(key, [])
                if asset_id not in candidates:
                    candidates.append(asset_id)
                    n += 1
                else:
                    dup += 1
        for product, mat, _amt in mats:
            e = uses.setdefault(norm(mat), {"n": 0, "p": []})
            e["n"] += 1
            if len(e["p"]) < USES_MAX_PROD and product not in e["p"]:
                e["p"].append(product)
        print(f"  {name}: 收录 {n} 键 / 行预算 {budget}（材料用量 {len(mats)} 条）")

    # ★ 2026-10-23（Claude 定案）：**代码名标题的条目并入同资产的中文孪生条目**（见函数注释）。
    merged, entries, assets, id_entries = merge_code_name_twins(entries, assets, id_entries)
    if merged:
        print(f"  代码名标题并入孪生：{len(merged)} 条（条目数 −{len(merged)}）")
        for _m in merged:
            print(f"    {_m['code_name']} → {_m['into']}（补 {len(_m['added_lines'])} 行）")

    uses = {k: v for k, v in sorted(uses.items()) if v["n"] >= 1}
    full_data = {
        "_meta": {
            "source": "知识库 Markdown（warframe-kb-build 产物）构建期抽取，"
            "见 scripts/build_wiki_intro.py",
            "schema": 2,
            "note": "keys=归一化名 → 资产 ID 列表；id_entries=资产 ID → entries 下标列表；"
            "uses=材料名 → 配方数 + 前几个产物（反查「用途」）。"
            "卡片用；随开源 / 市场包分发（2026-09-25 用户拍板）。",
            "generated": time.strftime("%Y-%m-%d"),
            "count": len(entries),
            "uses": len(uses),
        },
        "entries": entries,
        "keys": dict(sorted(index.items())),
        "assets": assets,
        "id_entries": id_entries,
        "uses": uses,
    }
    identity_bytes = (json.dumps(full_data, ensure_ascii=False, indent=0) + "\n").encode("utf-8")
    args.identity_out.parent.mkdir(parents=True, exist_ok=True)
    args.identity_out.write_bytes(identity_bytes)
    runtime_data = compact_cards(full_data)
    runtime_data["_meta"]["identity_sha256"] = hashlib.sha256(identity_bytes).hexdigest()
    OUT.write_text(
        json.dumps(runtime_data, ensure_ascii=False, separators=(",", ": ")) + "\n",
        encoding="utf-8",
    )
    print(f"完整资产身份旁挂件：{args.identity_out}")

    size = OUT.stat().st_size
    print(
        f"\n写入 {OUT.relative_to(ROOT)}：{len(entries)} 个条目 / {len(index)} 键"
        f" / {len(uses)} 种材料，{size / 1024:.0f} KB"
    )
    for probe in ("托里德", "torid", "nekros prime", "膛线", "serration", "搬运者", "ash prime"):
        ids = index.get(norm(probe), [])
        i = id_entries[ids[0]][0] if ids else None
        print(f"   {probe:14s} -> {entries[i][0] if i is not None else '（无）'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
