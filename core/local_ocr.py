"""本地紫卡 OCR：默认关闭、只读检查、原子配置与单次隔离进程。

本模块不导入推理库、不安装依赖、不下载模型，也不调用任何云端渠道。
"""

from __future__ import annotations

import asyncio
import base64
import errno
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

MODEL_HASHES = {
    "PP-OCRv6_det_small.onnx": "090f04abcd9d9a7498bc4ebf677e4cb9bdce1fe4197ddb7e529f1ef44e1ff94f",
    "PP-OCRv6_rec_small.onnx": "6f327246b50388f3c176ae304bd95767ea6dc0c9ae92153ef8cbe210b3c14884",
    "ch_ppocr_mobile_v2.0_cls_mobile.onnx": "e47acedf663230f8863ff1ab0e64dd2d82b838fceb5957146dab185a89d6215c",
}
DEPENDENCIES = {"rapidocr": "3.10.0", "onnxruntime": "1.31.1"}
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_OUTPUT_BYTES = 2 * 1024 * 1024
CHECK_TIMEOUT = 20
RECOGNIZE_TIMEOUT = 45
MIB = 1024 * 1024


def decode_image_data_url(value: str) -> bytes:
    """只接已下载的图片数据，不把任意 URL 交给本地进程。"""
    if not isinstance(value, str) or len(value) > 14 * MIB:
        raise ValueError("本地 OCR 图片数据过大或格式无效")
    prefix, separator, encoded = value.partition(",")
    if not separator or prefix.lower() not in {
        "data:image/png;base64",
        "data:image/jpeg;base64",
        "data:image/jpg;base64",
        "data:image/webp;base64",
    }:
        raise ValueError("本地 OCR 只接受 PNG、JPEG 或 WebP 图片数据")
    try:
        image = base64.b64decode(encoded, validate=True)
    except ValueError as exc:
        raise ValueError("本地 OCR 图片编码无效") from exc
    if not image or len(image) > MAX_IMAGE_BYTES:
        raise ValueError("本地 OCR 图片不能为空且不能超过 10MiB")
    return image


class OCRConflict(ValueError):
    """配置已由另一请求修改；调用方应重新读取修订号。"""


def checked_models(directory: Path) -> dict[str, str]:
    """只读本地文件，缺件或哈希不符立即拒绝。"""
    paths = {}
    for name, expected in MODEL_HASHES.items():
        path = directory / name
        if not path.is_file():
            raise ValueError(f"缺少本地模型：{name}")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(MIB), b""):
                digest.update(chunk)
        if digest.hexdigest() != expected:
            raise ValueError(f"本地模型 SHA-256 不符：{name}")
        paths[name] = str(path)
    return paths


