"""视觉隔离迁移真实面板/selector回归；零网络、零模型调用、零真实配置文件。"""

from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import webpanel as W  # noqa: E402
from core.commands.scan import ScanCommands  # noqa: E402
from core.commands.vision import VisionCommands  # noqa: E402

LEGACY = "vision_provider_id"
RIVEN = "vision_provider_id_riven"
LINE = "vision_provider_id_line"
SCAN = "vision_provider_id_scan"
FLAG = "vision_routes_isolated"


class Config(dict):
    def __init__(self, values):
        super().__init__(values)
        self.save_calls = 0
        self.persisted = dict(values)
        self.fail_save = False

    def save_config(self):
        self.save_calls += 1
        if self.fail_save:
            raise OSError("合成保存失败")
        self.persisted = dict(self)


class Provider:
    def __init__(self, pid):
        self.pid = pid

    def meta(self):
        return SimpleNamespace(id=self.pid)

    async def text_chat(self, **_kwargs):
        raise AssertionError("迁移不得调用模型")


class Context:
    def __init__(self):
        self.providers = {
            name: Provider(name)
            for name in ("legacy-A", "riven-B", "line-C", "scan-D", "automatic-vision")
        }
        self.lookups = []
        self.enumerations = 0

    def get_provider_by_id(self, pid):
        self.lookups.append(pid)
        return self.providers.get(pid)

    def get_all_providers(self):
        self.enumerations += 1
        raise AssertionError("迁移不得枚举并猜填渠道")


class Plugin(VisionCommands, ScanCommands):
    def __init__(self, values):
        self._config = Config(values)
        self.cfg = dict(values)
        self.context = Context()
        self._ocr_busy = set()
        self._ocr_last = {}


class MigrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.host = Plugin({LEGACY: "legacy-A", RIVEN: "riven-B", LINE: "", SCAN: "", FLAG: False})
        self.panel = W.WebPanel(self.host, "synthetic_migration")
        self.panel._body = AsyncMock(return_value={})
        self.addCleanup(self.panel.close)

    async def post(self, method, body):
        self.panel._body.return_value = body
        return await getattr(self.panel, method)()

    async def prepare(self, operation="migrate"):
        return await self.post("vision_migration_prepare", {"operation": operation})

    async def apply(self, prepared, **changes):
        body = {"confirmed": True, "token": prepared["token"], "revision": prepared["revision"]}
        body.update(changes)
        return await self.post("vision_migration_apply", body)

    def assert_untouched(self, before):
        self.assertEqual(dict(self.host._config), before)
        self.assertEqual(self.host.cfg, before)
        self.assertEqual(self.host._config.persisted, before)
        self.assertEqual(self.host._config.save_calls, 0)

    async def test_status_refresh_prepare_are_readonly_and_routes_registered(self):
        before = dict(self.host.cfg)
        status1 = await self.panel.vision_migration_status()
        status2 = await self.panel.vision_migration_status()
        prepared = await self.prepare()
        self.assertEqual(status1, status2)
        self.assertEqual(status1["phase"], "legacy_pending")
        self.assertTrue(prepared["can_apply"])
        self.assertEqual(prepared["expires_in"], 180)
        self.assertEqual(prepared["changes"], {LINE: "riven-B", SCAN: "legacy-A", FLAG: True})
        self.assertEqual(prepared["attempted"], 0)
        self.assertFalse(prepared["sent"])
        self.assert_untouched(before)
        self.assertEqual(self.host.context.enumerations, 0)
        routes = {
            path.rsplit("/", 1)[-1]: methods
            for path, _method, methods, _label in self.panel.routes()
        }
        for name, methods in (("status", ["GET"]), ("prepare", ["POST"]), ("apply", ["POST"])):
            self.assertEqual(routes["vision_migration_" + name], methods)

    async def test_release_is_idempotent_scoped_readonly_and_preserves_other_window(self):
        before = dict(self.host.cfg)
        first, other = await self.prepare(), await self.prepare()
        lookups = list(self.host.context.lookups)
        for token in (first["token"], first["token"], "unknown", ""):
            result = await self.post("vision_migration_release", {"token": token})
            self.assertTrue(result["ok"])
            self.assertIn(other["token"], self.panel._migration_previews)
        for token in (None, [], {}, 123, True):
            result = await self.post("vision_migration_release", {"token": token})
            self.assertEqual(result["code"], "token_unavailable")
            self.assertIn(other["token"], self.panel._migration_previews)
        self.assertEqual(self.host.context.lookups, lookups)
        self.assert_untouched(before)
        self.assertTrue((await self.apply(other))["ok"])

    async def test_more_than_four_cancel_cycles_keep_active_limit(self):
        before = dict(self.host.cfg)
        for _ in range(8):
            prepared = await self.prepare()
            self.assertIn("token", prepared)
            await self.post("vision_migration_release", {"token": prepared["token"]})
        active = [await self.prepare() for _ in range(4)]
        self.assertTrue(all("token" in row for row in active))
        self.assertEqual((await self.prepare())["code"], "busy")
        self.assert_untouched(before)

    async def test_release_closed_before_and_after_body(self):
        prepared = await self.prepare()

        async def close_during_body():
            self.panel.closed = True
            return {"token": prepared["token"]}

        self.panel._body = AsyncMock(side_effect=close_during_body)
        self.assertEqual((await self.panel.vision_migration_release())["code"], "closed")
        self.assertIn(prepared["token"], self.panel._migration_previews)
        self.panel._body.reset_mock()
        self.assertEqual((await self.panel.vision_migration_release())["code"], "closed")
        self.panel._body.assert_not_awaited()

    async def test_default_empty_config_requires_selection_never_guesses_auto_provider(self):
        self.host._config.clear()
        self.host._config.persisted.clear()
        self.host.cfg.clear()
        status = await self.panel.vision_migration_status()
        prepared = await self.prepare()
        self.assertFalse(status["isolated"])
        self.assertFalse(prepared["can_apply"])
        self.assertEqual(prepared["code"], "selection_required")
        self.assertNotIn("token", prepared)
        self.assertCountEqual(prepared["requires_selection"], [RIVEN, LINE, SCAN])
        self.assertEqual(self.host.context.lookups, [])
        self.assertEqual(self.host.context.enumerations, 0)
        self.assert_untouched({})

    async def test_literal_confirmation_required_and_same_token_can_then_confirm(self):
        before = dict(self.host.cfg)
        preview = await self.prepare()
        for value in (None, False, 1, "true"):
            with self.subTest(confirmed=value):
                result = await self.apply(preview, confirmed=value)
                self.assertEqual(result["code"], "confirmation_required")
                self.assert_untouched(before)
        result = await self.apply(preview)
        self.assertTrue(result["ok"])
        self.assertEqual(self.host._config.save_calls, 1)

    async def test_confirm_copies_only_missing_preserves_legacy_and_existing_specialized(self):
        result = await self.apply(await self.prepare())
        self.assertTrue(result["ok"])
        expected = {
            LEGACY: "legacy-A",
            RIVEN: "riven-B",
            LINE: "riven-B",
            SCAN: "legacy-A",
            FLAG: True,
        }
        self.assertEqual(dict(self.host._config), expected)
        self.assertEqual(self.host.cfg, expected)
        self.assertEqual(self.host._config.persisted, expected)
        self.assertEqual(result["saved"], sorted([LINE, SCAN, FLAG]))
        self.assertTrue(result["legacy_retained"])
        self.assertEqual(self.host.context.enumerations, 0)

    async def test_all_existing_specialized_are_retained(self):
        for config in (self.host._config, self.host.cfg):
            config.update({LINE: "line-C", SCAN: "scan-D"})
        preview = await self.prepare()
        self.assertEqual(preview["changes"], {FLAG: True})
        result = await self.apply(preview)
        self.assertTrue(result["ok"])
        self.assertEqual(self.host.cfg[LINE], "line-C")
        self.assertEqual(self.host.cfg[SCAN], "scan-D")
        self.assertEqual(self.host.cfg[LEGACY], "legacy-A")

    async def test_three_minute_expiry_rejects_and_consumes_snapshot(self):
        before = dict(self.host.cfg)
        preview = await self.prepare()
        expires = self.panel._migration_previews[preview["token"]]["expires"]
        with patch.object(W.time, "monotonic", return_value=expires):
            result = await self.apply(preview)
        self.assertEqual(result["code"], "token_unavailable")
        self.assertNotIn(preview["token"], self.panel._migration_previews)
        self.assert_untouched(before)

    async def test_close_invalidates_and_all_three_endpoints_refuse(self):
        before = dict(self.host.cfg)
        preview = await self.prepare()
        self.panel.close()
        self.assertEqual(self.panel._migration_previews, {})
        self.assertEqual((await self.panel.vision_migration_status())["code"], "closed")
        self.assertEqual((await self.prepare())["code"], "closed")
        self.assertEqual((await self.apply(preview))["code"], "closed")
        self.assert_untouched(before)

    async def test_close_while_waiting_for_request_body_prevents_apply(self):
        preview = await self.prepare()
        before = dict(self.host.cfg)

        async def closing_body():
            self.panel.close()
            return {"confirmed": True, "token": preview["token"], "revision": preview["revision"]}

        self.panel._body = closing_body
        result = await self.panel.vision_migration_apply()
        self.assertEqual(result["code"], "closed")
        self.assert_untouched(before)

    async def test_config_change_rejects_old_preview_without_overwriting_new_values(self):
        preview = await self.prepare()
        for config in (self.host._config, self.host.cfg):
            config[SCAN] = "scan-D"
        before = dict(self.host.cfg)
        result = await self.apply(preview)
        self.assertEqual(result["code"], "revision_conflict")
        self.assertEqual(dict(self.host._config), before)
        self.assertEqual(self.host.cfg, before)
        self.assertEqual(self.host._config.save_calls, 0)

    async def test_provider_availability_change_rejects_old_snapshot(self):
        before = dict(self.host.cfg)
        preview = await self.prepare()
        del self.host.context.providers["riven-B"]
        result = await self.apply(preview)
        self.assertEqual(result["code"], "revision_conflict")
        self.assert_untouched(before)

    async def test_wrong_revision_and_replayed_tokens_never_save_twice(self):
        before = dict(self.host.cfg)
        preview = await self.prepare()
        self.assertEqual(
            (await self.apply(preview, revision="old-revision"))["code"], "revision_conflict"
        )
        self.assertEqual((await self.apply(preview))["code"], "token_unavailable")
        self.assert_untouched(before)
        valid = await self.prepare()
        self.assertTrue((await self.apply(valid))["ok"])
        saved = copy.deepcopy(self.host.cfg)
        self.assertEqual((await self.apply(valid))["code"], "token_unavailable")
        self.assertEqual(self.host._config.save_calls, 1)
        self.assertEqual(self.host.cfg, saved)

    async def test_four_preview_slots_then_expired_slot_can_be_replaced(self):
        before = dict(self.host.cfg)
        previews = [await self.prepare() for _ in range(4)]
        self.assertEqual(len({row["token"] for row in previews}), 4)
        self.assertEqual((await self.prepare())["code"], "busy")
        self.panel._migration_previews[previews[0]["token"]]["expires"] = 0
        self.assertIn("token", await self.prepare())
        self.assertEqual(len(self.panel._migration_previews), 4)
        self.assert_untouched(before)

    async def test_ocr_busy_and_settings_lock_refuse_without_consuming_token(self):
        before = dict(self.host.cfg)
        preview = await self.prepare()
        self.host._ocr_busy.add("synthetic-ongoing-image")
        self.assertEqual((await self.apply(preview))["code"], "busy")
        self.assertIn(preview["token"], self.panel._migration_previews)
        self.host._ocr_busy.clear()
        await self.panel._save_lock.acquire()
        try:
            self.assertEqual((await self.apply(preview))["code"], "busy")
        finally:
            self.panel._save_lock.release()
        self.assert_untouched(before)
        self.assertTrue((await self.apply(preview))["ok"])

    async def test_rollback_only_toggles_flag_and_retains_all_route_values(self):
        await self.apply(await self.prepare())
        before = copy.deepcopy(self.host.cfg)
        preview = await self.prepare("rollback")
        self.assertEqual(preview["changes"], {FLAG: False})
        self.assertTrue((await self.apply(preview))["ok"])
        self.assertEqual(self.host.cfg, {**before, FLAG: False})
        self.assertEqual(dict(self.host._config), {**before, FLAG: False})
        self.assertEqual(self.host._config.persisted, {**before, FLAG: False})

    async def test_save_failure_restores_present_and_absent_keys_and_runtime_snapshot(self):
        self.host._config.pop(LINE)
        self.host.cfg.pop(LINE)
        self.host._config.persisted = dict(self.host._config)
        before = dict(self.host.cfg)
        preview = await self.prepare()
        self.host._config.fail_save = True
        result = await self.apply(preview)
        self.assertEqual(result["code"], "save_failed")
        self.assertEqual(dict(self.host._config), before)
        self.assertEqual(self.host.cfg, before)
        self.assertEqual(self.host._config.persisted, before)
        self.assertNotIn(LINE, self.host._config)
        self.assertEqual(self.host._config.save_calls, 1)
        self.assertEqual((await self.apply(preview))["code"], "token_unavailable")

    async def test_ordinary_settings_save_cannot_toggle_flag_or_partially_write_other_keys(self):
        before = dict(self.host.cfg)
        result = await self.post("settings_save", {"changes": {FLAG: True, RIVEN: "line-C"}})
        self.assertEqual(result["status"], "error")
        self.assertIn("迁移", result["message"])
        self.assert_untouched(before)

    async def test_strict_selectors_read_new_explicit_live_values_and_do_not_fall_back(self):
        await self.apply(await self.prepare())
        context = self.host.context
        self.assertEqual(self.host._riven_vision_providers(), [context.providers["riven-B"]])
        self.assertEqual(self.host._line_vision_providers(), [context.providers["riven-B"]])
        self.assertEqual(self.host._scan_vision_providers(), [context.providers["legacy-A"]])
        # 隔离后修改显式值立即被实际selector读取，而非继续用迁移前缓存。
        self.host.cfg[SCAN] = "scan-D"
        self.assertEqual(self.host._scan_vision_providers(), [context.providers["scan-D"]])
        for key in (RIVEN, SCAN):
            self.host.cfg[key] = ""
        self.assertEqual(self.host._riven_vision_providers(), [])
        self.assertEqual(self.host._scan_vision_providers(), [])
        self.host.cfg[LINE] = ""
        self.assertEqual(self.host._line_vision_providers(), [])
        self.host.cfg[RIVEN] = "riven-B"
        self.assertEqual(self.host._line_vision_providers(), [context.providers["riven-B"]])
        self.host.cfg[LINE] = "missing-explicit-line"
        self.assertEqual(self.host._line_vision_providers(), [])
        self.assertEqual(self.host.cfg[LEGACY], "legacy-A")
        self.assertEqual(context.enumerations, 0)

    async def test_invalid_or_divergent_config_refuses_plan(self):
        for key, value in ((FLAG, "true"), (LINE, None)):
            with self.subTest(key=key):
                before = dict(self.host._config)
                self.host._config[key] = value
                result = await self.panel.vision_migration_status()
                self.assertEqual(result["code"], "invalid_config")
                self.host._config.clear()
                self.host._config.update(before)
        self.host.cfg[LEGACY] = "different-runtime-value"
        self.assertEqual((await self.prepare())["code"], "invalid_config")
        self.assertEqual(self.host._config.save_calls, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
