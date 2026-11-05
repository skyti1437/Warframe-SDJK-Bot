"""主动迁移后的严格路由与真实 handler 限流；所有渠道/OCR 均为内存桩。"""

import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.commands.riven import RivenCommands
from core.commands.scan import ScanCommands
from core.commands.vision import VisionCommands


class Provider:
    def __init__(self, name):
        self.name = name
        self.calls = []

    def meta(self):
        return SimpleNamespace(id=self.name)

    async def text_chat(self, **request):
        self.calls.append(request)
        text = (
            "+10% 暴击伤害\n+20% 攻击速度"
            if request["prompt"] == VisionCommands._RIVEN_LINE_PROMPT
            else '{"weapon":"欧玛","positive":[["暴击伤害",10],["攻击速度",20]]}'
        )
        return SimpleNamespace(completion_text=text)


class Host(ScanCommands, VisionCommands, RivenCommands):
    def __init__(self, cfg):
        self.cfg = cfg
        self._ocr_busy = set()
        self._ocr_last = {}
        self.providers = {name: Provider(name) for name in ("compat", "riven", "scan", "line")}
        self.lookups = []
        self.context = SimpleNamespace(
            get_provider_by_id=self.lookup, get_all_providers=self.enumerate
        )

    def lookup(self, name):
        self.lookups.append(name)
        return self.providers.get(name)

    @staticmethod
    def enumerate():
        raise AssertionError("隔离任务不应枚举自动渠道")

    @staticmethod
    def _safe_sender(event):
        return event.sender

    @staticmethod
    def _event_has_image(event):
        return True

    async def _image_data_urls(self, event):
        return ["synthetic-memory-image"]


class BrokenConfig(dict):
    def __init__(self, *args, broken=()):
        super().__init__(*args)
        self.broken = set(broken)

    def get(self, key, default=None):
        if key in self.broken:
            raise RuntimeError("合成配置读取失败")
        return super().get(key, default)


class IsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_single_ocr_is_explicit_and_preserves_local_priority(self):
        host = Host({"riven_single_ocr": True, "vision_routes_isolated": True})
        self.assertTrue(host._use_single_riven_ocr())
        reply = await host._h_riven_analysis(
            SimpleNamespace(content=[]), SimpleNamespace(sender="test"), "pc"
        )
        self.assertIn("主读渠道不可用", reply.raw_text)
        self.assertEqual(host.lookups, [])
        for enabled, ok in ((True, True), (False, False), (False, True)):
            host.local_ocr = SimpleNamespace(status=lambda: {"enabled": enabled, "ok": ok})
            self.assertEqual(host._use_single_riven_ocr(), ok and not enabled)

    def test_single_ocr_rereads_agree_without_overriding_a_digit(self):
        from core import riven_ocr_reader as reader

        def observation(lines, confirmed=False):
            obs = reader.Observation({"weapon": "合成名称", "lines": lines})
            if confirmed:
                pos, neg, legal, _ = VisionCommands._riven_lines_legal(lines)
                self.assertTrue(legal)
                obs.accepted = reader.Fields(pos, neg, 0)
            return obs

        clean = ["+20% 攻击速度", "+30% 暴击伤害", "-8% 暴击几率"]
        primary = observation(["+21% 攻击速度", *clean[1:]])
        second = observation(clean, True)
        third = observation(clean, True)
        self.assertIsNone(reader._select(primary, second, third))
        primary = observation(clean)
        self.assertTrue(reader._confirms(primary, second))
        self.assertIs(reader._select(primary, second, third), second)

    async def test_ocr_first_image_has_no_placeholder_or_automatic_retry(self):
        from unittest.mock import MagicMock
        from core import riven_ocr_reader as reader

        packet = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    index=0, delta=SimpleNamespace(content="原文"), finish_reason="stop"
                )
            ]
        )
        stream = MagicMock()
        stream.__aiter__.return_value = [packet]
        create = AsyncMock(return_value=stream)
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        provider = MagicMock()
        provider.client.with_options.return_value = client
        provider.get_model.return_value = "配置渠道"
        text = await reader.transcribe(provider, "image", "")
        self.assertEqual(text, "原文")
        provider.client.with_options.assert_called_once_with(max_retries=0)
        kwargs = create.await_args.kwargs
        self.assertEqual(
            kwargs["messages"],
            [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "image"}}]}],
        )
        self.assertEqual(
            (kwargs["temperature"], kwargs["max_tokens"], kwargs["stream"]), (0, 2048, True)
        )

    async def test_scan_equipment_uses_explicit_route_and_preserves_main(self):
        for isolated in (False, True):
            host = Host(
                {
                    "vision_routes_isolated": isolated,
                    "vision_provider_id_scan": "scan",
                    "vision_provider_id_scan_equipment": "equipment",
                }
            )
            equipment = host.providers["equipment"] = Provider("equipment")
            equipment.text_chat = AsyncMock(
                return_value=SimpleNamespace(completion_text='{"mods":[{"name":"合成装备"}]}')
            )
            observed = await host._read_scan_focus("synthetic-memory-image", "mods")
            self.assertEqual(observed["mods"][0]["name"], "合成装备")
            self.assertEqual(observed["_recognition"]["provider_id"], "equipment")
            self.assertEqual(equipment.text_chat.await_count, 1)
            self.assertFalse(host.providers["scan"].calls)
            self.assertEqual(host._scan_vision_providers(), [host.providers["scan"]])
            host.cfg["vision_provider_id_scan_equipment"] = "missing"
            self.assertIsNone(await host._read_scan_focus("synthetic-memory-image", "mods"))
            self.assertEqual(equipment.text_chat.await_count, 1)
            self.assertFalse(host.providers["scan"].calls)

    def test_scan_equipment_empty_inherits_scan_and_invalid_config_stops(self):
        for isolated in (False, True):
            host = Host({"vision_routes_isolated": isolated, "vision_provider_id_scan": "scan"})
            self.assertEqual(host._scan_equipment_providers(), [host.providers["scan"]])
            for invalid in (None, 8, [], {}):
                host.cfg["vision_provider_id_scan_equipment"] = invalid
                self.assertEqual(host._scan_equipment_providers(), [])
        host = Host({"vision_routes_isolated": True, "vision_provider_id": "compat"})
        self.assertEqual(host._scan_equipment_providers(), [])
        self.assertFalse(host.lookups)

    def test_missing_flag_and_explicit_false_preserve_legacy_inheritance(self):
        for extra in ({}, {"vision_routes_isolated": False}):
            host = Host({"vision_provider_id": "compat", **extra})
            self.assertFalse(host._vision_routes_isolated())
            for method in (
                host._riven_vision_providers,
                host._scan_vision_providers,
                host._line_vision_providers,
            ):
                self.assertEqual(method(), [host.providers["compat"]])
            host.cfg["vision_provider_id_line"] = "missing"
            self.assertEqual(host._line_vision_providers(), [host.providers["compat"]])

    async def test_empty_strict_routes_never_call_compat_or_automatic_provider(self):
        host = Host({"vision_routes_isolated": True, "vision_provider_id": "compat"})
        self.assertEqual(host._riven_vision_providers(), [])
        self.assertEqual(host._scan_vision_providers(), [])
        self.assertEqual(host._line_vision_providers(), [])
        self.assertIsNone(await host._extract_riven_from_image("synthetic-memory-image"))
        self.assertEqual(host.lookups, [])
        self.assertFalse(any(p.calls for p in host.providers.values()))

    def test_strict_explicit_routes_are_independent(self):
        host = Host(
            {
                "vision_routes_isolated": True,
                "vision_provider_id": "compat",
                "vision_provider_id_riven": "riven",
                "vision_provider_id_scan": "scan",
                "vision_provider_id_line": "line",
            }
        )
        self.assertEqual(host._riven_vision_providers(), [host.providers["riven"]])
        self.assertEqual(host._scan_vision_providers(), [host.providers["scan"]])
        self.assertEqual(host._line_vision_providers(), [host.providers["line"]])
        host.cfg["vision_provider_id_riven"] = "missing"
        self.assertEqual(host._riven_vision_providers(), [])
        self.assertEqual(host._line_vision_providers(), [host.providers["line"]])
        self.assertEqual(host._scan_vision_providers(), [host.providers["scan"]])

    async def test_strict_empty_line_inherits_only_riven_and_missing_line_does_not_fallback(self):
        host = Host(
            {
                "vision_routes_isolated": True,
                "vision_provider_id": "compat",
                "vision_provider_id_riven": "riven",
            }
        )
        await host._extract_riven_from_image("synthetic-memory-image")
        self.assertEqual(len(host.providers["riven"].calls), 2)
        self.assertFalse(host.providers["compat"].calls)
        host.providers["riven"].calls.clear()
        host.cfg["vision_provider_id_line"] = "missing"
        await host._extract_riven_from_image("synthetic-memory-image")
        self.assertEqual(len(host.providers["riven"].calls), 1)
        self.assertEqual(
            host.providers["riven"].calls[0]["prompt"], VisionCommands._RIVEN_JSON_PROMPT
        )

    def test_invalid_flag_types_and_unreadable_flag_fail_closed(self):
        configs = [
            {"vision_provider_id": "compat", "vision_routes_isolated": value}
            for value in (None, 0, 1, "false", "true", [], {})
        ]
        configs.append(
            BrokenConfig({"vision_provider_id": "compat"}, broken=("vision_routes_isolated",))
        )
        for cfg in configs:
            with self.subTest(cfg=cfg):
                host = Host(cfg)
                self.assertTrue(host._vision_routes_isolated())
                self.assertEqual(host._riven_vision_providers(), [])
                self.assertEqual(host._scan_vision_providers(), [])
                self.assertEqual(host._line_vision_providers(), [])
                self.assertEqual(host.lookups, [])

    async def test_broken_or_nonstring_strict_line_setting_is_not_treated_as_empty(self):
        for cfg in (
            BrokenConfig(
                {"vision_routes_isolated": True, "vision_provider_id_riven": "riven"},
                broken=("vision_provider_id_line",),
            ),
            {
                "vision_routes_isolated": True,
                "vision_provider_id_riven": "riven",
                "vision_provider_id_line": 1,
            },
        ):
            host = Host(cfg)
            await host._extract_riven_from_image("synthetic-memory-image")
            self.assertEqual(len(host.providers["riven"].calls), 1)

    async def test_enabled_local_ocr_still_returns_before_any_cloud_selector(self):
        host = Host({"vision_routes_isolated": True})
        host.local_ocr = SimpleNamespace(
            status=lambda: {"enabled": True, "ok": True},
            recognize=AsyncMock(
                return_value={"state": "partial", "structured": {"weapon": "欧玛"}}
            ),
        )
        result = await host._extract_riven_from_image("data:image/png;base64,eA==")
        self.assertEqual(result["_local_ocr"]["state"], "partial")
        host.local_ocr.recognize.assert_awaited_once_with(b"x")
        self.assertEqual(host.lookups, [])
        self.assertFalse(any(p.calls for p in host.providers.values()))

    async def test_failure_refunds_only_its_own_task_cooldown_through_handlers(self):
        event = SimpleNamespace(unified_msg_origin="room", sender="user")
        parsed = SimpleNamespace(content=[])
        host = Host({"vision_routes_isolated": True, "scan_cooldown": 60})
        host._vision_gate(event, "识卡", task="scan")
        scan_stamp = host._vision_last("scan")["user"]
        host._extract_riven_from_image = AsyncMock(return_value=None)
        await host._h_riven_analysis(parsed, event, "pc")
        self.assertEqual(host._vision_last("scan")["user"], scan_stamp)
        self.assertNotIn("user", host._vision_last("riven"))
        host._vision_last("scan").clear()
        host._vision_gate(event, "紫卡", task="riven")
        riven_stamp = host._vision_last("riven")["user"]
        host._extract_loadout_validated = AsyncMock(return_value=None)
        await host._h_scan(parsed, event, "pc")
        self.assertEqual(host._vision_last("riven")["user"], riven_stamp)
        self.assertNotIn("user", host._vision_last("scan"))

    def test_refund_does_not_remove_a_later_request_stamp(self):
        last = {"user": 2.0}
        Host._vision_refund(last, "user", 1.0)
        self.assertEqual(last, {"user": 2.0})

    async def test_real_riven_handler_unique_slots_respect_cap_and_independent_release(self):
        for isolated in (False, True):
            host = Host(
                {"vision_routes_isolated": isolated, "scan_cooldown": 0, "scan_max_concurrent": 2}
            )
            entered = []

            async def blocked(image):
                entered.append(image)
                await asyncio.Future()

            host._extract_riven_from_image = blocked
            event = SimpleNamespace(unified_msg_origin="same-room", sender="same-user")
            parsed = SimpleNamespace(content=[])
            tasks = [
                asyncio.create_task(host._h_riven_analysis(parsed, event, "pc")) for _ in range(4)
            ]
            try:
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                self.assertEqual(len(entered), 2)
                self.assertEqual(len(host._ocr_busy), 2)
                self.assertTrue(all("并发上限" in t.result().raw_text for t in tasks[2:]))
                tasks[0].cancel()
                await asyncio.gather(tasks[0], return_exceptions=True)
                self.assertEqual(len(host._ocr_busy), 1)
                tasks.append(asyncio.create_task(host._h_riven_analysis(parsed, event, "pc")))
                await asyncio.sleep(0)
                self.assertEqual(len(entered), 3)
                self.assertEqual(len(host._ocr_busy), 2)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
            self.assertFalse(host._ocr_busy)

    async def test_scan_session_serial_and_machine_budget_remain_shared(self):
        host = Host({"vision_routes_isolated": True, "scan_cooldown": 0, "scan_max_concurrent": 2})
        parsed = SimpleNamespace(content=[])
        event = SimpleNamespace(unified_msg_origin="room", sender="user")

        async def blocked(image):
            await asyncio.Future()

        host._extract_loadout_validated = blocked
        host._extract_riven_from_image = blocked
        tasks = [asyncio.create_task(host._h_scan(parsed, event, "pc"))]
        try:
            await asyncio.sleep(0)
            reply = await host._h_scan(parsed, event, "pc")
            self.assertIn("上一张配卡", reply.raw_text)
            tasks.append(asyncio.create_task(host._h_riven_analysis(parsed, event, "pc")))
            await asyncio.sleep(0)
            reply = await host._h_riven_analysis(parsed, event, "pc")
            self.assertIn("机器共享资源预算", reply.raw_text)
            self.assertEqual(len(host._ocr_busy), 2)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        self.assertFalse(host._ocr_busy)

    async def test_scan_empty_origin_counts_unique_slots_and_releases_completion_and_cancel(self):
        for isolated in (False, True):
            host = Host(
                {"vision_routes_isolated": isolated, "scan_cooldown": 0, "scan_max_concurrent": 2}
            )
            parsed = SimpleNamespace(content=[])
            event = SimpleNamespace(unified_msg_origin="", sender="same-user")
            pending = []

            async def blocked(image):
                future = asyncio.get_running_loop().create_future()
                pending.append(future)
                return await future

            host._extract_loadout_validated = blocked
            tasks = [asyncio.create_task(host._h_scan(parsed, event, "pc")) for _ in range(4)]
            try:
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                self.assertEqual(len(pending), 2)
                self.assertEqual(len(host._ocr_busy), 2)
                self.assertTrue(all("并发上限" in task.result().raw_text for task in tasks[2:]))
                pending[0].set_result(None)
                await tasks[0]
                self.assertEqual(len(host._ocr_busy), 1)
                tasks[1].cancel()
                await asyncio.gather(tasks[1], return_exceptions=True)
                self.assertFalse(host._ocr_busy)
                tasks.append(asyncio.create_task(host._h_scan(parsed, event, "pc")))
                await asyncio.sleep(0)
                self.assertEqual(len(pending), 3)
                self.assertEqual(len(host._ocr_busy), 1)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
            self.assertFalse(host._ocr_busy)


if __name__ == "__main__":
    unittest.main(verbosity=2)
