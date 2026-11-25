# -*- coding: utf-8 -*-
"""全局指令开关的配置、面板路由、聊天分发和帮助卡回归。"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import parser, webpanel  # noqa: E402
from core.store import CommandStats  # noqa: E402
from tests import test_command_conflict as chat  # noqa: E402 - 复用 AstrBot 桩与聊天事件

LOCKED = {"help", "status", "wm", "bounty"}
EXPERIMENTAL = {"scan", "scandamage"}
CONFIG_KEY = "disabled_commands"


class Config(dict):
    def __init__(self, path):
        super().__init__(render_mode="text")
        self.path = path
        self.fail = False

    def save_config(self):
        if self.fail:
            raise OSError("simulated write failure")
        self.path.write_text(json.dumps(self), encoding="utf-8")


class CommandSwitchesTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name)
        self.bot = chat._make_plugin(path)
        self.bot._routes = self.bot._build_routes()
        self.conf = self.bot._config = Config(path / "config.json")
        self.bot.cfg.update(self.conf)
        self.panel = webpanel.WebPanel(self.bot, chat.plugin.PLUGIN_NAME)

    async def save(self, values):
        self.panel._body = AsyncMock(return_value={"disabled": values})
        return await self.panel.commands_config_save()

    async def send(self, text):
        event = chat._Ev(text)
        replies = await chat._drive(self.bot, event)
        return replies, event.stopped

    async def test_progress_notice_uses_context_only_for_events_without_send(self):
        event = chat._Ev("词条推荐")
        await self.bot._send_progress_notice(event, "没有 context")
        send = AsyncMock()
        self.bot.context = SimpleNamespace(send_message=send)
        await self.bot._send_progress_notice(None, "没有事件")
        await self.bot._send_progress_notice(SimpleNamespace(), "没有会话")
        send.assert_not_awaited()
        await self.bot._send_progress_notice(event, "正在后台计算")
        send.assert_awaited_once_with(event.unified_msg_origin, "正在后台计算")
        send.side_effect = OSError("send failed")
        with patch.object(chat.plugin.logger, "warning") as warning:
            await self.bot._send_progress_notice(event, "Calculating in the background")
        warning.assert_called_once()
        self.assertEqual(send.await_count, 2)

    async def test_catalog_and_defaults(self):
        schema = webpanel.load_schema()
        self.assertEqual(
            schema[CONFIG_KEY],
            {
                "description": "后台全局关闭的指令列表（由控制台「指令开关」管理）",
                "type": "list",
                "default": sorted(EXPERIMENTAL),
                "invisible": True,
            },
        )
        self.assertIn(CONFIG_KEY, webpanel.PANEL_ONLY_KEYS)
        self.assertNotIn(CONFIG_KEY, webpanel.RELOAD_KEYS)
        settings = await self.panel.settings()
        self.assertEqual(settings[CONFIG_KEY], sorted(EXPERIMENTAL))
        self.assertNotIn(
            CONFIG_KEY, {field["key"] for group in settings["groups"] for field in group["fields"]}
        )
        old = await self.panel.commands_config()
        self.assertEqual(old["disabled"], sorted(EXPERIMENTAL))
        self.assertEqual(old["default_disabled"], schema[CONFIG_KEY]["default"])
        self.assertEqual(old["counts"], {"total": 59, "enabled": 57, "disabled": 2, "locked": 4})
        self.bot.cfg[CONFIG_KEY] = []
        current = await self.panel.commands_config()
        self.assertEqual(current["default_disabled"], old["default_disabled"])
        self.assertEqual(
            current["counts"], {"total": 59, "enabled": 59, "disabled": 0, "locked": 4}
        )
        groups = current["groups"]
        self.assertEqual([len(group["commands"]) for group in groups], [2, 13, 11, 7, 6, 5, 10, 5])
        rows = [row for group in groups for row in group["commands"]]
        self.assertCountEqual([row["key"] for row in rows], parser.COMMAND_ALIASES.keys())
        self.assertEqual({row["key"] for row in rows if row["locked"]}, LOCKED)
        self.assertEqual({row["key"] for row in rows if row["experimental"]}, EXPERIMENTAL)
        self.assertEqual(webpanel.GLOBAL_PROTECTED_COMMANDS, LOCKED)
        self.assertTrue(all(row["enabled"] for row in rows))
        for row in rows:
            for field in ("name", "name_en", "description", "description_en", "group"):
                self.assertTrue(row[field], (row["key"], field))
            for alias in row["aliases"] + row["aliases_en"]:
                self.assertEqual(parser.parse(alias).command, row["key"], alias)
            if row["experimental"]:
                self.assertIn("默认关闭", row["description"])
                self.assertIn("Disabled by default", row["description_en"])

    async def test_scan_dispatch_defaults_and_explicit_enable(self):
        for key in EXPERIMENTAL:
            self.bot._routes[key] = AsyncMock(return_value=chat.plugin.Reply(raw_text=key))
        for text in ("识卡", "识卡伤害"):
            self.assertEqual(await self.send(text), ([], False))
        for key in EXPERIMENTAL:
            self.bot._routes[key].assert_not_awaited()
        self.assertEqual((await self.save([]))["status"], "ok")
        for key, text in (("scan", "识卡"), ("scandamage", "识卡伤害")):
            self.assertEqual(await self.send(text), ([("plain", key)], True))

    async def test_lock_and_validation(self):
        self.bot.cfg[CONFIG_KEY] = [*LOCKED, "news", "新闻", "unknown", {}]
        result = await self.panel.commands_config()
        self.assertEqual(result["disabled"], ["news"])
        for key in LOCKED - {"help"}:
            self.bot._routes[key] = AsyncMock(return_value=chat.plugin.Reply(raw_text=key))
        for text in ("帮助", "状态", "wm", "赏金"):
            with self.subTest(text=text):
                replies, stopped = await self.send(text)
                self.assertTrue(replies)
                self.assertTrue(stopped)
        for key in sorted(LOCKED):
            with self.subTest(key=key):
                failed = await self.save(["news", key])
                self.assertEqual(failed["status"], "error")
                self.assertIn("核心必留", failed["message"])
        for values in (None, "news", ["新闻"], ["unknown"], [{}]):
            with self.subTest(values=values):
                self.assertEqual((await self.save(values))["status"], "error")
        self.panel._body = AsyncMock(return_value={"changes": {CONFIG_KEY: ["help"]}})
        self.assertEqual((await self.panel.settings_save())["status"], "error")
        self.assertNotIn(CONFIG_KEY, self.conf)
        self.assertFalse(self.conf.path.exists())

    async def test_save_roundtrip_and_rollback(self):
        disabled = sorted(set(parser.COMMAND_ALIASES) - LOCKED)
        result = await self.save([*reversed(disabled), "news"])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["disabled"], disabled)
        self.assertTrue(result["revision"])
        self.assertEqual(self.bot.cfg[CONFIG_KEY], disabled)
        self.assertEqual(json.loads(self.conf.path.read_text(encoding="utf-8")), dict(self.conf))
        result = await self.panel.commands_config()
        self.assertEqual(result["disabled"], disabled)
        self.assertEqual(result["counts"], {"total": 59, "enabled": 4, "disabled": 55, "locked": 4})
        for group in result["groups"]:
            self.assertTrue(all(row["enabled"] == row["locked"] for row in group["commands"]))

        self.conf.fail = True
        self.assertEqual((await self.save([]))["status"], "error")
        self.assertEqual(self.conf[CONFIG_KEY], disabled)
        self.assertEqual(self.bot.cfg[CONFIG_KEY], disabled)
        self.assertEqual(
            json.loads(self.conf.path.read_text(encoding="utf-8"))[CONFIG_KEY], disabled
        )
        self.conf.fail = False
        defaults = result["default_disabled"]
        self.assertEqual((await self.save(defaults))["disabled"], defaults)
        self.assertEqual((await self.panel.commands_config())["counts"]["enabled"], 57)
        self.assertEqual((await self.panel.settings())[CONFIG_KEY], defaults)
        self.assertEqual(
            json.loads(self.conf.path.read_text(encoding="utf-8"))[CONFIG_KEY], defaults
        )
        self.panel.close()
        self.assertEqual((await self.panel.commands_config())["status"], "error")
        self.assertEqual((await self.save(["news"]))["status"], "error")

    async def test_stale_full_forms_merge_different_keys(self):
        first, second = await self.panel.commands_config(), await self.panel.commands_config()
        self.assertEqual(first["revision"], second["revision"])
        # 两张旧页面只回传 disabled。它们同时基于同一公开给表单的快照。
        a = await self.save(first["disabled"] + ["news"])
        b = await self.save(second["disabled"] + ["timers"])
        self.assertEqual((a["status"], b["status"]), ("ok", "ok"))
        expected = sorted(EXPERIMENTAL | {"news", "timers"})
        self.assertEqual(b["disabled"], expected)
        self.assertEqual(self.conf[CONFIG_KEY], self.bot.cfg[CONFIG_KEY])
        self.assertEqual(json.loads(self.conf.path.read_bytes())[CONFIG_KEY], expected)

    async def test_abandoned_legacy_form_then_refresh(self):
        first = await self.panel.commands_config()
        self.panel._body = AsyncMock(
            return_value={"revision": first["revision"], "changes": {"news": True}}
        )
        saved = await self.panel.commands_config_save()
        latest = await self.panel.commands_config()
        self.assertEqual(latest["revision"], saved["revision"])
        result = await self.save(latest["disabled"] + ["timers"])
        self.assertEqual(result["disabled"], sorted(EXPERIMENTAL | {"news", "timers"}))
        ambiguous = await self.save(first["disabled"])
        self.assertEqual(ambiguous["code"], "legacy_revision_required")
        self.assertEqual(self.conf[CONFIG_KEY], result["disabled"])

    async def test_legacy_refresh_after_revision_eviction(self):
        first = await self.panel.commands_config()
        for change in range(65):
            self.bot.cfg[CONFIG_KEY] = ["news"] if change % 2 == 0 else []
            self.panel._command_snapshot()
        self.assertNotIn(first["revision"], self.panel._command_history)
        latest = await self.panel.commands_config()
        saved = await self.save(latest["disabled"] + ["timers"])
        self.assertEqual(saved["disabled"], ["news", "timers"])

    async def test_revision_merge_conflict_and_idempotence(self):
        from contextvars import ContextVar

        requests = ContextVar("command_form")

        async def read():
            await asyncio.sleep(0)
            return requests.get()

        async def post(body):
            requests.set(body)
            return await self.panel.commands_config_save()

        self.panel._body = read
        base = await self.panel.commands_config()
        rev = base["revision"]
        a, b = await asyncio.gather(
            post({"revision": rev, "changes": {"news": True}}),
            post({"revision": rev, "disabled": [*base["disabled"], "timers"]}),
        )
        self.assertEqual((a["status"], b["status"]), ("ok", "ok"))
        latest = sorted(EXPERIMENTAL | {"news", "timers"})
        self.assertEqual(self.conf[CONFIG_KEY], latest)
        repeat = await post({"revision": rev, "changes": {"news": True}})
        self.assertEqual(repeat["disabled"], latest)
        conflict = await post({"revision": rev, "changes": {"news": False, "sortie": True}})
        self.assertEqual((conflict["status"], conflict["conflicts"]), ("conflict", ["news"]))
        self.assertEqual(self.bot.cfg[CONFIG_KEY], latest)
        self.assertEqual(json.loads(self.conf.path.read_bytes())[CONFIG_KEY], latest)
        # 新窗口明确改回原值。旧草稿此时虽与初始快照同基线，仍有同键的真实冲突。
        await post({"revision": conflict["revision"], "changes": {"news": False}})
        stale = await post({"revision": rev, "disabled": [*base["disabled"], "news"]})
        self.assertEqual(stale["conflicts"], ["news"])
        self.assertEqual(stale["disabled"], sorted(EXPERIMENTAL | {"timers"}))
        self.assertEqual(
            self.bot.cfg[CONFIG_KEY], json.loads(self.conf.path.read_bytes())[CONFIG_KEY]
        )

    async def test_stats_use_catalog_names(self):
        self.bot.command_stats = CommandStats(self.conf.path.with_name("stats.json"), {"analysis"})
        self.bot.command_stats.record("analysis", "success", 1250.25)
        stats = await self.panel.command_stats()
        for period in ("last_7_days", "last_30_days"):
            (row,) = stats[period]
            self.assertEqual(row["command"], "analysis")
            self.assertEqual(row["name_zh"], "紫卡分析")
            self.assertEqual(row["name_en"], "Riven Analysis")
            self.assertEqual(row["avg_ms"], 1250.25)

    async def test_dispatch_and_bilingual_help(self):
        news = AsyncMock(return_value=chat.plugin.Reply(raw_text="NEWS"))
        conclave = AsyncMock(return_value=chat.plugin.Reply(raw_text="CONCLAVE"))
        self.bot._routes.update(news=news, conclave=conclave)
        self.bot.cfg[CONFIG_KEY] = []
        default_cn, _ = await self.send("帮助 -cn")
        default_en, _ = await self.send("wfhelp -en")
        await self.save(["news", "conclave", "timers"])
        umo = chat._Ev("新闻").unified_msg_origin
        await self.bot.groups.edit_option(umo, "disabled", lambda _: ["news"])
        await self.bot.groups.edit_option(umo, "aliases", lambda _: {"资讯速递": "news"})
        for text in ("新闻", "news", "资讯速递", "武形秘仪", "conclave"):
            with self.subTest(text=text):
                self.assertEqual(await self.send(text), ([], False))
        news.assert_not_awaited()
        conclave.assert_not_awaited()

        for text, hidden, retained in (
            (
                "帮助 -cn",
                ("新闻", "官方公告", "武形秘仪", "PvP 挑战", "时效 / 周报"),
                ("结合目标", "警报"),
            ),
            (
                "wfhelp -en",
                ("news", "Official news", "conclave", "PvP challenges", "timers"),
                ("simaris", "alerts"),
            ),
        ):
            replies, _ = await self.send(text)
            card = "\n".join(body for _, body in replies)
            for word in hidden:
                self.assertNotIn(word, card)
            for word in retained:
                self.assertIn(word, card)

        await self.save([])
        blocked, stopped = await self.send("新闻")
        self.assertTrue(stopped)
        self.assertIn("本群已关闭该指令", blocked[0][1])
        news.assert_not_awaited()
        await self.bot.groups.edit_option(umo, "disabled", lambda _: [])
        self.assertEqual(await self.send("新闻"), ([("plain", "NEWS")], True))
        self.assertEqual(await self.send("武形秘仪"), ([("plain", "CONCLAVE")], True))
        self.assertEqual((await self.send("帮助 -cn"))[0], default_cn)
        self.assertEqual((await self.send("wfhelp -en"))[0], default_en)

    @unittest.skipUnless(shutil.which("node"), "需要 Node.js 校验面板脚本")
    def test_panel_conflict_preserves_only_edited_keys(self):
        html = (ROOT / "pages/sdjk/index.html").read_text(encoding="utf-8")
        start = html.index("async function saveCommandSwitches()")
        save = html[start : html.index('$("commandSearch").oninput', start)]
        script = r"""
