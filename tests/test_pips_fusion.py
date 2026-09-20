# -*- coding: utf-8 -*-
"""豆子（第二信号）与容量反推的**融合逻辑**测试。

要解决的原始 bug（用户实测报障）：
  「私法补给」0 级、卡片右上角红色 5 → 代码按容量反推「取最高」判成**满级 5**。
  原因：容量 5 对这张卡（base=4 / max_rank=5）有**三重解** ——
  1级·白（4+1=5）/ 5级·绿（ceil(9/2)=5）/ 0级·红（round(5)=5），
  单看数字无法区分。豆子（亮豆数 = 等级）能唯一定出答案。

本测试覆盖：
  ① 有豆子信号时，等级以豆子为准（并在与容量解冲突时记 conflict）
  ② 0 级卡（豆数 0）不再被判成满级
  ③ 没有豆子信号时，行为与原来完全一致（不回归）
  ④ 豆数与容量候选集对不上时不硬猜
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import loadout_ocr as lo  # noqa: E402

FAILED = []


def check(name, cond, info=""):
    mark = "PASS" if cond else "FAIL"
    if not cond:
        FAILED.append(name)
    print(f"[{mark}] {name}" + (f"  ← {info}" if info and not cond else ""))


def pips_row(counts):
    """造一行豆子检测结果（装备区）。"""
    return [{"row": 1, "band": (300, 310), "counts": counts, "pos": [[]] * 4,
             "is_inventory": False, "pitch_col": 245}]


def ocr_of(mods):
    return {"weapon": "测试武器", "mods": mods, "panel": {}}


def rank_of(an, name):
    for m in an.get("mods") or []:
        if name in (m.get("raw") or "") or name in (m.get("zh") or ""):
            return m.get("rank")
    return "<该卡没出现在结果里>"


# ---- 前置：库里要有这几张卡，否则测试没意义 ----
PROBE = {}
for _n in ("压迫点", "私法补给"):
    _rec, _how = lo.match_mod(_n)
    PROBE[_n] = _rec
    print(f"  [info] 库中「{_n}」→ {'命中 base=%s max=%s' % (_rec.get('base_drain'), _rec.get('max_rank')) if _rec else '✗ 没找到（相关用例将跳过）'}")

print()
# ① 多重解：压迫点 base=4/max=5，容量 9 → 【3 级·红】或【5 级·白】
if PROBE.get("压迫点"):
    ocr = ocr_of([{"name": "压迫点", "drain": 9, "color": "白", "row": 1, "col": 1}])
    an5 = lo.analyze(ocr, pips_row([5, 0, 0, 0]))
    check("容量 9 + 豆数 5 → 5 级（5 级·白解）", rank_of(an5, "压迫点") == 5,
          str(rank_of(an5, "压迫点")))
    an3 = lo.analyze(ocr, pips_row([3, 0, 0, 0]))
    check("★ 容量 9 + 豆数 3 → 3 级（3 级·红解，旧的『取最高』会判成 5）",
          rank_of(an3, "压迫点") == 3, str(rank_of(an3, "压迫点")))

# ② 用户实测的 0 级卡：私法补给 base=4/max=5，容量 5（红）→ 三解
if PROBE.get("私法补给"):
    ocr = ocr_of([{"name": "私法补给", "drain": 5, "color": "红", "row": 1, "col": 1}])
    an0 = lo.analyze(ocr, pips_row([0, 0, 0, 0]))
    check("★ 私法补给 容量 5 + 豆数 0 → 0 级（修复『被判成满级』的报障）",
          rank_of(an0, "私法补给") == 0, str(rank_of(an0, "私法补给")))
    check("  并记录为「与容量反推冲突」", (an0.get("pips") or {}).get("conflict") == 1,
          str(an0.get("pips")))
    an1 = lo.analyze(ocr, pips_row([1, 0, 0, 0]))
    check("  容量 5 + 豆数 1 → 1 级（1 级·白解）",
          rank_of(an1, "私法补给") == 1, str(rank_of(an1, "私法补给")))

# ③ 没有豆子信号 → 与原来一致（不回归）
if PROBE.get("压迫点"):
    ocr = ocr_of([{"name": "压迫点", "drain": 9, "color": "白", "row": 1, "col": 1}])
    an_no = lo.analyze(ocr)
    an_none = lo.analyze(ocr, [])
    base_rank = lo.infer_rank(PROBE["压迫点"], 9, "白")[0]
    check("无豆子信号时 rank == infer_rank（行为不变）",
          rank_of(an_no, "压迫点") == base_rank
          and rank_of(an_none, "压迫点") == base_rank,
          f"{rank_of(an_no, '压迫点')} vs {base_rank}")
    check("无豆子信号时统计为 0", (an_no.get("pips") or {}).get("used") == 0)

# ④ 豆数与容量候选集对不上 → 不硬猜（保持原等级）
if PROBE.get("压迫点"):
    ocr = ocr_of([{"name": "压迫点", "drain": 9, "color": "白", "row": 1, "col": 1}])
    an_bad = lo.analyze(ocr, pips_row([2, 0, 0, 0]))     # 2 级不在候选 [3, 5] 里
    check("★ 豆数不在容量候选集里时不采信（不硬猜）",
          rank_of(an_bad, "压迫点") == lo.infer_rank(PROBE["压迫点"], 9, "白")[0],
          str(rank_of(an_bad, "压迫点")))

# ⑤ 对齐无解时安全回退
#    位置现在由「卡顺序 + 候选集约束」整体对齐得出（不依赖模型报的 row/col），
#    当某张卡的豆数在整行都找不到候选值时，对齐失败 → 不采用豆子。
if PROBE.get("压迫点"):
    ocr = ocr_of([{"name": "压迫点", "drain": 9, "color": "白", "row": 1, "col": 1}])
    an_x = lo.analyze(ocr, pips_row([7, 0, 0, 0]))   # 7 不在候选 [3,5] 里
    check("★ 豆数与容量完全对不上（对齐无解）→ 不采用豆子，保持原等级",
          (an_x.get("pips") or {}).get("used") == 0
          and rank_of(an_x, "压迫点") == lo.infer_rank(PROBE["压迫点"], 9, "白")[0],
          f"used={(an_x.get('pips') or {}).get('used')} rank={rank_of(an_x, '压迫点')}")

# ⑥ 模型没报 row/col 也照样能用豆子（对齐不依赖它）—— 端到端实测模型报的位置不可靠
if PROBE.get("压迫点"):
    ocr = ocr_of([{"name": "压迫点", "drain": 9, "color": "白"}])
    an_nopos = lo.analyze(ocr, pips_row([3, 0, 0, 0]))
    check("模型没报 row/col 时仍能用豆子定级（不依赖模型位置）",
          rank_of(an_nopos, "压迫点") == 3, str(rank_of(an_nopos, "压迫点")))

print()
if FAILED:
    print(f"[FAIL] {len(FAILED)} 项失败: {FAILED}")
    sys.exit(1)
print("[OK] 豆子融合逻辑测试全部通过")
