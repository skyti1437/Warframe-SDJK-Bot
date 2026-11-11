"""英文客户端紫卡的本地 OCR 版面解析；与中文 layout_candidate 分开维护。

英文卡面的排版规律：
- 每条词条都以数值起头（``+106.4%`` / ``-24.5%`` / ``x1.57``），不以数值起头的行
  就是上一条的折行（``Capacity`` / ``(x2 for Heavy Attacks)`` / ``Corpus``）；
- 标题是首条词条正上方紧邻的 1~3 行，可能在空格或连字符处折行，末词是紫卡名，
  前面是武器名（武器名可以有多个词，如 Dual Toxocyst）；
- 页脚以 ``MR`` / ``MASTERY`` / ``PM`` 起头，同一带还有循环次数，整带不算词条。

输出与 layout_candidate.analyze 同形状的版面字典，之后复用 base_worker 的语义
投影、逐行证据和采信判定，所以两种语言给出同样的内部词条 ID。
所有阈值都是相对词条行高的比例，不按卡名、武器或具体数值调参。
"""

from __future__ import annotations

import copy
import math
import re
from dataclasses import dataclass
from statistics import median

from . import base_worker as base
from .annotation_fields import exact_annotated_name

_CJK = re.compile(r"[\u3400-\u9fff]")
_LATIN_WORD = re.compile(r"[A-Za-z]{3,}")
_SIGN = "+＋\\-−–—－"
# 词条起头：符号+数字，或带小数的乘数（x1.57）。「x2 for Bows」这类括注没有小数，不算。
_VALUE_START = re.compile(rf"^[{_SIGN}]\s*\d|^[x×]\s*\d+[.,]\d")
# 行首图标被 OCR 读成的杂字（锁形图标常读成 0 / O / @ 等），最多一个字符。
_LEAD_NOISE = re.compile(
    rf"^\s*(?:[0-9OoQ@&•·◆◇*#]|[^\w\s{_SIGN}x×(])?\s*(?=[{_SIGN}]\s*\d|[x×]\s*\d)"
)
_VALUE_TOKEN = re.compile(
    rf"^(?P<value>(?:[{_SIGN}]|[x×])\s*\d+(?:[.,]\d+)?\s*[%％]?)\s*(?P<rest>.*)$"
)
_FOOTER = re.compile(r"^\W*(?:MR|PM|MASTERY)(?=[\s\d\W]|$)")
_HIDDEN = re.compile(r"^[^\w?？¿]*[?？¿]\s*[?？¿i•·]*[^\w]*$")


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
    def height(self):
        return self.bottom - self.top


def _rows(raw):
    rows = []
    for index, value in enumerate(raw.get("rows", [])):
        try:
            xs = [float(point[0]) for point in value["quad"]]
            ys = [float(point[1]) for point in value["quad"]]
            text = str(value.get("text", "")).strip()
        except (KeyError, TypeError, ValueError, IndexError):
            continue
        if not text or not xs or not all(math.isfinite(v) for v in xs + ys):
            continue
        row = _Row(index, text, min(xs), min(ys), max(xs), max(ys))
        if row.right > row.left and row.bottom > row.top:
            rows.append(row)
    return rows


def _strip_lead(text):
    return _LEAD_NOISE.sub("", text, count=1)


def _is_value(text):
    return bool(_VALUE_START.match(_strip_lead(text)))


def _is_footer(text):
    return bool(_FOOTER.match(text)) and not _is_value(text)


def is_english_card(raw):
    """卡面语言分流：中文卡的词条行全是汉字词名，图标杂字最多零星一两个汉字。"""
    texts = [str(row.get("text", "")) for row in raw.get("rows", []) if isinstance(row, dict)]
    if sum(len(_CJK.findall(text)) >= 2 for text in texts) >= 2:
        return False
    return sum(_is_value(text) and bool(_LATIN_WORD.search(text)) for text in texts) >= 2


