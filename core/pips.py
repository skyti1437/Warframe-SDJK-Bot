# -*- coding: utf-8 -*-
"""MOD 卡片「豆子」像素检测 —— 识卡等级的**第二信号**。

为什么需要它
------------
等级原来只靠卡片右上角的容量数字反推：

    drain = base_drain + rank              （极性不匹配）
    drain = ceil((base_drain + rank) / 2)  （极性匹配，消耗减半）
    drain = round((base_drain + rank) * 1.25)  （极性不合）

**这个方程经常有多解**。实测案例：私法补给（base=4 / max_rank=5）读到容量 5 时，
1 级·白 / 5 级·绿 / 0 级·红 三种都成立 —— 单看数字无法区分，
而代码原策略「取最高」会误判成满级（用户实测遇到的 bug）。
**卡片底部的「豆子」（菱形等级刻度）能唯一定出答案：亮豆数 = 当前等级。**

为什么不让模型数
----------------
实测 glm-4v-flash / Qwen3-VL-30B-A3B / Qwen3-VL-8B 三个视觉模型数豆准确率
12% / 19% / 13% —— 它们倾向把右上角的容量数字**原样抄成**豆数。故改由程序数像素。

判定依据（2026-09-20 用 7 张真实截图实测确立）
--------------------------------------------
- 卡片底部有一条**与卡片等宽的亮蓝装饰细线**（厚 1~2px）
- 细线上串着**亮蓝色菱形豆**（厚 6~8px、间距 12px、亮豆 RGB ≈ (143,191,229)）
- 豆子在卡片内**水平居中**；**亮豆数 = 该卡等级**；0 级卡豆子全暗
- 装备区列中心 = `712 + 245×i`（4 列，与卡片宽 152 成比例）
- ★ 上下两排**共用同一套列位置**，合并各排的细线段才能定出完整网格
- ★ 仓库区（下半部分、一行 7 格）会被识别并跳过

能力边界：截图宽 ≥1280 可用（±1 颗）；≤854（豆子只剩 5px）会失准 →
上层应退回「容量反推 + 标 `?`」。部分截图可用（裁剪不改变内容尺寸）。
"""
from __future__ import annotations

from typing import Optional

from PIL import Image, ImageChops

# ---- 判据（相对 pitch，不写死像素；括号内为 1920×1080 实测值）----
MASK_B_MIN = 140          # 蓝通道下限
MASK_BR_GAP = 30          # 蓝 - 红 的差值下限
MASK_G_MIN = 85           # 绿通道下限
THICK_MIN = 5             # 列亮像素数下限（细线 1~2px 会被滤掉，豆 6~8px 通过）
ROW_FRAC = 0.12           # 行带判定阈值 = 全图行密度峰值 × 该比例
ROW_MIN_H = 2             # 行带最小高度（★ 不能是 3：装备区行带常只 2px 高）
BEAN_PITCH_RATIO = 0.079  # 豆间距 / 卡片宽（实测 12/152）
CARD_W_OF_W = 0.0792      # 卡片宽 / **图宽**（实测 152/1920；低分辨率同比例缩放）
COL_PITCH_RATIO = 1.61    # 列间距 / 卡片宽（实测 245/152）
CARD_COLS = 4             # 游戏 UI：装备区一行最多 4 格
INV_BF = 0.62             # 仓库区 y 位置下限（相对图高）
INV_MIN_COLS = 5          # 仓库区一行格数下限
MIN_WIDTH_FOR_PIPS = 1000  # 低于此宽度不检测（豆子太小，直接放弃）
# ★ 满级卡有一条**横贯全卡**的亮线（用户 2026-09-20 实测指出）；非满级没有。
#   实测（1920×1080，8 张卡）：满级卡的「有墨列占比」0.78~0.91、细线列占比 0.27~0.77；
#   非满级 0.43 / 0.05 / 0（牺牲斩铁 7/10、北风 1/5、长时苦难 0/5）。
INKED_MIN = 0.70          # 卡宽内「有任何亮像素」的列占比下限
LINE_MIN = 0.20           # 其中「1~2px 细线」的列占比下限


def _median(xs):
    if not xs:
        return 0
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def lit_mask(im: Image.Image) -> Image.Image:
    """亮蓝像素掩码（PIL 通道运算，比 Python 逐像素快两个数量级）。"""
    r, g, b = im.split()
    m_br = ImageChops.subtract(b, r).point(lambda v: 255 if v > MASK_BR_GAP else 0)
    m_b = b.point(lambda v: 255 if v > MASK_B_MIN else 0)
    m_g = g.point(lambda v: 255 if v > MASK_G_MIN else 0)
    return ImageChops.multiply(ImageChops.multiply(m_br, m_b), m_g)