const assert = require("node:assert/strict");
const commandSwitches = {busy:false, config:{revision:"old", disabled:["scan"]}, disabled:new Set(["scan","news"])};
const commandEditCount = () => 1, updateCommandControls = () => {};
let commandSnapshot = 0, applied, message, sent;
function showCommandStatus(text){message=text;}
function applyDisabledCommands(keys){applied=keys;}
const bridge = {async apiPost(endpoint, body){
  assert.equal(endpoint,"commands_config_save"); sent=body;
  return {status:"conflict",revision:"new",disabled:["scan","timers"],message:"同一指令已变化"};
}};
"""
        assertions = r"""
saveCommandSwitches().then(()=>{
  assert.deepEqual(sent,{revision:"old",changes:{news:true}});
  assert.deepEqual(applied,["scan","timers"]);
  assert.deepEqual([...commandSwitches.disabled].sort(),["news","scan","timers"]);
  assert.equal(commandSwitches.config.revision,"new");
  assert.match(message,/保留你的改动/);
}).catch(error=>{console.error(error);process.exitCode=1;});
"""
        result = subprocess.run(
            ["node"], input=script + save + assertions, text=True, capture_output=True, timeout=15
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(shutil.which("node"), "需要 Node.js 校验面板脚本")
    def test_panel_javascript_syntax(self):
        html = (ROOT / "pages/sdjk/index.html").read_text(encoding="utf-8")
        scripts = re.findall(r"<script\b[^>]*>(.*?)</script>", html, flags=re.DOTALL)
        self.assertTrue(scripts)
        for script in scripts:
            checked = subprocess.run(
                ["node", "--input-type=module", "--check"],
                input=script,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(checked.returncode, 0, checked.stderr)


if __name__ == "__main__":
    unittest.main()
