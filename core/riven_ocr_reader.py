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
from .local_ocr import decode_image_data_url
from .local_riven_ocr import analyze_raw, weapon_identity
from .logging_compat import logger
from .parser import RIVEN_STAT_ZH

_REVERSE = {name: sid for sid, name in RIVEN_STAT_ZH.items()}


def _plain_lines(content):
    """保留原文数值和符号，只去除排版与明确声明的坐标列。"""
    content = unicodedata.normalize("NFKC", content).replace("−", "-")
    cleaned = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", content).strip()
    try:
        obj = json.loads(cleaned)
    except ValueError:
        obj = None
    if isinstance(obj, list):
        return loadout_ocr.coordinate_text_lines(content), None
    lines = []
    for text in content.splitlines():
        fields = text.strip().split(",", 5)
        if len(fields) == 6 and loadout_ocr.coordinate_aabb(fields[:5]):
            text = fields[5]
        text = text.replace("**", "").replace("`", "").replace("\\%", "%").replace("$", "")
        text = re.sub(r"^\s*(?:[-*•]\s+|#{1,6}\s+|\d+[.)、]\s+)", "", text)
        text = text.strip().strip("|").replace("|", ":").strip()
        suffix = re.fullmatch(
            r"([^:+\-]+?)\s*:\s*([+\-]\s*\d+(?:[.,]\d+)?\s*(?:%|秒|米|倍|x|×)?)\s*", text
        )
        if suffix:
            text = suffix[2] + " " + suffix[1].strip()
        if text and not re.fullmatch(r"[-:|\s]+", text):
            lines.append(text)
    return lines, obj


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


@dataclass
class Fields:
    """业务层通过了武器、数值和完整性校验的字段。"""

    positive: list
    negative: list
    hidden: int

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
    if (
        isinstance(obj, dict)
        and isinstance(obj.get("weapon"), str)
        and isinstance(obj.get("stat_lines"), list)
    ):
        title, body = obj["weapon"].strip(), obj["stat_lines"]
    else:
        rows = [r["text"] for r in _decode_lines(lines)["rows"] if not r.get("structural")]
        titles = [
            i for i, line in enumerate(rows) if riven_analysis._is_riven_title(line, _resolve)
        ]
        candidates = []
        for start, end in zip(titles, [*titles[1:], len(rows)]):
            for stop in range(start + 1, end):
                footer = re.fullmatch(r"(?:段位|精通段位|等级需求)\s*[:：]?\s*\d+(.*)", rows[stop])
                if not footer or re.search(r"[+−-]\s*\d", footer[1]) or _resolve(footer[1].strip()):
                    continue
                title, body = rows[start], rows[start + 1 : stop]
                while body and re.fullmatch(r"[A-Za-z\s-]+", body[0]):
                    title += " " + body.pop(0)
                body = _decode_lines(riven_analysis.merge_polarity_lines(body))["lines"]
                positive, negative, _, _ = VisionCommands._riven_lines_legal(body)
                if len(positive) + len(negative) >= 2:
                    candidates.append((title, body))
                break
        if len(candidates) != 1:
            return Observation(multiple=len(candidates) > 1)
        title, body = candidates[0]
    if not title or not body or not all(isinstance(v, str) and v.strip() for v in body):
        return Observation()
    return Observation(
        {
            "weapon": title,
            "lines": body,
            "_line_decode": {"rows": [{"text": title}]},
            "_recognition": {"source": "single_ocr"},
        }
    )


def _geometry(content, size):
    width, height = size
    rows = []
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
    result = analyze_raw({"width": width, "height": height, "orientation": 1, "rows": rows})
    if result.get("recognition_state") != "accepted":
        return Observation()
    structured = result["structured"]
    return Observation(
        {
            "weapon": structured["weapon"],
            "lines": structured["lines"],
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
    pos, neg, _, notes = VisionCommands._riven_lines_legal([line])
    return _fields(pos, neg) if len(pos) + len(neg) == 1 and not notes else Counter()


def _observed(obs):
    fields = Counter()
    other = []
    for line in (obs.data or {}).get("lines", []):
        if riven_analysis.hidden_line_count([line]):
            continue
        found = _one_field(line)
        if not found:
            prefix = re.fullmatch(r"\s*([^\s+\-−±×x])\s*([+\-−]\s*\d.*)", line)
            if prefix:
                found = _one_field(prefix[2])
        if found:
            fields.update(found)
        else:
            other.append(line)
    unknown = any(re.search(r"[+−\-±×x]\s*\d|[?？%％]", line) for line in other)
    return fields, bool(fields) and not unknown and (not other or sum(fields.values()) == 4)


def _hidden(obs):
    return riven_analysis.hidden_line_count((obs.data or {}).get("lines", []))


def _confirms(source, candidate):
    fields, covered = _observed(source)
    return bool(
        candidate.accepted
        and covered
        and fields == candidate.accepted.signature()
        and _hidden(source) == candidate.accepted.hidden
        and _same_title(source, candidate)
    )


def _sign_only(raw, target):
    if not raw or len({sid for sid, _, _ in raw}) != sum(raw.values()):
        return False

    def magnitudes(fields):
        return Counter((sid, value) for sid, _, value in fields.elements())

    npos = sum(n for (_, side, _), n in raw.items() if side == "positive")
    return (
        not riven_analysis.count_candidates(npos, sum(raw.values()) - npos, 0)
        and magnitudes(raw) == magnitudes(target)
        and raw != target
    )


def _select(primary, enhanced, geometry):
    if not geometry.accepted:
        return None
    target = geometry.accepted.signature()
    hints, covered = _observed(primary)
    if primary.data and (
        not _same_title(primary, geometry) or _hidden(primary) > geometry.accepted.hidden
    ):
        return None
    if enhanced.accepted:
        if (
            enhanced.accepted.signature() != target
            or enhanced.accepted.hidden != geometry.accepted.hidden
            or not _same_title(enhanced, geometry)
        ):
            return None
        if not (hints - target) or (covered and _sign_only(hints, target)):
            return enhanced
    elif (
        _confirms(primary, geometry)
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
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode(
            "ascii"
        ), image.size


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
            url = enhanced_url if view == "enhanced" else image_url
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
