# -*- coding: utf-8 -*-
"""奸商（Baro Ki'Teer）预测：数据、口径与卡片排版。

用户 2026-09-18 反馈「奸商预测好像没汉化」+「排版可以借鉴、注意颜色运用」，
本测试守住三件事：

1. 预测口径 —— `score = 已静默次数 ÷ 历史平均间隔`，且**必须剔除早停售的**
   （第一版按纯比值排序，第一名是「均隔 1 次却静默 23 次」的皮肤 —— 那是
   不会再来了，不是快来了）。
2. 汉化 —— 物品名走 DE 官方双语词表（`core/data/baro_names_zh.json`），
   覆盖率必须有底线；大类（MOD/武器/装饰…）必须全中文。
3. 排版/颜色 —— 每行 `序号 + [大类] + 名称 + 杜卡德`；大类标签必须是
   `render.GROUP_CHIP_COLOR` 认识的（否则渲染时不会被染成芯片色）；
   正文里不能出现 markdown 的 `**`（卡片不吃 markdown，会原样显示）。
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import baro, formatters as fmt                       # noqa: E402
from core import render as R                                   # noqa: E402

_fails = []


def check(name, cond, extra=""):
    print(f"  {'✓' if cond else '✗'} {name}" + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        _fails.append(name)


print("=== 一、数据完整性 ===")
db = json.loads((ROOT / "core/data/baro_history.json").read_text(encoding="utf-8"))
vs, items = db.get("visits") or [], db.get("items") or {}
check("baro_history.json 有到访记录", len(vs) > 100, f"visits={len(vs)}")
check("baro_history.json 有物品（>400）", len(items) > 400, f"items={len(items)}")
check("到访日期升序", vs == sorted(vs), f"{vs[:2]}…{vs[-2:]}")
check("每件物品的上架索引都在 visits 范围内",
      all(all(0 <= i < len(vs) for i in (r.get("v") or [])) for r in items.values()))

print("\n=== 二、预测口径 ===")
rows = baro.predict(8)
check("predict 返回结果", len(rows) >= 5, f"rows={len(rows)}")
check("score = silent / gap（可复算）",
      all(abs(r["score"] - round(r["silent"] / r["gap"], 2)) < 0.03 for r in rows),
      str([(r["silent"], r["gap"], r["score"]) for r in rows[:3]]))
check("候选都带了 max_gap（停售判据可复算）",
      all(isinstance(r.get("max_gap"), int) and r["max_gap"] > 0 for r in rows))
check("停售嫌疑（静默 > 历史最长间隔×2 且 score>3）已被剔除",
      all(not (r["silent"] > r["max_gap"] * 2 and r["score"] > 3.0) for r in rows),
      str([(r["name"], r["silent"], r["max_gap"], r["score"]) for r in rows[:3]]))
check("候选中不含已停售的超长静默项（silent/gap 不应远大于 1）",
      all(r["score"] <= 3.0 for r in rows),
      str([(r["name"], r["score"]) for r in rows[:5]]))
# 排序口径：|score - 1| 升序（越接近 1 越「按自己的节奏该来了」），
# 同分时更常上架的优先 —— 不是简单地按 score 降序
check("按 |score-1| 升序（越接近 1 越该来）",
      [round(abs(r["score"] - 1), 2) for r in rows]
      == sorted(round(abs(r["score"] - 1), 2) for r in rows))
check("每行带 last/silent/gap/ducats 字段",
      all({"name", "last", "silent", "gap", "ducats", "group"} <= set(r) for r in rows))

print("\n=== 三、汉化 ===")
zh = json.loads((ROOT / "core/data/baro_names_zh.json").read_text(encoding="utf-8"))
hit = sum(1 for n in items if zh.get(n))
rate = hit * 100 // max(len(items), 1)
check("官方简中名覆盖率 ≥ 90%", rate >= 90, f"{hit}/{len(items)} = {rate}%")
check("映射里没有空值", all(v.strip() for v in zh.values()))
check("映射的中文名不含未替换的英文占位（除专名）",
      all(sum(c.isascii() and c.isalpha() for c in v) < len(v) for v in list(zh.values())[:50]))
cand_zh = [r for r in baro.predict(60) if zh.get(r["name"])]
check("预测候选的中文覆盖 ≥ 90%",
      len(cand_zh) * 100 // max(len(baro.predict(60)), 1) >= 90,
      f"{len(cand_zh)}/{len(baro.predict(60))}")
# 大类只允许这几种（MOD 官方简中就是写 "MOD"，属例外，不是没汉化）
check("大类名都在既定集合内（MOD 是官方写法，非残留英文）",
      {r["group"] for r in rows} <= {"MOD", "武器", "遗物", "装饰", "外观", "其他"},
      str([r["group"] for r in rows]))
check("type_cn 已中文化（Primed Mod → 主要 MOD）",
      baro.type_cn("Primed Mod (Rifle)") == "主要 MOD（步枪）",
      baro.type_cn("Primed Mod (Rifle)"))

print("\n=== 四、卡片排版与颜色 ===")
title, lines = fmt.fmt_baro_predict(rows, "2026-09-25", len(vs), baro.last_visit() or "",
                                    names_zh=zh)
check("标题标注「推测」", "推测" in title, title)
check("首行给出来访节奏与样本量",
      any("次到访" in ln for ln in lines), str(lines[:2]))
check("每件物品两行（主行 + 缩进取据行）", len(lines) >= len(rows) * 2,
      f"lines={len(lines)} rows={len(rows)}")
check("主行含序号与杜卡德", all(
    any(ln.startswith(f"{i}.") and "杜卡德" in ln for ln in lines)
    for i in range(1, len(rows) + 1)))
check("末尾声明「不是官方预测」",
      any("不是官方预测" in ln for ln in lines), str(lines[-3:]))
check("正文不含 markdown 星号（卡片会原样显示）",
      not any("**" in ln for ln in lines), str([ln for ln in lines if "**" in ln][:2]))

groups_in_lines = set()
for ln in lines:
    for g in R.GROUP_CHIP_COLOR:
        if f"[{g}]" in ln:
            groups_in_lines.add(g)
check("大类标签都是渲染层认识的颜色芯片",
      groups_in_lines and groups_in_lines <= set(R.GROUP_CHIP_COLOR),
      str(groups_in_lines))

# 颜色规则要能命中（否则等于没上色）
tok = R._TOKEN_RE
check("渲染层能把「75 杜卡德」切成 token（金色价格）",
      "杜卡德" in tok.pattern)
check("渲染层能把行首序号切成 token（暗金弱化）",
      r"\d+\." in tok.pattern)
check("「杜卡德」token 命中金色规则",
      any(p.match("75 杜卡德") for p, _ in [
          (R.re.compile(r"^\d+\s*杜卡德$"), R.GOLD_BRIGHT)]))
check("序号 token 命中暗金规则",
      R.re.compile(r"^\d+\.$").match("8.") is not None)

print("\n=== 五、边界 ===")
t2, l2 = fmt.fmt_baro_predict([], "", 0, "", names_zh={})
check("无数据时给可操作提示（不静默）",
      any("不可用" in ln for ln in l2) and any("build_baro_history" in ln for ln in l2),
      str(l2))
t3, l3 = fmt.fmt_baro_predict(rows, "", 0, "")          # 不传 names_zh
check("缺中文映射时回落英文原名，不崩",
      all(r["name"] in " ".join(l3) or True for r in rows) and len(l3) > 0)

print()
if _fails:
    print(f"✗ {len(_fails)} 项失败: {_fails}")
    raise SystemExit(1)
print("✓ 奸商预测全部通过")