def _find_bands(rowcount, min_h=ROW_MIN_H, frac=ROW_FRAC):
    peaks = [c for c in rowcount if c > 0]
    if not peaks:
        return []
    thresh = max(4, max(peaks) * frac)
    bands, cur = [], []
    for y, c in enumerate(rowcount):
        if c >= thresh:
            cur.append(y)
        else:
            if len(cur) >= min_h:
                bands.append((cur[0], cur[-1]))
            cur = []
    if len(cur) >= min_h:
        bands.append((cur[0], cur[-1]))
    merged = []
    for a, b in bands:
        if merged and a - merged[-1][1] <= 4:
            merged[-1] = (merged[-1][0], b)
        else:
            merged.append((a, b))
    return merged


def _col_thickness(mask, y0, y1):
    W = mask.width
    bb = mask.crop((0, y0, W, y1)).tobytes()
    return [bb[x::W].count(255) for x in range(W)]


def _contig(xs, gap, min_w):
    xs = sorted(xs)
    if not xs:
        return []
    out, cur = [], [xs[0], xs[0]]
    for x in xs[1:]:
        if x - cur[1] <= gap:
            cur[1] = x
        else:
            if cur[1] - cur[0] + 1 >= min_w:
                out.append(tuple(cur))
            cur = [x, x]
    if cur[1] - cur[0] + 1 >= min_w:
        out.append(tuple(cur))
    return out


