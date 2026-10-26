# -*- coding: utf-8 -*-
"""灵化（Incarnon）进化选项 → core/data/evolutions.json。

数据源（2026-10-26 wfsim 迁移 P3 后半起；之前取自 wfsim data/evolutions，已弃用）：英文 wiki 各武器的灵化页
（「<武器> Incarnon Genesis」，天生灵化武器为武器页本身）里的「Evolutions」表，CC BY-NC-SA 3.0。页面原文存在
``<wiki 目录>/evo/``（本机低频取，不在服务器上抓 wiki）。

表格怎么读（``parse_page``）：
- 表头每个 ``{{Weapon|名}}`` = 一列变体（Braton 页四列：Braton / Mk1 / Vandal / Prime）；
- 「! EVOn」= 第 n 层；每个选项一行：名字格、描述格（条目列表，数值可写 X / Y 占位符）、各变体数值格
  （「X = 24<br>Y = 30」；跨列合并的「-」= 各变体相同）、备注格；「Evolution Challenge」行跳过。

效果怎么解析（``parse_effects``）：描述按条目、句子切开；带「On …: / With …: / While …:」这类条件前缀的句子
（及同一条目里它后面的句子、它下面的子条目）记为条件效果、不折算；句中带「on undamaged / for 4s / if / when …」
的句子同样不折算；只对灵化形态生效的句子（「for Incarnon Form」「Effect doubles in Incarnon Form」）作为同种
效果的 ``incarnon_value``（选「灵化」时用）。「Increase Base Status Chance to N%」按 by 处理
（+N%；Sicarus，维护者 10-26 定）。无条件句按固定措辞匹配计算器会折算的种类：
基伤 / 基础暴击 / 基础暴伤 / 基础触发 / 弹匣 / 弹药上限 / 多重 / 射速 / 装填 / 爆头伤害 / 初始连击 / 穿透 /
精准 / 后坐力 / 近战范围 / 震地范围（``_modellable`` = true）；其余原文存档（kind ``other``）。

key = 武器表（weapons_stats）里该武器英文名转小写、空白与撇号换成下划线（= damage_calc._weapon_family_key）；
武器表没有的变体按 wiki 名生成同样的键。

用法：python scripts/build_evolutions.py --wiki <wiki 页面目录> [--out 路径] [--report 路径]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _wiki_weapons import load_weapons, slug  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "core" / "data" / "evolutions.json"
WS = ROOT / "core" / "data" / "weapons_stats.json"

_NUM = r"(\d+(?:\.\d+)?)"
# (kind, 正则, 换算)：只用于无条件句
EFFECT_PATTERNS = [
    # 「Increase Damage by +70」（Kunai / Despair，不带 Base）同为平坦基伤；带 % 的是乘区加成，不算
    ("flat_base_damage", rf"Increase (?:Base )?Damage by \+?{_NUM}(?![\d.]*\s*%)", 1.0),
    # 「of current …」= 按另一属性折算（Cestra / Dera「30% of current Critical Chance」），不是平坦加成
    ("flat_base_crit_chance", rf"Increase (?:Base )?Critical Chance by \+?{_NUM}%(?!\s*of)", 0.01),
    (
        "flat_base_crit_multiplier",
        rf"Increase Base Critical (?:Damage )?Multiplier by \+?{_NUM}x",
        1.0,
    ),
    ("flat_base_status_chance", rf"Increase (?:Base )?Status Chance by \+?{_NUM}%(?!\s*of)", 0.01),
    ("flat_base_magazine", rf"Increase Base Magazine Capacity by \+?{_NUM}", 1.0),
    ("flat_base_magazine", rf"(?<![\w.])\+{_NUM} Base Magazine Capacity", 1.0),
    ("ammo_reserve_set", rf"Increase Base Ammo (?:Capacity|Maximum) to {_NUM}", 1.0),
    ("flat_base_multishot", rf"(?<![\w.])\+{_NUM}% Multishot", 0.01),
    (
        "flat_base_multishot",
        rf"(?<![\w.])\+{_NUM} Base Multishot",
        1.0,
    ),  # Bronco「+2 Base Multishot」
    ("fire_rate_bonus", rf"(?<![\w.])\+{_NUM}% Fire Rate", 0.01),
    (
        "fire_rate_bonus",
        rf"(?<![\w.])\+{_NUM}% Attack Speed",
        0.01,
    ),  # 近战攻速 = 计算器射速（Praedos）
    ("reload_speed_bonus", rf"(?<![\w.])\+{_NUM}% Reload Speed", 0.01),
    ("headshot_damage", rf"(?<![\w.])\+{_NUM}% Headshot (?:Damage|Multiplier)", 0.01),
    ("initial_combo", rf"(?<![\w.])\+{_NUM} Initial Combo", 1.0),
    ("punch_through_bonus", rf"(?<![\w.])\+{_NUM} Punch Through", 1.0),
    ("accuracy_bonus", rf"(?<![\w.])\+{_NUM}% Accuracy", 0.01),
    ("accuracy_bonus", rf"Increase Accuracy by \+?{_NUM}%", 0.01),
    ("recoil_reduction", rf"(?<![\w.])-{_NUM}% (?:Weapon )?Recoil", 0.01),  # 值 = 减少量
    ("melee_range_bonus_m", rf"(?<![\w.])\+{_NUM} (?:Melee )?Range\b", 1.0),
    ("slam_radius_bonus", rf"(?<![\w.])\+{_NUM}% Slam Radius", 0.01),
]
EFFECT_PATTERNS = [(k, re.compile(p, re.I), sc) for k, p, sc in EFFECT_PATTERNS]
# 句首条件前缀：「On Headshot:」「With Overshields:」「While Aiming:」…（冒号前 2–80 个字符、不含句号）
_COND = re.compile(r"^\s*[A-Z][^:.]{1,80}:(\s|$)")
# 只对某一形态生效的句子（Boar「+96% for Incarnon Form」）：计算器按当前形态套进化，不能当通用加成
_FORM_SCOPED = re.compile(
    r"\b(?:for|in|during|while in) (?:the )?Incarnon Form\b|Incarnon Form only", re.I
)
# 句中 / 句尾的条件（「… on undamaged enemies」「… on final magazine burst」「… for 4s」「… if enemy …」）
_COND_INLINE = re.compile(
    r"\bon (?:undamaged|final|kill|headshot|reload|critical|equip|status)\b"
    r"|\b(?:if|when|while|against|after)\b"
    r"|\bfor \d+(?:\.\d+)?\s*s(?:econds?)?\b|\bstacks? up\b|\bper (?:stack|status|kill)\b",
    re.I,
)


def family_key(name: str) -> str:
    return re.sub(r"[\s'’]+", "_", name.strip().lower())


def clean(text: str) -> str:
    """wiki 标记 → 纯文本（只处理灵化页会用到的写法）。"""
    t = re.sub(r"\[\[File:[^\]]*\]\]", "", text)
    t = re.sub(r"<br\s*/?>", " ", t)
    t = re.sub(r"<ref[^>]*>.*?</ref>|<ref[^>]*/>", "", t, flags=re.S)
    t = re.sub(r"<[^>]+>", "", t)
    for _ in range(3):  # 模板可能嵌套
        t = re.sub(r"\{\{[^{}|]+\|([^{}]*?)\}\}", lambda m: m.group(1).split("|")[-1], t)
        t = re.sub(r"\{\{([^{}|]+)\}\}", r"\1", t)
    t = re.sub(r"\[\[[^\]|]*\|([^\]]*)\]\]", r"\1", t)
    t = re.sub(r"\[\[([^\]]*)\]\]", r"\1", t)
    t = t.replace("'''", "").replace("''", "")
    return re.sub(r"\s+", " ", t).strip()


def split_cells(block: str) -> list[tuple[str, str]]:
    """表格的一行 → [(起始符 '!' 或 '|', 内容)]。内容 = 起始行最后一个属性分隔「|」之后 + 后续续行。"""
    cells: list[tuple[str, list[str]]] = []
    for line in block.split("\n"):
        if line.startswith(("!", "|")) and not line.startswith(("|-", "|}", "|+")):
            mark = line[0]
            body = line[1:]
            # 一行里用「||」或「!!」并排的多格
            parts = re.split(r"\|\||!!", body)
            for part in parts:
                # 属性分隔：「style=… | 内容」（属性里没有 [[ / {{ 才算）
                m = re.match(r"^([^|\[{]*=[^|\[{]*)\|(.*)$", part)
                content = m.group(2) if m else part
                attrs = m.group(1) if m else ""
                cells.append((mark + attrs, [content]))
        elif cells:
            cells[-1][1].append(line)
    return [(m, "\n".join(c)) for m, c in cells]


def colspan(attrs: str) -> int:
    m = re.search(r'colspan\s*=\s*"?(\d+)', attrs)
    return int(m.group(1)) if m else 1


def parse_values(text: str) -> dict:
    """「X = 24<br>Y = 30」→ {"X": "24", "Y": "30"}；「-」→ {}。"""
    # 原文偶有小写占位符（Obex「x = 50」）
    return {
        k.upper(): v.strip()
        for k, v in re.findall(
            r"\b([XYZWxyzw])\s*=\s*([^<\n|]+?)(?=<br|\n|$|\s+[XYZWxyzw]\s*=)", text
        )
    }


def parse_page(text: str) -> tuple[list[str], list[dict]]:
    """返回 (变体 wiki 名列表, [{tier, name, desc_lines(未替换), values: [每个变体的占位符表]}])。"""
    m = re.search(r"=+\s*Evolutions\s*=+", text)
    if not m:
        return [], []
    start = text.find("{|", m.end())
    end = text.find("\n|}", start)
    table = text[start:end]
    blocks = re.split(r"\n\|-[^\n]*", table)
    # 变体列只从表头行（「!」开头、不含 EVO）认：备注里也会出现 {{Weapon|…}}（Sicarus EVO1）
    head_blocks = [blocks[0]] + [x for x in blocks[1:2] if "EVO" not in x]
    header = "\n".join(ln for hb in head_blocks for ln in hb.split("\n") if ln.startswith("!"))
    variants = [w.split("|")[0].strip() for w in re.findall(r"\{\{Weapon\|([^}]+)\}\}", header)]
    if not variants:
        variants = [""]  # 单武器页（天生灵化）表头可能不写武器模板
    rows: list[dict] = []
    tier = ""
    for b in blocks[1:]:
        if b in head_blocks:
            continue  # 表头续行
        if "Evolution Challenge" in b or "Challenge" in b.split("\n", 1)[0]:
            continue
        cells = split_cells(b)
        if not cells:
            continue
        mt = re.search(r"EVO\s*(\d)", cells[0][1]) if cells[0][0].startswith("!") else None
        if mt:
            tier = mt.group(1)
            cells = cells[1:]
        if not tier or len(cells) < 2:
            continue
        name_cell, desc_cell, rest = cells[0][1], cells[1][1], cells[2:]
        name = clean(re.sub(r"\[\[File:[^\]]*\]\]", "", name_cell))
        if (
            not name or "challenge" in name.lower()
        ):  # 天生灵化武器页把挑战写成普通行（Felarx / Laetum）
            continue
        # 去掉一层「*」：子条目保留余下的「*」，parse_effects 据此继承上一级的条件
        lines = [
            ln.strip()[1:].strip() for ln in desc_cell.split("\n") if ln.strip().startswith("*")
        ]
        if not lines and clean(desc_cell):
            lines = [desc_cell.strip()]
        # 各变体数值格（跨列展开），最后一格是备注（多出来的忽略）
        vals: list[dict] = []
        for attrs, content in rest:
            if len(vals) >= len(variants):
                break
            v = parse_values(content)
            vals.extend([v] * colspan(attrs))
        vals = (vals + [{}] * len(variants))[: len(variants)]
        rows.append({"tier": tier, "name": name, "lines": lines, "values": vals})
    return variants, rows


def _match(sent: str, melee: bool) -> list[tuple[str, float]]:
    """一句里的可折算效果 [(kind, value)]（同一句同种同值只记一次：Sicarus「+X (Sicarus) / +X (Prime)」）。"""
    out: list[tuple[str, float]] = []
    for kind, pat, scale in EFFECT_PATTERNS:
        for mm in pat.finditer(sent):
            v = round(float(mm.group(1)) * scale, 6)
            if kind == "melee_range_bonus_m" and not melee:
                kind = "range_bonus_m"  # 枪的「+7 Range」= 光束长度（Atomos），不是近战范围
            if (kind, v) not in out:
                out.append((kind, v))
    return out


# 「Increase Base Status Chance to 12%」（Sicarus「Survivor's Edge」）：wiki 原文写 to，但 Sicarus Prime 基础触发
# 20%，「提高到 9%」无效 ⇒ 维护者 10-26 定先按 by（+12% / +9%）处理，与旧表一致
_STATUS_TO = re.compile(rf"Increase (?:Base )?Status Chance to {_NUM}%", re.I)
_DOUBLES_IN_INCARNON = re.compile(r"Effect doubles in Incarnon Form", re.I)


def parse_effects(lines: list[str], melee: bool = False) -> list[dict]:
    """描述条目 → 效果列表（规则见模块说明）。只对灵化形态生效的句子（「+96% for Incarnon Form」「Effect doubles
    in Incarnon Form」）作为同种效果的 ``incarnon_value`` 挂到普通形态那条上（wiki Boar Incarnon Genesis
    更新记录：普通形态按每颗弹丸、灵化形态另给一个值），计算器选了「灵化」时用它。"""
    effects: list[dict] = []
    form_vals: list[tuple[str, float]] = []  # 只对灵化形态的 (kind, value)
    parent_cond = False  # 上一级条目带条件 ⇒ 其子条目（「**」）同样是条件效果（Kunai「On equip from Primary:」）
    for raw in lines:
        sub = raw.startswith("*")  # lines 已去掉一层「*」，剩下的「*」= 子条目
        text = clean(raw.lstrip("*"))
        conditional = sub and parent_cond
        if not sub:
            parent_cond = False
        line_mod: list[
            dict
        ] = []  # 本条目里的可折算效果（「Effect doubles in Incarnon Form」作用于它们）
        for sent in [s.strip() for s in re.split(r"(?<=[.!])\s+", text) if s.strip()]:
            if _COND.match(sent):
                conditional = True
                if not sub:
                    parent_cond = True
            elif not conditional and _COND_INLINE.search(sent):
                effects.append({"kind": "conditional", "text": sent, "_modellable": False})
                continue  # 只这一句是条件，同条目后面的句子不受影响
            if conditional:
                effects.append({"kind": "conditional", "text": sent, "_modellable": False})
                continue
            if _DOUBLES_IN_INCARNON.search(sent) and line_mod:
                for e in line_mod:
                    e["incarnon_value"] = round(e["value"] * 2, 6)
                effects.append({"kind": "form_scoped", "text": sent, "_modellable": False})
                continue
            if _FORM_SCOPED.search(sent):
                form_vals += _match(sent, melee)
                effects.append({"kind": "form_scoped", "text": sent, "_modellable": False})
                continue
            mt = _STATUS_TO.search(sent)
            if mt:
                e = {
                    "kind": "flat_base_status_chance",
                    "value": round(float(mt.group(1)) / 100, 6),
                    "_modellable": True,
                }
                effects.append(e)
                line_mod.append(e)
                continue
            found = [(k, v) for k, v in _match(sent, melee) if v != 0]  # 「+0」（Boltor Prime）不记
            if not found and not _match(sent, melee):
                effects.append({"kind": "other", "text": sent, "_modellable": False})
            for k, v in found:
                e = {"kind": k, "value": v, "_modellable": True}
                effects.append(e)
                line_mod.append(e)
    for k, v in form_vals:
        base = next((e for e in effects if e.get("kind") == k and e.get("_modellable")), None)
        if base is not None:
            base["incarnon_value"] = v
        else:
            # 只有灵化形态的值（普通形态不加）：普通值记 0，计算器只在「灵化」时加
            effects.append({"kind": k, "value": 0.0, "incarnon_value": v, "_modellable": True})
    return effects


def substitute(lines: list[str], vals: dict) -> list[str]:
    """占位符换成该变体的数值。数值自带「%」「x」时去重（「+X%」+「30%」→「+30%」；「+Yx」+「0.6x」→「+0.6x」）。"""
    out = []
    for ln in lines:
        for k, v in vals.items():
            v = v.replace("+", "").strip()
            ln = re.sub(rf"(?<![A-Za-z]){k}(?=x\b|[^A-Za-z]|$)", lambda _m: v, ln)
        ln = ln.replace("%%", "%").replace("xx", "x")
        out.append(ln)
    return out


def build(wiki_dir: Path, ws: dict) -> tuple[dict, list[str]]:
    W = load_weapons(wiki_dir)
    ws_by_key = {k.split("#")[0]: v for k, v in ws.items()}
    report: list[str] = []
    out: dict = {}
    for page in sorted((wiki_dir / "evo").glob("*.wiki")):
        text = page.read_text(encoding="utf-8")
        variants, rows = parse_page(text)
        if not rows:
            report.append(f"{page.stem}：没有解析出进化表")
            continue
        if variants == [""]:
            variants = [page.stem.replace("_Incarnon_Genesis", "").replace("_", " ")]
        for vi, vname in enumerate(variants):
            e = W.get(vname) or {}
            wsrec = ws_by_key.get(str(e.get("InternalName") or "").lower()) or {}
            key = family_key(str(wsrec.get("name") or vname))
            tiers: dict[str, list] = {}
            for r in rows:
                lines = substitute(r["lines"], r["values"][vi] if vi < len(r["values"]) else {})
                left = [
                    ln
                    for ln in lines
                    if re.search(r"(?<![A-Za-z])[XYZ](?=x\b|[^A-Za-z]|$)", clean(ln))
                ]
                if left:
                    report.append(
                        f"{vname} EVO{r['tier']}「{r['name']}」占位符没替换：{clean(left[0])[:60]}"
                    )
                tiers.setdefault(r["tier"], []).append(
                    {
                        "id": f"{key}_{slug(r['name'])}",
                        "name": r["name"],
                        "description": " ".join(clean(ln.lstrip("*")) for ln in lines),
                        "effects": parse_effects(lines, melee=e.get("Slot") == "Melee"),
                    }
                )
            for t, opts in tiers.items():
                for o in opts:
                    o["selection"] = "fixed" if len(opts) == 1 else "choose_one"
            if key in out:
                report.append(f"{vname}：键 {key} 重复（{page.stem}），后者覆盖")
            out[key] = {
                "wiki_page": page.stem.replace("_", " "),
                "tiers": dict(sorted(tiers.items())),
            }
    return out, report


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wiki", required=True, type=Path)
    ap.add_argument("--out", type=Path, default=OUT)
    ap.add_argument("--report", type=Path)
    args = ap.parse_args(argv[1:])
    recs, report = build(args.wiki, json.loads(WS.read_text(encoding="utf-8")))
    out = {
        "_meta": {
            "source": "英文 wiki 各武器灵化页（<武器> Incarnon Genesis / 天生灵化武器页）的 Evolutions 表，CC BY-NC-SA 3.0",
            "wiki_snapshot": args.wiki.name,
            "generator": "scripts/build_evolutions.py",
            "note": "selection：choose_one = 该层多选一 / fixed = 固定。effects：_modellable = 计算器会折算的无条件面板效果；"
            "conditional = 带条件前缀的效果；other = 其余原文存档",
        }
    }
    out.update(dict(sorted(recs.items())))
    args.out.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    if args.report:
        args.report.write_text("\n".join(report) + "\n", encoding="utf-8")
    n_opt = sum(len(o) for v in recs.values() for o in v["tiers"].values())
    print(f"✓ {args.out.name}：{len(recs)} 把武器 / {n_opt} 个进化选项；提示 {len(report)} 条")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