def _bands(rows, height):
    groups = []
    for row in sorted(rows, key=lambda r: (r.cy, r.left)):
        if groups and abs(row.cy - median(r.cy for r in groups[-1])) <= 0.45 * height:
            groups[-1].append(row)
        else:
            groups.append([row])
    return [sorted(group, key=lambda r: r.left) for group in groups]


def _source(rows):
    return {"ids": [row.index for row in rows], "text": " ".join(row.text for row in rows)}


def _bounds(rows):
    return [
        min(r.left for r in rows),
        min(r.top for r in rows),
        max(r.right for r in rows),
        max(r.bottom for r in rows),
    ]


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
        "layout_language": "en",
    }


def _clean_stat(text, name_ok):
    """去掉行首图标杂字和数值与词名之间的元素图标杂字；只有去掉后整名命中才采用。"""
    stripped = _strip_lead(text)
    removed = text[: len(text) - len(stripped)].strip()
    match = _VALUE_TOKEN.match(stripped)
    if not match:
        return text, None
    value, rest = match["value"], match["rest"]
    # 数值与词名之间的非字母字符只可能是元素图标杂字（词名都以字母或括号起头）。
    junk = re.match(r"^[^A-Za-z(]*", rest).group(0)
    name = rest[len(junk) :]
    # 元素图标偶尔被读成一个字母（可带一个标点）贴在词名前（「XImpact」「S Toxin」「C:Magnetic」），
    # 去掉后须整名命中。
    if len(name) > 1 and not name_ok(name):
        one = re.match(r"^[A-Za-z][:;.,'`]?\s*(?=[A-Z])", name)
        if one and name_ok(name[one.end() :]):
            junk, name = junk + one.group(0), name[one.end() :]
    junk = junk.strip()
    if not removed and not junk:
        return text, None
    return f"{value.strip()} {name}".strip(), {"prefix": removed or None, "icon_text": junk or None}


