"""本地 OCR 管理回归：临时配置与真实桩子进程，不下载、不运行推理。"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import os
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from core import local_ocr as ocr  # noqa: E402

spec = importlib.util.spec_from_file_location(
    "local_ocr_worker_test", ROOT / "core/local_ocr_worker.py"
)
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)

ENOUGH = {"ok": True, "ram_available_bytes": 512 * ocr.MIB, "swap_available_bytes": 2048 * ocr.MIB}
STUB = """import json,os,sys,time,signal,subprocess
from pathlib import Path
q=json.loads(sys.stdin.read()); p=Path(q['model_dir']); mode=(p/'mode').read_text()
def publish_pid(path,pid):
 tmp=path.with_suffix('.ready'); tmp.write_text(str(pid)); os.replace(tmp,path)
if mode in ('sleep','descendant'):
 signal.signal(signal.SIGTERM,signal.SIG_IGN)
publish_pid(p/'pid',os.getpid())
if mode=='descendant':
 child_code="import os,signal,sys,time;from pathlib import Path;signal.signal(signal.SIGTERM,signal.SIG_IGN);p=Path(sys.argv[1]);tmp=p.with_suffix('.ready');tmp.write_text(str(os.getpid()));os.replace(tmp,p);time.sleep(30)"
 c=subprocess.Popen([sys.executable,'-c',child_code,str(p/'child_pid')])
if mode in ('sleep','descendant'):
 time.sleep(30)