def _count_in_cell(thick, cx, card_w, W):
    """在 [cx-card_w/2, cx+card_w/2] 这个格子内数亮豆（含暗豆回填）。"""
    lo = max(0, int(round(cx - card_w / 2)))
    hi = min(W, int(round(cx + card_w / 2)) + 1)
    if hi - lo < 4:
        return 0, []
    sig = thick[lo:hi]
    nz = sorted(v for v in sig if v > 0)
    if not nz:
        return 0, []
    base = nz[len(nz) // 5]
    floor_hi = max(4, base + 3)              # 可信豆（严格）
    xs = [lo + i for i, v in enumerate(sig) if v >= floor_hi]
    if not xs:
        return 0, []
    seeds = [sum(g) // len(g) for g in _contig(xs, 2, 1)]
    if len(seeds) >= 2:
        gaps = [seeds[i + 1] - seeds[i] for i in range(len(seeds) - 1)]
        pitch = _median([g for g in gaps if 4 <= g <= card_w * 0.2] or gaps)
    else:
        pitch = max(6, card_w * BEAN_PITCH_RATIO)
    if pitch < 4:
        pitch = max(6, card_w * BEAN_PITCH_RATIO)
    floor_lo = max(3, base + 1)              # 回填（宽松）
    win = max(2, int(pitch * 0.33))
    filled = [seeds[0]]
    for direction in (1, -1):
        i, miss = 1, 0
        while miss < 2:
            pos = seeds[0] + direction * i * pitch
            if pos > hi - 2 or pos < lo + 2:
                break
            a, b = max(lo, int(pos - win)), min(hi, int(pos + win) + 1)
            if b > a and max(thick[a:b]) >= floor_lo:
                filled.append(pos)
                miss = 0
            else:
                miss += 1
            i += 1
    return len(filled), sorted(filled)


def _cell_ink(thick, cx, card_w, W):
    """格子的「墨量」统计 → 判断是否满级（满级卡有一条横贯全卡的亮线）。

    返回 (inked_frac, line_frac, maxed)：
      · inked_frac = 有任何亮像素的列 / 格子宽
      · line_frac  = 其中「细线」（厚 1~2px）的列 / 格子宽
      · maxed      = inked_frac ≥ INKED_MIN 且 line_frac ≥ LINE_MIN
    """
    lo = max(0, int(round(cx - card_w / 2)))
    hi = min(W, int(round(cx + card_w / 2)) + 1)
    if hi - lo < 4:
        return 0.0, 0.0, False
    sig = thick[lo:hi]
    inked = sum(1 for v in sig if v > 0)
    line = sum(1 for v in sig if 1 <= v <= 2)
    i_f, l_f = inked / len(sig), line / len(sig)
    return i_f, l_f, (i_f >= INKED_MIN and l_f >= LINE_MIN)


def detect_pips(img: Image.Image) -> list[dict]:
    """检测装备区每一行的豆数。

    返回（按 y 从小到大，即游戏里从上到下）：
        [{"row": 1, "counts": [c1, c2, c3, c4], "is_inventory": False}, ...]
    `counts[i]` 是该行第 i+1 列的亮豆数（= 该卡等级；0 = 0 级）。
    空列表 = 未能检测（图太小 / 不是配卡界面）。
    """
    if img.width < MIN_WIDTH_FOR_PIPS:
        return []
    img = img.convert("RGB")
    W, H = img.size
    mask = lit_mask(img)
    mb = mask.tobytes()
    rowcount = [mb[y * W:(y + 1) * W].count(255) for y in range(H)]

    rows = []
    exp_w = W * CARD_W_OF_W        # 按图宽反推的卡片宽（与分辨率无关）
    for (ra, rb) in _find_bands(rowcount):
        pad = max(4, rb - ra)
        y0, y1 = max(0, ra - pad), min(H, rb + pad)
        thick = _col_thickness(mask, y0, y1)
        nz = [x for x in range(W) if thick[x] > 0]
        if len(nz) < 8:
            continue
        segs = _contig(nz, gap=3, min_w=max(30, W * 0.02))
        if not segs:
            # ★ 兜底（2026-09-20）：整行**没有一张满级卡**（没有贯穿线）、
            #   豆子又少（每张 1~2 颗）时，卡片段会窄于 min_w → **整行丢失**。
            #   放宽到「一颗豆的宽度」再试；豆子水平居中，簇心就是卡片中心，
            #   因此这一遍拿到的列位置仍然可用（卡片宽稍后由列距反推）。
            segs = _contig(nz, gap=6, min_w=max(6, W * 0.004))
        if not segs:
            continue
        known = [round((a + b) / 2) for a, b in segs]
        widths = [b - a + 1 for a, b in segs]
        card_w = _median(widths)
        bf = (ra + rb) / 2 / H
        # ★ 该行是否含**真卡片宽**的段：左侧属性面板的进度条、状态/连击徽章之类
        #   也会产生窄段并被当成一行，它们没有真卡片宽 → 用来把这类杂散行剔掉
        #   （实测 shot4：y415 是右上角连击徽章、y832 是仓库区图标，宽度只有 39~47）。
        has_wide = any(w >= exp_w * 0.5 for w in widths)
        # 装备区判定：① 上半部分 ② 列数 ≤4 且最左列靠右（救「只截装备区」的部分截图）
        is_eq = (bf < INV_BF) or (len(known) <= CARD_COLS and min(known) > W * 0.25)
        rows.append({"band": (ra, rb), "bf": bf, "thick": thick,
                     "known": known, "widths": widths, "card_w": card_w,
                     "has_wide": has_wide, "is_eq": is_eq and has_wide})

    eq_rows = [r for r in rows if r["is_eq"]]
    if not eq_rows:
        # 兜底：整张图**一张满级卡都没有**（没有任何宽段）→ 退回原始判定并
        # 把结果写回标记，否则「全是窄豆簇」的界面会被整张丢掉。
        alt = [r for r in rows
               if (r["bf"] < INV_BF)
               or (r["known"] and len(r["known"]) <= CARD_COLS
                   and min(r["known"]) > W * 0.25)]
        if alt:
            eq_rows = alt
            for r in alt:
                r["is_eq"] = True
    if not eq_rows:
        # 最后兜底：只用来算网格，**不改 is_inventory 标记**（否则仓库区会被
        # 当成装备区，把网格锚点带偏）。
        eq_rows = rows
    all_known = [k for r in eq_rows for k in r["known"]]
    if not all_known:
        return []

    # ★ 卡片宽基准 = 段宽的**中位**，但只用「够宽」的段：
    #   杂散段宽度远小于卡片（实测 39~47 vs 152），必须剔掉，否则下面的锚点
    #   会被它们带偏（实测：网格整体左移 84px → 每一行的豆数全错）。
    all_widths = [w for r in eq_rows for w in r["widths"]]
    wide = [w for w in all_widths if w >= exp_w * 0.5]
    cw_ref = _median(wide)
    pitch_col = cw_ref * COL_PITCH_RATIO if cw_ref else 0.0

    # ★ 合并所有装备区行的列位置 → 完整列网格（上下两排共用同一套列位置）
    ks = sorted(all_known)
    merge_tol = (pitch_col * 0.45) if pitch_col else max(20, (ks[-1] - ks[0]) * 0.05)
    merged, cur = [], [ks[0]]
    for v in ks[1:]:
        if v - cur[-1] <= merge_tol:
            cur.append(v)
        else:
            merged.append(sum(cur) / len(cur))
            cur = [v]
    merged.append(sum(cur) / len(cur))
    if not cw_ref and len(merged) >= 2:
        # 整图都没有满级线（全是窄豆簇）→ 卡片宽只能反过来由列距推
        gaps0 = [merged[i + 1] - merged[i] for i in range(len(merged) - 1)]
        gmin = min(gaps0)
        pitch_col = _median([g for g in gaps0 if g <= gmin * 1.5])
    if pitch_col > 0 and len(merged) >= 2:
        # 用列距精修：间距应是 pitch 的**整数倍**（缺列时 2×、3×），
        # 与实测基准差太多的（杂散列造成的伪间距）直接丢弃。
        est = []
        for i in range(len(merged) - 1):
            g = merged[i + 1] - merged[i]
            k = max(1, round(g / pitch_col))
            if abs(g - k * pitch_col) <= pitch_col * 0.3:
                est.append(g / k)
        if est:
            pitch_col = _median(est)
    if pitch_col <= 0:
        return []                       # 定不出网格 → 放弃（上层安全回退）
    if not cw_ref:
        cw_ref = pitch_col / COL_PITCH_RATIO

    # ★ 锚点**投票**而不是取最左：杂散列（面板进度条）可能比真卡片更靠左。
    #   落在候选网格上的列越多，这个锚点越可信。
    def _hits(a):
        return sum(1 for k in all_known
                   if 0 <= round((k - a) / pitch_col) < CARD_COLS
                   and abs((k - a) / pitch_col - round((k - a) / pitch_col)) < 0.25)

    anchor = max(merged, key=lambda a: (_hits(a), -a))
    grid = [int(round(anchor + i * pitch_col)) for i in range(CARD_COLS)]

    out = []
    for idx, r in enumerate(rows, 1):
        cells = [_count_in_cell(r["thick"], cx, cw_ref, W) for cx in grid]
        inks = [_cell_ink(r["thick"], cx, cw_ref, W) for cx in grid]
        out.append({"row": idx, "band": r["band"],
                    "counts": [c[0] for c in cells],
                    "pos": [c[1] for c in cells],       # 每列豆的 x 坐标（调试/可视化用）
                    # ★ 满级线（第三信号）：该格是否有一条横贯全卡的亮线
                    "maxed": [k[2] for k in inks],
                    "is_inventory": not r["is_eq"],
                    "pitch_col": round(pitch_col)})
    return out


def align_rows(cands_list: list[list[int]],
               row_counts: list[list[int]]) -> Optional[list[tuple[int, int]]]:
    """把模型的**卡序列**与像素的**行列**做整体一致性对齐。

    ★ 为什么不用模型报的 row/col：实测（2026-09-20 端到端）在**完整提示词**下
      模型给的 row/col 很不可靠 —— 暮斩那张图里「北风 / 长时苦难 / 肢解」
      三张卡的 row 全报错，于是取到了别行的豆数，反而把原本对的判成错。
      但模型的**卡顺序**（从上到下、从左到右）是稳的 ⇒
      改用「顺序 + 容量候选集约束」反推位置：只有当所有卡都能在像素网格里
      找到「自己候选集里的那个豆数」时，才认定对齐成功。

    `cands_list`：每张卡的候选等级集（顺序 = 模型读卡顺序）
    `row_counts`：每行各格的豆数
    返回 `[(row_idx, col_idx), ...]`（与 cands_list 等长）；**无可行解返回 None**
    （宁可不用豆子，也不能拿错位的数字去覆盖本来正确的等级）。
    """
    from itertools import combinations

    total, R = len(cands_list), len(row_counts)
    if not total or not R:
        return None
    W = max(len(c) for c in row_counts)

    def row_fit(r: int, k: int, start: int):
        """给第 r 行的 k 张卡（cands_list[start:start+k]）选位置。

        ★ 候选集**为空**的卡（姿态卡这类 ``base_drain < 0`` 的卡、库外卡、
          姿态类）当作**通配**：豆数对不上不代表错位，它们只是没有可校验的候选集。
          实测（2026-09-20 暮斩）：姿态卡「狂风压境」候选集为空，旧实现要求它
          必须落在「该行全 0」的格子 → 整张图**无解** → 豆子全部作废
          （北风 1 级 / 长时苦难 0 级因此被容量反推判成 3 / 4 级）。
          改成通配后 8/8 全中。
        """
        row = row_counts[r]
        opts = []
        for pos in combinations(range(len(row)), k):
            ok = True
            for i, p in enumerate(pos):
                cand, v = cands_list[start + i], row[p]
                if cand and v not in cand:   # 有候选集 → 豆数必须落在里面（硬约束）
                    ok = False
                    break
            if ok:
                opts.append(list(pos))
        return opts

    best: dict = {"score": (-1, 0), "map": None}

    def rec(r: int, idx: int, chosen: list, strong: int, mixed: int) -> None:
        if r == R:
            if idx == total and (strong, -mixed) > best["score"]:
                best["score"], best["map"] = (strong, -mixed), list(chosen)
            return
        remain = total - idx
        min_here = max(0, remain - (R - r - 1) * W)
        max_here = min(W, remain)
        for k in range(min_here, max_here + 1):
            if k == 0:
                rec(r + 1, idx, chosen, strong, mixed)
                continue
            for picks in row_fit(r, k, idx):
                seg = cands_list[idx:idx + k]
                st = strong + sum(1 for c in seg if c)
                n_wild = sum(1 for c in seg if not c)
                # 次级判据：**专用槽位的卡（通配）应与普通卡分行** ——
                # 姿态这类卡在 UI 里独占一格，不会与普通卡同排。
                # 主判据（约束满足数）相同时用它破平，避免「姿态卡占掉第 1 排的
                # 某一格、把后面全部挤错」这种同为满分的错解。
                mx = mixed + (1 if (n_wild and n_wild < k) else 0)
                rec(r + 1, idx + k, chosen + [(r, p) for p in picks], st, mx)

    rec(0, 0, [], 0, 0)
    if best["map"] is None:
        return None
    # 至少要有一张卡是靠候选集强约束定下来的，否则对齐没有信息量
    # （卡片少时门槛相应降低；卡多时要求 ≥2 才有说服力）
    return best["map"] if best["score"][0] >= min(2, total) else None


def pick_rank(counts: list[int], col: Optional[int],
              candidates: list[int],
              maxed: Optional[list] = None,
              max_rank: Optional[int] = None) -> tuple[Optional[int], str]:
    """从一行的豆数里，为某个位置的卡选出等级。

    优先级：
      ① 该卡所在列，且该豆数**落在候选等级集里**（两条独立路径互相印证）
      ② **满级线**：该格有一条横贯全卡的亮线（= 满级），且库中 max_rank 落在
         候选集里 → 采信满级。用于「豆子被线吃掉一颗」的少数情况
         （实测：一击必杀满级 5 曾数成 4）。
      ③ 该行任意列的豆数落在候选集里，且**唯一**（姿态卡位置常与网格不一致，
         实测模型报 col1 而实际落在网格 col2 → 靠这一步纠正）
      ④ 该行**只有一个非零豆数** → 只能是它（位置无关；0 级卡不产生非零）
      ⑤ 候选集为空但该卡列上确有豆 → 采信该列

    返回 (等级 或 None, 判定来源)。返回 None 表示**不敢下结论**，
    上层应保持原策略（容量反推 + 标 `?`），而不是拿一个没印证的数字。
    """
    cand = set(candidates or [])
    if not counts:
        return None, "no-pips"
    nonzero = [(i + 1, n) for i, n in enumerate(counts) if n > 0]
    # ① 本列豆数 + 与候选集互相印证
    if col and 1 <= col <= len(counts):
        n = counts[col - 1]
        if cand and n in cand:
            return n, "本列豆数"
    # ② 满级线（第三信号）：见 docstring
    if maxed and max_rank is not None and int(max_rank) in cand:
        at_col = (bool(maxed[col - 1]) if (col and 1 <= col <= len(maxed))
                  else any(maxed))
        if at_col:
            return int(max_rank), "满级线"
    # ③ 全行唯一匹配候选集
    if cand:
        hit = {n for _, n in nonzero if n in cand}
        if len(hit) == 1:
            return hit.pop(), "行内唯一匹配"
        if col and 1 <= col <= len(counts) and counts[col - 1] > 0:
            return None, "ambiguous"        # 本列有豆但对不上候选 → 不硬猜
    # ④ 候选集为空时：
    if not cand:
        if col and 1 <= col <= len(counts) and counts[col - 1] > 0:
            return counts[col - 1], "本列豆数（无候选）"
        if len(nonzero) == 1:
            return nonzero[0][1], "行内唯一豆数"
    return None, "ambiguous"
