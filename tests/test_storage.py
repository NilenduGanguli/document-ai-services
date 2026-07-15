"""Blob-storage tests — pure logic + the filesystem backend (no DB, no network).

The postgres/s3 backends are exercised only under ``@pytest.mark.integration``; everything here
runs against ``tmp_path`` or no I/O at all.
"""
from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from di.config import get_settings
from di.storage import (
    BlobNotFound,
    BlobStoreError,
    LocalBlobStore,
    NoneBlobStore,
    blob_key,
    get_blob_store,
    reset_blob_store,
    sha256_hex,
)

SHA = "ab" * 32
PDF = b"%PDF-1.7\nfake scan bytes\n"


@pytest.fixture
def local_settings(tmp_path: Path):
    """Settings pointing the local backend at an isolated tmp dir."""
    return get_settings().model_copy(
        update={"blob_backend": "local", "blob_local_dir": str(tmp_path / "blobs")}
    )


@pytest.fixture
def local_store(local_settings) -> Iterator[LocalBlobStore]:
    reset_blob_store()
    store = get_blob_store(local_settings)
    assert isinstance(store, LocalBlobStore)
    yield store
    reset_blob_store()


# ---------------------------------------------------------------------------
# blob_key
# ---------------------------------------------------------------------------
def test_blob_key_is_stable() -> None:
    """The same inputs always produce the same key."""
    assert blob_key("acme", SHA, "passport.pdf") == blob_key("acme", SHA, "passport.pdf")
    assert blob_key("acme", SHA, "passport.pdf") == f"{SHA}/passport.pdf"


def test_blob_key_is_tenant_relative() -> None:
    """The key deliberately carries no tenant segment.

    Every backend applies its own tenant scoping (Postgres by client_id column, local by a
    hashed per-client directory, S3 by a prefix it controls), so embedding the client id here
    would only duplicate it into every path (``documents/acme/acme/<sha>/f.pdf``). Tenant
    isolation is asserted against the backends themselves, not against this key.
    """
    assert blob_key("acme", SHA, "passport.pdf") == blob_key("globex", SHA, "passport.pdf")


def test_blob_key_separates_digests_and_names() -> None:
    """Within a tenant, a different payload or filename yields a different key."""
    keys = {
        blob_key("acme", SHA, "passport.pdf"),
        blob_key("acme", "cd" * 32, "passport.pdf"),
        blob_key("acme", SHA, "licence.pdf"),
    }
    assert len(keys) == 3


@pytest.mark.parametrize(
    "filename",
    [
        "../../etc/passwd",
        "..\\..\\windows\\system32\\config\\sam",
        "/etc/shadow",
        "..",
        "....//....//etc/passwd",
        "a b;rm -rf /.pdf",
        "\x00null.pdf",
        "",
    ],
)
def test_blob_key_is_path_safe(filename: str) -> None:
    """No hostile filename can add segments, escape upward, or emit a separator."""
    key = blob_key("acme", SHA, filename)
    segments = key.split("/")
    assert len(segments) == 2, key
    assert segments[0] == SHA
    assert segments[1] not in ("", ".", "..")
    assert ".." not in segments
    assert "\\" not in key and "\x00" not in key


def test_blob_key_ignores_hostile_client_id() -> None:
    """A hostile client_id cannot widen the key's shape — it is not part of the key at all."""
    key = blob_key("../root", SHA, "doc.pdf")
    assert key == f"{SHA}/doc.pdf"
    assert ".." not in key.split("/")


# ---------------------------------------------------------------------------
# local backend
# ---------------------------------------------------------------------------
async def test_local_round_trip(local_store: LocalBlobStore) -> None:
    """put -> get -> delete, with the ref describing the payload."""
    key = blob_key("acme", sha256_hex(PDF), "passport.pdf")
    ref = await local_store.put(
        client_id="acme", key=key, data=PDF, content_type="application/pdf"
    )

    assert ref.backend == "local"
    assert ref.uri.startswith("file:///")
    assert ref.size == len(PDF)
    assert ref.content_type == "application/pdf"
    assert ref.sha256 == sha256_hex(PDF)

    assert await local_store.get(ref.uri, client_id="acme") == PDF
    assert await local_store.delete(ref.uri, client_id="acme") is True
    assert await local_store.delete(ref.uri, client_id="acme") is False
    with pytest.raises(BlobNotFound):
        await local_store.get(ref.uri, client_id="acme")


async def test_local_put_is_idempotent_upsert(local_store: LocalBlobStore) -> None:
    """Re-putting the same key overwrites rather than duplicating."""
    key = blob_key("acme", SHA, "doc.pdf")
    first = await local_store.put(client_id="acme", key=key, data=b"v1")
    second = await local_store.put(client_id="acme", key=key, data=b"v2-longer")

    assert first.uri == second.uri
    assert await local_store.get(second.uri, client_id="acme") == b"v2-longer"
    assert second.size == len(b"v2-longer")


