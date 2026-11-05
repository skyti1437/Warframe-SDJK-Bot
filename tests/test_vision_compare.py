"""同图提示词比较的离线隔离测试；合成图只验证协议，不代表识别质量。"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from core import vision_compare as vc  # noqa: E402
from core.commands.scan import ScanCommands  # noqa: E402
from core.commands.vision import VisionCommands  # noqa: E402
from core import loadout_ocr as lo  # noqa: E402


def image_url(fmt="PNG", size=(320, 160)):
    image = Image.new("RGB", size, (30, 80, 120))
    for x in range(size[0]):
        image.putpixel((x, x % size[1]), (x % 256, 100, 200))
    out = io.BytesIO()
    image.save(out, fmt)
    return f"data:image/{fmt.lower()};base64," + base64.b64encode(out.getvalue()).decode()


class Provider:
    def __init__(self, answers):
        self.answers = iter(answers)
        self.calls = []

    async def text_chat(self, **kwargs):
        self.calls.append(kwargs)
        value = next(self.answers)
        if isinstance(value, BaseException):
            raise value
        return types.SimpleNamespace(completion_text=value)


SAMPLES = {
    "riven_json": '{"weapon":"绝路","positive":[["暴击",100],["基伤",120]],"negative":[]}',
    "riven_lines": "+100% 暴击几率\n+120% 伤害",
    "scan_main": '{"weapon":"绝路","capacity":60,"mods":[{"name":"膛线","drain":14}],"panel":{"暴击几率":"30%"},"arcanes":[]}',
    "scan_title": "绝路",
    "scan_panel": "暴击几率 30%\n暴击倍率 2.0x",
    "scan_mods": "膛线 | 14",
    "scan_arcanes": "主要·无情",
}


class ComparisonTests(unittest.IsolatedAsyncioTestCase):
    async def compare(self, task="scan_title", answers=None, **kwargs):
        provider = Provider(answers or [SAMPLES[task], SAMPLES[task]])
        prepared = vc.prepare_image(image_url(), task)
        result = await vc.run_comparison(
            provider, task, prepared, " 原版\n提示词 ", "候选\n提示词", _delay_seconds=0, **kwargs
        )
        return provider, prepared, result

    async def test_identical_bytes_frozen_prompts_and_isolated_sessions(self):
        provider, prepared, result = await self.compare()
        self.assertEqual(len(provider.calls), 2)
        self.assertEqual(result["attempted_unit"], "provider_text_chat_call")
        self.assertEqual(result["attempted"], 2)
        self.assertEqual(result["sent_semantics"], "provider_method_invoked")
        self.assertTrue(result["sent"])
        self.assertIsNone(result["actual_http_requests"])
        for side in result["sides"].values():
            self.assertEqual(side["attempted"], 1)
            self.assertEqual(side["provider_method_invoked"], 1)
            self.assertIsNone(side["actual_http_requests"])
        a, b = provider.calls
        self.assertEqual(a["prompt"], " 原版\n提示词 ")
        self.assertEqual(b["prompt"], "候选\n提示词")
        self.assertEqual(a["image_urls"], [prepared["image_url"]])
        self.assertEqual(a["image_urls"], b["image_urls"])
        self.assertNotEqual(a["session_id"], b["session_id"])
        self.assertEqual(set(a), {"prompt", "image_urls", "session_id"})
        self.assertNotIn("contexts", a)
        for key, prompt in (("original", a["prompt"]), ("candidate", b["prompt"])):
            self.assertEqual(
                result["sides"][key]["prompt_hash"], hashlib.sha256(prompt.encode()).hexdigest()
            )

    async def test_seven_tasks_use_existing_parser(self):
        for task in SAMPLES:
            with self.subTest(task=task):
                _, _, result = await self.compare(task)
                side = result["sides"]["original"]
                if task == "riven_json" or task == "scan_main":
                    expected = lo.parse_vision_json(SAMPLES[task])
                elif task == "riven_lines":
                    expected = VisionCommands._parse_riven_lines_text(SAMPLES[task])
                elif task == "scan_title":
                    expected = SAMPLES[task]
                else:
                    expected = lo.parse_focus_text(SAMPLES[task], task.removeprefix("scan_"))
                self.assertTrue(expected)
                self.assertEqual(
                    side["parsed"], {"weapon": expected} if task == "scan_title" else expected
                )
                self.assertTrue(side["accepted"], side)

    async def test_failures_do_not_leak_exception_details(self):
        detail = "上游测试异常详情"
        _, _, result = await self.compare(answers=[RuntimeError(detail), "绝路"])
        self.assertNotIn(detail, json.dumps(result, ensure_ascii=False))
        self.assertFalse(result["sides"]["original"]["accepted"])
        self.assertTrue(result["sides"]["candidate"]["accepted"])

    async def test_timeout_and_usage_are_bounded(self):
        _, _, result = await self.compare(answers=[asyncio.TimeoutError(), "绝路"])
        self.assertEqual(result["sides"]["original"]["parse_reason"], "调用超时")
        self.assertFalse(result["completed"])
        response = types.SimpleNamespace(
            usage={
                "total_tokens": 7,
                "prompt_tokens": True,
                "api_key": "secret",
                "completion_tokens": -1,
            }
        )
        self.assertEqual(vc._usage(response), {"total_tokens": 7})

    async def test_comparison_does_not_read_prompt_store(self):
        from core.vision_prompts import VisionPromptStore

        with patch.object(
            VisionPromptStore, "_read", side_effect=AssertionError("不应读取已保存提示词")
        ):
            await self.compare()

    async def test_empty_response_rejected(self):
        _, _, result = await self.compare(answers=["", "\n "])
        self.assertFalse(result["sides"]["original"]["accepted"])
        self.assertFalse(result["sides"]["candidate"]["accepted"])

    async def test_invalid_json_shape_rejected(self):
        _, _, result = await self.compare("riven_json", ["[]", '{"weapon": []}'])
        self.assertFalse(result["sides"]["original"]["accepted"])
        self.assertFalse(result["sides"]["candidate"]["accepted"])

    async def test_long_response_not_parsed(self):
        _, _, result = await self.compare(answers=["字" * (vc.MAX_RESPONSE_CHARS + 1), "绝路"])
        side = result["sides"]["original"]
        self.assertTrue(side["truncated"])
        self.assertEqual(len(side["raw_text"]), vc.MAX_RESPONSE_CHARS)
        self.assertFalse(side["accepted"])
        self.assertEqual(side["parsed"], {})

    async def test_default_three_second_serial_interval(self):
        provider = Provider(["绝路", "绝路"])
        prepared = vc.prepare_image(image_url(), "scan_title")

        async def gap(seconds):
            self.assertEqual(seconds, 3)
            self.assertEqual(len(provider.calls), 1)

        with patch.object(vc.asyncio, "sleep", side_effect=gap) as sleep:
            await vc.run_comparison(provider, "scan_title", prepared, "原", "候")
        sleep.assert_awaited_once_with(3)
        self.assertEqual(len(provider.calls), 2)

    async def test_known_title_parser_limitation_disclosed(self):
        # 生产标题parser只验证短非空文本；数字也会结构通过，不应冒充识别准确。
        _, _, result = await self.compare(answers=["12345", "绝路"])
        self.assertTrue(result["sides"]["original"]["accepted"])
        self.assertIn("不验证", result["validation_scope"])

    async def test_frozen_image_tampering_rejected(self):
        prepared = vc.prepare_image(image_url(), "scan_title")
        prepared["prepared_sha256"] = "0" * 64
        provider = Provider([])
        with self.assertRaises(ValueError):
            await vc.run_comparison(provider, "scan_title", prepared, "原", "候")
        self.assertEqual(provider.calls, [])

    async def test_stopped_before_call(self):
        provider, _, result = await self.compare(allowed=lambda: False)
        self.assertEqual(provider.calls, [])
        self.assertEqual(result["attempted"], 0)
        self.assertFalse(result["sent"])
        self.assertEqual(result["actual_http_requests"], 0)
        self.assertFalse(result["sides"]["original"]["provider_method_invoked"])

    async def test_stop_between_sides(self):
        provider = Provider(["绝路", "绝路"])
        prepared = vc.prepare_image(image_url(), "scan_title")
        await vc.run_comparison(
            provider,
            "scan_title",
            prepared,
            "原",
            "候",
            allowed=lambda: not provider.calls,
            _delay_seconds=0,
        )
        self.assertEqual(len(provider.calls), 1)

    async def test_repeated_cancel_waits_for_provider_cleanup(self):
        entered, cleanup, release, finished = (asyncio.Event() for _ in range(4))

        class Waiting(Provider):
            async def text_chat(self, **kwargs):
                self.calls.append(kwargs)
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cleanup.set()
                    await release.wait()
                    finished.set()

        provider = Waiting([])
        prepared = vc.prepare_image(image_url(), "scan_title")
        task = asyncio.create_task(
            vc.run_comparison(provider, "scan_title", prepared, "原", "候", _delay_seconds=0)
        )
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        await asyncio.wait_for(cleanup.wait(), 1)
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        self.assertTrue(finished.is_set())
        self.assertEqual(len(provider.calls), 1)

    async def test_cancel_never_calls_second_side(self):
        entered = asyncio.Event()

        class Waiting(Provider):
            async def text_chat(self, **kwargs):
                self.calls.append(kwargs)
                entered.set()
                await asyncio.Event().wait()

        provider = Waiting([])
        prepared = vc.prepare_image(image_url(), "scan_title")
        task = asyncio.create_task(
            vc.run_comparison(provider, "scan_title", prepared, "原", "候", _delay_seconds=0)
        )
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(len(provider.calls), 1)


class ImageTests(unittest.TestCase):
    def test_preprocessing_hashes_and_real_crop(self):
        source = image_url()
        for task in SAMPLES:
            with self.subTest(task=task):
                prepared = vc.prepare_image(source, task)
                raw = base64.b64decode(prepared["image_url"].split(",", 1)[1])
                self.assertEqual(prepared["prepared_sha256"], hashlib.sha256(raw).hexdigest())
                self.assertEqual((prepared["input_width"], prepared["input_height"]), (320, 160))
                if task in ("scan_panel", "scan_mods", "scan_arcanes"):
                    self.assertEqual(
                        prepared["image_url"],
                        ScanCommands._scan_focus_image(
                            ScanCommands._fit_scan_image(source), task.removeprefix("scan_")
                        ),
                    )

    def test_reject_untrusted_image_inputs(self):
        for source in [
            "https://example.com/x.png",
            "file:///tmp/x.png",
            "data:image/png;base64,%%%",
            "data:image/png;base64," + base64.b64encode(b"not a png").decode(),
            "",
            None,
        ]:
            with self.subTest(source=source):
                with self.assertRaises(ValueError):
                    vc.prepare_image(source, "scan_title")

    def test_format_mismatch_multiframe_and_dimensions(self):
        png = image_url()
        with self.assertRaises(ValueError):
            vc.prepare_image(png.replace("image/png", "image/jpeg"), "scan_title")
        frames = [Image.new("RGB", (8, 8), color) for color in ("red", "blue")]
        out = io.BytesIO()
        frames[0].save(out, "PNG", save_all=True, append_images=frames[1:])
        apng = "data:image/png;base64," + base64.b64encode(out.getvalue()).decode()
        with self.assertRaises(ValueError):
            vc.prepare_image(apng, "riven_json")
        with self.assertRaises(ValueError):
            vc.prepare_image(image_url(size=(10001, 1)), "riven_json")
        with self.assertRaises(ValueError):
            vc.prepare_image(image_url(size=(1, 3000)), "scan_main")
        with self.assertRaises(ValueError):
            vc.prepare_image(image_url(size=(6001, 6000)), "riven_json")
        with patch.object(vc, "MAX_IMAGE_BYTES", 20):
            with self.assertRaises(ValueError):
                vc.prepare_image(png, "riven_json")
        prepared = vc.prepare_image(image_url("JPEG"), "riven_json")
        self.assertEqual(prepared["width"], 320)

    def test_unknown_task(self):
        with self.assertRaises(ValueError):
            vc.prepare_image(image_url(), "scan_unknown")


if __name__ == "__main__":
    unittest.main(verbosity=2)
