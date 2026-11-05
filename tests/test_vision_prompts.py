"""视觉提示词存储回归：真实临时文件，不联网、不调用模型。"""

from __future__ import annotations

import json
import os
import runpy
import subprocess
import types
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.vision_prompts import (  # noqa: E402
    MAX_PROMPT_LENGTH,
    TASKS,
    PromptConflict,
    VisionPromptStore,
)


class PromptStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "prompts.json"
        self.store = VisionPromptStore(self.path)
        self.pid, self.task = "channel-a", "riven_json"

    def apply(self, action, revision, text=None):
        return self.store.apply(self.pid, self.task, action, revision, text)

    def test_first_read_has_no_filesystem_side_effect(self):
        path = Path(self.temp.name) / "absent" / "nested" / "prompts.json"
        store = VisionPromptStore(path)
        for _ in range(3):
            self.assertEqual(store.view(self.pid, self.task)["revision"], 0)
            self.assertEqual(store.effective(self.pid, self.task, " 原版\n"), " 原版\n")
        self.assertFalse(path.parent.parent.exists())

    def test_overview_first_read_has_no_filesystem_side_effect(self):
        path = Path(self.temp.name) / "absent" / "nested" / "prompts.json"
        store = VisionPromptStore(path)
        self.assertEqual(store.overview(), [])
        self.assertFalse(path.parent.parent.exists())

    def test_overview_snapshot_order_and_container_isolation(self):
        self.store.apply("失联渠道", "scan_title", "save", 0, "标题正文")
        self.store.apply("channel-a", "scan_main", "save", 0, "面板正文")
        self.apply("save", 0, "草稿甲")
        self.apply("activate", 1)
        self.apply("save", 2, "草稿乙")
        before = self.path.read_bytes()
        with patch.object(self.store, "_read", wraps=self.store._read) as read:
            overview = self.store.overview()
        read.assert_called_once_with()
        self.assertEqual(
            overview,
            [
                {
                    "provider_id": "channel-a",
                    "task": "riven_json",
                    "revision": 3,
                    "has_draft": True,
                    "active": True,
                    "draft_matches_active": False,
                },
                {
                    "provider_id": "channel-a",
                    "task": "scan_main",
                    "revision": 1,
                    "has_draft": True,
                    "active": False,
                    "draft_matches_active": False,
                },
                {
                    "provider_id": "失联渠道",
                    "task": "scan_title",
                    "revision": 1,
                    "has_draft": True,
                    "active": False,
                    "draft_matches_active": False,
                },
            ],
        )
        overview[0]["revision"] = 999
        overview.clear()
        self.assertEqual(self.store.overview()[0]["revision"], 3)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.store.effective(self.pid, self.task, "原版"), "草稿甲")

    def test_overview_reset_and_cross_instance_reload_cas(self):
        other = VisionPromptStore(self.path)
        self.assertEqual(other.overview(), [])
        self.apply("save", 0, "草稿甲")
        self.apply("activate", 1)
        self.assertTrue(other.overview()[0]["draft_matches_active"])
        self.apply("reset", 2)
        self.store.apply("empty-channel", "scan_main", "reset", 0)
        expected = [
            {
                "provider_id": self.pid,
                "task": self.task,
                "revision": 3,
                "has_draft": True,
                "active": False,
                "draft_matches_active": False,
            }
        ]
        self.assertEqual(other.overview(), expected)
        self.assertEqual(VisionPromptStore(self.path).overview(), expected)
        before = self.path.read_bytes()
        with self.assertRaises(PromptConflict):
            other.apply(self.pid, self.task, "activate", 2)
        self.assertEqual(self.path.read_bytes(), before)
        other.apply(self.pid, self.task, "activate", other.overview()[0]["revision"])
        self.assertEqual(self.store.overview()[0]["revision"], 4)
        self.assertTrue(self.store.overview()[0]["draft_matches_active"])

    def test_overview_corrupt_file_fails_without_writes_or_lock(self):
        for raw in (b"{", b"\xff", b"[]", b'{"version":2,"providers":{}}'):
            with self.subTest(raw=raw):
                self.path.write_bytes(raw)
                with self.assertRaisesRegex(ValueError, "视觉提示词配置损坏"):
                    self.store.overview()
                self.assertEqual(self.path.read_bytes(), raw)
                self.assertEqual(list(self.path.parent.iterdir()), [self.path])
        self.path.unlink()
        self.path.mkdir()
        with self.assertRaises(OSError):
            self.store.overview()

    def test_invalid_utf8_has_safe_error(self):
        self.path.write_bytes(b"secret-raw-byte-\xff")
        for call in (lambda: self.store.view(self.pid, self.task), lambda: self.apply("reset", 0)):
            with self.assertRaises(ValueError) as result:
                call()
            self.assertEqual(str(result.exception), "视觉提示词配置损坏，未改写文件")
            self.assertNotIsInstance(result.exception, UnicodeError)
        self.assertEqual(self.path.read_bytes(), b"secret-raw-byte-\xff")

    def test_windows_import_and_lock_offset(self):
        calls = []
        fake = types.SimpleNamespace(LK_LOCK=1, LK_UNLCK=2)

        def locking(fd, mode, count):
            calls.append((mode, count, os.lseek(fd, 0, os.SEEK_CUR)))

        fake.locking = locking
        real_import = __import__

        def importing(name, *args, **kwargs):
            if name == "fcntl":
                raise ImportError("Windows不提供fcntl")
            return real_import(name, *args, **kwargs)

        with (
            patch.object(sys, "platform", "win32"),
            patch.dict(sys.modules, {"msvcrt": fake}),
            patch("builtins.__import__", side_effect=importing),
        ):
            sys.modules.pop("core.vision_prompts", None)
            namespace = runpy.run_module("core.vision_prompts")
            store = namespace["VisionPromptStore"](self.path)
            store.apply(self.pid, self.task, "save", 0, "甲")
            store.apply(self.pid, self.task, "activate", 1)
        self.assertEqual(calls, [(1, 1, 0), (2, 1, 0)] * 2)
        self.assertEqual(self.store.view(self.pid, self.task)["active"], "甲")

    def test_cross_process_cas(self):
        code = (
            "import sys; from pathlib import Path; "
            "from core.vision_prompts import VisionPromptStore, PromptConflict\n"
            "try: VisionPromptStore(Path(sys.argv[1])).apply('channel-a','riven_json','save',0,'甲')\n"
            "except PromptConflict: sys.exit(3)\n"
        )
        children = [
            subprocess.Popen(
                [sys.executable, "-c", code, str(self.path)],
                cwd=ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            for _ in range(6)
        ]
        outcomes = []
        for child in children:
            out, err = child.communicate(timeout=15)
            self.assertIn(child.returncode, (0, 3), (out, err))
            outcomes.append(child.returncode)
        self.assertEqual(outcomes.count(0), 1)
        self.assertEqual(outcomes.count(3), 5)

    def test_task_contracts(self):
        self.assertEqual(
            set(TASKS),
            {
                "riven_json",
                "riven_lines",
                "scan_main",
                "scan_title",
                "scan_panel",
                "scan_mods",
                "scan_arcanes",
                "scan_tiles",
            },
        )
        for value in TASKS.values():
            self.assertTrue(value["label"])
            self.assertTrue(value["contract"])

    def test_snapshot_reset_reload_isolation(self):
        original = " 原版\n\t"
        self.assertEqual(self.store.effective(self.pid, self.task, original), original)
        self.assertEqual(
            self.store.view(self.pid, self.task),
            {
                "revision": 0,
                "draft": None,
                "active": None,
            },
        )
        saved = self.apply("save", 0, " 草稿甲\n")
        self.assertEqual(saved["active"], None)
        self.assertEqual(self.store.effective(self.pid, self.task, original), original)
        self.apply("activate", 1)
        self.apply("save", 2, "草稿乙")
        reopened = VisionPromptStore(self.path)
        self.assertEqual(reopened.effective(self.pid, self.task, original), " 草稿甲\n")
        self.assertEqual(reopened.view(self.pid, self.task)["draft"], "草稿乙")
        self.assertEqual(reopened.effective("channel-b", self.task, original), original)
        self.assertEqual(reopened.effective(self.pid, "riven_lines", original), original)
        state = reopened.apply(self.pid, self.task, "reset", 3)
        self.assertEqual(state, {"revision": 4, "draft": "草稿乙", "active": None})
        self.assertEqual(self.store.effective(self.pid, self.task, original), original)
        self.apply("activate", 4)
        self.assertEqual(self.store.effective(self.pid, self.task, original), "草稿乙")

    def test_cas_across_old_instances(self):
        other = VisionPromptStore(self.path)
        other.view(self.pid, self.task)
        self.apply("save", 0, "甲")
        before = self.path.read_bytes()
        with self.assertRaises(PromptConflict):
            other.apply(self.pid, self.task, "save", 0, "乙")
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(other.view(self.pid, self.task)["draft"], "甲")

    def test_concurrent_cas(self):
        def write(index):
            try:
                VisionPromptStore(self.path).apply(self.pid, self.task, "save", 0, str(index))
                return True
            except PromptConflict:
                return False

        with ThreadPoolExecutor(max_workers=6) as pool:
            self.assertEqual(sum(pool.map(write, range(12))), 1)
        self.assertEqual(self.store.view(self.pid, self.task)["revision"], 1)

    def test_validation_and_boundaries(self):
        for text in (None, "", " \n", 1, "甲" * (MAX_PROMPT_LENGTH + 1)):
            with self.assertRaises(ValueError):
                self.apply("save", 0, text)
        self.assertFalse(self.path.exists())
        self.apply("save", 0, "甲" * MAX_PROMPT_LENGTH)
        for action, revision, text in (
            ("unknown", 1, None),
            ("reset", True, None),
            ("reset", -1, None),
            ("activate", 1, "暗中启用"),
        ):
            with self.assertRaises(ValueError):
                self.apply(action, revision, text)
        for pid, task in (("", self.task), (self.pid, "unknown")):
            with self.assertRaises(ValueError):
                self.store.effective(pid, task, "原版")
        empty = VisionPromptStore(Path(self.temp.name) / "empty.json")
        with self.assertRaises(ValueError):
            empty.apply(self.pid, self.task, "activate", 0)

    def test_empty_config(self):
        self.path.write_text("{}")
        self.assertEqual(self.store.view(self.pid, self.task)["revision"], 0)
        self.apply("reset", 0)
        self.assertEqual(self.store.view(self.pid, self.task)["revision"], 1)

    def test_corrupt_file_preserved_and_recoverable(self):
        for raw in (
            b"{",
            b"\xff",
            b"[]",
            b'{"version": true,"providers":{}}',
            json.dumps(
                {
                    "version": 1,
                    "providers": {
                        self.pid: {
                            self.task: {
                                "revision": 1,
                                "draft": " ",
                                "active": None,
                            }
                        }
                    },
                }
            ).encode(),
        ):
            self.path.write_bytes(raw)
            with self.assertRaises((ValueError, UnicodeError)):
                self.store.view(self.pid, self.task)
            with self.assertRaises((ValueError, UnicodeError)):
                self.apply("reset", 0)
            self.assertEqual(self.store.effective(self.pid, self.task, "原版"), "原版")
            self.assertEqual(self.path.read_bytes(), raw)
        self.path.write_text("{}")
        self.apply("save", 0, "恢复")
        self.assertEqual(self.store.view(self.pid, self.task)["draft"], "恢复")

    def test_invalid_states(self):
        for state in (
            {"revision": False, "draft": "甲", "active": None},
            {"revision": -1, "draft": "甲", "active": None},
            {"revision": 1, "draft": None, "active": "甲"},
            {"revision": 1, "draft": "甲", "active": ""},
            {"revision": 1, "draft": "甲", "active": None, "extra": 1},
        ):
            self.path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "providers": {self.pid: {self.task: state}},
                    }
                )
            )
            with self.assertRaises(ValueError):
                self.store.view(self.pid, self.task)

    def test_write_failure_does_not_publish(self):
        self.apply("save", 0, "甲")
        before = self.path.read_bytes()
        with patch("core.vision_prompts.os.replace", side_effect=OSError("模拟写失败")):
            with self.assertRaises(OSError):
                self.apply("activate", 1)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.store.view(self.pid, self.task)["revision"], 1)
        self.assertEqual(self.store.effective(self.pid, self.task, "原版"), "原版")
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])
        self.path.unlink()
        self.path.mkdir()
        with self.assertRaises(OSError):
            self.apply("save", 0, "乙")
        self.assertEqual(self.store.effective(self.pid, self.task, "原版"), "原版")


if __name__ == "__main__":
    unittest.main(verbosity=2)
