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

# ⑦ ★★ 姿态卡（候选集为空）必须能与普通卡**同排共存**
#    用户实测（暮斩）：姿态卡「狂风压境」的候选集为空（base_drain 是哨兵 -2），
#    旧实现要求它只能落在「该行全 0」的格子 → **整张图无解** → 豆子全部作废，
#    于是「北风 1 级」被判成 3 级、「长时苦难 0 级」被判成 4 级（用户报障）。
#    现在：候选集为空的卡当**通配**（豆数对不上不代表错位），照常对齐。
from core.pips import align_rows, pick_rank  # noqa: E402

CANDS = [[],            # 姿态卡（无候选）
         [3, 5],        # 压迫点 容量 9
         [7],           # 牺牲斩铁 容量 13
         [3],           # 剑风 容量 7
         [1, 3],        # 北风 容量 9
         [0, 2, 9, 10], # 热病打击 Prime 容量 8
         [0, 5],        # 一击必杀 容量 6
         [0, 3, 4],     # 长时苦难 容量 4
         [0, 1, 5]]     # 肢解 容量 5
ROWS = [[0, 3, 0, 0], [5, 7, 3, 1], [10, 5, 0, 5]]
_map = align_rows(CANDS, ROWS)
check("★ 姿态卡（候选集为空）不再让整图对齐失败", _map is not None, str(_map))
if _map:
    check("  对齐结果与真实布局一致（姿态独占第 1 排，其余 4+4）",
          _map == [(0, 0), (1, 0), (1, 1), (1, 2), (1, 3),
                   (2, 0), (2, 1), (2, 2), (2, 3)], str(_map))
    got = [ROWS[r][c] for r, c in _map]
    check("  由此得到的豆数序列（姿态格取到 0，其等级由行内唯一豆数定）",
          got == [0, 5, 7, 3, 1, 10, 5, 0, 5], str(got))
    _sr, _ss = pick_rank(ROWS[0], _map[0][1] + 1, CANDS[0])
    check("  姿态卡由『行内唯一豆数』定到 3 级（与真值一致）",
          _sr == 3, f"{_sr} / {_ss}")

# ⑧ 满级线（第三信号）：豆子被那条线「吃掉」一颗时，用线判定满级
check("★ 满级线：豆数 4 不在候选 [3,5] 里，但该格有贯穿线 → 采信满级 5",
      pick_rank([4, 0, 0, 0], 1, [3, 5], maxed=[True, False, False, False],
                max_rank=5) == (5, "满级线"),
      str(pick_rank([4, 0, 0, 0], 1, [3, 5], maxed=[True, False, False, False],
                    max_rank=5)))
check("  没有满级线时不采信（不硬猜）",
      pick_rank([4, 0, 0, 0], 1, [3, 5], maxed=[False] * 4, max_rank=5)[0] is None,
      str(pick_rank([4, 0, 0, 0], 1, [3, 5], maxed=[False] * 4, max_rank=5)))
check("  满级但库中 max_rank 不在候选里 → 不覆盖（剑风那种库错的情形）",
      pick_rank([4, 0, 0, 0], 1, [3], maxed=[True, False, False, False],
                max_rank=5)[0] is None,
      str(pick_rank([4, 0, 0, 0], 1, [3], maxed=[True, False, False, False],
                    max_rank=5)))

# ⑨ ★★ 对齐从「全有或全无」改成**最优拟合**（2026-09-20 4K 事故的回归）
#    事故：4K 下「结霜侵蚀」（0 级）那一格被暖色兜底误数成 **2 颗**，
#    旧实现要求每张卡都满足约束 → **整图无解** → 7 张卡的豆子信号全部作废
#    （用户看到「北风/长时苦难等级又对不上」）。
#    现在：容忍个别格噪声，只有那一张卡退回容量反推 + 标 `?`。
CANDS7 = [[0, 1, 5],    # 匍匐靶心（Creeping Bullseye）容量 5
          [0, 5],       # 病原弹头（Pathogen Rounds）容量 6
          [3, 5],       # 弹头扩散（Barrel Diffusion）容量 11
          [7, 10],      # 黄蜂螫刺（Hornet Strike）容量 14
          [0, 3],       # 结霜侵蚀（Frostbite）容量 4
          [3, 5],       # 致命洪流（Lethal Torrent）容量 11
          [0, 1, 5]]    # 神枪手（Gunslinger）容量 5
ROWS7 = [[5, 0, 5, 0], [10, 0, 5, 1]]
TRUE_MAP = [(0, 0), (0, 1), (0, 2), (1, 0), (1, 1), (1, 2), (1, 3)]

_m7 = align_rows(CANDS7, ROWS7)
check("★ 7 张卡（真机 4K 那张）对齐正确", _m7 == TRUE_MAP, str(_m7))

ROWS7_BAD = [list(ROWS7[0]), list(ROWS7[1])]
ROWS7_BAD[1][1] = 2                    # 模拟暖色兜底误数出的 2 颗
_m7b = align_rows(CANDS7, ROWS7_BAD)
check("★★ 单格噪声（豆数 2 不在候选 [0,3]）不再让整图作废", _m7b == TRUE_MAP, str(_m7b))
if _m7b:
    _n_bad = ROWS7_BAD[_m7b[4][0]][_m7b[4][1]]
    check("  该卡（结霜侵蚀）的噪声豆会被拒收（不照抄成等级）",
          _n_bad not in CANDS7[4], f"豆数 {_n_bad} 候选 {CANDS7[4]}")

ROWS7_BAD2 = [list(ROWS7[0]), list(ROWS7[1])]
ROWS7_BAD2[1][1] = 2
ROWS7_BAD2[0][0] = 4                   # 第 2 处噪声
check("  噪声过多时仍判为「对齐无解」（不放太宽）",
      align_rows(CANDS7, ROWS7_BAD2) is None,
      str(align_rows(CANDS7, ROWS7_BAD2)))

# ⑩ ★ 漏读交叉校验：像素网格给出「至少有多少张卡」的硬下界
from core.pips import expected_min_cards, underread_penalty  # noqa: E402

_rows = [{"is_inventory": False, "counts": [5, 0, 5, 0]},
         {"is_inventory": False, "counts": [10, 0, 5, 1]},
         {"is_inventory": True, "counts": [5, 5, 5, 5]}]      # 仓库区不计入
check("像素网格给出的卡数下界 = 有豆的格数（仓库不算）",
      expected_min_cards(_rows) == 5, str(expected_min_cards(_rows)))
check("★ 模型只读到 3 张（真机 glm 只读了上排）→ 判为漏读并重罚",
      underread_penalty(_rows, 3) == 6, str(underread_penalty(_rows, 3)))
check("  读够 5 张及以上不罚", underread_penalty(_rows, 5) == 0
      and underread_penalty(_rows, 7) == 0)
check("  没有像素信号时不罚（不误伤）",
      underread_penalty([], 3) == 0 and underread_penalty(_rows, 5) == 0)

print()
if FAILED:
    print(f"[FAIL] {len(FAILED)} 项失败: {FAILED}")
    sys.exit(1)
print("[OK] 豆子融合逻辑测试全部通过")