if mode=='oversize': print('x'*3000000); sys.exit()
print(json.dumps({'status':'ok','recognition_state':mode,'structured':{'weapon':'测试'},'raw':{'rows':[]},'trace':{},'reasons':['保留原因'],'configuration_revision':99999}))
"""


def alive(pid):
    try:
        os.kill(pid, 0)
        # Linux 未被 init 收割的僵尸已不持有模型或内存，不算运行进程。
        path = Path(f"/proc/{pid}/stat")
        if path.exists() and path.read_text().split()[2] == "Z":
            return False
        return True
    except ProcessLookupError:
        return False


class ConfigTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.plugin = self.root / "plugin"
        (self.plugin / "core").mkdir(parents=True)
        (self.plugin / "core/local_ocr_worker.py").write_text(STUB)
        self.models = self.root / "models"
        self.models.mkdir()
        (self.models / "mode").write_text("accepted")
        self.manager = ocr.LocalRivenOCR(self.root / "data", self.plugin)
        self.addAsyncCleanup(self.manager.close)
        self.payload = dict(
            engine="rapidocr", python=sys.executable, model_dir=str(self.models), revision=0
        )
        self.resources = patch.object(ocr, "resource_snapshot", return_value=ENOUGH)
        self.resources.start()
        self.addCleanup(self.resources.stop)

    async def ready(self):
        await self.manager.save(self.payload)
        with patch.object(
            self.manager, "_check_settings", new=AsyncMock(return_value={"ok": True})
        ):
            await self.manager.check("rapidocr", sys.executable, str(self.models))
        await self.manager.enable(True, self.manager.status()["revision"])

    async def wait_pid(self, path=None):
        path = path or self.models / "pid"
        deadline = asyncio.get_running_loop().time() + 10
        while asyncio.get_running_loop().time() < deadline:
            try:
                pid = int(path.read_text())
                if pid > 0:
                    return pid
            except (FileNotFoundError, ValueError):
                pass
            await asyncio.sleep(0.01)
        self.fail(f"桩进程未发布可解析的正 PID：{path.name}")

    async def test_pid_handshake_rejects_empty_invalid_and_nonpositive_markers(self):
        path = self.root / "handshake-pid"
        for content in ("", "not-a-pid", "0", "-1"):
            path.write_text(content)
            task = asyncio.create_task(self.wait_pid(path))
            try:
                await asyncio.sleep(0)
                self.assertFalse(task.done())
                ready = path.with_suffix(".ready")
                ready.write_text(str(os.getpid()))
                os.replace(ready, path)
                self.assertEqual(await asyncio.wait_for(task, 10), os.getpid())
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_default_status_has_no_side_effect_or_process(self):
        with patch("asyncio.create_subprocess_exec", side_effect=AssertionError("禁止启动")):
            status = self.manager.status()
            self.assertFalse(status["enabled"])
            self.assertIsNone(status["check_ok"])
            self.assertEqual(status["revision"], 0)
            self.assertEqual((await self.manager.recognize(b"image"))["state"], "disabled")
        self.assertFalse(self.manager.path.parent.exists())

    async def test_save_cas_disable_check_and_restart_persistence(self):
        await self.ready()
        before = self.manager.status()
        self.assertTrue(before["enabled"])
        other = ocr.LocalRivenOCR(self.manager.path.parent, self.plugin)
        self.assertEqual(other.status()["revision"], before["revision"])
        with self.assertRaises(ocr.OCRConflict):
            await other.save(self.payload)
        await other.save({**self.payload, "revision": before["revision"]})
        self.assertFalse(self.manager.status()["enabled"])
        self.assertIsNone(self.manager.status()["check_ok"])
        with self.assertRaises(ValueError):
            await self.manager.enable(True, self.manager.status()["revision"])
        await other.close()

    async def test_concurrent_cas_has_one_winner(self):
        def write():
            manager = ocr.LocalRivenOCR(self.manager.path.parent, self.plugin)
            try:
                asyncio.run(manager.save(self.payload))
                return "saved"
            except ocr.OCRConflict:
                return "conflict"

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: write(), range(2)))
        self.assertCountEqual(results, ["saved", "conflict"])
        self.assertEqual(self.manager.status()["revision"], 1)

    async def test_paths_engine_boolean_and_corrupt_config_reject(self):
        for change in (
            {"engine": "cloud"},
            {"python": "python"},
            {"model_dir": "models"},
            {"model_dir": str(self.plugin / "weights")},
        ):
            with self.assertRaises(ValueError):
                await self.manager.save({**self.payload, **change})
        await self.manager.save(self.payload)
        with self.assertRaises(ValueError):
            await self.manager.enable("false", 1)
        self.manager.path.write_text("{broken")
        self.assertFalse(self.manager.status()["ok"])
        self.assertEqual((await self.manager.recognize(b"i"))["state"], "invalid_config")
        self.assertEqual(self.manager.path.read_text(), "{broken")

    async def test_check_draft_and_stale_check_cannot_mutate_saved(self):
        await self.ready()
        before = self.manager.path.read_bytes()
        with patch.object(
            self.manager, "_check_settings", new=AsyncMock(return_value={"ok": False})
        ):
            result = await self.manager.check("rapidocr", sys.executable, str(self.root / "other"))
        self.assertFalse(result["check_persisted"])
        self.assertEqual(self.manager.path.read_bytes(), before)
        started, release = asyncio.Event(), asyncio.Event()

        async def checking(_):
            started.set()
            await release.wait()
            return {"ok": True}

        with patch.object(self.manager, "_check_settings", new=checking):
            task = asyncio.create_task(
                self.manager.check("rapidocr", sys.executable, str(self.models))
            )
            await started.wait()
            await self.manager.save({**self.payload, "revision": self.manager.status()["revision"]})
            release.set()
            self.assertFalse((await task)["check_persisted"])
        self.assertIsNone(self.manager.status()["check_ok"])

    async def test_metadata_check_missing_hash_version_and_no_model_import(self):
        probe = self.root / "probe"
        probe.write_text(
            f"#!{sys.executable}\nimport sys,json\nfrom pathlib import Path\n"
            f"Path({str(self.root / 'probe_code')!r}).write_text(sys.argv[-1])\n"
            "print(json.dumps({'python':[3,12,1],'versions':{'rapidocr':'3.10.0','onnxruntime':'1.31.1'}}))\n"
        )
        probe.chmod(0o700)
        payload = {**self.payload, "python": str(probe)}
        await self.manager.save(payload)
        result = await self.manager.check("rapidocr", str(probe), str(self.models))
        self.assertFalse(result["ok"])
        self.assertIn("缺少本地模型", result["message"])
        code = (self.root / "probe_code").read_text()
        self.assertIn("importlib.metadata", code)
        self.assertNotIn("import rapidocr", code)
        content = b"stub model"
        model = self.models / "stub.onnx"
        model.write_bytes(content)
        with patch.object(ocr, "MODEL_HASHES", {"stub.onnx": hashlib.sha256(content).hexdigest()}):
            result = await self.manager.check("rapidocr", str(probe), str(self.models))
            self.assertTrue(result["ok"])
            self.assertTrue(self.manager.status()["check_ok"])
            model.write_bytes(b"tampered")
            result = await self.manager.check("rapidocr", str(probe), str(self.models))
            self.assertFalse(result["ok"])
            self.assertIn("SHA-256", result["message"])
        probe.write_text(probe.read_text().replace("'3.10.0'", "'0.0.0'"))
        result = await self.manager.check("rapidocr", str(probe), str(self.models))
        self.assertIn("依赖版本", result["message"])

    async def test_recognition_preserves_partial_and_isolated_args(self):
        await self.ready()
        revision = self.manager.status()["revision"]
        for state in ("accepted", "partial", "rejected"):
            (self.models / "mode").write_text(state)
            before_path, before_env = sys.path[:], dict(os.environ)
            with patch(
                "asyncio.create_subprocess_exec", wraps=asyncio.create_subprocess_exec
            ) as spawn:
                result = await self.manager.recognize(b"image", expected_revision=revision)
            self.assertEqual(spawn.call_args.args[:3], (sys.executable, "-I", "-B"))
            self.assertTrue(spawn.call_args.kwargs["start_new_session"])
            self.assertNotIn("env", spawn.call_args.kwargs)
            self.assertNotIn("shell", spawn.call_args.kwargs)
            self.assertEqual(result["state"], state)
            self.assertEqual(result["ok"], state == "accepted")
            self.assertEqual(result["structured"], {"weapon": "测试"})
            self.assertEqual(result["reasons"], ["保留原因"])
            self.assertEqual(result["configuration_revision"], revision)
            self.assertEqual(sys.path, before_path)
            self.assertEqual(dict(os.environ), before_env)
            self.assertFalse(alive(await self.wait_pid()))

    async def test_stale_confirmation_rejects_before_resources_or_spawn(self):
        await self.ready()
        stale = self.manager.status()["revision"]
        await self.manager.enable(False, stale)
        await self.manager.enable(True, self.manager.status()["revision"])
        current = self.manager.status()["revision"]
        with (
            patch.object(ocr, "resource_snapshot") as resources,
            patch("asyncio.create_subprocess_exec") as spawn,
            patch.object(self.manager, "_try_run_slot") as slot,
        ):
            for expected in (stale, True):
                result = await self.manager.recognize(b"image", expected_revision=expected)
                self.assertEqual(result["state"], "revision_conflict")
                self.assertFalse(result["ok"])
                self.assertEqual(result["configuration_revision"], current)
            resources.assert_not_called()
            slot.assert_not_called()
            spawn.assert_not_called()
        self.assertFalse(self.manager._busy)
        # 聊天调用不携带确认修订号，继续按当前配置识别。
        result = await self.manager.recognize(b"image")
        self.assertTrue(result["ok"])
        self.assertEqual(result["configuration_revision"], current)

    async def test_configuration_change_during_resources_still_prevents_spawn(self):
        await self.ready()
        revision = self.manager.status()["revision"]

        def change_configuration():
            data = self.manager._read()
            data.update(enabled=False, revision=data["revision"] + 1)
            self.manager._write(data)
            return ENOUGH

        with (
            patch.object(ocr, "resource_snapshot", side_effect=change_configuration),
            patch("asyncio.create_subprocess_exec") as spawn,
        ):
            result = await self.manager.recognize(b"image", expected_revision=revision)
            spawn.assert_not_called()
        self.assertEqual(result["state"], "disabled")
        self.assertEqual(result["configuration_revision"], revision)
        self.assertEqual(self.manager.status()["revision"], revision + 1)

    async def test_busy_cancel_and_process_group_kill(self):
        await self.ready()
        (self.models / "mode").write_text("descendant")
        task = asyncio.create_task(self.manager.recognize(b"image"))
        try:
            pid = await self.wait_pid()
            child = await self.wait_pid(self.models / "child_pid")
            self.assertEqual((await self.manager.recognize(b"image"))["state"], "busy")
            other = ocr.LocalRivenOCR(self.manager.path.parent, self.plugin)
            self.assertEqual((await other.recognize(b"image"))["state"], "busy")
            await other.close()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertFalse(alive(pid))
            for _ in range(100):
                if not alive(child):
                    break
                await asyncio.sleep(0.01)
            self.assertFalse(alive(child))
            self.assertFalse(self.manager._busy)
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_timeout_output_limit_disable_and_close_cleanup(self):
        await self.ready()
        (self.models / "mode").write_text("sleep")
        real_wait_for = asyncio.wait_for

        async def recognize_timeout_after_ready(awaitable, timeout):
            if timeout != ocr.RECOGNIZE_TIMEOUT:
                return await real_wait_for(awaitable, timeout)
            exchange = asyncio.create_task(awaitable)
            try:
                await self.wait_pid()
                return await real_wait_for(exchange, 0)
            finally:
                if not exchange.done():
                    exchange.cancel()
                await asyncio.gather(exchange, return_exceptions=True)

        with patch("asyncio.wait_for", new=recognize_timeout_after_ready):
            result = await self.manager.recognize(b"image")
        self.assertEqual(result["state"], "timeout")
        self.assertFalse(alive(await self.wait_pid()))
        (self.models / "mode").write_text("oversize")
        result = await self.manager.recognize(b"image")
        self.assertIn("输出超限", result["message"])
        for method in ("disable", "close"):
            (self.models / "mode").write_text("sleep")
            (self.models / "pid").unlink()
            task = asyncio.create_task(self.manager.recognize(b"image"))
            try:
                pid = await self.wait_pid()
                if method == "disable":
                    await self.manager.enable(False, self.manager.status()["revision"])
                    await self.manager.enable(True, self.manager.status()["revision"])
                else:
                    await self.manager.close()
                await task
                self.assertFalse(alive(pid))
            finally:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.assertEqual((await self.manager.recognize(b"image"))["state"], "closed")

    async def test_cancel_during_spawn_still_reaps_process(self):
        await self.ready()
        original = asyncio.create_subprocess_exec
        started = asyncio.Event()

        async def delayed(*args, **kwargs):
            started.set()
            await asyncio.sleep(0.08)
            return await original(*args, **kwargs)

        with patch("asyncio.create_subprocess_exec", new=delayed):
            task = asyncio.create_task(self.manager.recognize(b"image"))
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertIsNone(self.manager._active)
        self.assertFalse(self.manager._busy)

    async def test_check_timeout_and_cancel_reap_metadata_process(self):
        probe = self.root / "slow-python"
        pid_path = self.root / "metadata-pid"
        probe.write_text(
            f"#!{sys.executable}\nimport os,time,signal\nfrom pathlib import Path\n"
            "signal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
            f"p=Path({str(pid_path)!r}); tmp=p.with_suffix('.ready')\n"
            "tmp.write_text(str(os.getpid())); os.replace(tmp,p)\n"
            "time.sleep(30)\n"
        )
        probe.chmod(0o700)
        await self.manager.save({**self.payload, "python": str(probe)})
        real_wait_for = asyncio.wait_for
        real_spawn = asyncio.create_subprocess_exec
        spawn_entered = asyncio.Event()
        allow_spawn = asyncio.Event()
        deadline_entered = asyncio.Event()

        async def ready_pid():
            return await self.wait_pid(pid_path)

        async def delayed_spawn(*args, **kwargs):
            spawn_entered.set()
            await allow_spawn.wait()
            return await real_spawn(*args, **kwargs)

        async def timeout_after_ready(awaitable, timeout):
            if timeout != ocr.CHECK_TIMEOUT:
                return await real_wait_for(awaitable, timeout)
            deadline_entered.set()
            await real_wait_for(ready_pid(), 10)
            # Drive exactly the outer check deadline after a live SIGTERM-ignoring
            # process acknowledges readiness. The inner eight-second collector is real.
            raise asyncio.TimeoutError

        task = None
        try:
            with (
                patch("asyncio.create_subprocess_exec", new=delayed_spawn),
                patch("asyncio.wait_for", new=timeout_after_ready),
            ):
                task = asyncio.create_task(
                    self.manager.check("rapidocr", str(probe), str(self.models))
                )
                await real_wait_for(spawn_entered.wait(), 10)
                await real_wait_for(deadline_entered.wait(), 10)
                # Deterministically hold startup until after the deadline wrapper
                # is waiting: no elapsed-time assumption can make the PID appear.
                self.assertFalse(pid_path.exists())
                self.assertFalse(task.done())
                allow_spawn.set()
                result = await real_wait_for(task, 15)
            self.assertEqual(result["state"], "check_timeout")
            self.assertFalse(alive(await ready_pid()))
            self.assertFalse(self.manager.status()["check_ok"])
        finally:
            allow_spawn.set()
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        pid_path.unlink()
        task = asyncio.create_task(self.manager.check("rapidocr", str(probe), str(self.models)))
        try:
            pid = await real_wait_for(ready_pid(), 10)
            task.cancel()
            await asyncio.sleep(0.03)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertFalse(alive(pid))
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_check_repeated_cancel_waits_for_owned_finally_and_cancels_once(self):
        entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        cancellations = []

        async def checking(_):
            try:
                entered.set()
                await asyncio.Future()
            except asyncio.CancelledError:
                cancellations.append("cancel")
                raise
            finally:
                cleaning.set()
                await release.wait()

        with patch.object(self.manager, "_check_settings", new=checking):
            task = asyncio.create_task(
                self.manager.check("rapidocr", sys.executable, str(self.models))
            )
            await entered.wait()
            task.cancel()
            await cleaning.wait()
            task.cancel()
            # 只让已排队的取消执行一轮，不用延长sleep掩盖进程清理竞态。
            checkpoint = asyncio.Event()
            asyncio.get_running_loop().call_soon(checkpoint.set)
            await checkpoint.wait()
            premature = task.done()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertFalse(premature, "调用方不能在所属检查finally完成前返回取消")
        self.assertEqual(cancellations, ["cancel"])
        self.assertFalse(self.manager._check_tasks)
        self.assertFalse(self.manager._check_cancelled)

    def slow_metadata_probe(self):
        probe = self.root / "spawn-slow-python"
        probe.write_text(
            f"#!{sys.executable}\nimport time,signal\n"
            "signal.signal(signal.SIGTERM,signal.SIG_IGN)\ntime.sleep(30)\n"
        )
        probe.chmod(0o700)
        return probe

    async def test_metadata_spawn_cancel_still_waits_for_handle_and_reaps(self):
        probe = self.slow_metadata_probe()
        original_spawn = asyncio.create_subprocess_exec
        original_finish = self.manager._finish_check
        spawned, cleanup, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        processes = []

        async def gated_spawn(*args, **kwargs):
            process = await original_spawn(*args, **kwargs)
            processes.append(process)
            spawned.set()
            await release.wait()
            return process

        async def finishing(task):
            cleanup.set()
            await original_finish(task)

        with (
            patch("asyncio.create_subprocess_exec", new=gated_spawn),
            patch.object(self.manager, "_finish_check", new=finishing),
        ):
            task = asyncio.create_task(self.manager.check("rapidocr", str(probe), str(self.models)))
            await spawned.wait()
            task.cancel()
            await cleanup.wait()
            task.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(len(processes), 1)
        self.assertFalse(alive(processes[0].pid))
        self.assertIsNotNone(processes[0].returncode)
        self.assertFalse(self.manager._check_tasks)

    async def test_close_during_metadata_spawn_waits_and_rejects_later_check(self):
        probe = self.slow_metadata_probe()
        original_spawn = asyncio.create_subprocess_exec
        original_cancel = self.manager._cancel_check_once
        spawned, cancelling, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        processes = []

        async def gated_spawn(*args, **kwargs):
            process = await original_spawn(*args, **kwargs)
            processes.append(process)
            spawned.set()
            await release.wait()
            return process

        def cancel(task):
            original_cancel(task)
            cancelling.set()

        with (
            patch("asyncio.create_subprocess_exec", new=gated_spawn),
            patch.object(self.manager, "_cancel_check_once", new=cancel),
        ):
            checking = asyncio.create_task(
                self.manager.check("rapidocr", str(probe), str(self.models))
            )
            await spawned.wait()
            closing = asyncio.create_task(self.manager.close())
            await cancelling.wait()
            self.assertFalse(closing.done())
            release.set()
            await closing
            with self.assertRaises(asyncio.CancelledError):
                await checking
            result = await self.manager.check("rapidocr", str(probe), str(self.models))
        self.assertEqual(result["state"], "closed")
        self.assertEqual(len(processes), 1)
        self.assertFalse(alive(processes[0].pid))
        self.assertIsNotNone(processes[0].returncode)
        self.assertFalse(self.manager._check_tasks)

    async def test_repeated_cancel_during_worker_cleanup(self):
        await self.ready()
        (self.models / "mode").write_text("sleep")
        task = asyncio.create_task(self.manager.recognize(b"image"))
        try:
            pid = await self.wait_pid()
            task.cancel()
            await asyncio.sleep(0.03)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertFalse(alive(pid))
            self.assertIsNone(self.manager._active)
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_resource_and_size_fail_without_spawn(self):
        await self.ready()
        with patch("asyncio.create_subprocess_exec", side_effect=AssertionError("禁止启动")):
            self.assertEqual((await self.manager.recognize(b""))["state"], "invalid_image")
            self.assertEqual(
                (await self.manager.recognize(b"x" * (ocr.MAX_IMAGE_BYTES + 1)))["state"],
                "invalid_image",
            )
            with patch.object(
                ocr, "resource_snapshot", return_value={"ok": False, "message": "不足"}
            ):
                self.assertEqual((await self.manager.recognize(b"i"))["state"], "resources")


class ResourceAndWorkerTests(unittest.TestCase):
    def test_ram_swap_and_cgroup_headroom(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            proc, group = root / "proc", root / "cgroup"
            (proc / "self").mkdir(parents=True)
            group.mkdir()
            (proc / "meminfo").write_text("MemAvailable: 437248 kB\nSwapFree: 2097152 kB\n")
            (proc / "self/cgroup").write_text("0::/container\n")
            self.assertTrue(ocr.resource_snapshot(proc, group)["ok"])
            (group / "memory.max").write_text(str(1024 * ocr.MIB))
            (group / "memory.current").write_text(str(800 * ocr.MIB))
            self.assertFalse(ocr.resource_snapshot(proc, group)["ok"])
            (group / "memory.current").write_text(str(600 * ocr.MIB))
            (group / "memory.swap.max").write_text("0")
            (group / "memory.swap.current").write_text("0")
            self.assertFalse(ocr.resource_snapshot(proc, group)["ok"])
            (group / "memory.swap.max").write_text(str(2048 * ocr.MIB))
            self.assertTrue(ocr.resource_snapshot(proc, group)["ok"])

    def test_downloader_and_network_audit_deny(self):
        with self.assertRaises(PermissionError):
            worker.deny_download({"url": "https://example.invalid/model"})
        for event in ("socket.connect", "socket.getaddrinfo", "socket.sendto", "subprocess.Popen"):
            with self.assertRaises(PermissionError):
                worker.network_guard(event, ())
        worker.network_guard("open", ())
        # 在真实子进程中安装审计钩子，socket.connect 不会到达操作系统网络层。
        import subprocess

        code = (
            "import importlib.util,socket,sys;"
            f"s=importlib.util.spec_from_file_location('w',{str(ROOT / 'core/local_ocr_worker.py')!r});"
            "w=importlib.util.module_from_spec(s);s.loader.exec_module(w);sys.addaudithook(w.network_guard);"
            "socket.create_connection(('127.0.0.1',9))"
        )
        result = subprocess.run(
            [sys.executable, "-I", "-B", "-c", code], capture_output=True, timeout=5
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("禁止访问网络".encode(), result.stderr)


if __name__ == "__main__":
    result = unittest.main(exit=False).result
    if not result.wasSuccessful():
        raise SystemExit(1)
    print("ALL PASS")
