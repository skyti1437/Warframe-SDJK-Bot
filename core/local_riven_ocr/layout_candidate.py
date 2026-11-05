"""本地紫卡候选的通用几何层；接收 OCR 原文/框、锁框及可选整名 exact 回调。

参数都是相对行高/文字框宽度的无量纲比例。数值是通用排版容差：
同列中心容差 2.2 行高，垂直连续上限 4.2 行高，换行邻近 1.5 行高；
卡区在实际标题/词条包络外扩 2 行高，以容纳相邻图标，不扩到整图。
需要至少两行带符号/百分号的证据；多列时仅在一列独有标题、属性和页脚结构链时选中。
保留输入文字和四边形；输出 ids 始终是 raw.rows 的数组索引。
整名回调只参与同列相邻词条间的续行归属，不向布局层提供词表或模糊匹配。
"""

import math
import re
from dataclasses import dataclass
from statistics import median


_STRONG = re.compile(r"[+＋\-−\ufe63×xX]\s*\d|\d(?:[.,]\d+)?\s*[%％]")
_VALUE_TOKEN = re.compile(r"[+＋\-−\ufe63×xX]?\s*\d+(?:[.,]\d+)?(?:\s*[%％])?")
_ANCHOR = re.compile(r"^(?:段位|内融|循环次数|容量|极性)")
_HAN = re.compile(r"[\u3400-\u9fff]")
_LATIN = re.compile(r"[A-Za-z]")
_LETTER = re.compile(r"[A-Za-z\u3400-\u9fff]")
_UNIT_BASE = (
    r"(?:[皮纳微毫厘分十百千万兆吉太]?(?:米|秒|克|瓦|伏|安|升|焦|赫兹|字节)"
    r"|公里|分钟|小时|天|度|帧|次|发|点|个|mm|cm|km|m|ms|s|g|kg|hz|fps|px|kb|mb|gb|tb)"
)
_UNIT_ONLY = re.compile(
    rf"(?:平方|立方)?{_UNIT_BASE}(?:[/／每](?:平方|立方)?{_UNIT_BASE})?",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class _Row:
    index: int
    text: str
    left: float
    top: float
    right: float
    bottom: float

    @property
    def cx(self):
        return (self.left + self.right) / 2

    @property
    def cy(self):
        return (self.top + self.bottom) / 2

    @property
    def width(self):
        return self.right - self.left

    @property
    def height(self):
        return self.bottom - self.top


def _rows(raw):
    rows = []
    for index, value in enumerate(raw.get("rows", [])):
        try:
            quad = value["quad"]
            xs = [float(point[0]) for point in quad]
            ys = [float(point[1]) for point in quad]
            text = str(value.get("text", "")).strip()
            if not xs or not all(math.isfinite(v) for v in xs + ys) or not text:
                continue
            row = _Row(index, text, min(xs), min(ys), max(xs), max(ys))
            if row.width > 0 and row.height > 0:
                rows.append(row)
        except (KeyError, TypeError, ValueError, IndexError):
            continue
    return rows


def _same_column(a, b, height):
    center_distance = abs(a.cx - b.cx)
    if center_distance <= max(2.2 * height, 0.25 * min(a.width, b.width)):
        return True
    overlap = max(0, min(a.right, b.right) - max(a.left, b.left))
    return overlap >= 0.5 * min(a.width, b.width) and center_distance <= 0.55 * max(
        a.width, b.width
    )


def _column_scope(seeds, height):
    center = median(row.cx for row in seeds)
    column_width = max(row.right for row in seeds) - min(row.left for row in seeds)

    def in_column(row):
        return (
            row.width <= 1.8 * column_width
            and abs(row.cx - center) <= max(2.2 * height, 0.55 * column_width)
            and any(_same_column(row, seed, height) for seed in seeds)
        )

    return center, column_width, in_column


def _has_attribute_words(row):
    """数值余文或独立词行须有文字，纯计量单位不作为属性证据。"""
    match = _VALUE_TOKEN.search(row.text)
    words = row.text if match is None else row.text[: match.start()] + row.text[match.end() :]
    words = words.strip(" \t\r\n,，.。:：()（）[]【】")
    return bool(_LETTER.search(words)) and not _UNIT_ONLY.fullmatch(words)


def _has_ordered_structure(rows, anchors, seeds, height):
    """只验该数值列自己的标题→属性词条→元数据；距离沿用原几何阈值。"""
    _, _, in_column = _column_scope(seeds, height)
    first_y, last_y = min(r.cy for r in seeds), max(r.cy for r in seeds)
    local = [row for row in rows if in_column(row)]
    if not any(last_y < row.cy <= last_y + 4.2 * height for row in anchors if in_column(row)):
        return False

    numeric_bands = _bands(seeds, height)
    title_bands = _bands(
        [
            row
            for row in local
            if first_y - 6.6 * height <= row.cy < first_y
            and row not in anchors
            and not _STRONG.search(row.text)
        ],
        height,
    )
    title = []
    boundary = first_y
    skipped_preposed = False
    for group in reversed(title_bands):
        cy = median(row.cy for row in group)
        # 与单列路径相同：首条裸数值前的紧邻汉字词名属于属性，不属于标题。
        if (
            not title
            and not skipped_preposed
            and first_y - cy <= 1.5 * height
            and any(_HAN.search(row.text) for row in group)
            and not any(_LATIN.search(row.text) for row in group)
            and not any(_LETTER.search(_STRONG.sub("", row.text)) for row in numeric_bands[0])
        ):
            boundary = cy
            skipped_preposed = True
            continue
        if boundary - cy > (4.2 if not title else 2.4) * height:
            break
        if any(_LETTER.search(row.text) for row in group):
            title.append(group)
            boundary = cy
    title.reverse()
    han_title = next(
        (i for i, group in enumerate(title) if any(_HAN.search(row.text) for row in group)),
        None,
    )
    if han_title is None or not any(
        i >= han_title and any(_LATIN.search(row.text) for row in group)
        for i, group in enumerate(title)
    ):
        return False
    title_ids = {row.index for group in title for row in group}

    for band in numeric_bands:
        cy = median(row.cy for row in band)
        same_band = [
            row for row in local if abs(row.cy - cy) <= 0.45 * height and row not in anchors
        ]
        if any(_has_attribute_words(row) for row in same_band):
            continue
        preceding = _bands(
            [
                row
                for row in local
                if cy - 1.5 * height <= row.cy < cy - 0.45 * height
                and row.index not in title_ids
                and row not in anchors
                and not _STRONG.search(row.text)
            ],
            height,
        )
        if not preceding or not (
            any(_has_attribute_words(row) for row in preceding[-1])
            and not any(_LATIN.search(row.text) for row in preceding[-1])
        ):
            return False
    return True


def _bands(rows, height):
    result = []
    for row in sorted(rows, key=lambda item: (item.cy, item.left, item.index)):
        if result and abs(row.cy - median(r.cy for r in result[-1])) <= 0.45 * height:
            result[-1].append(row)
        else:
            result.append([row])
    return [sorted(group, key=lambda item: (item.left, item.index)) for group in result]


def _bounds(rows):
    return [
        min(row.left for row in rows),
        min(row.top for row in rows),
        max(row.right for row in rows),
        max(row.bottom for row in rows),
    ]


def _source(rows):
    return {"ids": [row.index for row in rows], "text": " ".join(row.text for row in rows)}


def _exact_full_name(rows, height, exact_name):
    """只把数值边界外的整段词名交给 exact；只允许空白归一。"""
    from .name_evidence import _extract

    ordered = [row for band in _bands(rows, height) for row in band]
    name, _, _ = _extract(_source(ordered)["text"])
    if name is None:
        return None
    sid = exact_name(name)
    if sid:
        return sid
    compact = re.sub(r"\s+", "", name)
    return exact_name(compact) if compact != name else None


def _empty(raw, reason, diagnostics):
    return {
        "accepted_region": False,
        "reason": [reason],
        "region": None,
        "weapon": None,
        "lines": [],
        "line_sources": [],
        "title_ids": [],
        "excluded_ids": list(range(len(raw.get("rows", [])))),
        "diagnostics": diagnostics,
    }


def analyze(raw, exact_name=None):
    """推断唯一连续列；整名证据仅可消解相邻词条间的续行归属。"""
    rows = _rows(raw)
    diagnostics = {
        "title_raw": None,
        "title_lines": [],
        "candidate_regions": [],
        "unresolved_ids": [],
        "ambiguities": [],
    }
    if not rows:
        return _empty(raw, "no_text", diagnostics)
    height = median(row.height for row in rows)
    diagnostics["line_height"] = height
    anchors = [row for row in rows if _ANCHOR.search(row.text) and not _STRONG.search(row.text)]
    # 裸数字可在同列同一行被接回，但不能串联主区域；否则数字标题/宽页脚会桥接两张卡。
    seeds = [row for row in rows if _STRONG.search(row.text) and row not in anchors]
    graph = {row.index: set() for row in seeds}
    for offset, a in enumerate(seeds):
        for b in seeds[offset + 1 :]:
            gap = abs(a.cy - b.cy)
            if gap <= 0.45 * height or gap > 4.2 * height or not _same_column(a, b, height):
                continue
            if any(
                min(a.cy, b.cy) < anchor.cy < max(a.cy, b.cy)
                and _same_column(anchor, a, height)
                and _same_column(anchor, b, height)
                for anchor in anchors
            ):
                continue
            graph[a.index].add(b.index)
            graph[b.index].add(a.index)
    by_id = {row.index: row for row in seeds}
    visited = set()
    candidates = []
    for seed in seeds:
        if seed.index in visited:
            continue
        pending, component = [seed.index], []
        while pending:
            index = pending.pop()
            if index in visited:
                continue
            visited.add(index)
            component.append(by_id[index])
            pending.extend(graph[index] - visited)
        strong_bands = _bands([r for r in component if _STRONG.search(r.text)], height)
        if len(strong_bands) >= 2:
            candidates.append(component)
            diagnostics["candidate_regions"].append(_bounds(component))
    if len(candidates) > 1:
        supported = [
            component
            for component in candidates
            if _has_ordered_structure(rows, anchors, component, height)
        ]
        if len(supported) == 1:
            candidates = supported
    if len(candidates) != 1:
        if candidates:
            diagnostics["ambiguities"].append("multiple_continuous_columns")
        return _empty(
            raw, "ambiguous_regions" if candidates else "no_supported_region", diagnostics
        )

    seeds = candidates[0]
    seed_ids = {row.index for row in seeds}
    center, column_width, in_column = _column_scope(seeds, height)

    first_y, last_y = min(r.cy for r in seeds), max(r.cy for r in seeds)
    footer = min((r.cy for r in anchors if r.cy > first_y and in_column(r)), default=math.inf)
    last_seed_bottom = max(r.bottom for r in seeds)
    rank_anchors = [
        row
        for row in anchors
        if row.cy > last_y and in_column(row) and re.fullmatch(r"段位\s*\d+", row.text)
    ]
    footer_companions = {}
    for row in rows:
        # 页脚图标和数字可能被 OCR 合成字母数字串；仍须在唯一段位锚点同带，
        # 且严格位于最后词条以下。带符号、计量单位或中文的行不能按页脚丢弃。
        if not (
            in_column(row)
            and re.fullmatch(r"[A-Za-z0-9]+", row.text)
            and re.search(r"\d", row.text)
            and not re.fullmatch(r"\d+(?:m|s|ms)", row.text, re.I)
        ):
            continue
        nearby_anchors = [
            anchor
            for anchor in rank_anchors
            if row.top >= last_seed_bottom
            and abs(row.cy - anchor.cy) <= 0.45 * height
            and max(row.top, anchor.top) < min(row.bottom, anchor.bottom)
        ]
        if len(nearby_anchors) == 1:
            footer_companions[row.index] = nearby_anchors[0].index
    column = [
        row
        for row in rows
        if in_column(row)
        and first_y - 6.6 * height <= row.cy <= last_y + 1.5 * height
        and row.cy < footer
        and row not in anchors
        and row.index not in footer_companions
    ]
    bands = _bands(column, height)
    numeric_bands = [i for i, group in enumerate(bands) if any(r.index in seed_ids for r in group)]
    if len(numeric_bands) < 2:
        return _empty(raw, "insufficient_same_column_rows", diagnostics)
    grouped = {index: list(bands[index]) for index in numeric_bands}
    assigned = {r.index for group in grouped.values() for r in group}
    first_band = numeric_bands[0]

    def preposed_target(index):
        # 紧邻的汉字词名 + 无词名的数值行，才是向后归属的结构证据。
        # 混合汉字/Latin 自命名与纯 Latin 标题续行不满足这一条件。
        group = bands[index]
        target = index + 1
        if (
            target in grouped
            and any(_HAN.search(r.text) for r in group)
            and not any(re.search(r"[A-Za-z]", r.text) for r in group)
            and not any(_LETTER.search(_STRONG.sub("", r.text)) for r in bands[target])
            and (median(r.cy for r in bands[target]) - median(r.cy for r in group)) / height <= 1.5
        ):
            return target
        return None

    # 先在未扩展的 bands 上保留标题，不能让首词条的续行过程吞掉标题。
    title_groups = []
    title_boundary = median(r.cy for r in bands[first_band])
    for index in range(first_band - 1, -1, -1):
        group = bands[index]
        if any(_STRONG.search(r.text) for r in group):
            continue
        if not any(_LETTER.search(r.text) for r in group):
            continue
        cy = median(r.cy for r in group)
        if preposed_target(index) is not None:
            title_boundary = cy
            continue
        if title_boundary - cy > (4.2 if not title_groups else 2.4) * height:
            break
        title_groups.append(group)
        title_boundary = cy
    title_groups.reverse()
    title = [row for group in title_groups for row in group]
    title_ids = {row.index for row in title}

    # 读序中已开启的上一词条接收普通续行；仅明确前置词名能接下一数值行。
    ownership_handled = set()
    for index, group in enumerate(bands):
        if (
            index in grouped
            or index in ownership_handled
            or any(r.index in title_ids or _STRONG.search(r.text) for r in group)
        ):
            continue
        cy = median(r.cy for r in group)
        previous = max((target for target in numeric_bands if target < index), default=None)
        previous_distance = math.inf
        closes_previous = False
        pending_closers = []
        if previous is not None:
            previous_bands = _bands(grouped[previous], height)
            previous_distance = (cy - median(r.cy for r in previous_bands[-1])) / height
            previous_text = " ".join(r.text for band in previous_bands for r in band)
            text = " ".join(r.text for r in group)
            pairs = {"(": ")", "（": "）", "[": "]", "【": "】"}
            for character in previous_text:
                if character in pairs:
                    pending_closers.append(pairs[character])
                elif pending_closers and character == pending_closers[-1]:
                    pending_closers.pop()
            closes_previous = bool(pending_closers and pending_closers[-1] in text)
        if not closes_previous and not any(_LETTER.search(r.text) for r in group):
            continue
        following = min((target for target in numeric_bands if target > index), default=None)
        if exact_name is not None and previous is not None and following is not None:
            # 只比较相邻两个数值带之间的完整连续片段，不从中截取词库子串。
            run_indices = list(range(index, following))
            run = [row for i in run_indices for row in bands[i]]
            chain = [previous_bands[-1], *(bands[i] for i in run_indices), bands[following]]
            local_chain = all(
                (median(r.cy for r in b) - median(r.cy for r in a)) / height <= 1.5
                for a, b in zip(chain, chain[1:])
            )
            valid_run = all(
                any(_LETTER.search(r.text) for r in bands[i])
                and not any(r.index in title_ids or _STRONG.search(r.text) for r in bands[i])
                for i in run_indices
            )
            # 既有括号闭合与标题证据优先，整名查询不能抢走括号续行。
            closes_in_run = bool(pending_closers and pending_closers[-1] in _source(run)["text"])
            if local_chain and valid_run and not closes_in_run:
                previous_exact = _exact_full_name(grouped[previous], height, exact_name)
                previous_extended = _exact_full_name(grouped[previous] + run, height, exact_name)
                next_extended = _exact_full_name(run + bands[following], height, exact_name)
                exact_target = None
                if previous_extended and not next_extended:
                    exact_target = previous
                elif previous_exact and not previous_extended and next_extended:
                    exact_target = following
                evidence = {
                    "source": _source(run),
                    "previous_ids": [r.index for r in grouped[previous]],
                    "next_ids": [r.index for r in bands[following]],
                    "previous_exact": bool(previous_exact),
                    "previous_extended_exact": bool(previous_extended),
                    "next_extended_exact": bool(next_extended),
                    "target": (
                        "previous"
                        if exact_target == previous
                        else "next"
                        if exact_target == following
                        else None
                    ),
                }
                diagnostics.setdefault("name_ownership", []).append(evidence)
                ownership_handled.update(run_indices)
                if exact_target is not None:
                    grouped[exact_target].extend(run)
                    assigned.update(r.index for r in run)
                elif "ambiguous_name_continuation" not in diagnostics["ambiguities"]:
                    diagnostics["ambiguities"].append("ambiguous_name_continuation")
                continue
        target = preposed_target(index)
        if closes_previous and previous_distance <= 1.5:
            target = previous
        elif target is None and previous_distance <= 1.5:
            target = previous
        if target is None:
            continue
        grouped[target].extend(group)
        assigned.update(r.index for r in group)

    sources = []
    for index in numeric_bands:
        ordered = [row for band in _bands(grouped[index], height) for row in band]
        sources.append(_source(ordered))
    title_lines = [_source(group)["text"] for group in title_groups]
    title_raw = " ".join(title_lines) or None
    # 沿用业务层的武器名投影：仅裁去首个 Latin 起的标题后缀，不校正任何字。
    weapon = re.split(r"[A-Za-z]", title_raw or "", maxsplit=1)[0].strip() or None
    used_ids = {row.index for row in title} | assigned
    used = [row for row in rows if row.index in used_ids]
    region = _bounds(used)
    width, image_height = float(raw.get("width", math.inf)), float(raw.get("height", math.inf))
    region = [
        max(0, region[0] - 2 * height),
        max(0, region[1] - 2 * height),
        min(width, region[2] + 2 * height),
        min(image_height, region[3] + 2 * height),
    ]
    diagnostics.update(
        title_raw=title_raw,
        title_lines=title_lines,
        seed_ids=sorted(seed_ids),
        column_center=center,
        column_width=column_width,
        footer_y=footer if math.isfinite(footer) else None,
        footer_companion_exclusions=[
            {"row_id": row_id, "rank_anchor_id": anchor_id}
            for row_id, anchor_id in sorted(footer_companions.items())
        ],
        input_row_ids={str(i): value.get("id", i) for i, value in enumerate(raw.get("rows", []))},
        unresolved_ids=[
            row.index for row in column if row.index not in used_ids and row.cy >= first_y
        ],
    )
    return {
        "accepted_region": True,
        "reason": ["unique_continuous_column"],
        "region": region,
        "weapon": weapon,
        "lines": [source["text"] for source in sources],
        "line_sources": sources,
        "title_ids": [row.index for row in title],
        "excluded_ids": [i for i in range(len(raw.get("rows", []))) if i not in used_ids],
        "diagnostics": diagnostics,
    }


def assign_locks(icons, analysis, raw):
    """锁必须在共享卡区且贴近某一词条的实际文字框；等近两行不抢配。"""
    rows = {row.index: row for row in _rows(raw)}
    associations = []
    for icon_index, icon in enumerate(icons):
        association = {
            "icon_index": icon_index,
            "line_index": None,
            "reason": "region_unconfirmed",
            "in_region": False,
        }
        associations.append(association)
        if not analysis.get("accepted_region") or not analysis.get("region"):
            continue
        try:
            left, top, right, bottom = map(float, icon.get("box", icon.get("bbox", [])))
            if (
                right <= left
                or bottom <= top
                or not all(math.isfinite(v) for v in (left, top, right, bottom))
            ):
                raise ValueError
        except (TypeError, ValueError):
            association["reason"] = "invalid_icon_box"
            continue
        cx, cy = (left + right) / 2, (top + bottom) / 2
        x0, y0, x1, y1 = analysis["region"]
        association["in_region"] = x0 <= cx <= x1 and y0 <= cy <= y1
        if not association["in_region"]:
            association["reason"] = "outside_region"
            continue
        candidates = []
        for line_index, source in enumerate(analysis.get("line_sources", [])):
            local_distances = []
            for index in source.get("ids", []):
                row = rows.get(index)
                if row is None:
                    continue
                scale = max(row.height, bottom - top)
                horizontal_gap = max(row.left - cx, cx - row.right, 0)
                vertical_distance = abs(cy - row.cy) / scale
                if horizontal_gap <= 1.35 * scale and vertical_distance <= 1.1:
                    local_distances.append(vertical_distance)
            if local_distances:
                candidates.append((min(local_distances), line_index))
        candidates.sort()
        if not candidates:
            association["reason"] = "unmatched_line"
        elif len(candidates) > 1 and candidates[1][0] - candidates[0][0] <= 0.25:
            association["reason"] = "ambiguous_line"
        else:
            association["line_index"] = candidates[0][1]
            association["reason"] = "assigned"
            association["distance"] = candidates[0][0]
    retained = [a["icon_index"] for a in associations if a["in_region"]]
    return {
        "associations": associations,
        "retained": retained,
        "assigned": [a["icon_index"] for a in associations if a["line_index"] is not None],
        "rejected": [a["icon_index"] for a in associations if not a["in_region"]],
    }