def analyze(raw, exact_name):
    """推断唯一英文卡区；输出字段与中文版面层一致，另带 layout_language=en。"""

    def name_ok(name):
        if not name:
            return False
        if exact_name(name) or exact_name(re.sub(r"\s+", "", name)):
            return True
        return bool(exact_annotated_name(name, exact_name)[1])

    rows = _rows(raw)
    diagnostics = {
        "title_raw": None,
        "title_lines": [],
        "candidate_regions": [],
        "unresolved_ids": [],
        "ambiguities": [],
        "cleaned_lines": [],
    }
    if not rows:
        return _empty(raw, "no_text", diagnostics)
    values = [row for row in rows if _is_value(row.text)]
    if len(values) < 2:
        return _empty(raw, "insufficient_value_rows", diagnostics)
    height = median(row.height for row in values)
    diagnostics["line_height"] = height
    anchors = [row for row in rows if _is_footer(row.text)]

    # 数值行按「同列且垂直相近」连成卡区候选；中间夹着折行也不超过 4.2 行高。
    graph = {row.index: set() for row in values}
    for offset, a in enumerate(values):
        for b in values[offset + 1 :]:
            if abs(a.cy - b.cy) <= 4.2 * height and abs(a.cx - b.cx) <= 3.0 * height:
                graph[a.index].add(b.index)
                graph[b.index].add(a.index)
    by_id = {row.index: row for row in values}
    seen, clusters = set(), []
    for row in values:
        if row.index in seen:
            continue
        pending, component = [row.index], []
        while pending:
            index = pending.pop()
            if index in seen:
                continue
            seen.add(index)
            component.append(by_id[index])
            pending.extend(graph[index] - seen)
        if len(_bands(component, height)) >= 2:
            clusters.append(sorted(component, key=lambda r: r.cy))
            diagnostics["candidate_regions"].append(_bounds(component))

    def footer_of(cluster):
        left, _, right, _ = _bounds(cluster)
        last = max(r.cy for r in cluster)
        near = [
            a
            for a in anchors
            if last < a.cy <= last + 6.0 * height
            and left - 3 * height <= a.left <= right + 3 * height
        ]
        return min(near, key=lambda a: a.cy) if near else None

    if len(clusters) > 1:
        # 拼接/洗卡整屏里还有 GAIN 栏等数值列：只认下方带段位页脚的那一列。
        with_footer = [cluster for cluster in clusters if footer_of(cluster)]
        if len(with_footer) == 1:
            clusters = with_footer
    if len(clusters) != 1:
        if clusters:
            diagnostics["ambiguities"].append("multiple_value_columns")
        return _empty(raw, "ambiguous_regions" if clusters else "no_supported_region", diagnostics)

    cluster = clusters[0]
    left, _, right, _ = _bounds(cluster)
    center = median(r.cx for r in cluster)
    first_y, last_y = cluster[0].cy, cluster[-1].cy
    footer = footer_of(cluster)

    def in_column(row):
        # 只按词条列左右范围判：元素图标会把一行切成「+92.7%」「Corrosive」两段，
        # 后半段离列中心很远但仍在列内；相邻卡露出的文字在范围之外。
        return left - 2 * height <= row.left and row.right <= right + 2 * height

    footer_rows = (
        [r for r in rows if abs(r.cy - footer.cy) <= 0.6 * height and r.top >= last_y]
        if footer
        else []
    )
    footer_ids = {r.index for r in footer_rows}
    limit = footer.cy - 0.6 * height if footer else last_y + 4.2 * height
    body = [
        r
        for r in rows
        if in_column(r)
        and first_y - 0.45 * height <= r.cy < limit
        and r.index not in footer_ids
        and r not in anchors
    ]
    lines, sources, unresolved = [], [], []
    previous_bottom = None
    for band in _bands(body, height):
        text = " ".join(r.text for r in band)
        gap_ok = (
            previous_bottom is None or min(r.top for r in band) - previous_bottom <= 1.6 * height
        )
        if _is_value(text):
            sources.append({"rows": list(band)})
        elif _HIDDEN.match(text) and ("?" in text or "？" in text or "¿" in text):
            sources.append({"rows": list(band), "hidden": True})
        elif sources and gap_ok and not sources[-1].get("hidden"):
            sources[-1]["rows"].extend(band)
        else:
            unresolved.extend(r.index for r in band)
            continue
        previous_bottom = max(r.bottom for r in band)
    if not footer:
        # 没有页脚锚点时，末条之后的折行只能靠紧邻关系接回；之后的行不算卡区。
        diagnostics["footer_missing"] = True

    for item in sources:
        source = _source(item["rows"])
        raw_text = source["text"]
        if item.get("hidden"):
            text, cleaned = "???", {"prefix": None, "icon_text": raw_text}
        else:
            text, cleaned = _clean_stat(raw_text, name_ok)
        if cleaned:
            source.update(
                text=text,
                raw_text=raw_text,
                text_kind="derived_parse_copy_not_OCR_raw",
                removed_icon_text=cleaned,
            )
            diagnostics["cleaned_lines"].append({"line": text, "raw": raw_text, **cleaned})
        lines.append(text)
        sources_index = len(lines) - 1
        item["source"] = source
        item["line_index"] = sources_index

    # 标题：首条词条正上方紧邻的 1~3 带，自下而上取，连续间距不超过 1.8 行高。
    above = [
        r
        for r in rows
        if in_column(r)
        and first_y - 6.0 * height <= r.cy < first_y - 0.45 * height
        and r not in anchors
        and not _is_value(r.text)
        # 标题框要有拉丁字母或问号，且不小于半个行高；装饰被读成的极小杂字框不进标题。
        and (re.search(r"[A-Za-z?？]", r.text) and r.height >= 0.5 * height)
    ]
    title_groups, edge = [], cluster[0].top
    for band in reversed(_bands(above, height)):
        if len(title_groups) == 3 or edge - max(r.bottom for r in band) > 1.8 * height:
            break
        if not _LATIN_WORD.search(" ".join(r.text for r in band)) and not any(
            "?" in r.text for r in band
        ):
            break
        title_groups.insert(0, band)
        edge = min(r.top for r in band)
    title_lines = [_source(group)["text"] for group in title_groups]
    title_raw = None
    for line in title_lines:
        title_raw = (
            line
            if title_raw is None
            else (title_raw + line if title_raw.endswith("-") else f"{title_raw} {line}")
        )
    tokens = (title_raw or "").split()
    # 末词是紫卡名（含「???」拼接预览）；只裁掉这一个词，不校正任何字。
    weapon = " ".join(tokens[:-1]) or None
    title = [r for group in title_groups for r in group]

    used = title + [r for item in sources for r in item["rows"]]
    used_ids = {r.index for r in used}
    region = _bounds(used + footer_rows)
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
        riven_name=tokens[-1] if tokens else None,
        splice_preview=bool(tokens) and tokens[-1].strip("?？") == "",
        seed_ids=sorted(r.index for r in cluster),
        column_center=center,
        footer_y=footer.cy if footer else None,
        footer_row_ids=sorted(footer_ids),
        input_row_ids={str(i): value.get("id", i) for i, value in enumerate(raw.get("rows", []))},
        unresolved_ids=sorted(unresolved),
    )
    return {
        "accepted_region": True,
        "reason": ["unique_value_column_en"],
        "region": region,
        "weapon": weapon,
        "lines": lines,
        "line_sources": [item["source"] for item in sources],
        "title_ids": [r.index for r in title],
        "excluded_ids": [i for i in range(len(raw.get("rows", []))) if i not in used_ids],
        "diagnostics": diagnostics,
        "layout_language": "en",
    }