async def test_local_writes_sidecar_metadata(local_store: LocalBlobStore) -> None:
    """The original key/content-type survive alongside the digest-named payload."""
    key = blob_key("acme", SHA, "passport.pdf")
    ref = await local_store.put(
        client_id="acme", key=key, data=PDF, content_type="application/pdf"
    )
    sidecar = Path(ref.uri[len("file://") :] + ".json")
    meta = json.loads(sidecar.read_text())

    assert meta["key"] == key
    assert meta["content_type"] == "application/pdf"
    assert meta["size"] == len(PDF)
    assert meta["sha256"] == sha256_hex(PDF)


async def test_local_get_missing_raises_blob_not_found(local_store: LocalBlobStore) -> None:
    """A well-formed URI for a blob that was never written is a miss, not an error."""
    key = blob_key("acme", SHA, "never-written.pdf")
    path = local_store._blob_path("acme", key)  # noqa: SLF001 - path derivation is the unit here
    with pytest.raises(BlobNotFound):
        await local_store.get(f"file://{path}", client_id="acme")


async def test_local_delete_client(local_store: LocalBlobStore) -> None:
    """Purging a tenant removes all of its blobs and none of anyone else's."""
    acme = [
        await local_store.put(client_id="acme", key=blob_key("acme", SHA, f"d{i}.pdf"), data=PDF)
        for i in range(3)
    ]
    other = await local_store.put(
        client_id="globex", key=blob_key("globex", SHA, "d.pdf"), data=b"keep me"
    )

    assert await local_store.delete_client("acme") == 3
    for ref in acme:
        with pytest.raises(BlobNotFound):
            await local_store.get(ref.uri, client_id="acme")
    assert await local_store.get(other.uri, client_id="globex") == b"keep me"

    assert await local_store.delete_client("acme") == 0
    assert await local_store.delete_client("never-existed") == 0


async def test_local_traversal_key_stays_inside_base(
    local_store: LocalBlobStore, tmp_path: Path
) -> None:
    """A malicious key is neutralized: bytes land inside the tenant tree, nothing escapes."""
    base = (tmp_path / "blobs").resolve()
    ref = await local_store.put(client_id="acme", key="../../etc/passwd", data=b"pwned?")

    written = Path(ref.uri[len("file://") :])
    assert written.resolve().is_relative_to(base)
    assert await local_store.get(ref.uri, client_id="acme") == b"pwned?"
    # Nothing was created outside the base dir.
    assert not (tmp_path / "etc").exists()
    assert sorted(p.name for p in base.parent.iterdir()) == ["blobs"]


async def test_local_traversal_key_is_not_the_path(local_store: LocalBlobStore) -> None:
    """The on-disk name is a digest of the key, so key text never appears in the path."""
    ref = await local_store.put(client_id="acme", key="../../etc/passwd", data=b"x")
    assert "etc" not in ref.uri
    assert "passwd" not in ref.uri
    assert ".." not in ref.uri


async def test_local_rejects_uri_outside_base(local_store: LocalBlobStore) -> None:
    """A hand-crafted URI pointing out of the volume is refused, not read."""
    with pytest.raises(BlobStoreError):
        await local_store.get("file:///etc/passwd", client_id="acme")
    with pytest.raises(BlobStoreError):
        await local_store.delete("file:///etc/passwd", client_id="acme")


async def test_local_rejects_cross_tenant_uri(local_store: LocalBlobStore) -> None:
    """One tenant cannot read another tenant's blob by presenting its URI."""
    ref = await local_store.put(
        client_id="acme", key=blob_key("acme", SHA, "secret.pdf"), data=b"acme only"
    )
    with pytest.raises(BlobStoreError):
        await local_store.get(ref.uri, client_id="globex")


async def test_local_rejects_foreign_scheme(local_store: LocalBlobStore) -> None:
    """URIs from another backend are refused."""
    with pytest.raises(BlobStoreError):
        await local_store.get("pg://acme/some/key", client_id="acme")


async def test_local_health(local_store: LocalBlobStore) -> None:
    """Health creates the base dir on demand and reports it writable."""
    health = await local_store.health()
    assert health["backend"] == "local"
    assert health["ok"] is True
    assert isinstance(health["detail"], str)


# ---------------------------------------------------------------------------
# none backend
# ---------------------------------------------------------------------------
async def test_none_backend_discards_bytes() -> None:
    """put reports the payload but retains nothing; reads always miss."""
    store = NoneBlobStore()
    ref = await store.put(
        client_id="acme", key="acme/k/doc.pdf", data=PDF, content_type="application/pdf"
    )

    assert ref.uri == ""
    assert ref.backend == "none"
    assert ref.size == len(PDF)
    assert ref.sha256 == sha256_hex(PDF)

    with pytest.raises(BlobNotFound):
        await store.get(ref.uri, client_id="acme")
    assert await store.delete(ref.uri, client_id="acme") is False
    assert await store.delete_client("acme") == 0
    assert (await store.health())["ok"] is True


