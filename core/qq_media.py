"""QQ 图片素材的短时租约、账号隔离与持久化回收账本。"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx


class QQMaterialError(RuntimeError):
    """素材链不可用；调用方应降级原图片发送。"""


@dataclass(frozen=True)
class QQMaterialRef:
    url: str
    width: int
    height: int
    image_id: str
    sha256: str


class QQMaterialLease:
    def __init__(self, store, key, ref):
        self.ref = ref
        self._store, self._key, self._released = store, key, False

    async def release(self, confirmed: bool = True):
        if not self._released:
            self._released = True
            await asyncio.shield(self._store._release(self._key, confirmed))


def _image(path):
    from PIL import Image

    with Path(path).open("rb") as source:
        data = source.read(10 * 1024 * 1024 + 1)
    if not data or len(data) > 10 * 1024 * 1024:
        raise QQMaterialError("图片大小超限")
    try:
        with Image.open(io.BytesIO(data)) as im:
            kind, size = im.format, im.size
            if size[0] * size[1] > 40_000_000:
                raise QQMaterialError("图片像素过大")
            im.verify()
    except Exception:
        raise QQMaterialError("图片格式无效") from None
    formats = {
        "PNG": ("png", "image/png"),
        "JPEG": ("jpg", "image/jpeg"),
        "GIF": ("gif", "image/gif"),
        "WEBP": ("webp", "image/webp"),
    }
    if kind not in formats or min(size) <= 0:
        raise QQMaterialError("不支持的图片格式")
    ext, mime = formats[kind]
    return data, hashlib.sha256(data).hexdigest(), hashlib.md5(data).hexdigest(), size, ext, mime


def _https(value, cos=False):
    if not isinstance(value, str):
        raise QQMaterialError("素材URL无效")
    parts = urlsplit(value)
    host = parts.hostname or ""
    if (
        parts.scheme != "https"
        or not host
        or parts.username
        or parts.password
        or parts.port not in (None, 443)
        or parts.fragment
    ):
        raise QQMaterialError("素材URL不安全")
    if cos and not host.endswith(".myqcloud.com"):
        raise QQMaterialError("上传目标不是腾讯COS")
    return value


class QQMaterialStore:
    def __init__(self, cache_dir: Path, ttl=300, max_items=32):
        self._path = Path(cache_dir) / "qq_material_ownership.json"
        self._ttl, self._max = ttl, max_items
        self._lock = asyncio.Lock()
        self._sweep_lock = asyncio.Lock()
        self._records, self._refs, self._pins, self._apis = {}, {}, {}, {}
        self._task = None
        self._closed = False
        try:
            loaded = json.loads(self._path.read_text())
            for row in loaded.get("items", []):
                # 账本不接受无UUID登记的ID，重启也不触及其他素材。
                uuid.UUID(row["owner"])
                if row.get("account") and "/" not in row.get("image_id", ""):
                    self._records[row["owner"]] = row
        except (OSError, ValueError, TypeError, KeyError):
            pass

    def register_account(self, account: str, api: Any):
        if self._closed:
            raise QQMaterialError("素材管理器已关闭")
        self._apis[str(account)] = api
        if self._task is None:
            self._task = asyncio.create_task(self._maintenance())

    def _save(self):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"items": list(self._records.values())}))
        temporary.replace(self._path)

    async def get(self, account: str, api: Any, path: str) -> QQMaterialLease:
        if not account:
            raise QQMaterialError("QQ账号标识缺失")
        data, sha, md5, size, ext, mime = await asyncio.to_thread(_image, path)
        account = str(account)
        self.register_account(account, api)
        key = (account, sha)
        # 上传也持锁：同图并发只创建一个素材；不同图上传数量受配额限制。
        async with self._lock:
            if self._closed:
                raise QQMaterialError("素材管理器已关闭")
            cached = self._refs.get(key)
            if cached and self._records[cached[0]]["expires"] > time.time():
                self._pins[cached[0]] = self._pins.get(cached[0], 0) + 1
                return QQMaterialLease(self, cached[0], cached[1])
            if len(self._records) >= self._max:
                raise QQMaterialError("本插件素材配额已满")
            owner = str(uuid.uuid4())
            row = {
                "owner": owner,
                "account": account,
                "sha256": sha,
                "filename": f"{owner}.{ext}",
                "expires": time.time() + self._ttl,
                "attempts": 0,
                "retry_at": 0,
            }
            ref = await self._upload(api, row, data, md5, size, mime)
            self._refs[key] = (owner, ref)
            self._pins[owner] = 1
            return QQMaterialLease(self, owner, ref)

    async def _release(self, owner, confirmed):
        async with self._lock:
            self._pins[owner] = max(0, self._pins.get(owner, 0) - 1)
            if not confirmed and owner in self._records:
                # 发送结果不确定时延后回收，防止请求仍在服务端处理。
                self._records[owner]["expires"] = max(
                    self._records[owner]["expires"], time.time() + self._ttl
                )
                self._save()

    async def invalidate(self, account, ref):
        sha = ref.sha256 if isinstance(ref, QQMaterialRef) else str(ref)
        async with self._lock:
            cached = self._refs.pop((str(account), sha), None)
            if cached:
                self._records[cached[0]]["expires"] = 0
                self._save()

    async def _maintenance(self):
        try:
            while True:
                try:
                    await self._sweep()
                except Exception:
                    # 账本磁盘异常等不能让维护任务永久退出；不打印响应或签名URL。
                    pass
                await asyncio.sleep(min(30, max(0.05, self._ttl)))
        except asyncio.CancelledError:
            pass

    async def _sweep(self):
        async with self._sweep_lock:
            await self._sweep_once()

    async def _sweep_once(self):
        # complete响应丢失：仅按预登记的UUID文件名找回本插件素材。
        async with self._lock:
            pending = [
                (owner, row.copy())
                for owner, row in self._records.items()
                if not row.get("image_id")
                and row["expires"] <= time.time()
                and row["account"] in self._apis
                and row.get("attempts", 0) < 5
                and row.get("retry_at", 0) <= time.time()
            ]
        for owner, row in pending:
            try:
                match = await asyncio.wait_for(self._find_pending(row), timeout=60)
                async with self._lock:
                    if (
                        match
                        and isinstance(match.get("image_id"), str)
                        and "/" not in match["image_id"]
                    ):
                        row["image_id"] = match["image_id"]
                        row["expires"] = 0
                    elif match is not None:
                        raise QQMaterialError("恢复素材标识无效")
                    else:
                        # complete超时不代表服务端未创建，列表短暂缺失不能抹掉所有权。
                        # 有界退避后仍保留账本供重载后诊断，不越权删除其他素材。
                        raise QQMaterialError("待确认素材尚未出现在列表中")
                    self._records[owner] = row
                    self._save()
            except Exception:
                async with self._lock:
                    row["attempts"] = row.get("attempts", 0) + 1
                    row["retry_at"] = time.time() + 5 * 2 ** row["attempts"]
                    self._records[owner] = row
                    self._save()
        now = time.time()
        async with self._lock:
            candidates = [
                (owner, row.copy())
                for owner, row in self._records.items()
                if row.get("image_id")
                and row["expires"] <= now
                and not self._pins.get(owner, 0)
                and row.get("retry_at", 0) <= now
                and row.get("attempts", 0) < 5
                and row["account"] in self._apis
            ]
            # 到期条目不会再get命中；先移除缓存后在锁外做网络回收。
            for owner, row in candidates:
                key = (row["account"], row["sha256"])
                if self._refs.get(key, (None,))[0] == owner:
                    self._refs.pop(key, None)
        for owner, row in candidates:
            try:
                await asyncio.wait_for(self._delete(self._apis[row["account"]], row), timeout=15)
            except Exception:
                async with self._lock:
                    row["attempts"] = row.get("attempts", 0) + 1
                    row["retry_at"] = now + min(600, 5 * 2 ** row["attempts"])
                    self._records[owner] = row
                    self._save()
            else:
                async with self._lock:
                    self._records.pop(owner, None)
                    self._pins.pop(owner, None)
                    self._save()

    async def _find_pending(self, row):
        cursor, seen, total = None, set(), 0
        for _ in range(10):
            path = "/v2/media-images"
            if cursor:
                path += "?" + urlencode({"cursor": cursor})
            listed = await self._request(self._apis[row["account"]], "GET", path)
            images = listed.get("images")
            if not isinstance(images, list):
                raise QQMaterialError("素材列表无效")
            total += len(images)
            if total > 100:
                raise QQMaterialError("素材列表超出恢复边界")
            for item in images:
                if item.get("file_name") == row["filename"]:
                    return item
            cursor = listed.get("next_cursor")
            if not cursor:
                return None
            if not isinstance(cursor, str) or cursor in seen:
                raise QQMaterialError("素材分页无效")
            seen.add(cursor)
        raise QQMaterialError("素材列表超出恢复页数")

    async def close(self):
        self._closed = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        async with self._lock:
            for owner, count in self._pins.items():
                if count and owner in self._records:
                    self._records[owner]["expires"] = max(
                        self._records[owner]["expires"], time.time() + self._ttl
                    )
            self._save()

    async def _request(self, api, method, path, **kwargs):
        from botpy.http import Route

        try:
            return await asyncio.wait_for(
                api._http.request(Route(method, path), **kwargs), timeout=25
            )
        except Exception:
            raise QQMaterialError("QQ素材接口调用失败") from None

    async def _upload(self, api, row, data, md5, size, mime):
        prepared = await self._request(
            api,
            "POST",
            "/v2/media-images/prepare",
            json={
                "file_name": row["filename"],
                "file_size": len(data),
                "content_type": mime,
                "md5": md5,
                "sha1": await asyncio.to_thread(lambda: hashlib.sha1(data).hexdigest()),
            },
        )
        upload_id = prepared.get("upload_id")
        if not upload_id:
            raise QQMaterialError("QQ未返回上传标识")
        url = _https(prepared.get("presigned_url"), cos=True)
        # 独立客户端不接收SDK认证信息，也不读取代理/环境凭据。
        try:
            async with httpx.AsyncClient(
                timeout=20, follow_redirects=False, trust_env=False
            ) as client:
                response = await client.put(url, content=data, headers={"Content-Type": mime})
            if response.status_code not in (200, 201, 204):
                raise QQMaterialError("COS上传失败")
            etag = response.headers.get("etag", "").strip('"').lower()
            if etag != md5:
                raise QQMaterialError("COS图片校验失败")
        except QQMaterialError:
            raise
        except Exception:
            raise QQMaterialError("COS上传失败") from None
        # complete可能服务端成功但响应丢失，提前登记唯一文件名供运维恢复。
        row["image_id"] = ""
        self._records[row["owner"]] = row
        self._save()
        completed = await self._request(
            api,
            "POST",
            "/v2/media-images/complete",
            json={
                "upload_id": upload_id,
                "md5": etag,
            },
        )
        image_id = completed.get("image_id")
        if not isinstance(image_id, str) or not image_id or "/" in image_id:
            raise QQMaterialError("QQ素材标识无效")
        row["image_id"] = image_id
        self._save()
        media_url = completed.get("url", "")
        if not isinstance(media_url, str) or not media_url.startswith("qqbot-media://image/v1/"):
            row["expires"] = 0
            self._save()
            raise QQMaterialError("QQ素材引用无效")
        row["expires"] = min(row["expires"], time.time() + float(completed.get("ttl", self._ttl)))
        self._save()
        return QQMaterialRef(media_url, size[0], size[1], image_id, row["sha256"])

    async def _delete(self, api, row):
        await self._request(api, "DELETE", f"/v2/media-images/{row['image_id']}/delete")
