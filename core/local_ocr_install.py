"""管理员主动触发的冻结本地 OCR 资源安装；不修改识别配置或启用状态。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import platform
import re
import signal
import sys
import uuid
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

import httpx


MAX_ASSET_BYTES = 256 * 1024 * 1024
MAX_DOWNLOAD_BYTES = 1024 * 1024 * 1024
MAX_COMMAND_OUTPUT = 256 * 1024
MARKER = ".local-ocr-installer.json"
OWNER = "warframe-local-ocr-installer-v1"
OFFICIAL_HOSTS = frozenset(
    {
        "pypi.org",
        "files.pythonhosted.org",
        "modelscope.cn",
        "www.modelscope.cn",
        "modelscope.com",
        "www.modelscope.com",
    }
)
MODELSCOPE_ENTRY_HOSTS = frozenset(
    {"modelscope.cn", "www.modelscope.cn", "modelscope.com", "www.modelscope.com"}
)
MODELSCOPE_MODEL_CDN = "cdn-lfs-cn-1.modelscope.cn"


def _official_url(url: str) -> tuple[str, str, int | None]:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in OFFICIAL_HOSTS
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 443)
        or parsed.fragment
    ):
        raise ValueError("冻结资源缺少可信官方 HTTPS 地址")
    return parsed.scheme, parsed.hostname, parsed.port or 443


def _redirect_allowed(kind, origin, current_url: str, target_url: str) -> bool:
    """保持同源限制；仅冻结模型允许官方入口直跳实测精确 CDN 对象路径。

    CDN 不加入冻结入口白名单，也不允许其继续跨源跳转；完整文件仍须通过 SHA256。
    """
    try:
        if _official_url(target_url) == origin and _official_url(current_url) == origin:
            return True
    except ValueError:
        pass
    if kind != "models" or origin[1] not in MODELSCOPE_ENTRY_HOSTS:
        return False
    try:
        if _official_url(current_url) != origin:
            return False
        parsed = urlsplit(target_url)
        path = unquote(parsed.path)
        prefix = "/prod/lfs-objects/"
        return (
            parsed.scheme == "https"
            and parsed.hostname == MODELSCOPE_MODEL_CDN
            and parsed.username is None
            and parsed.password is None
            and parsed.port in (None, 443)
            and not parsed.fragment
            and path.startswith(prefix)
            and len(path) > len(prefix)
            and not any(part in (".", "..") for part in path.split("/"))
            and "\\" not in path
        )
    except ValueError:
        return False


async def _finish(task):
    """清理等待不受调用方再次取消打断；原取消由调用方继续传播。"""
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    return task.result()


class LocalOCRInstaller:
    def __init__(self, data_dir: Path, plugin_root: Path):
        self.root = Path(data_dir) / "riven-ocr"
        self.wheels = self.root / "wheels"
        self.models = self.root / "models"
        self.runtime = self.root / "runtime"
        self.manifest_path = Path(plugin_root) / "core" / "data" / "local_ocr_assets.json"
        self._task = None
        self._proc = None
        self._closed = False
        self._downloaded = 0
        self._job = {
            "job_id": uuid.uuid4().hex,
            "state": "idle",
            "progress": 0,
            "message": "尚未执行安装",
        }

    def status(self) -> dict:
        return {
            **self._job,
            "running": self._task is not None and not self._task.done(),
            "recommended_python": str(self.runtime / "bin" / "python")
            if self._job["state"] == "succeeded"
            else "",
            "model_dir": str(self.models),
        }

    @staticmethod
    def _supported() -> bool:
        return (
            platform.system() == "Linux"
            and platform.machine().lower() == "x86_64"
            and platform.python_implementation() == "CPython"
            and sys.version_info[:2] == (3, 12)
        )

    def _manifest(self):
        content = self.manifest_path.read_bytes()
        manifest = json.loads(content)
        if not isinstance(manifest, dict):
            raise ValueError("冻结资源清单必须是对象")
        if manifest.get("platform") != {"system": "Linux", "machine": "x86_64", "python": "3.12"}:
            raise ValueError("冻结资源平台清单不受支持")
        files = []
        seen = set()
        for kind in ("dependencies", "models"):
            entries = manifest.get(kind)
            if not isinstance(entries, list) or not entries:
                raise ValueError("冻结资源清单不完整")
            for entry in entries:
                if not isinstance(entry, dict):
                    raise ValueError("冻结资源条目不是对象")
                filename = entry.get("filename", "")
                sha = entry.get("sha256", "")
                if (
                    not isinstance(filename, str)
                    or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.+-]*", filename)
                    or filename in seen
                    or not isinstance(sha, str)
                    or not re.fullmatch(r"[a-fA-F0-9]{64}", sha)
                ):
                    raise ValueError("冻结资源名称或 SHA256 无效")
                if kind == "dependencies" and (
                    not filename.endswith(".whl")
                    or not isinstance(entry.get("name"), str)
                    or not entry["name"]
                    or not isinstance(entry.get("version"), str)
                    or not entry["version"]
                ):
                    raise ValueError("冻结依赖必须有准确名称、版本和 wheel 文件")
                if kind == "models" and not filename.endswith(".onnx"):
                    raise ValueError("冻结模型必须是 ONNX 文件")
                _official_url(entry.get("url", ""))
                seen.add(filename)
                files.append((kind, {**entry, "sha256": sha.lower()}))
        return manifest, files, hashlib.sha256(content).hexdigest()

    async def start(self, confirm: bool = False, *, expected_job_id: str | None = None) -> dict:
        if self._closed:
            return {**self.status(), "accepted": False, "reason": "closed"}
        if confirm is not True:
            return {**self.status(), "accepted": False, "reason": "confirmation_required"}
        if not isinstance(expected_job_id, str) or expected_job_id != self._job["job_id"]:
            return {**self.status(), "accepted": False, "reason": "job_conflict"}
        if self._task is not None and not self._task.done():
            return {**self.status(), "accepted": False, "reason": "busy"}
        # 比较、换代和排队之间不让出事件循环；旧窗口不能重用已消费的状态世代。
        job_id = uuid.uuid4().hex
        if not self._supported():
            self._job = {
                "job_id": job_id,
                "state": "unsupported",
                "progress": 0,
                "message": "自动部署仅支持 Linux x86_64 CPython 3.12，请使用手动环境路径",
            }
            return {**self.status(), "accepted": False, "reason": "unsupported"}
        try:
            manifest, files, digest = self._manifest()
        except (OSError, TypeError, ValueError) as exc:
            self._job = {
                "job_id": job_id,
                "state": "failed",
                "progress": 0,
                "message": f"冻结清单不可用：{exc}",
            }
            return {**self.status(), "accepted": False, "reason": "invalid_manifest"}
        self._job = {
            "job_id": job_id,
            "state": "queued",
            "progress": 0,
            "message": "已确认，准备校验冻结资源",
        }
        self._task = asyncio.create_task(self._run(manifest, files, digest))
        return {**self.status(), "accepted": True}

    async def _hash_file(self, path: Path) -> str:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_ASSET_BYTES:
            raise ValueError(f"资源不是允许大小的普通文件：{path.name}")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
                await asyncio.sleep(0)
        return digest.hexdigest()

    async def _asset(self, client, kind, entry):
        directory = self.wheels if kind == "dependencies" else self.models
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / entry["filename"]
        if target.exists() or target.is_symlink():
            if await self._hash_file(target) != entry["sha256"]:
                raise ValueError(f"已有缓存 SHA256 不符，未覆盖：{target.name}")
            return
        temporary = directory / ("." + entry["filename"] + "." + uuid.uuid4().hex + ".part")
        url = entry["url"]
        origin = _official_url(url)

        async def download():
            current = url
            for redirect in range(4):
                async with client.stream("GET", current) as response:
                    if response.status_code in (301, 302, 303, 307, 308):
                        next_url = urljoin(current, response.headers.get("location", ""))
                        if redirect == 3 or not _redirect_allowed(kind, origin, current, next_url):
                            raise ValueError("资源重定向超出官方同源边界")
                        current = next_url
                        continue
                    response.raise_for_status()
                    declared = response.headers.get("content-length")
                    if declared and int(declared) > MAX_ASSET_BYTES:
                        raise ValueError("资源超过大小上限")
                    digest, size = hashlib.sha256(), 0
                    with temporary.open("xb") as stream:
                        async for chunk in response.aiter_bytes(1024 * 1024):
                            size += len(chunk)
                            self._downloaded += len(chunk)
                            if size > MAX_ASSET_BYTES or self._downloaded > MAX_DOWNLOAD_BYTES:
                                raise ValueError("下载超过大小上限")
                            digest.update(chunk)
                            stream.write(chunk)
                    if digest.hexdigest() != entry["sha256"]:
                        raise ValueError(f"下载资源 SHA256 不符：{entry['filename']}")
                    os.replace(temporary, target)
                    return
            raise ValueError("资源重定向过多")

        try:
            await asyncio.wait_for(download(), 90)
        finally:
            temporary.unlink(missing_ok=True)

    async def _stop(self, process):
        grouped = type(getattr(process, "pid", None)) is int

        def send(sig):
            try:
                if grouped:
                    os.killpg(process.pid, sig)
                elif process.returncode is None:
                    (process.terminate if sig == signal.SIGTERM else process.kill)()
            except ProcessLookupError:
                pass

        send(signal.SIGTERM)
        try:
            await asyncio.wait_for(process.wait(), 3)
        except asyncio.TimeoutError:
            send(signal.SIGKILL)
            await process.wait()
        finally:
            # venv/ensurepip 可能有子进程；进程组只由本次创建，不留下后台安装。
            if grouped:
                send(signal.SIGKILL)

    async def _command(self, args):
        env = dict(os.environ)
        for name in (
            "PYTHONPATH",
            "PYTHONHOME",
            "VIRTUAL_ENV",
            "PIP_INDEX_URL",
            "PIP_EXTRA_INDEX_URL",
        ):
            env.pop(name, None)
        env.update(
            PYTHONNOUSERSITE="1",
            PIP_NO_INDEX="1",
            PIP_DISABLE_PIP_VERSION_CHECK="1",
            PIP_CONFIG_FILE=os.devnull,
        )
        spawn = asyncio.create_task(
            asyncio.create_subprocess_exec(
                *map(str, args),
                env=env,
                start_new_session=True,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        )
        try:
            process = await asyncio.shield(spawn)
        except asyncio.CancelledError:
            process = await _finish(spawn)
            await _finish(asyncio.create_task(self._stop(process)))
            raise
        self._proc = process
        try:
            output = bytearray()
            while chunk := await process.stdout.read(8192):
                output.extend(chunk)
                if len(output) > MAX_COMMAND_OUTPUT:
                    raise ValueError("安装子进程输出超过上限")
            code = await process.wait()
            text = output.decode("utf-8", errors="replace")
            if code:
                raise ValueError(f"安装子进程失败（exit={code}）：{text[-1200:]}")
            return text
        finally:
            await _finish(asyncio.create_task(self._stop(process)))
            self._proc = None

    async def _versions(self, manifest):
        expected = {entry["name"]: entry["version"] for entry in manifest["dependencies"]}
        script = (
            "import importlib.metadata as m,json,sys; "
            "print(json.dumps({name:m.version(name) for name in json.loads(sys.argv[1])}))"
        )
        actual = json.loads(
            await self._command(
                [
                    self.runtime / "bin" / "python",
                    "-I",
                    "-B",
                    "-c",
                    script,
                    json.dumps(list(expected)),
                ]
            )
        )
        if actual != expected:
            raise ValueError("隔离运行环境的实际依赖版本与冻结清单不符")

    def _marker(self, digest, state, **extra):
        content = {"owner": OWNER, "manifest_sha256": digest, "state": state, **extra}
        temporary = self.runtime / (MARKER + "." + uuid.uuid4().hex + ".tmp")
        try:
            temporary.write_text(json.dumps(content, ensure_ascii=False), encoding="utf-8")
            os.replace(temporary, self.runtime / MARKER)
        finally:
            temporary.unlink(missing_ok=True)

    async def _runtime(self, manifest, digest):
        if self.runtime.exists() or self.runtime.is_symlink():
            if self.runtime.is_symlink():
                raise ValueError("runtime 已存在且是符号链接，未覆盖；请使用手动环境路径")
            try:
                marker = json.loads((self.runtime / MARKER).read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise ValueError(
                    "runtime 已存在且不属于本安装器，未覆盖；请使用手动环境路径"
                ) from exc
            if marker != {"owner": OWNER, "manifest_sha256": digest, "state": "succeeded"}:
                raise ValueError("runtime 已存在但标记/版本不匹配，未覆盖；请检查保留的安装诊断")
            await self._versions(manifest)
            return
        self.runtime.mkdir()  # 独占占位；整个运行环境不移动，venv shebang/解释器链接路径稳定。
        self._marker(digest, "installing")
        try:
            self._job.update(state="creating_venv", progress=70, message="创建专用 Python 虚拟环境")
            await self._command([sys.executable, "-I", "-B", "-m", "venv", self.runtime])
            self._job.update(state="installing", progress=80, message="仅从冻结 wheel 缓存安装依赖")
            await self._command(
                [
                    self.runtime / "bin" / "python",
                    "-I",
                    "-B",
                    "-m",
                    "pip",
                    "install",
                    "--no-index",
                    "--find-links",
                    self.wheels,
                    "--only-binary=:all:",
                    "--no-deps",
                    *[self.wheels / entry["filename"] for entry in manifest["dependencies"]],
                ]
            )
            await self._versions(manifest)
            self._marker(digest, "succeeded")
        except BaseException as exc:
            self._marker(digest, "failed", error=type(exc).__name__)
            raise

    async def _run(self, manifest, files, digest):
        self._downloaded = 0

        async def prepare():
            self.root.mkdir(parents=True, exist_ok=True)
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(60, connect=15), follow_redirects=False, trust_env=False
            ) as client:
                for index, (kind, entry) in enumerate(files):
                    self._job.update(
                        state="downloading",
                        progress=int(index * 65 / len(files)),
                        message=f"校验/准备冻结资源：{entry['filename']}",
                    )
                    await self._asset(client, kind, entry)
            await asyncio.wait_for(self._runtime(manifest, digest), 300)

        try:
            await asyncio.wait_for(prepare(), 900)
            self._job.update(
                state="succeeded",
                progress=100,
                message="专用环境与模型已准备好；请手动保存推荐路径、检查并启用，本任务未更改配置",
            )
        except asyncio.CancelledError:
            self._job.update(
                state="cancelled", message="安装已取消；自建环境诊断已保留，配置未改变"
            )
            raise
        except asyncio.TimeoutError:
            self._job.update(
                state="failed",
                message="资源准备或隔离环境安装超时（单件90秒、安装300秒、整项900秒）；配置未改变",
            )
        except Exception as exc:  # noqa: BLE001 - 后台任务错误必须进入可观察状态
            self._job.update(state="failed", message=f"准备失败：{exc}")

    async def close(self):
        self._closed = True
        task = self._task
        if task is not None and not task.done():
            task.cancel()
        if task is not None:
            cleanup = asyncio.ensure_future(asyncio.gather(task, return_exceptions=True))
            await _finish(cleanup)
            if task.cancelled():
                self._job.update(state="cancelled", message="安装已取消，配置未改变")