# ---------------------------------------------------------------------------
# factory
# ---------------------------------------------------------------------------
def test_get_blob_store_is_cached_and_resettable(local_settings) -> None:
    """The store is a singleton until reset."""
    reset_blob_store()
    try:
        first = get_blob_store(local_settings)
        assert get_blob_store(local_settings) is first
        reset_blob_store()
        assert get_blob_store(local_settings) is not first
    finally:
        reset_blob_store()


@pytest.mark.parametrize(
    ("backend", "cls_name"),
    [
        ("postgres", "PostgresBlobStore"),
        ("local", "LocalBlobStore"),
        ("s3", "S3BlobStore"),
        ("none", "NoneBlobStore"),
    ],
)
def test_get_blob_store_selects_backend(backend: str, cls_name: str, tmp_path: Path) -> None:
    """Every backend name builds without touching its dependency (s3 does not need boto3)."""
    settings = get_settings().model_copy(
        update={"blob_backend": backend, "blob_local_dir": str(tmp_path)}
    )
    reset_blob_store()
    try:
        store = get_blob_store(settings)
        assert type(store).__name__ == cls_name
        assert store.backend == backend
    finally:
        reset_blob_store()


def test_get_blob_store_rejects_unknown_backend() -> None:
    """An unknown backend name fails loudly at construction."""
    settings = get_settings().model_copy(update={"blob_backend": "dropbox"})
    reset_blob_store()
    try:
        with pytest.raises(BlobStoreError, match="unknown blob_backend"):
            get_blob_store(settings)
    finally:
        reset_blob_store()


# ---------------------------------------------------------------------------
# integration — postgres (live DB + migration 005) and s3 (live MinIO/S3)
# ---------------------------------------------------------------------------
@pytest.mark.integration
async def test_postgres_round_trip() -> None:
    """put/get/delete/delete_client against a live di_blob table."""
    from di.storage import PostgresBlobStore

    store = PostgresBlobStore()
    key = blob_key("acme-test", sha256_hex(PDF), "passport.pdf")
    ref = await store.put(
        client_id="acme-test", key=key, data=PDF, content_type="application/pdf"
    )

    assert ref.uri == f"pg://acme-test/{key}"
    assert await store.get(ref.uri, client_id="acme-test") == PDF

    # Upsert, not duplicate.
    await store.put(client_id="acme-test", key=key, data=b"v2")
    assert await store.get(ref.uri, client_id="acme-test") == b"v2"

    assert await store.delete(ref.uri, client_id="acme-test") is True
    assert await store.delete(ref.uri, client_id="acme-test") is False
    with pytest.raises(BlobNotFound):
        await store.get(ref.uri, client_id="acme-test")


@pytest.mark.integration
async def test_postgres_rejects_cross_tenant_uri() -> None:
    """A URI stamped with another tenant is refused before it reaches SQL."""
    from di.storage import PostgresBlobStore

    store = PostgresBlobStore()
    with pytest.raises(BlobStoreError):
        await store.get("pg://globex/globex/k/doc.pdf", client_id="acme-test")


@pytest.mark.integration
async def test_postgres_delete_client_and_health() -> None:
    """delete_client purges every row for the tenant; health finds the table."""
    from di.storage import PostgresBlobStore

    store = PostgresBlobStore()
    for i in range(3):
        await store.put(client_id="purge-me", key=blob_key("purge-me", SHA, f"d{i}.pdf"), data=PDF)

    assert await store.delete_client("purge-me") == 3
    assert await store.delete_client("purge-me") == 0
    assert (await store.health())["ok"] is True


@pytest.mark.integration
async def test_s3_round_trip_and_purge() -> None:
    """put/get/delete/delete_client against a live MinIO or S3 bucket."""
    from di.storage import S3BlobStore

    settings = get_settings()
    store = S3BlobStore(
        bucket=settings.s3_bucket,
        prefix=settings.s3_prefix,
        endpoint=settings.s3_endpoint,
        region=settings.s3_region,
        access_key=settings.s3_access_key,
        secret_key=settings.s3_secret_key,
    )
    key = blob_key("acme-test", sha256_hex(PDF), "passport.pdf")
    ref = await store.put(client_id="acme-test", key=key, data=PDF, content_type="application/pdf")

    assert ref.uri.startswith(f"s3://{settings.s3_bucket}/")
    assert await store.get(ref.uri, client_id="acme-test") == PDF
    with pytest.raises(BlobStoreError):
        await store.get(ref.uri, client_id="globex")

    assert await store.delete(ref.uri, client_id="acme-test") is True
    assert await store.delete(ref.uri, client_id="acme-test") is False
    with pytest.raises(BlobNotFound):
        await store.get(ref.uri, client_id="acme-test")

    await store.put(client_id="purge-me", key=blob_key("purge-me", SHA, "d.pdf"), data=PDF)
    assert await store.delete_client("purge-me") == 1
    assert (await store.health())["ok"] is True
