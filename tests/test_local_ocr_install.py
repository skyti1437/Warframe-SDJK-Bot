"""冻结安装器的离线资源/生命周期测试；子进程和下载均用桩替代。"""

import asyncio
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.local_ocr_install import LocalOCRInstaller, MARKER, _official_url


class InstallerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.plugin = self.base / "plugin"
        (self.plugin / "core" / "data").mkdir(parents=True)
        self.installer = LocalOCRInstaller(self.base / "data", self.plugin)
        self.addAsyncCleanup(self.installer.close)
        self.payloads = {
            "demo-1.0-py3-none-any.whl": b"frozen wheel",
            "model.onnx": b"frozen model",
        }
        self.manifest = {
            "platform": {"system": "Linux", "machine": "x86_64", "python": "3.12"},
            "dependencies": [
                {
                    "name": "demo",
                    "version": "1.0",
                    "filename": "demo-1.0-py3-none-any.whl",
                    "url": "https://files.pythonhosted.org/packages/demo.whl",
                }
            ],
            "models": [
                {
                    "filename": "model.onnx",
                    "url": "https://www.modelscope.cn/models/fixed/model.onnx",
                }
            ],
        }
        for kind in ("dependencies", "models"):
            for entry in self.manifest[kind]:
                entry["sha256"] = hashlib.sha256(self.payloads[entry["filename"]]).hexdigest()
        self.save_manifest()
        supported = patch.object(LocalOCRInstaller, "_supported", return_value=True)
        supported.start()
        self.addCleanup(supported.stop)
        self.commands = []

    async def start_current(self):
        return await self.installer.start(True, expected_job_id=self.installer.status()["job_id"])

    async def test_job_id_status_unconfirmed_and_closed_are_side_effect_free(self):
        job = self.installer.status()["job_id"]
        self.assertRegex(job, r"^[0-9a-f]{32}$")
        other = LocalOCRInstaller(self.base / "other", self.plugin)
        self.assertNotEqual(job, other.status()["job_id"])
        with (
            patch.object(self.installer, "_manifest", side_effect=AssertionError("不得读清单")),
            patch(
                "core.local_ocr_install.httpx.AsyncClient", side_effect=AssertionError("不得联网")
            ),
            patch("asyncio.create_subprocess_exec", side_effect=AssertionError("不得安装")),
        ):
            for _ in range(3):
                self.assertEqual(self.installer.status()["job_id"], job)
            out = await self.installer.start(False, expected_job_id="stale")
            self.assertEqual(out["reason"], "confirmation_required")
            await self.installer.close()
            self.assertEqual((await self.installer.start(True))["reason"], "closed")
            self.assertEqual(self.installer.status()["job_id"], job)
        self.assertFalse(self.installer.root.exists())

    async def test_job_conflict_rejects_missing_invalid_and_stale_without_work(self):
        before = self.installer.status()
        with (
            patch.object(self.installer, "_supported", side_effect=AssertionError("不得检查安装")),
            patch.object(self.installer, "_manifest", side_effect=AssertionError("不得读清单")),
            patch("asyncio.create_task", side_effect=AssertionError("不得排队")),
        ):
            for kwargs in (
                {},
                *({"expected_job_id": value} for value in (None, 1, True, [], {}, "", "0" * 32)),
            ):
                out = await self.installer.start(True, **kwargs)
                self.assertEqual(out, {**before, "accepted": False, "reason": "job_conflict"})
        self.assertFalse(self.installer.root.exists())

    async def test_same_observed_id_cannot_restart_after_instant_completion(self):
        observed = self.installer.status()["job_id"]

        async def instant(*args):
            self.installer._job.update(state="succeeded", progress=100)

        with patch.object(self.installer, "_run", side_effect=instant) as run:
            first = await self.installer.start(True, expected_job_id=observed)
            self.assertTrue(first["accepted"])
            self.assertNotEqual(first["job_id"], observed)
            await self.installer._task
            second = await self.installer.start(True, expected_job_id=observed)
            self.assertEqual(second["reason"], "job_conflict")
            self.assertEqual(second["job_id"], first["job_id"])
            run.assert_awaited_once()
            third = await self.installer.start(True, expected_job_id=first["job_id"])
            self.assertTrue(third["accepted"])
            self.assertNotEqual(third["job_id"], first["job_id"])
            await self.installer._task

    async def test_busy_keeps_current_id_and_old_id_conflicts(self):
        observed = self.installer.status()["job_id"]
        with patch.object(self.installer, "_run", side_effect=lambda *args: asyncio.sleep(100)):
            first = await self.installer.start(True, expected_job_id=observed)
            current = first["job_id"]
            busy = await self.installer.start(True, expected_job_id=current)
            self.assertEqual(busy["reason"], "busy")
            stale = await self.installer.start(True, expected_job_id=observed)
            self.assertEqual(stale["reason"], "job_conflict")
            self.assertEqual(self.installer.status()["job_id"], current)
            await self.installer.close()
            self.assertEqual(self.installer.status()["job_id"], current)

    async def test_rejected_attempt_allocates_new_id_without_rewriting_previous_job(self):
        await self.complete_cached()
        previous = dict(self.installer._job)
        with patch.object(self.installer, "_supported", return_value=False):
            out = await self.installer.start(True, expected_job_id=previous["job_id"])
        self.assertEqual(out["reason"], "unsupported")
        self.assertFalse(out["accepted"])
        self.assertNotEqual(out["job_id"], previous["job_id"])
        self.assertEqual(previous["state"], "succeeded")
        with patch.object(self.installer, "_manifest", side_effect=ValueError("坏清单")):
            failed = await self.installer.start(True, expected_job_id=out["job_id"])
        self.assertEqual(failed["reason"], "invalid_manifest")
        self.assertFalse(failed["accepted"])
        self.assertNotEqual(failed["job_id"], out["job_id"])

    async def test_real_run_progress_success_failure_and_cancel_keep_id(self):
        states = []

        async def asset(*args):
            states.append(self.installer.status())

        async def command(args):
            states.append(self.installer.status())
            return await self.command(args)

        with (
            patch.object(self.installer, "_asset", side_effect=asset),
            patch.object(self.installer, "_command", side_effect=command),
        ):
            out = await self.start_current()
            await self.installer._task
        states.append(self.installer.status())
        self.assertEqual(
            {s["state"] for s in states},
            {"downloading", "creating_venv", "installing", "succeeded"},
        )
        self.assertEqual({s["job_id"] for s in states}, {out["job_id"]})
        with patch.object(self.installer, "_asset", side_effect=ValueError("失败桩")):
            failed = await self.start_current()
            await self.installer._task
        self.assertEqual(self.installer.status()["state"], "failed")
        self.assertEqual(self.installer.status()["job_id"], failed["job_id"])
        with patch.object(self.installer, "_asset", side_effect=lambda *args: asyncio.sleep(100)):
            cancelled = await self.start_current()
            await self.installer.close()
        self.assertEqual(self.installer.status()["state"], "cancelled")
        self.assertEqual(self.installer.status()["job_id"], cancelled["job_id"])

    def save_manifest(self):
        self.installer.manifest_path.write_text(json.dumps(self.manifest), encoding="utf-8")

    def cached(self):
        for kind, directory in (
            ("dependencies", self.installer.wheels),
            ("models", self.installer.models),
        ):
            directory.mkdir(parents=True, exist_ok=True)
            for entry in self.manifest[kind]:
                (directory / entry["filename"]).write_bytes(self.payloads[entry["filename"]])

    async def command(self, args):
        self.commands.append(list(map(str, args)))
        return json.dumps({"demo": "1.0"}) if "-c" in args else ""

    async def complete_cached(self):
        self.cached()
        with (
            patch.object(self.installer, "_command", side_effect=self.command),
            patch.object(
                httpx.AsyncClient, "stream", side_effect=AssertionError("缓存命中不得下载")
            ),
        ):
            response = await self.start_current()
            self.assertTrue(response["accepted"])
            await self.installer._task

    async def test_construction_status_and_unconfirmed_start_have_no_side_effect(self):
        with (
            patch(
                "core.local_ocr_install.httpx.AsyncClient", side_effect=AssertionError("不能下载")
            ),
            patch("asyncio.create_subprocess_exec", side_effect=AssertionError("不能安装")),
        ):
            self.assertEqual(self.installer.status()["state"], "idle")
            self.assertFalse((await self.installer.start(False))["accepted"])
            self.assertFalse((await self.installer.start(1))["accepted"])
            self.assertFalse(self.installer.root.exists())

    async def test_cached_install_is_venv_only_and_never_enables(self):
        await self.complete_cached()
        status = self.installer.status()
        self.assertEqual(status["state"], "succeeded")
        self.assertEqual(
            status["recommended_python"], str(self.installer.runtime / "bin" / "python")
        )
        self.assertEqual(
            self.commands[0],
            [sys.executable, "-I", "-B", "-m", "venv", str(self.installer.runtime)],
        )
        install = self.commands[1]
        self.assertEqual(install[0], status["recommended_python"])
        for flag in ("--no-index", "--no-deps", "--only-binary=:all:"):
            self.assertIn(flag, install)
        self.assertIn(
            str(self.installer.wheels / self.manifest["dependencies"][0]["filename"]), install
        )
        self.assertFalse(
            any("enabled" in p.name or "config" in p.name for p in self.base.rglob("*"))
        )

    async def test_successful_owned_runtime_reuses_after_version_check(self):
        await self.complete_cached()
        self.commands.clear()
        with patch.object(self.installer, "_command", side_effect=self.command):
            await self.start_current()
            await self.installer._task
        self.assertEqual(self.installer.status()["state"], "succeeded")
        self.assertEqual(len(self.commands), 1)
        self.assertIn("-c", self.commands[0])

    async def test_successful_runtime_is_reusable_by_new_instance_after_reload(self):
        await self.complete_cached()
        await self.installer.close()
        reloaded = LocalOCRInstaller(self.base / "data", self.plugin)
        self.addAsyncCleanup(reloaded.close)
        self.assertEqual(reloaded.status()["state"], "idle")
        self.assertNotEqual(reloaded.status()["job_id"], self.installer.status()["job_id"])
        self.commands.clear()
        with patch.object(reloaded, "_command", side_effect=self.command):
            await reloaded.start(True, expected_job_id=reloaded.status()["job_id"])
            await reloaded._task
        self.assertEqual(reloaded.status()["state"], "succeeded")
        self.assertEqual(len(self.commands), 1)
        self.assertIn("-c", self.commands[0])

    async def test_request_cancellation_does_not_cancel_background_install(self):
        started, release, response_sent = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def blocked(*args):
            started.set()
            await release.wait()

        async def request():
            response = await self.start_current()
            self.assertTrue(response["accepted"])
            response_sent.set()
            await asyncio.Future()

        with (
            patch.object(self.installer, "_asset", side_effect=blocked),
            patch.object(self.installer, "_runtime", new_callable=AsyncMock) as runtime,
        ):
            caller = asyncio.create_task(request())
            await response_sent.wait()
            await started.wait()
            caller.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await caller
            self.assertFalse(self.installer._task.done())
            release.set()
            await self.installer._task
        self.assertEqual(self.installer.status()["state"], "succeeded")
        runtime.assert_awaited_once()

    async def test_existing_unowned_runtime_is_never_overwritten(self):
        self.cached()
        self.installer.runtime.mkdir()
        sentinel = self.installer.runtime / "user.txt"
        sentinel.write_text("用户环境")
        with patch.object(self.installer, "_command", side_effect=AssertionError("不应执行")):
            await self.start_current()
            await self.installer._task
        self.assertEqual(self.installer.status()["state"], "failed")
        self.assertEqual(sentinel.read_text(), "用户环境")
        self.assertFalse((self.installer.runtime / MARKER).exists())

    async def test_bad_cache_sha_fails_before_install_and_preserves_file(self):
        self.cached()
        wheel = self.installer.wheels / self.manifest["dependencies"][0]["filename"]
        wheel.write_bytes(b"wrong")
        with patch.object(self.installer, "_command", side_effect=AssertionError("不得安装")):
            await self.start_current()
            await self.installer._task
        self.assertEqual(self.installer.status()["state"], "failed")
        self.assertIn("SHA256", self.installer.status()["message"])
        self.assertEqual(wheel.read_bytes(), b"wrong")

    async def test_busy_and_close_cancel_inflight_job(self):
        entered = asyncio.Event()

        async def blocked(*args):
            entered.set()
            await asyncio.Future()

        with patch.object(self.installer, "_asset", side_effect=blocked):
            await self.start_current()
            await entered.wait()
            self.assertEqual((await self.start_current())["reason"], "busy")
            await self.installer.close()
        self.assertEqual(self.installer.status()["state"], "cancelled")
        self.assertFalse(self.installer.status()["running"])
        self.assertEqual((await self.start_current())["reason"], "closed")

    async def test_close_before_background_first_step_sets_cancelled(self):
        await self.start_current()
        await self.installer.close()
        self.assertEqual(self.installer.status()["state"], "cancelled")
        self.assertFalse(self.installer.status()["running"])

    async def test_unsupported_platform_has_no_download_or_subprocess(self):
        with (
            patch.object(LocalOCRInstaller, "_supported", return_value=False),
            patch.object(self.installer, "_asset", side_effect=AssertionError("不得下载")),
        ):
            out = await self.start_current()
        self.assertEqual(out["state"], "unsupported")
        self.assertFalse(out["accepted"])

    async def test_untrusted_manifest_url_rejected_before_job(self):
        self.manifest["models"][0]["url"] = "https://evil.example/model.onnx"
        self.save_manifest()
        out = await self.start_current()
        self.assertFalse(out["accepted"])
        self.assertEqual(out["state"], "failed")
        self.assertFalse(self.installer.root.exists())

    async def test_download_only_frozen_url_then_checks_sha(self):
        _, files, _ = self.installer._manifest()
        kind, entry = files[0]
        seen = []

        def respond(request):
            seen.append(str(request.url))
            return httpx.Response(200, content=self.payloads[entry["filename"]])

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            await self.installer._asset(client, kind, entry)
        self.assertEqual(seen, [entry["url"]])
        self.assertEqual(
            (self.installer.wheels / entry["filename"]).read_bytes(),
            self.payloads[entry["filename"]],
        )
        self.assertEqual(list(self.installer.wheels.glob("*.part")), [])

    async def test_bad_download_sha_and_cross_origin_redirect_leave_no_target(self):
        _, files, _ = self.installer._manifest()
        kind, entry = files[0]
        for response in (
            httpx.Response(200, content=b"wrong"),
            httpx.Response(302, headers={"location": "https://pypi.org/other.whl"}),
        ):
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(lambda _: response)
            ) as client:
                with self.assertRaises(ValueError):
                    await self.installer._asset(client, kind, entry)
            self.assertFalse((self.installer.wheels / entry["filename"]).exists())
            self.assertFalse(any(self.installer.wheels.iterdir()))

    async def test_modelscope_precise_cdn_redirect_downloads_and_requires_full_sha(self):
        entry = self.manifest["models"][0]
        # 形态来自首尔实际302：精确CDN、对象路径及临时签名查询参数。
        cdn = (
            "https://cdn-lfs-cn-1.modelscope.cn/prod/lfs-objects/e4/7a/"
            "cedf663230f8863ff1ab0e64dd2d82b838fceb5957146dab185a89d6215c"
            "?filename=model.onnx&namespace=RapidAI&repository=RapidOCR"
            "&revision=v3.10.0&tag=model&auth_key=fixture-signature"
        )
        for body, valid in ((b"wrong model", False), (self.payloads[entry["filename"]], True)):
            with self.subTest(valid_sha=valid):
                seen = []

                def respond(request):
                    seen.append(str(request.url))
                    if len(seen) == 1:
                        return httpx.Response(302, headers={"location": cdn})
                    return httpx.Response(200, content=body)

                async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
                    if valid:
                        await self.installer._asset(client, "models", entry)
                    else:
                        with self.assertRaisesRegex(ValueError, "SHA256"):
                            await self.installer._asset(client, "models", entry)
                self.assertEqual(seen, [entry["url"], cdn])
                target = self.installer.models / entry["filename"]
                self.assertEqual(target.exists(), valid)
                if valid:
                    self.assertEqual(target.read_bytes(), body)
                self.assertFalse(list(self.installer.models.glob(".*.part")))

    async def test_cdn_exception_rejects_other_origins_paths_and_protocols(self):
        model = self.manifest["models"][0]
        wheel = self.manifest["dependencies"][0]
        permitted = "https://cdn-lfs-cn-1.modelscope.cn/prod/lfs-objects/e4/7a/object"
        cases = [
            ("dependencies", wheel, permitted),
            ("models", {**model, "url": "https://files.pythonhosted.org/model.onnx"}, permitted),
            ("models", model, "https://cdn-lfs-cn-2.modelscope.cn/prod/lfs-objects/object"),
            ("models", model, "https://evil.modelscope.cn/prod/lfs-objects/object"),
            (
                "models",
                model,
                "https://cdn-lfs-cn-1.modelscope.cn.evil.example/prod/lfs-objects/object",
            ),
            ("models", model, "http://cdn-lfs-cn-1.modelscope.cn/prod/lfs-objects/object"),
            ("models", model, "https://cdn-lfs-cn-1.modelscope.cn:8443/prod/lfs-objects/object"),
            ("models", model, "https://u:p@cdn-lfs-cn-1.modelscope.cn/prod/lfs-objects/object"),
            ("models", model, "https://@cdn-lfs-cn-1.modelscope.cn/prod/lfs-objects/object"),
            ("models", model, permitted + "#fragment"),
            ("models", model, "https://cdn-lfs-cn-1.modelscope.cn/other/object"),
            ("models", model, "https://cdn-lfs-cn-1.modelscope.cn/prod/lfs-objects-evil/object"),
            ("models", model, "https://cdn-lfs-cn-1.modelscope.cn/prod/lfs-objects/"),
            ("models", model, "https://cdn-lfs-cn-1.modelscope.cn/prod/lfs-objects/%2e%2e/other"),
        ]
        for kind, entry, target in cases:
            with self.subTest(kind=kind, source=entry["url"], target=target):
                seen = []

                def respond(request):
                    seen.append(str(request.url))
                    return httpx.Response(302, headers={"location": target})

                async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
                    with self.assertRaisesRegex(ValueError, "重定向"):
                        await self.installer._asset(client, kind, entry)
                self.assertEqual(seen, [entry["url"]])
                directory = (
                    self.installer.wheels if kind == "dependencies" else self.installer.models
                )
                self.assertFalse(any(directory.iterdir()))
        # CDN不能成为新的冻结入口，初始URL白名单没有扩大。
        with self.assertRaises(ValueError):
            _official_url(permitted)

    async def test_same_origin_wheel_redirect_still_allowed_and_cdn_cannot_chain_elsewhere(self):
        entry = self.manifest["dependencies"][0]
        seen = []

        def wheel_response(request):
            seen.append(str(request.url))
            return (
                httpx.Response(302, headers={"location": "/packages/fixed.whl"})
                if len(seen) == 1
                else httpx.Response(200, content=self.payloads[entry["filename"]])
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(wheel_response)) as client:
            await self.installer._asset(client, "dependencies", entry)
        self.assertEqual(seen, [entry["url"], "https://files.pythonhosted.org/packages/fixed.whl"])
        model = self.manifest["models"][0]
        cdn = "https://cdn-lfs-cn-1.modelscope.cn/prod/lfs-objects/e4/7a/object"
        for target in (
            "https://pypi.org/model.onnx",
            "https://cdn-lfs-cn-2.modelscope.cn/prod/lfs-objects/object",
            model["url"],
        ):
            with self.subTest(next_target=target):
                seen = []

                def model_response(request):
                    seen.append(str(request.url))
                    return httpx.Response(
                        302, headers={"location": cdn if len(seen) == 1 else target}
                    )

                async with httpx.AsyncClient(
                    transport=httpx.MockTransport(model_response)
                ) as client:
                    with self.assertRaisesRegex(ValueError, "重定向"):
                        await self.installer._asset(client, "models", model)
                self.assertEqual(seen, [model["url"], cdn])
                self.assertFalse(any(self.installer.models.iterdir()))

    async def test_download_size_limit_leaves_no_partial_file(self):
        _, files, _ = self.installer._manifest()
        kind, entry = files[0]
        with patch("core.local_ocr_install.MAX_ASSET_BYTES", 3):
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"oversized"))
            ) as client:
                with self.assertRaises(ValueError):
                    await self.installer._asset(client, kind, entry)
        self.assertFalse(any(self.installer.wheels.iterdir()))

    async def test_cancelling_download_closes_response_and_removes_own_partial(self):
        _, files, _ = self.installer._manifest()
        kind, entry = files[0]
        entered = asyncio.Event()

        class BlockedBody(httpx.AsyncByteStream):
            closed = False

            async def __aiter__(self):
                yield b"first"
                entered.set()
                await asyncio.Future()

            async def aclose(self):
                self.closed = True

        body = BlockedBody()
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=body))
        ) as client:
            task = asyncio.create_task(self.installer._asset(client, kind, entry))
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(body.closed)
        self.assertFalse(any(self.installer.wheels.iterdir()))

    async def test_install_failure_keeps_only_its_owned_diagnostics(self):
        self.cached()
        with patch.object(self.installer, "_command", side_effect=ValueError("ensurepip 不可用")):
            await self.start_current()
            await self.installer._task
        self.assertEqual(self.installer.status()["state"], "failed")
        marker = json.loads((self.installer.runtime / MARKER).read_text())
        self.assertEqual(marker["state"], "failed")
        with patch.object(
            self.installer, "_command", side_effect=AssertionError("不能覆盖失败目录")
        ):
            await self.start_current()
            await self.installer._task
        self.assertEqual(self.installer.status()["state"], "failed")

    async def test_version_mismatch_in_owned_runtime_is_not_reused(self):
        await self.complete_cached()
        with patch.object(self.installer, "_command", return_value='{"demo":"0.9"}'):
            await self.start_current()
            await self.installer._task
        self.assertEqual(self.installer.status()["state"], "failed")
        self.assertIn("实际依赖版本", self.installer.status()["message"])

    async def test_install_timeout_becomes_visible_failure(self):
        self.cached()
        with patch.object(self.installer, "_runtime", side_effect=asyncio.TimeoutError("安装超时")):
            await self.start_current()
            await self.installer._task
        self.assertEqual(self.installer.status()["state"], "failed")
        self.assertIn("超时", self.installer.status()["message"])

    async def test_python310_compatible_wait_for_preserves_90_300_900_limits(self):
        manifest, files, digest = self.installer._manifest()
        kind, entry = files[0]
        with (
            patch("core.local_ocr_install.asyncio.wait_for", wraps=asyncio.wait_for) as wait,
            patch(
                "core.local_ocr_install.asyncio.timeout",
                side_effect=AssertionError("3.10没有此API"),
                create=True,
            ),
        ):
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda _: httpx.Response(200, content=self.payloads[entry["filename"]])
                )
            ) as client:
                await self.installer._asset(client, kind, entry)
            with (
                patch.object(self.installer, "_asset", new_callable=AsyncMock),
                patch.object(self.installer, "_runtime", new_callable=AsyncMock),
            ):
                await self.installer._run(manifest, files, digest)
        self.assertEqual([call.args[1] for call in wait.call_args_list], [90, 900, 300])
        self.assertEqual(self.installer.status()["state"], "succeeded")

    async def test_command_cancellation_terminates_and_waits_for_owned_process(self):
        process = AsyncMock()
        process.returncode = None
        process.terminate = unittest.mock.Mock()
        process.kill = unittest.mock.Mock()
        entered = asyncio.Event()

        async def read(_):
            entered.set()
            await asyncio.Future()

        process.stdout.read.side_effect = read
        process.wait.return_value = 0
        with patch("asyncio.create_subprocess_exec", return_value=process) as spawn:
            task = asyncio.create_task(self.installer._command(["dedicated-python", "-m", "pip"]))
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        process.terminate.assert_called_once()
        process.wait.assert_awaited()
        environment = spawn.call_args.kwargs["env"]
        self.assertEqual(environment["PIP_NO_INDEX"], "1")
        self.assertEqual(environment["PIP_CONFIG_FILE"], sys.modules["os"].devnull)
        self.assertNotIn("PYTHONPATH", environment)
        self.assertTrue(spawn.call_args.kwargs["start_new_session"])

    async def test_process_group_cleanup_kills_descendants_after_parent_exit(self):
        process = AsyncMock()
        process.pid = 12345
        process.returncode = 0
        process.wait.return_value = 0
        with patch("core.local_ocr_install.os.killpg") as kill:
            await self.installer._stop(process)
        self.assertEqual(kill.call_count, 2)
        self.assertEqual({call.args[0] for call in kill.call_args_list}, {12345})

    def test_url_whitelist_rejects_credentials_http_and_ports(self):
        for url in (
            "http://pypi.org/a",
            "https://pypi.org.evil.example/a",
            "https://u:p@pypi.org/a",
            "https://pypi.org:8443/a",
        ):
            with self.assertRaises(ValueError):
                _official_url(url)


if __name__ == "__main__":
    unittest.main(verbosity=2)
