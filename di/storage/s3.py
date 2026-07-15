"""S3 / MinIO blob backend.

Key layout inside the bucket::

    {s3_prefix}/{quoted client_id}/{key}

The tenant prefix is applied by the store, not taken on trust from the key, so ``delete_client``
can purge a tenant with a single prefix scan and a leaked URI cannot be read across tenants. The
client segment is percent-encoded (``quote(..., safe="")``), which is injective — unlike
character-class sanitizing, two distinct tenant ids can never collapse onto the same prefix.

``boto3`` is an optional dependency and is imported lazily *inside* the methods, so importing
``di.storage`` never requires it; a missing install surfaces as a :class:`BlobStoreError` with
install instructions rather than an ImportError at startup. Every boto3 call is blocking, so all
of them run in a worker thread via ``anyio.to_thread.run_sync``. The client is built once, under
a lock, and reused.
"""
from __future__ import annotations

import logging
import threading
from functools import partial
from typing import Any
from urllib.parse import quote

from anyio import to_thread

from di.storage.base import (
    BACKEND_S3,
    BlobNotFound,
    BlobRef,
    BlobStore,
    BlobStoreError,
    sha256_hex,
)

logger = logging.getLogger(__name__)

_URI_SCHEME = "s3://"
_MISSING_BOTO3 = (
    "boto3 is required for blob_backend='s3' but is not installed — "
    "install the optional extra: pip install 'document-intelligence[s3]'"
)
#: S3 DeleteObjects accepts at most 1000 keys per request.
_DELETE_BATCH = 1000
_NOT_FOUND_CODES = frozenset({"404", "NoSuchKey", "NotFound"})


def _import_boto3() -> Any:
    """Import boto3 lazily.

    Raises:
        BlobStoreError: boto3 is not installed.
    """
    try:
        import boto3
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise BlobStoreError(_MISSING_BOTO3) from exc
    return boto3


def _client_error_cls() -> type[Exception]:
    """Return ``botocore.exceptions.ClientError`` lazily."""
    try:
        from botocore.exceptions import ClientError
    except ImportError as exc:  # pragma: no cover - botocore ships with boto3
        raise BlobStoreError(_MISSING_BOTO3) from exc
    return ClientError


def _is_not_found(exc: Exception) -> bool:
    """Return whether a botocore ClientError represents a missing key/bucket."""
    response = getattr(exc, "response", None) or {}
    code = str(response.get("Error", {}).get("Code", ""))
    return code in _NOT_FOUND_CODES


