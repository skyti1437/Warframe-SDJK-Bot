# -*- coding: utf-8 -*-
"""修复终幕元素加成：
1) parse_wiki_valence 正则支持 'Dual Coda Torxica'（多词前缀）；
2) 把 wiki 实测的 Coda A 批元素/加成写进 core/data/rotations.json。
用法: python fix_coda_valence.py [--apply]
"""
import io, json, re, shutil, sys, time

APPLY = "--apply" in sys.argv
ROOT = "."
API = f"{ROOT}/core/api_client.py"
ROT = f"{ROOT}/core/data/rotations.json"

# wiki Reset 页实测值（2026-09-21 13:50 GMT+8 抓取，Eleanor 当前售 Batch A）
CODA_A = {
    "Coda Hema": ("Heat", 27.4),
    "Coda Sporothrix": ("Heat", 43.8),
    "Coda Catabolyst": ("Heat", 25.0),
    "Coda Pox": ("Radiation", 30.9),
    "Dual Coda Torxica": ("Heat", 25.1),
    "Coda Mire": ("Magnetic", 26.2),
    "Coda Motovore": ("Heat", 38.8),
}
SNAP = "2026-09-21T05:50:00+00:00"
SRC = ("wiki「Reset」页玩家上报值（FlareSolverr 抓取 2026-09-21 13:50 GMT+8，"
       "Eleanor 售 Batch A）；无官方 API，以游戏内商店为准")

# ---------- 1) 正则 ----------
src = io.open(API, encoding="utf-8").read()
OLD = r'r"(Tenet \w+|Coda \w+)[\s|]+"'
NEW = r'r"((?:Dual )?Coda \w+|Tenet \w+)[\s|]+"'
n = src.count(OLD)
print("正则旧写法出现:", n, "次")
patch_ok = False
if n == 1 and NEW not in src:
    if APPLY:
        shutil.copy2(API, API + ".bak_coda_" + time.strftime("%Y%m%d_%H%M%S"))
        io.open(API, "w", encoding="utf-8").write(src.replace(OLD, NEW))
        patch_ok = True
    print("  正则将改为:", NEW)
elif NEW in src:
    print("  正则已是新写法")
else:
    print("  !! 未唯一匹配，跳过正则修改")

# ---------- 2) 数据 ----------
d = json.load(io.open(ROT, encoding="utf-8"))
coda = d["coda"]
batches = coda["batches"]
label = coda.get("batch_label") or ["A", "B"]
idx_a = label.index("A") if "A" in label else 0
changed = []
for it in batches[idx_a]:
    en = it.get("en")
    if en in CODA_A:
        elem, bonus = CODA_A[en]
        if (it.get("element"), it.get("bonus")) != (elem, bonus):
            changed.append(f"  {it.get('cn') or en}: {it.get('element')} {it.get('bonus')} → {elem} {bonus}")
            it["element"], it["bonus"] = elem, bonus
    else:
        changed.append(f"  [缺口] A 批 {en} 不在实测表里")
print("A 批待更新:", len(changed), "项")
for c in changed:
    print(c)
coda["valence_snapshot"] = SNAP
coda["valence_source"] = SRC

if APPLY:
    shutil.copy2(ROT, ROT + ".bak_coda_" + time.strftime("%Y%m%d_%H%M%S"))
    io.open(ROT, "w", encoding="utf-8").write(
        json.dumps(d, ensure_ascii=False, indent=1) + "\n")
    print("已写入 rotations.json（正则补丁:%s）" % patch_ok)
else:
    print("(dry-run，未写入)")