def resource_snapshot(proc_root: Path = Path("/proc"), cgroup_root: Path = Path("/sys/fs/cgroup")):
    """读取可用 RAM/swap；cgroup 的剩余配额进一步收紧宿主数值。

    swap 仅用于避免资源明显不足时启动，不能据此保证推理速度。
    """
    memory, swap = None, 0
    try:
        entries = {}
        for line in (proc_root / "meminfo").read_text().splitlines():
            key, value = line.split(":", 1)
            entries[key] = int(value.strip().split()[0]) * 1024
        memory = entries.get("MemAvailable")
        swap = entries.get("SwapFree", 0)
    except (OSError, ValueError):
        if sys.platform == "darwin":
            try:
                info = subprocess.check_output(["/usr/bin/vm_stat"], timeout=2, text=True)
                page = int(re.search(r"page size of (\d+) bytes", info)[1])
                counts = dict(re.findall(r"^([^:]+):\s+(\d+)\.", info, re.M))
                memory = (
                    sum(
                        int(counts.get(key, 0))
                        for key in ("Pages free", "Pages inactive", "Pages speculative")
                    )
                    * page
                )
                usage = subprocess.check_output(
                    ["/usr/sbin/sysctl", "-n", "vm.swapusage"], timeout=2, text=True
                )
                match = re.search(r"free\s*=\s*([\d.]+)M", usage)
                swap = int(float(match[1]) * MIB) if match else 0
            except (OSError, ValueError, subprocess.SubprocessError, TypeError):
                memory = None
    cgroup_applied = False
    try:
        groups = (proc_root / "self/cgroup").read_text().splitlines()
    except OSError:
        groups = []
    for row in groups:
        parts = row.split(":", 2)
        if len(parts) != 3:
            continue
        if parts[0] == "0" and parts[1] == "":
            root = cgroup_root.resolve()
            path = root / parts[2].lstrip("/")
            # 容器命名空间可能给出宿主路径；挂载根也是有效的限制来源。
            directories = [root]
            if path.resolve().is_relative_to(root):
                directories += [path, *[p for p in path.parents if p != root and root in p.parents]]
            for directory in directories:
                for name, current, kind in (
                    ("memory.max", "memory.current", "ram"),
                    ("memory.swap.max", "memory.swap.current", "swap"),
                ):
                    try:
                        limit = (directory / name).read_text().strip()
                        if limit == "max":
                            continue
                        available = max(0, int(limit) - int((directory / current).read_text()))
                    except (OSError, ValueError):
                        continue
                    cgroup_applied = True
                    if kind == "ram":
                        memory = min(memory, available) if memory is not None else available
                    else:
                        swap = min(swap, available)
        elif "memory" in parts[1].split(","):
            directory = cgroup_root / "memory" / parts[2].lstrip("/")
            try:
                limit = int((directory / "memory.limit_in_bytes").read_text())
                used = int((directory / "memory.usage_in_bytes").read_text())
                available = max(0, limit - used)
                memory = min(memory, available) if memory is not None else available
                cgroup_applied = True
                try:
                    combined = int((directory / "memory.memsw.limit_in_bytes").read_text())
                    combined_used = int((directory / "memory.memsw.usage_in_bytes").read_text())
                    # v1 memsw 是 RAM+swap 的合并预算，不能重复给两种资源分配。
                    swap = min(swap, max(0, combined - combined_used - (memory or 0)))
                except (OSError, ValueError):
                    pass
            except (OSError, ValueError):
                pass
    ok = memory is not None and memory >= 256 * MIB and memory + swap >= 1024 * MIB
    return {
        "ok": ok,
        "ram_available_bytes": memory,
        "swap_available_bytes": swap,
        "cgroup_applied": cgroup_applied,
        "message": (
            "资源检查通过；使用 swap 时推理可能变慢"
            if ok
            else "本地 OCR 需要可用 RAM ≥256MiB，且可用 RAM+swap ≥1024MiB"
        ),
    }


