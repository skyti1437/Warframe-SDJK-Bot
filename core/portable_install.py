"""完整模拟引擎运行包的下载与校验。"""

from __future__ import annotations

import asyncio
import json
import platform
import re
import sys
import zipfile
from hashlib import sha256
from pathlib import PurePosixPath

# 验收包只含服务、冻结输入和原生模块。按成员读取，既不信任归档路径也不解压符号链接。
MAX_BUNDLE_SIZE = 60 * 1024 * 1024
MAX_UNPACKED_SIZE = 160 * 1024 * 1024
PORTABLE_ASSETS = {
    "darwin-arm64": (
        "portable-runtime-mac-arm64.zip",
        "c37bd92cd26dc54d4736c20e3439aee61540eb6105d2aa205512c0e9c6b8cc1f",
        2369576,
    ),
    "linux-x86_64": (
        "portable-runtime-linux-x86_64.zip",
        "daf8eed28cc8b3b6668222c9d9cce71b65f23d5e0f63841a589d46970a07d361",
        2465818,
    ),
}
SHA256 = re.compile(r"[a-f0-9]{64}")
UPSTREAM_REVISION = "d62e4ae56387e241d512dcf9176f3bfd3d426de7"
UPSTREAM_DATA = (
    "https://raw.githubusercontent.com/superexboom/warframe-weapon-loadout-build/"
    + UPSTREAM_REVISION
)


async def download_file(client, url, path, limit, expected=None):
    digest = sha256()
    count = 0
    async with client.stream("GET", url) as response:
        response.raise_for_status()
        with path.open("wb") as output:
            async for block in response.aiter_bytes(chunk_size=256 * 1024):
                count += len(block)
                if count > limit:
                    raise ValueError("引擎下载文件超过体积上限")
                digest.update(block)
                output.write(block)
    if expected is not None and digest.hexdigest() != expected:
        raise ValueError(f"引擎文件摘要不一致（{path.name}）")
    return count


async def download_inputs(client, dest, files, remaining):
    slots = asyncio.Semaphore(4)

    async def fetch(name, digest):
        async with slots:
            path = dest / name
            path.parent.mkdir(parents=True, exist_ok=True)
            return await download_file(client, f"{UPSTREAM_DATA}/{name}", path, remaining, digest)

    sizes = await asyncio.gather(
        *(fetch(name, digest) for name, digest in files.items()), return_exceptions=True
    )
    for size in sizes:
        if isinstance(size, Exception):
            raise size
    if sum(sizes) > remaining:
        raise ValueError("引擎数据超过安装体积上限")


def unpack(bundle, dest, expected, fingerprint, data_version):
    if not SHA256.fullmatch(expected):
        raise ValueError("请填写收到的运行包 SHA-256")
    if bundle.stat().st_size > MAX_BUNDLE_SIZE:
        raise ValueError("独立引擎运行包体积超过安装上限")
    with bundle.open("rb") as stream:
        digest = sha256()
        while block := stream.read(1024 * 1024):
            digest.update(block)
        if digest.hexdigest() != expected:
            raise ValueError("运行包 SHA-256 不一致")
        stream.seek(0)
        with zipfile.ZipFile(stream) as archive:
            members = archive.infolist()
            names = {m.filename for m in members}
            size = sum(m.file_size for m in members)
            if len(names) != len(members) or size > MAX_UNPACKED_SIZE:
                raise ValueError("独立引擎运行包文件有重复或超过解包上限")
            manifest = json.loads(archive.read("manifest.json"))
            if (
                manifest["fingerprint"] != fingerprint
                or manifest["dataVersion"] != data_version
                or manifest["platform"] != f"{sys.platform}-{platform.machine()}"
                or manifest["python"] != f"{sys.version_info.major}.{sys.version_info.minor}"
            ):
                raise ValueError("独立引擎运行包的版本、系统或 Python ABI 不匹配")
            files = manifest["files"]
            if not isinstance(files, dict):
                raise ValueError("运行包文件清单无效")
            upstream = manifest.get("upstreamFiles", [])
            if not isinstance(upstream, list) or len(upstream) > 64:
                raise ValueError("上游数据清单无效")
            if upstream and (
                manifest.get("upstream") != UPSTREAM_REVISION
                or any(
                    not isinstance(name, str) or not name.startswith("data/") for name in upstream
                )
                or len(upstream) != len(set(upstream))
                or not set(upstream) <= files.keys()
            ):
                raise ValueError("上游数据清单不符")
            external = {name: files[name] for name in upstream}
            if (
                names != ((files.keys() - external.keys()) | {"manifest.json"})
                or not {
                    "launch.py",
                    "LICENSE",
                    "THIRD_PARTY_NOTICES.md",
                    "portable-views.json",
                    "analysis-statics.json",
                    "app.py",
                }
                <= files.keys()
            ):
                raise ValueError("独立引擎运行包清单不完整")
            for name, expected_sha in files.items():
                part = PurePosixPath(name)
                if (
                    part.is_absolute()
                    or ".." in part.parts
                    or part.as_posix() != name
                    or "\\" in name
                    or not isinstance(expected_sha, str)
                    or not SHA256.fullmatch(expected_sha)
                ):
                    raise ValueError("独立引擎运行包含无效路径或文件摘要")
                if name in external:
                    continue
                raw = archive.read(name)
                if sha256(raw).hexdigest() != expected_sha:
                    raise ValueError("独立引擎运行包内的文件摘要不符")
                file = dest / part
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_bytes(raw)

            (dest / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False) + "\n")
            return external, MAX_UNPACKED_SIZE - size