class S3BlobStore(BlobStore):
    """Store blobs as objects in an S3-compatible bucket.

    Args:
        bucket: Target bucket name.
        prefix: Key prefix for every object (``settings.s3_prefix``); may be empty.
        endpoint: Custom endpoint for MinIO / S3-compatible servers; empty means real AWS.
            When set, path-style addressing is used (MinIO has no virtual-host DNS).
        region: AWS region.
        access_key: Static access key; empty falls back to the default credential chain
            (instance profile, env, config file) — preferred in production.
        secret_key: Static secret key.
    """

    backend = BACKEND_S3

    def __init__(
        self,
        *,
        bucket: str,
        prefix: str = "",
        endpoint: str = "",
        region: str = "us-east-1",
        access_key: str = "",
        secret_key: str = "",
    ) -> None:
        self._bucket = bucket
        self._prefix = prefix.strip("/")
        self._endpoint = endpoint
        self._region = region
        self._access_key = access_key
        self._secret_key = secret_key
        self._s3: Any | None = None
        self._lock = threading.Lock()

    # -- client ------------------------------------------------------------
    def _build_client_sync(self) -> Any:
        """Build (once) and return the boto3 S3 client. Runs in a worker thread."""
        with self._lock:
            if self._s3 is not None:
                return self._s3
            boto3 = _import_boto3()
            from botocore.config import Config

            config = Config(
                signature_version="s3v4",
                retries={"max_attempts": 3, "mode": "standard"},
                # MinIO and most S3-compatible servers do not support virtual-host addressing.
                s3={"addressing_style": "path"} if self._endpoint else {},
            )
            kwargs: dict[str, Any] = {"region_name": self._region, "config": config}
            if self._endpoint:
                kwargs["endpoint_url"] = self._endpoint
            if self._access_key and self._secret_key:
                kwargs["aws_access_key_id"] = self._access_key
                kwargs["aws_secret_access_key"] = self._secret_key
            self._s3 = boto3.client("s3", **kwargs)
            return self._s3

    async def _client(self) -> Any:
        """Return the cached boto3 client, building it off the event loop on first use."""
        return await to_thread.run_sync(self._build_client_sync)

    # -- key helpers -------------------------------------------------------
    def _client_prefix(self, client_id: str) -> str:
        """Return the tenant's key prefix (always ends with "/")."""
        tenant = quote(client_id, safe="")
        return f"{self._prefix}/{tenant}/" if self._prefix else f"{tenant}/"

    def _full_key(self, client_id: str, key: str) -> str:
        """Namespace ``key`` under the tenant prefix."""
        return f"{self._client_prefix(client_id)}{key.lstrip('/')}"

    def _key_from_uri(self, uri: str, client_id: str) -> str:
        """Parse ``s3://{bucket}/{key}``, verifying bucket and tenant ownership.

        Raises:
            BlobStoreError: Wrong scheme/bucket, or the key is outside the tenant's prefix.
        """
        if not uri.startswith(_URI_SCHEME):
            raise BlobStoreError(f"not an s3 blob uri: {uri!r}")
        bucket, _, key = uri[len(_URI_SCHEME) :].partition("/")
        if not bucket or not key:
            raise BlobStoreError(f"malformed s3 blob uri: {uri!r}")
        if bucket != self._bucket:
            raise BlobStoreError(f"blob uri bucket {bucket!r} is not the configured bucket")
        if not key.startswith(self._client_prefix(client_id)):
            raise BlobStoreError(f"blob uri does not belong to tenant {client_id!r}")
        return key

    # -- blocking helpers (run in a worker thread) -------------------------
    @staticmethod
    def _get_sync(client: Any, bucket: str, key: str) -> bytes:
        response = client.get_object(Bucket=bucket, Key=key)
        body = response["Body"]
        try:
            return bytes(body.read())
        finally:
            body.close()

    @staticmethod
    def _delete_sync(client: Any, bucket: str, key: str) -> bool:
        """Delete one object, reporting whether it existed (S3 delete is idempotent)."""
        client_error = _client_error_cls()
        try:
            client.head_object(Bucket=bucket, Key=key)
        except client_error as exc:
            if _is_not_found(exc):
                return False
            raise
        client.delete_object(Bucket=bucket, Key=key)
        return True

    @staticmethod
    def _purge_sync(client: Any, bucket: str, prefix: str) -> int:
        """Paginate the tenant prefix and batch-delete every object under it."""
        deleted = 0
        paginator = client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            keys = [{"Key": obj["Key"]} for obj in page.get("Contents", [])]
            for start in range(0, len(keys), _DELETE_BATCH):
                batch = keys[start : start + _DELETE_BATCH]
                response = client.delete_objects(
                    Bucket=bucket, Delete={"Objects": batch, "Quiet": True}
                )
                errors = response.get("Errors") or []
                if errors:
                    raise BlobStoreError(f"s3 purge failed for {len(errors)} object(s): {errors}")
                deleted += len(batch)
        return deleted

    # -- BlobStore ---------------------------------------------------------
    async def put(
        self, *, client_id: str, key: str, data: bytes, content_type: str | None = None
    ) -> BlobRef:
        """Upload the payload under the tenant prefix (S3 PUT is an overwrite)."""
        client = await self._client()
        full_key = self._full_key(client_id, key)
        digest = sha256_hex(data)
        kwargs: dict[str, Any] = {
            "Bucket": self._bucket,
            "Key": full_key,
            "Body": data,
            "Metadata": {"sha256": digest, "client_id": client_id},
        }
        if content_type:
            kwargs["ContentType"] = content_type
        try:
            await to_thread.run_sync(partial(client.put_object, **kwargs))
        except Exception as exc:  # noqa: BLE001 - botocore raises many shapes; normalize
            raise BlobStoreError(f"s3 blob write failed: {exc}") from exc
        return BlobRef(
            uri=f"{_URI_SCHEME}{self._bucket}/{full_key}",
            backend=self.backend,
            size=len(data),
            content_type=content_type,
            sha256=digest,
        )

    async def get(self, uri: str, *, client_id: str) -> bytes:
        """Download the object at ``uri``."""
        key = self._key_from_uri(uri, client_id)
        client = await self._client()
        try:
            return await to_thread.run_sync(partial(self._get_sync, client, self._bucket, key))
        except Exception as exc:  # noqa: BLE001 - normalize botocore errors
            if _is_not_found(exc):
                raise BlobNotFound(f"no blob at {uri}") from exc
            raise BlobStoreError(f"s3 blob read failed: {exc}") from exc

    async def delete(self, uri: str, *, client_id: str) -> bool:
        """Delete the object at ``uri``."""
        key = self._key_from_uri(uri, client_id)
        client = await self._client()
        try:
            return await to_thread.run_sync(partial(self._delete_sync, client, self._bucket, key))
        except Exception as exc:  # noqa: BLE001 - normalize botocore errors
            raise BlobStoreError(f"s3 blob delete failed: {exc}") from exc

    async def delete_client(self, client_id: str) -> int:
        """Purge every object under the tenant's prefix."""
        client = await self._client()
        prefix = self._client_prefix(client_id)
        try:
            return await to_thread.run_sync(
                partial(self._purge_sync, client, self._bucket, prefix)
            )
        except BlobStoreError:
            raise
        except Exception as exc:  # noqa: BLE001 - normalize botocore errors
            raise BlobStoreError(f"s3 blob purge failed: {exc}") from exc

    async def health(self) -> dict[str, Any]:
        """HEAD the bucket to confirm reachability and credentials."""
        try:
            client = await self._client()
            await to_thread.run_sync(partial(client.head_bucket, Bucket=self._bucket))
        except Exception as exc:  # noqa: BLE001 - health must never raise
            return {"backend": self.backend, "ok": False, "detail": str(exc)}
        endpoint = self._endpoint or "aws"
        return {
            "backend": self.backend,
            "ok": True,
            "detail": f"bucket={self._bucket} endpoint={endpoint}",
        }
