"""QQ素材真实入口契约：不联网，不接触真实账号。"""

import asyncio
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.qq_media import QQMaterialError, QQMaterialStore, _https


class Materials(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.image = self.root / "wrong-extension.txt"
        im = Image.new("RGB", (7, 11), "red")
        im.save(self.image, format="PNG")
        self.store = QQMaterialStore(self.root, ttl=300)
        self.serial = 0
        self.calls = []

        async def request(api, method, path, **kwargs):
            self.calls.append((api, method, path, kwargs))
            if path.endswith("prepare"):
                return {
                    "upload_id": "u",
                    "presigned_url": "https://bucket.cos.ap-shanghai.myqcloud.com/a?signature=private",
                }
            if path.endswith("complete"):
                self.serial += 1
                return {
                    "image_id": str(self.serial),
                    "url": "qqbot-media://image/v1/token",
                    "ttl": 300,
                }
            return {}

        self.store._request = request
        self.client = AsyncMock()
        self.client.__aenter__.return_value = self.client
        self.client.put.return_value.status_code = 200
        self.client.put.return_value.headers = {
            "etag": hashlib.md5(self.image.read_bytes()).hexdigest()
        }
        self.patch = patch("core.qq_media.httpx.AsyncClient", return_value=self.client)
        self.factory = self.patch.start()

    async def asyncTearDown(self):
        await self.store.close()
        self.patch.stop()
        self.tmp.cleanup()

    async def test_account_and_concurrent_dedup(self):
        leases = await asyncio.gather(
            *(self.store.get("a", object(), str(self.image)) for _ in range(8))
        )
        self.assertEqual(self.serial, 1)
        self.assertEqual((leases[0].ref.width, leases[0].ref.height), (7, 11))
        other = await self.store.get("b", object(), str(self.image))
        self.assertNotEqual(other.ref.image_id, leases[0].ref.image_id)
        for lease in leases + [other]:
            await lease.release()
        prepare = [call for call in self.calls if call[2].endswith("prepare")][0][3]["json"]
        self.assertTrue(prepare["file_name"].endswith(".png"))
        self.assertEqual(prepare["content_type"], "image/png")

    async def test_cos_isolated_and_no_url_persistence(self):
        lease = await self.store.get("a", object(), str(self.image))
        self.factory.assert_called_once_with(timeout=20, follow_redirects=False, trust_env=False)
        self.assertEqual(self.client.put.call_args.kwargs["headers"], {"Content-Type": "image/png"})
        ledger = self.store._path.read_text()
        self.assertNotIn("signature", ledger)
        self.assertNotIn("qqbot-media://", ledger)
        await lease.release()

    async def test_pinned_expiration_and_owned_delete(self):
        lease = await self.store.get("a", object(), str(self.image))
        await self.store.invalidate("a", lease.ref)
        await self.store._sweep()
        self.assertFalse(any(c[1] == "DELETE" for c in self.calls))
        await lease.release()
        await self.store._sweep()
        self.assertEqual(
            [c[2] for c in self.calls if c[1] == "DELETE"], ["/v2/media-images/1/delete"]
        )

    async def test_restart_recovers_ownership(self):
        lease = await self.store.get("a", object(), str(self.image))
        await self.store.invalidate("a", lease.ref)
        await lease.release()
        await self.store.close()
        recovered = QQMaterialStore(self.root)
        recovered._request = self.store._request
        recovered.register_account("a", object())
        await recovered._sweep()
        await recovered.close()
        self.assertEqual(json.loads(recovered._path.read_text())["items"], [])

    async def test_bad_etag_and_unsupported_image_fail(self):
        self.client.put.return_value.headers = {"etag": "wrong"}
        with self.assertRaises(QQMaterialError):
            await self.store.get("a", object(), str(self.image))
        self.assertFalse(any(c[2].endswith("complete") for c in self.calls))
        self.image.write_bytes(b"not an image")
        with self.assertRaises(QQMaterialError):
            await self.store.get("a", object(), str(self.image))

    async def test_quota_fails_without_evicting_pinned(self):
        self.store._max = 1
        lease = await self.store.get("a", object(), str(self.image))
        with self.assertRaises(QQMaterialError):
            await self.store.get("b", object(), str(self.image))
        await lease.release()

    async def test_uncertain_complete_recovers_only_owned_filename(self):
        original = self.store._request

        async def uncertain(api, method, path, **kwargs):
            if path.endswith("complete"):
                raise QQMaterialError("response lost")
            return await original(api, method, path, **kwargs)

        self.store._request = uncertain
        with self.assertRaises(QQMaterialError):
            await self.store.get("a", object(), str(self.image))
        row = next(iter(self.store._records.values()))
        await self.store._sweep()
        self.assertEqual(len(self.store._records), 1)
        self.assertFalse(any(c[1] == "DELETE" for c in self.calls))
        row["expires"] = 0

        async def recovered(api, method, path, **kwargs):
            if method == "GET":
                return {
                    "images": [
                        {"image_id": "foreign", "file_name": "other-plugin.png"},
                        {"image_id": "owned", "file_name": row["filename"]},
                    ]
                }
            return await original(api, method, path, **kwargs)

        self.store._request = recovered
        await self.store._sweep()
        self.assertEqual(
            [c[2] for c in self.calls if c[1] == "DELETE"], ["/v2/media-images/owned/delete"]
        )
        self.assertEqual(self.store._records, {})

    async def test_pending_missing_result_keeps_ownership_until_eventual_creation(self):
        original = self.store._request

        async def uncertain(api, method, path, **kwargs):
            if path.endswith("complete"):
                raise QQMaterialError("response lost")
            return await original(api, method, path, **kwargs)

        self.store._request = uncertain
        with self.assertRaises(QQMaterialError):
            await self.store.get("a", object(), str(self.image))
        owner, row = next(iter(self.store._records.items()))
        self.assertGreater(row["expires"], 0)
        row["expires"] = 0

        async def missing(api, method, path, **kwargs):
            if method == "GET":
                return {"images": [{"file_name": "foreign.png", "image_id": "foreign"}]}
            return await original(api, method, path, **kwargs)

        self.store._request = missing
        await self.store._sweep()
        self.assertIn(owner, self.store._records)
        self.assertGreater(self.store._records[owner]["retry_at"], 0)
        self.assertEqual(self.store._records[owner]["attempts"], 1)
        self.store._records[owner]["retry_at"] = 0

        async def appeared(api, method, path, **kwargs):
            if method == "GET":
                return {
                    "images": [
                        {"file_name": "foreign.png", "image_id": "foreign"},
                        {"file_name": row["filename"], "image_id": "eventual"},
                    ]
                }
            return await original(api, method, path, **kwargs)

        self.store._request = appeared
        await self.store._sweep()
        self.assertNotIn(owner, self.store._records)
        self.assertEqual(
            [c[2] for c in self.calls if c[1] == "DELETE"], ["/v2/media-images/eventual/delete"]
        )

    async def test_failed_delete_preserves_ledger_and_backs_off(self):
        lease = await self.store.get("a", object(), str(self.image))
        await self.store.invalidate("a", lease.ref)
        await lease.release()
        original = self.store._request
        deletes = []

        async def failed(api, method, path, **kwargs):
            if method == "DELETE":
                deletes.append(path)
                raise QQMaterialError("temporary failure")
            return await original(api, method, path, **kwargs)

        self.store._request = failed
        await self.store._sweep()
        await self.store._sweep()
        self.assertEqual(len(deletes), 1)
        self.assertEqual(len(json.loads(self.store._path.read_text())["items"]), 1)

    async def test_pending_follows_cursor_and_preserves_on_loop(self):
        row = {"account": "a", "filename": "owned.png"}
        self.store._apis["a"] = object()
        requests = []

        async def listed(api, method, path, **kwargs):
            requests.append(path)
            if "?" not in path:
                return {"images": [], "next_cursor": "next /+"}
            return {"images": [{"file_name": "owned.png", "image_id": "owned"}]}

        self.store._request = listed
        found = await self.store._find_pending(row)
        self.assertEqual(found["image_id"], "owned")
        self.assertEqual(requests[-1], "/v2/media-images?cursor=next+%2F%2B")

        async def loop(*args, **kwargs):
            return {"images": [], "next_cursor": "same"}

        self.store._request = loop
        with self.assertRaises(QQMaterialError):
            await self.store._find_pending(row)

    async def test_maintenance_survives_one_failure(self):
        calls = []
        done = asyncio.Event()

        async def sweep():
            calls.append(1)
            if len(calls) == 1:
                raise OSError("disk busy")
            done.set()

        self.store._ttl = 0.01
        self.store._sweep = sweep
        self.store.register_account("a", object())
        await asyncio.wait_for(done.wait(), timeout=1)
        self.assertFalse(self.store._task.done())

    async def test_jpeg_uses_actual_format_and_close_protects_pin(self):
        Image.new("RGB", (5, 9), "blue").save(self.image, format="JPEG")
        self.client.put.return_value.headers = {
            "etag": hashlib.md5(self.image.read_bytes()).hexdigest()
        }
        lease = await self.store.get("a", object(), str(self.image))
        prepare = next(c[3]["json"] for c in self.calls if c[2].endswith("prepare"))
        self.assertEqual(prepare["content_type"], "image/jpeg")
        self.assertTrue(prepare["file_name"].endswith(".jpg"))
        await self.store.invalidate("a", lease.ref)
        await self.store.close()
        self.assertGreater(json.loads(self.store._path.read_text())["items"][0]["expires"], 0)
        await lease.release()

    def test_unsafe_urls_rejected(self):
        for url in [
            "http://bucket.myqcloud.com/x",
            "https://evil.test/x",
            "https://user:pass@bucket.myqcloud.com/x",
            "https://bucket.myqcloud.com:444/x",
        ]:
            with self.assertRaises(QQMaterialError):
                _https(url, cos=True)


if __name__ == "__main__":
    unittest.main()
