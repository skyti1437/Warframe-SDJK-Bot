"""同图提示词串行对比；采信仅表示任务结构通过，不表示识别准确。"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import io
import re
import time
import uuid
import warnings

from PIL import Image

from . import loadout_ocr as lo
from .commands.scan import ScanCommands
from .commands.vision import VisionCommands, _cancel_vision_tasks, original_vision_prompt
from .vision_prompts import MAX_PROMPT_LENGTH

MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_RESPONSE_CHARS = 20000


def _decode_image(data: str) -> tuple[bytes, int, int]:
    if not isinstance(data, str):
        raise ValueError("图片必须为 PNG/JPEG data URL")
    match = re.fullmatch(r"data:image/(png|jpeg);base64,([A-Za-z0-9+/=]+)", data)
    if not match or len(match[2]) > ((MAX_IMAGE_BYTES + 2) // 3) * 4:
        raise ValueError("图片格式或大小不符合要求")
    try:
        raw = base64.b64decode(match[2], validate=True)
        if len(raw) > MAX_IMAGE_BYTES:
            raise ValueError("图片超过 8MB")
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as image:
                width, height = image.size
                if (
                    image.format != {"png": "PNG", "jpeg": "JPEG"}[match[1]]
                    or getattr(image, "n_frames", 1) != 1
                    or width > 10000
                    or height > 10000
                    or width * height > 36000000
                ):
                    raise ValueError("图片格式、帧数或尺寸不符合要求")
                image.verify()
        return raw, width, height
    except (
        binascii.Error,
        OSError,
        SyntaxError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as exc:
        raise ValueError("无法校验图片") from exc


def prepare_image(image_data: str, task: str) -> dict:
    """验证原图，按生产任务预处理一次并冻结；image_url 仅供内部发送。"""
    original_vision_prompt(task)  # 同生产任务白名单。
    raw, input_width, input_height = _decode_image(image_data)
    image_url = image_data
    if task.startswith("scan_"):
        # 预估生产缩放及聚焦上限，先拦截长条图，避免先分配超大位图。
        scale = min(1280 / input_width, 4.0) if input_width < 1600 else 1600 / input_width
        fit_width = max(1, int(input_width * scale))
        fit_height = max(1, int(input_height * scale))
        if max(fit_width, fit_height) > 10000 or fit_width * fit_height > 36000000:
            raise ValueError("预处理尺寸超过上限")
        if task in ("scan_mods", "scan_panel", "scan_arcanes"):
            fraction = {"scan_mods": 0.72, "scan_panel": 0.28, "scan_arcanes": 0.20}[task]
            crop_width = max(1, int(fit_width * fraction))
            focus_scale = max(1, min(1280 / crop_width, 4.0))
            if (
                fit_height * focus_scale > 10000
                or crop_width * fit_height * focus_scale**2 > 36000000
            ):
                raise ValueError("聚焦尺寸超过上限")
        image_url = ScanCommands._fit_scan_image(image_url)
        if task in ("scan_mods", "scan_panel", "scan_arcanes"):
            image_url = ScanCommands._scan_focus_image(image_url, task.removeprefix("scan_"))
    prepared, width, height = _decode_image(image_url)
    return {
        "task": task,
        "image_url": image_url,
        "input_sha256": hashlib.sha256(raw).hexdigest(),
        "prepared_sha256": hashlib.sha256(prepared).hexdigest(),
        "input_width": input_width,
        "input_height": input_height,
        "width": width,
        "height": height,
    }


def _parse(task: str, text: str) -> tuple[dict, bool, list[str]]:
    if task == "scan_title":
        line = text.strip().splitlines()[0].strip().strip("「」\"'`") if text.strip() else ""
        accepted = bool(line) and len(line) <= 40
        return {"weapon": line}, accepted, [] if accepted else ["weapon"]
    if task == "riven_lines":
        parsed = VisionCommands._parse_riven_lines_text(text)
        return parsed, bool(parsed), [] if parsed else ["lines"]
    if task in ("scan_mods", "scan_panel", "scan_arcanes"):
        key = task.removeprefix("scan_")
        parsed = lo.parse_focus_text(text, key) or {}
        accepted = bool(parsed.get(key))
        return parsed, accepted, [] if accepted else [key]
    parsed = lo.parse_vision_json(text) or {}
    keys = (
        ("weapon", "mods")
        if task == "scan_main"
        else ("weapon", "mods", "panel", "positive", "negative", "lines")
    )
    accepted = any(parsed.get(key) for key in keys)
    expected = (
        ("weapon", "capacity", "mods", "panel", "arcanes")
        if task == "scan_main"
        else ("weapon", "positive", "negative")
    )
    missing = [
        key for key in expected if key not in parsed or (key != "negative" and not parsed[key])
    ]
    return parsed, accepted, missing


def _usage(response) -> dict:
    """只提取已知计数字段，不透出 SDK 原始对象。"""
    usage = getattr(response, "usage", None)
    result = {}
    for key in (
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "input_tokens",
        "output_tokens",
    ):
        value = usage.get(key) if isinstance(usage, dict) else getattr(usage, key, None)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            result[key] = value
    return result


async def run_comparison(
    provider,
    task: str,
    prepared: dict,
    original: str,
    candidate: str,
    *,
    allowed=lambda: True,
    timeout: float = 75,
    _delay_seconds: float = 3,
) -> dict:
    """每侧一次方法调用，无重试、无共享会话；取消沿调用栈传播并回收请求。"""
    original_vision_prompt(task)
    if prepared.get("task") != task:
        raise ValueError("图片预处理任务不匹配")
    if timeout <= 0 or _delay_seconds < 0:
        raise ValueError("超时和间隔不符合要求")
    raw, _, _ = _decode_image(prepared["image_url"])
    if hashlib.sha256(raw).hexdigest() != prepared.get("prepared_sha256"):
        raise ValueError("冻结图片校验失败")
    if any(
        not isinstance(prompt, str) or not prompt.strip() or len(prompt) > MAX_PROMPT_LENGTH
        for prompt in (original, candidate)
    ):
        raise ValueError("提示词必须为非空文本且不超过长度上限")
    sides = {}
    for index, (name, prompt) in enumerate((("original", original), ("candidate", candidate))):
        if not isinstance(prompt, str):
            raise ValueError("提示词必须是文本")
        if index and sides["original"]["provider_method_invoked"]:
            await asyncio.sleep(_delay_seconds)
        side = {
            "raw_text": "",
            "parsed": {},
            "accepted": False,
            "parse_reason": "未发送",
            "missing_fields": [],
            "elapsed_ms": 0,
            "prompt_hash": hashlib.sha256(prompt.encode()).hexdigest(),
            "usage": {},
            "attempted_unit": "provider_text_chat_call",
            "attempted": 0,
            "sent_semantics": "provider_method_invoked",
            "sent": False,
            "provider_method_invoked": 0,
            "actual_http_requests": 0,
            "truncated": False,
        }
        sides[name] = side
        if not allowed():
            continue
        start = time.perf_counter()
        try:
            side.update(
                attempted=1, sent=True, provider_method_invoked=1, actual_http_requests=None
            )
            request = asyncio.create_task(
                provider.text_chat(
                    prompt=prompt,
                    session_id=f"sdjk-compare-{uuid.uuid4().hex}",
                    image_urls=[prepared["image_url"]],
                )
            )
            try:
                response = await asyncio.wait_for(asyncio.shield(request), timeout=timeout)
            finally:
                await _cancel_vision_tasks([request])
            side["usage"] = _usage(response)
            text = str(getattr(response, "completion_text", "") or "").strip()
            side["truncated"] = len(text) > MAX_RESPONSE_CHARS
            side["raw_text"] = text[:MAX_RESPONSE_CHARS]
            if side["truncated"]:
                side["parse_reason"] = "响应超过长度上限，未解析"
            elif not text:
                side["parse_reason"] = "空响应"
            else:
                try:
                    parsed, accepted, missing = _parse(task, text)
                    side.update(
                        parsed=parsed,
                        accepted=accepted,
                        missing_fields=missing,
                        parse_reason="任务结构通过" if accepted else "任务结构未通过",
                    )
                except Exception:  # noqa: BLE001 —— 解析错误不泄露响应外的上下文。
                    side["parse_reason"] = "解析失败"
        except asyncio.TimeoutError:
            side["parse_reason"] = "调用超时"
        except Exception:  # noqa: BLE001 —— 不返回 SDK 异常，避免密钥或端点泄露。
            side["parse_reason"] = "渠道调用失败"
        finally:
            side["elapsed_ms"] = round((time.perf_counter() - start) * 1000, 2)
    attempts = sum(side["provider_method_invoked"] for side in sides.values())
    return {
        "ok": True,
        "completed": all(
            side["provider_method_invoked"]
            and side["parse_reason"] not in ("调用超时", "渠道调用失败")
            for side in sides.values()
        ),
        "task": task,
        "sides": sides,
        "input_sha256": prepared["input_sha256"],
        "prepared_sha256": prepared["prepared_sha256"],
        "attempted_unit": "provider_text_chat_call",
        "attempted": attempts,
        "sent_semantics": "provider_method_invoked",
        "sent": bool(attempts),
        "provider_method_invoked": attempts,
        "actual_http_requests": None if attempts else 0,
        "validation_scope": "仅验证任务结构，不验证完整配卡、DPS 或识别准确性，不评选优胜",
    }
