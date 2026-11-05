"""本地紫卡 OCR 的指令/面板/卸载集成；离线、无真实推理或下载。"""

from __future__ import annotations

import ast
import asyncio
import base64
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import local_ocr as ocr  # noqa: E402
from core.commands.riven import RivenCommands  # noqa: E402
from core.commands.vision import VisionCommands  # noqa: E402
from core.local_ocr_install import LocalOCRInstaller  # noqa: E402
from core.webpanel import WebPanel  # noqa: E402

IMAGE = "data:image/png;base64," + base64.b64encode(b"synthetic-image-boundary").decode()


class Commands(VisionCommands, RivenCommands):
    """真实mixin组合，仅替换I/O边界；渠道调用为可断言的AsyncMock。"""

    def __init__(self, manager):
        self.cfg = {}
        self.local_ocr = manager
        self._ocr_busy = set()
        self._ocr_last = {"sender": 123}
        self.client = SimpleNamespace()
        self._vision_race_json = AsyncMock(side_effect=self.cloud_result)
        self._normalize_llm_stats = Mock(side_effect=AssertionError("待确认结果不得估价"))

    @staticmethod
    async def cloud_result(*_args, task, **_kwargs):
        if task == "riven_json":
            return {"weapon": "合成武器", "positive": [["暴击", 17.25]]}
        return {"lines": ["+17.25% 暴击几率"]}

    def _riven_vision_providers(self):
        return [SimpleNamespace(id="synthetic-cloud-boundary")]

    def _cfg_str(self, _key):
        return ""

    def _event_has_image(self, _event):
        return True

    def _vision_gate(self, _event, _label, *, task="scan"):
        return None, "sender"

    async def _image_data_urls(self, _event):
        return [IMAGE]


class IntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manager = ocr.LocalRivenOCR(self.root / "data", ROOT)
        self.installer = LocalOCRInstaller(self.root / "data", ROOT)
        self.addAsyncCleanup(self.manager.close)
        self.addAsyncCleanup(self.installer.close)
        self.commands = Commands(self.manager)
        self.plugin = SimpleNamespace(local_ocr=self.manager, local_ocr_installer=self.installer)
        self.panel = WebPanel(self.plugin, "synthetic_ocr_plugin")
        self.panel._body = AsyncMock(return_value={})
        self.addCleanup(self.panel.close)
        self.spawn = patch(
            "asyncio.create_subprocess_exec", side_effect=AssertionError("禁止真实子进程/OCR")
        )
        self.spawn.start()
        self.addCleanup(self.spawn.stop)
        self.payload = {
            "engine": "rapidocr",
            "python": sys.executable,
            "model_dir": str(self.root / "models"),
            "revision": 0,
        }

    def seed_enabled(self, checked=True):
        data = {**self.manager._default(), **self.payload, "enabled": True, "check_ok": checked}
        self.manager.path.parent.mkdir(parents=True)
        self.manager.path.write_text(json.dumps(data), encoding="utf-8")

    async def post(self, method, body):
        self.panel._body.return_value = body
        return await getattr(self.panel, method)()

    async def test_disabled_preserves_both_legacy_cloud_reads(self):
        result = await self.commands._extract_riven_from_image(IMAGE)
        self.assertNotIn("_local_ocr", result)
        self.assertEqual(result["weapon"], "合成武器")
        self.assertEqual(result["lines"], ["+17.25% 暴击几率"])
        calls = self.commands._vision_race_json.await_args_list
        self.assertCountEqual(
            [call.kwargs["task"] for call in calls], ["riven_json", "riven_lines"]
        )
        self.assertFalse(self.manager.path.parent.exists())

    async def test_enabled_unchecked_and_invalid_image_never_use_cloud(self):
        self.seed_enabled(checked=None)
        result = await self.commands._extract_riven_from_image(IMAGE)
        self.assertEqual(result["_local_ocr"]["state"], "unchecked")
        result = await self.commands._extract_riven_from_image(
            "https://not-fetched.invalid/image.png"
        )
        self.assertEqual(result["_local_ocr"]["state"], "invalid_image")
        self.commands._vision_race_json.assert_not_awaited()

    async def test_corrupt_configuration_is_preserved_and_never_uses_cloud(self):
        self.manager.path.parent.mkdir(parents=True)
        self.manager.path.write_text("{broken", encoding="utf-8")
        result = await self.commands._extract_riven_from_image(IMAGE)
        self.assertEqual(result["_local_ocr"]["state"], "invalid_config")
        self.commands._vision_race_json.assert_not_awaited()
        self.assertEqual(self.manager.path.read_text(), "{broken")

    async def test_enabled_local_error_does_not_fall_back_to_cloud(self):
        self.seed_enabled()
        with patch.object(
            ocr, "resource_snapshot", return_value={"ok": False, "message": "合成资源不足"}
        ):
            result = await self.commands._extract_riven_from_image(IMAGE)
        self.assertEqual(result["_local_ocr"]["state"], "resources")
        self.commands._vision_race_json.assert_not_awaited()

    async def test_partial_riven_handler_returns_review_before_analysis_or_price(self):
        self.seed_enabled()
        observation = {
            "ok": True,
            "state": "partial",
            "recognition_state": "partial",
            "structured": {"weapon": "合成武器", "lines": ["+17.25% 暴击几率"]},
            "recognition_reasons": ["未知前缀待确认"],
        }
        with patch.object(
            self.manager, "recognize", new=AsyncMock(return_value=observation)
        ) as recognize:
            reply = await self.commands._h_riven_analysis(
                SimpleNamespace(content=[]),
                SimpleNamespace(unified_msg_origin="synthetic-origin"),
                "qq",
            )
        self.assertIn("尚未用于估价", reply.raw_text)
        self.assertIn("+17.25% 暴击几率", reply.raw_text)
        self.assertNotIn("未知前缀待确认", reply.raw_text)
        self.assertEqual(self.commands._ocr_busy, set())
        self.assertNotIn("sender", self.commands._ocr_last)
        recognize.assert_awaited_once_with(b"synthetic-image-boundary")
        self.commands._normalize_llm_stats.assert_not_called()
        self.commands._vision_race_json.assert_not_awaited()

    async def test_status_is_complete_default_off_and_has_no_startup_side_effect(self):
        status = await self.panel.riven_ocr_status()
        self.assertTrue(status["ok"])
        self.assertTrue(status["implemented"])
        self.assertFalse(status["enabled"])
        self.assertEqual(status["engine"], "")
        self.assertIsNone(status["check_ok"])
        self.assertEqual(status["revision"], 0)
        self.assertEqual(status["engines"][0]["id"], "rapidocr")
        self.assertFalse(self.manager.path.parent.exists())
        self.assertFalse((self.root / "data/riven-ocr").exists())

    async def test_save_check_enable_cas_and_save_disables_again(self):
        saved = await self.post("riven_ocr_save", self.payload)
        self.assertTrue(saved["ok"])
        self.assertFalse(saved["enabled"])
        self.assertIsNone(saved["check_ok"])
        self.assertGreater(saved["revision"], 0)
        stale = await self.post("riven_ocr_save", self.payload)
        self.assertFalse(stale["ok"])
        self.assertEqual(stale["code"], "revision_conflict")
        not_ready = await self.post(
            "riven_ocr_enable", {"enabled": True, "revision": saved["revision"]}
        )
        self.assertFalse(not_ready["ok"])
        with patch.object(
            self.manager,
            "_check_settings",
            new=AsyncMock(return_value={"ok": True, "message": "合成检查通过"}),
        ):
            checked = await self.post("riven_ocr_check", self.payload)
        self.assertTrue(checked["ok"])
        self.assertTrue(checked["check_ok"])
        self.assertEqual(checked["findings"], ["合成检查通过"])
        status = await self.panel.riven_ocr_status()
        self.assertTrue(status["check_ok"])
        self.assertFalse(status["enabled"])
        enabled = await self.post(
            "riven_ocr_enable", {"enabled": True, "revision": status["revision"]}
        )
        self.assertTrue(enabled["enabled"])
        saved_again = await self.post(
            "riven_ocr_save", {**self.payload, "revision": enabled["revision"]}
        )
        self.assertFalse(saved_again["enabled"])
        self.assertIsNone(saved_again["check_ok"])

    async def test_check_failure_is_completed_request_with_false_check_and_no_enable(self):
        await self.post("riven_ocr_save", self.payload)
        with patch.object(
            self.manager,
            "_check_settings",
            new=AsyncMock(return_value={"ok": False, "message": "合成版本不符"}),
        ):
            checked = await self.post("riven_ocr_check", self.payload)
        self.assertTrue(checked["ok"])
        self.assertFalse(checked["check_ok"])
        self.assertEqual(checked["findings"], ["合成版本不符"])
        self.assertFalse((await self.panel.riven_ocr_status())["enabled"])

    async def test_try_requires_literal_confirmation_and_valid_data_url(self):
        with patch.object(
            self.manager, "recognize", new=AsyncMock(return_value={"ok": True, "state": "partial"})
        ) as recognize:
            for confirmed in (None, False, "true", 1):
                result = await self.post(
                    "riven_ocr_try", {"confirmed": confirmed, "image_data_url": IMAGE}
                )
                self.assertEqual(result["code"], "confirmation_required")
            invalid = await self.post(
                "riven_ocr_try",
                {
                    "confirmed": True,
                    "revision": 0,
                    "image_data_url": "https://not-fetched.invalid/card",
                },
            )
            self.assertEqual(invalid["code"], "invalid_image")
            recognize.assert_not_awaited()
            result = await self.post(
                "riven_ocr_try", {"confirmed": True, "revision": 0, "image_data_url": IMAGE}
            )
            self.assertEqual(result["state"], "partial")
            recognize.assert_awaited_once_with(b"synthetic-image-boundary", expected_revision=0)

    async def test_try_requires_safe_integer_revision_before_decoding_or_recognition(self):
        with (
            patch.object(self.manager, "recognize", new=AsyncMock()) as recognize,
            patch.object(ocr, "decode_image_data_url") as decode,
        ):
            for revision in (None, False, True, -1, 1.0, "0", 2**53):
                result = await self.post(
                    "riven_ocr_try",
                    {"confirmed": True, "revision": revision, "image_data_url": IMAGE},
                )
                self.assertEqual(result["code"], "invalid_revision")
                self.assertNotIn("configuration_revision", result)
            result = await self.post("riven_ocr_try", {"confirmed": True, "image_data_url": IMAGE})
            self.assertEqual(result["code"], "invalid_revision")
            decode.assert_not_called()
            recognize.assert_not_awaited()

    async def test_try_stale_window_cannot_start_recognition_under_new_configuration(self):
        self.seed_enabled()
        stale = self.manager.status()["revision"]
        await self.manager.enable(False, stale)
        await self.manager.enable(True, self.manager.status()["revision"])
        with patch.object(ocr, "resource_snapshot") as resources:
            result = await self.post(
                "riven_ocr_try", {"confirmed": True, "revision": stale, "image_data_url": IMAGE}
            )
            resources.assert_not_called()
        self.assertFalse(result["ok"])
        self.assertEqual(result["state"], "revision_conflict")
        self.assertEqual(result["configuration_revision"], self.manager.status()["revision"])

    async def test_try_reports_manager_configuration_revision_without_relabeling(self):
        self.seed_enabled()
        revision = self.manager.status()["revision"]
        with patch.object(
            ocr, "resource_snapshot", return_value={"ok": False, "message": "合成资源不足"}
        ):
            result = await self.post(
                "riven_ocr_try", {"confirmed": True, "revision": revision, "image_data_url": IMAGE}
            )
        self.assertEqual(result["state"], "resources")
        self.assertEqual(result["configuration_revision"], revision)

    async def test_install_has_zero_work_until_explicit_confirmation_and_does_not_enable(self):
        before = await self.panel.riven_ocr_status()
        with patch.object(self.installer, "_run", new=AsyncMock()) as install_run:
            for confirmed in (None, False, "true", 1):
                rejected = await self.post("riven_ocr_install", {"confirmed": confirmed})
                self.assertFalse(rejected["accepted"])
                self.assertEqual(rejected["reason"], "confirmation_required")
            idle = await self.panel.riven_ocr_install_status()
            self.assertFalse(idle["running"])
            install_run.assert_not_awaited()
            self.assertFalse(self.installer.root.exists())
            with (
                patch.object(self.installer, "_supported", return_value=True),
                patch.object(self.installer, "_manifest", return_value=({}, [], "synthetic-sha")),
            ):
                accepted = await self.post(
                    "riven_ocr_install", {"confirmed": True, "job_id": idle["job_id"]}
                )
            self.assertTrue(accepted["accepted"])
            await self.installer._task
            install_run.assert_awaited_once_with({}, [], "synthetic-sha")
        after = await self.panel.riven_ocr_status()
        self.assertEqual(before, after)
        self.assertFalse(self.manager.path.parent.exists())

    async def test_install_requires_current_job_id_before_any_install_work(self):
        before = await self.panel.riven_ocr_status()
        idle = await self.panel.riven_ocr_install_status()
        self.assertEqual(idle, await self.panel.riven_ocr_install_status())
        with (
            patch.object(self.installer, "_supported") as supported,
            patch.object(self.installer, "_manifest") as manifest,
            patch.object(self.installer, "_run", new=AsyncMock()) as run,
        ):
            for job_id in (None, "", False, True, 1, [], {}, "old-window"):
                result = await self.post("riven_ocr_install", {"confirmed": True, "job_id": job_id})
                self.assertFalse(result["accepted"])
                self.assertEqual(result["reason"], "job_conflict")
                self.assertEqual(result["job_id"], idle["job_id"])
            missing = await self.post("riven_ocr_install", {"confirmed": True})
            self.assertEqual(missing["reason"], "job_conflict")
            supported.assert_not_called()
            manifest.assert_not_called()
            run.assert_not_awaited()
        self.assertIsNone(self.installer._task)
        self.assertFalse(self.installer.root.exists())
        self.assertEqual(before, await self.panel.riven_ocr_status())

    async def test_install_stale_window_cannot_restart_after_other_window_finishes(self):
        before = await self.panel.riven_ocr_status()
        seen = (await self.panel.riven_ocr_install_status())["job_id"]
        with (
            patch.object(self.installer, "_supported", return_value=True),
            patch.object(self.installer, "_manifest", return_value=({}, [], "synthetic-sha")),
            patch.object(self.installer, "_asset", new=AsyncMock()) as asset,
            patch.object(self.installer, "_runtime", new=AsyncMock()) as runtime,
        ):
            first = await self.post("riven_ocr_install", {"confirmed": True, "job_id": seen})
            self.assertTrue(first["accepted"])
            self.assertNotEqual(first["job_id"], seen)
            first_task = self.installer._task
            await first_task
            done = await self.panel.riven_ocr_install_status()
            self.assertEqual(done["state"], "succeeded")
            self.assertFalse(done["running"])
            self.assertEqual(done["job_id"], first["job_id"])
            stale = await self.post("riven_ocr_install", {"confirmed": True, "job_id": seen})
            self.assertFalse(stale["accepted"])
            self.assertEqual(stale["reason"], "job_conflict")
            self.assertEqual(stale["job_id"], first["job_id"])
            self.assertEqual(stale["state"], "succeeded")
            self.assertIs(self.installer._task, first_task)
            runtime.assert_awaited_once_with({}, "synthetic-sha")
            fresh = await self.post(
                "riven_ocr_install", {"confirmed": True, "job_id": done["job_id"]}
            )
            self.assertTrue(fresh["accepted"])
            self.assertNotEqual(fresh["job_id"], done["job_id"])
            await self.installer._task
            self.assertEqual(runtime.await_count, 2)
            asset.assert_not_awaited()
        self.assertEqual(before, await self.panel.riven_ocr_status())
        self.assertFalse(self.manager.path.exists())
        self.commands._vision_race_json.assert_not_awaited()
        self.commands._normalize_llm_stats.assert_not_called()

    async def test_registered_ocr_routes_and_closed_panel_reject_all_actions(self):
        names = ("status", "check", "save", "enable", "try", "install", "install_status")
        routes = {
            path.rsplit("/", 1)[-1]: methods
            for path, _method, methods, _label in self.panel.routes()
        }
        for name in names:
            self.assertEqual(
                routes["riven_ocr_" + name],
                ["GET" if name in ("status", "install_status") else "POST"],
            )
        self.panel.close()
        with (
            patch.object(self.manager, "status", side_effect=AssertionError("停用后不读服务")),
            patch.object(self.installer, "status", side_effect=AssertionError("停用后不读安装器")),
        ):
            for name in names:
                self.panel._body.reset_mock()
                result = await getattr(self.panel, "riven_ocr_" + name)()
                self.assertFalse(result["ok"])
                self.assertFalse(result["implemented"])
                self.assertEqual(result["code"], "unavailable")
                self.panel._body.assert_not_awaited()

    async def test_real_main_terminate_method_closes_ocr_and_installer_once(self):
        # 与已有卸载回归同法提取真实方法，免去伪造整套AstrBot运行时。
        source = ROOT / "main.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        method = next(
            node
            for cls in tree.body
            if isinstance(cls, ast.ClassDef)
            for node in cls.body
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "terminate"
        )
        cls = ast.ClassDef(name="Host", bases=[], keywords=[], body=[method], decorator_list=[])
        module = ast.fix_missing_locations(ast.Module(body=[cls], type_ignores=[]))
        scope = {"asyncio": asyncio, "logger": Mock()}
        exec(compile(module, str(source), "exec"), scope)
        host = scope["Host"]()
        host.client = SimpleNamespace(begin_close=Mock(), close=AsyncMock())
        host.push = SimpleNamespace(stop=AsyncMock())
        host.local_ocr = self.manager
        host.local_ocr_installer = self.installer
        host._panel = self.panel
        with (
            patch.object(
                self.manager, "close", new=AsyncMock(wraps=self.manager.close)
            ) as stop_ocr,
            patch.object(
                self.installer, "close", new=AsyncMock(wraps=self.installer.close)
            ) as stop_install,
        ):
            await asyncio.gather(host.terminate(), host.terminate())
            stop_ocr.assert_awaited_once()
            stop_install.assert_awaited_once()
        self.assertTrue(self.manager._closed)
        self.assertTrue(self.installer._closed)
        self.assertTrue(self.panel.closed)
        host.client.close.assert_awaited_once()
        host.push.stop.assert_awaited_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
