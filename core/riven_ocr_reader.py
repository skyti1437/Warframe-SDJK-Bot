"""紫卡单渠道 OCR。首读原图，业务未采信时才用同渠道复读。"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal

from PIL import Image, ImageFilter, ImageOps

from . import loadout_ocr, riven_analysis
from .commands.vision import VisionCommands
from .i18n import L
from .local_ocr import decode_image_data_url
from .local_riven_ocr import analyze_raw, weapon_identity
from .logging_compat import logger
from .parser import RIVEN_STAT_ZH

_REVERSE = {name: sid for sid, name in RIVEN_STAT_ZH.items()}
_MASTERY = re.compile(r"(?:mastery[ _]rank|mr|段位|精通段位|等级需求)\s*:?\s*(\d+)(.*)", re.I)
_PROPERTIES = {
    "drain": re.compile(r"(?:drain|capacity|容量|消耗)\s*:?\s*(\d+)", re.I),
    "drain_color": re.compile(r"(?:drain[ _]color|容量颜色)\s*:?\s*(\w+)", re.I),
    "rank": re.compile(r"(?:upgrade[ _]rank|rank|强化等级)\s*:?\s*([0-8])", re.I),
    "polarity": re.compile(r"(?:polarity|极性)\s*:?\s*(madurai|vazarin|naramon)", re.I),
}


def _plain_lines(content):
    """保留原文数值和符号，只去除排版与明确声明的坐标列。"""
    content = unicodedata.normalize("NFKC", content).replace("−", "-")
    cleaned = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", content).strip()
    try:
        obj = json.loads(cleaned)
    except ValueError:
        obj = None
    if isinstance(obj, list):
        lines = loadout_ocr.coordinate_text_lines(content)
    else:
        lines = content.splitlines()
    decoded = []
    for text in lines:
        fields = text.strip().split(",", 5)
        if len(fields) == 6 and loadout_ocr.coordinate_aabb(fields[:5]):
            text = fields[5]
        text = text.replace("**", "").replace("`", "").replace("\\%", "%").replace("$", "")
        text = re.sub(r"^\s*(?:-\s+(?![\s\d])|[*•]\s+|#{1,6}\s+|\d+[.)、]\s+)", "", text)
        text = text.strip().strip("|").replace("|", ":").strip()
        suffix = re.fullmatch(
            r"([^:+\-]+?)\s*:\s*([+\-]\s*\d+(?:[.,]\d+)?\s*(?:%|秒|米|倍|x|×)?)\s*", text
        )
        if suffix:
            text = suffix[2] + " " + suffix[1].strip()
        if text and not re.fullmatch(r"[-:|\s]+", text):
            decoded.append(text)
    return decoded, obj


def _property_row(text):
    text = unicodedata.normalize("NFKC", text).strip()
    if riven_analysis.has_upgrade_preview_lines(text):
        return None
    if match := _MASTERY.fullmatch(text):
        suffix = match[2]
        if not re.search(r"[+−-]\s*\d", suffix) and not _resolve(suffix.strip()):
            return "mastery_rank", int(match[1])
    for key, pattern in _PROPERTIES.items():
        if match := pattern.fullmatch(text):
            return key, int(match[1]) if key in {"drain", "rank"} else match[1].casefold()
    return None


def has_upgrade_preview(data):
    """检查当前转录、属性与保留的原始观察，避免复读覆盖箭头证据。"""
    if not isinstance(data, dict):
        return False
    has_preview = riven_analysis.has_upgrade_preview_lines
    if any(
        has_preview(data.get(key)) for key in ("lines", "stat_lines", "drain", "capacity", "rank")
    ):
        return True
    for side in ("positive", "negative"):
        if has_preview(data.get(side)):
            return True
        if any(
            has_preview(name) or has_preview(value)
            for name, value in VisionCommands._iter_stat_pairs(data.get(side), side)
        ):
            return True
    raw = (data.get("_local_ocr") or {}).get("raw") or {}
    decoded = data.get("_line_decode") or {}
    recognition = data.get("_recognition") or {}
    recheck = recognition.get("line_recheck") or {}
    return (
        any(
            has_preview([row.get("text") for row in source.get("rows", ())])
            for source in (raw, decoded)
        )
        or has_upgrade_preview(recheck.get("original_line_read"))
        or has_upgrade_preview(recheck.get("recheck"))
    )


def _card_properties(structured, lines=()):
    fields = {}
    for line in lines:
        if found := _property_row(line):
            key, value = found
            fields.setdefault(key, set()).add(value)
    properties = {key: next(iter(values)) for key, values in fields.items() if len(values) == 1}
    for key, limit in (("drain", 23), ("mastery_rank", 40), ("rank", 8)):
        value = structured.get(key, properties.get(key))
        if type(value) is int and 0 <= value <= limit:
            properties[key] = value
        else:
            properties.pop(key, None)
    for key, values in (
        (
            "drain_color",
            ("red", "green", "white", "unknown", "红", "绿", "白", "未知", "红色", "绿色", "白色"),
        ),
        ("polarity", ("madurai", "vazarin", "naramon")),
    ):
        value = str(structured.get(key, properties.get(key)) or "").strip().casefold()
        if value in values:
            properties[key] = value
        else:
            properties.pop(key, None)
    return properties


def geometry_properties(result):
    """在本地确认的卡区内读取明确标注的属性，裸数字仍保留原观察。"""
    structured = result.get("structured") or {}
    candidate = result.get("candidate") or {}
    region = candidate.get("region")
    lines = []
    if region and len(region) == 4:
        left, top, right, bottom = region
        for row in (result.get("raw") or {}).get("rows", ()):
            quad = row.get("quad")
            if not quad or not isinstance(row.get("text"), str):
                continue
            xs, ys = zip(*quad)
            if left <= sum(xs) / len(xs) <= right and top <= sum(ys) / len(ys) <= bottom:
                lines.append(row["text"])
    return _card_properties(structured, lines)


def _resolve(name):
    return VisionCommands._stat_id_from_name(name, _REVERSE)


def _decode_lines(lines):
    return riven_analysis.decode_riven_lines(
        lines, lambda name: VisionCommands._stat_id_exact(name, _REVERSE), resolve=_resolve
    )


@dataclass
class Observation:
    data: dict | None = None
    multiple: bool = False
    accepted: Fields | None = None
    upgrade_preview: bool = False


@dataclass
class Fields:
    """业务层通过了武器、数值和完整性校验的字段。"""

    positive: list
    negative: list
    hidden: int
    weapon: str = ""

    def signature(self):
        return _fields(self.positive, self.negative)


def _fields(positive, negative):
    return Counter(
        (sid, side, abs(Decimal(str(value))))
        for side, pairs in (("positive", positive), ("negative", negative))
        for sid, value in pairs
    )


def decode_text(content):
    lines, obj = _plain_lines(content)
    if riven_analysis.has_upgrade_preview_lines(lines) or has_upgrade_preview(obj):
        return Observation(upgrade_preview=True)
    if (
        isinstance(obj, dict)
        and isinstance(obj.get("weapon"), str)
        and isinstance(obj.get("stat_lines"), list)
    ):
        title, body = obj["weapon"].strip(), obj["stat_lines"]
        properties = _card_properties(obj, (line for line in body if isinstance(line, str)))
    else:
        rows = [r["text"] for r in _decode_lines(lines)["rows"] if not r.get("structural")]
        titles = [
            i for i, line in enumerate(rows) if riven_analysis._is_riven_title(line, _resolve)
        ]
        candidates = []
        for start, end in zip(titles, [*titles[1:], len(rows)]):
            for stop in range(start + 1, end):
                footer = _property_row(rows[stop])
                if not footer or footer[0] != "mastery_rank":
                    continue
                title, body = rows[start], rows[start + 1 : stop]
                while body and re.fullmatch(r"[A-Za-z\s-]+", body[0]):
                    title += " " + body.pop(0)
                body = _decode_lines(riven_analysis.merge_polarity_lines(body))["lines"]
                # 标题与段位已界定卡面。残缺正文也要保留，供复读核对条数。
                candidates.append((title, body))
                break
        if len(candidates) != 1:
            return Observation(multiple=len(candidates) > 1)
        title, body = candidates[0]
        properties = _card_properties({}, lines)
    if not title or not all(isinstance(v, str) and v.strip() for v in body):
        return Observation()
    return Observation(
        {
            "weapon": title,
            "lines": [line for line in body if not _property_row(line)],
            **properties,
            "_line_decode": {"rows": [{"text": title}]},
            "_recognition": {"source": "single_ocr"},
        }
    )


def _geometry(content, size):
    width, height = size
    rows = []
    _, obj = _plain_lines(content)
    metadata = {}
    if isinstance(obj, dict) and isinstance(obj.get("rows"), list):
        metadata = _card_properties(obj)
        content = json.dumps(obj["rows"], ensure_ascii=False)
    for line in loadout_ocr.coordinate_text_lines(content):
        if not line.strip() or line.strip().startswith("```"):
            continue
        fields = line.strip().split(",", 5)
        rect = loadout_ocr.coordinate_aabb(fields[:5]) if len(fields) == 6 else None
        if not rect or not fields[5].strip():
            return Observation()
        x, y, h, w, _ = rect
        left, right = (x - w / 2) * width / 1000, (x + w / 2) * width / 1000
        top, bottom = (y - h / 2) * height / 1000, (y + h / 2) * height / 1000
        rows.append(
            {
                "id": len(rows),
                "text": fields[5].strip(),
                "score": None,
                "quad": [[left, top], [right, top], [right, bottom], [left, bottom]],
            }
        )
    if riven_analysis.has_upgrade_preview_lines(
        [row["text"] for row in rows]
    ) or has_upgrade_preview(obj):
        return Observation(upgrade_preview=True)
    result = analyze_raw({"width": width, "height": height, "orientation": 1, "rows": rows})
    if result.get("recognition_state") != "accepted":
        return Observation()
    structured = result["structured"]
    return Observation(
        {
            "weapon": structured["weapon"],
            "lines": structured["lines"],
            **geometry_properties(result),
            **metadata,
            "_recognition": {"source": "single_ocr_geometry"},
        }
    )


def _same_title(first, second):
    def title(obs):
        name = unicodedata.normalize("NFKC", (obs.data or {}).get("weapon", "")).strip()
        return re.sub(r"[A-Za-z-].*$", "", name).strip(" ·") or name.casefold()

    a, b = title(first), title(second)
    if not a or not b:
        return False
    if a == b:
        return True
    catalog = weapon_identity.load_catalog()
    left, right = (weapon_identity.assess(n, catalog) for n in (a, b))
    return bool(
        left["identity_usable"]
        and right["identity_usable"]
        and set(left["family_keys"]) & set(right["family_keys"])
    )


def _one_field(line):
    line = riven_analysis._trailing_to_leading(line) or line
    base, annotation, valid = riven_analysis.split_stat_annotation(line)
    if not valid or len(riven_analysis._NUM_RE.findall(base)) != 1:
        return Counter()
    polarity = riven_analysis._POL_RE.match(base)
    body = polarity[2] if polarity else base
    number = riven_analysis._MULT_RE.search(body) or riven_analysis._NUM_RE.search(body)
    suffix = re.sub(r"^\s*(?:[%米秒°]|(?:ms|m|s)\b)\s*", "", body[number.end() :], flags=re.I)
    name = (body[: number.start()] + " " + suffix).strip(" ·、:：")
    sid = VisionCommands._stat_id_exact(name, _REVERSE)
    if not sid or (annotation and sid != annotation):
        return Counter()
    # 整名已核对，再用业务解析处理极性反转、乘数与数值精度。
    pos, neg, notes = riven_analysis.parse_riven_lines([line], lambda _: sid)
    return _fields(pos, neg) if len(pos) + len(neg) == 1 and not notes else Counter()


def _observed(obs):
    fields = Counter()
    other = []
    for line in (obs.data or {}).get("lines", []):
        line = unicodedata.normalize("NFKC", line).strip()
        if riven_analysis.hidden_line_count([line]):
            continue
        found = _one_field(line)
        if not found:
            prefix = re.fullmatch(r"([^\s\d+\-−±×x])\s*([+\-−]\s*\d.*)", line)
            if prefix:
                found = _one_field(prefix[2])
        if not found and (number := riven_analysis._NUM_RE.match(line)):
            # 只记录看得见的数值和词条，极性留给通过业务校验的完整读图。
            unsigned = _one_field("+" + line)
            if unsigned:
                ((sid, _, magnitude),) = unsigned
                if magnitude == Decimal(number[0].replace(",", ".")):
                    found[sid, None, magnitude] = 1
        if found:
            fields.update(found)
        else:
            other.append(line)
    unknown = any(re.search(r"\d|[?？%％]", line) for line in other)
    return fields, bool(fields) and not unknown and (not other or sum(fields.values()) == 4)


def _hidden(obs):
    return riven_analysis.hidden_line_count((obs.data or {}).get("lines", []))


def _confirms(source, candidate):
    fields, covered = _observed(source)
    if (
        not candidate.accepted
        or not covered
        or len({sid for sid, _, _ in fields}) != fields.total()
    ):
        return False
    target = Counter(
        (sid, None if (sid, None, value) in fields else side, value)
        for sid, side, value in candidate.accepted.signature().elements()
    )
    return (
        fields == target
        and _hidden(source) == candidate.accepted.hidden
        and _same_title(source, candidate)
    )


def _select(primary, enhanced, geometry):
    if not geometry.accepted:
        return None
    target = geometry.accepted.signature()
    if primary.data and not _confirms(primary, geometry):
        return None
    if enhanced.accepted:
        if (
            enhanced.accepted.signature() != target
            or enhanced.accepted.hidden != geometry.accepted.hidden
            or not _same_title(enhanced, geometry)
        ):
            return None
        return enhanced
    elif (
        primary.data
        and not (_observed(enhanced)[0] - target)
        and (
            not enhanced.data
            or (_same_title(enhanced, geometry) and _hidden(enhanced) <= geometry.accepted.hidden)
        )
    ):
        return geometry
    return None


def _enhance(url):
    with Image.open(io.BytesIO(decode_image_data_url(url))) as image:
        gray = ImageOps.autocontrast(ImageOps.grayscale(image), cutoff=0.2)
        scale = min(3.0, 1800 / max(gray.size))
        size = tuple(max(1, round(n * scale)) for n in gray.size)
        gray = gray.resize(size, Image.Resampling.LANCZOS).filter(
            ImageFilter.UnsharpMask(radius=1.1, percent=135, threshold=2)
        )
        buf = io.BytesIO()
        gray.save(buf, "PNG")
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii"), size


async def transcribe(provider, image_url, prompt):
    """复用已配置的兼容客户端。构造图像消息以免宿主自动添入占位词。"""
    parts = [{"type": "image_url", "image_url": {"url": image_url}}]
    if prompt:
        parts.append({"type": "text", "text": prompt})
    client = provider.client.with_options(max_retries=0)
    stream = await client.chat.completions.create(
        model=provider.get_model(),
        messages=[{"role": "user", "content": parts}],
        temperature=0,
        max_tokens=2048,
        stream=True,
    )
    chunks, finish = [], None
    async with stream:
        async for packet in stream:
            for choice in packet.choices:
                if choice.index == 0:
                    if choice.delta.content:
                        chunks.append(choice.delta.content)
                    if choice.finish_reason:
                        finish = choice.finish_reason
    return "".join(chunks) if finish == "stop" else ""


async def read(provider, image_url, validate, geometry_prompt):
    """每次校验走真实业务规则；整份采纳，争议结果不估价。"""
    if provider is None:
        return None, "紫卡主读渠道不可用。"
    observations = []
    for view in ("original", "enhanced", "geometry"):
        try:
            if view == "enhanced":
                enhanced_url, size = await asyncio.to_thread(_enhance, image_url)
            url = image_url if view == "original" else enhanced_url
            prompt = geometry_prompt if view == "geometry" else ""
            text = await asyncio.wait_for(transcribe(provider, url, prompt), timeout=60)
            obs = (
                (await asyncio.to_thread(_geometry, text, size))
                if view == "geometry" and text
                else decode_text(text)
            )
        except Exception as exc:
            logger.warning("[sdjk] 紫卡单模型 OCR %s 失败：%s", view, type(exc).__name__)
            return None, "紫卡 OCR 请求或图像处理失败，请稍后再试。"
        if obs.upgrade_preview:
            return None, L(*riven_analysis.UPGRADE_PREVIEW_MESSAGE)
        if obs.multiple:
            return None, "画面中有多个完整紫卡，请仅保留要分析的一张卡。"
        if obs.data:
            validated = await validate(obs.data)
            if isinstance(validated, Fields):
                obs.accepted = validated
        observations.append(obs)
        primary = observations[0]
        if primary.accepted:
            chosen = primary
        elif view == "enhanced" and _confirms(primary, obs):
            chosen = obs
        elif view == "geometry":
            chosen = _select(*observations)
        else:
            chosen = None
        if chosen:
            chosen.data["_recognition"].update(
                views=len(observations),
                selected_view=("original", "enhanced", "geometry")[observations.index(chosen)],
            )
            return chosen.data, ""
    return (
        None,
        "紫卡识别结果待核对。多次读取未能确认同一组完整词条，请裁出单张卡或按文字格式发送。",
    )