class LocalRivenOCR:
    def __init__(self, data_dir: Path, plugin_root: Path):
        self.path = Path(data_dir) / "riven_ocr.json"
        self.plugin_root = Path(plugin_root).resolve()
        self._busy = False
        self._closed = False
        self._active = None
        self._spawn_task = None
        self._stop_lock = asyncio.Lock()
        self._check_tasks = set()
        self._check_cancelled = set()

    @staticmethod
    def _default():
        return dict(enabled=False, engine="", python="", model_dir="", revision=0, check_ok=None)

    def _read(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return self._default()
        except (ValueError, UnicodeError) as exc:
            raise ValueError("本地 OCR 配置损坏，未改写文件") from exc
        if (
            not isinstance(data, dict)
            or set(data) != set(self._default())
            or type(data["enabled"]) is not bool
            or type(data["revision"]) is not int
            or data["revision"] < 0
            or any(not isinstance(data[key], str) for key in ("engine", "python", "model_dir"))
            or (data["check_ok"] is not None and type(data["check_ok"]) is not bool)
        ):
            raise ValueError("本地 OCR 配置损坏，未改写文件")
        return data

    def status(self) -> dict:
        """无推理、无子进程、无落盘；仅检查模型文件是否存在，不计算哈希。"""
        try:
            data = self._read()
            models = bool(data["model_dir"]) and all(
                (Path(data["model_dir"]) / name).is_file() for name in MODEL_HASHES
            )
            return {
                **data,
                "ok": True,
                "implemented": True,
                "models_found": models,
                "engines": [{"id": "rapidocr", "label": "RapidOCR"}],
                "busy": self._busy,
                "closed": self._closed,
            }
        except (ValueError, OSError):
            return {
                **self._default(),
                "ok": False,
                "implemented": True,
                "models_found": False,
                "engines": [{"id": "rapidocr", "label": "RapidOCR"}],
                "state": "invalid_config",
                "message": "本地 OCR 配置读取失败，请检查配置文件",
            }

    @contextmanager
    def _locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.with_suffix(".json.lock").open("a+b") as stream:
            if sys.platform == "win32":
                import msvcrt

                stream.seek(0, os.SEEK_END)
                if stream.tell() == 0:
                    stream.write(b"\0")
                    stream.flush()
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(stream, fcntl.LOCK_EX)
            try:
                yield
            finally:
                if sys.platform == "win32":
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream, fcntl.LOCK_UN)

    def _write(self, data):
        name = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.path.parent, prefix=".riven-ocr-", delete=False
            ) as stream:
                name = stream.name
                json.dump(data, stream, ensure_ascii=False, allow_nan=False)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
        finally:
            if name and os.path.exists(name):
                os.unlink(name)

    @staticmethod
    def _cas(data, revision):
        if type(revision) is not int or revision != data["revision"]:
            raise OCRConflict("本地 OCR 配置已变化，请刷新后重试")

    def _settings(self, engine, python, model_dir):
        if engine != "rapidocr":
            raise ValueError("本地 OCR 仅支持 rapidocr")
        paths = []
        for value, label in ((python, "解释器"), (model_dir, "模型目录")):
            if not isinstance(value, str) or not value or "\0" in value:
                raise ValueError(f"{label}必须是绝对路径")
            path = Path(value)
            if not path.is_absolute():
                raise ValueError(f"{label}必须是绝对路径")
            # 解释器可以位于已有独立 venv；模型不得写进发行源码目录。
            if label == "模型目录" and path.resolve().is_relative_to(self.plugin_root):
                raise ValueError("模型目录不能位于插件源码目录内")
            paths.append(str(path.absolute()))
        return {"engine": engine, "python": paths[0], "model_dir": paths[1]}

    async def save(self, payload: dict) -> dict:
        if not isinstance(payload, dict):
            raise ValueError("本地 OCR 配置必须是对象")
        settings = self._settings(
            payload.get("engine"), payload.get("python"), payload.get("model_dir")
        )
        with self._locked():
            data = self._read()
            self._cas(data, payload.get("revision"))
            data.update(settings, enabled=False, check_ok=None, revision=data["revision"] + 1)
            self._write(data)
        await self._stop_active()
        return {**self.status(), "state": "saved", "message": "配置已保存，请检查通过后启用"}

    async def check(self, engine, python, model_dir) -> dict:
        if self._closed:
            return {**self.status(), "ok": False, "state": "closed", "message": "本地 OCR 已关闭"}
        start = self._read()
        task = None
        try:
            settings = self._settings(engine, python, model_dir)
            task = asyncio.create_task(self._check_settings(settings))
            self._check_tasks.add(task)
            # 3.10 wait_for 的取消等待可以被第二次cancel打断。超时/调用方取消只
            # 取消shield，实际检查由本层明确取消一次，并等它的finally回收完进程。
            result = await asyncio.wait_for(asyncio.shield(task), timeout=CHECK_TIMEOUT)
        except asyncio.TimeoutError:
            result = {"ok": False, "state": "check_timeout", "message": "本地 OCR 检查超时"}
        except (ValueError, OSError) as exc:
            result = {"ok": False, "state": "check_failed", "message": str(exc)}
        finally:
            if task is not None:
                try:
                    await self._complete_cleanup(self._finish_check(task))
                finally:
                    self._check_tasks.discard(task)
                    self._check_cancelled.discard(task)
        # 检查草稿不得把其它配置的已检查状态覆盖掉；并发保存后也不回写旧结果。
        persisted = False
        if "settings" in locals():
            with self._locked():
                data = self._read()
                if data["revision"] == start["revision"] and all(
                    data[key] == value for key, value in settings.items()
                ):
                    data.update(check_ok=result["ok"], revision=data["revision"] + 1)
                    if not result["ok"]:
                        data["enabled"] = False
                    self._write(data)
                    persisted = True
            if persisted and not result["ok"]:
                await self._stop_active()
        return {**self.status(), **result, "check_persisted": persisted}

    def _cancel_check_once(self, task):
        # close与请求取消可能同时抵达；只有本管理器拥有取消检查任务的权利。
        if not task.done() and task not in self._check_cancelled:
            self._check_cancelled.add(task)
            task.cancel()

    async def _finish_check(self, task):
        self._cancel_check_once(task)
        await asyncio.gather(task, return_exceptions=True)

    async def _check_settings(self, settings):
        python = Path(settings["python"])
        if not python.is_file() or not os.access(python, os.X_OK):
            raise ValueError("本地 OCR 解释器不存在或不可执行")
        probe = (
            "import sys,json,importlib.metadata as m;"
            "print(json.dumps({'python':list(sys.version_info[:3]),"
            "'versions':{k:m.version(k) for k in ('rapidocr','onnxruntime')}}))"
        )
        spawn = asyncio.create_task(
            asyncio.create_subprocess_exec(
                str(python),
                "-I",
                "-B",
                "-c",
                probe,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=(os.name == "posix"),
            )
        )
        process = None
        collect = None

        async def cleanup():
            if collect is not None:
                if not collect.done():
                    collect.cancel()
                await asyncio.gather(collect, return_exceptions=True)
            owned = process
            if owned is None:
                # create_subprocess_exec未交回句柄时也不能放弃它，先取得归属再回收。
                try:
                    owned = await spawn
                except OSError:
                    return
            await self._finish_process(owned)

        try:
            process = await asyncio.shield(spawn)
            collect = asyncio.create_task(self._collect(process))
            output, _ = await asyncio.wait_for(asyncio.shield(collect), timeout=8)
            if process.returncode != 0:
                raise ValueError("解释器元数据检查失败，请安装要求版本的独立本地 OCR 依赖")
            try:
                metadata = json.loads(output)
                if tuple(metadata["python"]) < (3, 10):
                    raise ValueError("本地 OCR 解释器需要 Python ≥3.10")
                if metadata["versions"] != DEPENDENCIES:
                    raise ValueError("本地 OCR 依赖版本需 rapidocr 3.10.0 / onnxruntime 1.31.1")
            except (KeyError, TypeError, json.JSONDecodeError) as exc:
                raise ValueError("本地 OCR 解释器元数据无效") from exc
        finally:
            await self._complete_cleanup(cleanup())
        await asyncio.to_thread(checked_models, Path(settings["model_dir"]))
        resources = await asyncio.to_thread(resource_snapshot)
        if not resources["ok"]:
            raise ValueError(resources["message"])
        return {
            "ok": True,
            "state": "checked",
            "message": "本地 OCR 检查通过",
            "resources": resources,
        }

    def _check_enable(self, data):
        if self._closed:
            raise ValueError("本地 OCR 管理器已关闭")
        self._settings(data["engine"], data["python"], data["model_dir"])
        if data["check_ok"] is not True:
            raise ValueError("请先保存配置并检查通过")

    def _switch_busy(self):
        return bool(self._busy or self._active or self._spawn_task or self._check_tasks)

    def _write_enabled(self, data, enabled):
        self._write({**data, "enabled": enabled, "revision": data["revision"] + 1})

    async def enable(self, enabled: bool, revision: int) -> dict:
        if type(enabled) is not bool:
            raise ValueError("enabled 必须是布尔值")
        with self._locked():
            data = self._read()
            self._cas(data, revision)
            if enabled:
                self._check_enable(data)
            self._write_enabled(data, enabled)
        if not enabled:
            await self._stop_active()
        return {
            **self.status(),
            "state": "enabled" if enabled else "disabled",
            "message": "本地 OCR 已启用" if enabled else "本地 OCR 已关闭",
        }

    async def _collect(self, process):
        process._wf_collecting = True

        async def read_limited(stream):
            chunks, size = [], 0
            while True:
                chunk = await stream.read(65536)
                if not chunk:
                    return b"".join(chunks)
                size += len(chunk)
                if size > MAX_OUTPUT_BYTES:
                    raise ValueError("本地 OCR 子进程输出超限")
                chunks.append(chunk)

        tasks = [asyncio.create_task(read_limited(s)) for s in (process.stdout, process.stderr)]
        try:
            output = await asyncio.gather(*tasks)
            await process.wait()
            return output
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            process._wf_collecting = False

    @staticmethod
    async def _complete_cleanup(coro):
        """重复取消也只能在清理完成后传播，不能把模型进程留在后台。"""
        task = asyncio.create_task(coro)
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
        result = task.result()
        if cancelled:
            raise asyncio.CancelledError
        return result

    @staticmethod
    async def _finish_process(process):
        # 即便领进程已退出，仍清理它的会话进程组，避免残存子孙进程。
        def send(sig):
            try:
                if os.name == "posix":
                    os.killpg(process.pid, sig)
                elif process.returncode is None:
                    process.terminate() if sig == signal.SIGTERM else process.kill()
            except ProcessLookupError:
                pass

        async def drain(stream):
            if stream is not None:
                while await stream.read(65536):
                    pass

        if process.stdin is not None:
            process.stdin.close()
        drains = []
        if not getattr(process, "_wf_collecting", False):
            # 超限/取消后的未读缓冲会暂停 pipe；丢弃残余以让 transport 真正关闭。
            drains = [asyncio.create_task(drain(s)) for s in (process.stdout, process.stderr)]
        send(signal.SIGTERM)
        try:
            await asyncio.wait_for(process.wait(), timeout=1)
        except asyncio.TimeoutError:
            pass
        finally:
            send(signal.SIGKILL)
            await process.wait()
            if drains:
                await asyncio.gather(*drains, return_exceptions=True)

    async def _stop_active(self):
        await self._complete_cleanup(self._stop_active_inner())

    async def _stop_active_inner(self):
        async with self._stop_lock:
            if self._spawn_task is not None:
                try:
                    await asyncio.shield(self._spawn_task)
                except (OSError, asyncio.CancelledError):
                    pass
            if self._active is not None:
                await self._finish_process(self._active)
                self._active = None

    @staticmethod
    def _failure(state, message):
        return {"ok": False, "state": state, "status": "local_error", "message": message}

    def _try_run_slot(self):
        """跨管理器/热重载实例也只允许一个模型进程，不等待运行席位。"""
        stream = self.path.with_suffix(".active.lock").open("a+b")
        try:
            if sys.platform == "win32":
                import msvcrt

                stream.seek(0, os.SEEK_END)
                if stream.tell() == 0:
                    stream.write(b"\0")
                    stream.flush()
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return stream
        except OSError as exc:
            stream.close()
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                return None
            raise

    async def recognize(self, image: bytes, *, expected_revision: int | None = None) -> dict:
        if self._closed:
            return self._failure("closed", "本地 OCR 已关闭")
        try:
            data = self._read()
        except (ValueError, OSError):
            return self._failure("invalid_config", "本地 OCR 配置读取失败")

        def failure(state, message):
            return {
                **self._failure(state, message),
                "configuration_revision": data["revision"],
            }

        # 面板的确认只授权当时看到的配置；聊天入口不传修订号，仍使用当前配置。
        if expected_revision is not None and (
            type(expected_revision) is not int or expected_revision != data["revision"]
        ):
            return failure("revision_conflict", "本地 OCR 配置已变化，请重新读取并确认试识别")
        if not data["enabled"]:
            return failure("disabled", "本地 OCR 未启用")
        if data["check_ok"] is not True:
            return failure("unchecked", "请先检查本地 OCR 配置")
        if not isinstance(image, bytes) or not image or len(image) > MAX_IMAGE_BYTES:
            return failure("invalid_image", "图片不能为空且不能超过 10MiB")
        if self._busy:
            return failure("busy", "本地 OCR 正忙，请稍后重试")
        self._busy = True
        run_slot = None
        try:
            run_slot = self._try_run_slot()
            if run_slot is None:
                return failure("busy", "本地 OCR 正忙，请稍后重试")
            settings = self._settings(data["engine"], data["python"], data["model_dir"])
            resources = await asyncio.to_thread(resource_snapshot)
            if not resources["ok"]:
                return failure("resources", resources["message"])

            # 每次加载前在 worker 内再次核验模型，防止检查后权重被替换。
            async def spawn():
                process = await asyncio.create_subprocess_exec(
                    settings["python"],
                    "-I",
                    "-B",
                    str(self.plugin_root / "core/local_ocr_worker.py"),
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=(os.name == "posix"),
                )
                self._active = process
                return process

            if self._closed or self._read() != data:
                return failure("disabled", "本地 OCR 配置已变化或已关闭")
            self._spawn_task = asyncio.create_task(spawn())
            process = await asyncio.shield(self._spawn_task)
            if self._closed or self._read() != data:
                return failure("disabled", "本地 OCR 配置已变化或已关闭")

            async def exchange():
                request = json.dumps(
                    {
                        "model_dir": settings["model_dir"],
                        "image": base64.b64encode(image).decode("ascii"),
                    }
                ).encode()
                process.stdin.write(request + b"\n")
                await process.stdin.drain()
                process.stdin.close()
                output, _ = await self._collect(process)
                result = json.loads(output)
                if not isinstance(result, dict):
                    raise ValueError("本地 OCR 子进程返回格式无效")
                if self._closed or self._read() != data:
                    return failure("disabled", "本地 OCR 配置已变化或已关闭")
                if process.returncode != 0 or result.get("status") == "local_error":
                    return {
                        **result,
                        **failure("worker_error", result.get("message", "本地 OCR 识别失败")),
                    }
                state = result.get("recognition_state", result.get("status", "partial"))
                return {
                    **result,
                    "ok": result.get("status") == "ok" and state == "accepted",
                    "state": state,
                    "message": result.get(
                        "message",
                        "本地 OCR 识别完成"
                        if state == "accepted"
                        else "本地识别字段不完整，请重发清晰原图或改用文字输入",
                    ),
                    "configuration_revision": data["revision"],
                }

            return await asyncio.wait_for(exchange(), timeout=RECOGNIZE_TIMEOUT)
        except asyncio.TimeoutError:
            return failure("timeout", "本地 OCR 识别超时，请稍后重试或改用文字输入")
        except (OSError, ValueError, ConnectionError) as exc:
            return failure("worker_error", f"本地 OCR 识别失败：{exc}")
        finally:
            try:
                await self._stop_active()
            finally:
                self._spawn_task = None
                self._busy = False
                if run_slot is not None:
                    run_slot.close()

    async def close(self):
        self._closed = True
        await self._complete_cleanup(self._close_owned())

    async def _close_owned(self):
        checks = list(self._check_tasks)
        for task in checks:
            self._cancel_check_once(task)
        await asyncio.gather(*checks, return_exceptions=True)
        await self._stop_active()