def replay(payload, *, exact, weapon_catalog):
    """英文版面 + 共用语义投影；返回字段与中文 replay_logic.replay 一致。"""
    from .replay_logic import validate_input

    validate_input(payload)
    raw = copy.deepcopy(payload["raw"])
    analysis = analyze(raw, exact)
    structured, semantics = base.project_semantics(analysis, weapon_catalog)
    geometry = base.line_geometry(raw, analysis.get("line_sources", []), structured["lines"])
    state, reasons = base.recognition_state(analysis, structured, semantics, geometry, [])
    diagnostics = analysis.get("diagnostics", {})
    if diagnostics.get("splice_preview"):
        state = "partial" if analysis.get("accepted_region") else "rejected"
        reasons.append("拼接预览卡：标题或词条含 ??? 未决项，不是成品紫卡")
    if diagnostics.get("cleaned_lines") and state == "accepted":
        state = "partial"
    if diagnostics.get("cleaned_lines"):
        reasons.append("去掉了图标被读成的杂字，词条行仅为解析副本，原文保留；需要卡区复读一致")
    parsed_count = len(semantics.get("parser_positive", [])) + len(
        semantics.get("parser_negative", [])
    )
    if parsed_count + base_hidden(structured["lines"]) != len(structured["lines"]):
        state = "partial" if analysis.get("accepted_region") else "rejected"
        reasons.append("解析词条数量与来源词条行数量不一致，保留缺项/重复冲突")
    return {
        "status": "ok",
        "raw": raw,
        "structured": structured,
        "candidate": analysis,
        "trace": {**analysis, **semantics, "layout_language": "en"},
        "raw_icons": [],
        "icons": [],
        "associations": [],
        "line_geometry": geometry,
        "recognition_state": state,
        "recognition_reasons": reasons,
        "prefix_recovered_lines": len(diagnostics.get("cleaned_lines", [])),
        "original_structured_before_recovery": structured,
        "action": None if state == "accepted" else "本地结果需核对，请补清晰原图或文字输入。",
        "OCR_calls": 0,
        "handler_executed": False,
    }


def base_hidden(lines):
    from .. import riven_analysis as ra

    return ra.hidden_line_count(lines)


__all__ = ["analyze", "is_english_card", "replay"]
